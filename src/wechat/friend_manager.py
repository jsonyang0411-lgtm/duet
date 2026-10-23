"""Friend manager: accepting friend requests and adding friends in the WeChat PC window.

It drives the real window with keystrokes and mouse clicks and finds the buttons by
OCR, so every step is guarded:

- the window lock of the send path is shared, so a friend action never interleaves
  with a reply being typed;
- nothing is typed or clicked unless the WeChat window is in the foreground;
- nothing is clicked blind: a button OCR cannot find ends the action with a reason;
- an action is only reported as done when the screen shows it (the button is gone,
  the dialog is closed).

Features:
- accept_friend_requests: Contacts -> 新的朋友, accepts the pending requests (at most
  MAX_ACCEPT_PER_RUN per call).
- add_friend: searches the wxid / phone number, opens the card, "添加到通讯录", fills the
  greeting and sends the application.
- AutoFriendService: periodic background worker for accept_friend_requests.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from .keyboard import press_key, send_combo

logger = logging.getLogger(__name__)

VK_BACK = 0x08
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_A = 0x41
VK_F = 0x46
VK_V = 0x56

MAX_ACCEPT_PER_RUN = 10      # a burst of requests is taken in a few runs, not all at once
MAX_KEYWORD_CHARS = 64
MIN_INTERVAL_SEC = 10        # same floor as AUTO_ACCEPT_INTERVAL_SEC in config validation
SAME_SPOT = 0.02             # two OCR rows whose centres differ by less are "the same spot"

NEW_FRIENDS_LABELS = ("新的朋友", "好友申请")
ACCEPT_LABELS = ("接受", "通过", "同意", "前往验证")
PENDING_REQUEST_LABELS = ("等待验证",)
ACCEPT_MIN_X0 = 0.40         # the accept button sits on the right side of its row
CONFIRM_LABELS = ("确定", "完成")
SEARCH_ENTRY_LABELS = ("网络查找", "查找微信号", "查找手机号")
NOT_FOUND_MARKERS = ("该用户不存在", "用户不存在", "查找失败")
ALREADY_FRIEND_MARKERS = ("音视频通话", "语音聊天", "视频聊天")   # next to "发消息" on a friend's card
ADD_BUTTON_LABELS = ("添加到通讯录", "加为朋友", "加为联系人")
SEND_LABELS = ("发送", "确定", "完成")


class _Abort(Exception):
    """The window action cannot go on safely (WeChat lost the foreground, a click failed)."""


def _clean_text(text) -> str:
    """Strip all whitespace and lower-case, so OCR spacing never breaks a match."""
    return "".join(str(text or "").split()).lower()


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


def _failure(target: str, reason: str, message: str) -> dict:
    return {"ok": False, "target": target, "reason": reason, "message": message}


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
    ``rect_of`` and ``sleeper`` are seams for tests.
    """

    def __init__(
        self,
        window_controller=None,
        ocr_engine=None,
        ui_lock=None,
        clicker: Callable[[int, int], bool] | None = None,
        rect_of: Callable[[int], tuple[int, int, int, int]] = window_rect,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._window = window_controller
        self._ocr = ocr_engine
        self._ui_lock = ui_lock if ui_lock is not None else threading.Lock()
        self._clicker = clicker or click_screen
        self._rect_of = rect_of
        self._sleep = sleeper

    # ── Window session ──────────────────────────────────────────────────

    @contextmanager
    def _ui_session(self) -> Iterator[int | None]:
        """The WeChat window in front, under the shared lock; ``None`` if it cannot be shown.

        The window is put back the way it was found (minimized / hidden) afterwards.
        """
        with self._ui_lock:
            window = self._window
            hwnd = window.find_hwnd(force=True) if window is not None else None
            if not hwnd:
                logger.warning("Friend management: the WeChat window was not found")
                yield None
                return
            snapshot = window._capture_window_state(hwnd)
            try:
                if window._activate_for_send(hwnd):
                    yield hwnd
                else:
                    logger.warning("Friend management: WeChat could not be brought to the front")
                    yield None
            finally:
                window._restore_window_state(snapshot, hwnd)

    def _foreground(self, hwnd: int) -> None:
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

    def _key(self, hwnd: int, vk: int) -> None:
        self._foreground(hwnd)
        press_key(vk)

    def _combo(self, hwnd: int, mod_vk: int, key_vk: int) -> None:
        self._foreground(hwnd)
        send_combo(mod_vk, key_vk)

    def _paste(self, hwnd: int, text: str) -> None:
        self._window._clipboard_text(text, hwnd)
        self._sleep(0.15)
        self._combo(hwnd, VK_CONTROL, VK_V)

    def _click_row(self, hwnd: int, row: dict) -> None:
        self._foreground(hwnd)
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

    def _capture_ocr(self) -> list[dict]:
        """The OCR rows of the WeChat window (empty if OCR fails)."""
        try:
            return self._ocr.capture() or []
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
            return []
        accepted: list[dict] = []
        with self._ui_session() as hwnd:
            if hwnd is None:
                return accepted
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
                        break
                    self._click_row(hwnd, button)
                    self._sleep(0.5)
                    self._confirm_dialog(hwnd)
                    rows = self._capture_ocr()
                    if _still_there(rows, button):
                        logger.warning("accept_friend_requests: a request is still pending after "
                                       "the click; stopping")
                        break
                    accepted.append({"status": "accepted", "timestamp": time.time()})
                    logger.info("Friend request accepted")
            except _Abort as e:
                logger.warning("accept_friend_requests stopped: %s", e)
        if not accepted:
            logger.info("accept_friend_requests: nothing accepted")
        return accepted

    def _open_new_friends(self, hwnd: int) -> None:
        if not self._window._goto_contacts_tab(hwnd):
            raise _Abort("the Contacts tab did not open")
        self._sleep(0.4)
        row = _find_row(self._capture_ocr(), *NEW_FRIENDS_LABELS)
        if row is None:
            raise _Abort("'新的朋友' is not on the screen")
        self._click_row(hwnd, row)
        self._sleep(0.6)

    def _confirm_dialog(self, hwnd: int) -> None:
        """Click the confirmation of the accept dialog, if one opened."""
        row = _find_row(self._capture_ocr(), *CONFIRM_LABELS, exact=True)
        if row is not None:
            self._click_row(hwnd, row)
            self._sleep(0.5)
            return
        popup_hwnd = self._find_popup_dialog(hwnd)
        if popup_hwnd:
            self._confirm_popup_dialog(popup_hwnd)
            self._sleep(0.5)

    def _find_popup_dialog(self, hwnd: int) -> int | None:
        if sys.platform != "win32":
            return None
        try:
            import win32gui, win32process
            _, main_pid = win32process.GetWindowThreadProcessId(hwnd)
            modals = []
            def _enum(h, _):
                if h != hwnd and win32gui.IsWindow(h) and win32gui.IsWindowVisible(h):
                    _, pid = win32process.GetWindowThreadProcessId(h)
                    if pid == main_pid:
                        title = win32gui.GetWindowText(h)
                        if any(kw in title for kw in ("验证", "确定", "朋友")):
                            modals.append(h)
            win32gui.EnumWindows(_enum, None)
            return modals[0] if modals else None
        except Exception:
            return None

    def _confirm_popup_dialog(self, popup_hwnd: int) -> None:
        if sys.platform == "win32":
            import ctypes, win32gui
            ctypes.windll.user32.SetForegroundWindow(popup_hwnd)
            self._sleep(0.2)
            rect = win32gui.GetWindowRect(popup_hwnd)
            cx = rect[0] + int(0.318 * (rect[2] - rect[0]))
            cy = rect[1] + int(0.930 * (rect[3] - rect[1]))
            self._clicker(cx, cy)
            self._sleep(0.2)
            press_key(0x0D)

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

        with self._ui_session() as hwnd:
            if hwnd is None:
                return _failure(keyword, "no_window", "未找到微信主窗口或无法切到前台，请确保微信已启动")
            try:
                return self._add_friend_in_window(hwnd, keyword, message)
            except _Abort as e:
                logger.warning("add_friend stopped: %s", e)
                return _failure(keyword, "aborted", f"微信窗口状态异常，已中止：{e}")

    def _add_friend_in_window(self, hwnd: int, keyword: str, message: str) -> dict:
        # 1. Search box: Ctrl+F -> Ctrl+A -> Backspace -> paste the keyword
        self._combo(hwnd, VK_CONTROL, VK_F)
        self._sleep(0.2)
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
    """Background worker that runs FriendManager.accept_friend_requests every ``interval_sec``."""

    def __init__(
        self,
        friend_manager: FriendManager,
        interval_sec: int = 300,
        enabled: bool = True,
    ):
        self._manager = friend_manager
        self._interval = max(MIN_INTERVAL_SEC, int(interval_sec))
        self._enabled = enabled
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
        """One check now (does nothing while disabled); never raises."""
        if not self._enabled:
            return []
        try:
            accepted = self._manager.accept_friend_requests()
        except Exception:
            logger.warning("AutoFriendService: the check failed", exc_info=True)
            return []
        if accepted:
            logger.info("AutoFriendService: accepted %d friend request(s)", len(accepted))
        return accepted

    def _run(self) -> None:
        # The first check comes one interval after the start, not while the bot is still starting up.
        while not self._stop_event.wait(self._interval):
            self.run_once()
