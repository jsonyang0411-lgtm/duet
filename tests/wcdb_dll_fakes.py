"""wcdb_api.dll for the real WcdbNativeClient in tests (no Windows, no WeChat).

``FakeDll`` answers the DLL functions the client calls, the way ctypes hands them over (the JSON
result through the last argument).  Every call notes whether the calling thread holds the client's
DLL lock (``wcdb_client._DLL_LOCK``) and how many threads are inside the DLL at once.  ``codes``
makes a function answer with an error code (a stale handle answers every read that way after WeChat
logged in again); ``chat_codes`` does it for one chat's messages only.  ``make_client`` builds a
WcdbNativeClient on it, with a data folder of its own.
"""

import ctypes as ct
import json
import threading
import time
from pathlib import Path

from src.wechat import wcdb_client
from src.wechat.wcdb_client import WcdbNativeClient

ME = "wxid_me_1234"


def dll_lock_held() -> bool:
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
    """The functions WcdbNativeClient calls.  ``sessions``: the session list; ``chats``: each chat's
    rows, newest first (``say`` adds one)."""

    wcdb_get_contacts_compact = None
    FUNCTIONS = ("wcdb_open_account", "wcdb_get_sessions", "wcdb_get_messages", "wcdb_get_display_names",
                 "wcdb_get_group_members", "wcdb_get_voice_data", "wcdb_free_string", "wcdb_close_account")

    def __init__(self, pause=0.0):
        for name in self.FUNCTIONS:
            setattr(self, name, _CFunc(getattr(self, name)))
        self.pause = pause
        self.calls = []                  # (function, did the caller hold the DLL lock)
        self.inside = 0
        self.most_inside = 0
        self._count = threading.Lock()
        self._kept = []                  # the strings handed out stay alive
        self.sessions: list = []
        self.chats: dict = {}
        self.names: dict = {}
        self.codes: dict = {}            # function -> the error code it answers with
        self.chat_codes: dict = {}       # chat -> the error code wcdb_get_messages answers with
        self.null_chats: set = set()     # chats answered with ret 0 and no string at all
        self.count = 0

    def say(self, chat, sender, text, at=None, local_type=1):
        self.count += 1
        self.chats.setdefault(chat, []).insert(0, {
            "sender_username": sender, "message_content": text, "local_type": local_type,
            "create_time": int(time.time() if at is None else at), "local_id": self.count,
            "server_id": f"svr{self.count}"})

    def rows(self, chat, limit):
        """What wcdb_get_messages answers for a chat (a subclass may make it up)."""
        return self.chats.get(chat, [])[:limit]

    # bookkeeping
    def _enter(self, name):
        with self._count:
            self.calls.append((name, dll_lock_held()))
            self.inside += 1
            self.most_inside = max(self.most_inside, self.inside)
        if self.pause:
            time.sleep(self.pause)       # a call takes a while: another thread could come in meanwhile

    def _leave(self):
        with self._count:
            self.inside -= 1

    def _out(self, out, raw: bytes) -> int:
        buf = ct.create_string_buffer(raw)
        self._kept.append(buf)
        out._obj.value = ct.addressof(buf)
        return 0

    def _json(self, name, out, value, code=None) -> int:
        self._enter(name)
        try:
            code = self.codes.get(name) if code is None else code
            if code:
                return code
            return self._out(out, json.dumps(value, ensure_ascii=False).encode("utf-8"))
        finally:
            self._leave()

    def names_of(self):
        return [name for name, _ in self.calls]

    # the DLL
    def wcdb_open_account(self, path, key, handle):
        self._enter("wcdb_open_account")
        try:
            code = self.codes.get("wcdb_open_account")
            if code:
                return code
            handle._obj.value = 7
            return 0
        finally:
            self._leave()

    def wcdb_get_sessions(self, handle, out):
        return self._json("wcdb_get_sessions", out, self.sessions)

    def wcdb_get_messages(self, handle, talker, limit, offset, out):
        chat = talker.decode("utf-8")
        if chat in self.null_chats and not self.codes.get("wcdb_get_messages"):
            self._enter("wcdb_get_messages")
            self._leave()
            return 0                     # ret 0 and a null pointer: nothing there
        return self._json("wcdb_get_messages", out, self.rows(chat, limit), code=self.chat_codes.get(chat))

    def wcdb_get_display_names(self, handle, usernames, out):
        wanted = json.loads(usernames.decode("utf-8"))
        return self._json("wcdb_get_display_names", out,
                          {u: self.names.get(u, f"名字{u[-3:]}") for u in wanted})

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


def data_dir(tmp: Path) -> Path:
    """A WeChat data folder WcdbNativeClient finds (WECHAT_DATA_DIR): <tmp>/wxid_me_1234/…/session.db."""
    session = tmp / ME / "db_storage" / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "session.db").write_bytes(b"")
    return tmp


def make_client(dll: FakeDll, tmp: Path, *, opened: bool = True) -> WcdbNativeClient:
    """The real client on the fake DLL (WECHAT_DATA_DIR must point at ``data_dir(tmp)``); ``opened``:
    with a handle already, as after open()."""
    client = WcdbNativeClient(dll_dir=str(tmp))
    client._dll = dll
    client._handle = 7 if opened else 0
    return client
