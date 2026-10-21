"""Characterization tests for the SQLite message store.

Covers src/db/schema.py (``initialize_db``) and src/db/store.py
(``MessageStore``) against a real in-memory SQLite database.

Phase 0 safety net: these tests pin what the store does TODAY -- including the
parts that are known to be wrong or that production never calls -- so that a
later refactor shows up as an intentional diff instead of a silent change.
Behaviour that is deliberately NOT endorsed is marked::

    # PINNED (known issue, expected to change in Phase N): <why>

When such behaviour is fixed, edit the expectation in the same change.

tests/test_history_store.py already covers the archive-import / live+archive
search happy path; this file adds the remaining branches.

Determinism: the ``time`` reference inside src/db/store.py is replaced by a
fake clock.  ``messages.created_at`` and ``trigger_log.processed_at`` come from
SQLite's own clock (a column DEFAULT), so tests that depend on them insert rows
with explicit values or only check plausibility.  Nothing sleeps, no threads
are started and file databases live in a temporary directory.
"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import src.db.store as store_module
from src.db import MessageStore, SCHEMA_SQL, initialize_db

CHAT = "chat1@chatroom"
OTHER_CHAT = "chat2@chatroom"
NOW = 1_700_000_000


class FakeClock:
    """Stand-in for the ``time`` module inside src/db/store.py."""

    def __init__(self, now: float = NOW):
        self.now = now

    def time(self) -> float:
        return self.now


def make_message(message_id, timestamp, *, chat_id=CHAT, sender_id="wxid_a",
                 sender_name="阿明", content="你好", msg_type=1, **extra):
    message = {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "content": content,
        "msg_type": msg_type,
        "timestamp": timestamp,
    }
    message.update(extra)
    return message


class StoreTestCase(unittest.TestCase):
    """A fresh in-memory database and a store reading a fake clock."""

    def setUp(self):
        self.conn = initialize_db(":memory:")
        self.addCleanup(self.conn.close)
        self.store = MessageStore(self.conn)
        self.clock = FakeClock()
        patcher = patch.object(store_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def insert(self, message_id, timestamp, **kwargs):
        self.assertTrue(
            self.store.insert_message(
                make_message(message_id, timestamp, **kwargs),
            ),
        )

    def rows(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params)]

    @staticmethod
    def ids(records):
        return [record["message_id"] for record in records]


# ── schema.py ──────────────────────────────────────────────────────────

class InitializeDbTests(unittest.TestCase):
    def _memory_db(self):
        conn = initialize_db(":memory:")
        self.addCleanup(conn.close)
        return conn

    def test_creates_the_expected_tables_and_indexes(self):
        conn = self._memory_db()

        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }

        self.assertEqual(tables, {
            "messages", "history_messages", "message_media",
            "user_last_message", "trigger_log", "group_memory", "group_facts",
            # Phase 3 (memory v2); always created, empty while MEMORY_V2 is off.
            "memory_facts", "memory_episodes", "memory_evidence",
            "memory_progress", "memory_state", "bot_replies",
        })
        self.assertEqual(indexes, {
            "idx_msg_chat_time", "idx_msg_chat_sender_time",
            "idx_history_msg_chat_time", "idx_media_chat_time",
            "idx_media_chat_server", "idx_trigger_chat_time",
            "idx_group_facts_chat_time",
            "idx_memory_facts_chat_status", "idx_memory_episodes_chat_time",
            "idx_memory_evidence_owner", "idx_memory_evidence_sender",
            "idx_bot_replies_chat_time",
        })

    def test_connection_returns_sqlite_row_objects(self):
        conn = self._memory_db()
        self.assertIs(conn.row_factory, sqlite3.Row)
        row = conn.execute("SELECT 1 AS answer").fetchone()
        self.assertEqual(row["answer"], 1)

    def test_schema_script_is_exported_and_idempotent(self):
        conn = self._memory_db()
        # Running the script a second time must not raise or duplicate anything.
        conn.executescript(SCHEMA_SQL)
        count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        self.assertEqual(count, 13)

    def test_file_database_creates_parent_dirs_and_enables_wal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "nested" / "dir" / "messages.db"

            conn = initialize_db(str(path))
            try:
                self.assertTrue(path.exists())
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0], "wal",
                )
                self.assertEqual(
                    conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000,
                )
                self.assertEqual(
                    conn.execute("PRAGMA foreign_keys").fetchone()[0], 1,
                )
                # synchronous=NORMAL
                self.assertEqual(
                    conn.execute("PRAGMA synchronous").fetchone()[0], 1,
                )
            finally:
                conn.close()

    def test_connection_may_be_used_from_another_thread(self):
        # check_same_thread=False: the polling loop and the web server share
        # one connection.  insert_message swallows errors and returns False, so
        # the return value is what proves the cross-thread write worked.
        conn = self._memory_db()
        outcome = []

        def worker():
            outcome.append(
                MessageStore(conn).insert_message(make_message("t1", 1)),
            )

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [True])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1,
        )

    def test_reinitialising_a_file_database_keeps_existing_rows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "messages.db")

            conn = initialize_db(path)
            MessageStore(conn).insert_message(make_message("m1", 100))
            conn.close()

            conn = initialize_db(path)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_old_media_table_gets_the_missing_columns_added(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "old.db")
            legacy = sqlite3.connect(path)
            legacy.executescript(
                """
                CREATE TABLE message_media (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id  TEXT    UNIQUE NOT NULL,
                    chat_id     TEXT    NOT NULL,
                    sender_id   TEXT    NOT NULL,
                    sender_name TEXT    NOT NULL,
                    content     TEXT    NOT NULL DEFAULT '',
                    msg_type    INTEGER NOT NULL,
                    media_kind  TEXT    NOT NULL DEFAULT '',
                    media_path  TEXT    NOT NULL DEFAULT '',
                    timestamp   INTEGER NOT NULL,
                    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );
                INSERT INTO message_media
                    (message_id, chat_id, sender_id, sender_name, content,
                     msg_type, media_kind, media_path, timestamp)
                VALUES ('old1', 'c', 's', 'n', '[图片]', 3, 'image', '/x.jpg', 5);
                """
            )
            legacy.commit()
            legacy.close()

            conn = initialize_db(path)
            try:
                columns = [
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(message_media)")
                ]
                row = dict(conn.execute("SELECT * FROM message_media").fetchone())
                recent = MessageStore(conn).get_recent_media("c", before_ts=10)
            finally:
                conn.close()

        # New columns are appended after the legacy ones, with '' defaults.
        self.assertEqual(columns[-3:], ["server_id", "media_base_dir", "media_hint"])
        self.assertEqual(row["media_path"], "/x.jpg")
        self.assertEqual(
            (row["server_id"], row["media_base_dir"], row["media_hint"]),
            ("", "", ""),
        )
        self.assertEqual([r["message_id"] for r in recent], ["old1"])

    def test_unique_constraints(self):
        conn = self._memory_db()
        statements = {
            "messages.message_id": (
                "INSERT INTO messages (message_id, chat_id, sender_id, "
                "sender_name, timestamp) VALUES ('m', 'c', 's', 'n', 1)"
            ),
            "history_messages.message_id": (
                "INSERT INTO history_messages (message_id, chat_id, sender_id, "
                "sender_name, timestamp) VALUES ('m', 'c', 's', 'n', 1)"
            ),
            "message_media.message_id": (
                "INSERT INTO message_media (message_id, chat_id, sender_id, "
                "sender_name, msg_type, timestamp) VALUES ('m', 'c', 's', 'n', 3, 1)"
            ),
            "user_last_message.(chat_id, sender_id)": (
                "INSERT INTO user_last_message (chat_id, sender_id, "
                "sender_name, last_timestamp) VALUES ('c', 's', 'n', 1)"
            ),
            "group_facts.(chat_id, subject, predicate, object)": (
                "INSERT INTO group_facts (chat_id, subject, predicate, object) "
                "VALUES ('c', 's', 'p', 'o')"
            ),
        }
        for name, statement in statements.items():
            with self.subTest(constraint=name):
                conn.execute(statement)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)

    def test_column_defaults_for_facts_and_memory(self):
        conn = self._memory_db()
        conn.execute(
            "INSERT INTO group_facts (chat_id, subject, predicate, object) "
            "VALUES ('c', 's', 'p', 'o')"
        )
        conn.execute("INSERT INTO group_memory (chat_id) VALUES ('c')")

        fact = dict(conn.execute("SELECT * FROM group_facts").fetchone())
        memory = dict(conn.execute("SELECT * FROM group_memory").fetchone())

        self.assertEqual((fact["source"], fact["confidence"]), ("heuristic", 0.8))
        self.assertEqual(
            (memory["memory_text"], memory["message_count"],
             memory["last_message_id"], memory["last_consolidated"]),
            ("", 0, None, None),
        )


# ── insert_message ─────────────────────────────────────────────────────

class InsertMessageTests(StoreTestCase):
    def test_new_message_is_stored_and_returns_true(self):
        stored = self.store.insert_message(
            make_message("m1", 1000, content="大家好"),
        )

        self.assertTrue(stored)
        row = self.rows("SELECT * FROM messages")[0]
        self.assertEqual(
            {k: row[k] for k in (
                "message_id", "chat_id", "sender_id", "sender_name",
                "content", "msg_type", "timestamp",
            )},
            {
                "message_id": "m1", "chat_id": CHAT, "sender_id": "wxid_a",
                "sender_name": "阿明", "content": "大家好", "msg_type": 1,
                "timestamp": 1000,
            },
        )
        # created_at is filled by SQLite's own clock.
        self.assertGreater(row["created_at"], 1_600_000_000)

    def test_duplicate_message_id_returns_false_and_changes_nothing(self):
        self.insert("dup", 1000, content="first", sender_name="Ann")

        with self.assertLogs("src.db.store", level="WARNING") as logs:
            stored = self.store.insert_message(
                make_message(
                    "dup", 2000, content="second", sender_name="Ann-renamed",
                ),
            )

        self.assertFalse(stored)
        self.assertIn("IntegrityError", logs.output[0])
        self.assertEqual(
            self.rows("SELECT content, timestamp FROM messages"),
            [{"content": "first", "timestamp": 1000}],
        )
        # The whole transaction is rolled back, the cursor row included.
        self.assertEqual(
            self.rows("SELECT sender_name, last_timestamp FROM user_last_message"),
            [{"sender_name": "Ann", "last_timestamp": 1000}],
        )

    def test_message_id_is_unique_across_chats(self):
        self.insert("shared-id", 1000, chat_id=CHAT)

        with self.assertLogs("src.db.store", level="WARNING"):
            stored = self.store.insert_message(
                make_message("shared-id", 1001, chat_id=OTHER_CHAT),
            )

        # PINNED (known issue, expected to change in Phase N): UNIQUE is on
        # message_id alone, so the same id in a second group silently drops the
        # message.  Production ids are md5 digests, which makes this latent.
        self.assertFalse(stored)
        self.assertEqual(
            self.rows("SELECT chat_id FROM messages"), [{"chat_id": CHAT}],
        )

    def test_fields_are_coerced_to_sqlite_safe_types(self):
        stored = self.store.insert_message({
            "message_id": 123,
            "chat_id": b"chat",
            "sender_id": 5,
            "sender_name": None,
            "content": None,
            "msg_type": "3",
            "timestamp": 1500.9,
        })

        self.assertTrue(stored)
        row = self.rows("SELECT * FROM messages")[0]
        self.assertEqual(row["message_id"], "123")
        self.assertEqual(row["sender_id"], "5")
        self.assertEqual(row["msg_type"], 3)
        self.assertEqual(row["timestamp"], 1500)  # truncated, not rounded
        # PINNED (known issue, expected to change in Phase N): str() is applied
        # blindly, so None becomes the text "None" and bytes are NOT decoded
        # (the comment in insert_message says WCDB may return bytes).
        self.assertEqual(row["sender_name"], "None")
        self.assertEqual(row["content"], "None")
        self.assertEqual(row["chat_id"], "b'chat'")

    def test_missing_optional_fields_use_defaults(self):
        stored = self.store.insert_message({
            "message_id": "min", "chat_id": CHAT,
            "sender_id": "wxid_a", "sender_name": "阿明",
        })

        self.assertTrue(stored)
        row = self.rows("SELECT content, msg_type, timestamp FROM messages")[0]
        self.assertEqual(row, {"content": "", "msg_type": 1, "timestamp": 0})

    def test_message_without_required_key_is_rejected(self):
        with self.assertLogs("src.db.store", level="ERROR"):
            stored = self.store.insert_message({"chat_id": CHAT})

        self.assertFalse(stored)
        self.assertEqual(self.rows("SELECT * FROM messages"), [])

    def test_non_numeric_msg_type_is_rejected(self):
        with self.assertLogs("src.db.store", level="ERROR"):
            stored = self.store.insert_message(
                make_message("bad-type", 1000, msg_type="abc"),
            )

        self.assertFalse(stored)
        self.assertEqual(self.rows("SELECT * FROM messages"), [])

    def test_duplicate_with_non_string_message_id_raises(self):
        self.assertTrue(
            self.store.insert_message(make_message(777, 1000)),
        )

        # PINNED (known issue, expected to change in Phase N): store.py logs the
        # duplicate with ``msg.get("message_id", "?")[:20]``, which raises
        # TypeError for a non-str id from inside the except handler, so the
        # "return False" promise is broken for int ids.  Production ids are
        # md5 hex strings, so this is latent.
        with self.assertRaises(TypeError):
            self.store.insert_message(make_message(777, 1000))

    def test_user_last_message_is_upserted_per_chat_and_sender(self):
        self.insert("m1", 1000, sender_name="阿明")
        self.insert("m2", 1100, sender_name="阿明-改名")
        self.insert("m3", 1200, sender_id="wxid_b", sender_name="小红")
        self.insert("m4", 1300, chat_id=OTHER_CHAT)

        rows = self.rows(
            "SELECT chat_id, sender_id, sender_name, last_timestamp "
            "FROM user_last_message ORDER BY chat_id, sender_id"
        )

        self.assertEqual(rows, [
            {"chat_id": CHAT, "sender_id": "wxid_a",
             "sender_name": "阿明-改名", "last_timestamp": 1100},
            {"chat_id": CHAT, "sender_id": "wxid_b",
             "sender_name": "小红", "last_timestamp": 1200},
            {"chat_id": OTHER_CHAT, "sender_id": "wxid_a",
             "sender_name": "阿明", "last_timestamp": 1300},
        ])

    def test_user_last_message_follows_insertion_order_not_timestamp(self):
        self.insert("late", 2000)
        self.insert("early", 1000)

        # PINNED (known issue, expected to change in Phase N): the upsert
        # overwrites last_timestamp with whatever was inserted last, so an
        # out-of-order delivery moves the "last message" cursor backwards.
        self.assertEqual(
            self.rows("SELECT last_timestamp FROM user_last_message WHERE chat_id = ?", (CHAT,)),
            [{"last_timestamp": 1000}],
        )

    def test_image_and_sticker_messages_are_indexed_in_message_media(self):
        self.insert(
            "img", 1000, content="[图片]", msg_type=3, server_id="S1",
            media_base_dir="/base", media_hint="hint", media_kind="image",
            media_path="/p/a.jpg",
        )
        self.insert("sticker", 1100, content="[表情]", msg_type=47)

        rows = self.rows(
            "SELECT message_id, chat_id, sender_id, sender_name, content, "
            "msg_type, server_id, media_base_dir, media_hint, media_kind, "
            "media_path, timestamp FROM message_media ORDER BY id"
        )

        self.assertEqual(rows, [
            {
                "message_id": "img", "chat_id": CHAT, "sender_id": "wxid_a",
                "sender_name": "阿明", "content": "[图片]", "msg_type": 3,
                "server_id": "S1", "media_base_dir": "/base",
                "media_hint": "hint", "media_kind": "image",
                "media_path": "/p/a.jpg", "timestamp": 1000,
            },
            {
                "message_id": "sticker", "chat_id": CHAT,
                "sender_id": "wxid_a", "sender_name": "阿明",
                "content": "[表情]", "msg_type": 47, "server_id": "",
                "media_base_dir": "", "media_hint": "", "media_kind": "",
                "media_path": "", "timestamp": 1100,
            },
        ])
        # Media messages are ALSO regular rows in `messages`.
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS n FROM messages"), [{"n": 2}],
        )
        self.assertEqual(store_module.MEDIA_MSG_TYPES, {3, 47})

    def test_other_message_types_are_not_indexed_as_media(self):
        for index, msg_type in enumerate((1, 34, 43, 49, 10000)):
            self.insert(
                f"t{index}", 1000 + index, msg_type=msg_type,
                server_id=f"S{index}", media_path="/p/x.jpg",
            )

        self.assertEqual(self.rows("SELECT * FROM message_media"), [])

    def test_redelivered_media_message_does_not_enrich_the_existing_row(self):
        self.insert(
            "img", 1000, content="[图片]", msg_type=3, server_id="S1",
            media_kind="image", media_path="",
        )

        with self.assertLogs("src.db.store", level="WARNING"):
            stored = self.store.insert_message(make_message(
                "img", 1000, msg_type=3, server_id="S1", media_kind="emoji",
                media_path="/p/now-downloaded.jpg", content="[图片2]",
            ))

        # PINNED (known issue, expected to change in Phase N): the ON CONFLICT
        # merge rules in insert_message are unreachable for a re-delivered
        # message, because the first INSERT (into `messages`) already raises
        # IntegrityError and rolls the media upsert back.  A media_path that
        # becomes known later is therefore never recorded.
        self.assertFalse(stored)
        row = self.rows(
            "SELECT media_kind, media_path, content FROM message_media"
        )[0]
        self.assertEqual(
            row, {"media_kind": "image", "media_path": "", "content": "[图片]"},
        )

    def _seed_media_row(self, message_id, **overrides):
        row = {
            "message_id": message_id, "chat_id": CHAT, "sender_id": "wxid_a",
            "sender_name": "阿明", "content": "old content", "msg_type": 3,
            "server_id": "OLD-SRV", "media_base_dir": "/old-base",
            "media_hint": "old-hint", "media_kind": "old-kind",
            "media_path": "/old/path.jpg", "timestamp": 900,
        }
        row.update(overrides)
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO message_media ({columns}) VALUES ({marks})",
            tuple(row.values()),
        )
        self.conn.commit()

    def test_media_upsert_keeps_old_values_when_new_ones_are_empty(self):
        # Reachable only when a media row exists without its `messages` row.
        self._seed_media_row("pre")

        self.insert(
            "pre", 1100, msg_type=3, content="new content",
            media_kind="new-kind",
        )

        row = self.rows("SELECT * FROM message_media WHERE message_id = 'pre'")[0]
        # Path-like fields keep the stored value when the new one is empty ...
        self.assertEqual(row["server_id"], "OLD-SRV")
        self.assertEqual(row["media_base_dir"], "/old-base")
        self.assertEqual(row["media_hint"], "old-hint")
        self.assertEqual(row["media_path"], "/old/path.jpg")
        # ... but content and media_kind are always overwritten, even by ''.
        self.assertEqual(row["content"], "new content")
        self.assertEqual(row["media_kind"], "new-kind")
        # chat/sender/timestamp are never touched by the merge.
        self.assertEqual(row["timestamp"], 900)

    def test_media_upsert_replaces_values_when_new_ones_are_present(self):
        self._seed_media_row("pre", msg_type=47)

        self.insert(
            "pre", 1100, msg_type=47, content="c2", server_id="NEW-SRV",
            media_base_dir="/new-base", media_hint="new-hint",
            media_kind="new-kind", media_path="/new/path.gif",
        )

        row = self.rows("SELECT * FROM message_media WHERE message_id = 'pre'")[0]
        self.assertEqual(
            (row["server_id"], row["media_base_dir"], row["media_hint"],
             row["media_kind"], row["media_path"], row["content"]),
            ("NEW-SRV", "/new-base", "new-hint", "new-kind",
             "/new/path.gif", "c2"),
        )

    def test_media_upsert_overwrites_media_kind_with_an_empty_value(self):
        self._seed_media_row("pre")

        self.insert("pre", 1100, msg_type=3, content="")

        row = self.rows("SELECT media_kind, content FROM message_media")[0]
        self.assertEqual(row, {"media_kind": "", "content": ""})
# ── get_messages_since ─────────────────────────────────────────────────

class MessagesSinceTests(StoreTestCase):
    def test_window_is_inclusive_and_ordered_oldest_first(self):
        for index, ts in enumerate((50, 10, 40, 20, 30)):
            self.insert(f"m{index}", ts, content=f"c{ts}")

        messages = self.store.get_messages_since(CHAT, 20, until_ts=40)

        self.assertEqual([m["timestamp"] for m in messages], [20, 30, 40])

    def test_rows_have_the_documented_shape(self):
        self.insert("m1", 100, content="hi", msg_type=3)

        message = self.store.get_messages_since(CHAT, 0, until_ts=200)[0]

        self.assertEqual(message, {
            "message_id": "m1", "chat_id": CHAT, "sender_id": "wxid_a",
            "sender_name": "阿明", "content": "hi", "msg_type": 3,
            "timestamp": 100,
        })

    def test_limit_keeps_the_newest_rows_of_the_window(self):
        for ts in (10, 20, 30, 40, 50):
            self.insert(f"m{ts}", ts)

        messages = self.store.get_messages_since(CHAT, 0, until_ts=100, limit=2)

        # Fixed in Phase 1: callers want "the last N messages" (the router's
        # chat context, fact extraction and proactive context), so the NEWEST
        # N of the window are kept -- still returned oldest first.
        self.assertEqual([m["timestamp"] for m in messages], [40, 50])

    def test_limit_with_equal_timestamps_keeps_the_latest_inserted(self):
        for name in ("a", "b", "c", "d"):
            self.insert(name, 100, content=name)

        messages = self.store.get_messages_since(CHAT, 0, until_ts=100, limit=2)

        self.assertEqual([m["content"] for m in messages], ["c", "d"])

    def test_zero_limit_returns_nothing_and_negative_limit_returns_all(self):
        for ts in (10, 20, 30):
            self.insert(f"m{ts}", ts)

        self.assertEqual(
            self.store.get_messages_since(CHAT, 0, until_ts=100, limit=0), [],
        )
        self.assertEqual(
            len(self.store.get_messages_since(CHAT, 0, until_ts=100, limit=-1)),
            3,
        )

    def test_until_defaults_to_now_inclusive(self):
        self.insert("past", NOW - 10)
        self.insert("now", NOW)
        self.insert("future", NOW + 1)

        messages = self.store.get_messages_since(CHAT, 0)

        self.assertEqual(self.ids(messages), ["past", "now"])

    def test_other_chats_are_excluded(self):
        self.insert("mine", 100)
        self.insert("theirs", 100, chat_id=OTHER_CHAT)

        messages = self.store.get_messages_since(CHAT, 0, until_ts=200)

        self.assertEqual(self.ids(messages), ["mine"])

    def test_every_message_type_is_returned(self):
        self.insert("text", 100, msg_type=1)
        self.insert("image", 101, msg_type=3)
        self.insert("system", 102, msg_type=10000)

        messages = self.store.get_messages_since(CHAT, 0, until_ts=200)

        self.assertEqual([m["msg_type"] for m in messages], [1, 3, 10000])

    def test_messages_in_the_same_second_come_back_in_insertion_order(self):
        self.insert("first", 100, content="first")
        self.insert("second", 100, content="second")
        self.insert("third", 100, content="third")

        messages = self.store.get_messages_since(CHAT, 100, until_ts=100)

        # Fixed in Phase 1: ``id`` is the tie-breaker, so bubbles sent within
        # one second reach the AI context in the order they were sent.
        self.assertEqual(
            [m["content"] for m in messages], ["first", "second", "third"],
        )


# ── get_sender_display_name ────────────────────────────────────────────

class SenderDisplayNameTests(StoreTestCase):
    def test_unknown_sender_has_no_display_name(self):
        self.assertIsNone(self.store.get_sender_display_name("wxid_nobody"))

    def test_rows_whose_name_equals_the_wxid_are_ignored(self):
        self.insert("m1", 100, sender_id="wxid_a", sender_name="wxid_a")

        self.assertIsNone(self.store.get_sender_display_name("wxid_a"))

    def test_latest_resolved_name_wins_by_insertion_order(self):
        self.insert("m1", 100, sender_name="Alice")
        self.insert("m2", 200, sender_name="wxid_a")
        self.assertEqual(self.store.get_sender_display_name("wxid_a"), "Alice")

        self.insert("m3", 300, sender_name="Alice2")
        self.assertEqual(self.store.get_sender_display_name("wxid_a"), "Alice2")

        # Ordering is by row id, not by message timestamp.
        self.insert("m4", 1, sender_name="OldTimestampNewRow")
        self.assertEqual(
            self.store.get_sender_display_name("wxid_a"), "OldTimestampNewRow",
        )

    def test_lookup_spans_all_chats(self):
        self.insert("m1", 100, chat_id=OTHER_CHAT, sender_name="Alice")

        self.assertEqual(self.store.get_sender_display_name("wxid_a"), "Alice")


# ── upsert_history_messages ────────────────────────────────────────────

class HistoryImportTests(StoreTestCase):
    def test_empty_input_imports_nothing(self):
        self.assertEqual(self.store.upsert_history_messages([]), 0)

    def test_records_without_id_chat_or_text_are_skipped(self):
        imported = self.store.upsert_history_messages([
            {"message_id": "", "chat_id": CHAT, "content": "x"},
            {"message_id": "   ", "chat_id": CHAT, "content": "x"},
            {"message_id": "h1", "chat_id": "", "content": "x"},
            {"message_id": "h2", "chat_id": CHAT, "content": "   "},
            {"message_id": "h3", "chat_id": CHAT},
        ])

        self.assertEqual(imported, 0)
        self.assertEqual(self.rows("SELECT * FROM history_messages"), [])

    def test_defaults_and_stripping(self):
        imported = self.store.upsert_history_messages([
            {"message_id": " h1 ", "chat_id": f" {CHAT} ", "content": "  hi  "},
            {"message_id": "h2", "chat_id": CHAT, "content": "x",
             "msg_type": 0, "timestamp": None, "source": ""},
        ])

        rows = self.rows(
            "SELECT message_id, chat_id, sender_id, sender_name, content, "
            "msg_type, source, timestamp FROM history_messages ORDER BY id"
        )
        self.assertEqual(imported, 2)
        self.assertEqual(rows[0], {
            "message_id": "h1", "chat_id": CHAT, "sender_id": "archive-unknown",
            "sender_name": "群友", "content": "hi", "msg_type": 1,
            "source": "archive", "timestamp": 0,
        })
        self.assertEqual(
            (rows[1]["msg_type"], rows[1]["timestamp"], rows[1]["source"]),
            (1, 0, "archive"),
        )

    def test_reimport_keeps_the_first_copy_and_counts_only_new_rows(self):
        first = {"message_id": "h1", "chat_id": CHAT, "content": "original"}
        again = {"message_id": "h1", "chat_id": CHAT, "content": "edited"}
        new = {"message_id": "h2", "chat_id": CHAT, "content": "fresh"}

        self.assertEqual(self.store.upsert_history_messages([first]), 1)
        self.assertEqual(self.store.upsert_history_messages([again]), 0)
        self.assertEqual(self.store.upsert_history_messages([again, new]), 1)

        self.assertEqual(
            self.rows("SELECT message_id, content FROM history_messages "
                      "ORDER BY id"),
            [{"message_id": "h1", "content": "original"},
             {"message_id": "h2", "content": "fresh"}],
        )

    def test_import_does_not_touch_live_tables(self):
        self.store.upsert_history_messages([
            {"message_id": "h1", "chat_id": CHAT, "content": "x",
             "sender_id": "wxid_a"},
        ])

        for table in ("messages", "user_last_message", "message_media",
                      "trigger_log", "group_memory", "group_facts"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )

    def test_non_numeric_timestamp_aborts_the_whole_batch(self):
        good = {"message_id": "h1", "chat_id": CHAT, "content": "ok"}
        bad = {"message_id": "h2", "chat_id": CHAT, "content": "x",
               "timestamp": "abc"}

        # PINNED (known issue, expected to change in Phase N): there is no
        # per-record error handling; one bad timestamp raises ValueError and
        # none of the batch is imported.
        with self.assertRaises(ValueError):
            self.store.upsert_history_messages([good, bad])

        self.assertEqual(self.rows("SELECT * FROM history_messages"), [])


# ── search_chat_history ────────────────────────────────────────────────

class SearchChatHistoryTests(StoreTestCase):
    def history(self, message_id, timestamp, content, *, chat_id=CHAT,
                source="wechat_backup", sender_id="archive-user"):
        self.store.upsert_history_messages([{
            "message_id": message_id, "chat_id": chat_id,
            "sender_id": sender_id, "sender_name": "群友",
            "content": content, "timestamp": timestamp, "source": source,
        }])

    def search(self, terms, **kwargs):
        return self.store.search_chat_history(CHAT, terms, **kwargs)

    def test_no_usable_terms_or_chat_returns_nothing(self):
        self.insert("m1", 100, content="火锅真好吃")

        self.assertEqual(self.search([]), [])
        self.assertEqual(self.search(["火", "", None, "   "]), [])
        self.assertEqual(
            self.store.search_chat_history("", ["火锅"]), [],
        )

    def test_matches_live_and_archive_text_in_the_same_chat_only(self):
        self.insert("live-1", 100, content="今天吃火锅")
        self.insert("live-img", 400, content="[图片] 火锅", msg_type=3)
        self.insert("live-other", 300, content="别的群火锅", chat_id=OTHER_CHAT)
        self.history("arch-1", 150, "火锅真好吃", source="wechat_backup")
        self.history("arch-other", 160, "别的群火锅", chat_id=OTHER_CHAT)
        self.history("arch-default", 170, "又是火锅", source="")

        results = self.search(["火锅"])

        self.assertEqual(
            [(r["message_id"], r["source"]) for r in results],
            [("arch-default", "archive"), ("arch-1", "wechat_backup"),
             ("live-1", "live")],
        )
        self.assertEqual(
            sorted(results[0]),
            ["content", "message_id", "msg_type", "sender_id",
             "sender_name", "source", "timestamp"],
        )

    def test_results_rank_by_matched_terms_then_recency(self):
        self.insert("both-old", 100, content="alpha beta")
        self.insert("alpha-new", 200, content="alpha only")
        self.insert("beta-newest", 300, content="beta only")
        self.insert("all-oldest", 50, content="alpha beta gamma")

        two_terms = self.search(["alpha", "beta"])
        three_terms = self.search(["alpha", "beta", "gamma"])

        self.assertEqual(
            self.ids(two_terms),
            ["both-old", "all-oldest", "alpha-new", "beta-newest"],
        )
        self.assertEqual(
            self.ids(three_terms),
            ["all-oldest", "both-old", "alpha-new", "beta-newest"],
        )

    def test_longer_matched_terms_score_higher_than_recency(self):
        self.insert("short-new", 300, content="has xy in it")
        self.insert("long-old", 100, content="has longterm1 in it")

        results = self.search(["xy", "longterm1"])

        # score = sum(10 + len(term)): 12 for "xy", 19 for "longterm1".
        self.assertEqual(self.ids(results), ["long-old", "short-new"])

    def test_term_cleaning_strips_dedupes_truncates_and_caps_at_six(self):
        self.insert("long", 100, content="火锅" * 20)
        self.insert("padded", 110, content="padded-term here")
        self.insert("sixth", 120, content="ff6")
        self.insert("seventh", 130, content="seventh")

        # strip, then truncate to 40 characters: 60 chars becomes "火锅"*20.
        self.assertEqual(self.ids(self.search(["火锅" * 30])), ["long"])
        self.assertEqual(self.ids(self.search(["  padded-term  "])), ["padded"])
        # Case-insensitive duplicates do not use up the six slots ...
        six = ["aa1", "AA1", "bb2", "cc3", "dd4", "ee5", "ff6"]
        self.assertEqual(self.ids(self.search(six)), ["sixth"])
        # ... but a genuine seventh term is silently ignored.
        seven = ["aa1", "bb2", "cc3", "dd4", "ee5", "ff6", "seventh"]
        self.assertEqual(self.ids(self.search(seven)), ["sixth"])
        self.assertEqual(
            self.ids(self.search(["seventh", "aa1", "bb2", "cc3", "dd4", "ee5"])),
            ["seventh"],
        )

    def test_ascii_matching_is_case_insensitive(self):
        self.insert("m1", 100, content="Hello World")

        self.assertEqual(self.ids(self.search(["HELLO"])), ["m1"])
        self.assertEqual(self.ids(self.search(["world"])), ["m1"])

    def test_like_wildcards_in_terms_are_not_escaped(self):
        self.insert("m1", 100, content="完全无关")
        self.insert("m2", 200, content="abc")

        # PINNED (known issue, expected to change in Phase N): the terms are
        # bound as parameters (no SQL injection) but %/_ stay LIKE wildcards,
        # so "%%" or "__" match every text message in the chat.
        self.assertEqual(self.ids(self.search(["%%"])), ["m2", "m1"])
        self.assertEqual(self.ids(self.search(["__"])), ["m2", "m1"])
        self.assertEqual(self.ids(self.search(["a_c"])), ["m2"])

    def test_live_and_archive_copies_of_one_message_are_collapsed(self):
        self.insert("live", 100, sender_id="wxid_a", content="重复内容")
        self.history(
            "arch", 100, "重复内容", sender_id="wxid_a", source="wechat_backup",
        )

        results = self.search(["重复"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["content"], "重复内容")

    def test_same_text_from_different_senders_is_kept(self):
        self.insert("a", 100, sender_id="wxid_a", content="重复内容")
        self.insert("b", 100, sender_id="wxid_b", content="重复内容")

        self.assertEqual(len(self.search(["重复"])), 2)

    def test_limit_is_at_least_one(self):
        for index in range(3):
            self.insert(f"m{index}", 100 + index, content="火锅")

        self.assertEqual(len(self.search(["火锅"], limit=0)), 1)
        self.assertEqual(len(self.search(["火锅"], limit=-5)), 1)
        self.assertEqual(len(self.search(["火锅"], limit=2)), 2)

    def test_candidate_pool_is_the_120_newest_matches(self):
        for index in range(130):
            self.insert(f"m{index}", 1000 + index, content=f"match {index}")

        results = self.search(["match"], limit=500)

        # PINNED (known issue, expected to change in Phase N): a hard 120-row
        # candidate cap applies before scoring, so ``limit`` cannot exceed it
        # and a better-scoring but older match is never considered.
        self.assertEqual(len(results), 120)
        self.assertEqual(
            (min(r["timestamp"] for r in results),
             max(r["timestamp"] for r in results)),
            (1010, 1129),
        )


# ── group memory ───────────────────────────────────────────────────────

class GroupMemoryTests(StoreTestCase):
    def test_unknown_chat_has_no_memory(self):
        self.assertIsNone(self.store.get_group_memory(CHAT))

    def test_upsert_creates_then_updates_keeping_created_at(self):
        self.clock.now = 1_700_000_000.5
        self.store.upsert_group_memory(CHAT, "diary 1", 10, "m10")

        self.assertEqual(self.store.get_group_memory(CHAT), {
            "chat_id": CHAT, "memory_text": "diary 1", "message_count": 10,
            "last_message_id": "m10", "last_consolidated": 1_700_000_000.5,
            "created_at": 1_700_000_000.5, "updated_at": 1_700_000_000.5,
        })

        self.clock.now = 1_700_000_100.25
        self.store.upsert_group_memory(CHAT, "diary 2", 25, "m25")

        self.assertEqual(self.store.get_group_memory(CHAT), {
            "chat_id": CHAT, "memory_text": "diary 2", "message_count": 25,
            "last_message_id": "m25", "last_consolidated": 1_700_000_100.25,
            "created_at": 1_700_000_000.5, "updated_at": 1_700_000_100.25,
        })

    def test_memory_is_per_chat(self):
        self.store.upsert_group_memory(CHAT, "mine", 1, "m1")

        self.assertIsNone(self.store.get_group_memory(OTHER_CHAT))
        self.assertEqual(
            self.store.get_group_memory(CHAT)["memory_text"], "mine",
        )


# ── group facts ────────────────────────────────────────────────────────

class GroupFactTests(StoreTestCase):
    def upsert(self, subject, predicate, obj, **kwargs):
        self.store.upsert_group_fact(CHAT, subject, predicate, obj, **kwargs)

    def test_defaults_are_heuristic_source_and_point_eight_confidence(self):
        self.clock.now = 1000.0
        self.upsert("宇肆", "alias", "次元哥哥")

        self.assertEqual(self.store.get_group_facts(CHAT), [{
            "chat_id": CHAT, "subject": "宇肆", "predicate": "alias",
            "object": "次元哥哥", "source": "heuristic",
            "source_message_id": "", "confidence": 0.8,
            "created_at": 1000.0, "updated_at": 1000.0,
        }])

    def test_blank_parts_are_ignored_and_values_are_stripped(self):
        self.upsert("", "alias", "x")
        self.upsert("s", "  ", "x")
        self.upsert("s", "p", None)
        self.store.upsert_group_fact("", "s", "p", "o")
        self.upsert("  s  ", " p ", " o ")

        facts = self.store.get_group_facts(CHAT)

        self.assertEqual(
            [(f["subject"], f["predicate"], f["object"]) for f in facts],
            [("s", "p", "o")],
        )
        self.assertEqual(self.store.get_group_facts(""), [])

    def test_refresh_keeps_max_confidence_and_updates_message_and_time(self):
        self.clock.now = 1000.0
        self.upsert("宇肆", "alias", "次元哥哥", source_message_id="m1",
                    confidence=0.86)
        self.clock.now = 1002.0
        self.upsert("宇肆", "alias", "次元哥哥", source_message_id="m2",
                    confidence=0.5, source="llm")

        fact = self.store.get_group_facts(CHAT)[0]

        self.assertEqual(fact["confidence"], 0.86)       # max(), not last
        self.assertEqual(fact["source_message_id"], "m2")
        self.assertEqual((fact["created_at"], fact["updated_at"]), (1000.0, 1002.0))
        # PINNED (known issue, expected to change in Phase N): ON CONFLICT does
        # not update `source`, so the first writer's label sticks forever.
        self.assertEqual(fact["source"], "heuristic")

    def test_higher_confidence_replaces_the_stored_one(self):
        self.upsert("宇肆", "alias", "cy", confidence=0.78)
        self.upsert("宇肆", "alias", "cy", confidence=0.99)

        self.assertEqual(self.store.get_group_facts(CHAT)[0]["confidence"], 0.99)

    def test_facts_are_ordered_by_recency_with_a_limit(self):
        self.clock.now = 1000.0
        self.upsert("a", "alias", "1")
        self.clock.now = 1001.0
        self.upsert("b", "alias", "2")
        self.clock.now = 1002.0
        self.upsert("c", "alias", "3")
        self.clock.now = 1003.0
        self.upsert("a", "alias", "1")  # refresh moves it to the front

        objects = [f["object"] for f in self.store.get_group_facts(CHAT)]

        self.assertEqual(objects, ["1", "3", "2"])
        self.assertEqual(
            [f["object"] for f in self.store.get_group_facts(CHAT, limit=2)],
            ["1", "3"],
        )
        self.assertEqual(self.store.get_group_facts(CHAT, limit=0), [])

    def test_facts_with_equal_timestamps_come_back_oldest_inserted_first(self):
        self.clock.now = 5.0
        self.upsert("a", "alias", "1")
        self.upsert("b", "alias", "2")
        self.upsert("c", "alias", "3")

        # PINNED (known issue, expected to change in Phase N): no tie-breaker
        # on ``updated_at DESC``; today ties follow the index's rowid order.
        self.assertEqual(
            [f["object"] for f in self.store.get_group_facts(CHAT)],
            ["1", "2", "3"],
        )

    def test_facts_are_per_chat(self):
        self.store.upsert_group_fact(OTHER_CHAT, "a", "alias", "1")

        self.assertEqual(self.store.get_group_facts(CHAT), [])
        self.assertEqual(len(self.store.get_group_facts(OTHER_CHAT)), 1)


# ── media lookups ──────────────────────────────────────────────────────

class MediaLookupTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.insert("i1", 1000, content="[图片]", msg_type=3,
                    server_id="S1", media_path="/p/1.jpg", media_kind="image")
        self.insert("i2", 1100, content="[表情]", msg_type=47, server_id="S2")
        self.insert("i3", 1200, content="[图片]", msg_type=3,
                    server_id="S3", chat_id=OTHER_CHAT)
        self.insert("t4", 1300, content="text", msg_type=1)
        self.insert("i5", 1400, content="[图片]", msg_type=3, server_id="S1")
        self.clock.now = 1500

    def test_media_by_server_id_returns_the_newest_match_in_the_chat(self):
        record = self.store.get_media_by_server_id(CHAT, "S1")

        self.assertEqual(record["message_id"], "i5")
        self.assertEqual(
            sorted(record),
            ["chat_id", "content", "media_base_dir", "media_hint",
             "media_kind", "media_path", "message_id", "msg_type",
             "sender_id", "sender_name", "server_id", "timestamp"],
        )

    def test_media_by_server_id_is_scoped_and_needs_both_arguments(self):
        self.assertIsNone(self.store.get_media_by_server_id(CHAT, "NOPE"))
        self.assertIsNone(self.store.get_media_by_server_id(CHAT, "S3"))
        self.assertIsNone(self.store.get_media_by_server_id("", "S1"))
        self.assertIsNone(self.store.get_media_by_server_id(CHAT, ""))
        self.assertEqual(
            self.store.get_media_by_server_id(OTHER_CHAT, "S3")["message_id"],
            "i3",
        )

    def test_recent_media_defaults_to_a_900_second_window_ending_now(self):
        self.assertEqual(
            self.ids(self.store.get_recent_media(CHAT)), ["i5", "i2", "i1"],
        )

        self.clock.now = 1400 + 900
        self.assertEqual(self.ids(self.store.get_recent_media(CHAT)), ["i5"])

    def test_recent_media_window_is_inclusive_on_both_ends(self):
        records = self.store.get_recent_media(
            CHAT, before_ts=1100, lookback_sec=100,
        )

        self.assertEqual(self.ids(records), ["i2", "i1"])

    def test_recent_media_filters_by_type_and_limit(self):
        self.assertEqual(
            self.ids(self.store.get_recent_media(
                CHAT, before_ts=1500, msg_types=(47,),
            )),
            ["i2"],
        )
        self.assertEqual(
            self.ids(self.store.get_recent_media(
                CHAT, before_ts=1500, limit=1,
            )),
            ["i5"],
        )
        self.assertEqual(
            self.store.get_recent_media(CHAT, before_ts=1500, msg_types=()), [],
        )

    def test_recent_media_never_returns_text_or_other_chats(self):
        records = self.store.get_recent_media(CHAT, before_ts=2000)

        self.assertNotIn("t4", self.ids(records))
        self.assertNotIn("i3", self.ids(records))


# ── cursor-based reads used by memory consolidation ───────────────────

class MessageCursorTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        for index in range(5):
            self.insert(f"n{index}", 100 + index, content=str(index))
        self.insert("other", 300, chat_id=OTHER_CHAT)

    def test_count_without_cursor_counts_the_whole_chat(self):
        self.assertEqual(self.store.get_new_message_count(CHAT, None), 5)
        self.assertEqual(self.store.get_new_message_count(OTHER_CHAT, None), 1)
        self.assertEqual(self.store.get_new_message_count("nochat", None), 0)

    def test_count_with_cursor_counts_only_later_rows(self):
        self.assertEqual(self.store.get_new_message_count(CHAT, "n1"), 3)
        self.assertEqual(self.store.get_new_message_count(CHAT, "n4"), 0)

    def test_unknown_or_empty_cursor_restarts_from_the_beginning(self):
        # PINNED (known issue, expected to change in Phase N): a cursor that
        # cannot be found (pruned message, typo) is treated as id 0, i.e. the
        # whole history counts as new again.
        self.assertEqual(self.store.get_new_message_count(CHAT, "missing"), 5)
        self.assertEqual(self.store.get_new_message_count(CHAT, ""), 5)

    def test_cursor_is_compared_by_row_id_across_chats(self):
        # "other" was inserted last, so its id is above every CHAT row.
        self.assertEqual(self.store.get_new_message_count(CHAT, "other"), 0)

    def test_messages_since_id_without_cursor_starts_at_the_beginning(self):
        messages = self.store.get_messages_since_id(CHAT, None)

        self.assertEqual(self.ids(messages), ["n0", "n1", "n2", "n3", "n4"])
        self.assertEqual(
            sorted(messages[0]),
            ["chat_id", "content", "message_id", "msg_type", "sender_id",
             "sender_name", "timestamp"],
        )

    def test_messages_since_id_with_cursor_returns_only_later_rows(self):
        self.assertEqual(
            self.ids(self.store.get_messages_since_id(CHAT, "n1")),
            ["n2", "n3", "n4"],
        )
        self.assertEqual(self.store.get_messages_since_id(CHAT, "n4"), [])

    def test_messages_since_id_limit_keeps_the_oldest_rows(self):
        self.assertEqual(
            self.ids(self.store.get_messages_since_id(CHAT, "n0", limit=2)),
            ["n1", "n2"],
        )
        self.assertEqual(
            self.ids(self.store.get_messages_since_id(CHAT, None, limit=2)),
            ["n0", "n1"],
        )

    def test_unknown_cursor_returns_everything_again(self):
        self.assertEqual(
            self.ids(self.store.get_messages_since_id(CHAT, "missing")),
            ["n0", "n1", "n2", "n3", "n4"],
        )

    def test_rows_are_filtered_by_id_but_sorted_by_timestamp(self):
        self.insert("late-id-early-ts", 1)

        messages = self.store.get_messages_since_id(CHAT, "n4")

        self.assertEqual(self.ids(messages), ["late-id-early-ts"])
        # Cursor "n0" now yields rows by id (> n0) but orders them by timestamp.
        self.assertEqual(
            self.ids(self.store.get_messages_since_id(CHAT, "n0")),
            ["late-id-early-ts", "n1", "n2", "n3", "n4"],
        )
if __name__ == "__main__":
    unittest.main()
