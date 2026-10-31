"""
Tests for webot: config loading and web API endpoints.

Uses Python's built-in unittest framework with unittest.mock for mocking.
No real DB or network -- all tests are fast and deterministic.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch


# ======================================================================
#  TestConfig -- configuration loading, validation, helpers
# ======================================================================


class TestConfig(unittest.TestCase):
    """Tests for src.config: load_config, _validate_config, find_env_file,
    is_onboarding_done, _sanitize_display_name, _decode_wechat_groups."""

    # -- import-time resilience ----------------------------------------

    def test_config_module_can_be_imported(self):
        """Importing config module should not raise NameError even when no
        .env file exists (regression test for dotenv fallback bug)."""
        try:
            from src import config as cfg
            self.assertTrue(hasattr(cfg, "load_config"))
            self.assertTrue(hasattr(cfg, "BotConfig"))
            self.assertTrue(hasattr(cfg, "find_env_file"))
            self.assertTrue(hasattr(cfg, "is_onboarding_done"))
        except NameError as e:
            self.fail(f"Import of src.config raised NameError: {e}")

    # -- load_config ---------------------------------------------------

    def test_load_config_defaults_when_vars_missing(self):
        """load_config should apply dataclass defaults for optional env vars (no key is needed)."""
        from src.config import load_config
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
            self.assertEqual(cfg.poll_interval_sec, 1.0)
            self.assertEqual(cfg.dedup_window_sec, 60)
            self.assertEqual(cfg.log_level, "INFO")
            self.assertEqual(cfg.wechat_backend, "wcdb")

    # -- _validate_config ----------------------------------------------

    def _call_validate(self, **overrides):
        from src.config import _validate_config
        _validate_config(dict(overrides))

    def test_validate_config_poll_interval_too_low(self):
        """POLL_INTERVAL_SEC < 0.1 must raise RuntimeError."""
        with self.assertRaises(RuntimeError) as ctx:
            self._call_validate(poll_interval_sec=0.05)
        self.assertIn("POLL_INTERVAL_SEC", str(ctx.exception))

    def test_validate_config_dedup_window_too_low(self):
        """DEDUP_WINDOW_SEC < 10 must raise RuntimeError."""
        with self.assertRaises(RuntimeError) as ctx:
            self._call_validate(dedup_window_sec=5)
        self.assertIn("DEDUP_WINDOW_SEC", str(ctx.exception))

    def test_validate_config_multiple_errors_reported(self):
        """Multiple invalid values should all appear in the error message."""
        with self.assertRaises(RuntimeError) as ctx:
            self._call_validate(dedup_window_sec=0, poll_interval_sec=0)
        msg = str(ctx.exception)
        self.assertIn("DEDUP_WINDOW_SEC", msg)
        self.assertIn("POLL_INTERVAL_SEC", msg)

    # -- find_env_file ------------------------------------------------

    def test_find_env_file_found_in_project_root(self):
        """find_env_file should return project-root .env if it exists."""
        from src.config import find_env_file, PROJECT_ROOT

        with (
            patch.object(sys, "frozen", False, create=True),
            patch.object(Path, "exists", return_value=True),
        ):
            result = find_env_file()
            self.assertIsNotNone(result)
            self.assertEqual(result, PROJECT_ROOT / ".env")

    def test_find_env_file_found_in_cwd(self):
        """find_env_file returns non-None when at least one .env exists."""
        from src.config import find_env_file

        with (
            patch.object(sys, "frozen", False, create=True),
            patch.object(Path, "exists", return_value=True),
        ):
            result = find_env_file()
            self.assertIsNotNone(result)

    def test_find_env_file_not_found(self):
        """find_env_file should return None when no .env exists anywhere."""
        from src.config import find_env_file
        with (
            patch.object(sys, "frozen", False, create=True),
            patch.object(Path, "exists", return_value=False),
        ):
            self.assertIsNone(find_env_file())

    def test_find_env_file_frozen_mode(self):
        """In frozen mode, EXE directory is searched first and returned
        when .env is present there."""
        from src.config import find_env_file

        exe_dir = Path(sys.executable).resolve().parent

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(Path, "exists", return_value=True),
        ):
            result = find_env_file()
            self.assertIsNotNone(result)
            # In frozen mode, EXE dir is checked first — it wins
            self.assertEqual(result, exe_dir / ".env")

    # -- is_onboarding_done --------------------------------------------

    def test_is_onboarding_done_true(self):
        """Returns True when .env has ONBOARDING_DONE=true."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_content = "ONBOARDING_DONE=true\nWECHAT_BACKEND=wcdb\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_content),
        ):
            self.assertTrue(is_onboarding_done())

    def test_is_onboarding_done_false_explicit(self):
        """Returns False when ONBOARDING_DONE=false."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_content = "ONBOARDING_DONE=false\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_content),
        ):
            self.assertFalse(is_onboarding_done())

    def test_is_onboarding_done_no_env_file(self):
        """Returns False when no .env exists."""
        from src.config import is_onboarding_done
        with (
            patch("src.config.find_env_file", return_value=None),
            patch("src.config.find_local_env_file", return_value=None),
        ):
            self.assertFalse(is_onboarding_done())

    def test_is_onboarding_done_missing_key(self):
        """Returns False when .env exists but ONBOARDING_DONE key is missing."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_content = "AI_BACKEND=deepseek\nWECHAT_BACKEND=wcdb\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_content),
        ):
            self.assertFalse(is_onboarding_done())

    def test_is_onboarding_done_case_insensitive_value(self):
        """'TRUE' / 'True' variants pass because .lower() == 'true'."""
        from src.config import is_onboarding_done

        fake_env = Path("/fake/.env")
        fake_content = "ONBOARDING_DONE=TRUE\n"
        with (
            patch("src.config.find_env_file", return_value=fake_env),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=fake_content),
        ):
            self.assertTrue(is_onboarding_done())

    # -- _sanitize_display_name ----------------------------------------

    def test_sanitize_display_name_empty_returns_default(self):
        """Empty string returns the default display name."""
        from src.config import _sanitize_display_name
        self.assertEqual(_sanitize_display_name(""), "群聊小助手")

    def test_sanitize_display_name_none_returns_default(self):
        """None (falsy) returns the default display name."""
        from src.config import _sanitize_display_name
        self.assertEqual(_sanitize_display_name(None), "群聊小助手")

    def test_sanitize_display_name_strips_control_chars(self):
        """CR, LF, and other control chars are removed."""
        from src.config import _sanitize_display_name
        result = _sanitize_display_name("hello\nworld\r\n\x00test\x1b")
        self.assertNotIn("\n", result)
        self.assertNotIn("\r", result)
        self.assertNotIn("\x00", result)
        self.assertNotIn("\x1b", result)
        self.assertIn("hello", result)
        self.assertIn("test", result)

    def test_sanitize_display_name_too_long_truncated(self):
        """Names longer than 128 chars are truncated."""
        from src.config import _sanitize_display_name
        long_name = "A" * 200
        result = _sanitize_display_name(long_name)
        self.assertLessEqual(len(result), 128)
        self.assertEqual(result, "A" * 128)

    def test_sanitize_display_name_whitespace_only_returns_default(self):
        """Whitespace-only names fall back to default."""
        from src.config import _sanitize_display_name
        self.assertEqual(_sanitize_display_name("   \t  \n  "), "群聊小助手")

    def test_sanitize_display_name_preserves_unicode(self):
        """Chinese characters are preserved."""
        from src.config import _sanitize_display_name
        result = _sanitize_display_name("你好世界")
        self.assertEqual(result, "你好世界")

    def test_sanitize_display_name_collapses_whitespace(self):
        """Multiple spaces collapse to a single space."""
        from src.config import _sanitize_display_name
        result = _sanitize_display_name("hello   world")
        self.assertEqual(result, "hello world")

    def test_sanitize_display_name_strips_leading_trailing(self):
        """Leading and trailing whitespace are stripped."""
        from src.config import _sanitize_display_name
        result = _sanitize_display_name("  hello  ")
        self.assertEqual(result, "hello")

    def test_sanitize_display_name_exactly_128_chars_passes(self):
        """A name exactly 128 chars is not truncated."""
        from src.config import _sanitize_display_name
        name = "A" * 128
        self.assertEqual(len(_sanitize_display_name(name)), 128)

    # -- _decode_wechat_groups ----------------------------------------

    def test_decode_wechat_groups_wildcard(self):
        """'*' stays '*'."""
        from src.config import _decode_wechat_groups
        self.assertEqual(_decode_wechat_groups("*"), "*")

    def test_decode_wechat_groups_empty_returns_wildcard(self):
        """Empty string returns '*'."""
        from src.config import _decode_wechat_groups
        self.assertEqual(_decode_wechat_groups(""), "*")

    def test_decode_wechat_groups_whitespace_only_returns_wildcard(self):
        """Whitespace-only returns '*'."""
        from src.config import _decode_wechat_groups
        self.assertEqual(_decode_wechat_groups("   "), "*")

    def test_decode_wechat_groups_single_url_encoded(self):
        """URL-encoded group name is decoded."""
        from src.config import _decode_wechat_groups
        result = _decode_wechat_groups("%E6%B5%8B%E8%AF%95%E7%BE%A4")
        self.assertEqual(result, "测试群")

    def test_decode_wechat_groups_multiple_encoded(self):
        """Multiple comma-separated encoded groups all decode."""
        from src.config import _decode_wechat_groups
        result = _decode_wechat_groups(
            "%E6%B5%8B%E8%AF%95%E7%BE%A4,%E5%B7%A5%E4%BD%9C%E7%BE%A4"
        )
        self.assertEqual(result, "测试群,工作群")

    def test_decode_wechat_groups_mixed_encoded_and_plain(self):
        """Mixed URL-encoded and plain names coexist."""
        from src.config import _decode_wechat_groups
        result = _decode_wechat_groups("plain_group,%E6%B5%8B%E8%AF%95%E7%BE%A4")
        self.assertEqual(result, "plain_group,测试群")

    def test_messages_table_exists_handles_empty_runtime_db(self):
        """Nickname APIs should treat an initialized-empty sqlite file as no groups."""
        from src.web.server import _messages_table_exists

        conn = sqlite3.connect(":memory:")
        self.assertFalse(_messages_table_exists(conn))

        conn.execute("CREATE TABLE messages (chat_id TEXT, sender_id TEXT)")
        self.assertTrue(_messages_table_exists(conn))


# ======================================================================
#  TestTriggerDetector -- keyword + @mention detection
# ======================================================================


# ======================================================================
#  Web API endpoint handler tests
# ======================================================================


class MockSocket:
    """Minimal mock socket for testing _UIHandler.

    Provides enough of the socket interface for BaseHTTPRequestHandler to
    parse an HTTP request and write a response.

    IMPORTANT: BaseHTTPRequestHandler / StreamRequestHandler sets ``wfile``
    to a ``socketserver._SocketWriter`` instance (not a file object from
    ``makefile``).  _SocketWriter accumulates writes and flushes them via
    ``self._sock.sendall()``.  We therefore capture **sendall** calls to
    reconstruct the HTTP response text.
    """

    def __init__(self, request_bytes: bytes = b""):
        self._rbuf = io.BytesIO(request_bytes)
        self._wbuf = io.BytesIO()                # returned for makefile('rb') compat
        self._sent_chunks: list[bytes] = []       # captured via sendall()

    def makefile(self, mode: str, *args, **kwargs):
        if mode == "rb":
            return self._rbuf
        # _SocketWriter bypasses makefile entirely.  Return self._wbuf
        # anyway for any code path that might still use it.
        return self._wbuf

    def sendall(self, data: bytes):
        """Captured by _SocketWriter when it flushes the HTTP response."""
        self._sent_chunks.append(data)

    def get_response_text(self) -> str:
        """Reconstruct the full HTTP response text from captured chunks."""
        return b"".join(self._sent_chunks).decode("utf-8", errors="replace")


def _build_handler(path: str, method: str = "GET", body: bytes = b"",
                   headers: dict | None = None):
    """Construct a _UIHandler with a pre-built HTTP request.

    Returns (handler, sock) where ``sock`` is the MockSocket that captured
    the response.  Call ``sock.get_response_text()`` to read the HTTP
    response, then split on ``\\r\\n\\r\\n`` to separate headers and body.
    """
    from src.web.server import _UIHandler

    if headers is None:
        headers = {}
    header_lines = "\r\n".join(
        f"{k}: {v}" for k, v in headers.items()
    )
    content_length = f"Content-Length: {len(body)}\r\n" if body else ""
    # A caller-supplied Host header replaces the default one (a second Host
    # line would be ignored by the parser).
    host_line = (
        "" if any(k.lower() == "host" for k in headers) else "Host: localhost\r\n"
    )
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"{host_line}"
        f"{content_length}"
        f"{header_lines}\r\n"
        f"\r\n"
    ).encode() + body

    sock = MockSocket(raw)
    handler = _UIHandler(sock, ("127.0.0.1", 9999), MagicMock())
    return handler, sock


# ---------------------------------------------------------------------------
# _ServerStatus tests
# ---------------------------------------------------------------------------


class ServerStatusTests(unittest.TestCase):
    """Tests for _ServerStatus -- thread-safe status tracking."""

    def setUp(self):
        from src.web.server import _ServerStatus
        self.status = _ServerStatus()

    def test_snapshot_returns_all_expected_fields(self):
        """snapshot() returns all documented _FIELDS."""
        snap = self.status.snapshot()
        expected_fields = {
            "running", "uptime_sec", "messages_processed",
            "wechat_backend", "db_ok", "timestamp", "error",
        }
        self.assertEqual(set(snap.keys()), expected_fields)

    def test_default_snapshot_values(self):
        """Fresh instance has sensible defaults."""
        snap = self.status.snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["uptime_sec"], 0)
        self.assertEqual(snap["messages_processed"], 0)
        self.assertEqual(snap["wechat_backend"], "")
        self.assertFalse(snap["db_ok"])
        self.assertEqual(snap["error"], "")

    def test_update_changes_fields(self):
        """update() sets fields and updates timestamp."""
        self.status.update(
            running=True, wechat_backend="wcdb", messages_processed=42,
        )
        snap = self.status.snapshot()
        self.assertTrue(snap["running"])
        self.assertEqual(snap["wechat_backend"], "wcdb")
        self.assertEqual(snap["messages_processed"], 42)
        self.assertTrue(snap["timestamp"])
        self.assertIn("T", snap["timestamp"])

    def test_update_ignores_unknown_fields(self):
        """update() silently ignores keys that are not real attributes."""
        self.status.update(nonexistent_field="value")
        snap = self.status.snapshot()
        self.assertNotIn("nonexistent_field", snap)

    def test_snapshot_is_a_copy(self):
        """Modifying the returned snapshot dict does not affect internal state."""
        snap = self.status.snapshot()
        snap["running"] = True
        snap["error"] = "hacked"
        snap2 = self.status.snapshot()
        self.assertFalse(snap2["running"])
        self.assertEqual(snap2["error"], "")

    def test_update_preserves_unchanged_fields(self):
        """Fields not passed to update() keep their values."""
        self.status.update(running=True)
        snap = self.status.snapshot()
        self.assertTrue(snap["running"])
        self.assertEqual(snap["wechat_backend"], "")


# ---------------------------------------------------------------------------
# _BotControl tests
# ---------------------------------------------------------------------------


class BotControlTests(unittest.TestCase):
    """Tests for _BotControl -- bot lifecycle management."""

    def setUp(self):
        from src.web.server import _BotControl
        self.control = _BotControl()

    def test_initial_state_not_running(self):
        """Fresh _BotControl is not running."""
        self.assertFalse(self.control.is_running())

    def test_set_running_makes_it_running(self):
        """set_running() -> is_running() returns True."""
        self.control.set_running()
        self.assertTrue(self.control.is_running())

    def test_register_sets_running_and_thread(self):
        """register() sets thread, backend, and running flag."""
        fake_thread = MagicMock()
        fake_backend = MagicMock()
        self.control.register(thread=fake_thread, backend=fake_backend)
        self.assertTrue(self.control.is_running())
        self.assertIs(self.control.thread, fake_thread)
        self.assertIs(self.control.backend, fake_backend)

    def test_mark_stopped_resets_state(self):
        """mark_stopped() resets running, backend, and thread."""
        self.control.set_running()
        fake_backend = MagicMock()
        fake_thread = MagicMock()
        self.control.register(backend=fake_backend, thread=fake_thread)
        self.control.mark_stopped()
        self.assertFalse(self.control.is_running())
        self.assertIsNone(self.control.backend)
        self.assertIsNone(self.control.thread)

    def test_mark_stopped_does_not_call_backend_stop(self):
        """mark_stopped() just resets state -- does NOT stop backend."""
        fake_backend = MagicMock()
        self.control.register(backend=fake_backend)
        self.control.mark_stopped()
        fake_backend.stop.assert_not_called()

    def test_stop_calls_backend_stop_and_joins_thread(self):
        """stop() calls backend.stop() and thread.join()."""
        fake_backend = MagicMock()
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        self.control.register(backend=fake_backend, thread=fake_thread)
        self.control.stop()
        fake_backend.stop.assert_called_once()
        fake_thread.join.assert_called_once_with(timeout=30)
        self.assertFalse(self.control.is_running())

    def test_stop_handles_backend_without_stop_method(self):
        """stop() does not crash if backend has no stop() method."""
        self.control.stop()
        self.assertFalse(self.control.is_running())

    def test_stop_handles_none_thread(self):
        """stop() does not crash if thread is None."""
        self.control.set_running()
        self.control.stop()
        self.assertFalse(self.control.is_running())

    def test_register_backend_stores_reference(self):
        """register_backend() stores the backend without setting running."""
        fake_backend = MagicMock()
        self.control.register_backend(fake_backend)
        self.assertIs(self.control.backend, fake_backend)


# ---------------------------------------------------------------------------
# _ServerStartGuard tests
# ---------------------------------------------------------------------------


class ServerStartGuardTests(unittest.TestCase):
    """Tests for _ServerStartGuard -- idempotent server start."""

    def test_first_try_start_returns_true(self):
        """First call to try_start() returns True."""
        from src.web.server import _ServerStartGuard
        guard = _ServerStartGuard()
        self.assertTrue(guard.try_start())

    def test_second_try_start_returns_false(self):
        """Second call to try_start() returns False (already started)."""
        from src.web.server import _ServerStartGuard
        guard = _ServerStartGuard()
        guard.try_start()
        self.assertFalse(guard.try_start())

    def test_multiple_try_start_always_false_after_first(self):
        """All calls after the first return False."""
        from src.web.server import _ServerStartGuard
        guard = _ServerStartGuard()
        self.assertTrue(guard.try_start())
        for _ in range(10):
            self.assertFalse(guard.try_start())


# ---------------------------------------------------------------------------
# Public API tests
# ---------------------------------------------------------------------------


class PublicApiTests(unittest.TestCase):
    """Tests for the public API wrapper functions."""

    def test_update_status_calls_status_update(self):
        """update_status() delegates to _status.update()."""
        from src.web.server import update_status, _status
        with patch.object(_status, "update") as mock_update:
            update_status(running=True, error="test")
            mock_update.assert_called_once_with(running=True, error="test")

    def test_stop_bot_returns_false_when_not_running(self):
        """_stop_bot() handles the case where nothing is running."""
        from src.web.server import _stop_bot, _bot_control
        _bot_control.mark_stopped()
        result = _stop_bot()
        self.assertFalse(result)

    def test_start_bot_in_thread_returns_error_when_already_running(self):
        """_start_bot_in_thread() refuses if bot is already running."""
        from src.web.server import _start_bot_in_thread, _bot_control
        _bot_control.set_running()
        result = _start_bot_in_thread()
        self.assertFalse(result["ok"])
        self.assertIn("already running", result["error"])
        _bot_control.mark_stopped()  # cleanup


# ---------------------------------------------------------------------------
# /api/status endpoint tests
# ---------------------------------------------------------------------------


class ApiStatusEndpointTests(unittest.TestCase):
    """Tests for GET /api/status."""

    def test_status_returns_json_with_expected_fields(self):
        """GET /api/status returns 200 with JSON containing key fields."""
        from src.web.server import _status
        _status.update(running=False, wechat_backend="wcdb")
        handler, sock = _build_handler("/api/status")
        response = sock.get_response_text()
        self.assertIn("200", response.splitlines()[0])
        parts = response.split("\r\n\r\n", 1)
        self.assertEqual(len(parts), 2,
                         "Response should have headers and body")
        body = json.loads(parts[1])
        self.assertIn("running", body)
        self.assertIn("messages_processed", body)
        self.assertIn("wechat_backend", body)
        self.assertNotIn("ai_backend", body)
        self.assertIn("error", body)

    def test_status_when_bot_stopped(self):
        """Status reflects bot stopped state with error info."""
        from src.web.server import _status
        _status.update(running=False, error="KEY_MISSING")
        handler, sock = _build_handler("/api/status")
        parts = sock.get_response_text().split("\r\n\r\n", 1)
        body = json.loads(parts[1])
        self.assertFalse(body["running"])
        self.assertEqual(body["error"], "KEY_MISSING")

    def test_status_echoes_a_loopback_origin_and_never_uses_a_wildcard(self):
        """CORS is granted to this machine's own pages only (Phase 1)."""
        handler, sock = _build_handler(
            "/api/status", headers={"Origin": "http://localhost:5173"},
        )
        response = sock.get_response_text()
        self.assertIn("Access-Control-Allow-Origin: http://localhost:5173", response)
        self.assertIn("Vary: Origin", response)
        self.assertNotIn("Access-Control-Allow-Origin: *", response)

    def test_status_without_origin_has_no_cors_header(self):
        handler, sock = _build_handler("/api/status")
        self.assertNotIn("Access-Control-Allow-Origin", sock.get_response_text())


# ---------------------------------------------------------------------------
# /api/start and /api/stop endpoint tests
# ---------------------------------------------------------------------------


class ApiStartStopEndpointTests(unittest.TestCase):
    """Tests for /api/start and /api/stop."""

    def tearDown(self):
        from src.web.server import _bot_control
        _bot_control.mark_stopped()

    def test_start_when_already_running_returns_already_running(self):
        """POST /api/start when bot is running -> already_running=True."""
        from src.web.server import _bot_control
        _bot_control.set_running()
        handler, sock = _build_handler("/api/start", method="POST")
        parts = sock.get_response_text().split("\r\n\r\n", 1)
        body = json.loads(parts[1])
        self.assertTrue(body["ok"])
        self.assertTrue(body.get("already_running"))

    def test_start_when_not_running_starts_bot(self):
        """POST /api/start when bot is not running -> starts bot thread."""
        from src.web.server import _bot_control
        _bot_control.mark_stopped()
        with patch("src.web.server._start_bot_in_thread",
                   return_value={"ok": True}):
            handler, sock = _build_handler("/api/start", method="POST")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertTrue(body["ok"])

    def test_stop_returns_ok(self):
        """POST /api/stop returns ok=True."""
        handler, sock = _build_handler("/api/stop", method="POST")
        parts = sock.get_response_text().split("\r\n\r\n", 1)
        body = json.loads(parts[1])
        self.assertTrue(body["ok"])

    def test_stop_when_bot_not_running_returns_ok(self):
        """POST /api/stop succeeds even when nothing is running."""
        from src.web.server import _bot_control
        _bot_control.mark_stopped()
        handler, sock = _build_handler("/api/stop", method="POST")
        parts = sock.get_response_text().split("\r\n\r\n", 1)
        body = json.loads(parts[1])
        self.assertTrue(body["ok"])


# ---------------------------------------------------------------------------
# /api/config and /api/load-config endpoint tests
# ---------------------------------------------------------------------------


class ApiConfigEndpointTests(unittest.TestCase):
    """Tests for /api/config and /api/load-config."""

    def test_load_config_returns_defaults_when_no_env(self):
        """GET /api/load-config returns defaults when no .env exists."""
        with patch("src.web.server._find_or_create_env") as mock_find:
            fake_env = MagicMock()
            fake_env.exists.return_value = False
            mock_find.return_value = fake_env
            handler, sock = _build_handler("/api/load-config")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertTrue(body["ok"])
            config = body["config"]
            self.assertEqual(config["wechat_backend"], "wcdb")
            self.assertEqual(config["wechat_groups_list"], [])
            self.assertFalse(config["private_chat_enabled"])
            for gone in ("ai_backend", "deepseek_api_key", "chat_persona_profile", "fun_enabled",
                         "proactive_enabled", "trigger_keywords", "feishu_export_enabled", "todo_enabled"):
                self.assertNotIn(gone, config)

    def test_load_config_reads_env_values(self):
        """GET /api/load-config reads values from .env file."""
        with patch("src.web.server._find_or_create_env") as mock_find:
            fake_env = MagicMock()
            fake_env.exists.return_value = True
            fake_env.read_text.return_value = (
                "AI_BACKEND=claude\n"
                "BOT_DISPLAY_NAME=MyBot\n"
                "WECHAT_GROUPS=%E6%B5%8B%E8%AF%95%2C%E7%BE%A4,dev\n"
                "PRIVATE_CHAT_ENABLED=true\n"
                "MESSAGE_BURST_MAX_CHARS=120\n"
                "VOICE_OPENAI_API_KEY=sk-voice-secret-1234\n"
            )
            mock_find.return_value = fake_env
            handler, sock = _build_handler("/api/load-config")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            config = body["config"]
            self.assertEqual(config["bot_display_name"], "MyBot")
            self.assertEqual(config["wechat_groups_list"], ["测试,群", "dev"])
            self.assertTrue(config["private_chat_enabled"])
            self.assertEqual(config["message_burst_max_chars"], 120)
            self.assertEqual(config["voice_openai_api_key"], "sk-v***1234")
            self.assertNotIn("ai_backend", config)

    def test_load_config_empty_env_returns_defaults(self):
        """GET /api/load-config with empty env returns defaults."""
        with patch("src.web.server._find_or_create_env") as mock_find:
            fake_env = MagicMock()
            fake_env.exists.return_value = True
            fake_env.read_text.return_value = ""
            mock_find.return_value = fake_env
            handler, sock = _build_handler("/api/load-config")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertTrue(body["ok"])
            self.assertIsInstance(body["config"], dict)

    def test_save_config_returns_ok_and_saved_keys(self):
        """POST /api/config saves settings and returns saved keys."""
        with patch("src.web.server._find_or_create_env") as mock_find:
            fake_env = MagicMock()
            fake_env.exists.return_value = True
            fake_env.read_text.return_value = (
                "WECHAT_BACKEND=wcdb\n"
                "BOT_DISPLAY_NAME=oldname\n"
            )
            fake_env.with_suffix.return_value = fake_env
            mock_find.return_value = fake_env
            with patch("os.replace") as mock_replace:
                body = json.dumps({
                    "bot_display_name": "newname",
                    "wechat_backend": "mac_hybrid",
                })
                handler, sock = _build_handler(
                    "/api/config", method="POST",
                    body=body.encode(),
                    headers={"Content-Type": "application/json"},
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertTrue(result["ok"])
                self.assertIn("BOT_DISPLAY_NAME", result["saved"])
                self.assertIn("WECHAT_BACKEND", result["saved"])
                self.assertTrue(result["requires_restart"])

    def test_save_config_adds_new_keys(self):
        """POST /api/config adds new keys not present in existing .env."""
        with patch("src.web.server._find_or_create_env") as mock_find:
            fake_env = MagicMock()
            fake_env.exists.return_value = True
            fake_env.read_text.return_value = "AI_BACKEND=deepseek\n"
            fake_env.with_suffix.return_value = fake_env
            mock_find.return_value = fake_env
            with patch("os.replace") as mock_replace:
                body = json.dumps({"bot_display_name": "MyBot"})
                handler, sock = _build_handler(
                    "/api/config", method="POST",
                    body=body.encode(),
                    headers={"Content-Type": "application/json"},
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertTrue(result["ok"])

    def test_save_config_roundtrip(self):
        """POST /api/config saves, and load-config reads it back correctly."""
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text(
            "AI_BACKEND=deepseek\nDEEPSEEK_API_KEY=old-key\n"
            "BOT_DISPLAY_NAME=old-name\n",
            encoding="utf-8",
        )
        try:
            # -- Save --
            save_body = json.dumps({
                "deepseek_api_key": "new-key",
                "fun_enabled": True,
                "bot_display_name": "new-name",
                "private_chat_enabled": True,
                "private_chats": "wxid_a",
                "message_burst_window_sec": 3.5,
            }).encode()
            with patch("src.web.server._find_or_create_env",
                       return_value=tmp_env):
                handler, sock = _build_handler(
                    "/api/config", method="POST",
                    body=save_body,
                    headers={"Content-Type": "application/json",
                             "Content-Length": str(len(save_body))},
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertTrue(result["ok"])

            # -- Read back --
            saved_content = tmp_env.read_text(encoding="utf-8")
            self.assertIn("BOT_DISPLAY_NAME=new-name", saved_content)
            self.assertIn("PRIVATE_CHAT_ENABLED=true", saved_content)
            self.assertIn("PRIVATE_CHATS=wxid_a", saved_content)
            self.assertIn("MESSAGE_BURST_WINDOW_SEC=3.5", saved_content)
            # chat-bot keys are neither written nor changed: an old line stays as it was
            self.assertIn("DEEPSEEK_API_KEY=old-key", saved_content)
            self.assertNotIn("FUN_ENABLED", saved_content)
        finally:
            tmp_env.unlink(missing_ok=True)
            for f in tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_save_config_preserves_masked_secrets(self):
        """POST /api/config does not overwrite real secrets with UI masks."""
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text(
            "VOICE_OPENAI_API_KEY=sk-voice-real-secret\n",
            encoding="utf-8",
        )
        save_body = json.dumps({
            "voice_openai_api_key": "sk-v***cret",
            "bot_display_name": "new-name",
        }).encode()
        try:
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("src.web.server._find_or_create_env", return_value=tmp_env),
            ):
                handler, sock = _build_handler(
                    "/api/config", method="POST",
                    body=save_body,
                    headers={"Content-Type": "application/json",
                             "Content-Length": str(len(save_body))},
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertTrue(result["ok"])
                self.assertEqual(os.environ["VOICE_OPENAI_API_KEY"], "sk-voice-real-secret")

            saved_content = tmp_env.read_text(encoding="utf-8")
            self.assertIn("VOICE_OPENAI_API_KEY=sk-voice-real-secret", saved_content)
            self.assertNotIn("sk-v***cret", saved_content)
        finally:
            tmp_env.unlink(missing_ok=True)
            for f in tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_import_config_preserves_masked_secrets(self):
        """POST /api/config/import keeps existing secrets when import has masks."""
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text("VOICE_OPENAI_API_KEY=sk-voice-real-secret\n", encoding="utf-8")
        import_body = json.dumps({
            "ai_backend": "deepseek",                 # an old export: its chat keys are ignored
            "wechat_backend": "wcdb",
            "voice_openai_api_key": "sk-v***cret",
        }).encode()
        try:
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("src.web.server._find_or_create_env", return_value=tmp_env),
            ):
                handler, sock = _build_handler(
                    "/api/config/import", method="POST",
                    body=import_body,
                    headers={"Content-Type": "application/json",
                             "Content-Length": str(len(import_body))},
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertTrue(result["ok"])
                self.assertEqual(os.environ["VOICE_OPENAI_API_KEY"], "sk-voice-real-secret")

            saved_content = tmp_env.read_text(encoding="utf-8")
            self.assertIn("VOICE_OPENAI_API_KEY=sk-voice-real-secret", saved_content)
            self.assertNotIn("sk-v***cret", saved_content)
            self.assertNotIn("AI_BACKEND", saved_content)
        finally:
            tmp_env.unlink(missing_ok=True)
            for f in tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_save_config_updates_process_environment(self):
        """Saved config is available to bot threads started in this process."""
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text(
            "BOT_DISPLAY_NAME=old\n"
            "PRIVATE_CHAT_ENABLED=false\n",
            encoding="utf-8",
        )
        save_body = json.dumps({
            "bot_display_name": "商务号",
            "private_chat_enabled": True,
            "wechat_groups": "dev",
        }).encode()
        try:
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("src.web.server._find_or_create_env",
                      return_value=tmp_env),
            ):
                handler, sock = _build_handler(
                    "/api/config", method="POST",
                    body=save_body,
                    headers={"Content-Type": "application/json",
                             "Content-Length": str(len(save_body))},
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertTrue(result["ok"])
                self.assertEqual(os.environ["BOT_DISPLAY_NAME"], "商务号")
                self.assertEqual(os.environ["PRIVATE_CHAT_ENABLED"], "true")
                self.assertEqual(os.environ["WECHAT_GROUPS"], "dev")
        finally:
            tmp_env.unlink(missing_ok=True)
            for f in tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_save_config_invalid_json_returns_error(self):
        """POST /api/config with malformed JSON returns error."""
        handler, sock = _build_handler(
            "/api/config", method="POST",
            body=b"not-json{{{",
            headers={"Content-Type": "application/json"},
        )
        parts = sock.get_response_text().split("\r\n\r\n", 1)
        body = json.loads(parts[1])
        self.assertFalse(body["ok"])
        self.assertIn("error", body)

    def test_save_config_empty_body_handled(self):
        """POST /api/config with empty body should not crash."""
        with patch("src.web.server._find_or_create_env") as mock_find:
            fake_env = MagicMock()
            fake_env.exists.return_value = True
            fake_env.read_text.return_value = "AI_BACKEND=deepseek\n"
            fake_env.with_suffix.return_value = fake_env
            mock_find.return_value = fake_env
            with patch("os.replace"):
                handler, sock = _build_handler(
                    "/api/config", method="POST",
                    body=b"",
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertIsNotNone(result)

    def test_save_config_creates_env_if_missing(self):
        """POST /api/config creates .env when none exists."""
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        save_body = json.dumps({
            "wechat_backend": "wcdb",
            "bot_display_name": "商务号",
        }).encode()
        try:
            tmp_env.write_text("", encoding="utf-8")
            with patch("src.web.server._find_or_create_env",
                       return_value=tmp_env):
                handler, sock = _build_handler(
                    "/api/config", method="POST",
                    body=save_body,
                    headers={"Content-Type": "application/json",
                             "Content-Length": str(len(save_body))},
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                result = json.loads(parts[1])
                self.assertTrue(result["ok"])
            saved = tmp_env.read_text(encoding="utf-8")
            self.assertIn("WECHAT_BACKEND=wcdb", saved)
            self.assertIn("BOT_DISPLAY_NAME=商务号", saved)
        finally:
            tmp_env.unlink(missing_ok=True)
            for f in tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class ApiErrorHandlingTests(unittest.TestCase):
    """Tests for error handling across API endpoints."""

    def test_unknown_post_path_returns_405(self):
        """POST to an unknown path returns 405 Method Not Allowed."""
        handler, sock = _build_handler("/api/nonexistent", method="POST")
        response = sock.get_response_text()
        self.assertIn("405", response.splitlines()[0])

    def test_unknown_post_path_has_json_error(self):
        """405 response includes a JSON error body."""
        handler, sock = _build_handler("/api/nonexistent", method="POST")
        parts = sock.get_response_text().split("\r\n\r\n", 1)
        body = json.loads(parts[1])
        self.assertFalse(body["ok"])
        self.assertIn("error", body)

    def test_options_returns_204_with_cors(self):
        """A loopback preflight gets 204 plus the CORS headers."""
        handler, sock = _build_handler(
            "/api/status", method="OPTIONS",
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        response = sock.get_response_text()
        self.assertIn("204", response.splitlines()[0])
        self.assertIn("Access-Control-Allow-Origin: http://127.0.0.1:5173", response)
        self.assertIn("Access-Control-Allow-Methods: GET, POST, OPTIONS", response)
        self.assertIn("Access-Control-Allow-Headers: Content-Type", response)


# ---------------------------------------------------------------------------
# /api/logs endpoint tests
# ---------------------------------------------------------------------------


class ApiLogsEndpointTests(unittest.TestCase):
    """Tests for GET /api/logs."""

    def test_logs_file_not_found_returns_empty(self):
        """GET /api/logs returns an empty list when log file missing."""
        with patch("src.web.server._read_recent_logs",
                   return_value={"ok": True, "logs": [],
                                 "message": "日志文件尚未创建"}):
            handler, sock = _build_handler("/api/logs")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertTrue(body["ok"])
            self.assertEqual(body["logs"], [])

    def test_logs_with_entries(self):
        """GET /api/logs returns parsed log entries."""
        mock_logs = {
            "ok": True,
            "logs": [
                {"raw": "2024-06-01 12:00:00 [INFO] bot: Bot started",
                 "ts": "2024-06-01 12:00:00", "level": "INFO",
                 "module": "bot", "msg": "Bot started"},
                {"raw": "2024-06-01 12:01:00 [ERROR] bot: Crash",
                 "ts": "2024-06-01 12:01:00", "level": "ERROR",
                 "module": "bot", "msg": "Crash"},
            ],
        }
        with patch("src.web.server._read_recent_logs",
                   return_value=mock_logs):
            handler, sock = _build_handler("/api/logs")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertTrue(body["ok"])
            self.assertEqual(len(body["logs"]), 2)
            self.assertEqual(body["logs"][0]["level"], "INFO")
            self.assertEqual(body["logs"][1]["level"], "ERROR")

    def test_logs_error_handling(self):
        """GET /api/logs returns error info on failure."""
        with patch("src.web.server._read_recent_logs",
                   return_value={"ok": False, "logs": [],
                                 "error": "Permission denied"}):
            handler, sock = _build_handler("/api/logs")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], "Permission denied")


# ---------------------------------------------------------------------------
# _read_recent_logs helper tests
# ---------------------------------------------------------------------------


class ReadRecentLogsTests(unittest.TestCase):
    """Tests for _read_recent_logs."""

    def test_log_file_not_found_returns_empty(self):
        """Returns empty logs when log file doesn't exist."""
        from src.web.server import _read_recent_logs
        with patch.object(Path, "exists", return_value=False):
            result = _read_recent_logs()
            self.assertTrue(result["ok"])
            self.assertEqual(result["logs"], [])

    def test_parses_standard_log_entries(self):
        """Parses timestamp, level, module, msg from log lines."""
        from src.web.server import _read_recent_logs
        log_content = (
            "2024-06-01 12:00:00 [INFO] bot: Bot started\n"
            "2024-06-01 12:01:00 [ERROR] router: Something went wrong\n"
            "Traceback line without timestamp\n"
        )
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=log_content),
        ):
            result = _read_recent_logs()
        self.assertTrue(result["ok"])
        entries = result["logs"]
        self.assertGreaterEqual(len(entries), 2)
        self.assertEqual(entries[0]["ts"], "2024-06-01 12:00:00")
        self.assertEqual(entries[0]["level"], "INFO")
        self.assertEqual(entries[0]["module"], "bot")
        self.assertEqual(entries[0]["msg"], "Bot started")
        # Traceback line preserves raw
        raw_texts = [e["raw"] for e in entries]
        self.assertIn("Traceback line without timestamp", raw_texts)


# ---------------------------------------------------------------------------
# _set_env_key helper tests
# ---------------------------------------------------------------------------


class SetEnvKeyTests(unittest.TestCase):
    """Tests for _set_env_key -- .env key management."""

    def test_creates_new_file(self):
        """Creates a .env with the key when file does not exist."""
        from src.web.server import _set_env_key
        tmp_env = Path(tempfile.mkdtemp()) / ".env"
        try:
            _set_env_key(tmp_env, "TEST_KEY", "test_value")
            content = tmp_env.read_text(encoding="utf-8")
            self.assertIn("TEST_KEY=test_value", content)
        finally:
            tmp_env.unlink(missing_ok=True)
            tmp_env.parent.rmdir()

    def test_updates_existing_key(self):
        """Updates an existing key, preserves other keys."""
        from src.web.server import _set_env_key
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text("TEST_KEY=old_value\nOTHER_KEY=keep\n",
                           encoding="utf-8")
        try:
            _set_env_key(tmp_env, "TEST_KEY", "new_value")
            content = tmp_env.read_text(encoding="utf-8")
            self.assertIn("TEST_KEY=new_value", content)
            self.assertNotIn("old_value", content)
            self.assertIn("OTHER_KEY=keep", content)
        finally:
            tmp_env.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_adds_new_key_to_existing_file(self):
        """Appends a key that does not already exist."""
        from src.web.server import _set_env_key
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text("EXISTING_KEY=value\n", encoding="utf-8")
        try:
            _set_env_key(tmp_env, "NEW_KEY", "new_value")
            content = tmp_env.read_text(encoding="utf-8")
            self.assertIn("EXISTING_KEY=value", content)
            self.assertIn("NEW_KEY=new_value", content)
        finally:
            tmp_env.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_ignores_comment_lines(self):
        """Does not match against commented-out lines."""
        from src.web.server import _set_env_key
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text("# TEST_KEY=commented\nREAL_KEY=real\n",
                           encoding="utf-8")
        try:
            _set_env_key(tmp_env, "TEST_KEY", "uncommented")
            content = tmp_env.read_text(encoding="utf-8")
            self.assertIn("# TEST_KEY=commented", content)
            self.assertIn("TEST_KEY=uncommented", content)
        finally:
            tmp_env.unlink(missing_ok=True)
            tmp_dir.rmdir()


# ---------------------------------------------------------------------------
# Onboarding API tests
# ---------------------------------------------------------------------------


class OnboardingApiTests(unittest.TestCase):
    """Tests for /api/onboarding/* endpoints."""

    def test_onboarding_status_returns_steps(self):
        """GET /api/onboarding/status returns step completion info."""
        with patch("src.config.is_onboarding_done", return_value=False):
            handler, sock = _build_handler("/api/onboarding/status")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertTrue(body["ok"])
            self.assertFalse(body["onboarding_done"])
            self.assertIn("steps", body)
            self.assertIn("step1", body["steps"])

    def test_onboarding_status_when_done(self):
        """Returns onboarding_done=True when completed."""
        with patch("src.config.is_onboarding_done", return_value=True):
            handler, sock = _build_handler("/api/onboarding/status")
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            body = json.loads(parts[1])
            self.assertTrue(body["onboarding_done"])

    def test_onboarding_reset_returns_ok(self):
        """POST /api/onboarding/reset returns ok=True."""
        with patch("src.web.server._find_or_create_env") as mock_find:
            fake_env = MagicMock()
            fake_env.exists.return_value = True
            fake_env.with_suffix.return_value = fake_env
            mock_find.return_value = fake_env
            with patch("os.replace"):
                handler, sock = _build_handler(
                    "/api/onboarding/reset", method="POST",
                )
                parts = sock.get_response_text().split("\r\n\r\n", 1)
                body = json.loads(parts[1])
                self.assertTrue(body["ok"])

    def test_onboarding_step2_posts_data(self):
        """POST /api/onboarding/step2 accepts WeChat identity data."""
        post_body = json.dumps({
            "bot_display_name": "TestBot",
            "wechat_groups": "*",
            "wechat_backend": "wcdb",
        })
        handler, sock = _build_handler(
            "/api/onboarding/step2", method="POST",
            body=post_body.encode(),
            headers={"Content-Type": "application/json"},
        )
        parts = sock.get_response_text().split("\r\n\r\n", 1)
        result = json.loads(parts[1])
        self.assertTrue(result["ok"])

    def test_onboarding_step3_writes_the_env_and_returns_ok(self):
        """POST /api/onboarding/step3 (the last step) takes the private chats and writes .env."""
        with (
            patch("src.web.server._find_or_create_env") as mock_find,
            patch("src.web.server._write_onboarding_to_env") as write,
        ):
            mock_find.return_value = MagicMock()
            post_body = json.dumps({"private_chat_enabled": True, "private_chats": "wxid_me"})
            handler, sock = _build_handler(
                "/api/onboarding/step3", method="POST",
                body=post_body.encode(),
                headers={"Content-Type": "application/json"},
            )
            parts = sock.get_response_text().split("\r\n\r\n", 1)
            result = json.loads(parts[1])
            self.assertTrue(result["ok"])
            write.assert_called_once()

    def test_there_is_no_fourth_onboarding_step(self):
        handler, sock = _build_handler("/api/onboarding/step4", method="POST", body=b"{}")
        self.assertIn("405", sock.get_response_text().splitlines()[0])

# ---------------------------------------------------------------------------
# Cross-site and DNS-rebinding protection (the console has no login)
# ---------------------------------------------------------------------------


class ConsoleCrossOriginTests(unittest.TestCase):
    """Only this machine's own pages may use the loopback console."""

    def _call(self, path="/api/status", method="GET", headers=None, body=b""):
        handler, sock = _build_handler(path, method=method, headers=headers, body=body)
        head, _, payload = sock.get_response_text().partition("\r\n\r\n")
        return head, payload

    def test_foreign_origin_is_rejected_without_cors_headers(self):
        for origin in ("https://evil.example", "http://192.168.1.5:7327", "null"):
            with self.subTest(origin=origin):
                head, payload = self._call(headers={"Origin": origin})
                self.assertIn("403", head.splitlines()[0])
                self.assertNotIn("Access-Control-Allow-Origin", head)
                self.assertFalse(json.loads(payload)["ok"])

    def test_state_changing_post_from_a_foreign_origin_does_nothing(self):
        with patch("src.web.server._bot_control") as control:
            head, _ = self._call(
                "/api/start", method="POST",
                headers={"Origin": "https://evil.example",
                         "Content-Type": "text/plain"},
                body=b"{}",
            )
        self.assertIn("403", head.splitlines()[0])
        control.assert_not_called()

    def test_foreign_preflight_is_rejected(self):
        head, _ = self._call(
            method="OPTIONS", headers={"Origin": "https://evil.example"},
        )
        self.assertIn("403", head.splitlines()[0])

    def test_dns_rebinding_host_is_rejected(self):
        for host in ("evil.example", "evil.example:7327", "192.168.1.5:7327"):
            with self.subTest(host=host):
                head, _ = self._call(headers={"Host": host})
                self.assertIn("403", head.splitlines()[0])

    def test_loopback_hosts_and_origins_are_allowed(self):
        for host in ("localhost", "localhost:7327", "127.0.0.1:7327", "[::1]:7327"):
            for origin in (None, "http://127.0.0.1:7327", "http://localhost:5173"):
                with self.subTest(host=host, origin=origin):
                    headers = {"Host": host}
                    if origin:
                        headers["Origin"] = origin
                    head, _ = self._call(headers=headers)
                    self.assertIn("200", head.splitlines()[0])

    def test_json_api_responses_echo_the_allowed_origin(self):
        head, _ = self._call(
            "/api/status", headers={"Origin": "http://localhost:7327"},
        )
        self.assertIn("Access-Control-Allow-Origin: http://localhost:7327", head)
        self.assertNotIn("Access-Control-Allow-Origin: *", head)

    def test_state_changing_endpoints_refuse_get(self):
        # A page on any website can make the browser GET 127.0.0.1 (an <img>
        # tag sends no Origin header), so these must only react to POST.
        from src.web.server import _POST_ONLY_PATHS

        self.assertIn("/api/config", _POST_ONLY_PATHS)
        self.assertIn("/api/stop", _POST_ONLY_PATHS)
        self.assertIn("/api/prm/friend-adds", _POST_ONLY_PATHS)          # 加百应达人微信: an add is a POST only
        for path in sorted(_POST_ONLY_PATHS):
            for suffix in ("", "?x=1"):
                with self.subTest(path=path + suffix), \
                        patch("src.web.server._find_or_create_env") as env, \
                        patch("src.web.server._bot_control") as control:
                    head, payload = self._call(path + suffix)
                    self.assertIn("405", head.splitlines()[0])
                    self.assertFalse(json.loads(payload)["ok"])
                    env.assert_not_called()
                    control.assert_not_called()

    def test_endpoints_that_read_and_write_still_answer_get(self):
        with patch("src.web.server._find_or_create_env") as env:
            env.return_value = Path("/nonexistent/.env")
            for path in ("/api/status", "/api/load-config", "/api/config/export"):
                with self.subTest(path=path):
                    head, _ = self._call(path)
                    self.assertIn("200", head.splitlines()[0])

    def test_a_console_bound_to_a_lan_address_is_not_restricted(self):
        from src.web.server import _UIHandler

        sock = MockSocket(
            b"GET /api/status HTTP/1.1\r\nHost: 192.168.1.5:7327\r\n"
            b"Origin: http://192.168.1.5:7327\r\n\r\n"
        )
        server = MagicMock()
        server.server_address = ("0.0.0.0", 7327)
        _UIHandler(sock, ("192.168.1.9", 9999), server)
        head = sock.get_response_text().partition("\r\n\r\n")[0]
        self.assertIn("200", head.splitlines()[0])


# ---------------------------------------------------------------------------
# WebSocket is intentionally disabled
# ---------------------------------------------------------------------------


class WebSocketDisabledTests(unittest.TestCase):
    """The local server is HTTP-only; /ws answers 404 and points to /api/status."""

    def test_ws_endpoint_reports_disabled(self):
        handler, sock = _build_handler("/ws")
        head, _, body = sock.get_response_text().partition("\r\n\r\n")
        self.assertIn("404", head.splitlines()[0])
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("/api/status", payload["error"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# /api/wechat-gaps: when PC WeChat was not online (2026-10-28)
# ---------------------------------------------------------------------------


class WechatGapsApiTests(unittest.TestCase):
    """GET lists the stretches without PC WeChat an operator has not seen (the bot stopped: read
    straight from DB_PATH, nothing made there); only a POST marks one 看过了 (a GET from another page
    cannot)."""

    def _json(self, sock):
        return json.loads(sock.get_response_text().split("\r\n\r\n", 1)[1])

    def test_get_lists_and_only_a_post_marks_seen(self):
        from src.wechat.presence import PresenceStore

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "messages.db"
            env = {"DB_PATH": str(db), "WECHAT_BACKEND": "wcdb"}
            with patch.dict(os.environ, env):
                _, sock = _build_handler("/api/wechat-gaps")
                self.assertEqual(self._json(sock), {"ok": True, "source": "windows", "open": None, "gaps": []})
                self.assertFalse(db.exists(), "a GET makes no database")
            store = PresenceStore(str(db))
            gap = store.add_gap(1761462000, "offline_while_running", "找不到微信主窗口", 1761462100)
            store.close_gap(gap["id"], 1761465600)
            store.add_gap(1761470000, "stopped_with_bot", "", 1761470100)        # going on: not shown when stopped
            store.close()
            with patch.dict(os.environ, env):
                _, sock = _build_handler(f"/api/wechat-gaps?id={gap['id']}&action=seen")
                body = self._json(sock)
                self.assertEqual([g["id"] for g in body["gaps"]], [gap["id"]])
                self.assertIsNone(body["open"])
                self.assertIn("同步最近的聊天记录", body["gaps"][0]["text"])
                payload = json.dumps({"id": gap["id"], "action": "seen"}).encode()
                _, sock = _build_handler("/api/wechat-gaps", method="POST", body=payload,
                                         headers={"Content-Type": "application/json"})
                self.assertEqual(self._json(sock), {"ok": True})
                _, sock = _build_handler("/api/wechat-gaps")
                self.assertEqual(self._json(sock)["gaps"], [])
                _, sock = _build_handler("/api/wechat-gaps", method="POST", body=b"[1]",
                                         headers={"Content-Type": "application/json"})
                self.assertFalse(self._json(sock)["ok"])
