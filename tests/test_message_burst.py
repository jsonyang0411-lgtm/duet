"""Tests for coalescing split WeChat bubbles into one conversational turn."""

import unittest

from src.wechat.burst import MessageBurstBuffer


def _msg(
    content,
    *,
    message_id="m1",
    sender_id="wxid_ee",
    sender_name="Ee",
    chat_id="room1@chatroom",
    is_at=False,
    msg_type=1,
):
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "group_name": "测试群",
        "chat_name": "测试群",
        "sender_id": sender_id,
        "sender_name": sender_name,
        "content": content,
        "msg_type": msg_type,
        "timestamp": 100,
        "is_at_mentioned": is_at,
        "is_group": True,
    }


class MessageBurstBufferTests(unittest.TestCase):
    def test_same_sender_split_bubbles_flush_as_one_turn(self):
        buffer = MessageBurstBuffer(window_sec=2.0, max_messages=5)

        self.assertEqual(
            buffer.push(_msg("土豆姐", message_id="m1"), now=0.0),
            [],
        )
        self.assertEqual(
            buffer.push(_msg("在呀", message_id="m2"), now=0.5),
            [],
        )
        self.assertEqual(
            buffer.push(_msg("我就是", message_id="m3"), now=1.0),
            [],
        )

        ready = buffer.flush_due(now=3.1)

        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["content"], "土豆姐\n在呀\n我就是")
        self.assertEqual(ready[0]["_burst_count"], 3)
        self.assertEqual(
            ready[0]["_burst_message_ids"],
            ["m1", "m2", "m3"],
        )

    def test_different_senders_are_not_merged(self):
        buffer = MessageBurstBuffer(window_sec=1.0)

        buffer.push(_msg("土豆姐", message_id="m1", sender_id="wxid_ee"), now=0.0)
        buffer.push(_msg("在呀", message_id="m2", sender_id="wxid_tudou"), now=0.1)

        ready = buffer.flush_due(now=1.2)

        self.assertEqual([msg["content"] for msg in ready], ["土豆姐", "在呀"])
        self.assertNotIn("_burst_count", ready[0])
        self.assertNotIn("_burst_count", ready[1])

    def test_at_mention_flag_is_preserved_across_burst(self):
        buffer = MessageBurstBuffer(window_sec=1.0)

        buffer.push(_msg("@林小满", message_id="m1", is_at=True), now=0.0)
        buffer.push(_msg("推荐几首英文歌", message_id="m2"), now=0.2)

        ready = buffer.flush_due(now=1.3)

        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0]["is_at_mentioned"])
        self.assertEqual(ready[0]["content"], "@林小满\n推荐几首英文歌")

    def test_disabled_buffer_returns_immediately(self):
        buffer = MessageBurstBuffer(enabled=False)
        message = _msg("在吗")

        self.assertEqual(buffer.push(message, now=0.0), [message])

    def test_full_feed_can_merge_text_with_image_and_sticker(self):
        buffer = MessageBurstBuffer(window_sec=2.0, max_messages=5)

        self.assertEqual(
            buffer.push(
                _msg("[图片]", message_id="m1", msg_type=3),
                allow_nontext=True,
                now=0.0,
            ),
            [],
        )
        buffer.push(_msg("还是普通出的", message_id="m2"), allow_nontext=True, now=0.5)
        buffer.push(
            _msg("[表情]", message_id="m3", msg_type=47),
            allow_nontext=True,
            now=1.0,
        )

        ready = buffer.flush_due(now=3.1)

        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["content"], "[图片]\n还是普通出的\n[表情]")
        self.assertEqual(ready[0]["msg_type"], 1)
        self.assertTrue(ready[0]["_burst_contains_media"])

    def test_media_stays_immediate_without_full_feed_opt_in(self):
        buffer = MessageBurstBuffer(window_sec=2.0)
        image = _msg("[图片]", message_id="m1", msg_type=3)

        self.assertEqual(buffer.push(image, now=0.0), [image])


if __name__ == "__main__":
    unittest.main()
