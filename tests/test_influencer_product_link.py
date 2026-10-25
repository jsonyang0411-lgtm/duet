"""商品推广链接：由小红书平台生成、通过小红书 CLI 调用；后台负责在批准后调用、存进审批单、
写进发给达人的确认消息。链接没生成不算执行失败，留给运营手动补。"""

import sqlite3

from src.influencer.store import InfluencerStore
from src.influencer.types import GateStatus
from src.influencer.xhs_cli import XHSCLIError, XiaohongshuCLIClient
from test_influencer_acceptance import FakeXHS, get_packet, make_handler, run, submit_full_request


def test_after_approval_the_link_is_saved_and_sent_with_the_message():
    xhs = FakeXHS()
    handler = make_handler(xhs=xhs)
    packet_id = submit_full_request(handler)
    result = run(handler.execute_operator_approval(packet_id, True))
    link = f"https://xhslink.com/test/{packet_id}"
    assert result["success"] is True and result["product_link"] == link
    assert link in result["notice"]
    assert get_packet(handler, packet_id).product_link == link
    [call] = xhs.link_calls
    assert call["idempotency_key"] == packet_id, "审批单号作为幂等键，重复调用只生成一次"


class NoLinkXHS(FakeXHS):
    async def create_product_link(self, **kwargs):
        raise XHSCLIError("product-link 命令不存在")


def test_a_missing_link_does_not_undo_a_shipped_sample():
    handler = make_handler(xhs=NoLinkXHS())
    packet_id = submit_full_request(handler)
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is True and "推广链接没有生成" in result["warning"]
    assert "推广链接" not in result["notice"]
    packet = get_packet(handler, packet_id)
    assert (packet.status, packet.product_link) == (GateStatus.FULFILLED, "")
    actions = [e["action"] for e in handler.store.events_for([packet_id])[packet_id]]
    assert "推广链接没有生成" in actions


class EmptyLinkXHS(FakeXHS):
    async def create_product_link(self, **kwargs):
        return {"success": True}


def test_a_success_without_a_link_counts_as_no_link():
    handler = make_handler(xhs=EmptyLinkXHS())
    result = run(handler.execute_operator_approval(submit_full_request(handler), True))
    assert "没有返回链接" in result["warning"]


def test_no_link_is_made_when_the_commission_fails():
    class FailingCommission(FakeXHS):
        async def set_exclusive_commission(self, **kwargs):
            return {"success": False, "error": "CLI 超时"}

    xhs = FailingCommission()
    handler = make_handler(xhs=xhs)
    run(handler.execute_operator_approval(submit_full_request(handler), True))
    assert xhs.link_calls == []


def test_the_cli_command_follows_the_draft_contract(monkeypatch):
    client = XiaohongshuCLIClient(cli_path="xhs", mock_mode=False)
    seen = {}

    async def fake_run(*args):
        seen["args"] = args
        return {"success": True, "link": "https://xhslink.com/abc"}

    monkeypatch.setattr(client, "_run", fake_run)
    res = run(client.create_product_link(xhs_uid="xhs_1", sku_id="SKU-1", idempotency_key="GATE-1"))
    assert res["link"] == "https://xhslink.com/abc"
    assert seen["args"] == (
        "product-link", "create", "--uid", "xhs_1", "--sku", "SKU-1", "--idempotency-key", "GATE-1", "--json",
    )


def test_a_database_from_before_links_gets_the_new_column(tmp_path):
    db_path = tmp_path / "messages.db"
    InfluencerStore(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE influencer_approvals DROP COLUMN product_link")
    conn.commit()
    conn.close()
    store = InfluencerStore(str(db_path))
    columns = [row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(influencer_approvals)")]
    assert "product_link" in columns
    assert store.list_approvals() == []
