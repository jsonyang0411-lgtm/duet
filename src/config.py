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
    # "on" (WCDB backend): after WeChat's search opened a chat, its title is read from the screen (Windows
    # OCR) and nothing is pasted into another chat the bot knows of (a name inside another one).
    send_verify_chat: str = "on"

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
    # PRM_AUTO_REGISTER (2026-10-29): "on": every new friend the new-friend scan finds (src/wechat/new_friends.py:
    # Windows wcdb, copilot mode only, never PRM_EDGE) is registered as a full creator, as if an operator had clicked
    # 登记 (PrmService.new_friend_found -> register_found on PRM's own thread): her messages since she was added become
    # one draft that waits for a person, her later drafts go through the gates and the night window like any creator's.
    # Never the admin or a cc user, never a wxid that has a party already (an operator's 停用 sticks).  "off": as before,
    # registering is a person's click in the console.
    prm_auto_register: str = "off"
    # 微信里审批草稿 (2026-11-09, the user: away from the PC, they copied every draft into WeChat by hand): "on": every
    # draft that waits for a person in 「待审批」 is also told once, as one message, to the admin's private chat
    # (ADMIN_WXID; src/prm/wechat_review.py), and the admin answers there (quoting it: 1 send / 2 drop / 改 + their words;
    # 「发 12」「不发 12」「改 12 …」), the same decision as the console's.  Never for what goes out by itself, a 百应 draft or
    # a holding reply.  A platform action runs from WeChat only by 「执行」 quoting its notification (or 「执行 12」), only
    # the preview that notification showed whole (one it could not list whole, whose 要注意 it cut, held for its photo
    # check or for pictures that did not come stays the console's 批准并执行); 1 / 好 / 发 never run one, and its reply is
    # edited in the console only.  A draft that changes after its notification (her recall, a new 要注意, group lines it
    # did not hear) is told again (「#12 草稿（第 2 次通知）」); an answer to the older one does nothing.  On without
    # ADMIN_WXID, or with a group's id or a registered creator's wxid in it, it does nothing (the console says why).
    # "off" (the default): nothing of it.
    prm_wechat_review: str = "off"
    # An operator's own WeChat reply in a creator's chat (or the console) gives the chat to
    # a person for this long: the creator's messages are only recorded meanwhile.
    prm_takeover_min: int = 120
    # A draft waiting this long gets one holding reply per round of the creator's
    # messages (8.1); 0 = never.
    prm_holding_min: int = 10
    prm_holding_text: str = "收到，我确认一下马上回您"
    # 开场白没人处理就自动发 (2026-11-02, the user: a friend who accepts after the operators went home heard nothing all
    # weekend): our first line to a new friend (a 开场白, or the first draft of a new friend's messages from before she was
    # registered) that passes the night checks (src/prm/service.py ``_opening_auto``) goes out by itself when it has waited
    # this many minutes in 待审批 with nobody deciding on it; in the 下班时段 or 自动模式 at once.  0 = off: it waits for a
    # person, as before.  Never a 老板拉的群's 开场白 (the boss reads it).
    prm_opening_auto_min: int = 0
    # A reply goes out as up to this many WeChat messages, one per paragraph of its draft (a blank line between
    # two), a typing pause apart (src/prm/service.py reply_parts); 1 = one message, as before.
    prm_reply_parts: int = 3
    # 发得慢一点 (2026-11-04, the user: 「回消息的时候尽量别一口气发一堆过去」): a reply's messages go out at a person's pace
    # (src/prm/service.py ``_deliver``: 2-5 s between its text messages by length, 3-6 s before its first file after
    # the text, 2-4 s between files); off = as before (a 0.4-3.5 s typing pause between text messages, files at once).
    prm_send_pace: str = "on"
    # 下班时段自动回复 ("18:00-09:00", may pass midnight; empty = off): inside it, by this PC's clock, a creator-reply
    # draft whose kind has not earned "auto" still goes out by itself when every night rule holds (src/prm/gates.py
    # night_reason: no complaint, nothing plain_reason minds, no commission above 25 % or 坑位 / 保底 …, a flagged
    # draft only as one holding line); holding replies inside it say prm_night_holding_text.
    prm_night_auto_hours: str = ""
    prm_night_holding_text: str = "收到～您说的我都记下啦，明早第一时间回复您哈～"
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
    # Where the sample orders the bot made stand in 聚水潭 (jst samples, read-only on the Mac): asked once
    # this many minutes (0 = off); a waybill fills the deal like the console's 填运单号 (the shipping notice
    # always waits for a person), 已签收 marks it like the console's (the 专属佣金 chain starts).  The deal
    # says so when an order is not sent PRM_SAMPLE_WAYBILL_DAYS after it was made, or is not signed for
    # PRM_SAMPLE_SIGN_DAYS after it was sent (聚水潭's list gives no waybill as yet: the deal asks for one).
    prm_sample_sync_min: int = 60
    prm_sample_waybill_days: int = 3
    prm_sample_sign_days: int = 10
    # 签收后问一句 (2026-11-07, the user: 「可以加」): an order the sync above sees signed for in 聚水潭 (the bot's own, or one
    # of ours made by hand whose waybill went into her deal) and she has not said she got it: one draft job asks her how
    # the samples suit her (kb 场景 D2, 「聚水潭显示签收」), only inside PRM_SIGNED_HELLO_HOURS by this PC's clock (a
    # signature outside waits for them).  By day its draft always waits in 待审批, whatever its kind's gate; in the
    # night window or 自动模式 the night's rules judge it, like a reply draft's.  "off" (the default): nothing of it.
    prm_signed_hello: str = "off"
    prm_signed_hello_hours: str = "10:00-20:00"
    # 直播手卡 (2026-11-09, the user: the bot makes the cards itself, a person still looks first): every WeChat draft job
    # tells the Mac it may make cards when she asks for them (``meta.handcards_ok``; the Mac's handcards.json and the
    # skill live-handcard make them, src/prm/handcards.py), and each deal has 「做手卡」.  A draft with cards always waits
    # for a person.  "off" (the default): nothing of it.
    prm_handcards: str = "off"
    # 起草的上下文 (2026-11-09, PRM_DRAFT_CONTEXT): a creator's draft jobs carry ``meta.draft_context`` (the 聚水潭 sample
    # orders her deal knows, her 抖音号 / 小红书号): the Mac reads her sample orders in 聚水潭 by them (<sample_status>),
    # matches the pictures she sent against its hat photo index (<photo_matches>), and checks a draft's sample order by
    # photo (a hat with no picture behind it is not ready; the side-by-side sheet shows on the draft, for the operators
    # only).  "off" (the default): nothing of it, the Mac's prompts as before (src/prm/draft_context.py).
    prm_draft_context: str = "off"
    # The 抖音百应 private messages (精选联盟私信) of the creators invited by 邀约带货: asked of the Mac once this many
    # minutes (0 = off, the default; the Mac's `dy buyin im-inbox`, read-only, is still to be written: until then the
    # console says it cannot be read).  A registered creator's new lines become a 百应 draft in 待审批 that an operator
    # copies and sends in 百应 by hand; the bot never sends it.
    prm_buyin_sync_min: int = 0
    # 加百应达人微信 (src/prm/friend_adds.py, CODEBASE_REFERENCE 功能 R): "on" lets an operator turn the contact a creator
    # left in 百应 into ONE WeChat friend request (the console's 「添加该达人微信」, or the admin's 加达人 N) through the
    # friend manager's add_friend (Windows: the WeChat window), at most prm_friend_add_daily_max a day and
    # prm_friend_add_gap_sec apart; WeChat's risk prompt pauses it with the auto-accept.  Off (the default): nothing of
    # it is kept or done.  The greeting: {昵称} her 百应 nickname, {品牌} prm_friend_add_brand (this PC's .env only).
    prm_friend_add: str = "off"
    prm_friend_add_daily_max: int = 10
    prm_friend_add_gap_sec: int = 120
    prm_friend_add_greeting: str = "您好，我是{品牌}，百应上联系您的～"
    prm_friend_add_brand: str = ""
    # The remark (备注) she gets in our WeChat when the bot adds her: {昵称} her 百应 nickname (her 抖音号 when it has
    # none or a masked number), at most 16 characters once filled in; "" (PRM_FRIEND_ADD_REMARK= in .env) = no remark.
    prm_friend_add_remark: str = "抖音-{昵称}"
    # 三方群 (src/prm/three_party.py, 2026-10-30): who goes into the group of 「拉三方群」 besides her and this account, by
    # their names in this account's contacts (their 备注), comma-separated, at most 3 (this PC's .env only, never the
    # repo); "" (the default) = no 「拉三方群」.  The group name the console suggests: {品牌} prm_friend_add_brand, {昵称}
    # her 称呼 (the bot does not rename the group itself).
    prm_group_with: str = ""
    prm_group_name: str = "{品牌}×{昵称}"
    # 老板拉的群 (src/prm/boss_groups.py, 2026-10-31): "on": a group a boss of PRM_GROUP_WITH makes with a creator and this
    # account (found after the switch first ran; at most prm_boss_group_max members) is taken over: with exactly one
    # person besides us and the boss she is registered and our first line in the group is drafted (it waits for a
    # person); else the console asks which member she is.  Her lines there are drafted like her private ones (the
    # gates, 下班时段 and 自动模式 alike; no holding reply in a group); the boss's and the other members' lines are only
    # context, every other group stays silent.  Needs PRM_COPILOT=on and PRM_GROUP_WITH (the console says so otherwise).
    # "off" (the default): nothing of it.
    prm_boss_groups: str = "off"
    prm_boss_group_max: int = 6
    # What the Mac is told the boss is called (1-10 characters): the boss's names (PRM_GROUP_WITH) never leave this PC;
    # a group job says this instead (its ``meta.group.boss``, and in the group's name and the lines it is told).
    prm_boss_call: str = "老板"
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

    for send_setting in ("send_typing_delay", "send_quiet", "send_keep_clipboard", "send_queue", "send_verify_chat"):
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
    # (on without PRM_COPILOT, or with PRM_EDGE, it only does nothing: Bot._start_new_friends logs so.  Not refused:
    # turning the copilot off in a hurry must not stop the bot from starting.)
    if kwargs.get("prm_auto_register", "off") not in ("off", "on"):
        errors.append(f"PRM_AUTO_REGISTER must be off or on, got {kwargs.get('prm_auto_register')}")
    # (on without ADMIN_WXID it only does nothing: 「待审批」 says why.  Not refused, as PRM_BOSS_GROUPS is not.)
    if kwargs.get("prm_wechat_review", "off") not in ("off", "on"):
        errors.append(f"PRM_WECHAT_REVIEW must be off or on, got {kwargs.get('prm_wechat_review')}")
    if not (5 <= int(kwargs.get("prm_takeover_min", 120)) <= 1440):
        errors.append(f"PRM_TAKEOVER_MIN must be between 5 and 1440, got {kwargs.get('prm_takeover_min')}")
    if not (0 <= int(kwargs.get("prm_holding_min", 10)) <= 120):
        errors.append(f"PRM_HOLDING_MIN must be between 0 and 120, got {kwargs.get('prm_holding_min')}")
    if not (1 <= len(str(kwargs.get("prm_holding_text", "收到，我确认一下马上回您")).strip()) <= 60):
        errors.append("PRM_HOLDING_TEXT must be 1-60 characters")
    if not (0 <= int(kwargs.get("prm_opening_auto_min", 0)) <= 1440):
        errors.append(f"PRM_OPENING_AUTO_MIN must be between 0 and 1440, got {kwargs.get('prm_opening_auto_min')}")
    if not (1 <= int(kwargs.get("prm_reply_parts", 3)) <= 5):
        errors.append(f"PRM_REPLY_PARTS must be between 1 and 5, got {kwargs.get('prm_reply_parts')}")
    if kwargs.get("prm_send_pace", "on") not in ("off", "on"):
        errors.append(f"PRM_SEND_PACE must be off or on, got {kwargs.get('prm_send_pace')}")
    if str(kwargs.get("prm_night_auto_hours", "") or "").strip():
        from src.prm.gates import night_window

        try:
            night_window(kwargs["prm_night_auto_hours"])
        except ValueError:
            errors.append("PRM_NIGHT_AUTO_HOURS must be HH:MM-HH:MM (like 18:00-09:00, may pass midnight; "
                          f"empty = off), got {kwargs.get('prm_night_auto_hours')}")
    if not (1 <= len(str(kwargs.get("prm_night_holding_text", "收到～您说的我都记下啦，明早第一时间回复您哈～")).strip())
            <= 60):
        errors.append("PRM_NIGHT_HOLDING_TEXT must be 1-60 characters")
    if not (1 <= int(kwargs.get("prm_retention_days", 90)) <= 365):
        errors.append(f"PRM_RETENTION_DAYS must be between 1 and 365, got {kwargs.get('prm_retention_days')}")
    if kwargs.get("prm_commission_after", "delivered") not in ("delivered", "created"):
        errors.append(f"PRM_COMMISSION_AFTER must be delivered or created, got {kwargs.get('prm_commission_after')}")
    if not (0 <= int(kwargs.get("prm_catchup_hours", 12)) <= 72):
        errors.append(f"PRM_CATCHUP_HOURS must be between 0 and 72, got {kwargs.get('prm_catchup_hours')}")
    sync_min = int(kwargs.get("prm_sample_sync_min", 60))
    if not (sync_min == 0 or 10 <= sync_min <= 1440):
        errors.append(f"PRM_SAMPLE_SYNC_MIN must be 0 (off) or between 10 and 1440, got {kwargs.get('prm_sample_sync_min')}")
    if not (1 <= int(kwargs.get("prm_sample_waybill_days", 3)) <= 30):
        errors.append(f"PRM_SAMPLE_WAYBILL_DAYS must be between 1 and 30, got {kwargs.get('prm_sample_waybill_days')}")
    if not (1 <= int(kwargs.get("prm_sample_sign_days", 10)) <= 60):
        errors.append(f"PRM_SAMPLE_SIGN_DAYS must be between 1 and 60, got {kwargs.get('prm_sample_sign_days')}")
    if kwargs.get("prm_signed_hello", "off") not in ("off", "on"):
        errors.append(f"PRM_SIGNED_HELLO must be off or on, got {kwargs.get('prm_signed_hello')}")
    if kwargs.get("prm_handcards", "off") not in ("off", "on"):
        errors.append(f"PRM_HANDCARDS must be off or on, got {kwargs.get('prm_handcards')}")
    if kwargs.get("prm_draft_context", "off") not in ("off", "on"):
        errors.append(f"PRM_DRAFT_CONTEXT must be off or on, got {kwargs.get('prm_draft_context')}")
    hello_hours = str(kwargs.get("prm_signed_hello_hours", "10:00-20:00") or "").strip()
    if hello_hours:                                    # (empty: the default, 10:00-20:00)
        from src.prm.gates import night_window

        try:
            night_window(hello_hours)
        except ValueError:
            errors.append(f"PRM_SIGNED_HELLO_HOURS must be HH:MM-HH:MM (like 10:00-20:00), got {hello_hours}")
    buyin_min = int(kwargs.get("prm_buyin_sync_min", 0))
    if not (buyin_min == 0 or 5 <= buyin_min <= 1440):
        errors.append(f"PRM_BUYIN_SYNC_MIN must be 0 (off) or between 5 and 1440, got {kwargs.get('prm_buyin_sync_min')}")
    friend_add = kwargs.get("prm_friend_add", "off")
    if friend_add not in ("off", "on"):
        errors.append(f"PRM_FRIEND_ADD must be off or on, got {friend_add}")
    if not (1 <= int(kwargs.get("prm_friend_add_daily_max", 10)) <= 30):
        errors.append(f"PRM_FRIEND_ADD_DAILY_MAX must be between 1 and 30, got {kwargs.get('prm_friend_add_daily_max')}")
    if not (30 <= int(kwargs.get("prm_friend_add_gap_sec", 120)) <= 3600):
        errors.append(f"PRM_FRIEND_ADD_GAP_SEC must be between 30 and 3600, got {kwargs.get('prm_friend_add_gap_sec')}")
    if not (1 <= len(str(kwargs.get("prm_friend_add_greeting", "您好，我是{品牌}，百应上联系您的～")).strip()) <= 60):
        errors.append("PRM_FRIEND_ADD_GREETING must be 1-60 characters")
    if len(str(kwargs.get("prm_friend_add_brand", "") or "").strip()) > 20:
        errors.append("PRM_FRIEND_ADD_BRAND must be at most 20 characters")
    remark = str(kwargs.get("prm_friend_add_remark", "抖音-{昵称}") or "").strip()
    if remark and (remark.count("{昵称}") != 1 or len(remark.replace("{昵称}", "")) > 10):
        errors.append("PRM_FRIEND_ADD_REMARK must be empty (no remark) or hold {昵称} once and at most 10 other characters")
    group_raw = str(kwargs.get("prm_group_with", "") or "")
    group_names = [" ".join(n.split()) for n in re.split(r"[,，、;；]", group_raw) if n.strip()]
    if len(group_names) > 3 or len(set(group_names)) != len(group_names):
        errors.append("PRM_GROUP_WITH must name at most 3 different people (comma-separated)")
    elif any(len(n) > 20 or not any(ch.isalnum() for ch in n) for n in group_names):
        errors.append("PRM_GROUP_WITH: each name must be 1-20 characters with a letter, a digit or a Chinese character")
    group_name = str(kwargs.get("prm_group_name", "{品牌}×{昵称}") or "").strip()
    if group_name.count("{昵称}") != 1 or len(group_name.replace("{昵称}", "").replace("{品牌}", "")) > 12:
        errors.append("PRM_GROUP_NAME must hold {昵称} once and at most 12 other characters ({品牌} aside)")
    # (on without PRM_COPILOT or PRM_GROUP_WITH it only does nothing: the console says why.  Not refused, as
    # PRM_AUTO_REGISTER is not.)
    if kwargs.get("prm_boss_groups", "off") not in ("off", "on"):
        errors.append(f"PRM_BOSS_GROUPS must be off or on, got {kwargs.get('prm_boss_groups')}")
    if not (3 <= int(kwargs.get("prm_boss_group_max", 6)) <= 20):
        errors.append(f"PRM_BOSS_GROUP_MAX must be between 3 and 20, got {kwargs.get('prm_boss_group_max')}")
    boss_call = " ".join(str(kwargs.get("prm_boss_call", "老板") or "").split())
    if not 1 <= len(boss_call) <= 10:
        errors.append(f"PRM_BOSS_CALL must be 1 to 10 characters, got {len(boss_call)}")
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
        "send_verify_chat": _parse_switch(os.getenv("SEND_VERIFY_CHAT", "on")),
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
        "prm_auto_register": _parse_switch(os.getenv("PRM_AUTO_REGISTER", "off")),
        "prm_wechat_review": _parse_switch(os.getenv("PRM_WECHAT_REVIEW", "off")),
        "prm_takeover_min": int(os.getenv("PRM_TAKEOVER_MIN", "120")),
        "prm_holding_min": int(os.getenv("PRM_HOLDING_MIN", "10")),
        "prm_holding_text": os.getenv("PRM_HOLDING_TEXT", "收到，我确认一下马上回您").strip() or "收到，我确认一下马上回您",
        "prm_opening_auto_min": int(os.getenv("PRM_OPENING_AUTO_MIN", "0")),
        "prm_reply_parts": int(os.getenv("PRM_REPLY_PARTS", "3")),
        "prm_send_pace": _parse_switch(os.getenv("PRM_SEND_PACE", "on")),
        "prm_night_auto_hours": os.getenv("PRM_NIGHT_AUTO_HOURS", "").strip(),
        "prm_night_holding_text": (os.getenv("PRM_NIGHT_HOLDING_TEXT", "收到～您说的我都记下啦，明早第一时间回复您哈～").strip()
                                   or "收到～您说的我都记下啦，明早第一时间回复您哈～"),
        "prm_retention_days": int(os.getenv("PRM_RETENTION_DAYS", "90")),
        "prm_commission_after": os.getenv("PRM_COMMISSION_AFTER", "delivered").strip().lower() or "delivered",
        "prm_catchup_hours": int(os.getenv("PRM_CATCHUP_HOURS", "12")),
        "prm_sample_sync_min": int(os.getenv("PRM_SAMPLE_SYNC_MIN", "60")),
        "prm_sample_waybill_days": int(os.getenv("PRM_SAMPLE_WAYBILL_DAYS", "3")),
        "prm_sample_sign_days": int(os.getenv("PRM_SAMPLE_SIGN_DAYS", "10")),
        "prm_signed_hello": _parse_switch(os.getenv("PRM_SIGNED_HELLO", "off")),
        "prm_signed_hello_hours": os.getenv("PRM_SIGNED_HELLO_HOURS", "").strip() or "10:00-20:00",
        "prm_handcards": _parse_switch(os.getenv("PRM_HANDCARDS", "off")),
        "prm_draft_context": _parse_switch(os.getenv("PRM_DRAFT_CONTEXT", "off")),
        "prm_buyin_sync_min": int(os.getenv("PRM_BUYIN_SYNC_MIN", "0")),
        "prm_friend_add": _parse_switch(os.getenv("PRM_FRIEND_ADD", "off")),
        "prm_friend_add_daily_max": int(os.getenv("PRM_FRIEND_ADD_DAILY_MAX", "10")),
        "prm_friend_add_gap_sec": int(os.getenv("PRM_FRIEND_ADD_GAP_SEC", "120")),
        "prm_friend_add_greeting": os.getenv("PRM_FRIEND_ADD_GREETING", "").strip() or "您好，我是{品牌}，百应上联系您的～",
        "prm_friend_add_brand": os.getenv("PRM_FRIEND_ADD_BRAND", "").strip(),
        "prm_friend_add_remark": os.getenv("PRM_FRIEND_ADD_REMARK", "抖音-{昵称}").strip(),   # set but empty: no remark
        "prm_group_with": os.getenv("PRM_GROUP_WITH", "").strip(),
        "prm_group_name": os.getenv("PRM_GROUP_NAME", "").strip() or "{品牌}×{昵称}",
        "prm_boss_groups": _parse_switch(os.getenv("PRM_BOSS_GROUPS", "off")),
        "prm_boss_group_max": int(os.getenv("PRM_BOSS_GROUP_MAX", "6")),
        "prm_boss_call": " ".join(os.getenv("PRM_BOSS_CALL", "").split()) or "老板",
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
