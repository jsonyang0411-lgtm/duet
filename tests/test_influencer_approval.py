"""运营审批时可以填写的内容：补填小红书账号、调低佣金（src/influencer/handler.py）。"""

from test_influencer_acceptance import (
    FULL_ADDRESS,
    INFLUENCER,
    SAMPLE_REQUEST,
    FakeERP,
    FakeXHS,
    make_handler,
    pending_packets,
    run,
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
