"""The business route, one step at a time (src/routing/steps.py)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from router_support import ADMIN, BOT, RouterCase, admin_msg, group_msg, private_msg

from src.routing import steps as S
from src.routing.context import RouteContext, RouteStep, Stop


class StepCase(RouterCase):
    """A real router (in-memory store, real admin handler); ``self.trace`` sees every step."""

    def ctx(self, router, msg):
        return RouteContext(router=router, msg=msg, trace=self.trace)


class OrderTests(StepCase):
    def test_the_chain_has_the_documented_order_and_unique_names(self):
        names = [step.name for step in S.ROUTE_STEPS]
        assert names == ["self_message", "edge", "wechat_review", "persist", "claude_relay", "prm", "admin_command",
                         "silence"]
        assert len(set(names)) == len(names) and all(step.doc for step in S.ROUTE_STEPS)

    def test_the_last_step_always_ends_the_route_in_silence(self):
        router = self.router()
        for msg in (group_msg("g1", f"@{BOT} 你好", is_at_mentioned=True), private_msg("p1", "你好"),
                    admin_msg("a1", "随便聊聊")):
            assert S.ROUTE_STEPS[-1].run(self.ctx(router, msg)) == Stop(None)
        assert self.trace.ignored == ["silent_end"] * 3

    def test_the_step_that_ended_the_route_is_named_in_the_trace(self):
        router = self.router()
        router.handle(group_msg("m1", f"@{BOT} 你好", is_at_mentioned=True))
        assert self.trace.notes["ended_by"] == "silence" and self.trace.reply is None
        router.handle(group_msg("m1", f"@{BOT} 你好"))                        # the same id again
        assert self.trace.notes["ended_by"] == "persist"
        router.handle(group_msg("m2", "我自己", sender_name=BOT))
        assert self.trace.notes["ended_by"] == "self_message"
        router.handle(admin_msg("m3", "帮助"))
        assert self.trace.notes["ended_by"] == "admin_command"

    def test_a_router_can_run_a_different_chain(self):
        router = self.router()
        calls = []
        router._route_steps = (
            RouteStep("first", lambda ctx: calls.append("first")),
            RouteStep("second", lambda ctx: Stop("custom")),
            RouteStep("never", lambda ctx: calls.append("never")),
        )
        assert router.handle(group_msg("m1", "x")) == "custom"
        assert calls == ["first"] and self.trace.notes["ended_by"] == "second"

    def test_a_chain_that_never_stops_returns_nothing(self):
        router = self.router()
        router._route_steps = (RouteStep("noop", lambda ctx: None),)
        assert router.handle(group_msg("m1", "x")) is None
        assert self.trace.ignored == ["silent_end"]

    def test_separators_in_a_reply_never_split_it(self):
        router = self.router()
        router._route_steps = (RouteStep("echo", lambda ctx: Stop("一⁣二⁤问号")),)
        assert router.handle(private_msg("m1", "x")) == "一二问号"


class BeforeTheMessageIsStored(StepCase):
    def test_self_message_stops_on_the_bots_name_even_decorated(self):
        router = self.router()
        for i, sender in enumerate((BOT, f"  {BOT}（机器人）  ", f"小{BOT}粉丝")):
            assert S.self_message(self.ctx(router, group_msg(f"s{i}", "x", sender_name=sender))) == Stop(None)
        assert self.trace.ignored == ["self_message"] * 3
        assert S.self_message(self.ctx(router, group_msg("s9", "x", sender_name="别人"))) is None

    def test_a_message_checked_by_wxid_is_not_judged_by_its_name(self):
        router = self.router()
        msg = group_msg("s1", "x", sender_name=BOT, _not_self_by_id=True)
        assert S.self_message(self.ctx(router, msg)) is None

    def test_without_a_display_name_nobody_is_the_bot(self):
        router = self.router(bot_display_name="")
        assert S.self_message(self.ctx(router, group_msg("s1", "x", sender_name=""))) is None

    def test_persist_stores_counts_and_continues_and_stops_on_a_duplicate(self):
        router = self.router()
        msg = group_msg("p1", "hello")
        assert S.persist(self.ctx(router, msg)) is None
        assert self.stored(router, "p1") == 1 and router.messages_processed == 1 and self.trace.steps == ["stored"]
        assert S.persist(self.ctx(router, msg)) == Stop(None)
        assert self.trace.ignored == ["duplicate_or_store_error"] and router.messages_processed == 1

    def test_persist_stops_when_the_database_fails(self):
        router = self.router()
        router._store = MagicMock(insert_message=MagicMock(side_effect=RuntimeError("disk full")))
        assert S.persist(self.ctx(router, group_msg("p1", "hello"))) == Stop(None)
        assert self.trace.ignored == ["duplicate_or_store_error"]


class TheServiceSteps(StepCase):
    def test_the_relay_answers_its_own_messages_and_passes_the_rest_on(self):
        relay = MagicMock()
        relay.handle.side_effect = lambda msg: (True, "收到 #1") if msg["content"].startswith("cc") else (False, None)
        router = self.router()
        router._claude_relay = relay
        assert S.claude_relay(self.ctx(router, admin_msg("r1", "cc 跑测试"))) == Stop("收到 #1")
        assert S.claude_relay(self.ctx(router, admin_msg("r2", "你好"))) is None
        assert self.trace.reasons == ["claude_relay"]
        router._claude_relay = None
        assert S.claude_relay(self.ctx(router, admin_msg("r3", "cc 跑测试"))) is None

    def test_a_relay_request_without_a_receipt_still_ends_the_route(self):
        router = self.router()
        router._claude_relay = MagicMock(handle=MagicMock(return_value=(True, None)))
        assert router.handle(admin_msg("r1", "cc 状态")) is None and self.trace.notes["ended_by"] == "claude_relay"

    def test_a_creators_message_goes_to_the_copilot_silently(self):
        prm = MagicMock(handle=MagicMock(return_value=True))
        router = self.router()
        router._prm = prm
        assert router.handle(private_msg("c1", "电话 13800000000", sender_id="wxid_creator")) is None
        assert self.trace.notes["ended_by"] == "prm" and self.trace.reasons[-1] == "prm_copilot"
        assert self.trace.redacted == "[已登记达人的私聊，内容不记录]"

    def test_a_breaking_copilot_still_swallows_a_creators_message(self):
        prm = MagicMock(handle=MagicMock(side_effect=RuntimeError("db locked")), is_party_chat=lambda chat: True)
        router = self.router()
        router._prm = prm
        assert S.prm(self.ctx(router, private_msg("c1", "你好", sender_id="wxid_creator"))) == Stop(None)
        prm.is_party_chat = lambda chat: False
        assert S.prm(self.ctx(router, private_msg("c2", "你好", sender_id="wxid_other"))) is None

    def test_the_edge_takes_a_listed_creator_before_anything_is_stored(self):
        edge = MagicMock(handle=MagicMock(return_value=True))
        router = self.router()
        router._edge = edge
        assert router.handle(private_msg("e1", "你好", sender_id="wxid_creator")) is None
        assert self.stored(router, "e1") == 0 and self.trace.notes["ended_by"] == "edge"
        assert self.trace.redacted == "[已登记达人的私聊，内容不记录]"

    def test_steps_without_their_service_pass_the_message_on(self):
        self.router()
        bare = SimpleNamespace(_config=SimpleNamespace(admin_wxid=ADMIN))
        for step in (S.edge, S.wechat_review, S.claude_relay, S.prm):
            assert step(RouteContext(router=bare, msg=private_msg("x", "你好"), trace=self.trace)) is None

    def test_the_admins_answer_to_a_draft_notification_ends_the_route_before_it_is_stored(self):
        """微信里审批草稿 (PRM_WECHAT_REVIEW): the review's answer is the reply, the message is not stored, the trace keeps
        no words; a message it does not take goes on to be stored as before; the switch off: the step passes."""
        review = MagicMock(on=True)
        review.answer.side_effect = lambda msg: (True, "#12 已批准") if msg["content"] == "1" else (False, None)
        router = self.router()
        router._prm = SimpleNamespace(wechat_review=review, handle=lambda msg: False)
        assert router.handle(admin_msg("w1", "1")) == "#12 已批准"
        assert self.trace.notes["ended_by"] == "wechat_review" and self.trace.reasons[-1] == "wechat_review"
        assert self.trace.redacted == "[管理员在微信里审批草稿，内容不记录]" and self.stored(router, "w1") == 0
        assert router.handle(admin_msg("w2", "你好")) is None and self.stored(router, "w2") == 1
        assert self.trace.notes["ended_by"] == "silence"
        review.on = False
        assert router.handle(admin_msg("w3", "1")) is None and self.stored(router, "w3") == 1
        assert review.answer.call_count == 2

    def test_a_breaking_review_still_keeps_an_answer_from_the_relay(self):
        review = MagicMock(on=True, answer=MagicMock(side_effect=RuntimeError("db locked")))
        router = self.router()
        router._prm = SimpleNamespace(wechat_review=review)
        review.claims.return_value = True
        assert S.wechat_review(self.ctx(router, admin_msg("w1", "发 12"))) == Stop(
            "处理这条草稿回复时出错了（详见日志）：请在控制台处理")
        review.claims.return_value = False
        assert S.wechat_review(self.ctx(router, admin_msg("w2", "cc 跑测试"))) is None
        review.claims.side_effect = RuntimeError("still locked")
        assert S.wechat_review(self.ctx(router, admin_msg("w3", "发 12"))) is None


class AdminCommands(StepCase):
    def test_the_admin_runs_account_commands_in_the_private_chat(self):
        router = self.router()
        reply = router.handle(admin_msg("a1", "帮助"))
        assert "查状态" in reply and "添加好友" in reply
        assert "未启用好友管理组件" in router.handle(admin_msg("a2", "同意好友"))
        assert router.handle(admin_msg("a3", f"{ADMIN}: 查状态")).startswith("账号状态")   # the quote prefix
        assert self.trace.reasons.count("admin_command") == 3

    def test_text_that_is_no_command_ends_in_silence(self):
        router = self.router()
        for i, text in enumerate(("你好", "总结一下", "改名 wxid_x = 小王", "抽签", "")):
            assert router.handle(admin_msg(f"a{i}", text)) is None
            assert self.trace.notes["ended_by"] == "silence"

    def test_nobody_else_and_no_group_runs_a_command(self):
        router = self.router()
        assert router.handle(private_msg("o1", "同意好友")) is None
        assert router.handle(group_msg("o2", "同意好友", sender_id=ADMIN, sender_name="管理员")) is None
        assert router.handle(group_msg("o3", f"@{BOT} 帮助", sender_id=ADMIN, is_at_mentioned=True)) is None
        assert self.trace.ignored.count("silent_end") == 3 and "admin_command" not in self.trace.reasons

    def test_without_an_admin_wxid_there_are_no_commands(self):
        router = self.router(admin_wxid="")
        assert router.handle(private_msg("o1", "帮助", sender_id="")) is None

    def test_the_handler_is_called_with_the_text_only(self):
        admin = MagicMock(handle=MagicMock(return_value="好"))
        router = self.router(admin=admin)
        assert router.handle(admin_msg("a1", "  查状态  ")) == "好"
        admin.handle.assert_called_once_with("查状态")
