"""WeChat Window Controller — reliable HWND finding, activation, and navigation.

Design principles:
1. Every candidate HWND is validated against process name (WeChat.exe/Weixin.exe).
2. Scoring: title="微信" + class starts with Qt + visible + reasonable size → highest.
3. HWND is validated before every use; stale HWND triggers re-find.
4. Navigation returns True only when we have reasonable confidence.
5. Comprehensive diagnostic logging at every step.
"""

import ctypes
import io
import logging
import struct
import subprocess
import time
import uuid

from .keyboard import press_key, send_combo
from .quiet_send import (
    CLIPBOARD_PRIVACY_FORMATS,
    MAIN_WINDOW_TITLES,
    IdleGate,
    QuietSendSettings,
    QuietSession,
    Win32Desktop,
    title_names_chat,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import win32gui
import win32con
import win32clipboard
import win32process
from PIL import Image, ImageGrab, ImageStat

logger = logging.getLogger(__name__)

def _ensure_default_desktop() -> None:
    """Ensure the calling thread is attached to the interactive 'Default' desktop."""
    try:
        user32 = ctypes.windll.user32
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
    except Exception:
        pass


def _reset_last_error() -> None:
    """Clear the Win32 last-error code before a call whose failure is read back.

    ``ctypes.windll`` exists only on Windows; elsewhere (the unit tests on a Mac) this does nothing.
    """
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        windll.kernel32.SetLastError(0)


# ── Constants ─────────────────────────────────────────────────────

WECHAT_PROCESS_NAMES = {"wechat.exe", "weixin.exe"}
MIN_WINDOW_WIDTH = 200
MIN_WINDOW_HEIGHT = 200
MAX_CANDIDATES_LOG = 20
WHITE_SCREEN_MEAN_THRESHOLD = 248
WHITE_SCREEN_STDDEV_THRESHOLD = 3.5

FAILURE_LOG = Path("data/send_failures.log")
FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
VISION_SCREENSHOT_DIR = Path("data/vision_screenshots")
_PICTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


@dataclass
class WindowCandidate:
    """A candidate WeChat window with scoring info."""
    hwnd: int = 0
    title: str = ""
    class_name: str = ""
    pid: int = 0
    process_name: str = ""
    rect: tuple = (0, 0, 0, 0)
    visible: bool = False
    iconic: bool = False
    score: int = 0
    reason: str = ""


@dataclass
class WindowSnapshot:
    """Original window state so temporary send activation can be undone."""
    hwnd: int
    was_visible: bool = False
    was_iconic: bool = False


# ── Window scoring ────────────────────────────────────────────────

def _get_process_name(pid: int) -> str:
    """Get process executable name from PID."""
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            return ""
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_uint32(260)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        )
        ctypes.windll.kernel32.CloseHandle(handle)
        if ok:
            return Path(buf.value).name.lower()
        return ""
    except Exception:
        return ""


def _rect_size(rect: tuple) -> tuple[int, int]:
    """Return non-negative width/height for a Win32 rect tuple."""
    try:
        return max(0, int(rect[2]) - int(rect[0])), max(0, int(rect[3]) - int(rect[1]))
    except Exception:
        return 0, 0


def _normal_placement_rect(hwnd: int) -> tuple | None:
    """Return the restored-position rect for minimized windows, if available."""
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement and len(placement) >= 5:
            return placement[4]
    except Exception:
        pass
    return None


def _candidate_size(c: WindowCandidate) -> tuple[int, int]:
    """Use restored placement size when minimized rect is off-screen/tiny."""
    w, h = _rect_size(c.rect)
    if (w >= MIN_WINDOW_WIDTH and h >= MIN_WINDOW_HEIGHT) or not c.iconic:
        return w, h

    normal = _normal_placement_rect(c.hwnd)
    if normal:
        nw, nh = _rect_size(normal)
        if nw >= MIN_WINDOW_WIDTH and nh >= MIN_WINDOW_HEIGHT:
            return nw, nh
    return w, h


def _looks_like_main_window(c: WindowCandidate) -> bool:
    """Filter out worker/helper windows while allowing minimized main windows."""
    if c.process_name not in WECHAT_PROCESS_NAMES:
        return False

    has_main_title = "微信" in c.title or "WeChat" in c.title
    has_main_class = (
        c.class_name.startswith("Qt")
        or c.class_name.startswith("Chrome_WidgetWin")
    )

    # Hidden tray-restored main windows usually still have the main title.
    # Visible/iconic windows are already safer, but still require a known class.
    if not c.visible and not c.iconic:
        return has_main_title and has_main_class
    return has_main_class or has_main_title


def _conversation_bbox(left: int, top: int, right: int, bottom: int,
                       sidebar: bool = True) -> tuple[int, int, int, int]:
    """The message area of a chat window: no title bar, input box or chat list.

    The main window has the chat list on the left; a chat open in its own
    window (``sidebar=False``) does not.  The latest incoming sticker stays
    visible near the bottom of this region.
    """
    width, height = right - left, bottom - top
    return (
        left + (max(180, int(width * 0.22)) if sidebar else max(12, int(width * 0.02))),
        top + max(55, int(height * 0.08)),
        right - max(12, int(width * 0.02)),
        bottom - max(150, int(height * 0.23)),
    )


def _dropfiles_bytes(path: str) -> bytes:
    """CF_HDROP data for one file: a DROPFILES header and the path, UTF-16, double-NUL ended."""
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)        # pFiles, pt.x, pt.y, fNC, fWide
    return header + (str(path) + "\0\0").encode("utf-16-le")


def _dib_bytes(path: str) -> bytes:
    """CF_DIB data for a picture: its first frame on white, as a bitmap without the file header."""
    with Image.open(path) as image:
        image.seek(0)
        frame = image.convert("RGBA")
    still = Image.new("RGB", frame.size, (255, 255, 255))
    still.paste(frame, mask=frame.split()[3])
    output = io.BytesIO()
    still.save(output, "BMP")
    return output.getvalue()[14:]                           # drop BITMAPFILEHEADER


def _score_window(hwnd: int) -> WindowCandidate:
    """Score a window for likelihood of being the WeChat main window.

    Returns a WindowCandidate with score and diagnostic info.
    """
    c = WindowCandidate(hwnd=hwnd)

    try:
        c.title = win32gui.GetWindowText(hwnd) or ""
        c.class_name = win32gui.GetClassName(hwnd) or ""
        c.visible = bool(win32gui.IsWindowVisible(hwnd))
        c.iconic = bool(win32gui.IsIconic(hwnd))

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        c.pid = pid
        c.process_name = _get_process_name(pid)
    except Exception:
        return c

    # Rect
    try:
        r = win32gui.GetWindowRect(hwnd)
        c.rect = (r[0], r[1], r[2], r[3])
    except Exception:
        pass

    # Scoring
    reasons = []

    # Must be WeChat process
    if c.process_name not in WECHAT_PROCESS_NAMES:
        c.reason = f"wrong process: {c.process_name}"
        return c

    c.score += 100
    reasons.append("wechat_process")

    # Title
    if c.title in ("微信", "WeChat"):
        c.score += 60
        reasons.append("title_wechat_exact")
    elif "微信" in c.title:
        c.score += 50
        reasons.append("title_wechat")
    elif "WeChat" in c.title:
        c.score += 40
        reasons.append("title_wechat_en")

    # Class name — WeChat has used Qt, CEF, and other frameworks
    if c.class_name.startswith("Qt"):
        c.score += 30
        reasons.append("qt_class")
    elif c.class_name.startswith("Chrome_WidgetWin"):
        c.score += 25
        reasons.append("cef_class")

    # Visibility
    if c.visible:
        c.score += 20
        reasons.append("visible")

    # Size
    w, h = _candidate_size(c)
    if w >= MIN_WINDOW_WIDTH and h >= MIN_WINDOW_HEIGHT:
        c.score += 10
        reasons.append("reasonable_size")
    else:
        reasons.append(f"too_small({w}x{h})")

    # Penalize iconic
    if c.iconic:
        c.score -= 15
        reasons.append("iconic")

    c.reason = "+".join(reasons)
    return c


# ── WeChat Window Controller ──────────────────────────────────────

class WeChatWindowController:
    """Manages WeChat main window discovery, validation, and activation.

    Usage:
        ctrl = WeChatWindowController()
        hwnd = ctrl.find_and_validate()
        if hwnd and ctrl.activate(hwnd):
            navigated = ctrl.navigate_to_chat(hwnd, "group name")
            if navigated:
                ctrl.send_message(hwnd, "Hello")
    """

    # ── Navigation timing constants ─────────────────────────────────
    SEARCH_FOCUS_DELAY: float = 0.15       # after Ctrl+F
    PASTE_DELAY: float = 0.05              # after Ctrl+A / after set clipboard
    SEARCH_POPULATE_DELAY: float = 0.3     # after paste into search
    SELECT_RESULT_DELAY: float = 0.15      # after Enter on search result
    CLIPBOARD_DELAY: float = 0.03          # after set clipboard (pre-paste)
    ENTER_SEND_DELAY: float = 0.1          # after Enter to send
    PASTE_SEND_DELAY: float = 0.15         # after Ctrl+V paste in send
    TAB_SWITCH_DELAY: float = 0.25         # after switching tabs (e.g. Chat→Contacts)
    QUIET_ACTIVATE_DELAY: float = 0.1      # after a key-free foreground switch
    CLIPBOARD_RESTORE_DELAY: float = 0.3   # before the user's clipboard is put back
    FILE_PASTE_DELAY: float = 0.8          # extra wait after pasting a picture (WeChat loads it)

    def __init__(self, quiet: QuietSendSettings | None = None, desktop=None,
                 paste_files_as: str = "file"):
        self._cached_hwnd: Optional[int] = None
        self._cached_at: float = 0.0
        self._cache_ttl: float = 30.0  # seconds
        # SEND_QUIET (see quiet_send.py).  Off: every step stays as it was.
        self._quiet = quiet if quiet is not None and quiet.enabled else None
        self._desktop = desktop if desktop is not None else (Win32Desktop() if self._quiet else None)
        self._idle_gate = (
            IdleGate(self._desktop, self._quiet.idle_sec, self._quiet.idle_max_wait_sec)
            if self._quiet else None
        )
        self._session: QuietSession | None = None
        # Chats open in their own window; never taken for the main window.
        self._chat_windows: set[int] = set()
        # Set by the backend: False once the bot stops (ends a wait for the user).
        self.alive: Callable[[], bool] | None = None
        # STICKER_PASTE: "file" pastes a sticker as the file, "image" as a still picture.
        self._paste_files_as = paste_files_as if paste_files_as in ("file", "image") else "file"

    # ── HWND discovery ────────────────────────────────────────────

    def find_hwnd(self, force: bool = False, wake: bool = True) -> Optional[int]:
        """Find the WeChat main window HWND with strict validation.

        Args:
            force: If True, bypass cache and re-scan.
            wake: If True, try to wake a running tray/hidden WeChat once.

        Returns:
            Validated HWND or None.
        """
        # Check cache
        if not force and self._cached_hwnd:
            if self._validate_hwnd(self._cached_hwnd):
                age = time.time() - self._cached_at
                if age < self._cache_ttl:
                    logger.debug(f"Using cached HWND={self._cached_hwnd} (age={age:.0f}s)")
                    return self._cached_hwnd

        # Scan all windows
        all_candidates: list[WindowCandidate] = []
        wechat_candidates: list[WindowCandidate] = []

        _ensure_default_desktop()

        def _enum(hwnd, _ctx):
            c = _score_window(hwnd)
            if c.process_name in WECHAT_PROCESS_NAMES:
                wechat_candidates.append(c)
            if c.title or c.class_name:
                all_candidates.append(c)
            return True

        _reset_last_error()
        try:
            win32gui.EnumWindows(_enum, None)
        except Exception as e:
            logger.debug("EnumWindows status: %s", e)

        # Log candidates for diagnostics
        if wechat_candidates:
            logger.debug(
                f"WeChat windows found: {len(wechat_candidates)} "
                f"(total scanned: {len(all_candidates)})"
            )
            for c in sorted(wechat_candidates, key=lambda x: -x.score)[:10]:
                w, h = _candidate_size(c)
                logger.debug(
                    f"  HWND={c.hwnd} score={c.score} "
                    f"title='{c.title[:30]}' class='{c.class_name[:30]}' "
                    f"pid={c.pid} process={c.process_name} "
                    f"size={w}x{h} visible={c.visible} iconic={c.iconic} "
                    f"reason={c.reason}"
                )

        if not wechat_candidates:
            if wake and self._wake_wechat_from_process():
                time.sleep(1.0)
                return self.find_hwnd(force=True, wake=False)
            logger.error(
                f"No WeChat process windows found. "
                f"Is WeChat running? Scanned {len(all_candidates)} windows."
            )
            return None

        usable_candidates = [
            c for c in wechat_candidates
            if _looks_like_main_window(c)
            and c.hwnd not in self._chat_windows
            and _candidate_size(c)[0] >= MIN_WINDOW_WIDTH
            and _candidate_size(c)[1] >= MIN_WINDOW_HEIGHT
        ]
        if not usable_candidates:
            if wake and self._wake_wechat_from_process():
                time.sleep(1.0)
                return self.find_hwnd(force=True, wake=False)
            logger.error(
                "WeChat is running, but no usable chat main window was found. "
                "It may be locked, logged out, or showing a small login prompt."
            )
            return None

        # Sort by score descending
        usable_candidates.sort(key=lambda x: -x.score)
        best = usable_candidates[0]

        if best.score < 100:
            w, h = _candidate_size(best)
            logger.warning(
                f"Best WeChat candidate has low score ({best.score}): "
                f"HWND={best.hwnd} title='{best.title}' class='{best.class_name}' "
                f"size={w}x{h}"
            )

        self._cached_hwnd = best.hwnd
        self._cached_at = time.time()
        logger.info(
            f"WeChat window selected: HWND={best.hwnd} score={best.score} "
            f"title='{best.title[:30]}' class='{best.class_name}'"
        )
        return best.hwnd

    def _validate_hwnd(self, hwnd: int) -> bool:
        """Check if an HWND is still valid and belongs to WeChat."""
        try:
            if not win32gui.IsWindow(hwnd):
                return False
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = _get_process_name(pid)
            return proc in WECHAT_PROCESS_NAMES
        except Exception:
            return False

    def invalidate_cache(self) -> None:
        """Force next find_hwnd() to re-scan."""
        self._cached_hwnd = None

    @staticmethod
    def _wake_wechat_from_process() -> bool:
        """Ask an already-running WeChat process to show its main window."""
        try:
            import psutil
        except Exception as e:
            logger.debug("wake WeChat skipped: psutil unavailable: %s", e)
            return False

        launched: set[str] = set()
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                exe = proc.info.get("exe") or ""
                if name not in WECHAT_PROCESS_NAMES or not exe:
                    continue
                exe_path = str(Path(exe))
                if exe_path in launched:
                    continue
                launched.add(exe_path)

                cwd = str(Path(exe_path).parent)
                logger.info("Trying to wake WeChat main window via executable: %s", exe_path)
                try:
                    result = ctypes.windll.shell32.ShellExecuteW(
                        None, "open", exe_path, None, cwd, win32con.SW_SHOWNORMAL
                    )
                    if int(result) > 32:
                        return True
                except Exception as e:
                    logger.debug("ShellExecute wake failed, falling back to Popen: %s", e)

                subprocess.Popen(
                    [exe_path],
                    cwd=cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception as e:
                logger.debug("Failed to inspect/wake WeChat process: %s", e)

        return False

    @staticmethod
    def _capture_window_state(hwnd: int) -> WindowSnapshot:
        """Capture visibility/minimized state before temporary activation."""
        try:
            return WindowSnapshot(
                hwnd=hwnd,
                was_visible=bool(win32gui.IsWindowVisible(hwnd)),
                was_iconic=bool(win32gui.IsIconic(hwnd)),
            )
        except Exception as e:
            logger.debug("Failed to capture WeChat window state: %s", e)
            return WindowSnapshot(hwnd=hwnd)

    def _restore_window_state(
        self,
        snapshot: WindowSnapshot | None,
        current_hwnd: int | None = None,
    ) -> None:
        """Restore the window if the send pipeline temporarily showed it."""
        if not snapshot:
            return

        hwnd = current_hwnd if current_hwnd and self._validate_hwnd(current_hwnd) else snapshot.hwnd
        if not self._validate_hwnd(hwnd):
            return

        try:
            if snapshot.was_iconic:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                logger.debug("Restored WeChat minimized state after send: HWND=%s", hwnd)
            elif not snapshot.was_visible:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                logger.debug("Restored WeChat hidden state after send: HWND=%s", hwnd)
        except Exception as e:
            logger.debug("Failed to restore WeChat window state after send: %s", e)

    # ── Window activation ─────────────────────────────────────────

    def activate(self, hwnd: int) -> bool:
        _ensure_default_desktop()
        """Activate the WeChat window: restore if iconic, bring to foreground.

        Windows foreground lock: SetForegroundWindow fails with
        ERROR_ACCESS_DENIED (5) unless the calling thread has "foreground
        authority" — granted when the thread generated the most recent
        input event (keyboard/mouse).  Running from a terminal, we have
        zero authority until we simulate input.

        Strategy (layered):
        1. keybd_event priming → acquire foreground authority via fake input
        2. AllowSetForegroundWindow + SetForegroundWindow
        3. AttachThreadInput bypass (cross-integrity)
        4. Alt+Esc cycling (last resort)

        Returns True if the window is now the foreground window.
        """
        if not self._validate_hwnd(hwnd):
            logger.error(f"HWND={hwnd} is invalid, cannot activate")
            self.invalidate_cache()
            return False

        try:
            # Restore if minimized
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)

            # Show and bring to top of Z-order
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOP,
                0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
            win32gui.BringWindowToTop(hwnd)

            # ── Layer 1: Prime foreground authority via keybd_event ──
            # Windows grants SetForegroundWindow permission to the thread
            # that last generated input.  Simulating a keystroke (even a
            # no-op one) into the CURRENT foreground window (the terminal)
            # gives us that authority.  We use a brief Alt tap — it's
            # harmless regardless of what window receives it.
            self._prime_foreground_authority()
            ctypes.windll.user32.AllowSetForegroundWindow(-1)

            # ── Layer 2: SetForegroundWindow ────────────────────────
            try:
                foreground_set = win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                logger.debug("Layer 2 SetForegroundWindow failed: %s", e)
                foreground_set = False
            time.sleep(0.3)
            if self._foreground_matches(hwnd):
                logger.debug("Window activated: HWND=%s is foreground", hwnd)
                return True

            # ── Layer 3: AttachThreadInput bypass ──────────────────
            logger.debug(
                "SetForegroundWindow returned %s, trying AttachThreadInput. %s",
                foreground_set, self.get_foreground_info(),
            )
            self._prime_foreground_authority()
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
            foreground_set = self._force_foreground(hwnd)
            time.sleep(0.3)
            if self._foreground_matches(hwnd):
                logger.info("Window activated via AttachThreadInput: HWND=%s", hwnd)
                return True

            # ── Layer 4: Alt+Esc cycling (last resort) ─────────────
            logger.debug(
                "AttachThreadInput did not help — trying Alt+Esc. %s",
                self.get_foreground_info(),
            )
            self._alt_tab_to_window(hwnd)
            time.sleep(0.5)
            if self._foreground_matches(hwnd):
                logger.info("Window activated via Alt+Esc: HWND=%s", hwnd)
                return True

            logger.warning(
                "All activation methods failed for HWND=%s. %s",
                hwnd, self.get_foreground_info(),
            )
            return False

        except Exception as e:
            logger.error(f"Activation failed for HWND={hwnd}: {e}")
            return False

    @classmethod
    def _alt_tab_to_window(cls, hwnd: int) -> None:
        """Simulate Alt+Tab to switch to a specific window as a last resort.

        This bypasses foreground lock restrictions that block
        SetForegroundWindow across different integrity levels.
        Uses Alt+Esc as a simpler alternative — it activates the next
        window in the Z-order without the Alt+Tab UI.
        """
        try:
            # Alt key down → Esc (activate next window) → Alt up
            # We do this a few times since WeChat might not be immediately next
            for _ in range(8):
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)  # Alt down
                time.sleep(0.02)
                ctypes.windll.user32.keybd_event(0x1B, 0, 0, 0)  # Esc
                time.sleep(0.02)
                ctypes.windll.user32.keybd_event(0x1B, 0, 2, 0)  # Esc up
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)  # Alt up
                time.sleep(0.05)
                if cls._foreground_matches(hwnd):
                    break
        except Exception:
            pass

    @staticmethod
    def _prime_foreground_authority() -> None:
        """Acquire foreground authority by simulating a harmless keystroke.

        Windows grants SetForegroundWindow permission exclusively to the
        thread that generated the most recent input event (keyboard or
        mouse).  When running headless from a terminal, the Python process
        has zero authority — every SetForegroundWindow call returns
        ERROR_ACCESS_DENIED (5) regardless of AllowSetForegroundWindow.

        Simulating a brief Alt key press/release into whichever window
        currently has focus (the terminal, in our case) gives this thread
        the "last input" status.  Alt is harmless: it doesn't type text,
        submit forms, or trigger shortcuts on its own.
        """
        try:
            # Alt down → Alt up (fast, harmless, grants authority)
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)  # Alt down
            time.sleep(0.01)
            ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)  # Alt up
            time.sleep(0.02)
        except Exception:
            pass

    @staticmethod
    def _force_foreground(hwnd: int) -> bool:
        """Use AttachThreadInput to bypass common Windows foreground restrictions.

        Returns True if the window became foreground after our best effort.
        """
        attached_current = False
        attached_foreground = False
        try:
            user32 = ctypes.windll.user32
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)
            if not target_thread:
                logger.debug("Force foreground: no target thread for HWND=%s", hwnd)
                return False

            current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            foreground = user32.GetForegroundWindow()
            foreground_thread = (
                user32.GetWindowThreadProcessId(foreground, None)
                if foreground else 0
            )

            if current_thread != target_thread:
                attached_current = bool(
                    user32.AttachThreadInput(current_thread, target_thread, True)
                )
            if foreground_thread and foreground_thread != target_thread:
                attached_foreground = bool(
                    user32.AttachThreadInput(foreground_thread, target_thread, True)
                )

            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)

            # Allow foreground steal (Windows ≥2000: only works once per process
            # unless we also attached threads above)
            ctypes.windll.user32.AllowSetForegroundWindow(-1)

            try:
                success = bool(win32gui.SetForegroundWindow(hwnd))
            except Exception:
                success = False

            # If SetForegroundWindow failed but we got thread attachments,
            # try one more time after a brief sleep
            if not success and (attached_current or attached_foreground):
                time.sleep(0.15)
                try:
                    success = bool(win32gui.SetForegroundWindow(hwnd))
                except Exception:
                    success = False

            return success
        except Exception as e:
            logger.debug("Force foreground failed for HWND=%s: %s", hwnd, e)
            return False
        finally:
            try:
                user32 = ctypes.windll.user32
                target_thread = user32.GetWindowThreadProcessId(hwnd, None)
                foreground = user32.GetForegroundWindow()
                foreground_thread = (
                    user32.GetWindowThreadProcessId(foreground, None)
                    if foreground else 0
                )
                if attached_foreground and foreground_thread:
                    user32.AttachThreadInput(foreground_thread, target_thread, False)
                if attached_current:
                    user32.AttachThreadInput(
                        ctypes.windll.kernel32.GetCurrentThreadId(),
                        target_thread, False,
                    )
            except Exception:
                pass

    def get_foreground_info(self) -> str:
        """Diagnostic: return info about the current foreground window."""
        try:
            fg = win32gui.GetForegroundWindow()
            if fg:
                title = win32gui.GetWindowText(fg) or ""
                cls = win32gui.GetClassName(fg) or ""
                _, pid = win32process.GetWindowThreadProcessId(fg)
                proc = _get_process_name(pid)
                return f"FG_HWND={fg} title='{title[:50]}' class='{cls}' proc={proc}"
        except Exception:
            pass
        return "FG=unknown"

    # ── Chat navigation ───────────────────────────────────────────

    def _goto_contacts_tab(self, hwnd: int) -> bool:
        """Navigate to the Contacts (通讯录) tab before searching.

        In WeChat's main chat list view, Ctrl+F opens a global search that
        routes to the "搜一搜" (Search) page instead of entering the group
        chat directly.  Navigating to the Contacts tab first changes the
        search context: the search box in Contacts searches specifically
        for contacts and group chats, bypassing the 搜一搜 redirect.

        Uses Ctrl+2 (verified shortcut for WeChat PC Contacts tab).
        Falls back to UIA click if the keyboard shortcut doesn't work.

        Returns True if the tab switch succeeded, False if all methods failed.
        """
        if not self._foreground_matches(hwnd):
            logger.debug("_goto_contacts_tab: HWND not foreground, skipping")
            return False

        # ── Method 1: Ctrl+2 (verified in WeChat PC) ─────────────────
        logger.debug("_goto_contacts_tab: trying Ctrl+2")
        send_combo(0x11, 0x32)  # Ctrl+2
        time.sleep(self.TAB_SWITCH_DELAY)
        hwnd = self._adopt_foreground_hwnd(hwnd, "after Ctrl+2")
        if self._foreground_matches(hwnd):
            logger.info("Contacts tab: Ctrl+2 navigation succeeded")
            return True

        # ── Method 2: try UIA to find Contacts button ───────────────
        try:
            import uiautomation as uia
            root = uia.ControlFromHandle(hwnd)
            if root:
                # Search for the Contacts button by name or automation ID
                contacts_keywords = ("通讯录", "Contacts", "contacts_tab")
                for child, depth in uia.WalkControl(root, maxDepth=6):
                    try:
                        name = (child.Name or "").strip()
                        auto_id = (child.AutomationId or "").strip()
                        if any(kw in name for kw in contacts_keywords) or \
                           any(kw in auto_id for kw in contacts_keywords):
                            child.Click()
                            time.sleep(self.TAB_SWITCH_DELAY)
                            if self._foreground_matches(hwnd):
                                logger.info(
                                    "Contacts tab: UIA click on '%s' succeeded",
                                    name or auto_id,
                                )
                                return True
                    except Exception:
                        pass
        except ImportError:
            logger.debug("_goto_contacts_tab: uiautomation not available")
        except Exception as e:
            logger.debug("_goto_contacts_tab: UIA navigation failed: %s", e)

        logger.warning(
            "_goto_contacts_tab: all methods failed — "
            "will search from current tab (may hit 搜一搜)"
        )
        return False

    def navigate_to_chat(self, hwnd: int, group_name: str) -> bool:
        """Navigate to a specific group chat using keyboard-only input.

        Flow: Contacts tab → Ctrl+F → Ctrl+A → paste name → Enter.
        The Contacts-tab-first approach avoids the "搜一搜" global search
        redirect that occurs when searching from the main chat list.
        If Contacts tab navigation fails, falls back to searching from
        the current view.
        """
        if not group_name:
            logger.error("navigate_to_chat: empty group_name")
            return False

        if not self._validate_hwnd(hwnd):
            logger.error(f"navigate_to_chat: invalid HWND={hwnd}")
            return False

        if not self._foreground_matches(hwnd):
            adopted = self._current_wechat_foreground_hwnd()
            if adopted:
                hwnd = self._adopt_foreground_hwnd(hwnd, "before navigation")
            else:
                logger.error(
                    "navigate_to_chat: WeChat is not the active foreground window. "
                    f"{self.get_foreground_info()}"
                )
                return False

        if not self._foreground_matches(hwnd):
            logger.error(
                "navigate_to_chat: WeChat is not the active foreground window. "
                f"{self.get_foreground_info()}"
            )
            return False

        logger.info(
            "Navigating to chat: '%s' (HWND=%s, keyboard-only)", group_name, hwnd,
        )

        # Phase 0: Navigate to Contacts tab to avoid 搜一搜 redirect.
        # When searching from the Contacts tab, the search box finds
        # contacts and group chats without routing through 搜一搜.
        self._goto_contacts_tab(hwnd)
        hwnd = self._adopt_foreground_hwnd(hwnd, "after contacts tab")

        # Phase 1: Ctrl+F to focus search box
        send_combo(0x11, 0x46)  # Ctrl+F
        time.sleep(self.SEARCH_FOCUS_DELAY)
        hwnd = self._adopt_foreground_hwnd(hwnd, "after Ctrl+F")
        if not self._validate_hwnd(hwnd):
            logger.error("navigate_to_chat: HWND became invalid after Ctrl+F")
            return False

        # Phase 2: Select any existing search text and replace with group name
        send_combo(0x11, 0x41)  # Ctrl+A
        time.sleep(self.PASTE_DELAY)
        self._clipboard_text(group_name, hwnd)
        time.sleep(self.CLIPBOARD_DELAY)
        send_combo(0x11, 0x56)  # Ctrl+V
        time.sleep(self.SEARCH_POPULATE_DELAY)  # wait for search results to populate
        hwnd = self._adopt_foreground_hwnd(hwnd, "after search paste")
        if not self._validate_hwnd(hwnd):
            logger.error("navigate_to_chat: HWND became invalid after search paste")
            return False

        # Phase 3: Press Enter to select the first search result
        press_key(0x0D)  # Enter
        time.sleep(self.SELECT_RESULT_DELAY)
        hwnd = self._adopt_foreground_hwnd(hwnd, "after search Enter")
        if not self._validate_hwnd(hwnd):
            logger.error("navigate_to_chat: HWND became invalid after search Enter")
            return False

        if not self._verify_chat_title(hwnd, group_name):
            # UIA may be unavailable (2-node skeleton). Accept as long as
            # foreground stayed with us through every step.
            if self._foreground_matches(hwnd):
                logger.warning(
                    "Navigation to '%s' completed via keyboard; "
                    "UIA title verification unavailable.", group_name,
                )
            else:
                logger.error(
                    "Navigation to '%s' lost foreground. %s",
                    group_name, self.get_foreground_info(),
                )
                return False

        return hwnd

    def _verify_chat_title(self, hwnd: int, group_name: str) -> bool:
        """Try to verify we're in the right chat using window title or UIA."""
        # Check window title for group name
        try:
            title = win32gui.GetWindowText(hwnd) or ""
            if group_name in title:
                return True
        except Exception:
            pass

        # Try UIA
        try:
            import uiautomation as uia
            root = uia.ControlFromHandle(hwnd)
            if root:
                for child, depth in uia.WalkControl(root, maxDepth=4):
                    try:
                        name = child.Name or ""
                        if group_name in name:
                            logger.debug(f"UIA found '{group_name}' at depth {depth}")
                            return True
                    except Exception:
                        pass
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"UIA verify failed: {e}")

        return False

    # ── Internals ─────────────────────────────────────────────────

    def send_message(self, hwnd: int, text: str, file_path: str | None = None) -> bool:
        """Send a text message via keyboard only: Ctrl+V → Enter.

        The caller MUST have already navigated to the correct chat and
        ensured input area focus (via Tab in navigate_to_chat).  With
        ``file_path`` the file is pasted instead of ``text`` (a label for logs).
        """
        if not text:
            logger.warning("send_message: empty text")
            return False

        if not self._validate_hwnd(hwnd):
            logger.error(f"send_message: invalid HWND={hwnd}")
            return False

        if not self._foreground_matches(hwnd):
            logger.warning(
                "send_message: WeChat not foreground before send. "
                "Attempting re-activation. %s",
                self.get_foreground_info(),
            )
            if not self.activate(hwnd):
                logger.error(
                    "send_message: re-activation failed. %s",
                    self.get_foreground_info(),
                )
                return False
            # Re-validate HWND after activation (WeChat may have recreated it)
            active = self._current_wechat_foreground_hwnd()
            if active:
                hwnd = self._adopt_foreground_hwnd(hwnd, "after send re-activation")

        logger.info("Sending message: %d chars to HWND=%s (keyboard-only)", len(text), hwnd)

        # Set clipboard and paste
        if file_path:
            if not self._clipboard_file(file_path, hwnd):
                logger.error("send_message: could not put %s on the clipboard", file_path)
                return False
        else:
            self._clipboard_text(text, hwnd)
        time.sleep(self.PASTE_DELAY)

        if not self._foreground_matches(hwnd):
            logger.warning(
                "send_message: foreground changed before paste; "
                "re-activating. %s",
                self.get_foreground_info(),
            )
            if not self.activate(hwnd):
                logger.error(
                    "send_message: re-activation before paste failed. %s",
                    self.get_foreground_info(),
                )
                return False
            active = self._current_wechat_foreground_hwnd()
            if active:
                hwnd = self._adopt_foreground_hwnd(hwnd, "after paste re-activation")

        send_combo(0x11, 0x56)  # Ctrl+V
        time.sleep(self.PASTE_SEND_DELAY + (self.FILE_PASTE_DELAY if file_path else 0))

        hwnd = self._adopt_foreground_hwnd(hwnd, "after paste")
        if not self._foreground_matches(hwnd):
            logger.warning(
                "send_message: foreground changed before Enter; "
                "re-activating. %s",
                self.get_foreground_info(),
            )
            if not self.activate(hwnd):
                logger.error(
                    "send_message: re-activation before Enter failed. %s",
                    self.get_foreground_info(),
                )
                return False
            active = self._current_wechat_foreground_hwnd()
            if active:
                hwnd = self._adopt_foreground_hwnd(hwnd, "after enter re-activation")

        # Press Enter to send
        press_key(0x0D)  # Enter
        time.sleep(self.ENTER_SEND_DELAY)

        logger.info("Message send action completed: %d chars", len(text))
        return True

    # ── Full send pipeline ────────────────────────────────────────

    def send_to_chat(self, group_name: str, text: str,
                     max_retries: int = 2) -> bool:
        """Complete send pipeline: find → activate → navigate → send.

        This is the main entry point for sending a message.
        Every step is verified; failures are logged.

        Args:
            group_name: Target group chat name.
            text: Message content.
            max_retries: Max retry attempts on failure.

        Returns:
            True if successfully sent.
        """
        if not group_name or not text:
            logger.error("send_to_chat: empty group_name or text")
            return False
        return self._run_send(group_name, text, max_retries)

    def send_file_to_chat(self, group_name: str, path: str, max_retries: int = 2) -> bool:
        """Send a file the same way: paste it, then Enter (a sticker, a relay result)."""
        if not group_name or not path or not Path(path).is_file():
            logger.error("send_file_to_chat: no group name or no file at %r", path)
            return False
        return self._run_send(group_name, f"[file] {Path(path).name}", max_retries, file_path=str(path))

    def _run_send(self, group_name: str, text: str, max_retries: int,
                  file_path: str | None = None) -> bool:
        if self._quiet is None:
            return self._send_to_chat(group_name, text, max_retries, file_path=file_path)
        session = self._begin_quiet()
        if session is None:
            return False
        try:
            return self._send_to_chat(group_name, text, max_retries, file_path=file_path)
        finally:
            self._end_quiet(session)

    def _send_to_chat(self, group_name: str, text: str, max_retries: int,
                      file_path: str | None = None) -> bool:
        _ensure_default_desktop()
        state_to_restore: WindowSnapshot | None = None
        last_hwnd: int | None = None

        for attempt in range(max_retries):
            if attempt > 0:
                logger.info(f"Retry attempt {attempt + 1}/{max_retries}")
                self.invalidate_cache()
                time.sleep(1.0)

            # Quiet send: a group open in its own window needs no search.
            chat_hwnd = self.find_chat_window(group_name) if self._session else None
            if chat_hwnd:
                if self._send_in_chat_window(chat_hwnd, group_name, text, file_path):
                    return True
                self._log_failure(group_name, text, "chat window send failed", chat_hwnd)
                continue

            hwnd = self.find_hwnd(force=(attempt > 0))
            if not hwnd:
                self._log_failure(group_name, text, "no WeChat window found")
                continue

            last_hwnd = hwnd
            if state_to_restore is None:
                state_to_restore = self._capture_window_state(hwnd)

            if not self._activate_for_send(hwnd):
                self._log_failure(group_name, text, "window activation failed",
                                  hwnd)
                continue

            if self._looks_like_blank_window(hwnd):
                self._log_failure(group_name, text, "WeChat window is blank/white", hwnd)
                logger.error(
                    "WeChat window appears blank/white; refusing to send. "
                    "Restart WeChat before retrying."
                )
                continue

            nav_result = self.navigate_to_chat(hwnd, group_name)
            if not nav_result:
                self._log_failure(group_name, text, "navigation failed", hwnd)
                continue
            if type(nav_result) is int:
                hwnd = nav_result
            else:
                hwnd = self._adopt_foreground_hwnd(hwnd, "after navigation")
            last_hwnd = hwnd

            sent = (self.send_message(hwnd, text, file_path=file_path) if file_path
                    else self.send_message(hwnd, text))
            if not sent:
                self._log_failure(group_name, text, "send failed", hwnd)
                continue

            logger.info(
                f"send_to_chat SUCCESS: group='{group_name}' "
                f"({len(text)} chars) HWND={hwnd}"
            )
            self._restore_window_state(state_to_restore, hwnd)
            return True

        logger.error(
            f"send_to_chat FAILED after {max_retries} attempts: "
            f"group='{group_name}'"
        )
        self._restore_window_state(state_to_restore, last_hwnd)
        return False

    # ── Quiet send (SEND_QUIET=on) ────────────────────────────────

    def find_chat_window(self, group_name: str) -> Optional[int]:
        """A WeChat window of its own showing this chat (title = chat name), or None."""
        _ensure_default_desktop()
        name = (group_name or "").strip()
        if not name or name in MAIN_WINDOW_TITLES:
            return None
        found: list[int] = []

        def _enum(hwnd, _ctx):
            try:
                if not title_names_chat(win32gui.GetWindowText(hwnd) or "", name):
                    return True
                if not (win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd)):
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if _get_process_name(pid) in WECHAT_PROCESS_NAMES:
                    found.append(hwnd)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_enum, None)
        except Exception as e:
            logger.debug("Chat window scan failed: %s", e)
            return None
        if not found:
            return None
        hwnd = found[0]
        self._chat_windows = {h for h in self._chat_windows if self._validate_hwnd(h)}
        self._chat_windows.add(hwnd)
        if self._cached_hwnd == hwnd:
            self.invalidate_cache()
        return hwnd

    def describe_chat_windows(self, group_names) -> None:
        """Log, per group, whether quiet sends can skip the search."""
        if self._quiet is None:
            return
        for name in group_names:
            hwnd = self.find_chat_window(name)
            if hwnd:
                logger.info("Quiet send: %r is open in its own window (HWND=%s); replies are typed there",
                            name, hwnd)
            else:
                logger.info("Quiet send: %r has no window of its own; replies go through the "
                            "search in the main window (open the chat in its own window to skip it)", name)

    def _send_in_chat_window(self, hwnd: int, group_name: str, text: str,
                             file_path: str | None = None) -> bool:
        """Paste and send in a chat's own window: no tab switch, no search."""
        _ensure_default_desktop()
        snapshot = self._capture_window_state(hwnd)
        try:
            if not self._bring_forward(hwnd):
                return False
            if file_path:
                if not self._clipboard_file(file_path, hwnd):
                    logger.error("Could not put %s on the clipboard", file_path)
                    return False
            else:
                self._clipboard_text(text, hwnd)
            time.sleep(self.PASTE_DELAY)
            if not self._foreground_matches(hwnd):
                logger.warning("Chat window lost the foreground before paste. %s",
                               self.get_foreground_info())
                return False
            # Ctrl+A first: a retry replaces its own earlier paste instead of
            # adding to it, and a half-typed draft is never sent along.
            send_combo(0x11, 0x41)  # Ctrl+A
            send_combo(0x11, 0x56)  # Ctrl+V
            time.sleep(self.PASTE_SEND_DELAY + (self.FILE_PASTE_DELAY if file_path else 0))
            if not self._foreground_matches(hwnd) and not self._bring_forward(hwnd):
                logger.error("Chat window lost the foreground before Enter. %s",
                             self.get_foreground_info())
                return False
            press_key(0x0D)  # Enter
            time.sleep(self.ENTER_SEND_DELAY)
            logger.info("send_to_chat SUCCESS: group='%s' (%d chars) in its own window HWND=%s",
                        group_name, len(text), hwnd)
            return True
        finally:
            self._restore_window_state(snapshot, hwnd)

    def _activate_for_send(self, hwnd: int) -> bool:
        return self._bring_forward(hwnd) if self._session is not None else self.activate(hwnd)

    def _bring_forward(self, hwnd: int) -> bool:
        """Bring a WeChat window to the front without the Alt tap; fall back to activate()."""
        _ensure_default_desktop()
        if not self._validate_hwnd(hwnd):
            return False
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.15)
            elif not win32gui.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        except Exception as e:
            logger.debug("Showing HWND=%s failed: %s", hwnd, e)
        if self._foreground_matches(hwnd):
            return True
        try:
            self._desktop.bring_forward(hwnd)
        except Exception as e:
            logger.debug("Key-free activation failed for HWND=%s: %s", hwnd, e)
        time.sleep(self.QUIET_ACTIVATE_DELAY)
        if self._foreground_matches(hwnd):
            return True
        logger.info("Key-free activation did not work; using the Alt-key activation. %s",
                    self.get_foreground_info())
        return self.activate(hwnd)

    def _begin_quiet(self) -> QuietSession | None:
        """Wait for the user to pause, then note what the send will change."""
        if not self._idle_gate.wait(alive=self.alive):
            logger.info("Bot stopped while waiting for the user to pause; nothing sent")
            return None
        session = QuietSession()
        try:
            session.previous_foreground = self._desktop.foreground_window()
        except Exception as e:
            logger.debug("Foreground window unavailable: %s", e)
        if self._quiet.keep_clipboard:
            try:
                session.clipboard = self._desktop.read_clipboard()
            except Exception as e:
                logger.debug("Clipboard could not be saved: %s", e)
        self._session = session
        return session

    def _end_quiet(self, session: QuietSession) -> None:
        """Give back the focus, then the clipboard once WeChat has used it."""
        self._session = None
        try:
            processed = self._desktop.wait_until_processed(session.target_hwnd)
        except Exception as e:
            logger.debug("WeChat did not answer: %s", e)
            processed = False
        self._return_focus(session.previous_foreground)
        if session.clipboard is not None and session.clipboard_sequence is not None:
            self._restore_clipboard(session, processed)
        try:
            self._idle_gate.mark_own_input()
        except Exception:
            pass

    def _return_focus(self, previous: int) -> None:
        """Back to the user's window, unless the user went somewhere else meanwhile."""
        if not previous:
            return
        try:
            current = self._desktop.foreground_window()
            if current == previous:
                return
            if current and not self._validate_hwnd(current):
                return
            if not self._desktop.is_window(previous):
                return
            self._desktop.bring_forward(previous)
        except Exception as e:
            logger.debug("Focus could not be returned to HWND=%s: %s", previous, e)

    def _restore_clipboard(self, session: QuietSession, processed: bool) -> None:
        if not processed:
            # WeChat might still paste later; the user's clipboard must not be
            # what it pastes.
            logger.info("WeChat did not confirm the paste; the reply stays on the clipboard")
            return
        time.sleep(self.CLIPBOARD_RESTORE_DELAY)
        try:
            if self._desktop.clipboard_sequence() != session.clipboard_sequence:
                logger.info("The clipboard changed during the send; left as it is")
                return
            if self._desktop.write_clipboard(session.clipboard):
                logger.debug("Clipboard restored (%d formats)", len(session.clipboard))
        except Exception as e:
            logger.debug("Clipboard could not be restored: %s", e)

    def capture_chat_media(self, group_name: str) -> str:
        """Capture only the visible conversation pane for vision fallback.

        The caller is responsible for deleting the returned temporary file
        after the vision request finishes.
        """
        if not group_name:
            return ""
        if self._quiet is None:
            return self._capture_chat_media(group_name)
        session = self._begin_quiet()
        if session is None:
            return ""
        try:
            return self._capture_chat_media(group_name)
        finally:
            self._end_quiet(session)

    def _capture_chat_media(self, group_name: str) -> str:
        chat_hwnd = self.find_chat_window(group_name) if self._session else None
        hwnd = chat_hwnd or self.find_hwnd(force=True)
        if not hwnd:
            logger.warning("Vision capture skipped: no WeChat window")
            return ""
        snapshot = self._capture_window_state(hwnd)
        try:
            if chat_hwnd:
                if not self._bring_forward(chat_hwnd):
                    logger.warning("Vision capture skipped: cannot show the window of %r", group_name)
                    return ""
            elif not self._activate_for_send(hwnd) or not self.navigate_to_chat(hwnd, group_name):
                logger.warning("Vision capture skipped: cannot navigate to %r", group_name)
                return ""
            time.sleep(0.25)
            if not chat_hwnd:
                hwnd = self._adopt_foreground_hwnd(hwnd, "vision capture")
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
                return ""
            bbox = _conversation_bbox(left, top, right, bottom, sidebar=not chat_hwnd)
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                return ""
            image = ImageGrab.grab(bbox=bbox).convert("RGB")
            VISION_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = VISION_SCREENSHOT_DIR / f"chat-{int(time.time())}-{uuid.uuid4().hex}.jpg"
            image.save(path, "JPEG", quality=85, optimize=True)
            logger.info("Vision capture created for chat=%r", group_name)
            return str(path)
        except Exception:
            logger.exception("Vision capture failed for chat=%r", group_name)
            return ""
        finally:
            self._restore_window_state(snapshot, hwnd)

    # ── Internals ─────────────────────────────────────────────────


    @staticmethod
    def _foreground_root(hwnd: int) -> int:
        try:
            return win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        except Exception:
            return hwnd

    @classmethod
    def _foreground_matches(cls, hwnd: int) -> bool:
        """Return True only for the target main window or rooted child windows."""
        try:
            fg = win32gui.GetForegroundWindow()
            if not fg:
                return False
            return fg == hwnd or cls._foreground_root(fg) == hwnd
        except Exception:
            return False

    def _current_wechat_foreground_hwnd(self) -> Optional[int]:
        """Return the current usable foreground WeChat main-window HWND.

        Validates via process name (wechat.exe/weixin.exe), not class name —
        WeChat has used Qt (Qt51514QWindowIcon), CEF (Chrome_WidgetWin_*), and
        other frameworks across versions.
        """
        try:
            fg = win32gui.GetForegroundWindow()
            if not fg:
                return None
            candidates = [fg, self._foreground_root(fg)]
            for candidate in candidates:
                if not candidate or not self._validate_hwnd(candidate):
                    continue
                c = _score_window(candidate)
                width = c.rect[2] - c.rect[0]
                height = c.rect[3] - c.rect[1]
                if (
                    c.visible
                    and width >= MIN_WINDOW_WIDTH
                    and height >= MIN_WINDOW_HEIGHT
                ):
                    return candidate
        except Exception:
            return None
        return None

    @staticmethod
    def _looks_like_blank_window(hwnd: int) -> bool:
        """Detect a white Qt render surface before sending into a bad window."""
        try:
            if not WeChatWindowController._foreground_matches(hwnd):
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.BringWindowToTop(hwnd)
                    ctypes.windll.user32.AllowSetForegroundWindow(-1)
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.2)
                except Exception:
                    pass
            if not WeChatWindowController._foreground_matches(hwnd):
                logger.debug("blank window detection skipped: target not foreground")
                return False

            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = right - left
            height = bottom - top
            if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
                return False

            margin_x = min(120, width // 5)
            margin_y = min(100, height // 5)
            bbox = (
                left + margin_x,
                top + margin_y,
                right - margin_x,
                bottom - margin_y,
            )
            image = ImageGrab.grab(bbox=bbox).convert("L")
            stat = ImageStat.Stat(image)
            mean = stat.mean[0]
            stddev = stat.stddev[0]
            return (
                mean >= WHITE_SCREEN_MEAN_THRESHOLD
                and stddev <= WHITE_SCREEN_STDDEV_THRESHOLD
            )
        except Exception as e:
            logger.debug("blank window detection failed: %s", e)
            return False

    def _adopt_foreground_hwnd(self, hwnd: int, context: str) -> int:
        """Switch to a new foreground WeChat HWND when WeChat recreates it."""
        active = self._current_wechat_foreground_hwnd()
        if active and active != hwnd and active not in self._chat_windows:
            logger.info(
                "WeChat foreground HWND changed during %s: %s -> %s",
                context, hwnd, active,
            )
            self._cached_hwnd = active
            self._cached_at = time.time()
            return active
        return hwnd

    def _clipboard_text(self, text: str, hwnd: int) -> None:
        """Put text on the clipboard for a paste into ``hwnd``.

        In a quiet send the text stays out of the clipboard history, and the
        session notes the write so the user's clipboard can be put back.
        """
        session = self._session
        if session is None:
            self._set_clipboard(text)
            return
        self._set_clipboard(text, private=True)
        self._note_clipboard_write(session, hwnd)

    def _clipboard_file(self, path: str, hwnd: int) -> bool:
        """Put a picture on the clipboard (as the file, or as a still image); False if it failed.

        A failure must stop the send: the paste would repeat whatever text is
        still on the clipboard.
        """
        session = self._session
        # STICKER_PASTE=image turns pictures into still images; any other file (a relay
        # result) is always pasted as the file.
        as_image = self._paste_files_as == "image" and Path(path).suffix.lower() in _PICTURE_SUFFIXES
        ok = self._set_clipboard_file(path, as_image=as_image, private=session is not None)
        if session is not None:
            # Noted even after a failure: it may have emptied the clipboard, and
            # the saved one goes back afterwards (nothing was pasted).
            self._note_clipboard_write(session, hwnd)
        return ok

    def _note_clipboard_write(self, session: QuietSession, hwnd: int) -> None:
        session.target_hwnd = hwnd
        try:
            session.clipboard_sequence = self._desktop.clipboard_sequence()
        except Exception as e:
            logger.debug("Clipboard sequence unavailable: %s", e)
            session.clipboard_sequence = None

    @staticmethod
    def _set_clipboard_file(path: str, as_image: bool = False, private: bool = False) -> bool:
        try:
            data = _dib_bytes(path) if as_image else _dropfiles_bytes(path)
        except Exception as e:
            logger.error("Cannot read %s: %s", path, e)
            return False
        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                if as_image:
                    win32clipboard.SetClipboardData(win32con.CF_DIB, data)
                else:
                    win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
                    win32clipboard.SetClipboardData(
                        win32clipboard.RegisterClipboardFormat("Preferred DropEffect"),
                        struct.pack("<I", 1),                # DROPEFFECT_COPY, as Explorer's Copy
                    )
                if private:
                    try:
                        for name in CLIPBOARD_PRIVACY_FORMATS:
                            win32clipboard.SetClipboardData(
                                win32clipboard.RegisterClipboardFormat(name), b"\0\0\0\0",
                            )
                    except Exception as e:
                        logger.debug("Clipboard history opt-out failed: %s", e)
                win32clipboard.CloseClipboard()
                return True
            except Exception:
                time.sleep(0.1)
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        return False

    @staticmethod
    def _set_clipboard(text: str, private: bool = False) -> None:
        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                if private:
                    try:
                        for name in CLIPBOARD_PRIVACY_FORMATS:
                            win32clipboard.SetClipboardData(
                                win32clipboard.RegisterClipboardFormat(name), b"\0\0\0\0",
                            )
                    except Exception as e:
                        logger.debug("Clipboard history opt-out failed: %s", e)
                win32clipboard.CloseClipboard()
                return
            except Exception:
                time.sleep(0.1)
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

    @staticmethod
    def _log_failure(group: str, text: str, reason: str,
                     hwnd: Optional[int] = None) -> None:
        """Log a send failure to both the logger and data/send_failures.log."""
        fg_info = ""
        try:
            fg = win32gui.GetForegroundWindow()
            fg_title = win32gui.GetWindowText(fg) or ""
            fg_cls = win32gui.GetClassName(fg) or ""
            fg_info = f"FG: HWND={fg} title='{fg_title[:50]}' class='{fg_cls}'"
        except Exception:
            fg_info = "FG: unknown"

        msg = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"group='{group}' reason='{reason}' "
            f"hwnd={hwnd} text_len={len(text)} {fg_info}\n"
        )

        logger.error(f"Send failure: {msg.strip()}")

        try:
            with open(FAILURE_LOG, "a", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
