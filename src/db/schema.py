"""Database schema and initialization.

Idempotent — safe to call on every startup.
"""

import sqlite3
from pathlib import Path


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

-- Every message received from a monitored chat (groups are only recorded)
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT    UNIQUE NOT NULL,
    chat_id     TEXT    NOT NULL,
    sender_id   TEXT    NOT NULL,
    sender_name TEXT    NOT NULL,
    content     TEXT    NOT NULL DEFAULT '',
    msg_type    INTEGER NOT NULL DEFAULT 1,
    timestamp   INTEGER NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Fast lookup: "all messages in chat X since time T"
CREATE INDEX IF NOT EXISTS idx_msg_chat_time
    ON messages(chat_id, timestamp DESC);

-- Fast lookup: "last message from user S in chat X"
CREATE INDEX IF NOT EXISTS idx_msg_chat_sender_time
    ON messages(chat_id, sender_id, timestamp DESC);

-- Local index of picture and sticker messages (where WeChat keeps the file).
CREATE TABLE IF NOT EXISTS message_media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT    UNIQUE NOT NULL,
    chat_id     TEXT    NOT NULL,
    sender_id   TEXT    NOT NULL,
    sender_name TEXT    NOT NULL,
    content     TEXT    NOT NULL DEFAULT '',
    msg_type    INTEGER NOT NULL,
    server_id   TEXT    NOT NULL DEFAULT '',
    media_base_dir TEXT NOT NULL DEFAULT '',
    media_hint  TEXT    NOT NULL DEFAULT '',
    media_kind  TEXT    NOT NULL DEFAULT '',
    media_path  TEXT    NOT NULL DEFAULT '',
    timestamp   INTEGER NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_media_chat_time
    ON message_media(chat_id, timestamp DESC);

-- Materialized last-message-time per user per chat.
-- Updated via UPSERT on every message insert.
CREATE TABLE IF NOT EXISTS user_last_message (
    chat_id        TEXT    NOT NULL,
    sender_id      TEXT    NOT NULL,
    sender_name    TEXT    NOT NULL,
    last_timestamp INTEGER NOT NULL,
    PRIMARY KEY (chat_id, sender_id)
);
"""


def initialize_db(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database, creating tables if they don't exist.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        A sqlite3.Connection with WAL mode enabled and row_factory set.
    """
    # Ensure the parent directory exists
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _ensure_message_media_columns(conn)
    conn.commit()
    return conn


def _ensure_message_media_columns(conn: sqlite3.Connection) -> None:
    """Migrate the media index without requiring users to rebuild its DB."""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(message_media)")
    }
    missing_columns = {
        "server_id": "TEXT NOT NULL DEFAULT ''",
        "media_base_dir": "TEXT NOT NULL DEFAULT ''",
        "media_hint": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in missing_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE message_media ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_chat_server "
        "ON message_media(chat_id, server_id)"
    )
