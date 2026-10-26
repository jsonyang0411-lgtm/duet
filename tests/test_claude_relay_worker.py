"""Claude relay, Mac side (tools/claude_relay/worker.py) against the real relay server.

A fake ``claude`` (a small Python script) stands in for Claude Code: it echoes
the task, can wait to be interrupted, and refuses an unknown session the way
the real one does.
"""

import importlib.util
import json
import re
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

ROOT = Path(__file__).resolve().parents[1]
ME = "wxid_me"
TOKEN = "k" * 32

FAKE_CLAUDE = textwrap.dedent("""\
    import json, os, signal, sys, time
    args = sys.argv[1:]
    prompt = json.loads(sys.stdin.readline())["message"]["content"]   # stream-json input
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "cwd": os.getcwd(), "prompt": prompt}) + "\\n")
    session = args[args.index("--resume") + 1] if "--resume" in args else "new-session"
    if session == "gone":
        print("No conversation found with session ID: gone")
        sys.exit(1)
    if prompt.startswith("wait"):
        signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
        time.sleep(60)
    if prompt.startswith("deny"):
        print(json.dumps({"result": "部分完成", "session_id": session, "is_error": False,
                          "permission_denials": [{"tool_name": "Bash"}, {"tool_name": "Bash"}]}))
        sys.exit(0)
    print(json.dumps({"type": "result", "result": "完成：" + prompt, "session_id": session,
                      "is_error": False, "total_cost_usd": 0.01, "permission_denials": []}))
""")


@pytest.fixture
def worker(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("claude_relay_worker", ROOT / "tools/claude_relay/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(module, "HEARTBEAT_SEC", 0.2)
    monkeypatch.setattr(module, "STOP_GRACE_SEC", 5)
    return module


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return SimpleNamespace(path=str(script), calls=lambda: [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ] if log.exists() else [])


@pytest.fixture
def bot(tmp_path):
    sent = []
    cfg = SimpleNamespace(
        claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME,
    )
    # The fake claude takes about a second under load: drop "（1秒）" from the heading.
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"),
                        send=lambda chat, text: sent.append(re.sub(r"^(#\d+ \S+?)（[^）]*）", r"\1", text)) or True)
    relay.start()
    host, port = relay.address
    yield SimpleNamespace(relay=relay, url=f"http://{host}:{port}", sent=sent,
                          ask=lambda text: relay.handle({
                              "chat_id": ME, "sender_id": ME, "content": text, "is_group": False}))
    relay.stop()


def settings(bot, fake_claude, tmp_path, **extra):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    values = dict(relay_url=bot.url, token=TOKEN, projects={"proj": str(project)},
                  default_project="proj", claude_bin=fake_claude.path, permission_mode="auto",
                  max_budget_usd=0, timeout_min=60, model="", extra_args=[])
    values.update(extra)
    return values


def run_one(worker, bot, s):
    client = worker.RelayClient(s["relay_url"], s["token"])
    worker.serve(s, client, worker.Stopper(), once=True)
    bot.relay.tick()


def test_a_job_runs_in_the_project_folder_and_the_session_carries_on(worker, bot, fake_claude, tmp_path):
    s = settings(bot, fake_claude, tmp_path)
    bot.ask("cc --help 是什么")                            # a task that looks like an option
    run_one(worker, bot, s)
    assert bot.sent == ["#1 完成\n完成：--help 是什么"]
    call = fake_claude.calls()[0]
    assert call["prompt"] == "--help 是什么" and call["cwd"] == str((tmp_path / "proj").resolve())
    assert "--resume" not in call["args"]
    assert call["args"][call["args"].index("--permission-prompts") + 1] == "none"
    assert call["args"][call["args"].index("--permission-mode") + 1] == "auto"
    bot.ask("cc 接着做")
    run_one(worker, bot, s)
    assert "--resume" in fake_claude.calls()[1]["args"]
    assert fake_claude.calls()[1]["args"][fake_claude.calls()[1]["args"].index("--resume") + 1] == "new-session"
    logs = list((tmp_path / "home/logs").glob("*-job2.md"))
    assert logs and "接着做" in logs[0].read_text(encoding="utf-8")
    assert list((tmp_path / "home/outbox").glob("*.json")) == []


def test_an_unknown_project_is_reported_without_running_claude(worker, bot, fake_claude, tmp_path):
    bot.ask("cc @nope 跑")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path))
    assert bot.sent == ["#1 失败\nMac 上没有叫 nope 的项目。可用的有：proj"] and fake_claude.calls() == []


def test_a_cancel_from_wechat_stops_claude(worker, bot, fake_claude, tmp_path):
    s = settings(bot, fake_claude, tmp_path)
    bot.ask("cc wait 一会儿")
    t = threading.Thread(target=run_one, args=(worker, bot, s))
    t.start()
    for _ in range(50):
        if fake_claude.calls():
            break
        time.sleep(0.1)
    assert "已通知 Mac 停下来" in bot.ask("cc 取消 1")[1]
    t.join(20)
    assert not t.is_alive()
    bot.relay.tick()
    assert bot.sent == ["#1 已取消\n已按你的要求停下。"]


def test_a_vanished_session_starts_a_new_one(worker, bot, fake_claude, tmp_path):
    store = bot.relay._store
    job = store.add(ME, ME, "先做一个")
    store.claim_next()
    store.finish(job, ok=True, text="", session_id="gone")
    store.mark_delivered(job, "done")
    bot.ask("cc 再做一个")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path))
    calls = fake_claude.calls()
    assert len(calls) == 2 and "--resume" in calls[0]["args"] and "--resume" not in calls[1]["args"]
    assert bot.sent == ["#2 完成\n完成：再做一个"]


def test_permission_denials_are_named_in_the_result(worker, bot, fake_claude, tmp_path):
    bot.ask("cc deny 推送一下")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path))
    assert bot.sent == ["#1 完成\n部分完成\n（有 2 个操作需要人工批准，已被拒绝：Bash）\n"
                        "要重做就回复「cc 重试 1」，需要批准时再回「同意」。"]


def test_a_timeout_stops_claude(worker, bot, fake_claude, tmp_path):
    bot.ask("cc wait 很久")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path, timeout_min=0.005))
    assert bot.sent[0].startswith("#1 失败") and "已经停下" in bot.sent[0]


def test_a_result_waits_in_the_outbox_until_the_bot_has_it(worker, bot, fake_claude, tmp_path, monkeypatch):
    s = settings(bot, fake_claude, tmp_path)
    bot.ask("cc 跑")
    client = worker.RelayClient(s["relay_url"], s["token"])
    job = client.next_job(0)
    stopper = worker.Stopper()
    stopper.event.set()                                    # the worker is shutting down
    down = worker.RelayClient("http://127.0.0.1:9", s["token"])   # nothing listens there
    assert worker.deliver(down, job["id"], {"ok": True, "text": "离线时做完的"}, stopper) is False
    assert (tmp_path / "home/outbox/1.json").exists()
    worker.flush_outbox(client, worker.Stopper())          # next start, the bot is back
    bot.relay.tick()
    assert bot.sent == ["#1 完成\n离线时做完的"]
    assert not (tmp_path / "home/outbox/1.json").exists()


def test_check_and_a_wrong_token(worker, bot, fake_claude, tmp_path, capsys, monkeypatch):
    s = settings(bot, fake_claude, tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(s), encoding="utf-8")
    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    assert worker.main(["--config", str(cfg), "--check"]) == 0
    assert "bot " + bot.url + ": ok" in capsys.readouterr().out
    monkeypatch.setenv("RELAY_TOKEN", "wrong" * 8)
    assert worker.main(["--config", str(cfg), "--check"]) == 1
    assert "token wrong" in capsys.readouterr().out


def test_find_claude_prefers_the_newest_vscode_copy(worker, tmp_path, monkeypatch):
    monkeypatch.setattr(worker.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(worker.shutil, "which", lambda name: None)
    for version in ("2.1.9", "2.1.300", "2.1.30"):
        exe = tmp_path / f".vscode/extensions/anthropic.claude-code-{version}-darwin-arm64/resources/native-binary/claude"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    if any(Path(p).exists() for p in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude")):
        pytest.skip("a system-wide claude is installed")
    assert "claude-code-2.1.300-" in worker.find_claude()


def test_a_refused_token_is_read_again_from_the_config(worker, bot):
    """Windows got a new token; config.json already has it; the running worker follows."""
    client = worker.RelayClient(bot.url, "old" * 11, reload_token=lambda: TOKEN)
    assert client.health() and client.token == TOKEN
    stale = worker.RelayClient(bot.url, "old" * 11, reload_token=lambda: "old" * 11)
    with pytest.raises(worker.urllib.error.HTTPError):
        stale.health()
    with pytest.raises(worker.urllib.error.HTTPError):
        worker.RelayClient(bot.url, "old" * 11, reload_token=lambda: None).health()


def test_read_token_follows_the_file(worker, tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"relay_url": "http://x", "token": "a" * 32}), encoding="utf-8")
    assert worker.read_token(cfg) == "a" * 32
    cfg.write_text("{broken", encoding="utf-8")
    assert worker.read_token(cfg) is None


def release(handles):
    for handle in handles:
        handle.close()


def test_only_one_worker_of_a_kind_runs_at_a_time(worker):
    first = worker.single_instance()                 # admin,prm: both locks
    assert first is not None
    assert worker.single_instance() is None          # a second worker on this Mac exits
    assert worker.single_instance(("admin",)) is None and worker.single_instance(("prm",)) is None
    release(first)
    admin, prm = worker.single_instance(("admin",)), worker.single_instance(("prm",))
    assert admin is not None and prm is not None     # a cc worker and a draft worker side by side
    assert worker.single_instance() is None
    release(admin)
    release(prm)
    assert {p.name for p in worker.HOME_DIR.glob("*.lock")} == {"worker.lock", "worker-prm.lock"}


def test_a_second_worker_exits_without_serving(worker, bot, fake_claude, tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(settings(bot, fake_claude, tmp_path)), encoding="utf-8")
    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    held = worker.single_instance()
    monkeypatch.setattr(worker, "serve", lambda *a, **k: pytest.fail("a second worker must not serve"))
    monkeypatch.setattr(worker, "LOCK_WAIT_SEC", 0.3)               # it waited for the lock, in vain
    assert worker.main(["--config", str(cfg)]) == 0
    release(held)


def test_a_restarted_worker_waits_for_the_old_one_to_let_go(worker):
    import threading
    import time
    held = worker.single_instance(("prm",))
    threading.Timer(0.4, lambda: release(held)).start()            # the old worker ends its long poll
    started = time.monotonic()
    handles = worker.wait_for_locks(("prm",), wait=10, poll=0.05)
    assert handles is not None and 0.3 < time.monotonic() - started < 5
    release(handles)
    still = worker.single_instance(("prm",))
    assert worker.wait_for_locks(("prm",), wait=0.3, poll=0.05) is None   # really another worker
    release(still)


def test_stay_awake_lasts_as_long_as_the_worker(worker, monkeypatch):
    started = []
    monkeypatch.setattr(worker.subprocess, "Popen", lambda cmd: started.append(cmd) or "proc")
    assert worker.stay_awake() == "proc"
    assert started == [["/usr/bin/caffeinate", "-s", "-w", str(worker.os.getpid())]]


def test_the_token_can_live_in_its_own_file(worker, tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)
    token = tmp_path / "token"
    token.write_text("b" * 32 + "\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"relay_url": "http://x", "token": "old", "token_file": str(token)}),
                   encoding="utf-8")
    assert worker.load_settings(cfg)["token"] == "b" * 32          # the file wins over "token"
    token.write_text("c" * 32, encoding="utf-8")
    assert worker.read_token(cfg) == "c" * 32                       # a running worker follows it
    token.unlink()
    with pytest.raises(SystemExit):
        worker.load_settings(cfg)
    assert worker.read_token(cfg) is None
    monkeypatch.setenv("RELAY_TOKEN", "d" * 32)
    assert worker.load_settings(cfg)["token"] == "d" * 32           # the environment wins


def test_a_follow_up_without_project_stays_in_the_sessions_project(worker, bot, fake_claude, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    s = settings(bot, fake_claude, tmp_path)
    s["projects"]["other"] = str(other)
    bot.ask("cc @other 统计行数")
    run_one(worker, bot, s)
    bot.ask("cc 最长的那个有几个类")                  # no @other: continues the session's project
    run_one(worker, bot, s)
    bot.ask("cc 新对话")
    bot.ask("cc 现在在哪")                             # a new session: the default project again
    run_one(worker, bot, s)
    cwds = [c["cwd"] for c in fake_claude.calls()]
    assert cwds == [str(other.resolve()), str(other.resolve()), str((tmp_path / "proj").resolve())]
    assert worker.session_projects() == {"new-session": ""}
