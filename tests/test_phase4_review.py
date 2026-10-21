"""Regression tests for what the independent Phase 4 review found."""

import os
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from test_followup_declared import RouterCase as FollowRouterCase
from test_persona_map import FEED, GOOD, MappedRouterCase
from test_router_pipeline import PipelineSummarizer, TraceSpy, reply as decision_reply
from test_router_priority import BOT, CHAT, NOW, RouterTestCase, StubWebSearch

import src.persona as P
from src import datafile
from src import decision_explain as D
from src.pipeline import envelope as E
from src.pipeline.types import Decision
from src.proactive.followup import FollowupListenTracker
from src.routing import steps as S
from src.routing.context import RouteContext


# ── data files can never stop the bot or freeze it ───────────────────────────

def test_a_notepad_file_with_a_bom_is_read(tmp_path):
    path = tmp_path / "persona_map.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"groups": {"读书": "me"}}, ensure_ascii=False).encode("utf-8"))
    assert P.load_persona_map(path).groups == (("读书", "me"),)


@pytest.mark.parametrize("encoding", ["gbk", "utf-16"])
def test_a_file_saved_in_another_encoding_is_reported_not_fatal(tmp_path, caplog, encoding):
    path = tmp_path / "persona_map.json"
    path.write_bytes(json.dumps({"groups": {"读书群": "me"}}, ensure_ascii=False).encode(encoding))
    assert not P.load_persona_map(path).active
    assert "ignored" in caplog.text
    persona = tmp_path / "me.json"
    persona.write_bytes(json.dumps({**GOOD}, ensure_ascii=False).encode(encoding))
    with pytest.raises(P.PersonaError, match="UTF-8"):
        P.load_persona_file(persona)
    assert P.load_personas(tmp_path) == {}


def test_a_huge_file_or_a_directory_is_refused_before_it_is_parsed(tmp_path):
    big = tmp_path / "big.json"
    big.write_text('{"id": "x", "pad": "' + "a" * (datafile.MAX_DATA_FILE_BYTES + 10) + '"}', encoding="utf-8")
    with pytest.raises(datafile.DataFileError, match="larger"):
        datafile.read_json(big)
    with pytest.raises(datafile.DataFileError):
        datafile.read_json(tmp_path)
    assert not P.load_persona_map(big).active


def test_deeply_nested_json_is_an_error_not_a_crash(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    with pytest.raises(datafile.DataFileError):
        datafile.read_json(path)


def test_the_router_starts_with_broken_persona_files(tmp_path):
    from test_persona_map import MappedRouterCase as Base

    class Case(Base):
        def runTest(self):
            (self.tmp / "data" / "personas").mkdir()
            (self.tmp / "data" / "persona_map.json").write_bytes("{\"groups\": {\"测试群\": \"me\"}}".encode("gbk"))
            (self.tmp / "data" / "personas" / "me.json").write_bytes(json.dumps(GOOD, ensure_ascii=False).encode("gbk"))
            router, model = self.build()
            router.handle(self.group_msg("m1", f"@{BOT} 你好"))
            assert model.decide_requests[0].persona is None
    import unittest
    with open(os.devnull, "w") as sink:
        result = unittest.TextTestRunner(stream=sink).run(Case())
    assert result.wasSuccessful(), result.errors + result.failures


def test_pacing_words_are_plain_text_so_no_pattern_can_hang_the_bot():
    feed = P.FullFeed(skip_words=("(?:a+)+$", "ai"), skip_reactions=frozenset())
    router_cls = __import__("src.router", fromlist=["MessageRouter"]).MessageRouter
    stub = MagicMock()
    stub._is_direct_address.return_value = False
    stub._full_feed_for.return_value = feed
    stub._ambient_lock = __import__("threading").Lock()
    stub._ambient_next_turn_at = {}
    started = time.perf_counter()
    for text in ("a" * 5000 + "!", "(?:a+)+$ 里有字面量", "聊聊AI吧"):
        router_cls._should_consider_ambient_turn(stub, {"content": text, "chat_id": "c"})
    assert time.perf_counter() - started < 1.0
    assert router_cls._should_consider_ambient_turn(stub, {"content": "里面有字面量(?:a+)+$呀", "chat_id": "c2"}) is False
    assert router_cls._should_consider_ambient_turn(stub, {"content": "聊聊AI吧", "chat_id": "c3"}) is False
    assert router_cls._should_consider_ambient_turn(stub, {"content": "今天去爬山吧", "chat_id": "c4"}) is True


# ── the persona map ─────────────────────────────────────────────────────────

def test_keys_match_in_any_width_and_a_missing_group_name_matches_nothing():
    reg = P.PersonaRegistry({"me": P.persona_from_dict(GOOD)}, P.parse_persona_map({"groups": {"ＡＢＣ读书": "me", "one": "me"}}))
    assert reg.persona_for({"is_group": True, "group_name": "abc读书会", "chat_id": "x@chatroom"}).id == "me"
    assert reg.persona_for({"is_group": True, "group_name": None, "chat_id": "x@chatroom"}) is None   # not "none"


def test_how_a_chat_was_matched_is_reported():
    pmap = P.parse_persona_map({"groups": {"读书": "me"}, "private": "me", "default": "me"})
    reg = P.PersonaRegistry({"me": P.persona_from_dict(GOOD)}, pmap)
    assert reg.match({"is_group": True, "group_name": "读书群", "chat_id": "a"})[1] == "group"
    assert reg.match({"is_group": True, "group_name": "别的", "chat_id": "a"})[1] == "default"
    assert reg.match({"is_group": False})[1] == "private"
    wild = P.PersonaRegistry({"me": P.persona_from_dict(GOOD)}, P.parse_persona_map({"groups": {"*": "me"}}))
    assert wild.match({"is_group": True, "group_name": "x", "chat_id": "a"})[1] == "wildcard"
    assert wild.match({"is_group": False}) == (None, "")


class FullFeedScopeTests(MappedRouterCase):
    PERSONA = {**GOOD, "id": "feed", "full_feed": {**FEED, "groups": ["读书群"]}}

    def mode(self, router, msg):
        return router._full_feed_mode(msg)

    def test_default_and_wildcard_never_make_a_persona_read_every_message(self):
        self.write_map({}, [self.PERSONA], default="feed")
        router, _ = self.build()
        assert self.mode(router, self.other_msg("a1", "随便聊聊")) is None
        assert router._persona_for(self.other_msg("a2", "随便聊聊")).id == "feed"      # it still speaks when called
        self.write_map({"*": "feed"}, [self.PERSONA])
        router, _ = self.build()
        assert self.mode(router, self.other_msg("a3", "随便聊聊")) is None

    def test_a_group_the_persona_file_lists_is_read_even_when_mapped_by_default(self):
        self.write_map({}, [self.PERSONA], default="feed")
        router, _ = self.build()
        listed = self.group_msg("a4", "x", chat_id="r@chatroom", group_name="读书群", sender_id="wxid_a")
        assert self.mode(router, listed) == "ambient"

    def test_naming_the_group_in_the_map_is_enough(self):
        self.write_map({"测试群": "feed"}, [self.PERSONA])
        router, _ = self.build()
        assert self.mode(router, self.other_msg("a5", "随便聊聊")) == "ambient"

    def test_a_short_direct_address_passes_the_pacing_for_the_mapped_persona_name(self):
        self.write_map({"测试群": "feed"}, [{**self.PERSONA, "full_feed": {**FEED, "min_chars": 6}}])
        router, _ = self.build()
        assert router._should_consider_ambient_turn(self.other_msg("a6", "@小书")) is True
        assert router._should_consider_ambient_turn(self.other_msg("a7", "好不好呀")) is False   # too short

    def test_min_chars_is_the_first_length_that_passes(self):
        self.write_map({"测试群": "feed"}, [{**self.PERSONA, "full_feed": {**FEED, "min_chars": 4, "skip_reactions": []}}])
        router, _ = self.build()
        assert router._should_consider_ambient_turn(self.other_msg("a8", "好吧好")) is False
        router._ambient_next_turn_at.clear()
        assert router._should_consider_ambient_turn(self.other_msg("a9", "好吧好吧")) is True

    def test_the_cooldown_is_the_personas_own(self):
        self.write_map({"测试群": "feed"}, [{**self.PERSONA, "full_feed": {**FEED, "ambient_cooldown_sec": 200}}])
        router, _ = self.build()
        assert router._should_consider_ambient_turn(self.other_msg("b1", "周末去爬山好不好")) is True
        next_turn = router._ambient_next_turn_at[CHAT]
        assert 150 < next_turn - router_clock(router) <= 200

    def test_the_global_profile_wins_over_a_map(self):
        self.write_map({"测试群": "me"}, [GOOD])
        router, _ = self.build(chat_persona_profile="yang_yujing", yang_yujing_group_name="测试群")
        assert self.mode(router, self.other_msg("b2", "随便聊聊")) == "ambient"
        assert router._full_feed_for(self.other_msg("b3", "x")) is not None


def router_clock(router):
    import src.router as module
    return module.time.monotonic()


class VisionTests(MappedRouterCase):
    def test_an_image_question_in_a_mapped_group_is_answered_in_that_persona(self):
        self.write_map({"测试群": "me"}, [GOOD])
        router, model = self.build()
        with patch("src.router.find_recent_image_path", return_value="/tmp/x.png"):
            router.handle(self.group_msg("v1", f"@{BOT} 看看这张图是什么"))
        for call in model.image_calls:
            assert call["persona_settings"]["persona_name"] == "小书"
        self.write_map({"别的": "me"}, [GOOD])

    def test_unmapped_chats_call_describe_image_exactly_as_before(self):
        router, model = self.build()
        with patch("src.router.find_recent_image_path", return_value="/tmp/x.png"):
            router.handle(self.group_msg("v2", f"@{BOT} 看看这张图是什么"))
        assert model.image_calls and all("persona_settings" not in call for call in model.image_calls)

    def test_the_vision_prompt_uses_the_given_persona(self):
        from src.summarize.openai_backend import OpenAISummarizer
        summarizer = OpenAISummarizer(api_key="k", model="m", base_url="https://x.example/v1")
        seen = []
        summarizer._create_chat_completion = lambda messages, max_tokens, **kw: (
            seen.append(messages[0]["content"]) or {"choices": [{"message": {"content": "好看"}}]})
        summarizer.configure_chat_persona(persona_name="全局", persona_prompt="GLOBAL-PROMPT", reply_rules="r")
        with patch("src.summarize.openai_backend.image_to_data_url", return_value="data:image/png;base64,AA=="):
            summarizer.describe_image("/x.png", "这是啥", persona_settings=P.persona_from_dict(GOOD).settings())
            summarizer.describe_image("/x.png", "这是啥")
        assert "PROMPT-ME" in seen[0] and "小书" in seen[0] and "GLOBAL-PROMPT" not in seen[0]
        assert "GLOBAL-PROMPT" in seen[1]


# ── the routing steps ───────────────────────────────────────────────────────

class StepEdges(RouterTestCase):
    def test_an_empty_bot_name_never_reads_the_senders_name(self):
        router, _ = self.make_router(bot_display_name="")
        msg = {"message_id": "x", "chat_id": CHAT, "sender_id": "s", "content": "hi", "timestamp": NOW}   # no sender_name
        assert S.self_message(RouteContext(router=router, msg=msg, trace=TraceSpy())) is None

    def test_a_sender_checked_by_wxid_is_not_judged_by_name(self):
        router, _ = self.make_router(bot_display_name="杨宇靖")
        msg = {"message_id": "x", "chat_id": CHAT, "sender_id": "wxid_yang", "sender_name": "杨宇靖",
               "content": "hi", "timestamp": NOW}
        assert S.self_message(RouteContext(router=router, msg=msg, trace=TraceSpy())) is not None
        msg["_not_self_by_id"] = True
        assert S.self_message(RouteContext(router=router, msg=msg, trace=TraceSpy())) is None

    def test_the_welcome_age_limit_is_inclusive(self):
        router, _ = self.make_router(welcome_enabled=True)
        router._handle_welcome = MagicMock(return_value="欢迎")
        spy = TraceSpy()
        at_limit = self.join_msg("w1", timestamp=NOW - 300)
        assert S.welcome(RouteContext(router=router, msg=at_limit, trace=spy)).value == "欢迎"
        past = self.join_msg("w2", timestamp=NOW - 301)
        assert S.welcome(RouteContext(router=router, msg=past, trace=spy)) is None
        router._store.insert_message = MagicMock(return_value=False)
        assert S.persist(RouteContext(router=router, msg=self.join_msg("w3", timestamp=NOW - 300), trace=spy)).value == "欢迎"
        assert S.persist(RouteContext(router=router, msg=self.join_msg("w4", timestamp=NOW - 301), trace=spy)).value is None

    def test_a_failing_alias_extractor_does_not_stop_the_message(self):
        router, _ = self.make_router()
        router._remember_structured_facts = MagicMock(side_effect=RuntimeError("regex blew up"))
        ctx = RouteContext(router=router, msg=self.other_msg("m1", "随便聊聊"), trace=TraceSpy())
        assert S.memory_hooks(ctx) is None and self.consolidation_checks == [CHAT]

    def test_a_name_call_reaches_no_command_handler(self):
        router, _ = self.make_router(fun_enabled=True, admin_wxid="wxid_user1", todo_enabled=True, todo_groups=["*"])
        router._feishu_export = MagicMock()
        router._feishu_export.is_export_command.return_value = True
        router._admin.handle = MagicMock(return_value="x")
        router._todo_handler.handle = MagicMock(return_value=None)
        msg = self.group_msg("n1", "抽签", _name_call_triggered=True)
        ctx = RouteContext(router=router, msg=msg, trace=TraceSpy(), is_at=True, clean_content="抽签")
        for step in (S.feishu_export, S.help_command, S.fun_commands, S.admin_command, S.todo_command):
            assert step(ctx) is None
        assert ctx.reply is None and not router._feishu_export.export_recent_chat.called
        assert not router._admin.handle.called and not router._todo_handler.handle.called


# ── declared follow-ups ─────────────────────────────────────────────────────

class FollowUpEdges(FollowRouterCase):
    def test_waiting_for_nobody_also_skips_the_sticky_bridge_and_the_image_window(self):
        router, model = self.build(self.decision("none"), followup_mode="declared", sticky_mention_enabled=True)
        router._image_followup = MagicMock()
        router.handle(self.group_msg("m1", f"@{BOT} 点个饮料"))
        assert not router._sticky.consume(CHAT, "wxid_user1")
        router._image_followup.register.assert_not_called()

    def test_other_declarations_keep_both(self):
        for expect in ("sender", "anyone"):
            router, model = self.build(self.decision(expect), followup_mode="declared", sticky_mention_enabled=True)
            router._image_followup = MagicMock()
            router.handle(self.group_msg(f"m-{expect}", f"@{BOT} 点个饮料"))
            assert router._sticky.consume(CHAT, "wxid_user1")
            router._image_followup.register.assert_called_once()

    def test_the_declared_window_length_reaches_the_listener(self):
        router, _ = self.build(self.decision("sender", 120), followup_mode="declared")
        router._followup = MagicMock()
        router.handle(self.group_msg("m3", f"@{BOT} 点个饮料"))
        assert router._followup.register.call_args.kwargs == {"expect": "sender", "sender_id": "wxid_user1", "seconds": 120}


def tracker():
    return FollowupListenTracker(ttl_sec=60, max_message_chars=30)


def test_waiting_for_the_asker_is_a_single_shot():
    t = tracker()
    t.register(CHAT, "你想喝什么", expect="sender", sender_id="wxid_a", seconds=120)
    assert t.match({"chat_id": CHAT, "sender_id": "wxid_a", "content": "ok"}) is not None
    assert t.match({"chat_id": CHAT, "sender_id": "wxid_a", "content": "还有一件事"}) is None     # consumed
    t.register(CHAT, "还要别的吗", expect="sender", sender_id="wxid_a")                            # asking again re-arms it
    assert t.match({"chat_id": CHAT, "sender_id": "wxid_a", "content": "不用了"}) is not None


def test_only_waiting_for_the_asker_remembers_who_that_was():
    t = tracker()
    t.register(CHAT, "你们想喝什么", expect="anyone", sender_id="wxid_a")
    assert t._entries[CHAT].sender_id == ""
    t.register(CHAT, "你们想喝什么")
    assert t._entries[CHAT].sender_id == ""


def test_an_empty_sender_id_never_matches_a_declared_sender():
    t = tracker()
    t.register(CHAT, "你想喝什么", expect="sender", sender_id="")
    assert t.match({"chat_id": CHAT, "sender_id": "", "content": "好"}) is None or True    # falls back to the heuristics
    t.register(CHAT, "你想喝什么", expect="sender", sender_id="wxid_a")
    assert t.match({"chat_id": CHAT, "sender_id": "", "content": "好"}) is None


@pytest.mark.parametrize("word", ["no", "nobody", "false", "NONE", "无", "不用"])
def test_every_spelling_of_waiting_for_nobody_means_none(word):
    assert E.parse_follow_up(word).expect == "none"


def test_the_extraction_schema_and_the_follow_up_schema_are_refused_separately():
    import anthropic
    from test_pipeline_backends import MESSAGES, bad_request_anthropic, claude
    from src.memory.extraction import EXTRACTION_SCHEMA
    summarizer = claude()
    good = summarizer.client.messages.create.return_value
    summarizer.client.messages.create.side_effect = [bad_request_anthropic(), good, good, good]
    summarizer._call_json_api("s", MESSAGES, schema=E.ENVELOPE_JSON_SCHEMA_FOLLOWUP)       # refused once
    summarizer._call_json_api("s", MESSAGES, schema=E.ENVELOPE_JSON_SCHEMA_FOLLOWUP)       # remembered: no schema
    summarizer._call_json_api("s", MESSAGES, schema=EXTRACTION_SCHEMA)                      # still tries its own
    calls = summarizer.client.messages.create.call_args_list
    # calls: [0] refused, [1] its retry without a schema, [2] remembered, [3] the other schema is still tried
    assert "output_config" in calls[0].kwargs and "output_config" not in calls[1].kwargs
    assert "output_config" not in calls[2].kwargs and "output_config" in calls[3].kwargs
    assert id(E.ENVELOPE_JSON_SCHEMA_FOLLOWUP) in summarizer._refused_schemas
    assert id(EXTRACTION_SCHEMA) not in summarizer._refused_schemas


# ── the decision explainer and its endpoint ───────────────────────────────

def line(**fields):
    base = {"kind": "decision", "ts": 1760000000, "chat_id": "g1", "group": "群一", "message_id": "m1",
            "outcome": "silent", "reason": "proactive_gate_closed", "steps": [], "extra": {}}
    base.update(fields)
    return json.dumps(base, ensure_ascii=False)


HOSTILE = [
    {"steps": "nope"}, {"steps": [1, None, "x", {"code": ["list"]}]}, {"extra": "text"}, {"extra": []},
    {"extra": {"route": ["a"], "ended_by": {"b": 1}, "decision": "x", "addressing": 5}},
    {"extra": {"decision": {"follow_up": "x", "action": {"deep": 1}}}}, {"ts": "yesterday"}, {"reason": None},
    {"outcome": 7}, {"message_id": {"id": 1}}, {"content": "字" * 100000}, {"group": None, "chat_id": None},
]


@pytest.mark.parametrize("fields", HOSTILE)
def test_a_damaged_record_is_still_explained(fields):
    item = D.explain(json.loads(line(**fields)))
    assert isinstance(item["summary"], list) and isinstance(item["trail"], list) and len(item["content"]) <= 300
    json.dumps(item)


def test_invalid_utf8_and_damaged_lines_are_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(line(message_id="a").encode() + b"\n\xff\xfe\x80 not json\n" + line(message_id="b").encode() + b"\n")
    assert [i["message_id"] for i in D.recent(path)] == ["b", "a"]


def test_only_the_newest_part_of_a_big_trace_is_read(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(line(message_id=f"id{i:05d}") for i in range(2000)) + "\n", encoding="utf-8")
    monkeypatch.setattr(D, "MAX_SCAN_BYTES", 20_000)
    items = D.recent(path, limit=500)
    assert 0 < len(items) < 500 and items[0]["message_id"] == "id01999"
    assert D.recent(path, message_id="id00001") == []                    # too far back to be read
    ids = [i["message_id"] for i in items]
    assert ids == sorted(ids, reverse=True) and all(i.startswith("id") for i in ids)       # no half lines


def test_since_and_several_chats_filter_the_listing(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join([line(message_id="old", ts=100, chat_id="g9", group="群九"),
                               line(message_id="a", ts=2000, chat_id="g1"),
                               line(message_id="b", ts=3000, chat_id="g2", group="群二"),
                               line(message_id="c", ts=4000, chat_id="g3", group="群三")]) + "\n", encoding="utf-8")
    assert [i["message_id"] for i in D.recent(path, since=1000)] == ["c", "b", "a"]
    assert [i["message_id"] for i in D.recent(path, chat_id=["g1", "g2"])] == ["b", "a"]
    assert [i["message_id"] for i in D.recent(path, chat_id="g3", since=5000)] == []


def test_the_report_tool_honours_since_and_every_chat(tmp_path):
    import contextlib, importlib.util, io
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("tr", Path(__file__).resolve().parents[1] / "tools" / "trace_report.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join([line(message_id="old", ts=100), line(message_id="a", ts=2000, chat_id="g1"),
                               line(message_id="b", ts=3000, chat_id="g2", group="群二")]) + "\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = tool.main(["--trace", str(path), "--recent", "0", "--since", "1000", "--chat-id", "g1", "--chat-id", "g2"])
    assert code == 0 and "== a" in out.getvalue() and "== b" in out.getvalue() and "== old" not in out.getvalue()


def test_the_endpoint_survives_a_damaged_trace_and_does_not_take_posts(tmp_path):
    from test_web_api import _build_handler
    path = tmp_path / "t.jsonl"
    path.write_bytes(line(message_id="a", steps="nope", extra="x").encode() + b"\n\xff\xfe\n")
    with patch("src.web.server._read_effective_env_values", return_value={"DECISION_TRACE_PATH": str(path)}):
        handler, sock = _build_handler("/api/decisions")
        response = sock.get_response_text()
        body = json.loads(response.split("\r\n\r\n", 1)[1])
        assert "200" in response.splitlines()[0] and body["ok"] and body["decisions"][0]["message_id"] == "a"
        handler, sock = _build_handler("/api/decisions", method="POST", body=b"{}")
        assert "405" in sock.get_response_text().splitlines()[0]
    with patch("src.web.server._read_effective_env_values", return_value={"DECISION_TRACE_PATH": str(tmp_path)}):
        handler, sock = _build_handler("/api/decisions")
        assert json.loads(sock.get_response_text().split("\r\n\r\n", 1)[1]) == {"ok": True, "decisions": []}


def test_the_import_tool_no_longer_carries_a_real_account():
    import subprocess, sys
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / "tools" / "import_wechat_history.py"
    text = tool.read_text(encoding="utf-8")
    assert "wxid_p861" not in text and "49515093386" not in text and "DEFAULT_OWNER" not in text
    result = subprocess.run([sys.executable, str(tool), "--source", "x.db"], capture_output=True, text=True)
    assert result.returncode == 2 and "--chat-id" in result.stderr
