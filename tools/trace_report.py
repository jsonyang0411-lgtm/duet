"""Summarise the bot's decision trace: what happened to each message, and why.

Usage:
    python tools/trace_report.py                       # data/decision_trace.jsonl
    python tools/trace_report.py --since 2026-10-15    # only newer records
    python tools/trace_report.py --chat-id 123@chatroom --json
    python tools/trace_report.py --recent 20 --outcome ignored  # why did it NOT answer?
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


def summarize(decisions: Iterable[dict]) -> dict:
    decisions = list(decisions)

    outcomes = Counter(d.get("outcome", "?") for d in decisions)
    handled = len(decisions) - outcomes.get("ignored", 0)
    reasons: dict[str, Counter] = defaultdict(Counter)
    for d in decisions:
        reasons[d.get("reason", "?")][d.get("outcome", "?")] += 1
    ended_by = Counter((d.get("extra") or {}).get("ended_by", "?") for d in decisions)

    per_chat: dict[str, Counter] = defaultdict(Counter)
    for d in decisions:
        key = d.get("group") or d.get("chat_id") or "?"
        per_chat[key]["total"] += 1
        if d.get("outcome") == "reply":
            per_chat[key]["reply"] += 1

    slowest = sorted(decisions, key=lambda d: d.get("elapsed_ms", 0), reverse=True)[:5]

    return {
        "decisions": {
            "total": len(decisions),
            "handled": handled,
            "outcomes": dict(outcomes),
            "reply_rate_of_handled": round(outcomes.get("reply", 0) / handled, 3) if handled else 0.0,
            "reasons": {
                reason: dict(counter) for reason, counter in
                sorted(reasons.items(), key=lambda kv: -sum(kv[1].values()))
            },
            "ended_by": dict(ended_by.most_common()),
            "per_chat": {
                chat: {"total": c["total"], "reply": c["reply"]}
                for chat, c in sorted(per_chat.items(), key=lambda kv: -kv[1]["total"])[:10]
            },
            "slowest": [
                {"reason": d.get("reason"), "elapsed_ms": d.get("elapsed_ms"),
                 "message_id": d.get("message_id")} for d in slowest
            ],
        },
    }


def format_text(summary: dict) -> str:
    d = summary["decisions"]
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
    if d["ended_by"]:
        lines += ["", "ended by step: " + ", ".join(f"{k}={v}" for k, v in d["ended_by"].items())]
    if d["per_chat"]:
        lines += ["", "busiest chats:"]
        for chat, stats in list(d["per_chat"].items())[:5]:
            lines.append(f"  {chat}: {stats['total']} msgs, {stats['reply']} replies")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trace", default="data/decision_trace.jsonl")
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

    decisions = [r for r in iter_jsonl(Path(args.trace)) if keep(r) and r.get("kind", "decision") == "decision"]
    summary = summarize(decisions)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_text(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
