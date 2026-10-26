"""小红书 CLI 适配器的真实调用路径（src/influencer/xhs_cli.py）。

CLI 是同事的千帆命令行工具（doupi-org/xiaohongshu-engineering），平时用 `node bin/xhs.mjs` 运行。
这里把 node 换成 /bin/sh、把 CLI 换成一个 shell 脚本，按 CLI 真实的输出格式（stdout 一份 JSON、
写命令的 items[].outcome 和 summary.overall、退出码 0 / 1 / 2 / 77）检查参数和结果的处理。
"""

import asyncio
import json
import sys

from pathlib import Path

import pytest

from src.influencer.xhs_cli import XHSCLIError, XiaohongshuCLIClient, plan_name, rate_text
from test_influencer_acceptance import FakeERP, FakeXHS, get_packet, make_handler, run, submit_full_request
from src.influencer.types import GateStatus

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="用 shell 脚本冒充 CLI")

ROOT = Path(__file__).resolve().parent.parent
BUYER = "5f0000000000000000000001"
PROFILE = f"https://www.xiaohongshu.com/user/profile/{BUYER}"
ITEM = "66aa00000000000000000001"


@pytest.fixture(autouse=True)
def no_mock_env(monkeypatch):
    monkeypatch.delenv("INFLUENCER_MOCK", raising=False)


def fake_cli(tmp_path, *, find=None, create=None, code_find=0, code_create=0, items=None):
    """find / create 是两个命令要打印的 JSON；每次调用的参数一行一条记在 calls.txt。"""
    find_json = json.dumps(find if find is not None else {"view": "find-buyer", "matches": 0, "buyers": []})
    create_json = json.dumps(create if create is not None else {
        "executed": True, "items": [{"targetKind": "direct-plan", "targetId": "GROUP-1", "outcome": "VERIFIED_SUCCESS"}],
        "summary": {"overall": "VERIFIED"},
    })
    (tmp_path / "find.json").write_text(find_json, encoding="utf-8")
    (tmp_path / "create.json").write_text(create_json, encoding="utf-8")
    script = tmp_path / "xhs.mjs"
    script.write_text(
        f'echo "$*" >> "{tmp_path}/calls.txt"\n'
        f'if [ "$1" = "direct-plans" ]; then cat "{tmp_path}/find.json"; exit {code_find}; fi\n'
        f'cat "{tmp_path}/create.json"; exit {code_create}\n',
        encoding="utf-8",
    )
    items_file = tmp_path / "xhs_items.json"
    items_file.write_text(json.dumps(items if items is not None else {"VC精华液": ITEM}), encoding="utf-8")
    return XiaohongshuCLIClient(cli_path=str(script), node_path="/bin/sh", items_file=str(items_file))


def calls(tmp_path):
    path = tmp_path / "calls.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def commission(client, account=PROFILE, rate=0.25):
    return asyncio.run(client.set_exclusive_commission(
        xhs_uid=account, sku_id="SKU-1", rate=rate, idempotency_key="GATE-ABC", product_name="VC精华液",
    ))


# ---- 绑佣金：定向计划 ----

def test_a_plan_is_created_for_the_buyer_in_the_profile_link(tmp_path):
    result = commission(fake_cli(tmp_path))
    assert result == {"success": True, "plan_id": "GROUP-1", "plan_name": "寄样-GATE-ABC"}
    assert calls(tmp_path) == [
        f"direct-plan --action create --name 寄样-GATE-ABC --buyer-id {BUYER} --item {ITEM}:25 --long-term --execute",
    ], "主页链接里有 user_id，不用再搜"


def test_a_xhs_number_is_looked_up_first(tmp_path):
    client = fake_cli(tmp_path, find={"matches": 1, "buyers": [{"user_id": BUYER, "fan_cnt": 20000}]})
    assert commission(client, account="12345678")["success"] is True
    assert calls(tmp_path)[0] == "direct-plans --find-buyer 12345678 --raw"
    assert f"--buyer-id {BUYER}" in calls(tmp_path)[1]


def test_a_xhs_number_that_matches_several_buyers_is_refused(tmp_path):
    two = {"buyers": [{"user_id": BUYER}, {"user_id": "5f0000000000000000000002"}]}
    with pytest.raises(XHSCLIError, match="找不到唯一"):
        commission(fake_cli(tmp_path, find=two), account="小美")
    assert not any(c.startswith("direct-plan ") for c in calls(tmp_path)), "没确定是谁就不建计划"


def test_a_state_conflict_is_a_failure_with_the_reason(tmp_path):
    conflict = {"items": [{"outcome": "STATE_CONFLICT", "detail": "a plan group named this already exists (G9)"}],
                "summary": {"overall": "REFUSED"}}
    result = commission(fake_cli(tmp_path, create=conflict, code_create=1))
    assert result["success"] is False
    assert "STATE_CONFLICT" in result["error"] and "already exists" in result["error"]


def test_an_uncertain_outcome_says_not_to_retry(tmp_path):
    unsure = {"items": [{"outcome": "INDETERMINATE", "detail": "no answer"}], "summary": {"overall": "UNCERTAIN"}}
    result = commission(fake_cli(tmp_path, create=unsure, code_create=1))
    assert result["success"] is False and "不要直接重试" in result["error"]


def test_an_expired_login_is_said_plainly(tmp_path):
    with pytest.raises(XHSCLIError, match="千帆登录失效"):
        commission(fake_cli(tmp_path, code_create=77))


def test_a_product_missing_from_the_mapping_is_caught_before_any_call(tmp_path):
    client = fake_cli(tmp_path, items={"别的产品": ITEM})
    result = commission(client)
    assert result["success"] is False and "对照表" in result["error"]
    assert calls(tmp_path) == []


def test_the_mapping_can_be_keyed_by_sku(tmp_path):
    client = fake_cli(tmp_path, items={"SKU-1": ITEM})
    assert client.item_id_for("VC精华液", "SKU-1") == ITEM


def test_the_template_placeholder_counts_as_not_filled_in(tmp_path):
    template = json.loads((ROOT / "docs" / "xhs_items.example.json").read_text(encoding="utf-8"))
    client = fake_cli(tmp_path, items=template)
    assert client.item_id_for("VC精华液", "SKU-1") == ""
    assert "对照表" in client.commission_problem("VC精华液", "SKU-1", PROFILE)


def test_a_short_link_cannot_be_used(tmp_path):
    problem = fake_cli(tmp_path).commission_problem("VC精华液", "SKU-1", "https://xhslink.com/a/AbC")
    assert "短链" in problem


@pytest.mark.parametrize("rate, text", [(0.25, "25"), (0.125, "12.5"), (0.1, "10"), (0.055, "5.5")])
def test_the_rate_is_a_percent_with_one_decimal(rate, text):
    assert rate_text(rate) == text


def test_the_plan_name_fits_the_limit():
    assert len(plan_name("GATE-" + "X" * 60)) == 35


# ---- 查达人 ----

def test_followers_come_from_the_buyer_search(tmp_path):
    client = fake_cli(tmp_path, find={"buyers": [{"user_id": BUYER, "nick_name": "小美", "fan_cnt": 23000}]})
    assert asyncio.run(client.get_creator_stats(PROFILE)) == {"follower_count": 23000, "user_id": BUYER}
    assert calls(tmp_path) == [f"direct-plans --find-buyer {BUYER} --raw"]


def test_a_search_with_no_match_means_unknown_not_zero(tmp_path):
    assert asyncio.run(fake_cli(tmp_path).get_creator_stats("12345678")) == {}


def test_a_short_link_is_not_searched(tmp_path):
    assert asyncio.run(fake_cli(tmp_path).get_creator_stats("https://xhslink.com/a/AbC")) == {}
    assert calls(tmp_path) == []


def test_output_that_is_not_json_is_an_error(tmp_path):
    script = tmp_path / "xhs.mjs"
    script.write_text("echo ok\n", encoding="utf-8")
    client = XiaohongshuCLIClient(cli_path=str(script), node_path="/bin/sh")
    with pytest.raises(XHSCLIError, match="不是 JSON"):
        asyncio.run(client.get_creator_stats("12345678"))


# ---- 状态 ----

def test_readiness_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("XHS_CLI_PATH", raising=False)
    assert "XHS_CLI_PATH" in XiaohongshuCLIClient(node_path="/bin/sh").readiness()["detail"]
    script = tmp_path / "bin" / "xhs.mjs"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    client = XiaohongshuCLIClient(cli_path=str(tmp_path), node_path="/bin/sh", items_file=str(tmp_path / "none.json"))
    assert client.cli_path == str(script), "给仓库目录就找 bin/xhs.mjs"
    assert "产品对照表" in client.readiness()["detail"]


# ---- 审批时：佣金绑不上就不建单 ----

class NoMappingXHS(FakeXHS):
    def commission_problem(self, product_name, sku_id, xhs_account):
        return "对照表里没有【VC精华液】"


def test_an_approval_stops_before_the_order_when_the_commission_cannot_be_set():
    erp = FakeERP()
    handler = make_handler(erp, NoMappingXHS())
    packet_id = submit_full_request(handler)
    result = run(handler.execute_operator_approval(packet_id, True))
    assert result["success"] is False and "对照表" in result["error"]
    assert erp.order_calls == [], "出库单不建"
    assert get_packet(handler, packet_id).status == GateStatus.PENDING, "补好对照表后还能再批"
