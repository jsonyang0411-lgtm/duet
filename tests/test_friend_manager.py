"""Tests for WeChat friend manager (adding friends and auto-accepting friend requests)."""

import time
import unittest
from unittest.mock import MagicMock, patch

from src.admin import AdminCommandHandler
from src.config import BotConfig, _validate_config
from src.wechat.friend_manager import AutoFriendService, FriendManager


class MockWindowController:
    def __init__(self, hwnd=12345):
        self._hwnd = hwnd
        self._activated = False
        self._clipboard = []

    def find_hwnd(self, force=False):
        return self._hwnd

    def _activate_for_send(self, hwnd):
        self._activated = True

    def _goto_contacts_tab(self, hwnd):
        pass

    def _clipboard_text(self, text, hwnd):
        self._clipboard.append(text)


class TestFriendManager(unittest.TestCase):
    def setUp(self):
        self.clicked = []
        self.mock_clicker = lambda x, y: self.clicked.append((x, y)) or True
        self.mock_sleeper = lambda s: None

    def test_accept_friend_requests_no_window(self):
        mgr = FriendManager(window_controller=None, clicker=self.mock_clicker, sleeper=self.mock_sleeper)
        res = mgr.accept_friend_requests()
        self.assertEqual(res, [])

    def test_accept_friend_requests_none_pending(self):
        win = MockWindowController()
        mock_ocr = MagicMock()
        mock_ocr.capture.side_effect = [
            [{"t": "新的朋友", "x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2}],
            [{"t": "没有新朋友", "x0": 0.5, "y0": 0.5, "x1": 0.6, "y1": 0.6}],
        ]

        mgr = FriendManager(
            window_controller=win,
            ocr_engine=mock_ocr,
            clicker=self.mock_clicker,
            sleeper=self.mock_sleeper,
        )
        res = mgr.accept_friend_requests()
        self.assertEqual(res, [])
        self.assertTrue(len(self.clicked) >= 1)  # Clicked "新的朋友"

    def test_accept_friend_requests_success(self):
        win = MockWindowController()
        mock_ocr = MagicMock()
        mock_ocr.capture.side_effect = [
            # 1. Contacts list
            [{"t": "新的朋友", "x0": 0.15, "y0": 0.12, "x1": 0.25, "y1": 0.16}],
            # 2. Requests panel with "接受" button on the right
            [{"t": "张三 申请添加你为朋友", "x0": 0.35, "y0": 0.20, "x1": 0.50, "y1": 0.25},
             {"t": "接受", "x0": 0.70, "y0": 0.21, "x1": 0.76, "y1": 0.25}],
            # 3. Confirmation dialog
            [{"t": "通过朋友验证", "x0": 0.40, "y0": 0.30, "x1": 0.60, "y1": 0.35},
             {"t": "确定", "x0": 0.52, "y0": 0.60, "x1": 0.58, "y1": 0.64}],
        ]

        mgr = FriendManager(
            window_controller=win,
            ocr_engine=mock_ocr,
            clicker=self.mock_clicker,
            sleeper=self.mock_sleeper,
        )

        with patch("src.wechat.friend_manager.press_key") as mock_press:
            res = mgr.accept_friend_requests()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["status"], "accepted")
            # Should have clicked "新的朋友", "接受", and "确定"
            self.assertTrue(len(self.clicked) >= 3)

    def test_add_friend_empty_keyword(self):
        mgr = FriendManager(window_controller=MockWindowController())
        res = mgr.add_friend("")
        self.assertFalse(res["ok"])
        self.assertIn("请输入有效", res["message"])

    def test_add_friend_not_found(self):
        win = MockWindowController()
        mock_ocr = MagicMock()
        mock_ocr.capture.side_effect = [
            # Dropdown search
            [{"t": "网络查找微信号：test_wxid", "x0": 0.15, "y0": 0.15, "x1": 0.30, "y1": 0.20}],
            # Card response
            [{"t": "该用户不存在", "x0": 0.45, "y0": 0.40, "x1": 0.55, "y1": 0.45}],
        ]

        mgr = FriendManager(
            window_controller=win,
            ocr_engine=mock_ocr,
            clicker=self.mock_clicker,
            sleeper=self.mock_sleeper,
        )

        with patch("src.wechat.friend_manager.press_key") as mock_press, \
             patch("src.wechat.friend_manager.send_combo") as mock_combo:
            res = mgr.add_friend("test_wxid")
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "not_found")
            mock_press.assert_any_call(0x1B)  # Esc to dismiss

    def test_add_friend_already_friend(self):
        win = MockWindowController()
        mock_ocr = MagicMock()
        mock_ocr.capture.side_effect = [
            # Dropdown search
            [{"t": "查找手机号：13800000000", "x0": 0.15, "y0": 0.15, "x1": 0.30, "y1": 0.20}],
            # Card response
            [{"t": "发消息", "x0": 0.50, "y0": 0.60, "x1": 0.60, "y1": 0.65},
             {"t": "音视频通话", "x0": 0.50, "y0": 0.70, "x1": 0.60, "y1": 0.75}],
        ]

        mgr = FriendManager(
            window_controller=win,
            ocr_engine=mock_ocr,
            clicker=self.mock_clicker,
            sleeper=self.mock_sleeper,
        )

        with patch("src.wechat.friend_manager.press_key") as mock_press, \
             patch("src.wechat.friend_manager.send_combo") as mock_combo:
            res = mgr.add_friend("13800000000")
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "already_friend")

    def test_add_friend_success(self):
        win = MockWindowController()
        mock_ocr = MagicMock()
        mock_ocr.capture.side_effect = [
            # Dropdown
            [{"t": "网络查找微信号：newbie", "x0": 0.15, "y0": 0.15, "x1": 0.30, "y1": 0.20}],
            # Card
            [{"t": "添加到通讯录", "x0": 0.55, "y0": 0.70, "x1": 0.65, "y1": 0.75}],
            # Verification modal
            [{"t": "发送", "x0": 0.50, "y0": 0.65, "x1": 0.58, "y1": 0.70}],
        ]

        mgr = FriendManager(
            window_controller=win,
            ocr_engine=mock_ocr,
            clicker=self.mock_clicker,
            sleeper=self.mock_sleeper,
        )

        with patch("src.wechat.friend_manager.press_key") as mock_press, \
             patch("src.wechat.friend_manager.send_combo") as mock_combo:
            res = mgr.add_friend("newbie", message="Hello!")
            self.assertTrue(res["ok"])
            self.assertIn("已成功向「newbie」发送好友申请", res["message"])
            self.assertIn("Hello!", win._clipboard)


class TestAutoFriendService(unittest.TestCase):
    def test_service_lifecycle_and_tick(self):
        mock_mgr = MagicMock()
        mock_mgr.accept_friend_requests.return_value = [{"status": "accepted"}]

        service = AutoFriendService(mock_mgr, interval_sec=10, enabled=True)
        self.assertTrue(service.enabled)

        # Disable
        service.disable()
        self.assertFalse(service.enabled)

        # Enable
        service.enable()
        self.assertTrue(service.enabled)


class TestAdminFriendCommands(unittest.TestCase):
    def test_admin_commands_with_friend_manager(self):
        mock_mgr = MagicMock()
        mock_mgr.add_friend.return_value = {"ok": True, "message": "已成功向「13800000000」发送好友申请"}
        mock_mgr.accept_friend_requests.return_value = [{"status": "accepted"}]

        mock_auto = MagicMock()

        handler = AdminCommandHandler(
            nickname_service=MagicMock(),
            friend_manager=mock_mgr,
            auto_friend_service=mock_auto,
        )

        # 1. Add friend command
        r = handler.handle("添加好友 13800000000 请通过", "Admin")
        self.assertIn("已成功向「13800000000」发送好友申请", r)
        mock_mgr.add_friend.assert_called_once_with("13800000000", "请通过")

        # 2. Accept friend command
        r = handler.handle("同意好友", "Admin")
        self.assertIn("已成功同意 1 位新好友申请", r)
        mock_mgr.accept_friend_requests.assert_called_once()

        # 3. Toggle auto-accept
        r = handler.handle("自动同意好友 关", "Admin")
        self.assertIn("已关闭", r)
        mock_auto.disable.assert_called_once()

        r = handler.handle("自动同意好友 开", "Admin")
        self.assertIn("已开启", r)
        mock_auto.enable.assert_called_once()

    def test_admin_commands_without_friend_manager(self):
        handler = AdminCommandHandler(nickname_service=MagicMock())
        r = handler.handle("添加好友 13800000000", "Admin")
        self.assertIn("未启用好友管理组件", r)

        r = handler.handle("同意好友", "Admin")
        self.assertIn("未启用好友管理组件", r)


class TestFriendConfigValidation(unittest.TestCase):
    def test_valid_config(self):
        cfg = {"auto_accept_friends": "on", "auto_accept_interval_sec": 300}
        # Should not raise RuntimeError
        _validate_config(cfg)

    def test_invalid_auto_accept(self):
        cfg = {"auto_accept_friends": "invalid"}
        with self.assertRaises(RuntimeError):
            _validate_config(cfg)

    def test_invalid_interval(self):
        cfg = {"auto_accept_interval_sec": 5}
        with self.assertRaises(RuntimeError):
            _validate_config(cfg)
