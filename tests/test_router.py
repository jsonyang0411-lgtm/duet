"""Tests for message routing behavior around @mentions."""

import time
import unittest
from unittest.mock import MagicMock, patch

from src.config import BotConfig
from src.persona import PERSONA_PROFILE_YANG_YUJING
from src.router import MessageRouter
from src.trigger import TriggerDetector


class FakeStore:
    def __init__(self):
        self.inserted = []
        self.context_messages = []
        self.facts = []
        self.media_records = []
        self.quoted_media = {}
        self.history_records = []
        self.history_queries = []
        self.group_memory = None

    def insert_message(self, msg):
        self.inserted.append(msg)
        return True

    def get_group_memory(self, chat_id):
        return self.group_memory

    def get_new_message_count(self, chat_id, last_message_id):
        return 0

    def get_messages_since(self, chat_id=None, since_ts=0, until_ts=None, limit=500):
        if self.context_messages:
            return list(self.context_messages[-limit:])
        until_ts = until_ts if until_ts is not None else int(time.time())
        rows = [
            m for m in self.inserted
            if (chat_id is None or m.get("chat_id") == chat_id)
            and since_ts <= int(m.get("timestamp", 0)) <= until_ts
        ]
        return rows[-limit:]

    def upsert_group_fact(self, chat_id, subject, predicate, object_value,
                          source_message_id="", confidence=0.8,
                          source="heuristic"):
        fact = {
            "chat_id": chat_id,
            "subject": subject,
            "predicate": predicate,
            "object": object_value,
            "source_message_id": source_message_id,
            "confidence": confidence,
            "source": source,
        }
        self.facts = [
            f for f in self.facts
            if not (
                f["chat_id"] == chat_id
                and f["subject"] == subject
                and f["predicate"] == predicate
                and f["object"] == object_value
            )
        ]
        self.facts.append(fact)

    def get_group_facts(self, chat_id, limit=30):
        return [f for f in reversed(self.facts) if f["chat_id"] == chat_id][:limit]

    def get_recent_media(self, chat_id, before_ts=None, lookback_sec=900, limit=10):
        return list(self.media_records[:limit])

    def get_media_by_server_id(self, chat_id, server_id):
        return self.quoted_media.get((chat_id, str(server_id)))

    def search_chat_history(self, chat_id, query_terms, limit=8):
        self.history_queries.append((chat_id, list(query_terms), limit))
        return list(self.history_records[:limit])


class FakeNicks:
    def resolve_name(self, value):
        return value

    def resolve_wxids(self, text):
        return text


class FakeSummarizer:
    def __init__(self, reply="嗯，在。"):
        self.reply = reply
        self.image_reply = "是一张测试图"
        self.error = None
        self.chat_calls = []
        self.image_calls = []
        self.last_chat_reply_was_intentional_silence = False

    def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.reply

    def describe_image(self, **kwargs):
        self.image_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.image_reply

    def consolidate_memory(self, existing_memory, new_messages):
        return existing_memory


class FakeWebSearch:
    reply_max_chars = 120

    def __init__(self):
        self.queries = []

    def extract_query(self, text, *, bot_name=""):
        if "查一下" in text:
            return "鸣潮最新活动"
        return ""

    def search(self, query):
        self.queries.append(query)
        return []

    def format_for_prompt(self, query, results):
        return (
            f"联网搜索请求：{query}\n"
            "联网搜索结果：1. 鸣潮活动公告｜来源：example.com｜摘要：测试摘要"
        )


def make_router(*, sticky=True, followup=True, web_search_client=None, config=None):
    summarizer = FakeSummarizer()
    config = config or BotConfig(
        bot_display_name="林小满",
        proactive_enabled=False,
        sticky_mention_enabled=sticky,
        followup_listen_enabled=followup,
        todo_enabled=False,
    )
    router = MessageRouter(
        store=FakeStore(),
        detector=TriggerDetector([], "林小满"),
        summarizer=summarizer,
        admin_handler=MagicMock(handle=MagicMock(return_value=None)),
        nickname_service=FakeNicks(),
        config=config,
        web_search_client=web_search_client,
    )
    return router, summarizer


def make_msg(message_id, content, *, is_at=True):
    return {
        "message_id": message_id,
        "chat_id": "chat1@chatroom",
        "group_name": "测试群",
        "sender_id": "wxid_user1",
        "sender_name": "小明",
        "content": content,
        "msg_type": 1,
        "timestamp": int(time.time()),
        "is_at_mentioned": is_at,
        "is_group": True,
    }


def make_other_msg(message_id, content, *, is_at=False):
    msg = make_msg(message_id, content, is_at=is_at)
    msg["sender_id"] = "wxid_user2"
    msg["sender_name"] = "土豆"
    return msg


def make_private_msg(message_id, content):
    return {
        "message_id": message_id,
        "chat_id": "wxid_friend",
        "group_name": "私聊好友",
        "chat_name": "私聊好友",
        "sender_id": "wxid_friend",
        "sender_name": "私聊好友",
        "content": content,
        "msg_type": 1,
        "timestamp": int(time.time()),
        "is_at_mentioned": False,
        "is_group": False,
    }


def make_image_msg(message_id, *, sender_id="wxid_user1"):
    msg = make_msg(message_id, "[表情]", is_at=False)
    msg["sender_id"] = sender_id
    msg["sender_name"] = "小明" if sender_id == "wxid_user1" else "土豆"
    msg["msg_type"] = 47
    msg["media_path"] = r"C:\\tmp\\wechat-sticker.gif"
    return msg


class YangYujingFullFeedTests(unittest.TestCase):
    def _router(self, always_reply_group=""):
        config = BotConfig(
            bot_display_name="杨宇靖",
            chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
            yang_yujing_group_name="群主应该是pyc",
            yang_yujing_always_reply_group_name=always_reply_group,
            proactive_enabled=False,
            sticky_mention_enabled=True,
            followup_listen_enabled=True,
            todo_enabled=False,
        )
        return make_router(config=config)

    def test_reads_non_mentioned_message_in_target_group(self):
        router, summarizer = self._router()
        msg = make_msg("yang1", "太极好啊", is_at=False)
        msg["group_name"] = "群主应该是pyc"

        self.assertEqual(router.handle(msg), "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 1)
        self.assertEqual(summarizer.chat_calls[0]["message"], "太极好啊")
        self.assertTrue(summarizer.chat_calls[0]["ambient"])
        self.assertFalse(summarizer.chat_calls[0]["force_reply"])

    def test_forced_group_replies_even_when_the_model_chooses_silence(self):
        router, summarizer = self._router("shit bro 和emperor 还有maybe哥")
        summarizer.reply = ""
        summarizer.last_chat_reply_was_intentional_silence = True
        msg = make_msg("yang-forced", "嗯", is_at=False)
        msg["group_name"] = "shit bro 和emperor 还有maybe哥"

        self.assertEqual(router.handle(msg), "啊？")
        self.assertEqual(len(summarizer.chat_calls), 1)
        self.assertTrue(summarizer.chat_calls[0]["ambient"])
        self.assertTrue(summarizer.chat_calls[0]["force_reply"])

    def test_reads_media_as_a_safe_placeholder(self):
        router, summarizer = self._router()
        msg = make_msg("yang-image", "deadbeef" * 40, is_at=False)
        msg["group_name"] = "群主应该是pyc"
        msg["msg_type"] = 3

        router.handle(msg)

        self.assertEqual(len(summarizer.chat_calls), 1)
        self.assertEqual(summarizer.chat_calls[0]["message"], "[图片]")

    def test_ambient_cooldown_limits_consecutive_turns(self):
        router, summarizer = self._router()
        first = make_msg("yang-cooldown-1", "来吃麦当劳了", is_at=False)
        first["group_name"] = "群主应该是pyc"
        second = make_msg("yang-cooldown-2", "经理还给打五折", is_at=False)
        second["group_name"] = "群主应该是pyc"

        self.assertEqual(router.handle(first), "嗯，在。")
        self.assertIsNone(router.handle(second))
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_ambient_ai_discussion_stays_silent_without_direct_address(self):
        router, summarizer = self._router()
        msg = make_msg("yang-meta", "这ai真人机", is_at=False)
        msg["group_name"] = "群主应该是pyc"

        self.assertIsNone(router.handle(msg))
        self.assertEqual(summarizer.chat_calls, [])

    def test_direct_address_bypasses_ambient_cooldown(self):
        router, summarizer = self._router()
        first = make_msg("yang-direct-1", "来吃麦当劳了", is_at=False)
        first["group_name"] = "群主应该是pyc"
        direct = make_msg("yang-direct-2", "@杨宇靖 你觉得呢", is_at=True)
        direct["group_name"] = "群主应该是pyc"

        router.handle(first)
        self.assertEqual(router.handle(direct), "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 2)

    def test_ambient_chat_uses_facts_without_full_diary_memory(self):
        router, summarizer = self._router()
        router._store.group_memory = {
            "memory_text": "很长的群日记，不应进入普通插话",
            "last_message_id": None,
            "last_consolidated": None,
            "message_count": 0,
        }
        router._store.facts.append({
            "chat_id": "chat1@chatroom",
            "subject": "宇肆",
            "predicate": "alias",
            "object": "次元",
        })
        msg = make_msg("yang-facts", "次元刚才说啥", is_at=False)
        msg["group_name"] = "群主应该是pyc"

        router.handle(msg)

        group_memory = summarizer.chat_calls[0]["group_memory"]
        self.assertIn("固定事实/称呼记忆", group_memory)
        self.assertIn("次元", group_memory)
        self.assertNotIn("很长的群日记", group_memory)

    def test_does_not_read_other_groups_in_yang_mode(self):
        router, summarizer = self._router()
        msg = make_msg("yang-other", "太极好啊", is_at=False)
        msg["group_name"] = "别的群"

        self.assertIsNone(router.handle(msg))
        self.assertEqual(summarizer.chat_calls, [])

    def test_ignores_stale_full_feed_messages_on_startup(self):
        router, summarizer = self._router()
        msg = make_msg("yang-old", "太极好啊", is_at=False)
        msg["group_name"] = "群主应该是pyc"
        msg["timestamp"] -= 301

        self.assertIsNone(router.handle(msg))
        self.assertEqual(summarizer.chat_calls, [])

    def test_custom_profile_keeps_non_mentioned_message_silent(self):
        router, summarizer = make_router(config=BotConfig(bot_display_name="林小满", chat_persona_profile="custom",
                                                          proactive_enabled=False, todo_enabled=False))
        msg = make_msg("custom-normal", "太极好啊", is_at=False)
        msg["group_name"] = "群主应该是pyc"

        self.assertIsNone(router.handle(msg))
        self.assertEqual(summarizer.chat_calls, [])


class RouterAtMentionTests(unittest.TestCase):
    def test_empty_at_mention_replies_and_keeps_sticky_followup(self):
        router, summarizer = make_router(sticky=True)

        reply = router.handle(make_msg("m1", "wxid_other:\n@林小满"))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "在吗")

        followup = router.handle(make_msg("m2", "你怎么看", is_at=False))

        self.assertEqual(followup, "嗯，在。")
        self.assertIn("连续聊天", summarizer.chat_calls[1]["message"])
        self.assertIn("最新消息：你怎么看", summarizer.chat_calls[1]["message"])

    def test_empty_at_mention_replies_even_without_sticky_mode(self):
        router, summarizer = make_router(sticky=False)

        reply = router.handle(make_msg("m1", "@林小满"))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "在吗")

    def test_explicit_empty_at_mention_gets_presence_fallback(self):
        router, summarizer = make_router(sticky=True)
        summarizer.reply = ""

        reply = router.handle(make_msg("m1", "@林小满"))

        self.assertEqual(reply, "嗯，在的")

    def test_explicit_at_mention_gets_retry_fallback_when_ai_is_blank(self):
        router, summarizer = make_router(sticky=True)
        summarizer.reply = ""

        reply = router.handle(make_msg("m1", "@林小满 你怎么看"))

        self.assertEqual(reply, "我刚才没接住，再说一次好不好")

    def test_sticky_followup_gets_retry_fallback_when_ai_is_blank(self):
        router, summarizer = make_router(sticky=True)

        router.handle(make_msg("m1", "@林小满"))
        summarizer.reply = ""
        reply = router.handle(make_msg("m2", "你怎么看", is_at=False))

        self.assertIsNone(reply)

    def test_intentional_silence_sends_nothing_without_fallback(self):
        router, summarizer = make_router(sticky=True)

        router.handle(make_msg("m1", "@林小满"))
        summarizer.reply = ""
        summarizer.last_chat_reply_was_intentional_silence = True
        reply = router.handle(make_msg("m2", "没有", is_at=False))

        self.assertIsNone(reply)

    def test_explicit_at_mention_gets_timeout_fallback(self):
        router, summarizer = make_router(sticky=True)
        summarizer.error = TimeoutError("simulated timeout")

        reply = router.handle(make_msg("m1", "@林小满 你怎么看"))

        self.assertEqual(reply, "我刚才有点卡，再说一次好不好")

    def test_private_chat_replies_without_at_mention(self):
        router, summarizer = make_router(sticky=True)

        reply = router.handle(make_private_msg("p1", "你好呀"))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "你好呀")
        self.assertFalse(summarizer.chat_calls[0]["is_group"])

    def test_web_search_context_is_added_for_search_request(self):
        search = FakeWebSearch()
        router, summarizer = make_router(sticky=True, web_search_client=search)

        reply = router.handle(make_msg("m_search", "@林小满 查一下鸣潮最新活动"))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(search.queries, ["鸣潮最新活动"])
        call = summarizer.chat_calls[0]
        self.assertIn("联网搜索请求：鸣潮最新活动", call["message"])
        self.assertIn("测试摘要", call["message"])
        self.assertEqual(call["max_chars_override"], 120)

    def test_image_question_uses_vision_without_normal_chat(self):
        router, summarizer = make_router(sticky=True)
        router._store.media_records = [
            {"media_path": r"C:\tmp\wechat-image.jpg", "timestamp": int(time.time())}
        ]

        with patch(
            "src.router.find_recent_image_path",
            return_value=r"C:\tmp\wechat-image.jpg",
        ):
            reply = router.handle(make_msg("m_img", "@林小满 这张图里是什么"))

        self.assertEqual(reply, "是一张测试图")
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(len(summarizer.image_calls), 1)
        self.assertEqual(
            summarizer.image_calls[0]["image_path"],
            r"C:\tmp\wechat-image.jpg",
        )

    def test_image_question_without_recent_media_asks_to_resend(self):
        router, summarizer = make_router(sticky=True)

        with patch("src.router.find_recent_image_path", return_value=""):
            reply = router.handle(make_msg("m_img_missing", "@林小满 表情包什么意思"))

        self.assertIn("没找到刚才那张图", reply)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(summarizer.image_calls, [])

    def test_named_question_about_quoted_image_uses_exact_media(self):
        router, summarizer = make_router(sticky=True)
        router._store.quoted_media[("chat1@chatroom", "img-server-42")] = {
            "server_id": "img-server-42",
            "media_path": r"C:\tmp\quoted-image.jpg",
            "timestamp": int(time.time()),
        }
        msg = make_msg("m_quoted_image", "林小满，这张图是什么意思", is_at=False)
        msg["quoted_server_id"] = "img-server-42"
        msg["quoted_content"] = "[图片]"

        with patch(
            "src.router.find_recent_image_path",
            return_value=r"C:\tmp\quoted-image.jpg",
        ):
            reply = router.handle(msg)

        self.assertEqual(reply, "是一张测试图")
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(len(summarizer.image_calls), 1)
        self.assertEqual(
            summarizer.image_calls[0]["image_path"],
            r"C:\tmp\quoted-image.jpg",
        )

    def test_quoted_image_question_requires_bot_name(self):
        router, summarizer = make_router(sticky=True)
        msg = make_msg("m_quoted_image_plain", "这张图是什么意思", is_at=False)
        msg["quoted_server_id"] = "img-server-42"

        reply = router.handle(msg)

        self.assertIsNone(reply)
        self.assertEqual(summarizer.image_calls, [])

    def test_image_followup_uses_vision_for_same_sender_after_bot_reply(self):
        router, summarizer = make_router(sticky=True)

        router.handle(make_msg("m_first", "@林小满 你在吗"))
        with patch(
            "src.router.find_recent_image_path",
            return_value=r"C:\tmp\wechat-sticker.gif",
        ):
            reply = router.handle(make_image_msg("m_sticker"))

        self.assertEqual(reply, "是一张测试图")
        self.assertEqual(len(summarizer.chat_calls), 1)
        self.assertEqual(len(summarizer.image_calls), 1)
        self.assertEqual(
            summarizer.image_calls[0]["previous_bot_reply"], "嗯，在。",
        )

    def test_image_followup_uses_screen_capture_when_media_cache_is_missing(self):
        router, summarizer = make_router(sticky=True)
        router.handle(make_msg("m_first", "@林小满 你在吗"))
        sticker = make_image_msg("m_sticker_capture")
        sticker["_capture_media_screenshot"] = MagicMock(
            return_value=r"C:\tmp\wechat-chat-capture.jpg",
        )

        with patch("src.router.find_recent_image_path", return_value=""):
            reply = router.handle(sticker)

        self.assertEqual(reply, "是一张测试图")
        sticker["_capture_media_screenshot"].assert_called_once()
        self.assertEqual(
            summarizer.image_calls[0]["image_path"],
            r"C:\tmp\wechat-chat-capture.jpg",
        )
        self.assertTrue(summarizer.image_calls[0]["is_chat_screenshot"])

    def test_image_followup_ignores_a_different_sender(self):
        router, summarizer = make_router(sticky=True)

        router.handle(make_msg("m_first", "@林小满 你在吗"))
        with patch("src.router.find_recent_image_path") as find_image:
            reply = router.handle(make_image_msg("m_other_sticker", sender_id="wxid_user2"))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.image_calls, [])
        find_image.assert_not_called()

    def test_image_followup_is_one_shot_and_does_not_chain_stickers(self):
        router, summarizer = make_router(sticky=True)

        router.handle(make_msg("m_first", "@林小满 你在吗"))
        with patch(
            "src.router.find_recent_image_path",
            return_value=r"C:\tmp\wechat-sticker.gif",
        ):
            first = router.handle(make_image_msg("m_sticker_1"))
            second = router.handle(make_image_msg("m_sticker_2"))

        self.assertEqual(first, "是一张测试图")
        self.assertIsNone(second)
        self.assertEqual(len(summarizer.image_calls), 1)

    def test_tarot_command_replies_without_ai_call(self):
        router, summarizer = make_router(sticky=True)

        with patch("src.fun.draw_tarot", return_value="@小明 塔罗三张：🔮\n正缘怎么来：星星（正位）｜希望｜还有希望。"):
            reply = router.handle(make_msg("m_tarot", "@林小满 测测正缘"))

        self.assertIn("塔罗三张", reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_visible_reply_strips_emoji_and_kaomoji(self):
        router, _ = make_router(sticky=True)

        reply = router._finish_visible_reply(
            make_msg("clean-emoticons", "@林小满 在吗"),
            "行😂 ^_^ (￣▽￣) :D，明天（下午）见",
        )

        self.assertEqual(reply, "行，明天（下午）见")

    def test_tarot_followup_replies_without_ai_call(self):
        router, summarizer = make_router(sticky=True, followup=True)

        with patch(
            "src.fun.draw_tarot",
            side_effect=[
                "@小明 塔罗三张：🔮\n正缘怎么来：星星（正位）｜希望｜还有希望。",
                "@土豆 塔罗三张：🔮\n正缘怎么来：太阳（正位）｜明朗｜事情会变清楚。",
            ],
        ):
            first = router.handle(make_msg("m_tarot1", "@林小满 塔罗牌"))
            second = router.handle(make_other_msg("m_tarot2", "我也要测"))

        self.assertIn("星星", first)
        self.assertIn("太阳", second)
        self.assertEqual(summarizer.chat_calls, [])

    def test_private_tarot_command_replies_without_at(self):
        router, summarizer = make_router(sticky=True)

        with patch("src.fun.draw_tarot", return_value="@私聊好友 塔罗三张：🔮\n正缘怎么来：月亮（逆位）｜迷雾｜先确认事实。"):
            reply = router.handle(make_private_msg("p_tarot", "塔罗牌 感情"))

        self.assertIn("月亮", reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_private_chat_gets_retry_fallback_when_ai_is_blank(self):
        router, summarizer = make_router(sticky=True)
        summarizer.reply = ""

        reply = router.handle(make_private_msg("p1", "你在吗"))

        self.assertEqual(reply, "我刚才没接住，再说一次好不好")

    def test_quote_reply_to_bot_triggers_without_at_mention(self):
        router, summarizer = make_router(sticky=True)
        msg = make_msg("q1", "都可以", is_at=True)
        msg["is_reply_to_bot"] = True
        msg["quoted_sender_name"] = "林小满"
        msg["quoted_content"] = "啊？你喜欢哪种风格的呀"

        reply = router.handle(msg)

        self.assertEqual(reply, "嗯，在。")
        ai_message = summarizer.chat_calls[0]["message"]
        self.assertIn("引用了你上一句话", ai_message)
        self.assertIn("你喜欢哪种风格", ai_message)

    def test_quote_reply_to_another_member_does_not_trigger_bot(self):
        router, summarizer = make_router(sticky=True)
        msg = make_msg("q_other", "厉害", is_at=False)
        msg["quoted_content"] = "大概是吧"
        msg["quoted_sender_name"] = "刘俊威"
        msg["is_reply_to_bot"] = False

        reply = router.handle(msg)

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_quote_reply_to_another_member_with_bot_name_still_triggers(self):
        router, summarizer = make_router(sticky=True)
        msg = make_msg("q_other_named", "林小满，你怎么看？", is_at=True)
        msg["quoted_content"] = "大概是吧"
        msg["quoted_sender_name"] = "刘俊威"
        msg["is_reply_to_bot"] = False

        reply = router.handle(msg)

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_quote_reply_preserves_prior_direct_request_constraints(self):
        router, summarizer = make_router(sticky=True)
        now = int(time.time())
        router._store.context_messages = [
            make_msg(
                "m1",
                "wxid_itd1k388r8lc19:\n@林小满 推荐几首英文歌",
                is_at=True,
            ),
            make_msg("m2", "我爸妈又嫌我在家了", is_at=False),
            make_msg("q1", "都可以", is_at=True),
        ]
        for i, item in enumerate(router._store.context_messages):
            item["sender_id"] = "wxid_itd1k388r8lc19"
            item["timestamp"] = now + i
        msg = router._store.context_messages[-1]
        msg["is_reply_to_bot"] = True
        msg["quoted_sender_name"] = "林小满"
        msg["quoted_content"] = "啊？我平时听的不多……你喜欢哪种风格的呀"

        reply = router.handle(msg)

        self.assertEqual(reply, "嗯，在。")
        ai_message = summarizer.chat_calls[0]["message"]
        self.assertIn("推荐几首英文歌", ai_message)
        self.assertIn("硬约束", ai_message)
        self.assertIn("都可以", ai_message)

    def test_short_followup_to_bot_reply_triggers_without_quote_or_at(self):
        router, summarizer = make_router(sticky=True, followup=True)
        summarizer.reply = "《Counting Stars》《Love Story》还有《Shape of You》"

        first = router.handle(make_msg("m1", "@林小满 推荐几首英文歌"))

        self.assertIn("Shape of You", first)
        summarizer.reply = "Shape of You，土豆说的那首。"
        reply = router.handle(make_other_msg("m2", "shapeofyou", is_at=False))

        self.assertEqual(reply, "Shape of You，土豆说的那首。")
        self.assertEqual(len(summarizer.chat_calls), 2)
        ai_message = summarizer.chat_calls[1]["message"]
        self.assertIn("你刚才在群里回复", ai_message)
        self.assertIn("Shape of You", ai_message)
        self.assertIn("shapeofyou", ai_message)
        self.assertIn("shares-bot-reply-token", ai_message)
        self.assertLessEqual(len(summarizer.chat_calls[1]["context_messages"]), 6)

    def test_short_question_after_bot_reply_triggers_candidate_check(self):
        router, summarizer = make_router(sticky=True, followup=True)
        summarizer.reply = "《Counting Stars》《Love Story》还有《Shape of You》"

        router.handle(make_msg("m1", "@林小满 推荐几首英文歌"))
        summarizer.reply = "嗯……你是在问 Shape of You 吗"
        reply = router.handle(make_other_msg("m2", "吗", is_at=False))

        self.assertEqual(reply, "嗯……你是在问 Shape of You 吗")
        ai_message = summarizer.chat_calls[1]["message"]
        self.assertIn("短消息", ai_message)
        self.assertIn("short-question", ai_message)

    def test_followup_listener_ignores_unrelated_short_message(self):
        router, summarizer = make_router(sticky=True, followup=True)
        summarizer.reply = "《Counting Stars》《Love Story》还有《Shape of You》"

        router.handle(make_msg("m1", "@林小满 推荐几首英文歌"))
        reply = router.handle(make_other_msg("m2", "我爸妈又嫌我在家了", is_at=False))

        self.assertIsNone(reply)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_followup_listener_can_be_disabled(self):
        router, summarizer = make_router(sticky=True, followup=False)
        summarizer.reply = "《Counting Stars》《Love Story》还有《Shape of You》"

        router.handle(make_msg("m1", "@林小满 推荐几首英文歌"))
        reply = router.handle(make_other_msg("m2", "shape什么", is_at=False))

        self.assertIsNone(reply)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_alias_fact_is_remembered_from_pronoun_answer(self):
        router, summarizer = make_router(sticky=True)

        router.handle(make_msg("m1", "林小满，见到宇肆要叫什么"))
        router.handle(make_other_msg("m2", "叫他次元哥哥@林小满", is_at=True))

        self.assertTrue(
            any(
                f["subject"] == "宇肆"
                and f["predicate"] == "alias"
                and f["object"] == "次元哥哥"
                for f in router._store.facts
            )
        )
        self.assertTrue(
            any(
                f["subject"] == "宇肆"
                and f["predicate"] == "alias"
                and f["object"] == "cy"
                for f in router._store.facts
            )
        )

        summarizer.reply = "次元哥哥呀"
        reply = router.handle(make_msg("m3", "你应该叫宇肆什么"))

        self.assertEqual(reply, "次元哥哥呀")
        group_memory = summarizer.chat_calls[-1]["group_memory"]
        self.assertIn("固定事实/称呼记忆", group_memory)
        self.assertIn("宇肆", group_memory)
        self.assertIn("次元哥哥", group_memory)
        self.assertIn("cy", group_memory)

    def test_alias_fact_is_remembered_from_direct_call_rule(self):
        router, _summarizer = make_router(sticky=True)

        router.handle(make_msg("m1", "@林小满 叫宇肆次元哥哥"))

        self.assertTrue(
            any(
                f["subject"] == "宇肆"
                and f["predicate"] == "alias"
                and f["object"] == "次元哥哥"
                for f in router._store.facts
            )
        )


class HistoricalChatRecallTests(unittest.TestCase):
    def test_explicit_old_chat_question_adds_matching_records_to_ai_prompt(self):
        router, summarizer = make_router(sticky=True)
        router._store.history_records = [
            {
                "sender_id": "wxid_archive_user",
                "sender_name": "阿明",
                "content": "次元是 cy，他之前说自己是南通人。",
                "timestamp": 1760000000,
                "source": "wechat_backup",
            },
        ]

        reply = router.handle(
            make_msg("history-1", "@林小满 之前谁说过 cy 是南通人？"),
        )

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(len(router._store.history_queries), 1)
        self.assertEqual(router._store.history_queries[0][0], "chat1@chatroom")
        self.assertIn("cy", router._store.history_queries[0][1])
        ai_message = summarizer.chat_calls[0]["message"]
        self.assertIn("历史聊天检索结果", ai_message)
        self.assertIn("次元是 cy", ai_message)
        self.assertIn("阿明", ai_message)

    def test_ordinary_message_does_not_search_history(self):
        router, _summarizer = make_router(sticky=True)

        router.handle(make_msg("history-2", "@林小满 记得吃饭"))

        self.assertEqual(router._store.history_queries, [])


if __name__ == "__main__":
    unittest.main()
