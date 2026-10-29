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

import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
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
# AUTO_ACCEPT_DAILY_MAX's default.  WeChat holds back accounts that take on friends in bulk (「操作过于频繁」,
# then days without adding friends); people who run business accounts keep automated accepting to a few
# dozen a day.  50 covers a normal day of creators asking; past it the admin accepts by hand (同意好友).
DAILY_MAX_DEFAULT = 50
STUCK_RUNS = 3               # auto-accept checks in a row whose request stayed pending: it pauses
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
# What WeChat shows when it holds an account back (frequency / risk control), matched in text that was not on
# the screen before the click (a greeting in 「新的朋友」 may say anything).  「操作过于频繁，请稍后再试」 is the
# one widely reported; the others are WeChat's words for a restricted account elsewhere, kept as a guess
# (none of them read on this account yet).  「稍后再试」 also ends a network error: stopping then is the safe side.
RISK_MARKERS = (
    "过于频繁", "太频繁", "操作频繁", "频繁操作", "频率过快", "稍后再试",
    "帐号存在异常", "账号存在异常", "帐号异常", "账号异常", "被限制", "限制使用",
    "暂时无法添加", "无法添加朋友", "安全风险", "被盗风险", "已达上限", "达到上限",
)
ADD_DIALOG_TITLES = ("添加朋友", "Add Friend")   # title of WeChat 4.0's add-friend window (read on a real machine);
                                                # exact, because the application window is "申请添加朋友"
# Some WeChat 4.x builds title the add-friend window plainly 「微信」 / 「Weixin」 (geigp, 2026-10-29). So are other
# windows of the process, so these titles count ONLY for a window that appears right after our click on 「添加朋友」,
# never for one that was already open (taking such a window for the dialog made the keys go elsewhere: 「the dialog
# … is not the window in front」).
GENERIC_DIALOG_TITLES = ("微信", "Weixin")
# WeChat 4.0's other dialogs, matched exactly (a chat opened in a window of its own has the same process and
# class, and its title is the chat's name: 「达人朋友群」, 「小朋友」 must never be taken for one).
APPLY_DIALOG_TITLES = ("申请添加朋友",)         # the application window (probe-apply.txt, real machine 2026-10-23)
ACCEPT_DIALOG_TITLES = ("通过朋友验证",)         # what 「前往验证」 opens (its title not read on a real machine yet:
                                                # a window that appears with another title is logged, never used)
DIALOG_CLASS = "Qt51514QWindowIcon"
DIALOG_LOOKS = 10                                # a dialog is looked for this many times, 0.1 s apart
# add_friend's ``remark`` (备注; 加百应达人微信 asks for 「抖音-<百应昵称>」): only in WeChat 4.0's application window, whose
# controls UIA cannot see (probe-apply.txt), and nobody has looked yet where its remark field is.  So it is found by OCR or
# not at all: a row reading exactly one of REMARK_LABELS (a trailing colon aside), nothing beside it on its line, and the
# first row right below it (within REMARK_REACH label heights, overlapping it horizontally: normally her pre-filled
# nickname) is the input.  A row below that reads like another part of the window (APPLY_WORDS) is no input.
REMARK_LABELS = ("备注", "备注名", "设置备注")
REMARK_REACH = 3.0
APPLY_WORDS = ("标签", "权限", "朋友圈", "仅聊天", "不让", "不看", "确定", "取消", "发送", "完成")
# Words of the application window worth naming in the log when no remark field was seen (never anything she wrote)
APPLY_LANDMARKS = ("发送添加朋友申请", *REMARK_LABELS, "标签", "朋友权限", "设置朋友权限", "确定", "取消")
REMARK_MAX_CHARS = 32


class _Abort(Exception):
    """The window action cannot go on safely (WeChat lost the foreground, a click failed)."""


class AcceptResult(list):
    """What ``accept_friend_requests`` returns: one ``{"status": "accepted", "timestamp"}`` per request
    accepted (a list, as before), and why the run ended early: ``error`` (an unexpected error: a pywin32
    error, a window handle gone; logged with its traceback), ``risk`` (what WeChat said when it held the
    account back, RISK_MARKERS), ``stuck`` (a request stayed pending after its click)."""

    error: str = ""
    risk: str = ""
    stuck: bool = False


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


def _label_text(row: dict) -> str:
    """A row's cleaned text without a trailing colon (a label may be read 「备注：」)."""
    return _clean_text(row.get("t", "")).rstrip(":：")


def _remark_field(rows: list[dict]) -> tuple[dict | None, str]:
    """The remark's input among the OCR rows of WeChat 4.0's application window, and why there is none ("" when found):
    the first row right below a label reading exactly 备注 / 备注名 / 设置备注 (the topmost such label), its top at most
    REMARK_REACH label heights under the label's bottom, overlapping the label horizontally.  Nothing is guessed: no
    such label, a row beside the label on its line (another layout: the row below would be the next field), nothing
    right below it, or a row below that reads like another part of the window (标签, 朋友权限, a button, a second
    label) give None."""
    labels = [r for r in rows if _label_text(r) in REMARK_LABELS]
    if not labels:
        return None, "no 备注 label"
    label = min(labels, key=lambda r: (float(r.get("y0", 0.0)), float(r.get("x0", 0.0))))
    x0, x1 = float(label.get("x0", 0.0)), float(label.get("x1", 0.0))
    y0, y1 = float(label.get("y0", 0.0)), float(label.get("y1", 0.0))
    height = max(y1 - y0, 1e-6)
    middle = (y0 + y1) / 2
    others = [r for r in rows if r is not label]
    if any(float(r.get("x0", 0.0)) >= x1 and float(r.get("y0", 1.0)) <= middle <= float(r.get("y1", 0.0))
           for r in others):
        return None, "something beside the label on its line"
    below = [r for r in others
             if float(r.get("y0", 0.0)) >= y1 - 0.25 * height
             and float(r.get("y0", 0.0)) - y1 <= REMARK_REACH * height
             and float(r.get("x0", 1.0)) <= x1 and float(r.get("x1", 0.0)) >= x0]
    if not below:
        return None, "nothing right below the label"
    field = min(below, key=lambda r: (float(r.get("y0", 0.0)), float(r.get("x0", 0.0))))
    text = _label_text(field)
    if not text or text in REMARK_LABELS or any(word in text for word in APPLY_WORDS):
        return None, "the row below the label is another part of the window"
    return field, ""


def _landmarks(rows: list[dict]) -> str:
    """The application window's own words OCR read and where (``APPLY_LANDMARKS`` only: never her nickname or our
    greeting), for the log line of a remark field that was not seen."""
    seen = [f"{_label_text(r)}@{float(r.get('y0', 0.0)):.2f}" for r in rows if _label_text(r) in APPLY_LANDMARKS]
    return ", ".join(seen[:12]) or "none of its labels"


def _failure(target: str, reason: str, message: str) -> dict:
    return {"ok": False, "target": target, "reason": reason, "message": message}


def _risk_line(rows: list[dict], seen=frozenset()) -> str:
    """The first OCR row that reads like WeChat holding the account back (RISK_MARKERS) and is not in
    ``seen`` (the cleaned texts that were on the screen before the click); "" when there is none."""
    for r in rows or []:
        text = _clean_text(r.get("t", ""))
        if text and text not in seen and any(marker in text for marker in RISK_MARKERS):
            return " ".join(str(r.get("t", "")).split())[:100]
    return ""


def _risk_failure(keyword: str, risk: str) -> dict:
    return _failure(keyword, "risk", f"微信提示「{risk}」，可能是操作太频繁被限制了，已停止：先别再加好友、同意好友，"
                                     "过几个小时再试")


def print_main_window(hwnd: int):
    """A picture of the window taken without bringing it forward (Windows only)."""
    from .ocr.win_ocr import print_window
    return print_window(hwnd)


def _safe_ensure_default_desktop():
    try:
        from .keyboard import _ensure_default_desktop
        _ensure_default_desktop()
    except Exception:
        pass


def post_click_window(hwnd: int, screen_x: int, screen_y: int) -> bool:
    """Send WM_LBUTTONDOWN and WM_LBUTTONUP directly to hwnd's message queue without moving cursor."""
    if sys.platform != "win32":
        return False
    try:
        import win32con, win32gui
        _safe_ensure_default_desktop()
        if not (win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)):
            return False
        cx, cy = win32gui.ScreenToClient(hwnd, (int(screen_x), int(screen_y)))
        lParam = (cy << 16) | (cx & 0xFFFF)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
        time.sleep(0.04)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)
        return True
    except Exception as e:
        logger.debug("post_click_window failed for HWND=%s (%s, %s): %s", hwnd, screen_x, screen_y, e)
        return False


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
        idle_gate=None,
    ):
        self._window = window_controller
        self._ocr = ocr_engine
        self._ui_lock = ui_lock if ui_lock is not None else threading.Lock()
        self._idle_gate = idle_gate
        self._clicker = clicker or click_screen
        self._rect_of = rect_of
        self._sleep = sleeper
        self._grab_window = grab_window
        self._rail = RailWatcher(self._grab_main_window)
        self._on_accepted = on_accepted
        self._chat_names = chat_names
        self._remark_told = False                    # the single-window UI's "no remark here" is logged once

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
        _safe_ensure_default_desktop()
        with self._ui_lock:
            if getattr(self, "_idle_gate", None) is not None:
                try:
                    self._idle_gate.wait()
                except Exception:
                    pass
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
            if self._window._foreground_matches(hwnd):
                return
            # Another window of WeChat's own process is in front (the main window, the closing (+) menu, a focus
            # change still under way): bring the dialog forward once and look again.  Any other program in front
            # stops it as before: keys never go to the user's own window.
            if self._is_wechat_foreground(hwnd):
                try:
                    self._window.activate(hwnd)
                except Exception as e:
                    logger.debug("Friend management: the dialog could not be brought forward: %s", e)
                self._sleep(0.2)
                if self._window._foreground_matches(hwnd):
                    logger.info("Friend management: the dialog (HWND=%s) was brought in front again", hwnd)
                    return
            raise _Abort(f"the dialog (HWND={hwnd}) is not the window in front")
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

    def _click_at(self, hwnd: int, x: int, y: int, required: bool = True, exact: bool = True) -> bool:
        """A click at a position of a dialog of its own, only while that very window is in front (``exact``;
        otherwise a window of WeChat's process will do: a click only moves the focus, the keys after it are
        checked exactly).  ``required``: a click that is not delivered stops the action; otherwise it is reported."""
        self._foreground(hwnd, exact)
        delivered = bool(self._clicker(x, y))          # ONE click: a posted click before it made every button get two
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
        if not self._clicker(x, y):                    # ONE click (see _click_at)
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

    def accept_friend_requests(self, limit: int | None = None) -> AcceptResult:
        """Open 'New Friends' and accept the pending requests, one by one (at most ``limit``, and at most
        MAX_ACCEPT_PER_RUN; a limit of 0 does not touch the window).

        The screen is read again after every click (the list moves), and a request
        counts as accepted only when its button is gone afterwards.  What WeChat says when it holds the
        account back (RISK_MARKERS, text new after the click) ends the run (``risk``).

        Returns:
            One ``{"status": "accepted", "timestamp": ...}`` per accepted request (``AcceptResult``).
        """
        if self._ocr is None:
            logger.warning("accept_friend_requests: no OCR engine, so nothing can be clicked safely")
            return AcceptResult()
        accepted = AcceptResult()
        runs = MAX_ACCEPT_PER_RUN if limit is None else max(0, min(MAX_ACCEPT_PER_RUN, int(limit)))
        if runs == 0:
            return accepted
        try:
            self._accept_in_window(accepted, runs)
        except Exception as e:
            # Not an _Abort: a pywin32 error, a window handle gone.  The session put the window back and
            # gave the focus back; a dialog it opened was closed (_confirm_dialog).  The caller hears why.
            logger.exception("accept_friend_requests failed")
            accepted.error = f"{type(e).__name__}: {e}"[:200]
        if not accepted:
            logger.info("accept_friend_requests: nothing accepted")
        return accepted

    def _accept_in_window(self, accepted: AcceptResult, runs: int = MAX_ACCEPT_PER_RUN) -> None:
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

                for _ in range(runs):
                    button = _find_row(rows, *ACCEPT_LABELS, exact=True, min_x0=ACCEPT_MIN_X0)
                    if button is None:
                        clean = True
                        break
                    label = _row_label(rows, button)          # read before the click: the list moves
                    seen = {_clean_text(r.get("t", "")) for r in rows}     # the screen's text before the click
                    before = self._window_snapshot(hwnd)      # a dialog is a window that appears after it
                    self._click_row(hwnd, button)
                    self._sleep(0.5)
                    risk = self._confirm_dialog(hwnd, before)
                    rows = self._capture_ocr()
                    risk = risk or _risk_line(rows, seen)
                    if not risk and _still_there(rows, button):
                        for _ in range(3):
                            self._sleep(0.4)
                            rows = self._capture_ocr()
                            risk = _risk_line(rows, seen)
                            if risk or not _still_there(rows, button):
                                break
                    risk = risk or self._risk_in_new_windows(hwnd, before)
                    if risk:
                        logger.warning("accept_friend_requests: WeChat says %r (it holds the account back?); "
                                       "stopping", risk)
                        accepted.risk = risk
                        break
                    if _still_there(rows, button):
                        logger.warning("accept_friend_requests: a request is still pending after "
                                       "the click; stopping")
                        accepted.stuck = True
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

    def _confirm_dialog(self, hwnd: int, before: set | None = None) -> str:
        """Click the confirmation of the accept dialog, if one opened: on the screen of the main window, or
        (WeChat 4.0) a dialog window that appeared after the click (``before``: the windows until then).  A
        dialog that cannot be confirmed safely is closed (cancelled) and the run stops.  Returns what WeChat
        said in a dialog still open after its 确定 when that reads like a risk prompt (it is closed), else ""."""
        row = _find_row(self._capture_ocr(), *CONFIRM_LABELS, exact=True)
        if row is not None:
            self._click_row(hwnd, row)
            self._sleep(0.5)
            return ""
        popup_hwnd = self._find_popup_dialog(hwnd, before)
        if not popup_hwnd:
            return ""
        try:
            self._confirm_popup_dialog(popup_hwnd)
        except Exception:
            self._close_window(popup_hwnd)            # cancelled: nothing typed anywhere else
            raise
        self._sleep(0.5)
        if self._is_window_visible(popup_hwnd):       # still there after 确定: what does it say?
            risk = _risk_line(self._capture_ocr(popup_hwnd))
            if risk:
                self._close_window(popup_hwnd)
                return risk
        return ""

    def _risk_in_new_windows(self, main_hwnd: int, before: set | None) -> str:
        """A window of WeChat's that appeared since ``before`` and reads like a risk prompt (a message box of
        its own): its text (it is closed), else ""."""
        if before is None or sys.platform != "win32":
            return ""
        try:
            fresh = [h for h in self._process_windows(main_hwnd) if h not in before]
        except Exception:
            return ""
        for h in fresh:
            risk = _risk_line(self._capture_ocr(h))
            if risk:
                self._close_window(h)
                return risk
        return ""

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

    def add_friend(self, keyword: str, message: str = "", remark: str = "") -> dict:
        """Search and add a new WeChat friend by phone number or WeChat ID.

        Args:
            keyword: Target phone number, wxid, or search keyword.
            message: Greeting / verification message to send.
            remark: The remark (备注) she gets on our side (加百应达人微信: 「抖音-<百应昵称>」).  Only WeChat 4.0's
                application window gets it, typed into its 「备注」 field when OCR shows that field (``_fill_remark``);
                otherwise nothing is clicked for it and the request goes without it.  "" (the admin's 添加好友):
                the window is driven exactly as it always was.

        Returns:
            Result dict with keys: 'ok' (bool), 'target' (str), 'message' (str), 'reason' (str, on failure);
            with a ``remark`` also 'remark_set' (bool: it was typed into the 「备注」 field).
        """
        remark = " ".join(str(remark or "").split())[:REMARK_MAX_CHARS]
        result = self._add_friend(keyword, message, remark)
        if remark:
            result = {**result, "remark_set": bool(result.get("remark_set"))}
        return result

    def _add_friend(self, keyword: str, message: str, remark: str) -> dict:
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
                    return self._add_friend_in_window(hwnd, keyword, message, remark)
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
        _safe_ensure_default_desktop()
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
        self._sleep(0.3)

        self._foreground(main_hwnd)
        rect = self._rect_of(main_hwnd)
        plus_x = rect[0] + 280
        plus_y = rect[1] + 56
        self._sleep(0.2)

        def _find_menu() -> int | None:
            found = None
            def _enum_menu(h, _):
                nonlocal found
                if h != main_hwnd and win32gui.IsWindow(h) and win32gui.IsWindowVisible(h):
                    _, pid = win32process.GetWindowThreadProcessId(h)
                    if pid == main_pid and "Qt51514QWindowToolSaveBits" in win32gui.GetClassName(h):
                        found = h
            win32gui.EnumWindows(_enum_menu, None)
            return found

        menu_hwnd = _find_menu()
        if not menu_hwnd:
            for attempt in range(2):
                if not post_click_window(main_hwnd, plus_x, plus_y):
                    if not self._clicker(plus_x, plus_y):
                        return None
                for _ in range(15):
                    self._sleep(0.08)
                    menu_hwnd = _find_menu()
                    if menu_hwnd:
                        break
                if menu_hwnd:
                    break
                self._sleep(0.2)

        if not menu_hwnd:
            logger.warning("_open_add_friend_dialog_wechat4: popup menu not found after clicking (+)")
            return None

        self._foreground(main_hwnd)
        m_rect = win32gui.GetWindowRect(menu_hwnd)
        add_friend_x = m_rect[0] + int(0.5 * (m_rect[2] - m_rect[0]))
        add_friend_y = m_rect[1] + int(0.51 * (m_rect[3] - m_rect[1]))
        if not post_click_window(menu_hwnd, add_friend_x, add_friend_y):
            if not self._clicker(add_friend_x, add_friend_y):
                return None
        self._sleep(0.5)

        for _ in range(25):
            fresh = [h for h in self._add_dialog_candidates(main_hwnd) if h not in before]
            if fresh:
                return fresh[0]
            self._sleep(0.08)
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
                    if title in ADD_DIALOG_TITLES or title in GENERIC_DIALOG_TITLES:
                        found.append(h)                      # (a generic title is used only if the window is new)
                        return
                    r = win32gui.GetWindowRect(h)
                    if 300 <= r[2] - r[0] <= 600 and 400 <= r[3] - r[1] <= 800:
                        found.append(h)
        win32gui.EnumWindows(_enum, None)
        return found

    def _add_friend_wechat4(self, hwnd: int, keyword: str, message: str, remark: str = "") -> dict:
        """WeChat 4.0: the add-friend window and the application window are windows of their own.  Every key
        and paste goes to one of them only while that very window is in front (a chat open in a window of its
        own belongs to the same process; a click only moves the focus, so WeChat in front is enough for it, as
        before); the application window is one that appears after the click on 「添加到通讯录」, titled exactly
        「申请添加朋友」.  When the action stops, both are closed.  A ``remark`` goes into that window's 「备注」 field
        before the greeting, only when OCR shows the field (``_fill_remark``; the answer then has ``remark_set``)."""
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
            self._click_at(dialog_hwnd, input_x, input_y, exact=False)   # the search input box
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

            risk = _risk_line(rows)
            if risk:
                self._close_window(dialog_hwnd)
                return _risk_failure(keyword, risk)

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
            self._click_row(dialog_hwnd, add_button)
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

            extra = {"remark_set": False} if remark else {}      # (the answer has it only when a remark was asked)
            if apply_hwnd:
                a_rect = self._rect_of(apply_hwnd)
                a_w = a_rect[2] - a_rect[0]
                a_h = a_rect[3] - a_rect[1]
                if remark:                                       # before the greeting: OCR read the window as it is
                    extra = {"remark_set": self._fill_remark(apply_hwnd, app_rows, a_rect, remark)}
                if message:
                    # Focus the greeting input box near top (cx=0.30, cy=0.18)
                    input_x = a_rect[0] + int(0.30 * a_w)
                    input_y = a_rect[1] + int(0.18 * a_h)
                    if self._click_at(apply_hwnd, input_x, input_y, required=False, exact=False):
                        self._sleep(0.1)
                        self._combo(apply_hwnd, VK_CONTROL, VK_A, exact=True)
                        self._sleep(0.05)
                        self._key(apply_hwnd, VK_BACK, exact=True)
                        self._sleep(0.05)
                        self._paste(apply_hwnd, message, exact=True)
                        self._sleep(0.2)

                if send_button:
                    self._click_row(apply_hwnd, send_button)
                else:
                    # Default position for WeChat 4.0 green "确定" button (cx=0.337, cy=0.933)
                    btn_x = a_rect[0] + int(0.337 * a_w)
                    btn_y = a_rect[1] + int(0.933 * a_h)
                    self._click_at(apply_hwnd, btn_x, btn_y, required=False, exact=False)
                self._sleep(0.2)
                self._key(apply_hwnd, 0x0D, exact=True)
                self._sleep(1.0)

                for _ in range(15):
                    if not self._is_window_visible(apply_hwnd):
                        break
                    self._sleep(0.1)

                if self._is_window_visible(apply_hwnd):
                    risk = _risk_line(self._capture_ocr(apply_hwnd))
                    self._close_window(apply_hwnd)
                    self._close_window(dialog_hwnd)
                    if risk:
                        return {**_risk_failure(keyword, risk), **extra}
                    return {**_failure(keyword, "not_sent", "申请窗口还在，申请可能没发出去，请在微信里确认"), **extra}
            else:
                if remark:
                    logger.info("Friend management: no application window of its own appeared, so no remark field "
                                "(备注) was seen; the request goes without it")
                if message:
                    self._combo(target_dialog, VK_CONTROL, VK_A, exact=True)
                    self._sleep(0.05)
                    self._paste(target_dialog, message, exact=True)
                    self._sleep(0.3)
                self._click_row(target_dialog, send_button)
                self._sleep(0.8)
                if _still_there(self._capture_ocr(target_dialog), send_button):
                    self._close_window(dialog_hwnd)
                    return _failure(keyword, "not_sent", "申请窗口还在，申请可能没发出去，请在微信里确认")

            self._close_window(dialog_hwnd)
            return {
                "ok": True,
                "target": keyword,
                "message": f"已成功向「{keyword}」发送好友申请" + (f"（附言：{message}）" if message else ""),
                **extra,
            }
        except Exception:                             # _Abort, or a pywin32 error: no dialog is left open
            for opened in (apply_hwnd, dialog_hwnd):
                if opened:
                    self._close_window(opened)
            raise

    def _fill_remark(self, apply_hwnd: int, rows: list[dict], rect, remark: str) -> bool:
        """The remark into the 「备注」 field of the application window: its input row (``_remark_field`` over the OCR
        rows read when the window opened) is clicked like the greeting box, then Ctrl+A, Backspace and the remark
        pasted, each into that very window (exact foreground checks, as for the greeting).  True when it was typed.
        No field seen: nothing is clicked for it (an INFO line says so, with the window's own labels OCR read).  The
        remark never stops the request: a click not delivered or an error is logged and the request goes without it;
        only ``_Abort`` (that window lost the foreground) ends the add, as it does for the greeting."""
        field, why = _remark_field(rows)
        if field is None:
            logger.info("Friend management: no remark field (备注) was seen in the application window (%s; read: %s); "
                        "the request goes without it", why, _landmarks(rows))
            return False
        try:
            left, top, right, bottom = rect
            rx, ry = _centre(field)
            x = left + int(rx * max(1, right - left))
            y = top + int(ry * max(1, bottom - top))
            if not self._click_at(apply_hwnd, x, y, required=False, exact=False):
                logger.info("Friend management: the click on the remark field was not delivered; "
                            "the request goes without the remark")
                return False
            self._sleep(0.1)
            self._combo(apply_hwnd, VK_CONTROL, VK_A, exact=True)
            self._sleep(0.05)
            self._key(apply_hwnd, VK_BACK, exact=True)
            self._sleep(0.05)
            self._paste(apply_hwnd, remark, exact=True)
            self._sleep(0.2)
        except _Abort:
            raise
        except Exception:
            logger.warning("Friend management: the remark could not be typed; the request goes without it",
                           exc_info=True)
            return False
        logger.info("Friend management: the remark was typed into the field below the 备注 label (y %.2f)",
                    float(field.get("y0", 0.0)))
        return True

    def _add_friend_in_window(self, hwnd: int, keyword: str, message: str, remark: str = "") -> dict:
        if self._is_wechat4(hwnd):
            return self._add_friend_wechat4(hwnd, keyword, message, remark)
        if remark and not self._remark_told:
            self._remark_told = True
            logger.info("Friend management: the remark (备注) is typed only into WeChat 4.0's application window; "
                        "this WeChat window is not 4.0's, so requests go without it (said once)")
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
        risk = _risk_line(card)
        if risk:
            self._dismiss(hwnd)
            return _risk_failure(keyword, risk)
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

    It accepts at most ``daily_max`` (AUTO_ACCEPT_DAILY_MAX) a day (this computer's calendar day), then
    rests until the next day; what WeChat says when it holds the account back (``AcceptResult.risk``), or
    a request that stays pending STUCK_RUNS checks in a row, pauses it until it is switched on again
    (``enable``: the admin's 自动同意好友 开).  ``notify(text)`` tells the admin (once per day for the cap).
    The day's count and a pause are kept in ``state_path`` (JSON), so a restart does not start over.
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
        daily_max: int = DAILY_MAX_DEFAULT,
        state_path=None,
        notify: Callable[[str], object] | None = None,
        wall_clock: Callable[[], float] = time.time,
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
        self._daily_max = max(1, int(daily_max))
        self._state_path = Path(state_path) if state_path else None
        self._notify = notify
        self._wall_clock = wall_clock
        self._day_lock = threading.Lock()
        self._day = self._load_day()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        """Switch on (a pause after a risk prompt is lifted: the admin decided); also starts the worker if it
        was never started (the config had it off).  The day's count stays: WeChat counts the day, not us."""
        with self._day_lock:
            if self._day.get("paused") or self._day.get("stuck"):
                self._day["paused"] = None
                self._day["stuck"] = 0
                self._save_day()
        self._enabled = True
        self.start()
        logger.info("AutoFriendService enabled")

    # ── the day's count and a pause (kept in state_path) ───────────────

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._wall_clock()))

    def _load_day(self) -> dict:
        day = {"day": "", "accepted": 0, "cap_told": False, "paused": None, "stuck": 0}
        if self._state_path is not None and self._state_path.is_file():
            try:
                saved = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    day.update({k: saved[k] for k in day if k in saved})
            except (OSError, ValueError):
                logger.warning("AutoFriendService: %s could not be read; starting the day's count afresh",
                               self._state_path)
        return day

    def _save_day(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._day, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            logger.warning("AutoFriendService: %s could not be written", self._state_path, exc_info=True)

    def _roll_day(self) -> None:
        today = self._today()
        if self._day.get("day") != today:
            self._day.update(day=today, accepted=0, cap_told=False)

    def accepted_today(self) -> int:
        with self._day_lock:
            self._roll_day()
            return int(self._day.get("accepted") or 0)

    def status_note(self) -> str:
        """For 查状态 and the switch's answer: where the day stands, or why it is paused."""
        with self._day_lock:
            self._roll_day()
            paused = self._day.get("paused")
            accepted = int(self._day.get("accepted") or 0)
        if paused:
            return f"已暂停：{paused.get('why', '')}；发「自动同意好友 开」恢复"
        if accepted >= self._daily_max:
            return f"今天已自动同意 {accepted} 个，到了上限 {self._daily_max}，明天继续"
        return f"今天已自动同意 {accepted}/{self._daily_max}"

    def paused_why(self) -> str:
        """Why it is paused ("" when it is not); only reads (加百应达人微信 asks before every add, src/prm/friend_adds.py)."""
        with self._day_lock:
            paused = self._day.get("paused")
        return str(paused.get("why") or "已暂停") if isinstance(paused, dict) else ("已暂停" if paused else "")

    def pause(self, why: str, tell: bool = True) -> None:
        """Stop accepting until switched on again (``enable``), also after a restart; ``tell``: the admin hears it."""
        with self._day_lock:
            self._day["paused"] = {"at": self._wall_clock(), "why": str(why)[:200]}
            self._save_day()
        logger.warning("AutoFriendService paused: %s", why)
        if tell:
            self._tell(f"自动同意好友已暂停：{why}。先别再加好友、同意好友，过几个小时再看；"
                       "要恢复自动同意，发「自动同意好友 开」。")

    def _tell(self, text: str) -> None:
        if self._notify is None:
            return
        try:
            self._notify(text)
        except Exception:
            logger.warning("AutoFriendService: the admin could not be told", exc_info=True)

    def _count(self, result) -> None:
        """Add a check's accepts to the day; a risk prompt or a request stuck STUCK_RUNS checks in a row
        pauses; reaching the cap tells the admin (once a day)."""
        risk = getattr(result, "risk", "")
        risk = risk if isinstance(risk, str) else ""
        stuck = getattr(result, "stuck", False) is True
        with self._day_lock:
            self._roll_day()
            self._day["accepted"] = int(self._day.get("accepted") or 0) + len(result)
            self._day["stuck"] = 0 if len(result) or not stuck else int(self._day.get("stuck") or 0) + 1
            stuck_runs = self._day["stuck"]
            self._save_day()
        if risk:
            self.pause(f"微信提示「{risk}」，可能是操作太频繁被限制了")
        elif stuck_runs >= STUCK_RUNS:
            self.pause(f"连续 {STUCK_RUNS} 次点了「接受」申请还在（可能是微信在限制，也可能是界面识别出了问题，详见日志）")
        self._tell_cap()

    def _tell_cap(self) -> None:
        with self._day_lock:
            accepted = int(self._day.get("accepted") or 0)
            if accepted < self._daily_max or self._day.get("cap_told"):
                return
            self._day["cap_told"] = True
            self._save_day()
        logger.info("AutoFriendService: %d accepted today, the daily cap %d: resting until tomorrow",
                    accepted, self._daily_max)
        self._tell(f"自动同意好友：今天已自动同意 {accepted} 个申请，到了每天的上限 {self._daily_max}"
                   "（AUTO_ACCEPT_DAILY_MAX），今天不再自动同意，明天自动继续。还有申请的话可以发「同意好友」手动同意。")

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
        with self._day_lock:
            self._roll_day()
            paused = bool(self._day.get("paused"))
            remaining = self._daily_max - int(self._day.get("accepted") or 0)
        if paused:
            return []                          # until the admin switches it on again
        if remaining <= 0:
            self._tell_cap()
            return []                          # the day's cap: the window is not touched
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
            accepted = self._manager.accept_friend_requests(limit=remaining)
            self._last_full_check = self._clock()
            self._mark_own_input()
        except Exception:
            logger.warning("AutoFriendService: the check failed", exc_info=True)
            return []
        if accepted:
            logger.info("AutoFriendService: accepted %d friend request(s)", len(accepted))
        self._count(accepted)
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
