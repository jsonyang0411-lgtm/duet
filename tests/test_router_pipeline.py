"""MessageRouter with the structured pipeline (PIPELINE_V2) switched on.

The legacy route is pinned by test_router_priority.py; here we check that with
``off`` nothing changes and that ``single`` / ``two_step`` hand messages to
``decide_and_reply`` with the right addressing, and turn its decision into the
right reply (or silence, or a fallback).
"""

import time

from test_pipeline_engine import ScriptedSummarizer
from test_router_priority import (
    AT_MENTION_MAX_AGE_SEC,
    BOT,
    CHAT,
    NOW,
    RouterTestCase,
    StubSummarizer,
    StubWebSearch,
    YANG_BOT,
    YANG_GROUP,
)

from src.persona import PERSONA_PROFILE_YANG_YUJING
from src.pipeline.addressing import (
    LEVEL_AMBIENT,
    LEVEL_EXPLICIT,
    LEVEL_STRONG,
    LEVEL_WEAK,
)
from src.pipeline.types import STATUS_OK, STATUS_PARSE_FAILED, STATUS_EMPTY, Decision
from src.proactive.modes import ProactiveMode

MODE = ProactiveMode("CASUAL", "闲聊", "正常聊天", "自然接话", 4.0, 120, 0.25, 50, 50)


def reply(text="好呀", addressee="me", **kwargs):
    return Decision(action="reply", messages=(text,), addressee=addressee, **kwargs)


def silent(addressee="group", **kwargs):
    return Decision(action="silent", addressee=addressee, **kwargs)


def failed(status=STATUS_PARSE_FAILED, code="no_json"):
    return Decision(action="silent", status=status, error_code=code)


class PipelineSummarizer(StubSummarizer):
    """StubSummarizer that also speaks the structured interface."""

    def __init__(self, decision=None):
        super().__init__()
        self.decision = decision if decision is not None else reply()
        self.decide_requests = []
        self.decide_strategies = []

    def decide_and_reply(self, request, *, strategy="single"):
        self.events.append("decide_and_reply")
        self.decide_requests.append(request)
        self.decide_strategies.append(strategy)
        if self.error:
            raise self.error
        decision = self.decision
        return decision(request) if callable(decision) else decision


class TraceSpy:
    """Collects what the router reports to the decision trace."""

    enabled = False

    def __init__(self):
        self.notes = {}
        self.reasons = []

    def begin(self, msg):
        return self

    def current(self):
        return self

    def note(self, **info):
        self.notes.update(info)

    def reason(self, code, **info):
        self.reasons.append(code)

    def step(self, code, **info):
        pass

    def ignore(self, code, **info):
        self.reasons.append(code)

    def add_llm_call(self, call_id, latency_ms):
        pass

    def finish_reply(self, reply_text):
        pass

    def finish_error(self, exc):
        pass

    def record_llm_call(self, **fields):
        pass


class PipelineTestCase(RouterTestCase):
    def pipeline_router(self, strategy="single", decision=None, **config):
        summarizer = PipelineSummarizer(decision)
        router, _ = self.make_router(
            summarizer=summarizer, pipeline_v2=strategy, **config,
        )
        router._tracer = self.trace = TraceSpy()
        return router, summarizer


class SwitchTests(PipelineTestCase):
    def test_off_keeps_the_legacy_chat_call(self):
        router, summarizer = self.pipeline_router("off")
        result = router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert result == "嗯，在。"
        assert summarizer.events == ["chat"]

    def test_a_summarizer_without_the_structured_interface_stays_on_chat(self):
        summarizer = StubSummarizer()
        router, _ = self.make_router(summarizer=summarizer, pipeline_v2="single")
        assert router.handle(self.group_msg("m1", f"@{BOT} 你好")) == "嗯，在。"
        assert summarizer.events == ["chat"]

    def test_off_does_not_treat_a_name_as_a_call(self):
        router, summarizer = self.pipeline_router("off")
        self.gate(router, allow=False)
        assert router.handle(self.group_msg("m1", f"{BOT}你觉得呢", at=False)) is None
        assert summarizer.events == []

    def test_unknown_strategy_in_config_means_off(self):
        router, summarizer = self.pipeline_router("bogus-mode")
        assert router._pipeline_strategy == "off"


class ExplicitCallTests(PipelineTestCase):
    def test_an_at_mention_goes_through_the_engine_with_explicit_addressing(self):
        router, summarizer = self.pipeline_router("single", reply("在呢"))
        result = router.handle(self.group_msg("m1", f"@{BOT} 今晚吃什么"))

        assert result == "在呢"
        assert summarizer.events == ["decide_and_reply"]
        (request,) = summarizer.decide_requests
        assert request.message == "今晚吃什么"
        assert request.addressing.level == LEVEL_EXPLICIT
        assert request.addressing.signals == ("at",)
        assert (request.requester_name, request.bot_name) == ("小明", BOT)
        assert request.group_name == "测试群" and request.is_group
        assert request.call_name == "chat_v2" and not request.ambient
        assert summarizer.decide_strategies == ["single"]

    def test_the_strategy_is_passed_through(self):
        router, summarizer = self.pipeline_router("two_step")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert summarizer.decide_strategies == ["two_step"]

    def test_context_includes_the_newest_lines_and_resolved_nicknames(self):
        router, summarizer = self.pipeline_router("single")
        router.handle(self.other_msg("m0", "先聊点别的"))
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        request = summarizer.decide_requests[-1]
        assert [m["content"] for m in request.context_messages] == ["先聊点别的", f"@{BOT} 你好"]

    def test_the_legacy_trigger_hint_sentences_are_not_in_the_structured_message(self):
        router, summarizer = self.pipeline_router("single")
        router.handle(self.group_msg("m1", f"@{BOT}"))                 # bare @: sticky
        router.handle(self.group_msg("m2", "那明天呢", at=False))
        request = summarizer.decide_requests[-1]
        assert request.message == "那明天呢"
        assert "回复空白" not in request.message and "连续聊天" not in request.message
        assert request.addressing.level == LEVEL_STRONG
        assert "sticky" in request.addressing.signals

    def test_what_the_bot_said_before_is_handed_back(self):
        router, summarizer = self.pipeline_router("single", reply("第一句回复"))
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        router.handle(self.group_msg("m2", f"@{BOT} 再来"))
        assert summarizer.decide_requests[0].recent_bot_replies == ()
        assert summarizer.decide_requests[1].recent_bot_replies == ("第一句回复",)

    def test_private_chat_is_explicit_and_replies(self):
        router, summarizer = self.pipeline_router("single", reply("私聊回复"))
        result = router.handle(self.private_msg("p1", "在吗"))
        assert result == "私聊回复"
        (request,) = summarizer.decide_requests
        assert request.addressing.signals == ("private",)
        assert not request.is_group

    def test_commands_still_run_before_the_engine(self):
        router, summarizer = self.pipeline_router("single")
        result = router.handle(self.group_msg("m1", f"@{BOT} 抽签"))
        assert "抽签结果" in result
        assert summarizer.events == []

    def test_a_stale_at_mention_is_still_ignored(self):
        router, summarizer = self.pipeline_router("single")
        stale = self.group_msg("m1", f"@{BOT} 你好", timestamp=self.now() - AT_MENTION_MAX_AGE_SEC - 5)
        assert router.handle(stale) is None
        assert summarizer.events == []


class NameCallTests(PipelineTestCase):
    def test_saying_the_name_without_an_at_asks_the_model(self):
        router, summarizer = self.pipeline_router("single", reply("我在"))
        result = router.handle(self.group_msg("m1", f"{BOT}你觉得呢", at=False))

        assert result == "我在"
        (request,) = summarizer.decide_requests
        assert request.addressing.level == LEVEL_STRONG
        assert request.addressing.signals == ("name_call",)
        assert self.trace.notes["route"] == "name_call"

    def test_the_persona_name_and_aliases_count_too(self):
        router, summarizer = self.pipeline_router(
            "single", chat_persona_name="小柔柔", bot_aliases=["阿柔"],
        )
        router.handle(self.group_msg("m1", "小柔柔在不在", at=False))
        router.handle(self.group_msg("m2", "阿柔 看一下", at=False, sender_id="wxid_user2"))
        assert len(summarizer.decide_requests) == 2

    def test_the_model_may_stay_silent_on_a_name_without_a_fallback(self):
        router, summarizer = self.pipeline_router("single", silent("others"))
        assert router.handle(self.group_msg("m1", f"我觉得{BOT}这个角色不错", at=False)) is None
        assert self.trace.reasons[-1] == "ai_intentional_silence"

    def test_a_name_inside_an_at_of_someone_else_is_still_a_call(self):
        router, summarizer = self.pipeline_router("single")
        router.handle(self.group_msg("m1", f"@阿明 {BOT}说得对", at=False))
        (request,) = summarizer.decide_requests
        assert "at_other" in request.addressing.signals and "name_call" in request.addressing.signals

    def test_ordinary_chatter_does_not_wake_the_engine(self):
        router, summarizer = self.pipeline_router("single")
        self.gate(router, allow=False)
        assert router.handle(self.group_msg("m1", "今天吃火锅", at=False)) is None
        assert summarizer.events == []


class DecisionMappingTests(PipelineTestCase):
    def test_silence_on_an_explicit_call_gets_the_direct_fallback(self):
        router, _ = self.pipeline_router("single", silent("me"))
        assert router.handle(self.group_msg("m1", f"@{BOT} 帮我看看这个")) == "我刚才没接住，再说一次好不好"
        assert self.trace.reasons[-1] == "ai_blank_direct_fallback"

    def test_a_presence_check_gets_the_short_fallback(self):
        router, _ = self.pipeline_router("single", silent("group"))
        assert router.handle(self.group_msg("m1", f"@{BOT} 在吗")) == "嗯，在的"

    def test_the_model_can_decline_an_at_that_was_not_for_it(self):
        router, _ = self.pipeline_router("single", silent("others"))
        assert router.handle(self.group_msg("m1", f"@{BOT} 你们看她回的啥")) is None
        assert self.trace.reasons[-1] == "ai_intentional_silence"

    def test_an_unusable_answer_to_a_direct_call_still_answers_something(self):
        for decision in (failed(STATUS_PARSE_FAILED, "no_json"), failed(STATUS_EMPTY, "empty")):
            router, _ = self.pipeline_router("single", decision)
            assert router.handle(self.group_msg(f"m-{decision.status}", f"@{BOT} 你好")) == "我刚才没接住，再说一次好不好"

    def test_an_unusable_answer_to_a_weak_signal_is_silence(self):
        router, summarizer = self.pipeline_router("single", failed())
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))             # arms follow-up
        summarizer.decision = failed()
        assert router.handle(self.group_msg("m2", "哪首？", at=False)) is None
        assert self.trace.reasons[-1] == "ai_unusable_answer"

    def test_a_forced_reply_group_always_answers(self):
        router, _ = self.pipeline_router(
            "single", silent(),
            chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
            yang_yujing_group_name="",
            yang_yujing_always_reply_group_name=YANG_GROUP,
            chat_persona_name=YANG_BOT, bot_display_name=YANG_BOT,
        )
        message = self.group_msg("m1", "随便一句", at=False, group_name=YANG_GROUP)
        assert router.handle(message) == "啊？"
        assert self.trace.reasons[-1] == "ai_blank_forced_fallback"

    def test_a_transport_error_uses_the_legacy_error_fallbacks(self):
        router, summarizer = self.pipeline_router("single")
        summarizer.error = RuntimeError("boom")
        assert router.handle(self.group_msg("m1", f"@{BOT} 你好")) == "我刚才有点卡，再说一次好不好"
        # a short follow-up from the person who was just answered is not a direct call
        summarizer.error = RuntimeError("boom")
        assert router.handle(self.group_msg("m2", "你觉得呢", at=False)) == "大脑短路了，稍等再试～"

    def test_the_decision_and_addressing_are_written_to_the_trace(self):
        router, _ = self.pipeline_router("single", reply("好", reason="被问"))
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert self.trace.notes["addressing"] == {"level": "explicit", "signals": ["at"]}
        assert self.trace.notes["decision"]["action"] == "reply"
        assert self.trace.notes["decision"]["reason"] == "被问"
        assert self.trace.reasons[-1] == "ai_reply"

    def test_the_reply_goes_through_the_usual_clean_up(self):
        router, _ = self.pipeline_router("single", reply("**好**呀😂"))
        assert router.handle(self.group_msg("m1", f"@{BOT} 你好")) == "好呀"


class ProactivePipelineTests(PipelineTestCase):
    def test_an_open_gate_asks_the_engine_with_ambient_addressing(self):
        router, summarizer = self.pipeline_router("single", reply("哈哈确实"), proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        self.other_msg("c0", "昨天那个比赛看了吗")                      # not stored yet
        result = router.handle(self.other_msg("m1", "决赛太精彩了"))

        assert result == "哈哈确实"
        (request,) = summarizer.decide_requests
        assert request.call_name == "proactive_v2" and request.ambient
        assert request.mode is MODE
        assert request.addressing.level == LEVEL_AMBIENT
        assert request.message == "决赛太精彩了"
        assert request.requester_name == "土豆"
        assert summarizer.events == ["decide_and_reply"]

    def test_silence_feeds_the_gate_backoff(self):
        router, _ = self.pipeline_router("single", silent(), proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        assert router.handle(self.other_msg("m1", "决赛太精彩了")) is None
        assert router._proactive.get_consecutive_silence(CHAT) == 1
        assert self.trace.reasons[-1] == "proactive_model_silence"

    def test_an_unusable_answer_counts_as_silence_but_is_labelled(self):
        router, _ = self.pipeline_router("single", failed(), proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        assert router.handle(self.other_msg("m1", "决赛太精彩了")) is None
        assert router._proactive.get_consecutive_silence(CHAT) == 1
        assert self.trace.reasons[-1] == "proactive_unusable_answer"

    def test_speech_resets_the_backoff_and_is_remembered(self):
        router, summarizer = self.pipeline_router("single", reply("哈哈确实"), proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        router._proactive.record_silence(CHAT)
        router.handle(self.other_msg("m1", "决赛太精彩了"))
        assert router._proactive.get_consecutive_silence(CHAT) == 0
        router.handle(self.other_msg("m2", "还有加时赛"))
        assert summarizer.decide_requests[1].recent_bot_replies == ("哈哈确实",)

    def test_an_engine_error_is_swallowed(self):
        router, summarizer = self.pipeline_router("single", proactive_enabled=True)
        summarizer.error = RuntimeError("boom")
        self.gate(router, allow=True, mode=MODE)
        assert router.handle(self.other_msg("m1", "决赛太精彩了")) is None
        assert self.trace.reasons[-1] == "proactive_api_error"

    def test_off_keeps_the_legacy_proactive_call(self):
        router, summarizer = self.pipeline_router("off", proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        assert router.handle(self.other_msg("m1", "决赛太精彩了")) == "接一句"
        assert summarizer.events == ["proactive_chat"]


class FullFeedPipelineTests(PipelineTestCase):
    def yang_router(self, decision):
        return self.pipeline_router(
            "single", decision,
            chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
            yang_yujing_group_name=YANG_GROUP,
            chat_persona_name=YANG_BOT, bot_display_name=YANG_BOT,
        )

    def test_the_full_feed_group_is_ambient_with_the_full_feed_signal(self):
        router, summarizer = self.yang_router(reply("牛"))
        message = self.group_msg("m1", "刚才那把太离谱了吧", at=False, group_name=YANG_GROUP)
        assert router.handle(message) == "牛"
        (request,) = summarizer.decide_requests
        assert request.ambient and request.addressing.level == LEVEL_AMBIENT
        assert request.addressing.signals == ("full_feed",)

    def test_an_at_in_the_full_feed_group_is_explicit(self):
        router, summarizer = self.yang_router(reply("在"))
        message = self.group_msg("m1", f"@{YANG_BOT} 在吗", group_name=YANG_GROUP)
        router.handle(message)
        assert summarizer.decide_requests[0].addressing.level == LEVEL_EXPLICIT

    def test_silence_in_the_full_feed_group_is_not_answered(self):
        router, _ = self.yang_router(silent())
        message = self.group_msg("m1", "刚才那把太离谱了吧", at=False, group_name=YANG_GROUP)
        assert router.handle(message) is None


class QuietUnlessAddressedTests(PipelineTestCase):
    """A name, a keyword or a follow-up only asks the model; its silence is respected."""

    def test_a_keyword_call_the_model_declines_stays_silent(self):
        for addressee in ("group", "me", "others"):
            router, _ = self.pipeline_router("single", silent(addressee), keywords=["在干嘛"])
            assert router.handle(self.group_msg(f"k-{addressee}", "阿明你在干嘛呢", at=False)) is None
            assert self.trace.reasons[-1] == "ai_intentional_silence"

    def test_an_unusable_answer_to_a_keyword_or_name_is_silence(self):
        router, _ = self.pipeline_router("single", failed(), keywords=["在干嘛"])
        assert router.handle(self.group_msg("k1", "阿明你在干嘛呢", at=False)) is None
        assert router.handle(self.group_msg("n1", f"{BOT}在不在", at=False, sender_id="wxid_user2")) is None

    def test_a_reply_the_model_says_was_for_someone_else_is_not_sent_on_a_weak_signal(self):
        router, _ = self.pipeline_router("single", reply("我也觉得", addressee="others"))
        assert router.handle(self.group_msg("n1", f"我觉得{BOT}这个角色不错", at=False)) is None
        assert self.trace.reasons[-1] == "ai_addressee_others"

    def test_an_explicit_call_is_answered_whatever_the_addressee_field_says(self):
        router, _ = self.pipeline_router("single", reply("看到啦", addressee="others"))
        assert router.handle(self.group_msg("m1", f"@{BOT} 帮我看看")) == "看到啦"

    def test_silence_with_addressee_others_cannot_decline_a_private_chat(self):
        router, _ = self.pipeline_router("single", silent("others"))
        assert router.handle(self.private_msg("p1", "你好呀")) == "我刚才没接住，再说一次好不好"

    def test_a_name_in_passing_is_not_a_command(self):
        router, summarizer = self.pipeline_router("single", reply("哈哈是吧"))
        result = router.handle(self.group_msg("n1", f"{BOT}上次塔罗算得好准", at=False))
        assert result == "哈哈是吧"
        assert "塔罗三张" not in result and summarizer.events == ["decide_and_reply"]
        assert self.trace.notes["route"] == "name_call"

    def test_a_name_in_passing_never_triggers_the_other_command_handlers(self):
        router, summarizer = self.pipeline_router("single", silent("others"))
        for index, text in enumerate((f"{BOT}帮助", f"{BOT} 抽签", f"{BOT}记一下 买菜")):
            assert router.handle(self.group_msg(f"c{index}", text, at=False, sender_id=f"wxid_x{index}")) is None
        assert len(summarizer.decide_requests) == 3

    def test_an_explicit_at_still_runs_its_commands(self):
        router, summarizer = self.pipeline_router("single")
        assert "抽签结果" in router.handle(self.group_msg("m1", f"@{BOT} 抽签"))
        assert summarizer.events == []

    def test_a_name_in_passing_with_the_api_down_says_nothing_in_the_group(self):
        router, summarizer = self.pipeline_router("single")
        summarizer.error = RuntimeError("boom")
        assert router.handle(self.group_msg("n1", f"我觉得{BOT}这个角色不错", at=False)) is None
        assert self.trace.reasons[-1] == "ai_error"


class RouterToEngineEndToEndTests(PipelineTestCase):
    """The router, the real engine and the real prompts, with only the model scripted."""

    class Model(ScriptedSummarizer):
        def consolidate_memory(self, existing_memory, new_messages):
            return existing_memory

    def build(self, *answers, **config):
        answer = '{"addressee":"me","action":"reply","messages":["林小满：好呀"],"reason":"x"}'
        model = self.Model(*(answers or (answer,) * 6))
        router, _ = self.make_router(
            summarizer=model, pipeline_v2="single", web=StubWebSearch(), **config,
        )
        router._tracer = self.trace = TraceSpy()
        return router, model

    def prompt(self, model, index=-1):
        system, messages = model.calls[index]
        return system, messages[0]["content"]

    def test_search_results_history_memory_and_the_budget_reach_the_prompt(self):
        router, model = self.build()
        router._store.upsert_group_memory(CHAT, "阿明是群里的吉他手", 1, "m0")
        router._store.insert_message({
            "message_id": "old1", "chat_id": CHAT, "sender_id": "wxid_user2", "sender_name": "土豆",
            "content": "上周五说好去吃烧烤", "msg_type": 1, "timestamp": self.now() - 3 * 86400,
        })
        reply = router.handle(self.group_msg("m1", f"@{BOT} 之前说的烧烤 帮我查一下"))

        system, user = self.prompt(model)
        assert reply == "好呀"                                      # the speaker label was stripped
        assert "RESULTS[查询词]" in user                            # web search
        assert "上周五说好去吃烧烤" in user                         # history recall
        assert "阿明是群里的吉他手" in system                       # long-term memory
        assert "不超过 99 个字" in system                           # web-search reply budget
        assert "之前说的烧烤" in user                               # the message itself

    def test_the_quote_context_and_own_recent_replies_reach_the_prompt(self):
        router, model = self.build()
        router.handle(self.group_msg("m1", f"@{BOT} 推荐几家烧烤店"))
        router.handle(self.group_msg(
            "m2", "第二家呢", at=True, is_reply_to_bot=True,
            quoted_content="推荐三家烧烤店", quoted_sender_name=BOT,
        ))
        system, user = self.prompt(model)
        assert "你随后回复/追问：「推荐三家烧烤店」" in user
        assert "1. 好呀" in user                                    # what the bot said before
        assert "这条消息引用了你的上一句话" in user

    def test_the_decision_and_prompt_are_logged_with_the_message(self):
        router, model = self.build()
        calls = []
        model.set_call_recorder(lambda **fields: calls.append(fields))
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        (record,) = calls
        assert record["call"] == "chat_v2" and record["stage"] == "single"
        assert "你好" in record["user_prompt"] and "json" in record["system_prompt"]

    def test_a_proactive_reply_carries_the_group_memory_and_mode(self):
        router, model = self.build(proactive_enabled=True)
        router._store.upsert_group_memory(CHAT, "阿明是群里的吉他手", 1, "m0")
        short_mode = ProactiveMode("BURST", "炸了", "刷屏", "极短反应", 8.5, 30, 0.7, 20, 80)
        self.gate(router, allow=True, mode=short_mode)
        router.handle(self.other_msg("m1", "决赛太精彩了"))
        system, user = self.prompt(model)
        assert "阿明是群里的吉他手" in system
        assert "氛围模式：炸了（刷屏）" in user and "决赛太精彩了" in user
        assert "不超过 20 个字" in system                           # the mode's budget beats the persona's 30


class GroupFilterTests(PipelineTestCase):
    """PIPELINE_V2_GROUPS: one group can try the pipeline while the rest do not."""

    def other_group(self, message_id, content, **kwargs):
        return self.group_msg(
            message_id, content, chat_id="other@chatroom", group_name="别的群", **kwargs,
        )

    def test_all_groups_by_default(self):
        router, summarizer = self.pipeline_router("single")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        router.handle(self.other_group("m2", f"@{BOT} 你好"))
        assert summarizer.events == ["decide_and_reply", "decide_and_reply"]

    def test_only_a_listed_group_uses_the_pipeline(self):
        router, summarizer = self.pipeline_router("single", pipeline_v2_groups=["测试群"])
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        router.handle(self.other_group("m2", f"@{BOT} 你好"))
        assert summarizer.events == ["decide_and_reply", "chat"]

    def test_a_group_can_be_listed_by_chat_id_or_part_of_its_name(self):
        for entry in (CHAT, "TESTCHAT", "测试"):
            router, summarizer = self.pipeline_router(
                "single", pipeline_v2_groups=[entry.upper() if entry == "TESTCHAT" else entry],
            )
            router.handle(self.group_msg("m1", f"@{BOT} 你好", chat_id="testchat@chatroom"
                                         if entry == "TESTCHAT" else CHAT))
            assert summarizer.events == ["decide_and_reply"], entry

    def test_private_chats_need_the_private_entry(self):
        router, summarizer = self.pipeline_router("single", pipeline_v2_groups=["测试群"])
        router.handle(self.private_msg("p1", "在吗"))
        assert summarizer.events == ["chat"]
        router, summarizer = self.pipeline_router("single", pipeline_v2_groups=["测试群", "private"])
        router.handle(self.private_msg("p1", "在吗"))
        assert summarizer.events == ["decide_and_reply"]

    def test_a_name_call_is_only_a_call_in_a_listed_group(self):
        router, summarizer = self.pipeline_router("single", pipeline_v2_groups=["测试群"])
        self.gate(router, allow=False)
        router.handle(self.group_msg("m1", f"{BOT}你觉得呢", at=False))
        router.handle(self.other_group("m2", f"{BOT}你觉得呢", at=False))
        assert summarizer.events == ["decide_and_reply"]

    def test_the_gate_is_told_which_chats_use_the_score(self):
        router, summarizer = self.pipeline_router(
            "single", pipeline_v2_groups=["测试群"], proactive_enabled=True,
        )
        gate = self.gate(router, allow=False)
        router.handle(self.other_msg("m1", "闲聊一句"))
        router.handle(self.other_msg("m2", "闲聊二句", chat_id="other@chatroom", group_name="别的群"))
        assert [call.kwargs for call in gate.call_args_list] == [{"scored": True}, {"scored": False}]

    def test_an_unlisted_group_keeps_the_legacy_proactive_call(self):
        router, summarizer = self.pipeline_router(
            "single", pipeline_v2_groups=["测试群"], proactive_enabled=True,
        )
        self.gate(router, allow=True, mode=MODE)
        result = router.handle(self.other_msg("m1", "决赛太精彩了", chat_id="other@chatroom", group_name="别的群"))
        assert result == "接一句" and summarizer.events == ["proactive_chat"]

    def test_with_the_pipeline_off_the_gate_is_called_exactly_as_before(self):
        router, summarizer = self.pipeline_router("off", proactive_enabled=True)
        gate = self.gate(router, allow=False)
        router.handle(self.other_msg("m1", "闲聊一句"))
        assert [(call.args, call.kwargs) for call in gate.call_args_list][0][1] == {}
