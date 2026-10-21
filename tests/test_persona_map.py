"""Different personas in different groups (data/persona_map.json)."""

import json

import pytest
from unittest.mock import MagicMock

from test_pipeline_engine import ScriptedSummarizer
from test_router_pipeline import PipelineSummarizer, TraceSpy, reply as decision_reply
from test_router_priority import BOT, CHAT, NOW, RouterTestCase, StubWebSearch

import src.persona as P
from src.pipeline.types import ChatRequest
from src.proactive.modes import ProactiveMode

GOOD = {"id": "me", "name": "小书", "prompt": "你是爱读书的群友PROMPT-ME", "reply_rules": "RULES-ME",
        "max_chars": 20, "context_window_sec": 600, "context_count": 7}
FEED = {"groups": ["读书群"], "always_reply_groups": ["必回群"], "ambient_cooldown_sec": 30,
        "skip_reactions": ["嗯", "好"], "skip_words": ["ai", "机器人"], "min_chars": 4}


# ── the map ─────────────────────────────────────────────────────────────────

def test_a_map_is_read_and_normalised():
    pmap = P.parse_persona_map({"groups": {" 读书群 ": "me", "Game": "yang_yujing", "": "x", "y": ""},
                                "private": "me", "default": None})
    assert pmap.groups == (("读书群", "me"), ("game", "yang_yujing"))
    assert (pmap.private, pmap.default, pmap.active) == ("me", "", True)
    assert not P.parse_persona_map({}).active


@pytest.mark.parametrize("bad", [[], "x", {"groups": []}, {"groups": {"a": 1}}, {"private": 5}, {"default": ["x"]}])
def test_a_malformed_map_is_refused(bad):
    with pytest.raises(P.PersonaError):
        P.parse_persona_map(bad)


def test_a_missing_or_broken_map_file_changes_nothing(tmp_path, caplog):
    assert not P.load_persona_map(tmp_path / "none.json").active
    broken = tmp_path / "m.json"
    for content in ("{oops", "[1]", json.dumps({"groups": 3})):
        broken.write_text(content, encoding="utf-8")
        assert not P.load_persona_map(broken).active
    assert "ignored" in caplog.text


def registry(groups=None, **kw):
    pmap = P.parse_persona_map({"groups": groups or {}, **kw})
    return P.PersonaRegistry({"me": P.persona_from_dict(GOOD),
                              "yang_yujing": P.builtin_personas()["yang_yujing"]}, pmap)


def group(name="读书群", chat="chat1@chatroom"):
    return {"is_group": True, "group_name": name, "chat_id": chat}


def test_a_group_is_matched_by_part_of_its_name_or_its_chat_id_in_file_order():
    reg = registry({"读书": "me", "群": "yang_yujing", "9988": "yang_yujing"})
    assert reg.persona_for(group("我们的读书群")).id == "me"             # the first matching entry wins
    assert reg.persona_for(group("游戏群")).id == "yang_yujing"
    assert reg.persona_for(group("别的", "9988@chatroom")) .id == "yang_yujing"
    assert registry({"读书": "me"}).persona_for(group("游戏群")) is None


def test_case_wildcard_private_and_default():
    reg = registry({"Book": "me"})
    assert reg.persona_for(group("my BOOK club")).id == "me"
    assert registry({"*": "me"}).persona_for(group("任何群")).id == "me"
    reg = registry({"读书": "me"}, private="yang_yujing", default="me")
    assert reg.persona_for({"is_group": False, "chat_id": "wxid_x"}).id == "yang_yujing"
    assert reg.persona_for(group("别的群")).id == "me"
    assert registry({"读书": "me"}).persona_for({"is_group": False}) is None


def test_an_unknown_persona_id_is_reported_and_means_the_configured_persona(caplog):
    reg = registry({"读书": "ghost"}, default="ghost2")
    assert reg.unknown_ids() == ["ghost", "ghost2"] and "ghost" in caplog.text
    assert reg.persona_for(group("读书群")) is None


def test_without_a_map_the_registry_is_inactive_and_loads_nothing(tmp_path):
    reg = P.PersonaRegistry.from_files(user_dir=tmp_path, map_path=tmp_path / "none.json")
    assert not reg.active and reg.personas == {} and reg.persona_for(group()) is None


def test_the_registry_reads_builtin_and_user_personas_when_a_map_exists(tmp_path):
    (tmp_path / "personas").mkdir()
    (tmp_path / "personas" / "me.json").write_text(json.dumps(GOOD), encoding="utf-8")
    (tmp_path / "map.json").write_text(json.dumps({"groups": {"读书": "me", "游戏": "yang_yujing"}}), encoding="utf-8")
    reg = P.PersonaRegistry.from_files(user_dir=tmp_path / "personas", map_path=tmp_path / "map.json")
    assert reg.persona_for(group("读书群")).prompt == GOOD["prompt"]
    assert reg.persona_for(group("游戏群")).name == "杨宇靖" and reg.unknown_ids() == []


# ── the summarizer prompts ───────────────────────────────────────────────────

def test_the_engine_prefers_the_persona_of_the_request():
    model = ScriptedSummarizer('{"addressee":"me","action":"reply","messages":["好"],"reason":"x"}')
    request = ChatRequest(message="你好", bot_name="b", requester_name="张三",
                          persona=P.persona_from_dict(GOOD).settings())
    from src.pipeline.engine import decide
    decide(model, request, strategy="single")
    system, _ = model.calls[0]
    assert "PROMPT-ME" in system and "RULES-ME" in system and "PERSONA-TEXT" not in system
    assert "小书" in system and "不超过 20 个字" in system


def test_the_engine_uses_the_configured_persona_without_one():
    model = ScriptedSummarizer('{"addressee":"me","action":"reply","messages":["好"],"reason":"x"}')
    from src.pipeline.engine import decide
    decide(model, ChatRequest(message="你好", bot_name="b", requester_name="张三"), strategy="single")
    assert "PERSONA-TEXT" in model.calls[0][0]


class Legacy(ScriptedSummarizer):
    def _call_chat_api(self, system_prompt, messages):
        self.calls.append((system_prompt, messages))
        return "嗯"


def test_the_legacy_chat_and_proactive_prompts_take_a_persona_too():
    model = Legacy()
    settings = P.persona_from_dict(GOOD).settings()
    model.chat("你好", requester_name="张三", persona_settings=settings)
    assert "PROMPT-ME" in model.calls[-1][0] and "PERSONA-TEXT" not in model.calls[-1][0]
    mode = ProactiveMode("CASUAL", "闲聊", "d", "i", 4.0, 120, 0.25, 50, 50)
    ctx = [{"sender_name": "张三", "content": "今天吃啥", "timestamp": NOW}]
    model.proactive_chat(mode, ctx, persona_settings=settings)
    assert "PROMPT-ME" in model.calls[-1][0]
    model.chat("你好", requester_name="张三")
    assert "PERSONA-TEXT" in model.calls[-1][0]
    model.proactive_chat(mode, ctx)
    assert "PERSONA-TEXT" in model.calls[-1][0]


# ── the router ──────────────────────────────────────────────────────────────

class MappedRouterCase(RouterTestCase):
    def write_map(self, groups, personas=(), **extra):
        (self.tmp / "data" / "personas").mkdir(exist_ok=True)
        for persona in personas:
            (self.tmp / "data" / "personas" / f"{persona['id']}.json").write_text(
                json.dumps(persona, ensure_ascii=False), encoding="utf-8")
        (self.tmp / "data" / "persona_map.json").write_text(
            json.dumps({"groups": groups, **extra}, ensure_ascii=False), encoding="utf-8")

    def build(self, *, pipeline="single", summarizer=None, **config):
        config.setdefault("chat_persona_profile", "custom")      # the 杨宇靖 preset would override the map
        router, summarizer = self.make_router(
            summarizer=summarizer or PipelineSummarizer(decision_reply("好呀")), web=StubWebSearch(),
            pipeline_v2=pipeline, **config)
        router._tracer = self.trace = TraceSpy()
        return router, summarizer

    def other_group(self, message_id, content, **kwargs):
        return self.group_msg(message_id, content, chat_id="other@chatroom", group_name="别的群", **kwargs)


class WithAMapTests(MappedRouterCase):
    def test_a_mapped_group_gets_its_persona_in_the_request_others_do_not(self):
        self.write_map({"测试群": "me"}, [GOOD])
        router, model = self.build()
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        router.handle(self.other_group("m2", f"@{BOT} 你好"))
        assert model.decide_requests[0].persona == P.persona_from_dict(GOOD).settings()
        assert model.decide_requests[1].persona is None

    def test_the_legacy_chat_call_gets_the_persona_only_when_mapped(self):
        self.write_map({"测试群": "me"}, [GOOD])
        router, model = self.build(pipeline="off")
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        router.handle(self.other_group("m2", f"@{BOT} 你好"))
        assert model.chat_calls[0]["persona_settings"]["persona_name"] == "小书"
        assert "persona_settings" not in model.chat_calls[1]                     # exactly the old call

    def test_the_personas_own_context_limits_are_used(self):
        self.write_map({"测试群": "me"}, [{**GOOD, "context_count": 2}])
        router, model = self.build()
        for i in range(5):
            router.handle(self.other_msg(f"c{i}", f"闲聊第{i}句"))
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert len(model.decide_requests[0].context_messages) == 2

    def test_the_personas_name_counts_as_calling_the_bot_in_its_group(self):
        self.write_map({"测试群": "me"}, [GOOD])
        router, model = self.build()
        assert "小书" in router._names_for(self.group_msg("n1", "x"))
        assert "小书" not in router._names_for(self.other_group("n2", "x"))
        self.gate(router, allow=False)
        router.handle(self.other_msg("n3", "小书你觉得呢", at=False))
        router.handle(self.other_group("n4", "小书你觉得呢", at=False, sender_id="wxid_user2", sender_name="土豆"))
        assert len(model.decide_requests) == 1                                   # only in the mapped group

    def test_a_proactive_reply_uses_the_mapped_persona(self):
        from test_router_pipeline import MODE
        self.write_map({"测试群": "me"}, [GOOD])
        router, model = self.build(proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        router.handle(self.other_msg("p1", "决赛太精彩了"))
        assert model.decide_requests[-1].persona["persona_name"] == "小书"

    def test_the_legacy_proactive_call_gets_the_persona(self):
        from test_router_pipeline import MODE
        self.write_map({"测试群": "me"}, [GOOD])
        router, model = self.build(pipeline="off", proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        router.handle(self.other_msg("p2", "决赛太精彩了"))
        assert model.proactive_calls[-1]["persona_settings"]["persona_name"] == "小书"

    def test_private_chats_and_the_default(self):
        self.write_map({"测试群": "me"}, [GOOD], private="me")
        router, model = self.build()
        router.handle(self.private_msg("q1", "在吗"))
        assert model.decide_requests[0].persona["persona_name"] == "小书"

    def test_a_missing_persona_file_means_the_configured_persona(self):
        self.write_map({"测试群": "nobody"})
        router, model = self.build()
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert model.decide_requests[0].persona is None


class WithoutAMapTests(MappedRouterCase):
    def test_no_file_means_nothing_changes(self):
        router, model = self.build()
        assert not router._personas.active and router._persona_for(self.group_msg("m1", "x")) is None
        router.handle(self.group_msg("m1", f"@{BOT} 你好"))
        assert model.decide_requests[0].persona is None
        assert router._names_for(self.group_msg("m2", "x")) is router._bot_names
        legacy, lmodel = self.build(pipeline="off")
        legacy.handle(self.group_msg("m3", f"@{BOT} 你好"))
        assert "persona_settings" not in lmodel.chat_calls[0]


class FullFeedPersonaTests(MappedRouterCase):
    PERSONA = {**GOOD, "id": "feed", "full_feed": FEED}

    def mode(self, router, msg):
        return router._full_feed_mode(msg)

    def test_a_mapped_full_feed_persona_listens_to_its_group(self):
        self.write_map({"测试群": "feed", "必回": "feed"}, [self.PERSONA])
        router, _ = self.build()
        assert self.mode(router, self.other_msg("f1", "随便聊聊")) == "ambient"
        always = self.group_msg("f2", "x", chat_id="a@chatroom", group_name="必回群", sender_id="wxid_a")
        assert self.mode(router, always) == "always_reply"
        assert self.mode(router, self.other_group("f3", "x")) is None                 # not mapped
        assert self.mode(router, self.private_msg("f4", "x")) is None
        assert self.mode(router, self.join_msg("f5")) is None
        assert self.mode(router, self.other_msg("f6", "x", timestamp=NOW - 10_000)) is None   # stale
        assert self.mode(router, self.other_msg("f7", "x", sender_id="")) is None

    def test_a_persona_without_full_feed_is_not_a_listener(self):
        self.write_map({"测试群": "me"}, [GOOD])
        router, _ = self.build()
        assert self.mode(router, self.other_msg("f8", "随便聊聊")) is None

    def test_the_pacing_comes_from_the_personas_feed(self):
        self.write_map({"测试群": "feed"}, [self.PERSONA])
        router, _ = self.build()
        should = lambda text, mid: router._should_consider_ambient_turn(self.other_msg(mid, text))
        assert should("嗯", "g1") is False                       # a listed one-word reaction
        assert should("今天天气不错", "g2") is True
        router._ambient_next_turn_at.clear()
        assert should("好", "g3") is False
        assert should("嗯嗯嗯", "g4") is False                    # shorter than min_chars (4)
        assert should("讨论一下AI吧大家", "g5") is False          # the persona's skip pattern
        assert should("周末去爬山好不好", "g6") is True
        assert should("周末去爬山好不好", "g7") is False          # cool-down of this persona

    def test_a_directly_addressed_persona_is_always_considered(self):
        self.write_map({"测试群": "feed"}, [self.PERSONA])
        router, _ = self.build()
        assert router._should_consider_ambient_turn(self.other_msg("g8", "@小书 嗯"))

    def test_a_full_feed_persona_gets_facts_but_not_the_diary_on_ambient_turns(self):
        self.write_map({"测试群": "feed"}, [self.PERSONA])
        router, _ = self.build()
        router._store.upsert_group_memory(CHAT, "旧日记：不该出现", 1, "m0")
        router._store.upsert_group_fact(CHAT, "宇肆", "alias", "次元哥哥", "m0", 0.9)
        text = router._group_memory_for_prompt(self.other_msg("h1", "x"), "x", [], ambient=True)
        assert "旧日记" not in text and "次元哥哥" in text
        plain = router._group_memory_for_prompt(self.other_msg("h2", "x"), "x", [], ambient=False)
        assert "旧日记：不该出现" in plain

    def test_the_global_profile_is_unchanged(self):
        router, _ = self.build(chat_persona_profile="yang_yujing", yang_yujing_group_name="测试群")
        assert self.mode(router, self.other_msg("i1", "随便聊聊")) == "ambient"
        assert router._full_feed_for(self.other_msg("i2", "x")) is not None
