"""When may a draft go out without a person (8.1 of the assessment, confirmed as proposed).

Every kind of message starts reviewed.  A kind goes ``auto`` once, among the last
``window`` drafts of it a person decided on (since its last reset), at least
``min_unchanged`` were sent exactly as drafted and no edit touched key facts
(numbers, amounts, dates, phone placeholders).  Money and complaints are locked:
always reviewed.  An operator marking a sent message "发错了" puts the kind back to
``review`` and starts its count again.

Even an ``auto`` kind waits for a person when the draft itself is not plain: the
model asked for attention, the deal is not in copilot mode, a placeholder is
unknown, an FAQ answer does not name its kb/ source, a shipping notice does not
carry the system's waybill, fewer files came than the draft named, or material comes
without its files.
"""

from __future__ import annotations

import re
from collections import Counter

AUTO, REVIEW = "auto", "review"

# kind -> (window, at least this many sent unchanged, locked)
DEFAULT_GATES = {
    "greeting": (20, 18, False),
    "faq": (20, 18, False),
    "shipping_notice": (20, 18, False),
    "material": (20, 19, False),
    "other": (20, 18, False),
    "address_confirm": (30, 29, False),
    "money": (20, 18, True),
    "complaint": (20, 18, True),
}

# Key facts: placeholders, numbers (amounts, dates, waybills, percentages), Chinese numerals
# with a unit.  An edit that changes any of them is a key change.
_KEY = re.compile(r"〈(?:手机|证件)\d+〉|[A-Za-z]*\d+(?:[.:：/-]\d+)*%?|[零一二两三四五六七八九十百千万]+(?:元|块|个点|折|天|号|月|日|周|小时|分钟|份|个|条)")
# What a waybill looks like in text: letters then at least 6 digits (SF1234567890123, 7735…).
WAYBILL = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{0,4}\d{6,}[A-Za-z0-9]*(?![A-Za-z0-9])")


def key_facts(text: str) -> Counter:
    return Counter(m.group(0) for m in _KEY.finditer(str(text or "")))


def key_change(original: str, final: str) -> bool:
    """Did an edit change a number, an amount, a date or a placeholder?"""
    return key_facts(original) != key_facts(final)


def window_stats(reviewed: list[dict]) -> dict:
    """``{"decided", "unchanged", "key_changed"}`` of the drafts a person decided on."""
    unchanged = sum(1 for p in reviewed if p["status"] in ("sent", "failed") and not p["edited"])
    key_changed = any(p["edited"] and key_change(p["text"], p.get("final_text") or "") for p in reviewed)
    return {"decided": len(reviewed), "unchanged": unchanged, "key_changed": key_changed}


def earned(gate: dict, stats: dict) -> bool:
    """Has this kind earned ``auto``?"""
    return (not gate["locked"] and stats["decided"] >= gate["window"]
            and stats["unchanged"] >= gate["min_unchanged"] and not stats["key_changed"])


def waybill_ok(text: str, waybill: str) -> bool:
    """A shipping notice carries the system's waybill, and no other number that looks like one."""
    if not waybill or waybill not in str(text):
        return False
    return all(m.group(0) == waybill for m in WAYBILL.finditer(str(text)))


def plain_reason(kind: str, *, needs_attention: bool, mode: str, unknown_tokens: list, reason: str,
                 text: str, waybill: str, attachments: list, files_named: int = 0) -> str:
    """Why this draft must wait for a person even when its kind is ``auto`` ("" = it need not).
    ``files_named``: how many files the draft named (more than came: "两份资料发你啦" with one)."""
    if needs_attention:
        return "模型要求运营注意"
    if mode != "copilot":
        return "合作单不在副驾驶模式"
    if unknown_tokens:
        return "有对不上的占位符"
    if kind == "faq" and "kb/" not in reason:
        return "回答没有注明 kb/ 出处"
    if kind == "shipping_notice" and not waybill_ok(text, waybill):
        return "单号和系统记录对不上"
    if files_named > len(attachments or []):
        return f"草稿要附 {files_named} 个文件，只收到 {len(attachments or [])} 个"
    if kind == "material" and not attachments:
        return "素材文件没有收到"
    return ""
