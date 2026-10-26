"""The Windows WeChat edge protocol v1 (edge-protocol.md at the repository root).

What both sides share: names, limits, the checks of a request and the errors.  The
Mac business service imports this through ``client.py``; the Windows bot (``service.py``)
and the fake Windows server (``fake.py``) apply the same checks, so the protocol tests
can run one set of cases against both.

Standard library only, Python 3.9 compatible (the Mac side may run the system Python).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

PROTOCOL = 1
PATH_PREFIX = "/edge/"

# ── events ────────────────────────────────────────────────────────────────
CREATOR_MESSAGE = "creator_message"
OPERATOR_MESSAGE = "operator_message"
CHAT_NOTICE = "chat_notice"
CREATOR_MESSAGE_RECALLED = "creator_message_recalled"   # reserved: not sent in v1
SEND_RESULT = "send_result"
SEND_SEEN = "send_seen"
SEND_UNSEEN = "send_unseen"
EDGE_STATUS = "edge_status"
EVENT_TYPES = (CREATOR_MESSAGE, OPERATOR_MESSAGE, CHAT_NOTICE, CREATOR_MESSAGE_RECALLED,
               SEND_RESULT, SEND_SEEN, SEND_UNSEEN, EDGE_STATUS)

# ── sends ─────────────────────────────────────────────────────────────────
QUEUED, SENDING = "queued", "sending"
SENT, FAILED, EXPIRED, INDETERMINATE = "sent", "failed", "expired", "indeterminate"
FINAL_STATES = (SENT, FAILED, EXPIRED, INDETERMINATE)
SEND_STATES = (QUEUED, SENDING) + FINAL_STATES

# ── the edge's own state (edge_status, /edge/health) ─────────────────────
ONLINE, OFFLINE = "online", "offline"          # wechat ("verify" is reserved for later)
OK, UNREADABLE, FAILING = "ok", "unreadable", "failing"   # db: ok / unreadable; send: ok / failing

# ── limits ────────────────────────────────────────────────────────────────
MAX_TEXT_CHARS = 1500
MAX_UPLOADS = 9
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
TTL_MIN_SEC, TTL_MAX_SEC, TTL_DEFAULT_SEC = 60, 86400, 1800
MAX_WAIT_SEC, DEFAULT_WAIT_SEC = 30, 25
MAX_LIMIT, DEFAULT_LIMIT = 200, 50
MAX_BODY_BYTES = 1_000_000
MAX_PARTIES = 5000

# Defaults of the Windows settings (EDGE_* in .env) and fixed periods.
DEFAULT_SEND_MIN_GAP_SEC = 3
DEFAULT_SEND_PER_HOUR = 60
DEFAULT_SEEN_TEXT_SEC = 1800      # a sent text read back within this is seen; then send_unseen
DEFAULT_SEEN_FILE_SEC = 600       # the same for a send with files only
LATE_SEEN_SEC = 86400             # read back later than that window: still seen, not an operator
DEFAULT_KEEP_DAYS = 7             # events and files after the ack, finished sends' uploads
UNACKED_KEEP_DAYS = 30            # events the Mac never acknowledged
UPLOAD_UNUSED_HOURS = 24
FAILING_AFTER = 3                 # sends failed in a row before send: failing
HISTORY_SEC = 300                 # an own line older than this when read is history, not reported

SEND_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
WXID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{3,63}$")


class EdgeError(Exception):
    """A request the edge refuses: HTTP ``status`` and the body
    ``{"error": code, "detail": 中文说明, **extra}``.  ``close``: the request body may be
    unread (the connection must not be reused)."""

    def __init__(self, status: int, code: str, detail: str = "", close: bool = False, **extra):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.close = close
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, "detail": self.detail, **self.extra}


def bad_request(detail: str) -> EdgeError:
    return EdgeError(400, "bad_request", detail)


def _int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_request(f"{name} 要是整数")
    return value


def check_send(body) -> dict:
    """A send request as it is kept: ``{"send_id", "chat_id", "text", "uploads", "ttl_sec"}``,
    or EdgeError(400) saying what is wrong.  Whether ``chat_id`` is a party (403) and whether
    the uploads exist (400) is up to the edge that holds them."""
    if not isinstance(body, dict):
        raise bad_request("请求要是 JSON 对象")
    send_id = body.get("send_id")
    if not isinstance(send_id, str) or not SEND_ID.match(send_id):
        raise bad_request("send_id 要是 1–64 个字符，只能用字母、数字和 _-.:")
    chat_id = body.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise bad_request("要给出 chat_id")
    text = body.get("text")
    text = "" if text is None else text
    if not isinstance(text, str):
        raise bad_request("text 要是文字")
    if len(text) > MAX_TEXT_CHARS:
        raise bad_request(f"text 最长 {MAX_TEXT_CHARS} 字")
    uploads = body.get("uploads")
    uploads = [] if uploads is None else uploads
    if not isinstance(uploads, list) or not all(isinstance(u, str) and u for u in uploads):
        raise bad_request("uploads 要是 upload_id 的列表")
    if len(uploads) > MAX_UPLOADS:
        raise bad_request(f"一条最多 {MAX_UPLOADS} 个附件")
    ttl = body.get("ttl_sec", TTL_DEFAULT_SEC)
    ttl = TTL_DEFAULT_SEC if ttl is None else _int(ttl, "ttl_sec")
    if not TTL_MIN_SEC <= ttl <= TTL_MAX_SEC:
        raise bad_request(f"ttl_sec 要在 {TTL_MIN_SEC} 到 {TTL_MAX_SEC} 之间")
    if not text.strip() and not uploads:
        raise bad_request("没有要发的内容")
    return {"send_id": send_id, "chat_id": chat_id.strip(), "text": text, "uploads": list(uploads),
            "ttl_sec": ttl}


def send_digest(send: dict) -> str:
    """What makes two requests with one send_id the same request (the TTL may differ on a retry)."""
    key = json.dumps([send["chat_id"], send["text"], list(send["uploads"])], ensure_ascii=False)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def check_parties(body) -> tuple[int, list[str]]:
    """``(version, wxids)`` of a PUT /edge/parties, wxids stripped and without repeats."""
    if not isinstance(body, dict):
        raise bad_request("请求要是 JSON 对象")
    version = _int(body.get("version"), "version")
    if version < 1:
        raise bad_request("version 从 1 开始")
    wxids = body.get("wxids")
    if not isinstance(wxids, list) or not all(isinstance(w, str) for w in wxids):
        raise bad_request("wxids 要是文字的列表")
    if len(wxids) > MAX_PARTIES:
        raise bad_request(f"名单最多 {MAX_PARTIES} 人")
    return version, list(dict.fromkeys(w.strip() for w in wxids if w.strip()))


def parties_digest(wxids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(wxids))).encode("utf-8")).hexdigest()


def decide_parties(current_version: int, current_digest: str, version: int, wxids: list[str]) -> str:
    """``"apply"`` (newer), ``"same"`` (the same push again: a safe retry) or ``"conflict"`` (409)."""
    if version > current_version:
        return "apply"
    if version == current_version and parties_digest(wxids) == current_digest:
        return "same"
    return "conflict"


def party_rejection(wxid: str, reserved: Iterable[str] = ()) -> str:
    """Why this wxid cannot be on the list ("" = it can)."""
    if wxid.endswith("@chatroom"):
        return "群聊不能当达人"
    if not WXID.match(wxid):
        return "wxid 格式不对"
    if wxid in set(reserved):
        return "管理员或 cc 用户不能当达人"
    return ""


def clamp(value, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)
