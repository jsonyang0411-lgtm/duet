"""CLAUDE_RELAY_ONLY=on lets "cc" be left out; the whole bot starts and runs without any AI setting.

The route itself (relay-only lines, admin commands, groups never answered) is in
tests/test_router_business.py.
"""

import os
import socket
from unittest.mock import patch

import pytest

ME = "wxid_me"


def test_relay_only_needs_the_relay():
    from src.config import _validate_config
    with pytest.raises(RuntimeError, match="CLAUDE_RELAY_ONLY=on needs CLAUDE_RELAY=on"):
        _validate_config({"claude_relay_only": "on"})
    _validate_config({"claude_relay_only": "on", "claude_relay": "on", "claude_relay_token": "t" * 32,
                      "admin_wxid": ME, "claude_relay_prefix": "cc"})
    with pytest.raises(RuntimeError, match="CLAUDE_RELAY_ONLY must be off or on"):
        _validate_config({"claude_relay_only": "maybe"})


def test_the_environment_is_read():
    from src.config import load_config
    env = {"CLAUDE_RELAY": "on", "CLAUDE_RELAY_TOKEN": "t" * 32, "ADMIN_WXID": ME, "CLAUDE_RELAY_ONLY": "true"}
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


@pytest.mark.parametrize("relay_only", ["on", "off"])
def test_the_bot_starts_and_runs_without_any_ai_setting(tmp_path, monkeypatch, relay_only):
    """No AI_BACKEND, no key: the whole start-up and a few messages."""
    import time as _time
    import src.bot as bot_module
    from src.bot import Bot
    from src.config import load_config

    monkeypatch.chdir(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    env = {"CLAUDE_RELAY": "on", "CLAUDE_RELAY_ONLY": relay_only,
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
        {"message_id": "p2", "chat_id": "wxid_y", "group_name": "别人", "sender_id": "wxid_y", "sender_name": "别人",
         "content": "你好", "msg_type": 1, "timestamp": now, "is_at_mentioned": False, "is_group": False},
    ])
    bot = Bot(config)
    monkeypatch.setattr(bot, "_create_wechat_backend", lambda store=None: backend)
    monkeypatch.setattr(bot_module.signal, "signal", lambda *a: None)
    bot.run()
    assert backend.replies[0] is None                                    # the group mention: not answered
    assert backend.replies[1] and "#1" in backend.replies[1]             # the relay request: a receipt
    assert backend.replies[2] is None                                    # someone else's private chat
