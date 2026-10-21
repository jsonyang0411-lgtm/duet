import unittest

from src.proactive.image_followup import ImageFollowupTracker


class ImageFollowupTrackerTests(unittest.TestCase):
    def test_late_old_text_cannot_clear_newer_reply_window(self):
        tracker = ImageFollowupTracker(ttl_sec=90)
        tracker.register("chat", "sender", "刚才那句", source_timestamp=200)

        tracker.clear("chat", "sender", message_timestamp=100)

        match = tracker.consume({
            "chat_id": "chat",
            "sender_id": "sender",
            "msg_type": 47,
        })
        self.assertIsNotNone(match)
        self.assertEqual(match.bot_reply, "刚才那句")

    def test_newer_text_closes_reply_window(self):
        tracker = ImageFollowupTracker(ttl_sec=90)
        tracker.register("chat", "sender", "刚才那句", source_timestamp=200)

        tracker.clear("chat", "sender", message_timestamp=201)

        self.assertIsNone(tracker.consume({
            "chat_id": "chat",
            "sender_id": "sender",
            "msg_type": 47,
        }))
