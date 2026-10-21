"""Tests for todo command parsing (src/todo/handler.py) on a real TodoStore."""

import tempfile
import unittest
from pathlib import Path

from src.config import BotConfig
from src.todo.handler import TodoHandler, format_todo_reply
from src.todo.store import TodoStore

CHAT = "g1@chatroom"


class TodoHandlerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config = BotConfig(db_path=str(Path(self._tmp.name) / "todo.db"))
        self.store = TodoStore(db_path=self.config.db_path)
        self.handler = TodoHandler(self.store, self.config)

    def tearDown(self):
        self._tmp.cleanup()

    def say(self, text, *, admin=False):
        return self.handler.handle(text, CHAT, "wxid_a", "阿明", admin)

    def reply(self, text, *, admin=False):
        result = self.say(text, admin=admin)
        return None if result is None else format_todo_reply(result, "阿明")

    # ── ordinary chat that merely starts with a todo keyword ────────

    def test_delete_keyword_followed_by_a_question_is_not_a_todo_command(self):
        self.assertIsNone(self.say("取消订阅怎么弄"))

    def test_complete_keyword_followed_by_chat_is_not_a_todo_command(self):
        self.assertIsNone(self.say("完成作业了吗"))

    def test_restore_keyword_followed_by_chat_is_not_a_todo_command(self):
        self.assertIsNone(self.say("恢复出厂设置怎么弄"))
        self.assertIsNone(self.say("恢复出厂设置怎么弄", admin=True))

    # ── real commands keep working ──────────────────────────────────

    def test_add_then_complete_by_number(self):
        self.assertIn("周五聚餐", self.reply("记一下 周五聚餐"))
        reply = self.reply("搞定 1")
        self.assertIn("已将第 1 项标记为已完成", reply)

    def test_complete_and_delete_by_content_still_match_an_existing_todo(self):
        self.say("记一下 买菜")
        self.say("记一下 订酒店")

        # A successful complete/delete answers with the updated active list.
        remaining = self.reply("完成 订酒店")
        self.assertIn("1. 买菜", remaining)
        self.assertNotIn("订酒店", remaining)
        self.assertIn("已将第 1 项移至已删除：买菜", self.reply("删掉 买菜"))

    def test_unknown_number_keeps_the_explicit_not_found_feedback(self):
        self.assertEqual(self.reply("删掉 9"), "未找到匹配的待办：9")
        self.assertEqual(self.reply("搞定 第三项"), "未找到匹配的待办：第三项")

    def test_bare_keyword_still_explains_the_usage(self):
        self.assertIn("请指定要完成的待办编号", self.reply("搞定"))
        self.assertIn("请指定要删除的待办编号", self.reply("删掉"))

    def test_restore_is_admin_only_for_a_todo_reference(self):
        self.say("记一下 买菜")
        self.say("删掉 1")

        self.assertEqual(
            self.reply("恢复待办 1"), "仅群管理员可以恢复已删除的待办事项。",
        )
        self.assertIn("买菜", self.reply("恢复待办 1", admin=True))

    # ── empty lists answer something ────────────────────────────────

    def test_empty_active_list_explains_how_to_add(self):
        reply = self.reply("查看待办")
        self.assertIn("当前没有待办事项", reply)
        self.assertIn("记一下", reply)

    def test_empty_completed_and_deleted_lists_answer_too(self):
        self.assertEqual(self.reply("已完成列表"), "还没有已完成的待办事项。")
        self.assertEqual(self.reply("已删除列表"), "已删除列表是空的。")

    def test_non_empty_list_is_unchanged(self):
        self.say("记一下 买菜")
        reply = self.reply("查看待办")
        self.assertIn("1. 买菜", reply)
        self.assertIn("共 1 项待办", reply)


if __name__ == "__main__":
    unittest.main()
