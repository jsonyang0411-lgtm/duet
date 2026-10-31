"""老板拉的群 (功能 U, the backend's part): a boss (PRM_GROUP_WITH) makes a small WeChat group of himself, a creator who is
his friend and this account, and PRM takes it over.  The WCDB backend finds the bosses' wxids by name (``contact_ids``),
the new small groups a boss is in (``boss_groups``) and their members (``group_members``); a watched group
(``watch_group``) is read whatever WECHAT_GROUPS says and its lines go through the route as group messages (its
catch-up once, oldest first; after a restart and a re-opened database only what was not handed on yet), this account's
own lines there to the own-message hook; a send reaches a group by the name WeChat gives it, like any chat, and a name
another chat has too is refused.  Every other group is read and kept silent as before.

The real WcdbBackend over the fake WeChat 4 database of tests/test_new_friends.py (session rows without names, names
from get_display_names, messages newest first, this account's own lines from its wxid without the data folder's suffix;
group members added here), a real router and message store (the route stores every line it gets: step persist); the
own-message hook and the window write down what they got.  Every name and id is made up.
"""

import logging

import pytest

from router_support import ADMIN, BOT, TraceSpy
from test_new_friends import ME, ME_SENDER, Clock, WeChatDb

from src.admin import AdminCommandHandler
from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.router import MessageRouter
from src.wechat import wcdb_backend
from src.wechat.wcdb_backend import GROUP_NO_NAME, WcdbBackend
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

    def send_to_chat(self, name, text, max_retries=2, check=None):
        refused = check(100) if check is not None else None
        if refused is not None:
            return refused
        self.typed.append((name, text))
        return True

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


def test_no_name_is_logged(tmp_path, db, clock, caplog):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 60)
    db.line(GROUP, HER, "在吗", t - 30)
    with caplog.at_level(logging.DEBUG, logger="src.wechat.wcdb_backend"):
        run = Run(tmp_path, db).start()
        run.backend.contact_ids([BOSS_NAME])
        run.backend.boss_groups({BOSS}, set(), 6)
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
