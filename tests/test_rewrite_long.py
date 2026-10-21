"""PIPELINE_REWRITE_LONG: an over-long reply is sent back once to be rewritten shorter."""

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config import BotConfig, load_config
from src.pipeline import engine
from src.pipeline.addressing import LEVEL_EXPLICIT, LEVEL_STRONG, Addressing
from src.pipeline.types import ChatRequest

SETTINGS = {"persona_name": "林小满", "persona_prompt": "人设", "reply_rules": "规则",
            "max_chars": 12, "context_count": 10}
LONG = "今天的火锅真的特别好吃，下次我们还要一起去那家店。"
SHORT = "火锅超好吃，下次还去"


def answer(text, **extra):
    return json.dumps({"addressee": "me", "action": "reply", "messages": [text], "reason": "", **extra},
                      ensure_ascii=False)


class Model:
    pipeline_repair = True

    def __init__(self, *answers, rewrite=True):
        self.answers = list(answers)
        self.pipeline_rewrite_long = rewrite
        self.prompts, self.conversations = [], []

    def _chat_persona_settings(self):
        return SETTINGS

    def _retry_with_backoff(self, fn, label, max_attempts=2):
        return fn()

    def _call_json_api(self, system, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        self.conversations.append(messages)
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, usage=None, native_json=True)

    def _record_call(self, **fields):
        pass


def request(level=LEVEL_EXPLICIT, **fields):
    return ChatRequest(message="好吃吗", bot_name="林小满", addressing=Addressing(level), **fields)


def test_an_over_long_reply_is_rewritten_once():
    model = Model(answer(LONG), answer(SHORT))
    decision = engine.decide(model, request())
    assert decision.messages == (SHORT,) and decision.calls == 2
    assert "rewritten_long" in decision.warnings and "truncated" not in decision.warnings
    assert "超过 12 个字" in model.prompts[1]


def test_a_reply_that_fits_costs_nothing_extra():
    model = Model(answer(SHORT))
    decision = engine.decide(model, request())
    assert decision.messages == (SHORT,) and decision.calls == 1 and len(model.prompts) == 1


def test_switched_off_the_reply_is_cut_as_before():
    model = Model(answer(LONG), rewrite=False)
    decision = engine.decide(model, request())
    assert decision.calls == 1 and "truncated" in decision.warnings
    assert len(decision.messages[0]) <= 12


@pytest.mark.parametrize("second", [
    answer(LONG),                                                     # still too long
    '{"addressee":"me","action":"silent","messages":[],"reason":""}',  # changed its mind
    "not json",                                                       # unusable
    RuntimeError("API down"),                                         # transport error
])
def test_when_the_rewrite_does_not_help_the_cut_reply_stands(second):
    model = Model(answer(LONG), second)
    decision = engine.decide(model, request())
    assert decision.is_reply and "truncated" in decision.warnings and "rewrite_failed" in decision.warnings
    assert len(decision.messages[0]) <= 12 and decision.calls == 2


def test_the_rewrite_keeps_a_new_sticker_choice():
    model = Model(answer(LONG), answer(SHORT, sticker="问号"))
    decision = engine.decide(model, request(stickers=(("问号", "没听懂"),)))
    assert decision.sticker == "问号" and decision.messages == (SHORT,)


def test_two_steps_rewrite_only_the_written_reply():
    decide = '{"addressee":"me","action":"reply","messages":[],"reason":""}'
    model = Model(decide, answer(LONG), answer(SHORT))
    decision = engine.decide(model, request(level=LEVEL_STRONG), strategy="two_step")
    assert decision.messages == (SHORT,) and decision.calls == 3


def test_a_repaired_answer_is_rewritten_from_the_repaired_text():
    model = Model("oops", answer(LONG), answer(SHORT))
    decision = engine.decide(model, request())
    assert decision.messages == (SHORT,) and decision.calls == 3
    assert decision.status == "repaired"


def test_the_setting_reaches_the_summarizer():
    from src.summarize import create_summarizer
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "PIPELINE_REWRITE_LONG": "on"}, clear=True):
        config = load_config()
    assert config.pipeline_rewrite_long == "on"
    assert create_summarizer(config).pipeline_rewrite_long is True
    assert BotConfig().pipeline_rewrite_long == "off"
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "PIPELINE_REWRITE_LONG": "maybe"}, clear=True):
        with pytest.raises(RuntimeError, match="PIPELINE_REWRITE_LONG"):
            load_config()


# ── review findings ─────────────────────────────────────────────────────────

def test_only_the_text_is_taken_from_the_rewrite():
    from src.pipeline.envelope import FollowUp
    first = answer(LONG, follow_up={"expect": "sender", "seconds": 60})
    second = json.dumps({"addressee": "others", "action": "reply", "messages": [SHORT], "reason": "换了"},
                        ensure_ascii=False)
    model = Model(first, second)
    decision = engine.decide(model, request(declare_followup=True))
    assert decision.messages == (SHORT,) and decision.addressee == "me"
    assert decision.reason == "" and decision.follow_up == FollowUp("sender", 60)


def test_a_rewrite_into_a_sticker_alone_is_refused():
    second = json.dumps({"addressee": "me", "action": "reply", "messages": [], "reason": "", "sticker": "问号"},
                        ensure_ascii=False)
    model = Model(answer(LONG), second)
    decision = engine.decide(model, request(stickers=(("问号", "没听懂"),)))
    assert "rewrite_failed" in decision.warnings and decision.sticker == ""


def test_a_reply_meant_for_someone_else_is_not_rewritten():
    model = Model(json.dumps({"addressee": "others", "action": "reply", "messages": [LONG], "reason": ""},
                             ensure_ascii=False))
    decision = engine.decide(model, request(level=LEVEL_STRONG))
    assert decision.calls == 1 and "rewrite_failed" not in decision.warnings


def test_other_warnings_do_not_ask_for_a_rewrite():
    model = Model('{"action":"reply","messages":["火锅好吃"],"reason":""}')            # addressee missing
    decision = engine.decide(model, request())
    assert decision.calls == 1 and "addressee_missing" in decision.warnings


def test_the_repaired_text_is_what_the_rewrite_shows():
    model = Model("oops", answer(LONG), answer(SHORT))
    engine.decide(model, request())
    shown = model.conversations[2][1]["content"]
    assert LONG in shown and "oops" not in shown


def test_the_summarizer_is_off_unless_configured():
    from src.summarize import create_summarizer
    # The Anthropic client reads ~/.config/anthropic: keep the home directory
    # variables (Windows has no fallback when USERPROFILE is gone).
    home = {k: v for k, v in os.environ.items() if k in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", **home}, clear=True):
        assert create_summarizer(load_config()).pipeline_rewrite_long is False
