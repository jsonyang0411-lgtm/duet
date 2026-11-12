"""接手任何合作群 (2026-11-09) end to end: the admin links a cooperation group nobody's scan sees (her 商务 made it with her
and us) to the creator from his own WeChat chat (「接群」, 「接群 1」, 「是 1」), with PRM_BOSS_GROUPS off.  Each part has its
own tests (the backend: tests/test_group_link_backend.py; PRM and the admin's words on a fake backend:
tests/test_group_link.py); here they run together.

The REAL WcdbBackend over the fake WeChat 4 database, the real router and route steps, the relay, PrmService and the
console's API, wired as Bot.run wires them (tests/test_boss_groups_e2e.py ``World`` / ``Run``; the admin handler also
gets the PRM's 接群 commands, as Bot._wire_friend_adds does).  The admin's lines are read from his chat and the bot's
answers go out through WeChat's window, as every admin command's do.  Every name and id is made up.
"""

import json

import pytest

from router_support import ADMIN
from test_boss_groups_e2e import GROUPS, HER, HER_NAME, ME_SENDER, NOTICE_GROUP, World, approve, mac_writes
from test_prm_catchup import text_lines

from src.web import server
from src.wechat import wcdb_backend

BD, BD_NAME = "wxid_bd0000000001", "测试商务"
LINKED = "500000001@chatroom"                # the group her 商务 made with her and us, long before
LINKED_NAME = "小美合作对接群"
ADMIN_NAME = "管理员测试"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wcdb_backend, "VERIFY_PAUSE_SEC", 0)
    w = World(tmp_path)
    t = w.clock.now
    w.db.names.update({BD: BD_NAME, ADMIN: ADMIN_NAME})
    w.db.say(ADMIN, ADMIN, "早", t - 9000)                               # the admin's chat (PRIVATE_CHATS)
    w.db.group(LINKED, LINKED_NAME, [HER, BD, ME_SENDER], t - 3 * 86400)
    w.db.line(LINKED, HER, "你们家帽子多少钱", t - 3000)
    w.db.line(LINKED, ME_SENDER, "两三百～我发您看看款式", t - 2900)        # an operator, by hand, before the link
    w.db.line(LINKED, BD, "她这周想先看样", t - 2000)
    w.db.line(LINKED, HER, "那寄样怎么弄呀", t - 900)                       # nobody answered her yet
    yield w
    w.stop()


def started(world, **settings):
    """A run with PRM_BOSS_GROUPS off, the admin handler wired with the PRM's 接群 commands."""
    run = world.run(on=False, **settings)
    run.router._admin.set_group_links(run.prm.group_links)
    return run


def admin_says(world, run, text):
    """The admin writes in his own chat; the next poll reads it and the bot answers there: the answer."""
    world.clock.now += 5
    world.db.say(ADMIN, ADMIN, text, world.clock.now)
    before = len(world.window.typed)
    run.poll()
    answers = [said for name, said in world.window.typed[before:] if name == ADMIN_NAME]
    assert len(answers) == 1, world.window.typed[before:]
    return answers[0]


@GROUPS
def test_the_admin_links_a_group_by_hand_and_her_waiting_line_is_one_draft(world, groups):
    run = started(world, groups=groups)
    run.poll()
    assert run.lines(LINKED) == [] and world.jobs() == []                 # before: silent, nothing drafted
    listed = admin_says(world, run, "接群").split("\n")
    assert listed[0].startswith("最近有动静、还没接手的群") and listed[1].startswith(f"1. {LINKED_NAME} · 3 人 · ")
    question = admin_says(world, run, "接群 1").split("\n")
    assert question == [f"群「{LINKED_NAME}」（3 人）里谁是达人？回「是 编号」，「不接」作罢（30 分钟内有效）：",
                        f"1. {HER_NAME}", f"2. {BD_NAME}"]
    shown = server._prm.snapshot()["boss_groups"]                        # (the console's card shows it too)
    assert shown["enabled"] is False and [m["wxid"] for m in shown["manual"][0]["members"]] == [HER, BD]
    answer = admin_says(world, run, "是 1")
    party = world.prm_store.party_by_wxid(HER)
    deal = world.prm_store.deal_of(party["id"])
    assert answer == (f"接手了：群「{LINKED_NAME}」→ {HER_NAME}（新登记的达人），合作单 {deal['id']} 号；之后她在群里说的话会起草，"
                      "在「待审批」里等你看；群里最近 3 句记成了背景；她最后 1 句还没人回，派了一份草稿")
    assert party["note"] == f"手动接的群（{LINKED_NAME}）" and deal["mode"] == "copilot"
    [job] = world.jobs()
    meta = json.loads(job["meta"])
    assert (job["chat_id"], text_lines(job)) == (LINKED, ["那寄样怎么弄呀"]) and "opening" not in meta
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [
        ("她", "你们家帽子多少钱"), ("我们", "两三百～我发您看看款式"), ("群成员", "她这周想先看样")]
    assert (meta["group"]["manual"], meta["group"]["others"], meta["group"]["boss"]) == (True, 1, [])
    assert meta["late"]                                                   # (read late: a person decides)
    assert LINKED in run.backend._watched
    run.poll()                                                            # the watch: nothing of before again
    assert run.lines(LINKED) == [] and run.own == [] and len(world.jobs()) == 1
    assert world.events(deal["id"], "operator_message") == []             # our line before the link took nothing over

    # the draft goes out in the group, by its name; read back, it is ours
    _, packet = mac_writes(run, "寄样的话您把地址发群里，我这边安排～")
    assert (packet["chat_id"], packet["status"]) == (LINKED, "pending")
    assert approve(packet["id"]) == {"ok": True, "status": "approved"}
    run.tick()
    assert world.window.typed[-1] == (LINKED_NAME, "寄样的话您把地址发群里，我这边安排～")
    run.poll()
    sent = world.prm_store.packet(packet["id"])
    assert (sent["status"], sent["outcome"]) == ("sent", "sent_confirmed")
    assert world.prm_store.deal(deal["id"])["mode"] == "copilot"

    # her 商务 says something: context only; her next line: a draft job of the group
    world.clock.now += 30
    world.db.line(LINKED, BD, "地址我晚点发", world.clock.now)
    run.poll()
    assert [j for j in world.jobs() if j["status"] == "pending"] == []
    world.clock.now += 30
    world.db.line(LINKED, HER, "好的谢谢", world.clock.now)
    run.poll()
    job = world.open_job(LINKED)
    assert text_lines(job) == ["好的谢谢"] and "late" not in json.loads(job["meta"])
    assert [(x["who"], x["text"]) for x in json.loads(job["meta"])["group"]["lines"]] == [("群成员", "地址我晚点发")]


def test_an_operators_own_line_takes_her_over_a_restart_reads_on_and_bu_jie_qun_stops_it(world):
    run = started(world)
    admin_says(world, run, "接群")
    admin_says(world, run, "接群 1")
    admin_says(world, run, "是 1")
    deal_id = world.prm_store.boss_group(LINKED)["deal_id"]
    run.poll()
    # an operator answers in the group by hand: her deal is a person's
    world.clock.now += 30
    world.db.line(LINKED, ME_SENDER, "我是品牌的运营，地址发我就行", world.clock.now)
    run.poll()
    assert world.prm_store.deal(deal_id)["mode"] == "human"
    [ev] = world.events(deal_id, "operator_message")
    assert ev["detail"]["text"].endswith("我是品牌的运营，地址发我就行")

    # a restart: the group is read again from the second after its last line, nothing twice, no takeover again
    run.stop()
    world.prm_store.update_deal(deal_id, world.clock.now, mode="copilot", mode_until=None)
    again = started(world)
    assert LINKED in again.backend._watched and again.prm.is_party_group(LINKED)
    again.poll()
    assert again.lines(LINKED) == [] and again.own == []
    assert world.prm_store.deal(deal_id)["mode"] == "copilot"
    world.clock.now += 30
    world.db.line(LINKED, HER, "地址：测试路1号", world.clock.now)
    again.poll()
    job = world.open_job(LINKED)
    assert text_lines(job) == ["地址：测试路1号"] and json.loads(job["meta"])["group"]["manual"] is True

    # 「不接群 群名」: not read any more, her lines there silent again, the deal goes on
    answer = admin_says(world, again, f"不接群 {LINKED_NAME}")
    assert answer.startswith(f"不接了：群「{LINKED_NAME}」里她的话不再起草") and not again.prm.is_party_group(LINKED)
    assert world.prm_store.boss_group(LINKED)["state"] == "gone" and LINKED not in again.backend._watched
    jobs = len(world.jobs())
    world.clock.now += 30
    world.db.line(LINKED, HER, "还在吗", world.clock.now)
    again.poll()
    assert len(world.jobs()) == jobs and world.window.typed[-1][0] == ADMIN_NAME


def test_with_the_switch_off_and_nothing_linked_every_group_stays_silent(world):
    """The control: PRM_BOSS_GROUPS off and nobody linked a group: every group's line is read and stored, never a job,
    never an answer, the console's card has nothing."""
    run = started(world, groups=("*",))
    world.clock.now += 30
    world.db.line(LINKED, HER, "在吗", world.clock.now)
    world.db.line(NOTICE_GROUP, HER, "在吗", world.clock.now)
    run.poll()
    assert world.jobs() == [] and world.window.typed == []
    assert world.stored(LINKED) == 1 and world.stored(NOTICE_GROUP) == 1
    assert server._prm.snapshot()["boss_groups"] == {"enabled": False, "why_not": "", "warning": "",
                                                     "boss": ["老板测试"], "boss_call": "老板", "pending": [],
                                                     "waiting": 0, "linked": 0}
    assert not run.prm.is_party_group(LINKED) and run.prm.boss_groups.start() == 0
