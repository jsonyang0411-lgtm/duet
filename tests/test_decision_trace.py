"""Tests for the observe-only decision trace (src/decision_trace.py)."""

import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import tempfile

from src.config import BotConfig
from src.decision_trace import (
    DecisionTracer,
    JsonlSink,
    NullTrace,
    NullTracer,
    OUTCOME_ERROR,
    OUTCOME_IGNORED,
    OUTCOME_REPLY,
    OUTCOME_SILENT,
)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _msg(**overrides) -> dict:
    msg = {
        "message_id": "m1",
        "chat_id": "g1@chatroom",
        "group_name": "测试群",
        "sender_id": "wxid_a",
        "sender_name": "阿明",
        "content": "cc 看一下今天的发货",
        "msg_type": 1,
        "timestamp": 1_760_000_000,
        "is_at_mentioned": True,
        "is_group": True,
    }
    msg.update(overrides)
    return msg


class JsonlSinkTests(unittest.TestCase):
    def test_writes_one_json_object_per_line_with_unicode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            sink = JsonlSink(Path(tmp) / "sub" / "t.jsonl")
            sink.write({"a": "中文"})
            sink.write({"b": 2})
            raw = (Path(tmp) / "sub" / "t.jsonl").read_text(encoding="utf-8")
            self.assertIn("中文", raw)           # not \u-escaped
            self.assertEqual(len(raw.splitlines()), 2)

    def test_rotates_when_size_limit_is_reached(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "t.jsonl"
            sink = JsonlSink(path, max_bytes=1024, backups=2)
            for i in range(60):
                sink.write({"i": i, "pad": "x" * 100})
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertIn("t.jsonl", names)
            self.assertIn("t.jsonl.1", names)
            self.assertNotIn("t.jsonl.3", names)       # only 2 backups kept
            self.assertLessEqual(path.stat().st_size, 1024)

    def test_never_raises_on_unwritable_path(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            sink = JsonlSink(blocker / "child" / "t.jsonl")   # parent is a file
            sink.write({"a": 1})                                # must not raise


class DecisionTracerTests(unittest.TestCase):
    def make(self, tmp, **kwargs):
        decision = JsonlSink(Path(tmp) / "decision.jsonl")
        tracer = DecisionTracer(decision, clock=lambda: 1_760_000_005.0, **kwargs)
        return tracer, Path(tmp) / "decision.jsonl", None

    def test_reply_record_has_header_steps_and_outcome(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, decision_path, _ = self.make(tmp)
            trace = tracer.begin(_msg())
            trace.step("stored")
            trace.reason("prm_copilot")
            trace.reason("claude_relay", job="j1")
            trace.finish_reply("收到，已转给 Mac")

            (rec,) = _read(decision_path)
            self.assertEqual(rec["kind"], "decision")
            self.assertEqual(rec["outcome"], OUTCOME_REPLY)
            self.assertEqual(rec["reason"], "claude_relay")      # last reason wins
            self.assertEqual(rec["reply_chars"], 10)
            self.assertEqual(rec["chat_id"], "g1@chatroom")
            self.assertEqual(rec["message_id"], "m1")
            self.assertEqual(rec["age_sec"], 5)
            self.assertTrue(rec["flags"]["at"])
            self.assertEqual(
                [s["code"] for s in rec["steps"]],
                ["stored", "prm_copilot", "claude_relay"],
            )
            self.assertEqual(rec["steps"][2]["job"], "j1")

    def test_silent_vs_ignored_outcome(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, decision_path, _ = self.make(tmp)
            t = tracer.begin(_msg(message_id="m2"))
            t.reason("prm_copilot")
            t.finish_reply(None)
            t = tracer.begin(_msg(message_id="m3"))
            t.ignore("self_message")
            t.finish_reply(None)

            first, second = _read(decision_path)
            self.assertEqual(first["outcome"], OUTCOME_SILENT)
            self.assertEqual(second["outcome"], OUTCOME_IGNORED)
            self.assertEqual(second["reason"], "self_message")

    def test_error_outcome_records_exception_type(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, decision_path, _ = self.make(tmp)
            trace = tracer.begin(_msg())
            trace.finish_error(ValueError("boom"))
            (rec,) = _read(decision_path)
            self.assertEqual(rec["outcome"], OUTCOME_ERROR)
            self.assertEqual(rec["reason"], "exception:ValueError")
            self.assertIn("boom", rec["extra"]["error"])

    def test_finish_is_idempotent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, decision_path, _ = self.make(tmp)
            trace = tracer.begin(_msg())
            trace.finish_reply("hi")
            trace.finish_reply("hi")
            self.assertEqual(len(_read(decision_path)), 1)

    def test_content_is_truncated_or_omitted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, decision_path, _ = self.make(tmp, content_chars=5)
            tracer.begin(_msg(content="一二三四五六七八")).finish_reply(None)
            (rec,) = _read(decision_path)
            self.assertEqual(rec["content"], "一二三四五…")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, decision_path, _ = self.make(tmp, content_chars=0)
            tracer.begin(_msg()).finish_reply(None)
            (rec,) = _read(decision_path)
            self.assertNotIn("content", rec)

    def test_a_creator_message_is_redacted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, decision_path, _ = self.make(tmp)
            trace = tracer.begin(_msg(content="我的电话 13800000000"))
            trace.redact("[已登记达人的私聊，内容不记录]")
            trace.finish_reply(None)
            (rec,) = _read(decision_path)
            self.assertEqual(rec["content"], "[已登记达人的私聊，内容不记录]")
            self.assertNotIn("13800000000", decision_path.read_text(encoding="utf-8"))

    def test_current_trace_is_per_thread(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tracer, _, _ = self.make(tmp)
            main_trace = tracer.begin(_msg(message_id="main"))
            seen = {}

            def worker():
                seen["before"] = tracer.current().message_id
                t = tracer.begin(_msg(message_id="worker"))
                seen["inside"] = tracer.current().message_id
                t.finish_reply(None)
                seen["after"] = tracer.current().message_id

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
            self.assertEqual(seen, {"before": "", "inside": "worker", "after": ""})
            self.assertEqual(tracer.current().message_id, "main")
            main_trace.finish_reply(None)
            self.assertEqual(tracer.current().message_id, "")


class FromConfigTests(unittest.TestCase):
    def test_default_botconfig_enables_the_trace(self):
        tracer = DecisionTracer.from_config(BotConfig())
        self.assertIsInstance(tracer, DecisionTracer)
        self.assertTrue(tracer.enabled)

    def test_disabled_config_gives_noop_tracer(self):
        cfg = BotConfig(decision_trace_enabled=False)
        tracer = DecisionTracer.from_config(cfg)
        self.assertIsInstance(tracer, NullTracer)
        trace = tracer.begin(_msg())
        self.assertIsInstance(trace, NullTrace)
        trace.finish_reply("x")            # no-ops must not raise

    def test_mock_or_bare_configs_never_enable_tracing(self):
        # Tests build routers with MagicMock / SimpleNamespace configs; their
        # truthy attributes must not switch file logging on.
        self.assertIsInstance(DecisionTracer.from_config(MagicMock()), NullTracer)
        self.assertIsInstance(
            DecisionTracer.from_config(SimpleNamespace()), NullTracer,
        )

    def test_paths_and_flags_are_honoured(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = BotConfig(
                decision_trace_path=str(Path(tmp) / "d.jsonl"),
                decision_trace_content_chars=3,
            )
            tracer = DecisionTracer.from_config(cfg)
            tracer.begin(_msg(content="abcdef")).finish_reply(None)
            (rec,) = _read(Path(tmp) / "d.jsonl")
            self.assertEqual(rec["content"], "abc…")


if __name__ == "__main__":
    unittest.main()
