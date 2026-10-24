"""The Mac's answer for a creator's messages: a draft plus slot updates, checked here.

The worker asks Claude for one JSON object (``--json-schema``)::

    {"intent": "…", "slots": {"收件人": "…"}, "stage_proposal": "address",
     "message_kind": "address_confirm", "reply_draft": "…", "attachments_to_send": ["assets/…"],
     "needs_attention": false, "reason": "…"}

This side is where it is trusted or not: ``parse_draft`` validates instead of
repairing; anything it cannot use raises ``DraftError`` and nothing is drafted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

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


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DraftError("bad_type", f"expected text, got {type(value).__name__}")
    return value.strip()[:limit]


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
    needs = data.get("needs_attention", False)
    if not isinstance(needs, bool):
        raise DraftError("bad_needs_attention")
    draft = _text(data.get("reply_draft"), MAX_DRAFT_CHARS + 1)
    if len(draft) > MAX_DRAFT_CHARS:
        raise DraftError("draft_too_long")
    return Draft(
        intent=_text(data.get("intent"), 200),
        slots=clean_slots,
        stage_proposal=stage,
        message_kind=kind,
        reply_draft=draft,
        attachments_to_send=[str(f).strip()[:200] for f in files if str(f).strip()],
        needs_attention=needs,
        reason=_text(data.get("reason"), 300),
    )
