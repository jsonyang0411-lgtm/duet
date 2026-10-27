"""Claude relay, Mac side (tools/claude_relay/worker.py) against the real relay server.

A fake ``claude`` (a small Python script) stands in for Claude Code: it echoes
the task, can wait to be interrupted, and refuses an unknown session the way
the real one does.
"""

import importlib.util
import json
import os
import re
import signal
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


def test_a_new_relay_url_in_config_json_needs_no_restart(worker, bot, fake_claude, tmp_path):
    """The bot's address changed once (its Tailscale address, cross-network.md): the worker asks
    there from its next round on."""
    cfg = SimpleNamespace(
        claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME)
    sent = []
    other = ClaudeRelay(cfg, store=RelayStore(":memory:"),
                        send=lambda chat, text: sent.append(re.sub(r"（[^）]*）", "", text)) or True)
    other.start()
    try:
        host, port = other.address
        s = settings(bot, fake_claude, tmp_path)
        config = tmp_path / "config.json"
        config.write_text(json.dumps(s), encoding="utf-8")
        watched = worker.SettingsFile(config, worker.load_settings(config))
        config.write_text(json.dumps({**s, "relay_url": f"http://{host}:{port}"}), encoding="utf-8")
        os.utime(config, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        other.handle({"chat_id": ME, "sender_id": ME, "content": "cc 在新地址做", "is_group": False})

        class QuickClient(worker.RelayClient):           # short long polls: a stuck worker fails, not hangs
            def next_job(self, wait=1, settings=None, kinds=worker.ALL_KINDS):
                return super().next_job(wait, settings, kinds)

        client = QuickClient(s["relay_url"], s["token"])
        stopper = worker.Stopper()
        runner = threading.Thread(target=worker.serve, args=(s, client, stopper),
                                  kwargs={"once": True, "settings_file": watched}, daemon=True)
        runner.start()
        runner.join(timeout=15)
        stopper.event.set()
        runner.join(timeout=10)
        other.tick()
        assert client.base_url == f"http://{host}:{port}" and sent == ["#1 完成\n完成：在新地址做"]
        assert bot.sent == []
    finally:
        other.stop()


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
    out = capsys.readouterr().out
    assert "bot " + bot.url + ": ok" in out
    assert "network: 127.0.0.1 是本机地址，往返 " in out                       # which network, how fast
    monkeypatch.setenv("RELAY_TOKEN", "wrong" * 8)
    assert worker.main(["--config", str(cfg), "--check"]) == 1
    assert "token wrong" in capsys.readouterr().out


PONG_DIRECT = "pong from win-pc (100.101.102.103) via 192.168.1.20:41641 in 2ms\n"
PONG_DERP = "pong from win-pc (100.101.102.103) via DERP(hkg) in 118ms\n"


def test_check_tells_the_network_and_tailscales_way(worker, monkeypatch):
    def resolve(name):
        if name == "win-pc":
            return "100.101.102.103"
        raise worker.socket.gaierror("no such name")

    monkeypatch.setattr(worker.socket, "gethostbyname", resolve)
    assert [worker.address_kind(h) for h in ("192.168.1.20", "100.101.102.103", "win-pc", "8.8.8.8", "nowhere")] == [
        "lan", "tailscale", "tailscale", "public", "unknown"]
    calls = []
    lan = worker.link_report("http://192.168.1.20:8790", lambda: calls.append(1))
    assert lan == [lan[0]] and lan[0].startswith("network: 192.168.1.20 是局域网地址，往返 ") and len(calls) == 3
    assert "这个地址连不上" in worker.link_report("http://192.168.1.20:8790")[1]   # the bot did not answer
    assert "明文 http 经过公网" in worker.link_report("http://8.8.8.8:8790", lambda: None)[1]
    assert worker.link_report("https://8.8.8.8", lambda: None)[1:] == []           # https: no warning

    def down():
        raise OSError("refused")

    assert "往返" not in worker.link_report("http://192.168.1.20:8790", down)[0]
    ts = worker.link_report("http://win-pc:8790", lambda: None, ping=lambda host: PONG_DERP + PONG_DIRECT)
    assert ts[0].startswith("network: win-pc 是 Tailscale 地址，往返 ")
    assert ts[1] == "  Tailscale：直连（经 192.168.1.20:41641，2 ms）"
    relayed = worker.tailscale_path("win-pc", lambda host: PONG_DERP * 5 + "direct connection not established\n")
    assert relayed.startswith("走中转 DERP(hkg)（118 ms）")
    assert "没有回音" in worker.tailscale_path("win-pc", lambda host: "")
    monkeypatch.setattr(worker.shutil, "which", lambda name: None)
    monkeypatch.setattr(worker, "TAILSCALE_APP_CLI", ())
    assert "找不到 tailscale 命令" in worker.tailscale_path("100.101.102.103")


def test_a_file_going_back_is_read_as_it_goes(worker, tmp_path):
    """One send of the whole file has two minutes in all (a slow link to the bot would cut a big
    file off); read as it goes, the time limit is per block."""
    path = tmp_path / "brief.pdf"
    path.write_bytes(b"%PDF" + b"x" * 100_000)
    client = worker.RelayClient("http://127.0.0.1:9", TOKEN)
    seen = {}

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": 1, "size": 100004}'

    def open_(req, timeout):
        seen.update(streamed=hasattr(req.data, "read"), length=req.get_header("Content-length"), timeout=timeout)
        return Answer()

    client.opener = SimpleNamespace(open=open_)
    assert client.upload(7, path, "brief.pdf") == {"id": 1, "size": 100004}
    assert seen == {"streamed": True, "length": "100004", "timeout": 120}


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


# ── restart in place (kill -USR1): no Terminal window left at 「进程已完成」 ──────────

def test_a_restart_lets_the_job_in_hand_finish_and_asks_for_no_new_one(worker, bot, fake_claude, tmp_path,
                                                                         monkeypatch):
    s = settings(bot, fake_claude, tmp_path)
    bot.ask("cc 第一件")
    stopper = worker.Stopper()
    do_job = worker.do_job

    def restart_midway(*args, **kwargs):
        stopper.request_restart(signal.SIGUSR1, None)           # kill -USR1 while the job runs
        return do_job(*args, **kwargs)

    monkeypatch.setattr(worker, "do_job", restart_midway)
    client = worker.RelayClient(s["relay_url"], s["token"])
    serving = threading.Thread(target=worker.serve, args=(s, client, stopper), daemon=True)
    serving.start()
    serving.join(30)
    assert not serving.is_alive(), "a restart request ends the loop once the job in hand is done"
    bot.relay.tick()
    assert bot.sent == ["#1 完成\n完成：第一件"] and len(fake_claude.calls()) == 1   # not cut short
    assert stopper.exit_code() == worker.RESTART_EXIT
    stopper(signal.SIGTERM, None)                                  # a stop on top of it: a plain stop
    assert stopper.exit_code() == 0


def test_a_restart_asked_while_idle_takes_no_job(worker, bot, fake_claude, tmp_path):
    s = settings(bot, fake_claude, tmp_path)
    bot.ask("cc 重启以后再做")
    stopper = worker.Stopper()
    stopper.request_restart()
    serving = threading.Thread(target=worker.serve, args=(s, worker.RelayClient(s["relay_url"], s["token"]), stopper),
                               daemon=True)
    serving.start()
    serving.join(30)
    assert not serving.is_alive() and fake_claude.calls() == [] and bot.sent == []
    assert stopper.exit_code() == worker.RESTART_EXIT


@pytest.mark.parametrize("name", ["start-worker.command", "start-admin-worker.command", "start-prm-worker.command"])
def test_the_start_scripts_run_a_restarted_worker_again_at_once(worker, name):
    script = (ROOT / "tools/claude_relay" / name).read_text(encoding="utf-8")
    restart, stop = script.index(f"$code -eq {worker.RESTART_EXIT} ]]"), script.index("$code -eq 0 ||")
    assert restart < stop and "continue" in script[restart:stop] and "sleep" not in script[restart:stop]
