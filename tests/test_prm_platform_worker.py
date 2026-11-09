"""Platform actions end to end on the Mac side (tools/claude_relay/worker.py) against the real relay
and PRM service: the draft's action is previewed by the CLI before the draft goes back, an
operator's approval runs it with --execute exactly once, and the reply goes out only after
VERIFIED_SUCCESS.  A fake ``claude`` drafts; a fake ``node`` answers like the platform CLIs."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_prm_worker import CREATOR, TOKEN, bot, creator_says, run_one, settings, worker, workspace  # noqa: F401

from platform_fakes import (
    DY_ITEM, DY_UID, JST_ORDER, JST_SHOP_DY, JST_UID, FakeNode, catalog, dy_designated_done, dy_designated_preview,
    jst_sample_row, jst_samples, rule,
)
from src.prm import platforms
from src.prm.result import SCHEMA

DESIGNATED = {"action": "dy.designated_commission", "item_id": DY_ITEM, "creator_platform_id": DY_UID,
              "commission_rate": 20, "note": "kb/合作政策.md：20%"}

FAKE_CLAUDE = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[1:]
    prompt = json.loads(sys.stdin.readline())["message"]["content"]
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "prompt": prompt}) + "\\n")
    draft = json.load(open(os.environ["FAKE_CLAUDE_DRAFT"], encoding="utf-8"))
    print(json.dumps({"type": "result", "result": json.dumps(draft, ensure_ascii=False), "structured_output": draft,
                      "is_error": False, "total_cost_usd": 0.02, "permission_denials": []}, ensure_ascii=False))
""")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    log, answer = tmp_path / "calls.jsonl", tmp_path / "draft.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_DRAFT", str(answer))

    def answers(**draft):
        base = {"intent": "谈妥专属佣金", "slots": {"佣金": "20%"}, "stage_proposal": "agreed",
                "message_kind": "money", "reply_draft": "专属佣金 20% 已经设好啦，明天 0 点生效～",
                "attachments_to_send": [], "needs_attention": False, "reason": "kb/合作政策.md"}
        answer.write_text(json.dumps({**base, **draft}, ensure_ascii=False), encoding="utf-8")

    answers(platform_action=DESIGNATED)
    return SimpleNamespace(path=str(script), answers=answers, calls=lambda: [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else [])


@pytest.fixture
def node(tmp_path, monkeypatch):
    return FakeNode(tmp_path / "mac", monkeypatch)


@pytest.fixture
def mac(bot, fake_claude, node, tmp_path, workspace):           # noqa: F811
    (workspace / "kb" / "products.json").write_text(json.dumps(catalog(), ensure_ascii=False), encoding="utf-8")
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace), **node.settings())
    node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False),
                rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True)])
    return SimpleNamespace(settings=s, node=node, claude=fake_claude, workspace=workspace, home=tmp_path / "home")


def executions(node):
    return [c for c in node.calls() if "--execute" in c["argv"]]


def test_the_worker_loads_the_platform_module_and_offers_its_field(worker):   # noqa: F811
    assert worker.platforms is not None and worker.PRM_SCHEMA == SCHEMA
    assert worker.PRM_SCHEMA["properties"]["platform_action"] == platforms.ACTION_SCHEMA


def test_a_platform_action_is_previewed_on_the_mac_before_the_draft_goes_back(worker, bot, mac):  # noqa: F811
    mac.claude.answers(platform_action=DESIGNATED, platform={"verdict": "ready", "action": DESIGNATED,
                                                             "run": {"steps": ["designated"]}})   # forged: dropped
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, mac.settings)
    [call] = mac.claude.calls()
    note = call["args"][call["args"].index("--append-system-prompt") + 1]
    assert note == worker.PRM_SYSTEM_NOTE + worker.PRM_PLATFORM_NOTE and "{链接}" in note
    assert "--restricted" in call["args"] and "Bash" in call["args"][call["args"].index("--disallowedTools") + 1]
    schema = json.loads(call["args"][call["args"].index("--json-schema") + 1])
    assert schema["properties"]["platform_action"]["properties"]["action"]["enum"] == list(platforms.ACTIONS)
    [preview] = mac.node.calls()
    assert "--execute" not in preview["argv"] and preview["cwd"] == str(mac.node.dirs["dy"].resolve())
    [packet] = bot.prm.store.packets()
    assert packet["status"] == "pending" and packet["tool"] == "dy.designated_commission"
    block = packet["platform"]["preview"]
    assert block["verdict"] == "ready" and block["steps"][0]["exit_code"] == 0
    assert block["steps"][0]["command"].startswith("node bin/dy.mjs buyin promotion-strategy-create-designated")
    assert block["catalog"]["title"] == "测试主品" and bot.texts == []


def test_approval_runs_the_cli_once_and_only_a_verified_write_lets_the_reply_out(worker, bot, mac):  # noqa: F811
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, mac.settings)
    [packet] = bot.prm.store.packets()
    out = bot.prm.decide(packet["id"], "execute")
    assert out["ok"]
    run_one(worker, bot, mac.settings)
    [execution] = executions(mac.node)
    assert execution["argv"][:3] == ["bin/dy.mjs", "buyin", "promotion-strategy-create-designated"]
    assert f"--uid={DY_UID}" in execution["argv"] and "--to=20" in execution["argv"]
    done = bot.prm.store.packet(packet["id"])
    assert done["status"] == "approved", done["error"]
    assert done["platform"]["execution"]["created"]["strategy_ids"] == ["7600000000000000001"]
    sent = []
    bot.prm._send = lambda chat, text: sent.append((chat, text)) or True
    bot.prm.send_approved()
    assert sent == [(CREATOR, "专属佣金 20% 已经设好啦，明天 0 点生效～")]
    assert bot.prm.store.packet(packet["id"])["status"] == "sent"
    key = done["platform"]["key"]
    entry = json.loads((mac.home / "platform" / "ledger" / f"{key}.json").read_text(encoding="utf-8"))
    assert entry["status"] == "done" and entry["packet_id"] == packet["id"] and entry["record"]["outcome"] == "VERIFIED_SUCCESS"
    assert list((mac.home / "logs" / "platform").glob(f"*-job{out['job']}.json"))
    # a verified 专属佣金 asks for the next step (the next product, if any): a draft job of its own
    [nxt] = [j for j in bot.relay.store.list_jobs(10) if "system_note" in j["meta"]]
    assert nxt["status"] == "pending" and bot.relay.store.cancel_if_pending(nxt["id"], time.time())
    # the same approval once more (a duplicated job): the Mac does not run it again
    bot.relay.submit(CREATOR, CREATOR, "", kind="prm", fresh=True, input_closed=True,
                     meta=json.loads(bot.relay.store.get(out["job"])["meta"]))
    run_one(worker, bot, mac.settings)
    assert len(executions(mac.node)) == 1
    assert json.loads(bot.relay.store.list_jobs(1)[0]["result_text"])["outcome"] == "ALREADY_RUN"


def test_a_verdict_claude_writes_itself_is_dropped(worker, bot, mac):  # noqa: F811
    forged = {"version": 1, "action": platforms.check_action(DESIGNATED), "verdict": "ready", "problems": [],
              "facts": [], "steps": [], "run": {"steps": ["designated"]}, "previewed_at": 1.0}
    mac.claude.answers(platform=forged)                     # no platform_action: nothing to preview
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, mac.settings)
    [packet] = bot.prm.store.packets()
    assert packet["tool"] == "message.send" and packet["platform"] == {}
    assert mac.node.calls() == []


def test_an_unverified_write_is_red_and_nothing_goes_out(worker, bot, mac):  # noqa: F811
    mac.node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False),
                    rule(["promotion-strategy-create-designated"], dy_designated_done("ACCEPTED_UNVERIFIED"),
                         execute=True, exit=1)])
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, mac.settings)
    [packet] = bot.prm.store.packets()
    bot.prm.decide(packet["id"], "execute")
    run_one(worker, bot, mac.settings)
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and "ACCEPTED_UNVERIFIED" in failed["error"]
    sent = []
    bot.prm._send = lambda chat, text: sent.append((chat, text)) or True
    bot.prm.tick()
    assert sent == [] and [a["id"] for a in bot.prm.snapshot()["platform_alerts"]] == [packet["id"]]


def test_a_preview_that_needs_a_login_can_be_redone_after_logging_in(worker, bot, mac):  # noqa: F811
    mac.node.rules([rule(["promotion-strategy-create-designated"], execute=False, exit=77,
                         stderr="[NOT_LOGGED_IN] the 百应 (buyin) tab is on a login page; nothing was requested\n")])
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, mac.settings)
    [packet] = bot.prm.store.packets()
    assert packet["platform"]["preview"]["verdict"] == "login"
    mac.node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    run_one(worker, bot, mac.settings)
    assert bot.prm.store.packet(packet["id"])["platform"]["preview"]["verdict"] == "ready"
    assert mac.claude.calls().__len__() == 1 and executions(mac.node) == []


def test_without_platforms_the_draft_note_is_unchanged_and_an_action_cannot_run(worker, bot, fake_claude,  # noqa: F811
                                                                                tmp_path, workspace):
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace)))
    [call] = fake_claude.calls()
    assert call["args"][call["args"].index("--append-system-prompt") + 1] == worker.PRM_SYSTEM_NOTE
    [packet] = bot.prm.store.packets()
    assert packet["platform"]["preview"]["verdict"] in ("invalid", "unavailable")
    assert not bot.prm.decide(packet["id"], "execute")["ok"]


def test_sync_products_writes_the_catalogue(worker, node, tmp_path, workspace, monkeypatch, capsys):  # noqa: F811
    image = "https://p3-aio.ecombdimg.com/obj/ecom-shop-material/jpeg_m_test_main"
    node.rules([
        rule(["fxg", "products"], {"site": "fxg", "complete": True, "rows": [
            {"productId": DY_ITEM, "name": "测试主品", "priceLower": 1999, "discountPrice": 0, "img": image}]}),
        rule(["fxg", "sku-prices"], {"site": "fxg", "rows": []}),
    ])
    fetched = []
    monkeypatch.setattr(worker.platforms, "fetch_image",
                        lambda url, platform: fetched.append(url) or b"\xff\xd8\xff\xe0 a picture")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"relay_url": "http://127.0.0.1:9", "token": TOKEN, "prm_workspace": str(workspace),
                                  **node.settings(xhs="", tb="")}), encoding="utf-8")
    assert worker.main(["--config", str(config), "--sync-products"]) == 0
    md = (workspace / "kb" / "products.md").read_text(encoding="utf-8")
    assert f"| {DY_ITEM} | 测试主品 | 19.99 |  | assets/products/dy-{DY_ITEM}.jpg |" in md
    assert fetched == [image] and (workspace / "assets" / "products" / f"dy-{DY_ITEM}.jpg").is_file()
    assert "款式图：新下 1 张" in capsys.readouterr().out
    assert worker.prm_asset(workspace, f"assets/products/dy-{DY_ITEM}.jpg") is not None   # a draft may send it
    assert worker.main(["--config", str(config), "--sync-products"]) == 0 and fetched == [image]   # kept, not fetched again
    assert worker.main(["--config", str(config), "--sync-products", "--platforms", "pdd"]) == 2


def test_one_sync_products_at_a_time(worker, node, tmp_path, workspace, monkeypatch, capsys):  # noqa: F811
    """(Review 2026-11-09, round 3.)  A --sync-products by hand while the night's one runs (or the other way round):
    the second waits for the first's lock (its images and catalogue files are written outside the platform CLI lock)
    and, when that does not come in time, runs nothing and writes nothing."""
    import fcntl

    node.rules([rule(["fxg", "products"], {"site": "fxg", "complete": True, "rows": []})])
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"relay_url": "http://127.0.0.1:9", "token": TOKEN, "prm_workspace": str(workspace),
                                  **node.settings(xhs="", tb="")}), encoding="utf-8")
    lock = worker.HOME_DIR / "platform" / "products-sync.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(worker, "PRODUCTS_SYNC_MAX_SEC", 0.3)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert worker.main(["--config", str(config), "--sync-products"]) == 1
    out = capsys.readouterr().out
    assert out.count("在等另一个同步跑完（最多 0.3 秒）…") == 1                   # (round 4: one line as the wait begins)
    assert f"另一个 --sync-products 一直没跑完（等了 0.3 秒，{lock}）" in out
    assert not (workspace / "kb" / "products.md").exists() and node.calls() == []
    assert worker.main(["--config", str(config), "--sync-products"]) == 0          # (let go: it runs)
    assert signal.getsignal(signal.SIGTERM) is not worker._sync_terminated        # (its handler only while it runs)
    assert "在等另一个同步跑完" not in capsys.readouterr().out
    # the platform CLI lock is another one: its wait says so, not 「另一个 --sync-products」 (round 4)
    cli = worker.HOME_DIR / "platform" / "cli.lock"
    monkeypatch.setattr(worker, "_cli_lock", lambda heartbeat=None: platforms.CliLock(cli, wait=0.3))
    with open(cli, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert worker.main(["--config", str(config), "--sync-products"]) == 1
    out = capsys.readouterr().out
    assert "平台命令行的锁一直没等到：" in out and "cli.lock" in out and "另一个 --sync-products" not in out


def test_a_platform_cli_in_its_own_group_is_stopped_with_the_sync(tmp_path):
    """(Round 3, item 3.)  ``run_cli`` starts each command in a process group of its own, which a signal to the sync
    does not reach: ``stop_running`` (the sync's SIGTERM handler) stops it."""
    import threading

    out = {}
    run = threading.Thread(target=lambda: out.update(run=platforms.run_cli(
        [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=60)), daemon=True)
    started = time.monotonic()
    run.start()
    while not platforms._RUNNING and time.monotonic() - started < 10:
        time.sleep(0.05)
    assert platforms.stop_running(grace=2.0) == 1
    run.join(10)
    assert out["run"].exit_code not in (0, None) and time.monotonic() - started < 10 and not platforms._RUNNING
    assert platforms.stop_running() == 0


@pytest.mark.parametrize("where", ["inside", "itself"])
def test_a_preview_that_raises_never_loses_the_draft(worker, bot, mac, monkeypatch, where):  # noqa: F811
    """The preview of a draft's action blows up (a bug, an answer it cannot read): the reply still reaches
    the bot, its action marked 预览出错 and not executable."""
    def boom(*args, **kwargs):
        raise RuntimeError("the preview broke")

    if where == "inside":
        monkeypatch.setattr(worker.platforms, "preview", boom)              # platforms.preview, in its try
    else:
        monkeypatch.setattr(worker, "platform_preview", boom)               # worker.platform_preview as a whole
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, mac.settings)
    [packet] = bot.prm.store.packets()
    assert packet["status"] == "pending" and packet["text"] == "专属佣金 20% 已经设好啦，明天 0 点生效～"
    preview = packet["platform"]["preview"]
    assert preview["verdict"] == "error" and "预览出错" in preview["problems"][0] and "the preview broke" in preview["problems"][0]
    assert packet["tool"] == "dy.designated_commission" and preview["action"]["item_id"] == DY_ITEM
    assert not bot.prm.decide(packet["id"], "execute")["ok"] and mac.node.calls() == []
    assert "预览没通过" in bot.prm.store.deal(packet["deal_id"])["attention"]


def test_a_cli_lock_that_stays_taken_is_an_error_preview_and_a_not_run_execution(worker, bot, mac, monkeypatch):  # noqa: F811
    """Another platform command (--sync-products, say) holds the Mac's CLI lock past the wait: the preview
    says so (the draft still comes back), the execution is NOT_RUN and its approval key is not used up."""
    lock = mac.home / "platform" / "cli.lock"
    monkeypatch.setattr(worker, "_cli_lock",
                        lambda heartbeat=None: worker.platforms.CliLock(lock, wait=0.3, heartbeat=heartbeat))
    other = worker.platforms.CliLock(lock, wait=5)
    with other:
        creator_says(bot, "好的，就按 20% 吧")
        run_one(worker, bot, mac.settings)
    [packet] = bot.prm.store.packets()
    preview = packet["platform"]["preview"]
    assert packet["status"] == "pending" and preview["verdict"] == "error"
    assert "另一个平台命令还在跑" in preview["problems"][0] and mac.node.calls() == []
    assert bot.prm.decide(packet["id"], "repreview")["ok"]                 # the other command is done
    run_one(worker, bot, mac.settings)
    assert bot.prm.store.packet(packet["id"])["platform"]["preview"]["verdict"] == "ready"
    out = bot.prm.decide(packet["id"], "execute")
    assert out["ok"]
    with other:                                                             # and runs again meanwhile
        run_one(worker, bot, mac.settings)
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == "platform_not_run" and executions(mac.node) == []
    key = failed["platform"]["key"]
    assert "另一个平台命令还在跑" in failed["error"] and not (mac.home / "platform" / "ledger" / f"{key}.json").exists()
    bot.relay.submit(CREATOR, CREATOR, "", kind="prm", fresh=True, input_closed=True,   # the same approval again
                     meta=json.loads(bot.relay.store.get(out["job"])["meta"]))
    run_one(worker, bot, mac.settings)
    assert len(executions(mac.node)) == 1
    entry = json.loads((mac.home / "platform" / "ledger" / f"{key}.json").read_text(encoding="utf-8"))
    assert entry["status"] == "done" and entry["record"]["outcome"] == "VERIFIED_SUCCESS"


def test_a_preview_that_could_not_be_made_is_an_error_the_bot_takes(worker, monkeypatch):  # noqa: F811
    block = worker.preview_failed({"action": "dy.designated_commission"}, "Mac 上预览出错：x")
    assert block["verdict"] == "error" and block["action"] is None and block["requested"]
    assert platforms.check_block(block)["verdict"] == "error"
    monkeypatch.setattr(worker, "platforms", None)                          # no platform module at all
    bare = worker.preview_failed(DESIGNATED, "Mac 上预览出错：y")
    assert platforms.check_block(bare)["problems"] == ["Mac 上预览出错：y"]


# ── a cancel before the write starts (mac review C#1) ─────────────────────

def approved_job(worker, bot, mac):  # noqa: F811
    """A draft with a ready action, approved: its execution job is queued (``decide``'s answer)."""
    creator_says(bot, "好的，就按 20% 吧")
    run_one(worker, bot, mac.settings)
    [packet] = bot.prm.store.packets()
    out = bot.prm.decide(packet["id"], "execute")
    assert out["ok"]
    return packet, out


def test_cancelling_while_the_cli_lock_is_taken_runs_nothing(worker, bot, mac):  # noqa: F811
    """Another platform command (--sync-products, a preview) holds the lock; the operator cancels the execution
    in the console meanwhile: the heartbeat says so while the job waits, nothing is executed (it used to take
    the lock afterwards and write anyway), the result is NOT_RUN and the approval key stays unused."""
    import threading

    packet, out = approved_job(worker, bot, mac)
    other = worker.platforms.CliLock(mac.home / "platform" / "cli.lock", wait=5)
    with other:
        serving = threading.Thread(target=run_one, args=(worker, bot, mac.settings), daemon=True)
        serving.start()
        deadline = time.monotonic() + 20
        while bot.relay.store.get(out["job"])["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert bot.prm.store.packet(packet["id"])["status"] == "executing"
        assert bot.relay.cancel_from_console(out["job"]) == "requested"
        serving.join(10)                                        # done while the other command still runs
        assert not serving.is_alive()
    assert executions(mac.node) == []
    job = bot.relay.store.get(out["job"])
    record = json.loads(job["result_text"])
    assert job["status"] == "cancelled" and record["outcome"] == "NOT_RUN" and "平台没有动" in record["problems"][0]
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == "platform_not_run"
    assert not (mac.home / "platform" / "ledger" / f"{failed['platform']['key']}.json").exists()


def test_a_job_cancelled_by_the_time_it_has_the_lock_runs_nothing(worker, bot, mac):  # noqa: F811
    """The lock is free, but the cancel came in between the Mac taking the job and its first write: asked once
    more right before the write, nothing is executed."""
    packet, out = approved_job(worker, bot, mac)
    client = worker.RelayClient(mac.settings["relay_url"], mac.settings["token"])
    job = client.next_job(wait=1, settings=mac.settings, kinds=("prm",))
    assert job["id"] == out["job"] and bot.relay.cancel_from_console(out["job"]) == "requested"
    result = worker.do_job(mac.settings, job, client, worker.Stopper(), ("prm",))
    record = json.loads(result["text"])
    assert result["ok"] and record["outcome"] == "NOT_RUN" and "开始写平台之前被取消" in record["problems"][0]
    assert executions(mac.node) == []
    assert not (mac.home / "platform" / "ledger" / f"{record['key']}.json").exists()
    assert worker.deliver(client, job["id"], result, worker.Stopper())
    bot.relay.tick()
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == "platform_not_run"


def test_a_write_under_way_is_not_stopped_by_a_cancel(worker, bot, mac):  # noqa: F811
    """Cancelled while the CLI is writing: it runs to its end (stopping a write half way is worse), the outcome
    is the platform's, and the reply does not go out on its own."""
    import threading

    mac.node.rules([rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False),
                    rule(["promotion-strategy-create-designated"], dy_designated_done(), execute=True, sleep=1.5)])
    packet, out = approved_job(worker, bot, mac)
    serving = threading.Thread(target=run_one, args=(worker, bot, mac.settings), daemon=True)
    serving.start()
    deadline = time.monotonic() + 20
    while not executions(mac.node) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bot.relay.cancel_from_console(out["job"]) == "requested"
    serving.join(30)
    record = json.loads(bot.relay.store.get(out["job"])["result_text"])
    assert record["outcome"] == "VERIFIED_SUCCESS" and len(executions(mac.node)) == 1
    done = bot.prm.store.packet(packet["id"])
    assert done["status"] == "failed" and "中途被取消" in done["error"]


# ── once the approval key is taken: never "not run" (mac review F7) ───────

def test_a_platform_log_that_cannot_be_written_changes_nothing(worker, bot, mac, monkeypatch):  # noqa: F811
    """The write is done and verified, then the Mac cannot write its log (a full disk): the outcome goes back as
    it is (it used to become 「Mac 没有执行」 NOT_RUN, and the same 专属佣金 could be approved once more)."""
    def full_disk(job, record):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(worker, "save_platform_log", full_disk)
    packet, out = approved_job(worker, bot, mac)
    run_one(worker, bot, mac.settings)
    assert len(executions(mac.node)) == 1
    done = bot.prm.store.packet(packet["id"])
    assert done["status"] == "approved", done["error"]
    assert done["platform"]["execution"]["outcome"] == "VERIFIED_SUCCESS"


@pytest.mark.parametrize("when", ["after", "before"])
def test_an_execution_that_breaks_once_its_key_is_taken_is_unknown(worker, bot, mac, monkeypatch, when):  # noqa: F811
    """Something breaks in the worker after the approval key went into the ledger (here: after the platform
    call): the bot gets UNKNOWN 「不知道执行了没有」, never NOT_RUN.  Broken before the key was taken: NOT_RUN."""
    real = worker.platforms.execute

    def breaks(*args, **kwargs):
        if when == "before":
            raise RuntimeError("broke before the ledger")
        real(*args, **kwargs)
        raise RuntimeError("broke after the platform call")

    monkeypatch.setattr(worker.platforms, "execute", breaks)
    packet, out = approved_job(worker, bot, mac)
    run_one(worker, bot, mac.settings)
    job = bot.relay.store.get(out["job"])
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed"
    if when == "after":
        record = json.loads(job["result_text"])
        assert len(executions(mac.node)) == 1 and job["status"] == "done"
        assert record["outcome"] == "UNKNOWN" and "broke after the platform call" in record["problems"][0]
        assert failed["outcome"] == "platform_unknown" and "UNKNOWN" in failed["error"]
    else:
        assert executions(mac.node) == [] and job["status"] == "failed"
        assert failed["outcome"] == "platform_not_run"


# ── a sample order's receiver in the Mac's platform log: province and city only (mac review C#2) ──

def test_the_platform_log_keeps_a_sample_orders_receiver_to_province_and_city(worker, bot, mac):  # noqa: F811
    """The log of an execution is kept for good: it used to hold the district, the start of the street and the
    masked number (the CLI's JSON in a step's raw and the 收货 fact).  Now only the province and the city; what
    goes back to the bot is all of it (the operators check the order until the 8.2 clean-up)."""
    from platform_fakes import DY_SKU, JST_SHOP_DY, JST_UID, jst_done, jst_preview

    s = {**mac.settings, **mac.node.settings(jst_shops={"dy": JST_SHOP_DY}, jst_expect_uid=JST_UID, jst_logistics=["SF"])}
    mac.node.rules([rule(["sample-create"], jst_preview(), execute=False), rule(["sample-create"], jst_done(), execute=True)])
    mac.claude.answers(reply_draft="寄样单建好啦～", platform_action={
        "action": "jst.sample_order", "shop": "dy", "items": [{"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]}],
        "creator_platform": "dy", "creator_uid": DY_UID,
        "receiver": {"name": "张三", "phone": "〈手机1〉", "province": "广东省", "city": "佛山市", "district": "南海区",
                     "address": "某某路100号1栋101"}})
    creator_says(bot, "收件人张三 13900001111，广东省佛山市南海区某某路100号1栋101")
    run_one(worker, bot, s)
    [packet] = bot.prm.store.packets()
    assert packet["platform"]["preview"]["verdict"] == "ready", packet["platform"]["preview"]["problems"]
    out = bot.prm.decide(packet["id"], "execute")
    assert out["ok"], out
    run_one(worker, bot, s)
    done = bot.prm.store.packet(packet["id"])
    assert done["status"] == "approved", done["error"]
    assert "南海区" in json.dumps(done["platform"]["execution"], ensure_ascii=False)    # the bot has all of it
    [log_file] = (mac.home / "logs" / "platform").glob(f"*-job{out['job']}.json")
    text = log_file.read_text(encoding="utf-8")
    for gone in ("南海区", "某某路", "13900001111"):
        assert gone not in text, gone
    kept = json.loads(text)
    [step] = kept["steps"]
    assert json.loads(step["raw"])["receiver"]["region"] == "广东省 佛山市"
    assert {f["label"]: f["value"] for f in step["facts"]}["收货"] == "张* 139****1111 广东省 佛山市 （详细地址已清除）（新增到达人名下）"
    assert kept["outcome"] == "VERIFIED_SUCCESS" and kept["created"]["sample_order_no"]


def test_older_platform_logs_are_cut_down_once_they_are_a_week_old(worker, workspace, tmp_path):  # noqa: F811
    """Logs an older worker wrote (with the district and the start of the street) are cut to province and city
    once they are KEEP_INBOX_DAYS old, like the Mac's other prm files; each log once, keeping its time; a log that
    is not JSON is left as it is.  prune_prm_files (every prm job) does it."""
    from platform_fakes import jst_done

    folder = tmp_path / "home" / "logs" / "platform"
    folder.mkdir(parents=True)
    record = worker.platforms.record("VERIFIED_SUCCESS")
    record["steps"] = [{"name": "create", "raw": json.dumps(jst_done(), ensure_ascii=False, indent=2), "facts": [
        {"label": "收货", "value": "张* 139****1111 广东省 佛山市 南海区 某某路 100…（12 字）（新增到达人名下）"}]}]
    now = time.time()
    old, young, broken = folder / "20261001-120000-job7.json", folder / "20261027-120000-job8.json", folder / "x-job9.json"
    for path, age in ((old, 8), (young, 1), (broken, 8)):
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) if path != broken else "{cut", encoding="utf-8")
        os.utime(path, (now - age * 86400, now - age * 86400))
    worker.prune_prm_files(workspace)
    assert "南海区" not in old.read_text(encoding="utf-8") and "某某路" not in old.read_text(encoding="utf-8")
    assert abs(old.stat().st_mtime - (now - 8 * 86400)) < 2                    # its time stays
    assert "南海区" in young.read_text(encoding="utf-8") and broken.read_text(encoding="utf-8") == "{cut"
    step = json.loads(old.read_text(encoding="utf-8"))["steps"][0]
    assert json.loads(step["raw"])["receiver"]["region"] == "广东省 佛山市"
    assert step["facts"][0]["value"] == "张* 139****1111 广东省 佛山市 （详细地址已清除）（新增到达人名下）"
    old.write_text("touched", encoding="utf-8")                                # looked at once: not again
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    assert worker.coarse_platform_logs(now - 7 * 86400) == 0 and old.read_text(encoding="utf-8") == "touched"
    assert worker.coarse_platform_logs(now + 1) == 1                           # a week later: the young one too
    assert "南海区" not in young.read_text(encoding="utf-8")


# ── 聚水潭寄样单状态（PRM_SAMPLE_SYNC_MIN）：不跑模型的「sample_status」任务 ──

SYNC_CHAT = "prm:sample-status"
ORDER2 = "YP2610260000002"


@pytest.fixture
def jst_mac(bot, fake_claude, node, tmp_path, workspace):           # noqa: F811
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace),
                 **node.settings(jst_expect_uid=JST_UID, jst_shops={"dy": JST_SHOP_DY}, jst_logistics=["SF"]))
    return SimpleNamespace(settings=s, node=node, claude=fake_claude, home=tmp_path / "home")


def status_job(bot, orders):  # noqa: F811
    """What the bot's sync round queues: one prm job, no creator, the order numbers in its meta."""
    _, job_id = bot.relay.submit(SYNC_CHAT, SYNC_CHAT, f"查聚水潭寄样单的状态（{len(orders)} 张）", kind="prm",
                                 meta={"op": "sample_status", "orders": list(orders)}, fresh=True, input_closed=True)
    return job_id


def answer_of(bot, job_id):  # noqa: F811
    job = bot.relay.store.get(job_id)
    assert job["status"] == "done", job
    return json.loads(job["result_text"])


def test_a_sample_status_job_reads_jushuitan_without_the_model_and_answers_briefly(worker, bot, jst_mac):  # noqa: F811
    jst_mac.node.rules([rule(["samples"], jst_samples(
        jst_sample_row(JST_ORDER, "SENT", waybills=[("顺丰速运", "SF1234567890", "2026-10-27 18:00:00")]),
        jst_sample_row(ORDER2, "INIT")))])
    job_id = status_job(bot, [JST_ORDER, ORDER2])
    run_one(worker, bot, jst_mac.settings)
    assert jst_mac.claude.calls() == []                                          # no model
    [call] = jst_mac.node.calls()
    assert call["argv"][:2] == ["bin/jst.mjs", "samples"] and "--raw" not in call["argv"]
    assert f"--order-no={JST_ORDER},{ORDER2}" in call["argv"] and f"--expect-uid={JST_UID}" in call["argv"]
    answer = answer_of(bot, job_id)
    assert answer["op"] == "sample_status" and answer["stopped"] is None
    assert answer["orders"][JST_ORDER]["ships"][0]["waybill"] == "SF1234567890"
    assert answer["orders"][ORDER2]["status"] == "INIT"
    text = bot.relay.store.get(job_id)["result_text"]
    assert "测试达人" not in text and "test_daren_01" not in text and len(text) < 2000      # short, no creator
    assert (jst_mac.home / "platform" / "cli.lock").exists()                     # under the platform CLI lock


@pytest.mark.parametrize("exit_code, verdict", [(77, "login"), (76, "wall"), (79, "wrong_account")])
def test_login_risk_and_another_account_go_back_as_they_are_and_nothing_is_retried(worker, bot, jst_mac,  # noqa: F811
                                                                                  exit_code, verdict):
    jst_mac.node.rules([rule(["samples"], stderr=f"[TAG] 退出码 {exit_code} 的那一行\n", exit=exit_code)])
    orders = [f"YP26102600{i:05d}" for i in range(25)]
    job_id = status_job(bot, orders)
    run_one(worker, bot, jst_mac.settings)
    assert len(jst_mac.node.calls()) == 1                                        # stopped at the first call
    answer = answer_of(bot, job_id)
    assert answer["stopped"]["verdict"] == verdict and answer["stopped"]["exit_code"] == exit_code
    assert f"退出码 {exit_code} 的那一行" in answer["stopped"]["message"] and answer["not_asked"] == orders


def test_the_job_waits_for_the_cli_lock_and_pauses_between_two_calls(worker, bot, jst_mac, monkeypatch):  # noqa: F811
    import threading

    monkeypatch.setattr(worker.platforms, "SAMPLES_GAP_SEC", 0.4)
    orders = [f"YP26102600{i:05d}" for i in range(25)]
    jst_mac.node.rules([rule(["samples"], jst_samples(*[jst_sample_row(n, "WAITING_SEND") for n in orders]))])
    job_id = status_job(bot, orders)
    other = worker.platforms.CliLock(jst_mac.home / "platform" / "cli.lock", wait=5)
    with other:                                                       # another platform command runs meanwhile
        serving = threading.Thread(target=run_one, args=(worker, bot, jst_mac.settings), daemon=True)
        serving.start()
        time.sleep(0.5)
        assert jst_mac.node.calls() == []                             # it waits for the lock
    serving.join(20)
    assert not serving.is_alive()
    first, second = jst_mac.node.calls()
    assert second["at"] - first["at"] >= 0.4                          # paced like the CLIs' own paging
    assert len(answer_of(bot, job_id)["orders"]) == 25


def test_a_mac_without_the_jushuitan_cli_says_so_and_runs_nothing(worker, bot, fake_claude, node, tmp_path,  # noqa: F811
                                                                  workspace):
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace), **node.settings(jst=""))
    job_id = status_job(bot, [JST_ORDER])
    run_one(worker, bot, s)
    answer = answer_of(bot, job_id)
    assert answer["stopped"]["verdict"] == "unavailable" and node.calls() == []


def test_the_sample_status_job_runs_on_the_macs_system_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "jst" / "bin").mkdir(parents=True)
    (tmp_path / "jst" / "bin" / "jst.mjs").write_text(
        "import json, sys\n"
        "numbers = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--order-no=')][0].split(',')\n"
        "print(json.dumps({'command': 'samples', 'rows': [{'orderNo': n, 'status': 'SIGNED', 'signTime': "
        "'2026-10-29 09:30:00', 'erpOrders': [], 'daren': {'nickName': 'x'}} for n in numbers]}))\n",
        encoding="utf-8")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(root / 'tools/claude_relay/worker.py')!r})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        "class Client:\n"
        "    def heartbeat(self, job_id, progress=None):\n"
        "        return {'cancel': False}\n"
        f"settings = {{'platforms': {{'jst': {str(tmp_path / 'jst')!r}, 'node_bin': sys.executable}}}}\n"
        "job = {'id': 7, 'kind': 'prm', 'meta': {'op': 'sample_status', 'orders': ['YP2610260000001']}}\n"
        "out = w.do_job(settings, job, Client(), w.Stopper(), ('prm',))\n"
        "answer = json.loads(out['text'])\n"
        "assert out['ok'] and answer['orders']['YP2610260000001']['signed_at'] == 1793237400.0, out\n"
        "assert 'nickName' not in out['text']\n"
        "ctx = w.invite_context(settings, {'douyin_ids': ['test_daren_01', 7]})\n"
        "assert ctx['douyin_ids'] == ['test_daren_01'] and ctx['invite_ledger'].endswith('creator-invites'), ctx\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")


def test_the_bots_round_goes_through_the_real_worker_and_fills_the_deal(worker, bot, jst_mac, monkeypatch):  # noqa: F811
    """端到端（副驾驶模式）：机器人排的一轮由真的 worker 读假的 jst samples，答复回到机器人：运单号填进合作单、
    发货通知等人审；同一轮再读一次我们手动建的单（``samples --mine``，问过的单不算进去）。"""
    from test_prm_sample_sync import verified_order

    monkeypatch.setattr(worker.platforms, "SAMPLES_GAP_SEC", 0.01)
    jst_mac.node.rules([rule(["samples"], jst_samples(
        jst_sample_row(JST_ORDER, "SENT", waybills=[("顺丰速运", "SF1234567890", "2026-10-27 18:00:00")])))])
    order = verified_order(bot.prm)
    bot.prm._sample_sync_sec = 3600
    bot.prm.store.set_meta_value("sample_sync_at", time.time() - 3601)       # an interval since the last round
    job_id = bot.prm.sweep_sample_status()
    assert job_id is not None
    run_one(worker, bot, jst_mac.settings)
    calls = jst_mac.node.calls()
    assert jst_mac.claude.calls() == [] and len(calls) == 2
    assert "--mine" not in calls[0]["argv"] and "--mine" in calls[1]["argv"]
    assert bot.prm.store.hand_sample(JST_ORDER) is None                          # the bot's own: not one of ours by hand
    deal = bot.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "shipped" and bot.prm.store.latest_shipment(deal["id"])["waybill"] == "SF1234567890"
    [notice] = [p for p in bot.prm.store.packets(("pending",)) if p["kind"] == "shipping_notice"]
    assert notice["route"] == "review" and "SF1234567890" in notice["text"]
    assert bot.prm.store.sample_order(JST_ORDER)["sent_at"] == 1793095200.0


def test_the_mac_reads_the_invite_ledger_and_a_product_the_card_opened_is_not_opened_again(worker, bot, mac):  # noqa: F811
    """衔接（邀约卡的 25%）：批量邀约工具的台账在 Mac 上（默认 ~/.claude-relay/platform/creator-invites/invites.jsonl）。
    机器人把达人的抖音号随起草任务交给 Mac（``meta.creator``，来自核验成功的抖店寄样单）；草稿要开的专属佣金这一款邀约卡
    已经开过，预览不跑命令行，草稿里的平台动作去掉、回复留给运营核对。"""
    from test_prm_sample_sync import verified_order

    ledger = mac.home / "platform" / "creator-invites" / "invites.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in [
        {"at": "2026-10-28T15:00:00+08:00", "event": "attempt", "attempt": "9f1c00000001", "plan_id": "P1",
         "aweme_id": "test_daren_01", "products": [DY_ITEM], "commission_rate": 25, "category": "small"},
        {"at": "2026-10-28T15:00:05+08:00", "event": "result", "attempt": "9f1c00000001", "plan_id": "P1",
         "aweme_id": "test_daren_01", "exit": 0, "outcome": "VERIFIED_SUCCESS", "platform_writes": 1, "sent": True,
         "timed_out": False}]), encoding="utf-8")
    verified_order(bot.prm, account="test_daren_01")
    creator_says(bot, "好的，就按 20% 吧")
    job = bot.relay.store.list_jobs(1)[0]
    assert json.loads(job["meta"])["creator"]["douyin_ids"] == ["test_daren_01"]
    run_one(worker, bot, mac.settings)
    assert mac.node.calls() == []                                            # the platform is not asked
    [packet] = [p for p in bot.prm.store.packets() if p["job_id"] == job["id"]]
    assert packet["tool"] == "message.send" and packet["status"] == "pending" and packet["needs_attention"]
    assert "邀约卡开过 25%" in packet["reason"]
