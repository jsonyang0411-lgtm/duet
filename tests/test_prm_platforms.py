"""Platform actions (src/prm/platforms.py): the whitelist and its checks, the CLIs' preview
and execution against a fake ``node`` that answers like the three platform CLIs, the
ledger that runs an approval once, the catalogue, and what the bot accepts back."""

import json
import os
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from src.prm import platforms
from src.prm.platforms import ActionError

from platform_fakes import (
    DRY_STDERR, DY_CHANNEL, DY_ITEM, DY_ITEM2, DY_ITEM3, DY_LINK, DY_SKU, DY_SKU2, DY_SKU2B, DY_SKU3, DY_UID, JST_ORDER, JST_SHOP_DY, JST_SHOP_XHS, JST_UID,
    TB_CAMPAIGN, TB_CROWD, TB_ITEM, TB_PRECHECK, XHS_BUYER, XHS_ITEM, XHS_SKU, FakeNode, catalog, dy_agreed_preview,
    dy_bind_done, dy_bind_preview, dy_bound_list, dy_create_done, dy_designated_done, dy_designated_preview, dy_promotion,
    dy_read_preview,
    jst_blocked, jst_done, jst_preview, jst_sample_row, jst_samples, rule, tb_done, tb_row, xhs_buyers,
    xhs_channel_done, xhs_channel_preview, xhs_plan,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = "a" * 32

DESIGNATED = {"action": "dy.designated_commission", "item_id": DY_ITEM, "creator_platform_id": DY_UID,
              "commission_rate": 20, "note": "kb/合作政策.md：腰部达人 20%"}
CHANNEL = {"action": "dy.channel_goods", "item_id": DY_ITEM, "creator_platform_id": DY_UID, "title_prefix": "小美专属"}
# What the preview of CHANNEL reads and agrees (19.99 → 17: 9 折 floored to the yuan; 40 can be spared, so 40)
CHANNEL_READ = dy_read_preview([(DY_SKU, "红色/M", "19.99", 40)])
CHANNEL_AGREED = dy_agreed_preview([(DY_SKU, "红色/M", "19.99", "17.00", 40, 40)])
CHANNEL_RUN = {"steps": ["create", "bind"], "skus": [{"id": DY_SKU, "price": "17.00", "stock": 40, "main_price": "19.99"}]}
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
                                               "creator_platform_id": DY_UID, "title_prefix": "【小美专属】",
                                               "exclusive": True, "convention": 1}
    assert platforms.check_action({**CHANNEL, "exclusive": False, "title_prefix": "", "price": "15",
                                   "stock_per_sku": 50}) == {
        "action": "dy.channel_goods", "item_id": DY_ITEM, "creator_platform_id": DY_UID, "exclusive": False,
        "price": "15.00", "stock_per_sku": 50, "convention": 1}
    assert platforms.check_action(platforms.check_action(CHANNEL)) == platforms.check_action(CHANNEL)   # (round trip)
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
    ({**DESIGNATED, "commission_rate": 26}, "over_limit"),               # 公司定的上限 25%
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
    ({k: v for k, v in CHANNEL.items() if k != "title_prefix"}, "missing_prefix"),   # 专属: 【某某专属】
    ({**CHANNEL, "title_prefix": "  "}, "missing_prefix"),
    ({**CHANNEL, "title_prefix": "这是一个非常非常长的名字的专属"}, "bad_prefix"),   # 32 > TITLE_PREFIX_CAP
    ({**CHANNEL, "title_prefix": "小美V123专属"}, "bad_prefix"),          # reads like contact details
    ({**CHANNEL, "convention": 2}, "bad_convention"),
    ({**CHANNEL, "convention": True}, "bad_convention"),
    ({**CHANNEL, "exclusive": "yes"}, "bad_exclusive"),
    ({**CHANNEL, "stock": 100}, "extra_param"),                         # the old parameter: stock_per_sku now
    ({**CHANNEL, "stock_per_sku": 0}, "bad_stock"),
    ({**CHANNEL, "stock_per_sku": -1}, "bad_stock"),
    ({**CHANNEL, "stock_per_sku": True}, "bad_stock"),
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


def test_the_mac_can_lower_the_commission_cap_but_not_raise_it():
    assert platforms.check_action({**DESIGNATED, "commission_rate": 25}, limits={"max_commission_rate": 70})
    with pytest.raises(ActionError) as e:
        platforms.check_action({**DESIGNATED, "commission_rate": 26}, limits={"max_commission_rate": 70})
    assert e.value.code == "over_limit"
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
    cfg = platforms.config(node.settings(tb=str(tmp_path / "nowhere"), max_commission_rate=15))
    assert set(cfg["dirs"]) == {"xhs", "dy", "jst"} and "tb" in cfg["missing"] and cfg["enabled"]
    assert cfg["limits"]["max_commission_rate"] == 15
    assert platforms.config(node.settings(max_commission_rate=30))["limits"]["max_commission_rate"] == 25
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


# ── 专属链接 the shop's way (dy.channel_goods, 2026-11-09) ─────────────────
# 专属达人带货, 【某某专属】, 9 折 of each SKU's daily price floored to the yuan, 100 a colour capped at what the main
# product can spare, 长期有效; the preview reads the product, works the agreement out and previews with it.

DY_SKU2C = "3100000000000000206"          # a colour the catalogue does not list yet (added after the last sync)
HAT = {"action": "dy.channel_goods", "item_id": DY_ITEM2, "creator_platform_id": DY_UID, "title_prefix": "【小美专属】"}
HAT_READ = dy_read_preview([(DY_SKU2, "均码黑色", "315.00", 500), (DY_SKU2B, "均码咖啡", "290.00", 30),
                            (DY_SKU2C, "均码米白", "269.00", 0)], item=DY_ITEM2)
HAT_SKUS = [{"id": DY_SKU2, "price": "283.00", "stock": 100, "main_price": "315.00"},
            {"id": DY_SKU2B, "price": "261.00", "stock": 30, "main_price": "290.00"},
            {"id": DY_SKU2C, "price": "242.00", "stock": 0, "main_price": "269.00"}]
HAT_RUN = {"steps": ["create", "bind"], "skus": HAT_SKUS}
HAT_AGREEMENT = {"productId": DY_ITEM2, "saleChannel": "kol-exclusive", "titlePrefix": "【小美专属】",
                 "skus": [{"id": s["id"], "price": s["price"], "stock": s["stock"]} for s in HAT_SKUS]}


def hat_agreed(main=("315.00", "290.00", "269.00"), **kw):
    """The CLI's preview with HAT's agreement (``main``: the daily prices it reads now)."""
    return dy_agreed_preview([(DY_SKU2, "均码黑色", main[0], "283.00", 100, 500),
                              (DY_SKU2B, "均码咖啡", main[1], "261.00", 30, 30),
                              (DY_SKU2C, "均码米白", main[2], "242.00", 0, 0)], item=DY_ITEM2, **kw)


def hat_rules(*more, read=HAT_READ, agreed=None, bound=None):
    """She is bound to no channel product of it yet, the read (no agreement), the preview with the agreement, then
    ``more``."""
    return [rule(["channel-goods-list"], dy_bound_list(item=DY_ITEM2) if bound is None else bound),
            rule(["channel-goods-create"], read, execute=False, agreement=False),
            rule(["channel-goods-create"], hat_agreed() if agreed is None else agreed, execute=False, agreement=True),
            *more]


@pytest.mark.parametrize("main, live", [("315", "283"), ("290", "261"), ("399", "359"), ("188", "169"), ("269", "242"),
                                        ("315.00", "283"), ("199.90", "179"), ("100", "90"), ("1.12", "1"), (220, "198")])
def test_the_channel_price_is_9_zhe_of_the_daily_price_floored_to_the_yuan(main, live):
    assert platforms.live_price(main) == Decimal(live)


@pytest.mark.parametrize("main", ["1.00", "0", "-5", "", None, "x", True, "NaN", "Infinity"])
def test_no_channel_price_without_a_daily_price_worth_a_yuan(main):
    assert platforms.live_price(main) is None


@pytest.mark.parametrize("given, prefix", [
    ("小美专属", "【小美专属】"), ("【小美专属】", "【小美专属】"), ("  【小美专属】 ", "【小美专属】"),
    ("[小美专属]", "【小美专属】"), ("「小美专属」", "【小美专属】"), ("（小美专属）", "【小美专属】"),
    ("【 小美专属 】", "【小美专属】"), ("三个字专属", "【三个字专属】"),          # 1 + 5 × 2 + 1 = 12: the usual limit
    ("Amy专属", "【Amy专属】"), ("小美🌸", "【小美🌸】"), ("Vivi专属", "【Vivi专属】"),
    ("“小美专属”", "【小美专属】"), ("‘小美专属’", "【小美专属】"), ('"小美专属"', "【小美专属】"), ("'小美专属'", "【小美专属】"),
    ("小\u200b美专属\ufeff", "【小美专属】"), ("\u2060【小美专属】\u200e", "【小美专属】"),   # zero-width, BOM, marks go
    ("四个字的专属", "【四个字的专属】"),          # 14: the product's own limit (12 unless its config says more) is the CLI's
    # a quote or bracket alone at an end goes (review 2026-11-09 F7); spaces inside stay, the CLI takes them
    ('"小美专属', "【小美专属】"), ("小美专属”", "【小美专属】"), ("「小美专属", "【小美专属】"), ("【“小美专属】", "【小美专属】"),
    ("〈小美专属〉", "【小美专属】"), ("小美 专属", "【小美 专属】"), ("小美QQ专属", "【小美QQ专属】"),
    ("抖音小美专属", "【抖音小美专属】"), ("小美美呀专属", "【小美美呀专属】"), ("2026专属", "【2026专属】"),
])
def test_a_prefix_gets_the_shops_brackets_and_fits_the_page(given, prefix):
    assert platforms.title_prefix_of(given) == prefix
    assert platforms.title_length(prefix) <= platforms.TITLE_PREFIX_CAP


@pytest.mark.parametrize("given, code, words", [
    ("这是一个非常非常长的名字的专属", "bad_prefix", "是 32 个字符"), ("【这是一个非常非常长的名字的专属】", "bad_prefix", "是 32 个字符"),
    ("小美V123专属", "bad_prefix", "像联系方式"), ("微信小美专属", "bad_prefix", "像联系方式"), ("小美vx专属", "bad_prefix", "像联系方式"),
    ("加V专属", "bad_prefix", "像联系方式"), ("13800001111", "bad_prefix", "像联系方式"), ("小美 v:abc", "bad_prefix", "像联系方式"),
    ("\u200b\u200b", "missing_prefix", "【某某专属】"),
    ("【小美专属", "bad_prefix", "不成对"), ("小美】专属", "bad_prefix", "不成对"),
    ("小美\n专属", "bad_prefix", "换行"), ("{链接}", "bad_prefix", "花括号"), (12, "bad_prefix", "要是文字"),
    ("", "missing_prefix", "【某某专属】"), ("【】", "missing_prefix", "【某某专属】"), ("[ ]", "missing_prefix", "【某某专属】"),
    # C1 controls and the Unicode line separators, the bot's own check too; 「微 信」 with a space, full-width letters,
    # digits apart (review 2026-11-09 F7); a lone quote is not a prefix
    ("小美\x85专属", "bad_prefix", "控制字符"), ("小美\x9f专属", "bad_prefix", "控制字符"),
    ("小美\u2028专属", "bad_prefix", "换行"), ("小美\u2029专属", "bad_prefix", "换行"),
    ("小美微 信专属", "bad_prefix", "像联系方式"), ("小美薇\u3000信专属", "bad_prefix", "像联系方式"),
    ("小美ｖｘ专属", "bad_prefix", "像联系方式"), ("小美Ｖ１２３专属", "bad_prefix", "像联系方式"),
    ("小美 1 2 3 4 5", "bad_prefix", "像联系方式"), ("小美138-0000专属", "bad_prefix", "像联系方式"),
    ("V587专属", "bad_prefix", "像联系方式"), ("小美加V专属", "bad_prefix", "像联系方式"), ("小美w x专属", "bad_prefix", "像联系方式"),
    ('"', "missing_prefix", "【某某专属】"), ("「」", "missing_prefix", "【某某专属】"),
])
def test_a_prefix_the_page_would_not_take_is_refused_saying_why(given, code, words):
    with pytest.raises(ActionError) as e:
        platforms.title_prefix_of(given)
    assert e.value.code == code and words in str(e.value), str(e.value)


def test_the_page_counts_a_han_character_two_and_anything_else_its_utf16_length():
    assert platforms.title_length("【三个字专属】") == 12 and platforms.title_length("【小美专属】") == 10
    assert platforms.title_length("Amy 专属") == 8
    assert platforms.title_length("🌸") == 2                     # an emoji: two UTF-16 units
    assert platforms.title_length("〇々") == 4                   # Han by script, outside the common block
    assert platforms.title_length("\U00020000") == 2            # an extension-B ideograph
    assert platforms.title_length("") == 0 and platforms.title_length(None) == 0


def test_the_agreement_is_9_zhe_and_100_a_colour_capped_at_what_the_product_can_spare():
    rows = HAT_READ["channelGoods"]["skus"] + [
        {"skuId": "3100000000000000207", "name": "均码灰", "mainPrice": "188.00", "assignableStock": None},
        {"skuId": "3100000000000000208", "name": "均码红", "mainPrice": "399.00", "assignableStock": -3}]
    skus, warnings, problems = platforms.channel_skus(rows, platforms.check_action(HAT, catalog=catalog()))
    assert problems == []
    assert [(s["id"], s["main_price"], s["price"], s["stock"], s["assignable"]) for s in skus] == [
        (DY_SKU2, "315.00", "283.00", 100, 500), (DY_SKU2B, "290.00", "261.00", 30, 30),
        (DY_SKU2C, "269.00", "242.00", 0, 0), ("3100000000000000207", "188.00", "169.00", 0, None)]
    assert warnings == [
        f"SKU {DY_SKU2C}（均码米白）：主商品这个颜色没有可分的库存，渠道品里这个颜色 0 件",
        "SKU 3100000000000000207（均码灰）：平台没说主商品这个颜色还能分多少库存，渠道品里这个颜色先放 0 件",
        "SKU 3100000000000000208（均码红）：主商品这个颜色可分的库存是 -3（已经分超了），渠道品里不放这个颜色"]
    action = platforms.check_action({**HAT, "note": "kb：专属链接"}, catalog=catalog())
    assert platforms.channel_agreement(action, skus) == {
        "productId": DY_ITEM2, "saleChannel": "kol-exclusive", "titlePrefix": "【小美专属】", "note": "kb：专属链接",
        "skus": [{"id": DY_SKU2, "price": "283.00", "stock": 100}, {"id": DY_SKU2B, "price": "261.00", "stock": 30},
                 {"id": DY_SKU2C, "price": "242.00", "stock": 0}, {"id": "3100000000000000207", "price": "169.00", "stock": 0}]}


def test_a_draft_may_name_another_price_or_stock_and_the_cap_still_holds():
    rows = HAT_READ["channelGoods"]["skus"]
    action = platforms.check_action({**HAT, "price": "250", "stock_per_sku": 50}, catalog=catalog())
    skus, _, problems = platforms.channel_skus(rows, action)
    assert problems == [] and [(s["price"], s["stock"]) for s in skus] == [("250.00", 50), ("250.00", 30), ("250.00", 0)]
    shared = platforms.check_action({**HAT, "exclusive": False}, catalog=catalog())       # 达人带货: shares the stock
    skus, warnings, problems = platforms.channel_skus(rows, shared)
    assert problems == [] and warnings == [] and [s["stock"] for s in skus] == [None, None, None]
    agreement = platforms.channel_agreement(shared, skus)
    assert agreement["saleChannel"] == "kol" and all(set(s) == {"id", "price"} for s in agreement["skus"])
    assert [s["price"] for s in agreement["skus"]] == ["283.00", "261.00", "242.00"]       # its price: 9 折 too


def test_no_agreement_without_stock_in_any_colour_or_without_a_daily_price():
    action = platforms.check_action(HAT, catalog=catalog())
    none_left = [{"skuId": DY_SKU2, "name": "均码黑色", "mainPrice": "315.00", "assignableStock": 0}]
    assert "每个颜色都没有可分的库存" in platforms.channel_skus(none_left, action)[2][0]
    unpriced = [{"skuId": DY_SKU2, "name": "均码黑色", "mainPrice": None, "assignableStock": 9}]
    assert "没读到日常价" in platforms.channel_skus(unpriced, action)[2][0]
    assert platforms.channel_skus([], action)[2] == ["读主商品的结果里没有能放进渠道品的 SKU"]
    # a price of its own is held to the daily price: one not read cannot be checked (review 2026-11-09, item 5's gap)
    priced = platforms.check_action({**HAT, "price": "250"}, catalog=catalog())
    [problem] = platforms.channel_skus(unpriced, priced)[2]
    assert "没读到日常价，动作里写的渠道价 250.00 元没法核对（渠道价不能比日常价高）" in problem


def test_the_bot_holds_the_approved_agreement_to_the_shops_way():
    action = platforms.check_action(HAT)
    assert platforms.check_plan(action, HAT_RUN) == {"steps": ["create", "bind"], "skus": HAT_SKUS}

    def changed(**change):
        return {**HAT_RUN, "skus": [{**HAT_SKUS[0], **change}, *HAT_SKUS[1:]]}

    for plan, words in [
        ({"steps": ["create", "bind"]}, "点「重新预览」"),                          # an approval from before the agreement
        (changed(price="250.00"), "不是日常价 315.00 元的 9 折取整"), (changed(main_price=""), "（没写）的 9 折取整"),
        (changed(stock=101), "0–100 件"), (changed(stock=-1), "0–100 件"), (changed(stock="100"), "0–100 件"),
        (changed(id="x1"), "SKU ID"), (changed(colour="黑"), "{id, price, stock, main_price}"),
        ({**HAT_RUN, "skus": [HAT_SKUS[0], HAT_SKUS[0]]}, "写了两次"),
        ({**HAT_RUN, "skus": [{**s, "stock": 0} for s in HAT_SKUS]}, "每个颜色都是 0 件"),
    ]:
        with pytest.raises(ActionError) as e:
            platforms.check_plan(action, plan)
        assert e.value.code == "bad_plan" and words in str(e.value), (plan, str(e.value))
    fixed = platforms.check_action({**HAT, "price": "250", "stock_per_sku": 50, "sku_ids": [DY_SKU2, DY_SKU2B]})
    run = {"steps": ["create", "bind"], "skus": [{"id": DY_SKU2, "price": "250.00", "stock": 50, "main_price": "315.00"},
                                                 {"id": DY_SKU2B, "price": "250.00", "stock": 30, "main_price": "290.00"}]}
    assert platforms.check_plan(fixed, run)["skus"][1] == {"id": DY_SKU2B, "price": "250.00", "stock": 30,
                                                           "main_price": "290.00"}
    with pytest.raises(ActionError) as e:            # its daily price not known: the fixed price cannot be checked
        platforms.check_plan(fixed, {**run, "skus": [run["skus"][0], {**run["skus"][1], "main_price": ""}]})
    assert "SKU 3100000000000000203 没有日常价：动作里写的渠道价没法核对" in str(e.value)
    for skus, words in [([{**run["skus"][0], "price": "283.00"}], "不是动作里写的 250.00 元"),
                        ([{**run["skus"][0], "stock": 51}], "0–50 件"),
                        ([{**HAT_SKUS[2], "price": "250.00", "stock": 1}], "不是动作里写的（sku_ids）")]:
        with pytest.raises(ActionError) as e:
            platforms.check_plan(fixed, {**run, "skus": skus})
        assert words in str(e.value), str(e.value)
    shared = platforms.check_action({**HAT, "exclusive": False})
    assert platforms.check_plan(shared, {**HAT_RUN, "skus": [{**s, "stock": None} for s in HAT_SKUS]})
    with pytest.raises(ActionError) as e:
        platforms.check_plan(shared, HAT_RUN)
    assert "共用主商品库存" in str(e.value)


def test_the_preview_reads_the_product_works_out_the_agreement_and_previews_with_it(node, cfg):
    node.rules(hat_rules())
    block = platforms.preview(HAT, cfg, catalog=catalog())
    assert block["verdict"] == "ready", block
    assert block["run"] == HAT_RUN
    bound, read, agreed = node.calls()
    assert bound["argv"][1:] == ["fxg", "channel-goods-list", f"--product-id={DY_ITEM2}", f"--uid={DY_UID}"]
    assert read["argv"][1:] == ["fxg", "channel-goods-create", f"--product-id={DY_ITEM2}", "--sale-channel=kol",
                                "--title-prefix=【小美专属】"]               # (the product's own prefix rule)
    assert agreed["argv"][1:] == ["fxg", "channel-goods-create", "--agreement=" + agreed["agreement_path"]]
    assert agreed["agreement"] == HAT_AGREEMENT
    assert agreed["agreement_mode"] == "0o600" and not os.path.exists(agreed["agreement_path"])   # removed after the run
    assert [(s["name"], s["verdict"]) for s in block["steps"]] == [("bound", "ready"), ("read", "ready"),
                                                                   ("create", "ready")]
    facts = {f["label"]: f["value"] for f in block["facts"]}
    assert facts["渠道类型"] == "专属达人带货：只绑这一位达人，库存不共享" and facts["有效期"] == "长期有效"
    assert facts["标题前缀"] == "【小美专属】（10/12 个字符，汉字算 2 个）"
    assert facts["SKU"].startswith("3 个；渠道价按日常价 9 折取整到元；库存一共 130 件")
    assert facts[f"SKU {DY_SKU2} 均码黑色"] == "日常价 315.00 元 → 渠道价 283.00 元；库存 100 件（主商品可分 500 件）"
    assert facts[f"SKU {DY_SKU2B} 均码咖啡"] == "日常价 290.00 元 → 渠道价 261.00 元；库存 30 件（主商品可分 30 件）"
    assert facts[f"SKU {DY_SKU2C} 均码米白"] == "日常价 269.00 元 → 渠道价 242.00 元；库存 0 件（主商品可分 0 件）"
    assert [f["value"] for f in block["facts"] if f["label"] == "提醒"] == [
        f"SKU {DY_SKU2C}（均码米白）：主商品这个颜色没有可分的库存，渠道品里这个颜色 0 件"]
    assert any("仅支持预览查看" in f["value"] for f in block["facts"] if f["label"] == "页面提示")
    assert block["facts"][-1]["label"] == "批准以后" and DY_UID in block["facts"][-1]["value"]
    assert block["problems"] == []
    # what the console lists of the action: the shop's way spelled out
    params = {p["label"]: p["value"] for p in platforms.describe(block["action"])}
    assert params["标题前缀"] == "【小美专属】（10 个字符，汉字算 2 个；这个商品最多几个看预览）" and "9 折" in params["渠道价（元）"]
    assert "convention" not in json.dumps(platforms.describe(block["action"]), ensure_ascii=False)
    assert params["每个颜色的库存（件）"].startswith("100 件") and params["有效期"] == "长期有效"
    assert params["专属达人带货"].startswith("是")
    # the bot takes the block as it is (the agreement held to the shop's way there too)
    assert platforms.check_block(block)["run"] == HAT_RUN


def test_the_shop_check_goes_with_every_read_of_a_channel_product(node):
    cfg = platforms.config(node.settings(dy_expect_shop_id="1234567"))
    node.rules(hat_rules())
    assert platforms.preview(HAT, cfg, catalog=catalog())["verdict"] == "ready"
    assert all("--expect-shop-id=1234567" in c["argv"] for c in node.calls()) and len(node.calls()) == 3


def test_a_preview_names_its_skus_when_the_draft_does(node, cfg):
    node.rules(hat_rules(read=dy_read_preview([(DY_SKU2, "均码黑色", "315.00", 500)], item=DY_ITEM2),
                         agreed=dy_agreed_preview([(DY_SKU2, "均码黑色", "315.00", "283.00", 100, 500)], item=DY_ITEM2)))
    block = platforms.preview({**HAT, "sku_ids": [DY_SKU2]}, cfg, catalog=catalog())
    assert block["verdict"] == "ready" and [s["id"] for s in block["run"]["skus"]] == [DY_SKU2]
    assert f"--sku-id={DY_SKU2}" in node.calls()[1]["argv"]


@pytest.mark.parametrize("read, agreed, verdict, words", [
    (dy_read_preview([], item=DY_ITEM2, blockers=["the main product is not on sale (status 1, check_status 3)"]), None,
     "blocked", "the main product is not on sale"),
    (dy_read_preview([(DY_SKU2, "均码黑色", "315.00", 0), (DY_SKU2B, "均码咖啡", "290.00", 0)], item=DY_ITEM2), None,
     "blocked", "主商品的每个颜色都没有可分的库存"),
    (dy_read_preview([(DY_SKU2, "均码黑色", None, 9)], item=DY_ITEM2), None, "blocked", "没读到日常价"),
    (HAT_READ, hat_agreed(price_kind="block"), "blocked", "平台的价格检查没通过（block）"),
    (HAT_READ, hat_agreed(price_kind="warn"), "blocked", "平台的价格检查没通过（warn）"),
    (HAT_READ, hat_agreed(blockers=["the title prefix is 14 characters long (Han characters count 2)"]), "blocked",
     "the title prefix is 14 characters long"),
    (HAT_READ, hat_agreed(main=("320.00", "290.00", "269.00")), "error", "日常价变了：315.00 → 320.00 元"),
], ids=["not-on-sale", "no-stock-anywhere", "no-daily-price", "price-check-block", "price-check-warn", "cli-blocker",
        "price-moved-between-the-reads"])
def test_what_stops_a_channel_preview_is_said(node, cfg, read, agreed, verdict, words):
    node.rules(hat_rules(read=read, agreed=agreed if agreed is not None else hat_agreed()))
    block = platforms.preview(HAT, cfg, catalog=catalog())
    assert block["verdict"] == verdict
    assert any(words in p for p in block["problems"]), block["problems"]
    assert len(node.calls()) == (2 if agreed is None else 3)                 # no agreement: no preview with one
    assert platforms.check_block(block)["verdict"] == verdict                # the bot shows it, not approvable


@pytest.mark.parametrize("answer, verdict", [
    (dict(stderr="[NOT_LOGGED_IN] the 抖店 tab is on a login page; nothing was requested\n", exit=77), "login"),
    (dict(stderr="[RISK_CONTROL] verification wall\n", exit=76), "wall"),
    (dict(stderr="[IDENTITY_MISMATCH] this login is not the one expected\n", exit=79), "wrong_account"),
    (dict(stdout="not json"), "error"),
])
def test_a_read_that_does_not_come_back_stops_the_channel_preview(node, cfg, answer, verdict):
    node.rules([rule(["channel-goods-list"], dy_bound_list(item=DY_ITEM2)),
                rule(["channel-goods-create"], execute=False, agreement=False, **answer)])
    block = platforms.preview(HAT, cfg, catalog=catalog())
    assert block["verdict"] == verdict and len(node.calls()) == 2 and block["problems"]


def test_a_creator_bound_to_a_channel_product_of_it_already_gets_no_second_one(node, cfg, tmp_path):
    """She is on a channel product of this product already (made by hand, 2026-11-09, the bot does not know it):
    the preview stops there (blocked: the channel product, its state, its link to copy), nothing else is read; an
    execution that finds one stops before anything is written."""
    node.rules(hat_rules(bound=dy_bound_list(("7200000000000009", 2), item=DY_ITEM2)))
    block = platforms.preview(HAT, cfg, catalog=catalog())
    assert block["verdict"] == "blocked" and len(node.calls()) == 1
    assert "她已经绑在这个商品的渠道品 7200000000000009 上了（专属达人带货，售卖中）：不再建第二个" in block["problems"][0]
    links = [f["value"] for f in block["steps"][0]["facts"] if f["label"] == "渠道品 7200000000000009 的链接"]
    assert links and links[0].startswith("https://haohuo.jinritemai.com/") and "channel_id=7200000000000009" in links[0]
    node.rules(hat_rules(rule(["channel-goods-create"], dy_create_done(item=DY_ITEM2), execute=True, agreement=True),
                         bound=dy_bound_list(("7200000000000009", 0), item=DY_ITEM2)))
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == "NOT_RUN" and argvs(node, "--execute") == []
    assert [(s["name"], s["outcome"]) for s in out["steps"]] == [("bound", "NOT_RUN")]
    assert "已下架" in out["problems"][0] and "平台没有动" in out["problems"][0]


BOUND_UNREAD = [
    dict(stderr="[SCHEMA_MISMATCH] 渠道品管理 page 1: rows[0].channel_id is not an id\n", exit=1),
    dict(stderr="[USAGE] unknown option --uid\n", exit=2),
    dict(stdout={"site": "fxg", "command": "channel-goods-list", "error": "SCHEMA_MISMATCH"}),
    dict(stdout={**dy_bound_list(item=DY_ITEM2), "complete": False}),
]
BOUND_UNREAD_IDS = ["schema-mismatch", "usage", "no-rows", "not-complete"]


@pytest.mark.parametrize("answer", BOUND_UNREAD, ids=BOUND_UNREAD_IDS)
def test_a_binding_check_that_cannot_be_read_stops_the_preview(node, cfg, answer):
    """Not knowing whether she is bound is never a yes (review 2026-11-09 #1): the preview is an error, the reason is
    among the block's problems, nothing else is read, and the bot cannot approve it."""
    node.rules([rule(["channel-goods-list"], **answer), *hat_rules()[1:]])
    block = platforms.preview(HAT, cfg, catalog=catalog())
    assert block["verdict"] == "error" and len(node.calls()) == 1
    assert any("查不到她是不是已经绑在这个商品的渠道品上" in p and "平台没有动" in p for p in block["problems"])
    assert platforms.check_block(block)["verdict"] == "error"


@pytest.mark.parametrize("answer", BOUND_UNREAD, ids=BOUND_UNREAD_IDS)
def test_a_binding_check_that_cannot_be_read_at_execution_writes_nothing(node, cfg, tmp_path, answer):
    """The review's probe: the list failed at execution and the create went on; now NOT_RUN, nothing written."""
    node.rules([rule(["channel-goods-list"], **answer), *hat_rules(
        rule(["channel-goods-create"], dy_create_done(item=DY_ITEM2), execute=True, agreement=True),
        rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
        rule(["channel-goods-bind"], dy_bind_done(), execute=True))[1:]])
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == "NOT_RUN" and argvs(node, "--execute") == []
    assert [(s["name"], s["outcome"]) for s in out["steps"]] == [("bound", "NOT_RUN")]
    assert out["problems"][0] == "没有执行：查不到她绑没绑，平台没有动"


def test_a_price_of_the_drafts_above_a_colours_daily_price_is_refused(node, cfg):
    """A unified price (270) above a colour's daily price (269): not a 专属 price for it (review 2026-11-09 #5)."""
    node.rules(hat_rules())
    block = platforms.preview({**HAT, "price": "270"}, cfg, catalog=catalog())
    assert block["verdict"] == "blocked" and len(node.calls()) == 2
    assert f"SKU {DY_SKU2C}（均码米白）：动作里写的渠道价 270.00 元比这个颜色的日常价 269.00 元还高" in block["problems"]
    fixed = platforms.check_action({**HAT, "price": "270"})
    with pytest.raises(ActionError) as e:
        platforms.check_plan(fixed, {**HAT_RUN, "skus": [{"id": DY_SKU2C, "price": "270.00", "stock": 1, "main_price": "269.00"}]})
    assert "比日常价 269.00 元还高" in str(e.value)


def test_the_products_own_prefix_rule_is_its_configs(node, cfg):
    """The read passes the prefix: the product's own limit (title_rule.name_prefix_max_length, the CLI's prefixMax)
    decides, not a hard 12; a product whose config requires a prefix is read with one (review 2026-11-09 #8)."""
    long = {**HAT, "title_prefix": "四个字的专属"}                                       # 14
    node.rules(hat_rules(read=dy_read_preview([(DY_SKU2, "均码黑色", "315.00", 500)], item=DY_ITEM2, prefix="【四个字的专属】",
                                              blockers=["the title prefix is 14 characters long (Han characters count 2): "
                                                        "above the page's limit of 12 for a prefix"])))
    block = platforms.preview(long, cfg, catalog=catalog())
    assert block["verdict"] == "blocked" and len(node.calls()) == 2
    # one line, in Chinese, with a shorter one the shop's way (the CLI's English lines of it go: review 2026-11-09 F7)
    assert block["problems"] == ["标题前缀太长：「【四个字的专属】」是 14 个字符（汉字算 2 个），这个商品最多 12 个：少写几个字，"
                                 "比如「【四个字专属】」（名字留前几个字加「专属」）"]
    assert platforms.prefix_stopped(platforms.check_block(block))
    wide = dict(item=DY_ITEM2, prefix="【四个字的专属】", prefix_max=20)
    node.rules(hat_rules(read=dy_read_preview([(DY_SKU2, "均码黑色", "315.00", 500)], **wide),
                         agreed=dy_agreed_preview([(DY_SKU2, "均码黑色", "315.00", "283.00", 100, 500)], **wide)))
    block = platforms.preview(long, cfg, catalog=catalog())
    assert block["verdict"] == "ready", block["problems"]
    assert {f["label"]: f["value"] for f in block["facts"]}["标题前缀"] == "【四个字的专属】（14/20 个字符，汉字算 2 个）"
    required = ("the platform requires a custom title prefix for this product (title_rule.name_prefix_required: the page "
                "will not save an empty 标题前缀): give one with --title-prefix")
    node.rules(hat_rules(read=dy_read_preview([(DY_SKU2, "均码黑色", "315.00", 500)], item=DY_ITEM2, prefix=None,
                                              blockers=[required])))
    block = platforms.preview({**HAT, "exclusive": False, "title_prefix": ""}, cfg, catalog=catalog())
    assert block["verdict"] == "blocked"
    assert any(p.startswith("这个商品要求写标题前缀（title_prefix") for p in block["problems"])


def test_a_daily_price_that_moved_inside_the_clis_own_run_stops_before_the_bind(node, cfg, tmp_path):
    """The look just before agreed; the CLI's own read inside the create saw another daily price: the channel product
    is made with the approved prices, but not bound and no link goes out: PARTIAL (review 2026-11-09 #9)."""
    made = dy_create_done(item=DY_ITEM2)
    made["channelGoods"]["skus"] = [{"skuId": DY_SKU2, "name": "均码黑色", "mainPrice": "330.00", "channelPrice": "283.00",
                                     "channelStock": "增 100", "assignableStock": 500},
                                    {"skuId": DY_SKU2B, "name": "均码咖啡", "mainPrice": "290.00", "channelPrice": "261.00",
                                     "channelStock": "增 30", "assignableStock": 30}]
    node.rules(hat_rules(rule(["channel-goods-create"], made, execute=True, agreement=True),
                         rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
                         rule(["channel-goods-bind"], dy_bind_done(), execute=True)))
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == "PARTIAL" and argvs(node, "channel-goods-bind") == []
    assert out["created"]["channel_id"] == DY_CHANNEL and out["created"]["link"].endswith(f"channel_id={DY_CHANNEL}&channel_type=4")
    assert "日常价和批准时不一样了（SKU 3100000000000000202 的日常价变了：315.00 → 330.00 元" in out["problems"][0]
    assert "没有绑定达人、没有发链接" in out["problems"][0]


LEFT = "7200000000000077"
LEFTOVER_DONE = (rule(["channel-goods-create"], dy_create_done(item=DY_ITEM2), execute=True, agreement=True),
                 rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
                 rule(["channel-goods-bind"], dy_bind_done(), execute=True))


@pytest.mark.parametrize("listed, outcome, words", [
    (dy_bound_list((LEFT, 4), item=DY_ITEM2), "NOT_RUN", f"上次建好、没绑上的渠道品 {LEFT} 还在：先在抖店绑上她或删掉"),
    ({**dy_bound_list(item=DY_ITEM2), "complete": False}, "NOT_RUN",
     f"查不到上次建好、没绑上的渠道品 {LEFT} 还在不在（列表没读全）"),
    ({"site": "fxg", "command": "channel-goods-list", "error": "SCHEMA_MISMATCH"}, "NOT_RUN", "（命令没有给出列表）"),
    (dy_bound_list(("7200000000000001", 2), item=DY_ITEM2), "VERIFIED_SUCCESS", ""),        # only another one: gone
], ids=["still-there", "not-complete", "no-rows", "gone"])
def test_a_channel_product_left_unbound_stops_another_until_it_is_gone(node, cfg, tmp_path, listed, outcome, words):
    """review 2026-11-09 F3: the approval names what earlier ones made and did not bind (the bot's record:
    ``context.leftover``).  Before a create the product's channel products are listed without the UID filter (read
    only: the bound check before it, filtered on her UID, cannot see an unbound one); one of those still listed stops
    it, nothing written; a list that does not say stops it too; gone, the create goes on."""
    node.rules([rule(["channel-goods-list", f"--uid={DY_UID}"], dy_bound_list(item=DY_ITEM2)),
                rule(["channel-goods-list"], listed), *hat_rules()[1:], *LEFTOVER_DONE])
    out = platforms.execute(HAT, cfg, plan=HAT_RUN, key=KEY, ledger=tmp_path / "ledger", catalog=catalog(),
                            context={"packet_id": 3, "leftover": [LEFT, "x1", 7]})
    assert out["outcome"] == outcome, out
    [unfiltered] = [a for a in argvs(node, "channel-goods-list") if not any(x.startswith("--uid=") for x in a)]
    assert f"--product-id={DY_ITEM2}" in unfiltered and "--execute" not in unfiltered
    if outcome == "NOT_RUN":
        assert out["problems"] == [f"没有执行：{words}，平台没有动"] if words.startswith("上次") else words in out["problems"][0]
        assert argvs(node, "channel-goods-create", "--execute") == []
        assert [s["name"] for s in out["steps"]][-1] == "leftover"
    else:
        assert out["created"]["link"] and argvs(node, "channel-goods-bind", "--execute")


def test_without_a_leftover_the_product_is_not_listed_again(node, cfg, tmp_path):
    """Nothing named (the bot knows of no channel product left unbound): only the bound check lists, with her UID."""
    node.rules(hat_rules(*LEFTOVER_DONE))
    out = platforms.execute(HAT, cfg, plan=HAT_RUN, key=KEY, ledger=tmp_path / "ledger", catalog=catalog(),
                            context={"packet_id": 3, "leftover": "7200000000000077"})       # (not a list: ignored)
    assert out["outcome"] == "VERIFIED_SUCCESS"
    assert all(f"--uid={DY_UID}" in a for a in argvs(node, "channel-goods-list"))


def test_many_colours_without_stock_keep_every_price_line(node, cfg):
    """30 colours, 25 of them with nothing to spare: one warning line names them all, so every colour's price line
    and 「批准以后」 stay within the 40 facts the bot keeps (review 2026-11-09 #10)."""
    rows = [(str(3100000000000000300 + i), f"色{i}", "315.00", 0 if i >= 5 else 500) for i in range(30)]
    node.rules(hat_rules(read=dy_read_preview(rows, item=DY_ITEM2),
                         agreed=dy_agreed_preview([(s, n, m, "283.00", 0 if a == 0 else 100, a) for s, n, m, a in rows],
                                                  item=DY_ITEM2)))
    block = platforms.preview(HAT, cfg, catalog=catalog())
    assert block["verdict"] == "ready", block["problems"]
    facts = block["facts"]
    assert len(facts) <= 40 and facts[-1]["label"] == "批准以后"
    assert sum(1 for f in facts if f["label"].startswith("SKU 3100")) == 30
    [warned] = [f["value"] for f in facts if f["label"] == "提醒"]
    assert warned.startswith("主商品这 25 个颜色没有可分的库存，渠道品里这些颜色 0 件：SKU 3100000000000000305（色5）")
    assert len(platforms.check_block(block)["facts"]) == len(facts)


def test_a_ten_colour_links_facts_are_few_and_whole(node, cfg):
    """A 10-colour link (the wechat-review branch lists a draft's facts in WeChat and refuses 「执行」 when they are cut):
    18 facts with the real CLI's three page hints (here its fake gives two: 17), one more per kind of warning."""
    rows = [(str(3100000000000000400 + i), f"色{i}", "315.00", 500) for i in range(10)]
    node.rules(hat_rules(read=dy_read_preview(rows, item=DY_ITEM2),
                         agreed=dy_agreed_preview([(s, n, m, "283.00", 100, a) for s, n, m, a in rows], item=DY_ITEM2)))
    block = platforms.preview(HAT, cfg, catalog=catalog())
    labels = [f["label"] for f in block["facts"]]
    assert block["verdict"] == "ready" and len(labels) == 17
    assert labels[:4] == ["渠道类型", "标题前缀", "有效期", "SKU"] and labels[-1] == "批准以后"
    assert labels.count("页面提示") == 2 and sum(1 for x in labels if x.startswith("SKU 3100")) == 10


def test_an_approval_of_the_shops_way_is_refused_by_a_worker_from_before_it(node, cfg, tmp_path):
    """review 2026-11-09 #6: an old Mac (feb20a0) must not run a new approval its old way (达人带货: price following
    the main product, shared stock).  The new action always carries ``convention``, a key the old check refuses."""
    import importlib.util

    try:
        old = subprocess.run(["git", "show", "feb20a0:src/prm/platforms.py"], capture_output=True, text=True, cwd=ROOT,
                             timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("no git")
    if old.returncode != 0:
        pytest.skip("feb20a0 is not in this checkout")
    path = tmp_path / "platforms_feb20a0.py"
    path.write_text(old.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("platforms_feb20a0", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shared = platforms.check_action({**HAT, "exclusive": False, "title_prefix": ""})
    assert "title_prefix" not in shared and shared["convention"] == 1
    with pytest.raises(module.ActionError) as e:
        module.check_action(shared)
    assert e.value.code == "extra_param" and "convention" in str(e.value)
    out = module.execute(shared, cfg, plan={"steps": ["create", "bind"]}, key=KEY, ledger=tmp_path / "ledger",
                         catalog=catalog())
    assert out["outcome"] == "NOT_RUN" and node.calls() == []


def test_the_agreement_file_is_removed_whatever_the_run_did(cfg):
    seen = []

    def broken(argv, platform, execute):
        path = next(a for a in argv if a.startswith("--agreement=")).split("=", 1)[1]
        seen.append((path, oct(os.stat(path).st_mode & 0o777), json.loads(Path(path).read_text(encoding="utf-8"))))
        raise RuntimeError("the CLI could not start")

    with pytest.raises(RuntimeError):
        platforms._dy_agreement_run(cfg, HAT_AGREEMENT, broken, execute=True)
    [(path, mode, written)] = seen
    assert mode == "0o600" and written == HAT_AGREEMENT and not os.path.exists(path)


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
    """Approved: the agreement previewed once more (the same file contents), the channel product created with it
    once, then the bind previewed and written; the link comes back, each temp file gone."""
    node.rules(hat_rules(rule(["channel-goods-create"], dy_create_done(item=DY_ITEM2), execute=True, agreement=True),
                         rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
                         rule(["channel-goods-bind"], dy_bind_done(), execute=True)))
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS", out
    assert out["created"]["link"] == DY_LINK and out["created"]["channel_id"] == DY_CHANNEL
    names = [(s["name"], s["execute"]) for s in out["steps"]]
    assert names == [("bound", False), ("create", False), ("create", True), ("bind", False), ("bind", True)]
    looked, made = [c for c in node.calls() if c["argv"][2] == "channel-goods-create"]   # no read of the product again
    assert looked["agreement"] == made["agreement"] == HAT_AGREEMENT
    assert "--execute" not in looked["argv"] and made["argv"][1:] == [
        "fxg", "channel-goods-create", "--agreement=" + made["agreement_path"], "--execute"]
    assert made["agreement_mode"] == "0o600" and not os.path.exists(made["agreement_path"])
    assert not os.path.exists(looked["agreement_path"])
    bind = argvs(node, "channel-goods-bind", "--execute")[0]
    assert bind[1:] == ["fxg", "channel-goods-bind", f"--product-id={DY_ITEM2}", f"--channel-id={DY_CHANNEL}",
                        f"--uid={DY_UID}", "--execute"]
    ledger = json.loads((tmp_path / "ledger" / f"{KEY}.json").read_text(encoding="utf-8"))
    assert ledger["status"] == "done" and ledger["record"]["outcome"] == "VERIFIED_SUCCESS" and ledger["packet_id"] == 3
    assert ledger["run"] == HAT_RUN                                       # what was approved, kept with it
    assert oct((tmp_path / "ledger" / f"{KEY}.json").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("look, words", [
    (dict(stdout=hat_agreed(main=("299.00", "290.00", "269.00"))), "日常价变了：315.00 → 299.00 元"),
    (dict(stdout=dy_agreed_preview([(DY_SKU2, "均码黑色", "315.00", "283.00", 100, 500)], item=DY_ITEM2)), "没有 SKU"),
    (dict(stdout=hat_agreed(blockers=["SKU 3100000000000000201: 增 100 is above its assignable stock 80"])), "没通过"),
    (dict(stderr="[NOT_LOGGED_IN] the 抖店 tab is on a login page; nothing was requested\n", exit=77), "没通过"),
], ids=["daily-price-moved", "a-colour-gone", "stock-no-longer-covers-it", "login"])
def test_what_changed_since_the_preview_stops_the_execution_before_anything_is_written(node, cfg, tmp_path, look, words):
    node.rules([rule(["channel-goods-list"], dy_bound_list(item=DY_ITEM2)),
                rule(["channel-goods-create"], execute=False, agreement=True, **look),
                rule(["channel-goods-create"], dy_create_done(item=DY_ITEM2), execute=True, agreement=True),
                rule(["channel-goods-bind"], dy_bind_done(), execute=True)])
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == "NOT_RUN", out
    assert argvs(node, "--execute") == [] and [(s["name"], s["execute"], s["outcome"]) for s in out["steps"]] == [
        ("bound", False, ""), ("create", False, "NOT_RUN")]
    assert out["problems"][0].startswith("没有执行：建渠道品之前按批准的约定再预览了一次") and words in out["problems"][0]
    assert "平台没有动" in out["problems"][0]


def test_an_approval_from_before_the_agreement_is_not_run(node, cfg, tmp_path):
    """An approval whose preview carried no agreement (a worker from before 2026-11-09): nothing runs."""
    out = execute(cfg, HAT, {"steps": ["create", "bind"]}, tmp_path)
    assert out["outcome"] == "NOT_RUN" and node.calls() == [] and "点「重新预览」" in out["problems"][0]
    assert not (tmp_path / "ledger" / f"{KEY}.json").exists()               # the key is not taken


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
    rules = hat_rules(rule(["channel-goods-create"], create, execute=True, agreement=True))
    rules.append(rule(["channel-goods-bind"], bind_preview if bind_preview is not None else dy_bind_preview(),
                      execute=False))
    rules.append(rule(["channel-goods-bind"], dy_bind_done(), execute=True))
    node.rules(rules)
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == outcome, out
    assert len(argvs(node, "channel-goods-bind")) == bind_calls
    assert len(argvs(node, "channel-goods-bind", "--execute")) == 0
    if outcome != "VERIFIED_SUCCESS":
        assert out["problems"] or any(s["problems"] for s in out["steps"])


def test_a_channel_product_made_whose_creator_the_platform_refuses_ends_failed_with_the_channel_named(node, cfg,
                                                                                                    tmp_path):
    """Create ok, bind refused (the platform's 「无法创建提示」: no active commission plan for her): the channel product
    is there, unbound; the record says FAILED with the channel id, so the operator can find it (the bot marks it
    red as a result that may have written: test_prm_platform_flow)."""
    refused = dy_bind_done("FAILED")
    refused["items"][0]["detail"] = "the platform refused this UID (data.failed): 达人无生效中的佣金计划"
    refused["channelGoods"]["failedTalents"] = [{"uid": DY_UID, "reason": "达人无生效中的佣金计划"}]
    node.rules(hat_rules(rule(["channel-goods-create"], dy_create_done(item=DY_ITEM2), execute=True, agreement=True),
                         rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
                         rule(["channel-goods-bind"], refused, execute=True)))
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == "FAILED", out
    assert [(s["name"], s["execute"], s["outcome"]) for s in out["steps"]] == [
        ("bound", False, ""), ("create", False, ""), ("create", True, "VERIFIED_SUCCESS"), ("bind", False, ""),
        ("bind", True, "FAILED")]
    assert out["created"]["channel_id"] == DY_CHANNEL and out["created"]["link"] == DY_LINK
    assert any("达人无生效中的佣金计划" in p for p in out["steps"][-1]["problems"])


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


@pytest.mark.parametrize("fresh", [
    dict(stdout=dy_designated_preview(addable=False)),
    dict(stderr="[NOT_LOGGED_IN] the 百应 (buyin) tab is on a login page; nothing was requested\n", exit=77),
], ids=["creator-refused", "login"])
def test_a_promotion_whose_fresh_commission_preview_fails_is_partial(node, cfg, tmp_path, fresh):
    """The promotion is made and verified, but the 专属佣金's fresh preview does not pass: PARTIAL (the
    promotion stays), and the commission is never written."""
    node.rules([rule(["promotion-create"], dy_promotion("VERIFIED_SUCCESS"), execute=True),
                rule(["promotion-strategy-create-designated"], execute=False, **fresh),
                rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    out = execute(cfg, DESIGNATED, {"steps": ["promotion", "designated"]}, tmp_path)
    assert out["outcome"] == "PARTIAL", out
    assert [(s["name"], s["execute"], s["outcome"]) for s in out["steps"]] == [
        ("promotion", True, "VERIFIED_SUCCESS"), ("designated", False, "NOT_RUN")]
    assert argvs(node, "promotion-strategy-create-designated", "--execute") == []
    assert out["problems"][0].startswith("推广建好了，但专属佣金的预览没通过")
    entry = json.loads((tmp_path / "ledger" / f"{KEY}.json").read_text(encoding="utf-8"))
    assert entry["status"] == "done" and entry["record"]["outcome"] == "PARTIAL"


def test_a_channel_product_whose_answer_has_no_id_is_partial_and_binds_nobody(node, cfg, tmp_path):
    made = dy_create_done(item=DY_ITEM2)
    made["channelGoods"]["created"] = {"productId": DY_ITEM2}                # verified, but no channelId
    node.rules(hat_rules(rule(["channel-goods-create"], made, execute=True, agreement=True),
                         rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
                         rule(["channel-goods-bind"], dy_bind_done(), execute=True)))
    out = execute(cfg, HAT, HAT_RUN, tmp_path)
    assert out["outcome"] == "PARTIAL" and "没有渠道品 ID" in out["problems"][0]
    assert argvs(node, "channel-goods-bind") == [] and "channel_id" not in out["created"]
    assert [s["name"] for s in out["steps"]] == ["bound", "create", "create"]


def test_a_failure_inside_the_execution_is_indeterminate_and_the_ledger_is_settled(node, cfg, tmp_path, monkeypatch):
    """A bug after the write went out (here: reading its answer): what the platform did is not known —
    INDETERMINATE, never NOT_RUN — and the ledger entry is closed, so the key is not run again."""
    def broken(data):
        raise KeyError("createdStrategies")

    monkeypatch.setattr(platforms, "_dy_created", broken)
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    out = execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)
    assert out["outcome"] == "INDETERMINATE" and "Mac 上执行出错" in out["problems"][-1]
    assert len(argvs(node, "--execute")) == 1
    entry = json.loads((tmp_path / "ledger" / f"{KEY}.json").read_text(encoding="utf-8"))
    assert entry["status"] == "done" and entry["record"]["outcome"] == "INDETERMINATE" and entry["finished_at"]
    again = execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)
    assert again["outcome"] == "ALREADY_RUN" and again["earlier"]["outcome"] == "INDETERMINATE"
    assert len(argvs(node, "--execute")) == 1


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


TB_READBACK = ("ok: false\nerror:\n  code: COMMAND_EXEC\n  message: 'campaign 90000000555 was created; reading it back "
               "failed: socket hang up'\n  exitCode: 1\n")


@pytest.mark.parametrize("answer, outcome", [
    (dict(stderr=TB_PRECHECK, exit=2), "FAILED"),                                   # CREATE_PRECHECK_*: not sent
    (dict(stderr=TB_READBACK, exit=1), "INDETERMINATE"),                            # created, the read-back failed
    (dict(stderr="ok: false\nerror:\n  code: ARGUMENT\n  message: 'bad --crowd'\n  exitCode: 2\n", exit=2),
     "INDETERMINATE"),
    (dict(stderr="ok: false\nerror:\n  code: AUTH_REQUIRED\n  message: '[NOT_LOGGED_IN] …'\n  exitCode: 77\n", exit=77),
     "INDETERMINATE"),
    (dict(stderr="TypeError: cannot read properties of undefined\n", exit=1), "INDETERMINATE"),
], ids=["precheck", "readback-error", "argument", "login", "no-envelope"])
def test_a_campaign_without_result_rows_is_failed_only_on_a_precheck(node, cfg, tmp_path, answer, outcome):
    """oc.mjs can exit 1 after it created the plan (a read-back error); its contract: only an envelope whose
    code is CREATE_PRECHECK_* says the create request was not sent, an exit code proves nothing."""
    node.rules([rule(["create-display-custom-campaign"], execute=True, **answer)])
    out = execute(cfg, CAMPAIGN, {"steps": ["create"], "name": "达人合作7_10261015"}, tmp_path)
    assert out["outcome"] == outcome, out
    assert ("请到万相台核对" in " ".join(out["problems"])) == (outcome == "INDETERMINATE")
    assert out["steps"][0]["problems"] and len(argvs(node, "--execute=true")) == 1


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


def test_two_platform_commands_never_run_at_once(tmp_path):
    """The worker's previews and executions and --sync-products share one CliLock (the CLIs share the
    browser tabs): runs wait for each other; a wait that runs out raises TimeoutError, holding nothing."""
    lock = tmp_path / "platform" / "cli.lock"
    spans = []

    def run():
        with platforms.CliLock(lock, wait=30):
            started = time.monotonic()
            platforms.run_cli([sys.executable, "-c", "import time; time.sleep(0.3)"], tmp_path, timeout=30)
            spans.append((started, time.monotonic()))

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    spans.sort()
    assert len(spans) == 3 and all(first[1] <= second[0] for first, second in zip(spans, spans[1:]))
    with platforms.CliLock(lock, wait=30):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with platforms.CliLock(lock, wait=0.3):
                pass
        assert time.monotonic() - started >= 0.3
    with platforms.CliLock(lock, wait=0.3):                                  # let go: free again
        pass


def test_a_cancel_while_waiting_for_the_lock_ends_the_wait(tmp_path):
    """The heartbeat says the job is cancelled while another command holds the lock: the wait ends with
    Cancelled (it used to go on and take the lock, mac review C#1); a heartbeat that fails or says no keeps waiting."""
    lock = tmp_path / "platform" / "cli.lock"
    beats = []
    with platforms.CliLock(lock, wait=30):
        started = time.monotonic()
        with pytest.raises(platforms.Cancelled, match="平台没有动"):
            with platforms.CliLock(lock, wait=30, heartbeat=lambda: beats.append(1) or len(beats) >= 2, every=0.05):
                pytest.fail("the lock was taken")
        assert len(beats) == 2 and time.monotonic() - started < 5

        def broken():
            raise OSError("the bot is restarting")

        with pytest.raises(TimeoutError):
            with platforms.CliLock(lock, wait=0.3, heartbeat=broken, every=0.05):
                pass
    with platforms.CliLock(lock, wait=0.3, heartbeat=lambda: True, every=0.05):    # free: no wait, no question
        pass


def test_an_execution_cancelled_before_its_first_write_runs_nothing(node, cfg, tmp_path):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    out = platforms.execute(DESIGNATED, cfg, plan={"steps": ["designated"]}, key=KEY, ledger=tmp_path / "ledger",
                            catalog=catalog(), cancelled=lambda: True)
    assert out["outcome"] == "NOT_RUN" and "开始写平台之前被取消" in out["problems"][0] and node.calls() == []
    assert not (tmp_path / "ledger" / f"{KEY}.json").exists()                  # the key is not used up
    asked = []
    out = platforms.execute(DESIGNATED, cfg, plan={"steps": ["designated"]}, key=KEY, ledger=tmp_path / "ledger",
                            catalog=catalog(), cancelled=lambda: asked.append(1) and False)
    assert out["outcome"] == "VERIFIED_SUCCESS" and asked == [1]               # asked once, before the write


def test_a_ledger_that_cannot_be_settled_keeps_the_outcome(node, cfg, tmp_path, monkeypatch):
    """The write is verified, then the ledger's last word cannot be written: the outcome stands (it used to
    raise, and the worker reported 「没有执行」); the key stays taken all the same."""
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])

    def broken(*args, **kwargs):
        raise TypeError("cannot write it")

    monkeypatch.setattr(platforms, "settle", broken)
    out = execute(cfg, DESIGNATED, {"steps": ["designated"]}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS"
    assert platforms.claimed(tmp_path / "ledger", KEY) and not platforms.claimed(tmp_path / "ledger", "b" * 32)
    assert not platforms.claimed(tmp_path / "ledger", "../ledger/" + KEY)


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


def test_the_bot_holds_a_macs_action_to_the_form_and_the_commission_cap(node):
    cfg = platforms.config(node.settings(max_commission_rate=70, max_day_budget=2000))
    action = platforms.check_action({**DESIGNATED, "commission_rate": 25}, limits=cfg["limits"])
    block = {"version": 1, "action": action, "verdict": "ready", "run": {"steps": ["designated"]}}
    assert platforms.check_block(block)["action"]["commission_rate"] == "25"
    with pytest.raises(ActionError):                    # Mac 改过的 worker 交回 26% 也不收
        platforms.check_block({**block, "action": {**action, "commission_rate": "26"}})
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
            {"item": {"item_id": XHS_ITEM}, "skus": [{"skuId": XHS_SKU, "skuName": "50ml", "price": {"price": 9900}, "available": True,
                                                  "erpCode": "A7261010037A-M"}]}]}),
        rule(["fxg", "products"], {"site": "fxg", "command": "products", "complete": True, "rows": [
            {"productId": DY_ITEM, "name": "测试主品", "status": 0, "checkStatus": 3, "priceLower": 1999, "discountPrice": 0}]}),
        rule(["fxg", "sku-prices", "--raw"], {"site": "fxg", "command": "sku-prices", "rows": [
            {"productId": DY_ITEM, "data": {"spec_prices": [
                {"id": DY_SKU, "price": 1999, "stock_num": 100, "code": "A7260810095A-M",
                 "sell_properties": [{"value_name": "红色"}, {"value_name": "M"}]},
                {"id": "not-a-sku", "price": 1}]}}]}),
        rule(["alimama", "items", "--page=1"], [{"itemId": TB_ITEM, "itemTitle": "连衣裙 夏季", "price": 199.0}]),
    ])
    cat = platforms.sync_catalog(cfg, ["xhs", "dy", "tb"])
    xhs, dy, tb = (cat["platforms"][p] for p in ("xhs", "dy", "tb"))
    assert xhs["ok"] and [i["id"] for i in xhs["items"]] == [XHS_ITEM, other]
    assert xhs["items"][0]["skus"] == [{"id": XHS_SKU, "name": "50ml", "price": "99.00", "on_sale": True,
                                        "code": "A7261010037A-M"}]
    assert xhs["items"][0]["price"] == "99.00"
    link = f"https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id={DY_ITEM}&origin_type=604"
    assert dy["items"] == [{"id": DY_ITEM, "title": "测试主品", "price": "19.99", "link": link,
                            "skus": [{"id": DY_SKU, "name": "红色/M", "price": "19.99", "code": "A7260810095A-M"}]}]
    assert tb["items"][0] == {"id": TB_ITEM, "title": "连衣裙 夏季", "price": "199.00", "skus": []}
    ws = tmp_path / "ws"
    (ws / "kb").mkdir(parents=True)
    paths = platforms.write_catalog(ws, cat)
    md = (ws / "kb/products.md").read_text(encoding="utf-8")
    assert f"| {XHS_ITEM} | 防晒霜 | 99.00 | {XHS_SKU}：50ml 99.00 编码 A7261010037A-M | — | — |" in md   # 小红书：没有链接
    assert "面膜 ｜ 补水" in md and f"| {DY_ITEM} | 测试主品 |" in md and "## 淘宝（tb）— 1 个商品" in md
    assert f"| {DY_SKU}：红色/M 19.99 编码 A7260810095A-M | — | {link} |" in md and "| 款式图 | 链接 |" in md
    assert "照抄这一格，不要自己拼、不要编" in md
    assert platforms.load_catalog(ws)["platforms"]["dy"]["items"][0]["id"] == DY_ITEM and len(paths) == 2
    for argv in (c["argv"] for c in node.calls()):
        assert "--execute" not in argv and "--execute=true" not in argv


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
DY_IMG = "https://p3-aio.ecombdimg.com/obj/ecom-shop-material/jpeg_m_test_main"
XHS_IMG = "http://qimg.xiaohongshu.com/material_space/test-main"


@pytest.mark.parametrize("platform, raw, expected", [
    ("dy", DY_IMG, DY_IMG),
    ("xhs", XHS_IMG, XHS_IMG.replace("http://", "https://")),     # 千帆给 http，换成 https（两种都试过能下）
    ("dy", "https://ecombdimg.com.evil.example/x.jpg", ""),         # 后缀要在主机名的末尾
    ("dy", "https://evil.example/p3-aio.ecombdimg.com/x.jpg", ""),
    ("xhs", DY_IMG, ""),                                            # 别家的 CDN 不认
    ("tb", DY_IMG, ""),                                             # 淘宝没有图片来源
    ("dy", "ftp://p3-aio.ecombdimg.com/x.jpg", ""),
    ("dy", "https://p3-aio.ecombdimg.com/x\n.jpg", ""),
    ("dy", None, ""), ("dy", 42, ""),
])
def test_only_the_platforms_own_image_cdn_is_taken(platform, raw, expected):
    assert platforms.image_url(platform, raw) == expected


def test_the_catalogue_keeps_each_items_main_picture(node, cfg, tmp_path):
    node.rules([
        rule(["items", "--tab=shelf"], {"site": "ark", "complete": True, "items": [
            {"item_id": XHS_ITEM, "item_name": "防晒霜", "min_price": 9900,
             "images": [{"link": XHS_IMG, "width": 1440}, {"link": XHS_IMG + "-2"}]}]}),
        rule(["item", f"--item-id={XHS_ITEM}"], {"site": "ark", "notFound": [], "items": []}),
        rule(["fxg", "products"], {"site": "fxg", "command": "products", "complete": True, "rows": [
            {"productId": DY_ITEM, "name": "测试主品", "priceLower": 1999, "img": DY_IMG, "pics": [DY_IMG]},
            {"productId": DY_ITEM2, "name": "没图的", "priceLower": 999, "img": "https://evil.example/x.jpg"}]}),
        rule(["fxg", "sku-prices", "--raw"], {"site": "fxg", "command": "sku-prices", "rows": []}),
    ])
    cat = platforms.sync_catalog(cfg, ["xhs", "dy"])
    assert cat["platforms"]["xhs"]["items"][0]["image"] == XHS_IMG.replace("http://", "https://")
    dy = cat["platforms"]["dy"]["items"]
    assert dy[0]["image"] == DY_IMG and "image" not in dy[1]
    ws = tmp_path / "ws"
    (ws / "kb").mkdir(parents=True)
    fetched = []
    counts = platforms.fetch_images(ws, cat, fetch=lambda url, platform: fetched.append((url, platform)) or JPEG)
    assert counts == {"fetched": 2, "kept": 0, "failed": 0, "removed": 0}
    assert sorted(fetched) == sorted([(DY_IMG, "dy"), (XHS_IMG.replace("http://", "https://"), "xhs")])
    assert (ws / "assets/products" / f"dy-{DY_ITEM}.jpg").read_bytes() == JPEG
    platforms.write_catalog(ws, cat)
    md = (ws / "kb/products.md").read_text(encoding="utf-8")
    assert f"| {DY_ITEM} | 测试主品 | 19.99 |  | assets/products/dy-{DY_ITEM}.jpg |" in md
    assert f"| {DY_ITEM2} | 没图的 | 9.99 |  | — |" in md and "attachments_to_send" in md
    assert platforms.load_catalog(ws)["platforms"]["xhs"]["items"][0]["image_file"] == f"assets/products/xhs-{XHS_ITEM}.jpg"


def _one(platform, item_id, image, **extra):
    return {"platforms": {platform: {"items": [{"id": item_id, "image": image, **extra}], "notes": []}}}


def test_a_picture_is_fetched_again_only_when_its_address_changed(tmp_path):
    calls = []
    fetch = lambda url, platform: calls.append(url) or JPEG                        # noqa: E731
    first = _one("dy", DY_ITEM, DY_IMG)
    platforms.fetch_images(tmp_path, first, fetch=fetch)
    again = _one("dy", DY_ITEM, DY_IMG)
    assert platforms.fetch_images(tmp_path, again, previous=first, fetch=fetch)["kept"] == 1 and calls == [DY_IMG]
    assert again["platforms"]["dy"]["items"][0]["image_file"] == f"assets/products/dy-{DY_ITEM}.jpg"
    moved = _one("dy", DY_ITEM, DY_IMG + "-new")
    assert platforms.fetch_images(tmp_path, moved, previous=again,
                                  fetch=lambda url, platform: calls.append(url) or PNG)["fetched"] == 1
    assert calls[-1] == DY_IMG + "-new"
    assert (tmp_path / "assets/products" / f"dy-{DY_ITEM}.png").is_file()           # the format from the bytes
    assert not (tmp_path / "assets/products" / f"dy-{DY_ITEM}.jpg").exists()        # the old file went


def test_a_picture_that_does_not_come_keeps_the_last_one_or_has_none(tmp_path):
    first = _one("dy", DY_ITEM, DY_IMG)
    platforms.fetch_images(tmp_path, first, fetch=lambda url, platform: JPEG)

    def broken(url, platform):
        raise RuntimeError("下载不了：timed out")

    moved = _one("dy", DY_ITEM, DY_IMG + "-new")
    lines = []
    counts = platforms.fetch_images(tmp_path, moved, previous=first, fetch=broken, progress=lines.append)
    assert counts["failed"] == 1 and moved["platforms"]["dy"]["items"][0]["image_file"] == f"assets/products/dy-{DY_ITEM}.jpg"
    assert "没下成" in lines[0] and "没下载成" in moved["platforms"]["dy"]["notes"][0]
    assert (tmp_path / "assets/products" / f"dy-{DY_ITEM}.jpg").is_file()
    new = _one("xhs", XHS_ITEM, XHS_IMG)
    assert platforms.fetch_images(tmp_path, new, fetch=lambda url, platform: b"<html>not a picture</html>")["failed"] == 1
    assert "image_file" not in new["platforms"]["xhs"]["items"][0]
    odd = _one("dy", "../../kb/x", DY_IMG)                     # an id that is not letters and digits never names a file
    assert platforms.fetch_images(tmp_path, odd, fetch=lambda url, platform: JPEG)["fetched"] == 0
    assert "image_file" not in odd["platforms"]["dy"]["items"][0]
    assert not (tmp_path / "kb").exists()


def test_pictures_of_items_no_longer_listed_go_and_nothing_else_does(tmp_path):
    folder = tmp_path / "assets/products"
    folder.mkdir(parents=True)
    for name in (f"dy-{DY_ITEM2}.jpg", "运营自己放的.jpg", "dy-notes.txt"):
        (folder / name).write_bytes(JPEG)
    counts = platforms.fetch_images(tmp_path, _one("dy", DY_ITEM, DY_IMG), fetch=lambda url, platform: JPEG)
    assert counts["removed"] == 1
    assert sorted(p.name for p in folder.iterdir()) == sorted([f"dy-{DY_ITEM}.jpg", "运营自己放的.jpg", "dy-notes.txt"])


def test_fetch_image_refuses_a_redirect_off_the_cdn_and_a_file_too_large(monkeypatch):
    import urllib.request

    class Answer:
        def __init__(self, url, body):
            self.url, self.body = url, body

        def geturl(self):
            return self.url

        def read(self, n):
            return self.body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    answers = []

    class Opener:
        def open(self, request, timeout):
            assert timeout == platforms.IMAGE_TIMEOUT_SEC
            return answers.pop(0)

    seen = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: seen.append(handlers) or Opener())
    answers.append(Answer(DY_IMG, JPEG))
    assert platforms.fetch_image(DY_IMG, "dy") == JPEG
    assert isinstance(seen[0][0], urllib.request.ProxyHandler) and seen[0][0].proxies == {}   # no system proxy
    answers.append(Answer("https://evil.example/x.jpg", JPEG))
    with pytest.raises(RuntimeError, match="CDN"):
        platforms.fetch_image(DY_IMG, "dy")
    answers.append(Answer(DY_IMG, b"\xff" * (platforms.IMAGE_MAX_BYTES + 5)))
    with pytest.raises(RuntimeError, match="MB"):
        platforms.fetch_image(DY_IMG, "dy")


def test_a_dy_product_link_is_the_one_douyins_copy_link_gives_and_old_catalogues_get_it_too(tmp_path):
    """抖店「复制链接」的样子（product_url_for_copy），只换商品 ID；10-31 以前同步的目录（JSON 里没有 link）写
    products.md 时也带上；小红书、淘宝、不像抖店商品 ID 的没有链接（2026-10-31，用户：「链接你不能读取吗」）。"""
    assert platforms.product_link("dy", {"id": "3000000000000000101"}) == (
        "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id=3000000000000000101&origin_type=604")
    for platform, item in (("xhs", {"id": XHS_ITEM}), ("tb", {"id": TB_ITEM}), ("dy", {"id": "x?y=1"}), ("dy", {})):
        assert platforms.product_link(platform, item) == ""
    old = {"version": 1, "synced_at": 0, "platforms": {"dy": {"ok": True, "items": [
        {"id": DY_ITEM, "title": "老目录里的主品", "price": "19.99", "skus": []}]}}}
    assert f"| — | https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id={DY_ITEM}&origin_type=604 |" \
        in platforms.catalog_markdown(old)


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
        "cli = {'receiver': {'name': '张*', 'phone': '139****1111', 'region': '广东省 佛山市 南海区', 'address': '某某路 100…（12 字）'}}\n"
        "cut, changed = p.coarse_receivers({'steps': [{'raw': json.dumps(cli, ensure_ascii=False, indent=2), 'facts': [\n"
        "    {'label': '收货', 'value': '张* 139****1111 广东省 佛山市 南海区 某某路 100…（12 字）（新增到达人名下）'}]}]})\n"
        "assert changed and '南海区' not in json.dumps(cut, ensure_ascii=False) and '广东省 佛山市' in cut['steps'][0]['raw']\n"
        f"lock = {str(tmp_path / 'cli.lock')!r}\n"
        "with p.CliLock(lock, wait=5):\n"
        "    try:\n"
        "        with p.CliLock(lock, wait=5, heartbeat=lambda: True, every=0.01):\n"
        "            raise SystemExit('the lock was taken')\n"
        "    except p.Cancelled:\n"
        "        pass\n"
        # 聚水潭寄样单状态：读时间、整理一行、跑一次（这里的「node」就是这个 Python，跑的是假的 bin/jst.mjs）
        "assert p.jst_time('2026-10-27 18:00:00') == 1793095200.0 and p.jst_time(1793095200000) == 1793095200.0\n"
        f"jst = {str(tmp_path / 'jst')!r}\n"
        "import os, pathlib; pathlib.Path(jst, 'bin').mkdir(parents=True)\n"
        "pathlib.Path(jst, 'bin', 'jst.mjs').write_text('import json, sys\\nprint(json.dumps({\"command\": \"samples\", '\n"
        "    '\"rows\": [{\"orderNo\": sys.argv[2].split(\"=\")[1].split(\",\")[0], \"status\": \"SENT\", '\n"
        "    '\"erpOrders\": [{\"logisticsCompany\": \"SF\", \"logisticsNo\": \"SF1234567890\", '\n"
        "    '\"sendTime\": \"2026-10-27 18:00:00\"}], \"daren\": {\"nickName\": \"x\"}}]}))\\n', encoding='utf-8')\n"
        "cfg = {'node': sys.executable, 'dirs': {'jst': pathlib.Path(jst)}, 'env': {}, 'preview_timeout_sec': 30}\n"
        "out = p.sample_statuses(cfg, ['YP2610260000001', 'YP2610260000002'], sleep=lambda s: None)\n"
        "got = p.check_sample_result(json.loads(json.dumps(out)), ['YP2610260000001', 'YP2610260000002'])\n"
        "assert got['orders']['YP2610260000001']['ships'][0]['sent_at'] == 1793095200.0, out\n"
        "assert got['missing'] == ['YP2610260000002'] and 'nickName' not in json.dumps(out), out\n"
        # 邀约卡已经开过的专属佣金：读台账
        f"inv = pathlib.Path({str(tmp_path / 'invites')!r}); inv.mkdir()\n"
        "(inv / 'invites.jsonl').write_text(json.dumps({'at': '2026-10-28T15:00:00+08:00', 'event': 'attempt', "
        "'attempt': 'a1', 'aweme_id': 'test_daren_01', 'products': ['3000000000000000101'], 'commission_rate': 25}) + "
        "'\\n' + json.dumps({'event': 'result', 'attempt': 'a1', 'sent': True, 'outcome': 'VERIFIED_SUCCESS'}) + "
        "'\\n', encoding='utf-8')\n"
        "cover = p.invite_cover(str(inv), '3000000000000000101', douyin_ids=['test_daren_01'])\n"
        "assert cover['state'] == 'covered' and cover['rate'] == 25, cover\n"
        "assert p.covered_of({'by': 'invite', **cover}, a)['rate'] == 25\n"
        # 邀约背景（加百应达人微信第 6 步）：同一份台账
        "hist = p.invite_history(str(inv), ['TEST_DAREN_01'])\n"
        "assert [h['commission_rate'] for h in hist] == [25] and hist[0]['sent'] is True, hist\n"
        "print(a['commission_rate'], r.exit_code, r.stdout.strip(), sys.version_info[:2])\n",
        encoding="utf-8")
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("20 0 1")


def test_the_shops_way_to_a_channel_link_runs_on_the_macs_system_python(node, tmp_path):
    """专属链接 on macOS's /usr/bin/python3 (3.9): the prefix and its count, the 9 折, the preview that reads the product
    and previews with the agreement, the bot's check of it, the execution (look, create, bind) against the fake CLI.
    The long values go into the script ASCII-escaped (``!a``): 3.9 refuses a source line of 1500+ UTF-8 bytes."""
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    node.rules(hat_rules(rule(["channel-goods-create"], dy_create_done(item=DY_ITEM2), execute=True, agreement=True),
                         rule(["channel-goods-bind"], dy_bind_preview(), execute=False),
                         rule(["channel-goods-bind"], dy_bind_done(), execute=True)))
    cases = [["三个字专属", "【三个字专属】"], ["这是一个非常非常长的名字的专属", None], ["「小美专属」", "【小美专属】"],
             ["小美🌸", "【小美🌸】"], ["小美V123专属", None], ["小\u200b美专属", "【小美专属】"], ['"小美专属', "【小美专属】"],
             ["小美\x85专属", None], ["小美\u2028专属", None], ["小美微 信专属", None], ["小美ｖｘ专属", None]]
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('p', {str(ROOT / 'src/prm/platforms.py')!r})\n"
        "p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)\n"
        f"settings = json.loads({json.dumps(node.settings())!a})\n"
        f"catalog = json.loads({json.dumps(catalog(), ensure_ascii=False)!a})\n"
        f"hat = json.loads({json.dumps(HAT, ensure_ascii=False)!a})\n"
        f"cases = json.loads({json.dumps(cases, ensure_ascii=False)!a})\n"
        "for given, want in cases:\n"
        "    try:\n"
        "        got = p.title_prefix_of(given)\n"
        "    except p.ActionError:\n"
        "        got = None\n"
        "    assert got == want, (given, got)\n"
        "assert p.title_length('\\U00020000') == 2 and p.title_length(cases[0][1]) == 12\n"
        "assert [str(p.live_price(m)) for m in ('315', '290', '399', '188', '269')] == ['283', '261', '359', '169', '242']\n"
        "cfg = p.config(settings)\n"
        "block = p.preview(hat, cfg, catalog=catalog)\n"
        "assert block['verdict'] == 'ready', block\n"
        "assert p.check_block(json.loads(json.dumps(block)))['run'] == block['run']\n"
        f"ledger = {str(tmp_path / 'ledger')!r}\n"
        "out = p.execute(block['action'], cfg, plan=block['run'], key='b' * 32, ledger=ledger, catalog=catalog)\n"
        "print(json.dumps({'run': block['run'], 'outcome': out['outcome'], 'link': out['created'].get('link'),\n"
        "                  'steps': [[s['name'], s['execute']] for s in out['steps']], 'version': list(sys.version_info[:2])}))\n",
        encoding="utf-8")
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["version"][0] == 3 and got["run"] == HAT_RUN
    assert got["outcome"] == "VERIFIED_SUCCESS" and got["link"] == DY_LINK
    assert got["steps"] == [["bound", False], ["create", False], ["create", True], ["bind", False], ["bind", True]]
    agreed = [c for c in node.calls() if "agreement" in c]
    assert len(agreed) == 3 and all(c["agreement"] == HAT_AGREEMENT and c["agreement_mode"] == "0o600" for c in agreed)
    assert not any(os.path.exists(c["agreement_path"]) for c in agreed)


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
    ({"receiver": {**RECEIVER, "district": ""}}, "bad_receiver"),          # 聚水潭省市区都要有（东莞空着被拒）
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
    assert out["created"] == {"sample_order_no": JST_ORDER, "creator_uid": DY_UID,       # the 抖音 UID 聚水潭 found
                              "creator_account": "test_daren_01"}                        # and the 抖音号
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
    assert platforms.check_record(json.loads(json.dumps(out)))["created"] == {
        "sample_order_no": JST_ORDER, "creator_uid": DY_UID, "creator_account": "test_daren_01"}


@pytest.mark.parametrize("region, kept", [
    ("广东省 佛山市 南海区", "广东省 佛山市"), ("上海市 上海市 浦东新区", "上海市 上海市"), ("上海 上海市 浦东新区", "上海 上海市"),
    ("北京市 朝阳区", "北京市"), ("佛山市 南海区", "佛山市"), ("广东省 东莞市 长安镇", "广东省 东莞市"),
    ("新疆维吾尔自治区 伊犁哈萨克自治州 伊宁市", "新疆维吾尔自治区 伊犁哈萨克自治州"), ("广东省佛山市南海区", "广东省佛山市"),
    ("西藏自治区 阿里地区 噶尔县", "西藏自治区 阿里地区"), ("香港特别行政区 九龙城区", "香港特别行政区"),
    ("南海区", ""), ("", ""), (None, ""),
])
def test_a_clis_region_keeps_its_province_and_city(region, kept):
    assert platforms.coarse_region(region) == kept


def test_a_receiver_is_cut_the_same_way_in_any_shape_and_any_depth():
    """The one rule for what of a receiver may be kept for good (the bot's 8.2 clean-up, the Mac's ledger and platform
    logs, mac review F8 / C#2): a receiver object at any depth, one in a CLI's JSON kept as text, the 收货 fact."""
    cli = jst_done()                                         # receiver {source, name, phone, region "省 市 区", address}
    record = {"outcome": "VERIFIED_SUCCESS", "steps": [{"raw": json.dumps(cli, ensure_ascii=False, indent=2), "facts": [
        {"label": "收货", "value": "张* 139****1111 广东省 佛山市 南海区 某某路 100…（12 字）（新增到达人名下）"},
        {"label": "快递", "value": "菜鸟顺丰-优惠陆运（SF）"}]}],
        "action": {**SAMPLE, "receiver": {**RECEIVER, "phone": "13900001111"}}}
    out, changed = platforms.coarse_receivers(record)
    assert changed and record["action"]["receiver"]["district"] == "南海区"   # the original is not touched
    assert out["action"]["receiver"] == {"name": "张*", "phone": "139****1111", "province": "广东省", "city": "佛山市",
                                         "district": "", "address": platforms.RECEIVER_CLEARED}
    raw = json.loads(out["steps"][0]["raw"])
    assert raw["receiver"] == {**cli["receiver"], "region": "广东省 佛山市", "address": platforms.RECEIVER_CLEARED}
    assert {k: v for k, v in raw.items() if k != "receiver"} == {k: v for k, v in cli.items() if k != "receiver"}
    assert out["steps"][0]["facts"] == [
        {"label": "收货", "value": "张* 139****1111 广东省 佛山市 （详细地址已清除）（新增到达人名下）"},
        {"label": "快递", "value": "菜鸟顺丰-优惠陆运（SF）"}]
    assert platforms.coarse_receivers(out) == (out, False)                  # again: nothing changes
    assert platforms.coarse_receivers({"receiver": None, "raw": '{"receiver": null}', "a": [1, "x"]})[1] is False
    assert platforms.coarse_receipt("（预览里没有电话，提交时给）") == platforms.RECEIVER_CLEARED   # not the fact's shape
    assert platforms.ledger_action({**SAMPLE, "receiver": {**RECEIVER, "phone": "〈手机1〉"}})["receiver"]["phone"] == "〈手机1〉"


def test_a_sample_order_without_a_receiver_sends_none(node, jcfg, tmp_path):
    """No receiver in the draft (聚水潭 uses the address saved under the creator): neither the preview
    nor the execution names one."""
    bare = {k: v for k, v in SAMPLE.items() if k != "receiver"}
    node.rules([rule(["sample-create"], jst_preview(), execute=False), rule(["sample-create"], jst_done(), execute=True)])
    block = platforms.preview(bare, jcfg, catalog=catalog())
    assert block["verdict"] == "ready" and "receiver" not in block["action"]
    out = execute(jcfg, bare, {"steps": ["create"]}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS" and out["created"]["sample_order_no"] == JST_ORDER
    shown, run = node.calls()
    assert "--execute" not in shown["argv"] and "--execute" in run["argv"]
    assert "receiver" not in shown["spec"] and "receiver" not in run["spec"]
    assert "receiver" not in json.loads((tmp_path / "ledger" / f"{KEY}.json").read_text(encoding="utf-8"))["action"]


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
    rec["created"] = {"sample_order_no": JST_ORDER, "creator_uid": DY_UID}
    assert platforms.check_record(rec)["created"] == {"sample_order_no": JST_ORDER, "creator_uid": DY_UID}
    for odd in ("6aaa5100a553c3001554dc79", "0123456789", "12; rm", ""):
        rec["created"]["creator_uid"] = odd                                 # only a 抖音 UID: digits
        assert platforms.check_record(rec)["created"] == {"sample_order_no": JST_ORDER}



def test_a_sample_order_keeps_the_creator_uid_only_when_it_is_a_douyin_one(node, jcfg, tmp_path):
    answer = jst_done()
    answer["daren"] = {**answer["daren"], "platformUid": None, "referrerId": "6aaa5100a553c3001554dc79"}
    node.rules([rule(["sample-create"], answer, execute=True)])
    out = execute(jcfg, {**SAMPLE, "receiver": {**RECEIVER, "phone": "13900001111"}}, {"steps": ["create"]}, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS" and out["created"] == {"sample_order_no": JST_ORDER,
                                                                        "creator_account": "test_daren_01"}

def test_jushuitan_is_no_catalogue_platform(node, jcfg):
    cat = platforms.sync_catalog(jcfg, ["jst"], previous=catalog())
    assert "jst" not in cat["platforms"] and node.calls() == []
    assert "## 聚水潭" not in platforms.catalog_markdown(cat)                  # 没有聚水潭这一张表（开头说明里提到编码不算）


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
    assert out["outcome"] == "VERIFIED_SUCCESS" and out["created"] == {"sample_order_no": JST_ORDER, "creator_uid": DY_UID,
                                                                        "creator_account": "test_daren_01"}
    [call] = node.calls()
    assert "--execute" in call["argv"] and len(call["spec"]["goods"]) == 4
    assert {g["title"] for g in call["spec"]["goods"]} == {"测试主品", "测试贝雷帽 秋冬羊毛款", "测试药盒帽"}


# ── 聚水潭: where sample orders stand (`jst samples`, read-only; PRM_SAMPLE_SYNC_MIN) ──

ORDER2, ORDER3 = "YP2610260000002", "YP2610260000003"


def test_sample_statuses_reads_each_order_without_the_receiver_or_the_creator(node, jcfg):
    node.rules([rule(["samples"], jst_samples(
        jst_sample_row(JST_ORDER, "SENT", waybills=[("顺丰速运", "SF1234567890", "2026-10-27 18:00:00")],
                       send_time="2026-10-27 18:00:00"),
        jst_sample_row(ORDER2, "SIGNED", waybills=[("中通", "ZT7700000001", 1793095200000)],
                       sign_time="2026-10-29 09:30:00"),
        jst_sample_row(ORDER3, "DELETED", reject="地址不全，重新提交"),
        jst_sample_row("YP2610260000099", "SENT")))])                           # not asked: left out
    slept = []
    out = platforms.sample_statuses(jcfg, [JST_ORDER, ORDER2, ORDER3, JST_ORDER, "--raw", "YP1"], sleep=slept.append)
    [call] = node.calls()
    assert call["argv"] == ["bin/jst.mjs", "samples", f"--order-no={JST_ORDER},{ORDER2},{ORDER3}", "--page-size=50",
                            f"--expect-uid={JST_UID}"]
    assert "--raw" not in call["argv"] and "--execute" not in call["argv"] and slept == []
    assert call["cwd"] == str(node.dirs["jst"].resolve())
    assert out["op"] == "sample_status" and out["stopped"] is None and out["calls"] == 1 and out["missing"] == []
    assert set(out["orders"]) == {JST_ORDER, ORDER2, ORDER3}
    sent = out["orders"][JST_ORDER]
    assert sent["status"] == "SENT" and sent["ships"] == [
        {"carrier": "顺丰速运", "waybill": "SF1234567890", "sent_at": 1793095200.0}]      # Beijing time
    assert sent["sent_at"] == 1793095200.0 and sent["signed_at"] is None and sent["reason"] == ""
    assert out["orders"][ORDER2]["ships"][0]["sent_at"] == 1793095200.0                     # milliseconds
    assert out["orders"][ORDER2]["signed_at"] == 1793237400.0
    assert out["orders"][ORDER3] == {"status": "DELETED", "ships": [], "packages": 0, "unshipped": 0, "sent_at": None,
                                     "signed_at": None, "reason": "地址不全，重新提交"}
    assert (sent["packages"], sent["unshipped"]) == (1, 0)                        # one package, its waybill there
    text = json.dumps(out, ensure_ascii=False)
    for kept_out in ("测试达人", "test_daren_01", "测试抖店", "T100", "红色"):
        assert kept_out not in text
    assert platforms.check_sample_result(json.loads(text), [JST_ORDER, ORDER2, ORDER3])["orders"] == out["orders"]


def test_sample_statuses_asks_in_batches_with_a_pause_between_calls(node, jcfg):
    numbers = [f"YP26102600{i:05d}" for i in range(45)]
    node.rules([rule(["samples"], jst_samples(*[jst_sample_row(n, "WAITING_SEND") for n in numbers]))])
    slept = []
    out = platforms.sample_statuses(jcfg, numbers, sleep=slept.append)
    calls = [c["argv"][2] for c in node.calls()]
    assert [len(c.split("=", 1)[1].split(",")) for c in calls] == [20, 20, 5]
    assert slept == [platforms.SAMPLES_GAP_SEC] * 2 and platforms.SAMPLES_GAP_SEC >= 3
    assert out["calls"] == 3 and len(out["orders"]) == 45
    many = platforms.sample_statuses(jcfg, [f"YP26102601{i:05d}" for i in range(250)], sleep=lambda s: None)
    assert many["asked"] == platforms.MAX_SAMPLE_ORDERS


def test_an_order_not_in_the_answer_is_missing(node, jcfg):
    node.rules([rule(["samples"], jst_samples(jst_sample_row(JST_ORDER, "INIT")))])
    out = platforms.sample_statuses(jcfg, [JST_ORDER, ORDER2], sleep=lambda s: None)
    assert list(out["orders"]) == [JST_ORDER] and out["missing"] == [ORDER2] and out["stopped"] is None


@pytest.mark.parametrize("exit_code, stderr, verdict", [
    (77, "[NOT_LOGGED_IN] 聚水潭登录失效（登录状态已失效）：到调试版 Chrome 里重新登录\n", "login"),
    (76, "[RISK] /api/market/daren/sampleOrder/list 被限流（[10001]请求太频繁啦）：停下\n", "wall"),
    (79, "[WRONG_ACCOUNT] 登录的是账号 900002，不是指定的 900001\n", "wrong_account"),
    (2, "[USAGE] 不认识的参数：--order-no\n", "invalid"),
    (1, "[FAILED] POST /api/market/daren/sampleOrder/list 失败：fetch failed\n", "error"),
])
def test_a_call_that_fails_stops_the_job_as_it_is_and_nothing_is_tried_again(node, jcfg, exit_code, stderr, verdict):
    numbers = [f"YP26102600{i:05d}" for i in range(30)]
    node.rules([rule([f"--order-no={','.join(numbers[20:])}"], stderr=stderr, exit=exit_code),
                rule(["samples"], jst_samples(*[jst_sample_row(n, "SENT") for n in numbers[:20]]))])
    out = platforms.sample_statuses(jcfg, numbers, sleep=lambda s: None)
    assert out["calls"] == 2 and len(node.calls()) == 2                       # the second call failed: no third
    assert len(out["orders"]) == 20 and out["not_asked"] == numbers[20:]
    assert out["stopped"]["verdict"] == verdict and out["stopped"]["exit_code"] == exit_code
    assert stderr.strip()[:20] in out["stopped"]["message"]
    checked = platforms.check_sample_result(json.loads(json.dumps(out)), numbers)
    assert checked["stopped"] == out["stopped"] and checked["not_asked"] == numbers[20:]


def test_no_jushuitan_cli_on_this_mac_asks_nothing(node):
    cfg = platforms.config(node.settings(jst=""))
    out = platforms.sample_statuses(cfg, [JST_ORDER])
    assert node.calls() == [] and out["stopped"]["verdict"] == "unavailable" and out["not_asked"] == [JST_ORDER]
    assert "platforms.jst" in out["stopped"]["message"]


def test_an_answer_without_a_list_is_an_error(node, jcfg):
    node.rules([rule(["samples"], {"command": "samples"})])
    out = platforms.sample_statuses(jcfg, [JST_ORDER])
    assert out["stopped"]["verdict"] == "error" and out["not_asked"] == [JST_ORDER]


@pytest.mark.parametrize("value, expected", [
    ("2026-10-27 18:00:00", 1793095200.0),
    ("2026/10/27 18:00", 1793095200.0),
    ("2026-10-27T10:00:00Z", 1793095200.0),
    ("2026-10-27T18:00:00+08:00", 1793095200.0),
    (1793095200, 1793095200.0),
    (1793095200000, 1793095200.0),
    ("1793095200000", 1793095200.0),
    ("2026-10-27", 1793030400.0),
    (None, None), ("", None), (True, None), ("昨天", None), ("2026-13-45 99:00:00", None), (0, None),
    ("2026-10-27 18:00:00+99:00", None),
])
def test_jushuitans_times_are_read_as_beijing_time(value, expected):
    assert platforms.jst_time(value) == expected


def test_the_bot_keeps_only_what_it_asked_about_in_the_shape_it_uses():
    raw = {"op": "sample_status", "orders": {
        JST_ORDER: {"status": "sent", "ships": [
            {"carrier": "顺丰\n速运", "waybill": "SF1234567890", "sent_at": 1793095200},
            {"carrier": "x", "waybill": "SF1234567890", "sent_at": None},              # the same waybill twice
            {"carrier": "y", "waybill": "SF 123; rm", "sent_at": None},                # not a waybill
            "junk"],
            "sent_at": "yesterday", "signed_at": True, "reason": "x" * 500, "daren": {"nickName": "测试达人"}},
        ORDER2: {"status": "<b>", "ships": "none"},
        "YP2610260000099": {"status": "SIGNED"},                                     # not asked about
        "../etc": {"status": "SENT"}},
        "missing": [ORDER3, "--raw", ORDER3], "not_asked": [JST_ORDER],
        "stopped": {"verdict": "rm -rf", "exit_code": "77", "message": "m" * 999}}
    out = platforms.check_sample_result(raw, [JST_ORDER, ORDER2, ORDER3])
    assert set(out["orders"]) == {JST_ORDER, ORDER2}
    assert out["orders"][JST_ORDER] == {"status": "SENT", "ships": [
        {"carrier": "顺丰 速运", "waybill": "SF1234567890", "sent_at": 1793095200.0}],
        "packages": None, "unshipped": None,                                      # (an answer without the counts)
        "sent_at": None, "signed_at": None, "reason": "x" * 200 + "…"}
    assert out["orders"][ORDER2] == {"status": "", "ships": [], "packages": None, "unshipped": None, "sent_at": None,
                                     "signed_at": None, "reason": ""}
    assert out["missing"] == [ORDER3] and out["not_asked"] == []                   # (answered after all)
    assert out["stopped"] == {"verdict": "error", "exit_code": None, "message": "m" * 300 + "…"}
    for bad in ("not a dict", {"op": "platform_execute"}, None):
        with pytest.raises(ActionError):
            platforms.check_sample_result(bad, [JST_ORDER])


# ── 邀约卡已经开过的专属佣金（批量邀约工具的台账 invites.jsonl，2026-10-29） ──

INVITED = "test_daren_01"                    # the creator's 抖音号 (jst_view's platformNo)


def invite_ledger(folder, *lines):
    """The batch invite tool's ledger (tools/creator_invite, branch creator-invite-batch): one JSON a line."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "invites.jsonl"
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    return path


def card(attempt="9f1c00000001", aweme=INVITED, products=(DY_ITEM, DY_ITEM2), rate=25, sent=True,
         outcome="VERIFIED_SUCCESS", at="2026-10-28T15:00:00+08:00"):
    out = [{"at": at, "event": "attempt", "attempt": attempt, "plan_id": "P20261028-1500-ab12", "aweme_id": aweme,
            "products": list(products), "commission_rate": rate, "category": "small"}]
    if sent != "no result":
        out.append({"at": at, "event": "result", "attempt": attempt, "plan_id": "P20261028-1500-ab12", "aweme_id": aweme,
                    "exit": 0, "outcome": outcome, "platform_writes": 1 if sent else 0, "sent": sent, "timed_out": False})
    return out


def test_a_product_the_invite_card_opened_is_not_opened_again_and_the_cli_is_not_run(node, cfg, tmp_path):
    """衔接（2026-10-29）：抖音命令行 `buyin author-invite --commission-rate 25` 发出邀约卡时，平台马上给这位达人、卡上的
    商品建一条长期有效的指定达人佣金策略。之后这位达人寄样签收，开佣金链条再给同一款开 dy.designated_commission：命令行认不出
    「已经有策略」（只看平台的 check/user，可加时还可能把达人迁到新策略）。Mac 上的预览先查邀约台账：这一款这位达人的邀约卡
    发出去了，就不跑命令行，预览说「邀约卡开过」，机器人当作开好。"""
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    ledger = invite_ledger(tmp_path / "invites", {"at": "2026-10-28T14:59:00+08:00", "event": "run-start"}, *card())
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog(),
                              context={"invite_ledger": str(ledger.parent), "douyin_ids": [INVITED]})
    assert argvs(node, "promotion-strategy-create-designated") == []           # nothing asked of the platform
    assert block["verdict"] == "blocked" and block["covered"] == {
        "by": "invite", "rate": 25, "at": "2026-10-28T15:00:00+08:00", "aweme_id": INVITED,
        "attempt": "9f1c00000001", "outcome": "VERIFIED_SUCCESS"}
    assert "邀约卡" in block["problems"][0] and "25%" in block["problems"][0]
    kept = platforms.check_block(json.loads(json.dumps(block, ensure_ascii=False)))
    assert kept["covered"] == block["covered"] and kept["verdict"] == "blocked"


@pytest.mark.parametrize("lines, why", [
    (card(aweme="someone_else"), "another creator"),
    (card(products=(DY_ITEM2,)), "another product"),
    (card(rate=None), "a card without a 专属佣金"),
    (card(rate=0), "the tool's card without a 专属佣金 (2026-10-29: the rate is talked over on WeChat)"),
    (card(sent=False, outcome="STATE_CONFLICT"), "a card that did not go out"),
    ([], "no card at all"),
])
def test_an_invite_that_did_not_open_this_product_for_this_creator_changes_nothing(node, cfg, tmp_path, lines, why):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    ledger = invite_ledger(tmp_path / "invites", *lines)
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog(),
                              context={"invite_ledger": str(ledger), "douyin_ids": [INVITED]})
    assert block["verdict"] == "ready" and "covered" not in block, why
    assert len(argvs(node, "promotion-strategy-create-designated")) == 1


@pytest.mark.parametrize("sent, outcome", [(None, None), (True, "INDETERMINATE"), ("no result", None)])
def test_an_invite_whose_card_may_have_gone_out_is_said_before_approval(node, cfg, tmp_path, sent, outcome):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    ledger = invite_ledger(tmp_path / "invites", *card(sent=sent, outcome=outcome))
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog(),
                              context={"invite_ledger": str(ledger), "douyin_ids": [INVITED]})
    assert block["verdict"] == "ready" and "covered" not in block                 # a person decides
    [said] = [f for f in block["facts"] if f["label"].startswith("注意")]
    assert "邀约卡" in said["value"] and "不确定" in said["value"] and "百应" in said["value"]


def test_a_card_that_went_out_counts_over_an_earlier_one_whose_result_is_not_known(node, cfg, tmp_path):
    ledger = invite_ledger(tmp_path / "invites", *card(attempt="a1", sent=None, outcome=None, at="2026-10-27T10:00:00+08:00"),
                           *card(attempt="a2", at="2026-10-28T15:00:00+08:00"),
                           *card(attempt="a3", sent="no result", at="2026-10-29T09:00:00+08:00"))
    cover = platforms.invite_cover(ledger, DY_ITEM, douyin_ids=["TEST_DAREN_01"])           # (case is not looked at)
    assert cover["state"] == "covered" and cover["attempt"] == "a2"
    assert platforms.invite_cover(ledger, DY_ITEM3, douyin_ids=[INVITED]) is None              # not on the card
    assert platforms.invite_cover(tmp_path / "nowhere", DY_ITEM, douyin_ids=[INVITED]) is None


def test_the_ledger_is_matched_by_the_uid_when_the_tool_writes_one(node, cfg, tmp_path):
    lines = card(aweme="whoever")
    lines[0]["uid"] = DY_UID
    ledger = invite_ledger(tmp_path / "invites", *lines)
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog(), context={"invite_ledger": str(ledger)})
    assert block["covered"]["rate"] == 25 and node.calls() == []


def test_a_ledger_that_cannot_be_read_is_said_and_the_preview_goes_on(node, cfg, tmp_path):
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    folder = tmp_path / "invites"
    (folder / "invites.jsonl").mkdir(parents=True)                                # not a file
    block = platforms.preview(DESIGNATED, cfg, catalog=catalog(),
                              context={"invite_ledger": str(folder), "douyin_ids": [INVITED]})
    assert block["verdict"] == "ready"
    assert any(f["label"] == "邀约卡台账" and "读不了" in f["value"] for f in block["facts"])
    missing = platforms.preview(DESIGNATED, cfg, catalog=catalog(),
                                context={"invite_ledger": str(tmp_path / "none"), "douyin_ids": [INVITED]})
    assert missing["verdict"] == "ready" and not any(f["label"] == "邀约卡台账" for f in missing["facts"])


def test_the_bot_keeps_a_cover_only_for_a_blocked_designated_commission():
    cover = {"by": "invite", "rate": 25, "at": "2026-10-28T15:00:00+08:00", "aweme_id": INVITED, "attempt": "a1",
             "outcome": "VERIFIED_SUCCESS"}
    base = {"version": 1, "action": DESIGNATED, "verdict": "blocked", "problems": ["邀约卡开过"], "facts": [], "steps": [],
            "run": {}, "previewed_at": 1.0}
    assert platforms.check_block({**base, "covered": cover})["covered"] == cover
    assert "covered" not in platforms.check_block({**base, "verdict": "ready", "run": {"steps": ["designated"]},
                                                   "covered": cover})
    assert "covered" not in platforms.check_block({**base, "action": CHANNEL, "covered": cover})
    assert "covered" not in platforms.check_block({**base, "covered": {**cover, "rate": "lots"}})


def test_a_sample_order_records_the_creators_douyin_id_it_found(node, jcfg, tmp_path):
    node.rules([rule(["sample-create"], jst_done(), execute=True)])
    approved = {**SAMPLE, "receiver": {**RECEIVER, "phone": "13900001111"}}
    out = execute(jcfg, approved, {"steps": ["create"]}, tmp_path)
    assert out["created"]["creator_account"] == "test_daren_01"
    assert platforms.check_record(json.loads(json.dumps(out)))["created"]["creator_account"] == "test_daren_01"
    assert "creator_account" not in platforms.check_record({**out, "created": {"creator_account": "x y; rm"}})["created"]


# ── 邀约背景（加百应达人微信第 6 步，功能 R）：她收到过的邀约卡，给起草用 ─────────────

def plan_file(folder, plan_id="P20261028-1500-ab12", content="您好！我们是测试帽饰品牌，合作细节加微信详聊～"):
    """The batch invite tool's plan beside its ledger: the products' names, each creator's card words (made up)."""
    (folder / "plans").mkdir(parents=True, exist_ok=True)
    plan = {"plan_id": plan_id, "products": [{"id": DY_ITEM, "name": "测试贝雷帽"}, {"id": DY_ITEM2, "name": "测试药盒帽"}],
            "creators": [{"aweme_id": INVITED, "content": content}, {"aweme_id": "someone_else", "content": "别人的话"}]}
    (folder / "plans" / f"plan-{plan_id}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")


def test_invite_history_gives_her_cards_newest_first_with_the_plans_words_and_names(tmp_path):
    ledger = invite_ledger(tmp_path / "invites", *card(attempt="a1", rate=25, at="2026-10-29T09:00:00+08:00"),
                           *card(attempt="a2", rate=0, at="2026-10-29T15:00:00+08:00"),
                           *card(attempt="a3", sent=False, outcome="STATE_CONFLICT", at="2026-10-29T16:00:00+08:00"),
                           *card(attempt="a4", aweme="someone_else", at="2026-10-29T17:00:00+08:00"))
    plan_file(tmp_path / "invites")
    cards = platforms.invite_history(ledger.parent, ["TEST_DAREN_01"])           # (case is not looked at)
    assert [c["attempt"] for c in cards] == ["a2", "a1"]                            # newest first; not sent: left out
    first = cards[0]
    assert (first["commission_rate"], first["sent"], first["category"], first["plan_id"]) == (
        0, True, "small", "P20261028-1500-ab12")
    assert first["content"] == "您好！我们是测试帽饰品牌，合作细节加微信详聊～"
    assert [(p["id"], p["name"]) for p in first["products"]] == [(DY_ITEM, "测试贝雷帽"), (DY_ITEM2, "测试药盒帽")]
    assert [c["attempt"] for c in platforms.invite_history(ledger, [INVITED], limit=1)] == ["a2"]   # the file, at most 1


def test_invite_history_without_a_plan_a_ledger_or_a_card_for_her(tmp_path):
    ledger = invite_ledger(tmp_path / "invites", *card(sent="no result"),
                           {"at": "2026-10-29T10:00:00+08:00", "event": "attempt", "attempt": "x1", "plan_id": "../../etc",
                            "aweme_id": INVITED, "products": [DY_ITEM], "commission_rate": 0})
    cards = platforms.invite_history(ledger, [INVITED])
    assert [(c["attempt"], c["sent"]) for c in cards] == [("x1", None), ("9f1c00000001", None)]   # (not known)
    assert all(c["content"] == "" and c["products"][0]["name"] == "" for c in cards)              # no plan: ids only
    assert platforms.invite_history(ledger, ["nobody"]) == [] and platforms.invite_history(ledger, []) == []
    assert platforms.invite_history(tmp_path / "nowhere", [INVITED]) == []
    (tmp_path / "invites" / "plans").mkdir()
    (tmp_path / "invites" / "plans" / "plan-P20261028-1500-ab12.json").write_text("{not json", encoding="utf-8")
    assert platforms.invite_history(ledger, [INVITED])[1]["content"] == ""                       # a broken plan
    broken = tmp_path / "broken"
    (broken / "invites.jsonl").mkdir(parents=True)                                                # cannot be read
    with pytest.raises(OSError):
        platforms.invite_history(broken, [INVITED])

