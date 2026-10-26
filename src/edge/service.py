"""The Windows WeChat edge (``PRM_EDGE=on``): WeChat in and out for the Mac business service.

The flow (edge-protocol.md)::

    a listed creator writes ─► route step ``edge`` (before ``persist``) ─► ``handle``: the
    line (text.py), a reply's quote, pictures decoded and files found here ─► an event in
    edge.db ─► the Mac pulls ``/edge/events``, acknowledges ─► … ─► ``POST /edge/sends``
    ─► the sender thread: pacing, TTL, text then files, once per send_id ─► ``send_result``
    ─► the line shows up in WeChat (``on_own_message``): ``send_seen``; an own line that is
    no send of ours: ``operator_message``; the read-back window passed: ``send_unseen``.

Nothing is ever said to a creator here unless the Mac asked for it, and a creator's
messages are not written to the chat archive.  Windows keeps no creator content for long:
events and their files go ``EDGE_KEEP_DAYS`` after the Mac acknowledged them, uploads a
day after nobody used them, the text of finished sends after ``EDGE_KEEP_DAYS``.  Logs
carry ids and counts only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from . import protocol as P
from . import text as T
from .store import EdgeStore

logger = logging.getLogger(__name__)

STATUS_CHECK_SEC = 30
CLEANUP_SEC = 3600
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe(name: str) -> str:
    return _UNSAFE.sub("_", str(name)).strip(" .")[:120] or "file"


def _private_names_file(path: str = "data/private_chat_names.json") -> dict:
    """wxid -> name of the bot account's private chats (written by the Windows backend)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class EdgeService:
    def __init__(self, config, *, store: Optional[EdgeStore] = None,
                 send_text: Optional[Callable[[str, str], bool]] = None,
                 send_file: Optional[Callable[[str, str], bool]] = None,
                 clock: Callable[[], float] = time.time,
                 files_dir: Optional[Path] = None,
                 collect: Optional[Callable[[dict, str, int], tuple]] = None,
                 own_wxid: Optional[Callable[[], str]] = None,
                 wechat_online: Optional[Callable[[], Optional[bool]]] = None,
                 db_readable: Optional[Callable[[], bool]] = None,
                 private_names: Optional[Callable[[], dict]] = None,
                 on_parties_changed: Optional[Callable[[], None]] = None):
        self._config = config
        self._store = store if store is not None else EdgeStore(getattr(config, "edge_db", "data/edge.db"))
        self._send_text = send_text
        self._send_file = send_file
        self._clock = clock
        self._files_dir = Path(files_dir or "data/edge_files")
        self._collect = collect or self._resolve
        self._own_wxid = own_wxid or (lambda: "")
        self._wechat_online = wechat_online or (lambda: True)
        self._db_readable = db_readable or (lambda: True)
        self._private_names = private_names or _private_names_file
        self.on_parties_changed = on_parties_changed
        self._min_gap = float(getattr(config, "edge_send_min_gap_sec", P.DEFAULT_SEND_MIN_GAP_SEC))
        self._per_hour = int(getattr(config, "edge_send_per_hour", P.DEFAULT_SEND_PER_HOUR))
        self._seen_text_sec = float(getattr(config, "edge_seen_text_sec", P.DEFAULT_SEEN_TEXT_SEC))
        self._seen_file_sec = float(getattr(config, "edge_seen_file_sec", P.DEFAULT_SEEN_FILE_SEC))
        self._keep_sec = float(getattr(config, "edge_keep_days", P.DEFAULT_KEEP_DAYS)) * 86400
        # The admin and the cc users are never on the list.
        self._reserved = {str(w).strip() for w in [getattr(config, "admin_wxid", ""),
                                                    *(getattr(config, "claude_relay_wxids", None) or [])]
                          if str(w).strip()}
        self._parties = self._store.parties()
        self._cond = threading.Condition()       # notified when an event is stored
        self._send_lock = threading.Lock()       # send requests and the list change one at a time
        self._seen_lock = threading.Lock()
        self._lines: dict[str, deque] = {}       # chat -> (text, sent at, send_id): this run's sends
        self._files: dict[str, deque] = {}       # chat -> (sent at, send_id of a files-only send or None)
        self._started = clock()
        self._status = {"wechat": P.ONLINE, "db": P.OK, "send": P.OK, "detail": ""}
        self._failures = 0
        self._last_send_at = 0.0
        self._last_status_check = 0.0
        self._last_cleanup = 0.0
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def store(self) -> EdgeStore:
        return self._store

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, run_thread: bool = True) -> None:
        """Settle what a stopped edge left half done, say the state, then the sender thread
        (``run_thread=False``: the caller runs ``tick``, as the tests do)."""
        for record in self._store.sends_in((P.SENDING,)):
            self._finish(record["send_id"], P.INDETERMINATE,
                         "发送时 Windows 停了，不知道发出去没有；请到微信里看一眼，需要就手动补发")
        self._status = {**self._probe_status(), "detail": "Windows 边缘启动了"}
        self._emit(P.EDGE_STATUS, **self._status)
        self._last_status_check = self._clock()
        if run_thread:
            self._thread = threading.Thread(target=self._loop, name="edge-sender", daemon=True)
            self._thread.start()
        logger.info("EDGE: started (%d creator(s) on the list, version %d)", len(self._parties),
                    self._store.parties_version())

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("EDGE: sending round failed")
            self._wake.wait(1.0)
            self._wake.clear()

    def tick(self) -> None:
        """One round of the sender thread (tests call it directly)."""
        now = self._clock()
        if now - self._last_status_check >= STATUS_CHECK_SEC:
            self._last_status_check = now
            self._check_status()
        self._expire(now)
        self._send_due(now)
        self._check_unseen(now)
        if now - self._last_cleanup >= CLEANUP_SEC:
            self._last_cleanup = now
            self.cleanup(now)

    # ── events ────────────────────────────────────────────────────────────

    def _emit(self, kind: str, chat_id: str = "", message_id: Optional[str] = None, files=(), **fields):
        event = self._store.add_event(kind, self._clock(), chat_id, message_id, files, **fields)
        if event is not None:
            with self._cond:
                self._cond.notify_all()
        return event

    # ── from WeChat ───────────────────────────────────────────────────────

    def is_party_chat(self, chat_id: str) -> bool:
        """Is this private chat on the list? (the backend then reads it)"""
        return str(chat_id) in self._parties

    def _recent(self, msg: dict, now: float) -> bool:
        sent_at = float(msg.get("timestamp", 0) or 0)
        return not sent_at or now - sent_at <= P.HISTORY_SEC

    def handle(self, msg: dict) -> bool:
        """The route step's call: True when this is a listed creator's private message (it
        is then forwarded, silently; never answered here)."""
        if msg.get("is_group", True):
            return False
        chat_id = str(msg.get("chat_id", ""))
        if chat_id not in self._parties:
            return False
        now = self._clock()
        if not self._recent(msg, now):
            logger.info("EDGE: an old message of a listed chat is history, not forwarded")
            return True
        message_id = str(msg.get("message_id") or "") or None
        if T.is_notice(msg):
            self._emit(P.CHAT_NOTICE, chat_id, message_id, text=T.notice_text(msg))
            return True
        files = self._collect_files(msg)
        fields = {"msg_type": T.msg_type_of(msg), "text": T.message_text(msg)}
        quote = T.quote_of(msg, self._own_wxid(), chat_id)
        if quote is not None:
            fields["quote"] = quote
        event = self._emit(P.CREATOR_MESSAGE, chat_id, message_id, files, **fields)
        if event is None:                                     # this message came already
            self._delete([f["path"] for f in files if f.get("owned") and f.get("path")])
            return True
        logger.info("EDGE: creator message -> event #%d (%d file(s))", event["id"], len(files))
        return True

    def _resolve(self, msg: dict, kind: str, number: int) -> tuple:
        from ..relay.attachments import Held, resolve
        return resolve(self._config, Held(kind, msg, self._clock()), number)

    def _collect_files(self, msg: dict) -> list[dict]:
        """The picture (decoded, copied here) or the file (where WeChat saved it) of a message."""
        from ..relay.attachments import attachment_kind
        kind = attachment_kind(msg)
        if not kind:
            return []
        fallback = "图片-1.jpg" if kind == "image" else (str(msg.get("file_name") or "").strip() or "文件")
        try:
            path, name, problem = self._collect(msg, kind, 1)
        except Exception:
            logger.exception("EDGE: the attachment of a creator's message could not be prepared")
            return [{"n": 1, "name": fallback, "missing": "附件出错了，没取到"}]
        if not path or not os.path.isfile(str(path)):
            return [{"n": 1, "name": name or fallback, "missing": problem or "Windows 没取到这个附件"}]
        name = name or fallback
        if kind == "file":                       # it can be large, and WeChat keeps it
            return [{"n": 1, "name": name, "path": str(path), "size": os.path.getsize(path), "kind": "file",
                     "owned": False}]
        folder = self._files_dir / "in" / f"{int(self._clock())}-{uuid.uuid4().hex[:8]}"
        folder.mkdir(parents=True, exist_ok=True)
        copy = folder / _safe(name)
        shutil.copyfile(path, copy)
        return [{"n": 1, "name": name, "path": str(copy), "size": copy.stat().st_size, "kind": "image",
                 "owned": True}]

    def on_own_message(self, msg: dict) -> None:
        """A line this WeChat account wrote in a listed chat (the backend's hook): one of ours
        read back (``send_seen``), or an operator's (``operator_message``)."""
        chat_id = str(msg.get("chat_id", ""))
        if msg.get("is_group", True) or chat_id not in self._parties:
            return
        now = self._clock()
        if not self._recent(msg, now):
            return                                            # history (a chat read for the first time)
        message_id = str(msg.get("message_id") or "") or None
        if T.is_notice(msg):
            self._emit(P.CHAT_NOTICE, chat_id, message_id, text=T.notice_text(msg))
            return
        matched, send_id = self._match_own(chat_id, msg, now)
        if matched:
            if send_id and self._store.mark_seen(send_id, now):
                record = self._store.send(send_id) or {}
                delay = round(max(0.0, now - float(record.get("sent_at") or now)), 1)
                self._emit(P.SEND_SEEN, send_id=send_id, delay_s=delay)
                logger.info("EDGE: send %s seen in WeChat %.0f s after sending", send_id, delay)
            return
        event = self._emit(P.OPERATOR_MESSAGE, chat_id, message_id, msg_type=T.msg_type_of(msg),
                           text=T.message_text(msg))
        if event is not None:
            logger.info("EDGE: an operator wrote in a listed chat by hand -> event #%d", event["id"])

    def _note_line(self, chat_id: str, text: str, send_id: str, at: float) -> None:
        with self._seen_lock:
            self._lines.setdefault(chat_id, deque(maxlen=200)).append((T.normalized(text), at, send_id))

    def _note_file(self, chat_id: str, send_id: Optional[str], at: float) -> None:
        with self._seen_lock:
            self._files.setdefault(chat_id, deque(maxlen=200)).append((at, send_id))

    def _match_own(self, chat_id: str, msg: dict, now: float) -> tuple[bool, Optional[str]]:
        """Is this own line one of our sends? ``(True, send_id or None)``."""
        if T.msg_type_of(msg) != 1 or T.is_reply(msg):
            with self._seen_lock:                 # a picture or file: one we sent lately?
                files = self._files.get(chat_id) or deque()
                while files:
                    at, send_id = files.popleft()
                    if now - at <= self._seen_file_sec:
                        return True, send_id
            return False, None
        line = T.normalized(str(msg.get("content") or ""))
        with self._seen_lock:
            lines = self._lines.get(chat_id) or deque()
            for item in list(lines):
                if now - item[1] > P.LATE_SEEN_SEC:
                    lines.remove(item)
                elif item[0] == line:
                    lines.remove(item)
                    return True, item[2]
        # Sent before this run (the bot restarted in between): the database, only what WeChat
        # has not shown yet; this run's sends are all in memory.
        for record in self._store.unconfirmed(chat_id, now - P.LATE_SEEN_SEC):
            if float(record["sent_at"]) < self._started and T.normalized(record["text"]) == line:
                return True, record["send_id"]
        return False, None

    # ── state ─────────────────────────────────────────────────────────────

    def _probe_status(self) -> dict:
        out = {"wechat": self._status["wechat"], "db": self._status["db"], "send": self._status["send"]}
        try:
            online = self._wechat_online()
        except Exception:
            online = False
        if online is not None:                   # None: busy sending right now, ask later
            out["wechat"] = P.ONLINE if online else P.OFFLINE
        try:
            out["db"] = P.OK if self._db_readable() else P.UNREADABLE
        except Exception:
            out["db"] = P.UNREADABLE
        return out

    def _set_status(self, detail: str, **changes) -> None:
        new = {**self._status, **changes}
        if all(new[k] == self._status[k] for k in ("wechat", "db", "send")):
            return
        new["detail"] = detail
        self._status = new
        self._emit(P.EDGE_STATUS, **new)
        logger.warning("EDGE: state now wechat=%s db=%s send=%s (%s)", new["wechat"], new["db"], new["send"], detail)

    def _check_status(self) -> None:
        probe = self._probe_status()
        if probe["wechat"] == P.OFFLINE and self._status["wechat"] != P.OFFLINE:
            detail = "找不到微信主窗口（没开、没登录或被关了）"
        elif probe["db"] == P.UNREADABLE and self._status["db"] != P.UNREADABLE:
            detail = "微信数据库读不了，达人的消息收不进来"
        else:
            detail = "恢复正常"
        self._set_status(detail, wechat=probe["wechat"], db=probe["db"])

    # ── sending ───────────────────────────────────────────────────────────

    def _expire(self, now: float) -> None:
        for record in self._store.sends_in((P.QUEUED,)):
            if now > float(record["deadline"]):
                self._finish(record["send_id"], P.EXPIRED, "超过 ttl_sec 没发出去，已经不发了")

    def _send_due(self, now: float) -> int:
        """Send the oldest queued request when the pace allows; how many went."""
        if self._status["wechat"] == P.OFFLINE:
            return 0                             # waits in the queue (until its ttl_sec)
        if self._min_gap > 0 and now - self._last_send_at < self._min_gap:
            return 0
        if self._per_hour > 0 and self._store.sent_since(now - 3600) >= self._per_hour:
            return 0
        queued = self._store.sends_in((P.QUEUED,), limit=1)
        if not queued:
            return 0
        self._deliver(queued[0])
        return 1

    def _deliver(self, record: dict) -> None:
        send_id, chat_id, text = record["send_id"], record["chat_id"], record["text"]
        if not self._store.move_send(send_id, (P.QUEUED,), P.SENDING):
            return
        self._last_send_at = self._clock()
        if chat_id not in self._parties:
            self._finish(send_id, P.FAILED, "不在名单里")
            return
        problem, text_sent = "", False
        if text.strip():
            try:
                ok = self._send_text is not None and bool(self._send_text(chat_id, text))
            except Exception:
                logger.exception("EDGE: send %s: the text failed", send_id)
                ok = False
            if ok:
                text_sent = True
                self._note_line(chat_id, text, send_id, self._clock())
            else:
                problem = "文字没发出去"
        for upload_id in [] if problem else record["uploads"]:
            upload = self._store.upload(upload_id)
            name = upload["name"] if upload else upload_id
            if self._send_file is None:
                problem = "这个微信后端不能发文件"
                break
            if upload is None or not os.path.isfile(upload["path"]):
                problem = f"文件「{name}」在 Windows 上找不到了"
                break
            try:
                ok = bool(self._send_file(chat_id, upload["path"]))
            except Exception:
                logger.exception("EDGE: send %s: a file failed", send_id)
                ok = False
            if not ok:
                problem = f"文件「{name}」没发出去"
                break
            self._note_file(chat_id, None if text.strip() else send_id, self._clock())
        if problem:
            self._finish(send_id, P.FAILED, f"文字已发出；{problem}" if text_sent else problem)
        else:
            self._finish(send_id, P.SENT)

    def _finish(self, send_id: str, status: str, error: str = "") -> None:
        now = self._clock()
        fields = {"error": error, "finished_at": now}
        if status == P.SENT:
            fields["sent_at"] = now
        if not self._store.move_send(send_id, (P.QUEUED, P.SENDING), status, **fields):
            return
        self._emit(P.SEND_RESULT, send_id=send_id, status=status, error=error)
        logger.info("EDGE: send %s %s%s", send_id, status, f" ({error})" if error else "")
        if status == P.SENT:
            self._failures = 0
            self._set_status("发送恢复正常", send=P.OK)
        elif status == P.FAILED:
            self._failures += 1
            if self._failures >= P.FAILING_AFTER:
                self._set_status(f"连续 {self._failures} 条没发出去：{error}", send=P.FAILING)

    def _check_unseen(self, now: float) -> None:
        for record in self._store.waiting_read_back():
            window = self._seen_text_sec if record["text"].strip() else self._seen_file_sec
            if now - float(record["sent_at"]) > window and self._store.mark_unseen(record["send_id"], now):
                self._emit(P.SEND_UNSEEN, send_id=record["send_id"], window_s=int(window))
                logger.warning("EDGE: send %s not seen in WeChat within %d s", record["send_id"], window)

    # ── cleaning up ───────────────────────────────────────────────────────

    def _delete(self, paths) -> None:
        for path in paths:
            try:
                os.remove(path)
                parent = os.path.dirname(path)
                if parent and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass

    def cleanup(self, now: Optional[float] = None) -> None:
        """Events and their files ``EDGE_KEEP_DAYS`` after the ack (unacknowledged: 30 days),
        uploads a day after nobody used them or after their send ended, the text of sends."""
        now = self._clock() if now is None else now
        self._delete(self._store.drop_events(now - self._keep_sec, now - P.UNACKED_KEEP_DAYS * 86400))
        self._delete(self._store.drop_uploads(now - P.UPLOAD_UNUSED_HOURS * 3600, now - self._keep_sec))
        self._store.clear_sends(now - self._keep_sec, now - P.UNACKED_KEEP_DAYS * 86400)

    # ── the protocol (http.py calls these) ────────────────────────────────

    def health(self) -> dict:
        return {"ok": True, "protocol": P.PROTOCOL, "parties_version": self._store.parties_version(),
                **self._status, "can_send_files": self._send_file is not None,
                "events_pending": self._store.pending()}

    def events(self, after: int, wait: float, limit: int) -> list[dict]:
        deadline = time.monotonic() + wait
        while True:
            found = self._store.events_after(after, limit)
            remaining = deadline - time.monotonic()
            if found or remaining <= 0 or self._stopping.is_set():
                return found
            with self._cond:
                self._cond.wait(min(remaining, 1.0))

    def ack(self, upto: int) -> dict:
        return {"ok": True, "pending": self._store.ack(upto, self._clock())}

    def event_file(self, event_id: int, n: int) -> Optional[str]:
        return self._store.event_file(event_id, n)

    def put_parties(self, version: int, wxids: list[str]) -> dict:
        with self._send_lock:
            current = self._store.parties_version()
            decision = P.decide_parties(current, self._store.parties_digest(), version, wxids)
            if decision == "conflict":
                raise P.EdgeError(409, "conflict", "名单版本要比现在的大（同一次推送重试除外）", current_version=current)
            rejected = [{"wxid": w, "reason": P.party_rejection(w, self._reserved)} for w in wxids
                        if P.party_rejection(w, self._reserved)]
            if decision == "apply":
                kept = [w for w in wxids if not P.party_rejection(w, self._reserved)]
                self._store.set_parties(version, P.parties_digest(wxids), kept)
                self._parties = set(kept)
                for record in self._store.sends_in((P.QUEUED,)):
                    if record["chat_id"] not in self._parties:
                        self._finish(record["send_id"], P.FAILED, "不在名单里")
                logger.info("EDGE: list of creators version %d: %d on it, %d refused", version, len(kept),
                            len(rejected))
                if self.on_parties_changed is not None:
                    try:
                        self.on_parties_changed()
                    except Exception:
                        logger.exception("EDGE: the WeChat backend could not reload its private chats")
            return {"ok": True, "version": self._store.parties_version(), "rejected": rejected}

    def private_chats(self) -> list[dict]:
        names = self._private_names() or {}
        chats = [{"wxid": str(w), "name": str(n)} for w, n in names.items()
                 if str(w) not in self._parties and str(w) not in self._reserved and not str(w).endswith("@chatroom")]
        return sorted(chats, key=lambda c: c["name"])[:300]

    def upload(self, name: str, read: Callable[[int], bytes], length: int) -> dict:
        upload_id = f"up-{uuid.uuid4().hex[:16]}"
        folder = self._files_dir / "uploads" / upload_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _safe(name)
        digest, left = hashlib.sha256(), length
        try:
            with open(path, "wb") as out:
                while left > 0:
                    chunk = read(min(left, 1024 * 1024))
                    if not chunk:
                        raise P.EdgeError(400, "incomplete", "上传没收全", close=True)
                    digest.update(chunk)
                    out.write(chunk)
                    left -= len(chunk)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self._store.add_upload(upload_id, name, str(path), length, digest.hexdigest(), self._clock())
        return {"upload_id": upload_id, "size": length, "sha256": digest.hexdigest()}

    def send(self, request: dict) -> dict:
        digest = P.send_digest(request)
        with self._send_lock:
            existing = self._store.send(request["send_id"])
            if existing is not None:
                if existing["digest"] != digest:
                    raise P.EdgeError(409, "conflict", "这个 send_id 已经用过了，内容不一样")
                return {"send_id": existing["send_id"], "status": existing["status"]}
            if request["chat_id"] not in self._parties:
                raise P.EdgeError(403, "not_party", "只能发给名单里的私聊")
            for upload_id in request["uploads"]:
                upload = self._store.upload(upload_id)
                if upload is None:
                    raise P.bad_request(f"没有这个上传：{upload_id}")
                if upload["send_id"] and upload["send_id"] != request["send_id"]:
                    raise P.bad_request(f"这个上传已经用在别的发送里了：{upload_id}")
            self._store.add_send(request, digest, self._clock())
        self._wake.set()
        logger.info("EDGE: send %s queued (%d chars, %d file(s))", request["send_id"], len(request["text"]),
                    len(request["uploads"]))
        return {"send_id": request["send_id"], "status": P.QUEUED}

    def send_status(self, send_id: str) -> Optional[dict]:
        record = self._store.send(send_id)
        if record is None:
            return None
        return {k: record[k] for k in ("send_id", "status", "error", "sent_at", "seen_at")}
