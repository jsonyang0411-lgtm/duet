"""抖音百应私信，Mac 这一侧（tools/claude_relay/worker.py 的 ``buyin_inbox`` 任务、``meta.channel == "buyin"`` 的草稿任务）
接在真的中转和 PRM 服务上：读消息不跑模型、只跑一次只读的 `dy buyin im-inbox`（平台命令锁里，从不带 --execute，每个值
都是 --名字=值，店铺照别的抖音命令核对），答复原样交回；命令行还没有这个命令（用法错误「unknown command」）时交回
``unavailable``，控制台说读不了；百应的草稿任务告诉模型这是百应私信、运营手动发，不预览平台动作、不传文件。假 ``node``
按 tools/claude_relay/README.md「百应私信」里的契约答复。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_prm_platform_worker import DESIGNATED, fake_claude, node  # noqa: F401
from test_prm_worker import CREATOR, bot, run_one, settings, worker, workspace  # noqa: F401

from platform_fakes import DY_UID, rule
from src.prm import platforms
from src.prm.service import BUYIN_CHAT, BUYIN_SYNC_CHAT
from src.prm.store import BUYIN_TOOL

SHOP = "7000000000000001"
CID = "conv_7001"
PHONE = "13812345678"


def inbox(*messages, cursor=None, cid=CID, uid=DY_UID, nickname="小美的抖音") -> dict:
    """What `dy buyin im-inbox --names` prints (exit 0), as the contract says."""
    return {"command": "im-inbox", "conversations": [
        {"conversationId": cid, "creator": {"uid": uid, "awemeId": "xiaomei_dy", "nickname": nickname},
         "messages": list(messages)}], "cursor": cursor}


def said(mid, text, frm="creator", ago=60, kind="text") -> dict:
    return {"id": mid, "at": int(time.time() - ago), "from": frm, "kind": kind, "text": text}


@pytest.fixture
def dy_mac(bot, fake_claude, node, tmp_path, workspace):           # noqa: F811
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace), **node.settings(dy_expect_shop_id=SHOP))
    bot.prm.creator_directory = lambda: [{"wxid": CREATOR, "platform": "DY", "platform_no": "", "platform_uid": DY_UID}]
    return SimpleNamespace(settings=s, node=node, claude=fake_claude, home=tmp_path / "home", workspace=workspace)


def poll_job(bot, cursor=None):  # noqa: F811
    """What the bot's round queues (``sweep_buyin``): one prm job, no creator, the cursor in its meta."""
    _, job_id = bot.relay.submit(BUYIN_SYNC_CHAT, BUYIN_SYNC_CHAT, "读抖音百应私信（精选联盟）", kind="prm",
                                 meta={"op": "buyin_inbox", "cursor": cursor, "limit": platforms.IM_LIMIT},
                                 fresh=True, input_closed=True)
    return job_id


def status(bot):  # noqa: F811
    return bot.prm.snapshot()["buyin"]["status"]


def test_the_job_reads_baiying_once_without_the_model_and_never_with_execute(worker, bot, dy_mac):  # noqa: F811
    dy_mac.node.rules([rule(["buyin", "im-inbox"], inbox(said("m1", f"佣金多少？电话 {PHONE}"), cursor={"t": "a1"}))])
    job_id = poll_job(bot)
    run_one(worker, bot, dy_mac.settings)
    assert dy_mac.claude.calls() == []                                          # no model
    [call] = dy_mac.node.calls()
    assert call["argv"][:3] == ["bin/dy.mjs", "buyin", "im-inbox"] and "--execute" not in call["argv"]
    assert "--names" in call["argv"] and f"--limit={platforms.IM_LIMIT}" in call["argv"]
    assert f"--expect-shop-id={SHOP}" in call["argv"] and not any(a.startswith("--after") for a in call["argv"])
    assert all(a.startswith("--") for a in call["argv"][3:])                    # every value --name=value
    assert (dy_mac.home / "platform" / "cli.lock").exists()                     # under the platform CLI lock
    # the bot took it: a 百应 draft job of her (masked), the poll job keeps only a summary, the cursor is kept
    [draft_job] = [j for j in bot.relay.store.list_jobs(20) if j["chat_id"] == BUYIN_CHAT + CID]
    assert "〈手机1〉" in draft_job["text"] and PHONE not in draft_job["text"]
    kept = bot.relay.store.get(job_id)["result_text"]
    assert PHONE not in kept and json.loads(kept)["summary"]["drafts"] == 1
    assert json.loads(bot.prm.store.meta_value("buyin_cursor")) == {"t": "a1"}
    for path in dy_mac.home.rglob("*"):                                          # nothing of her words on the Mac
        if path.is_file():
            assert PHONE not in path.read_text(encoding="utf-8", errors="replace"), path


def test_the_cursor_goes_back_as_after(worker, bot, dy_mac):  # noqa: F811
    dy_mac.node.rules([rule(["buyin", "im-inbox"], inbox(cursor={"t": "b2"}))])
    poll_job(bot, cursor={"t": "a1", "n": 3})
    run_one(worker, bot, dy_mac.settings)
    [call] = dy_mac.node.calls()
    assert '--after={"t":"a1","n":3}' in call["argv"]


def test_a_cli_without_the_command_is_unavailable_and_the_console_says_so(worker, bot, dy_mac):  # noqa: F811
    # douyin-engineering today: an unknown command is a usage error (exit 2, [USAGE] unknown command)
    dy_mac.node.rules([rule(["buyin", "im-inbox"], stderr='[USAGE] unknown command "buyin im-inbox"\nhint: …\n', exit=2)])
    job_id = poll_job(bot)
    run_one(worker, bot, dy_mac.settings)
    answer = json.loads(bot.relay.store.get(job_id)["result_text"])
    assert answer["stopped"]["verdict"] == "unavailable" and answer["summary"]["conversations"] == 0
    assert "百应私信读不了" in status(bot)["text"] and "buyin im-inbox" in status(bot)["text"]
    assert [j for j in bot.relay.store.list_jobs(20) if j["chat_id"].startswith(BUYIN_CHAT)] == []


@pytest.mark.parametrize("exit_code, verdict, words", [(77, "login", "退出码 77"), (76, "wall", "退出码 76"),
                                                       (79, "wrong_account", "退出码 79"), (2, "invalid", "不认")])
def test_login_risk_another_shop_and_a_usage_error_go_back_as_they_are(worker, bot, dy_mac,  # noqa: F811
                                                                       exit_code, verdict, words):
    dy_mac.node.rules([rule(["buyin", "im-inbox"], stderr=f"[TAG] 退出码 {exit_code} 的那一行\n", exit=exit_code)])
    job_id = poll_job(bot)
    run_one(worker, bot, dy_mac.settings)
    assert len(dy_mac.node.calls()) == 1                                         # tried once
    assert json.loads(bot.relay.store.get(job_id)["result_text"])["stopped"]["verdict"] == verdict
    assert words in status(bot)["text"]


def test_a_mac_without_the_douyin_cli_runs_nothing(worker, bot, fake_claude, node, tmp_path, workspace):  # noqa: F811
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace), **node.settings(dy=""))
    job_id = poll_job(bot)
    run_one(worker, bot, s)
    assert node.calls() == []
    assert json.loads(bot.relay.store.get(job_id)["result_text"])["stopped"]["verdict"] == "unavailable"


def test_a_buyin_draft_is_told_it_is_baiying_and_runs_no_preview_nor_upload(worker, bot, dy_mac):  # noqa: F811
    dy_mac.node.rules([rule(["buyin", "im-inbox"], inbox(said("m0", "邀约卡片：帽子专场", "shop", 3600, "card"),
                                                         said("m1", "佣金多少？")))])
    poll_job(bot)
    run_one(worker, bot, dy_mac.settings)                                        # the round
    dy_mac.claude.answers(platform_action=DESIGNATED, attachments_to_send=["assets/brief.txt"],
                          reply_draft="你好呀，佣金 20% 哦")
    run_one(worker, bot, dy_mac.settings)                                        # her draft
    [call] = dy_mac.claude.calls()
    prompt = call["prompt"]
    for expected in ("抖音百应", "不是微信", "店铺的微信在邀约卡片上", "不要填 platform_action", "小美的抖音",
                     "<creator_message>", "佣金多少？", "<operator_message>", "[卡片] 邀约卡片"):
        assert expected in prompt, expected
    assert len(dy_mac.node.calls()) == 1                                         # only the read: no preview
    [packet] = [p for p in bot.prm.store.packets(("pending",)) if p["tool"] == BUYIN_TOOL]
    assert packet["platform"] == {} and packet["attachments"] == [] and packet["text"] == "你好呀，佣金 20% 哦"
    assert "不带平台动作" in packet["reason"] and "不带附件" in packet["reason"]
    assert bot.relay.store.outfiles(packet["job_id"]) == []                     # nothing went back
    assert bot.texts == []


def test_the_buyin_job_runs_on_the_macs_system_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "dy" / "bin").mkdir(parents=True)
    (tmp_path / "dy" / "bin" / "dy.mjs").write_text(
        "import json, sys\n"
        "assert sys.argv[1:3] == ['buyin', 'im-inbox'] and '--execute' not in sys.argv, sys.argv\n"
        "print(json.dumps({'command': 'im-inbox', 'cursor': {'t': 1}, 'conversations': [{'conversationId': 'c1', "
        "'creator': {'uid': '5100000000000002', 'awemeId': 'xiaomei_dy', 'nickname': '小美'}, "
        "'messages': [{'id': 'm1', 'at': 1793237400, 'from': 'creator', 'kind': 'text', 'text': '你好'}]}]}))\n",
        encoding="utf-8")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(root / 'tools/claude_relay/worker.py')!r})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        "class Client:\n"
        "    def heartbeat(self, job_id, progress=None):\n"
        "        return {'cancel': False}\n"
        f"settings = {{'platforms': {{'dy': {str(tmp_path / 'dy')!r}, 'node_bin': sys.executable}}}}\n"
        "job = {'id': 7, 'kind': 'prm', 'meta': {'op': 'buyin_inbox', 'cursor': None, 'limit': 50}}\n"
        "out = w.do_job(settings, job, Client(), w.Stopper(), ('prm',))\n"
        "answer = json.loads(out['text'])\n"
        "assert out['ok'] and answer['op'] == 'buyin_inbox' and answer['stopped'] is None, out\n"
        "assert answer['conversations'][0]['messages'][0]['at'] == 1793237400.0, answer\n"
        "assert answer['cursor'] == {'t': 1}\n"
        "p = w.buyin_prompt({'text': '你好', 'meta': {'buyin_name': '小美', 'history': [{'who': 'operator', "
        "'text': '邀约'}]}}, 1, '小美', 'deals/1.md')\n"
        "assert '抖音百应' in p and '<operator_message>' in p\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")
