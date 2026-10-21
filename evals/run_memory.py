"""Offline evaluation of the memory rework (Phase 3).

    python -m evals.run_memory [--tag phase3_after] [--out evals/baseline] [--bench]

No network, no model.  Three things are measured on *invented* chats:

1. **Recall questions** - for each question the old search (``search_chat_history``
   with the terms the router extracts) and the new search (FTS5 trigram + LIKE +
   recency, fused) are asked for the message that answers it.  ``hit@k`` is
   "an expected message is among the first k results"; for questions without
   an answer the measure is the share that still returned something.
2. **Fact selection** - given stored facts, which ones would be put in the
   prompt for a message.  The old code injects every alias line it has.
3. **Latency** (``--bench``) - 100 000 synthetic messages, p50 / p95 of both
   searches.

The recall gate (the router only searches when the message looks like a
question about the past) is the same in both versions and not measured here.
The fixtures were written before the first run and the retrieval was not tuned
afterwards, but the thresholds come from the unit tests written earlier by the same
author: treat the numbers as an in-sample measurement on a small set of invented
chats, not as a forecast for real ones.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.schema import initialize_db  # noqa: E402
from src.db.store import MessageStore  # noqa: E402
from src.memory.search import rank_facts, search_messages  # noqa: E402
from src.memory.store import MemoryStore  # noqa: E402
from src.router import MessageRouter  # noqa: E402

CASES = REPO_ROOT / "evals" / "cases" / "memory_v1.jsonl"
BASELINE_DIR = REPO_ROOT / "evals" / "baseline"
CHAT = "eval@chatroom"
T0 = 1_760_000_000
KS = (1, 3, 8)
KINDS = ("exact", "paraphrase", "two_char", "distractor", "old", "no_answer")


def load_cases(path: Path = CASES) -> tuple[list[dict], dict]:
    scenarios, facts = [], {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") == "facts":
            facts = row
        else:
            scenarios.append(row)
    return scenarios, facts


# ── building a chat ──────────────────────────────────────────────────────

def build_store(scenario: dict, *, index: bool):
    conn = initialize_db(":memory:")
    store = MessageStore(conn)
    mstore = MemoryStore(store)
    for i, (sender, text) in enumerate(scenario["chat"]):
        store.insert_message({
            "message_id": f"c{i}", "chat_id": CHAT, "sender_id": f"wxid_{sender}",
            "sender_name": sender, "content": text, "msg_type": 1,
            "timestamp": T0 + i * 60,
        })
    old = T0 - 400 * 86400                      # more than a year before the live chat
    store.upsert_history_messages([
        {"message_id": f"h{i}", "chat_id": CHAT, "sender_id": f"wxid_{sender}",
         "sender_name": sender, "content": text, "msg_type": 1, "source": "eval",
         "timestamp": old + i * 60}
        for i, (sender, text) in enumerate(scenario.get("history", []))
    ])
    if index:
        assert mstore.enable_search_index(), mstore.index_error
        while not mstore.backfill_step():
            pass
    return conn, store, mstore


def expected_ids(question: dict) -> set[str]:
    return {f"{'c' if source == 'chat' else 'h'}{pos}" for source, pos in question["expect"]}


# ── the two searches ─────────────────────────────────────────────────────

class _LegacyTerms:
    """Borrows the router's term extraction without building a router."""

    @staticmethod
    def terms(text: str) -> list[str]:
        # The prefix only opens the router's "this asks about the past" gate;
        # the router's own noise list removes it from the terms again.
        return MessageRouter._history_recall_terms(object(), "之前" + text)


def legacy_search(store: MessageStore, text: str) -> list[str]:
    terms = _LegacyTerms.terms(text)
    if not terms:
        return []
    return [r["message_id"] for r in store.search_chat_history(CHAT, terms, limit=8)]


def v2_search(mstore: MemoryStore, text: str) -> list[str]:
    return [h.message_id for h in search_messages(mstore, CHAT, text, limit=8)]


# ── scoring ──────────────────────────────────────────────────────────────

def score_retrieval(scenarios: list[dict]) -> dict:
    systems = {"legacy": [], "v2_like_only": [], "v2": []}
    for scenario in scenarios:
        for system in systems:
            conn, store, mstore = build_store(scenario, index=system == "v2")
            try:
                for question in scenario["questions"]:
                    if system == "legacy":
                        got = legacy_search(store, question["text"])
                    else:
                        got = v2_search(mstore, question["text"])
                    want = expected_ids(question)
                    rank = next((i for i, m in enumerate(got, 1) if m in want), None)
                    systems[system].append({
                        "id": question["id"], "kind": question["kind"], "text": question["text"],
                        "rank": rank, "returned": len(got), "answerable": bool(want),
                    })
            finally:
                conn.close()
    return {name: summarize(rows) | {"rows": rows} for name, rows in systems.items()}


def summarize(rows: list[dict]) -> dict:
    out: dict = {"questions": len(rows), "by_kind": {}}
    groups = defaultdict(list)
    for row in rows:
        groups[row["kind"]].append(row)
    groups["ALL_ANSWERABLE"] = [r for r in rows if r["answerable"]]
    for kind in (*KINDS, "ALL_ANSWERABLE"):
        subset = groups.get(kind, [])
        if not subset:
            continue
        if kind == "no_answer":
            out["by_kind"][kind] = {
                "n": len(subset),
                "false_alarm": round(sum(1 for r in subset if r["returned"]) / len(subset), 3),
            }
            continue
        entry = {"n": len(subset)}
        for k in KS:
            entry[f"hit@{k}"] = round(
                sum(1 for r in subset if r["rank"] is not None and r["rank"] <= k) / len(subset), 3,
            )
        entry["mean_returned"] = round(statistics.mean(r["returned"] for r in subset), 2)
        out["by_kind"][kind] = entry
    return out


def score_facts(facts_case: dict) -> dict:
    conn = initialize_db(":memory:")
    store = MessageStore(conn)
    mstore = MemoryStore(store)
    ids = []
    for i, f in enumerate(facts_case["facts"]):
        fid, _ = mstore.upsert_fact(
            CHAT, kind=f["kind"], subject=f["subject"], predicate=f["predicate"], obj=f["object"],
            confidence=0.9, now=float(T0), evidence=[
                {"message_id": f"f{i}a", "quote": f"{f['subject']}{f['predicate']}{f['object']}",
                 "sender_id": "wxid_x", "sender_name": "甲", "ts": T0},
                {"message_id": f"f{i}b", "quote": f"{f['subject']}{f['predicate']}{f['object']}",
                 "sender_id": "wxid_y", "sender_name": "乙", "ts": T0}],
        )
        ids.append(fid)
    alias_count = sum(1 for f in facts_case["facts"] if f["kind"] == "alias")
    rows = []
    for q in facts_case["questions"]:
        hits = rank_facts(mstore, CHAT, query=q["text"], people=q["people"], now=float(T0))
        got = [ids.index(h.fact["id"]) for h in hits]
        want = set(q["expect"])
        legacy_got = [i for i, f in enumerate(facts_case["facts"]) if f["kind"] == "alias"]
        rows.append({
            "id": q["id"], "text": q["text"], "expected": sorted(want),
            "v2_selected": got, "legacy_selected": legacy_got,
        })
    conn.close()

    def stats(key: str, representable) -> dict:
        found = total = injected = 0
        for row in rows:
            want = {w for w in row["expected"] if representable(w)}
            total += len(want)
            found += len(want & set(row[key]))
            injected += len(row[key])
        return {
            "recall": round(found / total, 3) if total else None,
            "injected_per_message": round(injected / len(rows), 2),
            "precision": round(found / injected, 3) if injected else None,
        }

    kinds = [f["kind"] for f in facts_case["facts"]]
    return {
        "questions": len(rows), "alias_facts": alias_count,
        "v2": stats("v2_selected", lambda i: True),
        # the old table can only hold alias facts, so only those can be recalled
        "legacy": stats("legacy_selected", lambda i: kinds[i] == "alias"),
        "legacy_note": "old extractor stores aliases only; recall counted over alias facts",
        "rows": rows,
    }


# ── latency ─────────────────────────────────────────────────────────────

VOCAB = [w for w in (
    "今天 明天 周末 公司 项目 会议 火锅 烧烤 咖啡 奶茶 电影 游戏 旅游 出差 加班 请假 生日 红包 "
    "地铁 高铁 飞机 手机 电脑 网络 服务器 测试 上线 报销 老板 同事 朋友 孩子 作业 考试 运动 跑步 "
    "天气 下雨 太阳 晚饭 早餐 外卖 超市 快递 装修 房租 工资 预算 方案 合同 客户 简历 面试 培训"
).split()]


def bench(n_messages: int = 100_000, queries: int = 60) -> dict:
    rng = random.Random(7)
    conn = initialize_db(":memory:")
    store = MessageStore(conn)
    mstore = MemoryStore(store)
    started = time.perf_counter()
    rows = []
    for i in range(n_messages):
        text = "".join(rng.choice(VOCAB) + rng.choice(("", "，", "的", "了")) for _ in range(rng.randint(3, 7)))
        rows.append((f"b{i}", CHAT, f"wxid_{i % 7}", f"人{i % 7}", text, 1, T0 + i * 30))
    with conn:
        conn.executemany(
            "INSERT INTO messages(message_id, chat_id, sender_id, sender_name, content, msg_type, timestamp) "
            "VALUES (?,?,?,?,?,?,?)", rows,
        )
    load_s = time.perf_counter() - started

    started = time.perf_counter()
    mstore.enable_search_index()
    while not mstore.backfill_step(batch=20000):
        pass
    index_s = time.perf_counter() - started

    asked = [
        rng.choice(VOCAB) + rng.choice(VOCAB) + rng.choice(("", "的", "了")) + rng.choice(VOCAB)
        for _ in range(queries)
    ]
    timings = {"legacy": [], "v2": []}
    for q in asked:
        t = time.perf_counter()
        store.search_chat_history(CHAT, _LegacyTerms.terms(q) or [q[:2]], limit=8)
        timings["legacy"].append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        search_messages(mstore, CHAT, q, limit=8)
        timings["v2"].append((time.perf_counter() - t) * 1000)
    conn.close()

    def pct(values, p):
        values = sorted(values)
        return round(values[min(len(values) - 1, int(len(values) * p))], 2)

    return {
        "messages": n_messages, "load_s": round(load_s, 1), "index_build_s": round(index_s, 1),
        "queries": queries,
        "legacy_ms": {"p50": pct(timings["legacy"], 0.5), "p95": pct(timings["legacy"], 0.95)},
        "v2_ms": {"p50": pct(timings["v2"], 0.5), "p95": pct(timings["v2"], 0.95)},
    }


# ── report ──────────────────────────────────────────────────────────────

def build_report(*, with_bench: bool) -> dict:
    scenarios, facts_case = load_cases()
    report = {
        "schema": "memory-eval/v1",
        "scenarios": len(scenarios),
        "retrieval": score_retrieval(scenarios),
        "facts": score_facts(facts_case),
    }
    if with_bench:
        report["bench"] = bench()
    return report


def render_md(report: dict, tag: str) -> str:
    out = [f"# memory evaluation - {tag}", "",
           f"{report['scenarios']} invented chats, "
           f"{report['retrieval']['legacy']['questions']} recall questions.", "",
           "## Recall questions", ""]
    header = "| kind | n | " + " | ".join(f"hit@{k}" for k in KS) + " |"
    for name, label in (("legacy", "old LIKE search"), ("v2_like_only", "new, no index (LIKE fallback)"),
                        ("v2", "new, FTS5 + LIKE + recency")):
        out += [f"### {label}", "", header, "|---|---|" + "---|" * len(KS)]
        for kind, entry in report["retrieval"][name]["by_kind"].items():
            if kind == "no_answer":
                out.append(f"| no_answer (false alarm) | {entry['n']} | {entry['false_alarm']} | | |")
            else:
                out.append(f"| {kind} | {entry['n']} | " + " | ".join(str(entry[f'hit@{k}']) for k in KS) + " |")
        out.append("")
    facts = report["facts"]
    out += ["## Fact selection", "",
            "| | recall | injected / message | precision |", "|---|---|---|---|",
            f"| old (every alias line) | {facts['legacy']['recall']} | {facts['legacy']['injected_per_message']} | {facts['legacy']['precision']} |",
            f"| new (relevance-ranked) | {facts['v2']['recall']} | {facts['v2']['injected_per_message']} | {facts['v2']['precision']} |",
            "", f"_{facts['legacy_note']}_", ""]
    if "bench" in report:
        b = report["bench"]
        out += ["## Latency", "",
                f"{b['messages']} messages (load {b['load_s']} s, index build {b['index_build_s']} s), {b['queries']} queries.", "",
                "| | p50 ms | p95 ms |", "|---|---|---|",
                f"| old | {b['legacy_ms']['p50']} | {b['legacy_ms']['p95']} |",
                f"| new | {b['v2_ms']['p50']} | {b['v2_ms']['p95']} |", ""]
    return "\n".join(out)


def strip_rows(report: dict) -> dict:
    slim = json.loads(json.dumps(report))
    for system in slim["retrieval"].values():
        system.pop("rows", None)
    slim["facts"].pop("rows", None)
    return slim


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tag", default="current_memory")
    parser.add_argument("--out", default=str(BASELINE_DIR))
    parser.add_argument("--bench", action="store_true", help="also time 100k messages (about 30 s)")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--rows", action="store_true", help="print every question with its rank")
    args = parser.parse_args(argv)

    report = build_report(with_bench=args.bench)
    text = render_md(report, args.tag)
    print(text)
    if args.rows:
        for name, data in report["retrieval"].items():
            print(f"\n-- {name} --")
            for row in data["rows"]:
                print(f"{row['id']:<12} {row['kind']:<12} rank={row['rank']} returned={row['returned']}  {row['text']}")
    if not args.no_write:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{args.tag}.json").write_text(
            json.dumps(strip_rows(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out / f"{args.tag}.md").write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out / (args.tag + '.json')} and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
