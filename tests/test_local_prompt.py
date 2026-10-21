"""LOCAL_PROMPT=compact: the chat prompt for a small local model."""

import os
from unittest.mock import patch

import pytest

from test_model_route import capture, local_config
from test_pipeline_prompts import NOW, SETTINGS, make

from src.config import load_config
from src.pipeline import engine
from src.pipeline.addressing import LEVEL_AMBIENT, LEVEL_WEAK, SIGNAL_FOLLOWUP
from src.pipeline.envelope import parse_envelope
from src.pipeline.prompts import (
    PROFILE_COMPACT,
    STAGE_DECIDE,
    STAGE_GENERATE,
    STAGE_SINGLE,
    build_prompts,
    output_format,
)
from src.summarize.openai_backend import OpenAISummarizer
from src.summarize import create_local_summarizer

REQUEST_RULES = "## 别人找你做事时（最要紧）"


def compact(request, stage=STAGE_SINGLE):
    return build_prompts(SETTINGS, request, stage=stage, now=NOW, profile=PROFILE_COMPACT)


def standard(request, stage=STAGE_SINGLE):
    return build_prompts(SETTINGS, request, stage=stage, now=NOW)


# ── the prompt ──────────────────────────────────────────────────────────

def test_the_request_rules_come_right_after_the_persona_and_end_the_user_prompt():
    system, user = compact(make())
    assert system.index("PERSONA") < system.index("RULES") < system.index(REQUEST_RULES) < system.index("## 场景理解铁律")
    assert "讲笑话" in system and "不要瞎解释" in system
    assert user.splitlines()[-2].endswith("对方要你做的事，这句话里就直接做到；不懂就说不懂，不要编。")
    assert compact(make(), STAGE_GENERATE)[1].splitlines()[-2].endswith("不要编。")


def test_empty_memory_and_search_sections_are_left_out():
    system, _ = compact(make())
    assert "## 长期记忆" not in system and "## 联网搜索" not in system
    system, _ = compact(make(group_memory="阿明喜欢吃火锅",
                             message="今晚吃什么\n联网搜索结果（这些是刚搜到的资料）：……"))
    assert "## 长期记忆\n阿明喜欢吃火锅\n如果长期记忆里有" in system and "## 联网搜索" in system


def test_the_compact_example_does_what_it_was_asked_and_parses():
    examples = [line for line in output_format(30, STAGE_SINGLE, profile=PROFILE_COMPACT).splitlines()
                if line.startswith('{"addressee"')]
    assert parse_envelope(examples[1], max_chars=30).messages == ("买吧，打折挺划算的",)
    assert '"messages":["好呀"]' not in compact(make())[0]


def test_the_decide_stage_and_the_cautious_policies_do_not_change():
    for request in (make(level=LEVEL_AMBIENT, signals=()), make(level=LEVEL_WEAK, signals=(SIGNAL_FOLLOWUP,))):
        assert compact(request, STAGE_DECIDE) == standard(request, STAGE_DECIDE)
        assert compact(request)[1] == standard(request)[1]


def test_the_standard_prompt_is_untouched():
    system, user = standard(make())
    assert REQUEST_RULES not in system and "## 长期记忆" in system and "## 联网搜索" in system
    assert '"messages":["好呀"]' in system and "不懂就说不懂" not in user


# ── the setting and the wiring ──────────────────────────────────────────

def load(**env):
    with patch.dict(os.environ, env, clear=True):
        return load_config()


def test_the_setting_defaults_to_standard_and_is_checked():
    assert load(ANTHROPIC_API_KEY="sk-ant-test").local_prompt == "standard"
    assert load(ANTHROPIC_API_KEY="sk-ant-test", LOCAL_PROMPT="Compact").local_prompt == "compact"
    with pytest.raises(RuntimeError, match="LOCAL_PROMPT"):
        load(ANTHROPIC_API_KEY="sk-ant-test", LOCAL_PROMPT="short")


def system_prompt_sent(summarizer):
    seen = capture(summarizer, '{"addressee":"me","action":"reply","messages":["好呀"],"reason":"x"}')
    decision = engine.decide(summarizer, make())
    assert decision.messages == ("好呀",)
    return seen[0][0]["messages"][0]["content"]


def test_only_the_local_model_set_to_compact_gets_the_compact_prompt():
    assert REQUEST_RULES in system_prompt_sent(create_local_summarizer(local_config(local_prompt="compact")))
    assert REQUEST_RULES not in system_prompt_sent(create_local_summarizer(local_config()))
    cloud = OpenAISummarizer(api_key="k", model="gpt", base_url="https://relay.example/v1")
    assert REQUEST_RULES not in system_prompt_sent(cloud)
