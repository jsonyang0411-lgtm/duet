"""Bubbles and typing pauses, wired through config, router and the three backends."""

import os
import threading
import time
from unittest.mock import patch

import pytest

from test_macos_adaptation import FakeChatlogClient, FakeMacAutomation
from test_router_pipeline import PipelineSummarizer, TraceSpy
from test_router_priority import BOT, CHAT, RouterTestCase, StubWebSearch
from test_wcdb_backend_locking import _Client, _message

from src.config import BotConfig, load_config
from src.pipeline.types import Decision
from src.send import bubbles
from src.send.bubbles import BUBBLE_SEP, BubbleSender, configure_sender, get_sender
from src.wechat.mac_hybrid_backend import MacHybridBackend
from src.wechat.wcdb_backend import WcdbBackend


@pytest.fixture(autouse=True)
def _default_sender():
    configure_sender(typing=False)
    yield
    configure_sender(typing=False)


# ── configuration ───────────────────────────────────────────────────────────

def load(**env):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", **env}, clear=True):
        return load_config()


def test_both_settings_default_to_off_and_are_validated():
    config = BotConfig()
    assert (config.send_bubbles, config.send_typing_delay) == ("off", "off")
    loaded = load()
    assert (loaded.send_bubbles, loaded.send_typing_delay) == ("off", "off")
    on = load(SEND_BUBBLES="true", SEND_TYPING_DELAY="ON")
    assert (on.send_bubbles, on.send_typing_delay) == ("on", "on")
    with pytest.raises(RuntimeError, match="SEND_BUBBLES"):
        load(SEND_BUBBLES="two")
    with pytest.raises(RuntimeError, match="SEND_TYPING_DELAY"):
        load(SEND_TYPING_DELAY="slow")


def test_the_process_wide_sender_can_be_replaced():
    assert get_sender().typing is False
    sender = configure_sender(typing=True, sleep=lambda s: None)
    assert get_sender() is sender and sender.typing is True


def test_the_bot_configures_the_sender_from_the_config():
    import inspect
    import src.bot as bot_module
    source = inspect.getsource(bot_module.Bot.run)
    assert 'configure_sender(typing=getattr(config, "send_typing_delay", "off") == "on")' in source
    assert source.index("configure_sender") < source.index("_create_wechat_backend")


def test_a_reply_without_the_separator_is_sent_untouched():
    sent = []
    sender = BubbleSender(sleep=lambda s: None)
    assert sender.deliver("c", "  两行\n文字  ", lambda text: sent.append(text) or True)
    assert sent == ["  两行\n文字  "]                          # not even stripped: exactly the old call


# ── the router ──────────────────────────────────────────────────────────────

class RouterBubbles(RouterTestCase):
    def build(self, **config):
        model = PipelineSummarizer(Decision(action="reply", messages=("好呀", "一起去爬山"), addressee="me"))
        router, _ = self.make_router(summarizer=model, pipeline_v2="single", web=StubWebSearch(),
                                     memory_v2=config.pop("memory_v2", "off"), **config)
        router._tracer = self.trace = TraceSpy()
        return router, model

    def test_off_joins_the_messages_into_one_line_as_before(self):
        router, _ = self.build()
        assert router.handle(self.group_msg("m1", f"@{BOT} 周末干嘛")) == "好呀 一起去爬山"

    def test_on_keeps_them_as_separate_bubbles(self):
        router, _ = self.build(send_bubbles="on")
        reply = router.handle(self.group_msg("m1", f"@{BOT} 周末干嘛"))
        assert reply == f"好呀{BUBBLE_SEP}一起去爬山"

    def test_a_single_message_is_the_same_either_way(self):
        model = PipelineSummarizer(Decision(action="reply", messages=("好呀",), addressee="me"))
        router, _ = self.make_router(summarizer=model, pipeline_v2="single", web=StubWebSearch(), send_bubbles="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 周末干嘛")) == "好呀"

    def test_what_the_bot_remembers_saying_is_one_plain_line(self):
        router, model = self.build(send_bubbles="on", followup_mode="declared")
        router._followup = type("Spy", (), {"register": lambda self, *a, **k: seen.append(a[1])})()
        seen = []
        router.handle(self.group_msg("m1", f"@{BOT} 周末干嘛"))
        assert router._recent_replies.recent(CHAT) == ("好呀 一起去爬山",)
        assert seen == ["好呀 一起去爬山"]
        assert all(BUBBLE_SEP not in text for text in router._recent_replies.recent(CHAT))

    def test_the_memory_store_gets_the_plain_line_too(self):
        router, _ = self.build(send_bubbles="on", memory_v2="on")
        router._memory_worker.start = lambda: None
        router.handle(self.group_msg("m1", f"@{BOT} 周末干嘛"))
        assert router._mstore.recent_bot_replies(CHAT, 0) == ["好呀 一起去爬山"]


# ── the backends ────────────────────────────────────────────────────────────

def wcdb(sleep=None):
    backend = WcdbBackend(bot_display_name="小助手", groups=["测试群"])
    backend._client = _Client()
    backend._running = True
    return backend


def test_wcdb_sends_each_bubble_in_order_and_releases_the_window_between_them():
    backend = wcdb()
    sent, lock_free_between = [], []
    backend._send_and_confirm = lambda group, talker, text: sent.append(text) or True

    def sleep(seconds):                                     # runs between bubbles
        got = backend._window_lock.acquire(blocking=False)
        lock_free_between.append(got)
        if got:
            backend._window_lock.release()

    configure_sender(typing=False, sleep=sleep)
    backend._handle_message("测试群", "room1@chatroom", _message(), lambda msg: f"好呀{BUBBLE_SEP}一起去爬山")
    assert sent == ["好呀", "一起去爬山"] and lock_free_between == [True]


def test_wcdb_single_reply_goes_through_the_old_path_untouched():
    backend = wcdb()
    sent = []
    backend._send_and_confirm = lambda group, talker, text: sent.append((group, talker, text)) or True
    backend._handle_message("测试群", "room1@chatroom", _message(), lambda msg: "好呀")
    assert sent == [("测试群", "room1@chatroom", "好呀")]


def test_wcdb_stops_after_a_failed_bubble():
    backend = wcdb()
    sent = []
    backend._send_and_confirm = lambda group, talker, text: (sent.append(text), len(sent) < 2)[1]
    configure_sender(typing=False, sleep=lambda s: None)
    backend._handle_message("测试群", "r@chatroom", _message(), lambda msg: BUBBLE_SEP.join(["一", "二", "三"]))
    assert sent == ["一", "二"]


def test_wcdb_typing_mode_waits_before_every_bubble():
    backend = wcdb()
    backend._send_and_confirm = lambda *a: True
    pauses = []
    configure_sender(typing=True, sleep=pauses.append)
    backend._handle_message("测试群", "r@chatroom", _message(), lambda msg: "好呀")
    backend._handle_message("测试群", "r@chatroom", _message("m2"), lambda msg: BUBBLE_SEP.join(["一", "二"]))
    assert len(pauses) == 3 and all(0.4 <= p <= 3.5 for p in pauses)


def test_mac_hybrid_sends_every_bubble_through_send_text():
    reply = f"收到{BUBBLE_SEP}马上看"
    client = FakeChatlogClient([{
        "count": 1, "new_state": {"room1@chatroom": 1780727000},
        "messages": [{"timestamp": 1780726999, "sender": "Alice", "type": "text", "content": "@群聊小助手 在吗",
                      "local_id": 7, "chat": "摸鱼群", "username": "room1@chatroom", "is_group": True}],
    }])
    automation = FakeMacAutomation()
    configure_sender(typing=False, sleep=lambda s: None)
    backend = MacHybridBackend(bot_display_name="群聊小助手", groups=["摸鱼群"], poll_sec=0.01,
                               client=client, automation=automation)
    backend.poll_once(lambda msg: reply)
    assert automation.sent == ["收到", "马上看"]


def test_mac_ui_sends_every_bubble_through_send_text():
    from src.wechat.mac_ui_backend import MacUIBackend
    automation = FakeMacAutomation([["Alice: ping"]])
    configure_sender(typing=False, sleep=lambda s: None)
    backend = MacUIBackend(bot_display_name="群聊小助手", groups=["摸鱼群"], poll_sec=0.01, automation=automation)
    backend.poll_once(lambda msg: f"pong{BUBBLE_SEP}pong2")
    assert automation.sent == ["pong", "pong2"]


# ── only the router may put a separator into a reply ────────────────────────

from test_router_pipeline import MODE                          # noqa: E402


class SeparatorHygieneTests(RouterTestCase):
    """A separator that came from a nickname, a command or echoed text never splits a message."""

    def build(self, *messages, **config):
        model = PipelineSummarizer(Decision(action="reply", messages=tuple(messages), addressee="me"))
        router, _ = self.make_router(summarizer=model, pipeline_v2="single", web=StubWebSearch(), **config)
        router._tracer = TraceSpy()
        return router, model

    def ask(self, router, content=f"@{BOT} 周末干嘛", **overrides):
        return router.handle(self.group_msg("m1", content, **overrides))

    def test_a_separator_inside_one_model_message_cannot_add_a_bubble(self):
        router, _ = self.build(f"好{BUBBLE_SEP}呀", "一起去爬山", send_bubbles="on")
        assert self.ask(router) == f"好呀{BUBBLE_SEP}一起去爬山"

    def test_a_nickname_with_a_separator_cannot_add_a_bubble(self):
        class Nicks:
            def resolve_wxids(self, text):
                return text.replace("wxid_a", f"小{BUBBLE_SEP}明")
            def resolve_name(self, wxid):
                return wxid
        router, _ = self.build("wxid_a 好呀", "一起去爬山", send_bubbles="on", )
        router._nicks = Nicks()
        assert self.ask(router) == f"小明 好呀{BUBBLE_SEP}一起去爬山"

    def test_an_echoed_separator_in_a_single_message_is_removed_with_bubbles_on_or_off(self):
        for mode in ("on", "off"):
            router, _ = self.build(f"a{BUBBLE_SEP}b", send_bubbles=mode)
            assert self.ask(router) == "ab", mode
            assert router._recent_replies.recent(CHAT) == ("ab",), mode

    def test_a_welcome_reply_with_a_separator_in_the_member_name_stays_one_message(self):
        router, _ = self.make_router(welcome_enabled=True)
        reply = router.handle(self.join_msg("j1", member=f"小{BUBBLE_SEP}红"))
        assert reply == "欢迎 @@小红 加入群聊！🎉"

    def test_a_reply_that_is_only_separators_is_no_reply(self):
        router, _ = self.build("好呀")
        router._handle = lambda msg: BUBBLE_SEP + " " + BUBBLE_SEP        # a path that bypasses the reply cleaning
        assert router.handle(self.group_msg("m1", f"@{BOT} 周末干嘛")) is None

    def test_two_messages_that_clean_to_nothing_send_nothing(self):
        for mode in ("on", "off"):
            router, _ = self.build("😂", "👍", send_bubbles=mode)
            assert self.ask(router) is None, mode
            assert len(router._sticky) == 0 and router._followup._entries == {}, mode
            assert router._recent_replies.recent(CHAT) == (), mode

    def test_a_bubble_that_cleans_to_nothing_is_dropped_from_the_reply(self):
        router, _ = self.build("😂", "好呀", send_bubbles="on")
        assert self.ask(router) == "好呀"
        router, _ = self.build("好呀", "😂", "一起去爬山", send_bubbles="on")
        assert self.ask(router) == f"好呀{BUBBLE_SEP}一起去爬山"

    def test_the_proactive_path_sends_bubbles_too(self):
        router, _ = self.build("好呀", "一起去爬山", send_bubbles="on", proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        reply = router.handle(self.other_msg("m1", "周末谁去爬山"))
        assert reply == f"好呀{BUBBLE_SEP}一起去爬山"
        assert router._recent_replies.recent(CHAT) == ("好呀 一起去爬山",)
        router, _ = self.build("好呀", "一起去爬山", proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        assert router.handle(self.other_msg("m1", "周末谁去爬山")) == "好呀 一起去爬山"

    def test_the_image_window_and_followup_keep_the_plain_line(self):
        router, _ = self.build("好呀", "一起去爬山", send_bubbles="on")
        self.ask(router)
        assert router._image_followup._entries[(CHAT, "wxid_user1")].bot_reply == "好呀 一起去爬山"
        assert router._followup._entries[CHAT].bot_reply == "好呀 一起去爬山"


# ── stopping while a reply waits between bubbles ────────────────────────────

def test_wcdb_sends_no_more_bubbles_once_the_backend_has_stopped():
    backend = wcdb()
    sent = []
    backend._send_and_confirm = lambda group, talker, text: sent.append(text) or True

    def sleep(seconds):                                     # the backend is stopped during the pause
        backend._running = False

    configure_sender(typing=False, sleep=sleep)
    backend._handle_message("测试群", "r@chatroom", _message(), lambda msg: BUBBLE_SEP.join(["一", "二", "三"]))
    assert sent == ["一"]


def test_wcdb_does_not_start_a_typing_pause_on_a_stopped_backend():
    backend = wcdb()
    backend._send_and_confirm = lambda *a: True
    pauses = []
    configure_sender(typing=True, sleep=pauses.append)
    backend._running = False
    backend._handle_message("测试群", "r@chatroom", _message(), lambda msg: BUBBLE_SEP.join(["一", "二"]))
    assert pauses == []


def test_the_bot_configures_the_sender_before_it_builds_anything_else():
    import src.bot as bot_module

    class Stop(Exception):
        pass

    def stop_at_database(path):
        raise Stop

    cfg = BotConfig(send_typing_delay="on", log_file="")
    configure_sender(typing=False)
    with patch.object(bot_module, "setup_logging"), patch.object(bot_module.Bot, "_log_banner"), \
            patch.object(bot_module, "initialize_db", stop_at_database):
        with pytest.raises(Stop):
            bot_module.Bot(cfg).run()
    assert get_sender().typing is True
    configure_sender(typing=False)
    with patch.object(bot_module, "setup_logging"), patch.object(bot_module.Bot, "_log_banner"), \
            patch.object(bot_module, "initialize_db", stop_at_database):
        with pytest.raises(Stop):
            bot_module.Bot(BotConfig(send_typing_delay="off", log_file="")).run()
    assert get_sender().typing is False


def test_the_window_lock_is_not_held_while_the_typing_pause_runs():
    backend = wcdb()
    seen = []
    backend._send_and_confirm = lambda *a: True

    def sleep(seconds):
        got = backend._window_lock.acquire(blocking=False)
        seen.append(got)
        if got:
            backend._window_lock.release()

    configure_sender(typing=True, sleep=sleep)
    backend._handle_message("测试群", "r@chatroom", _message(), lambda msg: "好呀")
    assert seen == [True]
