"""Quieter sending on Windows: wait for a pause, then leave things as they were.

WeChat 4.x draws its own controls, so the only reliable way to send is still to
type into its window, and that needs the window in front.  With
``SEND_QUIET=on`` the window controller (``window_controller.py``) does that
with as little disturbance as it can:

* it waits until keyboard and mouse have been idle for ``SEND_IDLE_SEC``
  (at most ``SEND_IDLE_MAX_WAIT_SEC``), so it does not cut into the user's typing;
* it gives the focus back to the window the user was in, and puts the clipboard
  back as it was (``SEND_KEEP_CLIPBOARD``);
* a group that is open in its own WeChat window is typed into directly, without
  the search in the main window;
* it brings WeChat forward without the Alt key tap, and uses that only as a
  fallback.

Every Windows call is in :class:`Win32Desktop`; the rest takes it as a
parameter, so the logic is tested on any platform with a fake.
"""

from __future__ import annotations

import ctypes
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Titles of the WeChat main window; a group with one of these names cannot be
# told apart from it, so it never gets the own-window path.
MAIN_WINDOW_TITLES = {"微信", "WeChat", "Weixin"}

# Formats whose data is a GDI handle, not memory: they cannot be copied byte by
# byte.  The system re-creates CF_BITMAP from CF_DIB, which is kept.
_GDI_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
MAX_CLIPBOARD_BYTES = 64 * 1024 * 1024

# Registered formats that keep text out of the clipboard history (Win+V) and
# the cloud clipboard, so the bot's replies do not pile up there.
CLIPBOARD_PRIVACY_FORMATS = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)


@dataclass(frozen=True)
class QuietSendSettings:
    enabled: bool = False
    idle_sec: float = 3.0
    idle_max_wait_sec: float = 30.0
    keep_clipboard: bool = True

    @classmethod
    def from_config(cls, config) -> "QuietSendSettings":
        return cls(
            enabled=getattr(config, "send_quiet", "off") == "on",
            idle_sec=float(getattr(config, "send_idle_sec", 3.0)),
            idle_max_wait_sec=float(getattr(config, "send_idle_max_wait_sec", 30.0)),
            keep_clipboard=getattr(config, "send_keep_clipboard", "on") == "on",
        )


@dataclass
class QuietSession:
    """What one send changes and has to put back."""
    previous_foreground: int = 0
    clipboard: Optional[list] = None       # [(format, bytes)], None = not saved
    clipboard_sequence: Optional[int] = None  # sequence number right after our last write
    target_hwnd: int = 0                   # a WeChat window we typed into


def title_names_chat(title: str, chat_name: str) -> bool:
    """True when a window title is the chat's name, optionally with a member count."""
    title = (title or "").strip()
    name = (chat_name or "").strip()
    if not title or not name:
        return False
    if title == name:
        return True
    return re.fullmatch(re.escape(name) + r"\s*[(（]\d+[)）]", title) is not None


def keep_clipboard_format(fmt: int) -> bool:
    """Formats that are plain memory and can be saved and written back."""
    return fmt not in _GDI_FORMATS and not (0x200 <= fmt <= 0x3FF)


class IdleGate:
    """Wait until the user has not touched keyboard or mouse for a while."""

    POLL_SEC = 0.25

    def __init__(
        self,
        desktop,
        idle_sec: float,
        max_wait_sec: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._desktop = desktop
        self._idle_sec = max(0.0, float(idle_sec))
        self._max_wait_sec = max(0.0, float(max_wait_sec))
        self._sleep = sleep
        self._clock = clock
        self._own_input_tick: Optional[int] = None

    def idle_seconds(self) -> float:
        last = self._desktop.last_input_tick()
        if last is None:
            return math.inf
        # Our own keystrokes count as input too; nothing newer means the user
        # has not moved since before the last send.
        if last == self._own_input_tick:
            return math.inf
        return ((self._desktop.tick_count() - last) & 0xFFFFFFFF) / 1000.0

    def mark_own_input(self) -> None:
        """Call after a send so its keystrokes do not delay the next one."""
        self._own_input_tick = self._desktop.last_input_tick()

    def wait(self, alive: Optional[Callable[[], bool]] = None) -> bool:
        """True when it is time to send; False when ``alive`` says the bot stopped."""
        if self._idle_sec <= 0:
            return True
        start = self._clock()
        announced = False
        while True:
            idle = self.idle_seconds()
            if idle >= self._idle_sec:
                if announced:
                    logger.info("User paused after %.1fs; sending", self._clock() - start)
                return True
            waited = self._clock() - start
            if waited >= self._max_wait_sec:
                logger.info("User still busy after %.0fs; sending anyway", waited)
                return True
            if alive is not None and not alive():
                return False
            if not announced:
                logger.info(
                    "Waiting for the user to pause (idle %.1fs, need %.1fs, at most %.0fs)",
                    idle, self._idle_sec, self._max_wait_sec,
                )
                announced = True
            self._sleep(min(self.POLL_SEC, max(0.05, self._idle_sec - idle)))


class Win32Desktop:
    """The Win32 calls quiet sending needs (ctypes; loaded on first use, Windows only).

    Private ``WinDLL`` instances keep these prototypes away from the
    ``ctypes.windll.user32`` functions the rest of the code calls with plain ints.
    """

    PROCESSED_PINGS = 3
    PING_TIMEOUT_MS = 1000

    def __init__(self) -> None:
        self._dlls = None

    def _api(self):
        if self._dlls is None:
            self._dlls = _load_win32()
        return self._dlls

    def foreground_window(self) -> int:
        user32, _ = self._api()
        return int(user32.GetForegroundWindow() or 0)

    def is_window(self, hwnd: int) -> bool:
        user32, _ = self._api()
        return bool(hwnd) and bool(user32.IsWindow(hwnd))

    def bring_forward(self, hwnd: int) -> bool:
        """Make ``hwnd`` the foreground window without simulated keys.

        Attaching to the input queue of the current foreground thread lifts the
        foreground lock for this call; no key press reaches the user's window.
        """
        user32, kernel32 = self._api()
        foreground = user32.GetForegroundWindow()
        current = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = False
        try:
            if foreground_thread and foreground_thread != current:
                attached = bool(user32.AttachThreadInput(current, foreground_thread, True))
            user32.BringWindowToTop(hwnd)
            return bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(current, foreground_thread, False)

    def last_input_tick(self) -> Optional[int]:
        user32, _ = self._api()
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return int(info.dwTime)

    def tick_count(self) -> int:
        _, kernel32 = self._api()
        return int(kernel32.GetTickCount())

    def wait_until_processed(self, hwnd: int) -> bool:
        """True once WeChat's UI thread has handled the input queued before this call.

        A sent message is answered when the thread next looks at its queue, and
        a Qt thread handles all pending input in that same pass, so the second
        answer comes after the paste was handled.  False when WeChat does not
        answer within a second (busy or hung).
        """
        if not hwnd:
            return False
        user32, _ = self._api()
        result = ctypes.c_size_t()
        for _ in range(self.PROCESSED_PINGS):
            if not user32.SendMessageTimeoutW(
                hwnd, 0, 0, 0, 0x0002, self.PING_TIMEOUT_MS, ctypes.byref(result),
            ):   # WM_NULL, SMTO_ABORTIFHUNG
                return False
            time.sleep(0.02)
        return True

    def clipboard_sequence(self) -> int:
        user32, _ = self._api()
        return int(user32.GetClipboardSequenceNumber())

    def read_clipboard(self) -> Optional[list]:
        """Every format that can be copied as bytes, or None when that is not possible."""
        user32, kernel32 = self._api()
        if not self._open_clipboard():
            return None
        try:
            saved, total, fmt = [], 0, 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if not fmt:
                    return saved
                if not keep_clipboard_format(fmt):
                    continue
                handle = user32.GetClipboardData(fmt)
                if not handle:
                    continue
                size = kernel32.GlobalSize(handle)
                if not size:
                    continue
                total += size
                if total > MAX_CLIPBOARD_BYTES:
                    logger.info("Clipboard holds more than %d MB; it will not be restored",
                                MAX_CLIPBOARD_BYTES // (1024 * 1024))
                    return None
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    continue
                try:
                    saved.append((fmt, ctypes.string_at(pointer, size)))
                finally:
                    kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def write_clipboard(self, formats: list) -> bool:
        user32, kernel32 = self._api()
        if not self._open_clipboard():
            return False
        try:
            user32.EmptyClipboard()
            for fmt, data in formats:
                memory = kernel32.GlobalAlloc(0x0002, max(1, len(data)))   # GMEM_MOVEABLE
                if not memory:
                    continue
                pointer = kernel32.GlobalLock(memory)
                if not pointer:
                    kernel32.GlobalFree(memory)
                    continue
                ctypes.memmove(pointer, data, len(data))
                kernel32.GlobalUnlock(memory)
                if not user32.SetClipboardData(fmt, memory):
                    kernel32.GlobalFree(memory)
            return True
        finally:
            user32.CloseClipboard()

    def _open_clipboard(self) -> bool:
        user32, _ = self._api()
        for _ in range(5):
            if user32.OpenClipboard(None):
                return True
            time.sleep(0.02)
        logger.debug("Clipboard is held by another program")
        return False


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]


def _load_win32():
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    HWND, HANDLE, UINT, DWORD, BOOL = (
        wintypes.HWND, wintypes.HANDLE, wintypes.UINT, wintypes.DWORD, wintypes.BOOL,
    )
    prototypes = (
        (user32.GetForegroundWindow, [], HWND),
        (user32.IsWindow, [HWND], BOOL),
        (user32.GetWindowThreadProcessId, [HWND, ctypes.POINTER(DWORD)], DWORD),
        (user32.AttachThreadInput, [DWORD, DWORD, BOOL], BOOL),
        (user32.BringWindowToTop, [HWND], BOOL),
        (user32.SetForegroundWindow, [HWND], BOOL),
        (user32.GetLastInputInfo, [ctypes.POINTER(_LASTINPUTINFO)], BOOL),
        (user32.SendMessageTimeoutW,
         [HWND, UINT, wintypes.WPARAM, wintypes.LPARAM, UINT, UINT, ctypes.POINTER(ctypes.c_size_t)],
         wintypes.LPARAM),
        (user32.GetClipboardSequenceNumber, [], DWORD),
        (user32.OpenClipboard, [HWND], BOOL),
        (user32.CloseClipboard, [], BOOL),
        (user32.EmptyClipboard, [], BOOL),
        (user32.EnumClipboardFormats, [UINT], UINT),
        (user32.GetClipboardData, [UINT], HANDLE),
        (user32.SetClipboardData, [UINT, HANDLE], HANDLE),
        (kernel32.GetCurrentThreadId, [], DWORD),
        (kernel32.GetTickCount, [], DWORD),
        (kernel32.GlobalSize, [HANDLE], ctypes.c_size_t),
        (kernel32.GlobalLock, [HANDLE], ctypes.c_void_p),
        (kernel32.GlobalUnlock, [HANDLE], BOOL),
        (kernel32.GlobalAlloc, [UINT, ctypes.c_size_t], HANDLE),
        (kernel32.GlobalFree, [HANDLE], HANDLE),
    )
    for function, argtypes, restype in prototypes:
        function.argtypes = argtypes
        function.restype = restype
    return user32, kernel32
