"""Prompts of the structured pipeline: what the model is told, per situation."""

import datetime
import json
import re

import pytest

from src.pipeline.addressing import (
    LEVEL_AMBIENT,
    LEVEL_EXPLICIT,
    LEVEL_STRONG,
    LEVEL_WEAK,
    SIGNAL_AT,
    SIGNAL_AT_OTHER,
    SIGNAL_FOLLOWUP,
    SIGNAL_FORCED,
    SIGNAL_FULL_FEED,
    SIGNAL_KEYWORD,
    SIGNAL_NAME_CALL,
    SIGNAL_PRIVATE,
    SIGNAL_QUOTE_BOT,
    SIGNAL_QUOTE_OTHER,
    SIGNAL_STICKY,
    Addressing,
)
from src.pipeline.envelope import ENVELOPE_JSON_SCHEMA, parse_envelope
from src.pipeline.prompts import (
    STAGE_DECIDE,
    STAGE_GENERATE,
    STAGE_SINGLE,
    build_prompts,
    build_repair_messages,
    effective_max_chars,
    output_format,
    render_facts,
    render_policy,
)
from src.pipeline.types import ChatRequest
from src.proactive.modes import ProactiveMode

SETTINGS = {
    "persona_name": "小柔", "persona_prompt": "PERSONA", "reply_rules": "RULES",
    "max_chars": 30, "context_count": 3,
}
NOW = datetime.datetime(2026, 10, 16, 21, 30)


def make(level=LEVEL_EXPLICIT, signals=(SIGNAL_AT,), **kwargs):
    defaults = dict(
        message="今晚吃什么", context_messages=(), requester_name="阿明",
        bot_name="小助手", group_name="测试群", addressing=Addressing(level, signals),
    )
    defaults.update(kwargs)
    return ChatRequest(**defaults)


def prompts(request, stage=STAGE_SINGLE):
    return build_prompts(SETTINGS, request, stage=stage, now=NOW)


# ── structure ───────────────────────────────────────────────────────────

def test_system_prompt_is_stable_and_user_prompt_carries_what_changes():
    system_a, user_a = prompts(make(message="第一句", requester_name="阿明"))
    system_b, user_b = prompts(make(message="第二句", requester_name="小红"))
    assert system_a == system_b
    assert "第一句" in user_a and "第二句" in user_b
    assert "2026-10-16 21:30" in user_a


def test_single_prompt_has_persona_rules_memory_and_format():
    system, user = prompts(make(group_memory="阿明喜欢火锅"))
    for needle in ("PERSONA", "RULES", "你是小柔", "测试群", "json", '"addressee"',
                   "不超过 30 个字"):
        assert needle in system
    assert "阿明喜欢火锅" in system
    assert "只输出 JSON" in user


def test_missing_memory_uses_a_group_or_private_default():
    assert "还没有形成对这个群的印象" in prompts(make())[0]
    assert "这段私聊还没有形成长期记忆" in prompts(make(is_group=False))[0]


def test_recent_chat_and_own_lines_are_listed_oldest_first_within_the_limit():
    context = tuple({"sender_name": f"人{i}", "content": f"第{i}句"} for i in range(6))
    _, user = prompts(make(context_messages=context, recent_bot_replies=("甲", "乙")))
    assert "人3: 第3句" in user and "人5: 第5句" in user
    assert "人2: 第2句" not in user                         # context_count is 3
    assert user.index("人3") < user.index("人5")
    assert "1. 甲" in user and "2. 乙" in user


def test_empty_context_and_no_own_lines_have_placeholders():
    _, user = prompts(make())
    assert "（没有可用的最近聊天记录）" in user
    assert "（你最近还没在这里说过话）" in user


def test_raw_media_blobs_in_the_context_become_a_placeholder():
    context = ({"sender_name": "a", "content": "ab12" * 30},)
    _, user = prompts(make(context_messages=context))
    assert "a: [图片]" in user and "ab12ab12" not in user


def test_media_placeholders_and_blank_lines_are_skipped():
    context = ({"sender_name": "a", "content": "  "}, {"sender_name": "b", "content": "[图片]"})
    _, user = prompts(make(context_messages=context))
    assert "b: [图片]" in user and "a:" not in user


# ── scenes ──────────────────────────────────────────────────────────────

def test_scene_wording_follows_the_route():
    assert "里的普通群友" in prompts(make())[0]
    assert "会看到每条新消息的普通群友" in prompts(make(**{"level": LEVEL_AMBIENT, "signals": ()}, ambient=True))[0]
    forced = prompts(make(level=LEVEL_EXPLICIT, signals=(SIGNAL_FORCED,), ambient=True, force_reply=True))[0]
    assert "会逐条回复" in forced and "必须回复" in forced
    private = prompts(make(is_group=False, signals=(SIGNAL_PRIVATE,)))[0]
    assert "一对一微信私聊" in private and "不要提“群里大家”" in private


def test_proactive_mode_text_goes_into_the_user_prompt():
    mode = ProactiveMode("LIVELY", "热闹", "多人同时讨论", "接短句", 6.5, 60, 0.5, 35, 2)
    context = tuple({"sender_name": "a", "content": f"c{i}"} for i in range(5))
    _, user = prompts(make(level=LEVEL_AMBIENT, signals=(), ambient=True, mode=mode,
                           context_messages=context))
    assert "氛围模式：热闹（多人同时讨论）" in user
    assert "你应该：接短句" in user
    assert "c3" in user and "c4" in user and "c2" not in user      # the mode caps the context


# ── facts ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("signal,needle", [
    (SIGNAL_AT, "@ 了你"), (SIGNAL_QUOTE_BOT, "引用了你的上一句话"),
    (SIGNAL_PRIVATE, "私聊"), (SIGNAL_FORCED, "逐条回复"),
    (SIGNAL_KEYWORD, "关键词"), (SIGNAL_NAME_CALL, "提到了你的名字"),
    (SIGNAL_STICKY, "刚得到过你的回复"),
    (SIGNAL_AT_OTHER, "@ 了别人"), (SIGNAL_QUOTE_OTHER, "引用了别人的话"),
])
def test_each_signal_becomes_a_fact(signal, needle):
    facts = render_facts(Addressing(LEVEL_STRONG, (signal,)), make())
    assert any(needle in fact for fact in facts)


def test_followup_fact_quotes_the_bot_and_the_local_reason():
    addressing = Addressing(LEVEL_WEAK, (SIGNAL_FOLLOWUP,), "short-question", "推荐稻香")
    (fact,) = render_facts(addressing, make())
    assert "「推荐稻香」" in fact and "short-question" in fact


def test_ambient_messages_are_marked_as_listening_only():
    (fact,) = render_facts(Addressing(LEVEL_AMBIENT), make())
    assert "旁听" in fact
    facts = render_facts(Addressing(LEVEL_AMBIENT, (SIGNAL_FULL_FEED,)), make())
    assert len(facts) == 2


# ── policy ──────────────────────────────────────────────────────────────

def test_policy_gets_looser_as_the_signal_gets_weaker():
    explicit = render_policy(make(), STAGE_SINGLE)
    strong = render_policy(make(LEVEL_STRONG, (SIGNAL_NAME_CALL,)), STAGE_SINGLE)
    weak = render_policy(make(LEVEL_WEAK, (SIGNAL_FOLLOWUP,)), STAGE_SINGLE)
    ambient = render_policy(make(LEVEL_AMBIENT, ()), STAGE_SINGLE)
    assert "明确是对你说的" in explicit and "应当是 reply" in explicit
    assert "很可能是对你说的" in strong
    assert "很可能不是对你说的" in weak
    assert "默认 silent" in ambient


def test_forced_and_private_must_reply():
    for request in (make(force_reply=True), make(is_group=False)):
        assert "action 必须是 reply" in render_policy(request, STAGE_SINGLE)


def test_generate_stage_says_the_decision_is_made():
    assert "你已经决定回复" in render_policy(make(LEVEL_AMBIENT, ()), STAGE_GENERATE)


# ── output format ───────────────────────────────────────────────────────

def test_decide_stage_asks_for_no_text():
    system, _ = prompts(make(LEVEL_AMBIENT, ()), STAGE_DECIDE)
    assert "永远写 []" in system and "判断器" in system
    assert "PERSONA" not in system and "RULES" in system


def test_the_examples_in_the_prompt_parse_with_the_real_parser():
    for stage in (STAGE_SINGLE, STAGE_GENERATE, STAGE_DECIDE):
        examples = [line for line in output_format(30, stage).splitlines() if line.startswith('{"addressee"')]
        assert len(examples) == 3                      # the schema line and two examples
        for line in examples[1:]:
            envelope = parse_envelope(line, max_chars=30, decide_only=(stage == STAGE_DECIDE))
            assert envelope.action in ("reply", "silent")


def test_the_schema_line_matches_the_json_schema():
    props = ENVELOPE_JSON_SCHEMA["properties"]
    assert set(props) == {"addressee", "action", "messages", "reason"}
    assert ENVELOPE_JSON_SCHEMA["additionalProperties"] is False
    assert json.loads(json.dumps(ENVELOPE_JSON_SCHEMA)) == ENVELOPE_JSON_SCHEMA


# ── budget ──────────────────────────────────────────────────────────────

def test_effective_max_chars():
    mode = ProactiveMode("BURST", "炸了", "d", "i", 8.5, 30, 0.7, 20, 80)
    assert effective_max_chars(SETTINGS, make()) == 30
    assert effective_max_chars(SETTINGS, make(mode=mode)) == 20
    assert effective_max_chars(SETTINGS, make(max_chars_override=99)) == 99
    assert effective_max_chars(SETTINGS, make(max_chars_override=1)) == 8
    assert effective_max_chars({**SETTINGS, "max_chars": 10}, make(mode=mode)) == 10


# ── repair turns ────────────────────────────────────────────────────────

def test_repair_turns_show_the_bad_answer_and_the_fault():
    turns = build_repair_messages("原问题", "x" * 900, "bad_json")
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[0]["content"] == "原问题"
    assert turns[1]["content"].endswith("…") and len(turns[1]["content"]) == 601
    assert "bad_json" in turns[2]["content"]
    assert build_repair_messages("q", "", "empty")[1]["content"] == "（空）"
