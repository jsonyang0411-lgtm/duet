"""她那边的人 (PRM_GROUP_HER_SIDE, 2026-11-11; PrmService._group_message, BossGroups.her_side): in a linked group her team's
lines count as hers.

The real case: in a creator's group it was her ASSISTANT who wrote 「帽子到了，上一下链接」.  Only the registered account's
own lines are drafted, so even in a linked group no draft would have come.  With the switch on, in a linked group (a
老板拉的群, one linked by hand, one the scan linked) a line of any member who is not ours (us, a boss, the admin, a cc
user) and not another registered creator is drafted as her own line is: the same job, gates, 下班时段, 自动模式, redraft
and bookkeeping.  The Mac is told it came from her side (〔她那边的人〕 before the line, ``meta.group.her_side``), never
that person's name.  With the switch off everything is as before: other members' lines are context only.

The backend is test_group_link's fake; the relay and the PRM service are real.  Every name, id and number is made up:
she is 小美, her assistant 测试商务.
"""

import json
import logging
import time

import pytest

from test_group_link import (BD, COLLEAGUE, GROUP, GROUP_NAME, drafted, gmsg, held, job_of, linked, recall,
                             setup as link_setup)
from test_prm import ADMIN, CREATOR, Clock
from test_prm_boss_groups import BOSS_ID, HELPER, ME
from test_prm_boss_groups import GROUP as BOSS_GROUP, boss_makes, gmsg as boss_gmsg, make, no_opening
from test_prm_groups_auto_link import setup as auto_setup, told

from src.prm import boss_groups as bg
from src.prm.service import HAND_GROUP_HELD, HER_SIDE_HELD, SETTLE_SEC

MARK = bg.HER_SIDE_MARK
SECOND = "wxid_creator02"                 # another registered creator in the same group
ASSISTANT = "wxid_assist02"               # her live assistant, a member of her group (another one of her side)
ASK = "帽子到了，上一下链接"


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 11, 14, 30, 0, 0, 0, -1)))


def setup(clock, tmp_path, side=True, **prm):
    """test_group_link's bot (her team's group: her, her assistant, a colleague, us), PRM_GROUP_HER_SIDE on."""
    return link_setup(clock, tmp_path, **({"prm_group_her_side": "on"} if side else {}), **prm)


@pytest.fixture
def bot(clock, tmp_path):
    b = setup(clock, tmp_path)
    yield b
    stop(b)


def stop(b):
    b.prm.stop()
    b.relay.stop()


def stamp(at):
    return time.strftime("%m-%d %H:%M", time.localtime(at))


# ── the switch off: exactly as before ─────────────────────────────────────────

def test_switched_off_her_assistants_line_is_context_only(clock, tmp_path):
    b = setup(clock, tmp_path, side=False)
    try:
        linked(b, member=1)
        assert b.prm.handle(gmsg(b, ASK, sender=BD)) is True
        assert job_of(b) is None and b.texts == []                    # never drafted, never answered
        b.clock.now += 30
        b.prm.handle(gmsg(b, "在吗"))
        job = job_of(b)
        meta = json.loads(job["meta"])
        assert job["text"] == f"[{stamp(b.clock.now)}] 在吗" and MARK not in job["text"]
        assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("群成员", ASK)]
        assert "her_side" not in meta["group"]                        # no key at all: the Mac's prompt as before
        assert not b.prm.boss_groups.her_side_on and b.prm.boss_groups.side_ids(b.prm.store.boss_group(GROUP),
                                                                                b.prm.store.party_by_wxid(CREATOR)) == set()
    finally:
        stop(b)


# ── the switch on: her side's line is drafted as hers ─────────────────────────

def test_her_assistants_line_is_drafted_as_hers_and_the_mac_is_told_whose_it_is(bot, caplog):
    linked(bot, member=1)
    party = bot.prm.store.party_by_wxid(CREATOR)
    with caplog.at_level(logging.INFO):
        assert bot.prm.handle(gmsg(bot, ASK, sender=BD)) is True
    job = job_of(bot)
    meta = json.loads(job["meta"])
    assert job["chat_id"] == GROUP and job["text"] == f"[{stamp(bot.clock.now)}] {MARK}{ASK}"
    assert (meta["party_id"], meta["name"], meta["channel"]) == (party["id"], "小美", "group")
    assert meta["group"]["her_side"] is True and meta["group"]["lines"] == []      # no context line: it is the message
    # that person's name and wxid never go to the Mac; the log has an id's first characters only
    assert "测试商务" not in json.dumps([job["text"], meta], ensure_ascii=False) and BD not in job["meta"]
    assert "测试商务" not in caplog.text and BD not in caplog.text and "a line of her side" in caplog.text
    # her deal hears it as a line of her side there (the recall window; kept apart from her own lines, under 「side:」:
    # where her side last spoke counts it only while the switch is on, PRM_REACH_LAST_SPOKE)
    deal = bot.prm.store.deal_of(party["id"])
    assert deal["last_by_chat"] == {"side:" + GROUP: bot.clock.now} and deal["last_message_at"] == bot.clock.now
    assert bot.prm._her_last(deal, GROUP) == bot.clock.now
    # the draft goes to the group, by the rules of a group linked by hand (it waits; a person sends it)
    _, packet = drafted(bot, "好的老师～链接这边马上安排")
    assert (packet["chat_id"], packet["status"], packet["route"]) == (GROUP, "pending", "review")
    assert bot.prm.decide(packet["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(GROUP, "好的老师～链接这边马上安排")]


def test_her_own_line_and_her_assistants_are_one_job_only_the_assistants_marked(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "样品收到啦"))
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, ASK, sender=BD))
    job = job_of(bot)
    assert job["text"].split("\n") == [f"[{stamp(bot.clock.now - 5)}] 样品收到啦", f"[{stamp(bot.clock.now)}] {MARK}{ASK}"]
    assert json.loads(job["meta"])["group"]["her_side"] is True      # (every job of the group says so)


def test_a_line_of_her_side_puts_her_waiting_draft_out_of_date_as_her_own_would(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "链接什么时候能上呀"))
    _, packet = drafted(bot, "今天就能上哈")
    bot.clock.now += 60
    bot.prm.handle(gmsg(bot, "标题前缀用【测试专属】", sender=BD))
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    job = job_of(bot)
    assert [line.split("] ", 1)[1] for line in job["text"].split("\n")] == ["链接什么时候能上呀",
                                                                           f"{MARK}标题前缀用【测试专属】"]
    sent = json.loads(job["meta"])["sent"]                            # (the Mac hears the first draft never went out)
    assert [(x["text"], x["what"]) for x in sent] == [("今天就能上哈", "failed")], sent


@pytest.mark.parametrize("who", ["creator", "stopped", "boss", "admin", "cc"])
def test_another_registered_creator_and_our_own_people_are_never_her_side(clock, tmp_path, who):
    """In the same group: another registered creator (a stopped one too), a boss, the admin, a cc user.  None of their
    lines is drafted for this deal (nor for the other creator's: the group is not hers); they are context, as before."""
    b = setup(clock, tmp_path)
    try:
        sender = {"creator": SECOND, "stopped": SECOND, "boss": BOSS_ID, "admin": ADMIN, "cc": HELPER}[who]
        b.backend.names[SECOND] = "测试达人乙"
        b.backend.groups[GROUP]["members"] = [ME, CREATOR, BD, sender]
        if who in ("creator", "stopped"):
            other = b.prm.register(SECOND, "测试达人乙", "测试")
            if who == "stopped":
                b.prm.set_active(other["id"], False)
        linked(b, member=2 if who == "creator" else 1)                # (the registered are listed first)
        assert b.prm.store.boss_group(GROUP)["party_id"] == b.prm.store.party_by_wxid(CREATOR)["id"]
        assert b.prm.handle(gmsg(b, "我也想要这款", sender=sender)) is True
        assert job_of(b) is None and b.relay.store.recent(sender, limit=5) == []
        row = b.prm.store.boss_group(GROUP)
        assert [(x["who"], x["text"]) for x in row["lines"]] == [("老板" if who == "boss" else "群成员", "我也想要这款")]
        party = b.prm.store.party_by_wxid(CREATOR)
        assert b.prm.boss_groups.side_ids(row, party) == {BD}         # only her assistant is of her side
    finally:
        stop(b)


def test_wechats_own_lines_and_ours_are_never_her_side(bot):
    linked(bot, member=1)
    row, party = bot.prm.store.boss_group(GROUP), bot.prm.store.party_by_wxid(CREATOR)
    side = bot.prm.boss_groups.her_side
    assert side(row, party, BD) and side(row, party, "wxid_newcomer01")   # (someone who came in since: her side too)
    for sender in ("", ME, CREATOR, f"{GROUP}", ADMIN, HELPER):
        assert not side(row, party, sender), sender
    line = '"测试商务" 拍了拍我'
    assert bot.prm.handle(gmsg(bot, line, sender="", msg_type=10000, special={"kind": "system", "title": line})) is True
    assert job_of(bot) is None


def test_a_recall_of_her_side_holds_her_drafts_as_her_own_recall_does(bot):
    linked(bot, member=1)
    theirs = gmsg(bot, "UID 发错了，等下", sender=BD)
    bot.prm.handle(theirs)
    _, packet = drafted(bot)
    assert bot.prm.handle(recall(bot, "不认识的名字", recalled_id=theirs["message_id"])) is True
    assert held(bot, packet)                                          # by the line WeChat wrote over: her assistant's
    bot.prm.decide(packet["id"], "drop")
    bot.backend.names[ASSISTANT] = "测试助理"
    bot.backend.groups[GROUP]["members"].append(ASSISTANT)
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.sweep()                                       # (her assistant is a member now)
    bot.clock.now += 30
    bot.prm.handle(gmsg(bot, "在吗", sender=BD))
    _, packet = drafted(bot)
    assert bot.prm.handle(recall(bot, "测试助理")) is True              # (another member of her side: hers as well)
    assert held(bot, packet)


def test_a_recall_of_our_colleague_named_in_prm_group_ours_is_not_hers(clock, tmp_path):
    """Review 2026-11-11 #1: the brand's colleague in her group is ours (PRM_GROUP_OURS), never her side: his recall holds
    none of her drafts."""
    b = setup(clock, tmp_path, prm_group_ours="品牌同事")
    try:
        b.backend.contacts["品牌同事"] = COLLEAGUE
        linked(b, member=1)
        b.prm.handle(gmsg(b, "在吗", sender=BD))
        _, packet = drafted(b)
        assert b.prm.handle(recall(b, "品牌同事")) is True
        assert not held(b, packet)
    finally:
        stop(b)


def test_switched_off_her_assistants_recall_holds_nothing(clock, tmp_path):
    b = setup(clock, tmp_path, side=False)
    try:
        linked(b, member=1)
        b.prm.handle(gmsg(b, "在吗"))
        _, packet = drafted(b)
        assert b.prm.handle(recall(b, "测试商务")) is True
        assert not held(b, packet)
    finally:
        stop(b)


def test_while_a_person_has_her_deal_her_sides_line_is_only_recorded(bot):
    linked(bot, member=1)
    deal = bot.prm.store.deal_of(bot.prm.store.party_by_wxid(CREATOR)["id"])
    bot.prm.store.update_deal(deal["id"], bot.clock.now, mode="human", mode_until=bot.clock.now + 3600)
    bot.prm.handle(gmsg(bot, ASK, sender=BD))
    assert job_of(bot) is None
    [ev] = bot.prm.store.events(deal["id"], kinds=("creator_message",))
    assert ev["detail"]["text"].endswith(f"{MARK}{ASK}")


# ── 接群: her side's lines that waited ───────────────────────────────────────

def test_at_jie_qun_her_sides_unanswered_lines_wait_for_an_answer_too(bot):
    t = bot.clock.now
    say = bot.backend.say
    say(GROUP, BD, "我是小美的助理，以后对接找我", t - 3000)
    say(GROUP, ME, "好的～", t - 2900)
    say(GROUP, CREATOR, "样品收到啦", t - 900)
    say(GROUP, BD, ASK, t - 800)
    answer = linked(bot, member=1)
    assert answer.endswith("群里最近 2 句记成了背景；她最后 2 句还没人回，派了一份草稿")
    job = job_of(bot)
    meta = json.loads(job["meta"])
    assert job["text"].split("\n") == [f"[{stamp(t - 900)}] 样品收到啦", f"[{stamp(t - 800)}] {MARK}{ASK}"]
    assert meta["link_first"] is True                                 # its draft always waits
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [
        (bg.HER_SIDE, "我是小美的助理，以后对接找我"), ("我们", "好的～")]
    assert "测试商务" not in job["meta"]


def test_switched_off_her_assistants_lines_at_jie_qun_are_context_as_before(clock, tmp_path):
    b = setup(clock, tmp_path, side=False)
    try:
        b.backend.say(GROUP, BD, ASK, b.clock.now - 800)
        assert linked(b, member=1).endswith("群里最近 1 句记成了背景；她最近没有在等我们回：等她在群里说话再起草")
        assert job_of(b) is None
        assert [(x["who"], x["text"]) for x in b.prm.store.boss_group(GROUP)["lines"]] == [("群成员", ASK)]
    finally:
        stop(b)


# ── a 老板拉的群 and a group the scan linked ──────────────────────────────────

@pytest.mark.parametrize("on", [True, False])
def test_in_a_boss_group_her_sides_line_is_drafted_and_always_waits_for_a_person(clock, tmp_path, on):
    """A 老板拉的群 the scan took over: her assistant came in later.  On: the assistant's line is drafted, and its draft
    waits for a person even in the 下班时段, where her own would go out by itself (review 2026-11-11 #1: her side is
    anybody not known to be ours); its card says why.  Off: context only."""
    b = make(clock, tmp_path, prm_night_auto_hours="00:00-23:59", **({"prm_group_her_side": "on"} if on else {}))
    try:
        assert boss_makes(b) == 1
        no_opening(b, BOSS_GROUP)
        b.backend.groups[BOSS_GROUP]["members"].append(BD)
        b.clock.now += 60
        assert b.prm.handle(boss_gmsg(b, ASK, sender=BD)) is True
        if not on:
            assert job_of(b, BOSS_GROUP) is None
            assert [x["who"] for x in b.prm.store.boss_group(BOSS_GROUP)["lines"]] == ["群成员"]
            return
        job, packet = drafted(b, chat=BOSS_GROUP)
        meta = json.loads(job["meta"])
        assert job["text"].endswith(f"{MARK}{ASK}") and meta["group"]["her_side"] is True
        assert "night" not in meta                                    # (the Mac writes as by day: a person sends it)
        assert "manual" not in meta["group"]                          # (a 老板拉的群: told as one)
        assert (packet["status"], packet["route"]) == ("pending", "review")
        assert packet["meta"]["hold"] == packet["meta"]["night_held"] == HER_SIDE_HELD
        assert next(p for p in b.prm.snapshot()["pending"] if p["id"] == packet["id"])["hold"] == HER_SIDE_HELD
        b.prm.send_approved()
        assert b.texts == []
        # her own line there afterwards: by the night window, as before (no line of her side in its job)
        b.prm.decide(packet["id"], "drop")
        b.clock.now += 60
        b.prm.handle(boss_gmsg(b, "在吗"))
        _, own = drafted(b, chat=BOSS_GROUP)
        assert (own["status"], own["route"]) == ("approved", "night") and "hold" not in own["meta"]
        # the boss's own line there stays context
        b.clock.now += 60
        b.prm.handle(boss_gmsg(b, "这位是她助理，对接找她", sender=BOSS_ID))
        assert job_of(b, BOSS_GROUP) is None
    finally:
        stop(b)


def test_the_real_case_the_group_links_itself_and_her_assistants_request_is_one_draft(clock, tmp_path):
    """Both switches on, the case of 2026-11-11: her team's group, never linked; she is registered and silent; her
    assistant asks for the link with the title prefix and the UID.  The scan links the group; the assistant's lines are
    one draft that waits for a person; nothing goes out by itself; the admin is told."""
    b = auto_setup(clock, tmp_path, prm_group_her_side="on")
    try:
        t = b.clock.now
        b.backend.say(GROUP, BD, ASK, t - 120)
        b.backend.say(GROUP, BD, "标题前缀【测试专属】 UID 1234567890123", t - 100)
        assert b.prm.boss_groups.auto_scan()["linked"] == 1
        job = job_of(b)
        meta = json.loads(job["meta"])
        assert [line.split("] ", 1)[1] for line in job["text"].split("\n")] == [
            f"{MARK}{ASK}", f"{MARK}标题前缀【测试专属】 UID 1234567890123"]
        assert meta["link_first"] is True and meta["group"]["her_side"] is True and meta["group"]["manual"] is True
        _, packet = drafted(b, "好的老师～链接这边马上安排")
        assert (packet["chat_id"], packet["status"], packet["route"]) == (GROUP, "pending", "review")
        b.prm.send_approved()
        assert [chat for chat, _ in b.texts] == [ADMIN]              # only the admin's line: nothing in the group
        assert "她最后 2 句还没人回，派了一份草稿" in told(b)[0]
        # later her assistant writes again: drafted, and it waits as a group linked by hand's does
        b.prm.decide(packet["id"], "drop")
        b.clock.now += 300
        b.prm.handle(gmsg(b, "链接好了吗", sender=BD))
        job, packet = drafted(b)
        assert job["text"].endswith(f"{MARK}链接好了吗") and (packet["status"], packet["route"]) == ("pending", "review")
        assert COLLEAGUE not in job["meta"] and GROUP_NAME in job["meta"]
    finally:
        stop(b)


def test_a_hand_linked_groups_draft_of_her_sides_line_waits_and_says_why(bot):
    """PRM_GROUP_HER_SIDE never lets more go out by itself: in a group linked by hand (which waits anyway) the draft of
    her side's line waits, by the stricter reason (review 2026-11-11 #1: who that person is is not known)."""
    assert bot.prm.set_auto_mode(True)["ok"]
    linked(bot, member=1)
    bot.clock.now += 30
    bot.prm.handle(gmsg(bot, ASK, sender=BD))
    job = job_of(bot)
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == job["id"] and "night" not in json.loads(job["meta"])
    from test_prm import draft

    bot.finish(job["id"], draft(message_kind="faq", reply_draft="好的老师～", slots={}, stage_proposal="",
                                reason="kb/合作政策.md"))
    packet = bot.prm.store.packet_for_job(job["id"])
    assert (packet["status"], packet["meta"]["night_held"], packet["meta"]["hold"]) == ("pending", HER_SIDE_HELD,
                                                                                         HER_SIDE_HELD)
    assert bot.prm._hand_hold(GROUP) == HAND_GROUP_HELD               # (the group's own reason, as before)


def test_in_a_big_group_linked_by_hand_the_others_stay_context(clock, tmp_path):
    """A group over PRM_BOSS_GROUP_MAX an operator linked all the same (the 接群 question said: 只起草你选的那个人的话，
    别人的话只当背景): nobody else there is her side, or every member's line would be a Mac job."""
    from test_group_link import BIG

    b = setup(clock, tmp_path)
    try:
        b.say("接群")
        b.say("接群 2")                                               # the big group: 12 members
        answer = b.say("是 1")
        assert answer.startswith("接手了：群「品牌测试大群」→ 小美") and "之后她在群里说的话会起草" in answer
        row, party = b.prm.store.boss_group(BIG), b.prm.store.party_by_wxid(CREATOR)
        assert len(row["members"]) + 1 == 12 and b.prm.boss_groups.side_ids(row, party) == set()
        assert b.prm.handle(gmsg(b, "大家好", sender="wxid_guest03", chat=BIG)) is True
        assert job_of(b, BIG) is None and [x["who"] for x in b.prm.store.boss_group(BIG)["lines"]] == ["群成员"]
        b.prm.handle(gmsg(b, "在吗", chat=BIG))
        assert job_of(b, BIG)["text"].endswith("在吗")                # her own line there is drafted as ever
    finally:
        stop(b)


def test_the_jie_qun_answer_says_her_sides_lines_are_drafted_too(bot):
    assert "之后她和她那边的人（助理、商务）在群里说的话会起草，在「待审批」里等你看" in linked(bot, member=1)


def test_our_own_line_is_never_her_side_whatever_suffix_our_wxid_carries(clock, tmp_path):
    """This account's wxid as the backend gives it carries the data folder's suffix (wxid_abc_b6ce); its own lines are
    written by wxid_abc.  A line of ours handed on as a group line (a stored one, say) is never drafted as her side's."""
    b = setup(clock, tmp_path)
    try:
        linked(b, member=1)
        b.backend.own = ME + "_b6ce"
        row, party = b.prm.store.boss_group(GROUP), b.prm.store.party_by_wxid(CREATOR)
        assert not b.prm.boss_groups.her_side(row, party, ME) and b.prm.boss_groups.her_side(row, party, BD)
        b.backend.own = ME
        assert b.prm.handle({**gmsg(b, "我们自己说的", sender="wxid_other_device"), "is_own": True}) is True
        assert job_of(b) is None
    finally:
        stop(b)


# ── review 2026-11-11 #1: a draft with a line of her side always waits ────────

@pytest.mark.parametrize("how", ["gate", "night", "auto_mode"])
def test_a_draft_of_her_sides_line_never_goes_by_itself_whatever_the_switches(clock, tmp_path, how):
    """In a group linked by hand with PRM_LINKED_GROUPS_AUTO on (its drafts go as a 老板拉的群's) and confirmed: her own
    line's draft goes by its kind's gate, the 下班时段 or 自动模式; a line of her side's is never sent by any of them."""
    prm = {"prm_linked_groups_auto": "on", **({"prm_night_auto_hours": "00:00-23:59"} if how == "night" else {})}
    b = setup(clock, tmp_path, **prm)
    try:
        linked(b, member=1)
        if how == "gate":
            b.prm.store.set_gate("faq", "auto", b.clock.now)
        if how == "auto_mode":
            assert b.prm.set_auto_mode(True)["ok"]
        b.clock.now += 30
        b.prm.handle(gmsg(b, ASK, sender=BD))
        job, packet = drafted(b, "好的老师～链接这边马上安排")
        assert "night" not in json.loads(job["meta"])
        assert (packet["status"], packet["route"], packet["meta"]["hold"]) == ("pending", "review", HER_SIDE_HELD)
        b.prm.send_approved()
        assert b.texts == []
        b.prm.decide(packet["id"], "drop")
        b.clock.now += 60
        b.prm.handle(gmsg(b, "在吗"))                                 # her own line: by the switches, as before
        _, own = drafted(b, "在的老师～")
        assert own["status"] == "approved" and "hold" not in own["meta"]
    finally:
        stop(b)


def test_her_own_line_joined_by_one_of_her_sides_waits_too(clock, tmp_path):
    b = setup(clock, tmp_path, prm_linked_groups_auto="on")
    try:
        linked(b, member=1)
        b.prm.store.set_gate("faq", "auto", b.clock.now)
        b.prm.handle(gmsg(b, "在吗"))
        b.clock.now += 5
        b.prm.handle(gmsg(b, ASK, sender=BD))                         # (joins her job)
        _, packet = drafted(b, "在的老师～")
        assert (packet["status"], packet["meta"]["hold"]) == ("pending", HER_SIDE_HELD)
    finally:
        stop(b)
