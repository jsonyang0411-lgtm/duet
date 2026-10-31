"""老板拉的群 (功能 U, the backend's part): a boss (PRM_GROUP_WITH) makes a small WeChat group of himself, a creator who is
his friend and this account, and PRM takes it over.  The WCDB backend finds the bosses' wxids by name (``contact_ids``),
the new small groups a boss is in (``boss_groups``: with whether their whole history was read and when this account
came in, by WeChat's line; one whose members or lines cannot be read now is given as ``unreadable``) and their members
(``group_members``), tells WeChat's clock (``wechat_now``: the newest session time), and says quietly whether she has
a private chat with us (``has_private_chat``); a watched group (``watch_group``) is read whatever WECHAT_GROUPS says and
its lines go through the route as group messages, one by one in order in the poll thread (its catch-up once, oldest
first, the lines at ``since`` or later; ``watch_caught_up`` once it ran; after a restart and a re-opened database only
what was not handed on yet), this account's own lines there to the own-message hook; a send reaches a group by the
name WeChat gives it, like any chat, a name another chat has too is refused, and so is a search that opened a private
chat the bot knows (its title without a member count, SEND_VERIFY_CHAT: ``NOT_A_GROUP``); a title the bot does not
know, or one whose count OCR cut off, is sent to as before.  Every other group is read and kept silent as before.

The real WcdbBackend over the fake WeChat 4 database of tests/test_new_friends.py (session rows without names, names
from get_display_names, messages newest first, this account's own lines from its wxid without the data folder's suffix;
group members added here), a real router and message store (the route stores every line it gets: step persist); the
own-message hook and the window write down what they got.  Every name and id is made up.
"""

import json
import logging
import threading
import time

import pytest

from router_support import ADMIN, BOT, TraceSpy
from test_new_friends import ME, ME_SENDER, Clock, WeChatDb

from src.admin import AdminCommandHandler
from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.router import MessageRouter
from src.wechat import wcdb_backend
from src.wechat.wcdb_backend import (CATCHUP_READ, GROUP_NO_NAME, JOINED_LINE, NOT_A_GROUP, VERIFY_LOOKS, WRONG_CHAT,
                                     WcdbBackend, _ocr_key, title_verdict)
from src.wechat.wcdb_client import WcdbCallError
from src.wechat.window_controller import SendRefused

BOSS, BOSS_NAME = "wxid_boss0000000001", "老板测试"
HER, HER_NAME = "wxid_xiaomei0000001", "小美"
TESTER, TESTER_NAME = "wxid_tester00000001", "测试达人"
COLLEAGUE = "wxid_colleague000001"
STRANGER = "wxid_stranger000001"
OWN_NAME = "商务号测试"
OLD_GROUP = "1000000001@chatroom"           # there when the feature started: the baseline
NOTICE_GROUP = "1000000002@chatroom"        # the group WECHAT_GROUPS names
NOTICE_NAME = "公司通知群"
GROUP = "1000000003@chatroom"               # the boss's new group with her
GROUP_NAME = "小美合作群"
REVOKE_XML = ('<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg><content>"小美" 撤回了一条消息</content>'
              '<revoketime>0</revoketime></revokemsg></sysmsg>')


class GroupDb(WeChatDb):
    """tests/test_new_friends.py's database with group members: ``members`` per group, ``rows`` = more fields of a
    member's row (wcdb_get_group_members gives ``username``); ``fail`` = nothing can be read."""

    def __init__(self, clock):
        super().__init__(clock)
        self.members: dict[str, list[str]] = {}
        self.rows: dict[str, dict] = {}

    def group(self, chat, name, members, at):
        """A group WeChat lists: its name ("" = nobody named it), its members, WeChat's line that we were added."""
        if name:
            self.names[chat] = name
        self.members[chat] = list(members)
        self.system(chat, f'"{BOSS_NAME}"邀请你加入了群聊', at)

    def line(self, chat, sender, text, at):
        """A line in a group: WeChat 4 writes ``<sender>:\\n`` before a member's words, nothing before ours."""
        self.say(chat, sender, text if sender == ME_SENDER else f"{sender}:\n{text}", at)

    def get_group_members(self, chat):
        if self.fail:
            raise RuntimeError("the database went away")
        return [{"username": wxid, **self.rows.get(wxid, {})} for wxid in self.members.get(chat, [])]


class Window:
    """The WeChat window as the backend drives it: the search for a name opens a chat, ``check`` reads its title."""

    def __init__(self):
        self.typed = []
        self.shots = []

    def send_to_chat(self, name, text, max_retries=2, check=None):
        refused = check(100) if check is not None else None
        if refused is not None:
            return refused
        self.typed.append((name, text))
        return True

    def send_file_to_chat(self, name, path, max_retries=2, check=None):
        return self.send_to_chat(name, f"<文件 {path}>", max_retries, check)

    def capture_chat_media(self, name, check=None):
        if check is not None and check(100) is not None:
            return ""
        self.shots.append(name)
        return "shot.png"

    def find_hwnd(self, *args, **kwargs):
        return None


class Run:
    """One run of the bot (copilot mode: PRIVATE_CHAT_ENABLED, the admin's chat) as Bot.run wires the backend: the
    real backend, router and message store over the fake database; what reached the route, the own-message hook and
    the window is written down."""

    def __init__(self, tmp_path, db, groups=(NOTICE_NAME,), messages=None):
        config = BotConfig(private_chat_enabled=True, private_chats=ADMIN, admin_wxid=ADMIN, bot_display_name=BOT,
                           prm_copilot="on")
        self.messages = messages or MessageStore(initialize_db(str(tmp_path / "messages.db")))
        self.router = MessageRouter(store=self.messages, admin_handler=AdminCommandHandler(), config=config,
                                    tracer=TraceSpy())
        self.backend = WcdbBackend(bot_display_name=BOT, groups=list(groups), poll_sec=0, store=self.messages,
                                   config=config)
        self.backend._client = db
        self.routed, self.own, self.typed = [], [], []
        self.backend.set_own_message_hook(self.own.append)
        self.backend._send_and_confirm = lambda name, chat, text: self.typed.append((name, text)) or True

    def start(self):
        self.backend._resolve_groups()
        self.backend._prime_known_ids()
        self.backend._running = True
        return self

    def route(self, msg):
        self.routed.append(dict(msg))
        return self.router.handle(msg)

    def poll(self):
        """One cycle of the poll loop, the bursts still held handed on too."""
        self.backend._poll_cycle(self.route)
        for msg in self.backend._burst.flush_all():
            self.backend._dispatch_message(msg, self.route)

    def lines(self, chat=GROUP):
        """What reached the route from a chat: (sender, words; WeChat's line: its text)."""
        return [(m["sender_id"], m["special"].get("title") if m["msg_type"] == 10000 else m["content"])
                for m in self.routed if m["chat_id"] == chat]

    def stored(self, chat=GROUP):
        return self.messages.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat,)).fetchone()[0]

    def stop(self):
        self.backend._running = False


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(clock):
    d = GroupDb(clock)
    d.names.update({BOSS: BOSS_NAME, HER: HER_NAME, TESTER: TESTER_NAME, COLLEAGUE: "同事测试", ME_SENDER: OWN_NAME})
    d.group(OLD_GROUP, "公司老群", [BOSS, COLLEAGUE, ME_SENDER], clock.now - 30 * 86400)
    d.group(NOTICE_GROUP, NOTICE_NAME, [BOSS, COLLEAGUE, TESTER, ME_SENDER], clock.now - 20 * 86400)
    return d


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # data/group_names.json, data/group_members.json …
    monkeypatch.setattr(wcdb_backend, "VERIFY_PAUSE_SEC", 0)


# ── contact_ids: a boss's name -> the one wxid with it ───────────────────────

def test_contact_ids_gives_the_one_wxid_with_each_name(tmp_path, db, clock):
    db.say(HER, HER, "你好", clock.now - 3600)                           # her private chat
    run = Run(tmp_path, db)
    assert run.backend.contact_ids([BOSS_NAME, " 老板 测试 ", HER_NAME, "没有这个人", ""]) == {
        BOSS_NAME: BOSS, " 老板 测试 ": BOSS, HER_NAME: HER, "没有这个人": None, "": None}
    # (the boss has no private chat with us: he is found in the groups he is in with us)
    db.names[TESTER] = "Tester Li"
    assert run.backend.contact_ids(["testerli"]) == {"testerli": TESTER}       # no spaces, no case


def test_two_people_with_the_name_are_nobody(tmp_path, db, clock):
    """Never a guess: a second person of the boss's name anywhere the database shows (a private chat, a member of
    any group, the console's list of private chats) makes it None."""
    run = Run(tmp_path, db)
    assert run.backend.contact_ids([BOSS_NAME]) == {BOSS_NAME: BOSS}
    db.members[NOTICE_GROUP].append(STRANGER)
    db.names[STRANGER] = "老板测试 "
    assert run.backend.contact_ids([BOSS_NAME]) == {BOSS_NAME: None}
    db.members[NOTICE_GROUP].remove(STRANGER)
    db.say(STRANGER, STRANGER, "在吗", clock.now)                        # a private chat of the name
    assert run.backend.contact_ids([BOSS_NAME]) == {BOSS_NAME: None}
    db.times.pop(STRANGER)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "private_chat_names.json").write_text('{"%s": "老板测试"}' % STRANGER, encoding="utf-8")
    assert run.backend.contact_ids([BOSS_NAME]) == {BOSS_NAME: None}


def test_this_account_is_never_a_contact(tmp_path, db):
    run = Run(tmp_path, db)
    assert run.backend.contact_ids([OWN_NAME]) == {OWN_NAME: None}        # (a member of every group)


def test_contact_ids_while_the_database_cannot_be_read_are_none(tmp_path, db):
    run = Run(tmp_path, db)
    db.fail = True
    assert run.backend.contact_ids([BOSS_NAME, HER_NAME]) == {BOSS_NAME: None, HER_NAME: None}
    db.fail = False
    run.backend._client = None
    assert run.backend.contact_ids([BOSS_NAME]) == {BOSS_NAME: None}


# ── boss_groups / group_members ─────────────────────────────────────────────

def test_boss_groups_are_the_new_small_groups_a_boss_is_in(tmp_path, db, clock):
    run = Run(tmp_path, db)
    baseline = run.backend.group_ids()
    assert baseline == {OLD_GROUP, NOTICE_GROUP}
    assert run.backend.boss_groups({BOSS}, baseline, 6) == []            # what was there before is never new
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 600)
    db.line(GROUP, BOSS, "给大家介绍一下，这是小美", t - 590)
    db.group("1000000004@chatroom", "没有老板的群", [HER, TESTER, ME_SENDER], t - 500)
    big = "1000000005@chatroom"
    db.group(big, "老板的大群", [BOSS, ME_SENDER] + [f"wxid_member{i:010d}" for i in range(5)], t - 400)
    assert run.backend.boss_groups({BOSS}, baseline, 6) == [{
        "chat": GROUP, "name": GROUP_NAME, "created_at": float(int(t - 600)),
        "history_complete": True, "joined_at": float(int(t - 600)),     # WeChat's 「邀请你加入了群聊」 line
        "members": [{"wxid": BOSS, "name": BOSS_NAME}, {"wxid": HER, "name": HER_NAME},
                    {"wxid": ME, "name": OWN_NAME}]}]                    # this account: under own_wxid()
    assert run.backend.own_wxid() == ME and ME != ME_SENDER
    assert [g["chat"] for g in run.backend.boss_groups({BOSS}, baseline, 7)] == [big, GROUP]   # begun last first
    assert run.backend.boss_groups({BOSS}, baseline | {GROUP, big}, 20) == []      # seen to already
    assert run.backend.boss_groups({"wxid_nobody00000001"}, baseline, 20) == []
    assert run.backend.boss_groups(set(), baseline, 20) == []


def test_a_members_name_is_the_one_this_account_sees_else_her_group_nickname(tmp_path, db, clock):
    nameless = "wxid_nameless000001"
    db.group(GROUP, "", [BOSS, STRANGER, nameless, ME_SENDER], clock.now)
    db.rows[STRANGER] = {"displayName": "群里的小美"}                  # not a contact: only her member row names her
    run = Run(tmp_path, db)
    assert run.backend.group_members(GROUP) == [
        {"wxid": BOSS, "name": BOSS_NAME}, {"wxid": STRANGER, "name": "群里的小美"},
        {"wxid": nameless, "name": ""}, {"wxid": ME, "name": OWN_NAME}]
    db.names[STRANGER] = "小美（备注）"                                  # a contact now, with a remark
    assert run.backend.group_members(GROUP)[1] == {"wxid": STRANGER, "name": "小美（备注）"}
    [found] = run.backend.boss_groups({BOSS}, {OLD_GROUP, NOTICE_GROUP}, 6)
    assert found["name"] == "" and found["members"][1]["name"] == "小美（备注）"   # nobody named the group


def test_group_members_now_and_none_while_they_cannot_be_read(tmp_path, db, clock):
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], clock.now)
    run = Run(tmp_path, db)
    assert [m["wxid"] for m in run.backend.group_members(GROUP)] == [BOSS, HER, ME]
    db.members[GROUP].remove(HER)                                       # she left the group
    assert [m["wxid"] for m in run.backend.group_members(GROUP)] == [BOSS, ME]
    assert run.backend.group_members("1000000099@chatroom") is None      # no member at all: the read failed
    db.fail = True
    assert run.backend.group_members(GROUP) is None and run.backend.boss_groups({BOSS}, set(), 6) is None
    db.fail = False

    def broken(limit=500):
        raise WcdbCallError("wcdb_get_sessions", -3)
    db.get_sessions = broken                                            # an error code: no session list
    assert run.backend.boss_groups({BOSS}, set(), 6) is None
    del db.get_sessions
    run.backend._client = None                                          # the database is not open
    assert run.backend.group_members(GROUP) is None and run.backend.boss_groups({BOSS}, set(), 6) is None


# ── boss_groups: is the group really new for us (history_complete, joined_at) ──

INTRO = "给大家介绍一下，这是小美"
# WeChat 4's own lines (type 10000, put into the fake database as WeChat words them) when this account comes in …
JOINED = [
    f'"{BOSS_NAME}"邀请你加入了群聊',                                         # what GroupDb.group writes
    f'"{BOSS_NAME}"邀请你加入了群聊，群聊参与人还有：{HER_NAME}',
    f'"{BOSS_NAME}"邀请你和"{HER_NAME}"加入了群聊',                          # he made it with her and us
    f'"{BOSS_NAME}"邀请你和"{HER_NAME}"、"{TESTER_NAME}"加入了群聊',
    "你加入了群聊",
    f'你通过扫描"{BOSS_NAME}"分享的二维码加入群聊',
    f'你邀请"{HER_NAME}"、"{BOSS_NAME}"加入了群聊',                          # this account made it
]
# … and the lines of someone else coming in, or none at all
NOT_JOINED = [
    f'"{BOSS_NAME}"邀请"{HER_NAME}"加入了群聊',
    f'"{HER_NAME}"通过扫描"{BOSS_NAME}"分享的二维码加入群聊',
    f'"你好呀"邀请"{HER_NAME}"加入了群聊',                                    # a member called 你好呀
    f'"{BOSS_NAME}"邀请"你好呀"加入了群聊',
    f'"{BOSS_NAME}"修改群名为"{GROUP_NAME}"',
    "你撤回了一条消息",
    f'你被"{BOSS_NAME}"移出了群聊',
]
KNOWN = {OLD_GROUP, NOTICE_GROUP}                                       # the baseline of the db fixture


def made(db, line, at, chat=GROUP, members=(BOSS, HER, ME_SENDER)):
    """A group WeChat lists, its first row one of WeChat's lines (``line``), the boss's introduction after it."""
    db.names[chat] = GROUP_NAME
    db.members[chat] = list(members)
    db.system(chat, line, at)
    db.line(chat, BOSS, INTRO, at + 10)


@pytest.mark.parametrize("line", JOINED)
def test_joined_at_is_when_wechats_line_says_this_account_came_into_the_group(tmp_path, db, clock, line):
    assert JOINED_LINE.search(line)
    made(db, line, clock.now - 600)
    [found] = Run(tmp_path, db).backend.boss_groups({BOSS}, KNOWN, 6)
    assert (found["joined_at"], found["created_at"], found["history_complete"]) == (
        float(int(clock.now - 600)), float(int(clock.now - 600)), True)


@pytest.mark.parametrize("line", NOT_JOINED)
def test_a_line_of_someone_else_coming_in_is_no_joined_at(tmp_path, db, clock, line):
    assert not JOINED_LINE.search(line)
    made(db, line, clock.now - 600)
    [found] = Run(tmp_path, db).backend.boss_groups({BOSS}, KNOWN, 6)
    assert (found["joined_at"], found["created_at"], found["history_complete"]) == (
        None, float(int(clock.now - 600)), True)


def test_only_wechats_own_lines_count_and_the_oldest_of_them(tmp_path, db, clock):
    """A member's words of the very text are hers, never WeChat's line; added, taken out and added again: since the
    first time (the oldest line)."""
    t = clock.now
    db.names[GROUP], db.members[GROUP] = GROUP_NAME, [BOSS, HER, ME_SENDER]
    db.line(GROUP, HER, f'"{BOSS_NAME}"邀请你加入了群聊', t - 900)
    [found] = Run(tmp_path, db).backend.boss_groups({BOSS}, KNOWN, 6)
    assert found["joined_at"] is None and found["created_at"] == float(int(t - 900))
    db.system(GROUP, f'"{BOSS_NAME}"邀请你加入了群聊', t - 800)
    db.system(GROUP, f'你被"{BOSS_NAME}"移出了群聊', t - 700)
    db.system(GROUP, f'"{BOSS_NAME}"邀请你加入了群聊', t - 600)
    [found] = Run(tmp_path, db).backend.boss_groups({BOSS}, KNOWN, 6)
    assert found["joined_at"] == float(int(t - 800))


def test_history_complete_is_whether_the_groups_whole_history_was_read(tmp_path, db, clock):
    """Fewer rows than CATCHUP_READ came back: all of it (WeChat shows a new member nothing from before she came in,
    so a group this account was just added to begins with WeChat's line).  A busy old group: its line is older than
    the rows read, so it is no joined_at; ``created_at`` is the oldest row read.  Read once a run."""
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 86400)      # this account was added a day ago
    for i in range(CATCHUP_READ - 2):
        db.line(GROUP, BOSS, f"第{i}句", t - 80000 + i)
    first = Run(tmp_path, db)
    [found] = first.backend.boss_groups({BOSS}, KNOWN, 6)
    assert (found["history_complete"], found["joined_at"], found["created_at"]) == (
        True, float(int(t - 86400)), float(int(t - 86400)))                 # CATCHUP_READ - 1 rows
    db.line(GROUP, BOSS, "又一句", t - 70000)
    assert first.backend.boss_groups({BOSS}, KNOWN, 6) == [found]         # (read once a run)
    [found] = Run(tmp_path, db).backend.boss_groups({BOSS}, KNOWN, 6)    # CATCHUP_READ rows: not all of it, maybe
    assert (found["history_complete"], found["joined_at"]) == (False, float(int(t - 86400)))
    db.line(GROUP, BOSS, "再一句", t - 60000)
    [found] = Run(tmp_path, db).backend.boss_groups({BOSS}, KNOWN, 6)
    assert (found["history_complete"], found["joined_at"], found["created_at"]) == (
        False, None, float(int(t - 80000)))                                 # WeChat's line is no longer among them


UNREADABLE = {"chat": GROUP, "unreadable": True}                        # those two keys only


def test_a_group_whose_lines_cannot_be_read_now_is_given_as_unreadable_until_they_can(tmp_path, db, clock):
    """(second review, N5) Never left out: PRM took a group the backend gave none of back twice for one with no boss and
    did not look at it again for an hour.  Unreadable at two calls (one a minute), readable at the third: given as
    unreadable twice (no name, no member, no guess), then as it is; another new boss group is given all along."""
    t = clock.now
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 60)
    other = "1000000007@chatroom"
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 3600, chat=other, members=(BOSS, TESTER, ME_SENDER))
    run = Run(tmp_path, db)
    real = db.get_messages

    def flaky(talker, limit=200, offset=0):
        if talker == GROUP and misses:
            misses.pop()
            raise WcdbCallError("wcdb_get_messages", -1)
        return real(talker, limit, offset)
    misses = [1, 1]
    db.get_messages = flaky
    for _ in range(2):
        first, second = run.backend.boss_groups({BOSS}, KNOWN, 6)
        assert first == UNREADABLE and second["chat"] == other and "unreadable" not in second
    [found, second] = run.backend.boss_groups({BOSS}, KNOWN, 6)
    assert found["chat"] == GROUP and found["joined_at"] == float(int(t - 60)) and "unreadable" not in found
    assert [m["wxid"] for m in found["members"]] == [BOSS, HER, ME] and second["chat"] == other


@pytest.mark.parametrize("how", ["error code", "no member yet"])
def test_a_group_whose_members_cannot_be_read_now_is_given_as_unreadable(tmp_path, db, clock, how):
    """Its members answer with an error code (only this group's: the others are read), or with none at all (a group
    always has this account: WeChat may not have written a new group's members yet): it may be a boss's, so it is given
    as unreadable, in its place by its session's time; once they read, as it is.  A group of the baseline (``known``)
    and a new one without a boss are never given."""
    t = clock.now
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 60)
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 600, chat="1000000004@chatroom", members=(HER, TESTER, ME_SENDER))
    old = "1000000008@chatroom"
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 30, chat=old, members=(BOSS, HER, ME_SENDER))
    run = Run(tmp_path, db)
    real = db.get_group_members
    if how == "error code":
        def flaky(chat):
            if chat in (GROUP, old):
                raise WcdbCallError("wcdb_get_group_members", -2)
            return real(chat)
        db.get_group_members = flaky
    else:
        db.members[GROUP], db.members[old] = [], []
    assert run.backend.boss_groups({BOSS}, KNOWN | {old}, 6) == [UNREADABLE]
    db.get_group_members = real
    db.members[GROUP], db.members[old] = [BOSS, HER, ME_SENDER], [BOSS, HER, ME_SENDER]
    [found] = run.backend.boss_groups({BOSS}, KNOWN | {old}, 6)
    assert found["chat"] == GROUP and [m["wxid"] for m in found["members"]] == [BOSS, HER, ME]


def test_an_unreadable_group_stands_in_its_place_and_the_database_closed_is_still_none(tmp_path, db, clock):
    t = clock.now
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 600)                  # begun before the other one …
    later = "1000000009@chatroom"
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 300, chat=later, members=(BOSS, TESTER, ME_SENDER))
    db.members[later] = []                                               # … whose members are not written yet
    run = Run(tmp_path, db)
    assert [g["chat"] for g in run.backend.boss_groups({BOSS}, KNOWN, 6)] == [later, GROUP]   # begun last first
    assert run.backend.boss_groups({BOSS}, KNOWN, 6)[0] == {"chat": later, "unreadable": True}
    db.fail = True                                                       # the database itself: nothing at all
    assert run.backend.boss_groups({BOSS}, KNOWN, 6) is None
    db.fail = False
    run.backend._client = None
    assert run.backend.boss_groups({BOSS}, KNOWN, 6) is None


# ── wechat_now: WeChat's clock, by the session list ─────────────────────────

def test_wechat_now_is_the_newest_session_time_in_wechats_clock(tmp_path, db, clock):
    """This PC's clock three hours ahead of WeChat's: ``wechat_now`` is WeChat's time (the newest line written, read
    off the session list), never this PC's, and moves on as WeChat writes."""
    wechat = float(int(time.time())) - 3 * 3600
    db.line(NOTICE_GROUP, COLLEAGUE, "明天开会", wechat - 60)
    db.say(HER, HER, "你好", wechat - 5)
    run = Run(tmp_path, db)
    assert run.backend.wechat_now() == wechat - 5
    db.line(NOTICE_GROUP, COLLEAGUE, "收到", wechat)
    assert run.backend.wechat_now() == wechat


def test_wechat_now_takes_a_sessions_last_line_before_its_order_key_and_milliseconds_as_seconds(tmp_path, db, clock):
    """A session row of WeChat 4: ``last_timestamp`` (its last line) before ``sort_timestamp`` (the list's order key,
    which need not be a line's time); milliseconds are seconds; a row without a time is passed over."""
    t = float(int(clock.now))
    run = Run(tmp_path, db)
    db.get_sessions = lambda limit=500: [
        {"username": GROUP, "sort_timestamp": t + 3 * 3600, "last_timestamp": t - 10},
        {"username": HER, "sort_timestamp": (t - 20) * 1000},                  # (milliseconds)
        {"username": TESTER, "last_timestamp": 0, "sort_timestamp": t - 30},
        {"username": STRANGER}, "not a row"]
    assert run.backend.wechat_now() == t - 10
    db.get_sessions = lambda limit=500: [{"username": STRANGER}]
    assert run.backend.wechat_now() is None                                     # no session gives a time
    db.get_sessions = lambda limit=500: []
    assert run.backend.wechat_now() is None


def test_wechat_now_while_the_database_cannot_be_read_is_none_and_quiet(tmp_path, db, clock, caplog):
    run = Run(tmp_path, db)
    with caplog.at_level(logging.DEBUG, logger="src.wechat.wcdb_backend"):
        assert run.backend.wechat_now() == clock.now - 20 * 86400              # (the db fixture's newest line)
        db.fail = True
        assert run.backend.wechat_now() is None

        def broken(limit=500):
            raise WcdbCallError("wcdb_get_sessions", -3)
        db.fail, db.get_sessions = False, broken                               # an error code: no session list
        assert run.backend.wechat_now() is None
        del db.get_sessions
        run.backend._client = None                                             # the database is not open
        assert run.backend.wechat_now() is None
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    text = "\n".join(r.getMessage() for r in caplog.records)
    for name in (BOSS_NAME, HER_NAME, NOTICE_NAME, OWN_NAME, BOSS, HER, OLD_GROUP, NOTICE_GROUP):
        assert name not in text


# ── has_private_chat: has she a private chat with us, quietly ──────────────────

def test_has_private_chat_by_the_consoles_list_and_the_session_list(tmp_path, db, clock):
    """She is only in the boss's group: no.  WeChat listed her private chat before (data/private_chat_names.json), or
    lists it now (the session list): yes; her chat is not read because of it."""
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 60)
    run = Run(tmp_path, db).start()
    assert run.backend.has_private_chat(HER) is False
    listed = tmp_path / "data" / "private_chat_names.json"
    listed.parent.mkdir(exist_ok=True)
    listed.write_text(json.dumps({HER: HER_NAME}, ensure_ascii=False), encoding="utf-8")
    assert run.backend.has_private_chat(HER) is True
    listed.unlink()
    assert run.backend.has_private_chat(HER) is False
    db.say(HER, HER, "你好", t)                                            # she adds us: WeChat lists her chat
    run.backend.refresh_private_chats_soon()
    run.poll()
    assert run.backend.has_private_chat(HER) is True
    assert HER not in run.backend._chat_names                            # (PRIVATE_CHATS is the admin's chat only)


def test_a_registered_creators_chat_read_is_hers_and_a_namesake_still_counts(tmp_path, db, clock):
    """A chat the bot reads (she is registered: the private filter wants her).  Another chat of her name: her chat is
    there all the same (a send to it is refused until she has a remark of her own)."""
    db.say(HER, HER, "你好", clock.now - 30)
    db.names[STRANGER] = HER_NAME
    db.say(STRANGER, STRANGER, "在吗", clock.now - 20)                    # another 小美
    run = Run(tmp_path, db)
    run.backend.set_private_filter(lambda wxid: wxid == HER)
    run.start()
    assert HER in run.backend._chat_names and run.backend.has_private_chat(HER) is True
    refused = run.backend.send_text(HER, "你好")
    assert isinstance(refused, SendRefused) and "没法确定发给谁" in refused.reason


def test_has_private_chat_is_never_a_group_this_account_or_wechats_own_chats(tmp_path, db, clock):
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    listed = tmp_path / "data" / "private_chat_names.json"
    listed.parent.mkdir(exist_ok=True)
    listed.write_text(json.dumps({"gh_0123456789ab": "品牌公众号", "filehelper": "文件传输助手", ME_SENDER: OWN_NAME,
                                  GROUP: GROUP_NAME}, ensure_ascii=False), encoding="utf-8")
    run = Run(tmp_path, db, groups=("*",)).start()
    assert run.backend._chat_names[GROUP] == GROUP_NAME                  # (a chat read, under its name)
    for chat in (GROUP, ME, ME_SENDER, "gh_0123456789ab", "filehelper", "", None):
        assert run.backend.has_private_chat(chat) is False


def test_has_private_chat_logs_nothing_and_a_send_logs_only_the_ids_first_characters(tmp_path, db, clock, caplog):
    """PRM asks for every message the bot starts for her: never a log line, never her wxid, nothing read from the
    database.  ``chat_target`` (a send's rule) still says ERROR for a chat it cannot name, with the id's first 8
    characters only."""
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    run = Run(tmp_path, db).start()
    read = list(db.read)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert run.backend.has_private_chat(HER) is False
    assert caplog.records == [] and db.read == read
    with caplog.at_level(logging.DEBUG):
        assert run.backend.chat_target(HER)[0] == ""
    [error] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert HER[:8] in error.getMessage() and HER not in error.getMessage()


# ── watch_group: read, routed as group messages, the catch-up once ─────────────

def test_a_watched_group_is_read_and_its_lines_since_then_go_through_the_route_once_oldest_first(
        tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 900)
    db.line(GROUP, BOSS, "上次说的那个事", t - 800)                       # before ``since``: history
    run = Run(tmp_path, db).start()                                      # WECHAT_GROUPS=公司通知群
    db.line(GROUP, BOSS, "给大家介绍一下，这是小美，以后由品牌的运营和你对接", t - 120)
    db.line(GROUP, HER, "谢谢老板邀请～", t - 60)                         # 「邀请」: her words all the same
    run.poll()
    assert GROUP not in run.backend._chat_names and run.lines() == []    # not watched: not read
    run.backend.watch_group(GROUP, since=t - 300)
    run.poll()
    assert run.lines() == [(BOSS, "给大家介绍一下，这是小美，以后由品牌的运营和你对接"), (HER, "谢谢老板邀请～")]
    hers = run.routed[-1]
    assert {key: hers[key] for key in ("is_group", "chat_id", "group_name", "sender_id", "sender_name", "msg_type",
                                       "timestamp")} == {
        "is_group": True, "chat_id": GROUP, "group_name": GROUP_NAME, "sender_id": HER, "sender_name": HER_NAME,
        "msg_type": 1, "timestamp": int(t - 60)}
    assert hers["message_id"] and hers["message_id"] != run.routed[0]["message_id"]
    assert hers["_not_self_by_id"] is True
    run.poll()
    assert len(run.lines()) == 2                                         # nothing twice
    db.line(GROUP, HER, "平时主要带美妆", t + 5)
    db.line(GROUP, HER, "有样品吗", t + 6)                               # each line on its own (no burst)
    run.poll()
    assert run.lines()[2:] == [(HER, "平时主要带美妆"), (HER, "有样品吗")]
    assert run.stored() == 4 and run.own == []


def test_wechats_lines_in_a_watched_group_go_through_the_route_and_her_custom_id_is_cut_off(tmp_path, db, clock):
    """A line without a sender (WeChat's: someone added, a recall) is nobody's: through the route, never to the
    own-message hook (a line without a sender used to count as this account's).  An old account's custom id has no
    wxid_ prefix: its ``<id>:`` is cut off her words all the same."""
    custom = "meimei_88"
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, custom, ME_SENDER], t - 60)
    run = Run(tmp_path, db).start()
    run.backend.watch_group(GROUP, since=t - 60)
    run.poll()
    db.system(GROUP, f'"{BOSS_NAME}"邀请"{TESTER_NAME}"加入了群聊', t + 1)
    db.line(GROUP, custom, "在的", t + 2)
    run.poll()
    assert run.lines() == [("", f'"{BOSS_NAME}"邀请你加入了群聊'), ("", f'"{BOSS_NAME}"邀请"{TESTER_NAME}"加入了群聊'),
                           (custom, "在的")]
    assert run.own == []


def test_lines_the_route_got_before_the_watch_are_not_handed_on_again(tmp_path, db, clock):
    """WECHAT_GROUPS=* reads the groups the bot saw at start: what the route got of the group before the watch is
    stored (and silent; a burst's lines merged), and the catch-up does not hand it on again; the rest since
    ``since`` goes."""
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 900)
    db.line(GROUP, BOSS, "给大家介绍一下，这是小美", t - 200)              # before the start: not read
    run = Run(tmp_path, db, groups=("*",)).start()
    db.line(GROUP, HER, "老板好", t - 100)
    db.line(GROUP, HER, "我是小美", t - 99)                             # one burst
    run.poll()
    assert run.lines() == [(HER, "老板好\n我是小美")]
    run.backend.watch_group(GROUP, since=t - 300)
    run.poll()
    assert run.lines() == [(HER, "老板好\n我是小美"), (BOSS, "给大家介绍一下，这是小美")]


@pytest.mark.parametrize("groups", [(NOTICE_NAME,), ("*",)], ids=["not in WECHAT_GROUPS", "WECHAT_GROUPS=*"])
def test_after_a_restart_the_watch_hands_on_only_what_came_while_the_bot_was_stopped(tmp_path, db, clock, groups):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 900)
    first = Run(tmp_path, db, groups=groups).start()
    first.backend.watch_group(GROUP, since=t - 900)
    db.line(GROUP, HER, "在吗", t - 100)
    first.poll()
    assert first.lines() == [("", f'"{BOSS_NAME}"邀请你加入了群聊'), (HER, "在吗")]
    last = first.routed[-1]["timestamp"]                                 # PRM's last handled line
    first.stop()
    db.line(GROUP, HER, "我平时主要带美妆", t + 60)                       # while the bot was stopped
    db.line(GROUP, ME_SENDER, "好的，稍后联系你", t + 90)                 # an operator on the phone
    db.line(GROUP, BOSS, "辛苦了", t + 120)
    clock.now = t + 600
    second = Run(tmp_path, db, groups=groups, messages=first.messages)
    second.backend.watch_group(GROUP, since=last)                        # PRM at start, before the poll loop
    second.start()
    second.poll()
    assert second.lines() == [(HER, "我平时主要带美妆"), (BOSS, "辛苦了")]
    assert [(m["chat_id"], m["content"], m["is_group"]) for m in second.own] == [(GROUP, "好的，稍后联系你", True)]
    second.poll()
    assert len(second.lines()) == 2 and len(second.own) == 1


@pytest.mark.parametrize("groups", [(NOTICE_NAME,), ("*",)], ids=["not in WECHAT_GROUPS", "WECHAT_GROUPS=*"])
def test_after_the_database_is_reopened_what_came_meanwhile_goes_once(tmp_path, db, clock, monkeypatch, groups):
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: db)
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 900)
    run = Run(tmp_path, db, groups=groups).start()
    run.backend._window = Window()
    run.backend.watch_group(GROUP, since=t - 300)
    db.line(GROUP, HER, "在吗", t - 10)
    run.poll()
    db.line(GROUP, HER, "数据库读不了的时候说的", t + 30)
    db.line(GROUP, ME_SENDER, "我在手机上回了", t + 40)
    clock.now = t + 120
    run.backend._reinitialize()                                           # priming marks all of it seen …
    run.poll()
    run.poll()
    assert run.lines() == [(HER, "在吗"), (HER, "数据库读不了的时候说的")]  # … the watch hands it on all the same
    assert [m["content"] for m in run.own] == ["我在手机上回了"]


def test_a_group_that_cannot_be_read_gets_its_catch_up_at_a_later_poll(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 60)
    db.line(GROUP, HER, "在吗", t - 30)
    run = Run(tmp_path, db).start()
    real = db.get_messages

    def flaky(talker, limit=200, offset=0):
        if talker == GROUP:
            raise WcdbCallError("wcdb_get_messages", -1)
        return real(talker, limit, offset)
    db.get_messages = flaky
    run.backend.watch_group(GROUP, since=t - 60)
    run.poll()
    assert run.lines() == []
    db.get_messages = real
    run.poll()
    assert run.lines() == [("", f'"{BOSS_NAME}"邀请你加入了群聊'), (HER, "在吗")]


def test_unwatching_reads_the_group_only_if_wechat_groups_names_it(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 60)
    run = Run(tmp_path, db).start()
    run.backend.watch_group(GROUP, since=t)
    run.backend.watch_group(NOTICE_GROUP, since=t)
    run.poll()
    run.backend.unwatch_group(GROUP)
    run.backend.unwatch_group(NOTICE_GROUP)
    db.line(GROUP, HER, "还在吗", t + 10)
    db.line(GROUP, ME_SENDER, "在的", t + 11)
    db.line(NOTICE_GROUP, COLLEAGUE, "明天开会", t + 12)
    db.line(NOTICE_GROUP, ME_SENDER, "收到", t + 13)
    run.poll()
    assert GROUP not in run.backend._chat_names and run.lines() == []           # not read any more
    assert NOTICE_GROUP in run.backend._chat_names                                # WECHAT_GROUPS names it: read …
    assert run.lines(NOTICE_GROUP) == [(COLLEAGUE, "明天开会")] and run.own == []   # … as any group
    run.backend.watch_group(GROUP, since=t)                                      # watched again: what was missed
    run.poll()
    assert run.lines() == [(HER, "还在吗")] and [m["content"] for m in run.own] == ["在的"]


# ── the catch-up's since, after a restart ───────────────────────────────────

def test_the_catch_up_hands_on_the_lines_written_at_since_or_later(tmp_path, db, clock):
    """``timestamp >= since``: PRM gives its last line handled + 1 s, so that line's second is never handed on again
    (this account's lines are never stored: only ``since`` keeps them from the own-message hook)."""
    t = clock.now
    db.names[GROUP], db.members[GROUP] = GROUP_NAME, [BOSS, HER, ME_SENDER]
    db.line(GROUP, HER, "上一句", t - 101)                                 # since - 1
    db.line(GROUP, ME_SENDER, "我们回的上一句", t - 101)
    db.line(GROUP, HER, "这一句", t - 100)                                 # since
    db.line(GROUP, ME_SENDER, "我们回的这一句", t - 100)
    db.line(GROUP, BOSS, "下一句", t - 99)
    run = Run(tmp_path, db).start()
    run.backend.watch_group(GROUP, since=t - 100)
    run.poll()
    assert run.lines() == [(HER, "这一句"), (BOSS, "下一句")]
    assert [m["content"] for m in run.own] == ["我们回的这一句"]


@pytest.mark.parametrize("groups", [(NOTICE_NAME,), ("*",)], ids=["not in WECHAT_GROUPS", "WECHAT_GROUPS=*"])
def test_after_a_restart_our_line_read_back_before_since_is_not_handed_on_again(tmp_path, db, clock, groups):
    """(test_r1) Our reply read back before the stop is PRM's last line handled; watched again from it + 1 s, it does
    not reach the own-message hook a second time (PRM would take it for an operator's), and her line of the very
    next second, written while the bot was stopped, is handed on."""
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 900)
    first = Run(tmp_path, db, groups=groups).start()
    first.backend.watch_group(GROUP, since=t - 900)
    db.line(GROUP, HER, "在吗", t - 100)
    first.poll()
    db.line(GROUP, ME_SENDER, "在的，您好～", t - 50)                        # the bot's reply, read back in this run
    first.poll()
    assert [m["content"] for m in first.own] == ["在的，您好～"]
    last = first.own[-1]["timestamp"]                                    # PRM's last line handled: ours
    first.stop()
    db.line(GROUP, HER, "我想了解一下合作", last + 1)                       # while the bot was stopped
    clock.now = t + 120
    second = Run(tmp_path, db, groups=groups, messages=first.messages)
    second.backend.watch_group(GROUP, since=last + 1)                    # PRM at start, before the poll loop
    second.start()
    second.poll()
    assert second.own == [] and second.lines() == [(HER, "我想了解一下合作")]
    second.poll()
    assert second.own == [] and len(second.lines()) == 1


# ── watch_caught_up: the catch-up has run ───────────────────────────────────

def test_watch_caught_up_once_the_catch_up_ran_since_the_latest_watch(tmp_path, db, clock):
    t = clock.now
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 120)
    run = Run(tmp_path, db).start()
    assert run.backend.watch_caught_up(GROUP) is False                    # not watched
    run.backend.watch_group(GROUP, since=t - 300)
    assert run.backend.watch_caught_up(GROUP) is False                    # its catch-up is still to run
    run.poll()
    assert run.backend.watch_caught_up(GROUP) is True
    assert run.lines()[-1] == (BOSS, INTRO)                               # the boss's introduction is handed on by then
    run.poll()
    assert run.backend.watch_caught_up(GROUP) is True
    run.backend.watch_group(GROUP, since=t - 300)                         # watched again (PRM at start …)
    assert run.backend.watch_caught_up(GROUP) is False
    run.poll()
    assert run.backend.watch_caught_up(GROUP) is True and len(run.lines()) == 2    # (nothing twice)
    run.backend.unwatch_group(GROUP)
    assert run.backend.watch_caught_up(GROUP) is False
    assert run.backend.watch_caught_up(NOTICE_GROUP) is False             # read (WECHAT_GROUPS), never watched


def test_watch_caught_up_waits_for_a_group_that_cannot_be_read_and_for_a_watch_during_the_catch_up(
        tmp_path, db, clock):
    t = clock.now
    made(db, f'"{BOSS_NAME}"邀请你加入了群聊', t - 120)
    run = Run(tmp_path, db).start()
    real = db.get_messages

    def flaky(talker, limit=200, offset=0):
        if talker == GROUP:
            raise WcdbCallError("wcdb_get_messages", -1)
        return real(talker, limit, offset)
    db.get_messages = flaky
    run.backend.watch_group(GROUP, since=t - 300)
    run.poll()
    assert run.backend.watch_caught_up(GROUP) is False and run.lines() == []
    db.get_messages = real
    route = run.route

    def watching_again(msg):                     # PRM watches the group again while its catch-up runs
        if msg.get("content") == INTRO:
            run.backend.watch_group(GROUP, since=t - 300)
        return route(msg)
    run.route = watching_again
    run.poll()
    assert run.lines() == [("", f'"{BOSS_NAME}"邀请你加入了群聊'), (BOSS, INTRO)]
    assert run.backend.watch_caught_up(GROUP) is False                    # that watch's catch-up is still to run
    run.poll()
    assert run.backend.watch_caught_up(GROUP) is True and len(run.lines()) == 2


# ── the order: a watched group's lines one by one, in the poll thread ─────────

def test_a_watched_groups_lines_reach_the_route_in_order_and_other_chats_keep_the_pool(tmp_path, db, clock):
    """With the bot's worker pool running: her picture takes a while (looked for on disk), and the words after it,
    the boss's line before it, her recall, the catch-up's lines all reach the route one by one in the order WeChat
    wrote them, in the poll thread; every other chat's lines still go through the pool."""
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 60)
    run = Run(tmp_path, db).start()
    run.backend._start_workers()
    heard = []
    route = run.route

    def slow_for_a_picture(msg):
        if msg["msg_type"] == 3:
            time.sleep(0.2)
        heard.append((msg["chat_id"], msg["special"].get("title") if msg["msg_type"] == 10000 else msg["content"],
                      threading.current_thread().name))
        return route(msg)
    run.route = slow_for_a_picture
    try:
        db.say(GROUP, HER, f"{HER}:\n<msg><img/></msg>", t - 30, local_type=3)    # before the watch: the catch-up's
        db.line(GROUP, HER, "这是我的主页", t - 29)
        run.backend.watch_group(GROUP, since=t - 60)
        run.poll()
        db.line(GROUP, BOSS, "她是我朋友", t + 1)
        db.say(GROUP, HER, f"{HER}:\n<msg><img/></msg>", t + 2, local_type=3)
        db.line(GROUP, HER, "这是我的数据", t + 3)
        db.line(NOTICE_GROUP, COLLEAGUE, "明天开会", t + 4)
        run.poll()
        row = next(r for r in db.chats[GROUP] if r["message_content"].endswith("这是我的数据"))
        row.update(local_type=10000, message_content=REVOKE_XML)          # she takes it back
        db.line(GROUP, HER, "发错了", t + 5)
        run.poll()
    finally:
        run.backend._pool.shutdown(wait=True)
    here = threading.current_thread().name
    assert [(text, thread) for chat, text, thread in heard if chat == GROUP] == [
        (f'"{BOSS_NAME}"邀请你加入了群聊', here), ("[图片]", here), ("这是我的主页", here),
        ("她是我朋友", here), ("[图片]", here), ("这是我的数据", here), ('"小美" 撤回了一条消息', here), ("发错了", here)]
    [(text, thread)] = [(text, thread) for chat, text, thread in heard if chat == NOTICE_GROUP]
    assert text == "明天开会" and thread.startswith("bot-cb-") and thread != here


# ── this account's own lines ──────────────────────────────────────────────

def test_own_lines_in_a_watched_group_reach_the_hook_and_other_groups_stay_as_they_were(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 900)
    run = Run(tmp_path, db, groups=("*",)).start()                       # every group read
    run.backend.watch_group(GROUP, since=t)
    run.poll()
    db.line(GROUP, ME_SENDER, "你好小美，我是品牌的运营", t + 5)
    db.line(NOTICE_GROUP, ME_SENDER, "收到", t + 6)
    db.line(NOTICE_GROUP, TESTER, "谢谢邀请", t + 7)
    db.system(NOTICE_GROUP, f'"{BOSS_NAME}"邀请"{HER_NAME}"加入了群聊', t + 8)
    run.poll()
    [own] = run.own
    assert {key: own[key] for key in ("chat_id", "sender_id", "content", "is_group", "group_name")} == {
        "chat_id": GROUP, "sender_id": ME_SENDER, "content": "你好小美，我是品牌的运营", "is_group": True,
        "group_name": GROUP_NAME}
    assert run.lines() == []                                             # this account's: never through the route
    # another group, as before: this account's line to no hook; a member's words with 「邀请」 and WeChat's line
    # left out (that rule stays for every group not watched)
    assert run.lines(NOTICE_GROUP) == []
    hook_calls = []
    run.backend.set_own_message_hook(hook_calls.append)
    run.backend.unwatch_group(GROUP)
    db.line(GROUP, ME_SENDER, "再说一句", t + 9)
    run.poll()
    assert hook_calls == []                                              # unwatched (WECHAT_GROUPS=*): no hook


def test_a_recall_in_a_watched_group_is_handed_on_as_wechats_line(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 60)
    run = Run(tmp_path, db).start()
    run.backend.watch_group(GROUP, since=t)
    run.poll()
    db.line(GROUP, HER, "这个价格可以", t + 1)
    run.poll()
    row = next(r for r in db.chats[GROUP] if r["message_content"].endswith("这个价格可以"))
    row.update(local_type=10000, message_content=REVOKE_XML)              # WeChat rewrites the row in place
    run.poll()
    run.poll()
    assert run.lines() == [(HER, "这个价格可以"), (HER, '"小美" 撤回了一条消息')]
    assert run.routed[-1]["recalled_id"] == run.routed[-2]["message_id"]


# ── sending into a group ──────────────────────────────────────────────────

def sending(tmp_path, db, title, groups=(NOTICE_NAME,)):
    """A run whose window searches by name and reads the opened chat's title (SEND_VERIFY_CHAT, on by default)."""
    run = Run(tmp_path, db, groups=groups).start()
    del run.backend._send_and_confirm                                   # the real one: the window and its check
    run.backend._window = Window()
    run.backend._title_reader = lambda hwnd: title
    return run


def test_a_send_reaches_a_watched_group_by_its_name(tmp_path, db, clock):
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    run = sending(tmp_path, db, f"{GROUP_NAME} (3)")                     # a group's title: its member count
    run.backend.watch_group(GROUP, since=clock.now)
    assert run.backend.send_text(GROUP, "你好小美～") is True              # before its first poll too
    run.poll()
    assert run.backend.send_text(GROUP, "我是品牌的运营") is True
    assert run.backend._window.typed == [(GROUP_NAME, "你好小美～"), (GROUP_NAME, "我是品牌的运营")]


def test_a_search_that_opens_another_chat_is_not_pasted_into(tmp_path, db, clock, caplog):
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    db.say(HER, HER, "你好", clock.now - 30)                               # her private chat, known to the bot
    run = sending(tmp_path, db, HER_NAME)
    run.backend.watch_group(GROUP, since=clock.now)
    run.poll()
    caplog.set_level(logging.WARNING, logger="src.wechat.wcdb_backend")
    refused = run.backend.send_text(GROUP, "你好小美～")
    assert isinstance(refused, SendRefused) and not refused
    assert f"「{HER_NAME}」" in refused.reason and run.backend._window.typed == []


@pytest.mark.parametrize("namesake", ["group", "private"])
def test_a_group_whose_name_another_chat_has_too_is_refused(tmp_path, db, clock, namesake):
    db.group(GROUP, HER_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)      # the boss named the group after her
    if namesake == "group":
        db.group("1000000006@chatroom", HER_NAME, [HER, TESTER, ME_SENDER], clock.now - 50)
    else:
        db.say(HER, HER, "你好", clock.now - 30)                           # her private chat: 小美 too
    run = sending(tmp_path, db, f"{HER_NAME} (3)", groups=("*",))
    run.backend.watch_group(GROUP, since=clock.now)
    run.poll()
    refused = run.backend.send_text(GROUP, "你好小美～")
    assert isinstance(refused, SendRefused) and not refused
    assert f"「{HER_NAME}」" in refused.reason and "没法确定发给谁" in refused.reason
    assert run.backend._window.typed == []


def test_a_group_wechat_gives_no_name_is_not_typed_until_it_has_one(tmp_path, db, clock, monkeypatch):
    db.group(GROUP, "", [BOSS, HER, ME_SENDER], clock.now - 60)          # nobody named it
    run = sending(tmp_path, db, f"{GROUP_NAME} (3)")
    run.backend.watch_group(GROUP, since=clock.now)
    run.poll()
    assert run.backend._chat_names[GROUP] == GROUP                        # read all the same, under its id
    refused = run.backend.send_text(GROUP, "你好小美～")
    assert isinstance(refused, SendRefused) and refused.reason == GROUP_NO_NAME
    assert run.backend._window.typed == []
    db.names[GROUP] = GROUP_NAME                                          # the boss names the group
    monkeypatch.setattr(wcdb_backend, "SESSION_REFRESH_SEC", 0)           # (its name is asked for again a minute on)
    run.poll()
    assert run.backend._chat_names[GROUP] == GROUP_NAME
    assert run.backend.send_text(GROUP, "你好小美～") is True
    assert run.backend._window.typed == [(GROUP_NAME, "你好小美～")]


KNOWN_PRIVATE = {_ocr_key("小美美"), _ocr_key("Amy 陈")}                      # private chats the bot knows
KNOWN_OTHERS = KNOWN_PRIVATE | {_ocr_key(NOTICE_NAME)}                         # every other chat it knows


@pytest.mark.parametrize("title, name, is_group, verdict", [
    ("小美 (3)", "小美", True, "ok"),                          # a group's title: its member count
    ("小美（3）", "小美", True, "ok"),
    ("小美合作群(3", "小美合作群", True, "ok"),                  # (second review, F10) OCR cut its count off …
    ("小美合作群（3", "小美合作群", True, "ok"),
    ("小美合作群 (35人", "小美合作群", True, "ok"),
    ("小美合作群(", "小美合作群", True, "ok"),                   # … even all of it but its bracket
    ("小美美", "小美", True, "private"),                        # a private chat the bot knows, opened for the group
    ("AMY陈", "小美", True, "private"),                         # (「Amy 陈」: no spaces, no case)
    ("小美美(3", "小美", True, "other"),                        # a known name with a count cut off: a group's title
    ("小美美 (3)", "小美", True, "other"),                      # (another known chat: as before)
    ("公司通知群", "小美", True, "other"),                      # a known group, its count lost
    ("小美", "小美", True, "unknown"),                          # the group's own name, its count lost: sent as before
    ("小美美美美", "小美", True, "unknown"),                    # a contact the bot does not know: sent as before
    ("小美美美美…", "小美合作群", True, "unknown"),              # cut off, not known
    ("小美美美美 (8)", "小美", True, "unknown"),                # a group the bot does not know: as before
    ("小美美美美(8", "小美", True, "unknown"),
    ("", "小美", True, "unknown"),                             # nothing read: as before
    ("……", "小美", True, "unknown"),
    ("小美美", "小美", False, "other"),                         # a private send: as before (a known chat) …
    ("小美美美美", "小美", False, "unknown"),                   # … one it does not know …
    ("小美", "小美", False, "ok"),
    ("小美美美美 (8)", "小美", False, "group"),                 # … a group's title …
    ("小美美美美(8", "小美", False, "unknown"),                 # … a count cut off is no count for it
    ("小美(3", "小美", False, "unknown"),
])
def test_what_a_title_says_when_a_group_was_asked_for(title, name, is_group, verdict):
    assert title_verdict(title, name, KNOWN_OTHERS - {_ocr_key(name)}, is_group, KNOWN_PRIVATE) == verdict


def test_a_private_chat_whose_name_reads_like_the_groups_is_known_too():
    """The group 「小美」 and a private chat 「小美🌸」 (no namesake: the names differ, so the search is typed): OCR does
    not read the emoji, so a title 「小美」 without a count may be hers.  Refused; with a count it is the group."""
    private = {_ocr_key("小美🌸")}
    assert title_verdict("小美", "小美", set(), True, private) == "private"
    assert title_verdict("小美 (3)", "小美", set(), True, private) == "ok"
    assert title_verdict("小美", "小美", set(), True) == "unknown"                # nobody known by it


def reading(run, *titles):
    """The opened chat's title as the screen gives it, one per look (the last one again); what was read."""
    reads, titles = [], list(titles)

    def reader(hwnd):
        reads.append(hwnd)
        title = titles.pop(0) if len(titles) > 1 else titles[0]
        if isinstance(title, Exception):
            raise title
        return title
    run.backend._title_reader = reader
    return reads


MEIMEI = "小美美"                                                          # a contact whose name holds the group's


@pytest.mark.parametrize("known", ["session list", "console's list", "read"])
def test_a_group_send_whose_search_opened_a_known_private_chat_is_refused(tmp_path, db, clock, known):
    """SEND_VERIFY_CHAT: the boss named the group 「小美」 after her, and the search opened the contact 「小美美」, a
    private chat the bot knows (WeChat lists it, the console's list has it, or the bot reads it): no member count,
    not the group.  Read again first (the screen may still show the chat before), then refused (``NOT_A_GROUP``):
    nothing pasted, no file, no screenshot of that chat."""
    db.group(GROUP, HER_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    db.names[STRANGER] = MEIMEI
    if known == "session list":
        db.say(STRANGER, STRANGER, "在吗", clock.now - 30)
    elif known == "console's list":
        listed = tmp_path / "data" / "private_chat_names.json"
        listed.parent.mkdir(exist_ok=True)
        listed.write_text(json.dumps({STRANGER: MEIMEI}, ensure_ascii=False), encoding="utf-8")
    run = sending(tmp_path, db, MEIMEI)
    if known == "read":
        run.backend._add_talker(MEIMEI, STRANGER, is_group=False)        # (a creator's chat, read before WeChat lists it)
    reads = reading(run, MEIMEI)
    run.backend.watch_group(GROUP, since=clock.now)
    run.poll()
    refused = run.backend.send_text(GROUP, "你好小美～")
    assert isinstance(refused, SendRefused) and not refused
    assert refused.reason == NOT_A_GROUP.format(name=HER_NAME, title=MEIMEI) and len(reads) == VERIFY_LOOKS
    assert not run.backend.send_file(GROUP, str(tmp_path / "样品.png"))
    assert run.backend._screenshot_of(HER_NAME, GROUP) == ""
    assert run.backend._window.typed == [] and run.backend._window.shots == []


def test_a_group_send_whose_title_reads_as_a_known_private_chat_of_the_groups_name_is_refused(tmp_path, db, clock):
    """「小美🌸」 is no namesake of the group 「小美」 (the names differ: the search is typed), but OCR reads her title
    「小美」: without a member count it may be hers, so the send is refused; with the group's count it goes."""
    db.group(GROUP, HER_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    db.names[STRANGER] = "小美🌸"
    db.say(STRANGER, STRANGER, "在吗", clock.now - 30)
    run = sending(tmp_path, db, HER_NAME)
    run.backend.watch_group(GROUP, since=clock.now)
    run.poll()
    refused = run.backend.send_text(GROUP, "你好小美～")
    assert isinstance(refused, SendRefused) and refused.reason == NOT_A_GROUP.format(name=HER_NAME, title=HER_NAME)
    run.backend._title_reader = lambda hwnd: f"{HER_NAME} (3)"
    assert run.backend.send_text(GROUP, "你好小美～") is True
    assert run.backend._window.typed == [(HER_NAME, "你好小美～")]


@pytest.mark.parametrize("titles", [
    [f"{HER_NAME} (3)"],
    [f"{NOTICE_NAME} (4)", f"{HER_NAME} (3)"],                   # the screen still showed the chat before
    [f"{HER_NAME}(3"], [f"{HER_NAME}（3"], [f"{HER_NAME} (3人"],  # (second review, F10) OCR cut its count off
    [MEIMEI], [HER_NAME],                                         # a contact the bot does not know, its own name
    [""], ["……"], [RuntimeError("Windows has no OCR language installed")],      # cannot be read: as before
], ids=["its title", "read again", "count cut", "full-width count cut", "count of 人 cut", "unknown contact",
        "its own name without a count", "nothing read", "no letters", "no OCR"])
def test_a_group_send_goes_ahead_with_the_groups_title_or_one_the_bot_does_not_know(tmp_path, db, clock, titles):
    """Only a known private chat's title is refused: a title the bot does not know goes ahead, as it did before the
    groups were sent to (a contact never seen; the group's own name, its member count lost), and so does a group's
    title whose count OCR cut off at the end."""
    db.group(GROUP, HER_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    run = sending(tmp_path, db, "")
    reading(run, *titles)
    run.backend.watch_group(GROUP, since=clock.now)
    run.poll()
    assert run.backend.send_text(GROUP, "你好小美～") is True
    assert run.backend._window.typed == [(HER_NAME, "你好小美～")]


def test_a_group_whose_count_ocr_cut_off_is_sent_to_as_asked(tmp_path, db, clock, caplog):
    """(second review, F10) This PC's OCR reads the group's title without its closing bracket, 「小美合作群(3」: a group's
    title all the same (refused, every group draft on such a PC would fail), and the one asked for."""
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    db.say(HER, HER, "你好", clock.now - 30)                    # her private chat, known (her name is in the group's)
    run = sending(tmp_path, db, f"{GROUP_NAME}(3")
    run.backend.watch_group(GROUP, since=clock.now)
    run.poll()
    with caplog.at_level(logging.INFO, logger="src.wechat.wcdb_backend"):
        assert run.backend.send_text(GROUP, "你好小美～") is True
        run.backend._title_reader = lambda hwnd: f"{GROUP_NAME}（3"
        assert run.backend.send_text(GROUP, "我是品牌的运营") is True
    assert run.backend._window.typed == [(GROUP_NAME, "你好小美～"), (GROUP_NAME, "我是品牌的运营")]
    assert sum("as asked" in r.getMessage() for r in caplog.records) == 2


def test_a_private_send_is_checked_as_before(tmp_path, db, clock):
    """Her private chat (the console's list names it): a title the bot does not know is no judgement, as it was, and a
    count cut off is no count for it; a group's title, or another private chat the bot knows, is refused
    (``WRONG_CHAT``), never ``NOT_A_GROUP``."""
    listed = tmp_path / "data" / "private_chat_names.json"
    listed.parent.mkdir(exist_ok=True)
    listed.write_text(json.dumps({HER: HER_NAME, STRANGER: MEIMEI}, ensure_ascii=False), encoding="utf-8")
    for title in ("小美美美美", HER_NAME, "小美美美美(8", f"{HER_NAME}(3"):
        run = sending(tmp_path, db, title)
        assert run.backend.send_text(HER, "你好～") is True
        assert run.backend._window.typed == [(HER_NAME, "你好～")]
    for title in (f"{GROUP_NAME} (3)", MEIMEI):                          # a group's title; a private chat it knows
        run = sending(tmp_path, db, title)
        refused = run.backend.send_text(HER, "你好～")
        assert isinstance(refused, SendRefused) and refused.reason == WRONG_CHAT.format(name=HER_NAME, title=title)
        assert run.backend._window.typed == []


def test_a_refused_send_logs_the_ids_first_characters_and_the_verdict_only(tmp_path, db, clock, caplog):
    """(second review, F12) The reason, with the names, goes back to the caller (PRM shows it on the draft); the log
    line has the chat id's first 8 characters and the verdict only: a known private chat opened for the group
    (NOT_A_GROUP), another known chat (WRONG_CHAT), a group WeChat gives no name (GROUP_NO_NAME)."""
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], clock.now - 60)
    nameless = "1000000010@chatroom"
    db.group(nameless, "", [BOSS, TESTER, ME_SENDER], clock.now - 50)
    db.names[STRANGER] = MEIMEI
    db.say(STRANGER, STRANGER, "在吗", clock.now - 30)
    run = sending(tmp_path, db, MEIMEI)
    run.backend.watch_group(GROUP, since=clock.now)
    run.backend.watch_group(nameless, since=clock.now)
    run.poll()
    caplog.set_level(logging.DEBUG, logger="src.wechat.wcdb_backend")
    caplog.clear()
    assert run.backend.send_text(GROUP, "你好小美～").reason == NOT_A_GROUP.format(name=GROUP_NAME, title=MEIMEI)
    run.backend._title_reader = lambda hwnd: f"{NOTICE_NAME} (4)"
    assert run.backend.send_text(GROUP, "你好小美～").reason == WRONG_CHAT.format(name=GROUP_NAME,
                                                                                  title=f"{NOTICE_NAME} (4)")
    assert run.backend.send_text(nameless, "你好").reason == GROUP_NO_NAME
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(said) == 3 and all(line.startswith("Not ") and "10000000…" in line for line in said)
    assert "'private' (NOT_A_GROUP)" in said[0] and "'other' (WRONG_CHAT)" in said[1]
    for name in (GROUP_NAME, MEIMEI, NOTICE_NAME, HER_NAME, GROUP, nameless):
        assert all(name not in line for line in said)


def test_no_name_is_logged(tmp_path, db, clock, caplog):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 60)
    db.line(GROUP, HER, "在吗", t - 30)
    unread = "1000000011@chatroom"
    db.group(unread, "老板测试和小美的另一个群", [BOSS, HER, ME_SENDER], t - 50)
    db.members[unread] = []                                              # (its members are not written yet)
    with caplog.at_level(logging.DEBUG, logger="src.wechat.wcdb_backend"):
        run = Run(tmp_path, db).start()
        run.backend.contact_ids([BOSS_NAME])
        assert {"chat": unread, "unreadable": True} in run.backend.boss_groups({BOSS}, set(), 6)
        assert run.backend.wechat_now() == t - 30
        run.backend.group_members(GROUP)
        run.backend.watch_group(GROUP, since=t - 60)
        run.poll()
        run.backend.unwatch_group(GROUP)
        run.poll()
    text = "\n".join(r.getMessage() for r in caplog.records if "老板拉的群" in r.getMessage()
                     or "Group catch-up" in r.getMessage())
    assert "老板拉的群" in text and "Group catch-up" in text
    for name in (BOSS_NAME, HER_NAME, GROUP_NAME, OWN_NAME, BOSS, HER):
        assert name not in text
