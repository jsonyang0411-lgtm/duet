"""Persona cards: many personas for training, unseen ones for the evaluation."""

import json
from types import SimpleNamespace

import pytest

from evals import persona_cards as P
from src.summarize.openai_backend import OpenAISummarizer

CARD = {"id": "p-1", "persona_name": "老王", "persona_prompt": "你是老王，开面馆的。", "reply_rules": "每条回复不超过20字。",
        "max_chars": 20}


def write(tmp_path, *cards):
    path = tmp_path / "cards.jsonl"
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cards), encoding="utf-8")
    return path


def test_cards_load_and_bad_ones_are_named(tmp_path):
    assert P.load_cards(write(tmp_path, CARD, dict(CARD, id="p-2", persona_name="小李"))) == [
        CARD, dict(CARD, id="p-2", persona_name="小李")]
    for bad, message in ((dict(CARD, max_chars=500), "max_chars"), ({"id": "x"}, "missing persona_name"),
                         (dict(CARD, persona_prompt=" "), "persona_prompt")):
        with pytest.raises(ValueError, match=message):
            P.load_cards(write(tmp_path, bad))
    with pytest.raises(ValueError, match="repeated"):
        P.load_cards(write(tmp_path, CARD, dict(CARD, id="p-2")))


def test_a_card_is_picked_by_id_or_alone():
    assert P.pick_card([CARD], None) is CARD
    with pytest.raises(ValueError, match="name one"):
        P.pick_card([CARD, dict(CARD, id="p-2")], None)
    with pytest.raises(ValueError, match="no card"):
        P.pick_card([CARD], "p-9")


def test_a_card_makes_the_model_speak_as_it():
    model = OpenAISummarizer(api_key="k", model="m", base_url="https://relay.example/v1")
    model.configure_chat_persona(context_count=7)
    P.apply_card(model, CARD)
    settings = model._chat_persona_settings()
    assert (settings["persona_name"], settings["persona_prompt"], settings["max_chars"], settings["context_count"]) == (
        "老王", "你是老王，开面馆的。", 20, 7)
    assert P.card_from_settings(settings, "mine") == dict(CARD, id="mine")


def test_the_evaluation_personas_are_varied_and_named():
    cards = P.load_cards(P.HELD_OUT)
    assert len(cards) == 6 and len({c["max_chars"] for c in cards}) >= 4
    assert P.held_out_names() == {c["persona_name"] for c in cards}
