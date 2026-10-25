"""达人商务模块的验收测试（handoff.md 第 4 节的 S1–S7 及补充用例）。

第 1 阶段先写测试，再修代码。修复之前这些用例必然失败，所以标成
xfail(strict=True)，并在 reason 里写明对应的问题编号。某个问题修好后，
对应用例会变成"意外通过"，strict 模式会把它报成失败，提醒修复的人
删掉这条用例上的 known_failure 标记。

外部系统一律用替身：FakeERP 代替聚水潭，FakeXHS 代替小红书 CLI。
它们只记录调用，不碰真实库存和佣金。
"""

import asyncio
import time

import pytest

from src.influencer.handler import InfluencerBusinessHandler
from src.influencer.jst_erp import JuShuiTanERPClient
from src.influencer.store import SOURCE_CONSOLE, InfluencerStore
from src.influencer.types import GateStatus
from src.influencer.xhs_cli import XiaohongshuCLIClient
from test_router import make_private_msg, make_router

INFLUENCER = "wxid_influencer_a"
SAMPLE_REQUEST = "你好，想申请一下VC精华液的样品"
FULL_ADDRESS = "张三 13812345678 上海市浦东新区世纪大道100号"
XHS_PROFILE = "https://www.xiaohongshu.com/user/profile/5f0000000000000000000001"

# 审批通过之前，回复里不能出现的承诺性字眼（D1）
COMMITMENT_WORDS = ("今天", "明天", "下午", "马上", "立刻", "顺丰", "已安排", "已经安排", "排单")


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
        self.link_calls = []

    async def get_creator_stats(self, xhs_uid):
        self.stats_calls.append(xhs_uid)
        return dict(self.stats)

    async def set_exclusive_commission(self, **kwargs):
        self.commission_calls.append(kwargs)
        return {"success": True, "plan_id": f"PLAN-{len(self.commission_calls)}"}

    async def create_product_link(self, **kwargs):
        self.link_calls.append(kwargs)
        return {"success": True, "link": f"https://xhslink.com/test/{kwargs['idempotency_key']}"}


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


def age_draft(handler, chat_id, seconds):
    draft = handler.store.get_draft(chat_id)
    draft.updated_at -= seconds
    handler.store.save_draft(draft)


# ---- 场景辅助 ----

def submit_full_request(handler, chat_id=INFLUENCER):
    """走完"索样 → 补收件信息和小红书主页"两步，返回生成的审批单号。"""
    run(handler.process_incoming_message(chat_id, SAMPLE_REQUEST, nickname="小美"))
    run(handler.process_incoming_message(chat_id, f"{FULL_ADDRESS} 小红书主页 {XHS_PROFILE}", nickname="小美"))
    [packet] = pending_packets(handler)
    return packet.packet_id


def assert_no_commitment(reply):
    found = [w for w in COMMITMENT_WORDS if w in reply]
    assert not found, f"审批前的回复不能有承诺性字眼 {found}：{reply}"


def private_msg(message_id, chat_id, nickname, content):
    """模拟 Mac 后端（mac_hybrid）发来的私聊：sender_id 是昵称，chat_id 才是稳定的 ID。"""
    msg = make_private_msg(message_id, content)
    msg.update(chat_id=chat_id, sender_id=nickname, sender_name=nickname,
               group_name=nickname, chat_name=nickname)
    return msg


def router_with(handler):
    router, summarizer = make_router()
    router._influencer_handler = handler
    return router, summarizer


# ---- S1–S7 ----

def test_s1_non_influencer_private_chat_gets_the_normal_reply():
    router, summarizer = router_with(make_handler())
    reply = router.handle(private_msg("m1", "wxid_friend", "老同学", "上次合作的项目挺顺利，周末聚一下？"))
    assert summarizer.chat_calls, "白名单以外的私聊应该走原有的聊天逻辑"
    assert "收件" not in (reply or "")


def test_s2_asking_for_the_address_makes_no_promise():
    reply = run(make_handler().process_incoming_message(INFLUENCER, SAMPLE_REQUEST, nickname="小美"))
    assert reply and ("收件" in reply or "地址" in reply)
    assert_no_commitment(reply)


def test_s3_full_address_is_submitted_for_review_without_promise():
    handler = make_handler()
    run(handler.process_incoming_message(INFLUENCER, SAMPLE_REQUEST, nickname="小美"))
    reply = run(handler.process_incoming_message(INFLUENCER, FULL_ADDRESS, nickname="小美"))
    assert "审核" in reply
    assert_no_commitment(reply)


def test_s3_packet_carries_the_recipient_from_the_message():
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
    assert len(xhs.commission_calls) == 1


def test_s5_a_rejected_packet_cannot_be_approved_later():
    erp, xhs = FakeERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet_id = submit_full_request(handler)
    run(handler.execute_operator_approval(packet_id, False))
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False
    assert erp.order_calls == [] and xhs.commission_calls == []


def test_s6_two_people_with_the_same_nickname_stay_apart():
    handler = make_handler(influencers=("wxid_a", "wxid_b"))
    router, _ = router_with(handler)
    router.handle(private_msg("m1", "wxid_a", "Lily", "想申请泥膜样品"))
    router.handle(private_msg("m2", "wxid_b", "Lily", "王五 13700002222 北京市朝阳区建国路8号"))
    assert pending_packets(handler) == [], "B 发来的地址被挂到了 A 的索样申请上"


def test_s7_an_unnamed_product_is_asked_about_not_placeheld():
    erp = FakeERP()
    handler = make_handler(erp)
    reply = run(handler.process_incoming_message(INFLUENCER, "老师您好，想跟您谈谈带货合作", nickname="阿杰"))
    assert reply, "白名单里的达人应该得到回复"
    assert "主推爆款商品" not in reply
    assert erp.inventory_calls == []
    run(handler.process_incoming_message(INFLUENCER, FULL_ADDRESS, nickname="阿杰"))
    assert pending_packets(handler) == []


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
    assert len(xhs.commission_calls) == 1


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


def test_default_commission_is_25_percent():
    handler = make_handler()
    packet = get_packet(handler, submit_full_request(handler))
    assert packet.proposed_commission_rate == 0.25


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


def test_known_unqualified_creator_is_flagged_not_auto_rejected_and_cannot_be_shipped():
    erp, xhs = FakeERP(), FakeXHS(stats={"follower_count": 500, "recent_highest_likes": 80})
    handler = make_handler(erp, xhs)
    run(handler.process_incoming_message(INFLUENCER, SAMPLE_REQUEST, nickname="小美"))
    reply = run(handler.process_incoming_message(
        INFLUENCER, f"{FULL_ADDRESS} 小红书主页 {XHS_PROFILE}", nickname="小美",
    ))
    assert "名额" not in reply, "D4：不自动拒绝"
    [packet] = pending_packets(handler)
    assert packet.qualification.startswith("不达标")
    result = run(handler.execute_operator_approval(packet.packet_id, True))
    assert result["success"] is False, "R3：数据明确不达标的达人，批准了也不寄样"
    assert erp.order_calls == [] and xhs.commission_calls == []


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
    result = outcome(JuShuiTanERPClient().check_inventory("VC精华液"))
    assert isinstance(result, Exception) or not result.get("in_stock")


def test_erp_real_mode_does_not_report_a_fake_order(no_mock_env):
    # 第 5 阶段接入真实接口后，这条要改成拦截 HTTP 请求的写法
    client = JuShuiTanERPClient(app_key="k", app_secret="s", access_token="t")
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
    assert run(JuShuiTanERPClient().check_inventory("VC精华液"))["in_stock"]
    client = XiaohongshuCLIClient(cli_path="/nonexistent/xhs-cli")
    assert run(client.set_exclusive_commission(xhs_uid="xhs_123", sku_id="SKU-1", rate=0.25))["success"]
