"""Relay progress: the worker reads what Claude is doing from its stream, the bot tells WeChat."""

import json
import sqlite3
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from test_claude_relay_worker import ROOT, TOKEN, bot, fake_claude, run_one, settings, worker  # noqa: F401  (fixtures)

from src.config import _validate_config
from src.relay.service import ClaudeRelay, progress_line
from src.relay.store import RelayStore

ME = "wxid_me"


def tool(name, **args):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": args}]}})


def said(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


# ── the worker's side ─────────────────────────────────────────────────────

def test_progress_follows_the_stream(worker):
    p = worker.Progress()
    p.feed(json.dumps({"type": "system", "subtype": "init"}))
    p.feed("not json")
    assert p.report() == {"steps": 0, "doing": "", "plan": ""}
    p.feed(said("我先看一下测试文件"))
    assert p.report()["doing"] == "我先看一下测试文件"
    p.feed(tool("Read", file_path="/Users/x/proj/src/relay/store.py"))
    p.feed(tool("Bash", command="python -m pytest  tests/ -q", description="Run tests"))
    assert p.report() == {"steps": 2, "doing": "运行 python -m pytest tests/ -q", "plan": ""}
    p.feed(tool("TodoWrite", todos=[
        {"content": "改 store", "activeForm": "正在改 store", "status": "completed"},
        {"content": "改 service", "activeForm": "正在改 service", "status": "in_progress"},
        {"content": "写测试", "activeForm": "正在写测试", "status": "pending"},
    ]))
    assert p.report() == {"steps": 3, "doing": "正在改 service", "plan": "1/3"}
    p.feed(tool("Edit", file_path="/a/b/service.py", old_string="x", new_string="y"))
    assert p.report()["doing"] == "改 service.py" and p.report()["plan"] == "1/3"


def test_steps_in_a_few_words(worker):
    d = worker.describe_step
    assert d("Write", {"file_path": "/a/notes.md"}) == "写 notes.md"
    assert d("Grep", {"pattern": "def handle"}) == "搜索 def handle"
    assert d("WebFetch", {"url": "https://example.com"}) == "打开网页 https://example.com"
    assert d("Agent", {"description": "查一下调用方"}) == "派子任务 查一下调用方"
    assert d("mcp__x__y", {}) == "mcp__x__y"
    assert d("Bash", {"command": "echo " + "x" * 100}).endswith("…")
    assert d("Bash", "not a dict") == "运行 "


def test_the_result_is_the_last_result_line_of_the_stream(worker):
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s"}),
        json.dumps({"type": "user", "result": "一个工具的结果，不是最终结果"}),
        said("好"),
        json.dumps({"type": "result", "result": "完成", "session_id": "s", "is_error": False}),
        "",
    ])
    assert worker.parse_output(stream)["result"] == "完成"
    assert worker.parse_output(json.dumps({"result": "旧格式", "session_id": "s"}))["result"] == "旧格式"
    assert worker.parse_output("No conversation found with session ID: gone") is None
    assert worker.parse_output(said("没有结果行")) is None


def test_claude_is_asked_for_a_stream(worker, tmp_path):
    cmd = worker.claude_command({"claude_bin": sys.executable, "permission_mode": "auto"}, None)
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd


STEPPING_CLAUDE = textwrap.dedent("""\
    import json, sys, time
    prompt = sys.stdin.read()
    for name, args in (("Read", {"file_path": "/p/a.py"}), ("Edit", {"file_path": "/p/service.py"})):
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": args}]}}), flush=True)
    time.sleep(1.0)
    print(json.dumps({"type": "result", "result": "改好了", "session_id": "s1", "is_error": False}))
""")


def test_heartbeats_carry_the_progress_to_the_bot(worker, bot, tmp_path):
    script = tmp_path / "stepping-claude"
    script.write_text(f"#!{sys.executable}\n" + STEPPING_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    s = settings(bot, SimpleNamespace(path=str(script)), tmp_path)
    bot.ask("cc 改一下")
    seen = []

    def watch():
        for _ in range(40):
            job = bot.relay.store.get(1)
            if job and job["status"] == "running" and progress_line(job["progress"]):
                seen.append(progress_line(job["progress"]))
                seen.append(bot.relay.status_text(ME))
                return
            time.sleep(0.05)

    t = threading.Thread(target=watch)
    t.start()
    run_one(worker, bot, s)
    t.join()
    assert seen and seen[0] == "第 2 步 · 改 service.py"
    assert "    第 2 步 · 改 service.py" in seen[1]
    assert bot.sent == ["#1 完成\n改好了"]
    log = next((tmp_path / "home/logs").glob("*-job1.md")).read_text(encoding="utf-8")
    assert '"name": "Edit"' in log                                   # the whole stream is kept


# ── the bot's side ────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_relay(progress_min=3):
    clock, sent = Clock(), []
    cfg = SimpleNamespace(
        claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME, claude_relay_only="off",
        claude_relay_progress_min=progress_min,
    )
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock,
                        send=lambda chat, text: sent.append(text) or True)
    return relay, clock, sent


def wait(relay, clock, seconds, progress=None):
    """Let time pass while the Mac sends a heartbeat every minute (so the job is not lost)."""
    while seconds > 0:
        step = min(60, seconds)
        clock.now += step
        seconds -= step
        for job in relay.store.running():
            relay.heartbeat(job["id"], progress)


def ask(relay, text):
    return relay.handle({"chat_id": ME, "sender_id": ME, "content": text, "is_group": False,
                         "timestamp": relay._clock()})


def test_a_job_that_waited_says_when_it_starts_then_reports_every_few_minutes():
    relay, clock, sent = make_relay()
    ask(relay, "cc 大任务")
    clock.now += 90                                                   # the Mac was busy or offline
    relay.next_job(0)
    relay.tick()
    assert sent == ["#1 开始了（排队了 1分30秒）。"]
    wait(relay, clock, 179, {"steps": 2, "doing": "改 a.py", "plan": ""})
    relay.tick()
    assert len(sent) == 1                                             # not yet 3 minutes
    wait(relay, clock, 1)
    relay.tick()
    assert sent[-1] == "#1 进行中（3分钟）：第 2 步 · 改 a.py"
    wait(relay, clock, 600)
    relay.tick()
    assert len(sent) == 2                                             # nothing new: no repeat
    relay.heartbeat(1, {"steps": 9, "doing": "运行 pytest", "plan": "2/4"})
    relay.tick()
    assert sent[-1] == "#1 进行中（13分钟）：第 9 步 · 运行 pytest · 计划 2/4"
    relay.finish(1, {"ok": True, "text": "好了", "duration_s": 900})
    relay.tick()
    assert sent[-1] == "#1 完成（15分钟）\n好了"


def test_a_job_that_started_at_once_gets_no_started_message():
    relay, clock, sent = make_relay()
    ask(relay, "cc 小任务")
    relay.next_job(0)
    relay.tick()
    clock.now += 60
    relay.heartbeat(1, {"steps": 1, "doing": "看 a.py"})
    relay.finish(1, {"ok": True, "text": "好了"})
    relay.tick()
    assert sent == ["#1 完成\n好了"]


def test_progress_can_be_switched_off():
    relay, clock, sent = make_relay(progress_min=0)
    ask(relay, "cc 大任务")
    clock.now += 90
    relay.next_job(0)
    wait(relay, clock, 3600, {"steps": 2, "doing": "改 a.py"})
    relay.tick()
    assert sent == []


def test_a_bad_heartbeat_body_is_harmless():
    relay, clock, sent = make_relay()
    ask(relay, "cc 任务")
    relay.next_job(0)
    assert relay.heartbeat(1, {"steps": "many", "doing": None}) == {"cancel": False}
    assert progress_line(relay.store.get(1)["progress"]) == ""
    assert relay.heartbeat(1, {}) == {"cancel": False}                 # an old worker: no progress
    assert progress_line("{broken") == "" and progress_line("[]") == ""


def test_an_old_database_gets_the_progress_columns(tmp_path):
    path = tmp_path / "relay.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE relay_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
        sender_id TEXT NOT NULL, text TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', session_id TEXT,
        status TEXT NOT NULL, created_at REAL NOT NULL, claimed_at REAL, heartbeat_at REAL, finished_at REAL,
        cancel_requested INTEGER NOT NULL DEFAULT 0, result_text TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}', delivered INTEGER NOT NULL DEFAULT 1)""")
    con.execute("INSERT INTO relay_jobs (chat_id, sender_id, text, status, created_at) VALUES ('a', 'a', 'x', 'running', 1)")
    con.commit()
    con.close()
    store = RelayStore(path)
    assert store.get(1)["progress"] == "{}" and store.get(1)["progress_sent_at"] is None
    store.heartbeat(1, 2, {"steps": 1, "doing": "看", "plan": ""})
    assert progress_line(store.get(1)["progress"]) == "第 1 步 · 看"


@pytest.mark.parametrize("value, ok", [(0, True), (120, True), (-1, False), (121, False)])
def test_the_progress_interval_is_checked(value, ok):
    kwargs = dict(claude_relay="on", claude_relay_token="t" * 32, admin_wxid=ME, claude_relay_prefix="cc",
                  claude_relay_progress_min=value)
    if ok:
        _validate_config(kwargs)
    else:
        with pytest.raises(RuntimeError, match="CLAUDE_RELAY_PROGRESS_MIN"):
            _validate_config(kwargs)
