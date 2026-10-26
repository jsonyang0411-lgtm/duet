"""MessageStore — all database read/write operations."""

import sqlite3
import time
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

MEDIA_MSG_TYPES = {3, 47}


class MessageStore:
    """Wraps all database operations for message persistence and querying."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.RLock()

    # ── Write operations ──────────────────────────────────────────

    def rewrite_chat(self, chat_id: str, rewrite) -> int:
        """Rewrite the stored text of one chat (live and imported messages) with ``rewrite``:
        the PRM clean-up of a creator's address and numbers (8.2).  Full-text index rows
        (memory v2) are swapped too, so no old words stay searchable.  Returns how many
        messages changed."""
        changed = 0
        with self._lock:
            with self.conn:
                indexes = {r[0] for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE name IN ('fts_messages', 'fts_history')")}
                for table, index in (("messages", "fts_messages"), ("history_messages", "fts_history")):
                    # Only rows the index has seen (memory v2 indexes in id order up to a cursor)
                    # have entries to swap; later ones are indexed with the new text anyway.
                    upto = 0
                    if index in indexes:
                        row = self.conn.execute("SELECT value FROM memory_state WHERE key = ?",
                                                (f"{index}.cursor",)).fetchone()
                        upto = int(row[0] or 0) if row else 0
                    rows = self.conn.execute(
                        f"SELECT id, content FROM {table} WHERE chat_id = ?", (chat_id,)).fetchall()
                    for row_id, content in rows:
                        new = rewrite(content or "")
                        if new == (content or ""):
                            continue
                        indexed = index in indexes and row_id <= upto
                        if indexed:
                            self.conn.execute(
                                f"INSERT INTO {index}({index}, rowid, content) VALUES('delete', ?, ?)",
                                (row_id, content or ""))
                        self.conn.execute(f"UPDATE {table} SET content = ? WHERE id = ?", (new, row_id))
                        if indexed:
                            self.conn.execute(f"INSERT INTO {index}(rowid, content) VALUES(?, ?)", (row_id, new))
                        changed += 1
        return changed

    def insert_message(self, msg: dict) -> bool:
        """Insert a message and update the user's last-message cursor.

        Args:
            msg: Standardized message dict with keys:
                message_id, chat_id, sender_id, sender_name,
                content, msg_type, timestamp

        Returns:
            True if inserted, False if duplicate (silently skipped).
        """
        try:
            # Coerce all fields to SQLite-safe types (defensive).
            # WCDB may return unexpected types (bytes, float timestamps, etc.)
            # that cause sqlite3.InterfaceError at bind time.
            message_id = str(msg["message_id"])
            chat_id = str(msg["chat_id"])
            sender_id = str(msg["sender_id"])
            sender_name = str(msg["sender_name"])
            content = str(msg.get("content", ""))
            msg_type = int(msg.get("msg_type", 1))
            timestamp = int(msg.get("timestamp", 0))

            with self._lock:
                with self.conn:
                    self.conn.execute(
                        """INSERT INTO messages
                           (message_id, chat_id, sender_id, sender_name,
                            content, msg_type, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (message_id, chat_id, sender_id, sender_name,
                         content, msg_type, timestamp),
                    )
                    # Upsert the last-message cursor (use coerced values)
                    self.conn.execute(
                        """INSERT INTO user_last_message
                           (chat_id, sender_id, sender_name, last_timestamp)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(chat_id, sender_id) DO UPDATE SET
                           sender_name = excluded.sender_name,
                           last_timestamp = excluded.last_timestamp""",
                        (chat_id, sender_id, sender_name, timestamp),
                    )
                    if msg_type in MEDIA_MSG_TYPES:
                        self.conn.execute(
                            """INSERT INTO message_media
                               (message_id, chat_id, sender_id, sender_name,
                                content, msg_type, server_id, media_base_dir,
                                media_hint, media_kind, media_path,
                                timestamp)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(message_id) DO UPDATE SET
                               media_path = CASE
                                   WHEN excluded.media_path != ''
                                   THEN excluded.media_path
                                   ELSE message_media.media_path
                               END,
                               server_id = CASE
                                   WHEN excluded.server_id != ''
                                   THEN excluded.server_id
                                   ELSE message_media.server_id
                               END,
                               media_base_dir = CASE
                                   WHEN excluded.media_base_dir != ''
                                   THEN excluded.media_base_dir
                                   ELSE message_media.media_base_dir
                               END,
                               media_hint = CASE
                                   WHEN excluded.media_hint != ''
                                   THEN excluded.media_hint
                                   ELSE message_media.media_hint
                               END,
                               media_kind = excluded.media_kind,
                               content = excluded.content""",
                            (
                                message_id, chat_id, sender_id, sender_name,
                                content, msg_type, str(msg.get("server_id", "")),
                                str(msg.get("media_base_dir", "")),
                                str(msg.get("media_hint", "")),
                                str(msg.get("media_kind", "")),
                                str(msg.get("media_path", "")),
                                timestamp,
                            ),
                        )
            return True
        except sqlite3.IntegrityError as e:
            # Duplicate message_id — silently skip
            logger.warning("IntegrityError inserting msg_id=%s: %s", msg.get("message_id","?")[:20], e)
            return False
        except Exception:
            logger.exception(
                "Failed to insert message (msg_id=%s, chat=%s, sender=%s)",
                msg.get("message_id", "?"), msg.get("chat_id", "?"),
                msg.get("sender_id", "?"),
            )
            return False

    # ── Query operations ───────────────────────────────────────────

    def get_sender_display_name(self, sender_id: str) -> Optional[str]:
        """Return a previously seen display name for a wxid, or None.

        Used as a fallback when WCDB can't resolve the wxid.  Queries the
        messages table for any past message where sender_name differs from
        sender_id (i.e. was successfully resolved at some point).
        """
        with self._lock:
            row = self.conn.execute(
                """SELECT sender_name FROM messages
                   WHERE sender_id = ? AND sender_name != sender_id
                   ORDER BY id DESC LIMIT 1""",
                (sender_id,),
            ).fetchone()
        return row["sender_name"] if row else None

    def get_messages_since(self, chat_id: str, since_ts: int,
                           until_ts: Optional[int] = None,
                           limit: int = 500) -> list[dict]:
        """Fetch messages from a chat in a time window.

        Args:
            chat_id: The chatroom ID.
            since_ts: Start of window (inclusive), Unix seconds.
            until_ts: End of window (inclusive). Defaults to now.
            limit: Maximum number of messages to return.

        Returns:
            List of message dicts, oldest first.  When more than ``limit``
            messages match, the NEWEST ``limit`` of them are returned (callers
            want "the last N messages" as context).  Messages sharing a
            timestamp keep their insertion order.
        """
        if until_ts is None:
            until_ts = int(time.time())

        with self._lock:
            rows = self.conn.execute(
                """SELECT message_id, chat_id, sender_id, sender_name,
                          content, msg_type, timestamp
                   FROM (
                       SELECT id, message_id, chat_id, sender_id, sender_name,
                              content, msg_type, timestamp
                       FROM messages
                       WHERE chat_id = ? AND timestamp BETWEEN ? AND ?
                       ORDER BY timestamp DESC, id DESC
                       LIMIT ?
                   )
                   ORDER BY timestamp ASC, id ASC""",
                (chat_id, since_ts, until_ts, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── Historical chat archive operations ────────────────────────

    def upsert_history_messages(self, messages: list[dict]) -> int:
        """Import archive messages without letting them enter the live queue.

        ``history_messages`` deliberately has no side effects on
        ``user_last_message``, trigger logs, or group-memory consolidation.
        Re-running an import is safe because archive message IDs are unique.
        """
        rows = []
        for msg in messages:
            message_id = str(msg.get("message_id", "")).strip()
            chat_id = str(msg.get("chat_id", "")).strip()
            content = str(msg.get("content", "")).strip()
            if not message_id or not chat_id or not content:
                continue
            rows.append((
                message_id,
                chat_id,
                str(msg.get("sender_id", "") or "archive-unknown"),
                str(msg.get("sender_name", "") or "群友"),
                content,
                int(msg.get("msg_type", 1) or 1),
                str(msg.get("source", "") or "archive"),
                int(msg.get("timestamp", 0) or 0),
            ))
        if not rows:
            return 0

        with self._lock:
            before = self.conn.total_changes
            with self.conn:
                self.conn.executemany(
                    """INSERT INTO history_messages
                       (message_id, chat_id, sender_id, sender_name, content,
                        msg_type, source, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(message_id) DO NOTHING""",
                    rows,
                )
            return self.conn.total_changes - before

    def search_chat_history(self, chat_id: str, query_terms: list[str],
                            limit: int = 8) -> list[dict]:
        """Return relevant text messages from live and imported group history.

        The caller supplies a small set of content terms only after detecting
        an explicit history-recall question.  Searching is bounded and uses
        parameterized LIKE expressions so archive contents are never treated
        as SQL.
        """
        terms = []
        seen = set()
        for value in query_terms:
            term = str(value or "").strip()[:40]
            key = term.casefold()
            if len(term) < 2 or key in seen:
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= 6:
                break
        if not chat_id or not terms:
            return []

        clauses = " OR ".join("content LIKE ?" for _ in terms)
        like_params = [f"%{term}%" for term in terms]
        with self._lock:
            rows = self.conn.execute(
                f"""WITH candidates AS (
                        SELECT message_id, sender_id, sender_name, content,
                               msg_type, timestamp, 'live' AS source
                        FROM messages
                        WHERE chat_id = ? AND msg_type = 1 AND ({clauses})
                        UNION ALL
                        SELECT message_id, sender_id, sender_name, content,
                               msg_type, timestamp, source
                        FROM history_messages
                        WHERE chat_id = ? AND msg_type = 1 AND ({clauses})
                    )
                    SELECT message_id, sender_id, sender_name, content,
                           msg_type, timestamp, source
                    FROM candidates
                    ORDER BY timestamp DESC
                    LIMIT 120""",
                [chat_id, *like_params, chat_id, *like_params],
            ).fetchall()

        # Prefer records matching more of the question, then newer records.
        # A duplicate can exist if an archive overlaps with live collection.
        ranked = []
        seen_records = set()
        for row in rows:
            record = dict(row)
            dedup_key = (
                record["sender_id"], record["content"], record["timestamp"],
            )
            if dedup_key in seen_records:
                continue
            seen_records.add(dedup_key)
            haystack = record["content"].casefold()
            score = sum(
                10 + len(term) for term in terms if term.casefold() in haystack
            )
            ranked.append((score, int(record["timestamp"]), record))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [record for _, _, record in ranked[:max(1, int(limit))]]

    # ── Group memory operations ────────────────────────────────────

    def get_group_memory(self, chat_id: str) -> dict | None:
        """Retrieve the memory record for a group.

        Returns:
            Dict with keys: chat_id, memory_text, message_count,
            last_message_id, last_consolidated, created_at, updated_at.
            None if no memory exists yet for this group.
        """
        with self._lock:
            row = self.conn.execute(
                """SELECT chat_id, memory_text, message_count,
                          last_message_id, last_consolidated,
                          created_at, updated_at
                   FROM group_memory
                   WHERE chat_id = ?""",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_group_memory(self, chat_id: str, memory_text: str,
                            message_count: int, last_message_id: str) -> None:
        """Insert or update a group's memory record.

        Args:
            chat_id: Group chat ID.
            memory_text: The consolidated first-person memory text.
            message_count: Total messages incorporated into this memory.
            last_message_id: The last message ID that was incorporated.
        """
        now = time.time()
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO group_memory
                       (chat_id, memory_text, message_count, last_message_id,
                        last_consolidated, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                       memory_text = excluded.memory_text,
                       message_count = excluded.message_count,
                       last_message_id = excluded.last_message_id,
                       last_consolidated = excluded.last_consolidated,
                       updated_at = excluded.updated_at""",
                    (chat_id, memory_text, message_count, last_message_id,
                     now, now, now),
                )

    # ── Group fact operations ─────────────────────────────────────

    def upsert_group_fact(self, chat_id: str, subject: str, predicate: str,
                          object_value: str, source_message_id: str = "",
                          confidence: float = 0.8,
                          source: str = "heuristic") -> None:
        """Insert or refresh a durable structured group fact."""
        now = time.time()
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        object_value = str(object_value or "").strip()
        if not chat_id or not subject or not predicate or not object_value:
            return
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO group_facts
                       (chat_id, subject, predicate, object, source,
                        source_message_id, confidence, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(chat_id, subject, predicate, object)
                       DO UPDATE SET
                         source_message_id = excluded.source_message_id,
                         confidence = max(group_facts.confidence, excluded.confidence),
                         updated_at = excluded.updated_at""",
                    (
                        chat_id, subject, predicate, object_value, source,
                        source_message_id, float(confidence), now, now,
                    ),
                )

    def get_group_facts(self, chat_id: str, limit: int = 30) -> list[dict]:
        """Return recent structured facts for a group."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT chat_id, subject, predicate, object, source,
                          source_message_id, confidence, created_at, updated_at
                   FROM group_facts
                   WHERE chat_id = ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (chat_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_media_by_server_id(self, chat_id: str, server_id: str) -> dict | None:
        """Return the exact media message referenced by a WeChat quote."""
        if not chat_id or not server_id:
            return None
        with self._lock:
            row = self.conn.execute(
                """SELECT message_id, chat_id, sender_id, sender_name,
                          content, msg_type, server_id, media_base_dir, media_hint,
                          media_kind, media_path,
                          timestamp
                   FROM message_media
                   WHERE chat_id = ? AND server_id = ?
                   ORDER BY timestamp DESC
                   LIMIT 1""",
                (str(chat_id), str(server_id)),
            ).fetchone()
        return dict(row) if row else None

    def get_recent_media(self, chat_id: str, before_ts: int | None = None,
                         lookback_sec: int = 900,
                         msg_types: tuple[int, ...] = (3, 47),
                         limit: int = 10) -> list[dict]:
        """Return recent media messages, newest first."""
        if before_ts is None:
            before_ts = int(time.time())
        since_ts = int(before_ts) - int(lookback_sec)
        placeholders = ",".join("?" for _ in msg_types)
        params = [chat_id, since_ts, int(before_ts), *msg_types, int(limit)]
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT message_id, chat_id, sender_id, sender_name,
                           content, msg_type, server_id, media_base_dir, media_hint,
                           media_kind, media_path,
                           timestamp
                    FROM message_media
                    WHERE chat_id = ?
                      AND timestamp BETWEEN ? AND ?
                      AND msg_type IN ({placeholders})
                    ORDER BY timestamp DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_new_message_count(self, chat_id: str,
                              since_message_id: str | None) -> int:
        """Count new messages in a chat since a given message ID.

        Args:
            chat_id: Group chat ID.
            since_message_id: The last incorporated message ID.
                              If None, count all messages.

        Returns:
            Number of messages after the given ID.
        """
        if since_message_id is None:
            with self._lock:
                row = self.conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
        else:
            # Get the id of the last incorporated message, then count newer ones
            with self._lock:
                row = self.conn.execute(
                    """SELECT COUNT(*) as cnt FROM messages
                       WHERE chat_id = ? AND id > (
                           SELECT COALESCE(
                               (SELECT id FROM messages WHERE message_id = ?), 0
                           )
                       )""",
                    (chat_id, since_message_id),
                ).fetchone()
        return row["cnt"] if row else 0

    def get_messages_since_id(self, chat_id: str,
                              since_message_id: str | None,
                              limit: int = 200) -> list[dict]:
        """Fetch messages since a given message ID.

        Args:
            chat_id: Group chat ID.
            since_message_id: Last incorporated message ID. None = from beginning.
            limit: Max messages to return.

        Returns:
            List of message dicts in chronological order.
        """
        if since_message_id is None:
            with self._lock:
                rows = self.conn.execute(
                    """SELECT message_id, chat_id, sender_id, sender_name,
                              content, msg_type, timestamp
                       FROM messages
                       WHERE chat_id = ?
                       ORDER BY timestamp ASC
                       LIMIT ?""",
                    (chat_id, limit),
                ).fetchall()
        else:
            with self._lock:
                rows = self.conn.execute(
                    """SELECT message_id, chat_id, sender_id, sender_name,
                              content, msg_type, timestamp
                       FROM messages
                       WHERE chat_id = ? AND id > (
                           SELECT COALESCE(
                               (SELECT id FROM messages WHERE message_id = ?), 0
                           )
                       )
                       ORDER BY timestamp ASC
                       LIMIT ?""",
                    (chat_id, since_message_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]
