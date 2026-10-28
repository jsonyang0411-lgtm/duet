"""聚水潭寄样单状态自动同步（PRM_SAMPLE_SYNC_MIN，src/prm/service.py）：机器人定时把核验成功、还没签收、没被删的
寄样单攒成一个任务交给 Mac（不跑模型，Mac 端见 tests/test_prm_platform_worker.py），答复按控制台的路改合作单：
有了运单号照「填运单号」填上、发货通知一律等人审；已签收照「标已签收」标上、开专属佣金的链条照常（人工接管、停用
时欠着）；删除、驳回、运单号对不上、太久没运单号 / 没签收、聚水潭登录失效都写进合作单的提醒；状态只往前走，
同一个变化只处理一次，重启也一样。Mac 交回来的是 ``platforms.sample_result`` 的形状。"""

import json
from types import SimpleNamespace

import pytest

from test_prm import CREATOR, Bot, Clock, config
from test_prm_platform_flow import approve, mac_runs, ordered, record, system_jobs

from platform_fakes import DY_ITEM, JST_ORDER
from src.config import BotConfig, _validate_config
from src.prm import platforms
from src.prm.service import (SAMPLE_ERRORS, SAMPLE_GIVE_UP_DAYS, SAMPLE_MISSES, SAMPLE_RISK_PAUSE_SEC, SAMPLE_SYNC_CHAT,
                             SAMPLE_SYNC_FIRST_SEC, PrmService, event_out)
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

HOUR, DAY = 3600, 86400
ORDER2 = "YP2610260000002"
WAYBILL = "SF1234567890"


@pytest.fixture
def clock():
    return Clock()


def syncing(b, minutes=60, waybill_days=3, sign_days=10):
    """PRM_SAMPLE_SYNC_MIN / PRM_SAMPLE_WAYBILL_DAYS / PRM_SAMPLE_SIGN_DAYS (the tests' config objects have none: off)."""
    b.prm._sample_sync_sec = minutes * 60
    b.prm._waybill_days, b.prm._sign_days = waybill_days, sign_days
    return b


@pytest.fixture
def bot(clock, tmp_path):
    b = syncing(Bot(clock, tmp_path))
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def made(b, clock, number=JST_ORDER):
    """A sample order drafted, approved and verified in 聚水潭, its reply sent: the deal waits for the sample."""
    order = ordered(b, clock)
    mac_runs(b, approve(b, order), record(sample_order_no=number))
    b.prm.send_approved()
    b.texts.clear()
    return b.prm.store.packet(order["id"])


def verified_order(prm, chat=CREATOR, number=JST_ORDER, at=None, account=""):
    """A ``jst.sample_order`` draft whose execution was verified (its reply sent), put straight into the store (the
    edge and the worker tests have no Mac drafting it)."""
    from platform_fakes import DY_SKU, DY_UID
    party = prm.store.party_by_wxid(chat)
    deal = prm.store.deal_of(party["id"])
    at = prm._clock() if at is None else at
    action = platforms.check_action({"action": "jst.sample_order", "shop": "dy", "item_id": DY_ITEM,
                                     "skus": [{"sku_id": DY_SKU, "qty": 1}], "creator_platform": "dy",
                                     "creator_uid": DY_UID})
    execution = platforms.record("VERIFIED_SUCCESS")
    execution["created"] = {"sample_order_no": number, "creator_uid": DY_UID,
                            **({"creator_account": account} if account else {})}
    packet_id = prm.store.add_packet(
        deal_id=deal["id"], party_id=party["id"], job_id=None, chat_id=chat, kind="money", text="寄样单建好啦",
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="", status="sent",
        decided_at=at, tool="jst.sample_order", platform={"action": action, "preview": {}, "execution": execution},
        now=at)
    return prm.store.packet(packet_id)


def sync_jobs(b):
    return [j for j in b.relay.store.list_jobs(100) if j["chat_id"] == SAMPLE_SYNC_CHAT]


def next_round(b, clock, advance=HOUR + 1):
    """The clock moves on, the sender thread ticks: the round's job, or None when nothing was asked."""
    before = {j["id"] for j in sync_jobs(b)}
    clock.now += advance
    b.prm.tick()
    new = [j for j in sync_jobs(b) if j["id"] not in before]
    return new[0] if new else None


def info(status="WAITING_SEND", waybill=None, carrier="顺丰速运", sent=None, signed=None, reason="", more=()):
    ships = ([{"carrier": carrier, "waybill": waybill, "sent_at": sent}] if waybill else []) + [
        {"carrier": c, "waybill": w, "sent_at": None} for c, w in more]
    return {"status": status, "ships": ships, "sent_at": sent, "signed_at": signed, "reason": reason}


def mac_answers(b, job, *orders, stopped=None, missing=(), not_asked=(), ok=True, error=""):
    """The worker takes the round's job and hands back what 聚水潭 said."""
    taken = b.relay.next_job(0, ("prm",))
    assert taken["id"] == job["id"] and taken["meta"]["op"] == "sample_status"
    result = platforms.sample_result(taken["meta"]["orders"], orders=dict(orders), missing=list(missing),
                                     not_asked=list(not_asked), stopped=stopped)
    body = {"ok": ok, "text": json.dumps(result, ensure_ascii=False) if ok else "", "duration_s": 2,
            **({"error": error} if error else {})}
    assert b.relay.finish(job["id"], body)
    b.relay.tick()
    return taken


def round_says(b, clock, *orders, **kw):
    """A whole round: queued, taken and answered."""
    job = next_round(b, clock)
    assert job is not None
    return mac_answers(b, job, *orders, **kw)


def attention(b, packet):
    return b.prm.store.deal(packet["deal_id"])["attention"]


def notices(b, packet, key):
    return [n for n in b.prm.store.deal(packet["deal_id"])["notices"] if n["key"] == key]


def shipping_notices(b):
    return [p for p in b.prm.store.packets(limit=200) if p["kind"] == "shipping_notice"]


# ── which orders, how often ──────────────────────────────────────────────

def test_once_an_interval_the_verified_orders_not_signed_for_go_to_the_mac_in_one_job(bot, clock):
    assert next_round(bot, clock, SAMPLE_SYNC_FIRST_SEC + 1) is None          # nothing verified: nothing asked
    order = made(bot, clock)
    job = next_round(bot, clock)
    assert json.loads(job["meta"]) == {"op": "sample_status", "orders": [JST_ORDER]}
    assert job["kind"] == "prm" and job["input_closed"] == 1 and "1 张" in job["text"] and CREATOR not in job["text"]
    assert next_round(bot, clock, 60) is None                                 # not before the interval
    assert next_round(bot, clock) is None                                     # due, but the last one is not back yet
    mac_answers(bot, job, (JST_ORDER, info("WAITING_SEND")))
    assert next_round(bot, clock) is not None                                 # back: asked again
    assert bot.prm.store.packet(order["id"])["status"] == "sent" and bot.texts == []


def test_the_sync_is_off_at_zero_and_an_unverified_order_is_never_asked_about(bot, clock):
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record("ACCEPTED_UNVERIFIED", sample_order_no=JST_ORDER))
    assert next_round(bot, clock, 2 * HOUR) is None
    made(bot, clock, ORDER2)
    bot.prm._sample_sync_sec = 0
    assert next_round(bot, clock, 2 * HOUR) is None and sync_jobs(bot) == []


def test_an_order_given_up_on_or_of_a_cleared_deal_is_not_asked_about(bot, clock):
    order = made(bot, clock)
    clock.now += SAMPLE_GIVE_UP_DAYS * DAY + 1
    assert next_round(bot, clock) is None
    assert bot.prm.store.sample_order(JST_ORDER)["done"] == "old"


def test_a_deal_marked_signed_by_hand_before_the_first_round_is_not_asked_about(bot, clock):
    order = made(bot, clock)
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    assert next_round(bot, clock) is None
    assert bot.prm.store.sample_order(JST_ORDER)["done"] == "signed" and len(system_jobs(bot)) == 1


# ── a waybill ─────────────────────────────────────────────────────────────

def test_a_waybill_is_filled_like_the_console_and_its_shipping_notice_always_waits_for_a_person(bot, clock):
    bot.prm.store.set_gate("shipping_notice", "auto", clock.now)               # even a kind that goes out on its own
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL, sent=clock.now - 600)))
    deal = bot.prm.store.deal(order["deal_id"])
    shipment = bot.prm.store.latest_shipment(deal["id"])
    assert deal["stage"] == "shipped" and (shipment["carrier"], shipment["waybill"]) == ("顺丰速运", WAYBILL)
    [notice] = shipping_notices(bot)
    assert notice["status"] == "pending" and notice["route"] == "review" and WAYBILL in notice["text"]
    assert notice["meta"]["source"] == "system" and notice["meta"]["sample_order"] == JST_ORDER
    assert JST_ORDER in notice["reason"]
    bot.prm.tick()
    assert bot.texts == []                                                      # nothing goes out by itself
    filled = next(e for e in bot.prm.store.events(deal["id"], limit=30) if e["kind"] == "shipment")
    assert filled["actor"] == "system" and filled["detail"]["order"] == JST_ORDER
    assert event_out(filled)["name"] == "填运单号（聚水潭同步）"
    status = next(e for e in bot.prm.store.events(deal["id"], limit=30) if e["kind"] == "sample_status")
    assert event_out(status)["name"] == f"聚水潭寄样单 {JST_ORDER}：已发货（{WAYBILL}）"
    assert bot.prm.decide(notice["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, f"小美你好～样品已经寄出啦：顺丰速运 {WAYBILL}。签收之后跟我说一声哦～")]
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))                  # the same again: nothing more
    assert len(shipping_notices(bot)) == 1
    assert len([e for e in bot.prm.store.events(deal["id"], limit=50) if e["kind"] in ("shipment", "sample_status")]) == 2


def test_a_waybill_an_operator_typed_that_jushuitan_does_not_have_is_kept_and_the_deal_says_so(bot, clock):
    order = made(bot, clock)
    assert bot.prm.deal_action("shipment", {"deal_id": order["deal_id"], "carrier": "中通", "waybill": "ZT0000000001"})["ok"]
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    assert bot.prm.store.latest_shipment(order["deal_id"])["waybill"] == "ZT0000000001"      # not overwritten
    assert len(shipping_notices(bot)) == 1                                        # only the operator's own
    said = attention(bot, order)
    assert "对不上" in said and WAYBILL in said and "ZT0000000001" in said and "系统没有改" in said
    assert bot.prm.deal_action("shipment", {"deal_id": order["deal_id"], "carrier": "顺丰", "waybill": WAYBILL})["ok"]
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    assert "对不上" not in attention(bot, order)


def test_a_waybill_jushuitan_changed_after_the_sync_filled_one_is_filled_again(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    round_says(bot, clock, (JST_ORDER, info("SENT", "SF9999999999")))
    assert bot.prm.store.latest_shipment(order["deal_id"])["waybill"] == "SF9999999999"
    first, second = sorted(shipping_notices(bot), key=lambda p: p["id"])
    assert first["status"] == "superseded" and second["status"] == "pending" and "SF9999999999" in second["text"]


def test_several_waybills_fill_the_first_and_name_the_others(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL, sent=clock.now, more=[("中通", "ZT0000000002")])))
    assert bot.prm.store.latest_shipment(order["deal_id"])["waybill"] == WAYBILL
    assert "有 2 个运单号" in attention(bot, order) and "ZT0000000002" in attention(bot, order)


# ── signed for ────────────────────────────────────────────────────────────

def test_signed_for_marks_the_deal_and_starts_the_commission_chain_once(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    for pending in bot.prm.store.packets(("pending",)):
        bot.prm.decide(pending["id"], "drop")                                    # the shipping notice: not this test
    round_says(bot, clock, (JST_ORDER, info("SIGNED", WAYBILL, signed=clock.now - 60)))
    deal = bot.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "delivered" and deal["delivered_at"] == clock.now
    assert bot.prm.store.latest_shipment(deal["id"])["status"] == "delivered"
    [step] = system_jobs(bot)
    note = step["meta"] if isinstance(step["meta"], dict) else json.loads(step["meta"])
    assert "达人已经签收了寄样单" in note["system_note"] and JST_ORDER in note["system_note"]
    assert DY_ITEM in note["system_note"]
    marked = [e for e in bot.prm.store.events(deal["id"], limit=50) if e["kind"] == "shipment_status"]
    assert len(marked) == 1 and marked[0]["actor"] == "system" and event_out(marked[0])["name"].endswith("（聚水潭同步）")
    assert bot.prm.store.sample_order(JST_ORDER)["done"] == "signed"
    assert next_round(bot, clock) is None and len(system_jobs(bot)) == 1          # not asked about again


def test_signed_for_before_any_waybill_records_it_without_a_shipping_notice(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("SIGNED", WAYBILL, sent=clock.now - DAY, signed=clock.now)))
    deal = bot.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "delivered" and bot.prm.store.latest_shipment(deal["id"])["waybill"] == WAYBILL
    assert shipping_notices(bot) == [] and len(system_jobs(bot)) == 1


def test_signed_for_without_any_waybill_marks_the_stage(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("SIGNED", signed=clock.now)))
    deal = bot.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "delivered" and bot.prm.store.latest_shipment(deal["id"]) is None
    assert len(system_jobs(bot)) == 1


@pytest.mark.parametrize("how", ["human", "inactive"])
def test_signed_for_while_a_person_has_the_deal_or_the_creator_is_off_the_list_owes_the_step(bot, clock, how):
    order = made(bot, clock)
    if how == "human":
        assert bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "paused"})["ok"]
    else:
        bot.prm.set_active(order["party_id"], False)
    round_says(bot, clock, (JST_ORDER, info("SIGNED", WAYBILL, signed=clock.now)))
    deal = bot.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "delivered" and system_jobs(bot) == []
    assert f"#{order['id']} 之后该开专属佣金了" in deal["attention"]
    if how == "human":
        assert bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "copilot"})["ok"]
    else:
        bot.prm.set_active(order["party_id"], True)
    bot.prm.tick()
    assert len(system_jobs(bot)) == 1


def test_only_the_deals_newest_order_changes_the_deal(bot, clock):
    first = made(bot, clock, JST_ORDER)
    made(bot, clock, ORDER2)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)), (ORDER2, info("WAITING_SEND")))
    assert bot.prm.store.latest_shipment(first["deal_id"]) is None and shipping_notices(bot) == []
    assert "不是这张合作单最新的寄样单" in attention(bot, first)
    round_says(bot, clock, (JST_ORDER, info("SIGNED", WAYBILL)), (ORDER2, info("WAITING_SEND")))
    deal = bot.prm.store.deal(first["deal_id"])
    assert deal["stage"] != "delivered" and system_jobs(bot) == []
    assert bot.prm.store.sample_order(JST_ORDER)["done"] == "signed"
    assert "不是这张合作单最新的寄样单" not in deal["attention"]


def test_a_deal_past_the_sample_is_left_as_it_is(bot, clock):
    order = made(bot, clock)
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "published"})["ok"]
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    round_says(bot, clock, (JST_ORDER, info("SIGNED", WAYBILL)))
    deal = bot.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "published" and bot.prm.store.latest_shipment(deal["id"]) is None
    assert shipping_notices(bot) == [] and system_jobs(bot) == []


# ── deleted, rejected, not found ──────────────────────────────────────────

def test_a_deleted_order_turns_the_deal_red_with_its_reason_until_someone_sees_to_it(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("DELETED", reason="地址不全，重新提交")))
    said = attention(bot, order)
    assert f"聚水潭寄样单 {JST_ORDER} 被删除了（地址不全，重新提交）" in said
    assert bot.prm.store.sample_order(JST_ORDER)["done"] == "deleted" and next_round(bot, clock) is None
    clock.now += 5 * DAY
    bot.prm.tick()
    assert "被删除了" in attention(bot, order)                                     # until someone sees to it
    deleted = next(e for e in bot.prm.store.events(order["deal_id"], limit=30) if e["kind"] == "sample_status")
    assert event_out(deleted)["name"] == f"聚水潭寄样单 {JST_ORDER}：已删除，原因：地址不全，重新提交"
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "address"})["ok"]
    assert "被删除了" not in attention(bot, order)


def test_a_new_order_sees_to_an_earlier_ones_deletion(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("DELETED")))
    assert "被删除了（聚水潭没写原因）" in attention(bot, order)
    made(bot, clock, ORDER2)
    job = next_round(bot, clock)
    assert json.loads(job["meta"])["orders"] == [ORDER2]
    assert "被删除了" not in attention(bot, order)


def test_a_rejected_order_says_why_until_it_is_audited_again(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("INIT", reason="SKU 缺货")))
    assert f"聚水潭寄样单 {JST_ORDER} 被驳回了（SKU 缺货）" in attention(bot, order)
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND")))
    assert "被驳回" not in attention(bot, order)
    assert bot.prm.store.sample_order(JST_ORDER)["done"] == ""


def test_the_clean_up_cuts_what_jushuitan_said_of_a_deleted_order(bot, clock):
    """8.2: 聚水潭's reason for a deletion can hold the address or the number ("地址不全…"): the clean-up cuts it in
    the order kept, in what was done of it and in the sync's answer, as in the deal's notices and events."""
    order = made(bot, clock)
    why = "地址不全：桂城街道海八东路12号保利花园，电话13900001111"
    round_says(bot, clock, (JST_ORDER, info("DELETED", reason=why)))
    kept = bot.prm.store.sample_order(JST_ORDER)
    assert kept["reason"] == why and why in kept["applied"]["dead"]                  # the operators see it until then
    bot.prm.purge(bot.prm.store.deal(order["deal_id"]), clock.now)
    everything = json.dumps([bot.prm.store.sample_order(JST_ORDER), sync_jobs(bot),
                             bot.prm.store.events(order["deal_id"], limit=100), bot.prm.store.deal(order["deal_id"])],
                            ensure_ascii=False)
    for gone in ("海八东路12号", "13900001111"):
        assert gone not in everything, gone
    assert "地址不全" in bot.prm.store.sample_order(JST_ORDER)["reason"]


def test_an_order_jushuitan_does_not_find_twice_is_said(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, missing=[JST_ORDER])
    assert "查不到" not in attention(bot, order)
    for _ in range(SAMPLE_MISSES - 1):
        round_says(bot, clock, missing=[JST_ORDER])
    assert f"聚水潭里查不到寄样单 {JST_ORDER}（连续 {SAMPLE_MISSES} 次）" in attention(bot, order)
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND")))
    assert "查不到" not in attention(bot, order)


# ── reminders ─────────────────────────────────────────────────────────────

def test_no_waybill_days_after_the_order_and_no_signature_days_after_sending_are_said(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND")))
    assert "还没有运单号" not in attention(bot, order)
    clock.now += 3 * DAY
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND")))
    assert f"聚水潭寄样单 {JST_ORDER} 建好 3 天了还没有运单号（待发货）" in attention(bot, order)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL, sent=clock.now)))
    assert "还没有运单号" not in attention(bot, order)
    clock.now += 10 * DAY
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    assert f"聚水潭寄样单 {JST_ORDER} 发货 10 天了还没签收（顺丰速运 {WAYBILL}）" in attention(bot, order)
    round_says(bot, clock, (JST_ORDER, info("SIGNED", WAYBILL)))
    assert "还没签收" not in attention(bot, order)


def test_the_reminder_days_follow_the_settings(bot, clock):
    syncing(bot, waybill_days=1)
    order = made(bot, clock)
    clock.now += DAY
    round_says(bot, clock, (JST_ORDER, info("INIT")))
    assert "建好 1 天了还没有运单号（待审核）" in attention(bot, order)


# ── the status only goes forward; a change once; a restart ────────────────

def test_the_status_only_goes_forward_and_a_change_is_acted_on_once(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND")))
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND", WAYBILL)))           # an older read: not back
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))
    assert bot.prm.store.sample_order(JST_ORDER)["status"] == "SENT"
    said = [e["detail"]["status"] for e in reversed(bot.prm.store.events(order["deal_id"], limit=50))
            if e["kind"] == "sample_status"]
    assert said == ["WAITING_SEND", "SENT"]
    assert len(shipping_notices(bot)) == 1 and bot.prm.store.deal(order["deal_id"])["stage"] == "shipped"


class FileBot(Bot):
    """The relay and the PRM service on files: a restart is a new one on the same files."""

    def __init__(self, clock, tmp_path):
        self.clock = clock
        self.texts, self.files, self.relay_sent = [], [], []
        self.send_ok = self.files_ok = True
        self.changed = 0
        self.relay = ClaudeRelay(config(), store=RelayStore(tmp_path / "relay.db"), clock=clock,
                                 files_dir=tmp_path / "relay", send=lambda c, t: self.relay_sent.append((c, t)) or True)
        self.prm = PrmService(config(), self.relay, store=PrmStore(tmp_path / "prm.db"), clock=clock,
                              send=self._send, send_file=self._send_file, on_parties_changed=self._changed)
        syncing(self)


def test_a_restart_neither_asks_again_at_once_nor_acts_on_a_change_twice(clock, tmp_path):
    first = FileBot(clock, tmp_path)
    first.prm.register(CREATOR, "小美", "测试达人")
    order = made(first, clock)
    round_says(first, clock, (JST_ORDER, info("SENT", WAYBILL)))
    first.prm.stop()
    first.relay.stop()
    again = FileBot(clock, tmp_path)                                         # restarted
    assert next_round(again, clock, SAMPLE_SYNC_FIRST_SEC + 1) is None       # the last round was not an interval ago
    round_says(again, clock, (JST_ORDER, info("SENT", WAYBILL)))
    assert len(shipping_notices(again)) == 1
    assert len([e for e in again.prm.store.events(order["deal_id"], limit=50) if e["kind"] == "shipment"]) == 1
    round_says(again, clock, (JST_ORDER, info("SIGNED", WAYBILL)))
    assert len(system_jobs(again)) == 1 and again.prm.store.deal(order["deal_id"])["stage"] == "delivered"
    again.prm.stop()
    again.relay.stop()


# ── when a round could not be made ────────────────────────────────────────

LOGIN = {"verdict": "login", "exit_code": 77, "message": "退出码 77（登录失效）：[NOT_LOGGED_IN] 聚水潭登录失效"}


def test_a_lapsed_login_is_said_once_and_goes_away_by_itself(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, not_asked=[JST_ORDER], stopped=LOGIN)
    [said] = notices(bot, order, "sample_sync")
    assert "聚水潭登录失效（退出码 77）" in said["text"] and said["text"] in attention(bot, order)
    updated = bot.prm.store.deal(order["deal_id"])["updated_at"]
    events = len(bot.prm.store.events(order["deal_id"], limit=100))
    round_says(bot, clock, not_asked=[JST_ORDER], stopped=LOGIN)
    assert notices(bot, order, "sample_sync") == [said]                          # said once
    assert bot.prm.store.deal(order["deal_id"])["updated_at"] == updated         # (not the deal's own sign of life)
    assert len(bot.prm.store.events(order["deal_id"], limit=100)) == events
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND")))
    assert notices(bot, order, "sample_sync") == [] and "登录失效" not in attention(bot, order)


def test_a_risk_wall_pauses_the_sync_longer(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, not_asked=[JST_ORDER], stopped={"verdict": "wall", "exit_code": 76, "message": "限流"})
    assert "聚水潭限流或者要人机验证（退出码 76）" in attention(bot, order)
    assert next_round(bot, clock) is None                                        # an interval later: still paused
    assert next_round(bot, clock, SAMPLE_RISK_PAUSE_SEC) is not None


@pytest.mark.parametrize("stopped, words", [
    ({"verdict": "wrong_account", "exit_code": 79, "message": "登录的是账号 900002"}, "不是这台 Mac 配的子账号（退出码 79）"),
    ({"verdict": "unavailable", "exit_code": None, "message": "这台 Mac 的 config.json 里没有配置聚水潭的命令行项目"},
     "Mac 上查不了聚水潭"),
    ({"verdict": "invalid", "exit_code": 2, "message": "[USAGE] 不认识的参数"}, "请更新 jushuitan-engineering"),
])
def test_what_needs_a_person_on_the_mac_is_said_at_once(bot, clock, stopped, words):
    order = made(bot, clock)
    round_says(bot, clock, not_asked=[JST_ORDER], stopped=stopped)
    assert words in attention(bot, order)


def test_a_worker_too_old_for_the_job_is_said_at_once(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, ok=False, error="任务里没有达人编号")
    assert "Mac 上的 worker 还不会查聚水潭寄样单的状态" in attention(bot, order)


def test_a_hiccup_is_said_only_after_three_rounds_in_a_row(bot, clock):
    order = made(bot, clock)
    hiccup = {"verdict": "error", "exit_code": 1, "message": "退出码 1：[FAILED] fetch failed"}
    for _ in range(SAMPLE_ERRORS - 1):
        round_says(bot, clock, not_asked=[JST_ORDER], stopped=hiccup)
        assert "没同步成" not in attention(bot, order)
    round_says(bot, clock, not_asked=[JST_ORDER], stopped=hiccup)
    assert f"寄样单状态连续 {SAMPLE_ERRORS} 次没同步成" in attention(bot, order)
    round_says(bot, clock, (JST_ORDER, info("WAITING_SEND")))
    assert "没同步成" not in attention(bot, order)


def test_an_answer_that_cannot_be_read_is_a_hiccup(bot, clock):
    made(bot, clock)
    job = next_round(bot, clock)
    bot.relay.next_job(0, ("prm",))
    assert bot.relay.finish(job["id"], {"ok": True, "text": "not json"})
    bot.relay.tick()
    assert bot.prm._sample_errors == 1


def test_a_partial_answer_is_applied_before_the_stop(bot, clock):
    first = made(bot, clock, JST_ORDER)
    made(bot, clock, ORDER2)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)), not_asked=[ORDER2], stopped=LOGIN)
    assert bot.prm.store.sample_order(JST_ORDER)["status"] == "SENT"
    assert "聚水潭登录失效" in attention(bot, first)


# ── the settings ──────────────────────────────────────────────────────────

def test_the_sync_settings_have_defaults_and_ranges(monkeypatch):
    config_ = BotConfig()
    assert (config_.prm_sample_sync_min, config_.prm_sample_waybill_days, config_.prm_sample_sign_days) == (60, 3, 10)
    _validate_config({"prm_sample_sync_min": 0})                              # 0 = off
    with pytest.raises(RuntimeError) as e:
        _validate_config({"prm_sample_sync_min": 5, "prm_sample_waybill_days": 0, "prm_sample_sign_days": 99})
    for key in ("PRM_SAMPLE_SYNC_MIN", "PRM_SAMPLE_WAYBILL_DAYS", "PRM_SAMPLE_SIGN_DAYS"):
        assert key in str(e.value)
    service = PrmService(SimpleNamespace(**vars(config()), prm_sample_sync_min=30, prm_sample_waybill_days=2,
                                         prm_sample_sign_days=7),
                         ClaudeRelay(config(), store=RelayStore(":memory:")), store=PrmStore(":memory:"))
    assert (service._sample_sync_sec, service._waybill_days, service._sign_days) == (1800, 2, 7)
    old = PrmService(config(), ClaudeRelay(config(), store=RelayStore(":memory:")), store=PrmStore(":memory:"))
    assert old._sample_sync_sec == 0                                          # a config without it: off
