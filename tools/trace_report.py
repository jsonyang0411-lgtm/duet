"""Summarise the bot's decision trace and model-call log.

Usage:
    python tools/trace_report.py                       # data/decision_trace.jsonl + data/llm_calls.jsonl
    python tools/trace_report.py --since 2026-10-15    # only newer records
    python tools/trace_report.py --chat-id 123@chatroom --json
    python tools/trace_report.py --recent 20 --outcome silent   # why did it NOT answer?
    python tools/trace_report.py --why <message_id>             # one message, step by step

Reads the size-rotated backups (``.3`` ... ``.1``) before the live file, so a
long run is reported as one stream.  Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional


def iter_jsonl(path: Path, backups: int = 5) -> Iterator[dict]:
    """Yield records from ``path`` and its rotated backups, oldest first."""
    candidates = [path.with_name(f"{path.name}.{i}") for i in range(backups, 0, -1)]
    candidates.append(path)
    for file in candidates:
        if not file.exists():
            continue
        with open(file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def parse_since(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value).timestamp()


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return round(ordered[index], 1)


_FALLBACK_REASONS = ("ai_blank_direct_fallback", "ai_blank_forced_fallback")


def summarize_pipeline(decisions: list[dict], llm_calls: list[dict]) -> Optional[dict]:
    """Numbers for the structured pipeline (PIPELINE_V2), or None if it never ran."""
    structured = [
        d for d in decisions if (d.get("extra") or {}).get("decision")
    ]
    if not structured:
        return None
    levels: Counter = Counter()
    actions: Counter = Counter()
    statuses: Counter = Counter()
    strategies: Counter = Counter()
    calls = 0
    by_level: dict[str, Counter] = defaultdict(Counter)
    for d in structured:
        extra = d["extra"]
        decision = extra["decision"]
        level = (extra.get("addressing") or {}).get("level", "?")
        levels[level] += 1
        actions[decision.get("action", "?")] += 1
        statuses[decision.get("status", "?")] += 1
        strategies[decision.get("strategy", "?")] += 1
        calls += int(decision.get("calls", 1) or 1)
        by_level[level][d.get("outcome", "?")] += 1
    n = len(structured)
    total = sum(statuses.values())
    unusable = statuses.get("empty", 0) + statuses.get("parse_failed", 0)
    attempts = Counter(c.get("attempt", "?") for c in llm_calls if c.get("attempt"))
    usage_in = sum(int((c.get("usage") or {}).get("input_tokens") or 0) for c in llm_calls)
    usage_out = sum(int((c.get("usage") or {}).get("output_tokens") or 0) for c in llm_calls)
    native = [c for c in llm_calls if "native_json" in c]
    return {
        "decisions": n,
        "strategies": dict(strategies),
        "levels": dict(levels),
        "actions": dict(actions),
        "status": dict(statuses),
        "unusable_rate": round(unusable / total, 4) if total else 0.0,
        "repaired_rate": round(statuses.get("repaired", 0) / total, 4) if total else 0.0,
        "model_calls_per_decision": round(calls / n, 3),
        "attempts": dict(attempts),
        "native_json_share": (
            round(sum(1 for c in native if c["native_json"]) / len(native), 3) if native else None
        ),
        "tokens": {"input": usage_in, "output": usage_out},
        "fallbacks": sum(1 for d in decisions if d.get("reason") in _FALLBACK_REASONS),
        "outcome_by_level": {lv: dict(c) for lv, c in sorted(by_level.items())},
    }


def summarize_memory(decisions: list[dict], llm_calls: list[dict]) -> Optional[dict]:
    """Numbers for memory v2 (MEMORY_V2): what was injected, what the job cost."""
    injected = [
        step for d in decisions for step in (d.get("steps") or [])
        if step.get("code") == "memory_v2"
    ]
    jobs = [c for c in llm_calls if c.get("call") == "memory_extract"]
    if not injected and not jobs:
        return None
    totals = {"facts": 0, "episodes": 0, "messages": 0}
    for step in injected:
        for key in totals:
            totals[key] += int(step.get(key, 0) or 0)
    latencies = [float(c["latency_ms"]) for c in jobs if c.get("latency_ms") is not None]
    return {
        "prompts_with_memory": len(injected),
        "injected": totals,
        "avg_facts_per_prompt": round(totals["facts"] / len(injected), 2) if injected else 0.0,
        "extraction_calls": len(jobs),
        "extraction_errors": sum(1 for c in jobs if c.get("error")),
        "extraction_tokens": {
            "input": sum(int((c.get("usage") or {}).get("input_tokens") or 0) for c in jobs),
            "output": sum(int((c.get("usage") or {}).get("output_tokens") or 0) for c in jobs),
        },
        "extraction_latency_ms": {
            "p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95),
        },
    }


def summarize(decisions: Iterable[dict], llm_calls: Iterable[dict]) -> dict:
    decisions = list(decisions)
    llm_calls = list(llm_calls)

    outcomes = Counter(d.get("outcome", "?") for d in decisions)
    handled = len(decisions) - outcomes.get("ignored", 0)
    reasons: dict[str, Counter] = defaultdict(Counter)
    for d in decisions:
        reasons[d.get("reason", "?")][d.get("outcome", "?")] += 1

    per_chat: dict[str, Counter] = defaultdict(Counter)
    for d in decisions:
        key = d.get("group") or d.get("chat_id") or "?"
        per_chat[key]["total"] += 1
        if d.get("outcome") == "reply":
            per_chat[key]["reply"] += 1

    slowest = sorted(decisions, key=lambda d: d.get("elapsed_ms", 0), reverse=True)[:5]

    kinds = Counter(c.get("call", "?") for c in llm_calls)
    latencies = [float(c.get("latency_ms", 0) or 0) for c in llm_calls]
    errors = sum(1 for c in llm_calls if c.get("error"))
    empty_after_clean = sum(
        1 for c in llm_calls
        if str(c.get("raw_reply", "") or "").strip()
        and not str(c.get("cleaned_reply", "") or "").strip()
    )
    intentional_silence = sum(1 for c in llm_calls if c.get("intentional_silence"))
    blank_raw = sum(
        1 for c in llm_calls
        if not str(c.get("raw_reply", "") or "").strip() and not c.get("error")
    )
    truncated = 0
    for c in llm_calls:
        raw = str(c.get("raw_reply", "") or "").strip()
        cleaned = str(c.get("cleaned_reply", "") or "")
        limit = c.get("max_chars")
        if limit and cleaned and len(raw) > len(cleaned) >= int(limit):
            truncated += 1

    pipeline = summarize_pipeline(decisions, llm_calls)
    summary = {
        "decisions": {
            "total": len(decisions),
            "handled": handled,
            "outcomes": dict(outcomes),
            "reply_rate_of_handled": round(outcomes.get("reply", 0) / handled, 3) if handled else 0.0,
            "reasons": {
                reason: dict(counter) for reason, counter in
                sorted(reasons.items(), key=lambda kv: -sum(kv[1].values()))
            },
            "per_chat": {
                chat: {"total": c["total"], "reply": c["reply"]}
                for chat, c in sorted(per_chat.items(), key=lambda kv: -kv[1]["total"])[:10]
            },
            "slowest": [
                {"reason": d.get("reason"), "elapsed_ms": d.get("elapsed_ms"),
                 "message_id": d.get("message_id")} for d in slowest
            ],
        },
        "llm_calls": {
            "total": len(llm_calls),
            "by_kind": dict(kinds),
            "latency_ms": {
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "max": round(max(latencies), 1) if latencies else 0.0,
            },
            "errors": errors,
            "blank_raw_reply": blank_raw,
            "intentional_silence": intentional_silence,
            "cleaned_to_empty": empty_after_clean,
            "hard_truncated": truncated,
        },
    }
    if pipeline is not None:
        summary["pipeline"] = pipeline
    memory = summarize_memory(decisions, llm_calls)
    if memory is not None:
        summary["memory"] = memory
    return summary


def format_text(summary: dict) -> str:
    d, c = summary["decisions"], summary["llm_calls"]
    lines = [
        "== Decisions ==",
        f"messages traced: {d['total']}  (handled {d['handled']}, "
        f"reply rate of handled {d['reply_rate_of_handled']:.1%})",
        "outcomes: " + ", ".join(f"{k}={v}" for k, v in d["outcomes"].items()),
        "",
        f"{'reason':<34}{'count':>7}  outcomes",
    ]
    for reason, counter in list(d["reasons"].items())[:25]:
        lines.append(
            f"{reason:<34}{sum(counter.values()):>7}  "
            + ", ".join(f"{k}={v}" for k, v in counter.items())
        )
    if d["per_chat"]:
        lines += ["", "busiest chats:"]
        for chat, stats in list(d["per_chat"].items())[:5]:
            lines.append(f"  {chat}: {stats['total']} msgs, {stats['reply']} replies")
    lines += [
        "",
        "== Model calls ==",
        f"calls: {c['total']}  by kind: {c['by_kind']}",
        f"latency ms: p50={c['latency_ms']['p50']} p95={c['latency_ms']['p95']} "
        f"max={c['latency_ms']['max']}",
        f"errors={c['errors']}  blank raw reply={c['blank_raw_reply']}  "
        f"intentional silence={c['intentional_silence']}",
        f"cleaned to empty (possible false kill)={c['cleaned_to_empty']}  "
        f"hard-truncated={c['hard_truncated']}",
    ]
    p = summary.get("pipeline")
    if p:
        lines += [
            "",
            "== Structured pipeline ==",
            f"decisions: {p['decisions']}  strategy: {p['strategies']}",
            "addressing levels: " + ", ".join(f"{k}={v}" for k, v in p["levels"].items()),
            "model said: " + ", ".join(f"{k}={v}" for k, v in p["actions"].items()),
            "answer status: " + ", ".join(f"{k}={v}" for k, v in p["status"].items())
            + f"  (unusable {p['unusable_rate']:.1%}, repaired {p['repaired_rate']:.1%})",
            f"model calls per decision: {p['model_calls_per_decision']}  "
            f"attempts: {p['attempts']}",
            f"tokens: in={p['tokens']['input']} out={p['tokens']['output']}  "
            f"native JSON mode: {p['native_json_share']}",
            f"direct-call fallbacks sent: {p['fallbacks']}",
        ]
        for level, counter in p["outcome_by_level"].items():
            lines.append(
                f"  {level:<9} -> " + ", ".join(f"{k}={v}" for k, v in counter.items())
            )
    m = summary.get("memory")
    if m:
        lines += [
            "",
            "== Memory v2 ==",
            f"prompts with memory: {m['prompts_with_memory']}  injected: {m['injected']}  "
            f"facts per prompt: {m['avg_facts_per_prompt']}",
            f"extraction calls: {m['extraction_calls']}  errors: {m['extraction_errors']}  "
            f"tokens: in={m['extraction_tokens']['input']} out={m['extraction_tokens']['output']}  "
            f"latency p50/p95: {m['extraction_latency_ms']['p50']}/{m['extraction_latency_ms']['p95']} ms",
        ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trace", default="data/decision_trace.jsonl")
    parser.add_argument("--llm", default="data/llm_calls.jsonl")
    parser.add_argument("--since", help="epoch seconds or ISO date/time")
    parser.add_argument("--chat-id", action="append", default=[])
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--recent", type=int, metavar="N", help="explain the newest N decisions instead of summarising")
    parser.add_argument("--outcome", choices=("reply", "silent", "ignored", "error"), default="",
                        help="with --recent: only this outcome")
    parser.add_argument("--why", metavar="MESSAGE_ID", help="explain the decision for one message")
    args = parser.parse_args(argv)

    if args.recent is not None or args.why:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src import decision_explain
        items = decision_explain.recent(
            Path(args.trace), limit=args.recent if args.recent else 5, outcome=args.outcome,
            chat_id=args.chat_id, message_id=args.why or "", since=parse_since(args.since),
        )
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(decision_explain.format_text(items) or "(no matching decisions)")
        return 0 if items else 1

    since = parse_since(args.since)
    chats = set(args.chat_id)

    def keep(rec: dict) -> bool:
        if since is not None and float(rec.get("ts", 0) or 0) < since:
            return False
        if chats and rec.get("chat_id") not in chats:
            return False
        return True

    decisions = [r for r in iter_jsonl(Path(args.trace)) if keep(r)]
    llm_calls = list(iter_jsonl(Path(args.llm)))
    if since is not None:
        llm_calls = [r for r in llm_calls if float(r.get("ts", 0) or 0) >= since]
    if chats:
        ids = {d.get("message_id") for d in decisions}
        llm_calls = [r for r in llm_calls if r.get("message_id") in ids]

    summary = summarize(decisions, llm_calls)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_text(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
