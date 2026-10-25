"""给 claude -p 用的达人工具（src/influencer/mcp_server.py）：工具本身、MCP 协议，
以及"模型只能碰这一位达人、只能提交申请、不能建单和改佣金"这几条保证。"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.influencer.handler import InfluencerBusinessHandler
from src.influencer.mcp_server import (
    ALLOWED_TOOLS,
    InfluencerTools,
    ToolError,
    handle_message,
    mcp_config,
    serve,
)
from src.influencer.settings import DEFAULT_COMMISSION_RATE
from src.influencer.store import INBOX_MESSAGE, INBOX_OUT_OF_STOCK, SOURCE_BOT, SOURCE_CONSOLE, InfluencerStore
from src.influencer.types import GateStatus
from test_influencer_acceptance import INFLUENCER, FakeERP, FakeXHS
from test_influencer_store import NEXT_YEAR, make_packet

ROOT = Path(__file__).resolve().parent.parent
PHONE = "13812345678"
FULL = dict(
    product="VC精华液", recipient_name="张三", recipient_phone=PHONE,
    shipping_address="上海市浦东新区世纪大道100号",
    xhs_account="https://www.xiaohongshu.com/user/profile/5f0000000000000000000001",
    evidence="想申请VC精华液的样品",
)


class SoldOutERP(FakeERP):
    async def check_inventory(self, product_name):
        return {**await super().check_inventory(product_name), "in_stock": False, "available_quantity": 0}


def make_tools(erp=None, chat_id=INFLUENCER):
    store = InfluencerStore(":memory:")
    store.add_contacts([INFLUENCER], source=SOURCE_CONSOLE)
    handler = InfluencerBusinessHandler(store, erp_client=erp or FakeERP(), xhs_client=FakeXHS())
    return InfluencerTools(handler, chat_id, nickname="小美")


def call(tools, name, **arguments):
    response = handle_message(tools, {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments},
    })
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


# ---- 工具 ----

def test_inventory_is_checked_without_showing_the_cost():
    data, is_error = call(make_tools(), "check_inventory", product="VC精华液")
    assert not is_error
    assert data == {"product_name": "VC精华液", "in_stock": True, "available_quantity": 100}


def test_a_complete_request_becomes_a_pending_approval():
    tools = make_tools()
    data, is_error = call(tools, "submit_sample_request", **FULL)
    assert not is_error and data["submitted"] is True
    packet = tools.store.get_approval(data["packet_id"])
    assert packet.status == GateStatus.PENDING
    assert (packet.influencer_id, packet.recipient_phone) == (INFLUENCER, PHONE)
    assert packet.proposed_commission_rate == DEFAULT_COMMISSION_RATE
    assert packet.evidence == "想申请VC精华液的样品"
    assert PHONE not in json.dumps(data, ensure_ascii=False), "返回给模型的电话要打码"
    assert "不要向达人承诺" in data["message"]


def test_a_request_missing_the_address_is_not_submitted():
    tools = make_tools()
    data, _ = call(tools, "submit_sample_request", **{**FULL, "shipping_address": ""})
    assert data["submitted"] is False and data["missing"] == ["shipping_address"]
    assert tools.store.list_approvals([GateStatus.PENDING]) == []


def test_an_out_of_stock_request_is_not_submitted_and_the_operator_is_told():
    tools = make_tools(SoldOutERP())
    data, _ = call(tools, "submit_sample_request", **FULL)
    assert data["submitted"] is False and "没有库存" in data["suggested_reply"]
    assert [i["kind"] for i in tools.store.list_inbox()] == [INBOX_OUT_OF_STOCK]


def test_submitting_twice_does_not_make_a_second_approval():
    tools = make_tools()
    first, _ = call(tools, "submit_sample_request", **FULL)
    second, _ = call(tools, "submit_sample_request", **FULL)
    assert second["already_submitted"] is True and second["packet_id"] == first["packet_id"]
    assert len(tools.store.list_approvals([GateStatus.PENDING])) == 1


def test_someone_not_on_the_list_cannot_submit():
    data, is_error = call(make_tools(chat_id="wxid_stranger"), "submit_sample_request", **FULL)
    assert is_error and "不在达人名单" in data["error"]


def test_status_shows_only_this_influencers_requests_and_no_recipient_details():
    tools = make_tools()
    tools.store.add_approval(
        make_packet("GATE-MINE", chat_id=INFLUENCER, expires_at=NEXT_YEAR,
                    status=GateStatus.FULFILLED, tracking_number="SF777", product_link="https://xhslink.com/x"),
        source=SOURCE_BOT,
    )
    tools.store.add_approval(make_packet("GATE-OTHER", chat_id="wxid_other", expires_at=NEXT_YEAR), source=SOURCE_BOT)
    data, _ = call(tools, "get_request_status")
    assert [r["packet_id"] for r in data["requests"]] == ["GATE-MINE"]
    assert data["requests"][0]["tracking_number"] == "SF777"
    assert data["requests"][0]["product_link"] == "https://xhslink.com/x"
    text = json.dumps(data, ensure_ascii=False)
    assert PHONE not in text and "世纪大道" not in text


def test_hand_over_puts_it_in_the_inbox():
    tools = make_tools()
    data, _ = call(tools, "hand_over_to_operator", summary="达人问能不能签年框")
    assert data["handed_over"] is True
    [item] = tools.store.list_inbox()
    assert (item["kind"], item["chat_id"], item["content"]) == (INBOX_MESSAGE, INFLUENCER, "达人问能不能签年框")


def test_the_model_has_no_tool_to_ship_approve_or_change_commission():
    names = {spec["name"] for spec in make_tools().specs()}
    assert names == {"check_inventory", "submit_sample_request", "get_request_status", "hand_over_to_operator"}
    for spec in make_tools().specs():
        assert "chat_id" not in spec["inputSchema"]["properties"], "达人由启动参数固定，模型换不了人"
        assert "commission" not in json.dumps(spec["inputSchema"])


def test_unknown_tools_and_bad_arguments_are_errors_not_crashes():
    tools = make_tools()
    assert call(tools, "approve_packet")[1] is True
    data, is_error = call(tools, "check_inventory", product="眼霜", chat_id="wxid_other")
    assert is_error and "参数不对" in data["error"]
    with pytest.raises(ToolError):
        tools.check_inventory("")


class BrokenERP(FakeERP):
    async def check_inventory(self, product_name):
        raise RuntimeError("聚水潭超时")


def test_a_system_error_tells_the_model_to_hand_over():
    data, is_error = call(make_tools(BrokenERP()), "check_inventory", product="眼霜")
    assert is_error and "交给运营" in data["error"]


# ---- 协议 ----

def test_initialize_echoes_the_protocol_version_and_offers_tools():
    response = handle_message(make_tools(), {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"},
    })
    assert response["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in response["result"]["capabilities"]


def test_notifications_get_no_reply_and_unknown_methods_get_an_error():
    tools = make_tools()
    assert handle_message(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle_message(tools, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"})["error"]["code"] == -32601


def test_serve_answers_one_line_per_request():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\nnot json\n")
    stdout = io.StringIO()
    serve(make_tools(), stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert len(replies[1]["result"]["tools"]) == 4


def test_the_server_runs_as_a_separate_process(tmp_path):
    db_path = tmp_path / "messages.db"
    InfluencerStore(str(db_path)).add_contacts([INFLUENCER], source=SOURCE_CONSOLE)
    config = mcp_config(INFLUENCER, str(db_path), nickname="小美")["mcpServers"]["influencer"]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "submit_sample_request", "arguments": FULL}},
    ]
    proc = subprocess.run(
        [config["command"], *config["args"]],
        input="\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n",
        capture_output=True, text=True, cwd=ROOT, timeout=60,
        env={**os.environ, "INFLUENCER_MOCK": "1"},
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2], proc.stderr
    submitted = json.loads(replies[1]["result"]["content"][0]["text"])
    assert submitted["submitted"] is True
    assert InfluencerStore(str(db_path)).get_approval(submitted["packet_id"]).status == GateStatus.PENDING
    assert PHONE not in proc.stderr, "日志里不能有完整电话"


def test_the_claude_config_allows_only_these_tools():
    config = mcp_config("wxid_a", "/tmp/x.db")
    server = config["mcpServers"]["influencer"]
    assert server["command"] == sys.executable
    assert server["env"]["PYTHONPATH"] == str(ROOT), "claude -p 不在项目目录里运行时也找得到 src"
    assert ALLOWED_TOOLS == (
        "mcp__influencer__check_inventory", "mcp__influencer__submit_sample_request",
        "mcp__influencer__get_request_status", "mcp__influencer__hand_over_to_operator",
    )
