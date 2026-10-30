"""PRM copilot storage (``PRM_COPILOT=on``), one SQLite file (``PRM_DB``, data/prm.db).

``parties``         the creators the operators registered (their private chat is their wxid);
                    ``auto_registered``: the bot registered her as a new friend (PRM_AUTO_REGISTER)
``deals``           one collaboration per creator: stage, mode (``copilot`` / ``human`` until
                    ``mode_until`` / ``paused``), the latest slot values, the stage the AI
                    suggests, why a person should look (``notices``, JSON; ``attention`` is
                    their texts, what the console shows), when it was delivered and when its
                    personal data was cleared (8.2), and where its 专属佣金 chain stands
                    (``chain``, JSON: the signature the bot last looked at, the sample order
                    the chain follows and since when; src/prm/service.py)
``action_packets``  every draft reply: what would be sent (text with placeholders,
                    attachments), why, and what became of it
``pii_tokens``      the phone and ID numbers behind the placeholders (〈手机1〉): the Mac
                    and its model only ever see the placeholder; ``masked`` stays when the
                    number itself is cleared
``shipments``       the samples sent: carrier, waybill, logistics status
``send_gates``      per message kind: does a draft wait for a person (``review``) or go out
                    at once (``auto``), and from when its count starts (8.1)
``deal_events``     what happened to a deal, in order: stage and mode changes, shipments,
                    the operators' own WeChat replies, a creator's messages while a person
                    has the chat, holding replies, numbers shown in full, clean-ups.  The
                    audit trail, and the backlog the next draft job is told about.
``sample_orders``   the sample orders verified in 聚水潭 and where they stand there (status,
                    waybill, sent / signed for, why rejected or deleted), what the deal got of
                    them (``applied``) and whether they are still asked about (``done`` empty):
                    PRM_SAMPLE_SYNC_MIN, src/prm/service.py
``buyin_threads``   抖音百应 private-message conversations (精选联盟私信, PRM_BUYIN_SYNC_MIN): which registered
                    creator a conversation is (none: nobody on the list matched), her 百应 id and 抖音号, our
                    invite it is (``invited``, from the Mac's invite ledger match), the message ids handled, when
                    she and our side last wrote and up to when her lines are answered, how many contact cards came
                    in a conversation that is not ours (never their values); src/prm/service.py
``prm_meta``        small values the service keeps over a restart (when the sync last asked)
``friend_adds``     加百应达人微信 (PRM_FRIEND_ADD, src/prm/friend_adds.py): a creator's contact left in 百应, one record
                    each (one per 百应 message): her 抖音号 and nickname, the full 微信号 / 手机号 (the only place they
                    are kept, cleared later; ``masked`` stays), the greeting, the remark (备注) she gets in our WeChat and
                    whether it is on her (``remark_set``), the invite, where it stands (``state``), the new friend that
                    may be her (``candidate``) and, once linked, her chat and party
``friend_add_attempts``  every add_friend call made for a record (who, with which number kind, what came of it): the
                    audit, the day's count and the gap between two adds; no number in it
``creator_links``   who a registered creator is on 抖音 (her 抖音号, as an operator confirmed it): ``_creator_ids``
                    reads it

A packet's life: ``pending`` (waits in the console) -> ``approved`` (an operator said
send, the kind's gate is ``auto``, or the 下班时段 window sent it: route ``night``) -> ``sending`` ->
``sent`` | ``failed``; or
``pending`` -> ``dropped`` (not sent) | ``superseded`` (the creator wrote again, a newer
draft follows).  ``no_reply`` is a draft that says nothing needs to be sent.  Moves are
compare-and-set (``move``), so a double click or two threads never send one packet twice.

A 百应 private-message draft (``tool`` ``BUYIN_TOOL``, ``meta.channel`` "buyin", its chat ``buyin:<conversation>``)
never goes out by the bot: ``pending`` -> ``sent`` (outcome ``sent_by_hand``: an operator copied it and sent it in
百应 by hand) | ``dropped`` | ``superseded`` (a newer draft of the same conversation); it is no WeChat reply (its
own supersede lane, no gate, no 「标为发错了」, not told to later WeChat drafts).

A packet with a platform action (``tool`` is the action's name, ``platform`` the JSON:
the action, the Mac's preview, the execution; src/prm/platforms.py) is always reviewed:
``pending`` -> ``executing`` (an operator said 批准并执行; the Mac runs the platform CLI
with --execute, once) -> ``approved`` (read back VERIFIED_SUCCESS: the reply, with the
link filled in, goes out) | ``failed`` (anything less: nothing is sent to the creator).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

PENDING, APPROVED, SENDING, SENT, FAILED = "pending", "approved", "sending", "sent", "failed"
DROPPED, SUPERSEDED, NO_REPLY = "dropped", "superseded", "no_reply"
EXECUTING = "executing"      # a platform action an operator approved, running on the Mac
PACKET_STATES = (PENDING, APPROVED, SENDING, SENT, FAILED, DROPPED, SUPERSEDED, NO_REPLY, EXECUTING)
LANE_SYSTEM, LANE_REPLY = "system", "reply"   # supersede(lane=): the bot's next steps, the replies
LANE_BUYIN = "buyin"         # supersede(lane=): the 抖音百应 private-message drafts (never a WeChat reply's lane)
MESSAGE_TOOL = "message.send"   # ``tool`` of a plain reply (a platform action's name otherwise)
BUYIN_TOOL = "buyin.reply"      # ``tool`` of a 百应 private-message draft: an operator sends it in 百应 by hand

# The stages of a collaboration (docs: 合作单状态机); the AI may only suggest these.
STAGES = {
    "new": "待建联", "talk": "意向沟通", "agreed": "意向确认", "hold": "搁置",
    "address": "收集收件信息", "to_ship": "待寄出", "shipped": "已寄出", "transit": "运输中",
    "delivered": "已签收", "ship_error": "物流异常", "brief": "素材已发", "draft": "初稿已交",
    "revise": "修改中", "published": "已发布", "closed": "结案",
}
COPILOT, HUMAN, PAUSED = "copilot", "human", "paused"
MODES = {COPILOT: "副驾驶", HUMAN: "人工接管", PAUSED: "暂停"}
# Logistics status of a shipment -> the stage it puts the deal in.
SHIPMENT_STATES = {"shipped": "已寄出", "transit": "运输中", "delivered": "已签收", "ship_error": "物流异常"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wxid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    authorized_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'talk',
    mode TEXT NOT NULL DEFAULT 'copilot',
    slots TEXT NOT NULL DEFAULT '{}',
    stage_proposal TEXT NOT NULL DEFAULT '',
    attention TEXT NOT NULL DEFAULT '',
    last_message_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deals_party ON deals(party_id, id);
CREATE TABLE IF NOT EXISTS action_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    party_id INTEGER NOT NULL,
    job_id INTEGER,
    chat_id TEXT NOT NULL,
    tool TEXT NOT NULL DEFAULT 'message.send',
    kind TEXT NOT NULL DEFAULT 'other',
    route TEXT NOT NULL DEFAULT 'review',
    text TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    intent TEXT NOT NULL DEFAULT '',
    slots TEXT NOT NULL DEFAULT '{}',
    stage_proposal TEXT NOT NULL DEFAULT '',
    needs_attention INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    final_text TEXT,
    edited INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS packets_status ON action_packets(status, id);
CREATE INDEX IF NOT EXISTS packets_deal ON action_packets(deal_id, id);
CREATE TABLE IF NOT EXISTS pii_tokens (
    party_id INTEGER NOT NULL,
    token TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (party_id, token)
);
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    carrier TEXT NOT NULL,
    waybill TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'shipped',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS shipments_deal ON shipments(deal_id, id);
CREATE TABLE IF NOT EXISTS send_gates (
    kind TEXT PRIMARY KEY,
    route TEXT NOT NULL DEFAULT 'review',
    window INTEGER NOT NULL,
    min_unchanged INTEGER NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    reset_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    party_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    forwarded INTEGER NOT NULL DEFAULT 1,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deal_events_deal ON deal_events(deal_id, id);
CREATE TABLE IF NOT EXISTS sample_orders (
    order_no TEXT PRIMARY KEY,
    packet_id INTEGER NOT NULL,
    deal_id INTEGER NOT NULL,
    party_id INTEGER NOT NULL,
    made_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    carrier TEXT NOT NULL DEFAULT '',
    waybill TEXT NOT NULL DEFAULT '',
    sent_at REAL,
    signed_at REAL,
    reason TEXT NOT NULL DEFAULT '',
    misses INTEGER NOT NULL DEFAULT 0,
    checked_at REAL,
    done TEXT NOT NULL DEFAULT '',
    applied TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sample_orders_deal ON sample_orders(deal_id);
CREATE TABLE IF NOT EXISTS buyin_threads (
    conversation_id TEXT PRIMARY KEY,
    party_id INTEGER,
    deal_id INTEGER,
    nickname TEXT NOT NULL DEFAULT '',
    creator_uid TEXT NOT NULL DEFAULT '',
    aweme_id TEXT NOT NULL DEFAULT '',
    seen TEXT NOT NULL DEFAULT '[]',
    last_creator_at REAL,
    last_shop_at REAL,
    answered_upto REAL,
    unanswered INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS buyin_threads_party ON buyin_threads(party_id);
CREATE TABLE IF NOT EXISTS prm_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS friend_adds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    aweme_id TEXT NOT NULL DEFAULT '',
    creator_uid TEXT NOT NULL DEFAULT '',
    nickname TEXT NOT NULL DEFAULT '',
    wechat TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    masked TEXT NOT NULL DEFAULT '{}',
    unavailable INTEGER NOT NULL DEFAULT 0,
    greeting TEXT NOT NULL DEFAULT '',
    invited TEXT NOT NULL DEFAULT '{}',
    party_id INTEGER,
    deal_id INTEGER,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    candidate TEXT NOT NULL DEFAULT '{}',
    rejected TEXT NOT NULL DEFAULT '[]',
    wxid TEXT NOT NULL DEFAULT '',
    link_on_accept INTEGER NOT NULL DEFAULT 1,
    reveals TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    requested_at REAL,
    linked_at REAL,
    cleared_at REAL,
    closed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS friend_adds_msg ON friend_adds(conversation_id, message_id) WHERE message_id != '';
CREATE INDEX IF NOT EXISTS friend_adds_state ON friend_adds(state, aweme_id);
CREATE TABLE IF NOT EXISTS friend_add_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    add_id INTEGER NOT NULL,
    at REAL NOT NULL,
    by TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS friend_add_attempts_at ON friend_add_attempts(at);
CREATE TABLE IF NOT EXISTS creator_links (
    party_id INTEGER NOT NULL,
    platform TEXT NOT NULL DEFAULT 'dy',
    account TEXT NOT NULL,
    uid TEXT NOT NULL DEFAULT '',
    uid_source TEXT NOT NULL DEFAULT '',
    nickname TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    add_id INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY (party_id, platform, account)
);
"""

# Columns added after the first version: (table, column, type).
_ADDED_COLUMNS = (
    ("deals", "mode_until", "REAL"),
    ("deals", "delivered_at", "REAL"),
    ("deals", "purged_at", "REAL"),
    ("deals", "holding_round", "REAL"),
    ("action_packets", "meta", "TEXT NOT NULL DEFAULT '{}'"),
    ("action_packets", "wrong", "INTEGER NOT NULL DEFAULT 0"),
    ("pii_tokens", "masked", "TEXT NOT NULL DEFAULT ''"),
    ("pii_tokens", "purged_at", "REAL"),
    ("action_packets", "platform", "TEXT NOT NULL DEFAULT '{}'"),
    ("deals", "notices", "TEXT NOT NULL DEFAULT '[]'"),
    ("deals", "chain", "TEXT NOT NULL DEFAULT '{}'"),
    ("parties", "auto_registered", "INTEGER NOT NULL DEFAULT 0"),
    ("buyin_threads", "buyin_id", "TEXT NOT NULL DEFAULT ''"),
    ("buyin_threads", "invited", "TEXT NOT NULL DEFAULT '{}'"),
    ("buyin_threads", "contacts", "INTEGER NOT NULL DEFAULT 0"),
    ("buyin_threads", "other_invite", "INTEGER NOT NULL DEFAULT 0"),   # somebody else's invite card (2026-10-30)
    ("friend_adds", "remark", "TEXT NOT NULL DEFAULT ''"),            # 加百应达人微信的备注 (2026-10-29)
    ("friend_adds", "remark_set", "INTEGER NOT NULL DEFAULT 0"),
)

# What the creators wrote, or our side's own words, waiting for the next draft job.
BACKLOG_KINDS = ("creator_message", "operator_message")

# A deal's notices: ``[{"key", "text", "at", ...}]``, what the console says about it (``attention``).
# ``draft:<lane>`` is the latest draft's own notes (the next draft of that lane replaces them); every
# other key is the system's and stays until what it says is dealt with (src/prm/service.py: a draft
# that was not written, a 专属佣金 step owed, a platform action that ended red, a recall, messages
# read too late to draft, this PC's clock off WeChat's, a holding line the night window sent: ``night:<packet>``).
# ``show`` False: kept for what it holds, no longer shown.
DRAFT_NOTES = "draft:"
NOTICE_CHARS = 300          # one notice's text
ATTENTION_CHARS = 600       # all of them, as the console shows them


class PartyError(ValueError):
    """A creator cannot be registered or changed (the message says why, in Chinese)."""


def _loads(text: str, empty):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return empty
    return value if isinstance(value, type(empty)) else empty


def packet_view(row: dict) -> dict:
    """A packet with its JSON columns read."""
    out = dict(row)
    out["attachments"] = _loads(row.get("attachments"), [])
    out["slots"] = _loads(row.get("slots"), {})
    out["meta"] = _loads(row.get("meta"), {})
    out["platform"] = _loads(row.get("platform"), {})
    out["needs_attention"] = bool(row.get("needs_attention"))
    out["edited"] = bool(row.get("edited"))
    out["wrong"] = bool(row.get("wrong"))
    return out


def deal_view(row: dict) -> dict:
    out = dict(row)
    out["slots"] = _loads(row.get("slots"), {})
    out["notices"] = notices_of(row)
    out["chain"] = _loads(row.get("chain"), {})
    return out


def notices_of(row: dict) -> list:
    """A deal row's notices.  A deal from before them (2026-10-28) has only its ``attention``: that
    is taken as its latest draft's notes (the next draft replaced it then too)."""
    notices = [n for n in _loads(row.get("notices"), []) if isinstance(n, dict) and n.get("key")]
    if not notices and row.get("attention"):
        notices = [{"key": DRAFT_NOTES + "reply", "text": str(row["attention"])}]
    return notices


def attention_of(notices: list) -> str:
    """What the console shows of a deal's notices (``deals.attention``): the system's first (what is
    still to be dealt with, oldest first), then the latest drafts' notes."""
    shown = [n for n in notices if n.get("text") and n.get("show", True)]
    system = [n for n in shown if not str(n["key"]).startswith(DRAFT_NOTES)]
    drafts = [n for n in shown if str(n["key"]).startswith(DRAFT_NOTES)]
    return "；".join(str(n["text"]) for n in system + drafts)[:ATTENTION_CHARS]


def event_view(row: dict) -> dict:
    out = dict(row)
    out["detail"] = _loads(row.get("detail"), {})
    return out


def sample_order_view(row: dict) -> dict:
    out = dict(row)
    out["applied"] = _loads(row.get("applied"), {})
    return out


def buyin_thread_view(row: dict) -> dict:
    out = dict(row)
    out["seen"] = [str(i) for i in _loads(row.get("seen"), [])]
    out["invited"] = _loads(row.get("invited"), {}) or None
    out["other_invite"] = bool(row.get("other_invite"))
    return out


# 加百应达人微信 (src/prm/friend_adds.py): the JSON columns of a record, and what may be set besides its state.
FRIEND_ADD_JSON = {"masked": {}, "invited": {}, "candidate": {}, "rejected": [], "reveals": []}
FRIEND_ADD_FIELDS = {"source", "conversation_id", "message_id", "aweme_id", "creator_uid", "nickname", "wechat", "phone",
                     "masked", "unavailable", "greeting", "invited", "party_id", "deal_id", "reason", "candidate",
                     "rejected", "wxid", "link_on_accept", "reveals", "requested_at", "linked_at", "cleared_at",
                     "closed_at", "remark", "remark_set"}
FRIEND_ADD_FLAGS = ("unavailable", "link_on_accept", "remark_set")


def friend_add_view(row: dict) -> dict:
    out = dict(row)
    for key, empty in FRIEND_ADD_JSON.items():
        out[key] = _loads(row.get(key), empty)
    for key in FRIEND_ADD_FLAGS:
        out[key] = bool(row.get(key))
    out["remark"] = str(row.get("remark") or "")
    return out


def _friend_add_values(fields: dict) -> dict:
    """``fields`` as columns: the JSON ones written as JSON, the flags as 0 / 1; an unknown one is a ValueError."""
    unknown = set(fields) - FRIEND_ADD_FIELDS
    if unknown:
        raise ValueError(f"not a friend_adds field: {sorted(unknown)}")
    out = {}
    for key, value in fields.items():
        if key in FRIEND_ADD_JSON:
            value = json.dumps(value if value is not None else FRIEND_ADD_JSON[key], ensure_ascii=False)
        elif key in FRIEND_ADD_FLAGS:
            value = int(bool(value))
        out[key] = value
    return out


class PrmStore:
    def __init__(self, path: str | Path = "data/prm.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for table, column, kind in _ADDED_COLUMNS:   # a database from before these columns
                columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            self._conn.commit()

    def seed_gates(self, defaults: dict) -> None:
        """``{kind: (window, min_unchanged, locked)}``: the gates a database does not have yet."""
        with self._lock:
            for kind, (window, minimum, locked) in defaults.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO send_gates (kind, window, min_unchanged, locked) VALUES (?, ?, ?, ?)",
                    (kind, int(window), int(minimum), int(bool(locked))),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── creators ──────────────────────────────────────────────────────────

    def add_party(self, wxid: str, name: str, note: str = "", now: Optional[float] = None, *,
                  auto: bool = False) -> dict:
        """Register a creator (a removed one comes back) and open their deal.  ``auto`` (PRM_AUTO_REGISTER, the bot
        registering a new friend): only a wxid that has no party at all, active or stopped (an operator's 停用 sticks;
        checked in the same step as the insert), marked ``auto_registered``.  A person registering a stopped party
        again makes it a person's (``auto_registered`` 0)."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT * FROM parties WHERE wxid = ?", (wxid,)).fetchone()
            if row is not None and row["active"]:
                raise PartyError(f"{wxid} 已经登记过了（{row['name']}）")
            if row is not None and auto:
                raise PartyError(f"{wxid} 停用过，不自动登记")
            if row is not None:
                self._conn.execute(
                    "UPDATE parties SET name = ?, note = ?, active = 1, auto_registered = 0, authorized_at = ?,"
                    " updated_at = ? WHERE id = ?", (name, note, now, now, row["id"]),
                )
                party_id = int(row["id"])
            else:
                cur = self._conn.execute(
                    "INSERT INTO parties (wxid, name, note, authorized_at, created_at, updated_at, auto_registered)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)", (wxid, name, note, now, now, now, int(bool(auto))),
                )
                party_id = int(cur.lastrowid)
            if self._conn.execute("SELECT 1 FROM deals WHERE party_id = ?", (party_id,)).fetchone() is None:
                self._conn.execute(
                    "INSERT INTO deals (party_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (party_id, now, now),
                )
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone())

    def update_party(self, party_id: int, *, name: Optional[str] = None, note: Optional[str] = None,
                     active: Optional[bool] = None, now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE parties SET name = ?, note = ?, active = ?, updated_at = ? WHERE id = ?",
                (row["name"] if name is None else name, row["note"] if note is None else note,
                 row["active"] if active is None else int(bool(active)), now, party_id),
            )
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone())

    def party(self, party_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
        return dict(row) if row else None

    def party_by_wxid(self, wxid: str, include_inactive: bool = False) -> Optional[dict]:
        """The registered (active) creator with this wxid; ``include_inactive``: a stopped one too."""
        sql = "SELECT * FROM parties WHERE wxid = ?" + ("" if include_inactive else " AND active = 1")
        with self._lock:
            row = self._conn.execute(sql, (wxid,)).fetchone()
        return dict(row) if row else None

    def parties(self, include_inactive: bool = False) -> list[dict]:
        sql = "SELECT * FROM parties" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY id"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def active_wxids(self) -> set[str]:
        with self._lock:
            return {r["wxid"] for r in self._conn.execute("SELECT wxid FROM parties WHERE active = 1")}

    # ── deals ─────────────────────────────────────────────────────────────

    def deal_of(self, party_id: int, now: Optional[float] = None) -> dict:
        """The creator's (newest) deal, made when there is none."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deals WHERE party_id = ? ORDER BY id DESC LIMIT 1", (party_id,)
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO deals (party_id, created_at, updated_at) VALUES (?, ?, ?)", (party_id, now, now),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM deals WHERE id = ?", (cur.lastrowid,)).fetchone()
        return deal_view(dict(row))

    def deal(self, deal_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        return deal_view(dict(row)) if row else None

    def deals(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM deals ORDER BY id").fetchall()
        return [deal_view(dict(r)) for r in rows]

    def update_deal(self, deal_id: int, now: Optional[float] = None, *, merge_slots: Optional[dict] = None,
                    replace_slots: Optional[dict] = None, **fields) -> None:
        """Set ``fields`` and merge (or replace) the slot values; ``chain`` is a dict (stored as JSON).
        (``attention`` is the notices' texts: ``set_notice`` / ``drop_notices`` / ``edit_notices``.)"""
        allowed = {"stage", "mode", "mode_until", "stage_proposal", "last_message_at",
                   "delivered_at", "purged_at", "holding_round", "chain"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a deal field: {sorted(unknown)}")
        if "chain" in fields:
            fields["chain"] = json.dumps(fields["chain"] or {}, ensure_ascii=False)
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT slots FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None:
                return
            if replace_slots is not None:
                fields["slots"] = json.dumps(replace_slots, ensure_ascii=False)
            elif merge_slots:
                slots = _loads(row["slots"], {})
                slots.update(merge_slots)
                fields["slots"] = json.dumps(slots, ensure_ascii=False)
            fields["updated_at"] = now
            names = sorted(fields)
            self._conn.execute(
                f"UPDATE deals SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                (*[fields[n] for n in names], deal_id),
            )
            self._conn.commit()

    def edit_notices(self, deal_id: int, edit: Callable[[list], list], now: Optional[float] = None, *,
                     touch: bool = True) -> list:
        """Change a deal's notices in one step under the lock: ``edit`` gets the list and returns the
        new one; ``attention`` follows (``attention_of``).  ``touch`` False: the deal's ``updated_at``
        stays (a notice about this PC, not the deal: no sign of life for the 8.2 clean-up).  Returns the
        new list ([] for no deal)."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT notices, attention FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None:
                return []
            before = notices_of(dict(row))
            notices = edit([dict(n) for n in before])
            if notices != before or attention_of(notices) != row["attention"]:
                self._conn.execute(f"UPDATE deals SET notices = ?, attention = ?{', updated_at = ?' if touch else ''}"
                                   " WHERE id = ?", (json.dumps(notices, ensure_ascii=False), attention_of(notices),
                                                     *((now,) if touch else ()), deal_id))
                self._conn.commit()
        return notices

    def set_notice(self, deal_id: int, key: str, text: str, now: Optional[float] = None, *, touch: bool = True,
                   **data) -> None:
        """The deal's notice ``key`` says ``text`` (``data``: what the system keeps to deal with it later),
        in its place when it had one; an empty ``text`` drops it (``touch``: ``edit_notices``)."""
        now = time.time() if now is None else now

        def edit(notices):
            old = next((n for n in notices if n["key"] == key), None)
            if not text:
                return [n for n in notices if n is not old]
            item = {**(old or {}), **data, "key": key, "text": str(text)[:NOTICE_CHARS],
                    "at": (old or {}).get("at", now)}
            return [item if n is old else n for n in notices] if old is not None else [*notices, item]

        self.edit_notices(deal_id, edit, now, touch=touch)

    def drop_notices(self, deal_id: int, match: Callable[[dict], bool], now: Optional[float] = None) -> list:
        """Remove the deal's notices ``match`` says yes to; returns them (one step: of two threads only
        one gets a notice)."""
        dropped: list = []

        def edit(notices):
            keep = []
            for n in notices:
                (dropped if match(n) else keep).append(n)
            return keep

        self.edit_notices(deal_id, edit, now)
        return dropped

    def deals_with_notice(self, key: str) -> list[dict]:
        """The deals that have the notice ``key`` (the 专属佣金 steps owed: ``step``)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM deals WHERE notices LIKE ? ORDER BY id",
                                      (f'%"key": "{key}"%',)).fetchall()
        return [d for d in (deal_view(dict(r)) for r in rows) if any(n["key"] == key for n in d["notices"])]

    def deals_with_notice_prefix(self, prefix: str) -> list[dict]:
        """The deals that have a notice whose key starts with ``prefix`` (the 百应 lines held back: ``buyin_held:``)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM deals WHERE notices LIKE ? ORDER BY id",
                                      (f'%"key": "{prefix}%',)).fetchall()
        return [d for d in (deal_view(dict(r)) for r in rows) if any(str(n["key"]).startswith(prefix) for n in d["notices"])]

    def rewrite_notices(self, deal_id: int, rewrite: Callable[[str], str]) -> int:
        """Rewrite the words of a deal's notices (8.2 clean-up): their texts and the creator's lines and
        history a notice keeps; returns how many changed."""
        changed = [0]

        def edit(notices):
            out = []
            for n in notices:
                new = dict(n)
                for k in ("text", "head", "lines"):
                    if isinstance(new.get(k), str):
                        new[k] = rewrite(new[k])
                if isinstance(new.get("history"), list):
                    new["history"] = [{**h, "text": rewrite(str(h.get("text") or ""))} if isinstance(h, dict) else h
                                      for h in new["history"]]
                changed[0] += int(new != n)
                out.append(new)
            return out

        self.edit_notices(deal_id, edit)
        return changed[0]

    def delivered_deals(self) -> list[dict]:
        """The deals marked 已签收 whose details were not cleared yet (``delivered_at`` set, no ``purged_at``)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM deals WHERE delivered_at IS NOT NULL AND purged_at IS NULL"
                                      " ORDER BY id").fetchall()
        return [deal_view(dict(r)) for r in rows]

    def deals_to_purge(self, now: float, days: int) -> list[dict]:
        """Deals whose details are due to be cleared (8.2, ``retention.purge_due_at``)."""
        from .retention import purge_due_at

        with self._lock:
            rows = self._conn.execute("SELECT * FROM deals ORDER BY id").fetchall()
        deals = [deal_view(dict(r)) for r in rows]
        return [d for d in deals if (purge_due_at(d, days) or now + 1) <= now]

    # ── drafts ────────────────────────────────────────────────────────────

    def add_packet(self, *, deal_id: int, party_id: int, job_id: Optional[int], chat_id: str, kind: str,
                   text: str, attachments: list, intent: str, slots: dict, stage_proposal: str,
                   needs_attention: bool, reason: str, status: str = PENDING, route: str = "review",
                   meta: Optional[dict] = None, decided_at: Optional[float] = None,
                   tool: str = MESSAGE_TOOL, platform: Optional[dict] = None,
                   now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO action_packets (deal_id, party_id, job_id, chat_id, tool, kind, route, text, attachments,"
                " intent, slots, stage_proposal, needs_attention, reason, status, meta, platform, decided_at,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (deal_id, party_id, job_id, chat_id, tool, kind, route, text,
                 json.dumps(attachments, ensure_ascii=False), intent, json.dumps(slots, ensure_ascii=False),
                 stage_proposal, int(bool(needs_attention)), reason, status,
                 json.dumps(meta or {}, ensure_ascii=False), json.dumps(platform or {}, ensure_ascii=False),
                 decided_at, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def packet(self, packet_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM action_packets WHERE id = ?", (packet_id,)).fetchone()
        return packet_view(dict(row)) if row else None

    def packets(self, statuses: Optional[tuple] = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM action_packets", []
        if statuses:
            sql += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            args += list(statuses)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, int(limit))).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def outcomes(self, deal_id: int, limit: int = 6) -> list[dict]:
        """A deal's latest drafts a person (or a gate) decided on, newest first: sent (as drafted
        or edited), not sent (dropped), failed.  What the creator really got from us in WeChat: a 百应
        private-message draft (``BUYIN_TOOL``, sent there by hand) is none of it."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE deal_id = ? AND status IN (?, ?, ?) AND decided_at IS NOT NULL"
                " AND tool != ? ORDER BY COALESCE(sent_at, decided_at) DESC, id DESC LIMIT ?",
                (deal_id, SENT, DROPPED, FAILED, BUYIN_TOOL, int(limit)),
            ).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def platform_failures(self, limit: int = 50) -> list[dict]:
        """Packets with a platform action that ended ``failed`` (the platform write was not
        verified, or the reply did not go out), newest first: the console marks them red
        until an operator says they are dealt with (``platform.acked_at``).  A 百应 draft is no platform action."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE tool NOT IN (?, ?) AND status = ? ORDER BY id DESC LIMIT ?",
                (MESSAGE_TOOL, BUYIN_TOOL, FAILED, int(limit)),
            ).fetchall()
        return [p for p in (packet_view(dict(r)) for r in rows) if not p["platform"].get("acked_at")]

    def deal_packets(self, deal_id: int, tools: tuple = (), statuses: tuple = (),
                     since: Optional[float] = None) -> list[dict]:
        """A deal's packets, newest first: of these ``tools`` (platform action names) and in these
        ``statuses`` (all when empty), drafted at ``since`` or later (all when None).  Every one of
        them, however old (a 专属佣金 opened months ago)."""
        sql, args = "SELECT * FROM action_packets WHERE deal_id = ?", [deal_id]
        if since is not None:
            sql += " AND created_at >= ?"
            args.append(float(since))
        if tools:
            sql += f" AND tool IN ({', '.join('?' * len(tools))})"
            args += list(tools)
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            args += list(statuses)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def packets_of(self, tools: tuple, statuses: tuple = ()) -> list[dict]:
        """Every deal's packets of these ``tools`` (platform action names), in these ``statuses`` (all when empty),
        newest first (the sample orders the 聚水潭 sync keeps: ``jst.sample_order``)."""
        sql, args = f"SELECT * FROM action_packets WHERE tool IN ({', '.join('?' * len(tools))})", list(tools)
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            args += list(statuses)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def packet_for_job(self, job_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM action_packets WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
        return packet_view(dict(row)) if row else None

    def sent_since(self, chat_id: str, since: float) -> list[dict]:
        """Packets sent (or being sent) to this chat after ``since`` (the bot's own lines)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE chat_id = ? AND status IN (?, ?) AND sent_at >= ?"
                " ORDER BY id DESC", (chat_id, SENT, SENDING, since),
            ).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def unsure_since(self, chat_id: str, since: float) -> list[dict]:
        """Packets the bot was stopped in the middle of sending to this chat (``failed`` /
        ``indeterminate``: ``PrmService.start``), settled at ``since`` or later, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE chat_id = ? AND status = ? AND outcome = 'indeterminate'"
                " AND sent_at >= ? ORDER BY id DESC", (chat_id, FAILED, since),
            ).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def settle_unsure(self, packet_id: int, *, sent_at: Optional[float] = None,
                      error: Optional[str] = None) -> bool:
        """A packet the bot was stopped in the middle of sending (``failed`` / ``indeterminate``) that
        WeChat shows after all: with ``error`` None all of it went out (``sent`` / ``sent_confirmed``;
        ``sent_at`` becomes the time WeChat shows when that is earlier than the restart's); with an
        ``error`` it stays failed and says what was seen (a draft with files whose text went out).
        True when it was such a packet."""
        with self._lock:
            if error is None:
                when = "" if sent_at is None else ", sent_at = MIN(COALESCE(sent_at, ?), ?)"
                cur = self._conn.execute(
                    f"UPDATE action_packets SET status = ?, outcome = 'sent_confirmed', error = ''{when}"
                    " WHERE id = ? AND status = ? AND outcome = 'indeterminate'",
                    (SENT, *(() if sent_at is None else (sent_at, sent_at)), packet_id, FAILED),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE action_packets SET error = ? WHERE id = ? AND status = ? AND outcome = 'indeterminate'",
                    (error, packet_id, FAILED),
                )
            self._conn.commit()
            return cur.rowcount == 1

    def reviewed(self, kind: str, since: float, limit: int) -> list[dict]:
        """The newest drafts of ``kind`` a person decided on after ``since`` (8.1's window).
        Plain replies only: a platform action is always reviewed and earns no kind ``auto``; what a gate
        (``auto``) or the night window (``night``, PRM_NIGHT_AUTO_HOURS) sent was no person's decision."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE kind = ? AND route = 'review' AND tool = ? AND status IN (?, ?, ?)"
                " AND decided_at IS NOT NULL AND decided_at > ? ORDER BY decided_at DESC, id DESC LIMIT ?",
                (kind, MESSAGE_TOOL, SENT, FAILED, DROPPED, since, int(limit)),
            ).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def move(self, packet_id: int, from_states: tuple, to_state: str, **fields) -> bool:
        """Compare-and-set: the packet goes to ``to_state`` only if it is in one of ``from_states``.
        ``platform`` (a dict) replaces the packet's platform JSON in the same step."""
        allowed = {"final_text", "edited", "outcome", "error", "decided_at", "sent_at", "platform"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a packet field: {sorted(unknown)}")
        if "platform" in fields:
            fields["platform"] = json.dumps(fields["platform"] or {}, ensure_ascii=False)
        names = sorted(fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE action_packets SET status = ?{''.join(f', {n} = ?' for n in names)}"
                f" WHERE id = ? AND status IN ({', '.join('?' * len(from_states))})",
                (to_state, *[fields[n] for n in names], packet_id, *from_states),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def set_platform(self, packet_id: int, platform: dict, statuses: tuple, *,
                     expect: Optional[Callable[[dict], bool]] = None) -> bool:
        """Replace the platform JSON of a packet that is in one of ``statuses`` (and, with
        ``expect``, whose platform JSON now passes it): one step under the lock."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT platform FROM action_packets WHERE id = ? AND status IN ({', '.join('?' * len(statuses))})",
                (packet_id, *statuses),
            ).fetchone()
            if row is None or (expect is not None and not expect(_loads(row["platform"], {}))):
                return False
            self._conn.execute("UPDATE action_packets SET platform = ? WHERE id = ?",
                               (json.dumps(platform or {}, ensure_ascii=False), packet_id))
            self._conn.commit()
            return True

    def edit_platform(self, packet_id: int, edit: Callable[[dict], Optional[dict]], statuses: tuple, *,
                      error: Optional[Callable[[str], str]] = None) -> Optional[dict]:
        """Change the platform JSON of a packet that is in one of ``statuses`` in one step under the lock:
        ``edit(current)`` returns the new JSON, or None for no change; ``error(current error)`` gives its new
        error text at the same time.  Returns the new JSON (None: not changed)."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT platform, error FROM action_packets WHERE id = ? AND status IN ({', '.join('?' * len(statuses))})",
                (packet_id, *statuses),
            ).fetchone()
            if row is None:
                return None
            new = edit(_loads(row["platform"], {}))
            if new is None:
                return None
            if error is None:
                self._conn.execute("UPDATE action_packets SET platform = ? WHERE id = ?",
                                   (json.dumps(new, ensure_ascii=False), packet_id))
            else:
                self._conn.execute("UPDATE action_packets SET platform = ?, error = ? WHERE id = ?",
                                   (json.dumps(new, ensure_ascii=False), error(row["error"] or ""), packet_id))
            self._conn.commit()
            return new

    def set_outcome(self, packet_id: int, outcome: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE action_packets SET outcome = ? WHERE id = ?", (outcome, packet_id))
            self._conn.commit()

    def confirm_sent(self, packet_id: int) -> bool:
        """Its line was read back from WeChat: ``sent_confirmed`` while it is going out or sent
        unconfirmed, never over a failure (True: newly confirmed)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE action_packets SET outcome = 'sent_confirmed' WHERE id = ?"
                " AND COALESCE(outcome, '') != 'sent_confirmed'"
                " AND (status = ? OR (status = ? AND COALESCE(outcome, 'sent_unconfirmed') = 'sent_unconfirmed'))",
                (packet_id, SENDING, SENT),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_sent(self, packet_id: int, now: float) -> bool:
        """Sending → sent: ``sent_unconfirmed``, unless its line was read back already."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE action_packets SET status = ?, sent_at = ?, outcome = CASE WHEN outcome = 'sent_confirmed'"
                " THEN outcome ELSE 'sent_unconfirmed' END WHERE id = ? AND status = ?",
                (SENT, now, packet_id, SENDING),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_wrong(self, packet_id: int) -> bool:
        """An operator says this sent packet should not have gone out (True: newly marked)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE action_packets SET wrong = 1 WHERE id = ? AND status = ? AND wrong = 0", (packet_id, SENT),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def supersede(self, deal_id: int, now: Optional[float] = None, keep: Optional[int] = None,
                  system_kind: Optional[str] = None, lane: Optional[str] = None,
                  chat_id: Optional[str] = None) -> int:
        """Pending drafts of this deal (but ``keep``) are out of date: ``superseded``; returns how many.
        The Mac's drafts (they answer the creator), or with ``system_kind`` the system's own drafts of
        that kind (a shipping notice is not out of date because the creator wrote).  ``lane``: only the
        Mac's drafts of that lane — ``LANE_SYSTEM`` (a next step the bot asked for, ``meta.lane``),
        ``LANE_BUYIN`` (the 百应 private-message drafts, ``BUYIN_TOOL``) or ``LANE_REPLY`` (the WeChat
        replies: neither of those): a reply, a 专属佣金 step and a 百应 draft never put each other out of
        date.  ``chat_id``: only the drafts of that chat (a 百应 conversation's own)."""
        now = time.time() if now is None else now
        which = "job_id IS NOT NULL" if system_kind is None else "job_id IS NULL AND kind = ?"
        args: list = []
        if system_kind is None and lane is not None:
            if lane == LANE_SYSTEM:
                which += " AND meta LIKE '%\"lane\": \"system\"%'"
            elif lane == LANE_BUYIN:
                which += " AND tool = ?"
                args.append(BUYIN_TOOL)
            else:
                which += " AND meta NOT LIKE '%\"lane\": \"system\"%' AND tool != ?"
                args.append(BUYIN_TOOL)
        if chat_id is not None:
            which += " AND chat_id = ?"
            args.append(chat_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE action_packets SET status = ?, decided_at = ? WHERE deal_id = ? AND status = ?"
                f" AND id != ? AND {which}",
                (SUPERSEDED, now, deal_id, PENDING, -1 if keep is None else keep,
                 *(() if system_kind is None else (system_kind,)), *args),
            )
            self._conn.commit()
            return cur.rowcount

    def hold_for_review(self, deal_id: int, reason: str, now: Optional[float] = None) -> int:
        """The Mac's drafts of this deal that have not gone out wait for a person, ``reason`` first in
        theirs (``needs_attention``): a waiting one stays waiting, a plain reply its gate (``auto``) or the
        night window (``night``, PRM_NIGHT_AUTO_HOURS) approved and not being sent yet goes back to waiting
        (``review``).  Not the bot's own shipping notices (a template), nor a 百应 private-message draft (her
        WeChat recall says nothing of 百应).  Returns how many."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status, reason FROM action_packets WHERE deal_id = ? AND job_id IS NOT NULL"
                " AND tool != ? AND (status = ? OR (status = ? AND route IN ('auto', 'night') AND tool = ?))",
                (deal_id, BUYIN_TOOL, PENDING, APPROVED, MESSAGE_TOOL),
            ).fetchall()
            held = 0
            for row in rows:
                cur = self._conn.execute(
                    "UPDATE action_packets SET status = ?, route = 'review', needs_attention = 1, reason = ?,"
                    " decided_at = CASE WHEN status = ? THEN NULL ELSE decided_at END WHERE id = ? AND status = ?",
                    (PENDING, "；".join(r for r in (reason, row["reason"]) if r)[:500], APPROVED, row["id"],
                     row["status"]),
                )
                held += cur.rowcount
            self._conn.commit()
        return held

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM action_packets GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def rewrite_packets(self, deal_id: int, rewrite: Callable[[str], str]) -> int:
        """Rewrite the text of every packet of a deal, its platform action too (8.2 clean-up), and a 百应
        draft's ``meta`` (it keeps her 百应 lines for the console); returns how many changed."""
        changed = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tool, text, final_text, slots, intent, reason, platform, meta FROM action_packets"
                " WHERE deal_id = ?", (deal_id,),
            ).fetchall()
            for row in rows:
                old = (row["text"] or "", row["final_text"], row["slots"] or "{}", row["intent"] or "",
                       row["reason"] or "", row["platform"] or "{}")
                new = tuple(None if value is None else rewrite(value) for value in old)
                meta = row["meta"] or "{}"
                new_meta = rewrite(meta) if row["tool"] == BUYIN_TOOL else meta
                if new != old or new_meta != meta:
                    self._conn.execute(
                        "UPDATE action_packets SET text = ?, final_text = ?, slots = ?, intent = ?, reason = ?,"
                        " platform = ?, meta = ? WHERE id = ?", (*new, new_meta, row["id"]),
                    )
                    changed += 1
            self._conn.commit()
        return changed

    # ── placeholders for phone and ID numbers ─────────────────────────────

    def token_for(self, party_id: int, kind: str, label: str, value: str, now: Optional[float] = None) -> str:
        """The placeholder of ``value`` for this creator (the same value keeps its placeholder)."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT token FROM pii_tokens WHERE party_id = ? AND kind = ? AND value = ? AND purged_at IS NULL",
                (party_id, kind, value),
            ).fetchone()
            if row is not None:
                return str(row["token"])
            count = self._conn.execute(
                "SELECT COUNT(*) FROM pii_tokens WHERE party_id = ? AND kind = ?", (party_id, kind),
            ).fetchone()[0]
            token = f"〈{label}{count + 1}〉"
            self._conn.execute(
                "INSERT INTO pii_tokens (party_id, token, kind, value, created_at) VALUES (?, ?, ?, ?, ?)",
                (party_id, token, kind, value, now),
            )
            self._conn.commit()
            return token

    def tokens(self, party_id: int) -> dict[str, tuple[str, str]]:
        """``{placeholder: (kind, value)}`` of this creator, the numbers that are still kept."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, kind, value FROM pii_tokens WHERE party_id = ? AND purged_at IS NULL", (party_id,)
            ).fetchall()
        return {r["token"]: (r["kind"], r["value"]) for r in rows}

    def token_rows(self, party_id: int) -> list[dict]:
        """Every placeholder of this creator, cleared ones too (``masked`` is what the console shows)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, kind, value, masked, purged_at FROM pii_tokens WHERE party_id = ? ORDER BY created_at",
                (party_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_tokens(self, party_id: int, masked: Callable[[str, str], str], now: Optional[float] = None) -> int:
        """Clear the numbers of this creator, keep their masked form; returns how many."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, kind, value FROM pii_tokens WHERE party_id = ? AND purged_at IS NULL", (party_id,)
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE pii_tokens SET masked = ?, value = '', purged_at = ? WHERE party_id = ? AND token = ?",
                    (masked(row["kind"], row["value"]), now, party_id, row["token"]),
                )
            self._conn.commit()
        return len(rows)

    # ── shipments ─────────────────────────────────────────────────────────

    def add_shipment(self, deal_id: int, carrier: str, waybill: str, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO shipments (deal_id, carrier, waybill, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (deal_id, carrier, waybill, now, now),
            )
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM shipments WHERE id = ?", (cur.lastrowid,)).fetchone())

    def latest_shipment(self, deal_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shipments WHERE deal_id = ? ORDER BY id DESC LIMIT 1", (deal_id,)
            ).fetchone()
        return dict(row) if row else None

    def shipments(self, deal_id: int) -> list[dict]:
        """A deal's shipments, newest first."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM shipments WHERE deal_id = ? ORDER BY id DESC", (deal_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_shipment_status(self, shipment_id: int, status: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("UPDATE shipments SET status = ?, updated_at = ? WHERE id = ?",
                               (status, now, shipment_id))
            self._conn.commit()

    # ── send gates (8.1) ──────────────────────────────────────────────────

    def gates(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM send_gates ORDER BY kind").fetchall()
        return {r["kind"]: dict(r) for r in rows}

    def gate(self, kind: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM send_gates WHERE kind = ?", (kind,)).fetchone()
        return dict(row) if row else None

    def set_gate(self, kind: str, route: str, now: Optional[float] = None, reset: bool = False) -> None:
        now = time.time() if now is None else now
        with self._lock:
            if reset:
                self._conn.execute("UPDATE send_gates SET route = ?, reset_at = ?, updated_at = ? WHERE kind = ?",
                                   (route, now, now, kind))
            else:
                self._conn.execute("UPDATE send_gates SET route = ?, updated_at = ? WHERE kind = ?", (route, now, kind))
            self._conn.commit()

    # ── events (the audit trail and the backlog for the next draft) ───────

    def add_event(self, deal_id: int, party_id: int, kind: str, detail: Optional[dict] = None, *,
                  actor: str = "", forwarded: bool = True, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO deal_events (deal_id, party_id, kind, detail, actor, forwarded, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (deal_id, party_id, kind, json.dumps(detail or {}, ensure_ascii=False), actor, int(bool(forwarded)), now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def events(self, deal_id: Optional[int] = None, limit: Optional[int] = 50, kinds: tuple = (),
               since: Optional[float] = None) -> list[dict]:
        """The newest events first (of these ``kinds`` only, when given; recorded at ``since`` or later,
        when given; all of them when ``limit`` is None)."""
        sql, where, args = "SELECT * FROM deal_events", [], []
        if deal_id is not None:
            where.append("deal_id = ?")
            args.append(deal_id)
        if since is not None:
            where.append("at >= ?")
            args.append(float(since))
        if kinds:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args += list(kinds)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [event_view(dict(r)) for r in rows]

    def backlog(self, deal_id: int) -> list[dict]:
        """What ``take_backlog`` would hand the next draft job now, oldest first, without taking it."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM deal_events WHERE deal_id = ? AND forwarded = 0 AND kind IN"
                f" ({', '.join('?' * len(BACKLOG_KINDS))}) ORDER BY id", (deal_id, *BACKLOG_KINDS),
            ).fetchall()
        return [event_view(dict(r)) for r in rows]

    def take_backlog(self, deal_id: int) -> list[dict]:
        """The creator's and the operators' messages the next draft job has not heard of, oldest
        first; they count as told."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM deal_events WHERE deal_id = ? AND forwarded = 0 AND kind IN"
                f" ({', '.join('?' * len(BACKLOG_KINDS))}) ORDER BY id", (deal_id, *BACKLOG_KINDS),
            ).fetchall()
            if rows:
                self._conn.executemany("UPDATE deal_events SET forwarded = 1 WHERE id = ?", [(r["id"],) for r in rows])
                self._conn.commit()
        return [event_view(dict(r)) for r in rows]

    def rewrite_events(self, deal_id: int, rewrite: Callable[[str], str]) -> int:
        changed = 0
        with self._lock:
            rows = self._conn.execute("SELECT id, detail FROM deal_events WHERE deal_id = ?", (deal_id,)).fetchall()
            for row in rows:
                new = rewrite(row["detail"] or "{}")
                if new != row["detail"]:
                    self._conn.execute("UPDATE deal_events SET detail = ? WHERE id = ?", (new, row["id"]))
                    changed += 1
            self._conn.commit()
        return changed

    # ── sample orders in 聚水潭 (PRM_SAMPLE_SYNC_MIN, src/prm/service.py) ──────

    def add_sample_order(self, order_no: str, *, packet_id: int, deal_id: int, party_id: int, made_at: float,
                         done: str = "", applied: Optional[dict] = None, now: Optional[float] = None) -> bool:
        """Start keeping a verified sample order (its packet, deal, creator, when it was made); True when it is
        new (an order kept already stays as it is)."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO sample_orders (order_no, packet_id, deal_id, party_id, made_at, done, applied,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_no, packet_id, deal_id, party_id, made_at, done, json.dumps(applied or {}, ensure_ascii=False),
                 now, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def sample_order(self, order_no: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sample_orders WHERE order_no = ?", (order_no,)).fetchone()
        return sample_order_view(dict(row)) if row else None

    def sample_orders(self, *, live: Optional[bool] = None, deal_id: Optional[int] = None) -> list[dict]:
        """The sample orders kept, oldest first: ``live`` True only those still asked about (``done`` empty), False
        only those that are not; of one deal (``deal_id``)."""
        sql, where, args = "SELECT * FROM sample_orders", [], []
        if live is not None:
            where.append("done = ''" if live else "done != ''")
        if deal_id is not None:
            where.append("deal_id = ?")
            args.append(deal_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY made_at, order_no", args).fetchall()
        return [sample_order_view(dict(r)) for r in rows]

    def update_sample_order(self, order_no: str, now: Optional[float] = None, **fields) -> None:
        allowed = {"status", "carrier", "waybill", "sent_at", "signed_at", "reason", "misses", "checked_at", "done",
                   "applied"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a sample order field: {sorted(unknown)}")
        if "applied" in fields:
            fields["applied"] = json.dumps(fields["applied"] or {}, ensure_ascii=False)
        fields["updated_at"] = time.time() if now is None else now
        names = sorted(fields)
        with self._lock:
            self._conn.execute(f"UPDATE sample_orders SET {', '.join(f'{n} = ?' for n in names)} WHERE order_no = ?",
                               (*[fields[n] for n in names], order_no))
            self._conn.commit()

    def rewrite_sample_orders(self, deal_id: int, rewrite: Callable[[str], str], now: Optional[float] = None) -> int:
        """Rewrite what a deal's sample orders keep in 聚水潭's words (why rejected or deleted: ``reason``, and the
        same in ``applied``) (8.2 clean-up); returns how many changed."""
        now = time.time() if now is None else now
        changed = 0
        with self._lock:
            rows = self._conn.execute("SELECT order_no, reason, applied FROM sample_orders WHERE deal_id = ?",
                                      (deal_id,)).fetchall()
            for row in rows:
                old = (row["reason"] or "", row["applied"] or "{}")
                new = tuple(rewrite(value) for value in old)
                if new != old:
                    self._conn.execute("UPDATE sample_orders SET reason = ?, applied = ?, updated_at = ?"
                                       " WHERE order_no = ?", (*new, now, row["order_no"]))
                    changed += 1
            self._conn.commit()
        return changed

    # ── 抖音百应 private messages (PRM_BUYIN_SYNC_MIN, src/prm/service.py) ─────

    def buyin_thread(self, conversation_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM buyin_threads WHERE conversation_id = ?",
                                     (conversation_id,)).fetchone()
        return buyin_thread_view(dict(row)) if row else None

    def buyin_threads(self, *, party_id: Optional[int] = None, deal_id: Optional[int] = None,
                      matched: Optional[bool] = None) -> list[dict]:
        """The 百应 conversations kept, the latest her line first: of one creator (``party_id``) or deal
        (``deal_id``); ``matched`` True only those of a registered creator, False only the others."""
        sql, where, args = "SELECT * FROM buyin_threads", [], []
        if party_id is not None:
            where.append("party_id = ?")
            args.append(party_id)
        if deal_id is not None:
            where.append("deal_id = ?")
            args.append(deal_id)
        if matched is not None:
            where.append("party_id IS NOT NULL" if matched else "party_id IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY COALESCE(last_creator_at, 0) DESC, conversation_id",
                                      args).fetchall()
        return [buyin_thread_view(dict(r)) for r in rows]

    def save_buyin_thread(self, conversation_id: str, now: Optional[float] = None, **fields) -> None:
        """Keep a 百应 conversation (made when new) with ``fields`` set; ``seen`` is a list, ``invited`` a dict or None
        (stored as JSON), ``other_invite`` a flag."""
        allowed = {"party_id", "deal_id", "nickname", "creator_uid", "aweme_id", "seen", "last_creator_at",
                   "last_shop_at", "answered_upto", "unanswered", "buyin_id", "invited", "contacts", "other_invite"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a 百应 conversation field: {sorted(unknown)}")
        if "seen" in fields:
            fields["seen"] = json.dumps(list(fields["seen"] or []), ensure_ascii=False)
        if "invited" in fields:
            fields["invited"] = json.dumps(dict(fields["invited"] or {}), ensure_ascii=False)
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO buyin_threads (conversation_id, created_at, updated_at)"
                               " VALUES (?, ?, ?)", (conversation_id, now, now))
            fields["updated_at"] = now
            names = sorted(fields)
            self._conn.execute(f"UPDATE buyin_threads SET {', '.join(f'{n} = ?' for n in names)}"
                               " WHERE conversation_id = ?", (*[fields[n] for n in names], conversation_id))
            self._conn.commit()

    def drop_buyin_threads(self, *, unmatched_before: float) -> int:
        """Forget the conversations of nobody on the list that were quiet since ``unmatched_before``; returns how many."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM buyin_threads WHERE party_id IS NULL AND updated_at < ?",
                                     (float(unmatched_before),))
            self._conn.commit()
            return cur.rowcount

    def meta_value(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM prm_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta_value(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO prm_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                               " value = excluded.value", (key, str(value)))
            self._conn.commit()

    # ── 加百应达人微信 (PRM_FRIEND_ADD, src/prm/friend_adds.py; CODEBASE_REFERENCE 功能 R) ─────

    def add_friend_add(self, *, state: str, now: Optional[float] = None, **fields) -> tuple[dict, bool]:
        """A new record in ``state`` (``fields``: ``FRIEND_ADD_FIELDS``; dicts and lists are stored as JSON).  With a
        ``message_id`` one 百应 message is one record: the one there is comes back.  Returns (record, made)."""
        now = time.time() if now is None else now
        values = _friend_add_values(fields)
        values.update(state=state, created_at=now, updated_at=now)
        names = sorted(values)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO friend_adds ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                [values[n] for n in names])
            self._conn.commit()
            if cur.rowcount == 1:
                row = self._conn.execute("SELECT * FROM friend_adds WHERE id = ?", (cur.lastrowid,)).fetchone()
                return friend_add_view(dict(row)), True
            row = self._conn.execute("SELECT * FROM friend_adds WHERE conversation_id = ? AND message_id = ?",
                                     (values.get("conversation_id", ""), values.get("message_id", ""))).fetchone()
        return friend_add_view(dict(row)), False

    def friend_add(self, add_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM friend_adds WHERE id = ?", (int(add_id),)).fetchone()
        return friend_add_view(dict(row)) if row else None

    def friend_add_by_message(self, conversation_id: str, message_id: str) -> Optional[dict]:
        """The record of one 百应 message (None for none, or no message id)."""
        if not message_id:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM friend_adds WHERE conversation_id = ? AND message_id = ?",
                                     (conversation_id, message_id)).fetchone()
        return friend_add_view(dict(row)) if row else None

    def friend_adds(self, *, states: Optional[tuple] = None, aweme_id: Optional[str] = None,
                    conversation_id: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
        """The records, newest first: in these ``states``, of this 抖音号 (any case), made from this 百应 conversation."""
        sql, where, args = "SELECT * FROM friend_adds", [], []
        if states:
            where.append(f"state IN ({', '.join('?' * len(states))})")
            args += list(states)
        if aweme_id is not None:
            where.append("LOWER(aweme_id) = LOWER(?)")
            args.append(str(aweme_id))
        if conversation_id is not None:
            where.append("conversation_id = ?")
            args.append(str(conversation_id))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [friend_add_view(dict(r)) for r in rows]

    def move_friend_add(self, add_id: int, from_states: tuple, to_state: str, now: Optional[float] = None,
                        **fields) -> bool:
        """Compare-and-set: the record goes to ``to_state`` (and ``fields`` are set) only if it is in one of
        ``from_states``; a double click or two threads move it once."""
        values = _friend_add_values(fields)
        values["updated_at"] = time.time() if now is None else now
        names = sorted(values)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE friend_adds SET state = ?{''.join(f', {n} = ?' for n in names)}"
                f" WHERE id = ? AND state IN ({', '.join('?' * len(from_states))})",
                (to_state, *[values[n] for n in names], int(add_id), *from_states))
            self._conn.commit()
            return cur.rowcount == 1

    def update_friend_add(self, add_id: int, now: Optional[float] = None, *, states: Optional[tuple] = None,
                          **fields) -> bool:
        """Set ``fields`` of a record (only while it is in ``states``, when given); True when it was changed."""
        values = _friend_add_values(fields)
        values["updated_at"] = time.time() if now is None else now
        names = sorted(values)
        sql = f"UPDATE friend_adds SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?"
        args = [*[values[n] for n in names], int(add_id)]
        if states:
            sql += f" AND state IN ({', '.join('?' * len(states))})"
            args += list(states)
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.rowcount == 1

    def clear_friend_add_values(self, add_id: int, now: Optional[float] = None) -> bool:
        """Clear a record's full numbers (``masked`` stays); True when there was one to clear."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "UPDATE friend_adds SET wechat = '', phone = '', cleared_at = COALESCE(cleared_at, ?), updated_at = ?"
                " WHERE id = ? AND (wechat != '' OR phone != '')", (now, now, int(add_id)))
            self._conn.commit()
            return cur.rowcount == 1

    def delete_friend_adds(self, *, closed_before: float) -> int:
        """Forget the records that ended (``closed_at``) before ``closed_before``, their attempts too; how many."""
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM friend_adds WHERE closed_at IS NOT NULL AND closed_at < ?", (float(closed_before),))]
            if ids:
                marks = ", ".join("?" * len(ids))
                self._conn.execute(f"DELETE FROM friend_add_attempts WHERE add_id IN ({marks})", ids)
                self._conn.execute(f"DELETE FROM friend_adds WHERE id IN ({marks})", ids)
                self._conn.commit()
        return len(ids)

    def scrub_friend_adds(self, party_id: int, rewrite: Callable[[str], str], now: Optional[float] = None) -> int:
        """8.2 clean-up of a creator: her records' numbers are cleared (once more: linking cleared them already) and
        their nicknames rewritten (numbers masked), her 抖音 links' nicknames too; returns how many changed."""
        now = time.time() if now is None else now
        changed = 0
        with self._lock:
            for row in self._conn.execute("SELECT id, nickname, wechat, phone FROM friend_adds WHERE party_id = ?",
                                          (int(party_id),)).fetchall():
                nickname = rewrite(row["nickname"] or "")
                if nickname != row["nickname"] or row["wechat"] or row["phone"]:
                    self._conn.execute(
                        "UPDATE friend_adds SET nickname = ?, wechat = '', phone = '', cleared_at = COALESCE(cleared_at, ?),"
                        " updated_at = ? WHERE id = ?", (nickname, now, now, row["id"]))
                    changed += 1
            for row in self._conn.execute("SELECT platform, account, nickname FROM creator_links WHERE party_id = ?",
                                          (int(party_id),)).fetchall():
                nickname = rewrite(row["nickname"] or "")
                if nickname != row["nickname"]:
                    self._conn.execute("UPDATE creator_links SET nickname = ? WHERE party_id = ? AND platform = ?"
                                       " AND account = ?", (nickname, int(party_id), row["platform"], row["account"]))
                    changed += 1
            self._conn.commit()
        return changed

    def add_friend_attempt(self, add_id: int, *, by: str, kind: str, now: Optional[float] = None) -> int:
        """One add_friend call for a record (``by`` console / admin, ``kind`` wechat / phone); returns its id."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute("INSERT INTO friend_add_attempts (add_id, at, by, kind) VALUES (?, ?, ?, ?)",
                                     (int(add_id), now, str(by), str(kind)))
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_friend_attempt(self, attempt_id: int, reason: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("UPDATE friend_add_attempts SET reason = ?, finished_at = ? WHERE id = ?",
                               (str(reason), now, int(attempt_id)))
            self._conn.commit()

    def friend_attempts(self, *, since: Optional[float] = None, add_id: Optional[int] = None,
                        unfinished: bool = False) -> list[dict]:
        """The add_friend calls, newest first: made at ``since`` or later, of one record, or not finished."""
        sql, where, args = "SELECT * FROM friend_add_attempts", [], []
        if since is not None:
            where.append("at >= ?")
            args.append(float(since))
        if add_id is not None:
            where.append("add_id = ?")
            args.append(int(add_id))
        if unfinished:
            where.append("finished_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY at DESC, id DESC", args).fetchall()
        return [dict(r) for r in rows]

    def add_creator_link(self, party_id: int, account: str, *, platform: str = "dy", uid: str = "",
                         uid_source: str = "", nickname: str = "", source: str, add_id: Optional[int] = None,
                         now: Optional[float] = None) -> bool:
        """Who a registered creator is on 抖音 (an operator confirmed it); the first link of an account stays.  True
        when it is new."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO creator_links (party_id, platform, account, uid, uid_source, nickname, source,"
                " add_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(party_id), platform, str(account), str(uid or ""), str(uid_source or ""), str(nickname or ""),
                 str(source), add_id, now))
            self._conn.commit()
            return cur.rowcount == 1

    def creator_links(self, party_id: Optional[int] = None, *, account: Optional[str] = None) -> list[dict]:
        """The 抖音 links, oldest first: of one creator, or of one 抖音号 (any case)."""
        sql, where, args = "SELECT * FROM creator_links", [], []
        if party_id is not None:
            where.append("party_id = ?")
            args.append(int(party_id))
        if account is not None:
            where.append("LOWER(account) = LOWER(?)")
            args.append(str(account))
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, account", args).fetchall()
        return [dict(r) for r in rows]

    def delete_creator_link(self, party_id: int, account: str, platform: str = "dy") -> bool:
        """An operator says a link was wrong (取消关联); True when there was one."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM creator_links WHERE party_id = ? AND platform = ? AND account = ?",
                                     (int(party_id), platform, str(account)))
            self._conn.commit()
            return cur.rowcount == 1
