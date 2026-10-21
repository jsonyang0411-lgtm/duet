"""JSON-mode chat calls of the three backends (no network: clients are fakes)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
import openai
import pytest

from src.pipeline.envelope import ENVELOPE_JSON_SCHEMA
from src.summarize.claude_backend import ClaudeSummarizer
from src.summarize.deepseek_backend import DeepSeekSummarizer
from src.summarize.openai_backend import OpenAISummarizer

ANSWER = '{"addressee":"me","action":"reply","messages":["好呀"],"reason":"测试"}'
MESSAGES = [{"role": "user", "content": "hi"}]


def bad_request_openai():
    request = httpx.Request("POST", "https://x.example/v1/chat/completions")
    return openai.BadRequestError(
        "response_format is not supported", response=httpx.Response(400, request=request), body=None,
    )


def unprocessable_openai():
    request = httpx.Request("POST", "https://x.example/v1/chat/completions")
    return openai.UnprocessableEntityError(
        "invalid parameter response_format", response=httpx.Response(422, request=request), body=None,
    )


def unprocessable_anthropic():
    request = httpx.Request("POST", "https://x.example/v1/messages")
    return anthropic.UnprocessableEntityError(
        "invalid parameter output_config", response=httpx.Response(422, request=request), body=None,
    )


def bad_request_anthropic():
    request = httpx.Request("POST", "https://x.example/v1/messages")
    return anthropic.BadRequestError(
        "output_config is not supported", response=httpx.Response(400, request=request), body=None,
    )


# ── Claude ──────────────────────────────────────────────────────────────

def claude():
    summarizer = ClaudeSummarizer(api_key="k", chat_model="claude-haiku-4-5-20251001")
    summarizer.client = MagicMock()
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=ANSWER)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
    )
    summarizer.client.messages.create.return_value = response
    return summarizer


def test_claude_asks_for_schema_constrained_json_and_reports_usage():
    summarizer = claude()
    output = summarizer._call_json_api("sys", MESSAGES)

    kwargs = summarizer.client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5-20251001"
    assert kwargs["system"] == "sys" and kwargs["messages"] == MESSAGES
    assert kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": ENVELOPE_JSON_SCHEMA},
    }
    assert output.text == ANSWER and output.native_json is True
    assert output.usage == {"input_tokens": 120, "output_tokens": 30}


@pytest.mark.parametrize("error", [
    TypeError("unexpected keyword output_config"), bad_request_anthropic(), unprocessable_anthropic(),
])
def test_claude_falls_back_to_prompt_only_json_once_and_remembers(error):
    summarizer = claude()
    good = summarizer.client.messages.create.return_value
    summarizer.client.messages.create.side_effect = [error, good, good]

    first = summarizer._call_json_api("sys", MESSAGES)
    second = summarizer._call_json_api("sys", MESSAGES)

    calls = summarizer.client.messages.create.call_args_list
    assert "output_config" in calls[0].kwargs
    assert "output_config" not in calls[1].kwargs and "output_config" not in calls[2].kwargs
    assert first.native_json is False and second.native_json is False
    assert first.text == ANSWER


def test_claude_json_mode_can_be_switched_off():
    summarizer = claude()
    summarizer.configure_pipeline(json_mode=False)
    summarizer._call_json_api("sys", MESSAGES)
    assert "output_config" not in summarizer.client.messages.create.call_args.kwargs


def test_claude_joins_text_blocks_and_tolerates_missing_usage():
    summarizer = claude()
    summarizer.client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="{"), SimpleNamespace(type="tool_use"),
                 SimpleNamespace(type="text", text="}")],
    )
    output = summarizer._call_json_api("sys", MESSAGES)
    assert output.text == "{}" and output.usage is None


def test_claude_errors_other_than_the_param_rejection_propagate_when_not_native():
    summarizer = claude()
    summarizer.configure_pipeline(json_mode=False)
    summarizer.client.messages.create.side_effect = bad_request_anthropic()
    with pytest.raises(anthropic.BadRequestError):
        summarizer._call_json_api("sys", MESSAGES)


# ── DeepSeek ────────────────────────────────────────────────────────────

def deepseek():
    summarizer = DeepSeekSummarizer(api_key="k", model="deepseek-v4-flash")
    create = MagicMock()
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=ANSWER))],
        usage=SimpleNamespace(prompt_tokens=200, completion_tokens=40),
    )
    summarizer.client = MagicMock()
    summarizer.client.with_options.return_value.chat.completions.create = create
    summarizer._create = create
    return summarizer


def test_deepseek_uses_json_output_mode_with_thinking_off():
    summarizer = deepseek()
    output = summarizer._call_json_api("sys", MESSAGES)

    kwargs = summarizer._create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["messages"][1:] == MESSAGES
    assert kwargs["model"] == "deepseek-v4-flash" and kwargs["max_tokens"] == 600
    assert output.text == ANSWER and output.native_json is True
    assert output.usage == {"input_tokens": 200, "output_tokens": 40}


@pytest.mark.parametrize("rejection", [bad_request_openai, unprocessable_openai])
def test_deepseek_falls_back_when_json_output_is_rejected_and_remembers(rejection):
    summarizer = deepseek()
    good = summarizer._create.return_value
    summarizer._create.side_effect = [rejection(), good, good]

    first = summarizer._call_json_api("sys", MESSAGES)
    second = summarizer._call_json_api("sys", MESSAGES)

    calls = summarizer._create.call_args_list
    assert "response_format" in calls[0].kwargs
    assert "response_format" not in calls[1].kwargs and "response_format" not in calls[2].kwargs
    assert not first.native_json and not second.native_json


def test_deepseek_none_content_becomes_empty_text():
    summarizer = deepseek()
    summarizer._create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None))], usage=None,
    )
    output = summarizer._call_json_api("sys", MESSAGES)
    assert output.text == "" and output.usage is None


# ── OpenAI-compatible ───────────────────────────────────────────────────

def openai_backend():
    summarizer = OpenAISummarizer(api_key="k", model="gpt-x", base_url="https://relay.example/v1")
    summarizer._create_chat_completion = MagicMock(return_value={
        "choices": [{"message": {"content": ANSWER}}],
        "usage": {"prompt_tokens": 90, "completion_tokens": 25},
    })
    return summarizer


def test_openai_compatible_uses_json_object_response_format():
    summarizer = openai_backend()
    output = summarizer._call_json_api("sys", MESSAGES)

    args, kwargs = summarizer._create_chat_completion.call_args
    assert args[0][0] == {"role": "system", "content": "sys"}
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["max_tokens"] == 600
    assert output.text == ANSWER and output.native_json is True
    assert output.usage == {"input_tokens": 90, "output_tokens": 25}


@pytest.mark.parametrize("status", [400, 422])
def test_openai_compatible_falls_back_when_the_relay_rejects_the_format(status):
    summarizer = openai_backend()
    good = summarizer._create_chat_completion.return_value
    summarizer._create_chat_completion.side_effect = [
        RuntimeError(f"OpenAI-compatible API error {status}: unknown parameter response_format"),
        good, good,
    ]

    first = summarizer._call_json_api("sys", MESSAGES)
    second = summarizer._call_json_api("sys", MESSAGES)

    calls = summarizer._create_chat_completion.call_args_list
    assert "response_format" in calls[0].kwargs
    assert "response_format" not in calls[1].kwargs and "response_format" not in calls[2].kwargs
    assert not first.native_json and not second.native_json


def test_openai_compatible_other_errors_are_not_swallowed():
    summarizer = openai_backend()
    summarizer._create_chat_completion.side_effect = RuntimeError(
        "OpenAI-compatible API error 401: bad key")
    with pytest.raises(RuntimeError, match="401"):
        summarizer._call_json_api("sys", MESSAGES)
    assert summarizer._create_chat_completion.call_count == 1


def test_openai_compatible_without_usage():
    summarizer = openai_backend()
    summarizer._create_chat_completion.return_value = {"choices": [{"message": {"content": ANSWER}}]}
    assert summarizer._call_json_api("sys", MESSAGES).usage is None


# ── shared ──────────────────────────────────────────────────────────────

def test_configure_pipeline_switches_default_to_on():
    for summarizer in (claude(), deepseek(), openai_backend()):
        assert summarizer.json_mode is True and summarizer.pipeline_repair is True
        summarizer.configure_pipeline(repair=False, json_mode=False)
        assert summarizer.json_mode is False and summarizer.pipeline_repair is False
