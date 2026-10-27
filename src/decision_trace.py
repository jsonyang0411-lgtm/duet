"""Decision trace (observe only).

Record WHY the bot answered or stayed silent for each message (which route step
ended it, with which reason code), without changing any behaviour.

Design rules
------------
* Best effort: a failure to write a trace must never affect message handling.
  Every public method swallows its own exceptions.
* Thread safe: the router runs in a worker pool; the "current" trace is kept
  per thread, so helpers deeper in the call stack can attach events without
  extra parameters.
* Local files only (JSON Lines, size-rotated).  Content is optional and
  truncated; a registered creator's message is never kept (``redact``).

File
----
``decision_trace.jsonl``  one record per handled message (kind="decision")
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .pii import mask_pii

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULT_DECISION_PATH = "data/decision_trace.jsonl"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUPS = 3

OUTCOME_REPLY = "reply"
OUTCOME_SILENT = "silent"      # the bot looked at the message and chose not to speak
OUTCOME_IGNORED = "ignored"    # filtered before any decision (self, duplicate, stale...)
OUTCOME_ERROR = "error"


def _truncate(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


class JsonlSink:
    """Append-only JSON Lines writer with simple size-based rotation."""

    def __init__(self, path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 backups: int = DEFAULT_BACKUPS):
        self._path = Path(path)
        self._max_bytes = max(1024, int(max_bytes))
        self._backups = max(0, int(backups))
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: dict) -> None:
        try:
            line = json.dumps(
                record, ensure_ascii=False, separators=(",", ":"), default=str,
            ) + "\n"
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed(len(line.encode("utf-8")))
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception:  # pragma: no cover - defensive
            logger.debug("trace write failed for %s", self._path, exc_info=True)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size + incoming <= self._max_bytes:
            return
        if self._backups <= 0:
            self._path.unlink(missing_ok=True)
            return
        oldest = self._path.with_name(f"{self._path.name}.{self._backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self._backups - 1, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{index}")
            if src.exists():
                src.replace(self._path.with_name(f"{self._path.name}.{index + 1}"))
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))


class NullTrace:
    """No-op stand-in so call sites never need ``if trace:``."""

    message_id = ""
    active = False

    def step(self, code: str, **info: Any) -> None:
        return None

    def note(self, **info: Any) -> None:
        return None

    def reason(self, code: str, **info: Any) -> None:
        return None

    def ignore(self, code: str, **info: Any) -> None:
        return None

    def redact(self, content: str) -> None:
        return None

    def finish_reply(self, reply: Optional[str]) -> None:
        return None

    def finish_error(self, exc: BaseException) -> None:
        return None


_NULL_TRACE = NullTrace()


class DecisionTrace(NullTrace):
    """Accumulates the steps taken for ONE incoming message."""

    active = True

    def __init__(self, tracer: "DecisionTracer", msg: dict):
        self._tracer = tracer
        self._t0 = time.monotonic()
        self._steps: list[dict] = []
        self._extra: dict = {}
        self._reason = ""
        self._ignored = False
        self._finished = False
        self.message_id = str(msg.get("message_id", "") or "")
        self._header = tracer._header(msg)

    def step(self, code: str, **info: Any) -> None:
        try:
            entry = {"code": str(code)}
            if info:
                entry.update(info)
            self._steps.append(entry)
        except Exception:  # pragma: no cover - defensive
            pass

    def note(self, **info: Any) -> None:
        self._extra.update(info)

    def reason(self, code: str, **info: Any) -> None:
        """Set the final reason (the last call wins) and record it as a step."""
        self._reason = str(code)
        self.step(code, **info)

    def ignore(self, code: str, **info: Any) -> None:
        """Final reason for a message dropped before any decision."""
        self._ignored = True
        self.reason(code, **info)

    def redact(self, content: str) -> None:
        """Keep ``content`` instead of the message's text (a creator's message: no numbers in logs)."""
        if "content" in self._header:
            self._header["content"] = str(content)

    def finish_reply(self, reply: Optional[str]) -> None:
        if reply:
            outcome = OUTCOME_REPLY
            reply_chars = len(reply)
        else:
            outcome = OUTCOME_IGNORED if self._ignored else OUTCOME_SILENT
            reply_chars = 0
        self._finish(outcome, reply_chars)

    def finish_error(self, exc: BaseException) -> None:
        self._reason = self._reason or f"exception:{type(exc).__name__}"
        self._extra["error"] = _truncate(repr(exc), 200)
        self._finish(OUTCOME_ERROR, 0)

    def _finish(self, outcome: str, reply_chars: int) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            record = dict(self._header)
            record.update({
                "v": SCHEMA_VERSION,
                "kind": "decision",
                "ts": round(self._tracer._clock(), 3),
                "outcome": outcome,
                "reason": self._reason or ("reply" if reply_chars else "no_handler"),
                "reply_chars": reply_chars,
                "elapsed_ms": round((time.monotonic() - self._t0) * 1000, 1),
                "steps": self._steps,
            })
            if self._extra:
                record["extra"] = self._extra
            self._tracer._write_decision(record)
        finally:
            self._tracer._clear_current(self)


class NullTracer:
    """Tracer used when tracing is disabled."""

    enabled = False

    def begin(self, msg: dict) -> NullTrace:
        return _NULL_TRACE

    def current(self) -> NullTrace:
        return _NULL_TRACE


class DecisionTracer(NullTracer):
    """Creates per-message traces and writes the decision records."""

    enabled = True

    def __init__(self, decision_sink: Optional[JsonlSink] = None, *,
                 content_chars: int = 120,
                 clock: Callable[[], float] = time.time):
        self._decision_sink = decision_sink
        self._content_chars = max(0, int(content_chars))
        self._clock = clock
        self._local = threading.local()

    # ── construction ────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Any, *,
                    clock: Callable[[], float] = time.time) -> NullTracer:
        """Build a tracer from a BotConfig-like object.

        Only a literal ``True`` enables the sink, so Mock/namespace configs used
        in tests silently get a no-op tracer.
        """
        if getattr(config, "decision_trace_enabled", False) is not True:
            return NullTracer()

        def _text(name: str, default: str) -> str:
            value = getattr(config, name, default)
            return value if isinstance(value, str) and value.strip() else default

        def _int(name: str, default: int) -> int:
            value = getattr(config, name, default)
            return value if isinstance(value, int) and not isinstance(value, bool) else default

        return cls(
            JsonlSink(_text("decision_trace_path", DEFAULT_DECISION_PATH)),
            content_chars=_int("decision_trace_content_chars", 120),
            clock=clock,
        )

    # ── per-message traces ──────────────────────────────────────────

    def begin(self, msg: dict) -> NullTrace:
        try:
            trace = DecisionTrace(self, msg)
        except Exception:  # pragma: no cover - defensive
            return _NULL_TRACE
        self._local.current = trace
        return trace

    def current(self) -> NullTrace:
        return getattr(self._local, "current", None) or _NULL_TRACE

    def _clear_current(self, trace: DecisionTrace) -> None:
        if getattr(self._local, "current", None) is trace:
            self._local.current = None

    def _header(self, msg: dict) -> dict:
        now = self._clock()
        try:
            msg_ts = int(msg.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            msg_ts = 0
        flags = {
            "at": bool(msg.get("is_at_mentioned")),
            "reply_to_bot": bool(msg.get("is_reply_to_bot")),
            "quoted": bool(msg.get("quoted_content")),
            "join": bool(msg.get("is_system_join")),
        }
        header = {
            "chat_id": str(msg.get("chat_id", "") or ""),
            "group": str(msg.get("group_name", "") or ""),
            "message_id": str(msg.get("message_id", "") or ""),
            "sender_id": str(msg.get("sender_id", "") or ""),
            "sender_name": str(msg.get("sender_name", "") or ""),
            "is_group": bool(msg.get("is_group", True)),
            "msg_type": msg.get("msg_type", 1),
            "msg_ts": msg_ts,
            "age_sec": int(now - msg_ts) if msg_ts else None,
            "flags": flags,
        }
        if self._content_chars:
            # 追踪文件会一直留着，手机号、地址这类个人信息先打码（B14）
            header["content"] = _truncate(mask_pii(msg.get("content", "")), self._content_chars)
        return header

    def _write_decision(self, record: dict) -> None:
        if self._decision_sink is not None:
            self._decision_sink.write(record)
