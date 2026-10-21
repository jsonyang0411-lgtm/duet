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

-- Every message received from any monitored group chat
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

-- Imported chat archives are kept separate from live messages.  They are
-- searchable when someone explicitly asks about old chat, but never replayed
-- through the router or fed into background memory consolidation.
CREATE TABLE IF NOT EXISTS history_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT    UNIQUE NOT NULL,
    chat_id     TEXT    NOT NULL,
    sender_id   TEXT    NOT NULL,
    sender_name TEXT    NOT NULL,
    content     TEXT    NOT NULL DEFAULT '',
    msg_type    INTEGER NOT NULL DEFAULT 1,
    source      TEXT    NOT NULL DEFAULT '',
    timestamp   INTEGER NOT NULL,
    imported_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_history_msg_chat_time
    ON history_messages(chat_id, timestamp DESC);

-- Local media index for on-demand image/sticker understanding.  The bot only
-- sends an image to the model when a user explicitly asks about a recent one.
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

-- Prevent duplicate trigger responses.
CREATE TABLE IF NOT EXISTS trigger_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            TEXT    NOT NULL,
    requester_id       TEXT    NOT NULL,
    trigger_message_id TEXT    NOT NULL,
    processed_at       INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Per-group long-term memory. Consolidates conversation history into
-- a first-person diary-style text that is injected into every prompt,
-- giving the bot a persistent sense of identity and group awareness.
CREATE TABLE IF NOT EXISTS group_memory (
    chat_id           TEXT    PRIMARY KEY,
    memory_text       TEXT    NOT NULL DEFAULT '',
    message_count     INTEGER NOT NULL DEFAULT 0,
    last_message_id   TEXT,
    last_consolidated REAL,
    created_at        REAL    NOT NULL DEFAULT (unixepoch()),
    updated_at        REAL    NOT NULL DEFAULT (unixepoch())
);

-- Small durable facts extracted by local rules, e.g. "小明的群内称呼是阿明".
-- These are injected into chat prompts separately from the free-form diary
-- memory so important aliases are not lost during summarization.
CREATE TABLE IF NOT EXISTS group_facts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           TEXT    NOT NULL,
    subject           TEXT    NOT NULL,
    predicate         TEXT    NOT NULL,
    object            TEXT    NOT NULL,
    source            TEXT    NOT NULL DEFAULT 'heuristic',
    source_message_id TEXT,
    confidence        REAL    NOT NULL DEFAULT 0.8,
    created_at        REAL    NOT NULL DEFAULT (unixepoch()),
    updated_at        REAL    NOT NULL DEFAULT (unixepoch()),
    UNIQUE(chat_id, subject, predicate, object)
);

CREATE INDEX IF NOT EXISTS idx_trigger_chat_time
    ON trigger_log(chat_id, processed_at DESC);

CREATE INDEX IF NOT EXISTS idx_group_facts_chat_time
    ON group_facts(chat_id, updated_at DESC);

-- Memory v2 (MEMORY_V2).  The tables are always created and stay empty while
-- the feature is off.  Every fact and episode is backed by rows in
-- memory_evidence: a message id plus a quote that was checked against that
-- message before anything was written.
CREATE TABLE IF NOT EXISTS memory_facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    subject       TEXT    NOT NULL,
    predicate     TEXT    NOT NULL,
    object        TEXT    NOT NULL,
    norm_key      TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'active',
    confidence    REAL    NOT NULL DEFAULT 0.7,
    support_count INTEGER NOT NULL DEFAULT 1,
    source        TEXT    NOT NULL DEFAULT 'llm',
    created_at    REAL    NOT NULL DEFAULT (unixepoch()),
    updated_at    REAL    NOT NULL DEFAULT (unixepoch()),
    last_seen_at  REAL    NOT NULL DEFAULT (unixepoch()),
    expires_at    REAL,
    UNIQUE(chat_id, norm_key)
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_chat_status
    ON memory_facts(chat_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_episodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       TEXT    NOT NULL,
    summary       TEXT    NOT NULL,
    topics        TEXT    NOT NULL DEFAULT '',
    start_ts      INTEGER NOT NULL DEFAULT 0,
    end_ts        INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_memory_episodes_chat_time
    ON memory_episodes(chat_id, end_ts DESC);

CREATE TABLE IF NOT EXISTS memory_evidence (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_kind TEXT    NOT NULL,
    owner_id   INTEGER NOT NULL,
    message_id TEXT    NOT NULL,
    quote      TEXT    NOT NULL DEFAULT '',
    sender_id  TEXT    NOT NULL DEFAULT '',
    sender_name TEXT   NOT NULL DEFAULT '',
    ts         INTEGER NOT NULL DEFAULT 0,
    UNIQUE(owner_kind, owner_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_evidence_owner
    ON memory_evidence(owner_kind, owner_id);

CREATE INDEX IF NOT EXISTS idx_memory_evidence_sender
    ON memory_evidence(sender_id);

-- How far the extraction job has read each chat (messages.id), and its
-- back-off after a failed model call.
CREATE TABLE IF NOT EXISTS memory_progress (
    chat_id     TEXT PRIMARY KEY,
    last_row_id INTEGER NOT NULL DEFAULT 0,
    last_run_at REAL    NOT NULL DEFAULT 0,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    retry_after REAL    NOT NULL DEFAULT 0,
    parse_fail_count INTEGER NOT NULL DEFAULT 0,
    fail_end_row_id  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- What the bot itself said.  Kept apart from ``messages`` on purpose: the
-- bot's own words are context, never evidence, and the old context, counting
-- and diary code must not see them.
CREATE TABLE IF NOT EXISTS bot_replies (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            TEXT    NOT NULL,
    text               TEXT    NOT NULL,
    timestamp          INTEGER NOT NULL,
    trigger_message_id TEXT    NOT NULL DEFAULT '',
    mode               TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_bot_replies_chat_time
    ON bot_replies(chat_id, timestamp DESC);
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
