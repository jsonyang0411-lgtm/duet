"""LOCAL_TASKS=decide (the local model decides, the cloud model writes) and
CLOUD_FALLBACK=local (the local model answers when the cloud model fails)."""

import json
import os
from unittest.mock import patch

import httpx
import pytest

from test_model_route import CLOUD, local_config
from test_pipeline_prompts import make

from src.config import load_config
from src.pipeline import engine
from src.pipeline.addressing import LEVEL_STRONG, SIGNAL_NAME_CALL
from src.summarize import create_models
from src.summarize.fallback import FALLBACK_COOLDOWN_SEC, install_fallback
from src.summarize.openai_backend import OpenAISummarizer

REPLY = '{"addressee":"me","action":"reply","messages":["%s"],"reason":"x"}'
DECIDE = '{"addressee":"me","action":"%s","messages":[],"reason":"x"}'


def serve(summarizer, answers):
    """Answer this model's requests from ``answers`` (text, or an int HTTP status); record them."""
    seen = []
    answers = list(answers)

    def handler(request):
        seen.append(json.loads(request.content))
        answer = answers.pop(0)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": {"code": "API_KEY_QUOTA_EXHAUSTED"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    summarizer.client = httpx.Client(transport=httpx.MockTransport(handler))
    return seen


def unclear():
    """A message that gets the "should I speak?" step: the bot's name, no @."""
    return make(level=LEVEL_STRONG, signals=(SIGNAL_NAME_CALL,))


def split(**overrides):
    return create_models(local_config(model_route="split", pipeline_v2="two_step", **CLOUD, **overrides))


# ── LOCAL_TASKS=decide ──────────────────────────────────────────────────

def test_the_local_model_decides_and_the_cloud_model_writes():
    models = split(local_tasks=["memory", "decide"])
    cloud, local = models.chat, models.memory
    assert cloud.model == "gpt" and cloud.decide_model is local and models.proactive is cloud
    cloud_seen = serve(cloud, [REPLY % "我在"])
    local_seen = serve(local, [DECIDE % "reply"])
    decision = engine.decide(cloud, unclear(), strategy="two_step")
    assert decision.messages == ("我在",) and decision.calls == 2
    assert "判断器" in local_seen[0]["messages"][0]["content"]          # the decide prompt went local
    assert "判断器" not in cloud_seen[0]["messages"][0]["content"]


def test_a_local_silence_costs_no_cloud_call():
    models = split(local_tasks=["memory", "decide"])
    cloud_seen = serve(models.chat, [])
    serve(models.memory, [DECIDE % "silent"])
    decision = engine.decide(models.chat, unclear(), strategy="two_step")
    assert decision.action == "silent" and cloud_seen == []


def test_an_at_message_goes_straight_to_the_cloud_model():
    models = split(local_tasks=["memory", "decide"])
    cloud_seen = serve(models.chat, [REPLY % "好呀"])
    local_seen = serve(models.memory, [])
    assert engine.decide(models.chat, make(), strategy="two_step").messages == ("好呀",)
    assert len(cloud_seen) == 1 and local_seen == []


def test_the_cloud_model_decides_when_the_local_one_is_down():
    models = split(local_tasks=["memory", "decide"])
    cloud_seen = serve(models.chat, [DECIDE % "reply", REPLY % "我在"])
    models.memory.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    with patch.dict(os.environ, {"CHAT_API_MAX_ATTEMPTS": "1"}):
        decision = engine.decide(models.chat, unclear(), strategy="two_step")
    assert decision.messages == ("我在",) and len(cloud_seen) == 2


def test_without_decide_nothing_changes():
    models = split()
    assert models.chat.decide_model is None
    cloud_seen = serve(models.chat, [DECIDE % "reply", REPLY % "我在"])
    assert engine.decide(models.chat, unclear(), strategy="two_step").messages == ("我在",)
    assert len(cloud_seen) == 2


# ── CLOUD_FALLBACK=local ────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fallback_pair():
    cloud = OpenAISummarizer(api_key="k", model="gpt", base_url="https://relay.example/v1")
    local = OpenAISummarizer(api_key="local", model="qwen3.5:4b", base_url="http://127.0.0.1:11434/v1")
    clock = Clock()
    state = install_fallback(cloud, local, clock=clock)
    return cloud, local, clock, state


def test_a_failed_cloud_call_is_answered_by_the_local_model_and_the_cloud_rests():
    cloud, local, clock, state = fallback_pair()
    cloud_seen = serve(cloud, [429, "云端回来了"])
    local_seen = serve(local, ["本地", "本地二"])
    assert cloud._call_chat_api("系统", [{"role": "user", "content": "在吗"}]) == "本地"
    assert local_seen[0]["messages"][-1]["content"] == "在吗" and len(cloud_seen) == 1
    clock.now += FALLBACK_COOLDOWN_SEC - 1                       # still resting: straight to local
    assert cloud._call_json_api("系统", [{"role": "user", "content": "在吗"}]).text == "本地二"
    assert len(cloud_seen) == 1 and state.fallbacks == 2
    clock.now += 2                                               # tried again
    assert cloud._call_chat_api("系统", [{"role": "user", "content": "在吗"}]) == "云端回来了"


def test_the_pipeline_reply_survives_an_exhausted_cloud_key():
    cloud, local, _, _ = fallback_pair()
    serve(cloud, [429])
    serve(local, [REPLY % "本地回的"])
    with patch.dict(os.environ, {"CHAT_API_MAX_ATTEMPTS": "1"}):
        assert engine.decide(cloud, make()).messages == ("本地回的",)


def test_create_models_installs_the_fallback_on_the_cloud_model_only():
    models = create_models(local_config(model_route="cloud", cloud_fallback="local", **CLOUD))
    assert models.chat.model == "gpt" and models.chat.cloud_fallback.local.model == "qwen3.5:4b"
    models = split(cloud_fallback="local")
    assert models.chat.cloud_fallback.local is models.memory
    assert not hasattr(models.memory, "cloud_fallback")
    assert not hasattr(create_models(local_config(model_route="cloud", **CLOUD)).chat, "cloud_fallback")


# ── settings ────────────────────────────────────────────────────────────

def load(**env):
    with patch.dict(os.environ, env, clear=True):
        return load_config()


BASE = dict(ANTHROPIC_API_KEY="sk-ant-test", LOCAL_MODEL="qwen3.5:4b")


def test_decide_needs_the_two_step_strategy():
    config = load(**BASE, MODEL_ROUTE="split", LOCAL_TASKS="memory,decide", PIPELINE_V2="two_step")
    assert config.local_tasks == ["memory", "decide"]
    with pytest.raises(RuntimeError, match="PIPELINE_V2=two_step"):
        load(**BASE, MODEL_ROUTE="split", LOCAL_TASKS="decide", PIPELINE_V2="single")


def test_the_fallback_setting_is_checked():
    assert load(**BASE).cloud_fallback == "off"
    assert load(**BASE, CLOUD_FALLBACK="Local").cloud_fallback == "local"
    with pytest.raises(RuntimeError, match="CLOUD_FALLBACK must be"):
        load(**BASE, CLOUD_FALLBACK="on")
    with pytest.raises(RuntimeError, match="CLOUD_FALLBACK=local needs LOCAL_MODEL"):
        load(ANTHROPIC_API_KEY="sk-ant-test", CLOUD_FALLBACK="local")


# ── the local model reads images (fully local) ──────────────────────────

def _gif(path):
    from PIL import Image
    frames = [Image.new("RGB", (8, 8), color) for color in ("yellow", "green")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return str(path)


def test_a_gif_goes_to_the_local_model_as_a_png_frame(tmp_path):
    from src.vision import image_to_data_url
    gif = _gif(tmp_path / "blink.gif")
    assert image_to_data_url(gif).startswith("data:image/gif;base64,")          # the cloud gets it as it is
    still = image_to_data_url(gif, still=True)
    assert still.startswith("data:image/png;base64,")
    import base64
    assert base64.b64decode(still.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_local_model_looks_with_its_own_name(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_VISION_MODEL", "gpt-vision-cloud")
    local = create_models(local_config()).chat
    assert local.is_local is True
    seen = serve(local, ["黄色的"])
    assert local.describe_image(_gif(tmp_path / "s.gif"), "什么颜色", requester_name="阿明") == "黄色的"
    assert seen[0]["model"] == "qwen3.5:4b"
    image = seen[0]["messages"][1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,")
    cloud = OpenAISummarizer(api_key="k", model="gpt", base_url="https://relay.example/v1")
    assert cloud.is_local is False
    seen = serve(cloud, ["黄色的"])
    cloud.describe_image(_gif(tmp_path / "c.gif"), "什么颜色", requester_name="阿明")
    assert seen[0]["model"] == "gpt-vision-cloud"                              # unchanged for the cloud
    assert seen[0]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/gif;base64,")



def test_the_label_names_what_answers():
    from src.summarize import model_label
    assert model_label(local_config(model_route="cloud", **CLOUD)) == "openai"
    assert model_label(local_config()) == "本地 qwen3.5:4b"
    assert model_label(local_config(model_route="split", **CLOUD)) == "openai + 本地 qwen3.5:4b"
