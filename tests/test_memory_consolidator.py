"""Characterization tests for MemoryConsolidator (Phase 0 safety net).

Pins when ``MemoryConsolidator`` decides to ask the summarizer for a new group
memory, what it writes back, and how it behaves around de-duplication, daemon
threads and the "30 s timeout".  A real ``MessageStore`` on in-memory SQLite is
used (``initialize_db(":memory:")``) together with a fake summarizer; the
consolidator's module clock and thread factory are patched where a test needs
determinism.  Nothing sleeps and every thread is joined.

Convention: ``# PINNED (known issue, expected to change in Phase N): <why>``
marks behaviour that is documented here but NOT endorsed.
"""

import concurrent.futures
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from src.db import MessageStore, initialize_db
from src.memory import consolidator as consolidator_module
from src.memory.consolidator import (
    CONSOLIDATE_MSG_THRESHOLD,
    CONSOLIDATE_TIME_THRESHOLD_SEC,
    MAX_NEW_MSGS_PER_CONSOLIDATION,
    MemoryConsolidator,
)

CHAT = "room1@chatroom"
OTHER_CHAT = "room2@chatroom"
BASE_TS = 1_700_000_000


# ── fakes and helpers ──────────────────────────────────────────────────

class FakeSummarizer:
    """Duck-typed stand-in: the consolidator only calls consolidate_memory().

    ``result`` is returned as is; an exception instance is raised; a callable
    is invoked with ``(existing_memory, new_messages)``.
    """

    def __init__(self, result="新的记忆"):
        self.result = result
        self.calls = []

    def consolidate_memory(self, existing_memory, new_messages):
        self.calls.append({
            "existing_memory": existing_memory,
            "message_ids": [m["message_id"] for m in new_messages],
        })
        result = self.result
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(existing_memory, new_messages)
        return result


class FakeClock:
    now = 0.0


@pytest.fixture
def conn():
    connection = initialize_db(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def store(conn):
    return MessageStore(conn)


@pytest.fixture
def clock(monkeypatch):
    """Replace ONLY the consolidator module's ``time`` with a settable clock."""
    fake = FakeClock()
    monkeypatch.setattr(consolidator_module, "time", SimpleNamespace(time=lambda: fake.now))
    return fake


def add_messages(store, chat_id, count, *, start=0, timestamps=None):
    """Insert ``count`` text messages; ids are ``<chat_id>-<n>``."""
    ids = []
    for i in range(start, start + count):
        message_id = f"{chat_id}-{i}"
        timestamp = timestamps[i - start] if timestamps else BASE_TS + i
        assert store.insert_message({
            "message_id": message_id,
            "chat_id": chat_id,
            "sender_id": "wxid_member",
            "sender_name": "小明",
            "content": f"第{i}条消息",
            "msg_type": 1,
            "timestamp": timestamp,
        })
        ids.append(message_id)
    return ids


def age_memory(conn, chat_id, last_consolidated=1000.0):
    """Force ``last_consolidated`` to a known value (the store writes real time)."""
    conn.execute(
        "UPDATE group_memory SET last_consolidated = ? WHERE chat_id = ?",
        (last_consolidated, chat_id),
    )
    conn.commit()


def seed_memory(store, conn, chat_id, *, text="旧记忆", message_count=0,
                last_message_id=None, last_consolidated=1000.0):
    store.upsert_group_memory(chat_id, text, message_count, last_message_id)
    age_memory(conn, chat_id, last_consolidated)


class RecordingThreadFactory:
    """Drop-in for ``threading`` in the consolidator module that records threads."""

    def __init__(self):
        self.threads = []
        outer = self

        class _Thread(threading.Thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                outer.threads.append(self)

        self.Thread = _Thread
        self.Lock = threading.Lock

    def join_all(self):
        for thread in self.threads:
            thread.join(5)
            assert not thread.is_alive()


@pytest.fixture
def threads(monkeypatch):
    factory = RecordingThreadFactory()
    monkeypatch.setattr(consolidator_module, "threading", factory)
    yield factory
    factory.join_all()


# ── trigger thresholds ──────────────────────────────────────────────────

class TestTriggerThresholds:
    # NOTE: the module docstring still says "30 messages" and "2 hours"; the
    # constants below are what actually runs.
    def test_tuning_constants(self):
        assert CONSOLIDATE_MSG_THRESHOLD == 50
        assert CONSOLIDATE_TIME_THRESHOLD_SEC == 3600
        assert MAX_NEW_MSGS_PER_CONSOLIDATION == 400

    def test_first_ever_consolidation_uses_the_unix_epoch_as_time_base(self, store, clock):
        ids = add_messages(store, CHAT, 1)
        summarizer = FakeSummarizer("第一篇日记")
        consolidator = MemoryConsolidator(store, summarizer)

        clock.now = CONSOLIDATE_TIME_THRESHOLD_SEC - 1     # 1 message, "age" 3599 s
        assert consolidator.check_and_consolidate(CHAT) is False
        assert summarizer.calls == []

        clock.now = CONSOLIDATE_TIME_THRESHOLD_SEC         # "age" exactly 3600 s
        assert consolidator.check_and_consolidate(CHAT) is True
        assert summarizer.calls == [{"existing_memory": "", "message_ids": ids}]

    def test_with_the_real_clock_a_new_chat_consolidates_after_a_single_message(self, store):
        add_messages(store, CHAT, 1)
        summarizer = FakeSummarizer("第一篇日记")

        assert MemoryConsolidator(store, summarizer).check_and_consolidate(CHAT) is True

        assert store.get_group_memory(CHAT)["memory_text"] == "第一篇日记"

    def test_chat_without_any_message_is_skipped(self, store, clock):
        summarizer = FakeSummarizer()
        clock.now = 10 * CONSOLIDATE_TIME_THRESHOLD_SEC

        assert MemoryConsolidator(store, summarizer).check_and_consolidate(CHAT) is False

        assert summarizer.calls == []
        assert store.get_group_memory(CHAT) is None

    def test_no_new_messages_skips_even_when_the_time_threshold_is_met(self, store, conn, clock):
        ids = add_messages(store, CHAT, 3)
        seed_memory(store, conn, CHAT, message_count=3, last_message_id=ids[-1])
        summarizer = FakeSummarizer()
        clock.now = 1000.0 + 10 * CONSOLIDATE_TIME_THRESHOLD_SEC

        assert MemoryConsolidator(store, summarizer).check_and_consolidate(CHAT) is False

        assert summarizer.calls == []

    def test_time_threshold_boundary_with_existing_memory(self, store, conn, clock):
        ids = add_messages(store, CHAT, 2)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=ids[0])
        summarizer = FakeSummarizer("更新后的记忆")
        consolidator = MemoryConsolidator(store, summarizer)

        clock.now = 1000.0 + CONSOLIDATE_TIME_THRESHOLD_SEC - 0.001
        assert consolidator.check_and_consolidate(CHAT) is False
        assert summarizer.calls == []

        clock.now = 1000.0 + CONSOLIDATE_TIME_THRESHOLD_SEC
        assert consolidator.check_and_consolidate(CHAT) is True
        assert summarizer.calls == [{"existing_memory": "旧记忆", "message_ids": [ids[1]]}]

    def test_message_threshold_boundary_with_existing_memory(self, store, conn, clock):
        seen = add_messages(store, CHAT, 1)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=seen[0])
        clock.now = 1000.0 + 10                       # consolidated 10 s ago
        summarizer = FakeSummarizer("更新后的记忆")
        consolidator = MemoryConsolidator(store, summarizer)

        add_messages(store, CHAT, CONSOLIDATE_MSG_THRESHOLD - 1, start=1)
        assert consolidator.check_and_consolidate(CHAT) is False
        assert summarizer.calls == []

        add_messages(store, CHAT, 1, start=CONSOLIDATE_MSG_THRESHOLD)
        assert consolidator.check_and_consolidate(CHAT) is True
        assert len(summarizer.calls[0]["message_ids"]) == CONSOLIDATE_MSG_THRESHOLD
        assert store.get_group_memory(CHAT)["message_count"] == 1 + CONSOLIDATE_MSG_THRESHOLD

    def test_counted_messages_that_cannot_be_fetched_are_a_no_op(self):
        class VanishingStore:
            def get_group_memory(self, chat_id):
                return None

            def get_new_message_count(self, chat_id, since_message_id):
                return 5

            def get_messages_since_id(self, chat_id, since_message_id, limit=200):
                return []

        summarizer = FakeSummarizer()

        assert MemoryConsolidator(VanishingStore(), summarizer).check_and_consolidate(CHAT) is False

        assert summarizer.calls == []

    def test_messages_of_other_chats_do_not_count(self, store, conn, clock):
        seen = add_messages(store, CHAT, 1)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=seen[0])
        add_messages(store, OTHER_CHAT, 10)
        clock.now = 1000.0 + 10 * CONSOLIDATE_TIME_THRESHOLD_SEC
        summarizer = FakeSummarizer()

        assert MemoryConsolidator(store, summarizer).check_and_consolidate(CHAT) is False

        assert summarizer.calls == []

    def test_backlog_beyond_the_per_run_cap_waits_for_the_next_trigger(
        self, store, conn, clock, monkeypatch,
    ):
        monkeypatch.setattr(consolidator_module, "MAX_NEW_MSGS_PER_CONSOLIDATION", 3)
        ids = add_messages(store, CHAT, 5)
        summarizer = FakeSummarizer("记忆")
        consolidator = MemoryConsolidator(store, summarizer)
        clock.now = 10 * CONSOLIDATE_TIME_THRESHOLD_SEC

        assert consolidator.check_and_consolidate(CHAT) is True

        memory = store.get_group_memory(CHAT)
        assert summarizer.calls[0]["message_ids"] == ids[:3]
        assert (memory["message_count"], memory["last_message_id"]) == (3, ids[2])

        summarizer.result = "记忆二"
        age_memory(conn, CHAT)
        assert consolidator.check_and_consolidate(CHAT) is True

        memory = store.get_group_memory(CHAT)
        assert summarizer.calls[1] == {"existing_memory": "记忆", "message_ids": ids[3:]}
        assert (memory["message_count"], memory["last_message_id"]) == (5, ids[4])


# ── what is written back ────────────────────────────────────────────────

class TestWriteBehaviour:
    def test_success_persists_text_count_and_cursor(self, store, conn):
        first = add_messages(store, CHAT, 3)
        summarizer = FakeSummarizer("日记一")
        consolidator = MemoryConsolidator(store, summarizer)
        before = time.time()

        assert consolidator.check_and_consolidate(CHAT) is True

        memory = store.get_group_memory(CHAT)
        assert memory["memory_text"] == "日记一"
        assert memory["message_count"] == 3
        assert memory["last_message_id"] == first[-1]
        assert before <= memory["last_consolidated"] <= time.time()

        second = add_messages(store, CHAT, 2, start=3)
        summarizer.result = "日记二"
        age_memory(conn, CHAT)

        assert consolidator.check_and_consolidate(CHAT) is True

        assert summarizer.calls[1] == {"existing_memory": "日记一", "message_ids": second}
        memory = store.get_group_memory(CHAT)
        assert (memory["memory_text"], memory["message_count"], memory["last_message_id"]) == (
            "日记二", 5, second[-1],
        )

    def test_summarizer_is_called_with_keywords_and_full_message_dicts(self, store):
        add_messages(store, CHAT, 1)
        seen = {}

        class KeywordRecorder:
            def consolidate_memory(self, **kwargs):
                seen.update(kwargs)
                return "日记"

        assert MemoryConsolidator(store, KeywordRecorder()).check_and_consolidate(CHAT) is True

        assert set(seen) == {"existing_memory", "new_messages"}
        assert seen["existing_memory"] == ""
        assert set(seen["new_messages"][0]) == {
            "message_id", "chat_id", "sender_id", "sender_name",
            "content", "msg_type", "timestamp",
        }

    @pytest.mark.parametrize("result", ["", None], ids=["empty-string", "none"])
    def test_empty_result_is_not_written(self, store, conn, result):
        ids = add_messages(store, CHAT, 2)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=ids[0])
        before = store.get_group_memory(CHAT)
        summarizer = FakeSummarizer(result)

        assert MemoryConsolidator(store, summarizer).check_and_consolidate(CHAT) is False

        assert len(summarizer.calls) == 1
        assert store.get_group_memory(CHAT) == before

    def test_empty_result_for_a_new_chat_creates_no_row(self, store):
        add_messages(store, CHAT, 2)

        assert MemoryConsolidator(store, FakeSummarizer("")).check_and_consolidate(CHAT) is False

        assert store.get_group_memory(CHAT) is None

    def test_unchanged_text_is_not_written(self, store, conn):
        ids = add_messages(store, CHAT, 2)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=ids[0])
        before = store.get_group_memory(CHAT)
        summarizer = FakeSummarizer("旧记忆")

        assert MemoryConsolidator(store, summarizer).check_and_consolidate(CHAT) is False

        assert store.get_group_memory(CHAT) == before

    # Fixed in Phase 1: a no-op result still advances neither the cursor nor
    # ``last_consolidated`` (so the time trigger stays satisfied), but the chat
    # now backs off for RETRY_BACKOFF_SEC instead of re-sending the same backlog
    # to the model on EVERY later message (an API outage that makes the backend
    # return the old text looked exactly like this).
    def test_unchanged_text_backs_off_instead_of_calling_the_model_every_time(
        self, store, conn, clock,
    ):
        ids = add_messages(store, CHAT, 2)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=ids[0])
        summarizer = FakeSummarizer("旧记忆")
        consolidator = MemoryConsolidator(store, summarizer)
        clock.now = 10_000.0                       # time trigger satisfied

        for _ in range(3):
            assert consolidator.check_and_consolidate(CHAT) is False
        assert summarizer.calls == [{"existing_memory": "旧记忆", "message_ids": [ids[1]]}]

        clock.now += consolidator_module.RETRY_BACKOFF_SEC - 1
        consolidator.check_and_consolidate(CHAT)
        assert len(summarizer.calls) == 1          # still backing off

        clock.now += 2
        consolidator.check_and_consolidate(CHAT)
        assert len(summarizer.calls) == 2          # backoff over: one more try

    def test_a_failing_model_call_also_backs_off(self, store, conn, clock):
        ids = add_messages(store, CHAT, 2)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=ids[0])
        summarizer = FakeSummarizer(RuntimeError("API down"))
        consolidator = MemoryConsolidator(store, summarizer)
        clock.now = 10_000.0

        for _ in range(3):
            assert consolidator.check_and_consolidate(CHAT) is False

        assert len(summarizer.calls) == 1

    def test_backoff_is_per_chat(self, store, conn, clock):
        for chat in (CHAT, OTHER_CHAT):
            ids = add_messages(store, chat, 2)
            seed_memory(store, conn, chat, message_count=1, last_message_id=ids[0])
        summarizer = FakeSummarizer("旧记忆")
        consolidator = MemoryConsolidator(store, summarizer)
        clock.now = 10_000.0

        consolidator.check_and_consolidate(CHAT)
        consolidator.check_and_consolidate(OTHER_CHAT)

        assert len(summarizer.calls) == 2          # the second chat is not held back

    def test_a_successful_run_clears_the_backoff(self, store, conn, clock):
        ids = add_messages(store, CHAT, 2)
        seed_memory(store, conn, CHAT, message_count=1, last_message_id=ids[0])
        results = iter(["旧记忆", "新写入的记忆"])
        summarizer = FakeSummarizer(lambda existing, messages: next(results))
        consolidator = MemoryConsolidator(store, summarizer)
        clock.now = 10_000.0

        assert consolidator.check_and_consolidate(CHAT) is False
        assert CHAT in consolidator._retry_after
        clock.now += consolidator_module.RETRY_BACKOFF_SEC + 1
        assert consolidator.check_and_consolidate(CHAT) is True

        assert store.get_group_memory(CHAT)["memory_text"] == "新写入的记忆"
        assert CHAT not in consolidator._retry_after

    def test_summarizer_exception_is_swallowed(self, store):
        add_messages(store, CHAT, 2)
        consolidator = MemoryConsolidator(store, FakeSummarizer(RuntimeError("API down")))

        assert consolidator.check_and_consolidate(CHAT) is False

        assert store.get_group_memory(CHAT) is None
        assert consolidator._active_chats == set()

    def test_store_failure_is_swallowed(self):
        class BrokenStore:
            def get_group_memory(self, chat_id):
                raise RuntimeError("database is locked")

        consolidator = MemoryConsolidator(BrokenStore(), FakeSummarizer())

        assert consolidator.check_and_consolidate(CHAT) is False
        assert consolidator._active_chats == set()

    # PINNED (known issue, expected to change in Phase 3): the cursor is the
    # message_id of the LAST message in timestamp order, but "new" means "rowid
    # greater than the cursor's rowid".  A message that arrives late with an
    # older timestamp has a larger rowid and is therefore consolidated twice.
    def test_late_arriving_older_message_is_consolidated_twice(self, store, conn):
        ids = add_messages(store, CHAT, 3, timestamps=[100, 300, 200])
        summarizer = FakeSummarizer("记忆一")
        consolidator = MemoryConsolidator(store, summarizer)

        assert consolidator.check_and_consolidate(CHAT) is True

        # ordered by timestamp: id0 (100), id2 (200), id1 (300)
        assert summarizer.calls[0]["message_ids"] == [ids[0], ids[2], ids[1]]
        assert store.get_group_memory(CHAT)["last_message_id"] == ids[1]

        summarizer.result = "记忆二"
        age_memory(conn, CHAT)
        assert consolidator.check_and_consolidate(CHAT) is True

        assert summarizer.calls[1] == {"existing_memory": "记忆一", "message_ids": [ids[2]]}
        assert store.get_group_memory(CHAT)["message_count"] == 4   # 3 messages, counted 4


# ── per-chat de-duplication ─────────────────────────────────────────────

class TestActiveChatDeduplication:
    def test_reentrant_requests_for_the_same_chat_are_skipped(self, store):
        add_messages(store, CHAT, 1)
        inner = []

        def summarize(existing_memory, new_messages):
            inner.append(consolidator.check_and_consolidate(CHAT))
            inner.append(consolidator.check_and_consolidate_async(CHAT))
            return "日记"

        summarizer = FakeSummarizer(summarize)
        consolidator = MemoryConsolidator(store, summarizer)

        assert consolidator.check_and_consolidate(CHAT) is True

        assert inner == [False, False]
        assert len(summarizer.calls) == 1

    def test_other_chats_are_not_blocked_by_an_active_one(self, store):
        add_messages(store, CHAT, 1)
        add_messages(store, OTHER_CHAT, 1)
        nested = []

        def summarize(existing_memory, new_messages):
            if new_messages[0]["chat_id"] == CHAT:
                nested.append(consolidator.check_and_consolidate(OTHER_CHAT))
                return "日记A"
            return "日记B"

        consolidator = MemoryConsolidator(store, FakeSummarizer(summarize))

        assert consolidator.check_and_consolidate(CHAT) is True

        assert nested == [True]
        assert store.get_group_memory(CHAT)["memory_text"] == "日记A"
        assert store.get_group_memory(OTHER_CHAT)["memory_text"] == "日记B"

    def test_chat_is_released_after_every_run(self, store, conn):
        add_messages(store, CHAT, 1)
        summarizer = FakeSummarizer("日记")
        consolidator = MemoryConsolidator(store, summarizer)

        assert consolidator.check_and_consolidate(CHAT) is True
        assert consolidator._active_chats == set()

        add_messages(store, CHAT, 1, start=1)
        age_memory(conn, CHAT)
        summarizer.result = "日记二"
        assert consolidator.check_and_consolidate(CHAT) is True
        assert consolidator._active_chats == set()


# ── check_and_consolidate_async ─────────────────────────────────────────

class TestAsyncWorker:
    def test_starts_a_named_daemon_thread_and_releases_the_chat_when_done(self, store, threads):
        add_messages(store, CHAT, 2)
        started, release = threading.Event(), threading.Event()

        def blocking(existing_memory, new_messages):
            started.set()
            release.wait(5)
            return "后台日记"

        summarizer = FakeSummarizer(blocking)
        consolidator = MemoryConsolidator(store, summarizer)

        try:
            assert consolidator.check_and_consolidate_async(CHAT) is True
            assert started.wait(5)
            (worker,) = threads.threads
            assert worker.daemon is True
            assert worker.name == f"memory-consolidate-{CHAT[:12]}"
            assert CHAT in consolidator._active_chats

            # while the worker is busy the same chat is skipped, sync or async
            assert consolidator.check_and_consolidate_async(CHAT) is False
            assert consolidator.check_and_consolidate(CHAT) is False
            assert len(threads.threads) == 1
        finally:
            release.set()
            threads.join_all()

        assert consolidator._active_chats == set()
        assert len(summarizer.calls) == 1
        assert store.get_group_memory(CHAT)["memory_text"] == "后台日记"

    # PINNED (known issue, expected to change in Phase 3): the "if needed" in
    # the docstring is not checked before spawning: one thread is started per
    # call (i.e. per incoming message) even when there is nothing to do.
    def test_a_worker_is_started_even_when_there_is_nothing_to_consolidate(self, store, threads):
        summarizer = FakeSummarizer()
        consolidator = MemoryConsolidator(store, summarizer)

        assert consolidator.check_and_consolidate_async(CHAT) is True
        threads.join_all()

        assert len(threads.threads) == 1
        assert summarizer.calls == []
        assert store.get_group_memory(CHAT) is None
        assert consolidator._active_chats == set()

    def test_worker_exceptions_are_swallowed_and_the_chat_is_released(self, store, threads):
        add_messages(store, CHAT, 1)
        consolidator = MemoryConsolidator(store, FakeSummarizer(RuntimeError("API down")))

        assert consolidator.check_and_consolidate_async(CHAT) is True
        threads.join_all()

        assert consolidator._active_chats == set()
        assert store.get_group_memory(CHAT) is None


# ── the 30 s timeout wrapper ────────────────────────────────────────────

class TestTimeoutWrapper:
    # Fixed in Phase 1: the pool is no longer a ``with`` block, so when the
    # timeout fires the caller (the router thread for the sync API, the daemon
    # worker for the async one) is released at once instead of waiting for
    # the slow model call; its late result is discarded.
    #
    # The timeout constant cannot be shortened per test, so ``Future.result``
    # is replaced by a version that "times out" as soon as the worker is busy
    # and records the timeout it was given.
    def test_timeout_frees_the_caller_while_the_worker_is_still_running(
        self, store, monkeypatch, caplog,
    ):
        add_messages(store, CHAT, 1)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        requested = []

        def slow_summarizer(existing_memory, new_messages):
            started.set()
            release.wait(5)
            finished.set()
            return "晚到的日记"

        def expire_once_started(self, timeout=None):
            requested.append(timeout)
            assert started.wait(5)
            raise concurrent.futures.TimeoutError()

        monkeypatch.setattr(concurrent.futures.Future, "result", expire_once_started)
        consolidator = MemoryConsolidator(store, FakeSummarizer(slow_summarizer))

        try:
            with caplog.at_level(logging.WARNING, logger=consolidator_module.logger.name):
                outcome = consolidator.check_and_consolidate(CHAT)

            assert outcome is False
            assert not finished.is_set()             # the caller did not wait
            assert requested == [consolidator_module.CALL_TIMEOUT_SEC] == [30]
            assert "timed out after 30s" in caplog.text
            assert store.get_group_memory(CHAT) is None
            assert consolidator._active_chats == set()
            assert CHAT in consolidator._retry_after  # and it backs off
        finally:
            release.set()                            # let the worker end
            assert finished.wait(5)

    def test_a_local_model_gets_its_own_timeout(self, store):
        cloud = MemoryConsolidator(store, FakeSummarizer())
        assert cloud.call_timeout() == 30
        local = FakeSummarizer()
        local.timeout_floor = 90.0                      # LOCAL_TIMEOUT_SEC
        assert MemoryConsolidator(store, local).call_timeout() == 90.0
        odd = FakeSummarizer()
        odd.timeout_floor = "slow"
        assert MemoryConsolidator(store, odd).call_timeout() == 30

    def test_normal_completion_returns_the_worker_result(self, store):
        add_messages(store, CHAT, 1)
        consolidator = MemoryConsolidator(store, FakeSummarizer("按时完成"))

        assert consolidator.check_and_consolidate(CHAT) is True

        assert store.get_group_memory(CHAT)["memory_text"] == "按时完成"

    # A finished call must not leave its pool thread winding down in the
    # background: callers (and the router tests' "no thread left running"
    # check) see the thread count return to where it was.
    def test_no_thread_is_left_behind_after_a_finished_call(self, store):
        add_messages(store, CHAT, 1)
        before = set(threading.enumerate())
        consolidator = MemoryConsolidator(store, FakeSummarizer("按时完成"))

        assert consolidator.check_and_consolidate(CHAT) is True

        assert set(threading.enumerate()) - before == set()

    def test_no_thread_is_left_behind_after_a_failed_call(self, store):
        add_messages(store, CHAT, 1)
        before = set(threading.enumerate())
        consolidator = MemoryConsolidator(store, FakeSummarizer(RuntimeError("API down")))

        assert consolidator.check_and_consolidate(CHAT) is False

        assert set(threading.enumerate()) - before == set()
