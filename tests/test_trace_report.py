"""Tests for tools/trace_report.py (decision-trace summariser)."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load_tool():
    path = Path(__file__).resolve().parent.parent / "tools" / "trace_report.py"
    spec = importlib.util.spec_from_file_location("trace_report_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decision(reason, outcome, **extra):
    rec = {
        "kind": "decision", "ts": 1_760_000_000, "chat_id": "g1", "group": "群一",
        "message_id": extra.pop("message_id", reason), "reason": reason,
        "outcome": outcome, "elapsed_ms": extra.pop("elapsed_ms", 5),
    }
    rec.update(extra)
    return rec


def _structured(reason, outcome, level, action, status="ok", calls=1, strategy="single", **extra):
    return _decision(
        reason, outcome,
        extra={
            "addressing": {"level": level, "signals": []},
            "decision": {"action": action, "addressee": "me", "status": status,
                         "strategy": strategy, "calls": calls},
        },
        **extra,
    )


class TraceReportTests(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_structured_pipeline_numbers_appear_only_when_it_ran(self):
        self.assertNotIn("pipeline", self.tool.summarize([_decision("chat_reply", "reply")], []))

        decisions = [
            _structured("ai_reply", "reply", "explicit", "reply", message_id="a"),
            _structured("ai_intentional_silence", "silent", "strong", "silent", message_id="b"),
            _structured("ai_blank_direct_fallback", "reply", "explicit", "silent",
                        status="parse_failed", calls=2, message_id="c"),
            _structured("ai_reply", "reply", "weak", "reply", status="repaired", calls=2, message_id="d"),
            _decision("self_message", "ignored"),
        ]
        calls = [
            {"call": "chat_v2", "attempt": "ask", "usage": {"input_tokens": 100, "output_tokens": 10},
             "native_json": True},
            {"call": "chat_v2", "attempt": "repair", "usage": {"input_tokens": 120, "output_tokens": 12},
             "native_json": False},
        ]
        out = self.tool.summarize(decisions, calls)["pipeline"]
        self.assertEqual(out["decisions"], 4)
        self.assertEqual(out["levels"], {"explicit": 2, "strong": 1, "weak": 1})
        self.assertEqual(out["actions"], {"reply": 2, "silent": 2})
        self.assertEqual(out["status"], {"ok": 2, "parse_failed": 1, "repaired": 1})
        self.assertEqual(out["unusable_rate"], 0.25)
        self.assertEqual(out["repaired_rate"], 0.25)
        self.assertEqual(out["model_calls_per_decision"], 1.5)
        self.assertEqual(out["attempts"], {"ask": 1, "repair": 1})
        self.assertEqual(out["tokens"], {"input": 220, "output": 22})
        self.assertEqual(out["native_json_share"], 0.5)
        self.assertEqual(out["fallbacks"], 1)
        self.assertEqual(out["outcome_by_level"]["explicit"], {"reply": 2})

        text = self.tool.format_text(self.tool.summarize(decisions, calls))
        self.assertIn("== Structured pipeline ==", text)
        self.assertIn("unusable 25.0%", text)

    def test_summarize_counts_outcomes_reasons_and_reply_rate(self):
        decisions = [
            _decision("chat_reply", "reply"),
            _decision("chat_reply", "reply"),
            _decision("proactive_gate_blocked", "silent"),
            _decision("self_message", "ignored"),
        ]
        out = self.tool.summarize(decisions, [])["decisions"]
        self.assertEqual(out["total"], 4)
        self.assertEqual(out["handled"], 3)             # ignored is excluded
        self.assertAlmostEqual(out["reply_rate_of_handled"], 0.667, places=3)
        self.assertEqual(out["reasons"]["chat_reply"], {"reply": 2})
        self.assertEqual(list(out["reasons"])[0], "chat_reply")   # most frequent first

    def test_llm_call_quality_counters(self):
        calls = [
            {"call": "chat", "latency_ms": 100, "raw_reply": "好的", "cleaned_reply": "好的"},
            {"call": "chat", "latency_ms": 300, "raw_reply": "（沉默）", "cleaned_reply": "",
             "intentional_silence": True},
            {"call": "chat", "latency_ms": 200, "raw_reply": "这是一个很长很长的回复", "cleaned_reply": "这是一个很长",
             "max_chars": 6},
            {"call": "proactive_chat", "latency_ms": 50, "raw_reply": "", "cleaned_reply": ""},
            {"call": "chat", "latency_ms": 10, "error": "Timeout", "raw_reply": "", "cleaned_reply": ""},
        ]
        out = self.tool.summarize([], calls)["llm_calls"]
        self.assertEqual(out["total"], 5)
        self.assertEqual(out["by_kind"], {"chat": 4, "proactive_chat": 1})
        self.assertEqual(out["errors"], 1)
        self.assertEqual(out["intentional_silence"], 1)
        self.assertEqual(out["cleaned_to_empty"], 1)
        self.assertEqual(out["hard_truncated"], 1)
        self.assertEqual(out["blank_raw_reply"], 1)
        self.assertEqual(out["latency_ms"]["max"], 300.0)

    def test_reads_rotated_backups_before_live_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp) / "decision_trace.jsonl"
            base.with_name(base.name + ".1").write_text(
                json.dumps({"n": 1}) + "\n", encoding="utf-8")
            base.write_text(json.dumps({"n": 2}) + "\nnot json\n", encoding="utf-8")
            self.assertEqual([r["n"] for r in self.tool.iter_jsonl(base)], [1, 2])

    def test_cli_filters_by_since_and_prints_json(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            trace = Path(tmp) / "d.jsonl"
            trace.write_text(
                json.dumps(_decision("old", "reply", ts=100)) + "\n"
                + json.dumps(_decision("new", "reply", ts=2_000_000_000)) + "\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = self.tool.main([
                    "--trace", str(trace), "--llm", str(Path(tmp) / "none.jsonl"),
                    "--since", "1000", "--json",
                ])
            self.assertEqual(code, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["decisions"]["total"], 1)
            self.assertIn("new", data["decisions"]["reasons"])


class MemorySectionTests(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_the_memory_section_appears_only_when_memory_v2_ran(self):
        summary = self.tool.summarize([_decision("chat_reply", "reply")], [])
        self.assertNotIn("memory", summary)
        self.assertNotIn("Memory v2", self.tool.format_text(summary))

    def test_injections_and_extraction_calls_are_counted(self):
        decisions = [
            _decision("ai_reply", "reply", message_id="a", steps=[
                {"code": "stored"}, {"code": "memory_v2", "facts": 3, "episodes": 1, "sources": {}},
            ]),
            _decision("ai_reply", "reply", message_id="b", steps=[
                {"code": "memory_v2", "facts": 1, "messages": 4, "sources": {}},
            ]),
            _decision("ai_reply", "reply", message_id="c", steps=[{"code": "stored"}]),
        ]
        calls = [
            {"call": "memory_extract", "usage": {"input_tokens": 4000, "output_tokens": 300}, "latency_ms": 900},
            {"call": "memory_extract", "error": "Timeout()", "latency_ms": 30000},
            {"call": "chat_v2", "usage": {"input_tokens": 100, "output_tokens": 10}},
        ]
        memory = self.tool.summarize(decisions, calls)["memory"]
        self.assertEqual(memory["prompts_with_memory"], 2)
        self.assertEqual(memory["injected"], {"facts": 4, "episodes": 1, "messages": 4})
        self.assertEqual(memory["avg_facts_per_prompt"], 2.0)
        self.assertEqual((memory["extraction_calls"], memory["extraction_errors"]), (2, 1))
        self.assertEqual(memory["extraction_tokens"], {"input": 4000, "output": 300})
        text = self.tool.format_text(self.tool.summarize(decisions, calls))
        self.assertIn("== Memory v2 ==", text)
        self.assertIn("extraction calls: 2  errors: 1", text)

    def test_extraction_calls_alone_are_enough(self):
        memory = self.tool.summarize([], [{"call": "memory_extract", "latency_ms": 5}])["memory"]
        self.assertEqual(memory["prompts_with_memory"], 0)
        self.assertEqual(memory["avg_facts_per_prompt"], 0.0)


if __name__ == "__main__":
    unittest.main()
