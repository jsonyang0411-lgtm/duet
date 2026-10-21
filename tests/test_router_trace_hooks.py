"""The decision trace is observe-only: wiring it in must not change results.

Covers the router hooks (src/router.py), the model-call recorder in
AbstractSummarizer (src/summarize/base.py) and the end-to-end link between
the two records.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.memory.consolidator import MemoryConsolidator
from src.router import MessageRouter
from src.summarize.base import AbstractSummarizer
from src.summarize.models import SummaryResult
from src.decision_trace import DecisionTracer, JsonlSink, NullTracer
from src.trigger import TriggerDetector


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _ScriptedSummarizer(AbstractSummarizer):
    """Concrete summarizer whose chat API returns scripted text."""

    model = "fake-model"
    retry_exceptions = (ConnectionError,)

    def __init__(self, reply="好呀", error=None):
        self.reply = reply
        self.error = error
        self.calls = 0

    def _call_chat_api(self, system_prompt, messages):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.reply

    def _summarize_direct(self, messages, requester_name):
        return SummaryResult(summary_text="", topics=[], participants=[])

    def _summarize_chunk(self, chunk, chunk_num, total, requester_name):
        return ""

    def _merge_chunk_summaries(self, chunk_summaries, requester_name):
        return SummaryResult(summary_text="", topics=[], participants=[])

    def consolidate_memory(self, existing_memory, new_messages):
        return existing_memory


class _Nicks:
    def resolve_name(self, value):
        return value

    def resolve_wxids(self, text):
        return text


def _msg(content, *, mid, at=False, ts=None, **extra):
    msg = {
        "message_id": mid,
        "chat_id": "g1@chatroom",
        "group_name": "测试群",
        "sender_id": "wxid_a",
        "sender_name": "阿明",
        "content": content,
        "msg_type": 1,
        "timestamp": int(time.time()) if ts is None else ts,
        "is_at_mentioned": at,
        "is_group": True,
    }
    msg.update(extra)
    return msg


class RouterTraceHooksTests(unittest.TestCase):
    def setUp(self):
        # Router.handle starts a background consolidation thread per message;
        # a thread still querying while a test closes its sqlite connection
        # can crash the interpreter.  Consolidation is not under test here.
        patcher = patch.object(
            MemoryConsolidator, "check_and_consolidate_async", return_value=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.decision_path = self.tmp / "decision.jsonl"
        self.llm_path = self.tmp / "llm.jsonl"
        self.conn = initialize_db(str(self.tmp / "m.db"))

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def tracer(self):
        return DecisionTracer(
            JsonlSink(self.decision_path), JsonlSink(self.llm_path),
        )

    def router(self, summarizer, tracer):
        config = BotConfig(
            db_path=str(self.tmp / "m.db"),
            bot_display_name="小助手",
            todo_enabled=False,
            web_search_enabled=False,
        )
        return MessageRouter(
            store=MessageStore(self.conn),
            detector=TriggerDetector(keywords=[], bot_display_name="小助手"),
            summarizer=summarizer,
            admin_handler=MagicMock(),
            nickname_service=_Nicks(),
            config=config,
            web_search_client=None,
            tracer=tracer,
        )

    # ── results are unchanged by tracing ────────────────────────────

    def test_replies_identical_with_and_without_tracing(self):
        script = [
            _msg("@小助手 今天吃什么", mid="1", at=True),
            _msg("哈哈哈哈", mid="2"),
            _msg("@小助手 ", mid="3", at=True),
            _msg("老旧消息", mid="4", at=True, ts=int(time.time()) - 4000),
            _msg("好的", mid="5", sender_id="wxid_b", sender_name="小红"),
        ]

        def run(tracer):
            conn = initialize_db(":memory:")
            try:
                router = MessageRouter(
                    store=MessageStore(conn),
                    detector=TriggerDetector(keywords=[], bot_display_name="小助手"),
                    summarizer=_ScriptedSummarizer("在的"),
                    admin_handler=MagicMock(),
                    nickname_service=_Nicks(),
                    config=BotConfig(
                        db_path=str(self.tmp / "t.db"),
                        bot_display_name="小助手",
                        todo_enabled=False,
                        web_search_enabled=False,
                    ),
                    web_search_client=None,
                    tracer=tracer,
                )
                return [router.handle(dict(m)) for m in script]
            finally:
                conn.close()

        off = run(NullTracer())
        on = run(self.tracer())
        self.assertEqual(on, off)
        self.assertEqual(len(_read(self.decision_path)), len(script))

    def test_replay_harness_report_is_identical_with_tracing_enabled(self):
        from evals import run_offline

        paths = [run_offline.DEFAULT_CASES]
        raw = run_offline.DEFAULT_RAW_OUTPUTS
        off = run_offline.canonical_json(
            run_offline.build_report(seed=7, case_paths=paths, raw_path=raw),
        )

        tracer = self.tracer()
        original = DecisionTracer.from_config
        DecisionTracer.from_config = classmethod(lambda cls, config, **kw: tracer)
        try:
            on = run_offline.canonical_json(
                run_offline.build_report(seed=7, case_paths=paths, raw_path=raw),
            )
        finally:
            DecisionTracer.from_config = original

        self.assertEqual(on, off)
        records = _read(self.decision_path)
        self.assertGreater(len(records), 100)         # one per replayed message
        for rec in records:
            self.assertIn(rec["outcome"], {"reply", "silent", "ignored", "error"})
            self.assertTrue(rec["reason"])
            self.assertTrue(rec["steps"])

    # ── what gets recorded ──────────────────────────────────────────

    def test_at_mention_reply_records_route_reason_and_llm_link(self):
        router = self.router(_ScriptedSummarizer("在的"), self.tracer())
        reply = router.handle(_msg("@小助手 在吗", mid="m1", at=True))
        self.assertEqual(reply, "在的")

        (rec,) = _read(self.decision_path)
        (call,) = _read(self.llm_path)
        self.assertEqual(rec["outcome"], "reply")
        self.assertEqual(rec["reason"], "ai_reply")
        self.assertEqual(rec["extra"]["route"], "at")
        self.assertEqual(
            [s["code"] for s in rec["steps"]][:3],
            ["stored", "chat_request", "ai_reply"][:3],
        )
        self.assertEqual(rec["llm_calls"], [call["call_id"]])
        self.assertEqual(call["message_id"], "m1")
        self.assertEqual(call["call"], "chat")
        self.assertEqual(call["backend"], "_ScriptedSummarizer")
        self.assertEqual(call["model"], "fake-model")
        self.assertEqual(call["raw_reply"], "在的")
        self.assertEqual(call["cleaned_reply"], "在的")
        self.assertIn("小助手", call["system_prompt"])

    def test_blank_model_output_is_traced_as_silence(self):
        router = self.router(_ScriptedSummarizer("（沉默）"), self.tracer())
        reply = router.handle(_msg("@小助手 哈", mid="m2", at=True))
        # Current behaviour: an intentional silence on a direct @ is dropped.
        self.assertIsNone(reply)
        (rec,) = _read(self.decision_path)
        (call,) = _read(self.llm_path)
        self.assertEqual(rec["outcome"], "silent")
        self.assertEqual(rec["reason"], "ai_intentional_silence")
        self.assertEqual(call["raw_reply"], "（沉默）")
        self.assertEqual(call["cleaned_reply"], "")
        self.assertTrue(call["intentional_silence"])

    def test_model_error_is_traced_and_behaviour_preserved(self):
        summarizer = _ScriptedSummarizer(error=ConnectionError("down"))
        router = self.router(summarizer, self.tracer())
        reply = router.handle(_msg("@小助手 在吗", mid="m3", at=True))
        self.assertEqual(reply, "嗯，在的")             # existing canned fallback
        (rec,) = _read(self.decision_path)
        (call,) = _read(self.llm_path)
        self.assertEqual(rec["outcome"], "reply")
        self.assertEqual(rec["reason"], "ai_error")
        self.assertIn("RuntimeError", call["error"])
        self.assertEqual(call["raw_reply"], "")

    def test_filtered_messages_have_explicit_reasons(self):
        router = self.router(_ScriptedSummarizer(), self.tracer())
        router.handle(_msg("我自己说的", mid="s1", sender_name="小助手"))
        router.handle(_msg("重复", mid="d1"))
        router.handle(_msg("重复", mid="d1"))                    # duplicate id
        router.handle(_msg("@小助手 迟到", mid="old", at=True,
                           ts=int(time.time()) - 4000))
        records = {r["message_id"]: r for r in _read(self.decision_path)
                   if r["message_id"] != "d1"}
        self.assertEqual(records["s1"]["reason"], "self_message")
        self.assertEqual(records["s1"]["outcome"], "ignored")
        self.assertEqual(records["old"]["reason"], "stale_mention_ignored")
        reasons = [r["reason"] for r in _read(self.decision_path)
                   if r["message_id"] == "d1"]
        self.assertEqual(reasons[-1], "duplicate_or_store_error")

    def test_proactive_gate_closed_is_recorded_with_gate_reason(self):
        router = self.router(_ScriptedSummarizer(), self.tracer())
        router.handle(_msg("大家早", mid="p1"))
        (rec,) = _read(self.decision_path)
        self.assertEqual(rec["outcome"], "silent")
        self.assertEqual(rec["reason"], "proactive_gate_closed")
        self.assertEqual(rec["steps"][-1]["gate"], "disabled")
        self.assertEqual(rec["extra"]["route"], "ambient")

    def test_exception_is_recorded_and_still_propagates(self):
        router = self.router(_ScriptedSummarizer(), self.tracer())
        router._detector = MagicMock()
        router._detector.is_trigger.side_effect = ValueError("boom")
        with self.assertRaises(ValueError):
            router.handle(_msg("你好", mid="e1"))
        (rec,) = _read(self.decision_path)
        self.assertEqual(rec["outcome"], "error")
        self.assertEqual(rec["reason"], "exception:ValueError")


class SummarizerRecorderTests(unittest.TestCase):
    def test_recorder_is_optional_and_failures_never_propagate(self):
        summarizer = _ScriptedSummarizer("ok")
        self.assertEqual(summarizer.chat("hi", requester_name="a"), "ok")

        def broken(**fields):
            raise RuntimeError("recorder bug")

        summarizer.set_call_recorder(broken)
        self.assertEqual(summarizer.chat("hi", requester_name="a"), "ok")

    def test_router_attaches_recorder_only_when_tracing_is_enabled(self):
        summarizer = _ScriptedSummarizer()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            conn = initialize_db(str(Path(tmp) / "m.db"))
            try:
                kwargs = dict(
                    store=MessageStore(conn),
                    detector=TriggerDetector(keywords=[], bot_display_name="x"),
                    summarizer=summarizer,
                    admin_handler=MagicMock(),
                    nickname_service=_Nicks(),
                    config=BotConfig(
                        db_path=str(Path(tmp) / "m.db"), todo_enabled=False,
                        web_search_enabled=False,
                    ),
                    web_search_client=None,
                )
                MessageRouter(tracer=NullTracer(), **kwargs)
                self.assertIsNone(summarizer._call_recorder)
                tracer = DecisionTracer(None, JsonlSink(Path(tmp) / "l.jsonl"))
                MessageRouter(tracer=tracer, **kwargs)
                self.assertEqual(summarizer._call_recorder, tracer.record_llm_call)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
