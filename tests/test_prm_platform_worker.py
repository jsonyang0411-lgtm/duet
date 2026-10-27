"""Platform actions end to end on the Mac side (tools/claude_relay/worker.py) against the real relay
and PRM service: the draft's action is previewed by the CLI before the draft goes back, an
operator's approval runs it with --execute exactly once, and the reply goes out only after
VERIFIED_SUCCESS.  A fake ``claude`` drafts; a fake ``node`` answers like the platform CLIs."""

import json
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from test_prm_worker import CREATOR, TOKEN, bot, creator_says, run_one, settings, worker, workspace  # noqa: F401

from platform_fakes import (
    DY_ITEM, DY_UID, FakeNode, catalog, dy_designated_done, dy_designated_preview, rule,
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


def test_sync_products_writes_the_catalogue(worker, node, tmp_path, workspace):  # noqa: F811
    node.rules([
        rule(["fxg", "products"], {"site": "fxg", "complete": True, "rows": [
            {"productId": DY_ITEM, "name": "测试主品", "priceLower": 1999, "discountPrice": 0}]}),
        rule(["fxg", "sku-prices"], {"site": "fxg", "rows": []}),
    ])
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"relay_url": "http://127.0.0.1:9", "token": TOKEN, "prm_workspace": str(workspace),
                                  **node.settings(xhs="", tb="")}), encoding="utf-8")
    assert worker.main(["--config", str(config), "--sync-products"]) == 0
    md = (workspace / "kb" / "products.md").read_text(encoding="utf-8")
    assert f"| {DY_ITEM} | 测试主品 | 19.99 |" in md
    assert worker.main(["--config", str(config), "--sync-products", "--platforms", "pdd"]) == 2
