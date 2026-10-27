"""8.2: a deal's receiving details are cleared 90 days after it is done.

Addresses go to the model in plain text (the user's decision: it has to judge whether
an address is complete); phone and ID numbers never leave this PC except as
placeholders.  Both follow the same life: ``RETENTION_DAYS`` after the deal is done,
every copy is cut down to province and city / the masked number.

When a deal is done (``purge_due_at``; the rule both sides agreed, handoff.md D20 / D27):
``RETENTION_DAYS`` after it was delivered, or after its last sign of life (a message,
any change of the deal) - whichever comes first.  A deal closed, or whose sample request
was turned down or ran out, goes quiet: the second clause clears it.  A creator who
writes again after a clean-up starts a new count from that message.

``scrub`` is the one rule for every copy: the addresses known (the deal's slots, and
any written out in the text itself: after a label (``地址：`` / ``| 地址 |`` / ``地址 ``),
from the province / city on (with or without 省 / 市), or a street address with no
province in front: a door number on a road, a building with its unit or room, its
district and the parts around it, also after a name and a phone in one line), then
anything that still looks like one, then pieces of a known address quoted without its
beginning (the model writes "张江路100号3栋1201" back), then phone and ID numbers.
Dates, colours, SKUs, prices and counts ("11月11号发货", "3号色", "第2条") are none of
these.  The Mac worker carries the same rule for the case files (a test keeps the two
in step).
"""

from __future__ import annotations

import json
import re

from . import pii

RETENTION_DAYS = 90
DAY = 86400


def last_activity(deal: dict) -> float:
    """The deal's last sign of life: a message from the creator or any change of the deal."""
    return max(float(deal.get("updated_at") or 0), float(deal.get("last_message_at") or 0))


def purge_due_at(deal: dict, days: int = RETENTION_DAYS):
    """When this deal's details are to be cleared (None: nothing new since the last clean-up).
    Delivered ``days`` ago, or quiet for ``days``; what came before a clean-up does not count again."""
    purged = float(deal.get("purged_at") or 0)
    due = []
    delivered = float(deal.get("delivered_at") or 0)
    if delivered and delivered > purged:
        due.append(delivered + days * DAY)
    active = last_activity(deal)
    if active > purged:
        due.append(active + days * DAY)
    return min(due) if due else None


CLEARED = "（详细地址已清除）"
ADDRESS_KEY = re.compile(r"地址|住址")
# Province-level names, for an address written without 省 / 市 / 自治区 ("广东佛山南海区…").
PROVINCES = ("北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
             "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
             "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门")
_BARE = "(?:" + "|".join(PROVINCES) + ")(?![省市])"
_PROVINCE = rf"(?:[一-龥]{{2,7}}?(?:省|自治区|特别行政区)|{_BARE})"
# A city's name has no district, road or number in it ("广东省南海区某某市场路…" keeps 广东省 only).
_CITY = r"(?:(?![区县旗路街巷号])[一-龥]){2,7}?(?:市|自治州|地区|盟)"
_REGION = re.compile(rf"^\s*((?:{_PROVINCE}(?:\s*{_CITY})?|{_CITY})?)")
# Starts with a province or a city, then street-address characters.
ADDRESS = re.compile(r"(?:[一-龥]{2,7}?(?:省|自治区|特别行政区|市))[一-龥A-Za-z0-9#\-－（）()]{3,60}")
# The same from a province named without 省 / 市; cut only with a district, a door number or a
# building after it ("广东仓发货", "浙江的达人" stay).
BARE_ADDRESS = re.compile(rf"{_BARE}[一-龥A-Za-z0-9#\-－]{{2,60}}")
# The value after a label: "地址：…" (whatever it is), "| 地址 | … |" (a table cell, with a door
# mark), "地址 …" (a bare space: with a district, road or building); never past the end of the
# line, the cell or a JSON string.
ADDRESS_LINE = re.compile(r"((?:收件|详细|收货)?(?:地址|住址)(?:[ \t　]*[:：|][ \t　]*|[ \t　]+))"
                          r"([^\n\r，,。；;\"\\|]{4,80})")
_STREET = re.compile(r"\d|[路街道号栋幢室弄巷村镇区县单元楼层]")
# A street address with no province or city in front is found by one of these: a door number on a
# road (某某路100号, 某某村5组), a building with its unit or room (1栋101, 2号楼301, A座, 2单元301,
# 1201室), a district and a road with a number (南海区某某路东侧100号).  "知道3号", "频道1号",
# "SUV座椅" are not.
_ROAD = r"(?:路|街|大道|巷|胡同|村|(?<![知频渠味报难通管轨跑赛隧弯车航])道)"
_DOOR = re.compile(rf"{_ROAD}\s*\d+(?:[-－]\d+)*\s*(?:号|弄|组|巷|栋|幢)")
_BUILDING = re.compile(r"\d+\s*(?:栋|幢|号楼)\s*\d+|(?<![A-Za-z])[A-Za-z]\s*(?:座|栋|幢)(?![椅位谈垫])"
                       r"|\d+\s*单元\s*\d+|\d{3,4}\s*室")
_AREA_ROAD = re.compile(rf"(?:区|县|旗|镇|乡)[一-龥]{{1,8}}?{_ROAD}[一-龥]{{0,4}}\d+\s*号")
# What makes a word part of an address (a district, a road, a building …), not just a number.
_STRONG = re.compile(r"[区县旗镇乡路街巷弄村栋幢室]|大道|单元|号楼|小区|大厦|花园|公寓|广场|"
                     r"(?<![知频渠味报难通管轨跑赛隧弯车航])道")
_AREA = re.compile(r"[一-龥]{1,8}?(?:区|县|旗|镇|乡|街道)$")
_UNIT = re.compile(r"(?=\S)(?:\d{1,5}(?:[-－]\d{1,5})*(?:号楼|号|室|楼|层|弄|栋|幢|单元|座|户)|[A-Za-z](?:座|栋|幢))*"
                   r"(?:\d{1,5}(?:[-－]\d{1,5})*)?$")
# Where an address ends inside a word ("某某路100号门卫": after 100号).
_PIECE_END = re.compile(r"\d+(?:[-－]\d+)*(?:号楼|号|室|楼|层|弄|栋|幢|单元|座|户)?|[A-Za-z](?:座|栋|幢)|"
                        r"路|街|大道|巷|胡同|村|小区|花园|公寓|大厦|广场|苑")
# Words in front of an address that stay ("寄到…", "新地址是…", "放…门卫").
_LEAD = re.compile(r"(?:[一-龥]{0,3}?(?:地址|住址)(?:是|为|在)?|寄到|寄往|寄去|发到|送到|住在|放在|放到|改成|改为|"
                   r"改到|换成|换到|搬到|是|在|到|放|送|寄)")
# One clause: between punctuation, never starting right after a backslash (a JSON escape).
_CLAUSE = re.compile(r"(?<!\\)[^\n\r，,。；;：:、|/\\\"'“”‘’（）()\[\]{}<>《》〈〉【】!！?？…]+")
_WORD = re.compile(r"[^ \t　]+")
PIECE_MIN = 5           # a piece of a known address this long, with a street mark, is cut too


def coarse_address(address: str) -> str:
    """Province and city of an address ("" when it does not start with them; a province without
    省 counts: "广东佛山…" gives 广东)."""
    m = _REGION.match(str(address or ""))
    return m.group(1) if m else ""


def cleared(address: str) -> str:
    return f"{coarse_address(address)}{CLEARED}"


def address_values(slots: dict) -> list[str]:
    """The addresses a deal's slots hold, longest first (so a part never cuts a whole)."""
    values = {str(v).strip() for k, v in (slots or {}).items() if ADDRESS_KEY.search(str(k)) and str(v).strip()}
    return sorted((v for v in values if CLEARED not in v), key=len, reverse=True)


def _strong(text: str) -> bool:
    return bool(_STRONG.search(text) or _DOOR.search(text) or _BUILDING.search(text))


def _belongs_before(word: str) -> bool:
    """A word in front of a street address that is part of it: a province / city, a district."""
    return coarse_address(word) == word or bool(_AREA.match(word)) or _strong(word)


def _belongs_after(word: str) -> bool:
    """A word after it that still is: a building, a unit, a room ("101", "3栋", "某某花园")."""
    return _strong(word) or bool(_UNIT.match(word))


def _street_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every street address in ``text`` found by its door number, building or
    district (``_DOOR`` / ``_BUILDING`` / ``_AREA_ROAD``), with the words of the same clause around
    it that belong to it ("广东省 佛山市 南海区 某某路100号1栋101"), without a lead-in ("寄到") or
    what follows the last number ("门卫")."""
    spans = []
    for clause in _CLAUSE.finditer(str(text or "")):
        body = clause.group(0)
        found = sorted(m.span() for rx in (_DOOR, _BUILDING, _AREA_ROAD) for m in rx.finditer(body))
        if not found:
            continue
        first_at, last_at = found[0][0], max(e for _, e in found)
        words = [m.span() for m in _WORD.finditer(body)]
        first = next(i for i, (_, e) in enumerate(words) if e > first_at)
        last = max(i for i, (s, _) in enumerate(words) if s < last_at)
        while first > 0 and _belongs_before(body[words[first - 1][0]:words[first - 1][1]]):
            first -= 1
        while last + 1 < len(words) and _belongs_after(body[words[last + 1][0]:words[last + 1][1]]):
            last += 1
        start = words[first][0]
        lead = _LEAD.match(body, start, words[first][1])
        if lead and start < lead.end() <= min(first_at, words[first][1] - 1):
            start = lead.end()
        ends = [m.end() for m in _PIECE_END.finditer(body, words[last][0], words[last][1])]
        end = max(ends + [min(last_at, words[last][1])]) if ends else words[last][1]
        if CLEARED not in body[start:end]:
            spans.append((clause.start() + start, clause.start() + end))
    return spans


def _label_value(m: re.Match) -> str:
    """The address after a label ("" when there is none: cleared already, a table header next to
    the cell, or after a bare space something with no district, road or building)."""
    value = m.group(2).strip()
    if not value or CLEARED in value:
        return ""
    if "|" in m.group(1):
        return value if _STREET.search(value[len(coarse_address(value)):]) else ""
    if not re.search(r"[:：]", m.group(1)) and not _strong(value):
        return ""
    return value


def _labelled(m: re.Match) -> str:
    value = _label_value(m)
    return m.group(1) + m.group(2).replace(value, cleared(value), 1) if value else m.group(0)


def _street(m: re.Match) -> str:
    text = m.group(0)
    if CLEARED in text or not _STREET.search(text[len(coarse_address(text)):]):
        return text                                   # "上海市场不错": not an address
    return cleared(text)


def _bare(m: re.Match) -> str:
    text = m.group(0)
    rest = text[len(coarse_address(text)):]
    if CLEARED in text or not (re.match(r"[一-龥]{1,7}?(?:区|县|旗|镇|乡)", rest) or _DOOR.search(rest)
                               or _BUILDING.search(rest)):
        return text                                   # "广东仓发货": not an address
    return cleared(text)


def _written_out(text: str) -> list[str]:
    """Addresses ``text`` itself writes out: after a label, from the province / city on, or a
    street address found by its door number or building."""
    found = [v for v in (_label_value(m) for m in ADDRESS_LINE.finditer(text)) if v]
    found += [m.group(0) for m in ADDRESS.finditer(text)]
    found += [m.group(0) for m in BARE_ADDRESS.finditer(text) if _bare(m) != m.group(0)]
    found += [text[s:e] for s, e in _street_spans(text)]
    return [a for a in found if CLEARED not in a and _STREET.search(a[len(coarse_address(a)):])]


def _cut_streets(text: str) -> str:
    for start, end in reversed(_street_spans(text)):
        text = text[:start] + cleared(text[start:end]) + text[end:]
    return text


def _cut_pieces(text: str, detail: str) -> str:
    """Every run of ``text`` that is also part of ``detail`` (PIECE_MIN or longer, with a
    street mark) becomes CLEARED."""
    out, i = [], 0
    while i < len(text):
        n = 0
        while i + n < len(text) and text[i:i + n + 1] in detail:
            n += 1
        if n >= PIECE_MIN and _STREET.search(text[i:i + n]):
            out.append(CLEARED)
            i += n
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def scrub(text: str, addresses: list[str]) -> str:
    """``text`` with the deal's addresses, any street address and any piece of a known one cut
    to province and city, and phone / ID numbers masked.  Running it again changes nothing."""
    text = str(text or "")
    known = sorted({a.strip() for a in [*addresses, *_written_out(text)] if str(a).strip()}, key=len, reverse=True)
    for address in known:
        text = text.replace(address, cleared(address))
    text = ADDRESS_LINE.sub(_labelled, text)
    text = ADDRESS.sub(_street, text)
    text = BARE_ADDRESS.sub(_bare, text)
    text = _cut_streets(text)
    for address in known:
        detail = address[len(coarse_address(address)):]
        if len(detail) >= PIECE_MIN:
            text = _cut_pieces(text, detail)
    return pii.mask_numbers(text)


def clear_receivers(text: str) -> str:
    """A JSON text (a packet's platform action, a relay job's meta …) with every sample-order
    receiver (platforms.py's ``{name, phone, province, city, district, address}``) cut down:
    the name to its first character, the number masked, the street address cleared.  Text that
    is not JSON, or has no receiver, comes back as it was."""
    text = str(text or "")
    if '"receiver"' not in text:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    changed = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key == "receiver" and isinstance(value, dict) and "address" in value:
                    cut = _cut_receiver(value)
                    if cut != value:
                        node[key] = cut
                        changed.append(key)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return json.dumps(data, ensure_ascii=False) if changed else text


def _cut_receiver(receiver: dict) -> dict:
    """Province and city stay (as everywhere else); the district goes with the street address."""
    name, phone = str(receiver.get("name") or ""), str(receiver.get("phone") or "")
    return {**receiver, "name": name[:1] + "*" if name else name,
            "phone": pii.masked_value(pii.PHONE, phone) if re.fullmatch(r"1\d{10}", phone) else phone,
            "district": "", "address": CLEARED}


def cleared_slots(slots: dict) -> dict:
    """The deal's slots with every address cut to province and city: the whole value under an
    address key, what looks like an address (or a number) in any other text value."""
    addresses = address_values(slots)
    out = {}
    for k, v in (slots or {}).items():
        if ADDRESS_KEY.search(str(k)) and CLEARED not in str(v):
            out[k] = cleared(v)
        else:
            out[k] = scrub(v, addresses) if isinstance(v, str) else v
    return out
