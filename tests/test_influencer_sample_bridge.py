"""寄样接上 PRM（src/influencer/prm_bridge.SampleBridge，计划第 4 步）。

合作单到了「待寄出」（Claude 建议的，或者运营改的）→ 用合作单上确认过的信息生成我们的审批单（号码换回真的）；
缺东西、没库存就在合作单上提示；运营在「达人审批」页批准 / 驳回 → 给达人的通知作为系统消息进 PRM 的待审批，
一定要人审（D22），运单号从聚水潭出库单记进合作单；达人要求提高佣金 → 我们的固定话术作为系统消息等审核。
"""

import json

import pytest

from test_influencer_acceptance import FakeERP, FakeXHS
from test_prm import Bot as PrmBot, CREATOR, Clock, draft

from src.influencer import console
from src.influencer.handler import COMMISSION_REPLY, InfluencerBusinessHandler
from src.influencer.prm_bridge import SampleBridge, match_listings
from src.influencer.store import INBOX_COMMISSION, INBOX_OUT_OF_STOCK, SOURCE_CONSOLE, InfluencerStore
from src.influencer.types import GateStatus

READY = {"商品": "保湿面霜", "收件人": "小美", "手机": "〈手机1〉", "地址": "上海市浦东新区测试路1号2栋301"}
SAYS = "我是小美，手机 13812345678，地址上海市浦东新区测试路1号2栋301，就要保湿面霜"


class OutOfStock(FakeERP):
    """ERP 里有，但没有现货（D35：照样生成审批单）。"""

    async def check_inventory(self, product_name):
        return {**await super().check_inventory(product_name), "found": True, "in_stock": False, "available_quantity": 0}


class NotInERP(FakeERP):
    async def check_inventory(self, product_name):
        return {**await super().check_inventory(product_name), "found": False, "in_stock": False, "available_quantity": 0}


@pytest.fixture
def side(tmp_path):
    b = PrmBot(Clock(), tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def make_bridge(side, tmp_path, erp=None, workspace=None):
    store = InfluencerStore(str(tmp_path / "messages.db"))
    store.save_contact(CREATOR, source=SOURCE_CONSOLE, display_name="小美", xhs_account="https://www.xiaohongshu.com/user/profile/5f1e2d3c4b5a697887766554")
    handler = InfluencerBusinessHandler(store, erp_client=erp or FakeERP(), xhs_client=FakeXHS())
    bridge = SampleBridge(handler, side.prm, workspace=workspace)
    bridge.attach()
    return handler, bridge


@pytest.fixture
def bridged(side, tmp_path):
    handler, bridge = make_bridge(side, tmp_path)
    yield handler
    bridge.detach()


def drafts(side, text=SAYS, **overrides):
    side.prm.handle(side.msg(text))
    job = side.worker_takes()
    side.finish(job["id"], draft(**overrides))
    return side.prm.store.packet_for_job(job["id"])


def approvals(handler, *statuses):
    return [p for p in handler.store.list_approvals(list(statuses) or None) if p.influencer_id == CREATOR]


# ---- 生成审批单 ----

def test_a_deal_ready_to_ship_becomes_an_approval_with_the_real_number(side, bridged):
    drafts(side, slots=READY, stage_proposal="to_ship")
    [packet] = approvals(bridged, GateStatus.PENDING)
    assert (packet.requested_product, packet.recipient_name, packet.recipient_phone) == ("保湿面霜", "小美", "13812345678")
    assert packet.shipping_address == "上海市浦东新区测试路1号2栋301"
    assert packet.xhs_account.endswith("5f1e2d3c4b5a697887766554"), "小红书账号从名单里拿"
    assert any(e["kind"] == "sample_request" for e in side.prm.store.events(1))


def test_nothing_is_made_before_the_deal_is_ready_or_when_something_is_missing(side, bridged):
    drafts(side, slots=READY, stage_proposal="address")
    assert approvals(bridged) == [], "只是在收集收件信息，不生成"
    # 缺项（合作单会合并之前几轮的信息，所以用一位新达人）
    side.prm.register("wxid_creator02", "阿青")
    side.prm.handle(side.msg("想要面霜", chat="wxid_creator02"))
    job = side.worker_takes()
    side.finish(job["id"], draft(slots={"商品": "保湿面霜", "收件人": "阿青"}, stage_proposal="to_ship"))
    deal = side.prm.store.deal_of(side.prm.store.party_by_wxid("wxid_creator02")["id"])
    assert "手机" in deal["attention"] and "地址" in deal["attention"]


def test_one_waiting_approval_per_creator(side, bridged):
    drafts(side, slots=READY, stage_proposal="to_ship")
    drafts(side, "确认没问题", slots=READY, stage_proposal="to_ship")
    assert len(approvals(bridged)) == 1


def test_the_operator_moving_the_deal_to_ready_makes_the_approval(side, bridged):
    drafts(side, slots=READY, stage_proposal="")
    assert approvals(bridged) == []
    assert side.prm.deal_action("stage", {"deal_id": 1, "stage": "to_ship"})["ok"]
    assert len(approvals(bridged, GateStatus.PENDING)) == 1


def test_no_stock_on_hand_still_makes_an_approval(side, tmp_path):
    handler, bridge = make_bridge(side, tmp_path, erp=OutOfStock())
    try:
        drafts(side, slots=READY, stage_proposal="to_ship")
    finally:
        bridge.detach()
    assert len(approvals(handler)) == 1, "D35：ERP 里有就能寄，不看现货"


def test_a_product_missing_from_the_erp_is_told_on_the_deal_and_in_the_inbox(side, tmp_path):
    handler, bridge = make_bridge(side, tmp_path, erp=NotInERP())
    try:
        drafts(side, slots=READY, stage_proposal="to_ship")
    finally:
        bridge.detach()
    assert approvals(handler) == []
    assert "查不到" in side.prm.store.deal(1)["attention"]
    assert [i["kind"] for i in handler.store.list_inbox()] == [INBOX_OUT_OF_STOCK]


# ---- 审批结果回到 PRM ----

@pytest.fixture
def console_fakes(monkeypatch):
    erp = FakeERP()
    monkeypatch.setattr(console, "InfluencerBusinessHandler",
                        lambda store: InfluencerBusinessHandler(store, erp_client=erp, xhs_client=FakeXHS()))
    return erp


def test_an_approval_puts_the_notice_up_for_review_and_the_waybill_on_the_deal(side, bridged, console_fakes):
    drafts(side, slots=READY, stage_proposal="to_ship")
    [packet] = approvals(bridged, GateStatus.PENDING)
    result = console.act_on_approval(bridged.store, {"packet_id": packet.packet_id, "action": "approve"})
    assert result["ok"] and result["notice_queued"]
    deal = side.prm.store.deal(1)
    assert deal["stage"] == "shipped" and side.prm.store.latest_shipment(1)["waybill"] == "SF0001"
    [notice] = [p for p in side.prm.store.packets(("pending",)) if p["job_id"] is None]
    assert notice["kind"] == "other" and notice["route"] == "review", "系统消息一定人审"
    assert "SF0001" in notice["text"] and "专属佣金" not in notice["text"], "佣金另由平台动作开（D34）"
    # 审核通过才发
    assert side.texts == []
    assert side.prm.decide(notice["id"], "send")["ok"]
    side.prm.send_approved()
    assert side.texts and "SF0001" in side.texts[-1][1]


def test_a_rejection_puts_the_polite_no_up_for_review(side, bridged, console_fakes):
    drafts(side, slots=READY, stage_proposal="to_ship")
    [packet] = approvals(bridged, GateStatus.PENDING)
    console.act_on_approval(bridged.store, {"packet_id": packet.packet_id, "action": "reject", "reason": "粉丝太少"})
    [notice] = [p for p in side.prm.store.packets(("pending",)) if p["job_id"] is None]
    assert notice["kind"] == "other" and "暂时没能通过" in notice["text"] and "粉丝太少" not in notice["text"]
    assert side.prm.store.latest_shipment(1) is None


def test_system_notices_are_not_dropped_when_the_creator_writes_again(side, bridged, console_fakes):
    drafts(side, slots=READY, stage_proposal="to_ship")
    [packet] = approvals(bridged, GateStatus.PENDING)
    console.act_on_approval(bridged.store, {"packet_id": packet.packet_id, "action": "approve"})
    drafts(side, "请问什么时候发货呀")
    assert [p["kind"] for p in side.prm.store.packets(("pending",)) if p["job_id"] is None] == ["other"]


# ---- 谈佣金的固定话术 ----

def test_asking_for_more_commission_puts_our_script_up_for_review_once(side, bridged):
    drafts(side, "佣金能不能再高一点，30%可以吗", message_kind="money", needs_attention=True)
    drafts(side, "真的不能再高点吗佣金", message_kind="money", needs_attention=True)
    scripts = [p for p in side.prm.store.packets(("pending",)) if p["job_id"] is None]
    assert [p["text"] for p in scripts] == [COMMISSION_REPLY], "一段时间内只放一次"
    assert scripts[0]["kind"] == "money" and scripts[0]["route"] == "review"
    assert [i["kind"] for i in bridged.store.list_inbox()] == [INBOX_COMMISSION, INBOX_COMMISSION]


def test_asking_for_more_commission_tells_the_operator_the_last_30_days(side, bridged, monkeypatch):
    # D37：系统不改佣金，只把近 30 天在我们店的单量和成交额写给运营看，由运营决定
    from src.influencer import jst_erp

    monkeypatch.setattr(jst_erp, "run_jst_cli", lambda *args: {"rows": [
        {"platformNo": "xhs_1", "coop30d": {"orders": 46, "amount": 7728.0, "netAmount": 0.0, "commission": 0}},
    ]})
    bridged.store.save_contact(CREATOR, source=SOURCE_CONSOLE, xhs_account="xhs_1")
    drafts(side, "佣金能不能再高一点", message_kind="money", needs_attention=True)
    [script] = [p for p in side.prm.store.packets(("pending",)) if p["job_id"] is None]
    assert "46 单" in script["reason"] and "满足涨佣条件" in script["reason"]
    [item] = bridged.store.list_inbox()
    assert "46 单" in item["content"]


def test_without_a_platform_number_the_operator_is_told_to_look_it_up(side, bridged):
    bridged.store.save_contact(CREATOR, source=SOURCE_CONSOLE, xhs_account="")
    assert not (bridged.store.get_contact(CREATOR) or {}).get("xhs_account")
    drafts(side, "佣金能不能再高一点", message_kind="money", needs_attention=True)
    [script] = [p for p in side.prm.store.packets(("pending",)) if p["job_id"] is None]
    assert "查不了合作数据" in script["reason"]


def test_the_approval_carries_platform_tier_items_and_live_session(side, bridged, monkeypatch):
    # 第 2 步：审批单上的平台、档位、主播类型来自名单（没查过就先查聚水潭），商品拆成清单，专场照写
    from src.influencer import tiers

    monkeypatch.setattr(tiers, "run_jst_cli", lambda *args: {"found": True, "daren": {
        "sales30d": {"available": True, "min": 250000, "max": 500000, "label": "25万~50万"},
        "channelBySales": "直播", "level": 3,
    }})
    bridged.store.save_contact(CREATOR, source=SOURCE_CONSOLE, platform="DY", platform_no="dy_mei")
    slots = {**READY, "商品": "法式复古贝雷帽 深棕色、丹麦小众贝雷帽 黑色、北欧套头帽", "直播专场": "11 月 3 日"}
    drafts(side, slots=slots, stage_proposal="to_ship")
    [packet] = approvals(bridged)
    assert (packet.platform, packet.platform_no, packet.tier, packet.channel) == ("DY", "dy_mei", "问专场", "直播")
    assert "25万~50万" in packet.sales_label
    assert [i["name"] for i in json.loads(packet.items_json)] == ["法式复古贝雷帽 深棕色", "丹麦小众贝雷帽 黑色", "北欧套头帽"]
    assert packet.live_session == "11 月 3 日"


XHS_BERET = "6ad705003ae8b10015cc5be7"
CATALOG = {"version": 1, "platforms": {
    "xhs": {"ok": True, "items": [{"id": XHS_BERET, "title": "ACE7CN 英伦复古经典手缝线贝雷帽", "price": "220.00", "skus": [
        {"id": "6ad705003ae8b10015cc5be8", "name": "黑色 M", "price": "220.00", "code": "A7261010037A-M"},
        {"id": "6ad705003ae8b10015cc5be9", "name": "黑色 L", "price": "220.00", "code": "A7261010037A-L"}]}]},
    "dy": {"ok": True, "items": [{"id": "3712345678901234567", "title": "法式贝雷帽", "price": "199.00", "skus": []}]},
}}


@pytest.mark.parametrize("name, expected", [
    ("手缝线贝雷帽 黑色 M（A7261010037A-M）", XHS_BERET),           # SKU 的商品编码
    ("手缝线贝雷帽 A7261010037A 黑色", XHS_BERET),                  # 去掉尺码后缀的款
    (f"千帆商品 {XHS_BERET}", XHS_BERET),                            # 平台商品 ID
])
def test_an_item_is_matched_to_the_platforms_listing_by_code_or_id(name, expected):
    items = [{"name": name, "qty": 1}]
    assert match_listings(items, "XHS", CATALOG) is True
    assert items[0]["listing"]["id"] == expected and "贝雷帽" in items[0]["listing"]["title"]


def test_a_code_not_in_this_platforms_catalogue_is_marked_and_no_code_is_left_alone():
    items = [{"name": "丹麦贝雷帽 A7260610003B-F", "qty": 1}, {"name": "北欧套头帽 黑色", "qty": 1}]
    assert match_listings(items, "XHS", CATALOG) is True
    assert items[0].get("unlisted") is True and "listing" not in items[0]
    assert items[1] == {"name": "北欧套头帽 黑色", "qty": 1}
    # 小红书的编码在抖音的目录里找不到；没有目录、平台不认识都不标
    dy = [{"name": "A7261010037A-M", "qty": 1}]
    assert match_listings(dy, "DY", CATALOG) is True and dy[0].get("unlisted") is True
    for catalog, platform in ((None, "XHS"), ({"platforms": {}}, "XHS"), (CATALOG, "")):
        untouched = [{"name": "A7261010037A-M", "qty": 1}]
        assert match_listings(untouched, platform, catalog) is False and untouched == [{"name": "A7261010037A-M", "qty": 1}]


def test_the_approval_shows_the_platform_listing_of_each_item(side, tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "kb").mkdir(parents=True)
    (workspace / "kb" / "products.json").write_text(json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8")
    handler, bridge = make_bridge(side, tmp_path, workspace=workspace)
    try:
        handler.store.save_contact(CREATOR, source=SOURCE_CONSOLE, platform="XHS", platform_no="mei")
        drafts(side, slots={**READY, "商品": "手缝线贝雷帽 黑色 M（A7261010037A-M）"}, stage_proposal="to_ship")
        [packet] = approvals(handler)
        [item] = json.loads(packet.items_json)          # 只有一款，对上了也列出来
        assert item["listing"]["id"] == XHS_BERET
    finally:
        bridge.detach()


def test_detach_unhooks_everything(side, tmp_path):
    handler, bridge = make_bridge(side, tmp_path)
    bridge.detach()
    assert console._decision_hooks == [] and side.prm.draft_hooks == [] and side.prm.stage_hooks == []
