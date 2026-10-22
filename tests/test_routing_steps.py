"""The router's priority chain, one step at a time (src/routing/steps.py)."""

from unittest.mock import MagicMock

from test_router_pipeline import PipelineSummarizer, TraceSpy, reply as decision_reply
from test_router_priority import BOT, CHAT, NOW, RouterTestCase, StubWebSearch

from src.routing import steps as S
from src.routing.context import RouteContext, RouteStep, Stop


class StepSpy(TraceSpy):
    def __init__(self):
        super().__init__()
        self.steps = []

    def step(self, code, **info):
        self.steps.append((code, info))


class StepCase(RouterTestCase):
    def router(self, **config):
        router, summarizer = self.make_router(
            summarizer=PipelineSummarizer(decision_reply("好呀")), web=StubWebSearch(),
            **{"pipeline_v2": "single", **config},
        )
        router._tracer = self.trace = StepSpy()
        self.summarizer = summarizer
        return router

    def ctx(self, router, msg, **fields):
        return RouteContext(router=router, msg=msg, trace=self.trace, **fields)

    def stored(self, router, message_id):
        return router._store.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id = ?", (message_id,)).fetchone()[0]


class OrderTests(StepCase):
    def test_the_chain_has_the_documented_order_and_unique_names(self):
        names = [step.name for step in S.ROUTE_STEPS]
        assert names == [
            "self_message", "persist", "claude_relay", "relay_only", "memory_hooks", "full_feed", "quoted_image", "image_message",
            "close_image_window", "private_chat", "quote_of_other_member", "welcome", "detect_trigger",
            "mention_gate", "feishu_export", "help_command", "fun_commands", "admin_command",
            "todo_command", "chat_request", "ambient", "finish",
        ]
        assert len(set(names)) == len(names) and all(step.doc for step in S.ROUTE_STEPS)

    def test_the_step_that_ended_the_route_is_named_in_the_trace(self):
        router = self.router()
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert self.trace.notes["ended_by"] == "finish"
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))                    # the same id again
        assert self.trace.notes["ended_by"] == "persist"
        router.handle(self.group_msg("m2", "我自己", sender_name=BOT))
        assert self.trace.notes["ended_by"] == "self_message"

    def test_a_router_can_run_a_different_chain(self):
        router = self.router()
        calls = []
        router._route_steps = (
            RouteStep("first", lambda ctx: calls.append("first")),
            RouteStep("second", lambda ctx: Stop("custom")),
            RouteStep("never", lambda ctx: calls.append("never")),
        )
        assert router.handle(self.group_msg("m1", "x")) == "custom"
        assert calls == ["first"] and self.trace.notes["ended_by"] == "second"

    def test_a_chain_that_never_stops_returns_nothing(self):
        router = self.router()
        router._route_steps = (RouteStep("noop", lambda ctx: None),)
        assert router.handle(self.group_msg("m1", "x")) is None


class BeforeTheMessageIsStored(StepCase):
    def test_self_message_stops_on_the_bots_name_even_decorated(self):
        router = self.router()
        for sender in (BOT, f"  {BOT}（机器人）  ", f"小{BOT}粉丝"):
            outcome = S.self_message(self.ctx(router, self.group_msg("s1", "x", sender_name=sender)))
            assert outcome == Stop(None)
        assert "self_message" in self.trace.reasons
        assert S.self_message(self.ctx(router, self.group_msg("s2", "x"))) is None

    def test_persist_stores_counts_and_continues_and_stops_on_a_duplicate(self):
        router = self.router()
        msg = self.group_msg("p1", "你好")
        assert S.persist(self.ctx(router, msg)) is None
        assert router.messages_processed == 1 and self.stored(router, "p1") == 1
        assert ("stored", {}) in self.trace.steps
        assert S.persist(self.ctx(router, msg)) == Stop(None)
        assert router.messages_processed == 1 and "duplicate_or_store_error" in self.trace.reasons

    def test_persist_still_welcomes_when_the_database_fails(self):
        router = self.router(welcome_enabled=True)
        router._store.insert_message = MagicMock(side_effect=RuntimeError("disk full"))
        router._handle_welcome = MagicMock(return_value="欢迎小红")
        assert S.persist(self.ctx(router, self.join_msg("j1"))) == Stop("欢迎小红")
        assert "welcome" in self.trace.reasons

    def test_persist_ignores_a_stale_join_when_the_database_fails(self):
        router = self.router(welcome_enabled=True)
        router._store.insert_message = MagicMock(return_value=False)
        router._handle_welcome = MagicMock()
        outcome = S.persist(self.ctx(router, self.join_msg("j2", timestamp=NOW - 10_000)))
        assert outcome == Stop(None) and not router._handle_welcome.called
        assert any(code == "stale_join_ignored" for code, _ in self.trace.steps)

    def test_memory_hooks_pick_the_diary_trigger_or_the_memory_job(self):
        router = self.router()
        ctx = self.ctx(router, self.other_msg("h1", "叫宇肆次元哥哥"))
        assert S.memory_hooks(ctx) is None
        assert self.consolidation_checks == [CHAT]
        assert len(router._store.get_group_facts(CHAT)) == 2           # the old regex facts
        router, _ = self.make_router(summarizer=PipelineSummarizer(), memory_v2="on")
        router._memory_worker.start = lambda: None
        ctx = RouteContext(router=router, msg=self.other_msg("h2", "叫宇肆次元哥哥"), trace=TraceSpy())
        assert S.memory_hooks(ctx) is None
        assert router._memory_worker._dirty == {CHAT} and router._store.get_group_facts(CHAT) == []


class RoutesThatSkipTheMentionLogic(StepCase):
    def test_private_chats_are_answered_directly(self):
        router = self.router()
        outcome = S.private_chat(self.ctx(router, self.private_msg("q1", "在吗")))
        assert outcome == Stop("好呀") and ("route_private", {}) in self.trace.steps
        assert S.private_chat(self.ctx(router, self.group_msg("q2", "在吗"))) is None

    def test_a_quote_of_another_member_is_not_for_the_bot(self):
        router = self.router()
        msg = self.other_msg("q3", "厉害", quoted_content="我拿奖了", quoted_sender_name="阿明")
        assert S.quote_of_other_member(self.ctx(router, msg)) == Stop(None)
        assert "quote_of_other_member" in self.trace.reasons
        for keep in ({"is_reply_to_bot": True}, {"is_at_mentioned": True}, {"content": f"{BOT} 厉害"}):
            assert S.quote_of_other_member(self.ctx(router, {**msg, **keep})) is None
        assert S.quote_of_other_member(self.ctx(router, self.other_msg("q4", "厉害"))) is None

    def test_a_join_event_is_welcomed_unless_it_is_old(self):
        router = self.router(welcome_enabled=True)
        router._handle_welcome = MagicMock(return_value="欢迎")
        assert S.welcome(self.ctx(router, self.join_msg("w1"))) == Stop("欢迎")
        assert S.welcome(self.ctx(router, self.join_msg("w2", timestamp=NOW - 10_000))) is None
        assert S.welcome(self.ctx(router, self.group_msg("w3", "你好"))) is None
        router._config.welcome_enabled = False
        assert S.welcome(self.ctx(router, self.join_msg("w4"))) is None

    def test_an_image_message_without_a_follow_up_ends_the_route_quietly(self):
        router = self.router()
        router._handle_image_followup = MagicMock(return_value=None)
        assert S.image_message(self.ctx(router, self.sticker_msg("i1"))) == Stop(None)
        assert "image_message_no_followup" in self.trace.reasons
        assert S.image_message(self.ctx(router, self.group_msg("i2", "文字"))) is None

    def test_an_image_message_with_a_follow_up_is_answered_and_does_not_reopen_the_window(self):
        router = self.router()
        router._handle_image_followup = MagicMock(return_value="好可爱")
        router._finish_visible_reply = MagicMock(return_value="好可爱")
        assert S.image_message(self.ctx(router, self.sticker_msg("i3"))) == Stop("好可爱")
        assert router._finish_visible_reply.call_args.kwargs == {"open_image_followup": False}

    def test_a_quoted_image_question_is_answered_first(self):
        router = self.router()
        router._handle_quoted_image_question = MagicMock(return_value="这是一只猫")
        assert S.quoted_image(self.ctx(router, self.group_msg("i4", "这是什么"))) == Stop("这是一只猫")
        router._handle_quoted_image_question = MagicMock(return_value=None)
        assert S.quoted_image(self.ctx(router, self.group_msg("i5", "这是什么"))) is None

    def test_a_text_message_closes_the_image_window(self):
        router = self.router()
        router._image_followup = MagicMock()
        msg = self.group_msg("i6", "文字")
        assert S.close_image_window(self.ctx(router, msg)) is None
        router._image_followup.clear.assert_called_once_with(CHAT, "wxid_user1", msg["timestamp"])
        router._image_followup = None
        assert S.close_image_window(self.ctx(router, msg)) is None

    def test_the_full_feed_persona_takes_its_group_before_anything_else(self):
        router = self.router()
        router._full_feed_mode = MagicMock(return_value="ambient")
        router._should_consider_ambient_turn = MagicMock(return_value=False)
        assert S.full_feed(self.ctx(router, self.other_msg("f1", "随便"))) == Stop(None)
        assert "full_feed_local_skip" in self.trace.reasons
        router._should_consider_ambient_turn = MagicMock(return_value=True)
        router._handle_full_feed_chat = MagicMock(return_value="嗯")
        assert S.full_feed(self.ctx(router, self.other_msg("f2", "随便"))) == Stop("嗯")
        assert router._handle_full_feed_chat.call_args.kwargs == {"force_reply": False}
        router._full_feed_mode = MagicMock(return_value="always_reply")
        S.full_feed(self.ctx(router, self.other_msg("f3", "随便")))
        assert router._handle_full_feed_chat.call_args.kwargs == {"force_reply": True}
        router._full_feed_mode = MagicMock(return_value=None)
        assert S.full_feed(self.ctx(router, self.other_msg("f4", "随便"))) is None


class IsThisACall(StepCase):
    def detect(self, router, msg):
        ctx = self.ctx(router, msg)
        assert S.detect_trigger(ctx) is None
        return ctx

    def test_an_at_mention(self):
        router = self.router()
        ctx = self.detect(router, self.group_msg("d1", f"@{BOT} 你好"))
        assert ctx.is_at and not ctx.sticky_triggered and self.trace.notes["route"] == "at"

    def test_a_plain_message_is_ambient(self):
        router = self.router()
        ctx = self.detect(router, self.other_msg("d2", "今天吃啥"))
        assert not ctx.is_at and self.trace.notes["route"] == "ambient" and ctx.raw == "今天吃啥"

    def test_the_quote_prefix_is_removed_from_raw(self):
        router = self.router()
        ctx = self.detect(router, self.other_msg("d3", "wxid_abc123:\n今天吃啥"))
        assert ctx.raw == "今天吃啥"

    def test_sticky_listening_after_a_bare_at(self):
        router = self.router()
        router._sticky.register(CHAT, "wxid_user2")
        msg = self.other_msg("d4", "那明天呢")
        ctx = self.detect(router, msg)
        assert ctx.is_at and ctx.sticky_triggered and msg["_sticky_triggered"]
        assert self.trace.notes["route"] == "sticky"
        assert not self.detect(router, self.other_msg("d5", "那明天呢")).is_at          # one shot

    def test_a_keyword_trigger_arms_sticky_listening_unless_it_is_stale(self):
        router, _ = self.make_router(keywords=["小柔"])
        router._tracer = self.trace = StepSpy()
        fresh = self.other_msg("d6", "小柔在吗")
        ctx = self.detect(router, fresh)
        assert ctx.is_at and fresh["_kw_triggered"] and self.trace.notes["route"] == "keyword"
        assert router._sticky.consume(CHAT, "wxid_user2")
        stale = self.other_msg("d7", "小柔在吗", timestamp=NOW - 10_000)
        assert self.detect(router, stale).is_at
        assert not router._sticky.consume(CHAT, "wxid_user2")

    def test_a_name_call_needs_the_pipeline(self):
        router, _ = self.make_router(summarizer=PipelineSummarizer(), pipeline_v2="single")
        router._tracer = self.trace = StepSpy()
        msg = self.other_msg("d8", f"{BOT}你觉得呢")
        ctx = self.detect(router, msg)
        assert ctx.is_at and msg["_name_call_triggered"] and self.trace.notes["route"] == "name_call"
        off, _ = self.make_router(pipeline_v2="off")
        off._tracer = self.trace = StepSpy()
        assert not self.detect(off, self.other_msg("d9", f"{BOT}你觉得呢")).is_at


class TheMentionPath(StepCase):
    def at(self, router, content, **fields):
        msg = self.group_msg("a1", content)
        ctx = self.ctx(router, msg, is_at=True, **fields)
        return ctx

    def test_a_stale_mention_is_dropped(self):
        router = self.router()
        ctx = self.ctx(router, self.group_msg("a2", f"@{BOT} 你好", timestamp=NOW - 10_000), is_at=True)
        assert S.mention_gate(ctx) == Stop(None) and "stale_mention_ignored" in self.trace.reasons

    def test_the_bot_name_and_the_quote_prefix_are_cut_out(self):
        router = self.router()
        ctx = self.at(router, f"wxid_abc:\n@{BOT} 今晚吃什么")
        assert S.mention_gate(ctx) is None and ctx.clean_content == "今晚吃什么"
        ctx = self.at(router, f"@{BOT} wxid_abc123:\n今晚吃什么")
        assert S.mention_gate(ctx) is None and ctx.clean_content == "今晚吃什么"

    def test_a_bare_at_becomes_a_greeting_and_arms_sticky(self):
        router = self.router()
        ctx = self.at(router, f"@{BOT}")
        assert S.mention_gate(ctx) is None and ctx.clean_content == "在吗"
        assert ("empty_mention", {}) in self.trace.steps
        assert router._sticky.consume(CHAT, "wxid_user1")

    def test_every_mention_step_is_skipped_for_a_message_that_is_no_call(self):
        router = self.router()
        ctx = self.ctx(router, self.other_msg("a3", "帮助"), clean_content="帮助")
        for step in (S.mention_gate, S.feishu_export, S.help_command, S.fun_commands, S.admin_command,
                     S.todo_command, S.chat_request):
            assert step(ctx) is None
        assert ctx.reply is None and not self.trace.reasons

    def test_help(self):
        router = self.router()
        router._admin.handle = MagicMock(return_value="命令列表")
        for word in ("帮助", "help", "命令"):
            ctx = self.ctx(router, self.group_msg("a4", word), is_at=True, clean_content=word)
            S.help_command(ctx)
            assert ctx.reply == "命令列表"
        ctx = self.ctx(router, self.group_msg("a5", "帮助我"), is_at=True, clean_content="帮助我")
        S.help_command(ctx)
        assert ctx.reply is None
        ctx = self.ctx(router, self.group_msg("a6", "帮助", _name_call_triggered=True), is_at=True,
                       clean_content="帮助")
        S.help_command(ctx)
        assert ctx.reply is None                                     # a name call is chat, not a command

    def test_lots_and_tarot(self):
        router = self.router(fun_enabled=True)
        ctx = self.ctx(router, self.group_msg("a7", f"@{BOT} 抽签"), is_at=True, clean_content="抽签")
        S.fun_commands(ctx)
        assert "抽签结果" in ctx.reply and "lots_command" in self.trace.reasons
        ctx = self.ctx(router, self.group_msg("a8", f"@{BOT} 塔罗 我的工作"), is_at=True,
                       clean_content="塔罗 我的工作")
        S.fun_commands(ctx)
        assert ctx.reply and "tarot_command" in self.trace.reasons
        router._config.fun_enabled = False
        ctx = self.ctx(router, self.group_msg("a9", "抽签"), is_at=True, clean_content="抽签")
        S.fun_commands(ctx)
        assert ctx.reply is None

    def test_a_fun_command_replaces_an_earlier_reply(self):
        router = self.router(fun_enabled=True)
        ctx = self.ctx(router, self.group_msg("a10", "抽签"), is_at=True, clean_content="抽签", reply="先前的回复")
        S.fun_commands(ctx)
        assert "抽签结果" in ctx.reply

    def test_the_admin_runs_commands_others_do_not(self):
        router = self.router(admin_wxid="wxid_user1")
        router._admin.handle = MagicMock(return_value="已重启")
        ctx = self.ctx(router, self.group_msg("a11", "重启"), is_at=True, clean_content="重启")
        S.admin_command(ctx)
        assert ctx.reply == "已重启" and "admin_command" in self.trace.reasons
        other = self.ctx(router, self.other_msg("a12", "重启"), is_at=True, clean_content="重启")
        S.admin_command(other)
        assert other.reply is None

    def test_the_todo_list_answers_in_todo_groups_only(self):
        router = self.router(todo_enabled=True, todo_groups=["*"])
        ctx = self.ctx(router, self.group_msg("a13", "待办 买牛奶"), is_at=True, clean_content="待办 买牛奶")
        S.todo_command(ctx)
        assert ctx.reply and "todo_command" in self.trace.reasons
        router._todo_handler.handle = MagicMock(side_effect=RuntimeError("boom"))
        ctx = self.ctx(router, self.group_msg("a14", "待办"), is_at=True, clean_content="待办")
        assert S.todo_command(ctx) is None and ctx.reply is None            # a failing handler is contained

    def test_chat_is_the_fallback(self):
        router = self.router()
        ctx = self.ctx(router, self.group_msg("a15", f"@{BOT} 你好"), is_at=True, clean_content="你好")
        assert S.chat_request(ctx) is None and ctx.reply == "好呀" and "chat_request" in self.trace.reasons
        done = self.ctx(router, self.group_msg("a16", "x"), is_at=True, clean_content="你好", reply="命令结果")
        S.chat_request(done)
        assert done.reply == "命令结果"                                       # a command already answered


class Ambient(StepCase):
    def test_a_closed_gate_ends_the_route_in_silence(self):
        router = self.router()
        self.gate(router, allow=False)
        assert S.ambient(self.ctx(router, self.other_msg("n1", "闲聊"))) == Stop(None)
        assert "proactive_gate_closed" in self.trace.reasons

    def test_an_open_gate_asks_for_a_proactive_reply_and_continues(self):
        from test_router_pipeline import MODE
        router = self.router()
        self.gate(router, allow=True, mode=MODE)
        router._handle_proactive_chat = MagicMock(return_value="接一句")
        ctx = self.ctx(router, self.other_msg("n2", "闲聊"))
        assert S.ambient(ctx) is None and ctx.reply == "接一句"
        assert any(code == "proactive_gate_open" for code, _ in self.trace.steps)

    def test_a_call_skips_the_ambient_step(self):
        router = self.router()
        assert S.ambient(self.ctx(router, self.group_msg("n3", "x"), is_at=True)) is None

    def test_the_feishu_auto_export_never_stops_the_route(self):
        router = self.router()
        router._feishu_export = MagicMock()
        router._feishu_export.maybe_auto_export.side_effect = RuntimeError("down")
        self.gate(router, allow=False)
        assert S.ambient(self.ctx(router, self.other_msg("n4", "闲聊"))) == Stop(None)

    def test_finish_cleans_the_reply_and_registers_it(self):
        router = self.router()
        outcome = S.finish(self.ctx(router, self.group_msg("n5", "x"), reply="**好**呀😀"))
        assert outcome == Stop("好呀")
        assert router._recent_replies.recent(CHAT) == ("好呀",)
        assert S.finish(self.ctx(router, self.group_msg("n6", "x"), reply=None)) == Stop(None)
