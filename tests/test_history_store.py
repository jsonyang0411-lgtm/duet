"""Tests for archived group-chat retrieval."""

import unittest

from src.db import MessageStore, initialize_db


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = initialize_db(":memory:")
        self.store = MessageStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_archive_import_is_idempotent_and_searches_with_live_messages(self):
        records = [
            {
                "message_id": "backup:1",
                "chat_id": "group@chatroom",
                "sender_id": "archive-user",
                "sender_name": "群友",
                "content": "cy 是南通人",
                "timestamp": 1700000000,
                "source": "wechat_backup",
            },
            {
                "message_id": "backup:2",
                "chat_id": "group@chatroom",
                "sender_id": "archive-user",
                "sender_name": "群友",
                "content": "今天聊别的事",
                "timestamp": 1700000001,
                "source": "wechat_backup",
            },
        ]

        self.assertEqual(self.store.upsert_history_messages(records), 2)
        self.assertEqual(self.store.upsert_history_messages(records), 0)
        self.store.insert_message({
            "message_id": "live:1",
            "chat_id": "group@chatroom",
            "sender_id": "live-user",
            "sender_name": "小明",
            "content": "我也听说 cy 是南通人",
            "msg_type": 1,
            "timestamp": 1700000002,
        })

        results = self.store.search_chat_history(
            "group@chatroom", ["cy", "南通人"], limit=8,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["source"], "live")
        self.assertEqual(results[1]["source"], "wechat_backup")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1,
        )


if __name__ == "__main__":
    unittest.main()
