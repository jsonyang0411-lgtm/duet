"""The reply envelope: what the model must return, and how it is checked.

The model answers with one JSON object::

    {"addressee":"me","action":"reply","messages":["好呀"],"reason":"被问晚饭"}
    {"addressee":"others","action":"silent","messages":[],"reason":"他们在互相聊"}

Silence is an explicit ``action``, never an empty string, so a model that
explains itself ("沉默，因为……") can no longer leak that text into the chat:
only ``messages`` of a ``reply`` envelope are ever sent, and each message still
goes through the last-line rules in :mod:`src.pipeline.text`.

``parse_envelope`` validates instead of repairing.  Anything it cannot trust
raises :class:`EnvelopeError` with a short ``code``; the caller decides whether
to ask the model once more or to stay silent.
"""

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .text import finalize_message

ACTION_REPLY = "reply"
ACTION_SILENT = "silent"
ACTIONS = (ACTION_REPLY, ACTION_SILENT)

ADDRESSEE_ME = "me"
ADDRESSEE_OTHERS = "others"
ADDRESSEE_GROUP = "group"
ADDRESSEE_UNKNOWN = "unknown"
ADDRESSEES = (ADDRESSEE_ME, ADDRESSEE_OTHERS, ADDRESSEE_GROUP)

MAX_MESSAGES = 2
MAX_RAW_CHARS = 4000       # a longer answer is a rambling model, not an envelope
MAX_REASON_CHARS = 60

_ACTION_ALIASES = {
    "reply": ACTION_REPLY, "speak": ACTION_REPLY, "say": ACTION_REPLY,
    "respond": ACTION_REPLY, "answer": ACTION_REPLY, "回复": ACTION_REPLY,
    "silent": ACTION_SILENT, "silence": ACTION_SILENT, "skip": ACTION_SILENT,
    "quiet": ACTION_SILENT, "pass": ACTION_SILENT, "ignore": ACTION_SILENT,
    "no_reply": ACTION_SILENT, "noreply": ACTION_SILENT, "none": ACTION_SILENT,
    "沉默": ACTION_SILENT, "不回复": ACTION_SILENT,
}
_ADDRESSEE_ALIASES = {
    "me": ADDRESSEE_ME, "bot": ADDRESSEE_ME, "self": ADDRESSEE_ME,
    "you": ADDRESSEE_ME, "我": ADDRESSEE_ME, "对我": ADDRESSEE_ME,
    "others": ADDRESSEE_OTHERS, "other": ADDRESSEE_OTHERS,
    "someone": ADDRESSEE_OTHERS, "member": ADDRESSEE_OTHERS,
    "他人": ADDRESSEE_OTHERS, "别人": ADDRESSEE_OTHERS,
    "group": ADDRESSEE_GROUP, "all": ADDRESSEE_GROUP, "everyone": ADDRESSEE_GROUP,
    "nobody": ADDRESSEE_GROUP, "大家": ADDRESSEE_GROUP, "没有": ADDRESSEE_GROUP,
}
_MESSAGE_KEYS = ("messages", "reply", "message", "text", "content")
# A model that loses track of the JSON writes another field into the messages:
# ["刚满十八", "reason被@确认年龄"], or "……饿不动了。 reason".  Such text must not
# reach the chat.  CJK letters count as word characters, hence the lookahead.
_FIELD_NAME_ITEM_RE = re.compile(r'^\s*["“]?(?:reason|addressee|action|follow_up|sticker)(?![A-Za-z])', re.I)
_TRAILING_REASON_RE = re.compile(r'\s+["“]?reason["”]?\s*[:：]?\s*$', re.I)


# For providers with schema-constrained output.  Limits such as the message
# length are enforced by parse_envelope, not here: providers reject most
# length keywords.
ENVELOPE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "addressee": {"type": "string", "enum": list(ADDRESSEES)},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "messages": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["addressee", "action", "messages", "reason"],
    "additionalProperties": False,
}


# The model may say whom it is waiting for after a reply (FOLLOWUP_MODE=declared).
EXPECT_SENDER = "sender"        # the person it just answered, within ``seconds``
EXPECT_ANYONE = "anyone"        # whoever answers; the usual heuristics decide
EXPECT_NONE = "none"            # nothing: do not listen after this reply
EXPECTS = (EXPECT_SENDER, EXPECT_ANYONE, EXPECT_NONE)
MIN_FOLLOW_UP_SEC, MAX_FOLLOW_UP_SEC, DEFAULT_FOLLOW_UP_SEC = 10, 180, 60

_EXPECT_ALIASES = {
    "sender": EXPECT_SENDER, "asker": EXPECT_SENDER, "user": EXPECT_SENDER, "requester": EXPECT_SENDER,
    "them": EXPECT_SENDER, "对方": EXPECT_SENDER, "他": EXPECT_SENDER,
    "anyone": EXPECT_ANYONE, "any": EXPECT_ANYONE, "group": EXPECT_ANYONE, "all": EXPECT_ANYONE,
    "大家": EXPECT_ANYONE, "任何人": EXPECT_ANYONE,
    "none": EXPECT_NONE, "no": EXPECT_NONE, "nobody": EXPECT_NONE, "false": EXPECT_NONE,
    "无": EXPECT_NONE, "不用": EXPECT_NONE,
}

# Same as ENVELOPE_JSON_SCHEMA plus the optional field (used only in that mode).
ENVELOPE_JSON_SCHEMA_FOLLOWUP = {
    **ENVELOPE_JSON_SCHEMA,
    "properties": {
        **ENVELOPE_JSON_SCHEMA["properties"],
        "follow_up": {
            "type": "object",
            "properties": {
                "expect": {"type": "string", "enum": list(EXPECTS)},
                "seconds": {"type": "integer"},
            },
            "required": ["expect", "seconds"],
            "additionalProperties": False,
        },
    },
}


# The model may add a sticker by name (SEND_STICKERS=on).  The names are checked
# by parse_envelope, not by an enum here: the schema objects stay the same from
# call to call, so a provider that refuses one is remembered once.
_STICKER_PROPERTY = {"sticker": {"type": "string"}}
ENVELOPE_JSON_SCHEMA_STICKER = {
    **ENVELOPE_JSON_SCHEMA,
    "properties": {**ENVELOPE_JSON_SCHEMA["properties"], **_STICKER_PROPERTY},
}
ENVELOPE_JSON_SCHEMA_FOLLOWUP_STICKER = {
    **ENVELOPE_JSON_SCHEMA_FOLLOWUP,
    "properties": {**ENVELOPE_JSON_SCHEMA_FOLLOWUP["properties"], **_STICKER_PROPERTY},
}


def envelope_schema(*, follow_up: bool, sticker: bool) -> dict | None:
    """The schema for these optional fields; None means the backend's default envelope schema."""
    if follow_up and sticker:
        return ENVELOPE_JSON_SCHEMA_FOLLOWUP_STICKER
    if follow_up:
        return ENVELOPE_JSON_SCHEMA_FOLLOWUP
    if sticker:
        return ENVELOPE_JSON_SCHEMA_STICKER
    return None


def parse_sticker(value, offered) -> str:
    """The offered sticker the model named, or ""."""
    if not isinstance(value, str):
        return ""
    name = value.strip().strip("\"'“”‘’[]【】「」")
    if name.startswith(("表情包：", "表情包:")):
        name = name[4:].strip()
    return name if name in offered else ""


@dataclass(frozen=True)
class FollowUp:
    expect: str
    seconds: int = DEFAULT_FOLLOW_UP_SEC


def parse_follow_up(value) -> "FollowUp | None":
    """The declaration as a ``FollowUp``, or None.  It is a hint: anything odd is ignored."""
    try:
        if isinstance(value, dict):
            raw_expect, raw_seconds = value.get("expect"), value.get("seconds", DEFAULT_FOLLOW_UP_SEC)
        else:
            raw_expect, raw_seconds = value, DEFAULT_FOLLOW_UP_SEC
        if isinstance(raw_expect, bool):
            raw_expect = "sender" if raw_expect else "none"
        expect = _EXPECT_ALIASES.get(str(raw_expect or "").strip().strip("\"'").lower())
        if expect is None:
            return None
        if isinstance(raw_seconds, bool):
            raw_seconds = DEFAULT_FOLLOW_UP_SEC
        seconds = int(float(raw_seconds))
    except (TypeError, ValueError, OverflowError):
        return None
    return FollowUp(expect, min(MAX_FOLLOW_UP_SEC, max(MIN_FOLLOW_UP_SEC, seconds)))


class EnvelopeError(ValueError):
    """The model output is not a usable envelope.

    ``code`` is stable and countable: ``empty``, ``too_long``, ``no_json``,
    ``bad_json``, ``not_object``, ``missing_action``, ``bad_action``,
    ``bad_messages``, ``reply_without_text``, ``sticker_unknown`` (a reply that
    is only a sticker whose name was not offered).
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Envelope:
    action: str
    addressee: str
    messages: tuple[str, ...]
    reason: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)
    follow_up: "FollowUp | None" = None
    sticker: str = ""

    @property
    def text(self) -> str:
        """The messages as the single line this pipeline stage sends."""
        return " ".join(self.messages)

    @property
    def is_reply(self) -> bool:
        return self.action == ACTION_REPLY and (bool(self.messages) or bool(self.sticker))


# ── extracting the JSON object ───────────────────────────────────────

def iter_json_objects(text: str) -> Iterator[str]:
    """Yield every balanced ``{...}`` span of ``text`` (string-aware)."""
    position = text.find("{")
    while position != -1:
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for index in range(position, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            return                       # unbalanced from here on: truncated
        yield text[position:end + 1]
        position = text.find("{", end + 1)


_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _load_object(span: str):
    """json.loads, then a safe literal fallback for Python-style dicts."""
    try:
        return json.loads(span)
    except Exception:                      # noqa: BLE001 - JSONDecodeError, RecursionError, ...
        pass
    relaxed = _TRAILING_COMMA_RE.sub(r"\1", span)
    try:
        return json.loads(relaxed)
    except Exception:                      # noqa: BLE001
        pass
    try:
        return ast.literal_eval(relaxed)   # single quotes, True/False/None
    except Exception:                      # noqa: BLE001 - untrusted text: any failure is "not an object"
        return None


load_json_object = _load_object        # public name for the memory extractor


def extract_envelope_dict(raw: str) -> dict:
    """Find the envelope object in ``raw`` (code fences and prose tolerated)."""
    text = str(raw or "").strip()
    if not text:
        raise EnvelopeError("empty")
    if len(text) > MAX_RAW_CHARS:
        raise EnvelopeError("too_long", f"{len(text)} chars")
    candidates: list[dict] = []
    saw_span = False
    for span in iter_json_objects(text):
        saw_span = True
        value = _load_object(span)
        if isinstance(value, dict):
            candidates.append(value)
    if not saw_span:
        # A doubly encoded answer: "{\"action\": ...}" as one JSON string.
        try:
            inner = json.loads(text)
        except Exception:                  # noqa: BLE001
            inner = None
        if isinstance(inner, str) and inner.strip() != text:
            return extract_envelope_dict(inner)
        raise EnvelopeError("no_json", text[:60])
    if not candidates:
        raise EnvelopeError("bad_json", text[:60])
    for value in candidates:
        if "action" in value:
            return value
    for value in candidates:            # {"result": {"action": ...}}
        for inner in value.values():
            if isinstance(inner, dict) and "action" in inner:
                return inner
    return candidates[0]


# ── validating the fields ────────────────────────────────────────────

def _normalize_action(value) -> str:
    if isinstance(value, bool):
        return ACTION_REPLY if value else ACTION_SILENT
    key = str(value or "").strip().strip("\"'").lower().replace(" ", "_")
    action = _ACTION_ALIASES.get(key)
    if action is None:
        raise EnvelopeError("bad_action", str(value)[:30])
    return action


def _normalize_addressee(value) -> str:
    key = str(value or "").strip().strip("\"'").lower()
    return _ADDRESSEE_ALIASES.get(key, ADDRESSEE_UNKNOWN)


def _raw_messages(data: dict) -> list:
    for key in _MESSAGE_KEYS:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return list(value)
        raise EnvelopeError("bad_messages", f"{key} is {type(value).__name__}")
    return []


def parse_envelope(raw: str, *, max_chars: int, names: Iterable[str] = (),
                   decide_only: bool = False, follow_up: bool = False,
                   stickers: Iterable[str] = ()) -> Envelope:
    """Validate a model answer and return the envelope to act on.

    ``max_chars`` is the budget for the whole text that will be sent; the
    messages share it.  ``names`` are speaker names whose ``name：`` label may
    be stripped from the front of a message.  ``decide_only`` is for the first
    half of the two-step strategy, where ``reply`` carries no text yet.
    ``follow_up`` reads the optional declaration of whom the model waits for.
    ``stickers`` are the sticker names offered; the model may name one of them,
    even with no text (a sticker is a reply on its own).  Other names are ignored.
    """
    data = extract_envelope_dict(raw)
    warnings: list[str] = []
    if "action" not in data:
        raise EnvelopeError("missing_action")
    action = _normalize_action(data["action"])
    addressee = _normalize_addressee(data.get("addressee"))
    if addressee == ADDRESSEE_UNKNOWN:
        warnings.append("addressee_missing")
    reason = re.sub(r"\s+", " ", str(data.get("reason", "") or "")).strip()
    reason = reason[:MAX_REASON_CHARS]

    items = _raw_messages(data)
    offered = frozenset(stickers or ())
    sticker = ""
    if offered and not decide_only and data.get("sticker") not in (None, ""):
        sticker = parse_sticker(data.get("sticker"), offered)
        if not sticker:
            warnings.append("sticker_unknown")
    if action == ACTION_SILENT:
        if any(str(item or "").strip() for item in items):
            warnings.append("text_ignored_on_silent")
        if sticker:
            warnings.append("sticker_ignored_on_silent")
        return Envelope(ACTION_SILENT, addressee, (), reason, tuple(warnings))

    for item in items:
        if not isinstance(item, str):
            raise EnvelopeError("bad_messages", f"item is {type(item).__name__}")
    if decide_only:
        return Envelope(ACTION_REPLY, addressee, (), reason, tuple(warnings))
    # Only beside real text: a reply that is nothing but "action: silent" stays
    # what it was, a silence marker (see finalize_message).
    kept = [item for item in items if not _FIELD_NAME_ITEM_RE.match(item)]
    kept = [_TRAILING_REASON_RE.sub("", item) for item in (kept if any(i.strip() for i in kept) else items)]
    if kept != items:
        warnings.append("field_name_removed")
        items = kept
    declared = parse_follow_up(data.get("follow_up")) if follow_up and "follow_up" in data else None
    if not any(item.strip() for item in items):
        if sticker:
            return Envelope(ACTION_REPLY, addressee, (), reason, tuple(warnings), declared, sticker)
        if "sticker_unknown" in warnings:
            raise EnvelopeError("sticker_unknown", str(data.get("sticker"))[:30])
        raise EnvelopeError("reply_without_text")
    if len(items) > MAX_MESSAGES:
        warnings.append("too_many_messages")
        items = items[:MAX_MESSAGES]

    # The messages share one budget because this stage sends a single bubble:
    # the first may use all of it, a later one is kept only if it fits whole.
    cleaned: list[str] = []
    for item in items:
        room = max_chars - (sum(len(m) for m in cleaned) + len(cleaned))
        if cleaned and room < 2:
            warnings.append("message_dropped")
            break
        text, notes = finalize_message(
            item, max_chars=max_chars if not cleaned else room, names=names,
        )
        if cleaned and "truncated" in notes:
            warnings.append("message_dropped")
            continue
        warnings.extend(notes)
        if text:
            cleaned.append(text)
    if not cleaned:
        # The model asked to reply but everything it wrote was a silence marker
        # or an explanation: that is silence, and it is counted as such.
        return Envelope(ACTION_SILENT, addressee, (), reason,
                        tuple(warnings + ["reply_text_rejected"]))
    return Envelope(ACTION_REPLY, addressee, tuple(cleaned), reason, tuple(warnings), declared, sticker)
