"""Platform actions of a creator deal: the three platform CLIs on the Mac (小红书, 抖音, 淘宝).

When Claude and a creator have agreed in WeChat (the platform, the product, the
commission or the exclusive price, the creator's platform account), the draft may
carry ``platform_action``: one of ``ACTIONS`` with typed parameters, never a command
line.  The drafting Claude stays locked down (no Bash); this module is what runs, on
the Mac (tools/claude_relay/worker.py), in two phases:

1. preview, with the draft: ``check_action`` (types, ranges, limits, and that the
   product is in the catalogue kb/products.json that ``sync_catalog`` wrote), then the
   CLI WITHOUT ``--execute``: it reads the platform and says what it would write and
   what stops it.  The verdict (``ready`` or why not) goes back with the draft; the
   console's 待审批 page shows it, and a platform action is always reviewed.
2. execute, after an operator's 批准并执行: the same CLI WITH ``--execute``, once per
   approval (a ledger file per approval key on the Mac; never retried).  Only
   ``VERIFIED_SUCCESS`` lets the reply go out, with the link filled in
   (``fill_reply``); anything else (ACCEPTED_UNVERIFIED, INDETERMINATE, FAILED,
   STATE_CONFLICT, exit 76 / 77 / 79 …) stops it and the console marks it red.

The CLIs (xiaohongshu-engineering ``node bin/xhs.mjs``, douyin-engineering ``node
bin/dy.mjs``, campaign-engineering ``node bin/oc.mjs``) share one discipline: a write
is a preview unless ``--execute``; it is sent once, read back, and every target gets
an ``outcome``; the result is JSON on stdout.  They run here with an argv list (no
shell), every value as ``--name=value`` and checked first, so nothing the model or a
creator wrote can become an option.

Standard library only, Python 3.9: the Mac worker loads this file by its path with
macOS's /usr/bin/python3; the bot imports it as src.prm.platforms (the console, the
packets, the reply).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

LINK = "{链接}"            # in a reply: the link the platform made, filled in after VERIFIED_SUCCESS
_BRACED = re.compile(r"\{[^{}\s]{1,12}\}")
# What a reply may name, per action: placeholder -> the field of the execution's ``created``
# that replaces it after VERIFIED_SUCCESS.
REPLY_FIELDS = {
    "xhs.direct_plan": {"{计划名}": "plan_name", "{计划ID}": "group_id"},
    "xhs.channel_item": {"{渠道商品ID}": "item_id"},
    "dy.designated_commission": {},
    "dy.channel_goods": {LINK: "link", "{渠道品ID}": "channel_id"},
    "tb.campaign": {"{计划名}": "plan_name", "{计划ID}": "campaign_id"},
}
PLACEHOLDER_LABELS = {LINK: "专属推广链接", "{渠道品ID}": "抖店渠道品 ID", "{渠道商品ID}": "小红书渠道商品 ID",
                      "{计划名}": "计划名", "{计划ID}": "计划 ID"}

PLATFORMS = {"xhs": "小红书", "dy": "抖音", "tb": "淘宝"}
CLI_FILES = {"xhs": "bin/xhs.mjs", "dy": "bin/dy.mjs", "tb": "bin/oc.mjs"}

# Outcomes of a write, as the CLIs say them (one per target), and what this module adds.
VERIFIED = "VERIFIED_SUCCESS"
OUTCOMES = {
    VERIFIED: "写入成功，读回核验通过",
    "NO_CHANGE_NEEDED": "平台上已经是这样，不用写",
    "OBSERVED_DESIRED_STATE": "回执不干净，但读回看到了想要的状态",
    "ACCEPTED_UNVERIFIED": "平台接受了，但读回没证实",
    "CHANGED_WITH_SIDE_EFFECT": "写入了，但有别的变化",
    "INDETERMINATE": "发出了，结果未知",
    "FAILED": "平台拒绝了，或者没有发出",
    "STATE_CONFLICT": "发送前复核发现情况变了，没有发",
    # Not the CLIs' words: what happened when no target outcome was read.
    "PARTIAL": "做了一部分：前面的步骤成功了，后面的没有做（或者缺了回复要用的结果）",
    "NOT_RUN": "没有执行",
    "ALREADY_RUN": "这次批准已经执行过，不再执行",
    "UNKNOWN": "不知道执行了没有",
}
# Outcomes that let the step after it (and the reply) go on.
_DONE = (VERIFIED, "NO_CHANGE_NEEDED")
# The worst first: a run's outcome is the worst of its targets' (and its steps').
_RANK = ("INDETERMINATE", "UNKNOWN", "ACCEPTED_UNVERIFIED", "CHANGED_WITH_SIDE_EFFECT", "OBSERVED_DESIRED_STATE",
         "PARTIAL", "FAILED", "STATE_CONFLICT", "NOT_RUN", "ALREADY_RUN", "NO_CHANGE_NEEDED", VERIFIED)

# What a preview says.
VERDICTS = {
    "ready": "预览通过，可以批准执行",
    "blocked": "平台或工具拦下了，不能执行",
    "invalid": "参数不对，没有预览",
    "unavailable": "这台 Mac 不能做这个动作",
    "login": "平台没登录或登录失效（退出码 77）",
    "wall": "平台要验证码或限流（退出码 76）",
    "wrong_account": "登录的不是指定的账号（退出码 79）",
    "error": "预览出错",
}
EXIT_CODES = {
    0: "完成", 1: "失败或结果未知", 2: "参数错误或发送前被拒绝（什么都没发）",
    76: "验证码或限流：立刻停下，到浏览器里处理", 77: "登录失效：回调试 Chrome 重新登录",
    79: "登录的不是指定的账号", 130: "被打断：写命令可能已经发出，先读回再说",
    # campaign-engineering (OpenCLI): a command that did not start
    69: "uv 没能启动", 75: "整条命令超时", 78: "不在完整的仓库检出里运行",
}
_EXIT_VERDICT = {77: "login", 76: "wall", 79: "wrong_account"}


class ActionError(ValueError):
    """A platform action that cannot be used as given (``code``; the message is Chinese)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── the actions ───────────────────────────────────────────────────────────

ACTIONS = {
    "xhs.direct_plan": {
        "platform": "xhs", "label": "小红书 · 定向计划（给达人专属佣金）",
        "required": ("item_id", "creator_platform_id", "commission_rate"), "optional": (),
        "link": False,
        "effect": "新建一个长期有效的定向计划：指定这位买手带这个商品，按这个佣金率结算（买手可见，对买手有约束力；停止后不能恢复）",
        "caveat": "",
    },
    "xhs.channel_item": {
        "platform": "xhs", "label": "小红书 · 渠道商品（专属价）",
        "required": ("item_id",), "optional": ("sku_prices", "title", "short_title"),
        "link": False,
        "effect": "从主商品建一个渠道商品，审核通过后立即上架开售；专属价只改渠道商品，不动主商品；这条命令不产生链接",
        "caveat": "小红书工具对渠道商品的创建，就算成功读回也只能给 ACCEPTED_UNVERIFIED（「表单常量」「复制的表单」两项永远核验不了），"
                  "按规则回复不会自动发出：执行后请在千帆核对渠道商品，再自己回达人",
    },
    "dy.designated_commission": {
        "platform": "dy", "label": "抖音百应 · 指定达人专属佣金",
        "required": ("item_id", "creator_platform_id", "commission_rate"), "optional": (),
        "link": False,
        "effect": "在百应给这个商品建一条「指定达人佣金」策略（长期有效），只对这位达人生效（页面说提交后次日 0 点生效）",
        "caveat": "商品还没在百应推广时，会先建推广（buyin promotion-create）：这条写命令还没在真实平台上跑过",
    },
    "dy.channel_goods": {
        "platform": "dy", "label": "抖店 · 达人渠道品（专属链接）并绑定达人",
        "required": ("item_id", "creator_platform_id"), "optional": ("price", "sku_ids", "stock", "exclusive"),
        "link": True,
        "effect": "从主商品建一个达人渠道品（新渠道品要平台审核，审核通过后生效），再把这位达人绑上去；两步都核验通过才把推广链接发给达人",
        "caveat": "",
    },
    "tb.campaign": {
        "platform": "tb", "label": "淘宝万相台 · 精准人群推广计划（建完立即暂停）",
        "required": ("item_id", "crowd_ids", "bid_target", "day_budget"), "optional": (),
        "link": False,
        "effect": "在万相台从零新建一个精准人群推广（自定义）计划：建成即投放，命令确认新计划后马上暂停（中间有几秒在投放、可能产生消耗）",
        "caveat": "到目前为止这条命令的真实运行读回都是 ACCEPTED_UNVERIFIED（父资源位读回暂停且锁定等），按规则回复不会自动发出：执行后请在万相台核对",
    },
}
ACTION_NAMES = tuple(ACTIONS)
PARAM_LABELS = {
    "item_id": "商品 ID", "creator_platform_id": "达人平台账号", "commission_rate": "佣金率（%）",
    "sku_prices": "SKU 专属价（元）", "title": "商品标题", "short_title": "导购短标题", "price": "渠道价（元）",
    "sku_ids": "SKU", "stock": "渠道库存", "exclusive": "专属达人带货", "crowd_ids": "达摩盘人群包",
    "bid_target": "出价目标", "day_budget": "日预算（元）", "note": "说明",
}
BID_TARGETS = {"display_pay": "成交", "display_cart": "加购", "display_click": "点击"}

# The JSON schema of ``platform_action`` (the draft's optional field; result.SCHEMA and the
# worker's PRM_SCHEMA carry it).  Which parameters an action takes is checked in
# ``check_action``: the schema keeps every one optional but the action and the item.
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTION_NAMES)},
        "item_id": {"type": "string"},
        "creator_platform_id": {"type": "string"},
        "commission_rate": {"type": "number"},
        "sku_prices": {"type": "array", "items": {
            "type": "object",
            "properties": {"sku_id": {"type": "string"}, "price": {"type": "string"}},
            "required": ["sku_id", "price"], "additionalProperties": False}},
        "title": {"type": "string"},
        "short_title": {"type": "string"},
        "price": {"type": "string"},
        "sku_ids": {"type": "array", "items": {"type": "string"}},
        "stock": {"type": "integer"},
        "exclusive": {"type": "boolean"},
        "crowd_ids": {"type": "array", "items": {"type": "string"}},
        "bid_target": {"type": "string", "enum": list(BID_TARGETS)},
        "day_budget": {"type": "integer"},
        "note": {"type": "string"},
    },
    "required": ["action", "item_id"],
    "additionalProperties": False,
}

# The limits an action is held to besides the platform's own (the Mac's config.json
# "platforms" may lower or raise them).
DEFAULT_LIMITS = {
    "max_commission_rate": 50,      # percent
    "max_day_budget": 300,          # yuan a day
    "min_price_ratio": 0.5,         # an exclusive price at least this share of the catalogue price
    "max_sku_prices": 20,
    "max_crowds": 10,
}
# What the bot holds a Mac's action to: the form and the platforms' own ranges only (the
# Mac's limits are the Mac's: it may have raised them).
FORM_LIMITS = {**DEFAULT_LIMITS, "max_commission_rate": 80, "max_day_budget": 100_000_000}

_DIGITS = re.compile(r"^[1-9]\d{3,31}$")              # dy / tb ids (no leading zero)
_HEX24 = re.compile(r"^[0-9A-Za-z]{24}$")              # a 千帆 user id
_XHS_ID = re.compile(r"^[0-9A-Za-z]{24}$")             # an item / SKU id of 千帆
_KEY = re.compile(r"^[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _err(code: str, message: str) -> ActionError:
    return ActionError(code, message)


def _text_id(value, pattern, code: str, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _err(code, f"{what}要是文字")
    text = str(value).strip()
    if not pattern.match(text):
        raise _err(code, f"{what}格式不对：{text[:40]}")
    return text


def _decimal(value, code: str, what: str, places: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _err(code, f"{what}要是数字")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise _err(code, f"{what}不是数字：{str(value)[:20]}") from None
    if not number.is_finite():
        raise _err(code, f"{what}不是数字")
    if number != number.quantize(Decimal(1).scaleb(-places)):
        raise _err(code, f"{what}最多 {places} 位小数：{value}")
    return number


def _plain(number: Decimal) -> str:
    """12.50 -> "12.5", 20.0 -> "20": the shortest exact text of a number."""
    text = format(number.normalize(), "f")
    return text


def _limits(limits: Optional[dict]) -> dict:
    out = dict(DEFAULT_LIMITS)
    for key, value in (limits or {}).items():
        if key in out and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            out[key] = value
    return out


def _free_text(value, code: str, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise _err(code, f"{what}要是文字")
    text = value.strip()
    if not text or len(text) > limit:
        raise _err(code, f"{what}要 1–{limit} 个字")
    if _CONTROL.search(text) or text.startswith("-") or "{" in text or "}" in text:
        raise _err(code, f"{what}里不能有换行、控制字符、花括号，也不能以 - 开头")
    return text


def check_action(raw, *, catalog: Optional[dict] = None, limits: Optional[dict] = None) -> dict:
    """The action as it will be run: ``{"action": name, <its parameters, normalised>}``.

    ``raw`` is what the draft says.  Unknown actions and parameters, missing ones, a wrong
    type, an id that is not one, a number out of range (the platform's own and
    ``limits``) are refused with ``ActionError``.  With ``catalog`` (``load_catalog``)
    the item must be one of the platform's listed products, and SKUs and prices are
    held to it; without it only the form is checked (the bot does that).
    """
    if not isinstance(raw, dict):
        raise _err("not_object", "平台动作要是一个对象")
    name = raw.get("action")
    if name not in ACTIONS:
        raise _err("unknown_action", f"不认识的平台动作：{str(name)[:40]}")
    spec = ACTIONS[name]
    allowed = {"action", "note", *spec["required"], *spec["optional"]}
    extra = sorted(k for k in raw if k not in allowed)
    if extra:
        raise _err("extra_param", f"{name} 不接受这些参数：{'、'.join(extra)}")
    missing = [k for k in spec["required"] if raw.get(k) in (None, "", [])]
    if missing:
        raise _err("missing_param", f"{name} 缺少参数：{'、'.join(PARAM_LABELS.get(k, k) for k in missing)}")
    lim = _limits(limits)
    platform = spec["platform"]
    out: dict = {"action": name}
    if platform == "xhs":
        out["item_id"] = _text_id(raw["item_id"], _XHS_ID, "bad_item", "商品 ID")
    else:
        out["item_id"] = _text_id(raw["item_id"], _DIGITS, "bad_item", "商品 ID")
    item = catalog_item(catalog, platform, out["item_id"]) if catalog is not None else None
    if catalog is not None and item is None:
        raise _err("not_in_catalog", f"商品 {out['item_id']} 不在{PLATFORMS[platform]}的商品目录里（kb/products.md），"
                                     "先在 Mac 上同步商品目录，或者换目录里的商品")
    if name == "xhs.direct_plan":
        # A user id (24 characters) is used as it is; a 小红书号 or nickname is looked up in the preview.
        out["creator_platform_id"] = _free_text(raw["creator_platform_id"], "bad_creator",
                                                "达人的小红书 userId、小红书号或昵称", 40)
        rate = _decimal(raw["commission_rate"], "bad_rate", "佣金率", 1)
        if not (Decimal(0) < rate <= Decimal(80)):
            raise _err("bad_rate", f"小红书定向计划的佣金率要大于 0、最多 80（%），收到 {_plain(rate)}")
        _cap_rate(rate, lim)
        out["commission_rate"] = _plain(rate)
    elif name == "xhs.channel_item":
        prices = raw.get("sku_prices") or []
        if not isinstance(prices, list) or len(prices) > lim["max_sku_prices"]:
            raise _err("bad_prices", f"SKU 专属价要是列表，最多 {lim['max_sku_prices']} 个")
        seen, normal = set(), []
        for entry in prices:
            if not isinstance(entry, dict) or set(entry) - {"sku_id", "price"}:
                raise _err("bad_prices", "每个 SKU 专属价写成 {sku_id, price}")
            sku = _text_id(entry.get("sku_id"), _XHS_ID, "bad_sku", "SKU ID")
            if sku in seen:
                raise _err("bad_prices", f"SKU {sku} 写了两次")
            seen.add(sku)
            price = _price(entry.get("price"), "SKU 专属价")
            _hold_price(item, sku, price, lim)
            normal.append({"sku_id": sku, "price": _plain_price(price)})
        if normal:
            out["sku_prices"] = normal
        for key, limit in (("title", 60), ("short_title", 24)):
            if raw.get(key) not in (None, ""):
                out[key] = _free_text(raw[key], "bad_title", PARAM_LABELS[key], limit)
    elif name == "dy.designated_commission":
        out["creator_platform_id"] = _text_id(raw["creator_platform_id"], _DIGITS, "bad_creator", "达人的抖音 UID（数字）")
        rate = _decimal(raw["commission_rate"], "bad_rate", "佣金率", 0)
        if not (Decimal(1) <= rate <= Decimal(80)):
            raise _err("bad_rate", f"百应指定达人佣金是整数百分比，1–80，收到 {_plain(rate)}")
        _cap_rate(rate, lim)
        out["commission_rate"] = _plain(rate)
    elif name == "dy.channel_goods":
        out["creator_platform_id"] = _text_id(raw["creator_platform_id"], _DIGITS, "bad_creator", "达人的抖音 UID（数字）")
        exclusive = raw.get("exclusive", False)
        if not isinstance(exclusive, bool):
            raise _err("bad_exclusive", "exclusive 要是 true 或 false")
        out["exclusive"] = exclusive
        skus = raw.get("sku_ids") or []
        if not isinstance(skus, list) or len(skus) > lim["max_sku_prices"]:
            raise _err("bad_sku", f"SKU 要是列表，最多 {lim['max_sku_prices']} 个")
        clean = []
        for sku in skus:
            sku = _text_id(sku, _DIGITS, "bad_sku", "SKU ID")
            if sku in clean:
                raise _err("bad_sku", f"SKU {sku} 写了两次")
            if item is not None and item.get("skus") and sku not in {s.get("id") for s in item["skus"]}:
                raise _err("bad_sku", f"SKU {sku} 不是商品 {out['item_id']} 的（商品目录里没有）")
            clean.append(sku)
        if clean:
            out["sku_ids"] = clean
        if raw.get("price") not in (None, ""):
            price = _price(raw["price"], "渠道价")
            for sku in clean or [s.get("id") for s in (item or {}).get("skus") or []] or [None]:
                _hold_price(item, sku, price, lim)
            out["price"] = _plain_price(price)
        stock = raw.get("stock")
        if stock is not None:
            if isinstance(stock, bool) or not isinstance(stock, int) or not (0 <= stock <= 1_000_000):
                raise _err("bad_stock", "渠道库存要是 0–1000000 的整数")
            out["stock"] = stock
        if exclusive and not stock:
            raise _err("bad_stock", "专属达人带货的渠道品不能共用主商品库存：要给 stock（每个 SKU 划多少件）")
    elif name == "tb.campaign":
        crowds = raw.get("crowd_ids")
        if not isinstance(crowds, list) or not crowds or len(crowds) > lim["max_crowds"]:
            raise _err("bad_crowd", f"达摩盘人群包要是 1–{lim['max_crowds']} 个 ID 的列表")
        clean = []
        for crowd in crowds:
            crowd = _text_id(crowd, _DIGITS, "bad_crowd", "人群包 ID")
            if crowd not in clean:
                clean.append(crowd)
        out["crowd_ids"] = clean
        if raw.get("bid_target") not in BID_TARGETS:
            raise _err("bad_bid_target", f"出价目标只能是 {'、'.join(BID_TARGETS)}")
        out["bid_target"] = raw["bid_target"]
        budget = raw.get("day_budget")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise _err("bad_budget", "日预算要是正整数（元/天）")
        if budget > lim["max_day_budget"]:
            raise _err("over_limit", f"日预算 {budget} 元超过这台 Mac 设的上限 {lim['max_day_budget']} 元")
        out["day_budget"] = budget
    if raw.get("note") not in (None, ""):
        if not isinstance(raw["note"], str):
            raise _err("bad_note", "说明要是文字")
        out["note"] = " ".join(raw["note"].split())[:200]
    return out


def _cap_rate(rate: Decimal, lim: dict) -> None:
    if rate > Decimal(str(lim["max_commission_rate"])):
        raise _err("over_limit", f"佣金率 {_plain(rate)}% 超过这台 Mac 设的上限 {lim['max_commission_rate']}%")


def _price(value, what: str) -> Decimal:
    price = _decimal(value, "bad_price", what, 2)
    if not (Decimal("0.01") <= price <= Decimal("9999999.99")):
        raise _err("bad_price", f"{what}要在 0.01–9999999.99 元之间，收到 {value}")
    return price


def _plain_price(price: Decimal) -> str:
    return format(price.quantize(Decimal("0.01")), "f")


def _hold_price(item: Optional[dict], sku: Optional[str], price: Decimal, lim: dict) -> None:
    """An exclusive price is not below ``min_price_ratio`` of the catalogue's (a decimal point
    in the wrong place costs money); a SKU the catalogue does not list is refused."""
    if item is None:
        return
    skus = {s.get("id"): s for s in item.get("skus") or []}
    if sku is not None and skus and sku not in skus:
        raise _err("bad_sku", f"SKU {sku} 不是商品 {item.get('id')} 的（商品目录里没有）")
    listed = (skus.get(sku) or {}).get("price") if sku is not None else item.get("price")
    if listed in (None, ""):
        listed = item.get("price")
    try:
        listed = Decimal(str(listed)) if listed not in (None, "") else None
    except InvalidOperation:
        listed = None
    if listed is not None and listed > 0 and price < listed * Decimal(str(lim["min_price_ratio"])):
        raise _err("price_too_low", f"专属价 {_plain_price(price)} 元低于目录价 {_plain_price(listed)} 元的 "
                                    f"{int(Decimal(str(lim['min_price_ratio'])) * 100)}%，按这台 Mac 的设置不能用")


# ── the catalogue (kb/products.json, written by ``sync_catalog``) ─────────

CATALOG_JSON = "kb/products.json"
CATALOG_MD = "kb/products.md"


def load_catalog(workspace) -> Optional[dict]:
    """The catalogue the last sync wrote, or None when there is none (or it cannot be read)."""
    try:
        data = json.loads((Path(workspace) / CATALOG_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("platforms"), dict) else None


def catalog_item(catalog: Optional[dict], platform: str, item_id: str) -> Optional[dict]:
    if not catalog:
        return None
    for item in ((catalog.get("platforms") or {}).get(platform) or {}).get("items") or []:
        if isinstance(item, dict) and str(item.get("id")) == str(item_id):
            return item
    return None


# ── replies ───────────────────────────────────────────────────────────────

def reply_problems(text: str, action: Optional[dict]) -> list:
    """Why this reply cannot go with this action ([] = it can): it says something, and its
    placeholders are the action's own (``REPLY_FIELDS``)."""
    text = str(text or "")
    out = []
    if not text.strip():
        out.append("回复是空的：执行成功以后要发给达人的话要写上")
    name = (action or {}).get("action")
    fields = REPLY_FIELDS.get(name, {})
    others = sorted({m.group(0) for m in _BRACED.finditer(text)} - set(fields))
    if others:
        allowed = "、".join(fields) or "没有"
        out.append(f"回复里有{label(name)}用不了的占位符：{'、'.join(others)}（能用的：{allowed}）")
    return out


def fill_reply(text: str, action: dict, created: Optional[dict]) -> tuple:
    """The reply as it goes out after VERIFIED_SUCCESS and the placeholders that had no value:
    ``(text, missing)``.  Each placeholder of the action becomes what the platform made
    (``{链接}`` the link, ``{计划名}`` the plan's name …); an action that makes a link gets it
    appended when the reply has no ``{链接}``."""
    text = str(text or "")
    created = created or {}
    name = (action or {}).get("action")
    had_link = LINK in text
    missing = []
    for placeholder, key in REPLY_FIELDS.get(name, {}).items():
        if placeholder not in text:
            continue
        value = str(created.get(key) or "").strip()
        if value:
            text = text.replace(placeholder, value)
        else:
            missing.append(placeholder)
    link = str(created.get("link") or "")
    if (ACTIONS.get(name) or {}).get("link") and link and not had_link:
        text = f"{text.rstrip()}\n{link}"
    return text, missing


def describe(action: Optional[dict], catalog_entry: Optional[dict] = None) -> list:
    """``[{"label", "value"}]``: the action's parameters for a person (the console)."""
    if not action:
        return []
    out = []
    for key, value in action.items():
        if key == "action":
            continue
        if key == "sku_prices":
            value = "；".join(f"{p['sku_id']}：{p['price']} 元" for p in value)
        elif key in ("sku_ids", "crowd_ids"):
            value = "、".join(value)
        elif key == "exclusive":
            value = "是（只给这一位达人）" if value else "否（达人带货，共用主商品库存）"
        elif key == "bid_target":
            value = f"{BID_TARGETS.get(value, value)}（{value}）"
        elif key == "commission_rate":
            value = f"{value}%"
        out.append({"label": PARAM_LABELS.get(key, key), "value": str(value)})
    if catalog_entry and catalog_entry.get("title"):
        out.insert(0, {"label": "商品", "value": str(catalog_entry["title"])[:80]})
    return out


def label(name: str) -> str:
    return (ACTIONS.get(name) or {}).get("label", name)


def outcome_text(outcome: str) -> str:
    return OUTCOMES.get(outcome, outcome)


def worst(outcomes) -> str:
    """The worst of several outcomes (``UNKNOWN`` when there are none)."""
    found = [o for o in outcomes if o]
    if not found:
        return "UNKNOWN"
    return min(found, key=lambda o: _RANK.index(o) if o in _RANK else 0)


# ── the Mac: settings, running a CLI, the ledger ─────────────────────────

try:
    import fcntl
except ImportError:                     # Windows (the bot): nothing here runs there
    fcntl = None

PREVIEW_TIMEOUT_SEC = 300
EXECUTE_TIMEOUT_SEC = 900
STOP_GRACE_SEC = 20
MAX_RAW_CHARS = 20000                   # a CLI's JSON kept with a preview / an execution


def find_node(configured: str = "") -> Optional[str]:
    """The node executable: ``node_bin`` of the settings, PATH, then the usual places."""
    import shutil

    candidates = [configured] if configured else []
    candidates += [shutil.which("node") or "", str(Path.home() / ".local/bin/node"),
                   "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for c in candidates:
        path = os.path.expanduser(c) if c else ""
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def config(settings: dict) -> dict:
    """The Mac's ``platforms`` settings (config.json), checked::

        {"node": "/…/node" | None, "dirs": {"xhs": Path, …}, "missing": {"dy": "why"},
         "limits": {…}, "env": {…}, "dy_expect_shop_id": "…", "tb_shop": "…",
         "preview_timeout_sec": 300, "execute_timeout_sec": 900}

    A platform is usable when its folder holds its CLI (``bin/xhs.mjs`` …) and node is
    found; ``enabled`` says whether any is.
    """
    raw = settings.get("platforms") if isinstance(settings.get("platforms"), dict) else {}
    out = {"node": None, "dirs": {}, "missing": {}, "limits": _limits(raw), "env": {},
           "dy_expect_shop_id": "", "tb_shop": "",
           "preview_timeout_sec": PREVIEW_TIMEOUT_SEC, "execute_timeout_sec": EXECUTE_TIMEOUT_SEC}
    for key in ("preview_timeout_sec", "execute_timeout_sec"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 30 <= value <= 3600:
            out[key] = float(value)
    if isinstance(raw.get("env"), dict):
        out["env"] = {str(k): str(v) for k, v in raw["env"].items()
                      if re.match(r"^[A-Z][A-Z0-9_]{1,63}$", str(k)) and isinstance(v, (str, int))}
    shop = str(raw.get("dy_expect_shop_id") or "").strip()
    if shop and _DIGITS.match(shop):
        out["dy_expect_shop_id"] = shop
    tb_shop = str(raw.get("tb_shop") or "").strip()
    if tb_shop and not tb_shop.startswith("-") and not _CONTROL.search(tb_shop) and len(tb_shop) <= 40:
        out["tb_shop"] = tb_shop
    out["node"] = find_node(str(raw.get("node_bin") or ""))
    for platform, cli in CLI_FILES.items():
        folder = str(raw.get(platform) or "").strip()
        if not folder:
            continue
        path = Path(os.path.realpath(os.path.expanduser(folder)))
        if not (path / cli).is_file():
            out["missing"][platform] = f"{path} 里没有 {cli}"
            continue
        out["dirs"][platform] = path
    if out["dirs"] and out["node"] is None:
        out["missing"].update({p: "找不到 node（在 platforms.node_bin 里写上它的路径）" for p in out["dirs"]})
        out["dirs"] = {}
    out["enabled"] = bool(out["dirs"])
    return out


def cli_env(cfg: dict) -> dict:
    """The CLIs' environment: this process's, node's folder and ~/.local/bin first on PATH
    (the taobao CLI starts uv), plus ``platforms.env`` of the settings."""
    env = dict(os.environ)
    first = [str(Path(cfg["node"]).parent)] if cfg.get("node") else []
    first.append(str(Path.home() / ".local/bin"))
    env["PATH"] = os.pathsep.join([*first, env.get("PATH", "")])
    env.update(cfg.get("env") or {})
    env.setdefault("NO_COLOR", "1")
    return env


class CliRun:
    """One run of a platform CLI: what it printed and how it ended."""

    def __init__(self, argv: list, exit_code: Optional[int], stdout: str, stderr: str, seconds: float,
                 stopped: str = "", error: str = ""):
        self.argv, self.exit_code, self.stdout, self.stderr = argv, exit_code, stdout, stderr
        self.seconds, self.stopped, self.error = seconds, stopped, error

    def command(self) -> str:
        """The command line for a person (node's path shortened to "node")."""
        parts = ["node", *self.argv[1:]] if self.argv else []
        return " ".join(p if re.match(r"^[\w@%+=:,./-]+$", p) else json.dumps(p, ensure_ascii=False) for p in parts)


def _stop(proc: subprocess.Popen) -> None:
    """SIGINT to the command's group (it ends its request cleanly), SIGKILL if it stays."""
    for sig in (signal.SIGINT, signal.SIGKILL):
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, sig)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            return
        try:
            proc.wait(timeout=STOP_GRACE_SEC)
            return
        except subprocess.TimeoutExpired:
            continue


def run_cli(argv: list, cwd, *, timeout: float, env: Optional[dict] = None,
            heartbeat: Optional[Callable[[], bool]] = None, every: float = 30.0,
            interruptible: bool = False) -> CliRun:
    """Run one CLI command (an argv list, no shell) to its end.

    ``heartbeat()`` is called every ``every`` seconds meanwhile (the relay job stays
    alive); it returns True when the job is cancelled, and only an ``interruptible`` run
    (a preview: it only reads) is stopped for that.  A write is stopped only at
    ``timeout`` (then its outcome is unknown: the CLI may have sent it).
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                start_new_session=True)
    except OSError as e:
        return CliRun(argv, None, "", "", 0.0, error=f"启动不了：{e}")
    chunks = {"out": b"", "err": b""}

    def read(name, stream):
        chunks[name] = stream.read()

    readers = [threading.Thread(target=read, args=("out", proc.stdout), daemon=True),
               threading.Thread(target=read, args=("err", proc.stderr), daemon=True)]
    for t in readers:
        t.start()
    deadline = started + float(timeout)
    stopped = ""
    while True:
        try:
            proc.wait(timeout=max(0.05, min(every, deadline - time.monotonic())))
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            stopped = "timeout"
            break
        if heartbeat is not None:
            try:
                cancel = bool(heartbeat())
            except Exception:
                cancel = False
            if cancel and interruptible:
                stopped = "cancelled"
                break
    if stopped:
        _stop(proc)
    for t in readers:
        t.join(timeout=STOP_GRACE_SEC)
    return CliRun(argv, proc.returncode, chunks["out"].decode("utf-8", "replace"),
                  chunks["err"].decode("utf-8", "replace"), round(time.monotonic() - started, 1), stopped)


class CliLock:
    """One platform CLI at a time on this Mac (the worker and ``--sync-products`` share the
    browser tabs): an flock on ``path``, waited for up to ``wait`` seconds (``heartbeat()``
    every 30 s meanwhile: the relay job stays alive)."""

    def __init__(self, path, wait: float = 600.0, heartbeat: Optional[Callable[[], bool]] = None):
        self.path, self.wait, self.handle, self.heartbeat = Path(path), wait, None, heartbeat

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "w")
        if fcntl is None:
            return self
        deadline = time.monotonic() + self.wait
        beat = time.monotonic()
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise TimeoutError(f"等了 {int(self.wait)} 秒，另一个平台命令还在跑（{self.path}）")
                if self.heartbeat is not None and time.monotonic() - beat >= 30:
                    beat = time.monotonic()
                    try:
                        self.heartbeat()
                    except Exception:
                        pass
                time.sleep(0.5)

    def __exit__(self, *exc):
        if self.handle is not None:
            self.handle.close()
        return False


def claim(ledger: Path, key: str, record: dict) -> Optional[dict]:
    """Take the approval ``key`` for one execution: None when it is taken now (``record``
    is written first, mode 0600, created exclusively); the earlier entry when this key
    was taken before (the action is then NOT run again)."""
    if not _KEY.match(str(key)):
        raise _err("bad_key", "批准编号不对")
    ledger = Path(ledger)
    ledger.mkdir(parents=True, exist_ok=True)
    path = ledger / f"{key}.json"
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            earlier = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            earlier = {}
        return earlier if isinstance(earlier, dict) and earlier else {"status": "unreadable"}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    return None


def settle(ledger: Path, key: str, record: dict) -> None:
    """Write the end of an execution into its ledger entry (in place, atomically)."""
    path = Path(ledger) / f"{key}.json"
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(str(part), 0o600)
    os.replace(str(part), str(path))


# ── reading what a CLI printed ────────────────────────────────────────────

def parse_json(text: str):
    """The JSON document a CLI printed on stdout (None when there is none)."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i > 0]
    for start in sorted(starts):
        try:
            return json.loads(text[start:])
        except ValueError:
            continue
    return None


def _clip(value, limit: int = 300) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _stderr_line(stderr: str) -> str:
    """The first ``[CODE] message`` line of stderr (or its first line)."""
    lines = [l.strip() for l in str(stderr or "").splitlines() if l.strip()]
    for line in lines:
        if re.match(r"^\[[A-Z][A-Z_]+\]", line):
            return _clip(line)
    return _clip(lines[0]) if lines else ""


def _may_have_sent(run: CliRun) -> bool:
    """Did a run without a result perhaps send its write?  Interrupted, timed out, or the
    CLI says so (``a write request … may have reached the platform`` / ``in flight``)."""
    text = str(run.stderr or "").lower()
    return (run.exit_code == 130 or run.stopped == "timeout" or run.exit_code is None
            or "may have reached" in text or "in flight" in text or "outcome_unknown" in text)


def _raw(run: CliRun) -> str:
    text = run.stdout if str(run.stdout or "").strip() else run.stderr
    text = str(text or "")
    return text if len(text) <= MAX_RAW_CHARS else text[:MAX_RAW_CHARS] + "\n…（太长，后面截掉了）"


def _exit_problem(run: CliRun) -> str:
    if run.error:
        return run.error
    if run.stopped == "timeout":
        return "命令超时，被停下了"
    if run.stopped == "cancelled":
        return "任务被取消，预览停下了"
    meaning = EXIT_CODES.get(run.exit_code, "")
    line = _stderr_line(run.stderr)
    return _clip(f"退出码 {run.exit_code}{'（' + meaning + '）' if meaning else ''}{'：' + line if line else ''}", 400)


def _step(name: str, label_: str, run: CliRun, execute: bool) -> dict:
    return {"name": name, "label": label_, "execute": execute, "command": run.command(), "exit_code": run.exit_code,
            "seconds": run.seconds, "verdict": "", "outcome": "", "problems": [], "facts": [], "raw": _raw(run)}


def _fact(label_: str, value) -> dict:
    return {"label": _clip(label_, 40), "value": _clip(value, 400)}


def _rows(data, key: str = "items") -> list:
    rows = data.get(key) if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _preview_verdict(run: CliRun, data, rows: list, step: dict) -> str:
    """ready / blocked / login / wall / wrong_account / error of a write command's preview whose
    targets are ``rows`` (``outcome`` PREVIEW; ``blocked`` or ``unlocated`` say what stops it)."""
    if run.exit_code in _EXIT_VERDICT and data is None:
        step["problems"].append(_exit_problem(run))
        return _EXIT_VERDICT[run.exit_code]
    if data is None or run.error or run.stopped:
        step["problems"].append(_exit_problem(run) if (run.exit_code or run.error or run.stopped)
                                else "命令没有输出 JSON")
        return "error"
    if not rows:
        step["problems"].append("预览结果里没有目标行")
        return "error"
    verdict = "ready"
    for row in rows:
        if row.get("blocked") or row.get("unlocated") or row.get("outcome") in ("FAILED", "STATE_CONFLICT"):
            step["problems"].append(_clip(f"{row.get('targetId', '')}：{row.get('detail') or row.get('outcome')}", 500))
            verdict = "blocked"
        elif row.get("detail"):
            step["facts"].append(_fact("将写入", row["detail"]))
    if run.exit_code in _EXIT_VERDICT:
        step["problems"].append(_exit_problem(run))
        return _EXIT_VERDICT[run.exit_code]
    return verdict


def _execute_outcome(run: CliRun, data, rows: list, step: dict) -> str:
    """The outcome of an executed write: the worst of its targets'; without a result, FAILED
    when the CLI stopped before it could send (usage, wall, login, account), else INDETERMINATE."""
    if data is None:
        step["problems"].append(_exit_problem(run))
        if run.exit_code in (2, 76, 77, 79) and not _may_have_sent(run):
            return "FAILED"
        return "INDETERMINATE"
    if not rows:
        step["problems"].append("结果里没有目标行")
        return "INDETERMINATE"
    for row in rows:
        if row.get("outcome") not in _DONE:
            step["problems"].append(_clip(f"{row.get('targetId', '')}：{row.get('outcome')} {row.get('detail') or ''}", 500))
    stop = (data.get("stop") or data.get("stopped")) if isinstance(data, dict) else None
    if isinstance(stop, dict):
        kind = "验证码/限流" if stop.get("kind") == "wall" or stop.get("code") in _WALL_CODES else "登录" \
            if stop.get("kind") == "login" or stop.get("code") in _LOGIN_CODES else "错误"
        step["problems"].append(_clip(f"停在{kind}：{stop.get('message', '')}"))
    outcome = worst(row.get("outcome") for row in rows)
    if outcome not in OUTCOMES:
        step["problems"].append(f"不认识的结果 {outcome}")
        return "INDETERMINATE"
    return outcome


# ── 抖音 (douyin-engineering, node bin/dy.mjs) ─────────────────────────────

DY_STEPS = {
    "designated": ("百应 · 建指定达人佣金策略", ("buyin", "promotion-strategy-create-designated")),
    "promotion": ("百应 · 给商品建推广（还没推广时）", ("buyin", "promotion-create")),
    "create": ("抖店 · 建达人渠道品", ("fxg", "channel-goods-create")),
    "bind": ("抖店 · 渠道品绑定达人", ("fxg", "channel-goods-bind")),
}
_NEEDS_PROMOTION = "create its promotion first"
_LINK_HOSTS = {"dy": (".jinritemai.com", ".douyin.com", ".snssdk.com"),
               "xhs": (".xiaohongshu.com", ".xhslink.com")}


def _dy_argv(cfg: dict, step: str, action: dict, *, execute: bool, channel_id: str = "") -> list:
    a = action
    if step == "designated":
        opts = [f"--id={a['item_id']}", f"--uid={a['creator_platform_id']}", f"--to={a['commission_rate']}",
                "--end=long"]
    elif step == "promotion":
        opts = [f"--id={a['item_id']}"]
    elif step == "create":
        opts = [f"--product-id={a['item_id']}", "--sale-channel=" + ("kol-exclusive" if a.get("exclusive") else "kol")]
        if a.get("sku_ids"):
            opts.append("--sku-id=" + ",".join(a["sku_ids"]))
        if a.get("price"):
            opts.append(f"--price={a['price']}")
        if a.get("stock") is not None:
            opts.append(f"--stock={int(a['stock'])}")
    elif step == "bind":
        if not _DIGITS.match(str(channel_id)):
            raise _err("bad_channel", "渠道品 ID 不对")
        opts = [f"--product-id={a['item_id']}", f"--channel-id={channel_id}", f"--uid={a['creator_platform_id']}"]
    else:
        raise _err("bad_step", f"不认识的步骤 {step}")
    if cfg.get("dy_expect_shop_id"):
        opts.append(f"--expect-shop-id={cfg['dy_expect_shop_id']}")
    return [cfg["node"], CLI_FILES["dy"], *DY_STEPS[step][1], *opts, *(["--execute"] if execute else [])]


def _dy_preview(cfg: dict, step: str, action: dict, run_one, channel_id: str = "") -> tuple:
    """One dy preview: ``(step record, data)``."""
    run = run_one(_dy_argv(cfg, step, action, execute=False, channel_id=channel_id), "dy", False)
    data = parse_json(run.stdout)
    rows = _rows(data)
    rec = _step(step, DY_STEPS[step][0], run, False)
    if step == "designated" and rows and all(r.get("unlocated") and _NEEDS_PROMOTION in str(r.get("detail") or "")
                                             for r in rows):
        rec["verdict"] = "needs_promotion"
        rec["facts"].append(_fact("商品状态", "这个商品还没在百应「已推广商品」里（还没推广），要先建推广"))
    else:
        rec["verdict"] = _preview_verdict(run, data, rows, rec)
    if isinstance(data, dict):
        _dy_facts(step, data, rec, action)
    return rec, data


def _dy_facts(step: str, data: dict, rec: dict, action: dict) -> None:
    """What the operator should see of a dy answer besides its rows."""
    goods = data.get("channelGoods") if isinstance(data.get("channelGoods"), dict) else {}
    if step == "create" and goods:
        channel = goods.get("saleChannel") or {}
        rec["facts"].append(_fact("渠道类型", channel.get("label") or channel.get("option") or ""))
        for sku in goods.get("skus") or []:
            if isinstance(sku, dict):
                rec["facts"].append(_fact(f"SKU {sku.get('skuId')} {sku.get('name') or ''}",
                                          f"主品价 {sku.get('mainPrice')} 元 → 渠道价 {sku.get('channelPrice')}；"
                                          f"库存 {sku.get('channelStock')}（可划 {sku.get('assignableStock')}）"))
        if goods.get("period"):
            rec["facts"].append(_fact("有效期", goods["period"]))
        check = goods.get("priceCheck") if isinstance(goods.get("priceCheck"), dict) else {}
        if check:
            rec["facts"].append(_fact("价格检查", f"{check.get('kind') or check.get('verdict')} "
                                                 + "；".join(_clip(r, 120) for r in check.get("reasons") or [])))
            if check.get("kind") in ("block", "warn") and rec["verdict"] == "ready":
                rec["verdict"] = "blocked"
                rec["problems"].append("平台的价格检查没通过（" + str(check.get("kind")) + "）："
                                       + "；".join(_clip(r, 120) for r in check.get("reasons") or []))
        for text in goods.get("blockers") or []:
            line = _clip(text, 300)
            if line not in rec["problems"]:
                rec["problems"].append(line)
        for text in goods.get("warnings") or []:
            rec["facts"].append(_fact("页面提示", text))
        if goods.get("stockDefaultedToZero"):
            rec["problems"].append("渠道品库存会是 0（专属达人带货不能共用库存）")
            if rec["verdict"] == "ready":
                rec["verdict"] = "blocked"
    elif step == "bind" and goods:
        channel = goods.get("channel") or {}
        if channel:
            rec["facts"].append(_fact("渠道品", f"{channel.get('channelId')} {channel.get('statusName') or ''}"))
        for text in goods.get("warnings") or []:
            rec["facts"].append(_fact("页面提示", text))
        for talent in goods.get("failedTalents") or []:
            if isinstance(talent, dict):
                rec["problems"].append(_clip(f"达人 {talent.get('uid')} 绑定被拒：{talent.get('reason')}"))
    elif step == "designated":
        check = (data.get("creators") or {}).get("check") if isinstance(data.get("creators"), dict) else None
        for verdict in (check or {}).get("verdicts") or []:
            if not isinstance(verdict, dict):
                continue
            text = ("可以加" if verdict.get("addable") else "不能加") + (f"：{verdict.get('reason')}" if verdict.get("reason") else "")
            if verdict.get("reminder"):
                text += f"（平台提醒：{_clip(verdict['reminder'], 200)}）"
            rec["facts"].append(_fact(f"达人 {verdict.get('uid')}", text))
            if verdict.get("addable") is False and rec["verdict"] == "ready":
                rec["verdict"] = "blocked"
                rec["problems"].append(f"平台说达人 {verdict.get('uid')} 不能加进这个策略：{verdict.get('reason') or ''}")
        low = data.get("lowCommission") if isinstance(data.get("lowCommission"), dict) else None
        if low and low.get("floor") not in (None, ""):
            rec["facts"].append(_fact("低佣金规则", f"{low.get('rule')}（底线 {low.get('floor')}%）"))
    elif step == "promotion":
        pass


def _dy_link(data, item_id: str) -> str:
    goods = data.get("channelGoods") if isinstance(data, dict) and isinstance(data.get("channelGoods"), dict) else {}
    link = str(((goods.get("links") or {}).get("channelLink")) or "").strip()
    return check_link(link, "dy")


def check_link(link: str, platform: str) -> str:
    """A link the platform made, fit to go into a WeChat message ("" when it is not one):
    http(s) only, no spaces, on the platform's own hosts."""
    link = str(link or "").strip()
    if not link:
        return ""
    if not re.match(r"^https?://", link):
        link = "https://" + link
    if len(link) > 600 or re.search(r"[\s<>\"'{}]", link):
        return ""
    m = re.match(r"^https?://([^/?#:]+)", link)
    host = ("." + m.group(1).lower()) if m else ""
    return link if host and any(host.endswith(h) for h in _LINK_HOSTS.get(platform, ())) else ""


def _dy_created(data) -> dict:
    """Ids and the link a dy write's result names (channelGoods.created / links, createdStrategies)."""
    out = {}
    if not isinstance(data, dict):
        return out
    goods = data.get("channelGoods") if isinstance(data.get("channelGoods"), dict) else {}
    created = goods.get("created") if isinstance(goods.get("created"), dict) else {}
    if created.get("channelId") and _DIGITS.match(str(created["channelId"])):
        out["channel_id"] = str(created["channelId"])
    if goods.get("review"):
        out["review"] = _clip(goods["review"], 200)
    strategies = [s for s in data.get("createdStrategies") or [] if isinstance(s, dict)]
    ids = [str(s.get("strategyId")) for s in strategies if s.get("strategyId") is not None]
    if ids:
        out["strategy_ids"] = ids[:10]
    return out


def _preview_dy_designated(action: dict, cfg: dict, run_one, block: dict) -> None:
    rec, _ = _dy_preview(cfg, "designated", action, run_one)
    block["steps"].append(rec)
    if rec["verdict"] != "needs_promotion":
        block["run"] = {"steps": ["designated"]}
        return
    prom, data = _dy_preview(cfg, "promotion", action, run_one)
    block["steps"].append(prom)
    if prom["verdict"] == "ready" and any(r.get("outcome") == "NO_CHANGE_NEEDED" for r in _rows(data)):
        prom["verdict"] = "blocked"
        prom["problems"].append("百应的两个命令对这个商品是否已经推广说法不一致，先在百应页面上看一眼")
    rec["verdict"] = prom["verdict"]
    if prom["verdict"] == "ready":
        block["run"] = {"steps": ["promotion", "designated"]}
        block["facts"].append(_fact("批准以后", "先给商品建推广（buyin promotion-create：这条写命令还没在真实平台上跑过），"
                                              "推广建好、读回核验通过以后，再重新预览并建专属佣金；任何一步不成功就停下"))


def _preview_dy_channel_goods(action: dict, cfg: dict, run_one, block: dict) -> None:
    rec, _ = _dy_preview(cfg, "create", action, run_one)
    block["steps"].append(rec)
    block["run"] = {"steps": ["create", "bind"]}
    block["facts"].append(_fact("批准以后", f"先建渠道品；建好且读回核验通过以后，预览并绑定达人 UID {action['creator_platform_id']}；"
                                          "两步都核验通过才把渠道品的推广链接发给达人"))


def _execute_dy(action: dict, cfg: dict, plan: dict, run_one, record: dict) -> None:
    def write(step: str, channel_id: str = "") -> tuple:
        run = run_one(_dy_argv(cfg, step, action, execute=True, channel_id=channel_id), "dy", True)
        data = parse_json(run.stdout)
        rec = _step(step, DY_STEPS[step][0], run, True)
        rec["outcome"] = _execute_outcome(run, data, _rows(data), rec)
        record["steps"].append(rec)
        record["created"].update(_dy_created(data))
        return rec["outcome"], data

    def look(step: str, channel_id: str = "") -> tuple:
        rec, data = _dy_preview(cfg, step, action, run_one, channel_id=channel_id)
        record["steps"].append(rec)
        return rec, data

    name = action["action"]
    if name == "dy.designated_commission":
        if plan["steps"][0] == "promotion":
            outcome, _ = write("promotion")
            if outcome not in _DONE:
                return
            again, _ = look("designated")
            if again["verdict"] != "ready":
                again["outcome"] = "NOT_RUN"
                record["problems"].append("推广建好了，但专属佣金的预览没通过，没有建专属佣金：" + "；".join(again["problems"]))
                return
        write("designated")
        return
    # dy.channel_goods: the channel product, then its talent
    outcome, data = write("create")
    if outcome not in _DONE:
        return
    link = _dy_link(data, action["item_id"])
    channel_id = record["created"].get("channel_id", "")
    if not channel_id:
        record["problems"].append("渠道品建好了，但结果里没有渠道品 ID，没法绑定达人")
        record["incomplete"] = True
        return
    if not link:
        record["problems"].append("渠道品建好了，但结果里没有能发的推广链接（channelGoods.links.channelLink）")
        record["incomplete"] = True
        return
    record["created"]["link"] = link
    bind, data = look("bind", channel_id)
    rows = _rows(data)
    if rows and all(r.get("outcome") == "NO_CHANGE_NEEDED" for r in rows):
        bind["outcome"] = "NO_CHANGE_NEEDED"
        bind["facts"].append(_fact("绑定", "这位达人已经绑在这个渠道品上了，不用再绑"))
        return
    if bind["verdict"] != "ready":
        bind["outcome"] = "NOT_RUN"
        record["problems"].append(f"渠道品 {channel_id} 建好了，但绑定达人的预览没通过，没有绑定：" + "；".join(bind["problems"]))
        return
    write("bind", channel_id)


# ── the two phases ────────────────────────────────────────────────────────

# What an approval may run, per action (the preview picks one and hands it back as ``run``).
PLANS = {
    "xhs.direct_plan": (("create",),),
    "xhs.channel_item": (("create",),),
    "dy.designated_commission": (("designated",), ("promotion", "designated")),
    "dy.channel_goods": (("create", "bind"),),
    "tb.campaign": (("create",),),
}
_VERDICT_RANK = ("error", "wrong_account", "wall", "login", "unavailable", "invalid", "blocked", "ready")


def _worse_verdict(a: str, b: str) -> str:
    rank = {v: i for i, v in enumerate(_VERDICT_RANK)}
    return a if rank.get(a, 0) <= rank.get(b, 0) else b


def _new_block(raw, action: Optional[dict]) -> dict:
    return {"version": 1, "action": action, "requested": None if action is not None else _requested(raw),
            "verdict": "ready", "problems": [], "facts": [], "steps": [], "run": {}, "catalog": None,
            "previewed_at": time.time()}


def _requested(raw) -> Optional[dict]:
    """What the draft asked for, kept small (shown when it could not be used)."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, value in list(raw.items())[:20]:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        out[_clip(key, 40)] = _clip(text, 200)
    return out


def failed_preview(raw, cfg: Optional[dict], why: str) -> dict:
    """A preview that could not run (``error``)."""
    try:
        action = check_action(raw, limits=(cfg or {}).get("limits"))
    except ActionError:
        action = None
    block = _new_block(raw, action)
    block["verdict"] = "error"
    block["problems"].append(_clip(why, 400))
    return block


def _runner(cfg: dict, heartbeat):
    def run_one(argv: list, platform: str, execute: bool) -> CliRun:
        timeout = cfg["execute_timeout_sec"] if execute else cfg["preview_timeout_sec"]
        return run_cli(argv, cfg["dirs"][platform], timeout=timeout, env=cli_env(cfg), heartbeat=heartbeat,
                       interruptible=not execute)
    return run_one


def preview(raw, cfg: dict, *, catalog: Optional[dict], heartbeat=None, context: Optional[dict] = None) -> dict:
    """Phase 1 (the Mac, with the draft): the action checked and its CLI run WITHOUT
    --execute.  Returns the ``platform`` block: ``verdict`` (``ready`` = an operator may
    approve it), ``problems``, ``facts``, ``steps`` (each command, its exit code and JSON),
    ``run`` (what an approval executes), the catalogue entry."""
    try:
        action = check_action(raw, catalog=catalog, limits=cfg.get("limits"))
    except ActionError as e:
        block = _new_block(raw, None)
        block["verdict"] = "invalid"
        block["problems"].append(str(e))
        return block
    block = _new_block(raw, action)
    name = action["action"]
    platform = ACTIONS[name]["platform"]
    entry = catalog_item(catalog, platform, action["item_id"]) or {}
    block["catalog"] = {"synced_at": (catalog or {}).get("synced_at"), "title": _clip(entry.get("title", ""), 120)}
    if catalog is None:
        block["verdict"] = "invalid"
        block["problems"].append("这台 Mac 还没有商品目录：先运行 python3 tools/claude_relay/worker.py --sync-products")
        return block
    if platform not in cfg.get("dirs", {}):
        block["verdict"] = "unavailable"
        block["problems"].append(cfg.get("missing", {}).get(platform)
                                 or f"这台 Mac 的 config.json 里没有配置{PLATFORMS[platform]}的命令行项目（platforms.{platform}）")
        return block
    _PREVIEWS[name](action, cfg, _runner(cfg, heartbeat), block, {**(context or {}), "catalog_entry": entry or None})
    verdict = "ready"
    for step in block["steps"]:
        verdict = _worse_verdict(verdict, step.get("verdict") or "error")
        block["problems"].extend(p for p in step.get("problems") or [] if p not in block["problems"])
    if verdict == "ready" and tuple(block["run"].get("steps") or ()) not in PLANS[name]:
        verdict = "error"
        block["problems"].append("预览没有给出执行步骤")
    block["verdict"] = verdict
    block["problems"] = block["problems"][:20]
    return block


def check_plan(action: dict, plan) -> dict:
    """What an approval runs (the preview's ``run``), held to ``PLANS`` and its own formats."""
    if not isinstance(plan, dict):
        raise _err("bad_plan", "批准里没有执行步骤")
    steps = tuple(plan.get("steps") or ())
    if steps not in PLANS[action["action"]]:
        raise _err("bad_plan", f"{action['action']} 不能这样执行：{'、'.join(map(str, steps))}")
    out = {"steps": list(steps)}
    if action["action"] == "xhs.direct_plan":
        out["buyer_id"] = _text_id(plan.get("buyer_id"), _HEX24, "bad_plan", "买手 userId")
        out["name"] = _plan_name(plan.get("name"))
    elif action["action"] == "tb.campaign":
        out["name"] = _tb_name(plan.get("name"))
    return out


def record(outcome: str, problems: Optional[list] = None) -> dict:
    """An execution's record (what the worker hands back)."""
    return {"version": 1, "op": "platform_execute", "outcome": outcome, "problems": list(problems or []),
            "facts": [], "steps": [], "created": {}, "finished_at": time.time()}


def execute(raw, cfg: dict, *, plan, key: str, ledger, catalog: Optional[dict], heartbeat=None,
            context: Optional[dict] = None) -> dict:
    """Phase 2 (the Mac, after 批准并执行): the approved steps WITH --execute, each write sent
    once, the next step only after the one before ended VERIFIED_SUCCESS (or needed no
    change).  ``key`` is taken in ``ledger`` first: a key that was taken before is never
    run again (``ALREADY_RUN``).  Returns the record; ``outcome`` is VERIFIED_SUCCESS
    only when every step was and the reply has what it needs (the link)."""
    out = record("NOT_RUN")
    out["key"] = str(key)
    try:
        action = check_action(raw, catalog=catalog, limits=cfg.get("limits"))
        approved = check_plan(action, plan)
        if catalog is None:
            raise _err("no_catalog", "这台 Mac 上没有商品目录了，不执行")
    except ActionError as e:
        out["problems"].append(f"没有执行：{e}")
        return out
    platform = ACTIONS[action["action"]]["platform"]
    if platform not in cfg.get("dirs", {}):
        out["problems"].append("没有执行：" + (cfg.get("missing", {}).get(platform)
                                            or f"这台 Mac 没有配置{PLATFORMS[platform]}的命令行项目"))
        return out
    try:
        earlier = claim(ledger, key, {"key": key, "status": "started", "started_at": time.time(),
                                      "action": action, "run": approved, **(context or {})})
    except (ActionError, OSError) as e:
        out["problems"].append(f"没有执行：执行台账写不了（{e}）")
        return out
    if earlier is not None:
        out["outcome"] = "ALREADY_RUN"
        out["problems"].append("这次批准已经执行过（或者执行到一半中断了），不会再执行；结果以平台后台为准")
        if isinstance(earlier.get("record"), dict):
            out["earlier"] = {"outcome": earlier["record"].get("outcome"), "finished_at": earlier.get("finished_at")}
        return out
    try:
        _EXECUTES[platform](action, cfg, approved, _runner(cfg, heartbeat), out,
                            {"catalog_entry": catalog_item(catalog, platform, action["item_id"])})
        writes = [step for step in out["steps"] if step.get("execute")]
        outcome = worst(step.get("outcome") for step in writes) if writes else "NOT_RUN"
        done = {step["name"] for step in out["steps"] if step.get("outcome") in _DONE}
        incomplete = out.pop("incomplete", False)
        if outcome in _DONE:
            if incomplete or any(name not in done for name in approved["steps"]):
                outcome = "PARTIAL"
            elif (ACTIONS[action["action"]]["link"]
                  and not check_link(out["created"].get("link", ""), platform)):
                outcome = "PARTIAL"
                out["problems"].append("平台上做好了，但没拿到能发给达人的链接")
            else:
                outcome = VERIFIED
        out["outcome"] = outcome
    except Exception as e:                       # a bug here: what the CLI did is not known
        out["outcome"] = "INDETERMINATE"
        out["problems"].append(f"Mac 上执行出错：{_clip(e, 200)}；先到平台后台核对")
    finally:
        out["finished_at"] = time.time()
        try:
            settle(ledger, key, {"key": key, "status": "done", "action": action, "run": approved,
                                 "finished_at": out["finished_at"], "record": {k: v for k, v in out.items() if k != "steps"},
                                 **(context or {})})
        except OSError:
            pass
    return out


# ── the bot's side: what comes back from the Mac, checked ─────────────────

_STEP_KEYS = ("name", "label", "execute", "command", "exit_code", "seconds", "verdict", "outcome")


def _texts(value, limit: int, count: int) -> list:
    return [_clip(v, limit) for v in (value if isinstance(value, list) else [])[:count] if isinstance(v, (str, int, float))]


def _facts(value, count: int = 40) -> list:
    out = []
    for item in (value if isinstance(value, list) else [])[:count]:
        if isinstance(item, dict) and "label" in item:
            out.append(_fact(item.get("label", ""), item.get("value", "")))
    return out


def _steps(value) -> list:
    out = []
    for item in (value if isinstance(value, list) else [])[:8]:
        if not isinstance(item, dict):
            continue
        step = {k: item.get(k) for k in _STEP_KEYS}
        step["name"], step["label"] = _clip(step["name"], 30), _clip(step["label"], 60)
        step["command"] = _clip(step["command"], 1000)
        step["execute"] = bool(step["execute"])
        step["exit_code"] = step["exit_code"] if isinstance(step["exit_code"], int) else None
        step["seconds"] = step["seconds"] if isinstance(step["seconds"], (int, float)) else None
        step["verdict"] = step["verdict"] if step["verdict"] in VERDICTS or step["verdict"] == "needs_promotion" else ""
        step["outcome"] = step["outcome"] if step["outcome"] in OUTCOMES else ""
        step["problems"] = _texts(item.get("problems"), 500, 20)
        step["facts"] = _facts(item.get("facts"))
        raw = item.get("raw")
        step["raw"] = (raw if len(raw) <= MAX_RAW_CHARS + 40 else raw[:MAX_RAW_CHARS]) if isinstance(raw, str) else ""
        out.append(step)
    return out


def check_block(block) -> dict:
    """The Mac's preview of an action (a draft's ``platform``), checked and cut to size.
    ``ActionError`` when it is not one; an action that does not pass ``check_action`` (the
    form) cannot be ``ready``."""
    if not isinstance(block, dict) or block.get("verdict") not in VERDICTS:
        raise _err("bad_block", "平台预览的格式不对")
    action = None
    if block.get("action") is not None:
        action = check_action(block["action"], limits=FORM_LIMITS)
    verdict = block["verdict"]
    if action is None and verdict == "ready":
        raise _err("bad_block", "预览说可以执行，却没有动作")
    plan = {}
    if verdict == "ready":
        plan = check_plan(action, block.get("run"))
    catalog = block.get("catalog") if isinstance(block.get("catalog"), dict) else {}
    synced = catalog.get("synced_at")
    return {
        "version": 1, "action": action,
        "requested": _requested(block.get("requested")) if block.get("requested") is not None else None,
        "verdict": verdict, "problems": _texts(block.get("problems"), 500, 20), "facts": _facts(block.get("facts")),
        "steps": _steps(block.get("steps")), "run": plan,
        "catalog": {"synced_at": synced if isinstance(synced, (int, float)) else None,
                    "title": _clip(catalog.get("title", ""), 120)} if catalog else None,
        "previewed_at": block["previewed_at"] if isinstance(block.get("previewed_at"), (int, float)) else None,
    }


def unpreviewed_block(raw) -> dict:
    """A draft that names an action the Mac did not preview: shown, never executable."""
    try:
        action = check_action(raw, limits=FORM_LIMITS)
    except ActionError:
        action = None
    block = _new_block(raw, action)
    block["requested"] = _requested(raw)
    block["verdict"] = "unavailable"
    block["problems"].append("Mac 没有预览这个平台动作（worker 太旧，或者那台 Mac 没配置平台命令行），不能执行")
    return block


def check_record(value) -> dict:
    """The Mac's record of an execution, checked: ``outcome`` one of ``OUTCOMES``, the link
    one the platform made (``check_link``)."""
    if not isinstance(value, dict) or value.get("op") != "platform_execute" or value.get("outcome") not in OUTCOMES:
        raise _err("bad_record", "执行结果的格式不对")
    created_in = value.get("created") if isinstance(value.get("created"), dict) else {}
    created = {}
    for key in ("channel_id", "group_id", "task_id", "item_id", "campaign_id", "plan_name", "review"):
        if created_in.get(key) not in (None, ""):
            created[key] = _clip(created_in[key], 200)
    if isinstance(created_in.get("strategy_ids"), list):
        created["strategy_ids"] = [_clip(s, 40) for s in created_in["strategy_ids"][:10]]
    link = str(created_in.get("link") or "")
    if link:
        platform = next((p for p in _LINK_HOSTS if check_link(link, p)), "")
        if platform:
            created["link"] = check_link(link, platform)
    outcome = value["outcome"]
    return {"version": 1, "op": "platform_execute", "outcome": outcome, "key": _clip(value.get("key", ""), 40),
            "problems": _texts(value.get("problems"), 500, 20), "facts": _facts(value.get("facts")),
            "steps": _steps(value.get("steps")), "created": created,
            "finished_at": value["finished_at"] if isinstance(value.get("finished_at"), (int, float)) else None}


# ── the catalogue: what each platform has on sale (read-only commands) ────

CATALOG_TIMEOUT_SEC = 600
MAX_CATALOG_ITEMS = 2000


def _yuan(value, fen: bool) -> str:
    """A price as yuan text ("19.90"); ``fen``: the platform's number is in fen."""
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return ""
    if fen:
        number = number / 100
    return format(number.quantize(Decimal("0.01")), "f")


def _catalog_run(cfg: dict, platform: str, words: list, progress) -> tuple:
    argv = [cfg["node"], CLI_FILES[platform], *words]
    run = run_cli(argv, cfg["dirs"][platform], timeout=CATALOG_TIMEOUT_SEC, env=cli_env(cfg), interruptible=True)
    if progress:
        progress(f"{platform}: {run.command()} -> 退出码 {run.exit_code}（{run.seconds}s）")
    data = parse_json(run.stdout)
    if run.exit_code != 0 or data is None:
        raise RuntimeError(_exit_problem(run) if run.exit_code else "命令没有输出 JSON")
    return run, data


def _sync_dy(cfg: dict, progress) -> dict:
    shop = [f"--expect-shop-id={cfg['dy_expect_shop_id']}"] if cfg.get("dy_expect_shop_id") else []
    _, data = _catalog_run(cfg, "dy", ["fxg", "products", "--page-size=100", *shop], progress)
    items, notes = [], []
    for row in _rows(data, "rows")[:MAX_CATALOG_ITEMS]:
        pid = str(row.get("productId") or "")
        if not _DIGITS.match(pid):
            continue
        items.append({"id": pid, "title": _clip(row.get("name") or "", 120),
                      "price": _yuan(row.get("discountPrice") or row.get("priceLower"), fen=True), "skus": []})
    if not data.get("complete", True):
        notes.append("抖店商品列表没读完整（页数上限等），目录里可能少了商品")
    by_id = {item["id"]: item for item in items}
    ids = list(by_id)
    for start in range(0, len(ids), 20):
        batch = ids[start:start + 20]
        try:
            _, prices = _catalog_run(cfg, "dy", ["fxg", "sku-prices", "--product-id=" + ",".join(batch), *shop], progress)
        except RuntimeError as e:
            notes.append(f"SKU 价格没读到（{_clip(e, 120)}）：这些商品在目录里没有 SKU")
            break
        for row in _rows(prices, "rows"):
            item = by_id.get(str(row.get("productId") or ""))
            if item is None:
                continue
            for sku in row.get("skus") or []:
                if isinstance(sku, dict) and _DIGITS.match(str(sku.get("skuId") or "")):
                    item["skus"].append({"id": str(sku["skuId"]), "name": _clip(sku.get("name") or "", 60),
                                         "price": _yuan(sku.get("priceYuan"), fen=False) or _yuan(sku.get("price"), fen=True)})
    return {"items": items, "notes": notes, "complete": bool(data.get("complete", True))}


def sync_catalog(cfg: dict, wanted: list, *, previous: Optional[dict] = None, progress=None) -> dict:
    """Read every wanted platform's on-sale products (and SKUs, prices) with its read-only
    commands.  A platform that fails keeps its previous list, marked ``stale``."""
    old = ((previous or {}).get("platforms") or {}) if isinstance(previous, dict) else {}
    out = {"version": 1, "synced_at": time.time(), "platforms": {}}
    for platform in PLATFORMS:
        if platform not in wanted or platform not in cfg.get("dirs", {}):
            if isinstance(old.get(platform), dict):
                out["platforms"][platform] = old[platform]
            continue
        try:
            part = _SYNCS[platform](cfg, progress)
            part.update(ok=True, error="", synced_at=time.time(), stale=False)
        except Exception as e:
            kept = old.get(platform) if isinstance(old.get(platform), dict) else {}
            part = {"items": kept.get("items") or [], "notes": kept.get("notes") or [], "ok": False,
                    "error": _clip(e, 300), "synced_at": kept.get("synced_at"), "stale": bool(kept.get("items")),
                    "complete": False}
        out["platforms"][platform] = part
    return out


def _cell(text) -> str:
    return _clip(text, 120).replace("|", "｜")


def catalog_markdown(catalog: dict) -> str:
    """kb/products.md: the catalogue for the drafting Claude (ids are only to be taken from here)."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(catalog.get("synced_at") or time.time()))
    lines = ["# 在售商品目录（自动生成，不要手改）", "",
             f"同步时间：{when}（Mac 上运行 `python3 tools/claude_relay/worker.py --sync-products` 生成）。",
             "起草平台动作（platform_action）时，商品 ID 和 SKU ID 只能从这张表里取；价格是目录里的售价（元），"
             "专属价、佣金按 kb/ 里的合作规则定。", ""]
    for platform, name in PLATFORMS.items():
        part = (catalog.get("platforms") or {}).get(platform)
        if not isinstance(part, dict):
            continue
        items = part.get("items") or []
        lines += [f"## {name}（{platform}）— {len(items)} 个商品", ""]
        if not part.get("ok"):
            lines += [f"这次没读成：{part.get('error')}" + ("（下面是上一次同步的）" if part.get("stale") else ""), ""]
        for note in part.get("notes") or []:
            lines.append(f"- {note}")
        if part.get("notes"):
            lines.append("")
        if items:
            lines += ["| 商品 ID | 标题 | 售价（元） | SKU（ID：规格 售价） |", "|---|---|---|---|"]
            for item in items:
                skus = "；".join(f"{s.get('id')}：{_cell(s.get('name') or '')} {s.get('price') or ''}".strip()
                                for s in item.get("skus") or [])
                lines.append(f"| {item.get('id')} | {_cell(item.get('title') or '')} | {item.get('price') or ''} | {skus} |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_catalog(workspace, catalog: dict) -> list:
    """Write kb/products.json and kb/products.md (each atomically); returns the two paths."""
    paths = []
    for rel, text in ((CATALOG_JSON, json.dumps(catalog, ensure_ascii=False, indent=1)),
                      (CATALOG_MD, catalog_markdown(catalog))):
        path = Path(workspace) / rel
        part = path.with_name(path.name + ".part")
        part.write_text(text, encoding="utf-8")
        os.replace(str(part), str(path))
        paths.append(path)
    return paths


# ── 小红书 (xiaohongshu-engineering, node bin/xhs.mjs) ──────────────────────
# direct-plan's own preview reads nothing; the page's checks (the name is free, the buyer is
# known, the account's commission minimum, the item picker) run only with --execute.  So
# its preview is the project's read-only full run, tools/dry-execute.mjs: the command
# WITH --execute through a transport that lets only reads through (the refused send
# shows as INDETERMINATE with "writes refused: 1" on stderr).  channel-item has its own
# reading preview (--read-current).

XHS_DRY_RUN = "tools/dry-execute.mjs"
XHS_STEPS = {
    "buyer": "千帆 · 按小红书号 / 昵称找买手",
    "plan": "千帆 · 定向计划（只读整条演练：读平台、做页面的检查，不发送）",
    "create": "千帆 · 建定向计划",
    "channel": "千帆 · 渠道商品（读当前表单）",
    "channel_create": "千帆 · 建渠道商品",
}
_LOGIN_CODES = ("NOT_LOGGED_IN", "SESSION_EXPIRED", "AUTH_REQUIRED")
_WALL_CODES = ("RISK_CONTROL",)


def _plan_name(value) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 35 or _CONTROL.search(text) or text.startswith("-"):
        raise _err("bad_plan", "计划名不对")
    return text


def _xhs_plan_options(action: dict, plan: dict) -> list:
    return ["direct-plan", "--action=create", f"--name={plan['name']}", f"--buyer-id={plan['buyer_id']}",
            f"--item={action['item_id']}:{action['commission_rate']}", "--long-term"]


def _xhs_channel_options(action: dict) -> list:
    opts = ["channel-item", "--action=create", f"--main-item={action['item_id']}"]
    if action.get("title"):
        opts.append(f"--title={action['title']}")
    if action.get("short_title"):
        opts.append(f"--short-title={action['short_title']}")
    if action.get("sku_prices"):
        opts.append("--sku-price=" + ",".join(f"{p['sku_id']}:{p['price']}" for p in action["sku_prices"]))
    opts.append("--shelf=on")
    return opts


def _code_of(data, stderr: str) -> str:
    """The error code a run stopped with (``stopped.code`` of the JSON, else the first
    ``[CODE]`` / ``[run threw] CODE`` line of stderr)."""
    stopped = data.get("stopped") if isinstance(data, dict) else None
    if isinstance(stopped, dict) and stopped.get("code"):
        return str(stopped["code"])
    for line in str(stderr or "").splitlines():
        m = re.match(r"^\[run threw\]\s+([A-Z][A-Z_]+)", line.strip()) or re.match(r"^\[([A-Z][A-Z_]+)\]", line.strip())
        if m and m.group(1) not in ("DRY", "HTTP", "READ", "WRITE", "INCOMPLETE", "PREVIEW"):
            return m.group(1)
    return ""


def _code_verdict(code: str) -> str:
    if code in _LOGIN_CODES:
        return "login"
    if code in _WALL_CODES:
        return "wall"
    return ""


def _find_buyer(cfg: dict, action: dict, run_one, block: dict) -> Optional[dict]:
    run = run_one([cfg["node"], CLI_FILES["xhs"], "direct-plans", f"--find-buyer={action['creator_platform_id']}", "--raw"],
                  "xhs", False)
    data = parse_json(run.stdout)
    rec = _step("buyer", XHS_STEPS["buyer"], run, False)
    block["steps"].append(rec)
    if run.exit_code != 0 or not isinstance(data, dict):
        rec["problems"].append(_exit_problem(run))
        rec["verdict"] = _EXIT_VERDICT.get(run.exit_code) or _code_verdict(_code_of(data, run.stderr)) or "error"
        return None
    buyers = [b for b in data.get("buyers") or [] if isinstance(b, dict)]
    if data.get("matches") == 1 and len(buyers) == 1 and _HEX24.match(str(buyers[0].get("user_id") or "")):
        buyer = buyers[0]
        rec["verdict"] = "ready"
        rec["facts"].append(_fact("买手", f"{buyer.get('nick_name')}（粉丝 {buyer.get('fan_cnt')}，笔记 {buyer.get('note_cnt')}），"
                                         f"userId {buyer['user_id']}：请核对是不是这位达人"))
        return {"id": str(buyer["user_id"]), "nick": _clip(buyer.get("nick_name") or "", 40)}
    rec["verdict"] = "blocked"
    if not buyers:
        rec["problems"].append(f"千帆没找到「{action['creator_platform_id']}」这个买手" +
                               (f"：{_clip(data.get('refusal'), 100)}" if data.get("refusal") else "") + "；请让达人给小红书 userId")
    else:
        names = "、".join(f"{_clip(b.get('nick_name') or '?', 20)}（粉丝 {b.get('fan_cnt')}）" for b in buyers[:5])
        rec["problems"].append(f"「{action['creator_platform_id']}」找到 {data.get('matches', len(buyers))} 个买手：{names}；"
                               "不能确定是哪一位，请让达人给小红书 userId")
    return None


def _preview_xhs_direct_plan(action: dict, cfg: dict, run_one, block: dict, context: dict) -> None:
    who = action["creator_platform_id"]
    if _HEX24.match(who):
        buyer = {"id": who, "nick": ""}
    else:
        buyer = _find_buyer(cfg, action, run_one, block)
        if buyer is None:
            return
    plan = {"steps": ["create"], "buyer_id": buyer["id"],
            "name": f"达人合作{context.get('party_id') or ''}-{time.strftime('%m%d%H%M%S')}"}
    if not (cfg["dirs"]["xhs"] / XHS_DRY_RUN).is_file():
        run = run_one([cfg["node"], CLI_FILES["xhs"], *_xhs_plan_options(action, plan)], "xhs", False)
        rec = _step("plan", "千帆 · 定向计划（预览：这台 Mac 上没有只读演练工具，平台的检查要到执行时才做）", run, False)
        rec["verdict"] = _preview_verdict(run, parse_json(run.stdout), _rows(parse_json(run.stdout)), rec)
        block["steps"].append(rec)
        block["run"] = plan
        return
    run = run_one([cfg["node"], XHS_DRY_RUN, *_xhs_plan_options(action, plan)], "xhs", False)
    data = parse_json(run.stdout)
    rows = _rows(data)
    rec = _step("plan", XHS_STEPS["plan"], run, False)
    block["steps"].append(rec)
    refused = re.search(r"writes refused:\s*(\d+)", str(run.stderr or ""))
    code = _code_of(data, run.stderr)
    if run.error or run.stopped or not rows:
        rec["verdict"] = _code_verdict(code) or _EXIT_VERDICT.get(run.exit_code) or "error"
        rec["problems"].append(_exit_problem(run) if run.exit_code or run.error or run.stopped else "演练没有输出结果")
        return
    verdict = "ready"
    for row in rows:
        outcome = row.get("outcome")
        if outcome == "INDETERMINATE" and refused and int(refused.group(1)) >= 1:
            body = (row.get("request") or {}).get("body") if isinstance(row.get("request"), dict) else None
            for info in (body or {}).get("plan_infos") or [] if isinstance(body, dict) else []:
                if isinstance(info, dict):
                    rec["facts"].append(_fact("将写入", f"商品 {info.get('item_id')} 佣金率 {info.get('distributor_rate')}%"))
            continue
        verdict = "blocked" if outcome in ("STATE_CONFLICT", "NO_CHANGE_NEEDED") else (_code_verdict(code) or "blocked")
        if outcome == VERIFIED:
            verdict = "error"
            rec["problems"].append("只读演练居然报告写成功了：先到千帆看一眼有没有多出来的定向计划")
        rec["problems"].append(_clip(f"{outcome}：{row.get('detail') or ''}", 500))
    state = data.get("state") if isinstance(data, dict) else None
    for warning in (state or {}).get("warnings") or [] if isinstance(state, dict) else []:
        rec["facts"].append(_fact("页面提示", warning))
    window = data.get("window") if isinstance(data, dict) else None
    if isinstance(window, dict):
        rec["facts"].append(_fact("有效期", f"{window.get('start')} 起，{window.get('end')}"))
    rec["facts"].append(_fact("计划名", plan["name"]))
    rec["verdict"] = verdict
    block["run"] = plan


def _preview_xhs_channel_item(action: dict, cfg: dict, run_one, block: dict, context: dict) -> None:
    run = run_one([cfg["node"], CLI_FILES["xhs"], *_xhs_channel_options(action), "--read-current"], "xhs", False)
    data = parse_json(run.stdout)
    rec = _step("channel", XHS_STEPS["channel"], run, False)
    block["steps"].append(rec)
    block["run"] = {"steps": ["create"]}
    if run.exit_code != 0 or not isinstance(data, dict):
        rec["verdict"] = _EXIT_VERDICT.get(run.exit_code) or _code_verdict(_code_of(data, run.stderr)) or "error"
        rec["problems"].append(_exit_problem(run))
        return
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    verdict = _preview_verdict(run, data, _rows(data), rec)
    for key, what in (("blocked", "页面会拒绝"), ("unsupported", "工具不支持")):
        for text in current.get(key) or []:
            rec["problems"].append(_clip(f"{what}：{text}"))
            verdict = "blocked"
    if data.get("adjustmentProblem"):
        rec["problems"].append(_clip(f"改动不合页面规则：{data['adjustmentProblem']}"))
        verdict = "blocked"
    for sku in current.get("skus") or []:
        if isinstance(sku, dict):
            rec["facts"].append(_fact(f"SKU {sku.get('mainSkuId') or sku.get('skuId')}",
                                      f"现价 {sku.get('price')} 元" + (f" → 专属价 {sku['newPrice']} 元" if sku.get("newPrice") else "")
                                      + f"；库存 {sku.get('stock')}"))
    would = data.get("wouldSend") if isinstance(data.get("wouldSend"), dict) else None
    if would:
        rec["facts"].append(_fact("将提交", f"标题「{would.get('title')}」；导购短标题「{would.get('shortTitle')}」；"
                                          f"改动字段 {', '.join(map(str, would.get('customFields') or []))}"))
    if current.get("channelItemsOfThisMainItem") is not None:
        rec["facts"].append(_fact("这个主商品现有的渠道商品", current["channelItemsOfThisMainItem"]))
    rec["verdict"] = verdict


def _xhs_created(data, name: str) -> dict:
    out = {}
    rows = _rows(data)
    if rows and _HEX24.match(str(rows[0].get("targetId") or "")):
        out["group_id" if name == "xhs.direct_plan" else "item_id"] = str(rows[0]["targetId"])
    if isinstance(data, dict) and name == "xhs.direct_plan" and data.get("name"):
        out["plan_name"] = _clip(data["name"], 40)
    info = ((rows[0].get("readBack") or {}).get("info") or {}) if rows and isinstance(rows[0].get("readBack"), dict) else {}
    if isinstance(info, dict) and info.get("taskId"):
        out["task_id"] = _clip(info["taskId"], 40)
    if isinstance(info, dict) and info.get("checkStatus") is not None:
        out["review"] = {1: "审核通过", 2: "审核中", 3: "审核驳回"}.get(info["checkStatus"], str(info["checkStatus"]))
    return out


def _execute_xhs(action: dict, cfg: dict, plan: dict, run_one, record: dict) -> None:
    name = action["action"]
    if name == "xhs.direct_plan":
        argv = [cfg["node"], CLI_FILES["xhs"], *_xhs_plan_options(action, plan), "--execute"]
        step, label_ = "create", XHS_STEPS["create"]
    else:
        argv = [cfg["node"], CLI_FILES["xhs"], *_xhs_channel_options(action), "--execute"]
        step, label_ = "create", XHS_STEPS["channel_create"]
    run = run_one(argv, "xhs", True)
    data = parse_json(run.stdout)
    rec = _step(step, label_, run, True)
    rec["outcome"] = _execute_outcome(run, data, _rows(data), rec)
    record["steps"].append(rec)
    record["created"].update(_xhs_created(data, name))
    rows = _rows(data)
    back = rows[0].get("readBack") if rows and isinstance(rows[0].get("readBack"), dict) else {}
    if back.get("cannotVerify"):
        rec["facts"].append(_fact("核验不了的字段", "、".join(map(str, back["cannotVerify"]))))


# ── 淘宝 (campaign-engineering, node bin/oc.mjs; OpenCLI) ─────────────────
# Rows come as a JSON array (--format=json); a refusal before any row (a CREATE_PRECHECK_*,
# a login) is OpenCLI's YAML envelope on stderr, with -f json too.  The exit code is 0
# for any row outcome (FAILED, INDETERMINATE, STATE_CONFLICT too): only the rows tell.
# Optional-value flags (--execute, --pause-after-create) are given as --flag=true so they
# cannot take the next word.

TB_STEP = "万相台 · 精准人群推广（自定义）计划"


def _tb_name(value) -> str:
    text = str(value or "")
    if not re.match(r"^[0-9A-Za-z_一-龥]{1,32}$", text):
        raise _err("bad_plan", "推广计划名不对")
    return text


def _tb_argv(cfg: dict, action: dict, plan: dict, title: str, *, execute: bool) -> list:
    shop = [f"--shop={cfg['tb_shop']}"] if cfg.get("tb_shop") else []
    opts = [f"--item-id={action['item_id']}", f"--crowd={','.join(action['crowd_ids'])}",
            f"--bid-target={action['bid_target']}", f"--day-budget={int(action['day_budget'])}",
            f"--campaign-name={plan['name']}", "--transport=http", "--format=json", "--pause-after-create=true"]
    if title:
        opts.insert(1, f"--item-title={title}")
    return [cfg["node"], CLI_FILES["tb"], *shop, "alimama", "create-display-custom-campaign", *opts,
            *(["--execute=true"] if execute else [])]


def yaml_error(stderr: str) -> Optional[dict]:
    """OpenCLI's error envelope on stderr (``ok: false`` / ``error:`` code, message, exitCode)."""
    text = str(stderr or "")
    start = text.find("ok: false")
    if start < 0:
        return None
    out = {}
    for key in ("code", "message", "help", "exitCode"):
        m = re.search(rf"^\s+{key}:\s*(.*)$", text[start:], re.M)
        if not m:
            continue
        value = m.group(1).strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                value = value.strip('"')
        elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1].replace("''", "'")
        out[key] = value
    return out or None


def _tb_title(catalog_entry: Optional[dict]) -> str:
    title = str((catalog_entry or {}).get("title") or "").strip()
    return title if title and not _CONTROL.search(title) and len(title) <= 120 else ""


def _preview_tb(action: dict, cfg: dict, run_one, block: dict, context: dict) -> None:
    plan = {"steps": ["create"], "name": f"达人合作{context.get('party_id') or ''}_{time.strftime('%m%d%H%M')}"}
    title = _tb_title(context.get("catalog_entry"))
    run = run_one(_tb_argv(cfg, action, plan, title, execute=False), "tb", False)
    data = parse_json(run.stdout)
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    rec = _step("create", TB_STEP, run, False)
    block["steps"].append(rec)
    block["run"] = plan
    envelope = yaml_error(run.stderr)
    if not rows:
        if envelope:
            code = str(envelope.get("code") or "")
            rec["problems"].append(_clip(f"{code}：{envelope.get('message') or ''}", 500))
            rec["verdict"] = ("login" if code in _LOGIN_CODES or run.exit_code == 77 else
                              "blocked" if code.startswith("CREATE_PRECHECK") or run.exit_code == 2 else "error")
        else:
            rec["problems"].append(_exit_problem(run))
            rec["verdict"] = _EXIT_VERDICT.get(run.exit_code) or "error"
        return
    verdict = "ready"
    for row in rows:
        risk = row.get("risk") if isinstance(row.get("risk"), dict) else {}
        if row.get("outcome") != "PREVIEW" or risk.get("stopRequired"):
            verdict = "blocked"
            rec["problems"].append(_clip(f"{row.get('outcome')}：{row.get('detail') or ''} {risk.get('codes') or ''}", 500))
        else:
            rec["facts"].append(_fact("将新建", row.get("desiredValue") or ""))
            if row.get("detail"):
                rec["facts"].append(_fact("说明", row["detail"]))
    rec["facts"].append(_fact("计划名", plan["name"]))
    rec["verdict"] = verdict


def _execute_tb(action: dict, cfg: dict, plan: dict, run_one, record: dict, catalog_entry: Optional[dict] = None) -> None:
    run = run_one(_tb_argv(cfg, action, plan, _tb_title(catalog_entry), execute=True), "tb", True)
    data = parse_json(run.stdout)
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    rec = _step("create", TB_STEP, run, True)
    record["steps"].append(rec)
    if not rows:
        envelope = yaml_error(run.stderr) or {}
        code = str(envelope.get("code") or "")
        rec["problems"].append(_clip(f"{code}：{envelope.get('message') or ''}" if envelope else _exit_problem(run), 500))
        sent = _may_have_sent(run) or run.exit_code == 75
        rec["outcome"] = "INDETERMINATE" if sent else "FAILED"
        return
    rec["outcome"] = _execute_outcome(run, {"items": rows}, rows, rec)
    for row in rows:
        if row.get("targetKind") == "campaignId" and _DIGITS.match(str(row.get("targetId") or "")):
            record["created"].setdefault("campaign_id", str(row["targetId"]))
        if row.get("desiredValue") == "pause":
            rec["facts"].append(_fact("建后暂停", f"{row.get('outcome')}（读回：{row.get('observedValue')}）"))
    record["created"]["plan_name"] = plan["name"]


# ── the catalogue readers of 小红书 and 淘宝 ───────────────────────────────

def _sync_xhs(cfg: dict, progress) -> dict:
    _, data = _catalog_run(cfg, "xhs", ["items", "--tab=shelf", "--all", "--page-size=100"], progress)
    items, notes = [], []
    for row in (data.get("items") if isinstance(data, dict) else None) or []:
        if not isinstance(row, dict) or not _XHS_ID.match(str(row.get("item_id") or "")):
            continue
        items.append({"id": str(row["item_id"]), "title": _clip(row.get("item_name") or "", 120),
                      "price": _yuan(row.get("min_price"), fen=True), "skus": []})
        if len(items) >= MAX_CATALOG_ITEMS:
            break
    if isinstance(data, dict) and data.get("complete") is False:
        notes.append("千帆在售商品没读完整（页数上限等），目录里可能少了商品")
    by_id = {item["id"]: item for item in items}
    ids = list(by_id)
    for start in range(0, len(ids), 20):
        try:
            _, detail = _catalog_run(cfg, "xhs", ["item", "--item-id=" + ",".join(ids[start:start + 20])], progress)
        except RuntimeError as e:
            notes.append(f"SKU 没读到（{_clip(e, 120)}）：这些商品在目录里没有 SKU")
            break
        for entry in (detail.get("items") if isinstance(detail, dict) else None) or []:
            if not isinstance(entry, dict):
                continue
            item = by_id.get(str((entry.get("item") or {}).get("item_id") or ""))
            if item is None:
                continue
            for sku in entry.get("skus") or []:
                if isinstance(sku, dict) and _XHS_ID.match(str(sku.get("skuId") or "")):
                    price = (sku.get("price") or {}).get("price") if isinstance(sku.get("price"), dict) else None
                    item["skus"].append({"id": str(sku["skuId"]), "name": _clip(sku.get("skuName") or sku.get("variantName") or "", 60),
                                         "price": _yuan(price, fen=True), "on_sale": bool(sku.get("available", True))})
    return {"items": items, "notes": notes, "complete": not (isinstance(data, dict) and data.get("complete") is False)}


def _sync_tb(cfg: dict, progress) -> dict:
    shop = [f"--shop={cfg['tb_shop']}"] if cfg.get("tb_shop") else []
    items, notes, seen = [], [], set()
    for page in range(1, 21):
        _, rows = _catalog_run(cfg, "tb", [*shop, "alimama", "items", "--transport=http", "--format=json",
                                           f"--page={page}", "--page-size=200"], progress)
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        for row in rows:
            iid = str(row.get("itemId") or "")
            if _DIGITS.match(iid) and iid not in seen:
                seen.add(iid)
                items.append({"id": iid, "title": _clip(row.get("itemTitle") or "", 120),
                              "price": _yuan(row.get("price"), fen=False), "skus": []})
        if len(rows) < 200 or len(items) >= MAX_CATALOG_ITEMS:
            break
    else:
        notes.append("万相台选品列表超过 20 页，后面的没读")
    notes.append("淘宝的目录来自万相台选品列表（alimama items），没有 SKU；价格是选品列表里的一口价")
    return {"items": items, "notes": notes, "complete": True}


# ── the registries ────────────────────────────────────────────────────────

_PREVIEWS = {
    "xhs.direct_plan": _preview_xhs_direct_plan,
    "xhs.channel_item": _preview_xhs_channel_item,
    "dy.designated_commission": lambda a, c, r, b, ctx: _preview_dy_designated(a, c, r, b),
    "dy.channel_goods": lambda a, c, r, b, ctx: _preview_dy_channel_goods(a, c, r, b),
    "tb.campaign": _preview_tb,
}
_EXECUTES = {
    "xhs": lambda a, c, p, r, rec, ctx: _execute_xhs(a, c, p, r, rec),
    "dy": lambda a, c, p, r, rec, ctx: _execute_dy(a, c, p, r, rec),
    "tb": lambda a, c, p, r, rec, ctx: _execute_tb(a, c, p, r, rec, ctx.get("catalog_entry")),
}
_SYNCS = {"xhs": _sync_xhs, "dy": _sync_dy, "tb": _sync_tb}
