"""Tests for local fun commands."""

import random
import unittest
from unittest.mock import patch

from src.fun import (
    draw_tarot,
    is_lots_command,
    parse_tarot_followup_question,
    parse_tarot_question,
)


class FunCommandTests(unittest.TestCase):
    def test_lots_command_accepts_bare_bot_name_prefix(self):
        self.assertTrue(is_lots_command("林小满 抽签", "林小满"))
        self.assertTrue(is_lots_command("@林小满 抽签", "林小满"))

    def test_parse_tarot_command_with_optional_question(self):
        self.assertEqual(parse_tarot_question("塔罗牌"), "")
        self.assertEqual(
            parse_tarot_question("林小满 塔罗牌 今天适合出门吗", "林小满"),
            "今天适合出门吗",
        )
        self.assertEqual(
            parse_tarot_question("@林小满 帮我抽一张塔罗 感情", "林小满"),
            "感情",
        )
        self.assertEqual(
            parse_tarot_question("小满我想测一下塔罗牌", "林小满"),
            "",
        )
        self.assertEqual(
            parse_tarot_question("林小满，帮我测一下塔罗牌财运", "林小满"),
            "财运",
        )
        self.assertEqual(
            parse_tarot_question("@林小满 测测正缘", "林小满"),
            "正缘",
        )
        self.assertEqual(
            parse_tarot_question("林小满，我想测一下事业运", "林小满"),
            "事业运",
        )
        self.assertEqual(
            parse_tarot_question("@林小满 测测我的事业运", "林小满"),
            "事业运",
        )
        self.assertEqual(
            parse_tarot_question("@林小满 给我测测", "林小满"),
            "",
        )
        self.assertIsNone(parse_tarot_question("抽签", "林小满"))

    def test_parse_tarot_followup_question(self):
        self.assertEqual(parse_tarot_followup_question("我也要测"), "")
        self.assertEqual(parse_tarot_followup_question("给我测测"), "")
        self.assertEqual(parse_tarot_followup_question("测测财运"), "财运")
        self.assertIsNone(parse_tarot_followup_question("我也要吃饭"))

    def test_draw_tarot_formats_three_card_love_reading(self):
        with (
            patch.object(random, "sample", return_value=[
                ("星星", "希望", "还有希望。", "期待别太空。"),
                ("月亮", "迷雾", "现在信息不全。", "容易想太多。"),
                ("太阳", "明朗", "事情会变清楚。", "别太乐观。"),
            ]),
            patch.object(random, "choice", side_effect=[
                "正位",
                "逆位",
                "正位",
            ])
        ):
            reply = draw_tarot("小明", "")

        self.assertIn("@小明 塔罗三张：", reply)
        self.assertNotIn("🔮", reply)
        self.assertIn("只看三个月内", reply)
        self.assertIn("问题：未来三个月内的正缘", reply)
        self.assertIn("正缘怎么来：星星（正位）｜希望", reply)
        self.assertIn("大概什么时候：月亮（逆位）｜迷雾", reply)
        self.assertIn("你们会怎么来往：太阳（正位）｜明朗", reply)
        self.assertIn("合起来看", reply)


class TarotFalsePositiveTests(unittest.TestCase):
    """A topic word plus an action character somewhere is not a tarot request."""

    def test_ordinary_requests_are_not_tarot_commands(self):
        for text in (
            "帮我算算房租加水电一共多少钱",
            "帮我想个生日礼物 送女朋友的 预算500",
            "看看今天的工作安排",
            "抽空学习一下",
            "占用了多少钱",
            "算了，不考试了",
            "这个月的预算看一下",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_tarot_question(text, "林小满"))

    def test_action_directly_followed_by_a_topic_still_means_tarot(self):
        cases = {
            "测测我的财运": "财运",
            "帮我看看桃花": "桃花",
            "算算今年的运势": "今年的运势",
            "测一测我的感情": "感情",
            "帮我算一下最近的事业运": "最近的事业运",
            "请你看看我的正缘": "正缘",
            "@林小满 抽一抽我的姻缘": "姻缘",
        }
        for text, question in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    parse_tarot_question(text, "林小满"), question,
                )

    def test_explicit_tarot_word_is_unaffected(self):
        self.assertEqual(parse_tarot_question("塔罗 今年的钱"), "今年的钱")
        self.assertEqual(
            parse_tarot_question("帮我看看塔罗 这个月的工作"), "这个月的工作",
        )


if __name__ == "__main__":
    unittest.main()
