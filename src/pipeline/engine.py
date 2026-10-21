"""The decision engine: one request in, one :class:`Decision` out.

Strategies
----------
``single``    one call returns the whole envelope (addressee, action, messages).
``two_step``  an explicit call (@, quote, private, forced) is a single call as
              well — there is nothing to decide.  Everything else first asks a
              short "should I speak?" question, and only when the answer is
              yes writes the reply in a second call.

Failure policy
--------------
* a transport error is retried by the summarizer's usual backoff and, if it
  persists, raised to the caller (same as the legacy ``chat``);
* an empty answer is retried once;
* an answer that is not a usable envelope gets one repair call that shows the
  model its own output and the fault;
* if that fails too the decision is silence with ``status`` ``parse_failed`` /
  ``empty`` so the caller can count it (and apply its own fallback for
  direct calls).
"""

import logging
import os
import time
from dataclasses import replace

from .addressing import LEVEL_EXPLICIT
from .envelope import (
    ACTION_REPLY, ACTION_SILENT, EnvelopeError, envelope_schema, parse_envelope,
)
from .prompts import (
    PROFILE_COMPACT,
    PROFILE_STANDARD,
    STAGE_DECIDE,
    STAGE_GENERATE,
    STAGE_SINGLE,
    build_prompts,
    build_repair_messages,
    build_rewrite_messages,
    effective_max_chars,
)
from .types import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_REPAIRED,
    STRATEGY_SINGLE,
    STRATEGY_TWO_STEP,
    ChatRequest,
    Decision,
)

logger = logging.getLogger(__name__)


def uses_two_steps(request: ChatRequest, strategy: str) -> bool:
    """Whether this request is worth a separate "should I speak?" question."""
    return (
        strategy == STRATEGY_TWO_STEP
        and request.is_group
        and not request.force_reply
        and request.addressing.level != LEVEL_EXPLICIT
    )


def decide(summarizer, request: ChatRequest, *, strategy: str = STRATEGY_SINGLE) -> Decision:
    """Run the strategy for ``request`` and return the model's decision."""
    settings = request.persona or summarizer._chat_persona_settings()
    names = (settings["persona_name"], request.bot_name, request.requester_name)
    max_chars = effective_max_chars(settings, request)

    if not uses_two_steps(request, strategy):
        decision = _ask(summarizer, request, settings, names, max_chars, STAGE_SINGLE)
        decision.strategy = STRATEGY_SINGLE if strategy != STRATEGY_TWO_STEP else STRATEGY_TWO_STEP
        return decision

    first = _decide_step(summarizer, request, settings, names, max_chars)
    first.strategy = STRATEGY_TWO_STEP
    if first.failed or first.action != ACTION_REPLY:
        return first
    second = _ask(summarizer, request, settings, names, max_chars, STAGE_GENERATE)
    second.strategy = STRATEGY_TWO_STEP
    second.calls += first.calls
    if second.addressee == "unknown":
        second.addressee = first.addressee
    if not second.reason:
        second.reason = first.reason
    if first.status == STATUS_REPAIRED and second.status == STATUS_OK:
        second.status = STATUS_REPAIRED
    return second


def _decide_step(summarizer, request: ChatRequest, settings: dict, names, max_chars: int) -> Decision:
    """The "should I speak?" step, on ``summarizer.decide_model`` when one is set
    (LOCAL_TASKS=decide: the local model decides, the cloud model writes).  If
    that model cannot be reached, the main model decides as before."""
    decider = getattr(summarizer, "decide_model", None)
    if decider is None or decider is summarizer:
        return _ask(summarizer, request, settings, names, max_chars, STAGE_DECIDE)
    try:
        return _ask(decider, request, settings, names, max_chars, STAGE_DECIDE)
    except Exception as exc:                              # noqa: BLE001 - the main model decides
        logger.warning("The decide model failed (%s); the main model decides", str(exc)[:120])
        return _ask(summarizer, request, settings, names, max_chars, STAGE_DECIDE)


# ── one stage ───────────────────────────────────────────────────────────

def _ask(summarizer, request: ChatRequest, settings: dict, names, max_chars: int,
         stage: str) -> Decision:
    compact = getattr(summarizer, "prompt_profile", None) == PROFILE_COMPACT
    system, user = build_prompts(settings, request, stage=stage,
                                 profile=PROFILE_COMPACT if compact else PROFILE_STANDARD)
    decide_only = stage == STAGE_DECIDE
    ask_follow_up = request.declare_followup and not decide_only
    offered = () if decide_only else tuple(name for name, _ in request.stickers)
    messages = [{"role": "user", "content": user}]
    calls = 0

    raw, calls = _call(summarizer, request, stage, "ask", system, messages, calls)
    if not raw.strip():
        raw, calls = _call(summarizer, request, stage, "retry_empty", system, messages, calls)
    if not raw.strip():
        return _failure(STATUS_EMPTY, "empty", calls)

    def parse(text):
        return parse_envelope(
            text, max_chars=max_chars, names=names, decide_only=decide_only,
            follow_up=ask_follow_up, stickers=offered,
        )

    try:
        envelope = parse(raw)
        status = STATUS_OK
    except EnvelopeError as first_error:
        if not getattr(summarizer, "pipeline_repair", True):
            return _failure(STATUS_PARSE_FAILED, first_error.code, calls)
        fix_messages = build_repair_messages(user, raw, first_error.code)
        fixed, calls = _call(summarizer, request, stage, "repair", system, fix_messages, calls)
        try:
            envelope = parse(fixed)
            raw = fixed
            status = STATUS_REPAIRED
        except EnvelopeError as second_error:
            return _failure(STATUS_PARSE_FAILED, second_error.code, calls)

    if (
        not decide_only and envelope.is_reply and "truncated" in envelope.warnings
        and envelope.addressee != "others"            # such a reply is dropped anyway
        and getattr(summarizer, "pipeline_rewrite_long", False)
    ):
        envelope, calls = _rewrite_long(summarizer, request, stage, system, user, raw,
                                        max_chars, parse, envelope, calls)

    return Decision(
        action=envelope.action, messages=envelope.messages,
        addressee=envelope.addressee, reason=envelope.reason,
        status=status, strategy=STRATEGY_SINGLE, calls=calls,
        warnings=envelope.warnings, follow_up=envelope.follow_up,
        sticker=envelope.sticker,
    )


def _rewrite_long(summarizer, request, stage, system, user, raw, max_chars, parse, envelope, calls):
    """Ask once for the same answer within the limit; keep the cut one if that fails.

    Only the text is taken from the rewrite: whom the reply is for, the reason
    and a declared follow-up stay those of the first answer (and its sticker,
    unless it had none).
    """
    messages = build_rewrite_messages(user, raw, max_chars)
    failed = replace(envelope, warnings=envelope.warnings + ("rewrite_failed",))
    try:
        shorter_raw, calls = _call(summarizer, request, stage, "rewrite_long", system, messages, calls)
    except Exception as exc:                  # noqa: BLE001 - a transport error: the cut reply stands
        logger.info("Rewrite of an over-long reply failed: %s", exc)
        return failed, calls + 1
    try:
        shorter = parse(shorter_raw)
    except EnvelopeError:
        return failed, calls
    if not shorter.is_reply or not shorter.messages or "truncated" in shorter.warnings:
        return failed, calls
    kept = tuple(w for w in envelope.warnings if w != "truncated")
    return replace(envelope, messages=shorter.messages, sticker=envelope.sticker or shorter.sticker,
                   warnings=kept + ("rewritten_long",)), calls


def _failure(status: str, code: str, calls: int) -> Decision:
    return Decision(
        action=ACTION_SILENT, messages=(), status=status,
        strategy=STRATEGY_SINGLE, calls=calls, error_code=code,
    )


def _call(summarizer, request: ChatRequest, stage: str, label: str, system: str,
          messages: list, calls: int):
    """One model call, logged.  Transport errors propagate to the caller."""
    started = time.monotonic()
    attempts = max(1, int(os.getenv("CHAT_API_MAX_ATTEMPTS", "2") or "2"))
    schema = envelope_schema(
        follow_up=request.declare_followup and stage != STAGE_DECIDE,
        sticker=bool(request.stickers) and stage != STAGE_DECIDE,
    )
    try:
        output = summarizer._retry_with_backoff(
            lambda: (
                summarizer._call_json_api(system, messages, schema=schema)
                if schema is not None
                else summarizer._call_json_api(system, messages)
            ),
            f"AI chat v2 ({stage}/{label})",
            max_attempts=attempts,
        )
    except Exception as exc:
        summarizer._record_call(
            call=request.call_name, system_prompt=system,
            user_prompt=messages[-1]["content"],
            raw_reply="", cleaned_reply="", error=repr(exc)[:300],
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            stage=stage, attempt=label, ambient=request.ambient,
            is_group=request.is_group,
        )
        raise
    text = str(getattr(output, "text", "") or "")
    summarizer._record_call(
        call=request.call_name, system_prompt=system,
        user_prompt=messages[-1]["content"],
        raw_reply=text, cleaned_reply="",
        latency_ms=round((time.monotonic() - started) * 1000, 1),
        stage=stage, attempt=label, ambient=request.ambient,
        force_reply=request.force_reply, is_group=request.is_group,
        usage=getattr(output, "usage", None),
        native_json=bool(getattr(output, "native_json", False)),
        mode=getattr(request.mode, "name", None),
    )
    return text, calls + 1
