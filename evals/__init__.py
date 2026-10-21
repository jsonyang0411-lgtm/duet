"""Offline evaluation harness for the WeChat group-chat bot (Phase 0).

This package never talks to a network or an LLM.  It replays hand-written
scenarios through the *current* router / proactive gate / reply cleaner with
stubs and reports deterministic numbers, so later refactor phases can be
compared against a fixed baseline.

The module itself only holds the ``replay-case/v1`` schema constants and a
pure-Python validator, so exporters and tests can import it without pulling in
any production code::

    from evals import validate_case
    problems = validate_case(case)      # [] when the case is valid

The metrics live in :mod:`evals.metrics`; the replay and the CLI live in
:mod:`evals.run_offline` (``python -m evals.run_offline``).

Real exports (``tools/export_replay_cases.py``) are slightly looser than the
hand-written fixtures: ``is_reply_to_bot`` may be ``null``, ``config_overrides``
and ``next_messages[*].msg_type`` may be absent, ``expect`` is ``null`` until a
case is labeled and ``expect.addressee`` may be ``null``.  Run such rows
through :func:`normalize_case` and validate with ``allow_unlabeled=True``; the
fixtures in ``evals/cases`` are validated strictly without either.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "replay-case/v1"

ACTIONS = ("speak", "silent", "either")
STRENGTHS = ("must", "should", "free")
ADDRESSEES = ("bot", "other_member", "group", "nobody")

#: Required top-level keys of one case (``bot_state`` may be ``null``).
CASE_KEYS = (
    "schema", "id", "category", "chat", "bot_name", "now", "context",
    "trigger", "next_messages", "bot_state", "expect", "config_overrides",
    "source",
)
CHAT_KEYS = ("chat_id", "group_name", "is_group")
CONTEXT_KEYS = ("offset_sec", "sender_id", "sender_name", "content", "msg_type")
TRIGGER_KEYS = (
    "sender_id", "sender_name", "content", "msg_type", "is_at_mentioned",
    "is_reply_to_bot", "quoted_content", "quoted_sender_name", "offset_sec",
)
BOT_REPLY_KEYS = ("to_sender_id", "text", "offset_sec")
EXPECT_KEYS = ("action", "strength", "addressee", "note")

_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_keys(obj: Any, keys: Iterable[str], where: str, problems: list[str],
                *, strict: bool) -> bool:
    if not isinstance(obj, dict):
        problems.append(f"{where}: expected an object, got {type(obj).__name__}")
        return False
    missing = [k for k in keys if k not in obj]
    if missing:
        problems.append(f"{where}: missing keys {missing}")
    if strict:
        extra = sorted(set(obj) - set(keys))
        if extra:
            problems.append(f"{where}: unexpected keys {extra}")
    return not missing


def _check_str(obj: dict, key: str, where: str, problems: list[str],
               *, non_empty: bool = False) -> None:
    value = obj.get(key)
    if not isinstance(value, str):
        problems.append(f"{where}.{key}: expected a string")
    elif non_empty and not value.strip():
        problems.append(f"{where}.{key}: must not be empty")


def _check_message(obj: Any, keys: tuple[str, ...], where: str,
                   problems: list[str], *, strict: bool) -> None:
    if not _check_keys(obj, keys, where, problems, strict=strict):
        return
    for key in ("sender_id", "sender_name"):
        _check_str(obj, key, where, problems, non_empty=True)
    _check_str(obj, "content", where, problems)
    if not _is_num(obj["offset_sec"]):
        problems.append(f"{where}.offset_sec: expected a number")
    if not _is_int(obj["msg_type"]):
        problems.append(f"{where}.msg_type: expected an integer")


def normalize_case(case: Mapping[str, Any]) -> dict:
    """Fill the documented gaps of exporter output (returns a deep copy).

    Only *absent or null* optional fields are defaulted; nothing is
    invented for a field that carries a value, and ``expect`` stays ``None``
    for unlabeled cases.
    """
    out = copy.deepcopy(dict(case))
    if out.get("config_overrides") is None:
        out["config_overrides"] = {}
    if out.get("next_messages") is None:
        out["next_messages"] = []
    out.setdefault("bot_state", None)
    trigger = out.get("trigger")
    if isinstance(trigger, dict):
        if trigger.get("is_reply_to_bot") is None:
            trigger["is_reply_to_bot"] = False
        for key in ("quoted_content", "quoted_sender_name"):
            if trigger.get(key) is None:
                trigger[key] = ""
    for field in ("context", "next_messages"):
        items = out.get(field)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("msg_type") is None:
                    item["msg_type"] = 1
    expect = out.get("expect")
    if isinstance(expect, dict) and expect.get("note") is None:
        expect["note"] = ""
    return out


def validate_case(case: Any, *, strict: bool = True,
                  allow_unlabeled: bool = False) -> list[str]:
    """Return a list of schema problems for one ``replay-case/v1`` object.

    ``strict`` additionally rejects unknown keys (typos, drifting exporters).
    ``allow_unlabeled`` accepts ``expect: null`` (not labeled yet) and a
    ``null`` ``expect.addressee`` (the labeler lets you skip it).
    An empty list means the case is valid.
    """
    problems: list[str] = []
    if not _check_keys(case, CASE_KEYS, "case", problems, strict=strict):
        return problems

    if case["schema"] != SCHEMA:
        problems.append(f"schema: expected {SCHEMA!r}, got {case['schema']!r}")
    _check_str(case, "id", "case", problems, non_empty=True)
    _check_str(case, "category", "case", problems, non_empty=True)
    if isinstance(case["category"], str) and not _CATEGORY_RE.match(case["category"]):
        problems.append("category: must match [a-z][a-z0-9_]*")
    _check_str(case, "bot_name", "case", problems, non_empty=True)
    _check_str(case, "source", "case", problems, non_empty=True)
    if not _is_int(case["now"]):
        problems.append("now: expected an integer unix timestamp")

    chat = case["chat"]
    if _check_keys(chat, CHAT_KEYS, "chat", problems, strict=strict):
        _check_str(chat, "chat_id", "chat", problems, non_empty=True)
        _check_str(chat, "group_name", "chat", problems)
        if not isinstance(chat["is_group"], bool):
            problems.append("chat.is_group: expected a boolean")

    for field in ("context", "next_messages"):
        items = case[field]
        if not isinstance(items, list):
            problems.append(f"{field}: expected a list")
            continue
        for i, item in enumerate(items):
            _check_message(item, CONTEXT_KEYS, f"{field}[{i}]", problems,
                           strict=strict)

    trigger = case["trigger"]
    if _check_keys(trigger, TRIGGER_KEYS, "trigger", problems, strict=strict):
        _check_message(
            {k: trigger[k] for k in CONTEXT_KEYS}, CONTEXT_KEYS, "trigger",
            problems, strict=False,
        )
        for key in ("is_at_mentioned", "is_reply_to_bot"):
            if not isinstance(trigger[key], bool):
                problems.append(f"trigger.{key}: expected a boolean")
        for key in ("quoted_content", "quoted_sender_name"):
            _check_str(trigger, key, "trigger", problems)

    state = case["bot_state"]
    if state is not None:
        if _check_keys(state, ("last_bot_reply", "sticky_for"), "bot_state",
                       problems, strict=strict):
            reply = state["last_bot_reply"]
            if reply is not None and _check_keys(
                reply, BOT_REPLY_KEYS, "bot_state.last_bot_reply", problems,
                strict=strict,
            ):
                _check_str(reply, "to_sender_id", "bot_state.last_bot_reply",
                           problems, non_empty=True)
                _check_str(reply, "text", "bot_state.last_bot_reply", problems,
                           non_empty=True)
                if not _is_num(reply["offset_sec"]):
                    problems.append("bot_state.last_bot_reply.offset_sec: expected a number")
            sticky = state["sticky_for"]
            if not isinstance(sticky, list) or not all(isinstance(s, str) for s in sticky):
                problems.append("bot_state.sticky_for: expected a list of strings")

    expect = case["expect"]
    if expect is None and allow_unlabeled:
        pass
    elif _check_keys(expect, EXPECT_KEYS, "expect", problems, strict=strict):
        for key, allowed in (("action", ACTIONS), ("strength", STRENGTHS),
                             ("addressee", ADDRESSEES)):
            if key == "addressee" and expect[key] is None and allow_unlabeled:
                continue
            if expect[key] not in allowed:
                problems.append(f"expect.{key}: {expect[key]!r} not in {allowed}")
        _check_str(expect, "note", "expect", problems)

    if not isinstance(case["config_overrides"], dict):
        problems.append("config_overrides: expected an object")
    return problems


def load_jsonl(path: str | Path) -> list[dict]:
    """Read a UTF-8 JSON Lines file (blank lines and ``#`` comments skipped)."""
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON ({exc})") from exc
    return rows


__all__ = [
    "SCHEMA", "ACTIONS", "STRENGTHS", "ADDRESSEES", "CASE_KEYS", "CHAT_KEYS",
    "CONTEXT_KEYS", "TRIGGER_KEYS", "BOT_REPLY_KEYS", "EXPECT_KEYS",
    "normalize_case", "validate_case", "load_jsonl",
]
