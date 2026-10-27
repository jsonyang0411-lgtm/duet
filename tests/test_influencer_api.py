"""控制台「达人名单」页的接口：/api/influencer/*（src/influencer/console.py，经 src/web/server.py 转发）。

请求走真实的 _UIHandler（沿用 test_web_api 的写法）；数据库是临时文件。
"""

import json

import pytest

from src.influencer.store import InfluencerStore
from test_web_api import _build_handler


@pytest.fixture
def store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "messages.db")
    monkeypatch.setattr("src.web.server._influencer_db_path", lambda: db_path)
    return InfluencerStore(db_path)


def call(path, body=None, *, method=None, headers=None):
    """发一个请求，返回 (HTTP 状态码, JSON)。"""
    method = method or ("POST" if body is not None else "GET")
    raw = json.dumps(body).encode() if body is not None else b""
    # _build_handler 要至少有一个请求头，请求体才拼得对
    headers = {"Content-Type": "application/json", **(headers or {})}
    _, sock = _build_handler(path, method=method, body=raw, headers=headers)
    head, _, payload = sock.get_response_text().partition("\r\n\r\n")
    return int(head.split(" ", 2)[1]), json.loads(payload)


def enabled(store, chat_id):
    return (store.get_contact(chat_id) or {}).get("enabled") is True


# ---- 名单 ----

def test_contacts_can_be_added_disabled_and_listed(store):
    _, added = call("/api/influencer/contacts/action", {"chat_id": "wxid_a", "display_name": "小美"})
    assert added["ok"] is True and added["contact"]["enabled"] is True
    assert enabled(store, "wxid_a")
    call("/api/influencer/contacts/action", {"chat_id": "wxid_a", "enabled": False})
    assert not enabled(store, "wxid_a")
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


def test_contacts_keep_a_platform_and_number(store):
    status, data = call("/api/influencer/contacts/action", {"chat_id": "wxid_p", "platform": "dy", "platform_no": " test_daren01 "})
    assert status == 200 and data["ok"] is True
    assert (data["contact"]["platform"], data["contact"]["platform_no"]) == ("DY", "test_daren01")
    _, bad = call("/api/influencer/contacts/action", {"chat_id": "wxid_p", "platform": "TB"})
    assert bad["ok"] is False and "平台" in bad["error"]
    _, missing = call("/api/influencer/contacts/action", {"chat_id": "wxid_q", "platform_no": "x"})
    assert missing["ok"] is False and "选平台" in missing["error"]


# ---- 安全 ----

@pytest.mark.parametrize("path", ["/api/influencer/contacts/action", "/api/influencer/inbox/action"])
def test_the_write_endpoints_refuse_get(store, path):
    status, _ = call(path, method="GET")
    assert status == 405


def test_another_website_cannot_change_the_list(store):
    status, _ = call("/api/influencer/contacts/action", {"chat_id": "wxid_a"}, headers={"Origin": "https://evil.example"})
    assert status == 403
    assert store.list_contacts() == []


@pytest.mark.parametrize("path", ["/api/influencer/nope", "/api/influencer/approvals", "/api/influencer/status",
                                  "/api/influencer/overview"])
def test_an_unknown_influencer_path_is_not_found(store, path):
    # 寄样审批单 2026-10-27 起走 PRM 的平台动作：老的审批接口都没有了
    status, data = call(path)
    assert status == 404 and data["ok"] is False


def test_the_old_approval_action_is_gone(store):
    status, _ = call("/api/influencer/approvals/action", {"packet_id": "GATE-1", "action": "approve"})
    assert status == 405


# ---- 待处理 ----

def test_inbox_items_are_listed_counted_and_marked_handled(store):
    item_id = store.add_inbox_item("wxid_a", "draft", "老师好")
    assert call("/api/influencer/counts")[1] == {"ok": True, "inbox": 1}
    _, listed = call("/api/influencer/inbox")
    assert [i["content"] for i in listed["items"]] == ["老师好"]
    _, done = call("/api/influencer/inbox/action", {"id": item_id})
    assert done["ok"] is True
    _, again = call("/api/influencer/inbox/action", {"id": item_id})
    assert again["ok"] is False
    assert call("/api/influencer/inbox")[1]["items"] == []


def test_the_sidebar_count_never_creates_the_database(tmp_path, monkeypatch):
    db_path = tmp_path / "messages.db"
    monkeypatch.setattr("src.web.server._influencer_db_path", lambda: str(db_path))
    assert call("/api/influencer/counts")[1] == {"ok": True, "inbox": 0}
    assert not db_path.exists(), "角标每 30 秒问一次，不能顺手建库建表"
