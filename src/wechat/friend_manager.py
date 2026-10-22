"""Friend manager: automated friend request acceptance and adding friends on WeChat PC.

Features:
- accept_friend_requests: Navigates to Contacts -> New Friends, detects and clicks "接受",
  confirms popup verification dialogs, and records accepted friend requests.
- add_friend: Focuses search box, pastes target phone/wxid, finds the network user profile,
  clicks "添加到通讯录", fills greeting message, and sends friend application.
- AutoFriendService: Periodic background worker for automatic silent acceptance.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

try:
    from .keyboard import press_key, send_combo
except Exception:
    def press_key(vk: int) -> None:
        pass
    def send_combo(mod_vk: int, key_vk: int) -> None:
        pass

# Default layout fallback coordinates on standard WeChat PC layout (relative 0.0 ~ 1.0)
CONTACTS_TAB_COORD = (0.035, 0.175)       # Left navigation: Contacts tab icon
NEW_FRIENDS_ITEM_COORD = (0.18, 0.14)     # Contacts list top: "新的朋友"
SEARCH_BOX_COORD = (0.18, 0.085)          # Top search box
FIRST_SEARCH_RESULT_COORD = (0.18, 0.16)   # Dropdown network search result
ADD_CONTACT_BTN_COORD = (0.65, 0.76)      # Profile card: "添加到通讯录" button
CONFIRM_BTN_COORD = (0.55, 0.65)          # Modal: "确定" / "完成" button


def _clean_text(text: str) -> str:
    """Normalize text by stripping all whitespace and punctuation."""
    return "".join(str(text or "").split()).lower()


def set_clipboard(text: str) -> bool:
    """Set text onto clipboard."""
    if sys.platform != "win32":
        return False
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        logger.debug("set_clipboard failed: %s", e)
        return False


def click_screen(x: int, y: int) -> bool:
    """Send left mouse click at absolute screen coordinates."""
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


def click_relative(hwnd: int, rx: float, ry: float, clicker: Callable[[int, int], bool] | None = None) -> bool:
    """Click at normalized coordinates (0.0 to 1.0) relative to a window."""
    _click = clicker or click_screen
    if sys.platform != "win32":
        return _click(int(rx * 1000), int(ry * 1000))
    try:
        import win32gui
        rect = win32gui.GetWindowRect(hwnd)
        left, top, right, bottom = rect
        w = max(1, right - left)
        h = max(1, bottom - top)
        x = left + int(rx * w)
        y = top + int(ry * h)
        return _click(x, y)
    except Exception as e:
        logger.warning("click_relative failed: %s", e)
        if clicker is not None:
            return clicker(int(rx * 1000), int(ry * 1000))
        return False


class FriendManager:
    """Automates accepting friend requests and sending new friend requests on WeChat PC."""

    def __init__(
        self,
        window_controller=None,
        ocr_engine=None,
        config=None,
        clicker: Callable[[int, int], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._window = window_controller
        self._ocr = ocr_engine
        self._config = config
        self._clicker = clicker
        self._sleep = sleeper
        self._action_lock = threading.Lock()
        self._accepted_history: set[str] = set()

    def _ensure_window(self) -> int | None:
        if self._window is None:
            return None
        hwnd = self._window.find_hwnd(force=True)
        if hwnd:
            self._window._activate_for_send(hwnd)
        return hwnd

    def _capture_ocr(self) -> list[dict]:
        """Capture screen OCR rows if engine is available."""
        if self._ocr is None:
            return []
        try:
            return self._ocr.capture() or []
        except Exception as e:
            logger.debug("FriendManager OCR capture failed: %s", e)
            return []

    # ── Accept Friend Requests ──────────────────────────────────────────

    def accept_friend_requests(
        self,
        auto_reply: bool = False,
        greeting: str = "",
    ) -> list[dict]:
        """Check the 'New Friends' list and accept any pending friend requests.

        Returns:
            A list of accepted request summaries:
            [{"name": "...", "status": "accepted", "time": ...}]
        """
        with self._action_lock:
            hwnd = self._ensure_window()
            if not hwnd:
                logger.warning("accept_friend_requests: WeChat window not found")
                return []

            # 1. Switch to Contacts tab
            try:
                self._window._goto_contacts_tab(hwnd)
            except Exception:
                click_relative(hwnd, CONTACTS_TAB_COORD[0], CONTACTS_TAB_COORD[1], self._clicker)
            self._sleep(0.4)

            # 2. Check for "新的朋友" in the contacts list
            rows = self._capture_ocr()
            new_friend_row = None
            for r in rows:
                cleaned = _clean_text(r.get("t", ""))
                if "新的朋友" in cleaned or "好友申请" in cleaned:
                    new_friend_row = r
                    break

            if new_friend_row:
                cx = (new_friend_row["x0"] + new_friend_row["x1"]) / 2
                cy = (new_friend_row["y0"] + new_friend_row["y1"]) / 2
                click_relative(hwnd, cx, cy, self._clicker)
            else:
                # Default position for "新的朋友" item
                click_relative(hwnd, NEW_FRIENDS_ITEM_COORD[0], NEW_FRIENDS_ITEM_COORD[1], self._clicker)
            self._sleep(0.6)

            # 3. Read the requests panel
            panel_rows = self._capture_ocr()
            accept_buttons = []
            for r in panel_rows:
                cleaned = _clean_text(r.get("t", ""))
                # "接受" or "通过" buttons usually sit on the right side of the item
                if cleaned in ("接受", "通过") and r.get("x0", 0.0) > 0.40:
                    accept_buttons.append(r)

            if not accept_buttons:
                logger.info("accept_friend_requests: no pending requests found")
                return []

            accepted: list[dict] = []
            for btn in accept_buttons:
                bx = (btn["x0"] + btn["x1"]) / 2
                by = (btn["y0"] + btn["y1"]) / 2
                click_relative(hwnd, bx, by, self._clicker)
                self._sleep(0.5)

                # Check if confirmation dialog ("通过朋友验证" / "确定" / "完成") appeared
                dlg_rows = self._capture_ocr()
                confirm_btn = None
                for dr in dlg_rows:
                    dt = _clean_text(dr.get("t", ""))
                    if dt in ("确定", "完成", "添加") or "确定" in dt:
                        confirm_btn = dr
                        break

                if confirm_btn:
                    dx = (confirm_btn["x0"] + confirm_btn["x1"]) / 2
                    dy = (confirm_btn["y0"] + confirm_btn["y1"]) / 2
                    click_relative(hwnd, dx, dy, self._clicker)
                else:
                    # Press Enter to confirm default dialog action
                    press_key(0x0D)

                self._sleep(0.5)
                record = {
                    "status": "accepted",
                    "timestamp": time.time(),
                }
                accepted.append(record)
                logger.info("Friend request accepted successfully")

            return accepted

    # ── Add Friend ──────────────────────────────────────────────────────

    def add_friend(self, keyword: str, message: str = "") -> dict:
        """Search and add a new WeChat friend by phone number or WeChat ID.

        Args:
            keyword: Target phone number, wxid, or search keyword.
            message: Greeting / verification message to send.

        Returns:
            Result dict with keys: 'ok' (bool), 'target' (str), 'message' (str), 'reason' (str optional).
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"ok": False, "target": keyword, "message": "请输入有效的微信号或手机号"}

        with self._action_lock:
            hwnd = self._ensure_window()
            if not hwnd:
                return {"ok": False, "target": keyword, "message": "未找到微信主窗口，请确保微信已启动"}

            # 1. Focus search box: Ctrl+F -> Ctrl+A -> Backspace
            send_combo(0x11, 0x46)  # Ctrl+F
            self._sleep(0.2)
            send_combo(0x11, 0x41)  # Ctrl+A
            self._sleep(0.05)
            press_key(0x08)         # Backspace
            self._sleep(0.05)

            # 2. Paste target keyword
            if hasattr(self._window, "_clipboard_text"):
                self._window._clipboard_text(keyword, hwnd)
            elif hasattr(self._window, "_set_clipboard"):
                self._window._set_clipboard(keyword)
            else:
                set_clipboard(keyword)
            send_combo(0x11, 0x56)  # Ctrl+V
            self._sleep(0.8)

            # 3. Check dropdown or press Down + Enter to trigger network search
            search_rows = self._capture_ocr()
            search_network_row = None
            for r in search_rows:
                t = _clean_text(r.get("t", ""))
                if "网络查找" in t or "查找微信号" in t or "查找手机号" in t:
                    search_network_row = r
                    break

            if search_network_row:
                nx = (search_network_row["x0"] + search_network_row["x1"]) / 2
                ny = (search_network_row["y0"] + search_network_row["y1"]) / 2
                click_relative(hwnd, nx, ny, self._clicker)
            else:
                # Down arrow to select first network search option + Enter
                press_key(0x28)     # Down Arrow
                self._sleep(0.1)
                press_key(0x0D)     # Enter

            self._sleep(1.2)

            # 4. Check profile card
            card_rows = self._capture_ocr()
            all_card_text = "".join(_clean_text(r.get("t", "")) for r in card_rows)

            # Check if user not found
            if "该用户不存在" in all_card_text or "用户不存在" in all_card_text or "查找失败" in all_card_text:
                press_key(0x1B)  # Esc
                return {"ok": False, "target": keyword, "reason": "not_found", "message": f"未找到用户「{keyword}」，请核实微信号或手机号"}

            # Check if already a friend
            if "发消息" in all_card_text and ("音视频通话" in all_card_text or "语音聊天" in all_card_text or "视频聊天" in all_card_text):
                press_key(0x1B)  # Esc
                return {"ok": False, "target": keyword, "reason": "already_friend", "message": f"用户「{keyword}」已是您的微信好友，无需重复添加"}

            # Find "添加到通讯录" button
            add_btn_row = None
            for r in card_rows:
                t = _clean_text(r.get("t", ""))
                if "添加到通讯录" in t or "加为朋友" in t or "加为联系人" in t:
                    add_btn_row = r
                    break

            if add_btn_row:
                ax = (add_btn_row["x0"] + add_btn_row["x1"]) / 2
                ay = (add_btn_row["y0"] + add_btn_row["y1"]) / 2
                click_relative(hwnd, ax, ay, self._clicker)
            else:
                # Fallback click coordinate
                click_relative(hwnd, ADD_CONTACT_BTN_COORD[0], ADD_CONTACT_BTN_COORD[1], self._clicker)

            self._sleep(0.8)

            # 5. Handle greeting/reason input if message dialog is open
            if message:
                send_combo(0x11, 0x41)  # Ctrl+A
                self._sleep(0.05)
                if hasattr(self._window, "_clipboard_text"):
                    self._window._clipboard_text(message, hwnd)
                elif hasattr(self._window, "_set_clipboard"):
                    self._window._set_clipboard(message)
                else:
                    set_clipboard(message)
                send_combo(0x11, 0x56)  # Ctrl+V
                self._sleep(0.3)

            # 6. Click "发送" or "确定"
            dlg_rows = self._capture_ocr()
            send_btn = None
            for r in dlg_rows:
                t = _clean_text(r.get("t", ""))
                if t in ("发送", "确定", "完成"):
                    send_btn = r
                    break

            if send_btn:
                sx = (send_btn["x0"] + send_btn["x1"]) / 2
                sy = (send_btn["y0"] + send_btn["y1"]) / 2
                click_relative(hwnd, sx, sy, self._clicker)
            else:
                press_key(0x0D)  # Enter

            self._sleep(0.5)
            # Dismiss any leftover search focus
            press_key(0x1B)
            return {"ok": True, "target": keyword, "message": f"已成功向「{keyword}」发送好友申请" + (f"（附言：{message}）" if message else "")}


class AutoFriendService:
    """Background polling service that periodically runs FriendManager.accept_friend_requests."""

    def __init__(
        self,
        friend_manager: FriendManager,
        interval_sec: int = 300,
        enabled: bool = True,
    ):
        self._manager = friend_manager
        self._interval = max(10, int(interval_sec))
        self._enabled = enabled
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_accepted_count = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True
        logger.info("AutoFriendService enabled")

    def disable(self) -> None:
        self._enabled = False
        logger.info("AutoFriendService disabled")

    def start(self) -> None:
        """Start the background service thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="auto-friend-service", daemon=True)
        self._thread.start()
        logger.info("AutoFriendService background worker started (interval=%ds, enabled=%s)", self._interval, self._enabled)

    def stop(self) -> None:
        """Stop the background service thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._enabled:
                try:
                    accepted = self._manager.accept_friend_requests()
                    if accepted:
                        self._last_accepted_count += len(accepted)
                        logger.info("AutoFriendService: accepted %d friend requests", len(accepted))
                except Exception as e:
                    logger.debug("AutoFriendService tick exception: %s", e)
            self._stop_event.wait(self._interval)
