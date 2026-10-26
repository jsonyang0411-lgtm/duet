"""Bot orchestrator — wires all components and manages the bot lifecycle.

This is the central class that initializes, starts, and gracefully shuts down
the WeChat summarizer bot. It replaces the inline wiring previously in main.py.
"""

import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from .config import BotConfig, PROJECT_ROOT
from .db import initialize_db, MessageStore
from .summarize import create_models, relay_only
from .trigger import TriggerDetector
from .nickname import NicknameService
from .admin import AdminCommandHandler
from .router import MessageRouter
from .integrations.feishu import FeishuExportService
from .utils.logging_config import setup_logging
from .send.bubbles import configure_sender
from .send.stickers import StickerLibrary, configure_stickers

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Background health heartbeat: periodic logging + JSON status file.

    Runs in a daemon thread so it never blocks shutdown.
    """

    def __init__(self, summarizer, router, conn, backend, config: BotConfig,
                 on_tick=None):
        self._summarizer = summarizer
        self._router = router
        self._conn = conn
        self._backend = backend
        self._config = config
        self._on_tick = on_tick or (lambda **kw: None)
        self._start_time = time.time()
        self._running = False
        self._thread: threading.Thread | None = None

    # ── Public API ──────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Health monitor started (interval=5m, daemon)")

    def stop(self) -> None:
        self._running = False

    # ── Internals ───────────────────────────────────────────────────

    _FAST_TICK_SEC = 30    # push message count to dashboard every 30s
    _FULL_TICK_CYCLES = 10  # full health check every 10 fast ticks (5 min)

    def _run(self) -> None:
        cycle = 0
        while self._running:
            time.sleep(self._FAST_TICK_SEC)
            if not self._running:
                break
            cycle += 1
            try:
                # Fast tick (every 30s): push live stats to dashboard
                self._on_tick(
                    messages_processed=self._router.messages_processed,
                    last_api_call_time=self._summarizer.last_api_call_time,
                    last_api_call_sec_ago=(
                        int(time.time() - self._summarizer.last_api_call_time)
                        if self._summarizer.last_api_call_time > 0 else -1
                    ),
                )
                # Full tick (every 300s): logging + JSON + health checks
                if cycle % self._FULL_TICK_CYCLES == 0:
                    self._tick()
            except Exception:
                logger.exception("Health monitor tick failed")

    def _tick(self) -> None:
        uptime_sec = int(time.time() - self._start_time)
        uptime_min = uptime_sec // 60
        msgs = self._router.messages_processed

        db_status = self._check_db()
        wechat_status = self._check_wechat_hwnd()
        last_api_str = self._last_api_ago()

        # Push to Web UI
        self._on_tick(
            uptime_sec=uptime_sec,
            messages_processed=msgs,
            db_ok=db_status == "OK",
            last_api_call_time=self._summarizer.last_api_call_time,
            last_api_call_sec_ago=int(time.time() - self._summarizer.last_api_call_time)
                if self._summarizer.last_api_call_time > 0 else -1,
        )

        logger.info(
            "HEARTBEAT: uptime=%dm, msgs=%d, db=%s, wechat=%s, last_api=%s",
            uptime_min, msgs, db_status, wechat_status, last_api_str,
        )

        self._write_status_json()

    def _check_db(self) -> str:
        """Check database connection is alive."""
        try:
            self._conn.execute("SELECT 1")
            return "OK"
        except Exception as e:
            return f"ERR:{e}"

    def _check_wechat_hwnd(self) -> str:
        """Check WeChat window HWND."""
        try:
            health_status = getattr(self._backend, "health_status", None)
            if callable(health_status):
                return str(health_status())

            wc = getattr(self._backend, "_window", None)
            if wc is None:
                return f"{self._config.wechat_backend}_ok"
            hwnd = wc._cached_hwnd
            if hwnd is not None:
                if wc._validate_hwnd(hwnd):
                    return f"HWND_{hwnd}"
            return "no_hwnd"
        except Exception as e:
            return f"ERR:{e}"

    def _last_api_ago(self) -> str:
        """Human-readable 'time since last successful API call'."""
        last = self._summarizer.last_api_call_time
        if last <= 0:
            return "never"
        ago = int(time.time() - last)
        if ago < 60:
            return f"{ago}s_ago"
        elif ago < 3600:
            return f"{ago // 60}m_ago"
        else:
            return f"{ago // 3600}h_ago"

    def _write_status_json(self) -> None:
        """Write a lightweight status file for external watchdogs."""
        status = {
            "uptime_sec": int(time.time() - self._start_time),
            "messages_processed": self._router.messages_processed,
            "db_ok": self._check_db() == "OK",
            "wechat_backend": self._config.wechat_backend,
            "last_api_call_sec_ago": (
                int(time.time() - self._summarizer.last_api_call_time)
                if self._summarizer.last_api_call_time > 0
                else -1
            ),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        out_dir = PROJECT_ROOT / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "bot_status.json"
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)  # atomic write
        except Exception:
            logger.exception("Failed to write status JSON")


class Bot:
    """Orchestrates the WeChat summarizer bot.

    Usage:
        config = load_config()
        bot = Bot(config)
        bot.run()
    """

    def __init__(self, config: BotConfig):
        self._config = config
        self._conn = None
        self._backend = None
        self._health: HealthMonitor | None = None
        self._friend_mgr = None
        self._auto_friend = None

    def run(self) -> None:
        """Initialize all components and start the bot. Blocks until stopped."""
        config = self._config

        # ── 1. Logging ──────────────────────────────────────────
        setup_logging(level=config.log_level, log_file=config.log_file)
        self._log_banner()

        # How replies are delivered (bubbles, typing pauses); see src/send/bubbles.py.
        configure_sender(typing=getattr(config, "send_typing_delay", "off") == "on")
        self._configure_expressions(config)

        # ── 2. Database ─────────────────────────────────────────
        self._conn = initialize_db(config.db_path)
        store = MessageStore(self._conn)

        # Notify Web UI early: database is ready
        try:
            from .web.server import update_status as _us
            _us(db_ok=True)
        except Exception:
            pass

        # ── 3. Components ───────────────────────────────────────
        detector = TriggerDetector(
            keywords=config.trigger_keywords,
            bot_display_name=config.bot_display_name,
        )
        # MODEL_ROUTE: one cloud model (as before), the local one, or split by task.
        models = create_models(config)
        summarizer = models.chat
        nickname_service = NicknameService()
        admin_handler = AdminCommandHandler(nickname_service)
        feishu_export_service = FeishuExportService(
            config=config,
            store=store,
            summarizer=summarizer,
        )

        # 达人商务模块：名单、草稿和审批单存在同一个数据库里，控制台的"达人审批"页也读写这里
        try:
            from .influencer.handler import build_influencer_handler
            influencer_handler = build_influencer_handler(config.db_path)
        except Exception as e:
            logger.warning("Failed to initialize InfluencerBusinessHandler: %s", e)
            influencer_handler = None

        router = MessageRouter(
            store=store,
            detector=detector,
            summarizer=summarizer,
            admin_handler=admin_handler,
            nickname_service=nickname_service,
            config=config,
            feishu_export_service=feishu_export_service,
            proactive_summarizer=models.proactive,
            memory_summarizer=models.memory,
            influencer_handler=influencer_handler,
        )

        # ── 4. Web UI status ────────────────────────────────────
        # (web server already started by desktop.py)
        try:
            from .web.server import update_status
            from .summarize import model_label
            update_status(
                wechat_backend=config.wechat_backend,
                ai_backend=model_label(config),
            )
            self._update_status = update_status
        except Exception as e:
            logger.debug("Web UI status: %s", e)
            self._update_status = lambda **kw: None

        # ── 5. WeChat backend ───────────────────────────────────
        backend = self._create_wechat_backend(store)
        self._backend = backend
        self.backend = backend   # public ref for lifecycle control

        # Register backend with web server for stop/restart (explicit
        # API — no monkey-patching needed).
        try:
            from .web.server import _register_backend
            _register_backend(backend)
        except Exception:
            pass

        # WECHAT_BACKEND=edge（Mac 端）：名单从达人模块来，回复存成待审核的草稿、不直接发（D19）
        self._wire_edge_backend(backend, influencer_handler)

        # CLAUDE_RELAY=on: "cc ..." in an allowed private chat goes to the Mac.
        relay = self._start_claude_relay(config, router, backend)
        # PRM_COPILOT=on: registered creators' messages become silent draft jobs.
        prm = self._start_prm(config, router, backend, relay)
        # PRM_EDGE=on: Windows only receives and sends WeChat for the Mac business service.
        edge = self._start_edge(config, router, backend, relay)

        self._start_friend_management(config, backend, admin_handler)

        # ── 6. Health monitor ───────────────────────────────────
        self._health = HealthMonitor(
            summarizer=summarizer,
            router=router,
            conn=self._conn,
            backend=backend,
            config=config,
            on_tick=self._update_status,
        )
        self._health.start()

        # ── 7. Signal handling ──────────────────────────────────
        def shutdown(signum, frame):
            logger.info("Received signal %d. Shutting down...", signum)
            if self._auto_friend:
                self._auto_friend.stop()
            backend.stop()
            if self._health:
                self._health.stop()

        try:
            signal.signal(signal.SIGINT, shutdown)
            signal.signal(signal.SIGTERM, shutdown)
        except ValueError:
            # Running in a thread — signals not available
            pass

        # ── 8. Start listening (blocks) ─────────────────────────
        #
        # DESIGN NOTE — fire-and-forget callback execution:
        #   WcdbBackend uses a ThreadPoolExecutor (max_workers=4) to
        #   offload AI-triggering callbacks from the poll loop.  The poll
        #   thread submits each message to the pool and returns immediately,
        #   so a slow summarization in one group never blocks polling of
        #   other groups.  Reply sending + WCDB confirmation happen inside
        #   the worker, serialized through a client_lock to keep ctypes
        #   safe.  On shutdown the pool drains with a 30 s timeout.
        #
        # Legacy design (pre-2026-06):
        #   The old single-threaded loop caused head-of-line blocking:
        #   one slow AI call delayed ALL groups' message polling.
        #   The old comment is archived in AUDIT.md §C1.
        try:
            logger.info("Bot is running. Press Ctrl+C to stop.")
            backend.start(router.handle)
        except KeyboardInterrupt:
            pass
        finally:
            if self._auto_friend:
                self._auto_friend.stop()
            if self._health:
                self._health.stop()
            if edge is not None:
                edge.stop()
                relay.attach_edge(None, "")
            if prm is not None:
                prm.stop()
                try:
                    from .web.server import register_prm
                    register_prm(None)
                except Exception:
                    pass
            if relay is not None:
                relay.stop()
                try:
                    from .web.server import register_relay
                    register_relay(None)
                except Exception:
                    pass
            # Stop the memory thread before its database goes away.
            close_router = getattr(router, "close", None)
            if callable(close_router):
                try:
                    close_router()
                except Exception:
                    logger.exception("Router shutdown failed")
            if self._conn is not None:
                self._conn.close()
            if hasattr(self, 'backend') and hasattr(self.backend, 'router'):
                pass  # router cleanup handled by backend shutdown
            try:
                self._update_status(running=False)
            except Exception:
                pass
            logger.info("Bot shut down gracefully.")

    # ── Helpers ──────────────────────────────────────────────────

    def _start_friend_management(self, config, backend, admin_handler) -> None:
        """The admin's friend commands and, with AUTO_ACCEPT_FRIENDS=on, the auto-accept worker.

        Both drive the WeChat window, so only a backend that has one (Windows wcdb)
        gets them; the manager shares that backend's window lock and reads the screen
        with its own OCR engine (OCR_FALLBACK is a different engine and may be off).
        """
        window = getattr(backend, "window_controller", None)
        if window is None:
            logger.info("Friend management is off: this backend has no WeChat window to drive")
            return
        try:
            from .wechat.friend_manager import AutoFriendService, FriendManager
            from .wechat.ocr.win_ocr import WindowsOcrEngine
            from .wechat.quiet_send import IdleGate, Win32Desktop
            manager = FriendManager(
                window_controller=window,
                ocr_engine=WindowsOcrEngine(window=window),
                ui_lock=getattr(backend, "window_lock", None),
            )
            auto = AutoFriendService(
                manager,
                interval_sec=config.auto_accept_interval_sec,
                enabled=config.auto_accept_friends == "on",
                idle_gate=IdleGate(Win32Desktop(), config.auto_accept_idle_sec, 0),
                idle_sec=config.auto_accept_idle_sec,
                watch=config.auto_accept_watch == "on",
                full_check_sec=config.auto_accept_full_check_sec,
            )
        except Exception:
            logger.warning("Friend management could not start", exc_info=True)
            return
        self._friend_mgr, self._auto_friend = manager, auto
        admin_handler.set_friend_manager(manager, auto)
        if auto.enabled:
            auto.start()

    @staticmethod
    def _start_claude_relay(config, router, backend):
        """The Claude relay (src/relay) with the backend as its sender, or None when off.

        If its port cannot be opened the requests are still queued (the
        receipt then says the Mac is offline) and the error is in the log.
        """
        if getattr(config, "claude_relay", "off") != "on":
            return None
        from .admin import RELAY_HELP
        from .relay import ClaudeRelay
        # When the bot only relays, the admin's management commands still go to the
        # admin handler (route step relay_only), not to the Mac.
        is_command = getattr(getattr(router, "_admin", None), "is_command", None)
        relay = ClaudeRelay(config, send=backend.send_text, send_file=getattr(backend, "send_file", None),
                            reserved=is_command, extra_help=RELAY_HELP if is_command else "")
        try:
            relay.start()
        except OSError:
            logger.exception(
                "CLAUDE_RELAY: cannot listen on %s:%s; the Mac cannot reach this bot",
                config.claude_relay_host, config.claude_relay_port,
            )
        router._claude_relay = relay
        try:
            from .web.server import register_relay
            register_relay(relay)
        except Exception:
            pass
        return relay

    @staticmethod
    def _start_prm(config, router, backend, relay):
        """The PRM copilot (src/prm) on top of the relay, or None when off.

        The WeChat backend learns to read the registered creators' private chats
        (set_private_filter, Windows only); the console finds the service through
        register_prm.
        """
        if getattr(config, "prm_copilot", "off") != "on":
            return None
        if relay is None:
            logger.warning("PRM_COPILOT is on but the Claude relay is not: creators' messages are not drafted")
            return None
        from .prm import PrmService
        store = getattr(router, "_store", None)
        prm = PrmService(config, relay, send=backend.send_text, send_file=getattr(backend, "send_file", None),
                         on_parties_changed=getattr(backend, "refresh_private_chats_soon", None),
                         scrub_messages=getattr(store, "rewrite_chat", None))
        set_filter = getattr(backend, "set_private_filter", None)
        if callable(set_filter):
            set_filter(prm.is_party_chat)
        else:
            logger.warning("PRM_COPILOT: this WeChat backend cannot read extra private chats; "
                           "add the creators to PRIVATE_CHATS")
        set_hook = getattr(backend, "set_own_message_hook", None)
        if callable(set_hook):
            set_hook(prm.on_own_message)           # an operator replying by hand takes the chat over
        else:
            logger.warning("PRM_COPILOT: this WeChat backend does not report this account's own lines; "
                           "an operator's own replies will not take a chat over")
        prm.start()
        router._prm = prm
        try:
            from .web.server import register_prm
            register_prm(prm)
        except Exception:
            pass
        logger.info("PRM_COPILOT: %d registered creator(s)", len(prm.store.active_wxids()))
        return prm

    @staticmethod
    def _wire_edge_backend(backend, influencer_handler) -> None:
        """Mac 端的 edge 后端：达人名单从达人模块来；路由给的回复存进「待处理」当草稿，不直接发给达人（D19）。
        没有达人模块时名单为空，Windows 什么都不转。"""
        from .wechat.edge_backend import EdgeBackend
        if not isinstance(backend, EdgeBackend):
            return
        if influencer_handler is None:
            logger.warning("WECHAT_BACKEND=edge 但达人模块没有启动：名单为空，Windows 不会转任何消息")
            return
        from .influencer.store import INBOX_DRAFT
        store = influencer_handler.store
        backend.set_parties_source(lambda: [c["chat_id"] for c in store.list_contacts() if c["enabled"]])
        backend.set_reply_sink(lambda chat_id, reply, msg: store.add_inbox_item(chat_id, INBOX_DRAFT, reply))

    @staticmethod
    def _start_edge(config, router, backend, relay):
        """The WeChat edge (src/edge) on the relay's port, or None when off.

        Listed creators' private chats are read (set_private_filter) and forwarded as events
        the Mac pulls; this account's own lines there are read back or reported as an
        operator's (set_own_message_hook); the Mac's send requests go out through the backend.
        """
        if getattr(config, "prm_edge", "off") != "on":
            return None
        if relay is None:
            logger.warning("PRM_EDGE is on but the Claude relay is not: /edge/* has no server")
            return None
        from .edge.service import EdgeService
        edge = EdgeService(
            config, send_text=backend.send_text, send_file=getattr(backend, "send_file", None),
            own_wxid=getattr(backend, "own_wxid", None), wechat_online=getattr(backend, "wechat_window_found", None),
            db_readable=getattr(backend, "db_readable", None),
            on_parties_changed=getattr(backend, "refresh_private_chats_soon", None))
        set_filter = getattr(backend, "set_private_filter", None)
        set_hook = getattr(backend, "set_own_message_hook", None)
        if callable(set_filter):
            set_filter(edge.is_party_chat)
        else:
            logger.warning("PRM_EDGE: this WeChat backend cannot read extra private chats")
        if callable(set_hook):
            set_hook(edge.on_own_message)
        else:
            logger.warning("PRM_EDGE: this WeChat backend does not report this account's own lines; "
                           "sends are not read back and operators' own replies are not reported")
        relay.attach_edge(edge, config.prm_edge_token)
        edge.start()
        router._edge = edge
        return edge

    def _log_banner(self) -> None:
        """Log the startup banner with configuration details."""
        config = self._config
        logger.info("=" * 50)
        logger.info("webot starting...")
        logger.info("WeChat backend: %s", config.wechat_backend)
        route = getattr(config, "model_route", "cloud")
        if relay_only(config):
            logger.info("AI: none (CLAUDE_RELAY_ONLY: the bot only relays cc requests)")
        elif route != "local":
            logger.info("AI backend: %s", config.ai_backend)
            if config.ai_backend == "deepseek":
                logger.info("Model: %s", config.deepseek_model)
            else:
                logger.info("Model: %s", config.summarize_model)
        if route != "cloud" and not relay_only(config):
            logger.info("Local model: %s at %s (MODEL_ROUTE=%s)",
                        config.local_model, config.local_base_url, route)
        logger.info("Bot name: %r", config.bot_display_name)
        if config.wechat_groups:
            logger.info("Groups: %s", config.wechat_groups)
        logger.info(
            "Private chats: %s",
            "enabled" if config.private_chat_enabled else "disabled",
        )
        logger.info("DB path: %s", config.db_path)
        logger.info("=" * 50)

    @staticmethod
    def _configure_expressions(config) -> None:
        """Load the sticker library (SEND_STICKERS) and say when a switch cannot work."""
        stickers_on = getattr(config, "send_stickers", "off") == "on"
        library = StickerLibrary.load(getattr(config, "sticker_dir", "data/stickers")) if stickers_on else StickerLibrary()
        configure_stickers(library)
        if stickers_on:
            logger.info("Stickers: %d usable in %s", len(library), getattr(config, "sticker_dir", ""))
            if getattr(config, "wechat_backend", "wcdb") != "wcdb":
                logger.warning("SEND_STICKERS: the %s backend cannot send pictures; no sticker is offered",
                               getattr(config, "wechat_backend", ""))
        wanted = [name for name, on in (("WECHAT_EMOJI", getattr(config, "wechat_emoji", "off") == "on"),
                                        ("SEND_STICKERS", stickers_on)) if on]
        if wanted and getattr(config, "pipeline_v2", "off") == "off":
            logger.warning("%s only work with PIPELINE_V2=single or two_step; with PIPELINE_V2=off they do nothing",
                           " and ".join(wanted))

    def _create_wechat_backend(self, store=None):
        """Create the appropriate WeChat backend based on config.

        Returns an AbstractWeChatBackend instance.
        """
        config = self._config
        groups = [
            g.strip() for g in config.wechat_groups.split(",") if g.strip()
        ]

        if config.wechat_backend == "wcdb":
            from .wechat.wcdb_backend import WcdbBackend
            return WcdbBackend(
                bot_display_name=config.bot_display_name,
                groups=groups,
                poll_sec=config.poll_interval_sec,
                store=store,
                config=config,
            )

        if config.wechat_backend == "mac_ui":
            from .wechat.mac_ui_backend import MacUIBackend
            return MacUIBackend(
                bot_display_name=config.bot_display_name,
                groups=groups,
                poll_sec=config.poll_interval_sec,
                store=store,
            )

        if config.wechat_backend == "edge":
            # Mac 端：通过 Windows 的微信边缘收发（D18），这台 Mac 不登录微信
            from .wechat.edge_backend import EdgeBackend
            return EdgeBackend(config=config)

        if config.wechat_backend == "mac_hybrid":
            from .wechat.mac_hybrid_backend import MacHybridBackend
            return MacHybridBackend(
                bot_display_name=config.bot_display_name,
                groups=groups,
                poll_sec=config.poll_interval_sec,
                store=store,
                config=config,
            )

        else:
            raise ValueError(
                f"Unknown WECHAT_BACKEND: '{config.wechat_backend}'. "
                f"Supported: wcdb, mac_ui, mac_hybrid, edge."
            )
