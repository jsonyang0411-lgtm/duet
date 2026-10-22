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
