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
        self.backend._chat_names = {"room1@chatroom": "测试群"}
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


# ── Every call into wcdb_api.dll holds one lock ────────────────────────────────
#
# The DLL is not known to be thread-safe, and the poll loop is no longer its only caller: the
# new-friend scan (src/wechat/new_friends.py) reads the database from a thread of its own.  The real
# client (WcdbNativeClient) runs here on a fake DLL whose every function notes whether the calling
# thread holds the client's DLL lock and how many threads are inside the DLL at once.

import ctypes as ct          # noqa: E402
import json                  # noqa: E402
import os                    # noqa: E402
import tempfile              # noqa: E402
from pathlib import Path     # noqa: E402
from unittest.mock import patch  # noqa: E402

from src.config import BotConfig  # noqa: E402
from src.wechat import wcdb_client  # noqa: E402
from src.wechat.wcdb_client import WcdbNativeClient  # noqa: E402

DLL_GROUP = "room1@chatroom"
DLL_FRIEND = "wxid_friend"


def _dll_lock_held() -> bool:
    lock = getattr(wcdb_client, "_DLL_LOCK", None)
    return bool(lock is not None and lock._is_owned())


class _CFunc:
    """A ctypes function stand-in: callable, and argtypes / restype can be set on it."""

    def __init__(self, fn):
        self._fn = fn
        self.__name__ = fn.__name__

    def __call__(self, *args):
        return self._fn(*args)


class FakeDll:
    """wcdb_api.dll as WcdbNativeClient calls it (ctypes: the result through the last argument)."""

    wcdb_get_contacts_compact = None
    FUNCTIONS = ("wcdb_get_sessions", "wcdb_get_messages", "wcdb_get_display_names", "wcdb_get_group_members",
                 "wcdb_get_voice_data", "wcdb_free_string", "wcdb_close_account")

    def __init__(self, pause=0.002):
        for name in self.FUNCTIONS:
            setattr(self, name, _CFunc(getattr(self, name)))
        self.pause = pause
        self.calls = []                  # (function, did the caller hold the DLL lock)
        self.inside = 0
        self.most_inside = 0
        self._count = threading.Lock()
        self._kept = []                  # the strings handed out stay alive
        self._n = 0
        self.block = None                # an Event wcdb_get_messages waits for
        self.entered = threading.Event()

    def _enter(self, name):
        with self._count:
            self.calls.append((name, _dll_lock_held()))
            self.inside += 1
            self.most_inside = max(self.most_inside, self.inside)
        time.sleep(self.pause)           # a call takes a while: another thread could come in meanwhile

    def _leave(self):
        with self._count:
            self.inside -= 1

    def _out(self, out, raw: bytes) -> int:
        buf = ct.create_string_buffer(raw)
        self._kept.append(buf)
        out._obj.value = ct.addressof(buf)
        return 0

    def _json(self, name, out, value) -> int:
        self._enter(name)
        try:
            return self._out(out, json.dumps(value, ensure_ascii=False).encode("utf-8"))
        finally:
            self._leave()

    def wcdb_get_sessions(self, handle, out):
        return self._json("wcdb_get_sessions", out, [{"username": DLL_GROUP, "displayName": "测试群"},
                                                     {"username": DLL_FRIEND, "displayName": "私聊好友"}])

    def wcdb_get_messages(self, handle, talker, limit, offset, out):
        if self.block is not None:
            self.entered.set()
            self._enter("wcdb_get_messages")
            try:
                self.block.wait(WAIT)
                return self._out(out, b"[]")
            finally:
                self._leave()
        with self._count:
            self._n += 1
            n = self._n
        now = int(time.time())
        rows = [                          # newest first; new rows on every read
            {"sender_username": f"wxid_new{n}", "message_content": f"第{n}句", "local_type": 1,
             "create_time": now, "local_id": 2 * n, "server_id": f"s{2 * n}"},
            {"sender_username": f"wxid_new{n}", "message_content": "", "local_type": 34,
             "create_time": now, "local_id": 2 * n - 1, "server_id": f"s{2 * n - 1}"},
        ]
        return self._json("wcdb_get_messages", out, rows)

    def wcdb_get_display_names(self, handle, usernames, out):
        return self._json("wcdb_get_display_names", out,
                          {u: f"名字{u[-3:]}" for u in json.loads(usernames.decode("utf-8"))})

    def wcdb_get_group_members(self, handle, chat, out):
        return self._json("wcdb_get_group_members", out, [{"username": "wxid_member1"}, {"username": "wxid_member2"}])

    def wcdb_get_voice_data(self, handle, talker, local_id, create_time, svr_id, extra, out):
        self._enter("wcdb_get_voice_data")
        try:
            return self._out(out, b"0102")
        finally:
            self._leave()

    def wcdb_free_string(self, ptr):
        self._enter("wcdb_free_string")
        self._leave()

    def wcdb_close_account(self, handle):
        self._enter("wcdb_close_account")
        self._leave()
        return 0


class FakeVoice:
    """The voice pipeline: recognises whatever bytes WCDB gave."""
    _cache: dict = {}

    def process(self, msg, voice_bytes=None):
        return "语音的字" if voice_bytes else None


class DllCallsAreSerializedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "wxid_me_1234" / "db_storage" / "session").mkdir(parents=True)
        (self.tmp / "wxid_me_1234" / "db_storage" / "session" / "session.db").write_bytes(b"")
        env = patch.dict(os.environ, {"WECHAT_DATA_DIR": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        cwd = os.getcwd()
        os.chdir(self.tmp)                # data/group_names.json and friends land here
        self.addCleanup(os.chdir, cwd)
        self.dll = FakeDll()

    def client(self):
        client = WcdbNativeClient(dll_dir=str(self.tmp))
        client._dll = self.dll
        client._handle = 7
        return client

    def assertEveryCallHeldTheLock(self):
        self.assertTrue(self.dll.calls)
        unlocked = sorted({name for name, held in self.dll.calls if not held})
        self.assertEqual(unlocked, [], "called without the DLL lock")

    def test_each_client_call_holds_the_dll_lock(self):
        client = self.client()
        client.get_sessions()
        client.get_messages(DLL_FRIEND, limit=5)
        client.get_display_names(["wxid_a"])
        client.get_group_members(DLL_GROUP)
        client.get_voice_data(DLL_FRIEND, 1, int(time.time()), "s1")
        client.resolve_nickname("wxid_unknown")              # not cached: asks the DLL
        client.close()
        self.assertEqual({name for name, _ in self.dll.calls}, {
            "wcdb_get_sessions", "wcdb_get_messages", "wcdb_get_display_names", "wcdb_get_group_members",
            "wcdb_get_voice_data", "wcdb_free_string", "wcdb_close_account"})
        self.assertEveryCallHeldTheLock()

    def test_the_poll_thread_and_the_new_friend_scan_never_are_in_the_dll_at_once(self):
        backend = WcdbBackend(bot_display_name="小助手", groups=["*"],
                              config=BotConfig(private_chat_enabled=True, message_burst_enabled=False))
        backend._client = self.client()
        backend._voice = FakeVoice()
        backend._running = True
        stop = threading.Event()

        def scan():                       # what NewFriendService does, through with_client
            while not stop.is_set():
                backend.with_client(lambda client: client.get_sessions())
                backend.with_client(lambda client: client.get_messages(DLL_FRIEND, limit=20))
                backend.with_client(lambda client: client.get_display_names(["wxid_x"]))

        scanner = self.start_thread(scan)
        try:
            for _ in range(3):
                backend._resolve_groups()                    # sessions, group members, their names
                for chat, name in list(backend._chat_names.items()):
                    # a voice message (its audio from WCDB) and a sender not in the name cache
                    backend._poll_group(name, chat, lambda msg: None)
        finally:
            stop.set()
            scanner.join(WAIT)
        names = {name for name, _ in self.dll.calls}
        self.assertTrue({"wcdb_get_voice_data", "wcdb_get_group_members", "wcdb_get_display_names"} <= names)
        self.assertEveryCallHeldTheLock()
        self.assertEqual(self.dll.most_inside, 1)

    def test_closing_waits_for_a_call_in_progress_and_a_closed_client_never_reaches_the_dll(self):
        client = self.client()
        self.dll.block = threading.Event()
        reader = self.start_thread(lambda: client.get_messages(DLL_FRIEND, limit=5))
        self.assertTrue(self.dll.entered.wait(WAIT))
        closer = self.start_thread(client.close)
        closer.join(0.2)
        self.assertNotIn("wcdb_close_account", [name for name, _ in self.dll.calls])   # the read is not done
        self.dll.block.set()
        reader.join(WAIT)
        closer.join(WAIT)
        self.assertEqual([name for name, _ in self.dll.calls][-1], "wcdb_close_account")
        before = len(self.dll.calls)
        self.assertEqual(client.get_messages(DLL_FRIEND, limit=5), [])
        self.assertEqual(client.get_sessions(), [])
        self.assertEqual(len(self.dll.calls), before)       # a closed handle is never passed to the DLL

    def start_thread(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread
