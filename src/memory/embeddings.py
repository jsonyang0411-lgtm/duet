"""Optional meaning-based search over remembered facts (``MEMORY_EMBEDDINGS=on``).

Fact retrieval matches words (``search.rank_facts``): "我不能吃虾" does not find
the fact "小明对海鲜过敏".  With embeddings each active fact gets a vector from
an OpenAI-compatible ``/embeddings`` endpoint (OpenAI, SiliconFlow, Zhipu,
DashScope compatible mode, a local server ...), and the latest message is
compared with them; a close match counts like a word match.

* Vectors live in ``memory_vectors`` (created on first use), keyed by fact and
  model, with a hash of the fact text so an edited fact is embedded again.
* The memory worker embeds new facts in the background; a failing endpoint is
  retried later with a growing pause and never blocks a reply.
* On the reply path one short call embeds a message addressed to the bot, and
  only when the chat has vectors.  Any error there means: words only, as
  without this switch.  Indexing and these queries pause separately after errors.
* A fact the endpoint refuses (content filter) is marked and skipped until it changes.
* Privacy: fact texts and messages addressed to the bot go to that endpoint.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from array import array
from typing import Iterable

logger = logging.getLogger(__name__)

BATCH = 32
MAX_TEXT_CHARS = 300
MIN_RETRY_SEC, MAX_RETRY_SEC = 60.0, 1800.0
QUERY_TIMEOUT_SEC = 3.0          # the reply path waits at most this long

VECTOR_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    owner_kind TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    model TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner_kind, owner_id, model)
);
CREATE INDEX IF NOT EXISTS idx_memory_vectors_chat ON memory_vectors(chat_id, model);
"""


def fact_text(fact: dict) -> str:
    return f'{fact["subject"]}{fact["predicate"]}{fact["object"]}'[:MAX_TEXT_CHARS]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def normalized(values: Iterable[float]) -> array:
    vec = array("f", (float(v) for v in values))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = array("f", (v / norm for v in vec))
    return vec


def dot(a: array, b: array) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    """One OpenAI-compatible embeddings endpoint."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 10.0,
                 client=None) -> None:
        self.model = model
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._client = client

    def embed(self, texts: list[str], *, timeout: float | None = None) -> list[array]:
        """Unit vectors for ``texts``, in order; raises on any error."""
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self._base_url or None, api_key=self._api_key or "none",
                                  timeout=self._timeout, max_retries=0)
        client = self._client
        if timeout is not None and hasattr(client, "with_options"):
            client = client.with_options(timeout=timeout)
        response = client.embeddings.create(model=self.model, input=[t[:MAX_TEXT_CHARS] for t in texts])
        data = sorted(response.data, key=lambda item: item.index)
        if len(data) != len(texts):
            raise ValueError(f"{len(data)} vectors for {len(texts)} texts")
        return [normalized(item.embedding) for item in data]


def embedder_from_config(config) -> Embedder | None:
    if getattr(config, "memory_embeddings", "off") != "on":
        return None
    return Embedder(
        base_url=str(getattr(config, "embedding_base_url", "") or ""),
        api_key=str(getattr(config, "embedding_api_key", "") or ""),
        model=str(getattr(config, "embedding_model", "") or ""),
    )


def _rejected(exc: Exception) -> bool:
    """The endpoint refused this input (content filter, too long), not the request as such."""
    return getattr(exc, "status_code", None) in (400, 413, 422)


class _Pause:
    """Retry later after a failure: 60 s, doubling up to 30 min; a success resets it."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.retry_at = 0.0
        self.seconds = MIN_RETRY_SEC

    def waiting(self) -> bool:
        with self._lock:
            return self._clock() < self.retry_at

    def failed(self, what: str, exc: Exception) -> None:
        with self._lock:
            self.retry_at = self._clock() + self.seconds
            logger.warning("Embeddings: %s failed (%s); next try in %.0f s", what, exc, self.seconds)
            self.seconds = min(MAX_RETRY_SEC, self.seconds * 2)

    def ok(self) -> None:
        with self._lock:
            self.retry_at, self.seconds = 0.0, MIN_RETRY_SEC


class VectorIndex:
    """Fact vectors in the memory database, and the calls that make them.

    Indexing (background) and the query on the reply path pause separately,
    so a fact the endpoint keeps refusing never switches meaning search off.
    """

    def __init__(self, mstore, embedder: Embedder, *, clock=time.time) -> None:
        self._mstore = mstore
        self.embedder = embedder
        self._clock = clock
        self._ready = False
        self.index_pause = _Pause(clock)
        self.query_pause = _Pause(clock)
        self._dim = 0                     # length of the vectors this model returns, once seen

    @property
    def model(self) -> str:
        return self.embedder.model

    def _ensure_table(self) -> None:
        if self._ready:
            return
        with self._mstore._lock:
            self._mstore.conn.executescript(VECTOR_TABLE_SQL)
        self._ready = True

    # ── background: the memory worker ──────────────────────────────────────

    def index_pending(self, limit: int = 4 * BATCH) -> int:
        """Embed active facts that have no vector (or a stale one); returns how many."""
        self._ensure_table()
        with self._mstore._lock:
            with self._mstore.conn:
                # Local clean-up, also while the endpoint is pausing: a retracted
                # or forgotten fact takes its vector with it, and so does a fact
                # embedded by another model.
                self._mstore.conn.execute(
                    """DELETE FROM memory_vectors WHERE owner_kind = 'fact' AND (model != ?
                       OR owner_id NOT IN (SELECT id FROM memory_facts WHERE status = 'active'))""",
                    (self.model,),
                )
        if self.index_pause.waiting():
            return 0
        with self._mstore._lock:
            rows = self._mstore.conn.execute(
                """SELECT f.id, f.chat_id, f.subject, f.predicate, f.object,
                          v.text_hash AS stored, v.dim AS stored_dim
                   FROM memory_facts f
                   LEFT JOIN memory_vectors v
                     ON v.owner_kind = 'fact' AND v.owner_id = f.id AND v.model = ?
                   WHERE f.status = 'active'
                   ORDER BY f.updated_at DESC""",
                (self.model,),
            ).fetchall()
        todo = []
        for row in rows:
            text = fact_text(dict(row))
            digest = text_hash(text)
            refused = row["stored"] == digest and row["stored_dim"] == 0
            resized = bool(self._dim) and row["stored_dim"] not in (0, self._dim)
            if not refused and (row["stored"] != digest or resized):
                todo.append((row["id"], row["chat_id"], text, digest))
            if len(todo) >= limit:
                break
        done = 0
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            try:
                vectors = self.embedder.embed([text for _, _, text, _ in batch])
            except Exception as exc:                       # noqa: BLE001 - network, quota, bad model ...
                if not _rejected(exc):
                    self.index_pause.failed("indexing facts", exc)
                    return done
                vectors = self._one_by_one(batch)
                if vectors is None:
                    return done
            self._store(batch, vectors)
            done += sum(1 for vec in vectors if vec is not None)
        if done:
            self.index_pause.ok()
            logger.info("Embeddings: %d fact(s) indexed", done)
        return done

    def _one_by_one(self, batch) -> list | None:
        """After a refused batch: each fact alone; a refused one gets an empty mark."""
        vectors: list = []
        for _, _, text, _ in batch:
            try:
                vectors.append(self.embedder.embed([text])[0])
            except Exception as exc:                       # noqa: BLE001
                if not _rejected(exc):
                    self.index_pause.failed("indexing facts", exc)
                    return None
                logger.info("Embeddings: the endpoint refused a fact (%s); it is skipped until it changes", exc)
                vectors.append(None)
        return vectors

    def _store(self, batch, vectors) -> None:
        now = self._clock()
        for vec in vectors:
            if vec is not None:
                self._dim = len(vec)
        rows = [
            (fid, chat, self.model, digest,
             len(vec) if vec is not None else 0, vec.tobytes() if vec is not None else b"", now, fid)
            for (fid, chat, _, digest), vec in zip(batch, vectors)
        ]
        with self._mstore._lock:
            with self._mstore.conn:
                # Only for a fact that is still active: one retracted meanwhile stays without.
                self._mstore.conn.executemany(
                    """INSERT OR REPLACE INTO memory_vectors
                       (owner_kind, owner_id, chat_id, model, text_hash, dim, vec, updated_at)
                       SELECT 'fact', ?, ?, ?, ?, ?, ?, ?
                       WHERE EXISTS (SELECT 1 FROM memory_facts WHERE id = ? AND status = 'active')""",
                    rows,
                )

    # ── reply path ─────────────────────────────────────────────────────────

    def vectors_for(self, chat_id: str) -> dict[int, array]:
        if not self._ready:
            try:
                self._ensure_table()
            except Exception:                              # noqa: BLE001
                return {}
        with self._mstore._lock:
            rows = self._mstore.conn.execute(
                """SELECT owner_id, vec FROM memory_vectors
                   WHERE chat_id = ? AND model = ? AND owner_kind = 'fact' AND dim > 0""",
                (chat_id, self.model),
            ).fetchall()
        vectors = {}
        for row in rows:
            vec = array("f")
            vec.frombytes(row["vec"])
            vectors[int(row["owner_id"])] = vec
        return vectors

    def query_vector(self, text: str) -> array | None:
        """The message as a vector, or None (words only) when the endpoint is unhappy."""
        text = str(text or "").strip()
        if not text or self.query_pause.waiting():
            return None
        try:
            vector = self.embedder.embed([text], timeout=QUERY_TIMEOUT_SEC)[0]
        except Exception as exc:                           # noqa: BLE001
            self.query_pause.failed("embedding a message", exc)
            return None
        self.query_pause.ok()
        self._dim = len(vector)            # a changed model size makes indexing redo the facts
        return vector
