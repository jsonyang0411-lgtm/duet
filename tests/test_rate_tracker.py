"""Characterization tests for RateTracker (src/proactive/rate_tracker.py).

Phase 0 safety net: these tests pin the CURRENT behaviour of the in-memory
sliding-window rate tracker -- quirks included -- so that a later refactor
shows up as an intentional diff instead of a silent change.

A fake clock replaces the module's ``time`` reference, so nothing sleeps and
nothing depends on the wall clock.  Quirks are marked with
``# PINNED (known issue, expected to change in Phase N): <why>``.
"""

import threading
import unittest
from unittest.mock import patch

import src.proactive.rate_tracker as rate_tracker_module
from src.proactive.rate_tracker import RateTracker


class FakeClock:
    """Minimal stand-in for the ``time`` module (only ``time()`` is used)."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_LOCK_TYPES = (type(threading.Lock()), type(threading.RLock()))


def holds_lock(obj) -> bool:
    """True when any instance attribute of *obj* is a threading lock."""
    return any(isinstance(v, _LOCK_TYPES) for v in vars(obj).values())


class RateTrackerTestCase(unittest.TestCase):
    """Base class: patches the tracker module's clock."""

    def setUp(self):
        self.clock = FakeClock()
        patcher = patch.object(rate_tracker_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record_n(self, tracker: RateTracker, chat_id: str, n: int) -> None:
        for _ in range(n):
            tracker.record(chat_id)


class RateComputationTests(RateTrackerTestCase):
    def test_unknown_chat_rate_is_zero(self):
        tracker = RateTracker()
        self.assertEqual(tracker.rate("never-seen"), 0.0)

    def test_unknown_chat_count_is_zero(self):
        tracker = RateTracker()
        self.assertEqual(tracker.count("never-seen"), 0)

    def test_querying_unknown_chat_does_not_create_a_bucket(self):
        tracker = RateTracker()
        tracker.rate("ghost")
        tracker.count("ghost")
        tracker.clear("ghost")
        self.assertEqual(len(tracker._buckets), 0)

    def test_default_window_is_120_seconds(self):
        tracker = RateTracker()
        tracker.record("chat")
        # 1 message / (120 s / 60) = 0.5 msgs per minute
        self.assertEqual(tracker.rate("chat"), 0.5)

    def test_rate_is_count_divided_by_window_in_minutes(self):
        for window, count, expected in [
            (120, 6, 3.0),
            (60, 6, 6.0),
            (30, 3, 6.0),
            (300, 10, 2.0),
        ]:
            tracker = RateTracker(window_sec=window)
            self.record_n(tracker, "chat", count)
            self.assertEqual(
                tracker.rate("chat"), expected,
                msg=f"window={window} count={count}",
            )

    def test_rate_is_not_normalised_by_elapsed_time_since_first_message(self):
        # Three messages in the same instant already count as 3 / 2 min.
        tracker = RateTracker(window_sec=120)
        self.record_n(tracker, "chat", 3)
        self.assertEqual(tracker.rate("chat"), 1.5)

    def test_count_reports_messages_in_window(self):
        tracker = RateTracker()
        self.assertEqual(tracker.count("chat"), 0)
        self.record_n(tracker, "chat", 4)
        self.assertEqual(tracker.count("chat"), 4)

    def test_chats_are_tracked_independently(self):
        tracker = RateTracker()
        self.record_n(tracker, "a", 4)
        self.record_n(tracker, "b", 1)
        self.assertEqual(tracker.rate("a"), 2.0)
        self.assertEqual(tracker.rate("b"), 0.5)
        self.assertEqual(tracker.count("a"), 4)
        self.assertEqual(tracker.count("b"), 1)

    def test_rate_zero_window_with_live_message_raises_zero_division(self):
        # PINNED (known issue, expected to change in Phase N): RateTracker does
        # not validate window_sec; only BotConfig does (>= 30).  A zero window
        # divides by zero as soon as one message is inside the window.
        tracker = RateTracker(window_sec=0)
        tracker.record("chat")
        with self.assertRaises(ZeroDivisionError):
            tracker.rate("chat")


class SlidingWindowTests(RateTrackerTestCase):
    def test_messages_inside_window_keep_counting(self):
        tracker = RateTracker(window_sec=120)
        self.record_n(tracker, "chat", 3)
        self.clock.advance(119)
        self.assertEqual(tracker.count("chat"), 3)
        self.assertEqual(tracker.rate("chat"), 1.5)

    def test_messages_past_window_expire(self):
        tracker = RateTracker(window_sec=120)
        self.record_n(tracker, "chat", 3)
        self.clock.advance(121)
        self.assertEqual(tracker.rate("chat"), 0.0)
        self.assertEqual(tracker.count("chat"), 0)

    def test_message_exactly_window_old_is_still_counted(self):
        # PINNED (known issue, expected to change in Phase N): pruning uses
        # ``timestamp < now - window`` so the window is closed on its old
        # edge; an arrival exactly window_sec old still counts.  Compare the
        # other trackers, whose expiry boundaries are not the same.
        tracker = RateTracker(window_sec=120)
        tracker.record("chat")
        self.clock.advance(120)
        self.assertEqual(tracker.count("chat"), 1)
        self.clock.advance(0.001)
        self.assertEqual(tracker.count("chat"), 0)

    def test_only_expired_messages_are_pruned(self):
        tracker = RateTracker(window_sec=120)
        tracker.record("chat")          # t0
        self.clock.advance(30)
        tracker.record("chat")          # t0 + 30
        self.clock.advance(30)
        tracker.record("chat")          # t0 + 60
        self.clock.advance(70)          # now = t0 + 130 -> cutoff = t0 + 10
        self.assertEqual(tracker.count("chat"), 2)
        self.assertEqual(tracker.rate("chat"), 1.0)

    def test_rate_prunes_stored_timestamps_in_place(self):
        tracker = RateTracker(window_sec=120)
        self.record_n(tracker, "chat", 5)
        self.clock.advance(500)
        tracker.rate("chat")
        self.assertEqual(len(tracker._buckets["chat"]), 0)

    def test_expired_chat_rate_drops_back_to_zero_after_burst(self):
        tracker = RateTracker(window_sec=120)
        self.record_n(tracker, "chat", 20)
        self.assertEqual(tracker.rate("chat"), 10.0)
        self.clock.advance(200)
        self.assertEqual(tracker.rate("chat"), 0.0)

    def test_new_message_after_expiry_starts_a_fresh_window(self):
        tracker = RateTracker(window_sec=120)
        self.record_n(tracker, "chat", 5)
        self.clock.advance(300)
        tracker.record("chat")
        self.assertEqual(tracker.count("chat"), 1)
        self.assertEqual(tracker.rate("chat"), 0.5)

    def test_expired_chat_keeps_an_empty_bucket_until_cleanup(self):
        # PINNED (known issue, expected to change in Phase N): rate() prunes the
        # timestamps but leaves the (now empty) per-chat deque in _buckets, so
        # chat ids accumulate until cleanup() runs on every 100th rate() call.
        tracker = RateTracker(window_sec=120)
        tracker.record("chat")
        self.clock.advance(500)
        self.assertEqual(tracker.rate("chat"), 0.0)
        self.assertIn("chat", tracker._buckets)
        self.assertEqual(len(tracker._buckets["chat"]), 0)


class ClearTests(RateTrackerTestCase):
    def test_clear_removes_all_data_for_a_chat(self):
        tracker = RateTracker()
        self.record_n(tracker, "chat", 4)
        tracker.clear("chat")
        self.assertEqual(tracker.rate("chat"), 0.0)
        self.assertEqual(tracker.count("chat"), 0)
        self.assertNotIn("chat", tracker._buckets)

    def test_clear_only_affects_the_given_chat(self):
        tracker = RateTracker()
        self.record_n(tracker, "a", 2)
        self.record_n(tracker, "b", 2)
        tracker.clear("a")
        self.assertEqual(tracker.count("a"), 0)
        self.assertEqual(tracker.count("b"), 2)

    def test_clear_unknown_chat_is_a_noop(self):
        tracker = RateTracker()
        tracker.clear("never-seen")  # must not raise
        self.assertEqual(len(tracker._buckets), 0)

    def test_recording_after_clear_starts_from_zero(self):
        tracker = RateTracker()
        self.record_n(tracker, "chat", 4)
        tracker.clear("chat")
        tracker.record("chat")
        self.assertEqual(tracker.count("chat"), 1)


class CleanupTests(RateTrackerTestCase):
    def test_cleanup_on_empty_tracker_is_a_noop(self):
        tracker = RateTracker()
        tracker.cleanup()
        self.assertEqual(len(tracker._buckets), 0)

    def test_cleanup_drops_chats_with_no_messages_in_window(self):
        tracker = RateTracker(window_sec=120)
        tracker.record("old")                 # t0
        self.clock.advance(100)
        tracker.record("recent")              # t0 + 100
        self.clock.advance(30)                # now = t0 + 130
        tracker.cleanup()
        self.assertNotIn("old", tracker._buckets)
        self.assertIn("recent", tracker._buckets)

    def test_cleanup_prunes_partially_expired_chats_in_place(self):
        tracker = RateTracker(window_sec=120)
        tracker.record("chat")                # t0
        self.clock.advance(100)
        tracker.record("chat")                # t0 + 100
        self.clock.advance(30)                # now = t0 + 130
        tracker.cleanup()
        self.assertEqual(len(tracker._buckets["chat"]), 1)

    def test_cleanup_keeps_message_exactly_window_old(self):
        # PINNED (known issue, expected to change in Phase N): same closed
        # old-edge boundary as rate(): ``timestamp < cutoff`` is pruned,
        # ``timestamp == cutoff`` is kept.
        tracker = RateTracker(window_sec=120)
        tracker.record("chat")
        self.clock.advance(120)
        tracker.cleanup()
        self.assertIn("chat", tracker._buckets)
        self.clock.advance(1)
        tracker.cleanup()
        self.assertNotIn("chat", tracker._buckets)

    def test_rate_triggers_cleanup_on_every_100th_call(self):
        tracker = RateTracker(window_sec=120)
        tracker.record("stale")
        self.clock.advance(500)
        for _ in range(99):
            tracker.rate("other")
        # 99 rate() calls: nobody asked about "stale", so it is still stored.
        self.assertIn("stale", tracker._buckets)
        tracker.rate("other")                 # 100th call -> cleanup()
        self.assertNotIn("stale", tracker._buckets)

    def test_count_also_advances_the_cleanup_counter(self):
        # count() delegates to rate(), so it consumes one tick of the
        # every-100-calls cleanup cadence per call.
        tracker = RateTracker(window_sec=120)
        tracker.record("stale")
        self.clock.advance(500)
        for _ in range(99):
            tracker.count("other")
        self.assertIn("stale", tracker._buckets)
        tracker.count("other")
        self.assertNotIn("stale", tracker._buckets)

    def test_cleanup_cadence_repeats_every_100_calls(self):
        tracker = RateTracker(window_sec=120)
        for _ in range(100):
            tracker.rate("x")
        tracker.record("stale")
        self.clock.advance(500)
        for _ in range(99):
            tracker.rate("x")
        self.assertIn("stale", tracker._buckets)
        tracker.rate("x")                     # 200th call
        self.assertNotIn("stale", tracker._buckets)

    def test_record_never_prunes_or_advances_the_cleanup_counter(self):
        # PINNED (known issue, expected to change in Phase N): only rate(),
        # count() and cleanup() prune.  A caller that records but never reads
        # (ProactiveGate does exactly that while proactive_enabled is False)
        # grows the per-chat deque without bound.
        tracker = RateTracker(window_sec=120)
        for _ in range(250):
            tracker.record("chat")
            self.clock.advance(10)            # far longer than the window overall
        self.assertEqual(len(tracker._buckets["chat"]), 250)
        self.assertEqual(tracker._call_count, 0)


class ThreadSafetyTests(unittest.TestCase):
    def test_tracker_holds_a_lock(self):
        # The bucket dict and deques are shared by every worker thread of the
        # message pool (WcdbBackend uses ThreadPoolExecutor(max_workers=4)).
        # Fixed in Phase 5; the concurrency itself is exercised in
        # tests/test_shared_state_locks.py.
        self.assertTrue(holds_lock(RateTracker()))


if __name__ == "__main__":
    unittest.main()
