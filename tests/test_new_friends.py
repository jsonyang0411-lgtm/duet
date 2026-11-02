"""New friends (src/wechat/new_friends.py): found in WeChat's database beside the poll loop, reported
once each, told apart by how they came; WeChat's friend notices are nobody's words; in copilot mode
registering a new friend drafts her messages since she was added (one reviewed job), and with
PRM_AUTO_REGISTER=on the bot registers every new friend itself (on PRM's sender thread, after the look).

The WeChat database is a fake shaped like the real one (WeChat 4.1.15.36, tools/friend_probe.py,
2026-10-27): session rows carry no names, messages newest first, system lines type 10000, this
account's own lines from its wxid without the data folder's suffix.
"""

import json
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from router_support import ADMIN, BOT, TraceSpy

from src.admin import AdminCommandHandler
from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.edge import protocol as P
from src.edge.service import EdgeService
from src.edge.store import EdgeStore
from src.prm import gates
from src.prm.service import AUTO_QUEUE, OPENING_UNSURE_TEXT, SETTLE_SEC, PrmService, new_friend_note, opening_note
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter
from src.wechat import new_friends as NF
from src.wechat.new_friends import (AUTO_ACCEPT, MANUAL, REQUESTED, UNKNOWN, AcceptLog, NewFriendService,
                                    NewFriendStore, find_added, is_friend_notice, recent_new_friends)
from src.wechat.wcdb_backend import WcdbBackend

ME = "wxid_me_b6ce"                  # myWxid: the data folder's name, with its suffix
ME_SENDER = "wxid_me"                # what this account's own messages say as their sender
NEW = "wxid_newfriend12345"          # 19 characters, like the probe's
CUSTOM = "meimei_88"                 # an old account's custom id: no wxid_ prefix
ASKED = "wxid_brand0000000001"
OLD = "wxid_oldfriend001234"
GREETING = "我是阿花，做美妆的，电话 13800138000"
PASSED_LINE = "我通过了你的朋友验证请求，现在我们可以开始聊天了"


class Clock:
    def __init__(self, now=None):
        self.now = float(int(time.time())) if now is None else now

    def __call__(self):
        return self.now


class WeChatDb:
    """WeChat's database as the WCDB backend and the scan read it."""

    is_open = True

    def __init__(self, clock):
        self._config = {"myWxid": ME}
        self.clock = clock
        self.chats: dict[str, list[dict]] = {}
        self.times: dict[str, float] = {}
        self.names: dict[str, str] = {}
        self.count = 0
        self.read: list[str] = []
        self.fail = False

    def say(self, chat, sender, text, at=None, local_type=1):
        at = self.clock.now if at is None else at
        self.count += 1
        self.chats.setdefault(chat, []).insert(0, {
            "sender_username": sender, "message_content": text, "local_type": local_type,
            "create_time": int(at), "local_id": self.count, "server_id": f"svr{self.count}"})
        self.chats[chat].sort(key=lambda m: (-m["create_time"], -m["local_id"]))
        self.times[chat] = max(self.times.get(chat, 0), at)

    def system(self, chat, text, at=None):
        self.say(chat, "", text, at, local_type=10000)

    # what WcdbNativeClient offers
    def init(self):
        pass

    def open(self):
        pass

    def close(self):
        pass

    def get_sessions(self, limit=500):
        if self.fail:
            raise RuntimeError("the database went away")
        return [{"username": chat, "sort_timestamp": at, "unread_count": 0}
                for chat, at in sorted(self.times.items(), key=lambda kv: -kv[1])]

    def get_messages(self, talker, limit=200, offset=0):
        if self.fail:
            raise RuntimeError("the database went away")
        self.read.append(talker)
        return [dict(m) for m in self.chats.get(talker, [])[:limit]]

    def get_display_names(self, usernames):
        return {u: self.names[u] for u in usernames if u in self.names}

    def resolve_nickname(self, wxid):
        return self.names.get(wxid, wxid)

    def get_group_members(self, chat):
        return []


class Backend:
    """What the scan uses of WcdbBackend."""

    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.names = {}
        self.asked = []

    def with_client(self, fn):
        with self.lock:
            return fn(self.db) if self.db.is_open else None

    def own_wxid(self):
        return ME

    def note_private_name(self, wxid, name):
        self.names[wxid] = name

    def catch_up_chat(self, talker, name, since, sink):
        self.asked.append((talker, name, since, sink))


# the three ways the probe saw on the real machine
def accepted_on_pc(db, wxid, name, at, greeting=GREETING):
    if greeting:
        db.say(wxid, wxid, greeting, at - 5)
    db.system(wxid, f"你已添加了{name}，现在可以开始聊天了。", at)


def accepted_on_phone(db, wxid, name, at, greeting=GREETING):
    if greeting:
        db.say(wxid, wxid, greeting, at - 5)
    db.system(wxid, f"你已添加了{name}，以上是打招呼的消息。", at)


def they_accepted(db, wxid, at, our_greeting="你好，我是品牌商务小王"):
    db.say(wxid, ME_SENDER, our_greeting, at - 60)
    db.say(wxid, wxid, PASSED_LINE, at)
    db.system(wxid, "以上是打招呼的消息", at + 1)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(clock):
    d = WeChatDb(clock)
    d.say(OLD, OLD, "老朋友的话", clock.now - 3 * 86400)
    d.system(OLD, "你已添加了老朋友，现在可以开始聊天了。", clock.now - 30 * 86400)   # a friend from before
    d.names.update({OLD: "老朋友", NEW: "阿花", CUSTOM: "小美美", ASKED: "品牌小青"})
    return d


@pytest.fixture
def backend(db):
    return Backend(db)


def make(tmp_path, backend, clock, **kw):
    return NewFriendService(backend, NewFriendStore(str(tmp_path / "messages.db")), clock=clock, pause=0, **kw)


def started(tmp_path, backend, clock, **kw):
    """A service after its first look ever (the start marker), a minute later."""
    service = make(tmp_path, backend, clock, **kw)
    assert service.scan_once() == []
    clock.now += 60
    return service


# ── what a new friend looks like ─────────────────────────────────────────────

def test_the_three_ways_of_the_probe_are_found_with_name_greeting_and_how(tmp_path, db, backend, clock):
    service = started(tmp_path, backend, clock)
    t = clock.now
    accepted_on_pc(db, NEW, "阿花", t)
    accepted_on_phone(db, CUSTOM, "小美美", t + 1, greeting="想合作，加个微信")
    they_accepted(db, ASKED, t + 2)
    found = {e["wxid"]: e for e in service.scan_once()}
    assert {wxid: {k: e[k] for k in ("wxid", "name", "remark", "verify_text", "how")} for wxid, e in found.items()} == {
        NEW: {"wxid": NEW, "name": "阿花", "remark": "", "verify_text": GREETING, "how": MANUAL},
        CUSTOM: {"wxid": CUSTOM, "name": "小美美", "remark": "", "verify_text": "想合作，加个微信", "how": MANUAL},
        ASKED: {"wxid": ASKED, "name": "品牌小青", "remark": "", "verify_text": "", "how": REQUESTED},
    }
    # 加百应达人微信 (功能 R): which line it was, when, and our own greeting when she accepted our request
    assert {wxid: (e["kind"], e["added_at"], e["own_greeting"]) for wxid, e in found.items()} == {
        NEW: ("pc", t, ""), CUSTOM: ("phone", t + 1, ""), ASKED: ("requested", t + 2, "你好，我是品牌商务小王")}
    assert {r["wxid"]: r["kind"] for r in service.store.recent(0)} == {NEW: "pc", CUSTOM: "phone", ASKED: "requested"}
    assert backend.names == {NEW: "阿花", CUSTOM: "小美美", ASKED: "品牌小青"}       # names for the console / sends


def test_find_added_reads_the_lines_oldest_first_and_only_her_greeting(clock):
    t = clock.now
    messages = [
        {"local_type": 1, "sender_username": NEW, "message_content": "早先聊过一句", "create_time": t - 9 * 86400},
        {"local_type": 1, "sender_username": ME_SENDER, "message_content": "我们的话", "create_time": t - 100},
        {"local_type": 1, "sender_username": NEW, "message_content": "ㅤ", "create_time": t - 50},
        {"local_type": 1, "sender_username": NEW, "message_content": "你好", "create_time": t - 20},
        {"local_type": 1, "sender_username": NEW, "message_content": "我是阿花 13800138000", "create_time": t - 10},
        {"local_type": 10000, "sender_username": "", "message_content": "你已添加了阿花，现在可以开始聊天了。",
         "create_time": t},
        {"local_type": 1, "sender_username": NEW, "message_content": "在吗", "create_time": t + 30},
    ]
    added = find_added(list(reversed(messages)), NEW, ME)
    assert (added.kind, added.name, added.at) == ("pc", "阿花", t)
    assert added.verify_text == "你好\n我是阿花 13800138000" and added.verify_from == t - 20   # not ours, no filler
    assert find_added(messages[:5], NEW, ME) is None                 # no line yet: not a new friend
    # an XML system line and a 「以上是打招呼的消息」 of its own are read too
    xml = {"local_type": 10000, "sender_username": "", "create_time": t,
           "message_content": "<sysmsg><content>你已添加了小美，以上是打招呼的消息。</content></sysmsg>"}
    assert (find_added([xml], NEW, ME).kind, find_added([xml], NEW, ME).name) == ("phone", "小美")
    alone = {"local_type": 10000, "sender_username": "", "message_content": "以上是打招呼的消息", "create_time": t}
    assert find_added([alone], NEW, ME).kind == "greeting"


def test_a_chat_of_hangul_fillers_is_not_a_new_friend(tmp_path, db, backend, clock):
    """Someone sending ㅤ (U+3164) to see whether we deleted them: no friend line, nothing reported."""
    service = started(tmp_path, backend, clock)
    for n in range(3):
        db.say(NEW, NEW, "ㅤ" * (n + 1), clock.now + n)
    db.say(OLD, OLD, "ㅤ", clock.now + 5)
    assert service.scan_once() == [] and service.store.recent(0) == []
    assert NEW in db.read                                            # it was looked at, and passed over


def test_groups_official_accounts_wecom_wechat_chats_and_self_are_never_read(tmp_path, db, backend, clock):
    service = started(tmp_path, backend, clock)
    for chat in ("room1@chatroom", "gh_abcdef123456", "wangzong@openim", "filehelper", "weixin",
                 "brandsessionholder", "@placeholder_foldgroup", ME_SENDER, ME):
        accepted_on_pc(db, chat, "谁", clock.now)
    accepted_on_pc(db, NEW, "阿花", clock.now)
    accepted_on_pc(db, "brandon_88", "布兰登", clock.now + 1)       # a person whose custom id starts with brand
    assert [e["wxid"] for e in service.scan_once()] == [NEW, "brandon_88"]
    assert set(db.read) == {NEW, "brandon_88"}


def test_a_friend_is_reported_once_also_after_a_restart(tmp_path, db, backend, clock):
    service = started(tmp_path, backend, clock)
    accepted_on_pc(db, NEW, "阿花", clock.now)
    assert [e["wxid"] for e in service.scan_once()] == [NEW]
    clock.now += 60
    db.say(NEW, NEW, "在吗", clock.now)                              # her chat changes again
    assert service.scan_once() == []
    restarted = make(tmp_path, backend, clock)                       # the bot restarted: same database
    clock.now += 60
    db.say(NEW, NEW, "你好？", clock.now)
    assert restarted.scan_once() == [] and len(restarted.store.recent(0)) == 1


def test_what_came_while_the_bot_was_stopped_is_found_at_the_first_look(tmp_path, db, backend, clock):
    service = started(tmp_path, backend, clock)
    service.stop()
    clock.now += 3600
    accepted_on_phone(db, NEW, "阿花", clock.now)                   # added on the phone meanwhile
    later = make(tmp_path, backend, clock)
    assert [(e["wxid"], e["how"]) for e in later.scan_once()] == [(NEW, MANUAL)]


def test_history_before_the_start_marker_is_never_reported(tmp_path, db, backend, clock):
    accepted_on_pc(db, NEW, "阿花", clock.now - 600)                 # added before the feature was first on
    service = make(tmp_path, backend, clock)
    assert service.scan_once() == []                                 # the first look ever: the start marker
    first_start = service.store.started_at()
    clock.now += 60
    db.say(NEW, NEW, "又发了一句", clock.now)                         # her chat changes: still an old friend
    db.say(OLD, OLD, "我也是", clock.now)
    assert service.scan_once() == [] and service.store.recent(0) == []
    accepted_on_pc(db, CUSTOM, "小美美", clock.now + 5)
    assert [e["wxid"] for e in service.scan_once()] == [CUSTOM]
    assert make(tmp_path, backend, clock).store.started_at() == first_start      # set once


def test_only_chats_that_changed_are_read_and_a_burst_is_spread_over_looks(tmp_path, db, backend, clock, monkeypatch):
    service = started(tmp_path, backend, clock)
    db.read.clear()
    assert service.scan_once() == [] and db.read == []               # nothing changed: nothing read
    monkeypatch.setattr(NF, "MAX_CHATS_PER_SCAN", 2)
    for n in range(5):
        accepted_on_pc(db, f"wxid_burst{n:015d}", f"人{n}", clock.now + n)
    assert len(service.scan_once()) == 2 and len(service.scan_once()) == 2 and len(service.scan_once()) == 1
    assert service.scan_once() == []


def test_errors_never_stop_the_scan(tmp_path, db, backend, clock, caplog):
    service = started(tmp_path, backend, clock)
    db.fail = True
    with caplog.at_level(logging.WARNING):
        assert service.scan_once() == [] and service.scan_once() == []
    assert len([r for r in caplog.records if "the look failed" in r.getMessage()]) == 1   # said once
    db.is_open = False
    assert service.scan_once() == []                                 # the database is not open: nothing
    db.fail, db.is_open = False, True
    accepted_on_pc(db, NEW, "阿花", clock.now)
    assert [e["wxid"] for e in service.scan_once()] == [NEW]


def test_the_thread_looks_on_its_own_and_stops(tmp_path, db, backend, clock):
    service = make(tmp_path, backend, clock, interval=1, first_wait=0)
    service.start()
    try:
        for _ in range(50):
            if service.store.started_at() is not None:
                break
            time.sleep(0.05)
        assert service.store.started_at() is not None
    finally:
        service.stop()
    assert service._thread is None


# ── how they came ────────────────────────────────────────────────────────────

def test_a_request_the_bot_accepted_a_few_minutes_before_is_auto_accept(tmp_path, db, backend, clock):
    service = started(tmp_path, backend, clock)
    service.accepts.note(clock.now - 30, "阿花 我是阿花，做美妆的")     # FriendManager's on_accepted
    accepted_on_pc(db, NEW, "阿花", clock.now)
    accepted_on_pc(db, CUSTOM, "小美美", clock.now + 2)               # a person on the PC, same minute
    found = {e["wxid"]: e["how"] for e in service.scan_once()}
    assert found == {NEW: AUTO_ACCEPT, CUSTOM: UNKNOWN}              # the bot accepted one: hers by the name


def test_how_by_time_name_and_where_it_was_accepted(tmp_path, clock):
    log = AcceptLog(NewFriendStore(str(tmp_path / "a.db")), clock)
    t = clock.now
    assert log.judge("阿花", t) == MANUAL                             # the bot accepted nothing
    log.note(t - 400)
    assert log.judge("阿花", t) == MANUAL                             # too long before
    log.note(t + 10)
    assert log.judge("阿花", t) == AUTO_ACCEPT                        # just before / after: the clocks
    assert log.judge("小美", t + 1) == UNKNOWN                        # that accept counted already
    log.note(t + 100, "小美 申请添加你为朋友")
    assert log.judge("小美", t + 101, phone=True) == AUTO_ACCEPT      # her name on the row: the bot's
    log.note(t + 200, "张三")
    assert log.judge("阿青", t + 201, phone=True) == MANUAL           # the phone, another name: a person's
    assert log.judge("阿青", t + 202) == UNKNOWN                      # OCR read another name: cannot tell
    restarted = AcceptLog(NewFriendStore(str(tmp_path / "a.db")), clock)    # the times outlive a restart
    assert restarted.judge("别人", t + 5) == UNKNOWN                   # t+10 counted already: written down
    assert restarted.judge("某人", t + 201) == AUTO_ACCEPT              # t+200, its row's text forgotten


def test_only_a_greeting_line_is_unknown(tmp_path, db, backend, clock):
    service = started(tmp_path, backend, clock)
    db.say(NEW, NEW, "你好", clock.now - 5)
    db.system(NEW, "以上是打招呼的消息", clock.now)
    assert [(e["wxid"], e["how"], e["verify_text"]) for e in service.scan_once()] == [(NEW, UNKNOWN, "")]


def test_the_friend_manager_tells_every_accept(tmp_path, db, backend, clock):
    """AUTO_ACCEPT_FRIENDS and 同意好友 both accept through FriendManager: its on_accepted is the log."""
    from test_friend_manager import FakeOcr, FakeWindow, row
    from src.wechat import friend_manager
    from src.wechat.friend_manager import FriendManager

    service = started(tmp_path, backend, clock)
    told = []
    manager = FriendManager(window_controller=FakeWindow(), clicker=lambda x, y: True, sleeper=lambda s: None,
                            rect_of=lambda hwnd: (0, 0, 1000, 1000),
                            on_accepted=lambda at, label: told.append(label) or service.accepts.note(clock.now, label),
                            ocr_engine=FakeOcr([row("新的朋友", 0.15, 0.12, 0.25, 0.16)],
                                               [row("阿花", 0.35, 0.20, 0.50, 0.22),
                                                row("我是阿花，做美妆的", 0.35, 0.225, 0.60, 0.25),
                                                row("接受", 0.70, 0.21, 0.76, 0.25),
                                                row("张三", 0.35, 0.40, 0.50, 0.45)], [], []))
    original = friend_manager.press_key
    friend_manager.press_key = lambda vk: None
    try:
        assert len(manager.accept_friend_requests()) == 1
    finally:
        friend_manager.press_key = original
    assert told == ["阿花 我是阿花，做美妆的"]                          # its row, not the next one
    accepted_on_pc(db, NEW, "阿花", clock.now + 3)
    assert [e["how"] for e in service.scan_once()] == [AUTO_ACCEPT]


# ── edge mode: a friend_added event; the greeting only in it ─────────────────

def edge_service(tmp_path, clock):
    config = type("C", (), dict(prm_edge="on", admin_wxid="wxid_admin", claude_relay_wxids=[], edge_keep_days=7,
                                edge_send_min_gap_sec=0, edge_send_per_hour=1000, edge_seen_text_sec=1800,
                                edge_seen_file_sec=600))()
    edge = EdgeService(config, store=EdgeStore(":memory:"), clock=clock, files_dir=tmp_path / "edge",
                       send_text=lambda c, t: True, own_wxid=lambda: ME, private_names=lambda: {})
    edge.start(run_thread=False)
    return edge


def test_edge_mode_a_new_friend_is_one_event_and_her_greeting_lives_only_there(tmp_path, db, backend, clock, caplog):
    edge = edge_service(tmp_path, clock)
    service = started(tmp_path, backend, clock, on_found=[edge.friend_added])
    accepted_on_pc(db, NEW, "阿花", clock.now)
    with caplog.at_level(logging.DEBUG):
        service.scan_once()
        clock.now += 60
        db.say(NEW, NEW, "在吗", clock.now)
        service.scan_once()
    [event] = edge.store.events_after(0, 100)[1:]                    # (the first is the edge's state)
    assert event["type"] == P.FRIEND_ADDED and "chat_id" not in event
    assert {k: event[k] for k in ("wxid", "name", "remark", "verify_text", "how")} == {
        "wxid": NEW, "name": "阿花", "remark": "", "verify_text": GREETING, "how": MANUAL}
    assert service.store.get(NEW)["verify_text"] == ""                # not in the bot's database
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "13800138000" not in logged and "做美妆" not in logged and "阿花" not in logged
    # a week after the Mac acknowledged it, the event (and the greeting) is gone
    edge.ack(event["id"])
    edge.cleanup(clock.now + 8 * 86400)
    assert edge.store.events_after(0, 100) == []


def test_an_event_that_cannot_be_stored_is_tried_again_at_the_next_look(tmp_path, db, backend, clock):
    calls = []

    def flaky(event):
        calls.append(event["wxid"])
        if len(calls) == 1:
            raise OSError("edge.db is busy")

    service = started(tmp_path, backend, clock, on_found=[flaky])
    service.accepts.note(clock.now - 5)
    accepted_on_pc(db, NEW, "阿花", clock.now)
    assert service.scan_once() == []
    [event] = service.scan_once()                                    # again, as first judged
    assert calls == [NEW, NEW] and event["how"] == AUTO_ACCEPT
    assert service.scan_once() == [] and calls == [NEW, NEW]


# ── copilot mode: the console's list ─────────────────────────────────────────

def test_copilot_mode_keeps_the_greeting_masked_and_forgets_it_after_a_week(tmp_path, db, backend, clock):
    service = started(tmp_path, backend, clock, keep_verify=True)
    accepted_on_pc(db, NEW, "阿花", clock.now)
    service.scan_once()
    [record] = service.recent()
    assert record["verify_text"] == "我是阿花，做美妆的，电话 138****8000" and record["how_label"] == "手动通过的"
    assert recent_new_friends(str(tmp_path / "messages.db"), now=clock.now)[0]["wxid"] == NEW   # a stopped bot
    clock.now += NF.NEW_DAYS * 86400 + 3600
    db.say(OLD, OLD, "一周后", clock.now)
    service.scan_once()
    assert service.store.get(NEW)["verify_text"] == "" and service.recent() == []


def test_a_stopped_bot_without_a_database_shows_no_new_friends(tmp_path):
    assert recent_new_friends(str(tmp_path / "nothing.db")) == []
    assert not (tmp_path / "nothing.db").exists()
    initialize_db(str(tmp_path / "old.db")).close()                  # a database from before the feature
    assert recent_new_friends(str(tmp_path / "old.db")) == []


# ── WeChat's notice is nobody's words ────────────────────────────────────────

def standardized(text, msg_type=1, sender=NEW, **extra):
    return {"message_id": f"m-{time.time_ns()}", "chat_id": NEW, "sender_id": sender, "sender_name": "阿花",
            "content": text, "msg_type": msg_type, "is_group": False, "timestamp": int(time.time()), **extra}


def test_friend_notices_are_recognised_and_ordinary_words_are_not():
    assert is_friend_notice(standardized(PASSED_LINE))
    assert is_friend_notice(standardized("[消息类型:10000]", 10000,
                                         special={"kind": "system", "title": "你已添加了阿花，现在可以开始聊天了。"}))
    assert is_friend_notice(standardized("[消息类型:10000]", 10000, special={"title": "以上是打招呼的消息"}))
    assert is_friend_notice(standardized("我通过了你的朋友验证请求, 现在我们可以开始聊天了。"))
    for words in ("我通过了你的朋友验证请求吗？我怎么没看到", "在吗", "你已添加了我吗",
                  "我通过了你的朋友验证请求，现在我们可以开始聊天了，你好呀我是小美"):
        assert not is_friend_notice(standardized(words))
    assert not is_friend_notice(standardized("[消息类型:10000]", 10000, special={"title": "对方撤回了一条消息"}))


def prm_for(clock, tmp_path):
    from test_prm import config as prm_config
    relay = ClaudeRelay(prm_config(), store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    prm = PrmService(prm_config(), relay, store=PrmStore(":memory:"), clock=clock, send=lambda c, t: True)
    prm.register(NEW, "阿花")
    return prm, relay


def test_the_notice_is_never_drafted_and_never_an_operators_reply(tmp_path, clock):
    prm, relay = prm_for(clock, tmp_path)
    try:
        assert prm.handle(standardized(PASSED_LINE, timestamp=clock.now)) is True       # handled: silently
        prm.on_own_message(standardized(PASSED_LINE, sender=ME_SENDER, timestamp=clock.now))
        clock.now += SETTLE_SEC + 1
        assert relay.next_job(0, ("prm",)) is None
        deal = prm.store.deal_of(prm.store.party_by_wxid(NEW)["id"])
        assert deal["mode"] == "copilot" and prm.store.events(deal["id"]) == []      # no takeover, nothing recorded
        prm.handle(standardized("那我们聊聊合作", timestamp=clock.now))
        clock.now += SETTLE_SEC + 1
        assert relay.next_job(0, ("prm",))["text"].endswith("那我们聊聊合作")         # her words still are
    finally:
        prm.stop()
        relay.stop()


def test_the_notice_is_never_forwarded_as_a_creators_message(tmp_path, clock):
    edge = edge_service(tmp_path, clock)
    edge.put_parties(1, [NEW])
    assert edge.handle(standardized(PASSED_LINE, timestamp=clock.now)) is True
    edge.on_own_message(standardized(PASSED_LINE, sender=ME_SENDER, timestamp=clock.now))
    edge.handle(standardized("在吗", timestamp=clock.now))
    kinds = [(e["type"], e.get("text")) for e in edge.store.events_after(0, 100)[1:]]
    assert kinds == [(P.CHAT_NOTICE, PASSED_LINE), (P.CHAT_NOTICE, PASSED_LINE), (P.CREATOR_MESSAGE, "在吗")]


# ── copilot mode: registering a new friend drafts what she said ──────────────

class Copilot:
    """The bot in copilot mode as Bot.run wires it: the WCDB backend over the fake database, the
    router, the relay, PRM and the new-friend scan (the bot's database in tmp_path).  ``auto_register``:
    PRM_AUTO_REGISTER (the scan's ``on_found`` is then PRM's ``new_friend_found``, as Bot._start_new_friends
    makes it); ``night``: PRM_NIGHT_AUTO_HOURS; ``relay_wxids``: the cc users; ``scan``: the scan's timing."""

    def __init__(self, tmp_path, clock, db, private_chats="*", catchup_hours=12, prm_store=None, relay_store=None,
                 auto_register="off", night="", relay_wxids=(), friend_add=False, **scan):
        config = BotConfig(
            private_chat_enabled=True, private_chats=private_chats, admin_wxid=ADMIN, bot_display_name=BOT,
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_db=":memory:", prm_copilot="on", prm_db=":memory:", prm_catchup_hours=catchup_hours,
            prm_auto_register=auto_register, prm_night_auto_hours=night, claude_relay_wxids=list(relay_wxids),
            prm_friend_add="on" if friend_add else "off")
        self.clock, self.db = clock, db
        self.messages = MessageStore(initialize_db(str(tmp_path / "messages.db")))
        self.router = MessageRouter(store=self.messages, admin_handler=AdminCommandHandler(), config=config,
                                    tracer=TraceSpy())
        self.sent = []
        self.relay = ClaudeRelay(config, store=relay_store or RelayStore(":memory:"), clock=clock,
                                 files_dir=tmp_path / "relay",
                                 send=lambda chat, text: self.sent.append((chat, text)) or True)
        self.router._claude_relay = self.relay
        self.backend = WcdbBackend(bot_display_name=BOT, groups=["*"], poll_sec=0, store=self.messages, config=config)
        self.prm = PrmService(config, self.relay, store=prm_store or PrmStore(":memory:"), clock=clock,
                              send=lambda chat, text: self.sent.append((chat, text)) or True,
                              on_parties_changed=self.backend.refresh_private_chats_soon)
        self.router._prm = self.prm
        self.backend._client = db
        self.backend.set_private_filter(self.prm.is_party_chat)
        self.backend.set_own_message_hook(self.prm.on_own_message)
        self.backend._send_and_confirm = lambda name, chat, text: self.sent.append((chat, text)) or True
        self.backend._resolve_groups()
        self.backend._prime_known_ids()
        self.backend._running = True
        # 加百应达人微信 (friend_add): as Bot._start_new_friends wires it, a fake friend manager behind the admin handler
        self.friends = NewFriendService(self.backend, NewFriendStore(str(tmp_path / "messages.db")),
                                        on_found=([self.prm.friend_adds.on_new_friend] if friend_add else [])
                                        + ([self.prm.new_friend_found] if self.prm.auto_register else []),
                                        keep_verify=True, catchup_hours=catchup_hours, clock=clock, pause=0, **scan)
        self.friends.set_catch_up_sink(self.prm.take_new_friend)
        self.prm.on_registered = self.friends.registered
        if self.prm.auto_register:
            self.prm.resume_found()
        self.added, self.remarks = [], []
        if friend_add:                    # (a 4.0 window whose 「备注」 field OCR saw: remark_set)
            manager = SimpleNamespace(add_friend=lambda keyword, message="", remark="": self.added.append((keyword, message))
                                      or self.remarks.append(remark)
                                      or {"ok": True, "target": keyword, "message": "已成功",
                                          **({"remark_set": True} if remark else {})})
            self.prm.friend_adds.attach(AdminCommandHandler(friend_manager=manager), None, None)
            self.prm.friend_adds.spawn, self.prm.friend_adds.clear_clipboard = (lambda fn: fn()), (lambda text: False)
            self.prm.friend_adds.set_new_friends(self.friends)

    def poll(self):
        self.backend.refresh_private_chats_soon()
        self.backend._poll_cycle(self.router.handle)
        for msg in self.backend._burst.flush_all():
            self.backend._dispatch_message(msg, self.router.handle)

    def jobs(self):
        return list(reversed(self.relay.store.list_jobs(limit=100)))

    def mac_drafts(self, kind="faq"):
        self.clock.now += SETTLE_SEC + 1
        job = self.relay.next_job(0, ("prm",))
        draft = {"intent": "想合作", "slots": {}, "stage_proposal": "talk", "message_kind": kind,
                 "reply_draft": "你好阿花～欢迎", "attachments_to_send": [], "needs_attention": False,
                 "reason": "kb/合作政策.md"}
        self.relay.finish(job["id"], {"ok": True, "text": json.dumps(draft, ensure_ascii=False), "duration_s": 3})
        self.relay.tick()
        return job

    def stop(self):
        self.backend._running = False
        self.friends.stop()
        self.prm.stop()
        self.relay.stop()


def lines(job):
    return [line.split("] ", 1)[1] for line in job["text"].split("\n")]


@pytest.fixture
def copilot_bot(tmp_path, clock, db, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # data/private_chat_names.json goes here
    bots = []

    def make_bot(**kw):
        bot = Copilot(tmp_path, clock, db, **kw)
        bots.append(bot)
        return bot

    yield make_bot
    for bot in bots:
        bot.stop()


def test_registering_a_new_friend_drafts_her_messages_since_she_was_added_once(copilot_bot, db, clock):
    bot = copilot_bot()
    bot.friends.scan_once()                                          # the start marker
    clock.now += 120
    t = clock.now
    accepted_on_phone(db, NEW, "阿花", t)                             # greeting at t-5, added at t
    db.say(NEW, NEW, "在吗？想要个样品", t + 60)
    db.say(NEW, ME_SENDER, "你好呀", t + 90)                           # an operator on the phone
    clock.now = t + 120
    bot.poll()                                                       # PRIVATE_CHATS=*: read, stored, silence
    assert bot.jobs() == [] and bot.sent == []
    [found] = bot.friends.scan_once()
    assert (found["how"], found["verify_text"]) == (MANUAL, GREETING)

    party = bot.prm.register(NEW, "阿花")                              # an operator's decision
    assert party["catch_up"] is True
    bot.poll()
    [job] = bot.jobs()
    assert lines(job) == ["（加好友时的验证消息）我是阿花，做美妆的，电话 〈手机1〉", "在吗？想要个样品"]
    assert json.loads(job["meta"])["new_friend"] == {"from": t - 5, "count": 2}

    # even a kind that has earned "auto" waits for a person
    bot.prm.store.set_gate("faq", gates.AUTO, clock.now)
    bot.mac_drafts()
    [packet] = bot.prm.store.packets(("pending", "approved"))
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert bot.prm.store.deal(1)["attention"].startswith(new_friend_note(2, t - 5))
    assert bot.sent == []

    # nothing of it is handed on again: not by the next polls, not after a restart
    assert bot.prm.decide(packet["id"], "send")["ok"] and bot.prm.send_approved() == 1   # (she got the answer)
    bot.poll()
    db.say(NEW, NEW, "收到了吗", clock.now)
    bot.poll()
    assert len(bot.jobs()) == 2 and lines(bot.jobs()[-1]) == ["收到了吗"]
    assert "new_friend" not in json.loads(bot.jobs()[-1]["meta"])
    bot.stop()
    clock.now += 600
    again = copilot_bot(prm_store=bot.prm.store, relay_store=bot.relay.store)
    again.poll()
    again.poll()
    assert len(again.jobs()) == 2 and again.friends.scan_once() == []


def test_the_same_draft_of_a_creator_who_is_not_new_goes_out_by_its_gate(copilot_bot, db, clock):
    """The control of the test above: only being a new friend's earlier messages makes the review."""
    bot = copilot_bot()
    bot.friends.scan_once()
    bot.prm.register(OLD, "老朋友")
    bot.poll()
    bot.prm.store.set_gate("faq", gates.AUTO, clock.now)
    db.say(OLD, OLD, "在吗", clock.now)
    bot.poll()
    bot.mac_drafts()
    [packet] = bot.prm.store.packets(("pending", "approved"))
    assert (packet["status"], packet["route"]) == ("approved", "auto")
    assert bot.prm.store.deal(1)["attention"] == ""


def test_a_new_friend_whose_chat_was_not_read_is_caught_up_when_registered(copilot_bot, db, clock):
    """A custom id is not read under PRIVATE_CHATS=*, nor anything without being listed: the catch-up
    reads it once it is registered, and the first read of the chat does not hand anything twice."""
    bot = copilot_bot(private_chats="wxid_admin")
    bot.friends.scan_once()
    clock.now += 120
    t = clock.now
    accepted_on_pc(db, CUSTOM, "小美美", t, greeting="想合作")
    db.say(CUSTOM, CUSTOM, "你好", t + 30)
    clock.now = t + 60                                               # within the last 5 minutes, too
    bot.poll()
    assert CUSTOM not in bot.backend._chat_is_group and bot.messages.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (CUSTOM,)).fetchone()[0] == 0
    assert [e["wxid"] for e in bot.friends.scan_once()] == [CUSTOM]
    assert bot.prm.register(CUSTOM, "小美美")["catch_up"] is True
    bot.poll()
    bot.poll()
    [job] = bot.jobs()
    assert lines(job) == ["（加好友时的验证消息）想合作", "你好"]
    assert bot.messages.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (CUSTOM,)).fetchone()[0] == 2


def test_the_catch_up_goes_back_at_most_prm_catchup_hours_and_leaves_out_the_notice(copilot_bot, db, clock):
    bot = copilot_bot(catchup_hours=1)
    bot.friends.scan_once()
    clock.now += 120
    t = clock.now
    db.say(ASKED, ME_SENDER, "你好，我是品牌商务小王", t - 7200)       # our request, two hours earlier
    db.say(ASKED, ASKED, "我早上发过一句", t - 5400)                  # hers, older than an hour
    db.say(ASKED, ASKED, PASSED_LINE, t)
    db.system(ASKED, "以上是打招呼的消息", t + 1)
    db.say(ASKED, ASKED, "我们的产品有什么", t + 30)
    clock.now = t + 60
    bot.poll()                                                       # (the poll loop reads the sessions)
    assert [e["how"] for e in bot.friends.scan_once()] == [REQUESTED]
    assert bot.prm.register(ASKED, "小青")["catch_up"] is True
    bot.poll()
    [job] = bot.jobs()
    assert lines(job) == ["我们的产品有什么"]                          # no notice, nothing of ours, nothing old


def test_registering_someone_who_is_not_a_new_friend_asks_for_nothing(copilot_bot, db, clock):
    bot = copilot_bot()
    bot.friends.scan_once()
    db.say(OLD, OLD, "我也想合作", clock.now)
    bot.poll()
    assert bot.prm.register(OLD, "老朋友")["catch_up"] is False
    bot.poll()
    assert bot.jobs() == []                                          # the old rule: no catch-up


def test_the_bot_starts_the_scan_only_for_a_mode_that_uses_it(tmp_path, db, monkeypatch):
    from src.bot import Bot
    monkeypatch.chdir(tmp_path)
    config = BotConfig(db_path=str(tmp_path / "messages.db"), prm_catchup_hours=6)
    backend = Backend(db)
    assert Bot._start_new_friends(config, backend, None, None) is None              # neither mode
    assert Bot._start_new_friends(config, object(), object(), None) is None         # no database beside the loop

    class Prm:
        on_registered = None

        def take_new_friend(self, *a, **k):
            return 0

    prm = Prm()
    service = Bot._start_new_friends(config, backend, prm, None)
    try:
        assert prm.on_registered == service.registered and service._catch_up_sink == prm.take_new_friend
        assert service._keep_verify is True and service._catchup_sec == 6 * 3600
    finally:
        service.stop()
        from src.web import server
        server.register_new_friends(None)
    adds = SimpleNamespace(on_new_friend=lambda event: None, set_new_friends=lambda source: seen.append(source))
    seen = []
    prm.friend_adds = adds                                                    # 加百应达人微信 (功能 R)
    service = Bot._start_new_friends(config, backend, prm, None)
    try:
        assert service._on_found == [adds.on_new_friend] and seen == [service]
    finally:
        service.stop()
        from src.web import server
        server.register_new_friends(None)
    edge = type("E", (), {"friend_added": lambda self, e: None})()
    service = Bot._start_new_friends(config, backend, None, edge)
    try:
        assert service._keep_verify is False and len(service._on_found) == 1
    finally:
        service.stop()
        from src.web import server
        server.register_new_friends(None)


# ── copilot mode with PRM_AUTO_REGISTER=on: every new friend becomes a creator ──

HELPER = "wxid_helper000000001"      # a cc user (CLAUDE_RELAY_WXIDS)


def auto_lines(caplog):
    return [r.getMessage() for r in caplog.records if "registered automatically" in r.getMessage()]


def within(seconds, fn, *args):
    """``fn(*args)`` on a thread of its own: its result, or a failure when it has not ended after ``seconds`` (a
    deadlock is a failed test, not a suite that never ends)."""
    out = {}

    def run():
        try:
            out["value"] = fn(*args)
        except BaseException as e:                       # (raised again below, on the test's thread)
            out["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{getattr(fn, '__name__', fn)} did not end within {seconds} s"
    if "error" in out:
        raise out["error"]
    return out.get("value")


def test_off_a_new_friend_still_waits_for_a_person(copilot_bot, db, clock):
    """PRM_AUTO_REGISTER off (the default): exactly as before, nobody is registered by the bot."""
    bot = copilot_bot()
    assert bot.prm.auto_register is False and bot.friends._on_found == []
    bot.friends.scan_once()
    clock.now += 120
    accepted_on_pc(db, NEW, "阿花", clock.now)
    bot.poll()
    [found] = bot.friends.scan_once()
    bot.prm.new_friend_found(found)                                  # even were it handed over: nothing
    bot.prm.tick()
    bot.poll()
    assert bot.prm.store.parties(include_inactive=True) == [] and bot.jobs() == []
    assert not bot.prm.is_party_chat(NEW)


def test_on_a_new_friend_is_registered_after_the_look_and_her_messages_are_one_reviewed_draft(
        copilot_bot, db, clock, caplog):
    bot = copilot_bot(auto_register="on")
    assert bot.prm.auto_register is True and bot.friends._on_found == [bot.prm.new_friend_found]
    bot.friends.scan_once()                                          # the start marker
    clock.now += 120
    t = clock.now
    accepted_on_phone(db, NEW, "阿花", t)                             # her greeting at t-5, added at t
    db.say(NEW, NEW, "在吗？想要个样品", t + 30)
    clock.now = t + 60
    bot.poll()                                                       # PRIVATE_CHATS=*: read, stored, silence
    with caplog.at_level(logging.INFO):
        [found] = within(10, bot.friends.scan_once)
        # inside the look she was only queued: register() (-> on_registered -> the scan's registered) never runs
        # while the scan holds its lock
        assert bot.prm.store.parties(include_inactive=True) == []
        within(10, bot.prm.tick)                                     # the sender thread's next round
    [party] = bot.prm.store.parties()
    assert (party["wxid"], party["name"], party["note"], party["auto_registered"]) == (
        NEW, "阿花", "自动登记（新好友，她加我们）", 1)
    assert bot.prm.is_party_chat(NEW)
    [event] = bot.prm.store.events(bot.prm.store.deal_of(party["id"])["id"])
    assert (event["kind"], event["detail"], event["actor"]) == ("auto_register", {"how": MANUAL}, "system")
    assert auto_lines(caplog) == [f"PRM: new friend {NEW[:8]}… registered automatically as party 1 "
                                  "(PRM_AUTO_REGISTER; manual)"]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "阿花" not in logged and "13800138000" not in logged and NEW not in logged

    bot.poll()                                                       # her chat read back on its next poll
    [job] = bot.jobs()
    assert lines(job) == ["（加好友时的验证消息）我是阿花，做美妆的，电话 〈手机1〉", "在吗？想要个样品"]
    assert json.loads(job["meta"])["new_friend"] == {"from": t - 5, "count": 2}
    bot.prm.store.set_gate("faq", gates.AUTO, clock.now)             # even a kind that has earned "auto"
    bot.mac_drafts()
    [packet] = bot.prm.store.packets(("pending", "approved"))
    assert (packet["status"], packet["route"]) == ("pending", "review") and bot.sent == []

    # after it, she is a creator like any other: her next draft goes by its gate
    assert bot.prm.decide(packet["id"], "send")["ok"] and bot.prm.send_approved() == 1
    db.say(NEW, NEW, "那佣金怎么算", clock.now)
    bot.poll()
    assert len(bot.jobs()) == 2 and "new_friend" not in json.loads(bot.jobs()[-1]["meta"])
    bot.mac_drafts()
    [later] = bot.prm.store.packets(("pending", "approved"))
    assert (later["status"], later["route"]) == ("approved", "auto")


def test_at_night_her_first_draft_still_waits_and_a_later_one_may_go_out_by_itself(copilot_bot, db, clock):
    """The 下班时段 is on and it is inside it now: the catch-up draft of the friend the bot registered waits for a
    person (a new friend's job is never told it is night); once that is done her next draft may go out by itself."""
    hour = time.localtime(clock.now).tm_hour
    bot = copilot_bot(auto_register="on", night=f"{hour:02d}:00-{(hour + 2) % 24:02d}:00")
    bot.friends.scan_once()
    clock.now += 60
    accepted_on_pc(db, NEW, "阿花", clock.now)
    bot.poll()
    bot.friends.scan_once()
    assert bot.prm.register_found() == 1
    bot.poll()
    [job] = bot.jobs()
    meta = json.loads(job["meta"])
    assert meta["new_friend"]["count"] == 1 and "night" not in meta
    bot.mac_drafts()
    [first] = bot.prm.store.packets(("pending", "approved"))
    assert (first["status"], first["route"]) == ("pending", "review") and bot.sent == []
    assert bot.prm.decide(first["id"], "send")["ok"] and bot.prm.send_approved() == 1
    db.say(NEW, NEW, "你们是做什么产品的", clock.now)
    bot.poll()
    assert "night" in json.loads(bot.jobs()[-1]["meta"])
    bot.mac_drafts()
    [later] = bot.prm.store.packets(("pending", "approved"))
    assert (later["status"], later["route"]) == ("approved", "night")


def test_the_admin_cc_users_and_anyone_with_a_party_are_never_registered_or_started_again(
        copilot_bot, db, clock, caplog):
    bot = copilot_bot(auto_register="on", relay_wxids=[HELPER])
    bot.friends.scan_once()
    bot.prm.register(CUSTOM, "运营起的称呼", "运营登记的")             # an operator's creator
    stopped = bot.prm.register(ASKED, "品牌小青", "运营登记过")
    bot.prm.set_active(stopped["id"], False)                        # an operator's 停用
    clock.now += 120
    for n, (wxid, name) in enumerate(((ADMIN, "管理员"), (HELPER, "助理"), (CUSTOM, "小美美"), (ASKED, "品牌小青"))):
        accepted_on_pc(db, wxid, name, clock.now + n)
    bot.poll()
    with caplog.at_level(logging.INFO):
        assert len(bot.friends.scan_once()) == 4                     # all four are new friends to the scan
        assert bot.prm.register_found() == 0
    parties = {p["wxid"]: p for p in bot.prm.store.parties(include_inactive=True)}
    assert set(parties) == {CUSTOM, ASKED}
    assert ({k: parties[CUSTOM][k] for k in ("name", "note", "active", "auto_registered")}
            == {"name": "运营起的称呼", "note": "运营登记的", "active": 1, "auto_registered": 0})
    assert (parties[ASKED]["active"], parties[ASKED]["note"]) == (0, "运营登记过")     # 停用 sticks
    assert not bot.prm.is_party_chat(ASKED) and bot.prm.store.events(None, kinds=("auto_register",)) == []
    said = auto_lines(caplog)
    assert len(said) == 4 and all("not registered automatically" in line for line in said)
    assert sum("the admin or a cc user" in line for line in said) == 2
    assert any("stopped: an operator's 停用 stays" in line for line in said)
    # nor a group, an official account or one of WeChat's own chats, were one ever handed over
    for wxid in ("room1@chatroom", "gh_abcdef123456", "filehelper", "wangzong@openim"):
        bot.prm.new_friend_found({"wxid": wxid, "name": "谁", "how": MANUAL})
    assert bot.prm.register_found() == 0 and len(bot.prm.store.parties(include_inactive=True)) == 2


def test_a_friend_seen_again_after_a_restart_is_not_registered_twice(copilot_bot, db, clock):
    bot = copilot_bot(auto_register="on")
    bot.friends.scan_once()
    clock.now += 120
    accepted_on_pc(db, NEW, "阿花", clock.now)
    bot.poll()
    bot.friends.scan_once()
    assert bot.prm.register_found() == 1
    bot.poll()
    assert len(bot.jobs()) == 1
    bot.stop()
    clock.now += 600
    again = copilot_bot(auto_register="on", prm_store=bot.prm.store, relay_store=bot.relay.store)
    db.say(NEW, NEW, "你好？", clock.now)
    again.poll()
    assert again.friends.scan_once() == [] and again.prm.register_found() == 0      # reported once, ever
    again.prm.new_friend_found({"wxid": NEW, "name": "阿花", "how": MANUAL})       # and were she handed over again
    assert again.prm.register_found() == 0
    assert len(again.prm.store.parties(include_inactive=True)) == 1
    assert len(again.prm.store.events(None, kinds=("auto_register",))) == 1
    assert lines(again.jobs()[-1])[-1] == "你好？"                  # a creator's message (it joins the job not taken)


def test_a_new_friend_queued_when_the_bot_stopped_is_registered_after_the_restart(copilot_bot, db, clock):
    """Review 2026-10-30: the scan reports a new friend once, ever, and the queue lived in memory only: a restart in the
    seconds before the sender thread's round lost her.  It is kept in prm_meta and taken up again at start."""
    bot = copilot_bot(auto_register="on")
    bot.friends.scan_once()
    clock.now += 120
    accepted_on_pc(db, NEW, "阿花", clock.now)
    bot.poll()
    assert [e["wxid"] for e in bot.friends.scan_once()] == [NEW]      # queued; the bot stops before its round
    assert [e["wxid"] for e in json.loads(bot.prm.store.meta_value(AUTO_QUEUE))] == [NEW]
    bot.stop()
    clock.now += 60
    again = copilot_bot(auto_register="on", prm_store=bot.prm.store, relay_store=bot.relay.store)
    assert again.friends.scan_once() == []                            # reported once, ever
    assert again.prm.register_found() == 1 and again.prm.is_party_chat(NEW)
    assert json.loads(again.prm.store.meta_value(AUTO_QUEUE)) == []   # seen to: off the kept queue
    again.poll()
    [job] = again.jobs()
    assert json.loads(job["meta"])["new_friend"]["count"] == 1      # her greeting, drafted as for any new friend
    assert again.prm.resume_found() == 0 and again.prm.register_found() == 0


def test_an_error_in_register_never_stops_the_scan_and_she_stays_a_candidate(
        copilot_bot, db, clock, caplog, monkeypatch):
    from src.web import server

    bot = copilot_bot(auto_register="on")
    bot.friends.scan_once()
    clock.now += 120
    accepted_on_pc(db, NEW, "阿花", clock.now)
    bot.poll()
    real = bot.prm.store.add_party

    def locked(wxid, *args, **kwargs):
        if wxid == NEW:
            raise sqlite3.OperationalError("database is locked")
        return real(wxid, *args, **kwargs)

    monkeypatch.setattr(bot.prm.store, "add_party", locked)
    with caplog.at_level(logging.INFO):
        assert [e["wxid"] for e in bot.friends.scan_once()] == [NEW]
        bot.prm.tick()                                               # the round goes on (it would send, sweep ...)
    assert bot.prm.store.parties(include_inactive=True) == []
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "src.prm.service"]
    assert "could not be registered automatically" in warning.getMessage() and "阿花" not in warning.getMessage()
    # the scan goes on: the next new friend is found and registered
    clock.now += 60
    accepted_on_pc(db, CUSTOM, "小美美", clock.now)
    bot.poll()
    assert [e["wxid"] for e in bot.friends.scan_once()] == [CUSTOM]
    assert bot.prm.register_found() == 1 and bot.prm.is_party_chat(CUSTOM)
    # she stays in 登记达人's list, for a person to register
    server.register_new_friends(bot.friends)
    try:
        offered = [c["wxid"] for c in server._prm_candidates(bot.prm.snapshot()) if c.get("new")]
    finally:
        server.register_new_friends(None)
    assert offered == [NEW]
    monkeypatch.undo()
    assert bot.prm.register(NEW, "阿花")["catch_up"] is True


def test_with_both_threads_running_she_is_registered_and_nothing_hangs(copilot_bot, db, clock):
    """The real threads: the scan's (new-friends) and PRM's sender thread, beside the poll loop (this test)."""
    bot = copilot_bot(auto_register="on", interval=1, first_wait=0)
    bot.friends.start()
    bot.prm.start()
    scan_thread, prm_thread = bot.friends._thread, bot.prm._thread
    deadline = time.monotonic() + 15
    try:
        while bot.friends.store.started_at() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert bot.friends.store.started_at() is not None            # the first look: the start marker
        clock.now += 120
        accepted_on_phone(db, NEW, "阿花", clock.now)
        db.say(NEW, NEW, "在吗", clock.now + 10)
        clock.now += 30
        while not bot.jobs() and time.monotonic() < deadline:        # the poll loop, beside both threads: the
            within(10, bot.poll)                                     # sessions, then her chat read back once
            time.sleep(0.05)                                         # she is registered
        assert bot.prm.store.party_by_wxid(NEW)["auto_registered"] == 1
    finally:
        within(20, bot.friends.stop)
        within(20, bot.prm.stop)
    assert not scan_thread.is_alive() and not prm_thread.is_alive()
    [job] = bot.jobs()
    assert json.loads(job["meta"])["new_friend"]["count"] == 2


def test_her_name_note_and_record():
    from src.prm.service import auto_name, auto_note, event_out

    assert auto_name("  阿花 　 美妆\n号 ", NEW) == "阿花 美妆 号"            # whitespace collapsed
    assert auto_name("长" * 40, NEW) == "长" * 30
    assert auto_name("ㅤㅤ", NEW) == auto_name(None, NEW) == "新好友 wxid_new"    # no name (U+3164 only, or none)
    assert auto_name("", CUSTOM) == "新好友 meimei_8"
    assert [auto_note(h) for h in (REQUESTED, MANUAL, AUTO_ACCEPT, UNKNOWN, "")] == [
        "自动登记（新好友，我们加她）", "自动登记（新好友，她加我们）", "自动登记（新好友，机器人自动通过）",
        "自动登记（新好友，说不清）", "自动登记（新好友，说不清）"]
    ev = {"id": 1, "kind": "auto_register", "detail": {"how": AUTO_ACCEPT}, "actor": "system", "at": 0}
    assert event_out(ev)["name"] == "自动登记（新好友，机器人自动通过）"


def test_the_bot_hands_new_friends_to_prm_only_in_copilot_mode_with_the_switch_on(tmp_path, db, monkeypatch, caplog):
    from src.bot import Bot
    from src.web import server

    monkeypatch.chdir(tmp_path)
    backend = Backend(db)

    class Prm:
        on_registered = None

        def __init__(self, auto):
            self.auto_register = auto
            self.resumed = 0

        def take_new_friend(self, *a, **k):
            return 0

        def new_friend_found(self, event):
            pass

        def resume_found(self):
            self.resumed += 1
            return 0

    def sinks(config, prm, edge):
        service = Bot._start_new_friends(config, backend, prm, edge)
        try:
            return list(service._on_found)
        finally:
            service.stop()
            server.register_new_friends(None)

    on = BotConfig(db_path=str(tmp_path / "messages.db"), prm_auto_register="on")
    off = BotConfig(db_path=str(tmp_path / "messages.db"))
    prm, stays = Prm(True), Prm(False)
    assert sinks(on, prm, None) == [prm.new_friend_found]
    assert prm.resumed == 1                                          # the queue kept over a restart is taken up
    assert sinks(off, stays, None) == [] and stays.resumed == 0
    edge = type("E", (), {"friend_added": lambda self, e: None})()
    with caplog.at_level(logging.INFO):
        assert sinks(on, None, edge) == [edge.friend_added]          # edge mode: the Mac's friend_added only
    assert any("PRM_AUTO_REGISTER is on but this bot runs no PRM copilot" in r.getMessage() for r in caplog.records)


# ── 开场白: a creator who has not written gets one reviewed draft of our first line (2026-10-30) ──

def test_a_friend_who_accepted_our_request_and_said_nothing_gets_one_reviewed_opening(copilot_bot, db, clock):
    """Real machine, 2026-10-30: she accepted our friend request, said nothing, and 待审批 stayed empty (WeChat's notice
    is all her chat had, and it is never drafted).  Registering her now asks the Mac for our first line: a job with no
    message of hers, a draft that waits for a person whatever its kind's gate, no holding reply, tagged for the console."""
    bot = copilot_bot()
    bot.friends.scan_once()                                          # the start marker
    clock.now += 120
    t = clock.now
    they_accepted(db, ASKED, t)                                      # our greeting, her accept, 以上是打招呼的消息
    clock.now = t + 60
    bot.poll()
    assert [e["how"] for e in bot.friends.scan_once()] == [REQUESTED]
    party = bot.prm.register(ASKED, "小青")
    assert party["catch_up"] is True
    bot.poll()
    [job] = bot.jobs()
    meta = json.loads(job["meta"])
    assert job["text"] == "" and meta["opening"] == {"how": REQUESTED, "by": "new_friend"}
    assert "new_friend" not in meta and "night" not in meta
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    [event] = bot.prm.store.events(deal_id, kinds=("opening",))
    assert (event["detail"], event["actor"]) == ({"job": job["id"], "how": REQUESTED, "by": "new_friend"}, "system")

    bot.prm.store.set_gate("greeting", gates.AUTO, clock.now)        # even a kind that has earned "auto" waits
    bot.mac_drafts(kind="greeting")
    [packet] = bot.prm.store.packets(("pending", "approved"))
    assert (packet["status"], packet["route"], packet["meta"].get("opening")) == ("pending", "review", True)
    assert bot.prm.store.deal(deal_id)["attention"].startswith(opening_note({"how": REQUESTED}))
    assert opening_note({"how": REQUESTED}) == "她通过了我们的好友申请：这份是我们先打招呼的开场白，要人看过再发"
    [shown] = bot.prm.snapshot()["pending"]
    assert shown["opening"] is True
    clock.now += 3600                                                # an hour in 待审批: no 「收到，我确认一下…」 to her
    assert bot.prm.send_holding_replies() == 0 and bot.sent == []
    assert bot.prm.decide(packet["id"], "send")["ok"] and bot.prm.send_approved() == 1
    assert bot.sent == [(ASKED, "你好阿花～欢迎")]
    bot.poll()
    bot.poll()
    assert len(bot.jobs()) == 1                                      # asked for once


def test_a_friend_registered_long_after_she_was_added_gets_no_opening_by_itself_but_a_notice(copilot_bot, db, clock):
    """Review 2026-10-30: registered more than PRM_CATCHUP_HOURS after she was added, the read-back does not reach back
    to her first lines, so nothing of hers in it does not mean she has not written.  No 开场白 by itself: a notice asks a
    person to look, and goes when one is asked for."""
    bot = copilot_bot(catchup_hours=1)
    bot.friends.scan_once()
    clock.now += 120
    t = clock.now
    they_accepted(db, ASKED, t)
    clock.now = t + 60
    bot.poll()
    assert [e["how"] for e in bot.friends.scan_once()] == [REQUESTED]
    clock.now = t + 2 * 3600                                         # registered two hours on (the window is one)
    party = bot.prm.register(ASKED, "小青")
    assert party["catch_up"] is True
    bot.poll()
    assert bot.jobs() == []
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    assert bot.prm.store.deal(deal_id)["attention"] == OPENING_UNSURE_TEXT
    asked = bot.prm.deal_action("opening", {"deal_id": deal_id})     # the operator looked: she has not written
    assert asked["ok"] is True and bot.prm.store.deal(deal_id)["attention"] == ""
    assert json.loads(bot.jobs()[-1]["meta"])["opening"] == {"how": "", "by": "console"}


def test_the_look_at_her_chat_notice_goes_when_an_operator_writes_to_her_or_she_writes(copilot_bot, db, clock):
    for who in ("operator", "creator"):
        bot = copilot_bot(catchup_hours=1)
        bot.friends.scan_once()
        clock.now += 120
        wxid = f"{ASKED}_{who}"
        db.names[wxid] = "品牌小青"                                   # (the read-back reads her chat under its name)
        they_accepted(db, wxid, clock.now)
        clock.now += 60
        bot.poll()
        bot.friends.scan_once()
        clock.now += 2 * 3600
        party = bot.prm.register(wxid, "小青")
        bot.poll()
        deal_id = bot.prm.store.deal_of(party["id"])["id"]
        assert bot.prm.store.deal(deal_id)["attention"] == OPENING_UNSURE_TEXT
        clock.now += 60
        if who == "operator":
            db.say(wxid, ME_SENDER, "你好呀～我是 ACE7CN 的运营", clock.now)   # on the phone: a person greeted her
            bot.poll()
        else:
            db.say(wxid, wxid, "你好，在吗", clock.now)
            bot.poll()
            bot.mac_drafts()
        assert OPENING_UNSURE_TEXT not in bot.prm.store.deal(deal_id)["attention"], who
        bot.stop()


def test_with_auto_register_at_night_her_opening_still_waits_for_a_person(copilot_bot, db, clock):
    hour = time.localtime(clock.now).tm_hour
    bot = copilot_bot(auto_register="on", night=f"{hour:02d}:00-{(hour + 2) % 24:02d}:00")
    bot.friends.scan_once()
    clock.now += 120
    they_accepted(db, ASKED, clock.now)
    clock.now += 60
    bot.poll()
    bot.friends.scan_once()
    assert bot.prm.register_found() == 1
    bot.poll()
    [job] = bot.jobs()
    meta = json.loads(job["meta"])
    assert meta["opening"] == {"how": REQUESTED, "by": "new_friend"} and "night" not in meta   # never told it is night
    bot.mac_drafts(kind="greeting")
    [packet] = bot.prm.store.packets(("pending", "approved"))
    assert (packet["status"], packet["route"]) == ("pending", "review") and bot.sent == []


def test_her_message_joins_the_opening_job_not_taken_yet_and_then_gets_the_holding_reply(copilot_bot, db, clock):
    bot = copilot_bot()
    bot.friends.scan_once()
    clock.now += 120
    they_accepted(db, ASKED, clock.now)
    clock.now += 60
    bot.poll()
    bot.friends.scan_once()
    bot.prm.register(ASKED, "小青")
    bot.poll()
    db.say(ASKED, ASKED, "在吗", clock.now)                           # before the Mac took the job
    bot.poll()
    [job] = bot.jobs()
    assert lines(job) == ["在吗"] and "opening" in json.loads(job["meta"])
    bot.mac_drafts(kind="greeting")
    [packet] = bot.prm.store.packets(("pending",))
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1                      # she wrote: a round of hers, as for any draft


def test_her_message_outdates_a_waiting_opening_and_her_job_hears_it_never_went_out(copilot_bot, db, clock):
    bot = copilot_bot()
    bot.friends.scan_once()
    clock.now += 120
    they_accepted(db, ASKED, clock.now)
    clock.now += 60
    bot.poll()
    bot.friends.scan_once()
    bot.prm.register(ASKED, "小青")
    bot.poll()
    [opening] = bot.jobs()
    bot.mac_drafts(kind="greeting")
    [packet] = bot.prm.store.packets(("pending",))
    db.say(ASKED, ASKED, "你好，你们是什么品牌", clock.now)
    bot.poll()
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    reply = bot.jobs()[-1]
    assert reply["id"] != opening["id"] and lines(reply) == ["你好，你们是什么品牌"]      # nothing put before her line
    meta = json.loads(reply["meta"])
    assert "opening" not in meta
    assert [(s["what"], s["text"]) for s in meta["sent"]] == [("failed", "你好阿花～欢迎")]   # 「没发出去，达人没看到」


def test_an_opening_the_mac_did_not_write_says_so_until_one_is_asked_for_again(copilot_bot, db, clock):
    bot = copilot_bot()
    bot.friends.scan_once()
    clock.now += 120
    they_accepted(db, ASKED, clock.now)
    clock.now += 60
    bot.poll()
    bot.friends.scan_once()
    party = bot.prm.register(ASKED, "小青")
    bot.poll()
    [job] = bot.jobs()
    clock.now += SETTLE_SEC + 1
    taken = bot.relay.next_job(0, ("prm",))
    bot.relay.finish(taken["id"], {"ok": False, "text": "", "error": "claude 没有给出结构化结果"})
    bot.relay.tick()
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    attention = bot.prm.store.deal(deal_id)["attention"]
    assert attention.startswith(f"#{job['id']} 开场白没写成：claude 没有给出结构化结果")
    assert "可以在合作单里点「写开场白」再写一次，或者直接在微信里跟她打招呼" in attention
    assert "她这几条消息" not in attention                          # she has said nothing to take along
    asked = bot.prm.deal_action("opening", {"deal_id": deal_id})
    assert asked["ok"] is True and asked["job"] == bot.jobs()[-1]["id"]
    assert bot.prm.store.deal(deal_id)["attention"] == ""
    assert json.loads(bot.jobs()[-1]["meta"])["opening"] == {"how": "", "by": "console"}


def test_the_console_asks_for_an_opening_only_for_a_creator_who_has_not_written(copilot_bot, db, clock):
    from src.prm.service import event_out

    bot = copilot_bot()
    bot.friends.scan_once()
    party = bot.prm.register(OLD, "老朋友")                         # no new friend: nothing asked for by itself
    bot.poll()
    assert bot.jobs() == []
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    asked = bot.prm.deal_action("opening", {"deal_id": deal_id})
    assert asked["ok"] is True
    [job] = bot.jobs()
    assert job["id"] == asked["job"] and job["text"] == ""
    assert json.loads(job["meta"])["opening"] == {"how": "", "by": "console"}
    [event] = bot.prm.store.events(deal_id, kinds=("opening",))
    assert event["actor"] == "console" and event_out(event)["name"] == f"派写开场白的任务 #{job['id']}（运营点的）"
    assert bot.prm.deal_action("opening", {"deal_id": deal_id}) == {
        "ok": False, "error": "她的草稿正在写，写好会出现在「待审批」里"}
    bot.mac_drafts(kind="greeting")
    [packet] = bot.prm.store.packets(("pending",))
    assert bot.prm.store.deal(deal_id)["attention"].startswith("运营点了「写开场白」：这份是我们先打招呼的话")
    assert bot.prm.deal_action("opening", {"deal_id": deal_id})["error"] == "「待审批」里已经有她的草稿"
    assert bot.prm.decide(packet["id"], "drop")["ok"]               # 不发: another may be asked for
    bot.prm.deal_action("mode", {"deal_id": deal_id, "mode": "human"})
    assert "不在副驾驶模式" in bot.prm.deal_action("opening", {"deal_id": deal_id})["error"]
    bot.prm.deal_action("mode", {"deal_id": deal_id, "mode": "copilot"})
    bot.prm.set_active(party["id"], False)
    assert "已经停用" in bot.prm.deal_action("opening", {"deal_id": deal_id})["error"]
    bot.prm.set_active(party["id"], True)
    db.say(OLD, OLD, "你好", clock.now)                              # she writes: her messages are drafted instead
    bot.poll()
    assert bot.prm.deal_action("opening", {"deal_id": deal_id})["error"] == "她的草稿正在写，写好会出现在「待审批」里"
    bot.mac_drafts()
    [answer] = bot.prm.store.packets(("pending",))
    assert bot.prm.decide(answer["id"], "send")["ok"] and bot.prm.send_approved() == 1     # she had a real line from us
    assert bot.prm.deal_action("opening", {"deal_id": deal_id})["error"] == "她已经发过消息了：她的消息会自己起草，不用开场白"
    assert bot.prm.deal_action("opening", {"deal_id": 999}) == {"ok": False, "error": "没有找到这个合作单"}
    assert bot.prm.deal_action("opening", {"deal_id": "x"}) == {"ok": False, "error": "没有找到这个合作单"}
    assert [json.loads(j["meta"]).get("opening") is not None for j in bot.jobs()] == [True, False]


def test_a_creator_who_wrote_but_never_had_a_real_line_from_us_can_be_given_an_opening(copilot_bot, db, clock):
    """Real machine, 2026-11-02: she added us with 「我是…」 (her only line), its draft was dropped and only the night 稍等
    went out; 「写开场白」 was hidden and refused.  Now the console offers it (``unanswered``: not while a draft of hers
    waits) and it is an opening job that says she wrote (``wrote``), told what she got from us; once a draft of ours
    went out it is refused again, and so it is after an operator wrote to her by hand."""
    bot = copilot_bot()
    bot.friends.scan_once()
    party = bot.prm.register(OLD, "老朋友")
    bot.poll()
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    db.say(OLD, OLD, "我是阿花的助理", clock.now)                      # her one line
    bot.poll()
    bot.mac_drafts(kind="greeting")
    [packet] = bot.prm.store.packets(("pending",))
    deal = lambda: next(p for p in bot.prm.snapshot()["parties"] if p["id"] == party["id"])["deal"]
    assert deal()["unanswered"] is False                             # her draft waits: answer that one
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1                      # the bot's own 稍等: no answer
    clock.now += 60
    assert bot.prm.decide(packet["id"], "drop")["ok"]               # 不发
    assert deal()["last_message_at"] and deal()["unanswered"] is True
    asked = bot.prm.deal_action("opening", {"deal_id": deal_id})
    assert asked["ok"] is True
    job = bot.jobs()[-1]
    meta = json.loads(job["meta"])
    assert (job["id"], job["text"]) == (asked["job"], "")
    assert meta["opening"] == {"how": "", "by": "console", "wrote": True}
    assert [(s["what"], s["kind"]) for s in meta["sent"]] == [("sent", "holding"), ("dropped", "greeting")]
    bot.mac_drafts(kind="greeting")
    [opening] = bot.prm.store.packets(("pending",))
    assert (opening["route"], opening["meta"].get("opening")) == ("review", True)
    assert deal()["unanswered"] is False                             # a draft of hers waits in 待审批
    assert bot.prm.decide(opening["id"], "send")["ok"] and bot.prm.send_approved() == 1
    assert deal()["unanswered"] is False
    assert bot.prm.deal_action("opening", {"deal_id": deal_id})["error"] == "她已经发过消息了：她的消息会自己起草，不用开场白"
    # an operator's line by hand is an answer too
    other = bot.prm.register(ASKED, "小青")
    bot.poll()
    other_deal = bot.prm.store.deal_of(other["id"])["id"]
    db.say(ASKED, ASKED, "在吗", clock.now)
    bot.poll()
    bot.mac_drafts()
    [waiting] = bot.prm.store.packets(("pending",))
    assert bot.prm.decide(waiting["id"], "drop")["ok"]
    assert bot.prm.store.has_answer(other_deal) is False
    db.say(ASKED, ME_SENDER, "在的在的", clock.now)                   # an operator, on the phone
    bot.poll()
    assert bot.prm.store.has_answer(other_deal) is True


def test_an_opening_asked_for_after_an_operator_greeted_her_by_hand_hears_of_it(copilot_bot, db, clock):
    """An operator greeted her by hand (a takeover); once it is over, 「写开场白」's job is told what was said
    (``meta.history``: the Mac writes a follow-up, not a second introduction), and the line is not used up: her next
    draft job hears of it too."""
    bot = copilot_bot()
    bot.friends.scan_once()
    party = bot.prm.register(OLD, "老朋友")
    bot.poll()
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    db.say(OLD, ME_SENDER, "你好呀～我是 ACE7CN 的", clock.now)        # an operator, on the phone
    bot.poll()
    assert "不在副驾驶模式" in bot.prm.deal_action("opening", {"deal_id": deal_id})["error"]
    clock.now += 3 * 3600                                            # the takeover is over
    assert bot.prm.deal_action("opening", {"deal_id": deal_id})["ok"] is True
    [job] = bot.jobs()
    told = json.loads(job["meta"])["history"]
    assert [(h["who"], h["text"].split("] ", 1)[1]) for h in told] == [("operator", "你好呀～我是 ACE7CN 的")]
    bot.mac_drafts(kind="greeting")
    db.say(OLD, OLD, "你好", clock.now)                              # she answers before it went out
    bot.poll()
    later = json.loads(bot.jobs()[-1]["meta"])
    assert [h["who"] for h in later["history"]] == ["operator"] and "opening" not in later


# ── 加百应达人微信 (功能 R): who accepted which request, and linking her ─────────
# The real WCDB backend over the fake database; WeChat writes what the probe saw (they_accepted): our greeting,
# her 「我通过了你的朋友验证请求」, 「以上是打招呼的消息」.  Every value is made up.

def test_our_greeting_in_the_new_chat_says_which_request_she_accepted_and_linking_drafts_her_words(copilot_bot, db,
                                                                                                   clock):
    bot = copilot_bot(friend_add=True)
    bot.friends.scan_once()                                          # the start marker
    clock.now += 120
    adds = bot.prm.friend_adds
    first = adds.paste("dy_demo01", "小鹿", {"wechat": "abcdefyz"})["id"]
    adds.confirm(first)
    clock.now += 121
    second = adds.paste("dy_demo02", "小青", {"wechat": "abcdefgh"})["id"]
    adds.confirm(second)
    assert [g for _, g in bot.added] == ["您好小鹿，百应上联系您的～", "您好小青，百应上联系您的～"]
    t = clock.now + 600
    they_accepted(db, NEW, t, our_greeting="您好小鹿，百应上联系您的～")   # our greeting at t-60
    db.say(NEW, NEW, "你好呀，样品还有吗", t + 30)
    clock.now = t + 60
    bot.poll()                                                       # her chat: read, stored, silence
    [found] = bot.friends.scan_once()
    assert (found["how"], found["own_greeting"]) == (REQUESTED, "您好小鹿，百应上联系您的～")
    record = adds._store.friend_add(first)
    assert record["candidate"]["level"] == "sure" and record["candidate"]["wxid"] == NEW
    assert adds._store.friend_add(second)["candidate"] == {} and bot.prm.store.parties() == []   # nobody linked
    assert bot.jobs() == [] and bot.sent == []
    assert bot.friends.store.get(NEW)["own_greeting"] == "您好小鹿，百应上联系您的～"

    out = adds.link(first, NEW)                                      # an operator's 「是她，登记并关联」
    assert out["catch_up"] is True
    bot.poll()
    [job] = bot.jobs()
    meta = json.loads(job["meta"])
    assert meta["creator"] == {"douyin_ids": ["dy_demo01"], "uid": ""} and meta["new_friend"]["count"] == 1
    assert lines(job) == ["你好呀，样品还有吗"]                         # hers only: no notice, not our greeting
    deal = bot.prm.store.deal_of(out["party_id"])
    assert deal["mode"] == "copilot"                                 # our greeting took nothing over
    assert [e["kind"] for e in bot.prm.store.events(deal["id"])] == ["friend_add_link"]


def test_her_name_on_our_side_is_the_remark_of_our_request_and_says_which_one_she_accepted(copilot_bot, db, clock):
    """The remark our request carried (「抖音-〈百应昵称〉」) is the name WeChat gives her once she accepted: the scan
    reads it as her display name (wcdb_get_display_names), and with two requests waiting and our greeting not in the
    new chat it alone says which record she is.  Nobody is registered by it."""
    bot = copilot_bot(friend_add=True)
    bot.friends.scan_once()                                          # the start marker
    clock.now += 120
    adds = bot.prm.friend_adds
    first = adds.paste("dy_demo01", "小鹿", {"wechat": "abcdefyz"})["id"]
    adds.confirm(first)
    clock.now += 121
    second = adds.paste("dy_demo02", "小青", {"wechat": "abcdefgh"})["id"]
    adds.confirm(second)
    assert bot.remarks == ["抖音-小鹿", "抖音-小青"]
    t = clock.now + 600
    db.names[NEW] = "抖音-小青"                                       # WeChat shows her by the remark we typed
    db.say(NEW, NEW, PASSED_LINE, t)                                 # she accepted; our greeting is not in the chat
    db.system(NEW, "以上是打招呼的消息", t + 1)
    clock.now = t + 60
    bot.poll()
    [found] = bot.friends.scan_once()
    assert (found["how"], found["name"], found["own_greeting"]) == (REQUESTED, "抖音-小青", "")
    candidate = adds._store.friend_add(second)["candidate"]
    assert (candidate["wxid"], candidate["level"]) == (NEW, "sure")
    assert candidate["evidence"] == ["她在我们这边显示的名字就是加她时写的备注"]
    assert adds._store.friend_add(first)["candidate"] == {} and bot.prm.store.parties() == []


def test_she_adds_us_naming_baiying_and_is_offered_for_the_record_waiting_for_her(copilot_bot, db, clock):
    bot = copilot_bot(friend_add=True)
    bot.friends.scan_once()
    clock.now += 120
    add_id = bot.prm.friend_adds.paste("dy_demo01", "小鹿", {})["id"]           # no number: she adds us
    accepted_on_pc(db, NEW, "鹿鹿", clock.now, greeting="你好，我是百应上的小鹿")
    clock.now += 30
    bot.poll()
    bot.friends.scan_once()
    candidate = bot.prm.friend_adds._store.friend_add(add_id)["candidate"]
    assert (candidate["wxid"], candidate["level"], candidate["how"]) == (NEW, "possible", MANUAL)
    assert bot.prm.store.parties() == []


def test_an_old_database_gets_the_own_greeting_column(tmp_path):
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE new_friends (wxid TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL"
                 " DEFAULT '', how TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', added_at REAL NOT NULL, verify_from REAL,"
                 " verify_text TEXT NOT NULL DEFAULT '', found_at REAL NOT NULL)")
    conn.execute("INSERT INTO new_friends (wxid, how, added_at, found_at) VALUES ('wxid_before', 'manual', 1, 1)")
    conn.commit()
    conn.close()
    store = NewFriendStore(str(path))
    assert store.get("wxid_before")["own_greeting"] == ""
    assert store.add(wxid=NEW, name="阿花", remark="", how=REQUESTED, kind="requested", added_at=2.0, verify_from=None,
                     verify_text="", found_at=2.0, own_greeting="您好阿花")
    assert store.get(NEW)["own_greeting"] == "您好阿花"
    assert store.forget_verify(3.0) == 1 and store.get(NEW)["own_greeting"] == ""
    store.close()

