"""Phone and ID numbers in a creator's messages become placeholders before they leave this PC.

The model on the Mac sees 〈手机1〉 instead of 13812345678; the bot keeps the number
(``PrmStore.token_for``) and puts it back only when an operator sends the draft
(``unmask``).  The console shows the placeholders with a masked number next to them
(``legend``: 〈手机1〉 = 138****5678).

Addresses are NOT masked: whether an address is complete is exactly what the
model has to judge, and a rule cannot find addresses reliably.
"""

from __future__ import annotations

import re
from typing import Callable

PHONE, ID_CARD = "phone", "id_card"
LABELS = {PHONE: "手机", ID_CARD: "证件"}

# A mainland mobile number, also written 138 1234 5678 / 138-1234-5678 / +86 138…
_PHONE = re.compile(r"(?<![\dA-Za-z])(?:\+?86[-\s]?)?(1[3-9]\d)[-\s]?(\d{4})[-\s]?(\d{4})(?![\dA-Za-z])")
# An 18-character resident ID number (birth date in the middle, last character may be X).
_ID_CARD = re.compile(r"(?<![\dA-Za-z])(\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![\dA-Za-z])")
TOKEN = re.compile(r"〈(手机|证件)(\d+)〉")


def mask(text: str, token_for: Callable[[str, str, str], str]) -> str:
    """``text`` with every phone and ID number replaced by its placeholder.

    ``token_for(kind, label, value)`` gives the placeholder (the same number keeps its one).
    ID numbers go first: their digits must not be read as a phone number.
    """
    text = _ID_CARD.sub(lambda m: token_for(ID_CARD, LABELS[ID_CARD], m.group(1).upper()), str(text))
    return _PHONE.sub(lambda m: token_for(PHONE, LABELS[PHONE], "".join(m.groups())), text)


def unknown_tokens(text: str, tokens: dict) -> list[str]:
    """Placeholders in ``text`` this creator has no number for (the draft cannot be sent as is)."""
    return sorted({m.group(0) for m in TOKEN.finditer(str(text)) if m.group(0) not in tokens})


def unmask(text: str, tokens: dict) -> str:
    """The numbers back in place of their placeholders (``tokens``: ``{placeholder: (kind, value)}``)."""
    return TOKEN.sub(lambda m: tokens[m.group(0)][1] if m.group(0) in tokens else m.group(0), str(text))


def masked_value(kind: str, value: str) -> str:
    """138****5678 / 1101**********123X: enough to recognise, not enough to use."""
    if kind == PHONE and len(value) == 11:
        return f"{value[:3]}****{value[-4:]}"
    if len(value) > 7:
        return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
    return "*" * len(value)


def display(rows: list[dict]) -> dict[str, str]:
    """``{placeholder: what the console shows}`` from ``PrmStore.token_rows``: the masked number,
    or for a cleared one the masked form kept at clean-up time."""
    out = {}
    for row in rows:
        if row.get("purged_at"):
            out[row["token"]] = f"{row.get('masked') or '***'}（已清除）"
        else:
            out[row["token"]] = masked_value(row["kind"], row["value"])
    return out


def legend(text: str, shown: dict[str, str]) -> list[dict]:
    """``[{"token", "masked"}]`` for the placeholders in ``text``, in order of first use."""
    out, seen = [], set()
    for m in TOKEN.finditer(str(text)):
        token = m.group(0)
        if token in seen or token not in shown:
            continue
        seen.add(token)
        out.append({"token": token, "masked": shown[token]})
    return out


def mask_numbers(text: str) -> str:
    """Every phone and ID number in ``text`` in its masked form (138****5678): for clean-ups of
    text that never went through placeholders (the stored chat messages)."""
    text = _ID_CARD.sub(lambda m: masked_value(ID_CARD, m.group(1).upper()), str(text))
    return _PHONE.sub(lambda m: masked_value(PHONE, "".join(m.groups())), text)
