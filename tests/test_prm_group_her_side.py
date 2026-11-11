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
from src.prm.service import HAND_GROUP_HELD, SETTLE_SEC

MARK = bg.HER_SIDE_MARK
SECOND = "wxid_creator02"                 # another registered creator in the same group
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
    # her deal hears it as a line of hers there (what counts as answered, the recall window)
    deal = bot.prm.store.deal_of(party["id"])
    assert deal["last_by_chat"][GROUP] == bot.clock.now
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
    bot.clock.now += 30
    bot.prm.handle(gmsg(bot, "在吗", sender=BD))
    _, packet = drafted(bot)
    assert bot.prm.handle(recall(bot, "品牌同事")) is True              # (a colleague of the group: her side as well)
    assert held(bot, packet)


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
def test_in_a_boss_group_her_sides_line_goes_by_the_night_window_as_hers(clock, tmp_path, on):
    """A 老板拉的群 the scan took over: her assistant came in later.  On: the assistant's line is drafted and, in the
    下班时段, goes out by itself as her own would.  Off: context only."""
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
        assert job["text"].endswith(f"{MARK}{ASK}") and meta["group"]["her_side"] is True and "night" in meta
        assert "manual" not in meta["group"]                          # (a 老板拉的群: told as one)
        assert (packet["status"], packet["route"]) == ("approved", "night")
        b.prm.send_approved()
        assert [chat for chat, _ in b.texts] == [BOSS_GROUP]
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


def test_the_hold_reason_of_a_hand_linked_group_is_unchanged(bot):
    """PRM_GROUP_HER_SIDE changes who is drafted, never what goes out by itself: a group linked by hand still waits."""
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
    assert (packet["status"], packet["meta"]["night_held"]) == ("pending", HAND_GROUP_HELD)
