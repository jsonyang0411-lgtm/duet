"""CLAUDE_RELAY_ONLY=on: the bot relays cc requests and answers nothing else."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_router_priority import BOT, RouterTestCase, StubSummarizer

from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

ME = "wxid_me"


class RelayOnlyRoutes(RouterTestCase):
    def build(self, **config):
        self.summarizer = StubSummarizer()
        router, _ = self.make_router(summarizer=self.summarizer, **config)
        relay_cfg = SimpleNamespace(
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
            claude_relay_max_chars=1500, admin_wxid=ME,
        )
        router._claude_relay = ClaudeRelay(relay_cfg, store=RelayStore(":memory:"), clock=lambda: self.now())
        return router

    def mine(self, message_id, text):
        return self.private_msg(message_id, text, chat_id=ME, sender_id=ME, sender_name="我")

    def test_only_relay_requests_are_handled(self):
        router = self.build(claude_relay="on", claude_relay_only="on", claude_relay_token="t" * 32, admin_wxid=ME,
                            private_chat_enabled=True, welcome_enabled=True, proactive_enabled=True)
        assert router.handle(self.group_msg("g1", f"@{BOT} 你好")) is None               # a mention
        assert router.handle(self.private_msg("p1", "你好呀")) is None                  # someone else's private chat
        assert router.handle(self.mine("p2", "随便聊聊")) is None                         # the owner, not cc
        receipt = router.handle(self.mine("p3", "cc 跑一下测试"))
        assert receipt and "#1" in receipt
        assert self.summarizer.events == []                                             # no model call at all
        stored = router._store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert stored == 4                                                              # still logged

    def test_off_by_default_the_bot_answers_as_before(self):
        router = self.build()
        assert router.handle(self.group_msg("g1", f"@{BOT} 你好")) == "嗯，在。"

    def test_admin_commands_in_private_chat_are_allowed_in_relay_only(self):
        from unittest.mock import MagicMock
        admin_mock = MagicMock()
        admin_mock.handle.side_effect = lambda text, sender: "可用命令" if text == "帮助" else None
        router = self.build(claude_relay="on", claude_relay_only="on", claude_relay_token="t" * 32, admin_wxid=ME,
                            private_chat_enabled=True, admin=admin_mock)
        # Help command from admin works
        reply = router.handle(self.mine("m1", "帮助"))
        assert reply is not None
        assert "可用命令" in reply
        # Non-admin trying help gets ignored
        assert router.handle(self.private_msg("p1", "帮助")) is None
        # Group @bot help gets ignored
        assert router.handle(self.group_msg("g1", f"@{BOT} 帮助")) is None
        # Normal chat from admin still ignored
        assert router.handle(self.mine("m2", "你好呀")) is None

    def test_with_the_real_relay_only_relay_admin_commands_stay_commands(self):
        """As bot.py wires it: the relay takes every line of the owner, except management commands."""
        from unittest.mock import MagicMock
        from src.admin import RELAY_HELP, AdminCommandHandler
        admin = AdminCommandHandler(MagicMock())
        router = self.build(claude_relay="on", claude_relay_only="on", claude_relay_token="t" * 32, admin_wxid=ME,
                            private_chat_enabled=True, admin=admin)
        relay_cfg = SimpleNamespace(
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
            claude_relay_max_chars=1500, admin_wxid=ME, claude_relay_only="on",
        )
        relay = ClaudeRelay(relay_cfg, store=RelayStore(":memory:"), clock=lambda: self.now(),
                            reserved=admin.is_command, extra_help=RELAY_HELP)
        router._claude_relay = relay
        assert "未启用好友管理组件" in router.handle(self.mine("m1", "添加好友 abc 你好"))
        assert "未启用好友管理组件" in router.handle(self.mine("m2", "同意好友"))
        assert "未启用好友管理组件" in router.handle(self.mine("m3", "自动同意好友 开"))
        assert relay.store.get(1) is None                                       # nothing went to the Mac
        help_text = router.handle(self.mine("m4", "帮助"))
        assert "不用打 cc" in help_text and "添加好友 号码 [附言]" in help_text
        assert "#1" in router.handle(self.mine("m5", "跑一下测试"))               # everything else is a task
        assert "#2" in router.handle(self.mine("m6", "cc 添加好友 是什么功能"))    # with cc: a task too
        assert relay.store.get(2)["text"] == "添加好友 是什么功能"


def test_relay_only_needs_the_relay():
    from src.config import _validate_config
    with pytest.raises(RuntimeError, match="CLAUDE_RELAY_ONLY=on needs CLAUDE_RELAY=on"):
        _validate_config({"claude_relay_only": "on"})
    _validate_config({"claude_relay_only": "on", "claude_relay": "on", "claude_relay_token": "t" * 32,
                      "admin_wxid": ME, "claude_relay_prefix": "cc"})
    with pytest.raises(RuntimeError, match="CLAUDE_RELAY_ONLY must be off or on"):
        _validate_config({"claude_relay_only": "maybe"})


def test_the_environment_is_read():
    import os
    from src.config import load_config
    env = {"ANTHROPIC_API_KEY": "k", "CLAUDE_RELAY": "on", "CLAUDE_RELAY_TOKEN": "t" * 32,
           "ADMIN_WXID": ME, "CLAUDE_RELAY_ONLY": "true"}
    with patch.dict(os.environ, env, clear=True):
        assert load_config().claude_relay_only == "on"


class FakeBackend:
    """Starts and returns at once; records what the router answers."""

    def __init__(self, messages):
        self.messages = messages
        self.replies = []

    def start(self, callback):
        for msg in self.messages:
            self.replies.append(callback(msg))

    def stop(self):
        pass

    def send_text(self, chat_id, content):
        return True


def test_a_relay_only_bot_starts_and_runs_without_any_ai_key(tmp_path, monkeypatch):
    """No DEEPSEEK/OPENAI/ANTHROPIC key, AI_BACKEND left at deepseek: the whole start-up and a few messages."""
    import os
    import time as _time
    from src.bot import Bot
    from src.config import load_config
    from src.summarize.no_model import NoModel

    import socket
    monkeypatch.chdir(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    env = {"AI_BACKEND": "deepseek", "DEEPSEEK_API_KEY": "", "CLAUDE_RELAY": "on", "CLAUDE_RELAY_ONLY": "on",
           "CLAUDE_RELAY_TOKEN": "t" * 32, "ADMIN_WXID": ME, "CLAUDE_RELAY_PORT": str(free_port),
           "CLAUDE_RELAY_HOST": "127.0.0.1", "PRIVATE_CHAT_ENABLED": "true",
           "DB_PATH": str(tmp_path / "messages.db"), "LOG_FILE": str(tmp_path / "bot.log"),
           "CLAUDE_RELAY_DB": str(tmp_path / "relay.db"), "DECISION_TRACE_ENABLED": "false"}
    with patch.dict(os.environ, env, clear=True):
        config = load_config()
    now = int(_time.time())
    backend = FakeBackend([
        {"message_id": "g1", "chat_id": "room@chatroom", "group_name": "群", "sender_id": "wxid_x",
         "sender_name": "群友", "content": "@群聊小助手 你好", "msg_type": 1, "timestamp": now,
         "is_at_mentioned": True, "is_group": True},
        {"message_id": "p1", "chat_id": ME, "group_name": "我", "sender_id": ME, "sender_name": "我",
         "content": "cc 只回复：收到", "msg_type": 1, "timestamp": now, "is_at_mentioned": False, "is_group": False},
    ])
    bot = Bot(config)
    seen = {}

    def fake_backend(store=None):
        return backend

    import src.bot as bot_module
    real_create_models = bot_module.create_models

    def spy_create_models(cfg):
        models = real_create_models(cfg)
        seen["chat"] = models.chat
        return models

    monkeypatch.setattr(bot, "_create_wechat_backend", fake_backend)
    monkeypatch.setattr(bot_module, "create_models", spy_create_models)
    monkeypatch.setattr(bot_module.signal, "signal", lambda *a: None)
    bot.run()
    assert isinstance(seen["chat"], NoModel)
    assert backend.replies[0] is None                                    # the group mention: not answered
    assert backend.replies[1] and "#1" in backend.replies[1]             # the relay request: a receipt
