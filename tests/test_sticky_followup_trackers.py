"""Characterization tests for the small in-memory trackers in src/proactive/.

Covers StickyMentionTracker (sticky.py), FollowupListenTracker (followup.py)
and -- beyond the two cases already in tests/test_image_followup.py --
ImageFollowupTracker (image_followup.py).

Phase 0 safety net: pins the CURRENT behaviour, quirks included, so a later
refactor shows up as an intentional diff.  Quirks are marked
``# PINNED (known issue, expected to change in Phase N): <why>``.

Each module's ``time`` reference is replaced by a fake clock; nothing sleeps,
no threads are started and nothing is written to disk.
"""

import logging
import threading
import unittest
from unittest.mock import patch

import src.proactive.followup as followup_module
import src.proactive.image_followup as image_followup_module
import src.proactive.sticky as sticky_module
from src.proactive.followup import FollowupListenTracker, FollowupMatch
from src.proactive.image_followup import ImageFollowupMatch, ImageFollowupTracker
from src.proactive.sticky import StickyMentionTracker


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


class ClockedTestCase(unittest.TestCase):
    """Base class: replaces ``MODULE.time`` with a fake clock."""

    MODULE = None

    def setUp(self):
        self.clock = FakeClock()
        patcher = patch.object(self.MODULE, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Expected WARNINGs (re-registration cap, nearly-expired consume) stay
        # out of the test output; assertLogs()/assertNoLogs() still see them
        # because they install their own level for the duration of the block.
        logger = logging.getLogger(self.MODULE.__name__)
        previous_level = logger.level
        logger.setLevel(logging.CRITICAL + 1)
        self.addCleanup(logger.setLevel, previous_level)


# ═══════════════════════════════════════════════════════════════════════
# StickyMentionTracker
# ═══════════════════════════════════════════════════════════════════════


class StickyBasicsTests(ClockedTestCase):
    MODULE = sticky_module
    CHAT = "room@chatroom"
    USER = "wxid_alice"

    def test_register_then_consume_succeeds(self):
        tracker = StickyMentionTracker()
        tracker.register(self.CHAT, self.USER)
        self.assertTrue(tracker.consume(self.CHAT, self.USER))

    def test_consume_without_register_is_false(self):
        tracker = StickyMentionTracker()
        self.assertFalse(tracker.consume(self.CHAT, self.USER))

    def test_consume_is_one_shot(self):
        tracker = StickyMentionTracker()
        tracker.register(self.CHAT, self.USER)
        self.assertTrue(tracker.consume(self.CHAT, self.USER))
        self.assertFalse(tracker.consume(self.CHAT, self.USER))
        self.assertEqual(len(tracker), 0)

    def test_different_sender_in_the_same_chat_cannot_consume(self):
        tracker = StickyMentionTracker()
        tracker.register(self.CHAT, "wxid_alice")
        self.assertFalse(tracker.consume(self.CHAT, "wxid_bob"))
        # ...and Bob's attempt did not disturb Alice's entry.
        self.assertTrue(tracker.consume(self.CHAT, "wxid_alice"))

    def test_same_sender_in_another_chat_cannot_consume(self):
        tracker = StickyMentionTracker()
        tracker.register("chat-1", self.USER)
        self.assertFalse(tracker.consume("chat-2", self.USER))
        self.assertTrue(tracker.consume("chat-1", self.USER))

    def test_entries_for_two_senders_are_independent(self):
        tracker = StickyMentionTracker()
        tracker.register(self.CHAT, "wxid_alice")
        tracker.register(self.CHAT, "wxid_bob")
        self.assertEqual(len(tracker), 2)
        self.assertTrue(tracker.consume(self.CHAT, "wxid_bob"))
        self.assertEqual(len(tracker), 1)
        self.assertTrue(tracker.consume(self.CHAT, "wxid_alice"))

    def test_empty_ids_are_accepted_without_validation(self):
        # PINNED (known issue, expected to change in Phase N): unlike
        # FollowupListenTracker and ImageFollowupTracker, register() does not
        # reject empty chat/sender ids, so every message with a blank sender
        # id in a given chat shares one sticky slot.
        tracker = StickyMentionTracker()
        tracker.register("", "")
        self.assertEqual(len(tracker), 1)
        self.assertTrue(tracker.consume("", ""))

    def test_clear_removes_the_entry(self):
        tracker = StickyMentionTracker()
        tracker.register(self.CHAT, self.USER)
        tracker.clear(self.CHAT, self.USER)
        self.assertFalse(tracker.consume(self.CHAT, self.USER))
        self.assertEqual(len(tracker), 0)

    def test_clear_is_scoped_to_the_key(self):
        tracker = StickyMentionTracker()
        tracker.register(self.CHAT, "wxid_alice")
        tracker.register(self.CHAT, "wxid_bob")
        tracker.clear(self.CHAT, "wxid_alice")
        self.assertTrue(tracker.consume(self.CHAT, "wxid_bob"))

    def test_clear_unknown_key_is_a_noop(self):
        tracker = StickyMentionTracker()
        tracker.clear(self.CHAT, self.USER)       # must not raise
        self.assertEqual(len(tracker), 0)

    def test_len_and_repr(self):
        tracker = StickyMentionTracker()
        self.assertEqual(repr(tracker), "StickyMentionTracker(active=0, ttl=60s)")
        tracker.register(self.CHAT, self.USER)
        self.assertEqual(len(tracker), 1)
        self.assertEqual(repr(tracker), "StickyMentionTracker(active=1, ttl=60s)")
        self.assertEqual(
            repr(StickyMentionTracker(ttl_sec=15)),
            "StickyMentionTracker(active=0, ttl=15s)",
        )


class StickyTtlTests(ClockedTestCase):
    MODULE = sticky_module
    CHAT = "room@chatroom"
    USER = "wxid_alice"

    def test_default_ttl_is_sixty_seconds(self):
        tracker = StickyMentionTracker()
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(59)
        self.assertTrue(tracker.consume(self.CHAT, self.USER))

        tracker.register(self.CHAT, self.USER)
        self.clock.advance(60)
        self.assertFalse(tracker.consume(self.CHAT, self.USER))

    def test_custom_ttl(self):
        tracker = StickyMentionTracker(ttl_sec=10)
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(9)
        self.assertTrue(tracker.consume(self.CHAT, self.USER))

        tracker.register(self.CHAT, self.USER)
        self.clock.advance(10)
        self.assertFalse(tracker.consume(self.CHAT, self.USER))

    def test_consume_exactly_at_expiry_is_already_expired(self):
        # Boundary: remaining <= 0 counts as expired.
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(60)
        self.assertFalse(tracker.consume(self.CHAT, self.USER))

    def test_expired_consume_deletes_the_entry_and_its_counter(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, self.USER)
        tracker.register(self.CHAT, self.USER)       # re-reg count -> 1
        self.assertEqual(tracker._re_reg_count[(self.CHAT, self.USER)], 1)
        self.clock.advance(61)
        self.assertFalse(tracker.consume(self.CHAT, self.USER))
        self.assertEqual(len(tracker), 0)
        self.assertEqual(tracker._re_reg_count, {})

    def test_reregistration_extends_the_ttl_from_now(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(30)
        tracker.register(self.CHAT, self.USER)       # expiry moves to t0 + 90
        self.clock.advance(50)                       # t0 + 80: 20 s after t0's expiry
        self.assertTrue(tracker.consume(self.CHAT, self.USER))

    def test_consume_close_to_expiry_still_succeeds_but_warns(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(56)                       # 4 s remaining (< 5 s)
        with self.assertLogs("src.proactive.sticky", level="WARNING") as captured:
            self.assertTrue(tracker.consume(self.CHAT, self.USER))
        self.assertIn("nearly expired", "\n".join(captured.output))

    def test_consume_with_margin_does_not_warn(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(10)
        with self.assertNoLogs("src.proactive.sticky", level="WARNING"):
            self.assertTrue(tracker.consume(self.CHAT, self.USER))

    def test_warning_threshold_is_strictly_below_five_seconds_remaining(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(55)                       # exactly 5 s left: quiet
        with self.assertNoLogs("src.proactive.sticky", level="WARNING"):
            self.assertTrue(tracker.consume(self.CHAT, self.USER))

        tracker.register(self.CHAT, self.USER)
        self.clock.advance(55.5)                     # 4.5 s left: warns
        with self.assertLogs("src.proactive.sticky", level="WARNING"):
            self.assertTrue(tracker.consume(self.CHAT, self.USER))


class StickyReregistrationCapTests(ClockedTestCase):
    MODULE = sticky_module
    CHAT = "room@chatroom"
    USER = "wxid_alice"
    KEY = ("room@chatroom", "wxid_alice")

    def test_first_two_reregistrations_extend_and_the_third_is_capped(self):
        # Code behaviour (not the docstring's "after 3"): the counter is
        # incremented before the check, so the 3rd re-registration (4th call)
        # is the first one that is ignored.
        tracker = StickyMentionTracker(ttl_sec=60)
        t0 = self.clock.now

        tracker.register(self.CHAT, self.USER)       # new
        self.assertEqual(tracker._entries[self.KEY], t0 + 60)

        self.clock.advance(10)
        tracker.register(self.CHAT, self.USER)       # re-registration #1
        self.assertEqual(tracker._entries[self.KEY], t0 + 70)

        self.clock.advance(10)
        tracker.register(self.CHAT, self.USER)       # re-registration #2
        self.assertEqual(tracker._entries[self.KEY], t0 + 80)

        self.clock.advance(10)
        with self.assertLogs("src.proactive.sticky", level="WARNING") as captured:
            tracker.register(self.CHAT, self.USER)   # re-registration #3: capped
        self.assertEqual(tracker._entries[self.KEY], t0 + 80)
        self.assertIn("re-registrations without consume", "\n".join(captured.output))

    def test_capped_registration_does_not_extend_the_ttl(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        for _ in range(3):                           # new + 2 honoured
            tracker.register(self.CHAT, self.USER)
        self.clock.advance(50)
        tracker.register(self.CHAT, self.USER)       # capped: expiry stays t0 + 60
        self.clock.advance(11)                       # t0 + 61
        self.assertFalse(tracker.consume(self.CHAT, self.USER))

    def test_counter_keeps_counting_while_capped(self):
        # PINNED (known issue, expected to change in Phase N): the docstring
        # says the count is "stopped" once capped, but it keeps incrementing
        # on every further register() call.
        tracker = StickyMentionTracker()
        for _ in range(6):
            tracker.register(self.CHAT, self.USER)
        self.assertEqual(tracker._re_reg_count[self.KEY], 5)

    def test_consume_resets_the_cap(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        for _ in range(5):
            tracker.register(self.CHAT, self.USER)
        self.assertTrue(tracker.consume(self.CHAT, self.USER))
        self.assertNotIn(self.KEY, tracker._re_reg_count)

        t1 = self.clock.now
        tracker.register(self.CHAT, self.USER)       # fresh registration
        self.clock.advance(10)
        tracker.register(self.CHAT, self.USER)       # honoured again
        self.assertEqual(tracker._entries[self.KEY], t1 + 10 + 60)

    def test_clear_resets_the_cap(self):
        tracker = StickyMentionTracker()
        for _ in range(5):
            tracker.register(self.CHAT, self.USER)
        tracker.clear(self.CHAT, self.USER)
        self.assertNotIn(self.KEY, tracker._re_reg_count)

    def test_cap_is_tracked_per_key(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        for _ in range(5):
            tracker.register(self.CHAT, "wxid_alice")
        tracker.register(self.CHAT, "wxid_bob")
        self.clock.advance(10)
        tracker.register(self.CHAT, "wxid_bob")      # Bob's first re-registration
        self.assertEqual(
            tracker._entries[(self.CHAT, "wxid_bob")], self.clock.now + 60,
        )

    def test_expired_but_uncleaned_entry_counts_as_a_reregistration(self):
        # PINNED (known issue, expected to change in Phase N): register()
        # asks ``key in self._entries`` without checking the expiry, so an
        # expired entry that nobody has consumed or cleaned yet is treated as
        # a live one: the new registration is logged as "extended" and eats
        # one slot of the 3-re-registration cap.
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, self.USER)
        self.clock.advance(100)                      # entry is stale
        tracker.register(self.CHAT, self.USER)
        self.assertEqual(tracker._re_reg_count[self.KEY], 1)

    def test_spaced_out_registrations_get_capped_and_the_last_is_dropped(self):
        # PINNED (known issue, expected to change in Phase N): four bare
        # "@bot" messages, each more than ttl apart and with no other message
        # from that sender in between, hit the cap through *stale* entries.
        # The fourth register() is ignored, so the immediate consume() --
        # one second later -- fails although the user just re-registered.
        tracker = StickyMentionTracker(ttl_sec=60)
        for _ in range(4):
            tracker.register(self.CHAT, self.USER)
            self.clock.advance(100)
        self.clock.advance(-99)                      # 1 s after the 4th register
        self.assertFalse(tracker.consume(self.CHAT, self.USER))

    def test_burst_then_wait_then_register_is_dropped_by_the_stale_cap(self):
        # Same root cause, shorter path: four quick registrations cap the
        # entry; once it has expired (but is still stored) the next register()
        # is ignored too and the following consume() fails.
        tracker = StickyMentionTracker(ttl_sec=60)
        for _ in range(4):
            tracker.register(self.CHAT, self.USER)   # 4th is capped
        self.clock.advance(61)
        tracker.register(self.CHAT, self.USER)       # existed (stale) -> capped
        self.assertFalse(tracker.consume(self.CHAT, self.USER))


class StickyCleanupTests(ClockedTestCase):
    MODULE = sticky_module
    CHAT = "room@chatroom"

    def noop_calls(self, tracker: StickyMentionTracker, n: int) -> None:
        for _ in range(n):
            tracker.consume("nobody-room", "nobody")

    def test_len_includes_expired_entries_until_cleanup(self):
        # PINNED (known issue, expected to change in Phase N): __len__ is
        # documented as "number of active sticky entries" but it counts
        # expired entries until a consume() or the every-200th-call cleanup
        # removes them.
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, "wxid_alice")
        self.clock.advance(500)
        self.assertEqual(len(tracker), 1)

    def test_cleanup_runs_on_the_200th_register_or_consume(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        for user in ("wxid_a", "wxid_b", "wxid_c"):  # calls 1..3
            tracker.register(self.CHAT, user)
        self.clock.advance(500)
        self.noop_calls(tracker, 196)                # calls 4..199
        self.assertEqual(len(tracker), 3)
        self.noop_calls(tracker, 1)                  # call 200 -> _cleanup()
        self.assertEqual(len(tracker), 0)

    def test_cleanup_keeps_unexpired_entries(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, "wxid_old")
        self.clock.advance(100)
        tracker.register(self.CHAT, "wxid_new")      # expires at t0 + 160
        self.noop_calls(tracker, 198)                # calls 3..200
        self.assertEqual(len(tracker), 1)
        self.assertTrue(tracker.consume(self.CHAT, "wxid_new"))

    def test_cleanup_drops_reregistration_counters_of_expired_entries(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, "wxid_a")
        tracker.register(self.CHAT, "wxid_a")        # counter -> 1
        self.clock.advance(500)
        self.noop_calls(tracker, 198)                # calls 3..200
        self.assertEqual(len(tracker), 0)
        self.assertEqual(tracker._re_reg_count, {})

    def test_clear_len_and_repr_do_not_advance_the_cleanup_counter(self):
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, "wxid_a")        # call 1
        self.clock.advance(500)
        for _ in range(300):
            tracker.clear("x", "y")
            len(tracker)
            repr(tracker)
        self.assertEqual(len(tracker), 1)            # still not cleaned
        self.noop_calls(tracker, 198)                # calls 2..199
        self.assertEqual(len(tracker), 1)
        self.noop_calls(tracker, 1)                  # call 200
        self.assertEqual(len(tracker), 0)

    def test_cleanup_and_consume_disagree_on_the_expiry_boundary(self):
        # PINNED (known issue, expected to change in Phase N): consume()
        # treats ``remaining <= 0`` as expired while _cleanup() only removes
        # ``expiry < now``.  An entry expiring exactly now survives cleanup
        # yet can no longer be consumed.
        tracker = StickyMentionTracker(ttl_sec=60)
        tracker.register(self.CHAT, "wxid_a")        # call 1
        self.clock.advance(60)                       # now == expiry
        self.noop_calls(tracker, 199)                # calls 2..200 -> cleanup
        self.assertEqual(len(tracker), 1)
        self.assertFalse(tracker.consume(self.CHAT, "wxid_a"))
        self.assertEqual(len(tracker), 0)


class StickySharedStateTests(unittest.TestCase):
    def test_tracker_holds_a_lock(self):
        # router.handle() runs on up to 4 pool threads (WcdbBackend
        # ThreadPoolExecutor) and calls register()/consume() on one shared
        # tracker.  Fixed in Phase 5 (it used to be unsynchronised); see
        # tests/test_shared_state_locks.py for the concurrent use.
        self.assertTrue(holds_lock(StickyMentionTracker()))


# ═══════════════════════════════════════════════════════════════════════
# FollowupListenTracker
# ═══════════════════════════════════════════════════════════════════════


class FollowupTestCase(ClockedTestCase):
    MODULE = followup_module
    CHAT = "room@chatroom"

    def msg(self, content, *, chat_id=None, sender_id="wxid_other") -> dict:
        return {
            "chat_id": chat_id or self.CHAT,
            "sender_id": sender_id,
            "content": content,
        }

    def reason(self, bot_reply, content, *, tracker=None):
        """Register *bot_reply*, feed *content*; return the reason or None."""
        tracker = tracker or FollowupListenTracker()
        tracker.register(self.CHAT, bot_reply)
        result = tracker.match(self.msg(content))
        return None if result is None else result.reason


class FollowupRegisterAndExpiryTests(FollowupTestCase):
    def test_register_then_match_returns_the_bot_reply_and_a_reason(self):
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "你想听哪首？")
        result = tracker.match(self.msg("随便"))
        self.assertEqual(
            result, FollowupMatch(bot_reply="你想听哪首？", reason="answers-bot-question"),
        )

    def test_match_without_a_registered_reply_is_none(self):
        tracker = FollowupListenTracker()
        self.assertIsNone(tracker.match(self.msg("吗")))

    def test_match_with_missing_chat_id_is_none(self):
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "你想听哪首？")
        self.assertIsNone(tracker.match({"content": "吗"}))
        self.assertIsNone(tracker.match({"chat_id": None, "content": "吗"}))

    def test_match_with_missing_or_none_content_is_none(self):
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "你想听哪首？")
        self.assertIsNone(tracker.match({"chat_id": self.CHAT}))
        self.assertIsNone(tracker.match({"chat_id": self.CHAT, "content": None}))

    def test_register_ignores_empty_chat_or_reply(self):
        tracker = FollowupListenTracker()
        tracker.register("", "你想听哪首？")
        tracker.register(self.CHAT, "")
        tracker.register(self.CHAT, "   \n ")
        tracker.register(self.CHAT, None)
        self.assertEqual(tracker._entries, {})

    def test_register_cleans_the_stored_reply(self):
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "wxid_bot123: 你好\r\n世界   想听吗")
        result = tracker.match(self.msg("好的呀"))
        self.assertEqual(result.bot_reply, "你好 世界 想听吗")

    def test_latest_reply_replaces_the_previous_one(self):
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "第一句：想听哪首？")
        tracker.register(self.CHAT, "第二句：收到")
        # The question from the first reply no longer counts as "asked".
        self.assertIsNone(tracker.match(self.msg("周杰伦")))

    def test_replies_are_tracked_per_chat(self):
        tracker = FollowupListenTracker()
        tracker.register("chat-1", "你想听哪首？")
        self.assertIsNone(tracker.match(self.msg("随便", chat_id="chat-2")))
        self.assertEqual(
            tracker.match(self.msg("随便", chat_id="chat-1")).reason,
            "answers-bot-question",
        )

    def test_match_does_not_consume_the_entry(self):
        # PINNED (known issue, expected to change in Phase N): unlike
        # StickyMentionTracker, a successful match leaves the entry in place
        # until the TTL runs out (the router never calls clear()), so every
        # plausible short message in the chat keeps matching.
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "你想听哪首？")
        self.assertIsNotNone(tracker.match(self.msg("随便")))
        self.assertIsNotNone(tracker.match(self.msg("随便")))
        self.assertIsNotNone(tracker.match(self.msg("周杰伦")))

    def test_match_ignores_the_sender(self):
        # PINNED (known issue, expected to change in Phase N): the entry is
        # per chat, not per (chat, sender); anyone in the group can continue
        # the bot's last reply.  The bot's own id is not excluded either.
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "你想听哪首？")
        for sender in ("wxid_asker", "wxid_bystander", ""):
            result = tracker.match(self.msg("随便", sender_id=sender))
            self.assertIsNotNone(result, msg=repr(sender))

    def test_clear_removes_the_entry(self):
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "你想听哪首？")
        tracker.clear(self.CHAT)
        self.assertIsNone(tracker.match(self.msg("随便")))
        tracker.clear(self.CHAT)                     # unknown chat: no error

    def test_sender_prefix_is_stripped_from_incoming_content(self):
        self.assertEqual(
            self.reason("收到", "wxid_abc123: 真的吗"), "short-question",
        )

    def test_followup_match_is_a_plain_value_object(self):
        self.assertEqual(
            FollowupMatch(bot_reply="a", reason="b"),
            FollowupMatch(bot_reply="a", reason="b"),
        )


class FollowupTtlTests(FollowupTestCase):
    def test_entry_is_valid_until_just_before_ttl(self):
        tracker = FollowupListenTracker(ttl_sec=60)
        tracker.register(self.CHAT, "你想听哪首？")
        self.clock.advance(59)
        self.assertIsNotNone(tracker.match(self.msg("随便")))

    def test_entry_expires_exactly_at_ttl_and_is_removed(self):
        tracker = FollowupListenTracker(ttl_sec=60)
        tracker.register(self.CHAT, "你想听哪首？")
        self.clock.advance(60)
        self.assertIsNone(tracker.match(self.msg("随便")))
        self.assertEqual(tracker._entries, {})

    def test_register_again_refreshes_the_window(self):
        tracker = FollowupListenTracker(ttl_sec=60)
        tracker.register(self.CHAT, "你想听哪首？")
        self.clock.advance(50)
        tracker.register(self.CHAT, "你想听哪首？")
        self.clock.advance(50)
        self.assertIsNotNone(tracker.match(self.msg("随便")))

    def test_ttl_below_five_seconds_is_raised_to_five(self):
        tracker = FollowupListenTracker(ttl_sec=1)
        tracker.register(self.CHAT, "你想听哪首？")
        self.clock.advance(4)
        self.assertIsNotNone(tracker.match(self.msg("随便")))
        self.clock.advance(1)
        self.assertIsNone(tracker.match(self.msg("随便")))

    def test_ttl_zero_or_none_falls_back_to_sixty(self):
        # PINNED (known issue, expected to change in Phase N): ``ttl or 60``
        # makes 0/None mean "default", while a negative value is floored to 5.
        for value in (0, None):
            tracker = FollowupListenTracker(ttl_sec=value)
            tracker.register(self.CHAT, "你想听哪首？")
            self.clock.advance(59)
            self.assertIsNotNone(tracker.match(self.msg("随便")), msg=repr(value))
            self.clock.advance(1)
            self.assertIsNone(tracker.match(self.msg("随便")), msg=repr(value))

    def test_negative_ttl_is_floored_to_five(self):
        tracker = FollowupListenTracker(ttl_sec=-30)
        tracker.register(self.CHAT, "你想听哪首？")
        self.clock.advance(5)
        self.assertIsNone(tracker.match(self.msg("随便")))

    def test_defaults_are_sixty_seconds_and_thirty_characters(self):
        tracker = FollowupListenTracker()
        tracker.register(self.CHAT, "收到")
        self.assertIsNotNone(tracker.match(self.msg("？" * 30)))
        self.assertIsNone(tracker.match(self.msg("？" * 31)))
        self.clock.advance(60)
        self.assertIsNone(tracker.match(self.msg("？")))


class FollowupCandidateShapeTests(FollowupTestCase):
    """The cheap pre-filter applied before any reason is considered."""

    ASKING_REPLY = "你想听哪首？"          # would answer-match almost anything short

    def test_empty_and_whitespace_content_is_rejected(self):
        for content in ("", "   ", "\n\t "):
            self.assertIsNone(
                self.reason(self.ASKING_REPLY, content), msg=repr(content),
            )

    def test_length_limit_is_inclusive(self):
        self.assertEqual(self.reason("收到", "？" * 30), "short-question")
        self.assertIsNone(self.reason("收到", "？" * 31))

    def test_custom_length_limit(self):
        tracker = FollowupListenTracker(max_message_chars=5)
        self.assertEqual(self.reason("收到", "吗" * 5, tracker=tracker), "short-question")
        self.assertIsNone(self.reason("收到", "吗" * 6, tracker=tracker))

    def test_zero_or_none_length_limit_falls_back_to_thirty(self):
        for value in (0, None):
            tracker = FollowupListenTracker(max_message_chars=value)
            self.assertIsNotNone(
                self.reason("收到", "？" * 30, tracker=tracker), msg=repr(value),
            )
            self.assertIsNone(
                self.reason("收到", "？" * 31, tracker=tracker), msg=repr(value),
            )

    def test_negative_length_limit_is_floored_to_one(self):
        tracker = FollowupListenTracker(max_message_chars=-4)
        self.assertEqual(self.reason("收到", "？", tracker=tracker), "short-question")
        self.assertIsNone(self.reason("收到", "？？", tracker=tracker))

    def test_length_is_measured_after_cleaning(self):
        # wxid_ prefix stripped and runs of whitespace collapsed first.
        content = "wxid_abc123:   " + "？" * 30
        self.assertEqual(self.reason("收到", content), "short-question")
        self.assertEqual(self.reason("收到", "吗 \n\n 吗"), "short-question")

    def test_wxid_prefix_may_contain_underscores(self):
        # The router's own prefix regex does not allow "_" after "wxid_";
        # the follow-up tracker's does.
        content = "wxid_ab_cd: " + "？" * 30
        self.assertEqual(self.reason("收到", content), "short-question")

    def test_only_the_first_sender_prefix_is_stripped(self):
        content = "wxid_a: wxid_b: " + "？" * 25          # 8 + 25 = 33 after one strip
        self.assertIsNone(self.reason("收到", content))

    def test_only_wxid_style_sender_prefixes_are_stripped(self):
        # "alice123: " is kept, so it counts towards the length limit.
        content = "alice123: " + "？" * 25          # 10 + 25 = 35 > 30
        self.assertIsNone(self.reason("收到", content))
        content = "wxid_alice123: " + "？" * 25      # stripped -> 25
        self.assertEqual(self.reason("收到", content), "short-question")

    def test_media_only_placeholders_are_rejected(self):
        for content in (
            "[表情]", "[图片]", "[语音]", "[视频]", "[文件]", "[位置]", "[动画表情]",
        ):
            self.assertIsNone(
                self.reason(self.ASKING_REPLY, content), msg=content,
            )

    def test_media_filter_is_a_closed_list_matched_in_full(self):
        # Control: these are NOT media-only, so they reach the reason rules.
        self.assertEqual(self.reason(self.ASKING_REPLY, "[链接]"), "answers-bot-question")
        self.assertEqual(self.reason(self.ASKING_REPLY, "[表情]x"), "answers-bot-question")
        self.assertEqual(self.reason(self.ASKING_REPLY, "表情"), "answers-bot-question")
        self.assertEqual(self.reason("收到", "[表情]吗"), "short-question")

    def test_ack_only_messages_are_rejected_case_insensitively(self):
        for content in (
            "嗯", "哦", "噢", "好", "好的", "没事", "谢谢", "谢了", "ok", "OK", "Ok",
        ):
            self.assertIsNone(
                self.reason(self.ASKING_REPLY, content), msg=content,
            )

    def test_near_miss_acks_are_not_filtered(self):
        # PINNED (known issue, expected to change in Phase N): the ack filter
        # is an exact-match set; punctuation or repetition slips through and
        # is then treated as an "answer" to the bot's question.
        for content in ("好的。", "嗯嗯", "ok啦", "谢谢你"):
            self.assertEqual(
                self.reason(self.ASKING_REPLY, content), "answers-bot-question",
                msg=content,
            )


class FollowupReasonTests(FollowupTestCase):
    TAROT_REPLY = "今天抽到的塔罗牌是《命运之轮》"

    # -- tarot-followup ----------------------------------------------------

    def test_tarot_keywords_after_a_tarot_reply(self):
        for content in (
            "我也要", "也给我来一张", "也帮我", "测测", "算算", "看看", "财运怎么样",
            "感情", "事业", "考试", "钱", "桃花", "运势",
        ):
            self.assertEqual(
                self.reason(self.TAROT_REPLY, content), "tarot-followup",
                msg=content,
            )

    def test_tarot_rule_needs_tarot_in_the_bot_reply(self):
        self.assertIsNone(self.reason("今天抽到的是《命运之轮》", "我也要"))

    def test_tarot_rule_needs_a_tarot_keyword_in_the_message(self):
        self.assertIsNone(self.reason(self.TAROT_REPLY, "好准啊"))

    def test_single_character_tarot_keywords_are_very_broad(self):
        # PINNED (known issue, expected to change in Phase N): "看", "算",
        # "测", "钱" match as plain substrings, so unrelated chatter after a
        # tarot reply is classified as a tarot follow-up.
        self.assertEqual(self.reason(self.TAROT_REPLY, "我看你在算账"), "tarot-followup")
        self.assertEqual(self.reason(self.TAROT_REPLY, "没钱了"), "tarot-followup")

    # -- shares-bot-reply-token --------------------------------------------

    def test_quoted_titles_become_tokens_in_every_supported_quote_style(self):
        for bot_reply, content in (
            ("推荐你听《晴天》", "晴天不错"),
            ("来一首「七里香」", "七里香呢"),
            ("她说“夜曲”不错", "夜曲呢"),
            ('试试"稻香"这首', "稻香"),
            ("听听'彩虹'吧", "彩虹"),
        ):
            self.assertEqual(
                self.reason(bot_reply, content), "shares-bot-reply-token",
                msg=bot_reply,
            )

    def test_ascii_words_of_three_or_more_characters_become_tokens(self):
        self.assertEqual(
            self.reason("Try Radiohead sometime", "radiohead"), "shares-bot-reply-token",
        )
        self.assertEqual(
            self.reason("Try Radiohead sometime", "RADIOHEAD好听"), "shares-bot-reply-token",
        )

    def test_two_letter_ascii_words_are_not_tokens(self):
        self.assertIsNone(self.reason("Try ok", "ok吧"))

    def test_stop_words_are_ignored_as_tokens(self):
        self.assertIsNone(self.reason("Listen with you", "with you"))
        self.assertEqual(
            self.reason("Listen with you", "listen up"), "shares-bot-reply-token",
        )

    def test_title_with_mixed_scripts_matches_on_either_part(self):
        self.assertEqual(
            self.reason("听听《Jay晴天》", "jay"), "shares-bot-reply-token",
        )
        self.assertEqual(
            self.reason("听听《Jay晴天》", "jay晴天"), "shares-bot-reply-token",
        )

    def test_unquoted_cjk_words_are_not_tokens(self):
        # Falls through to the "answers a question" rule instead ("推荐").
        self.assertEqual(
            self.reason("推荐你听晴天", "晴天"), "answers-bot-question",
        )
        self.assertIsNone(self.reason("听听晴天吧", "晴天"))

    def test_single_character_titles_are_not_tokens(self):
        self.assertIsNone(self.reason("《晴》", "晴"))

    def test_token_matching_is_a_plain_substring_test(self):
        # PINNED (known issue, expected to change in Phase N): no word
        # boundaries -- the 3-letter token "art" matches inside "partyyy".
        self.assertEqual(
            self.reason("I like art", "partyyy"), "shares-bot-reply-token",
        )

    # -- short-question ----------------------------------------------------

    def test_question_markers_trigger_short_question(self):
        for content in (
            "？", "?", "吗", "嘛", "么", "真的吗", "是嘛", "什么", "啥", "哪个",
            "哪首", "哪一个", "是不是", "咋整", "怎么办", "啥意思", "什么意思",
            "who?", "这是谁？",
        ):
            self.assertEqual(
                self.reason("收到", content), "short-question", msg=content,
            )

    def test_statements_without_markers_do_not_match(self):
        self.assertIsNone(self.reason("收到", "今天天气不错"))
        self.assertIsNone(self.reason("收到", "哈哈哈"))

    def test_single_character_question_marker_is_a_substring_test(self):
        # PINNED (known issue, expected to change in Phase N): "么" alone is a
        # marker, so non-questions such as "那么好" or "多么" match.
        self.assertEqual(self.reason("收到", "那么好"), "short-question")
        self.assertEqual(self.reason("收到", "多么好看"), "short-question")

    # -- answers-bot-question ----------------------------------------------

    def test_short_message_after_a_bot_question_is_an_answer(self):
        for content in ("随便", "都可以", "周杰伦", "英文", "不用", "一二三四五六七八"):
            self.assertEqual(
                self.reason("你想听哪首？", content), "answers-bot-question",
                msg=content,
            )

    def test_answer_length_limit_is_eight_characters(self):
        self.assertEqual(
            self.reason("你想听哪首？", "一二三四五六七八"), "answers-bot-question",
        )
        self.assertIsNone(self.reason("你想听哪首？", "一二三四五六七八九"))

    def test_short_message_after_a_statement_is_not_an_answer(self):
        self.assertIsNone(self.reason("收到", "周杰伦"))

    def test_each_asking_marker_in_the_bot_reply_enables_answers(self):
        for bot_reply in (
            "好吗", "是这个？", "是这个?", "哪边", "什么歌", "你喜欢周杰伦",
            "要不要来一首", "可以吗", "好不好", "想听一首", "想要什么", "要哪个",
            "要哪一个", "推荐一首给你",
        ):
            self.assertEqual(
                self.reason(bot_reply, "周杰伦"), "answers-bot-question",
                msg=bot_reply,
            )

    def test_statements_containing_asking_words_count_as_questions(self):
        # PINNED (known issue, expected to change in Phase N): "喜欢" and
        # "推荐" are asking markers even in plain statements.
        self.assertEqual(
            self.reason("我喜欢这个", "哈哈哈"), "answers-bot-question",
        )
        self.assertEqual(
            self.reason("给你推荐的歌单", "哈哈哈"), "answers-bot-question",
        )

    def test_short_answer_marker_set_is_redundant_with_the_length_rule(self):
        # PINNED (known issue, expected to change in Phase N): every entry of
        # _SHORT_ANSWER_MARKERS already satisfies ``1 <= len <= 8`` and is not
        # an ack, so the set can never change the outcome (dead data).
        for marker in followup_module._SHORT_ANSWER_MARKERS:
            self.assertTrue(1 <= len(marker) <= 8, msg=marker)
            self.assertNotIn(marker, followup_module._ACK_ONLY)

    # -- priority & no match -----------------------------------------------

    def test_rule_priority_is_tarot_then_token_then_question_then_answer(self):
        reply = "塔罗《命运之轮》你想问什么？"
        self.assertEqual(self.reason(reply, "命运之轮的感情吗"), "tarot-followup")
        self.assertEqual(self.reason(reply, "命运之轮吗"), "shares-bot-reply-token")
        self.assertEqual(self.reason(reply, "真的吗"), "short-question")
        self.assertEqual(self.reason(reply, "周杰伦"), "answers-bot-question")

    def test_message_matching_no_rule_returns_none(self):
        self.assertIsNone(self.reason("你想听哪首？", "我觉得这件事情其实不复杂"))
        self.assertIsNone(self.reason("收到", "今天去了趟超市买了点东西"))


class FollowupCleanupTests(FollowupTestCase):
    def noop_calls(self, tracker: FollowupListenTracker, n: int) -> None:
        for _ in range(n):
            tracker.match({})

    def test_cleanup_runs_on_the_200th_register_or_match(self):
        tracker = FollowupListenTracker(ttl_sec=60)
        for chat in ("c1", "c2", "c3"):              # calls 1..3
            tracker.register(chat, "你想听哪首？")
        self.clock.advance(500)
        self.noop_calls(tracker, 196)                # calls 4..199
        self.assertEqual(len(tracker._entries), 3)
        self.noop_calls(tracker, 1)                  # call 200 -> cleanup
        self.assertEqual(len(tracker._entries), 0)

    def test_cleanup_keeps_unexpired_entries(self):
        tracker = FollowupListenTracker(ttl_sec=60)
        tracker.register("old", "你想听哪首？")       # call 1
        self.clock.advance(100)
        tracker.register("new", "你想听哪首？")       # call 2, expires t0 + 160
        self.noop_calls(tracker, 198)                # calls 3..200
        self.assertEqual(set(tracker._entries), {"new"})

    def test_clear_does_not_advance_the_cleanup_counter(self):
        tracker = FollowupListenTracker(ttl_sec=60)
        tracker.register("c1", "你想听哪首？")        # call 1
        self.clock.advance(500)
        for _ in range(300):
            tracker.clear("unknown")
        self.noop_calls(tracker, 198)                # calls 2..199
        self.assertEqual(len(tracker._entries), 1)
        self.noop_calls(tracker, 1)                  # call 200
        self.assertEqual(len(tracker._entries), 0)

    def test_invalid_register_calls_still_advance_the_cleanup_counter(self):
        tracker = FollowupListenTracker(ttl_sec=60)
        tracker.register("c1", "你想听哪首？")        # call 1
        self.clock.advance(500)
        for _ in range(198):
            tracker.register("", "")                 # calls 2..199, all ignored
        self.assertEqual(len(tracker._entries), 1)
        tracker.register("", "")                     # call 200 -> cleanup
        self.assertEqual(len(tracker._entries), 0)

    def test_cleanup_and_match_disagree_on_the_expiry_boundary(self):
        # PINNED (known issue, expected to change in Phase N): match() treats
        # ``remaining <= 0`` as expired, the cleanup only drops ``expiry <
        # now``; same inconsistency as StickyMentionTracker.
        tracker = FollowupListenTracker(ttl_sec=60)
        tracker.register(self.CHAT, "你想听哪首？")   # call 1
        self.clock.advance(60)                       # now == expiry
        self.noop_calls(tracker, 199)                # calls 2..200 -> cleanup
        self.assertEqual(len(tracker._entries), 1)
        self.assertIsNone(tracker.match(self.msg("随便")))
        self.assertEqual(len(tracker._entries), 0)


class FollowupSharedStateTests(unittest.TestCase):
    def test_tracker_holds_a_lock(self):
        # Shared by the message-pool worker threads.  Fixed in Phase 5.
        self.assertTrue(holds_lock(FollowupListenTracker()))


# ═══════════════════════════════════════════════════════════════════════
# ImageFollowupTracker (complements tests/test_image_followup.py)
# ═══════════════════════════════════════════════════════════════════════


class ImageFollowupExtraTests(ClockedTestCase):
    MODULE = image_followup_module

    def image_msg(self, chat_id="chat", sender_id="sender", msg_type=47) -> dict:
        return {"chat_id": chat_id, "sender_id": sender_id, "msg_type": msg_type}

    def test_consume_returns_the_registered_reply(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "sender", "  刚才那句  ")
        self.assertEqual(
            tracker.consume(self.image_msg()), ImageFollowupMatch(bot_reply="刚才那句"),
        )

    def test_register_ignores_missing_chat_sender_or_reply(self):
        tracker = ImageFollowupTracker()
        tracker.register("", "sender", "reply")
        tracker.register("chat", "", "reply")
        tracker.register("chat", "sender", "")
        tracker.register("chat", "sender", "   ")
        tracker.register("chat", "sender", None)
        self.assertEqual(tracker._entries, {})

    def test_consume_is_one_shot(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "sender", "reply")
        self.assertIsNotNone(tracker.consume(self.image_msg()))
        self.assertIsNone(tracker.consume(self.image_msg()))

    def test_only_image_and_sticker_types_consume(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "sender", "reply")
        for msg_type in (1, 34, 43, 49, 10000):      # text, voice, video, link, system
            self.assertIsNone(
                tracker.consume(self.image_msg(msg_type=msg_type)), msg=msg_type,
            )
        # Non-image messages left the entry untouched.
        self.assertIsNotNone(tracker.consume(self.image_msg(msg_type=3)))

        tracker.register("chat", "sender", "reply")
        self.assertIsNotNone(tracker.consume(self.image_msg(msg_type=47)))

    def test_msg_type_coercion(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "sender", "reply")
        no_type = {"chat_id": "chat", "sender_id": "sender"}
        for message in (
            no_type,
            {**no_type, "msg_type": None},
            {**no_type, "msg_type": 0},          # ``0 or 1`` -> text
            {**no_type, "msg_type": "abc"},
            {**no_type, "msg_type": []},
        ):
            self.assertIsNone(tracker.consume(message), msg=repr(message))
        self.assertIsNotNone(tracker.consume({**no_type, "msg_type": "47"}))

    def test_entries_are_per_chat_and_sender(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "alice", "to alice")
        self.assertIsNone(tracker.consume(self.image_msg(sender_id="bob")))
        self.assertIsNone(tracker.consume(self.image_msg(chat_id="other", sender_id="alice")))
        self.assertEqual(
            tracker.consume(self.image_msg(sender_id="alice")).bot_reply, "to alice",
        )

    def test_ids_are_coerced_to_strings(self):
        tracker = ImageFollowupTracker()
        tracker.register(123, 456, "reply")
        match = tracker.consume({"chat_id": "123", "sender_id": "456", "msg_type": 3})
        self.assertEqual(match.bot_reply, "reply")

    def test_register_replaces_and_refreshes(self):
        tracker = ImageFollowupTracker(ttl_sec=90)
        tracker.register("chat", "sender", "first")
        self.clock.advance(60)
        tracker.register("chat", "sender", "second")
        self.clock.advance(60)                       # 120 s after "first"
        self.assertEqual(tracker.consume(self.image_msg()).bot_reply, "second")

    def test_entry_is_still_valid_exactly_at_expiry(self):
        # PINNED (known issue, expected to change in Phase N): the expiry
        # check is ``expiry < now`` (valid on the boundary), whereas
        # StickyMentionTracker and FollowupListenTracker treat the boundary
        # as already expired (``remaining <= 0``).
        tracker = ImageFollowupTracker(ttl_sec=90)
        tracker.register("chat", "sender", "reply")
        self.clock.advance(90)
        self.assertIsNotNone(tracker.consume(self.image_msg()))

    def test_entry_expires_after_ttl_and_is_removed(self):
        tracker = ImageFollowupTracker(ttl_sec=90)
        tracker.register("chat", "sender", "reply")
        self.clock.advance(90.001)
        self.assertIsNone(tracker.consume(self.image_msg()))
        self.assertEqual(tracker._entries, {})

    def test_expired_entry_is_popped_before_the_expiry_check(self):
        # An expired entry is removed by the consume() that finds it stale,
        # so a second attempt cannot resurrect it even with a fresh clock.
        tracker = ImageFollowupTracker(ttl_sec=90)
        tracker.register("chat", "sender", "reply")
        self.clock.advance(1000)
        self.assertIsNone(tracker.consume(self.image_msg()))
        self.clock.advance(-1000)
        self.assertIsNone(tracker.consume(self.image_msg()))

    def test_ttl_floor_and_default(self):
        low = ImageFollowupTracker(ttl_sec=1)        # floored to 15
        low.register("chat", "sender", "reply")
        self.clock.advance(15)
        self.assertIsNotNone(low.consume(self.image_msg()))

        default = ImageFollowupTracker(ttl_sec=0)    # ``0 or 90`` -> 90
        default.register("chat", "sender", "reply")
        self.clock.advance(90)
        self.assertIsNotNone(default.consume(self.image_msg()))
        default.register("chat", "sender", "reply")
        self.clock.advance(90.001)
        self.assertIsNone(default.consume(self.image_msg()))

    def test_clear_with_the_same_timestamp_closes_the_window(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "sender", "reply", source_timestamp=200)
        tracker.clear("chat", "sender", message_timestamp=200)
        self.assertIsNone(tracker.consume(self.image_msg()))

    def test_clear_without_timestamp_always_closes_the_window(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "sender", "reply", source_timestamp=200)
        tracker.clear("chat", "sender")
        self.assertIsNone(tracker.consume(self.image_msg()))

    def test_clear_when_the_source_timestamp_is_unknown_always_closes(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "sender", "reply")                   # source 0
        tracker.clear("chat", "sender", message_timestamp=1)
        self.assertIsNone(tracker.consume(self.image_msg()))

        tracker.register("chat", "sender", "reply", source_timestamp=-5)
        tracker.clear("chat", "sender", message_timestamp=1)          # clamped to 0
        self.assertIsNone(tracker.consume(self.image_msg()))

    def test_clear_is_scoped_to_chat_and_sender(self):
        tracker = ImageFollowupTracker()
        tracker.register("chat", "alice", "reply")
        tracker.clear("chat", "bob")
        tracker.clear("other", "alice")
        self.assertIsNotNone(tracker.consume(self.image_msg(sender_id="alice")))

    def test_clear_unknown_entry_is_a_noop(self):
        ImageFollowupTracker().clear("chat", "sender", message_timestamp=5)

    def test_tracker_holds_a_lock_unlike_the_other_trackers(self):
        self.assertTrue(holds_lock(ImageFollowupTracker()))


if __name__ == "__main__":
    unittest.main()
