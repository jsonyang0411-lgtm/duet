"""Configuration loading from .env file."""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from dotenv import dotenv_values, load_dotenv

from .send.wechat_emoji import parse_codes as parse_wechat_emoji_codes

from .persona import (
    DEFAULT_CHAT_CONTEXT_COUNT,
    DEFAULT_CHAT_CONTEXT_WINDOW_SEC,
    DEFAULT_CHAT_MAX_CHARS,
    DEFAULT_CHAT_PERSONA_NAME,
    DEFAULT_CHAT_PERSONA_PROMPT,
    DEFAULT_CHAT_REPLY_RULES,
    DEFAULT_YANG_YUJING_ALWAYS_REPLY_GROUP_NAME,
    DEFAULT_YANG_YUJING_GROUP_NAME,
    PERSONA_PROFILES,
    PERSONA_PROFILE_YANG_YUJING,
    persona_defaults_for_profile,
)


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

def _decode_env_text(raw: str | None, default: str = "", limit: int = 8000) -> str:
    """Decode textarea-like values stored in .env with literal \n escapes."""
    if raw is None:
        return default

    text = str(raw)
    if not text.strip():
        return default

    text = (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    # Keep newlines/tabs, strip other control chars.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return text[:limit] if text else default


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
    """All configuration for the WeChat summarizer bot."""

    # === AI Backend ===
    # "claude", "deepseek", or "openai"
    ai_backend: str = "claude"

    # === Claude (Anthropic) ===
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    summarize_model: str = "claude-haiku-4-5-20251001"
    # Model for chat replies, proactive replies, chunk summaries and memory
    # consolidation.  Defaults to the Haiku model those calls always used, so
    # changing SUMMARIZE_MODEL (the final summaries) does not silently make
    # every chat reply more expensive.
    anthropic_chat_model: str = "claude-haiku-4-5-20251001"

    # === DeepSeek ===
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"

    # === OpenAI ===
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-terra"
    openai_base_url: str = ""

    # === WeChat Backend ===
    wechat_backend: str = "wcdb"
    # Comma-separated group names to monitor. "*" = auto-discover all groups.
    wechat_groups: str = "*"
    # Reply to one-on-one WeChat chats. Private chats do not require @mention.
    private_chat_enabled: bool = False
    # Comma-separated private chat display names/user IDs. "*" = auto-discover.
    private_chats: str = "*"
    # Custom WeChat data directory. Leave empty to auto-detect from Documents.
    # Set to the parent directory containing wxid_* folders (e.g. D:\WeChatData).
    wechat_data_dir: str = ""

    # === Bot Identity ===
    bot_display_name: str = "群聊小助手"
    # Admin wxid (can manage nicknames and bot settings)
    admin_wxid: str = ""


    # === Conversational Persona ===
    # ``yang_yujing`` (the default) listens to every new message in its
    # configured target group. ``custom`` answers when called; its text is
    # edited in the console and starts as 杨宇靖's.
    chat_persona_profile: str = PERSONA_PROFILE_YANG_YUJING
    chat_persona_name: str = DEFAULT_CHAT_PERSONA_NAME
    chat_persona_prompt: str = DEFAULT_CHAT_PERSONA_PROMPT
    chat_reply_rules: str = DEFAULT_CHAT_REPLY_RULES
    chat_max_chars: int = DEFAULT_CHAT_MAX_CHARS
    chat_context_window_sec: int = DEFAULT_CHAT_CONTEXT_WINDOW_SEC
    chat_context_count: int = DEFAULT_CHAT_CONTEXT_COUNT
    yang_yujing_group_name: str = DEFAULT_YANG_YUJING_GROUP_NAME
    yang_yujing_always_reply_group_name: str = (
        DEFAULT_YANG_YUJING_ALWAYS_REPLY_GROUP_NAME
    )
    # === Trigger Keywords ===
    trigger_keywords: list[str] = field(default_factory=lambda: [
        "总结一下", "之前发了什么", "错过了什么", "summarize",
        "what did i miss", "聊天总结", "帮我总结", "前面说了什么",
        "说了啥", "发生了什么",
    ])

    # === Summarization ===
    summarize_enabled: bool = True
    fallback_window_hours: int = 8

    # === Feishu / Lark Export ===
    feishu_export_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # knowledge, spreadsheet, bitable, or docx
    feishu_export_mode: str = "knowledge"
    feishu_export_window_hours: int = 8
    feishu_auto_sync_enabled: bool = False
    feishu_auto_sync_min_messages: int = 20
    feishu_auto_sync_cooldown_sec: int = 1800
    feishu_knowledge_base_name: str = "webot 群聊沉淀"
    feishu_knowledge_folder_token: str = ""
    feishu_export_trigger_keywords: list[str] = field(default_factory=lambda: [
        "同步到飞书", "导出到飞书", "写到飞书", "沉淀到飞书",
    ])
    feishu_spreadsheet_token: str = ""
    feishu_spreadsheet_range: str = "Sheet1!A:H"
    feishu_bitable_app_token: str = ""
    feishu_bitable_table_id: str = ""
    feishu_doc_folder_token: str = ""

    # === Database ===
    db_path: str = "data/messages.db"

    # === Fun Features ===
    # @抽签 — draw a fortune lot (大吉/中吉/小吉/末吉/凶)
    fun_enabled: bool = True

    # === Proactive Participation ===
    # Master switch — enable autonomous chat participation without @mention
    proactive_enabled: bool = False
    # Rate thresholds for each mode (msgs/min).  When the message rate
    # exceeds a threshold, the bot enters that mode.  Calibrate these
    # by running:  python tools/analyze_chat_rhythm.py
    proactive_rate_window_sec: int = 120  # rate calculation window
    proactive_rate_quiet: float = 1.5     # SLEEP → QUIET  boundary
    proactive_rate_casual: float = 4.0    # QUIET → CASUAL boundary
    proactive_rate_lively: float = 6.5    # CASUAL → LIVELY boundary
    proactive_rate_burst: float = 8.5     # LIVELY → BURST  boundary

    # === Welcome New Member ===
    # Master switch — automatically send a welcome message when a new
    # member joins a group chat.  Welcome templates and per-group settings
    # are stored in data/welcome_templates.json.
    welcome_enabled: bool = False

    # === Sticky Mention ===
    # When a user sends @bot with no message text, enter sticky listening
    # mode.  The user's next message in the same group is treated as if it
    # were @mentioned (one-shot).  Set enabled=false to disable entirely.
    sticky_mention_enabled: bool = True
    sticky_mention_ttl_sec: int = 60  # max wait for follow-up message

    # === Follow-up Listener ===
    # After the bot sends a group reply, briefly watch short messages that look
    # like they are continuing that reply.  Local heuristics filter candidates
    # before any AI call is made.
    followup_listen_enabled: bool = True
    followup_listen_ttl_sec: int = 60
    followup_listen_context_count: int = 6
    followup_listen_max_message_chars: int = 30

    # === Message Burst Coalescing ===
    # WeChat users often split one thought across multiple short bubbles.
    # Hold same-sender messages briefly so the AI sees one coherent turn.
    message_burst_enabled: bool = True
    message_burst_window_sec: float = 2.2
    message_burst_max_messages: int = 5
    message_burst_max_chars: int = 200

    # === Web Search ===
    # Triggered only for messages that clearly ask for search/current info.
    web_search_enabled: bool = True
    web_search_max_results: int = 4
    web_search_timeout_sec: float = 8.0
    web_search_region: str = "cn-zh"
    web_search_reply_max_chars: int = 120

    # === Image Understanding ===
    # Calls the vision model for explicit image questions, and once when the
    # same user sends an image/sticker immediately after the bot replied.
    image_vision_enabled: bool = True
    image_vision_lookback_sec: int = 900
    image_vision_max_bytes: int = 5 * 1024 * 1024
    image_vision_reply_max_chars: int = 80
    image_followup_enabled: bool = True
    image_followup_ttl_sec: int = 90
    image_screen_fallback_enabled: bool = True

    # === Group Todo ===
    # Master switch — @bot mention triggers todo management commands.
    todo_enabled: bool = True
    # Comma-separated group names where todo is active. "*" = all groups.
    todo_groups: list[str] = field(default_factory=lambda: ["*"])
    todo_max_per_group: int = 50           # max active todos per group (1-200)
    todo_completed_retention_days: int = 30  # auto-clean completed (0=forever)
    todo_deleted_retention_days: int = 30    # auto-clean deleted (0=forever)
    todo_add_keywords: list[str] = field(default_factory=lambda: [
        "记一下", "添加待办", "新建待办", "帮我记", "待办",
    ])
    todo_complete_keywords: list[str] = field(default_factory=lambda: [
        "搞定", "做完了", "完成", "完成了", "done",
    ])
    todo_delete_keywords: list[str] = field(default_factory=lambda: [
        "删掉", "删除", "取消", "不要了",
    ])

    # === Tuning ===
    poll_interval_sec: float = 1.0
    dedup_window_sec: int = 60
    max_messages_for_summary: int = 5000
    chunk_size: int = 400

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
    # One JSON line per chat / proactive model call (prompt, raw + cleaned reply).
    llm_call_log_enabled: bool = True
    llm_call_log_path: str = "data/llm_calls.jsonl"
    # Set false to keep only fingerprints + lengths instead of full prompts.
    llm_call_log_prompts: bool = True

    # === Structured pipeline (Phase 2) ===
    # "off": the original single prompt with regex clean-up of the reply.
    # "single": one call returns a JSON envelope (who is it for / speak or stay
    #   silent / the message).  "two_step": unclear messages first get a short
    #   "should I speak?" call, and only a yes is followed by the reply call.
    pipeline_v2: str = "off"
    # One extra call when the model's answer is not a usable envelope.
    pipeline_repair: bool = True
    # Use the provider's JSON mode when it has one (falls back by itself).
    pipeline_json_mode: bool = True
    # A reply over the length limit is sent back once to be rewritten shorter
    # instead of being cut at a sentence end (one more call, only when too long).
    pipeline_rewrite_long: str = "off"
    # Further names that count as calling the bot (the display name and the
    # persona name always do).  Only used when pipeline_v2 is on.
    bot_aliases: list[str] = field(default_factory=list)
    # Groups that use the structured pipeline (and, with it, the scored gate):
    # "*" = all, otherwise names or chat ids (substring match, like TODO_GROUPS);
    # "private" adds one-to-one chats.  Lets one group try it while the rest
    # keep the old behaviour.
    pipeline_v2_groups: list[str] = field(default_factory=lambda: ["*"])
    # Proactive gate: "dice" rolls a per-mode probability, "score" uses a
    # deterministic willingness score plus an hourly reply budget.
    proactive_gate_mode: str = "dice"
    proactive_hourly_budget: int = 6        # replies per group per hour, 0 = no limit
    proactive_score_threshold: float = 0.35

    # "heuristic": after a reply the bot listens for a short window with local
    # rules (the old behaviour).  "declared": with the structured pipeline the
    # model says in its answer whom it waits for (or that it waits for nobody).
    followup_mode: str = "heuristic"

    # Delivery of a reply: with ``send_bubbles`` on, a structured reply of two
    # messages is sent as two chat bubbles instead of one joined line; with
    # ``send_typing_delay`` on, each bubble waits a few seconds like a person typing.
    send_bubbles: str = "off"
    send_typing_delay: str = "off"
    # Quiet sending (Windows, src/wechat/quiet_send.py): wait until keyboard and
    # mouse were idle for ``send_idle_sec`` (at most ``send_idle_max_wait_sec``),
    # type into a chat's own window when there is one, then give back the focus
    # and, with ``send_keep_clipboard`` on, the clipboard.
    send_quiet: str = "off"
    send_idle_sec: float = 3.0
    send_idle_max_wait_sec: float = 30.0
    send_keep_clipboard: str = "on"
    # Expressions (structured pipeline only, src/send/): ``wechat_emoji`` lets the
    # model use WeChat's own emoji codes such as [捂脸] (``wechat_emoji_codes``
    # replaces the built-in list); ``send_stickers`` lets it add a sticker picture
    # from ``sticker_dir`` now and then, at most once per ``sticker_min_interval_sec``
    # per chat.  ``sticker_paste``: "file" pastes the file (a GIF stays animated),
    # "image" pastes a still picture (for a WeChat that sends pasted files as files).
    wechat_emoji: str = "off"
    wechat_emoji_codes: list[str] = field(default_factory=list)
    send_stickers: str = "off"
    sticker_dir: str = "data/stickers"
    sticker_min_interval_sec: int = 600
    sticker_paste: str = "file"
    # Which model does what (src/summarize/__init__.py:create_models).  "cloud":
    # everything on AI_BACKEND, as before.  "local": everything on the model on
    # your own machine.  "split": the LOCAL_TASKS groups (chat, proactive,
    # memory) on the local model, the rest on the cloud.
    model_route: str = "cloud"
    local_tasks: list[str] = field(default_factory=lambda: ["memory"])
    local_base_url: str = "http://127.0.0.1:11434/v1"
    local_model: str = ""
    local_api_key: str = ""
    local_timeout_sec: float = 90.0
    local_max_tokens: int = 2048
    # Sent with every local request to switch thinking off.  Both fields by
    # default: Ollama (the default LOCAL_BASE_URL) reads only reasoning_effort
    # and ignores chat_template_kwargs (measured: 20 s of thinking for "3+5-2"),
    # llama.cpp / vLLM read chat_template_kwargs.
    local_extra_body: dict | None = field(
        default_factory=lambda: {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
    )
    # The chat prompt the local model gets (src/pipeline/prompts.py): "standard"
    # is the cloud model's; "compact" leaves out empty sections and puts the
    # rules a small model misses (do what you are asked, do not make things up)
    # where it reads them.
    local_prompt: str = "standard"
    # CLOUD_FALLBACK=local (src/summarize/fallback.py): when a cloud chat call
    # fails (out of quota, rate limited, unreachable) the local model answers.
    cloud_fallback: str = "off"
    # Example dialogues of the persona (src/persona_examples.py): how many to show
    # per reply (0 = none) and where the files are; without a file nothing changes.
    persona_examples: int = 4
    persona_examples_dir: str = "data/examples"
    # One thread sends every reply (WCDB backend): a typing pause or the wait for
    # the user to pause (SEND_QUIET) no longer holds a worker thread.
    send_queue: str = "off"

    # === Memory v2 (Phase 3) ===
    # "off": the overwritten diary and the LIKE history search.  "on": validated
    # facts/episodes written by a background job, FTS5 + LIKE retrieval, and
    # the bot's own replies stored.
    memory_v2: str = "off"
    # Meaning-based fact search for memory v2 (src/memory/embeddings.py): an
    # OpenAI-compatible /embeddings endpoint; a fact counts as related when the
    # similarity is above memory_embedding_min_sim (depends on the model).
    memory_embeddings: str = "off"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    memory_embedding_min_sim: float = 0.55
    # Groups that use memory v2 ("*" = all, otherwise names or chat ids,
    # substring match; "private" adds one-to-one chats).
    memory_v2_groups: list[str] = field(default_factory=lambda: ["*"])
    memory_window_messages: int = 40        # new messages that trigger an extraction window
    memory_idle_minutes: int = 20           # quiet time after which >= 8 messages are processed
    memory_min_confidence: float = 0.6      # facts below this are never put in a prompt
    memory_prompt_facts: int = 12           # most facts injected into one prompt

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


def _validate_config(kwargs: dict) -> None:
    """Validate numeric config values.  Prints clear errors and exits on bad values."""
    errors: list[str] = []

    persona_profile = str(
        kwargs.get("chat_persona_profile", PERSONA_PROFILE_YANG_YUJING)
    ).strip().lower()
    if persona_profile not in PERSONA_PROFILES:
        errors.append(
            "CHAT_PERSONA_PROFILE must be one of "
            + ", ".join(PERSONA_PROFILES)
            + f", got {persona_profile or '<empty>'}"
        )
    if (
        persona_profile == "yang_yujing"
        and not str(kwargs.get("yang_yujing_group_name", DEFAULT_YANG_YUJING_GROUP_NAME)).strip()
    ):
        errors.append("YANG_YUJING_GROUP_NAME must not be empty for yang_yujing")

    # poll_interval_sec
    poll_interval_sec = kwargs.get("poll_interval_sec", 1.0)
    if poll_interval_sec < 0.1:
        errors.append(
            f"POLL_INTERVAL_SEC must be >= 0.1, got {poll_interval_sec}"
        )

    # chunk_size
    chunk_size = kwargs.get("chunk_size", 400)
    if not (10 <= chunk_size <= 1000):
        errors.append(
            f"CHUNK_SIZE must be between 10 and 1000, got {chunk_size}"
        )

    # max_messages_for_summary
    max_messages_for_summary = kwargs.get("max_messages_for_summary", 5000)
    if max_messages_for_summary < 10:
        errors.append(
            f"MAX_MESSAGES_FOR_SUMMARY must be >= 10, got {max_messages_for_summary}"
        )

    # fallback_window_hours
    fallback_window_hours = kwargs.get("fallback_window_hours", 8)
    if fallback_window_hours < 1:
        errors.append(
            f"FALLBACK_WINDOW_HOURS must be >= 1, got {fallback_window_hours}"
        )

    # dedup_window_sec
    dedup_window_sec = kwargs.get("dedup_window_sec", 60)
    if dedup_window_sec < 10:
        errors.append(
            f"DEDUP_WINDOW_SEC must be >= 10, got {dedup_window_sec}"
        )

    # max_retries is optional; validate only when explicitly provided.
    max_retries = kwargs.get("max_retries")
    if max_retries is not None and not (1 <= max_retries <= 10):
        errors.append(
            f"MAX_RETRIES must be between 1 and 10, got {max_retries}"
        )

    # sticky_mention_ttl_sec
    sticky_mention_ttl_sec = kwargs.get("sticky_mention_ttl_sec", 60)
    if not (10 <= sticky_mention_ttl_sec <= 300):
        errors.append(
            f"STICKY_MENTION_TTL_SEC must be between 10 and 300, "
            f"got {sticky_mention_ttl_sec}"
        )

    followup_listen_ttl_sec = kwargs.get("followup_listen_ttl_sec", 60)
    if not (10 <= followup_listen_ttl_sec <= 300):
        errors.append(
            f"FOLLOWUP_LISTEN_TTL_SEC must be between 10 and 300, "
            f"got {followup_listen_ttl_sec}"
        )

    followup_listen_context_count = kwargs.get("followup_listen_context_count", 6)
    if not (1 <= followup_listen_context_count <= 20):
        errors.append(
            "FOLLOWUP_LISTEN_CONTEXT_COUNT must be between 1 and 20, "
            f"got {followup_listen_context_count}"
        )

    followup_listen_max_message_chars = kwargs.get("followup_listen_max_message_chars", 30)
    if not (1 <= followup_listen_max_message_chars <= 80):
        errors.append(
            "FOLLOWUP_LISTEN_MAX_MESSAGE_CHARS must be between 1 and 80, "
            f"got {followup_listen_max_message_chars}"
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

    web_search_max_results = kwargs.get("web_search_max_results", 4)
    if not (1 <= web_search_max_results <= 8):
        errors.append(
            "WEB_SEARCH_MAX_RESULTS must be between 1 and 8, "
            f"got {web_search_max_results}"
        )

    web_search_timeout_sec = kwargs.get("web_search_timeout_sec", 8.0)
    if not (1.0 <= web_search_timeout_sec <= 20.0):
        errors.append(
            "WEB_SEARCH_TIMEOUT_SEC must be between 1.0 and 20.0, "
            f"got {web_search_timeout_sec}"
        )

    web_search_reply_max_chars = kwargs.get("web_search_reply_max_chars", 120)
    if not (40 <= web_search_reply_max_chars <= 300):
        errors.append(
            "WEB_SEARCH_REPLY_MAX_CHARS must be between 40 and 300, "
            f"got {web_search_reply_max_chars}"
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

    image_vision_reply_max_chars = kwargs.get("image_vision_reply_max_chars", 80)
    if not (20 <= image_vision_reply_max_chars <= 200):
        errors.append(
            "IMAGE_VISION_REPLY_MAX_CHARS must be between 20 and 200, "
            f"got {image_vision_reply_max_chars}"
        )

    image_followup_ttl_sec = kwargs.get("image_followup_ttl_sec", 90)
    if not (15 <= image_followup_ttl_sec <= 300):
        errors.append(
            "IMAGE_FOLLOWUP_TTL_SEC must be between 15 and 300, "
            f"got {image_followup_ttl_sec}"
        )

    chat_max_chars = kwargs.get("chat_max_chars", DEFAULT_CHAT_MAX_CHARS)
    if not (8 <= chat_max_chars <= 200):
        errors.append(
            f"CHAT_MAX_CHARS must be between 8 and 200, got {chat_max_chars}"
        )

    chat_context_window_sec = kwargs.get("chat_context_window_sec", DEFAULT_CHAT_CONTEXT_WINDOW_SEC)
    if not (60 <= chat_context_window_sec <= 7200):
        errors.append(
            f"CHAT_CONTEXT_WINDOW_SEC must be between 60 and 7200, got {chat_context_window_sec}"
        )

    chat_context_count = kwargs.get("chat_context_count", DEFAULT_CHAT_CONTEXT_COUNT)
    if not (1 <= chat_context_count <= 120):
        errors.append(
            f"CHAT_CONTEXT_COUNT must be between 1 and 120, got {chat_context_count}"
        )

    # proactive_rate_window_sec
    proactive_rate_window_sec = kwargs.get("proactive_rate_window_sec", 120)
    if proactive_rate_window_sec < 30:
        errors.append(
            f"PROACTIVE_RATE_WINDOW_SEC must be >= 30, got {proactive_rate_window_sec}"
        )

    # proactive_rate thresholds: all > 0 and in strict ascending order
    quiet = kwargs.get("proactive_rate_quiet", 1.5)
    casual = kwargs.get("proactive_rate_casual", 4.0)
    lively = kwargs.get("proactive_rate_lively", 6.5)
    burst = kwargs.get("proactive_rate_burst", 8.5)

    rate_names = ("quiet", "casual", "lively", "burst")
    rate_values = (quiet, casual, lively, burst)

    if any(v <= 0 for v in rate_values):
        errors.append(
            "All PROACTIVE_RATE_* values must be > 0, got: "
            + ", ".join(f"{n}={v}" for n, v in zip(rate_names, rate_values))
        )

    if not (quiet < casual < lively < burst):
        errors.append(
            "PROACTIVE_RATE_* values must be in strict ascending order "
            "(quiet < casual < lively < burst), got: "
            + ", ".join(f"{n}={v}" for n, v in zip(rate_names, rate_values))
        )

    # feishu_export_mode
    feishu_export_mode = kwargs.get("feishu_export_mode", "knowledge")
    if feishu_export_mode not in ("knowledge", "spreadsheet", "bitable", "docx"):
        errors.append(
            "FEISHU_EXPORT_MODE must be one of knowledge, spreadsheet, bitable, docx, "
            f"got {feishu_export_mode}"
        )

    # feishu_export_window_hours
    feishu_export_window_hours = kwargs.get("feishu_export_window_hours", 8)
    if not (1 <= feishu_export_window_hours <= 168):
        errors.append(
            "FEISHU_EXPORT_WINDOW_HOURS must be between 1 and 168, "
            f"got {feishu_export_window_hours}"
        )

    feishu_auto_sync_min_messages = kwargs.get("feishu_auto_sync_min_messages", 20)
    if not (1 <= feishu_auto_sync_min_messages <= 500):
        errors.append(
            "FEISHU_AUTO_SYNC_MIN_MESSAGES must be between 1 and 500, "
            f"got {feishu_auto_sync_min_messages}"
        )

    feishu_auto_sync_cooldown_sec = kwargs.get("feishu_auto_sync_cooldown_sec", 1800)
    if not (60 <= feishu_auto_sync_cooldown_sec <= 86400):
        errors.append(
            "FEISHU_AUTO_SYNC_COOLDOWN_SEC must be between 60 and 86400, "
            f"got {feishu_auto_sync_cooldown_sec}"
        )

    decision_trace_content_chars = kwargs.get("decision_trace_content_chars", 120)
    if not (0 <= decision_trace_content_chars <= 1000):
        errors.append(
            "DECISION_TRACE_CONTENT_CHARS must be between 0 and 1000, "
            f"got {decision_trace_content_chars}"
        )

    pipeline_v2 = kwargs.get("pipeline_v2", "off")
    if pipeline_v2 not in ("off", "single", "two_step"):
        errors.append(
            f"PIPELINE_V2 must be one of off, single, two_step, got {pipeline_v2}"
        )

    for send_setting in ("send_bubbles", "send_typing_delay", "send_quiet", "send_keep_clipboard",
                         "wechat_emoji", "send_stickers", "send_queue", "pipeline_rewrite_long"):
        if kwargs.get(send_setting, "off") not in ("off", "on"):
            errors.append(f"{send_setting.upper()} must be off or on, got {kwargs.get(send_setting)}")
    send_idle_sec = kwargs.get("send_idle_sec", 3.0)
    if not (0 <= send_idle_sec <= 60):
        errors.append(f"SEND_IDLE_SEC must be between 0 and 60, got {send_idle_sec}")
    model_route = kwargs.get("model_route", "cloud")
    if model_route not in ("cloud", "local", "split"):
        errors.append(f"MODEL_ROUTE must be cloud, local or split, got {model_route}")
    elif model_route != "cloud":
        if not kwargs.get("local_model"):
            errors.append(f"MODEL_ROUTE={model_route} needs LOCAL_MODEL (the model name on your local server)")
        if not str(kwargs.get("local_base_url", "")).startswith(("http://", "https://")):
            errors.append("LOCAL_BASE_URL must start with http:// or https://")
    local_tasks = kwargs.get("local_tasks", ["memory"])
    bad_tasks = [t for t in local_tasks if t not in ("chat", "proactive", "memory", "decide")]
    if bad_tasks:
        errors.append(f"LOCAL_TASKS may only name chat, proactive, memory, decide; got {', '.join(bad_tasks)}")
    if model_route == "split" and "decide" in local_tasks and kwargs.get("pipeline_v2", "off") != "two_step":
        errors.append("LOCAL_TASKS=decide hands the \"should I speak?\" step of PIPELINE_V2=two_step "
                      "to the local model; set PIPELINE_V2=two_step")
    cloud_fallback = kwargs.get("cloud_fallback", "off")
    if cloud_fallback not in ("off", "local"):
        errors.append(f"CLOUD_FALLBACK must be off or local, got {cloud_fallback}")
    elif cloud_fallback == "local" and not kwargs.get("local_model"):
        errors.append("CLOUD_FALLBACK=local needs LOCAL_MODEL (the model name on your local server)")
    if not (5 <= kwargs.get("local_timeout_sec", 90.0) <= 600):
        errors.append(f"LOCAL_TIMEOUT_SEC must be between 5 and 600, got {kwargs.get('local_timeout_sec')}")
    if not (256 <= kwargs.get("local_max_tokens", 2048) <= 32768):
        errors.append(f"LOCAL_MAX_TOKENS must be between 256 and 32768, got {kwargs.get('local_max_tokens')}")
    if kwargs.get("local_prompt", "standard") not in ("standard", "compact"):
        errors.append(f"LOCAL_PROMPT must be standard or compact, got {kwargs.get('local_prompt')}")
    if not isinstance(kwargs.get("local_extra_body", {}), dict):
        errors.append("LOCAL_EXTRA_BODY must be a JSON object, e.g. {\"chat_template_kwargs\": {\"enable_thinking\": false}}")
    persona_examples = kwargs.get("persona_examples", 4)
    if not (0 <= persona_examples <= 8):
        errors.append(f"PERSONA_EXAMPLES must be between 0 and 8, got {persona_examples}")
    sticker_min_interval_sec = kwargs.get("sticker_min_interval_sec", 600)
    if not (0 <= sticker_min_interval_sec <= 86400):
        errors.append(
            f"STICKER_MIN_INTERVAL_SEC must be between 0 and 86400, got {sticker_min_interval_sec}"
        )
    if kwargs.get("sticker_paste", "file") not in ("file", "image"):
        errors.append(f"STICKER_PASTE must be file or image, got {kwargs.get('sticker_paste')}")
    send_idle_max_wait_sec = kwargs.get("send_idle_max_wait_sec", 30.0)
    if not (0 <= send_idle_max_wait_sec <= 600):
        errors.append(
            f"SEND_IDLE_MAX_WAIT_SEC must be between 0 and 600, got {send_idle_max_wait_sec}"
        )

    followup_mode = kwargs.get("followup_mode", "heuristic")
    if followup_mode not in ("heuristic", "declared"):
        errors.append(f"FOLLOWUP_MODE must be heuristic or declared, got {followup_mode}")

    memory_v2 = kwargs.get("memory_v2", "off")
    if memory_v2 not in ("off", "on"):
        errors.append(f"MEMORY_V2 must be off or on, got {memory_v2}")
    memory_embeddings = kwargs.get("memory_embeddings", "off")
    if memory_embeddings not in ("off", "on"):
        errors.append(f"MEMORY_EMBEDDINGS must be off or on, got {memory_embeddings}")
    elif memory_embeddings == "on" and not (
        kwargs.get("embedding_model") and (kwargs.get("embedding_api_key") or kwargs.get("embedding_base_url"))
    ):
        errors.append("MEMORY_EMBEDDINGS=on needs EMBEDDING_MODEL and EMBEDDING_API_KEY "
                      "(or EMBEDDING_BASE_URL alone for a local server without a key)")
    memory_embedding_min_sim = kwargs.get("memory_embedding_min_sim", 0.55)
    if not (0.0 <= memory_embedding_min_sim < 1.0):
        errors.append(f"MEMORY_EMBEDDING_MIN_SIM must be from 0 to below 1, got {memory_embedding_min_sim}")
    memory_window_messages = kwargs.get("memory_window_messages", 40)
    if not (5 <= memory_window_messages <= 500):
        errors.append(
            f"MEMORY_WINDOW_MESSAGES must be between 5 and 500, got {memory_window_messages}"
        )
    memory_idle_minutes = kwargs.get("memory_idle_minutes", 20)
    if not (1 <= memory_idle_minutes <= 1440):
        errors.append(
            f"MEMORY_IDLE_MINUTES must be between 1 and 1440, got {memory_idle_minutes}"
        )
    memory_min_confidence = kwargs.get("memory_min_confidence", 0.6)
    if not (0.0 <= memory_min_confidence <= 1.0):
        errors.append(
            f"MEMORY_MIN_CONFIDENCE must be between 0 and 1, got {memory_min_confidence}"
        )
    memory_prompt_facts = kwargs.get("memory_prompt_facts", 12)
    if not (1 <= memory_prompt_facts <= 40):
        errors.append(
            f"MEMORY_PROMPT_FACTS must be between 1 and 40, got {memory_prompt_facts}"
        )

    proactive_gate_mode = kwargs.get("proactive_gate_mode", "dice")
    if proactive_gate_mode not in ("dice", "score"):
        errors.append(
            f"PROACTIVE_GATE_MODE must be dice or score, got {proactive_gate_mode}"
        )

    proactive_hourly_budget = kwargs.get("proactive_hourly_budget", 6)
    if not (0 <= proactive_hourly_budget <= 120):
        errors.append(
            "PROACTIVE_HOURLY_BUDGET must be between 0 and 120, "
            f"got {proactive_hourly_budget}"
        )

    proactive_score_threshold = kwargs.get("proactive_score_threshold", 0.35)
    if not (0.05 <= proactive_score_threshold <= 0.95):
        errors.append(
            "PROACTIVE_SCORE_THRESHOLD must be between 0.05 and 0.95, "
            f"got {proactive_score_threshold}"
        )

    claude_relay = kwargs.get("claude_relay", "off")
    if claude_relay not in ("off", "on"):
        errors.append(f"CLAUDE_RELAY must be off or on, got {claude_relay}")
    elif claude_relay == "on":
        # Anyone who can reach the port and knows the token can make the Mac run things.
        if len(str(kwargs.get("claude_relay_token", ""))) < 16:
            errors.append("CLAUDE_RELAY=on needs CLAUDE_RELAY_TOKEN (at least 16 characters, random)")
        if not (kwargs.get("claude_relay_wxids") or kwargs.get("admin_wxid")):
            errors.append("CLAUDE_RELAY=on needs CLAUDE_RELAY_WXIDS or ADMIN_WXID (who may use it)")
        if not re.fullmatch(r"[^\s]{1,10}", str(kwargs.get("claude_relay_prefix", ""))):
            errors.append("CLAUDE_RELAY_PREFIX must be 1-10 characters without spaces")
        if not (1 <= int(kwargs.get("claude_relay_port", 8790)) <= 65535):
            errors.append(f"CLAUDE_RELAY_PORT must be between 1 and 65535, got {kwargs.get('claude_relay_port')}")
    if not (100 <= int(kwargs.get("claude_relay_max_chars", 1500)) <= 5000):
        errors.append(
            f"CLAUDE_RELAY_MAX_CHARS must be between 100 and 5000, got {kwargs.get('claude_relay_max_chars')}"
        )

    if errors:
        msg = "配置值无效:\n" + "\n".join(f"  - {err}" for err in errors)
        raise RuntimeError(msg)


_PIPELINE_ALIASES = {
    "": "off", "off": "off", "false": "off", "0": "off", "no": "off", "none": "off",
    "single": "single", "true": "single", "1": "single", "on": "single", "yes": "single",
    "two_step": "two_step", "two-step": "two_step", "twostep": "two_step", "two": "two_step",
}


def _parse_pipeline_mode(raw: str) -> str:
    """PIPELINE_V2 accepts off/single/two_step and the usual yes/no spellings."""
    value = str(raw or "").strip().lower()
    return _PIPELINE_ALIASES.get(value, value)


def _parse_memory_mode(raw: str) -> str:
    """MEMORY_V2 accepts off/on and the usual yes/no spellings."""
    value = str(raw or "").strip().lower()
    if value in ("on", "true", "1", "yes", "y"):
        return "on"
    if value in ("", "off", "false", "0", "no", "n", "none"):
        return "off"
    return value


def _parse_json_object(raw: str):
    """A JSON object from the environment; anything else is returned for validation to reject."""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


def _split_names(raw: str) -> list[str]:
    return [
        name.strip() for name in re.split(r"[,，]", str(raw or "")) if name.strip()
    ]


def load_config() -> BotConfig:
    """Load configuration from environment variables.

    Returns a validated BotConfig instance.
    Raises RuntimeError if required configuration is missing.
    """
    ai_backend = os.getenv("AI_BACKEND", "claude").strip().lower()
    model_route = os.getenv("MODEL_ROUTE", "cloud").strip().lower() or "cloud"

    # Validate required API keys based on selected backend (not needed when
    # every task runs on the local model: MODEL_ROUTE=local)
    if model_route == "local":
        pass
    elif ai_backend == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key or "***" in api_key:
            msg = (
                "DEEPSEEK_API_KEY 未设置，或当前值是被打码后的占位；"
                "请在 .env 文件中配置完整 API Key，或通过引导页重新填写"
            )
            raise RuntimeError(msg)
    elif ai_backend == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key or "***" in api_key:
            msg = (
                "OPENAI_API_KEY 未设置，或当前值是被打码后的占位；"
                "请在 .env 文件中配置完整 API Key，或通过引导页重新填写"
            )
            raise RuntimeError(msg)
    elif ai_backend == "claude":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key or "***" in api_key:
            msg = (
                "ANTHROPIC_API_KEY 未设置，或当前值是被打码后的占位；"
                "请在 .env 文件中配置完整 API Key，或通过引导页重新填写"
            )
            raise RuntimeError(msg)
    else:
        raise RuntimeError(
            f"AI_BACKEND 不支持: {ai_backend}；可选值: claude, deepseek, openai"
        )

    # Parse trigger keywords from comma-separated string
    keywords_str = os.getenv("TRIGGER_KEYWORDS", "").strip()
    trigger_keywords = (
        [kw.strip() for kw in keywords_str.split(",") if kw.strip()]
        if keywords_str
        else None  # let the dataclass default apply
    )
    feishu_keywords_str = os.getenv("FEISHU_EXPORT_TRIGGER_KEYWORDS", "").strip()
    feishu_export_trigger_keywords = (
        [kw.strip() for kw in feishu_keywords_str.split(",") if kw.strip()]
        if feishu_keywords_str
        else None
    )
    todo_add_kw_str = os.getenv("TODO_ADD_KEYWORDS", "").strip()
    todo_add_keywords = (
        [kw.strip() for kw in todo_add_kw_str.split(",") if kw.strip()]
        if todo_add_kw_str else None
    )
    todo_complete_kw_str = os.getenv("TODO_COMPLETE_KEYWORDS", "").strip()
    todo_complete_keywords = (
        [kw.strip() for kw in todo_complete_kw_str.split(",") if kw.strip()]
        if todo_complete_kw_str else None
    )
    todo_delete_kw_str = os.getenv("TODO_DELETE_KEYWORDS", "").strip()
    todo_delete_keywords = (
        [kw.strip() for kw in todo_delete_kw_str.split(",") if kw.strip()]
        if todo_delete_kw_str else None
    )

    kwargs: dict = {
        "ai_backend": ai_backend,
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", "").strip(),
        "anthropic_base_url": os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip(),
        "summarize_model": os.getenv("SUMMARIZE_MODEL", "claude-haiku-4-5-20251001").strip(),
        "anthropic_chat_model": (
            os.getenv("CLAUDE_CHAT_MODEL", "claude-haiku-4-5-20251001").strip()
            or "claude-haiku-4-5-20251001"
        ),
        "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY", "").strip(),
        "deepseek_base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        # deepseek_model handled conditionally below (dataclass default)
        "openai_api_key": os.getenv("OPENAI_API_KEY", "").strip(),
        "openai_base_url": os.getenv("OPENAI_BASE_URL", "").strip(),
        "openai_model": os.getenv("OPENAI_MODEL", "gpt-5.6-terra").strip(),
        "wechat_backend": os.getenv("WECHAT_BACKEND", "wcdb").strip(),
        "wechat_groups": _decode_wechat_groups(os.getenv("WECHAT_GROUPS", "*")),
        "private_chat_enabled": os.getenv("PRIVATE_CHAT_ENABLED", "false").strip().lower() == "true",
        "private_chats": _decode_wechat_groups(os.getenv("PRIVATE_CHATS", "*")),
        "wechat_data_dir": os.getenv("WECHAT_DATA_DIR", "").strip(),
        "bot_display_name": _sanitize_display_name(os.getenv("BOT_DISPLAY_NAME", "群聊小助手")),
        "admin_wxid": os.getenv("ADMIN_WXID", "").strip(),
        "chat_persona_profile": os.getenv(
            "CHAT_PERSONA_PROFILE", PERSONA_PROFILE_YANG_YUJING,
        ).strip().lower(),
        "chat_persona_name": _sanitize_display_name(os.getenv("CHAT_PERSONA_NAME", DEFAULT_CHAT_PERSONA_NAME)),
        "chat_persona_prompt": _decode_env_text(os.getenv("CHAT_PERSONA_PROMPT"), DEFAULT_CHAT_PERSONA_PROMPT),
        "chat_reply_rules": _decode_env_text(os.getenv("CHAT_REPLY_RULES"), DEFAULT_CHAT_REPLY_RULES),
        "chat_max_chars": int(os.getenv("CHAT_MAX_CHARS", str(DEFAULT_CHAT_MAX_CHARS))),
        "chat_context_window_sec": int(os.getenv("CHAT_CONTEXT_WINDOW_SEC", str(DEFAULT_CHAT_CONTEXT_WINDOW_SEC))),
        "chat_context_count": int(os.getenv("CHAT_CONTEXT_COUNT", str(DEFAULT_CHAT_CONTEXT_COUNT))),
        "yang_yujing_group_name": os.getenv(
            "YANG_YUJING_GROUP_NAME", DEFAULT_YANG_YUJING_GROUP_NAME,
        ).strip(),
        "yang_yujing_always_reply_group_name": os.getenv(
            "YANG_YUJING_ALWAYS_REPLY_GROUP_NAME",
            DEFAULT_YANG_YUJING_ALWAYS_REPLY_GROUP_NAME,
        ).strip(),
        "db_path": os.getenv("DB_PATH", "data/messages.db").strip(),
        "poll_interval_sec": float(os.getenv("POLL_INTERVAL_SEC", "1.0")),
        "dedup_window_sec": int(os.getenv("DEDUP_WINDOW_SEC", "60")),
        "max_messages_for_summary": int(os.getenv("MAX_MESSAGES_FOR_SUMMARY", "5000")),
        "chunk_size": int(os.getenv("CHUNK_SIZE", "400")),
        "fallback_window_hours": int(os.getenv("FALLBACK_WINDOW_HOURS", "8")),
        "feishu_export_enabled": os.getenv("FEISHU_EXPORT_ENABLED", "false").strip().lower() == "true",
        "feishu_app_id": os.getenv("FEISHU_APP_ID", "").strip(),
        "feishu_app_secret": os.getenv("FEISHU_APP_SECRET", "").strip(),
        "feishu_export_mode": os.getenv("FEISHU_EXPORT_MODE", "knowledge").strip().lower(),
        "feishu_export_window_hours": int(os.getenv("FEISHU_EXPORT_WINDOW_HOURS", "8")),
        "feishu_auto_sync_enabled": os.getenv("FEISHU_AUTO_SYNC_ENABLED", "false").strip().lower() == "true",
        "feishu_auto_sync_min_messages": int(os.getenv("FEISHU_AUTO_SYNC_MIN_MESSAGES", "20")),
        "feishu_auto_sync_cooldown_sec": int(os.getenv("FEISHU_AUTO_SYNC_COOLDOWN_SEC", "1800")),
        "feishu_knowledge_base_name": os.getenv("FEISHU_KNOWLEDGE_BASE_NAME", "webot 群聊沉淀").strip(),
        "feishu_knowledge_folder_token": os.getenv("FEISHU_KNOWLEDGE_FOLDER_TOKEN", "").strip(),
        "feishu_spreadsheet_token": os.getenv("FEISHU_SPREADSHEET_TOKEN", "").strip(),
        "feishu_spreadsheet_range": os.getenv("FEISHU_SPREADSHEET_RANGE", "Sheet1!A:H").strip(),
        "feishu_bitable_app_token": os.getenv("FEISHU_BITABLE_APP_TOKEN", "").strip(),
        "feishu_bitable_table_id": os.getenv("FEISHU_BITABLE_TABLE_ID", "").strip(),
        "feishu_doc_folder_token": os.getenv("FEISHU_DOC_FOLDER_TOKEN", "").strip(),
        "fun_enabled": os.getenv("FUN_ENABLED", "true").strip().lower() == "true",
        "summarize_enabled": os.getenv("SUMMARIZE_ENABLED", "true").strip().lower() == "true",
        "proactive_enabled": os.getenv("PROACTIVE_ENABLED", "false").strip().lower() == "true",
        # proactive_rate_window_sec handled conditionally below (dataclass default)
        "proactive_rate_quiet": float(os.getenv("PROACTIVE_RATE_QUIET", "1.5")),
        "proactive_rate_casual": float(os.getenv("PROACTIVE_RATE_CASUAL", "4.0")),
        "proactive_rate_lively": float(os.getenv("PROACTIVE_RATE_LIVELY", "6.5")),
        "proactive_rate_burst": float(os.getenv("PROACTIVE_RATE_BURST", "8.5")),
        "welcome_enabled": os.getenv("WELCOME_ENABLED", "false").strip().lower() == "true",
        "sticky_mention_enabled": os.getenv("STICKY_MENTION_ENABLED", "true").strip().lower() == "true",
        "sticky_mention_ttl_sec": int(os.getenv("STICKY_MENTION_TTL_SEC", "60")),
        "followup_listen_enabled": os.getenv("FOLLOWUP_LISTEN_ENABLED", "true").strip().lower() == "true",
        "followup_listen_ttl_sec": int(os.getenv("FOLLOWUP_LISTEN_TTL_SEC", "60")),
        "followup_listen_context_count": int(os.getenv("FOLLOWUP_LISTEN_CONTEXT_COUNT", "6")),
        "followup_listen_max_message_chars": int(os.getenv("FOLLOWUP_LISTEN_MAX_MESSAGE_CHARS", "30")),
        "message_burst_enabled": os.getenv("MESSAGE_BURST_ENABLED", "true").strip().lower() == "true",
        "message_burst_window_sec": float(os.getenv("MESSAGE_BURST_WINDOW_SEC", "2.2")),
        "message_burst_max_messages": int(os.getenv("MESSAGE_BURST_MAX_MESSAGES", "5")),
        "message_burst_max_chars": int(os.getenv("MESSAGE_BURST_MAX_CHARS", "200")),
        "web_search_enabled": os.getenv("WEB_SEARCH_ENABLED", "true").strip().lower() == "true",
        "web_search_max_results": int(os.getenv("WEB_SEARCH_MAX_RESULTS", "4")),
        "web_search_timeout_sec": float(os.getenv("WEB_SEARCH_TIMEOUT_SEC", "8.0")),
        "web_search_region": os.getenv("WEB_SEARCH_REGION", "cn-zh").strip() or "cn-zh",
        "web_search_reply_max_chars": int(os.getenv("WEB_SEARCH_REPLY_MAX_CHARS", "120")),
        "image_vision_enabled": os.getenv("IMAGE_VISION_ENABLED", "true").strip().lower() == "true",
        "image_vision_lookback_sec": int(os.getenv("IMAGE_VISION_LOOKBACK_SEC", "900")),
        "image_vision_max_bytes": int(os.getenv("IMAGE_VISION_MAX_BYTES", str(5 * 1024 * 1024))),
        "image_vision_reply_max_chars": int(os.getenv("IMAGE_VISION_REPLY_MAX_CHARS", "80")),
        "image_followup_enabled": os.getenv("IMAGE_FOLLOWUP_ENABLED", "true").strip().lower() == "true",
        "image_followup_ttl_sec": int(os.getenv("IMAGE_FOLLOWUP_TTL_SEC", "90")),
        "image_screen_fallback_enabled": os.getenv("IMAGE_SCREEN_FALLBACK_ENABLED", "true").strip().lower() == "true",
        # Group Todo
        "todo_enabled": os.getenv("TODO_ENABLED", "true").strip().lower() == "true",
        "todo_groups": [g.strip() for g in os.getenv("TODO_GROUPS", "*").split(",") if g.strip()],
        "todo_max_per_group": int(os.getenv("TODO_MAX_PER_GROUP", "50")),
        "todo_completed_retention_days": int(os.getenv("TODO_COMPLETED_RETENTION_DAYS", "30")),
        "todo_deleted_retention_days": int(os.getenv("TODO_DELETED_RETENTION_DAYS", "30")),
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
        # Observability (decision trace + model-call log)
        "decision_trace_enabled": os.getenv("DECISION_TRACE_ENABLED", "true").strip().lower() == "true",
        "decision_trace_path": os.getenv("DECISION_TRACE_PATH", "data/decision_trace.jsonl").strip() or "data/decision_trace.jsonl",
        "decision_trace_content_chars": int(os.getenv("DECISION_TRACE_CONTENT_CHARS", "120")),
        "llm_call_log_enabled": os.getenv("LLM_CALL_LOG_ENABLED", "true").strip().lower() == "true",
        "llm_call_log_path": os.getenv("LLM_CALL_LOG_PATH", "data/llm_calls.jsonl").strip() or "data/llm_calls.jsonl",
        "llm_call_log_prompts": os.getenv("LLM_CALL_LOG_PROMPTS", "true").strip().lower() == "true",
        # Structured pipeline
        "pipeline_v2": _parse_pipeline_mode(os.getenv("PIPELINE_V2", "off")),
        "pipeline_repair": os.getenv("PIPELINE_REPAIR", "true").strip().lower() == "true",
        "pipeline_json_mode": os.getenv("PIPELINE_JSON_MODE", "true").strip().lower() == "true",
        "pipeline_rewrite_long": _parse_memory_mode(os.getenv("PIPELINE_REWRITE_LONG", "off")),
        "bot_aliases": _split_names(os.getenv("BOT_ALIASES", "")),
        "pipeline_v2_groups": _split_names(os.getenv("PIPELINE_V2_GROUPS", "*")) or ["*"],
        "proactive_gate_mode": os.getenv("PROACTIVE_GATE_MODE", "dice").strip().lower(),
        "proactive_hourly_budget": int(os.getenv("PROACTIVE_HOURLY_BUDGET", "6")),
        "proactive_score_threshold": float(os.getenv("PROACTIVE_SCORE_THRESHOLD", "0.35")),
        "followup_mode": os.getenv("FOLLOWUP_MODE", "heuristic").strip().lower() or "heuristic",
        "send_bubbles": _parse_memory_mode(os.getenv("SEND_BUBBLES", "off")),
        "send_typing_delay": _parse_memory_mode(os.getenv("SEND_TYPING_DELAY", "off")),
        "send_quiet": _parse_memory_mode(os.getenv("SEND_QUIET", "off")),
        "send_idle_sec": float(os.getenv("SEND_IDLE_SEC", "3")),
        "send_idle_max_wait_sec": float(os.getenv("SEND_IDLE_MAX_WAIT_SEC", "30")),
        "send_keep_clipboard": _parse_memory_mode(os.getenv("SEND_KEEP_CLIPBOARD", "on")),
        "wechat_emoji": _parse_memory_mode(os.getenv("WECHAT_EMOJI", "off")),
        "wechat_emoji_codes": list(parse_wechat_emoji_codes(os.getenv("WECHAT_EMOJI_CODES", ""))),
        "send_stickers": _parse_memory_mode(os.getenv("SEND_STICKERS", "off")),
        "sticker_dir": os.getenv("STICKER_DIR", "data/stickers").strip() or "data/stickers",
        "sticker_min_interval_sec": int(os.getenv("STICKER_MIN_INTERVAL_SEC", "600")),
        "sticker_paste": os.getenv("STICKER_PASTE", "file").strip().lower() or "file",
        "send_queue": _parse_memory_mode(os.getenv("SEND_QUEUE", "off")),
        "model_route": model_route,
        "local_tasks": [t.lower() for t in _split_names(os.getenv("LOCAL_TASKS", "memory"))],
        "local_base_url": os.getenv("LOCAL_BASE_URL", "http://127.0.0.1:11434/v1").strip().rstrip("/"),
        "local_model": os.getenv("LOCAL_MODEL", "").strip(),
        "local_api_key": os.getenv("LOCAL_API_KEY", "").strip(),
        "local_timeout_sec": float(os.getenv("LOCAL_TIMEOUT_SEC", "90")),
        "local_max_tokens": int(os.getenv("LOCAL_MAX_TOKENS", "2048")),
        "local_extra_body": _parse_json_object(
            os.getenv("LOCAL_EXTRA_BODY", '{"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": false}}')
        ),
        "local_prompt": os.getenv("LOCAL_PROMPT", "standard").strip().lower() or "standard",
        "cloud_fallback": os.getenv("CLOUD_FALLBACK", "off").strip().lower() or "off",
        "persona_examples": int(os.getenv("PERSONA_EXAMPLES", "4")),
        "persona_examples_dir": os.getenv("PERSONA_EXAMPLES_DIR", "data/examples").strip() or "data/examples",
        # Memory v2
        "memory_v2": _parse_memory_mode(os.getenv("MEMORY_V2", "off")),
        "memory_v2_groups": _split_names(os.getenv("MEMORY_V2_GROUPS", "*")) or ["*"],
        "memory_embeddings": _parse_memory_mode(os.getenv("MEMORY_EMBEDDINGS", "off")),
        "embedding_base_url": os.getenv("EMBEDDING_BASE_URL", "").strip(),
        "embedding_api_key": os.getenv("EMBEDDING_API_KEY", "").strip(),
        "embedding_model": os.getenv("EMBEDDING_MODEL", "").strip(),
        "memory_embedding_min_sim": float(os.getenv("MEMORY_EMBEDDING_MIN_SIM", "0.55")),
        "memory_window_messages": int(os.getenv("MEMORY_WINDOW_MESSAGES", "40")),
        "memory_idle_minutes": int(os.getenv("MEMORY_IDLE_MINUTES", "20")),
        "memory_min_confidence": float(os.getenv("MEMORY_MIN_CONFIDENCE", "0.6")),
        "memory_prompt_facts": int(os.getenv("MEMORY_PROMPT_FACTS", "12")),
        "claude_relay": _parse_memory_mode(os.getenv("CLAUDE_RELAY", "off")),
        "claude_relay_token": os.getenv("CLAUDE_RELAY_TOKEN", "").strip(),
        "claude_relay_host": os.getenv("CLAUDE_RELAY_HOST", "0.0.0.0").strip() or "0.0.0.0",
        "claude_relay_port": int(os.getenv("CLAUDE_RELAY_PORT", "8790")),
        "claude_relay_prefix": os.getenv("CLAUDE_RELAY_PREFIX", "cc").strip(),
        "claude_relay_wxids": _split_names(os.getenv("CLAUDE_RELAY_WXIDS", "")),
        "claude_relay_db": os.getenv("CLAUDE_RELAY_DB", "data/claude_relay.db").strip() or "data/claude_relay.db",
        "claude_relay_max_chars": int(os.getenv("CLAUDE_RELAY_MAX_CHARS", "1500")),
    }

    deepseek_model = os.getenv("DEEPSEEK_MODEL")
    if deepseek_model is not None:
        kwargs["deepseek_model"] = deepseek_model.strip()

    proactive_rate_window_sec = os.getenv("PROACTIVE_RATE_WINDOW_SEC")
    if proactive_rate_window_sec is not None:
        kwargs["proactive_rate_window_sec"] = int(proactive_rate_window_sec)

    if trigger_keywords is not None:
        kwargs["trigger_keywords"] = trigger_keywords
    if feishu_export_trigger_keywords is not None:
        kwargs["feishu_export_trigger_keywords"] = feishu_export_trigger_keywords
    if todo_add_keywords is not None:
        kwargs["todo_add_keywords"] = todo_add_keywords
    if todo_complete_keywords is not None:
        kwargs["todo_complete_keywords"] = todo_complete_keywords
    if todo_delete_keywords is not None:
        kwargs["todo_delete_keywords"] = todo_delete_keywords

    # Yang Yujing is a fixed preset learned from the supplied group history.
    # Keep the custom persona's editable prompt values untouched, so
    # switching back restores its current behavior and customizations.
    profile_defaults = persona_defaults_for_profile(kwargs["chat_persona_profile"])
    if profile_defaults is not None:
        # The full-feed preset has a fixed group identity.  Keep the custom
        # persona's display name untouched when switching back, but make all
        # runtime mention parsing and prompt identity agree in Yang mode.
        kwargs["bot_display_name"] = profile_defaults["name"]
        kwargs["chat_persona_name"] = profile_defaults["name"]
        kwargs["chat_persona_prompt"] = profile_defaults["prompt"]
        kwargs["chat_reply_rules"] = profile_defaults["reply_rules"]
        kwargs["chat_max_chars"] = profile_defaults["max_chars"]
        kwargs["chat_context_window_sec"] = profile_defaults["context_window_sec"]
        kwargs["chat_context_count"] = profile_defaults["context_count"]

    _validate_config(kwargs)

    return BotConfig(**kwargs)


def is_onboarding_done() -> bool:
    """Check if onboarding has been completed without loading full config.

    Uses find_env_file() plus the optional machine-local override. In VM
    deployments, WCDB_KEY can live in .env.local while shared .env keeps the
    common persona/API settings.
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
