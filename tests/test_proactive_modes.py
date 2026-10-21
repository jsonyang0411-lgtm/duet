"""Characterization tests for src/proactive/modes.py.

Phase 0 safety net: pins the CURRENT mode table, threshold lookup and the
module-level mode cache so that a later refactor shows up as an intentional
diff.  Quirks are marked
``# PINNED (known issue, expected to change in Phase N): <why>``.

``get_modes`` caches in a module global, so every test resets the cache both
before and after it runs (setUp/tearDown *and* an autouse fixture) -- nothing
here may leak into other test modules such as tests/test_router.py.
"""

import dataclasses
import unittest

import pytest

import src.proactive as proactive_package
from src.config import BotConfig
from src.proactive import modes as modes_module
from src.proactive.modes import (
    _MODE_BEHAVIOR,
    ProactiveMode,
    build_modes,
    get_modes,
    lookup_mode,
    reset_modes,
)


@pytest.fixture(autouse=True)
def _reset_mode_cache_around_every_test():
    reset_modes()
    yield
    reset_modes()


# Exact expected table for the default config (thresholds 1.5 / 4.0 / 6.5 / 8.5).
EXPECTED_DEFAULT_MODES = [
    {
        "name": "SLEEP",
        "label": "沉睡",
        "description": "群里没人说话，或者很久才有一条消息",
        "instruction": "群里很安静，如果没有什么特别值得说的，就保持沉默。",
        "min_rate": 0.0,
        "eval_interval_sec": 9999,
        "reply_probability": 0.0,
        "max_chars": 0,
        "context_count": 0,
    },
    {
        "name": "QUIET",
        "label": "冷清",
        "description": "偶尔有人冒泡，节奏很慢",
        "instruction": (
            "群里比较安静，偶尔才有人说话。看最近聊了什么，如果话题你能接上就自然地回一句。"
            "保持克制，不要说无关的话。如果群里出现重大打击、情绪崩溃、吵架、裁员离婚等严重话题，"
            "必须保持沉默。"
        ),
        "min_rate": 1.5,
        "eval_interval_sec": 300,
        "reply_probability": 0.10,
        "max_chars": 30,
        "context_count": 30,
    },
    {
        "name": "CASUAL",
        "label": "闲聊",
        "description": "正常聊天节奏，几个人在聊",
        "instruction": (
            "群里在正常聊天。仔细看最近大家在聊什么话题，确保你的回复是接着前面话题的。"
            "可以自然地插话、吐槽、接梗，但不要突然跳到无关话题。"
            "如果群里出现重大打击、情绪崩溃、吵架等严重话题，保持沉默，不要接话。"
        ),
        "min_rate": 4.0,
        "eval_interval_sec": 120,
        "reply_probability": 0.25,
        "max_chars": 50,
        "context_count": 50,
    },
    {
        "name": "LIVELY",
        "label": "热闹",
        "description": "多人同时在讨论，节奏较快",
        "instruction": (
            "群里聊得很嗨。快速扫一眼最近的话题，接短句、吐槽、起哄都可以。"
            "但要确保你接的是正在聊的事，不要答非所问。"
            "注意：如果话题转向严重负面事件（事故、重病、情绪崩溃等），停止插话。"
        ),
        "min_rate": 6.5,
        "eval_interval_sec": 60,
        "reply_probability": 0.50,
        "max_chars": 35,
        "context_count": 60,
    },
    {
        "name": "BURST",
        "label": "炸了",
        "description": "刷屏级别，瓜来了或者大事件",
        "instruction": (
            "群聊爆炸了！极短的感叹、吐槽、表情反应为主。快速扫上下文确保你没接错话题。"
            "但如果是负面大事件刷屏（事故、重病等），不要跟风发言。"
        ),
        "min_rate": 8.5,
        "eval_interval_sec": 30,
        "reply_probability": 0.70,
        "max_chars": 20,
        "context_count": 80,
    },
]


class ModesTestCase(unittest.TestCase):
    """Base class: guarantees an empty mode cache around every test."""

    def setUp(self):
        reset_modes()
        self.addCleanup(reset_modes)


class ModeBehaviorTableTests(ModesTestCase):
    def test_behavior_table_lists_five_modes_in_escalating_order(self):
        self.assertEqual(
            [b["name"] for b in _MODE_BEHAVIOR],
            ["SLEEP", "QUIET", "CASUAL", "LIVELY", "BURST"],
        )

    def test_behavior_table_carries_no_thresholds(self):
        # min_rate is injected from BotConfig at build time.
        for behavior in _MODE_BEHAVIOR:
            self.assertNotIn("min_rate", behavior)

    def test_behavior_table_keys(self):
        for behavior in _MODE_BEHAVIOR:
            self.assertEqual(
                set(behavior),
                {
                    "name", "label", "description", "instruction",
                    "eval_interval_sec", "reply_probability",
                    "max_chars", "context_count",
                },
            )

    def test_proactive_mode_is_frozen(self):
        mode = build_modes(BotConfig())[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            mode.eval_interval_sec = 1  # type: ignore[misc]

    def test_proactive_mode_field_order(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(ProactiveMode)],
            [
                "name", "label", "description", "instruction", "min_rate",
                "eval_interval_sec", "reply_probability", "max_chars",
                "context_count",
            ],
        )


class BuildModesTests(ModesTestCase):
    def test_default_config_builds_the_expected_table(self):
        built = build_modes(BotConfig())
        actual = [dataclasses.asdict(m) for m in built]
        self.assertEqual(actual, EXPECTED_DEFAULT_MODES)

    def test_default_thresholds_come_from_botconfig(self):
        cfg = BotConfig()
        self.assertEqual(
            (
                cfg.proactive_rate_quiet,
                cfg.proactive_rate_casual,
                cfg.proactive_rate_lively,
                cfg.proactive_rate_burst,
            ),
            (1.5, 4.0, 6.5, 8.5),
        )
        self.assertEqual([m.min_rate for m in build_modes(cfg)], [0.0, 1.5, 4.0, 6.5, 8.5])

    def test_per_mode_numbers(self):
        by_name = {m.name: m for m in build_modes(BotConfig())}
        actual = {
            name: (m.eval_interval_sec, m.reply_probability, m.max_chars, m.context_count)
            for name, m in by_name.items()
        }
        self.assertEqual(actual, {
            "SLEEP": (9999, 0.0, 0, 0),
            "QUIET": (300, 0.10, 30, 30),
            "CASUAL": (120, 0.25, 50, 50),
            "LIVELY": (60, 0.50, 35, 60),
            "BURST": (30, 0.70, 20, 80),
        })

    def test_custom_thresholds_are_injected_per_mode(self):
        cfg = BotConfig(
            proactive_rate_quiet=2.0,
            proactive_rate_casual=5.0,
            proactive_rate_lively=7.0,
            proactive_rate_burst=10.0,
        )
        built = build_modes(cfg)
        self.assertEqual(
            [(m.name, m.min_rate) for m in built],
            [("SLEEP", 0.0), ("QUIET", 2.0), ("CASUAL", 5.0), ("LIVELY", 7.0), ("BURST", 10.0)],
        )

    def test_custom_thresholds_do_not_change_mode_behaviour(self):
        custom = build_modes(BotConfig(
            proactive_rate_quiet=0.5, proactive_rate_casual=1.0,
            proactive_rate_lively=2.0, proactive_rate_burst=3.0,
        ))
        default = build_modes(BotConfig())
        for c, d in zip(custom, default):
            self.assertEqual(
                dataclasses.replace(c, min_rate=0.0),
                dataclasses.replace(d, min_rate=0.0),
            )

    def test_sleep_min_rate_is_always_zero(self):
        built = build_modes(BotConfig(proactive_rate_quiet=0.1))
        self.assertEqual(built[0].name, "SLEEP")
        self.assertEqual(built[0].min_rate, 0.0)

    def test_window_and_enabled_flag_do_not_influence_the_table(self):
        a = build_modes(BotConfig(proactive_enabled=True, proactive_rate_window_sec=600))
        b = build_modes(BotConfig(proactive_enabled=False, proactive_rate_window_sec=30))
        self.assertEqual(a, b)

    def test_build_returns_a_fresh_equal_list_each_time(self):
        cfg = BotConfig()
        first = build_modes(cfg)
        second = build_modes(cfg)
        self.assertIsNot(first, second)
        self.assertEqual(first, second)

    def test_build_does_not_touch_the_module_cache(self):
        build_modes(BotConfig())
        self.assertIsNone(modes_module._MODES)

    def test_unordered_thresholds_are_resorted_by_min_rate(self):
        # PINNED (known issue, expected to change in Phase N): build_modes()
        # does not validate ordering (only config validation does) and simply
        # sorts by min_rate, so a swapped threshold silently swaps behaviours:
        # QUIET (300 s, 10 %) ends up ABOVE CASUAL.
        cfg = BotConfig(
            proactive_rate_quiet=5.0,
            proactive_rate_casual=3.0,
            proactive_rate_lively=7.0,
            proactive_rate_burst=9.0,
        )
        built = build_modes(cfg)
        self.assertEqual(
            [(m.name, m.min_rate) for m in built],
            [("SLEEP", 0.0), ("CASUAL", 3.0), ("QUIET", 5.0), ("LIVELY", 7.0), ("BURST", 9.0)],
        )

    def test_zero_threshold_hides_sleep_mode(self):
        # PINNED (known issue, expected to change in Phase N): a zero
        # threshold ties with SLEEP; the stable sort keeps SLEEP first and
        # lookup_mode() (last satisfied mode wins) never returns it again.
        cfg = BotConfig(proactive_rate_quiet=0.0)
        built = build_modes(cfg)
        self.assertEqual([m.name for m in built][:2], ["SLEEP", "QUIET"])
        self.assertEqual(lookup_mode(0.0, cfg).name, "QUIET")


class GetModesCacheTests(ModesTestCase):
    def test_cache_starts_empty(self):
        self.assertIsNone(modes_module._MODES)

    def test_first_call_builds_and_caches(self):
        cfg = BotConfig()
        built = get_modes(cfg)
        self.assertIs(modes_module._MODES, built)
        self.assertEqual([m.name for m in built], ["SLEEP", "QUIET", "CASUAL", "LIVELY", "BURST"])

    def test_second_call_returns_the_same_list_object(self):
        cfg = BotConfig()
        self.assertIs(get_modes(cfg), get_modes(cfg))

    def test_cached_values_equal_a_fresh_build(self):
        cfg = BotConfig()
        self.assertEqual(get_modes(cfg), build_modes(cfg))

    def test_cache_ignores_a_different_config_on_later_calls(self):
        # PINNED (known issue, expected to change in Phase N): the cache is a
        # process-wide global keyed on nothing.  Whichever config reaches
        # get_modes() first wins for every ProactiveGate / config afterwards,
        # and nothing in src/ ever calls reset_modes(), so a threshold edited
        # at runtime only takes effect after a full restart.
        first = BotConfig()
        second = BotConfig(
            proactive_rate_quiet=3.0, proactive_rate_casual=5.0,
            proactive_rate_lively=7.0, proactive_rate_burst=9.0,
        )
        built_first = get_modes(first)
        built_second = get_modes(second)
        self.assertIs(built_second, built_first)
        self.assertEqual([m.min_rate for m in built_second], [0.0, 1.5, 4.0, 6.5, 8.5])

    def test_cache_ignores_in_place_config_mutation(self):
        # PINNED (known issue, expected to change in Phase N): thresholds
        # edited on the same config object after the first call are not seen.
        cfg = BotConfig()
        get_modes(cfg)
        cfg.proactive_rate_quiet = 3.0
        self.assertEqual(get_modes(cfg)[1].min_rate, 1.5)

    def test_reset_clears_the_cache(self):
        get_modes(BotConfig())
        reset_modes()
        self.assertIsNone(modes_module._MODES)

    def test_reset_forces_rebuild_from_the_new_config(self):
        get_modes(BotConfig())
        reset_modes()
        custom = BotConfig(
            proactive_rate_quiet=3.0, proactive_rate_casual=5.0,
            proactive_rate_lively=7.0, proactive_rate_burst=9.0,
        )
        self.assertEqual([m.min_rate for m in get_modes(custom)], [0.0, 3.0, 5.0, 7.0, 9.0])

    def test_reset_builds_a_new_list_object(self):
        cfg = BotConfig()
        before = get_modes(cfg)
        reset_modes()
        after = get_modes(cfg)
        self.assertIsNot(before, after)
        self.assertEqual(before, after)

    def test_reset_on_empty_cache_is_a_noop(self):
        reset_modes()
        reset_modes()
        self.assertIsNone(modes_module._MODES)

    def test_returned_list_is_the_live_cache(self):
        # PINNED (known issue, expected to change in Phase N): get_modes()
        # hands out the mutable cache itself, so a caller can corrupt it for
        # the whole process (tearDown/fixture reset_modes() repairs it here).
        cfg = BotConfig()
        cached = get_modes(cfg)
        cached.pop()
        self.assertEqual(len(get_modes(cfg)), 4)
        self.assertEqual(get_modes(cfg)[-1].name, "LIVELY")


class LookupModeTests(ModesTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = BotConfig()

    def assertMode(self, rate, name, cfg=None):
        mode = lookup_mode(rate, cfg or self.cfg)
        self.assertEqual(mode.name, name, msg=f"rate={rate!r}")

    def test_boundaries_with_default_thresholds(self):
        for rate, expected in [
            (0.0, "SLEEP"),
            (0.5, "SLEEP"),
            (1.4999, "SLEEP"),      # just below QUIET
            (1.5, "QUIET"),         # exactly at the threshold -> upgrades
            (1.5001, "QUIET"),
            (3.9999, "QUIET"),
            (4.0, "CASUAL"),
            (4.0001, "CASUAL"),
            (6.4999, "CASUAL"),
            (6.5, "LIVELY"),
            (6.5001, "LIVELY"),
            (8.4999, "LIVELY"),
            (8.5, "BURST"),
            (8.5001, "BURST"),
        ]:
            self.assertMode(rate, expected)

    def test_far_above_the_top_threshold_is_burst(self):
        for rate in (20.0, 100.0, 1e6, float("inf")):
            self.assertMode(rate, "BURST")

    def test_negative_rate_falls_back_to_sleep(self):
        self.assertMode(-1.0, "SLEEP")
        self.assertMode(-1e9, "SLEEP")

    def test_nan_rate_falls_back_to_sleep(self):
        # Every ``nan >= x`` comparison is False, so the SLEEP fallback wins.
        self.assertMode(float("nan"), "SLEEP")

    def test_at_threshold_the_higher_mode_wins_for_every_boundary(self):
        # Equality semantics: ``rate >= min_rate`` (inclusive lower bound).
        cfg = BotConfig(
            proactive_rate_quiet=2.0, proactive_rate_casual=5.0,
            proactive_rate_lively=7.0, proactive_rate_burst=10.0,
        )
        for rate, expected in [
            (1.999, "SLEEP"),
            (2.0, "QUIET"),
            (4.999, "QUIET"),
            (5.0, "CASUAL"),
            (6.999, "CASUAL"),
            (7.0, "LIVELY"),
            (9.999, "LIVELY"),
            (10.0, "BURST"),
        ]:
            self.assertMode(rate, expected, cfg)

    def test_returns_the_cached_mode_objects(self):
        cached = get_modes(self.cfg)
        self.assertIs(lookup_mode(0.0, self.cfg), cached[0])
        self.assertIs(lookup_mode(1.5, self.cfg), cached[1])
        self.assertIs(lookup_mode(4.0, self.cfg), cached[2])
        self.assertIs(lookup_mode(6.5, self.cfg), cached[3])
        self.assertIs(lookup_mode(8.5, self.cfg), cached[4])

    def test_lookup_populates_the_module_cache(self):
        self.assertIsNone(modes_module._MODES)
        lookup_mode(0.0, self.cfg)
        self.assertIsNotNone(modes_module._MODES)

    def test_lookup_uses_the_cache_not_the_config_passed_later(self):
        # PINNED (known issue, expected to change in Phase N): same global
        # cache as get_modes(): the second config's thresholds are ignored
        # until reset_modes() is called.
        other = BotConfig(
            proactive_rate_quiet=3.0, proactive_rate_casual=5.0,
            proactive_rate_lively=7.0, proactive_rate_burst=9.0,
        )
        self.assertMode(1.5, "QUIET", self.cfg)          # builds the cache
        self.assertMode(1.5, "QUIET", other)             # would be SLEEP if rebuilt
        reset_modes()
        self.assertMode(1.5, "SLEEP", other)
        self.assertMode(3.0, "QUIET", other)

    def test_attributes_seen_through_lookup(self):
        for rate, expected in [
            (0.0, ("SLEEP", 9999, 0.0, 0, 0)),
            (1.5, ("QUIET", 300, 0.10, 30, 30)),
            (4.0, ("CASUAL", 120, 0.25, 50, 50)),
            (6.5, ("LIVELY", 60, 0.50, 35, 60)),
            (8.5, ("BURST", 30, 0.70, 20, 80)),
        ]:
            mode = lookup_mode(rate, self.cfg)
            self.assertEqual(
                (
                    mode.name, mode.eval_interval_sec, mode.reply_probability,
                    mode.max_chars, mode.context_count,
                ),
                expected,
                msg=f"rate={rate}",
            )

    def test_sleep_mode_never_replies_by_construction(self):
        sleep = lookup_mode(0.0, self.cfg)
        self.assertEqual(sleep.reply_probability, 0.0)
        self.assertEqual(sleep.max_chars, 0)
        self.assertEqual(sleep.context_count, 0)

    def test_every_non_sleep_mode_interval_shrinks_as_rate_grows(self):
        intervals = [lookup_mode(r, self.cfg).eval_interval_sec for r in (1.5, 4.0, 6.5, 8.5)]
        self.assertEqual(intervals, sorted(intervals, reverse=True))
        probabilities = [lookup_mode(r, self.cfg).reply_probability for r in (1.5, 4.0, 6.5, 8.5)]
        self.assertEqual(probabilities, sorted(probabilities))


class PackageExportsTests(unittest.TestCase):
    def test_package_all_is_pinned(self):
        # The package re-exports a subset; build_modes, reset_modes,
        # StickyMentionTracker and ImageFollowupTracker are NOT re-exported.
        self.assertEqual(
            proactive_package.__all__,
            [
                "ProactiveMode",
                "get_modes",
                "lookup_mode",
                "RateTracker",
                "ProactiveGate",
                "FollowupListenTracker",
                "FollowupMatch",
            ],
        )

    def test_package_exports_resolve_to_the_module_objects(self):
        self.assertIs(proactive_package.get_modes, get_modes)
        self.assertIs(proactive_package.lookup_mode, lookup_mode)
        self.assertIs(proactive_package.ProactiveMode, ProactiveMode)
        for name in ("build_modes", "reset_modes", "StickyMentionTracker", "ImageFollowupTracker"):
            self.assertFalse(hasattr(proactive_package, name), msg=name)


if __name__ == "__main__":
    unittest.main()
