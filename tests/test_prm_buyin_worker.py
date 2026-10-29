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

from platform_fakes import DY_UID, catalog, dy_designated_preview, rule
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
    # a Mac where the action's preview would run (the catalogue and the CLI's answer are there)
    (dy_mac.workspace / "kb" / "products.json").write_text(json.dumps(catalog(), ensure_ascii=False), encoding="utf-8")
    dy_mac.node.rules([rule(["buyin", "im-inbox"], inbox(said("m0", "邀约卡片：帽子专场", "shop", 3600, "card"),
                                                         said("m1", "佣金多少？"))),
                       rule(["promotion-strategy-create-designated"], dy_designated_preview(), execute=False)])
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
    assert [c["argv"][1:3] for c in dy_mac.node.calls()] == [["buyin", "im-inbox"]]   # only the read: no preview
    [packet] = [p for p in bot.prm.store.packets(("pending",)) if p["tool"] == BUYIN_TOOL]
    sent_back = json.loads(bot.relay.store.get(packet["job_id"])["result_text"])
    assert "platform" not in sent_back and sent_back["platform_action"]["action"] == DESIGNATED["action"]   # unrun
    assert sent_back["attachments_to_send"] == [] and sent_back["attachments_named"] == ["assets/brief.txt"]
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
        "'messages': [{'id': 'm1', 'at': 1793237400, 'from': 'creator', 'kind': 'text', 'text': '你好'}]}, "
        "{'conversationId': 'c2', 'creator': {'buyinId': '1777000000000000042'}, 'messages': ["
        f"{{'id': '9001', 'at': {int((T0 + 4) * 1000)}, 'from': 'shop', 'kind': 'card', "
        f"'card': 'alliance_invite_card', 'text': {INVITE_TEXT!r}}}, "
        f"{{'id': '9002', 'at': {int((T0 + 60) * 1000)}, 'from': 'creator', 'kind': 'contact', "
        f"'contact': {{'wechat': {WECHAT!r}, 'phone': {SHOP_PHONE!r}}}}}]}}]}}))\n",
        encoding="utf-8")
    invite_ledger(tmp_path / "home" / "platform" / "creator-invites", ("test_daren_01", T0, True))
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(root / 'tools/claude_relay/worker.py')!r})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        "class Client:\n"
        "    def heartbeat(self, job_id, progress=None):\n"
        "        return {'cancel': False}\n"
        f"settings = {{'platforms': {{'dy': {str(tmp_path / 'dy')!r}, 'node_bin': sys.executable, "
        f"'dy_invite_phone': {SHOP_PHONE!r}}}}}\n"
        "job = {'id': 7, 'kind': 'prm', 'meta': {'op': 'buyin_inbox', 'cursor': None, 'limit': 50}}\n"
        "out = w.do_job(settings, job, Client(), w.Stopper(), ('prm',))\n"
        "answer = json.loads(out['text'])\n"
        "assert out['ok'] and answer['op'] == 'buyin_inbox' and answer['stopped'] is None, out\n"
        "assert answer['conversations'][0]['messages'][0]['at'] == 1793237400.0, answer\n"
        "assert answer['cursor'] == {'t': 1} and w.memory_only(job)\n"
        "c2 = answer['conversations'][1]\n"
        "assert c2['creator']['aweme_id'] == 'test_daren_01' and c2['invited']['plan_id'] == " + repr(PLAN) + ", c2\n"
        "assert c2['messages'][1]['contact'] == {'wechat': " + repr(WECHAT) + "}, c2\n"
        "p = w.buyin_prompt({'text': '你好', 'meta': {'buyin_name': '小美', 'history': [{'who': 'operator', "
        "'text': '邀约'}]}}, 1, '小美', 'deals/1.md')\n"
        "assert '抖音百应' in p and '<operator_message>' in p\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")


# ── 真的 `dy buyin im-inbox`（2026-10-29）：联系方式卡片、邀约台账认人、结果不进 outbox ───────────────
# 命令行的真实形状：creator 只有 buyinId（页面没有抖音号 / UID），at 是毫秒，我方的邀约卡是 kind card、card
# alliance_invite_card，达人的联系方式卡片是 kind contact、contact 带完整的微信号 / 手机号。下面的号码、编号、名字都是编的。

from datetime import datetime, timedelta, timezone  # noqa: E402

import logging  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
T0 = 1793237400.0                                    # when the card went out (the ledger's result)
INVITE_TEXT = "你好呀～我们是测试帽子小店，秋冬新款帽子专场想邀请你带货，佣金 25%，样品包邮寄给你，有兴趣回我一下哦"
WECHAT = "zq_test_wx_83"
CARD_PHONE = "13700001234"
SHOP_WECHAT = "shop_test_wx1"                        # this Mac's own contact on the invite card (config.json)
SHOP_PHONE = "13600000000"
PLAN = "PTEST-plan-0001"


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, BEIJING).isoformat(timespec="seconds")


def invite_ledger(folder: Path, *sent, plan=PLAN, content=INVITE_TEXT, spare=(), write_plan=True) -> Path:
    """The batch invite tool's ledger and plan: one ``attempt`` + ``result`` per (抖音号, result time, sent)."""
    (folder / "plans").mkdir(parents=True, exist_ok=True)
    rows = []
    for n, (aweme, at, ok) in enumerate(sent):
        rows += [{"at": iso(at - 6), "event": "attempt", "attempt": f"att{n:02d}", "plan_id": plan, "aweme_id": aweme,
                  "products": ["3000000000000000101", "3000000000000000102"], "commission_rate": 25,
                  "category": "small"},
                 {"at": iso(at), "event": "result", "attempt": f"att{n:02d}", "plan_id": plan, "aweme_id": aweme,
                  "exit": 0, "outcome": "VERIFIED_SUCCESS", "platform_writes": 1, "sent": ok, "timed_out": False}]
    (folder / "invites.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + '{"cut',
                                          encoding="utf-8")
    if write_plan:
        entries = [{"aweme_id": aweme, "content": content} for aweme, _, _ in sent]
        (folder / "plans" / f"plan-{plan}.json").write_text(json.dumps(
            {"kind": "creator-invite-plan", "plan_id": plan, "creators": entries[:1],
             "spare": [*entries[1:], *spare]}, ensure_ascii=False), encoding="utf-8")
    return folder


def invite_card(mid, at, text=INVITE_TEXT, ms=True) -> dict:
    return {"id": mid, "at": int(at * 1000) if ms else int(at), "from": "shop", "kind": "card",
            "card": "alliance_invite_card", "text": text}


def contact_card(mid, at, contact=None) -> dict:
    return {"id": mid, "at": int(at * 1000), "from": "creator", "kind": "contact", "text": "[联系方式卡片]",
            "cardId": "88000000001", "contact": {"wechat": WECHAT, "phone": CARD_PHONE} if contact is None else contact}


def real_conv(cid, *messages, buyin="1777000000000000042", nickname="测试达人") -> dict:
    """One conversation as the real CLI prints it (no 抖音号, no UID)."""
    return {"conversationId": cid, "creator": {"buyinId": buyin, "nickname": nickname}, "messages": list(messages)}


def parsed(*convs) -> list:
    return [platforms.im_conversation(c) for c in convs]


class Warnings:
    def __init__(self):
        self.lines = []

    def __call__(self, fmt, *args):
        self.lines.append(fmt % args)


def test_a_conversation_is_our_invite_when_one_card_fits_one_ledger_result(tmp_path):
    ledger = invite_ledger(tmp_path / "inv", ("test_daren_01", T0, True), ("test_daren_02", T0 + 900, True))
    convs = parsed(real_conv("7300000000000000001", invite_card("9001", T0 + 4)),
                   real_conv("7300000000000000002", invite_card("9002", T0 + 903.4, "  " + INVITE_TEXT[:40] + "\n…")),
                   real_conv("7300000000000000003", {"id": "9003", "at": int(T0 * 1000), "from": "creator",
                                                     "kind": "text", "text": INVITE_TEXT}))   # not a card of ours
    warn = Warnings()
    assert platforms.invite_match(ledger, convs, warn=warn) == 2 and warn.lines == []
    assert convs[0]["creator"]["aweme_id"] == "test_daren_01" and convs[1]["creator"]["aweme_id"] == "test_daren_02"
    assert convs[0]["invited"] == {"aweme_id": "test_daren_01", "invited_at": iso(T0 - 6), "plan_id": PLAN,
                                   "attempt": "att00", "category": "small",
                                   "products": ["3000000000000000101", "3000000000000000102"], "commission_rate": 25}
    assert convs[2]["invited"] is None and convs[2]["creator"]["aweme_id"] == ""
    # the ledger as its folder or the file itself; seconds or milliseconds on the card
    again = parsed(real_conv("7300000000000000001", invite_card("9001", T0 + 4, ms=False)))
    assert platforms.invite_match(ledger / "invites.jsonl", again) == 1 and again[0]["invited"]["attempt"] == "att00"
    # her 抖音号 already known: kept
    known = parsed(real_conv("7300000000000000001", invite_card("9001", T0 + 4)))
    known[0]["creator"]["aweme_id"] = "already_known"
    assert platforms.invite_match(ledger, known) == 1
    assert known[0]["creator"]["aweme_id"] == "already_known" and known[0]["invited"]["aweme_id"] == "test_daren_01"


@pytest.mark.parametrize("offset, matched", [(60, 1), (-60, 1), (61, 0), (-61, 0)])
def test_only_a_card_within_60_seconds_of_the_result_fits(tmp_path, offset, matched):
    ledger = invite_ledger(tmp_path / "inv", ("test_daren_01", T0, True))
    convs = parsed(real_conv("c1", invite_card("9001", T0 + offset)))
    assert platforms.invite_match(ledger, convs) == matched
    assert (convs[0]["invited"] is not None) == bool(matched)
    assert platforms.INVITE_MATCH_SEC == 60


def test_two_ways_to_pair_them_are_none(tmp_path):
    # the same invite text to two creators 30 s apart: her card fits both results
    ledger = invite_ledger(tmp_path / "a", ("test_daren_01", T0, True), ("test_daren_02", T0 + 30, True))
    convs = parsed(real_conv("c1", invite_card("9001", T0 + 15)))
    assert platforms.invite_match(ledger, convs) == 0 and convs[0]["invited"] is None
    assert convs[0]["creator"]["aweme_id"] == ""
    # one result, two conversations with a card near it: that result fits both
    ledger = invite_ledger(tmp_path / "b", ("test_daren_01", T0, True))
    convs = parsed(real_conv("c1", invite_card("9001", T0 + 3)), real_conv("c2", invite_card("9002", T0 + 5)))
    assert platforms.invite_match(ledger, convs) == 0 and [c["invited"] for c in convs] == [None, None]
    # and one of them fitting another result too: still none of the two (the shared result spoils both)
    ledger = invite_ledger(tmp_path / "c", ("test_daren_01", T0, True), ("test_daren_02", T0 + 100, True))
    convs = parsed(real_conv("c1", invite_card("9001", T0 + 3)),
                   real_conv("c2", invite_card("9002", T0 + 50), invite_card("9003", T0 + 101)))
    assert platforms.invite_match(ledger, convs) == 0


def test_another_text_no_content_or_a_card_not_sent_is_no_match(tmp_path):
    ledger = invite_ledger(tmp_path / "a", ("test_daren_01", T0, True))
    assert platforms.invite_match(ledger, parsed(real_conv("c1", invite_card("9001", T0 + 4, "另一张卡的字")))) == 0
    ledger = invite_ledger(tmp_path / "b", ("test_daren_01", T0, True), content="  ")
    assert platforms.invite_match(ledger, parsed(real_conv("c1", invite_card("9001", T0 + 4, "  ")))) == 0
    for sent in (False, None):
        ledger = invite_ledger(tmp_path / f"s{sent}", ("test_daren_01", T0, sent))
        assert platforms.invite_match(ledger, parsed(real_conv("c1", invite_card("9001", T0 + 4)))) == 0
    # a short invite text: the whole of it must be on the card
    ledger = invite_ledger(tmp_path / "short", ("test_daren_01", T0, True), content="想邀请你带货")
    assert platforms.invite_match(ledger, parsed(real_conv("c1", invite_card("9001", T0 + 4, "想邀请你带货～")))) == 1
    assert platforms.invite_match(ledger, parsed(real_conv("c1", invite_card("9001", T0 + 4, "想邀请你")))) == 0
    # the creator in the plan's spare list is found too; no ledger given: nothing is looked at
    ledger = invite_ledger(tmp_path / "spare", ("test_daren_09", T0 + 2000, True), ("test_daren_01", T0, True))
    assert platforms.invite_match(ledger, parsed(real_conv("c1", invite_card("9001", T0 + 4)))) == 1
    assert platforms.invite_match("", parsed(real_conv("c1", invite_card("9001", T0 + 4)))) == 0


def test_a_missing_plan_or_an_unreadable_ledger_is_no_match_and_a_warning_without_values(tmp_path):
    convs = parsed(real_conv("c1", invite_card("9001", T0 + 4)))
    warn = Warnings()
    ledger = invite_ledger(tmp_path / "a", ("test_daren_01", T0, True), write_plan=False)
    assert platforms.invite_match(ledger, convs, warn=warn) == 0
    broken = tmp_path / "b"
    (broken / "invites.jsonl").mkdir(parents=True)                               # cannot be read as a file
    assert platforms.invite_match(broken, convs, warn=warn) == 0
    assert platforms.invite_match(tmp_path / "nowhere", convs, warn=warn) == 0
    assert len(warn.lines) == 3 and convs[0]["invited"] is None
    for said in warn.lines:
        for value in ("test_daren_01", PLAN, INVITE_TEXT[:10], str(tmp_path)):
            assert value not in said, (value, said)
    # no invite card in this round: the ledger is not even read, nothing to warn of
    quiet = Warnings()
    assert platforms.invite_match(broken, parsed(real_conv("c1", contact_card("9002", T0))), warn=quiet) == 0
    assert quiet.lines == []


def test_the_shops_own_contact_is_taken_out_of_her_cards():
    cfg = {"dy_invite_wechat": SHOP_WECHAT, "dy_invite_phone": SHOP_PHONE}
    convs = parsed(real_conv("c1", contact_card("1", T0, {"wechat": SHOP_WECHAT.upper(), "phone": CARD_PHONE}),
                             contact_card("2", T0 + 1, {"wechat": SHOP_WECHAT}),
                             contact_card("3", T0 + 2, {"phone": "+86 136-0000-0000"}),
                             contact_card("4", T0 + 3), contact_card("5", T0 + 4, {"unavailable": True, "why": "older"})))
    assert platforms.im_strip_own(convs, cfg) == 3
    assert [m["contact"] for m in convs[0]["messages"]] == [
        {"phone": CARD_PHONE}, {"unavailable": True, "why": "own"}, {"unavailable": True, "why": "own"},
        {"wechat": WECHAT, "phone": CARD_PHONE}, {"unavailable": True, "why": "older"}]
    assert platforms.im_strip_own(convs, {}) == 0


@pytest.mark.parametrize("code, verdict, words", [
    ("NO_CHAT_TAB", "unavailable", "没开百应「精选联盟私信」页"),
    ("CHAT_OFFLINE", "error", "私信连接断了"),
    ("PAGE_CHANGED", "unavailable", "代码变了")])
def test_the_commands_own_failures_say_what_to_do(worker, bot, dy_mac, code, verdict, words):  # noqa: F811
    dy_mac.node.rules([rule(["buyin", "im-inbox"], stderr=f"[{code}] the chat page is not usable; nothing was read\n"
                                                          "hint: open it\n", exit=1)])
    job_id = poll_job(bot)
    run_one(worker, bot, dy_mac.settings)
    stopped = json.loads(bot.relay.store.get(job_id)["result_text"])["stopped"]
    assert stopped["verdict"] == verdict and words in stopped["message"] and stopped["exit_code"] == 1
    if verdict == "unavailable":                                                  # said at once
        assert words in status(bot)["text"]


@pytest.fixture
def invited_mac(bot, fake_claude, node, tmp_path, workspace):          # noqa: F811
    """A Mac whose config names the shop's own contact; the invite ledger in its default place; the creator on the
    达人名单 by her 抖音号 only (the page never gives it)."""
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace),
                 **node.settings(dy_expect_shop_id=SHOP, dy_invite_wechat=SHOP_WECHAT, dy_invite_phone=SHOP_PHONE))
    invite_ledger(tmp_path / "home" / "platform" / "creator-invites", ("test_daren_01", T0, True),
                  ("test_daren_02", T0 + 3600, True))
    bot.prm.creator_directory = lambda: [{"wxid": CREATOR, "platform": "DY", "platform_no": "TEST_DAREN_01",
                                          "platform_uid": ""}]
    return SimpleNamespace(settings=s, node=node, claude=fake_claude, home=tmp_path / "home", workspace=workspace)


def test_the_worker_matches_the_ledger_strips_our_contact_and_never_writes_the_answer(worker, bot, invited_mac,  # noqa: F811
                                                                                    monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    events = []
    bot.prm.on_buyin_contact = lambda e: events.append(e) or {"id": 5, "state": "pending", "masked": {}}
    now = time.time()
    invited_mac.node.rules([rule(["buyin", "im-inbox"], {
        "site": "buyin", "command": "im-inbox", "cursor": {"v": 1, "t": int(now * 1000)},
        "conversations": [
            real_conv("7300000000000000001", invite_card("9001", T0 + 4),
                      {"id": "9002", "at": int((now - 60) * 1000), "from": "creator", "kind": "text", "text": "好的呀"},
                      contact_card("9003", now - 30, {"wechat": WECHAT, "phone": SHOP_PHONE})),
            real_conv("7300000000000000002", invite_card("9004", T0 + 3603), contact_card("9005", now - 20),
                      buyin="1777000000000000043", nickname="没登记的达人"),
            real_conv("7300000000000000003", contact_card("9006", now - 10), buyin="1777000000000000044")],
        "summary": {"messages": 6}, "page": {"websocket": "open"}, "requests": 2})])
    delivered, sent = [], []
    real_deliver, real_send = worker.deliver, worker._send_until_taken
    monkeypatch.setattr(worker, "deliver", lambda c, j, r, st: delivered.append(j) or real_deliver(c, j, r, st))
    monkeypatch.setattr(worker, "_send_until_taken",
                        lambda c, j, r, st, path, **k: sent.append((j, path)) or real_send(c, j, r, st, path, **k))
    job_id = poll_job(bot)
    run_one(worker, bot, invited_mac.settings)
    assert delivered == [] and sent == [(job_id, None)]                         # from memory, never the outbox
    assert not (invited_mac.home / "outbox").exists() or list((invited_mac.home / "outbox").iterdir()) == []
    # her 抖音号 from the ledger: she is the registered creator; the other invite is ours but not registered
    [draft_job] = [j for j in bot.relay.store.list_jobs(20) if j["chat_id"] == BUYIN_CHAT + "7300000000000000001"]
    assert "[联系方式卡片] 微信号 zq****83（加微信 #5）" in draft_job["text"] and SHOP_PHONE not in draft_job["text"]
    assert [(e["conversation_id"], e["party_id"] is not None, e["contact"]["wechat"], e["contact"]["phone"])
            for e in events] == [("7300000000000000001", True, WECHAT, ""), ("7300000000000000002", False, WECHAT,
                                                                             CARD_PHONE)]
    assert events[0]["invited"]["aweme_id"] == "test_daren_01" and events[1]["invited"]["aweme_id"] == "test_daren_02"
    assert events[1]["creator"] == {"aweme_id": "test_daren_02", "uid": "1777000000000000043", "nickname": "没登记的达人"}
    snap = bot.prm.snapshot()["buyin"]
    assert snap["others_contacts"] == 1
    assert sorted(str((u["invited"] or {}).get("aweme_id")) for u in snap["unmatched"]) == ["None", "test_daren_02"]
    # counts only on the Mac: the log, and nothing of the numbers anywhere in its folder
    assert "3 conversation(s), 6 message(s), 3 contact card(s), 2 matched to an invite" in caplog.text
    for value in (WECHAT, CARD_PHONE, "好的呀"):
        assert value not in caplog.text
        for path in invited_mac.home.rglob("*"):
            if path.is_file():
                assert value not in path.read_text(encoding="utf-8", errors="replace"), path
    # the bot kept counts of that answer only
    kept = [j for j in bot.relay.store.list_jobs(20) if j["chat_id"] == BUYIN_SYNC_CHAT][0]["result_text"]
    assert WECHAT not in kept and CARD_PHONE not in kept


def test_other_jobs_still_go_through_the_outbox(worker, bot, dy_mac, monkeypatch):  # noqa: F811
    from test_prm_worker import creator_says

    delivered = []
    real_deliver = worker.deliver
    monkeypatch.setattr(worker, "deliver", lambda c, j, r, st: delivered.append(j) or real_deliver(c, j, r, st))
    job_id = creator_says(bot, "在吗")
    run_one(worker, bot, dy_mac.settings)
    assert delivered == [job_id]                                                 # a draft's result: outbox first
    assert worker.memory_only({"kind": "prm", "meta": {"op": "buyin_inbox"}})
    assert not worker.memory_only({"kind": "prm", "meta": {"op": "sample_status"}})
    assert not worker.memory_only({"kind": "admin", "meta": {"op": "buyin_inbox"}})


def test_a_memory_only_result_is_sent_again_until_taken_and_lost_when_the_worker_stops(worker, caplog):
    class Client:
        def __init__(self, answers):
            self.answers, self.sent = list(answers), []

        def send_result(self, job_id, result):
            self.sent.append(job_id)
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    def stopper(stopped):
        return SimpleNamespace(event=SimpleNamespace(wait=lambda seconds: stopped))

    client = Client([OSError("bot down"), False, True])
    assert worker.deliver_from_memory(client, 7, {"ok": True, "text": WECHAT}, stopper(False)) and client.sent == [7] * 3
    caplog.set_level(logging.INFO)
    gone = Client([OSError("bot down")])
    assert not worker.deliver_from_memory(gone, 8, {"ok": True, "text": WECHAT}, stopper(True))
    assert "never written to the outbox" in caplog.text and WECHAT not in caplog.text
