"""Opt-in live evaluation of the memory extraction job with a real model.

    python -m evals.run_live_memory                 # plan only: windows and a token estimate
    python -m evals.run_live_memory --yes           # run it (costs API credit)
    python -m evals.run_live_memory --yes --price-in 0.27 --price-out 1.10

The invented chats of ``evals/cases/memory_v1.jsonl`` go through the real
:class:`~src.memory.worker.MemoryWorker` and the summarizer configured in your
``.env``.  Nothing is sent to WeChat and no real chat data is involved.

What it answers (the offline tests cannot): does the real model produce JSON
the validator accepts, how much of what it says is rejected and why, does it
find the facts a person would write down, and what does a window cost.  Every
stored fact is re-checked against the stored messages afterwards, so the
"traceable" figure is measured, not assumed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.run_memory import CHAT, T0, build_store, load_cases  # noqa: E402
from src.memory.text import canon, quote_matches  # noqa: E402
from src.memory.worker import MemoryWorker  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "data" / "replay" / "live"
SCHEMA = "live-memory-eval/v1"
CHARS_PER_TOKEN = 1.6


def estimate(scenarios: Sequence[dict], window: int = 120) -> dict:
    messages = sum(len(s["chat"]) for s in scenarios)
    chars = sum(len(t) for s in scenarios for _sender, t in s["chat"])
    windows = sum(-(-len(s["chat"]) // window) for s in scenarios)
    return {
        "scenarios": len(scenarios), "messages": messages, "windows": windows,
        "input_tokens": int(chars / CHARS_PER_TOKEN) + windows * 900,
        "output_tokens": windows * 600,
    }


def _fact_text(fact: dict) -> str:
    return canon(f"{fact['subject']}{fact['predicate']}{fact['object']}")


def found(expected: Sequence[str], facts: Sequence[dict]) -> bool:
    """An expected fact is ('subject keyword', 'a|b' content keywords)."""
    subject, content = expected
    wanted = [canon(c) for c in content.split("|")]
    return any(
        canon(subject) in _fact_text(f) and any(w in _fact_text(f) for w in wanted)
        for f in facts
    )


def run_scenario(scenario: dict, summarizer, *, window: int = 120) -> dict:
    conn, store, mstore = build_store(scenario, index=False)
    try:
        worker = MemoryWorker(mstore, summarizer, window_messages=window, max_window=window,
                              first_window=10 ** 6, clock=lambda: T0 + 10 ** 6)
        worker.note_message(CHAT)
        results = []
        for _ in range(-(-len(scenario["chat"]) // window) + 1):
            if mstore.count_pending(CHAT, (mstore.get_progress(CHAT) or {"last_row_id": 0})["last_row_id"])[0] == 0:
                break
            results.append(worker.run_window(CHAT, T0 + 10 ** 6))
        facts = mstore.list_facts(CHAT, status=None)
        traceable = total = 0
        for fact in facts:
            for ev in mstore.get_evidence("fact", fact["id"]):
                total += 1
                row = conn.execute("SELECT content FROM messages WHERE message_id = ?",
                                   (ev["message_id"],)).fetchone()
                traceable += bool(row and quote_matches(ev["quote"], row["content"]))
        episodes = mstore.recent_episodes(CHAT, limit=50)
        usage = [r.usage or {} for r in results]
        expected = scenario.get("expect_facts", [])
        return {
            "id": scenario["id"],
            "windows": [{"status": r.status, "messages": r.messages, "detail": r.detail} for r in results],
            "facts": [{k: f[k] for k in ("kind", "subject", "predicate", "object", "confidence")} for f in facts],
            "episodes": [e["summary"] for e in episodes],
            "expected": [list(e) for e in expected],
            "expected_found": [list(e) for e in expected if found(e, facts)],
            "evidence_rows": total, "evidence_traceable": traceable,
            "input_tokens": sum(u.get("input_tokens") or 0 for u in usage),
            "output_tokens": sum(u.get("output_tokens") or 0 for u in usage),
        }
    finally:
        conn.close()


def summarize(runs: Sequence[dict], price_in: float = 0.0, price_out: float = 0.0) -> dict:
    rejected = Counter()
    statuses = Counter()
    accepted = 0
    for run in runs:
        for w in run["windows"]:
            statuses[w["status"]] += 1
            detail = w.get("detail") or {}
            accepted += detail.get("facts_created", 0) + detail.get("facts_supported", 0)
            rejected.update(detail.get("rejected_reasons", []))
    expected = sum(len(r["expected"]) for r in runs)
    hit = sum(len(r["expected_found"]) for r in runs)
    tin = sum(r["input_tokens"] for r in runs)
    tout = sum(r["output_tokens"] for r in runs)
    rows = sum(r["evidence_rows"] for r in runs)
    ok_rows = sum(r["evidence_traceable"] for r in runs)
    proposed = accepted + sum(rejected.values())
    return {
        "windows": dict(statuses),
        "facts_accepted": accepted, "facts_rejected": sum(rejected.values()),
        "reject_reasons": dict(rejected.most_common()),
        "accepted_share": round(accepted / proposed, 3) if proposed else None,
        "expected_facts": expected, "expected_found": hit,
        "expected_recall": round(hit / expected, 3) if expected else None,
        "evidence_traceable": round(ok_rows / rows, 3) if rows else None,
        "episodes": sum(len(r["episodes"]) for r in runs),
        "input_tokens": tin, "output_tokens": tout,
        "cost": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 4) if (price_in or price_out) else None,
    }


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [f"# live memory evaluation - {report['tag']}", "",
             f"model: {report.get('model') or 'unknown'}", "",
             "| measure | value |", "|---|---|",
             f"| windows | {s['windows']} |",
             f"| facts accepted / rejected | {s['facts_accepted']} / {s['facts_rejected']} |",
             f"| accepted share of what the model proposed | {s['accepted_share']} |",
             f"| expected facts found | {s['expected_found']} of {s['expected_facts']} ({s['expected_recall']}) |",
             f"| evidence rows re-verified | {s['evidence_traceable']} |",
             f"| episodes | {s['episodes']} |",
             f"| tokens in / out | {s['input_tokens']} / {s['output_tokens']} |",
             f"| cost | {s['cost']} |", ""]
    if s["reject_reasons"]:
        lines += ["Rejected because: " + ", ".join(f"{k} x{v}" for k, v in s["reject_reasons"].items()), ""]
    for run in report["runs"]:
        lines += [f"## {run['id']}", ""]
        lines += [f"- {f['kind']}: {f['subject']} {f['predicate']} {f['object']} ({f['confidence']})" for f in run["facts"]]
        missing = [e for e in run["expected"] if e not in run["expected_found"]]
        if missing:
            lines += ["", "not found: " + "; ".join(f"{a}/{b}" for a, b in missing)]
        lines += [f"- episode: {e}" for e in run["episodes"]] + [""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None,
         summarizer_factory: Callable[[], object] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="really call the model (costs credit)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--tag", default="memory")
    parser.add_argument("--window", type=int, default=120)
    parser.add_argument("--price-in", type=float, default=0.0, help="USD per million input tokens")
    parser.add_argument("--price-out", type=float, default=0.0, help="USD per million output tokens")
    args = parser.parse_args(argv)

    scenarios, _facts = load_cases()
    plan = estimate(scenarios, args.window)
    print(json.dumps(plan, ensure_ascii=False))
    if not args.yes:
        print("nothing sent; add --yes to run it")
        return 0
    if summarizer_factory is None:
        from src.config import load_config
        from src.summarize import create_models
        config = load_config()
        summarizer = create_models(config).memory          # MODEL_ROUTE decides which model
        model = getattr(summarizer, "chat_model", None) or getattr(summarizer, "model", "")
    else:
        summarizer, model = summarizer_factory(), "scripted"
    if not callable(getattr(summarizer, "complete_json", None)):
        print("the configured AI backend cannot run JSON tasks")
        return 1

    runs = [run_scenario(s, summarizer, window=args.window) for s in scenarios]
    report = {"schema": SCHEMA, "tag": args.tag, "model": model,
              "summary": summarize(runs, args.price_in, args.price_out), "runs": runs}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.tag}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text = render_markdown(report)
    (out / f"{args.tag}.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"wrote {out / (args.tag + '.json')} and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
