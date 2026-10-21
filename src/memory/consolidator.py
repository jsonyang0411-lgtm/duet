"""MemoryConsolidator — triggers and orchestrates group memory updates.

Core logic:
- After every N new messages (default 30), or
- After T hours since last consolidation (default 2h),
  consolidate the new messages into the group's memory diary.

Uses the DeepSeek Flash API for low-cost, low-latency consolidation.
"""

import concurrent.futures
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.store import MessageStore
    from ..summarize.base import AbstractSummarizer

logger = logging.getLogger(__name__)

# ── Tuning constants ─────────────────────────────────────────────────

# Trigger consolidation after this many NEW messages
CONSOLIDATE_MSG_THRESHOLD = 50
# Or after this many seconds since last consolidation (with new messages)
CONSOLIDATE_TIME_THRESHOLD_SEC = 1 * 3600  # 1 hour
# Maximum new messages to send per consolidation (limits prompt size)
MAX_NEW_MSGS_PER_CONSOLIDATION = 400
# How long the caller waits for one model call before giving up on it.  A
# local model (LOCAL_TIMEOUT_SEC, its summarizer's timeout_floor) gets as long
# as its own requests may take: 30 s is far too short for it, and every attempt
# would time out and back off, so the diary would never be written.
CALL_TIMEOUT_SEC = 30
# After an attempt that wrote nothing (model error, timeout, empty or identical
# text) leave the chat alone for this long.  Without it the still-satisfied
# time trigger would send the same backlog to the model on EVERY new message.
RETRY_BACKOFF_SEC = 600


class MemoryConsolidator:
    """Checks consolidation triggers and orchestrates memory updates.

    Usage:
        consolidator = MemoryConsolidator(store, summarizer)

        # In router.handle(), after persisting each message:
        consolidator.check_and_consolidate(chat_id)
    """

    def __init__(self, store: "MessageStore",
                 summarizer: "AbstractSummarizer"):
        self._store = store
        self._summarizer = summarizer
        self._lock = threading.Lock()
        self._active_chats: set[str] = set()
        # chat_id -> earliest time of the next attempt after a failed one
        self._retry_after: dict[str, float] = {}

    def check_and_consolidate(self, chat_id: str) -> bool:
        """Check if consolidation is needed and run it if so.

        Returns True if consolidation was performed, False if skipped.
        Never raises — all errors are caught and logged internally.
        """
        with self._lock:
            if chat_id in self._active_chats:
                logger.debug(
                    "Memory consolidation already active for %s; skipping",
                    chat_id[:30],
                )
                return False
            self._active_chats.add(chat_id)
        try:
            return self._check_and_consolidate_impl(chat_id)
        except Exception as e:
            logger.error(
                "Memory consolidation error for chat %s: %s",
                chat_id[:30], e,
            )
            return False
        finally:
            with self._lock:
                self._active_chats.discard(chat_id)

    def check_and_consolidate_async(self, chat_id: str) -> bool:
        """Start consolidation in a daemon thread if needed.

        This keeps user-facing replies from waiting on memory summarization.
        Returns True if a background worker was started.
        """
        with self._lock:
            if chat_id in self._active_chats:
                logger.debug(
                    "Memory consolidation already active for %s; skipping async start",
                    chat_id[:30],
                )
                return False
            self._active_chats.add(chat_id)

        thread = threading.Thread(
            target=self._run_reserved,
            args=(chat_id,),
            daemon=True,
            name=f"memory-consolidate-{chat_id[:12]}",
        )
        thread.start()
        return True

    def _run_reserved(self, chat_id: str) -> None:
        try:
            self._check_and_consolidate_impl(chat_id)
        except Exception as e:
            logger.error(
                "Memory consolidation error for chat %s: %s",
                chat_id[:30], e,
            )
        finally:
            with self._lock:
                self._active_chats.discard(chat_id)

    # ── Internal ───────────────────────────────────────────────────

    def _back_off(self, chat_id: str) -> None:
        with self._lock:
            self._retry_after[chat_id] = time.time() + RETRY_BACKOFF_SEC

    def call_timeout(self) -> float:
        """Seconds to wait for the model: ``CALL_TIMEOUT_SEC``, or a local model's own timeout."""
        try:
            floor = float(getattr(self._summarizer, "timeout_floor", 0) or 0)
        except (TypeError, ValueError):
            floor = 0.0
        return max(float(CALL_TIMEOUT_SEC), floor)

    def _call_model(self, existing_memory: str, new_messages: list[dict]) -> str:
        """Run one consolidation call, waiting at most ``call_timeout()`` seconds.

        On timeout the caller is released at once; the slow call keeps running
        in its worker thread and its late result is discarded.  A call that
        finished (with a result or an error) has its idle worker thread
        reaped before returning, so nothing lingers after it.
        """
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(
            self._summarizer.consolidate_memory,
            existing_memory=existing_memory,
            new_messages=new_messages,
        )
        try:
            return future.result(timeout=self.call_timeout())
        finally:
            pool.shutdown(wait=future.done())

    def _check_and_consolidate_impl(self, chat_id: str) -> bool:
        """Internal: check triggers and run consolidation."""
        with self._lock:
            retry_after = self._retry_after.get(chat_id, 0.0)
        if time.time() < retry_after:
            return False  # a recent attempt failed or changed nothing

        memory = self._store.get_group_memory(chat_id)
        last_id = memory["last_message_id"] if memory else None
        last_consolidated = memory["last_consolidated"] if memory else None

        # Count new messages since last consolidation
        new_count = self._store.get_new_message_count(chat_id, last_id)

        # Trigger check
        # 首次合并使用 Unix epoch 作为虚拟基点，使时间阈值自然生效
        effective_last = last_consolidated if last_consolidated is not None else 0
        time_ok = (time.time() - effective_last) >= CONSOLIDATE_TIME_THRESHOLD_SEC
        msg_ok = new_count >= CONSOLIDATE_MSG_THRESHOLD

        if not time_ok and not msg_ok:
            return False  # nothing to do

        if new_count == 0:
            return False  # no new messages to incorporate

        # Fetch new messages
        new_messages = self._store.get_messages_since_id(
            chat_id, last_id, limit=MAX_NEW_MSGS_PER_CONSOLIDATION,
        )
        if not new_messages:
            return False

        existing_memory = memory["memory_text"] if memory else ""

        logger.info(
            "Consolidating memory for %s (%d new msgs, existing memory=%d chars)...",
            chat_id[:30], len(new_messages), len(existing_memory),
        )

        # Call AI consolidation with a timeout so a hung API doesn't block
        # the caller indefinitely.
        try:
            updated = self._call_model(existing_memory, new_messages)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Memory consolidation timed out after %ds for %s — skipping this cycle",
                self.call_timeout(), chat_id[:30],
            )
            self._back_off(chat_id)
            return False
        except Exception:
            self._back_off(chat_id)
            raise

        if not updated or updated == existing_memory:
            logger.info(
                "Memory consolidation returned unchanged text for %s — skipping write",
                chat_id[:30],
            )
            self._back_off(chat_id)
            return False

        # Persist
        total_count = (memory["message_count"] if memory else 0) + len(new_messages)
        last_msg_id = new_messages[-1]["message_id"]

        self._store.upsert_group_memory(
            chat_id=chat_id,
            memory_text=updated,
            message_count=total_count,
            last_message_id=last_msg_id,
        )
        with self._lock:
            self._retry_after.pop(chat_id, None)

        logger.info(
            "Memory consolidated for %s: %d msgs → %d chars (total %d msgs processed)",
            chat_id[:30], len(new_messages), len(updated), total_count,
        )
        return True
