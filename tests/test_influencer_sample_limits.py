"""寄样上限（D5，数字还没定）：环境变量不设就不限；设了以后，超过上限的单不能批准，
控制台上标出原因。"""

import time

import pytest

from src.influencer.handler import sample_limit_problem
from src.influencer.settings import sample_limits
from src.influencer.store import SOURCE_BOT, InfluencerStore
from src.influencer.types import GateStatus
from test_influencer_acceptance import INFLUENCER, get_packet, make_handler, run, submit_full_request
from test_influencer_api import call
from test_influencer_store import NEXT_YEAR, make_packet

LIMIT_VARS = ("INFLUENCER_MAX_SAMPLES_PER_PERSON", "INFLUENCER_MAX_SAMPLES_PER_DAY", "INFLUENCER_SAMPLE_WINDOW_DAYS")


@pytest.fixture(autouse=True)
def no_limits(monkeypatch):
    for name in LIMIT_VARS:
        monkeypatch.delenv(name, raising=False)


def shipped(store, packet_id, chat_id=INFLUENCER, handled_at=None):
    store.add_approval(make_packet(packet_id, chat_id=chat_id, expires_at=NEXT_YEAR), source=SOURCE_BOT)
    store.transition(packet_id, GateStatus.PENDING, GateStatus.EXECUTING)
    store.transition(packet_id, GateStatus.EXECUTING, GateStatus.FULFILLED, handled_at=handled_at or time.time())


def test_without_settings_there_is_no_limit():
    assert sample_limits() == {"per_person": None, "window_days": 30, "per_day": None}
    handler = make_handler()
    for n in range(3):
        shipped(handler.store, f"GATE-OLD{n}")
    assert run(handler.execute_operator_approval(submit_full_request(handler), True))["success"] is True


@pytest.mark.parametrize("raw", ["", "0", "-1", "两单"])
def test_a_limit_that_is_not_a_positive_number_means_no_limit(monkeypatch, raw):
    monkeypatch.setenv("INFLUENCER_MAX_SAMPLES_PER_PERSON", raw)
    assert sample_limits()["per_person"] is None


def test_the_per_person_limit_stops_the_approval_and_keeps_it_pending(monkeypatch):
    monkeypatch.setenv("INFLUENCER_MAX_SAMPLES_PER_PERSON", "1")
    handler = make_handler()
    shipped(handler.store, "GATE-OLD")
    packet_id = submit_full_request(handler)
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False and "每人 1 单的上限" in result["error"]
    assert get_packet(handler, packet_id).status == GateStatus.PENDING


def test_only_samples_inside_the_window_count(monkeypatch):
    monkeypatch.setenv("INFLUENCER_MAX_SAMPLES_PER_PERSON", "1")
    monkeypatch.setenv("INFLUENCER_SAMPLE_WINDOW_DAYS", "30")
    handler = make_handler()
    shipped(handler.store, "GATE-OLD", handled_at=time.time() - 31 * 24 * 3600)
    assert run(handler.execute_operator_approval(submit_full_request(handler), True))["success"] is True


def test_other_influencers_do_not_count_against_the_per_person_limit(monkeypatch):
    monkeypatch.setenv("INFLUENCER_MAX_SAMPLES_PER_PERSON", "1")
    handler = make_handler()
    shipped(handler.store, "GATE-OTHER", chat_id="wxid_other")
    assert run(handler.execute_operator_approval(submit_full_request(handler), True))["success"] is True


def test_the_daily_limit_counts_everyone_including_what_is_executing(monkeypatch):
    monkeypatch.setenv("INFLUENCER_MAX_SAMPLES_PER_DAY", "2")
    handler = make_handler()
    shipped(handler.store, "GATE-A", chat_id="wxid_a")
    handler.store.add_approval(make_packet("GATE-B", chat_id="wxid_b", expires_at=NEXT_YEAR), source=SOURCE_BOT)
    handler.store.transition("GATE-B", GateStatus.PENDING, GateStatus.EXECUTING)
    packet = get_packet(handler, submit_full_request(handler))
    assert "每天 2 单的上限" in sample_limit_problem(handler.store, packet)


def test_yesterdays_samples_do_not_count_today(monkeypatch):
    monkeypatch.setenv("INFLUENCER_MAX_SAMPLES_PER_DAY", "1")
    handler = make_handler()
    shipped(handler.store, "GATE-Y", chat_id="wxid_a", handled_at=time.time() - 2 * 24 * 3600)
    packet = get_packet(handler, submit_full_request(handler))
    assert sample_limit_problem(handler.store, packet) == ""


def test_the_console_marks_a_packet_over_the_limit(tmp_path, monkeypatch):
    db_path = str(tmp_path / "messages.db")
    monkeypatch.setattr("src.web.server._influencer_db_path", lambda: db_path)
    api_store = InfluencerStore(db_path)
    monkeypatch.setenv("INFLUENCER_MAX_SAMPLES_PER_PERSON", "1")
    shipped(api_store, "GATE-OLD", chat_id="wxid_a")
    api_store.add_approval(make_packet("GATE-NEW", chat_id="wxid_a", expires_at=NEXT_YEAR), source=SOURCE_BOT)
    _, data = call("/api/influencer/approvals?status=pending")
    [item] = data["items"]
    assert "上限" in item["limit_problem"]
    _, status = call("/api/influencer/status")
    assert status["sample_limits"]["per_person"] == 1
