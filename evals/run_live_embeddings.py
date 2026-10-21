"""Opt-in: does meaning-based fact search (MEMORY_EMBEDDINGS) put the right facts in the prompt?

    python -m evals.run_live_embeddings --model qwen3-embedding:0.6b          # plan only
    python -m evals.run_live_embeddings --model qwen3-embedding:0.6b --yes    # local Ollama
    python -m evals.run_live_embeddings --model bge-m3 --yes --rows           # every question
    python -m evals.run_live_embeddings --base-url https://api.siliconflow.cn/v1 \\
        --model BAAI/bge-m3 --api-key-env EMBEDDING_API_KEY --yes          # a hosted endpoint

``evals/cases/memory_semantic_v1.jsonl`` holds invented facts ("阿红对海鲜过敏")
and messages that need them without sharing a word with them, said by someone
else ("今晚去吃小龙虾吧"), plus messages that need nothing.  Each message goes
through the real ``rank_facts`` twice: words only (MEMORY_EMBEDDINGS=off) and
with the vectors of the given embeddings model, for several MEMORY_EMBEDDING_MIN_SIM
values.  Recall is the share of the expected facts selected; precision the
share of the selected facts that were expected; "noise" the facts selected
for the messages that need none.

The facts and messages go to the embeddings endpoint (two calls); with the
default URL that is the local Ollama.  Without ``--yes`` only the plan is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.schema import initialize_db  # noqa: E402
from src.db.store import MessageStore  # noqa: E402
from src.memory.embeddings import Embedder, fact_text  # noqa: E402
from src.memory.search import rank_facts  # noqa: E402
from src.memory.store import MemoryStore  # noqa: E402

CASES = REPO_ROOT / "evals" / "cases" / "memory_semantic_v1.jsonl"
DEFAULT_OUT = REPO_ROOT / "data" / "replay" / "live"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
SCHEMA = "live-embeddings-eval/v1"
CHAT = "eval@chatroom"
T0 = 1_760_000_000
THRESHOLDS = (0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7)


def load(path: Path = CASES) -> tuple[list[dict], list[dict]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    facts = next(r["facts"] for r in rows if r["kind"] == "facts")
    return facts, [r for r in rows if r["kind"] == "question"]


def fact_store(facts: Sequence[dict]):
    """An in-memory store holding the facts (each with two quoted pieces of evidence); their ids in order."""
    conn = initialize_db(":memory:")
    mstore = MemoryStore(MessageStore(conn))
    ids = []
    for i, f in enumerate(facts):
        quote = f"{f['subject']}{f['predicate']}{f['object']}"
        fid, _ = mstore.upsert_fact(
            CHAT, kind=f["kind"], subject=f["subject"], predicate=f["predicate"], obj=f["object"],
            confidence=0.9, now=float(T0), evidence=[
                {"message_id": f"f{i}a", "quote": quote, "sender_id": "wxid_x", "sender_name": "甲", "ts": T0},
                {"message_id": f"f{i}b", "quote": quote, "sender_id": "wxid_y", "sender_name": "乙", "ts": T0}],
        )
        ids.append(fid)
    return conn, mstore, ids


def score(selected: dict[str, list[int]], questions: Sequence[dict]) -> dict:
    """Recall over the expected facts, precision over the selected ones, noise on the empty questions."""
    want = found = picked = noise = 0
    for q in questions:
        got = set(selected[q["id"]])
        expect = set(q["expect"])
        if expect:
            want += len(expect)
            found += len(expect & got)
            picked += len(got)
        else:
            noise += len(got)
    empty = sum(1 for q in questions if not q["expect"])
    return {
        "recall": round(found / want, 3) if want else None,
        "precision": round(found / picked, 3) if picked else None,
        "noise_per_empty_question": round(noise / empty, 2) if empty else None,
    }


def evaluate(embedder: Embedder | None, facts: Sequence[dict], questions: Sequence[dict],
             thresholds: Sequence[float] = THRESHOLDS) -> dict:
    conn, mstore, ids = fact_store(facts)
    try:
        def select(q, semantic=None):
            hits = rank_facts(mstore, CHAT, query=q["text"], people=q["people"], now=float(T0),
                              semantic=semantic)
            return [ids.index(h.fact["id"]) for h in hits]

        words = {q["id"]: select(q) for q in questions}
        report = {"words_only": score(words, questions), "with_vectors": {}, "rows": []}
        if embedder is None:
            return report
        stored = mstore.active_facts(CHAT, now=float(T0), min_confidence=0.0)
        fact_vectors = dict(zip((f["id"] for f in stored), embedder.embed([fact_text(f) for f in stored])))
        query_vectors = dict(zip((q["id"] for q in questions), embedder.embed([q["text"] for q in questions])))
        per_threshold = {}
        for min_sim in thresholds:
            picked = {q["id"]: select(q, (query_vectors[q["id"]], fact_vectors, min_sim)) for q in questions}
            report["with_vectors"][str(min_sim)] = score(picked, questions)
            per_threshold[str(min_sim)] = picked
        from src.memory.embeddings import dot
        for q in questions:
            closest = sorted(((dot(query_vectors[q["id"]], fact_vectors[ids[i]]), i) for i in range(len(ids))),
                             reverse=True)[:2]
            report["rows"].append({
                "id": q["id"], "text": q["text"], "expected": q["expect"], "words_only": words[q["id"]],
                "with_vectors": {t: per_threshold[t][q["id"]] for t in per_threshold},
                "closest": [{"fact": i, "similarity": round(s, 3)} for s, i in closest],
            })
        return report
    finally:
        conn.close()


def render(report: dict, model: str) -> str:
    lines = [f"# Meaning-based fact search: {model}", "",
             "| setting | recall | precision | facts per message that needs none |", "| --- | ---: | ---: | ---: |"]

    def row(name, s):
        lines.append(f"| {name} | {s['recall']} | {s['precision']} | {s['noise_per_empty_question']} |")

    row("words only (MEMORY_EMBEDDINGS=off)", report["words_only"])
    for threshold, s in report["with_vectors"].items():
        row(f"vectors, MEMORY_EMBEDDING_MIN_SIM={threshold}", s)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None, embedder_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_live_embeddings", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="embeddings model name on the endpoint")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="", help="name of the environment variable holding the key")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--tag", default="")
    parser.add_argument("--rows", action="store_true", help="print every question")
    parser.add_argument("--yes", action="store_true", help="call the endpoint (without it only the plan)")
    args = parser.parse_args(argv)
    facts, questions = load()
    print(f"{len(facts)} facts, {len(questions)} messages "
          f"({sum(1 for q in questions if not q['expect'])} need none); "
          f"2 calls to {args.base_url} ({args.model})")
    if not args.yes:
        print("Dry run: nothing was sent. Add --yes to call the endpoint.")
        print(render(evaluate(None, facts, questions), "words only"))
        return 0
    if embedder_factory is None:
        key = os.getenv(args.api_key_env, "") if args.api_key_env else ""
        embedder = Embedder(base_url=args.base_url, api_key=key, model=args.model, timeout=120.0)
    else:
        embedder = embedder_factory()
    report = {"schema": SCHEMA, "model": args.model, **evaluate(embedder, facts, questions)}
    markdown = render(report, args.model)
    print(markdown)
    if args.rows:
        for r in report["rows"]:
            print(f"{r['id']} {r['text']} expected {r['expected']} words {r['words_only']} "
                  f"vectors@0.55 {r['with_vectors'].get('0.55')} closest {r['closest']}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = args.tag or "embeddings_" + "".join(c if c.isalnum() else "_" for c in args.model)
    (out / f"{tag}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / f"{tag}.md").write_text(markdown, encoding="utf-8")
    print(f"wrote {out / (tag + '.json')} and .md")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
