"""
WCDB Native Backend — zero external dependencies.

Reads WeChat messages directly from the encrypted WCDB database via
patched wcdb_api.dll (ctypes).  Uses WeChatWindowController for sending.

WCDB native access — reads encrypted database in-process, no external dependencies.
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

from .base import AbstractWeChatBackend, MessageCallback
from .burst import MessageBurstBuffer
from .wcdb_client import WcdbNativeClient
from .quiet_send import QuietSendSettings
from .window_controller import WeChatWindowController
from .helpers import DedupSet, format_nontext_content, normalize_msg_type
from src.persona import PERSONA_PROFILE_YANG_YUJING
from src.vision import IMAGE_MSG_TYPES, find_existing_media_path_from_message
from ..send.bubbles import get_sender
from ..send.queue import SendQueue

logger = logging.getLogger(__name__)

DEFAULT_POLL_SEC = 1.0
MAX_DEDUP_SIZE = 5000
MAX_CONSECUTIVE_ERRORS = 5   # trigger reinit after this many consecutive failures
SESSION_REFRESH_SEC = 60.0
APPMSG_TYPE = 49
QUOTE_APPMSG_SUBTYPE = 57
_HEX_BLOB_RE = re.compile(r"^[0-9a-fA-F]{80,}$")
_MEDIA_MD5_RE = re.compile(r"\bmd5=[\"']([0-9a-fA-F]{16,64})[\"']")
_SPECIAL_PRIVATE_TALKERS = {
    "filehelper", "fmessage", "medianote", "floatbottle", "qqmail",
    "tmessage", "qmessage", "qqsync", "weixin", "weixinreminder",
    "newsapp", "feedsapp", "brandsessionholder", "notification_messages",
    "officialaccounts", "notifymessage", "mphelper",
}


def _looks_like_wechat_blob(content: str) -> bool:
    """Detect raw WCDB media payloads that were misreported as text."""
    compact = re.sub(r"\s+", "", content or "")
    return bool(_HEX_BLOB_RE.fullmatch(compact))


def _extract_media_hint(content: str) -> str:
    """Extract an emoji/image cache identifier from WeChat XML content."""
    match = _MEDIA_MD5_RE.search(str(content or ""))
    return match.group(1).lower() if match else ""


def _message_local_type(msg: dict) -> int:
    raw = msg.get("local_type", msg.get("localType", msg.get("msg_type", 1)))
    try:
        value = int(raw)
        # Some WeChat app messages pack subtype in the high 32 bits, e.g.
        # quote/refer messages: (57 << 32) | 49.
        if value > 0xFFFFFFFF:
            return value & 0xFFFFFFFF
        return value
    except (TypeError, ValueError):
        return normalize_msg_type(raw)


def _message_subtype(msg: dict) -> int:
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
            "zstandard is not installed; cannot decode WeChat quote message"
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
                 config=None):
        self._bot_name = bot_display_name
        yang_target_groups = []
        for attribute in (
            "yang_yujing_group_name",
            "yang_yujing_always_reply_group_name",
        ):
            group = str(getattr(config, attribute, "")).strip()
            if group and group not in yang_target_groups:
                yang_target_groups.append(group)
        if (
            getattr(config, "chat_persona_profile", "")
            == PERSONA_PROFILE_YANG_YUJING
            and yang_target_groups
        ):
            # Yang Yujing only reads its configured full-feed groups. This
            # also avoids eagerly querying member metadata for unrelated groups.
            self._groups = yang_target_groups
        else:
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
        # SEND_QUEUE=on: one thread sends every reply, so typing pauses and the
        # wait for the user never hold a worker (src/send/queue.py).
        self._send_queue: SendQueue | None = (
            SendQueue(typing=get_sender().typing, alive=lambda: self._running)
            if getattr(config, "send_queue", "off") == "on" else None
        )
        self._talker_ids: dict[str, str] = {}
        self._chat_is_group: dict[str, bool] = {}
        self._last_session_refresh: float = 0.0
        self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
        # Thread safety: WCDB DLL (ctypes) may not be thread-safe internally.
        # All _client calls are serialized through this lock.
        self._client_lock = threading.Lock()
        # WeChat window automation (sending, screenshots) takes seconds and
        # must not interleave, but it never touches the DLL: it has its own
        # lock so a reply being typed no longer stalls message polling.
        self._window_lock = threading.Lock()
        # Callback thread pool — fire-and-forget AI calls so the poll loop
        # never blocks on a slow summarization.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        # Voice recognition pipeline (lazy-init when voice_asr_enabled)
        self._voice: Optional[object] = None
        self._config = config
        self._voice_config = config
        self._burst = MessageBurstBuffer(
            enabled=bool(getattr(config, "message_burst_enabled", True)),
            window_sec=float(getattr(config, "message_burst_window_sec", 2.2)),
            max_messages=int(getattr(config, "message_burst_max_messages", 5)),
            max_chars=int(getattr(config, "message_burst_max_chars", 200)),
        )

    # ── Public API ─────────────────────────────────────────────────

    def start(self, callback: MessageCallback) -> None:
        if not self._groups:
            logger.error("No groups configured. Set WECHAT_GROUPS in .env")
            return

        logger.info(
            "WcdbBackend starting (groups=%s, poll=%ss, bot=%r)",
            self._groups, self._poll_sec, self._bot_name,
        )

        # Init and open database
        try:
            self._client = WcdbNativeClient()
            self._client.init()
            self._client.open()
            logger.info("WCDB database opened successfully")
        except Exception as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("Failed to initialize WCDB: %s", e)
            # Push error to Web UI so the user sees a recovery path
            try:
                from src.web.server import update_status
                update_status(running=False, error=str(e))
            except Exception:
                pass
            return

        # Resolve group talker IDs
        self._resolve_groups()

        if not self._talker_ids:
            logger.error("No groups resolved. Check WECHAT_GROUPS.")
            return

        # Pre-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window pre-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found — will retry on first send")
        self._window.describe_chat_windows(
            [name for name, talker in self._talker_ids.items() if self._chat_is_group.get(talker)]
        )

        self._prime_known_ids()
        self._last_session_refresh = time.monotonic()

        self._start_workers()
        self._running = True
        consecutive_errors = 0

        # Import once to avoid per-iteration overhead
        from src.web.server import is_shutting_down as _is_shutting_down

        try:
            while self._running and not _is_shutting_down():
                try:
                    self._poll_cycle(callback)
                    consecutive_errors = 0
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    consecutive_errors += 1

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

                    wait = min(2 ** min(consecutive_errors % MAX_CONSECUTIVE_ERRORS, 5), 30)
                    logger.warning(
                        "Poll error #%d (%s): %s. Retry in %ss...",
                        consecutive_errors, type(e).__name__, e, wait,
                    )
                    time.sleep(wait)
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
        if not content:
            return False

        chat_name = self._talker_to_name(chat_id)
        if not chat_name:
            logger.error("Cannot resolve chat_id=%s to chat name", chat_id)
            return False

        with self._window_lock:
            return self._send_and_confirm(chat_name, chat_id, content)

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
            logger.error("WCDB reinitialization failed: %s", e)
            raise
        # Clear dedup set — WCDB may return messages with new IDs
        self._known_ids = DedupSet(max_size=MAX_DEDUP_SIZE)
        # Re-resolve groups (talker IDs may have changed)
        self._resolve_groups()
        self._prime_known_ids()
        # Re-find WeChat window
        hwnd = self._window.find_hwnd()
        if hwnd:
            logger.info("WeChat window re-detected: HWND=%s", hwnd)
        else:
            logger.warning("WeChat window not found after reinit")

    # ── Group resolution ────────────────────────────────────────────

    def _resolve_groups(self) -> None:
        """Map configured group names to talker IDs from WCDB sessions.

        WCDB session records only contain usernames (e.g. 20968749111@chatroom).
        Display names must be resolved via the DLL's get_display_names() or
        the local nickname cache (WeChat contacts / manual overrides).
        """
        sessions = self._client.get_sessions()
        self._talker_ids = {}
        self._chat_is_group = {}

        # Build a map of all @chatroom entries: username -> {name, member_count}
        all_chatrooms: dict[str, dict] = {}
        all_private_chats: dict[str, dict] = {}
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
                if auto_private_chats and not username.lower().startswith("wxid_"):
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
                if self._bot_name and (
                    display == self._bot_name
                    or self._bot_name in display
                ):
                    continue
                if not _matches_configured_chat(display, username, private_config):
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

        self._groups = list(self._talker_ids.keys())

        # Persist chat_id -> display_name so the web UI can show
        # human-readable group names in the nickname dropdown.
        if all_chatrooms:
            self._save_group_names(all_chatrooms)

        # ── Resolve and persist group members ──────────────────────────
        group_members: dict[str, dict[str, str]] = {}
        selected_group_ids = [
            username
            for username in self._talker_ids.values()
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
        chat_name = str(chat_name or "").strip()
        talker = str(talker or "").strip()
        if not chat_name or not talker:
            return False

        existing = self._talker_ids.get(chat_name)
        if existing and existing != talker:
            logger.warning(
                "Skipping chat '%s' (%s): display name collides with %s",
                chat_name, talker[:20], existing[:20],
            )
            return False

        self._talker_ids[chat_name] = talker
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

    def _talker_to_name(self, talker_id: str) -> str:
        for name, tid in self._talker_ids.items():
            if tid == talker_id:
                return name
        return ""

    # ── Message polling ──────────────────────────────────────────────

    def _poll_cycle(self, callback: MessageCallback) -> None:
        self._refresh_private_chats_if_due()
        for group_name in list(self._groups):
            if not self._running:
                break
            talker = self._talker_ids.get(group_name)
            if not talker:
                continue
            self._poll_group(group_name, talker, callback)
        self._flush_due_bursts(callback)
        # Check shutdown signal before sleeping so stop() is responsive
        if not self._running:
            return
        time.sleep(self._poll_sec)

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
                my_wxid = str(
                    getattr(self._client, "_config", {}).get("myWxid", "") or ""
                )
                added = 0
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
                    if auto_private_chats and not username.lower().startswith("wxid_"):
                        continue

                    display = self._resolve_session_display_name(s, username)
                    if not display or (
                        display == username and username.startswith("wxid_")
                    ):
                        continue
                    if self._bot_name and (
                        display == self._bot_name
                        or self._bot_name in display
                    ):
                        continue
                    if not _matches_configured_chat(
                        display, username, private_config,
                    ):
                        continue
                    if self._add_talker(display, username, is_group=False):
                        added += 1

                if added:
                    self._groups = list(self._talker_ids.keys())
                    logger.info(
                        "Discovered %d new private chats for reply",
                        added,
                    )
        except Exception:
            logger.exception("Failed to refresh private chat sessions")

    def _prime_known_ids(self) -> None:
        """Mark currently visible WCDB messages as historical on startup."""
        primed = 0
        for group_name, talker in list(self._talker_ids.items()):
            try:
                with self._client_lock:
                    messages = self._client.get_messages(talker=talker, limit=50)
            except Exception as e:
                logger.warning("Failed to prime WCDB state for %s: %s", group_name, e)
                continue
            for msg in messages or []:
                standardized = self._standardize(
                    msg, group_name, talker, resolve_voice=False,
                    is_group=self._chat_is_group.get(
                        talker, talker.endswith("@chatroom"),
                    ),
                )
                if standardized is None:
                    continue
                self._known_ids.add(standardized["message_id"])
                primed += 1
        logger.info("Primed WCDB state (%d historical messages skipped)", primed)

    def _poll_group(self, group_name: str, talker: str,
                    callback: MessageCallback) -> None:
        """Fetch messages for one group and dispatch new ones.

        AI-triggering callbacks are submitted to the thread pool so slow
        summarization in one group never blocks polling of other groups.
        """
        with self._client_lock:
            messages = self._client.get_messages(talker=talker, limit=50)
        if not messages:
            return

        for msg in reversed(messages):
            if not self._running:
                break

            standardized = self._standardize(
                msg,
                group_name,
                talker,
                is_group=self._chat_is_group.get(
                    talker, talker.endswith("@chatroom"),
                ),
            )
            if standardized is None:
                continue

            msg_id = standardized["message_id"]
            if msg_id in self._known_ids:
                continue
            self._known_ids.add(msg_id)

            # Skip own messages.  With the account's wxid known that is the
            # whole test: a member whose display name is the bot's (a persona
            # named after a real member, round 4b: the member himself) must
            # still be heard.  The name is only a fallback without the wxid.
            sender_id = standardized.get("sender_id", "")
            my_wxid = self._client._config.get("myWxid", "")
            if my_wxid:
                if sender_id == my_wxid or my_wxid.startswith(sender_id) or sender_id.startswith(my_wxid):
                    continue
                # Tells the router's self_message step not to judge by the name.
                standardized["_not_self_by_id"] = True
            else:
                sender_name = standardized.get("sender_name", "")
                if self._bot_name and (self._bot_name in sender_name or sender_name in self._bot_name):
                    continue

            self._trim_dedup()

            is_yang_full_feed = self._uses_yang_yujing_full_feed(standardized)
            # Yang Yujing's full-feed groups still receive every conversational
            # turn, but a rapid same-sender run is one turn rather than one AI
            # call per WeChat bubble.  Only this mode also coalesces the image
            # and sticker placeholders around that text.
            ready_messages = self._burst.push(
                standardized,
                allow_nontext=is_yang_full_feed,
            )
            for ready in ready_messages:
                self._dispatch_message(ready, callback)

    def _uses_yang_yujing_full_feed(self, standardized: dict) -> bool:
        """Whether a message uses Yang Yujing full-feed burst behavior."""
        config = self._config
        if (
            getattr(config, "chat_persona_profile", "")
            != PERSONA_PROFILE_YANG_YUJING
        ):
            return False
        target_groups = {
            str(getattr(config, attribute, "")).strip()
            for attribute in (
                "yang_yujing_group_name",
                "yang_yujing_always_reply_group_name",
            )
        }
        target_groups.discard("")
        if not target_groups:
            return False
        return bool(target_groups & {
            str(standardized.get("group_name", "")).strip(),
            str(standardized.get("chat_id", "")).strip(),
        })

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

        # Fire-and-forget: callback (potentially AI call) + send run in
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
                        standardized: dict, callback: MessageCallback) -> None:
        """Execute callback and send reply (runs in thread pool worker)."""
        if not self._running:
            return

        try:
            if int(standardized.get("msg_type", 1) or 1) in IMAGE_MSG_TYPES:
                standardized["_capture_media_screenshot"] = (
                    lambda name=group_name: self._capture_media_screenshot(name)
                )
            logger.debug(
                "Handling message: msg_id=%s is_at=%s content=%.60s",
                standardized.get("message_id", "?"),
                standardized.get("is_at_mentioned"),
                standardized.get("content", ""),
            )
            cb_start = time.monotonic()
            reply = callback(standardized)
            logger.debug(
                "Message callback returned: reply=%s",
                repr(reply)[:100] if reply else "None",
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
                        send_file=lambda path: self._send_file_part(group_name, path),
                        on_done=lambda sent: self._log_delivery(group_name, reply, sent),
                    )
                    if not queued:
                        self._log_delivery(group_name, reply, False)
                    return
                success = get_sender().deliver(
                    group_name, reply,
                    lambda part: self._send_part(group_name, talker, part),
                    alive=lambda: self._running,
                    send_file=lambda path: self._send_file_part(group_name, path),
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
        """One bubble: the window lock is held per bubble, never while waiting between them."""
        with self._window_lock:
            return self._send_and_confirm(group_name, talker, text)

    def _send_file_part(self, group_name: str, path: str) -> bool:
        """One sticker picture, under the window lock like a text bubble."""
        with self._window_lock:
            return self._window.send_file_to_chat(group_name, path)

    def _capture_media_screenshot(self, group_name: str) -> str:
        """Serialize vision capture with normal WeChat navigation/sending."""
        with self._window_lock:
            return self._window.capture_chat_media(group_name)

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

    def _try_voice(self, msg: dict) -> Optional[str]:
        """Attempt voice recognition; return text or None on failure."""
        voice = self._get_voice()
        if not voice:  # False or None → disabled
            return None
        try:
            return voice.process(msg)
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
        media_kind = ""
        media_path = ""
        media_base_dir = ""
        media_hint = _extract_media_hint(content)
        quote_info = (
            _extract_quote_appmsg(content, msg_subtype)
            if local_type == APPMSG_TYPE or msg_subtype == QUOTE_APPMSG_SUBTYPE
            else None
        )

        # ── Voice recognition ──────────────────────────────────────
        # Voice messages (localType=34) have empty message_content;
        # we must recognise them BEFORE the empty-content check below.
        if quote_info:
            content = quote_info["content"]
        elif local_type == 34 and resolve_voice:
            voice_text = self._try_voice(msg)
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
        if is_group and local_type == 1 and not quote_info:
            content = _strip_wechat_group_sender_prefix(content)
            if not content:
                return None

        # ── System message handling ───────────────────────────────
        # Extract "xxx joined the group" events → welcome feature.
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
        bot_names = {self._bot_name, "假企鹅", "qier"}
        if hasattr(self, "_config") and self._config:
            if getattr(self._config, "bot_display_name", None):
                bot_names.add(self._config.bot_display_name)
            if getattr(self._config, "chat_persona_name", None):
                bot_names.add(self._config.chat_persona_name)
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
            content[:100], resolved_content[:100], self._bot_name, is_at,
            is_reply_to_bot,
        )

        # Generate stable message ID.
        # For system join messages, use a content-based ID to avoid
        # volatile WCDB server_id/local_id causing dedup misses.
        if is_system_join:
            raw_id = f"join|{talker}|{new_member_id}|{content}|{ts}"
        else:
            raw_id = (
                str(msg.get("server_id", ""))
                or str(msg.get("local_id", ""))
                or f"{sender}|{content}|{ts}"
            )
            # Always append timestamp to avoid dedup collisions for repeated text
            raw_id = f"{raw_id}|{ts}"
        msg_id = hashlib.md5(str(raw_id).encode()).hexdigest()

        return {
            "message_id": msg_id,
            "chat_id": talker,
            "group_name": group_name,
            "chat_name": group_name,
            "sender_id": str(sender),
            "sender_name": str(sender_name),
            "content": resolved_content,
            "msg_type": local_type,
            "server_id": str(msg.get("server_id", msg.get("serverId", "")) or ""),
            "media_base_dir": media_base_dir,
            "media_hint": media_hint or str(
                msg.get("server_id", msg.get("serverId", "")) or ""
            ),
            "media_kind": media_kind,
            "media_path": media_path,
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
        """
        return self._window.send_to_chat(group_name, content)
