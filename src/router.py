"""Message router — receives standardized messages and dispatches to handlers.

Routes messages to three response modes (the ordered steps are in ``src/routing/steps.py``):
1. Admin commands: @bot mention + admin wxid → AdminCommandHandler
2. AI chat: @bot mention or trigger keyword → summarizer.chat()
3. Proactive chat: ambient participation via rate-based gating → summarizer.proactive_chat()
"""

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .proactive.gate import ProactiveGate
from .proactive.followup import FollowupListenTracker
from .proactive.image_followup import ImageFollowupTracker
from .proactive.sticky import StickyMentionTracker
from .memory.facts import GroupFactExtractor
from .memory.consolidator import MemoryConsolidator
from .memory.context import NO_RELEVANT_MEMORY, MemoryContext, build_memory_context
from .memory.store import MemoryStore
from .memory.worker import MemoryWorker
from .memory.embeddings import VectorIndex, embedder_from_config
from .todo.store import TodoStore
from .todo.handler import TodoHandler
from .web_search import WebSearchClient
from .vision import IMAGE_MSG_TYPES, find_recent_image_path, is_image_question
from .persona import (
    PERSONA_PROFILE_YANG_YUJING, FullFeed, Persona, PersonaRegistry, builtin_personas,
)
from .decision_trace import DecisionTracer
from .pipeline.addressing import (
    LEVEL_STRONG,
    LEVEL_WEAK,
    SIGNAL_PRIVATE,
    Addressing,
    bot_names,
    classify,
    clean_content as pipeline_clean_content,
)
from .pipeline.recent import RecentReplyLog
from .routing.context import (  # noqa: F401 - re-exported: tests and tools import them from here
    ALL_MESSAGES_MAX_AGE_SEC, AT_MENTION_MAX_AGE_SEC, WELCOME_MAX_AGE_SEC,
)
from .routing.steps import ROUTE_STEPS, run_route, run_fun_command
from .send.bubbles import BUBBLE_SEP, join_bubbles, plain_text, split_bubbles, strip_separators
from .send.stickers import STICKER_MARK, get_stickers, pop_sticker_mentions, sticker_in, sticker_token
from .send.wechat_emoji import DEFAULT_CODES as DEFAULT_WECHAT_EMOJI, keep_known
from .persona_examples import ExampleLibrary, render_examples
from .pipeline.types import STRATEGIES, STRATEGY_OFF, ChatRequest, Decision

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────
CHAT_CONTEXT_WINDOW_SEC = 600      # fetch last N seconds of chat as context for @mentions
MAX_CONTENT_LENGTH = 997           # max chars per message sent to AI (997 + "..." = 1000)
MAX_CONTENT_LINES = 20             # max context lines fed to AI chat prompt

# Markdown patterns to strip before sending to WeChat.
# These regexes may miss edge cases like nested formatting or asterisks at
# line boundaries.  AI output typically uses simple bold/italic/code/
# strikethrough — these patterns handle 99% of cases.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_STRIKE = re.compile(r"~~(.+?)~~")
_MD_CODE = re.compile(r"`(.+?)`")
_WECHAT_SENDER_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9_]+:\s*")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U0001FC00-\U0001FFFD"
    "\u2600-\u27BF"
    "\u2300-\u23FF"
    "\u2B00-\u2BFF"
    "\u200D\uFE0E\uFE0F"
    "]+",
)
_BRACKETED_KAOMOJI_RE = re.compile(
    r"[（(][^（）()\r\n]{0,16}[￣^▽∀ωಠಥಢ༎ರ皿ﾉヽง╯╰]"
    r"[^（）()\r\n]{0,16}[）)]",
)
_ASCII_KAOMOJI_RE = re.compile(
    r"(?<![\w\u4e00-\u9fff])(?:"
    r"[:;=8xX](?:[-'oO])?[)(DPp/\\|]+"
    r"|[)(DPp/\\|]+(?:[-'oO])?[:;=8xX]"
    r"|\^[_\-.]?\^|[TtQq][_.\-][TtQq]"
    r"|[oO][wW][oO]|[Qq][Aa][Qq]|[Qq][wW][Qq]|[xX][Dd]+"
    r")(?![\w\u4e00-\u9fff])",
)
_HISTORY_RECALL_RE = re.compile(
    r"之前|以前|上次|前面|早些|聊天记录|历史(?:消息|记录)?|原话|"
    r"还记得|记不记得|谁(?:说|讲|提|发)过?|(?:说|讲|提|发)过",
)
_HISTORY_QUERY_NOISE_RE = re.compile(
    r"之前|以前|上次|前面|早些|聊天记录|历史(?:消息|记录)?|原话|"
    r"还记得|记不记得|谁(?:说|讲|提|发)过?|(?:说|讲|提|发)过|"
    r"当时|是不是|是否|有没有|还有|这个|那个|那件事|这件事|"
    r"内容|消息|聊天|记录|(?:谁|哪|什么|怎么)|一下|吗|呢|啊|呀|的|了|是|在",
)
# The full-feed persona's pacing (cool-down, one-word reactions it stays out of,
# talk about the bot itself) is data: see "full_feed" in src/personas/*.json.
_BUILTIN_FEED = (
    getattr(builtin_personas().get(PERSONA_PROFILE_YANG_YUJING), "full_feed", None) or FullFeed()
)
_FEED_META_RE = re.compile(
    "|".join(re.escape(word) for word in _BUILTIN_FEED.skip_words) or r"(?!)", re.IGNORECASE,
)
_FEED_SHORT_REACTIONS = set(_BUILTIN_FEED.skip_reactions)
AMBIENT_COOLDOWN_SEC = _BUILTIN_FEED.ambient_cooldown_sec     # avoid replying like a bot to every new group turn


class MessageRouter:
    """Routes incoming WeChat messages to the correct handler.

    Usage:
        router = MessageRouter(
            store=message_store,
            detector=trigger_detector,
            summarizer=summarizer,
            admin_handler=admin_handler,
            nickname_service=nickname_service,
            config=bot_config,
        )

        def on_message(msg: dict) -> str | None:
            return router.handle(msg)
    """

    def __init__(self, store, detector, summarizer, admin_handler,
                 nickname_service, config, feishu_export_service=None,
                 web_search_client=None, tracer=None,
                 proactive_summarizer=None, memory_summarizer=None):
        """
        Args:
            store: MessageStore instance for persistence and queries.
            detector: TriggerDetector instance for keyword matching.
            summarizer: AbstractSummarizer instance for AI responses.
            admin_handler: AdminCommandHandler instance.
            nickname_service: NicknameService instance.
            config: BotConfig instance.
            feishu_export_service: Optional FeishuExportService instance.
            tracer: Optional DecisionTracer; built from ``config`` if omitted.
            proactive_summarizer / memory_summarizer: the models for proactive
                turns and for memory work when MODEL_ROUTE splits the tasks
                (``create_models``); both default to ``summarizer``.
        """
        self._store = store
        self._detector = detector
        self._summarizer = summarizer
        self._proactive_summarizer = proactive_summarizer or summarizer
        self._memory_summarizer = memory_summarizer or summarizer
        self._admin = admin_handler
        self._nicks = nickname_service
        self._config = config
        # Observe-only decision trace + model-call log (never alters results).
        self._tracer = (
            tracer if tracer is not None else DecisionTracer.from_config(config)
        )
        if getattr(self._tracer, "enabled", False):
            for model in {id(m): m for m in (summarizer, self._proactive_summarizer,
                                             self._memory_summarizer)}.values():
                attach_recorder = getattr(model, "set_call_recorder", None)
                if callable(attach_recorder):
                    attach_recorder(self._tracer.record_llm_call)
        self._feishu_export = feishu_export_service
        self._web_search = (
            web_search_client
            if web_search_client is not None
            else WebSearchClient.from_config(config)
        )
        self._proactive = ProactiveGate(config)
        self._fact_extractor = GroupFactExtractor(config.bot_display_name)
        self._sticky = StickyMentionTracker(
            ttl_sec=config.sticky_mention_ttl_sec,
        ) if config.sticky_mention_enabled else None
        self._followup = FollowupListenTracker(
            ttl_sec=config.followup_listen_ttl_sec,
            max_message_chars=config.followup_listen_max_message_chars,
        ) if config.followup_listen_enabled else None
        self._image_followup = ImageFollowupTracker(
            ttl_sec=getattr(config, "image_followup_ttl_sec", 90),
        ) if getattr(config, "image_followup_enabled", True) else None
        self._memory = MemoryConsolidator(store, self._memory_summarizer)
        # Structured pipeline (PIPELINE_V2).  "off" keeps the original path.
        strategy = getattr(config, "pipeline_v2", STRATEGY_OFF)
        self._pipeline_strategy = (
            strategy if isinstance(strategy, str) and strategy in STRATEGIES
            else STRATEGY_OFF
        )
        groups = getattr(config, "pipeline_v2_groups", None)
        self._pipeline_groups = tuple(
            str(g).strip().lower() for g in groups if str(g).strip()
        ) if isinstance(groups, (list, tuple)) and groups else ("*",)
        self._recent_replies = RecentReplyLog()
        self._bot_names = bot_names(
            getattr(config, "bot_display_name", ""),
            getattr(config, "chat_persona_name", ""),
            getattr(config, "bot_aliases", ()),
        )
        # Memory v2 (MEMORY_V2): off keeps the diary and the LIKE history search.
        self._memory_v2 = getattr(config, "memory_v2", "off") == "on"
        groups = getattr(config, "memory_v2_groups", None)
        self._memory_groups = tuple(
            str(g).strip().lower() for g in groups if str(g).strip()
        ) if isinstance(groups, (list, tuple)) and groups else ("*",)
        self._mstore: Optional[MemoryStore] = None
        self._memory_worker: Optional[MemoryWorker] = None
        self._replies_seeded: set[str] = set()
        # MEMORY_EMBEDDINGS=on: facts are also matched by meaning (src/memory/embeddings.py).
        self._vectors: Optional[VectorIndex] = None
        if self._memory_v2 and hasattr(store, "conn"):
            self._mstore = MemoryStore(store)
            embedder = embedder_from_config(config)
            if embedder is not None:
                self._vectors = VectorIndex(self._mstore, embedder)
            self._memory_worker = MemoryWorker(
                self._mstore, self._memory_summarizer,
                window_messages=getattr(config, "memory_window_messages", 40),
                idle_minutes=getattr(config, "memory_idle_minutes", 20),
                bot_name=getattr(config, "bot_display_name", "") or "机器人",
                bot_names=tuple(self._bot_names),
                vectors=self._vectors,
            )
            if self._memory_worker.usable:
                self._memory_worker.start()
            else:
                logger.warning("MEMORY_V2 is on but the AI backend cannot run JSON tasks")
        elif getattr(config, "memory_embeddings", "off") == "on":
            logger.warning("MEMORY_EMBEDDINGS only works with MEMORY_V2=on; it does nothing now")
        self._route_steps = ROUTE_STEPS
        # SEND_BUBBLES=on: the model's messages stay separate bubbles (src/send/bubbles.py).
        self._send_bubbles = getattr(config, "send_bubbles", "off") == "on"
        # WECHAT_EMOJI / SEND_STICKERS (structured pipeline only): WeChat's own
        # emoji codes and sticker pictures the model may use (src/send/).
        self._wechat_emoji: tuple = (
            tuple(getattr(config, "wechat_emoji_codes", None) or ()) or DEFAULT_WECHAT_EMOJI
            if getattr(config, "wechat_emoji", "off") == "on" else ()
        )
        # Only the Windows backend can send a picture; elsewhere none is offered.
        self._send_stickers = (
            getattr(config, "send_stickers", "off") == "on"
            and getattr(config, "wechat_backend", "wcdb") == "wcdb"
        )
        self._sticker_interval = float(getattr(config, "sticker_min_interval_sec", 600))
        self._sticker_lock = threading.Lock()
        self._sticker_last_at: dict[str, float] = {}
        # data/examples/<persona>.txt: real example dialogues shown to the model.
        self._example_count = int(getattr(config, "persona_examples", 4))
        self._examples = ExampleLibrary(getattr(config, "persona_examples_dir", "data/examples"))
        # FOLLOWUP_MODE=declared: the model says whom it waits for after a reply.
        self._declare_followups = (
            getattr(config, "followup_mode", "heuristic") == "declared"
            and self._followup is not None
        )
        # data/persona_map.json: a different persona per group.  Without the
        # file the registry is inactive and the configured persona speaks everywhere.
        self._personas = PersonaRegistry.from_files()
        self._ambient_lock = threading.Lock()
        self._ambient_next_turn_at: dict[str, float] = {}
        # Todo: init store and handler if feature is enabled
        self._todo_store: Optional[TodoStore] = None
        self._todo_handler: Optional[TodoHandler] = None
        if config.todo_enabled:
            self._todo_store = TodoStore(db_path=config.db_path)
            self._todo_handler = TodoHandler(
                self._todo_store, config,
            )
        # Health monitoring: count unique messages processed (post-dedup)
        self.messages_processed: int = 0

    # ── persona per chat ────────────────────────────────────────────────

    def _persona_for(self, msg: dict) -> Persona | None:
        """The persona ``data/persona_map.json`` assigns to this chat, if any."""
        return self._personas.persona_for(msg)

    def _persona_settings_for(self, msg: dict) -> dict | None:
        persona = self._persona_for(msg)
        return persona.settings() if persona is not None else None

    def _persona_kwargs(self, msg: dict) -> dict:
        """Extra arguments for the legacy chat calls: only present when a persona is mapped."""
        settings = self._persona_settings_for(msg)
        return {"persona_settings": settings} if settings is not None else {}

    def _names_for(self, msg: dict) -> tuple:
        """Names that count as calling the bot in this chat (a mapped persona adds its own)."""
        persona = self._persona_for(msg)
        if persona is None or persona.name in self._bot_names:
            return self._bot_names
        return tuple(self._bot_names) + (persona.name,)

    def close(self) -> None:
        """Stop the background memory job (call before the database is closed)."""
        if self._memory_worker is not None:
            self._memory_worker.stop()

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Remove markdown formatting characters that WeChat can't render."""
        text = _MD_BOLD.sub(r"\1", text)
        text = _MD_ITALIC.sub(r"\1", text)
        text = _MD_STRIKE.sub(r"\1", text)
        text = _MD_CODE.sub(r"\1", text)
        return text.strip()

    @staticmethod
    def _strip_emoticons(text: str) -> str:
        """Remove emoji and common kaomoji from every visible bot reply."""
        text = _EMOJI_RE.sub("", text)
        text = _BRACKETED_KAOMOJI_RE.sub("", text)
        text = _ASCII_KAOMOJI_RE.sub("", text)
        text = re.sub(r"\s{2,}", " ", text)
        return re.sub(r"\s+([，。！？、；：])", r"\1", text).strip()

    def handle(self, msg: dict) -> Optional[str]:
        """Process an incoming WeChat message.

        Returns reply text if a reply should be sent, or None.

        Thin wrapper around ``_handle``: it only records a decision trace
        (observe only — the result is returned unchanged).
        """
        trace = self._tracer.begin(msg)
        try:
            reply = self._handle(msg)
        except Exception as exc:
            trace.finish_error(exc)
            raise
        if reply and (BUBBLE_SEP in reply or STICKER_MARK in reply) and not msg.get("_bubbled"):
            # Only _decision_reply_text joins bubbles or adds a sticker; a mark from a
            # nickname, a command reply or echoed user text must not split a message.
            reply = strip_separators(reply).strip() or None
        trace.finish_reply(reply)
        return reply

    def _now(self) -> int:
        """Whole seconds on the router's clock (tests swap this module's ``time``)."""
        return int(time.time())

    def _handle(self, msg: dict) -> Optional[str]:
        """Route one message through the ordered steps of ``src/routing/steps.py``."""
        return run_route(self, msg, self._tracer.current(), self._route_steps)

    def _handle_private_chat(self, msg: dict) -> Optional[str]:
        """Private one-on-one chats reply directly without @mention."""
        clean_content = re.sub(
            r'^\s*wxid_[a-zA-Z0-9]+:\s*', '', msg.get("content", ""),
        ).strip()
        if not clean_content:
            return None

        msg["_private_triggered"] = True
        self._tracer.current().reason("private_chat")
        logger.info(
            "Private chat from '%s': %s",
            msg["sender_name"], clean_content[:80],
        )

        reply: Optional[str] = None

        if clean_content in ("帮助", "help", "命令"):
            self._tracer.current().reason("help_command")
            reply = self._admin.handle(clean_content, msg["sender_name"])

        if self._config.fun_enabled:
            matched, fun_reply = run_fun_command(
                self, msg, clean_content, self._tracer.current(),
            )
            if matched:
                reply = fun_reply

        if reply is None and (
            self._config.admin_wxid
            and msg["sender_id"] == self._config.admin_wxid
        ):
            self._tracer.current().reason("admin_command")
            reply = self._admin.handle(clean_content, msg["sender_name"])

        if reply is None:
            self._tracer.current().reason("chat_request")
            reply = self._handle_chat(msg, clean_content)

        return self._finish_visible_reply(msg, reply)

    def _full_feed_for(self, msg: dict) -> FullFeed | None:
        """The pacing rules of the full-feed persona that handles this message."""
        if getattr(self._config, "chat_persona_profile", "") == PERSONA_PROFILE_YANG_YUJING:
            return _BUILTIN_FEED
        persona = self._persona_for(msg)
        return persona.full_feed if persona is not None else None

    def _mapped_full_feed_mode(self, msg: dict) -> str | None:
        """Full-feed behaviour of the persona mapped to this group, if it has one."""
        persona, how = self._personas.match(msg)
        if persona is None or persona.full_feed is None:
            return None
        if not msg.get("is_group", True) or msg.get("is_system_join"):
            return None
        if not str(msg.get("sender_id", "")).strip():
            return None
        group_name = str(msg.get("group_name", "") or "").strip()
        chat_id = str(msg.get("chat_id", "")).strip()
        feed = persona.full_feed
        # Reading every message is a big step: it needs the map to name the group
        # (not "*" or "default"), or the persona's own file to list it.
        if how != "group" and not any(
            name in feed.groups or name in feed.always_reply_groups for name in (group_name, chat_id) if name
        ):
            return None
        msg_age_sec = int(time.time()) - int(msg.get("timestamp", 0) or 0)
        if msg_age_sec > ALL_MESSAGES_MAX_AGE_SEC:
            logger.info(
                "Ignoring stale full-feed message in %s (age=%ds, max=%ds)",
                chat_id[:20], msg_age_sec, ALL_MESSAGES_MAX_AGE_SEC,
            )
            return None
        always = persona.full_feed.always_reply_groups
        return "always_reply" if (group_name in always or chat_id in always) else "ambient"

    def _full_feed_mode(self, msg: dict) -> str | None:
        """Return the full-feed behavior for this message, if any.

        Two sources: the global ``yang_yujing`` profile (groups from the
        configuration, as before) and, without it, the persona mapped to the
        group in ``data/persona_map.json`` when that persona has a ``full_feed``.
        """
        if (
            getattr(self._config, "chat_persona_profile", "")
            != PERSONA_PROFILE_YANG_YUJING
        ):
            return self._mapped_full_feed_mode(msg)
        if not msg.get("is_group", True) or msg.get("is_system_join"):
            return None

        # A normalized group message has a sender ID. This excludes WCDB
        # system notices without treating media sent by real members as noise.
        if not str(msg.get("sender_id", "")).strip():
            return None

        target_group = str(
            getattr(self._config, "yang_yujing_group_name", ""),
        ).strip()
        always_reply_group = str(
            getattr(self._config, "yang_yujing_always_reply_group_name", ""),
        ).strip()
        if not target_group and not always_reply_group:
            return None

        group_name = str(msg.get("group_name", "")).strip()
        chat_id = str(msg.get("chat_id", "")).strip()
        always_reply_groups = [g.strip() for g in always_reply_group.split(",") if g.strip()]
        target_groups = [g.strip() for g in target_group.split(",") if g.strip()]

        if any(g in (group_name, chat_id) for g in always_reply_groups):
            mode = "always_reply"
        elif any(g in (group_name, chat_id) for g in target_groups):
            mode = "ambient"
        else:
            return None

        msg_age_sec = int(time.time()) - int(msg.get("timestamp", 0) or 0)
        if msg_age_sec > ALL_MESSAGES_MAX_AGE_SEC:
            logger.info(
                "Ignoring stale Yang Yujing full-feed message in %s (age=%ds, max=%ds)",
                chat_id[:20], msg_age_sec, ALL_MESSAGES_MAX_AGE_SEC,
            )
            return None
        return mode

    def _handle_full_feed_chat(
        self,
        msg: dict,
        *,
        force_reply: bool = False,
    ) -> str | None:
        """Let Yang Yujing consider every fresh message from the target group."""
        if self._is_image_message(msg):
            # The text backend has no visual details here. It can still use
            # the surrounding context, but must not see a raw media blob.
            clean_content = "[图片]"
        else:
            clean_content = _WECHAT_SENDER_PREFIX.sub(
                "", str(msg.get("content", "") or ""), count=1,
            ).strip()
        if not clean_content and not force_reply:
            return None
        if not clean_content:
            clean_content = "[空消息]"

        logger.info(
            "Yang Yujing full-feed message in %s from '%s': %s",
            msg.get("group_name", msg.get("chat_id", "群聊")),
            msg.get("sender_name", "群友"),
            clean_content[:80],
        )
        return self._handle_chat(
            msg,
            clean_content,
            ambient=True,
            force_reply=force_reply,
        )

    def _should_consider_ambient_turn(self, msg: dict) -> bool:
        """Keep Yang's ambient group participation sparse before the AI call."""
        if self._is_direct_address(msg):
            return True
        if msg.get("quoted_content") or msg.get("is_reply_to_bot"):
            return False

        content = _WECHAT_SENDER_PREFIX.sub(
            "", str(msg.get("content", "") or ""), count=1,
        ).strip()
        feed = self._full_feed_for(msg) or _BUILTIN_FEED
        compact = re.sub(r"\s+", "", content)
        without_media = re.sub(r"\[(?:图片|表情|视频|语音)\]", "", compact)
        if not without_media:
            return False
        if compact in feed.skip_reactions or len(compact) < feed.min_chars:
            return False
        lowered = compact.casefold()
        if any(word.casefold() in lowered for word in feed.skip_words):
            return False

        chat_id = str(msg.get("chat_id", ""))
        now = time.monotonic()
        with self._ambient_lock:
            next_turn_at = self._ambient_next_turn_at.get(chat_id, 0.0)
            if now < next_turn_at:
                logger.info(
                    "Yang Yujing ambient cooldown in %s (%.1fs remaining)",
                    chat_id[:20], next_turn_at - now,
                )
                return False
            # Reserve before calling the model so simultaneous messages cannot
            # create a burst of parallel replies.
            self._ambient_next_turn_at[chat_id] = (
                now + feed.ambient_cooldown_sec
            )
        return True

    def _is_direct_address(self, msg: dict) -> bool:
        if msg.get("is_at_mentioned") or msg.get("is_reply_to_bot"):
            return True
        content = str(msg.get("content", "") or "")
        persona = self._persona_for(msg)
        names = {
            str(getattr(self._config, "bot_display_name", "") or "").strip(),
            str(getattr(self._config, "chat_persona_name", "") or "").strip(),
            persona.name if persona is not None else "",
            "假企鹅",
            "qier",
        }
        return any(name and f"@{name}" in content for name in names)

    # ── Todo helpers ────────────────────────────────────────────

    _group_names_cache: dict[str, str] | None = None
    _group_names_loaded: bool = False

    def _load_group_names(self) -> dict[str, str]:
        """Load chat_id → display_name mapping from disk (lazy + cached)."""
        if not self._group_names_loaded:
            self._group_names_loaded = True
            import json
            from pathlib import Path
            path = Path("data/group_names.json")
            if path.exists():
                try:
                    self._group_names_cache = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    self._group_names_cache = {}
        return self._group_names_cache or {}

    def _is_todo_group(self, chat_id: str) -> bool:
        """Check if the given chat_id is in the todo allowed-groups list.

        Uses group_names.json (persisted by WcdbBackend) to match
        configured group names against actual chat_ids and their
        display names.
        """
        groups = self._config.todo_groups
        if not groups or groups == ["*"]:
            return True
        # Load display name mapping (chat_id → display_name)
        name_map = self._load_group_names()
        display_name = name_map.get(chat_id, "")
        for g in groups:
            g_lower = g.lower()
            # Exact match against chat_id or display name
            if g == chat_id or g == display_name:
                return True
            # Substring match against chat_id or display name
            if g_lower in chat_id.lower():
                return True
            if display_name and g_lower in display_name.lower():
                return True
        return False

    # ── Memory helper ────────────────────────────────────────────

    def _get_group_memory(self, chat_id: str) -> str:
        """Return the group's memory text, or empty string if none."""
        mem = self._store.get_group_memory(chat_id)
        memory_text = mem["memory_text"] if mem else ""
        facts_text = self._get_group_facts_text(chat_id)
        if facts_text:
            if memory_text:
                return memory_text + "\n\n" + facts_text
            return facts_text
        return memory_text

    def _get_group_memory_for_chat(self, chat_id: str, *, ambient: bool,
                                   facts_only: bool = False) -> str:
        """Avoid injecting a free-form diary into every full-feed ambient turn."""
        if ambient and (
            facts_only
            or getattr(self._config, "chat_persona_profile", "")
            == PERSONA_PROFILE_YANG_YUJING
        ):
            return self._get_group_facts_text(chat_id)
        return self._get_group_memory(chat_id)

    # ── Memory v2 helpers (only reached when _memory_v2_for(msg)) ──

    def _memory_context_v2(self, msg: dict, query: str, context: list[dict], *,
                           directed: bool, recall: bool,
                           with_memory: bool = True) -> MemoryContext:
        """Facts/episodes for the people present and, for recall questions, old messages."""
        context = context or []
        names = [self._nicks.resolve_name(msg.get("sender_id", "")) or ""]
        names.append(str(msg.get("sender_name", "")))
        names += [str(m.get("sender_name", "")) for m in context[-10:]]
        exclude = {str(msg.get("message_id", ""))}
        exclude.update(str(m.get("message_id", "")) for m in context)

        def name_of(sender_id: str, fallback: str) -> str:
            custom = self._nicks.resolve_name(sender_id)
            return custom if custom and custom != sender_id else fallback

        try:
            ctx = build_memory_context(
                self._mstore,
                chat_id=msg["chat_id"], query=query,
                people=[n for n in dict.fromkeys(names) if n],
                context_text=" ".join(str(m.get("content", "")) for m in context[-10:]),
                directed=directed and with_memory, recall=recall, with_memory=with_memory,
                now=time.time(),
                min_confidence=getattr(self._config, "memory_min_confidence", 0.6),
                max_facts=getattr(self._config, "memory_prompt_facts", 12),
                exclude_ids=exclude, name_of=name_of,
                vectors=self._vectors,
                min_similarity=getattr(self._config, "memory_embedding_min_sim", 0.55),
            )
        except Exception:
            logger.exception("Memory v2 retrieval failed for %s", msg["chat_id"][:20])
            return MemoryContext()
        counts = {k: len(v) for k, v in ctx.sources.items() if v}
        if counts:
            self._tracer.current().step("memory_v2", **counts, sources=ctx.sources)
        return ctx

    def _group_memory_for_prompt(self, msg: dict, query: str, context: list[dict], *,
                                 ambient: bool, proactive: bool = False) -> str:
        """The long-term memory text a prompt carries (diary or memory v2)."""
        chat_id = msg["chat_id"]

        def legacy() -> str:
            if proactive:
                return self._get_group_memory(chat_id)
            persona = self._persona_for(msg)
            if persona is not None and persona.full_feed is not None:
                return self._get_group_memory_for_chat(chat_id, ambient=ambient, facts_only=True)
            return self._get_group_memory_for_chat(chat_id, ambient=ambient)

        if not self._memory_v2_for(msg):
            return legacy()
        ctx = self._memory_context_v2(
            msg, query, context, directed=not (ambient or proactive), recall=False,
        )
        if not ctx.memory_text and not self._mstore.has_memory(chat_id):
            return legacy()                     # cold start: keep the diary until v2 has content
        parts = [t for t in (ctx.memory_text, self._get_group_facts_text(chat_id)) if t]
        return "\n\n".join(parts) if parts else NO_RELEVANT_MEMORY

    def _get_group_facts_text(self, chat_id: str) -> str:
        if not hasattr(self._store, "get_group_facts"):
            return ""
        try:
            facts = self._store.get_group_facts(chat_id, limit=30)
        except Exception:
            logger.exception("Failed to load group facts for %s", chat_id[:20])
            return ""
        lines = []
        for fact in facts:
            subject = str(fact.get("subject", "") or "").strip()
            predicate = str(fact.get("predicate", "") or "").strip()
            obj = str(fact.get("object", "") or "").strip()
            if not subject or not obj:
                continue
            if predicate == "alias":
                lines.append(f"- {subject} 在这个群里的固定称呼/别名是「{obj}」。")
        if not lines:
            return ""
        return (
            "## 固定事实/称呼记忆（优先级高于普通聊天日记）\n"
            + "\n".join(lines[:20])
        )

    def _history_recall_terms(self, text: str) -> list[str]:
        """Extract search terms only from a clear request about prior chat."""
        text = str(text or "").strip()
        if not text or not _HISTORY_RECALL_RE.search(text):
            return []

        remainder = _HISTORY_QUERY_NOISE_RE.sub(" ", text)
        candidates = re.findall(
            r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,40}", remainder,
        )
        terms = []
        seen = set()
        for candidate in candidates:
            candidate = candidate.strip()
            if len(candidate) < 2:
                continue
            variants = [candidate]
            # Long uninterrupted Chinese questions rarely match one archived
            # sentence exactly. Its last few content characters are a useful
            # secondary search term (for example, "南通人").
            if re.fullmatch(r"[\u4e00-\u9fff]{5,40}", candidate):
                variants.append(candidate[-4:])
            for variant in variants:
                key = variant.casefold()
                if key in seen:
                    continue
                seen.add(key)
                terms.append(variant)
                if len(terms) >= 6:
                    return terms
        return terms

    def _get_history_recall_context(self, msg: dict, clean_content: str,
                                    context: list[dict] | None = None) -> str:
        """Format a few matching archive lines for an explicit recall question."""
        if not msg.get("is_group", True):
            return ""
        if self._memory_v2_for(msg):
            if not _HISTORY_RECALL_RE.search(str(clean_content or "")):
                return ""
            return self._memory_context_v2(
                msg, clean_content, context or [], directed=True, recall=True,
                with_memory=False,
            ).recall_text
        search = getattr(self._store, "search_chat_history", None)
        if not callable(search):
            return ""

        terms = self._history_recall_terms(clean_content)
        if not terms:
            return ""
        try:
            records = search(msg["chat_id"], terms, limit=8)
        except Exception:
            logger.exception("Historical chat search failed for %s", msg["chat_id"][:20])
            return ""
        if not records:
            logger.info("No historical chat records matched recall terms: %s", terms)
            return ""

        lines = []
        for record in records:
            content = re.sub(r"\s+", " ", str(record.get("content", ""))).strip()
            if not content:
                continue
            sender_id = str(record.get("sender_id", "") or "")
            sender_name = str(record.get("sender_name", "") or "群友")
            custom_name = self._nicks.resolve_name(sender_id)
            if custom_name and custom_name != sender_id:
                sender_name = custom_name
            timestamp = int(record.get("timestamp", 0) or 0)
            when = (
                datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
                if timestamp > 0 else "时间未知"
            )
            lines.append(f"- {when} {sender_name}：{content[:180]}")
        if not lines:
            return ""
        return (
            "【历史聊天检索结果】\n"
            "下面是本群旧记录的原文片段。只在它们直接回答当前问题时使用；"
            "没有足够依据就说记不清，不要把推测说成事实。\n"
            + "\n".join(lines)
        )

    def _remember_structured_facts(self, msg: dict) -> None:
        if not hasattr(self._store, "upsert_group_fact"):
            return
        timestamp = int(msg.get("timestamp", 0) or time.time())
        try:
            recent = self._store.get_messages_since(
                msg["chat_id"],
                timestamp - 300,
                until_ts=timestamp,
                limit=16,
            )
        except Exception:
            logger.exception("Failed to fetch context for fact extraction")
            recent = []
        facts = self._fact_extractor.extract(msg, recent)
        for fact in facts:
            subject = self._nicks.resolve_wxids(fact.subject)
            obj = self._nicks.resolve_wxids(fact.object)
            try:
                self._store.upsert_group_fact(
                    chat_id=msg["chat_id"],
                    subject=subject,
                    predicate=fact.predicate,
                    object_value=obj,
                    source_message_id=str(msg.get("message_id", "")),
                    confidence=fact.confidence,
                )
                logger.info(
                    "Remembered group fact: %s %s %s in %s",
                    subject, fact.predicate, obj, msg["chat_id"][:20],
                )
            except Exception:
                logger.exception("Failed to persist group fact")

    # ── Summary handler ──────────────────────────────────────────

    # ── AI Chat handler ──────────────────────────────────────────

    def _handle_chat(
        self,
        msg: dict,
        clean_content: str,
        *,
        ambient: bool = False,
        force_reply: bool = False,
    ) -> str | None:
        """Handle a direct chat request or an always-listen persona message."""
        # Resolve custom nickname from file (via sender_id=wxid),
        # not sender_name which may already be a backend-resolved default.
        display_name = self._nicks.resolve_name(msg["sender_id"])
        if display_name == msg["sender_id"]:
            display_name = msg["sender_name"]

        logger.info(
            "AI chat: '%s' asks '%s'",
            display_name, clean_content[:60],
        )

        if not ambient:
            image_reply = self._handle_image_question(
                msg, clean_content, display_name,
            )
            if image_reply is not None:
                self._tracer.current().reason("image_question")
                return image_reply

        # Always fetch recent chat context for @mentions.
        # The bot is @mentioned inside a group conversation — the surrounding
        # chat is almost always relevant.  Keyword-based gating (e.g. "刚才",
        # "之前") is too brittle: natural language has countless ways to
        # reference prior chat without those specific words ("挑一件事评价一下",
        # "怎么看", "那件事", etc.).
        context_window_sec = getattr(
            self._config, "chat_context_window_sec", CHAT_CONTEXT_WINDOW_SEC,
        )
        context_count = getattr(
            self._config, "chat_context_count", MAX_CONTENT_LINES,
        )
        persona = self._persona_for(msg)
        if persona is not None:
            context_window_sec, context_count = persona.context_window_sec, persona.context_count
        if msg.get("_followup_listen_triggered"):
            context_count = min(
                context_count,
                getattr(self._config, "followup_listen_context_count", 6),
            )
        since = int(time.time()) - context_window_sec
        context = self._store.get_messages_since(
            msg["chat_id"], since, limit=context_count,
        )
        if context:
            for m in context:
                custom = self._nicks.resolve_name(m["sender_id"])
                if custom != m["sender_id"]:
                    m["sender_name"] = custom
            logger.info(
                "Chat context: %d messages for '%s'",
                len(context), display_name,
            )
        trace = self._tracer.current()
        trace.note(
            context_msgs=len(context), ambient=ambient, force_reply=force_reply,
        )
        use_pipeline = self._pipeline_applies(msg)
        ai_message = self._message_for_ai_chat(
            msg,
            clean_content,
            context_messages=context,
            bot_name=self._config.bot_display_name,
            trigger_hints=not use_pipeline,
        )
        history_context = self._get_history_recall_context(msg, clean_content, context)
        if history_context:
            trace.step("history_recall", chars=len(history_context))
            ai_message = f"{ai_message}\n\n{history_context}"
        web_search_used = False
        search_query = ""
        if not ambient and self._web_search is not None:
            search_query = self._web_search.extract_query(
                clean_content,
                bot_name=self._config.bot_display_name,
            )
            if search_query:
                logger.info("Web search requested by '%s': %s", display_name, search_query)
                trace.step("web_search", query=search_query[:80])
                try:
                    search_results = self._web_search.search(search_query)
                    search_context = self._web_search.format_for_prompt(
                        search_query,
                        search_results,
                    )
                    ai_message = (
                        f"{ai_message}\n\n{search_context}\n"
                        "这次用户需要联网信息；如果搜索结果不足，请明确说没查到，不要编。"
                    )
                    web_search_used = True
                except Exception as e:
                    logger.warning("Web search failed for %r: %s", search_query, e)
                    ai_message = (
                        f"{ai_message}\n\n"
                        f"联网搜索请求：{search_query}\n"
                        "联网搜索失败。请不要编造实时信息，可以告诉对方暂时没查到。"
                    )
                    web_search_used = True

        try:
            chat_kwargs = {}
            chat_kwargs.update(self._persona_kwargs(msg))
            if web_search_used:
                chat_kwargs["max_chars_override"] = getattr(
                    self._web_search,
                    "reply_max_chars",
                    None,
                )
            if use_pipeline:
                return self._reply_with_pipeline(
                    msg, clean_content, ai_message, context, display_name,
                    ambient=ambient, force_reply=force_reply,
                    max_chars_override=chat_kwargs.get("max_chars_override"),
                )
            ai_reply = self._summarizer.chat(
                message=ai_message,
                context_messages=context,
                requester_name=display_name,
                bot_name=self._config.bot_display_name,
                group_name=msg.get("group_name", msg.get("chat_id", "群聊")),
                is_group=msg.get("is_group", True),
                group_memory=self._group_memory_for_prompt(
                    msg, clean_content, context, ambient=ambient,
                ),
                ambient=ambient,
                force_reply=force_reply,
                **chat_kwargs,
            )
            ai_reply = self._nicks.resolve_wxids(ai_reply)
            # Guard against empty AI reply — sending a bare @mention is confusing
            if not ai_reply or not ai_reply.strip():
                trace.reason("ai_blank_reply")
                if getattr(
                    self._summarizer,
                    "last_chat_reply_was_intentional_silence",
                    False,
                ):
                    logger.info(
                        "AI chose silence for '%s' in %s",
                        display_name, msg.get("group_name", msg["chat_id"][:20]),
                    )
                    if not force_reply:
                        trace.reason("ai_intentional_silence")
                        return None
                logger.warning(
                    "AI chat returned empty for '%s' in %s",
                    display_name, msg.get("group_name", msg["chat_id"][:20]),
                )
                if force_reply:
                    trace.reason("ai_blank_forced_fallback")
                    return "啊？"
                if self._needs_direct_fallback(msg):
                    trace.reason("ai_blank_direct_fallback")
                    prompt = (clean_content or "").strip()
                    if prompt in {"", "在吗", "在不在", "在"}:
                        return "嗯，在的"
                    return "我刚才没接住，再说一次好不好"
                return None
            trace.reason("ai_reply")
            return ai_reply
        except Exception as e:
            logger.error("AI chat failed: %s", e)
            trace.reason("ai_error", error=type(e).__name__)
            if force_reply:
                return "刚没看懂"
            if ambient:
                # Full-feed mode must never produce an error-message flood.
                return None
            if msg.get("_name_call_triggered"):
                # Only a name was said; nobody asked for an answer.
                return None
            if self._needs_direct_fallback(msg):
                prompt = (clean_content or "").strip()
                if prompt in {"", "在吗", "在不在", "在"}:
                    return "嗯，在的"
                return "我刚才有点卡，再说一次好不好"
            return "大脑短路了，稍等再试～"

    def _handle_image_question(
        self,
        msg: dict,
        clean_content: str,
        display_name: str,
    ) -> str | None:
        """Answer only explicit questions about a recent image/sticker."""
        if not getattr(self._config, "image_vision_enabled", True):
            return None
        if not is_image_question(
            clean_content, self._config.bot_display_name,
        ):
            return None

        lookback_sec = int(
            getattr(self._config, "image_vision_lookback_sec", 900) or 900,
        )
        before_ts = int(msg.get("timestamp", int(time.time())) or time.time())
        media_records: list[dict] = []
        get_recent_media = getattr(self._store, "get_recent_media", None)
        if callable(get_recent_media):
            try:
                media_records = get_recent_media(
                    msg["chat_id"],
                    before_ts=before_ts,
                    lookback_sec=lookback_sec,
                    limit=8,
                )
            except Exception:
                logger.exception("Failed to query recent media")

        image_path = find_recent_image_path(self._config, media_records)
        if not image_path:
            logger.info(
                "Image question but no recent media found: chat=%s sender=%s",
                msg.get("chat_id", "")[:20], display_name,
            )
            return "我没找到刚才那张图……再发一次好不好"

        describe_image = getattr(self._summarizer, "describe_image", None)
        if not callable(describe_image):
            return "我现在还看不了图……"

        try:
            reply = describe_image(
                **self._persona_kwargs(msg),
                image_path=image_path,
                question=clean_content,
                requester_name=display_name,
                bot_name=self._config.bot_display_name,
                max_chars=getattr(self._config, "image_vision_reply_max_chars", 80),
                max_bytes=getattr(
                    self._config,
                    "image_vision_max_bytes",
                    5 * 1024 * 1024,
                ),
            )
            reply = self._nicks.resolve_wxids(reply)
            return reply.strip() if reply and reply.strip() else "我看不太清……"
        except Exception:
            logger.exception("Image understanding failed")
            return "我这边看图有点卡，再发一次好不好"

    def _handle_quoted_image_question(self, msg: dict) -> str | None:
        """Answer a named, explicit question about the exact quoted image."""
        if not getattr(self._config, "image_vision_enabled", True):
            return None
        bot_name = self._config.bot_display_name.strip()
        content = str(msg.get("content", "") or "").strip()
        quoted_server_id = str(msg.get("quoted_server_id", "") or "").strip()
        if (
            not bot_name
            or bot_name not in content
            or not is_image_question(content, bot_name)
            or not quoted_server_id
        ):
            return None

        get_media_by_server_id = getattr(self._store, "get_media_by_server_id", None)
        if not callable(get_media_by_server_id):
            return "我没找到你引用的那张图……再发一次好不好"
        try:
            record = get_media_by_server_id(msg["chat_id"], quoted_server_id)
        except Exception:
            logger.exception("Failed to find quoted image")
            return "我没找到你引用的那张图……再发一次好不好"
        if not record:
            return "我没找到你引用的那张图……再发一次好不好"

        image_path = find_recent_image_path(self._config, [record])
        if not image_path:
            return "我没找到你引用的那张图……再发一次好不好"
        describe_image = getattr(self._summarizer, "describe_image", None)
        if not callable(describe_image):
            return "我现在还看不了图……"

        display_name = self._nicks.resolve_name(msg["sender_id"])
        if display_name == msg["sender_id"]:
            display_name = msg["sender_name"]
        question = re.sub(re.escape(bot_name), "", content).strip(" ，,：:")
        try:
            reply = describe_image(
                **self._persona_kwargs(msg),
                image_path=image_path,
                question=question or "这张图是什么意思？",
                requester_name=display_name,
                bot_name=bot_name,
                max_chars=getattr(self._config, "image_vision_reply_max_chars", 80),
                max_bytes=getattr(
                    self._config, "image_vision_max_bytes", 5 * 1024 * 1024,
                ),
            )
            reply = self._nicks.resolve_wxids(reply)
            return reply.strip() if reply and reply.strip() else "我看不太清……"
        except Exception:
            logger.exception("Quoted image understanding failed")
            return "我这边看图有点卡，再发一次好不好"

    @staticmethod
    def _is_image_message(msg: dict) -> bool:
        try:
            return int(msg.get("msg_type", 1) or 1) in IMAGE_MSG_TYPES
        except (TypeError, ValueError):
            return False

    def _handle_image_followup(self, msg: dict) -> str | None:
        """Reply only when the sender's image immediately follows our reply."""
        if (
            not getattr(self._config, "image_vision_enabled", True)
            or self._image_followup is None
        ):
            return None

        match = self._image_followup.consume(msg)
        if match is None:
            return None

        image_path = find_recent_image_path(self._config, [msg])
        captured_path = ""
        if (
            not image_path
            and getattr(self._config, "image_screen_fallback_enabled", True)
        ):
            capture = msg.get("_capture_media_screenshot")
            if callable(capture):
                try:
                    captured_path = str(capture() or "")
                    image_path = captured_path
                except Exception:
                    logger.exception("Image follow-up screen capture failed")
        if not image_path:
            logger.info(
                "Image follow-up skipped: media file unavailable chat=%s sender=%s",
                msg.get("chat_id", "")[:20], msg.get("sender_name", "")[:20],
            )
            return None

        describe_image = getattr(self._summarizer, "describe_image", None)
        if not callable(describe_image):
            return None

        display_name = self._nicks.resolve_name(msg["sender_id"])
        if display_name == msg["sender_id"]:
            display_name = msg["sender_name"]
        try:
            reply = describe_image(
                **self._persona_kwargs(msg),
                image_path=image_path,
                question="这张图片或表情包是在接你刚才的话吗？",
                requester_name=display_name,
                bot_name=self._config.bot_display_name,
                max_chars=getattr(self._config, "image_vision_reply_max_chars", 80),
                max_bytes=getattr(
                    self._config, "image_vision_max_bytes", 5 * 1024 * 1024,
                ),
                previous_bot_reply=match.bot_reply,
                is_chat_screenshot=bool(captured_path),
            )
        except Exception:
            logger.exception("Image follow-up understanding failed")
            return None
        finally:
            if captured_path:
                try:
                    Path(captured_path).unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to delete temporary vision capture")

        reply = self._nicks.resolve_wxids(reply or "").strip()
        if reply:
            logger.info(
                "Image follow-up matched: chat=%s sender=%s",
                msg.get("chat_id", "")[:20], display_name,
            )
        return reply or None

    def _finish_visible_reply(
        self,
        msg: dict,
        reply: str | None,
        *,
        open_image_followup: bool = True,
    ) -> str | None:
        """Clean a reply and open the short image-followup window for its sender."""
        if reply and not msg.get("_bubbled"):
            reply = strip_separators(reply)
        if reply and STICKER_MARK in reply:
            # Clean the text bubbles only: a sticker name such as "QAQ" is not an emoticon.
            reply_text = join_bubbles([
                part if sticker_in(part) is not None else self._strip_emoticons(self._strip_markdown(part))
                for part in split_bubbles(reply, limit=10 ** 6)
            ])
        else:
            reply_text = self._strip_markdown(reply) if reply else None
            if reply_text:
                reply_text = self._strip_emoticons(reply_text)
        if not reply_text:
            if reply:
                self._tracer.current().step("reply_empty_after_cleanup")
            return None

        # What the bot "said", as one line, for its own memory of the conversation.
        said = reply_text
        if BUBBLE_SEP in reply_text or STICKER_MARK in reply_text:
            reply_text = join_bubbles(split_bubbles(reply_text, limit=10 ** 6))   # cleaning may have emptied a bubble
            said = plain_text(reply_text)
            if not said:
                self._tracer.current().step("reply_empty_after_cleanup")
                return None
        self._recent_replies.add(msg.get("chat_id", ""), said)
        if self._memory_v2_for(msg):
            try:
                self._mstore.add_bot_reply(
                    msg["chat_id"], said, int(time.time()),
                    trigger_message_id=str(msg.get("message_id", "")),
                )
            except Exception:
                logger.exception("Failed to store the bot's reply")
        declared = msg.get("_followup_declared")
        waits_for_nobody = declared is not None and declared.expect == "none"
        if msg.get("is_group", True):
            if self._sticky is not None and not waits_for_nobody:
                self._sticky.register(msg["chat_id"], msg["sender_id"])
            if self._followup is not None:
                if declared is not None:
                    self._followup.register(
                        msg["chat_id"], said, expect=declared.expect,
                        sender_id=str(msg.get("sender_id", "") or ""), seconds=declared.seconds,
                    )
                else:
                    self._followup.register(msg["chat_id"], said)
        if open_image_followup and self._image_followup is not None and not waits_for_nobody:
            self._image_followup.register(
                msg["chat_id"],
                msg["sender_id"],
                said,
                int(msg.get("timestamp", 0) or 0),
            )
        return reply_text

    @classmethod
    def _message_for_ai_chat(cls, msg: dict, clean_content: str,
                             context_messages: list[dict] | None = None,
                             bot_name: str = "",
                             trigger_hints: bool = True) -> str:
        # ``trigger_hints`` are the legacy "is this meant for you? answer with
        # blank if not" sentences; the structured pipeline states the same
        # facts in its own prompt and must not contradict its JSON format.
        if trigger_hints and msg.get("_followup_listen_triggered"):
            bot_reply = str(msg.get("_followup_bot_reply", "") or "").strip()
            reason = str(msg.get("_followup_listen_reason", "") or "").strip()
            sender = msg.get("sender_name", "群友")
            return (
                f"你刚才在群里回复：「{bot_reply}」。"
                f"现在 {sender} 发了一句可能是在接你话的短消息：「{clean_content}」。"
                f"本地候选原因：{reason or '像是在接上一句'}。"
                "请结合最近上下文判断这是不是在对你说；"
                "如果不是，回复空白；如果是，只接这一句自然回复。"
            )

        if trigger_hints and msg.get("_sticky_triggered"):
            return (
                "这条消息没有直接@你，但来自刚才正在和你连续聊天的人。"
                "请结合最近上下文判断是不是还在对你说；如果只是和别人说，回复空白。"
                f"最新消息：{clean_content}"
            )

        if not msg.get("is_reply_to_bot"):
            return clean_content

        quoted = str(msg.get("quoted_content", "") or "").strip()
        if len(quoted) > 120:
            quoted = quoted[:120].rstrip() + "..."
        prior_request = cls._find_prior_direct_request(
            msg, context_messages, bot_name,
        )
        if not quoted:
            if prior_request:
                return (
                    f"对方前面直接对你说：「{prior_request}」。"
                    f"现在对方引用了你上一句话，然后说：{clean_content}。"
                    "回复必须延续前面请求里的硬约束，不要被中间别人的聊天带偏。"
                )
            return f"对方引用了你上一句话，然后说：{clean_content}"
        if clean_content:
            if prior_request:
                return (
                    f"对方前面直接对你说：「{prior_request}」。"
                    f"你随后回复/追问：「{quoted}」。"
                    f"现在对方引用这句话回答：「{clean_content}」。"
                    "回复必须延续前面请求里的硬约束，比如语言、数量、主题和格式；"
                    "如果前面是推荐几首/几个，就直接给2到3个结果，少解释，尽量用短名字；"
                    "不要被中间别人的聊天带偏。"
                )
            return f"对方引用了你上一句话「{quoted}」，然后说：{clean_content}"
        return f"对方引用了你上一句话「{quoted}」"

    @classmethod
    def _find_prior_direct_request(cls, msg: dict,
                                   context_messages: list[dict] | None,
                                   bot_name: str = "") -> str:
        if not context_messages:
            return ""

        sender_id = msg.get("sender_id", "")
        current_id = msg.get("message_id", "")
        current_ts = int(msg.get("timestamp", 0) or 0)
        current_content = cls._clean_context_text(
            msg.get("content", ""), bot_name,
        )
        for item in reversed(context_messages):
            if current_id and item.get("message_id") == current_id:
                continue
            if sender_id and item.get("sender_id") != sender_id:
                continue
            item_ts = int(item.get("timestamp", 0) or 0)
            if current_ts and item_ts > current_ts:
                continue

            content = cls._clean_context_text(item.get("content", ""), bot_name)
            if not content or (current_content and content == current_content):
                continue
            direct = bool(bot_name and (
                f"@{bot_name}" in str(item.get("content", ""))
                or bot_name in str(item.get("content", ""))
                or f"@{bot_name}" in content
                or bot_name in content
            ))
            if direct:
                return content
        return ""

    @staticmethod
    def _clean_context_text(content: object, bot_name: str = "") -> str:
        text = _WECHAT_SENDER_PREFIX.sub("", str(content or ""), count=1).strip()
        if bot_name:
            text = re.sub(
                re.escape(f"@{bot_name}") + r"[^\w]*",
                "",
                text,
            ).strip()
        if len(text) > 160:
            text = text[:160].rstrip() + "..."
        return text

    @staticmethod
    def _needs_direct_fallback(msg: dict) -> bool:
        return bool(
            msg.get("is_at_mentioned")
            or msg.get("_kw_triggered")
            or msg.get("_private_triggered")
        )

    # ── Structured pipeline (PIPELINE_V2) ───────────────────────────

    def _pipeline_active(self) -> bool:
        """True when chat should go through the structured decision engine."""
        return (
            self._pipeline_strategy != STRATEGY_OFF
            and callable(getattr(self._summarizer, "decide_and_reply", None))
        )

    def _pipeline_applies(self, msg: dict) -> bool:
        """The structured pipeline is on, and this message's chat is switched over."""
        if not self._pipeline_active():
            return False
        return self._chat_in_groups(self._pipeline_groups, msg)

    @staticmethod
    def _chat_in_groups(groups: tuple, msg: dict) -> bool:
        """Does this message's chat match a canary list ("*", names, "private")?"""
        if "*" in groups:
            return True
        if not msg.get("is_group", True):
            return any(g in ("private", "私聊") for g in groups)
        places = (
            str(msg.get("group_name", "")).lower(),
            str(msg.get("chat_id", "")).lower(),
        )
        return any(
            g in place
            for g in groups if g not in ("private", "私聊")
            for place in places if place
        )

    def _recent_bot_replies(self, msg: dict) -> tuple:
        """What the bot said lately; after a restart it is read back from the store once."""
        chat_id = msg["chat_id"]
        replies = self._recent_replies.recent(chat_id)
        if (replies or chat_id in self._replies_seeded
                or not self._memory_v2_for(msg)):
            return replies
        self._replies_seeded.add(chat_id)
        try:
            for text in self._mstore.recent_bot_replies(
                chat_id, int(time.time()) - 1800, limit=6,
            ):
                self._recent_replies.add(chat_id, text)
        except Exception:
            logger.exception("Failed to restore the bot's recent replies")
        return self._recent_replies.recent(chat_id)

    def _memory_v2_for(self, msg: dict) -> bool:
        """Memory v2 is on and this message's chat is switched over."""
        return self._mstore is not None and self._chat_in_groups(self._memory_groups, msg)

    def _scored_gate_for(self, msg: dict) -> bool | None:
        """Which proactive gate this message uses (None: whatever is configured).

        The scored gate comes with the pipeline: when the pipeline is on but
        this chat is not switched over, the old dice keep deciding there.
        """
        if self._pipeline_strategy == STRATEGY_OFF:
            return None
        return self._pipeline_applies(msg)

    def _reply_with_pipeline(
        self,
        msg: dict,
        clean_content: str,
        ai_message: str,
        context: list[dict],
        display_name: str,
        *,
        ambient: bool,
        force_reply: bool,
        max_chars_override: int | None,
    ) -> str | None:
        """Ask the engine for a decision on a message that reached the chat route."""
        addressing = classify(
            msg,
            names=self._names_for(msg),
            private=not msg.get("is_group", True),
            forced=force_reply,
            full_feed=ambient,
        )
        request = ChatRequest(
            message=ai_message,
            context_messages=tuple(context),
            requester_name=display_name,
            bot_name=self._config.bot_display_name,
            group_name=msg.get("group_name", msg.get("chat_id", "群聊")),
            is_group=msg.get("is_group", True),
            group_memory=self._group_memory_for_prompt(
                msg, clean_content, context, ambient=ambient,
            ),
            addressing=addressing,
            ambient=ambient,
            force_reply=force_reply,
            max_chars_override=max_chars_override,
            recent_bot_replies=self._recent_bot_replies(msg),
            call_name="chat_v2",
            persona=self._persona_settings_for(msg),
            declare_followup=self._declare_followups,
            **self._expression_options(msg),
            examples=self._examples_for(msg, clean_content),
        )
        decision = self._summarizer.decide_and_reply(
            request, strategy=self._pipeline_strategy,
        )
        self._tracer.current().note(
            addressing=addressing.as_trace(), decision=decision.as_trace(),
        )
        return self._reply_from_decision(
            msg, clean_content, decision, addressing, force_reply,
        )

    def _decision_reply_text(self, msg: dict, decision: Decision) -> str:
        """The model's messages as the reply string: one line, or bubbles with ``SEND_BUBBLES=on``.

        The one place that may put a bubble separator or a sticker mark into a
        reply: marks in the model's text or in a nickname are removed.  With
        ``WECHAT_EMOJI=on`` only the offered emoji codes stay in the text (checked
        before nicknames are filled in); a sticker the model picked, or wrote as
        ``[表情包：name]``, becomes a bubble of its own at the end.
        """
        if self._send_bubbles and len(decision.messages) > 1:
            texts = list(decision.messages)
        else:
            texts = [decision.text] if decision.messages else []
        named: list[str] = []
        cleaned = []
        for text in texts:
            text, names = pop_sticker_mentions(strip_separators(text))
            named += names
            if self._wechat_emoji:
                text = keep_known(text, self._wechat_emoji)
            cleaned.append(strip_separators(self._nicks.resolve_wxids(text)))
        parts = [text for text in cleaned if text.strip()]
        sticker = self._take_sticker(msg, getattr(decision, "sticker", "") or (named[0] if named else ""))
        if sticker:
            parts.append(sticker_token(sticker))
        if len(parts) > 1 or sticker:
            msg["_bubbled"] = True
            return join_bubbles(parts)
        return parts[0] if parts else ""

    def _persona_ids(self, msg: dict) -> tuple[str, str]:
        """Name and id of the persona speaking in this chat (stickers can be limited to one)."""
        persona = self._persona_for(msg)
        if persona is not None:
            return persona.name, persona.id
        return self._config.chat_persona_name, getattr(self._config, "chat_persona_profile", "")

    def _examples_for(self, msg: dict, text: str) -> str:
        """The persona's example dialogues closest to ``text``, rendered for the prompt ("" for none)."""
        if self._example_count <= 0:
            return ""
        name, persona_id = self._persona_ids(msg)
        try:
            book = self._examples.book_for(persona_id, name)
        except Exception:                                  # noqa: BLE001 - examples are optional
            logger.exception("Persona examples could not be read")
            return ""
        if not book:
            return ""
        picked = book.pick(text, self._example_count, seed=str(msg.get("message_id", "")))
        if not picked:
            return ""
        self._tracer.current().note(persona_examples=len(picked))
        return render_examples(picked, name)

    def _expression_options(self, msg: dict) -> dict:
        """The emoji codes and stickers to offer the model for this chat (none when switched off)."""
        options = {"wechat_emoji": self._wechat_emoji}
        if self._send_stickers and not self._sticker_cooling(msg.get("chat_id", "")):
            choices = get_stickers().choices(*self._persona_ids(msg))
            if choices:
                options["stickers"] = tuple((s.name, s.when) for s in choices)
        return options

    def _sticker_cooling(self, chat_id: str) -> bool:
        with self._sticker_lock:
            last = self._sticker_last_at.get(chat_id)
        return last is not None and time.time() - last < self._sticker_interval

    def _take_sticker(self, msg: dict, name: str) -> str:
        """The sticker to send with this reply ("" for none); starts the chat's cooldown.

        Checked again here, under the lock: two replies built at the same time
        in one chat were both offered stickers, but only one may send one.
        """
        if not name or not self._send_stickers:
            return ""
        sticker = get_stickers().get(name)
        if sticker is None or not sticker.fits(*self._persona_ids(msg)):
            return ""
        chat_id = msg.get("chat_id", "")
        now = time.time()
        with self._sticker_lock:
            last = self._sticker_last_at.get(chat_id)
            if last is not None and now - last < self._sticker_interval:
                return ""
            self._sticker_last_at[chat_id] = now
        return name

    def _reply_from_decision(
        self,
        msg: dict,
        clean_content: str,
        decision: Decision,
        addressing: Addressing,
        force_reply: bool,
    ) -> str | None:
        """Turn a decision into the reply text, keeping the legacy fallbacks."""
        trace = self._tracer.current()
        private = addressing.has(SIGNAL_PRIVATE)
        if decision.is_reply:
            # A reply the model itself says was meant for somebody else is not
            # sent when the only reason to ask was a name, a keyword or a
            # short follow-up (an @ or a quote of the bot still gets its answer).
            if (
                addressing.level in (LEVEL_STRONG, LEVEL_WEAK)
                and not force_reply
                and decision.addressee == "others"
            ):
                trace.reason("ai_addressee_others")
                return None
            reply = self._decision_reply_text(msg, decision)
            if reply:
                trace.reason("ai_reply")
                if decision.follow_up is not None and self._declare_followups:
                    msg["_followup_declared"] = decision.follow_up   # read by _finish_visible_reply
                return reply
            # Nothing was left to send (only an emoji code WeChat does not know):
            # the same as silence, so an explicit call still gets its answer.
            trace.step("reply_empty_after_cleanup")

        if decision.failed:
            logger.warning(
                "Structured reply unusable in %s: %s (%s)",
                msg.get("group_name", msg["chat_id"][:20]),
                decision.error_code, decision.status,
            )
        if force_reply:
            trace.reason("ai_blank_forced_fallback")
            return "啊？"
        # An explicit call (@, quote of the bot, private chat) is never left
        # without an answer -- unless the model explicitly judged that the
        # message was meant for somebody else (impossible in a private chat).
        # A name, a keyword or a follow-up only asks the model; its silence
        # is respected.
        model_declined = (
            not decision.failed
            and decision.addressee == "others"
            and not private
        )
        if addressing.explicit and not model_declined:
            trace.reason("ai_blank_direct_fallback")
            prompt = (clean_content or "").strip()
            if prompt in {"", "在吗", "在不在", "在"}:
                return "嗯，在的"
            return "我刚才没接住，再说一次好不好"
        trace.reason("ai_unusable_answer" if decision.failed else "ai_intentional_silence")
        return None

    def _proactive_with_pipeline(self, msg: dict, mode, context: list[dict]) -> str | None:
        """Proactive path of the structured pipeline: the gate has opened."""
        trace = self._tracer.current()
        chat_id = msg["chat_id"]
        display_name = self._nicks.resolve_name(msg["sender_id"])
        if display_name == msg["sender_id"]:
            display_name = msg["sender_name"]
        addressing = classify(msg, names=self._names_for(msg))
        request = ChatRequest(
            message=pipeline_clean_content(msg.get("content", "")),
            context_messages=tuple(context),
            requester_name=display_name,
            bot_name=self._config.bot_display_name,
            group_name=msg.get("group_name", msg.get("chat_id", "群聊")),
            is_group=True,
            group_memory=self._group_memory_for_prompt(
                msg, pipeline_clean_content(msg.get("content", "")), context,
                ambient=True, proactive=True,
            ),
            addressing=addressing,
            ambient=True,
            mode=mode,
            recent_bot_replies=self._recent_bot_replies(msg),
            call_name="proactive_v2",
            persona=self._persona_settings_for(msg),
            **self._expression_options(msg),
            examples=self._examples_for(msg, pipeline_clean_content(msg.get("content", ""))),
        )
        try:
            decision = self._proactive_summarizer.decide_and_reply(
                request, strategy=self._pipeline_strategy,
            )
        except Exception as e:
            logger.error("Proactive chat API failed: %s", e)
            trace.reason("proactive_api_error", error=type(e).__name__)
            return None
        trace.note(addressing=addressing.as_trace(), decision=decision.as_trace())

        reply = self._decision_reply_text(msg, decision) if decision.is_reply else ""
        if not reply:
            self._proactive.record_eval(chat_id)
            self._proactive.record_silence(chat_id)
            consecutive = self._proactive.get_consecutive_silence(chat_id)
            trace.reason(
                "proactive_unusable_answer" if decision.failed else "proactive_model_silence",
                consecutive=consecutive,
            )
            logger.log(
                logging.WARNING if consecutive >= 3 else logging.INFO,
                "Proactive: AI chose silence (mode=%s consecutive=%d chat=%s)",
                mode.name, consecutive, chat_id[:20],
            )
            return None

        self._proactive.record_speech(chat_id)
        trace.reason("proactive_reply", mode=mode.name)
        logger.info(
            "Proactive reply: mode=%s len=%d → '%s'", mode.name, len(reply), reply[:40],
        )
        return reply

    # ── Welcome handler ─────────────────────────────────────────

    def _handle_welcome(self, msg: dict) -> str | None:
        """Send a welcome message for a new group member.

        Resolves the appropriate welcome template for the group,
        replaces ``{new_member}`` with the new member's identifier,
        and returns the final text.  Returns None if the group has
        explicitly disabled welcome or no template matches.
        """
        from .welcome import get_welcome_manager

        new_member = msg.get("new_member_id", "")
        if not new_member:
            logger.warning("Welcome: missing new_member_id in join event")
            return None

        chat_id = msg["chat_id"]
        wm = get_welcome_manager()
        welcome_text = wm.resolve_message(chat_id, new_member)

        if not welcome_text:
            logger.info(
                "Welcome: skipped for '%s' in %s (disabled or no template)",
                new_member, msg.get("group_name", chat_id[:20]),
            )
            return None

        logger.info(
            "Welcome: new member '%s' in %s → '%s'",
            new_member, msg.get("group_name", chat_id[:20]), welcome_text[:40],
        )
        return welcome_text

    # ── Proactive chat handler ────────────────────────────────────

    def _handle_proactive_chat(self, msg: dict, mode) -> str | None:
        """Generate a spontaneous reply based on recent chat context.

        The mode (ProactiveMode) determines:
          - context_count: how many recent messages to fetch
          - max_chars: hard cap on reply length
          - label/description/instruction: injected into AI prompt

        The AI may return an empty string if it judges the conversation
        inappropriate for interjection — no message is sent in that case.
        """
        now = int(time.time())

        # Fetch recent messages — limit to mode's context window
        window_start = now - self._config.proactive_rate_window_sec
        context = self._store.get_messages_since(
            msg["chat_id"], window_start, limit=mode.context_count,
        )

        trace = self._tracer.current()
        if not context:
            logger.debug("Proactive: no context available for %s", msg["chat_id"])
            trace.reason("proactive_no_context")
            return None

        # Resolve nicknames
        for m in context:
            custom = self._nicks.resolve_name(m["sender_id"])
            if custom != m["sender_id"]:
                m["sender_name"] = custom

        logger.info(
            "Proactive chat: mode=%s context=%d msgs chat=%s",
            mode.name, len(context), msg.get("group_name", msg["chat_id"][:20]),
        )

        if self._pipeline_applies(msg):
            return self._proactive_with_pipeline(msg, mode, context)

        try:
            ai_reply = self._proactive_summarizer.proactive_chat(
                mode=mode,
                context_messages=context,
                bot_name=self._config.bot_display_name,
                group_name=msg.get("group_name", msg.get("chat_id", "群聊")),
                group_memory=self._group_memory_for_prompt(
                    msg, str(msg.get("content", "")), context,
                    ambient=True, proactive=True,
                ),
                **self._persona_kwargs(msg),
            )
        except Exception as e:
            logger.error("Proactive chat API failed: %s", e)
            trace.reason("proactive_api_error", error=type(e).__name__)
            return None

        if not ai_reply:
            self._proactive.record_eval(msg["chat_id"])
            self._proactive.record_silence(msg["chat_id"])
            consecutive = self._proactive.get_consecutive_silence(msg["chat_id"])
            trace.reason("proactive_model_silence", consecutive=consecutive)
            log_level = logging.WARNING if consecutive >= 3 else logging.INFO
            logger.log(
                log_level,
                "Proactive: AI chose silence (mode=%s consecutive=%d chat=%s)",
                mode.name, consecutive, msg["chat_id"][:20],
            )
            return None

        self._proactive.record_speech(msg["chat_id"])
        trace.reason("proactive_reply", mode=mode.name)
        ai_reply = self._nicks.resolve_wxids(ai_reply)
        logger.info(
            "Proactive reply: mode=%s len=%d → '%s'",
            mode.name, len(ai_reply), ai_reply[:40],
        )
        # No @prefix — bot speaks as a natural group member
        return ai_reply
