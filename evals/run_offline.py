"""Offline, deterministic evaluation harness (Phase 0 safety net + baseline).

    python -m evals.run_offline [--out evals/baseline] [--seed 7]

Three independent replays, none of which touches a network or an LLM:

1. **Routing replay** - every ``replay-case/v1`` scenario is fed through the
   real :class:`src.router.MessageRouter` (real sticky / follow-up / image
   trackers, real proactive gate, real todo/lots/help handlers, real SQLite
   store) with a stub summarizer.  The stub only records *which* model entry
   point the router tried to call (chat / proactive_chat / describe_image) and
   returns a fixed reply, so we can see whether the router *engaged* the model.
2. **Cleaning replay** - recorded raw model outputs go through
   ``AbstractSummarizer._clean_chat_reply``.
3. **Gate streams** - synthetic Poisson message streams at fixed rates go
   through the real :class:`src.proactive.gate.ProactiveGate` on a fake clock.

Production code is imported and treated as read-only.  Time is faked by
swapping the module-level ``time`` name of the few production modules that use
it (no global ``time.time`` patching); the replay runs inside a throw-away
working directory so nothing can be written under the repo's ``data/``.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import hashlib
import importlib
import json
import logging
import os
import random
import re
import sys
import tempfile
import time as _real_time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # `python evals/run_offline.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from evals import SCHEMA, load_jsonl, normalize_case, validate_case  # noqa: E402
from evals import metrics  # noqa: E402

from src.admin import AdminCommandHandler  # noqa: E402
from src.config import BotConfig  # noqa: E402
from src.db import MessageStore, initialize_db  # noqa: E402
from src.memory.consolidator import MemoryConsolidator  # noqa: E402
try:  # the structured pipeline does not exist in older commits the harness can replay
    from src.pipeline.envelope import EnvelopeError, parse_envelope  # noqa: E402
    from src.pipeline.types import Decision  # noqa: E402
except ImportError:  # pragma: no cover - only when replaying a pre-pipeline checkout
    EnvelopeError = parse_envelope = Decision = None
from src.proactive import gate as gate_module  # noqa: E402
from src.proactive.gate import ProactiveGate  # noqa: E402
from src.proactive.modes import get_modes, lookup_mode, reset_modes  # noqa: E402
from src.proactive.rate_tracker import RateTracker  # noqa: E402
from src.router import MessageRouter  # noqa: E402
from src.summarize.base import AbstractSummarizer  # noqa: E402
from src.todo.handler import TodoHandler  # noqa: E402
from src.trigger import TriggerDetector  # noqa: E402
from src.web_search import WebSearchClient  # noqa: E402
from src import fun as fun_module  # noqa: E402

HARNESS_VERSION = "1"
STUB_REPLY = "好"
BASELINE_SCHEMA = "phase0-baseline/v1"
DEFAULT_CASES = REPO_ROOT / "evals" / "cases" / "scenarios_v1.jsonl"
DEFAULT_RAW_OUTPUTS = REPO_ROOT / "evals" / "cases" / "raw_outputs_v1.jsonl"
DEFAULT_ENVELOPES = REPO_ROOT / "evals" / "cases" / "envelopes_v1.jsonl"
DEFAULT_OUT = REPO_ROOT / "evals" / "baseline"
DEFAULT_TAG = "current"
DEFAULT_SEED = 7

#: Production modules whose module-level ``time`` name is swapped for the fake.
_TIME_MODULES = (
    "src.router",
    "src.proactive.gate",
    "src.proactive.rate_tracker",
    "src.proactive.sticky",
    "src.proactive.followup",
    "src.proactive.image_followup",
    "src.db.store",
    "src.todo.store",
    "src.memory.consolidator",
    "src.web_search",
)

GATE_RATES = (0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0)
GATE_MINUTES = 120
GATE_CHAT_ID = "g_stream@chatroom"
CLEAN_DEFAULT_MAX_CHARS = 30

NOT_MEASURED = (
    "Whether the real LLM actually stays silent when it should (the stub model "
    "always answers; 'engaged' only means the router called the model).",
    "Reply quality, persona fit, factual correctness, latency and cost.",
    "The backend layer: WeChat dedup, message-burst coalescing, real "
    "is_at_mentioned / is_reply_to_bot detection (taken from the case as given).",
    "Real group traffic: the proactive rate window only sees the messages in a "
    "case's context, and gate streams are synthetic Poisson arrivals.",
    "Vision, web search and memory consolidation content (stubbed offline).",
    "A live run needs an API key and a WeChat account; no live mode is "
    "implemented here by design.",
)


# ═════════════════════════════════════════════════════════════════════════
# Fake clock / stubs
# ═════════════════════════════════════════════════════════════════════════

class FakeClock:
    """Module-like stand-in for ``time`` with a manually advanced clock."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def set(self, value: float) -> None:
        self._now = float(value)

    def time(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    def perf_counter(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += max(0.0, float(seconds))

    def __getattr__(self, name: str) -> Any:  # strftime, localtime, ...
        return getattr(_real_time, name)


class FakeDice:
    """Replaces ``random`` inside the proactive gate (only ``random()`` is used)."""

    def __init__(self, value: float = 1.0) -> None:
        self.value = float(value)

    def random(self) -> float:
        return self.value


class SeededDice:
    """Deterministic uniform dice for the gate stream simulation."""

    def __init__(self, seed: str) -> None:
        self._rng = random.Random(seed)

    def random(self) -> float:
        return self._rng.random()


class RecordingSummarizer:
    """Stub model: records which entry point was called, returns a fixed reply."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.last_chat_reply_was_intentional_silence = False
        self._reply = ""
        self._event = ""

    def begin_event(self, event: str, reply: str) -> None:
        self._event = event
        self._reply = reply

    @staticmethod
    def _context_tail(kwargs: dict) -> dict:
        context = kwargs.get("context_messages") or []
        return {
            "context_n": len(context),
            "context_last": context[-1].get("content") if context else None,
        }

    def chat(self, **kwargs: Any) -> str:
        self.calls.append({
            "kind": "chat",
            "event": self._event,
            "ambient": bool(kwargs.get("ambient")),
            "force_reply": bool(kwargs.get("force_reply")),
            **self._context_tail(kwargs),
        })
        self.last_chat_reply_was_intentional_silence = self._reply == ""
        return self._reply

    def proactive_chat(self, **kwargs: Any) -> str:
        mode = kwargs.get("mode")
        self.calls.append({
            "kind": "proactive_chat",
            "event": self._event,
            "mode": getattr(mode, "name", None),
            "reply_probability": getattr(mode, "reply_probability", None),
            **self._context_tail(kwargs),
        })
        return self._reply

    def decide_and_reply(self, request: Any, *, strategy: str = "single") -> Decision:
        """The structured interface: record the addressing, answer like ``chat``."""
        mode = getattr(request, "mode", None)
        self.calls.append({
            "kind": "decide",
            "event": self._event,
            "call": request.call_name,
            "level": request.addressing.level,
            "signals": list(request.addressing.signals),
            "ambient": bool(request.ambient),
            "force_reply": bool(request.force_reply),
            "mode": getattr(mode, "name", None),
            "reply_probability": getattr(mode, "reply_probability", None),
            "strategy": strategy,
            **self._context_tail({"context_messages": request.context_messages}),
        })
        if self._reply:
            return Decision(action="reply", messages=(self._reply,),
                            addressee="me", strategy=strategy)
        return Decision(action="silent", addressee="group", strategy=strategy)

    def describe_image(self, **kwargs: Any) -> str:
        self.calls.append({"kind": "describe_image", "event": self._event})
        return self._reply

    def consolidate_memory(self, existing_memory: str, new_messages: list) -> str:
        return existing_memory


class OfflineNicknames:
    """NicknameService stand-in: identity resolution, never touches disk."""

    def load(self, force: bool = False) -> dict:
        return {}

    def resolve_name(self, wxid: str) -> str:
        return wxid

    def resolve_wxids(self, text: str) -> str:
        return text

    def update(self, wxid: str, nickname: str) -> None:  # pragma: no cover
        return None

    def remove(self, wxid: str) -> None:  # pragma: no cover
        return None


class OfflineWebSearch(WebSearchClient):
    """Real local trigger detection (``extract_query``), no network search."""

    def search(self, query: str) -> list:
        return []


# ═════════════════════════════════════════════════════════════════════════
# Context managers
# ═════════════════════════════════════════════════════════════════════════

@contextlib.contextmanager
def _hermetic_cwd():
    """Run inside an empty temp dir so relative ``data/...`` paths are harmless.

    On Windows a SQLite file still open in there cannot be deleted at the end
    (WinError 32); a left-over temp dir must not fail the run.
    """
    previous = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="webot-evals-", ignore_cleanup_errors=True) as tmp:
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(previous)


@contextlib.contextmanager
def _quiet_logging():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


@contextlib.contextmanager
def _patched_time(clock: FakeClock):
    """Swap the module-level ``time`` of the production modules for ``clock``."""
    with contextlib.ExitStack() as stack:
        for name in _TIME_MODULES:
            try:
                module = importlib.import_module(name)
            except Exception:  # pragma: no cover - optional module missing
                continue
            if hasattr(module, "time"):
                stack.enter_context(mock.patch.object(module, "time", clock))
        yield


# ═════════════════════════════════════════════════════════════════════════
# Routing replay
# ═════════════════════════════════════════════════════════════════════════

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)
_IMAGE_TYPES = {3, 47}
_RATE_RE = re.compile(r"rate=([0-9.]+)/min")
_SCORE_RE = re.compile(r"score[= ]([0-9.]+)")


@dataclasses.dataclass
class _Event:
    kind: str       # "context" | "prime" | "trigger"
    clock: float    # fake "now" while the router handles it
    order: tuple    # stable chronological sort key
    msg: dict
    reply: str      # what the stub model answers during this event


def _find_name(case: dict, sender_id: str) -> str:
    for item in [*case["context"], case["trigger"], *case["next_messages"]]:
        if item["sender_id"] == sender_id:
            return item["sender_name"]
    return "群友"


def _make_msg(case: dict, item: dict, message_id: str, timestamp: int,
              image_path: str, *, trigger: bool = False,
              at_override: bool | None = None) -> dict:
    """Build the standardized message dict the backend would hand the router."""
    chat = case["chat"]
    bot_name = case["bot_name"]
    content = item["content"]
    msg_type = int(item.get("msg_type", 1))
    if at_override is not None:
        is_at = at_override
    elif trigger:
        is_at = bool(item["is_at_mentioned"])
    else:  # context rows carry no flag: derive it like the WCDB backend does
        is_at = bool(chat["is_group"] and bot_name and f"@{bot_name}" in content)
    return {
        "message_id": message_id,
        "chat_id": chat["chat_id"],
        "group_name": chat["group_name"],
        "chat_name": chat["group_name"],
        "sender_id": item["sender_id"],
        "sender_name": item["sender_name"],
        "content": content,
        "msg_type": msg_type,
        "server_id": "",
        "media_base_dir": "",
        "media_hint": "",
        "media_kind": "",
        "media_path": image_path if msg_type in _IMAGE_TYPES else "",
        "timestamp": int(timestamp),
        "is_at_mentioned": is_at,
        "is_group": bool(chat["is_group"]),
        "is_system_join": False,
        "new_member_id": "",
        "is_reply_to_bot": bool(item.get("is_reply_to_bot", False)) if trigger else False,
        "quoted_sender_id": "",
        "quoted_sender_name": str(item.get("quoted_sender_name", "")) if trigger else "",
        "quoted_content": str(item.get("quoted_content", "")) if trigger else "",
        "quoted_server_id": "",
    }


def _plan_events(case: dict, image_path: str) -> list[_Event]:
    now = int(case["now"])
    cid = case["id"]
    events: list[_Event] = []

    for i, item in enumerate(case["context"]):
        ts = now + int(round(item["offset_sec"]))
        # The stub answers every engagement, so history that the router would
        # have picked up through an always-on route leaves the same sticky /
        # follow-up state a real reply would.
        events.append(_Event(
            "context", float(ts), (float(ts), 0, i),
            _make_msg(case, item, f"{cid}:ctx{i}", ts, image_path), STUB_REPLY,
        ))

    # Prime the real follow-up / sticky trackers the way production does: an
    # @ message is handled and the bot visibly replies, which registers them.
    state = case.get("bot_state")
    if state:
        last = state.get("last_bot_reply")
        to_sender = last["to_sender_id"] if last else None
        order = [s for s in state.get("sticky_for") or [] if s != to_sender]
        if to_sender:
            order.append(to_sender)
        bot_ts = now + int(round(last["offset_sec"])) if last else now - 5
        text = last["text"] if last else ""
        bare = not last  # no reply known: a bare @ registers sticky by itself
        for j, sender_id in enumerate(order):
            content = f"@{case['bot_name']}" if bare else f"@{case['bot_name']} 在吗"
            if not case["chat"]["is_group"]:
                content = "在吗"
            item = {
                "sender_id": sender_id,
                "sender_name": _find_name(case, sender_id),
                "content": content,
                "msg_type": 1,
            }
            msg = _make_msg(
                case, item, f"{cid}:prime{j}", bot_ts - 2, image_path,
                at_override=bool(case["chat"]["is_group"]),
            )
            events.append(_Event(
                "prime", float(bot_ts), (float(bot_ts), 1, j), msg, text,
            ))

    trig = case["trigger"]
    t_ts = now + int(round(trig["offset_sec"]))
    events.append(_Event(
        "trigger", float(now), (float(now), 2, 0),
        _make_msg(case, trig, f"{cid}:trigger", t_ts, image_path, trigger=True),
        STUB_REPLY,
    ))
    events.sort(key=lambda e: e.order)
    return events


def _build_config(case: dict, workdir: Path,
                  profile: dict[str, Any] | None = None) -> BotConfig:
    names = {f.name for f in dataclasses.fields(BotConfig)}
    kwargs: dict[str, Any] = {
        "bot_display_name": case["bot_name"],
        "db_path": str(workdir / f"{case['id']}.todo.db"),
        "proactive_enabled": False,
    }
    kwargs.update(profile or {})
    # Observability sinks (if this checkout has them) must never write to the
    # repo's data/ directory during a replay.
    for key, value in (
        ("decision_trace_enabled", False),
        ("llm_call_log_enabled", False),
        ("decision_trace_path", str(workdir / "decision_trace.jsonl")),
        ("llm_call_log_path", str(workdir / "llm_calls.jsonl")),
    ):
        if key in names:
            kwargs[key] = value
    for key, value in (case.get("config_overrides") or {}).items():
        if key not in names:
            raise ValueError(f"{case['id']}: unknown config override {key!r}")
        kwargs[key] = value
    return BotConfig(**kwargs)


def _local_kind(reply: str) -> str:
    if "抽签结果" in reply:
        return "local:lots"
    if "塔罗三张" in reply:
        return "local:tarot"
    if "可用命令" in reply:
        return "local:help"
    if any(tok in reply for tok in ("待办", "未找到匹配", "请指定", "仅群管理员", "未在已删除")):
        return "local:todo"
    if any(tok in reply for tok in ("没找到刚才那张图", "没找到你引用的那张图", "看不了图")):
        return "local:image_fallback"
    return "local:other"


def _classify(msg: dict, reply: str | None, calls: list[dict]) -> tuple[str, str]:
    kinds = [c["kind"] for c in calls]
    if "decide" in kinds:
        call = calls[kinds.index("decide")]
        if call.get("call") == "proactive_v2":
            return "engaged_proactive", "proactive"
    if "chat" in kinds or "decide" in kinds:
        call = calls[kinds.index("chat" if "chat" in kinds else "decide")]
        if msg.get("_private_triggered"):
            route = "private"
        elif msg.get("_kw_triggered"):
            route = "keyword"
        elif msg.get("_sticky_triggered"):
            route = "sticky"
        elif msg.get("_followup_listen_triggered"):
            route = "followup"
        elif msg.get("_name_call_triggered"):
            route = "name_call"
        elif call.get("ambient"):
            route = "full_feed"
        elif msg.get("is_reply_to_bot"):
            route = "quote_bot"
        else:
            route = "at_mention"
        return "engaged_chat", route
    if "proactive_chat" in kinds:
        return "engaged_proactive", "proactive"
    if "describe_image" in kinds:
        is_image = int(msg.get("msg_type", 1)) in _IMAGE_TYPES
        return "engaged_vision", "image_followup" if is_image else "image_question"
    if reply:
        return "handled_locally", _local_kind(reply)
    return "none", "none"


def replay_case(case: dict, *, workdir: Path, image_path: str, seed: int,
                profile: dict[str, Any] | None = None,
                summarizer: "RecordingSummarizer | None" = None,
                keep_reply: bool = False) -> dict:
    """Replay one case through a fresh router and return its result record.

    ``summarizer`` lets the live runner put a real model behind the trigger;
    ``keep_reply`` adds the router's final reply text to the record (it is not
    part of the committed baselines).
    """
    cfg = _build_config(case, workdir, profile)
    clock = FakeClock(case["now"])
    if summarizer is None:
        summarizer = RecordingSummarizer()
    nicks = OfflineNicknames()
    dice = FakeDice(1.0)
    gate_log: list[tuple] = []

    original_should_speak = ProactiveGate.should_speak
    original_todo_handle = TodoHandler.handle
    local_log: list[bool] = []  # one entry per todo-handler call: matched?

    def recording_should_speak(self, msg, **kwargs):  # records the real gate's verdict
        result = original_should_speak(self, msg, **kwargs)
        gate_log.append(result)
        return result

    def recording_todo_handle(self, *args, **kwargs):
        result = original_todo_handle(self, *args, **kwargs)
        local_log.append(result is not None)
        return result

    conn = initialize_db(":memory:")
    store = MessageStore(conn)
    events = _plan_events(case, image_path)
    trigger_record: dict[str, Any] = {}
    warnings: list[str] = []

    reset_modes()
    fun_module.reset_lots_cache()
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(_patched_time(clock))
            stack.enter_context(mock.patch.object(gate_module, "random", dice))
            stack.enter_context(mock.patch.object(
                fun_module, "random", random.Random(f"{seed}:{case['id']}"),
            ))
            stack.enter_context(mock.patch.object(
                fun_module, "_LOTS_PATH", workdir / "no-such-lots.json",
            ))
            stack.enter_context(mock.patch.object(
                MemoryConsolidator, "check_and_consolidate_async",
                lambda self, chat_id: False,
            ))
            stack.enter_context(mock.patch.object(
                ProactiveGate, "should_speak", recording_should_speak,
            ))
            stack.enter_context(mock.patch.object(
                TodoHandler, "handle", recording_todo_handle,
            ))

            router = MessageRouter(
                store=store,
                detector=TriggerDetector(cfg.trigger_keywords, cfg.bot_display_name),
                summarizer=summarizer,
                admin_handler=AdminCommandHandler(nicks),
                nickname_service=nicks,
                config=cfg,
                web_search_client=OfflineWebSearch.from_config(cfg),
            )

            for event in events:
                clock.set(event.clock)
                is_trigger = event.kind == "trigger"
                # The probability-gated ambient path is only exercised for the
                # trigger; a dice-dependent reply to history would make the
                # state the trigger sees depend on chance.
                cfg.proactive_enabled = is_trigger
                dice.value = 0.0 if is_trigger else 1.0
                summarizer.begin_event(event.kind, event.reply)
                calls_before = len(summarizer.calls)
                gate_before = len(gate_log)
                local_before = len(local_log)
                try:
                    reply = router.handle(event.msg)
                    error = ""
                except Exception as exc:  # noqa: BLE001 - report, don't crash
                    reply, error = None, f"{type(exc).__name__}: {exc}"
                if is_trigger:
                    trigger_record = {
                        "msg": event.msg,
                        "reply": reply,
                        "calls": summarizer.calls[calls_before:],
                        "gate": gate_log[gate_before:],
                        "local_matched": any(local_log[local_before:]),
                        "error": error,
                    }
                elif error:
                    warnings.append(f"{event.kind}: {error}")
    finally:
        reset_modes()
        fun_module.reset_lots_cache()
        conn.close()

    expect = case["expect"]  # None for an unlabeled (not yet reviewed) case
    trig = case["trigger"]
    if not case["chat"]["is_group"]:
        addressing = "private"
    elif trig["is_at_mentioned"] or trig["is_reply_to_bot"]:
        addressing = "explicit"
    else:
        addressing = "implicit"
    base = {
        "id": case["id"],
        "category": case["category"],
        "addressing": addressing,
        "expect": None if expect is None else {
            "action": expect["action"],
            "strength": expect["strength"],
            "addressee": expect.get("addressee"),
        },
    }
    if case.get("source") == "handwritten":
        base["text"] = case["trigger"]["content"].replace("\u2005", " ")[:48]

    if trigger_record.get("error"):
        base.update({
            "outcome": "error", "route": "error", "model_calls": [],
            "reply_head": "", "gate": None, "gate_reason": "",
            "local_empty": False,
            "verdict": "error", "error": trigger_record["error"][:160],
        })
        return base

    msg = trigger_record["msg"]
    reply = trigger_record["reply"]
    calls = trigger_record["calls"]
    outcome, route = _classify(msg, reply, calls)

    gate_info = None
    gate_reason = ""
    for verdict in trigger_record["gate"]:
        _should, mode, reason = verdict
        match = _RATE_RE.search(str(reason))
        if outcome == "engaged_proactive":
            call = next(c for c in calls if c["kind"] in ("proactive_chat", "decide"))
            gate_info = {
                "mode": call.get("mode"),
                "reply_probability": call.get("reply_probability"),
                "rate_per_min": float(match.group(1)) if match else None,
            }
            hit = _SCORE_RE.search(str(reason))
            if hit:
                gate_info["score"] = float(hit.group(1))
        else:
            gate_reason = str(reason)[:60]

    action = expect["action"] if expect else "unlabeled"
    if action == "unlabeled":
        verdict_name = "unlabeled"
    elif action == "either":
        verdict_name = "neutral"
    elif outcome == "engaged_proactive":
        verdict_name = "proactive_only"
    elif action == "speak":
        verdict_name = "ok" if outcome in metrics.ADDRESSED_OK else "miss"
    else:
        verdict_name = "ok" if outcome == "none" else "false_engagement"

    # A local command handler matched but its reply was empty: the user gets
    # nothing although a route exists (distinct from "no route at all").
    local_empty = bool(outcome == "none" and trigger_record["local_matched"])
    if local_empty:
        route = "local:empty_reply"
        gate_reason = ""
    head = re.sub(r"\s+", " ", reply or "").strip()[:24]
    # The model should see the message it is answering as the newest context
    # line.  None when no model context was built (local handlers, no route).
    context_call = next(
        (c for c in calls if c["kind"] in ("chat", "proactive_chat", "decide")), None,
    )
    context_has_trigger = (
        None if context_call is None
        else context_call.get("context_last") == msg.get("content")
    )
    decide_call = next((c for c in calls if c["kind"] == "decide"), None)
    if decide_call is not None:
        # Only present for the structured pipeline, so legacy reports stay
        # byte-identical.
        base["route_level"] = decide_call["level"]
        base["route_signals"] = decide_call["signals"]
        base["strategy"] = decide_call["strategy"]
    elif profile and profile.get("pipeline_v2", "off") != "off":
        base["route_level"] = "local" if outcome == "handled_locally" else "none"
    if profile and profile.get("proactive_gate_mode") == "score" and outcome != "engaged_proactive":
        score_hit = _SCORE_RE.search(gate_reason)
        if score_hit:
            base["gate_score"] = float(score_hit.group(1))
    base.update({
        "context_has_trigger": context_has_trigger,
        "outcome": outcome,
        "route": route,
        "model_calls": [c["kind"] for c in calls],
        "reply_head": head if outcome == "handled_locally" else "",
        "gate": gate_info,
        "gate_reason": gate_reason,
        "local_empty": local_empty,
        "verdict": verdict_name,
    })
    if keep_reply:
        base["reply"] = reply or ""
    if warnings:
        base["warnings"] = warnings
    return base


PIPELINE_CHOICES = ("off", "single", "two_step")
GATE_CHOICES = ("dice", "score")


def make_profile(pipeline: str = "off", gate: str = "dice") -> dict[str, Any]:
    """Config overrides that select the code path under test.

    The default profile ("off", "dice") is the production default and adds
    nothing, so legacy reports stay byte-identical to earlier baselines.
    """
    if pipeline not in PIPELINE_CHOICES:
        raise ValueError(f"unknown pipeline {pipeline!r}")
    if gate not in GATE_CHOICES:
        raise ValueError(f"unknown gate {gate!r}")
    profile: dict[str, Any] = {}
    if pipeline != "off":
        profile["pipeline_v2"] = pipeline
    if gate != "dice":
        profile["proactive_gate_mode"] = gate
    return profile


def run_routing(cases: Sequence[dict], *, seed: int,
                profile: dict[str, Any] | None = None) -> dict:
    with _hermetic_cwd() as workdir, _quiet_logging():
        image_path = workdir / "img.png"
        image_path.write_bytes(_PNG_1X1)
        results = [
            replay_case(case, workdir=workdir, image_path=str(image_path),
                        seed=seed, profile=profile)
            for case in cases
        ]
    return {"summary": metrics.routing_summary(results), "cases": results}


# ═════════════════════════════════════════════════════════════════════════
# Envelope replay (structured pipeline)
# ═════════════════════════════════════════════════════════════════════════

def run_envelopes(rows: Sequence[dict]) -> dict:
    """Run recorded / hand-made model answers through the envelope parser."""
    results = []
    for row in rows:
        try:
            envelope = parse_envelope(
                row["raw"], max_chars=int(row.get("max_chars") or CLEAN_DEFAULT_MAX_CHARS),
                names=tuple(row.get("names") or ()),
            )
            got = (
                {"action": "reply", "text": envelope.text} if envelope.is_reply
                else {"action": "silent"}
            )
        except EnvelopeError as error:
            got = {"action": "fail", "code": error.code}
        results.append({
            "id": row["id"], "kind": row["kind"],
            "expect": row["expect"], "got": got,
            "verdict": metrics.classify_envelope_row(row["expect"], got),
            **({"known_gap": True} if row.get("known_gap") else {}),
        })
    return {"summary": metrics.envelope_summary(results), "rows": results}


# ═════════════════════════════════════════════════════════════════════════
# Cleaning replay
# ═════════════════════════════════════════════════════════════════════════

def _clean(cleaner, raw: str, max_chars: int, names: tuple) -> str:
    """Call the production cleaner; older checkouts take no ``names`` argument
    (lets the same harness measure a before/after across commits)."""
    try:
        return cleaner(raw, max_chars, names=names)
    except TypeError:
        return cleaner(raw, max_chars)


def run_cleaning(rows: Sequence[dict]) -> dict:
    cleaner = AbstractSummarizer._clean_chat_reply
    is_meta = AbstractSummarizer._is_silence_meta_reply
    out_rows = []
    for row in rows:
        max_chars = int(row.get("max_chars") or CLEAN_DEFAULT_MAX_CHARS)
        raw = row["raw"]
        names = tuple(row.get("names") or ())
        out = _clean(cleaner, raw, max_chars, names)
        full = _clean(cleaner, raw, 10_000, names)
        verdict = metrics.classify_clean_row(
            expect_silent=bool(row["expect_silent"]),
            expect_text=row.get("expect_text"),
            out=out, full=full, max_chars=max_chars,
        )
        out_rows.append({
            "id": row["id"],
            "kind": row["kind"],
            "expect_silent": bool(row["expect_silent"]),
            "max_chars": max_chars,
            "raw": raw,
            "expect_text": row.get("expect_text"),
            "out": out,
            # Mirrors AbstractSummarizer.chat(): blank + meta => "intentional
            # silence" (no fallback message); blank without it => fallback.
            "silence_flag": bool(raw and not out and is_meta(raw)),
            **verdict,
        })
    return {"summary": metrics.clean_summary(out_rows), "rows": out_rows}


# ═════════════════════════════════════════════════════════════════════════
# Gate streams
# ═════════════════════════════════════════════════════════════════════════

# Message mix for the score-mode streams (the dice gate ignores content).  The
# shares are invented; they only make the rates comparable between profiles.
# (share, text, worth speaking about?)
_STREAM_MIX = (
    (0.12, "怎么把列表去重又保持顺序啊", True),     # information-seeking question
    (0.12, "晚上有人打游戏吗", False),              # social question
    (0.06, "这个AI也太蠢了吧", False),              # talk about bots: not a call
    (0.30, "哈哈", False),                          # short reaction
    (0.40, "今天天气不错我们出去走走吧", False),      # statement
)
_STREAM_SENDERS = tuple(f"u_{i}" for i in range(6))


def _stream_message(rng: random.Random, with_content: bool) -> tuple[dict, bool]:
    """One synthetic message and whether it is worth the bot speaking up."""
    msg: dict[str, Any] = {"chat_id": GATE_CHAT_ID}
    if not with_content:
        return msg, False
    roll = rng.random()
    _, text, worthy = _STREAM_MIX[-1]
    acc = 0.0
    for share, candidate, candidate_worthy in _STREAM_MIX:
        acc += share
        if roll < acc:
            text, worthy = candidate, candidate_worthy
            break
    msg.update({
        "sender_id": rng.choice(_STREAM_SENDERS), "content": text, "msg_type": 1,
    })
    return msg, worthy


def _simulate_stream(rate_per_min: float, *, model: str, seed: int,
                     config: BotConfig, minutes: int,
                     with_content: bool = False) -> dict:
    """One synthetic stream through the real gate with a fake clock.

    ``model`` is the hypothetical model behind every gate pass:
    ``silent`` always returns blank (the router then calls ``record_eval`` and
    ``record_silence``, which feeds the gate's exponential backoff) and
    ``speak`` always replies (``record_speech`` resets the backoff).
    """
    start = 1_760_000_000.0
    horizon = minutes * 60.0
    arrivals_rng = random.Random(f"{seed}:arrivals:{rate_per_min}")
    arrivals: list[float] = []
    t = 0.0
    while True:
        t += arrivals_rng.expovariate(rate_per_min / 60.0)
        if t >= horizon:
            break
        arrivals.append(t)

    clock = FakeClock(start)
    dice = SeededDice(f"{seed}:dice:{rate_per_min}")
    content_rng = random.Random(f"{seed}:content:{rate_per_min}")
    reset_modes()
    mode_order = [m.name for m in get_modes(config)]
    msg_modes: Counter = Counter()
    pass_modes: Counter = Counter()
    passes = 0
    worthy_messages = 0
    worthy_passes = 0
    with contextlib.ExitStack() as stack:
        for name in ("src.proactive.gate", "src.proactive.rate_tracker"):
            stack.enter_context(
                mock.patch.object(importlib.import_module(name), "time", clock)
            )
        stack.enter_context(mock.patch.object(gate_module, "random", dice))
        gate = ProactiveGate(config)
        mirror = RateTracker(config.proactive_rate_window_sec)  # same window
        for offset in arrivals:
            clock.set(start + offset)
            mirror.record(GATE_CHAT_ID)
            msg_modes[lookup_mode(mirror.rate(GATE_CHAT_ID), config).name] += 1
            message, worthy = _stream_message(content_rng, with_content)
            worthy_messages += worthy
            should, mode, _reason = gate.should_speak(message)
            if not should or mode is None:
                continue
            passes += 1
            worthy_passes += worthy
            pass_modes[mode.name] += 1
            if model == "silent":
                gate.record_eval(GATE_CHAT_ID)
                gate.record_silence(GATE_CHAT_ID)
            else:
                gate.record_speech(GATE_CHAT_ID)
    reset_modes()
    hours = minutes / 60.0
    result = {
        "messages": len(arrivals),
        "passes": passes,
        "passes_per_hour": round(passes / hours, 2),
        "calls_per_100_messages": round(100.0 * passes / len(arrivals), 2) if arrivals else 0.0,
        "pass_mode_counts": {k: pass_modes.get(k, 0) for k in mode_order},
        "_msg_modes": {k: msg_modes.get(k, 0) for k in mode_order},
        "_mode_order": mode_order,
    }
    if with_content:
        result["worthy_messages"] = worthy_messages
        result["worthy_passes"] = worthy_passes
        result["worthy_share_of_passes"] = (
            round(worthy_passes / passes, 4) if passes else None
        )
    return result


def run_gate_streams(*, seed: int, rates: Iterable[float] = GATE_RATES,
                     minutes: int = GATE_MINUTES, mode: str = "dice") -> dict:
    scored = mode == "score"
    config = (
        BotConfig(proactive_enabled=True, proactive_gate_mode="score")
        if scored else BotConfig(proactive_enabled=True)
    )
    dice_config = BotConfig(proactive_enabled=True)
    rows = []
    mode_order: list[str] = []
    with _quiet_logging():
        for rate in rates:
            silent = _simulate_stream(rate, model="silent", seed=seed,
                                      config=config, minutes=minutes,
                                      with_content=scored)
            speak = _simulate_stream(rate, model="speak", seed=seed,
                                     config=config, minutes=minutes,
                                     with_content=scored)
            mode_order = silent.pop("_mode_order")
            speak.pop("_mode_order")
            msg_modes = silent.pop("_msg_modes")
            speak.pop("_msg_modes")
            row = {
                "rate_per_min": rate,
                "messages": silent["messages"],
                "mode_share_of_messages": metrics.distribution(msg_modes, mode_order),
                "always_silent_model": {
                    **silent,
                    "wasted_model_calls": silent["passes"],
                    "wasted_calls_per_hour": silent["passes_per_hour"],
                },
                "always_speaking_model": speak,
            }
            if scored:
                # The legacy dice gate on the very same messages.
                reference = _simulate_stream(rate, model="silent", seed=seed,
                                             config=dice_config, minutes=minutes,
                                             with_content=True)
                reference.pop("_mode_order")
                reference.pop("_msg_modes")
                row["dice_reference"] = {
                    k: reference[k] for k in (
                        "passes_per_hour", "worthy_passes", "worthy_share_of_passes",
                    )
                }
                row["always_silent_model"]["worthy_recall"] = (
                    round(silent["worthy_passes"] / silent["worthy_messages"], 4)
                    if silent["worthy_messages"] else None
                )
                row["dice_reference"]["worthy_recall"] = (
                    round(reference["worthy_passes"] / reference["worthy_messages"], 4)
                    if reference["worthy_messages"] else None
                )
            rows.append(row)
    gate_config: dict[str, Any] = {
        "proactive_enabled": True,
        "rate_window_sec": config.proactive_rate_window_sec,
        "thresholds_msgs_per_min": {
            "QUIET": config.proactive_rate_quiet,
            "CASUAL": config.proactive_rate_casual,
            "LIVELY": config.proactive_rate_lively,
            "BURST": config.proactive_rate_burst,
        },
        "simulated_minutes": minutes,
        "arrivals": "poisson, seeded",
    }
    if scored:
        gate_config.update({
            "gate_mode": "score",
            "hourly_budget": config.proactive_hourly_budget,
            "score_threshold": config.proactive_score_threshold,
            "content_mix": [[share, text, worthy] for share, text, worthy in _STREAM_MIX],
        })
    return {"config": gate_config, "mode_order": mode_order, "rates": rows}


# ═════════════════════════════════════════════════════════════════════════
# Report assembly
# ═════════════════════════════════════════════════════════════════════════

def _sha256(path: Path) -> str:
    """Hash of a text input with line endings normalised: a Windows checkout
    (core.autocrlf) has CRLF and must report the same inputs as macOS."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_cases(paths: Sequence[Path]) -> list[dict]:
    """Load, normalize and validate cases; ``category == "bad"`` rows are skipped.

    Exporter output (null ``is_reply_to_bot``, no ``config_overrides``,
    ``expect: null`` before labeling) is accepted; see ``evals.normalize_case``.
    """
    return _load_cases(paths)[0]


def _load_cases(paths: Sequence[Path]) -> tuple[list[dict], int]:
    cases: list[dict] = []
    skipped_bad = 0
    seen: set[str] = set()
    for path in paths:
        for raw in load_jsonl(path):
            case = normalize_case(raw)
            problems = validate_case(case, allow_unlabeled=True)
            if problems:
                raise ValueError(
                    f"{path.name}: case {case.get('id', '?')!r} invalid: "
                    + "; ".join(problems[:4])
                )
            if case["id"] in seen:
                raise ValueError(f"{path.name}: duplicate case id {case['id']!r}")
            seen.add(case["id"])
            if case["category"] == "bad":  # labeler marked it unusable
                skipped_bad += 1
                continue
            cases.append(case)
    return cases, skipped_bad


def build_report(*, seed: int = DEFAULT_SEED,
                 case_paths: Sequence[Path] = (DEFAULT_CASES,),
                 raw_path: Path | None = DEFAULT_RAW_OUTPUTS,
                 pipeline: str = "off", gate: str = "dice",
                 envelope_path: Path | None = DEFAULT_ENVELOPES) -> dict:
    """Run all three replays and return the (deterministic) report dict."""
    profile = make_profile(pipeline, gate)
    case_paths = [Path(p) for p in case_paths]
    cases, skipped_bad = _load_cases(case_paths)
    default_cfg = BotConfig()
    report: dict[str, Any] = {
        "schema": BASELINE_SCHEMA,
        "harness": {"version": HARNESS_VERSION, "seed": seed, "case_schema": SCHEMA},
        "inputs": {
            "scenarios": [
                {"file": p.name, "sha256": _sha256(p)} for p in case_paths
            ],
            "n_cases": len(cases),
            "n_skipped_bad": skipped_bad,
        },
        "assumptions": {
            "router": "src.router.MessageRouter, production BotConfig defaults",
            "proactive": "disabled for context/priming; enabled for the trigger "
                         "with the probability roll forced open",
            "sticky_ttl_sec": default_cfg.sticky_mention_ttl_sec,
            "followup_ttl_sec": default_cfg.followup_listen_ttl_sec,
            "followup_max_chars": default_cfg.followup_listen_max_message_chars,
            "image_followup_ttl_sec": default_cfg.image_followup_ttl_sec,
            "stale_at_max_age_sec": 300,
            "trigger_keywords": list(default_cfg.trigger_keywords),
            "stub_model_reply": STUB_REPLY,
            "media_files": "available (tiny PNG)",
        },
        "routing": run_routing(cases, seed=seed, profile=profile),
        "not_measured_offline": list(NOT_MEASURED),
    }
    if profile:
        report["profile"] = {"pipeline": pipeline, "gate": gate}
    if raw_path is not None and Path(raw_path).exists():
        rows = load_jsonl(raw_path)
        report["inputs"]["raw_outputs"] = {
            "file": Path(raw_path).name, "sha256": _sha256(Path(raw_path)),
            "n_rows": len(rows),
        }
        report["cleaning"] = run_cleaning(rows)
    if profile and envelope_path is not None and Path(envelope_path).exists():
        envelope_rows = load_jsonl(envelope_path)
        report["inputs"]["envelopes"] = {
            "file": Path(envelope_path).name, "sha256": _sha256(Path(envelope_path)),
            "n_rows": len(envelope_rows),
        }
        report["envelopes"] = run_envelopes(envelope_rows)
    report["gate"] = run_gate_streams(seed=seed, mode=gate)
    return report


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


# ═════════════════════════════════════════════════════════════════════════
# Rendering
# ═════════════════════════════════════════════════════════════════════════

def _pct(value: dict) -> str:
    return metrics.fmt_ratio(value)


def _case_line(case: dict) -> str:
    expect = case["expect"]
    label = f"{expect['action']}/{expect['strength']}" if expect else "unlabeled"
    text = case.get("text", "")
    gate = f" [{case['gate_reason']}]" if case.get("gate_reason") else ""
    return (
        f"`{case['id']}` {case['category']} "
        f"({label}) -> {case['outcome']}"
        f"/{case['route']}" + (f" - {text}" if text else "") + gate
    )


def render_markdown(report: dict, title: str = "Offline replay baseline") -> str:
    routing = report["routing"]["summary"]
    cases = report["routing"]["cases"]
    lines: list[str] = []
    add = lines.append

    add(f"# {title}")
    add("")
    add("Generated by `python -m evals.run_offline` - deterministic, no network, "
        "no LLM. Scenarios are hand-written synthetic chats, so the numbers are "
        "a **regression baseline**, not an estimate of production accuracy.")
    add("")
    add(f"- seed: `{report['harness']['seed']}`, harness v{report['harness']['version']}, "
        f"{report['inputs']['n_cases']} scenarios")
    for entry in report["inputs"]["scenarios"]:
        add(f"- scenarios file: `{entry['file']}` (sha256 `{entry['sha256'][:12]}`)")
    if "raw_outputs" in report["inputs"]:
        raw = report["inputs"]["raw_outputs"]
        add(f"- raw outputs file: `{raw['file']}` ({raw['n_rows']} rows, "
            f"sha256 `{raw['sha256'][:12]}`)")
    add("")

    # headline ---------------------------------------------------------
    add("## Headline numbers")
    add("")
    rec = routing["addressing_recall"]
    fe = routing["false_engagement"]
    ce = routing["cannot_engage"]
    add("| metric | value |")
    add("|---|---|")
    add(f"| addressing recall, expect speak/must | {_pct(rec['speak_must'])} |")
    for kind, row in routing.get("by_addressing", {}).items():
        add(f"| ... {kind} addressing only | {_pct(row['speak_must'])} |")
    add(f"| addressing recall, expect speak (must+should) | {_pct(rec['speak_any'])} |")
    add(f"| false engagement, expect silent - always-on routes | "
        f"{_pct(fe['silent_any']['deterministic'])} |")
    add(f"| ... incl. proactive path open (upper bound, fires with p<=0.7) | "
        f"{_pct(fe['silent_any']['incl_proactive'])} |")
    add(f"| false engagement, expect silent/must - always-on routes | "
        f"{_pct(fe['silent_must']['deterministic'])} |")
    add(f"| speak cases with no route at all (implicit addressing) | "
        f"{_pct(ce['speak_any'])} |")
    add(f"| ... of which must-speak | {_pct(ce['speak_must'])} |")
    add(f"| handled locally outside command categories (possible hijack) | "
        f"{routing['unexpected_local']['n']} |")
    add(f"| local handler matched but sent an empty reply | "
        f"{routing['local_empty_reply']['n']} |")
    ctx = routing["context"]
    add(f"| model context ends with the triggering message (busy groups) | "
        f"{_pct(ctx['latest_in_context'])} |")
    unl = routing.get("unlabeled", {})
    if unl.get("n"):
        add(f"| unlabeled cases: router engaged (always-on routes) | "
            f"{_pct(unl['engaged_always_on'])} |")
        add(f"| unlabeled cases: engaged incl. proactive path open | "
            f"{_pct(unl['engaged_incl_proactive'])} |")
    pipe = routing.get("pipeline")
    if pipe:
        must = pipe["must_speak"]
        add(f"| structured pipeline: strategy / gate | {report['profile']['pipeline']} / "
            f"{report['profile']['gate']} |")
        add(f"| ... must-speak ends: reply guaranteed / model decides / vision / "
            f"command / no route | {must['guaranteed']} / {must['model_decides']} / "
            f"{must['vision']} / {must['local']} / {must['none']} (of {must['n']}) |")
        add(f"| ... expect silent but reply guaranteed (hard false positive) | "
            f"{_pct(pipe['silent_guaranteed'])} |")
        add(f"| ... expect silent, model asked and must decline | "
            f"{_pct(pipe['silent_model_asked'])} |")
        scores = pipe["gate_scores"]
        if scores["auc"] is not None:
            add(f"| ... willingness score separation, ambient cases (AUC) | "
                f"{scores['auc']:.2f} ({scores['speak_n']} speak vs {scores['silent_n']} silent) |")
    if "envelopes" in report:
        es = report["envelopes"]["summary"]
        add(f"| envelope parsing: exact | {_pct(es['exact'])} |")
        add(f"| ... leak rate (text where none is due) | {_pct(es['leak_rate'])} |")
        add(f"| ... false-kill rate (reply due, nothing sent) | {_pct(es['false_kill_rate'])} |")
    if "cleaning" in report:
        cs = report["cleaning"]["summary"]
        add(f"| cleaning: leak rate (meta text that gets sent) | {_pct(cs['leak_rate'])} |")
        add(f"| cleaning: false-kill rate (legit reply blanked) | {_pct(cs['false_kill_rate'])} |")
        add(f"| cleaning: cuts inside a clause | {cs['mid_sentence_truncations']} "
            f"of {cs['truncated_rows']} cut |")
        add(f"| cleaning: exact-match rate | {_pct(cs['exact_match_rate'])} |")
    add("")

    # routing ----------------------------------------------------------
    add("## 1. Routing replay")
    add("")
    add("Outcome = what the current router did with the trigger message. "
        "`engaged_*` means the (stub) model was called, not that a real model "
        "would have replied.")
    add("")
    add("| outcome | cases |")
    add("|---|---|")
    for key, value in routing["outcomes"].items():
        add(f"| {key} | {value} |")
    add("")
    add("### expect vs outcome")
    add("")
    outcomes = list(routing["outcomes"])
    add("| expect.action | " + " | ".join(outcomes) + " |")
    add("|---|" + "---|" * len(outcomes))
    for action, row in routing["expect_vs_outcome"].items():
        add(f"| {action} | " + " | ".join(str(row[o]) for o in outcomes) + " |")
    add("")
    add("### By addressing style")
    add("")
    add("`explicit` = @bot or quote of the bot, `implicit` = neither (group "
        "chat), `private` = one-to-one chat.")
    add("")
    add("| addressing | n | speak/must ok | speak ok | silent engaged (always-on) "
        "| silent engaged (incl. proactive) |")
    add("|---|---|---|---|---|---|")
    for kind, row in routing.get("by_addressing", {}).items():
        add(f"| {kind} | {row['n']} | {_pct(row['speak_must'])} | "
            f"{_pct(row['speak_any'])} | "
            f"{_pct(row['silent_engaged_deterministic'])} | "
            f"{_pct(row['silent_engaged_incl_proactive'])} |")
    add("")
    add("### By category")
    add("")
    add("| category | n | must-speak ok | silent engaged | outcomes |")
    add("|---|---|---|---|---|")
    for cat, row in routing["by_category"].items():
        outs = ", ".join(f"{k} {v}" for k, v in row["outcomes"].items())
        add(f"| {cat} | {row['n']} | {_pct(row['speak_must'])} | "
            f"{_pct(row['silent_engaged'])} | {outs} |")
    add("")
    add("### Engagement routes")
    add("")
    add("| route | total | wanted (expect speak) | unwanted (expect silent) "
        "| neutral | unlabeled |")
    add("|---|---|---|---|---|---|")
    for route, row in routing["by_route"].items():
        add(f"| {route} | {row['total']} | {row['wanted']} | "
            f"{row['unwanted']} | {row['neutral']} | {row['unlabeled']} |")
    add("")

    by_id = {c["id"]: c for c in cases}

    def listing(title: str, ids: list[str]) -> None:
        add(f"### {title} ({len(ids)})")
        add("")
        if not ids:
            add("_none_")
        for case_id in ids:
            add(f"- {_case_line(by_id[case_id])}")
        add("")

    listing("Must-speak cases the router did not answer", routing["missed_must_ids"])
    listing("Cannot engage: expect speak, router has no route", routing["cannot_engage"]["ids"])
    listing("Expect silent but engaged (any path)", routing["false_engagement_ids"])
    listing("Handled locally outside command categories", routing["unexpected_local"]["ids"])

    # structured pipeline ---------------------------------------------
    if pipe:
        add("## 1b. Structured pipeline")
        add("")
        add("The replay stub always answers, so these numbers say how much "
            "freedom the model is given at each addressing level, not what a "
            "real model does with it.")
        add("")
        add("| level | model-asking cases |")
        add("|---|---|")
        for level, count in pipe["levels"].items():
            add(f"| {level} | {count} |")
        add("")
        add("Expected addressee x level reached (`none` = no route, `local` = a command):")
        add("")
        levels = ["explicit", "strong", "weak", "ambient", "local", "none"]
        add("| expected addressee | " + " | ".join(levels) + " |")
        add("|---|" + "---|" * len(levels))
        for addressee, row in pipe["addressee"].items():
            add(f"| {addressee} | " + " | ".join(str(row.get(lv, 0)) for lv in levels) + " |")
        add("")
        if pipe["silent_guaranteed_ids"]:
            add("Expect silent but the reply is guaranteed:")
            add("")
            by_id = {c["id"]: c for c in cases}
            for case_id in pipe["silent_guaranteed_ids"]:
                add(f"- {_case_line(by_id[case_id])}")
            add("")

    # cleaning ---------------------------------------------------------
    if "cleaning" in report:
        cs = report["cleaning"]["summary"]
        rows = report["cleaning"]["rows"]
        add("## 2. Reply cleaning")
        add("")
        add(f"`AbstractSummarizer._clean_chat_reply` over {cs['n_rows']} recorded "
            f"raw outputs ({cs['n_should_be_blank']} should end blank, "
            f"{cs['n_legit']} are legitimate replies).")
        add("")
        add("| metric | value |")
        add("|---|---|")
        add(f"| leak rate (should be blank, was sent) | {_pct(cs['leak_rate'])} |")
        add(f"| false-kill rate (legit reply blanked) | {_pct(cs['false_kill_rate'])} |")
        add(f"| mangled / hard-cut legit replies | {_pct(cs['mangled_rate'])} |")
        add(f"| blank but not flagged as intentional silence "
            f"(router sends a canned fallback to a direct @) | "
            f"{cs['blank_without_silence_flag']['n']} |")
        add(f"| rows cut by the length limit | {cs['truncated_rows']} |")
        add(f"| cuts inside a clause (not at a sentence end or clause break) | {cs['mid_sentence_truncations']} |")
        add(f"| exact-match rate | {_pct(cs['exact_match_rate'])} |")
        add("")
        add("| kind | n | ok | leak | false_kill | truncated | mangled |")
        add("|---|---|---|---|---|---|---|")
        for kind, row in cs["by_kind"].items():
            add(f"| {kind} | {row['n']} | {row.get('ok', 0)} | {row.get('leak', 0)} | "
                f"{row.get('false_kill', 0)} | {row.get('truncated', 0)} | "
                f"{row.get('mangled', 0)} |")
        add("")
        bad = [r for r in rows if r["status"] != "ok"]
        add(f"### Rows that do not match their expectation ({len(bad)})")
        add("")
        add("| id | kind | status | raw | got | expected |")
        add("|---|---|---|---|---|---|")
        for r in bad:
            expected = "(blank)" if r["expect_silent"] else r["expect_text"]
            got = r["out"] if r["out"] else "(blank)"
            add(f"| {r['id']} | {r['kind']} | {r['status']} | "
                f"`{_md(r['raw'])}` | `{_md(got)}` | `{_md(expected)}` |")
        add("")

    # gate -------------------------------------------------------------
    gate = report["gate"]
    cfg = gate["config"]
    thr = cfg["thresholds_msgs_per_min"]
    add("## 3. Proactive gate streams")
    add("")
    add(f"Real `ProactiveGate`, default thresholds (QUIET >= {thr['QUIET']}, "
        f"CASUAL >= {thr['CASUAL']}, LIVELY >= {thr['LIVELY']}, BURST >= "
        f"{thr['BURST']} msgs/min over a {cfg['rate_window_sec']} s window), "
        f"{cfg['simulated_minutes']} simulated minutes of seeded Poisson "
        "arrivals per rate, fake clock, one group.")
    add("")
    add("`always silent` = every gate pass reaches a model that returns blank "
        "(router calls `record_eval` + `record_silence`, so the gate's "
        "exponential backoff applies); passes are wasted model calls. "
        "`always speaks` = every pass produces a reply (no backoff).")
    add("")
    order = gate["mode_order"]
    add("| msgs/min | messages | silent: wasted calls/h | silent: pass modes | "
        "speaks: bot msgs/h | speaks: pass modes |")
    add("|---|---|---|---|---|---|")
    for row in gate["rates"]:
        s = row["always_silent_model"]
        k = row["always_speaking_model"]
        add(f"| {row['rate_per_min']:g} | {row['messages']} | "
            f"{s['wasted_calls_per_hour']:g} | {_modes(s['pass_mode_counts'], order)} | "
            f"{k['passes_per_hour']:g} | {_modes(k['pass_mode_counts'], order)} |")
    add("")
    if cfg.get("gate_mode") == "score":
        add(f"Score mode (threshold {cfg['score_threshold']}, hourly budget "
            f"{cfg['hourly_budget']}) on a synthetic message mix; the dice gate sees the "
            "very same messages. A message is *worth speaking about* if it asks "
            "something a bot can answer "
            f"({100 * sum(s for s, _t, w in cfg['content_mix'] if w):.0f}% of the mix). "
            "Always-silent model:")
        add("")
        add("| msgs/min | score: calls/h | score: worthy share of calls | "
            "score: worthy messages reached | dice: calls/h | dice: worthy share | "
            "dice: worthy reached |")
        add("|---|---|---|---|---|---|---|")
        for row in gate["rates"]:
            s = row["always_silent_model"]
            d = row["dice_reference"]

            def share(value):
                return "n/a" if value is None else f"{100 * value:.0f}%"

            add(f"| {row['rate_per_min']:g} | {s['passes_per_hour']:g} | "
                f"{share(s['worthy_share_of_passes'])} | {share(s['worthy_recall'])} | "
                f"{d['passes_per_hour']:g} | {share(d['worthy_share_of_passes'])} | "
                f"{share(d['worthy_recall'])} |")
        add("")
    add("Share of messages evaluated in each mode (rate window classification):")
    add("")
    add("| msgs/min | " + " | ".join(order) + " |")
    add("|---|" + "---|" * len(order))
    for row in gate["rates"]:
        shares = row["mode_share_of_messages"]
        add(f"| {row['rate_per_min']:g} | "
            + " | ".join(f"{100 * shares[m]:.0f}%" for m in order) + " |")
    add("")

    # envelopes --------------------------------------------------------
    if "envelopes" in report:
        es = report["envelopes"]["summary"]
        env_rows = report["envelopes"]["rows"]
        add("## 4. Envelope replay")
        add("")
        add(f"`parse_envelope` over {es['n_rows']} model answers: well-formed, sloppy "
            "(code fences, prose, python dicts), schema drift, the legacy leak strings "
            "both inside a reply and written instead of JSON, truncated and broken JSON. "
            "Written by hand next to the rules they test, so this is a robustness "
            "check, not a measure of what a model does. Rows marked known gap document "
            "limits of the text rules on purpose.")
        add("")
        add("| metric | value |")
        add("|---|---|")
        add(f"| exact | {_pct(es['exact'])} |")
        add(f"| leak (text sent where none is due) | {_pct(es['leak_rate'])} |")
        add(f"| false kill (reply due, nothing sent) | {_pct(es['false_kill_rate'])} |")
        add(f"| mangled text | {es['mangled']} |")
        add(f"| rejected for another reason than expected | {es['wrong_failure']} |")
        add("")
        add("| kind | rows | ok |")
        add("|---|---|---|")
        for kind, row in es["by_kind"].items():
            add(f"| {kind} | {row['n']} | {row['ok']} |")
        add("")
        if es["mismatches"]:
            add("Rows that do not match their expectation:")
            add("")
            for r in env_rows:
                if r["verdict"] != "ok":
                    flag = " (known gap)" if r.get("known_gap") else ""
                    add(f"- `{r['id']}` {r['kind']}: {r['verdict']}{flag} - "
                        f"expected `{_md(json.dumps(r['expect'], ensure_ascii=False))}`, got "
                        f"`{_md(json.dumps(r['got'], ensure_ascii=False))}`")
            add("")

    # not measured -----------------------------------------------------
    add("## Not measured offline")
    add("")
    for item in report["not_measured_offline"]:
        add(f"- {item}")
    add("")
    add("Reproduce: `python -m evals.run_offline --seed "
        f"{report['harness']['seed']}` (writes this file and the JSON next to it).")
    add("")
    return "\n".join(lines)


def _modes(counts: dict, order: Sequence[str]) -> str:
    parts = [f"{m} {counts[m]}" for m in order if counts.get(m)]
    return ", ".join(parts) if parts else "-"


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("`", "'").replace("\n", " ")


def render_console(report: dict, *, runtime_sec: float | None = None) -> str:
    routing = report["routing"]["summary"]
    rec = routing["addressing_recall"]
    fe = routing["false_engagement"]
    ce = routing["cannot_engage"]
    out: list[str] = []
    add = out.append
    add(f"== evals.run_offline  seed={report['harness']['seed']}  "
        f"cases={report['inputs']['n_cases']} ==")
    add("-- routing replay (current router) --")
    add(f"  addressing recall (speak/must)      {_pct(rec['speak_must'])}")
    add(f"  addressing recall (speak any)       {_pct(rec['speak_any'])}")
    add(f"  false engagement (silent, always-on) {_pct(fe['silent_any']['deterministic'])}"
        f"   (+{fe['silent_any']['proactive_open_only']} proactive-open)")
    add(f"  cannot engage (speak, no route)     {_pct(ce['speak_any'])}"
        f"   must-speak: {_pct(ce['speak_must'])}")
    add(f"  handled locally outside commands    {routing['unexpected_local']['n']}")
    unl = routing.get("unlabeled", {})
    if unl.get("n"):
        add(f"  unlabeled: engaged always-on        {_pct(unl['engaged_always_on'])}"
            f"   incl. proactive: {_pct(unl['engaged_incl_proactive'])}")
    add("  outcomes: " + ", ".join(f"{k}={v}" for k, v in routing["outcomes"].items() if v))
    for kind, row in routing.get("by_addressing", {}).items():
        add(f"  recall speak/must, {kind:<8} addressing: {_pct(row['speak_must'])}")
    add("  category                    n  must-ok         silent-engaged")
    for cat, row in routing["by_category"].items():
        add(f"  {cat:<24} {row['n']:>3}  {_pct(row['speak_must']):<14}  "
            f"{_pct(row['silent_engaged'])}")
    pipe = routing.get("pipeline")
    if pipe:
        must = pipe["must_speak"]
        add(f"-- structured pipeline ({report['profile']['pipeline']}, "
            f"gate {report['profile']['gate']}) --")
        add(f"  levels: " + ", ".join(f"{k}={v}" for k, v in pipe["levels"].items()))
        add(f"  must-speak ends: guaranteed={must['guaranteed']} "
            f"model-decides={must['model_decides']} vision={must['vision']} "
            f"command={must['local']} no-route={must['none']} (of {must['n']})")
        add(f"  expect silent, reply guaranteed:  {_pct(pipe['silent_guaranteed'])}")
        add(f"  expect silent, model must decline: {_pct(pipe['silent_model_asked'])}")
        if pipe["gate_scores"]["auc"] is not None:
            add(f"  willingness score AUC (ambient):   {pipe['gate_scores']['auc']:.2f}")
    if "envelopes" in report:
        es = report["envelopes"]["summary"]
        add("-- envelope parsing --")
        add(f"  exact {_pct(es['exact'])}   leak {_pct(es['leak_rate'])}   "
            f"false-kill {_pct(es['false_kill_rate'])}   known gaps {len(es['known_gaps'])}")
    if "cleaning" in report:
        cs = report["cleaning"]["summary"]
        add("-- reply cleaning --")
        add(f"  leak rate        {_pct(cs['leak_rate'])}")
        add(f"  false-kill rate  {_pct(cs['false_kill_rate'])}")
        add(f"  mid-sentence truncations  {cs['mid_sentence_truncations']}"
            f" (of {cs['truncated_rows']} cut)")
        add(f"  exact-match rate {_pct(cs['exact_match_rate'])}")
    gate = report["gate"]
    add("-- proactive gate streams (120 simulated min) --")
    add("  msgs/min  msgs  silent:wasted/h  speak:msgs/h")
    for row in gate["rates"]:
        add(f"  {row['rate_per_min']:>7g}  {row['messages']:>4}  "
            f"{row['always_silent_model']['wasted_calls_per_hour']:>14g}  "
            f"{row['always_speaking_model']['passes_per_hour']:>12g}")
    det = report.get("determinism")
    if det:
        add(f"-- determinism: {'identical' if det['identical'] else 'DIFFERENT'} "
            f"across {det['runs']} runs --")
    if runtime_sec is not None:
        add(f"-- runtime: {runtime_sec:.2f}s --")
    return "\n".join(out)


# ═════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_offline",
        description="Offline deterministic replay of scenarios, reply cleaning "
                    "and proactive-gate streams (no network, no LLM).",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="directory for <tag>.json / <tag>.md "
                             "(default: evals/baseline)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cases", nargs="+", default=[str(DEFAULT_CASES)],
                        help="replay-case/v1 JSONL file(s)")
    parser.add_argument("--raw-outputs", default=str(DEFAULT_RAW_OUTPUTS),
                        help="raw model outputs JSONL for the cleaning replay "
                             "('' to skip)")
    parser.add_argument("--envelopes", default=str(DEFAULT_ENVELOPES),
                        help="structured-output answers JSONL for the envelope "
                             "replay ('' to skip; only run with --pipeline/--gate)")
    parser.add_argument("--pipeline", choices=PIPELINE_CHOICES, default="off",
                        help="router code path: off (legacy), single, two_step")
    parser.add_argument("--gate", choices=GATE_CHOICES, default="dice",
                        help="proactive gate: dice (legacy) or score")
    parser.add_argument("--tag", default=DEFAULT_TAG,
                        help="output file stem (default: current, git-ignored; "
                             "commit a baseline under its own tag, e.g. phase2_after)")
    parser.add_argument("--no-write", action="store_true",
                        help="print the summary only, write no files")
    args = parser.parse_args(argv)

    started = _real_time.perf_counter()
    case_paths = [Path(p).resolve() for p in args.cases]
    raw_path = Path(args.raw_outputs).resolve() if args.raw_outputs else None

    envelope_path = Path(args.envelopes).resolve() if args.envelopes else None
    first = build_report(seed=args.seed, case_paths=case_paths, raw_path=raw_path,
                         pipeline=args.pipeline, gate=args.gate,
                         envelope_path=envelope_path)
    second = build_report(seed=args.seed, case_paths=case_paths, raw_path=raw_path,
                          pipeline=args.pipeline, gate=args.gate,
                          envelope_path=envelope_path)
    text_first = canonical_json(first)
    identical = text_first == canonical_json(second)
    first["determinism"] = {
        "runs": 2,
        "identical": identical,
        "sha256": hashlib.sha256(text_first.encode("utf-8")).hexdigest(),
    }
    runtime = _real_time.perf_counter() - started

    if not args.no_write:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{args.tag}.json").write_text(
            canonical_json(first), encoding="utf-8",
        )
        (out_dir / f"{args.tag}.md").write_text(
            render_markdown(first, title=f"Offline replay baseline: {args.tag}"),
            encoding="utf-8",
        )
    print(render_console(first, runtime_sec=runtime))
    if not args.no_write:
        print(f"wrote {Path(args.out) / (args.tag + '.json')} and .md")

    errors = first["routing"]["summary"]["outcomes"].get("error", 0)
    return 0 if identical and not errors else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
