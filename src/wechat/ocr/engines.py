"""The OCR engine of this platform."""

from __future__ import annotations

import sys


def default_engine(window=None):
    """``MacVisionEngine`` on macOS, ``WindowsOcrEngine`` on Windows (given the backend's window controller).

    Raises ``RuntimeError`` elsewhere: Linux has no WeChat client this bot can read.
    """
    if sys.platform == "darwin":
        from .mac_vision import MacVisionEngine
        return MacVisionEngine()
    if sys.platform == "win32":
        from .win_ocr import WindowsOcrEngine
        return WindowsOcrEngine(window=window)
    raise RuntimeError(f"no OCR engine for {sys.platform}")
