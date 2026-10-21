"""FOLLOWUP_MODE=declared: the model says whom it waits for after a reply."""

import json
import os
from unittest.mock import patch

import pytest

from test_pipeline_engine import ScriptedSummarizer
from test_router_pipeline import PipelineSummarizer, TraceSpy
from test_router_priority import BOT, CHAT, NOW, RouterTestCase, StubWebSearch

from src.config import BotConfig, load_config
from src.pipeline import envelope as E
from src.pipeline.engine import decide
from src.pipeline.prompts import STAGE_DECIDE, STAGE_GENERATE, STAGE_SINGLE, build_prompts, output_format
from src.pipeline.types import ChatRequest, Decision
from src.proactive.followup import FollowupListenTracker


def env(**fields):
    return json.dumps({"addressee": "me", "action": "reply", "messages": ["你想喝什么"], "reason": "问对方", **fields},
                      ensure_ascii=False)


# ── reading the declaration ─────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ({"expect": "sender", "seconds": 45}, ("sender", 45)),
    ({"expect": "anyone", "seconds": 90}, ("anyone", 90)),
    ({"expect": "none", "seconds": 5}, ("none", 10)),                   # clamped to 10 .. 180
    ({"expect": "sender", "seconds": 9999}, ("sender", 180)),
    ({"expect": "sender"}, ("sender", 60)),                              # default window
    ("sender", ("sender", 60)), ("对方", ("sender", 60)), ("none", ("none", 60)), ("Anyone", ("anyone", 60)),
    (True, ("sender", 60)), (False, ("none", 60)),
    ({"expect": "asker", "seconds": "30"}, ("sender", 30)),
    ({"expect": "sender", "seconds": 30.7}, ("sender", 30)),
])
def test_the_declaration_is_read_leniently(value, expected):
    follow = E.parse_follow_up(value)
    assert (follow.expect, follow.seconds) == expected


@pytest.mark.parametrize("value", [None, "", "maybe", 5, [], {}, {"expect": "who"}, {"expect": None},
                                   {"expect": "sender", "seconds": "soon"}, {"expect": "sender", "seconds": float("inf")},
                                   {"expect": "sender", "seconds": 10 ** 400}])
def test_anything_odd_is_ignored_not_an_error(value):
    assert E.parse_follow_up(value) is None


def test_the_envelope_reads_it_only_when_asked_and_only_for_replies():
    raw = env(follow_up={"expect": "sender", "seconds": 30})
    assert E.parse_envelope(raw, max_chars=30).follow_up is None
    assert E.parse_envelope(raw, max_chars=30, follow_up=True).follow_up == E.FollowUp("sender", 30)
    silent = json.dumps({"addressee": "others", "action": "silent", "messages": [], "reason": "x",
                         "follow_up": {"expect": "sender"}})
    assert E.parse_envelope(silent, max_chars=30, follow_up=True).follow_up is None
    assert E.parse_envelope(env(follow_up="nonsense"), max_chars=30, follow_up=True).follow_up is None
    assert E.parse_envelope(env(), max_chars=30, follow_up=True).follow_up is None


def test_the_extended_schema_only_adds_an_optional_field():
    base, extended = E.ENVELOPE_JSON_SCHEMA, E.ENVELOPE_JSON_SCHEMA_FOLLOWUP
    assert extended["required"] == base["required"] and extended["additionalProperties"] is False
    assert set(extended["properties"]) - set(base["properties"]) == {"follow_up"}
    assert "follow_up" not in base["properties"]                      # the normal schema is untouched


# ── the prompt and the engine ───────────────────────────────────────────────

def test_the_prompt_mentions_the_field_only_in_declared_mode_and_only_for_the_reply_stage():
    assert "follow_up" not in output_format(30, STAGE_SINGLE)
    assert "follow_up" in output_format(30, STAGE_SINGLE, follow_up=True)
    assert "follow_up" in output_format(30, STAGE_GENERATE, follow_up=True)
    assert "follow_up" not in output_format(30, STAGE_DECIDE, follow_up=True)
    settings = {"persona_name": "小柔", "persona_prompt": "P", "reply_rules": "R", "max_chars": 30, "context_count": 10}
    on = build_prompts(settings, ChatRequest(message="x", declare_followup=True))[0]
    off = build_prompts(settings, ChatRequest(message="x"))[0]
    assert "follow_up" in on and "follow_up" not in off


class Recording(ScriptedSummarizer):
    def _call_json_api(self, system_prompt, messages, **kwargs):
        self.kwargs = getattr(self, "kwargs", []) + [kwargs]
        return super()._call_json_api(system_prompt, messages)


def test_the_engine_hands_the_extended_schema_to_the_backend_and_returns_the_declaration():
    model = Recording(env(follow_up={"expect": "sender", "seconds": 30}))
    decision = decide(model, ChatRequest(message="你好", bot_name="b", requester_name="张三", declare_followup=True))
    assert decision.follow_up == E.FollowUp("sender", 30)
    assert model.kwargs == [{"schema": E.ENVELOPE_JSON_SCHEMA_FOLLOWUP}]
    assert decision.as_trace()["follow_up"] == {"expect": "sender", "seconds": 30}


def test_without_the_mode_the_backend_is_called_exactly_as_before():
    model = Recording(env(follow_up={"expect": "sender", "seconds": 30}))
    decision = decide(model, ChatRequest(message="你好", bot_name="b", requester_name="张三"))
    assert decision.follow_up is None and model.kwargs == [{}]
    assert "follow_up" not in decision.as_trace()


def test_a_back_end_that_does_not_know_the_schema_argument_still_works_in_the_old_mode():
    model = ScriptedSummarizer(env())          # its _call_json_api(system, messages) takes no schema
    assert decide(model, ChatRequest(message="你好", bot_name="b", requester_name="张三")).is_reply


def test_the_two_step_strategy_asks_only_the_reply_stage_for_it():
    model = Recording(
        json.dumps({"addressee": "me", "action": "reply", "messages": [], "reason": "值得回"}),
        env(follow_up={"expect": "anyone", "seconds": 20}),
    )
    request = ChatRequest(message="今晚吃什么", bot_name="b", requester_name="张三", declare_followup=True)
    decision = decide(model, request, strategy="two_step")
    assert decision.follow_up == E.FollowUp("anyone", 20)
    assert model.kwargs == [{}, {"schema": E.ENVELOPE_JSON_SCHEMA_FOLLOWUP}]


# ── the listener ────────────────────────────────────────────────────────────

def tracker():
    return FollowupListenTracker(ttl_sec=60, max_message_chars=30)


def msg(content, sender="wxid_a", chat=CHAT):
    return {"chat_id": chat, "sender_id": sender, "content": content}


def test_the_default_registration_is_the_old_one():
    t = tracker()
    t.register(CHAT, "你想喝什么")
    assert t.match(msg("都行")) is not None                   # an answer to the bot's question, as before
    assert t.match(msg("好的")) is None                       # a bare acknowledgement still is not


def test_waiting_for_the_asker_accepts_any_short_answer_from_that_person_only():
    t = tracker()
    t.register(CHAT, "你想喝什么", expect="sender", sender_id="wxid_a", seconds=30)
    assert t.match(msg("好的", "wxid_b")) is None
    assert t.match(msg("[图片]", "wxid_a")) is None
    assert t.match(msg("x" * 100, "wxid_a")) is None
    match = t.match(msg("好的", "wxid_a"))
    assert match.reason == "declared-sender" and match.bot_reply == "你想喝什么"


def test_waiting_for_nobody_stops_listening_even_if_something_was_registered_before():
    t = tracker()
    t.register(CHAT, "第一句", )
    t.register(CHAT, "不用等你回", expect="none")
    assert t.match(msg("为什么呀？")) is None


def test_waiting_for_anyone_keeps_the_heuristics_with_the_declared_window():
    t = tracker()
    t.register(CHAT, "你们想喝什么", expect="anyone", seconds=30)
    assert t.match(msg("都行", "wxid_b")).reason == "answers-bot-question"
    assert t.match(msg("好的", "wxid_b")) is None


def test_the_declared_window_replaces_the_default():
    t = tracker()
    t.register(CHAT, "你想喝什么", expect="sender", sender_id="wxid_a", seconds=120)
    with patch("src.proactive.followup.time.time", return_value=__import__("time").time() + 90):
        assert t.match(msg("好的", "wxid_a")) is not None          # still listening after the default 60 s
    with patch("src.proactive.followup.time.time", return_value=__import__("time").time() + 130):
        assert t.match(msg("好的", "wxid_a")) is None


# ── the whole path through the router ───────────────────────────────────────

class RouterCase(RouterTestCase):
    def build(self, decision, **config):
        model = PipelineSummarizer(decision)
        # sticky listening is a separate mechanism (the asker's next message counts as an @);
        # it is switched off here so only the follow-up listener is under test
        config.setdefault("sticky_mention_enabled", False)
        router, _ = self.make_router(summarizer=model, pipeline_v2="single", web=StubWebSearch(), **config)
        router._tracer = self.trace = TraceSpy()
        return router, model

    def decision(self, expect, seconds=45):
        return Decision(action="reply", messages=("你想喝什么",), addressee="me",
                        follow_up=E.FollowUp(expect, seconds))


class DeclaredModeTests(RouterCase):
    def test_the_request_asks_for_it_only_in_declared_mode(self):
        router, model = self.build(self.decision("sender"), followup_mode="declared")
        router.handle(self.group_msg("m1", f"@{BOT} 点个饮料"))
        assert model.decide_requests[0].declare_followup is True
        router, model = self.build(self.decision("sender"))
        router.handle(self.group_msg("m2", f"@{BOT} 点个饮料"))
        assert model.decide_requests[0].declare_followup is False

    def test_after_a_reply_that_waits_for_the_asker_only_that_person_is_heard(self):
        router, model = self.build(self.decision("sender"), followup_mode="declared")
        assert router.handle(self.group_msg("m1", f"@{BOT} 点个饮料")) == "你想喝什么"
        self.gate(router, allow=False)
        model.decision = Decision(action="reply", messages=("好的，给你点",), addressee="me")
        other = self.other_msg("m2", "好的", at=False)
        assert router.handle(other) is None and len(model.decide_requests) == 1
        answer = self.group_msg("m3", "好的", at=False)
        assert router.handle(answer) == "好的，给你点"
        assert answer["_followup_listen_reason"] == "declared-sender"

    def test_a_reply_that_waits_for_nobody_is_not_followed_up(self):
        router, model = self.build(self.decision("none"), followup_mode="declared")
        router.handle(self.group_msg("m1", f"@{BOT} 点个饮料"))
        self.gate(router, allow=False)
        assert router.handle(self.group_msg("m2", "你想喝什么吗？", at=False)) is None
        assert len(model.decide_requests) == 1

    def test_a_decision_without_a_declaration_keeps_the_heuristics(self):
        router, model = self.build(Decision(action="reply", messages=("你想喝什么",), addressee="me"),
                                   followup_mode="declared")
        router.handle(self.group_msg("m1", f"@{BOT} 点个饮料"))
        self.gate(router, allow=False)
        model.decision = Decision(action="reply", messages=("好",), addressee="me")
        router.handle(self.group_msg("m2", "都行", at=False))
        assert len(model.decide_requests) == 2

    def test_the_default_mode_ignores_a_declaration_it_did_not_ask_for(self):
        router, model = self.build(self.decision("none"))
        router.handle(self.group_msg("m1", f"@{BOT} 点个饮料"))
        self.gate(router, allow=False)
        model.decision = Decision(action="reply", messages=("好",), addressee="me")
        router.handle(self.group_msg("m2", "都行", at=False))
        assert len(model.decide_requests) == 2                                   # heard, as always

    def test_the_declaration_reaches_the_decision_trace(self):
        router, _ = self.build(self.decision("sender", 30), followup_mode="declared")
        router.handle(self.group_msg("m1", f"@{BOT} 点个饮料"))
        assert self.trace.notes["decision"]["follow_up"] == {"expect": "sender", "seconds": 30}


# ── configuration ───────────────────────────────────────────────────────────

def load(**fields):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", **fields}, clear=True):
        return load_config()


def test_the_mode_defaults_to_the_old_behaviour_and_is_validated():
    assert BotConfig().followup_mode == "heuristic" and load().followup_mode == "heuristic"
    assert load(FOLLOWUP_MODE=" Declared ").followup_mode == "declared"
    assert load(FOLLOWUP_MODE="").followup_mode == "heuristic"
    with pytest.raises(RuntimeError, match="FOLLOWUP_MODE"):
        load(FOLLOWUP_MODE="telepathy")
