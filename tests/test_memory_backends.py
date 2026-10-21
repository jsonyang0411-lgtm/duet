"""complete_json: the background-task entry point of every backend (no network)."""

import pytest

from test_pipeline_backends import ANSWER, MESSAGES, claude, deepseek, openai_backend
from test_pipeline_engine import ScriptedSummarizer

from src.memory.extraction import EXTRACTION_SCHEMA
from src.pipeline.envelope import ENVELOPE_JSON_SCHEMA
from src.pipeline.types import ModelOutput


class Recording(ScriptedSummarizer):
    """Accepts the keyword arguments complete_json passes down."""

    def _call_json_api(self, system_prompt, messages, *, schema=None, max_tokens=None):
        self.kwargs = {"schema": schema, "max_tokens": max_tokens}
        return super()._call_json_api(system_prompt, messages)


def test_complete_json_passes_the_prompts_schema_and_budget_down_and_logs_the_call():
    summarizer = Recording("{}")
    output = summarizer.complete_json("系统", "用户", schema=EXTRACTION_SCHEMA, max_tokens=1234,
                                      call_name="memory_extract")
    assert output.text == "{}"
    assert summarizer.kwargs == {"schema": EXTRACTION_SCHEMA, "max_tokens": 1234}
    system, messages = summarizer.calls[0]
    assert system == "系统" and messages == [{"role": "user", "content": "用户"}]
    [record] = summarizer.records
    assert record["call"] == "memory_extract" and record["raw_reply"] == "{}"
    assert record["user_prompt"] == "用户" and record["usage"] is None


def test_complete_json_defaults_to_no_schema():
    summarizer = Recording("{}")
    summarizer.complete_json("s", "u")
    assert summarizer.kwargs == {"schema": None, "max_tokens": None}


def test_complete_json_logs_failures_and_reraises():
    class Boom(Recording):
        retry_exceptions = ()

        def _call_json_api(self, system_prompt, messages, **kw):
            raise ValueError("kaputt")

    summarizer = Boom()
    with pytest.raises(ValueError):
        summarizer.complete_json("s", "u", call_name="memory_extract")
    [record] = summarizer.records
    assert record["call"] == "memory_extract" and "kaputt" in record["error"]


def test_the_base_default_ignores_schema_and_budget():
    class Plain(ScriptedSummarizer):
        def _call_chat_api(self, system_prompt, messages):
            return "plain"
    # AbstractSummarizer._call_json_api falls back to the chat call
    from src.summarize.base import AbstractSummarizer
    out = AbstractSummarizer._call_json_api(Plain(), "s", MESSAGES, schema={"x": 1}, max_tokens=5)
    assert out == ModelOutput(text="plain")


# ── Claude ──────────────────────────────────────────────────────────────

def test_claude_without_a_schema_asks_for_json_in_the_prompt_only():
    summarizer = claude()
    output = summarizer._call_json_api("sys", MESSAGES, schema=None, max_tokens=2000)
    kwargs = summarizer.client.messages.create.call_args.kwargs
    assert "output_config" not in kwargs and kwargs["max_tokens"] == 2000
    assert output.native_json is False and output.text == ANSWER


def test_claude_uses_the_schema_it_is_given():
    summarizer = claude()
    output = summarizer._call_json_api("sys", MESSAGES, schema=EXTRACTION_SCHEMA, max_tokens=2000)
    kwargs = summarizer.client.messages.create.call_args.kwargs
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}}
    assert output.native_json is True


def test_claude_chat_calls_keep_the_envelope_schema_and_budget():
    summarizer = claude()
    summarizer._call_json_api("sys", MESSAGES)
    kwargs = summarizer.client.messages.create.call_args.kwargs
    assert kwargs["output_config"]["format"]["schema"] == ENVELOPE_JSON_SCHEMA
    assert kwargs["max_tokens"] == 600


# ── DeepSeek / OpenAI-compatible ─────────────────────────────────────────

def test_deepseek_honours_the_output_budget_and_ignores_the_schema():
    summarizer = deepseek()
    summarizer._call_json_api("sys", MESSAGES, schema=EXTRACTION_SCHEMA, max_tokens=2000)
    kwargs = summarizer._create.call_args.kwargs
    assert kwargs["max_tokens"] == 2000 and kwargs["response_format"] == {"type": "json_object"}
    summarizer._call_json_api("sys", MESSAGES)
    assert summarizer._create.call_args.kwargs["max_tokens"] == 600


def test_openai_compatible_honours_the_output_budget():
    summarizer = openai_backend()
    summarizer._call_json_api("sys", MESSAGES, schema=EXTRACTION_SCHEMA, max_tokens=2000)
    assert summarizer._create_chat_completion.call_args.kwargs["max_tokens"] == 2000
    summarizer._call_json_api("sys", MESSAGES)
    assert summarizer._create_chat_completion.call_args.kwargs["max_tokens"] == 600


# ── a refused custom schema must not switch off the chat pipeline's JSON mode ──

def test_a_refused_extraction_schema_does_not_disable_the_envelope_schema():
    import anthropic
    from test_pipeline_backends import bad_request_anthropic

    summarizer = claude()
    good = summarizer.client.messages.create.return_value
    summarizer.client.messages.create.side_effect = [bad_request_anthropic(), good, good, good]
    summarizer._call_json_api("sys", MESSAGES, schema=EXTRACTION_SCHEMA, max_tokens=2000)     # refused, retried
    summarizer._call_json_api("sys", MESSAGES, schema=EXTRACTION_SCHEMA, max_tokens=2000)     # remembered
    summarizer._call_json_api("sys", MESSAGES)                                                # the chat envelope
    calls = summarizer.client.messages.create.call_args_list
    assert "output_config" in calls[0].kwargs and "output_config" not in calls[1].kwargs
    assert "output_config" not in calls[2].kwargs                      # the custom schema stays off ...
    assert calls[3].kwargs["output_config"]["format"]["schema"] == ENVELOPE_JSON_SCHEMA   # ... the envelope is untouched
    assert summarizer._json_mode_unsupported is False and id(EXTRACTION_SCHEMA) in summarizer._refused_schemas


def test_a_refused_envelope_schema_does_not_disable_the_extraction_schema():
    from test_pipeline_backends import bad_request_anthropic

    summarizer = claude()
    good = summarizer.client.messages.create.return_value
    summarizer.client.messages.create.side_effect = [bad_request_anthropic(), good, good]
    summarizer._call_json_api("sys", MESSAGES)                                                # envelope refused
    summarizer._call_json_api("sys", MESSAGES, schema=EXTRACTION_SCHEMA, max_tokens=2000)
    assert "output_config" in summarizer.client.messages.create.call_args_list[2].kwargs
