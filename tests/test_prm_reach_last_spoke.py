"""发在她那边最近说话的地方 (PRM_REACH_LAST_SPOKE, 2026-11-11; PrmService._reaching, the one place that decides).

The real case: a creator's team talks only in their group; her deal also has an old private chat with her account; the
operator clicked 「做手卡」 and the cards went to the private chat where nobody looks.  With the switch on, what the bot
starts itself (a shipping notice, a 专属佣金 step, the console's 开场白 / 补发货盘 / 做手卡, 签收后问一句) goes to a linked
group she is still in when the latest line of her side on the deal was there; her latest line in private, or none at
all: as before.  The job says it is a group job as every group job does (``meta.channel``, ``meta.group``) and why
(``meta.reach``); the console says where and why.  With the switch off nothing changes.

The backend is test_group_link's fake (her team's group: her, her 商务, a colleague, us; linked by hand); the relay and
the PRM service are real.  Every name, id and number is made up.
"""

import json
import time

import pytest

from test_group_link import BD, GROUP, GROUP_NAME, LinkBackend, gmsg, job_of, linked, setup as link_setup
from test_prm import CREATOR, Clock, draft
from test_prm_boss_groups import BOSS_ID, restart

from src.prm import boss_groups as bg
from src.prm.service import HAND_GROUP_HELD, SETTLE_SEC, apply_deal_action


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 11, 15, 0, 0, 0, 0, -1)))


def setup(clock, tmp_path, on=True, side=False, **prm):
    """test_group_link's bot, PRM_REACH_LAST_SPOKE on (``on``), PRM_GROUP_HER_SIDE as ``side``; the group linked to her
    (小美, CREATOR)."""
    b = link_setup(clock, tmp_path, **({"prm_reach_last_spoke": "on"} if on else {}),
                   **({"prm_group_her_side": "on"} if side else {}), **prm)
    linked(b, member=1)
    b.party = b.prm.store.party_by_wxid(CREATOR)
    b.deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
    return b


def stop(b):
    b.prm.stop()
    b.relay.stop()


def quiet(b):
    """Her lines' reply jobs are put away (as if done with): a bot-started job is asked for only when none of hers is
    being written."""
    for chat in (CREATOR, GROUP):
        job = job_of(b, chat)
        if job is not None:
            assert b.relay.store.cancel_if_pending(job["id"], b.clock.now)


def private_then_group(b, sender=CREATOR):
    """She writes in her private chat, a minute later her side writes in the group."""
    b.prm.handle(b.msg("私聊问一下"))
    b.clock.now += 60
    b.prm.handle(gmsg(b, "群里说一句", sender=sender))
    b.clock.now += 5
    quiet(b)


def group_then_private(b):
    b.prm.handle(gmsg(b, "群里说一句"))
    b.clock.now += 60
    b.prm.handle(b.msg("私聊问一下"))
    b.clock.now += 5
    quiet(b)


def ship(b, waybill="SF1234567890"):
    out = b.prm.deal_action("shipment", {"deal_id": b.deal_id, "carrier": "顺丰", "waybill": waybill})
    assert out["ok"], out
    return b.prm.store.packet(out["packet"])


def meta_of(job):
    return json.loads(job["meta"])


def the_deal(b):
    return next(p for p in b.prm.snapshot()["parties"] if p["wxid"] == CREATOR)["deal"]


# ── her side last spoke in the group: everything the bot starts goes there ────

def test_her_latest_line_in_the_group_sends_every_bot_started_message_there(clock, tmp_path):
    b = setup(clock, tmp_path, prm_handcards="on")
    try:
        private_then_group(b)
        assert b.prm._private_known(b.party)                        # (she has a private chat with us)
        notice = ship(b)                                               # a shipping notice (no job)
        assert notice["chat_id"] == GROUP
        assert (notice["meta"]["channel"], notice["meta"]["group_manual"], notice["meta"]["reach"]) == (
            "group", True, "last_spoke")
        pending = next(p for p in b.prm.snapshot()["pending"] if p["id"] == notice["id"])
        assert (pending["channel"], pending["reach"], pending["group_name"]) == ("group", "last_spoke", GROUP_NAME)
        b.prm.decide(notice["id"], "drop")
        made = {
            "huopan": lambda: b.prm.ask_huopan(b.deal_id)["job"],
            "handcards": lambda: b.prm.ask_handcards(b.deal_id, None)["job"],
            "opening": lambda: b.prm.ask_opening(b.deal_id)["job"],
            "step": lambda: b.prm._dispatch_step(b.prm.store.deal(b.deal_id), b.party, "签收了：开专属佣金", None, True,
                                                 b.clock.now),
            "signed": lambda: b.prm._ask_signed_hello({"order_no": "YP0000000001", "signed_at": b.clock.now - 600},
                                                      b.prm.store.deal(b.deal_id), b.party, b.clock.now),
        }
        for kind, ask in made.items():
            job = b.relay.store.get(ask())
            meta = meta_of(job)
            assert job["chat_id"] == GROUP, kind
            assert (meta["channel"], meta["reach"], meta["group"]["name"], meta["group"]["manual"]) == (
                "group", "last_spoke", GROUP_NAME, True), kind
            assert b.relay.store.cancel_if_pending(job["id"], b.clock.now), kind
        # the deal page says where and why
        assert the_deal(b)["reach"] == {"where": "group", "name": GROUP_NAME, "why": "last_spoke"}
    finally:
        stop(b)


def test_its_draft_says_why_it_goes_to_the_group_and_goes_there(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        private_then_group(b)
        job = b.relay.store.get(b.prm.ask_huopan(b.deal_id)["job"])
        b.clock.now += SETTLE_SEC + 1
        assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
        b.finish(job["id"], draft(message_kind="faq", reply_draft="老师，货盘再发您一份～", slots={}, stage_proposal="",
                                  reason="kb/合作政策.md"))
        packet = b.prm.store.packet_for_job(job["id"])
        assert (packet["chat_id"], packet["meta"]["reach"], packet["status"]) == (GROUP, "last_spoke", "pending")
        text = b.prm.wechat_review._notice(packet, log=False)[0]
        assert "她那边最近是在这个群里说的话" in text and "发在群里，不是她的私聊" in text
        b.prm.decide(packet["id"], "send")
        b.prm.send_approved()
        assert b.texts[-1] == (GROUP, "老师，货盘再发您一份～")
    finally:
        stop(b)


def test_a_step_in_the_group_counts_as_under_way_though_she_has_a_private_chat(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        private_then_group(b)
        job_id = b.prm._dispatch_step(b.prm.store.deal(b.deal_id), b.party, "签收了：开专属佣金", None, True, b.clock.now)
        assert b.prm._step_job(b.party)["id"] == job_id                # (her private chat and every group she is in)
    finally:
        stop(b)


# ── her latest line in private, or none: as before ────────────────────────────

def test_her_latest_line_in_her_private_chat_keeps_them_there(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        group_then_private(b)
        notice = ship(b)
        assert notice["chat_id"] == CREATOR and notice["meta"]["reach"] == "private"
        assert "channel" not in notice["meta"]
        b.prm.decide(notice["id"], "drop")
        job = b.relay.store.get(b.prm.ask_huopan(b.deal_id)["job"])
        assert job["chat_id"] == CREATOR and "channel" not in meta_of(job) and "reach" not in meta_of(job)
        assert the_deal(b)["reach"] == {"where": "private", "why": "last_private"}
    finally:
        stop(b)


def test_she_never_wrote_anywhere_it_goes_as_before(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.backend.friends.add(CREATOR)                                 # our friend, silent: her private chat
        assert ship(b)["chat_id"] == CREATOR
        assert the_deal(b)["reach"] == {"where": "private", "why": "never"}
    finally:
        stop(b)
    b = setup(clock, tmp_path)
    try:                                                               # no private chat at all: the group, as before
        notice = ship(b)
        assert notice["chat_id"] == GROUP and "reach" not in notice["meta"]
        assert the_deal(b)["reach"] == {"where": "group", "name": GROUP_NAME, "why": "no_private"}
    finally:
        stop(b)


def test_she_left_the_group_it_goes_to_her_private_chat(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        private_then_group(b)
        b.backend.groups[GROUP]["members"].remove(CREATOR)
        b.clock.now += bg.BOSS_SCAN_SEC + 1
        b.prm.boss_groups.sweep()                                      # (the deal says she left)
        assert b.prm.boss_groups.reach_groups(b.party["id"]) == []
        assert ship(b)["chat_id"] == CREATOR
        assert the_deal(b)["reach"] == {"where": "private", "why": "elsewhere"}
    finally:
        stop(b)


def test_a_read_only_three_party_group_is_never_a_target(clock, tmp_path):
    """The group has the boss in it: linked by hand it became her 三方群; 「不接了」 makes it read-only again.  Her latest
    line was written there (while it was linked): what the bot starts goes to her private chat."""
    b = link_setup(clock, tmp_path, prm_reach_last_spoke="on")
    try:
        b.backend.groups[GROUP]["members"].append(BOSS_ID)
        linked(b, member=1)
        deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
        b.prm.handle(b.msg("私聊问一下"))
        b.clock.now += 60
        b.prm.handle(gmsg(b, "群里说一句"))
        quiet(b)
        assert b.prm.deal_action("group_unlink", {"deal_id": deal_id, "chat": GROUP})["three_party"] is True
        assert b.prm.store.deal(deal_id)["three_party"]["chat"] == GROUP     # (still her 三方群, read only)
        b.clock.now += 60
        b.prm.handle(gmsg(b, "三方群里又说一句"))                         # (read only: never a job, never a target)
        out = b.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"})
        assert b.prm.store.packet(out["packet"])["chat_id"] == CREATOR
    finally:
        stop(b)


# ── her side's lines (PRM_GROUP_HER_SIDE) count only while that switch is on ──

@pytest.mark.parametrize("side", [True, False])
def test_her_sides_line_in_the_group_counts_only_with_her_side_on(clock, tmp_path, side):
    b = setup(clock, tmp_path, side=side)
    try:
        private_then_group(b, sender=BD)                               # her 商务 wrote last, in the group
        assert ship(b)["chat_id"] == (GROUP if side else CREATOR)
    finally:
        stop(b)


def test_her_sides_line_kept_while_her_side_was_on_does_not_count_once_it_is_off(clock, tmp_path):
    b = setup(clock, tmp_path, side=True)
    try:
        private_then_group(b, sender=BD)
        assert b.prm._spoke(b.party) is not None
        b.prm._config.prm_group_her_side = "off"
        restart(b, LinkBackend())
        assert not b.prm.boss_groups.her_side_on and b.prm._spoke(b.party) is None
        out = b.prm.deal_action("shipment", {"deal_id": b.deal_id, "carrier": "顺丰", "waybill": "SF1234567890"})
        assert b.prm.store.packet(out["packet"])["chat_id"] == CREATOR
    finally:
        stop(b)


# ── decided once, when the message is made ────────────────────────────────────

def test_a_notice_decided_for_her_private_chat_does_not_move_when_she_writes_in_the_group(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        group_then_private(b)
        notice = ship(b)
        b.clock.now += 60
        b.prm.handle(gmsg(b, "群里又说一句"))                             # (her latest line is in the group now)
        b.prm.decide(notice["id"], "send")
        b.prm.send_approved()
        assert [chat for chat, _ in b.texts] == [CREATOR]              # (where the console said it would go)
    finally:
        stop(b)


def test_a_notice_made_while_the_bot_was_stopped_is_decided_when_it_is_first_aimed(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        private_then_group(b)
        out = apply_deal_action(b.prm.store, "shipment", {"deal_id": b.deal_id, "carrier": "顺丰",
                                                          "waybill": "SF1234567890"}, now=b.clock.now, takeover_sec=7200)
        assert b.prm.store.packet(out["packet"])["chat_id"] == CREATOR and "reach" not in \
            b.prm.store.packet(out["packet"])["meta"]
        b.prm.decide(out["packet"], "send")
        b.prm.send_approved()
        assert b.texts[-1][0] == GROUP and b.prm.store.packet(out["packet"])["meta"]["reach"] == "last_spoke"
    finally:
        stop(b)


def test_a_new_friends_opening_answers_her_friend_request_in_private(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.backend.friends.add(CREATOR)
        b.prm.handle(gmsg(b, "群里说一句"))
        quiet(b)
        with b.prm._handle_lock:
            job_id, why = b.prm._ask_opening(b.party, how="search")
        assert why == "" and b.relay.store.get(job_id)["chat_id"] == CREATOR
        b.relay.store.cancel_if_pending(job_id, b.clock.now)
        assert b.relay.store.get(b.prm.ask_opening(b.deal_id)["job"])["chat_id"] == GROUP   # (the console's: the group)
    finally:
        stop(b)


# ── the rules of the group still hold ─────────────────────────────────────────

def test_a_notice_its_gate_approved_waits_in_a_group_linked_by_hand(clock, tmp_path):
    """A shipping notice whose kind earned its gate goes out by itself in her private chat; aimed at a group linked by
    hand by this rule it waits for a person (PRM_LINKED_GROUPS_AUTO off: everybody in it reads what goes there)."""
    b = setup(clock, tmp_path)
    try:
        private_then_group(b)
        b.prm.store.set_gate("shipping_notice", "auto", b.clock.now)
        notice = ship(b)
        assert (notice["chat_id"], notice["status"]) == (GROUP, "approved")
        b.prm.send_approved()
        packet = b.prm.store.packet(notice["id"])
        assert b.texts == [] and (packet["status"], packet["route"]) == ("pending", "review")
        assert b.prm._hand_hold(GROUP) == HAND_GROUP_HELD
    finally:
        stop(b)


# ── the switch off: exactly as before ─────────────────────────────────────────

def test_switched_off_nothing_changes(clock, tmp_path):
    b = setup(clock, tmp_path, on=False, prm_handcards="on")
    try:
        private_then_group(b)
        notice = ship(b)
        assert notice["chat_id"] == CREATOR and "reach" not in notice["meta"] and "channel" not in notice["meta"]
        b.prm.decide(notice["id"], "drop")
        for ask in (lambda: b.prm.ask_huopan(b.deal_id)["job"], lambda: b.prm.ask_handcards(b.deal_id, None)["job"],
                    lambda: b.prm.ask_opening(b.deal_id)["job"],
                    lambda: b.prm._dispatch_step(b.prm.store.deal(b.deal_id), b.party, "签收了：开专属佣金", None, True,
                                                 b.clock.now)):
            job = b.relay.store.get(ask())
            assert job["chat_id"] == CREATOR and "channel" not in meta_of(job) and "reach" not in meta_of(job)
            b.relay.store.cancel_if_pending(job["id"], b.clock.now)
        assert "reach" not in the_deal(b)
        assert all("reach" not in p for p in b.prm.snapshot()["pending"])
        assert b.prm._reaching(b.party, made=True) == (CREATOR, None, "")
    finally:
        stop(b)
