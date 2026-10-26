"""Tests for WeChat friend management (adding friends, accepting requests, the admin commands).

The WeChat window, the keyboard and the OCR are fakes: the tests pin what the manager
types and clicks, and that it types and clicks nothing it cannot see or is not allowed to.
"""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.admin import AdminCommandHandler
from src.bot import Bot
from src.config import BotConfig, _validate_config, load_config
from src.wechat import friend_manager
from src.wechat.friend_manager import AutoFriendService, FriendManager
from src.wechat.wcdb_backend import WcdbBackend

HWND = 12345
VK_ESCAPE, VK_RETURN, VK_BACK = 0x1B, 0x0D, 0x08
CTRL, KEY_A, KEY_F, KEY_V = 0x11, 0x41, 0x46, 0x56
WAIT = 5  # seconds; only ever spent when a test is failing


def row(text, x0, y0, x1, y1):
    return {"t": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def centre(r):
    """Where a click on ``r`` lands in a 1000 x 1000 window."""
    return int((r["x0"] + r["x1"]) / 2 * 1000), int((r["y0"] + r["y1"]) / 2 * 1000)


class FakeWindow:
    """The window controller as FriendManager uses it."""

    def __init__(self, hwnd=HWND, activates=True, contacts_open=True):
        self.contacts_calls = 0
        self.hwnd = hwnd
        self.activates = activates
        self.contacts_open = contacts_open
        self.foreground = True
        self.pasted = []
        self.restored = []
        self.activations = 0
        self.find_calls = []             # (force, wake) of every find_hwnd
        self.previous = 777              # the window the user was in
        self.focus_returned = []
        self._desktop = SimpleNamespace(foreground_window=lambda: self.previous)

    def find_hwnd(self, force=False, wake=True):
        self.find_calls.append((force, wake))
        return self.hwnd

    def _return_focus(self, previous):
        self.focus_returned.append(previous)

    def _capture_window_state(self, hwnd):
        return ("state", hwnd)

    def _activate_for_send(self, hwnd):
        self.activations += 1
        return self.activates

    def _restore_window_state(self, snapshot, hwnd):
        self.restored.append(snapshot)

    def _foreground_matches(self, hwnd):
        return self.foreground

    def _goto_contacts_tab(self, hwnd):
        self.contacts_calls += 1
        return self.contacts_open

    def _clipboard_text(self, text, hwnd):
        self.pasted.append(text)


class FakeOcr:
    """One screen per capture(), in order; the last one stays once the list runs out."""

    def __init__(self, *screens, after_capture=None):
        self.screens = list(screens)
        self.captures = 0
        self.hwnds = []              # the window each capture() was asked for (None = the main window)
        self._after = after_capture

    def capture(self, hwnd=None):
        screen = self.screens[min(self.captures, len(self.screens) - 1)]
        self.captures += 1
        self.hwnds.append(hwnd)
        if self._after:
            self._after(self.captures)
        return screen


class FriendTestCase(unittest.TestCase):
    def setUp(self):
        self.clicks = []
        self.keys = []
        self.events = []             # clicks and keys in the order they happened
        for name, record in (("press_key", lambda vk: self._key_event(("key", vk))),
                             ("send_combo", lambda mod, key: self._key_event(("combo", mod, key)))):
            patcher = patch.object(friend_manager, name, side_effect=record)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _key_event(self, event):
        self.keys.append(event)
        self.events.append(event)

    def manager(self, window=None, ocr=None, lock=None, click_ok=True):
        def click(x, y):
            self.clicks.append((x, y))
            self.events.append(("click", x, y))
            return click_ok
        return FriendManager(
            window_controller=window, ocr_engine=ocr, ui_lock=lock, clicker=click,
            rect_of=lambda hwnd: (0, 0, 1000, 1000), sleeper=lambda s: None,
        )

    def assertNothingHappened(self):
        self.assertEqual(self.keys, [])
        self.assertEqual(self.clicks, [])


class AcceptFriendRequestsTests(FriendTestCase):
    NEW_FRIENDS = row("新的朋友", 0.15, 0.12, 0.25, 0.16)

    def test_without_a_window_nothing_happens(self):
        self.assertEqual(self.manager(None, FakeOcr([])).accept_friend_requests(), [])
        self.assertNothingHappened()

    def test_without_ocr_nothing_is_clicked_blind(self):
        window = FakeWindow()
        self.assertEqual(self.manager(window, None).accept_friend_requests(), [])
        self.assertNothingHappened()
        self.assertEqual(window.restored, [])

    def test_window_not_found(self):
        window = FakeWindow(hwnd=None)
        self.assertEqual(self.manager(window, FakeOcr([])).accept_friend_requests(), [])
        self.assertNothingHappened()

    def test_failed_activation_types_and_clicks_nothing_and_puts_the_window_back(self):
        window = FakeWindow(activates=False)
        ocr = FakeOcr([self.NEW_FRIENDS])
        self.assertEqual(self.manager(window, ocr).accept_friend_requests(), [])
        self.assertNothingHappened()
        self.assertEqual(ocr.captures, 0)
        self.assertEqual(window.restored, [("state", HWND)])

    def test_contacts_tab_that_does_not_open_stops_the_run(self):
        window = FakeWindow(contacts_open=False)
        self.assertEqual(self.manager(window, FakeOcr([self.NEW_FRIENDS])).accept_friend_requests(), [])
        self.assertNothingHappened()

    def test_missing_new_friends_entry_is_not_clicked_for_blind(self):
        ocr = FakeOcr([row("别的东西", 0.1, 0.1, 0.2, 0.2)])
        self.assertEqual(self.manager(FakeWindow(), ocr).accept_friend_requests(), [])
        self.assertNothingHappened()

    def test_missing_new_friends_entry_retries_after_scroll_to_top(self):
        ocr = FakeOcr(
            [row("联系人", 0.1, 0.1, 0.2, 0.2)],
            [self.NEW_FRIENDS],
            [row("没有新朋友", 0.5, 0.5, 0.6, 0.6)],
        )
        manager = self.manager(FakeWindow(), ocr)
        manager._scroll_contacts_top = MagicMock()
        self.assertEqual(manager.accept_friend_requests(), [])
        manager._scroll_contacts_top.assert_called_once()
        self.assertEqual(self.clicks, [centre(self.NEW_FRIENDS)])

    def test_no_click_while_wechat_is_not_in_front(self):
        window = FakeWindow()
        window.foreground = False
        self.assertEqual(self.manager(window, FakeOcr([self.NEW_FRIENDS])).accept_friend_requests(), [])
        self.assertNothingHappened()

    def test_nothing_pending(self):
        ocr = FakeOcr([self.NEW_FRIENDS], [row("没有新朋友", 0.5, 0.5, 0.6, 0.6)])
        self.assertEqual(self.manager(FakeWindow(), ocr).accept_friend_requests(), [])
        self.assertEqual(self.clicks, [centre(self.NEW_FRIENDS)])

    def test_accepts_a_request_and_confirms_its_dialog(self):
        accept = row("接受", 0.70, 0.21, 0.76, 0.25)
        confirm = row("确定", 0.52, 0.60, 0.58, 0.64)
        ocr = FakeOcr(
            [self.NEW_FRIENDS],                                                    # contacts list
            [row("张三 申请添加你为朋友", 0.35, 0.20, 0.50, 0.25), accept],        # requests panel
            [row("通过朋友验证", 0.40, 0.30, 0.60, 0.35), confirm],                # dialog
            [row("张三", 0.35, 0.20, 0.50, 0.25)],                                 # button gone
        )
        result = self.manager(FakeWindow(), ocr).accept_friend_requests()
        self.assertEqual([r["status"] for r in result], ["accepted"])
        self.assertEqual(self.clicks, [centre(self.NEW_FRIENDS), centre(accept), centre(confirm)])
        self.assertEqual(self.keys, [])          # no blind Enter

    def test_without_a_dialog_the_gone_button_is_enough(self):
        accept = row("接受", 0.70, 0.21, 0.76, 0.25)
        ocr = FakeOcr([self.NEW_FRIENDS], [accept], [], [])
        self.assertEqual(len(self.manager(FakeWindow(), ocr).accept_friend_requests()), 1)
        self.assertEqual(self.keys, [])

    def test_reads_the_screen_again_after_each_click_because_the_list_moves(self):
        first = row("接受", 0.70, 0.20, 0.76, 0.24)
        second = row("接受", 0.70, 0.32, 0.76, 0.36)
        second_moved = row("接受", 0.70, 0.27, 0.76, 0.31)
        ocr = FakeOcr(
            [self.NEW_FRIENDS],
            [second, first],                 # the topmost one goes first
            [],                              # no dialog
            [second_moved],                  # the first is gone and the list moved up
            [],                              # no dialog
            [],                              # the second is gone too
        )
        result = self.manager(FakeWindow(), ocr).accept_friend_requests()
        self.assertEqual(len(result), 2)
        self.assertEqual(self.clicks, [centre(self.NEW_FRIENDS), centre(first), centre(second_moved)])

    def test_a_button_that_is_still_there_is_not_counted_and_not_clicked_again(self):
        accept = row("接受", 0.70, 0.21, 0.76, 0.25)
        ocr = FakeOcr([self.NEW_FRIENDS], [accept], [], [accept])
        self.assertEqual(self.manager(FakeWindow(), ocr).accept_friend_requests(), [])
        self.assertEqual(self.clicks, [centre(self.NEW_FRIENDS), centre(accept)])

    def test_at_most_max_accept_per_run_requests_are_taken(self):
        b1, b2, b3 = (row("接受", 0.70, y, 0.76, y + 0.04) for y in (0.20, 0.30, 0.40))
        b2_up, b3_up = row("接受", 0.70, 0.25, 0.76, 0.29), row("接受", 0.70, 0.35, 0.76, 0.39)
        ocr = FakeOcr([self.NEW_FRIENDS], [b1, b2, b3], [], [b2_up, b3_up], [], [b3_up])
        with patch.object(friend_manager, "MAX_ACCEPT_PER_RUN", 2):
            result = self.manager(FakeWindow(), ocr).accept_friend_requests()
        self.assertEqual(len(result), 2)
        self.assertEqual(len(self.clicks), 3)

    def test_a_slow_screen_gets_a_few_more_looks_before_the_request_counts_as_pending(self):
        accept = row("接受", 0.70, 0.21, 0.76, 0.25)
        ocr = FakeOcr([self.NEW_FRIENDS], [accept], [], [accept], [accept], [])      # gone on the second re-look
        self.assertEqual(len(self.manager(FakeWindow(), ocr).accept_friend_requests()), 1)

    def test_a_button_that_never_goes_away_is_looked_at_a_few_times_and_not_counted(self):
        accept = row("接受", 0.70, 0.21, 0.76, 0.25)
        ocr = FakeOcr([self.NEW_FRIENDS], [accept], [], [accept])                    # the last screen stays
        self.assertEqual(self.manager(FakeWindow(), ocr).accept_friend_requests(), [])
        self.assertEqual(ocr.captures, 2 + 1 + 1 + 3)                                # nav, panel, dialog, after click, 3 re-looks
        self.assertEqual(len(self.clicks), 2)                                        # and never clicked again

    def test_a_click_that_is_not_delivered_ends_the_run(self):
        ocr = FakeOcr([self.NEW_FRIENDS], [row("接受", 0.70, 0.21, 0.76, 0.25)])
        self.assertEqual(self.manager(FakeWindow(), ocr, click_ok=False).accept_friend_requests(), [])
        self.assertEqual(len(self.clicks), 1)

    def test_the_window_is_put_back_and_the_lock_released(self):
        window, lock = FakeWindow(), threading.Lock()
        self.manager(window, FakeOcr([self.NEW_FRIENDS], []), lock).accept_friend_requests()
        self.assertEqual(window.restored, [("state", HWND)])
        self.assertFalse(lock.locked())

    def test_it_waits_for_the_lock_of_the_send_path(self):
        lock = threading.Lock()
        manager = self.manager(FakeWindow(), FakeOcr([self.NEW_FRIENDS], []), lock)
        lock.acquire()
        worker = threading.Thread(target=manager.accept_friend_requests)
        worker.start()
        worker.join(0.2)
        self.assertTrue(worker.is_alive())       # a reply is being typed: the manager waits
        self.assertEqual(self.clicks, [])
        lock.release()
        worker.join(WAIT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.clicks), 1)


class AddFriendTests(FriendTestCase):
    ENTRY = row("网络查找微信号：newbie", 0.15, 0.15, 0.30, 0.20)
    ADD = row("添加到通讯录", 0.55, 0.70, 0.65, 0.75)
    SEND = row("发送", 0.50, 0.65, 0.58, 0.70)
    SEARCH_BOX = row("搜索", 0.06, 0.04, 0.14, 0.08)

    def test_empty_keyword(self):
        res = self.manager(FakeWindow(), FakeOcr([])).add_friend("   ")
        self.assertFalse(res["ok"])
        self.assertIn("请输入有效", res["message"])
        self.assertNothingHappened()

    def test_overlong_keyword(self):
        res = self.manager(FakeWindow(), FakeOcr([])).add_friend("1" * 200)
        self.assertEqual(res["reason"], "invalid")
        self.assertNothingHappened()

    def test_keyword_is_one_clean_line(self):
        window = FakeWindow()
        self.manager(window, FakeOcr([self.SEARCH_BOX])).add_friend(" 138\n0000 ")
        self.assertEqual(window.pasted, ["138 0000"])

    def test_without_ocr_nothing_is_typed(self):
        res = self.manager(FakeWindow(), None).add_friend("newbie")
        self.assertEqual(res["reason"], "no_ocr")
        self.assertNothingHappened()

    def test_without_a_window(self):
        res = self.manager(FakeWindow(hwnd=None), FakeOcr([])).add_friend("newbie")
        self.assertEqual(res["reason"], "no_window")
        self.assertNothingHappened()

    def test_failed_activation_types_nothing(self):
        window = FakeWindow(activates=False)
        res = self.manager(window, FakeOcr([])).add_friend("newbie")
        self.assertEqual(res["reason"], "no_window")
        self.assertNothingHappened()
        self.assertEqual(window.pasted, [])

    def test_without_a_search_box_on_the_screen_nothing_is_typed_or_clicked(self):
        window = FakeWindow()
        res = self.manager(window, FakeOcr([row("张三", 0.1, 0.2, 0.2, 0.3)])).add_friend("newbie", "你好")
        self.assertEqual(res["reason"], "no_search_box")
        self.assertNothingHappened()
        self.assertEqual(window.pasted, [])

    def test_a_search_word_elsewhere_on_the_screen_is_not_the_search_box(self):
        for other in (row("搜索", 0.60, 0.04, 0.70, 0.08),        # right half: not the search field
                      row("搜索", 0.06, 0.40, 0.14, 0.44),        # a chat called 搜索 in the list
                      row("搜索结果", 0.06, 0.04, 0.20, 0.08)):    # not the placeholder
            self.clicks.clear()
            res = self.manager(FakeWindow(), FakeOcr([other])).add_friend("newbie")
            self.assertEqual(res["reason"], "no_search_box", other)
            self.assertEqual(self.clicks, [])

    def test_the_search_box_is_read_even_when_the_magnifier_comes_out_as_a_letter(self):
        box = row("Q 搜索", 0.06, 0.04, 0.16, 0.08)
        ocr = FakeOcr([box], [row("张三", 0.1, 0.2, 0.2, 0.3)])
        res = self.manager(FakeWindow(), ocr).add_friend("newbie")
        self.assertEqual(res["reason"], "no_search_entry")
        self.assertEqual(self.clicks, [centre(box)])

    def test_the_whole_chain_is_ocr_no_shortcut_no_tab_switch_before_the_first_click(self):
        window = FakeWindow()
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [self.ADD], [self.SEND], [])
        self.assertTrue(self.manager(window, ocr).add_friend("newbie", "你好")["ok"])
        self.assertEqual(self.events[0][0], "click")                       # the first thing done is a click on a seen box
        self.assertNotIn(("combo", CTRL, KEY_F), self.keys)                # no Ctrl+F shortcut
        self.assertEqual(window.contacts_calls, 0)                         # no tab navigation outside OCR
        self.assertNotIn(("key", VK_RETURN), self.keys)                    # no blind Enter

    def test_no_network_search_entry_stops_before_any_click(self):
        window = FakeWindow()
        res = self.manager(window, FakeOcr([self.SEARCH_BOX], [row("张三", 0.1, 0.2, 0.2, 0.3)])).add_friend("newbie", "你好")
        self.assertEqual(res["reason"], "no_search_entry")
        self.assertEqual(self.clicks, [centre(self.SEARCH_BOX)])
        self.assertEqual(window.pasted, ["newbie"])      # the greeting was never pasted
        self.assertIn(("key", VK_ESCAPE), self.keys)
        self.assertNotIn(("key", VK_RETURN), self.keys)

    def test_user_not_found(self):
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [row("该用户不存在", 0.45, 0.40, 0.55, 0.45)])
        res = self.manager(FakeWindow(), ocr).add_friend("newbie")
        self.assertEqual(res["reason"], "not_found")
        self.assertEqual(self.clicks, [centre(self.SEARCH_BOX), centre(self.ENTRY)])
        self.assertIn(("key", VK_ESCAPE), self.keys)

    def test_already_a_friend(self):
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [row("发消息", 0.50, 0.60, 0.60, 0.65), row("音视频通话", 0.50, 0.70, 0.60, 0.75)])
        res = self.manager(FakeWindow(), ocr).add_friend("newbie")
        self.assertEqual(res["reason"], "already_friend")

    def test_no_add_button_is_not_clicked_for_blind(self):
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [row("某个资料卡", 0.5, 0.5, 0.6, 0.6)])
        res = self.manager(FakeWindow(), ocr).add_friend("newbie")
        self.assertEqual(res["reason"], "no_add_button")
        self.assertEqual(self.clicks, [centre(self.SEARCH_BOX), centre(self.ENTRY)])

    def test_the_greeting_is_never_pasted_when_no_application_dialog_opened(self):
        window = FakeWindow()
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [self.ADD], [row("已发送", 0.5, 0.5, 0.6, 0.6)])
        res = self.manager(window, ocr).add_friend("newbie", "你好")
        self.assertEqual(res["reason"], "no_dialog")
        self.assertEqual(window.pasted, ["newbie"])
        self.assertNotIn(("key", VK_RETURN), self.keys)
        self.assertEqual(self.clicks, [centre(self.SEARCH_BOX), centre(self.ENTRY), centre(self.ADD)])

    def test_sends_the_application_with_the_greeting(self):
        window = FakeWindow()
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [self.ADD], [self.SEND], [row("新朋友", 0.1, 0.1, 0.2, 0.2)])
        res = self.manager(window, ocr).add_friend("newbie", "Hello!")
        self.assertTrue(res["ok"])
        self.assertIn("已成功向「newbie」发送好友申请", res["message"])
        self.assertIn("Hello!", res["message"])
        self.assertEqual(window.pasted, ["newbie", "Hello!"])
        self.assertEqual(self.clicks, [centre(self.SEARCH_BOX), centre(self.ENTRY), centre(self.ADD), centre(self.SEND)])
        self.assertEqual(self.keys[:3], [("combo", CTRL, KEY_A), ("key", VK_BACK), ("combo", CTRL, KEY_V)])
        self.assertNotIn(("key", VK_RETURN), self.keys)

    def test_sends_without_a_greeting(self):
        window = FakeWindow()
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [self.ADD], [self.SEND], [])
        res = self.manager(window, ocr).add_friend("newbie")
        self.assertTrue(res["ok"])
        self.assertEqual(window.pasted, ["newbie"])
        self.assertNotIn("附言", res["message"])

    def test_a_dialog_that_stays_open_is_not_reported_as_sent(self):
        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], [self.ADD], [self.SEND], [self.SEND])
        res = self.manager(FakeWindow(), ocr).add_friend("newbie")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "not_sent")

    def test_losing_the_foreground_midway_stops_everything(self):
        window = FakeWindow()

        def lose_foreground(captures):
            window.foreground = False

        ocr = FakeOcr([self.SEARCH_BOX], [self.ENTRY], after_capture=lose_foreground)
        res = self.manager(window, ocr).add_friend("newbie", "你好")
        self.assertEqual(res["reason"], "aborted")
        self.assertEqual(self.clicks, [])
        self.assertEqual(window.pasted, [])
        self.assertEqual(window.restored, [("state", HWND)])

    def test_the_window_is_put_back_and_the_lock_released(self):
        window, lock = FakeWindow(), threading.Lock()
        self.manager(window, FakeOcr([self.SEARCH_BOX], [self.ENTRY], [row("该用户不存在", 0.4, 0.4, 0.5, 0.5)]), lock).add_friend("x")
        self.assertEqual(window.restored, [("state", HWND)])
        self.assertFalse(lock.locked())


class AutoFriendServiceTests(unittest.TestCase):
    def test_disabled_service_does_not_touch_the_window(self):
        manager = MagicMock()
        service = AutoFriendService(manager, enabled=False)
        self.assertEqual(service.run_once(), [])
        manager.accept_friend_requests.assert_not_called()

    def test_run_once_returns_what_was_accepted(self):
        manager = MagicMock()
        manager.accept_friend_requests.return_value = [{"status": "accepted"}]
        self.assertEqual(AutoFriendService(manager).run_once(), [{"status": "accepted"}])

    def test_a_failing_check_never_raises(self):
        manager = MagicMock()
        manager.accept_friend_requests.side_effect = RuntimeError("boom")
        self.assertEqual(AutoFriendService(manager).run_once(), [])

    def test_interval_has_a_floor(self):
        self.assertEqual(AutoFriendService(MagicMock(), interval_sec=1)._interval, 10)

    def test_enable_and_disable(self):
        service = AutoFriendService(MagicMock(), enabled=False)
        self.addCleanup(service.stop)
        self.assertFalse(service.enabled)
        service.enable()
        self.assertTrue(service.enabled)
        service.disable()
        self.assertFalse(service.enabled)

    def test_enable_starts_a_worker_that_was_never_started(self):
        service = AutoFriendService(MagicMock(), enabled=False)
        self.addCleanup(service.stop)
        self.assertIsNone(service._thread)
        service.enable()
        self.assertTrue(service._thread.is_alive())

    def test_start_is_idempotent_and_stop_ends_the_worker(self):
        service = AutoFriendService(MagicMock())
        service.start()
        thread = service._thread
        service.start()
        self.assertIs(service._thread, thread)
        service.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(service._thread)

    def test_the_first_check_waits_one_interval(self):
        manager = MagicMock()
        service = AutoFriendService(manager)
        service.start()
        self.addCleanup(service.stop)
        time.sleep(0.1)
        manager.accept_friend_requests.assert_not_called()

    def test_the_worker_checks_every_interval(self):
        ticked = threading.Event()
        manager = MagicMock()
        manager.accept_friend_requests.side_effect = lambda: ticked.set() or []
        service = AutoFriendService(manager)
        service._interval = 0.01
        service.start()
        self.addCleanup(service.stop)
        self.assertTrue(ticked.wait(WAIT))


class AdminFriendCommandTests(unittest.TestCase):
    def handler(self, friend=True, auto=True):
        self.friend = MagicMock()
        self.friend.add_friend.return_value = {"ok": True, "message": "已成功向「13800000000」发送好友申请"}
        self.friend.accept_friend_requests.return_value = [{"status": "accepted"}]
        self.auto = MagicMock()
        return AdminCommandHandler(
            friend_manager=self.friend if friend else None,
            auto_friend_service=self.auto if auto else None,
        )

    def test_add_friend(self):
        handler = self.handler()
        self.assertIn("已成功向「13800000000」发送好友申请", handler.handle("添加好友 13800000000 请通过"))
        self.friend.add_friend.assert_called_once_with("13800000000", "请通过")

    def test_add_friend_short_form_and_extra_spaces(self):
        handler = self.handler()
        handler.handle("加好友   13800000000")
        self.friend.add_friend.assert_called_once_with("13800000000", "")

    def test_add_friend_without_a_target_explains_the_usage(self):
        handler = self.handler()
        reply = handler.handle("添加好友")
        self.assertIn("请指定", reply)
        self.friend.add_friend.assert_not_called()

    def test_accept_friends(self):
        handler = self.handler()
        self.assertIn("已成功同意 1 位", handler.handle("同意好友"))

    def test_accepting_nothing_does_not_claim_there_were_no_requests(self):
        handler = self.handler()
        self.friend.accept_friend_requests.return_value = []
        reply = handler.handle("同意好友")
        self.assertIn("没有同意任何申请", reply)
        self.assertIn("日志", reply)

    def test_auto_accept_switch(self):
        handler = self.handler()
        self.assertIn("已关闭", handler.handle("自动同意好友 关"))
        self.auto.disable.assert_called_once()
        self.assertIn("已开启", handler.handle("自动同意好友  开"))      # two spaces
        self.auto.enable.assert_called_once()
        self.assertIn("已开启", handler.handle("自动同意好友开"))         # no spaces
        self.assertEqual(self.auto.enable.call_count, 2)
        self.assertIn("已关闭", handler.handle("自动同意好友关"))         # no spaces
        self.assertEqual(self.auto.disable.call_count, 2)

    def test_without_the_components_no_command_claims_success(self):
        handler = self.handler(friend=False, auto=False)
        for command in ("添加好友 13800000000", "同意好友", "自动同意好友 开", "关闭自动同意"):
            reply = handler.handle(command)
            self.assertIn("未启用好友管理组件", reply, command)
            self.assertNotIn("已开启", reply)
            self.assertNotIn("已关闭", reply)

    def test_other_commands_are_not_taken(self):
        self.assertIsNone(self.handler().handle("添加好友们的名字"))
        self.friend.add_friend.assert_not_called()

    def test_status_lists_the_bot_lines_and_the_friend_switches(self):
        handler = self.handler()
        self.auto.enabled = True
        handler.set_status(lambda: ["微信：已连接", "已处理消息：12 条"])
        reply = handler.handle("查状态")
        self.assertEqual(reply.splitlines(), ["账号状态：", "微信：已连接", "已处理消息：12 条",
                                              "好友管理：可用", "自动同意好友：开"])
        self.assertTrue(handler.is_command("账号状态"))

    def test_status_without_components_or_with_a_broken_source_still_answers(self):
        handler = self.handler(friend=False, auto=False)
        handler.set_status(lambda: 1 / 0)
        reply = handler.handle("查状态")
        self.assertIn("运行状态读取失败", reply)
        self.assertIn("好友管理：未启用", reply)
        self.assertNotIn("自动同意好友：", reply)

    def test_help_lists_only_account_commands(self):
        reply = self.handler().handle("帮助")
        for word in ("查状态", "添加好友", "同意好友", "自动同意好友"):
            self.assertIn(word, reply)
        for word in ("改名", "昵称", "抽签", "塔罗", "总结", "@bot"):
            self.assertNotIn(word, reply)
        self.assertFalse(self.handler().is_command("帮助"))      # the relay keeps its own help

    def test_set_friend_manager_attaches_both(self):
        handler = AdminCommandHandler()
        friend, auto = MagicMock(), MagicMock()
        friend.accept_friend_requests.return_value = []
        handler.set_friend_manager(friend, auto)
        handler.handle("同意好友")
        handler.handle("开启自动同意")
        friend.accept_friend_requests.assert_called_once()
        auto.enable.assert_called_once()


class FriendConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(BotConfig().auto_accept_friends, "off")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-key"}, clear=True):
            config = load_config()
        self.assertEqual(config.auto_accept_friends, "off")
        self.assertEqual(config.auto_accept_interval_sec, 300)

    def test_switched_on_from_the_environment(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test-key", "AUTO_ACCEPT_FRIENDS": "true", "AUTO_ACCEPT_INTERVAL_SEC": "60"}
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertEqual(config.auto_accept_friends, "on")
        self.assertEqual(config.auto_accept_interval_sec, 60)

    def test_valid_values(self):
        _validate_config({"auto_accept_friends": "on", "auto_accept_interval_sec": 300})

    def test_invalid_switch(self):
        with self.assertRaises(RuntimeError):
            _validate_config({"auto_accept_friends": "maybe"})

    def test_invalid_interval(self):
        for value in (5, 86401):
            with self.assertRaises(RuntimeError):
                _validate_config({"auto_accept_interval_sec": value})


class BotFriendWiringTests(unittest.TestCase):
    def start(self, backend, enabled="off"):
        config = BotConfig(auto_accept_friends=enabled, auto_accept_interval_sec=300)
        bot = Bot(config)
        admin = MagicMock()
        bot._start_friend_management(config, backend, admin)
        if bot._auto_friend is not None:
            self.addCleanup(bot._auto_friend.stop)
        return bot, admin

    def test_a_backend_without_a_window_gets_no_friend_management(self):
        bot, admin = self.start(SimpleNamespace())
        self.assertIsNone(bot._friend_mgr)
        self.assertIsNone(bot._auto_friend)
        admin.set_friend_manager.assert_not_called()

    def test_manager_gets_the_backends_window_lock_and_its_own_ocr_engine(self):
        window, lock = FakeWindow(), threading.Lock()
        backend = SimpleNamespace(window_controller=window, window_lock=lock, _ocr=MagicMock(spec=[]))
        with patch("src.wechat.ocr.win_ocr.WindowsOcrEngine") as engine_class:
            bot, admin = self.start(backend)
        engine_class.assert_called_once_with(window=window)
        self.assertIs(bot._friend_mgr._window, window)
        self.assertIs(bot._friend_mgr._ocr, engine_class.return_value)
        self.assertIs(bot._friend_mgr._ui_lock, lock)
        admin.set_friend_manager.assert_called_once_with(bot._friend_mgr, bot._auto_friend)

    def test_the_worker_runs_only_when_switched_on(self):
        backend = SimpleNamespace(window_controller=FakeWindow(), window_lock=threading.Lock())
        with patch("src.wechat.ocr.win_ocr.WindowsOcrEngine"):
            off, _ = self.start(backend, "off")
            on, _ = self.start(backend, "on")
        self.assertIsNone(off._auto_friend._thread)
        self.assertTrue(on._auto_friend._thread.is_alive())

    def test_a_failing_setup_does_not_stop_the_bot(self):
        backend = SimpleNamespace(window_controller=FakeWindow(), window_lock=threading.Lock())
        with patch("src.wechat.friend_manager.FriendManager", side_effect=RuntimeError("boom")):
            bot, admin = self.start(backend, "on")
        self.assertIsNone(bot._friend_mgr)
        admin.set_friend_manager.assert_not_called()


class WcdbBackendExposesItsWindowTests(unittest.TestCase):
    def test_window_controller_and_lock(self):
        backend = WcdbBackend(bot_display_name="小助手", groups=["测试群"])
        self.assertIs(backend.window_controller, backend._window)
        self.assertIs(backend.window_lock, backend._window_lock)


class WeChat4AddFriendTests(FriendTestCase):
    def test_wechat4_not_found(self):
        manager = self.manager(FakeWindow(), FakeOcr([row("无法找到该用户，请检查你填写的账号是否正确", 0.1, 0.5, 0.8, 0.6)]))
        with patch.object(manager, "_open_add_friend_dialog_wechat4", return_value=999), \
             patch.object(manager, "_focus_window"), \
             patch.object(manager, "_close_window"):
            res = manager._add_friend_wechat4(HWND, "nonexistent", "")
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "not_found")

    def test_wechat4_already_friend(self):
        manager = self.manager(FakeWindow(), FakeOcr([row("朋友资料", 0.1, 0.4, 0.3, 0.45), row("语音聊天", 0.4, 0.8, 0.6, 0.9)]))
        with patch.object(manager, "_open_add_friend_dialog_wechat4", return_value=999), \
             patch.object(manager, "_focus_window"), \
             patch.object(manager, "_close_window"):
            res = manager._add_friend_wechat4(HWND, "friend_user", "")
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "already_friend")

    def test_wechat4_send_success(self):
        add_btn = row("添加到通讯录", 0.4, 0.8, 0.6, 0.9)
        send_btn = row("发送", 0.4, 0.8, 0.6, 0.9)
        ocr = FakeOcr([add_btn], [send_btn], [])
        window = FakeWindow()
        manager = self.manager(window, ocr)
        with patch.object(manager, "_open_add_friend_dialog_wechat4", return_value=999), \
             patch.object(manager, "_focus_window"), \
             patch.object(manager, "_close_window"):
            res = manager._add_friend_wechat4(HWND, "new_friend", "你好呀")
            self.assertTrue(res["ok"])
            self.assertEqual(res["target"], "new_friend")
            self.assertIn("你好呀", res["message"])


class CloseWindowTests(FriendTestCase):
    """_close_window sends Alt+F4 only to a window that has the keyboard (Windows API faked)."""

    def close(self, foreground, visible=True):
        win32gui = MagicMock()
        win32gui.IsWindow.return_value = True
        win32gui.IsWindowVisible.return_value = visible
        win32gui.GetForegroundWindow.return_value = foreground
        win32con = SimpleNamespace(WM_CLOSE=0x10)
        manager = self.manager(FakeWindow(), FakeOcr([]))
        with patch.object(friend_manager, "sys", SimpleNamespace(platform="win32")), \
             patch.dict("sys.modules", {"win32gui": win32gui, "win32con": win32con}), \
             patch.object(manager, "_focus_window"):
            manager._close_window(999)
        return win32gui

    def test_alt_f4_when_the_window_has_the_keyboard(self):
        win32gui = self.close(foreground=999)
        self.assertEqual(self.keys, [("combo", 0x12, 0x73)])
        win32gui.PostMessage.assert_not_called()

    def test_by_message_when_another_window_has_the_keyboard(self):
        win32gui = self.close(foreground=555)
        self.assertEqual(self.keys, [])                  # no Alt+F4 into the other window
        win32gui.PostMessage.assert_called_once_with(999, 0x10, 0, 0)

    def test_a_window_that_is_already_gone_is_left_alone(self):
        win32gui = self.close(foreground=999, visible=False)
        self.assertEqual(self.keys, [])
        win32gui.PostMessage.assert_not_called()


class CaptureTargetTests(FriendTestCase):
    def test_a_dialog_is_captured_by_its_own_handle(self):
        ocr = FakeOcr([row("x", 0, 0, 1, 1)])
        manager = self.manager(FakeWindow(), ocr)
        manager._capture_ocr()
        manager._capture_ocr(777)
        self.assertEqual(ocr.hwnds, [None, 777])

    def test_an_error_inside_capture_is_not_retried_as_a_main_window_capture(self):
        ocr = MagicMock()
        ocr.capture.side_effect = TypeError("boom")
        self.assertEqual(self.manager(FakeWindow(), ocr)._capture_ocr(777), [])
        self.assertEqual(ocr.capture.call_count, 1)


class FakeDesktop:
    """A few top-level windows with the win32gui / win32process calls the 4.0 dialog lookup makes."""

    DIALOG_CLASS = "Qt51514QWindowIcon"
    MENU_CLASS = "Qt51514QWindowToolSaveBits"
    PID = 4242

    def __init__(self):
        self.windows = {HWND: {"cls": self.DIALOG_CLASS, "title": "微信", "rect": (0, 0, 1000, 800), "pid": self.PID}}
        self.on_click = []                       # one callback per click: what appears on the screen
        self.clicks = 0

    def add(self, hwnd, cls=DIALOG_CLASS, title="", rect=(100, 100, 600, 700), pid=PID):
        self.windows[hwnd] = {"cls": cls, "title": title, "rect": rect, "pid": pid}

    def click(self, x, y):
        if self.clicks < len(self.on_click):
            self.on_click[self.clicks]()
        self.clicks += 1
        return True

    def modules(self):
        gui, process = MagicMock(), MagicMock()
        gui.IsWindow.side_effect = lambda h: h in self.windows
        gui.IsWindowVisible.return_value = True
        gui.GetClassName.side_effect = lambda h: self.windows[h]["cls"]
        gui.GetWindowText.side_effect = lambda h: self.windows[h]["title"]
        gui.GetWindowRect.side_effect = lambda h: self.windows[h]["rect"]
        gui.EnumWindows.side_effect = lambda cb, extra: [cb(h, extra) for h in list(self.windows)] and None
        process.GetWindowThreadProcessId.side_effect = lambda h: (1, self.windows[h]["pid"])
        return {"win32gui": gui, "win32process": process}


class OpenAddFriendDialogTests(FriendTestCase):
    """The WeChat 4.0 add-friend window is found among the windows of the process without taking a chat for it."""

    CHAT, MENU, DIALOG = 501, 502, 503

    def open(self, desktop, window=None):
        manager = self.manager(window or FakeWindow(), FakeOcr([]))
        manager._clicker = desktop.click
        manager._rect_of = lambda h: (0, 0, 1000, 800)
        with patch.object(friend_manager, "sys", SimpleNamespace(platform="win32")), \
             patch.dict("sys.modules", desktop.modules()):
            return manager._open_add_friend_dialog_wechat4(HWND)

    def appears_after_clicks(self, desktop):
        desktop.on_click = [lambda: desktop.add(self.MENU, cls=FakeDesktop.MENU_CLASS, rect=(10, 60, 200, 300)),
                            lambda: desktop.add(self.DIALOG)]

    def test_opens_the_dialog_through_the_plus_menu_and_returns_the_new_window(self):
        desktop = FakeDesktop()
        self.appears_after_clicks(desktop)
        self.assertEqual(self.open(desktop), self.DIALOG)
        self.assertEqual(self.keys, [("combo", CTRL, 0x31)])             # Ctrl+1, then the two clicks
        self.assertEqual(desktop.clicks, 2)

    def test_a_chat_in_a_window_of_its_own_is_not_taken_for_the_dialog(self):
        desktop = FakeDesktop()
        desktop.add(self.CHAT, title="张三")                              # same class, same size range
        self.appears_after_clicks(desktop)
        self.assertEqual(self.open(desktop), self.DIALOG)
        self.assertEqual(desktop.clicks, 2)                              # it still opened the real dialog

    def test_without_a_new_window_nothing_is_returned_even_if_a_chat_window_fits(self):
        desktop = FakeDesktop()
        desktop.add(self.CHAT, title="张三")
        desktop.on_click = [lambda: desktop.add(self.MENU, cls=FakeDesktop.MENU_CLASS, rect=(10, 60, 200, 300))]
        self.assertIsNone(self.open(desktop))

    def test_the_dialog_is_found_by_its_title_whatever_size_the_process_sees(self):
        """A real machine: 746x1133 physical pixels, which a DPI-aware process sees; the size band would miss it."""
        desktop = FakeDesktop()
        desktop.on_click = [lambda: desktop.add(self.MENU, cls=FakeDesktop.MENU_CLASS, rect=(10, 60, 200, 300)),
                            lambda: desktop.add(self.DIALOG, title="添加朋友", rect=(1545, 470, 2291, 1603))]
        self.assertEqual(self.open(desktop), self.DIALOG)

    def test_without_the_title_a_window_of_the_wrong_size_is_not_the_dialog(self):
        desktop = FakeDesktop()
        desktop.on_click = [lambda: desktop.add(self.MENU, cls=FakeDesktop.MENU_CLASS, rect=(10, 60, 200, 300)),
                            lambda: desktop.add(self.DIALOG, rect=(1545, 470, 2291, 1603))]
        self.assertIsNone(self.open(desktop))

    def test_the_application_window_is_not_a_leftover_add_dialog(self):
        desktop = FakeDesktop()
        application = 504
        desktop.add(application, title="申请添加朋友", rect=(100, 100, 500, 800))      # a size that is a candidate
        self.appears_after_clicks(desktop)
        self.assertEqual(self.open(desktop), self.DIALOG)                # not the application window, which only
        self.assertEqual(desktop.clicks, 2)                              # contains the words: the real one was opened

    def test_a_dialog_left_over_from_an_earlier_run_is_reused_by_its_title(self):
        desktop = FakeDesktop()
        desktop.add(self.DIALOG, title="添加朋友")
        self.assertEqual(self.open(desktop), self.DIALOG)
        self.assertEqual(self.keys, [])
        self.assertEqual(desktop.clicks, 0)

    def test_nothing_is_typed_or_clicked_when_wechat_is_not_in_front(self):
        desktop = FakeDesktop()
        window = FakeWindow()
        window.foreground = False
        with self.assertRaises(friend_manager._Abort):
            self.open(desktop, window)
        self.assertEqual(self.keys, [])
        self.assertEqual(desktop.clicks, 0)

    def test_the_menu_is_not_clicked_when_it_never_appears(self):
        desktop = FakeDesktop()
        self.assertIsNone(self.open(desktop))
        self.assertEqual(desktop.clicks, 1)                              # only the (+)


def badge_picture(*positions):
    """A window picture with red round badges in its left rail."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (1000, 800), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    for x, y in positions:
        draw.ellipse((x, y, x + 16, y + 16), fill=(250, 81, 81))
    return image


class LookForNewsTests(FriendTestCase):
    """The silent look: a picture of the window, nothing brought forward, clicked or typed."""

    NEW_FRIENDS = row("新的朋友", 0.15, 0.12, 0.25, 0.16)

    def manager_seeing(self, picture, window=None, ocr=None):
        manager = self.manager(window or FakeWindow(), ocr)
        manager._grab_window = lambda hwnd: picture
        return manager

    def test_without_a_window_it_cannot_tell(self):
        self.assertEqual(self.manager(None, None).look_for_news(), "unknown")

    def test_it_pictures_the_window_without_waking_or_activating_it(self):
        window = FakeWindow()
        manager = self.manager_seeing(badge_picture(), window)
        manager.look_for_news()
        self.assertEqual(window.find_calls, [(False, False)])              # not forced, not woken from the tray
        self.assertEqual(window.activations, 0)
        self.assertNothingHappened()

    def test_it_takes_no_lock_so_it_never_waits_for_a_reply(self):
        lock = threading.Lock()
        manager = self.manager(FakeWindow(), None, lock)
        manager._grab_window = lambda hwnd: badge_picture()
        lock.acquire()
        try:
            self.assertEqual(manager.look_for_news(), "changed")
        finally:
            lock.release()

    def test_a_window_that_cannot_be_pictured_is_unknown(self):
        manager = self.manager(FakeWindow(), None)
        manager._grab_window = lambda hwnd: (_ for _ in ()).throw(RuntimeError("WeChat is minimized"))
        self.assertEqual(manager.look_for_news(), "unknown")

    def test_no_window_found_is_unknown(self):
        self.assertEqual(self.manager_seeing(badge_picture(), FakeWindow(hwnd=None)).look_for_news(), "unknown")

    def test_a_clean_check_settles_the_rail(self):
        manager = self.manager_seeing(badge_picture((40, 120)), ocr=FakeOcr([self.NEW_FRIENDS], []))
        self.assertEqual(manager.look_for_news(), "changed")               # nothing to compare with yet
        manager.accept_friend_requests()                                   # reads the list to its end: nothing there
        self.assertEqual(manager.look_for_news(), "unchanged")

    def test_a_check_that_did_not_finish_leaves_the_rail_unsettled(self):
        manager = self.manager_seeing(badge_picture((40, 120)), FakeWindow(contacts_open=False), FakeOcr([]))
        manager.accept_friend_requests()                                   # the Contacts tab did not open
        self.assertEqual(manager.look_for_news(), "changed")

    def test_a_stuck_request_leaves_the_rail_unsettled_so_the_next_look_asks_again(self):
        accept = row("接受", 0.70, 0.21, 0.76, 0.25)
        manager = self.manager_seeing(badge_picture((40, 120)), ocr=FakeOcr([self.NEW_FRIENDS], [accept], [], [accept]))
        manager.accept_friend_requests()
        self.assertEqual(manager.look_for_news(), "changed")

    def test_a_new_badge_after_a_clean_check_is_news(self):
        pictures = [badge_picture((40, 120))]
        manager = self.manager(FakeWindow(), FakeOcr([self.NEW_FRIENDS], []))
        manager._grab_window = lambda hwnd: pictures[-1]
        manager.accept_friend_requests()
        pictures.append(badge_picture((40, 120), (40, 190)))              # a request arrives
        self.assertEqual(manager.look_for_news(), "changed")


class FocusReturnTests(FriendTestCase):
    NEW_FRIENDS = row("新的朋友", 0.15, 0.12, 0.25, 0.16)

    def test_the_focus_goes_back_to_the_window_the_user_was_in(self):
        window = FakeWindow()
        self.manager(window, FakeOcr([self.NEW_FRIENDS], [])).accept_friend_requests()
        self.assertEqual(window.focus_returned, [777])

    def test_also_when_the_window_could_not_be_brought_forward(self):
        window = FakeWindow(activates=False)
        self.manager(window, FakeOcr([])).accept_friend_requests()
        self.assertEqual(window.focus_returned, [777])

    def test_also_after_an_add_friend(self):
        window = FakeWindow()
        ocr = FakeOcr([row("搜索", 0.06, 0.04, 0.14, 0.08)])
        self.manager(window, ocr).add_friend("newbie")
        self.assertEqual(window.focus_returned, [777])

    def test_nothing_to_go_back_to(self):
        window = FakeWindow()
        window.previous = 0
        self.manager(window, FakeOcr([self.NEW_FRIENDS], [])).accept_friend_requests()
        self.assertEqual(window.focus_returned, [])

    def test_a_focus_that_cannot_be_returned_does_not_fail_the_check(self):
        window = FakeWindow()
        window._return_focus = lambda previous: (_ for _ in ()).throw(OSError("gone"))
        result = self.manager(window, FakeOcr([self.NEW_FRIENDS], [])).accept_friend_requests()
        self.assertEqual(result, [])


class FakeGate:
    def __init__(self, idle=1000.0):
        self.idle = idle
        self.own_input_marked = 0
        self.fail = False

    def idle_seconds(self):
        if self.fail:
            raise OSError("no GetLastInputInfo")
        return self.idle

    def mark_own_input(self):
        self.own_input_marked += 1


class QuietServiceTests(unittest.TestCase):
    """AutoFriendService: look silently, wait for the user to pause, force a check now and then."""

    def setUp(self):
        self.now = 0.0
        self.manager = MagicMock()
        self.manager.accept_friend_requests.return_value = []
        self.manager.look_for_news.return_value = "changed"
        self.gate = FakeGate()

    def service(self, **kw):
        kw.setdefault("idle_gate", self.gate)
        kw.setdefault("idle_sec", 60)
        kw.setdefault("full_check_sec", 900)
        return AutoFriendService(self.manager, interval_sec=300, clock=lambda: self.now, **kw)

    def test_when_nothing_changed_nothing_is_done_and_the_user_is_not_even_asked(self):
        self.manager.look_for_news.return_value = "unchanged"
        gate_calls = []
        self.gate.idle_seconds = lambda: gate_calls.append(1) or 0.0           # busy, but it must not matter
        self.assertEqual(self.service().run_once(), [])
        self.manager.accept_friend_requests.assert_not_called()
        self.assertEqual(gate_calls, [])

    def test_news_and_an_idle_user_means_a_check(self):
        self.manager.accept_friend_requests.return_value = [{"status": "accepted"}]
        self.assertEqual(self.service().run_once(), [{"status": "accepted"}])

    def test_news_while_the_user_is_busy_waits(self):
        service = self.service()
        self.gate.idle = 5.0
        self.assertEqual(service.run_once(), [])
        self.assertTrue(service.postponed)
        self.manager.accept_friend_requests.assert_not_called()
        self.gate.idle = 61.0                                                   # the user paused
        service.run_once()
        self.assertFalse(service.postponed)
        self.manager.accept_friend_requests.assert_called_once()

    def test_a_window_that_cannot_be_pictured_still_gets_a_check_when_the_user_is_idle(self):
        self.manager.look_for_news.return_value = "unknown"
        self.service().run_once()
        self.manager.accept_friend_requests.assert_called_once()

    def test_a_window_that_cannot_be_pictured_waits_for_an_idle_user_too(self):
        self.manager.look_for_news.return_value = "unknown"
        self.gate.idle = 3.0
        service = self.service()
        service.run_once()
        self.assertTrue(service.postponed)
        self.manager.accept_friend_requests.assert_not_called()

    def test_with_the_watch_off_every_tick_is_a_check_but_still_waits_for_the_user(self):
        service = self.service(watch=False)
        self.gate.idle = 2.0
        service.run_once()
        self.manager.look_for_news.assert_not_called()
        self.manager.accept_friend_requests.assert_not_called()
        self.gate.idle = 100.0
        service.run_once()
        self.manager.accept_friend_requests.assert_called_once()

    def test_a_check_is_forced_now_and_then_even_when_nothing_looks_new(self):
        self.manager.look_for_news.return_value = "unchanged"
        service = self.service()
        self.now = 899.0
        service.run_once()
        self.manager.accept_friend_requests.assert_not_called()
        self.now = 901.0                                                        # a badge that is not recognised must not hide a request for good
        service.run_once()
        self.manager.accept_friend_requests.assert_called_once()
        self.now = 1000.0                                                       # and the clock starts again
        service.run_once()
        self.manager.accept_friend_requests.assert_called_once()

    def test_the_forced_check_can_be_switched_off(self):
        self.manager.look_for_news.return_value = "unchanged"
        service = self.service(full_check_sec=0)
        self.now = 10 ** 6
        service.run_once()
        self.manager.accept_friend_requests.assert_not_called()

    def test_without_an_idle_time_there_is_no_waiting(self):
        self.gate.idle = 0.0
        self.service(idle_sec=0).run_once()
        self.manager.accept_friend_requests.assert_called_once()
        self.manager.accept_friend_requests.reset_mock()
        self.service(idle_gate=None).run_once()
        self.manager.accept_friend_requests.assert_called_once()

    def test_idle_time_that_cannot_be_read_does_not_block_the_check(self):
        self.gate.fail = True
        self.service().run_once()
        self.manager.accept_friend_requests.assert_called_once()

    def test_the_checks_own_input_is_marked_so_it_does_not_look_like_the_user(self):
        self.service().run_once()
        self.assertEqual(self.gate.own_input_marked, 1)

    def test_a_tick_that_did_nothing_marks_nothing(self):
        self.manager.look_for_news.return_value = "unchanged"
        self.service().run_once()
        self.assertEqual(self.gate.own_input_marked, 0)

    def test_a_failing_look_never_raises(self):
        self.manager.look_for_news.side_effect = RuntimeError("boom")
        self.assertEqual(self.service().run_once(), [])

    def test_the_worker_asks_again_soon_while_the_user_is_busy_and_after_an_interval_otherwise(self):
        waits = []

        class Event:
            def __init__(self, service):
                self.service = service

            def wait(self, seconds):
                waits.append(seconds)
                if len(waits) == 3:
                    self.service.gate.idle = 100.0                                # the user pauses
                return len(waits) > 4

            def set(self):
                pass

        service = self.service()
        service.gate = self.gate
        self.gate.idle = 1.0
        service._stop_event = Event(service)
        service._run()
        self.assertEqual(waits, [300, friend_manager.POSTPONE_RETRY_SEC, friend_manager.POSTPONE_RETRY_SEC, 300, 300])


class QuietConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = BotConfig()
        self.assertEqual((config.auto_accept_idle_sec, config.auto_accept_watch, config.auto_accept_full_check_sec),
                         (60, "on", 900))

    def test_from_the_environment(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test-key", "AUTO_ACCEPT_IDLE_SEC": "30",
               "AUTO_ACCEPT_WATCH": "no", "AUTO_ACCEPT_FULL_CHECK_SEC": "0"}
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertEqual((config.auto_accept_idle_sec, config.auto_accept_watch, config.auto_accept_full_check_sec),
                         (30, "off", 0))

    def test_valid_values(self):
        _validate_config({"auto_accept_idle_sec": 0, "auto_accept_watch": "off", "auto_accept_full_check_sec": 0})
        _validate_config({"auto_accept_idle_sec": 3600, "auto_accept_full_check_sec": 60})

    def test_invalid_values(self):
        for bad in ({"auto_accept_idle_sec": -1}, {"auto_accept_idle_sec": 3601}, {"auto_accept_watch": "maybe"},
                    {"auto_accept_full_check_sec": 30}, {"auto_accept_full_check_sec": 86401}):
            with self.assertRaises(RuntimeError, msg=bad):
                _validate_config(bad)

    def test_the_bot_hands_them_to_the_service(self):
        config = BotConfig(auto_accept_friends="off", auto_accept_idle_sec=45, auto_accept_watch="off",
                           auto_accept_full_check_sec=120)
        bot = Bot(config)
        backend = SimpleNamespace(window_controller=FakeWindow(), window_lock=threading.Lock())
        with patch("src.wechat.ocr.win_ocr.WindowsOcrEngine"):
            bot._start_friend_management(config, backend, MagicMock())
        service = bot._auto_friend
        self.assertEqual((service._idle_sec, service._watch, service._full_check_sec), (45.0, False, 120.0))
        self.assertTrue(hasattr(service._idle_gate, "idle_seconds"))


if __name__ == "__main__":
    unittest.main()
