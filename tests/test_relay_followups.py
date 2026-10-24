"""Follow-ups: what the person sends while their job runs goes to the running Claude."""

import io
import json
import sqlite3
import sys
import textwrap
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from test_claude_relay_worker import ROOT, TOKEN, bot, fake_claude, run_one, settings, worker  # noqa: F401  (fixtures)

from src.relay import commands as C
from src.relay.attachments import Held
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

ME = "wxid_me"


def make_relay(**overrides):
    values = dict(
        claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME, claude_relay_only="on", claude_relay_progress_min=0,
    )
    values.update(overrides)
    sent = []
    relay = ClaudeRelay(SimpleNamespace(**values), store=RelayStore(":memory:"),
                        send=lambda chat, text: sent.append(text) or True)
    return relay, sent


def say(relay, text, sender=ME):
    return relay.handle({"chat_id": sender, "sender_id": sender, "content": text, "is_group": False,
                         "msg_type": 1, "timestamp": time.time()})[1]


# ── commands ──────────────────────────────────────────────────────────────

def test_a_new_task_of_its_own():
    assert C.parse("新任务 跑测试") == C.RelayCommand("task", text="跑测试", queue=True)
    assert C.parse("排队：@relay 看日志") == C.RelayCommand("task", text="看日志", project="relay", queue=True)
    assert C.parse("跑测试") == C.RelayCommand("task", text="跑测试")
    assert C.parse("新任务的想法是这样") == C.RelayCommand("task", text="新任务的想法是这样")
    assert C.parse("排队中时再发") == C.RelayCommand("task", text="排队中时再发")


# ── the bot ───────────────────────────────────────────────────────────────

def test_a_message_sent_while_the_job_runs_goes_to_it():
    relay, sent = make_relay()
    say(relay, "做个大任务")
    assert "（#2）" in say(relay, "排队中时再发")                            # #1 has not started: a new job
    assert relay.store.get(2)["text"] == "排队中时再发"
    relay.next_job(0)                                                       # #1 runs now
    reply = say(relay, "顺便看看测试")
    assert reply.startswith("收到，已转给正在做的 #1（Claude 下一步就能看到）。") and "「新任务 内容」" in reply
    assert relay.store.get(3) is None                                       # no new job
    assert relay.followups(1, 0, 0) == {"messages": [{"id": 1, "text": "顺便看看测试"}]}


def test_what_does_not_go_to_the_running_job():
    relay, sent = make_relay(claude_relay_wxids=[ME, "wxid_two"])
    say(relay, "大任务")
    relay.next_job(0)
    assert "#2" in say(relay, "新任务 另一件事")                              # asked for a job of its own
    assert "#3" in say(relay, "@relay 别的项目")                              # another project
    assert "#4" in say(relay, "别人的任务", sender="wxid_two")                # another chat
    relay._hold(ME, Held("image", {"msg_type": 3, "media_path": ""}, relay._clock()))
    assert "#5" in say(relay, "看看这张图")                                   # pictures go with a job
    assert relay.followups(1, 0, 0) == {"messages": []}
    for command in ("状态", "取消 99"):
        assert "已转给" not in say(relay, command)                              # commands stay commands


def test_the_worker_reads_past_its_cursor_and_closing_is_one_step():
    relay, sent = make_relay()
    say(relay, "大任务")
    relay.next_job(0)
    say(relay, "补充一")
    say(relay, "补充二")
    assert [m["text"] for m in relay.followups(1, 0, 0)["messages"]] == ["补充一", "补充二"]
    assert [m["text"] for m in relay.followups(1, 1, 0)["messages"]] == ["补充二"]
    assert relay.close_input(1, 1) == {"messages": [{"id": 2, "text": "补充二"}]}   # not handed on yet: still open
    assert "已转给正在做的 #1" in say(relay, "补充三")
    assert relay.close_input(1, 3) == {"messages": []}                          # nothing left: closed
    assert "#2" in say(relay, "收尾时再发")                                     # a new job now
    assert relay.followups(99, 0, 0) is None and relay.close_input(99, 0) is None


def test_waiting_for_a_follow_up_returns_when_it_comes():
    relay, sent = make_relay()
    say(relay, "大任务")
    relay.next_job(0)
    threading.Timer(0.3, lambda: say(relay, "来了")).start()
    started = time.monotonic()
    assert relay.followups(1, 0, 10)["messages"][0]["text"] == "来了"
    assert time.monotonic() - started < 5


def test_a_cancel_takes_no_more_input():
    relay, sent = make_relay()
    say(relay, "大任务")
    relay.next_job(0)
    say(relay, "补充")
    say(relay, "取消")
    assert relay.close_input(1, 0) == {"messages": []}
    relay.finish(1, {"ok": False, "text": ""})
    assert relay.store.get(2) is None                                           # dropped, not re-queued


def test_follow_ups_that_came_too_late_become_the_next_job():
    relay, sent = make_relay()
    say(relay, "大任务")
    relay.next_job(0)
    say(relay, "补充一")
    say(relay, "补充二")
    relay.followups(1, 1, 0)                                                    # the worker handed on #1 only
    relay.finish(1, {"ok": True, "text": "好了"})
    job = relay.store.get(2)
    assert job["text"] == "补充二" and job["status"] == "pending"
    relay.tick()
    assert sent == ["#1 完成\n好了\n（你在中途发的消息没赶上，已作为 #2 接着做）"]


def test_an_old_database_gets_the_input_columns(tmp_path):
    path = tmp_path / "relay.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE relay_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
        sender_id TEXT NOT NULL, text TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', session_id TEXT,
        status TEXT NOT NULL, created_at REAL NOT NULL, claimed_at REAL, heartbeat_at REAL, finished_at REAL,
        cancel_requested INTEGER NOT NULL DEFAULT 0, result_text TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}', delivered INTEGER NOT NULL DEFAULT 1)""")
    con.execute("INSERT INTO relay_jobs (chat_id, sender_id, text, status, created_at) VALUES (?, ?, 'x', 'running', 1)",
                (ME, ME))
    con.commit()
    con.close()
    store = RelayStore(path)
    assert store.get(1)["input_closed"] == 0 and store.get(1)["input_upto"] == 0
    assert store.add_followup(ME, "补充", 2) == (1, 1)


def test_over_http(bot):
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(bot.url + path, data=data, method="POST" if body is not None else "GET",
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    bot.ask("cc 大任务")
    bot.relay.next_job(0)
    bot.ask("cc 补充")
    assert call("/relay/jobs/1/messages?after=0&wait=0") == (200, {"messages": [{"id": 1, "text": "补充"}]})
    assert call("/relay/jobs/1/close", {"upto": 1}) == (200, {"messages": []})
    assert call("/relay/jobs/9/messages?wait=0")[0] == 404 and call("/relay/jobs/9/close", {})[0] == 404


# ── the worker ────────────────────────────────────────────────────────────

class FakeProc:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdin.close = lambda: setattr(self, "stdin_closed", True)
        self.stdin_closed = False

    def poll(self):
        return None

    def written(self):
        return [json.loads(line)["message"]["content"] for line in self.stdin.getvalue().splitlines()]


def test_the_conversation_hands_each_follow_up_on_once(worker):
    proc = FakeProc()
    client = SimpleNamespace(close_input=lambda job_id, upto: [{"id": 3, "text": "三"}] if upto < 3 else [])
    c = worker.Conversation(proc, client, 7)
    c.start("任务")
    c.hand_on([{"id": 1, "text": "一"}, {"id": 2, "text": "二"}])
    c.hand_on([{"id": 2, "text": "二"}])                                       # fetched twice: written once
    assert proc.written() == ["任务", "（微信里补充）一", "（微信里补充）二"] and c.upto == 2
    c.on_result()                                                               # #3 came after the turn
    assert proc.written()[-1] == "（微信里补充）三" and not proc.stdin_closed
    c.on_result()                                                               # nothing left
    assert proc.stdin_closed and c.closed
    c.hand_on([{"id": 4, "text": "四"}])
    assert len(proc.written()) == 4                                             # closed: nothing more


def test_an_unreachable_bot_ends_the_input(worker):
    def broken(job_id, upto):
        raise OSError("connection refused")
    proc = FakeProc()
    c = worker.Conversation(proc, SimpleNamespace(close_input=broken), 7)
    c.start("任务")
    c.on_result()
    assert proc.stdin_closed


def test_an_older_bot_without_follow_ups(worker, monkeypatch):
    client = worker.RelayClient("http://127.0.0.1:9", "t")

    def not_found(*args, **kwargs):
        raise urllib.error.HTTPError("u", 404, "not found", {}, None)
    monkeypatch.setattr(client, "_call", not_found)
    assert client.followups(1, 0, wait=0) is None and client.close_input(1, 0) == []


def test_claude_takes_stream_json_input(worker):
    cmd = worker.claude_command({"claude_bin": sys.executable, "permission_mode": "auto"}, None)
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"


def test_several_turns_give_one_result(worker):
    stream = "\n".join(json.dumps(e) for e in (
        {"type": "result", "result": "第一回合", "session_id": "s"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
        {"type": "result", "result": "第二回合", "session_id": "s"},
    ))
    run = {"stdout": stream, "stderr": "", "returncode": 0, "stopped": "", "duration_s": 1}
    assert worker.summarize(run, worker.parse_output(stream), {})["text"] == "第一回合\n\n第二回合"
    single = json.dumps({"type": "result", "result": "只有一个", "session_id": "s"})
    run["stdout"] = single
    assert worker.summarize(run, worker.parse_output(single), {})["text"] == "只有一个"


# Reads the task, "works" for a while, and like the real claude takes the messages that
# arrive meanwhile into this turn; a message after the turn ended makes another turn.
FOLLOWING_CLAUDE = textwrap.dedent("""\
    import json, sys, threading, time
    first = json.loads(sys.stdin.readline())["message"]["content"]
    got = []
    reader = threading.Thread(target=lambda: [got.append(json.loads(l)["message"]["content"]) for l in sys.stdin],
                              daemon=True)
    reader.start()
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "sleep 2"}}]}}), flush=True)
    time.sleep(2.0)
    seen = len(got)
    print(json.dumps({"type": "result", "result": first + " | " + "；".join(got[:seen]),
                      "session_id": "s1", "is_error": False}), flush=True)
    while reader.is_alive() or len(got) > seen:
        if len(got) > seen:
            new, seen = got[seen:], len(got)
            print(json.dumps({"type": "result", "result": "又：" + "；".join(new), "session_id": "s1",
                              "is_error": False}), flush=True)
        time.sleep(0.05)
""")


def test_a_follow_up_reaches_the_running_claude(worker, bot, tmp_path):
    script = tmp_path / "following-claude"
    script.write_text(f"#!{sys.executable}\n" + FOLLOWING_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    s = settings(bot, SimpleNamespace(path=str(script)), tmp_path)
    bot.ask("cc 大任务")
    replies = []

    def add_when_running():
        for _ in range(100):
            job = bot.relay.store.get(1)
            if job and job["status"] == "running":
                time.sleep(0.5)
                replies.append(bot.ask("cc 顺便加上 X")[1])
                return
            time.sleep(0.05)

    t = threading.Thread(target=add_when_running)
    t.start()
    run_one(worker, bot, s)
    t.join()
    assert replies and replies[0].startswith("收到，已转给正在做的 #1")
    assert bot.sent == ["#1 完成\n大任务 | （微信里补充）顺便加上 X"]
    assert bot.relay.store.get(2) is None                                       # no new job
    assert bot.relay.store.get(1)["input_closed"] == 1
