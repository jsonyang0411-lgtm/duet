"""执行被中断的审批单（B23）：断电、强制退出后停在"执行中"的单，运营人工核对后
在控制台标成已执行或失败（handler.resolve_interrupted_execution）。"""

import time

import pytest

from src.influencer.handler import InfluencerBusinessHandler
from src.influencer.settings import PERSONAL_INFO_RETENTION_SECONDS, STUCK_EXECUTION_SECONDS
from src.influencer.store import EVENT_EXECUTION_STARTED, SOURCE_BOT, InfluencerStore
from src.influencer.types import GateStatus
from test_influencer_acceptance import FakeERP, get_packet, make_handler, run, submit_full_request
from test_influencer_store import NEXT_YEAR, make_packet


class ProcessKilled(BaseException):
    """模拟执行到一半进程被杀：不是 Exception，处理器的失败处理接不住。"""


class KilledWhileOrderingERP(FakeERP):
    async def create_sample_order(self, **kwargs):
        raise ProcessKilled()


def interrupted_packet():
    """走真实的批准流程，在聚水潭建单时"断电"，留下一张停在执行中的单。"""
    handler = make_handler(KilledWhileOrderingERP())
    packet_id = submit_full_request(handler)
    with pytest.raises(ProcessKilled):
        run(handler.execute_operator_approval(packet_id, True))
    assert get_packet(handler, packet_id).status == GateStatus.EXECUTING
    return handler, packet_id, handler.store.execution_started_at(packet_id)


def test_starting_an_execution_is_on_record():
    handler, packet_id, started = interrupted_packet()
    assert started is not None
    actions = [e["action"] for e in handler.store.events_for([packet_id])[packet_id]]
    assert actions[-1] == EVENT_EXECUTION_STARTED


def test_a_packet_that_may_still_be_running_cannot_be_resolved():
    handler, packet_id, started = interrupted_packet()
    result = handler.resolve_interrupted_execution(
        packet_id, True, order_sn="JST-9", now=started + STUCK_EXECUTION_SECONDS - 60,
    )
    assert result["success"] is False and "可能还在跑" in result["error"]
    assert get_packet(handler, packet_id).status == GateStatus.EXECUTING


def test_after_checking_the_operator_marks_it_done_and_gets_the_message():
    handler, packet_id, started = interrupted_packet()
    now = started + STUCK_EXECUTION_SECONDS
    result = handler.resolve_interrupted_execution(
        packet_id, True, order_sn=" JST-9 ", tracking_number="SF123", commission_plan="PLAN-1", now=now,
    )
    assert result["success"] is True and "SF123" in result["notice"]
    packet = get_packet(handler, packet_id)
    assert (packet.status, packet.order_sn, packet.tracking_number, packet.commission_plan) == (
        GateStatus.FULFILLED, "JST-9", "SF123", "PLAN-1",
    )
    assert packet.handled_at == now
    events = handler.store.events_for([packet_id])[packet_id]
    assert (events[-1]["action"], events[-1]["detail"]) == ("人工核对：已执行", "出库单 JST-9")


def test_marking_it_done_needs_the_order_number():
    handler, packet_id, started = interrupted_packet()
    result = handler.resolve_interrupted_execution(packet_id, True, now=started + STUCK_EXECUTION_SECONDS)
    assert result["success"] is False and "出库单号" in result["error"]
    assert get_packet(handler, packet_id).status == GateStatus.EXECUTING


def test_marking_it_failed_needs_what_the_check_found():
    handler, packet_id, started = interrupted_packet()
    now = started + STUCK_EXECUTION_SECONDS
    assert handler.resolve_interrupted_execution(packet_id, False, note="  ", now=now)["success"] is False
    result = handler.resolve_interrupted_execution(packet_id, False, note="聚水潭里没有这张单", now=now)
    assert result["success"] is True
    packet = get_packet(handler, packet_id)
    assert packet.status == GateStatus.FAILED
    assert "聚水潭里没有这张单" in packet.failure_reason


def test_it_can_only_be_resolved_once():
    handler, packet_id, started = interrupted_packet()
    now = started + STUCK_EXECUTION_SECONDS
    assert handler.resolve_interrupted_execution(packet_id, True, order_sn="JST-9", now=now)["success"]
    again = handler.resolve_interrupted_execution(packet_id, False, note="重复点了", now=now)
    assert again["success"] is False
    assert get_packet(handler, packet_id).status == GateStatus.FULFILLED


def test_a_packet_that_is_not_executing_is_left_alone():
    handler = make_handler()
    packet_id = submit_full_request(handler)
    result = handler.resolve_interrupted_execution(packet_id, True, order_sn="JST-9", now=NEXT_YEAR)
    assert result["success"] is False
    assert get_packet(handler, packet_id).status == GateStatus.PENDING


def test_a_packet_from_before_start_times_were_recorded_can_be_resolved_right_away():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet("GATE-OLD", expires_at=NEXT_YEAR), source=SOURCE_BOT)
    store.transition("GATE-OLD", GateStatus.PENDING, GateStatus.EXECUTING)
    handler = InfluencerBusinessHandler(store, erp_client=FakeERP())
    result = handler.resolve_interrupted_execution("GATE-OLD", False, note="小红书里没有绑上")
    assert result["success"] is True


def test_the_recipient_details_are_cleared_90_days_after_it_is_resolved():
    handler, packet_id, started = interrupted_packet()
    now = started + STUCK_EXECUTION_SECONDS
    handler.resolve_interrupted_execution(packet_id, True, order_sn="JST-9", now=now)
    handler.store.run_maintenance(now=now + PERSONAL_INFO_RETENTION_SECONDS + 1)
    packet = get_packet(handler, packet_id)
    assert (packet.recipient_name, packet.recipient_phone, packet.shipping_address) == ("", "", "")


def test_until_it_is_resolved_a_stuck_packet_keeps_its_details():
    """停在执行中的单不算处理完，收件信息不清除（运营核对时还要用）；标完以后才开始算 90 天。"""
    handler, packet_id, started = interrupted_packet()
    handler.store.run_maintenance(now=time.time() + PERSONAL_INFO_RETENTION_SECONDS * 2)
    assert get_packet(handler, packet_id).recipient_phone
