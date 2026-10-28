"""
WCDB Native Backend — zero external dependencies.

Reads WeChat messages directly from the encrypted WCDB database via
patched wcdb_api.dll (ctypes).  Uses WeChatWindowController for sending.

WCDB native access — reads encrypted database in-process, no external dependencies.

Fallback (``OCR_FALLBACK=on``): while the database cannot be opened or read, the open
chat is read from a screenshot of the WeChat window (src/wechat/ocr); sending is unchanged.
"""
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from ..edge import text as edge_text
from .base import AbstractWeChatBackend, MessageCallback
from .burst import MessageBurstBuffer
from .wcdb_client import WcdbCallError, WcdbNativeClient
from .quiet_send import QuietSendSettings
from .window_controller import SendRefused, WeChatWindowController
from .helpers import DedupSet, format_nontext_content, normalize_msg_type
from src.pii import mask_pii
from src.wechat.media import IMAGE_MSG_TYPES, find_existing_media_path_from_message
from ..relay.service import MAX_AGE_SEC
from ..send.bubbles import get_sender
from ..send.queue import SendQueue

logger = logging.getLogger(__name__)

DEFAULT_POLL_SEC = 1.0
# Message ids remembered per chat (a read shows the last 50).  One set per chat: a busy group
# never pushes a quiet chat's history out of it (which would hand that history on again).
CHAT_KNOWN_SIZE = 500
# PRM_CATCHUP_HOURS: how many of a registered creator's latest messages are read back on
# startup and after a re-initialisation, to find the ones the bot never handled.
CATCHUP_READ = 200
MAX_CONSECUTIVE_ERRORS = 5   # trigger reinit after this many consecutive failures
SESSION_REFRESH_SEC = 60.0
# catch_up_chat: a chat asked for that is not read within this long is given up on (logged).
CATCH_UP_WAIT_SEC = 600.0
# WeChat 4 takes a message back by rewriting its row in place (same local_id and time, type 10000,
# the recall's XML): the rows of a watched private chat written this long ago are looked at again
# on every poll (_recall_in_place).  WeChat allows a recall for 2 minutes; the rest is margin for
# this PC's clock and a slow poll.
RECALL_WINDOW_SEC = 300
# PC WeChat was offline (src/wechat/presence.py): rows it brings in after logging in again (synced
# from the phone: new ids, old times) are read back and handed on as catch-up while it is offline
# and this long after it is back; a row written up to GAP_MARGIN_SEC before the gap began counts too.
GAP_WATCH_SEC = 3600
GAP_MARGIN_SEC = 60
# data/private_chat_names.json is written by the poll loop and the new-friend scan.
_PRIVATE_NAMES_LOCK = threading.Lock()
# OCR cannot see who wrote a line of a group chat.
OCR_SENDER = "OCR群友"
APPMSG_TYPE = 49
QUOTE_APPMSG_SUBTYPE = 57
_HEX_BLOB_RE = re.compile(r"^[0-9a-fA-F]{80,}$")
_MEDIA_MD5_RE = re.compile(r"\bmd5=[\"']([0-9a-fA-F]{16,64})[\"']")
_PICTURE_FIELD_LOGS = [0]       # how many picture messages have had their field names logged
_SPECIAL_PRIVATE_TALKERS = {
    "filehelper", "fmessage", "medianote", "floatbottle", "qqmail",
    "tmessage", "qmessage", "qqsync", "weixin", "weixinreminder",
    "newsapp", "feedsapp", "brandsessionholder", "notification_messages",
    "officialaccounts", "notifymessage", "mphelper",
}


def _backoff_sec(consecutive_errors: int) -> float:
    """Seconds to wait after the n-th failed poll in a row: 2, 4, 8, 16, then 1 again after a reinit."""
    return min(2 ** min(consecutive_errors % MAX_CONSECUTIVE_ERRORS, 5), 30)


def _ocr_from_config(config, window):
    """The OCR fallback the configuration asks for (``OCR_FALLBACK=on``), else None."""
    if getattr(config, "ocr_fallback", "off") != "on":
        return None
    from .ocr import OcrFallback
    from .ocr.win_ocr import WindowsOcrEngine
    return OcrFallback(
        WindowsOcrEngine(window=window),
        after=getattr(config, "ocr_fallback_after", 3),
        interval=getattr(config, "ocr_poll_sec", 4.0),
    )


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _row_time(msg: dict) -> int:
    """A WCDB row's ``create_time`` (0 when it has none)."""
    try:
        return int(msg.get("create_time", msg.get("createTime", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _row_id(msg: dict, talker: str) -> str:
    """The message id of a WCDB row with a ``local_id`` without standardizing it (``_message_ids``),
    "" for a row without one."""
    local_id = msg.get("local_id", msg.get("localId"))
    local_id = "" if local_id is None else str(local_id).strip()
    return "" if local_id in ("", "0") else _md5(f"{talker}|{local_id}|{_row_time(msg)}")


# SendRefused (window_controller): what a send the backend does not go through with returns, falsy, with
# the reason.  Why the window is not used for a chat (see WcdbBackend._window_target):
UNKNOWN_NAME = "机器人不知道这个聊天在微信里叫什么，没法在微信里找到它，没有发。请手动发"
NAMESAKE = ("和另一个聊天同名（都叫「{name}」），机器人没法确定发给谁，没有发：请手动发，"
            "或者在微信里给对方设一个不重复的备注（改好后重启 Windows 上的机器人）")
# SEND_VERIFY_CHAT: the search opened another chat than the one asked for (read from the screen).
WRONG_CHAT = ("微信搜索「{name}」打开的是「{title}」，不是要发的聊天，没有发：请手动发，或者给对方设一个"
              "不和别人重名、也不被别人的名字包含的备注（改好后重启 Windows 上的机器人）")
VERIFY_LOOKS = 3                 # a title that reads as another chat is read again this often, VERIFY_PAUSE_SEC apart
VERIFY_PAUSE_SEC = 0.6           # (the screen may not show the opened chat yet)
VERIFY_OFF_SEC = 600             # the screen could not be read (no OCR language, window too small): not tried for this long


def _ocr_key(name: str) -> str:
    """A chat name as OCR can read it back: letters, digits and CJK only (OCR does not read emoji or most
    symbols), no case."""
    return "".join(ch for ch in str(name or "") if ch.isalnum()).casefold()


def title_verdict(title: str, name: str, others, is_group: bool) -> str:
    """What the chat title read from the screen says about the chat the search opened for ``name``:
    ``ok`` (it is that chat), ``other`` (it is another chat the bot knows: ``others``, their ``_ocr_key``),
    ``group`` (a group's title, with its member count, while a private chat was asked for), ``unknown``
    (nothing read, cut off, misread, a name the bot does not know: no judgement)."""
    from .ocr.reader import clean_group_name

    raw = " ".join(str(title or "").split())
    if not raw:
        return "unknown"
    shown = clean_group_name(raw)
    key = _ocr_key(shown)
    if not key:
        return "unknown"
    if key == _ocr_key(name):
        return "ok"
    if key in others:
        return "other"
    if not is_group and shown != raw:                # 「名字 (35)」: a group
        return "group"
    return "unknown"


def _name_key(name: str) -> str:
    """A chat name as WeChat's search may match it: no spaces, no case."""
    return "".join(str(name or "").split()).casefold()


def _message_ids(msg: dict, talker: str, sender: str, content: str, ts: int) -> tuple[str, list[str]]:
    """``(message id, older ids)`` of a WCDB row (not a group's join line: that has its own).

    The id never changes for a row: md5 of the chat, its ``local_id`` and its time.  WeChat
    (4.1.15) writes a line this account sends with no server id and fills one in about a
    second later; the id that used the server id changed then, and the same row was handed
    on twice (PRM took the bot's own reply read back for an operator's and took the chat
    over; the WeChat edge reported ``send_seen`` and then ``operator_message`` for it).  A
    row without a ``local_id``: sender, text and time, as before, never the server id.

    The older ids are what versions up to 2026-10-27 made of the row: from its server id, or
    from its local id while the server id was still empty.  The message store still holds
    those, so a message handled before the update counts as stored (``_catchup_plan``,
    ``MessageStore.insert_message``) and is not handed on a second time."""
    local_id = msg.get("local_id", msg.get("localId"))
    local_id = "" if local_id is None else str(local_id).strip()
    if local_id in ("", "0"):
        msg_id = _md5(f"{sender}|{content}|{ts}|{ts}")
    else:
        msg_id = _md5(f"{talker}|{local_id}|{ts}")
    old_local = str(msg.get("local_id", ""))           # exactly as those versions read the fields
    old_key = str(msg.get("server_id", "")) or old_local or f"{sender}|{content}|{ts}"
    older = [_md5(f"{old_key}|{ts}")]
    if old_local:
        older.append(_md5(f"{old_local}|{ts}"))
    return msg_id, [i for i in dict.fromkeys(older) if i != msg_id]


def _looks_like_wechat_blob(content: str) -> bool:
    """Detect raw WCDB media payloads that were misreported as text."""
    compact = re.sub(r"\s+", "", content or "")
    return bool(_HEX_BLOB_RE.fullmatch(compact))


def _extract_media_hint(content: str) -> str:
    """Extract an emoji/image cache identifier (the md5 that names its files) from WeChat XML content.

    WeChat 4 may hand the XML over zstd compressed and hex encoded; that is unpacked too.
    """
    text = str(content or "")
    match = _MEDIA_MD5_RE.search(text) or _MEDIA_MD5_RE.search(_decompress_zstd_hex(text))
    return match.group(1).lower() if match else ""


def _message_local_type(msg: dict) -> int:
    """The message type: the low 32 bits of WCDB's ``local_type``.  WeChat 4 packs an app message's
    kind into the high 32 bits (a reply (57 << 32) | 49, 拍一拍 (62 << 32) | 49 = 266287972401); that
    kind is ``_message_subtype``'s, and the standardized message carries it as ``msg_subtype``."""
    raw = msg.get("local_type", msg.get("localType", msg.get("msg_type", 1)))
    try:
        value = int(raw)
        if value > 0xFFFFFFFF:
            return value & 0xFFFFFFFF
        return value
    except (TypeError, ValueError):
        return normalize_msg_type(raw)


def _message_subtype(msg: dict) -> int:
    """The high 32 bits of WCDB's ``local_type`` (an app message's kind), 0 when there are none."""
    raw = msg.get("local_type", msg.get("localType", msg.get("msg_type", 1)))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value > 0xFFFFFFFF:
        return value >> 32
    return 0


def _decompress_zstd_hex(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    if not compact.startswith("28b52ffd") or len(compact) % 2 != 0:
        return ""
    try:
        import zstandard as zstd
    except Exception:
        logger.warning(
            "zstandard is not installed; cannot decode a compressed WeChat message"
        )
        return ""
    try:
        raw = bytes.fromhex(compact)
        data = zstd.ZstdDecompressor().decompress(raw)
        return data.decode("utf-8", errors="replace").strip()
    except Exception:
        logger.debug("Failed to decompress zstd WeChat payload", exc_info=True)
        return ""


def _extract_xml_document(text: str) -> str:
    if not text:
        return ""
    start = text.find("<msg")
    if start < 0:
        start = text.find("<?xml")
    if start < 0:
        return ""
    return text[start:].strip()


def _extract_file_appmsg(content: str) -> dict | None:
    """``{"name", "size"}`` of a file someone sent (an appmsg of type 6), else None."""
    decoded = _decompress_zstd_hex(content) or content
    xml_text = _extract_xml_document(decoded)
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    appmsg = root if root.tag == "appmsg" else root.find("appmsg")
    if appmsg is None or _text_at(appmsg, "type") != "6":
        return None
    name = _text_at(appmsg, "title")
    if not name:
        return None
    try:
        size = int(_text_at(appmsg, "appattach/totallen") or 0)
    except ValueError:
        size = 0
    return {"name": name, "size": size}


def _text_at(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    value = node.findtext(path, default="")
    return str(value or "").strip()


def _extract_quote_appmsg(content: str,
                          msg_subtype: int = 0) -> dict[str, str] | None:
    decoded = _decompress_zstd_hex(content) or content
    xml_text = _extract_xml_document(decoded)
    if not xml_text:
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.debug("Failed to parse WeChat appmsg XML", exc_info=True)
        return None

    appmsg = root.find("appmsg")
    msg_type = _text_at(appmsg, "type")
    if msg_type != str(QUOTE_APPMSG_SUBTYPE) and msg_subtype != QUOTE_APPMSG_SUBTYPE:
        return None

    refer = appmsg.find("refermsg") if appmsg is not None else None
    if refer is None:
        return None

    title = _text_at(appmsg, "title")
    quoted_content = _text_at(refer, "content")
    return {
        "content": title or "[引用消息]",
        "quoted_sender_id": _text_at(refer, "chatusr"),
        "quoted_sender_name": _text_at(refer, "displayname"),
        "quoted_content": quoted_content,
        "quoted_server_id": _text_at(refer, "svrid"),
    }


def _special_info(content: str, local_type: int) -> dict:
    """What a link, mini program, card, location or system line is, for the WeChat edge
    (src/edge/text.py writes "[链接] 标题"); {} for anything else."""
    decoded = _decompress_zstd_hex(content) or str(content or "")
    if local_type == 10000:                      # a system line: plain text, or XML (a recall)
        text = decoded
        start = decoded.find("<")
        if start >= 0 and decoded[start:].lstrip().startswith("<"):
            try:
                root = ET.fromstring(decoded[start:].strip())
                # a recall: <replacemsg>, or (WeChat 4.1, a row rewritten in place, 2026-10-28)
                # <revokemsg><content> beside <revoketime>
                revoke = root if root.tag == "revokemsg" else root.find(".//revokemsg")
                text = (_text_at(root, ".//replacemsg") or _text_at(revoke, "content")
                        or " ".join(t.strip() for t in root.itertext() if t.strip()))
            except ET.ParseError:
                text = re.sub(r"<[^>]+>", " ", decoded)
        return {"kind": "system", "title": " ".join(str(text).split())[:200]}
    xml_text = _extract_xml_document(decoded)
    if not xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    if local_type == 42:                         # a contact card
        return {"kind": "card", "title": str(root.get("nickname") or "").strip()}
    if local_type == 48:                         # a location
        place = root if root.tag == "location" else root.find("location")
        if place is None:
            return {}
        return {"kind": "location", "title": str(place.get("poiname") or place.get("label") or "").strip()}
    if local_type == APPMSG_TYPE:
        appmsg = root if root.tag == "appmsg" else root.find("appmsg")
        if appmsg is None:
            return {}
        info = {"kind": "appmsg", "type": _text_at(appmsg, "type"), "title": _text_at(appmsg, "title")}
        url = _text_at(appmsg, "url")
        if url:
            info["url"] = url                    # a link's address (src/edge/text.py keeps http/https only)
        return info
    return {}


def _strip_wechat_group_sender_prefix(content: str) -> str:
    """Remove WCDB's leading ``wxid_xxx:\n`` prefix from group messages."""
    return re.sub(
        r"^\s*wxid_[a-zA-Z0-9_]+:\s*",
        "",
        str(content or ""),
        count=1,
    ).strip()


def _session_username(session: dict) -> str:
    return str(
        session.get("username")
        or session.get("userName")
        or session.get("user_name")
        or session.get("talker")
        or ""
    ).strip()


def _is_private_talker(username: str) -> bool:
    lowered = str(username or "").strip().lower()
    if not lowered or lowered.endswith("@chatroom"):
        return False
    if lowered in _SPECIAL_PRIVATE_TALKERS:
        return False
    if lowered.startswith("gh_"):
        return False
    if lowered.startswith("brand"):
        return False
    return True


def _matches_configured_chat(name: str, username: str,
                             configured: list[str]) -> bool:
    if not configured or configured == ["*"]:
        return True
    name_lower = name.lower()
    username_lower = username.lower()
    for item in configured:
        needle = item.strip().lower()
        if not needle:
            continue
        if needle in {"*", "all"}:
            return True
        if needle == username_lower or needle == name_lower:
            return True
        if needle in name_lower or needle in username_lower:
            return True
    return False


class WcdbBackend(AbstractWeChatBackend):
    """Native WCDB backend — database read + window send.

    Reads messages directly from WeChat's session.db via wcdb_api.dll
    with one-byte DRM patch. Sends via WeChatWindowController.

    Usage:
        backend = WcdbBackend(
            bot_display_name="机器人",
            groups=["摸鱼群"],
            poll_sec=1.0,
        )
        backend.start(my_callback)
    """

    def __init__(self,
                 bot_display_name: str = "",
                 groups: list[str] | None = None,
                 poll_sec: float = DEFAULT_POLL_SEC,
                 store=None,
                 config=None,
                 ocr=None):
        self._bot_name = bot_display_name
        self._groups = groups or []
        self._poll_sec = poll_sec
        self._store = store  # MessageStore fallback for name resolution
        self._running = False
        self._client: Optional[WcdbNativeClient] = None
        self._window = WeChatWindowController(
            quiet=QuietSendSettings.from_config(config),
            paste_files_as=str(getattr(config, "sticker_paste", "file")),
        )
        # A quiet send that waits for the user gives up once the bot stops.
        self._window.alive = lambda: self._running
        # OCR_FALLBACK=on: the open chat is read from the screen while the database cannot be read.
        self._ocr = ocr if ocr is not None else _ocr_from_config(config, self._window)
        # True for the first cycle after an OCR stretch: what the database returns then is the
        # outage's backlog (part of it was answered from the screen), so it is marked seen only.
        self._ocr_priming = False
        # SEND_QUEUE=on: one thread sends every reply, so typing pauses and the
        # wait for the user never hold a worker (src/send/queue.py).
        self._send_queue: SendQueue | None = (
            SendQueue(typing=get_sender().typing, alive=lambda: self._running)
            if getattr(config, "send_queue", "off") == "on" else None
        )
        # The chats read, by chat id (wxid / @chatroom) -> the name WeChat shows (each one is polled;
        # two chats may have the same name), and every chat of the last session list read whose name
        # is known, read or not (a send refuses a name another chat has: _window_target).
        # ``_groups`` stays what WECHAT_GROUPS says (_resolve_groups reads it again on a re-open).
        self._chat_names: dict[str, str] = {}
        self._session_names: dict[str, str] = {}
        self._chat_is_group: dict[str, bool] = {}
        self._last_session_refresh: float = 0.0
        # PRM_COPILOT: private chats read whatever PRIVATE_CHATS says (the registered creators),
        # and who hears this account's own lines there (an operator's hand-typed reply).
        self._private_filter = None
        self._own_message_hook = None
        self._db_failures = 0          # database reads failed in a row (the WeChat edge's state)
        # This poll cycle's chat reads: [made, answered with an error code] (_read_messages); every one
        # failing is a failed read of the database (_poll_cycle).  And the chats whose reads fail while
        # the others read (chat id -> the error), said once in the log.
        self._reads = [0, 0]
        self._unreadable: dict[str, str] = {}
        self._known: dict[str, DedupSet] = {}      # per chat: the message ids seen (_known_set)
        # Per watched private chat (the private filter's): message id -> time of the ordinary rows
        # read that were written in the last RECALL_WINDOW_SEC; one rewritten into a recall is
        # handed on as a system line of its own (_recall_in_place).
        self._recallable: dict[str, dict[str, int]] = {}
        # PRM_CATCHUP_HOURS: per registered creator's chat, the messages the bot never handled
        # (found by _prime_chat; it was stopped, the database re-opened), oldest first, for the
        # chat's next poll; is a chat a creator's that the catch-up follows (_tracked_chat); how
        # far each was read, as last written to chat_read_marks.
        self._catchup: dict[str, list[dict]] = {}
        self._tracked: dict[str, bool] = {}
        self._read_marks: dict[str, int] = {}
        # The WeChat edge (PRM_EDGE): floor(chat) -> when the edge last handled the chat (or None);
        # its catch-up goes by that instead of the message store (set_catchup_floor).
        self._catchup_floor = None
        # catch_up_chat (a new friend registered as a creator, src/wechat/new_friends.py): per chat
        # (name, since, sink, asked at), taken by the chat's next poll; and the session list the
        # poll loop read last (monotonic time, list) for the new-friend scan (recent_sessions).
        self._catch_up_requests: dict[str, tuple] = {}
        self._catch_up_lock = threading.Lock()
        self._sessions_read: Optional[tuple] = None
        # PC WeChat offline (note_wechat_offline / catch_up_after_gap, src/wechat/presence.py): since when,
        # until when rows it brings in late are looked for (None: still offline), when it was seen back
        # (every creator's chat read back before that is read back again), when each chat was read back
        # for a gap.  Under _catch_up_lock.  And per chat, the rows a gap read-back looked at already.
        self._gap_since: Optional[float] = None
        self._gap_until: Optional[float] = None
        self._gap_back_at = 0.0
        self._gap_read_at: dict[str, float] = {}
        self._gap_looked: dict[str, set] = {}
        # Thread safety: the WCDB DLL (ctypes) may not be thread-safe internally.  The client itself
        # serializes every call into it, from any thread (wcdb_client._DLL_LOCK: the poll loop, the
        # new-friend scan, a re-initialisation's close and open).  This lock is held around the poll
        # loop's reads and every with_client call, so another reader gets one call in at a time.
        self._client_lock = threading.Lock()
        # WeChat window automation (sending, screenshots) takes seconds and
        # must not interleave, but it never touches the DLL: it has its own
        # lock so a reply being typed no longer stalls message polling.
        self._window_lock = threading.Lock()
        # Callback thread pool — fire-and-forget route callbacks so the poll
        # loop never blocks on a slow send.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        # Voice recognition pipeline (lazy-init when voice_asr_enabled)
        self._voice: Optional[object] = None
        self._config = config
        self._voice_config = config
        # SEND_VERIFY_CHAT: the chat title the search opened is read from the screen before anything is pasted
        # (_open_chat_check).  The reader is made on Windows on first use (tests give one).
        self._verify_chat = str(getattr(config, "send_verify_chat", "on") or "on") == "on"
        self._title_reader = None
        self._verify_off_until = 0.0
        self._burst = MessageBurstBuffer(
            enabled=bool(getattr(config, "message_burst_enabled", True)),
            window_sec=float(getattr(config, "message_burst_window_sec", 2.2)),
            max_messages=int(getattr(config, "message_burst_max_messages", 5)),
            max_chars=int(getattr(config, "message_burst_max_chars", 200)),
        )

    # ── Public API ─────────────────────────────────────────────────

    @property
    def window_controller(self) -> WeChatWindowController:
        """The controller of the WeChat window (friend management drives the same window)."""
        return self._window

    @property
    def window_lock(self) -> threading.Lock:
        """Held for every stretch of window automation; friend management takes it too."""
        return self._window_lock

    def chat_names(self) -> list[str]:
        """The names of the chats read and of every chat of the last session list: a WeChat window titled
        like one of them is a chat open in a window of its own (friend management never takes it for one
        of WeChat's dialogs)."""
        names = [name for _, name in list(self._chat_names.items())]
        names += [name for _, name in list(self._session_names.items())]
        return [name for name in dict.fromkeys(names) if name]

    def start(self, callback: MessageCallback) -> None:
        if not self._groups:
            logger.error("No groups configured. Set WECHAT_GROUPS in .env")
            return

        logger.info(
            "WcdbBackend starting (groups=%s, poll=%ss, bot=%r)",
            self._groups, self._poll_sec, self._bot_name,
        )

        # Init and open database, and read its chat list (an error code there: the database cannot
        # be read either, handled the same way)
        try:
            self._client = WcdbNativeClient()
            self._client.init()
            self._client.open()
            logger.info("WCDB database opened successfully")
            self._resolve_groups()
        except Exception as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to initialize WCDB: %s", e)
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
            # Push error to Web UI so the user sees a recovery path
            try:
                from src.web.server import update_status
                if self._ocr is None:
                    update_status(running=False, error=str(e))
                else:
                    update_status(error=f"数据库打不开，已改用 OCR 读取屏幕：{e}")
            except Exception:
                pass
            if self._ocr is None:
                return
            # OCR_FALLBACK=on: go on without the database.  The poll loop below fails on every
            # cycle, which turns the OCR fallback on and retries the database (_reinitialize).
            logger.warning("OCR_FALLBACK is on: starting without the WCDB database")
            self._client = None

        # Group talker IDs without the database (the database's were resolved above)
        if self._client is None:
            self._register_groups_without_database()

        if not self._chat_names:
            logger.error("No groups resolved. Check WECHAT_GROUPS.")
            return

        # Pre-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window pre-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found — will retry on first send")
        self._window.describe_chat_windows(list(dict.fromkeys(
            name for talker, name in self._chat_names.items() if self._chat_is_group.get(talker)
        )))

        if self._client is not None:
            self._prime_known_ids()
        self._last_session_refresh = time.monotonic()

        self._start_workers()
        self._running = True
        consecutive_errors = 0

        # Import once to avoid per-iteration overhead
        from src.web.server import is_shutting_down as _is_shutting_down

        try:
            while self._running and not _is_shutting_down():
                # The first cycle after an OCR stretch only marks the database's backlog as seen.
                self._ocr_priming = self._ocr is not None and self._ocr.active
                try:
                    self._poll_cycle(callback)
                    consecutive_errors = 0
                    self._db_failures = 0
                    if self._ocr is not None:
                        self._ocr.note_db_ok()
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    consecutive_errors += 1
                    self._db_failures += 1
                    if self._ocr is not None:
                        self._ocr.note_db_failure()

                    # After MAX_CONSECUTIVE_ERRORS consecutive failures,
                    # attempt full reinitialization (WeChat may have restarted).
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            "Hit %d consecutive errors — attempting "
                            "reinitialization...", consecutive_errors,
                        )
                        try:
                            self._reinitialize()
                            consecutive_errors = 0
                            continue
                        except Exception as reinit_err:
                            logger.error(
                                "Reinitialization failed: %s", reinit_err,
                            )
                            # Fall through to backoff; will retry next cycle.
                            push_error = str(reinit_err)
                            try:
                                from src.web.server import update_status
                                update_status(error=push_error)
                            except Exception:
                                pass

                    wait = _backoff_sec(consecutive_errors)
                    logger.warning(
                        "Poll error #%d (%s): %s. Retry in %ss...",
                        consecutive_errors, type(e).__name__, e, wait,
                    )
                    self._sleep_polling_ocr(wait, callback)
                finally:
                    self._ocr_priming = False
        finally:
            # Drain in-flight callbacks gracefully
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
            if self._send_queue is not None:
                # Let a send in progress finish (it gives back focus and clipboard).
                self._send_queue.stop(timeout=15.0)
            if self._client:
                self._client.close()
        logger.info("WcdbBackend stopped.")

    def _start_workers(self) -> None:
        """The callback pool and, with SEND_QUEUE=on, the sending thread."""
        # Typing pauses keep a worker busy for seconds per bubble, so leave room
        # for other chats (not with the send queue: then sending never waits on a worker).
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8 if get_sender().typing and self._send_queue is None else 4,
            thread_name_prefix="bot-cb-",
        )
        if self._send_queue is not None:
            self._send_queue.start()

    def send_text(self, chat_id: str, content: str) -> bool:
        """Send a text into a chat (the relay's results, PRM's drafts, the WeChat edge's sends): the
        window types the chat's name into WeChat's search.  A chat whose name cannot lead to it gets a
        falsy ``SendRefused`` with the reason, and nothing is typed (``_window_target``)."""
        if not content:
            return False

        chat_name, refused = self._window_target(chat_id)
        if refused is not None:
            return refused

        with self._window_lock:
            return self._send_and_confirm(chat_name, chat_id, content)

    def send_file(self, chat_id: str, path: str) -> bool:
        """Send a file into a chat (pasted, like a sticker): the Claude relay's long results, PRM's and
        the WeChat edge's files.  Refused like ``send_text``."""
        chat_name, refused = self._window_target(chat_id)
        if refused is not None:
            return refused
        with self._window_lock:
            return self._window.send_file_to_chat(chat_name, path, **self._check_kwargs(chat_id, chat_name))

    def stop(self) -> None:
        self._running = False
        if self._pool:
            self._pool.shutdown(wait=False)
        if self._send_queue is not None:
            self._send_queue.stop()

    # ── Recovery ─────────────────────────────────────────────────────

    def _reinitialize(self) -> None:
        """Close and re-open the WCDB client after persistent errors.

        Called when the poll loop hits MAX_CONSECUTIVE_ERRORS consecutive
        failures — typically because WeChat was restarted and the DB handle
        or HWND became stale.
        """
        logger.warning("Reinitializing WCDB backend after consecutive errors...")
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        try:
            self._client = WcdbNativeClient()
            self._client.init()
            self._client.open()
            logger.info("WCDB reinitialized successfully")
        except Exception as e:
            self._client = None
            logger.error("WCDB reinitialization failed: %s", e)
            raise
        # Re-resolve groups (talker IDs may have changed).  A failure here (the new handle cannot read
        # the chat list either) leaves what was seen as it was: the next try starts over.
        self._resolve_groups()
        # Forget what was seen: priming below marks what WCDB shows now (a row keeps its id,
        # _message_ids), and the catch-up finds a creator's messages from the gap.
        self._known = {}
        self._tracked = {}
        self._recallable = {}
        # What came in while the database could not be read: history, except a registered
        # creator's messages the bot never handled (PRM_CATCHUP_HOURS, _prime_chat).
        self._prime_known_ids()
        # Re-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window re-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found after reinit")

    # ── Group resolution ────────────────────────────────────────────

    def _resolve_groups(self) -> None:
        """Find the chats to read in WCDB's sessions: the groups WECHAT_GROUPS names (``*``: all),
        the private chats (PRIVATE_CHAT_ENABLED, PRIVATE_CHATS, set_private_filter).

        WCDB session records only contain usernames (e.g. 20968749111@chatroom).
        Display names must be resolved via the DLL's get_display_names() or
        the local nickname cache (WeChat contacts / manual overrides).  Chats are
        kept by their id (``_chat_names``): two of them may have the same name.
        """
        sessions = self._client.get_sessions()
        self._sessions_read = (time.monotonic(), sessions)
        self._chat_names = {}
        self._chat_is_group = {}

        # Build a map of all @chatroom entries: username -> {name, member_count}
        all_chatrooms: dict[str, dict] = {}
        all_private_chats: dict[str, dict] = {}
        private_seen: dict[str, str] = {}   # every private chat with a name (the console's creator picker)
        private_enabled = bool(
            getattr(self._config, "private_chat_enabled", False)
        )
        private_config = [
            g.strip()
            for g in str(getattr(self._config, "private_chats", "*")).split(",")
            if g.strip()
        ] or ["*"]
        auto_private_chats = any(
            item.strip().lower() in {"*", "all", ""}
            for item in private_config
        )
        my_wxid = str(getattr(self._client, "_config", {}).get("myWxid", "") or "")
        for s in sessions:
            username = _session_username(s)
            if not username:
                continue
            if my_wxid and (
                username == my_wxid
                or username.startswith(my_wxid)
                or my_wxid.startswith(username)
            ):
                continue

            display = self._resolve_session_display_name(s, username)

            if username.endswith("@chatroom"):
                if not display:
                    display = username

                all_chatrooms[username] = {
                    "name": display,
                    "member_count": 0,
                }
                continue

            if private_enabled and _is_private_talker(username):
                wanted = self._wants_private(username)        # a registered creator
                if auto_private_chats and not wanted and not username.lower().startswith("wxid_"):
                    logger.debug(
                        "Skipping private chat %s: not an ordinary wxid contact",
                        username[:20],
                    )
                    continue
                if not display or (display == username and username.startswith("wxid_")):
                    logger.debug(
                        "Skipping private chat %s: display name unresolved",
                        username[:20],
                    )
                    continue
                if not wanted and self._bot_name and (
                    display == self._bot_name
                    or self._bot_name in display
                ):
                    continue
                private_seen[username] = display
                if not wanted and not _matches_configured_chat(display, username, private_config):
                    continue
                all_private_chats[username] = {
                    "name": display,
                    "member_count": 1,
                }

        if not all_chatrooms:
            logger.warning(
                "No @chatroom sessions found in WCDB (total sessions: %d). "
                "Make sure WeChat is logged in and session.db is accessible.",
                len(sessions),
            )

        auto_discover = (
            not self._groups
            or (len(self._groups) == 1 and self._groups[0].strip() in ("*", "all", ""))
        )

        if auto_discover and all_chatrooms:
            for username, info in all_chatrooms.items():
                self._add_talker(info["name"], username, is_group=True)
            logger.info(
                "Auto-discovered %d group chats: %s",
                len(all_chatrooms), [v["name"] for v in all_chatrooms.values()],
            )

        elif not auto_discover:
            # Manual mode: match configured names against resolved display names
            for group_name in self._groups:
                found = None
                for username, info in all_chatrooms.items():
                    display = info["name"]
                    if group_name.lower() in display.lower() or display.lower() in group_name.lower():
                        found = username
                        break
                if found:
                    self._add_talker(group_name, found, is_group=True)
                    logger.info("Resolved '%s' -> %s (display='%s')", group_name, found, all_chatrooms[found]["name"])
                else:
                    # Direct lookup: maybe group_name IS a username like 20968749111@chatroom
                    if group_name in all_chatrooms:
                        self._add_talker(group_name, group_name, is_group=True)
                        logger.info("Resolved '%s' as direct username", group_name)
                    else:
                        logger.warning(
                            "Could not resolve group '%s'. Available: %s",
                            group_name, list(all_chatrooms.keys()),
                        )

        if private_enabled:
            added_private = 0
            for username, info in all_private_chats.items():
                if self._add_talker(info["name"], username, is_group=False):
                    added_private += 1
            logger.info(
                "Auto-discovered %d private chats for reply",
                added_private,
            )

        # Every chat of the list whose name is known, read or not: a send refuses a name one of
        # them has too (_namesake).  self._groups stays WECHAT_GROUPS: a re-open resolves it again.
        self._session_names = {
            **{username: info["name"] for username, info in all_chatrooms.items()}, **private_seen}

        # Persist chat_id -> display_name so the web UI can show
        # human-readable group names in the console's group picker.
        if all_chatrooms:
            self._save_group_names(all_chatrooms)
        if private_seen and self._private_filter is not None:
            self._save_private_names(private_seen)

        # ── Resolve and persist group members ──────────────────────────
        group_members: dict[str, dict[str, str]] = {}
        selected_group_ids = [
            username
            for username in self._chat_names
            if self._chat_is_group.get(username, username.endswith("@chatroom"))
        ]
        for username in selected_group_ids:
            info = all_chatrooms.get(username, {"name": username})
            try:
                members = self._client.get_group_members(username)
                if not members:
                    continue
                wxids = [m.get("username", "") for m in members if m.get("username")]
                if not wxids:
                    continue
                # Resolve display names in batches of 200
                names = {}
                for i in range(0, len(wxids), 200):
                    batch = wxids[i:i + 200]
                    names.update(self._client.get_display_names(batch))
                # Filter out unresolved (where name == wxid) and save
                group_members[username] = {
                    wxid: names.get(wxid, wxid)
                    for wxid in wxids
                }
                logger.info(
                    "Resolved %d/%d member names for %s",
                    len(group_members[username]), len(wxids), info["name"],
                )
            except Exception as e:
                logger.warning("Failed to resolve members for %s: %s", username, e)
        if group_members:
            self._save_group_members(group_members)

    def _resolve_session_display_name(self, session: dict, username: str) -> str:
        display = str(
            session.get("displayName") or session.get("displayname")
            or session.get("nickname") or session.get("nickName")
            or session.get("remark") or session.get("remarkName")
            or session.get("display_name")
            or ""
        ).strip()
        if not display:
            display = self._client.resolve_nickname(username)
        if not display or display == username:
            display = str(session.get("last_sender_display_name", "") or "").strip()
        return display or username

    def _add_talker(self, chat_name: str, talker: str, is_group: bool) -> bool:
        """Read this chat from the next poll on, under its id (False only without a name or id).  A
        name another chat read has too does not keep it out (that lost a creator's whole chat); the
        window is not used for either of them (_window_target)."""
        chat_name = str(chat_name or "").strip()
        talker = str(talker or "").strip()
        if not chat_name or not talker:
            return False

        key = _name_key(chat_name)
        namesake = next((other for other, name in list(self._chat_names.items())
                         if other != talker and _name_key(name) == key), "")
        if namesake:
            logger.warning(
                "Chat '%s' (%s) has the same name as %s: both are read; the bot sends to neither "
                "(WeChat's search could open the other one)", chat_name, talker[:20], namesake[:20],
            )

        self._chat_names[talker] = chat_name
        self._chat_is_group[talker] = bool(is_group)
        return True

    @staticmethod
    def _save_group_members(chat_members: dict[str, dict[str, str]]) -> None:
        """Persist chat_id -> {wxid: display_name} to data/group_members.json."""
        import os as _os
        path = Path("data/group_members.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chat_members, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            total = sum(len(m) for m in chat_members.values())
            logger.info("Saved %d member names across %d groups to %s", total, len(chat_members), path)
        except Exception as e:
            logger.warning("Failed to persist group_members.json: %s", e)

    @staticmethod
    def _save_group_names(chatrooms: dict[str, dict]) -> None:
        """Persist chat_id -> {name, member_count} to data/group_names.json atomically."""
        import os as _os
        path = Path("data/group_names.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            data = json.dumps(chatrooms, ensure_ascii=False, indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            _os.replace(tmp_path, path)
            logger.info(
                "Saved %d group-name mappings to %s",
                len(chatrooms), path,
            )
        except Exception as e:
            logger.warning("Failed to persist group_names.json: %s", e)

    @staticmethod
    def _save_private_names(chats: dict[str, str]) -> None:
        """Persist the private chats WeChat lists (wxid -> name) to data/private_chat_names.json, so
        the console can offer them when an operator registers a creator (PRM_COPILOT only)."""
        import os as _os
        path = Path("data/private_chat_names.json")
        try:
            with _PRIVATE_NAMES_LOCK:          # the new-friend scan writes here too (note_private_name)
                known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                known = known if isinstance(known, dict) else {}
                if all(known.get(k) == v for k, v in chats.items()):
                    return
                known.update(chats)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")
                _os.replace(tmp_path, path)
        except Exception as e:
            logger.warning("Failed to persist private_chat_names.json: %s", e)

    def _talker_to_name(self, talker_id: str) -> str:
        """The name WeChat shows for this chat ("" when not known): a chat read; else, for a private
        chat not read yet (a creator just put on the list, or one WeChat does not list any more: PRM
        or the WeChat edge sends to her first), the name the console's list of private chats has
        (data/private_chat_names.json), and the chat is read from the next poll on (fd9419b, the
        Windows PC 2026-10-26: our line and her answer are read at once).  A name another chat has
        too is added all the same; ``_window_target`` decides whether the window may use it."""
        name = self._chat_names.get(talker_id)
        if name:
            return name
        if talker_id.endswith("@chatroom"):
            return ""
        name = str(self._listed_private_names().get(talker_id) or "").strip()
        if name:
            self._add_talker(name, talker_id, is_group=False)
        return name

    @staticmethod
    def _listed_private_names() -> dict:
        """data/private_chat_names.json (every private chat WeChat listed with a name, read or not;
        written with PRM_COPILOT / PRM_EDGE): {} when there is none."""
        names_file = Path("data/private_chat_names.json")
        if not names_file.is_file():
            return {}
        try:
            data = json.loads(names_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _namesake(self, talker: str, name: str) -> str:
        """Another chat with this name (its id; "" when there is none): one the bot reads, one of the
        last session list, or a private chat the console's list has (a friend the bot does not read
        is found by the same search)."""
        key = _name_key(name)
        if not key:
            return ""
        for names in (self._chat_names, self._session_names, self._listed_private_names()):
            for other, other_name in list(names.items()):     # the poll thread may add chats meanwhile
                if other != talker and _name_key(str(other_name or "")) == key:
                    return other
        return ""

    def _window_target(self, talker: str, name: str = "") -> tuple[str, Optional[SendRefused]]:
        """The name to type into WeChat's search to reach this chat (a send, a reply, a screenshot),
        and why not (None: go ahead).  The window finds a chat only by that name and opens the first
        result (window_controller.navigate_to_chat; WeChat 4 cannot be asked which chat opened), so
        a name another chat has too could open that one: refused, nothing is typed (``NAMESAKE``: send
        it by hand, or give her a remark of her own).  ``name``: what to go by when the chat is not
        known (a message's ``group_name``)."""
        name = self._talker_to_name(talker) or str(name or "").strip()
        if not name:
            logger.error("Cannot resolve chat_id=%s to chat name", talker)
            return "", SendRefused(UNKNOWN_NAME)
        other = self._namesake(talker, name)
        if other:
            refused = SendRefused(NAMESAKE.format(name=name))
            logger.warning("Not using the WeChat window for %s: %s (the other chat: %s)",
                           talker[:20], refused.reason, other[:20])
            return name, refused
        return name, None

    # ── OCR fallback ─────────────────────────────────────────────────

    def _register_groups_without_database(self) -> None:
        """Group names to chat ids when there is no database to look them up in.

        The id only has to lead back to the name: sending goes through the name.
        ``*`` (all groups) cannot be listed without the database.
        """
        for name in self._groups:
            if name and name != "*":
                self._add_talker(name, f"ocr:{name}", is_group=True)

    def _sleep_polling_ocr(self, seconds: float, callback: MessageCallback) -> None:
        """``time.sleep`` that, while the OCR fallback is on, also reads the screen."""
        if self._ocr is None or not self._ocr.active:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            self._poll_ocr(callback)
            time.sleep(max(0.0, min(1.0, end - time.monotonic())))

    def _poll_ocr(self, callback: MessageCallback) -> None:
        for found in self._ocr.poll():
            ready = self._message_from_ocr(found)
            if ready is None:
                continue
            group_name, talker, standardized = ready
            msg_id = standardized["message_id"]
            known = self._known_set(talker)
            if msg_id in known:
                continue
            known.add(msg_id)
            # Not through the burst buffer: it holds a message until _poll_cycle flushes it (which
            # does not run while the database is down) and would merge different people's lines,
            # since every line has the same unknown speaker.  The looks are seconds apart anyway.
            self._dispatch_message(standardized, callback)

    def _group_for_title(self, title: str) -> tuple[str, str] | None:
        """``(name, chat id)`` of the group read that this window title is: the only one of that name,
        or (for a long name that the window shows cut off with an ellipsis) the only group that starts
        with what is shown.  Two groups of the name: None (the screen cannot tell which it is)."""
        groups = [(name, talker) for talker, name in list(self._chat_names.items())
                  if self._chat_is_group.get(talker)]
        wanted = _name_key(title)
        same = [group for group in groups if _name_key(group[0]) == wanted]
        if same:
            return same[0] if len(same) == 1 else None
        if wanted.endswith(("…", "...")):
            head = wanted.rstrip("….")
            if len(head) >= 3:
                starts = [group for group in groups if _name_key(group[0]).startswith(head)]
                if len(starts) == 1:
                    return starts[0]
        return None

    def _message_from_ocr(self, found):
        """``(group name, chat id, standard message)`` for a line read from the screen, or None.

        Only the groups in ``WECHAT_GROUPS`` are answered; a title with no member count is a
        private chat, which OCR does not serve.
        """
        text = str(found.text or "").strip()
        title = str(found.title_clean or "").strip()
        if not text or not title or not found.is_group:
            return None
        group = self._group_for_title(title)
        if group is None:
            return None
        group_name, talker = group
        timestamp = int(time.time())
        bot_names = {self._bot_name, getattr(self._config, "bot_display_name", None) or ""}
        bot_names.discard("")
        digest = hashlib.md5(f"ocr|{talker}|{text}|{timestamp}".encode("utf-8")).hexdigest()
        return group_name, talker, {
            "message_id": digest,
            "chat_id": talker,
            "group_name": group_name,
            "chat_name": group_name,
            "sender_id": OCR_SENDER,
            "sender_name": OCR_SENDER,
            "content": text,
            "msg_type": 1,
            "msg_subtype": 0,
            "server_id": "",
            "media_base_dir": "",
            "media_hint": "",
            "media_kind": "",
            "media_path": "",
            "file_name": "",
            "file_size": 0,
            "timestamp": timestamp,
            "is_at_mentioned": any(f"@{name}" in text for name in bot_names),
            "is_group": True,
            "is_system_join": False,
            "new_member_id": "",
            "is_reply_to_bot": False,
            "quoted_sender_id": "",
            "quoted_sender_name": "",
            "quoted_content": "",
            "quoted_server_id": "",
            "special": {},
            "_legacy_ids": [],                  # an OCR line's id was always made this way
        }

    # ── Message polling ──────────────────────────────────────────────

    def _poll_cycle(self, callback: MessageCallback) -> None:
        if self._client is None or not getattr(self._client, "is_open", False):
            raise RuntimeError("the WCDB database is not open")
        self._reads = [0, 0]
        self._refresh_private_chats_if_due()
        self._admit_catch_up_chats()
        for talker, group_name in list(self._chat_names.items()):     # every chat read, by its id
            if not self._running:
                break
            self._poll_group(group_name, talker, callback)
        self._flush_due_bursts(callback)
        made, failed = self._reads
        if made and failed == made:
            # Not one chat could be read: the database cannot be (a stale handle answers every call
            # with an error code).  Raised so the loop counts it: re-open, OCR fallback, db_readable.
            raise WcdbCallError("wcdb_get_messages", detail=(
                f"every chat read of this poll failed ({failed} of {made}): the WeChat database cannot be read"))
        # Check shutdown signal before sleeping so stop() is responsive
        if not self._running:
            return
        time.sleep(self._poll_sec)

    def set_private_filter(self, accept) -> None:
        """Also read the private chats ``accept(username)`` says yes to, whatever PRIVATE_CHATS
        says (PRM_COPILOT: the registered creators).  Needs PRIVATE_CHAT_ENABLED=true."""
        self._private_filter = accept
        self._tracked = {}

    def refresh_private_chats_soon(self) -> None:
        """The next poll looks for newly allowed private chats at once (a creator was registered)."""
        self._last_session_refresh = 0.0

    def set_catchup_floor(self, floor) -> None:
        """The WeChat edge (PRM_EDGE): ``floor(chat)`` is when it last handled a listed chat
        (``EdgeService.catchup_floor``; None: nothing kept).  With PRM_CATCHUP_HOURS on, the
        lines of a listed chat written after it are handed on after a restart (``_catchup``)."""
        self._catchup_floor = floor
        self._tracked = {}

    def set_own_message_hook(self, hook) -> None:
        """``hook(msg)`` hears this account's own lines in the private chats the filter wants
        (PRM_COPILOT: a registered creator's chat, where an operator may reply by hand)."""
        self._own_message_hook = hook

    def own_wxid(self) -> str:
        """This WeChat account's wxid ("" when not known yet)."""
        return str(getattr(self._client, "_config", {}).get("myWxid", "") or "") if self._client else ""

    # ── Readers beside the poll loop (src/wechat/new_friends.py) ─────

    def with_client(self, fn):
        """``fn(client)`` under the client lock: another reader of the WeChat database beside the
        poll loop (the new-friend scan), one call at a time so polling waits for one call at most.
        None while the database is not open."""
        with self._client_lock:
            client = self._client
            if client is None or not getattr(client, "is_open", False):
                return None
            return fn(client)

    def recent_sessions(self, max_age: float):
        """The session list the poll loop read last (``_refresh_private_chats_if_due``,
        ``_resolve_groups``) when it is at most ``max_age`` seconds old, else None."""
        seen = self._sessions_read
        if seen is None or time.monotonic() - seen[0] > max_age:
            return None
        return seen[1]

    def note_private_name(self, wxid: str, name: str) -> None:
        """A new friend's name (src/wechat/new_friends.py): a name lookup that missed before she was
        a contact is replaced (the poll loop then finds her chat), and with a private filter
        (PRM_COPILOT / PRM_EDGE) the console's and the Mac's lists of private chats offer her."""
        wxid, name = str(wxid or "").strip(), str(name or "").strip()
        if not wxid or not name or name == wxid:
            return
        remember = getattr(self._client, "remember_nickname", None)
        if callable(remember):
            remember(wxid, name)
        self._session_names[wxid] = name           # a send to someone else of her name is refused
        if self._private_filter is not None:
            self._save_private_names({wxid: name})

    def catch_up_chat(self, talker: str, name: str, since: float, sink) -> None:
        """Hand a private chat's messages written since ``since`` to ``sink(messages)`` on the chat's
        next poll, past the route (they may be stored already): a new friend just registered as a
        creator (PRM_COPILOT; ``sink`` is PrmService.take_new_friend through the new-friend service).
        Only the other party's words go (oldest first, standardized; not this account's lines,
        WeChat's system lines or friend notices); everything the chat shows then is marked seen, so
        nothing of it is handed on again.  A chat not read yet is read from the next poll on under
        ``name``."""
        with self._catch_up_lock:
            self._catch_up_requests[str(talker)] = (str(name or "").strip(), float(since), sink, time.monotonic())
        self._last_session_refresh = 0.0

    def _admit_catch_up_chats(self) -> None:
        """The chats of catch_up_chat requests that are not read yet are read from this cycle on."""
        if not self._catch_up_requests:
            return
        with self._catch_up_lock:
            pending = list(self._catch_up_requests.items())
        for talker, (name, _since, _sink, asked_at) in pending:
            if talker in self._chat_is_group:
                continue                                  # read already: its poll takes the request
            if time.monotonic() - asked_at > CATCH_UP_WAIT_SEC:
                with self._catch_up_lock:
                    self._catch_up_requests.pop(talker, None)
                logger.warning("Catch-up of a new friend's chat (%s…) given up: the chat could not be read",
                               talker[:8])
                continue
            if name:
                self._add_talker(name, talker, is_group=False)    # polled from this cycle on

    def _catch_up_new_friend(self, group_name: str, talker: str, request: tuple) -> bool:
        """A catch_up_chat request, in the poll thread before the chat's own poll: read the chat back
        (CATCHUP_READ messages), mark all of it seen, and hand the other party's messages since the
        request's time to its sink (stored first, so the restart catch-up counts them as handled).
        False when the chat could not be read (an error code: ``_read_messages``): the request waits for
        the next poll, and this poll leaves the chat alone (nothing is handed on before the catch-up)."""
        from .new_friends import is_friend_notice

        _name, since, sink, _asked = request
        try:
            messages = self._read_messages(talker, CATCHUP_READ)
        except Exception:
            with self._catch_up_lock:
                self._catch_up_requests.setdefault(talker, request)      # asked again at the next poll
            raise
        if messages is None:                          # this chat cannot be read now: nothing handed on
            with self._catch_up_lock:
                self._catch_up_requests.setdefault(talker, request)
            return False
        known = self._known_set(talker)
        my_wxid = self.own_wxid()
        picked, newest = [], None
        now = time.time()
        watched = self._wants_private(talker)                # registered: a recall in place is looked for
        for msg in reversed(messages or []):
            standardized = self._standardize(msg, group_name, talker, is_group=False)
            if standardized is None:
                continue
            ts = int(standardized.get("timestamp") or 0)
            newest = ts if newest is None else max(newest, ts)
            known.add(standardized["message_id"])
            if watched:
                self._note_recallable(talker, standardized, now)
            if (ts < since or self._is_own_line(standardized, my_wxid) or is_friend_notice(standardized)
                    or edge_text.is_notice(standardized)):     # WeChat's lines: a recall, 拍一拍 …
                continue
            if my_wxid:
                standardized["_not_self_by_id"] = True
            picked.append(standardized)
        if self._store is not None:
            for standardized in picked:
                try:
                    self._store.insert_message(standardized)
                except Exception:
                    logger.exception("Catch-up of a new friend: a message could not be stored")
        if newest is not None and self._tracked_chat(talker):
            self._note_read(talker, newest)
        logger.info("Catch-up of a new friend (%s…): %d message(s) since she was added handed on", talker[:8],
                    len(picked))
        try:
            sink(picked)
        except Exception:
            logger.exception("Catch-up of a new friend (%s…): the messages could not be handed on", talker[:8])
        return True

    def db_readable(self) -> bool:
        """Can the WeChat database be read (fewer than 3 failed reads in a row)?"""
        return self._db_failures < 3

    def wechat_window_found(self) -> Optional[bool]:
        """Is WeChat's main window there?  None while a send holds the window (asked again later)."""
        if not self._window_lock.acquire(blocking=False):
            return None
        try:
            return self._window.find_hwnd(wake=False) is not None
        except Exception:
            return False
        finally:
            self._window_lock.release()

    # ── PC WeChat offline (src/wechat/presence.py) ──────────────────────

    def wechat_presence(self) -> dict:
        """Is PC WeChat logged in now (``presence.probe``: its process and its main window)?  Only on
        Windows; elsewhere there is no answer (``online`` None) and no gap is ever seen."""
        from . import presence
        if os.name != "nt":
            return {"online": None, "process": None, "started_at": None, "detail": "不是 Windows，不看电脑微信"}
        return presence.probe(self.wechat_window_found)

    def note_wechat_offline(self, since: float) -> None:
        """PC WeChat has not been online since ``since``: from now on, rows written since then that show
        up late in a creator's chat (WeChat brings them in when it is logged in again with 「同步最近的聊天
        记录」 ticked) are read back and handed on as catch-up (``_poll_group``), not left as history."""
        with self._catch_up_lock:
            self._gap_since = float(since) if self._gap_since is None else min(self._gap_since, float(since))
            self._gap_until = None
        logger.warning("PC WeChat is not online since %s: rows it brings in later are handed on",
                       time.strftime("%m-%d %H:%M", time.localtime(since)))

    def catch_up_after_gap(self, since: float) -> None:
        """PC WeChat is online again after not being online since ``since``: like after a restart, every
        registered (PRM_COPILOT) or listed (PRM_EDGE) creator's chat is read back (CATCHUP_READ) at its
        next poll and the rows written since then that this run has not seen are handed on in order;
        for GAP_WATCH_SEC more, rows it brings in later are read back the same way.  Needs
        PRM_CATCHUP_HOURS above 0, like the restart catch-up."""
        with self._catch_up_lock:
            self._gap_since = float(since) if self._gap_since is None else min(self._gap_since, float(since))
            self._gap_until = time.time() + GAP_WATCH_SEC
            self._gap_back_at = time.time()
        logger.info("PC WeChat is online again (offline since %s): the creators' chats are read back",
                    time.strftime("%m-%d %H:%M", time.localtime(since)))

    def _gap_watch(self, now: float) -> Optional[float]:
        """Since when PC WeChat was offline, while rows it brings in late are looked for; else None."""
        with self._catch_up_lock:
            if self._gap_since is None:
                return None
            if self._gap_until is not None and now > self._gap_until:
                self._gap_since = self._gap_until = None
                self._gap_back_at = 0.0
                self._gap_read_at = {}
                self._gap_looked = {}
                return None
            return self._gap_since

    def _gap_rows_unseen(self, talker: str, messages: list, known: DedupSet, since: float, now: float) -> bool:
        """Do these rows (WCDB's, newest first) hold one this run has not seen, written since the gap
        began and read late (more than MAX_AGE_SEC old): WeChat brought it in after logging in again?
        Looked at by the row's ``local_id`` and time only (no standardizing)."""
        line = since - GAP_MARGIN_SEC
        looked = self._gap_looked.get(talker) or set()
        for msg in messages or []:
            ts = _row_time(msg)
            if ts < line or now - ts <= MAX_AGE_SEC:
                continue
            row_id = _row_id(msg, talker)
            if row_id and row_id not in known and row_id not in looked:
                return True
        return False

    def _gap_catch_up(self, group_name: str, talker: str, since: float) -> bool:
        """Read a creator's chat back (CATCHUP_READ) for a gap: the rows ``_gap_plan`` picks go to
        ``_catchup[talker]`` (handed on right after, oldest first), the rest is marked seen.  False when
        the chat could not be read (an error code): it is read back at the next poll instead, and this
        poll leaves the chat alone (its late rows are not handed on as new ones meanwhile)."""
        with self._catch_up_lock:
            previous = self._gap_read_at.get(talker)
            self._gap_read_at[talker] = time.time()
        messages = self._read_messages(talker, CATCHUP_READ)
        if messages is None:
            with self._catch_up_lock:
                if previous is None:
                    self._gap_read_at.pop(talker, None)
                else:
                    self._gap_read_at[talker] = previous
            return False
        self._prime_chat(group_name, talker, messages, catch_up=True, gap_since=since)
        # a row that is no message (standardized to nothing) never asks for another read-back
        self._gap_looked.setdefault(talker, set()).update(i for i in (_row_id(m, talker) for m in messages or []) if i)
        return True

    def _gap_plan(self, talker: str, items: list[dict], since: float) -> set[str]:
        """The ids of rows (``items``: standardized, what WCDB shows now) to hand on for a gap: written
        since it began (GAP_MARGIN_SEC before), not seen by this run (``_known_set``: what the poll saw
        it handed on already, alone or in a burst), not held in a burst, not stored (PRM_COPILOT: an
        earlier run's).  Not the restart catch-up's floors: whatever came before the gap was read while
        WeChat was online, and an id that dropped out of the seen set must not go a second time."""
        known = self._known_set(talker)
        held = self._burst.held_ids()
        try:
            stored = self._stored_of(talker, items) if self._catchup_floor is None and self._store is not None else set()
        except Exception:
            logger.exception("Gap catch-up: the message store could not be read for %s…", talker[:8])
            stored = set()
        line = since - GAP_MARGIN_SEC
        return {s["message_id"] for s in items
                if s["message_id"] not in known and s["message_id"] not in held and s["message_id"] not in stored
                and int(s.get("timestamp") or 0) >= line}

    def _tell_own_message(self, standardized: dict) -> None:
        hook = getattr(self, "_own_message_hook", None)
        if hook is None or standardized.get("is_group", True):
            return
        if not self._wants_private(str(standardized.get("chat_id", ""))):
            return
        try:
            hook(standardized)
        except Exception:
            logger.exception("The own-message hook failed; polling goes on")

    def _wants_private(self, username: str) -> bool:
        accept = self._private_filter
        if accept is None:
            return False
        try:
            return bool(accept(username))
        except Exception:
            logger.exception("The private chat filter failed for %s", username[:20])
            return False

    def _refresh_private_chats_if_due(self) -> None:
        """Discover private chats created after startup.

        Newly discovered private chats are not primed: the first visible
        message is usually the message that created the session, and should
        be eligible for an immediate reply.
        """
        if not getattr(self._config, "private_chat_enabled", False):
            return
        now = time.monotonic()
        if now - self._last_session_refresh < SESSION_REFRESH_SEC:
            return
        self._last_session_refresh = now
        self._tracked = {}                  # creators registered or removed meanwhile: asked again

        private_config = [
            g.strip()
            for g in str(getattr(self._config, "private_chats", "*")).split(",")
            if g.strip()
        ] or ["*"]
        auto_private_chats = any(
            item.strip().lower() in {"*", "all", ""}
            for item in private_config
        )

        try:
            with self._client_lock:
                sessions = self._client.get_sessions()
                self._sessions_read = (time.monotonic(), sessions)
                my_wxid = str(
                    getattr(self._client, "_config", {}).get("myWxid", "") or ""
                )
                added = 0
                seen: dict[str, str] = {}
                for s in sessions:
                    username = _session_username(s)
                    if (
                        not username
                        or username in self._chat_is_group
                        or not _is_private_talker(username)
                    ):
                        continue
                    if my_wxid and (
                        username == my_wxid
                        or username.startswith(my_wxid)
                        or my_wxid.startswith(username)
                    ):
                        continue
                    wanted = self._wants_private(username)    # a registered creator
                    if auto_private_chats and not wanted and not username.lower().startswith("wxid_"):
                        continue

                    display = self._resolve_session_display_name(s, username)
                    if not display or (
                        display == username and username.startswith("wxid_")
                    ):
                        continue
                    if not wanted and self._bot_name and (
                        display == self._bot_name
                        or self._bot_name in display
                    ):
                        continue
                    seen[username] = display
                    if not wanted and not _matches_configured_chat(
                        display, username, private_config,
                    ):
                        continue
                    if self._add_talker(display, username, is_group=False):
                        added += 1

                self._session_names.update(seen)       # read or not: a send refuses their names too
                if seen and self._private_filter is not None:
                    self._save_private_names(seen)
                if added:
                    logger.info(
                        "Discovered %d new private chats for reply",
                        added,
                    )
        except WcdbCallError as e:
            logger.warning("Failed to refresh private chat sessions: %s", e)
        except Exception:
            logger.exception("Failed to refresh private chat sessions")

    def _read_messages(self, talker: str, limit: int) -> Optional[list]:
        """WCDB's latest ``limit`` rows of a chat (newest first), read under the client lock.  An error
        code from the DLL (``WcdbCallError``) gives None and is counted for this poll cycle: a chat
        whose reads fail while the others read is that chat only (skipped, looked at again next poll;
        said once in the log); every read of a cycle failing is a failed read of the database
        (``_poll_cycle``).  A chat with nothing in it reads as [] (no failure).  Other errors raise."""
        self._reads[0] += 1
        try:
            with self._client_lock:
                messages = self._client.get_messages(talker=talker, limit=limit)
        except WcdbCallError as e:
            self._reads[1] += 1
            if talker not in self._unreadable:
                self._unreadable[talker] = str(e)
                logger.warning("WCDB could not read %s (%s): skipped until it can be; the other chats "
                               "are read", talker[:20], e)
            return None
        if self._unreadable.pop(talker, None) is not None:
            logger.info("WCDB reads %s again", talker[:20])
        return messages

    def _known_set(self, talker: str) -> DedupSet:
        """The message ids already seen in this chat (made empty on its first read)."""
        known = self._known.get(talker)
        if known is None:
            known = self._known[talker] = DedupSet(max_size=CHAT_KNOWN_SIZE)
        return known

    def _catchup_hours(self) -> int:
        """PRM_CATCHUP_HOURS (0: a creator's messages that came in while the bot was not
        reading stay history, as before)."""
        try:
            return max(0, int(getattr(self._config, "prm_catchup_hours", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _tracked_chat(self, talker: str) -> bool:
        """Is this a registered creator's private chat whose reading is kept track of, for the
        catch-up after a restart (``chat_read_marks``)?  Needs the message store; with the
        WeChat edge (PRM_EDGE) the edge's floor instead (``set_catchup_floor``: it stores none
        of these messages here).  Remembered until the next session refresh (a creator
        registered or removed is asked again then)."""
        tracked = self._tracked.get(talker)
        if tracked is None:
            edge = getattr(self._config, "prm_edge", "off") == "on"
            tracked = bool(
                (self._catchup_floor is not None if edge else self._store is not None)
                and not self._chat_is_group.get(talker, str(talker).endswith("@chatroom"))
                and self._wants_private(talker)
            )
            self._tracked[talker] = tracked
        return tracked

    def _is_own_line(self, standardized: dict, my_wxid: str) -> bool:
        """Did this account write it?  With the account's wxid known that is the whole test:
        a member whose display name is the account's must still be heard.  The name is only a
        fallback without the wxid."""
        if my_wxid:
            sender_id = standardized.get("sender_id", "")
            return sender_id == my_wxid or my_wxid.startswith(sender_id) or sender_id.startswith(my_wxid)
        sender_name = standardized.get("sender_name", "")
        return bool(self._bot_name and (self._bot_name in sender_name or sender_name in self._bot_name))

    def _prime_known_ids(self) -> None:
        """Mark currently visible WCDB messages as historical on startup (and after a
        re-initialisation).  PRM_CATCHUP_HOURS: in a registered creator's private chat the
        messages the bot never handled are kept for the chat's next poll instead (_prime_chat)."""
        primed = queued = 0
        self._catchup = {}
        catching_up = self._catchup_hours() > 0
        for talker, group_name in list(self._chat_names.items()):
            catch_up = catching_up and self._tracked_chat(talker)
            try:
                with self._client_lock:
                    messages = self._client.get_messages(talker=talker, limit=CATCHUP_READ if catch_up else 50)
            except Exception as e:
                logger.warning("Failed to prime WCDB state for %s: %s", group_name, e)
                continue
            marked, kept = self._prime_chat(group_name, talker, messages, catch_up=catch_up)
            primed += marked
            queued += kept
        logger.info("Primed WCDB state (%d historical messages skipped%s)", primed,
                    f"; {queued} message(s) of registered creators that came in while the bot was not "
                    "reading are handed on" if queued else "")

    def _prime_chat(self, group_name: str, talker: str, messages, *, catch_up: bool,
                    first_read: bool = False, gap_since: Optional[float] = None) -> tuple[int, int]:
        """Mark one chat's messages (``messages``: WCDB's list, newest first) as seen.  With
        ``catch_up`` (a registered creator's chat, PRM_CATCHUP_HOURS on) the ones
        ``_catchup_plan`` picks are kept, oldest first, for the chat's next poll instead;
        with ``gap_since`` (PC WeChat was offline since then: ``_gap_catch_up``) the ones
        ``_gap_plan`` picks.  Kept rows join those kept already, in time order.
        Returns ``(marked, kept)``."""
        is_group = self._chat_is_group.get(talker, talker.endswith("@chatroom"))
        known = self._known_set(talker)
        items = []
        for msg in reversed(messages or []):
            standardized = self._standardize(msg, group_name, talker, resolve_voice=False, is_group=is_group)
            if standardized is not None:
                items.append((msg, standardized))
        if gap_since is not None:
            picked = self._gap_plan(talker, [s for _, s in items], gap_since)
        else:
            picked = self._catchup_plan(talker, [s for _, s in items], first_read=first_read) if catch_up else set()
        kept, marked = [], 0
        now = time.time()
        watched = not is_group and self._wants_private(talker)      # a recall in place is looked for
        for msg, standardized in items:
            msg_id = standardized["message_id"]
            if watched:
                self._note_recallable(talker, standardized, now)
            if msg_id in picked:
                kept.append(msg)
            elif msg_id not in known:
                known.add(msg_id)
                marked += 1
        if kept:
            waiting = self._catchup.get(talker) or []
            have = {(str(m.get("local_id", m.get("localId", ""))), m.get("create_time", m.get("createTime")))
                    for m in waiting}
            more = [m for m in kept if (str(m.get("local_id", m.get("localId", ""))),
                                        m.get("create_time", m.get("createTime"))) not in have]
            self._catchup[talker] = sorted(waiting + more, key=_row_time)
            logger.info("Catch-up: %d message(s) in a registered creator's chat (%s…) came in while %s (the oldest "
                        "at %s); handed on in order", len(kept), talker[:8],
                        "PC WeChat was not online" if gap_since is not None else "the bot was not reading",
                        time.strftime("%m-%d %H:%M", time.localtime(min(
                            int(s.get("timestamp") or 0) for _, s in items if s["message_id"] in picked))))
        return marked, len(kept)

    def _catchup_plan(self, talker: str, items: list[dict], *, first_read: bool = False) -> set[str]:
        """The ids of a registered creator's messages (``items``: standardized, what WCDB shows
        now) that the bot never handled: they are handed on, the rest is history.

        The creator's lines: newer than the newest message stored for the chat (the route
        stores every message it handles first, step ``persist``) and not stored themselves
        (under their id, or one an older version made of the row: ``_stored_of``).
        This account's own lines (an operator typing on the phone; they are never stored):
        newer than that and than the chat's read mark (``chat_read_marks``: how far the bot
        has read the chat as a creator's).  Without a stored message the read mark is the
        line for the creator too; without a read mark this account's lines are history (the
        bot never kept track of the chat, or ran a version that did not).  Nothing to go by:
        history, as before.  The first read of a chat found after startup that the bot never
        read as a creator's (registered just now) keeps the old rule too: only the last
        MAX_AGE_SEC seconds are handed on, its older lines are history whatever was stored
        of it long ago.  A message a burst still holds (a re-initialisation) is the burst's.

        The WeChat edge (``set_catchup_floor``) stores none of a listed chat's messages here:
        every line (hers and this account's) written after the edge last handled the chat is
        handed on; nothing kept of the chat: history (the first read keeps the old rule).
        """
        if self._catchup_floor is not None:
            return self._edge_catchup_plan(talker, items, first_read=first_read)
        try:
            stored_at = self._store.last_stored_at(talker)
            mark = self._store.read_mark(talker)
            stored = self._stored_of(talker, items)
        except Exception:
            logger.exception("Catch-up: the message store could not be read for %s…; its messages are history",
                             talker[:8])
            return set()
        if first_read and mark is None:
            creator_floor = own_floor = None
        else:
            creator_floor = stored_at if stored_at is not None else mark
            own_floor = None if mark is None else max(mark, stored_at if stored_at is not None else mark)
        held = self._burst.held_ids()
        my_wxid = self.own_wxid()
        recent = time.time() - MAX_AGE_SEC
        picked = set()
        for standardized in items:
            msg_id = standardized["message_id"]
            if msg_id in stored or msg_id in held:
                continue
            ts = int(standardized.get("timestamp") or 0)
            floor = own_floor if self._is_own_line(standardized, my_wxid) else creator_floor
            if floor is not None and ts > floor:
                picked.add(msg_id)
            elif floor is None and first_read and ts >= recent:
                picked.add(msg_id)
        return picked

    def _edge_catchup_plan(self, talker: str, items: list[dict], *, first_read: bool) -> set[str]:
        """``_catchup_plan`` with the WeChat edge."""
        try:
            floor = self._catchup_floor(talker)
        except Exception:
            logger.exception("Catch-up: the edge could not tell when %s… was last handled; its messages are "
                             "history", talker[:8])
            return set()
        if floor is None and not first_read:
            return set()
        held = self._burst.held_ids()
        recent = time.time() - MAX_AGE_SEC
        return {s["message_id"] for s in items if s["message_id"] not in held
                and (int(s.get("timestamp") or 0) > floor if floor is not None
                     else int(s.get("timestamp") or 0) >= recent)}

    def _stored_of(self, talker: str, items: list[dict]) -> set[str]:
        """The ids of these messages (standardized) that the message store holds for the chat:
        under their id, or under an id a version up to 2026-10-27 made of the row
        (``_legacy_ids``; the first start after the update still finds what it handled)."""
        wanted: dict[str, str] = {}
        for standardized in items:
            for key in (standardized["message_id"], *(standardized.get("_legacy_ids") or ())):
                wanted.setdefault(key, standardized["message_id"])
        return {wanted[key] for key in self._store.stored_ids(list(wanted), chat_id=talker)}

    def _hand_on_missed(self, group_name: str, talker: str, messages: list[dict],
                        callback: MessageCallback) -> None:
        """The messages ``_prime_chat`` kept for a registered creator's chat, oldest first, one
        by one in this thread: no burst merging and no worker pool, so PRM hears them in order
        (its settle makes one draft job of them) and each is stored under its own id."""
        known = self._known_set(talker)
        my_wxid = self.own_wxid()
        handed = 0
        for msg in messages:
            if not self._running:
                break                                  # not handed: the next start finds it again
            standardized = self._standardize(msg, group_name, talker, is_group=False)
            if standardized is None or standardized["message_id"] in known:
                continue
            known.add(standardized["message_id"])
            if self._catchup_floor is not None:
                standardized["_catchup"] = True        # the edge forwards it although it is old
            if self._is_own_line(standardized, my_wxid):
                if my_wxid:
                    self._tell_own_message(standardized)
                continue
            if my_wxid:
                standardized["_not_self_by_id"] = True
            self._handle_message(group_name, talker, standardized, callback, screenshot=False)
            handed += 1
        logger.info("Catch-up: %d message(s) of a registered creator (%s…) handed on", handed, talker[:8])

    def _note_read(self, talker: str, newest: int) -> None:
        """Move the chat's read mark (``chat_read_marks``) to the newest message seen there.
        Not with the WeChat edge: its catch-up goes by the edge's own record."""
        if self._catchup_floor is not None or newest <= self._read_marks.get(talker, -1):
            return
        try:
            self._store.set_read_mark(talker, newest)
            self._read_marks[talker] = newest
        except Exception:
            logger.exception("Could not note how far %s… was read", talker[:8])

    # ── A message taken back: WeChat 4 rewrites its row in place ─────────

    def _recallable_rows(self, talker: str, now: float) -> dict[str, int]:
        """The chat's rows that may still be taken back (``_note_recallable``), the ones written
        more than RECALL_WINDOW_SEC ago dropped."""
        rows = self._recallable.get(talker)
        if not rows:
            return {}
        for msg_id in [i for i, ts in rows.items() if now - ts > RECALL_WINDOW_SEC]:
            del rows[msg_id]
        if not rows:
            del self._recallable[talker]
        return rows

    def _note_recallable(self, talker: str, standardized: dict, now: float) -> None:
        """An ordinary row (not one of WeChat's lines) of a watched private chat, written in the last
        RECALL_WINDOW_SEC: looked at again on every poll, in case WeChat rewrites it into a recall."""
        if edge_text.is_notice(standardized):
            return
        ts = int(standardized.get("timestamp") or 0)
        if now - ts > RECALL_WINDOW_SEC:
            return
        self._recallable.setdefault(talker, {})[standardized["message_id"]] = ts

    def _recall_in_place(self, talker: str, standardized: dict, now: float) -> Optional[dict]:
        """A row read before as an ordinary line that now is one of WeChat's lines: WeChat 4.1 takes a
        message back by rewriting its row (same ``local_id`` and time, so the same message id; type
        10000, ``<revokemsg><content>"小美" 撤回了一条消息</content>``; 2026-10-28 on the real
        machine), which the poll would skip as read.  Returns that line as a message of its own (once):
        its own id (made from the row's: the route's store and the edge's events take it as new),
        ``recalled_id`` = the id the row was handed on under, written now.  None otherwise."""
        rows = self._recallable.get(talker) or {}
        msg_id = standardized["message_id"]
        if msg_id not in rows or not edge_text.is_notice(standardized):
            return None
        del rows[msg_id]
        recall = dict(standardized)
        recall.update(message_id=_md5(f"recall|{talker}|{msg_id}"), recalled_id=msg_id, timestamp=int(now),
                      _legacy_ids=[])
        return recall

    def _hand_on_recall(self, recall: dict, callback: MessageCallback, my_wxid: str) -> None:
        """Hand on a line taken back (``_recall_in_place``) like any line of the chat: this account's
        to the own-message hook, hers through the route.  A burst still holding the line taken back
        goes first, so the line is never handed on after its recall."""
        for ready in self._burst.flush_holding(recall["recalled_id"]):
            self._dispatch_message(ready, callback)
        talker = str(recall.get("chat_id") or "")
        logger.info("A line in %s…'s chat was taken back (WeChat rewrote its row); handed on as WeChat's line",
                    talker[:8])
        if self._is_own_line(recall, my_wxid):
            if my_wxid:
                self._tell_own_message(recall)
            return
        if my_wxid:
            recall["_not_self_by_id"] = True
        self._dispatch_message(recall, callback)

    def _poll_group(self, group_name: str, talker: str,
                    callback: MessageCallback) -> None:
        """Fetch messages for one group and dispatch new ones.

        Route callbacks are submitted to the thread pool so a slow send in
        one chat never blocks polling of the other chats.

        A registered creator's private chat (PRM_CATCHUP_HOURS): first what the bot never
        handled (``_prime_chat`` kept it at startup; on the first read of a chat found
        later, it is picked now), then the new messages; the newest message time seen goes
        to its read mark.

        A watched private chat (the private filter's: a registered creator, a listed one): a row
        read lately that WeChat rewrote into a recall is handed on as a system line of its own
        (``_recall_in_place``).
        """
        if self._catch_up_requests:
            with self._catch_up_lock:
                request = self._catch_up_requests.pop(talker, None)
            if request is not None and not self._catch_up_new_friend(group_name, talker, request):
                return                    # the chat cannot be read now: the request waits for the next poll
        messages = self._read_messages(talker, 50)
        if messages is None:
            return                        # an error code for this chat (_read_messages): nothing seen yet
        first_read = talker not in self._known
        known = self._known_set(talker)
        tracked = self._tracked_chat(talker)
        catching_up = tracked and self._catchup_hours() > 0
        if first_read and catching_up:
            self._prime_chat(group_name, talker, messages, catch_up=True, first_read=True)
        gap_since = self._gap_watch(time.time()) if catching_up and not first_read else None
        if gap_since is not None:
            # PC WeChat was offline: read the chat back once it is online again, and whenever rows written
            # since then show up late (synced from the phone), so they go oldest first, as catch-up
            with self._catch_up_lock:
                due = self._gap_read_at.get(talker, 0.0) < self._gap_back_at
            if due or self._gap_rows_unseen(talker, messages, known, gap_since, time.time()):
                if not self._gap_catch_up(group_name, talker, gap_since):
                    return                # read back at the next poll (its late rows are not new ones)
        missed = self._catchup.pop(talker, None)
        if missed:
            self._hand_on_missed(group_name, talker, missed, callback)
        if not messages:
            return

        is_group = self._chat_is_group.get(talker, talker.endswith("@chatroom"))
        my_wxid = str(getattr(self._client, "_config", {}).get("myWxid", "") or "")
        now = time.time()
        recallable = self._recallable_rows(talker, now)
        watched = None                   # is a recall in place looked for here (asked once, on a new row)
        newest = None
        for msg in reversed(messages):
            if not self._running:
                break

            # Without the voice recognised: a row's id never depends on a voice message's words
            # (_message_ids), so the rows seen already need none.  Recognition (seconds with a local
            # whisper) runs once, for a new row below; one it fails on is not tried on every poll.
            standardized = self._standardize(msg, group_name, talker, resolve_voice=False, is_group=is_group)
            if standardized is None:
                continue
            ts = int(standardized.get("timestamp") or 0)
            newest = ts if newest is None else max(newest, ts)

            msg_id = standardized["message_id"]
            if msg_id in known:
                if recallable and msg_id in recallable:
                    recall = self._recall_in_place(talker, standardized, now)
                    if recall is not None:
                        self._hand_on_recall(recall, callback, my_wxid)
                continue
            known.add(msg_id)
            if not is_group:
                if watched is None:
                    watched = self._wants_private(talker)
                if watched:
                    self._note_recallable(talker, standardized, now)
            if self._ocr_priming and not catching_up:
                # The backlog of an OCR stretch: seen, not answered (part of it was answered
                # from the screen).  OCR never reads a private chat: a creator's is handed on.
                continue
            if standardized.get("msg_type") == 34:
                # a new voice message: now with its words (the id stays the one marked seen above)
                standardized = self._standardize(msg, group_name, talker, is_group=is_group) or standardized

            # Skip own messages.  With the account's wxid known that is the
            # whole test: a member whose display name is the account's must
            # still be heard.  The name is only a fallback without the wxid.
            if self._is_own_line(standardized, my_wxid):
                if my_wxid:
                    self._tell_own_message(standardized)
                continue
            if my_wxid:
                # Tells the router's self_message step not to judge by the name.
                standardized["_not_self_by_id"] = True

            self._trim_dedup()

            # A rapid same-sender run of text bubbles is one turn.
            for ready in self._burst.push(standardized):
                self._dispatch_message(ready, callback)
        if tracked and newest is not None:
            self._note_read(talker, newest)

    def _flush_due_bursts(self, callback: MessageCallback) -> None:
        """Dispatch message bursts that have gone quiet long enough."""
        for standardized in self._burst.flush_due():
            self._dispatch_message(standardized, callback)

    def _dispatch_message(self, standardized: dict,
                          callback: MessageCallback) -> None:
        group_name = str(
            standardized.get("group_name")
            or standardized.get("chat_name")
            or ""
        )
        talker = str(standardized.get("chat_id") or "")
        if not group_name or not talker:
            return

        if standardized.get("_burst_count", 1) > 1:
            logger.info(
                "Dispatching merged message burst: group='%s' sender='%s' count=%s",
                group_name,
                standardized.get("sender_name", "?"),
                standardized.get("_burst_count"),
            )

        # Fire-and-forget: callback (the route) + send run in
        # a thread pool worker so the poll loop continues immediately.
        if self._pool:
            self._pool.submit(
                self._handle_message,
                group_name, talker, standardized, callback,
            )
        else:
            # Fallback (pool already shut down): run inline
            self._handle_message(
                group_name, talker, standardized, callback,
            )

    def _handle_message(self, group_name: str, talker: str,
                        standardized: dict, callback: MessageCallback, *, screenshot: bool = True) -> None:
        """Execute callback and send reply (runs in thread pool worker).  ``screenshot``: a
        picture not found on disk may be taken from the chat window (not for one read late:
        the window shows the chat as it is now)."""
        if not self._running:
            return

        try:
            if screenshot and int(standardized.get("msg_type", 1) or 1) in IMAGE_MSG_TYPES:
                standardized["_capture_media_screenshot"] = (
                    lambda name=group_name, chat=talker: self._screenshot_of(name, chat)
                )
            logger.debug(
                "Handling message: msg_id=%s is_at=%s content=%.60s",
                standardized.get("message_id", "?"),
                standardized.get("is_at_mentioned"),
                mask_pii(standardized.get("content", "")),
            )
            cb_start = time.monotonic()
            reply = callback(standardized)
            logger.debug(
                "Message callback returned: reply=%s",
                repr(mask_pii(reply))[:100] if reply else "None",
            )
            cb_elapsed = time.monotonic() - cb_start
            if cb_elapsed > 0.5:
                logger.debug(
                    "Callback took %.2fs (msg_id=%s, group='%s')",
                    cb_elapsed, standardized["message_id"], group_name,
                )

            if reply:
                logger.info(
                    "Reply ready: group='%s' sender='%s' len=%d",
                    group_name, standardized["sender_name"], len(reply),
                )
                if self._send_queue is not None:
                    queued = self._send_queue.submit(
                        group_name, reply,
                        lambda part: self._send_part(group_name, talker, part),
                        send_file=lambda path: self._send_file_part(group_name, talker, path),
                        on_done=lambda sent: self._log_delivery(group_name, reply, sent),
                    )
                    if not queued:
                        self._log_delivery(group_name, reply, False)
                    return
                success = get_sender().deliver(
                    group_name, reply,
                    lambda part: self._send_part(group_name, talker, part),
                    alive=lambda: self._running,
                    send_file=lambda path: self._send_file_part(group_name, talker, path),
                )
                self._log_delivery(group_name, reply, success)
        except Exception:
                logger.exception(
                "Unhandled error in callback worker (group='%s', sender='%s')",
                    group_name, standardized.get("sender_name", "?"),
                )

    @staticmethod
    def _log_delivery(group_name: str, reply: str, success: bool) -> None:
        if success:
            logger.info(
                "Reply sent: group='%s' (%d chars)",
                group_name, len(reply),
            )
        else:
            logger.error(
                "Reply FAILED: group='%s' — check WeChat window",
                group_name,
            )

    def _send_part(self, group_name: str, talker: str, text: str) -> bool:
        """One bubble: the window lock is held per bubble, never while waiting between them.  Refused
        like ``send_text`` (a falsy ``SendRefused``, nothing typed) when the chat's name could open
        another chat (``_window_target``)."""
        name, refused = self._window_target(talker, group_name)
        if refused is not None:
            return refused
        with self._window_lock:
            return self._send_and_confirm(name, talker, text)

    def _send_file_part(self, group_name: str, talker: str, path: str) -> bool:
        """One sticker picture, under the window lock like a text bubble, and refused like one."""
        name, refused = self._window_target(talker, group_name)
        if refused is not None:
            return refused
        with self._window_lock:
            return self._window.send_file_to_chat(name, path, **self._check_kwargs(talker, name))

    def _screenshot_of(self, group_name: str, talker: str) -> str:
        """A picture message's ``_capture_media_screenshot``: the chat window's screenshot, or "" when
        the chat's name could open another chat (the picture would be someone else's)."""
        name, refused = self._window_target(talker, group_name)
        if refused is not None:
            return ""
        check = self._check_kwargs(talker, name)
        if check:                                  # SEND_VERIFY_CHAT: not another chat's picture either
            with self._window_lock:
                return self._window.capture_chat_media(name, **check)
        return self._capture_media_screenshot(name)

    def _capture_media_screenshot(self, group_name: str) -> str:
        """The relay's screenshot of a picture it cannot find on disk, serialized with
        normal WeChat navigation/sending."""
        with self._window_lock:
            return self._window.capture_chat_media(group_name)

    # ── SEND_VERIFY_CHAT: which chat did the search open? ─────────────

    def _check_kwargs(self, talker: str, name: str) -> dict:
        """``{"check": …}`` for a window operation in this chat when the open chat is checked, else {}."""
        check = self._open_chat_check(talker, name)
        return {"check": check} if check is not None else {}

    def _read_title(self, hwnd: int) -> str:
        """The chat title in WeChat's main window, read by OCR ("" when none is read)."""
        if getattr(self, "_title_reader", None) is None:
            from .ocr.reader import read_rows
            from .ocr.win_ocr import WindowsOcrEngine
            engine = WindowsOcrEngine(window=self._window)
            self._title_reader = lambda h: read_rows(engine.capture(h))["title"]
        return str(self._title_reader(hwnd) or "")

    def _open_chat_check(self, talker: str, name: str):
        """``check(hwnd)`` for the window (SEND_VERIFY_CHAT=on): the search opens its first result, which may
        be another chat (a name inside another's: 「Amy」 and 「Amy陈」 / 「Amy粉丝群」; one the bot does not
        read).  After the search the chat title is read from the screen (OCR) and compared:

        - the chat asked for: go ahead;
        - another chat the bot knows of (read, in the session list, in the console's list), or a group's
          title while a private chat was asked for: read again (VERIFY_LOOKS, the screen may lag), then a
          ``SendRefused`` (``WRONG_CHAT``): nothing is pasted;
        - nothing readable, cut off, a name it does not know: go ahead, as without the check.

        A screen that cannot be read at all (no OCR language, a window too small) is not tried again for
        VERIFY_OFF_SEC.  None (no check) when off, or with no way to read the screen (not Windows)."""
        if not getattr(self, "_verify_chat", True):
            return None
        if getattr(self, "_title_reader", None) is None and os.name != "nt":
            return None
        target = _ocr_key(name)
        others = {_ocr_key(n) for names in (self._chat_names, self._session_names, self._listed_private_names())
                  for t, n in list(names.items()) if t != talker} - {target, ""}
        is_group = self._chat_is_group.get(talker, str(talker).endswith("@chatroom"))

        def check(hwnd):
            if time.monotonic() < getattr(self, "_verify_off_until", 0.0):
                return None
            title, verdict = "", "unknown"
            for look in range(VERIFY_LOOKS):
                try:
                    title = self._read_title(hwnd)
                except Exception as e:
                    self._verify_off_until = time.monotonic() + VERIFY_OFF_SEC
                    logger.warning("SEND_VERIFY_CHAT: the chat title could not be read (%s); sends go on without "
                                   "the check for %d minutes", e, VERIFY_OFF_SEC // 60)
                    return None
                verdict = title_verdict(title, name, others, is_group)
                if verdict not in ("other", "group"):
                    break
                if look + 1 < VERIFY_LOOKS:
                    time.sleep(VERIFY_PAUSE_SEC)
            if verdict == "ok":
                logger.info("SEND_VERIFY_CHAT: the open chat is %r, as asked", name)
                return None
            if verdict == "unknown":
                logger.info("SEND_VERIFY_CHAT: the open chat could not be confirmed (read %r); sent as before",
                            title[:30])
                return None
            refused = SendRefused(WRONG_CHAT.format(name=name, title=" ".join(title.split())[:40]))
            logger.warning("Not sending to %s: %s", talker[:20], refused.reason)
            return refused
        return check

    # ── Voice recognition helpers ────────────────────────────────────

    def _get_voice(self):
        """Lazy-init the VoicePipeline (avoids import unless enabled)."""
        if self._voice is not None:
            return self._voice
        if self._voice_config is None:
            self._voice = False  # Sentinel: no config → disabled
            return False
        try:
            from src.voice import VoicePipeline
            self._voice = VoicePipeline(self._voice_config)
        except Exception:
            logger.exception("VoicePipeline init failed — voice disabled")
            self._voice = False
        return self._voice

    def _try_voice(self, msg: dict, talker: str = "") -> Optional[str]:
        """Attempt voice recognition; return text or None on failure."""
        voice = self._get_voice()
        if not voice:  # False or None → disabled
            return None
        msg_svr_id = (
            msg.get("msg_svr_id") or msg.get("msgSvrId") or
            msg.get("server_id") or msg.get("serverId") or 0
        )
        if msg_svr_id and hasattr(voice, "_cache"):
            cached = voice._cache.get(str(msg_svr_id))
            if cached is not None:
                return cached
        voice_bytes = None
        if self._client and talker:
            local_id = msg.get("local_id") or msg.get("localId") or 0
            create_time = msg.get("create_time") or msg.get("createTime") or 0
            msg_svr_id = (
                msg.get("msg_svr_id") or msg.get("msgSvrId") or
                msg.get("server_id") or msg.get("serverId") or 0
            )
            # WeChat 4.0 may write media_0.db asynchronously with a 0.5-2s lag for long audio.
            # Retry a few times with short sleeps so long voice messages aren't prematurely dropped.
            is_recent = abs(time.time() - float(create_time)) < 30 if create_time else True
            max_attempts = 4 if is_recent else 1
            for attempt in range(max_attempts):
                try:
                    voice_bytes = self._client.get_voice_data(
                        talker, local_id, create_time, msg_svr_id
                    )
                    if voice_bytes:
                        logger.info(
                            "Retrieved %d voice bytes from WCDB for %s (local_id=%s, svr_id=%s%s)",
                            len(voice_bytes), talker, local_id, msg_svr_id,
                            f", attempt {attempt + 1}" if attempt > 0 else ""
                        )
                        break
                except Exception:
                    logger.debug("Failed to retrieve voice bytes from WCDB client", exc_info=True)
                if attempt + 1 < max_attempts:
                    time.sleep(0.5)
        try:
            return voice.process(msg, voice_bytes=voice_bytes)
        except Exception:
            logger.exception("VoicePipeline.process failed")
            return None

    # ── Message standardization ──────────────────────────────────────

    def _standardize(self, msg: dict, group_name: str,
                     talker: str, resolve_voice: bool = True,
                     is_group: bool | None = None) -> Optional[dict]:
        """Convert WCDB raw message to standard format."""
        # WCDB message fields: sender_username, message_content, local_type, create_time
        if is_group is None:
            is_group = str(talker).endswith("@chatroom")
        sender = str(msg.get("sender_username", msg.get("senderUsername", msg.get("sender", ""))))
        if not sender and not is_group:
            sender = talker
        content = str(msg.get("message_content", msg.get("content", ""))).strip()
        local_type = _message_local_type(msg)
        msg_subtype = _message_subtype(msg)
        if local_type == 1:
            # WeChat 4 keeps a longer text zstd compressed and hex encoded (28b52ffd…): it is the
            # text, not a picture blob (a sent line read back must match itself, a long task too).
            unpacked = _decompress_zstd_hex(content)
            if unpacked:
                content = unpacked
        media_kind = ""
        media_path = ""
        media_base_dir = ""
        media_hint = _extract_media_hint(content)
        if (local_type == 3 or _looks_like_wechat_blob(content)) and _PICTURE_FIELD_LOGS[0] < 3:
            # What WCDB gives for a picture decides how its file is found; names only, never content.
            _PICTURE_FIELD_LOGS[0] += 1
            logger.info("Picture message: fields %s; content is %s; md5 of the picture %s",
                        sorted(msg), "a hex blob" if _looks_like_wechat_blob(content) else "text",
                        "found" if media_hint else "NOT found (files are matched by time only)")
        quote_info = (
            _extract_quote_appmsg(content, msg_subtype)
            if local_type == APPMSG_TYPE or msg_subtype == QUOTE_APPMSG_SUBTYPE
            else None
        )
        # A file someone sent: its name and size (the Claude relay forwards it to the Mac).
        file_info = (_extract_file_appmsg(content)
                     if local_type == APPMSG_TYPE and not quote_info else None) or {}
        # A link / mini program / card / location / system line: its title (the WeChat edge).
        special_info = (_special_info(content, local_type)
                        if local_type in (42, 48, 10000) or (local_type == APPMSG_TYPE and not quote_info
                                                            and not file_info) else {})

        # ── Voice recognition ──────────────────────────────────────
        # Voice messages (localType=34) have empty message_content;
        # we must recognise them BEFORE the empty-content check below.
        if quote_info:
            content = quote_info["content"]
        elif local_type == 34 and resolve_voice:
            voice_text = self._try_voice(msg, talker=talker)
            if voice_text:
                content = f"[语音] {voice_text}"
            else:
                content = "[语音]"
        elif local_type == 34:
            content = "[语音]"
        elif local_type != 1:
            content = format_nontext_content(local_type)
            if local_type in IMAGE_MSG_TYPES:
                media_kind = "emoji" if local_type == 47 else "image"
                try:
                    media_base = str(
                        getattr(self._config, "wechat_data_dir", "") or ""
                    )
                    if not media_base or not os.path.isdir(media_base):
                        media_base = str(
                            getattr(self._client, "_config", {}).get("dbPath", "")
                            or ""
                        )
                    media_base_dir = media_base
                    media_path = find_existing_media_path_from_message(
                        msg, media_base,
                    )
                except Exception:
                    logger.debug("Failed to resolve media path", exc_info=True)
        elif _looks_like_wechat_blob(content):
            content = "[图片]"
            local_type = 3
            media_kind = "image"
            try:
                media_base = str(
                    getattr(self._config, "wechat_data_dir", "") or ""
                )
                if not media_base or not os.path.isdir(media_base):
                    media_base = str(
                        getattr(self._client, "_config", {}).get("dbPath", "")
                        or ""
                    )
                media_base_dir = media_base
                media_path = find_existing_media_path_from_message(msg, media_base)
            except Exception:
                logger.debug("Failed to resolve media blob path", exc_info=True)
        elif not content:
            return None
        if edge_text.is_pat({"msg_type": local_type, "msg_subtype": msg_subtype, "special": special_info}):
            # 拍一拍 is WeChat's line, not a file: its words (src/edge/text.py is_notice)
            content = edge_text.notice_text({"msg_type": local_type, "msg_subtype": msg_subtype,
                                             "special": special_info})
        if is_group and local_type == 1 and not quote_info:
            content = _strip_wechat_group_sender_prefix(content)
            if not content:
                return None

        # ── System message handling ───────────────────────────────
        # Extract "xxx joined the group" events (recorded; groups are never answered).
        # Pattern matches:
        #   "张三"加入了群聊                    → 张三
        #   "张三"通过扫描二维码加入了群聊      → 张三
        #   "李四"邀请"张三"加入了群聊          → 张三 (last quoted name)
        # Using [^"]+ (cannot cross quote boundaries) instead of .+?
        # to avoid backtracking into the wrong quoted string.
        _JOIN_PATTERN = re.compile(r'"([^"]+)"(?:通过[^"]*)?加入了群聊')
        join_match = _JOIN_PATTERN.search(content)
        new_member_id: str = ""
        is_system_join: bool = False
        if is_group and join_match:
            new_member_id = join_match.group(1)
            is_system_join = True
            logger.info(
                "Join event detected: new_member=%s group=%s",
                new_member_id, group_name[:20],
            )

        # Filter other system messages (but NOT join events)
        if is_group and not is_system_join:
            _FILTER_KEYWORDS = (
                "修改群名", "退出了群聊",
                "撤回了一条消息", "被移除", "开启了朋友验证",
                "邀请", "移出了群聊",
            )
            if any(kw in content for kw in _FILTER_KEYWORDS):
                return None

        # Parse timestamp
        ts = msg.get("create_time", msg.get("createTime", msg.get("timestamp", 0)))
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = int(time.time())

        # Resolve sender display name
        sender_name = self._client.resolve_nickname(sender)
        if not is_group and sender_name == sender:
            sender_name = group_name

        # Fallback: if WCDB DLL can't resolve (user not in contacts),
        # try the messages table for a previously seen display name
        if sender_name == sender and self._store is not None:
            prev = self._store.get_sender_display_name(sender)
            if prev:
                sender_name = prev

        # Resolve @mentions in content
        resolved_content = content
        if is_group and "@" in content:
            def _replace_at(match):
                at_wxid = match.group(0)[1:]
                name = self._client.resolve_nickname(at_wxid)
                return f"@{name}" if name != at_wxid else match.group(0)
            resolved_content = re.sub(r'@wxid_[a-zA-Z0-9]+', _replace_at, content)

        my_wxid = str(getattr(self._client, "_config", {}).get("myWxid", "") or "")
        quoted_sender_id = quote_info.get("quoted_sender_id", "") if quote_info else ""
        quoted_sender_name = quote_info.get("quoted_sender_name", "") if quote_info else ""
        quote_name_matches_bot = bool(
            is_group and self._bot_name and quoted_sender_name and (
                quoted_sender_name == self._bot_name
                or self._bot_name in quoted_sender_name
                or quoted_sender_name in self._bot_name
            )
        )
        quote_id_matches_bot = bool(
            is_group and my_wxid and quoted_sender_id and (
                quoted_sender_id == my_wxid
                or quoted_sender_id.startswith(my_wxid)
                or my_wxid.startswith(quoted_sender_id)
            )
        )
        # With both ids known the id decides: quoting a member whose display
        # name is the bot's is not quoting the bot.
        if my_wxid and quoted_sender_id:
            is_reply_to_bot = bool(quote_info and quote_id_matches_bot)
        else:
            is_reply_to_bot = bool(quote_info and (quote_name_matches_bot or quote_id_matches_bot))

        # Detect @mention of bot
        bot_names = {self._bot_name}
        if getattr(self, "_config", None) is not None and getattr(self._config, "bot_display_name", None):
            bot_names.add(self._config.bot_display_name)
        bot_names.discard("")

        is_at = bool(
            is_reply_to_bot
            or (
                is_group and any(
                    f"@{bn}" in resolved_content or f"@{bn}" in content
                    for bn in bot_names
                )
            )
        )
        logger.debug(
            "Standardized message: content=%r resolved=%r bot=%r is_at=%s quote_bot=%s",
            mask_pii(content)[:100], mask_pii(resolved_content)[:100], self._bot_name, is_at,
            is_reply_to_bot,
        )

        # The message id stays the same for the row whatever WeChat fills in later
        # (_message_ids).  A join line keeps its content-based id.  Not the recognised words of
        # a voice message: priming reads it without recognising it, and must get the same id.
        if is_system_join:
            msg_id = _md5(f"join|{talker}|{new_member_id}|{content}|{ts}")
            older_ids: list[str] = []
        else:
            msg_id, older_ids = _message_ids(
                msg, talker, sender, "[语音]" if local_type == 34 and not quote_info else content, ts)

        return {
            "message_id": msg_id,
            "chat_id": talker,
            "group_name": group_name,
            "chat_name": group_name,
            "sender_id": str(sender),
            "sender_name": str(sender_name),
            "content": resolved_content,
            "msg_type": local_type,
            # an app message's kind from the high 32 bits of local_type (62: 拍一拍, 57: a reply), else 0
            "msg_subtype": msg_subtype,
            "server_id": str(msg.get("server_id", msg.get("serverId", "")) or ""),
            "media_base_dir": media_base_dir,
            "media_hint": media_hint or str(
                msg.get("server_id", msg.get("serverId", "")) or ""
            ),
            "media_kind": media_kind,
            "media_path": media_path,
            "file_name": file_info.get("name", ""),
            "file_size": file_info.get("size", 0),
            "timestamp": ts,
            "is_at_mentioned": is_at,
            "is_group": bool(is_group),
            "is_system_join": is_system_join,
            "new_member_id": new_member_id,
            "is_reply_to_bot": is_reply_to_bot,
            "quoted_sender_id": quoted_sender_id,
            "quoted_sender_name": quoted_sender_name,
            "quoted_content": quote_info.get("quoted_content", "") if quote_info else "",
            "quoted_server_id": quote_info.get("quoted_server_id", "") if quote_info else "",
            "special": special_info,
            # The ids versions up to 2026-10-27 made of this row (the message store may hold them).
            "_legacy_ids": older_ids,
        }

    def _trim_dedup(self) -> None:
        """DedupSet handles this internally."""
        pass

    # ── Message sending ──────────────────────────────────────────────

    def _send_and_confirm(self, group_name: str, talker: str,
                          content: str) -> bool:
        """Send via WeChatWindowController (fire-and-forget).

        Returns True if the keyboard send action completed successfully.
        No confirmation polling — the window controller already retries
        on failure, and polling WCDB adds 3s of latency for marginal gain.
        With SEND_VERIFY_CHAT a chat the search opened that is positively
        another one is not pasted into (a falsy ``SendRefused``).
        """
        return self._window.send_to_chat(group_name, content, **self._check_kwargs(talker, group_name))
