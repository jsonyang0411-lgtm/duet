"""群里的话都先审 (PRM_GROUPS_REVIEW, 2026-11-13; PrmService._hand_hold).  The user: 「可以改一下，群里的消息都经过审批再发，
因为后面感觉都是比较正式的寄样之类的环节，然后私信依旧可以自动回复」.

With the switch on nothing goes into a group chat by itself: every draft whose chat is a group (a 老板拉的群, one linked
by hand, one the scan linked or took over, confirmed or not, her own line's or her side's, and what the bot starts itself
there: the shipping notice made from its template, 签收后问一句, a 专属佣金 step, 补发货盘, an opening) waits in 待审批 with
one line why (GROUPS_REVIEW_HELD), whatever its kind's gate, the 下班时段, 自动模式 and PRM_LINKED_GROUPS_AUTO say; one
approved by itself before it was aimed at a group goes back to 待审批 before it is sent, never lost nor sent twice.
Private chats are untouched, a creator's who has a group too.  With the switch off everything is as before.

The backends are test_prm_boss_groups' and test_group_link's fakes; the relay and the PRM service are real.  Every name,
id and number is made up.
"""

import json
import time

import pytest

from test_group_link import GROUP as HAND, linked, setup as link_setup
from test_prm import CREATOR, Clock, draft
from test_prm_boss_groups import GROUP, boss_makes, gmsg, job_of, make, no_opening
from test_prm_groups_auto_link import setup as auto_setup

from src.prm.service import AUTO_UNCONFIRMED_HELD, GROUPS_REVIEW_HELD, SETTLE_SEC

REPLY = "您好～在的"
HELLO = "老师您好～样品显示已经签收啦，拿到了吗？戴着还合适吗？"


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 13, 14, 30, 0, 0, 0, -1)))


def stop(b):
    b.prm.stop()
    b.relay.stop()


def writes(b, job_id, text=REPLY, kind="faq", reason="kb/合作政策.md"):
    """The Mac takes job ``job_id`` and drafts it: its packet."""
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job_id
    b.finish(job_id, draft(message_kind=kind, reply_draft=text, slots={}, stage_proposal="", reason=reason,
                           attachments_to_send=[]))
    return b.prm.store.packet_for_job(job_id)


def boss(clock, tmp_path, on=True, **prm):
    """A 老板拉的群 the scan took over (she alone with us and the boss: no question), its 开场白 put away."""
    b = make(clock, tmp_path, **({"prm_groups_review": "on"} if on else {}), **prm)
    boss_makes(b)
    no_opening(b)
    return b


def her_line(b, text="在吗", chat=GROUP, private=False):
    b.clock.now += 30
    b.prm.handle(b.msg(text) if private else gmsg(b, text, chat=chat))
    job = job_of(b, CREATOR if private else chat)
    return json.loads(job["meta"]), writes(b, job["id"])


def waits(b, packet, chat=GROUP):
    """It waits in 待审批 for the switch's reason (the card says so); sent by a person it goes out once."""
    assert (packet["status"], packet["route"], packet["chat_id"]) == ("pending", "review", chat)
    assert packet["meta"]["hold"] == GROUPS_REVIEW_HELD
    [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == packet["id"]]
    assert shown["hold"] == GROUPS_REVIEW_HELD
    assert b.prm.send_approved() == 0
    assert b.prm.decide(packet["id"], "send")["ok"] and b.prm.send_approved() == 1
    assert [c for c, _ in b.texts].count(chat) == 1 and b.prm.send_approved() == 0


# ── a 老板拉的群: its gate, the 下班时段 and 自动模式 send nothing there ─────────────

@pytest.mark.parametrize("how", ["gate", "night", "auto_mode"])
@pytest.mark.parametrize("on", [False, True])
def test_a_boss_groups_draft_waits_whatever_would_send_it(clock, tmp_path, how, on):
    b = boss(clock, tmp_path, on=on, **({"prm_night_auto_hours": "00:00-23:59"} if how == "night" else {}))
    try:
        if how == "gate":
            b.prm.store.set_gate("faq", "auto", clock.now)
        if how == "auto_mode":
            assert b.prm.set_auto_mode(True)["ok"]
        meta, packet = her_line(b)
        if not on:                                                 # (as before: it goes by itself)
            assert (packet["status"], packet["route"]) == ("approved", "auto" if how == "gate" else "night")
            assert b.prm.send_approved() == 1 and b.texts == [(GROUP, REPLY)]
            return
        assert "night" not in meta                                 # (the Mac is not told it may go by itself)
        waits(b, packet)
    finally:
        stop(b)


@pytest.mark.parametrize("how", ["gate", "night", "auto_mode"])
def test_her_private_drafts_still_go_by_themselves_though_she_has_a_group(clock, tmp_path, how):
    b = boss(clock, tmp_path, **({"prm_night_auto_hours": "00:00-23:59"} if how == "night" else {}))
    try:
        if how == "gate":
            b.prm.store.set_gate("faq", "auto", clock.now)
        if how == "auto_mode":
            assert b.prm.set_auto_mode(True)["ok"]
        assert b.prm._hand_hold(CREATOR) == "" and b.prm._hand_hold(GROUP) == GROUPS_REVIEW_HELD
        meta, packet = her_line(b, "私聊问一下", private=True)
        assert packet["chat_id"] == CREATOR and ("night" in meta) is (how != "gate")
        assert (packet["status"], packet["route"]) == ("approved", "auto" if how == "gate" else "night")
        assert "hold" not in packet["meta"]
        assert b.prm.send_approved() == 1 and b.texts == [(CREATOR, REPLY)]
    finally:
        stop(b)


def test_a_holding_reply_never_goes_into_a_group(clock, tmp_path):
    b = boss(clock, tmp_path, prm_holding_min=10)
    try:
        her_line(b)
        clock.now += 11 * 60
        assert b.prm.send_holding_replies() == 0 and b.texts == []
    finally:
        stop(b)


# ── groups linked by hand or by the scan ────────────────────────────────────

def test_a_group_linked_by_hand_waits_though_prm_linked_groups_auto_is_on(clock, tmp_path):
    b = link_setup(clock, tmp_path, prm_linked_groups_auto="on", prm_groups_review="on")
    try:
        b.prm.store.set_gate("faq", "auto", clock.now)
        answer = linked(b, member=1)
        assert "在「待审批」里等你看（「群里的话都先审」开着）" in answer
        _, packet = her_line(b, chat=HAND)
        waits(b, packet, chat=HAND)
    finally:
        stop(b)


@pytest.mark.parametrize("on", [False, True])
def test_a_confirmed_group_the_scan_linked_waits_only_with_the_switch(clock, tmp_path, on):
    """Confirmed, a group the scan linked goes by PRM_LINKED_GROUPS_AUTO (on here); with the switch on it waits: the
    unconfirmed state no longer changes anything then (unconfirmed, the switch's reason is shown)."""
    b = auto_setup(clock, tmp_path, prm_linked_groups_auto="on", **({"prm_groups_review": "on"} if on else {}))
    try:
        b.prm.store.set_gate("faq", "auto", clock.now)
        b.prm.boss_groups.auto_scan()
        assert b.prm._hand_hold(HAND) == (GROUPS_REVIEW_HELD if on else AUTO_UNCONFIRMED_HELD)
        answer = b.say("确认接群 小美合作对接群")
        assert not b.prm.boss_groups.unconfirmed(HAND)
        assert ("「群里的话都先审」开着" in answer) is on
        _, packet = her_line(b, chat=HAND)
        if not on:
            assert (packet["status"], packet["route"]) == ("approved", "auto")
            return
        waits(b, packet, chat=HAND)
    finally:
        stop(b)


# ── what the bot starts itself and sends into the group ──────────────────────

@pytest.mark.parametrize("on", [False, True])
def test_the_template_shipping_notice_its_gate_approved_waits_before_it_goes_into_the_group(clock, tmp_path, on):
    """The shipping notice is no Mac draft: deals.add_shipment writes it from its template and its kind's gate may
    approve it at once (route auto); she has no private chat, so it is aimed at the group.  Switched on, it goes back to
    待审批 before it is sent (the card says why), and a person's 「发送」 sends it once."""
    b = boss(clock, tmp_path, on=on)
    try:
        b.prm.store.set_gate("shipping_notice", "auto", clock.now)
        deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
        out = b.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"})
        notice = b.prm.store.packet(out["packet"])
        assert notice["chat_id"] == GROUP and (notice["status"], notice["route"]) == ("approved", "auto")
        if not on:
            assert b.prm.send_approved() == 1 and [c for c, _ in b.texts] == [GROUP]
            return
        assert b.prm.send_approved() == 0 and b.texts == []
        notice = b.prm.store.packet(notice["id"])
        assert (notice["status"], notice["route"], notice["decided_at"]) == ("pending", "review", None)
        waits(b, notice)
    finally:
        stop(b)


def test_the_shipping_notice_for_her_private_chat_still_goes_by_itself(clock, tmp_path):
    b = boss(clock, tmp_path)
    try:
        b.backend.friends.add(CREATOR)                               # (she is our friend: her private chat)
        b.prm.store.set_gate("shipping_notice", "auto", clock.now)
        deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
        out = b.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"})
        assert b.prm.store.packet(out["packet"])["chat_id"] == CREATOR
        assert b.prm.send_approved() == 1 and [c for c, _ in b.texts] == [CREATOR]
    finally:
        stop(b)


@pytest.mark.parametrize("on", [False, True])
def test_the_signed_hello_in_the_group_goes_by_the_night_rules_only_with_the_switch_off(clock, tmp_path, on):
    b = boss(clock, tmp_path, on=on, prm_signed_hello="on")
    try:
        assert b.prm.set_auto_mode(True)["ok"]
        party = b.prm.store.party_by_wxid(CREATOR)
        deal = b.prm.store.deal_of(party["id"])
        job_id = b.prm._ask_signed_hello({"order_no": "YP2611130000001", "signed_at": clock.now - 600}, deal, party,
                                         clock.now)
        job = b.relay.store.get(job_id)
        assert job["chat_id"] == GROUP and ("night" in json.loads(job["meta"])) is not on
        packet = writes(b, job_id, HELLO, reason="kb/合作政策.md 场景 D2：聚水潭显示签收")
        if not on:
            assert (packet["status"], packet["route"]) == ("approved", "night")
            assert b.prm.send_approved() == 1 and b.texts == [(GROUP, HELLO)]
            return
        waits(b, packet)
    finally:
        stop(b)


@pytest.mark.parametrize("what", ["huopan", "opening", "step"])
@pytest.mark.parametrize("on", [False, True])
def test_the_consoles_messages_and_the_next_steps_in_the_group_wait_as_before(clock, tmp_path, what, on):
    """补发货盘, the console's 开场白 and a 专属佣金 step always wait for a person (as before); with the switch on their
    cards say the switch's reason too."""
    b = boss(clock, tmp_path, on=on)
    try:
        b.prm.store.set_gate("faq", "auto", clock.now)
        b.prm.store.set_gate("greeting", "auto", clock.now)
        party = b.prm.store.party_by_wxid(CREATOR)
        deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
        ask = {"huopan": lambda: b.prm.ask_huopan(deal_id)["job"], "opening": lambda: b.prm.ask_opening(deal_id)["job"],
               "step": lambda: b.prm._dispatch_step(b.prm.store.deal(deal_id), party, "签收了：开专属佣金", None, True,
                                                    clock.now)}[what]
        job_id = ask()
        assert b.relay.store.get(job_id)["chat_id"] == GROUP
        packet = writes(b, job_id, "老师，货盘再发您一份～", kind="greeting" if what == "opening" else "faq")
        assert (packet["status"], packet["route"], packet["chat_id"]) == ("pending", "review", GROUP)
        assert (packet["meta"].get("hold") == GROUPS_REVIEW_HELD) is on
        assert b.prm.send_approved() == 0 and b.texts == []
    finally:
        stop(b)
