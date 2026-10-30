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

下班时段 (``PRM_NIGHT_AUTO_HOURS``, 2026-10-29; empty = off, as before): inside the window a draft that
waits for a person by the rules above may still go out by itself (route ``night``) when ``night_reason``
finds nothing: never a complaint, everything ``plain_reason`` checks but the attention, no red line
(``night_red_line``: a percentage above 25, 坑位 / 保底 / 返现 …), and a draft the model flagged only as
one safe holding line (``night_holding_problem``).  A night send never counts toward its kind's gate (a
person did not decide on it: ``PrmStore.reviewed`` takes ``review`` only).  Which drafts are judged at
all (the reply lane, made and finished inside the window, this PC's clock right) is
src/prm/service.py's ``_night_judge``.  自动模式 (the console's switch, 2026-10-30) judges by the same rules at any
hour (route ``night`` too).
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from typing import Optional

AUTO, REVIEW = "auto", "review"
NIGHT = "night"     # the route of a draft the night window sent (never counts toward a kind's gate)

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


# ── 下班时段 (PRM_NIGHT_AUTO_HOURS) ──────────────────────────────────────────

NIGHT_MAX_PERCENT = 25          # the user's red line: a commission up to 25 % (kb: 佣金 25% 封顶)
NIGHT_HOLDING_CHARS = 120       # a holding line is short
# Words a draft sent at night must not say (money beyond the commission, refunds, guarantees …).
NIGHT_RED_WORDS = ("坑位", "保底", "保量", "包量", "对赌", "返现", "定金", "预付", "打款", "转账", "赔付", "赔偿",
                   "退款", "红包", "专属价", "现金", "补贴")
# A holding line says the operators will come back to her (at least one of these).
NIGHT_CUES = ("记下", "明早", "明天", "确认", "稍后", "第一时间", "回复您", "回您", "拉群", "小群", "拉个群")
# 「100%羊毛」 is what a hat is made of, not a commission (product titles say so).
NIGHT_MATERIALS = ("羊毛", "羊绒", "拉菲草", "纸草", "草编", "麦秆", "棉", "真丝", "桑蚕丝", "蚕丝", "麻", "亚麻",
                   "聚酯", "涤纶", "锦纶", "腈纶", "兔毛", "马海毛", "羊驼毛", "牛皮", "羊皮")
_MATERIAL_AFTER = re.compile(r"\s*(?:澳洲|新西兰|进口|天然|纯|精纺|美利奴|山羊|绵羊|长绒|有机)*(?:"
                             + "|".join(sorted(NIGHT_MATERIALS, key=len, reverse=True)) + ")")

_HOURS = re.compile(r"(\d{1,2}):(\d{2})\s*(?:-|~|–|—|至|到)\s*(\d{1,2}):(\d{2})")


def night_window(text) -> Optional[tuple]:
    """PRM_NIGHT_AUTO_HOURS as minutes of the day ``(start, end)``: from ``start`` on, until before ``end``, past
    midnight when ``start > end`` ("18:00-09:00": ``(1080, 540)``).  None when empty (off).  ValueError when it
    is not ``HH:MM-HH:MM`` (full-width digits and colons, –, ~, 至 and 到 do too; 24:00 only as the end) or it
    ends where it starts (a whole day is no 下班时段)."""
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not raw:
        return None
    m = _HOURS.fullmatch(raw)
    if not m:
        raise ValueError(f"not HH:MM-HH:MM: {raw!r}")
    sh, sm, eh, em = (int(g) for g in m.groups())
    if sh > 23 or sm > 59 or em > 59 or eh > 24 or (eh == 24 and em):
        raise ValueError(f"no such time of day: {raw!r}")
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end % 1440:
        raise ValueError(f"starts where it ends: {raw!r}")
    return start, end


def in_night(window, at: float) -> bool:
    """Is ``at`` inside ``window`` (``night_window``) by this PC's local clock?"""
    local = time.localtime(at)
    minute = local.tm_hour * 60 + local.tm_min
    start, end = window
    return start <= minute < end if start < end else (minute >= start or minute < end)


def night_window_text(window, dash: str = "–") -> str:
    """``(1080, 540)`` → "18:00–09:00" (``dash`` "-": as the setting and the Mac's job meta write it)."""
    start, end = window
    return f"{start // 60:02d}:{start % 60:02d}{dash}{end // 60:02d}:{end % 60:02d}"


_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
       "几": 9}                  # 「二十几个点」: up to 29
_CN_NUM = r"[零〇一二两三四五六七八九十百几]+(?:点[零〇一二三四五六七八九]+)?"
_NUM = rf"(?:\d+(?:\.\d+)?|{_CN_NUM})"
_MORE = r"(?P<more>多|来|余)?"   # 「二十多个点」: up to 30
_RANGE = r"\s*(?:到|至|~|-|—|–)\s*"
_CHENG_WORDS = "功本品员交为长熟立年人绩果就型形都套批色"   # 成功、成本 … are no 成 of a hundred
_PERCENTS = (
    (re.compile(rf"(?P<n>\d+(?:\.\d+)?){_MORE}\s*%"), 1),                                       # 26%、30 %、３０％
    (re.compile(rf"百分之\s*(?P<n>{_NUM}){_MORE}(?:{_RANGE}(?:百分之)?\s*(?P<m>{_NUM}))?"), 1),  # 百分之三十（到三十五）
    (re.compile(rf"(?P<n>{_NUM}){_MORE}\s*个?\s*百分点"), 1),                                   # 五个百分点
    (re.compile(rf"(?P<n>{_NUM}){_MORE}\s*个\s*点"), 1),                                        # 三十个点、30个点
    (re.compile(rf"(?<![零〇一二两三四五六七八九十百几\d.])(?P<n>[一二两三四五六七八九十几]|\d+(?:\.\d+)?)\s*成"
                rf"(?![{_CHENG_WORDS}])"), 10),                                                 # 三成 = 30%
)


def _cn_value(text: str) -> float:
    """「二十五」 25, 「十」 10, 「一百」 100, 「百」 100, 「二十五点五」 25.5, 「二十几」 29."""
    whole, _, frac = text.partition("点")
    total, digit = 0, 0
    for ch in whole:
        if ch == "十":
            total += (digit or 1) * 10
            digit = 0
        elif ch == "百":
            total += (digit or 1) * 100
            digit = 0
        else:
            digit = _CN.get(ch, 0)
    value = float(total + digit)
    if frac:
        value += float("0." + "".join(str(_CN.get(ch, 0)) for ch in frac))
    return value


def _number(text: str) -> float:
    text = str(text or "")
    try:
        return float(text)
    except ValueError:
        return _cn_value(text)


def night_red_line(text: str) -> str:
    """Why a draft must not go out by itself at night ("" = fine): it names a percentage above
    ``NIGHT_MAX_PERCENT`` (26%、30 %、百分之三十、三十个点、30个点、三成、full-width digits too; a range by its
    top: 「百分之二十到三十」, 「二十多个点」), except what a product is made of (「100%羊毛」, 「100% 棉」), or
    one of ``NIGHT_RED_WORDS`` (坑位、保底、返现、定金、专属价 …)."""
    plain = unicodedata.normalize("NFKC", str(text or ""))
    for pattern, scale in _PERCENTS:
        for m in pattern.finditer(plain):
            values = [_number(m.group("n"))]
            if "m" in pattern.groupindex and m.group("m"):
                values.append(_number(m.group("m")))
            top = max(values) * scale + (10 if m.groupdict().get("more") else 0)
            if top <= NIGHT_MAX_PERCENT:
                continue
            if scale == 1 and "个" not in m.group(0) and "点" not in m.group(0) and _MATERIAL_AFTER.match(plain, m.end()):
                continue                                   # 100%羊毛: what the hat is made of
            return f"说到了「{m.group(0).strip()}」（超过 {NIGHT_MAX_PERCENT}%）"
    for word in NIGHT_RED_WORDS:
        if word in plain:
            return f"说到了「{word}」"
    return ""


# A holding line names no amount, ratio, count or date: digits are checked apart; these are the Chinese numerals
# with what they count (``_KEY``'s units and more), and 百分之 / 百分百 / 三万 without a unit.
_HOLDING_NUMBERS = re.compile(
    r"百分[之百]|[零〇一二两三四五六七八九十半几]+[百千万亿]"
    r"|[零〇一二两三四五六七八九十百千万亿半几]+\s*(?:元|块|毛|角|个点|个百分点|成|折|倍|价|天|号|日|月|周|星期|礼拜|年"
    r"|点|小时|钟头|分钟|秒|份|个|条|款|件|顶)")
_A_LITTLE = re.compile(r"(?<![零〇一二两三四五六七八九十百几])一点(?![零〇一二两三四五六七八九十钟半])")   # 晚一点：a little


def night_holding_problem(text: str) -> str:
    """Why ``text`` is not one safe holding line for a draft the model flagged (needs_attention), so it waits
    for a person even at night ("" = it may go): a digit (ASCII or full-width: amounts, dates, placeholders
    〈手机1〉 too), a Chinese numeral with what it counts (三十个点、两天、三顶、一个；「晚一点」 is no
    number), a red-line word, more than NIGHT_HOLDING_CHARS characters, or no word saying the operators come
    back to her (``NIGHT_CUES``: 记下、明早、确认、第一时间、回您 …)."""
    plain = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not plain:
        return "没有文字"
    if re.search(r"\d", plain):
        return "有数字"
    m = _HOLDING_NUMBERS.search(_A_LITTLE.sub("", plain))
    if m:
        return f"有数量或日期「{m.group(0)}」"
    for word in NIGHT_RED_WORDS:
        if word in plain:
            return f"说到了「{word}」"
    if len(plain) > NIGHT_HOLDING_CHARS:
        return f"超过 {NIGHT_HOLDING_CHARS} 个字"
    if not any(cue in plain for cue in NIGHT_CUES):
        return "没有说会记下、明早确认或第一时间回她"
    return ""


def night_holding_ok(text: str) -> bool:
    """Is ``text`` one safe holding / transition line (``night_holding_problem`` finds nothing)?"""
    return not night_holding_problem(text)


def night_reason(kind: str, *, needs_attention: bool, mode: str, unknown_tokens: list, reason: str, text: str,
                 waybill: str, attachments: list, files_named: int = 0, when: str = "夜里") -> str:
    """Why a draft that waits for a person must not go out by itself in the night window either ("" = it may):
    a complaint never; everything ``plain_reason`` checks but the model's attention; ``night_red_line``; and a
    draft the model flagged goes only as one safe holding line without files (``night_holding_problem``).
    ``when``: how the reasons name the time (自动模式 by day: 「自动模式下」)."""
    if kind == "complaint":
        return "投诉一律等运营"
    why = plain_reason(kind, needs_attention=False, mode=mode, unknown_tokens=unknown_tokens, reason=reason,
                       text=text, waybill=waybill, attachments=attachments, files_named=files_named)
    if why:
        return why
    red = night_red_line(text)
    if red:
        return red
    if needs_attention:
        if attachments:
            return f"模型要求注意，草稿还带着文件（{when}只发一句过渡的话）"
        problem = night_holding_problem(text)
        if problem:
            return f"模型要求注意，回复又不是一句稳妥的过渡话：{problem}"
    return ""
