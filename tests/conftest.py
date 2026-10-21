"""Shared pytest setup.

Keeps the suite runnable on non-Windows development machines:

* the repository root is put on ``sys.path`` so ``pytest tests`` works the same
  as ``python -m pytest tests``;
* the pywin32 modules that ``src.wechat.window_controller`` imports at module
  level are replaced by inert stand-ins, so the pure-Python logic of the WCDB
  backend can be unit-tested on macOS/Linux.  Nothing here changes behaviour on
  Windows, where the real modules are used;
* the browser-based functional tests are skipped when Playwright is missing;
* tests that write cwd-relative runtime files (``data/messages.db``,
  ``data/group_names.json`` ...) run inside a temporary directory, so a test
  run never touches a real bot's data folder;
* a test that creates or rewrites the project's ``.env`` fails (see below).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if sys.platform != "win32":
    for _name in ("win32gui", "win32con", "win32clipboard", "win32process"):
        if importlib.util.find_spec(_name) is None:
            sys.modules.setdefault(_name, MagicMock(name=_name))

collect_ignore = []
if importlib.util.find_spec("playwright") is None:
    collect_ignore.append("test_functional.py")

_ISOLATE_CWD = {"test_feishu_export.py", "test_wcdb_backend.py"}


@pytest.fixture(autouse=True)
def _isolate_runtime_files(request, tmp_path, monkeypatch):
    """Run tests that create ``data/*`` files in a throw-away working dir."""
    if Path(str(request.node.fspath)).name in _ISOLATE_CWD:
        (tmp_path / "data").mkdir()
        monkeypatch.chdir(tmp_path)
    yield


# BotConfig enables the decision trace / model-call log by default, which would
# make every router built with a stock config write data/*.jsonl into the
# working directory.  Only the tests that exercise tracing opt back in.
_TRACE_ENABLED_TESTS = {"test_decision_trace.py", "test_router_trace_hooks.py"}


@pytest.fixture(autouse=True)
def _no_trace_files_unless_testing_trace(request, monkeypatch):
    if Path(str(request.node.fspath)).name in _TRACE_ENABLED_TESTS:
        yield
        return
    from src import decision_trace as trace

    monkeypatch.setattr(
        trace.DecisionTracer, "from_config",
        classmethod(lambda cls, config, **kwargs: trace.NullTracer()),
    )
    yield


# pytest puts the id of the running test into the PYTEST_CURRENT_TEST
# environment variable, and Windows refuses a variable longer than 32767
# characters: a test parametrized with a huge argument (its id contains the
# argument) errors there before it starts.  Fail such a test everywhere, so it
# is noticed on the development machine; give its parametrize short ``ids=``.
_MAX_TEST_ID = 1000


@pytest.fixture(autouse=True)
def _test_ids_stay_short(request):
    if len(request.node.nodeid) > _MAX_TEST_ID:
        pytest.fail(
            f"{request.node.nodeid[:120]}... has an id of {len(request.node.nodeid)} characters, "
            f"more than {_MAX_TEST_ID}; Windows cannot run it (PYTEST_CURRENT_TEST is limited to "
            "32767 characters): give the parametrize short ids=",
            pytrace=False,
        )
    yield


# The web console saves settings straight into the project's .env, and a GET or
# POST that reaches those handlers without patching ``_find_or_create_env``
# would create or rewrite the real file -- on a developer machine, the live bot
# configuration.  Fail the offending test instead of letting that pass silently.
_PROJECT_ENV = _ROOT / ".env"


def _env_fingerprint():
    try:
        stat = _PROJECT_ENV.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


@pytest.fixture(autouse=True)
def _project_env_file_is_never_touched(request):
    before = _env_fingerprint()
    yield
    if _env_fingerprint() != before:
        pytest.fail(
            f"{request.node.nodeid} created or modified the project's .env; "
            "patch src.web.server._find_or_create_env (or src.config.find_env_file) "
            "so the test works on a temporary file",
            pytrace=False,
        )
