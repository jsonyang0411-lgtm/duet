"""The model of a bot that only relays (``CLAUDE_RELAY=on`` + ``CLAUDE_RELAY_ONLY=on``).

Such a bot answers nothing but cc requests (the ``relay_only`` route step), so it
never asks a model anything and needs no AI key.  ``NoModel`` stands in where the
bot wires its models; every call raises, so a path that would reach it shows up
at once instead of silently calling a cloud service.
"""

from __future__ import annotations

from .base import AbstractSummarizer

NO_MODEL_ERROR = "CLAUDE_RELAY_ONLY: this bot only relays and uses no AI model"


class NoModel(AbstractSummarizer):
    model = "none"

    def _refuse(self, *args, **kwargs):
        raise RuntimeError(NO_MODEL_ERROR)

    _call_chat_api = _refuse
    _summarize_direct = _refuse
    _summarize_chunk = _refuse
    _merge_chunk_summaries = _refuse
    consolidate_memory = _refuse


def relay_only(config) -> bool:
    """The bot only relays: no model is created and no AI key is needed."""
    return (getattr(config, "claude_relay", "off") == "on"
            and getattr(config, "claude_relay_only", "off") == "on")
