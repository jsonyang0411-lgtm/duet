"""The decision engine and the summarizer hook that exposes it."""

import json

import pytest

from src.pipeline.addressing import (
    LEVEL_AMBIENT,
    LEVEL_EXPLICIT,
    LEVEL_STRONG,
    Addressing,
    SIGNAL_AT,
    SIGNAL_NAME_CALL,
)
from src.pipeline.engine import decide, uses_two_steps
from src.pipeline.types import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_REPAIRED,
    STRATEGY_SINGLE,
    STRATEGY_TWO_STEP,
    ChatRequest,
    ModelOutput,
)
from src.proactive.modes import ProactiveMode
from src.summarize.base import AbstractSummarizer


class Transient(Exception):
    pass


class ScriptedSummarizer(AbstractSummarizer):
    """A real AbstractSummarizer whose model answers from a script."""

    retry_exceptions = (Transient,)

    def __init__(self, *answers):
        self.script = list(answers)
        self.calls = []            # (system_prompt, messages)
        self.records = []
        self.configure_chat_persona(
            persona_name="小柔", persona_prompt="PERSONA-TEXT", reply_rules="RULES-TEXT",
            max_chars=30, context_count=10,
        )
        self.set_call_recorder(lambda **fields: self.records.append(fields))

    def _call_json_api(self, system_prompt, messages):
        self.calls.append((system_prompt, [dict(m) for m in messages]))
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, ModelOutput) else ModelOutput(text=answer, native_json=True)

    # unused abstract methods
    def _call_chat_api(self, system_prompt, messages): raise AssertionError
    def _summarize_direct(self, messages, requester_name): raise AssertionError
    def _summarize_chunk(self, chunk, chunk_num, total, requester_name): raise AssertionError
    def _merge_chunk_summaries(self, chunk_summaries, requester_name): raise AssertionError
    def consolidate_memory(self, existing_memory, new_messages): raise AssertionError


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("src.summarize.base.time.sleep", lambda seconds: None)


def envelope_json(action="reply", messages=("好呀",), addressee="me", reason="测试"):
    return json.dumps({"addressee": addressee, "action": action,
                       "messages": list(messages), "reason": reason}, ensure_ascii=False)


def request(level=LEVEL_EXPLICIT, signals=(SIGNAL_AT,), **kwargs):
    defaults = dict(
        message="晚上吃什么", context_messages=({"sender_name": "阿明", "content": "饿了"},),
        requester_name="阿明", bot_name="小助手", group_name="测试群",
        addressing=Addressing(level, signals),
    )
    defaults.update(kwargs)
    return ChatRequest(**defaults)


AMBIENT = dict(level=LEVEL_AMBIENT, signals=(), ambient=True)
STRONG = dict(level=LEVEL_STRONG, signals=(SIGNAL_NAME_CALL,))


# ── single strategy ─────────────────────────────────────────────────────

def test_a_valid_answer_is_one_call_and_status_ok():
    model = ScriptedSummarizer(envelope_json())
    decision = model.decide_and_reply(request())

    assert decision.is_reply and decision.text == "好呀"
    assert (decision.status, decision.calls, decision.strategy) == (STATUS_OK, 1, STRATEGY_SINGLE)
    assert decision.addressee == "me" and decision.reason == "测试"
    assert len(model.calls) == 1


def test_the_prompts_carry_persona_message_context_facts_and_the_json_contract():
    model = ScriptedSummarizer(envelope_json())
    model.decide_and_reply(request(recent_bot_replies=("刚才说过的话",)))
    system, messages = model.calls[0]
    user = messages[0]["content"]

    assert "PERSONA-TEXT" in system and "RULES-TEXT" in system
    assert "json" in system.lower() and '"action"' in system
    assert "晚上吃什么" in user and "阿明: 饿了" in user
    assert "这条消息 @ 了你" in user
    assert "刚才说过的话" in user
    assert messages[0]["role"] == "user"


def test_braces_in_chat_text_reach_the_model_untouched():
    model = ScriptedSummarizer(envelope_json())
    model.decide_and_reply(request(message="代码里有 {x} 和 {{y}}"))
    assert "代码里有 {x} 和 {{y}}" in model.calls[0][1][0]["content"]


def test_a_silent_envelope_is_a_silent_decision():
    model = ScriptedSummarizer(envelope_json("silent", (), "others", "他们在互相聊"))
    decision = model.decide_and_reply(request(**STRONG))
    assert not decision.is_reply
    assert decision.action == "silent" and decision.addressee == "others"
    assert decision.status == STATUS_OK and decision.messages == ()


def test_every_call_is_recorded_with_its_stage_and_usage():
    usage = {"input_tokens": 100, "output_tokens": 20}
    model = ScriptedSummarizer(ModelOutput(text=envelope_json(), usage=usage, native_json=True))
    model.decide_and_reply(request())

    (record,) = model.records
    assert record["call"] == "chat_v2"
    assert (record["stage"], record["attempt"]) == ("single", "ask")
    assert record["usage"] == usage and record["native_json"] is True
    assert record["raw_reply"] == envelope_json()
    assert record["latency_ms"] >= 0


# ── failure policy ──────────────────────────────────────────────────────

def test_an_empty_answer_is_retried_once_not_taken_as_silence():
    model = ScriptedSummarizer("", envelope_json())
    decision = model.decide_and_reply(request())
    assert decision.is_reply and decision.status == STATUS_OK
    assert decision.calls == 2
    assert [r["attempt"] for r in model.records] == ["ask", "retry_empty"]


def test_two_empty_answers_give_status_empty_and_no_repair_call():
    model = ScriptedSummarizer("", "  ")
    decision = model.decide_and_reply(request())
    assert (decision.status, decision.error_code, decision.action) == (STATUS_EMPTY, "empty", "silent")
    assert decision.calls == 2 and decision.failed
    assert model.script == []


def test_an_unusable_answer_gets_one_repair_call_that_shows_the_fault():
    model = ScriptedSummarizer("好的，我觉得可以", envelope_json())
    decision = model.decide_and_reply(request())

    assert decision.is_reply and decision.status == STATUS_REPAIRED and decision.calls == 2
    system, messages = model.calls[1]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "好的，我觉得可以"
    assert "no_json" in messages[2]["content"]
    assert system == model.calls[0][0]                      # same system prompt


def test_two_unusable_answers_end_in_silence_with_a_countable_code():
    model = ScriptedSummarizer("乱说一气", '{"action":"maybe"}')
    decision = model.decide_and_reply(request())
    assert (decision.status, decision.error_code) == (STATUS_PARSE_FAILED, "bad_action")
    assert decision.action == "silent" and decision.messages == () and decision.calls == 2


def test_repair_can_be_switched_off():
    model = ScriptedSummarizer("乱说一气")
    model.configure_pipeline(repair=False)
    decision = model.decide_and_reply(request())
    assert (decision.status, decision.error_code, decision.calls) == (STATUS_PARSE_FAILED, "no_json", 1)


def test_a_reply_that_is_only_a_silence_marker_is_silence_not_a_failure():
    model = ScriptedSummarizer(envelope_json("reply", ("（沉默）",)))
    decision = model.decide_and_reply(request(**STRONG))
    assert decision.action == "silent" and decision.status == STATUS_OK
    assert "reply_text_rejected" in decision.warnings


def test_a_transport_error_is_retried_then_raised():
    model = ScriptedSummarizer(Transient("boom"), Transient("boom"))
    with pytest.raises(RuntimeError, match="Failed after 2 attempt"):
        model.decide_and_reply(request())
    assert len(model.calls) == 2
    (record,) = model.records                      # one line for the failed attempt series
    assert "RuntimeError" in record["error"] and record["raw_reply"] == ""


def test_a_transient_error_followed_by_success_still_works():
    model = ScriptedSummarizer(Transient("blip"), envelope_json())
    decision = model.decide_and_reply(request())
    assert decision.is_reply and decision.status == STATUS_OK


def test_a_non_retryable_error_is_raised_at_once_and_logged():
    model = ScriptedSummarizer(ValueError("bad request"))
    with pytest.raises(ValueError):
        model.decide_and_reply(request())
    (record,) = model.records
    assert "ValueError" in record["error"]


# ── length budget ───────────────────────────────────────────────────────

def test_the_reply_is_cut_to_the_persona_budget():
    long_text = "我觉得周末去爬山挺好的，不过要看天气，如果下雨的话我们可以改成去看电影"
    model = ScriptedSummarizer(envelope_json("reply", (long_text,)))
    decision = model.decide_and_reply(request())
    assert decision.messages == ("我觉得周末去爬山挺好的，不过要看天气",) or len(decision.text) <= 30
    assert "truncated" in decision.warnings


def test_the_budget_follows_override_and_proactive_mode():
    mode = ProactiveMode("LIVELY", "热闹", "多人讨论", "接短句", 6.5, 60, 0.5, 12, 20)
    model = ScriptedSummarizer(envelope_json("reply", ("哈" * 40,)), envelope_json("reply", ("哈" * 40,)))
    assert len(model.decide_and_reply(request(**AMBIENT, mode=mode)).text) <= 12
    assert len(model.decide_and_reply(request(max_chars_override=20)).text) <= 20
    assert "不超过 12 个字" in model.calls[0][0]
    assert "不超过 20 个字" in model.calls[1][0]


# ── two-step strategy ───────────────────────────────────────────────────

def test_two_step_asks_whether_to_speak_before_writing():
    model = ScriptedSummarizer(
        envelope_json("reply", (), "me", "像是在叫我"),
        envelope_json("reply", ("在呢",), "me", "回应"),
    )
    decision = model.decide_and_reply(request(**STRONG), strategy=STRATEGY_TWO_STEP)

    assert decision.text == "在呢" and decision.strategy == STRATEGY_TWO_STEP
    assert decision.calls == 2 and decision.addressee == "me"
    decide_system = model.calls[0][0]
    generate_system, generate_messages = model.calls[1]
    assert "PERSONA-TEXT" not in decide_system and "判断器" in decide_system
    assert "PERSONA-TEXT" in generate_system
    assert "你已经决定回复" in generate_messages[0]["content"]
    assert [r["stage"] for r in model.records] == ["decide", "generate"]


def test_two_step_stops_after_a_silent_decision():
    model = ScriptedSummarizer(envelope_json("silent", (), "others", "他们在聊"))
    decision = model.decide_and_reply(request(**AMBIENT), strategy=STRATEGY_TWO_STEP)
    assert not decision.is_reply and decision.calls == 1
    assert decision.strategy == STRATEGY_TWO_STEP and decision.addressee == "others"


def test_two_step_reports_a_failed_decision_without_writing():
    model = ScriptedSummarizer("???", "!!!")
    decision = model.decide_and_reply(request(**AMBIENT), strategy=STRATEGY_TWO_STEP)
    assert decision.failed and decision.strategy == STRATEGY_TWO_STEP
    assert len(model.calls) == 2                      # ask + repair, no generate


def test_a_repaired_decision_is_remembered_in_the_final_status():
    model = ScriptedSummarizer("???", envelope_json("reply", ()), envelope_json("reply", ("好",)))
    decision = model.decide_and_reply(request(**STRONG), strategy=STRATEGY_TWO_STEP)
    assert decision.status == STATUS_REPAIRED and decision.calls == 3


def test_explicit_calls_skip_the_decision_step():
    model = ScriptedSummarizer(envelope_json())
    decision = model.decide_and_reply(request(), strategy=STRATEGY_TWO_STEP)
    assert decision.calls == 1 and len(model.calls) == 1
    assert decision.strategy == STRATEGY_TWO_STEP


def test_uses_two_steps_only_for_the_gray_zone():
    assert uses_two_steps(request(**STRONG), STRATEGY_TWO_STEP)
    assert uses_two_steps(request(**AMBIENT), STRATEGY_TWO_STEP)
    assert not uses_two_steps(request(), STRATEGY_TWO_STEP)                       # explicit
    assert not uses_two_steps(request(**STRONG, force_reply=True), STRATEGY_TWO_STEP)
    assert not uses_two_steps(request(**STRONG, is_group=False), STRATEGY_TWO_STEP)
    assert not uses_two_steps(request(**STRONG), STRATEGY_SINGLE)


def test_the_module_function_and_the_summarizer_method_agree():
    model = ScriptedSummarizer(envelope_json(), envelope_json())
    assert decide(model, request()).text == model.decide_and_reply(request()).text
