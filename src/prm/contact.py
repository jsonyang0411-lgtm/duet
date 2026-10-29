"""A creator's contact (her 微信号 / 手机号) for 加百应达人微信 (src/prm/friend_adds.py): pure functions, no state.

- ``parse_contact(kind, value)``: what an operator types into the console (or a contact the 百应 inbox hands on),
  normalized, or ``ContactError`` (in Chinese) why it is none.
- ``find_contacts(text)``: the contacts in a creator's own words: a mainland mobile number (the rule of
  ``pii._PHONE``: 138 1234 5678, 138-1234-5678, +86 …), and a 微信号 ONLY after a label (微信 / vx / wx / v信 / 薇 /
  威 / 加V / ➕V …): an unlabelled latin string (a word, a product code, a link, an 抖音号) is never taken, there are
  far too many of those.
- ``mask_contacts(text)``: the same text with those contacts in their masked form.
- ``masked`` / ``masked_contact`` / ``masked_line``: the masked forms that everything outside ``friend_adds.wechat /
  phone`` shows: the logs, the console, the events, the Mac's prompts.  A 微信号 keeps its first two and last two
  characters (``ab****yz``), a mobile number its first three and last four (``138****0000``).

``masked_line`` is the line the 百应 inbox (dy-im-inbox, webot-04) writes for a 联系方式卡片 in its own records:
「[联系方式卡片] 微信号 ab****yz、手机 138****0001（加微信 #12）」 (agreed 2026-10-29).
"""

from __future__ import annotations

import re

from .pii import _PHONE

WECHAT, PHONE = "wechat", "phone"
KINDS = (WECHAT, PHONE)
LABELS = {WECHAT: "微信号", PHONE: "手机"}
CARD_HEAD = "[联系方式卡片]"


class ContactError(ValueError):
    """Not a 微信号 / 手机号 the bot can search for (the message says why, in Chinese)."""


# WeChat's own rule for a 微信号: 6-20 letters, digits, underscores and hyphens, starting with a letter.
_WECHAT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{5,19}$")
_MOBILE = re.compile(r"^1[3-9]\d{9}$")

# A 微信号 in her words only right after one of these labels.  A latin label (vx / wx / 加V / ➕V / +V) is a word of
# its own: "wxid_…", "vxlan", "加Vip" are no label.
_LABEL = (r"(?:微信|[vV]信|薇信?|威信?|加微|"
          r"(?:加|➕|\+)\s*[vV](?![A-Za-z0-9])|"
          r"(?<![A-Za-z0-9])(?:[vV][xX]|[wW][xX])(?![A-Za-z0-9]))")
_FILLER = r"(?:\s*(?:号码|号|账号|[Ii][Dd]))?\s*(?:[:：=＝]|是[:：]?|为[:：]?)?\s*[「“\"'（(]?\s*"
_LABELLED_WECHAT = re.compile(_LABEL + _FILLER + r"(?P<id>[A-Za-z][A-Za-z0-9_-]{5,19})(?![A-Za-z0-9_-])")
# What may stand just before a number to say what it is (only shown, never needed).
_PHONE_LABEL = re.compile(r"(微信同号|微信|[vV][xX]|[wW][xX]|手机号?|电话|联系方式|tel|TEL|Tel)\s*(?:号码?)?\s*[:：]?\s*$")
# A latin label written right against the number ("vx13800000000"): pii's rule wants no letter before a number.
_GLUED_PHONE = re.compile(r"(?<![A-Za-z0-9])([vV][xX]|[wW][xX]|[tT][eE][lL])(?P<num>(?:\+?86)?(1[3-9]\d)(\d{4})(\d{4}))(?!\d)")
_MASKED = re.compile(r"\*{3,}")
_URL = re.compile(r"(?:https?://|www\.)[^\s，。！？、；（）「」“”<>]+", re.IGNORECASE)   # nothing in a link is a contact


def normalize_phone(value: str) -> str:
    """11 digits without blanks, dashes or a leading +86 / 86 ("" when it is no mainland mobile number)."""
    digits = re.sub(r"[\s\-－]", "", str(value or ""))
    digits = re.sub(r"^\+?86(?=1\d{10}$)", "", digits)
    return digits if _MOBILE.match(digits) else ""


def parse_contact(kind: str, value) -> str:
    """``value`` as the bot would search for it: a 微信号 as written (blanks around it dropped), a 手机号 as 11 digits.
    "" for an empty value; ``ContactError`` for one that is not a 微信号 / 手机号."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if kind == PHONE:
        phone = normalize_phone(text)
        if not phone:
            raise ContactError("手机号要是 11 位的大陆手机号（可以带空格、短横线或 +86）")
        return phone
    if kind == WECHAT:
        if normalize_phone(text):
            raise ContactError("这是手机号：填到「手机号」里（微信可以用手机号搜）")
        if not _WECHAT_ID.match(text):
            raise ContactError("微信号是 6–20 位的字母、数字、下划线或短横线，要以字母开头")
        return text
    raise ContactError(f"不认识的联系方式类型 {kind}")


def parse_contacts(contact) -> dict:
    """``{"wechat"?, "phone"?}`` with every value checked by ``parse_contact`` (empty ones left out)."""
    contact = contact if isinstance(contact, dict) else {}
    out = {}
    for kind in KINDS:
        value = parse_contact(kind, contact.get(kind))
        if value:
            out[kind] = value
    return out


def masked(kind: str, value: str) -> str:
    """ab****yz / 138****0000: enough to recognise her, not enough to add her."""
    value = str(value or "")
    if not value:
        return ""
    if kind == PHONE and len(value) == 11:
        return f"{value[:3]}****{value[-4:]}"
    if len(value) >= 5:
        return f"{value[:2]}****{value[-2:]}"
    return "*" * len(value)


def masked_contact(contact) -> dict:
    """``{"wechat": "ab****yz", "phone": "138****0000"}`` of the values ``contact`` has."""
    contact = contact if isinstance(contact, dict) else {}
    return {kind: masked(kind, contact[kind]) for kind in KINDS if contact.get(kind)}


def masked_line(contact, add_id=None, head: str = CARD_HEAD) -> str:
    """「[联系方式卡片] 微信号 ab****yz、手机 138****0001（加微信 #12）」: the one line a record outside friend_adds keeps of
    a contact (``contact``: the full values, or ``{"unavailable": True}`` when the card's number was not read)."""
    contact = contact if isinstance(contact, dict) else {}
    parts = [f"{LABELS[kind]} {masked(kind, contact[kind])}" for kind in KINDS if contact.get(kind)]
    said = "、".join(parts) if parts else "这次没读到号码"
    return f"{head} {said}" + (f"（加微信 #{int(add_id)}）" if add_id else "")


def has_masked(text) -> bool:
    """Does ``text`` hold a masked number (``138****0000``, ``ab****yz``)?"""
    return bool(_MASKED.search(str(text or "")))


def find_contacts(text) -> list[dict]:
    """The contacts in a creator's own words, in order: ``[{"kind", "value", "start", "end", "label"}]`` (``value``
    normalized; ``start``/``end`` the span of the value in ``text``; ``label`` what stood before it, or "").  A mobile
    number anywhere; a 微信号 only after a label."""
    text = str(text or "")
    found = []
    for m in _PHONE.finditer(text):
        before = _PHONE_LABEL.search(text[max(0, m.start() - 8):m.start()])
        found.append({"kind": PHONE, "value": "".join(m.groups()), "start": m.start(), "end": m.end(),
                      "label": before.group(1) if before else ""})
    for m in _GLUED_PHONE.finditer(text):
        found.append({"kind": PHONE, "value": "".join(m.groups()[2:5]), "start": m.start("num"), "end": m.end("num"),
                      "label": m.group(1)})
    for m in _LABELLED_WECHAT.finditer(text):
        start, end = m.span("id")
        if any(f["start"] < end and start < f["end"] for f in found):
            continue                                       # (a number's digits are no 微信号)
        found.append({"kind": WECHAT, "value": m.group("id"), "start": start, "end": end,
                      "label": text[m.start():start].strip(" ：:=＝「“\"'（(是为")})
    links = [m.span() for m in _URL.finditer(text)]
    found = [f for f in found if not any(a < f["end"] and f["start"] < b for a, b in links)]
    return sorted(found, key=lambda f: f["start"])


def mask_contacts(text) -> str:
    """``text`` with every contact ``find_contacts`` finds in its masked form (the labels stay)."""
    text = str(text or "")
    for f in reversed(find_contacts(text)):
        text = text[:f["start"]] + masked(f["kind"], f["value"]) + text[f["end"]:]
    return text
