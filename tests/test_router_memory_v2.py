"""MessageRouter with memory v2 (MEMORY_V2): the real engine, store and prompts."""

from unittest.mock import patch

from memory_support import make_stores
from test_pipeline_engine import ScriptedSummarizer
from test_router_pipeline import PipelineTestCase, TraceSpy
from test_router_priority import BOT, CHAT, NOW, StubWebSearch

from src.memory.context import FACTS_HEADER, NO_RELEVANT_MEMORY, RECALL_HEADER
import src.router as router_module

ANSWER = '{"addressee":"me","action":"reply","messages":["好呀"],"reason":"x"}'


class StepSpy(TraceSpy):
    def __init__(self):
        super().__init__()
        self.steps = []

    def step(self, code, **info):
        self.steps.append((code, info))


class Model(ScriptedSummarizer):
    def consolidate_memory(self, existing_memory, new_messages):
        return existing_memory


class MemoryRouterCase(PipelineTestCase):
    """Builds routers whose memory worker never starts a thread."""

    def setUp(self):
        super().setUp()
        starter = patch.object(router_module.MemoryWorker, "start", lambda self: None)
        starter.start()
        self.addCleanup(starter.stop)

    def build(self, *, memory="on", pipeline="single", store=None, **config):
        model = Model(*((ANSWER,) * 8))
        router, _ = self.make_router(
            summarizer=model, pipeline_v2=pipeline, memory_v2=memory,
            web=StubWebSearch(), store=store, **config,
        )
        router._tracer = self.trace = StepSpy()
        return router, model

    def fact(self, router, subject, predicate, obj, *, kind="other", chat=CHAT, confidence=0.9):
        quote = f"{subject}{predicate}{obj}"
        return router._mstore.upsert_fact(
            chat, kind=kind, subject=subject, predicate=predicate, obj=obj, confidence=confidence,
            evidence=[{"message_id": f"ev1-{subject}-{obj}", "quote": quote, "sender_id": "wxid_x",
                       "sender_name": "群友甲", "ts": NOW},
                      {"message_id": f"ev2-{subject}-{obj}", "quote": quote, "sender_id": "wxid_y",
                       "sender_name": "群友乙", "ts": NOW}], now=NOW,
        )[0]

    @staticmethod
    def prompt(model, index=-1):
        system, messages = model.calls[index]
        return system, messages[0]["content"]


class SwitchTests(MemoryRouterCase):
    def test_off_builds_no_memory_machinery_and_keeps_the_diary_path(self):
        router, _model = self.build(memory="off")
        assert router._mstore is None and router._memory_worker is None
        router.handle(self.other_msg("m1", "随便聊聊"))
        assert self.consolidation_checks == [CHAT]

    def test_on_notes_the_chat_for_the_background_job_instead_of_the_diary(self):
        router, _model = self.build()
        router.handle(self.other_msg("m1", "随便聊聊"))
        assert self.consolidation_checks == []
        assert router._memory_worker._dirty == {CHAT}

    def test_on_skips_the_hard_coded_alias_regex(self):
        router, _model = self.build()
        router.handle(self.other_msg("m1", "叫宇肆次元哥哥"))
        assert router._store.get_group_facts(CHAT) == []
        legacy, _ = self.build(memory="off")
        legacy.handle(self.other_msg("m2", "叫宇肆次元哥哥"))
        assert len(legacy._store.get_group_facts(CHAT)) == 2      # the alias and its "cy" shorthand

    def test_only_listed_groups_use_memory_v2(self):
        router, _model = self.build(memory_v2_groups=["别的群"])
        router.handle(self.other_msg("m1", "叫宇肆次元哥哥"))
        assert self.consolidation_checks == [CHAT]
        assert router._memory_worker._dirty == set()
        assert len(router._store.get_group_facts(CHAT)) == 2             # the old path still ran

    def test_listing_the_group_by_part_of_its_name_switches_it_on(self):
        router, _model = self.build(memory_v2_groups=["测试"])
        router.handle(self.other_msg("m1", "随便聊聊"))
        assert router._memory_worker._dirty == {CHAT}

    def test_a_store_without_a_connection_silently_stays_off(self):
        class Bare:
            pass
        with patch.object(router_module.MemoryWorker, "start", lambda self: None):
            router, _ = self.make_router(memory_v2="on", store=None)
        assert router._mstore is not None                                  # the real store has .conn
        cfg = self.make_config(memory_v2="on")
        router2 = router_module.MessageRouter(
            store=Bare(), detector=router._detector, summarizer=Model(ANSWER),
            admin_handler=router._admin, nickname_service=router._nicks, config=cfg,
            web_search_client=StubWebSearch(),
        )
        assert router2._mstore is None and router2._memory_worker is None


class MemoryInThePromptTests(MemoryRouterCase):
    def test_facts_about_the_speaker_and_the_alias_reach_the_system_prompt(self):
        router, model = self.build()
        self.fact(router, "小明", "喜欢", "美式咖啡")
        self.fact(router, "宇肆", "称呼是", "次元哥哥", kind="alias")
        self.fact(router, "老王", "住在", "杭州")
        router.handle(self.group_msg("m1", f"@{BOT} 次元哥哥今天来不来"))
        system, _user = self.prompt(model)
        assert FACTS_HEADER in system
        assert "小明喜欢美式咖啡" in system
        assert "宇肆 在这个群里的固定称呼/别名是「次元哥哥」" in system
        assert "老王" not in system
        assert "群友乙说：「" in system                                    # every line says who said it

    def test_recent_episodes_are_added_for_direct_conversation_only(self):
        router, model = self.build()
        router._mstore.add_episode(CHAT, summary="大家商量周六去爬山", topics=[], start_ts=NOW - 900,
                                   end_ts=NOW - 600, message_count=9, evidence=[], now=NOW)
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert "大家商量周六去爬山" in self.prompt(model)[0]

    def test_the_diary_is_used_until_v2_has_something_then_never_again(self):
        router, model = self.build()
        router._store.upsert_group_memory(CHAT, "旧日记：阿明是吉他手", 1, "m0")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert "旧日记：阿明是吉他手" in self.prompt(model)[0]               # cold start

        self.fact(router, "老王", "住在", "杭州")
        router.handle(self.group_msg("m2", f"@{BOT} 你好"))
        system = self.prompt(model)[0]
        assert "旧日记" not in system and NO_RELEVANT_MEMORY in system      # nothing relevant to say

    def test_legacy_alias_facts_are_still_shown(self):
        router, model = self.build()
        self.fact(router, "老王", "住在", "杭州")
        router._store.upsert_group_fact(CHAT, "宇肆", "alias", "次元哥哥", "m0", 0.9)
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert "宇肆 在这个群里的固定称呼/别名是「次元哥哥」" in self.prompt(model)[0]

    def test_a_fact_of_another_chat_is_never_used(self):
        router, model = self.build()
        self.fact(router, "小明", "喜欢", "美式咖啡", chat="elsewhere@chatroom")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert "美式咖啡" not in self.prompt(model)[0]

    def test_the_legacy_chat_path_gets_the_same_memory(self):
        router, model = self.build(pipeline="off")
        self.fact(router, "小明", "喜欢", "美式咖啡")
        summarizer = router._summarizer
        calls = []
        summarizer.chat = lambda **kw: calls.append(kw) or "好"
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert "小明喜欢美式咖啡" in calls[0]["group_memory"]

    def test_the_trace_names_the_sources(self):
        router, _model = self.build()
        fid = self.fact(router, "小明", "喜欢", "美式咖啡")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        [(code, info)] = [s for s in self.trace.steps if s[0] == "memory_v2"]
        assert info["facts"] == 1 and info["sources"]["facts"] == [fid]

    def test_a_retrieval_error_does_not_cost_the_reply(self):
        router, _model = self.build()
        with patch.object(router_module, "build_memory_context", side_effect=RuntimeError("boom")):
            assert router.handle(self.group_msg("m1", f"@{BOT} 你好")) == "好呀"

    def test_off_is_untouched(self):
        router, model = self.build(memory="off")
        router._store.upsert_group_memory(CHAT, "旧日记：阿明是吉他手", 1, "m0")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert "旧日记：阿明是吉他手" in self.prompt(model)[0]
        assert FACTS_HEADER not in self.prompt(model)[0]


class RecallTests(MemoryRouterCase):
    def old(self, router, message_id, text, days=3, sender="wxid_user2", name="土豆"):
        router._store.insert_message({
            "message_id": message_id, "chat_id": CHAT, "sender_id": sender, "sender_name": name,
            "content": text, "msg_type": 1, "timestamp": NOW - days * 86400,
        })

    def test_a_recall_question_gets_matching_old_messages_with_dates(self):
        router, model = self.build()
        router._mstore.enable_search_index()
        while not router._mstore.backfill_step():
            pass
        self.old(router, "o1", "上周五说好去吃烧烤")
        self.old(router, "o2", "今天股市大涨")
        router.handle(self.group_msg("m1", f"@{BOT} 之前谁说过去吃烧烤"))
        user = self.prompt(model)[1]
        assert RECALL_HEADER in user and "土豆：上周五说好去吃烧烤" in user
        assert "股市" not in user
        assert "之前谁说过去吃烧烤" not in user.split(RECALL_HEADER)[1]    # the question is not "found" again

    def test_it_works_without_the_index_through_like(self):
        router, model = self.build()
        self.old(router, "o1", "上周五说好去吃烧烤")
        router.handle(self.group_msg("m1", f"@{BOT} 之前谁说过去吃烧烤"))
        assert "土豆：上周五说好去吃烧烤" in self.prompt(model)[1]

    def test_a_two_character_topic_is_found(self):
        router, model = self.build()
        self.old(router, "o1", "我最近老是加班")
        router.handle(self.group_msg("m1", f"@{BOT} 之前谁说过加班"))
        assert "土豆：我最近老是加班" in self.prompt(model)[1]

    def test_ordinary_messages_do_not_trigger_a_search(self):
        router, model = self.build()
        self.old(router, "o1", "上周五说好去吃烧烤")
        router.handle(self.group_msg("m1", f"@{BOT} 我想吃烧烤"))
        assert RECALL_HEADER not in self.prompt(model)[1]

    def test_messages_already_in_the_context_are_not_repeated(self):
        router, model = self.build()
        router.handle(self.other_msg("m0", "刚才说想吃烧烤"))
        router.handle(self.group_msg("m1", f"@{BOT} 之前谁说过想吃烧烤"))
        user = self.prompt(model)[1]
        assert RECALL_HEADER not in user or "刚才说想吃烧烤" not in user.split(RECALL_HEADER)[1]

    def test_private_chats_never_search_the_group_archive(self):
        router, model = self.build()
        self.old(router, "o1", "上周五说好去吃烧烤")
        router.handle(self.private_msg("p1", "之前谁说过去吃烧烤"))
        assert RECALL_HEADER not in self.prompt(model)[1]

    def test_the_legacy_recall_is_unchanged_when_off(self):
        router, model = self.build(memory="off")
        self.old(router, "o1", "上周五说好去吃烧烤")
        router.handle(self.group_msg("m1", f"@{BOT} 之前谁说过去吃烧烤"))
        assert "【历史聊天检索结果】" in self.prompt(model)[1]


class BotReplyTests(MemoryRouterCase):
    def test_visible_replies_are_stored_apart_from_the_group_messages(self):
        router, _model = self.build()
        assert router.handle(self.group_msg("m1", f"@{BOT} 你好")) == "好呀"
        stored = router._mstore.recent_bot_replies(CHAT, 0)
        assert stored == ["好呀"]
        row = router._store.conn.execute("SELECT * FROM bot_replies").fetchone()
        assert row["trigger_message_id"] == "m1" and row["timestamp"] == NOW
        assert self.stored_ids(router) == ["m1"]

    def test_silence_stores_nothing(self):
        router, model = self.build()
        model.script = ['{"addressee":"others","action":"silent","messages":[],"reason":"x"}'] * 3
        router.handle(self.other_msg("m1", f"{BOT}你说呢", at=False))
        assert router._mstore.recent_bot_replies(CHAT, 0) == []

    def test_a_restarted_bot_remembers_what_it_said(self):
        router, _model = self.build()
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        restarted, model = self.build(store=router._store)
        restarted.handle(self.group_msg("m2", f"@{BOT} 再来"))
        assert "1. 好呀" in self.prompt(model)[1]

    def test_without_memory_v2_a_restart_forgets_as_before(self):
        router, _model = self.build(memory="off")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert router._store.conn.execute("SELECT COUNT(*) FROM bot_replies").fetchone()[0] == 0

    def test_old_replies_are_not_brought_back(self):
        router, _model = self.build()
        router._mstore.add_bot_reply(CHAT, "很久以前的话", NOW - 7200)
        restarted, model = self.build(store=router._store)
        restarted.handle(self.group_msg("m2", f"@{BOT} 再来"))
        assert "很久以前的话" not in self.prompt(model)[1]


class ProactiveTests(MemoryRouterCase):
    def test_a_proactive_turn_gets_facts_but_no_episodes(self):
        from test_router_pipeline import MODE
        router, model = self.build(proactive_enabled=True)
        self.fact(router, "土豆", "喜欢", "打游戏")
        router._mstore.add_episode(CHAT, summary="不该出现的摘要", topics=[], start_ts=1, end_ts=2,
                                   message_count=1, evidence=[], now=NOW)
        self.gate(router, allow=True, mode=MODE)
        router.handle(self.other_msg("m1", "决赛太精彩了"))
        system = self.prompt(model)[0]
        assert "土豆喜欢打游戏" in system and "不该出现的摘要" not in system


class GroupListTests(MemoryRouterCase):
    def other_group(self, message_id, content, **kwargs):
        return self.group_msg(
            message_id, content, chat_id="other@chatroom", group_name="别的群", **kwargs,
        )

    def test_by_default_every_group_and_private_chat_is_switched_over(self):
        router, _model = self.build()
        assert router._memory_groups == ("*",)
        assert router._memory_v2_for(self.group_msg("m1", "x")) and router._memory_v2_for(self.private_msg("p1", "x"))

    def test_a_listed_group_only_and_private_only_when_listed(self):
        router, _model = self.build(memory_v2_groups=["测试群"])
        assert router._memory_v2_for(self.group_msg("m1", "x"))
        assert not router._memory_v2_for(self.other_group("m2", "x"))
        assert not router._memory_v2_for(self.private_msg("p1", "x"))
        router, _model = self.build(memory_v2_groups=["private"])
        assert router._memory_v2_for(self.private_msg("p1", "x")) and not router._memory_v2_for(self.group_msg("m1", "x"))

    def test_an_unlisted_group_keeps_the_diary_the_old_recall_and_stores_no_bot_replies(self):
        router, model = self.build(memory_v2_groups=["测试群"])
        router._store.upsert_group_memory("other@chatroom", "旧日记：别的群的事", 1, "m0")
        router._mstore.upsert_fact("other@chatroom", kind="preference", subject="小明", predicate="喜欢",
                                   obj="美式咖啡", confidence=0.9, now=NOW, evidence=[
                                       {"message_id": "e1", "quote": "x", "sender_id": "a", "sender_name": "甲", "ts": NOW},
                                       {"message_id": "e2", "quote": "x", "sender_id": "b", "sender_name": "乙", "ts": NOW}])
        router._store.insert_message({"message_id": "old1", "chat_id": "other@chatroom", "sender_id": "wxid_user2",
                                      "sender_name": "土豆", "content": "上周五说好去吃烧烤", "msg_type": 1,
                                      "timestamp": NOW - 3 * 86400})
        router.handle(self.other_group("m1", f"@{BOT} 之前谁说过去吃烧烤"))
        system, user = self.prompt(model)
        assert "旧日记：别的群的事" in system and FACTS_HEADER not in system     # old memory path
        # the old recall: same header, and it finds the question itself (v2 leaves it out)
        assert "【历史聊天检索结果】" in user and "小明：@林小满 之前谁说过去吃烧烤" in user
        assert router._mstore.recent_bot_replies("other@chatroom", 0) == []
        assert router._store.conn.execute("SELECT COUNT(*) FROM bot_replies").fetchone()[0] == 0

    def test_a_proactive_turn_in_an_unlisted_group_uses_the_diary(self):
        from test_router_pipeline import MODE
        router, model = self.build(memory_v2_groups=["测试群"], proactive_enabled=True)
        router._store.upsert_group_memory("other@chatroom", "旧日记：别的群的事", 1, "m0")
        self.gate(router, allow=True, mode=MODE)
        router.handle(self.other_group("m1", "决赛太精彩了", sender_id="wxid_user2", sender_name="土豆"))
        assert "旧日记：别的群的事" in self.prompt(model)[0]


class LifecycleTests(MemoryRouterCase):
    def test_close_stops_the_worker_and_is_safe_without_one(self):
        router, _model = self.build()
        stopped = []
        router._memory_worker.stop = lambda *a, **k: stopped.append(True)
        router.close()
        assert stopped == [True]
        off, _model = self.build(memory="off")
        off.close()                                              # nothing to stop

    def test_the_bot_closes_the_router_before_it_closes_the_database(self):
        import inspect
        import src.bot as bot_module
        source = inspect.getsource(bot_module.Bot.run)
        assert source.index("close_router()") < source.index("self._conn.close()")

    def test_the_worker_gets_the_bots_names_so_its_own_lines_never_become_evidence(self):
        router, _model = self.build(bot_aliases=["柔柔"])
        assert "柔柔" in router._memory_worker.bot_names and BOT in router._memory_worker.bot_names
