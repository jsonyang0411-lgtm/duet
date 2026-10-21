#!/usr/bin/env bash
# Run the unit-test suite in a throw-away virtualenv (clean-machine check).
#
#   scripts/run-tests.sh            # whole suite
#   scripts/run-tests.sh tests/test_router.py -k sticky
#
# Needs Python 3.12+ on PATH (override with PYTHON=/path/to/python).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
VENV="$(mktemp -d "${TMPDIR:-/tmp}/webot-tests.XXXXXX")"
trap 'rm -rf "$VENV"' EXIT

"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements-macos.txt" -r "$ROOT/requirements-dev.txt"

cd "$ROOT"
if [ "$#" -eq 0 ]; then set -- tests; fi
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -m pytest "$@" -q -p no:cacheprovider
