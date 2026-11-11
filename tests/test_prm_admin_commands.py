"""合作单指令 (PRM_WECHAT_COMMANDS, 2026-11-11, src/prm/admin_commands.py): the console's deal buttons typed in the
admin's WeChat chat, 「做手卡 <达人>」「补发货盘 <达人>」「写开场白 <达人>」「签收 <达人>」「备注 <达人> <文字>」「接管 <达人>」
「副驾驶 <达人>」「合作单 <达人>」 and 「指令」, her deal found by her name (several, or a piece of a name: 「选 N」).

The relay, the PRM service and the router as Bot.run wires them (test_prm_wechat_review's ``Desk``), the admin handler
given the PRM's commands as Bot._wire_friend_adds does.  Each command runs the console's own service method (the same
checks and refusals; the events name the admin), nothing is sent to a creator by it, and with the switch off every line
goes where it went before.  The review of d8263fa (2026-11-11) has its cases here too.  Every name, id and number is made
up.
"""

import io
import json
import logging
import time
from types import SimpleNamespace

import pytest

from test_prm import ADMIN, CREATOR, Clock
from test_prm_handcards import CARDS, card_draft
from test_prm_sample_sync import verified_order
from test_prm_wechat_review import Desk, reply_draft

from platform_fakes import JST_ORDER
from src.admin import HELP_TEXT, RELAY_HELP, AdminCommandHandler
from src.prm import admin_commands as ac
from src.prm.admin_commands import (ACTOR, ASKED_KEEP_SEC, CHOICE_SEC, HELP_LINE, NAME_MAX, SAMPLE_STAGES, TRACE_TEXT,
                                    UNDO_SEC, parse)
from src.prm.service import OPENING_HELD, SETTLE_SEC, apply_deal_action, event_out
from src.prm.store import DRAFT_NOTE_CHARS, STAGES
from src.relay.service import MAX_AGE_SEC

OTHER = "wxid_creator02"            # 抖音-小美美 / 小美美
THIRD = "wxid_creator03"            # a creator named by the friend add's remark with her 抖音号 (no nickname known)
FOURTH = "wxid_creator04"           # 小雨, Lucy Anne …
FIFTH = "wxid_creator05"
DOUYIN_NO = "xm_test1234"
WAYBILL = "SF1234567890"


def at(hour, minute=0, day=11):
    """A time of day on this machine's clock (2026-11-11)."""
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


@pytest.fixture
def clock():
    return Clock(at(14, 0))


def make(clock, tmp_path, *, commands="on", relay_only="off", review="off", **prm):
    """The desk (router, relay, PRM) with 抖音-小美 registered; the admin handler gets the commands while they are on,
    as Bot._wire_friend_adds hands them over."""
    d = Desk(clock, tmp_path, review=review, relay_only=relay_only, prm_wechat_commands=commands,
             **{"prm_handcards": "on", **prm})
    if d.prm.deal_commands.on:
        d.router._admin.set_deal_commands(d.prm.deal_commands)
    d.prm.register(CREATOR, "抖音-小美", "测试达人")
    return d


@pytest.fixture
def desk(clock, tmp_path):
    d = make(clock, tmp_path)
    yield d
    d.stop()


def party(d, wxid=CREATOR):
    return d.prm.store.party_by_wxid(wxid, include_inactive=True)


def deal(d, wxid=CREATOR):
    return d.prm.store.deal_of(party(d, wxid)["id"])


def events(d, kind, wxid=CREATOR):
    return d.prm.store.events(deal(d, wxid)["id"], limit=None, kinds=(kind,))


def open_job(d, chat=CREATOR):
    """Her draft job waiting for the Mac (its ``meta`` read), or None."""
    job = d.relay.store.latest_open(chat, drafts_only=True, system=False)
    if job is not None and isinstance(job.get("meta"), str):
        job = {**job, "meta": json.loads(job["meta"] or "{}")}
    return job


def jobs(d):
    return d.relay.store.list_jobs(100)


def admin_msg(text, **extra):
    """The admin's typed line in the admin's own chat, as the backend gives it."""
    return {"message_id": "x1", "chat_id": ADMIN, "sender_id": ADMIN, "is_group": False, "msg_type": 1,
            "content": text, **extra}


def cards_drafted(d, job):
    """The Mac takes the job and hands back two cards with their verdict: the packet."""
    d.clock.now += SETTLE_SEC + 1
    assert d.relay.next_job(0, ("prm",))["id"] == job["id"]
    for name, body in CARDS:
        assert d.relay.outfile(job["id"], name, io.BytesIO(body).read, len(body))
    assert d.relay.finish(job["id"], {"ok": True, "text": json.dumps(card_draft(), ensure_ascii=False),
                                      "duration_s": 3})
    d.relay.tick()
    return d.prm.store.packet_for_job(job["id"])


def opening_drafted(d, job, text="老师您好，我是品牌的运营～"):
    d.clock.now += SETTLE_SEC + 1
    assert d.relay.next_job(0, ("prm",))["id"] == job["id"]
    d.relay.finish(job["id"], {"ok": True, "text": json.dumps(reply_draft(text=text, message_kind="greeting"),
                                                              ensure_ascii=False), "duration_s": 3})
    d.relay.tick()
    return d.prm.store.packet_for_job(job["id"])


# ── what is a command ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text, verb, name, words", [
    ("做手卡 小美", "handcards", "小美", None),
    ("做手卡　小美", "handcards", "小美", None),                     # (a full-width space)
    ("补发货盘 小美。", "huopan", "小美", None),                     # (one 「。」 at the end: still a command)
    ("写开场白 小美！", "opening", "小美", None),
    ("签收 Lucy Anne", "sign", "Lucy Anne", None),
    ("接管 小美", "takeover", "小美", None),
    ("副驾驶 小美", "copilot", "小美", None),
    ("恢复副驾驶 小美", "copilot", "小美", None),
    ("合作单 小美", "status", "小美", None),
    ("签收 美", "sign", "美", None),                                 # (too short: a command, answered so)
    ("备注 小美 只发链接和brief。", "note", "小美", "只发链接和brief。"),   # (the note keeps its own ending)
    ("备注 小美：她要不要样品？", "note", "小美", "她要不要样品？"),       # (a colon: any words, a question too)
    ("备注 小美 清空", "note", "小美", ""),
    ("备注 小美 撤销", "note", "小美", "撤销"),
    ("备注 小美", "note", "小美", None),
    ("指令", "help", "", None),
])
def test_a_command_is_the_word_a_blank_and_her_name(text, verb, name, words):
    typed = parse(text)
    assert (typed.verb, typed.name, typed.text, typed.more) == (verb, name, words, False)


@pytest.mark.parametrize("text", [
    "做手卡怎么用", "签收了吗", "签收了没", "做手卡", "做手卡 ", "合作单 小美呢", "做手卡 小美？", "做手卡 小美?",
    "签收 小美…", "签收 小美...", "签收 小美。。", "合作单 小美 怎么样了", "做手卡 小美吗", "接管 小美是不是要先等等",
    "备注一下 小美", "备注：小美喜欢直接发链接", "指令？", "指令 是什么", "帮我做手卡 小美", "cc 做手卡 小美",
    "签收 的流程是怎样的", "接管 以后多久恢复", "做手卡 要用到哪些数据", "合作单 一共多少个", "1", "2", "发 12", "执行",
    "是 1", "不接", "接群", "选 1",
])
def test_near_misses_and_other_features_words_are_no_command(text):
    assert parse(text) is None


def test_more_lines_after_a_command_line_make_it_refused_whole():
    assert parse("做手卡 小美\ncc 跑测试").more is True
    assert parse("你好\n做手卡 小美") is None                        # (only a first line can be one)


def test_a_note_line_reads_every_way_and_says_when_it_would_be_no_command():
    assert parse("备注 小美 时间：周三").readings == (("小美 时间", "周三", True), ("小美", "时间：周三", False),
                                                     ("小美 时间：周三", None, False))
    assert parse("备注 Lucy Anne 只发链接").readings == (("Lucy", "Anne 只发链接", False), ("Lucy Anne", "只发链接", False),
                                                         ("Lucy Anne 只发链接", None, False))
    assert parse("备注 小美 不要问她要不要样品").asking is True and parse("备注 小美 只发链接").asking is False


def test_the_switch_is_off_by_default_and_never_on_the_macs_edge_side():
    from src.config import BotConfig, _validate_config

    assert BotConfig().prm_wechat_commands == "off"
    assert ac.switch_on(SimpleNamespace()) is False
    assert ac.DealCommands(None, SimpleNamespace(prm_wechat_commands="on", wechat_backend="edge")).on is False
    assert ac.DealCommands(None, SimpleNamespace(prm_wechat_commands="on")).on is True
    _validate_config({"prm_wechat_commands": "on"})
    with pytest.raises(RuntimeError) as ctx:
        _validate_config({"prm_wechat_commands": "maybe"})
    assert "PRM_WECHAT_COMMANDS must be off or on" in str(ctx.value)


def test_the_switch_is_read_from_the_env():
    import os
    from unittest.mock import patch

    from src.config import load_config

    for env, want in (({}, "off"), ({"PRM_WECHAT_COMMANDS": "on"}, "on"), ({"PRM_WECHAT_COMMANDS": "yes"}, "on"),
                      ({"PRM_WECHAT_COMMANDS": "0"}, "off")):
        with patch.dict(os.environ, env, clear=True):
            assert load_config().prm_wechat_commands == want, env


def test_the_sample_stages_are_real_stages():
    assert set(SAMPLE_STAGES) <= set(STAGES) and set(ac.AFTER_DELIVERY) <= set(STAGES)
    assert [STAGES[s] for s in SAMPLE_STAGES] == ["收集收件信息", "待寄出", "已寄出", "运输中", "物流异常"]


# ── each command: the console's own service method ────────────────────────

def test_handcards_is_the_consoles_with_nothing_ticked_and_its_draft_always_waits(desk, clock):
    """「做手卡 小美」: one draft job asking for her latest sample order's products, as the console's 「做手卡」 with nothing
    ticked; the deal's record names the admin; the answer carries no job number.  Its draft waits for a person though
    its kind's gate is at auto and 自动模式 is on: nothing goes to her."""
    verified_order(desk.prm)
    reply = desk.admin("做手卡 小美")
    job = open_job(desk)
    assert reply == (f"抖音-小美：已派做手卡的任务（按她最近一张寄样单 …{JST_ORDER[-4:]} 的 1 款），"
                     "做好会进「待审批」，要你看过才发")
    assert job["meta"]["handcards"]["by"] == "console" and job["meta"]["handcards"]["order"] == JST_ORDER
    [ev] = events(desk, "handcards")
    assert ev["actor"] == ACTOR == "admin" and ev["detail"] == {"job": job["id"], "items": 1}
    assert event_out(ev)["name"] == f"派做手卡的任务 #{job['id']}（运营点的，1 款）"
    desk.prm.store.set_gate("material", "auto", clock.now)
    assert desk.prm.set_auto_mode(True)["ok"]
    packet = cards_drafted(desk, job)
    assert (packet["status"], packet["route"]) == ("pending", "review")
    desk.prm.send_approved()
    assert not any(chat == CREATOR for chat, _ in desk.sent) and desk.files == []
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"


def test_handcards_without_a_sample_order_and_the_consoles_refusals(desk, clock):
    reply = desk.admin("做手卡 小美")
    assert reply == ("抖音-小美：已派做手卡的任务（她没有机器人建的抖店寄样单：Mac 按案卷里她选的款做），"
                     "做好会进「待审批」，要你看过才发")
    assert desk.admin("做手卡 小美") == "抖音-小美：没派做手卡：她的草稿正在写，写好会出现在「待审批」里"
    assert len(jobs(desk)) == 1


def test_handcards_off_says_what_the_console_says(clock, tmp_path):
    d = make(clock, tmp_path, prm_handcards="off")
    try:
        assert d.admin("做手卡 小美") == "抖音-小美：没派做手卡：手卡没打开（Windows 的 PRM_HANDCARDS），不能做手卡"
        assert open_job(d) is None and events(d, "handcards") == []
    finally:
        d.stop()


def test_huopan_is_the_consoles_and_its_draft_waits(desk, clock):
    reply = desk.admin("补发货盘 小美")
    job = open_job(desk)
    assert reply == "抖音-小美：已派补发货盘的任务，写好会进「待审批」，要你看过才发"
    assert job["meta"]["huopan"] == {"by": "console"}
    [ev] = events(desk, "huopan")
    assert ev["actor"] == "admin" and event_out(ev)["name"] == f"派补发货盘的任务 #{job['id']}（运营点的）"
    desk.prm.store.set_gate("greeting", "auto", clock.now)
    assert desk.prm.set_auto_mode(True)["ok"]
    packet = desk.mac(text="老师，这是我们的秋冬货盘，您看看喜欢哪几款～")
    assert packet["status"] == "pending"


def test_an_opening_ordered_in_wechat_always_waits_and_is_told_in_wechat(clock, tmp_path):
    """(Review 2026-11-11, item 2.)  With PRM_OPENING_AUTO_MIN on, the console's 「写开场白」 would go out by itself; ordered
    from WeChat it never does (``meta.opening.held``: no ``auto_at``, no ``opening_auto`` for the Mac), it is told to the
    admin's chat like any waiting draft, 「不发 N」 drops it, and an hour later nothing went to her."""
    d = make(clock, tmp_path, review="on", prm_opening_auto_min=30)
    try:
        clock.now += 200                                                  # (past the start's grace)
        assert d.admin("写开场白 小美") == "抖音-小美：已派写开场白的任务，写好会进「待审批」，要你看过才发"
        job = open_job(d)
        assert job["meta"]["opening"] == {"how": "", "by": "console", OPENING_HELD: True}
        assert "opening_auto" not in job["meta"]
        [ev] = events(d, "opening")
        assert ev["actor"] == "admin" and event_out(ev)["name"] == f"派写开场白的任务 #{job['id']}（运营点的）"
        packet = opening_drafted(d, job)
        assert packet["status"] == "pending" and "auto_at" not in packet["meta"]
        d.review.sweep()
        assert [t for t in d.told() if t.startswith(f"#{packet['id']} 草稿")]
        clock.now += 61 * 60
        assert d.prm.send_due_openings() == 0
        d.prm.send_approved()
        assert d.to(CREATOR) == []
        assert d.admin(f"不发 {packet['id']}").startswith(f"#{packet['id']} 不发了")
        assert d.prm.store.packet(packet["id"])["status"] == "dropped"
    finally:
        d.stop()


def test_an_opening_ordered_in_wechat_waits_by_night_and_in_auto_mode_too(clock, tmp_path):
    d = make(clock, tmp_path, prm_opening_auto_min=30, prm_night_auto_hours="00:00-23:59")
    try:
        clock.now += 200
        assert d.prm.set_auto_mode(True)["ok"]
        d.admin("写开场白 小美")
        packet = opening_drafted(d, open_job(d))
        assert (packet["status"], packet["route"]) == ("pending", "review") and "auto_at" not in packet["meta"]
        assert d.prm.send_due_openings() == 0 and d.to(CREATOR) == []
    finally:
        d.stop()


def test_the_consoles_opening_goes_by_itself_as_before(clock, tmp_path):
    """A click in the console keeps today's way exactly: ``opening_auto`` for the Mac, ``auto_at`` on its draft."""
    d = make(clock, tmp_path, prm_opening_auto_min=30)
    try:
        clock.now += 200
        assert d.prm.deal_action("opening", {"deal_id": deal(d)["id"]})["ok"]
        job = open_job(d)
        assert job["meta"]["opening"] == {"how": "", "by": "console"} and job["meta"]["opening_auto"] == 30
        packet = opening_drafted(d, job)
        assert isinstance(packet["meta"].get("auto_at"), (int, float))
        assert [e["actor"] for e in events(d, "opening")] == ["console"]
    finally:
        d.stop()


def test_a_refusal_is_the_consoles_own(desk):
    assert desk.prm.deal_action("mode", {"deal_id": deal(desk)["id"], "mode": "human"})["ok"]
    assert desk.admin("写开场白 小美") == "抖音-小美：没派写开场白：现在不在副驾驶模式（人工接管或暂停），不写开场白"
    assert desk.admin("补发货盘 小美") == "抖音-小美：没派补发货盘：现在不在副驾驶模式（人工接管或暂停），不补发货盘"
    assert open_job(desk) is None


def test_sign_on_her_one_waybill_is_the_consoles_update_to_delivered(desk, clock):
    """「签收 小美」 with one waybill: the console's 「更新物流 → 已签收」 (shipment_status delivered): the shipment, the stage,
    the signature (the 专属佣金 chain looks at it as from the console) and a deleted order's notice seen to."""
    deal_id = deal(desk)["id"]
    assert desk.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": WAYBILL})["ok"]
    sent = verified_order(desk.prm, number="YP2611110000009")
    store = desk.prm.store
    store.add_sample_order("YP2611110000009", packet_id=sent["id"], deal_id=deal_id, party_id=party(desk)["id"],
                           made_at=clock.now, done="deleted")
    store.set_notice(deal_id, "sample:YP2611110000009", "聚水潭里这张寄样单被删了", clock.now)
    clock.now += 60
    reply = desk.admin("签收 小美")
    assert reply == ("抖音-小美：标成「已签收」了（顺丰 …7890）；签收以后该做的（比如专属佣金）和控制台点「已签收」一样起草，"
                     "在「待审批」里等你批")
    after = store.deal(deal_id)
    assert after["stage"] == "delivered" and after["delivered_at"] == clock.now
    assert store.latest_shipment(deal_id)["status"] == "delivered"
    assert after["chain"].get("seen") == clock.now                       # (_examine_delivery looked at it)
    assert not any(n["key"] == "sample:YP2611110000009" for n in after["notices"])
    [ev] = events(desk, "shipment_status")
    assert ev["actor"] == "admin" and ev["detail"]["status"] == "delivered"
    assert desk.admin("签收 小美") == "抖音-小美：合作单本来就是「已签收」，没再标：要重新标请到控制台"


@pytest.mark.parametrize("stage", SAMPLE_STAGES)
def test_sign_without_a_waybill_at_a_sample_stage_is_the_consoles_stage_change(desk, stage):
    deal_id = deal(desk)["id"]
    assert desk.prm.deal_action("stage", {"deal_id": deal_id, "stage": stage})["ok"]
    assert desk.admin("签收 小美").startswith("抖音-小美：合作单没有运单号，阶段改成了「已签收」；")
    assert desk.prm.store.deal(deal_id)["stage"] == "delivered"
    assert (events(desk, "stage")[0]["actor"], events(desk, "stage")[0]["detail"]["to"]) == ("admin", "delivered")


@pytest.mark.parametrize("stage", ["new", "talk", "agreed", "hold"])
def test_sign_without_a_waybill_nor_samples_is_the_consoles(desk, stage):
    """(Review 2026-11-11, item 4.)  A deal that never had samples is not signed for from WeChat."""
    deal_id = deal(desk)["id"]
    assert desk.prm.deal_action("stage", {"deal_id": deal_id, "stage": stage})["ok"]
    assert desk.admin("签收 小美") == (f"抖音-小美：没标：合作单是「{STAGES[stage]}」，没有运单号也没有寄样单："
                                    "要标签收请到控制台")
    after = desk.prm.store.deal(deal_id)
    assert after["stage"] == stage and not after["delivered_at"]


def test_sign_without_a_waybill_but_with_a_sample_order(desk):
    verified_order(desk.prm)
    assert desk.admin("签收 小美").startswith("抖音-小美：合作单没有运单号，阶段改成了「已签收」；")
    assert deal(desk)["stage"] == "delivered"


def test_sign_with_two_waybills_or_past_the_signature_is_the_consoles(desk, clock):
    deal_id = deal(desk)["id"]
    for waybill in (WAYBILL, "YT9876543210"):
        assert desk.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": waybill})["ok"]
    assert desk.admin("签收 小美") == ("抖音-小美：没标：她的合作单有 2 个运单号，不知道签收的是哪个：请到控制台她的合作单里"
                                    "「更新物流」")
    assert desk.prm.store.deal(deal_id)["stage"] == "shipped" and events(desk, "shipment_status") == []
    assert desk.prm.deal_action("stage", {"deal_id": deal_id, "stage": "published"})["ok"]
    assert desk.admin("签收 小美") == "抖音-小美：没标：合作单已经是「已发布」了，标签收会把阶段改回「已签收」：要改请到控制台"
    assert desk.prm.store.deal(deal_id)["stage"] == "published"


def test_note_replaces_shows_both_clears_and_refuses_what_the_console_refuses(desk):
    deal_id = deal(desk)["id"]
    assert desk.admin("备注 小美") == "抖音-小美：还没有起草备注（「备注 名字 文字」写一条）"
    assert desk.admin("备注 小美 直接发链接和brief，不接语音") == (
        "抖音-小美：起草备注换好了。原来：（没有） 现在：直接发链接和brief，不接语音")
    assert desk.prm.store.deal(deal_id)["draft_note"] == "直接发链接和brief，不接语音"
    assert desk.admin("备注 小美：周三以后再约直播？") == (
        "抖音-小美：起草备注换好了。原来：直接发链接和brief，不接语音 现在：周三以后再约直播？（「备注 名字 撤销」能放回原来的）")
    assert desk.prm.store.deal(deal_id)["draft_note"] == "周三以后再约直播？"           # (replaced, not added)
    assert desk.admin("备注 小美：周三以后再约直播？") == "抖音-小美：起草备注本来就是这样，没改"
    assert desk.admin("备注 小美") == "抖音-小美：起草备注（9 字）：周三以后再约直播？"
    long = "长" * (DRAFT_NOTE_CHARS + 1)
    assert desk.admin(f"备注 小美 {long}") == (f"抖音-小美：没改起草备注：起草备注最多 {DRAFT_NOTE_CHARS} 个字"
                                           f"（现在 {DRAFT_NOTE_CHARS + 1} 个）")
    assert desk.admin("备注 小美 清空") == ("抖音-小美：起草备注清掉了。原来：周三以后再约直播？"
                                         "（「备注 名字 撤销」能放回原来的）")
    assert desk.prm.store.deal(deal_id)["draft_note"] == ""
    assert [e["detail"] for e in events(desk, "draft_note")] == [{"chars": 0}, {"chars": 9}, {"chars": 16}]
    assert [e["actor"] for e in events(desk, "draft_note")] == ["admin"] * 3


def test_note_undo_puts_back_the_note_it_replaced_once(desk, clock):
    """(Review 2026-11-11, item 6c.)  「备注 名字 撤销」: the note this command replaced last, put back once, within
    UNDO_SEC, unless it changed since; the events still record lengths only."""
    deal_id = deal(desk)["id"]
    assert desk.admin("备注 小美 撤销") == f"抖音-小美：{ac.NO_UNDO_TEXT}"
    desk.admin("备注 小美 先发货盘再谈佣金")
    desk.admin("备注 小美 的合作情况帮我总结下")                         # (a slip: it is her note now, the answer shows both)
    assert desk.admin("备注 小美 撤销") == "抖音-小美：起草备注放回去了：先发货盘再谈佣金"
    assert desk.prm.store.deal(deal_id)["draft_note"] == "先发货盘再谈佣金"
    assert desk.admin("备注 小美 撤销") == f"抖音-小美：{ac.NO_UNDO_TEXT}"             # (once)
    desk.admin("备注 小美 只发链接")
    assert desk.prm.deal_action("draft_note", {"deal_id": deal_id, "text": "控制台改的"})["ok"]
    assert desk.admin("备注 小美 撤销") == "抖音-小美：起草备注在那以后又改过了（不是这条指令改的），没撤销：请到控制台看"
    assert desk.prm.store.deal(deal_id)["draft_note"] == "控制台改的"
    desk.admin("备注 小美 只发链接")
    clock.now += UNDO_SEC + 1
    assert desk.admin("备注 小美 撤销") == f"抖音-小美：{ac.NO_UNDO_TEXT}"
    assert all(set(e["detail"]) <= {"chars"} for e in events(desk, "draft_note"))


def test_a_note_read_two_ways_takes_the_name_that_matches(desk):
    assert desk.admin("备注 小美 时间：周三").startswith("抖音-小美：起草备注换好了。")
    assert desk.prm.store.deal(deal(desk)["id"])["draft_note"] == "时间：周三"


@pytest.mark.parametrize("how", ["stopped", "purged"])
def test_a_stopped_or_cleaned_longer_name_is_never_a_shorter_live_ones_note(desk, clock, how):
    """(Re-review 2026-11-11, N1.)  「Lucy Anne」 stopped (or her deal cleaned up), 「Lucy」 live: 「备注 Lucy Anne 只发链接」
    writes nothing (never 「Anne 只发链接」 into Lucy's note), the answer is the stopped / cleaned one."""
    desk.prm.register(FOURTH, "Lucy Anne", "测试达人")
    desk.prm.register(OTHER, "Lucy", "测试达人")
    if how == "stopped":
        desk.prm.set_active(party(desk, FOURTH)["id"], False)
        said = "「Lucy Anne」是停用的达人（不在达人名单里）：没做，请到控制台看"
    else:
        desk.prm.store.update_deal(deal(desk, FOURTH)["id"], clock.now, purged_at=clock.now)
        said = "「Lucy Anne」的合作单收件信息已经按规定清过了：没做，请到控制台看"
    for text in ("备注 Lucy Anne 只发链接", "备注 Lucy Anne：只发链接", "备注 Lucy Anne"):
        assert desk.admin(text) == said, text
    assert deal(desk, OTHER)["draft_note"] == "" and events(desk, "draft_note", OTHER) == []
    assert desk.admin("备注 Lucy 只发链接").startswith("Lucy：起草备注换好了。")       # (her own name: hers)
    assert deal(desk, OTHER)["draft_note"] == "只发链接"


def test_a_name_with_spaces_goes_whole_and_none_of_it_into_the_note(desk):
    """(Review 2026-11-11, item 6a.)  The longest name that matches a live creator wins."""
    desk.prm.register(FOURTH, "Lucy Anne", "测试达人")
    assert desk.admin("备注 Lucy Anne 只发链接") == "Lucy Anne：起草备注换好了。原来：（没有） 现在：只发链接"
    assert deal(desk, FOURTH)["draft_note"] == "只发链接"
    assert desk.admin("备注 Lucy Anne：周三再说") .endswith("现在：周三再说（「备注 名字 撤销」能放回原来的）")
    assert desk.admin("备注 Lucy Anne").startswith("Lucy Anne：起草备注（4 字）：周三再说")


QUESTION_LIKE = ["不要问她要不要样品", "她什么款都可以", "样品还没", "只发链接...", "先别发货盘…", "她问能不能便宜",
                 "Only brief.", "她要的是哪个颜色？", "怎么写比较好？", "是什么意思"]


@pytest.mark.parametrize("words", QUESTION_LIKE)
def test_a_note_in_the_colon_form_takes_any_words(desk, words):
    """(Review 2026-11-11, item 6b.)  「备注 名字：…」 with a name that matches a live creator: the rest is her note,
    whatever it says."""
    assert desk.admin(f"备注 小美：{words}").startswith("抖音-小美：起草备注换好了。")
    assert deal(desk)["draft_note"] == words


@pytest.mark.parametrize("relay_only", ["off", "on"])
@pytest.mark.parametrize("words", QUESTION_LIKE)
def test_a_note_in_the_blank_form_that_reads_as_a_question_writes_nothing(clock, tmp_path, relay_only, words):
    """(Re-review 2026-11-11, N3.)  The blank form with words that ask or hesitate: the admin may have been asking;
    nothing written, nothing passed on, one line saying to use the colon (the cc part with CLAUDE_RELAY_ONLY)."""
    d = make(clock, tmp_path, relay_only=relay_only)
    try:
        d.admin("备注 小美 只发链接")
        before = len(jobs(d))
        assert d.admin(f"备注 小美 {words}") == ("这句像是在问话，没改备注：要写成备注请用冒号「备注 小美：…」"
                                               + ("；要问 Claude 请在前面加 cc" if relay_only == "on" else ""))
        assert deal(d)["draft_note"] == "只发链接" and len(jobs(d)) == before
        assert d.admin("备注 小美 撤销").startswith("抖音-小美：起草备注放回去了")      # (the last change is the first one)
    finally:
        d.stop()


def test_a_note_in_the_blank_form_without_a_question_still_writes(desk):
    assert desk.admin("备注 小美 只发链接，周三以后再约").startswith("抖音-小美：起草备注换好了。")
    assert deal(desk)["draft_note"] == "只发链接，周三以后再约"


def test_a_question_after_备注_that_names_nobody_goes_on_as_before(clock, tmp_path):
    for relay_only in ("off", "on"):
        d = make(clock, tmp_path / relay_only, relay_only=relay_only)
        try:
            reply = d.admin("备注 一下这个达人要不要样品")
            assert reply is None if relay_only == "off" else reply.startswith("收到，已交给 Mac")
            assert d.admin("备注 张三 只发链接") == "没有达人对得上「张三」（只找名单里在用的达人）" + (
                "（要交给 Claude 请在前面加 cc）" if relay_only == "on" else "")
            assert deal(d)["draft_note"] == ""
        finally:
            d.stop()


def test_takeover_and_back_to_copilot_are_the_consoles_modes(desk, clock):
    deal_id = deal(desk)["id"]
    assert desk.admin("副驾驶 小美") == "抖音-小美：本来就是副驾驶：她的消息照常起草"
    assert events(desk, "mode") == []
    reply = desk.admin("接管 小美")
    until = time.strftime("%H:%M", time.localtime(clock.now + 120 * 60))
    assert reply == f"抖音-小美：人工接管了，到 {until} 自动回副驾驶：这段时间她的消息只记录、不起草（「副驾驶 名字」提前恢复）"
    assert desk.prm.store.deal(deal_id)["mode"] == "human"
    assert desk.admin("恢复副驾驶 小美") == "抖音-小美：恢复副驾驶了：她的消息照常起草，接管期间双方说的话会带进下一份草稿"
    assert desk.prm.store.deal(deal_id)["mode"] == "copilot"
    assert [(e["actor"], e["detail"]["to"]) for e in events(desk, "mode")] == [("admin", "copilot"), ("admin", "human")]


def test_the_deal_status_is_read_only_and_shows_no_id_in_full(desk, clock):
    desk.prm.register(THIRD, f"抖音-{DOUYIN_NO}", "测试达人")
    deal_id = deal(desk, THIRD)["id"]
    assert desk.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": WAYBILL})["ok"]
    assert desk.prm.deal_action("draft_note", {"deal_id": deal_id, "text": "只发链接"})["ok"]
    before = len(desk.prm.store.events(deal_id, limit=None))
    reply = desk.admin(f"合作单 {DOUYIN_NO}")
    lines = reply.split("\n")
    assert lines[0] == f"抖音-…1234（合作单 {deal_id} 号）：阶段「已寄出」 · 副驾驶"
    assert lines[1] == "寄样：顺丰 …7890 已寄出"
    assert lines[2] == "回复：私聊；机器人主动发的（开场白、货盘、手卡、发货通知）发私聊"
    [notice] = desk.prm.store.deal_packets(deal_id, statuses=("pending",))
    assert lines[3] == f"待审批：1 份（#{notice['id']} 寄出、物流通知）"         # (a draft's number, as the notices)
    assert lines[4] == "起草备注：有（4 字，「备注 名字」看）"
    assert DOUYIN_NO not in reply and WAYBILL not in reply
    assert len(desk.prm.store.events(deal_id, limit=None)) == before
    desk.her("你好", chat=THIRD)
    assert desk.admin(f"合作单 {DOUYIN_NO}").split("\n")[3].endswith("；Mac 正在写她的草稿")


def test_no_answer_carries_a_job_number_one_could_answer_with(clock, tmp_path):
    """(Review 2026-11-11, item 7.)  A relay job's number can be another creator's notified draft's: no answer says it."""
    d = make(clock, tmp_path, review="on")
    try:
        d.prm.register(OTHER, "小雨", "测试达人")
        d.her("老师好", chat=OTHER)
        d.mac()
        d.review.sweep()
        for text in ("补发货盘 小美", "合作单 小美"):
            reply = d.admin(text)
            assert not any(f"#{j['id']}" in reply for j in jobs(d)), (text, reply)
            assert "#" not in reply.replace("「", "").split("待审批")[0], reply
    finally:
        d.stop()


def test_the_list_of_commands_and_the_admins_help(desk):
    assert desk.admin("指令") == ac.HELP_TEXT
    assert desk.admin("帮助") == HELP_TEXT + HELP_LINE


# ── finding her ───────────────────────────────────────────────────────────

def test_her_name_matches_with_or_without_the_prefix_case_and_spaces(desk):
    desk.prm.register(FOURTH, "Lucy Anne", "测试达人")
    for text in ("合作单 小美", "合作单 抖音-小美", "合作单 抖音小美", "合作单 抖音 小美"):
        assert desk.admin(text).startswith("抖音-小美（合作单 "), text
    for text in ("合作单 lucy anne", "合作单 LUCYANNE", "合作单 ＬＵＣＹ"):
        assert desk.admin(text).startswith("Lucy Anne（合作单 "), text
    assert deal(desk, FOURTH)["mode"] == "copilot"
    desk.admin("接管 Lucy Anne")
    assert deal(desk, FOURTH)["mode"] == "human"


def test_a_whole_name_wins_over_names_that_hold_it(desk):
    desk.prm.register(OTHER, "小美美", "测试达人")
    assert desk.admin("合作单 小美").startswith("抖音-小美（合作单 ")
    assert desk.admin("合作单 小美美").startswith("小美美（合作单 ")
    desk.admin("接管 小美")
    assert (deal(desk)["mode"], deal(desk, OTHER)["mode"]) == ("human", "copilot")


@pytest.mark.parametrize("how", ["stopped", "purged"])
def test_a_stopped_or_cleaned_whole_name_never_falls_back_to_a_piece_of_another(desk, clock, how):
    """(Review 2026-11-11, item 1.)  「接管 小美」 with 抖音-小美 stopped (or her deal cleaned up) never acts on the live
    小美美."""
    desk.prm.register(OTHER, "抖音-小美美", "测试达人")
    if how == "stopped":
        desk.prm.set_active(party(desk)["id"], False)
        said = "「小美」是停用的达人（不在达人名单里）：没做，请到控制台看"
    else:
        desk.prm.store.update_deal(deal(desk)["id"], clock.now, purged_at=clock.now)
        said = "「小美」的合作单收件信息已经按规定清过了：没做，请到控制台看"
    for text in ("接管 小美", "签收 小美", "合作单 小美"):
        assert desk.admin(text) == said, text
    other = deal(desk, OTHER)
    assert (other["mode"], other["stage"]) == ("copilot", "talk") and events(desk, "mode", OTHER) == []


def test_a_piece_of_a_name_always_asks_first_for_a_command_that_changes_something(desk):
    """(Review 2026-11-11, item 5.)  A piece of one name: listed, 「选 1」 runs it; 「合作单」 (read only) at once."""
    desk.prm.register(OTHER, "抖音-小美美", "测试达人")
    other = deal(desk, OTHER)["id"]
    reply = desk.admin("接管 美美")
    assert reply == "\n".join([f"「美美」只是名字的一部分，对得上 1 位：是她就回「选 1」人工接管（{CHOICE_SEC // 60} 分钟内有效）",
                               f"1. 抖音-小美美 · 意向沟通 · 合作单 {other} 号"])
    assert deal(desk, OTHER)["mode"] == "copilot"
    assert desk.admin("选 1").startswith("抖音-小美美：人工接管了")
    assert deal(desk, OTHER)["mode"] == "human" and deal(desk)["mode"] == "copilot"
    assert desk.admin("合作单 美美").startswith(f"抖音-小美美（合作单 {other} 号）")
    assert desk.admin("备注 美美").startswith("抖音-小美美：还没有起草备注")


def test_a_name_too_short_or_too_long_gets_one_line(clock, tmp_path):
    """(Review 2026-11-11, items 5 and 9.)"""
    for relay_only in ("off", "on"):
        d = make(clock, tmp_path / relay_only, relay_only=relay_only)
        try:
            hint = "（要交给 Claude 请在前面加 cc）" if relay_only == "on" else ""
            assert d.admin("接管 美") == ac.SHORT_TEXT
            assert d.admin("签收 " + "长" * (NAME_MAX + 1)) == ac.LONG_TEXT + hint
            assert d.admin("备注 美 只发链接") == ac.SHORT_TEXT
            assert deal(d)["mode"] == "copilot" and jobs(d) == []
        finally:
            d.stop()


def test_namesakes_are_listed_and_choose_n_runs_the_command_once(desk, clock):
    desk.prm.register(OTHER, "小雨", "测试达人")
    desk.prm.register(FOURTH, "小雨", "测试达人")
    other, fourth = deal(desk, OTHER)["id"], deal(desk, FOURTH)["id"]
    reply = desk.admin("补发货盘 小雨")
    assert reply == "\n".join([f"有 2 位达人都叫「小雨」：回「选 编号」补发货盘（{CHOICE_SEC // 60} 分钟内有效）",
                               f"1. 小雨 · 意向沟通 · 合作单 {other} 号", f"2. 小雨 · 意向沟通 · 合作单 {fourth} 号"])
    assert jobs(desk) == []
    assert desk.admin("选 3") == "没有第 3 个：回「选 1」到「选 2」"
    assert desk.admin("选 2") == "小雨：已派补发货盘的任务，写好会进「待审批」，要你看过才发"
    assert open_job(desk, FOURTH) is not None and open_job(desk, OTHER) is None
    assert desk.admin("选 2") == "刚才那个「选」已经回过了：要再来一次请重新发「补发货盘 小雨」"
    assert len(jobs(desk)) == 1


def test_several_pieces_are_listed(desk):
    desk.prm.register(OTHER, "抖音-小美美", "测试达人")
    desk.prm.register(FOURTH, "小美美的商务", "测试达人")
    mine, other, fourth = deal(desk)["id"], deal(desk, OTHER)["id"], deal(desk, FOURTH)["id"]
    lines = desk.admin("签收 美美").split("\n")
    assert lines == [f"有 2 位达人的名字里有「美美」：回「选 编号」标签收（{CHOICE_SEC // 60} 分钟内有效）",
                     f"1. 抖音-小美美 · 意向沟通 · 合作单 {other} 号", f"2. 小美美的商务 · 意向沟通 · 合作单 {fourth} 号"]
    assert mine not in (other, fourth)


def test_a_later_command_closes_the_open_list(desk):
    """(Review 2026-11-11, item 9.)  「选 1」 after another command runs nothing of the earlier list."""
    desk.prm.register(OTHER, "小雨", "测试达人")
    desk.prm.register(FOURTH, "小雨", "测试达人")
    desk.admin("签收 小雨")
    assert desk.admin("合作单 小美").startswith("抖音-小美（合作单 ")
    assert desk.admin("选 1") == "刚才那个列表作废了（后来又发了别的合作单指令）：要的话重新发「签收 小雨」"
    assert deal(desk, OTHER)["stage"] == "talk" and deal(desk, FOURTH)["stage"] == "talk"


def test_choose_n_expires_then_passes_on(desk, clock):
    desk.prm.register(OTHER, "小雨", "测试达人")
    desk.prm.register(FOURTH, "小雨", "测试达人")
    desk.admin("签收 小雨")
    clock.now += CHOICE_SEC + 1
    assert desk.admin("选 1") == f"刚才那个列表已经过了 {CHOICE_SEC // 60} 分钟，作废了：要的话重新发「签收 小雨」"
    assert deal(desk, OTHER)["stage"] == "talk" and deal(desk, FOURTH)["stage"] == "talk"
    clock.now += ASKED_KEEP_SEC
    assert desk.admin("选 1") is None                                     # (no list lately: the words are free)
    assert desk.router._admin.is_command("选 1") is False


def test_choose_n_is_no_command_without_a_list(desk):
    assert desk.admin("选 1") is None and not desk.router._admin.is_command("选 1")


def test_a_list_lists_ten_at_most(desk):
    for n in range(12):
        desk.prm.register(f"wxid_many{n:02d}", f"测试达人{n:02d}", "测试达人")
    lines = desk.admin("合作单 测试达人").split("\n")
    assert lines[0].startswith("有 12 位达人的名字里有「测试达人」：回「选 编号」看合作单")
    assert lines[0].endswith("；只列了前 10 位，不在里面的话名字写全一点再发")
    assert len(lines) == 11 and lines[1].endswith(" 号")


def test_none_gives_the_closest_names_and_is_not_handed_on(clock, tmp_path):
    d = make(clock, tmp_path, relay_only="on")
    try:
        d.prm.register(OTHER, "小玫", "测试达人")
        assert d.admin("做手卡 小梅") == ("没有达人对得上「小梅」（只找名单里在用的达人）；是不是：抖音-小美、小玫？"
                                        "（要交给 Claude 请在前面加 cc）")
        assert d.admin("做手卡 张三") == "没有达人对得上「张三」（只找名单里在用的达人）（要交给 Claude 请在前面加 cc）"
        assert jobs(d) == []                                              # (nothing for the Mac's Claude)
    finally:
        d.stop()


def test_only_live_deals_are_found(desk, clock):
    desk.prm.register(OTHER, "小雪", "测试达人")
    desk.prm.set_active(party(desk, OTHER)["id"], False)
    assert desk.admin("合作单 小雪") == "「小雪」是停用的达人（不在达人名单里）：没做，请到控制台看"
    desk.prm.register(FOURTH, "小雪儿", "测试达人")
    desk.prm.set_active(party(desk, FOURTH)["id"], False)
    assert desk.admin("合作单 雪儿") == "「雪儿」是停用的达人（不在达人名单里）：没做，请到控制台看"
    assert desk.admin("合作单 小雪").startswith("「小雪」是停用的达人")       # (her whole name: never 小雪儿)
    desk.prm.store.update_deal(deal(desk)["id"], clock.now, purged_at=clock.now)
    assert desk.admin("合作单 小美") == "「小美」的合作单收件信息已经按规定清过了：没做，请到控制台看"
    desk.prm.register(FIFTH, "抖音-小美美", "测试达人")
    assert desk.admin("接管 小美") == "「小美」的合作单收件信息已经按规定清过了：没做，请到控制台看"   # (item 1)
    assert deal(desk, FIFTH)["mode"] == "copilot"


# ── whose line, which kind of message, how old ────────────────────────────

def test_only_the_admins_typed_line_in_the_admins_own_chat_runs_one(desk, clock):
    assert desk.admin("签收 小美", sender=CREATOR, chat=CREATOR) is None              # (her own chat: hers)
    assert desk.admin("签收 小美", sender="wxid_stranger", chat="wxid_stranger") is None
    assert desk.admin("签收 小美", chat="300000001@chatroom", group=True) is None
    assert desk.admin("签收 小美", quote="#3 草稿 · 小美 · 私聊") == "引用的消息不算合作单指令：不引用，直接打字发「签收 小美」"
    assert desk.admin("[语音]签收 小美。", msg_type=34) == "合作单指令要打字发（语音不算，免得听错）：比如「签收 小美」"
    assert desk.admin("签收 小美", msg_type=3) == "只认打字发的合作单指令：比如「签收 小美」"
    assert deal(desk)["stage"] == "talk" and events(desk, "stage") == []
    assert desk.admin("签收 小美\n还有别的") == ac.MORE_TEXT
    assert deal(desk)["stage"] == "talk"


def test_how_a_message_is_read_by_itself(desk):
    """(Review 2026-11-11: the route's earlier steps stop most of these; ``_how`` holds on its own.)"""
    commands = desk.prm.deal_commands
    for msg in (admin_msg("签收 小美", is_group=True), admin_msg("签收 小美", sender_id=CREATOR),
                admin_msg("签收 小美", chat_id=CREATOR), admin_msg("签收 小美", chat_id="300000001@chatroom"),
                admin_msg("签收 小美", sender_id="")):
        assert commands.admin_command("签收 小美", msg) is None, msg
    no_group_key = admin_msg("签收 小美")
    del no_group_key["is_group"]                                          # (unknown: as a group, never ours)
    assert commands.admin_command("签收 小美", no_group_key) is None
    assert commands._how(admin_msg("x")) == "" and commands._how(None) == ""
    assert commands._how(admin_msg("x", msg_type=34)) == "voice"
    assert commands._how(admin_msg("x", quoted_content="wxid_abc: 随便一条")) == "quote"
    assert commands._how(admin_msg("x", msg_type=49)) == "other" and commands._how(admin_msg("x", msg_type="?")) == "other"
    assert deal(desk)["stage"] == "talk"


def test_nothing_runs_while_admin_wxid_is_a_creators(clock, tmp_path):
    d = make(clock, tmp_path)
    try:
        d.prm.store.add_party(ADMIN, "测试管理员", "", clock.now)       # (an ADMIN_WXID that is a creator's: her words)
        assert d.prm.deal_commands.admin_command("签收 小美", admin_msg("签收 小美")) is None
    finally:
        d.stop()


def test_a_command_read_late_is_not_run(desk, clock):
    """(Review 2026-11-11, item 3.)  A line WeChat reads more than MAX_AGE_SEC after it was written (a re-login syncing
    old lines) is not run, one line says so; the PC's clock against WeChat's (``PrmService._local``) counts."""
    reply = desk.admin("接管 小美", at=clock.now - 3600)
    assert reply == "这条合作单指令是 60 分钟前写的（微信这会儿才读到），太久了，没做：要做的话请再发一次"
    assert deal(desk)["mode"] == "copilot"
    desk.prm.register(OTHER, "小雨", "测试达人")
    desk.prm.register(FOURTH, "小雨", "测试达人")
    desk.admin("签收 小雨")
    assert desk.admin("选 1", at=clock.now - MAX_AGE_SEC - 60).startswith("这条合作单指令是 6 分钟前写的")
    assert deal(desk, OTHER)["stage"] == "talk"
    assert desk.admin("接管 小美", at=clock.now - MAX_AGE_SEC + 30).startswith("抖音-小美：人工接管了")
    desk.prm._skew = -3600.0                                              # (this PC an hour behind WeChat)
    assert desk.admin("副驾驶 小美", at=clock.now + 3600 - 60).startswith("抖音-小美：恢复副驾驶了")


def test_with_relay_only_a_command_stays_one_and_ordinary_sentences_go_to_the_mac(clock, tmp_path):
    d = make(clock, tmp_path, relay_only="on")
    try:
        assert d.admin("合作单 小美").startswith("抖音-小美（合作单 ") and jobs(d) == []
        assert d.admin("做手卡怎么用").startswith("收到，已交给 Mac")       # (a task for the Mac's Claude)
        for text in ("签收了吗", "签收 的流程是怎样的", "接管 以后多久恢复", "做手卡 要用到哪些数据"):
            assert d.admin(text).startswith("收到"), text                  # (... or a follow-up of it)
        assert jobs(d) and all(j["kind"] == "admin" and j["chat_id"] == ADMIN for j in jobs(d))
        # an ordinary sentence with a blank after a command word and no question in it: answered here, not to Claude
        before = len(jobs(d))
        assert d.admin("合作单 帮我导出一下").startswith("没有达人对得上「帮我导出一下」")
        assert len(jobs(d)) == before and deal(d)["stage"] == "talk"
    finally:
        d.stop()


def test_no_collision_with_jie_qun_or_the_draft_answers(clock, tmp_path):
    """接群's 「接群」「是 N」「不接」 and 微信里审批草稿's 1 / 2 / 发 / 执行 stay theirs; 「选 N」 is ours only with a list."""
    d = make(clock, tmp_path, review="on")
    try:
        d.router._admin.set_group_links(d.prm.group_links)
        commands = d.prm.deal_commands
        for text in ("接群", "是 1", "不接", "1", "2", "发 12", "不发 12", "执行", "执行 12", "改 12 好的", "对过图 1"):
            assert commands.admin_command(text) is None, text
        d.prm.register(OTHER, "小雨", "测试达人")
        d.prm.register(FOURTH, "小雨", "测试达人")
        d.admin("合作单 小雨")
        for text in ("是 1", "不接", "1", "2"):
            assert commands.admin_command(text) is None, text
        d.her("老师您好")
        packet = d.mac()
        d.review.sweep()
        head = next(t for t in d.told() if t.startswith(f"#{packet['id']} 草稿")).split("\n")[0]
        assert d.admin("签收 小美").startswith("抖音-小美：")              # (a command: never an answer to the draft)
        assert d.admin("合作单 小美", quote=head) == "引用的消息不算合作单指令：不引用，直接打字发「合作单 小美」"
        assert d.prm.store.packet(packet["id"])["status"] == "pending"
        assert d.admin("1", quote=head) == f"#{packet['id']} 已批准，正在发给 抖音-小美"   # (the review's, as before)
    finally:
        d.stop()


# ── what is kept: the log, the trace, the echo ────────────────────────────

def test_a_refusal_echoes_no_note_and_no_number(desk):
    """(Review 2026-11-11, item 8.)"""
    reply = desk.admin("备注 小美 她电话13812345678", quote="随便一条")
    assert reply == "引用的消息不算合作单指令：不引用，直接打字发「备注 小美 …」"
    assert "13812345678" not in desk.admin("签收 13812345678")
    assert deal(desk)["draft_note"] == ""


def test_the_decision_trace_keeps_no_name_nor_note(desk, tmp_path):
    """(Review 2026-11-11, item 8.)  The route step redacts a 合作单指令 line as the review's step does; other lines of
    the admin are kept as before."""
    from src.decision_trace import DecisionTracer, JsonlSink

    sink = JsonlSink(tmp_path / "decision_trace.jsonl")
    desk.router._tracer = DecisionTracer(sink, content_chars=200)
    desk.admin("备注 小美 她住北京市朝阳区测试路8号 13812345678")
    desk.admin("合作单 小美")
    desk.admin("[语音]签收 小美", msg_type=34)
    desk.admin("你好")
    records = [json.loads(line) for line in sink.path.read_text(encoding="utf-8").splitlines()]
    assert [r["content"] for r in records] == [TRACE_TEXT] * 3 + ["你好"]
    assert "小美" not in sink.path.read_text(encoding="utf-8")


def test_the_log_names_nobody(desk, caplog):
    desk.prm.register(OTHER, "小美美", "测试达人")
    with caplog.at_level(logging.INFO):
        desk.admin("备注 小美 只发链接")
        desk.admin("签收 美美")
        desk.admin("做手卡 张三")
        desk.admin("补发货盘 小美")
        desk.admin("写开场白 小美美")
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("src.prm"))
    assert "合作单指令" in text and "PRM: party" in text
    for word in ("小美", "张三", "只发链接", CREATOR, OTHER):
        assert word not in text, word


# ── the switch off: nothing changes ───────────────────────────────────────

@pytest.mark.parametrize("relay_only", ["off", "on"])
def test_with_the_switch_off_every_line_goes_where_it_went(clock, tmp_path, relay_only):
    d = make(clock, tmp_path, commands="off", relay_only=relay_only)
    try:
        assert d.prm.deal_commands.on is False and d.router._admin._deal_commands is None
        assert d.prm.deal_commands.admin_command("签收 小美") is None
        assert not d.router._admin.is_command("签收 小美") and not d.router._admin.is_command("指令")
        assert d.router._admin.redaction("签收 小美") is None
        helped = d.admin("帮助")
        assert helped == HELP_TEXT if relay_only == "off" else HELP_LINE not in helped
        replies = [d.admin(text) for text in ("签收 小美", "指令", "做手卡 小美")]
        if relay_only == "off":
            assert replies == [None, None, None] and jobs(d) == []
        else:                                                             # (tasks for the Mac's Claude, as before)
            assert all(r.startswith("收到") for r in replies) and jobs(d)
            assert all(j["kind"] == "admin" and j["chat_id"] == ADMIN for j in jobs(d))
        assert deal(d)["stage"] == "talk" and events(d, "stage") == [] and events(d, "handcards") == []
    finally:
        d.stop()


def test_the_bot_hands_the_commands_over_only_with_the_switch(desk):
    from unittest.mock import MagicMock

    from src.bot import Bot
    from src.config import BotConfig

    for on in (True, False):
        handler = AdminCommandHandler()
        commands = SimpleNamespace(on=on, help_line=HELP_LINE, admin_command=desk.prm.deal_commands.admin_command)
        prm = SimpleNamespace(friend_adds=MagicMock(), three_party=MagicMock(), group_links=None, deal_commands=commands)
        Bot(BotConfig(admin_wxid=ADMIN))._wire_friend_adds(BotConfig(admin_wxid=ADMIN), SimpleNamespace(), handler, prm)
        assert (handler._deal_commands is commands) is on
        assert handler.handle("帮助") == HELP_TEXT + (HELP_LINE if on else "")


@pytest.mark.parametrize("copilot, commands, named", [("on", "on", True), ("on", "off", False), ("off", "on", False)])
def test_the_relays_help_names_them_only_with_the_switch_and_the_copilot(monkeypatch, copilot, commands, named):
    from src import relay as relay_module
    from src.bot import Bot
    from src.config import BotConfig
    from src.web import server

    seen = {}

    class FakeRelay:
        def __init__(self, config, **kw):
            seen.update(kw)

        def start(self):
            pass

    monkeypatch.setattr(relay_module, "ClaudeRelay", FakeRelay)
    monkeypatch.setattr(server, "register_relay", lambda relay: None)
    router = SimpleNamespace(_admin=AdminCommandHandler())
    config = BotConfig(claude_relay="on", prm_copilot=copilot, prm_wechat_commands=commands, admin_wxid=ADMIN)
    Bot._start_claude_relay(config, router, SimpleNamespace(send_text=lambda chat, text: True))
    assert seen["extra_help"] == RELAY_HELP + (HELP_LINE if named else "")


def test_a_stopped_bot_still_refuses_what_it_refused(desk, clock):
    """The stopped console's path is apply_deal_action as before: the buttons that need the running bot say so."""
    deal_id = deal(desk)["id"]
    assert apply_deal_action(desk.prm.store, "handcards", {"deal_id": deal_id}, now=clock.now, takeover_sec=7200) == {
        "ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能做手卡"}
    assert apply_deal_action(desk.prm.store, "stage", {"deal_id": deal_id, "stage": "delivered"}, now=clock.now,
                             takeover_sec=7200) == {"ok": True}
    assert [e["actor"] for e in events(desk, "stage")] == ["console"]
