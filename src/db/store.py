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
        """Rewrite the stored text of one chat with ``rewrite``: the PRM clean-up of a
        creator's address and numbers (8.2).  A database from an older version can still
        have imported messages (``history_messages``) and full-text indexes of them
        (``fts_*`` with a cursor in ``memory_state``): those are rewritten and swapped too,
        so no old words stay searchable.  Returns how many messages changed."""
        changed = 0
        with self._lock:
            with self.conn:
                present = {r[0] for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('history_messages', 'fts_messages', 'fts_history', 'memory_state')")}
                indexes = present & {"fts_messages", "fts_history"} if "memory_state" in present else set()
                for table, index in (("messages", "fts_messages"), ("history_messages", "fts_history")):
                    if table != "messages" and table not in present:
                        continue
                    # Only rows the index has seen (it was filled in id order up to a cursor)
                    # have entries to swap; later ones were never indexed.
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
