"""Retrieval over stored messages, facts and episodes.

Messages: FTS5 trigram (ranked by bm25) + a LIKE fallback for two-character
words + recency, fused with reciprocal rank fusion.  Facts and episodes are
few, so they are scored in Python.  The retriever only reads.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .embeddings import dot
from .store import MemoryStore
from .text import bigrams, canon, content_bigrams, overlap, query_grams, short_terms

RRF_K = 60

# Hits that share less than this fraction of a long query's trigrams are noise.
MIN_COVERAGE_LONG_QUERY = 0.2
LONG_QUERY_PAIRS = 6


@dataclass(frozen=True)
class MessageHit:
    message_id: str
    sender_id: str
    sender_name: str
    content: str
    timestamp: int
    source: str                      # "live" | "history"
    score: float
    via: tuple[str, ...] = ()        # which lists found it: fts / like / recent


@dataclass(frozen=True)
class FactHit:
    fact: dict
    score: float
    evidence: list = field(default_factory=list)


def _dedup_key(row: dict) -> tuple:
    return (row["sender_id"], row["content"], row["timestamp"])


def search_messages(mstore: MemoryStore, chat_id: str, query: str, *,
                    limit: int = 8, exclude_ids: frozenset[str] | set[str] = frozenset(),
                    before_ts: int | None = None) -> list[MessageHit]:
    """Messages (live and imported) that answer ``query``, best first."""
    grams = query_grams(query)
    strong, weak = short_terms(query)
    fts_rows = mstore.fts_search(chat_id, grams, limit=60) if grams else []
    use_fts = bool(grams) and fts_rows is not None       # None: no usable index right now
    fts_rows = fts_rows or []

    like_terms = list(strong)
    if use_fts:
        if len(fts_rows) < limit:
            like_terms += weak
    else:
        # No usable trigram index: emulate it with a few of the trigrams.
        step = max(1, len(grams) // 8)
        like_terms += grams[::step][:8] + weak
    like_rows = mstore.like_search(chat_id, like_terms, limit=60) if like_terms else []

    candidates: dict[tuple, dict] = {}
    for rank, row in enumerate(fts_rows):
        entry = candidates.setdefault(_dedup_key(row), {"row": row, "via": set()})
        entry["via"].add("fts")
        entry["fts_rank"] = min(rank, entry.get("fts_rank", rank))
    matches: dict[tuple, int] = {}
    for row in like_rows:
        key = _dedup_key(row)
        entry = candidates.setdefault(key, {"row": row, "via": set()})
        entry["via"].add("like")
        haystack = canon(row["content"])
        matches[key] = sum(1 for t in like_terms if canon(t) in haystack)
    if not candidates:
        return []

    # Drop the question itself, anything the caller already shows, and (when
    # asked) everything newer than ``before_ts``.
    for key in list(candidates):
        row = candidates[key]["row"]
        if str(row["message_id"]) in exclude_ids:
            del candidates[key]
        elif before_ts is not None and int(row["timestamp"]) > before_ts:
            del candidates[key]

    wanted = content_bigrams(query)
    if len(wanted) >= LONG_QUERY_PAIRS:
        # A long question must share a real part of its content words with the
        # hit; one shared word is how unrelated old messages sneak in.
        for key in list(candidates):
            if overlap(wanted, candidates[key]["row"]["content"]) < MIN_COVERAGE_LONG_QUERY:
                del candidates[key]
    if not candidates:
        return []

    scores: dict[tuple, float] = {key: 0.0 for key in candidates}
    for key, entry in candidates.items():
        if "fts_rank" in entry:
            scores[key] += 1.0 / (RRF_K + entry["fts_rank"])
    like_order = sorted(
        (k for k in candidates if "like" in candidates[k]["via"]),
        key=lambda k: (-matches.get(k, 0), -int(candidates[k]["row"]["timestamp"])),
    )
    for rank, key in enumerate(like_order):
        scores[key] += 1.0 / (RRF_K + rank)
    recency_order = sorted(
        candidates, key=lambda k: -int(candidates[k]["row"]["timestamp"]),
    )
    for rank, key in enumerate(recency_order):
        scores[key] += 1.0 / (RRF_K + rank)
        candidates[key]["via"].add("recent")

    ranked = sorted(
        candidates, key=lambda k: (-scores[k], -int(candidates[k]["row"]["timestamp"])),
    )
    hits = []
    for key in ranked[:max(1, int(limit))]:
        row = candidates[key]["row"]
        hits.append(MessageHit(
            message_id=str(row["message_id"]), sender_id=str(row["sender_id"]),
            sender_name=str(row["sender_name"]), content=str(row["content"]),
            timestamp=int(row["timestamp"]), source=str(row.get("source", "live")),
            score=round(scores[key], 6),
            via=tuple(sorted(candidates[key]["via"] - {"recent"})) or ("recent",),
        ))
    return hits


# ── facts ───────────────────────────────────────────────────────────────

# Facts about someone who is talking or being talked about matter even when
# the message shares no words with them.
SUBJECT_WEIGHT = 0.5
OVERLAP_WEIGHT = 0.4
CONFIDENCE_WEIGHT = 0.1
MIN_FACT_SCORE = 0.3
RECENCY_HALF_LIFE_DAYS = 60.0
# MEMORY_EMBEDDINGS: a fact whose meaning is close to the message counts like a
# word match.  Similarity is rescaled so min_sim is 0 and a perfect match is 1.
SEMANTIC_WEIGHT = 0.5


def semantic_closeness(similarity: float, min_sim: float) -> float:
    if similarity <= min_sim or min_sim >= 1.0:
        return 0.0
    return min(1.0, (similarity - min_sim) / (1.0 - min_sim))


def rank_facts(mstore: MemoryStore, chat_id: str, *, query: str,
               people: list[str], now: float | None = None,
               min_confidence: float = 0.6, limit: int = 12,
               semantic: tuple | None = None) -> list[FactHit]:
    """Facts worth injecting, best first.

    ``semantic`` is ``(query_vector, {fact_id: vector}, min_sim)`` with
    MEMORY_EMBEDDINGS on; without it the ranking is words only, as before.
    """
    now = time.time() if now is None else now
    facts = mstore.active_facts(chat_id, now=now, min_confidence=min_confidence)
    if not facts:
        return []
    q = bigrams(query)
    names = {canon(p) for p in people if canon(p)}
    spoken = canon(query)
    scored: list[tuple[float, dict]] = []
    for fact in facts:
        subject = canon(fact["subject"])
        subject_hit = 1.0 if subject and (
            subject in names or (len(subject) >= 2 and subject in spoken)
        ) else 0.0
        text = f'{fact["subject"]}{fact["predicate"]}{fact["object"]}'
        # An alias is worth injecting when the alias itself is mentioned.
        mention = 1.0 if (
            canon(fact["object"]) and len(canon(fact["object"])) >= 2
            and canon(fact["object"]) in canon(query)
            and fact["kind"] in ("alias", "jargon")
        ) else 0.0
        text_overlap = max(overlap(q, text), mention)
        close = 0.0
        if semantic is not None:
            query_vector, vectors, min_sim = semantic
            vector = vectors.get(fact["id"])
            if vector is not None:
                close = semantic_closeness(dot(query_vector, vector), min_sim)
        score = (
            SUBJECT_WEIGHT * subject_hit
            + OVERLAP_WEIGHT * text_overlap
            + CONFIDENCE_WEIGHT * float(fact["confidence"])
            + SEMANTIC_WEIGHT * close
        )
        age_days = max(0.0, (now - float(fact["last_seen_at"])) / 86400.0)
        score += 0.05 * math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)
        if subject_hit == 0.0 and text_overlap == 0.0 and close == 0.0:
            continue                      # unrelated to anyone present or anything said
        # A meaning match above MEMORY_EMBEDDING_MIN_SIM is enough on its own:
        # that setting is the threshold, not the general score floor.
        if score >= MIN_FACT_SCORE or close > 0.0:
            scored.append((score, fact))
    scored.sort(key=lambda item: (-item[0], -float(item[1]["updated_at"])))
    hits: list[FactHit] = []
    for score, fact in scored:
        evidence = mstore.get_evidence("fact", fact["id"])
        if not _trusted(fact, evidence):
            continue
        hits.append(FactHit(fact=fact, score=round(score, 4), evidence=evidence))
        if len(hits) >= max(1, int(limit)):
            break
    return hits


# What one person says about someone else is only hearsay.  Nicknames and
# in-jokes are by nature said by others; claims about a person ("other",
# "relationship") are used when two messages back them, or the person said it.
UNCORROBORATED_KINDS = ("other", "relationship")


def _trusted(fact: dict, evidence: list) -> bool:
    if fact["kind"] not in UNCORROBORATED_KINDS or int(fact["support_count"]) >= 2:
        return True
    subject = canon(fact["subject"])
    return any(canon(e.get("sender_name", "")) == subject for e in evidence)
