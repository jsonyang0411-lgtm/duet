"""Platform actions (src/prm/platforms.py): the whitelist and its checks, the CLIs' preview
and execution against a fake ``node`` that answers like the three platform CLIs, the
ledger that runs an approval once, the catalogue, and what the bot accepts back."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.prm import platforms
from src.prm.platforms import ActionError

from platform_fakes import (
    DRY_STDERR, DY_CHANNEL, DY_ITEM, DY_ITEM2, DY_ITEM3, DY_LINK, DY_SKU, DY_SKU2, DY_SKU2B, DY_SKU3, DY_UID, JST_ORDER, JST_SHOP_DY, JST_SHOP_XHS, JST_UID,
    TB_CAMPAIGN, TB_CROWD, TB_ITEM, TB_PRECHECK, XHS_BUYER, XHS_ITEM, XHS_SKU, FakeNode, catalog, dy_bind_done,
    dy_bind_preview, dy_create_done, dy_create_preview, dy_designated_done, dy_designated_preview, dy_promotion,
    jst_blocked, jst_done, jst_preview, rule, tb_done, tb_row, xhs_buyers, xhs_channel_done, xhs_channel_preview,
    xhs_plan,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = "a" * 32

DESIGNATED = {"action": "dy.designated_commission", "item_id": DY_ITEM, "creator_platform_id": DY_UID,
              "commission_rate": 20, "note": "kb/合作政策.md：腰部达人 20%"}
CHANNEL = {"action": "dy.channel_goods", "item_id": DY_ITEM, "creator_platform_id": DY_UID, "price": "15"}
PLAN = {"action": "xhs.direct_plan", "item_id": XHS_ITEM, "creator_platform_id": "小美的小红书号", "commission_rate": 20}
XCHANNEL = {"action": "xhs.channel_item", "item_id": XHS_ITEM, "sku_prices": [{"sku_id": XHS_SKU, "price": "79"}]}
CAMPAIGN = {"action": "tb.campaign", "item_id": TB_ITEM, "crowd_ids": [TB_CROWD], "bid_target": "display_pay",
            "day_budget": 50}
RECEIVER = {"name": "张三", "phone": "〈手机1〉", "province": "广东省", "city": "佛山市", "district": "南海区",
            "address": "某某路 100 号 1 栋 101"}
SAMPLE = {"action": "jst.sample_order", "shop": "dy", "item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}],
          "creator_platform": "dy", "creator_uid": DY_UID, "receiver": RECEIVER}


@pytest.fixture
def node(tmp_path, monkeypatch):
    return FakeNode(tmp_path / "mac", monkeypatch)


@pytest.fixture
def cfg(node):
    return platforms.config(node.settings())


def argvs(node, *needles):
    """The logged command lines that hold every needle (``--execute`` counts as written)."""
    return [c["argv"] for c in node.calls() if all(n in c["argv"] for n in needles)]


# ── the whitelist and its checks ──────────────────────────────────────────

def test_each_action_is_normalised_and_typed():
    assert platforms.check_action(DESIGNATED) == {
        "action": "dy.designated_commission", "item_id": DY_ITEM, "creator_platform_id": DY_UID,
        "commission_rate": "20", "note": "kb/合作政策.md：腰部达人 20%"}
    assert platforms.check_action({**PLAN, "commission_rate": 12.5})["commission_rate"] == "12.5"
    assert platforms.check_action({**PLAN, "commission_rate": "20.0"})["commission_rate"] == "20"
    assert platforms.check_action(CHANNEL) == {"action": "dy.channel_goods", "item_id": DY_ITEM,
                                               "creator_platform_id": DY_UID, "exclusive": False, "price": "15.00"}
    assert platforms.check_action(XCHANNEL)["sku_prices"] == [{"sku_id": XHS_SKU, "price": "79.00"}]
    assert platforms.check_action(CAMPAIGN)["crowd_ids"] == [TB_CROWD]


@pytest.mark.parametrize("raw, code", [
    ("not an object", "not_object"),
    ({"action": "xhs.coupon", "item_id": XHS_ITEM}, "unknown_action"),
    ({**DESIGNATED, "command": "rm -rf /"}, "extra_param"),
    ({**DESIGNATED, "price": "1"}, "extra_param"),                      # not a parameter of this action
    ({k: v for k, v in DESIGNATED.items() if k != "commission_rate"}, "missing_param"),
    ({**DESIGNATED, "item_id": "--execute"}, "bad_item"),
    ({**DESIGNATED, "item_id": f"{DY_ITEM} --execute"}, "bad_item"),
    ({**DESIGNATED, "item_id": "0123456"}, "bad_item"),
    ({**DESIGNATED, "creator_platform_id": "dy_abc123"}, "bad_creator"),
    ({**DESIGNATED, "commission_rate": 20.5}, "bad_rate"),               # whole percents only on 百应
    ({**DESIGNATED, "commission_rate": 0}, "bad_rate"),
    ({**DESIGNATED, "commission_rate": True}, "bad_rate"),
    ({**DESIGNATED, "commission_rate": 60}, "over_limit"),               # the Mac's own cap (50%)
    ({**PLAN, "commission_rate": 12.55}, "bad_rate"),
    ({**PLAN, "commission_rate": 81}, "bad_rate"),
    ({**PLAN, "creator_platform_id": "--execute"}, "bad_creator"),
    ({**PLAN, "creator_platform_id": "小美\n--execute"}, "bad_creator"),
    ({**PLAN, "item_id": "64f1a2b3"}, "bad_item"),
    ({**XCHANNEL, "sku_prices": [{"sku_id": XHS_SKU, "price": "7.999"}]}, "bad_price"),
    ({**XCHANNEL, "sku_prices": [{"sku_id": XHS_SKU, "price": "0"}]}, "bad_price"),
    ({**XCHANNEL, "sku_prices": [{"sku_id": XHS_SKU, "price": "9"}, {"sku_id": XHS_SKU, "price": "8"}]}, "bad_prices"),
    ({**XCHANNEL, "title": "-x 防晒霜"}, "bad_title"),
    ({**XCHANNEL, "title": "防晒霜 {链接}"}, "bad_title"),
    ({**CHANNEL, "exclusive": True}, "bad_stock"),                       # 专属达人带货 cannot share the stock
    ({**CHANNEL, "stock": -1}, "bad_stock"),
    ({**CHANNEL, "sku_ids": [DY_SKU, DY_SKU]}, "bad_sku"),
    ({**CAMPAIGN, "day_budget": 301}, "over_limit"),
    ({**CAMPAIGN, "day_budget": "50"}, "bad_budget"),
    ({**CAMPAIGN, "bid_target": "display_roi"}, "bad_bid_target"),
    ({**CAMPAIGN, "crowd_ids": []}, "missing_param"),
    ({**CAMPAIGN, "crowd_ids": ["abc"]}, "bad_crowd"),
])
def test_what_does_not_fit_is_refused(raw, code):
    with pytest.raises(ActionError) as e:
        platforms.check_action(raw)
    assert e.value.code == code


def test_the_mac_can_raise_or_lower_its_limits():
    assert platforms.check_action({**DESIGNATED, "commission_rate": 60}, limits={"max_commission_rate": 70})
    with pytest.raises(ActionError):
        platforms.check_action(DESIGNATED, limits={"max_commission_rate": 10})


def test_the_catalogue_holds_items_skus_and_prices():
    cat = catalog()
    assert platforms.check_action(DESIGNATED, catalog=cat)["item_id"] == DY_ITEM
    with pytest.raises(ActionError) as e:
        platforms.check_action({**DESIGNATED, "item_id": "3000000000000000999"}, catalog=cat)
    assert e.value.code == "not_in_catalog"
    with pytest.raises(ActionError) as e:                   # a decimal point in the wrong place
        platforms.check_action({**CHANNEL, "price": "1.99"}, catalog=cat)
    assert e.value.code == "price_too_low"
    with pytest.raises(ActionError) as e:
        platforms.check_action({**CHANNEL, "sku_ids": ["3100000000000000999"]}, catalog=cat)
    assert e.value.code == "bad_sku"
    with pytest.raises(ActionError) as e:
        platforms.check_action({**XCHANNEL, "sku_prices": [{"sku_id": XHS_ITEM, "price": "79"}]}, catalog=cat)
    assert e.value.code == "bad_sku"
    assert platforms.check_action({**CHANNEL, "price": "1.99"}, catalog=cat, limits={"min_price_ratio": 0.05})


def test_the_schema_is_the_drafts_optional_field():
    from src.prm.result import SCHEMA

    assert SCHEMA["properties"]["platform_action"] is platforms.ACTION_SCHEMA
    assert "platform_action" not in SCHEMA["required"]
    assert set(platforms.ACTION_SCHEMA["properties"]["action"]["enum"]) == set(platforms.ACTIONS)
    params = {p for spec in platforms.ACTIONS.values() for p in (*spec["required"], *spec["optional"])}
    assert params | {"action", "note"} == set(platforms.ACTION_SCHEMA["properties"])


# ── replies ───────────────────────────────────────────────────────────────

def test_a_reply_uses_only_its_actions_placeholders():
    channel = platforms.check_action(CHANNEL)
    designated = platforms.check_action(DESIGNATED)
    plan = platforms.check_action(PLAN)
    assert platforms.reply_problems("链接在这：{链接}，渠道品 {渠道品ID}", channel) == []
    assert platforms.reply_problems("好的", channel) == []
    assert platforms.reply_problems("", channel)
    assert platforms.reply_problems("佣金设好了 {链接}", designated)
    assert platforms.reply_problems("你好 {姓名}", channel)
    assert platforms.reply_problems("计划「{计划名}」开好了", plan) == []
    assert platforms.reply_problems("计划「{计划名}」：{链接}", plan)
    assert platforms.fill_reply("链接在这：{链接} 有问题找我", channel, {"link": DY_LINK}) == (
        f"链接在这：{DY_LINK} 有问题找我", [])
    assert platforms.fill_reply("链接发你啦", channel, {"link": DY_LINK}) == (f"链接发你啦\n{DY_LINK}", [])
    assert platforms.fill_reply("设好啦", designated, {"link": DY_LINK}) == ("设好啦", [])
    assert platforms.fill_reply("计划「{计划名}」编号 {计划ID}", plan, {"plan_name": "达人合作7-1", "group_id": "g1"}) == (
        "计划「达人合作7-1」编号 g1", [])
    assert platforms.fill_reply("计划编号 {计划ID}", plan, {"plan_name": "达人合作7-1"}) == ("计划编号 {计划ID}", ["{计划ID}"])


def test_a_link_must_be_the_platforms_own():
    assert platforms.check_link(DY_LINK, "dy") == DY_LINK
    assert platforms.check_link("haohuo.jinritemai.com/x?id=1", "dy") == "https://haohuo.jinritemai.com/x?id=1"
    for bad in ("https://evil-jinritemai.com/x", "https://jinritemai.com.evil.cn/x", "javascript:alert(1)",
                "https://haohuo.jinritemai.com/x y", "https://haohuo.jinritemai.com/{链接}"):
        assert platforms.check_link(bad, "dy") == ""
    assert platforms.check_link(DY_LINK, "xhs") == ""


# ── settings ──────────────────────────────────────────────────────────────

def test_a_platform_is_usable_only_with_its_cli_and_node(node, tmp_path):
    cfg = platforms.config(node.settings(tb=str(tmp_path / "nowhere"), max_commission_rate=30))
    assert set(cfg["dirs"]) == {"xhs", "dy", "jst"} and "tb" in cfg["missing"] and cfg["enabled"]
    assert cfg["limits"]["max_commission_rate"] == 30
    assert platforms.config({})["enabled"] is False
    assert platforms.find_node(str(node.path)) == str(node.path)


# ── phase 1: the preview ──────────────────────────────────────────────────

def test_designated_commission_preview_reads_and_never_executes(node, cfg):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog(), context={"party_id": 7})
    assert block["verdict"] == "ready", block
    assert block["run"] == {"steps": ["designated"]}
    assert block["catalog"]["title"] == "测试主品"
    (argv,) = argvs(node, "promotion-strategy-create-designated")
    assert argv[0] == "bin/dy.mjs" and "--execute" not in argv
    assert argv[1:] == ["buyin", "promotion-strategy-create-designated", f"--id={DY_ITEM}", f"--uid={DY_UID}",
                        "--to=20", "--end=long"]
    facts = " ".join(f["value"] for f in block["steps"][0]["facts"])
    assert "fixed commission 20%" in facts and "可以加" in facts and "底线 5%" in facts


def test_a_product_not_yet_promoted_gets_a_promotion_first(node, cfg):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(unlocated=True), execute=False),
                rule(["promotion-create"], dy_promotion(), execute=False)])
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog())
    assert block["verdict"] == "ready"
    assert block["run"] == {"steps": ["promotion", "designated"]}
    assert "promotion-create" in " ".join(f["value"] for f in block["facts"])


@pytest.mark.parametrize("answer, verdict", [
    (dict(stdout=dy_designated_preview(blocked="status 4 (抖店下架)")), "blocked"),
    (dict(stdout=dy_designated_preview(addable=False)), "blocked"),
    (dict(stderr="[NOT_LOGGED_IN] the 百应 (buyin) tab is on a login page; nothing was requested\n", exit=77), "login"),
    (dict(stderr="[RISK_CONTROL] verification wall\n", exit=76), "wall"),
    (dict(stderr="[IDENTITY_MISMATCH] this login is not the one expected\n", exit=79), "wrong_account"),
    (dict(stderr="[SCHEMA_MISMATCH] the answer changed shape\n", exit=1), "error"),
    (dict(stdout="not json"), "error"),
])
def test_what_stops_a_preview_is_its_verdict(node, cfg, answer, verdict):
    node.rules([rule(["promotion-strategy-create-designated"], execute=False, **answer)])
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog())
    assert block["verdict"] == verdict
    assert block["problems"]


def test_channel_goods_preview_shows_prices_and_stops_on_the_price_check(node, cfg):
    node.rules([rule(["channel-goods-create"], dy_create_preview(), execute=False)])
    block = platforms.preview(CHANNEL, cfg, catalog=catalog())
    assert block["verdict"] == "ready" and block["run"] == {"steps": ["create", "bind"]}
    (argv,) = argvs(node, "channel-goods-create")
    assert argv[2:] == ["channel-goods-create", f"--product-id={DY_ITEM}", "--sale-channel=kol", "--price=15.00"]
    facts = " ".join(f["value"] for f in block["steps"][0]["facts"])
    assert "主品价 19.99 元 → 渠道价 15.00" in facts and "达人带货" in facts
    node.rules([rule(["channel-goods-create"], dy_create_preview(price_kind="warn"), execute=False)])
    assert platforms.preview(CHANNEL, cfg, catalog=catalog())["verdict"] == "blocked"
    node.rules([rule(["channel-goods-create"], dy_create_preview(blockers=["主商品不在售"]), execute=False)])
    blocked = platforms.preview(CHANNEL, cfg, catalog=catalog())
    assert blocked["verdict"] == "blocked" and "主商品不在售" in blocked["problems"]


def test_the_shop_check_goes_with_every_dy_command(node):
    cfg = platforms.config(node.settings(dy_expect_shop_id="1234567"))
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    platforms.preview(DESIGNATED, cfg, catalog=catalog())
    assert "--expect-shop-id=1234567" in argvs(node, "promotion-strategy-create-designated")[0]


def test_direct_plan_finds_the_buyer_then_runs_the_read_only_full_run(node, cfg):
    node.rules([rule(["--find-buyer=小美的小红书号", "--raw"], xhs_buyers((XHS_BUYER, "小美"))),
                rule(["tools/dry-execute.mjs", "direct-plan"], xhs_plan(), stderr=DRY_STDERR)])
    block = platforms.preview(PLAN, cfg, catalog=catalog(), context={"party_id": 7})
    assert block["verdict"] == "ready", block
    assert block["run"]["buyer_id"] == XHS_BUYER and block["run"]["name"].startswith("达人合作7-")
    dry = argvs(node, "tools/dry-execute.mjs")[0]
    assert "--execute" not in dry and f"--buyer-id={XHS_BUYER}" in dry and f"--item={XHS_ITEM}:20" in dry
    assert "--long-term" in dry and f"--name={block['run']['name']}" in dry
    facts = " ".join(f["value"] for s in block["steps"] for f in s["facts"])
    assert "小美（粉丝 12000" in facts and "佣金率 20%" in facts


@pytest.mark.parametrize("buyers", [(), ((XHS_BUYER, "小美"), ("5e9a8b7c6d5e4f3a2b1c0d9f", "小美呀"))])
def test_a_buyer_that_cannot_be_told_apart_stops_the_preview(node, cfg, buyers):
    node.rules([rule(["--raw"], xhs_buyers(*buyers))])
    block = platforms.preview(PLAN, cfg, catalog=catalog())
    assert block["verdict"] == "blocked" and "userId" in " ".join(block["problems"])
    assert not argvs(node, "tools/dry-execute.mjs")


def test_a_full_run_that_refused_no_write_is_not_ready(node, cfg):
    node.rules([rule(["tools/dry-execute.mjs"], xhs_plan("INDETERMINATE", detail="no answer"),
                     stderr="[DRY-EXECUTE] reads sent: 3; writes refused: 0\n")])
    block = platforms.preview({**PLAN, "creator_platform_id": XHS_BUYER}, cfg, catalog=catalog())
    assert block["verdict"] != "ready" and "INDETERMINATE" in " ".join(block["problems"])


def test_a_user_id_needs_no_lookup_and_the_pages_checks_block(node, cfg):
    node.rules([rule(["tools/dry-execute.mjs"], xhs_plan("STATE_CONFLICT", detail="nothing was sent: 最低佣金率不得低于1%"),
                     stderr="[DRY-EXECUTE] reads sent: 4; writes refused: 0\n")])
    block = platforms.preview({**PLAN, "creator_platform_id": XHS_BUYER}, cfg, catalog=catalog())
    assert block["verdict"] == "blocked" and "最低佣金率" in " ".join(block["problems"])
    assert not argvs(node, "--raw")


def test_a_login_page_during_the_full_run_is_a_login(node, cfg):
    stopped = {"code": "NOT_LOGGED_IN", "message": "[NOT_LOGGED_IN] the 小红书千帆 (ark) tab is on a login page"}
    node.rules([rule(["tools/dry-execute.mjs"], xhs_plan("FAILED", detail="not sent: the current state could not be read",
                                                         stopped=stopped), stderr="[DRY-EXECUTE] reads sent: 0; writes refused: 0\n")])
    assert platforms.preview({**PLAN, "creator_platform_id": XHS_BUYER}, cfg, catalog=catalog())["verdict"] == "login"


def test_channel_item_preview_reads_the_form(node, cfg):
    node.rules([rule(["channel-item", "--read-current"], xhs_channel_preview())])
    block = platforms.preview(XCHANNEL, cfg, catalog=catalog())
    assert block["verdict"] == "ready"
    (argv,) = argvs(node, "channel-item")
    assert argv[1:] == ["channel-item", "--action=create", f"--main-item={XHS_ITEM}", f"--sku-price={XHS_SKU}:79.00",
                        "--shelf=on", "--read-current"]
    assert "专属价 79.00" in " ".join(f["value"] for f in block["steps"][0]["facts"])
    node.rules([rule(["--read-current"], xhs_channel_preview(blocked=["主商品的渠道商品数量已达上限"]))])
    assert platforms.preview(XCHANNEL, cfg, catalog=catalog())["verdict"] == "blocked"
    node.rules([rule(["--read-current"], xhs_channel_preview(problem="售价超出范围"))])
    assert platforms.preview(XCHANNEL, cfg, catalog=catalog())["verdict"] == "blocked"
    node.rules([rule(["--read-current"], stderr="[NOT_LOGGED_IN] tab is on a login page\n", exit=77)])
    assert platforms.preview(XCHANNEL, cfg, catalog=catalog())["verdict"] == "login"


def test_campaign_preview_and_its_yaml_refusal(node, cfg):
    node.rules([rule(["create-display-custom-campaign"], [tb_row()], execute=False)])
    block = platforms.preview(CAMPAIGN, cfg, catalog=catalog(), context={"party_id": 7})
    assert block["verdict"] == "ready" and block["run"]["name"].startswith("达人合作7_")
    (argv,) = argvs(node, "create-display-custom-campaign")
    assert argv[0] == "bin/oc.mjs" and argv[1] == "alimama"
    assert f"--item-title=连衣裙 夏季" in argv and "--transport=http" in argv and "--format=json" in argv
    assert "--pause-after-create=true" in argv and "--execute=true" not in argv and "--execute" not in argv
    node.rules([rule(["create-display-custom-campaign"], stderr=TB_PRECHECK, exit=2, execute=False)])
    blocked = platforms.preview(CAMPAIGN, cfg, catalog=catalog())
    assert blocked["verdict"] == "blocked" and "商品已下架" in " ".join(blocked["problems"])
    node.rules([rule(["create-display-custom-campaign"], stderr="ok: false\nerror:\n  code: AUTH_REQUIRED\n"
                                                                "  message: '[NOT_LOGGED_IN] …'\n  exitCode: 77\n",
                     exit=77, execute=False)])
    assert platforms.preview(CAMPAIGN, cfg, catalog=catalog())["verdict"] == "login"


def test_the_tb_shop_goes_before_the_site(node):
    cfg = platforms.config(node.settings(tb_shop="店A"))
    node.rules([rule(["create-display-custom-campaign"], [tb_row()], execute=False)])
    platforms.preview(CAMPAIGN, cfg, catalog=catalog())
    argv = argvs(node, "create-display-custom-campaign")[0]
    assert argv[:3] == ["bin/oc.mjs", "--shop=店A", "alimama"]


def test_no_catalogue_or_no_cli_means_no_preview(node, cfg):
    block = platforms.preview(DESIGNATED, cfg, catalog=None)
    assert block["verdict"] == "invalid" and "--sync-products" in block["problems"][0]
    block = platforms.preview(DESIGNATED, platforms.config(node.settings(dy="")), catalog=catalog())
    assert block["verdict"] == "unavailable"
    block = platforms.preview({**DESIGNATED, "commission_rate": 99}, cfg, catalog=catalog())
    assert block["verdict"] == "invalid" and block["action"] is None and block["requested"]["commission_rate"] == "99"
    assert node.calls() == []


# ── phase 2: execution, once ──────────────────────────────────────────────

def execute(cfg, raw, plan, tmp_path, key=KEY, cat=None):
    return platforms.execute(raw, cfg, plan=plan, key=key, ledger=tmp_path / "ledger",
                             catalog=catalog() if cat is None else cat, context={"packet_id": 3})


def test_channel_goods_runs_create_then_bind_and_hands_back_the_link(node, cfg, tmp_path):
    node.rules([rule(["channel-goods-create"], dy_create_done(), execute=True),
                rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
                rule(["channel-goods-bind"], dy_bind_done(), execute=True)])
    out = execute(cfg, CHANNEL, {"steps": ["create", "bind"]}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS", out
    assert out["created"]["link"] == DY_LINK and out["created"]["channel_id"] == DY_CHANNEL
    names = [(s["name"], s["execute"]) for s in out["steps"]]
    assert names == [("create", True), ("bind", False), ("bind", True)]
    bind = argvs(node, "channel-goods-bind", "--execute")[0]
    assert f"--channel-id={DY_CHANNEL}" in bind and f"--uid={DY_UID}" in bind
    ledger = json.loads((tmp_path / "ledger" / f"{KEY}.json").read_text(encoding="utf-8"))
    assert ledger["status"] == "done" and ledger["record"]["outcome"] == "VERIFIED_SUCCESS" and ledger["packet_id"] == 3
    assert oct((tmp_path / "ledger" / f"{KEY}.json").stat().st_mode & 0o777) == "0o600"


def test_an_approval_key_is_run_once(node, cfg, tmp_path):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    first = execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)
    again = execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)
    assert first["outcome"] == "VERIFIED_SUCCESS" and first["created"]["strategy_ids"] == ["7600000000000000001"]
    assert again["outcome"] == "ALREADY_RUN" and again["earlier"]["outcome"] == "VERIFIED_SUCCESS"
    assert len(argvs(node, "--execute")) == 1


def test_a_key_taken_by_a_run_that_died_is_not_run_again(node, cfg, tmp_path):
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / f"{KEY}.json").write_text(json.dumps({"key": KEY, "status": "started"}), encoding="utf-8")
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    assert execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)["outcome"] == "ALREADY_RUN"
    assert node.calls() == []


@pytest.mark.parametrize("create, bind_preview, outcome, bind_calls", [
    (dy_create_done("ACCEPTED_UNVERIFIED"), None, "ACCEPTED_UNVERIFIED", 0),     # not verified: no bind, no reply
    (dy_create_done("INDETERMINATE"), None, "INDETERMINATE", 0),
    (dy_create_done(), dy_bind_preview(blocked=True), "PARTIAL", 1),             # previewed, not written
    (dy_create_done(), dy_bind_preview(already=True), "VERIFIED_SUCCESS", 1),
    (dy_create_done(link="https://evil.example.com/x"), None, "PARTIAL", 0),
])
def test_channel_goods_stops_at_the_first_step_that_is_not_verified(node, cfg, tmp_path, create, bind_preview,
                                                                    outcome, bind_calls):
    rules = [rule(["channel-goods-create"], create, execute=True)]
    rules.append(rule(["channel-goods-bind"], bind_preview if bind_preview is not None else dy_bind_preview(),
                      execute=False))
    rules.append(rule(["channel-goods-bind"], dy_bind_done(), execute=True))
    node.rules(rules)
    out = execute(cfg, CHANNEL, {"steps": ["create", "bind"]}, tmp_path)
    assert out["outcome"] == outcome, out
    assert len(argvs(node, "channel-goods-bind")) == bind_calls
    assert len(argvs(node, "channel-goods-bind", "--execute")) == 0
    if outcome != "VERIFIED_SUCCESS":
        assert out["problems"] or any(s["problems"] for s in out["steps"])


def test_promotion_first_then_a_fresh_preview_then_the_commission(node, cfg, tmp_path):
    node.rules([rule(["promotion-create"], dy_promotion("VERIFIED_SUCCESS"), execute=True),
                rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False),
                rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    out = execute(cfg, DESIGNATED, {"steps": ["promotion", "designated"]}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS"
    assert [(s["name"], s["execute"]) for s in out["steps"]] == [("promotion", True), ("designated", False),
                                                                 ("designated", True)]
    node.rules([rule(["promotion-create"], dy_promotion("FAILED"), execute=True),
                rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    out = execute(cfg, DESIGNATED, {"steps": ["promotion", "designated"]}, tmp_path, key="b" * 32)
    assert out["outcome"] == "FAILED" and len(argvs(node, "promotion-strategy-create-designated", "--execute")) == 1


def test_direct_plan_runs_with_the_previewed_buyer_and_name(node, cfg, tmp_path):
    node.rules([rule(["direct-plan"], xhs_plan("VERIFIED_SUCCESS", target="6a0000000000000000000a01"), execute=True)])
    plan = {"steps": ["create"], "buyer_id": XHS_BUYER, "name": "达人合作7-1026101500"}
    out = execute(cfg, PLAN, plan, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS"
    assert out["created"] == {"group_id": "6a0000000000000000000a01", "plan_name": "达人合作7-1026101500",
                              "task_id": "30000001"}
    argv = argvs(node, "--execute")[0]
    assert argv[0] == "bin/xhs.mjs" and f"--buyer-id={XHS_BUYER}" in argv and "--name=达人合作7-1026101500" in argv


def test_a_channel_item_is_never_more_than_accepted_unverified(node, cfg, tmp_path):
    node.rules([rule(["channel-item"], xhs_channel_done(), execute=True, exit=1)])
    out = execute(cfg, XCHANNEL, {"steps": ["create"]}, tmp_path)
    assert out["outcome"] == "ACCEPTED_UNVERIFIED" and out["created"]["item_id"] == "6a0000000000000000000b02"
    assert "form constants" in " ".join(f["value"] for f in out["steps"][0]["facts"])


def test_campaign_needs_the_create_and_the_pause_verified(node, cfg, tmp_path):
    node.rules([rule(["create-display-custom-campaign"], tb_done(), execute=True)])
    out = execute(cfg, CAMPAIGN, {"steps": ["create"], "name": "达人合作7_10261015"}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS" and out["created"]["campaign_id"] == TB_CAMPAIGN
    argv = argvs(node, "--execute=true")[0]
    assert "--pause-after-create=true" in argv and "--campaign-name=达人合作7_10261015" in argv
    node.rules([rule(["create-display-custom-campaign"], tb_done("ACCEPTED_UNVERIFIED"), execute=True)])
    assert execute(cfg, CAMPAIGN, {"steps": ["create"], "name": "x_1"}, tmp_path, key="c" * 32)["outcome"] == \
        "ACCEPTED_UNVERIFIED"
    node.rules([rule(["create-display-custom-campaign"], tb_done(pause="FAILED"), execute=True)])
    assert execute(cfg, CAMPAIGN, {"steps": ["create"], "name": "x_2"}, tmp_path, key="d" * 32)["outcome"] == "FAILED"


@pytest.mark.parametrize("answer, outcome", [
    (dict(stderr="[NOT_LOGGED_IN] the 百应 tab is on a login page; nothing was requested\n", exit=77), "FAILED"),
    (dict(stderr="[USAGE] --to must be a whole number\n", exit=2), "FAILED"),
    (dict(stderr="[INTERRUPTED] stopped; a write request of this run was in flight\n", exit=130), "INDETERMINATE"),
    (dict(stderr="[INTERNAL] TypeError at …\n", exit=1), "INDETERMINATE"),
    (dict(stdout=dy_designated_done("INDETERMINATE"), exit=1), "INDETERMINATE"),
    (dict(stdout=dy_designated_done("STATE_CONFLICT"), exit=1), "STATE_CONFLICT"),
])
def test_an_execution_that_is_not_verified_says_so(node, cfg, tmp_path, answer, outcome):
    node.rules([rule(["promotion-strategy-create-designated"], execute=True, **answer)])
    out = execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)
    assert out["outcome"] == outcome


def test_a_write_that_hangs_is_stopped_at_its_timeout_and_unknown(node, tmp_path):
    cfg = platforms.config(node.settings(execute_timeout_sec=30))
    cfg["execute_timeout_sec"] = 1                      # the floor of the settings is 30 s
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True, sleep=10)])
    started = time.monotonic()
    out = execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)
    assert out["outcome"] == "INDETERMINATE" and time.monotonic() - started < 9


@pytest.mark.parametrize("plan", [None, {"steps": ["bind"]}, {"steps": ["create", "bind", "create"]},
                                  {"steps": ["designated", "designated"]}])
def test_only_an_allowed_plan_runs(node, cfg, tmp_path, plan):
    out = execute(cfg, DESIGNATED, plan, tmp_path)
    assert out["outcome"] == "NOT_RUN" and node.calls() == []
    assert not (tmp_path / "ledger" / f"{KEY}.json").exists()


def test_the_mac_checks_the_action_again(node, cfg, tmp_path):
    out = execute(cfg, {**DESIGNATED, "item_id": "3000000000000000999"}, {"steps": ["designated"]}, tmp_path)
    assert out["outcome"] == "NOT_RUN" and "商品目录" in out["problems"][0]
    out = platforms.execute(DESIGNATED, cfg, plan={"steps": ["designated"]}, key=KEY, ledger=tmp_path / "ledger",
                            catalog=None)
    assert out["outcome"] == "NOT_RUN"
    with pytest.raises(ActionError):
        platforms.claim(tmp_path / "ledger", "../../etc/passwd", {})
    assert execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path, key="../x")["outcome"] == "NOT_RUN"
    assert node.calls() == []


# ── running a CLI ─────────────────────────────────────────────────────────

def test_a_preview_stops_when_its_job_is_cancelled_a_write_does_not(tmp_path):
    script = [sys.executable, "-c", "import time; time.sleep(3); print('{\"done\": true}')"]
    beats = []
    run = platforms.run_cli(script, tmp_path, timeout=30, heartbeat=lambda: beats.append(1) or True, every=0.2,
                            interruptible=True)
    assert run.stopped == "cancelled" and beats
    run = platforms.run_cli(script, tmp_path, timeout=30, heartbeat=lambda: True, every=0.2, interruptible=False)
    assert run.stopped == "" and run.exit_code == 0 and json.loads(run.stdout) == {"done": True}


def test_the_command_line_is_an_argv_list(tmp_path):
    marker = tmp_path / "pwned"
    run = platforms.run_cli([sys.executable, "-c", "import sys; print(sys.argv[1:])", f"--title=x; touch {marker}"],
                            tmp_path, timeout=30)
    assert "touch" in run.stdout and not marker.exists()


def test_yaml_errors_are_read():
    err = platforms.yaml_error(TB_PRECHECK)
    assert err["code"] == "CREATE_PRECHECK_NOT_ALLOWED" and err["exitCode"] == "2" and "商品已下架" in err["message"]
    assert platforms.yaml_error('ok: false\nerror:\n  code: TIMEOUT\n  message: "it\'s late"\n') == {
        "code": "TIMEOUT", "message": "it's late"}
    assert platforms.yaml_error("just text") is None


# ── the bot's side ────────────────────────────────────────────────────────

def test_the_bot_checks_what_comes_back(node, cfg):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog())
    clean = platforms.check_block(json.loads(json.dumps(block)))
    assert clean["verdict"] == "ready" and clean["action"]["item_id"] == DY_ITEM and clean["run"] == block["run"]
    with pytest.raises(ActionError):
        platforms.check_block({**block, "run": {"steps": ["designated", "designated"]}})
    with pytest.raises(ActionError):
        platforms.check_block({**block, "action": None})
    with pytest.raises(ActionError):
        platforms.check_block({**block, "action": {**DESIGNATED, "commission_rate": 99}})
    with pytest.raises(ActionError):
        platforms.check_block({"verdict": "great"})
    assert platforms.check_block({**block, "verdict": "blocked", "run": None})["run"] == {}
    huge = platforms.check_block({**block, "steps": [{**block["steps"][0], "raw": "x" * 100000}]})
    assert len(huge["steps"][0]["raw"]) <= platforms.MAX_RAW_CHARS + 40


def test_the_bot_holds_a_macs_action_to_the_form_not_to_the_macs_own_limits(node):
    cfg = platforms.config(node.settings(max_commission_rate=70, max_day_budget=2000))
    raised = {**DESIGNATED, "commission_rate": 60}
    action = platforms.check_action(raised, limits=cfg["limits"])
    block = {"version": 1, "action": action, "verdict": "ready", "run": {"steps": ["designated"]}}
    assert platforms.check_block(block)["action"]["commission_rate"] == "60"
    assert platforms.check_block({**block, "action": platforms.check_action({**CAMPAIGN, "day_budget": 2000},
                                                                         limits=cfg["limits"]),
                                  "run": {"steps": ["create"], "name": "x_1"}})["verdict"] == "ready"
    with pytest.raises(ActionError):                    # the platform's own range still holds
        platforms.check_block({**block, "action": {**action, "commission_rate": "81"}})


def test_the_bot_keeps_only_a_platforms_own_link():
    rec = {**platforms.record("VERIFIED_SUCCESS"), "created": {"link": DY_LINK, "channel_id": DY_CHANNEL}}
    assert platforms.check_record(rec)["created"] == {"link": DY_LINK, "channel_id": DY_CHANNEL}
    rec["created"]["link"] = "https://phish.example.com/x"
    assert "link" not in platforms.check_record(rec)["created"]
    with pytest.raises(ActionError):
        platforms.check_record({"op": "platform_execute", "outcome": "GREAT"})
    with pytest.raises(ActionError):
        platforms.check_record({"op": "purge", "outcome": "VERIFIED_SUCCESS"})


def test_an_unpreviewed_action_is_shown_but_cannot_run():
    block = platforms.unpreviewed_block(DESIGNATED)
    assert block["verdict"] == "unavailable" and block["action"]["item_id"] == DY_ITEM
    assert platforms.check_block(block)["verdict"] == "unavailable"
    bad = platforms.unpreviewed_block({"action": "rm -rf"})
    assert bad["action"] is None and bad["requested"] == {"action": "rm -rf"}


# ── the catalogue ─────────────────────────────────────────────────────────

def test_the_catalogue_is_read_from_the_three_platforms(node, cfg, tmp_path):
    other = "64f1a2b3c4d5e6f7a8b9c0e1"
    node.rules([
        rule(["items", "--tab=shelf"], {"site": "ark", "complete": True, "items": [
            {"item_id": XHS_ITEM, "item_name": "防晒霜", "min_price": 9900}, {"item_id": other, "item_name": "面膜 | 补水", "min_price": 5900}]}),
        rule(["item", f"--item-id={XHS_ITEM},{other}"], {"site": "ark", "notFound": [], "items": [
            {"item": {"item_id": XHS_ITEM}, "skus": [{"skuId": XHS_SKU, "skuName": "50ml", "price": {"price": 9900}, "available": True}]}]}),
        rule(["fxg", "products"], {"site": "fxg", "command": "products", "complete": True, "rows": [
            {"productId": DY_ITEM, "name": "测试主品", "status": 0, "checkStatus": 3, "priceLower": 1999, "discountPrice": 0}]}),
        rule(["fxg", "sku-prices"], {"site": "fxg", "command": "sku-prices", "rows": [
            {"productId": DY_ITEM, "skus": [{"skuId": DY_SKU, "name": "红色/M", "price": 1999, "priceYuan": "19.99"}]}]}),
        rule(["alimama", "items", "--page=1"], [{"itemId": TB_ITEM, "itemTitle": "连衣裙 夏季", "price": 199.0}]),
    ])
    cat = platforms.sync_catalog(cfg, ["xhs", "dy", "tb"])
    xhs, dy, tb = (cat["platforms"][p] for p in ("xhs", "dy", "tb"))
    assert xhs["ok"] and [i["id"] for i in xhs["items"]] == [XHS_ITEM, other]
    assert xhs["items"][0]["skus"] == [{"id": XHS_SKU, "name": "50ml", "price": "99.00", "on_sale": True}]
    assert xhs["items"][0]["price"] == "99.00"
    assert dy["items"] == [{"id": DY_ITEM, "title": "测试主品", "price": "19.99",
                            "skus": [{"id": DY_SKU, "name": "红色/M", "price": "19.99"}]}]
    assert tb["items"][0] == {"id": TB_ITEM, "title": "连衣裙 夏季", "price": "199.00", "skus": []}
    ws = tmp_path / "ws"
    (ws / "kb").mkdir(parents=True)
    paths = platforms.write_catalog(ws, cat)
    md = (ws / "kb/products.md").read_text(encoding="utf-8")
    assert f"| {XHS_ITEM} | 防晒霜 | 99.00 | {XHS_SKU}：50ml 99.00 |" in md
    assert "面膜 ｜ 补水" in md and f"| {DY_ITEM} | 测试主品 |" in md and "## 淘宝（tb）— 1 个商品" in md
    assert platforms.load_catalog(ws)["platforms"]["dy"]["items"][0]["id"] == DY_ITEM and len(paths) == 2
    for argv in (c["argv"] for c in node.calls()):
        assert "--execute" not in argv and "--execute=true" not in argv


def test_a_platform_that_fails_keeps_its_previous_list(node, cfg):
    node.rules([rule(["fxg", "products"], stderr="[NOT_LOGGED_IN] login page\n", exit=77)])
    cat = platforms.sync_catalog(cfg, ["dy"], previous=catalog())
    dy = cat["platforms"]["dy"]
    assert dy["ok"] is False and dy["stale"] is True and dy["items"][0]["id"] == DY_ITEM and "77" in dy["error"]
    assert cat["platforms"]["xhs"]["items"][0]["id"] == XHS_ITEM          # not asked for: kept as it was
    assert "这次没读成" in platforms.catalog_markdown(cat)


def test_the_module_runs_on_the_macs_system_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('p', {str(ROOT / 'src/prm/platforms.py')!r})\n"
        "p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)\n"
        f"a = p.check_action(json.loads({json.dumps(DESIGNATED, ensure_ascii=False)!r}))\n"
        f"s = p.check_action(json.loads({json.dumps(SAMPLE, ensure_ascii=False)!r}))\n"
        "r = p.run_cli([sys.executable, '-c', 'print(1)'], '.', timeout=10)\n"
        "assert p.ledger_action({**s, 'receiver': {**s['receiver'], 'phone': '13900001111'}})['receiver']['phone'] == '139****1111'\n"
        "print(a['commission_rate'], r.exit_code, r.stdout.strip(), sys.version_info[:2])\n",
        encoding="utf-8")
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("20 0 1")


# ── 聚水潭: sample orders ─────────────────────────────────────────────────

@pytest.fixture
def jcfg(node):
    return platforms.config(node.settings(jst_shops={"dy": JST_SHOP_DY, "xhs": JST_SHOP_XHS}, jst_expect_uid=JST_UID,
                                          jst_logistics=["SF", "ZTO"]))


def test_a_sample_order_is_normalised_and_held_to_the_catalogue():
    assert platforms.check_action({**SAMPLE, "receiver": {**RECEIVER, "phone": "+86 139-0000-1111"}},
                                  catalog=catalog()) == {
        "action": "jst.sample_order", "shop": "dy", "item_id": DY_ITEM,
        "items": [{"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]}],
        "creator_platform": "dy", "creator_uid": DY_UID,
        "receiver": {**RECEIVER, "phone": "13900001111"}}
    assert platforms.check_action(SAMPLE)["receiver"]["phone"] == "〈手机1〉"          # the draft's placeholder
    xhs = {**SAMPLE, "shop": "xhs", "item_id": XHS_ITEM, "skus": [{"sku_id": XHS_SKU, "qty": 2}],
           "creator_platform": "xhs", "creator_uid": None, "creator_account": "xhs_123", "logistics": "ZTO"}
    out = platforms.check_action({k: v for k, v in xhs.items() if v is not None}, catalog=catalog())
    assert out["creator_account"] == "xhs_123" and out["logistics"] == "ZTO" and out["items"][0]["skus"][0]["qty"] == 2
    without = {k: v for k, v in SAMPLE.items() if k != "receiver"}
    assert "receiver" not in platforms.check_action(without)
    with pytest.raises(ActionError) as e:
        platforms.check_action({**SAMPLE, "skus": [{"sku_id": "3100000000000000999", "qty": 1}]}, catalog=catalog())
    assert e.value.code == "bad_sku"
    with pytest.raises(ActionError) as e:
        platforms.check_action({**SAMPLE, "item_id": "3000000000000000999"}, catalog=catalog())
    assert e.value.code == "not_in_catalog"


@pytest.mark.parametrize("change, code", [
    ({"shop": "tb"}, "bad_shop"),
    ({"shop": None}, "missing_param"),
    ({"item_id": XHS_ITEM}, "bad_item"),                                        # a 小红书 id for the 抖店
    ({"skus": [{"sku_id": DY_SKU, "qty": 3}]}, "over_limit"),                   # the Mac's own cap (2)
    ({"skus": [{"sku_id": DY_SKU, "qty": 0}]}, "bad_qty"),
    ({"skus": [{"sku_id": DY_SKU, "qty": True}]}, "bad_qty"),
    ({"skus": [{"sku_id": DY_SKU, "qty": 1}, {"sku_id": DY_SKU, "qty": 1}]}, "bad_sku"),
    ({"skus": [{"sku_id": DY_SKU, "qty": 1, "price": "1"}]}, "bad_sku"),
    ({"skus": [{"sku_id": f"31000000000000002{i:02d}", "qty": 1} for i in range(11)]}, "over_limit"),  # 10 SKUs a order
    ({"skus": []}, "bad_sku"),
    ({"item_id": None, "skus": None}, "missing_param"),
    ({"items": [{"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]}]}, "bad_items"),     # items and skus
    ({"skus": None, "items": "x"}, "bad_items"),
    ({"skus": None, "items": [{"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}], "price": 1}]}, "bad_items"),
    ({"skus": None, "items": [{"item_id": DY_ITEM2, "skus": [{"sku_id": DY_SKU2, "qty": 1}]}]}, "bad_items"),  # item_id
    ({"item_id": None, "skus": None, "items": [{"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]},
                                               {"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]}]}, "bad_items"),
    ({"item_id": None, "skus": None, "items": [{"item_id": DY_ITEM, "skus": []}]}, "bad_sku"),
    ({"creator_platform": "ks"}, "bad_creator"),
    ({"creator_account": "dy_abc"}, "bad_creator"),                              # both the UID and the number
    ({"creator_uid": "--execute"}, "bad_creator"),
    ({"creator_uid": "小美"}, "bad_creator"),                                    # nicknames are fuzzy: not taken
    ({"receiver": {**RECEIVER, "phone": "12345"}}, "bad_receiver"),
    ({"receiver": {**RECEIVER, "address": ""}}, "bad_receiver"),
    ({"receiver": {**RECEIVER, "remark": "x"}}, "bad_receiver"),
    ({"receiver": {**RECEIVER, "name": "张三\n--execute"}}, "bad_receiver"),
    ({"logistics": "顺丰"}, "bad_logistics"),
    ({"commission_rate": 20}, "extra_param"),
])
def test_what_does_not_fit_a_sample_order_is_refused(change, code):
    raw = {**SAMPLE, **change}
    with pytest.raises(ActionError) as e:
        platforms.check_action({k: v for k, v in raw.items() if v is not None}, limits=platforms.DEFAULT_LIMITS)
    assert e.value.code == code


def test_a_sample_orders_limits_are_the_macs_and_the_form_is_wider():
    three = {**SAMPLE, "skus": [{"sku_id": DY_SKU, "qty": 3}]}
    assert platforms.check_action(three, limits={"max_sample_qty": 5})["items"][0]["skus"][0]["qty"] == 3
    assert platforms.check_action(three, limits=platforms.FORM_LIMITS)
    with pytest.raises(ActionError):
        platforms.check_action(three)


def test_the_jst_settings_are_read(node):
    cfg = platforms.config(node.settings(jst_shops={"dy": 30000001, "xhs": "x1", "tb": "1"}, jst_expect_uid="9 0",
                                         jst_logistics="SF"))
    assert cfg["jst_shops"] == {"dy": "30000001"} and cfg["jst_expect_uid"] == "" and cfg["jst_logistics"] == ["SF"]
    assert "jst" in cfg["dirs"]
    cfg = platforms.config(node.settings(jst_logistics=["SF.COLLECT", "顺丰", 3]))
    assert cfg["jst_logistics"] == ["SF.COLLECT"]


def test_the_console_shows_a_sample_orders_parameters():
    shown = {p["label"]: p["value"] for p in platforms.describe(platforms.check_action(SAMPLE))}
    assert shown["发货店铺"] == "抖音（dy）" and shown["寄样商品：SKU × 数量"] == f"{DY_ITEM}：{DY_SKU} × 1"
    assert "商品 ID" not in shown                                               # the order lists its products
    assert shown["收货信息"] == "张三 〈手机1〉 广东省佛山市南海区 某某路 100 号 1 栋 101"
    assert platforms.reply_problems("寄样单建好啦：{寄样单号}", platforms.check_action(SAMPLE)) == []
    assert platforms.fill_reply("单号 {寄样单号}", platforms.check_action(SAMPLE), {"sample_order_no": JST_ORDER}) == (
        f"单号 {JST_ORDER}", [])


def test_sample_order_preview_reads_with_a_private_order_file(node, jcfg):
    node.rules([rule(["sample-create"], jst_preview(), execute=False)])
    block = platforms.preview(SAMPLE, jcfg, catalog=catalog(), context={"party_id": 7})
    assert block["verdict"] == "ready", block
    assert block["run"] == {"steps": ["create"]} and block["catalog"]["title"] == "测试主品"
    [call] = node.calls()
    spec_arg = [a for a in call["argv"] if a.startswith("--spec=")][0]
    assert call["argv"] == ["bin/jst.mjs", "sample-create", spec_arg, f"--expect-uid={JST_UID}"]
    assert call["cwd"].endswith("jst-engineering") and call["spec_mode"] == "0o600"
    assert not os.path.exists(spec_arg[len("--spec="):])                       # removed after the run
    assert call["spec"] == {
        "daren": {"platform": "DY", "uid": DY_UID}, "logistics": "SF",
        "goods": [{"platform": "DY", "shop": JST_SHOP_DY, "product": DY_ITEM, "title": "测试主品", "sku": DY_SKU, "qty": 1}],
        "receiver": {"name": "张三", "phone": None, "state": "广东省", "city": "佛山市", "district": "南海区",
                     "address": "某某路 100 号 1 栋 101"}}                       # the placeholder stays here
    facts = {f["label"]: f["value"] for f in block["steps"][0]["facts"]}
    assert facts["快递"] == "菜鸟顺丰-优惠陆运（SF）" and "T100-RED-M 红色/M × 1" in facts["商品"]
    assert "归这个子账号跟进" in facts["达人"] and facts["查重"] == "这位达人没寄过这个商品"
    assert facts["执行时会做"] == "新增收货地址、提交寄样单" and "张*" in facts["收货"]


@pytest.mark.parametrize("answer, verdict", [
    (dict(stdout=jst_blocked("这位达人现在归 同事 跟进，不是当前登录的账号"), exit=1), "blocked"),
    (dict(stderr="[WRONG_ACCOUNT] 登录的是账号 900002，不是指定的 900001\n", exit=79), "wrong_account"),
    (dict(stderr="[NOT_LOGGED_IN] 聚水潭登录失效\n", exit=77), "login"),
    (dict(stderr="[RISK] 被限流（操作过于频繁）\n", exit=76), "wall"),
    (dict(stderr="[USAGE] goods[0].title 太长\n", exit=2), "invalid"),
    (dict(stderr="[FAILED] TypeError\n", exit=1), "error"),
])
def test_what_stops_a_sample_order_preview(node, jcfg, answer, verdict):
    node.rules([rule(["sample-create"], execute=False, **answer)])
    block = platforms.preview(SAMPLE, jcfg, catalog=catalog())
    assert block["verdict"] == verdict, block
    if verdict == "blocked":
        assert "归 同事 跟进" in block["problems"][0]


def test_a_sample_order_needs_this_macs_shop_and_courier(node, jcfg):
    for cfg, raw, why in ((platforms.config(node.settings()), SAMPLE, "jst_shops.dy"),
                          (jcfg, {**SAMPLE, "logistics": "YTO"}, "不在这台 Mac 允许的里面"),
                          ({**jcfg, "jst_logistics": []}, SAMPLE, "jst_logistics"),
                          ({**jcfg, "jst_logistics": []}, {**SAMPLE, "logistics": "SF.COLLECT"}, "jst_logistics")):
        block = platforms.preview(raw, cfg, catalog=catalog())
        assert block["verdict"] == "invalid" and why in block["problems"][0], block
    untitled = catalog()
    untitled["platforms"]["dy"]["items"][0]["title"] = ""
    assert platforms.preview(SAMPLE, jcfg, catalog=untitled)["verdict"] == "invalid"
    assert node.calls() == []
    unset = platforms.config(node.settings(jst_shops={"dy": JST_SHOP_DY}, jst_logistics=["SF"]))
    node.rules([rule(["sample-create"], jst_preview(), execute=False)])
    block = platforms.preview(SAMPLE, unset, catalog=catalog())
    assert block["verdict"] == "ready" and not any("--expect-uid" in a for a in node.calls()[0]["argv"])
    assert any("jst_expect_uid" in f["value"] for f in block["steps"][0]["facts"])


def test_a_sample_order_runs_once_with_the_number_and_the_ledger_keeps_it_masked(node, jcfg, tmp_path):
    node.rules([rule(["sample-create"], jst_done(), execute=True)])
    approved = {**SAMPLE, "receiver": {**RECEIVER, "phone": "13900001111"}}      # as the bot sends it
    out = execute(jcfg, approved, {"steps": ["create"]}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS", out
    assert out["created"] == {"sample_order_no": JST_ORDER}
    [call] = node.calls()
    assert "--execute" in call["argv"] and call["spec"]["receiver"]["phone"] == "13900001111"
    facts = {f["label"]: f["value"] for f in out["steps"][0]["facts"]}
    assert facts["寄样单号"] == f"{JST_ORDER}（INIT）" and facts["已经做了"] == "新增收货地址、提交寄样单"
    ledger = (tmp_path / "ledger" / f"{KEY}.json").read_text(encoding="utf-8")
    assert "13900001111" not in ledger and "某某路" not in ledger and "南海区" not in ledger and "139****1111" in ledger
    assert json.loads(ledger)["action"]["receiver"] == {"name": "张*", "phone": "139****1111", "province": "广东省",
                                                        "city": "佛山市", "district": "", "address": "（详细地址已清除）"}
    again = execute(jcfg, approved, {"steps": ["create"]}, tmp_path)
    assert again["outcome"] == "ALREADY_RUN" and len(node.calls()) == 1
    assert platforms.check_record(json.loads(json.dumps(out)))["created"] == {"sample_order_no": JST_ORDER}


def test_a_sample_order_with_the_placeholder_still_in_it_is_not_run(node, jcfg, tmp_path):
    out = execute(jcfg, SAMPLE, {"steps": ["create"]}, tmp_path)
    assert out["outcome"] == "NOT_RUN" and "占位符" in out["problems"][0] and node.calls() == []


@pytest.mark.parametrize("answer, outcome", [
    (dict(stdout=jst_done("ACCEPTED_UNVERIFIED", note="建了，但读回核对对不上：快递读回是 顺丰陆运到付"), exit=1),
     "ACCEPTED_UNVERIFIED"),
    (dict(stdout=jst_done("INDETERMINATE", order=None, note="提交没拿到应答：不要重发"), exit=1), "INDETERMINATE"),
    (dict(stdout=jst_blocked("这位达人寄过这些商品了（YP111）"), exit=1), "FAILED"),
    (dict(stdout=jst_done(order=None), exit=0), "ACCEPTED_UNVERIFIED"),          # "verified" without its number
    (dict(stderr="[RISK] 被限流（已经做了：加入达人库），单子没有建\n", exit=76), "FAILED"),
    (dict(stderr="[WRONG_ACCOUNT] 登录的不是 900001\n", exit=79), "FAILED"),
    (dict(stderr="[FAILED] TypeError at submit\n", exit=1), "INDETERMINATE"),
    (dict(stderr="[INTERRUPTED] 被打断：写命令可能已经发出\n", exit=130), "INDETERMINATE"),
])
def test_a_sample_order_that_is_not_verified_says_so(node, jcfg, tmp_path, answer, outcome):
    node.rules([rule(["sample-create"], execute=True, **answer)])
    out = execute(jcfg, {**SAMPLE, "receiver": {**RECEIVER, "phone": "13900001111"}}, {"steps": ["create"]}, tmp_path)
    assert out["outcome"] == outcome, out
    assert out["problems"] or out["steps"][0]["problems"]
    if out.get("created", {}).get("sample_order_no"):
        assert JST_ORDER in out["problems"][0]                              # the red mark names the order


def test_the_bot_keeps_only_a_sample_order_number():
    rec = {**platforms.record("VERIFIED_SUCCESS"), "created": {"sample_order_no": JST_ORDER}}
    assert platforms.check_record(rec)["created"] == {"sample_order_no": JST_ORDER}
    rec["created"]["sample_order_no"] = "YP1; rm"
    assert platforms.check_record(rec)["created"] == {}


def test_jushuitan_is_no_catalogue_platform(node, jcfg):
    cat = platforms.sync_catalog(jcfg, ["jst"], previous=catalog())
    assert "jst" not in cat["platforms"] and node.calls() == []
    assert "聚水潭" not in platforms.catalog_markdown(cat)


THREE = {"action": "jst.sample_order", "shop": "dy", "creator_platform": "dy", "creator_account": "test_daren_01",
         "receiver": RECEIVER, "items": [
             {"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]},
             {"item_id": DY_ITEM2, "skus": [{"sku_id": DY_SKU2, "qty": 1}, {"sku_id": DY_SKU2B, "qty": 1}]},
             {"item_id": DY_ITEM3, "skus": [{"sku_id": DY_SKU3, "qty": 1}]}]}


def test_one_sample_order_takes_several_products_of_one_shop():
    out = platforms.check_action(THREE, catalog=catalog())
    assert out["item_id"] == DY_ITEM and [i["item_id"] for i in out["items"]] == [DY_ITEM, DY_ITEM2, DY_ITEM3]
    assert platforms.check_action(out, catalog=catalog()) == out                  # what the bot stores runs again
    assert platforms.check_action({**THREE, "item_id": DY_ITEM}, catalog=catalog()) == out
    shown = {p["label"]: p["value"] for p in platforms.describe(out, {"title": "测试主品"})}
    assert shown["商品"] == "测试主品（等 3 款）"
    assert shown["寄样商品：SKU × 数量"] == (f"{DY_ITEM}：{DY_SKU} × 1；{DY_ITEM2}：{DY_SKU2} × 1、{DY_SKU2B} × 1；"
                                            f"{DY_ITEM3}：{DY_SKU3} × 1")
    with pytest.raises(ActionError) as e:                                        # every product in the catalogue
        platforms.check_action({**THREE, "items": [*THREE["items"], {"item_id": "3000000000000000999",
                                                                   "skus": [{"sku_id": DY_SKU, "qty": 1}]}]},
                               catalog=catalog())
    assert e.value.code == "not_in_catalog"
    with pytest.raises(ActionError) as e:                                        # and its SKUs its own
        platforms.check_action({**THREE, "items": [{"item_id": DY_ITEM2, "skus": [{"sku_id": DY_SKU, "qty": 1}]}]},
                               catalog=catalog())
    assert e.value.code == "bad_sku"


def test_a_sample_orders_skus_are_counted_over_its_products():
    many = {**THREE, "items": [{"item_id": f"30000000000001{i:05d}", "skus": [{"sku_id": f"31000000000001{i:05d}",
                                                                               "qty": 1}]} for i in range(12)]}
    with pytest.raises(ActionError) as e:
        platforms.check_action(many)                                             # the default: 10 SKUs a order
    assert e.value.code == "over_limit" and "12 个 SKU" in str(e.value)
    assert len(platforms.check_action(many, limits={"max_sample_skus": 50})["items"]) == 12
    fifty_one = {**THREE, "items": [{"item_id": f"30000000000001{i:05d}",
                                     "skus": [{"sku_id": f"31000000000001{i:05d}", "qty": 1}]} for i in range(51)]}
    for lim in ({"max_sample_skus": 80}, platforms.FORM_LIMITS):                # 聚水潭's 50 holds whatever the Mac says
        with pytest.raises(ActionError) as e:
            platforms.check_action(fifty_one, limits=lim)
        assert e.value.code == "over_limit" and "最多 50 个" in str(e.value)


def test_a_preview_of_several_products_searches_each_by_its_own_title(node, jcfg):
    node.rules([rule(["sample-create"], jst_preview(), execute=False)])
    block = platforms.preview(THREE, jcfg, catalog=catalog())
    assert block["verdict"] == "ready", block
    assert block["catalog"]["title"] == "测试主品"
    [call] = node.calls()
    assert [(g["product"], g["title"], g["sku"], g["qty"]) for g in call["spec"]["goods"]] == [
        (DY_ITEM, "测试主品", DY_SKU, 1), (DY_ITEM2, "测试贝雷帽 秋冬羊毛款", DY_SKU2, 1),
        (DY_ITEM2, "测试贝雷帽 秋冬羊毛款", DY_SKU2B, 1), (DY_ITEM3, "测试药盒帽", DY_SKU3, 1)]
    assert {g["shop"] for g in call["spec"]["goods"]} == {JST_SHOP_DY}
    assert call["spec"]["daren"] == {"platform": "DY", "account": "test_daren_01"}
    untitled = catalog()
    untitled["platforms"]["dy"]["items"][2]["title"] = ""
    block = platforms.preview(THREE, jcfg, catalog=untitled)
    assert block["verdict"] == "invalid" and DY_ITEM3 in block["problems"][0]


def test_several_products_run_in_one_order(node, jcfg, tmp_path):
    node.rules([rule(["sample-create"], jst_done(), execute=True)])
    approved = {**THREE, "receiver": {**RECEIVER, "phone": "13900001111"}}
    out = execute(jcfg, approved, {"steps": ["create"]}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS" and out["created"] == {"sample_order_no": JST_ORDER}
    [call] = node.calls()
    assert "--execute" in call["argv"] and len(call["spec"]["goods"]) == 4
    assert {g["title"] for g in call["spec"]["goods"]} == {"测试主品", "测试贝雷帽 秋冬羊毛款", "测试药盒帽"}
