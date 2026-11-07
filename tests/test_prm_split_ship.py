"""拆成几个包裹的寄样单（2026-11-06，用户：「等发货单全了再说」）：仓库先发有货的那一包、别的等到货再发，聚水潭给一张
寄样单几个 ERP 单（包裹），先发的有运单号、别的还没有。Mac 数一下有几个包裹、几个还没运单号（``packages`` /
``unshipped``，旧的 Mac 没有这两个数：照以前整张算）；机器人等每个包裹都有了运单号才填进合作单、出发货通知，通知里写全
几个运单号。只发了一部分时：机器人自己建的单在合作单上留一条提醒、不给达人发任何话；手动建的单什么都不出。第一包发出
PRM_SAMPLE_WAYBILL_DAYS 天了别的还没运单号：合作单的提醒改成请人去聚水潭看一下（手动建的单：「待审批」顶上一行，
到「知道了」为止），在合作单上「填运单号」照样能先给达人发通知；剩下的发出以后照样处理。聚水潭说已签收算整张。
名字、号码、单号都是编的。"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from test_prm import CREATOR, Bot, Clock
from test_prm_hand_samples import H2, H3, a_round, deal, lines, ours
from test_prm_hand_samples import notices as hand_notices
from test_prm_platform_flow import system_jobs
from test_prm_sample_sync import (DAY, WAYBILL, FileBot, attention, info, made, next_round, round_says,
                                  shipping_notices, syncing)

from platform_fakes import JST_ORDER, JST_SHOP_DY, JST_SHOP_XHS, JST_UID, FakeNode, jst_sample_row, jst_samples, rule
from src.prm import deals, platforms
from src.prm.service import HAND_SPLIT
from src.prm.store import PrmStore

ROOT = Path(__file__).resolve().parent.parent
ZTO = "ZT0000000002"
FIRST_TEXT = f"老师您好～样品已经寄出啦：顺丰速运 {WAYBILL}。签收之后跟我说一声哦～"
BOTH_TEXT = f"老师您好～样品已经寄出啦（分 2 个包裹）：顺丰速运 {WAYBILL}、中通 {ZTO}。签收之后跟我说一声哦～"


def part(status="SENT", out=1, of=2, **kw):
    """The bot's own order as the Mac answers it: ``of`` packages, ``out`` of them with a waybill (the first one's
    WAYBILL; more waybills with ``more``)."""
    return {**info(status, WAYBILL, **kw), "packages": of, "unshipped": of - out}


def whole(status="SENT", **kw):
    """Every package out: WAYBILL and ZTO."""
    return {**info(status, WAYBILL, more=[("中通", ZTO)], **kw), "packages": 2, "unshipped": 0}


# ── the Mac: how many packages, how many without a waybill ─────────────────

@pytest.fixture
def node(tmp_path, monkeypatch):
    return FakeNode(tmp_path / "mac", monkeypatch)


@pytest.fixture
def jcfg(node):
    return platforms.config(node.settings(jst_shops={"dy": JST_SHOP_DY, "xhs": JST_SHOP_XHS}, jst_expect_uid=JST_UID,
                                          jst_logistics=["SF", "ZTO"]))


def test_the_mac_counts_the_packages_and_the_ones_without_a_waybill(node, jcfg):
    half = [("中通速递", "ZT0000000001", "2026-11-06 14:00:00"), ("中通速递", None, None)]
    node.rules([rule(["--mine"], jst_samples(jst_sample_row(H2, "WAITING_SEND", waybills=half))),
                rule(["samples"], jst_samples(jst_sample_row(JST_ORDER, "WAITING_SEND", waybills=half)))])
    out = platforms.sample_statuses(jcfg, [JST_ORDER], sleep=lambda s: None, mine=True)
    for got in (out["orders"][JST_ORDER], out["mine"][H2]):
        assert (got["packages"], got["unshipped"]) == (2, 1)
        assert [s["waybill"] for s in got["ships"]] == ["ZT0000000001"]
    checked = platforms.check_sample_result(json.loads(json.dumps(out)), [JST_ORDER])
    for got in (checked["orders"][JST_ORDER], checked["mine"][H2]):
        assert (got["packages"], got["unshipped"]) == (2, 1)
        assert platforms.sample_split(got) == (1, 2)


def test_packages_that_share_a_waybill_and_odd_rows_are_counted_as_they_are():
    shared = jst_sample_row(JST_ORDER, "SENT", waybills=[("中通", "ZT0000000001", None), ("中通", "ZT0000000001", None)])
    got = platforms.sample_info(shared)
    assert (got["packages"], got["unshipped"], len(got["ships"])) == (2, 0, 1)
    assert platforms.sample_split(got) is None                               # every package has a waybill
    odd = {**shared, "erpOrders": [{"logisticsNo": "bad bill"}, "not a dict", {"logisticsNo": "ZT0000000001"}]}
    got = platforms.sample_info(odd)
    assert (got["packages"], got["unshipped"]) == (2, 1)                     # a waybill that is none counts as none
    none = platforms.sample_info({**shared, "erpOrders": None})
    assert (none["packages"], none["unshipped"], none["ships"]) == (0, 0, [])
    many = platforms.sample_info({**shared, "erpOrders": [{"logisticsNo": None}] * 80})
    assert (many["packages"], many["unshipped"]) == (platforms.SAMPLE_PACKAGES_MAX, platforms.SAMPLE_PACKAGES_MAX)


@pytest.mark.parametrize("counts", [{}, {"packages": "2", "unshipped": 1}, {"packages": True, "unshipped": 1},
                                    {"packages": 2, "unshipped": 3}, {"packages": 2, "unshipped": -1},
                                    {"packages": 99, "unshipped": 1}, {"packages": 2}])
def test_an_old_worker_or_counts_that_make_no_sense_take_the_order_whole(counts):
    value = {"op": "sample_status", "orders": {JST_ORDER: {"status": "SENT", "ships": [{"waybill": WAYBILL}], **counts}}}
    got = platforms.check_sample_result(value, [JST_ORDER])["orders"][JST_ORDER]
    assert (got["packages"], got["unshipped"]) == (None, None)
    assert platforms.sample_split(got) is None


def test_only_an_order_partly_out_is_split():
    ship = [{"carrier": "中通", "waybill": WAYBILL, "sent_at": None}]
    assert platforms.sample_split({"status": "SENT", "ships": ship, "packages": 3, "unshipped": 1}) == (2, 3)
    assert platforms.sample_split({"status": "WAITING_SEND", "ships": ship, "packages": 2, "unshipped": 1}) == (1, 2)
    assert platforms.sample_split({"status": "SIGNED", "ships": ship, "packages": 2, "unshipped": 1}) is None
    assert platforms.sample_split({"status": "DELETED", "ships": ship, "packages": 2, "unshipped": 1}) is None
    assert platforms.sample_split({"status": "SENT", "ships": [], "packages": 2, "unshipped": 2}) is None
    assert platforms.sample_split({"status": "SENT", "ships": ship, "packages": 2, "unshipped": 0}) is None
    assert platforms.sample_split({"status": "SENT", "ships": ship, "packages": 1, "unshipped": 1}) is None
    assert platforms.sample_split(None) is None


def test_the_counts_run_on_the_macs_system_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json\n"
        f"spec = importlib.util.spec_from_file_location('p', {str(ROOT / 'src/prm/platforms.py')!r})\n"
        "p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)\n"
        "row = {'orderNo': 'YP2611060000001', 'status': 'WAITING_SEND', 'erpOrders': [\n"
        "    {'logisticsCompany': 'ZTO', 'logisticsNo': 'ZT0000000001', 'sendTime': None},\n"
        "    {'logisticsCompany': 'ZTO', 'logisticsNo': None, 'sendTime': None}]}\n"
        "info = p.sample_info(row)\n"
        "out = p.sample_result(['YP2611060000001'], orders={info.pop('order_no'): info})\n"
        "got = p.check_sample_result(json.loads(json.dumps(out)), ['YP2611060000001'])\n"
        "o = got['orders']['YP2611060000001']\n"
        "assert (o['packages'], o['unshipped']) == (2, 1), o\n"
        "assert p.sample_split(o) == (1, 2), o\n"
        "print('ok')\n", encoding="utf-8")
    done = subprocess.run([python, str(script)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


# ── the shipping notice names every package ────────────────────────────────

def test_the_shipping_notice_names_every_package():
    assert deals.notice_text("顺丰速运", WAYBILL) == FIRST_TEXT                  # one package: as ever
    assert deals.notice_text("顺丰速运", WAYBILL, [("中通", ZTO)]) == BOTH_TEXT
    kept = deals._more_packages([("中通", ZTO), {"carrier": "", "waybill": "ZT0000000003"}, ("中通", WAYBILL),
                                 ("中通", ZTO), ("中\n通", "ZT0000000004"), ("中通", "bad bill"), "odd", ("x" * 21, "ZT5555555")]
                                + [("中通", f"ZT000000001{i}") for i in range(9)], WAYBILL)
    assert kept[:2] == [("中通", ZTO), ("快递", "ZT0000000003")]               # the first waybill and repeats left out
    assert len(kept) == deals.MORE_PACKAGES


# ── the bot's own orders ───────────────────────────────────────────────────

@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bot(clock, tmp_path):
    b = syncing(Bot(clock, tmp_path))
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def test_part_of_an_order_out_waits_quietly_then_one_notice_names_every_package(bot, clock):
    order = made(bot, clock)
    stage = bot.prm.store.deal(order["deal_id"])["stage"]
    round_says(bot, clock, (JST_ORDER, part()))
    deal_now = bot.prm.store.deal(order["deal_id"])
    assert bot.prm.store.latest_shipment(deal_now["id"]) is None and deal_now["stage"] == stage
    assert shipping_notices(bot) == [] and bot.texts == []
    said = attention(bot, order)
    assert f"聚水潭寄样单 {JST_ORDER} 拆成了 2 个包裹，先发了 1 个（顺丰速运 {WAYBILL}）" in said
    assert "等全部发出，系统再给达人发货通知" in said
    round_says(bot, clock, (JST_ORDER, part()))                                  # still part: still nothing
    assert shipping_notices(bot) == [] and bot.prm.store.latest_shipment(order["deal_id"]) is None
    round_says(bot, clock, (JST_ORDER, whole()))
    shipment = bot.prm.store.latest_shipment(order["deal_id"])
    assert (shipment["carrier"], shipment["waybill"]) == ("顺丰速运", WAYBILL)   # the deal keeps the first
    [notice] = shipping_notices(bot)
    assert notice["status"] == "pending" and notice["route"] == "review" and notice["text"] == BOTH_TEXT
    assert notice["slots"]["其余包裹"] == f"中通 {ZTO}" and notice["meta"]["more_waybills"] == [
        {"carrier": "中通", "waybill": ZTO}]
    assert "拆成了" not in attention(bot, order) and "有 2 个运单号" in attention(bot, order)
    assert "split_since" not in bot.prm.store.sample_order(JST_ORDER)["applied"]
    assert bot.prm.decide(notice["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, BOTH_TEXT)]
    round_says(bot, clock, (JST_ORDER, whole()))                                 # the same again: nothing more
    assert len(shipping_notices(bot)) == 1


def test_a_notice_may_carry_its_own_other_packages_waybills_and_no_other_number(bot, clock):
    from src.prm import gates
    assert gates.waybill_ok(BOTH_TEXT, WAYBILL, [ZTO]) is True
    assert gates.waybill_ok(BOTH_TEXT, WAYBILL) is False                         # (a notice of one package: as ever)
    assert gates.waybill_ok(BOTH_TEXT + "，另一个 7700112233", WAYBILL, [ZTO]) is False
    made(bot, clock)
    round_says(bot, clock, (JST_ORDER, whole()))
    [notice] = shipping_notices(bot)
    bad = bot.prm.decide(notice["id"], "edit", BOTH_TEXT.replace(ZTO, "ZT0000009999"))
    assert bad["ok"] is False and "对不上" in bad["error"]
    edited = BOTH_TEXT.replace("签收之后", "到了以后")
    assert bot.prm.decide(notice["id"], "edit", edited)["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, edited)]


def test_the_rest_not_out_after_the_waybill_days_asks_for_a_look_and_a_typed_waybill_still_notifies(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, part()))
    assert "先发了 1 个" in attention(bot, order)
    clock.now += 3 * DAY
    round_says(bot, clock, (JST_ORDER, part()))
    said = attention(bot, order)
    assert f"聚水潭寄样单 {JST_ORDER} 拆成了 2 个包裹，3 天了只发出 1 个（顺丰速运 {WAYBILL}），另外的还没有运单号" in said
    assert "请到聚水潭寄样管理看一下" in said and "要先给达人发货通知，就在合作单上填运单号" in said
    assert shipping_notices(bot) == []
    assert bot.prm.deal_action("shipment", {"deal_id": order["deal_id"], "carrier": "顺丰速运", "waybill": WAYBILL})["ok"]
    [typed] = shipping_notices(bot)                                              # the console's way: a notice
    assert WAYBILL in typed["text"]
    round_says(bot, clock, (JST_ORDER, part()))
    assert "对不上" not in attention(bot, order) and len(shipping_notices(bot)) == 1
    round_says(bot, clock, (JST_ORDER, whole()))                                 # the rest went: no second notice
    assert len(shipping_notices(bot)) == 1
    assert bot.prm.store.latest_shipment(order["deal_id"])["waybill"] == WAYBILL


def test_the_waybill_days_follow_the_setting(bot, clock):
    syncing(bot, waybill_days=1)
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, part()))
    clock.now += DAY
    round_says(bot, clock, (JST_ORDER, part()))
    assert "1 天了只发出 1 个" in attention(bot, order)


def test_signed_for_while_part_still_waits_counts_as_whole(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, part()))
    round_says(bot, clock, (JST_ORDER, part("SIGNED", signed=clock.now)))
    d = bot.prm.store.deal(order["deal_id"])
    assert d["stage"] == "delivered" and bot.prm.store.latest_shipment(d["id"])["waybill"] == WAYBILL
    assert shipping_notices(bot) == [] and len(system_jobs(bot)) == 1          # the 专属佣金 chain as ever
    assert "拆成了" not in attention(bot, order)


def test_an_old_workers_answer_is_taken_whole_and_names_every_waybill(bot, clock):
    order = made(bot, clock)
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)))                   # no counts: as before
    [notice] = shipping_notices(bot)
    assert notice["text"] == FIRST_TEXT
    other = made(bot, clock, "YP2610260000009")
    for p in bot.prm.store.packets(("pending",)):
        bot.prm.decide(p["id"], "drop")
    round_says(bot, clock, (JST_ORDER, info("SENT", WAYBILL)),
               ("YP2610260000009", info("SENT", "SF0000000009", more=[("中通", ZTO)])))
    newest = max(shipping_notices(bot), key=lambda p: p["id"])
    assert newest["text"] == f"老师您好～样品已经寄出啦（分 2 个包裹）：顺丰速运 SF0000000009、中通 {ZTO}。签收之后跟我说一声哦～"
    assert other["deal_id"] == order["deal_id"]


def test_a_restart_in_the_middle_keeps_when_the_first_package_was_seen(clock, tmp_path):
    first = FileBot(clock, tmp_path)
    first.prm.register(CREATOR, "小美", "测试达人")
    order = made(first, clock)
    round_says(first, clock, (JST_ORDER, part()))
    first.prm.stop()
    first.relay.stop()
    clock.now += 3 * DAY
    again = FileBot(clock, tmp_path)
    try:
        job = next_round(again, clock)
        assert job is not None
        from test_prm_sample_sync import mac_answers
        mac_answers(again, job, (JST_ORDER, part()))
        assert "3 天了只发出 1 个" in again.prm.store.deal(order["deal_id"])["attention"]
        round_says(again, clock, (JST_ORDER, whole()))
        [notice] = shipping_notices(again)
        assert notice["text"] == BOTH_TEXT
    finally:
        again.prm.stop()
        again.relay.stop()


# ── our orders made by hand ────────────────────────────────────────────────

@pytest.fixture
def hbot(clock, tmp_path):
    b = syncing(Bot(clock, tmp_path))
    b.prm.register(CREATOR, "小美", "测试达人")
    b.directory = [{"wxid": CREATOR, "platform": "DY", "platform_no": "test_daren_01", "platform_uid": ""}]
    b.prm.creator_directory = lambda: list(b.directory)
    yield b
    b.prm.stop()
    b.relay.stop()


def hpart(account="test_daren_01", out=1, of=2, status="SENT"):
    return {**ours(status, "SF0000000002", account=account), "packages": of, "unshipped": of - out}


def hwhole(account="test_daren_01", status="SENT"):
    h = ours(status, "SF0000000002", account=account)
    return {**h, "ships": h["ships"] + [{"carrier": "中通", "waybill": ZTO, "sent_at": None}], "packages": 2,
            "unshipped": 0}


def test_a_hand_made_order_out_in_part_shows_nothing_until_the_rest_then_fills_her_deal_once(hbot, clock):
    a_round(hbot, clock, {H2: ours("WAITING_SEND")})                             # the first read: the baseline
    a_round(hbot, clock, {H2: hpart()})
    row = hbot.prm.store.hand_sample(H2)
    assert row["state"] == "waiting" and row["sent_at"] == clock.now and row["waybill"] == ""
    assert deal(hbot)["stage"] == "talk" and hand_notices(hbot) == [] and lines(hbot) == [] and hbot.texts == []
    a_round(hbot, clock, {H2: hwhole()})
    d = deal(hbot)
    assert d["stage"] == "shipped" and hbot.prm.store.latest_shipment(d["id"])["waybill"] == "SF0000000002"
    [notice] = hand_notices(hbot)
    assert notice["text"] == f"老师您好～样品已经寄出啦（分 2 个包裹）：顺丰速运_电商 SF0000000002、中通 {ZTO}。签收之后跟我说一声哦～"
    assert notice["route"] == "review" and hbot.prm.store.hand_sample(H2)["state"] == "filled" and lines(hbot) == []
    a_round(hbot, clock, {H2: hwhole()})
    assert len(hand_notices(hbot)) == 1


@pytest.mark.parametrize("account, then", [("test_daren_01", "filled"), ("other_daren", "listed")])
def test_a_hand_made_order_out_in_part_for_the_waybill_days_is_a_line_and_the_rest_still_acts(hbot, clock, account,
                                                                                              then):
    a_round(hbot, clock, {H3: ours("WAITING_SEND", account=account)})
    a_round(hbot, clock, {H3: hpart(account)})
    assert lines(hbot) == []
    clock.now += 3 * DAY
    a_round(hbot, clock, {H3: hpart(account)})
    [line] = lines(hbot)
    assert hbot.prm.store.hand_sample(H3)["state"] == HAND_SPLIT
    assert line["waybill"] == "SF0000000002" and line["carrier"] == "顺丰速运_电商"
    assert line["why"].startswith("拆成了 2 个包裹，3 天了只发出 1 个（顺丰速运_电商 SF0000000002），另外的还没有运单号")
    assert "请到聚水潭寄样管理看一下" in line["why"] and hand_notices(hbot) == []
    a_round(hbot, clock, {H3: hpart(account)})                                   # the same: the line as it was
    assert [x["why"] for x in lines(hbot)] == [line["why"]]
    assert hbot.prm.hand_sample_seen(H3)["ok"] is True and lines(hbot) == []
    a_round(hbot, clock, {H3: hpart(account)})
    assert lines(hbot) == []                                                     # seen to: not back while still part
    a_round(hbot, clock, {H3: hwhole(account)})                                  # the rest went out after all
    assert hbot.prm.store.hand_sample(H3)["state"] == then
    if then == "filled":
        assert lines(hbot) == [] and len(hand_notices(hbot)) == 1 and deal(hbot)["stage"] == "shipped"
    else:
        [back] = lines(hbot)                                                     # its line comes back, said anew
        assert back["why"] == "没登记的达人" and back["waybill"] == "SF0000000002" and hand_notices(hbot) == []


def test_a_hand_made_order_signed_for_while_part_waits_counts_as_whole(hbot, clock):
    a_round(hbot, clock, {H2: ours("WAITING_SEND")})
    a_round(hbot, clock, {H2: hpart()})
    a_round(hbot, clock, {H2: hpart(status="SIGNED")})
    d = deal(hbot)
    assert hbot.prm.store.latest_shipment(d["id"])["waybill"] == "SF0000000002" and hand_notices(hbot) == []
    assert hbot.prm.store.hand_sample(H2)["state"] == "filled"


def test_a_hand_made_order_partly_out_before_the_first_read_is_left_like_any_shipped_one(hbot, clock):
    a_round(hbot, clock, {H2: hpart()})
    assert hbot.prm.store.hand_sample(H2)["state"] == "baseline"
    a_round(hbot, clock, {H2: hwhole()})
    assert hand_notices(hbot) == [] and lines(hbot) == []


def test_a_restart_keeps_when_a_hand_made_orders_first_package_was_seen(clock, tmp_path):
    first = FileBot(clock, tmp_path)
    first.prm.register(CREATOR, "小美", "测试达人")
    a_round(first, clock, {H3: ours("WAITING_SEND", account="other_daren")})
    a_round(first, clock, {H3: hpart("other_daren")})
    first.prm.stop()
    first.relay.stop()
    clock.now += 3 * DAY
    again = FileBot(clock, tmp_path)
    try:
        job = next_round(again, clock)
        assert job is not None
        from test_prm_hand_samples import answer
        answer(again, job, {H3: hpart("other_daren")})
        [line] = lines(again)
        assert "3 天了只发出 1 个" in line["why"]
    finally:
        again.prm.stop()
        again.relay.stop()


def test_a_split_line_is_shown_and_seen_to_like_any_other(tmp_path):
    store = PrmStore(tmp_path / "prm.db")
    now = 1793872800.0
    store.add_hand_sample(H3, platform="DY", account="other_daren", nickname="测试达人三", applied_at=now,
                          status="SENT", state="waiting", now=now)
    store.update_hand_sample(H3, now, state=HAND_SPLIT, why="拆成了 2 个包裹，3 天了只发出 1 个", carrier="中通",
                             waybill="ZT0000000003", sent_at=now)
    assert [r["order_no"] for r in store.hand_samples(shown=True)] == [H3]
    assert store.dismiss_hand_sample(H3, now + 1) is True
    assert store.hand_samples(shown=True) == [] and store.dismiss_hand_sample(H3, now + 2) is False
