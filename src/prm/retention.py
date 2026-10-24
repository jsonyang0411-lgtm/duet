"""8.2: a delivered deal's receiving details are cleared 30 days after delivery.

Addresses go to the model in plain text (the user's decision: it has to judge whether
an address is complete); phone and ID numbers never leave this PC except as
placeholders.  Both follow the same life: ``RETENTION_DAYS`` after the deal is
delivered, every copy is cut down to province and city / the masked number.

``scrub`` is the one rule for every copy: the addresses known (the deal's slots, and
any written out in the text itself after a label or from the province / city on), then
anything that still looks like a Chinese street address, then pieces of a known
address quoted without its beginning (the model writes "张江路100号3栋1201" back),
then phone and ID numbers.  The Mac worker carries the same rule for the case files
(a test keeps the two in step).
"""

from __future__ import annotations

import re

from . import pii

RETENTION_DAYS = 30
CLEARED = "（详细地址已清除）"
ADDRESS_KEY = re.compile(r"地址|住址")
_REGION = re.compile(r"^\s*((?:[一-龥]{2,7}?(?:省|自治区|特别行政区))?(?:[一-龥]{2,7}?(?:市|自治州|地区|盟))?)")
# Starts with a province or a city, then street-address characters.
ADDRESS = re.compile(r"(?:[一-龥]{2,7}?(?:省|自治区|特别行政区|市))"
                     r"[一-龥A-Za-z0-9#\-－（）()]{3,60}")
# "地址：杭州西湖区文三路 90 号" (no province or city in front): the value after the label.
ADDRESS_LINE = re.compile(r"((?:收件|详细|收货)?(?:地址|住址)\s*[:：]\s*)([^\n，,。；;]{4,80})")
_STREET = re.compile(r"\d|[路街道号栋幢室弄巷村镇区县单元楼层]")
PIECE_MIN = 5           # a piece of a known address this long, with a street mark, is cut too


def coarse_address(address: str) -> str:
    """Province and city of an address ("" when it does not start with them)."""
    m = _REGION.match(str(address or ""))
    return m.group(1) if m else ""


def cleared(address: str) -> str:
    return f"{coarse_address(address)}{CLEARED}"


def address_values(slots: dict) -> list[str]:
    """The addresses a deal's slots hold, longest first (so a part never cuts a whole)."""
    values = {str(v).strip() for k, v in (slots or {}).items() if ADDRESS_KEY.search(str(k)) and str(v).strip()}
    return sorted((v for v in values if CLEARED not in v), key=len, reverse=True)


def _street(m: re.Match) -> str:
    text = m.group(0)
    if CLEARED in text or not _STREET.search(text[len(coarse_address(text)):]):
        return text                                   # "上海市场不错": not an address
    return cleared(text)


def _written_out(text: str) -> list[str]:
    """Addresses ``text`` itself writes out: after a label, or from the province / city on."""
    found = [m.group(2).strip() for m in ADDRESS_LINE.finditer(text)] + [m.group(0) for m in ADDRESS.finditer(text)]
    return [a for a in found if CLEARED not in a and _STREET.search(a[len(coarse_address(a)):])]


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
    to province and city, and phone / ID numbers masked."""
    text = str(text or "")
    known = sorted({a.strip() for a in [*addresses, *_written_out(text)] if str(a).strip()}, key=len, reverse=True)
    for address in known:
        text = text.replace(address, cleared(address))
    text = ADDRESS_LINE.sub(lambda m: m.group(0) if CLEARED in m.group(2) else m.group(1) + cleared(m.group(2)), text)
    text = ADDRESS.sub(_street, text)
    for address in known:
        detail = address[len(coarse_address(address)):]
        if len(detail) >= PIECE_MIN:
            text = _cut_pieces(text, detail)
    return pii.mask_numbers(text)


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
