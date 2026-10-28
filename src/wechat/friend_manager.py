"""Friend manager: accepting friend requests and adding friends in the WeChat PC window.

It drives the real window with keystrokes and mouse clicks and finds the buttons by
OCR, so every step is guarded:

- the window lock of the send path is shared, so a friend action never interleaves
  with a reply being typed;
- nothing is typed or clicked unless the WeChat window is in the foreground;
- nothing is clicked blind: a button OCR cannot find ends the action with a reason (the
  exceptions are the few WeChat PC 4.0 steps that have no text to read, see
  _add_friend_wechat4);
- an action is only reported as done when the screen shows it (the button is gone,
  the dialog is closed).

Features:
- accept_friend_requests: Contacts -> 新的朋友, accepts the pending requests (at most
  MAX_ACCEPT_PER_RUN per call); each accept is told to ``on_accepted`` (when, and the text
  beside its button) so the new-friend scan can say which friends the bot let in.
- add_friend: searches the wxid / phone number, opens the card, "添加到通讯录", fills the
  greeting and sends the application.  Without OCR it does not start.  On the single-window
  UI everything is found on the screen (the search box, every button, every dialog).  On
  WeChat PC 4.0 the add dialog and the application dialog are windows of their own: they are
  found by window class / size / title and read by handle, the (+) button and the input boxes
  are clicked by position, and so is the confirm button when OCR does not read it.
- AutoFriendService: periodic background worker for accept_friend_requests.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator

from .keyboard import press_key, send_combo
from .quiet_send import title_names_chat
from .rail_watch import RailWatcher

logger = logging.getLogger(__name__)

VK_BACK = 0x08
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_A = 0x41
VK_V = 0x56

MAX_ACCEPT_PER_RUN = 10      # a burst of requests is taken in a few runs, not all at once
MAX_KEYWORD_CHARS = 64
MIN_INTERVAL_SEC = 10        # same floor as AUTO_ACCEPT_INTERVAL_SEC in config validation
POSTPONE_RETRY_SEC = 15      # while the user is busy, look again this often (not once per interval)
SAME_SPOT = 0.02             # two OCR rows whose centres differ by less are "the same spot"
SEARCH_BOX_MAX_X0 = 0.40     # where the OCR reader looks for the search box's "搜索": top left
SEARCH_BOX_MAX_Y0 = 0.12

NEW_FRIENDS_LABELS = ("新的朋友", "好友申请")
ACCEPT_LABELS = ("接受", "通过", "同意", "前往验证")
PENDING_REQUEST_LABELS = ("等待验证",)
ACCEPT_MIN_X0 = 0.40         # the accept button sits on the right side of its row
CONFIRM_LABELS = ("确定", "完成")
SEARCH_ENTRY_LABELS = ("网络查找", "查找微信号", "查找手机号")
NOT_FOUND_MARKERS = ("该用户不存在", "用户不存在", "查找失败", "无法找到该用户", "请检查你填写的账号是否正确")
ALREADY_FRIEND_MARKERS = ("音视频通话", "语音聊天", "视频聊天", "朋友资料")   # next to "发消息" on a friend's card
ADD_BUTTON_LABELS = ("添加到通讯录", "加为朋友", "加为联系人")
SEND_LABELS = ("发送", "确定", "完成")
ADD_DIALOG_TITLES = ("添加朋友", "Add Friend")   # title of WeChat 4.0's add-friend window (read on a real machine);
                                                # exact, because the application window is "申请添加朋友"
# WeChat 4.0's other dialogs, matched exactly (a chat opened in a window of its own has the same process and
# class, and its title is the chat's name: 「达人朋友群」, 「小朋友」 must never be taken for one).
APPLY_DIALOG_TITLES = ("申请添加朋友",)         # the application window (probe-apply.txt, real machine 2026-10-23)
ACCEPT_DIALOG_TITLES = ("通过朋友验证",)         # what 「前往验证」 opens (its title not read on a real machine yet:
                                                # a window that appears with another title is logged, never used)
DIALOG_CLASS = "Qt51514QWindowIcon"
DIALOG_LOOKS = 10                                # a dialog is looked for this many times, 0.1 s apart


class _Abort(Exception):
    """The window action cannot go on safely (WeChat lost the foreground, a click failed)."""


class AcceptResult(list):
    """What ``accept_friend_requests`` returns: one ``{"status": "accepted", "timestamp"}`` per request
    accepted (a list, as before), and ``error``: why the run ended on an unexpected error (a pywin32
    error, a window handle gone; logged with its traceback), "" otherwise."""

    error: str = ""


def _clean_text(text) -> str:
    """Strip all whitespace and lower-case, so OCR spacing never breaks a match."""
    return "".join(str(text or "").split()).lower()


def _is_search_placeholder(text) -> bool:
    """Exactly the search field's placeholder ("搜索" / "Search"), also when the magnifier is read as a letter.

    Stricter than the OCR reader's test on purpose: this one decides where to click.
    """
    t = _clean_text(text).lstrip("0123456789q⌕🔍o")
    return t in ("搜索", "search")


def _centre(row: dict) -> tuple[float, float]:
    return (row["x0"] + row["x1"]) / 2, (row["y0"] + row["y1"]) / 2


def _find_row(rows: list[dict], *labels: str, exact: bool = False, min_x0: float = 0.0) -> dict | None:
    """The topmost OCR row whose text is (exact) or contains one of ``labels``."""
    found = []
    for row in rows:
        text = _clean_text(row.get("t", ""))
        if row.get("x0", 0.0) < min_x0:
            continue
        if (text in labels) if exact else any(label in text for label in labels):
            found.append(row)
    return min(found, key=lambda r: r.get("y0", 0.0)) if found else None


def _still_there(rows: list[dict], row: dict) -> bool:
    """Is a row with the same text still at the spot of ``row``?"""
    text = _clean_text(row.get("t", ""))
    cx, cy = _centre(row)
    for other in rows:
        if _clean_text(other.get("t", "")) != text:
            continue
        ox, oy = _centre(other)
        if abs(ox - cx) < SAME_SPOT and abs(oy - cy) < SAME_SPOT:
            return True
    return False


def _row_label(rows: list[dict], button: dict) -> str:
    """The text left of an accept button on its row of 新的朋友 (the requester's name and greeting as
    OCR read them).  Only to tell which new friend the bot let in (src/wechat/new_friends.py); it may
    hold a phone number, so it is never logged."""
    top, bottom = float(button.get("y0", 0.0)), float(button.get("y1", 0.0))
    reach = max(0.0, bottom - top)
    parts = [r for r in rows if r is not button
             and float(r.get("x1", 1.0)) <= float(button.get("x0", 0.0)) + SAME_SPOT
             and float(r.get("y1", 0.0)) >= top - reach and float(r.get("y0", 1.0)) <= bottom + reach
             and _clean_text(r.get("t", "")) not in ACCEPT_LABELS]
    parts.sort(key=lambda r: (r.get("y0", 0.0), r.get("x0", 0.0)))
    return " ".join(str(r.get("t", "")).strip() for r in parts)[:200]


def _failure(target: str, reason: str, message: str) -> dict:
    return {"ok": False, "target": target, "reason": reason, "message": message}


def print_main_window(hwnd: int):
    """A picture of the window taken without bringing it forward (Windows only)."""
    from .ocr.win_ocr import print_window
    return print_window(hwnd)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """The window rectangle in screen pixels (Windows only)."""
    import win32gui
    return win32gui.GetWindowRect(hwnd)


def click_screen(x: int, y: int) -> bool:
    """Send a left mouse click at absolute screen coordinates."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import win32con
        from .keyboard import _ensure_default_desktop
        _ensure_default_desktop()
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True
    except Exception as e:
        logger.warning("click_screen(%s, %s) failed: %s", x, y, e)
        return False


class FriendManager:
    """Accepts friend requests and sends new ones in the WeChat PC window.

    ``ui_lock`` is the lock the send path holds while it types (the backend's
    window lock); without one the manager only excludes itself.  ``clicker``,
    ``rect_of`` and ``sleeper`` are seams for tests.  ``on_accepted(at, label)`` hears every
    request it accepted (AUTO_ACCEPT_FRIENDS and the admin's 同意好友 both come here): when, and
    what OCR read beside its button (the new-friend scan tells the bot's accepts from a person's).
    ``chat_names()`` lists the chats' names (the backend's ``chat_names``): a window titled like one of
    them is a chat open in a window of its own, never one of WeChat's dialogs.
    """

    def __init__(
        self,
        window_controller=None,
        ocr_engine=None,
        ui_lock=None,
        clicker: Callable[[int, int], bool] | None = None,
        rect_of: Callable[[int], tuple[int, int, int, int]] = window_rect,
        sleeper: Callable[[float], None] = time.sleep,
        grab_window: Callable[[int], object] = print_main_window,
        on_accepted: Callable[[float, str], None] | None = None,
        chat_names: Callable[[], Iterable[str]] | None = None,
    ):
        self._window = window_controller
        self._ocr = ocr_engine
        self._ui_lock = ui_lock if ui_lock is not None else threading.Lock()
        self._clicker = clicker or click_screen
        self._rect_of = rect_of
        self._sleep = sleeper
        self._grab_window = grab_window
        self._rail = RailWatcher(self._grab_main_window)
        self._on_accepted = on_accepted
        self._chat_names = chat_names

    def _tell_accepted(self, at: float, label: str) -> None:
        if self._on_accepted is None:
            return
        try:
            self._on_accepted(at, label)
        except Exception:
            logger.warning("Friend management: an accept could not be written down", exc_info=True)

    # ── Looking without touching the screen ─────────────────────────────

    def look_for_news(self) -> str:
        """Has a red badge appeared in WeChat's left rail since the last clean check?

        ``"changed"`` / ``"unchanged"`` / ``"unknown"`` (the window cannot be pictured: minimized,
        not found).  Only a picture of the window is taken: nothing is brought forward,
        clicked or typed, so the user sees nothing.
        """
        return self._rail.look() if self._window is not None else RailWatcher.UNKNOWN

    def _grab_main_window(self):
        hwnd = self._window.find_hwnd(wake=False)
        if not hwnd:
            raise RuntimeError("the WeChat window was not found")
        return self._grab_window(hwnd)

    # ── Window session ──────────────────────────────────────────────────

    @contextmanager
    def _ui_session(self) -> Iterator[int | None]:
        """The WeChat window in front, under the shared lock; ``None`` if it cannot be shown.

        The window is put back the way it was found (minimized / hidden) and the focus goes back
        to the window the user was in.
        """
        with self._ui_lock:
            window = self._window
            hwnd = window.find_hwnd(force=True) if window is not None else None
            if not hwnd:
                logger.warning("Friend management: the WeChat window was not found")
                yield None
                return
            snapshot = window._capture_window_state(hwnd)
            previous = self._foreground_window()
            try:
                if window._activate_for_send(hwnd):
                    yield hwnd
                else:
                    logger.warning("Friend management: WeChat could not be brought to the front")
                    yield None
            finally:
                try:
                    window._restore_window_state(snapshot, hwnd)
                except Exception as e:
                    logger.warning("Friend management: the WeChat window could not be put back: %s", e)
                self._return_focus(previous)

    def _foreground_window(self) -> int:
        try:
            return int(self._window._desktop.foreground_window() or 0)
        except Exception:
            return 0

    def _return_focus(self, previous: int) -> None:
        """Back to the user's window (the controller leaves it alone if the user went elsewhere meanwhile)."""
        if not previous:
            return
        try:
            self._window._return_focus(previous)
        except Exception as e:
            logger.debug("Friend management: focus could not be returned to %s: %s", previous, e)

    def _foreground(self, hwnd: int, exact: bool = False) -> None:
        """The window must be in front.  ``exact`` (a dialog of its own): that very window, not just any window
        of WeChat's process (a chat open in a window of its own is one too, and keys sent to it go out)."""
        if exact:
            if not self._window._foreground_matches(hwnd):
                raise _Abort(f"the dialog (HWND={hwnd}) is not the window in front")
            return
        if not self._is_wechat_foreground(hwnd):
            raise _Abort("the WeChat window is not in the foreground")

    def _is_wechat_foreground(self, hwnd: int) -> bool:
        if self._window._foreground_matches(hwnd):
            return True
        if sys.platform == "win32":
            try:
                import win32gui, win32process
                fg = win32gui.GetForegroundWindow()
                if fg and win32gui.IsWindow(fg):
                    _, fg_pid = win32process.GetWindowThreadProcessId(fg)
                    _, main_pid = win32process.GetWindowThreadProcessId(hwnd)
                    if fg_pid == main_pid:
                        return True
            except Exception:
                pass
        return False

    # ── Guarded input ───────────────────────────────────────────────────

    def _key(self, hwnd: int, vk: int, exact: bool = False) -> None:
        self._foreground(hwnd, exact)
        press_key(vk)

    def _combo(self, hwnd: int, mod_vk: int, key_vk: int, exact: bool = False) -> None:
        self._foreground(hwnd, exact)
        send_combo(mod_vk, key_vk)

    def _paste(self, hwnd: int, text: str, exact: bool = False) -> None:
        self._foreground(hwnd, exact)                  # before the clipboard is touched
        self._window._clipboard_text(text, hwnd)
        self._sleep(0.15)
        self._combo(hwnd, VK_CONTROL, VK_V, exact)

    def _click_at(self, hwnd: int, x: int, y: int, required: bool = True) -> bool:
        """A click at a position of a dialog of its own, only while that very window is in front.
        ``required``: a click that is not delivered stops the action; otherwise it is reported."""
        self._foreground(hwnd, exact=True)
        delivered = bool(self._clicker(x, y))
        if not delivered and required:
            raise _Abort("the click was not delivered")
        return delivered

    def _click_row(self, hwnd: int, row: dict, exact: bool = False) -> None:
        self._foreground(hwnd, exact)
        rx, ry = _centre(row)
        try:
            left, top, right, bottom = self._rect_of(hwnd)
        except Exception as e:
            raise _Abort(f"the window position is unknown ({e})") from e
        x = left + int(rx * max(1, right - left))
        y = top + int(ry * max(1, bottom - top))
        if not self._clicker(x, y):
            raise _Abort("the click was not delivered")

    def _dismiss(self, hwnd: int) -> None:
        """Esc closes a leftover card / search; best effort, nothing to report."""
        try:
            self._key(hwnd, VK_ESCAPE)
        except _Abort:
            pass

    def _capture_ocr(self, hwnd: int | None = None) -> list[dict]:
        """The OCR rows of the WeChat window (empty if OCR fails)."""
        try:
            rows = self._ocr.capture(hwnd=hwnd) if hwnd is not None else self._ocr.capture()
            return rows or []
        except Exception as e:
            logger.warning("Friend management: OCR capture failed: %s", e)
            return []

    # ── Accept friend requests ──────────────────────────────────────────

    def accept_friend_requests(self) -> list[dict]:
        """Open 'New Friends' and accept the pending requests, one by one.

        The screen is read again after every click (the list moves), and a request
        counts as accepted only when its button is gone afterwards.

        Returns:
            One ``{"status": "accepted", "timestamp": ...}`` per accepted request.
        """
        if self._ocr is None:
            logger.warning("accept_friend_requests: no OCR engine, so nothing can be clicked safely")
            return AcceptResult()
        accepted = AcceptResult()
        try:
            self._accept_in_window(accepted)
        except Exception as e:
            # Not an _Abort: a pywin32 error, a window handle gone.  The session put the window back and
            # gave the focus back; a dialog it opened was closed (_confirm_dialog).  The caller hears why.
            logger.exception("accept_friend_requests failed")
            accepted.error = f"{type(e).__name__}: {e}"[:200]
        if not accepted:
            logger.info("accept_friend_requests: nothing accepted")
        return accepted

    def _accept_in_window(self, accepted: AcceptResult) -> None:
        """``accept_friend_requests`` in the window session; the accepted ones go into ``accepted``."""
        clean = False                 # the list was read to its end: no accept button was left
        with self._ui_session() as hwnd:
            if hwnd is None:
                return
            try:
                self._open_new_friends(hwnd)
                rows = self._capture_ocr()
                # In WeChat 4.0, if no accept button is visible, click pending item under "新的朋友"
                if _find_row(rows, *ACCEPT_LABELS, exact=True, min_x0=ACCEPT_MIN_X0) is None:
                    pending = _find_row(rows, *PENDING_REQUEST_LABELS)
                    if pending is not None and pending.get("x0", 0.0) < ACCEPT_MIN_X0:
                        self._click_row(hwnd, pending)
                        self._sleep(0.6)
                        rows = self._capture_ocr()

                for _ in range(MAX_ACCEPT_PER_RUN):
                    button = _find_row(rows, *ACCEPT_LABELS, exact=True, min_x0=ACCEPT_MIN_X0)
                    if button is None:
                        clean = True
                        break
                    label = _row_label(rows, button)          # read before the click: the list moves
                    before = self._window_snapshot(hwnd)      # a dialog is a window that appears after it
                    self._click_row(hwnd, button)
                    self._sleep(0.5)
                    self._confirm_dialog(hwnd, before)
                    rows = self._capture_ocr()
                    if _still_there(rows, button):
                        for _ in range(3):
                            self._sleep(0.4)
                            rows = self._capture_ocr()
                            if not _still_there(rows, button):
                                break
                    if _still_there(rows, button):
                        logger.warning("accept_friend_requests: a request is still pending after "
                                       "the click; stopping")
                        break
                    accepted_at = time.time()
                    accepted.append({"status": "accepted", "timestamp": accepted_at})
                    self._tell_accepted(accepted_at, label)
                    logger.info("Friend request accepted")
                if clean:
                    self._sleep(0.5)          # the badge goes when the list has been seen
                    self._rail.settle()       # the badges on the screen now are "nothing to do"
            except _Abort as e:
                logger.warning("accept_friend_requests stopped: %s", e)

    def _scroll_contacts_top(self, hwnd: int) -> None:
        """Scroll the contacts navigation list back to the top to reveal '新的朋友'."""
        if sys.platform != "win32":
            return
        try:
            left, top, right, bottom = self._rect_of(hwnd)
            target_x = left + int(0.13 * max(1, right - left))
            target_y = top + int(0.30 * max(1, bottom - top))
            import ctypes
            ctypes.windll.user32.SetCursorPos(target_x, target_y)
            self._sleep(0.05)
            # Send mouse wheel up events (0x0800 = MOUSEEVENTF_WHEEL, 120 = 1 notch up)
            for _ in range(12):
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, 120, 0)
                self._sleep(0.02)
        except Exception as e:
            logger.debug("Failed to scroll contacts list to top: %s", e)

    def _open_new_friends(self, hwnd: int) -> None:
        if not self._window._goto_contacts_tab(hwnd):
            raise _Abort("the Contacts tab did not open")
        self._sleep(0.4)
        row = _find_row(self._capture_ocr(), *NEW_FRIENDS_LABELS)
        if row is None:
            self._scroll_contacts_top(hwnd)
            self._sleep(0.4)
            row = _find_row(self._capture_ocr(), *NEW_FRIENDS_LABELS)
        if row is None:
            raise _Abort("'新的朋友' is not on the screen")
        self._click_row(hwnd, row)
        self._sleep(0.6)

    def _confirm_dialog(self, hwnd: int, before: set | None = None) -> None:
        """Click the confirmation of the accept dialog, if one opened: on the screen of the main window, or
        (WeChat 4.0) a dialog window that appeared after the click (``before``: the windows until then).  A
        dialog that cannot be confirmed safely is closed (cancelled) and the run stops."""
        row = _find_row(self._capture_ocr(), *CONFIRM_LABELS, exact=True)
        if row is not None:
            self._click_row(hwnd, row)
            self._sleep(0.5)
            return
        popup_hwnd = self._find_popup_dialog(hwnd, before)
        if popup_hwnd:
            try:
                self._confirm_popup_dialog(popup_hwnd)
            except Exception:
                self._close_window(popup_hwnd)        # cancelled: nothing typed anywhere else
                raise
            self._sleep(0.5)

    # ── WeChat 4.0's dialogs: windows of their own ─────────────────────

    def _process_windows(self, main_hwnd: int) -> list[int]:
        """The visible top-level windows of WeChat's process besides the main one (Windows only)."""
        import win32gui, win32process
        _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
        found: list[int] = []

        def _enum(h, _):
            if h != main_hwnd and win32gui.IsWindow(h) and win32gui.IsWindowVisible(h):
                _, pid = win32process.GetWindowThreadProcessId(h)
                if pid == main_pid:
                    found.append(h)
        win32gui.EnumWindows(_enum, None)
        return found

    def _window_snapshot(self, main_hwnd: int) -> set | None:
        """WeChat's windows before a click that may open a dialog: only a window that is not among them can be
        that dialog.  None when they cannot be listed (then no window is taken for a dialog)."""
        if sys.platform != "win32":
            return None
        try:
            return set(self._process_windows(main_hwnd))
        except Exception as e:
            logger.warning("Friend management: WeChat's windows could not be listed: %s", e)
            return None

    def _is_chat_title(self, title: str) -> bool:
        """Is this window title a chat's name (with or without a member count)?  Such a window is a chat
        open in a window of its own."""
        try:
            names = list(self._chat_names() or ()) if self._chat_names is not None else []
        except Exception:
            logger.debug("Friend management: the chats' names could not be read", exc_info=True)
            names = []
        return any(title_names_chat(title, str(name or "")) for name in names)

    def _find_new_dialog(self, main_hwnd: int, before: set | None, titles: tuple, *,
                         cls: str | None = None, exclude: tuple = ()) -> int | None:
        """A window of WeChat's process that appeared since ``before`` whose title is exactly one of
        ``titles`` (and whose class is ``cls``) and is not a chat's name; None if there is none (yet)."""
        if before is None or sys.platform != "win32":
            return None
        try:
            import win32gui
            for h in self._process_windows(main_hwnd):
                if h in before or h in exclude:
                    continue
                title = str(win32gui.GetWindowText(h) or "").strip()
                if title not in titles or (cls and win32gui.GetClassName(h) != cls):
                    continue
                if self._is_chat_title(title):
                    logger.warning("Friend management: a window titled like a chat (%r) is not taken for a dialog",
                                   title[:20])
                    continue
                return h
        except Exception as e:
            logger.warning("Friend management: looking for a dialog failed: %s", e)
        return None

    def _wait_for_dialog(self, main_hwnd: int, before: set | None, titles: tuple, what: str, *,
                         cls: str | None = None, exclude: tuple = ()) -> int | None:
        """``_find_new_dialog``, looked for DIALOG_LOOKS times; when it never shows, the titles of the windows
        that did appear are logged (a WeChat version that titles the dialog otherwise is seen in the log)."""
        if before is None or sys.platform != "win32":
            return None
        for look in range(DIALOG_LOOKS):
            found = self._find_new_dialog(main_hwnd, before, titles, cls=cls, exclude=exclude)
            if found:
                return found
            if look + 1 < DIALOG_LOOKS:
                self._sleep(0.1)
        try:
            import win32gui
            others = [str(win32gui.GetWindowText(h) or "")[:20] for h in self._process_windows(main_hwnd)
                      if h not in before and h not in exclude]
        except Exception:
            others = []
        logger.info("Friend management: no %s appeared%s", what,
                    f" (new WeChat windows: {others}; not used)" if others else "")
        return None

    def _find_popup_dialog(self, hwnd: int, before: set | None = None) -> int | None:
        """WeChat 4.0's confirmation of an accept (「通过朋友验证」): a window that appeared after the click on
        the accept button (``before``), titled exactly so, not a chat's name.  It used to be the first window
        of the process whose title contained 验证 / 确定 / 朋友: a chat open in a window of its own
        (「达人朋友群」, 「小朋友」) was clicked into and got an Enter."""
        return self._wait_for_dialog(hwnd, before, ACCEPT_DIALOG_TITLES, "confirmation dialog 「通过朋友验证」")

    def _confirm_popup_dialog(self, popup_hwnd: int) -> None:
        """Click its 确定 and press Enter, only while that very window is in front (SetForegroundWindow may
        fail: the click and the Enter would go to whatever window is in front).  ``_Abort`` otherwise."""
        if sys.platform != "win32":
            return
        self._focus_window(popup_hwnd)
        left, top, right, bottom = self._rect_of(popup_hwnd)
        cx = left + int(0.318 * (right - left))
        cy = top + int(0.930 * (bottom - top))
        if not self._click_at(popup_hwnd, cx, cy, required=False):
            logger.info("Friend management: the click on 确定 was not delivered; Enter confirms instead")
        self._sleep(0.2)
        self._key(popup_hwnd, 0x0D, exact=True)

    # ── Add friend ──────────────────────────────────────────────────────

    def add_friend(self, keyword: str, message: str = "") -> dict:
        """Search and add a new WeChat friend by phone number or WeChat ID.

        Args:
            keyword: Target phone number, wxid, or search keyword.
            message: Greeting / verification message to send.

        Returns:
            Result dict with keys: 'ok' (bool), 'target' (str), 'message' (str), 'reason' (str, on failure).
        """
        keyword = " ".join(str(keyword or "").split())
        message = " ".join(str(message or "").split())
        if not keyword:
            return _failure(keyword, "invalid", "请输入有效的微信号或手机号")
        if len(keyword) > MAX_KEYWORD_CHARS:
            return _failure(keyword[:MAX_KEYWORD_CHARS], "invalid", "微信号或手机号过长")
        if self._ocr is None:
            return _failure(keyword, "no_ocr", "好友管理需要屏幕识别（OCR），当前不可用")

        try:
            with self._ui_session() as hwnd:
                if hwnd is None:
                    return _failure(keyword, "no_window", "未找到微信主窗口或无法切到前台，请确保微信已启动")
                try:
                    return self._add_friend_in_window(hwnd, keyword, message)
                except _Abort as e:
                    logger.warning("add_friend stopped: %s", e)
                    return _failure(keyword, "aborted", f"微信窗口状态异常，已中止：{e}")
        except Exception:
            # Not an _Abort: a pywin32 error, a window handle gone.  The dialogs it opened were closed
            # (_add_friend_wechat4), the window put back and the focus returned (_ui_session).
            logger.exception("add_friend failed")
            return _failure(keyword, "error", "添加好友时出错，已中止（详见日志）：请在微信里看一下申请发出去没有")

    def _find_search_box(self) -> dict | None:
        """The search field's placeholder row in the top left of the window (topmost, then leftmost)."""
        boxes = [
            r for r in self._capture_ocr()
            if r.get("y0", 1.0) < SEARCH_BOX_MAX_Y0 and r.get("x0", 1.0) < SEARCH_BOX_MAX_X0
            and _is_search_placeholder(r.get("t", ""))
        ]
        return min(boxes, key=lambda r: (r["y0"], r["x0"])) if boxes else None

    def _is_wechat4(self, hwnd: int) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import win32gui
            if not win32gui.IsWindow(hwnd):
                return False
            cls = win32gui.GetClassName(hwnd)
            return "Qt" in cls
        except Exception:
            return False

    def _focus_window(self, hwnd: int) -> None:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            self._sleep(0.2)

    def _close_window(self, hwnd: int) -> None:
        """Close one of WeChat's dialog windows.

        Alt+F4 goes to whatever window has the keyboard, so it is only sent once this very
        window has it; otherwise the window is asked to close by message (it cannot hit
        another window).
        """
        if sys.platform != "win32":
            return
        try:
            import win32con
            import win32gui
            if not (win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)):
                return
            self._focus_window(hwnd)
            if win32gui.GetForegroundWindow() == hwnd:
                send_combo(0x12, 0x73)  # Alt+F4
            else:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            self._sleep(0.3)
        except Exception as e:
            logger.warning("Friend management: closing window %s failed: %s", hwnd, e)

    def _is_window_visible(self, hwnd: int) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import win32gui
            return bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd))
        except Exception:
            return False

    def _find_apply_dialog(self, main_hwnd: int, dialog_hwnd: int, before: set | None = None) -> int | None:
        """WeChat 4.0's application window (「申请添加朋友」): a window that appeared after the click on
        「添加到通讯录」 (``before``), titled exactly so, of the dialogs' class, not the add-friend window, not
        a chat's name.  It used to be the first window of that class whose title contained 申请 / 验证 / 朋友:
        a chat open in a window of its own got its input box cleared and the greeting sent into it."""
        return self._find_new_dialog(main_hwnd, before, APPLY_DIALOG_TITLES, cls=DIALOG_CLASS, exclude=(dialog_hwnd,))

    def _open_add_friend_dialog_wechat4(self, main_hwnd: int) -> int | None:
        if sys.platform != "win32":
            return None
        import win32gui, win32process
        _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)

        # Class and size also fit a chat opened in a window of its own, so a window that is
        # already open is used only if its title says it is the add-friend window (one left
        # over from an earlier run); otherwise only a window that appears now counts.
        before = self._add_dialog_candidates(main_hwnd)
        for h in before:
            title = win32gui.GetWindowText(h)
            if title in ADD_DIALOG_TITLES and not self._is_chat_title(title):
                return h

        self._combo(main_hwnd, VK_CONTROL, 0x31)    # Ctrl+1: the chat tab, where the (+) is
        self._sleep(0.4)

        self._foreground(main_hwnd)
        rect = self._rect_of(main_hwnd)
        plus_x = rect[0] + 285
        plus_y = rect[1] + 60
        if not self._clicker(plus_x, plus_y):
            return None
        self._sleep(0.4)

        menu_hwnd = None
        for _ in range(10):
            def _enum_menu(h, _):
                nonlocal menu_hwnd
                if h != main_hwnd and win32gui.IsWindow(h) and win32gui.IsWindowVisible(h):
                    _, pid = win32process.GetWindowThreadProcessId(h)
                    if pid == main_pid and "Qt51514QWindowToolSaveBits" in win32gui.GetClassName(h):
                        menu_hwnd = h
            win32gui.EnumWindows(_enum_menu, None)
            if menu_hwnd:
                break
            self._sleep(0.1)

        if not menu_hwnd:
            logger.warning("_open_add_friend_dialog_wechat4: popup menu not found after clicking (+)")
            return None

        self._foreground(main_hwnd)
        m_rect = win32gui.GetWindowRect(menu_hwnd)
        add_friend_x = m_rect[0] + int(0.5 * (m_rect[2] - m_rect[0]))
        add_friend_y = m_rect[1] + int(0.454 * (m_rect[3] - m_rect[1]))
        if not self._clicker(add_friend_x, add_friend_y):
            return None
        self._sleep(0.8)

        for _ in range(15):
            fresh = [h for h in self._add_dialog_candidates(main_hwnd) if h not in before]
            if fresh:
                return fresh[0]
            self._sleep(0.1)
        return None

    def _add_dialog_candidates(self, main_hwnd: int) -> list[int]:
        """Visible windows of WeChat's process, besides the main one, that may be the add-friend window.

        The title is the sure sign.  Without it the class and a size band decide, which only
        holds in the logical pixels of a process that is not DPI aware (a real machine showed the
        window at 746x1133 physical pixels, 373x566 logical) and also fits a chat of its own.
        """
        import win32gui, win32process
        _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
        found: list[int] = []

        def _enum(h, _):
            if h != main_hwnd and win32gui.IsWindow(h) and win32gui.IsWindowVisible(h):
                _, pid = win32process.GetWindowThreadProcessId(h)
                if pid == main_pid and win32gui.GetClassName(h) == DIALOG_CLASS:
                    title = win32gui.GetWindowText(h)
                    if self._is_chat_title(title):
                        return                               # a chat open in a window of its own
                    if title in ADD_DIALOG_TITLES:
                        found.append(h)
                        return
                    r = win32gui.GetWindowRect(h)
                    if 300 <= r[2] - r[0] <= 600 and 400 <= r[3] - r[1] <= 800:
                        found.append(h)
        win32gui.EnumWindows(_enum, None)
        return found

    def _add_friend_wechat4(self, hwnd: int, keyword: str, message: str) -> dict:
        """WeChat 4.0: the add-friend window and the application window are windows of their own.  Every key
        and click goes to one of them only while that very window is in front (a chat open in a window of its
        own belongs to the same process); the application window is one that appears after the click on
        「添加到通讯录」, titled exactly 「申请添加朋友」.  When the action stops, both are closed."""
        dialog_hwnd = self._open_add_friend_dialog_wechat4(hwnd)
        if not dialog_hwnd:
            return _failure(keyword, "no_window", "无法打开微信「添加朋友」窗口")

        apply_hwnd = None
        try:
            self._focus_window(dialog_hwnd)
            d_rect = self._rect_of(dialog_hwnd)
            w = d_rect[2] - d_rect[0]
            h = d_rect[3] - d_rect[1]

            input_x = d_rect[0] + int(0.35 * w)
            input_y = d_rect[1] + int(0.118 * h)
            self._click_at(dialog_hwnd, input_x, input_y)       # the search input box
            self._sleep(0.2)

            self._combo(dialog_hwnd, VK_CONTROL, VK_A, exact=True)
            self._sleep(0.05)
            self._key(dialog_hwnd, VK_BACK, exact=True)
            self._sleep(0.05)
            self._paste(dialog_hwnd, keyword, exact=True)
            self._sleep(0.2)
            self._key(dialog_hwnd, 0x0D, exact=True)
            self._sleep(1.5)

            rows = self._capture_ocr(dialog_hwnd)
            card_text = "".join(_clean_text(r.get("t", "")) for r in rows)

            if any(marker in card_text for marker in NOT_FOUND_MARKERS):
                self._close_window(dialog_hwnd)
                return _failure(keyword, "not_found", f"未找到用户「{keyword}」，请核实微信号或手机号")

            if any(marker in card_text for marker in ALREADY_FRIEND_MARKERS):
                self._close_window(dialog_hwnd)
                return _failure(keyword, "already_friend", f"用户「{keyword}」已是您的微信好友，无需重复添加")

            add_button = _find_row(rows, *ADD_BUTTON_LABELS)
            if add_button is None:
                self._close_window(dialog_hwnd)
                return _failure(keyword, "no_add_button", f"没有找到「{keyword}」的「添加到通讯录」按钮，已中止")

            before = self._window_snapshot(hwnd)                 # the application window appears after the click
            self._click_row(dialog_hwnd, add_button, exact=True)
            self._sleep(0.8)

            # 3. Application modal dialog
            for _ in range(DIALOG_LOOKS):
                apply_hwnd = self._find_apply_dialog(hwnd, dialog_hwnd, before)
                if apply_hwnd:
                    break
                self._sleep(0.1)

            target_dialog = apply_hwnd if apply_hwnd else dialog_hwnd
            self._focus_window(target_dialog)
            app_rows = self._capture_ocr(target_dialog)

            confirm_labels = ("确定", "发送", "完成")
            send_button = _find_row(app_rows, *confirm_labels, exact=True)
            if send_button is None and not apply_hwnd:
                self._close_window(dialog_hwnd)
                return _failure(keyword, "no_dialog", "没有出现填写附言的申请窗口（对方可能无需验证，或界面不同），请在微信里确认是否已发出")

            if apply_hwnd:
                a_rect = self._rect_of(apply_hwnd)
                a_w = a_rect[2] - a_rect[0]
                a_h = a_rect[3] - a_rect[1]
                if message:
                    # Focus the greeting input box near top (cx=0.30, cy=0.18)
                    input_x = a_rect[0] + int(0.30 * a_w)
                    input_y = a_rect[1] + int(0.18 * a_h)
                    if self._click_at(apply_hwnd, input_x, input_y, required=False):
                        self._sleep(0.1)
                        self._combo(apply_hwnd, VK_CONTROL, VK_A, exact=True)
                        self._sleep(0.05)
                        self._key(apply_hwnd, VK_BACK, exact=True)
                        self._sleep(0.05)
                        self._paste(apply_hwnd, message, exact=True)
                        self._sleep(0.2)

                if send_button:
                    self._click_row(apply_hwnd, send_button, exact=True)
                else:
                    # Default position for WeChat 4.0 green "确定" button (cx=0.337, cy=0.933)
                    btn_x = a_rect[0] + int(0.337 * a_w)
                    btn_y = a_rect[1] + int(0.933 * a_h)
                    self._click_at(apply_hwnd, btn_x, btn_y, required=False)
                self._sleep(0.2)
                self._key(apply_hwnd, 0x0D, exact=True)
                self._sleep(1.0)

                for _ in range(15):
                    if not self._is_window_visible(apply_hwnd):
                        break
                    self._sleep(0.1)

                if self._is_window_visible(apply_hwnd):
                    self._close_window(apply_hwnd)
                    self._close_window(dialog_hwnd)
                    return _failure(keyword, "not_sent", "申请窗口还在，申请可能没发出去，请在微信里确认")
            else:
                if message:
                    self._combo(target_dialog, VK_CONTROL, VK_A, exact=True)
                    self._sleep(0.05)
                    self._paste(target_dialog, message, exact=True)
                    self._sleep(0.3)
                self._click_row(target_dialog, send_button, exact=True)
                self._sleep(0.8)
                if _still_there(self._capture_ocr(target_dialog), send_button):
                    self._close_window(dialog_hwnd)
                    return _failure(keyword, "not_sent", "申请窗口还在，申请可能没发出去，请在微信里确认")

            self._close_window(dialog_hwnd)
            return {
                "ok": True,
                "target": keyword,
                "message": f"已成功向「{keyword}」发送好友申请" + (f"（附言：{message}）" if message else ""),
            }
        except Exception:                             # _Abort, or a pywin32 error: no dialog is left open
            for opened in (apply_hwnd, dialog_hwnd):
                if opened:
                    self._close_window(opened)
            raise

    def _add_friend_in_window(self, hwnd: int, keyword: str, message: str) -> dict:
        if self._is_wechat4(hwnd):
            return self._add_friend_wechat4(hwnd, keyword, message)
        # 1. The search box is found on the screen and clicked; nothing is typed before it is seen
        box = self._find_search_box()
        if box is None:
            return _failure(keyword, "no_search_box", "屏幕上没有认出搜索框（写着「搜索」的那一行），已中止，什么也没输入")
        self._click_row(hwnd, box)
        self._sleep(0.3)
        self._combo(hwnd, VK_CONTROL, VK_A)
        self._sleep(0.05)
        self._key(hwnd, VK_BACK)
        self._sleep(0.05)
        self._paste(hwnd, keyword)
        self._sleep(0.8)

        # 2. The network search entry of the dropdown
        entry = _find_row(self._capture_ocr(), *SEARCH_ENTRY_LABELS)
        if entry is None:
            self._dismiss(hwnd)
            return _failure(keyword, "no_search_entry", "搜索结果里没有「网络查找」入口，已中止（没有发出任何申请）")
        self._click_row(hwnd, entry)
        self._sleep(1.2)

        # 3. The profile card
        card = self._capture_ocr()
        card_text = "".join(_clean_text(r.get("t", "")) for r in card)
        if any(marker in card_text for marker in NOT_FOUND_MARKERS):
            self._dismiss(hwnd)
            return _failure(keyword, "not_found", f"未找到用户「{keyword}」，请核实微信号或手机号")
        if "发消息" in card_text and any(marker in card_text for marker in ALREADY_FRIEND_MARKERS):
            self._dismiss(hwnd)
            return _failure(keyword, "already_friend", f"用户「{keyword}」已是您的微信好友，无需重复添加")
        add_button = _find_row(card, *ADD_BUTTON_LABELS)
        if add_button is None:
            self._dismiss(hwnd)
            return _failure(keyword, "no_add_button", f"没有找到「{keyword}」的「添加到通讯录」按钮，已中止")
        self._click_row(hwnd, add_button)
        self._sleep(0.8)

        # 4. The application dialog: the greeting is pasted only once it is on the screen
        send_button = _find_row(self._capture_ocr(), *SEND_LABELS, exact=True)
        if send_button is None:
            self._dismiss(hwnd)
            return _failure(keyword, "no_dialog", "没有出现填写附言的申请窗口（对方可能无需验证，或界面不同），请在微信里确认是否已发出")
        if message:
            self._combo(hwnd, VK_CONTROL, VK_A)
            self._sleep(0.05)
            self._paste(hwnd, message)
            self._sleep(0.3)
        self._click_row(hwnd, send_button)
        self._sleep(0.8)

        # 5. The dialog must be gone, or the application did not go out
        if _still_there(self._capture_ocr(), send_button):
            self._dismiss(hwnd)
            return _failure(keyword, "not_sent", "申请窗口还在，申请可能没发出去，请在微信里确认")
        self._dismiss(hwnd)
        return {
            "ok": True,
            "target": keyword,
            "message": f"已成功向「{keyword}」发送好友申请" + (f"（附言：{message}）" if message else ""),
        }


class AutoFriendService:
    """Background worker that accepts friend requests without getting in the user's way.

    Every ``interval_sec`` it first looks, silently, whether a red badge has appeared in
    WeChat's rail (``watch``); if nothing changed it does nothing at all.  Only when something
    may be waiting (or the window cannot be pictured) does it need the screen, and then only
    once the user has not touched keyboard or mouse for ``idle_sec`` seconds (``idle_gate``);
    until then it asks again every POSTPONE_RETRY_SEC.  ``full_check_sec`` forces a full check
    even when nothing changed, so a badge that is not recognised delays a request that long at most.
    """

    def __init__(
        self,
        friend_manager: FriendManager,
        interval_sec: int = 300,
        enabled: bool = True,
        idle_gate=None,
        idle_sec: float = 0,
        watch: bool = True,
        full_check_sec: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._manager = friend_manager
        self._interval = max(MIN_INTERVAL_SEC, int(interval_sec))
        self._enabled = enabled
        self._idle_gate = idle_gate
        self._idle_sec = max(0.0, float(idle_sec))
        self._watch = watch
        self._full_check_sec = max(0.0, float(full_check_sec))
        self._clock = clock
        self._last_full_check = clock()
        self.postponed = False            # the last tick found the user busy and left it for later
        self._postponed_since: float | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        """Switch on; also starts the worker if it was never started (the config had it off)."""
        self._enabled = True
        self.start()
        logger.info("AutoFriendService enabled")

    def disable(self) -> None:
        self._enabled = False
        logger.info("AutoFriendService disabled")

    def start(self) -> None:
        """Start the background worker (no-op if it already runs)."""
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="auto-friend-service", daemon=True)
            self._thread.start()
        logger.info("AutoFriendService worker started (interval=%ds, enabled=%s)", self._interval, self._enabled)

    def stop(self) -> None:
        """Stop the background worker."""
        self._stop_event.set()
        with self._state_lock:
            thread, self._thread = self._thread, None
        if thread:
            thread.join(timeout=2.0)

    def run_once(self) -> list[dict]:
        """One tick now (does nothing while disabled); never raises.

        Looks silently first; only a screen-taking check waits for the user to be idle.
        ``postponed`` says whether the tick left a check for later.
        """
        if not self._enabled:
            return []
        try:
            if self._watch and not self._full_check_due() and self._manager.look_for_news() == RailWatcher.UNCHANGED:
                self._postponed_since = None
                self.postponed = False
                return []
            if not self._user_is_idle():
                if self._postponed_since is None:
                    self._postponed_since = self._clock()
                    logger.info("AutoFriendService: something may be waiting, but you are at the "
                                "computer; the check waits until you pause for %.0fs", self._idle_sec)
                self.postponed = True
                return []
            if self._postponed_since is not None:
                logger.info("AutoFriendService: you paused after %.0fs; checking now",
                            self._clock() - self._postponed_since)
            self._postponed_since = None
            self.postponed = False
            accepted = self._manager.accept_friend_requests()
            self._last_full_check = self._clock()
            self._mark_own_input()
        except Exception:
            logger.warning("AutoFriendService: the check failed", exc_info=True)
            return []
        if accepted:
            logger.info("AutoFriendService: accepted %d friend request(s)", len(accepted))
        return accepted

    def _full_check_due(self) -> bool:
        return self._full_check_sec > 0 and self._clock() - self._last_full_check >= self._full_check_sec

    def _user_is_idle(self) -> bool:
        if self._idle_gate is None or self._idle_sec <= 0:
            return True
        try:
            return self._idle_gate.idle_seconds() >= self._idle_sec
        except Exception as e:
            logger.warning("AutoFriendService: idle time unavailable (%s); not waiting for the user", e)
            return True

    def _mark_own_input(self) -> None:
        """The check's own clicks and keys are not the user: they must not make the user look busy."""
        try:
            if self._idle_gate is not None:
                self._idle_gate.mark_own_input()
        except Exception:
            pass

    def _run(self) -> None:
        # The first tick comes one interval after the start, not while the bot is still starting up.
        wait = self._interval
        while not self._stop_event.wait(wait):
            self.run_once()
            wait = POSTPONE_RETRY_SEC if self.postponed else self._interval
