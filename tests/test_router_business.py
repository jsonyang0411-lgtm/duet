"""The red lines of the business gateway: a group, a stranger or an old chat setting never gets a reply.

The route (src/routing/steps.py) ends every message that is not a relay task, a
registered creator's or the admin's account command in silence.  These tests run
the real router with the real relay and admin handler.
"""

import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import patch

from router_support import ADMIN, BOT, NOW, RouterCase, admin_msg, group_msg, private_msg

from src.admin import RELAY_HELP
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

# The personal chat line's modules: deleted from this code base.
GONE = (
    "src.persona", "src.personas", "src.persona_examples", "src.pipeline", "src.nickname", "src.proactive",
    "src.trigger", "src.fun", "src.welcome", "src.todo", "src.summarize", "src.integrations", "src.memory",
    "src.vision", "src.web_search", "evals",
)


class GatewayCase(RouterCase):
    def gateway(self, *, relay_only=False, **config):
        router = self.router(**config)
        cfg = SimpleNamespace(
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
            claude_relay_max_chars=1500, admin_wxid=ADMIN, claude_relay_only="on" if relay_only else "off",
        )
        self.relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=lambda: NOW,
                                 reserved=router._admin.is_command, extra_help=RELAY_HELP)
        router._claude_relay = self.relay
        return router

    def jobs(self) -> int:
        return len(self.relay.store.list_jobs(limit=1000))


GROUP_MESSAGES = [
    dict(content="大家好"),
    dict(content=f"@{BOT} 你好", is_at_mentioned=True),
    dict(content=f"@{BOT}", is_at_mentioned=True),
    dict(content="你刚才说的不对", is_reply_to_bot=True, quoted_content="上一句"),
    dict(content="总结一下"),
    dict(content=f"{BOT} 在吗"),
    dict(content="cc 跑一下测试", sender_id=ADMIN, sender_name="管理员"),
    dict(content="同意好友", sender_id=ADMIN, sender_name="管理员"),
    dict(content="加达人 1", sender_id=ADMIN, sender_name="管理员"),           # 加百应达人微信's commands (功能 R)
    dict(content="待加达人", sender_id=ADMIN, sender_name="管理员"),
    dict(content=f"@{BOT} 帮助", sender_id=ADMIN, sender_name="管理员", is_at_mentioned=True),
    dict(content="\"新人\"加入了群聊", is_system_join=True, new_member_id="wxid_new"),
    dict(content="[图片]", msg_type=3),
    dict(content="[语音]", msg_type=34),
]


class NoGroupIsEverAnswered(GatewayCase):
    def test_no_group_message_gets_a_reply_or_a_task(self):
        for relay_only in (False, True):
            router = self.gateway(relay_only=relay_only)
            for i, fields in enumerate(GROUP_MESSAGES):
                assert router.handle(group_msg(f"g{relay_only}{i}", **fields)) is None, fields
                assert self.trace.notes["ended_by"] == "silence", fields
            assert self.jobs() == 0

    def test_group_messages_are_still_recorded(self):
        router = self.gateway()
        router.handle(group_msg("g1", f"@{BOT} 你好", is_at_mentioned=True))
        assert self.stored(router, "g1") == 1 and router.messages_processed == 1


class NoStrangerIsEverAnswered(GatewayCase):
    def test_a_private_chat_that_is_no_task_ends_in_silence(self):
        router = self.gateway()
        for i, text in enumerate(("你好", "在吗？", "cc 跑一下测试", "帮助", "同意好友", "总结一下", "加达人 1", "待加达人")):
            assert router.handle(private_msg(f"p{i}", text)) is None, text
        assert self.jobs() == 0

    def test_the_admin_chatting_without_cc_gets_no_reply(self):
        router = self.gateway()
        assert router.handle(admin_msg("a1", "今天天气怎么样")) is None
        assert self.trace.notes["ended_by"] == "silence"


class WhatIsAnswered(GatewayCase):
    def test_the_admins_cc_task_gets_its_receipt(self):
        router = self.gateway()
        receipt = router.handle(admin_msg("a1", "cc 跑一下测试"))
        assert receipt and "#1" in receipt and self.jobs() == 1

    def test_relay_only_takes_every_line_of_the_admin_but_the_account_commands(self):
        router = self.gateway(relay_only=True)
        assert "未启用好友管理组件" in router.handle(admin_msg("a1", "添加好友 abc 你好"))
        assert "未启用好友管理组件" in router.handle(admin_msg("a1b", "添加好友：test_wxid_001 附言"))
        assert router.handle(admin_msg("a2", "查状态")).startswith("账号状态")
        assert self.jobs() == 0                                              # nothing went to the Mac
        assert "#1" in router.handle(admin_msg("a3", "跑一下测试"))
        assert "#2" in router.handle(admin_msg("a4", "cc 添加好友 是什么功能"))   # with cc: a task too
        assert self.relay.store.get(2)["text"] == "添加好友 是什么功能"
        help_text = router.handle(admin_msg("a5", "帮助"))
        assert "不用打 cc" in help_text and "添加好友 号码 [附言]" in help_text and "查状态" in help_text
        assert "加达人 编号" in help_text
        assert "没打开" in router.handle(admin_msg("a6", "加达人 1"))          # a command, never a task for the Mac
        assert self.jobs() == 2


class OldChatSettingsDoNothing(GatewayCase):
    OLD_ENV = {
        "AI_BACKEND": "deepseek", "DEEPSEEK_API_KEY": "", "CHAT_PERSONA_PROFILE": "custom",
        "CHAT_PERSONA_NAME": "某某", "PROACTIVE_ENABLED": "true", "WELCOME_ENABLED": "true",
        "PIPELINE_V2": "single", "MEMORY_V2": "on", "TRIGGER_KEYWORDS": "总结一下", "FUN_ENABLED": "true",
        "TODO_ENABLED": "true", "WEB_SEARCH_ENABLED": "true", "SEND_STICKERS": "on", "MODEL_ROUTE": "local",
        "BOT_DISPLAY_NAME": BOT, "ADMIN_WXID": ADMIN,
    }

    def test_an_old_env_file_loads_without_any_ai_key_and_its_chat_switches_are_gone(self):
        from src.config import load_config
        with patch.dict(os.environ, self.OLD_ENV, clear=True):
            config = load_config()
        for name in ("ai_backend", "chat_persona_profile", "proactive_enabled", "welcome_enabled",
                     "pipeline_v2", "memory_v2", "trigger_keywords", "fun_enabled", "todo_enabled",
                     "web_search_enabled", "send_stickers", "model_route"):
            assert not hasattr(config, name), name
        router = self.gateway(bot_display_name=config.bot_display_name)
        assert router.handle(group_msg("g1", "总结一下")) is None
        assert router.handle(group_msg("g2", f"@{BOT} 你好", is_at_mentioned=True)) is None

    def test_the_router_has_no_model_and_the_chat_modules_are_gone(self):
        router = self.router()
        for name in ("_summarizer", "_detector", "_nicks", "_proactive_gate", "_memory_worker", "_todo"):
            assert not hasattr(router, name), name
        for module in GONE:
            assert importlib.util.find_spec(module) is None, module
