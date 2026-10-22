"""macOS OCR engine: a screenshot of the WeChat window read by Apple Vision.

The recognizer is a small Swift program (``native/macos/wechat_ocr/ocr.swift``).  It
is compiled once with ``swiftc`` (Xcode Command Line Tools) into the user's cache
folder, named after a hash of its source so an edited source is rebuilt.  One look is
about half a second: ``screencapture`` of the window (it may be covered by other
windows, but not minimized), then the helper turns the picture into rows
(see ``reader.py`` for their shape).

The screenshot is a private file that is deleted as soon as it has been read.
Without the Screen Recording permission macOS hands out a picture without the
WeChat content: the rows are then empty, which is not an error here; the fallback
warns about it (``OcrFallback``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

def _default_source() -> Path:
    """The recognizer's source: next to the code, or inside the packaged app (PyInstaller's data folder)."""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parents[3]
    return root / "native" / "macos" / "wechat_ocr" / "ocr.swift"


SOURCE = _default_source()
COMPILE_TIMEOUT_SEC = 300
RUN_TIMEOUT_SEC = 10


def _cache_dir() -> Path:
    return Path.home() / "Library" / "Caches" / "webot" / "wechat_ocr"


class MacVisionEngine:
    """``capture()`` returns the OCR rows of the WeChat window; ``prepare()`` builds the helper."""

    def __init__(self, source: Path | None = None, cache_dir: Path | None = None, runner=subprocess.run):
        self._source = Path(source) if source else SOURCE
        self._cache_dir = Path(cache_dir) if cache_dir else _cache_dir()
        self._run = runner
        self._helper: str | None = None

    def supported(self) -> bool:
        return sys.platform == "darwin"

    def helper_path(self) -> Path:
        digest = hashlib.sha256(self._source.read_bytes()).hexdigest()[:12]
        return self._cache_dir / f"ocr-{digest}"

    def prepare(self) -> bool:
        """Compile the helper if it is not built yet.  False (with the reason logged) when it cannot be used."""
        if not self.supported():
            logger.warning("OCR fallback: Apple Vision exists on macOS only")
            return False
        try:
            path = self.helper_path()
        except OSError as exc:
            logger.warning("OCR fallback: cannot read %s: %s", self._source, exc)
            return False
        if path.exists() and os.access(path, os.X_OK):
            self._helper = str(path)
            return True
        swiftc = shutil.which("swiftc")
        if not swiftc:
            logger.warning("OCR fallback: swiftc not found; install the Xcode Command Line Tools "
                           "(xcode-select --install)")
            return False
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        logger.info("OCR fallback: compiling the recognizer (once, about a minute)...")
        try:
            result = self._run([swiftc, "-O", str(self._source), "-o", str(partial)],
                               capture_output=True, text=True, timeout=COMPILE_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("OCR fallback: compiling the recognizer failed: %s", exc)
            return False
        if result.returncode != 0 or not partial.exists():
            logger.warning("OCR fallback: compiling the recognizer failed: %s", (result.stderr or "")[-400:])
            partial.unlink(missing_ok=True)
            return False
        partial.chmod(0o755)
        partial.replace(path)
        self._helper = str(path)
        logger.info("OCR fallback: recognizer ready (%s)", path)
        return True

    def capture(self) -> list[dict]:
        """One look at the WeChat window.  Raises ``RuntimeError`` when there is no window to look at."""
        if self._helper is None and not self.prepare():
            raise RuntimeError("the OCR recognizer is not available")
        wid = self._run([self._helper, "wid"], capture_output=True, text=True,
                        timeout=RUN_TIMEOUT_SEC).stdout.strip()
        if not wid:
            raise RuntimeError("WeChat's main window was not found (WeChat is closed or minimized)")
        fd, shot = tempfile.mkstemp(prefix="webot_ocr_", suffix=".png")
        os.close(fd)
        try:
            self._run(["screencapture", "-l", wid, "-o", "-x", shot], check=True, timeout=RUN_TIMEOUT_SEC)
            out = self._run([self._helper, shot], capture_output=True, text=True, check=True,
                            timeout=RUN_TIMEOUT_SEC).stdout
        finally:
            try:
                os.unlink(shot)
            except OSError:
                pass
        return [json.loads(line) for line in out.splitlines() if line.strip()]
