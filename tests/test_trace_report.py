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


def _decision(reason, outcome, ended_by="silence", **extra):
    rec = {
        "kind": "decision", "ts": 1_760_000_000, "chat_id": "g1", "group": "群一",
        "message_id": extra.pop("message_id", reason), "reason": reason,
        "outcome": outcome, "elapsed_ms": extra.pop("elapsed_ms", 5),
        "extra": {"ended_by": ended_by},
    }
    rec.update(extra)
    return rec


class TraceReportTests(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_summarize_counts_outcomes_reasons_steps_and_reply_rate(self):
        decisions = [
            _decision("claude_relay", "reply", "claude_relay"),
            _decision("claude_relay", "reply", "claude_relay"),
            _decision("prm_copilot", "silent", "prm"),
            _decision("silent_end", "ignored"),
            _decision("self_message", "ignored", "self_message"),
        ]
        out = self.tool.summarize(decisions)["decisions"]
        self.assertEqual(out["total"], 5)
        self.assertEqual(out["handled"], 3)             # ignored is excluded
        self.assertAlmostEqual(out["reply_rate_of_handled"], 0.667, places=3)
        self.assertEqual(out["reasons"]["claude_relay"], {"reply": 2})
        self.assertEqual(list(out["reasons"])[0], "claude_relay")   # most frequent first
        self.assertEqual(out["ended_by"], {"claude_relay": 2, "silence": 1, "prm": 1, "self_message": 1})
        text = self.tool.format_text(self.tool.summarize(decisions))
        self.assertIn("ended by step: claude_relay=2", text)
        self.assertNotIn("Model calls", text)

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
                + json.dumps(_decision("new", "reply", ts=2_000_000_000)) + "\n"
                + json.dumps({"kind": "llm_call", "ts": 2_000_000_000}) + "\n",   # an old version's line
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = self.tool.main(["--trace", str(trace), "--since", "1000", "--json"])
            self.assertEqual(code, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["decisions"]["total"], 1)
            self.assertIn("new", data["decisions"]["reasons"])


if __name__ == "__main__":
    unittest.main()
