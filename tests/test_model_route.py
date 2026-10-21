"""MODEL_ROUTE: a cloud model, a local one, or split by task; local-model request options."""

import inspect
import json
import os
from unittest.mock import patch

import httpx
import pytest

from test_router_pipeline import MODE, PipelineSummarizer, TraceSpy
from test_router_priority import BOT, RouterTestCase, StubWebSearch

from src.config import BotConfig, load_config
from src.pipeline.types import Decision
from src.summarize import ModelSet, create_local_summarizer, create_models
from src.summarize.openai_backend import OpenAISummarizer, strip_reasoning


# ── the <think> part ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, answer", [
    ("<think>先想想……他在问我</think>好呀", "好呀"),
    ("<THINK>a\nb</THINK>\n{\"action\":\"reply\"}", "{\"action\":\"reply\"}"),
    ("他问的是周末</think>周末去爬山", "周末去爬山"),          # the template opened the block
    ("好呀<think>被截断的推理", "好呀"),                     # cut off while thinking
    ("<think>只有推理，没有答案", ""),
    ("普通回复", "普通回复"),
])
def test_the_reasoning_is_removed(raw, answer):
    assert strip_reasoning(raw) == answer


# ── the requests ────────────────────────────────────────────────────────────

def capture(summarizer, content="好的"):
    seen = []

    def handler(request):
        seen.append((json.loads(request.content), request.extensions.get("timeout")))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    summarizer.client = httpx.Client(transport=httpx.MockTransport(handler))
    return seen


def test_a_cloud_request_is_unchanged():
    cloud = OpenAISummarizer(api_key="k", model="gpt", base_url="https://relay.example/v1")
    seen = capture(cloud, "<think>x</think>好")
    assert cloud._call_chat_api("系统", [{"role": "user", "content": "在吗"}]) == "<think>x</think>好"
    payload, timeout = seen[0]
    assert set(payload) == {"model", "messages", "max_tokens"} and payload["max_tokens"] == 260
    assert timeout["read"] == 25.0


def local_config(**overrides):
    values = dict(model_route="local", local_model="qwen3.5:4b", local_base_url="http://127.0.0.1:11434/v1",
                  local_timeout_sec=90.0, local_max_tokens=2048)
    values.update(overrides)
    return BotConfig(**values)


def test_a_local_request_carries_the_options_and_the_answer_loses_its_thinking():
    local = create_local_summarizer(local_config())
    seen = capture(local, "<think>他在问我在不在</think>在的")
    assert local._call_chat_api("系统", [{"role": "user", "content": "在吗"}]) == "在的"
    payload, timeout = seen[0]
    assert payload["model"] == "qwen3.5:4b"
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["reasoning_effort"] == "none"                     # what Ollama reads
    assert payload["max_tokens"] == 2048 and timeout["read"] == 90.0
    output = local._call_json_api("系统", [{"role": "user", "content": "在吗"}])
    assert output.text == "在的" and seen[1][0]["response_format"] == {"type": "json_object"}
    assert local.base_url == "http://127.0.0.1:11434/v1" and local.api_key == "local"


def test_extra_fields_can_be_changed_or_dropped():
    local = create_local_summarizer(local_config(local_extra_body={"reasoning_effort": "none"}))
    seen = capture(local)
    local._call_chat_api("系统", [{"role": "user", "content": "在吗"}])
    assert seen[0][0]["reasoning_effort"] == "none" and "chat_template_kwargs" not in seen[0][0]
    bare = create_local_summarizer(local_config(local_extra_body={}))
    seen = capture(bare)
    bare._call_chat_api("系统", [{"role": "user", "content": "在吗"}])
    assert set(seen[0][0]) == {"model", "messages", "max_tokens"}


def test_the_local_model_gets_the_persona_and_pipeline_settings():
    local = create_local_summarizer(local_config(chat_persona_name="林小满", pipeline_rewrite_long="on"))
    assert local._chat_persona_settings()["persona_name"] == "林小满"
    assert local.pipeline_rewrite_long is True


# ── which model does what ───────────────────────────────────────────────────

CLOUD = dict(ai_backend="openai", openai_api_key="sk-x", openai_model="gpt")


def test_cloud_is_one_model_for_everything():
    models = create_models(BotConfig(**CLOUD))
    assert models.chat is models.proactive is models.memory
    assert models.chat.model == "gpt" and models.chat.extra_body is None


def test_local_is_the_local_model_for_everything():
    models = create_models(local_config())
    assert models.chat is models.proactive is models.memory and models.chat.model == "qwen3.5:4b"


def test_split_hands_the_named_groups_to_the_local_model():
    models = create_models(local_config(model_route="split", **CLOUD))
    assert models.chat.model == "gpt" and models.proactive.model == "gpt"
    assert models.memory.model == "qwen3.5:4b"
    models = create_models(local_config(model_route="split", local_tasks=["chat", "proactive"], **CLOUD))
    assert models.chat is models.proactive and models.chat.model == "qwen3.5:4b"
    assert models.memory.model == "gpt"


# ── settings ────────────────────────────────────────────────────────────────

def load(**env):
    with patch.dict(os.environ, env, clear=True):
        return load_config()


def test_the_settings_default_to_the_cloud():
    config = load(ANTHROPIC_API_KEY="sk-ant-test")
    assert (config.model_route, config.local_tasks, config.local_base_url, config.local_model) == (
        "cloud", ["memory"], "http://127.0.0.1:11434/v1", "")
    assert config.local_extra_body == {"reasoning_effort": "none",
                                       "chat_template_kwargs": {"enable_thinking": False}}
    assert (config.local_timeout_sec, config.local_max_tokens) == (90.0, 2048)


def test_local_needs_no_cloud_key_but_a_model():
    config = load(MODEL_ROUTE="local", LOCAL_MODEL="qwen3.5:4b", LOCAL_BASE_URL="http://192.168.1.5:11434/v1/")
    assert config.model_route == "local" and config.local_base_url == "http://192.168.1.5:11434/v1"
    with pytest.raises(RuntimeError, match="LOCAL_MODEL"):
        load(MODEL_ROUTE="local")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load(MODEL_ROUTE="split", LOCAL_MODEL="q")                                  # split still uses the cloud


@pytest.mark.parametrize("env, message", [
    ({"MODEL_ROUTE": "both"}, "MODEL_ROUTE"),
    ({"MODEL_ROUTE": "split", "LOCAL_MODEL": "q", "LOCAL_BASE_URL": "127.0.0.1:11434"}, "LOCAL_BASE_URL"),
    ({"LOCAL_TASKS": "memory,summary"}, "LOCAL_TASKS"),
    ({"LOCAL_EXTRA_BODY": "{not json"}, "LOCAL_EXTRA_BODY"),
    ({"LOCAL_EXTRA_BODY": "[1, 2]"}, "LOCAL_EXTRA_BODY"),
    ({"LOCAL_TIMEOUT_SEC": "1"}, "LOCAL_TIMEOUT_SEC"),
    ({"LOCAL_MAX_TOKENS": "100"}, "LOCAL_MAX_TOKENS"),
])
def test_bad_settings_are_named(env, message):
    with pytest.raises(RuntimeError, match=message):
        load(ANTHROPIC_API_KEY="sk-ant-test", **env)


def test_an_empty_extra_body_means_none():
    assert load(ANTHROPIC_API_KEY="sk-ant-test", LOCAL_EXTRA_BODY="").local_extra_body == {}


# ── wiring ──────────────────────────────────────────────────────────────────

class RouterModels(RouterTestCase):
    def test_proactive_turns_use_their_own_model(self):
        chat = PipelineSummarizer(Decision(action="reply", messages=("在呢",), addressee="me"))
        proactive = PipelineSummarizer(Decision(action="reply", messages=("哈哈",), addressee="group"))
        router, _ = self.make_router(summarizer=chat, pipeline_v2="single", web=StubWebSearch(),
                                     proactive_enabled=True)
        router._proactive_summarizer = proactive
        router._tracer = TraceSpy()
        self.gate(router, allow=True, mode=MODE)
        assert router.handle(self.group_msg("m1", "决赛太精彩了", at=False)) == "哈哈"
        assert proactive.decide_requests and not chat.decide_requests
        assert router.handle(self.group_msg("m2", f"@{BOT} 在吗")) == "在呢"
        assert len(chat.decide_requests) == 1

    def test_memory_work_and_the_call_log_use_the_given_models(self):
        chat = PipelineSummarizer()
        memory = PipelineSummarizer()
        recorded = []
        for model in (chat, memory):
            model.set_call_recorder = lambda recorder, model=model: recorded.append(model)

        class Tracer(TraceSpy):
            enabled = True

        cfg = self.make_config(memory_v2="on")
        from src.router import MessageRouter
        from src.trigger import TriggerDetector
        from unittest.mock import MagicMock
        router = MessageRouter(store=self.new_store(), detector=TriggerDetector([], BOT), summarizer=chat,
                               admin_handler=MagicMock(), nickname_service=MagicMock(), config=cfg,
                               tracer=Tracer(), memory_summarizer=memory)
        self.addCleanup(router.close)
        assert router._memory._summarizer is memory                       # the old diary, too
        assert router._memory_worker._summarizer is memory
        assert recorded == [chat, memory]


def test_the_bot_and_the_evaluations_follow_the_route():
    import src.bot as bot
    import evals.run_live as run_live
    import evals.run_live_memory as run_live_memory
    from tools import memory_admin
    source = inspect.getsource(bot.Bot.run)
    assert "models = create_models(config)" in source
    assert "proactive_summarizer=models.proactive" in source and "memory_summarizer=models.memory" in source
    assert "create_models(config).chat" in inspect.getsource(run_live)
    assert "create_models(config).memory" in inspect.getsource(run_live_memory)
    assert "create_models(load_config()).memory" in inspect.getsource(memory_admin)
    assert isinstance(ModelSet, type)


# ── tools/model_check.py ────────────────────────────────────────────────────

class FakeLocal:
    model = "qwen3.5:4b"
    base_url = "http://127.0.0.1:11434/v1"

    def __init__(self, answers, json_text='{"action":"reply","messages":["好的"]}', native=True):
        self.answers, self.json_text, self.native = list(answers), json_text, native
        self.strip_reasoning = True
        self.strip_seen = []

    def _call_chat_api(self, system, messages):
        self.strip_seen.append(self.strip_reasoning)
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _call_json_api(self, system, messages):
        from src.pipeline.types import ModelOutput
        return ModelOutput(text=self.json_text, native_json=self.native)


def run_check(model):
    import io
    from tools import model_check
    out = io.StringIO()
    ok = model_check.check_model("local", model, long_prompt=True, out=out)
    return ok, out.getvalue()


def test_a_good_local_model_passes():
    model = FakeLocal(["在的", "蓝莓-4721"])
    ok, text = run_check(model)
    assert ok and "answered in" in text and "native JSON mode: yes" in text and "kept whole" in text
    assert model.strip_seen[0] is False and model.strip_reasoning is True      # raw first, then restored


def test_thinking_a_lost_context_and_bad_json_are_reported():
    ok, text = run_check(FakeLocal(["<think>嗯</think>在的", "不知道"], json_text="我不会 JSON"))
    assert not ok
    assert "thinking is still on" in text and "start of a long prompt was lost" in text and "no usable JSON" in text


def test_an_unreachable_model_fails_cleanly():
    ok, text = run_check(FakeLocal([ConnectionError("refused")]))
    assert not ok and "not reachable" in text


class OllamaLike(FakeLocal):
    """Answers like Ollama: the thinking in message.reasoning, not in the text."""

    def __init__(self, reasoning, answers):
        super().__init__(answers)
        self.reasoning = reasoning

    def _create_chat_completion(self, messages, max_tokens, timeout=None, **kwargs):
        self.strip_seen.append(self.strip_reasoning)
        return {"choices": [{"message": {"content": "在的", "reasoning": self.reasoning}}]}


def test_thinking_in_a_separate_field_is_reported():
    ok, text = run_check(OllamaLike("用户在问我在不在……", ["蓝莓-4721"]))
    assert ok and f"thought before answering ({len('用户在问我在不在……')} characters of reasoning)" in text
    ok, text = run_check(OllamaLike("", ["蓝莓-4721"]))
    assert ok and "thought before answering" not in text
