"""Storage for memory v2: facts, episodes, evidence, progress, bot replies, search.

``MemoryStore`` shares the connection and the lock of the ``MessageStore`` it
wraps, so it never opens a second writer on the SQLite file.  Nothing here is
used while MEMORY_V2 is off.

The search index is kept up to date by this class (``sync_index``), never by
triggers on ``messages``: a trigger that fails would make the bot lose the
message it was storing.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from typing import Iterable

from .text import clip, norm_key, sanitize

logger = logging.getLogger(__name__)

FACT_KINDS = ("alias", "preference", "relationship", "plan", "event", "jargon", "other")
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_RETRACTED = "retracted"

OWNER_FACT = "fact"
OWNER_EPISODE = "episode"
OWNER_RETIRE = "retire"          # evidence for taking a fact out of use

# The search index is built in slices so a big database does not hold the
# shared lock (and with it message ingestion) for long.
BACKFILL_BATCH = 2000

_INDEXES = (
    # name, source table
    ("fts_messages", "messages"),
    ("fts_history", "history_messages"),
)
_READY_KEY = "fts.ready"


class MemoryStore:
    def __init__(self, store) -> None:
        self._store = store
        self.conn: sqlite3.Connection = store.conn
        self._lock = store._lock
        self._index_error = ""
        self._index_enabled = False       # enable_search_index() succeeded in this process
        self._tx_depth = 0

    # ── one transaction around several writes ───────────────────────────

    @contextlib.contextmanager
    def transaction(self):
        """Everything written inside commits together or not at all."""
        with self._lock:
            with self.conn:
                self._tx_depth += 1
                try:
                    yield
                finally:
                    self._tx_depth -= 1

    def _tx(self):
        """The connection's own commit scope, unless a transaction is already open."""
        return contextlib.nullcontext() if self._tx_depth else self.conn

    # ── search index (FTS5 trigram, external content) ──────────────────

    @property
    def index_error(self) -> str:
        return self._index_error

    @property
    def index_enabled(self) -> bool:
        return self._index_enabled

    def enable_search_index(self) -> bool:
        """Create the FTS5 tables once.  False when this SQLite cannot do it.

        Rows are added by ``sync_index`` (the first call indexes the whole
        archive in slices); there are no triggers.
        """
        try:
            with self._lock:
                with self.conn:
                    for index, table in _INDEXES:
                        for suffix in ("ai", "ad", "au"):       # left by an earlier build
                            self.conn.execute(f"DROP TRIGGER IF EXISTS {index}_{suffix}")
                        self.conn.execute(
                            f"CREATE VIRTUAL TABLE IF NOT EXISTS {index} USING fts5("
                            f"content, content='{table}', content_rowid='id', tokenize='trigram')"
                        )
                    self._set_state(_READY_KEY, "0")
            self._index_error = ""
            self._index_enabled = True
            return True
        except sqlite3.Error as exc:
            self._index_error = str(exc)
            self._index_enabled = False
            logger.warning(
                "Memory search index unavailable (%s); falling back to LIKE", exc,
            )
            return False

    def drop_search_index(self) -> None:
        """Remove the index (admin escape hatch)."""
        with self._lock:
            with self.conn:
                for index, _table in _INDEXES:
                    for suffix in ("ai", "ad", "au"):
                        self.conn.execute(f"DROP TRIGGER IF EXISTS {index}_{suffix}")
                    self.conn.execute(f"DROP TABLE IF EXISTS {index}")
                self.conn.execute("DELETE FROM memory_state WHERE key LIKE 'fts%'")
        self._index_enabled = False

    def sync_index(self, batch: int = BACKFILL_BATCH) -> bool:
        """Index one slice of rows the index has not seen; True when it has caught up.

        Rows are indexed in id order, so a cursor per table is all the state
        there is.  A failure (index dropped by another process, corrupt file)
        switches the index off for this process and searches fall back to LIKE.
        """
        if not self._index_enabled:
            return False
        caught_up = True
        try:
            with self._lock:
                with self.conn:
                    for index, table in _INDEXES:
                        cursor = int(self._state(f"{index}.cursor") or 0)
                        rows = self.conn.execute(
                            f"SELECT id FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
                            (cursor, int(batch)),
                        ).fetchall()
                        if not rows:
                            continue
                        last = int(rows[-1]["id"])
                        self.conn.execute(
                            f"INSERT INTO {index}(rowid, content) "
                            f"SELECT id, content FROM {table} WHERE id > ? AND id <= ?",
                            (cursor, last),
                        )
                        self._set_state(f"{index}.cursor", str(last))
                        if len(rows) >= int(batch):
                            caught_up = False
                    if caught_up:
                        self._set_state(_READY_KEY, "1")
        except sqlite3.Error as exc:
            self._index_error = str(exc)
            self._index_enabled = False
            logger.warning("Memory search index failed (%s); falling back to LIKE", exc)
            return False
        return caught_up

    backfill_step = sync_index            # the name the first version used

    def search_index_ready(self) -> bool:
        """True when the index exists and has seen every row at its last sync."""
        if not self._index_enabled:
            return False
        try:
            return self._state(_READY_KEY) == "1"
        except sqlite3.Error:
            return False

    # ── state key/value ────────────────────────────────────────────────

    def _state(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM memory_state WHERE key = ?", (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def _set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO memory_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value),
        )

    # ── message search (the lists the retriever fuses) ────────────────────

    def fts_search(self, chat_id: str, grams: list[str], *,
                   limit: int = 60) -> list[dict] | None:
        """bm25-ranked messages containing any of ``grams`` (live and imported).

        None means "no usable index" (not built yet, behind, or broken), which
        is different from an index that simply has no match.
        """
        grams = [g for g in grams if g and '"' not in g][:40]
        if not grams:
            return []
        if not self._index_enabled or not self.sync_index() or not self.search_index_ready():
            return None
        match = " OR ".join(f'"{g}"' for g in grams)
        results: list[dict] = []
        try:
            with self._lock:
                for index, table, source in (
                    ("fts_messages", "messages", "live"),
                    ("fts_history", "history_messages", "history"),
                ):
                    rows = self.conn.execute(
                        f"""SELECT m.id AS row_id, m.message_id, m.sender_id,
                                   m.sender_name, m.content, m.timestamp,
                                   bm25({index}) AS rank
                            FROM {index} JOIN {table} m ON m.id = {index}.rowid
                            WHERE {index} MATCH ? AND m.chat_id = ? AND m.msg_type = 1
                            ORDER BY rank LIMIT ?""",
                        (match, chat_id, int(limit)),
                    ).fetchall()
                    for row in rows:
                        record = dict(row)
                        record["source"] = source
                        results.append(record)
        except sqlite3.Error as exc:
            logger.warning("FTS search failed (%s); using LIKE only", exc)
            self._index_error = str(exc)
            self._index_enabled = False
            return None
        results.sort(key=lambda r: r["rank"])           # bm25: smaller is better
        return results[:limit]

    def like_search(self, chat_id: str, terms: list[str], *, limit: int = 60,
                    scan: int = 30000) -> list[dict]:
        """Messages containing any of ``terms`` (2-character words, FTS fallback).

        Looks at the newest ``scan`` live rows and imported rows only, so the
        cost does not grow with the age of the group.
        """
        terms = [t for t in dict.fromkeys(terms) if t][:8]
        if not terms:
            return []
        clauses = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in terms)
        params = [
            "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for t in terms
        ]
        results: list[dict] = []
        with self._lock:
            for table, source in (("messages", "live"), ("history_messages", "history")):
                rows = self.conn.execute(
                    f"""SELECT id AS row_id, message_id, sender_id, sender_name,
                               content, timestamp
                        FROM (SELECT * FROM {table}
                              WHERE chat_id = ? AND msg_type = 1
                              ORDER BY timestamp DESC LIMIT ?)
                        WHERE {clauses}
                        ORDER BY timestamp DESC LIMIT ?""",
                    [chat_id, int(scan), *params, int(limit)],
                ).fetchall()
                for row in rows:
                    record = dict(row)
                    record["source"] = source
                    results.append(record)
        return results

    # ── facts ───────────────────────────────────────────────────────────

    def upsert_fact(self, chat_id: str, *, kind: str, subject: str, predicate: str,
                    obj: str, confidence: float, evidence: Iterable[dict],
                    expires_at: float | None = None, source: str = "llm",
                    now: float | None = None) -> tuple[int, bool]:
        """Insert a fact or add evidence to the identical one.  (id, created).

        ``support_count`` is the number of distinct messages that back the fact,
        so reading the same window twice cannot inflate it.
        """
        now = time.time() if now is None else now
        key = norm_key(subject, predicate, obj)
        evidence = list(evidence)
        with self._lock:
            with self._tx():
                row = self.conn.execute(
                    "SELECT id, confidence, status, expires_at FROM memory_facts "
                    "WHERE chat_id = ? AND norm_key = ?", (chat_id, key),
                ).fetchone()
                if row is None:
                    cur = self.conn.execute(
                        """INSERT INTO memory_facts
                           (chat_id, kind, subject, predicate, object, norm_key,
                            confidence, source, created_at, updated_at,
                            last_seen_at, expires_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (chat_id, kind, subject, predicate, obj, key,
                         float(confidence), source, now, now, now, expires_at),
                    )
                    fact_id, created = int(cur.lastrowid), True
                else:
                    fact_id, created = int(row["id"]), False
                    # A retracted fact stays retracted: the operator said no.
                    if row["status"] == STATUS_RETRACTED:
                        return fact_id, False
                    new_expiry = row["expires_at"]
                    if expires_at is not None and (new_expiry is None or expires_at > new_expiry):
                        new_expiry = expires_at
                    self.conn.execute(
                        """UPDATE memory_facts
                           SET confidence = MAX(confidence, ?), status = ?,
                               updated_at = ?, last_seen_at = ?, expires_at = ?
                           WHERE id = ?""",
                        (float(confidence), STATUS_ACTIVE, now, now, new_expiry, fact_id),
                    )
                self._add_evidence(OWNER_FACT, fact_id, evidence)
                self.conn.execute(
                    """UPDATE memory_facts SET support_count = MAX(1, (
                           SELECT COUNT(*) FROM memory_evidence
                           WHERE owner_kind = 'fact' AND owner_id = ?)) WHERE id = ?""",
                    (fact_id, fact_id),
                )
        return fact_id, created

    def get_fact(self, fact_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM memory_facts WHERE id = ?", (int(fact_id),),
            ).fetchone()
        return dict(row) if row else None

    def list_facts(self, chat_id: str, *, status: str | None = STATUS_ACTIVE,
                   limit: int = 300) -> list[dict]:
        sql = "SELECT * FROM memory_facts WHERE chat_id = ?"
        params: list = [chat_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def active_facts(self, chat_id: str, *, now: float | None = None,
                     min_confidence: float = 0.0, limit: int = 300) -> list[dict]:
        """Active, unexpired facts of a chat, newest first."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM memory_facts
                   WHERE chat_id = ? AND status = ? AND confidence >= ?
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (chat_id, STATUS_ACTIVE, float(min_confidence), now, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_fact_status(self, chat_id: str, fact_id: int, status: str, *,
                        only_if: str | None = None) -> bool:
        """Change a fact's status; only facts of ``chat_id`` can be touched.

        ``only_if`` makes the change conditional on the current status, so the
        model's "retire" can never override an operator's "retract".
        """
        sql = "UPDATE memory_facts SET status = ?, updated_at = ? WHERE id = ? AND chat_id = ?"
        params: list = [status, time.time(), int(fact_id), chat_id]
        if only_if is not None:
            sql += " AND status = ?"
            params.append(only_if)
        with self._lock:
            with self._tx():
                cur = self.conn.execute(sql, params)
                if cur.rowcount and status != STATUS_ACTIVE:
                    self._drop_fact_vectors([int(fact_id)])
        return cur.rowcount > 0

    def _drop_fact_vectors(self, fact_ids: list[int]) -> None:
        """Delete the meaning vectors of facts that are no longer used (MEMORY_EMBEDDINGS)."""
        if not fact_ids or not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_vectors'"
        ).fetchone():
            return
        self.conn.executemany(
            "DELETE FROM memory_vectors WHERE owner_kind = 'fact' AND owner_id = ?",
            [(fid,) for fid in fact_ids],
        )

    def fact_exists(self, chat_id: str, fact_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM memory_facts WHERE id = ? AND chat_id = ?",
                (int(fact_id), chat_id),
            ).fetchone()
        return row is not None

    # ── episodes ───────────────────────────────────────────────────────

    def add_episode(self, chat_id: str, *, summary: str, topics: list[str],
                    start_ts: int, end_ts: int, message_count: int,
                    evidence: Iterable[dict], now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            with self._tx():
                cur = self.conn.execute(
                    """INSERT INTO memory_episodes
                       (chat_id, summary, topics, start_ts, end_ts, message_count, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (chat_id, summary, "、".join(topics), int(start_ts), int(end_ts),
                     int(message_count), now),
                )
                episode_id = int(cur.lastrowid)
                self._add_evidence(OWNER_EPISODE, episode_id, evidence)
        return episode_id

    def recent_episodes(self, chat_id: str, limit: int = 3) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memory_episodes WHERE chat_id = ? "
                "ORDER BY end_ts DESC, id DESC LIMIT ?", (chat_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def episodes(self, chat_id: str, limit: int = 200) -> list[dict]:
        return self.recent_episodes(chat_id, limit)

    def delete_episode(self, chat_id: str, episode_id: int) -> bool:
        with self._lock:
            with self._tx():
                cur = self.conn.execute(
                    "DELETE FROM memory_episodes WHERE id = ? AND chat_id = ?",
                    (int(episode_id), chat_id),
                )
                if cur.rowcount:
                    self.conn.execute(
                        "DELETE FROM memory_evidence WHERE owner_kind = 'episode' AND owner_id = ?",
                        (int(episode_id),),
                    )
        return cur.rowcount > 0

    # ── evidence ───────────────────────────────────────────────────────

    def _add_evidence(self, owner_kind: str, owner_id: int,
                      evidence: Iterable[dict]) -> None:
        for item in evidence:
            self.conn.execute(
                """INSERT OR IGNORE INTO memory_evidence
                   (owner_kind, owner_id, message_id, quote, sender_id, sender_name, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (owner_kind, int(owner_id), str(item.get("message_id", "")),
                 clip(item.get("quote", ""), 160), str(item.get("sender_id", "")),
                 sanitize(item.get("sender_name", ""), 40), int(item.get("ts", 0) or 0)),
            )

    def add_evidence(self, owner_kind: str, owner_id: int, evidence: Iterable[dict]) -> None:
        with self._lock:
            with self._tx():
                self._add_evidence(owner_kind, owner_id, list(evidence))

    def get_evidence(self, owner_kind: str, owner_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT message_id, quote, sender_id, sender_name, ts FROM memory_evidence "
                "WHERE owner_kind = ? AND owner_id = ? ORDER BY ts, id",
                (owner_kind, int(owner_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def forget_sender(self, chat_id: str, sender_id: str) -> dict:
        """Drop everything that rests on one person's messages.

        Evidence from the sender is deleted.  A fact left without any evidence
        is retracted.  An episode is deleted as soon as the sender appears
        among its evidence at all, because a summary can mention what a person
        said even when it cites someone else.  The raw messages are not touched.
        """
        counts = {"evidence": 0, "facts": 0, "episodes": 0}
        with self._lock:
            with self._tx():
                owners = self.conn.execute(
                    """SELECT DISTINCT e.owner_kind, e.owner_id FROM memory_evidence e
                       LEFT JOIN memory_facts f
                         ON e.owner_kind IN ('fact', 'retire') AND f.id = e.owner_id
                       LEFT JOIN memory_episodes p
                         ON e.owner_kind = 'episode' AND p.id = e.owner_id
                       WHERE e.sender_id = ? AND (f.chat_id = ? OR p.chat_id = ?)""",
                    (sender_id, chat_id, chat_id),
                ).fetchall()
                for owner in owners:
                    kind, oid = owner["owner_kind"], int(owner["owner_id"])
                    cur = self.conn.execute(
                        "DELETE FROM memory_evidence WHERE owner_kind = ? "
                        "AND owner_id = ? AND sender_id = ?", (kind, oid, sender_id),
                    )
                    counts["evidence"] += cur.rowcount
                    if kind == OWNER_EPISODE:
                        self.conn.execute("DELETE FROM memory_evidence WHERE owner_kind = 'episode' "
                                          "AND owner_id = ?", (oid,))
                        self.conn.execute("DELETE FROM memory_episodes WHERE id = ?", (oid,))
                        counts["episodes"] += 1
                        continue
                    if kind != OWNER_FACT:
                        continue
                    left = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM memory_evidence "
                        "WHERE owner_kind = 'fact' AND owner_id = ?", (oid,),
                    ).fetchone()["n"]
                    if left:
                        self.conn.execute(
                            "UPDATE memory_facts SET support_count = MAX(1, ?) WHERE id = ?",
                            (left, oid),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE memory_facts SET status = ?, updated_at = ? WHERE id = ?",
                            (STATUS_RETRACTED, time.time(), oid),
                        )
                        self._drop_fact_vectors([oid])
                        counts["facts"] += 1
        return counts

    # ── progress of the extraction job ────────────────────────────────────

    def get_progress(self, chat_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM memory_progress WHERE chat_id = ?", (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def init_progress(self, chat_id: str, *, keep_last: int) -> dict:
        """First sight of a chat: start ``keep_last`` text messages before the end.

        A group with months of stored messages must not send them all to the
        model the moment the switch is turned on.
        """
        with self._lock:
            with self._tx():
                row = self.conn.execute(
                    """SELECT id FROM messages WHERE chat_id = ? AND msg_type = 1
                       ORDER BY id DESC LIMIT 1 OFFSET ?""",
                    (chat_id, max(0, int(keep_last) - 1)),
                ).fetchone()
                start = int(row["id"]) - 1 if row else 0
                self.conn.execute(
                    "INSERT OR IGNORE INTO memory_progress(chat_id, last_row_id) "
                    "VALUES (?, ?)", (chat_id, max(0, start)),
                )
        return self.get_progress(chat_id) or {}

    def reset_progress(self, chat_id: str, *, keep_last: int) -> dict:
        """Move the read mark to ``keep_last`` text messages before the end (admin)."""
        with self._lock:
            with self._tx():
                self.conn.execute("DELETE FROM memory_progress WHERE chat_id = ?", (chat_id,))
        return self.init_progress(chat_id, keep_last=keep_last)

    def set_progress(self, chat_id: str, *, last_row_id: int | None = None,
                     ok: bool, now: float | None = None,
                     retry_after: float = 0.0, window_end: int | None = None,
                     deterministic: bool = False) -> None:
        """Record the outcome of a window.

        ``fail_count`` counts every failed attempt (it drives the back-off).
        ``parse_fail_count`` counts only answers that could not be used, and
        only for the same window end: a bigger window starts a new count, so
        messages that arrived meanwhile are never discarded unseen.
        """
        now = time.time() if now is None else now
        with self._lock:
            with self._tx():
                self.conn.execute(
                    "INSERT OR IGNORE INTO memory_progress(chat_id) VALUES (?)", (chat_id,),
                )
                if ok:
                    self.conn.execute(
                        """UPDATE memory_progress SET last_row_id = COALESCE(?, last_row_id),
                           last_run_at = ?, fail_count = 0, retry_after = 0,
                           parse_fail_count = 0, fail_end_row_id = 0
                           WHERE chat_id = ?""", (last_row_id, now, chat_id),
                    )
                    return
                row = self.conn.execute(
                    "SELECT parse_fail_count, fail_end_row_id FROM memory_progress WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
                parse_fails = int(row["parse_fail_count"])
                fail_end = int(row["fail_end_row_id"])
                if deterministic:
                    same = window_end is not None and fail_end == int(window_end)
                    parse_fails = parse_fails + 1 if same else 1
                    fail_end = int(window_end or 0)
                self.conn.execute(
                    """UPDATE memory_progress SET fail_count = fail_count + 1,
                       last_run_at = ?, retry_after = ?, parse_fail_count = ?,
                       fail_end_row_id = ? WHERE chat_id = ?""",
                    (now, retry_after, parse_fails, fail_end, chat_id),
                )

    def pending_messages(self, chat_id: str, after_row_id: int,
                         limit: int = 120) -> list[dict]:
        """Text messages after ``after_row_id``, oldest first, with their row id."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT id AS row_id, message_id, sender_id, sender_name,
                          content, timestamp
                   FROM messages
                   WHERE chat_id = ? AND id > ? AND msg_type = 1
                   ORDER BY id LIMIT ?""",
                (chat_id, int(after_row_id), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_pending(self, chat_id: str, after_row_id: int) -> tuple[int, int]:
        """(number of new text messages, newest timestamp among them)."""
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n, COALESCE(MAX(timestamp), 0) AS last_ts
                   FROM messages WHERE chat_id = ? AND id > ? AND msg_type = 1""",
                (chat_id, int(after_row_id)),
            ).fetchone()
        return int(row["n"]), int(row["last_ts"])

    # ── bot replies ──────────────────────────────────────────────────────

    def add_bot_reply(self, chat_id: str, text: str, timestamp: int, *,
                      trigger_message_id: str = "", mode: str = "") -> None:
        with self._lock:
            with self._tx():
                self.conn.execute(
                    """INSERT INTO bot_replies
                       (chat_id, text, timestamp, trigger_message_id, mode)
                       VALUES (?, ?, ?, ?, ?)""",
                    (chat_id, clip(text, 1000), int(timestamp),
                     str(trigger_message_id), str(mode)),
                )

    def bot_replies_between(self, chat_id: str, since_ts: int, until_ts: int,
                            limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT text, timestamp, trigger_message_id, mode FROM bot_replies
                   WHERE chat_id = ? AND timestamp BETWEEN ? AND ?
                   ORDER BY timestamp, id LIMIT ?""",
                (chat_id, int(since_ts), int(until_ts), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_bot_replies(self, chat_id: str, since_ts: int, limit: int = 6) -> list[str]:
        """The newest ``limit`` replies since ``since_ts``, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT text FROM (
                       SELECT text, timestamp, id FROM bot_replies
                       WHERE chat_id = ? AND timestamp >= ?
                       ORDER BY timestamp DESC, id DESC LIMIT ?)
                   ORDER BY timestamp, id""",
                (chat_id, int(since_ts), int(limit)),
            ).fetchall()
        return [row["text"] for row in rows]

    def has_memory(self, chat_id: str) -> bool:
        """True once the extraction job has written anything for this chat."""
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 WHERE EXISTS (SELECT 1 FROM memory_facts WHERE chat_id = ?)
                          OR EXISTS (SELECT 1 FROM memory_episodes WHERE chat_id = ?)""",
                (chat_id, chat_id),
            ).fetchone()
        return row is not None

    # ── statistics for the admin tool ──────────────────────────────────

    def stats(self, chat_id: str | None = None) -> dict:
        where, params = ("WHERE chat_id = ?", (chat_id,)) if chat_id else ("", ())
        out: dict = {}
        with self._lock:
            for name, table in (("facts", "memory_facts"), ("episodes", "memory_episodes"),
                                ("bot_replies", "bot_replies")):
                out[name] = self.conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} {where}", params,
                ).fetchone()["n"]
            by_status = self.conn.execute(
                f"SELECT status, COUNT(*) AS n FROM memory_facts {where} GROUP BY status",
                params,
            ).fetchall()
        out["facts_by_status"] = {row["status"]: row["n"] for row in by_status}
        out["index_ready"] = self.search_index_ready()
        return out
