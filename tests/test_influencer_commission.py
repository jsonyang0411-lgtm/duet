"""达人要求提高佣金（2026-10-25 用户定，D37 改起步 20%）：代码识别后用固定话术，不交给模型谈，
佣金上限不变。话术放进待审批、记进「待处理」的部分在 tests/test_influencer_sample_bridge.py。"""

import pytest

from src.influencer.extract import asks_for_higher_commission
from src.influencer.handler import COMMISSION_REPLY, FORBIDDEN_WORDS
from src.influencer.settings import DEFAULT_COMMISSION_RATE, MAX_COMMISSION_RATE


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


def test_start_at_20_percent_and_the_cap_is_still_25_percent():
    # D37：起步 20%，表现好再涨，最高 25%。达人要 25% 也是要求加佣金，转运营
    assert (DEFAULT_COMMISSION_RATE, MAX_COMMISSION_RATE) == (0.20, 0.25)
    assert not asks_for_higher_commission("20%可以吗")
    assert asks_for_higher_commission("25%可以吗")
    assert asks_for_higher_commission("26%可以吗")


def test_the_fixed_reply_only_says_we_will_ask():
    # D37：只说帮老师申请，不替运营拒绝，也不承诺能涨。什么时候放进待审批、一段时间内只放一次，
    # 见 tests/test_influencer_sample_bridge.py
    assert "申请" in COMMISSION_REPLY and "先收样品" in COMMISSION_REPLY and "已经是" not in COMMISSION_REPLY
    assert not [w for w in FORBIDDEN_WORDS if w in COMMISSION_REPLY], "固定话术里不能有违规词"
