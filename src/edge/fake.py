"""A fake Windows edge for the Mac side's tests: the whole ``/edge/*`` protocol, in memory.

It answers through the same HTTP code as the real Windows edge (http.py) and applies the
same checks (protocol.py); what WeChat would do is scripted::

    with FakeEdge(auto_send=False) as edge:              # starts on 127.0.0.1, a free port
        client = EdgeClient(edge.url, edge.token)
        client.put_parties(1, ["wxid_creator01"])
        edge.creator_says("wxid_creator01", "你好，我想申请寄样", files=[("图片-1.jpg", b"...")])
        ...                                              # the Mac pulls, drafts, sends
        edge.complete("pk-1")                            # the send went out: send_result sent
        edge.seen("pk-1", delay_s=3)                     # and was read back: send_seen
        edge.operator_says("wxid_creator01", "我来回")    # an operator typed by hand
        edge.set_status(wechat="offline", detail="微信没开")

``auto_send=True`` (the default) sends every accepted request at once (and, with
``auto_seen``, reads it back); ``fail_next`` makes the next sends fail; ``restart`` turns
a send in flight (``start_sending``) into ``indeterminate``.

Standard library only, Python 3.9 compatible.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from typing import Callable, Iterable, Optional

from . import protocol as P
from .http import serve

_IMAGE = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe(name: str) -> str:
    return _UNSAFE.sub("_", name).strip(" .")[:120] or "file"


class FakeEdge:
    def __init__(self, token: Optional[str] = None, *, reserved: Iterable[str] = ("wxid_admin",),
                 auto_send: bool = True, auto_seen: bool = True,
                 private_chats: Optional[list] = None, clock: Callable[[], float] = time.time):
        self.token = token or secrets.token_urlsafe(32)
        self.reserved = set(reserved)
        self.auto_send = auto_send
        self.auto_seen = auto_seen
        self.private_chats_list = list(private_chats or [])
        self._clock = clock
        self._cond = threading.Condition()       # guards everything; notified on every new event
        self._events: list[dict] = []
        self._acked = 0
        self._files: dict[tuple[int, int], str] = {}
        self._uploads: dict[str, dict] = {}
        self._sends: dict[str, dict] = {}
        self._parties: set[str] = set()
        self._parties_version = 0
        self._parties_digest = P.parties_digest([])
        self._status = {"wechat": P.ONLINE, "db": P.OK, "send": P.OK, "detail": ""}
        self._failures: list[str] = []
        self._seq = 0
        self._dir = tempfile.mkdtemp(prefix="fake-edge-")
        self._server = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> "FakeEdge":
        """Serve on 127.0.0.1 (a free port) and, like Windows, say the state once."""
        with self._cond:
            self._event(P.EDGE_STATUS, **{**self._status, "detail": "Windows 边缘启动了"})
        self._server = serve(self, self.token)
        return self

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> "FakeEdge":
        return self.start() if self._server is None else self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── what WeChat would do ──────────────────────────────────────────────

    def _event(self, kind: str, **fields) -> dict:
        """Append an event (the caller holds the lock)."""
        event = {"id": len(self._events) + 1, "type": kind, "at": round(self._clock(), 3), **fields}
        self._events.append(event)
        self._cond.notify_all()
        return dict(event)

    def _message(self, kind: str, chat_id: str, text: str, msg_type: int, message_id: Optional[str],
                 **extra) -> Optional[dict]:
        if chat_id not in self._parties:
            return None                          # not on the list: the edge does not forward it
        if message_id is None:
            self._seq += 1
            message_id = f"fake-{self._seq}"
        if any(e.get("message_id") == message_id and e.get("chat_id") == chat_id for e in self._events):
            return None                          # a message is forwarded once
        return self._event(kind, chat_id=chat_id, message_id=message_id, msg_type=msg_type, text=text, **extra)

    def creator_says(self, chat_id: str, text: str, *, msg_type: int = 1, quote: Optional[dict] = None,
                     files: Optional[list] = None, message_id: Optional[str] = None) -> Optional[dict]:
        """A creator's message.  ``files``: ``(name, data)`` / ``(name, data, kind)`` items, or
        ``{"name": …, "missing": "原因"}`` for one Windows could not get.  None when the chat is
        not on the list (or the message came already)."""
        with self._cond:
            if chat_id not in self._parties:
                return None                      # not on the list: the edge does not forward it
            entries, paths = [], {}
            for n, item in enumerate(files or [], 1):
                if isinstance(item, dict):
                    entries.append({"n": n, "name": str(item["name"]),
                                    "missing": str(item.get("missing") or "Windows 没取到这个文件")})
                    continue
                name, data, *rest = item
                kind = rest[0] if rest else ("image" if str(name).lower().endswith(_IMAGE) else "file")
                path = os.path.join(self._dir, f"in-{secrets.token_hex(6)}-{_safe(str(name))}")
                with open(path, "wb") as out:
                    out.write(bytes(data))
                paths[n] = path
                entries.append({"n": n, "name": str(name), "size": len(data), "kind": kind})
            extra = {"files": entries}
            if quote is not None:
                extra["quote"] = dict(quote)
            event = self._message(P.CREATOR_MESSAGE, chat_id, text, msg_type, message_id, **extra)
            if event is not None:
                for n, path in paths.items():
                    self._files[(event["id"], n)] = path
            return event

    def operator_says(self, chat_id: str, text: str, *, msg_type: int = 1,
                      message_id: Optional[str] = None) -> Optional[dict]:
        """An operator typed in the creator's chat by hand (on the PC or the phone)."""
        with self._cond:
            return self._message(P.OPERATOR_MESSAGE, chat_id, text, msg_type, message_id)

    def notice(self, chat_id: str, text: str) -> Optional[dict]:
        """A system line in the chat, e.g. 「对方撤回了一条消息」."""
        with self._cond:
            if chat_id not in self._parties:
                return None
            return self._event(P.CHAT_NOTICE, chat_id=chat_id, text=text)

    def set_status(self, *, wechat: Optional[str] = None, db: Optional[str] = None, send: Optional[str] = None,
                   detail: Optional[str] = None) -> Optional[dict]:
        """Change the edge's state; an ``edge_status`` event when something changed."""
        with self._cond:
            new = dict(self._status)
            for key, value in (("wechat", wechat), ("db", db), ("send", send), ("detail", detail)):
                if value is not None:
                    new[key] = value
            if new == self._status:
                return None
            self._status = new
            return self._event(P.EDGE_STATUS, **new)

    # ── sends, scripted ───────────────────────────────────────────────────

    def fail_next(self, n: int = 1, error: str = "文字没发出去") -> None:
        """The next ``n`` sends (automatic or ``complete``) fail with ``error``."""
        with self._cond:
            self._failures.extend([error] * n)

    def start_sending(self, send_id: str) -> None:
        with self._cond:
            record = self._sends[send_id]
            if record["status"] == P.QUEUED:
                record["status"] = P.SENDING

    def complete(self, send_id: str, status: str = P.SENT, error: str = "") -> dict:
        """End a queued (or sending) send: ``sent`` / ``failed`` / ``expired`` / ``indeterminate``."""
        with self._cond:
            return self._finish(self._sends[send_id], status, error)

    def _finish(self, record: dict, status: str, error: str = "") -> dict:
        if record["status"] in P.FINAL_STATES:
            return self._public(record)
        if status == P.SENT and self._failures:
            status, error = P.FAILED, self._failures.pop(0)
        record["status"] = status
        record["error"] = error
        if status == P.SENT:
            record["sent_at"] = round(self._clock(), 3)
        self._event(P.SEND_RESULT, send_id=record["send_id"], status=status, error=error)
        if status == P.SENT and record["seen_at"]:          # read back while it was going out
            self._event(P.SEND_SEEN, send_id=record["send_id"], delay_s=0)
        elif status == P.SENT and self.auto_send and self.auto_seen:
            self._seen(record, 1)
        return self._public(record)

    def _seen(self, record: dict, delay_s: float) -> None:
        """As on Windows: once per send; while it is still going out, ``send_seen`` waits for
        its ``send_result``; a send that did not go out is never seen."""
        if record["seen_at"] or record["status"] not in (P.QUEUED, P.SENDING, P.SENT):
            return
        record["seen_at"] = round(self._clock(), 3)
        if record["status"] == P.SENT:
            self._event(P.SEND_SEEN, send_id=record["send_id"], delay_s=delay_s)

    def seen(self, send_id: str, delay_s: float = 1) -> dict:
        """The sent message was read back from WeChat."""
        with self._cond:
            record = self._sends[send_id]
            self._seen(record, delay_s)
            return self._public(record)

    def unseen(self, send_id: str, window_s: int = P.DEFAULT_SEEN_TEXT_SEC) -> dict:
        """The read-back window passed without the message showing up."""
        with self._cond:
            record = self._sends[send_id]
            self._event(P.SEND_UNSEEN, send_id=send_id, window_s=window_s)
            return self._public(record)

    def restart(self) -> list[str]:
        """Windows restarted: a send in flight is ``indeterminate`` (never sent again)."""
        with self._cond:
            hit = [r for r in self._sends.values() if r["status"] == P.SENDING]
            for record in hit:
                self._finish(record, P.INDETERMINATE, "发送时 Windows 停了，不知道发出去没有")
            return [r["send_id"] for r in hit]

    def expire_due(self) -> list[str]:
        """Queued sends past their ``ttl_sec`` become ``expired``."""
        with self._cond:
            now = self._clock()
            hit = [r for r in self._sends.values() if r["status"] == P.QUEUED and now > r["deadline"]]
            for record in hit:
                self._finish(record, P.EXPIRED, "超过 ttl_sec 没发出去")
            return [r["send_id"] for r in hit]

    # ── what a test looks at ──────────────────────────────────────────────

    def _public(self, record: dict) -> dict:
        return {k: record[k] for k in ("send_id", "status", "error", "sent_at", "seen_at")}

    @property
    def sends(self) -> list[dict]:
        """Every send request, oldest first: ``send_id``, ``chat_id``, ``text``, ``uploads`` (file
        names), ``ttl_sec``, ``status``, ``error``, ``sent_at``, ``seen_at``."""
        with self._cond:
            return [{**self._public(r), "chat_id": r["chat_id"], "text": r["text"], "ttl_sec": r["ttl_sec"],
                     "uploads": [self._uploads[u]["name"] for u in r["uploads"]]} for r in self._sends.values()]

    def sent(self) -> list[tuple]:
        """``(chat_id, text, [file names])`` of what went out, in order."""
        return [(s["chat_id"], s["text"], s["uploads"]) for s in self.sends if s["status"] == P.SENT]

    @property
    def events_list(self) -> list[dict]:
        with self._cond:
            return [dict(e) for e in self._events]

    @property
    def acked_upto(self) -> int:
        return self._acked

    @property
    def parties(self) -> list[str]:
        return sorted(self._parties)

    @property
    def parties_version(self) -> int:
        return self._parties_version

    # ── the protocol (http.py calls these) ────────────────────────────────

    def health(self) -> dict:
        with self._cond:
            pending = sum(1 for e in self._events if e["id"] > self._acked)
            return {"ok": True, "protocol": P.PROTOCOL, "parties_version": self._parties_version,
                    **self._status, "can_send_files": True, "events_pending": pending}

    def events(self, after: int, wait: float, limit: int) -> list[dict]:
        deadline = time.monotonic() + wait
        with self._cond:
            while True:
                fresh = [dict(e) for e in self._events if e["id"] > after][:limit]
                remaining = deadline - time.monotonic()
                if fresh or remaining <= 0:
                    return fresh
                self._cond.wait(remaining)

    def ack(self, upto: int) -> dict:
        with self._cond:
            self._acked = max(self._acked, min(int(upto), len(self._events)))
            pending = sum(1 for e in self._events if e["id"] > self._acked)
            return {"ok": True, "pending": pending}

    def event_file(self, event_id: int, n: int) -> Optional[str]:
        return self._files.get((int(event_id), int(n)))

    def put_parties(self, version: int, wxids: list[str]) -> dict:
        with self._cond:
            decision = P.decide_parties(self._parties_version, self._parties_digest, version, wxids)
            if decision == "conflict":
                raise P.EdgeError(409, "conflict", "名单版本要比现在的大（同一次推送重试除外）",
                                  current_version=self._parties_version)
            rejected = [{"wxid": w, "reason": P.party_rejection(w, self.reserved)} for w in wxids
                        if P.party_rejection(w, self.reserved)]
            if decision == "apply":
                kept = {w for w in wxids if not P.party_rejection(w, self.reserved)}
                for record in self._sends.values():
                    if record["status"] == P.QUEUED and record["chat_id"] not in kept:
                        self._finish(record, P.FAILED, "不在名单里")
                self._parties = kept
                self._parties_version = version
                self._parties_digest = P.parties_digest(wxids)
            return {"ok": True, "version": self._parties_version, "rejected": rejected}

    def private_chats(self) -> list[dict]:
        with self._cond:
            return [dict(c) for c in self.private_chats_list
                    if c.get("wxid") not in self._parties and c.get("wxid") not in self.reserved]

    def upload(self, name: str, read: Callable[[int], bytes], length: int) -> dict:
        upload_id = f"up-{secrets.token_hex(6)}"
        path = os.path.join(self._dir, f"{upload_id}-{_safe(name)}")
        digest, left = hashlib.sha256(), length
        with open(path, "wb") as out:
            while left > 0:
                chunk = read(min(left, 1024 * 1024))
                if not chunk:
                    raise P.EdgeError(400, "incomplete", "上传没收全", close=True)
                digest.update(chunk)
                out.write(chunk)
                left -= len(chunk)
        with self._cond:
            self._uploads[upload_id] = {"name": name, "path": path, "size": length, "sha256": digest.hexdigest()}
        return {"upload_id": upload_id, "size": length, "sha256": digest.hexdigest()}

    def send(self, request: dict) -> dict:
        with self._cond:
            digest = P.send_digest(request)
            existing = self._sends.get(request["send_id"])
            if existing is not None:
                if existing["digest"] != digest:
                    raise P.EdgeError(409, "conflict", "这个 send_id 已经用过了，内容不一样")
                return {"send_id": existing["send_id"], "status": existing["status"]}
            if request["chat_id"] not in self._parties:
                raise P.EdgeError(403, "not_party", "只能发给名单里的私聊")
            for upload_id in request["uploads"]:
                if upload_id not in self._uploads:
                    raise P.bad_request(f"没有这个上传：{upload_id}")
                used_by = self._uploads[upload_id].get("send_id")
                if used_by and used_by != request["send_id"]:
                    raise P.bad_request(f"这个上传已经用在别的发送里了：{upload_id}")
            for upload_id in request["uploads"]:
                self._uploads[upload_id]["send_id"] = request["send_id"]
            now = self._clock()
            record = {**request, "digest": digest, "status": P.QUEUED, "error": "", "sent_at": None,
                      "seen_at": None, "created_at": now, "deadline": now + request["ttl_sec"]}
            self._sends[request["send_id"]] = record
            if self.auto_send:
                self._finish(record, P.SENT)
            return {"send_id": record["send_id"], "status": record["status"]}

    def send_status(self, send_id: str) -> Optional[dict]:
        with self._cond:
            record = self._sends.get(send_id)
            return None if record is None else self._public(record)
