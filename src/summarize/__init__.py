"""Summarization module — factory for AI backends.

Usage:
    from .summarize import create_summarizer

    summarizer = create_summarizer(config)
    result = summarizer.summarize(messages, requester_name)
"""

import logging
from dataclasses import dataclass

from .base import AbstractSummarizer
from .claude_backend import ClaudeSummarizer
from .deepseek_backend import DeepSeekSummarizer
from .fallback import install_fallback
from .no_model import NoModel, relay_only
from .openai_backend import OpenAISummarizer
from .models import ParticipantContribution, SummaryResult

logger = logging.getLogger(__name__)


def _configure_chat_persona(summarizer: AbstractSummarizer, config) -> None:
    if not hasattr(summarizer, "configure_chat_persona"):
        return
    summarizer.configure_chat_persona(
        persona_name=getattr(config, "chat_persona_name", ""),
        persona_prompt=getattr(config, "chat_persona_prompt", ""),
        reply_rules=getattr(config, "chat_reply_rules", ""),
        max_chars=getattr(config, "chat_max_chars", 36),
        context_window_sec=getattr(config, "chat_context_window_sec", 900),
        context_count=getattr(config, "chat_context_count", 40),
    )
    if hasattr(summarizer, "configure_pipeline"):
        summarizer.configure_pipeline(
            repair=getattr(config, "pipeline_repair", True),
            json_mode=getattr(config, "pipeline_json_mode", True),
            rewrite_long=getattr(config, "pipeline_rewrite_long", "off") == "on",
        )

__all__ = [
    "AbstractSummarizer",
    "ClaudeSummarizer",
    "DeepSeekSummarizer",
    "OpenAISummarizer",
    "SummaryResult",
    "ParticipantContribution",
    "create_summarizer",
    "create_local_summarizer",
    "create_models",
    "model_label",
    "ModelSet",
]

# Task groups MODEL_ROUTE=split can hand to the local model (LOCAL_TASKS).
LOCAL_TASK_GROUPS = ("chat", "proactive", "memory")
# Not a model of its own: the "should I speak?" step of PIPELINE_V2=two_step for
# the cloud models (the local model decides, the cloud model writes the reply).
LOCAL_TASK_DECIDE = "decide"


@dataclass(frozen=True)
class ModelSet:
    """Which model does what (MODEL_ROUTE): replies when called, proactive turns, memory work."""

    chat: AbstractSummarizer
    proactive: AbstractSummarizer
    memory: AbstractSummarizer


def create_local_summarizer(config) -> OpenAISummarizer:
    """The model on your own machine (Ollama, LM Studio, llama.cpp, vLLM ...), OpenAI-compatible."""
    logger.info("Creating local model (model=%s, base_url=%s)", config.local_model, config.local_base_url)
    summarizer = OpenAISummarizer(
        api_key=getattr(config, "local_api_key", "") or "local",
        model=config.local_model,
        base_url=config.local_base_url,
        chunk_size=config.chunk_size,
    )
    summarizer.extra_body = dict(getattr(config, "local_extra_body", None) or {})
    summarizer.strip_reasoning = True
    summarizer.token_floor = int(getattr(config, "local_max_tokens", 2048))
    summarizer.timeout_floor = float(getattr(config, "local_timeout_sec", 90))
    summarizer.prompt_profile = getattr(config, "local_prompt", "standard")
    summarizer.is_local = True
    _configure_chat_persona(summarizer, config)
    return summarizer


def model_label(config) -> str:
    """What answers, for the log and the console: the cloud backend, the local
    model, or both (MODEL_ROUTE).  AI_BACKEND alone would name a cloud service
    that a local setup never calls."""
    if relay_only(config):
        return "不使用（只做中转）"
    route = getattr(config, "model_route", "cloud")
    local = getattr(config, "local_model", "") or "?"
    if route == "local":
        return f"本地 {local}"
    if route == "split":
        return f"{getattr(config, 'ai_backend', '?')} + 本地 {local}"
    return str(getattr(config, "ai_backend", ""))


def create_models(config) -> ModelSet:
    """The models for MODEL_ROUTE: cloud (as before), local, or split by LOCAL_TASKS.

    With split, LOCAL_TASKS=decide hands the "should I speak?" step of the
    cloud models to the local model.  CLOUD_FALLBACK=local makes the cloud
    model's chat calls fall back to the local model when they fail.
    """
    if relay_only(config):
        none = NoModel()
        logger.info("Model route: none (CLAUDE_RELAY_ONLY: the bot only relays, no AI model)")
        return ModelSet(none, none, none)
    route = getattr(config, "model_route", "cloud")
    if route == "local":
        local = create_local_summarizer(config)
        logger.info("Model route: local (everything on %s)", local.model)
        return ModelSet(local, local, local)
    cloud = create_summarizer(config)
    fallback = getattr(config, "cloud_fallback", "off") == "local"
    if route != "split":
        if fallback:
            install_fallback(cloud, create_local_summarizer(config))
            logger.info("Cloud fallback: the local model answers when the cloud model fails")
        return ModelSet(cloud, cloud, cloud)
    local = create_local_summarizer(config)
    tasks = set(getattr(config, "local_tasks", None) or ())
    models = ModelSet(*(local if task in tasks else cloud for task in LOCAL_TASK_GROUPS))
    if LOCAL_TASK_DECIDE in tasks:
        cloud.decide_model = local
    if fallback:
        install_fallback(cloud, local)
    logger.info("Model route: split (chat=%s, proactive=%s, memory=%s, decide=%s, fallback=%s)",
                *("local" if task in tasks else "cloud" for task in LOCAL_TASK_GROUPS),
                "local" if LOCAL_TASK_DECIDE in tasks else "as the reply",
                "on" if fallback else "off")
    return models


def create_summarizer(config) -> AbstractSummarizer:
    """Create the appropriate summarizer based on config.ai_backend.

    Args:
        config: BotConfig instance with ai_backend, api keys, model, etc.

    Returns:
        An AbstractSummarizer implementation (Claude or DeepSeek).

    Raises:
        ValueError: If the configured backend is unknown.
    """
    backend = config.ai_backend.lower()

    if backend == "deepseek":
        logger.info("Creating DeepSeekSummarizer (model=%s)", config.deepseek_model)
        summarizer = DeepSeekSummarizer(
            api_key=config.deepseek_api_key,
            model=config.deepseek_model,
            base_url=config.deepseek_base_url,
            chunk_size=config.chunk_size,
        )
        _configure_chat_persona(summarizer, config)
        return summarizer

    elif backend == "openai":
        logger.info("Creating OpenAISummarizer (model=%s)", config.openai_model)
        summarizer = OpenAISummarizer(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=config.openai_base_url,
            chunk_size=config.chunk_size,
        )
        _configure_chat_persona(summarizer, config)
        return summarizer

    elif backend == "claude":
        logger.info("Creating ClaudeSummarizer (model=%s)", config.summarize_model)
        summarizer = ClaudeSummarizer(
            api_key=config.anthropic_api_key,
            model=config.summarize_model,
            base_url=config.anthropic_base_url,
            chunk_size=config.chunk_size,
            chat_model=config.anthropic_chat_model,
        )
        _configure_chat_persona(summarizer, config)
        return summarizer

    else:
        raise ValueError(
            f"Unknown AI_BACKEND: '{config.ai_backend}'. "
            f"Supported: 'claude', 'deepseek', 'openai'."
        )
