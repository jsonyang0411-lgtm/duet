"""达人档位（D30）和主播类型（D36）：按名单上的抖音号 / 小红书号，用聚水潭命令行查达人数据后算出来。

档位只决定"寄几款"的说法，审批照样由运营定：
- 抖音看近 30 日销售总额（sales30d），小红书看直播场均销售额（liveAvg）；平台给的是区间，按**下限**算
- 抖音：< 25 万 → 3 款；25 万–100 万 → 问专场；≥ 100 万 → 不限
- 小红书：< 50 万 → 3 款；50 万–100 万 → 问专场；≥ 100 万 → 不限
- 没有销售数据 → "无销售数据"（D30：不寄，由运营看）
主播类型用聚水潭命令行的 channelBySales（近 30 日直播、视频哪边卖得多，用户 10-26 定）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .jst_erp import ERPNotReadyError, run_jst_cli

TIER_THREE = "3 款"
TIER_ASK_LIVE = "问专场"
TIER_ANY = "不限"
TIER_NO_SALES = "无销售数据"

# 平台 → (看哪个数, 中文名, 3 款以下的线, 不限的线)
_RULES = {
    "DY": ("sales30d", "近 30 日销售总额", 250_000, 1_000_000),
    "XHS": ("liveAvg", "直播场均销售额", 500_000, 1_000_000),
}
PLATFORM_LABELS = {"DY": "抖音", "XHS": "小红书"}


def tier_of(platform: str, daren: Dict[str, Any]) -> tuple:
    """(档位, 用来分档的销售额说明)。daren 是聚水潭命令行 lookup 输出的 daren。"""
    rule = _RULES.get(platform)
    if rule is None:
        return "", f"{platform} 平台还没有分档规则"
    key, label, low, high = rule
    band = daren.get(key)
    if not isinstance(band, dict) or not band.get("available"):
        return TIER_NO_SALES, f"{label}：无数据"
    lower = float(band.get("min") or 0)
    tier = TIER_THREE if lower < low else TIER_ASK_LIVE if lower < high else TIER_ANY
    return tier, f"{label} {band.get('label', '')}"


def look_up(platform: str, platform_no: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """查一位达人。返回 {"ok", "tier", "channel", "sales_label", "level", "uid", "note"}；查不到时 ok=False，note 写原因。"""
    now = time.time() if now is None else now
    platform = (platform or "").strip().upper()
    platform_no = (platform_no or "").strip()
    empty = {"ok": False, "tier": "", "channel": "", "sales_label": "", "level": None, "uid": ""}
    if platform not in _RULES or not platform_no:
        return {**empty, "note": "名单上没有平台和抖音号 / 小红书号"}
    try:
        data = run_jst_cli("lookup", "--platform", platform, "--no", platform_no)
    except ERPNotReadyError as exc:
        return {**empty, "note": f"聚水潭查不了：{exc}"}
    if not data.get("found"):
        return {**empty, "note": f"聚水潭的达人广场里没有 {PLATFORM_LABELS[platform]}号 {platform_no}"}
    daren = data.get("daren") or {}
    tier, sales = tier_of(platform, daren)
    day = time.strftime("%m-%d", time.localtime(now))
    # 平台的达人 UID（聚水潭的 referrerId）：开抖音专属佣金要用；不是纯数字的不要
    uid = str(daren.get("uid") or "").strip()
    return {
        "ok": True,
        "tier": tier,
        "channel": str(daren.get("channelBySales") or ""),
        "sales_label": f"{sales}（聚水潭，{day} 查）",
        "level": daren.get("level"),
        "uid": uid if uid.isdigit() else "",
        "note": "",
    }


def refresh_contact(store, chat_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """按名单上的号码重查这位达人，把结果记进名单，返回 look_up 的结果。"""
    contact = store.get_contact(chat_id) or {}
    result = look_up(str(contact.get("platform") or ""), str(contact.get("platform_no") or ""), now=now)
    store.record_daren_profile(
        chat_id, tier=result["tier"], channel=result["channel"], sales_label=result["sales_label"],
        level=result["level"], note=result["note"], uid=result["uid"], now=now,
    )
    return result
