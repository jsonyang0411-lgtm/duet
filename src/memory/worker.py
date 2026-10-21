"""Background job that turns chat windows into validated facts and episodes.

The reply path never waits for it: the router only calls ``note_message`` (a
dict update).  A daemon thread wakes up once a minute, builds the search index
the first time, and runs a window for every noted chat that is due.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

from .extraction import (
    EXTRACTION_SCHEMA, PROMPT_KNOWN_FACTS, apply_extraction, build_prompt, make_window,
    parse_payload, validate_extraction,
)
from .store import MemoryStore

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SEC = 600
MAX_BACKOFF_SEC = 3 * 3600
# An answer that cannot be used (or a crash while storing it) for the same
# window this many times in a row: skip that window.  Transport errors are
# never a reason to skip: the window waits, with a longer pause each time.
MAX_FAILS_PER_WINDOW = 3
MAX_WINDOWS_PER_TICK = 3
MIN_WINDOW_TEXT_CHARS = 40
OUTPUT_TOKENS = 3000


@dataclass
class RunResult:
    chat_id: str
    status: str                 # ok / skipped_short / failed / dropped
    messages: int = 0
    detail: dict | None = None
    usage: dict | None = None


class MemoryWorker:
    def __init__(self, mstore: MemoryStore, summarizer, *, window_messages: int = 40,
                 idle_minutes: int = 20, min_window: int = 8, max_window: int = 120,
                 first_window: int = 120, bot_name: str = "机器人",
                 bot_names: tuple[str, ...] = (), interval_sec: float = 60.0,
                 clock=time.time, vectors=None) -> None:
        self._mstore = mstore
        self._summarizer = summarizer
        self.window_messages = max(5, int(window_messages))
        self.idle_sec = max(60, int(idle_minutes) * 60)
        self.min_window = max(1, int(min_window))
        self.max_window = max(self.window_messages, int(max_window))
        self.first_window = max(1, int(first_window))
        self.bot_name = bot_name
        self.bot_names = tuple(n for n in (bot_name, *bot_names) if n)
        self.interval_sec = interval_sec
        self._clock = clock
        self._dirty: set[str] = set()
        self._dirty_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # MEMORY_EMBEDDINGS: new facts get their vectors after each tick.
        self._vectors = vectors

    @property
    def usable(self) -> bool:
        return callable(getattr(self._summarizer, "complete_json", None))

    # ── called from the reply path ───────────────────────────────────────

    def note_message(self, chat_id: str) -> None:
        with self._dirty_lock:
            self._dirty.add(chat_id)

    # ── scheduling ───────────────────────────────────────────────────────

    def tick(self, now: float | None = None) -> list[RunResult]:
        """Run every due window of every noted chat, one after the other."""
        if not self._tick_lock.acquire(blocking=False):
            return []
        try:
            now = self._clock() if now is None else now
            with self._dirty_lock:
                chats = sorted(self._dirty)
            results: list[RunResult] = []
            for chat_id in chats:
                try:
                    for _ in range(MAX_WINDOWS_PER_TICK):
                        result = self._run_if_due(chat_id, now)
                        if result is None:
                            break
                        results.append(result)
                        if result.status == "failed":
                            break
                except sqlite3.ProgrammingError:
                    raise                                  # the database is gone: let the loop end
                except Exception:                          # noqa: BLE001 - one chat must not stop the others
                    logger.exception("Memory tick failed for %s", chat_id[:24])
            return results
        finally:
            self._tick_lock.release()

    def _run_if_due(self, chat_id: str, now: float) -> RunResult | None:
        progress = self._mstore.get_progress(chat_id)
        if progress is None:
            progress = self._mstore.init_progress(chat_id, keep_last=self.first_window)
        if float(progress.get("retry_after") or 0) > now:
            return None                                    # still dirty: look again next tick
        # Forget the chat BEFORE counting: a message that arrives meanwhile
        # notes it again, so the idle rule cannot lose a chat to this race.
        with self._dirty_lock:
            self._dirty.discard(chat_id)
        count, last_ts = self._mstore.count_pending(chat_id, int(progress["last_row_id"]))
        if count == 0:
            return None
        due = count >= self.window_messages or (
            count >= self.min_window and now - last_ts >= self.idle_sec
        )
        if not due:
            self.note_message(chat_id)
            return None
        result = self.run_window(chat_id, now)
        self.note_message(chat_id)
        return result

    def run_window(self, chat_id: str, now: float | None = None) -> RunResult:
        """Process the next window of one chat.  Never raises."""
        now = self._clock() if now is None else now
        try:
            progress = self._mstore.get_progress(chat_id) or self._mstore.init_progress(
                chat_id, keep_last=self.first_window,
            )
            rows = self._mstore.pending_messages(
                chat_id, int(progress["last_row_id"]), limit=self.max_window,
            )
            if not rows:
                return RunResult(chat_id, "skipped_short")
            window = make_window(
                chat_id, rows,
                self._mstore.bot_replies_between(
                    chat_id, int(rows[0]["timestamp"]), int(rows[-1]["timestamp"]) + 60,
                ),
                bot_names=self.bot_names,
            )
            if window.text_chars < MIN_WINDOW_TEXT_CHARS:
                self._mstore.set_progress(chat_id, last_row_id=window.last_row_id, ok=True, now=now)
                return RunResult(chat_id, "skipped_short", len(rows))
            if not self.usable:
                return RunResult(chat_id, "failed", len(rows),
                                 {"error": "summarizer has no complete_json"})

            known = self._mstore.list_facts(chat_id, limit=PROMPT_KNOWN_FACTS)
            system, user = build_prompt(
                window, known, today=_dt.date.fromtimestamp(now), bot_name=self.bot_name,
            )
            try:
                output = self._summarizer.complete_json(
                    system, user, schema=EXTRACTION_SCHEMA, max_tokens=OUTPUT_TOKENS,
                    call_name="memory_extract",
                )
            except Exception as exc:                      # noqa: BLE001 - model/transport failure
                return self._failed(chat_id, window, progress, now, exc, deterministic=False)
            try:
                payload = parse_payload(getattr(output, "text", ""))
                if payload is None:
                    raise ValueError("answer is not an extraction object")
                extraction = validate_extraction(
                    payload, window, known={int(f["id"]): f for f in known}, now=now,
                    bot_names=self.bot_names,
                )
                # Facts, summary and the read mark commit together or not at all.
                with self._mstore.transaction():
                    counts = apply_extraction(self._mstore, chat_id, extraction, now=now)
                    self._mstore.set_progress(
                        chat_id, last_row_id=window.last_row_id, ok=True, now=now,
                    )
            except Exception as exc:                      # noqa: BLE001 - unusable answer / storage crash
                return self._failed(chat_id, window, progress, now, exc, deterministic=True)

            usage = getattr(output, "usage", None)
            logger.info(
                "Memory window chat=%s msgs=%d facts+%d/~%d retired=%d episode=%s "
                "rejected=%d%s tokens=%s",
                chat_id[:24], len(rows), counts["facts_created"], counts["facts_supported"],
                counts["retired"], "yes" if counts["episode_id"] else "no",
                counts["rejected"],
                (" " + ",".join(sorted({r for r, _ in extraction.rejected}))) if extraction.rejected else "",
                usage,
            )
            counts["rejected_reasons"] = [r for r, _ in extraction.rejected]
            return RunResult(chat_id, "ok", len(rows), counts, usage)
        except sqlite3.ProgrammingError:
            raise
        except Exception:                                  # noqa: BLE001 - background job: never kill the thread
            logger.exception("Memory window crashed for %s", chat_id[:24])
            return RunResult(chat_id, "failed", 0, {"error": "internal"})

    def _failed(self, chat_id: str, window, progress: dict, now: float, exc: Exception, *,
                deterministic: bool) -> RunResult:
        if deterministic:
            same = int(progress.get("fail_end_row_id") or 0) == window.last_row_id
            tries = (int(progress.get("parse_fail_count") or 0) + 1) if same else 1
            if tries >= MAX_FAILS_PER_WINDOW:
                logger.error(
                    "Memory window for %s could not be used %d times (%s: %s); skipping %d messages",
                    chat_id[:24], tries, type(exc).__name__, str(exc)[:120], len(window.rows),
                )
                self._mstore.set_progress(chat_id, last_row_id=window.last_row_id, ok=True, now=now)
                return RunResult(chat_id, "dropped", len(window.rows), {"error": type(exc).__name__})
        pause = min(RETRY_BACKOFF_SEC * (2 ** min(int(progress.get("fail_count") or 0), 4)),
                    MAX_BACKOFF_SEC)
        logger.warning(
            "Memory extraction failed for %s (%s: %s); retrying in %ds",
            chat_id[:24], type(exc).__name__, str(exc)[:120], pause,
        )
        self._mstore.set_progress(
            chat_id, ok=False, now=now, retry_after=now + pause,
            window_end=window.last_row_id, deterministic=deterministic,
        )
        return RunResult(chat_id, "failed", len(window.rows), {"error": type(exc).__name__})

    # ── thread ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="memory-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        try:
            if self._mstore.enable_search_index():
                while (not self._stop.is_set() and self._mstore.index_enabled
                       and not self._mstore.sync_index()):
                    self._stop.wait(0.05)
        except sqlite3.ProgrammingError:
            return                                         # the database was closed under us
        except Exception:                                  # noqa: BLE001
            logger.exception("Memory search index setup failed; LIKE search stays in use")
        while not self._stop.wait(self.interval_sec):
            try:
                self._mstore.sync_index()
                self.tick()
                if self._vectors is not None:
                    self._vectors.index_pending()
            except sqlite3.ProgrammingError:
                logger.info("Memory worker stops: the database is closed")
                return
            except Exception:                              # noqa: BLE001
                logger.exception("Memory worker tick failed")
