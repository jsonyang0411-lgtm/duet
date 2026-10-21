"""Locking rules of WcdbBackend: window automation must not block DB polling.

Sending a reply drives the WeChat window for seconds.  The WCDB DLL lock
(``_client_lock``) must stay free during that time, while window operations
(sends, vision screenshots) still exclude each other (``_window_lock``).
"""

import threading
import time
import unittest

from src.wechat.wcdb_backend import WcdbBackend

WAIT = 5  # seconds; only ever spent when a test is failing


class _Client:
    _config = {"myWxid": "wxid_me"}

    def __init__(self):
        self.get_messages_calls = 0

    def get_messages(self, talker, limit=50):
        self.get_messages_calls += 1
        return []

    def resolve_nickname(self, wxid):
        return wxid


def _message(message_id="m1"):
    return {
        "message_id": message_id,
        "chat_id": "room1@chatroom",
        "group_name": "测试群",
        "sender_id": "wxid_user",
        "sender_name": "阿明",
        "content": "你好",
        "msg_type": 1,
        "timestamp": int(time.time()),
    }


class WcdbLockingTests(unittest.TestCase):
    def setUp(self):
        self.backend = WcdbBackend(bot_display_name="小助手", groups=["测试群"])
        self.backend._client = _Client()
        self.backend._running = True
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()
        self.threads = []

    def tearDown(self):
        self.release.set()
        for thread in self.threads:
            thread.join(WAIT)
            self.assertFalse(thread.is_alive())

    def blocking_send(self, group_name, talker, content):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.set()
        self.release.wait(WAIT)
        with self._guard:
            self.active -= 1
        return True

    def start(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def test_polling_is_not_blocked_while_a_reply_is_being_sent(self):
        self.backend._send_and_confirm = self.blocking_send
        self.start(
            self.backend._handle_message,
            "测试群", "room1@chatroom", _message(), lambda msg: "好的",
        )
        self.assertTrue(self.entered.wait(WAIT), "the send never started")

        poller = self.start(
            self.backend._poll_group, "测试群", "room1@chatroom", lambda msg: None,
        )
        poller.join(WAIT)

        self.assertFalse(poller.is_alive(), "polling waited for the send to finish")
        self.assertEqual(self.backend._client.get_messages_calls, 1)
        self.assertFalse(self.release.is_set())       # the send is still running

    def test_two_replies_are_never_typed_at_the_same_time(self):
        self.backend._send_and_confirm = self.blocking_send
        for index in range(2):
            self.start(
                self.backend._handle_message,
                "测试群", "room1@chatroom", _message(f"m{index}"), lambda msg: "好的",
            )
        self.assertTrue(self.entered.wait(WAIT))
        time.sleep(0.05)                              # give a second sender time to (wrongly) enter
        self.assertEqual(self.max_active, 1)

        self.release.set()
        for thread in self.threads:
            thread.join(WAIT)
        self.assertEqual(self.max_active, 1)

    def test_send_text_and_reply_sends_share_the_window_lock(self):
        self.backend._send_and_confirm = self.blocking_send
        self.backend._talker_ids = {"测试群": "room1@chatroom"}
        self.start(
            self.backend._handle_message,
            "测试群", "room1@chatroom", _message(), lambda msg: "好的",
        )
        self.assertTrue(self.entered.wait(WAIT))

        self.start(self.backend.send_text, "room1@chatroom", "手动发送")
        time.sleep(0.05)

        self.assertEqual(self.max_active, 1)

    def test_vision_screenshot_waits_for_a_send_in_progress(self):
        self.backend._send_and_confirm = self.blocking_send
        captured = threading.Event()

        class Window:
            def capture_chat_media(self, group_name):
                captured.set()
                return "shot.png"

        self.backend._window = Window()
        self.start(
            self.backend._handle_message,
            "测试群", "room1@chatroom", _message(), lambda msg: "好的",
        )
        self.assertTrue(self.entered.wait(WAIT))

        self.start(self.backend._capture_media_screenshot, "测试群")
        time.sleep(0.05)
        self.assertFalse(captured.is_set())           # still waiting for the window

        self.release.set()
        self.assertTrue(captured.wait(WAIT))


if __name__ == "__main__":
    unittest.main()
