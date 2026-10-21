"""Claude backend model selection (no network: the SDK client is replaced)."""

import os
import unittest
from unittest.mock import MagicMock, patch

from src.config import BotConfig, load_config
from src.summarize import create_summarizer
from src.summarize.claude_backend import ClaudeSummarizer
from src.summarize.models import SummaryResult

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-5-20250929"


def _text_response(text):
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


class ClaudeModelSelectionTests(unittest.TestCase):
    def summarizer(self, **kwargs):
        summarizer = ClaudeSummarizer(api_key="test-key", **kwargs)
        summarizer.client = MagicMock()
        summarizer.client.messages.create.return_value = _text_response("好呀")
        return summarizer

    def used_model(self, summarizer):
        return summarizer.client.messages.create.call_args.kwargs["model"]

    def test_chat_model_defaults_to_haiku_even_if_the_summary_model_is_stronger(self):
        summarizer = self.summarizer(model=SONNET)

        summarizer._call_chat_api("system", [{"role": "user", "content": "hi"}])

        self.assertEqual(self.used_model(summarizer), HAIKU)

    def test_configured_chat_model_is_used_for_chat(self):
        summarizer = self.summarizer(model=HAIKU, chat_model=SONNET)

        summarizer._call_chat_api("system", [{"role": "user", "content": "hi"}])

        self.assertEqual(self.used_model(summarizer), SONNET)

    def test_chunk_summaries_and_memory_consolidation_use_the_chat_model(self):
        summarizer = self.summarizer(model=HAIKU, chat_model=SONNET)
        messages = [{"sender_name": "阿明", "content": "你好", "timestamp": 1_700_000_000}]

        summarizer._summarize_chunk(messages, 1, 2, "小红")
        self.assertEqual(self.used_model(summarizer), SONNET)

        summarizer.client.messages.create.reset_mock()
        summarizer.consolidate_memory("旧记忆", messages)
        self.assertEqual(self.used_model(summarizer), SONNET)

    def test_final_summaries_use_the_summary_model(self):
        summarizer = self.summarizer(model=SONNET, chat_model=HAIKU)
        summarizer.client.messages.parse.return_value = MagicMock(
            parsed_output=SummaryResult(summary_text="x", topics=[], participants=[]),
        )

        summarizer._summarize_direct(
            [{"sender_name": "阿明", "content": "你好", "timestamp": 1_700_000_000}],
            "小红",
        )

        self.assertEqual(
            summarizer.client.messages.parse.call_args.kwargs["model"], SONNET,
        )

    def test_blank_chat_model_falls_back_to_haiku(self):
        self.assertEqual(self.summarizer(chat_model="").chat_model, HAIKU)

    def test_factory_passes_the_chat_model_from_the_config(self):
        config = BotConfig(
            ai_backend="claude", anthropic_api_key="test-key",
            summarize_model=SONNET, anthropic_chat_model=HAIKU,
        )
        summarizer = create_summarizer(config)
        self.assertEqual((summarizer.model, summarizer.chat_model), (SONNET, HAIKU))

        config = BotConfig(
            ai_backend="claude", anthropic_api_key="test-key",
            anthropic_chat_model=SONNET,
        )
        self.assertEqual(create_summarizer(config).chat_model, SONNET)

    def test_default_config_keeps_the_previous_models(self):
        config = BotConfig()
        self.assertEqual(config.summarize_model, HAIKU)
        self.assertEqual(config.anthropic_chat_model, HAIKU)

    def test_load_config_reads_claude_chat_model_from_the_environment(self):
        env = {"AI_BACKEND": "claude", "ANTHROPIC_API_KEY": "test-key"}
        with patch.dict(os.environ, {**env, "CLAUDE_CHAT_MODEL": SONNET}):
            self.assertEqual(load_config().anthropic_chat_model, SONNET)
        with patch.dict(os.environ, {**env, "CLAUDE_CHAT_MODEL": "  "}):
            self.assertEqual(load_config().anthropic_chat_model, HAIKU)
        environment = {k: v for k, v in os.environ.items() if k != "CLAUDE_CHAT_MODEL"}
        with patch.dict(os.environ, {**environment, **env}, clear=True):
            self.assertEqual(load_config().anthropic_chat_model, HAIKU)


if __name__ == "__main__":
    unittest.main()
