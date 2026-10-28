"""A WCDB read the DLL answers with an error code is a failed read, not an empty chat (win review F14).

After WeChat logs in again, updates or rebuilds its database, the old handle answers every call with
an error code.  The client used to turn that into "no messages" / "no sessions", so the poll loop saw
nothing new, never counted a failure, never re-opened the database, never turned the OCR fallback on
and kept saying the database could be read, until someone restarted the bot.  Now the reads raise
``WcdbCallError``; a chat whose read fails while the other chats read fine is only that chat (it is
skipped and looked at again); a poll in which every read fails is a failed read of the database.  A
chat that really has nothing new (ret 0, an empty list or no string at all) is not a failure.

The real client (WcdbNativeClient) runs on the fake DLL of tests/wcdb_dll_fakes.py.
"""

import logging
import threading
import time

import pytest

from src.config import BotConfig
from src.wechat import wcdb_backend
from src.wechat.wcdb_backend import WcdbBackend
from src.wechat.wcdb_client import WcdbCallError, WcdbNativeClient
from wcdb_dll_fakes import FakeDll, data_dir, make_client

GROUP = "room1@chatroom"
FRIEND = "wxid_friend"
OTHER = "wxid_other"
WAIT = 20
STALE = -2                                   # what the old handle answers with


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_DATA_DIR", str(data_dir(tmp_path)))
    monkeypatch.chdir(tmp_path)              # data/group_names.json and friends go here
    return tmp_path


def wechat(now=None):
    dll = FakeDll()
    dll.sessions = [{"username": GROUP, "displayName": "测试群"}, {"username": FRIEND, "displayName": "小美"},
                    {"username": OTHER, "displayName": "阿明"}]
    dll.say(GROUP, "wxid_member1", "群里早先的话", now)
    dll.say(FRIEND, FRIEND, "早先的话", now)
    dll.say(OTHER, OTHER, "阿明早先的话", now)
    return dll


# ── the client ───────────────────────────────────────────────────────────────

def test_an_error_code_is_a_failed_read_not_an_empty_list(tmp):
    dll = wechat()
    client = make_client(dll, tmp)
    assert [m["message_content"] for m in client.get_messages(FRIEND, limit=5)] == ["早先的话"]
    dll.codes["wcdb_get_messages"] = STALE
    with pytest.raises(WcdbCallError) as raised:
        client.get_messages(FRIEND, limit=5)
    assert raised.value.ret == STALE and "wcdb_get_messages" in str(raised.value)
    dll.codes = {"wcdb_get_sessions": STALE}
    with pytest.raises(WcdbCallError):
        client.get_sessions()


def test_nothing_there_is_no_failure(tmp):
    dll = wechat()
    client = make_client(dll, tmp)
    assert client.get_messages("wxid_nobody", limit=5) == []          # ret 0, an empty list
    dll.null_chats.add("wxid_nobody")
    assert client.get_messages("wxid_nobody", limit=5) == []          # ret 0, no string at all
    dll.sessions = []
    assert client.get_sessions() == []


def test_a_client_that_is_not_open_fails_its_reads(tmp):
    client = make_client(wechat(), tmp, opened=False)
    with pytest.raises(WcdbCallError) as raised:
        client.get_messages(FRIEND, limit=5)
    assert raised.value.ret is None and "not open" in str(raised.value)


def test_open_takes_a_key_whose_session_list_fails_for_a_wrong_key(tmp, monkeypatch):
    """open() checks a key by reading the session list: an error code there means "not this key"
    (next key, then KEY_MISSING), never a WcdbCallError out of open()."""
    monkeypatch.setenv("WCDB_KEY", "a" * 64)
    monkeypatch.setattr(WcdbNativeClient, "_save_key_to_env", staticmethod(lambda key: None))
    dll = wechat()
    dll.codes["wcdb_get_sessions"] = STALE
    client = make_client(dll, tmp, opened=False)
    with pytest.raises(RuntimeError, match="KEY_MISSING"):
        client.open()
    assert not client.is_open
    dll.codes = {}
    assert client.open() is True and client.is_open


# ── the backend ──────────────────────────────────────────────────────────────

def backend_on(dll, tmp, **config):
    backend = WcdbBackend(bot_display_name="小助手", groups=["*"],
                          config=BotConfig(private_chat_enabled=True, message_burst_enabled=False, **config))
    backend._client = make_client(dll, tmp)
    backend._running = True
    backend._poll_sec = 0
    backend._resolve_groups()
    backend._prime_known_ids()
    return backend


def test_a_chat_whose_read_fails_is_skipped_while_the_others_read(tmp, caplog):
    dll = wechat()
    backend = backend_on(dll, tmp)
    got = []
    dll.chat_codes[FRIEND] = -3                                        # this chat only (no rows for it …)
    dll.say(OTHER, OTHER, "阿明的新消息")
    dll.say(FRIEND, FRIEND, "小美的新消息")
    with caplog.at_level(logging.WARNING):
        backend._poll_cycle(got.append)                                # no failure: the database reads
        backend._poll_cycle(got.append)
    assert [m["content"] for m in got] == ["阿明的新消息"]
    assert len([r for r in caplog.records if "could not read" in r.getMessage()]) == 1   # said once
    del dll.chat_codes[FRIEND]
    backend._poll_cycle(got.append)
    assert [m["content"] for m in got] == ["阿明的新消息", "小美的新消息"]   # read again, nothing lost


def test_a_poll_in_which_every_read_fails_is_a_failed_read_of_the_database(tmp):
    dll = wechat()
    backend = backend_on(dll, tmp)
    dll.codes["wcdb_get_messages"] = STALE
    with pytest.raises(WcdbCallError):
        backend._poll_cycle(lambda msg: None)
    dll.codes = {}
    dll.say(FRIEND, FRIEND, "回来以后的话")
    got = []
    backend._poll_cycle(got.append)                                    # the same chats, nothing replayed
    assert [m["content"] for m in got] == ["回来以后的话"]


def test_quiet_chats_are_no_failure(tmp):
    dll = wechat()
    backend = backend_on(dll, tmp)
    for _ in range(3):
        backend._poll_cycle(lambda msg: None)                          # nothing new anywhere: fine


def test_a_stale_handle_is_counted_reopened_and_read_from_the_screen_meanwhile(tmp, monkeypatch):
    """The whole loop: WeChat logged in again, the old handle answers every read with an error code.
    The failures are counted (db_readable goes False after 3), the OCR fallback turns on, the 5th
    re-opens the database, and the new handle's messages are handed on."""
    old, new = wechat(), wechat()
    made = []

    def open_client():
        dll = old if not made else new
        client = make_client(dll, tmp, opened=False)
        client.init = lambda: None
        client.open = lambda: setattr(client, "_handle", 7) or True
        made.append(client)
        return client

    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", open_client)
    monkeypatch.setattr(wcdb_backend, "_backoff_sec", lambda n: 0.01)
    back = threading.Event()

    class Ocr:
        """The OCR fallback as the loop tells it how the database reads go."""
        active = False
        failures = 0

        def note_db_failure(self):
            self.failures += 1
            self.active = self.active or self.failures >= 3
            return self.active

        def note_db_ok(self):
            was, self.active, self.failures = self.active, False, 0
            if was:
                back.set()                                             # the database reads again
            return was

        def poll(self):
            return []

    ocr = Ocr()
    backend = WcdbBackend(bot_display_name="小助手", groups=["*"], poll_sec=0.01, ocr=ocr,
                          config=BotConfig(private_chat_enabled=True, message_burst_enabled=False))
    backend._window.find_hwnd = lambda *a, **k: None
    backend._window.describe_chat_windows = lambda *a, **k: None
    got, seen_unreadable, ocr_on = [], [], []
    delivered = threading.Event()

    def callback(msg):
        got.append(msg["content"])
        if msg["content"] == "重新登录以后的话":
            delivered.set()

    real_messages = old.wcdb_get_messages

    def stale_messages(*args):
        if old.codes:
            seen_unreadable.append(not backend.db_readable())
            ocr_on.append(ocr.active)
        return real_messages(*args)

    old.wcdb_get_messages = stale_messages
    thread = threading.Thread(target=backend.start, args=(callback,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + WAIT
        while not backend._running and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)                                               # a few good polls
        old.codes["wcdb_get_messages"] = old.codes["wcdb_get_sessions"] = STALE
        assert back.wait(WAIT), "the database was never read again"
        new.say(FRIEND, FRIEND, "重新登录以后的话")
        assert delivered.wait(WAIT), "a message after the re-open was not handed on"
    finally:
        backend.stop()
        thread.join(WAIT)
    assert len(made) == 2                                              # re-opened once
    assert True in seen_unreadable and True in ocr_on                  # counted as unreadable, OCR on meanwhile
    assert backend.db_readable() and not ocr.active                   # and back to normal
    assert got == ["重新登录以后的话"]                                   # nothing from before replayed


# ── the new-friend scan beside the poll loop ─────────────────────────────────

def test_a_chat_the_new_friend_scan_cannot_read_does_not_stop_the_look(tmp):
    """One private chat answers with an error code: the other chats' new friends are still found, and
    the chat is looked at again at the next look (the scan's mark does not move past it)."""
    from src.wechat.new_friends import NewFriendService, NewFriendStore

    dll = wechat()
    backend = backend_on(dll, tmp, prm_copilot="on")
    now = time.time()
    dll.sessions = [{"username": FRIEND, "sort_timestamp": now - 100}, {"username": OTHER, "sort_timestamp": now - 100}]
    backend._sessions_read = None                                      # the scan reads the sessions itself
    service = NewFriendService(backend, NewFriendStore(str(tmp / "nf.db")), clock=lambda: now)
    service.scan_once()                                                # the first look: the start marker
    for chat, at in ((FRIEND, now + 1), (OTHER, now + 2)):
        dll.say(chat, "", "你已添加了某人，现在可以开始聊天了。", at, local_type=10000)
    dll.sessions = [{"username": FRIEND, "sort_timestamp": now + 1}, {"username": OTHER, "sort_timestamp": now + 2}]
    dll.chat_codes[FRIEND] = -3
    assert [e["wxid"] for e in service.scan_once()] == [OTHER]
    del dll.chat_codes[FRIEND]
    assert [e["wxid"] for e in service.scan_once()] == [FRIEND]       # looked at again, not lost
