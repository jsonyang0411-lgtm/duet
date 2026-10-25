"""控制台「达人审批」页的接口：/api/influencer/*（src/influencer/console.py，经 src/web/server.py 转发）。

请求走真实的 _UIHandler（沿用 test_web_api 的写法）；数据库是临时文件；
执行审批时打开模拟模式，不碰真实的聚水潭和小红书。
"""

import json

import pytest

from src.influencer.store import SOURCE_BOT, InfluencerStore
from src.influencer.types import GateStatus
from test_influencer_store import NEXT_YEAR, make_packet
from test_web_api import _build_handler


@pytest.fixture
def store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "messages.db")
    monkeypatch.setattr("src.web.server._influencer_db_path", lambda: db_path)
    for name in ("INFLUENCER_MOCK", "JST_APP_KEY", "XHS_CLI_PATH"):
        monkeypatch.delenv(name, raising=False)
    return InfluencerStore(db_path)


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setenv("INFLUENCER_MOCK", "1")


def call(path, body=None, *, method=None, headers=None):
    """发一个请求，返回 (HTTP 状态码, JSON)。"""
    method = method or ("POST" if body is not None else "GET")
    raw = json.dumps(body).encode() if body is not None else b""
    # _build_handler 要至少有一个请求头，请求体才拼得对
    headers = {"Content-Type": "application/json", **(headers or {})}
    _, sock = _build_handler(path, method=method, body=raw, headers=headers)
    head, _, payload = sock.get_response_text().partition("\r\n\r\n")
    return int(head.split(" ", 2)[1]), json.loads(payload)


def add_packet(store, packet_id="GATE-1", **overrides):
    overrides.setdefault("expires_at", NEXT_YEAR)
    store.add_approval(make_packet(packet_id, **overrides), source=SOURCE_BOT)


# ---- 状态 ----

def test_status_says_why_the_erp_cannot_be_used(store):
    status, data = call("/api/influencer/status")
    assert status == 200 and data["ok"] is True
    assert data["mock"] is False
    assert data["erp"]["ready"] is False and "JST_APP_KEY" in data["erp"]["detail"]
    assert data["max_commission_rate"] == 0.25


def test_status_flags_mock_mode(store, mock_mode):
    add_packet(store)
    _, data = call("/api/influencer/status")
    assert data["mock"] is True
    assert data["erp"]["mock"] is True and data["xhs"]["mock"] is True
    assert data["pending"] == 1


# ---- 审批单 ----

def test_pending_approvals_come_with_the_message_preview_and_history(store):
    add_packet(store)
    _, data = call("/api/influencer/approvals?status=pending")
    [item] = data["items"]
    assert (item["packet_id"], item["status_label"]) == ("GATE-1", "待审批")
    assert "〔运单号〕" in item["notice"]
    assert [e["action"] for e in item["events"]] == ["提交审批"]
    assert data["counts"]["pending"] == 1


def test_an_approval_from_the_console_runs_once(store, mock_mode):
    add_packet(store)
    body = {"packet_id": "GATE-1", "action": "approve", "xhs_account": "xhs_1", "commission_rate": 0.2}
    _, first = call("/api/influencer/approvals/action", body)
    assert first["ok"] is True and first["order_sn"]
    assert first["packet"]["status"] == GateStatus.FULFILLED.value
    assert first["tracking_number"] in first["notice"]
    _, second = call("/api/influencer/approvals/action", body)
    assert second["ok"] is False
    assert [e["action"] for e in second["packet"]["events"]].count("执行成功") == 1


def test_a_rejection_keeps_the_reason_and_returns_the_polite_message(store):
    add_packet(store)
    _, data = call("/api/influencer/approvals/action",
                   {"packet_id": "GATE-1", "action": "reject", "reason": "粉丝画像不匹配"})
    assert data["ok"] is True and "没能通过" in data["notice"]
    assert data["packet"]["events"][-1]["detail"] == "粉丝画像不匹配"


def test_a_commission_that_is_not_a_number_is_refused(store):
    add_packet(store)
    _, data = call("/api/influencer/approvals/action",
                   {"packet_id": "GATE-1", "action": "approve", "commission_rate": "两成"})
    assert data["ok"] is False and "数字" in data["error"]
    assert store.get_approval("GATE-1").status == GateStatus.PENDING


def test_an_unknown_filter_is_an_error(store):
    _, data = call("/api/influencer/approvals?status=whatever")
    assert data["ok"] is False


def test_an_interrupted_packet_is_flagged_and_can_be_resolved(store):
    add_packet(store)
    store.transition("GATE-1", GateStatus.PENDING, GateStatus.EXECUTING)
    _, listed = call("/api/influencer/approvals?status=executing")
    assert listed["items"][0]["interrupted"] is True
    body = {"packet_id": "GATE-1", "action": "resolve", "outcome": "fulfilled", "order_sn": "JST-9"}
    _, data = call("/api/influencer/approvals/action", body)
    assert data["ok"] is True and data["packet"]["status"] == GateStatus.FULFILLED.value
    assert "快递单号出来后" in data["notice"]


def test_a_packet_that_just_started_is_not_flagged(store):
    add_packet(store)
    store.transition("GATE-1", GateStatus.PENDING, GateStatus.EXECUTING)
    store.add_event("开始执行", packet_id="GATE-1", source=SOURCE_BOT)
    _, listed = call("/api/influencer/approvals?status=executing")
    assert listed["items"][0]["interrupted"] is False
    _, data = call("/api/influencer/approvals/action",
                   {"packet_id": "GATE-1", "action": "resolve", "outcome": "failed", "note": "没有单"})
    assert data["ok"] is False and store.get_approval("GATE-1").status == GateStatus.EXECUTING


def test_an_unknown_resolve_outcome_is_an_error(store):
    add_packet(store)
    _, data = call("/api/influencer/approvals/action", {"packet_id": "GATE-1", "action": "resolve", "outcome": "maybe"})
    assert data["ok"] is False


# ---- 名单 ----

def test_contacts_can_be_added_disabled_and_listed(store):
    _, added = call("/api/influencer/contacts/action", {"chat_id": "wxid_a", "display_name": "小美"})
    assert added["ok"] is True and added["contact"]["enabled"] is True
    assert store.is_influencer("wxid_a")
    call("/api/influencer/contacts/action", {"chat_id": "wxid_a", "enabled": False})
    assert not store.is_influencer("wxid_a")
    _, listed = call("/api/influencer/contacts")
    assert [(c["chat_id"], c["display_name"], c["enabled"]) for c in listed["items"]] == [
        ("wxid_a", "小美", False),
    ]


def test_a_group_chat_cannot_be_made_an_influencer(store):
    _, data = call("/api/influencer/contacts/action", {"chat_id": "123@chatroom"})
    assert data["ok"] is False
    assert store.list_contacts() == []


def test_recent_chats_are_empty_before_the_bot_ever_ran(store):
    assert call("/api/influencer/recent-chats") == (200, {"ok": True, "items": []})


# ---- 安全 ----

@pytest.mark.parametrize("path", ["/api/influencer/approvals/action", "/api/influencer/contacts/action"])
def test_the_write_endpoints_refuse_get(store, path):
    status, _ = call(path, method="GET")
    assert status == 405


def test_another_website_cannot_approve(store, mock_mode):
    add_packet(store, xhs_account="xhs_1")
    status, _ = call("/api/influencer/approvals/action", {"packet_id": "GATE-1", "action": "approve"},
                     headers={"Origin": "https://evil.example"})
    assert status == 403
    assert store.get_approval("GATE-1").status == GateStatus.PENDING


def test_an_unknown_influencer_path_is_not_found(store):
    status, data = call("/api/influencer/nope")
    assert status == 404 and data["ok"] is False


def test_inbox_items_are_listed_counted_and_marked_handled(store):
    item_id = store.add_inbox_item("wxid_a", "message", "什么时候发货")
    _, status = call("/api/influencer/status")
    assert status["inbox"] == 1
    _, listed = call("/api/influencer/inbox")
    assert [i["content"] for i in listed["items"]] == ["什么时候发货"]
    _, done = call("/api/influencer/inbox/action", {"id": item_id})
    assert done["ok"] is True
    _, again = call("/api/influencer/inbox/action", {"id": item_id})
    assert again["ok"] is False
    assert call("/api/influencer/inbox")[1]["items"] == []
    assert call("/api/influencer/inbox/action", method="GET")[0] == 405


def test_the_sidebar_count_never_creates_the_database(tmp_path, monkeypatch):
    db_path = tmp_path / "messages.db"
    monkeypatch.setattr("src.web.server._influencer_db_path", lambda: str(db_path))
    assert call("/api/influencer/counts")[1] == {"ok": True, "pending": 0, "inbox": 0}
    assert not db_path.exists(), "角标每 30 秒问一次，不能顺手建库建表"


def test_the_sidebar_count_adds_pending_approvals_and_inbox_items(store):
    add_packet(store)
    store.add_inbox_item("wxid_a", "message", "什么时候发货")
    assert call("/api/influencer/counts")[1] == {"ok": True, "pending": 1, "inbox": 1}


def test_the_overview_comes_with_a_brief_to_copy(store):
    add_packet(store)
    _, data = call("/api/influencer/overview")
    assert data["ok"] is True and data["pending"] == 1
    assert "待审批：1 单" in data["brief"]
