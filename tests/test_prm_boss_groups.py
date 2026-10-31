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

import pytest

from test_prm import ADMIN, CREATOR, Bot, Clock, draft

from src.db import MessageStore, initialize_db
from src.prm import boss_groups as bg
from src.prm.service import GROUP_UNSENT, SETTLE_SEC, UNREACHABLE, PrmService, event_out
from src.wechat.window_controller import SendRefused

BOSS = "老板测试"
BOSS_ID = "wxid_boss01"
ME = "wxid_me01"
HELPER = "wxid_helper"                  # a cc user of test_prm's config: never a creator
MEMBER = "wxid_member02"
OLD = "100000001@chatroom"              # a group from before the switch
GROUP = "200000001@chatroom"
OTHER = "300000001@chatroom"
GROUP_NAME = "老板测试、小美、商务号"
NAMES = {CREATOR: "小美", BOSS_ID: BOSS, ME: "商务号", MEMBER: "测试达人", ADMIN: "管理员", HELPER: "助手",
         "wxid_old01": "老朋友"}
_IDS = itertools.count(1)


class Backend:
    """The WCDB backend as 老板拉的群 uses it (spec §4): the groups of WeChat's session list, each with its members."""

    def __init__(self, *, own=ME, contacts=None, friends=()):
        self.own = own
        self.contacts = {BOSS: BOSS_ID} if contacts is None else dict(contacts)
        self.groups = {OLD: {"name": "老群", "members": [ME, BOSS_ID, "wxid_old01"]}}
        self.readable = True
        self.friends = set(friends)            # whose private chat WeChat lists (chat_target names it)
        self.watched, self.unwatched, self.calls = {}, [], []

    def own_wxid(self):
        return self.own

    def contact_ids(self, names):
        self.calls.append("contact_ids")
        return {n: self.contacts.get(n) for n in names}

    def group_ids(self):
        self.calls.append("group_ids")
        return set(self.groups) if self.readable else None

    def boss_groups(self, members_any, known, max_members):
        self.calls.append("boss_groups")
        if not self.readable:
            return None
        found = [{"chat": chat, "name": g["name"], "created_at": g.get("created_at"),
                  "members": [{"wxid": w, "name": NAMES.get(w, "")} for w in g["members"]]}
                 for chat, g in self.groups.items()
                 if chat not in known and set(g["members"]) & set(members_any) and len(g["members"]) <= max_members]
        return list(reversed(found))            # newest first

    def group_members(self, chat):
        self.calls.append("group_members")
        g = self.groups.get(chat)
        return None if not self.readable or g is None else [{"wxid": w, "name": NAMES.get(w, "")} for w in g["members"]]

    def watch_group(self, chat, since):
        self.watched[chat] = since

    def unwatch_group(self, chat):
        self.unwatched.append(chat)
        self.watched.pop(chat, None)

    def chat_target(self, wxid):
        return (NAMES.get(wxid, ""), "") if wxid in self.friends else ("", "不知道她在微信里叫什么")


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 10, 31, 14, 30, 0, 0, 0, -1)))


def make(clock, tmp_path, *, on=True, backend=None, stored=None, **prm):
    b = Bot(clock, tmp_path, prm_boss_groups="on" if on else "off", prm_group_with=BOSS, **prm)
    b.backend = backend or Backend()
    b.prm.boss_groups.attach(b.backend, stored=stored)
    b.pauses = []
    b.prm._pause = lambda sec: b.pauses.append(sec) or False
    return b


def restart(b, backend=None):
    """The bot restarted: a new PRM service on the same prm.db and relay database, a new backend (its watches gone)."""
    b.prm.stop()
    b.prm = PrmService(b.prm._config, b.relay, store=b.prm.store, clock=b.clock, send=b._send, send_file=b._send_file)
    b.backend = backend or Backend()
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
    """The first scan keeps the baseline; the boss then makes ``chat``; the next scan sees it."""
    if b.prm.store.meta_value(bg.BASELINE) is None:
        assert b.prm.boss_groups.scan() == 0
    b.backend.groups[chat] = {"name": name, "members": list(members)}
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
    assert bot.prm.boss_groups.sweep() is None and "认不出老板" in bot.prm.boss_groups.why_not()
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
    assert meta["group"] == {"name": GROUP_NAME, "boss": [BOSS], "lines": []}
    assert meta["three_party"] == {"at": "10-31 14:31"}
    assert names(bot, deal["id"]) == [f"老板拉群介绍（{GROUP_NAME}）", f"派写开场白的任务 #{job['id']}（老板拉的群）"]
    assert bot.prm.is_party_group(GROUP) and not bot.prm.is_party_group(OLD)
    snap = bot.prm.snapshot()
    shown = snap["parties"][0]
    assert shown["source"] == shown["deal"]["source"] == "boss_group"
    assert (shown["deal"]["three_party"]["by"], shown["deal"]["three_party"]["chat_name"]) == ("boss", GROUP_NAME)
    assert snap["boss_groups"] == {"enabled": True, "why_not": "", "boss": [BOSS], "pending": [], "waiting": 0,
                                   "linked": 1}


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


def test_a_deal_with_a_group_made_already_keeps_it_and_says_so(bot):
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal = bot.prm.store.deal_of(party["id"])
    made = {"state": "created", "at": bot.clock.now, "by": "console", "chat": OTHER, "name": "品牌测试×小美", "members": 3}
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party=made)
    assert boss_makes(bot) == 1
    assert bot.prm.store.deal(deal["id"])["three_party"] == made
    assert names(bot, deal["id"])[0] == f"老板拉群介绍（{GROUP_NAME}），她原来的三方群保留"
    assert bot.prm.is_party_group(GROUP)                            # her lines there are drafted all the same
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
    assert bot.backend.watched == {GROUP: clock.now - 5}
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
    assert meta["channel"] == "group" and meta["group"] == {"name": GROUP_NAME, "boss": [BOSS], "lines": []}
    _, packet = drafted(bot)
    assert packet["chat_id"] == GROUP and packet["meta"]["channel"] == "group" and packet["meta"]["group_name"] == GROUP_NAME
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


def test_the_lines_told_start_after_our_last_reply_there(bot):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(gmsg(bot, "以后由运营跟你对接", sender=BOSS_ID))
    bot.clock.now += 10
    bot.prm.handle(gmsg(bot, "好的"))
    _, packet = drafted(bot)
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "寄样前跟我说一声", sender=BOSS_ID))     # after her job was made: still to be told
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    assert bot.texts == [(GROUP, "您好～我们是品牌的运营，之后由我们跟您对接哈")]
    bot.clock.now += 10
    bot.prm.handle(gmsg(bot, "什么时候寄呀"))
    assert [x["text"] for x in json.loads(job_of(bot)["meta"])["group"]["lines"]] == ["寄样前跟我说一声"]


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
    assert bot.texts == [(GROUP, "小美你好～样品已经寄出啦：顺丰 SF1234567890。签收之后跟我说一声哦～")]
    job_id = bot.prm._dispatch_step(bot.prm.store.deal(deal_id), party, "签收了：开专属佣金", None, True,
                                    bot.clock.now)                     # rule 2: a 专属佣金 step
    step = bot.relay.store.get(job_id)
    assert step["chat_id"] == GROUP and json.loads(step["meta"])["channel"] == "group"
    assert json.loads(step["meta"])["group"]["name"] == GROUP_NAME


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
    assert snap["boss_groups"] == {"enabled": False, "why_not": "", "boss": [BOSS], "pending": [], "waiting": 0,
                                   "linked": 0}
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
def test_a_group_found_while_her_three_party_is_being_made_or_unsure_is_not_taken_over(bot, state):
    party = bot.prm.register(CREATOR, "小美", "测试达人")
    deal_id = bot.prm.store.deal_of(party["id"])["id"]
    bot.prm.store.update_deal(deal_id, bot.clock.now, three_party={"state": state, "at": bot.clock.now, "by": "console"})
    assert boss_makes(bot) == 0
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("ignored", bg.OURS_WHY) and not bot.prm.is_party_group(GROUP)
    assert bot.prm.store.deal(deal_id)["three_party"]["state"] == state and bot.backend.watched == {}


def test_the_watch_starts_at_the_groups_oldest_line_when_the_backend_knows_it(bot, clock):
    assert bot.prm.boss_groups.scan() == 0
    found = clock.now + bg.BOSS_SCAN_SEC + 1
    bot.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "created_at": found - 3600}
    bot.backend.groups[OTHER] = {"name": "另一个群", "members": [ME, BOSS_ID, MEMBER], "created_at": found - 30 * 86400}
    clock.now = found
    assert bot.prm.boss_groups.scan() == 2
    assert bot.backend.watched == {GROUP: found - 3600, OTHER: found - 12 * 3600}   # (at most PRM_CATCHUP_HOURS back)
    party = bot.prm.store.party_by_wxid(CREATOR)
    assert bot.prm.store.deal_of(party["id"])["three_party"]["at"] == found


def test_lines_the_route_kept_before_the_takeover_are_handed_on_once(clock, tmp_path):
    """WECHAT_GROUPS=* read the group before it was taken over: the route stored its lines and said nothing, the watch
    skips stored lines.  At the takeover they come from the message store: the boss's introduction is context, her
    line a reply job (so no 开场白), and the same line handed on again later is no second job."""
    messages = MessageStore(initialize_db(":memory:"))
    b = make(clock, tmp_path, stored=messages.get_messages_since)
    assert b.prm.boss_groups.scan() == 0
    b.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR]}
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
    b.backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR]}
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
