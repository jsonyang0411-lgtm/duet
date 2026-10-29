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
        self.assertTrue(hasattr(BotConfig, "wechat_backend"))


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

    def test_load_config_needs_no_setting_at_all(self):
        """There is no model: an empty environment is a valid configuration."""
        from src.config import load_config, BotConfig

        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertIsInstance(config, BotConfig)
        self.assertEqual(config.wechat_backend, "wcdb")
        self.assertEqual(config.wechat_groups, "*")
        self.assertFalse(config.private_chat_enabled)
        self.assertEqual(config.bot_display_name, "群聊小助手")
        self.assertEqual(config.poll_interval_sec, 1.0)
        self.assertEqual(config.dedup_window_sec, 60)
        self.assertEqual((config.claude_relay, config.prm_copilot, config.prm_edge), ("off", "off", "off"))

    def test_old_chat_settings_are_ignored(self):
        """An .env of the chat bot (AI keys, persona, proactive, ...) still loads; nothing of it is read."""
        from src.config import load_config

        env = {"AI_BACKEND": "nonsense", "CHAT_PERSONA_PROFILE": "nonsense", "CHUNK_SIZE": "5",
               "STICKY_MENTION_TTL_SEC": "999", "PIPELINE_V2": "maybe", "MODEL_ROUTE": "x"}
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        for name in ("ai_backend", "chat_persona_profile", "chunk_size", "sticky_mention_ttl_sec",
                     "pipeline_v2", "model_route", "anthropic_api_key", "deepseek_api_key"):
            self.assertFalse(hasattr(config, name), name)

    def test_load_config_reads_the_chats(self):
        """Groups and private chats are URL-encoded names; private chats are off unless asked for."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "WECHAT_GROUPS": "%E6%B5%8B%E8%AF%95%2C%E7%BE%A4,dev",
            "PRIVATE_CHAT_ENABLED": "true",
            "PRIVATE_CHATS": "wxid_a",
            "BOT_DISPLAY_NAME": "  商务\n号  ",
            "ADMIN_WXID": " wxid_admin ",
        }, clear=True):
            config = load_config()
        self.assertEqual(config.wechat_groups, "测试,群,dev")
        self.assertTrue(config.private_chat_enabled)
        self.assertEqual(config.private_chats, "wxid_a")
        self.assertEqual(config.bot_display_name, "商务 号")
        self.assertEqual(config.admin_wxid, "wxid_admin")

    def test_load_config_with_custom_numeric_values(self):
        """Custom numeric env vars should be parsed correctly."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "POLL_INTERVAL_SEC": "2.5",
            "DEDUP_WINDOW_SEC": "120",
        }, clear=True):
            config = load_config()
            self.assertEqual(config.poll_interval_sec, 2.5)
            self.assertEqual(config.dedup_window_sec, 120)

    def test_load_config_message_burst_defaults(self):
        """Message burst coalescing has conversation-friendly defaults."""
        from src.config import load_config

        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
            self.assertTrue(config.message_burst_enabled)
            self.assertEqual(config.message_burst_window_sec, 2.2)
            self.assertEqual(config.message_burst_max_messages, 5)
            self.assertEqual(config.message_burst_max_chars, 200)

    def test_load_config_message_burst_env_override(self):
        """Message burst settings can be tuned from env."""
        from src.config import load_config

        with patch.dict(os.environ, {
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

    def test_load_config_sending_switches(self):
        """The send switches accept the usual on/off spellings."""
        from src.config import load_config

        with patch.dict(os.environ, {
            "SEND_TYPING_DELAY": "true", "SEND_QUIET": "yes", "SEND_QUEUE": "1", "SEND_KEEP_CLIPBOARD": "off",
            "STICKER_PASTE": "IMAGE",
        }, clear=True):
            config = load_config()
        self.assertEqual((config.send_typing_delay, config.send_quiet, config.send_queue), ("on", "on", "on"))
        self.assertEqual((config.send_keep_clipboard, config.sticker_paste), ("off", "image"))

    def test_load_config_night_window(self):
        """下班时段自动回复: off unless set; the window and the night's holding line are read and stripped."""
        from src.config import load_config

        night_text = "收到～您说的我都记下啦，明早第一时间回复您哈～"
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual((config.prm_night_auto_hours, config.prm_night_holding_text), ("", night_text))
        with patch.dict(os.environ, {"PRM_NIGHT_AUTO_HOURS": " 18:00-09:00 ", "PRM_NIGHT_HOLDING_TEXT": " 收到啦～明早回您 "},
                        clear=True):
            config = load_config()
        self.assertEqual((config.prm_night_auto_hours, config.prm_night_holding_text), ("18:00-09:00", "收到啦～明早回您"))
        with patch.dict(os.environ, {"PRM_NIGHT_HOLDING_TEXT": "  "}, clear=True):
            self.assertEqual(load_config().prm_night_holding_text, night_text)

    def test_load_config_auto_register(self):
        """PRM_AUTO_REGISTER: off unless set, the usual on/off spellings; on without PRM_COPILOT only does nothing
        (the bot still starts: turning the copilot off in a hurry must not stop it)."""
        from src.config import load_config

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config().prm_auto_register, "off")
        for raw, value in (("on", "on"), (" yes ", "on"), ("TRUE", "on"), ("1", "on"), ("off", "off"), ("0", "off"),
                           ("", "off")):
            with patch.dict(os.environ, {"PRM_AUTO_REGISTER": raw}, clear=True):
                self.assertEqual(load_config().prm_auto_register, value, raw)


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

    def test_dedup_window_sec_below_minimum_raises(self):
        """dedup_window_sec < 10 → RuntimeError."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError):
            _validate_config({"dedup_window_sec": 5})

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

    def test_picture_lookup_limits(self):
        """The relay's picture lookup: how far back, how large."""
        from src.config import _validate_config

        for bad, key in (({"image_vision_lookback_sec": 10}, "IMAGE_VISION_LOOKBACK_SEC"),
                         ({"image_vision_max_bytes": 100}, "IMAGE_VISION_MAX_BYTES")):
            with self.assertRaises(RuntimeError) as ctx:
                _validate_config(bad)
            self.assertIn(key, str(ctx.exception))

    def test_multiple_errors_reported_together(self):
        """Multiple invalid values should all appear in error message."""
        from src.config import _validate_config

        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({
                "poll_interval_sec": 0.01,
                "message_burst_max_chars": 5,
                "dedup_window_sec": 3,
            })
        msg = str(ctx.exception)
        self.assertIn("POLL_INTERVAL_SEC", msg)
        self.assertIn("MESSAGE_BURST_MAX_CHARS", msg)
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

    def test_invalid_poll_interval_in_env_raises(self):
        """Setting POLL_INTERVAL_SEC=0.01 via env should trigger validation error."""
        from src.config import load_config

        with patch.dict(os.environ, {"POLL_INTERVAL_SEC": "0.01"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("POLL_INTERVAL_SEC", str(ctx.exception))

    def test_invalid_message_burst_window_in_env_raises(self):
        """Setting MESSAGE_BURST_WINDOW_SEC=99 via env should fail."""
        from src.config import load_config

        with patch.dict(os.environ, {"MESSAGE_BURST_WINDOW_SEC": "99"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_config()
            self.assertIn("MESSAGE_BURST_WINDOW_SEC", str(ctx.exception))

    def test_a_night_window_that_is_none_stops_the_start(self):
        """PRM_NIGHT_AUTO_HOURS that is not HH:MM-HH:MM: the bot refuses to start (it never guesses an hour)."""
        from src.config import load_config

        for value in ("18-9", "18:00-18:00", "晚上"):
            with patch.dict(os.environ, {"PRM_NIGHT_AUTO_HOURS": value}, clear=True):
                with self.assertRaises(RuntimeError) as ctx:
                    load_config()
                self.assertIn("PRM_NIGHT_AUTO_HOURS", str(ctx.exception))

    def test_an_auto_register_that_is_no_switch_stops_the_start(self):
        """PRM_AUTO_REGISTER is on or off; anything else is refused (never guessed)."""
        from src.config import load_config

        for value in ("always", "全部", "2"):
            with patch.dict(os.environ, {"PRM_AUTO_REGISTER": value}, clear=True):
                with self.assertRaises(RuntimeError) as ctx:
                    load_config()
                self.assertIn("PRM_AUTO_REGISTER must be off or on", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
