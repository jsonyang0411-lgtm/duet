"""PRM copilot, the second part: takeovers, send gates (8.1), holding replies, shipments,
numbers shown in full and the 30-day clean-up (8.2), and what the backend tells about own lines."""

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_prm import ADMIN, CREATOR, Bot, Clock, draft, pending_packet

from src.config import BotConfig, _validate_config
from src.db.schema import initialize_db
from src.db.store import MessageStore
from src.prm import gates, pii, retention
from src.prm.deals import notice_text
from src.prm.service import HOLDING_TEXT, OWN_FILE_SEC, SETTLE_SEC, PrmService, apply_deal_action, reveal_numbers
from src.prm.store import PrmStore
from src.relay.store import PRM
from src.wechat.wcdb_backend import WcdbBackend

DAY = 86400


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bot(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def own(b, text, ts=None, msg_type=1):
    """A line this WeChat account sent in the creator's chat (what the backend hands on)."""
    return {"message_id": f"own{time.time_ns()}", "chat_id": CREATOR, "sender_id": "wxid_bot", "sender_name": "我",
            "content": text, "msg_type": msg_type, "is_group": False, "timestamp": b.clock.now if ts is None else ts}


def drafted(b, d=None, text="我是小美，手机 13812345678"):
    """The creator writes, the Mac drafts: the packet."""
    b.prm.handle(b.msg(text))
    job = b.worker_takes()
    b.finish(job["id"], d or draft())
    return b.prm.store.packet_for_job(job["id"])


# ── an operator replying by hand takes the chat over ──────────────────────

def test_the_bots_own_line_confirms_its_packet(bot):
    packet = drafted(bot)
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_unconfirmed"
    bot.prm.on_own_message(own(bot, "确认一下：小美，手机13812345678。"))      # WeChat shows what was sent
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.deal(1)["mode"] == "copilot"                         # no takeover


def test_a_hand_typed_reply_gives_the_chat_to_a_person(bot, clock):
    packet = drafted(bot)
    bot.prm.on_own_message(own(bot, "小美你好，我是运营小王，电话 13900001111 有事找我"))
    deal = bot.prm.store.deal(1)
    assert deal["mode"] == "human" and deal["mode_until"] == clock.now + 120 * 60
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    [takeover, line] = bot.prm.store.events(1, limit=2)
    assert takeover["kind"] == "takeover" and line["kind"] == "operator_message"
    assert "〈手机" in line["detail"]["text"] and "13900001111" not in line["detail"]["text"]
    # while a person has it: recorded, never drafted
    bot.prm.handle(bot.msg("好的那我等你消息"))
    assert [j["id"] for j in bot.relay.store.list_jobs()] == [1]
    assert bot.prm.store.events(1, limit=1)[0]["kind"] == "creator_message"
    # time is up: copilot again, and the next draft hears what both sides said
    clock.now += 121 * 60
    bot.prm.handle(bot.msg("我收到样品啦"))
    job = bot.relay.store.get(2)
    history = json.loads(job["meta"])["history"]
    assert [h["who"] for h in history] == ["operator", "creator"]
    assert "运营小王" in history[0]["text"] and "等你消息" in history[1]["text"]
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    assert any(e["kind"] == "mode_expired" for e in bot.prm.store.events(1))
    # told once: the creator writing on while job 2 runs is a follow-up, and if job 2
    # ends before reading it, the job made of it does not repeat the history
    job = bot.worker_takes()
    assert job["id"] == 2 and job["meta"]["history"] == history
    bot.prm.handle(bot.msg("在吗"))
    assert bot.relay.store.followups(2, 0)[0]["text"].endswith("在吗")
    bot.finish(2, draft())
    leftover = bot.relay.store.get(3)
    assert leftover["text"].endswith("在吗") and leftover["kind"] == PRM
    assert "history" not in json.loads(leftover["meta"]) and json.loads(leftover["meta"])["deal_id"] == 1


def test_old_own_lines_and_the_bots_files_are_not_a_takeover(bot, clock):
    bot.prm.on_own_message(own(bot, "上周说过的话", ts=clock.now - 600))      # history of a chat read first
    bot.prm._note_sent(CREATOR, file=True)
    clock.now += OWN_FILE_SEC - 1
    bot.prm.on_own_message(own(bot, "[图片]", msg_type=3))                    # the brief the bot just sent
    bot.prm.on_own_message(own(bot, HOLDING_TEXT))                            # never sent: a person typed it
    assert bot.prm.store.deal(1)["mode"] == "human"
    events = [e["kind"] for e in bot.prm.store.events(1)]
    assert events.count("takeover") == 1


def test_a_holding_reply_and_a_paused_deal_are_not_a_takeover(bot, clock):
    drafted(bot)
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1
    bot.prm.on_own_message(own(bot, HOLDING_TEXT))                            # the bot's own holding reply
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    apply_deal_action(bot.prm.store, "mode", {"deal_id": 1, "mode": "paused"}, now=clock.now, takeover_sec=7200)
    bot.prm.on_own_message(own(bot, "我亲手回一句"))
    assert bot.prm.store.deal(1)["mode"] == "paused"                          # paused stays paused
    kinds = [e["kind"] for e in bot.prm.store.events(1)]
    assert kinds[0] == "operator_message" and "takeover" not in kinds         # recorded for the next draft


def test_after_a_restart_only_lines_wechat_has_not_shown_yet_are_the_bots(bot, clock):
    first = drafted(bot, draft(message_kind="greeting", reply_draft="好的呀", slots={}))
    bot.prm.decide(first["id"], "send")
    bot.prm.send_approved()
    bot.prm.on_own_message(own(bot, "好的呀"))                                # read back: confirmed
    second = drafted(bot, draft(message_kind="greeting", reply_draft="稍等哈", slots={}), text="在吗")
    bot.prm.decide(second["id"], "send")
    bot.prm.send_approved()
    clock.now += 60
    restarted = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=clock, send=bot._send)
    restarted.on_own_message(own(bot, "稍等哈"))                              # sent before the restart
    assert bot.prm.store.packet(second["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    restarted.on_own_message(own(bot, "好的呀"))                              # already seen: a person typed it
    assert bot.prm.store.deal(1)["mode"] == "human"


def test_a_draft_finishing_while_a_person_has_the_chat_is_not_offered(bot):
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    bot.prm.on_own_message(own(bot, "我来回吧"))
    bot.finish(job["id"], draft())
    assert bot.prm.store.packet_for_job(job["id"])["status"] == "superseded"
    assert bot.prm.store.deal(1)["slots"]["收件人"] == "小美"                 # what it learned still counts


# ── 8.1: send gates ───────────────────────────────────────────────────────

def review_many(bot, kind, n, *, edit=None, d=None):
    for i in range(n):
        packet = drafted(bot, d or draft(message_kind=kind, reply_draft=f"第{i}条：好的", slots={}), text=f"消息{i}")
        if edit is not None and i == 0:
            assert bot.prm.decide(packet["id"], "edit", edit(packet["text"]))["ok"]
        else:
            assert bot.prm.decide(packet["id"], "send")["ok"]
        bot.prm.send_approved()


def test_a_kind_goes_auto_after_18_of_20_sent_unchanged(bot):
    review_many(bot, "greeting", 19)
    assert bot.prm.store.gate("greeting")["route"] == "review"
    review_many(bot, "greeting", 1)
    assert bot.prm.store.gate("greeting")["route"] == "auto"
    packet = drafted(bot, draft(message_kind="greeting", reply_draft="好的收到～", slots={}), text="收到")
    assert packet["route"] == "auto" and packet["status"] == "approved"
    bot.prm.send_approved()
    assert bot.texts[-1] == (CREATOR, "好的收到～")
    # still a person's when the draft itself is not plain
    flagged = drafted(bot, draft(message_kind="greeting", reply_draft="好的", needs_attention=True, slots={}))
    assert flagged["route"] == "review" and flagged["status"] == "pending"


def test_an_edit_of_a_key_fact_keeps_the_kind_reviewed(bot):
    review_many(bot, "greeting", 20, edit=lambda t: t.replace("第0条", "第9条"))
    stats = gates.window_stats(bot.prm.store.reviewed("greeting", 0, 20))
    assert stats["unchanged"] == 19 and stats["key_changed"] is True
    assert bot.prm.store.gate("greeting")["route"] == "review"


def test_money_and_complaints_are_always_reviewed(bot):
    review_many(bot, "money", 20, d=draft(message_kind="money", reply_draft="佣金是 20%", slots={}))
    assert bot.prm.store.gate("money")["route"] == "review" and bot.prm.store.gate("complaint")["locked"] == 1
    assert bot.prm.store.gate("address_confirm")["window"] == 30 and bot.prm.store.gate("address_confirm")["min_unchanged"] == 29
    assert bot.prm.store.gate("material")["min_unchanged"] == 19


def test_marked_wrong_the_kind_is_reviewed_again_from_zero(bot, clock):
    review_many(bot, "greeting", 20)
    sent = bot.prm.store.packets(("sent",), limit=1)[0]
    clock.now += 1
    assert bot.prm.decide(sent["id"], "wrong") == {"ok": True, "status": "wrong"}
    assert bot.prm.decide(sent["id"], "wrong")["ok"] is False
    gate = bot.prm.store.gate("greeting")
    assert gate["route"] == "review" and gate["reset_at"] == clock.now
    assert gates.window_stats(bot.prm.store.reviewed("greeting", gate["reset_at"], 20))["decided"] == 0
    assert bot.prm.decide(drafted(bot)["id"], "wrong")["ok"] is False      # only sent ones


def test_a_locked_kind_never_goes_auto(bot, clock):
    full = {"decided": 20, "unchanged": 20, "key_changed": False}
    assert gates.earned({"locked": 0, "window": 20, "min_unchanged": 18}, full)
    assert not gates.earned({"locked": 1, "window": 20, "min_unchanged": 18}, full)
    bot.prm.store.set_gate("money", "auto", clock.now)                         # even if the table says so
    packet = drafted(bot, draft(message_kind="money", reply_draft="佣金 20%", slots={}))
    assert packet["route"] == "review" and packet["status"] == "pending"


def test_plain_reason_rules():
    base = dict(needs_attention=False, mode="copilot", unknown_tokens=[], reason="kb/合作政策.md", text="x",
                waybill="", attachments=[])
    assert gates.plain_reason("greeting", **base) == ""
    assert gates.plain_reason("faq", **{**base, "reason": "我觉得"}) == "回答没有注明 kb/ 出处"
    assert gates.plain_reason("greeting", **{**base, "mode": "human"})
    assert gates.plain_reason("greeting", **{**base, "unknown_tokens": ["〈手机3〉"]})
    assert gates.plain_reason("material", **base) == "素材文件没有收到"
    assert gates.plain_reason("shipping_notice", **{**base, "text": "已寄出 SF123456789", "waybill": "SF123456789"}) == ""
    assert gates.plain_reason("shipping_notice", **{**base, "text": "已寄出 SF999999999", "waybill": "SF123456789"})
    assert gates.waybill_ok("单号 SF123456789，另一个 7700112233", "SF123456789") is False
    assert gates.key_change("佣金 20%，3 天内", "佣金 25%，3 天内") and not gates.key_change("好的呀", "好的～")


# ── 8.1: the holding reply ────────────────────────────────────────────────

def test_one_holding_reply_per_round_of_messages(bot, clock):
    drafted(bot)
    clock.now += 9 * 60
    assert bot.prm.send_holding_replies() == 0
    clock.now += 2 * 60
    assert bot.prm.send_holding_replies() == 1 and bot.texts == [(CREATOR, HOLDING_TEXT)]
    clock.now += 30 * 60
    assert bot.prm.send_holding_replies() == 0                                # once per round
    assert bot.prm.store.packets(("pending",))[0]["status"] == "pending"      # the draft still waits
    drafted(bot, text="还在吗")                                                # a new round
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1 and len(bot.texts) == 2


def test_no_holding_reply_while_a_person_has_the_chat_or_when_off(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm.register(CREATOR, "小美")
    drafted(b)
    b.prm.deal_action("mode", {"deal_id": 1, "mode": "human"})               # from the console: the draft stays
    clock.now += 60 * 60
    assert b.prm.store.packets(("pending",)) and b.prm.send_holding_replies() == 0
    b.prm.deal_action("mode", {"deal_id": 1, "mode": "paused"})
    assert b.prm.send_holding_replies() == 0 and b.texts == []
    b.relay.stop()
    off = Bot(clock, tmp_path / "off")
    off.prm._holding_sec = 0
    off.prm.register(CREATOR, "小美")
    drafted(off)
    clock.now += 60 * 60
    assert off.prm.send_holding_replies() == 0
    off.relay.stop()


# ── shipments, stages and modes from the console ──────────────────────────

def test_a_waybill_drafts_the_shipping_notice_with_the_systems_number(bot, clock):
    assert apply_deal_action(bot.prm.store, "shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "x"},
                             now=clock.now, takeover_sec=7200)["ok"] is False
    result = bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1234567890123"})
    packet = bot.prm.store.packet(result["packet"])
    assert packet["kind"] == "shipping_notice" and packet["text"] == notice_text("小美", "顺丰", "SF1234567890123")
    assert packet["meta"]["waybill"] == "SF1234567890123" and packet["meta"]["source"] == "system"
    assert bot.prm.store.deal(1)["stage"] == "shipped"
    wrong = bot.prm.decide(packet["id"], "edit", "已寄出：顺丰 SF0000000000000")
    assert wrong["ok"] is False and "SF1234567890123" in wrong["error"]
    assert bot.prm.decide(packet["id"], "edit", "小美～样品寄出啦，顺丰 SF1234567890123")["ok"]
    bot.prm.send_approved()
    assert bot.texts[-1] == (CREATOR, "小美～样品寄出啦，顺丰 SF1234567890123")


def test_a_model_written_shipping_notice_must_carry_the_systems_number(bot):
    bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1234567890123"})
    packet = drafted(bot, draft(message_kind="shipping_notice", reply_draft="已寄出 SF9999999999999", slots={}))
    assert "单号和系统记录对不上" in bot.prm.store.deal(1)["attention"]
    assert bot.prm.decide(packet["id"], "send")["ok"] is False


def test_the_shipping_notice_outlives_the_creators_messages_but_not_a_new_waybill(bot, clock):
    first = bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1111111111"})["packet"]
    drafted(bot, draft(message_kind="greeting", reply_draft="好的呀", slots={}), text="好的谢谢")
    assert bot.prm.store.packet(first)["status"] == "pending"                 # still waits for a person
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1                                # for the creator's message only
    second = bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "中通", "waybill": "ZT2222222222"})["packet"]
    assert bot.prm.store.packet(first)["status"] == "superseded"
    assert bot.prm.store.packet(second)["status"] == "pending"
    # a notice of the earlier waybill (the Mac wrote it before the change) cannot go out
    old = drafted(bot, draft(message_kind="shipping_notice", reply_draft="单号 SF1111111111", slots={}))
    assert "系统记录：ZT2222222222" in bot.prm.decide(old["id"], "send")["error"]
    assert bot.prm.decide(second, "send")["ok"]


def test_an_approved_notice_is_not_sent_once_the_waybill_changed(bot):
    first = bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1111111111"})["packet"]
    assert bot.prm.decide(first, "send")["ok"]                                # approved, not sent yet
    bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "中通", "waybill": "ZT2222222222"})
    bot.prm.send_approved()
    packet = bot.prm.store.packet(first)
    assert packet["status"] == "failed" and "单号和系统记录对不上" in packet["error"] and bot.texts == []


def test_a_system_notice_alone_gets_no_holding_reply(bot, clock):
    bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1111111111"})
    clock.now += 60 * 60
    assert bot.prm.send_holding_replies() == 0 and bot.texts == []


def test_going_back_before_delivery_restarts_the_clean_up_clock(bot, clock):
    delivered_deal(bot, clock)
    clock.now += 10 * DAY
    assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": "ship_error"})["ok"]   # signed by a neighbour
    assert bot.prm.store.deal(1)["delivered_at"] is None
    clock.now += 25 * DAY
    assert bot.prm.purge_due() == 0
    bot.prm.deal_action("shipment_status", {"deal_id": 1, "status": "delivered"})
    assert bot.prm.store.deal(1)["delivered_at"] == clock.now
    bot.prm.deal_action("stage", {"deal_id": 1, "stage": "published"})           # later stages keep it
    assert bot.prm.store.deal(1)["delivered_at"] == clock.now


def test_stage_adopt_mode_and_logistics(bot, clock):
    drafted(bot)                                                              # proposes "address"
    assert bot.prm.deal_action("adopt", {"deal_id": 1}) == {"ok": True}
    deal = bot.prm.store.deal(1)
    assert deal["stage"] == "address" and deal["stage_proposal"] == ""
    assert bot.prm.deal_action("adopt", {"deal_id": 1})["ok"] is False
    assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": "won"})["ok"] is False
    assert bot.prm.deal_action("stage", {"deal_id": 9, "stage": "agreed"})["ok"] is False
    assert bot.prm.deal_action("mode", {"deal_id": 1, "mode": "human"})["ok"]
    assert bot.prm.store.deal(1)["mode_until"] == clock.now + 7200
    assert bot.prm.deal_action("mode", {"deal_id": 1, "mode": "copilot"})["ok"]
    assert bot.prm.store.deal(1)["mode_until"] is None
    assert bot.prm.deal_action("shipment_status", {"deal_id": 1, "status": "delivered"})["ok"] is False   # no waybill
    bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1234567890123"})
    for status in ("transit", "delivered"):
        assert bot.prm.deal_action("shipment_status", {"deal_id": 1, "status": status})["ok"]
    deal = bot.prm.store.deal(1)
    assert deal["stage"] == "delivered" and deal["delivered_at"] == clock.now
    assert bot.prm.store.latest_shipment(1)["status"] == "delivered"
    kinds = [e["kind"] for e in bot.prm.store.events(1, limit=50)]
    assert {"stage", "mode", "shipment", "shipment_status"} <= set(kinds)
    assert all(e["actor"] == "console" for e in bot.prm.store.events(1) if e["kind"] in ("stage", "mode"))


# ── 8.2: numbers in full, and the clean-up 30 days after delivery ─────────

def test_the_full_numbers_are_shown_on_request_and_every_look_is_written_down(bot, clock):
    drafted(bot, text="收件人小美 13812345678")
    answer = reveal_numbers(bot.prm.store, 1, now=clock.now)
    assert answer["numbers"] == [{"token": "〈手机1〉", "value": "13812345678", "cleared": False}]
    [event] = [e for e in bot.prm.store.events(1) if e["kind"] == "reveal"]
    assert event["detail"] == {"tokens": ["〈手机1〉"]} and "13812345678" not in json.dumps(event)
    assert reveal_numbers(bot.prm.store, 99, now=clock.now)["ok"] is False


def delivered_deal(bot, clock, address="上海市浦东新区张江路100号3栋1201"):
    store = MessageStore(initialize_db(":memory:"))
    bot.prm._scrub_messages = store.rewrite_chat
    text = f"收件人小美，电话 138 1234 5678，地址：{address}"
    store.insert_message({"message_id": "m1", "chat_id": CREATOR, "sender_id": CREATOR, "sender_name": "小美",
                          "content": text, "msg_type": 1, "timestamp": int(clock.now)})
    packet = drafted(bot, draft(slots={"收件人": "小美", "手机": "〈手机1〉", "地址": address},
                                reply_draft=f"确认：小美 〈手机1〉 {address}", intent=f"寄到{address}",
                                reason=f"地址：{address}，信息齐了"), text=text)
    bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1234567890123"})
    bot.prm.deal_action("shipment_status", {"deal_id": 1, "status": "delivered"})
    return store, packet


def test_thirty_days_after_delivery_every_copy_is_cut_down(bot, clock):
    messages, packet = delivered_deal(bot, clock)
    # a later draft quotes the address without its beginning (seen with the real model)
    drafted(bot, draft(slots={}, reply_draft="再确认一下：张江路100号3栋1201，收件人小美"), text="好的")
    clock.now += 29 * DAY
    assert bot.prm.purge_due() == 0
    clock.now += 2 * DAY
    assert bot.prm.purge_due() == 1 and bot.prm.purge_due() == 0              # once
    deal = bot.prm.store.deal(1)
    assert deal["slots"]["地址"] == "上海市（详细地址已清除）" and deal["purged_at"] == clock.now
    assert bot.prm.store.tokens(1) == {}                                      # no number to put back
    rows = bot.prm.store.token_rows(1)
    assert rows[0]["value"] == "" and rows[0]["masked"] == "138****5678"
    cut = bot.prm.store.packet(packet["id"])
    assert "张江路" not in cut["text"] + cut["intent"] + cut["reason"] + json.dumps(cut["slots"], ensure_ascii=False)
    assert cut["intent"] == "寄到上海市（详细地址已清除）"
    everything = json.dumps([bot.prm.store.packets(limit=100), bot.prm.store.events(1, limit=100),
                             bot.relay.store.list_jobs(100)], ensure_ascii=False)
    assert "张江路" not in everything and "13812345678" not in everything
    stored = messages.conn.execute("SELECT content FROM messages WHERE chat_id = ?", (CREATOR,)).fetchone()[0]
    assert "张江路" not in stored and "1234 5678" not in stored and "138****5678" in stored
    purge_job = bot.relay.store.list_jobs(1)[0]
    assert purge_job["kind"] == PRM and json.loads(purge_job["meta"])["op"] == "purge" and purge_job["text"] == ""
    assert reveal_numbers(bot.prm.store, 1, now=clock.now)["numbers"][0]["cleared"] is True
    shown = pii.display(bot.prm.store.token_rows(1))
    assert shown["〈手机1〉"] == "138****5678（已清除）"
    [party] = bot.prm.snapshot()["parties"]
    assert party["has_numbers"] is True and party["deal"]["purged_at"] == clock.now   # the console still lists them
    # the Mac's answer to the purge job is recorded
    job = bot.relay.next_job(0, ("prm",))
    bot.relay.finish(job["id"], {"ok": True, "text": "案卷已清理，改了 2 行"})
    bot.relay.tick()
    assert bot.prm.store.events(1, limit=1)[0]["kind"] == "purge_case"


def test_the_creator_writing_around_the_purge_job_still_gets_a_draft(bot, clock):
    delivered_deal(bot, clock)
    clock.now += 31 * DAY
    bot.prm.purge_due()
    purge = bot.relay.store.list_jobs(1)[0]
    bot.prm.handle(bot.msg("样品很好用！"))                                  # the purge job is still queued
    message_job = bot.relay.store.list_jobs(1)[0]
    assert message_job["id"] == purge["id"] + 1 and bot.relay.store.get(purge["id"])["text"] == ""
    taken = bot.relay.next_job(0, ("prm",))
    assert taken["id"] == purge["id"] and taken["meta"]["op"] == "purge"
    bot.prm.handle(bot.msg("什么时候发素材"))                                 # while the purge job runs
    assert bot.relay.store.followups(purge["id"], 0) == []
    assert "什么时候发素材" in bot.relay.store.get(message_job["id"])["text"]


def test_a_cleared_number_is_never_sent_as_a_placeholder(bot, clock):
    delivered_deal(bot, clock)
    late = drafted(bot, draft(reply_draft="再确认下 〈手机1〉", slots={}), text="还有事")
    clock.now += 31 * DAY
    bot.prm.purge_due()
    assert bot.prm.decide(late["id"], "send")["ok"] is False


def test_an_older_databases_full_text_index_forgets_the_old_words():
    """A messages.db of the chat bot can still hold imported history and its full-text index
    (FTS5, external content, filled up to a cursor in memory_state): both are cleaned too."""
    import sqlite3
    conn = initialize_db(":memory:")
    store = MessageStore(conn)
    try:
        for index, table in (("fts_messages", "messages"), ("fts_history", "history_messages")):
            if table == "history_messages":
                conn.execute("CREATE TABLE history_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT "
                             "UNIQUE NOT NULL, chat_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT "
                             "NULL, content TEXT NOT NULL DEFAULT '', msg_type INTEGER NOT NULL DEFAULT 1, source "
                             "TEXT NOT NULL DEFAULT '', timestamp INTEGER NOT NULL, imported_at INTEGER NOT NULL "
                             "DEFAULT 0)")
            conn.execute(f"CREATE VIRTUAL TABLE {index} USING fts5(content, content='{table}', "
                         "content_rowid='id', tokenize='trigram')")
    except sqlite3.OperationalError:
        pytest.skip("this sqlite has no fts5 trigram")
    conn.execute("CREATE TABLE memory_state (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
    store.insert_message({"message_id": "m1", "chat_id": CREATOR, "sender_id": CREATOR, "sender_name": "小美",
                          "content": "地址：上海市浦东新区张江路100号", "msg_type": 1, "timestamp": 1})
    conn.execute("INSERT INTO history_messages (message_id, chat_id, sender_id, sender_name, content, timestamp) "
                 "VALUES ('h1', ?, ?, '小美', '旧地址：上海市浦东新区张江路100号', 1)", (CREATOR, CREATOR))
    for index, table in (("fts_messages", "messages"), ("fts_history", "history_messages")):
        conn.execute(f"INSERT INTO {index}(rowid, content) SELECT id, content FROM {table}")
        conn.execute("INSERT INTO memory_state(key, value) VALUES (?, '1')", (f"{index}.cursor",))
    conn.commit()
    assert store.rewrite_chat(CREATOR, lambda t: retention.scrub(t, [])) == 2
    for index, table in (("fts_messages", "messages"), ("fts_history", "history_messages")):
        assert conn.execute(f"SELECT rowid FROM {index} WHERE {index} MATCH '\"张江路\"'").fetchall() == []
        assert conn.execute(f"SELECT rowid FROM {index} WHERE {index} MATCH '\"上海市\"'").fetchall()
        assert "张江路" not in conn.execute(f"SELECT content FROM {table}").fetchone()[0]
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_new_database_has_only_the_live_messages_to_clean():
    conn = initialize_db(":memory:")
    store = MessageStore(conn)
    store.insert_message({"message_id": "m1", "chat_id": CREATOR, "sender_id": CREATOR, "sender_name": "小美",
                          "content": "电话 13812345678", "msg_type": 1, "timestamp": 1})
    assert store.rewrite_chat(CREATOR, lambda t: retention.scrub(t, [])) == 1
    assert conn.execute("SELECT content FROM messages").fetchone()[0] == "电话 138****5678"
    assert store.rewrite_chat("wxid_nobody", lambda t: "x") == 0


def test_scrub_cuts_addresses_and_masks_numbers():
    text = ("地址：上海市浦东新区张江路100号3栋1201，电话 138-1234-5678。上海市场不错。"
            "收货地址：杭州西湖区文三路90号；身份证 11010119900307123x")
    out = retention.scrub(text, [])
    assert "张江路" not in out and "文三路" not in out and "上海市场不错" in out
    assert "138****5678" in out and "1101**********123X" in out
    # the model quotes the address back without its beginning: known pieces go too
    address = "上海市浦东新区张江路100号3栋1201"
    assert retention.scrub("再确认一下：张江路100号3栋1201，收件人小美", [address]) == "再确认一下：（详细地址已清除），收件人小美"
    assert retention.scrub(f"地址：{address}。放张江路100号门卫；第100号订单", []) == (
        "地址：上海市（详细地址已清除）。放（详细地址已清除）门卫；第100号订单")         # short pieces stay
    assert retention.cleared_slots({"收件地址": "广东省深圳市南山区科苑路15号", "收件人": "小美", "件数": 2,
                                    "收件信息": "小美 广东省深圳市南山区科苑路15号 13812345678"}) == {
        "收件地址": "广东省深圳市（详细地址已清除）", "收件人": "小美", "件数": 2,
        "收件信息": "小美 广东省深圳市（详细地址已清除） 138****5678"}


# ── settings ──────────────────────────────────────────────────────────────

def test_the_new_settings_have_ranges():
    with pytest.raises(RuntimeError) as e:
        _validate_config({"prm_takeover_min": 1, "prm_holding_min": 500, "prm_holding_text": "", "prm_retention_days": 0})
    message = str(e.value)
    for key in ("PRM_TAKEOVER_MIN", "PRM_HOLDING_MIN", "PRM_HOLDING_TEXT", "PRM_RETENTION_DAYS"):
        assert key in message
    config = BotConfig()
    assert (config.prm_takeover_min, config.prm_holding_min, config.prm_retention_days) == (120, 10, 30)


# ── the backend tells PRM about this account's own lines ──────────────────

class FakeClient:
    _config = {"myWxid": "wxid_bot"}

    def __init__(self, messages):
        self.messages = messages

    def get_messages(self, talker, limit=50):
        return self.messages

    def resolve_nickname(self, wxid):
        return {"wxid_bot": "机器人号", CREATOR: "小美"}.get(wxid, wxid)

    def get_display_names(self, usernames):
        return {u: self.resolve_nickname(u) for u in usernames}


def test_a_long_reply_read_back_compressed_confirms_it_and_takes_nothing_over(bot, clock):
    """Windows, 2026-10-26: WeChat stored the 124-character draft zstd compressed; it was read
    back as a picture and the chat was taken over.  Read back through the backend now."""
    import zstandard
    long_reply = "好的～帮你核对一下收件信息：收件人测试，电话〈手机1〉，地址上海市浦东新区测试路1号2栋301。没问题回我一下哦～" * 2
    packet = drafted(bot, draft(reply_draft=long_reply))
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    sent_text = bot.texts[-1][1]
    assert "13812345678" in sent_text
    raw = [{"sender_username": "wxid_bot", "local_type": 1, "create_time": int(clock.now), "local_id": "z1",
            "message_content": zstandard.ZstdCompressor().compress(sent_text.encode("utf-8")).hex()}]
    backend = WcdbBackend(bot_display_name="机器人", groups=["*"],
                          config=BotConfig(private_chat_enabled=True))
    backend._client = FakeClient(raw)
    backend._chat_is_group[CREATOR] = False
    backend._running = True
    backend.set_private_filter(bot.prm.is_party_chat)
    backend.set_own_message_hook(bot.prm.on_own_message)
    backend._dispatch_message = lambda msg, callback: None
    backend._poll_group("小美", CREATOR, Mock())
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    assert not [e for e in bot.prm.store.events(1) if e["kind"] in ("takeover", "operator_message")]


def test_own_lines_in_a_creators_chat_reach_the_hook_and_nowhere_else():
    now = int(time.time())
    raw = [{"sender_username": who, "message_content": text, "local_type": 1, "create_time": now,
            "local_id": f"m{i}"} for i, (who, text) in enumerate([(CREATOR, "你好"), ("wxid_bot", "我亲手回的")])]
    for chat, registered, expect in ((CREATOR, True, ["我亲手回的"]), ("wxid_friend", False, [])):
        backend = WcdbBackend(bot_display_name="机器人", groups=["*"],
                              config=BotConfig(private_chat_enabled=True))
        backend._client = FakeClient(raw)
        backend._chat_is_group[chat] = False
        backend._running = True
        backend.set_private_filter(lambda username, registered=registered: registered)
        heard = []
        backend.set_own_message_hook(lambda msg: heard.append(msg["content"]))
        dispatched = []
        backend._dispatch_message = lambda msg, callback: dispatched.append(msg["sender_id"])
        backend._poll_group("小美", chat, Mock())
        for msg in backend._burst.flush_all():
            dispatched.append(msg["sender_id"])
        assert heard == expect and dispatched == [CREATOR]                    # never to the router
    backend.set_own_message_hook(lambda msg: 1 / 0)                           # a broken hook: polling goes on
    backend._tell_own_message({"chat_id": "wxid_friend", "is_group": False})

def test_send_race_condition_notes_sent_before_wechat_reads_back(bot, clock):
    """If WeChat's WCDB polling detects the sent message or attachment during sending
    (before the send action returns), it must be recognised as the bot's own send,
    confirm the packet, and NOT trigger manual takeover."""
    packet = drafted(bot, draft(reply_draft="这是预先登记的回复"))
    bot.prm.decide(packet["id"], "send")

    orig_send = bot._send
    def simulated_send(chat_id, text):
        bot.prm.on_own_message({"chat_id": chat_id, "content": text, "msg_type": 1, "timestamp": clock.now})
        return orig_send(chat_id, text)

    bot.prm._send = simulated_send
    bot.prm.send_approved()
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    assert not [e for e in bot.prm.store.events(1) if e["kind"] in ("takeover", "operator_message")]


def test_a_failed_packet_stays_failed_when_its_text_is_read_back_late(clock, tmp_path):
    b = Bot(clock, tmp_path, files_ok=False)
    b.prm.register(CREATOR, "小美")
    p = pending_packet(b, draft(attachments_to_send=["assets/a.txt"]), files=[("a.txt", b"a")])
    b.prm.decide(p["id"], "send")
    b.prm.send_approved()                                                     # the text went, the file not
    clock.now += 2
    b.prm.on_own_message(own(b, "确认一下：小美，手机13812345678。"))          # the text shows up after that
    packet = b.prm.store.packet(p["id"])
    assert packet["status"] == "failed" and packet["outcome"] == "failed"
    assert b.prm.store.deal(1)["mode"] == "copilot"
    b.relay.stop()


def test_a_later_file_failing_keeps_the_earlier_file_the_bots(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm.register(CREATOR, "小美")
    p = pending_packet(b, draft(attachments_to_send=["assets/a.txt", "assets/b.txt"]),
                       files=[("a.txt", b"a"), ("b.txt", b"b")])
    goes = iter([True, False])
    b.prm._send_file = lambda chat, path: next(goes)                          # a.txt goes, b.txt does not
    b.prm.decide(p["id"], "send")
    b.prm.send_approved()
    assert b.prm.store.packet(p["id"])["error"] == "文件「b.txt」没发出去"
    clock.now += 2
    b.prm.on_own_message(own(b, "[文件]", msg_type=49))                       # a.txt shows up in WeChat
    assert b.prm.store.deal(1)["mode"] == "copilot"
    assert not [e for e in b.prm.store.events(1) if e["kind"] in ("takeover", "operator_message")]
    b.relay.stop()


def test_a_failed_send_takes_back_only_its_own_line(bot, clock):
    first = drafted(bot, draft(reply_draft="好的，收到～"))
    bot.prm.decide(first["id"], "send")
    bot.prm.send_approved()                                                   # WeChat has not shown it yet
    second = drafted(bot, draft(reply_draft="好的，收到～"))
    bot.prm.decide(second["id"], "send")
    bot.send_ok = False
    bot.prm.send_approved()                                                   # the same words, not sent
    bot.prm.on_own_message(own(bot, "好的，收到～"))                          # the first one shows up
    assert bot.prm.store.packet(first["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.packet(second["id"])["outcome"] == "failed"


def test_a_creator_written_straight_into_the_database_is_read_at_once(bot):
    """The console writes to the database itself when it has no handle on the running
    service: the chat filter looks there before it says no."""
    assert not bot.prm.is_party_chat("wxid_creator02")
    bot.prm.store.add_party("wxid_creator02", "小丽", "", bot.clock.now)
    assert bot.prm.is_party_chat("wxid_creator02")
