"""Plain-words explanations of the decision trace (src/decision_explain.py)."""

import importlib.util
import io
import json
import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

from src import decision_explain as D
from src.routing.steps import ROUTE_STEPS

ROOT = Path(__file__).resolve().parents[1]


def record(message_id="m1", **fields):
    base = {"kind": "decision", "ts": 1760000000, "chat_id": "g1@chatroom", "group": "测试群",
            "message_id": message_id, "sender_name": "小明", "outcome": "ignored", "reason": "silent_end",
            "content": "今天吃什么", "steps": [{"code": "stored"}, {"code": "silent_end"}],
            "extra": {"ended_by": "silence"}}
    base.update(fields)
    return base


def write(path, *records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def test_every_ending_step_and_every_reason_has_a_plain_description():
    assert set(D.STEPS) == {step.name for step in ROUTE_STEPS}
    import re
    codes = set()
    for path in (ROOT / "src").rglob("*.py"):
        codes |= set(re.findall(r'\.(?:reason|ignore)\(\s*"([a-z_0-9]+)"', path.read_text(encoding="utf-8")))
    assert codes - set(D.REASONS) == set(), codes - set(D.REASONS)


def test_a_silent_decision_is_explained_with_reason_and_ending_step():
    item = D.explain(record())
    text = "\n".join(item["summary"])
    assert "没有回复" in text and "静默结束，不回复" in text and "群聊一律在这里" in text
    assert "由哪一步结束：静默结束（silence）" in text
    assert (item["message_id"], item["sender"], item["chat"]) == ("m1", "小明", "测试群")
    assert item["trail"] == ["stored", "silent_end"] and item["reason_text"].startswith("不是中转指令")


def test_a_relay_receipt_and_step_details_are_part_of_the_account():
    rec = record(outcome="reply", reason="claude_relay", is_group=False,
                 steps=[{"code": "stored"}, {"code": "claude_relay", "job": "j1"}],
                 extra={"ended_by": "claude_relay"})
    item = D.explain(rec)
    text = "\n".join(item["summary"])
    assert "回复了" in text and "转给 Mac 上的 Claude Code" in text and "由哪一步结束：转给 Mac（claude_relay）" in text
    assert item["trail"] == ["stored", "claude_relay {'job': 'j1'}"]


def test_unknown_codes_and_missing_fields_are_shown_as_they_are():
    item = D.explain({"outcome": "error", "reason": "brand_new_code"})
    text = "\n".join(item["summary"])
    assert "error" in text and "brand_new_code" in text and item["trail"] == []
    assert "未记录" in "\n".join(D.explain({"outcome": "ignored"})["summary"])


def test_recent_returns_the_newest_first_and_filters(tmp_path):
    path = tmp_path / "trace.jsonl"
    write(path,
          record("a", outcome="reply", reason="admin_command"),
          record("b"),
          record("c", chat_id="other@chatroom", group="别的群"),
          {"kind": "llm", "message_id": "x"})
    assert [i["message_id"] for i in D.recent(path)] == ["c", "b", "a"]
    assert [i["message_id"] for i in D.recent(path, limit=1)] == ["c"]
    assert [i["message_id"] for i in D.recent(path, outcome="reply")] == ["a"]
    assert [i["message_id"] for i in D.recent(path, chat_id="别的群")] == ["c"]
    assert [i["message_id"] for i in D.recent(path, chat_id="g1@chatroom")] == ["b", "a"]
    assert [i["message_id"] for i in D.recent(path, message_id="b")] == ["b"]
    assert D.recent(path, message_id="zzz") == []


def test_rotated_backups_and_bad_lines_are_handled(tmp_path):
    path = tmp_path / "trace.jsonl"
    write(path.with_name("trace.jsonl.1"), record("old"))
    path.write_text("not json\n" + json.dumps(record("new")) + "\n[1]\n", encoding="utf-8")
    assert [i["message_id"] for i in D.recent(path)] == ["new", "old"]
    assert D.recent(tmp_path / "missing.jsonl") == []


def test_the_limit_is_bounded(tmp_path):
    path = tmp_path / "trace.jsonl"
    write(path, *[record(str(i)) for i in range(600)])
    assert len(D.recent(path, limit=100000)) == 500 and len(D.recent(path, limit=0)) == 1


def test_the_text_form_lists_content_summary_and_trail():
    text = D.format_text([D.explain(record())])
    assert "== m1  [测试群] 小明" in text and "「今天吃什么」" in text and "经过：stored" in text
    assert D.format_text([]) == ""


# ── the command line ────────────────────────────────────────────────────────

def tool():
    spec = importlib.util.spec_from_file_location("trace_report_cli", ROOT / "tools" / "trace_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = tool().main(list(argv))
    return code, out.getvalue()


def test_why_explains_one_message(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, record("a"), record("b"))
    code, text = run("--trace", str(path), "--why", "b")
    assert code == 0 and text.count("==") == 1 and "== b" in text and "静默结束" in text
    assert run("--trace", str(path), "--why", "nope")[0] == 1


def test_recent_lists_the_newest_with_a_filter_and_as_json(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, record("a", outcome="reply", reason="claude_relay"), record("b"), record("c"))
    code, text = run("--trace", str(path), "--recent", "5", "--outcome", "ignored")
    assert code == 0 and text.index("== c") < text.index("== b") and "== a" not in text
    code, text = run("--trace", str(path), "--recent", "2", "--json")
    assert [i["message_id"] for i in json.loads(text)] == ["c", "b"]


def test_the_default_report_is_unchanged(tmp_path):
    path = tmp_path / "t.jsonl"
    write(path, record("a"))
    code, text = run("--trace", str(path))
    assert code == 0 and "decisions" in text.lower() and "== m1" not in text


# ── the HTTP endpoint ───────────────────────────────────────────────────────

def test_the_endpoint_returns_the_explained_decisions(tmp_path):
    from test_web_api import _build_handler
    path = tmp_path / "t.jsonl"
    write(path, record("a", outcome="reply", reason="admin_command"), record("b"))
    with patch("src.web.server._read_effective_env_values", return_value={"DECISION_TRACE_PATH": str(path)}):
        handler, sock = _build_handler("/api/decisions?limit=5&outcome=ignored")
        response = sock.get_response_text()
    assert "200" in response.splitlines()[0]
    body = json.loads(response.split("\r\n\r\n", 1)[1])
    assert body["ok"] and [d["message_id"] for d in body["decisions"]] == ["b"]
    assert body["decisions"][0]["reason_text"].startswith("不是中转指令")


def test_the_endpoint_is_get_only_safe_and_tolerates_odd_queries(tmp_path):
    from test_web_api import _build_handler
    with patch("src.web.server._read_effective_env_values", return_value={"DECISION_TRACE_PATH": str(tmp_path / "none")}):
        handler, sock = _build_handler("/api/decisions?limit=abc&outcome=%00&chat_id=")
        body = json.loads(sock.get_response_text().split("\r\n\r\n", 1)[1])
    assert body == {"ok": True, "decisions": []}
    handler, sock = _build_handler("/api/decisions", headers={"Origin": "http://evil.example"})
    assert "403" in sock.get_response_text().splitlines()[0]
