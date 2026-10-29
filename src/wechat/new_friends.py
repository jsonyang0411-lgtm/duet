"""New friends of this WeChat account, recognised in its database (Windows, WCDB backend).

WeChat writes into the private chat when two people become friends (WeChat 4.1.15.36, read with
tools/friend_probe.py on the real machine, 2026-10-27):

- someone added us and we accepted on the PC: their greeting (the verify text) as their own
  type-1 message, then the system line (type 10000) 「你已添加了X，现在可以开始聊天了。」;
- the same, accepted on the phone: 「你已添加了X，以上是打招呼的消息。」;
- we asked and they accepted: our greeting (ours), then a type-1 line from them
  「我通过了你的朋友验证请求，现在我们可以开始聊天了」 (WeChat's notice, not their words), then
  「以上是打招呼的消息」 and no 「你已添加了」.

``NewFriendService`` looks every SCAN_SEC seconds at the private chats whose session changed
since its last look (beside the poll loop: through the backend's client and its lock, the
newest READ_MESSAGES messages of each, at most MAX_CHATS_PER_SCAN chats a look), finds those
lines and reports each new friend once (``NewFriendStore`` in the bot's database; the first
look ever sets a start marker, so friends from before it are never reported):

- PRM_EDGE: a ``friend_added`` event for the Mac (``EdgeService.friend_added``);
- with ADMIN_WXID: a private message to the admin (``admin_notice``: who, and how they came);
- PRM_COPILOT: 登记达人's candidates in the console put new friends first, with their verify
  text (masked) and how they were added; registering one hands her messages since she was
  added to PRM as one reviewed draft job (``registered`` -> the backend's ``catch_up_chat``
  -> ``PrmService.take_new_friend``).  Registering stays a person's decision.

How a friend came (``how``): ``requested`` for 「我通过了你的朋友验证请求」; ``auto_accept`` when
the bot itself accepted the request a few minutes before the line (``AcceptLog``, fed by
FriendManager.accept_friend_requests: AUTO_ACCEPT_FRIENDS or the admin's 同意好友); ``manual``
for 「你已添加了」 otherwise; ``unknown`` when that cannot be told.

The verify text may hold a phone number: it goes only into the edge's event (deleted with the
events after EDGE_KEEP_DAYS) or, masked, into the console's list (cleared after NEW_DAYS); never
into the chat store or a log.  Errors are logged and the next look tries again: nothing here
stops the bot or holds the poll loop for longer than one database call.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

SCAN_SEC = 60                # how often the session list is looked at
FIRST_SCAN_SEC = 15          # the first look after start (the backend opens the database first)
READ_MESSAGES = 30           # newest messages read of a changed private chat
MAX_CHATS_PER_SCAN = 100     # changed chats read per look (the rest in the next one)
SESSIONS_MAX_AGE = 20        # the poll loop's session list (read every minute) is used when this fresh
MATCH_SEC = 300              # the bot's accept counts for a 「你已添加了」 line up to this long before it
SKEW_SEC = 30                # ... or this long after it (the two clocks)
NEW_DAYS = 7                 # a friend is "new" in the console this long; then the masked verify text goes
ACCEPT_KEEP_SEC = 86400      # the bot's accepts are remembered this long
CATCH_UP_FLOOR_SEC = 300     # PRM_CATCHUP_HOURS=0: only the last 5 minutes (relay MAX_AGE_SEC)
VERIFY_MAX_LINES = 5
VERIFY_MAX_CHARS = 500
VERIFY_WINDOW_SEC = 7 * 86400   # a greeting is at most this much older than the line (older: earlier talk)

AUTO_ACCEPT, MANUAL, REQUESTED, UNKNOWN = "auto_accept", "manual", "requested", "unknown"
HOWS = (AUTO_ACCEPT, MANUAL, REQUESTED, UNKNOWN)
HOW_LABELS = {AUTO_ACCEPT: "机器人自动通过的", MANUAL: "手动通过的", REQUESTED: "对方通过了我们的申请",
              UNKNOWN: "说不清是怎么加的"}



def admin_notice(event: dict) -> str:
    """The admin's message for a new friend: the remark (else the name, else the wxid's start) and how
    they came.  The verify text is left out (it may hold a phone number)."""
    remark = str(event.get("remark") or "").strip()
    name = str(event.get("name") or "").strip()
    who = remark or name or f"{str(event.get('wxid') or '')[:8]}…"
    if remark and name and name != remark:
        who = f"{remark}（微信名 {name}）"
    how = event.get("how")
    if how == REQUESTED:
        return f"新好友：{who} 通过了我们的好友申请，可以跟进了。"
    return f"新好友：{who}（{HOW_LABELS.get(how, HOW_LABELS[UNKNOWN])}）。"

# Which line said so (kept as ``kind``): accepted on the PC / on the phone / 「你已添加了」 of another
# wording / they accepted our request / only 「以上是打招呼的消息」.
PC, PHONE, ADDED_OTHER, PASSED, GREETING_ONLY = "pc", "phone", "added", "requested", "greeting"

ADDED_MARK = "你已添加了"
NOW_CHAT_MARK = "现在可以开始聊天了"
GREETING_MARK = "以上是打招呼的"
PASSED_MARK = "我通过了你的朋友验证请求"
PASSED_SYSTEM_MARK = "通过了你的朋友验证请求"
FILLER = "ㅤ"            # HANGUL FILLER: "ㅤ", sent to see whether one was deleted
SYSTEM_TYPE = 10000
SAME_EVENT_SEC = 120         # 「以上是打招呼的消息」 this soon after the other line is the same event
_PASSED_LINE = re.compile(r"^我通过了你的朋友验证请求[，,]?现在我们可以开始聊天了[。.!！]?$")
_ADDED_NAME = re.compile(r"你已添加了(?P<name>.+?)[，,]\s*(?:现在可以开始聊天了|以上是打招呼的)")
_ADDED_NAME_LOOSE = re.compile(r"你已添加了(?P<name>[^，,。.]+)")
# WeChat's own chats that are no person (besides wcdb_backend._SPECIAL_PRIVATE_TALKERS).
_WECHAT_CHATS = frozenset({"brandsessionholder", "brandservicesessionholder", "opencustomerservicemsg",
                           "conversationboxservice", "lbsapp", "shakeapp", "voipapp", "masssendapp", "blogapp",
                           "readerapp", "cardpackage", "qqfriend", "linkedinplugin", "facebookapp", "googlecontact",
                           "voicevoipapp", "voiceinputapp", "meishiapp", "topstoryapp", "downloaderapp"})
_TIME_KEYS = ("sort_timestamp", "sortTimestamp", "last_timestamp", "lastTimestamp", "timestamp",
              "last_msg_time", "lastMsgTime")


# ── the lines ──────────────────────────────────────────────────────────────

def _clean(text) -> str:
    return "".join(str(text or "").split()).lower()


def _first(d: dict, *keys, default=None):
    for key in keys:
        if d.get(key) not in (None, ""):
            return d[key]
    return default


def is_me(sender: str, me: str) -> bool:
    """Did this account write it?  ``myWxid`` comes from WeChat 4's data folder, which carries a
    suffix (wxid_abc_b6ce) a message's sender does not (wxid_abc); tools/friend_probe.py is_me."""
    sender, me = str(sender or ""), str(me or "")
    return bool(sender and me) and (sender == me or me.startswith(sender + "_") or sender.startswith(me + "_"))


def is_person_chat(username: str, me: str = "") -> bool:
    """A one-to-one chat with a person: not a group (@chatroom), WeCom (@openim) or any other id with
    an @, an official account (gh_), one of WeChat's own chats (filehelper, weixin, ...) or this
    account itself.  A person's id need not start with wxid_ (an old account's custom id)."""
    from .wcdb_backend import _SPECIAL_PRIVATE_TALKERS

    username = str(username or "").strip()
    lowered = username.lower()
    if not username or "@" in username or lowered.startswith("gh_"):
        return False
    if lowered in _SPECIAL_PRIVATE_TALKERS or lowered in _WECHAT_CHATS:
        return False
    return not is_me(username, me)


def is_filler(text: str) -> bool:
    """Only U+3164 fillers and blanks: someone checking whether we deleted them, not words."""
    return not str(text or "").replace(FILLER, "").strip()


def _passed_line(text: str) -> bool:
    """The line WeChat writes for the other side when they accept our request (exactly that line:
    a creator's own sentence about it is hers)."""
    return bool(_PASSED_LINE.match("".join(str(text or "").split())))


def is_friend_notice(msg: dict) -> bool:
    """One of WeChat's lines about becoming friends, in a standardized message
    (wcdb_backend._standardize): 「你已添加了X…」, 「以上是打招呼的消息」 (system lines) and the line
    WeChat writes in the other party's name when they accept our request,
    「我通过了你的朋友验证请求，现在我们可以开始聊天了」.  Never anyone's words: PRM does not draft it,
    the edge does not forward it as a creator's message."""
    try:
        msg_type = int(msg.get("msg_type", 1) or 1)
    except (TypeError, ValueError):
        msg_type = 1
    content = re.sub(r"^\s*wxid_[a-zA-Z0-9_]+:\s*", "", str(msg.get("content", "") or ""))
    if msg_type == 1:
        return _passed_line(content)
    if msg_type == SYSTEM_TYPE:
        special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
        text = f"{special.get('title') or ''} {content}"
        return any(mark in text for mark in (ADDED_MARK, GREETING_MARK, PASSED_SYSTEM_MARK, NOW_CHAT_MARK))
    return False


def _msg_type(msg: dict) -> int:
    from .wcdb_backend import _message_local_type
    return _message_local_type(msg)


def _msg_time(msg: dict) -> float:
    try:
        value = float(_first(msg, "create_time", "createTime", "timestamp", default=0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000 if value > 1e11 else value


def _msg_text(msg: dict, msg_type: int) -> str:
    from .wcdb_backend import _decompress_zstd_hex, _special_info

    content = str(_first(msg, "message_content", "content", default="") or "")
    if msg_type == SYSTEM_TYPE:
        return str(_special_info(content, SYSTEM_TYPE).get("title") or "")
    if msg_type == 1:
        return " ".join((_decompress_zstd_hex(content) or content).split())
    return ""


def _sender(msg: dict) -> str:
    return str(_first(msg, "sender_username", "senderUsername", "sender", default="") or "")


def session_time(session: dict) -> Optional[float]:
    """When a session last changed (seconds), or None when the row has no time."""
    value = _first(session, *_TIME_KEYS)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 1000 if value > 1e11 else value


def _session_stamp(session: dict, at: Optional[float]):
    if at is not None:
        return at
    return json.dumps(session, sort_keys=True, ensure_ascii=False, default=str)


def _added_name(text: str) -> str:
    match = _ADDED_NAME.search(text) or _ADDED_NAME_LOOSE.search(text)
    return match.group("name").strip().strip("“”\"「」") if match else ""


@dataclass
class Added:
    """A chat's newest becoming-friends line: ``kind`` (PC / PHONE / ADDED_OTHER / PASSED /
    GREETING_ONLY), the name in 「你已添加了X」, its time, and the other party's greeting before it
    (``(time, text)``, oldest first; only for 「你已添加了」)."""

    wxid: str
    kind: str
    at: float
    name: str = ""
    verify: list = field(default_factory=list)

    @property
    def verify_text(self) -> str:
        return "\n".join(text for _, text in self.verify)[:VERIFY_MAX_CHARS]

    @property
    def verify_from(self) -> Optional[float]:
        return min((at for at, _ in self.verify), default=None)


def find_added(messages: Iterable[dict], wxid: str, me: str = "") -> Optional[Added]:
    """The newest line in ``messages`` (raw WCDB messages of one private chat, any order) saying
    this account and ``wxid`` became friends, or None (a chat of old friends, or one with only
    U+3164 fillers in it)."""
    items = sorted((m for m in messages or [] if isinstance(m, dict)),
                   key=lambda m: (_msg_time(m), int(_first(m, "local_id", "localId", default=0) or 0)))
    lines = []
    for m in items:
        msg_type = _msg_type(m)
        lines.append((msg_type, _msg_time(m), _sender(m), _msg_text(m, msg_type)))
    found: Optional[tuple] = None                 # (index, kind, name)
    for i, (msg_type, at, sender, text) in enumerate(lines):
        if msg_type == SYSTEM_TYPE and ADDED_MARK in text:
            kind = PHONE if GREETING_MARK in text else PC if NOW_CHAT_MARK in text else ADDED_OTHER
            found = (i, kind, _added_name(text))
        elif msg_type == 1 and not is_me(sender, me) and _passed_line(text):
            found = (i, PASSED, "")
        elif msg_type == SYSTEM_TYPE and GREETING_MARK in text:
            if found is not None and 0 <= at - lines[found[0]][1] <= SAME_EVENT_SEC:
                continue                          # the end of the line just found
            found = (i, GREETING_ONLY, "")
    if found is None:
        return None
    index, kind, name = found
    added = Added(wxid=wxid, kind=kind, at=lines[index][1], name=name)
    if kind in (PC, PHONE, ADDED_OTHER):
        verify = []
        for msg_type, at, sender, text in reversed(lines[:index]):
            if (msg_type != 1 or is_me(sender, me) or _passed_line(text) or len(verify) >= VERIFY_MAX_LINES
                    or added.at - at > VERIFY_WINDOW_SEC):
                break
            if not is_filler(text):
                verify.append((at, text.replace(FILLER, "").strip()))
        added.verify = list(reversed(verify))
    return added


# ── what the bot itself accepted ───────────────────────────────────────────

class AcceptLog:
    """What the bot itself accepted (FriendManager.accept_friend_requests: AUTO_ACCEPT_FRIENDS or the
    admin's 同意好友) and when, to tell a friend the bot let in (``auto_accept``) from one a person
    let in.  The times are kept in the store (a restart in between still knows them); what OCR read
    on the request's row only in memory: it may hold the greeting, so it is never written down or
    logged."""

    def __init__(self, store: Optional["NewFriendStore"] = None, clock: Callable[[], float] = time.time):
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._items: list[dict] = []
        if store is not None:
            try:
                self._items = [{"id": r["id"], "at": float(r["at"]), "used": bool(r["used"]), "label": ""}
                               for r in store.accepts(clock() - ACCEPT_KEEP_SEC)]
            except Exception:
                logger.warning("New friends: the bot's earlier accepts could not be read", exc_info=True)

    def note(self, at: Optional[float] = None, label: str = "") -> None:
        """The bot accepted a request (``label``: the text OCR read beside its button)."""
        at = self._clock() if at is None else float(at)
        accept_id = None
        if self._store is not None:
            try:
                accept_id = self._store.add_accept(at)
            except Exception:
                logger.warning("New friends: an accept could not be written down", exc_info=True)
        with self._lock:
            self._items.append({"id": accept_id, "at": at, "used": False, "label": str(label or "")})
            self._items = [i for i in self._items if i["at"] >= at - ACCEPT_KEEP_SEC]

    def judge(self, name: str, at: float, *, phone: bool = False) -> str:
        """How a friend whose 「你已添加了」 line came at ``at`` was let in: ``auto_accept`` when the
        bot accepted a request between MATCH_SEC before and SKEW_SEC after it (the one whose row
        showed ``name`` first; one accept counts for one friend), ``manual`` when it did not, and
        ``unknown`` when it did but that accept is not hers for sure (it counted for another friend
        already, or OCR read another name on its row).  ``phone``: the line says it was accepted
        on the phone, where the bot never clicks; only a row with her name makes it the bot's."""
        key = _clean(name)
        with self._lock:
            window = [i for i in self._items if at - MATCH_SEC <= i["at"] <= at + SKEW_SEC]
            free = [i for i in window if not i["used"]]
            named = [i for i in free if key and i["label"] and key in _clean(i["label"])]
            unlabelled = [i for i in free if not i["label"]]
            pick = named[0] if named else (None if phone or not unlabelled else min(unlabelled, key=lambda i: i["at"]))
            if pick is None:
                return UNKNOWN if window and not phone else MANUAL
            pick["used"] = True
            accept_id = pick["id"]
        if accept_id is not None and self._store is not None:
            try:
                self._store.use_accept(accept_id)
            except Exception:
                logger.warning("New friends: an accept could not be marked used", exc_info=True)
        return AUTO_ACCEPT


# ── the bot's database ─────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS new_friends (
    wxid        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    remark      TEXT NOT NULL DEFAULT '',
    how         TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    added_at    REAL NOT NULL,
    verify_from REAL,
    verify_text TEXT NOT NULL DEFAULT '',
    found_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_new_friends_found ON new_friends(found_at);
CREATE TABLE IF NOT EXISTS new_friend_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friend_accepts (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at   REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(path: str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _record_out(row) -> dict:
    out = dict(row)
    out["how_label"] = HOW_LABELS.get(out.get("how"), HOW_LABELS[UNKNOWN])
    return out


class NewFriendStore:
    """The new friends found (once per wxid), the start marker and how far the session list was
    read (``new_friend_state``), the bot's accepts (times only): in the bot's database (DB_PATH),
    on a connection of its own."""

    def __init__(self, path: str = "data/messages.db"):
        self._conn = _connect(path)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # the start marker and the session mark
    def _state(self, key: str) -> Optional[float]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM new_friend_state WHERE key = ?", (key,)).fetchone()
        try:
            return float(row["value"]) if row else None
        except (TypeError, ValueError):
            return None

    def _set_state(self, key: str, value: float) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO new_friend_state (key, value) VALUES (?, ?)", (key, repr(value)))
            self._conn.commit()

    def started_at(self) -> Optional[float]:
        """The start marker: lines from before it are never reported (None: never started)."""
        return self._state("started_at")

    def start(self, at: float) -> None:
        self._set_state("started_at", at)
        self._set_state("mark", at)

    def mark(self) -> Optional[float]:
        """The newest session time read (the next look reads the sessions changed after it)."""
        return self._state("mark")

    def set_mark(self, at: float) -> None:
        self._set_state("mark", at)

    # the friends
    def known(self, wxid: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM new_friends WHERE wxid = ?", (wxid,)).fetchone() is not None

    def add(self, *, wxid: str, name: str, remark: str, how: str, kind: str, added_at: float,
            verify_from: Optional[float], verify_text: str, found_at: float) -> bool:
        """Write a new friend down; False when this wxid was reported already."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO new_friends (wxid, name, remark, how, kind, added_at, verify_from,"
                " verify_text, found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (wxid, name, remark, how, kind, added_at, verify_from, verify_text, found_at))
            self._conn.commit()
            return cur.rowcount == 1

    def get(self, wxid: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM new_friends WHERE wxid = ?", (wxid,)).fetchone()
        return _record_out(row) if row else None

    def recent(self, since: float) -> list[dict]:
        """The friends found since ``since``, newest first."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM new_friends WHERE found_at >= ? ORDER BY found_at DESC, added_at DESC",
                                      (since,)).fetchall()
        return [_record_out(r) for r in rows]

    def forget_verify(self, before: float) -> int:
        """Clear the (masked) verify text of friends found before ``before``."""
        with self._lock:
            cur = self._conn.execute("UPDATE new_friends SET verify_text = '' WHERE found_at < ? AND verify_text != ''",
                                     (before,))
            self._conn.commit()
            return cur.rowcount

    # the bot's accepts
    def add_accept(self, at: float) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO friend_accepts (at) VALUES (?)", (at,))
            self._conn.execute("DELETE FROM friend_accepts WHERE at < ?", (at - ACCEPT_KEEP_SEC,))
            self._conn.commit()
            return int(cur.lastrowid)

    def accepts(self, since: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM friend_accepts WHERE at >= ? ORDER BY at", (since,)).fetchall()
        return [dict(r) for r in rows]

    def use_accept(self, accept_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE friend_accepts SET used = 1 WHERE id = ?", (int(accept_id),))
            self._conn.commit()


def recent_new_friends(db_path: str, days: int = NEW_DAYS, now: Optional[float] = None) -> list[dict]:
    """The console's look at the bot's database while the bot is stopped: the friends found in the
    last ``days`` days, newest first ([] when there is no database or table yet; nothing is created)."""
    now = time.time() if now is None else now
    if not db_path or not Path(db_path).is_file():
        return []
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return []
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'new_friends'").fetchone() is None:
            return []
        rows = conn.execute("SELECT * FROM new_friends WHERE found_at >= ? ORDER BY found_at DESC, added_at DESC",
                            (now - days * 86400,)).fetchall()
        return [_record_out(r) for r in rows]
    except sqlite3.Error:
        logger.warning("New friends: the bot's database could not be read", exc_info=True)
        return []
    finally:
        conn.close()


# ── the service ────────────────────────────────────────────────────────────

class NewFriendService:
    """Looks for new friends beside the WCDB backend's poll loop (see the module's doc).

    ``backend`` offers ``with_client(fn)`` (``fn(client)`` under the client lock, None while the
    database is not open), ``own_wxid()``, and optionally ``recent_sessions(max_age)`` (the poll
    loop's last session list), ``note_private_name(wxid, name)`` and ``catch_up_chat(talker, name,
    since, sink)``.  ``on_found``: called with each new friend's event fields (``wxid``, ``name``,
    ``remark``, ``verify_text``, ``how``; the WeChat edge's ``friend_added``); one that raises is
    tried again at the next look.  ``keep_verify`` (PRM_COPILOT): the verify text is kept, masked,
    for the console."""

    def __init__(self, backend, store: NewFriendStore, *, accepts: Optional[AcceptLog] = None,
                 on_found: Iterable[Callable[[dict], object]] = (), keep_verify: bool = False,
                 catchup_hours: int = 12, clock: Callable[[], float] = time.time,
                 interval: float = SCAN_SEC, first_wait: float = FIRST_SCAN_SEC, pause: float = 0.02):
        self._backend = backend
        self._store = store
        self._clock = clock
        self.accepts = accepts if accepts is not None else AcceptLog(store, clock)
        self._on_found = list(on_found)
        self._keep_verify = bool(keep_verify)
        try:
            hours = min(max(int(catchup_hours), 0), 72)
        except (TypeError, ValueError):
            hours = 12
        self._catchup_sec = max(hours * 3600, CATCH_UP_FLOOR_SEC)
        self._interval = max(1.0, float(interval))
        self._first_wait = max(0.0, float(first_wait))
        self._pause = max(0.0, float(pause))
        self._catch_up_sink: Optional[Callable[..., object]] = None
        self._stamps: dict[str, object] = {}       # chat -> its session's time (or row) when last read
        self._retry: dict[str, dict] = {}          # chat -> an event a sink could not take yet
        self._reported: set[str] = set()           # this run's reports (the store may fail to write)
        self._unreadable: set[str] = set()         # chats whose last read failed (said once in the log)
        self._failing = False
        self._last_forget = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._scan_lock = threading.Lock()

    @property
    def store(self) -> NewFriendStore:
        return self._store

    def set_catch_up_sink(self, sink: Optional[Callable[..., object]]) -> None:
        """``sink(wxid, messages, verify_until=...)``: PRM_COPILOT's PrmService.take_new_friend."""
        self._catch_up_sink = sink

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="new-friends", daemon=True)
        self._thread.start()
        logger.info("New friends: looking every %.0f s", self._interval)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        wait = self._first_wait
        while not self._stop.wait(wait):
            self.scan_once()
            wait = self._interval

    # ── one look ──────────────────────────────────────────────────────────

    def scan_once(self) -> list[dict]:
        """Look once; the new friends reported (never raises)."""
        with self._scan_lock:
            try:
                found = self._scan()
            except Exception:
                if not self._failing:
                    logger.warning("New friends: the look failed; trying again in %.0f s", self._interval,
                                   exc_info=True)
                self._failing = True
                return []
            self._failing = False
            return found

    def _sessions(self) -> Optional[list]:
        recent = getattr(self._backend, "recent_sessions", None)
        sessions = recent(SESSIONS_MAX_AGE) if callable(recent) else None
        if sessions is None:
            sessions = self._backend.with_client(lambda client: client.get_sessions())
        return sessions

    def _scan(self) -> list[dict]:
        from .wcdb_backend import _session_username

        now = self._clock()
        found = [event for event in (self._tell(event) for event in list(self._retry.values())) if event]
        sessions = self._sessions()
        if sessions is None:
            return found                                  # the database is not open
        me = str(self._backend.own_wxid() or "")
        people = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            username = _session_username(session)
            if is_person_chat(username, me):
                people.append((username, session_time(session), session))
        started = self._store.started_at()
        if started is None:
            times = [at for _, at, _ in people if at is not None]
            started = max(times) if times else now
            self._store.start(started)
            for username, at, session in people:
                self._stamps[username] = _session_stamp(session, at)
            logger.info("New friends: first look (%d private chats); only friends added from now on are reported",
                        len(people))
            return found
        mark = self._store.mark()
        mark = started if mark is None else mark
        changed = []
        for username, at, session in people:
            stamp = _session_stamp(session, at)
            if at is not None:
                if at > mark and self._stamps.get(username) != stamp:
                    changed.append((username, at, session, stamp))
            elif username in self._stamps and self._stamps[username] != stamp:
                changed.append((username, at, session, stamp))
            else:
                self._stamps.setdefault(username, stamp)
        changed.sort(key=lambda item: (item[1] is None, item[1] or 0.0))
        done = changed[:MAX_CHATS_PER_SCAN]
        unread: list = []                                 # (chat, its session time) whose read failed
        for n, (username, at, session, stamp) in enumerate(done):
            if self._stop.is_set():
                done = done[:n]
                break
            if n and self._pause:
                time.sleep(self._pause)                   # the poll loop gets the lock in between
            try:
                messages = self._backend.with_client(
                    lambda client, chat=username: client.get_messages(chat, limit=READ_MESSAGES))
            except Exception as e:
                # This chat could not be read (an error code: WcdbCallError): the other chats are still
                # looked at, this one again at the next look (not stamped, the mark stays before it).
                unread.append((username, at))
                if username not in self._unreadable:
                    self._unreadable.add(username)
                    logger.warning("New friends: a private chat (%s…) could not be read (%s); looked at again "
                                   "at the next look", username[:8], e)
                continue
            if messages is None:
                done = done[:n]
                break                                     # the database closed meanwhile
            self._unreadable.discard(username)
            self._stamps[username] = stamp
            if username in self._reported or username in self._retry or self._store.known(username):
                continue
            added = find_added(messages, username, me)
            if added is None or added.at <= started:
                continue
            event = self._report(added, session, now)
            if event:
                found.append(event)
        timed = [at for _, at, _, _ in done if at is not None]
        if timed:
            newest = max(timed)
            if len(done) < len([c for c in changed if c[1] is not None]):
                newest -= 1e-3                            # the rest (same time too) at the next look
            held = [at for _, at in unread if at is not None]
            if held:
                newest = min(newest, min(held) - 1e-3)    # a chat not read yet stays after the mark
            if newest > mark:
                self._store.set_mark(newest)
        if now - self._last_forget >= 3600:
            self._last_forget = now
            self._store.forget_verify(now - NEW_DAYS * 86400)
        return found

    def _display_name(self, wxid: str) -> str:
        try:
            names = self._backend.with_client(lambda client: client.get_display_names([wxid])) or {}
        except Exception:
            logger.debug("New friends: no display name for %s…", wxid[:8], exc_info=True)
            return ""
        name = str(names.get(wxid, "") or "").strip() if isinstance(names, dict) else ""
        return "" if name == wxid else name

    def _report(self, added: Added, session: dict, now: float) -> Optional[dict]:
        display = self._display_name(added.wxid)
        name = added.name or display
        remark = str(_first(session, "remark", "remarkName", "remark_name", default="") or "").strip()
        verify_text = added.verify_text
        if added.kind == PASSED:
            how = REQUESTED
        elif added.kind == GREETING_ONLY:
            how = UNKNOWN
        else:
            how = self.accepts.judge(added.name or display, added.at, phone=added.kind == PHONE)
        event = {"wxid": added.wxid, "name": name, "remark": remark, "verify_text": verify_text, "how": how}
        self._reported.add(added.wxid)
        try:
            masked = ""
            if self._keep_verify and verify_text:
                from src.pii import mask_pii
                masked = mask_pii(verify_text)
            self._store.add(wxid=added.wxid, name=name, remark=remark, how=how, kind=added.kind, added_at=added.at,
                            verify_from=added.verify_from, verify_text=masked, found_at=now)
        except Exception:
            logger.warning("New friends: %s… could not be written down", added.wxid[:8], exc_info=True)
        note = getattr(self._backend, "note_private_name", None)
        if callable(note) and (display or name):
            try:
                note(added.wxid, display or name)
            except Exception:
                logger.debug("New friends: the backend could not take the name", exc_info=True)
        logger.info("New friends: %s… became a friend (%s, %s; %s)", added.wxid[:8], how, added.kind,
                    "with a greeting" if verify_text else "no greeting")
        return self._tell(event)

    def _tell(self, event: dict) -> Optional[dict]:
        """Hand the event to every ``on_found``; one that fails gets it again at the next look."""
        for sink in self._on_found:
            try:
                sink(dict(event))
            except Exception:
                logger.warning("New friends: %s… could not be passed on; trying again at the next look",
                               str(event.get("wxid"))[:8], exc_info=True)
                self._retry[event["wxid"]] = event
                return None
        self._retry.pop(event["wxid"], None)
        return event

    # ── the console (PRM_COPILOT) ─────────────────────────────────────────

    def recent(self, days: int = NEW_DAYS) -> list[dict]:
        """The friends found in the last ``days`` days, newest first (登记达人's candidates)."""
        return self._store.recent(self._clock() - days * 86400)

    def registered(self, wxid: str) -> bool:
        """A creator was registered (PrmService.on_registered): when she is a new friend, her messages
        since she was added (her greeting too; at most PRM_CATCHUP_HOURS old, 5 minutes with 0) go to
        PRM as one draft job, read back on her chat's next poll (the backend's ``catch_up_chat``).
        True when that was asked for."""
        sink = self._catch_up_sink
        ask = getattr(self._backend, "catch_up_chat", None)
        record = self._store.get(str(wxid))
        if record is None or sink is None or not callable(ask):
            return False
        now = self._clock()
        if now - float(record["found_at"]) > NEW_DAYS * 86400:
            return False
        added_at = float(record["added_at"])
        since = max(float(record["verify_from"] or added_at), now - self._catchup_sec)

        def hand_on(messages: list, wxid: str = str(wxid)) -> object:
            return sink(wxid, messages, verify_until=added_at)

        ask(str(wxid), str(record["name"] or ""), since, hand_on)
        logger.info("New friends: %s… registered as a creator; her messages since she was added go to PRM",
                    str(wxid)[:8])
        return True
