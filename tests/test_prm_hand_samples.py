"""手动建的寄样单（2026-11-05，用户：「我们怎么能第一时间得知发货」）：机器人每小时那一轮也请 Mac 看我们这个子账号最新的
寄样单（``meta.mine``，`jst samples --mine`），机器人自己建的单照旧；别的单有了运单号处理一次：达人的平台账号恰好对上一位
登记达人、合作单还在寄样这一步，就照「填运单号」填进她的合作单（发货通知一律等人审，不标已签收、不开专属佣金）；
对不上、对上两位、合作单过了寄样、合作单上是别的运单号、发了货没给运单号，就在「待审批」顶上出一行，到「知道了」为止；
第一次读到时已经发货的只记下不处理；30 天后忘掉。Mac 那一侧（`platforms.sample_statuses` 的 ``mine``、
`check_sample_result`、系统 Python 3.9）、机器人这一侧、控制台接口都在这里；页面在 tests/test_functional.py::TestPrmPages。
名字、号码、单号都是编的。"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from test_prm import CREATOR, Bot, Clock
from test_prm_console_api import call, running  # noqa: F401  (the running console's fixture)
from test_prm_platform_flow import system_jobs
from test_prm_sample_sync import EPOCH, HOUR, DAY, asked_in, next_round, syncing, verified_order

from platform_fakes import JST_ORDER, JST_SHOP_DY, JST_SHOP_XHS, JST_UID, FakeNode, jst_sample_row, jst_samples, rule
from src.prm import platforms
from src.prm.service import HAND_BASELINE, HAND_ERRORS, HAND_KEEP_DAYS, HAND_WAYBILL_WAIT_SEC, SAMPLE_GIVE_UP_DAYS
from src.prm.store import PrmStore

ROOT = Path(__file__).resolve().parent.parent
H1, H2, H3, H4 = "YP2611050000001", "YP2611050000002", "YP2611050000003", "YP2611050000004"
OTHER = "wxid_creator02"


# ── the Mac: `jst samples --mine` after the asked orders ───────────────────

@pytest.fixture
def node(tmp_path, monkeypatch):
    return FakeNode(tmp_path / "mac", monkeypatch)


@pytest.fixture
def jcfg(node):
    return platforms.config(node.settings(jst_shops={"dy": JST_SHOP_DY, "xhs": JST_SHOP_XHS}, jst_expect_uid=JST_UID,
                                          jst_logistics=["SF", "ZTO"]))


def test_ours_made_by_hand_are_read_after_the_asked_orders_without_a_receiver(node, jcfg):
    node.rules([rule(["--mine"], jst_samples(
                    jst_sample_row(JST_ORDER, "SENT"),                             # the bot's own: not ours by hand
                    jst_sample_row(H1, "WAITING_SEND"),
                    jst_sample_row(H2, "SENT", waybills=[("顺丰速运_电商", "SF0000000002", "2026-11-05 18:00:00")]))),
                rule(["samples"], jst_samples(jst_sample_row(JST_ORDER, "SENT")))])
    slept = []
    out = platforms.sample_statuses(jcfg, [JST_ORDER], sleep=slept.append, mine=True)
    calls = [c["argv"] for c in node.calls()]
    assert len(calls) == 2 and out["calls"] == 2 and slept == [platforms.SAMPLES_GAP_SEC]
    assert "--mine" not in calls[0] and calls[1][1:] == ["samples", "--mine", "--status=WAITING_SEND,SENT,SIGNED",
                                                         "--page-size=100", f"--expect-uid={JST_UID}"]
    assert list(out["orders"]) == [JST_ORDER] and sorted(out["mine"]) == [H1, H2] and out["mine_stopped"] is None
    h2 = out["mine"][H2]
    assert h2["daren"] == {"platform": "DY", "platform_no": "test_daren_01", "nickname": "测试达人"}
    assert h2["applied_at"] == platforms.jst_time("2026-10-26 10:00:00")
    assert h2["ships"] == [{"carrier": "顺丰速运_电商", "waybill": "SF0000000002", "sent_at": 1793872800.0}]
    checked = platforms.check_sample_result(json.loads(json.dumps(out)), [JST_ORDER])
    assert sorted(checked["mine"]) == [H1, H2] and checked["mine"][H2]["daren"]["platform_no"] == "test_daren_01"
    assert "测试主品" not in json.dumps(checked, ensure_ascii=False)          # the products are not kept


def test_without_mine_the_round_is_as_before(node, jcfg):
    node.rules([rule(["samples"], jst_samples(jst_sample_row(JST_ORDER, "SENT")))])
    out = platforms.sample_statuses(jcfg, [JST_ORDER], sleep=lambda s: None)
    assert len(node.calls()) == 1 and "mine" not in out and "mine_stopped" not in out
    checked = platforms.check_sample_result(json.loads(json.dumps(out)), [JST_ORDER])
    assert checked["mine"] is None and checked["mine_stopped"] is None          # (the bot: an old worker)


def test_a_cli_from_before_mine_leaves_the_asked_answer_and_says_so(node, jcfg):
    node.rules([rule(["--mine"], stderr="[USAGE] 不认识的参数：--mine\n", exit=2),
                rule(["samples"], jst_samples(jst_sample_row(JST_ORDER, "SENT")))])
    out = platforms.sample_statuses(jcfg, [JST_ORDER], sleep=lambda s: None, mine=True)
    assert list(out["orders"]) == [JST_ORDER] and out["stopped"] is None and out["mine"] is None
    assert out["mine_stopped"]["verdict"] == "unavailable" and "jushuitan-engineering" in out["mine_stopped"]["message"]
    checked = platforms.check_sample_result(json.loads(json.dumps(out)), [JST_ORDER])
    assert checked["mine"] is None and checked["mine_stopped"]["verdict"] == "unavailable"


@pytest.mark.parametrize("exit_code, stderr, verdict", [
    (77, "[NOT_LOGGED_IN] 聚水潭登录失效\n", "login"), (76, "[RISK] 请求太频繁啦\n", "wall"),
    (1, "[FAILED] fetch failed\n", "error")])
def test_ours_as_the_only_read_and_its_failures(node, jcfg, exit_code, stderr, verdict):
    node.rules([rule(["--mine"], stderr=stderr, exit=exit_code)])
    slept = []
    out = platforms.sample_statuses(jcfg, [], sleep=slept.append, mine=True)
    assert len(node.calls()) == 1 and slept == []                              # nothing asked: no pause first
    assert out["mine"] is None and out["mine_stopped"]["verdict"] == verdict and out["stopped"] is None


def test_a_stop_of_the_asked_orders_leaves_ours_unread(node, jcfg):
    node.rules([rule(["samples"], stderr="[NOT_LOGGED_IN] 聚水潭登录失效\n", exit=77)])
    out = platforms.sample_statuses(jcfg, [JST_ORDER], sleep=lambda s: None, mine=True)
    assert len(node.calls()) == 1 and out["stopped"]["verdict"] == "login"
    assert out["mine"] is None and out["mine_stopped"] == out["stopped"]
    nothing = platforms.sample_statuses(platforms.config(node.settings(jst="")), [], mine=True)
    assert nothing["mine"] is None and nothing["mine_stopped"]["verdict"] == "unavailable"


def test_the_bot_keeps_only_what_fits_of_ours():
    value = {"op": "sample_status", "orders": {JST_ORDER: {"status": "SENT"}}, "mine": {
        JST_ORDER: {"status": "SENT"},                                          # asked: the bot's own
        "XX0001": {"status": "SENT"},                                           # not an order number
        H1: {"status": "SENT", "ships": [{"carrier": "顺丰\n速运", "waybill": "SF0000000001"}, {"waybill": "bad bill"}],
             "applied_at": 1793872800.0, "daren": {"platform": "tb", "platform_no": "has space", "nickname": 7}},
        H2: {"status": "weird status", "applied_at": "yesterday",
             "daren": {"platform": "xhs", "platform_no": "5000000001", "nickname": "\n小\x00美\t" + "长" * 60}}}}
    checked = platforms.check_sample_result(value, [JST_ORDER])
    assert sorted(checked["mine"]) == [H1, H2]
    h1, h2 = checked["mine"][H1], checked["mine"][H2]
    assert h1["ships"] == [{"carrier": "顺丰 速运", "waybill": "SF0000000001", "sent_at": None}]
    assert h1["daren"] == {"platform": "", "platform_no": "", "nickname": "7"} and h1["applied_at"] == 1793872800.0
    assert h2["status"] == "" and h2["applied_at"] is None and h2["daren"]["platform"] == "XHS"
    assert h2["daren"]["platform_no"] == "5000000001"
    assert h2["daren"]["nickname"].startswith("小美长") and len(h2["daren"]["nickname"]) == platforms.SAMPLE_NICK_CHARS + 1
    odd = platforms.check_sample_result({"op": "sample_status", "mine_stopped": {"verdict": "boom", "exit_code": "x"}})
    assert odd["mine"] is None and odd["mine_stopped"] == {"verdict": "error", "exit_code": None, "message": ""}


def test_the_mine_read_runs_on_the_macs_system_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json, sys, pathlib\n"
        f"spec = importlib.util.spec_from_file_location('p', {str(ROOT / 'src/prm/platforms.py')!r})\n"
        "p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)\n"
        f"jst = pathlib.Path({str(tmp_path / 'jst')!r}); (jst / 'bin').mkdir(parents=True)\n"
        "(jst / 'bin' / 'jst.mjs').write_text('import json, sys\\nassert \"--mine\" in sys.argv\\nprint(json.dumps({'\n"
        "    '\"command\": \"samples\", \"rows\": [{\"orderNo\": \"YP2611050000001\", \"status\": \"SENT\", '\n"
        "    '\"applyTime\": \"2026-11-04 16:23:09\", \"daren\": {\"platform\": \"XHS\", \"platformNo\": \"5000000001\", '\n"
        "    '\"nickName\": \"x\"}, \"erpOrders\": [{\"logisticsCompany\": \"ZTO\", \"logisticsNo\": \"ZT0000000001\"}]}]}))\\n',"
        " encoding='utf-8')\n"
        "cfg = {'node': sys.executable, 'dirs': {'jst': jst}, 'env': {}, 'preview_timeout_sec': 30}\n"
        "out = p.sample_statuses(cfg, [], sleep=lambda s: None, mine=True)\n"
        "got = p.check_sample_result(json.loads(json.dumps(out)), [])\n"
        "h = got['mine']['YP2611050000001']\n"
        "assert h['daren'] == {'platform': 'XHS', 'platform_no': '5000000001', 'nickname': 'x'}, got\n"
        "assert h['ships'][0]['waybill'] == 'ZT0000000001' and h['applied_at'] == 1793780589.0, got\n"
        "print('ok')\n", encoding="utf-8")
    done = subprocess.run([python, str(script)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


# ── the bot: what a round of ours does ─────────────────────────────────────

@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bot(clock, tmp_path):
    b = syncing(Bot(clock, tmp_path))
    b.prm.register(CREATOR, "小美", "测试达人")
    b.directory = [{"wxid": CREATOR, "platform": "DY", "platform_no": "test_daren_01", "platform_uid": ""}]
    b.prm.creator_directory = lambda: list(b.directory)
    yield b
    b.prm.stop()
    b.relay.stop()


def ours(status="WAITING_SEND", waybill=None, *, account="test_daren_01", platform="DY", nickname="测试达人",
         carrier="顺丰速运_电商", applied=EPOCH, sent=None, signed=None):
    """One of our hand-made orders as the Mac's ``mine`` gives it."""
    return {"status": status, "ships": [{"carrier": carrier, "waybill": waybill, "sent_at": sent}] if waybill else [],
            "sent_at": sent, "signed_at": signed, "reason": "", "applied_at": applied,
            "daren": {"platform": platform, "platform_no": account, "nickname": nickname}}


def answer(b, job, mine=None, *, mine_stopped=None, orders=(), old_worker=False):
    """The worker takes the round's job and hands back the asked orders and ours made by hand."""
    taken = b.relay.next_job(0, ("prm",))
    assert taken["id"] == job["id"] and taken["meta"]["mine"] is True
    fields = {"orders": dict(orders)}
    if not old_worker:
        fields.update(mine=None if mine_stopped else dict(mine or {}), mine_stopped=mine_stopped)
    result = platforms.sample_result(taken["meta"]["orders"], **fields)
    assert b.relay.finish(job["id"], {"ok": True, "text": json.dumps(result, ensure_ascii=False), "duration_s": 1})
    b.relay.tick()


def a_round(b, clock, mine=None, advance=HOUR + 1, **kw):
    job = next_round(b, clock, advance)
    assert job is not None
    answer(b, job, mine, **kw)
    return job


def lines(b):
    return b.prm.snapshot()["hand_samples"]


def notices(b):
    return [p for p in b.prm.store.packets(limit=200) if p["kind"] == "shipping_notice"]


def deal(b, chat=CREATOR):
    return b.prm.store.deal_of(b.prm.store.party_by_wxid(chat, include_inactive=True)["id"])


def test_the_first_read_only_notes_what_was_shipped_then_a_waybill_goes_into_her_deal_once(bot, clock):
    job = a_round(bot, clock, {H1: ours("SENT", "SF0000000001"), H2: ours("WAITING_SEND")})
    assert asked_in(job) == [] and "手动建的单" in job["text"]
    assert bot.prm.store.hand_sample(H1)["state"] == "baseline"                 # shipped before we looked: left
    assert bot.prm.store.hand_sample(H2)["state"] == "waiting"
    assert bot.prm.store.meta_value(HAND_BASELINE) is not None
    assert deal(bot)["stage"] == "talk" and notices(bot) == [] and lines(bot) == []
    sent = EPOCH + DAY
    a_round(bot, clock, {H1: ours("SENT", "SF0000000001"), H2: ours("SENT", "SF0000000002", sent=sent)})
    d = deal(bot)
    shipment = bot.prm.store.latest_shipment(d["id"])
    assert d["stage"] == "shipped" and (shipment["carrier"], shipment["waybill"]) == ("顺丰速运_电商", "SF0000000002")
    [notice] = notices(bot)
    assert notice["status"] == "pending" and notice["route"] == "review" and "SF0000000002" in notice["text"]
    assert notice["meta"]["sample_order"] == H2 and H2 in notice["reason"]
    from src.prm.service import event_out
    filled = next(e for e in bot.prm.store.events(d["id"], limit=30) if e["kind"] == "shipment")
    assert filled["actor"] == "system" and event_out(filled)["name"] == "填运单号（聚水潭同步 · 手动建的单）"
    row = bot.prm.store.hand_sample(H2)
    assert (row["state"], row["party_id"], row["deal_id"], row["sent_at"]) == ("filled", d["party_id"], d["id"], sent)
    assert bot.prm.store.hand_sample(H1)["state"] == "baseline" and lines(bot) == []
    bot.prm.tick()
    assert bot.texts == []                                                      # the notice waits for a person
    a_round(bot, clock, {H2: ours("SIGNED", "SF0000000002", signed=EPOCH + 3 * DAY)})
    assert deal(bot)["stage"] == "shipped" and len(notices(bot)) == 1          # never 已签收 from a hand-made order
    assert system_jobs(bot) == []                                               # no 专属佣金 chain either


def test_nobody_registered_fits_a_line_until_it_is_seen_and_it_never_comes_back(bot, clock):
    a_round(bot, clock, {})                                                     # the first read: nothing yet
    a_round(bot, clock, {H3: ours("SENT", "ZT0000000003", account="other_daren", nickname="别人家的达人", carrier="中通",
                                  sent=EPOCH)})
    [line] = lines(bot)
    assert line == {"order_no": H3, "platform": "DY", "platform_label": "抖音", "nickname": "别人家的达人",
                    "party_name": "", "carrier": "中通", "waybill": "ZT0000000003", "sent_at": EPOCH,
                    "why": "没登记的达人"}
    assert notices(bot) == [] and deal(bot)["stage"] == "talk"
    assert bot.prm.hand_sample_seen(H3) == {"ok": True}
    assert lines(bot) == [] and bot.prm.hand_sample_seen(H3)["ok"] is False
    a_round(bot, clock, {H3: ours("SIGNED", "ZT0000000003", account="other_daren")})
    assert lines(bot) == []


@pytest.mark.parametrize("case, why", [
    ("two", "对得上 2 位登记的达人"), ("past", "小美 的合作单已经过了寄样这一步"), ("other_bill", "对不上"),
    ("stopped", "没登记的达人")])
def test_what_does_not_go_into_a_deal_is_a_line_saying_why(bot, clock, case, why):
    a_round(bot, clock, {})
    if case == "two":
        bot.prm.register(OTHER, "小丽", "测试达人二")
        bot.directory.append({"wxid": OTHER, "platform": "DY", "platform_no": "TEST_DAREN_01", "platform_uid": ""})
    elif case == "past":
        assert bot.prm.deal_action("stage", {"deal_id": deal(bot)["id"], "stage": "delivered"})["ok"]
    elif case == "other_bill":
        assert bot.prm.deal_action("shipment", {"deal_id": deal(bot)["id"], "carrier": "中通",
                                                "waybill": "ZT0000000009"})["ok"]
    else:
        assert bot.prm.set_active(bot.prm.store.party_by_wxid(CREATOR)["id"], False) is not None   # 停用
    before = len(notices(bot))
    a_round(bot, clock, {H4: ours("SENT", "SF0000000004", applied=None)})      # (made: when 聚水潭 says nothing)
    [line] = lines(bot)
    assert why in line["why"] and line["waybill"] == "SF0000000004"
    assert len(notices(bot)) == before                                          # no new shipping notice


def test_the_same_waybill_typed_in_already_is_left_as_it_is(bot, clock):
    a_round(bot, clock, {})
    clock.now += 60
    assert bot.prm.deal_action("shipment", {"deal_id": deal(bot)["id"], "carrier": "顺丰速运_电商",
                                            "waybill": "SF0000000004"})["ok"]
    before = len(notices(bot))
    a_round(bot, clock, {H4: ours("SENT", "SF0000000004", applied=None)})
    assert bot.prm.store.hand_sample(H4)["state"] == "filled" and lines(bot) == [] and len(notices(bot)) == before


def test_signed_at_first_sight_records_the_waybill_without_a_notice_and_never_signs_the_deal(bot, clock):
    a_round(bot, clock, {})
    a_round(bot, clock, {H4: ours("SIGNED", "SF0000000004", signed=EPOCH)})
    d = deal(bot)
    assert d["stage"] == "shipped" and bot.prm.store.latest_shipment(d["id"])["waybill"] == "SF0000000004"
    assert notices(bot) == [] and system_jobs(bot) == []


def test_sent_without_a_waybill_waits_a_little_then_is_a_line_and_a_late_waybill_still_counts(bot, clock):
    a_round(bot, clock, {H4: ours("WAITING_SEND")})
    a_round(bot, clock, {H4: ours("SENT")})                                     # 聚水潭 may give the waybill later
    assert lines(bot) == [] and bot.prm.store.hand_sample(H4)["state"] == "waiting"
    a_round(bot, clock, {H4: ours("SENT")}, advance=HAND_WAYBILL_WAIT_SEC + 1)
    [line] = lines(bot)
    assert "没给运单号" in line["why"] and line["waybill"] == "" and deal(bot)["stage"] == "talk"
    assert bot.prm.hand_sample_seen(H4)["ok"] is True
    a_round(bot, clock, {H4: ours("SENT", "SF0000000004")})                      # the waybill after all: into her deal
    d = deal(bot)
    assert d["stage"] == "shipped" and bot.prm.store.latest_shipment(d["id"])["waybill"] == "SF0000000004"
    assert bot.prm.store.hand_sample(H4)["state"] == "filled" and len(notices(bot)) == 1 and lines(bot) == []


def test_a_late_waybill_of_nobodys_order_brings_its_line_back_with_the_waybill(bot, clock):
    a_round(bot, clock, {H3: ours("WAITING_SEND", account="other_daren")})
    a_round(bot, clock, {H3: ours("SENT", account="other_daren")}, advance=HAND_WAYBILL_WAIT_SEC + HOUR)
    a_round(bot, clock, {H3: ours("SENT", account="other_daren")}, advance=HAND_WAYBILL_WAIT_SEC + HOUR)
    assert bot.prm.hand_sample_seen(H3)["ok"] is True and lines(bot) == []
    a_round(bot, clock, {H3: ours("SENT", "ZT0000000003", account="other_daren")})
    [line] = lines(bot)
    assert line["waybill"] == "ZT0000000003" and line["why"] == "没登记的达人"


def test_a_xiaohongshu_creator_is_matched_by_her_xiaohongshu_number(bot, clock):
    bot.prm.register(OTHER, "小丽", "小红书达人")
    bot.directory.append({"wxid": OTHER, "platform": "XHS", "platform_no": "5000000001", "platform_uid": ""})
    a_round(bot, clock, {})
    a_round(bot, clock, {H4: ours("SENT", "ZT0000000004", platform="XHS", account="5000000001", carrier="中通")})
    other = deal(bot, OTHER)
    assert other["stage"] == "shipped" and bot.prm.store.latest_shipment(other["id"])["waybill"] == "ZT0000000004"
    assert deal(bot)["stage"] == "talk" and lines(bot) == []
    a_round(bot, clock, {H3: ours("SENT", "ZT0000000003", platform="DY", account="5000000001")})
    assert [line["why"] for line in lines(bot)] == ["没登记的达人"]          # the same number on another platform


def test_the_bots_own_order_is_left_to_its_own_sync(bot, clock):
    order = verified_order(bot.prm, number=JST_ORDER)
    bot.prm.store.add_sample_order(JST_ORDER, packet_id=order["id"], deal_id=order["deal_id"],      # kept, signed for:
                                   party_id=order["party_id"], made_at=clock.now, done="signed")  # not asked about
    a_round(bot, clock, {})
    job = a_round(bot, clock, {JST_ORDER: ours("SENT", "SF0000000001")})
    assert asked_in(job) == [] and bot.prm.store.sample_order(JST_ORDER) is not None
    assert bot.prm.store.hand_sample(JST_ORDER) is None and lines(bot) == []


def test_an_order_made_long_ago_is_left_alone(clock, tmp_path):
    clock.now = EPOCH + SAMPLE_GIVE_UP_DAYS * DAY + 10 * DAY                 # (a clock 聚水潭's times compare with)
    b = syncing(Bot(clock, tmp_path))
    try:
        a_round(b, clock, {})
        a_round(b, clock, {H4: ours("SENT", "SF0000000004", account="other_daren", applied=EPOCH),   # 70 days ago
                           H3: ours("SENT", "ZT0000000003", account="other_daren", applied=clock.now - DAY)})
        assert b.prm.store.hand_sample(H4) is None and [line["order_no"] for line in lines(b)] == [H3]
    finally:
        b.prm.stop()
        b.relay.stop()


def test_why_ours_were_not_read_is_said_on_the_page_until_a_round_reads_them(bot, clock):
    a_round(bot, clock, old_worker=True)
    assert "旧版本" in bot.prm.snapshot()["hand_samples_problem"]
    a_round(bot, clock, {})
    assert bot.prm.snapshot()["hand_samples_problem"] == ""
    a_round(bot, clock, mine_stopped={"verdict": "unavailable", "exit_code": 2,
                                      "message": "这台 Mac 的聚水潭命令行太旧，没有 samples --mine"})
    assert "太旧" in bot.prm.snapshot()["hand_samples_problem"]
    a_round(bot, clock, {})
    hiccup = {"verdict": "error", "exit_code": 1, "message": "fetch failed"}
    for n in range(1, HAND_ERRORS + 1):
        a_round(bot, clock, mine_stopped=hiccup)
        said = bot.prm.snapshot()["hand_samples_problem"]
        assert (said == "") if n < HAND_ERRORS else (f"连续 {HAND_ERRORS} 次" in said)
    bot.prm._sample_sync_sec = 0
    assert bot.prm.snapshot()["hand_samples_problem"] == ""                     # the sync off: nothing said


def test_a_hand_made_order_is_forgotten_after_thirty_days(bot, clock):
    a_round(bot, clock, {})
    a_round(bot, clock, {H3: ours("SENT", "ZT0000000003", account="other_daren")})
    assert len(lines(bot)) == 1
    a_round(bot, clock, {}, advance=HAND_KEEP_DAYS * DAY + 1)
    assert lines(bot) == [] and bot.prm.store.hand_sample(H3) is None


def test_a_restart_keeps_what_was_noted_and_done(clock, tmp_path):
    from test_prm_sample_sync import FileBot

    b = FileBot(clock, tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    a_round(b, clock, {H1: ours("SENT", "SF0000000001"), H2: ours("WAITING_SEND", account="other_daren")})
    b.prm.stop()
    b.relay.stop()
    again = FileBot(clock, tmp_path)
    try:
        a_round(again, clock, {H1: ours("SENT", "SF0000000001"), H2: ours("SENT", "SF0000000002", account="other_daren")})
        assert again.prm.store.hand_sample(H1)["state"] == "baseline"           # not taken for a new first read
        assert [line["order_no"] for line in lines(again)] == [H2]
    finally:
        again.prm.stop()
        again.relay.stop()


# ── the console: GET /api/prm, POST /api/prm/hand-samples ─────────────────

def _listed(store, number=H3, now=None):
    now = 1793872800.0 if now is None else now
    store.add_hand_sample(number, platform="XHS", account="5000000009", nickname="测试达人九", applied_at=now,
                          status="SENT", state="waiting", now=now)
    store.update_hand_sample(number, now, state="listed", why="没登记的达人", carrier="中通", waybill="ZT0000000003",
                             sent_at=now)


def test_the_running_console_shows_the_lines_and_sees_to_one(running):  # noqa: F811
    _listed(running.prm.store, now=running.prm._clock())
    d = call("/api/prm")[1]
    assert [h["order_no"] for h in d["hand_samples"]] == [H3] and d["hand_samples"][0]["platform_label"] == "小红书"
    assert d["hand_samples_problem"] == ""
    assert call("/api/prm/hand-samples", "POST", {"order_no": H3})[1] == {"ok": True}
    assert call("/api/prm")[1]["hand_samples"] == []
    assert call("/api/prm/hand-samples", "POST", {"order_no": H3})[1]["ok"] is False
    assert call("/api/prm/hand-samples", "POST", {"order_no": ""})[1]["ok"] is False
    assert call("/api/prm/hand-samples", "POST", {"order_no": H3, "action": "drop"})[1]["ok"] is False


def test_the_hand_sample_endpoint_is_post_only_and_refuses_other_sites(running):  # noqa: F811
    from test_web_api import _build_handler

    _listed(running.prm.store, now=running.prm._clock())
    assert call("/api/prm/hand-samples")[0] == 405
    raw = json.dumps({"order_no": H3}).encode()
    _, sock = _build_handler("/api/prm/hand-samples", method="POST", body=raw,
                             headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert sock.get_response_text().split("\r\n", 1)[0].split()[1] == "403"
    assert len(call("/api/prm")[1]["hand_samples"]) == 1


def test_a_stopped_bot_shows_the_lines_and_writes_a_seen_one_into_the_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    assert call("/api/prm/hand-samples", "POST", {"order_no": H3})[1]["ok"] is False   # no database yet
    db = tmp_path / "data" / "prm.db"
    db.parent.mkdir()
    store = PrmStore(db)
    _listed(store)
    store.close()
    d = call("/api/prm")[1]
    assert d["running"] is False and [h["order_no"] for h in d["hand_samples"]] == [H3]
    assert "hand_samples_problem" not in d
    assert call("/api/prm/hand-samples", "POST", {"order_no": H3})[1] == {"ok": True}
    assert call("/api/prm")[1]["hand_samples"] == []
