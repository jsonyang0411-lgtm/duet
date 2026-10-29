"""
Zero-dependency web UI server for the bot dashboard.

Uses only Python stdlib (http.server for HTTP).
Serves the React UI from ui/dist/ and provides bot status via HTTP polling.

Runs in a daemon thread — no impact on the main bot loop.
"""
import json
import logging
import os
_os = os
import re
import threading
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Re-exported from config.py for use in API handlers.
# NOTE: _decode_wechat_groups is also imported inside _handle_request()
# conditional blocks, but for Python's scoping those later imports still
# make the name a local — so it must be imported at module level too,
# otherwise the first branch that references it raises UnboundLocalError.
from src.config import _decode_wechat_groups

logger = logging.getLogger(__name__)

import sys as _sys
if getattr(_sys, "frozen", False):
    UI_DIR = (Path(_sys._MEIPASS) / "ui" / "dist").resolve()
else:
    UI_DIR = (Path(__file__).resolve().parent.parent.parent / "ui" / "dist").resolve()
def _messages_table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    return row is not None


def _find_or_create_env() -> Path:
    """Find .env file, using the canonical search order from config.py.

    If .env is not found but .env.example is, copy it to create a new .env.
    """
    import sys

    # 1. Use the canonical search from config.py (consistent across the app)
    from src.config import find_env_file, _decode_wechat_groups
    existing = find_env_file()
    if existing:
        return existing

    # 2. Not found — try to create from .env.example
    # .env.example is bundled into _MEIPASS in frozen mode.
    if getattr(sys, "frozen", False):
        env_example = Path(sys._MEIPASS) / ".env.example"
    else:
        env_example = Path(__file__).resolve().parent.parent.parent / ".env.example"

    if env_example.exists():
        # Create .env in app home (CWD already set to EXE dir / project root)
        env_path = Path(".env")
        env_path.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Created .env from .env.example at %s", env_path.resolve())
        return env_path

    # 3. Last resort: create minimal .env in app home
    env_path = Path(".env")
    env_path.write_text(
        "WECHAT_BACKEND=wcdb\n"
        "BOT_DISPLAY_NAME=\n"
        "WECHAT_GROUPS=\n"
        "PRIVATE_CHAT_ENABLED=false\n"
        "PRIVATE_CHATS=*\n"
        "MESSAGE_BURST_ENABLED=true\n"
        "MESSAGE_BURST_WINDOW_SEC=2.2\n"
        "MESSAGE_BURST_MAX_MESSAGES=5\n"
        "MESSAGE_BURST_MAX_CHARS=200\n",
        encoding="utf-8",
    )
    logger.info("Created minimal .env at %s", env_path.resolve())
    return env_path


def _find_or_create_machine_env() -> Path:
    """Find/create the per-machine env override used for local WeChat data."""
    from src.config import find_local_env_file

    existing = find_local_env_file()
    if existing:
        return existing

    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        env_path = Path(appdata) / "webot" / ".env.local"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        if not env_path.exists():
            env_path.write_text("", encoding="utf-8")
        return env_path

    return _find_or_create_env()


def _detect_default_data_dir() -> str:
    """Auto-detect the default WeChat data directory (parent of wxid_*).

    Returns the base directory path string, or empty string if not found.
    Used by the UI to show what auto-detection would use.
    """
    candidates = [
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "xwechat_files",
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "WeChat Files",
    ]
    for base in candidates:
        if not base.exists():
            continue
        try:
            wxid_dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith("wxid_")]
            for wxid_dir in wxid_dirs:
                session_db = wxid_dir / "db_storage" / "session" / "session.db"
                if session_db.exists():
                    return str(base)
        except PermissionError:
            continue
    return ""


def _mask_key(value: str) -> str:
    """Mask a sensitive key: show first 4 + last 4 chars, or '***' if too short."""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


_SECRET_ENV_KEYS = {
    "VOICE_OPENAI_API_KEY",
}


def _is_masked_secret(value) -> bool:
    """Return True when a UI/API value is a displayed mask, not a real secret."""
    return isinstance(value, str) and "***" in value


def _env_values_from_lines(lines: list[str]) -> dict[str, str]:
    """Parse simple KEY=VALUE lines so masked secrets can preserve old values."""
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _read_env_values(env_path: Path | None) -> dict[str, str]:
    if not env_path or not env_path.exists():
        return {}
    return _env_values_from_lines(env_path.read_text(encoding="utf-8").splitlines())


def _read_effective_env_values() -> dict[str, str]:
    """Read shared .env plus optional machine-local overrides."""
    from src.config import find_local_env_file

    raw = _read_env_values(_find_or_create_env())
    for key, value in _read_env_values(find_local_env_file()).items():
        if str(value or "").strip():
            raw[key] = value
    return raw


def _preserve_masked_secret_updates(
    updates: dict[str, str | None],
    existing_values: dict[str, str],
) -> None:
    """Keep the existing secret when the UI posts back a masked placeholder."""
    for key in _SECRET_ENV_KEYS:
        if _is_masked_secret(updates.get(key)):
            updates[key] = existing_values.get(key, "")


def _decode_name_list(raw: str | None) -> list[str]:
    """Group / contact names from WECHAT_GROUPS or PRIVATE_CHATS as a list.

    Each comma-separated chunk is decoded on its own, so a name stored as
    ``%E6%B5%8B%E8%AF%95%2C%E7%BE%A4`` stays one name ("测试,群"); ``*`` and
    an empty value give ``[]`` (= all).
    """
    text = str(raw or "").strip()
    if not text or text == "*":
        return []
    names = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                names.append(unquote(chunk))
            except Exception:
                names.append(chunk)
    return names


def _detect_wxid_and_db_path():
    """Auto-detect WeChat wxid and database path from common locations.

    Respects WECHAT_DATA_DIR env var as a custom base dir (scanned first).
    """

    candidates: list[Path] = []

    # 1. Custom path from env (highest priority)
    custom_dir = _os.environ.get("WECHAT_DATA_DIR", "").strip()
    if custom_dir:
        custom = Path(custom_dir)
        if custom.exists() and custom.is_dir():
            candidates.append(custom)

    # 2. Default locations
    candidates += [
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "xwechat_files",
        Path(_os.environ.get("USERPROFILE", "")) / "Documents" / "WeChat Files",
    ]
    for base in candidates:
        if not base.exists():
            continue
        wxid_dirs = sorted(
            [d for d in base.iterdir() if d.is_dir() and d.name.startswith("wxid_")],
            key=lambda d: d.stat().st_mtime, reverse=True,
        )
        for wxid_dir in wxid_dirs:
            session_db = wxid_dir / "db_storage" / "session" / "session.db"
            if session_db.exists():
                return wxid_dir.name, str(session_db)
            # Older WeChat versions
            msg_dir = wxid_dir / "Msg"
            if msg_dir.exists():
                db_files = sorted(msg_dir.glob("MSG*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
                if db_files:
                    return wxid_dir.name, str(db_files[0])
    return None, None


def _set_env_key(env_path: Path, key: str, value: str) -> None:
    """Set or update one key=value in a .env file atomically."""
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines, found = [], False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.replace(tmp, env_path)


def _write_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    """Set several key=value pairs in a .env file in one atomic write.

    Existing lines keep their place and every occurrence of a key is
    replaced (dotenv reads the last one, and .env.example ships some keys
    uncommented, so a hand-added duplicate must not win); new keys are
    appended.
    """
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    new_lines, seen = [], set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.replace(tmp, env_path)


_WCDB_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
# Line breaks of any kind (str.splitlines() splits on all of them) or other
# control characters would break a .env line.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\x85  ]")


def _write_onboarding_to_env(env_path):
    """Write accumulated onboarding data to .env file atomically."""
    env_map = {
        "WECHAT_BACKEND": _onboarding_data.get("wechat_backend", "wcdb"),
        "WECHAT_GROUPS": _onboarding_data.get("wechat_groups", "*"),
        "PRIVATE_CHAT_ENABLED": str(_onboarding_data.get("private_chat_enabled", False)).lower(),
        "PRIVATE_CHATS": _onboarding_data.get("private_chats", "*"),
        "BOT_DISPLAY_NAME": _onboarding_data.get("bot_display_name", "群聊小助手"),
        "MESSAGE_BURST_ENABLED": str(_onboarding_data.get("message_burst_enabled", True)).lower(),
        "MESSAGE_BURST_WINDOW_SEC": str(_onboarding_data.get("message_burst_window_sec", 2.2)),
        "MESSAGE_BURST_MAX_MESSAGES": str(_onboarding_data.get("message_burst_max_messages", 5)),
        "MESSAGE_BURST_MAX_CHARS": str(_onboarding_data.get("message_burst_max_chars", 200)),
        "WCDB_KEY": _onboarding_data.get("key", ""),
        "ONBOARDING_DONE": "true",
    }
    machine_env_path = _find_or_create_machine_env()
    try:
        has_separate_machine_env = machine_env_path.resolve() != env_path.resolve()
    except Exception:
        has_separate_machine_env = str(machine_env_path).lower() != str(env_path).lower()
    if has_separate_machine_env:
        env_map["WCDB_KEY"] = None

    # Preserve existing keys not managed by onboarding
    if env_path.exists():
        lines = []
        seen = set()
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in env_map and env_map[key] is not None:
                    lines.append(f"{key}={env_map[key]}")
                    seen.add(key)
                    continue
            lines.append(line)
        for key, val in env_map.items():
            if key not in seen and val is not None:
                lines.append(f"{key}={val}")
        content = "\n".join(lines) + "\n"
    else:
        content = "\n".join(f"{k}={v}" for k, v in env_map.items() if v is not None) + "\n"

    tmp_path = env_path.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, env_path)
    logger.info("Onboarding complete — wrote .env")

    if has_separate_machine_env:
        wcdb_key = _onboarding_data.get("key", "")
        if wcdb_key:
            _set_env_key(machine_env_path, "WCDB_KEY", wcdb_key)
        detected_data_dir = _detect_default_data_dir()
        if detected_data_dir:
            _set_env_key(machine_env_path, "WECHAT_DATA_DIR", detected_data_dir)
        logger.info("Onboarding machine-local values written to %s", machine_env_path)


def _run_step1_extraction():
    """Background thread: wait for WeChat exit → restart → hook → capture.

    Uses extract_wcdb_key's on_progress callback to push real-time phase
    updates to the frontend so the user sees exactly what's happening.
    """
    # ── Ensure file logging is active during key extraction ──────────
    # setup_logging() normally runs inside Bot.run(), but key extraction
    # happens BEFORE the bot starts (during onboarding).  Without this,
    # all log output from extract_key.py goes to stdout only, which is
    # invisible in Windows GUI mode — making failures un-debuggable.
    from src.utils.logging_config import setup_logging
    from src.config import PROJECT_ROOT
    setup_logging(level="INFO", log_file=str(PROJECT_ROOT / "data" / "bot.log"))

    from src.wechat.extract_key import extract_wcdb_key

    def _on_progress(phase, message):
        """Push progress updates to the frontend via _step1_state."""
        with _step1_lock:
            _step1_state["phase"] = phase
            _step1_state["message"] = message

    try:
        # extract_wcdb_key(require_restart=True) handles the full flow.
        # on_progress pushes phase changes so the frontend can display
        # real-time instructions (hooking → waiting_exit → waiting_login
        # → hooking_restart).
        key = extract_wcdb_key(require_restart=True,
                               on_progress=_on_progress)

        if key:
            wxid, db_path = _detect_wxid_and_db_path()
            with _onboarding_lock:
                _onboarding_data["step1_done"] = True
                _onboarding_data["key"] = key
                _onboarding_data["wxid"] = wxid or ""
                _onboarding_data["db_path"] = db_path or ""

            # Persist the key to .env immediately so the bot can use it
            # on restart without needing to complete the full onboarding flow.
            machine_env_path = _find_or_create_machine_env()
            _set_env_key(machine_env_path, "WCDB_KEY", key)
            detected_data_dir = _detect_default_data_dir()
            if detected_data_dir:
                _set_env_key(machine_env_path, "WECHAT_DATA_DIR", detected_data_dir)
            # Also set in the current process for load_dotenv in this session
            _os.environ["WCDB_KEY"] = key
            if detected_data_dir:
                _os.environ["WECHAT_DATA_DIR"] = detected_data_dir
            # Clear the KEY_MISSING error so it doesn't reappear on page refresh
            update_status(error="")

            with _step1_lock:
                _step1_state["phase"] = "done"
                _step1_state["message"] = "密钥获取成功"
                _step1_state["result"] = {"key": key, "wxid": wxid or "", "db_path": db_path or ""}
                _step1_state["running"] = False
        else:
            with _step1_lock:
                _step1_state["phase"] = "timeout"
                _step1_state["message"] = "密钥提取超时，请确保微信已登录并重试"
                _step1_state["running"] = False

    except Exception as e:
        logger.exception("Step1 extraction failed")
        with _step1_lock:
            _step1_state["phase"] = "error"
            _step1_state["message"] = str(e)
            _step1_state["running"] = False


def _list_dir_entries(target: Path) -> list[dict]:
    """List directory entries for the filesystem browser API.

    Returns only directories (the user is browsing for parent dir of wxid_*).
    Sorted: directories first, then alphabetically.
    """
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith(".") or child.name.startswith("$"):
                continue  # skip hidden/system entries
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
            })
    except PermissionError:
        pass
    return entries


def _read_recent_logs():
    """Read the last 500 lines from the bot log file. Returns JSON-serializable list.

    Log format: ``YYYY-MM-DD HH:MM:SS [LEVEL] module: message``
    (configured in src/utils/logging_config.py).
    """
    import re
    # CWD is set to app home by desktop.py; relative path works for both
    # frozen (EXE dir) and dev (project root) modes.
    log_path = Path("data/bot.log")
    if not log_path.exists():
        return {"ok": True, "logs": [], "message": "日志文件尚未创建"}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        # Return last 500 lines
        recent = lines[-500:]
        # Regex: timestamp [LEVEL] module: message
        pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+'
            r'\[(DEBUG|INFO|WARNING|ERROR)\]\s+'
            r'([^:]+):\s+'
            r'(.*)$'
        )
        entries = []
        for line in recent:
            entry = {"raw": line}
            m = pattern.match(line.strip())
            if m:
                entry["ts"] = m.group(1)
                entry["level"] = m.group(2)
                entry["module"] = m.group(3)
                entry["msg"] = m.group(4)
            else:
                # Fallback for lines that don't match (tracebacks, multi-line, etc.)
                entry["ts"] = ""
                entry["level"] = "INFO"
                entry["module"] = ""
                entry["msg"] = line
            entries.append(entry)
        return {"ok": True, "logs": entries}
    except Exception as e:
        return {"ok": False, "logs": [], "error": str(e)}


def _can_import(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


def _platform_dependency_report(system_name=None, import_checker=None, command_checker=None):
    """Return platform-aware dependency diagnostics for onboarding."""
    import platform
    import shutil

    system = system_name or platform.system()
    import_checker = import_checker or _can_import
    command_checker = command_checker or shutil.which

    req_mapping = {
        "dotenv": "python-dotenv",
        "openai": "openai",
        "webview": "pywebview",
        "PIL": "Pillow",
        "psutil": "psutil",
        "pyperclip": "pyperclip",
    }
    if system == "Windows":
        req_mapping.update({
            "uiautomation": "uiautomation",
            "win32api": "pywin32",
            "comtypes": "comtypes",
        })

    missing_reqs = []
    for mod, pkg in req_mapping.items():
        if not import_checker(mod):
            missing_reqs.append(pkg)

    if system == "Darwin":
        for command in ("osascript", "pbcopy"):
            if not command_checker(command):
                missing_reqs.append(command)

    ok = len(missing_reqs) == 0
    value = "所有依赖已安装" if ok else f"缺少依赖: {', '.join(missing_reqs)}"
    return {"ok": ok, "value": value, "missing": missing_reqs}


def _platform_wechat_report(system_name=None):
    """Return a platform-aware WeChat process status."""
    import platform
    import subprocess

    system = system_name or platform.system()
    if system == "Darwin":
        app_name = _os.getenv("MAC_WECHAT_APP_NAME", "WeChat")
        try:
            result = subprocess.run(
                ["pgrep", "-x", app_name],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().splitlines()[0]
                return {"ok": True, "value": f"微信运行中 (PID {pid})", "error": None}
            return {"ok": False, "value": "微信未运行", "error": "请启动 macOS 微信并授权辅助功能权限"}
        except Exception as e:
            return {"ok": False, "value": f"微信检测出错: {e}", "error": str(e)}

    try:
        from src.wechat.native.injector import _find_wechat_pid
        wx_pid, wx_name = _find_wechat_pid()
        wx_ok = wx_pid is not None
        wx_val = f"微信运行中 (PID {wx_pid})" if wx_ok else "微信未运行"
        return {"ok": wx_ok, "value": wx_val, "error": None if wx_ok else "请登录微信电脑端"}
    except Exception as e:
        return {"ok": False, "value": f"微信检测出错: {e}", "error": str(e)}


def _macos_wechat_diagnostics(system_name=None, automation=None):
    """Run macOS WeChat permission diagnostics from this process identity."""
    import platform

    system = system_name or platform.system()
    if system != "Darwin":
        return {
            "ok": False,
            "skipped": True,
            "error": "macOS diagnostics are only available on Darwin",
        }

    try:
        if automation is None:
            from src.wechat.mac_ui_backend import MacUIAutomation

            automation = MacUIAutomation()
        return automation.diagnose_access()
    except Exception as exc:
        logger.exception("macOS WeChat diagnostics failed")
        return {
            "ok": False,
            "skipped": False,
            "error": str(exc),
        }


# ── Thread-safe server state classes ────────────────────────────────────


class _ServerStatus:
    """Thread-safe bot status snapshot served by ``/api/status``.

    All writes are serialized through an internal lock so concurrent
    update_status() calls from different threads never produce inconsistent
    status snapshots.
    """

    _FIELDS = (
        "running", "uptime_sec", "messages_processed",
        "wechat_backend", "db_ok",
        "timestamp", "error",
    )

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.uptime_sec = 0
        self.messages_processed = 0
        self.wechat_backend = ""
        self.db_ok = False
        self.timestamp = ""
        self.error = ""
    def update(self, **kwargs):
        """Update status fields atomically."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    def snapshot(self):
        """Return a consistent dict snapshot (thread-safe)."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        """Build a dict from fields (caller must hold _lock)."""
        return {k: getattr(self, k) for k in self._FIELDS}

class _BotControl:
    """Thread-safe bot lifecycle control.

    Serializes start/stop transitions so concurrent API requests cannot
    create duplicate bot instances or leave the state inconsistent.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.thread = None
        self.backend = None
        self.running = False

    def register(self, thread=None, backend=None):
        with self._lock:
            if thread is not None:
                self.thread = thread
            if backend is not None:
                self.backend = backend
            self.running = True

    def register_backend(self, backend):
        """Called by Bot.run() during initialization."""
        with self._lock:
            self.backend = backend

    def stop(self):
        """Stop the bot backend and wait for the thread to exit."""
        # Read refs under lock, then call stop + join outside the lock
        # to avoid deadlock if stop() needs the lock.
        with self._lock:
            backend = self.backend
            thread = self.thread

        if backend is not None and hasattr(backend, "stop"):
            backend.stop()

        if thread is not None and thread.is_alive():
            thread.join(timeout=30)

        with self._lock:
            self.running = False
            self.backend = None
            self.thread = None
        return backend is not None

    def is_running(self):
        with self._lock:
            return self.running

    def set_running(self):
        with self._lock:
            self.running = True

    def mark_stopped(self):
        """Reset running state when the bot thread exits on its own.

        Does NOT stop the backend or join the thread — use stop() for
        external shutdown requests.  This is called from within the bot
        thread's ``finally`` block so the next /api/start can proceed.
        """
        with self._lock:
            self.running = False
            self.backend = None
            self.thread = None

    def set_thread(self, thread):
        with self._lock:
            self.thread = thread


class _ServerStartGuard:
    """Thread-safe idempotent server start guard."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = False

    def try_start(self):
        """Return True if server should start, False if already started."""
        with self._lock:
            if self._started:
                return False
            self._started = True
            return True


# ── Module-level instances ────────────────────────────────────────────

_status = _ServerStatus()
_bot_control = _BotControl()
_server_guard = _ServerStartGuard()
_shutdown_event = threading.Event()
_voice_downloads: dict[str, dict] = {}  # model → {active, msg}


def signal_shutdown():
    """Signal all components to stop (called on app exit)."""
    _shutdown_event.set()


def is_shutting_down():
    """Check if shutdown has been signaled."""
    return _shutdown_event.is_set()

# ── Onboarding state ──────────────────────────────────────────────────

_onboarding_data = {
    "step1_done": False, "step2_done": False, "step3_done": False,
    "key": "", "wxid": "", "db_path": "",
    "bot_display_name": "", "wechat_groups": "*", "wechat_backend": "wcdb",
    "private_chat_enabled": False, "private_chats": "*",
    "message_burst_enabled": True,
    "message_burst_window_sec": 2.2,
    "message_burst_max_messages": 5,
    "message_burst_max_chars": 200,
}
_onboarding_lock = threading.Lock()

# Async step1 state
_step1_state = {
    "running": False,
    "phase": "idle",   # idle | waiting_exit | waiting_login | hooking | done | error
    "message": "",
    "result": None,    # {"key": ..., "wxid": ..., "db_path": ...}
}
_step1_thread = None
_step1_lock = threading.Lock()


# ── Public API wrappers (delegate to thread-safe classes) ─────────────


def update_status(**kwargs):
    """Update the status snapshot read by the HTTP status endpoint."""
    _status.update(**kwargs)


def register_bot(thread=None, backend=None):
    """Register bot thread/backend so the web API can control it."""
    _bot_control.register(thread=thread, backend=backend)
    update_status(running=True)


def _bot_exited():
    """Notify that the bot thread has exited (any path — normal/error).

    Resets the control lock so the next /api/start can proceed.
    Called from desktop.py's start_bot() and _start_bot_in_thread().
    """
    _bot_control.mark_stopped()


def _register_backend(backend):
    """Register backend from Bot.run() — explicit API, no monkey-patching."""
    _bot_control.register_backend(backend)


# The running ClaudeRelay (CLAUDE_RELAY=on), set by Bot.run(); None when the bot
# is stopped or the relay is off.  /api/relay reads it.
_relay = None


def register_relay(relay):
    """Register (or with None, forget) the running Claude relay for /api/relay."""
    global _relay
    _relay = relay


def _lan_addresses() -> list[dict]:
    """This computer's IPv4 addresses for the Mac's relay_url, ``[{"ip", "kind"}]``: the LAN
    ones first, then Tailscale's (when the Mac is on another network, cross-network.md)."""
    from src.relay.link import this_pc_addresses
    return this_pc_addresses()


def _relay_job_for_console(job: dict) -> dict:
    """A job as the console shows it: no wxid, the text cut to 200 characters; phone and ID numbers
    masked first, as everywhere on the console (a draft of the Mac may carry a number it read off a
    creator's picture, a cc task one typed in)."""
    from src.prm.pii import mask_numbers

    try:
        extra = json.loads(job.get("result_json") or "{}")
    except ValueError:
        extra = {}
    text = mask_numbers(str(job.get("text") or ""))
    return {
        "id": job["id"],
        "kind": job.get("kind") or "admin",
        "status": job["status"],
        "text": text[:200],
        "project": job.get("project") or "",
        "created_at": job.get("created_at"),
        "claimed_at": job.get("claimed_at"),
        "finished_at": job.get("finished_at"),
        "duration_s": extra.get("duration_s"),
        "result": mask_numbers(str(job.get("result_text") or ""))[:400],
        "error": mask_numbers(str(extra.get("error") or ""))[:300],
        "cancel_requested": bool(job.get("cancel_requested")),
    }


def _relay_snapshot(limit: int = 30) -> dict:
    """GET /api/relay: the relay's state and its newest jobs (also when the bot is stopped)."""
    relay = _relay
    addresses = _lan_addresses()
    here = {"lan_addresses": [a["ip"] for a in addresses], "addresses": addresses}
    if relay is not None:
        jobs = relay.store.list_jobs(limit)
        return {"ok": True, "running": True, **relay.snapshot(),
                "jobs": [_relay_job_for_console(j) for j in jobs], **here}
    out = {"ok": True, "running": False, "listening": False, "mac_online": False, "address": None,
           "mac_last_seen_sec": None, "mac_address": None, "mac_link": None, "counts": {}, "jobs": [], **here}
    db_path = Path(os.getenv("CLAUDE_RELAY_DB", "data/claude_relay.db").strip() or "data/claude_relay.db")
    if db_path.exists():
        from src.relay.store import RelayStore
        store = RelayStore(db_path)
        try:
            out["jobs"] = [_relay_job_for_console(j) for j in store.list_jobs(limit)]
            out["counts"] = store.counts()
        finally:
            store.close()
    return out


# Mac 端（WECHAT_BACKEND=edge）运行中的 edge 后端，由 Bot.run() 注册；/api/edge 读它的状态
_edge_client = None


def register_edge_client(backend):
    """登记（传 None 就是忘掉）运行中的 edge 后端，给 /api/edge 用。"""
    global _edge_client
    _edge_client = backend


def _edge_state() -> dict:
    """GET /api/edge：Windows 微信边缘的状态和要提醒运营的事。不是 Mac 端（edge）时 mode 为空。"""
    mode = "edge" if os.getenv("WECHAT_BACKEND", "").strip() == "edge" else ""
    if _edge_client is None:
        return {"mode": mode, "running": False, "problems": []}
    return {"mode": "edge", **_edge_client.status()}


# The running watch of PC WeChat (src/wechat/presence.py, Windows), set by Bot.run; None when the
# bot is stopped: /api/wechat-gaps then reads the bot's database (DB_PATH) itself.
_presence = None


def register_presence(service):
    """Register (or with None, forget) the running WeChat presence watch for /api/wechat-gaps."""
    global _presence
    _presence = service


def _db_path() -> Path:
    return Path(os.getenv("DB_PATH", "data/messages.db").strip() or "data/messages.db")


def _mac_side() -> bool:
    """This console is the Mac's (WECHAT_BACKEND=edge): the gaps are the ones Windows reported."""
    return _edge_client is not None or os.getenv("WECHAT_BACKEND", "").strip() == "edge"


def _edge_gaps():
    """Mac 端：Windows 报来的电脑微信掉线记录（运行中的 edge 后端的；机器人停着时直接读 edge_gaps.json）。"""
    from src.wechat.edge_backend import EdgeGaps
    if _edge_client is not None:
        return _edge_client.gaps
    return EdgeGaps(_db_path().resolve().parent / "edge_gaps.json")


def _wechat_gaps_state() -> dict:
    """GET /api/wechat-gaps: when PC WeChat was not online (the stretches no operator has marked
    看过了), for the alert on every page.  Windows: the bot's own record (src/wechat/presence.py), and
    while it runs the stretch going on; the Mac (edge): the ``wechat_gap`` events Windows reported."""
    from src.wechat.presence import gap_for_console, stored_state

    if _mac_side():
        return {"ok": True, "source": "mac", "open": None, "gaps": [gap_for_console(g) for g in _edge_gaps().unseen()]}
    state = _presence.console() if _presence is not None else stored_state(_db_path())
    return {"ok": True, "source": "windows", "open": gap_for_console(state["open"]) if state["open"] else None,
            "gaps": [gap_for_console(g) for g in state["gaps"]]}


def _wechat_gap_seen(data: dict) -> dict:
    """POST /api/wechat-gaps {"id", "action": "seen"}: an operator has looked (看过了); not shown again."""
    from src.wechat.presence import PresenceStore

    if str(data.get("action") or "seen") != "seen":
        return {"ok": False, "error": f"不认识的操作 {data.get('action')}"}
    try:
        gap_id = int(data.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "要给出是哪一段"}
    if _mac_side():
        done = _edge_gaps().mark_seen(gap_id, time.time())
    elif _presence is not None:
        done = _presence.mark_seen(gap_id)
    elif _db_path().exists():
        store = PresenceStore(str(_db_path()))
        try:
            done = store.mark_seen(gap_id, time.time())
        finally:
            store.close()
    else:
        done = False
    return {"ok": True} if done else {"ok": False, "error": "没有这一段，或者已经点过「看过了」"}


# The running PrmService (PRM_COPILOT=on), set by Bot.run(); None when the bot is
# stopped or the copilot is off.  /api/prm reads it.
_prm = None


def register_prm(service):
    """Register (or with None, forget) the running PRM copilot for /api/prm."""
    global _prm
    _prm = service


# The running new-friend scan (src/wechat/new_friends.py), set by Bot.run; None when the bot is
# stopped: /api/prm then reads the bot's database (DB_PATH) itself.
_new_friends = None


def register_new_friends(service):
    """Register (or with None, forget) the running new-friend scan for 登记达人's candidates."""
    global _new_friends
    _new_friends = service


def _recent_new_friends() -> list[dict]:
    """The friends found lately (newest first): from the running scan, else from the bot's database."""
    from src.wechat.new_friends import recent_new_friends

    try:
        if _new_friends is not None:
            return list(_new_friends.recent())
        return recent_new_friends(os.getenv("DB_PATH", "data/messages.db").strip() or "data/messages.db")
    except Exception:
        logger.warning("Could not read the new friends", exc_info=True)
        return []


def _prm_db_path() -> Path:
    return Path(os.getenv("PRM_DB", "data/prm.db").strip() or "data/prm.db")


def _prm_env_on(key: str) -> bool:
    return str(os.getenv(key, "")).strip().lower() in ("on", "true", "1", "yes", "y")


def _prm_reserved() -> set[str]:
    """The admin and the cc users: never creators."""
    return {w.strip() for w in [os.getenv("ADMIN_WXID", ""), *os.getenv("CLAUDE_RELAY_WXIDS", "").split(",")]
            if w.strip()}


def _prm_candidates(state: dict) -> list[dict]:
    """Who the register form offers, not creators yet: first the new friends of the last days
    (src/wechat/new_friends.py; ``new``, their greeting masked, how they were added), newest first,
    then the private chats WeChat lists (data/private_chat_names.json, written by the Windows
    backend while PRM_COPILOT is on) by name."""
    path = Path("data/private_chat_names.json")
    try:
        names = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        names = {}
    names = names if isinstance(names, dict) else {}
    taken = {p["wxid"] for p in state.get("parties", []) if p.get("active")} | _prm_reserved()
    fresh = []
    for f in _recent_new_friends():
        wxid = str(f.get("wxid") or "")
        if not wxid or wxid in taken or any(c["wxid"] == wxid for c in fresh):
            continue
        fresh.append({"wxid": wxid, "name": str(f.get("name") or names.get(wxid) or wxid), "new": True,
                      "verify_text": str(f.get("verify_text") or ""), "how": str(f.get("how") or "unknown"),
                      "how_label": str(f.get("how_label") or ""), "added_at": f.get("added_at"),
                      "remark": str(f.get("remark") or "")})
    listed = {c["wxid"] for c in fresh}
    out = [{"wxid": str(w), "name": str(n)} for w, n in names.items() if str(w) not in taken | listed]
    return (fresh + sorted(out, key=lambda c: c["name"]))[:300]


def _prm_state() -> dict:
    """GET /api/prm: creators, drafts waiting and recent ones (also when the bot is stopped)."""
    state = _prm_state_core()
    state["candidates"] = _prm_candidates(state)
    return state


def _prm_int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _prm_stopped_store():
    """The PRM database for a stopped bot (gates seeded), or None when there is none yet."""
    from src.prm.gates import DEFAULT_GATES
    from src.prm.store import PrmStore

    db = _prm_db_path()
    if not db.exists():
        return None
    store = PrmStore(db)
    store.seed_gates(DEFAULT_GATES)
    return store


def _prm_state_core() -> dict:
    from src.prm.service import snapshot as prm_snapshot

    settings = {"enabled": _prm_env_on("PRM_COPILOT"), "relay_on": _prm_env_on("CLAUDE_RELAY"),
                "private_on": _prm_env_on("PRIVATE_CHAT_ENABLED")}
    if _prm is not None:
        return {**_prm.snapshot(), **settings, "enabled": True}
    stopped = {"mac_online": False, "worker_kinds": [], "can_send": False, "can_send_files": False,
               "takeover_min": _prm_int_env("PRM_TAKEOVER_MIN", 120), "holding_min": _prm_int_env("PRM_HOLDING_MIN", 10),
               "retention_days": _prm_int_env("PRM_RETENTION_DAYS", 90),
               "catchup_hours": _prm_int_env("PRM_CATCHUP_HOURS", 12),
               "buyin_sync_min": _prm_int_env("PRM_BUYIN_SYNC_MIN", 0), **settings}
    store = _prm_stopped_store()
    if store is None:
        from src.prm.platforms import ACTIONS
        from src.prm.result import MESSAGE_KINDS
        from src.prm.store import MODES, SHIPMENT_STATES, STAGES
        return {"ok": True, "running": False, "parties": [], "pending": [], "recent": [], "counts": {}, "gates": [],
                "platform_alerts": [], "platform_actions": {k: v["label"] for k, v in ACTIONS.items()},
                "stages": STAGES, "kinds": MESSAGE_KINDS, "modes": MODES, "shipment_states": SHIPMENT_STATES,
                **stopped}
    try:
        return prm_snapshot(store, running=False, retention_days=stopped["retention_days"], extra=stopped)
    finally:
        store.close()


def _prm_deal(data: dict) -> dict:
    """POST /api/prm/deals {action: stage | adopt | mode | shipment | shipment_status, deal_id, ...}."""
    from src.prm.service import apply_deal_action

    action = str(data.get("action") or "")
    if _prm is not None:
        return _prm.deal_action(action, data)
    store = _prm_stopped_store()
    if store is None:
        return {"ok": False, "error": "还没有合作单"}
    try:
        return apply_deal_action(store, action, data, now=time.time(),
                                 takeover_sec=_prm_int_env("PRM_TAKEOVER_MIN", 120) * 60)
    finally:
        store.close()


def _prm_reveal(data: dict) -> dict:
    """POST /api/prm/reveal {party_id}: the full numbers; every look is written down."""
    from src.prm.service import reveal_numbers

    try:
        party_id = int(data.get("party_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "要给出达人编号"}
    if _prm is not None:
        return _prm.reveal(party_id)
    store = _prm_stopped_store()
    if store is None:
        return {"ok": False, "error": "没有找到这个达人"}
    try:
        return reveal_numbers(store, party_id, now=time.time())
    finally:
        store.close()


def _prm_parties(data: dict) -> dict:
    """POST /api/prm/parties: {"action": "add", "wxid", "name", "note"} | {"action": "enable"|"disable", "id"}."""
    from src.prm.service import check_wxid
    from src.prm.store import PartyError, PrmStore

    action = str(data.get("action") or "")
    try:
        if _prm is not None:
            if action == "add":
                party = _prm.register(data.get("wxid", ""), data.get("name", ""), data.get("note", ""))
                # catch_up: a new friend's messages since she was added are on their way to a draft
                return {"ok": True, "party": {"id": party["id"], "name": party["name"]},
                        "catch_up": bool(party.get("catch_up"))}
            if action in ("enable", "disable"):
                party = _prm.set_active(int(data.get("id")), action == "enable")
                return {"ok": True} if party else {"ok": False, "error": "没有找到这个达人"}
            return {"ok": False, "error": f"不认识的操作 {action}"}
        # The bot is stopped: the same checks, straight into the database.
        reserved = _prm_reserved()
        store = PrmStore(_prm_db_path())
        try:
            if action == "add":
                wxid = check_wxid(data.get("wxid", ""), reserved)
                name, note = str(data.get("name") or "").strip(), str(data.get("note") or "").strip()
                if not name or len(name) > 30:
                    return {"ok": False, "error": "称呼要填，最多 30 个字"}
                if len(note) > 200:
                    return {"ok": False, "error": "备注最多 200 个字"}
                party = store.add_party(wxid, name, note)
                return {"ok": True, "party": {"id": party["id"], "name": party["name"]}}
            if action in ("enable", "disable"):
                party = store.update_party(int(data.get("id")), active=action == "enable")
                return {"ok": True} if party else {"ok": False, "error": "没有找到这个达人"}
            return {"ok": False, "error": f"不认识的操作 {action}"}
        finally:
            store.close()
    except PartyError as e:
        return {"ok": False, "error": str(e)}
    except (TypeError, ValueError):
        return {"ok": False, "error": "要给出达人编号"}


def _prm_packet(data: dict) -> dict:
    """POST /api/prm/packets: {"id", "action": "send" | "edit" | "drop" | "wrong" | "execute" | "repreview"
    | "ack" | "copy" | "manual_sent", "text"}; a draft with a platform action is approved with "execute" (text: the
    reply as edited); a 抖音百应 draft is copied ("copy": the answer's ``text`` is the reply with the full numbers, each
    copy written down) and marked "manual_sent" once an operator sent it in 百应 by hand (the bot sends it nowhere),
    see PrmService.decide."""
    try:
        packet_id = int(data.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "要给出草稿编号"}
    if _prm is None:
        return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能处理草稿"}
    text = data.get("text")
    return _prm.decide(packet_id, str(data.get("action") or ""), None if text is None else str(text))


def _stop_bot():
    """Stop the running bot backend. Returns True if anything was stopped."""
    stopped = _bot_control.stop()
    update_status(running=False)
    if stopped:
        logger.info("Bot stopped via web API")
    return stopped


def _start_bot_in_thread():
    """Start the bot in a new daemon thread. Call from API handler."""
    if _bot_control.is_running():
        return {"ok": False, "error": "Bot is already running"}

    import sys
    from src.config import PROJECT_ROOT

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    def _run():
        try:
            from dotenv import load_dotenv
            from src.config import _env_path, _local_env_path, _load_non_empty_local_env
            if _env_path and _env_path.exists():
                load_dotenv(_env_path, override=True)
            if _local_env_path and _local_env_path.exists():
                _load_non_empty_local_env(_local_env_path)

            from src.config import load_config
            config = load_config()
            update_status(wechat_backend=config.wechat_backend, error="")
            from src.bot import Bot
            bot = Bot(config)
            # Bot.run() calls _register_backend() during init — no patch needed
            bot.run()
        except SystemExit:
            update_status(running=False)
        except Exception as e:
            update_status(running=False, error=str(e))
            logger.exception("Bot crashed during startup")
        finally:
            # Always clear the running flag so the user can restart
            # (bot.run() exits gracefully on errors like KEY_MISSING)
            _bot_control.mark_stopped()
            update_status(running=False)

    thread = threading.Thread(target=_run, daemon=True, name="bot-main")
    _bot_control.set_thread(thread)
    _bot_control.set_running()
    update_status(running=True)
    thread.start()
    return {"ok": True}


# Hostnames that mean "this machine".  The console has no login, so when it
# listens on loopback only requests that visibly come from this machine's own
# pages may use it: a Host that is not loopback is a DNS-rebinding attempt and
# an Origin that is not loopback is another website talking to the API.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Endpoints that change state (save the config, start/stop the bot, spend API
# credit, ...).  The handler below mostly ignores the HTTP method, so without
# this list a plain GET would run them -- and any web page can fire a GET at
# 127.0.0.1 with an <img> tag, without an Origin header the check above could
# look at.  The console itself only ever POSTs to these.
_POST_ONLY_PATHS = frozenset({
    "/api/config", "/api/config/import", "/api/start", "/api/stop",
    "/api/onboarding/reset", "/api/onboarding/step1", "/api/onboarding/step2",
    "/api/onboarding/step3",
    "/api/voice/download-model", "/api/wechat-data-dir/detect",
    "/api/influencer/contacts/action", "/api/influencer/inbox/action",
    "/api/relay/cancel", "/api/prm/parties", "/api/prm/packets", "/api/prm/deals", "/api/prm/reveal",
})


def _header_hostname(value: str) -> str | None:
    """Hostname of a ``Host`` header value ("127.0.0.1:7327", "[::1]:7327")."""
    try:
        return urlsplit("//" + value.strip()).hostname
    except ValueError:
        return None


def _origin_hostname(value: str) -> str | None:
    """Hostname of an ``Origin`` header value (None for "null" or garbage)."""
    try:
        return urlsplit(value.strip()).hostname
    except ValueError:
        return None


def _influencer_db_path() -> str:
    """达人模块的数据库：和群聊待办一样取 .env 里的 DB_PATH，默认 data/messages.db。"""
    from src.config import find_env_file
    db_path = "data/messages.db"
    env_path = find_env_file()
    if env_path and env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DB_PATH="):
                db_path = line.strip().split("=", 1)[1].strip()
                break
    return db_path


class _UIHandler(SimpleHTTPRequestHandler):
    """HTTP handler for static files and the JSON API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    # ── cross-origin protection ──────────────────────────────────────

    def _bound_to_loopback(self) -> bool:
        address = getattr(getattr(self, "server", None), "server_address", None)
        host = address[0] if isinstance(address, tuple) and address else None
        return not isinstance(host, str) or host in _LOOPBACK_HOSTS

    def _request_origin_ok(self) -> bool:
        """Reject DNS-rebinding and cross-site requests to a loopback console.

        Requests without an Origin header (the page's own navigation, curl)
        are fine; browsers always attach Origin to cross-site fetches/forms.
        """
        if not self._bound_to_loopback():
            return True          # the user deliberately exposed the console
        host = self.headers.get("Host")
        if host and _header_hostname(host) not in _LOOPBACK_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and _origin_hostname(origin) not in _LOOPBACK_HOSTS:
            return False
        return True

    def _forbid_cross_origin(self) -> None:
        body = json.dumps(
            {"ok": False, "error": "Forbidden: cross-origin request rejected"},
        ).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _method_not_allowed(self) -> None:
        self.send_response(405)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": "Method not allowed"}).encode())

    def _send_cors_headers(self, preflight: bool = False) -> None:
        """Echo an already-validated Origin; never answer with ``*``."""
        origin = self.headers.get("Origin")
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        if not self._request_origin_ok():
            self._forbid_cross_origin()
            return
        self.send_response(204)
        self._send_cors_headers(preflight=True)
        self.end_headers()

    def do_POST(self):
        if not self._request_origin_ok():
            self._forbid_cross_origin()
            return
        # Only delegate specific API paths; return 405 for unknown POST paths
        if self.path in ("/api/config", "/api/config/import", "/api/start", "/api/stop",
                         "/api/onboarding/reset",
                         "/api/onboarding/step1", "/api/onboarding/step2",
                         "/api/onboarding/step3",
                         "/api/advanced",
                         "/api/voice/download-model",
                         "/api/wechat-data-dir/detect",
                         "/api/influencer/contacts/action",
                         "/api/influencer/inbox/action",
                         "/api/relay/cancel",
                         "/api/prm/parties", "/api/prm/packets", "/api/prm/deals", "/api/prm/reveal",
                         "/api/wechat-gaps"):
            self.do_GET()
        else:
            self._method_not_allowed()

    def do_GET(self):
        if not self._request_origin_ok():
            self._forbid_cross_origin()
            return
        # do_POST delegates here, so ``command`` tells a real GET from a POST.
        if self.command != "POST" and self.path.split("?", 1)[0] in _POST_ONLY_PATHS:
            self._method_not_allowed()
            return
        self._handle_request()

    def _handle_influencer(self):
        """/api/influencer/*：控制台的「达人名单」页。具体处理在 src/influencer/console.py。"""
        from urllib.parse import parse_qs
        from src.influencer import console
        from src.influencer.store import InfluencerStore

        path, _, query = self.path.partition("?")
        if path == "/api/influencer/counts":
            # 左栏每 30 秒问一次：只读，不建表（不然原项目的浏览器测试会在真实数据库里建表）
            self.send_json(console.attention_counts(_influencer_db_path()))
            return
        try:
            body = {}
            if self.command == "POST":
                content_len = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_len) if content_len else b"{}")
            params = {k: v[0] if v else "" for k, v in parse_qs(query).items()}
            store = InfluencerStore(_influencer_db_path())
            result = console.handle(path, self.command, params, body, store)
        except Exception as e:
            logger.exception("Influencer API failed: %s", path)
            result = {"ok": False, "error": str(e)}
        if result is None:
            data = json.dumps({"ok": False, "error": "Not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_json(result)

    def _handle_advanced(self):
        """GET/POST /api/advanced: the allow-listed advanced switches.

        GET returns every group of ``advanced_config.GROUPS`` with its current
        value (secrets masked) and the keys a machine-local .env.local
        overrides. POST takes ``{"values": {KEY: value}}``, checks each value
        and then the merged result with _validate_config, and writes only the
        posted keys to the shared .env and os.environ (like /api/config, so a
        stop/start from the console picks them up). A masked secret keeps the
        stored one. Unknown keys and invalid values are refused, nothing is
        written then.
        """
        from src.config import find_local_env_file
        from src.web import advanced_config as adv

        if self.command != "POST":
            local = _read_env_values(find_local_env_file())
            self.send_json({
                "ok": True,
                "groups": adv.describe(_read_effective_env_values(), _mask_key),
                "local_overrides": sorted(k for k in adv.FIELDS if str(local.get(k, "")).strip()),
            })
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b"{}"
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"ok": False, "error": "请求不是有效的 JSON"})
            return
        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict) or not values:
            self.send_json({"ok": False, "error": "没有要保存的设置"})
            return
        updates = {
            k: v for k, v in values.items()
            if not (k in adv.SECRET_KEYS and _is_masked_secret(v))
        }
        errors = adv.validate_updates(updates, _read_effective_env_values())
        if errors:
            self.send_json({"ok": False, "error": "有设置不合法，没有保存", "errors": errors})
            return
        written = {
            k: str(v).strip() if k in adv.SECRET_KEYS else adv.normalize(k, v)
            for k, v in updates.items()
        }
        try:
            if written:
                _write_env_updates(_find_or_create_env(), written)
                for k, v in written.items():
                    os.environ[k] = v
        except Exception as e:
            logger.exception("Failed to save advanced settings")
            self.send_json({"ok": False, "error": str(e)})
            return
        self.send_json({"ok": True, "saved": sorted(written), "requires_restart": True})

    def _send_decisions(self):
        """GET /api/decisions?limit=50&outcome=silent&chat_id=...&message_id=...

        The newest decisions of the decision trace, each translated into
        plain words (src/decision_explain.py).  Read-only; a missing trace
        file is an empty list.
        """
        from urllib.parse import parse_qs
        from src import decision_explain

        query = parse_qs(urlsplit(self.path).query)

        def one(name: str, default: str = "") -> str:
            return (query.get(name) or [default])[0]

        try:
            limit = int(one("limit", "50"))
        except ValueError:
            limit = 50
        raw = _read_effective_env_values()
        path = Path(raw.get("DECISION_TRACE_PATH") or "data/decision_trace.jsonl")
        try:
            items = decision_explain.recent(
                path, limit=limit, outcome=one("outcome"), chat_id=one("chat_id"),
                message_id=one("message_id"),
            )
        except Exception as exc:                      # a damaged trace must not drop the connection
            logger.exception("Reading the decision trace failed")
            self.send_json({"ok": False, "error": f"cannot read the decision trace: {type(exc).__name__}"})
            return
        self.send_json({"ok": True, "decisions": items})

    def _handle_request(self):
        # WebSocket support is intentionally disabled. Status is available
        # through GET /api/status, which keeps the local server HTTP-only.
        if self.path == "/ws":
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": False,
                "error": "WebSocket support is disabled; use /api/status",
            }).encode())
            return

        # ── API: Claude relay — state, jobs, cancel ────────────────────
        if self.path.split("?", 1)[0] == "/api/relay":
            try:
                self.send_json(_relay_snapshot())
            except Exception as e:
                logger.exception("Reading the relay state failed")
                self.send_json({"ok": False, "error": str(e)})
            return
        if self.path == "/api/relay/cancel":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                job_id = int(json.loads(body).get("id"))
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                self.send_json({"ok": False, "error": "要给出任务编号"})
                return
            if _relay is None:
                self.send_json({"ok": False, "error": "机器人没有运行，中转没有开着"})
                return
            outcome = _relay.cancel_from_console(job_id)
            if outcome == "missing":
                self.send_json({"ok": False, "error": f"没有找到 #{job_id}"})
            else:
                self.send_json({"ok": True, "outcome": outcome})
            return

        if self.path.split("?", 1)[0] == "/api/edge":
            try:
                self.send_json(_edge_state())
            except Exception as e:
                logger.exception("Reading the edge state failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: when PC WeChat was not online (GET: the alert; POST: 看过了) ──
        if self.path.split("?", 1)[0] == "/api/wechat-gaps":
            try:
                if self.command == "POST":
                    content_len = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(content_len) if content_len else b"{}"
                    try:
                        data = json.loads(body)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        data = None
                    self.send_json(_wechat_gap_seen(data) if isinstance(data, dict)
                                   else {"ok": False, "error": "请求不是有效的 JSON"})
                else:
                    self.send_json(_wechat_gaps_state())
            except Exception as e:
                logger.exception("The WeChat gaps could not be read or marked")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: PRM copilot — creators, drafts, decisions ─────────────
        if self.path.split("?", 1)[0] == "/api/prm":
            try:
                self.send_json(_prm_state())
            except Exception as e:
                logger.exception("Reading the PRM state failed")
                self.send_json({"ok": False, "error": str(e)})
            return
        if self.path in ("/api/prm/parties", "/api/prm/packets", "/api/prm/deals", "/api/prm/reveal"):
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_json({"ok": False, "error": "请求不是有效的 JSON"})
                return
            if not isinstance(data, dict):
                self.send_json({"ok": False, "error": "请求不是有效的 JSON"})
                return
            handler = {"/api/prm/parties": _prm_parties, "/api/prm/packets": _prm_packet,
                       "/api/prm/deals": _prm_deal, "/api/prm/reveal": _prm_reveal}[self.path]
            try:
                self.send_json(handler(data))
            except Exception as e:
                logger.exception("PRM console action failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: why did the bot (not) answer? (read-only) ─────────────
        if self.path.split("?", 1)[0] == "/api/decisions":
            self._send_decisions()
            return

        # ── API: Start bot ────────────────────────────────────────────
        if self.path == "/api/start":
            if _bot_control.is_running():
                self.send_json({"ok": True, "already_running": True})
            else:
                result = _start_bot_in_thread()
                self.send_json(result)
            return

        # ── API: Stop bot ─────────────────────────────────────────────
        if self.path == "/api/stop":
            _stop_bot()
            self.send_json({"ok": True})
            return

        # ── API: Load config ───────────────────────────────────────────
        if self.path == "/api/load-config":
            from src.config import _decode_wechat_groups  # noqa: F811 - needed before use (scoping)
            raw = _read_effective_env_values()
            config_data = {
                "bot_display_name": raw.get("BOT_DISPLAY_NAME", ""),
                "wechat_backend": raw.get("WECHAT_BACKEND", "wcdb"),
                "wechat_groups": _decode_wechat_groups(raw.get("WECHAT_GROUPS", "*")),
                # The same names as a list ([] = all); a name may contain a comma.
                "wechat_groups_list": _decode_name_list(raw.get("WECHAT_GROUPS", "*")),
                "private_chat_enabled": raw.get("PRIVATE_CHAT_ENABLED", "false").lower() == "true",
                "private_chats": _decode_wechat_groups(raw.get("PRIVATE_CHATS", "*")),
                "private_chats_list": _decode_name_list(raw.get("PRIVATE_CHATS", "*")),
                "message_burst_enabled": raw.get("MESSAGE_BURST_ENABLED", "true").lower() == "true",
                "message_burst_window_sec": float(raw.get("MESSAGE_BURST_WINDOW_SEC", "2.2")),
                "message_burst_max_messages": int(raw.get("MESSAGE_BURST_MAX_MESSAGES", "5")),
                "message_burst_max_chars": int(raw.get("MESSAGE_BURST_MAX_CHARS", "200")),
                "log_level": raw.get("LOG_LEVEL", "INFO"),
                "wechat_data_dir": raw.get("WECHAT_DATA_DIR", ""),
                "voice_asr_enabled": raw.get("VOICE_ASR_ENABLED", "false").lower() == "true",
                "voice_asr_backend": raw.get("VOICE_ASR_BACKEND", "local_whisper"),
                "voice_asr_language": raw.get("VOICE_ASR_LANGUAGE", "zh"),
                "voice_openai_api_key": _mask_key(raw.get("VOICE_OPENAI_API_KEY", "")),
                "voice_openai_base_url": raw.get("VOICE_OPENAI_BASE_URL", ""),
                "voice_local_model": raw.get("VOICE_LOCAL_MODEL", "small"),
            }
            self.send_json({
                "ok": True,
                "config": config_data,
                "detected_data_dir": _detect_default_data_dir(),
            })
            return

        # ── API: Export config ───────────────────────────────────────
        if self.path == "/api/config/export":
            from datetime import date as _dt_date
            try:
                env_path = _find_or_create_env()
                raw = {}
                if env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            raw[k.strip()] = v.strip()
                export_data = {
                    "bot_display_name": raw.get("BOT_DISPLAY_NAME", ""),
                    "wechat_backend": raw.get("WECHAT_BACKEND", "wcdb"),
                    "wechat_groups": raw.get("WECHAT_GROUPS", "*"),
                    "private_chat_enabled": raw.get("PRIVATE_CHAT_ENABLED", "false").lower() == "true",
                    "private_chats": raw.get("PRIVATE_CHATS", "*"),
                    "message_burst_enabled": raw.get("MESSAGE_BURST_ENABLED", "true").lower() == "true",
                    "message_burst_window_sec": float(raw.get("MESSAGE_BURST_WINDOW_SEC", "2.2")),
                    "message_burst_max_messages": int(raw.get("MESSAGE_BURST_MAX_MESSAGES", "5")),
                    "message_burst_max_chars": int(raw.get("MESSAGE_BURST_MAX_CHARS", "200")),
                    "log_level": raw.get("LOG_LEVEL", "INFO"),
                    "wechat_data_dir": raw.get("WECHAT_DATA_DIR", ""),
                }
                filename = f"webot-config-{_dt_date.today().isoformat()}.json"
                body = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                logger.exception("Failed to export config")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Save config ──────────────────────────────────────────
        if self.path == "/api/config":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                config = json.loads(body)
                env_path = _find_or_create_env()
                if env_path.exists():
                    lines = env_path.read_text(encoding="utf-8").splitlines()
                else:
                    lines = []
                existing_values = _env_values_from_lines(lines)
                new_lines = []
                updates = {
                    "BOT_DISPLAY_NAME": config.get("bot_display_name"),
                    "WECHAT_BACKEND": config.get("wechat_backend"),
                    "WECHAT_GROUPS": config.get("wechat_groups") or "*",
                    "PRIVATE_CHAT_ENABLED": str(config.get("private_chat_enabled", False)).lower(),
                    "PRIVATE_CHATS": config.get("private_chats") or "*",
                    "MESSAGE_BURST_ENABLED": str(config.get("message_burst_enabled", True)).lower(),
                    "MESSAGE_BURST_WINDOW_SEC": str(config.get("message_burst_window_sec", 2.2)),
                    "MESSAGE_BURST_MAX_MESSAGES": str(config.get("message_burst_max_messages", 5)),
                    "MESSAGE_BURST_MAX_CHARS": str(config.get("message_burst_max_chars", 200)),
                    "LOG_LEVEL": config.get("log_level"),
                    "WECHAT_DATA_DIR": config.get("wechat_data_dir"),
                    "VOICE_ASR_ENABLED": str(config.get("voice_asr_enabled", False)).lower(),
                    "VOICE_ASR_BACKEND": config.get("voice_asr_backend", "local_whisper"),
                    "VOICE_ASR_LANGUAGE": config.get("voice_asr_language", "zh"),
                    "VOICE_OPENAI_API_KEY": config.get("voice_openai_api_key", ""),
                    "VOICE_OPENAI_BASE_URL": config.get("voice_openai_base_url", ""),
                    "VOICE_LOCAL_MODEL": config.get("voice_local_model", "small"),
                }
                _preserve_masked_secret_updates(updates, existing_values)
                seen = set()
                for line in lines:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        key = stripped.split("=", 1)[0].strip()
                        if key in updates and updates[key] is not None:
                            new_lines.append(f"{key}={updates[key]}")
                            seen.add(key)
                            continue
                    new_lines.append(line)
                for key, val in updates.items():
                    if key not in seen and val is not None:
                        new_lines.append(f"{key}={val}")
                # Atomic write: temp file then os.replace
                tmp_path = env_path.with_suffix(".tmp")
                tmp_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                os.replace(tmp_path, env_path)
                for key, val in updates.items():
                    if val is not None:
                        os.environ[key] = str(val)
                self.send_json({
                    "ok": True,
                    "saved": list(seen),
                    "requires_restart": True,
                })
            except Exception as e:
                logger.exception("Failed to save config")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Import config ─────────────────────────────────────────
        if self.path == "/api/config/import":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                config = json.loads(body)
                # Basic validation: must look like a webot config export
                expected_keys = ['wechat_backend', 'bot_display_name', 'wechat_groups']
                has_keys = any(k in config for k in expected_keys)
                if not has_keys:
                    raise ValueError("无效的配置文件格式：缺少必需字段")
                env_path = _find_or_create_env()
                if env_path.exists():
                    lines = env_path.read_text(encoding="utf-8").splitlines()
                else:
                    lines = []
                existing_values = _env_values_from_lines(lines)
                updates = {
                    "BOT_DISPLAY_NAME": config.get("bot_display_name"),
                    "WECHAT_BACKEND": config.get("wechat_backend"),
                    "WECHAT_GROUPS": config.get("wechat_groups") or "*",
                    "PRIVATE_CHAT_ENABLED": str(config.get("private_chat_enabled", False)).lower(),
                    "PRIVATE_CHATS": config.get("private_chats") or "*",
                    "MESSAGE_BURST_ENABLED": str(config.get("message_burst_enabled", True)).lower(),
                    "MESSAGE_BURST_WINDOW_SEC": str(config.get("message_burst_window_sec", 2.2)),
                    "MESSAGE_BURST_MAX_MESSAGES": str(config.get("message_burst_max_messages", 5)),
                    "MESSAGE_BURST_MAX_CHARS": str(config.get("message_burst_max_chars", 200)),
                    "LOG_LEVEL": config.get("log_level"),
                    "WECHAT_DATA_DIR": config.get("wechat_data_dir"),
                    "VOICE_ASR_ENABLED": str(config.get("voice_asr_enabled", False)).lower(),
                    "VOICE_ASR_BACKEND": config.get("voice_asr_backend", "local_whisper"),
                    "VOICE_ASR_LANGUAGE": config.get("voice_asr_language", "zh"),
                    "VOICE_OPENAI_API_KEY": config.get("voice_openai_api_key", ""),
                    "VOICE_OPENAI_BASE_URL": config.get("voice_openai_base_url", ""),
                    "VOICE_LOCAL_MODEL": config.get("voice_local_model", "small"),
                }
                _preserve_masked_secret_updates(updates, existing_values)
                new_lines = []
                seen = set()
                for line in lines:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        key = stripped.split("=", 1)[0].strip()
                        if key in updates and updates[key] is not None:
                            new_lines.append(f"{key}={updates[key]}")
                            seen.add(key)
                            continue
                    new_lines.append(line)
                for key, val in updates.items():
                    if key not in seen and val is not None:
                        new_lines.append(f"{key}={val}")
                # Atomic write
                tmp_path = env_path.with_suffix(".tmp")
                tmp_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                os.replace(tmp_path, env_path)
                # Update in-process environment
                for key, val in updates.items():
                    if val is not None:
                        os.environ[key] = str(val)
                self.send_json({
                    "ok": True,
                    "imported": list(seen),
                    "requires_restart": True,
                })
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)})
            except Exception as e:
                logger.exception("Failed to import config")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Known groups (the group picker of 微信连接) ────────────────
        if self.path == "/api/groups":
            try:
                from src.config import find_env_file, _decode_wechat_groups
                env_path = find_env_file()
                import sqlite3

                # Resolve group names same way as wcdb_backend: read env, match sessions
                groups_raw = "*"
                if env_path and env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        if line.strip().startswith("WECHAT_GROUPS="):
                            groups_raw = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            break
                groups_raw = _decode_wechat_groups(groups_raw)

                db_path = "data/messages.db"
                if env_path and env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        if line.strip().startswith("DB_PATH="):
                            db_path = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                            break

                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row

                # ── Load persisted chat_id -> group info ─────────────────
                # Written by WcdbBackend._save_group_names() when the bot
                # resolves group names from WeChat's session DB via WCDB DLL.
                # New format: {chat_id: {name, member_count}}
                # Old format: {chat_id: "name"} (backward compatible)
                group_names_path = Path("data/group_names.json")
                group_info: dict[str, dict] = {}
                if group_names_path.exists():
                    try:
                        raw = json.loads(group_names_path.read_text(encoding="utf-8"))
                        for chat_id, val in raw.items():
                            if isinstance(val, dict):
                                group_info[chat_id] = {
                                    "name": val.get("name", chat_id),
                                    "member_count": int(val.get("member_count", 0)),
                                }
                            else:
                                # Old format: plain string → name only, no member count
                                group_info[chat_id] = {
                                    "name": str(val),
                                    "member_count": 0,
                                }
                    except (json.JSONDecodeError, OSError):
                        pass
                # ──────────────────────────────────────────────────────────

                groups = []

                # Primary source: group_names.json (from WCDB session scan)
                if group_info:
                    for chat_id, info in group_info.items():
                        mc = info["member_count"]
                        if not mc:
                            # Fall back to message-sender count if WCDB didn't provide one
                            cnt_row = conn.execute(
                                "SELECT COUNT(DISTINCT sender_id) FROM messages WHERE chat_id=?",
                                (chat_id,),
                            ).fetchone()
                            mc = cnt_row[0] if cnt_row else 0
                        groups.append({
                            "chat_id": chat_id,
                            "group_name": info["name"],
                            "member_count": mc,
                        })

                # Secondary source: messages table (groups not yet in group_names.json)
                if _messages_table_exists(conn):
                    existing_ids = set(group_info.keys())
                    rows = conn.execute(
                        "SELECT DISTINCT chat_id FROM messages WHERE chat_id LIKE '%@chatroom%' ORDER BY chat_id"
                    ).fetchall()
                    for row in rows:
                        chat_id = row["chat_id"]
                        if chat_id in existing_ids:
                            continue
                        cnt_row = conn.execute(
                            "SELECT COUNT(DISTINCT sender_id) FROM messages WHERE chat_id=?",
                            (chat_id,),
                        ).fetchone()
                        groups.append({
                            "chat_id": chat_id,
                            "group_name": chat_id,
                            "member_count": cnt_row[0] if cnt_row else 0,
                        })

                conn.close()
                self.send_json({"ok": True, "groups": groups})
            except Exception as e:
                logger.exception("Failed to list the groups")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Advanced switches (allow-list: src/web/advanced_config.py) ──
        if self.path == "/api/advanced":
            self._handle_advanced()
            return

        # ── API: 达人名单（src/influencer/，说明见 handoff.md） ──────────
        if self.path.startswith("/api/influencer/"):
            self._handle_influencer()
            return

        # ── API: Get status ───────────────────────────────────────────
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(_status.snapshot(), ensure_ascii=False).encode())
            return

        # ── API: Get logs ────────────────────────────────────────────
        if self.path == "/api/logs":
            self.send_json(_read_recent_logs())
            return

        # ── API: macOS WeChat automation diagnostics ─────────────────
        if self.path == "/api/macos/diagnose":
            self.send_json({
                "ok": True,
                "diagnostics": _macos_wechat_diagnostics(),
            })
            return

        # ── API: Onboarding status ────────────────────────────────────
        if self.path == "/api/onboarding/status":
            from src.config import is_onboarding_done
            done = is_onboarding_done()
            with _onboarding_lock:
                steps = {
                    "step1": _onboarding_data["step1_done"],
                    "step2": _onboarding_data["step2_done"],
                    "step3": _onboarding_data["step3_done"],
                }
            self.send_json({"ok": True, "onboarding_done": done, "steps": steps})
            return

        # ── API: Onboarding diagnostics check ─────────────────────────
        if self.path == "/api/onboarding/diagnose":
            import sys

            # 1. Python check
            python_ok = sys.version_info >= (3, 10)
            python_val = f"Python {sys.version.split()[0]}"

            # 2. Requirements check
            req_report = _platform_dependency_report()

            # 3. WeChat PID check
            wx_report = _platform_wechat_report()

            # 4. .env check
            # In frozen mode, __file__ is inside the read-only _MEIPASS
            # extraction directory. Use PROJECT_ROOT from config.py which
            # correctly resolves to the EXE directory when frozen.
            from src.config import PROJECT_ROOT, find_env_file
            project_root = PROJECT_ROOT
            env_path = find_env_file() or (project_root / ".env")
            env_ok = env_path.exists()
            env_val = "配置文件已存在" if env_ok else "配置文件尚未创建"

            # 5. DB permissions check
            data_dir = project_root / "data"
            db_perm_ok = True
            db_perm_err = None
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                test_file = data_dir / ".write_test"
                test_file.write_text("test", encoding="utf-8")
                test_file.unlink()
            except Exception as e:
                db_perm_ok = False
                db_perm_err = str(e)

            # Check read permission to WeChat db path if it's set/detected
            db_path = None
            with _onboarding_lock:
                db_path = _onboarding_data.get("db_path")
            if not db_path:
                _, detected_db = _detect_wxid_and_db_path()
                if detected_db:
                    db_path = detected_db

            if db_path:
                db_path_obj = Path(db_path)
                if db_path_obj.exists():
                    try:
                        with open(db_path_obj, "rb") as f:
                            f.read(100)
                    except Exception as e:
                        db_perm_ok = False
                        db_perm_err = f"微信数据库读取失败: {e}"

            db_perm_val = "数据库读写权限正常" if db_perm_ok else f"数据库权限错误: {db_perm_err}"

            self.send_json({
                "ok": True,
                "diagnostics": {
                    "python": {"ok": python_ok, "value": python_val, "error": None},
                    "requirements": req_report,
                    "wechat": wx_report,
                    "env": {"ok": env_ok, "value": env_val, "error": None},
                    "db": {"ok": db_perm_ok, "value": db_perm_val, "error": db_perm_err}
                }
            })
            return

        # ── API: Onboarding step 1 - start extraction (async) ─────────
        if self.path == "/api/onboarding/step1":
            with _step1_lock:
                if _step1_state["running"]:
                    self.send_json({"ok": False, "phase": "busy", "message": "正在提取中..."})
                    return
                _step1_state["running"] = True
                _step1_state["phase"] = "idle"
                _step1_state["message"] = ""
                _step1_state["result"] = None

            # Start background thread
            t = threading.Thread(target=_run_step1_extraction, daemon=True)
            t.start()
            with _step1_lock:
                _step1_thread = t

            self.send_json({"ok": True, "phase": "started", "message": "提取已启动"})
            return

        # ── API: Onboarding step 1 - poll status ──────────────────────
        if self.path == "/api/onboarding/step1-status":
            with _step1_lock:
                s = dict(_step1_state)
            self.send_json(s)
            return

        # ── API: Onboarding step 2 - WeChat identity ──────────────────
        if self.path == "/api/onboarding/step2":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                # A key typed in by hand (when automatic extraction is not
                # possible). Stored like an extracted one: in the machine
                # .env right away, and in WCDB_KEY when step 3 writes .env.
                manual_key = str(data.get("wcdb_key") or "").strip()
                if manual_key and not _WCDB_KEY_RE.fullmatch(manual_key):
                    self.send_json({"ok": False, "error": "微信密钥应为 64 位十六进制字符（0-9、a-f）"})
                    return
                if manual_key:
                    _set_env_key(_find_or_create_machine_env(), "WCDB_KEY", manual_key)
                    os.environ["WCDB_KEY"] = manual_key
                with _onboarding_lock:
                    _onboarding_data["step2_done"] = True
                    if manual_key:
                        _onboarding_data["step1_done"] = True
                        _onboarding_data["key"] = manual_key
                    from src.config import _sanitize_display_name
                    _onboarding_data["bot_display_name"] = _sanitize_display_name(
                        data.get("bot_display_name", "群聊小助手")
                    )
                    _onboarding_data["wechat_groups"] = data.get("wechat_groups", "*")
                    _onboarding_data["wechat_backend"] = data.get("wechat_backend", "wcdb")
                    if data.get("wxid"):
                        _onboarding_data["wxid"] = data["wxid"]
                    if data.get("db_path"):
                        _onboarding_data["db_path"] = data["db_path"]
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Onboarding step 3 - private chats + write .env ───────
        if self.path == "/api/onboarding/step3":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                with _onboarding_lock:
                    _onboarding_data["step3_done"] = True
                    _onboarding_data["message_burst_enabled"] = data.get("message_burst_enabled", True)
                    if "private_chat_enabled" in data:
                        _onboarding_data["private_chat_enabled"] = bool(data["private_chat_enabled"])
                    if data.get("private_chats"):
                        _onboarding_data["private_chats"] = str(data["private_chats"])

                # Write all accumulated data to .env
                env_path = _find_or_create_env()
                _write_onboarding_to_env(env_path)
                self.send_json({"ok": True})
            except Exception as e:
                logger.exception("Onboarding step3 failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Reset onboarding → allow re-extraction ─────────────
        if self.path == "/api/onboarding/reset":
            # 1. Reset file-based state
            env_path = _find_or_create_env()
            _set_env_key(env_path, "ONBOARDING_DONE", "false")
            _set_env_key(_find_or_create_machine_env(), "WCDB_KEY", "")
            # 2. Reset in-memory state so a fresh extraction can start
            with _onboarding_lock:
                for k in _onboarding_data:
                    if isinstance(_onboarding_data[k], bool):
                        _onboarding_data[k] = False
                    elif isinstance(_onboarding_data[k], str):
                        _onboarding_data[k] = ""
            with _step1_lock:
                _step1_state["running"] = False
                _step1_state["phase"] = "idle"
                _step1_state["message"] = ""
                _step1_state["result"] = None
            self.send_json({"ok": True, "message": "请退出微信，然后点击「重新获取密钥」"})
            return

        # ── API: Browse filesystem directories ──────────────────────
        if self.path.startswith("/api/browse"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            dir_path = params.get("path", [""])[0].strip()
            if not dir_path:
                # No path given — list drives on Windows, home on others
                import platform
                if platform.system() == "Windows":
                    import string
                    drives = []
                    for letter in string.ascii_uppercase:
                        p = Path(f"{letter}:\\")
                        if p.exists():
                            drives.append({"name": f"{letter}:", "path": f"{letter}:\\", "is_dir": True})
                    self.send_json({"ok": True, "entries": drives, "current_path": ""})
                else:
                    home = Path.home()
                    entries = _list_dir_entries(home)
                    self.send_json({"ok": True, "entries": entries, "current_path": str(home)})
                return

            target = Path(dir_path)
            if not target.exists():
                self.send_json({"ok": False, "error": f"路径不存在: {dir_path}"})
                return
            if not target.is_dir():
                self.send_json({"ok": False, "error": "请选择一个目录"})
                return

            try:
                entries = _list_dir_entries(target)
                self.send_json({"ok": True, "entries": entries, "current_path": str(target)})
            except PermissionError:
                self.send_json({"ok": False, "error": "没有权限访问该目录"})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Detect WeChat data in a custom directory ──────────
        if self.path == "/api/wechat-data-dir/detect":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                dir_path = (data.get("path") or "").strip()
                if not dir_path:
                    self.send_json({"ok": False, "error": "请提供目录路径"})
                    return
                target = Path(dir_path)
                if not target.exists() or not target.is_dir():
                    self.send_json({"ok": False, "error": f"目录不存在: {dir_path}"})
                    return

                # Scan for wxid_* directories
                wxid_dirs = sorted(
                    [d for d in target.iterdir() if d.is_dir() and d.name.startswith("wxid_")],
                    key=lambda d: d.stat().st_mtime, reverse=True,
                )
                accounts = []
                for wxid_dir in wxid_dirs:
                    session_db = wxid_dir / "db_storage" / "session" / "session.db"
                    accounts.append({
                        "wxid": wxid_dir.name,
                        "has_session_db": session_db.exists(),
                        "db_path": str(session_db) if session_db.exists() else "",
                    })

                if accounts:
                    self.send_json({
                        "ok": True,
                        "found": True,
                        "accounts": accounts,
                        "message": f"找到 {len(accounts)} 个微信账号",
                    })
                else:
                    self.send_json({
                        "ok": True,
                        "found": False,
                        "accounts": [],
                        "message": f"在 {dir_path} 中未找到 wxid_* 目录。请确认路径正确。",
                    })
            except Exception as e:
                logger.exception("Failed to detect WeChat data dir")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Voice model status ────────────────────────────────────
        if self.path.startswith("/api/voice/model-status"):
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                model = qs.get("model", ["small"])[0]

                # Check HuggingFace cache
                cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
                model_dir = cache_dir / f"models--Systran--faster-whisper-{model}"
                downloaded = False
                if model_dir.exists():
                    snapshots = model_dir / "snapshots"
                    if snapshots.exists() and any(snapshots.iterdir()):
                        downloaded = True
                    else:
                        blobs = model_dir / "blobs"
                        if blobs.exists() and any(blobs.iterdir()):
                            downloaded = True

                dl = _voice_downloads.get(model)
                self.send_json({
                    "ok": True,
                    "downloaded": downloaded,
                    "phase": dl.get("phase", "") if dl else "",  # "downloading" | "installing"
                    "pct": dl.get("pct", 0) if dl else 0,
                    "error": dl.get("error", "") if dl else "",
                    "model": model,
                })
            except Exception as e:
                logger.exception("Voice model-status failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── API: Trigger voice model download ──────────────────────────
        if self.path == "/api/voice/download-model":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b"{}"
            try:
                data = json.loads(body)
                model = data.get("model", "small")

                _voice_downloads[model] = {"phase": "downloading", "pct": 0, "error": ""}

                def _run():
                    state = _voice_downloads.setdefault(model, {})
                    try:
                        # ── Phase 1: download via huggingface_hub ──
                        state["phase"] = "downloading"
                        state["pct"] = 0

                        from huggingface_hub import snapshot_download
                        cache = str(Path.home() / ".cache" / "huggingface")
                        repo_id = f"Systran/faster-whisper-{model}"

                        snapshot_download(
                            repo_id=repo_id,
                            cache_dir=cache,
                        )
                        state["pct"] = 90

                        # ── Phase 2: load model into memory ──────
                        state["phase"] = "installing"
                        state["pct"] = 95
                        from faster_whisper import WhisperModel
                        WhisperModel(
                            model, device="cpu", compute_type="int8",
                            download_root=cache,
                        )
                        state["phase"] = "done"
                        state["pct"] = 100
                        state["error"] = ""
                        logger.info("Voice model '%s' ready", model)
                    except Exception as exc:
                        state["phase"] = "error"
                        state["error"] = str(exc).split("\n")[0][:300]
                        logger.exception("Voice model download/install failed")

                t = threading.Thread(target=_run, daemon=True)
                t.start()
                self.send_json({"ok": True, "model": model, "message": "下载已在后台启动"})
            except Exception as e:
                _voice_downloads.pop(model, None)
                logger.exception("Voice download-model failed")
                self.send_json({"ok": False, "error": str(e)})
            return

        # ── SPA fallback: serve index.html for unknown paths ──────────
        if self.command != "GET" and self.command != "HEAD":
            self.send_response(405)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "Method not allowed"}).encode())
            return

        path = self.translate_path(self.path)
        if not Path(path).exists():
            self.path = "/index.html"

        super().do_GET()

    def log_message(self, format, *args):
        """Log HTTP errors but suppress normal access logs."""
        if args and any(
            code in str(args).lower()
            for code in ["error", "exception", "400", "401", "403", "404", "405", "500"]
        ):
            logger.warning("HTTP %s", format % args)

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())


def _run_server(host, port):
    """Run the HTTP server (blocking, called in daemon thread)."""
    # Enable SO_REUSEADDR so a rapid restart doesn't fail with "address in use"
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((host, port), _UIHandler)
    except OSError as e:
        logger.error("Failed to bind web server on %s:%s: %s", host, port, e)
        update_status(running=False, error=f"端口 {port} 被占用或无权绑定: {e}")
        return
    server.daemon_threads = True  # HTTP handlers won't block exit
    logger.info("Web UI: http://%s:%s", host, port)
    try:
        server.serve_forever()
    except Exception as e:
        logger.error("Web server crashed: %s", e)


def start_web_server(host="127.0.0.1", port=7327):
    """Start the web UI in a daemon thread (idempotent)."""
    if not _server_guard.try_start():
        logger.debug("Web server already running, skipping duplicate start")
        return None

    if not UI_DIR.exists():
        logger.warning("UI not built. Run: cd ui && npm run build")
        return None

    thread = threading.Thread(
        target=_run_server, args=(host, port),
        daemon=True, name="web-ui-server",
    )
    thread.start()
    return thread
