"""运营审批（src/influencer/handler.py）：审批时可以填写的内容、批准前的复查、
给达人的消息、操作记录，以及审批单存在数据库里带来的保证。"""

from src.influencer.handler import InfluencerBusinessHandler
from src.influencer.store import SOURCE_CONSOLE, InfluencerStore
from src.influencer.types import GateStatus
from test_influencer_acceptance import (
    FULL_ADDRESS,
    INFLUENCER,
    SAMPLE_REQUEST,
    FakeERP,
    FakeXHS,
    get_packet,
    make_handler,
    pending_packets,
    run,
    submit_full_request,
)

LATE_PROFILE = "https://www.xiaohongshu.com/user/profile/5f00000000000000000000aa"


def submit_without_xhs_account(handler):
    run(handler.process_incoming_message(INFLUENCER, SAMPLE_REQUEST, nickname="小美"))
    reply = run(handler.process_incoming_message(INFLUENCER, FULL_ADDRESS, nickname="小美"))
    [packet] = pending_packets(handler)
    return packet, reply


def test_without_an_xhs_account_the_reply_asks_for_the_profile_link():
    _, reply = submit_without_xhs_account(make_handler())
    assert "小红书主页链接" in reply


def test_approval_needs_an_xhs_account_which_the_operator_can_fill_in():
    erp, xhs = FakeERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet, _ = submit_without_xhs_account(handler)
    refused = run(handler.execute_operator_approval(packet.packet_id, True))
    assert refused["success"] is False and erp.order_calls == []
    approved = run(handler.execute_operator_approval(packet.packet_id, True, xhs_account="xhs_creator_001"))
    assert approved["success"] is True
    assert xhs.commission_calls[0]["xhs_uid"] == "xhs_creator_001"


def test_a_profile_link_sent_after_submission_is_attached_to_the_request():
    handler = make_handler()
    packet, _ = submit_without_xhs_account(handler)
    reply = run(handler.process_incoming_message(INFLUENCER, f"我的小红书主页 {LATE_PROFILE}", nickname="小美"))
    assert "补充到申请" in reply
    packet = get_packet(handler, packet.packet_id)
    assert packet.xhs_account == LATE_PROFILE
    assert packet.qualification == "达标"  # FakeXHS 默认返回 3.5 万粉


def test_the_operator_can_lower_the_commission_but_not_raise_it():
    erp, xhs = FakeERP(), FakeXHS()
    handler = make_handler(erp, xhs)
    packet, _ = submit_without_xhs_account(handler)
    too_high = run(handler.execute_operator_approval(
        packet.packet_id, True, xhs_account="xhs_creator_001", commission_rate=0.30,
    ))
    assert too_high["success"] is False and erp.order_calls == []
    lowered = run(handler.execute_operator_approval(packet.packet_id, True, commission_rate=0.20))
    assert lowered["success"] is True
    assert xhs.commission_calls[0]["rate"] == 0.20


# ---- 审批单存在数据库里 ----

def test_the_console_can_approve_what_the_bot_submitted_before_a_restart(tmp_path):
    db_path = str(tmp_path / "messages.db")
    bot_store = InfluencerStore(db_path)
    bot_store.add_contacts([INFLUENCER], source=SOURCE_CONSOLE)
    bot_erp, console_erp = FakeERP(), FakeERP()
    bot = InfluencerBusinessHandler(bot_store, erp_client=bot_erp, xhs_client=FakeXHS())
    packet_id = submit_full_request(bot)

    console = InfluencerBusinessHandler(InfluencerStore(db_path), erp_client=console_erp, xhs_client=FakeXHS())
    assert run(console.execute_operator_approval(packet_id, True))["success"] is True
    assert run(bot.execute_operator_approval(packet_id, True))["success"] is False
    assert len(console_erp.order_calls) == 1 and bot_erp.order_calls == []


class SoldOutLaterERP(FakeERP):
    """提交审批时有货，批准前卖完了。"""

    sold_out = False

    async def check_inventory(self, product_name):
        result = await super().check_inventory(product_name)
        if self.sold_out:
            result.update(in_stock=False, available_quantity=0)
        return result


def test_stock_is_checked_again_at_approval():
    erp = SoldOutLaterERP()
    handler = make_handler(erp)
    packet_id = submit_full_request(handler)
    erp.sold_out = True
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False and "没有库存" in result["error"]
    assert erp.order_calls == []
    assert get_packet(handler, packet_id).status == GateStatus.PENDING, "补货后还能再批"


class FailingCommissionXHS(FakeXHS):
    async def set_exclusive_commission(self, **kwargs):
        self.commission_calls.append(kwargs)
        return {"success": False, "error": "CLI 超时"}


def test_an_order_made_before_a_failed_commission_is_kept_for_manual_follow_up():
    handler = make_handler(xhs=FailingCommissionXHS())
    packet_id = submit_full_request(handler)
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False and "需要人工处理" in result["error"]
    packet = get_packet(handler, packet_id)
    assert (packet.status, packet.order_sn) == (GateStatus.FAILED, "JST-1")


# ---- 给达人的消息（Q2：由运营复制后发送）和操作记录 ----

def test_after_approval_the_operator_gets_the_message_for_the_influencer():
    handler = make_handler()
    result = run(handler.execute_operator_approval(submit_full_request(handler), True))
    assert "VC精华液" in result["notice"] and "SF0001" in result["notice"]


def test_a_rejection_gives_a_polite_message_and_keeps_the_reason_on_record():
    handler = make_handler()
    packet_id = submit_full_request(handler)
    result = run(handler.execute_operator_approval(packet_id, False, reason="粉丝画像不匹配"))
    assert result["success"] is True
    assert "没能通过" in result["notice"]
    assert "粉丝画像不匹配" not in result["notice"], "驳回原因不告诉达人"
    events = handler.store.events_for([packet_id])[packet_id]
    assert ("驳回", "粉丝画像不匹配") in [(e["action"], e["detail"]) for e in events]


def test_the_records_hold_no_recipient_details():
    handler = make_handler(FakeERP(order_ok=False))
    packet_id = submit_full_request(handler)
    run(handler.execute_operator_approval(packet_id, True))
    details = " ".join(e["detail"] for e in handler.store.events_for([packet_id])[packet_id])
    assert "执行失败" in [e["action"] for e in handler.store.events_for([packet_id])[packet_id]]
    for secret in ("张三", "13812345678", "世纪大道"):
        assert secret not in details


# ---- 资质只看粉丝（D16） ----

def test_qualification_looks_at_followers_only():
    from src.influencer.types import InfluencerProfile

    handler = make_handler()
    assert handler.assess_qualification(InfluencerProfile(wechat_id="w", follower_count=20000)) == "达标", \
        "点赞查不到不影响，运营审批时复查"
    assert handler.assess_qualification(InfluencerProfile(wechat_id="w", follower_count=500)).startswith("不达标：粉丝 500")
    assert handler.assess_qualification(InfluencerProfile(wechat_id="w")).startswith("待核实")
