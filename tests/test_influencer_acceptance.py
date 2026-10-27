"""达人商务模块的验收测试（handoff.md 第 4 节的 S1–S7 及补充用例）。

第 1 阶段先写测试，再修代码。修复之前这些用例必然失败，所以标成
xfail(strict=True)，并在 reason 里写明对应的问题编号。某个问题修好后，
对应用例会变成"意外通过"，strict 模式会把它报成失败，提醒修复的人
删掉这条用例上的 known_failure 标记。

外部系统一律用替身：FakeERP 代替聚水潭，FakeXHS 代替小红书 CLI。
它们只记录调用，不碰真实库存和佣金。

2026-10-26 第 8 步删了旧的规则对话（达人的私聊改由 PRM 起草、运营审核），只测对话回复的
S2、S3 的回复、S6、S7 一起删了；审批单改成按现在的路径生成（check_product → submit_for_approval，
prm_bridge 做的就是这两步），只执行一次、过期、上限这些保证照旧在这里测。
"""

import asyncio
import time

import pytest

from src.influencer.handler import InfluencerBusinessHandler
from src.influencer.jst_erp import JuShuiTanERPClient
from src.influencer.store import SOURCE_CONSOLE, InfluencerStore
from src.influencer.types import GateStatus, SampleRequestDraft
from src.influencer.xhs_cli import XiaohongshuCLIClient

INFLUENCER = "wxid_influencer_a"
PRODUCT = "英伦复古贝雷帽 黑色 M"
XHS_PROFILE = "https://www.xiaohongshu.com/user/profile/5f0000000000000000000001"


def known_failure(issue):
    return pytest.mark.xfail(strict=True, reason=f"{issue}：修复后删除此标记")


class FakeERP:
    """聚水潭替身：记录调用，可以模拟建单失败。"""

    def __init__(self, *, order_ok=True):
        self.order_ok = order_ok
        self.inventory_calls = []
        self.order_calls = []

    async def check_inventory(self, product_name):
        self.inventory_calls.append(product_name)
        return {
            "sku_id": f"SKU-{product_name}",
            "product_name": product_name,
            "in_stock": True,
            "available_quantity": 100,
            "cost_price": 18.5,
        }

    async def create_sample_order(self, **kwargs):
        self.order_calls.append(kwargs)
        if not self.order_ok:
            return {"success": False, "error": "聚水潭返回错误"}
        return {"success": True, "order_sn": f"JST-{len(self.order_calls)}", "tracking_number": "SF0001"}


class FakeXHS:
    """小红书 CLI 替身：记录查询和写操作。"""

    def __init__(self, stats=None):
        self.stats = {"follower_count": 35000, "recent_highest_likes": 3200} if stats is None else stats
        self.stats_calls = []
        self.commission_calls = []

    async def get_creator_stats(self, xhs_uid):
        self.stats_calls.append(xhs_uid)
        return dict(self.stats)

    def commission_problem(self, product_name, sku_id, xhs_account):
        return ""

    async def set_exclusive_commission(self, **kwargs):
        self.commission_calls.append(kwargs)
        return {"success": True, "plan_id": f"PLAN-{len(self.commission_calls)}"}


def run(coro):
    return asyncio.run(coro)


def outcome(coro):
    """执行协程；抛出的异常也算一种结果，因为"报错"是允许的处理方式。"""
    try:
        return run(coro)
    except Exception as exc:
        return exc


# ---- 以下辅助函数集中了对处理器内部结构的依赖，改结构时只改这里 ----
# 审批单和草稿存在数据库里（第 6 项）。取到的审批单是当时的快照，状态变了要重新取

def make_handler(erp=None, xhs=None, *, influencers=(INFLUENCER,)):
    store = InfluencerStore(":memory:")
    store.add_contacts(influencers, source=SOURCE_CONSOLE)
    return InfluencerBusinessHandler(store, erp_client=erp or FakeERP(), xhs_client=xhs or FakeXHS())


def pending_packets(handler):
    return handler.store.list_approvals([GateStatus.PENDING])


def get_packet(handler, packet_id):
    return handler.store.get_approval(packet_id)


def set_commission(handler, packet_id, rate):
    handler.store.update_pending(packet_id, proposed_commission_rate=rate)


def expire(handler, packet_id):
    handler.store.update_pending(packet_id, expires_at=time.time() - 1)


# ---- 场景辅助 ----

def submit_full_request(handler, chat_id=INFLUENCER, *, xhs_account=XHS_PROFILE):
    """合作单上的信息齐了以后生成审批单（prm_bridge 做的两步：先到聚水潭查商品，再提交），返回审批单号。"""
    draft = SampleRequestDraft(
        chat_id=chat_id, product=PRODUCT, recipient_name="张三", recipient_phone="13812345678",
        shipping_address="上海市浦东新区世纪大道100号", xhs_account=xhs_account,
    )
    assert run(handler.check_product(draft)) == ""
    return run(handler.submit_for_approval(chat_id, "小美", draft))


# ---- S1–S7 ----

# S1（名单以外的私聊走原来的聊天）不再适用：公司仓库改成业务网关后，名单以外的私聊一概不回，
# 由同事的 tests/test_router_business.py 守着。


def test_s3_packet_carries_the_recipient_as_confirmed():
    handler = make_handler()
    packet = get_packet(handler, submit_full_request(handler))
    assert packet.recipient_name == "张三"
    assert packet.recipient_phone == "13812345678"
    assert "上海市浦东新区世纪大道100号" in packet.shipping_address


def test_s4_a_packet_is_executed_only_once():
    erp, xhs = FakeERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)
    first = run(handler.execute_operator_approval(packet_id, True))
    second = run(handler.execute_operator_approval(packet_id, True))
    assert first["success"] is True
    assert second["success"] is False
    assert len(erp.order_calls) == 1
    assert xhs.commission_calls == [], "佣金由 PRM 的平台动作开（D34），审批单不开"


def test_s5_a_rejected_packet_cannot_be_approved_later():
    erp, xhs = FakeERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)
    run(handler.execute_operator_approval(packet_id, False))
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False
    assert erp.order_calls == [] and xhs.commission_calls == []


# ---- 补充用例 ----

def test_an_expired_packet_cannot_be_approved():
    erp, xhs = FakeERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)
    expire(handler, packet_id)
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False
    assert erp.order_calls == [] and xhs.commission_calls == []


class SlowERP(FakeERP):
    """建单时让出控制权，模拟网络等待，用来制造"两个请求同时在执行"的情况。"""

    async def create_sample_order(self, **kwargs):
        await asyncio.sleep(0.01)
        return await super().create_sample_order(**kwargs)


def test_two_simultaneous_approvals_execute_only_once():
    erp, xhs = SlowERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)

    async def double_click():
        return await asyncio.gather(
            handler.execute_operator_approval(packet_id, True),
            handler.execute_operator_approval(packet_id, True),
        )

    results = run(double_click())
    assert sorted(r["success"] for r in results) == [False, True]
    assert len(erp.order_calls) == 1
    assert xhs.commission_calls == []


def test_a_failed_erp_order_stops_before_the_commission():
    erp, xhs = FakeERP(order_ok=False), FakeXHS()
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False
    assert xhs.commission_calls == []
    assert get_packet(handler, packet_id).status == GateStatus.FAILED


def test_commission_above_the_25_percent_cap_is_refused():
    erp, xhs = FakeERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)
    set_commission(handler, packet_id, 0.30)
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False
    assert erp.order_calls == [] and xhs.commission_calls == []


def test_default_commission_starts_at_20_percent():
    # D37（2026-10-26）：起步 20%，带货表现好再涨，最高 25%
    handler = make_handler()
    packet = get_packet(handler, submit_full_request(handler))
    assert packet.proposed_commission_rate == 0.20


def test_missing_creator_stats_are_not_made_up():
    handler = make_handler(xhs=FakeXHS(stats={}))
    packet = get_packet(handler, submit_full_request(handler))
    assert packet.follower_count is None, "拿不到粉丝数就是未知，不能编成 2 万"


class NoCostERP(FakeERP):
    async def check_inventory(self, product_name):
        result = await super().check_inventory(product_name)
        del result["cost_price"]
        return result


def test_a_missing_sample_cost_is_not_made_up():
    handler = make_handler(NoCostERP())
    packet = get_packet(handler, submit_full_request(handler))
    assert packet.sample_cost is None, "聚水潭没给成本就是未知，不能编成 18.5"


def test_unknown_stats_are_marked_for_review_and_can_still_be_approved():
    erp, xhs = FakeERP(), FakeXHS(stats={})
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)
    assert get_packet(handler, packet_id).qualification.startswith("待核实")
    assert run(handler.execute_operator_approval(packet_id, True))["success"] is True


def test_a_low_follower_count_is_only_a_reference_and_does_not_block():
    # D30（2026-10-26）：寄几款按销售额分档，不看粉丝数；粉丝数只写在单上给运营参考
    erp, xhs = FakeERP(), FakeXHS(stats={"follower_count": 500, "recent_highest_likes": 80})
    handler = make_handler(erp, xhs)
    packet = get_packet(handler, submit_full_request(handler))
    assert "粉丝 500" in packet.qualification and "参考" in packet.qualification
    result = run(handler.execute_operator_approval(packet.packet_id, True))
    assert result["success"] is True
    assert len(erp.order_calls) == 1 and xhs.commission_calls == []


def test_the_wechat_identity_is_never_used_as_the_xhs_account():
    xhs = FakeXHS()
    handler = make_handler(xhs=xhs)
    packet_id = submit_full_request(handler)
    run(handler.execute_operator_approval(packet_id, True))
    used = xhs.stats_calls + [c["xhs_uid"] for c in xhs.commission_calls]
    assert INFLUENCER not in used and "小美" not in used


@pytest.fixture
def no_mock_env(monkeypatch):
    for name in ("INFLUENCER_MOCK", "JST_APP_KEY", "JST_APP_SECRET", "JST_ACCESS_TOKEN", "XHS_CLI_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_erp_without_keys_or_mock_flag_returns_no_fake_stock(no_mock_env):
    result = outcome(JuShuiTanERPClient().check_inventory(PRODUCT))
    assert isinstance(result, Exception) or not result.get("in_stock")


def test_erp_real_mode_does_not_report_a_fake_order(no_mock_env):
    # 建寄样单接上聚水潭命令行后，这条要改成用假命令行的写法
    client = JuShuiTanERPClient()
    result = outcome(client.create_sample_order(
        influencer_id=INFLUENCER, sku_id="SKU-1", recipient_name="张三",
        recipient_phone="13812345678", shipping_address="上海市浦东新区世纪大道100号",
    ))
    assert isinstance(result, Exception) or not result.get("success")


def test_xhs_without_cli_or_mock_flag_fakes_nothing(no_mock_env):
    client = XiaohongshuCLIClient(cli_path="/nonexistent/xhs-cli")
    stats = outcome(client.get_creator_stats("xhs_123"))
    assert isinstance(stats, Exception) or not stats.get("follower_count")
    result = outcome(client.set_exclusive_commission(xhs_uid="xhs_123", sku_id="SKU-1", rate=0.25))
    assert isinstance(result, Exception) or not result.get("success")


def test_explicit_mock_flag_keeps_working(no_mock_env, monkeypatch):
    monkeypatch.setenv("INFLUENCER_MOCK", "1")
    assert run(JuShuiTanERPClient().check_inventory(PRODUCT))["in_stock"]
    client = XiaohongshuCLIClient(cli_path="/nonexistent/xhs-cli")
    assert run(client.set_exclusive_commission(xhs_uid="xhs_123", sku_id="SKU-1", rate=0.25))["success"]
