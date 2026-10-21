"""Configuration of the structured pipeline: defaults, parsing, validation."""

import os
from unittest.mock import patch

import pytest

from src.config import BotConfig, load_config

BASE = {"ANTHROPIC_API_KEY": "sk-ant-test"}


def load(**env):
    with patch.dict(os.environ, {**BASE, **env}, clear=True):
        return load_config()


def test_defaults_keep_the_legacy_behaviour():
    config = BotConfig()
    assert config.pipeline_v2 == "off"
    assert config.pipeline_repair is True and config.pipeline_json_mode is True
    assert config.bot_aliases == []
    assert config.pipeline_v2_groups == ["*"]
    assert config.proactive_gate_mode == "dice"
    assert (config.proactive_hourly_budget, config.proactive_score_threshold) == (6, 0.35)


def test_an_empty_environment_loads_the_same_defaults():
    config = load()
    assert config.pipeline_v2 == "off" and config.proactive_gate_mode == "dice"
    assert config.bot_aliases == []


@pytest.mark.parametrize("raw,expected", [
    ("off", "off"), ("", "off"), ("false", "off"), ("0", "off"), ("No", "off"),
    ("single", "single"), ("true", "single"), ("1", "single"), ("ON", "single"),
    ("two_step", "two_step"), ("two-step", "two_step"), ("TwoStep", "two_step"),
])
def test_pipeline_modes_accept_the_usual_spellings(raw, expected):
    assert load(PIPELINE_V2=raw).pipeline_v2 == expected


def test_an_unknown_pipeline_mode_is_rejected():
    with pytest.raises(RuntimeError, match="PIPELINE_V2"):
        load(PIPELINE_V2="triple")


def test_aliases_split_on_ascii_and_chinese_commas():
    config = load(BOT_ALIASES="阿柔, 小柔柔，柔柔,,")
    assert config.bot_aliases == ["阿柔", "小柔柔", "柔柔"]


def test_switches_can_be_turned_off():
    config = load(PIPELINE_REPAIR="false", PIPELINE_JSON_MODE="false")
    assert config.pipeline_repair is False and config.pipeline_json_mode is False


def test_gate_settings_are_read():
    config = load(PROACTIVE_GATE_MODE="Score", PROACTIVE_HOURLY_BUDGET="3",
                  PROACTIVE_SCORE_THRESHOLD="0.5")
    assert config.proactive_gate_mode == "score"
    assert (config.proactive_hourly_budget, config.proactive_score_threshold) == (3, 0.5)


@pytest.mark.parametrize("env,message", [
    ({"PROACTIVE_GATE_MODE": "random"}, "PROACTIVE_GATE_MODE"),
    ({"PROACTIVE_HOURLY_BUDGET": "-1"}, "PROACTIVE_HOURLY_BUDGET"),
    ({"PROACTIVE_HOURLY_BUDGET": "500"}, "PROACTIVE_HOURLY_BUDGET"),
    ({"PROACTIVE_SCORE_THRESHOLD": "0"}, "PROACTIVE_SCORE_THRESHOLD"),
    ({"PROACTIVE_SCORE_THRESHOLD": "1.5"}, "PROACTIVE_SCORE_THRESHOLD"),
])
def test_invalid_gate_settings_are_rejected(env, message):
    with pytest.raises(RuntimeError, match=message):
        load(**env)


def test_a_zero_budget_means_unlimited_and_is_valid():
    assert load(PROACTIVE_HOURLY_BUDGET="0").proactive_hourly_budget == 0


def test_the_summarizer_factory_passes_the_pipeline_switches_on():
    from src.summarize import create_summarizer

    config = BotConfig(
        ai_backend="claude", anthropic_api_key="test-key",
        pipeline_repair=False, pipeline_json_mode=False,
    )
    summarizer = create_summarizer(config)
    assert summarizer.pipeline_repair is False and summarizer.json_mode is False
    default = create_summarizer(BotConfig(ai_backend="claude", anthropic_api_key="test-key"))
    assert default.pipeline_repair is True and default.json_mode is True


def test_the_group_list_defaults_to_everything_and_splits_on_both_commas():
    assert load().pipeline_v2_groups == ["*"]
    assert load(PIPELINE_V2_GROUPS="").pipeline_v2_groups == ["*"]
    assert load(PIPELINE_V2_GROUPS="测试群，项目讨论组, private").pipeline_v2_groups == [
        "测试群", "项目讨论组", "private",
    ]
