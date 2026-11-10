"""The Mac's answer for a creator's messages: a draft plus slot updates, checked here.

The worker asks Claude for one JSON object (``--json-schema``)::

    {"intent": "…", "slots": {"收件人": "…"}, "stage_proposal": "address",
     "message_kind": "address_confirm", "reply_draft": "…", "attachments_to_send": ["assets/…"],
     "needs_attention": false, "reason": "…",
     "platform_action": {"action": "dy.designated_commission", "item_id": "…", …}}

The worker keeps in ``attachments_to_send`` only the files it sent back (what an older bot
counts) and adds ``attachments_named`` (every file the draft named) and
``attachments_missing`` (``[{"name", "why"}]``: outside assets/, missing, too big, the upload
failed); an older worker sends neither.

``handcards`` (handcards.py, 直播手卡: the products to make cards of) is in a job's schema only when its prompt
offers cards (the worker's ``prm_schema``; this SCHEMA is the one without it); the Mac makes them and hands
back ``handcards_made``, its verdict (the cards are among the files that came back).

``operator_files`` (PRM_DRAFT_CONTEXT, src/prm/draft_context.py) are files the Mac sent back for the operators only
(``[{"name", "why", "sha256"}]``: a sample order's side-by-side photo sheet), never attachments for the creator;
``draft_context: {"v": 3, "gated": true}`` says the Mac checked the draft's sample order by photo (``Draft.gated``;
an older mark is not one); ``photo_held`` names the item codes that check held for photo reasons (the console's
「对过图了」) and ``photo_confirmed`` those it let go only by an operator's 「对过图了」 (``Draft.photo_held`` /
``Draft.photo_confirmed``); ``photo_hats`` where an operator sees each held one (``Draft.photo_hats``).

``platform_action`` is optional (platforms.py): the Mac checks it and previews it, and
hands back ``platform``, its verdict, in its place; a draft that names an action the Mac
did not preview carries the verdict ``unavailable`` and cannot be executed.  A preview whose
action the bot's own check refuses (``platforms.ActionRefused``: a Mac whose limits are wider
than ``FORM_LIMITS``, say) loses the action, not the draft: ``platform_refused`` says why.  A worker that could not
ask this bot what it knows (GET /relay/health, review 2026-11-09 N1) leaves a 专属链接 out and says so:
``platform_held`` is ``PLATFORM_UNASKED`` (anything else there is ignored).

This side is where it is trusted or not: ``parse_draft`` validates instead of
repairing; anything it cannot use raises ``DraftError`` and nothing is drafted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from . import draft_context, handcards, platforms
from .store import STAGES

# The kinds of message (8.1 of the assessment): they decide later what may go out unreviewed.
MESSAGE_KINDS = {
    "greeting": "寒暄、确认收到", "faq": "常见问答", "address_confirm": "收件信息复述",
    "shipping_notice": "寄出、物流通知", "material": "素材分发", "money": "钱（报价、佣金、结算、赔付）",
    "complaint": "投诉、纠纷", "other": "其他",
}
MAX_DRAFT_CHARS = 1500
MAX_SLOTS = 30
MAX_ATTACHMENTS = 9
# The worker could not ask the bot whether it knows 专属链接 the shop's way: the action was left out (``platform_held``).
PLATFORM_UNASKED = "unasked"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

# The schema the worker hands to ``claude --json-schema`` (the same rules as below).
SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "slots": {"type": "object", "additionalProperties": {"type": "string"}},
        "stage_proposal": {"type": "string", "enum": [""] + list(STAGES)},
        "message_kind": {"type": "string", "enum": list(MESSAGE_KINDS)},
        "reply_draft": {"type": "string"},
        "attachments_to_send": {"type": "array", "items": {"type": "string"}},
        "needs_attention": {"type": "boolean"},
        "reason": {"type": "string"},
        "platform_action": platforms.ACTION_SCHEMA,
    },
    "required": ["intent", "slots", "stage_proposal", "message_kind", "reply_draft",
                 "attachments_to_send", "needs_attention", "reason"],
    "additionalProperties": False,
}


class DraftError(ValueError):
    """The answer is not a usable draft (``code`` says why)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass
class Draft:
    intent: str
    slots: dict = field(default_factory=dict)
    stage_proposal: str = ""
    message_kind: str = "other"
    reply_draft: str = ""
    attachments_to_send: list = field(default_factory=list)
    needs_attention: bool = False
    reason: str = ""
    # The Mac's preview of the draft's platform action (platforms.check_block), or None.
    platform: Optional[dict] = None
    # Every file the draft named (None: an older worker, which reports only the ones it sent back)
    # and why some of them did not come (``[{"name", "why"}]``).
    attachments_named: Optional[list] = None
    attachments_missing: list = field(default_factory=list)
    # Why the Mac's platform action was left out ("" = it was not): the bot's own check refused it
    # (platforms.ActionRefused); the reply stays, as a plain draft for a person.
    platform_refused: str = ""
    # The Mac's 直播手卡 for this draft (handcards.check_made: the cards among its files, or why none), or None.
    handcards: Optional[dict] = None
    # ``PLATFORM_UNASKED``: the Mac left a 专属链接 out, not able to ask this bot what it knows ("" = it did not)
    platform_held: str = ""
    # The files the Mac sent back for the operators only (PRM_DRAFT_CONTEXT: a sample order's photo sheet),
    # ``[{"name", "why", "sha256"}]`` (draft_context.operator_entries): never sent to the creator.
    operator_files: list = field(default_factory=list)
    # The Mac checked the draft's sample order by photo (its ``draft_context: {"v": 3, "gated": true}``;
    # draft_context.ungated).
    gated: bool = False
    # The item codes that check held for photo reasons (its ``photo_held``: the console offers 「对过图了」 for each)
    # and those it let go only by an operator's 「对过图了」 (``photo_confirmed``); draft_context.codes_of.
    photo_held: list = field(default_factory=list)
    photo_confirmed: list = field(default_factory=list)
    # Where an operator sees each held code (its ``photo_hats``, 2026-11-10: draft_context.hat_marks); None: an older
    # worker's answer without it (the bot reads the preview's words instead).
    photo_hats: Optional[list] = None


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DraftError("bad_type", f"expected text, got {type(value).__name__}")
    return value.strip()[:limit]


def _missing_files(entries: list) -> list:
    """``[{"name", "why"}]`` of the files the Mac could not send back (anything else is left out)."""
    out = []
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip():
            out.append({"name": str(entry["name"]).strip()[:200], "why": str(entry.get("why") or "").strip()[:120]})
        elif isinstance(entry, str) and entry.strip():
            out.append({"name": entry.strip()[:200], "why": ""})
    return out


def parse_draft(raw: str) -> Draft:
    """The draft in ``raw`` (one JSON object, perhaps in a ``` fence), checked field by field."""
    body = _FENCE.sub("", str(raw or "").strip())
    try:
        data = json.loads(body)
    except ValueError as e:
        raise DraftError("not_json", str(e)[:80]) from None
    if not isinstance(data, dict):
        raise DraftError("not_object")
    slots = data.get("slots") or {}
    if not isinstance(slots, dict):
        raise DraftError("bad_slots")
    clean_slots = {}
    for key, value in list(slots.items())[:MAX_SLOTS]:
        key = str(key).strip()[:40]
        if key and value is not None and not isinstance(value, (dict, list)):
            clean_slots[key] = str(value).strip()[:300]
    stage = _text(data.get("stage_proposal"), 20)
    if stage and stage not in STAGES:
        raise DraftError("bad_stage", stage)
    kind = _text(data.get("message_kind"), 30) or "other"
    if kind not in MESSAGE_KINDS:
        raise DraftError("bad_kind", kind)
    files = data.get("attachments_to_send") or []
    if not isinstance(files, list) or len(files) > MAX_ATTACHMENTS:
        raise DraftError("bad_attachments")
    named, missing = data.get("attachments_named"), data.get("attachments_missing") or []
    if (named is not None and not isinstance(named, list)) or not isinstance(missing, list):
        raise DraftError("bad_attachments")
    needs = data.get("needs_attention", False)
    if not isinstance(needs, bool):
        raise DraftError("bad_needs_attention")
    draft = _text(data.get("reply_draft"), MAX_DRAFT_CHARS + 1)
    if len(draft) > MAX_DRAFT_CHARS:
        raise DraftError("draft_too_long")
    platform, refused = None, ""
    if data.get("platform") is not None:
        try:
            platform = platforms.check_block(data["platform"])
        except platforms.ActionRefused as e:
            refused = str(e)[:200]                       # the reply is still a draft; the action goes
        except platforms.ActionError as e:
            raise DraftError("bad_platform", e.code) from None
    elif data.get("platform_action") is not None:
        # An action the Mac did not preview (an older worker, or one without platforms).
        platform = platforms.unpreviewed_block(data["platform_action"])
    cards = None
    if data.get(handcards.MADE) is not None:
        try:
            cards = handcards.check_made(data[handcards.MADE])
        except ValueError as e:
            raise DraftError("bad_handcards", str(e)[:80]) from None
    return Draft(
        intent=_text(data.get("intent"), 200),
        slots=clean_slots,
        stage_proposal=stage,
        message_kind=kind,
        reply_draft=draft,
        attachments_to_send=[str(f).strip()[:200] for f in files if str(f).strip()],
        needs_attention=needs,
        reason=_text(data.get("reason"), 300),
        platform=platform,
        attachments_named=None if named is None else [str(f).strip()[:200] for f in named if str(f).strip()],
        attachments_missing=_missing_files(missing),
        platform_refused=refused,
        handcards=cards,
        platform_held=PLATFORM_UNASKED if data.get("platform_held") == PLATFORM_UNASKED else "",
        operator_files=draft_context.operator_entries(data.get(draft_context.OPERATOR_FILES)),
        gated=draft_context.gated_of(data),
        photo_held=draft_context.codes_of(data, draft_context.PHOTO_HELD) if draft_context.gated_of(data) else [],
        photo_confirmed=(draft_context.codes_of(data, draft_context.PHOTO_CONFIRMED) if draft_context.gated_of(data)
                         else []),
        photo_hats=draft_context.hat_marks(data) if draft_context.gated_of(data) else None,
    )
