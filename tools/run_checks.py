"""Run every offline check on this machine: the unit tests and the baseline comparisons.

    python tools/run_checks.py            # everything (a few minutes)
    python tools/run_checks.py --no-tests # only the replays and the memory evaluation

What it does, in order:

1. prints the commit and whether the working tree has local changes;
2. ``python -m pytest tests -q`` (with ``PYTHONUTF8=1``);
3. the offline replay in the default profile, compared with
   ``evals/baseline/phase1_after.json``;
4. the offline replay with the structured pipeline and the score gate,
   compared with ``phase2_after.json``;
5. the memory evaluation, compared with ``phase3_after.json`` (``bench`` ignored).

No model is called and nothing in the repository is written: the replays
write into a temporary folder.  Exit status 0 means everything passed.  The
GitHub workflow (.github/workflows/tests.yml) runs this same script.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

COMPARISONS = (
    ("offline replay, default profile", ["evals.run_offline", "--seed", "7"], "phase1_after.json"),
    ("offline replay, single + score", ["evals.run_offline", "--seed", "7", "--pipeline", "single",
                                        "--gate", "score"], "phase2_after.json"),
    ("memory evaluation", ["evals.run_memory"], "phase3_after.json"),
)


def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                              encoding="utf-8", timeout=30)
        return done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return env


def run_tests(out) -> bool:
    print("== unit tests: python -m pytest tests -q", file=out, flush=True)
    done = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
                          cwd=REPO_ROOT, env=_env(), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    if done.returncode != 0:
        failures = [line for line in lines if line.startswith(("FAILED", "ERROR"))]
        for line in failures[:30]:
            print("   " + line, file=out)
        if not failures:
            print("\n".join("   " + line for line in (lines[-30:] + done.stderr.splitlines()[-10:])), file=out)
    print("   " + (lines[-1] if lines else "(no output)"), file=out, flush=True)
    return done.returncode == 0


def compare(label: str, args: list[str], baseline: str, target: Path, out) -> bool:
    done = subprocess.run([sys.executable, "-m", *args, "--tag", "check", "--out", str(target)],
                          cwd=REPO_ROOT, env=_env(), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    produced = target / "check.json"
    if done.returncode != 0 or not produced.is_file():
        print(f"   ERROR     {label}: the run failed", file=out)
        for line in (done.stdout + done.stderr).splitlines()[-15:]:
            print("      " + line, file=out)
        return False
    new = json.loads(produced.read_text(encoding="utf-8"))
    old = json.loads((REPO_ROOT / "evals" / "baseline" / baseline).read_text(encoding="utf-8"))
    new.pop("bench", None)
    old.pop("bench", None)
    same = new == old
    differing = [] if same else sorted(k for k in set(new) | set(old) if new.get(k) != old.get(k))
    print(f"   {'IDENTICAL' if same else 'DIFFERENT'} {label} vs {baseline}"
          + (f" (differs in: {', '.join(differing)})" if differing else ""), file=out, flush=True)
    return same


def main(argv=None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-tests", action="store_true", help="skip the unit tests")
    args = parser.parse_args(argv)

    commit = _git("log", "-1", "--format=%h %s") or "(not a git checkout)"
    changes = _git("status", "--short", "--untracked-files=no")
    print(f"commit: {commit}", file=out)
    print(f"local changes: {'yes' if changes else 'none'}", file=out)
    for line in changes.splitlines()[:20]:
        print("   " + line, file=out)

    results = []
    if not args.no_tests:
        results.append(("unit tests", run_tests(out)))
    print("== baselines", file=out, flush=True)
    with tempfile.TemporaryDirectory(prefix="webot-checks-", ignore_cleanup_errors=True) as tmp:
        for index, (label, run_args, baseline) in enumerate(COMPARISONS, start=1):
            results.append((label, compare(label, run_args, baseline, Path(tmp) / f"run{index}", out)))

    failed = [label for label, ok in results if not ok]
    print("== " + ("ALL PASSED" if not failed else "FAILED: " + "; ".join(failed)), file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
