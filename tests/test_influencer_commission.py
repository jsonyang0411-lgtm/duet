"""达人要求提高佣金（2026-10-25 用户定）：代码识别后回固定话术，不交给模型谈，
佣金上限不变，并记进控制台「待处理」。"""

import pytest

from src.influencer.extract import asks_for_higher_commission
from src.influencer.handler import COMMISSION_REPLY, FORBIDDEN_WORDS
from src.influencer.settings import MAX_COMMISSION_RATE
from src.influencer.store import INBOX_COMMISSION
from test_influencer_acceptance import (
    FULL_ADDRESS,
    INFLUENCER,
    SAMPLE_REQUEST,
    XHS_PROFILE,
    make_handler,
    pending_packets,
    run,
)


@pytest.mark.parametrize("text", [
    "佣金能不能高点呀",
    "这个点位有点低了",
    "返点再多一点呗",
    "能给30%吗",
    "35个点可以吗",
    "佣金提到30点行不行",
    "老师们都是高佣合作的，你们能调高吗",
])
def test_asking_for_more_commission_is_recognised(text):
    assert asks_for_higher_commission(text)


@pytest.mark.parametrize("text", [
    "佣金是多少呀",
    "合作佣金怎么算的",
    "我想申请眼霜样品",
    "20%可以的",
    "明天下午30点之前发货吗",  # 单独的"点"可能是时间
    "好的谢谢老师",
])
def test_other_messages_are_not_taken_as_a_commission_request(text):
    assert not asks_for_higher_commission(text)


def test_the_cap_is_still_25_percent():
    assert MAX_COMMISSION_RATE == 0.25
    assert not asks_for_higher_commission("25%可以吗")
    assert asks_for_higher_commission("26%可以吗")


def test_the_fixed_reply_is_sent_and_the_operator_is_told():
    handler = make_handler()
    reply = run(handler.process_incoming_message(INFLUENCER, "佣金能不能高点呀", nickname="小美"))
    assert reply == COMMISSION_REPLY
    assert "诚意" in reply and "先收样品" in reply
    assert not [w for w in FORBIDDEN_WORDS if w in reply], "固定话术里不能有违规词"
    [item] = handler.store.list_inbox()
    assert (item["kind"], item["content"], item["auto_replied"]) == (INBOX_COMMISSION, "佣金能不能高点呀", True)


def test_asking_again_soon_does_not_repeat_the_same_reply():
    handler = make_handler()
    run(handler.process_incoming_message(INFLUENCER, "佣金能不能高点呀"))
    again = run(handler.process_incoming_message(INFLUENCER, "真的不能再高一点吗，30%吧"))
    assert again == ""
    assert [i["auto_replied"] for i in handler.store.list_inbox()] == [False, True]


def test_the_commission_reply_does_not_stop_the_neutral_reply_to_other_messages():
    handler = make_handler()
    run(handler.process_incoming_message(INFLUENCER, "佣金能不能高点呀"))
    assert run(handler.process_incoming_message(INFLUENCER, "你们是哪个品牌呀")) != ""


def test_a_request_in_progress_carries_on_and_the_commission_is_unchanged():
    handler = make_handler()
    run(handler.process_incoming_message(INFLUENCER, SAMPLE_REQUEST, nickname="小美"))
    run(handler.process_incoming_message(INFLUENCER, "佣金能给30%吗", nickname="小美"))
    run(handler.process_incoming_message(INFLUENCER, f"{FULL_ADDRESS} 小红书主页 {XHS_PROFILE}", nickname="小美"))
    [packet] = pending_packets(handler)
    assert packet.proposed_commission_rate == MAX_COMMISSION_RATE


def test_someone_not_on_the_list_is_not_answered():
    handler = make_handler()
    assert run(handler.process_incoming_message("wxid_stranger", "佣金能不能高点呀")) is None
