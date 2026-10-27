"""Characterization tests for the SQLite message store.

Covers src/db/schema.py (``initialize_db``) and src/db/store.py
(``MessageStore``) against a real in-memory SQLite database.

Phase 0 safety net: these tests pin what the store does TODAY -- including the
parts that are known to be wrong or that production never calls -- so that a
later refactor shows up as an intentional diff instead of a silent change.
Behaviour that is deliberately NOT endorsed is marked::

    # PINNED (known issue, expected to change in Phase N): <why>

When such behaviour is fixed, edit the expectation in the same change.

Determinism: the ``time`` reference inside src/db/store.py is replaced by a
fake clock.  ``messages.created_at`` comes from SQLite's own clock (a column
DEFAULT), so tests that depend on it insert rows with explicit values or only
check plausibility.  Nothing sleeps, no threads
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

        # No chat archive, group memory or reply log: the business gateway keeps none.
        # chat_read_marks only says how far a creator's chat was read (no text; PRM_CATCHUP_HOURS).
        self.assertEqual(tables, {"messages", "message_media", "user_last_message", "chat_read_marks"})
        self.assertEqual(indexes, {
            "idx_msg_chat_time", "idx_msg_chat_sender_time",
            "idx_media_chat_time", "idx_media_chat_server",
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
        self.assertEqual(count, 4)

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
            "message_media.message_id": (
                "INSERT INTO message_media (message_id, chat_id, sender_id, "
                "sender_name, msg_type, timestamp) VALUES ('m', 'c', 's', 'n', 3, 1)"
            ),
            "user_last_message.(chat_id, sender_id)": (
                "INSERT INTO user_last_message (chat_id, sender_id, "
                "sender_name, last_timestamp) VALUES ('c', 's', 'n', 1)"
            ),
        }
        for name, statement in statements.items():
            with self.subTest(constraint=name):
                conn.execute(statement)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)

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
