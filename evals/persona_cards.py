"""Persona cards: a persona as data.

The goal: given any persona description, the
local model plays it well.  So training uses many personas and the evaluation
uses personas the training never saw.  A card is one JSON object per line:

    {"id": "p-001", "persona_name": "老赵", "persona_prompt": "...", "reply_rules": "...", "max_chars": 40}

the same fields as the persona settings of the bot (CHAT_PERSONA_NAME,
CHAT_PERSONA_PROMPT, CHAT_REPLY_RULES, CHAT_MAX_CHARS).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

CARD_KEYS = ("id", "persona_name", "persona_prompt", "reply_rules", "max_chars")
HELD_OUT = Path(__file__).resolve().parent / "cases" / "personas_eval_v1.jsonl"


def check_card(card: Any) -> list[str]:
    """Problems with one card (empty when it is usable)."""
    if not isinstance(card, Mapping):
        return ["not an object"]
    problems = [f"missing {key}" for key in CARD_KEYS if key not in card]
    for key in ("id", "persona_name", "persona_prompt", "reply_rules"):
        if key in card and not (isinstance(card[key], str) and card[key].strip()):
            problems.append(f"{key} must be a non-empty string")
    if "max_chars" in card and not (isinstance(card["max_chars"], int) and 8 <= card["max_chars"] <= 200):
        problems.append("max_chars must be an integer between 8 and 200")
    return problems


def load_cards(path: str | Path) -> list[dict]:
    """The cards of a file; a bad card or a repeated id or name is an error."""
    cards, ids, names = [], set(), set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        card = json.loads(line)
        problems = check_card(card)
        if problems:
            raise ValueError(f"{path}:{number}: {'; '.join(problems)}")
        if card["id"] in ids or card["persona_name"] in names:
            raise ValueError(f"{path}:{number}: repeated id or name {card['id']} / {card['persona_name']}")
        ids.add(card["id"])
        names.add(card["persona_name"])
        cards.append({key: card[key] for key in CARD_KEYS})
    return cards


def pick_card(cards: list[dict], card_id: str | None) -> dict:
    """The card named ``card_id``, or the only card of the file."""
    if card_id is None:
        if len(cards) != 1:
            raise ValueError(f"the file has {len(cards)} cards: name one with its id")
        return cards[0]
    for card in cards:
        if card["id"] == card_id:
            return card
    raise ValueError(f"no card {card_id!r} (there are {', '.join(c['id'] for c in cards)})")


def apply_card(summarizer, card: Mapping[str, Any]) -> None:
    """Make ``summarizer`` speak as the card (its other settings stay)."""
    summarizer.configure_chat_persona(
        persona_name=card["persona_name"], persona_prompt=card["persona_prompt"],
        reply_rules=card["reply_rules"], max_chars=card["max_chars"],
        context_window_sec=getattr(summarizer, "chat_context_window_sec", 900),
        context_count=getattr(summarizer, "chat_context_count", 40),
    )


def card_from_settings(settings: Mapping[str, Any], card_id: str) -> dict:
    """A card from persona settings (``summarizer._chat_persona_settings()``)."""
    return {"id": card_id, "persona_name": settings["persona_name"], "persona_prompt": settings["persona_prompt"],
            "reply_rules": settings["reply_rules"], "max_chars": int(settings["max_chars"])}


def held_out_names() -> set[str]:
    """Names of the evaluation personas: no training card may use them."""
    return {card["persona_name"] for card in load_cards(HELD_OUT)}
