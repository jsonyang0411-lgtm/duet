"""个人微信防封的发送规则（src/influencer/send_policy.py）和主动发送队列（influencer_outbox）。"""

import random
import time

import pytest

from src.influencer.handler import InfluencerBusinessHandler
from src.influencer.send_policy import MAX_REPLY_DELAY_SECONDS, SendPolicy
from src.influencer.settings import PERSONAL_INFO_RETENTION_SECONDS
from src.influencer.store import SOURCE_CONSOLE, InfluencerStore
from test_influencer_acceptance import INFLUENCER, FakeERP, FakeXHS

POLICY_VARS = ("INFLUENCER_REPLY_DELAY", "INFLUENCER_SEND_MIN_INTERVAL", "INFLUENCER_SEND_MAX_PER_HOUR",
               "INFLUENCER_QUIET_HOURS")


def at(hour, minute=0, day=10):
    """2026-11-<day> 的本地时间。"""
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def default_env(monkeypatch):
    for name in POLICY_VARS:
        monkeypatch.delenv(name, raising=False)


# ---- 参数 ----

def test_the_defaults_are_conservative():
    policy = SendPolicy.from_env()
    assert (policy.reply_delay_range, policy.min_interval, policy.max_per_hour, policy.quiet_hours) == (
        (2.0, 6.0), 30, 20, (23, 8),
    )


def test_settings_can_be_changed_and_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("INFLUENCER_REPLY_DELAY", "1-3")
    monkeypatch.setenv("INFLUENCER_SEND_MIN_INTERVAL", "60")
    monkeypatch.setenv("INFLUENCER_SEND_MAX_PER_HOUR", "abc")
    monkeypatch.setenv("INFLUENCER_QUIET_HOURS", "22-7")
    policy = SendPolicy.from_env()
    assert (policy.reply_delay_range, policy.min_interval, policy.max_per_hour, policy.quiet_hours) == (
        (1.0, 3.0), 60, 20, (22, 7),
    )


@pytest.mark.parametrize("raw", ["", "0"])
def test_quiet_hours_can_be_turned_off(monkeypatch, raw):
    monkeypatch.setenv("INFLUENCER_QUIET_HOURS", raw)
    assert SendPolicy.from_env().quiet_hours is None


# ---- 回复前等待 ----

def test_the_reply_waits_longer_for_longer_text_but_not_forever():
    policy = SendPolicy(reply_delay_range=(2.0, 2.0))
    assert policy.reply_delay("好的") == pytest.approx(2.0 + 2 * 0.15)
    assert policy.reply_delay("好" * 1000) == MAX_REPLY_DELAY_SECONDS


def test_time_already_spent_is_taken_off():
    policy = SendPolicy(reply_delay_range=(4.0, 4.0))
    assert policy.reply_delay("嗯", already_spent=8.0) == 0.0, "Claude 已经想了 8 秒，不用再等"
    assert policy.reply_delay("嗯", already_spent=1.0) == pytest.approx(4.0 + 0.15 - 1.0)


def test_the_wait_is_random_inside_the_range():
    policy = SendPolicy(reply_delay_range=(2.0, 6.0))
    waits = {round(policy.reply_delay("嗯", rng=random.Random(seed)), 2) for seed in range(5)}
    assert len(waits) > 1 and all(2.0 <= w <= 6.0 + 0.15 for w in waits)


def test_no_wait_when_switched_off():
    assert SendPolicy(reply_delay_range=(0.0, 0.0)).reply_delay("老师好") == 0.0


def test_the_handler_waits_before_handing_back_the_reply(monkeypatch):
    store = InfluencerStore(":memory:")
    store.add_contacts([INFLUENCER], source=SOURCE_CONSOLE)
    handler = InfluencerBusinessHandler(
        store, erp_client=FakeERP(), xhs_client=FakeXHS(), send_policy=SendPolicy(reply_delay_range=(3.0, 3.0)),
    )
    slept = []
    monkeypatch.setattr("src.influencer.handler.time.sleep", slept.append)
    reply = handler.handle_private_message(INFLUENCER, "你们是哪个品牌")
    assert reply
    [wait] = slept
    assert 3.0 + len(reply) * 0.15 - 1 < wait <= 3.0 + len(reply) * 0.15


# ---- 夜间 ----

@pytest.mark.parametrize("hour, quiet", [(22, False), (23, True), (2, True), (7, True), (8, False), (14, False)])
def test_quiet_hours_wrap_past_midnight(hour, quiet):
    assert SendPolicy().in_quiet_hours(at(hour)) is quiet


def test_quiet_hours_end_the_next_morning():
    policy = SendPolicy()
    assert policy.quiet_hours_end(at(23, 30)) == at(8, day=11)
    assert policy.quiet_hours_end(at(2)) == at(8)


# ---- 主动发送队列 ----

@pytest.fixture
def store():
    return InfluencerStore(":memory:")


def test_an_empty_queue_has_nothing_to_send(store):
    assert store.next_outgoing(SendPolicy(), now=at(10)) == (None, 0.0, "")


def test_messages_are_spaced_out(store):
    policy = SendPolicy(min_interval=30)
    first = store.enqueue_outgoing("wxid_a", "老师好，样品已经安排寄出啦", kind="approval", packet_id="GATE-1")
    store.enqueue_outgoing("wxid_b", "老师好，样品已经安排寄出啦")
    item, wait, _ = store.next_outgoing(policy, now=at(10))
    assert (item["id"], item["packet_id"], wait) == (first, "GATE-1", 0.0)
    assert store.mark_outgoing_sent(first, now=at(10))
    assert not store.mark_outgoing_sent(first, now=at(10)), "同一条只算发出一次"
    item, wait, reason = store.next_outgoing(policy, now=at(10) + 10)
    assert item["chat_id"] == "wxid_b" and wait == pytest.approx(20) and "30 秒" in reason
    assert store.next_outgoing(policy, now=at(10) + 30)[1] == 0.0


def test_the_hourly_limit_holds_the_queue(store):
    policy = SendPolicy(min_interval=0, max_per_hour=2)
    for n in range(3):
        store.enqueue_outgoing(f"wxid_{n}", "消息")
    for n in range(2):
        item, _, _ = store.next_outgoing(policy, now=at(10, n))
        store.mark_outgoing_sent(item["id"], now=at(10, n))
    _, wait, reason = store.next_outgoing(policy, now=at(10, 5))
    assert "每小时最多主动发 2 条" in reason
    assert wait == pytest.approx(55 * 60), "等到最早那条满一小时"


def test_nothing_is_sent_at_night(store):
    store.enqueue_outgoing("wxid_a", "老师好")
    _, wait, reason = store.next_outgoing(SendPolicy(), now=at(23, 30))
    assert reason == "夜间不主动发消息" and wait == pytest.approx(8.5 * 3600)
    assert store.count_unsent() == 1


def test_sent_messages_are_deleted_after_90_days(store):
    item_id = store.enqueue_outgoing("wxid_a", "老师好，收件人张三")
    store.mark_outgoing_sent(item_id, now=at(10))
    store.enqueue_outgoing("wxid_b", "还没发的")
    store.run_maintenance(now=at(10) + PERSONAL_INFO_RETENTION_SECONDS + 1)
    item, _, _ = store.next_outgoing(SendPolicy(quiet_hours=None))
    assert item["chat_id"] == "wxid_b" and store.count_unsent() == 1
