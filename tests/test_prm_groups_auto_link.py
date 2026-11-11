"""群自己接上 (PRM_GROUPS_AUTO_LINK, 2026-11-11; src/prm/boss_groups.py ``auto_scan``): the groups link themselves when it
is clear whose they are.

The real case: a creator's own team made a group with us (her account, her live assistant, her 商务; the boss came in
later).  The boss scan had dropped it (no boss in it), nobody sent 「接群」, and the bot never read it.  With the switch on
the scan looks at every group this account is in, old ones too, with at most PRM_BOSS_GROUP_MAX members:

- exactly one member is an active registered creator: the group is linked to her deal as a link by hand is (no 开场白,
  the talk there its context, one draft when her side waits, and that draft always waits), the admin is told once;
- two or more registered creators: never a guess, the console asks and the admin is told once;
- none: nothing by itself; quiet for the groups that were there when the switch first ran, one WeChat line for a group
  that appeared or was active after it;
- a group that cannot be read now is retried, one a person stopped or refused is never linked again, and with the
  switch off nothing changes at all.

The WeChat backend is test_group_link's fake (its ``recent_groups`` taking ``sizes``); the admin handler, the relay and
the PRM service are real.  Every name, id and number is made up: she is 小美, her assistant 测试商务, the boss 老板测试.
"""

import json
import logging
import time

import pytest

from test_group_link import (BD, BIG, GROUP, GROUP_NAME, NAMELESS, OTHER, TESTER, LinkBackend, drafted, gmsg, groups,
                             job_of, names)
from test_prm import ADMIN, CREATOR, Clock
from test_prm_boss_groups import BOSS, BOSS_ID, HELPER, ME, make

from src.admin import AdminCommandHandler
from src.prm import boss_groups as bg
from src.prm import group_link as gl
from src.prm.service import AUTO_GROUP_HELD, HAND_GROUP_HELD, LINK_FIRST_HELD, PrmService

NEW = "400000009@chatroom"                # a group made after the switch went on
NEW_NAME = "小美直播对接"
SECOND = "wxid_creator02"                 # another registered creator
QUIET = {"linked": 0, "asked": 0, "told": 0, "unread": 0}


class AutoBackend(LinkBackend):
    """test_group_link's fake backend, its ``recent_groups`` as the real one's now is (``sizes`` False: no size read)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.names[SECOND] = "测试达人乙"
        self.listings = []                 # each listing asked for: (limit, the groups skipped, sizes)

    def recent_groups(self, limit=10, skip=(), name="", sizes=True):
        self.listings.append((limit, set(skip or ()), sizes))
        out = super().recent_groups(limit, skip, name)
        return out if out is None or sizes else [{**g, "size": None} for g in out]


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 11, 14, 30, 0, 0, 0, -1)))


def setup(clock, tmp_path, *, auto=True, register=True, **prm):
    """The bot with PRM_GROUPS_AUTO_LINK on (PRM_BOSS_GROUPS off unless a test says so), the admin handler wired, the
    session list of test_group_link (her team's group: her, her assistant, a colleague of ours, us; a big group; a
    nameless one of a stranger and us), and 小美 a registered creator."""
    prm.setdefault("on", False)
    b = make(clock, tmp_path, backend=AutoBackend(), prm_groups_auto_link="on" if auto else "off", **prm)
    handler = AdminCommandHandler()
    handler.set_group_links(b.prm.group_links)
    b.admin, b.say = handler, handler.handle
    groups(b)
    if register:
        b.her = b.prm.register(CREATOR, "小美", "测试达人")
    return b


@pytest.fixture
def bot(clock, tmp_path):
    b = setup(clock, tmp_path)
    yield b
    stop(b)


def stop(b):
    b.prm.stop()
    b.relay.stop()


def scan(b, after=bg.BOSS_SCAN_SEC + 1):
    """The next scan, a minute later: what it did."""
    b.clock.now += after
    return b.prm.boss_groups.auto_scan()


def told(b):
    """What the admin's chat was told so far."""
    return [text for chat, text in b.texts if chat == ADMIN]


def new_group(b, members=(ME, CREATOR, BD), chat=NEW, name=NEW_NAME):
    b.backend.groups[chat] = {"name": name, "members": list(members)}
    b.backend.active[chat] = b.clock.now
    return chat


def kept(b):
    return json.loads(b.prm.store.meta_value(bg.AUTO_STATE) or "{}")


# ── exactly one registered creator: the group links itself ────────────────────

def test_a_group_with_exactly_one_registered_creator_links_itself_old_as_it_is(bot):
    """The real case: her team's group from long before the switch (her, her assistant, a colleague, us; no boss, never
    「接群」-ed).  The first scan links it to her deal as a link by hand: no 开场白, the talk its context, one draft of the
    line of hers nobody answered, which waits; the deal says the bot did it and why; the admin is told once."""
    t = bot.clock.now
    say = bot.backend.say
    say(GROUP, CREATOR, "你们家这款还有货吗", t - 3000)
    say(GROUP, ME, "有的，我发您看看", t - 2900)
    say(GROUP, BD, "她这周想先看样", t - 2000)
    say(GROUP, CREATOR, "那寄样怎么弄呀", t - 900)
    assert bot.prm.boss_groups.auto_scan() == {**QUIET, "linked": 1}
    row = bot.prm.store.boss_group(GROUP)
    deal = bot.prm.store.deal_of(bot.her["id"])
    assert (row["state"], row["manual"], row["party_id"], row["deal_id"]) == ("linked", bg.AUTO, bot.her["id"], deal["id"])
    assert bot.prm.is_party_group(GROUP) and bot.prm.boss_groups.by_hand(GROUP) and bot.prm.boss_groups.by_scan(GROUP)
    assert bot.backend.watched[GROUP] == t - 900 + 1                  # the watch: after the newest line read
    assert [p["wxid"] for p in bot.prm.store.parties()] == [CREATOR]  # nobody was registered by it
    assert names(bot, deal["id"])[-1] == f"自动接的群（{GROUP_NAME}）：群里只有她一位登记过的达人"
    [ev] = bot.prm.store.events(deal["id"], kinds=(bg.EVENT,))
    assert ev["actor"] == "system" and ev["detail"] == {"name": GROUP_NAME, "chat": GROUP, "new": False, "manual": True,
                                                        "auto": True, "why": bg.AUTO_WHY}
    job = job_of(bot)
    meta = json.loads(job["meta"])
    assert "opening" not in meta and meta["link_first"] is True and job["text"].endswith("那寄样怎么弄呀")
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [
        ("她", "你们家这款还有货吗"), ("我们", "有的，我发您看看"), ("群成员", "她这周想先看样")]
    assert told(bot) == [
        f"已自动接群（群里只有她一位登记过的达人）：群「{GROUP_NAME}」→ 小美（已登记），合作单 #{deal['id']}；"
        "之后她在群里说的话会起草，在「待审批」里等你看；群里最近 3 句记成了背景；她最后 1 句还没人回，派了一份草稿；"
        f"不对就回「不接群 {GROUP_NAME}」"]
    # the console: listed with her deal as a group the bot linked; no question open
    shown = bot.prm.snapshot()
    assert shown["parties"][0]["deal"]["linked_groups"] == [{"chat": GROUP, "name": GROUP_NAME, "at": t, "auto": True}]
    assert "manual" not in shown["boss_groups"] and shown["boss_groups"]["manual_linked"] == 1
    # its first draft waits for a person; nothing is said twice at the next scans
    _, packet = drafted(bot)
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert scan(bot) == QUIET and scan(bot) == QUIET and len(told(bot)) == 1
    assert GROUP in bot.backend.listings[-1][1] and bot.backend.listings[-1][2] is False   # linked: not listed, no sizes


def test_whether_its_drafts_go_out_by_themselves_is_still_the_linked_groups_switch(clock, tmp_path):
    """Auto-sending in a group the scan linked is PRM_LINKED_GROUPS_AUTO's alone: off, every draft waits and its card
    says the bot linked the group; on, a later line goes by 自动模式, the draft of the lines that waited never does."""
    for on in (False, True):
        b = setup(clock, tmp_path / str(on), **({"prm_linked_groups_auto": "on"} if on else {}))
        try:
            assert b.prm.set_auto_mode(True)["ok"]
            b.backend.say(GROUP, CREATOR, "在吗", b.clock.now - 20)
            assert b.prm.boss_groups.auto_scan()["linked"] == 1
            _, first = drafted(b)
            assert (first["status"], first["route"], first["meta"]["night_held"]) == ("pending", "review", LINK_FIRST_HELD)
            assert b.prm.decide(first["id"], "send")["ok"]             # a person sends it
            b.prm.send_approved()
            b.clock.now += 30
            b.prm.handle(gmsg(b, "你们家帽子多少钱呀"))
            _, later = drafted(b)
            if on:
                assert (later["status"], later["route"]) == ("approved", "night")
            else:
                assert (later["status"], later["route"], later["meta"]["night_held"]) == ("pending", "review",
                                                                                          AUTO_GROUP_HELD)
                assert AUTO_GROUP_HELD != HAND_GROUP_HELD
        finally:
            stop(b)


def test_the_sweep_runs_it_once_a_minute_after_the_boss_scan(bot):
    assert bot.prm.boss_groups.sweep() is None                       # (the boss scan is off: its answer)
    assert bot.prm.is_party_group(GROUP)                             # ... and the groups' scan ran at once
    listed = len(bot.backend.listings)
    bot.clock.now += bg.BOSS_SCAN_SEC - 1
    bot.prm.boss_groups.sweep()
    assert len(bot.backend.listings) == listed                       # not due yet
    bot.clock.now += 1
    bot.prm.tick()                                                   # (PrmService.tick: the sender thread's round)
    assert len(bot.backend.listings) == listed + 1


def test_a_creator_registered_later_gets_her_group_at_the_next_scan(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        assert b.prm.boss_groups.auto_scan() == QUIET and told(b) == []
        reads = b.backend.calls.count("group_members")
        b.prm.register(CREATOR, "小美", "测试达人")
        assert scan(b) == {**QUIET, "linked": 1}
        assert b.backend.calls.count("group_members") == reads       # (who is in it was read already: not again)
        assert b.prm.is_party_group(GROUP) and len(told(b)) == 1
    finally:
        stop(b)


def test_a_group_is_read_again_only_when_something_was_said_in_it_or_after_an_hour(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        reads = b.backend.calls.count("group_members")
        assert reads == 3                                            # her team's group, the big one, the nameless one
        scan(b)
        assert b.backend.calls.count("group_members") == reads
        b.backend.active[GROUP] = b.clock.now                        # something was said in her team's group
        b.backend.active[BIG] = b.clock.now                          # ... and in the big one (never read for that)
        scan(b)
        assert b.backend.calls.count("group_members") == reads + 1
        scan(b, after=bg.AUTO_MEMBERS_SEC)
        assert b.backend.calls.count("group_members") == reads + 4
    finally:
        stop(b)


# ── who counts as the one registered creator ──────────────────────────────────

def test_a_stopped_creator_and_a_cleaned_up_deal_are_not_linked_by_itself(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.prm.set_active(b.her["id"], False)                         # 停用: no creator of ours now
        assert b.prm.boss_groups.auto_scan() == QUIET and b.prm.store.boss_group(GROUP) is None
        b.prm.set_active(b.her["id"], True)
        deal = b.prm.store.deal_of(b.her["id"])
        b.prm.store.update_deal(deal["id"], b.clock.now, purged_at=b.clock.now)   # 8.2: her deal is cleaned up
        assert scan(b) == QUIET and b.prm.store.boss_group(GROUP) is None and told(b) == []
    finally:
        stop(b)


def test_a_creator_with_a_linked_group_already_gets_no_second_one_by_itself(bot):
    """She has a linked group the bot reads: another group with her in it is left to a person (the admin hears of it
    once when it is new), until the first one is stopped."""
    assert bot.prm.boss_groups.auto_scan()["linked"] == 1
    new_group(bot)
    assert scan(bot) == {**QUIET, "told": 1} and bot.prm.store.boss_group(NEW) is None
    assert told(bot)[-1] == (f"新群「{NEW_NAME}」（3 人）还没接（里面的 小美 已经有接手的群，没自动接）："
                             f"回「接群 {NEW_NAME}」")
    assert scan(bot) == QUIET and len(told(bot)) == 2
    assert bot.say(f"不接群 {GROUP_NAME}").startswith("不接了：")
    assert scan(bot) == {**QUIET, "linked": 1}                       # she has none now: the new one is hers
    assert bot.prm.is_party_group(NEW) and not bot.prm.is_party_group(GROUP)


def test_the_bosses_the_admin_and_the_cc_users_are_never_her(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.backend.groups[GROUP]["members"] = [ME, CREATOR, BOSS_ID, ADMIN, HELPER]
        assert b.prm.boss_groups.auto_scan()["linked"] == 1
        row = b.prm.store.boss_group(GROUP)
        assert row["boss"] == [{"wxid": BOSS_ID, "name": BOSS}]
        # a boss in it and her deal had no 三方群: this group is it (as a link by hand's), and says the bot linked it
        group = b.prm.store.deal_of(b.her["id"])["three_party"]
        assert (group["state"], group["by"], group["chat"], group["why"]) == ("created", "manual", GROUP, bg.AUTO_LINK_WHY)
    finally:
        stop(b)


def test_a_group_over_the_size_limit_is_left_alone(clock, tmp_path):
    b = setup(clock, tmp_path, prm_boss_group_max=3)
    try:
        assert b.prm.boss_groups.auto_scan() == QUIET                # her team's group has 4
        b.backend.active[GROUP] = b.clock.now
        assert scan(b, after=bg.AUTO_MEMBERS_SEC) == QUIET and told(b) == []
        b.backend.groups[GROUP]["members"] = [ME, CREATOR, BD]
        assert scan(b, after=bg.AUTO_MEMBERS_SEC) == {**QUIET, "linked": 1}
    finally:
        stop(b)


# ── two or more registered creators: never a guess ────────────────────────────

def test_two_registered_creators_are_one_question_for_a_person_and_one_line(bot):
    bot.prm.register(SECOND, "测试达人乙", "测试")
    bot.backend.groups[GROUP]["members"] = [ME, CREATOR, SECOND, BD]
    assert bot.prm.boss_groups.auto_scan() == {**QUIET, "asked": 1}
    assert bot.prm.store.boss_group(GROUP) is None and not bot.prm.is_party_group(GROUP)
    assert told(bot) == [f"群「{GROUP_NAME}」（4 人）里有 2 位登记过的达人（小美、测试达人乙），机器人不猜是谁的群，没自动接："
                         f"回「接群 {GROUP_NAME}」选一位，或者到控制台「合作单」页顶上选；不是合作群就回「不接」或者不用管"]
    asked_at = bot.clock.now
    assert scan(bot) == QUIET and scan(bot) == QUIET and len(told(bot)) == 1      # once per group
    [shown] = bot.prm.snapshot()["boss_groups"]["manual"]                        # the console's card: until answered
    assert shown == {"chat": GROUP, "name": GROUP_NAME, "size": 4, "asked_at": asked_at, "until": None,
                     "three_party": False, "by": "scan",
                     "members": [{"wxid": CREATOR, "name": "小美", "registered": True, "stopped": False},
                                 {"wxid": SECOND, "name": "测试达人乙", "registered": True, "stopped": False},
                                 {"wxid": BD, "name": "测试商务", "registered": False, "stopped": False}]}
    assert bot.say("是 1") is None                                    # (no question of the admin's own: free words)
    # the admin answers the line as it says: the usual 接群 question, then 是 N
    assert bot.say(f"接群 {GROUP_NAME}").startswith(f"群「{GROUP_NAME}」（4 人）里谁是达人？")
    assert bot.say("是 2").startswith(f"接手了：群「{GROUP_NAME}」→ 测试达人乙（已登记）")
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["manual"]) == ("linked", 1)             # a person linked it: by hand
    assert bot.prm.boss_groups.asked_by_scan(GROUP) is None and "manual" not in bot.prm.snapshot()["boss_groups"]
    assert scan(bot) == QUIET and len(told(bot)) == 1


def test_the_consoles_shi_ta_on_the_scans_question_links_by_hand(bot):
    bot.prm.register(SECOND, "测试达人乙", "测试")
    bot.backend.groups[GROUP]["members"] = [ME, CREATOR, SECOND]
    bot.prm.boss_groups.auto_scan()
    assert bot.prm.boss_groups.pick(GROUP, ME)["ok"] is False         # only a member she may be
    out = bot.prm.boss_groups.pick(GROUP, CREATOR)
    assert out["ok"] is True and out["party_id"] == bot.her["id"]
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["manual"]) == ("linked", 1)
    [ev] = bot.prm.store.events(out["deal_id"], kinds=(bg.EVENT,))
    assert ev["actor"] == "console" and "auto" not in ev["detail"]
    assert scan(bot) == QUIET and bot.prm.boss_groups.asked_by_scan() is None


def test_a_persons_bu_jie_on_the_scans_question_is_remembered(bot):
    """「不接」 on the console's card: the question goes and the scan leaves the group alone for good, also once it has
    exactly one creator; an operator's own 「接群」 still links it."""
    bot.prm.register(SECOND, "测试达人乙", "测试")
    bot.backend.groups[GROUP]["members"] = [ME, CREATOR, SECOND]
    bot.prm.boss_groups.auto_scan()
    assert bot.prm.boss_groups.ignore(GROUP) == {"ok": True}
    assert bot.prm.boss_groups.asked_by_scan(GROUP) is None and GROUP in kept(bot)["no"]
    bot.backend.groups[GROUP]["members"] = [ME, CREATOR, BD]          # (the other creator left: she alone now)
    bot.backend.active[GROUP] = bot.clock.now
    assert scan(bot) == QUIET and bot.prm.store.boss_group(GROUP) is None and len(told(bot)) == 1
    assert GROUP in bot.backend.listings[-1][1]                       # never listed for the scan again
    assert bot.say(f"接群 {GROUP_NAME}").startswith(f"群「{GROUP_NAME}」（3 人）里谁是达人？")
    assert bot.say("是 1").startswith("接手了：") and bot.prm.is_party_group(GROUP)


def test_the_admins_bu_jie_in_wechat_is_remembered_too(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        assert b.say(f"接群 {GROUP_NAME}").startswith(f"群「{GROUP_NAME}」（4 人）里谁是达人？")
        assert b.say("不接") == f"好的，群「{GROUP_NAME}」不接了"
        assert GROUP in kept(b)["no"]
        b.prm.register(CREATOR, "小美", "测试达人")                    # she alone is a creator there now
        b.backend.active[GROUP] = b.clock.now
        assert scan(b) == QUIET and not b.prm.is_party_group(GROUP) and told(b) == []
    finally:
        stop(b)


def test_a_question_that_only_expired_is_no_refusal(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        b.say(f"接群 {GROUP_NAME}")
        b.clock.now += gl.OFFER_SEC + 1                              # nobody answered
        assert b.prm.boss_groups.offered() is None and "no" in kept(b) and kept(b)["no"] == {}
        b.prm.register(CREATOR, "小美", "测试达人")
        assert scan(b) == {**QUIET, "linked": 1}
    finally:
        stop(b)


# ── no registered creator: quiet for the baseline, one line for a new or active group ───

def test_the_groups_there_when_the_switch_went_on_stay_quiet(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        assert b.prm.boss_groups.auto_scan() == QUIET
        assert told(b) == [] and b.prm.store.boss_groups() == [] and b.prm.store.parties() == []
        state = kept(b)
        assert state["at"] == b.clock.now and state["base"] == sorted([GROUP, BIG, NAMELESS])
        assert state["told"] == {} and state["asked"] == {} and state["owed"] == []
        for _ in range(3):
            assert scan(b) == QUIET
        assert told(b) == []
    finally:
        stop(b)


def test_a_group_that_appears_after_the_switch_went_on_is_told_once(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        new_group(b, members=(ME, TESTER, BD))
        assert scan(b) == {**QUIET, "told": 1}
        assert told(b) == [f"新群「{NEW_NAME}」（3 人）还没接：回「接群 {NEW_NAME}」"]
        b.backend.active[NEW] = b.clock.now + 30                     # more is said there: never told twice
        assert scan(b) == QUIET and scan(b) == QUIET and len(told(b)) == 1
        assert b.prm.store.boss_group(NEW) is None                   # nothing was done by itself
        assert b.say(f"接群 {NEW_NAME}").startswith(f"群「{NEW_NAME}」（3 人）里谁是达人？")   # (the answer it names)
    finally:
        stop(b)


def test_a_group_from_before_that_becomes_active_is_told_once(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        b.clock.now += 600
        b.backend.active[GROUP] = b.clock.now                        # somebody writes in her team's old group
        assert scan(b) == {**QUIET, "told": 1}
        assert told(b) == [f"群「{GROUP_NAME}」（4 人）有新消息，还没接：回「接群 {GROUP_NAME}」"]
        b.backend.active[GROUP] = b.clock.now + 60
        assert scan(b) == QUIET and len(told(b)) == 1
    finally:
        stop(b)


def test_a_group_with_nobody_she_may_be_and_a_big_one_get_no_line(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        new_group(b, members=(ME, BOSS_ID, ADMIN), name="内部群")
        new_group(b, members=[ME, *(f"wxid_fan{n:02d}" for n in range(8))], chat=OTHER, name="粉丝大群")
        assert scan(b) == QUIET and told(b) == []
    finally:
        stop(b)


def test_a_nameless_group_points_at_the_list(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        new_group(b, members=(ME, TESTER), name="")
        scan(b)
        assert told(b) == ["一个没有群名的群（2 人）是新的，还没接：发「接群」从单子里选它"]
    finally:
        stop(b)


# ── a group that cannot be read now; a group a person stopped ────────────────

def test_a_group_that_cannot_be_read_now_is_retried_never_taken_for_one_without_a_creator(bot, caplog):
    real = bot.backend.group_members
    bot.backend.group_members = lambda chat: None if chat == GROUP else real(chat)
    assert bot.prm.boss_groups.auto_scan() == {**QUIET, "unread": 1}
    assert bot.prm.store.boss_group(GROUP) is None and told(bot) == [] and GROUP not in kept(bot)["told"]
    assert scan(bot) == {**QUIET, "unread": 1}                       # asked for again at each scan
    bot.backend.group_members = real
    assert scan(bot) == {**QUIET, "linked": 1} and bot.prm.is_party_group(GROUP)


def test_while_the_database_cannot_be_read_nothing_is_kept_not_even_the_baseline(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.backend.readable = False
        assert b.prm.boss_groups.auto_scan() is None and b.prm.store.meta_value(bg.AUTO_STATE) is None
        b.backend.readable = True
        b.backend.own = ""                                           # (who we are is not known yet)
        assert scan(b) is None and b.prm.store.meta_value(bg.AUTO_STATE) is None
        b.backend.own = ME
        assert scan(b) == QUIET and kept(b)["base"] == sorted([GROUP, BIG, NAMELESS])
        b.prm.boss_groups.attach(object())                           # a backend that cannot list groups (the Mac's)
        assert scan(b) is None
    finally:
        stop(b)


@pytest.mark.parametrize("how", ["bu_jie_qun", "bu_jie_le", "group_none"])
def test_a_group_a_person_stopped_is_never_linked_again_by_the_scan(clock, tmp_path, how):
    """「不接群 <群名>」 in WeChat, the console's 「不接了」, 「群已经不在了」: the scan never links that group again, though
    she is still its one registered creator."""
    b = setup(clock, tmp_path)
    try:
        if how == "group_none":                                      # (a boss in it: it is her deal's 三方群)
            b.backend.groups[GROUP]["members"] = [ME, CREATOR, BOSS_ID]
        assert b.prm.boss_groups.auto_scan()["linked"] == 1
        deal = b.prm.store.deal_of(b.her["id"])
        if how == "bu_jie_qun":
            assert b.say(f"不接群 {GROUP_NAME}").startswith("不接了：")
        elif how == "bu_jie_le":
            assert b.prm.deal_action("group_unlink", {"deal_id": deal["id"], "chat": GROUP})["ok"] is True
        else:
            assert b.prm.deal_action("group_none", {"deal_id": deal["id"]})["ok"] is True
        assert b.prm.store.boss_group(GROUP)["state"] == "gone" and not b.prm.is_party_group(GROUP)
        if how != "group_none":
            assert names(b, deal["id"])[-1] == "自动接的群不接了（运营说的），群里她的消息不再起草"
        b.backend.active[GROUP] = b.clock.now + 30                   # she writes there again
        for _ in range(3):
            assert scan(b) == QUIET
        assert b.prm.store.boss_group(GROUP)["state"] == "gone" and len(told(b)) == 1
        # a person may still link it by hand
        assert b.say(f"接群 {GROUP_NAME}").startswith(f"群「{GROUP_NAME}」")
        assert b.say("是 1").startswith("接手了：") and b.prm.store.boss_group(GROUP)["manual"] == 1
    finally:
        stop(b)


# ── the switch off: nothing changes at all ────────────────────────────────────

def test_with_the_switch_off_nothing_changes_at_all(clock, tmp_path):
    b = setup(clock, tmp_path, auto=False)
    try:
        b.backend.say(GROUP, CREATOR, "在吗", b.clock.now - 20)
        new_group(b)
        before = b.prm.snapshot()["boss_groups"]
        assert b.prm.boss_groups.auto_scan() is None
        for _ in range(3):
            b.clock.now += bg.BOSS_SCAN_SEC + 1
            b.prm.tick()
        assert b.backend.listings == [] and "group_members" not in b.backend.calls
        assert b.prm.store.meta_value(bg.AUTO_STATE) is None and b.prm.store.boss_groups() == []
        assert b.texts == [] and job_of(b) is None and not b.prm.is_party_group(GROUP)
        assert b.prm.snapshot()["boss_groups"] == before == {
            "enabled": False, "why_not": "", "warning": "", "boss": [BOSS], "boss_call": "老板", "pending": [],
            "waiting": 0, "linked": 0}
        assert b.prm.handle(gmsg(b, "有人吗")) is False               # her line in the group: silent, as before
        # a person's 「不接」 keeps nothing either, and 接群 by hand works as ever
        b.say(f"接群 {GROUP_NAME}")
        assert b.say("不接") == f"好的，群「{GROUP_NAME}」不接了" and b.prm.store.meta_value(bg.AUTO_STATE) is None
        b.say(f"接群 {GROUP_NAME}")
        assert b.say("是 1").startswith(f"接手了：群「{GROUP_NAME}」→ 小美（已登记）")
        assert b.prm.store.boss_group(GROUP)["manual"] == 1
    finally:
        stop(b)


def test_switched_off_again_the_groups_it_linked_stay_linked(clock, tmp_path):
    b = setup(clock, tmp_path)
    assert b.prm.boss_groups.auto_scan()["linked"] == 1
    b.prm.stop()
    off = dict(vars(b.prm._config), prm_groups_auto_link="off")
    b.prm = PrmService(type(b.prm._config)(**off), b.relay, store=b.prm.store, clock=b.clock, send=b._send,
                       send_file=b._send_file)
    b.backend = AutoBackend()
    groups(b)
    b.prm.boss_groups.attach(b.backend)
    try:
        b.prm.boss_groups.start()
        assert GROUP in b.backend.watched and b.prm.is_party_group(GROUP)
        new_group(b)
        b.clock.now += bg.BOSS_SCAN_SEC + 1
        b.prm.tick()
        assert b.backend.listings == [] and b.prm.store.boss_group(NEW) is None
        b.prm.handle(gmsg(b, "我选好了"))                              # her line there is still drafted
        assert job_of(b)["text"].endswith("我选好了")
    finally:
        stop(b)


# ── the boss scan, a deal's own 三方群 and linking by hand go on as they were ──

def test_the_boss_scan_keeps_its_new_groups_and_its_baseline(clock, tmp_path):
    """PRM_BOSS_GROUPS on too: a group a boss makes with someone nobody registered is the boss scan's (she is
    registered, our 开场白 there), never the groups' scan's; the boss scan's baseline is its own."""
    b = setup(clock, tmp_path, on=True, register=False)
    try:
        assert b.prm.boss_groups.sweep() == 0                        # the boss scan's first run: its baseline
        baseline = b.prm.store.meta_value(bg.BASELINE)
        assert json.loads(baseline) == sorted([GROUP, BIG, NAMELESS]) and told(b) == []
        b.backend.groups[NEW] = {"name": NEW_NAME, "members": [ME, BOSS_ID, TESTER], "joined_at": b.clock.now,
                                 "history_complete": True, "created_at": b.clock.now}
        b.backend.active[NEW] = b.clock.now
        b.clock.now += bg.BOSS_SCAN_SEC + 1
        assert b.prm.boss_groups.sweep() == 1                        # the boss scan took it over
        row = b.prm.store.boss_group(NEW)
        assert (row["state"], row["manual"]) == ("linked", 0)
        assert json.loads(job_of(b, NEW)["meta"])["opening"] == {"how": "boss_group", "by": "boss_group"}
        assert told(b) == [] and b.prm.store.meta_value(bg.BASELINE) == baseline
    finally:
        stop(b)


def test_a_group_the_boss_scan_dropped_as_no_new_one_links_itself_and_one_a_person_ignored_never(clock, tmp_path):
    """The real case's second half: the boss came into her team's old group later; the boss scan keeps it ignored
    (「不是新拉的群」).  That is the scan's own judgement, no person's: the group links itself by its one registered
    creator.  One an operator called 「这个群不是达人」 on the 「老板拉的群」 card is a person's: never."""
    b = setup(clock, tmp_path, on=True)
    try:
        b.prm.boss_groups.sweep()
        assert b.prm.is_party_group(GROUP)                           # (her team's group links itself at once)
        t = b.clock.now
        for chat, name, member in ((NEW, NEW_NAME, SECOND), (OTHER, "测试达人二号的群", TESTER)):
            b.backend.groups[chat] = {"name": name, "members": [ME, BOSS_ID, member, BD], "joined_at": None,
                                      "created_at": t - 30 * 86400}   # (old for us: the boss scan keeps it ignored)
            b.backend.active[chat] = t
        b.prm.register(SECOND, "测试达人乙", "测试")
        b.clock.now += bg.BOSS_SCAN_SEC + 1
        b.prm.boss_groups.sweep()
        assert b.prm.store.boss_group(OTHER)["why"] == bg.NOT_NEW_WHY
        row = b.prm.store.boss_group(NEW)
        assert (row["state"], row["manual"]) == ("linked", bg.AUTO)   # ignored by the boss scan, linked by this one
        # a group waiting on the card that a person calls no creator's is never linked, whoever registers later
        b.backend.groups[OTHER].update(joined_at=b.clock.now, created_at=b.clock.now)
        b.prm.store.update_boss_group(OTHER, b.clock.now, state="pending", why="")
        assert b.prm.boss_groups.ignore(OTHER) == {"ok": True}
        b.prm.register(TESTER, "测试达人二号", "测试")
        b.clock.now += bg.BOSS_SCAN_SEC + 1
        b.prm.boss_groups.sweep()
        assert (b.prm.store.boss_group(OTHER)["state"], b.prm.store.boss_group(OTHER)["why"]) == ("ignored",
                                                                                                 bg.IGNORE_WHY)
    finally:
        stop(b)


def test_a_group_waiting_on_the_boss_card_links_itself_only_by_its_one_registered_creator(clock, tmp_path):
    """A group the boss scan found with two people she may be waits on the 「老板拉的群」 card (「是她」).  One of the two
    is a registered creator: the group is hers.  Nobody of them is: the card goes on asking, nothing is told twice."""
    b = setup(clock, tmp_path, on=True)
    try:
        b.prm.boss_groups.sweep()
        fresh = {"joined_at": b.clock.now, "history_complete": True, "created_at": b.clock.now}
        b.backend.groups[NEW] = {"name": NEW_NAME, "members": [ME, BOSS_ID, TESTER, BD], **fresh}
        b.backend.active[NEW] = b.clock.now
        b.clock.now += bg.BOSS_SCAN_SEC + 1
        b.prm.boss_groups.sweep()
        assert b.prm.store.boss_group(NEW)["state"] == "pending" and len(told(b)) == 1   # (only her team's group's line)
        b.prm.register(TESTER, "测试达人二号", "测试")
        b.clock.now += bg.BOSS_SCAN_SEC + 1
        b.prm.boss_groups.sweep()
        row = b.prm.store.boss_group(NEW)
        assert (row["state"], row["manual"], row["party_id"]) == ("linked", bg.AUTO,
                                                                 b.prm.store.party_by_wxid(TESTER)["id"])
    finally:
        stop(b)


def test_a_deals_own_three_party_group_is_never_looked_at(bot):
    deal = bot.prm.store.deal_of(bot.her["id"])
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party={
        "state": "created", "at": bot.clock.now, "by": "console", "chat": GROUP, "name": GROUP_NAME, "members": 4})
    assert bot.prm.boss_groups.auto_scan() == QUIET and bot.prm.store.boss_group(GROUP) is None
    assert GROUP in bot.backend.listings[-1][1] and told(bot) == []
    assert bot.prm.handle(gmsg(bot, "在吗")) is True and job_of(bot) is None    # read only, as before
    # an operator may still take it over by hand
    assert "的三方群" in bot.say(f"接群 {GROUP_NAME}") and bot.say("是 1").startswith("接手了：")


def test_while_her_three_party_group_is_being_made_the_scan_waits(bot):
    deal = bot.prm.store.deal_of(bot.her["id"])
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party={"state": "creating", "at": bot.clock.now})
    assert bot.prm.boss_groups.auto_scan() == QUIET and bot.prm.store.boss_group(GROUP) is None
    assert scan(bot) == QUIET and told(bot) == [] and kept(bot)["told"] == {}
    # 「拉三方群」 is done and made another group: her team's group links itself, her deal keeps its 三方群
    made = {"state": "created", "at": bot.clock.now, "by": "console", "chat": OTHER, "name": "品牌×小美", "members": 3}
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party=made)
    assert scan(bot) == {**QUIET, "linked": 1}
    assert bot.prm.store.deal_of(bot.her["id"])["three_party"] == made
    [ev] = bot.prm.store.events(deal["id"], kinds=(bg.EVENT,))
    assert ev["detail"]["kept"] is True and ev["detail"]["auto"] is True
    assert "她原来的三方群保留" in told(bot)[0]


def test_linking_by_hand_goes_on_as_it_was_with_the_switch_on(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        assert b.say("接群").split("\n")[1].startswith(f"1. {GROUP_NAME} · 4 人")
        b.say("接群 1")
        answer = b.say("是 2")                                        # her 商务 is who talks: registered by the link
        assert answer.startswith(f"接手了：群「{GROUP_NAME}」→ 测试商务（新登记的达人）")
        row = b.prm.store.boss_group(GROUP)
        assert (row["state"], row["manual"]) == ("linked", 1) and not b.prm.boss_groups.by_scan(GROUP)
        assert scan(b) == QUIET and told(b) == []
    finally:
        stop(b)


# ── the lines for the admin's chat ───────────────────────────────────────────

def test_a_line_wechat_does_not_take_is_tried_again_and_said_once(bot):
    bot.send_ok = False
    assert bot.prm.boss_groups.auto_scan()["linked"] == 1
    assert len(told(bot)) == 1 and len(kept(bot)["owed"]) == 1       # tried, not taken: owed
    bot.send_ok = True
    assert scan(bot) == QUIET
    assert len(told(bot)) == 2 and told(bot)[0] == told(bot)[1] and kept(bot)["owed"] == []
    assert scan(bot) == QUIET and len(told(bot)) == 2


def test_a_line_never_taken_is_dropped_after_six_scans(bot, caplog):
    bot.send_ok = False
    bot.prm.boss_groups.auto_scan()
    with caplog.at_level(logging.ERROR, logger="src.prm.boss_groups"):
        for _ in range(bg.AUTO_TELL_TRIES):
            scan(bot)
    assert len(told(bot)) == bg.AUTO_TELL_TRIES and kept(bot)["owed"] == []
    assert "dropped" in caplog.text and GROUP_NAME not in caplog.text and "小美" not in caplog.text


def test_the_lines_owed_survive_a_restart(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.send_ok = False
    b.prm.boss_groups.auto_scan()
    owed = kept(b)["owed"]
    b.prm.stop()
    b.prm = PrmService(b.prm._config, b.relay, store=b.prm.store, clock=b.clock, send=b._send, send_file=b._send_file)
    b.backend = AutoBackend()
    groups(b)
    b.prm.boss_groups.attach(b.backend)
    b.send_ok = True
    try:
        assert kept(b)["owed"] == owed and len(owed) == 1
        assert scan(b) == QUIET and told(b)[-1] == owed[0][1] and kept(b)["owed"] == []
        assert scan(b) == QUIET and len(told(b)) == 2                # (the try before the restart, and this one)
    finally:
        stop(b)


def test_at_most_three_lines_a_scan(clock, tmp_path):
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.boss_groups.auto_scan()
        for n in range(5):
            new_group(b, members=(ME, f"wxid_guest{n:02d}"), chat=f"40000010{n}@chatroom", name=f"测试群{n}")
        assert scan(b) == {**QUIET, "told": 5} and len(told(b)) == bg.AUTO_TELL_MAX
        assert scan(b) == QUIET and len(told(b)) == 5
        assert len(set(told(b))) == 5
    finally:
        stop(b)


@pytest.mark.parametrize("admin", ["", "500000001@chatroom"])
def test_without_an_admins_chat_the_groups_still_link_and_nothing_is_sent(clock, tmp_path, admin):
    """ADMIN_WXID empty or a group's id: there is no admin's chat to tell.  The group links itself all the same (the
    console shows it), no line goes anywhere, none is kept for later."""
    b = setup(clock, tmp_path, admin_wxid=admin)
    try:
        assert b.prm.boss_groups.auto_scan()["linked"] == 1
        assert b.texts == [] and kept(b)["owed"] == [] and b.prm.is_party_group(GROUP)
    finally:
        stop(b)


def test_the_line_shows_her_platform_id_by_its_last_characters_and_logs_no_name(clock, tmp_path, caplog):
    """Masking as 微信里审批草稿's: a 称呼 that is the friend add's 「抖音-<抖音号>」 by its last characters; a group whose name
    the masking changes cannot be named in the answer, so the line points at the console."""
    b = setup(clock, tmp_path, register=False)
    try:
        b.prm.register(CREATOR, "抖音-testcreator2026", "测试")
        b.backend.groups[GROUP]["name"] = "对接 13800001234"
        with caplog.at_level(logging.DEBUG):
            assert b.prm.boss_groups.auto_scan()["linked"] == 1
        [line] = told(b)
        assert "testcreator2026" not in line and "13800001234" not in line and "抖音-…" in line
        assert line.endswith("不对就到控制台她的合作单上点这个群的「不接了」")
        assert "testcreator2026" not in caplog.text and "13800001234" not in caplog.text
        assert "测试商务" not in caplog.text and CREATOR not in caplog.text
    finally:
        stop(b)


def test_the_scan_never_sends_anything_to_a_group_or_a_creator(bot):
    """What the scan does by itself is reading and linking: the only messages it sends go to the admin's chat."""
    bot.backend.say(GROUP, CREATOR, "在吗", bot.clock.now - 20)
    new_group(bot, members=(ME, TESTER))
    bot.prm.boss_groups.auto_scan()
    for _ in range(3):
        bot.clock.now += bg.BOSS_SCAN_SEC + 1
        bot.prm.tick()
    assert {chat for chat, _ in bot.texts} == {ADMIN} and bot.files == []


def test_one_groups_trouble_never_stops_the_others(bot, caplog):
    real = bot.backend.group_members

    def members(chat):
        if chat == NAMELESS:
            raise RuntimeError("the member list broke")
        return real(chat)

    bot.backend.group_members = members
    with caplog.at_level(logging.INFO, logger="src.prm.boss_groups"):
        assert bot.prm.boss_groups.auto_scan() == {**QUIET, "linked": 1, "unread": 1}
    assert bot.prm.is_party_group(GROUP)
    assert "of 3 looked at, 1 linked by the scan itself" in caplog.text and GROUP_NAME not in caplog.text
    bot.backend.group_members = real
    assert scan(bot) == QUIET                                        # (read now: nobody registered in it, old: quiet)
