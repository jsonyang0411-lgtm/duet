"""The opt-in live evaluation runner, exercised with a fake "real" model."""

import json

import pytest

from evals import metrics, run_live, run_offline
from src.pipeline.types import Decision
from test_evals_harness import _case


class FakeReal:
    """Stands in for a configured summarizer; scripted from the message text."""

    last_chat_reply_was_intentional_silence = False

    def __init__(self):
        self.calls = []
        self._recorder = None

    def set_call_recorder(self, recorder):
        self._recorder = recorder

    def _note(self, name, **extra):
        if self._recorder:
            self._recorder(call=name, stage="single", attempt="ask", latency_ms=5.0,
                           usage={"input_tokens": 100, "output_tokens": 20}, **extra)

    def chat(self, **kwargs):
        self.calls.append("chat")
        self._note("chat")
        silent = "QUIETPLEASE" in kwargs["message"]
        self.last_chat_reply_was_intentional_silence = silent
        return "" if silent else "好的"

    def proactive_chat(self, **kwargs):
        self.calls.append("proactive_chat")
        return ""

    def decide_and_reply(self, request, *, strategy="single"):
        self.calls.append(f"decide:{request.addressing.level}")
        self._note("chat_v2")
        if "QUIETPLEASE" in request.message:
            return Decision(action="silent", addressee="others", strategy=strategy, calls=1)
        if request.addressing.level == "ambient":
            return Decision(action="silent", addressee="group", strategy=strategy, calls=1)
        return Decision(action="reply", messages=("好的",), addressee="me",
                        strategy=strategy, calls=1)


CHATTER = [(-60, "u_a", "阿明", "吃了吗"), (-30, "u_b", "小红", "还没呢")]
CASES = [
    _case("c-at", content="@小助手 你好", expect=("speak", "must", "bot")),
    _case("c-name", category="name_call", content="小助手在吗", at=False, context=CHATTER,
          expect=("speak", "must", "bot")),
    _case("c-quiet", content="今天好热啊", at=False, context=CHATTER,
          expect=("silent", "should", "group")),
    _case("c-decline", content="@小助手 QUIETPLEASE他们在聊", expect=("silent", "should", "other_member")),
    _case("c-local", category="lots_command", content="@小助手 抽签",
          expect=("speak", "must", "bot")),
]


def rows_for(strategy, real=None):
    real = real or FakeReal()
    return run_live.run_strategy(CASES, strategy, real, seed=7), real


# ── the live summarizer ─────────────────────────────────────────────────

def test_only_the_trigger_reaches_the_real_model():
    rows, real = rows_for("single")
    # The two context lines of every case go through the router with the stub;
    # the real model is asked about the trigger only, and not at all for the
    # command (c-local).  The proactive gate is forced open, so c-quiet is asked.
    assert real.calls == ["decide:explicit", "decide:strong", "decide:ambient", "decide:explicit"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["c-at"]["spoke"] and by_id["c-at"]["reply"] == "好的"
    assert by_id["c-at"]["route_level"] == "explicit" and by_id["c-at"]["decision_status"] == "ok"
    assert by_id["c-at"]["model_calls"] == 1 and by_id["c-at"]["tokens_in"] == 100


def test_the_legacy_strategy_uses_chat_and_the_silence_flag():
    rows, real = rows_for("off")
    by_id = {r["id"]: r for r in rows}
    assert real.calls.count("chat") == 2         # c-at and c-decline
    assert real.calls.count("proactive_chat") == 2   # c-quiet, and the name call read as ambient chatter
    # The legacy router has no name-call route: it is just ambient talk.
    assert by_id["c-name"]["route"] == "proactive" and not by_id["c-name"]["spoke"]
    assert by_id["c-decline"]["decision_status"] is None and not by_id["c-decline"]["spoke"]


def test_structured_strategies_route_the_name_call_and_let_the_model_decline():
    rows, _ = rows_for("single")
    by_id = {r["id"]: r for r in rows}
    assert by_id["c-name"]["route"] == "name_call" and by_id["c-name"]["spoke"]
    assert by_id["c-decline"]["decision_action"] == "silent" and not by_id["c-decline"]["spoke"]
    assert by_id["c-quiet"]["route"] == "proactive" and not by_id["c-quiet"]["spoke"]
    assert by_id["c-local"]["outcome"] == "handled_locally" and by_id["c-local"]["model_calls"] == 0


def test_replies_are_kept_only_when_asked():
    result = run_offline.run_routing([CASES[0]], seed=7)["cases"][0]
    assert "reply" not in result


# ── scoring ─────────────────────────────────────────────────────────────

def row(cid, action, strength, spoke, **extra):
    return {
        "id": cid, "category": "x", "addressing": extra.pop("addressing", "explicit"),
        "expect": None if action is None else {"action": action, "strength": strength, "addressee": "bot"},
        "outcome": extra.pop("outcome", "engaged_chat"), "route": "r", "reply": "好" if spoke else "",
        "spoke": spoke, **extra,
    }


def test_is_correct():
    is_correct = run_live.is_correct
    assert is_correct(row("a", "speak", "must", True)) is True
    assert is_correct(row("a", "speak", "must", False)) is False
    assert is_correct(row("a", "silent", "should", False)) is True
    assert is_correct(row("a", "silent", "should", True)) is False
    assert is_correct(row("a", "either", "free", True)) is None
    assert is_correct(row("a", None, "", True)) is None


def test_summary_balanced_accuracy_and_breakdowns():
    rows = [
        row("s1", "speak", "must", True), row("s2", "speak", "must", False),
        row("s3", "speak", "should", True), row("n1", "silent", "must", False),
        row("n2", "silent", "should", True, addressing="implicit"),
        row("e1", "either", "free", True),
        row("l1", "speak", "must", True, outcome="handled_locally"),
    ]
    summary = run_live.summarize_strategy(rows)
    assert (summary["all"]["speak_recall"]["k"], summary["all"]["speak_recall"]["n"]) == (3, 4)
    assert (summary["all"]["silent_recall"]["k"], summary["all"]["silent_recall"]["n"]) == (1, 2)
    assert summary["all"]["balanced_accuracy"] == pytest.approx((0.75 + 0.5) / 2, abs=1e-4)
    assert summary["must"]["silent_recall"] == metrics.ratio(1, 1)
    assert summary["by_addressing"]["implicit"]["silent_recall"] == metrics.ratio(0, 1)
    assert summary["n_model_cases"] == 6                  # the command is not a model case
    assert summary["model_cases"]["speak_recall"]["n"] == 3


def test_summary_counts_leaks_statuses_calls_and_cost():
    rows = [
        row("a", "speak", "must", True, reply="沉默", decision_status="ok", model_calls=1,
            tokens_in=1000, tokens_out=100, model_seconds=1.0),
        row("b", "speak", "must", True, decision_status="repaired", model_calls=2,
            tokens_in=2000, tokens_out=200, model_seconds=3.0),
        row("c", "speak", "must", False, decision_status="parse_failed", model_calls=2,
            tokens_in=2000, tokens_out=200, model_seconds=2.0),
        row("d", "silent", "must", False, decision_status="ok", model_calls=1,
            tokens_in=1000, tokens_out=100, model_seconds=1.0),
    ]
    rows[0]["reply"] = "沉默"                                # a silence marker that got sent
    summary = run_live.summarize_strategy(rows, price_in=1.0, price_out=2.0)
    assert summary["leaks"] == {"n": 1, "ids": ["a"], "of_replies": 2}
    assert summary["decision_status"] == {"ok": 2, "parse_failed": 1, "repaired": 1}
    assert summary["unusable_rate"] == metrics.ratio(1, 4)
    assert summary["repaired_rate"] == metrics.ratio(1, 4)
    assert summary["model_calls"] == 6 and summary["calls_per_model_case"] == 1.5
    assert (summary["tokens_in"], summary["tokens_out"]) == (6000, 600)
    assert summary["estimated_cost"] == pytest.approx(6000 / 1e6 * 1.0 + 600 / 1e6 * 2.0)
    assert summary["latency_s"]["p50"] in (1.0, 2.0)


def test_compare_lists_the_discordant_cases():
    a = [row("x", "speak", "must", True), row("y", "silent", "must", True), row("z", "speak", "must", True)]
    b = [row("x", "speak", "must", False), row("y", "silent", "must", False), row("z", "speak", "must", True)]
    (pair,) = run_live.compare({"A": a, "B": b})
    assert pair["only_a_correct"] == ["x"] and pair["only_b_correct"] == ["y"]


# ── end to end ──────────────────────────────────────────────────────────

def test_the_report_compares_the_strategies_on_the_same_cases():
    report = run_live.build_report(CASES, ["off", "single"], FakeReal(), seed=7)
    assert report["n_cases"] == len(CASES) and set(report["strategies"]) == {"off", "single"}
    off, single = (report["strategies"][s] for s in ("off", "single"))
    # the structured strategy answers the name call the legacy router never routes
    assert single["all"]["speak_recall"]["k"] > off["all"]["speak_recall"]["k"]
    (pair,) = report["compare"]
    assert "c-name" in pair["only_b_correct"]
    markdown = run_live.render_markdown(report)
    assert "balanced accuracy, all labeled cases" in markdown and "c-name" in markdown


def test_plan_counts_the_cases_that_reach_a_model():
    estimate = run_live.plan(CASES, ["off", "single", "two_step"], 7)
    assert estimate["off"]["model_cases"] == estimate["single"]["model_cases"] == 4
    assert estimate["single"]["max_calls"] == 4
    assert estimate["two_step"]["max_calls"] == 2 * estimate["two_step"]["model_cases"]


def test_a_dry_run_never_builds_or_calls_the_model(tmp_path, capsys):
    built = []
    code = run_live.main(
        ["--out", str(tmp_path), "--strategies", "single", "--limit", "6"],
        summarizer_factory=lambda: built.append(1) or FakeReal(),
    )
    assert code == 0 and built == []
    out = capsys.readouterr().out
    assert "Dry run" in out and "single" in out
    assert list(tmp_path.iterdir()) == []


def test_main_with_yes_writes_json_and_markdown(tmp_path, capsys):
    scenarios = tmp_path / "cases.jsonl"
    scenarios.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CASES), encoding="utf-8")
    real = FakeReal()
    code = run_live.main(
        ["--cases", str(scenarios), "--out", str(tmp_path / "out"), "--tag", "t",
         "--strategies", "off", "single", "--yes", "--price-in", "1", "--price-out", "2"],
        summarizer_factory=lambda: real,
    )
    assert code == 0 and real.calls
    report = json.loads((tmp_path / "out" / "t.json").read_text("utf-8"))
    assert report["schema"] == run_live.LIVE_SCHEMA
    assert "estimated_cost" in report["strategies"]["single"]
    assert (tmp_path / "out" / "t.md").read_text("utf-8").startswith("# Live evaluation")


# ── an outage must not be scored as behaviour ───────────────────────────

class DownReal(FakeReal):
    """The endpoint refuses every call (bad key, outage)."""

    def _fail(self):
        self._note("chat_v2", error="AuthenticationError: bad key")
        raise RuntimeError("Failed after 2 attempt(s): bad key")

    def chat(self, **kwargs):
        self.calls.append("chat")
        self._note("chat", error="AuthenticationError: bad key")
        raise RuntimeError("bad key")

    def decide_and_reply(self, request, *, strategy="single"):
        self.calls.append("decide")
        self._fail()


def test_a_failed_model_call_is_flagged_and_left_out_of_the_scores():
    flaky = FakeReal()
    original = flaky.decide_and_reply

    def sometimes(request, *, strategy="single"):
        if "QUIETPLEASE" in request.message:
            flaky._note("chat_v2", error="Timeout")
            raise RuntimeError("timed out")
        return original(request, strategy=strategy)

    flaky.decide_and_reply = sometimes
    rows = run_live.run_strategy(CASES, "single", flaky, seed=7)
    by_id = {r["id"]: r for r in rows}
    assert by_id["c-decline"]["model_errors"] == 1 and by_id["c-decline"]["spoke"]   # the apology reply
    summary = run_live.summarize_strategy(rows)
    assert summary["errored"]["ids"] == ["c-decline"]
    assert summary["errored"]["of_model_cases"] == metrics.ratio(1, 4)
    assert summary["n_cases"] == len(CASES)
    assert summary["all"]["silent_recall"]["n"] == 1          # c-decline is not scored (only c-quiet is)


def test_a_dead_endpoint_aborts_the_run():
    many = [_case(f"d{i}", content="@小助手 你好", expect=("speak", "must", "bot")) for i in range(6)]
    with pytest.raises(run_live.LiveRunAborted, match="bad key"):
        run_live.run_strategy(many, "single", DownReal(), seed=7)


def test_main_exits_nonzero_and_writes_nothing_when_the_endpoint_is_dead(tmp_path, capsys):
    scenarios = tmp_path / "cases.jsonl"
    scenarios.write_text("".join(
        json.dumps(_case(f"d{i}", content="@小助手 你好", expect=("speak", "must", "bot")),
                   ensure_ascii=False) + "\n" for i in range(6)), encoding="utf-8")
    code = run_live.main(
        ["--cases", str(scenarios), "--out", str(tmp_path / "out"), "--strategies", "single", "--yes"],
        summarizer_factory=DownReal,
    )
    assert code == 3 and not (tmp_path / "out").exists()
    assert "Aborted" in capsys.readouterr().err


def test_main_warns_and_exits_nonzero_when_too_many_cases_errored(tmp_path, capsys):
    flaky = FakeReal()
    original = flaky.decide_and_reply

    def mostly_down(request, *, strategy="single"):
        if request.message.endswith("0") or request.message.endswith("2") or request.message.endswith("4"):
            flaky._note("chat_v2", error="Timeout")
            raise RuntimeError("timed out")
        return original(request, strategy=strategy)

    flaky.decide_and_reply = mostly_down
    cases = [_case(f"d{i}", content=f"@小助手 你好{i}", expect=("speak", "must", "bot")) for i in range(5)]
    scenarios = tmp_path / "cases.jsonl"
    scenarios.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    code = run_live.main(
        ["--cases", str(scenarios), "--out", str(tmp_path / "out"), "--strategies", "single", "--yes"],
        summarizer_factory=lambda: flaky,
    )
    assert code == 3
    assert (tmp_path / "out" / "live.json").exists()
    assert "not comparable" in capsys.readouterr().err


# ── a persona card instead of the persona in .env ───────────────────────

def test_a_persona_card_is_played_and_recorded(tmp_path):
    from evals.persona_cards import HELD_OUT

    class CardReal(FakeReal):
        def configure_chat_persona(self, **kwargs):
            self.persona = kwargs

    scenarios = tmp_path / "cases.jsonl"
    scenarios.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CASES), encoding="utf-8")
    real = CardReal()
    code = run_live.main(
        ["--cases", str(scenarios), "--out", str(tmp_path / "out"), "--tag", "t", "--strategies", "single",
         "--yes", "--persona-cards", str(HELD_OUT), "--persona-id", "eval-coder"],
        summarizer_factory=lambda: real,
    )
    assert code == 0 and real.persona["persona_name"] == "阿K" and real.persona["max_chars"] == 25
    assert json.loads((tmp_path / "out" / "t.json").read_text("utf-8"))["persona"] == "eval-coder"
