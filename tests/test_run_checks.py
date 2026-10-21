"""tools/run_checks.py: the local stand-in for the CI workflow."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

from tools import run_checks

BASELINES = Path(run_checks.REPO_ROOT) / "evals" / "baseline"


def fake_run(produce=None, tests_ok=True):
    """subprocess.run that writes a replay result (a copy of the baseline, possibly changed)."""

    def run(cmd, **kwargs):
        if cmd[:3] == [run_checks.sys.executable, "-m", "pytest"]:
            out = "....\n3119 passed, 1 skipped in 90s\n" if tests_ok else \
                "FAILED tests/test_x.py::test_y - AssertionError\n1 failed, 3118 passed\n"
            return SimpleNamespace(returncode=0 if tests_ok else 1, stdout=out, stderr="")
        if cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="abc123 test\n" if "log" in cmd else "", stderr="")
        target = Path(cmd[cmd.index("--out") + 1])
        module = cmd[cmd.index("-m") + 1]
        baseline = "phase3_after.json" if module == "evals.run_memory" else (
            "phase2_after.json" if "--pipeline" in cmd else "phase1_after.json")
        data = json.loads((BASELINES / baseline).read_text(encoding="utf-8"))
        if produce:
            data = produce(baseline, data)
        target.mkdir(parents=True, exist_ok=True)
        (target / "check.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_everything_identical_passes(monkeypatch):
    monkeypatch.setattr(run_checks.subprocess, "run", fake_run())
    out = io.StringIO()
    assert run_checks.main([], out) == 0
    text = out.getvalue()
    assert "3119 passed, 1 skipped" in text and text.count("IDENTICAL") == 3 and "ALL PASSED" in text


def test_a_changed_replay_or_a_failed_test_fails(monkeypatch):
    def change(baseline, data):
        if baseline == "phase2_after.json":
            data = dict(data, extra_key=1)
        return data

    monkeypatch.setattr(run_checks.subprocess, "run", fake_run(change, tests_ok=False))
    out = io.StringIO()
    assert run_checks.main([], out) == 1
    text = out.getvalue()
    assert "FAILED tests/test_x.py::test_y" in text
    assert "DIFFERENT offline replay, single + score vs phase2_after.json (differs in: extra_key)" in text
    assert "FAILED: unit tests; offline replay, single + score" in text


def test_bench_numbers_are_ignored_and_tests_can_be_skipped(monkeypatch):
    def add_bench(baseline, data):
        return dict(data, bench={"p50": 1.0})

    monkeypatch.setattr(run_checks.subprocess, "run", fake_run(add_bench))
    out = io.StringIO()
    assert run_checks.main(["--no-tests"], out) == 0
    assert "unit tests" not in out.getvalue()
