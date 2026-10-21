"""Tests for config.py — loading, validation, .env resolution, and helpers."""

import os
import sys
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Test that the config module can be imported even WITHOUT a .env file.
# This guards against the NameError bug where _search_locations was
# referenced before assignment during module-level logging.
# ---------------------------------------------------------------------------


class ConfigModuleImportTests(unittest.TestCase):
    """Smoke tests for module-level import behaviour."""

    def test_import_config_module_without_env_file(self):
        """Importing src.config should succeed even when no .env file exists.

        The module-level code calls find_env_file() and load_dotenv().
        When .env is missing, load_dotenv() is a no-op and the module
        must still load without NameError or AttributeError.
        """
        # The module was already imported by the test runner, so if we got
        # here without an ImportError the scenario is already verified.
        # We still explicitly import to make the test self-documenting.
        import src.config

        self.assertIsNotNone(src.config)
        self.assertTrue(hasattr(src.config, "load_config"))
        self.assertTrue(hasattr(src.config, "BotConfig"))

    def test_config_constants_are_set(self):
        """PROJECT_ROOT must be a Path and the BotConfig class must exist."""
        from src.config import PROJECT_ROOT, BotConfig

        self.assertIsInstance(PROJECT_ROOT, Path)
        self.assertTrue(PROJECT_ROOT.exists())
        self.assertTrue(hasattr(BotConfig, "ai_backend"))


# ---------------------------------------------------------------------------
# _sanitize_display_name
# ---------------------------------------------------------------------------


class SanitizeDisplayNameTests(unittest.TestCase):
    """Edge-case tests for _sanitize_display_name."""

    def test_empty_string_returns_fallback(self):
        """Empty string → fallback display name."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("")
        self.assertEqual(result, "群聊小助手")

    def test_none_returns_fallback(self):
        """None → fallback display name."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name(None)
        self.assertEqual(result, "群聊小助手")

    def test_normal_name_passes_through(self):
        """Ordinary name should be returned unchanged."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("小明")
        self.assertEqual(result, "小明")

    def test_control_characters_are_stripped(self):
        """CR, LF, and other control characters must be removed."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("小\x00明\r\n测试")
        self.assertEqual(result, "小明 测试")

    def test_whitespace_collapsed_and_trimmed(self):
        """Multiple spaces → single space; leading/trailing stripped."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("   小明    测试   ")
        self.assertEqual(result, "小明 测试")

    def test_excessive_length_truncated_to_128(self):
        """Names longer than 128 chars must be truncated."""
        from src.config import _sanitize_display_name

        long_name = "A" * 200
        result = _sanitize_display_name(long_name)
        self.assertEqual(len(result), 128)
        self.assertTrue(result.startswith("A" * 128))

    def test_only_whitespace_returns_fallback(self):
        """A name consisting only of whitespace → fallback."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("   \t  \n  ")
        self.assertEqual(result, "群聊小助手")

    def test_only_control_chars_returns_fallback(self):
        """A name consisting only of stripped control chars → fallback."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("\x00\x01\x02\x1f")
        self.assertEqual(result, "群聊小助手")

    def test_exactly_128_chars_kept_intact(self):
        """A name of exactly 128 chars should not be truncated."""
        from src.config import _sanitize_display_name

        name_128 = "A" * 128
        result = _sanitize_display_name(name_128)
        self.assertEqual(len(result), 128)
        self.assertEqual(result, name_128)

    def test_tabs_converted_to_space(self):
        """Tabs should be collapsed to a single space."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("hello\tworld")
        self.assertEqual(result, "hello world")

    def test_delete_character_stripped(self):
        """DEL character (0x7f) must be removed."""
        from src.config import _sanitize_display_name

        result = _sanitize_display_name("hello\x7fworld")
        self.assertEqual(result, "helloworld")


# ---------------------------------------------------------------------------
# _decode_wechat_groups
# ---------------------------------------------------------------------------


class DecodeWeChatGroupsTests(unittest.TestCase):
    """Tests for _decode_wechat_groups URL-decoding helper."""

    def test_wildcard_passes_through(self):
        """'*' should be returned as '*'."""
        from src.config import _decode_wechat_groups

        self.assertEqual(_decode_wechat_groups("*"), "*")

    def test_empty_returns_wildcard(self):
        """Empty string or None → '*'."""
        from src.config import _decode_wechat_groups

        self.assertEqual(_decode_wechat_groups(""), "*")
        self.assertEqual(_decode_wechat_groups(None), "*")

    def test_single_encoded_group_decoded(self):
        """Single URL-encoded group name should be decoded."""
        from src.config import _decode_wechat_groups

        result = _decode_wechat_groups("Hello%20World")
        self.assertEqual(result, "Hello World")

    def test_multiple_groups_comma_separated(self):
        """Comma-separated, URL-encoded groups should all be decoded."""
        from src.config import _decode_wechat_groups

        result = _decode_wechat_groups("Hello%20World,%E5%B0%8F%E6%98%8E")
        self.assertEqual(result, "Hello World,小明")

    def test_unencoded_plain_text_preserved(self):
        """Plain (unencoded) text should pass through unchanged."""
        from src.config import _decode_wechat_groups

        result = _decode_wechat_groups("Group1,Group2")
        self.assertEqual(result, "Group1,Group2")

    def test_whitespace_stripped_around_groups(self):
        """Whitespace around each group name is stripped."""
        from src.config import _decode_wechat_groups

        result = _decode_wechat_groups("  Group1  ,  Group2  ")
        self.assertEqual(result, "Group1,Group2")


# ---------------------------------------------------------------------------
# find_env_file
# ---------------------------------------------------------------------------


class FindEnvFileTests(unittest.TestCase):
    """Tests for find_env_file() behaviour."""

    def test_returns_path_when_env_exists_in_project_root(self):
        """Should return PROJECT_ROOT / '.env' if it exists."""
        from src.config import find_env_file, PROJECT_ROOT

        with patch.object(Path, "exists", return_value=True):
            result = find_env_file()
            self.assertIsNotNone(result)
            self.assertEqual(result, PROJECT_ROOT / ".env")

    def test_returns_none_when_no_env_anywhere(self):
        """Should return None when .env does not exist at any location."""
        from src.config import find_env_file

        with patch.object(Path, "exists", return_value=False):
            result = find_env_file()
            self.assertIsNone(result)

    def test_frozen_includes_exe_dir_in_search(self):
        """In frozen mode, EXE directory is included as a search location."""
        from src.config import find_env_file, PROJECT_ROOT

        exe_dir = Path(sys.executable).resolve().parent
        exe_env = exe_dir / ".env"

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(Path, "exists", return_value=True),
        ):
            result = find_env_file()
            # When all locations exist, EXE dir is checked first (inserted at
            # position 0) and returned.
            self.assertIsNotNone(result)
            self.assertEqual(result, exe_env)

    def test_frozen_returns_none_when_no_env_anywhere(self):
        """Frozen mode with no .env anywhere → None."""
        from src.config import find_env_file

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(Path, "exists", return_value=False),
        ):
            result = find_env_file()
            self.assertIsNone(result)


class FindLocalEnvFileTests(unittest.TestCase):
    """Tests for machine-local env override discovery."""

    def test_explicit_local_env_file_wins(self):
        """WEBOT_LOCAL_ENV_FILE should be used when it exists."""
        from src.config import find_local_env_file

        with NamedTemporaryFile(delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with patch.dict(os.environ, {
                "WEBOT_LOCAL_ENV_FILE": str(path),
            }, clear=False):
                self.assertEqual(find_local_env_file(), path)
        finally:
            path.unlink(missing_ok=True)

    def test_returns_none_when_no_local_env_anywhere(self):
        """No local override file -> None."""
        from src.config import find_local_env_file

        with (
            patch.dict(os.environ, {
                "APPDATA": "",
                "LOCALAPPDATA": "",
                "WEBOT_LOCAL_ENV_FILE": "",
            }, clear=False),
            patch.object(Path, "exists", return_value=False),
        ):
            self.assertIsNone(find_local_env_file())


# ---------------------------------------------------------------------------
# is_onboarding_done
# ---------------------------------------------------------------------------


class IsOnboardingDoneTests(unittest.TestCase):
    """Tests for is_onboarding_done() with various .env states."""

    def test_returns_false_when_no_env_file(self):
        """No .env file → not onboarded."""
        from src.config import is_onboarding_done

        with (
            patch("src.config.find_env_file", return_value=None),
            patch("src.config.find_local_env_file", return_value=None),
        ):
            self.assertFalse(is_onboarding_done())

    def test_returns_false_when_env_has_no_onboarding_key(self):
        """Env exists but no ONBOARDING_DONE key → False."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_env_content = "AI_BACKEND=deepseek\nDEEPSEEK_API_KEY=sk-xxx\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_env_content),
        ):
            self.assertFalse(is_onboarding_done())

    def test_returns_true_when_onboarding_done_is_true(self):
        """ONBOARDING_DONE=true → True."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_env_content = (
            "AI_BACKEND=deepseek\n"
            "ONBOARDING_DONE=true\n"
        )
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_env_content),
        ):
            self.assertTrue(is_onboarding_done())

    def test_returns_false_when_onboarding_done_is_false(self):
        """ONBOARDING_DONE=false → False."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_env_content = "ONBOARDING_DONE=false\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_env_content),
        ):
            self.assertFalse(is_onboarding_done())

    def test_returns_true_when_wcdb_key_exists(self):
        """A real WCDB_KEY should skip repeated key extraction."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_env_content = "ONBOARDING_DONE=false\nWCDB_KEY=" + ("a" * 64) + "\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch("src.config.find_local_env_file", return_value=None),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_env_content),
        ):
            self.assertTrue(is_onboarding_done())

    def test_local_onboarding_done_override_is_used(self):
        """A VM-local ONBOARDING_DONE=true overrides shared false."""
        from src.config import is_onboarding_done

        shared_env = Path("/fake/.env")
        local_env = Path("/fake/.env.local")

        def read_text_side_effect(self, *args, **kwargs):
            if self == shared_env:
                return "ONBOARDING_DONE=false\n"
            if self == local_env:
                return "ONBOARDING_DONE=true\n"
            return ""

        with (
            patch("src.config.find_env_file", return_value=shared_env),
            patch("src.config.find_local_env_file", return_value=local_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", read_text_side_effect),
        ):
            self.assertTrue(is_onboarding_done())

    def test_case_insensitive_true(self):
        """ONBOARDING_DONE=TRUE (uppercase) → True."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_env_content = "ONBOARDING_DONE=TRUE\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_env_content),
        ):
            self.assertTrue(is_onboarding_done())

    def test_whitespace_around_value(self):
        """ONBOARDING_DONE=true (with spaces around value, not key) → True.

        The parser requires ONBOARDING_DONE= prefix with no spaces around
        the equals sign (standard .env format).  Trailing whitespace
        around the value after = is stripped.
        """
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_env_content = "ONBOARDING_DONE=  true  \n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_env_content),
        ):
            self.assertTrue(is_onboarding_done())

    def test_whitespace_around_equals_sign_returns_false(self):
        """ONBOARDING_DONE = true (spaces around =) → False.

        The current parser requires key=value with no spaces around =.
        This is the standard .env format; spaces around = are invalid.
        """
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_env_content = "ONBOARDING_DONE = true\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_env_content),
        ):
            self.assertFalse(is_onboarding_done())


# ---------------------------------------------------------------------------
# load_config / _validate_config
# ---------------------------------------------------------------------------


class LoadConfigTests(unittest.TestCase):
    """Tests for load_config() with mocked environment variables."""

    def test_load_config_with_minimal_valid_env(self):
        """load_config() should succeed with the minimum required env vars."""
        from src.config import load_config, BotConfig

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test-key",
        }, clear=True):
            config = load_config()
            self.assertIsInstance(config, BotConfig)
            self.assertEqual(config.ai_backend, "claude")
            self.assertEqual(config.anthropic_api_key, "sk-ant-test-key")

    def test_load_config_with_deepseek_backend(self):
        """When AI_BACKEND=deepseek, DEEPSEEK_API_KEY is required."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "AI_BACKEND": "deepseek",
            "DEEPSEEK_API_KEY": "sk-ds-test-key",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.ai_backend, "deepseek")
            self.assertEqual(config.deepseek_api_key, "sk-ds-test-key")

    def test_load_config_with_openai_backend(self):
        """When AI_BACKEND=openai, OPENAI_API_KEY is required."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "AI_BACKEND": "openai",
            "OPENAI_API_KEY": "sk-openai-test-key",
            "OPENAI_MODEL": "gpt-test",
            "OPENAI_BASE_URL": "https://proxy.example.com/v1",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.ai_backend, "openai")
            self.assertEqual(config.openai_api_key, "sk-openai-test-key")
            self.assertEqual(config.openai_model, "gpt-test")
            self.assertEqual(config.openai_base_url, "https://proxy.example.com/v1")

    def test_load_config_raises_when_api_key_missing_claude(self):
        """load_config() with default (claude) backend and no API key → RuntimeError."""
        from src.config import load_config

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_load_config_raises_when_api_key_missing_deepseek(self):
        """load_config() with deepseek backend and no API key → RuntimeError."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "AI_BACKEND": "deepseek",
        }, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))

    def test_load_config_rejects_masked_deepseek_api_key(self):
        """Masked UI placeholders are not usable DeepSeek API keys."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "AI_BACKEND": "deepseek",
            "DEEPSEEK_API_KEY": "sk-t***test",
        }, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("打码", str(ctx.exception))

    def test_load_config_rejects_masked_anthropic_api_key(self):
        """Masked UI placeholders are not usable Anthropic API keys."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "AI_BACKEND": "claude",
            "ANTHROPIC_API_KEY": "sk-a***test",
        }, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("打码", str(ctx.exception))

    def test_load_config_rejects_masked_openai_api_key(self):
        """Masked UI placeholders are not usable OpenAI API keys."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "AI_BACKEND": "openai",
            "OPENAI_API_KEY": "sk-o***test",
        }, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("打码", str(ctx.exception))

    def test_load_config_parses_trigger_keywords(self):
        """Custom TRIGGER_KEYWORDS should be parsed from comma-separated string."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "TRIGGER_KEYWORDS": "总结,回顾,summarize",
        }, clear=True):
            config = load_config()
            self.assertEqual(
                config.trigger_keywords,
                ["总结", "回顾", "summarize"],
            )

    def test_load_config_uses_defaults_for_unset_vars(self):
        """Unset environment variables should fall back to BotConfig defaults."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.poll_interval_sec, 1.0)
            self.assertEqual(config.chunk_size, 400)
            self.assertEqual(config.max_messages_for_summary, 5000)
            self.assertEqual(config.fallback_window_hours, 8)
            self.assertEqual(config.dedup_window_sec, 60)
            self.assertEqual(config.wechat_backend, "wcdb")
            # the default persona is the 杨宇靖 preset, which answers to its own name
            self.assertEqual(config.chat_persona_profile, "yang_yujing")
            self.assertEqual(config.bot_display_name, "杨宇靖")
            self.assertEqual(config.yang_yujing_group_name, "群主应该是pyc")

    def test_load_config_parses_persona_settings(self):
        """Persona env vars should be decoded and validated."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "CHAT_PERSONA_PROFILE": "custom",
            "CHAT_PERSONA_NAME": "小柔",
            "CHAT_PERSONA_PROMPT": "第一行\n第二行",
            "CHAT_REPLY_RULES": "短句\n不乱认人",
            "CHAT_MAX_CHARS": "48",
            "CHAT_CONTEXT_WINDOW_SEC": "1200",
            "CHAT_CONTEXT_COUNT": "55",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.chat_persona_name, "小柔")
            self.assertEqual(config.chat_persona_prompt, "第一行\n第二行")
            self.assertEqual(config.chat_reply_rules, "短句\n不乱认人")
            self.assertEqual(config.chat_max_chars, 48)
            self.assertEqual(config.chat_context_window_sec, 1200)
            self.assertEqual(config.chat_context_count, 55)

    def test_load_config_uses_yang_yujing_preset_and_target_group(self):
        """Yang's fixed preset overrides the custom persona's editable prompt fields."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "CHAT_PERSONA_PROFILE": "yang_yujing",
            "YANG_YUJING_GROUP_NAME": "群主应该是pyc",
            "YANG_YUJING_ALWAYS_REPLY_GROUP_NAME": "shit bro 和emperor 还有maybe哥",
            "CHAT_PERSONA_NAME": "林小满",
            "CHAT_PERSONA_PROMPT": "旧人设",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.chat_persona_profile, "yang_yujing")
            self.assertEqual(config.chat_persona_name, "杨宇靖")
            self.assertIn("游戏", config.chat_persona_prompt)
            self.assertIn("2026", config.chat_persona_prompt)
            self.assertIn("2 到 8 个字", config.chat_persona_prompt)
            self.assertIn("次元和 cy 是同一个人", config.chat_persona_prompt)
            self.assertIn("cy 是南通人", config.chat_persona_prompt)
            self.assertEqual(config.bot_display_name, "杨宇靖")
            self.assertEqual(config.chat_max_chars, 30)
            self.assertEqual(config.chat_context_count, 14)
            self.assertEqual(config.yang_yujing_group_name, "群主应该是pyc")
            self.assertEqual(
                config.yang_yujing_always_reply_group_name,
                "shit bro 和emperor 还有maybe哥",
            )

    def test_load_config_rejects_unknown_persona_profile(self):
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "CHAT_PERSONA_PROFILE": "unknown_persona",
        }, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("CHAT_PERSONA_PROFILE", str(ctx.exception))

    def test_load_config_with_custom_numeric_values(self):
        """Custom numeric env vars should be parsed correctly."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "POLL_INTERVAL_SEC": "2.5",
            "CHUNK_SIZE": "500",
            "MAX_MESSAGES_FOR_SUMMARY": "2000",
            "FALLBACK_WINDOW_HOURS": "12",
            "DEDUP_WINDOW_SEC": "120",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.poll_interval_sec, 2.5)
            self.assertEqual(config.chunk_size, 500)
            self.assertEqual(config.max_messages_for_summary, 2000)
            self.assertEqual(config.fallback_window_hours, 12)
            self.assertEqual(config.dedup_window_sec, 120)

    def test_load_config_boolean_parsing(self):
        """Boolean env vars (FUN_ENABLED, PROACTIVE_ENABLED) parse correctly."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "FUN_ENABLED": "false",
            "PROACTIVE_ENABLED": "true",
            "STICKY_MENTION_ENABLED": "false",
        }, clear=True):
            config = load_config()
            self.assertFalse(config.fun_enabled)
            self.assertTrue(config.proactive_enabled)
            self.assertFalse(config.sticky_mention_enabled)

    def test_load_config_sticky_mention_defaults(self):
        """Sticky mention settings have correct defaults."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }, clear=True):
            config = load_config()
            self.assertTrue(config.sticky_mention_enabled)
            self.assertEqual(config.sticky_mention_ttl_sec, 60)

    def test_load_config_message_burst_defaults(self):
        """Message burst coalescing has conversation-friendly defaults."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }, clear=True):
            config = load_config()
            self.assertTrue(config.message_burst_enabled)
            self.assertEqual(config.message_burst_window_sec, 2.2)
            self.assertEqual(config.message_burst_max_messages, 5)
            self.assertEqual(config.message_burst_max_chars, 200)

    def test_load_config_message_burst_env_override(self):
        """Message burst settings can be tuned from env."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "MESSAGE_BURST_ENABLED": "false",
            "MESSAGE_BURST_WINDOW_SEC": "3.5",
            "MESSAGE_BURST_MAX_MESSAGES": "4",
            "MESSAGE_BURST_MAX_CHARS": "120",
        }, clear=True):
            config = load_config()
            self.assertFalse(config.message_burst_enabled)
            self.assertEqual(config.message_burst_window_sec, 3.5)
            self.assertEqual(config.message_burst_max_messages, 4)
            self.assertEqual(config.message_burst_max_chars, 120)

    def test_load_config_web_search_defaults(self):
        """Web search has safe defaults for triggered current-info requests."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }, clear=True):
            config = load_config()
            self.assertTrue(config.web_search_enabled)
            self.assertEqual(config.web_search_max_results, 4)
            self.assertEqual(config.web_search_timeout_sec, 8.0)
            self.assertEqual(config.web_search_region, "cn-zh")
            self.assertEqual(config.web_search_reply_max_chars, 120)

    def test_load_config_web_search_env_override(self):
        """Web search settings can be tuned from env."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "WEB_SEARCH_ENABLED": "false",
            "WEB_SEARCH_MAX_RESULTS": "2",
            "WEB_SEARCH_TIMEOUT_SEC": "5.5",
            "WEB_SEARCH_REGION": "us-en",
            "WEB_SEARCH_REPLY_MAX_CHARS": "180",
        }, clear=True):
            config = load_config()
            self.assertFalse(config.web_search_enabled)
            self.assertEqual(config.web_search_max_results, 2)
            self.assertEqual(config.web_search_timeout_sec, 5.5)
            self.assertEqual(config.web_search_region, "us-en")
            self.assertEqual(config.web_search_reply_max_chars, 180)

    def test_load_config_deepseek_model_override(self):
        """DEEPSEEK_MODEL can override the default model."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "AI_BACKEND": "deepseek",
            "DEEPSEEK_API_KEY": "sk-ds-test",
            "DEEPSEEK_MODEL": "deepseek-v4-ultra",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.deepseek_model, "deepseek-v4-ultra")


class ValidateConfigTests(unittest.TestCase):
    """Tests for _validate_config() numeric range validation."""

    def test_poll_interval_sec_below_minimum_raises(self):
        """poll_interval_sec < 0.1 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"poll_interval_sec": 0.05})
        self.assertIn("POLL_INTERVAL_SEC", str(ctx.exception))

    def test_poll_interval_sec_at_boundary_passes(self):
        """poll_interval_sec == 0.1 should pass validation."""
        from src.config import _validate_config

        # Should not raise
        _validate_config({"poll_interval_sec": 0.1})

    def test_chunk_size_below_minimum_raises(self):
        """chunk_size < 10 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"chunk_size": 5})
        self.assertIn("CHUNK_SIZE", str(ctx.exception))

    def test_chunk_size_above_maximum_raises(self):
        """chunk_size > 1000 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"chunk_size": 2000})
        self.assertIn("CHUNK_SIZE", str(ctx.exception))

    def test_chunk_size_at_boundaries_passes(self):
        """chunk_size at 10 or 1000 should pass."""
        from src.config import _validate_config

        _validate_config({"chunk_size": 10})
        _validate_config({"chunk_size": 1000})

    def test_max_messages_for_summary_below_minimum_raises(self):
        """max_messages_for_summary < 10 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError):
            _validate_config({"max_messages_for_summary": 5})

    def test_fallback_window_hours_below_one_raises(self):
        """fallback_window_hours < 1 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError):
            _validate_config({"fallback_window_hours": 0})

    def test_dedup_window_sec_below_minimum_raises(self):
        """dedup_window_sec < 10 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError):
            _validate_config({"dedup_window_sec": 5})

    def test_sticky_mention_ttl_sec_below_minimum_raises(self):
        """sticky_mention_ttl_sec < 10 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"sticky_mention_ttl_sec": 5})
        self.assertIn("STICKY_MENTION_TTL_SEC", str(ctx.exception))

    def test_sticky_mention_ttl_sec_above_maximum_raises(self):
        """sticky_mention_ttl_sec > 300 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"sticky_mention_ttl_sec": 500})
        self.assertIn("STICKY_MENTION_TTL_SEC", str(ctx.exception))

    def test_message_burst_window_sec_out_of_range_raises(self):
        """message_burst_window_sec should stay within usable latency."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"message_burst_window_sec": 0.05})
        self.assertIn("MESSAGE_BURST_WINDOW_SEC", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"message_burst_window_sec": 9.0})
        self.assertIn("MESSAGE_BURST_WINDOW_SEC", str(ctx.exception))

    def test_message_burst_limits_out_of_range_raise(self):
        """message_burst limits should avoid huge merged turns."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"message_burst_max_messages": 99})
        self.assertIn("MESSAGE_BURST_MAX_MESSAGES", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"message_burst_max_chars": 10})
        self.assertIn("MESSAGE_BURST_MAX_CHARS", str(ctx.exception))

    def test_web_search_limits_out_of_range_raise(self):
        """web_search limits should avoid slow or oversized searches."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"web_search_max_results": 99})
        self.assertIn("WEB_SEARCH_MAX_RESULTS", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"web_search_timeout_sec": 0.5})
        self.assertIn("WEB_SEARCH_TIMEOUT_SEC", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"web_search_reply_max_chars": 10})
        self.assertIn("WEB_SEARCH_REPLY_MAX_CHARS", str(ctx.exception))

    def test_proactive_rate_window_sec_below_minimum_raises(self):
        """proactive_rate_window_sec < 30 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError):
            _validate_config({"proactive_rate_window_sec": 10})

    def test_proactive_rates_not_strictly_ascending_raises(self):
        """Proactive rates must be quiet < casual < lively < burst."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({
                "proactive_rate_quiet": 5.0,
                "proactive_rate_casual": 4.0,
                "proactive_rate_lively": 6.5,
                "proactive_rate_burst": 8.5,
            })
        self.assertIn("strict ascending", str(ctx.exception))

    def test_proactive_rates_with_zero_or_negative_raises(self):
        """Proactive rates must all be > 0."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({
                "proactive_rate_quiet": 0,
                "proactive_rate_casual": 4.0,
                "proactive_rate_lively": 6.5,
                "proactive_rate_burst": 8.5,
            })
        self.assertIn("must be > 0", str(ctx.exception))

    def test_proactive_rates_valid_order_passes(self):
        """Valid ascending proactive rates should pass."""
        from src.config import _validate_config

        _validate_config({
            "proactive_rate_quiet": 1.5,
            "proactive_rate_casual": 4.0,
            "proactive_rate_lively": 6.5,
            "proactive_rate_burst": 8.5,
        })

    def test_max_retries_below_minimum_raises(self):
        """max_retries < 1 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError):
            _validate_config({"max_retries": 0})

    def test_max_retries_above_maximum_raises(self):
        """max_retries > 10 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError):
            _validate_config({"max_retries": 11})

    def test_max_retries_valid_passes(self):
        """max_retries between 1-10 should pass."""
        from src.config import _validate_config

        _validate_config({"max_retries": 5})

    def test_multiple_errors_reported_together(self):
        """Multiple invalid values should all appear in error message."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({
                "poll_interval_sec": 0.01,
                "chunk_size": 5,
                "dedup_window_sec": 3,
            })
        msg = str(ctx.exception)
        self.assertIn("POLL_INTERVAL_SEC", msg)
        self.assertIn("CHUNK_SIZE", msg)
        self.assertIn("DEDUP_WINDOW_SEC", msg)

    def test_empty_kwargs_passes_all_defaults(self):
        """No kwargs → all defaults pass validation."""
        from src.config import _validate_config

        _validate_config({})


# ---------------------------------------------------------------------------
# load_config integration: triggers full _validate_config flow
# ---------------------------------------------------------------------------


class LoadConfigValidationIntegrationTests(unittest.TestCase):
    """load_config() → _validate_config() integration."""

    def test_invalid_chunk_size_in_env_raises(self):
        """Setting CHUNK_SIZE=5 via env should trigger validation error."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "CHUNK_SIZE": "5",
        }, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("CHUNK_SIZE", str(ctx.exception))

    def test_invalid_sticky_mention_ttl_in_env_raises(self):
        """Setting STICKY_MENTION_TTL_SEC=999 via env should fail."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "STICKY_MENTION_TTL_SEC": "999",
        }, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("STICKY_MENTION_TTL_SEC", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
