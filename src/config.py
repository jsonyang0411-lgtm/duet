"""Configuration loading from .env file."""

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from dotenv import dotenv_values, load_dotenv


def _decode_wechat_groups(raw: str) -> str:
    """Decode URL-encoded group names from .env WECHAT_GROUPS value.

    We store each group name URL-encoded (via encodeURIComponent / urllib.parse.quote)
    so that commas, equals signs, and newlines in real group names don't break the
    .env format or our comma-separated delimiter.  This function reverses that encoding
    with a fallback: if decoding a chunk doesn't change it (or raises), the original
    is kept — for backward compatibility with old unencoded .env files.
    """
    if not raw or raw.strip() == "*":
        return raw.strip() if raw else "*"
    decoded = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            d = unquote(chunk)
            decoded.append(d)
        except Exception:
            decoded.append(chunk)
    return ",".join(decoded) if decoded else "*"


def _sanitize_display_name(name: str) -> str:
    """Remove only truly dangerous characters from a display name.

    This preserves the user's actual name (including quotes, braces, etc.)
    and relies on *usage-point escaping* (``repr()`` in logs, ``_esc()``
    in ``str.format()`` calls) to prevent injection at each call site.

    Only stripped:
    - Control characters (CR/LF → log-line injection)
    - Leading/trailing whitespace + quotes (almost certainly accidental)
    - Excessive length (> 128 chars)
    """
    if not name:
        return "群聊小助手"

    # 1. Strip control chars (except space) — prevents log-line injection
    name = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", name)

    # 2. Collapse whitespace and strip
    name = re.sub(r"\s+", " ", name).strip()

    # 3. Truncate to reasonable length
    if len(name) > 128:
        name = name[:128]

    # 4. Fallback
    if not name:
        return "群聊小助手"

    return name


def _resolve_project_root() -> Path:
    app_home = os.getenv("WEBOT_APP_HOME", "").strip()
    if app_home:
        return Path(app_home).expanduser().resolve()

    # In a PyInstaller EXE, __file__ resolves inside the temp extraction dir.
    # We want writable data (.env, data/*) in the EXE directory, not in the temp dir.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _resolve_project_root()


def find_env_file() -> Path | None:
    """Find the .env file using a consistent search order.

    Order: WEBOT_ENV_FILE override → EXE directory (frozen) → project root → current working directory.
    Returns the Path if found, or None if no .env exists anywhere.
    """
    explicit_env = os.getenv("WEBOT_ENV_FILE", "").strip()
    if explicit_env:
        explicit_path = Path(explicit_env).expanduser()
        if explicit_path.exists():
            return explicit_path

    locations = [
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
    ]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        locations.insert(0, exe_dir / ".env")

    for loc in locations:
        if loc.exists():
            return loc
    return None


def find_local_env_file() -> Path | None:
    """Find an optional machine-local env override.

    This is useful when the project folder is shared into a VM: common bot
    settings can live in the shared .env, while machine-specific values such
    as WECHAT_DATA_DIR and WCDB_KEY live outside the shared folder.
    """
    explicit_env = os.getenv("WEBOT_LOCAL_ENV_FILE", "").strip()
    if explicit_env:
        explicit_path = Path(explicit_env).expanduser()
        if explicit_path.exists():
            return explicit_path

    locations: list[Path] = []
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        locations.append(Path(appdata) / "webot" / ".env.local")
    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if local_appdata:
        locations.append(Path(local_appdata) / "webot" / ".env.local")

    try:
        home_local_env = Path.home() / ".webot" / ".env.local"
    except RuntimeError:
        # Minimal/test processes can intentionally clear home-directory
        # variables. Project-local overrides remain available in that case.
        home_local_env = None

    if home_local_env is not None:
        locations.append(home_local_env)
    locations.extend([
        PROJECT_ROOT / ".env.local",
        Path.cwd() / ".env.local",
    ])

    for loc in locations:
        if loc.exists():
            return loc
    return None


_env_path = find_env_file()
_local_env_path = find_local_env_file()

if _env_path:
    load_dotenv(_env_path)
else:
    load_dotenv()

def _load_non_empty_local_env(path: Path) -> None:
    """Load machine-local overrides without letting blank values erase .env."""
    for key, value in dotenv_values(path).items():
        if key and value is not None and str(value).strip():
            os.environ[key] = str(value)


if _local_env_path:
    _load_non_empty_local_env(_local_env_path)

# Log which .env was loaded (helpful for debugging EXE packaging issues)
import logging as _logging
_log = _logging.getLogger(__name__)
if _env_path:
    _log.info("Loaded .env from: %s", _env_path)
else:
    _search_locations = [
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
    ]
    if getattr(sys, "frozen", False):
        _search_locations.insert(0, Path(sys.executable).resolve().parent / ".env")
    _log.warning(
        ".env not found in any search path (%s). Using defaults.",
        ", ".join(str(p) for p in _search_locations),
    )
if _local_env_path:
    _log.info("Loaded local env override from: %s", _local_env_path)


@dataclass
class BotConfig:
    """All configuration of the business WeChat gateway (there is no chat model)."""

    # === WeChat Backend ===
    wechat_backend: str = "wcdb"
    # Comma-separated group names to read. "*" = auto-discover all groups.  Group
    # messages are stored, never answered (src/routing/steps.py).
    wechat_groups: str = "*"
    # Read one-on-one WeChat chats (the relay's cc tasks, the creators of the PRM
    # copilot / the edge, the admin's commands).
    private_chat_enabled: bool = False
    # Comma-separated private chat display names/user IDs. "*" = auto-discover.
    private_chats: str = "*"
    # Custom WeChat data directory. Leave empty to auto-detect from Documents.
    # Set to the parent directory containing wxid_* folders (e.g. D:\WeChatData).
    wechat_data_dir: str = ""

    # === Account Identity ===
    # This account's WeChat display name (its own lines are skipped).
    bot_display_name: str = "群聊小助手"
    # Admin wxid (account commands in a private chat; the relay's default user)
    admin_wxid: str = ""

    # === Database ===
    db_path: str = "data/messages.db"

    # === Message Burst Coalescing ===
    # WeChat users often split one thought across multiple short bubbles.
    # Hold same-sender messages briefly so they are handled as one turn.
    message_burst_enabled: bool = True
    message_burst_window_sec: float = 2.2
    message_burst_max_messages: int = 5
    message_burst_max_chars: int = 200

    # === Pictures (src/wechat/media.py) ===
    # How far back a picture the relay forwards is looked for, and the largest one.
    image_vision_lookback_sec: int = 900
    image_vision_max_bytes: int = 5 * 1024 * 1024

    # === Tuning ===
    poll_interval_sec: float = 1.0
    dedup_window_sec: int = 60

    # === Logging ===
    log_level: str = "INFO"
    log_file: str = "data/bot.log"

    # === Voice Recognition ===
    # Master switch (default off — user must opt in)
    voice_asr_enabled: bool = False
    # "local_whisper" (free, offline) | "openai_whisper" (cloud, $0.006/min)
    voice_asr_backend: str = "local_whisper"
    voice_asr_language: str = "zh"
    # OpenAI Whisper API
    voice_openai_api_key: str = ""
    voice_openai_base_url: str = ""
    # Local Whisper model size: tiny / base / small / medium
    voice_local_model: str = "small"
    # Convert traditional Chinese → simplified (opencc t2s)
    voice_asr_to_simplified: bool = True

    # === Observability (observe only — never changes bot behaviour) ===
    # One JSON line per handled message: why the bot replied / stayed silent.
    decision_trace_enabled: bool = True
    decision_trace_path: str = "data/decision_trace.jsonl"
    # Characters of message text kept in each trace record (0 = none).
    decision_trace_content_chars: int = 120

    # === Sending ===
    # ``send_typing_delay`` on: each message waits a few seconds like a person typing.
    send_typing_delay: str = "off"
    # Quiet sending (Windows, src/wechat/quiet_send.py): wait until keyboard and
    # mouse were idle for ``send_idle_sec`` (at most ``send_idle_max_wait_sec``),
    # type into a chat's own window when there is one, then give back the focus
    # and, with ``send_keep_clipboard`` on, the clipboard.
    send_quiet: str = "off"
    send_idle_sec: float = 3.0
    send_idle_max_wait_sec: float = 30.0
    send_keep_clipboard: str = "on"
    # How a picture or file is pasted (Windows): "file" pastes the file (a GIF stays
    # animated), "image" a still picture (for a WeChat that sends pasted files as files).
    sticker_paste: str = "file"
    # One thread sends every message (WCDB backend): a typing pause or the wait for
    # the user to pause (SEND_QUIET) no longer holds a worker thread.
    send_queue: str = "off"

    # === Claude relay (src/relay) ===
    # "on": private messages from claude_relay_wxids (empty = ADMIN_WXID) that
    # start with claude_relay_prefix become jobs for Claude Code on a Mac
    # (tools/claude_relay/worker.py asks for them on host:port with the token);
    # the result comes back as a message.  Needs PRIVATE_CHAT_ENABLED=true.
    claude_relay: str = "off"
    claude_relay_token: str = ""
    claude_relay_host: str = "0.0.0.0"
    claude_relay_port: int = 8790
    claude_relay_prefix: str = "cc"
    claude_relay_wxids: list[str] = field(default_factory=list)
    claude_relay_db: str = "data/claude_relay.db"
    claude_relay_max_chars: int = 1500     # longer results are cut (the Mac keeps the full text)
    # Minutes between progress messages for a running job; a job that waited in the
    # queue also gets "started".  0 = only the receipt and the result.
    claude_relay_progress_min: int = 3
    # "on": a result longer than claude_relay_max_chars goes as a .txt file (backends
    # that can send files: Windows wcdb), with its start in a message; else it is cut.
    claude_relay_long_file: str = "on"
    # "on": every private message of an allowed person is a relay task, "cc" may be
    # left out (the admin's account commands stay commands).  Needs CLAUDE_RELAY=on.
    claude_relay_only: str = "off"

    # === PRM copilot (src/prm) ===
    # "on": private messages of the creators registered in the console become silent
    # "prm" jobs of the relay; the Mac worker drafts a reply in its locked-down
    # workspace and the draft waits in the console until an operator sends, edits
    # or drops it.  The creator gets no receipt.  Needs CLAUDE_RELAY=on and
    # PRIVATE_CHAT_ENABLED=true.
    prm_copilot: str = "off"
    prm_db: str = "data/prm.db"
    # An operator's own WeChat reply in a creator's chat (or the console) gives the chat to
    # a person for this long: the creator's messages are only recorded meanwhile.
    prm_takeover_min: int = 120
    # A draft waiting this long gets one holding reply per round of the creator's
    # messages (8.1); 0 = never.
    prm_holding_min: int = 10
    prm_holding_text: str = "收到，我确认一下马上回你"
    # A delivered deal's receiving details (address, phone and ID numbers) are cleared
    # everywhere this many days after delivery (8.2).
    prm_retention_days: int = 90
    # When a sample order's products get their 专属佣金 drafted: "delivered" (the creator signed
    # for the sample: the deal is marked delivered) or "created" (as soon as the order is made).
    prm_commission_after: str = "delivered"
    # A creator's message read more than 5 minutes late (it came in while the bot was not
    # reading WeChat: stopped, updated, the database re-opened) is still drafted up to this many
    # hours old, its draft always reviewed; an older one is only recorded and the deal asks a
    # person to look.  After a restart the Windows backend hands such messages on
    # (wcdb_backend._catchup_plan).  0 = the old rule: older than 5 minutes is history.
    prm_catchup_hours: int = 12
    # === WeChat edge (src/edge, edge-protocol.md) for the Mac business service ===
    # "on": Windows only receives and sends WeChat messages for the creators the Mac lists;
    # the Mac pulls /edge/* on the relay's port with PRM_EDGE_TOKEN.  Not with PRM_COPILOT.
    prm_edge: str = "off"
    prm_edge_token: str = ""
    edge_db: str = "data/edge.db"
    edge_send_min_gap_sec: int = 3
    edge_send_per_hour: int = 60
    edge_seen_text_sec: int = 1800
    edge_seen_file_sec: int = 600
    edge_keep_days: int = 7
    # Mac 端（WECHAT_BACKEND=edge）：不连本机微信，通过 Windows 的微信边缘收发（D18）。
    # EDGE_URL 是 Windows 边缘的地址，如 http://192.168.1.20:8790；口令同样用 PRM_EDGE_TOKEN
    edge_url: str = ""

    # === OCR fallback (src/wechat/ocr), macOS mac_hybrid backend only ===
    # "on": when the WeChat database cannot be read ocr_fallback_after times in a row, the
    # open chat is read from the screen (Apple Vision) every ocr_poll_sec seconds until the
    # database works again.  Groups only; the speaker of a line is not known.
    ocr_fallback: str = "off"
    ocr_fallback_after: int = 3
    ocr_poll_sec: float = 4.0

    # === Friend management ===
    # AUTO_ACCEPT_FRIENDS=on: every auto_accept_interval_sec seconds the bot opens "新的朋友" in
    # the WeChat window and accepts the pending requests (Windows wcdb backend only).  It takes
    # the screen, so it is off unless asked for; the admin commands 添加好友 / 同意好友 work either way.
    auto_accept_friends: str = "off"
    auto_accept_interval_sec: int = 300
    # It must not get in the way of the person at the computer: every interval it first looks, silently,
    # for a new red badge in WeChat's rail (auto_accept_watch) and does nothing if there is none; a check that
    # needs the screen waits until keyboard and mouse have been still for auto_accept_idle_sec (0 = do not wait);
    # a check is forced every auto_accept_full_check_sec even without a new badge (0 = never).
    auto_accept_idle_sec: int = 60
    auto_accept_watch: str = "on"
    auto_accept_full_check_sec: int = 900
    # At most this many requests accepted automatically a day (WeChat holds back accounts that take on
    # friends in bulk); a risk prompt (「操作过于频繁」) pauses it until the admin switches it on again.
    auto_accept_daily_max: int = 50


def _validate_config(kwargs: dict) -> None:
    """Validate numeric config values.  Prints clear errors and exits on bad values."""
    errors: list[str] = []

    # poll_interval_sec
    poll_interval_sec = kwargs.get("poll_interval_sec", 1.0)
    if poll_interval_sec < 0.1:
        errors.append(
            f"POLL_INTERVAL_SEC must be >= 0.1, got {poll_interval_sec}"
        )

    # dedup_window_sec
    dedup_window_sec = kwargs.get("dedup_window_sec", 60)
    if dedup_window_sec < 10:
        errors.append(
            f"DEDUP_WINDOW_SEC must be >= 10, got {dedup_window_sec}"
        )

    message_burst_window_sec = kwargs.get("message_burst_window_sec", 2.2)
    if not (0.2 <= message_burst_window_sec <= 8.0):
        errors.append(
            "MESSAGE_BURST_WINDOW_SEC must be between 0.2 and 8.0, "
            f"got {message_burst_window_sec}"
        )

    message_burst_max_messages = kwargs.get("message_burst_max_messages", 5)
    if not (1 <= message_burst_max_messages <= 10):
        errors.append(
            "MESSAGE_BURST_MAX_MESSAGES must be between 1 and 10, "
            f"got {message_burst_max_messages}"
        )

    message_burst_max_chars = kwargs.get("message_burst_max_chars", 200)
    if not (20 <= message_burst_max_chars <= 1000):
        errors.append(
            "MESSAGE_BURST_MAX_CHARS must be between 20 and 1000, "
            f"got {message_burst_max_chars}"
        )

    image_vision_lookback_sec = kwargs.get("image_vision_lookback_sec", 900)
    if not (60 <= image_vision_lookback_sec <= 7200):
        errors.append(
            "IMAGE_VISION_LOOKBACK_SEC must be between 60 and 7200, "
            f"got {image_vision_lookback_sec}"
        )

    image_vision_max_bytes = kwargs.get("image_vision_max_bytes", 5 * 1024 * 1024)
    if not (1024 <= image_vision_max_bytes <= 20 * 1024 * 1024):
        errors.append(
            "IMAGE_VISION_MAX_BYTES must be between 1024 and 20971520, "
            f"got {image_vision_max_bytes}"
        )

    decision_trace_content_chars = kwargs.get("decision_trace_content_chars", 120)
    if not (0 <= decision_trace_content_chars <= 1000):
        errors.append(
            "DECISION_TRACE_CONTENT_CHARS must be between 0 and 1000, "
            f"got {decision_trace_content_chars}"
        )

    for send_setting in ("send_typing_delay", "send_quiet", "send_keep_clipboard", "send_queue"):
        if kwargs.get(send_setting, "off") not in ("off", "on"):
            errors.append(f"{send_setting.upper()} must be off or on, got {kwargs.get(send_setting)}")
    send_idle_sec = kwargs.get("send_idle_sec", 3.0)
    if not (0 <= send_idle_sec <= 60):
        errors.append(f"SEND_IDLE_SEC must be between 0 and 60, got {send_idle_sec}")
    if kwargs.get("sticker_paste", "file") not in ("file", "image"):
        errors.append(f"STICKER_PASTE must be file or image, got {kwargs.get('sticker_paste')}")
    send_idle_max_wait_sec = kwargs.get("send_idle_max_wait_sec", 30.0)
    if not (0 <= send_idle_max_wait_sec <= 600):
        errors.append(
            f"SEND_IDLE_MAX_WAIT_SEC must be between 0 and 600, got {send_idle_max_wait_sec}"
        )

    claude_relay = kwargs.get("claude_relay", "off")
    if claude_relay not in ("off", "on"):
        errors.append(f"CLAUDE_RELAY must be off or on, got {claude_relay}")
    elif claude_relay == "on":
        # Anyone who can reach the port and knows the token can make the Mac run things.
        if len(str(kwargs.get("claude_relay_token", ""))) < 16:
            errors.append("CLAUDE_RELAY=on needs CLAUDE_RELAY_TOKEN (at least 16 characters, random)")
        # Mac 端（WECHAT_BACKEND=edge）的中转只给本机起草 worker 用，微信里没人能发 cc
        if not (kwargs.get("claude_relay_wxids") or kwargs.get("admin_wxid")) and kwargs.get("wechat_backend") != "edge":
            errors.append("CLAUDE_RELAY=on needs CLAUDE_RELAY_WXIDS or ADMIN_WXID (who may use it)")
        if not re.fullmatch(r"[^\s]{1,10}", str(kwargs.get("claude_relay_prefix", ""))):
            errors.append("CLAUDE_RELAY_PREFIX must be 1-10 characters without spaces")
    if not (1 <= int(kwargs.get("claude_relay_port", 8790)) <= 65535):
        errors.append(f"CLAUDE_RELAY_PORT must be between 1 and 65535, got {kwargs.get('claude_relay_port')}")
    ocr_fallback = kwargs.get("ocr_fallback", "off")
    if ocr_fallback not in ("off", "on"):
        errors.append(f"OCR_FALLBACK must be off or on, got {ocr_fallback}")
    if not (1 <= int(kwargs.get("ocr_fallback_after", 3)) <= 20):
        errors.append(f"OCR_FALLBACK_AFTER must be between 1 and 20, got {kwargs.get('ocr_fallback_after')}")
    if not (1 <= float(kwargs.get("ocr_poll_sec", 4.0)) <= 60):
        errors.append(f"OCR_POLL_SEC must be between 1 and 60, got {kwargs.get('ocr_poll_sec')}")
    claude_relay_only = kwargs.get("claude_relay_only", "off")
    if claude_relay_only not in ("off", "on"):
        errors.append(f"CLAUDE_RELAY_ONLY must be off or on, got {claude_relay_only}")
    elif claude_relay_only == "on" and kwargs.get("claude_relay", "off") != "on":
        errors.append("CLAUDE_RELAY_ONLY=on needs CLAUDE_RELAY=on (it only lets cc be left out)")
    if kwargs.get("claude_relay_long_file", "on") not in ("off", "on"):
        errors.append(f"CLAUDE_RELAY_LONG_FILE must be off or on, got {kwargs.get('claude_relay_long_file')}")
    if not (0 <= int(kwargs.get("claude_relay_progress_min", 3)) <= 120):
        errors.append(
            f"CLAUDE_RELAY_PROGRESS_MIN must be between 0 and 120, got {kwargs.get('claude_relay_progress_min')}"
        )
    if not (20 <= int(kwargs.get("claude_relay_max_chars", 1500)) <= 5000):
        errors.append(
            f"CLAUDE_RELAY_MAX_CHARS must be between 20 and 5000, got {kwargs.get('claude_relay_max_chars')}"
        )
    prm_copilot = kwargs.get("prm_copilot", "off")
    if prm_copilot not in ("off", "on"):
        errors.append(f"PRM_COPILOT must be off or on, got {prm_copilot}")
    elif prm_copilot == "on":
        if kwargs.get("claude_relay", "off") != "on":
            errors.append("PRM_COPILOT=on needs CLAUDE_RELAY=on (the drafts are written on the Mac)")
        # Only when the caller knows it (the console's advanced page checks its own keys only).
        if "private_chat_enabled" in kwargs and not kwargs["private_chat_enabled"]:
            errors.append("PRM_COPILOT=on needs PRIVATE_CHAT_ENABLED=true (creators write in private chats)")
    if not (5 <= int(kwargs.get("prm_takeover_min", 120)) <= 1440):
        errors.append(f"PRM_TAKEOVER_MIN must be between 5 and 1440, got {kwargs.get('prm_takeover_min')}")
    if not (0 <= int(kwargs.get("prm_holding_min", 10)) <= 120):
        errors.append(f"PRM_HOLDING_MIN must be between 0 and 120, got {kwargs.get('prm_holding_min')}")
    if not (1 <= len(str(kwargs.get("prm_holding_text", "收到，我确认一下马上回你")).strip()) <= 60):
        errors.append("PRM_HOLDING_TEXT must be 1-60 characters")
    if not (1 <= int(kwargs.get("prm_retention_days", 90)) <= 365):
        errors.append(f"PRM_RETENTION_DAYS must be between 1 and 365, got {kwargs.get('prm_retention_days')}")
    if kwargs.get("prm_commission_after", "delivered") not in ("delivered", "created"):
        errors.append(f"PRM_COMMISSION_AFTER must be delivered or created, got {kwargs.get('prm_commission_after')}")
    if not (0 <= int(kwargs.get("prm_catchup_hours", 12)) <= 72):
        errors.append(f"PRM_CATCHUP_HOURS must be between 0 and 72, got {kwargs.get('prm_catchup_hours')}")
    prm_edge = kwargs.get("prm_edge", "off")
    if prm_edge not in ("off", "on"):
        errors.append(f"PRM_EDGE must be off or on, got {prm_edge}")
    elif prm_edge == "on":
        if kwargs.get("claude_relay", "off") != "on":
            errors.append("PRM_EDGE=on needs CLAUDE_RELAY=on (/edge/* is served on the relay's port)")
        if "private_chat_enabled" in kwargs and not kwargs["private_chat_enabled"]:
            errors.append("PRM_EDGE=on needs PRIVATE_CHAT_ENABLED=true (creators write in private chats)")
        if prm_copilot == "on":
            errors.append("PRM_EDGE and PRM_COPILOT cannot both be on (the edge hands everything to the Mac)")
        edge_token = str(kwargs.get("prm_edge_token", "") or "")
        if len(edge_token) < 32:
            errors.append("PRM_EDGE=on needs PRM_EDGE_TOKEN of at least 32 characters")
        elif edge_token == str(kwargs.get("claude_relay_token", "") or ""):
            errors.append("PRM_EDGE_TOKEN must differ from CLAUDE_RELAY_TOKEN")
    if kwargs.get("wechat_backend") == "edge":
        # Mac 端：去连 Windows 的边缘，自己不是边缘
        edge_url = str(kwargs.get("edge_url", "") or "")
        if not edge_url.startswith(("http://", "https://")):
            errors.append("WECHAT_BACKEND=edge needs EDGE_URL, e.g. http://<Windows 的局域网 IP>:8790")
        if len(str(kwargs.get("prm_edge_token", "") or "")) < 32:
            errors.append("WECHAT_BACKEND=edge needs PRM_EDGE_TOKEN (the same token as on Windows, 32+ characters)")
        if prm_edge == "on":
            errors.append("WECHAT_BACKEND=edge is the Mac side; PRM_EDGE=on is the Windows side - not both")
        if claude_relay == "on" and str(kwargs.get("claude_relay_host", "0.0.0.0")).strip() not in (
                "127.0.0.1", "localhost", "::1"):
            # 起草 worker 在本机，中转不给局域网：别人连上来就能让这台 Mac 跑 Claude
            errors.append("WECHAT_BACKEND=edge with CLAUDE_RELAY=on needs CLAUDE_RELAY_HOST=127.0.0.1 "
                          "(the drafting worker runs on this Mac)")
    for key, low, high, default in (("edge_send_min_gap_sec", 0, 60, 3), ("edge_send_per_hour", 1, 1000, 60),
                                    ("edge_seen_text_sec", 60, 86400, 1800), ("edge_seen_file_sec", 60, 86400, 600),
                                    ("edge_keep_days", 1, 30, 7)):
        if not (low <= int(kwargs.get(key, default)) <= high):
            errors.append(f"{key.upper()} must be between {low} and {high}, got {kwargs.get(key)}")
    auto_accept = kwargs.get("auto_accept_friends", "off")
    if auto_accept not in ("off", "on"):
        errors.append(f"AUTO_ACCEPT_FRIENDS must be off or on, got {auto_accept}")
    if not (10 <= int(kwargs.get("auto_accept_interval_sec", 300)) <= 86400):
        errors.append(
            f"AUTO_ACCEPT_INTERVAL_SEC must be between 10 and 86400, got {kwargs.get('auto_accept_interval_sec')}"
        )
    if not (0 <= int(kwargs.get("auto_accept_idle_sec", 60)) <= 3600):
        errors.append(f"AUTO_ACCEPT_IDLE_SEC must be between 0 and 3600, got {kwargs.get('auto_accept_idle_sec')}")
    auto_watch = kwargs.get("auto_accept_watch", "on")
    if auto_watch not in ("off", "on"):
        errors.append(f"AUTO_ACCEPT_WATCH must be off or on, got {auto_watch}")
    full_check = int(kwargs.get("auto_accept_full_check_sec", 900))
    if full_check != 0 and not (60 <= full_check <= 86400):
        errors.append(f"AUTO_ACCEPT_FULL_CHECK_SEC must be 0 or between 60 and 86400, got {full_check}")
    if not (1 <= int(kwargs.get("auto_accept_daily_max", 50)) <= 300):
        errors.append(f"AUTO_ACCEPT_DAILY_MAX must be between 1 and 300, got {kwargs.get('auto_accept_daily_max')}")

    if errors:
        msg = "配置值无效:\n" + "\n".join(f"  - {err}" for err in errors)
        raise RuntimeError(msg)


def _parse_switch(raw: str) -> str:
    """An off/on switch; accepts the usual yes/no spellings (anything else is left for validation)."""
    value = str(raw or "").strip().lower()
    if value in ("on", "true", "1", "yes", "y"):
        return "on"
    if value in ("", "off", "false", "0", "no", "n", "none"):
        return "off"
    return value


def _split_names(raw: str) -> list[str]:
    return [
        name.strip() for name in re.split(r"[,，]", str(raw or "")) if name.strip()
    ]


def load_config() -> BotConfig:
    """Load configuration from environment variables.

    Returns a validated BotConfig instance.
    Raises RuntimeError if required configuration is missing.
    """
    kwargs: dict = {
        "wechat_backend": os.getenv("WECHAT_BACKEND", "wcdb").strip(),
        "wechat_groups": _decode_wechat_groups(os.getenv("WECHAT_GROUPS", "*")),
        "private_chat_enabled": os.getenv("PRIVATE_CHAT_ENABLED", "false").strip().lower() == "true",
        "private_chats": _decode_wechat_groups(os.getenv("PRIVATE_CHATS", "*")),
        "wechat_data_dir": os.getenv("WECHAT_DATA_DIR", "").strip(),
        "bot_display_name": _sanitize_display_name(os.getenv("BOT_DISPLAY_NAME", "群聊小助手")),
        "admin_wxid": os.getenv("ADMIN_WXID", "").strip(),
        "db_path": os.getenv("DB_PATH", "data/messages.db").strip(),
        "poll_interval_sec": float(os.getenv("POLL_INTERVAL_SEC", "1.0")),
        "dedup_window_sec": int(os.getenv("DEDUP_WINDOW_SEC", "60")),
        "message_burst_enabled": os.getenv("MESSAGE_BURST_ENABLED", "true").strip().lower() == "true",
        "message_burst_window_sec": float(os.getenv("MESSAGE_BURST_WINDOW_SEC", "2.2")),
        "message_burst_max_messages": int(os.getenv("MESSAGE_BURST_MAX_MESSAGES", "5")),
        "message_burst_max_chars": int(os.getenv("MESSAGE_BURST_MAX_CHARS", "200")),
        "image_vision_lookback_sec": int(os.getenv("IMAGE_VISION_LOOKBACK_SEC", "900")),
        "image_vision_max_bytes": int(os.getenv("IMAGE_VISION_MAX_BYTES", str(5 * 1024 * 1024))),
        "log_level": os.getenv("LOG_LEVEL", "INFO").strip(),
        "log_file": os.getenv("LOG_FILE", "data/bot.log").strip(),
        # Voice recognition
        "voice_asr_enabled": os.getenv("VOICE_ASR_ENABLED", "false").strip().lower() == "true",
        "voice_asr_backend": os.getenv("VOICE_ASR_BACKEND", "local_whisper").strip(),
        "voice_asr_language": os.getenv("VOICE_ASR_LANGUAGE", "zh").strip(),
        "voice_openai_api_key": os.getenv("VOICE_OPENAI_API_KEY", "").strip(),
        "voice_openai_base_url": os.getenv("VOICE_OPENAI_BASE_URL", "").strip(),
        "voice_local_model": os.getenv("VOICE_LOCAL_MODEL", "small").strip(),
        "voice_asr_to_simplified": os.getenv("VOICE_ASR_TO_SIMPLIFIED", "true").strip().lower() == "true",
        # Observability (decision trace)
        "decision_trace_enabled": os.getenv("DECISION_TRACE_ENABLED", "true").strip().lower() == "true",
        "decision_trace_path": os.getenv("DECISION_TRACE_PATH", "data/decision_trace.jsonl").strip() or "data/decision_trace.jsonl",
        "decision_trace_content_chars": int(os.getenv("DECISION_TRACE_CONTENT_CHARS", "120")),
        "send_typing_delay": _parse_switch(os.getenv("SEND_TYPING_DELAY", "off")),
        "send_quiet": _parse_switch(os.getenv("SEND_QUIET", "off")),
        "send_idle_sec": float(os.getenv("SEND_IDLE_SEC", "3")),
        "send_idle_max_wait_sec": float(os.getenv("SEND_IDLE_MAX_WAIT_SEC", "30")),
        "send_keep_clipboard": _parse_switch(os.getenv("SEND_KEEP_CLIPBOARD", "on")),
        "sticker_paste": os.getenv("STICKER_PASTE", "file").strip().lower() or "file",
        "send_queue": _parse_switch(os.getenv("SEND_QUEUE", "off")),
        "claude_relay": _parse_switch(os.getenv("CLAUDE_RELAY", "off")),
        "claude_relay_token": os.getenv("CLAUDE_RELAY_TOKEN", "").strip(),
        "claude_relay_host": os.getenv("CLAUDE_RELAY_HOST", "0.0.0.0").strip() or "0.0.0.0",
        "claude_relay_port": int(os.getenv("CLAUDE_RELAY_PORT", "8790")),
        "claude_relay_prefix": os.getenv("CLAUDE_RELAY_PREFIX", "cc").strip(),
        "claude_relay_wxids": _split_names(os.getenv("CLAUDE_RELAY_WXIDS", "")),
        "claude_relay_db": os.getenv("CLAUDE_RELAY_DB", "data/claude_relay.db").strip() or "data/claude_relay.db",
        "claude_relay_max_chars": int(os.getenv("CLAUDE_RELAY_MAX_CHARS", os.getenv("CLAUDE_RELAY_LONG_CHARS", "1500"))),
        "claude_relay_only": _parse_switch(os.getenv("CLAUDE_RELAY_ONLY", "off")),
        "claude_relay_progress_min": int(os.getenv("CLAUDE_RELAY_PROGRESS_MIN", "3")),
        "claude_relay_long_file": _parse_switch(os.getenv("CLAUDE_RELAY_LONG_FILE", "on")),
        "prm_copilot": _parse_switch(os.getenv("PRM_COPILOT", "off")),
        "prm_db": os.getenv("PRM_DB", "data/prm.db").strip() or "data/prm.db",
        "prm_takeover_min": int(os.getenv("PRM_TAKEOVER_MIN", "120")),
        "prm_holding_min": int(os.getenv("PRM_HOLDING_MIN", "10")),
        "prm_holding_text": os.getenv("PRM_HOLDING_TEXT", "收到，我确认一下马上回你").strip() or "收到，我确认一下马上回你",
        "prm_retention_days": int(os.getenv("PRM_RETENTION_DAYS", "90")),
        "prm_commission_after": os.getenv("PRM_COMMISSION_AFTER", "delivered").strip().lower() or "delivered",
        "prm_catchup_hours": int(os.getenv("PRM_CATCHUP_HOURS", "12")),
        "prm_edge": _parse_switch(os.getenv("PRM_EDGE", "off")),
        "prm_edge_token": os.getenv("PRM_EDGE_TOKEN", "").strip(),
        "edge_url": os.getenv("EDGE_URL", "").strip(),
        "edge_db": os.getenv("EDGE_DB", "data/edge.db").strip() or "data/edge.db",
        "edge_send_min_gap_sec": int(os.getenv("EDGE_SEND_MIN_GAP_SEC", "3")),
        "edge_send_per_hour": int(os.getenv("EDGE_SEND_PER_HOUR", "60")),
        "edge_seen_text_sec": int(os.getenv("EDGE_SEEN_TEXT_SEC", "1800")),
        "edge_seen_file_sec": int(os.getenv("EDGE_SEEN_FILE_SEC", "600")),
        "edge_keep_days": int(os.getenv("EDGE_KEEP_DAYS", "7")),
        "ocr_fallback": _parse_switch(os.getenv("OCR_FALLBACK", "off")),
        "ocr_fallback_after": int(os.getenv("OCR_FALLBACK_AFTER", "3")),
        "ocr_poll_sec": float(os.getenv("OCR_POLL_SEC", "4")),
        "auto_accept_friends": _parse_switch(os.getenv("AUTO_ACCEPT_FRIENDS", "off")),
        "auto_accept_interval_sec": int(os.getenv("AUTO_ACCEPT_INTERVAL_SEC", "300")),
        "auto_accept_idle_sec": int(os.getenv("AUTO_ACCEPT_IDLE_SEC", "60")),
        "auto_accept_watch": _parse_switch(os.getenv("AUTO_ACCEPT_WATCH", "on")),
        "auto_accept_full_check_sec": int(os.getenv("AUTO_ACCEPT_FULL_CHECK_SEC", "900")),
        "auto_accept_daily_max": int(os.getenv("AUTO_ACCEPT_DAILY_MAX", "50")),
    }

    _validate_config(kwargs)

    return BotConfig(**kwargs)


def is_onboarding_done() -> bool:
    """Check if onboarding has been completed without loading full config.

    Uses find_env_file() plus the optional machine-local override. In VM
    deployments, WCDB_KEY can live in .env.local while shared .env keeps the
    common settings.
    """
    onboarding_value = None
    has_real_wcdb_key = False

    for env_path in (find_env_file(), find_local_env_file()):
        if not env_path or not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("ONBOARDING_DONE="):
                onboarding_value = stripped.split("=", 1)[1].strip().lower()
            elif stripped.startswith("WCDB_KEY="):
                key = stripped.split("=", 1)[1].strip()
                if key and "***" not in key:
                    has_real_wcdb_key = True

    if onboarding_value == "true":
        return True

    # If the key is already present, don't send the user back through the
    # key-extraction flow just because an old shared ONBOARDING_DONE flag says
    # false. Bot startup will still validate the rest of the config.
    if has_real_wcdb_key:
        return True

    return False
