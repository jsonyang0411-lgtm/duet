"""Tests for configurable persona chat prompt behavior."""

import unittest

from src.summarize.base import AbstractSummarizer
from src.summarize.models import SummaryResult


class FakeSummarizer(AbstractSummarizer):
    def __init__(self, reply: str = "嗯……刚才没接住"):
        self.reply = reply
        self.captured_system_prompt = ""
        self.captured_messages = []

    def _call_chat_api(self, system_prompt: str, messages: list[dict]) -> str:
        self.captured_system_prompt = system_prompt
        self.captured_messages = messages
        return self.reply

    def _summarize_direct(self, messages: list[dict], requester_name: str) -> SummaryResult:
        return SummaryResult(summary_text="", topics=[], participants=[])

    def _summarize_chunk(self, chunk: list[dict], chunk_num: int, total: int, requester_name: str) -> str:
        return ""

    def _merge_chunk_summaries(self, chunk_summaries: list[str], requester_name: str) -> SummaryResult:
        return SummaryResult(summary_text="", topics=[], participants=[])

    def consolidate_memory(self, existing_memory: str, new_messages: list[dict]) -> str:
        return existing_memory


class PersonaChatTests(unittest.TestCase):
    def test_chat_prompt_injects_persona_and_group_context(self):
        summarizer = FakeSummarizer("好呀……我试试看")
        summarizer.configure_chat_persona(
            persona_name="赛博小柔",
            persona_prompt="说话像温柔群友，不要乱认人。",
            reply_rules="短句，必要时沉默。",
            max_chars=36,
            context_count=3,
        )

        reply = summarizer.chat(
            message="你接着说她就听不懂了",
            requester_name="张三",
            bot_name="林小满",
            group_name="测试群",
            group_memory="大家正在测试 AI 回复是否自然。",
            context_messages=[
                {"sender_name": "张三", "content": "你能帮我测测塔罗吗"},
                {"sender_name": "林小满", "content": "啊……塔罗吗……我只会一点点"},
                {"sender_name": "李四", "content": "怎么感觉人格分裂了一样"},
            ],
        )

        self.assertEqual(reply, "好呀……我试试看")
        self.assertIn("赛博小柔", summarizer.captured_system_prompt)
        self.assertIn("群聊截片", summarizer.captured_system_prompt)
        self.assertIn("李四: 怎么感觉人格分裂了一样", summarizer.captured_system_prompt)
        self.assertIn("不是在对你说", summarizer.captured_system_prompt)

    def test_chat_prompt_replaces_raw_hex_media_blob_in_context(self):
        blob = "28b52ffd" * 20
        summarizer = FakeSummarizer("这个我看不到图")

        summarizer.chat(
            message="这图什么意思",
            requester_name="张三",
            context_messages=[
                {"sender_name": "张三", "content": blob},
            ],
        )

        self.assertIn("张三: [图片]", summarizer.captured_system_prompt)
        self.assertNotIn(blob, summarizer.captured_system_prompt)

    def test_chat_reply_cleaner_allows_blank_silence(self):
        summarizer = FakeSummarizer("空白")
        self.assertEqual(summarizer.chat("笑死", context_messages=[]), "")

    def test_chat_reply_cleaner_drops_punctuation_only_replies(self):
        for raw in ("...", "....", "。。。", "……", "？？"):
            summarizer = FakeSummarizer(raw)
            self.assertEqual(summarizer.chat("测试", context_messages=[]), "")
            self.assertFalse(summarizer.last_chat_reply_was_intentional_silence)

    def test_chat_reply_cleaner_drops_meta_silence_explanations(self):
        samples = [
            "（杨宇靖这句话没有明确指向我，应该是在回应别人的问题。所以我不需要回复。）",
            "（沉默，不确定是在对谁说话）",
            "（不知道该不该接话，先沉默看看）",
            "（没敢接话，悄悄看了他一眼又低下头）",
            "我不需要回复这句话",
            "应该沉默",
        ]
        for raw in samples:
            summarizer = FakeSummarizer(raw)
            self.assertEqual(summarizer.chat("测试", context_messages=[]), "")
            self.assertTrue(summarizer.last_chat_reply_was_intentional_silence)

    def test_chat_reply_cleaner_strips_parenthetical_asides(self):
        summarizer = FakeSummarizer("嗯……我知道了（低下头）")

        self.assertEqual(summarizer.chat("测试", context_messages=[]), "嗯……我知道了")
        self.assertFalse(summarizer.last_chat_reply_was_intentional_silence)

    def test_chat_reply_cleaner_strips_dangling_bracket_marker(self):
        summarizer = FakeSummarizer("hi，你在呀、】【")

        self.assertEqual(summarizer.chat("测试", context_messages=[]), "hi，你在呀")

    def test_chat_reply_cleaner_strips_name_and_truncates(self):
        summarizer = FakeSummarizer("杨宇靖：1234567890abcdef")
        summarizer.configure_chat_persona(max_chars=10)
        self.assertEqual(summarizer.chat("测试", context_messages=[]), "1234567890")

    def test_ambient_chat_prompt_allows_natural_non_mention_replies(self):
        summarizer = FakeSummarizer("太极好啊")
        summarizer.configure_chat_persona(
            persona_name="杨宇靖",
            persona_prompt="短句接话。",
            reply_rules="看到每条消息，没必要就沉默。",
        )

        reply = summarizer.chat(
            message="还有一个小时体育课",
            requester_name="刘俊成",
            group_name="群主应该是pyc",
            context_messages=[
                {"sender_name": "刘俊成", "content": "今天好热"},
                {"sender_name": "刘俊成", "content": "还有一个小时体育课"},
            ],
            ambient=True,
        )

        self.assertEqual(reply, "太极好啊")
        self.assertIn("会看到每条新消息", summarizer.captured_system_prompt)
        self.assertIn("不需要等 @你", summarizer.captured_system_prompt)
        self.assertNotIn("明确 @你、叫你的名字", summarizer.captured_system_prompt)

    def test_forced_ambient_chat_prompt_requires_a_reply(self):
        summarizer = FakeSummarizer("哪个")

        reply = summarizer.chat(
            message="嗯",
            requester_name="刘俊成",
            group_name="shit bro 和emperor 还有maybe哥",
            context_messages=[],
            ambient=True,
            force_reply=True,
        )

        self.assertEqual(reply, "哪个")
        self.assertIn("每一条可读消息都必须回复", summarizer.captured_system_prompt)
        self.assertIn("不能输出空白", summarizer.captured_system_prompt)


if __name__ == "__main__":
    unittest.main()
