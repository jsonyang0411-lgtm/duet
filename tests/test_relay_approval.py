"""Approvals in WeChat: claude's PermissionRequest hook asks the bot, the person answers."""

import json
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from test_claude_relay_worker import ROOT, TOKEN, bot, fake_claude, run_one, settings, worker  # noqa: F401  (fixtures)

from src.relay import commands as C
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

ME = "wxid_me"


# ── the answer words ──────────────────────────────────────────────────────

def test_answers():
    assert C.parse_answer("同意") == ("allow", None)
    assert C.parse_answer("同意 17") == ("allow", 17)
    assert C.parse_answer("批准#3") == ("allow", 3)
    assert C.parse_answer("OK!") == ("allow", None)
    assert C.parse_answer("好的。") == ("allow", None)
    assert C.parse_answer("拒绝") == ("deny", None)
    assert C.parse_answer("不同意 4") == ("deny", 4)
    assert C.parse_answer("不行") == ("deny", None)
    assert C.parse_answer("同意一下这个提交") is None
    assert C.parse_answer("可以帮我跑测试吗") is None
    assert C.parse_answer("") is None


# ── the bot's side ────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_relay(**overrides):
    clock, sent = Clock(), []
    values = dict(
        claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME, claude_relay_only="off", claude_relay_progress_min=0,
    )
    values.update(overrides)
    relay = ClaudeRelay(SimpleNamespace(**values), store=RelayStore(":memory:"), clock=clock,
                        send=lambda chat, text: sent.append(text) or True)
    return relay, clock, sent


def say(relay, text, sender=ME, msg_type=1, **kw):
    msg = {"chat_id": kw.pop("chat_id", sender), "sender_id": sender, "content": text, "is_group": False,
           "msg_type": msg_type, "timestamp": relay._clock()}
    msg.update(kw)
    return relay.handle(msg)


def running_job(relay, text="cc 推送一下"):
    say(relay, text)
    return relay.next_job(0)["id"]


def test_a_request_goes_to_wechat_and_the_answer_comes_back():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    ask = relay.ask(job, {"tool": "Bash", "detail": "运行命令 git push origin main", "timeout_s": 600})
    assert ask == {"ask_id": 1, "timeout_s": 600.0}
    assert relay.ask_status(1, 0) == {"status": "pending"}
    relay.tick()
    assert sent == ["#1 要你批准：运行命令 git push origin main\n回复「同意」或「拒绝」（10分钟内不回就算拒绝）"]
    relay.tick()
    assert len(sent) == 1                                                  # sent once
    assert "#1 在等你批准：运行命令 git push origin main" in relay.status_text(ME)
    clock.now += 5
    assert say(relay, "同意") == (True, "好，已批准 #1：运行命令 git push origin main")
    assert relay.ask_status(1, 0) == {"status": "allow"}
    assert say(relay, "同意") == (False, None)                               # answered: an ordinary line again
    assert say(relay, "cc 同意") == (True, "现在没有等你批准的操作。")


def test_a_denial_and_an_answer_with_the_job_number():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 rm -rf build"})
    assert say(relay, "cc 拒绝 9") == (True, "#9 现在没有等你批准的操作。")
    assert say(relay, "拒绝 9") == (False, None)                              # no cc, nothing of #9: normal
    assert say(relay, "cc 拒绝 1")[1].startswith("好，#1 不做这一步")
    assert relay.ask_status(1, 0) == {"status": "deny"}


def test_without_a_request_plain_words_are_normal_messages():
    relay, clock, sent = make_relay()
    assert say(relay, "好") == (False, None)                                 # an ordinary chat line
    assert say(relay, "cc 好") == (True, "现在没有等你批准的操作。")
    only, _, _ = make_relay(claude_relay_only="on")
    assert say(only, "好") == (True, "现在没有等你批准的操作。")              # not a task "好"


def test_only_the_allowed_person_in_that_chat_answers():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 git push"})
    assert say(relay, "同意", sender="wxid_other") == (False, None)
    assert say(relay, "同意", is_group=True) == (False, None)
    assert relay.ask_status(1, 0) == {"status": "pending"}


def test_another_allowed_person_cannot_answer_for_this_chat():
    relay, clock, sent = make_relay(claude_relay_wxids=[ME, "wxid_two"])
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 git push"})
    assert say(relay, "cc 同意", sender="wxid_two") == (True, "现在没有等你批准的操作。")
    assert say(relay, "cc 同意", sender="wxid_other", chat_id=ME) == (False, None)   # not allowed
    assert relay.ask_status(1, 0) == {"status": "pending"}


def test_an_answer_replayed_after_a_restart_does_not_count():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 git push", "timeout_s": 3600})
    answered_at = clock.now + 10
    clock.now += 400                                                        # the bot was down meanwhile
    relay.heartbeat(job)
    assert say(relay, "同意", timestamp=answered_at) == (False, None)
    assert relay.ask_status(1, 0) == {"status": "pending"}


def test_no_answer_by_voice_and_none_from_before_the_request():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    clock.now += 10
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 git push"})
    assert say(relay, "同意", timestamp=clock.now - 5) == (False, None)       # typed before the request
    assert say(relay, "[语音] 同意。", msg_type=34) == (True, "批准要打字回复「同意」或「拒绝」，语音不算（免得听错）。")
    old = say(relay, "同意", timestamp=clock.now - 3600)
    assert relay.ask_status(1, 0) == {"status": "pending"} and old == (False, None)


def test_an_unanswered_request_expires_and_wechat_hears_it():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 git push", "timeout_s": 5})       # at least 60 s
    relay.tick()
    clock.now += 59
    relay.heartbeat(job)
    relay.tick()
    assert relay.ask_status(1, 0) == {"status": "pending"}
    clock.now += 2
    relay.heartbeat(job)
    assert relay.ask_status(1, 0) == {"status": "expired"}
    relay.tick()
    assert sent[-1] == "#1 的请求没等到回复，已按拒绝处理。"


def test_a_late_answer_does_not_count():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 git push", "timeout_s": 60})
    relay.tick()
    clock.now += 61
    relay.heartbeat(job)
    assert say(relay, "cc 同意") == (True, "现在没有等你批准的操作。")
    assert relay.ask_status(1, 0) == {"status": "expired"}


def test_cancelling_or_finishing_the_job_ends_its_request():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 a"})
    say(relay, "cc 取消")
    assert relay.ask_status(1, 0) == {"status": "cancelled"}
    job2 = running_job(relay, "cc 再来")
    relay.finish(job, {"ok": False, "text": ""})                              # the first one stops
    relay.ask(job2, {"tool": "Bash", "detail": "运行命令 b"})
    relay.finish(job2, {"ok": True, "text": "好了"})
    assert relay.ask_status(2, 0) == {"status": "cancelled"}
    relay.ask(job2, {"tool": "Bash", "detail": "运行命令 c"}) is None
    assert relay.ask(job2, {"tool": "Bash"}) is None                           # not running any more
    assert relay.ask(99, {"tool": "Bash"}) is None


def test_waiting_for_the_answer_returns_when_it_comes():
    relay, clock, sent = make_relay()
    job = running_job(relay)
    relay.ask(job, {"tool": "Bash", "detail": "运行命令 a"})
    threading.Timer(0.3, lambda: say(relay, "同意")).start()
    started = time.monotonic()
    assert relay.ask_status(1, 10) == {"status": "allow"}
    assert time.monotonic() - started < 5
    assert relay.ask_status(42, 0) is None


def test_over_http_the_hook_can_ask_but_not_answer(bot):
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

    bot.ask("cc 推送")
    assert call("/relay/jobs/1/ask", {"tool": "Bash"}) == (404, None)       # not running yet
    bot.relay.next_job(0)
    status, created = call("/relay/jobs/1/ask", {"tool": "Bash", "detail": "运行命令 git push"})
    assert status == 200 and created["ask_id"] == 1
    assert call("/relay/asks/1?wait=0") == (200, {"status": "pending"})
    assert call("/relay/asks/1", {"status": "allow"})[0] == 404             # no way to answer over HTTP
    assert call("/relay/asks/7?wait=0") == (404, None)


# ── the worker's side ─────────────────────────────────────────────────────

def test_requests_in_a_few_words(worker):
    d = worker.describe_request
    assert d("Bash", {"command": "git push\n origin main", "description": "Push"}) == "运行命令 git push origin main"
    assert d("Edit", {"file_path": "/Users/x/a.py"}) == "修改文件 /Users/x/a.py"
    assert d("Write", {"file_path": "/Users/x/b.md"}) == "写文件 /Users/x/b.md"
    assert d("WebFetch", {"url": "https://example.com"}) == "打开网页 https://example.com"
    assert d("mcp__github__create_pr", {"title": "x"}) == '使用 mcp__github__create_pr（{"title": "x"}）'


def test_claude_gets_the_hook_and_the_note(worker, tmp_path):
    s = {"claude_bin": sys.executable, "permission_mode": "auto", "approval_timeout_min": 5}
    cmd = worker.claude_command(s, None)
    hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert hooks["command"].endswith("worker.py --permission-hook") and hooks["timeout"] == 420
    note = cmd[cmd.index("--append-system-prompt") + 1]
    assert "发到对方微信上请对方批准" in note and "超过 5 分钟" in note and "会被直接拒绝" not in note
    off = worker.claude_command({**s, "wechat_approval": False}, None)
    assert "--settings" not in off and "会被直接拒绝" in off[off.index("--append-system-prompt") + 1]


def test_the_hook_says_no_when_the_bot_cannot_be_reached(worker, tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"relay_url": "http://127.0.0.1:9", "token": "a" * 32}), encoding="utf-8")
    answer = worker.permission_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "3", config)
    decision = answer["hookSpecificOutput"]["decision"]
    assert answer["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert decision["behavior"] == "deny" and "没能把批准请求发到微信" in decision["message"]


APPROVING_CLAUDE = textwrap.dedent("""\
    import json, os, subprocess, sys
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    hook = json.loads(args[args.index("--settings") + 1])["hooks"]["PermissionRequest"][0]["hooks"][0]
    request = {"hook_event_name": "PermissionRequest", "tool_name": "Bash",
               "tool_input": {"command": "git push origin main", "description": "Push"}}
    out = subprocess.run(hook["command"], shell=True, input=json.dumps(request), capture_output=True, text=True)
    decision = json.loads(out.stdout)["hookSpecificOutput"]["decision"]
    print(json.dumps({"type": "result", "result": "批准结果：" + decision["behavior"] + " " + decision.get("message", ""),
                      "session_id": "s1", "is_error": False}))
""")


@pytest.mark.parametrize("answer, expected", [("同意", "批准结果：allow"), ("拒绝", "批准结果：deny 对方在微信里拒绝了这一步")])
def test_a_whole_approval_through_the_hook(worker, bot, tmp_path, monkeypatch, answer, expected):
    script = tmp_path / "approving-claude"
    script.write_text(f"#!{sys.executable}\n" + APPROVING_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_RELAY_HOME", str(home))                       # the hook's own log goes here
    s = settings(bot, SimpleNamespace(path=str(script)), tmp_path)
    config = home / "config.json"
    config.write_text(json.dumps({"relay_url": bot.url, "token": TOKEN}), encoding="utf-8")
    s["config_path"] = str(config)
    bot.ask("cc 推送一下")

    def answer_when_asked():
        for _ in range(200):
            if any("要你批准" in m for m in bot.sent):
                bot.ask(answer)
                return
            bot.relay.tick()
            time.sleep(0.05)

    t = threading.Thread(target=answer_when_asked)
    t.start()
    run_one(worker, bot, s)
    t.join()
    assert bot.sent[0] == "#1 要你批准：运行命令 git push origin main\n回复「同意」或「拒绝」（10分钟内不回就算拒绝）"
    assert bot.sent[-1].startswith("#1 完成\n" + expected)
