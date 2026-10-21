"""Configuration of memory v2: defaults, parsing, validation."""

import os
from unittest.mock import patch

import pytest

from src.config import BotConfig, load_config

BASE = {"ANTHROPIC_API_KEY": "sk-ant-test"}


def load(**env):
    with patch.dict(os.environ, {**BASE, **env}, clear=True):
        return load_config()


def test_defaults_keep_the_feature_off():
    config = BotConfig()
    assert config.memory_v2 == "off" and config.memory_v2_groups == ["*"]
    assert (config.memory_window_messages, config.memory_idle_minutes) == (40, 20)
    assert (config.memory_min_confidence, config.memory_prompt_facts) == (0.6, 12)


def test_an_empty_environment_loads_the_same_defaults():
    config = load()
    assert config.memory_v2 == "off" and config.memory_v2_groups == ["*"]
    assert config.memory_window_messages == 40


@pytest.mark.parametrize("raw,expected", [
    ("on", "on"), ("ON", "on"), ("true", "on"), ("1", "on"), ("Yes", "on"),
    ("off", "off"), ("", "off"), ("false", "off"), ("0", "off"), ("No", "off"),
])
def test_the_switch_accepts_the_usual_spellings(raw, expected):
    assert load(MEMORY_V2=raw).memory_v2 == expected


def test_an_unknown_mode_is_rejected():
    with pytest.raises(RuntimeError, match="MEMORY_V2"):
        load(MEMORY_V2="maybe")


@pytest.mark.parametrize("name, bad", [
    ("MEMORY_WINDOW_MESSAGES", "4"), ("MEMORY_WINDOW_MESSAGES", "501"),
    ("MEMORY_IDLE_MINUTES", "0"), ("MEMORY_IDLE_MINUTES", "1441"),
    ("MEMORY_MIN_CONFIDENCE", "-0.1"), ("MEMORY_MIN_CONFIDENCE", "1.1"),
    ("MEMORY_PROMPT_FACTS", "0"), ("MEMORY_PROMPT_FACTS", "41"),
])
def test_out_of_range_numbers_are_rejected(name, bad):
    with pytest.raises(RuntimeError, match=name):
        load(**{name: bad})


def test_the_environment_is_read():
    config = load(
        MEMORY_V2="on", MEMORY_V2_GROUPS="群甲，群乙, private", MEMORY_WINDOW_MESSAGES="60",
        MEMORY_IDLE_MINUTES="30", MEMORY_MIN_CONFIDENCE="0.7", MEMORY_PROMPT_FACTS="8",
    )
    assert (config.memory_v2, config.memory_v2_groups) == ("on", ["群甲", "群乙", "private"])
    assert (config.memory_window_messages, config.memory_idle_minutes) == (60, 30)
    assert (config.memory_min_confidence, config.memory_prompt_facts) == (0.7, 8)


def test_an_empty_group_list_means_every_group():
    assert load(MEMORY_V2_GROUPS="").memory_v2_groups == ["*"]
