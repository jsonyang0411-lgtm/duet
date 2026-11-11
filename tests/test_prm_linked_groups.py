"""接手任何合作群 (「接群」, src/prm/group_link.py) and what may go out by itself (decision 2026-11-10, reviews A and C).

Everybody in a group linked by hand reads what goes there: with PRM_LINKED_GROUPS_AUTO off (the default) its drafts
always wait for a person, whatever its kind's gate, the 下班时段 (PRM_NIGHT_AUTO_HOURS), 自动模式, 开场白自动发
(PRM_OPENING_AUTO_MIN) or 签收后问一句 would do, also a packet the bot started for her private chat and aimed at the group
once she had none; on, the group goes as a 老板拉的群 the scan took over (tests/test_prm_boss_groups.py), but for the draft
of the lines that waited at 接群, which always waits; the 接群 answer says which (final check 2026-11-10 A5).  「是 N」
registers a creator with her new-friend catch-up but no 开场白, and nothing of it goes by itself (review A-F4: a private
开场白 went out by itself; final check A7: her earlier private lines were dropped).  Every name, id and number is made
up."""

import json
import time

import pytest

from test_group_link import BD, COLLEAGUE, GROUP, GROUP_NAME, drafted, gmsg, job_of, linked, setup
from test_prm import CREATOR, Clock, draft
from test_prm_boss_groups import BOSS_ID, ME
from test_prm_opening_auto import OPENING

from src.prm import boss_groups as bg
from src.prm.service import HAND_GROUP_HELD, LINK_FIRST_HELD, OPENING_AUTO_GRACE_SEC, SETTLE_SEC

REPLY = "您好～均价两三百，我发您看看款式"
HELLO = "老师您好～样品显示已经签收啦，拿到了吗？戴着还合适吗？"


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 9, 14, 30, 0, 0, 0, -1)))


def stop(b):
    b.prm.stop()
    b.relay.stop()


def her_line(b, text="你们家帽子多少钱呀", reply=REPLY):
    """She writes in the group a little after 接群 and the Mac drafts it: ``(job meta, packet)``."""
    b.clock.now += 30
    b.prm.handle(gmsg(b, text))
    job, packet = drafted(b, reply)
    return json.loads(job["meta"]), packet


def mac_writes(b, job_id, text, kind="greeting", reason="kb/合作政策.md"):
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job_id
    b.finish(job_id, draft(message_kind=kind, reply_draft=text, slots={}, stage_proposal="", reason=reason,
                           attachments_to_send=[]))
    return b.prm.store.packet_for_job(job_id)


# ── 「是 N」: her new-friend catch-up without a 开场白 ─────────────────────────

def test_jie_qun_registers_a_new_friend_without_an_opening(clock, tmp_path):
    """Review A-F4 (probe A3): 「是 N」 registered her as the console does, whose ``on_registered``
    (NewFriendService.registered) read her private chat back as a new friend's: nothing of hers there, a private 开场白,
    which PRM_OPENING_AUTO_MIN sent by itself half an hour later, though the operator only linked a group.  A link by
    hand asks for her catch-up without a 开场白 (``opening`` False; final check 2026-11-10 A7: not asking for it at all
    dropped her earlier private lines): nothing of hers there, nothing at all; the console's 登记达人 asks as before."""
    b = setup(clock, tmp_path, prm_opening_auto_min=30)
    try:
        b.backend.friends.add(CREATOR)                             # (her private chat is there: a new friend)
        asked = []

        def registered(wxid, **kw):                                 # (NewFriendService.registered: her read-back ...)
            asked.append((wxid, kw))
            b.prm.take_new_friend(wxid, [], whole=True, **kw)       # (... handed on: nothing of hers in her chat)
            return True

        b.prm.on_registered = registered
        b.backend.say(GROUP, CREATOR, "你好", clock.now - 3600)
        b.backend.say(GROUP, ME, "您好～", clock.now - 3400)         # (answered by hand there: no catch-up draft)
        answer = linked(b, member=1)
        assert "小美（新登记的达人）" in answer and b.prm.is_party_group(GROUP)
        assert asked == [(CREATOR, {"opening": False})]             # her read-back asked for, without a 开场白
        assert b.relay.store.latest_open(CREATOR, drafts_only=True, system=False) is None
        clock.now += 3600
        b.prm.send_due_openings()
        b.prm.send_approved()
        assert b.texts == []
        b.prm.register("wxid_creator02", "阿花", "测试")             # the console's 登记达人: as before
        assert asked[-1] == ("wxid_creator02", {})
        assert json.loads(job_of(b, "wxid_creator02")["meta"])["opening"]["by"] == "new_friend"
    finally:
        stop(b)


@pytest.mark.parametrize("auto_min", [30, 0])
def test_jie_qun_still_drafts_her_earlier_private_lines_and_none_of_it_goes_by_itself(clock, tmp_path, auto_min):
    """Final check 2026-11-10 A7: a new friend who wrote in private before the operator linked her group: 「是 N」 never
    drafted those lines (no catch-up asked for).  Her catch-up without a 开场白 makes them one draft, which waits for a
    person and never goes by itself: no 开场白自动发 (PRM_OPENING_AUTO_MIN), no holding reply; 「发送」 sends it."""
    b = setup(clock, tmp_path, prm_opening_auto_min=auto_min)
    try:
        b.backend.friends.add(CREATOR)
        b.prm.on_registered = lambda wxid, **kw: True
        linked(b, member=1)
        lines = [b.msg("老师您好，我是小美", ts=clock.now - 600), b.msg("想看看你们家的帽子", ts=clock.now - 590)]
        assert b.prm.take_new_friend(CREATOR, lines, whole=True, opening=False) == 2
        job = job_of(b, CREATOR)
        meta = json.loads(job["meta"])
        assert meta["new_friend"] == {"from": clock.now - 600, "count": 2, "linked": True}
        assert "opening_auto" not in meta and "opening" not in meta and "night" not in meta
        packet = mac_writes(b, job["id"], "您好～我是品牌的运营，帽子款式我发您看看", kind="faq")
        assert (packet["status"], packet["route"]) == ("pending", "review") and "auto_at" not in packet["meta"]
        clock.now += 3600
        assert b.prm.send_holding_replies() == 0 and b.prm.send_due_openings() == 0
        b.prm.send_approved()
        assert b.texts == []
        assert b.prm.decide(packet["id"], "send")["ok"] and b.prm.send_approved() == 1
        assert b.texts == [(CREATOR, "您好～我是品牌的运营，帽子款式我发您看看")]
    finally:
        stop(b)


def test_the_jie_qun_answer_says_how_her_drafts_there_go_by_the_switch(clock, tmp_path):
    """Final check 2026-11-10 A5: the answer to 「是 N」 said every draft there waits in 「待审批」; switched on
    (PRM_LINKED_GROUPS_AUTO), only the draft of the lines that waited at 接群 does, the rest goes as a 老板拉的群's."""
    for on, how in ((True, "之后她在群里说的话会起草，和老板拉的群一样照门槛、下班时段、自动模式发（「手动接的群也自动发」开着）；"
                           "接群这一刻她在等的那几句的草稿在「待审批」里等你看"),
                    (False, "之后她在群里说的话会起草，在「待审批」里等你看")):
        b = setup(clock, tmp_path / str(on), **({"prm_linked_groups_auto": "on"} if on else {}))
        try:
            answer = linked(b, member=1)
            deal = b.prm.store.deal_of(b.prm.store.party_by_wxid(CREATOR)["id"])
            assert answer == (f"接手了：群「{GROUP_NAME}」→ 小美（新登记的达人），合作单 {deal['id']} 号；{how}；"
                              "她最近没有在等我们回：等她在群里说话再起草"), on
        finally:
            stop(b)


# ── PRM_LINKED_GROUPS_AUTO off (the default) ───────────────────────────────

@pytest.mark.parametrize("how", ["auto_mode", "night"])
def test_auto_mode_and_the_night_window_never_send_into_a_group_linked_by_hand(clock, tmp_path, how):
    """Probe A6: 自动模式 on (or the 下班时段), her line in a group linked by hand (her, her 商务, a colleague, us; no boss,
    PRM_BOSS_GROUPS off) went into the group by itself.  Its job is not told it is night (the Mac writes as by day), its
    draft waits and says why on its card; a person's 「发送」 sends it."""
    b = setup(clock, tmp_path, **({"prm_night_auto_hours": "00:00-23:59"} if how == "night" else {}))
    try:
        if how == "auto_mode":
            assert b.prm.set_auto_mode(True)["ok"]
        linked(b, member=1)
        meta, packet = her_line(b)
        assert "night" not in meta and "link_first" not in meta
        assert (packet["status"], packet["route"], packet["meta"]["night_held"]) == ("pending", "review", HAND_GROUP_HELD)
        assert bool(packet["meta"].get("auto_mode")) is (how == "auto_mode")
        b.prm.send_approved()
        assert b.texts == []
        [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == packet["id"]]
        assert shown["night_held"] == HAND_GROUP_HELD and shown["group_manual"] is True
        assert b.prm.decide(packet["id"], "send")["ok"]
        b.prm.send_approved()
        assert b.texts == [(GROUP, REPLY)]
    finally:
        stop(b)


def test_a_kinds_gate_never_sends_into_a_group_linked_by_hand(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.prm.store.set_gate("faq", "auto", clock.now)
        linked(b, member=1)
        _, packet = her_line(b)
        assert (packet["status"], packet["route"]) == ("pending", "review") and "night_held" not in packet["meta"]
        b.prm.send_approved()
        assert b.texts == []
    finally:
        stop(b)


@pytest.mark.parametrize("on", [False, True])
def test_a_shipping_notice_its_gate_approved_waits_once_it_goes_to_a_group_linked_by_hand(clock, tmp_path, on):
    """A packet the bot started for her private chat (a shipping notice; its kind's gate open) goes to the group linked
    by hand when she has no private chat with us (``_aim``): off, it goes back to 待审批 before it is sent."""
    b = setup(clock, tmp_path, **({"prm_linked_groups_auto": "on"} if on else {}))
    try:
        b.prm.store.set_gate("shipping_notice", "auto", clock.now)
        linked(b, member=1)                                         # (she has no private chat with us)
        deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
        out = b.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"})
        notice = b.prm.store.packet(out["packet"])
        assert notice["chat_id"] == GROUP and (notice["status"], notice["route"]) == ("approved", "auto")
        assert b.prm.send_approved() == int(on)
        notice = b.prm.store.packet(out["packet"])
        if on:
            assert notice["status"] == "sent" and b.texts[0][0] == GROUP
            return
        assert (notice["status"], notice["route"], notice["decided_at"]) == ("pending", "review", None)
        assert b.texts == []
        assert b.prm.decide(notice["id"], "send")["ok"] and b.prm.send_approved() == 1
        assert [chat for chat, _ in b.texts] == [GROUP]
    finally:
        stop(b)


def test_an_opening_that_went_by_itself_waits_once_it_goes_to_a_group_linked_by_hand(clock, tmp_path):
    """开场白自动发 (PRM_OPENING_AUTO_MIN): our first line for her private chat (she was our friend then) is approved by
    itself; when it is sent she has no private chat with us any more and it would go to the group linked by hand
    (``_aim``): it waits for a person instead, its time to go by itself gone (the card says why)."""
    b = setup(clock, tmp_path, prm_opening_auto_min=30)
    try:
        b.backend.friends.add(CREATOR)
        linked(b, member=1)
        deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
        out = b.prm.ask_opening(deal_id)
        assert out["ok"], out
        packet = mac_writes(b, out["job"], OPENING)
        assert packet["chat_id"] == CREATOR and packet["status"] == "pending" and packet["meta"]["auto_at"]
        b.backend.friends.discard(CREATOR)                          # (not our friend any more)
        clock.now = max(packet["meta"]["auto_at"], b.prm._started + OPENING_AUTO_GRACE_SEC) + bg.KNOWS_SEC + 1
        assert b.prm.send_due_openings() == 1                       # (approved for her private chat)
        assert b.prm.send_approved() == 0 and b.texts == []
        held = b.prm.store.packet(packet["id"])
        assert (held["status"], held["route"], held["chat_id"]) == ("pending", "review", GROUP)
        assert "auto_at" not in held["meta"] and held["meta"]["auto_held"] == HAND_GROUP_HELD
        assert b.prm.send_due_openings() == 0
        [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == packet["id"]]
        assert shown["auto_at"] is None and shown["auto_held"] == HAND_GROUP_HELD
    finally:
        stop(b)


@pytest.mark.parametrize("on", [False, True])
def test_a_signed_hello_in_a_group_linked_by_hand_goes_by_the_switch(clock, tmp_path, on):
    """签收后问一句 (PRM_SIGNED_HELLO) reaches her in the group linked by hand (no private chat); 自动模式 on: off, its job
    is not told it is night and its draft waits; on, the night's rules send it, as in a 老板拉的群."""
    b = setup(clock, tmp_path, prm_signed_hello="on", **({"prm_linked_groups_auto": "on"} if on else {}))
    try:
        assert b.prm.set_auto_mode(True)["ok"]
        linked(b, member=1)
        party = b.prm.store.party_by_wxid(CREATOR)
        deal = b.prm.store.deal_of(party["id"])
        row = {"order_no": "YP2611100000001", "signed_at": clock.now - 600}
        job_id = b.prm._ask_signed_hello(row, deal, party, clock.now)
        job = b.relay.store.get(job_id)
        meta = json.loads(job["meta"])
        assert job["chat_id"] == GROUP and meta["signed"]["order"] == row["order_no"] and ("night" in meta) is on
        packet = mac_writes(b, job_id, HELLO, reason="kb/合作政策.md 场景 D2：聚水潭显示签收")
        assert (packet["status"], packet["route"]) == (("approved", "night") if on else ("pending", "review"))
        b.prm.send_approved()
        assert b.texts == ([(GROUP, HELLO)] if on else [])
    finally:
        stop(b)


# ── PRM_LINKED_GROUPS_AUTO on ──────────────────────────────────────────────

@pytest.mark.parametrize("how", ["gate", "auto_mode", "night"])
def test_switched_on_a_group_linked_by_hand_goes_as_a_boss_group(clock, tmp_path, how):
    b = setup(clock, tmp_path, prm_linked_groups_auto="on",
              **({"prm_night_auto_hours": "00:00-23:59"} if how == "night" else {}))
    try:
        if how == "gate":
            b.prm.store.set_gate("faq", "auto", clock.now)
        if how == "auto_mode":
            assert b.prm.set_auto_mode(True)["ok"]
        linked(b, member=1)
        meta, packet = her_line(b)
        assert ("night" in meta) is (how != "gate")
        assert (packet["status"], packet["route"]) == ("approved", "auto" if how == "gate" else "night")
        b.prm.send_approved()
        assert b.texts == [(GROUP, REPLY)]
    finally:
        stop(b)


def test_the_draft_of_the_lines_that_waited_at_jie_qun_always_waits(clock, tmp_path):
    """Switched on, her gate open and 自动模式 on: what she wrote a moment before 接群 (no late line) is one job of its own
    (``meta.link_first``, not told it is night) whose draft waits, as the 接群 answer said (「在「待审批」里等你看」); her
    next line goes by the gate."""
    b = setup(clock, tmp_path, prm_linked_groups_auto="on")
    try:
        b.prm.store.set_gate("faq", "auto", clock.now)
        assert b.prm.set_auto_mode(True)["ok"]
        b.backend.say(GROUP, CREATOR, "在吗", clock.now - 20)
        b.backend.say(GROUP, CREATOR, "想问下价格", clock.now - 10)
        answer = linked(b, member=1)
        assert "在「待审批」里等你看" in answer and answer.endswith("她最后 2 句还没人回，派了一份草稿")
        job = job_of(b)
        meta = json.loads(job["meta"])
        assert meta["link_first"] is True and "late" not in meta and "night" not in meta
        assert len(job["text"].split("\n")) == 2                     # (both lines: one job)
        _, packet = drafted(b, REPLY)
        assert (packet["status"], packet["route"], packet["meta"]["night_held"]) == ("pending", "review",
                                                                                      LINK_FIRST_HELD)
        b.prm.send_approved()
        assert b.texts == []
        assert b.prm.decide(packet["id"], "send")["ok"]
        b.prm.send_approved()
        meta, later = her_line(b, "那这款呢", "这款也有的～")
        assert "link_first" not in meta and (later["status"], later["route"]) == ("approved", "auto")
        b.prm.send_approved()
        assert b.texts == [(GROUP, REPLY), (GROUP, "这款也有的～")]
    finally:
        stop(b)


def test_the_draft_of_the_lines_that_waited_stays_a_persons_when_it_is_drafted_again(clock, tmp_path):
    """Switched on, her gate open: a colleague's line has her waiting 接群 draft drafted again (``_redraft_group``), and
    a job that takes its lines along when the Mac wrote nothing: they answer what waited at 接群 all the same."""
    b = setup(clock, tmp_path, prm_linked_groups_auto="on")
    try:
        b.prm.store.set_gate("faq", "auto", clock.now)
        b.backend.say(GROUP, CREATOR, "在吗", clock.now - 20)
        linked(b, member=1)
        _, first = drafted(b, REPLY)
        assert first["status"] == "pending"
        b.clock.now += 30
        b.prm.handle(gmsg(b, "她说的那款还有吗", sender=COLLEAGUE))
        assert b.prm.store.packet(first["id"])["status"] == "superseded"
        job = job_of(b)
        assert json.loads(job["meta"])["link_first"] is True
        b.clock.now += SETTLE_SEC + 1
        assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
        assert b.relay.finish(job["id"], {"ok": False, "text": "", "error": "测试：Mac 出错了"})
        b.relay.tick()                                              # (no draft: her lines wait for the next job)
        b.clock.now += 30
        b.prm.handle(gmsg(b, "在吗在吗"))
        assert json.loads(job_of(b)["meta"])["link_first"] is True
        _, again = drafted(b, REPLY)
        assert (again["status"], again["route"]) == ("pending", "review")
    finally:
        stop(b)


# ── 「不接群」: what the row keeps (review B-L5) ──────────────────────────────

def test_bu_jie_qun_keeps_only_her_and_the_bosses_of_the_members_and_none_of_the_lines(clock, tmp_path):
    """A group linked by hand kept every member's wxid and name and the lines told to her drafts; 「不接群」 only marked it
    gone.  Now the row keeps her and the bosses, no line."""
    b = setup(clock, tmp_path)
    try:
        b.backend.groups[GROUP]["members"].append(BOSS_ID)
        b.backend.say(GROUP, BD, "她这周想先看样", clock.now - 600)
        linked(b, member=1)
        row = b.prm.store.boss_group(GROUP)
        assert {m["wxid"] for m in row["members"]} == {CREATOR, BD, COLLEAGUE, BOSS_ID} and row["lines"]
        b.say(f"不接群 {GROUP_NAME}")
        row = b.prm.store.boss_group(GROUP)
        assert row["state"] == "gone" and row["lines"] == []
        assert sorted(m["wxid"] for m in row["members"]) == sorted([CREATOR, BOSS_ID])
    finally:
        stop(b)


def test_a_group_linked_again_to_another_creator_is_scrubbed_by_the_first_ones_purge(clock, tmp_path):
    """Her lines in a group's jobs are scrubbed by her deal's 8.2 clean-up (``boss_chats``): the group's row named only
    the deal it is linked to now, so a group 「不接群」 and linked again to another member was never scrubbed of hers.
    Every deal it was ever linked to scrubs it (its record names the group)."""
    b = setup(clock, tmp_path)
    try:
        linked(b, member=1)
        her_deal = b.prm.store.boss_group(GROUP)["deal_id"]
        b.clock.now += 30
        b.prm.handle(gmsg(b, "寄到上海市浦东新区张江路100号3栋1201"))
        job_id = job_of(b)["id"]
        assert "张江路100号" in b.relay.store.get(job_id)["text"]
        b.say(f"不接群 {GROUP_NAME}")
        b.say("接群")
        b.say("接群 1")
        assert b.say("是 2").startswith(f"接手了：群「{GROUP_NAME}」→ 测试商务（新登记的达人）")    # (her 商务 now)
        assert b.prm.store.boss_group(GROUP)["deal_id"] != her_deal
        b.prm.purge(b.prm.store.deal(her_deal), b.clock.now)
        assert "张江路100号" not in b.relay.store.get(job_id)["text"]
        assert b.prm.boss_groups.chats_of(her_deal) == [GROUP]
    finally:
        stop(b)
