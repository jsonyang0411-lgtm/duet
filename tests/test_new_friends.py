"""New friends (src/wechat/new_friends.py): found in WeChat's database beside the poll loop, reported
once each, told apart by how they came; WeChat's friend notices are nobody's words; in copilot mode
registering a new friend drafts her messages since she was added (one reviewed job).

The WeChat database is a fake shaped like the real one (WeChat 4.1.15.36, tools/friend_probe.py,
2026-10-27): session rows carry no names, messages newest first, system lines type 10000, this
account's own lines from its wxid without the data folder's suffix.
"""

import json
import logging
import threading
import time

import pytest

from router_support import ADMIN, BOT, TraceSpy

from src.admin import AdminCommandHandler
from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.edge import protocol as P
from src.edge.service import EdgeService
from src.edge.store import EdgeStore
from src.prm import gates
from src.prm.service import SETTLE_SEC, PrmService, new_friend_note
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
    accepted_on_pc(db, NEW, "阿花", clock.now)
    accepted_on_phone(db, CUSTOM, "小美美", clock.now + 1, greeting="想合作，加个微信")
    they_accepted(db, ASKED, clock.now + 2)
    found = {e["wxid"]: e for e in service.scan_once()}
    assert found == {
        NEW: {"wxid": NEW, "name": "阿花", "remark": "", "verify_text": GREETING, "how": MANUAL},
        CUSTOM: {"wxid": CUSTOM, "name": "小美美", "remark": "", "verify_text": "想合作，加个微信", "how": MANUAL},
        ASKED: {"wxid": ASKED, "name": "品牌小青", "remark": "", "verify_text": "", "how": REQUESTED},
    }
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
    router, the relay, PRM and the new-friend scan (the bot's database in tmp_path)."""

    def __init__(self, tmp_path, clock, db, private_chats="*", catchup_hours=12, prm_store=None, relay_store=None):
        config = BotConfig(
            private_chat_enabled=True, private_chats=private_chats, admin_wxid=ADMIN, bot_display_name=BOT,
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_db=":memory:", prm_copilot="on", prm_db=":memory:", prm_catchup_hours=catchup_hours)
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
        self.friends = NewFriendService(self.backend, NewFriendStore(str(tmp_path / "messages.db")),
                                        keep_verify=True, catchup_hours=catchup_hours, clock=clock, pause=0)
        self.friends.set_catch_up_sink(self.prm.take_new_friend)
        self.prm.on_registered = self.friends.registered

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
    edge = type("E", (), {"friend_added": lambda self, e: None})()
    service = Bot._start_new_friends(config, backend, None, edge)
    try:
        assert service._keep_verify is False and len(service._on_found) == 1
    finally:
        service.stop()
        from src.web import server
        server.register_new_friends(None)
