"""Windows OCR engine: a picture of the WeChat window read by Windows.Media.Ocr.

Windows 10/11 has a text recognizer built in (``Windows.Media.Ocr``).  Python cannot call
it without extra packages, so a short PowerShell script does (it is fed to PowerShell on
standard input: no script file, no execution-policy change).  The Chinese recognizer is a
Windows feature that may need installing once, as administrator:

    Add-WindowsCapability -Online -Name Language.OCR~~~zh-CN~0.0.1.0

One look:
  1. the WeChat main window (found by ``WeChatWindowController.find_hwnd``, which never wakes a
     tray window here) is captured with ``PrintWindow``, so it can be covered by other windows
     or on another monitor, but not minimized;
  2. the picture goes to a private temp file that is deleted right after;
  3. the script prints one JSON line per text line: ``{"t": text, "x", "y", "w", "h"}`` in pixels;
  4. ``rows_from_lines`` turns them into the rows ``reader.py`` reads (0..1 from the top left, plus
     the colour of the pixels left of the text, which tells the two sides' bubbles apart).

Windows OCR puts spaces between Chinese characters; they are taken out again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

RUN_TIMEOUT_SEC = 30
LANGUAGES = "zh-Hans-CN,zh-CN"        # tried in order, then the languages of the user profile
MIN_WINDOW_SIZE = 300                  # px; smaller is a tray or helper window
BG_OFFSET_PX = 8                       # the bubble colour is sampled this far left of the text
INSTALL_HINT = ("Add-WindowsCapability -Online -Name Language.OCR~~~zh-CN~0.0.1.0 "
                "(PowerShell as administrator)")

# ASCII only: it travels over a pipe.  Exit code 3 = no OCR language installed.
SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($operation, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$engine = $null
foreach ($tag in ($env:WEBOT_OCR_LANGS -split ',')) {
    if (-not $tag) { continue }
    $language = New-Object Windows.Globalization.Language $tag
    if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($language)) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
        if ($engine) { break }
    }
}
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if (-not $engine) { [Console]::Error.WriteLine('NO_OCR_LANGUAGE'); exit 3 }

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:WEBOT_OCR_IMAGE)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

foreach ($line in $result.Lines) {
    $x0 = [double]::MaxValue; $y0 = [double]::MaxValue; $x1 = 0.0; $y1 = 0.0
    foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        if ($r.X -lt $x0) { $x0 = $r.X }
        if ($r.Y -lt $y0) { $y0 = $r.Y }
        if ($r.X + $r.Width -gt $x1) { $x1 = $r.X + $r.Width }
        if ($r.Y + $r.Height -gt $y1) { $y1 = $r.Y + $r.Height }
    }
    if ($x1 -le $x0) { continue }
    $row = [ordered]@{ t = $line.Text; x = [math]::Round($x0, 1); y = [math]::Round($y0, 1);
                       w = [math]::Round($x1 - $x0, 1); h = [math]::Round($y1 - $y0, 1) }
    ConvertTo-Json -InputObject $row -Compress
}

'''

_CJK = "⺀-鿿＀-￯"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}]) +(?=[{_CJK}])")


def fix_spacing(text: str) -> str:
    """Take out the spaces Windows OCR puts between Chinese characters (``你 好`` -> ``你好``)."""
    return _CJK_GAP.sub("", text).strip()


def _pixel(image, x: int, y: int) -> list[int]:
    width, height = image.size
    px = image.getpixel((max(0, min(x, width - 1)), max(0, min(y, height - 1))))
    return [int(px[0]), int(px[1]), int(px[2])]


def rows_from_lines(lines: list[dict], image) -> list[dict]:
    """The reader's rows (see ``reader.py``) for the lines the script printed, in pixels of ``image``."""
    width, height = image.size
    rows = []
    for line in lines:
        text = fix_spacing(str(line.get("t", "")))
        if not text or width <= 0 or height <= 0:
            continue
        x, y, w, h = (float(line.get(k, 0) or 0) for k in ("x", "y", "w", "h"))
        rows.append({
            "t": text,
            "x0": x / width, "x1": (x + w) / width,
            "y0": y / height, "y1": (y + h) / height,
            "bg": _pixel(image, int(x) - BG_OFFSET_PX, int(y + h / 2)),
        })
    return rows


def print_window(hwnd: int):
    """The window as a PIL image, even when other windows cover it.  Windows only."""
    import ctypes

    import win32gui
    import win32ui
    from PIL import Image

    user32 = ctypes.windll.user32
    # The size must be in real pixels whatever the DPI awareness of this process.
    set_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous = None
    if set_context is not None:
        set_context.restype = ctypes.c_void_p
        set_context.argtypes = [ctypes.c_void_p]
        previous = set_context(ctypes.c_void_p(-4))      # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        if win32gui.IsIconic(hwnd):
            raise RuntimeError("WeChat is minimized; restore its window")
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width < MIN_WINDOW_SIZE or height < MIN_WINDOW_SIZE:
            raise RuntimeError(f"the WeChat window is too small ({width}x{height})")
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        try:
            ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)      # PW_RENDERFULLCONTENT
            bits = bitmap.GetBitmapBits(True)
            image = Image.frombuffer("RGB", (width, height), bits, "raw", "BGRX", 0, 1)
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
    finally:
        if set_context is not None and previous:
            set_context(ctypes.c_void_p(previous))
    if not ok or image.getbbox() is None:
        raise RuntimeError("PrintWindow gave no picture of the WeChat window (all black)")
    return image


class WindowsOcrEngine:
    """``capture()`` returns the OCR rows of the WeChat window; same contract as ``MacVisionEngine``."""

    def __init__(self, window=None, runner=subprocess.run, grabber=print_window, powershell: str | None = None):
        self._window = window
        self._run = runner
        self._grab = grabber
        self._powershell = powershell

    def supported(self) -> bool:
        return sys.platform == "win32"

    def prepare(self) -> bool:
        """True when PowerShell is there (whether the Chinese recognizer is installed shows at the first look)."""
        if not self.supported():
            logger.warning("OCR fallback: Windows.Media.Ocr exists on Windows only")
            return False
        self._powershell = self._powershell or shutil.which("powershell") or shutil.which("pwsh")
        if not self._powershell:
            logger.warning("OCR fallback: PowerShell was not found")
            return False
        return True

    def capture(self, hwnd: int | None = None) -> list[dict]:
        if self._powershell is None and not self.prepare():
            raise RuntimeError("the OCR recognizer is not available")
        target_hwnd = hwnd if hwnd is not None else self._find_window()
        image = self._grab(target_hwnd)
        return rows_from_lines(self._recognize(image), image)

    def _find_window(self) -> int:
        if self._window is None:
            from ..window_controller import WeChatWindowController
            self._window = WeChatWindowController()
        hwnd = self._window.find_hwnd(wake=False)
        if not hwnd:
            raise RuntimeError("WeChat's main window was not found (WeChat is closed or only in the tray)")
        return hwnd

    def _recognize(self, image) -> list[dict]:
        fd, path = tempfile.mkstemp(prefix="webot_ocr_", suffix=".png")
        os.close(fd)
        try:
            image.save(path, "PNG")
            env = dict(os.environ, WEBOT_OCR_IMAGE=path, WEBOT_OCR_LANGS=LANGUAGES)
            result = self._run(
                [self._powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"],
                input=SCRIPT, capture_output=True, text=True, encoding="utf-8", env=env,
                timeout=RUN_TIMEOUT_SEC,
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if result.returncode == 3 or "NO_OCR_LANGUAGE" in (result.stderr or ""):
            raise RuntimeError(f"Windows has no OCR language installed: {INSTALL_HINT}")
        if result.returncode != 0:
            raise RuntimeError(f"the OCR script failed ({result.returncode}): {(result.stderr or '').strip()[-300:]}")
        lines = []
        for raw in (result.stdout or "").splitlines():
            raw = raw.strip().lstrip("﻿")
            if raw.startswith("{"):
                try:
                    lines.append(json.loads(raw))
                except ValueError:
                    logger.debug("OCR fallback: ignoring a line that is not JSON: %r", raw[:80])
        return lines
