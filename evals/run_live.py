"""Opt-in live evaluation: the same scenarios, a real model, several strategies.

    python -m evals.run_live                      # plan only: what would be called
    python -m evals.run_live --yes                # run it (costs API credit)
    python -m evals.run_live --yes --strategies off single --limit 40

Each scenario is replayed through the real router exactly as in
``evals.run_offline``; only the *trigger* message reaches a real model (the
summarizer configured in your ``.env``), and only for the strategies you ask
for:

``off``       the original single prompt, blank means silence, regex clean-up
``single``    one structured call decides and writes
``two_step``  a short "should I speak?" call for unclear messages, then the reply

Nothing is sent to WeChat: the router only returns the text it would send.
The scenarios are invented chats; with ``--cases`` they can be your own labeled
exports, in which case their text goes to the API in your ``.env`` just like
production traffic would.

What it answers (the offline harness cannot): when the real model gets the
message and the facts the program computed, does it speak when it should and
stay silent when it should, how often is its answer unusable, what does it cost.
The proactive gate is forced open for every strategy so the model's judgement
is what is measured; the gate itself is judged offline.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _real_time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # `python evals/run_live.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from evals import metrics  # noqa: E402
from evals import run_offline  # noqa: E402
from evals.run_offline import RecordingSummarizer  # noqa: E402
from src.pipeline import text as reply_text  # noqa: E402

STRATEGIES = ("off", "single", "two_step")
#: Stop when this many model-asked cases in a row hit an API error (bad key, outage).
ABORT_AFTER_ERRORS = 3
#: Above this share of errored model cases the numbers are not comparable.
MAX_ERROR_SHARE = 0.2


class LiveRunAborted(RuntimeError):
    """The model endpoint is failing: continuing would only measure the outage."""

DEFAULT_OUT = REPO_ROOT / "data" / "replay" / "live"
LIVE_SCHEMA = "live-eval/v1"


# ── the summarizer that is real only for the trigger ─────────────────────

class LiveSummarizer(RecordingSummarizer):
    """Stub for context and priming events, a real model for the trigger.

    It keeps the stub's call records (so the router's outcome classification
    works unchanged) and adds what only a live run knows: the decisions, the
    token usage and the time the model took.
    """

    def __init__(self, real: Any) -> None:
        super().__init__()
        self.real = real
        self._live = False
        self.decisions: list[Any] = []
        self.model_calls: list[dict] = []
        self.model_seconds = 0.0
        if hasattr(real, "set_call_recorder"):
            real.set_call_recorder(self._note_call)

    def _note_call(self, **fields: Any) -> None:
        self.model_calls.append({
            key: fields[key] for key in
            ("call", "stage", "attempt", "latency_ms", "usage", "error", "native_json")
            if key in fields
        })

    def begin_event(self, event: str, reply: str) -> None:
        super().begin_event(event, reply)
        self._live = event == "trigger"

    def _timed(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        started = _real_time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            self.model_seconds += _real_time.perf_counter() - started

    def chat(self, **kwargs: Any) -> str:
        stub = super().chat(**kwargs)
        if not self._live:
            return stub
        reply = self._timed(self.real.chat, **kwargs)
        self.last_chat_reply_was_intentional_silence = bool(
            getattr(self.real, "last_chat_reply_was_intentional_silence", False)
        )
        return reply

    def proactive_chat(self, **kwargs: Any) -> str:
        stub = super().proactive_chat(**kwargs)
        return self._timed(self.real.proactive_chat, **kwargs) if self._live else stub

    def decide_and_reply(self, request: Any, *, strategy: str = "single") -> Any:
        stub = super().decide_and_reply(request, strategy=strategy)
        if not self._live:
            return stub
        decision = self._timed(self.real.decide_and_reply, request, strategy=strategy)
        self.decisions.append(decision)
        return decision


# ── scoring (pure) ───────────────────────────────────────────────────────

def _percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def is_correct(row: Mapping[str, Any]) -> bool | None:
    """Whether the bot did what the label asked; None for ``either`` / unlabeled."""
    expect = row.get("expect")
    if not expect or expect["action"] not in ("speak", "silent"):
        return None
    return bool(row["spoke"]) == (expect["action"] == "speak")


def _balanced(rows: Iterable[Mapping[str, Any]]) -> dict:
    rows = list(rows)
    speak = [r for r in rows if r.get("expect") and r["expect"]["action"] == "speak"]
    silent = [r for r in rows if r.get("expect") and r["expect"]["action"] == "silent"]
    speak_recall = metrics.ratio(sum(1 for r in speak if r["spoke"]), len(speak))
    silent_recall = metrics.ratio(sum(1 for r in silent if not r["spoke"]), len(silent))
    parts = [x["rate"] for x in (speak_recall, silent_recall) if x["rate"] is not None]
    return {
        "speak_recall": speak_recall,
        "silent_recall": silent_recall,
        "balanced_accuracy": round(sum(parts) / len(parts), 4) if len(parts) == 2 else None,
    }


def summarize_strategy(rows: Sequence[Mapping[str, Any]], *,
                       price_in: float = 0.0, price_out: float = 0.0) -> dict:
    """Aggregate one strategy's per-case rows into the comparison metrics.

    A case where a model call failed (API error, bad key) is counted under
    ``errored`` and left out of every other number: the router turns such a
    failure into an apology reply, which would otherwise score as "spoke".
    """
    all_rows = list(rows)
    errored = [r for r in all_rows if r.get("model_errors")]
    rows = [r for r in all_rows if not r.get("model_errors")]
    model_rows = [r for r in rows if r["outcome"] in ("engaged_chat", "engaged_proactive")]
    replies = [r["reply"] for r in rows if r["reply"]]
    leaks = [
        r["id"] for r in rows
        if r["reply"] and (reply_text.is_silence_marker(r["reply"])
                           or reply_text.is_silence_explanation(r["reply"]))
    ]
    statuses = Counter(
        r["decision_status"] for r in rows if r.get("decision_status")
    )
    tokens_in = sum(r.get("tokens_in") or 0 for r in rows)
    tokens_out = sum(r.get("tokens_out") or 0 for r in rows)
    calls = sum(r.get("model_calls") or 0 for r in rows)
    seconds = [r["model_seconds"] for r in model_rows if r.get("model_seconds")]
    asked = [
        r for r in all_rows
        if r["outcome"] in ("engaged_chat", "engaged_proactive") or r.get("model_errors")
    ]
    summary = {
        "n_cases": len(all_rows),
        "n_model_cases": len(model_rows),
        "errored": {
            "n": len(errored), "ids": [r["id"] for r in errored],
            "of_model_cases": metrics.ratio(len(errored), len(asked)),
        },
        "all": _balanced(rows),
        "must": _balanced(r for r in rows if r.get("expect") and r["expect"]["strength"] == "must"),
        "model_cases": _balanced(model_rows),
        "by_addressing": {
            kind: _balanced(r for r in rows if r.get("addressing") == kind)
            for kind in ("explicit", "implicit", "private")
            if any(r.get("addressing") == kind for r in rows)
        },
        "leaks": {"n": len(leaks), "ids": leaks, "of_replies": len(replies)},
        "decision_status": {k: statuses[k] for k in sorted(statuses)},
        "unusable_rate": metrics.ratio(
            statuses.get("empty", 0) + statuses.get("parse_failed", 0),
            sum(statuses.values()),
        ),
        "repaired_rate": metrics.ratio(statuses.get("repaired", 0), sum(statuses.values())),
        "model_calls": calls,
        "calls_per_model_case": round(calls / len(model_rows), 3) if model_rows else None,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_s": {"p50": _percentile(seconds, 50), "p95": _percentile(seconds, 95)},
    }
    if price_in or price_out:
        summary["estimated_cost"] = round(
            tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out, 6,
        )
    return summary


def compare(rows_by_strategy: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict]:
    """Pairwise discordant cases: where one strategy was right and the other wrong."""
    out = []
    names = list(rows_by_strategy)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            a = {r["id"]: is_correct(r) for r in rows_by_strategy[first]}
            b = {r["id"]: is_correct(r) for r in rows_by_strategy[second]}
            only_first = sorted(k for k in a if a[k] is True and b.get(k) is False)
            only_second = sorted(k for k in a if a[k] is False and b.get(k) is True)
            out.append({
                "a": first, "b": second,
                "only_a_correct": only_first, "only_b_correct": only_second,
            })
    return out


# ── running ──────────────────────────────────────────────────────────────

def plan(cases: Sequence[dict], strategies: Sequence[str], seed: int) -> dict:
    """Offline routing pass per strategy: how many cases will reach a model."""
    result = {}
    for strategy in strategies:
        routing = run_offline.run_routing(
            cases, seed=seed, profile=run_offline.make_profile(strategy, "dice"),
        )
        asked = [
            c for c in routing["cases"]
            if c["outcome"] in ("engaged_chat", "engaged_proactive")
        ]
        result[strategy] = {
            "model_cases": len(asked),
            # two_step asks a second question for the unclear ones
            "max_calls": len(asked) * (2 if strategy == "two_step" else 1),
        }
    return result


def run_strategy(cases: Sequence[dict], strategy: str, real: Any, *, seed: int,
                 progress: Callable[[str], None] | None = None) -> list[dict]:
    profile = run_offline.make_profile(strategy, "dice")
    rows = []
    with run_offline._hermetic_cwd() as workdir, run_offline._quiet_logging():
        image_path = workdir / "img.png"
        image_path.write_bytes(run_offline._PNG_1X1)
        for index, case in enumerate(cases, 1):
            live = LiveSummarizer(real)
            result = run_offline.replay_case(
                case, workdir=workdir, image_path=str(image_path), seed=seed,
                profile=profile, summarizer=live, keep_reply=True,
            )
            usage = [c.get("usage") or {} for c in live.model_calls]
            model_errors = sum(1 for c in live.model_calls if c.get("error"))
            statuses = [d.status for d in live.decisions]
            rows.append({
                "id": result["id"], "category": result["category"],
                "addressing": result["addressing"], "expect": result["expect"],
                "outcome": result["outcome"], "route": result["route"],
                "route_level": result.get("route_level"),
                "reply": result.get("reply", ""),
                "spoke": bool(result.get("reply")),
                "decision_status": statuses[-1] if statuses else None,
                "decision_action": live.decisions[-1].action if live.decisions else None,
                "model_calls": len(live.model_calls),
                "model_errors": model_errors,
                "tokens_in": sum(u.get("input_tokens") or 0 for u in usage),
                "tokens_out": sum(u.get("output_tokens") or 0 for u in usage),
                "model_seconds": round(live.model_seconds, 3),
                "error": result.get("error", ""),
            })
            if progress:
                progress(f"{strategy} {index}/{len(cases)} {result['id']}")
            tail = [r for r in rows if r["outcome"] in ("engaged_chat", "engaged_proactive")
                    or r["model_errors"]][-ABORT_AFTER_ERRORS:]
            if len(tail) == ABORT_AFTER_ERRORS and all(r["model_errors"] for r in tail):
                raise LiveRunAborted(
                    f"{strategy}: the last {ABORT_AFTER_ERRORS} model calls failed "
                    f"(last error: {live.model_calls[-1].get('error', '')[:120]}). "
                    "Check the API key, the base URL and the network, then run again."
                )
    return rows


def build_report(cases: Sequence[dict], strategies: Sequence[str], real: Any, *,
                 seed: int, price_in: float = 0.0, price_out: float = 0.0,
                 progress: Callable[[str], None] | None = None) -> dict:
    rows_by_strategy = {
        s: run_strategy(cases, s, real, seed=seed, progress=progress) for s in strategies
    }
    return {
        "schema": LIVE_SCHEMA,
        "seed": seed,
        "n_cases": len(cases),
        "strategies": {
            s: summarize_strategy(rows, price_in=price_in, price_out=price_out)
            for s, rows in rows_by_strategy.items()
        },
        "compare": compare(rows_by_strategy),
        "rows": rows_by_strategy,
    }


def render_markdown(report: dict) -> str:
    pct = metrics.fmt_ratio
    lines = ["# Live evaluation", ""]
    lines.append(f"{report['n_cases']} scenarios, seed {report['seed']}. The numbers come "
                 "from a real model on invented (or your own labeled) chats; they compare "
                 "strategies, they are not production accuracy.")
    lines.append("")
    strategies = report["strategies"]
    names = list(strategies)
    lines.append("| metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "---|" * len(names))

    def row(label: str, getter: Callable[[dict], Any]) -> None:
        cells = []
        for name in names:
            value = getter(strategies[name])
            cells.append("n/a" if value is None else
                         pct(value) if isinstance(value, dict) else str(value))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    row("balanced accuracy, all labeled cases", lambda s: s["all"]["balanced_accuracy"])
    row("... speaks when it should", lambda s: s["all"]["speak_recall"])
    row("... stays silent when it should", lambda s: s["all"]["silent_recall"])
    row("balanced accuracy, must-labeled only", lambda s: s["must"]["balanced_accuracy"])
    row("balanced accuracy, cases that reach the model", lambda s: s["model_cases"]["balanced_accuracy"])
    row("cases that hit an API error (left out above)", lambda s: s["errored"]["n"])
    row("leaks (silence text sent)", lambda s: s["leaks"]["n"])
    row("unusable structured answers", lambda s: s["unusable_rate"])
    row("repaired by the extra call", lambda s: s["repaired_rate"])
    row("model calls per model case", lambda s: s["calls_per_model_case"])
    row("tokens in / out", lambda s: f"{s['tokens_in']} / {s['tokens_out']}")
    row("latency p50 / p95 (s)", lambda s: f"{s['latency_s']['p50']} / {s['latency_s']['p95']}")
    if any("estimated_cost" in s for s in strategies.values()):
        row("estimated cost", lambda s: s.get("estimated_cost"))
    lines.append("")
    lines.append("## Where strategies disagree")
    lines.append("")
    for pair in report["compare"]:
        lines.append(f"- **{pair['a']}** right and **{pair['b']}** wrong: "
                     f"{', '.join(pair['only_a_correct']) or '-'}")
        lines.append(f"- **{pair['b']}** right and **{pair['a']}** wrong: "
                     f"{', '.join(pair['only_b_correct']) or '-'}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None,
         summarizer_factory: Callable[[], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_live",
        description="Replay the scenarios against a real model (costs API credit).",
    )
    parser.add_argument("--cases", nargs="+", default=[str(run_offline.DEFAULT_CASES)])
    parser.add_argument("--strategies", nargs="+", choices=STRATEGIES, default=list(STRATEGIES))
    parser.add_argument("--limit", type=int, default=0, help="only the first N scenarios")
    parser.add_argument("--seed", type=int, default=run_offline.DEFAULT_SEED)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--tag", default="live")
    parser.add_argument("--price-in", type=float, default=0.0,
                        help="price per million input tokens, for the cost estimate")
    parser.add_argument("--price-out", type=float, default=0.0,
                        help="price per million output tokens")
    parser.add_argument("--persona-cards", help="a persona card file (evals/persona_cards.py): "
                        "the model speaks as a card instead of the persona in .env")
    parser.add_argument("--persona-id", help="which card of --persona-cards (needed when it has several)")
    parser.add_argument("--yes", action="store_true",
                        help="actually call the model (without it only the plan is printed)")
    args = parser.parse_args(argv)

    cases, _skipped = run_offline._load_cases([Path(p).resolve() for p in args.cases])
    if args.limit:
        cases = cases[: args.limit]
    labeled = [c for c in cases if c.get("expect")]
    print(f"{len(cases)} scenarios ({len(labeled)} labeled), strategies: {', '.join(args.strategies)}")
    estimate = plan(cases, args.strategies, args.seed)
    for strategy, info in estimate.items():
        print(f"  {strategy:<9} reaches the model in {info['model_cases']} cases "
              f"(up to {info['max_calls']} calls, plus repair calls if answers are unusable)")
    if not args.yes:
        print("Dry run: nothing was sent. Add --yes to call the model configured in .env.")
        return 0

    if summarizer_factory is None:
        from src.config import load_config
        from src.summarize import create_models

        config = load_config()
        summarizer_factory = lambda: create_models(config).chat  # noqa: E731  (MODEL_ROUTE decides)
    real = summarizer_factory()
    persona_id = None
    if args.persona_cards:
        from evals.persona_cards import apply_card, load_cards, pick_card
        card = pick_card(load_cards(args.persona_cards), args.persona_id)
        apply_card(real, card)
        persona_id = card["id"]
    try:
        report = build_report(
            cases, args.strategies, real, seed=args.seed,
            price_in=args.price_in, price_out=args.price_out,
            progress=lambda text: print(f"\r{text}    ", end="", file=sys.stderr),
        )
    except LiveRunAborted as error:
        print(f"\nAborted: {error}", file=sys.stderr)
        return 3
    print(file=sys.stderr)
    if persona_id:
        report["persona"] = persona_id
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{args.tag}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    markdown = render_markdown(report)
    (out_dir / f"{args.tag}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out_dir / (args.tag + '.json')} and .md")
    shaky = [
        name for name, s in report["strategies"].items()
        if s["errored"]["of_model_cases"]["rate"] is not None
        and s["errored"]["of_model_cases"]["rate"] > MAX_ERROR_SHARE
    ]
    if shaky:
        print(f"WARNING: more than {MAX_ERROR_SHARE:.0%} of the model cases failed with an API "
              f"error for {', '.join(shaky)}; those numbers are not comparable.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
