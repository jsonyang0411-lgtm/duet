"""Characterization tests for ProactiveGate (src/proactive/gate.py).

Phase 0 safety net: pins the CURRENT behaviour of the proactive-speaking
gate -- every gate in order, the exact reason strings, the silence backoff and
the periodic ``_last_eval`` cleanup -- so a later refactor shows up as an
intentional diff.  Quirks are marked
``# PINNED (known issue, expected to change in Phase N): <why>``.

Determinism: the ``time`` reference of gate.py / rate_tracker.py and the
``random`` reference of gate.py are replaced by in-test fakes (the real
modules are never touched), and the module-level mode cache of
src/proactive/modes.py is reset before and after every test.  Nothing
sleeps, no threads are started and nothing is written to disk.
"""

import copy
import threading
import unittest
from unittest.mock import patch

import pytest

import src.proactive.gate as gate_module
import src.proactive.rate_tracker as rate_tracker_module
from src.config import BotConfig
from src.proactive.gate import ProactiveGate
from src.proactive.modes import ProactiveMode, get_modes, reset_modes


@pytest.fixture(autouse=True)
def _reset_mode_cache_around_every_test():
    reset_modes()
    yield
    reset_modes()


class FakeClock:
    """Minimal stand-in for the ``time`` module (only ``time()`` is used)."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRandom:
    """Stand-in for the ``random`` module (only ``random()`` is used).

    ``value`` must be set by tests that expect the dice to be rolled; an
    unexpected roll fails loudly instead of returning something plausible.
    """

    def __init__(self):
        self.value = None
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        if self.value is None:
            raise AssertionError("random.random() was consulted unexpectedly")
        return self.value


_LOCK_TYPES = (type(threading.Lock()), type(threading.RLock()))


def holds_lock(obj) -> bool:
    """True when any instance attribute of *obj* is a threading lock."""
    return any(isinstance(v, _LOCK_TYPES) for v in vars(obj).values())


class GateTestCase(unittest.TestCase):
    """Base class: fake clock + fake dice + empty mode cache + enabled gate."""

    CHAT = "room-1@chatroom"

    def setUp(self):
        reset_modes()
        self.addCleanup(reset_modes)

        self.clock = FakeClock()
        self.rng = FakeRandom()
        for module, attr, replacement in (
            (gate_module, "time", self.clock),
            (rate_tracker_module, "time", self.clock),
            (gate_module, "random", self.rng),
        ):
            patcher = patch.object(module, attr, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.config = BotConfig(proactive_enabled=True)
        self.gate = ProactiveGate(self.config)

    # -- helpers -----------------------------------------------------------

    def msg(self, chat_id=None) -> dict:
        return {
            "chat_id": chat_id or self.CHAT,
            "sender_id": "wxid_someone",
            "content": "hello",
        }

    def prime(self, n: int, chat_id=None, gate=None) -> None:
        """Record *n* arrivals on the rate tracker without evaluating any gate."""
        gate = gate or self.gate
        for _ in range(n):
            gate._tracker.record(chat_id or self.CHAT)

    def deliver(self, arrivals: int, chat_id=None, gate=None):
        """Run one message through ``should_speak``.

        The rate window is reset first so that exactly *arrivals* messages
        (this one included) sit in it -- the rate seen by the gate is then
        independent of whatever happened earlier in the test.
        """
        gate = gate or self.gate
        chat_id = chat_id or self.CHAT
        gate._tracker.clear(chat_id)
        self.prime(arrivals - 1, chat_id, gate)
        return gate.should_speak(self.msg(chat_id))


# With the default 120 s window: arrivals / 2 = msgs per minute.
QUIET_ARRIVALS = 3     # 1.5/min  (exactly the QUIET threshold)
CASUAL_ARRIVALS = 8    # 4.0/min
LIVELY_ARRIVALS = 13   # 6.5/min
BURST_ARRIVALS = 17    # 8.5/min


class GateOrderTests(GateTestCase):
    """The gates, in the order the code evaluates them."""

    # -- gate 0: chat_id ---------------------------------------------------

    def test_missing_chat_id_is_rejected_before_anything_else(self):
        for message in ({}, {"chat_id": ""}, {"chat_id": None}):
            self.assertEqual(
                self.gate.should_speak(message), (False, None, "no chat_id"),
                msg=repr(message),
            )
        # Nothing was recorded, counted or rolled.
        self.assertEqual(self.gate._call_count, 0)
        self.assertEqual(len(self.gate._tracker._buckets), 0)
        self.assertEqual(self.rng.calls, 0)

    def test_missing_chat_id_wins_over_disabled(self):
        self.config.proactive_enabled = False
        self.assertEqual(self.gate.should_speak({}), (False, None, "no chat_id"))

    # -- gate 1: master switch --------------------------------------------

    def test_disabled_reason(self):
        self.config.proactive_enabled = False
        self.assertEqual(
            self.gate.should_speak(self.msg()), (False, None, "disabled"),
        )

    def test_default_config_is_disabled(self):
        gate = ProactiveGate(BotConfig())
        self.assertEqual(gate.should_speak(self.msg()), (False, None, "disabled"))

    def test_disabled_wins_over_a_burst_rate_and_never_rolls(self):
        self.config.proactive_enabled = False
        self.prime(40)
        self.assertEqual(
            self.gate.should_speak(self.msg()), (False, None, "disabled"),
        )
        self.assertEqual(self.rng.calls, 0)

    def test_enabled_flag_is_read_on_every_call(self):
        self.config.proactive_enabled = False
        self.assertEqual(self.gate.should_speak(self.msg())[2], "disabled")
        self.config.proactive_enabled = True
        self.assertEqual(self.gate.should_speak(self.msg())[2], "rate 1.0/min → SLEEP")
        self.config.proactive_enabled = False
        self.assertEqual(self.gate.should_speak(self.msg())[2], "disabled")

    def test_arrivals_are_recorded_even_while_disabled(self):
        # Arrivals seen while disabled count towards the rate the moment the
        # switch is flipped: 7 recorded + the 8th = 4.0/min = CASUAL.
        self.config.proactive_enabled = False
        for _ in range(7):
            self.gate.should_speak(self.msg())
        self.assertEqual(len(self.gate._tracker._buckets[self.CHAT]), 7)

        self.config.proactive_enabled = True
        self.rng.value = 0.0
        allowed, mode, reason = self.gate.should_speak(self.msg())
        self.assertTrue(allowed)
        self.assertEqual(mode.name, "CASUAL")
        self.assertEqual(reason, "mode=CASUAL rate=4.0/min")

    def test_disabled_gate_never_prunes_recorded_arrivals(self):
        # PINNED (known issue, expected to change in Phase N): the default
        # config has proactive_enabled=False, yet should_speak() still records
        # every non-@ group message and returns before rate() could prune, so
        # the per-chat deque grows without bound for the life of the process.
        self.config.proactive_enabled = False
        for _ in range(300):
            self.gate.should_speak(self.msg())
            self.clock.advance(60)           # 5 h in total; window is 2 min
        self.assertEqual(len(self.gate._tracker._buckets[self.CHAT]), 300)

    # -- gate 2: message rate / SLEEP -------------------------------------

    def test_low_rate_is_blocked_as_sleep(self):
        first = self.gate.should_speak(self.msg())
        second = self.gate.should_speak(self.msg())
        self.assertEqual(first, (False, None, "rate 0.5/min → SLEEP"))
        self.assertEqual(second, (False, None, "rate 1.0/min → SLEEP"))

    def test_sleep_neither_rolls_the_dice_nor_touches_last_eval(self):
        self.gate.should_speak(self.msg())
        self.assertEqual(self.rng.calls, 0)
        self.assertEqual(self.gate._last_eval, {})

    def test_sleep_reason_formats_the_rate_with_one_decimal(self):
        gate = ProactiveGate(
            BotConfig(proactive_enabled=True, proactive_rate_window_sec=90),
        )
        # 1 / (90 s / 60) = 0.666...
        self.assertEqual(
            gate.should_speak(self.msg()), (False, None, "rate 0.7/min → SLEEP"),
        )

    def test_exactly_the_quiet_threshold_leaves_sleep(self):
        self.rng.value = 0.0
        self.gate.should_speak(self.msg())
        self.gate.should_speak(self.msg())
        allowed, mode, reason = self.gate.should_speak(self.msg())
        self.assertEqual(
            (allowed, mode.name, reason),
            (True, "QUIET", "mode=QUIET rate=1.5/min"),
        )

    def test_arrivals_older_than_the_window_stop_counting(self):
        self.gate.should_speak(self.msg())
        self.gate.should_speak(self.msg())
        self.clock.advance(121)
        self.assertEqual(
            self.gate.should_speak(self.msg()), (False, None, "rate 0.5/min → SLEEP"),
        )

    def test_rate_is_tracked_per_chat(self):
        self.prime(10, chat_id="busy")
        self.assertEqual(
            self.gate.should_speak(self.msg("quiet")),
            (False, None, "rate 0.5/min → SLEEP"),
        )


class EvalIntervalTests(GateTestCase):
    """Gate 3: per-mode evaluation interval with consecutive-silence backoff."""

    def setUp(self):
        super().setUp()
        self.rng.value = 0.0               # every roll passes the dice

    def test_first_evaluation_is_not_delayed_and_stamps_last_eval(self):
        allowed, mode, reason = self.deliver(QUIET_ARRIVALS)
        self.assertTrue(allowed)
        self.assertEqual(self.gate._last_eval[self.CHAT], self.clock.now)

    def test_second_evaluation_inside_the_interval_is_blocked(self):
        self.deliver(QUIET_ARRIVALS)
        self.clock.advance(10)
        self.assertEqual(
            self.deliver(QUIET_ARRIVALS),
            (False, None, "eval interval (10s < 300s, backoff=1x)"),
        )
        self.assertEqual(self.rng.calls, 1)  # dice only rolled for the first

    def test_interval_boundary_passes_when_elapsed_equals_interval(self):
        # elapsed < interval blocks; elapsed == interval proceeds.
        self.deliver(QUIET_ARRIVALS)
        self.clock.advance(299)
        self.assertEqual(
            self.deliver(QUIET_ARRIVALS),
            (False, None, "eval interval (299s < 300s, backoff=1x)"),
        )
        self.clock.advance(1)
        allowed, mode, reason = self.deliver(QUIET_ARRIVALS)
        self.assertTrue(allowed)
        self.assertEqual(reason, "mode=QUIET rate=1.5/min")

    def test_blocked_attempts_do_not_restart_the_interval(self):
        self.deliver(QUIET_ARRIVALS)
        stamped = self.gate._last_eval[self.CHAT]
        for _ in range(5):
            self.clock.advance(10)
            self.assertFalse(self.deliver(QUIET_ARRIVALS)[0])
        self.assertEqual(self.gate._last_eval[self.CHAT], stamped)

    def test_backoff_doubles_per_consecutive_silence_and_caps_at_16x(self):
        for silences, backoff in [
            (0, 1), (1, 2), (2, 4), (3, 8), (4, 16), (5, 16), (6, 16),
            (12, 16), (40, 16),
        ]:
            gate = ProactiveGate(self.config)
            effective = 300 * backoff       # QUIET base interval is 300 s
            for _ in range(silences):
                gate.record_silence(self.CHAT)
            gate.record_eval(self.CHAT)

            self.clock.advance(effective - 1)
            self.assertEqual(
                self.deliver(QUIET_ARRIVALS, gate=gate),
                (
                    False, None,
                    f"eval interval ({effective - 1}s < {effective}s, backoff={backoff}x)",
                ),
                msg=f"silences={silences}",
            )
            self.clock.advance(1)
            allowed, mode, reason = self.deliver(QUIET_ARRIVALS, gate=gate)
            self.assertTrue(allowed, msg=f"silences={silences}")
            self.assertEqual(mode.name, "QUIET")

    def test_silence_counter_itself_is_not_capped(self):
        for _ in range(40):
            self.gate.record_silence(self.CHAT)
        self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), 40)

    def test_backoff_multiplies_the_current_modes_interval(self):
        # BURST base interval is 30 s -> one silence makes it 60 s.
        self.deliver(BURST_ARRIVALS)
        self.gate.record_eval(self.CHAT)
        self.gate.record_silence(self.CHAT)
        self.clock.advance(59)
        self.assertEqual(
            self.deliver(BURST_ARRIVALS),
            (False, None, "eval interval (59s < 60s, backoff=2x)"),
        )
        self.clock.advance(1)
        self.assertTrue(self.deliver(BURST_ARRIVALS)[0])

    def test_interval_is_taken_from_the_current_mode_not_the_last_one(self):
        # Evaluated as QUIET (300 s); 31 s later the chat is BURST (30 s), so
        # the old evaluation no longer blocks.
        self.deliver(QUIET_ARRIVALS)
        self.clock.advance(31)
        allowed, mode, reason = self.deliver(BURST_ARRIVALS)
        self.assertTrue(allowed)
        self.assertEqual(mode.name, "BURST")

    def test_cooling_down_to_a_slower_mode_lengthens_the_wait(self):
        self.deliver(BURST_ARRIVALS)
        self.clock.advance(31)
        self.assertEqual(
            self.deliver(QUIET_ARRIVALS),
            (False, None, "eval interval (31s < 300s, backoff=1x)"),
        )

    def test_interval_state_is_per_chat(self):
        self.assertTrue(self.deliver(QUIET_ARRIVALS, chat_id="a")[0])
        self.assertTrue(self.deliver(QUIET_ARRIVALS, chat_id="b")[0])
        self.assertFalse(self.deliver(QUIET_ARRIVALS, chat_id="a")[0])

    def test_never_evaluated_chat_relies_on_an_epoch_zero_sentinel(self):
        # PINNED (known issue, expected to change in Phase N): "never
        # evaluated" is encoded as last_eval=0, i.e. elapsed = now - 0.  It
        # only works because time.time() is ~1.7e9; with a clock below the
        # interval the very first evaluation is blocked.
        self.clock.now = 100.0
        self.assertEqual(
            self.deliver(QUIET_ARRIVALS),
            (False, None, "eval interval (100s < 300s, backoff=1x)"),
        )


class ProbabilityGateTests(GateTestCase):
    """Gate 4: the dice roll against the mode's reply probability."""

    def test_roll_above_probability_is_a_miss(self):
        self.rng.value = 0.5
        self.assertEqual(
            self.deliver(QUIET_ARRIVALS),
            (False, None, "probability miss (0.50 > 0.1)"),
        )

    def test_roll_equal_to_probability_passes(self):
        # ``roll > probability`` is the miss condition, so equality passes.
        self.rng.value = 0.1
        allowed, mode, reason = self.deliver(QUIET_ARRIVALS)
        self.assertTrue(allowed)
        self.assertEqual(mode.name, "QUIET")

    def test_miss_reason_uses_each_modes_probability(self):
        for arrivals, roll, expected in [
            (QUIET_ARRIVALS, 0.11, "probability miss (0.11 > 0.1)"),
            (CASUAL_ARRIVALS, 0.26, "probability miss (0.26 > 0.25)"),
            (LIVELY_ARRIVALS, 0.51, "probability miss (0.51 > 0.5)"),
            (BURST_ARRIVALS, 0.71, "probability miss (0.71 > 0.7)"),
        ]:
            gate = ProactiveGate(self.config)
            self.rng.value = roll
            self.assertEqual(
                self.deliver(arrivals, gate=gate), (False, None, expected),
                msg=f"arrivals={arrivals}",
            )

    def test_roll_equal_to_probability_passes_in_every_mode(self):
        for arrivals, probability, name in [
            (QUIET_ARRIVALS, 0.10, "QUIET"),
            (CASUAL_ARRIVALS, 0.25, "CASUAL"),
            (LIVELY_ARRIVALS, 0.50, "LIVELY"),
            (BURST_ARRIVALS, 0.70, "BURST"),
        ]:
            gate = ProactiveGate(self.config)
            self.rng.value = probability
            allowed, mode, reason = self.deliver(arrivals, gate=gate)
            self.assertTrue(allowed, msg=name)
            self.assertEqual(mode.name, name)

    def test_miss_still_starts_a_new_interval(self):
        # Design note: a miss stamps last_eval so the dice are not re-rolled
        # on every message -- a miss "costs" a whole interval.
        self.rng.value = 0.99
        self.deliver(QUIET_ARRIVALS)
        self.assertEqual(self.gate._last_eval[self.CHAT], self.clock.now)

        self.clock.advance(60)
        self.rng.value = 0.0
        self.assertEqual(
            self.deliver(QUIET_ARRIVALS),
            (False, None, "eval interval (60s < 300s, backoff=1x)"),
        )
        self.clock.advance(240)
        self.assertTrue(self.deliver(QUIET_ARRIVALS)[0])

    def test_miss_is_not_counted_as_silence(self):
        self.rng.value = 0.99
        self.deliver(QUIET_ARRIVALS)
        self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), 0)

    def test_one_roll_per_evaluation(self):
        self.rng.value = 0.0
        self.deliver(QUIET_ARRIVALS)
        self.assertEqual(self.rng.calls, 1)


class GatePassTests(GateTestCase):
    """All gates pass: (True, mode, "mode=<NAME> rate=<r>/min")."""

    def setUp(self):
        super().setUp()
        self.rng.value = 0.0

    def test_pass_result_per_mode(self):
        for arrivals, name, rate, interval, max_chars, context in [
            (3, "QUIET", "1.5", 300, 30, 30),
            (7, "QUIET", "3.5", 300, 30, 30),
            (8, "CASUAL", "4.0", 120, 50, 50),
            (12, "CASUAL", "6.0", 120, 50, 50),
            (13, "LIVELY", "6.5", 60, 35, 60),
            (16, "LIVELY", "8.0", 60, 35, 60),
            (17, "BURST", "8.5", 30, 20, 80),
            (40, "BURST", "20.0", 30, 20, 80),
        ]:
            gate = ProactiveGate(self.config)
            allowed, mode, reason = self.deliver(arrivals, gate=gate)
            self.assertTrue(allowed, msg=f"arrivals={arrivals}")
            self.assertIsInstance(mode, ProactiveMode)
            self.assertEqual(mode.name, name, msg=f"arrivals={arrivals}")
            self.assertEqual(reason, f"mode={name} rate={rate}/min")
            self.assertEqual(
                (mode.eval_interval_sec, mode.max_chars, mode.context_count),
                (interval, max_chars, context),
            )

    def test_returned_mode_is_the_cached_mode_object(self):
        allowed, mode, reason = self.deliver(CASUAL_ARRIVALS)
        cached = get_modes(self.config)
        self.assertTrue(any(mode is m for m in cached))

    def test_pass_stamps_last_eval_with_now(self):
        self.clock.advance(1234)
        self.deliver(QUIET_ARRIVALS)
        self.assertEqual(self.gate._last_eval[self.CHAT], self.clock.now)

    def test_pass_leaves_the_silence_counter_alone(self):
        self.gate.record_silence(self.CHAT)
        self.gate.record_silence(self.CHAT)
        self.gate.record_eval(self.CHAT)
        self.clock.advance(1200)
        self.assertTrue(self.deliver(QUIET_ARRIVALS)[0])
        self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), 2)

    def test_should_speak_does_not_mutate_the_message(self):
        message = self.msg()
        before = copy.deepcopy(message)
        self.gate.should_speak(message)
        self.assertEqual(message, before)

    def test_gate_pass_is_logged_at_info(self):
        with self.assertLogs("src.proactive.gate", level="INFO") as captured:
            self.deliver(QUIET_ARRIVALS)
        text = "\n".join(captured.output)
        self.assertIn("GATE PASSED mode=QUIET", text)
        self.assertIn("rate=1.5/min", text)
        self.assertIn("interval=300s", text)
        self.assertIn("prob=10%", text)

    def test_blocked_paths_do_not_log_at_info(self):
        with self.assertNoLogs("src.proactive.gate", level="INFO"):
            self.gate.should_speak({})                       # no chat_id
            self.gate.should_speak(self.msg())               # SLEEP
            self.config.proactive_enabled = False
            self.gate.should_speak(self.msg())               # disabled


class GateConfigCouplingTests(GateTestCase):
    """How the gate picks up thresholds / window from BotConfig."""

    def setUp(self):
        super().setUp()
        self.rng.value = 0.0

    def custom_gate(self, **overrides) -> ProactiveGate:
        return ProactiveGate(BotConfig(proactive_enabled=True, **overrides))

    def test_thresholds_follow_the_config_while_the_cache_is_cold(self):
        gate = self.custom_gate(
            proactive_rate_quiet=1.0, proactive_rate_casual=2.0,
            proactive_rate_lively=3.0, proactive_rate_burst=4.0,
        )
        # 2 arrivals / 2 min = 1.0/min -> QUIET with these thresholds,
        # SLEEP with the defaults.
        allowed, mode, reason = self.deliver(2, gate=gate)
        self.assertEqual(
            (allowed, mode.name, reason),
            (True, "QUIET", "mode=QUIET rate=1.0/min"),
        )

    def test_rate_window_comes_from_the_config(self):
        gate = self.custom_gate(proactive_rate_window_sec=60)
        # 2 arrivals in a 60 s window = 2.0/min.
        allowed, mode, reason = self.deliver(2, gate=gate)
        self.assertEqual(
            (allowed, mode.name, reason),
            (True, "QUIET", "mode=QUIET rate=2.0/min"),
        )

    def test_window_is_fixed_when_the_gate_is_constructed(self):
        # PINNED (known issue, expected to change in Phase N): the RateTracker
        # is built once in __init__, so editing proactive_rate_window_sec
        # afterwards does not change how long arrivals are remembered.
        self.config.proactive_rate_window_sec = 600
        self.gate._tracker.record(self.CHAT)
        self.clock.advance(130)
        self.assertEqual(self.gate._tracker.count(self.CHAT), 0)

    def test_second_gate_inherits_modes_cached_by_the_first(self):
        # PINNED (known issue, expected to change in Phase N): the module
        # level mode cache is shared by every ProactiveGate in the process, so
        # the thresholds of whichever config evaluated first win.
        first = self.custom_gate()                            # default thresholds
        self.deliver(QUIET_ARRIVALS, gate=first)              # fills the cache

        custom = self.custom_gate(
            proactive_rate_quiet=1.0, proactive_rate_casual=2.0,
            proactive_rate_lively=3.0, proactive_rate_burst=4.0,
        )
        self.assertEqual(
            self.deliver(2, gate=custom),
            (False, None, "rate 1.0/min → SLEEP"),
        )

        reset_modes()                                         # what a fix would do
        allowed, mode, reason = self.deliver(2, gate=custom)
        self.assertEqual(
            (allowed, mode.name, reason),
            (True, "QUIET", "mode=QUIET rate=1.0/min"),
        )

    def test_threshold_edits_after_first_use_are_ignored_by_the_gate(self):
        # PINNED (known issue, expected to change in Phase N): same global
        # cache seen from the gate; only proactive_enabled is read live.
        self.assertTrue(self.deliver(QUIET_ARRIVALS)[0])      # cache: quiet=1.5
        self.config.proactive_rate_quiet = 0.1
        self.assertEqual(
            self.deliver(2),
            (False, None, "rate 1.0/min → SLEEP"),
        )


class SilenceBookkeepingTests(GateTestCase):
    """record_eval / record_silence / record_speech / get_consecutive_silence."""

    def test_unknown_chat_has_zero_silence(self):
        self.assertEqual(self.gate.get_consecutive_silence("never-seen"), 0)

    def test_record_silence_increments(self):
        for expected in (1, 2, 3):
            self.gate.record_silence(self.CHAT)
            self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), expected)

    def test_silence_is_counted_per_chat(self):
        self.gate.record_silence("a")
        self.gate.record_silence("a")
        self.gate.record_silence("b")
        self.assertEqual(self.gate.get_consecutive_silence("a"), 2)
        self.assertEqual(self.gate.get_consecutive_silence("b"), 1)
        self.assertEqual(self.gate.get_consecutive_silence("c"), 0)

    def test_record_speech_resets_the_counter_to_zero(self):
        for _ in range(3):
            self.gate.record_silence(self.CHAT)
        self.gate.record_speech(self.CHAT)
        self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), 0)
        # The key is kept (with value 0) rather than deleted.
        self.assertEqual(self.gate._consecutive_silence[self.CHAT], 0)

    def test_record_speech_only_resets_its_own_chat(self):
        self.gate.record_silence("a")
        self.gate.record_silence("b")
        self.gate.record_speech("a")
        self.assertEqual(self.gate.get_consecutive_silence("a"), 0)
        self.assertEqual(self.gate.get_consecutive_silence("b"), 1)

    def test_record_speech_for_unknown_chat_creates_no_entry(self):
        self.gate.record_speech("never-seen")
        self.assertNotIn("never-seen", self.gate._consecutive_silence)

    def test_silence_restarts_from_one_after_speech(self):
        self.gate.record_silence(self.CHAT)
        self.gate.record_silence(self.CHAT)
        self.gate.record_speech(self.CHAT)
        self.gate.record_silence(self.CHAT)
        self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), 1)

    def test_record_eval_stamps_now_and_blocks_the_interval(self):
        self.rng.value = 0.0
        self.gate.record_eval(self.CHAT)
        self.assertEqual(self.gate._last_eval[self.CHAT], self.clock.now)
        self.clock.advance(100)
        self.assertEqual(
            self.deliver(QUIET_ARRIVALS),
            (False, None, "eval interval (100s < 300s, backoff=1x)"),
        )

    def test_record_eval_does_not_touch_the_silence_counter(self):
        self.gate.record_silence(self.CHAT)
        self.gate.record_eval(self.CHAT)
        self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), 1)

    def test_record_silence_alone_does_not_start_an_interval(self):
        # Backoff only bites after a last-eval stamp exists (the router
        # always calls record_eval before record_silence).
        self.rng.value = 0.0
        for _ in range(5):
            self.gate.record_silence(self.CHAT)
        self.assertNotIn(self.CHAT, self.gate._last_eval)
        self.assertTrue(self.deliver(QUIET_ARRIVALS)[0])

    def test_router_style_silence_cycle_backs_off_then_recovers_on_speech(self):
        # Mirrors MessageRouter._handle_proactive_chat: after an empty AI
        # reply it calls record_eval() then record_silence(); after a real
        # reply it calls record_speech().
        self.rng.value = 0.0
        self.assertTrue(self.deliver(QUIET_ARRIVALS)[0])

        for silences, wait in [(1, 600), (2, 1200), (3, 2400)]:
            self.gate.record_eval(self.CHAT)
            self.gate.record_silence(self.CHAT)
            self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), silences)

            self.clock.advance(wait - 1)
            self.assertFalse(self.deliver(QUIET_ARRIVALS)[0], msg=f"wait={wait}")
            self.clock.advance(1)
            self.assertTrue(self.deliver(QUIET_ARRIVALS)[0], msg=f"wait={wait}")

        self.gate.record_speech(self.CHAT)
        self.assertEqual(self.gate.get_consecutive_silence(self.CHAT), 0)
        self.clock.advance(299)
        self.assertFalse(self.deliver(QUIET_ARRIVALS)[0])
        self.clock.advance(1)
        self.assertTrue(self.deliver(QUIET_ARRIVALS)[0])


class LastEvalCleanupTests(GateTestCase):
    """Every 500th counted call, with more than 100 entries, drops stale chats."""

    def seed(self, n_stale: int, n_fresh: int) -> None:
        """n_stale chats evaluated >1 h ago, n_fresh chats evaluated just now."""
        for i in range(n_stale):
            self.gate.record_eval(f"stale-{i}")
            self.gate.record_silence(f"stale-{i}")
        self.clock.advance(3601)
        for i in range(n_fresh):
            self.gate.record_eval(f"fresh-{i}")
            self.gate.record_silence(f"fresh-{i}")

    def pump(self, n: int) -> None:
        """Send *n* counted arrivals with the master switch off.

        Disabled calls return right after recording, so no gate or dice is
        reached -- yet they advance the cleanup counter (see
        test_cleanup_runs_even_while_disabled).
        """
        was_enabled = self.config.proactive_enabled
        self.config.proactive_enabled = False
        try:
            for _ in range(n):
                self.gate.should_speak(self.msg("pump"))
        finally:
            self.config.proactive_enabled = was_enabled

    def test_nothing_is_dropped_before_the_500th_call(self):
        self.seed(n_stale=101, n_fresh=3)
        self.pump(499)
        self.assertEqual(len(self.gate._last_eval), 104)

    def test_stale_entries_and_their_silence_counters_go_on_the_500th_call(self):
        self.seed(n_stale=101, n_fresh=3)
        self.pump(499)
        self.pump(1)                         # the 500th counted call
        fresh = {"fresh-0", "fresh-1", "fresh-2"}
        self.assertEqual(set(self.gate._last_eval), fresh)
        self.assertEqual(set(self.gate._consecutive_silence), fresh)
        # A purged chat starts from a clean backoff.
        self.assertEqual(self.gate.get_consecutive_silence("stale-0"), 0)
        self.assertEqual(self.gate.get_consecutive_silence("fresh-0"), 1)

    def test_cleanup_is_skipped_at_exactly_100_entries(self):
        self.seed(n_stale=100, n_fresh=0)
        self.pump(500)
        self.assertEqual(len(self.gate._last_eval), 100)

    def test_cleanup_runs_at_101_entries(self):
        self.seed(n_stale=101, n_fresh=0)
        self.pump(500)
        self.assertEqual(len(self.gate._last_eval), 0)

    def test_age_cutoff_keeps_entries_exactly_one_hour_old(self):
        self.gate.record_eval("older")       # S
        self.clock.advance(1)
        self.gate.record_eval("edge")        # S + 1
        self.clock.advance(10)
        for i in range(100):
            self.gate.record_eval(f"pad-{i}")  # S + 11
        self.clock.advance(3590)             # now = S + 3601 -> cutoff = S + 1
        self.pump(500)
        self.assertIn("edge", self.gate._last_eval)        # v == cutoff: kept
        self.assertNotIn("older", self.gate._last_eval)    # v < cutoff: dropped
        self.assertEqual(len(self.gate._last_eval), 101)

    def test_cleanup_repeats_on_the_1000th_call(self):
        self.pump(500)                       # first trigger, nothing to clean
        self.seed(n_stale=101, n_fresh=0)
        self.pump(499)
        self.assertEqual(len(self.gate._last_eval), 101)
        self.pump(1)                         # the 1000th counted call
        self.assertEqual(len(self.gate._last_eval), 0)

    def test_calls_without_chat_id_are_not_counted(self):
        self.seed(n_stale=101, n_fresh=0)
        for _ in range(600):
            self.gate.should_speak({})
        self.pump(499)
        self.assertEqual(len(self.gate._last_eval), 101)
        self.pump(1)
        self.assertEqual(len(self.gate._last_eval), 0)

    def test_cleanup_runs_even_while_disabled(self):
        # The cleanup sits above the master-switch check; pump() is disabled.
        self.seed(n_stale=101, n_fresh=0)
        self.config.proactive_enabled = False
        for _ in range(500):
            self.gate.should_speak(self.msg("pump"))
        self.assertEqual(len(self.gate._last_eval), 0)

    def test_cleanup_also_runs_for_an_enabled_gate(self):
        self.seed(n_stale=101, n_fresh=0)
        self.pump(499)
        # 500th call, enabled: SLEEP for this chat, but cleanup already ran.
        self.assertEqual(
            self.gate.should_speak(self.msg("someone-else")),
            (False, None, "rate 0.5/min → SLEEP"),
        )
        self.assertEqual(len(self.gate._last_eval), 0)

    def test_silence_counters_without_a_last_eval_entry_are_never_purged(self):
        # PINNED (known issue, expected to change in Phase N): cleanup walks
        # _last_eval only, so a counter whose chat has no stamp is never
        # removed (bounded in practice because the router stamps first).
        self.gate.record_silence("silent-only")
        self.seed(n_stale=101, n_fresh=0)
        self.pump(500)
        self.assertEqual(len(self.gate._last_eval), 0)
        self.assertEqual(self.gate.get_consecutive_silence("silent-only"), 1)


class GateSharedStateTests(GateTestCase):
    def test_gate_and_tracker_hold_a_lock(self):
        # Every worker thread of the message pool (WcdbBackend
        # ThreadPoolExecutor, max_workers=4) calls should_speak()/record_*() on
        # the same gate; _last_eval, _consecutive_silence, _call_count and the
        # tracker deques used to be mutated with no synchronisation at all.
        # Fixed in Phase 5; see tests/test_shared_state_locks.py.
        self.assertTrue(holds_lock(self.gate))
        self.assertTrue(holds_lock(self.gate._tracker))


if __name__ == "__main__":
    unittest.main()
