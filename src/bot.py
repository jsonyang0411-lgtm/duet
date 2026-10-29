"""Bot orchestrator — wires all components and manages the bot lifecycle.

This is the central class that initializes, starts, and gracefully shuts down
the business WeChat gateway: the Claude relay, the PRM copilot or the WeChat edge,
the admin's account commands.  There is no chat model: groups and unknown private
chats are never answered (src/routing/steps.py).
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
from .admin import AdminCommandHandler
from .router import MessageRouter
from .utils.logging_config import setup_logging
from .send.bubbles import configure_sender

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Background health heartbeat: periodic logging + JSON status file.

    Runs in a daemon thread so it never blocks shutdown.
    """

    def __init__(self, router, conn, backend, config: BotConfig,
                 on_tick=None):
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

    def uptime_sec(self) -> int:
        return int(time.time() - self._start_time)

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
                self._on_tick(messages_processed=self._router.messages_processed)
                # Full tick (every 300s): logging + JSON + health checks
                if cycle % self._FULL_TICK_CYCLES == 0:
                    self._tick()
            except Exception:
                logger.exception("Health monitor tick failed")

    def _tick(self) -> None:
        uptime_sec = self.uptime_sec()
        msgs = self._router.messages_processed

        db_status = self._check_db()
        wechat_status = self.wechat_status()

        # Push to Web UI
        self._on_tick(
            uptime_sec=uptime_sec,
            messages_processed=msgs,
            db_ok=db_status == "OK",
        )

        logger.info(
            "HEARTBEAT: uptime=%dm, msgs=%d, db=%s, wechat=%s",
            uptime_sec // 60, msgs, db_status, wechat_status,
        )

        self._write_status_json()

    def _check_db(self) -> str:
        """Check database connection is alive."""
        try:
            self._conn.execute("SELECT 1")
            return "OK"
        except Exception as e:
            return f"ERR:{e}"

    def wechat_status(self) -> str:
        """The backend's own health word, or the WeChat window's HWND."""
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

    def _write_status_json(self) -> None:
        """Write a lightweight status file for external watchdogs."""
        status = {
            "uptime_sec": int(time.time() - self._start_time),
            "messages_processed": self._router.messages_processed,
            "db_ok": self._check_db() == "OK",
            "wechat_backend": self._config.wechat_backend,
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
    """Orchestrates the business WeChat gateway.

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
        self._new_friends = None

    def run(self) -> None:
        """Initialize all components and start the bot. Blocks until stopped."""
        config = self._config

        # ── 1. Logging ──────────────────────────────────────────
        setup_logging(level=config.log_level, log_file=config.log_file)
        self._log_banner()

        # How replies are delivered (typing pauses); see src/send/bubbles.py.
        configure_sender(typing=getattr(config, "send_typing_delay", "off") == "on")

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
        admin_handler = AdminCommandHandler()
        router = MessageRouter(store=store, admin_handler=admin_handler, config=config)

        # 达人商务模块：名单存在同一个数据库里，控制台的「达人名单」页也读写这里。
        # 不挂在路由上（达人的私聊走 PRM 起草）；Mac 端用它做名单合一（src/influencer/prm_bridge.py）。
        # 寄样单、专属佣金走 PRM 的平台动作（jst.sample_order 等），不经过达人模块
        try:
            from .influencer.store import open_store
            influencer_store = open_store(config.db_path)
        except Exception as e:
            logger.warning("Failed to open the influencer list: %s", e)
            influencer_store = None

        # ── 4. Web UI status ────────────────────────────────────
        # (web server already started by desktop.py)
        try:
            from .web.server import update_status
            update_status(wechat_backend=config.wechat_backend)
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
        self._wire_edge_backend(backend, influencer_store)
        self._register_edge_client(backend)

        # CLAUDE_RELAY=on: "cc ..." in an allowed private chat goes to the Mac.
        relay = self._start_claude_relay(config, router, backend)
        # PRM_COPILOT=on: registered creators' messages become silent draft jobs.
        prm = self._start_prm(config, router, backend, relay)
        # PRM_BUYIN_SYNC_MIN: who a 抖音百应 private message is from, by the 达人名单's 抖音号 / 平台 UID
        self._wire_prm_directory(prm, influencer_store)
        # WECHAT_BACKEND=edge 加 PRM_COPILOT=on：名单合一、发送失败告诉 PRM；达人资料在后台线程里查
        party_sync = self._wire_edge_prm(backend, prm, influencer_store)
        if party_sync is not None:
            party_sync.start()
        # PRM_EDGE=on: Windows only receives and sends WeChat for the Mac business service.
        edge = self._start_edge(config, router, backend, relay)
        # New friends (Windows): friend_added events for the Mac (PRM_EDGE), 登记达人's candidates (PRM_COPILOT).
        new_friends = self._start_new_friends(config, backend, prm, edge)
        self._new_friends = new_friends
        # When PC WeChat was not online (Windows): the console's alert, wechat_gap for the Mac, a catch-up after.
        presence = self._start_presence(config, backend, edge)

        self._start_friend_management(config, backend, admin_handler,
                                      on_accepted=new_friends.accepts.note if new_friends is not None else None)
        # 加百应达人微信 (PRM_FRIEND_ADD): its one add per confirmation goes through the admin handler's add_friend_checked
        # (the 添加好友 breaker) and waits while the auto-accept is paused; the admin's 待加达人 / 加达人 N / 不加达人 N
        self._wire_friend_adds(config, backend, admin_handler, prm)
        admin_handler.set_status(lambda: self._status_lines(router, relay, prm, edge))

        # ── 6. Health monitor ───────────────────────────────────
        self._health = HealthMonitor(
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
        #   offload the route callbacks from the poll loop.  The poll
        #   thread submits each message to the pool and returns immediately,
        #   so a slow send in one chat never blocks polling of the other
        #   chats.  Reply sending + WCDB confirmation happen inside
        #   the worker, serialized through a client_lock to keep ctypes
        #   safe.  On shutdown the pool drains with a 30 s timeout.
        try:
            logger.info("Bot is running. Press Ctrl+C to stop.")
            backend.start(router.handle)
        except KeyboardInterrupt:
            pass
        finally:
            if party_sync is not None:
                party_sync.stop()
            if presence is not None:
                presence.stop()
                try:
                    from .web.server import register_presence
                    register_presence(None)
                except Exception:
                    pass
            if new_friends is not None:
                new_friends.stop()
                try:
                    from .web.server import register_new_friends
                    register_new_friends(None)
                except Exception:
                    pass
            if self._auto_friend:
                self._auto_friend.stop()
            if self._health:
                self._health.stop()
            if edge is not None:
                edge.stop()
                relay.attach_edge(None, "")
            self._register_edge_client(None)
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
            if self._conn is not None:
                self._conn.close()
            try:
                self._update_status(running=False)
            except Exception:
                pass
            logger.info("Bot shut down gracefully.")

    # ── Helpers ──────────────────────────────────────────────────

    def _status_lines(self, router, relay, prm, edge) -> list[str]:
        """The lines of the admin's 查状态 (the friend switches are the handler's own)."""
        config = self._config
        health = self._health
        lines = []
        if health is not None:
            lines.append(f"运行：{health.uptime_sec() // 60} 分钟")
            lines.append(f"微信后端：{config.wechat_backend}（{health.wechat_status()}）")
        else:
            lines.append(f"微信后端：{config.wechat_backend}")
        lines.append(f"已处理消息：{router.messages_processed} 条")
        if relay is None:
            lines.append("中转（cc → Mac）：关")
        else:
            lines.append("中转（cc → Mac）：开，" + ("Mac 在线" if relay.mac_online() else "Mac 未连接"))
        lines.append("达人副驾驶（PRM_COPILOT）：" + ("开" if prm is not None else "关"))
        lines.append("微信边缘（PRM_EDGE）：" + ("开" if edge is not None else "关"))
        lines.append("群聊：一律不回复")
        return lines

    @staticmethod
    def _start_new_friends(config, backend, prm, edge):
        """The new-friend scan (src/wechat/new_friends.py), or None: only with a consumer (PRM_EDGE: a
        ``friend_added`` event per new friend; PRM_COPILOT: new friends first in 登记达人's candidates,
        registering one drafts her messages since she was added; and 加百应达人微信's ``on_new_friend``: which
        of its records she may be, never registering anyone) and a backend whose database it can
        read beside the poll loop (Windows wcdb).  Its records are in the bot's database (DB_PATH)."""
        if prm is None and edge is None:
            return None
        if not callable(getattr(backend, "with_client", None)):
            logger.info("New friends: this WeChat backend cannot be read beside its poll loop; not looked for")
            return None
        adds = getattr(prm, "friend_adds", None) if prm is not None else None
        try:
            from .wechat.new_friends import NewFriendService, NewFriendStore
            service = NewFriendService(
                backend, NewFriendStore(config.db_path),
                on_found=[edge.friend_added] if edge is not None else [adds.on_new_friend] if adds is not None else [],
                keep_verify=prm is not None,
                catchup_hours=getattr(config, "prm_catchup_hours", 12))
        except Exception:
            logger.warning("New friends: the scan could not start", exc_info=True)
            return None
        if prm is not None:
            service.set_catch_up_sink(prm.take_new_friend)
            prm.on_registered = service.registered
            if adds is not None:
                adds.set_new_friends(service)     # (a record made after she became a friend looks back)
        try:
            from .web.server import register_new_friends
            register_new_friends(service)
        except Exception:
            pass
        service.start()
        return service

    @staticmethod
    def _start_presence(config, backend, edge):
        """The watch of PC WeChat (src/wechat/presence.py), or None for a backend that cannot tell
        whether WeChat is logged in (only Windows wcdb has ``wechat_presence``).  A stretch without it is
        kept in the bot's database (DB_PATH) and shown in the console until an operator has seen it;
        while it goes on, rows WeChat brings in late are handed on (``note_wechat_offline``); when it
        ends, the creators' chats are read back (``catch_up_after_gap``) and, with PRM_EDGE, the Mac
        gets a ``wechat_gap`` event."""
        probe = getattr(backend, "wechat_presence", None)
        if not callable(probe):
            return None
        try:
            from .wechat.presence import PresenceStore, WechatPresence

            def offline(gap: dict) -> None:
                note = getattr(backend, "note_wechat_offline", None)
                if callable(note):
                    note(gap["from"])

            def back(gap: dict) -> None:
                if edge is not None:
                    edge.wechat_gap(gap)
                catch_up = getattr(backend, "catch_up_after_gap", None)
                if callable(catch_up):
                    catch_up(gap["from"])

            service = WechatPresence(PresenceStore(config.db_path), probe, on_offline=offline, on_back=back)
        except Exception:
            logger.warning("WeChat presence: the watch could not start", exc_info=True)
            return None
        try:
            from .web.server import register_presence
            register_presence(service)
        except Exception:
            pass
        service.start()
        return service

    def _start_friend_management(self, config, backend, admin_handler, on_accepted=None) -> None:
        """The admin's friend commands and, with AUTO_ACCEPT_FRIENDS=on, the auto-accept worker.

        Both drive the WeChat window, so only a backend that has one (Windows wcdb)
        gets them; the manager shares that backend's window lock and reads the screen
        with its own OCR engine (OCR_FALLBACK is a different engine and may be off).
        ``on_accepted(at, label)`` hears every request it accepts (the new-friend scan's
        AcceptLog: a friend the bot let in is ``auto_accept``).  The backend's ``chat_names`` tell the
        manager which window titles are chats (open in windows of their own), never its dialogs.
        """
        window = getattr(backend, "window_controller", None)
        if window is None:
            logger.info("Friend management is off: this backend has no WeChat window to drive")
            return
        try:
            from .wechat.friend_manager import AutoFriendService, FriendManager
            from .wechat.ocr.win_ocr import WindowsOcrEngine
            from .wechat.quiet_send import IdleGate, Win32Desktop
            idle_gate = IdleGate(Win32Desktop(), 1.0, 10.0)
            manager = FriendManager(
                window_controller=window,
                ocr_engine=WindowsOcrEngine(window=window),
                ui_lock=getattr(backend, "window_lock", None),
                idle_gate=idle_gate,
                on_accepted=on_accepted,
                chat_names=getattr(backend, "chat_names", None),
            )
            auto = AutoFriendService(
                manager,
                interval_sec=config.auto_accept_interval_sec,
                enabled=config.auto_accept_friends == "on",
                idle_gate=IdleGate(Win32Desktop(), config.auto_accept_idle_sec, 0),
                idle_sec=config.auto_accept_idle_sec,
                watch=config.auto_accept_watch == "on",
                full_check_sec=config.auto_accept_full_check_sec,
                daily_max=config.auto_accept_daily_max,
                state_path=Path(config.db_path).parent / "auto_accept_state.json",
                notify=self._admin_notifier(config, backend),
            )
        except Exception:
            logger.warning("Friend management could not start", exc_info=True)
            return
        self._friend_mgr, self._auto_friend = manager, auto
        admin_handler.set_friend_manager(manager, auto)
        if auto.enabled:
            auto.start()

    def _wire_friend_adds(self, config, backend, admin_handler, prm) -> None:
        """加百应达人微信 (src/prm/friend_adds.py): the admin handler (``add_friend_checked``; no friend manager, as on the
        Mac: nothing is added there, the number can be copied), the auto-accept service (its pause stops every add)
        and the admin's notifier; the admin's commands.  Without the PRM nothing is wired."""
        if prm is None:
            return
        prm.friend_adds.attach(admin_handler, self._auto_friend, self._admin_notifier(config, backend))
        admin_handler.set_friend_adds(prm.friend_adds)

    @staticmethod
    def _admin_notifier(config, backend):
        """``notify(text)`` for the auto-accept worker: a private message to the admin (ADMIN_WXID) through the
        backend's sender; without an admin or a sender, None (it is only logged)."""
        admin = str(getattr(config, "admin_wxid", "") or "").strip()
        send = getattr(backend, "send_text", None)
        if not admin or not callable(send):
            return None

        def notify(text: str) -> None:
            if not send(admin, text):
                logger.warning("Auto-accept: the admin could not be told: %s", text)
        return notify

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
        # admin handler (route step admin_command), not to the Mac.
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
    def _wire_prm_directory(prm, influencer_store) -> None:
        """The PRM's ``creator_directory``: the 达人名单 (src/influencer) rows that are on (wxid, 平台, 抖音号 / 小红书号, 平台
        UID), by which a 抖音百应 private message is matched to a registered creator (PRM_BUYIN_SYNC_MIN).  Without the
        list (or the PRM) nothing is wired: only the ids the deals' verified 抖店 sample orders found are used."""
        if prm is None or influencer_store is None:
            return
        store = influencer_store

        def directory() -> list:
            return [{"wxid": c["chat_id"], "platform": c.get("platform") or "", "platform_no": c.get("platform_no") or "",
                     "platform_uid": c.get("platform_uid") or ""} for c in store.list_contacts() if c.get("enabled")]

        prm.creator_directory = directory

    @staticmethod
    def _wire_edge_backend(backend, influencer_store) -> None:
        """Mac 端的 edge 后端：达人名单从达人模块来；路由给的回复存进「待处理」当草稿，不直接发给达人（D19）。
        没有达人模块时名单为空，Windows 什么都不转。"""
        from .wechat.edge_backend import EdgeBackend
        if not isinstance(backend, EdgeBackend):
            return
        if influencer_store is None:
            logger.warning("WECHAT_BACKEND=edge 但达人模块没有启动：名单为空，Windows 不会转任何消息")
            return
        from .influencer.store import INBOX_DRAFT
        store = influencer_store
        backend.add_parties_source(lambda: [c["chat_id"] for c in store.list_contacts() if c["enabled"]])
        backend.set_reply_sink(lambda chat_id, reply, msg: store.add_inbox_item(chat_id, INBOX_DRAFT, reply))

    @staticmethod
    def _register_edge_client(backend) -> None:
        """控制台的 /api/edge 读 edge 后端的状态（Windows 连没连上）；不是 edge 后端就不登记。"""
        from .wechat.edge_backend import EdgeBackend
        if backend is not None and not isinstance(backend, EdgeBackend):
            return
        try:
            from .web.server import register_edge_client
            register_edge_client(backend)
        except Exception:
            pass

    @staticmethod
    def _wire_edge_prm(backend, prm, influencer_store=None):
        """Mac 端的 PRM（同事的副驾驶，见 src/prm/service.py）：起草在本机的 worker 沙箱里，发送、读回、
        运营亲手回复都经过 Windows。发送、钩子在 _start_prm 里已经按普通后端接好，这里补名单和发送失败。

        名单合一（src/influencer/prm_bridge.py）：达人模块的名单为准，PRM 的登记跟着它；合作单页上的改动
        也写回名单。edge 后端每拉一轮事件同步一次，同步完的 PRM 名单就是推给 Windows 的名单。
        返回 PartyListSync（调用方 start() 它的达人资料后台线程、关机时 stop()），没接上时返回 None。"""
        from .wechat.edge_backend import EdgeBackend
        if prm is None or not isinstance(backend, EdgeBackend):
            return None
        backend.set_send_failure_hook(prm.on_send_failed)
        from .edge import protocol as P

        def chat_notice(event: dict) -> None:
            """撤回、拍一拍这些私聊系统提示（Windows 报 chat_notice，不当达人的话）也交给 PRM。
            Windows 重启后补交的带 written_at（写的时间），按它算晚了多久；at 是转过来的时间。"""
            if event.get("type") == P.CHAT_NOTICE:
                prm.on_chat_notice(str(event.get("chat_id") or ""), str(event.get("text") or ""),
                                   event.get("written_at") or event.get("at"), event.get("id"))

        backend.add_event_hook(chat_notice)
        if influencer_store is None:
            backend.add_parties_source(prm.store.active_wxids)
            return None
        from .influencer.prm_bridge import PartyListSync
        sync = PartyListSync(influencer_store, prm)
        refresh = prm.on_parties_changed

        def parties_changed() -> None:
            sync.prm_changed()
            if refresh is not None:
                refresh()

        prm.on_parties_changed = parties_changed
        backend.add_parties_source(sync.sync)
        try:
            sync.sync()
        except Exception:
            logger.exception("名单合一：启动时同步失败，edge 后端拉事件时会再试")
        return sync

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
        set_floor = getattr(backend, "set_catchup_floor", None)
        if callable(set_floor):
            set_floor(edge.catchup_floor)          # PRM_CATCHUP_HOURS: what came in while the bot was stopped
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
        logger.info(
            "Business gateway: relay=%s, prm_copilot=%s, prm_edge=%s; no chat model, groups are never answered",
            getattr(config, "claude_relay", "off"), getattr(config, "prm_copilot", "off"),
            getattr(config, "prm_edge", "off"),
        )
        logger.info("Bot name: %r", config.bot_display_name)
        if config.wechat_groups:
            logger.info("Groups: %s", config.wechat_groups)
        logger.info(
            "Private chats: %s",
            "enabled" if config.private_chat_enabled else "disabled",
        )
        logger.info("DB path: %s", config.db_path)
        logger.info("=" * 50)

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
