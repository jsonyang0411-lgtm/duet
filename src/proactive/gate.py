"""ProactiveGate — decides whether the bot should evaluate speaking.

Combines message rate tracking, mode lookup, per-mode evaluation
intervals, and probabilistic gating.  Pure heuristic — zero AI cost
for the 99% of messages that get filtered out before reaching the AI.
"""

import logging
import random
import re
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

from . import willingness
from .modes import lookup_mode, ProactiveMode
from .rate_tracker import RateTracker
from ._sync import locked

if TYPE_CHECKING:
    from ..config import BotConfig
    from ..db.store import MessageStore

logger = logging.getLogger(__name__)


class ProactiveGate:
    """Multi-level gate for proactive chat participation.

    On every message (without @mention), the gate:
    1. Records the message for rate tracking
    2. Computes current message rate
    3. Looks up the corresponding mode
    4. Checks if the evaluation interval has elapsed
    5. Rolls the dice against the mode's reply probability

    Only when ALL gates pass does it return a mode for the handler
    to call the AI with.  No daily limit, no hard cooldown — the
    per-mode interval + probability provides natural pacing.

    With ``proactive_gate_mode == "score"`` the dice (step 5) is replaced by a
    deterministic willingness score (see :mod:`.willingness`) and a per-group
    hourly reply budget: the model is asked only at a moment that looks worth
    speaking in, and a low-scoring message does not use up the evaluation
    interval, so a better one a few seconds later still gets its chance.
    """

    def __init__(self, config: "BotConfig"):
        self._config = config
        self._tracker = RateTracker(config.proactive_rate_window_sec)
        # Per-group: last time we evaluated (to enforce eval_interval)
        self._last_eval: dict[str, float] = {}
        # Per-group: consecutive AI silence count (for exponential backoff)
        self._consecutive_silence: dict[str, int] = {}
        self._call_count: int = 0
        # Score mode only (stays unused in dice mode)
        self._score_mode = getattr(config, "proactive_gate_mode", "dice") == "score"
        self._budget = 0
        self._threshold = 0.35
        if self._score_mode:
            self._budget = int(getattr(config, "proactive_hourly_budget", 6) or 0)
            self._threshold = float(getattr(config, "proactive_score_threshold", 0.35))
        # The callbacks run on a thread pool: every public method holds the
        # state lock, and the score mode keeps its finer lock inside it.
        self._lock = threading.RLock()
        self._score_lock = threading.Lock() if self._score_mode else None
        # chat -> times the bot spoke here on its own initiative
        self._spoke_at: dict[str, deque] = {}
        # chat -> [asked_at, asker, answered]: the latest question in the chat
        self._question: dict[str, list] = {}
        # chat -> (sender, compact text) of the last few messages
        self._recent_texts: dict[str, deque] = {}

    @locked
    def should_speak(self, msg: dict,
                     scored: bool | None = None) -> tuple[bool, ProactiveMode | None, str]:
        """Record a message and decide whether to trigger AI evaluation.

        Args:
            msg: Standardized message dict (must have 'chat_id').
            scored: ``False`` makes this message use the dice even when the
                gate is configured for the score mode (a group that has not
                been switched over); ``None`` follows the configuration.

        Returns:
            (should_evaluate, mode, reason) — if should_evaluate is False,
            mode is None and reason explains which gate blocked.
        """
        chat_id = msg.get("chat_id", "")
        if not chat_id:
            return False, None, "no chat_id"
        now = time.time()

        # ── Periodic _last_eval cleanup ─────────────────────────────
        self._call_count += 1
        if self._call_count % 500 == 0 and len(self._last_eval) > 100:
            cutoff = time.time() - 3600  # 1 hour
            stale = [k for k, v in self._last_eval.items() if v < cutoff]
            for k in stale:
                del self._last_eval[k]
                self._consecutive_silence.pop(k, None)

        # ── Always record for rate tracking ───────────────────────
        self._tracker.record(chat_id)

        # The score mode reads every message, even while the gate is closed, so
        # its picture of the conversation (open questions, echoes) is complete
        # the moment it opens.
        if self._score_mode and scored is not False:
            with self._score_lock:
                observed = self._observe(chat_id, msg, now)
        else:
            observed = None

        # ── Gate 1: master switch ─────────────────────────────────
        if not self._config.proactive_enabled:
            return False, None, "disabled"

        # ── Gate 2: message rate ──────────────────────────────────
        rate = self._tracker.rate(chat_id)
        mode = lookup_mode(rate, self._config)

        if mode.name == "SLEEP":
            logger.debug(
                "Proactive: rate=%.1f/min → SLEEP (chat=%s)",
                rate, chat_id[:20],
            )
            return False, None, f"rate {rate:.1f}/min → SLEEP"

        # ── Gate 3: evaluation interval (with silence backoff) ───
        last = self._last_eval.get(chat_id, 0)
        elapsed = now - last
        # Exponential backoff: each consecutive silence doubles the
        # effective interval, capped at 16x.  During prolonged crises
        # this prevents burning API tokens every 2-8 minutes on calls
        # that all return empty.
        consecutive = self._consecutive_silence.get(chat_id, 0)
        backoff = min(2 ** consecutive, 16)
        effective_interval = mode.eval_interval_sec * backoff
        if elapsed < effective_interval:
            logger.debug(
                "Proactive: eval interval not met (%.0fs < %ds, mode=%s, "
                "backoff=%dx, consecutive_silence=%d)",
                elapsed, effective_interval, mode.name,
                backoff, consecutive,
            )
            return False, None, (
                f"eval interval ({elapsed:.0f}s < {effective_interval}s, "
                f"backoff={backoff}x)"
            )

        # ── Gate 4 (score mode): willingness score + hourly budget ─
        if observed is not None:
            with self._score_lock:
                return self._score_gate(chat_id, mode, rate, now, observed, last)

        # ── Gate 4: reply probability ─────────────────────────────
        roll = random.random()
        if roll > mode.reply_probability:
            logger.debug(
                "Proactive: probability miss (%.2f > %.2f, mode=%s)",
                roll, mode.reply_probability, mode.name,
            )
            # Update last_eval so we don't hammer the probability gate
            self._last_eval[chat_id] = now
            return False, None, f"probability miss ({roll:.2f} > {mode.reply_probability})"

        # ── All gates passed ──────────────────────────────────────
        self._last_eval[chat_id] = now
        logger.info(
            "Proactive: GATE PASSED mode=%s rate=%.1f/min "
            "interval=%ds prob=%.0f%% (chat=%s)",
            mode.name, rate, mode.eval_interval_sec,
            mode.reply_probability * 100, chat_id[:20],
        )
        return True, mode, f"mode={mode.name} rate={rate:.1f}/min"

    @locked
    def record_eval(self, chat_id: str) -> None:
        """Manually update last evaluation time (e.g., after AI returned blank)."""
        self._last_eval[chat_id] = time.time()

    @locked
    def record_silence(self, chat_id: str) -> None:
        """Increment consecutive silence counter for exponential backoff.

        Call this when the AI returns an empty string from proactive_chat().
        Each consecutive silence doubles the effective evaluation interval,
        capped at 16x, so the gate backs off during prolonged crises.
        """
        self._consecutive_silence[chat_id] = (
            self._consecutive_silence.get(chat_id, 0) + 1
        )
        logger.debug(
            "Proactive: silence recorded for chat=%s (consecutive=%d)",
            chat_id[:20], self._consecutive_silence[chat_id],
        )

    @locked
    def record_speech(self, chat_id: str) -> None:
        """Reset consecutive silence counter when the AI successfully speaks."""
        if self._score_mode:
            with self._score_lock:
                self._spoke_at.setdefault(chat_id, deque(maxlen=200)).append(time.time())
        if self._consecutive_silence.get(chat_id, 0) > 0:
            logger.debug(
                "Proactive: speech recorded for chat=%s — resetting "
                "silence counter from %d",
                chat_id[:20], self._consecutive_silence[chat_id],
            )
            self._consecutive_silence[chat_id] = 0

    @locked
    def get_consecutive_silence(self, chat_id: str) -> int:
        """Return the current consecutive silence count for a group."""
        return self._consecutive_silence.get(chat_id, 0)

    # ── score mode ──────────────────────────────────────────────────

    _SENDER_PREFIX_RE = re.compile(r"^\s*wxid_[a-zA-Z0-9_]+:\s*")

    def _observe(self, chat_id: str, msg: dict, now: float) -> dict:
        """Read one message, update the per-chat state, return its features."""
        text = self._SENDER_PREFIX_RE.sub("", str(msg.get("content", "") or ""), count=1).strip()
        sender = str(msg.get("sender_id", "") or "")
        flat = willingness.compact(text)
        try:
            msg_type = int(msg.get("msg_type", 1) or 1)
        except (TypeError, ValueError):
            msg_type = 1
        is_question = willingness.looks_like_question(text)

        question = self._question.get(chat_id)
        open_question = bool(
            question and not question[2] and question[1] != sender
            and 8.0 <= now - question[0] <= 90.0
        )
        recent = self._recent_texts.setdefault(chat_id, deque(maxlen=6))
        repeat = bool(
            flat and len(flat) <= 20
            and any(other != sender and seen == flat for other, seen in recent)
        )

        if is_question:
            self._question[chat_id] = [now, sender, False]
        elif question and not question[2] and sender != question[1]:
            question[2] = True            # somebody else has spoken since
        if flat:
            recent.append((sender, flat))

        return {
            "text": text,
            "is_question": is_question,
            "info_seeking": willingness.looks_info_seeking(text),
            "open_question": open_question,
            "too_short": willingness.is_too_short(text),
            "media_only": willingness.is_media_only(text, msg_type),
            "repeat": repeat,
            "substantial": len(flat) >= willingness.SUBSTANTIAL_CHARS,
        }

    def _score_gate(self, chat_id: str, mode: ProactiveMode, rate: float,
                    now: float, observed: dict,
                    last_eval: float = 0) -> tuple[bool, ProactiveMode | None, str]:
        # The interval was checked before the lock was taken: if another
        # message of this chat passed in between, this one must not pass too.
        if self._last_eval.get(chat_id, 0) != last_eval:
            return False, None, "eval interval (taken by a concurrent message)"
        if observed["media_only"]:
            return False, None, "media only"

        spoke = self._spoke_at.get(chat_id)
        if self._budget and spoke:
            used = sum(1 for stamp in spoke if now - stamp < 3600)
            if used >= self._budget:
                return False, None, f"hourly budget {used}/{self._budget}"

        features = willingness.Features(
            base=mode.reply_probability,
            is_question=observed["is_question"],
            info_seeking=observed["info_seeking"],
            open_question=observed["open_question"],
            too_short=observed["too_short"],
            media_only=False,
            repeat=observed["repeat"],
            substantial=observed["substantial"],
            seconds_since_bot_spoke=(now - spoke[-1]) if spoke else None,
        )
        value, contributions = willingness.score(features)
        why = willingness.explain(contributions)
        if value < self._threshold:
            logger.debug(
                "Proactive: score %.2f < %.2f (mode=%s %s chat=%s)",
                value, self._threshold, mode.name, why, chat_id[:20],
            )
            return False, None, f"score {value:.2f} < {self._threshold:.2f} ({why})"

        self._last_eval[chat_id] = now
        logger.info(
            "Proactive: GATE PASSED mode=%s rate=%.1f/min score=%.2f (%s) (chat=%s)",
            mode.name, rate, value, why, chat_id[:20],
        )
        return True, mode, f"mode={mode.name} rate={rate:.1f}/min score={value:.2f} {why}".strip()
