"""老板拉的群 (PRM_BOSS_GROUPS, src/prm/boss_groups.py, 2026-10-31): a boss of PRM_GROUP_WITH makes a small group with a
creator and this account; the bot finds it (never a group from before the switch), takes her over when it can tell who
she is (else the console picks), drafts our first line there (always reviewed), and from then on drafts only HER lines
in the group, as in her private chat: the gates, 下班时段 and 自动模式 alike, no holding reply; the boss's and the other
members' lines are context only.  What the bot starts itself (a shipping notice, a 专属佣金 step, a 开场白) goes to her
private chat when she has one, else to the group, else nowhere (the deal says so).  The WeChat backend is a fake with the
methods the backend part builds (spec §4).  Every name, id and number is made up; the boss is 老板测试."""

import itertools
import json
import logging
import time
from types import SimpleNamespace

import pytest

from test_prm import ADMIN, CREATOR, Bot, Clock, draft

from src.db import MessageStore, initialize_db
from src.prm import boss_groups as bg
from src.prm.service import (GROUP_CLOSED_TEXT, GROUP_UNSENT, PART_LATE_SEC, SETTLE_SEC, UNREACHABLE, PrmService,
                              event_out)
from src.wechat.window_controller import SendRefused

BOSS = "老板测试"
BOSS_ID = "wxid_boss01"
ME = "wxid_me01"
HELPER = "wxid_helper"                  # a cc user of test_prm's config: never a creator
MEMBER = "wxid_member02"
TWIN = "wxid_twin03"                    # a member whose name is the boss's (a namesake, or the boss under a new wxid)
OLD = "100000001@chatroom"              # a group from before the switch
GROUP = "200000001@chatroom"
OTHER = "300000001@chatroom"
GROUP_NAME = "老板测试、小美、商务号"
TOLD_NAME = "老板、小美、商务号"           # the group's name as the Mac is told it (PRM_BOSS_CALL for the boss's name)
NAMES = {CREATOR: "小美", BOSS_ID: BOSS, ME: "商务号", MEMBER: "测试达人", ADMIN: "管理员", HELPER: "助手",
         "wxid_old01": "老朋友", TWIN: BOSS, "wxid_boss02": "老板二"}
_IDS = itertools.count(1)


class Backend:
    """The WCDB backend as 老板拉的群 uses it (spec §4 and the reviews' 2026-10-31 additions): the groups of WeChat's
    session list, each with its members (``joined_at`` / ``history_complete`` given when a test sets them; ``unreadable``
    groups are given back as ``{"chat", "unreadable": True}``), WeChat's time now (``wechat_now``: ``wechat``, a value or
    a callable; ``make`` gives it the test's clock), the watches and whether their catch-up ran (``slow``: only once
    ``catch_up`` says the poll ran it; else at once), the private chats WeChat has (``has_private_chat``, quiet)."""

    def __init__(self, *, own=ME, contacts=None, friends=(), slow=False, wechat=None):
        self.own = own
        self.contacts = {BOSS: BOSS_ID} if contacts is None else dict(contacts)
        self.groups = {OLD: {"name": "老群", "members": [ME, BOSS_ID, "wxid_old01"]}}
        self.readable = True
        self.unreadable: set = set()           # groups whose lines or members cannot be read now
        self.friends = set(friends)            # whose private chat WeChat has
        self.slow = slow
        self.wechat = wechat
        self.caught: set = set()
        self.watched, self.unwatched, self.calls = {}, [], []

    def own_wxid(self):
        return self.own

    def wechat_now(self):
        """The newest session time of WeChat's session list (WeChat's clock)."""
        return self.wechat() if callable(self.wechat) else self.wechat

    def db_readable(self):
        return self.readable

    def contact_ids(self, names):
        self.calls.append("contact_ids")
        return {n: (self.contacts.get(n) if self.readable else None) for n in names}

    def group_ids(self):
        self.calls.append("group_ids")
        return set(self.groups) if self.readable else None

    def boss_groups(self, members_any, known, max_members):
        self.calls.append("boss_groups")
        if not self.readable:
            return None
        found = []
        for chat, g in self.groups.items():
            if chat in known:
                continue
            self.calls.append(f"members:{chat}")                   # (the backend reads each group's members)
            if chat in self.unreadable:
                found.append({"chat": chat, "unreadable": True})
                continue
            if set(g["members"]) & set(members_any) and len(g["members"]) <= max_members:
                found.append({"chat": chat, "name": g["name"], "created_at": g.get("created_at"),
                              "members": [{"wxid": w, "name": NAMES.get(w, "")} for w in g["members"]],
                              **{k: g[k] for k in ("joined_at", "history_complete") if k in g}})
        return list(reversed(found))            # newest first

    def group_members(self, chat):
        self.calls.append("group_members")
        g = self.groups.get(chat)
        return None if not self.readable or g is None else [{"wxid": w, "name": NAMES.get(w, "")} for w in g["members"]]

    def watch_group(self, chat, since):
        self.watched[chat] = since
        if self.slow:
            self.caught.discard(chat)
        else:
            self.caught.add(chat)

    def watch_caught_up(self, chat):
        return chat in self.caught

    def catch_up(self, chat=GROUP):
        """The backend's next poll ran the watch's catch-up."""
        self.caught.add(chat)

    def unwatch_group(self, chat):
        self.unwatched.append(chat)
        self.watched.pop(chat, None)

    def has_private_chat(self, wxid):
        self.calls.append("has_private_chat")
        return wxid in self.friends

    def chat_target(self, wxid):
        self.calls.append("chat_target")
        return (NAMES.get(wxid, ""), "") if wxid in self.friends else ("", "不知道她在微信里叫什么")

    def find_group_with(self, member, known):
        """「拉三方群」 looks for the group it made (src/prm/three_party.py ``_find``)."""
        for chat, g in self.groups.items():
            if chat not in known and member in g["members"]:
                return {"chat": chat, "name": g["name"], "members": len(g["members"])}
        return None


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 10, 31, 14, 30, 0, 0, 0, -1)))


def on_wechats_clock(backend, clock):
    """The backend's ``wechat_now`` follows the test's clock (WeChat's and this PC's clocks agree), unless a test set it."""
    if getattr(backend, "wechat", None) is None and callable(getattr(backend, "wechat_now", None)):
        backend.wechat = lambda: clock.now
    return backend


def make(clock, tmp_path, *, on=True, backend=None, stored=None, **prm):
    prm.setdefault("prm_group_with", BOSS)
    b = Bot(clock, tmp_path, prm_boss_groups="on" if on else "off", **prm)
    b.backend = on_wechats_clock(backend or Backend(), clock)
    b.prm.boss_groups.attach(b.backend, stored=stored)
    b.pauses = []
    b.prm._pause = lambda sec: b.pauses.append(sec) or False
    return b


def restart(b, backend=None):
    """The bot restarted: a new PRM service on the same prm.db and relay database, a new backend (its watches gone)."""
    b.prm.stop()
    b.prm = PrmService(b.prm._config, b.relay, store=b.prm.store, clock=b.clock, send=b._send, send_file=b._send_file)
    b.backend = on_wechats_clock(backend or Backend(), b.clock)
    b.prm.boss_groups.attach(b.backend)
    b.prm._pause = lambda sec: b.pauses.append(sec) or False
    return b


@pytest.fixture
def bot(clock, tmp_path):
    b = make(clock, tmp_path)
    yield b
    b.prm.stop()
    b.relay.stop()


def boss_makes(b, members=(ME, BOSS_ID, CREATOR), chat=GROUP, name=GROUP_NAME):
    """The first scan keeps the baseline; the boss then makes ``chat`` (WeChat's line that we were added: ``joined_at``);
    the next scan sees it."""
    if b.prm.store.meta_value(bg.BASELINE) is None:
        assert b.prm.boss_groups.scan() == 0
    b.backend.groups[chat] = {"name": name, "members": list(members), "joined_at": b.clock.now,
                              "history_complete": True, "created_at": b.clock.now}
    b.clock.now += bg.BOSS_SCAN_SEC + 1
    return b.prm.boss_groups.scan()


def gmsg(b, content, *, sender=CREATOR, chat=GROUP, ts=None, msg_type=1, **extra):
    """A line of the group as the backend hands it on (the standardized group message)."""
    return {"message_id": f"g{next(_IDS)}", "chat_id": chat, "group_name": GROUP_NAME, "sender_id": sender,
            "sender_name": NAMES.get(sender, ""), "content": content, "msg_type": msg_type, "is_group": True,
            "timestamp": b.clock.now if ts is None else ts, **extra}


def own(b, text, *, chat=GROUP, ts=None):
    """A line of this account (the own-message hook; a watched group's too)."""
    return {"message_id": f"own{next(_IDS)}", "chat_id": chat, "sender_id": ME, "sender_name": "商务号", "content": text,
            "msg_type": 1, "is_group": chat.endswith("@chatroom"), "timestamp": b.clock.now if ts is None else ts}


def job_of(b, chat=GROUP):
    return b.relay.store.latest_open(chat, drafts_only=True, system=False)


def no_opening(b, chat=GROUP):
    """Her line comes later in these tests: the group's 开场白 job is put away first (as if done with)."""
    job = job_of(b, chat)
    assert job is not None and json.loads(job["meta"])["opening"] == {"how": "boss_group", "by": "boss_group"}
    assert b.relay.store.cancel_if_pending(job["id"], b.clock.now)


def drafted(b, d=None, chat=GROUP):
    """The Mac takes the newest job of ``chat`` and drafts it: the job and its packet."""
    job = job_of(b, chat)
    b.clock.now += SETTLE_SEC + 1
    taken = b.relay.next_job(0, ("prm",))
    assert taken["id"] == job["id"]
    b.finish(job["id"], d or draft(message_kind="faq", reply_draft="您好～我们是品牌的运营，之后由我们跟您对接哈", slots={},
                                    stage_proposal="", reason="kb/合作政策.md"))
    return job, b.prm.store.packet_for_job(job["id"])


def names(b, deal_id):
    return [event_out(e)["name"] for e in reversed(b.prm.store.events(deal_id, limit=50))]


# ── the scan ──────────────────────────────────────────────────────────────

def test_the_first_scan_keeps_the_baseline_and_a_group_from_before_is_never_taken(bot):
    bot.backend.readable = False
    assert bot.prm.boss_groups.scan() is None                       # (the database cannot be read: no baseline yet)
    assert bot.prm.store.meta_value(bg.BASELINE) is None and "读不了" in bot.prm.boss_groups.why_not()
    bot.backend.readable = True
    assert bot.prm.boss_groups.scan() == 0
    assert json.loads(bot.prm.store.meta_value(bg.BASELINE)) == [OLD]
    assert bot.prm.store.boss_groups() == [] and bot.prm.store.parties() == []
    assert bot.prm.boss_groups.scan() == 0                          # OLD has the boss in it: it is not "new"
    assert bot.prm.store.boss_groups() == [] and bot.prm.boss_groups.why_not() == ""
    assert boss_makes(bot) == 1                                     # the boss's new group: taken over
    assert [r["chat"] for r in bot.prm.store.boss_groups()] == [GROUP]


def test_the_scan_runs_once_a_minute_and_says_why_it_cannot(bot):
    assert bot.prm.boss_groups.sweep() == 0                         # at once: the baseline
    assert bot.prm.boss_groups.sweep() is None                      # not due yet
    bot.clock.now += bg.BOSS_SCAN_SEC
    assert bot.prm.boss_groups.sweep() == 0
    bot.backend.own = ""
    bot.clock.now += bg.BOSS_SCAN_SEC
    assert bot.prm.boss_groups.sweep() is None and "wxid" in bot.prm.boss_groups.why_not()
    bot.backend.own, bot.backend.contacts = ME, {BOSS: None}        # no boss of that name (or two)
    bot.clock.now += bg.BOSS_IDS_SEC                                # (the bosses' ids are read again this seldom)
    assert bot.prm.boss_groups.sweep() is None
    assert bot.prm.boss_groups.why_not().startswith("PRM_GROUP_WITH 里的「老板测试」在这个微信里找不到 / 有重名")
    bot.prm.boss_groups.attach(object())                            # a backend that cannot (the Mac's edge)
    assert bot.prm.boss_groups.why_not() == bg.NO_BACKEND_WHY and bot.prm.boss_groups.scan() is None


def test_without_boss_names_the_console_says_where_to_fill_them_in(clock, tmp_path):
    b = Bot(clock, tmp_path, prm_boss_groups="on")
    b.prm.boss_groups.attach(Backend())
    assert b.prm.boss_groups.why_not() == "先在设置里填 PRM_GROUP_WITH（老板在这个微信里的名字）"
    assert b.prm.boss_groups.scan() is None
    assert b.prm.snapshot()["boss_groups"]["why_not"] == bg.NO_BOSS_WHY


def test_exactly_one_person_besides_us_and_the_boss_is_taken_over(bot, clock):
    found = clock.now + bg.BOSS_SCAN_SEC + 1
    assert boss_makes(bot) == 1
    [party] = bot.prm.store.parties()
    assert (party["wxid"], party["name"], party["note"]) == (CREATOR, "小美", f"老板介绍（老板拉的群：{GROUP_NAME}）")
    deal = bot.prm.store.deal_of(party["id"])
    assert deal["three_party"] == {"state": "created", "at": found, "by": "boss", "chat": GROUP, "name": GROUP_NAME,
                                   "members": 3, "why": "老板拉的群"}
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["party_id"], row["deal_id"]) == ("linked", party["id"], deal["id"])
    assert bot.backend.watched == {GROUP: found - bg.BOSS_WATCH_BACK_SEC}
    job = job_of(bot)
    meta = json.loads(job["meta"])
    assert job["text"] == "" and job["chat_id"] == GROUP
    assert meta["opening"] == {"how": "boss_group", "by": "boss_group"} and meta["channel"] == "group"
    assert meta["group"] == {"name": TOLD_NAME, "boss": ["老板"], "lines": []}         # (the boss's name stays here)
    assert BOSS not in job["meta"]
    assert meta["three_party"] == {"at": "10-31 14:31"}
    assert names(bot, deal["id"]) == [f"老板拉群介绍（{GROUP_NAME}）", f"派写开场白的任务 #{job['id']}（老板拉的群）"]
    assert bot.prm.is_party_group(GROUP) and not bot.prm.is_party_group(OLD)
    snap = bot.prm.snapshot()
    shown = snap["parties"][0]
    assert shown["source"] == shown["deal"]["source"] == "boss_group"
    assert (shown["deal"]["three_party"]["by"], shown["deal"]["three_party"]["chat_name"]) == ("boss", GROUP_NAME)
    assert snap["boss_groups"] == {"enabled": True, "why_not": "", "warning": "", "boss": [BOSS], "boss_call": "老板",
                                   "pending": [], "waiting": 0, "linked": 1}


def test_the_opening_always_waits_for_a_person_and_shows_where_it_goes(clock, tmp_path):
    b = make(clock, tmp_path, prm_night_auto_hours="00:00-23:59")
    b.prm.set_auto_mode(True)
    b.prm.store.set_gate("faq", "auto", clock.now)
    boss_makes(b)
    _, packet = drafted(b)
    assert (packet["status"], packet["route"], packet["chat_id"]) == ("pending", "review", GROUP)
    out = b.prm.snapshot()["pending"][0]
    assert (out["channel"], out["group_name"], out["opening"]) == ("group", GROUP_NAME, True)
    assert "老板在群里介绍的达人" in out["reason"] or "老板在群里介绍的达人" in b.prm.store.deal(packet["deal_id"])["attention"]
    b.prm.decide(packet["id"], "send")
    b.prm.send_approved()
    assert b.texts == [(GROUP, "您好～我们是品牌的运营，之后由我们跟您对接哈")]


def test_two_people_besides_us_and_the_boss_wait_for_the_console(bot):
    assert boss_makes(bot, members=(ME, BOSS_ID, CREATOR, MEMBER, ADMIN, HELPER)) == 0
    assert bot.prm.store.parties() == [] and bot.backend.watched == {}
    [pending] = bot.prm.snapshot()["boss_groups"]["pending"]
    assert pending["members"] == [{"wxid": CREATOR, "name": "小美"}, {"wxid": MEMBER, "name": "测试达人"}]
    assert (pending["chat"], pending["name"], pending["size"]) == (GROUP, GROUP_NAME, 6)
    for wxid in (BOSS_ID, ME, ADMIN, HELPER, "wxid_nobody"):
        out = bot.prm.boss_groups.pick(GROUP, wxid)
        assert out["ok"] is False and "只能选这个群里的人" in out["error"], wxid
    out = bot.prm.boss_groups.pick(GROUP, MEMBER)
    party = bot.prm.store.party_by_wxid(MEMBER)
    assert out == {"ok": True, "party_id": party["id"], "deal_id": bot.prm.store.deal_of(party["id"])["id"]}
    assert party["name"] == "测试达人" and bot.prm.is_party_group(GROUP) and GROUP in bot.backend.watched
    assert bot.prm.boss_groups.pick(GROUP, CREATOR)["ok"] is False                       # taken already
    assert names(bot, out["deal_id"])[0] == f"老板拉群介绍（{GROUP_NAME}）"


def test_ignore_puts_a_group_away_for_good(bot):
    boss_makes(bot, members=(ME, BOSS_ID, CREATOR, MEMBER))
    assert bot.prm.boss_groups.ignore(OTHER)["ok"] is False
    assert bot.prm.boss_groups.ignore(GROUP) == {"ok": True}
    assert bot.prm.boss_groups.ignore(GROUP)["ok"] is False and bot.prm.boss_groups.pick(GROUP, CREATOR)["ok"] is False
    assert bot.prm.store.boss_group(GROUP)["state"] == "ignored" and bot.prm.snapshot()["boss_groups"]["pending"] == []
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_group(GROUP)["state"] == "ignored"
    bot.clock.now += bg.IGNORED_KEEP_DAYS * 86400 + 1
    assert bot.prm.boss_groups.sweep_old(bot.clock.now) == 1
    assert bot.prm.store.boss_group(GROUP) is None and GROUP in json.loads(bot.prm.store.meta_value(bg.BASELINE))
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_groups() == []        # never "new" again


def test_a_group_with_nobody_yet_waits_and_she_is_taken_over_when_she_comes_in(bot):
    assert boss_makes(bot, members=(ME, BOSS_ID)) == 0
    assert bot.prm.store.boss_group(GROUP)["state"] == "waiting"
    assert bot.prm.snapshot()["boss_groups"]["waiting"] == 1
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.backend.calls.count("group_members") == 1
    bot.backend.groups[GROUP]["members"].append(CREATOR)            # the boss pulls her in
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 1
    assert bot.prm.store.boss_group(GROUP)["state"] == "linked" and bot.prm.store.party_by_wxid(CREATOR)
    assert job_of(bot) is not None                                  # her group 开场白


def test_a_waiting_group_that_two_join_waits_for_the_console_and_one_nobody_joins_is_dropped(bot):
    boss_makes(bot, members=(ME, BOSS_ID))
    boss_makes(bot, members=(ME, BOSS_ID), chat=OTHER, name="另一个群")
    bot.backend.groups[GROUP]["members"] += [CREATOR, MEMBER]
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    assert bot.prm.store.boss_group(GROUP)["state"] == "pending" and bot.prm.store.parties() == []
    bot.clock.now += bg.BOSS_WAIT_DAYS * 86400
    bot.prm.boss_groups.scan()
    other = bot.prm.store.boss_group(OTHER)
    assert (other["state"], other["why"]) == ("ignored", bg.NOBODY_WHY)


def test_a_registered_creator_only_gets_the_group_linked(bot):
    party = bot.prm.register(CREATOR, "小美美", "测试达人")
    assert boss_makes(bot) == 1
    [again] = bot.prm.store.parties()
    assert (again["id"], again["name"], again["note"]) == (party["id"], "小美美", "测试达人")
    deal = bot.prm.store.deal_of(party["id"])
    assert deal["three_party"]["by"] == "boss" and deal["three_party"]["chat"] == GROUP
    [ev] = bot.prm.store.events(deal["id"], kinds=(bg.EVENT,))
    assert ev["detail"] == {"name": GROUP_NAME, "new": False}


@pytest.mark.parametrize("made", [
    {"state": "unsure", "by": "console"},
    {"state": "created", "by": "console"},                          # 「建好了」 by hand: no group id kept
    {"state": "created", "by": "console", "chat": OTHER, "name": "品牌测试×小美", "members": 3},
    {"state": "created", "by": "boss", "chat": OTHER, "name": "另一个老板拉的群", "members": 3},
], ids=["unsure", "marked made", "made", "another boss group"])
def test_a_group_whose_one_candidate_has_a_three_party_group_is_never_taken_over(bot, made):
    """Review 2026-10-31 (3): 「拉三方群」 makes her, the boss and us a group; when its id is not kept (unsure, 「建好了」 by
    hand) it looks just like the boss's own.  A group whose one candidate has a 三方群 made or unsure is kept ignored
    (「她已经有三方群」): no takeover, no watch, no 开场白, her deal as it was; never looked at again.  (One being made right
    now waits instead: ``test_a_group_found_while_her_three_party_group_is_being_made_waits_and_is_looked_at_again``.)"""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    made = {**made, "at": bot.clock.now}
    bot.prm.store.update_deal(deal_id, bot.clock.now, three_party=made)
    assert boss_makes(bot) == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("ignored", "她已经有三方群") and not bot.prm.is_party_group(GROUP)
    assert bot.prm.store.deal(deal_id)["three_party"] == made and bot.backend.watched == {} and job_of(bot) is None
    assert bot.prm.store.events(deal_id, kinds=(bg.EVENT,)) == []
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_group(GROUP)["state"] == "ignored"


def test_our_own_three_party_group_marked_made_by_hand_is_not_taken_over(bot):
    """Review repro r4: 「拉三方群」 ended unsure (the database did not show the new group in time), the operator clicked
    「建好了」 (ThreeParty.mark: created, no group id), then the group shows up in the session list."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    assert bot.prm.boss_groups.scan() == 0                          # the baseline
    bot.prm.store.update_deal(deal_id, bot.clock.now, three_party={"state": "unsure", "at": bot.clock.now,
                                                                   "by": "console"})
    assert bot.prm.three_party.mark(deal_id, made=True)["ok"] is True
    assert "chat" not in bot.prm.store.deal(deal_id)["three_party"]
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": bot.clock.now,
                                 "history_complete": True}
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    assert bot.prm.store.boss_group(GROUP)["why"] == "她已经有三方群" and not bot.prm.is_party_group(GROUP)
    assert job_of(bot) is None and bot.relay.store.list_jobs(limit=10) == []


@pytest.mark.parametrize("how", ["made meanwhile", "being made: state written", "being made: not written yet"])
def test_the_takeover_looks_at_her_three_party_group_again_right_before_it_links(bot, how):
    """Review 2026-10-31 (3), the race: what the scan saw of her deal may be out of date by the takeover (「拉三方群」 runs
    on the console's thread).  Right before the link, under ThreeParty's lock (``guard``), her deal is read again: a
    三方群 made meanwhile, and the group is ignored; one being made right now (its state written or not yet), and the
    group waits, looked at again once it is done (second review F8: never ignored for it)."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    bot.prm.boss_groups._has_group = lambda wxid: False                 # (the scan saw her deal before the write)
    bot.prm.boss_groups._making = lambda people: False
    if how == "made meanwhile":
        bot.prm.store.update_deal(deal_id, bot.clock.now, three_party={"state": "created", "at": bot.clock.now,
                                                                       "by": "console"})
    elif how == "being made: state written":
        bot.prm.store.update_deal(deal_id, bot.clock.now, three_party={"state": "creating", "at": bot.clock.now,
                                                                       "by": "console"})
    else:
        bot.prm.three_party._busy = deal_id
    assert boss_makes(bot) == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == (("ignored", "她已经有三方群") if how == "made meanwhile" else
                                          ("waiting", bg.MAKING_WHY))
    assert not bot.prm.is_party_group(GROUP)
    assert bot.backend.watched == {} and bot.prm.store.events(deal_id, kinds=(bg.EVENT,)) == []


def test_three_party_start_reads_her_deal_again_under_its_lock(bot, monkeypatch):
    """The other side of the race: a 老板拉的群 linked as her deal's 三方群 after 「拉三方群」 read the deal, before it
    takes the lock: read again under the lock, it makes no second group."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    bot.backend.friends.add(CREATOR)
    admin = SimpleNamespace(can_add_friends=True, create_group_checked=lambda *a, **k: {"ok": True})
    bot.prm.three_party.attach(admin, None, bot.backend)
    spawned = []
    bot.prm.three_party.spawn = spawned.append
    bot.prm.store.update_deal(deal_id, bot.clock.now, three_party={"state": "created", "at": bot.clock.now, "by": "boss",
                                                                   "chat": GROUP, "name": GROUP_NAME, "members": 3})
    real, reads = bot.prm.store.deal, []

    def deal(deal_id):                                              # the first read: before the boss group was linked
        reads.append(deal_id)
        found = real(deal_id)
        return {**found, "three_party": {}} if len(reads) == 1 else found

    monkeypatch.setattr(bot.prm.store, "deal", deal)
    assert bot.prm.three_party.start(deal_id) == {"ok": False, "error": "这位达人的三方群已经建好了"}
    assert spawned == [] and len(reads) == 2


def test_a_console_pick_keeps_her_three_party_group_and_says_so(bot):
    """「是她」 by a person (a group of two candidates): her deal's 三方群 made before stays, the record says so, her lines
    there are drafted all the same."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal = bot.prm.store.deal_of(party["id"])
    made = {"state": "created", "at": bot.clock.now, "by": "console", "chat": OTHER, "name": "品牌测试×小美", "members": 3}
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party=made)
    assert boss_makes(bot, members=(ME, BOSS_ID, CREATOR, MEMBER)) == 0
    assert bot.prm.boss_groups.pick(GROUP, CREATOR)["ok"] is True
    assert bot.prm.store.deal(deal["id"])["three_party"] == made
    assert names(bot, deal["id"])[0] == f"老板拉群介绍（{GROUP_NAME}），她原来的三方群保留"
    assert bot.prm.is_party_group(GROUP)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "好的老板"))
    assert job_of(bot)["text"].endswith("好的老板")


def test_a_creator_an_operator_stopped_is_left_for_the_console(bot):
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    bot.prm.set_active(party["id"], False)
    assert boss_makes(bot) == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("pending", bg.STOPPED_WHY)
    assert bot.prm.store.party_by_wxid(CREATOR) is None             # still stopped
    assert bot.prm.snapshot()["boss_groups"]["pending"][0]["why"] == bg.STOPPED_WHY
    assert bot.prm.boss_groups.pick(GROUP, CREATOR)["ok"] is True    # a person's 「是她」 registers her again
    assert bot.prm.store.party_by_wxid(CREATOR)["active"] == 1


def test_a_restart_reads_the_group_again_from_its_last_line_and_takes_nothing_twice(bot, clock):
    boss_makes(bot)
    no_opening(bot)
    clock.now += 30
    bot.prm.handle(gmsg(bot, "好的老板", ts=clock.now - 5))
    jobs = len(bot.relay.store.list_jobs(limit=100))
    restart(bot)
    assert bot.prm.boss_groups.start() == 1                          # (PrmService.start does it)
    assert bot.backend.watched == {GROUP: clock.now - 5 + 1}         # (the second after its last line handled)
    assert bot.prm.is_party_group(GROUP)
    clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    assert len(bot.prm.store.parties()) == 1 and len(bot.relay.store.list_jobs(limit=100)) == jobs


# ── a linked group's lines ────────────────────────────────────────────────

def test_her_line_in_the_group_is_a_group_draft_job_and_its_packet_goes_to_the_group(bot):
    boss_makes(bot)
    opening = job_of(bot)
    assert bot.prm.handle(gmsg(bot, "老板好，你们家主要是什么产品呀"))
    assert bot.relay.store.get(opening["id"])["status"] == "cancelled"     # she spoke first: no 开场白
    job = job_of(bot)
    meta = json.loads(job["meta"])
    assert job["chat_id"] == GROUP and job["id"] != opening["id"] and "opening" not in meta
    assert meta["channel"] == "group" and meta["group"] == {"name": TOLD_NAME, "boss": ["老板"], "lines": []}
    _, packet = drafted(bot)
    assert packet["chat_id"] == GROUP and packet["meta"]["channel"] == "group"
    assert packet["meta"]["group_name"] == GROUP_NAME                 # (the console's: WeChat's name for it)
    out = bot.prm.snapshot()["pending"][0]
    assert (out["channel"], out["group_name"]) == ("group", GROUP_NAME)
    assert bot.texts == [] and bot.relay_sent == []                   # nothing said by itself


def test_an_opening_the_mac_was_writing_is_outdated_by_her_line(bot):
    boss_makes(bot)
    bot.clock.now += 1
    opening = bot.relay.next_job(0, ("prm",))                         # the Mac took the 开场白
    assert json.loads(bot.relay.store.get(opening["id"])["meta"])["opening"]["by"] == "boss_group"
    bot.prm.handle(gmsg(bot, "在的在的"))
    reply = job_of(bot)
    assert reply["id"] != opening["id"] and reply["text"].endswith("在的在的")
    bot.finish(opening["id"], draft(message_kind="greeting", reply_draft="您好呀～", slots={}, stage_proposal=""))
    assert bot.prm.store.packet_for_job(opening["id"])["status"] == "superseded"


def test_the_boss_and_the_other_members_are_context_for_her_next_job(bot):
    boss_makes(bot, members=(ME, BOSS_ID, CREATOR, MEMBER))
    bot.prm.boss_groups.pick(GROUP, CREATOR)
    no_opening(bot)
    assert bot.prm.handle(gmsg(bot, "大家好，有事打我电话 13812345678", sender=BOSS_ID)) is True
    bot.clock.now += 60
    assert bot.prm.handle(gmsg(bot, "我也在", sender=MEMBER)) is True
    assert job_of(bot) is None and bot.texts == []                    # no job, no word
    bot.clock.now += 60
    bot.prm.handle(gmsg(bot, "好的收到"))
    lines = json.loads(job_of(bot)["meta"])["group"]["lines"]
    assert [(x["who"], x["text"]) for x in lines] == [("老板", "大家好，有事打我电话 〈手机1〉"), ("群成员", "我也在")]
    assert lines[0]["at"] == time.strftime("%m-%d %H:%M", time.localtime(bot.clock.now - 120))
    for n in range(12):                                               # at most 10, each at most 300 characters
        bot.prm.handle(gmsg(bot, f"第{n}句" + "很长" * 200, sender=BOSS_ID))
    lines = bot.prm.boss_groups.job_group(GROUP)["lines"]
    assert len(lines) == bg.LINES_TOLD and lines[-1]["text"].startswith("第11句") and len(lines[-1]["text"]) == 300


def write(b, d=None, chat=GROUP):
    """The Mac takes the newest job of ``chat`` (it is being written now): its id, and ``done()`` hands the draft back."""
    job = job_of(b, chat)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    return job, lambda: b.finish(job["id"], d or draft(message_kind="faq", reply_draft="您好～我们是品牌的运营，之后由我们跟您对接哈",
                                                         slots={}, stage_proposal="", reason="kb/合作政策.md"))


def test_a_reply_sent_drops_only_the_lines_its_job_was_told(bot):
    """Review 2026-10-31 (5): the lines kept for her are dropped by number once a reply that was told them is sent (up
    to the highest number told, second review F7); a line kept after its job was made stays for the next one, whenever
    it was written (the watch's catch-up hands on a line written long before late; before, one written before the job
    was dropped by its time)."""
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "以后由运营跟你对接", sender=BOSS_ID))
    bot.clock.now += 10
    bot.prm.handle(gmsg(bot, "好的"))
    job, done = write(bot)
    assert [x["text"] for x in json.loads(job["meta"])["group"]["lines"]] == ["以后由运营跟你对接"]
    bot.clock.now += 5                                                # while the Mac writes: kept, no draft to redo
    bot.prm.handle(gmsg(bot, "寄样前跟我说一声", sender=BOSS_ID))
    bot.prm.handle(gmsg(bot, "早上那个链接你看下", sender=BOSS_ID, ts=bot.clock.now - 3600))   # written long before
    done()
    packet = bot.prm.store.packet_for_job(job["id"])
    assert packet["meta"]["group_told"] == [1]
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    assert bot.texts == [(GROUP, "您好～我们是品牌的运营，之后由我们跟您对接哈")]
    assert [x["text"] for x in bot.prm.store.boss_group(GROUP)["lines"]] == ["早上那个链接你看下", "寄样前跟我说一声"]
    bot.clock.now += 10
    bot.prm.handle(gmsg(bot, "什么时候寄呀"))
    assert [x["text"] for x in json.loads(job_of(bot)["meta"])["group"]["lines"]] == ["早上那个链接你看下", "寄样前跟我说一声"]


def test_a_boss_line_while_her_group_draft_waits_has_it_drafted_again(bot):
    """Review 2026-10-31 (9): the boss (or another member) says something in the group while her draft there waits in
    待审批 (not approved, not sent): it did not hear it, so it is out of date and her lines are drafted again with the
    group's lines now, as her own new line would.  A draft approved already, and her private draft, stay as they are."""
    bot.backend.friends.add(CREATOR)
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "你们家帽子多少钱"))
    _, packet = drafted(bot)
    bot.clock.now += 30
    bot.prm.handle(bot.msg("私聊也问一下"))                            # her private draft
    _, private = drafted(bot, chat=CREATOR)
    bot.clock.now += 30
    bot.prm.handle(gmsg(bot, "给她按团购价", sender=BOSS_ID))
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    assert bot.prm.store.packet(private["id"])["status"] == "pending"
    again = job_of(bot)
    meta = json.loads(again["meta"])
    assert again["chat_id"] == GROUP and again["text"].endswith("你们家帽子多少钱") and "给她按团购价" not in again["text"]
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("老板", "给她按团购价")]
    assert [s["what"] for s in meta["sent"] if s.get("where") == "group"] == ["failed"]      # (that draft never went)
    _, redone = drafted(bot)
    bot.prm.decide(redone["id"], "send")                              # approved: a later line leaves it be
    bot.prm.handle(gmsg(bot, "我也在", sender=MEMBER))
    assert bot.prm.store.packet(redone["id"])["status"] == "approved" and job_of(bot) is None


def test_a_boss_line_while_the_group_opening_waits_has_the_opening_drafted_again(bot):
    boss_makes(bot)
    _, opening = drafted(bot)                                         # the 开场白, waiting for a person
    bot.clock.now += 30
    bot.prm.handle(gmsg(bot, "她主要带美妆", sender=BOSS_ID))
    assert bot.prm.store.packet(opening["id"])["status"] == "superseded"
    again = json.loads(job_of(bot)["meta"])
    assert again["opening"] == {"how": "boss_group", "by": "boss_group"}
    assert [x["text"] for x in again["group"]["lines"]] == ["她主要带美妆"]


def test_an_operators_own_line_in_the_group_drops_every_line_kept_so_far(bot):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "以后由运营跟你对接", sender=BOSS_ID))
    bot.prm.on_own_message(own(bot, "好的老板，我来跟进"))             # typed in the group by hand
    assert bot.prm.store.boss_group(GROUP)["lines"] == []
    bot.prm.handle(gmsg(bot, "很早以前说的", sender=BOSS_ID, ts=bot.clock.now - 7200))      # kept after it: stays
    assert [x["text"] for x in bot.prm.boss_groups.job_group(GROUP)["lines"]] == ["很早以前说的"]


def test_wechats_own_lines_in_the_group_are_never_drafted_and_her_recall_holds_the_drafts(bot):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    for line in (f"\"{BOSS}\" 撤回了一条消息", "\"小美\" 拍了拍我", "\"老板测试\"邀请\"测试达人\"加入了群聊"):
        assert bot.prm.handle(gmsg(bot, line, sender="", msg_type=10000)) is True
    assert job_of(bot) is None and bot.prm.store.packet(packet["id"])["needs_attention"] is False
    assert bot.prm.handle(gmsg(bot, "\"小美\" 撤回了一条消息", sender="", msg_type=10000)) is True
    held = bot.prm.store.packet(packet["id"])
    assert held["status"] == "pending" and held["needs_attention"] and "撤回" in held["reason"]
    assert job_of(bot) is None


def test_she_left_the_group_her_deal_says_so_until_she_is_back(bot):
    boss_makes(bot)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    bot.backend.groups[GROUP]["members"].remove(CREATOR)
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.scan()
    assert "她已经不在老板拉的群里了" in bot.prm.store.deal(deal_id)["attention"]
    bot.backend.groups[GROUP]["members"].append(CREATOR)
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.scan()
    assert "老板拉的群" not in bot.prm.store.deal(deal_id)["attention"]


# ── sending, read-back, takeover ──────────────────────────────────────────

def test_a_group_draft_goes_to_the_group_in_parts_and_its_read_back_is_ours(clock, tmp_path):
    b = make(clock, tmp_path, prm_reply_parts=3)
    boss_makes(b)
    no_opening(b)
    b.prm.handle(gmsg(b, "均价多少呀"))
    _, packet = drafted(b, draft(message_kind="faq", reply_draft="您好～\n\n均价两三百\n\n您主要带什么呀", slots={},
                                 stage_proposal="", reason="问价"))
    b.prm.decide(packet["id"], "send")
    assert b.prm.send_approved() == 1
    assert b.texts == [(GROUP, "您好～"), (GROUP, "均价两三百"), (GROUP, "您主要带什么呀")] and len(b.pauses) == 2
    for text in ("您好～", "均价两三百", "您主要带什么呀"):
        b.prm.on_own_message(own(b, text))
    sent = b.prm.store.packet(packet["id"])
    assert (sent["status"], sent["outcome"]) == ("sent", "sent_confirmed")
    deal = b.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "copilot" and not b.prm.store.events(deal["id"], kinds=("operator_message",))


def test_after_a_restart_the_group_reply_read_back_is_still_ours(bot, clock):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    clock.now += 20
    restart(bot)                                                     # restarted before the line was read back
    bot.prm.on_own_message(own(bot, "您好～我们是品牌的运营，之后由我们跟您对接哈", ts=clock.now - 15))
    deal = bot.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "copilot" and bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"


def test_an_operators_own_line_in_the_group_takes_her_deal_over(bot):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "以后由运营跟你对接", sender=BOSS_ID))
    bot.prm.handle(gmsg(bot, "好的"))
    _, packet = drafted(bot)
    bot.clock.now += 5
    bot.prm.on_own_message(own(bot, "小美你好，我是品牌的运营"))
    deal = bot.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "human" and bot.prm.store.packet(packet["id"])["status"] == "superseded"
    [ev] = bot.prm.store.events(deal["id"], kinds=("operator_message",))
    assert ev["detail"]["text"].endswith("小美你好，我是品牌的运营")
    assert bot.prm.boss_groups.job_group(GROUP)["lines"] == []       # answered by hand: told no more
    bot.prm.handle(gmsg(bot, "好的谢谢"))
    assert job_of(bot) is None                                       # a person has her deal: only recorded


# ── the gates, 下班时段, 自动模式 ─────────────────────────────────────────────

def plain(b, text="您好～在的"):
    return draft(message_kind="faq", reply_draft=text, slots={}, stage_proposal="", reason="kb/合作政策.md")


def test_a_group_draft_goes_out_by_its_gate(bot):
    bot.prm.store.set_gate("faq", "auto", bot.clock.now)
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot, plain(bot))
    assert (packet["status"], packet["route"]) == ("approved", "auto")
    bot.prm.send_approved()
    assert bot.texts == [(GROUP, "您好～在的")]


@pytest.mark.parametrize("auto", [False, True])
def test_a_group_draft_goes_out_by_the_night_rules(clock, tmp_path, auto):
    clock.now = time.mktime((2026, 10, 31, 21, 30, 0, 0, 0, -1)) if not auto else clock.now
    b = make(clock, tmp_path, prm_night_auto_hours="" if auto else "18:00-09:00")
    if auto:
        b.prm.set_auto_mode(True)
    boss_makes(b)
    no_opening(b)
    b.prm.handle(gmsg(b, "在吗"))
    assert json.loads(job_of(b)["meta"])["night"]
    _, packet = drafted(b, plain(b))
    assert (packet["status"], packet["route"]) == ("approved", "night")
    assert bool(packet["meta"].get("auto_mode")) is auto and packet["meta"]["channel"] == "group"
    b.prm.send_approved()
    assert b.texts == [(GROUP, "您好～在的")]


def test_a_group_never_gets_a_holding_reply(clock, tmp_path):
    b = make(clock, tmp_path, prm_holding_min=10)
    boss_makes(b)
    no_opening(b)
    b.prm.handle(gmsg(b, "在吗"))
    drafted(b, plain(b))
    clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 0 and b.texts == []


# ── her private chat, and where what the bot starts itself goes ───────────

def test_her_private_chat_is_the_same_deal_and_each_reply_goes_where_she_wrote(bot):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "群里先说一声"))
    _, in_group = drafted(bot)
    bot.clock.now += 30
    bot.prm.handle(bot.msg("私聊问一下佣金"))                          # rule 1: her private line, her private chat
    private = job_of(bot, CREATOR)
    meta = json.loads(private["meta"])
    assert private["chat_id"] == CREATOR and "channel" not in meta and "group" not in meta
    assert meta["deal_id"] == in_group["deal_id"]
    assert bot.prm.store.packet(in_group["id"])["status"] == "pending"     # not put out of date by the other chat
    assert [s["where"] for s in meta.get("sent", [])] == []
    _, private_packet = drafted(bot, plain(bot, "佣金按合作政策来哈"), chat=CREATOR)
    assert private_packet["chat_id"] == CREATOR and bot.prm.store.packet(in_group["id"])["status"] == "pending"
    for packet in (in_group, private_packet):
        bot.prm.decide(packet["id"], "send")
        bot.prm.send_approved()
        bot.clock.now += 5
    assert bot.texts == [(GROUP, "您好～我们是品牌的运营，之后由我们跟您对接哈"), (CREATOR, "佣金按合作政策来哈")]
    bot.clock.now += 30
    bot.prm.handle(bot.msg("好的"))
    sent = json.loads(job_of(bot, CREATOR)["meta"])["sent"]
    assert [(s["where"], s["text"]) for s in sent] == [("group", "您好～我们是品牌的运营，之后由我们跟您对接哈"),
                                                        ("private", "佣金按合作政策来哈")]


def test_without_a_boss_group_the_sent_list_has_no_where(bot):
    bot.prm.register(CREATOR, "小美", "测试达人")
    bot.prm.handle(bot.msg("你好"))
    _, packet = drafted(bot, plain(bot), chat=CREATOR)
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    bot.prm.handle(bot.msg("好的"))
    assert [set(s) for s in json.loads(job_of(bot, CREATOR)["meta"])["sent"]] == [{"at", "kind", "what", "text"}]


def ship(b, deal_id, waybill="SF1234567890"):
    out = b.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": waybill})
    assert out["ok"], out
    return b.prm.store.packet(out["packet"]) if out.get("packet") else None


def test_what_the_bot_starts_itself_goes_to_the_group_when_she_has_no_private_chat(bot):
    boss_makes(bot)
    party = bot.prm.store.party_by_wxid(CREATOR)
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    notice = ship(bot, deal_id)                                       # rule 2: a shipping notice
    assert notice["chat_id"] == GROUP and notice["meta"]["channel"] == "group"
    assert bot.prm.snapshot()["pending"][-1]["channel"] == "group"
    bot.prm.decide(notice["id"], "send")
    bot.prm.send_approved()
    assert bot.texts == [(GROUP, "老师您好～样品已经寄出啦：顺丰 SF1234567890。签收之后跟我说一声哦～")]
    job_id = bot.prm._dispatch_step(bot.prm.store.deal(deal_id), party, "签收了：开专属佣金", None, True,
                                    bot.clock.now)                     # rule 2: a 专属佣金 step
    step = bot.relay.store.get(job_id)
    assert step["chat_id"] == GROUP and json.loads(step["meta"])["channel"] == "group"
    assert json.loads(step["meta"])["group"]["name"] == TOLD_NAME and BOSS not in step["meta"]


def test_what_the_bot_starts_itself_goes_to_her_private_chat_once_she_has_one(clock, tmp_path):
    for case in ("wrote", "friend"):
        b = make(clock, tmp_path, backend=Backend(friends=(CREATOR,) if case == "friend" else ()))
        boss_makes(b)
        party = b.prm.store.party_by_wxid(CREATOR)
        deal_id = b.prm.store.deal_of(party["id"])["id"]
        if case == "wrote":
            b.prm.handle(b.msg("加你好友啦"))                           # she wrote to us in private once
        assert ship(b, deal_id)["chat_id"] == CREATOR, case
        job_id = b.prm._dispatch_step(b.prm.store.deal(deal_id), party, "签收了：开专属佣金", None, True, clock.now)
        step = b.relay.store.get(job_id)
        assert step["chat_id"] == CREATOR and "channel" not in json.loads(step["meta"]), case
        b.prm.stop()
        b.relay.stop()


def test_the_consoles_opening_goes_to_the_group_when_she_has_no_private_chat(bot):
    boss_makes(bot)
    no_opening(bot)
    out = bot.prm.ask_opening(bot.prm.store.boss_group(GROUP)["deal_id"])
    job = bot.relay.store.get(out["job"])
    meta = json.loads(job["meta"])
    assert job["chat_id"] == GROUP and meta["channel"] == "group" and meta["opening"]["by"] == "console"


def test_when_she_can_be_reached_nowhere_nothing_is_sent_and_the_deal_says_so(bot):
    boss_makes(bot)
    party = bot.prm.store.party_by_wxid(CREATOR)
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    no_opening(bot)
    bot.backend.groups[GROUP]["members"].remove(CREATOR)              # she left the group, and is no friend of ours
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.scan()
    notice = ship(bot, deal_id)                                       # rule 3
    assert notice["status"] == "failed" and "发不了消息给她" in notice["error"]
    assert any(n["key"] == UNREACHABLE for n in bot.prm.store.deal(deal_id)["notices"])
    bot.prm.send_approved()
    assert bot.texts == []
    assert "发不了消息给她" in bot.prm._step_blocked(bot.prm.store.deal(deal_id), party, bot.clock.now)
    out = bot.prm.ask_opening(deal_id)
    assert out["ok"] is False and "发不了消息给她" in out["error"]
    bot.prm.handle(bot.msg("我加你好友了"))                             # she writes in private: reachable again
    assert not any(n["key"] == UNREACHABLE for n in bot.prm.store.deal(deal_id)["notices"])
    assert ship(bot, deal_id, "SF1234567891")["chat_id"] == CREATOR


def test_a_shipping_notice_made_while_the_bot_was_stopped_still_goes_where_she_is(bot):
    from src.prm.service import apply_deal_action

    boss_makes(bot)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    out = apply_deal_action(bot.prm.store, "shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"},
                            now=bot.clock.now, takeover_sec=7200)     # (the stopped console: straight on the database)
    assert bot.prm.store.packet(out["packet"])["chat_id"] == CREATOR
    bot.prm.decide(out["packet"], "send")
    bot.prm.send_approved()
    assert bot.texts[0][0] == GROUP and bot.prm.store.packet(out["packet"])["chat_id"] == GROUP


# ── 「群已经不在了」 ────────────────────────────────────────────────────────

def test_the_group_gone_stops_reading_it_and_her_lines_there_are_silent_again(bot):
    boss_makes(bot)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    no_opening(bot)
    assert bot.prm.deal_action("group_none", {"deal_id": deal_id}) == {"ok": True, "state": ""}
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("gone", bg.GONE_WHY) and bot.backend.unwatched == [GROUP]
    assert not bot.prm.is_party_group(GROUP) and bot.prm.store.deal(deal_id)["three_party"] == {}
    assert bot.prm.handle(gmsg(bot, "还在吗")) is False and job_of(bot) is None
    assert "老板拉的群不在了（运营说的），群里她的消息不再起草" in names(bot, deal_id)
    assert bot.prm.handle(bot.msg("我在私聊")) is True                  # the deal and her private chat go on
    assert job_of(bot, CREATOR)["text"].endswith("我在私聊")
    snap = bot.prm.snapshot()
    assert snap["boss_groups"]["linked"] == 0 and snap["parties"][0]["source"] == "boss_group"   # (where she came from)
    clock_now = bot.clock.now + bg.BOSS_SCAN_SEC + 1
    bot.clock.now = clock_now
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_group(GROUP)["state"] == "gone"


def test_the_group_gone_fails_its_drafts_not_sent_and_cancels_its_queued_job(bot):
    """Review 2026-11-09: before, a draft approved before 「群已经不在了」 (or written meanwhile) still went into the group.
    A draft for a group goes out only while the bot drafts in it."""
    boss_makes(bot)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    assert bot.prm.decide(packet["id"], "send")["ok"] is True         # approved, not sent yet
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "价格呢"))
    job = job_of(bot)                                                 # queued for the Mac
    assert bot.prm.deal_action("group_none", {"deal_id": deal_id})["ok"] is True
    p = bot.prm.store.packet(packet["id"])
    assert (p["status"], p["error"]) == ("failed", GROUP_CLOSED_TEXT)
    assert bot.relay.store.get(job["id"])["status"] == "cancelled"
    assert bot.prm.send_approved() == 0 and bot.texts == []


def test_bu_jie_le_on_a_boss_group_keeps_it_her_three_party_group_read_only(bot):
    """Review 2026-11-09: the console's 「不接了」 (the admin's 「不接群」 too) on a 老板拉的群 that is her 三方群 stops the
    drafting there only: it stays her 三方群 (as one 「建好了」 by the console), read only; never 「群已经不在了」."""
    bot.prm.three_party.attach(backend=bot.backend)
    boss_makes(bot)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    no_opening(bot)
    before = bot.prm.store.deal(deal_id)["three_party"]
    assert bot.prm.deal_action("group_unlink", {"deal_id": deal_id}) == {"ok": True, "three_party": True,
                                                                        "reading": True, "unsent": 0}
    assert bot.prm.store.deal(deal_id)["three_party"] == {**{k: v for k, v in before.items() if k != "why"},
                                                          "by": "console"}
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("gone", bg.MANUAL_GONE_WHY) and not bot.prm.is_party_group(GROUP)
    assert bot.prm.three_party.deal_of_chat(GROUP)["id"] == deal_id and GROUP in bot.backend.watched
    assert bot.prm.handle(gmsg(bot, "还在吗")) is True and job_of(bot) is None           # read only: never drafted
    assert "不接这个群了（运营说的），群里她的消息不再起草：它还是她的三方群，机器人只读" in names(bot, deal_id)
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_group(GROUP)["state"] == "gone"   # (not taken again)


def test_the_group_gone_of_a_deal_whose_group_is_not_a_boss_group_touches_nothing(bot):
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal = bot.prm.store.deal_of(party["id"])
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party={"state": "created", "at": 1, "by": "console",
                                                                      "chat": OTHER})
    assert bot.prm.deal_action("group_none", {"deal_id": deal["id"]})["ok"] is True
    assert bot.backend.unwatched == [] and bot.prm.store.events(deal["id"], kinds=(bg.EVENT,)) == []


# ── clean-up, logs, off ───────────────────────────────────────────────────

def test_the_deals_clean_up_clears_the_names_of_its_group(bot):
    boss_makes(bot, members=(ME, BOSS_ID, CREATOR, MEMBER))
    out = bot.prm.boss_groups.pick(GROUP, CREATOR)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "以后由运营跟你对接", sender=BOSS_ID))
    deal = bot.prm.store.deal(out["deal_id"])
    bot.prm.purge(deal, bot.clock.now)
    row = bot.prm.store.boss_group(GROUP)
    assert row["name"] == "" and {m["name"] for m in row["members"]} == {""} and row["lines"] == []
    assert row["state"] == "linked" and bot.prm.store.deal(deal["id"])["three_party"] == {
        "state": "created", "at": deal["three_party"]["at"], "by": "boss"}
    assert all("name" not in e["detail"] for e in bot.prm.store.events(deal["id"], kinds=(bg.EVENT,)))


def test_the_logs_have_no_names(bot, caplog):
    caplog.set_level(logging.INFO)
    boss_makes(bot, members=(ME, BOSS_ID, CREATOR, MEMBER))
    bot.prm.boss_groups.pick(GROUP, CREATOR)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "以后由运营跟你对接", sender=BOSS_ID))
    bot.prm.handle(gmsg(bot, "好的"))
    _, packet = drafted(bot)
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    bot.prm.deal_action("group_none", {"deal_id": packet["deal_id"]})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "boss group 20000000…" in text
    for secret in ("小美", BOSS, "测试达人", GROUP_NAME, GROUP, CREATOR, MEMBER, BOSS_ID, "以后由运营跟你对接"):
        assert secret not in text, secret


def test_off_nothing_at_all(clock, tmp_path):
    b = make(clock, tmp_path, on=False)
    store = b.prm.store
    assert b.prm.boss_groups.sweep() is None and b.prm.boss_groups.scan() is None
    assert b.backend.calls == [] and store.meta_value(bg.BASELINE) is None
    party = b.prm.register(CREATOR, "小美", "测试达人")
    store.add_boss_group(GROUP, name=GROUP_NAME, found_at=clock.now, state="linked", members=[], boss=[])
    store.update_boss_group(GROUP, clock.now, party_id=party["id"], deal_id=store.deal_of(party["id"])["id"])
    restart(b)                                                       # (a group linked while it was on)
    assert b.prm.handle(gmsg(b, "在吗")) is False and not b.prm.is_party_group(GROUP)
    b.prm.on_own_message(own(b, "运营亲手回的"))
    assert store.deal_of(party["id"])["mode"] == "copilot"
    assert b.prm.boss_groups.pick(GROUP, CREATOR)["ok"] is False
    assert b.prm.boss_groups.ignore(GROUP)["ok"] is False
    assert b.prm.boss_groups.start() == 0 and b.backend.watched == {}
    snap = b.prm.snapshot()
    assert snap["boss_groups"] == {"enabled": False, "why_not": "", "warning": "", "boss": [BOSS], "boss_call": "老板",
                                   "pending": [], "waiting": 0, "linked": 0}
    out = ship(b, store.deal_of(party["id"])["id"])
    assert out["chat_id"] == CREATOR and "channel" not in out["meta"]
    b.prm.handle(b.msg("你好"))
    meta = json.loads(job_of(b, CREATOR)["meta"])
    assert "channel" not in meta and all("where" not in s for s in meta.get("sent", []))
    b.prm.stop()
    b.relay.stop()


# ── what the backend part asked for ───────────────────────────────────────

def test_a_deals_own_three_party_group_is_never_taken_over(bot):
    """「拉三方群」 makes a group of her, the boss and us: a boss group to look at, never one to take over; its id stays
    known after the deal's clean-up drops it."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    assert bot.prm.boss_groups.scan() == 0
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR]}
    bot.prm.store.update_deal(deal_id, bot.clock.now, three_party={"state": "created", "at": bot.clock.now,
                                                                   "by": "console", "chat": GROUP, "name": GROUP_NAME,
                                                                   "members": 3})
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_groups() == []
    assert GROUP in json.loads(bot.prm.store.meta_value(bg.BASELINE)) and not bot.prm.is_party_group(GROUP)
    bot.prm.purge(bot.prm.store.deal(deal_id), bot.clock.now)
    assert "chat" not in bot.prm.store.deal(deal_id)["three_party"]
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_groups() == []


@pytest.mark.parametrize("state", ["creating", "unsure"])
def test_a_group_of_two_found_while_her_three_party_is_being_made_or_unsure_is_not_taken_over(bot, state):
    """One of two candidates has a 「拉三方群」 that ended unsure: maybe that very group, ignored; one being made right now:
    the group waits and is looked at again once it is done (second review F8)."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    bot.prm.store.update_deal(deal_id, bot.clock.now, three_party={"state": state, "at": bot.clock.now, "by": "console"})
    assert boss_makes(bot, members=(ME, BOSS_ID, CREATOR, MEMBER)) == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == (("ignored", bg.OURS_WHY) if state == "unsure" else ("waiting", bg.MAKING_WHY))
    assert not bot.prm.is_party_group(GROUP)
    assert bot.prm.store.deal(deal_id)["three_party"]["state"] == state and bot.backend.watched == {}


def test_the_watch_starts_at_the_groups_oldest_line_when_the_backend_knows_it(bot, clock):
    """A group the boss made while the bot was stopped, after the baseline: read back from WeChat's line that we were
    added (``joined_at``), at most PRM_CATCHUP_HOURS back."""
    assert bot.prm.boss_groups.scan() == 0                          # the baseline
    clock.now += 2 * 86400                                          # (stopped for two days)
    found = clock.now + bg.BOSS_SCAN_SEC + 1
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": found - 3600}
    bot.backend.groups[OTHER] = {"name": "另一个群", "members": [ME, BOSS_ID, MEMBER], "joined_at": found - 30 * 3600}
    clock.now = found
    assert bot.prm.boss_groups.scan() == 2
    assert bot.backend.watched == {GROUP: found - 3600, OTHER: found - 12 * 3600}   # (at most PRM_CATCHUP_HOURS back)
    party = bot.prm.store.party_by_wxid(CREATOR)
    assert bot.prm.store.deal_of(party["id"])["three_party"]["at"] == found


def timed(group: dict, start: float) -> dict:
    """A test's group keys with its times (seconds from ``start``) as times; True / False / None as they are."""
    return {k: (start + v if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in group.items()}


@pytest.mark.parametrize("group", [
    {"joined_at": -30 * 86400},                                     # we were added a month ago
    {"created_at": -30 * 86400, "history_complete": True},          # its whole history begins a month ago
    {"created_at": -2 * 86400, "history_complete": False},          # a line of two days before was read
], ids=["joined long ago", "began long ago", "a line long ago"])
def test_a_group_clearly_older_than_the_baseline_is_never_taken_over(bot, group):
    """Review 2026-10-31 (4), second review F4: an old group whose chat was deleted is not in the baseline and comes back
    in the session list like a new one.  One begun for us (WeChat's line that we were added, or its oldest line read)
    more than a day (OLD_SEC) before the baseline is kept ignored (「不是新拉的群」), for good."""
    assert bot.prm.boss_groups.scan() == 0                          # the baseline
    start = bot.clock.now
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR],
                                 **timed(group, start)}
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("ignored", "不是新拉的群")
    assert bot.prm.store.parties() == [] and bot.backend.watched == {} and bot.relay.store.list_jobs(limit=10) == []
    assert bot.prm.snapshot()["boss_groups"]["pending"] == []
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.parties() == []


@pytest.mark.parametrize("group", [
    {"created_at": 0, "history_complete": True},                    # a complete history begun now, no line we were added
    {"created_at": -500, "history_complete": True},                 # ... begun just before the baseline
    {"created_at": 0, "history_complete": False},                   # its last lines are new: its start is not known
    {"history_complete": False},                                    # nothing known
    {"joined_at": -3 * 3600, "history_complete": True},             # we were added the day before the baseline
    {"created_at": 0},                                              # a backend that says neither (an older one)
    {"created_at": None},
], ids=["complete, no join line", "began just before", "start unknown", "nothing known", "joined the day before",
        "older backend", "older backend, no line"])
def test_a_group_that_may_be_new_waits_for_the_console(bot, group):
    """Second review 2026-10-31 (F4, repro n3): only a group that WeChat's line says we were added to after the baseline
    is taken over by itself.  Any other boss group, even one whose whole history is recent (an old group whose chat was
    deleted with its history comes back so), waits for a person: pending, 「拿不准是不是新拉的群：是的话点「是她」」 (one
    「是她」 for her alone); nothing is ignored silently.  It is not asked again by the next scans; 「是她」 takes it over."""
    assert bot.prm.boss_groups.scan() == 0
    start = bot.clock.now
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR],
                                 **timed(group, start)}
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("pending", "拿不准是不是新拉的群：是的话点「是她」")
    assert bot.prm.store.parties() == [] and bot.backend.watched == {} and bot.relay.store.list_jobs(limit=10) == []
    [pending] = bot.prm.snapshot()["boss_groups"]["pending"]
    assert (pending["why"], pending["members"]) == (bg.MAYBE_WHY, [{"wxid": CREATOR, "name": "小美"}])
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    asked = bot.backend.calls.count(f"members:{GROUP}")
    assert bot.prm.boss_groups.scan() == 0 and bot.backend.calls.count(f"members:{GROUP}") == asked
    assert bot.prm.boss_groups.pick(GROUP, CREATOR)["ok"] is True and bot.prm.is_party_group(GROUP)


def test_an_old_group_deleted_with_its_history_waits_for_the_console(bot):
    """Second review, repro n3 on the fake backend: the boss's group of a month ago whose chat was deleted with its local
    history comes back when somebody writes: its history is that one line (complete, recent), no line that we were
    added.  Pending (「拿不准」), never taken over by itself; 「这个群不是达人」 puts it away."""
    assert bot.prm.boss_groups.scan() == 0
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": None,
                                 "history_complete": True, "created_at": bot.clock.now + 30}
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    assert bot.prm.store.boss_group(GROUP)["state"] == "pending" and bot.prm.store.parties() == []
    assert bot.prm.boss_groups.ignore(GROUP) == {"ok": True}
    assert bot.prm.store.boss_group(GROUP)["state"] == "ignored"


@pytest.mark.parametrize("group", [
    {"joined_at": -500},                                            # added just before the baseline (within 600 s)
    {"joined_at": 3600, "history_complete": False},                 # added after it: its earlier lines do not matter
    {"joined_at": 3600, "created_at": -30 * 86400, "history_complete": False},     # ... even ones of a month before
], ids=["joined just before", "joined after", "joined after, old lines"])
def test_a_group_we_were_added_to_after_the_baseline_is_taken_over(bot, group):
    assert bot.prm.boss_groups.scan() == 0
    start = bot.clock.now
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR],
                                 **timed(group, start)}
    bot.clock.now += 2 * 3600
    assert bot.prm.boss_groups.scan() == 1 and bot.prm.is_party_group(GROUP)
    assert bot.prm.store.boss_group(GROUP)["joined_at"] == start + group["joined_at"]


def test_lines_the_route_kept_before_the_takeover_are_handed_on_once(clock, tmp_path):
    """WECHAT_GROUPS=* read the group before it was taken over: the route stored its lines and said nothing, the watch
    skips stored lines.  At the takeover they come from the message store: the boss's introduction is context, her
    line a reply job (so no 开场白), and the same line handed on again later is no second job."""
    messages = MessageStore(initialize_db(":memory:"))
    b = make(clock, tmp_path, stored=messages.get_messages_since)
    assert b.prm.boss_groups.scan() == 0
    b.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": clock.now}
    intro = gmsg(b, "大家好，以后由运营跟你对接", sender=BOSS_ID, ts=clock.now)
    hers = gmsg(b, "好的老板，你们家主要卖什么", ts=clock.now + 20)
    ours = dict(gmsg(b, "收到", sender=ME, ts=clock.now + 30))
    for msg in (intro, hers, ours):
        assert messages.insert_message(msg)
    clock.now += bg.BOSS_SCAN_SEC + 1
    assert b.prm.boss_groups.scan() == 1
    [job] = b.relay.store.list_jobs(limit=100)
    meta = json.loads(job["meta"])
    assert job["chat_id"] == GROUP and job["text"].endswith("好的老板，你们家主要卖什么") and "opening" not in meta
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("老板", "大家好，以后由运营跟你对接")]
    assert b.prm.handle(hers) is True
    assert len(b.relay.store.list_jobs(limit=100)) == 1 and job_of(b)["text"].count("你们家主要卖什么") == 1
    b.prm.stop()
    b.relay.stop()


def test_only_the_bosss_introduction_kept_goes_with_the_opening(clock, tmp_path):
    messages = MessageStore(initialize_db(":memory:"))
    b = make(clock, tmp_path, stored=messages.get_messages_since)
    assert b.prm.boss_groups.scan() == 0
    b.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": clock.now}
    assert messages.insert_message(gmsg(b, "大家好，以后由运营跟你对接", sender=BOSS_ID, ts=clock.now))
    clock.now += bg.BOSS_SCAN_SEC + 1
    b.prm.boss_groups.scan()
    meta = json.loads(job_of(b)["meta"])
    assert meta["opening"]["by"] == "boss_group" and [x["text"] for x in meta["group"]["lines"]] == ["大家好，以后由运营跟你对接"]
    b.prm.stop()
    b.relay.stop()


def test_the_bosses_ids_are_read_once_in_ten_minutes(bot):
    for _ in range(5):
        bot.prm.boss_groups.scan()
        bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.backend.calls.count("contact_ids") == 1                # (the first scan only keeps the baseline)
    bot.clock.now += bg.BOSS_IDS_SEC
    bot.prm.boss_groups.scan()
    assert bot.backend.calls.count("contact_ids") == 2


def test_a_group_send_the_backend_refuses_fails_the_draft_and_the_deal_says_so(bot):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    bot.send_ok = SendRefused("这个群只知道编号、不知道群名，机器人找不到它")      # (the backend's GROUP_NO_NAME)
    bot.prm.decide(packet["id"], "send")
    assert bot.prm.send_approved() == 0
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and "不知道群名" in failed["error"]
    notice = next(n for n in bot.prm.store.deal(packet["deal_id"])["notices"] if n["key"] == GROUP_UNSENT)
    assert notice["text"].startswith(f"#{packet['id']} 没发到老板拉的群里")
    bot.send_ok = True
    bot.clock.now += 10
    bot.prm.handle(gmsg(bot, "还在吗"))
    _, again = drafted(bot)
    bot.prm.decide(again["id"], "send")
    assert bot.prm.send_approved() == 1
    assert not any(n["key"] == GROUP_UNSENT for n in bot.prm.store.deal(packet["deal_id"])["notices"])


# ── review 2026-10-31: the fixes ──────────────────────────────────────────

@pytest.mark.parametrize("parts", [1, 3], ids=["one message", "in parts"])
def test_a_confirmed_group_reply_handed_on_again_after_a_restart_is_ours(clock, tmp_path, parts):
    """Review (1), repro r2: our reply in the group was read back (sent_confirmed) before a restart; the watch's
    catch-up hands the same row on again after it.  Our own lines move the group's last line on (the restart watches
    from the second after it), and a line handed on all the same matches the group's packet sent before: never an
    operator's.  Each packet (each part) counts once, and only around its send: the same words in another row, or
    typed later, are a person's."""
    b = make(clock, tmp_path, prm_reply_parts=parts)
    boss_makes(b)
    no_opening(b)
    b.prm.handle(gmsg(b, "在吗"))
    texts = ["您好～", "在的"] if parts > 1 else ["您好～在的"]
    _, packet = drafted(b, plain(b, "\n\n".join(texts)))
    b.prm.decide(packet["id"], "send")
    b.prm.send_approved()
    lines = [own(b, text) for text in texts]
    for line in lines:
        b.prm.on_own_message(line)
    assert b.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert b.prm.store.boss_group(GROUP)["last_line_at"] == lines[-1]["timestamp"]     # (ours too)
    clock.now += 300
    restart(b)
    assert b.prm.boss_groups.start() == 1 and b.backend.watched == {GROUP: lines[-1]["timestamp"] + 1}
    for line in lines:
        b.prm.on_own_message(dict(line))                              # handed on again all the same
    deal = b.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "copilot" and b.prm.store.events(deal["id"], kinds=("operator_message",)) == []
    b.prm.on_own_message(own(b, texts[0], ts=lines[0]["timestamp"] + 2))   # the same words in another row
    assert b.prm.store.deal(packet["deal_id"])["mode"] == "human"
    b.prm.stop()
    b.relay.stop()


def test_after_a_restart_an_operators_own_new_line_in_the_group_still_takes_her_deal_over(bot, clock):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot, plain(bot, "好的"))
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    sent_at = bot.prm.store.packet(packet["id"])["sent_at"]
    bot.prm.on_own_message(own(bot, "好的"))
    clock.now += 600
    restart(bot)
    bot.prm.on_own_message(own(bot, "好的", ts=sent_at + PART_LATE_SEC + 60))     # the same words, typed later
    assert bot.prm.store.deal(packet["deal_id"])["mode"] == "human"


def test_an_unknown_boss_name_stops_every_automatic_takeover_and_says_which(clock, tmp_path):
    """Review (2), repro r7: PRM_GROUP_WITH names two bosses and the second is no one contact's (none, or two of that
    name): the console says which name, and a group of the first boss, the second and us (one 'candidate': the second
    boss) waits for a person instead of registering him as a creator."""
    backend = Backend(contacts={BOSS: BOSS_ID, "老板二": None})
    b = make(clock, tmp_path, backend=backend, prm_group_with=f"{BOSS},老板二")
    assert b.prm.boss_groups.scan() == 0
    assert b.prm.boss_groups.why_not() == b.prm.boss_groups.warning() == ""     # (the first scan keeps the baseline)
    backend.groups[GROUP] = {"name": "老板们", "members": [ME, BOSS_ID, "wxid_boss02x"], "joined_at": clock.now}
    clock.now += bg.BOSS_SCAN_SEC + 1
    assert b.prm.boss_groups.scan() == 0
    why = b.prm.boss_groups.warning()                                 # (second review F3: a warning, not why_not)
    assert why.startswith("PRM_GROUP_WITH 里的「老板二」在这个微信里找不到 / 有重名") and "老板测试" not in why
    assert b.prm.boss_groups.why_not() == ""
    row = b.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("pending", b.prm.boss_groups._unknown_why())
    assert b.prm.store.parties() == [] and backend.watched == {} and b.relay.store.list_jobs(limit=10) == []
    shown = b.prm.snapshot()["boss_groups"]
    assert (shown["why_not"], shown["warning"]) == ("", why)
    backend.groups[OTHER] = {"name": "另一个群", "members": [ME, BOSS_ID, CREATOR], "joined_at": clock.now}
    clock.now += bg.BOSS_SCAN_SEC + 1
    assert b.prm.boss_groups.scan() == 0 and b.prm.store.boss_group(OTHER)["state"] == "pending"   # her too: a person
    assert b.prm.boss_groups.pick(OTHER, CREATOR)["ok"] is True
    b.prm.stop()
    b.relay.stop()


def test_a_member_named_like_a_boss_is_never_a_candidate_and_has_the_bosses_read_again_once(bot):
    assert boss_makes(bot, members=(ME, BOSS_ID, TWIN, CREATOR)) == 1     # (TWIN is named 老板测试: not her)
    assert bot.prm.store.party_by_wxid(TWIN) is None and bot.prm.store.party_by_wxid(CREATOR) is not None
    assert bot.backend.calls.count("contact_ids") == 1
    for _ in range(3):
        bot.clock.now += bg.BOSS_SCAN_SEC + 1
        bot.prm.boss_groups.scan()
    assert bot.backend.calls.count("contact_ids") == 2                # read again once, not each scan
    boss_makes(bot, members=(ME, BOSS_ID, TWIN, CREATOR, MEMBER), chat=OTHER, name="另一个群")
    for wxid in (TWIN, BOSS_ID):
        assert bot.prm.boss_groups.pick(OTHER, wxid)["ok"] is False, wxid
    [pending] = bot.prm.snapshot()["boss_groups"]["pending"]
    assert [m["wxid"] for m in pending["members"]] == [CREATOR, MEMBER]
    assert bot.prm.boss_groups.who(bot.prm.store.boss_group(GROUP), TWIN) == "老板"


def test_the_group_opening_waits_for_the_watchs_catch_up_and_tells_the_bosss_introduction(clock, tmp_path):
    """Review (5), repro r6: the watch hands the group's lines from before the takeover on at the backend's next poll;
    the 开场白 is asked for only once the backend says that catch-up ran (``watch_caught_up``): the Mac hears the boss's
    introduction.  Asked once; it survives a restart in between."""
    b = make(clock, tmp_path, backend=Backend(slow=True))
    assert boss_makes(b) == 1
    assert job_of(b) is None and b.prm.store.boss_group(GROUP)["opening_due"] is not None
    b.prm.boss_groups.sweep()
    assert job_of(b) is None                                          # not read yet
    restart(b, Backend(slow=True))                                    # (a restart in between: still to be asked)
    assert b.prm.boss_groups.start() == 1 and job_of(b) is None
    intro = "这位是小美，以后你们对接，先给她寄3顶"
    b.prm.handle(gmsg(b, intro, sender=BOSS_ID, ts=clock.now - 50))    # the catch-up: the boss's introduction
    b.backend.catch_up()
    b.prm.boss_groups.sweep()                                         # the sender thread's next round
    opening = job_of(b)
    meta = json.loads(opening["meta"])
    assert meta["opening"] == {"how": "boss_group", "by": "boss_group"}
    assert [x["text"] for x in meta["group"]["lines"]] == [intro]
    assert b.prm.store.boss_group(GROUP)["opening_due"] is None
    b.prm.boss_groups.sweep()
    assert [j["id"] for j in b.relay.store.list_jobs(limit=10)] == [opening["id"]]
    b.prm.stop()
    b.relay.stop()


def test_her_own_line_in_the_catch_up_means_no_group_opening(clock, tmp_path):
    b = make(clock, tmp_path, backend=Backend(slow=True))
    boss_makes(b)
    b.prm.handle(gmsg(b, "老板好～我是小美", ts=clock.now - 40))       # her words before the takeover
    b.backend.catch_up()
    b.prm.boss_groups.sweep()
    [job] = b.relay.store.list_jobs(limit=10)
    assert job["text"].endswith("老板好～我是小美") and "opening" not in json.loads(job["meta"])
    b.prm.stop()
    b.relay.stop()


class OldBackend(Backend):
    """A backend that cannot say whether a watch's catch-up ran."""
    watch_caught_up = None


def test_a_backend_that_cannot_say_its_watch_caught_up_gets_the_opening_90_seconds_on(clock, tmp_path):
    b = make(clock, tmp_path, backend=OldBackend())
    assert boss_makes(b) == 1 and job_of(b) is None
    clock.now += bg.OPENING_WAIT_SEC - 1
    b.prm.boss_groups.sweep()
    assert job_of(b) is None
    clock.now += 1
    b.prm.boss_groups.sweep()
    assert json.loads(job_of(b)["meta"])["opening"]["by"] == "boss_group"
    b.prm.stop()
    b.relay.stop()


def test_the_bosss_introduction_kept_after_the_opening_was_asked_for_reaches_her_next_job(bot, clock):
    """Review (5), repro r6's other half: a line written before the opening job but kept after it (handed on late) is
    not dropped when the opening is sent: her next job hears it."""
    boss_makes(bot)
    job, done = write(bot)                                            # the Mac writes the 开场白
    bot.prm.handle(gmsg(bot, "这位是小美，以后你们对接，先给她寄3顶", sender=BOSS_ID, ts=clock.now - 60))
    done()
    opening = bot.prm.store.packet_for_job(job["id"])
    assert opening["meta"]["opening"] and opening["meta"]["group_told"] == []
    bot.prm.decide(opening["id"], "send")
    bot.prm.send_approved()
    assert bot.texts and bot.texts[0][0] == GROUP
    clock.now += 60
    bot.prm.handle(gmsg(bot, "好的谢谢"))
    assert any("寄3顶" in x["text"] for x in json.loads(job_of(bot)["meta"])["group"]["lines"])


def test_off_her_private_line_joins_an_opening_job_of_a_deal_whose_three_party_group_is_made(clock, tmp_path):
    """Review (6), repro r3, the switch OFF: as at f06ba2a, a creator whose 三方群 is made (「建好了」) and who has a
    console 开场白 job not taken yet writes privately: her line joins that job (``_submit``'s 三方群 meta is no longer
    read as the 老板拉的群 she wrote in)."""
    b = Bot(clock, tmp_path)                                          # PRM_BOSS_GROUPS off (the default)
    party = b.prm.register(CREATOR, "小美", "测试达人")
    deal = b.prm.store.deal_of(party["id"])
    b.prm.store.update_deal(deal["id"], clock.now, three_party={"state": "created", "at": clock.now, "by": "console"})
    assert b.prm.ask_opening(deal["id"])["ok"] is True
    opening = b.relay.store.latest_open(CREATOR, drafts_only=True, system=False)
    clock.now += 1
    b.prm.handle(b.msg("在吗"))
    assert b.relay.store.get(opening["id"])["status"] != "cancelled"
    assert b.relay.store.latest_open(CREATOR, drafts_only=True, system=False)["id"] == opening["id"]
    assert len(b.relay.store.list_jobs(limit=10)) == 1
    b.prm.stop()
    b.relay.stop()


def test_her_line_in_the_group_starts_no_round_of_holding_replies_in_her_private_chat(bot, clock):
    """Review (7), repro r9: a 稍等 round is one chat's lines: her line in the group (no 稍等 there) gives her pending
    private draft no second 稍等; her next private line starts a new round there."""
    bot.backend.friends.add(CREATOR)
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(bot.msg("在吗"))
    drafted(bot, chat=CREATOR)
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1
    clock.now += 60
    bot.prm.handle(gmsg(bot, "老板好"))
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 0
    drafted(bot)                                                      # (her group draft: never a 稍等 either)
    clock.now += 60
    bot.prm.handle(bot.msg("还在吗"))
    drafted(bot, chat=CREATOR)
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1
    assert [t for c, t in bot.texts] == ["收到，我确认一下马上回您"] * 2 and {c for c, _ in bot.texts} == {CREATOR}


def test_her_private_chat_is_looked_up_quietly_once_a_minute(bot, caplog):
    """Review (8), repro r8: where a packet the bot starts itself goes is asked every round for a step owed: the
    backend's quiet ``has_private_chat``, once a minute for a creator, never the send path's ``chat_target`` (it logs an
    error with her wxid for a chat it cannot name)."""
    boss_makes(bot)
    party = bot.prm.store.party_by_wxid(CREATOR)
    caplog.set_level(logging.DEBUG)
    for _ in range(12):                                               # a minute of the sender thread's rounds
        assert bot.prm._reach(party)[0] == GROUP
        bot.clock.now += 5
    assert bot.backend.calls.count("has_private_chat") == 1 and "chat_target" not in bot.backend.calls
    bot.backend.friends.add(CREATOR)
    assert bot.prm._reach(party)[0] == CREATOR and bot.backend.calls.count("has_private_chat") == 2
    assert not any(CREATOR in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_bosses_ids_are_read_once_an_hour_and_again_for_another_group_with(bot):
    """Review (10): ``contact_ids`` reads every group's members: once an hour (BOSS_IDS_SEC), and at once when
    PRM_GROUP_WITH changes."""
    for _ in range(5):
        bot.prm.boss_groups.scan()
        bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.backend.calls.count("contact_ids") == 1                # (the first scan only keeps the baseline)
    bot.clock.now += bg.BOSS_IDS_SEC
    bot.prm.boss_groups.scan()
    assert bot.backend.calls.count("contact_ids") == 2
    bot.prm._config.prm_group_with = f"{BOSS},老板二"
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.scan()
    assert bot.backend.calls.count("contact_ids") == 3 and "「老板二」" in bot.prm.boss_groups.warning()
    assert bot.prm.boss_groups.why_not() == ""


def test_a_failed_read_of_the_bosses_is_tried_again_at_the_next_scan(bot):
    calls = []

    def flaky(names):
        calls.append(names)
        if len(calls) == 1:
            raise RuntimeError("the database was closed")
        return {n: bot.backend.contacts.get(n) for n in names}

    bot.backend.contact_ids = flaky
    assert bot.prm.boss_groups.scan() == 0                            # the baseline
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": bot.clock.now}
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() is None and bot.prm.boss_groups.why_not() == bg.NO_READ_WHY
    bot.clock.now += bg.BOSS_SCAN_SEC + 1                             # (not ten minutes on)
    assert bot.prm.boss_groups.scan() == 1 and len(calls) == 2


def test_groups_looked_at_twice_with_no_boss_or_too_many_members_are_not_read_every_minute(bot):
    """Review (10): the backend reads each group's members it is asked about.  A group of the session list it looked
    at twice and gave none of back (no boss in it, or too many members) is left out until the bosses are read again;
    a group waiting for her is still read each scan."""
    crowd = [ME, BOSS_ID, *(f"wxid_guest{n:02d}" for n in range(8))]
    bot.prm.boss_groups.scan()                                        # the baseline
    bot.backend.groups.update({"300000002@chatroom": {"name": "不相干的群", "members": [ME, MEMBER, "wxid_guest99"]},
                               "300000003@chatroom": {"name": "老板的大群", "members": crowd}})
    boss_makes(bot, members=(ME, BOSS_ID))                            # (waiting for her)
    for _ in range(4):
        bot.clock.now += bg.BOSS_SCAN_SEC + 1
        bot.prm.boss_groups.scan()
    assert bot.backend.calls.count("members:300000002@chatroom") == 2
    assert bot.backend.calls.count("members:300000003@chatroom") == 2
    assert bot.backend.calls.count("group_members") == 4              # the waiting group: each scan
    bot.clock.now += bg.BOSS_IDS_SEC                                  # the bosses read again: looked at again
    bot.prm.boss_groups.scan()
    assert bot.backend.calls.count("members:300000002@chatroom") == 3


def test_the_mac_never_hears_the_bosses_names(clock, tmp_path):
    """Review (11), the coordinator's privacy rule: the bosses' names (PRM_GROUP_WITH) never leave this PC.  A group
    job calls him PRM_BOSS_CALL: ``meta.group.boss``, the group's name, the lines it is told, her words and an
    operator's there, our replies told back; the console (this PC) shows the real names."""
    b = make(clock, tmp_path, prm_boss_call="大老板")
    boss_makes(b)
    no_opening(b)
    b.prm.handle(gmsg(b, "我是老板测试，有事找我", sender=BOSS_ID))
    b.prm.handle(gmsg(b, "谢谢老板 测试"))
    job, packet = drafted(b)
    meta = json.loads(job["meta"])
    assert meta["group"]["name"] == "大老板、小美、商务号" and meta["group"]["boss"] == ["大老板"]
    assert [x["text"] for x in meta["group"]["lines"]] == ["我是大老板，有事找我"] and job["text"].endswith("谢谢大老板")
    assert packet["meta"]["group_name"] == GROUP_NAME                 # (the console's)
    b.prm.decide(packet["id"], "send", text="好的，按老板测试说的来")
    b.prm.send_approved()
    b.prm.on_own_message(own(b, "好的，按老板测试说的来"))
    clock.now += 30
    b.prm.on_own_message(own(b, "老板测试让我跟你说一声"))              # an operator, by hand
    deal_id = packet["deal_id"]
    b.prm.store.update_deal(deal_id, clock.now, mode="copilot", mode_until=None)
    clock.now += 30
    b.prm.handle(gmsg(b, "收到"))
    later = job_of(b)
    assert BOSS not in later["text"] and BOSS not in later["meta"] and "大老板" in later["meta"]
    snap = b.prm.snapshot()
    assert (snap["boss_groups"]["boss"], snap["boss_groups"]["boss_call"]) == ([BOSS], "大老板")
    b.prm.stop()
    b.relay.stop()


def test_off_a_group_draft_left_from_when_it_was_on_is_never_sent(clock, tmp_path):
    """Review (12): PRM_BOSS_GROUPS switched off with group drafts still waiting (approved, pending, or one written
    after): none goes out; they fail, 「老板拉的群关了，没发」."""
    b = make(clock, tmp_path)
    boss_makes(b)
    no_opening(b)
    b.prm.handle(gmsg(b, "在吗"))
    _, approved = drafted(b, plain(b, "在的"))
    b.prm.decide(approved["id"], "send")
    clock.now += 30
    b.prm.handle(gmsg(b, "价格多少"))
    _, waiting = drafted(b, plain(b, "两三百"))
    party = b.prm.store.party_by_wxid(CREATOR)
    step = b.prm._dispatch_step(b.prm.store.deal(waiting["deal_id"]), party, "签收了：开专属佣金", None, True,
                                clock.now)                            # (a 专属佣金 step for the group, being written)
    b.prm._config.prm_boss_groups = "off"
    restart(b)
    b.prm._stopping.set()                                             # (its sender thread ends at once)
    b.prm.start()
    for packet in (approved, waiting):
        failed = b.prm.store.packet(packet["id"])
        assert (failed["status"], failed["error"]) == ("failed", "老板拉的群关了，没发"), packet["id"]
    clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == step
    b.finish(step, plain(b, "专属佣金开好啦"))
    left = b.prm.store.packet_for_job(step)
    assert left["chat_id"] == GROUP and left["status"] == "pending"
    b.prm.decide(left["id"], "send")
    assert b.prm.send_approved() == 0
    assert b.prm.store.packet(left["id"])["error"] == "老板拉的群关了，没发" and b.texts == []
    b.relay.stop()


def test_a_step_owed_to_a_creator_who_can_be_reached_nowhere_logs_nothing_round_after_round(bot, caplog):
    """Review (8): she left the boss's group and is no friend of ours; a 专属佣金 step is owed.  The sender thread asks
    every round (5 s) whether it may go: where she can be reached is looked up once a minute, quietly, and nothing is
    logged round after round."""
    boss_makes(bot)
    party = bot.prm.store.party_by_wxid(CREATOR)
    deal = bot.prm.store.deal_of(party["id"])
    no_opening(bot)
    bot.backend.groups[GROUP]["members"].remove(CREATOR)
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.scan()                                        # (her deal: she is not in the group any more)
    bot.prm._owe_step(deal, "签收了", "签收了：开专属佣金", None, False, bot.clock.now)
    caplog.set_level(logging.DEBUG)
    calls = bot.backend.calls.count("has_private_chat")
    for _ in range(12):
        bot.prm.tick()
        bot.clock.now += 5
    assert bot.backend.calls.count("has_private_chat") - calls <= 2 and "chat_target" not in bot.backend.calls
    assert "发不了消息给她" in next(n for n in bot.prm.store.deal(deal["id"])["notices"] if n["key"] == "step")["text"]
    said = [r.getMessage() for r in caplog.records if r.name.startswith("src.prm")]
    assert not any(CREATOR in m for m in said) and len(said) <= 2, said


# ── second review 2026-10-31: the fixes ───────────────────────────────────

@pytest.mark.parametrize("later", [60, 300])
def test_a_group_reply_cut_off_by_a_stop_and_read_back_after_a_late_restart_is_ours(clock, tmp_path, later):
    """F1, repro n1: a group reply in two parts stopped after the first, which was not read back before the stop
    (``indeterminate``: PrmService.start writes the restart's time as its ``sent_at``).  After a restart minutes later
    the watch's catch-up hands that part on: the bot's (a packet not confirmed has no lower bound, as in her private
    chat), no takeover, the packet still unsure (its second part never went out); the same words in another row are a
    person's."""
    b = make(clock, tmp_path, prm_reply_parts=3)
    boss_makes(b)
    no_opening(b)
    b.prm.handle(gmsg(b, "在吗"))
    _, packet = drafted(b, plain(b, "您好～\n\n在的"))
    b.prm.decide(packet["id"], "send")
    b.prm._pause = lambda sec: True                                   # the bot stops in the pause after the first part
    b.prm.send_approved()
    assert b.prm.store.packet(packet["id"])["status"] == "sending" and b.texts == [(GROUP, "您好～")]
    written = clock.now                                               # the first part's time in WeChat
    clock.now += later
    restart(b)
    b.prm._stopping.set()                                             # (its sender thread ends at once)
    b.prm.start()
    unsure = b.prm.store.packet(packet["id"])
    assert (unsure["status"], unsure["outcome"], unsure["sent_at"]) == ("failed", "indeterminate", clock.now)
    b.prm.on_own_message(own(b, "您好～", ts=written))                 # the catch-up hands the first part on
    deal = b.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "copilot" and b.prm.store.events(deal["id"], kinds=("operator_message",)) == []
    assert b.prm.store.packet(packet["id"])["outcome"] == "indeterminate"
    b.prm.on_own_message(own(b, "您好～", ts=written + 5))             # the same words in another row
    assert b.prm.store.deal(packet["deal_id"])["mode"] == "human"


@pytest.mark.parametrize("chat", ["group", "private"])
def test_an_unconfirmed_reply_read_back_after_a_restart_with_this_pcs_clock_ahead_is_ours(clock, tmp_path, chat):
    """F1, repro n1c: a reply sent but not read back before a stop (``sent_unconfirmed``) by a PC whose clock is 10
    minutes ahead of WeChat's; after a restart (the skew not measured again yet) its line comes with WeChat's time, 10
    minutes before the packet's ``sent_at``: the bot's, in the group as in her private chat (as before there)."""
    b = make(clock, tmp_path)
    if chat == "group":
        boss_makes(b)
        no_opening(b)
        b.prm.handle(gmsg(b, "在吗"))
        target = GROUP
    else:
        b.prm.register(CREATOR, "小美", "测试达人")
        b.prm.handle(b.msg("在吗"))
        target = CREATOR
    _, packet = drafted(b, plain(b, "您好～在的"), chat=target)
    b.prm.decide(packet["id"], "send")
    b.prm.send_approved()
    sent = b.prm.store.packet(packet["id"])
    assert (sent["status"], sent["outcome"]) == ("sent", "sent_unconfirmed")
    clock.now += 60
    restart(b)
    b.prm.on_own_message(own(b, "您好～在的", chat=target, ts=sent["sent_at"] - 600))
    deal = b.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "copilot" and b.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"


def test_the_baseline_is_on_wechats_clock_and_a_new_group_is_new_with_this_pcs_clock_hours_off(clock, tmp_path):
    """F2, repro n2: the baseline's time is WeChat's (``wechat_now``: the newest session time, when it was kept) and the
    groups' WeChat times are held against it, never this PC's clock: with this PC 3 hours fast, a group the boss makes
    a minute after the switch's first scan is taken over; one we were added to a month before (WeChat's clock) is old."""
    backend = Backend(wechat=lambda: clock.now - 3 * 3600)           # this PC is 3 hours fast
    b = make(clock, tmp_path, backend=backend)
    assert b.prm.boss_groups.scan() == 0
    wechat = clock.now - 3 * 3600
    assert float(b.prm.store.meta_value(bg.BASELINE_AT)) == wechat
    backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": wechat + 60,
                             "history_complete": True, "created_at": wechat + 60}
    backend.groups[OTHER] = {"name": "上个月的群", "members": [ME, BOSS_ID, MEMBER], "joined_at": wechat - 30 * 86400,
                             "history_complete": True, "created_at": wechat - 30 * 86400}
    clock.now += bg.BOSS_SCAN_SEC + 1
    assert b.prm.boss_groups.scan() == 1 and b.prm.is_party_group(GROUP)
    assert b.prm.store.boss_group(OTHER)["why"] == "不是新拉的群"
    b.prm.stop()
    b.relay.stop()


class NoClockBackend(Backend):
    """A backend that cannot say WeChat's time (no ``wechat_now``: from before the second review)."""
    wechat_now = None


@pytest.mark.parametrize("measured", [True, False], ids=["skew measured", "skew not measured"])
def test_a_backend_without_wechats_time_has_the_baseline_on_this_pcs_clock_less_the_skew(clock, tmp_path, measured):
    """F2 on a backend without ``wechat_now``: the baseline's time is this PC's clock less the skew this run measured.
    Measured, the boss's new group is new; not measured yet (this PC 3 hours fast), it is no longer ignored: it waits for
    the console's 「是她」 (「拿不准」)."""
    b = make(clock, tmp_path, backend=NoClockBackend())
    if measured:
        b.prm._skew = 3 * 3600.0                                      # (this run read one of our lines back: 3 h fast)
    assert b.prm.boss_groups.scan() == 0
    assert float(b.prm.store.meta_value(bg.BASELINE_AT)) == clock.now - (3 * 3600 if measured else 0)
    wechat = clock.now - 3 * 3600
    b.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": wechat + 60,
                               "history_complete": True, "created_at": wechat + 60}
    clock.now += bg.BOSS_SCAN_SEC + 1
    assert b.prm.boss_groups.scan() == (1 if measured else 0)
    row = b.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == (("linked", "") if measured else ("pending", bg.MAYBE_WHY))
    b.prm.stop()
    b.relay.stop()


def test_an_unknown_boss_name_is_a_warning_and_the_groups_wait_for_a_pick(clock, tmp_path):
    """F3, repro n6: PRM_GROUP_WITH names two bosses and the second is no one contact's.  The scan goes on: ``why_not``
    stays empty (the console's buttons are on), ``warning`` says which name, the group of one candidate waits for
    「是她」 (no takeover by itself) and the console's pick takes it over.  A stopped bot has no warning."""
    b = make(clock, tmp_path, prm_group_with=f"{BOSS}，老板二", backend=Backend(contacts={BOSS: BOSS_ID}))
    assert boss_makes(b, members=(ME, BOSS_ID, CREATOR)) == 0
    status = b.prm.boss_groups.status()
    assert status["why_not"] == "" and status["warning"].startswith("PRM_GROUP_WITH 里的「老板二」在这个微信里找不到 / 有重名")
    assert "认出来以前，老板拉的群不会自动接手" in status["warning"] and BOSS not in status["warning"]
    [pending] = status["pending"]
    assert pending["why"] == status["warning"] and [m["wxid"] for m in pending["members"]] == [CREATOR]
    assert b.prm.store.parties() == [] and b.backend.watched == {}
    assert b.prm.boss_groups.pick(GROUP, CREATOR)["ok"] is True and b.prm.is_party_group(GROUP)
    assert bg.boss_groups_out(b.prm.store, enabled=True, why_not="机器人没有运行", boss=[BOSS])["warning"] == ""
    b.backend.contacts = {BOSS: BOSS_ID, "老板二": "wxid_boss02"}       # the name put right: no warning
    clock.now += bg.BOSS_IDS_RETRY_SEC + 1
    b.prm.boss_groups.scan()
    assert b.prm.boss_groups.warning() == "" and b.prm.boss_groups.status()["warning"] == ""
    b.prm.stop()
    b.relay.stop()


def test_no_boss_known_at_all_is_why_the_scan_cannot_run(bot):
    """F3: ``why_not`` only when the scan cannot run at all, as when no name of PRM_GROUP_WITH is a contact's."""
    bot.backend.contacts = {}
    assert bot.prm.boss_groups.scan() == 0                            # the baseline
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() is None
    assert bot.prm.boss_groups.why_not() == ("PRM_GROUP_WITH 里的「老板测试」在这个微信里找不到 / 有重名："
                                             "要和这个微信号里他的备注或名字一样，而且只有一个人叫这个名字")
    assert bot.prm.boss_groups.warning() == ""


def test_a_group_with_no_line_yet_waits_and_is_looked_at_again_until_its_lines_are_there(bot):
    """F5, repro n4: the backend gives a group with no line read yet (its whole history read: none; both times None).
    It waits (counted), the backend is asked for it again at each scan, and once its lines are there it is judged: we
    were added after the baseline: taken over."""
    assert bot.prm.boss_groups.scan() == 0
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": None,
                                 "history_complete": True, "created_at": None}
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    found = bot.clock.now
    assert bot.prm.boss_groups.scan() == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("waiting", bg.NO_LINES_WHY)
    assert bot.prm.snapshot()["boss_groups"]["waiting"] == 1 and bot.prm.store.parties() == []
    asked = bot.backend.calls.count(f"members:{GROUP}")
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.backend.calls.count(f"members:{GROUP}") == asked + 1
    assert bot.backend.calls.count("group_members") == 0               # (asked for through boss_groups only)
    bot.backend.groups[GROUP].update(joined_at=bot.clock.now - 30, created_at=bot.clock.now - 30)   # its lines are there
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 1 and bot.prm.is_party_group(GROUP)
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["found_at"], row["joined_at"]) == ("linked", found, bot.clock.now - bg.BOSS_SCAN_SEC - 31)


@pytest.mark.parametrize("lines", [
    {"joined_at": None, "created_at": "now"},                       # a line, none that we were added: maybe
    {"joined_at": "long ago", "created_at": "long ago"},            # old after all
    None,                                                           # never a line, BOSS_WAIT_DAYS on
], ids=["no join line", "old", "never a line"])
def test_a_group_whose_first_lines_say_it_may_not_be_new_is_never_taken_over(bot, lines):
    assert bot.prm.boss_groups.scan() == 0
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": None,
                                 "history_complete": True, "created_at": None}
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    if lines is None:
        bot.clock.now += bg.BOSS_WAIT_DAYS * 86400 + 1
    else:
        times = {"now": bot.clock.now, "long ago": bot.clock.now - 30 * 86400, None: None}
        bot.backend.groups[GROUP].update({k: times[v] for k, v in lines.items()})
        bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0
    row = bot.prm.store.boss_group(GROUP)
    expected = {"no join line": ("pending", bg.MAYBE_WHY), "old": ("ignored", bg.NOT_NEW_WHY),
                "never a line": ("ignored", bg.QUIET_WHY)}
    assert (row["state"], row["why"]) == expected["no join line" if lines and lines["joined_at"] is None
                                                  else "old" if lines else "never a line"]
    assert bot.prm.store.parties() == [] and bot.backend.watched == {}


class Flaky(Backend):
    """A backend that cannot read GROUP's lines or members for ``misses`` calls: it says so (``unreadable``)."""

    def __init__(self, misses, **kw):
        super().__init__(**kw)
        self.misses = misses

    def boss_groups(self, members_any, known, max_members):
        if GROUP in self.groups and self.misses > 0:
            self.misses -= 1
            self.unreadable.add(GROUP)
        else:
            self.unreadable.discard(GROUP)
        return super().boss_groups(members_any, known, max_members)


def test_a_group_the_backend_cannot_read_now_is_asked_for_again_and_never_counted_as_looked_at(clock, tmp_path):
    """F6, repro n5 (its PRM side): a group whose lines or members cannot be read now comes back as ``{"chat",
    "unreadable": True}``.  It is not kept, never counted as one with no boss (``_note_looked``), and asked for again at
    the next scan: readable from the third scan on, it is taken over then."""
    b = make(clock, tmp_path, backend=Flaky(2))
    assert b.prm.boss_groups.scan() == 0
    b.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": clock.now,
                               "history_complete": True, "created_at": clock.now}
    taken = []
    for n in range(5):                                                # five scans, a minute apart
        clock.now += bg.BOSS_SCAN_SEC + 1
        taken.append(b.prm.boss_groups.scan())
        if n < 2:
            assert b.prm.store.boss_group(GROUP) is None
            assert GROUP not in b.prm.boss_groups._looked | b.prm.boss_groups._once
    assert taken == [0, 0, 1, 0, 0] and b.prm.is_party_group(GROUP)
    b.prm.stop()
    b.relay.stop()


def test_a_reply_sent_drops_every_line_kept_up_to_the_last_its_job_was_told(bot):
    """F7, repro n7: a job is told the newest LINES_TOLD (10) of the lines kept; the reply sent drops every kept line
    numbered up to the highest it was told, the older ones not told too (they came before our reply): her next job is
    not told them as the group's latest lines."""
    boss_makes(bot)
    no_opening(bot)
    for n in range(12):
        bot.clock.now += 1
        bot.prm.handle(gmsg(bot, f"老板的第{n + 1}句", sender=BOSS_ID))
    bot.clock.now += 1
    bot.prm.handle(gmsg(bot, "在吗"))
    job, packet = drafted(bot, plain(bot, "在的～"))
    told = [x["text"] for x in json.loads(job["meta"])["group"]["lines"]]
    assert told == [f"老板的第{n}句" for n in range(3, 13)] and packet["meta"]["group_told"] == list(range(3, 13))
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    assert bot.prm.store.boss_group(GROUP)["lines"] == []
    bot.clock.now += 60
    bot.prm.handle(gmsg(bot, "那寄样怎么弄"))
    assert json.loads(job_of(bot)["meta"])["group"]["lines"] == []


def test_lines_kept_without_a_number_by_an_older_version_go_with_the_next_reply(bot):
    """The review's note: a kept line without ``seq`` (a prm.db an older version wrote) counts as 0 and goes with the
    next reply sent, whatever its job was told."""
    boss_makes(bot)
    no_opening(bot)
    bot.prm.store.update_boss_group(GROUP, bot.clock.now, lines=[{"who": "老板", "at": bot.clock.now - 60,
                                                                  "text": "以前留下的一句"}])
    bot.prm.handle(gmsg(bot, "在吗"))
    job, packet = drafted(bot, plain(bot))
    assert [x["text"] for x in json.loads(job["meta"])["group"]["lines"]] == ["以前留下的一句"]
    assert packet["meta"]["group_told"] == []
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    assert bot.prm.store.boss_group(GROUP)["lines"] == []


def make_groups(bot, result=None):
    """「拉三方群」 wired (the admin handler's ``create_group_checked`` faked: ``result``; a group it makes shows up as
    OTHER); its thread is not run until the test runs ``spawned[0]()``."""
    bot.backend.friends.add(CREATOR)                                  # (her chat's name: ``chat_target``)

    def create(members, dry_run=False):
        if not dry_run and (result or {"ok": True}).get("ok"):
            bot.backend.groups[OTHER] = {"name": "品牌测试×小美", "members": [ME, BOSS_ID, CREATOR]}
        return dict(result or {"ok": True})

    bot.prm.three_party.attach(SimpleNamespace(can_add_friends=True, create_group_checked=create), None, bot.backend)
    spawned = []
    bot.prm.three_party.spawn = spawned.append
    bot.prm.three_party._sleep = lambda sec: None
    return spawned


def test_a_three_party_try_running_never_counts_as_her_group(bot):
    """F8: 「先试一下」 makes nothing.  While it runs (10-30 s on a real PC) the scan takes the boss's group over as ever,
    and the try ending changes nothing of it."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    spawned = make_groups(bot)
    assert bot.prm.three_party.start(deal_id, dry_run=True) == {"ok": True, "state": "trying"}
    assert boss_makes(bot) == 1 and bot.prm.is_party_group(GROUP)
    spawned[0]()                                                      # the try ends
    assert bot.prm.store.deal(deal_id)["three_party"]["chat"] == GROUP and bot.prm.three_party.status()["busy"] is None


@pytest.mark.parametrize("result", [
    {"ok": False, "reason": "not_found", "message": "没在「发起群聊」里找到她"},       # nothing made
    {"ok": False, "reason": "not_created", "message": "窗口没关"},                  # unsure
    {"ok": True, "message": "群建好了"},                                            # made (another group)
], ids=["not made", "unsure", "made"])
def test_a_group_found_while_her_three_party_group_is_being_made_waits_and_is_looked_at_again(bot, result):
    """F8: while 「拉三方群」 makes her 三方群 the boss's group (it may be that very group) waits, never ignored for it, and
    is looked at again at the next scans: nothing made, it is taken over; unsure or made, she has a 三方群 (ignored)."""
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    spawned = make_groups(bot, result)
    assert bot.prm.three_party.start(deal_id) == {"ok": True, "state": "creating"}
    assert boss_makes(bot) == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("waiting", bg.MAKING_WHY) and not bot.prm.is_party_group(GROUP)
    assert bot.prm.snapshot()["boss_groups"]["waiting"] == 1
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    assert bot.prm.boss_groups.scan() == 0 and bot.prm.store.boss_group(GROUP)["state"] == "waiting"   # still making
    spawned[0]()                                                      # 「拉三方群」 ends
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    taken = bot.prm.boss_groups.scan()
    row = bot.prm.store.boss_group(GROUP)
    if result.get("reason") == "not_found":
        assert taken == 1 and row["state"] == "linked" and bot.prm.is_party_group(GROUP)
    else:
        assert taken == 0 and (row["state"], row["why"]) == ("ignored", bg.HAS_GROUP_WHY)
        # the boss's group is not taken; her own 三方群, made, is read only (2026-11-02: its lines go to her private drafts)
        ours = bot.prm.store.deal(deal_id)["three_party"].get("chat")
        assert not bot.prm.is_party_group(GROUP) and set(bot.backend.watched) == ({ours} if ours else set())


def test_a_creator_the_boss_introduced_has_his_name_hidden_in_her_new_friend_catch_up(bot):
    """F9: the takeover registers a creator who is a new friend of ours; her private chat's catch-up (take_new_friend:
    her lines since she was added, 「我是老板测试介绍的」) goes to the Mac with the boss called PRM_BOSS_CALL, as her live
    lines there do."""
    assert boss_makes(bot) == 1
    no_opening(bot)
    assert bot.prm.take_new_friend(CREATOR, [bot.msg("我是老板测试介绍的", ts=bot.clock.now - 60)],
                                   verify_until=bot.clock.now - 120) == 1
    job = job_of(bot, CREATOR)
    assert BOSS not in job["text"] and job["text"].endswith("我是老板介绍的")


def test_her_catch_up_read_while_the_takeover_has_not_linked_her_group_yet_is_masked_too(bot):
    """F9: the catch-up is read by the backend's poll thread; one read after the takeover registered her and before it
    linked the group is masked all the same (``masks``: she is being taken over)."""
    real = bot.prm.boss_groups._register

    def register(wxid, name, note):
        party = real(wxid, name, note)
        assert not bot.prm.boss_groups.party_has_group(party["id"])       # (not linked yet)
        bot.prm.take_new_friend(wxid, [bot.msg("我是老板测试介绍的", ts=bot.clock.now - 60)])
        return party

    bot.prm.boss_groups._register = register
    assert boss_makes(bot) == 1
    job = job_of(bot, CREATOR)
    assert BOSS not in job["text"] and job["text"].endswith("我是老板介绍的")
    assert not bot.prm.boss_groups.masks({"id": 0, "wxid": CREATOR})  # (done: by her linked group from now on)


def test_a_creator_without_a_boss_group_has_her_catch_up_as_before(bot):
    """F9 leaves every other creator's catch-up as it was."""
    party = bot.prm.register(MEMBER, "测试达人", "")
    assert not bot.prm.boss_groups.masks(party)
    bot.prm.take_new_friend(MEMBER, [bot.msg("我是老板测试介绍的", chat=MEMBER, ts=bot.clock.now - 60)])
    assert job_of(bot, MEMBER)["text"].endswith("我是老板测试介绍的")
