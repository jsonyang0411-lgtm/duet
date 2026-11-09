"""微信里审批草稿 (PRM_WECHAT_REVIEW, 2026-11-09, src/prm/wechat_review.py): the drafts waiting in 待审批 are told, once
each, to the admin's private chat, and the admin decides them there: quoting a notification (1 / 2 / 改 …), or
「发 N」「不发 N」「改 N …」, a bare 1 / 2 to the last thing the bot said there.  The same decision as the console's;
nothing of it with the switch off.  Each finding of the 2026-11-09 review (1–10) has its tests here.

The relay, the PRM service and the router as Bot.run wires them (``Desk``), with a fake WeChat that records every
message the bot sends and tells the review each one, as the WCDB backend's sent hook does (src/bot.py).  Names,
numbers, ids and addresses are made up.
"""

import itertools
import json
import logging
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from router_support import BOT, TraceSpy
from test_prm import ADMIN, CREATOR, Clock, config

from src.admin import RELAY_HELP, AdminCommandHandler
from src.db import MessageStore, initialize_db
from src.prm import deals, gates
from src.prm.service import SETTLE_SEC, UNSURE_ERROR, PrmService
from src.prm.store import BUYIN_TOOL, PrmStore
from src.prm.wechat_review import (BACKLOG_SEC, CLOSED_RACE_SEC, NO_ADMIN_WHY, ON_AT, REFUSE_TEXT, RELAY_QUIET_SEC,
                                   RETRY_SEC, SEND_TRIES, UNSURE_GRACE_SEC, VOICE_TEXT, read)
from src.relay.service import MAX_AGE_SEC, ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter

OTHER = "wxid_creator02"            # a second creator (小雨)
THIRD = "wxid_creator03"            # a third (小云)
GROUP = "200000009@chatroom"        # a 老板拉的群 (its name is made up)
IDS = itertools.count(1)
QUOTE = 49                          # a WeChat 引用 (local_type 57 << 32 | 49: the backend gives 49 and the quote)


def reply_draft(text="收到啦～戴着还合适吗？", **extra):
    values = {"intent": "收到样品", "slots": {}, "stage_proposal": "", "message_kind": "greeting", "reply_draft": text,
              "attachments_to_send": [], "needs_attention": False, "reason": "kb/合作政策.md"}
    values.update(extra)
    return values


class Desk:
    """The relay, the PRM service and the router as Bot.run wires them; ``sent``: every message the bot sent, in
    order (the PRM's and the relay's sends; the route's replies are what ``admin`` gives back), ``files`` every file.
    Each text that goes out is told to the review, as the WCDB backend's sent hook tells it (src/bot.py).  A send to a
    chat in ``refuse`` fails."""

    def __init__(self, clock, tmp_path, *, review="on", relay_only="off", admin=ADMIN, store=None, relay_store=None,
                 **prm):
        self.clock = clock
        self.sent: list = []
        self.files: list = []
        self.refuse: set = set()
        cfg = config(claude_relay_wxids=[], claude_relay_only=relay_only, admin_wxid=admin, prm_wechat_review=review,
                     **prm)
        self.router = MessageRouter(store=MessageStore(initialize_db(":memory:")), admin_handler=AdminCommandHandler(),
                                    config=SimpleNamespace(bot_display_name=BOT, admin_wxid=admin), tracer=TraceSpy())
        self.relay = ClaudeRelay(cfg, store=relay_store or RelayStore(":memory:"), clock=clock,
                                 files_dir=tmp_path / "relay", send=self._send, reserved=self.router._admin.is_command,
                                 extra_help=RELAY_HELP)
        self.router._claude_relay = self.relay
        self.prm = PrmService(cfg, self.relay, store=store or PrmStore(":memory:"), clock=clock, send=self._send,
                              send_file=self._send_file)
        self.router._prm = self.prm
        self.review = self.prm.wechat_review

    def _send(self, chat, text):
        self.sent.append((chat, text))
        if chat in self.refuse:
            return False
        self.review.note_sent(chat, text)              # (the backend's sent hook)
        return True

    def _send_file(self, chat, path):
        self.files.append((chat, path))
        return chat not in self.refuse

    def stop(self):
        self.prm.stop()
        self.relay.stop()

    # what happens in WeChat
    def her(self, text, chat=CREATOR):
        self.prm.handle({"message_id": f"c{next(IDS)}", "chat_id": chat, "sender_id": chat, "sender_name": "她",
                         "content": text, "msg_type": 1, "is_group": False, "timestamp": self.clock.now})

    def mac(self, **draft) -> dict:
        """The Mac worker takes the next prm job and hands back a draft: its packet."""
        self.clock.now += SETTLE_SEC + 1
        job = self.relay.next_job(0, ("prm",))
        assert job is not None
        self.relay.finish(job["id"], {"ok": True, "text": json.dumps(reply_draft(**draft), ensure_ascii=False),
                                      "duration_s": 3})
        self.relay.tick()
        return self.prm.store.packet_for_job(job["id"])

    def admin(self, content, *, quote=None, msg_type=None, at=None, sender=ADMIN, chat=ADMIN, group=False, **extra):
        """The admin writes in the admin's chat (``quote``: the text of the message quoted; ``_burst_count``: several
        bubbles merged); the route's reply (sent into that chat: the sent hook hears it too)."""
        msg = {"message_id": f"a{next(IDS)}", "chat_id": chat, "group_name": "管理员", "sender_id": sender,
               "sender_name": "管理员", "content": content, "msg_type": msg_type or (QUOTE if quote else 1),
               "timestamp": self.clock.now if at is None else at, "is_group": group, "quoted_content": quote or "",
               **extra}
        reply = self.router.handle(msg)
        if reply:
            self.review.note_sent(chat, reply)
        return reply

    def told(self) -> list[str]:
        """What the bot sent the admin besides the route's replies (the notifications, a reply told first, what came of
        a decision, the relay's messages)."""
        return [text for chat, text in self.sent if chat == ADMIN]

    def to(self, chat=CREATOR) -> list[str]:
        return [text for c, text in self.sent if c == chat]

    def jobs(self, kind="admin") -> list[dict]:
        return [j for j in self.relay.store.list_jobs(limit=100) if j.get("kind", "admin") == kind]


@pytest.fixture
def clock():
    return Clock(1_800_000_000.0)


@pytest.fixture
def desk(clock, tmp_path):
    d = Desk(clock, tmp_path)
    d.prm.register(CREATOR, "小美", "测试达人")
    yield d
    d.stop()


def waiting(desk, said="收到6件哈", chat=CREATOR, **draft) -> dict:
    """She writes ``said``, the Mac drafts; the draft waits in 待审批 and is told to the admin (the sender thread's
    round)."""
    desk.her(said, chat=chat)
    packet = desk.mac(**draft)
    assert packet["status"] == "pending"
    desk.review.sweep()
    return packet


def head(desk, number) -> str:
    """The first line of the notification of draft ``number``: what a quote of it starts with."""
    return next(t for t in desk.told() if t.startswith(f"#{number} 草稿")).split("\n")[0]


def notice_of(desk, number) -> list[str]:
    """The lines of the notification of draft ``number``."""
    return next(t for t in desk.told() if t.startswith(f"#{number} 草稿")).split("\n")


def group_packet(desk, text="在的～群里也可以直接问我") -> int:
    return desk.prm.store.add_packet(
        now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=GROUP, kind="other", text=text,
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
        meta={"channel": "group", "group_name": "小美合作群"})


# ── the notification ─────────────────────────────────────────────────────

def test_a_waiting_draft_is_told_to_the_admin_once_with_its_number_first(desk):
    packet = waiting(desk)
    [text] = desk.told()
    lines = text.split("\n")
    assert lines[0] == f"#{packet['id']} 草稿 · 小美 · 私聊"
    assert "她：收到6件哈" in lines and "草稿：收到啦～戴着还合适吗？" in lines
    assert "回 1 发出｜2 不发｜改 + 你的话（直接发你改的）" in lines
    assert lines[-1] == f"（引用这条回；不引用就回「发 {packet['id']}」「不发 {packet['id']}」「改 {packet['id']} 你的话」）"
    assert desk.to(CREATOR) == []                                       # she hears nothing of it
    assert desk.prm.store.wechat_review(packet["id"])["state"] == "notified"
    desk.review.sweep()
    desk.prm.tick()                                                     # (the sender thread's whole round)
    assert len(desk.told()) == 1 and desk.prm.store.packet(packet["id"])["status"] == "pending"


def test_the_sender_thread_tells_every_waiting_draft_a_few_a_round_oldest_first(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    for chat in (OTHER, CREATOR):                                       # two more, told in a later round
        desk.her("在吗", chat=chat)
        desk.mac()
    desk.prm.store.add_packet(now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="a",
                              attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="")
    pending = [p["id"] for p in desk.prm.store.packets(("pending",))]
    assert len(pending) == 3                                            # (her second line made #1 out of date)
    with patch("src.prm.wechat_review.PER_ROUND", 2):
        assert desk.review.sweep() == 2
        assert desk.review.sweep() == 1
    assert [t.split(" ")[0] for t in desk.told()] == [f"#{first['id']}", *(f"#{n}" for n in sorted(pending))]


def test_after_a_restart_a_told_draft_is_not_told_again_and_one_not_told_yet_is(clock, tmp_path):
    store, relay_store = PrmStore(":memory:"), RelayStore(":memory:")
    one = Desk(clock, tmp_path, store=store, relay_store=relay_store)
    one.prm.register(CREATOR, "小美", "")
    one.prm.register(OTHER, "小雨", "")
    told = waiting(one)
    one.her("在吗", chat=OTHER)
    untold = one.mac()                                                  # the bot stops before its round
    one.stop()
    two = Desk(clock, tmp_path, store=store, relay_store=relay_store)
    two.prm.start()                                                     # (what a start settles; its thread too)
    two.prm.stop()
    two.review.sweep()
    assert [t.split("\n")[0] for t in two.told()] == [f"#{untold['id']} 草稿 · 小雨 · 私聊"]
    assert store.wechat_review(told["id"])["notified_at"] < store.wechat_review(untold["id"])["notified_at"] + 1
    two.review.sweep()
    assert len(two.told()) == 1
    two.stop()


def test_with_the_switch_off_nothing_is_told_and_every_answer_goes_on_as_before(clock, tmp_path):
    desk = Desk(clock, tmp_path, review="off")
    desk.prm.register(CREATOR, "小美", "")
    desk.her("收到6件哈")
    packet = desk.mac()
    desk.prm.tick()
    assert desk.told() == [] and desk.prm.store.wechat_review(packet["id"]) is None
    assert desk.review.status() == {"on": False} and desk.prm.snapshot()["wechat_review"] == {"on": False}
    assert desk.prm.snapshot()["pending"][0]["wechat_at"] is None
    for content, quote in ((f"发 {packet['id']}", None), ("1", f"#{packet['id']} 草稿 · 小美 · 私聊")):
        assert desk.admin(content, quote=quote) is None                 # (silence, as before)
        assert desk.router._tracer.notes["ended_by"] == "silence"
    desk.review.note_sent(ADMIN, f"#{packet['id']} 草稿 · 小美 · 私聊")    # (the hook is never set with the switch off)
    assert desk.admin("1") is None and desk.router._tracer.notes["ended_by"] == "silence"
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.prm.store.wechat_reviews_open() == [] and desk.prm.store.meta_value(ON_AT) is None
    desk.stop()


def test_without_an_admin_nothing_is_told_and_the_console_says_why(clock, tmp_path):
    desk = Desk(clock, tmp_path, admin="")
    desk.prm.register(CREATOR, "小美", "")
    desk.her("收到6件哈")
    packet = desk.mac()
    assert desk.review.sweep() == 0 and desk.sent == []
    assert desk.prm.snapshot()["wechat_review"] == {"on": True, "why": NO_ADMIN_WHY, "open": 0, "unsent": 0,
                                                    "unsure": 0, "older": 0}
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    desk.stop()


def test_on_the_macs_edge_side_it_is_off_and_says_so(clock, tmp_path):
    desk = Desk(clock, tmp_path, wechat_backend="edge")
    assert desk.review.on is False and "WECHAT_BACKEND=edge" in desk.review.status()["why"]
    desk.stop()


# ── the first switch-on (review finding 9) ───────────────────────────────

def test_the_first_switch_on_tells_the_last_12_hours_only_and_the_console_counts_the_older(clock, tmp_path):
    """Switched on with drafts waiting for days: the ones from the last BACKLOG_SEC are told, the older stay the
    console's (「待审批」 counts them); the time is kept (prm_meta), so a restart floods nothing either."""
    store, relay_store = PrmStore(":memory:"), RelayStore(":memory:")
    off = Desk(clock, tmp_path, review="off", store=store, relay_store=relay_store)
    off.prm.register(CREATOR, "小美", "")
    off.prm.register(OTHER, "小雨", "")
    off.her("收到6件哈")
    old = off.mac()                                                     # waits in the console a long time
    clock.now += BACKLOG_SEC + 3600
    off.her("多少佣金呀", chat=OTHER)
    recent = off.mac(text="25% 哦～")                                    # an hour before the switch-on
    clock.now += 3600
    off.stop()
    on = Desk(clock, tmp_path, store=store, relay_store=relay_store)
    on.review.sweep()
    assert [t.split("\n")[0] for t in on.told()] == [f"#{recent['id']} 草稿 · 小雨 · 私聊"]
    assert on.review.status()["older"] == 1 and store.wechat_review(old["id"]) is None
    assert float(store.meta_value(ON_AT)) == clock.now
    assert on.admin(f"发 {old['id']}") is None                          # (not told here: it goes on, as before)
    on.stop()
    clock.now += 600
    again = Desk(clock, tmp_path, store=store, relay_store=relay_store)   # a restart: the same switch-on
    again.review.sweep()
    assert again.told() == [] and again.review.status()["older"] == 1
    again.stop()


def test_a_run_with_the_switch_off_makes_the_next_switch_on_start_afresh(clock, tmp_path):
    store, relay_store = PrmStore(":memory:"), RelayStore(":memory:")
    on = Desk(clock, tmp_path, store=store, relay_store=relay_store)
    on.prm.register(CREATOR, "小美", "")
    on.review.sweep()
    assert store.meta_value(ON_AT)
    on.stop()
    off = Desk(clock, tmp_path, review="off", store=store, relay_store=relay_store)
    off.her("收到6件哈")
    meanwhile = off.mac()
    off.stop()
    assert store.meta_value(ON_AT) == ""                                 # (off: forgotten)
    clock.now += BACKLOG_SEC + 1
    back = Desk(clock, tmp_path, store=store, relay_store=relay_store)
    back.review.sweep()
    assert back.told() == [] and back.review.status()["older"] == 1     # (it waited while the switch was off)
    assert store.wechat_review(meanwhile["id"]) is None
    back.stop()


# ── what is never told ───────────────────────────────────────────────────

def test_a_draft_its_gate_sends_by_itself_is_never_told(desk):
    desk.prm.store.set_gate("greeting", gates.AUTO, desk.clock.now)
    desk.her("收到6件哈")
    packet = desk.mac()
    assert packet["status"] == "approved" and packet["route"] == "auto"
    desk.prm.tick()
    assert desk.to(CREATOR) == ["收到啦～戴着还合适吗？"] and desk.told() == []


def test_a_draft_the_night_window_sends_is_never_told(clock, tmp_path):
    desk = Desk(clock, tmp_path, prm_night_auto_hours="00:00-23:59")
    desk.prm.register(CREATOR, "小美", "")
    desk.her("收到6件哈")
    packet = desk.mac()
    assert (packet["status"], packet["route"]) == ("approved", "night")
    desk.prm.tick()
    assert desk.told() == []
    desk.stop()


def test_a_baiying_draft_and_a_holding_reply_are_never_told(clock, tmp_path):
    desk = Desk(clock, tmp_path, prm_holding_min=1)
    desk.prm.register(CREATOR, "小美", "")
    desk.prm.store.add_packet(now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id="buyin:conv1", kind="other", text="您好～",
                              attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
                              tool=BUYIN_TOOL, meta={"channel": "buyin"})
    packet = waiting(desk)                                              # (told: the one WeChat draft)
    desk.clock.now += 120
    assert desk.prm.send_holding_replies() == 1                         # 「稍等」 goes to her, not to the admin
    desk.prm.tick()
    assert desk.to(CREATOR) == ["收到，我确认一下马上回您"]
    assert len(desk.told()) == 1 and desk.told()[0].startswith(f"#{packet['id']} 草稿")
    desk.stop()


def test_an_opening_that_goes_by_itself_is_not_told_unless_it_waits_for_a_person(clock, tmp_path):
    desk = Desk(clock, tmp_path, prm_opening_auto_min=30)
    desk.prm.register(CREATOR, "小美", "")
    by_itself = desk.prm.store.add_packet(
        now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="greeting", text="您好～我是品牌测试的运营",
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
        meta={"opening": True, "auto_at": clock.now + 1800})
    assert desk.review.sweep() == 0 and desk.told() == []
    with desk.prm.store._lock:                                          # held for a person (her recall)
        desk.prm.store._conn.execute("UPDATE action_packets SET needs_attention = 1, reason = '她撤回了一条消息'")
    assert desk.review.sweep() == 1
    [text] = desk.told()
    assert text.startswith(f"#{by_itself} 草稿 · 小美 · 私聊") and "（开场白：她还没跟我们说过话，这是我们先打招呼）" in text
    assert "要注意：她撤回了一条消息" in text
    desk.stop()


# ── what a notification says ─────────────────────────────────────────────

def test_her_words_keep_their_placeholders_and_lose_the_address_the_draft_is_shown_in_full(desk):
    desk.her("我的地址：广东省佛山市南海区某某路100号3栋1201 电话13812345678")
    desk.her("就寄这里吧")
    packet = desk.mac(text="好的，地址记下啦～电话〈手机1〉对吗？\n\n明天就给您寄", slots={"地址": "广东省佛山市南海区某某路100号3栋1201"})
    desk.prm._reply_parts = 3
    desk.review.sweep()
    [text] = desk.told()
    assert "她：我的地址：广东省佛山市（详细地址已清除）／就寄这里吧" in text
    assert "13812345678" not in text and "某某路" not in text and "1201" not in text
    assert "草稿：好的，地址记下啦～电话〈手机1〉对吗？\n\n明天就给您寄" in text
    assert "（会分 2 条发）" in text
    assert "号码：〈手机1〉 = 138****5678（发出时换成真实号码；改的时候保留占位符）" in text
    assert packet["id"] == 1


def test_only_her_last_lines_are_told_each_cut_short(desk):
    for line in ("第一句", "第二句", "这一句很长" * 20):
        desk.her(line)
    desk.mac()
    desk.review.sweep()
    [said] = [line for line in desk.told()[0].split("\n") if line.startswith("她：")]
    assert said.startswith("她：…第二句／这一句很长") and said.endswith("…") and len(said) < 80


def test_a_group_draft_says_it_goes_into_the_boss_group_and_needs_a_quote(desk):
    number = group_packet(desk)
    desk.review.sweep()
    lines = notice_of(desk, number)
    assert lines[0] == f"#{number} 草稿 · 小美 · 群里「小美合作群」"
    assert lines[1] == "（发在老板拉的群里，不是她的私聊：老板和群里的人都看得到）"
    assert "回 1 发到群里｜2 不发｜改 + 你的话（直接发你改的）" in lines
    assert lines[-1] == f"（发在群里：要引用这条回；不发也可以回「不发 {number}」）"


def test_a_shipping_notice_shows_its_waybill(desk):
    _, number = deals.add_shipment(desk.prm.store, 1, "中通快递", "ZT1234567890", now=desk.clock.now, review=True)
    desk.review.sweep()
    text = desk.told()[0]
    assert text.startswith(f"#{number} 草稿 · 小美 · 私聊") and "她：" not in text
    assert "运单：中通快递 ZT1234567890（发出的内容里必须是这个单号）" in text
    assert "草稿：老师您好～样品已经寄出啦：中通快递 ZT1234567890。签收之后跟我说一声哦～" in text


def test_a_draft_with_files_names_them(desk):
    number = desk.prm.store.add_packet(
        now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="material", text="",
        attachments=[{"name": "货盘.jpg", "path": "/nowhere/货盘.jpg", "size": 10}], intent="", slots={},
        stage_proposal="", needs_attention=False, reason="")
    desk.review.sweep()
    text = desk.told()[0]
    assert f"#{number} 草稿" in text and "草稿：（只发附件，没有文字）" in text and "（附 1 个文件：货盘.jpg）" in text


# ── a draft the bot started itself is aimed before it is told (review finding 7) ──

@pytest.fixture
def boss_bot(tmp_path):
    from test_prm_boss_groups import boss_makes, make, no_opening

    b = make(Clock(time.mktime((2026, 10, 31, 14, 30, 0, 0, 0, -1))), tmp_path, prm_wechat_review="on")
    boss_makes(b)
    no_opening(b)
    yield b
    b.prm.stop()
    b.relay.stop()


def shipped_while_stopped(b) -> int:
    """A shipping notice made while the bot was stopped (the console straight on the database): aimed at her private
    chat, whatever she has."""
    from test_prm_boss_groups import GROUP as BOSS_CHAT
    from src.prm.service import apply_deal_action

    deal_id = b.prm.store.boss_group(BOSS_CHAT)["deal_id"]
    out = apply_deal_action(b.prm.store, "shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"},
                            now=b.clock.now, takeover_sec=7200)
    assert b.prm.store.packet(out["packet"])["chat_id"] == CREATOR
    return out["packet"]


def test_a_draft_the_bot_started_is_aimed_where_she_is_before_it_is_told(boss_bot):
    from test_prm_boss_groups import GROUP as BOSS_CHAT

    number = shipped_while_stopped(boss_bot)
    boss_bot.prm.wechat_review.sweep()
    [text] = [t for chat, t in boss_bot.texts if chat == ADMIN]
    name = boss_bot.prm.store.packet(number)["meta"]["group_name"]
    assert text.split("\n")[0] == f"#{number} 草稿 · 小美 · 群里「{name}」"               # (no private chat with her)
    assert "（发在老板拉的群里，不是她的私聊：老板和群里的人都看得到）" in text
    assert boss_bot.prm.store.packet(number)["chat_id"] == BOSS_CHAT


def test_a_draft_the_bot_started_for_a_creator_reached_nowhere_waits_untold(boss_bot):
    from test_prm_boss_groups import GROUP as BOSS_CHAT, bg

    boss_bot.backend.groups[BOSS_CHAT]["members"].remove(CREATOR)       # she left the group, and is no friend of ours
    boss_bot.clock.now += bg.BOSS_SCAN_SEC + 1
    boss_bot.prm.boss_groups.scan()
    number = shipped_while_stopped(boss_bot)
    assert boss_bot.prm.wechat_review.sweep() == 0
    assert [t for chat, t in boss_bot.texts if chat == ADMIN] == []
    assert boss_bot.prm.store.packet(number)["status"] == "pending"      # (not failed by being told: the console has it)
    boss_bot.prm.handle(boss_bot.msg("我加你好友了"))                     # she writes in private: reachable there
    boss_bot.prm.wechat_review.sweep()
    [text] = [t for chat, t in boss_bot.texts if chat == ADMIN]
    assert text.split("\n")[0] == f"#{number} 草稿 · 小美 · 私聊"


# ── sending the notification ─────────────────────────────────────────────

def test_a_notification_that_fails_is_tried_later_and_later_and_given_up_after_six(desk, caplog):
    desk.refuse.add(ADMIN)
    packet = waiting(desk)                                              # (try 1)
    t0, tries = desk.clock.now, [desk.clock.now]
    for second in range(1, 300):
        desk.clock.now = t0 + second
        before = len(desk.told())
        desk.review.sweep()
        if len(desk.told()) > before:
            tries.append(desk.clock.now)
    gaps = [round(b - a) for a, b in zip(tries, tries[1:])]
    assert len(tries) == SEND_TRIES and gaps == [10, 20, 40, 80, 120][:SEND_TRIES - 1]
    assert desk.prm.store.wechat_review(packet["id"]) is None            # (each failed send: its row gone again)
    assert desk.review.status()["unsent"] == 1
    assert "not sent to the admin's chat after 6 tries" in caplog.text
    assert desk.prm.decide(packet["id"], "send")["ok"]                    # the console works as ever
    assert RETRY_SEC == (10, 120)


def test_a_notification_that_goes_out_after_a_failure_is_not_tried_again(desk):
    desk.refuse.add(ADMIN)
    packet = waiting(desk)
    desk.refuse.clear()
    desk.clock.now += 10
    desk.review.sweep()
    assert len(desk.told()) == 2 and desk.prm.store.wechat_review(packet["id"])["state"] == "notified"
    desk.clock.now += 300
    desk.review.sweep()
    assert len(desk.told()) == 2 and desk.review.status()["unsent"] == 0


def test_a_draft_decided_before_its_notification_went_is_not_told(desk):
    desk.her("收到6件哈")
    packet = desk.mac()
    assert desk.prm.decide(packet["id"], "drop")["ok"]
    assert desk.review.sweep() == 0 and desk.told() == []


def test_a_chat_that_takes_nothing_is_tried_once_a_round_and_the_rest_wait(desk):
    """The admin's chat refuses (a window problem): one send a round, then the chat waits; once it takes them again the
    drafts not told yet go, PER_ROUND a round."""
    desk.prm.register(OTHER, "小雨", "")
    for chat in (CREATOR, OTHER):
        desk.her("在吗", chat=chat)
        desk.mac()
    desk.prm.store.add_packet(now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="a",
                              attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="")
    desk.prm.store.add_packet(now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="b",
                              attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="")
    desk.refuse.add(ADMIN)
    assert desk.review.sweep() == 0 and len(desk.told()) == 1           # one try, then the round ends
    assert desk.review.sweep() == 0 and len(desk.told()) == 1           # the chat waits (10 s)
    desk.refuse.clear()
    desk.clock.now += 10
    assert desk.review.sweep() == 3 and len(desk.told()) == 4           # PER_ROUND
    assert desk.review.sweep() == 1 and len(desk.prm.store.wechat_reviews_open()) == 4


# ── a notification's row is written before its send (review finding 10) ──

def test_a_notification_whose_row_cannot_be_marked_after_the_send_is_never_sent_again(desk, caplog):
    """The send went out, then the database failed: the row stays ``telling`` and the draft is never told again
    (the console says it may have gone out); the round's failure never stops the sender thread's other work."""
    desk.her("收到6件哈")
    packet = desk.mac()
    real = desk.prm.store.set_wechat_review

    def broken(packet_id, states=(), now=None, **fields):
        if fields.get("state") == "notified":
            raise sqlite3.OperationalError("database is locked")
        return real(packet_id, states, now, **fields)

    with patch.object(desk.prm.store, "set_wechat_review", broken), \
            patch.object(desk.prm, "send_holding_replies", wraps=desk.prm.send_holding_replies) as holding:
        desk.prm.tick()                                                 # (the sender thread's round)
    assert holding.called and "telling the admin's chat of the drafts failed" in caplog.text
    assert len(desk.told()) == 1 and desk.prm.store.wechat_review(packet["id"])["state"] == "telling"
    for _ in range(3):
        desk.clock.now += 300
        desk.prm.tick()
    assert len(desk.told()) == 1
    status = desk.review.status()
    assert (status["unsure"], status["open"]) == (1, 1)
    assert desk.admin("1", quote=head(desk, packet["id"])) == f"#{packet['id']} 已批准，正在发给 小美"   # (answered as told)


def test_a_row_left_telling_by_a_stop_is_never_sent_again_after_the_restart(clock, tmp_path):
    store, relay_store = PrmStore(":memory:"), RelayStore(":memory:")
    one = Desk(clock, tmp_path, store=store, relay_store=relay_store)
    one.prm.register(CREATOR, "小美", "")
    one.her("收到6件哈")
    packet = one.mac()
    one.review._cutoff()
    assert store.add_wechat_review(packet["id"], packet["deal_id"], clock.now, state="telling")   # stopped mid-send
    one.stop()
    two = Desk(clock, tmp_path, store=store, relay_store=relay_store)
    assert two.review.sweep() == 0 and two.told() == []
    assert two.review.status()["unsure"] == 1 and two.prm.snapshot()["wechat_review"]["unsure"] == 1
    assert two.prm.decide(packet["id"], "send")["ok"]
    assert two.review.status()["unsure"] == 0
    two.stop()


# ── the admin's answers: quoting a notification (review finding 1) ───────

@pytest.mark.parametrize("word", ["1", "发", "发送", "好", "好的", "可以", "行", "ok", "OK", "同意", "嗯", "[强]", "👍", "１",
                                  "好的！"])
def test_quoting_a_notification_with_a_yes_sends_the_draft(desk, word):
    packet = waiting(desk)
    assert desk.admin(word, quote=head(desk, packet["id"])) == f"#{packet['id']} 已批准，正在发给 小美"
    assert desk.prm.store.packet(packet["id"])["status"] == "approved"
    assert desk.prm.send_approved() == 1 and desk.to(CREATOR) == ["收到啦～戴着还合适吗？"]
    assert desk.jobs() == [] and desk.router._tracer.notes["ended_by"] == "wechat_review"
    assert desk.prm.store.wechat_review(packet["id"])["decision"] == "send"


@pytest.mark.parametrize("word", ["2", "不发", "不要", "算了", "２。"])
def test_quoting_a_notification_with_a_no_drops_the_draft(desk, word):
    packet = waiting(desk)
    assert desk.admin(word, quote=head(desk, packet["id"])) == f"#{packet['id']} 不发了（她再发消息时会有新的草稿）"
    assert desk.prm.store.packet(packet["id"])["status"] == "dropped"
    desk.prm.tick()
    assert desk.to(CREATOR) == []


def test_quoting_with_改_sends_the_admins_words_after_the_consoles_checks(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("改 " + "长" * 1501, quote=head(desk, number)) == f"#{number} 没发：最多 1500 个字"
    assert desk.admin("改：您的电话〈手机9〉对吗", quote=head(desk, number)) == (
        f"#{number} 没发：草稿里有对不上的占位符：〈手机9〉")
    assert desk.admin("改：5", quote=head(desk, number)) == (
        f"#{number}：改的内容只有一个数字，没有改。要改请回「改 {number} 你的话」")
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin("改 收到啦～\n明天给您开佣金哈", quote=head(desk, number)) == f"#{number} 按你改的批准了，正在发给 小美"
    packet = desk.prm.store.packet(number)
    assert (packet["status"], packet["final_text"], packet["edited"]) == ("approved", "收到啦～\n明天给您开佣金哈", True)
    assert desk.prm.store.wechat_review(number)["decision"] == "edit"


def test_改_alone_asks_for_the_words(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("改", quote=head(desk, number)) == f"#{number} 要改成什么？引用通知回「改 你的话」，或者回「改 {number} 你的话」"
    assert desk.admin(f"改 {number}", quote=head(desk, number)) == f"#{number} 要改成什么？回「改 {number} 你的话」"
    assert desk.admin(f"改 {number}") == f"#{number} 要改成什么？回「改 {number} 你的话」"
    assert desk.prm.store.packet(number)["status"] == "pending"


def test_a_shipping_notice_edited_in_wechat_keeps_the_systems_waybill(desk):
    _, number = deals.add_shipment(desk.prm.store, 1, "中通快递", "ZT1234567890", now=desk.clock.now, review=True)
    desk.review.sweep()
    reply = desk.admin("改 样品寄出啦：中通快递 ZT0000000000", quote=head(desk, number))
    assert reply == f"#{number} 没发：发货通知里的单号和系统记录对不上（系统记录：ZT1234567890）"
    assert desk.admin("改 老师，样品寄出啦：中通快递 ZT1234567890", quote=head(desk, number)).endswith("正在发给 小美")


def test_other_words_quoting_a_notification_are_its_proposed_edit_sent_by_a_1_quoting_that_question(desk):
    packet = waiting(desk)
    number = packet["id"]
    reply = desk.admin("戴着合适吗老师～", quote=head(desk, number))
    assert reply == f"#{number} 改成：『戴着合适吗老师～』\n回 1 发出／2 不发"
    assert desk.prm.store.packet(number)["status"] == "pending"            # not sent yet
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["proposed"]) == ("proposed", "戴着合适吗老师～")
    reply = desk.admin("还是这样吧：戴着合适吗～", quote=reply.split("\n")[0])   # quoting the question: a new proposal
    assert reply == f"#{number} 改成：『还是这样吧：戴着合适吗～』\n回 1 发出／2 不发"
    assert desk.admin("1", quote=reply.split("\n")[0]) == f"#{number} 按你改的批准了，正在发给 小美"
    packet = desk.prm.store.packet(number)
    assert (packet["status"], packet["final_text"]) == ("approved", "还是这样吧：戴着合适吗～")


def test_a_1_quoting_the_notification_sends_the_draft_as_drafted_never_an_earlier_remark(desk):
    """Review s1: the admin's remark quoting the notification (a question, not meant for her) was kept as the proposal,
    and a later 1 quoting the notification (the draft as shown) sent the remark.  It sends the draft now, and the
    proposal is gone."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("这个价格对吗？", quote=head(desk, number)).startswith(f"#{number} 改成：『这个价格对吗？』")
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"
    sent = desk.prm.store.packet(number)
    assert sent["final_text"] == sent["text"] == "收到啦～戴着还合适吗？" and not sent["edited"]
    assert desk.prm.store.wechat_review(number)["proposed"] == ""


def test_quoting_an_older_question_never_sends_a_newer_proposal(desk):
    """Review s2: two proposals, the admin quotes the question that showed the first and answers 1: the second was
    sent.  Now nothing is sent, the bot shows the current one as a question of its own (its bare 1 sends it)."""
    packet = waiting(desk)
    number = packet["id"]
    first = desk.admin("明天寄出哦", quote=head(desk, number)).split("\n")[0]
    desk.admin("今天就寄出哦", quote=head(desk, number))
    assert desk.admin("1", quote=first) == (
        f"#{number} 改成：『今天就寄出哦』\n（改稿换过了：你引用的不是现在这一份，没发）回 1 发出／2 不发")
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin("1") == f"#{number} 按你改的批准了，正在发给 小美"          # (the question just asked)
    assert desk.prm.store.packet(number)["final_text"] == "今天就寄出哦"


def test_a_question_quoted_cut_short_sends_nothing(desk):
    packet = waiting(desk)
    number = packet["id"]
    desk.admin("明天寄出哦\n记得查收", quote=head(desk, number))
    reply = desk.admin("1", quote=f"#{number} 改成：『明天寄出哦")          # (the quote shows its first line only)
    assert reply == f"#{number} 改成：『明天寄出哦\n记得查收』\n（引用里看不全那一份改稿，没发）回 1 发出／2 不发"
    assert desk.prm.store.packet(number)["status"] == "pending"


@pytest.mark.parametrize("words", ["cc 帮我查下她之前的订单", "CC帮我查下她之前的订单", "Cc：查下订单", "查状态", "帮助",
                                   "同意好友", "批准", "不行", "拒绝", "yes", "[微笑]", "😀", "3", "12"])
def test_a_quote_of_a_notification_never_makes_these_a_proposed_edit(desk, words):
    """Review s7 / s8: a cc task, an admin command, a relay approval word, emoji only or a lone number quoting a
    notification became the proposed edit (and a 1 sent it to her).  A cc task or a command goes on to its step,
    untouched; the others get a line saying how to answer."""
    packet = waiting(desk)
    number = packet["id"]
    reply = desk.admin(words, quote=head(desk, number))
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["proposed"]) == ("notified", "")
    assert desk.prm.store.packet(number)["status"] == "pending"
    if words.lower().startswith("cc"):
        assert "已交给 Mac" in reply and desk.router._tracer.notes["ended_by"] == "claude_relay"
        assert len(desk.jobs()) == 1
    elif words in ("查状态", "帮助", "同意好友"):
        assert desk.router._tracer.notes["ended_by"] == "admin_command" and desk.jobs() == []
    else:
        assert reply == f"#{number}：这句没当成回复，也没记成改稿。回 1 发出原稿、2 不发、「改 你的话」改了再发"
        assert desk.jobs() == []
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "收到啦～戴着还合适吗？"


def test_unquoted_发_n_sends_the_draft_as_drafted_whatever_was_proposed(desk):
    packet = waiting(desk)
    number = packet["id"]
    desk.admin("明天给您寄～", quote=head(desk, number))
    assert desk.admin(f"发 {number}") == f"#{number} 已批准，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "收到啦～戴着还合适吗？"


def test_a_proposed_edit_is_checked_when_it_is_made(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("电话〈手机2〉对吗", quote=head(desk, number)) == f"#{number} 不能这样改：草稿里有对不上的占位符：〈手机2〉"
    assert desk.prm.store.wechat_review(number)["state"] == "notified"


def test_a_2_to_a_proposed_edits_question_drops_the_draft(desk):
    packet = waiting(desk)
    desk.admin("明天给您寄～", quote=head(desk, packet["id"]))
    assert desk.admin("2") == f"#{packet['id']} 不发了（她再发消息时会有新的草稿）"
    assert desk.prm.store.packet(packet["id"])["status"] == "dropped"


# ── numbered answers (review findings 2 and 3) ──────────────────────────

def test_without_a_quote_the_number_says_which_draft(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")
    third = waiting(desk, "那我等你", chat=CREATOR, text="好的～")       # (#1 out of date: her new line)
    assert desk.admin(f"发 {second['id']}") == f"#{second['id']} 已批准，正在发给 小雨"
    assert desk.admin(f"改 {third['id']} 好的老师～明天见") == f"#{third['id']} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(third["id"])["final_text"] == "好的老师～明天见"
    assert desk.admin(f"不发 {first['id']}") == f"#{first['id']} 已过时：她又发了新消息，新的草稿 #{third['id']} 已经处理过了"
    assert desk.jobs() == []


def test_unquoted_numbers_count_only_for_drafts_told_here(desk):
    """「发 99」 with no notification #99: not this feature's (it goes on, as before); 「改 N …」 only for a draft that
    still waits."""
    packet = waiting(desk)
    for words in ("发 99", "不发 99", "改 99 好的", "改 3 处错别字"):
        assert desk.admin(words) is None and desk.router._tracer.notes["ended_by"] == "silence", words
    assert desk.prm.decide(packet["id"], "drop")["ok"]
    assert desk.admin(f"改 {packet['id']} 好的老师") is None              # (no longer waiting: goes on)
    assert desk.admin(f"发 {packet['id']}") == f"#{packet['id']} 已经处理过了：点了不发"


def test_a_numbered_edit_quoting_another_draft_changes_nothing_and_asks_which(desk):
    """Review s5: quoting 小美's notification, 「改 <小雨's number> 件都收到了吗」 edited and sent 小雨's draft.  A number
    that is not the quoted draft's changes nothing now."""
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")
    reply = desk.admin(f"改 {second['id']} 件都收到了吗", quote=head(desk, first["id"]))
    assert reply == (f"你引用的是 #{first['id']}，写的却是「改 {second['id']}」：没有改。要改哪一份？引用那一份的通知回"
                     "「改 你的话」，或者回「改 编号 你的话」")
    for packet in (first, second):
        assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.admin("改 3 件都收到了吗", quote=head(desk, first["id"])).startswith(f"你引用的是 #{first['id']}")
    assert desk.admin("改成 3 件都收到了吗", quote=head(desk, first["id"])) == f"#{first['id']} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(first["id"])["final_text"] == "3 件都收到了吗"


def test_改_with_a_number_alone_never_sends_the_number(desk):
    """Review s9: quoting a notification, 「改 <a number typed wrong>」 sent that number to her as the reply."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin(f"改 {number + 7}", quote=head(desk, number)).startswith(f"你引用的是 #{number}")
    assert desk.admin(f"改 {number} 5", quote=head(desk, number)) == (
        f"#{number}：改的内容只有一个数字，没有改。要改请回「改 {number} 你的话」")
    assert desk.admin(f"改 {number} 5") == f"#{number}：改的内容只有一个数字，没有改。要改请回「改 {number} 你的话」"
    packet = desk.prm.store.packet(number)
    assert packet["status"] == "pending" and packet["final_text"] is None


def test_a_bare_1_or_2_answers_only_the_last_thing_the_bot_said_here(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")     # told last
    assert desk.admin("1") == f"#{second['id']} 已批准，正在发给 小雨"
    assert desk.prm.store.packet(first["id"])["status"] == "pending"
    for word in ("1", "2"):                                             # the last thing said: the bot's reply
        assert desk.admin(word) is None and desk.router._tracer.notes["ended_by"] == "silence"
    assert desk.prm.store.packet(first["id"])["status"] == "pending"
    desk.prm.register(THIRD, "小云", "")
    desk.clock.now += CLOSED_RACE_SEC
    third = waiting(desk, "在吗", chat=THIRD, text="在的～")
    assert desk.admin("2") == f"#{third['id']} 不发了（她再发消息时会有新的草稿）"
    assert desk.prm.store.packet(first["id"])["status"] == "pending"


def test_a_bare_answer_after_any_other_message_of_the_bot_is_nobodys(desk):
    packet = waiting(desk)
    assert "已交给 Mac" in desk.admin("cc 帮我查一下今天的订单")            # (the relay's receipt: the last thing said)
    desk.clock.now += RELAY_QUIET_SEC
    for word in ("1", "2", "１"):
        assert desk.admin(word) is None
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"


def test_a_bare_answer_near_a_cc_task_is_not_taken(desk):
    """No bare 1 / 2 while a cc task of the admin's chat runs, or within RELAY_QUIET_SEC after it ended (it may be
    Claude's 「1 还是 2？」); only a notification told after that counts."""
    desk.prm.register(OTHER, "小雨", "")
    desk.prm.register(THIRD, "小云", "")
    desk.admin("cc 帮我比较一下两种方案")
    job = desk.relay.next_job(0, ("admin",))
    first = waiting(desk)                                               # told while the task runs
    n = first["id"]
    assert desk.admin("1") == (f"Claude 那边有任务在做或者刚做完，单独一个 1 / 2 不算：#{n} 请引用那条通知回，或者回「发 {n}」"
                               f"「不发 {n}」")
    desk.relay.finish(job["id"], {"ok": True, "text": "方案 1 还是 2？", "duration_s": 3})
    desk.relay.tick()                                                   # its result: the last thing said here
    assert desk.admin("2") is None
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")     # told right after the task ended
    assert desk.admin("1").startswith(f"Claude 那边有任务在做或者刚做完，单独一个 1 / 2 不算：#{second['id']}")
    desk.clock.now += RELAY_QUIET_SEC
    third = waiting(desk, "在吗", chat=THIRD, text="在的～")
    assert desk.admin("1") == f"#{third['id']} 已批准，正在发给 小云"
    assert [desk.prm.store.packet(p["id"])["status"] for p in (first, second)] == ["pending", "pending"]


@pytest.mark.parametrize("review", ["on", "off"])
def test_relay_only_a_bare_answer_is_claudes_never_a_drafts(clock, tmp_path, review):
    """Review s3 / s4: in CLAUDE_RELAY_ONLY a bare 1 meant for Claude's 「选 1 还是 2？」 approved the one waiting draft,
    a bare 2 within the hour was swallowed.  Now both go to Claude as before (the switch off: the same)."""
    desk = Desk(clock, tmp_path, relay_only="on", review=review)
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("帮我比较一下两种方案")                                  # relay only: a task for Claude
    job = desk.relay.next_job(0, ("admin",))
    packet = waiting(desk)
    clock.now += 60
    desk.admin("1")
    desk.admin("2")
    assert [f["text"] for f in desk.relay.store.followups(job["id"], 0)] == ["1", "2"]
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    desk.stop()


def test_a_bare_answer_never_takes_a_draft_told_after_it_was_written(desk):
    """Review s6: the admin types 1 for one draft, the console drops it meanwhile, another is told 4 s after the 1 was
    written: the 1 approved that one, unseen."""
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    desk.her("多少佣金呀", chat=OTHER)
    second = desk.mac(text="25% 哦～")                                  # 小雨's draft, not told yet
    written = desk.clock.now                                            # the admin types a bare 1 for #first now
    assert desk.prm.decide(first["id"], "drop")["ok"]                   # meanwhile the console dropped #first
    desk.clock.now += 4
    desk.review.sweep()                                                 # #second told 4 s after the 1 was written
    desk.clock.now += 2
    n = second["id"]
    assert desk.admin("1", at=written) == f"#{n} 是刚发来的，你回的时候还没看到它：请引用它回，或者回「发 {n}」「不发 {n}」"
    assert desk.prm.store.packet(n)["status"] == "pending"


def test_a_bare_answer_right_after_another_told_draft_closed_asks_for_the_number(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")     # the last thing said
    assert desk.prm.decide(first["id"], "drop")["ok"]                   # the console, a moment ago
    assert desk.admin("1") == f"刚有一份处理掉了，请带编号回（发 {second['id']}）"
    assert desk.prm.store.packet(second["id"])["status"] == "pending"
    store, now = desk.prm.store, desk.clock.now
    assert store.wechat_reviews_closed_since(now - CLOSED_RACE_SEC, second["id"]) == [first["id"]]
    assert store.wechat_reviews_closed_since(now + 1, second["id"]) == []
    assert desk.admin(f"发 {second['id']}") == f"#{second['id']} 已批准，正在发给 小雨"


# ── several bubbles at once (review finding 4) ───────────────────────────

def test_a_merged_burst_edit_is_only_proposed_and_its_cc_line_goes_on_to_the_relay(desk):
    """Review s10: 「改 N …」 and a cc task sent as two bubbles (merged within 2.2 s) went to her as one text.  The edit
    is only proposed now, the cc line is a task of its own."""
    packet = waiting(desk)
    number = packet["id"]
    reply = desk.admin(f"改 {number} 好的老师～明天发\ncc 查一下今天的订单", _burst_count=2)
    assert "已交给 Mac" in reply and len(desk.jobs()) == 1
    assert "查一下今天的订单" in desk.jobs()[0]["text"] and "好的老师" not in desk.jobs()[0]["text"]
    assert desk.told()[-1] == f"#{number} 改成：『好的老师～明天发』\n回 1 发出／2 不发"
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.prm.store.wechat_review(number)["proposed"] == "好的老师～明天发"
    assert desk.admin("1", quote=f"#{number} 改成：『好的老师～明天发』") == f"#{number} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "好的老师～明天发"


def test_one_bubble_with_an_edit_and_a_cc_line_sends_the_edit_and_the_task_goes_on(desk):
    packet = waiting(desk)
    number = packet["id"]
    reply = desk.admin(f"改 {number} 好的老师～明天发\ncc 查一下今天的订单")
    assert "已交给 Mac" in reply and desk.told()[-1] == f"#{number} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "好的老师～明天发"


def test_a_burst_with_two_cc_or_command_lines_is_refused_whole(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin(f"发 {number}\ncc 查一下订单\n查状态", _burst_count=3) == REFUSE_TEXT
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.jobs() == []


def test_a_发_n_followed_by_other_lines_keeps_them(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")
    reply = desk.admin(f"发 {first['id']}\n不发 {second['id']}\ncc 查一下今天的订单", _burst_count=3)
    assert "已交给 Mac" in reply and "查一下今天的订单" in desk.jobs()[0]["text"]
    assert desk.told()[-2:] == [f"#{first['id']} 已批准，正在发给 小美", f"#{second['id']} 不发了（她再发消息时会有新的草稿）"]
    assert desk.admin(f"发 {first['id']}\n谢谢", _burst_count=2) is None   # (its other words go on: silence)
    assert desk.told()[-1] == f"#{first['id']} 已经处理过了：你在微信里回过了，已经批准，正在发"


def test_a_burst_edit_over_several_bubbles_is_proposed_whole(desk):
    packet = waiting(desk)
    number = packet["id"]
    reply = desk.admin(f"改 {number} 好的老师\n明天一早给您寄", _burst_count=2)
    assert reply == f"#{number} 改成：『好的老师\n明天一早给您寄』\n回 1 发出／2 不发"
    assert desk.admin("1") == f"#{number} 按你改的批准了，正在发给 小美"


# ── one decision, and what came of it ────────────────────────────────────

def test_one_decision_per_draft_the_console_first(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.prm.decide(number, "send")["ok"]                        # the console
    assert desk.admin("2", quote=head(desk, number)) == f"#{number} 已经处理过了：已经批准，正在发"
    desk.prm.send_approved()
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已经处理过了：已经发出去了"
    assert desk.to(CREATOR) == ["收到啦～戴着还合适吗？"]


def test_one_decision_per_draft_wechat_first(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("2", quote=head(desk, number)).endswith("不发了（她再发消息时会有新的草稿）")
    assert desk.prm.decide(number, "send") == {"ok": False, "error": "这条草稿已经处理过了（或者达人又发了新消息，草稿已过时）"}
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已经处理过了：你在微信里回过了，点了不发"


def test_an_out_of_date_notification_says_which_draft_replaced_it(desk):
    first = waiting(desk)
    desk.her("对了，颜色发错了")                                         # she writes again: #1 out of date
    assert desk.admin("1", quote=head(desk, first["id"])) == (
        f"#{first['id']} 已过时：她又发了新消息，新的草稿还在写，写好了会发到这里")
    second = desk.mac(text="不好意思～马上给您换")
    assert desk.admin("1", quote=head(desk, first["id"])) == f"#{first['id']} 已过时：她又发了新消息，新的草稿是 #{second['id']}"
    assert desk.prm.store.packet(second["id"])["status"] == "pending"     # (only #2's own answer sends it)


def test_a_draft_out_of_date_because_a_person_took_the_chat_says_so(desk):
    packet = waiting(desk)
    desk.prm.on_own_message({"message_id": "own1", "chat_id": CREATOR, "sender_id": "wxid_me", "content": "我来回她",
                             "msg_type": 1, "is_group": False, "timestamp": desk.clock.now})
    assert desk.prm.store.packet(packet["id"])["status"] == "superseded"
    assert desk.admin("1", quote=head(desk, packet["id"])).startswith(f"#{packet['id']} 已过时：")


def test_a_draft_approved_in_wechat_that_fails_to_send_is_told_in_one_line(desk):
    packet = waiting(desk)
    desk.refuse.add(CREATOR)
    desk.admin("1", quote=head(desk, packet["id"]))
    desk.prm.tick()                                                     # sent: refused, then told
    assert desk.prm.store.packet(packet["id"])["status"] == "failed"
    assert desk.told()[-1] == f"#{packet['id']} 没发出去：文字没发出去；需要的话请在微信里手动发给 小美"
    desk.prm.tick()
    assert len([t for t in desk.told() if "没发出去" in t]) == 1


def test_a_draft_approved_in_the_console_that_fails_is_not_told_in_wechat(desk):
    packet = waiting(desk)
    desk.refuse.add(CREATOR)
    desk.prm.decide(packet["id"], "send")
    desk.prm.tick()
    assert desk.prm.store.packet(packet["id"])["status"] == "failed" and len(desk.told()) == 1


def cut_off_by_a_stop(desk) -> int:
    """A draft approved here whose send the bot was stopped in the middle of (what ``start`` makes of it)."""
    packet = waiting(desk)
    number = packet["id"]
    desk.admin("1", quote=head(desk, number))
    store = desk.prm.store
    assert store.move(number, ("approved",), "sending")
    assert store.move(number, ("sending",), "failed", outcome="indeterminate", sent_at=desk.clock.now,
                      error=UNSURE_ERROR)
    return number


def test_an_unsure_send_is_never_told_as_not_sent_and_waits_for_its_read_back(desk):
    """Review s11: a send cut off by a stop (``indeterminate``) was told at once as 「没发出去」, and the read-back then
    found it had gone out.  It waits now: three minutes after the start it is told as unsure, once."""
    number = cut_off_by_a_stop(desk)
    before = len(desk.told())
    desk.review.sweep()
    assert len(desk.told()) == before                                   # (the read-back may still find it)
    desk.clock.now += UNSURE_GRACE_SEC
    desk.review.sweep()
    assert desk.told()[-1] == f"#{number} 不确定发出去没有（发的时候机器人停了），请看一眼微信再决定要不要手动发给 小美"
    desk.review.sweep()
    assert len(desk.told()) == before + 1


def test_an_unsure_send_that_the_read_back_settles_is_never_told(desk):
    number = cut_off_by_a_stop(desk)
    assert desk.prm.store.settle_unsure(number, sent_at=desk.clock.now)   # WeChat shows it went out after all
    desk.clock.now += UNSURE_GRACE_SEC
    before = len(desk.told())
    desk.review.sweep()
    assert len(desk.told()) == before and desk.prm.store.packet(number)["status"] == "sent"


# ── a 老板拉的群's draft (review finding 8) ──────────────────────────────

def test_a_group_draft_is_sent_from_wechat_only_by_an_answer_quoting_it(desk):
    number = group_packet(desk)
    desk.review.sweep()
    hint = f"#{number} 是发在群「小美合作群」里的，请引用那条通知回 1 确认"
    assert desk.admin("1") == hint                                     # (the bare 1 to its notification)
    assert desk.admin(f"发 {number}") == hint
    assert desk.admin(f"改 {number} 在的～") == hint
    assert desk.prm.store.packet(number)["status"] == "pending"
    question = desk.admin("在的～有什么问题群里直接说", quote=head(desk, number))
    assert question == f"#{number} 改成：『在的～有什么问题群里直接说』\n引用这条回 1 发到群里／2 不发"
    assert desk.admin("1") == hint                                     # (a bare 1 to that question: still no)
    assert desk.admin("1", quote=question.split("\n")[0]) == f"#{number} 按你改的批准了，正在发给 小美（群里「小美合作群」）"
    assert desk.prm.store.packet(number)["status"] == "approved"


def test_a_group_draft_answered_1_quoting_it_is_confirmed_for_the_group_and_不发_needs_no_quote(desk):
    number = group_packet(desk, "在的～")
    other = group_packet(desk, "好的～")
    desk.review.sweep()
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美（群里「小美合作群」）"
    assert desk.admin(f"不发 {other}") == f"#{other} 不发了（她再发消息时会有新的草稿）"


# ── late, voice, twice, other quotes ─────────────────────────────────────

def test_an_answer_read_late_is_history_and_gets_no_reply(desk):
    packet = waiting(desk)
    assert desk.admin("1", quote=head(desk, packet["id"]), at=desk.clock.now - MAX_AGE_SEC - 60) is None
    assert desk.router._tracer.notes["ended_by"] == "wechat_review"
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.jobs() == []


def test_a_voice_answer_never_counts(desk):
    packet = waiting(desk)
    for said in (f"[语音] 发 {packet['id']}。", "[语音] 1"):
        assert desk.admin(said, msg_type=34) == VOICE_TEXT
        desk.review.note_sent(ADMIN, head(desk, packet["id"]))          # (a notification the last thing said again)
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.admin("[语音] 帮我看看今天的订单", msg_type=34) is None      # (no answer: as before)


def test_a_voice_message_is_taken_only_when_its_words_typed_would_be(desk):
    waiting(desk)
    assert desk.admin("[语音] 改 3 处错别字", msg_type=34) is None        # (no notification #3: as before)
    assert desk.admin("[语音] 1", msg_type=34) == VOICE_TEXT             # (a 1 would answer the last notification)
    assert desk.admin("[语音] 1", msg_type=34) is None                  # (the last thing said: that reply)


def test_an_answer_handed_on_twice_counts_once(desk):
    packet = waiting(desk)
    msg = {"message_id": "dup1", "chat_id": ADMIN, "group_name": "管理员", "sender_id": ADMIN, "sender_name": "管理员",
           "content": "1", "msg_type": QUOTE, "timestamp": desk.clock.now, "is_group": False,
           "quoted_content": head(desk, packet["id"])}
    assert desk.router.handle(dict(msg)).endswith("正在发给 小美")
    assert desk.router.handle(dict(msg)) is None


def test_a_quote_of_another_message_is_no_draft_answer_but_a_number_still_says_which(desk):
    packet = waiting(desk)
    assert desk.admin("1", quote="#1 完成（3分钟）\n结果在这里") is None      # (a relay result: its job number)
    desk.review.note_sent(ADMIN, head(desk, packet["id"]))
    assert desk.admin("2", quote="这是新款的色卡") is None                  # (quoting something else: never a bare 2)
    assert desk.admin("好", quote="这是新款的色卡") is None
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.admin(f"发 {packet['id']}", quote="这是新款的色卡") == f"#{packet['id']} 已批准，正在发给 小美"


def test_a_number_never_told_says_so_only_when_a_notification_is_quoted(desk):
    packet = waiting(desk)
    number = desk.prm.store.add_packet(now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id="buyin:conv1", kind="other",
                                       text="您好～", attachments=[], intent="", slots={}, stage_proposal="",
                                       needs_attention=False, reason="", tool=BUYIN_TOOL, meta={"channel": "buyin"})
    assert desk.admin(f"发 {number}") is None                            # (not told here: it goes on, as before)
    assert desk.admin(f"发 {number}", quote=head(desk, packet["id"])) == (
        f"#{number} 没有发到微信里审批（只有发到这里的草稿能在微信里回）：请在控制台处理")
    assert desk.admin("发 99", quote=head(desk, packet["id"])) == "没有找到草稿 #99"
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"


def test_read_takes_typed_text_quotes_and_voice_only():
    said = read({"msg_type": 1, "content": "发12"})
    assert (said.kind, said.content, said.quoted) == ("text", "发12", False)
    assert read({"msg_type": 3, "content": "[图片]"}) is None
    assert read({"msg_type": 49, "content": "我的主页"}) is None                  # a link: no quote
    other = read({"msg_type": 49, "content": "1", "quoted_content": "#1 完成（3分钟）"})
    assert (other.kind, other.quoted) == ("text", True)
    quoted = read({"msg_type": 49, "content": "改天再说", "quoted_content": "#12 草稿 · 小美 · 私聊\n她：…"})
    assert (quoted.kind, quoted.number, quoted.head, quoted.content) == ("quote", 12, "草稿", "改天再说")
    question = read({"msg_type": 49, "content": "1", "quoted_content": "wxid_x:\n#7 改成：『明天寄』\n回 1 发出／2 不发"})
    assert (question.number, question.head, question.asked) == (7, "改成", "明天寄")
    assert read({"msg_type": 49, "content": "1", "quoted_content": "#7 改成：『明天"}).asked is None
    assert read({"msg_type": 34, "content": "[语音] 发 12。"}).content == "发 12"
    assert read({"msg_type": 34, "content": "[语音]"}) is None


# ── a platform action is the console's ───────────────────────────────

def platform_packet(desk, verdict="ready") -> int:
    return desk.prm.store.add_packet(
        now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="寄样单建好啦～",
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
        tool="jst.sample_order",
        platform={"action": {"action": "jst.sample_order"}, "preview": {"verdict": verdict,
                                                                         "catalog": {"title": "测试款贝雷帽"}}})


def test_a_platform_draft_is_told_but_only_the_console_runs_it(desk):
    number = platform_packet(desk)
    desk.review.sweep()
    text = desk.told()[0]
    assert "平台动作：聚水潭 · 达人寄样单（测试款贝雷帽）" in text
    assert text.endswith("这份带平台动作，请在控制台「批准并执行」；微信里只能回 2 不发")
    hint = f"#{number} 带平台动作：微信里不能执行，请在控制台点「批准并执行」；不要的话回 2"
    assert desk.admin("1") == hint                                     # (the bare 1 to its notification)
    for answer in ("1", "好", "改 好的", "就这样吧", "[微笑]"):
        assert desk.admin(answer, quote=head(desk, number)) == hint
    assert desk.admin(f"发 {number}") == hint and desk.admin(f"改 {number} 好的") == hint
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.jobs("prm") == []
    assert desk.admin("2", quote=head(desk, number)) == f"#{number} 不发了（她再发消息时会有新的草稿）"
    assert desk.prm.store.packet(number)["status"] == "dropped"


def test_a_platform_draft_whose_preview_failed_says_so(desk):
    platform_packet(desk, verdict="login")
    desk.review.sweep()
    assert "；预览没通过（" in desk.told()[0]


# ── red lines: only the admin's own private chat, and never the relay's ───────

@pytest.mark.parametrize("relay_only", ["off", "on"])
def test_a_draft_answer_never_becomes_a_cc_task_a_relay_approval_or_an_admin_command(clock, tmp_path, relay_only):
    desk = Desk(clock, tmp_path, relay_only=relay_only)
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("cc 跑一下测试")                                           # a cc task that asks for an approval
    job = desk.relay.next_job(0, ("admin",))
    ask = desk.relay.ask(job["id"], {"tool": "Bash", "detail": "清理 build 目录", "timeout_s": 600})
    desk.relay.tick()
    packet = waiting(desk)
    number = packet["id"]
    for content, quote in (("好", head(desk, number)), (f"发 {number}", None)):
        desk.admin(content, quote=quote)
    assert desk.prm.store.packet(number)["status"] == "approved"
    assert desk.relay.store.get_ask(ask["ask_id"])["status"] == "pending"     # the relay's question still waits
    second = waiting(desk, "那我等你", text="好的～")
    for content, quote in ((f"改 {second['id']} 好的老师", None), ("好的", head(desk, second["id"])),
                           (f"不发 {second['id']}", None)):
        desk.admin(content, quote=quote)
    assert desk.prm.store.packet(second["id"])["final_text"] == "好的老师"
    assert len(desk.jobs()) == 1                                         # only the cc task: nothing else went to the Mac
    assert desk.relay.store.followups(job["id"], 0) == []
    assert desk.relay.store.get_ask(ask["ask_id"])["status"] == "pending"
    # the controls: every other line of the admin works as before
    assert desk.admin("好").startswith(f"好，已批准 #{job['id']}")          # (the relay's question, plain 好)
    assert desk.admin("查状态", quote=head(desk, second["id"])).startswith("账号状态")   # (a command, quoting or not)
    assert "已交给 Mac" in desk.admin("cc 新任务 再跑一次")
    if relay_only == "on":
        assert "已交给 Mac" in desk.admin("新任务 导出今天的订单")
    assert len(desk.jobs()) == (3 if relay_only == "on" else 2)
    desk.stop()


def test_only_the_admins_own_one_to_one_chat_counts(desk):
    packet = waiting(desk)
    number = packet["id"]
    notice = head(desk, number)
    assert desk.admin(f"发 {number}", group=True, chat="room1@chatroom") is None          # the admin in a group
    assert desk.admin("1", quote=notice, group=True, chat="room1@chatroom") is None
    assert desk.admin("1", group=True, chat="room1@chatroom") is None
    assert desk.admin(f"发 {number}", sender="wxid_helper", chat="wxid_helper") is None    # somebody else
    assert desk.admin("1", sender="wxid_helper", chat="wxid_helper") is None
    assert desk.admin(f"发 {number}", sender="wxid_helper", chat=ADMIN) is None            # (not the admin's line)
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.to(CREATOR) == []


def test_a_creators_answer_in_her_own_chat_is_only_her_words(desk):
    """Whatever she writes in her chat — 「1」, 「发 1」, a quote of a notification's words — is her message for the next
    draft (the PRM's step), never a decision: nothing is approved, nothing is said to her."""
    packet = waiting(desk)
    for content, quote in (("1", None), (f"发 {packet['id']}", None), ("1", head(desk, packet["id"]))):
        msg = {"message_id": f"h{next(IDS)}", "chat_id": CREATOR, "group_name": "小美", "sender_id": CREATOR,
               "sender_name": "小美", "content": content, "msg_type": QUOTE if quote else 1,
               "timestamp": desk.clock.now, "is_group": False, "quoted_content": quote or ""}
        assert desk.router.handle(msg) is None and desk.router._tracer.notes["ended_by"] == "prm"
    assert desk.prm.store.packet(packet["id"])["status"] == "superseded"        # (her new lines: a new draft)
    assert not desk.prm.store.packets(("approved", "sent")) and desk.to(CREATOR) == []


# ── the log, the clean-up, the console, the switch, the wiring ───────────

def test_the_log_has_ids_and_counts_only(desk, caplog):
    caplog.set_level(logging.DEBUG)
    desk.her("我的地址：广东省佛山市南海区某某路100号")
    packet = desk.mac(text="收到地址啦～明天寄出")
    desk.review.sweep()
    desk.admin("明天一早就寄～", quote=head(desk, packet["id"]))
    desk.admin("1")
    desk.prm.tick()
    logged = caplog.text
    assert f"draft {packet['id']} told to the admin's chat" in logged and "proposed edit (7 chars)" in logged
    for words in ("小美", "某某路", "收到地址啦", "明天一早就寄", "她："):
        assert words not in logged, words


def test_the_clean_up_drops_the_closed_rows_and_cuts_a_waiting_proposed_edit(desk):
    first = waiting(desk, "我的地址：广东省佛山市南海区某某路100号", text="收到地址～")
    desk.admin("2", quote=head(desk, first["id"]))
    second = waiting(desk, "就寄这里", text="好的～")
    desk.admin("就寄广东省佛山市南海区某某路100号哈", quote=head(desk, second["id"]))
    counts = desk.prm.purge(desk.prm.store.deal(1), desk.clock.now)
    assert counts["wechat_reviews"] == 2
    assert desk.prm.store.wechat_review(first["id"]) is None
    row = desk.prm.store.wechat_review(second["id"])
    assert row["state"] == "proposed" and "某某路" not in row["proposed"] and "广东省佛山市" in row["proposed"]
    desk.review.sweep()
    assert len([t for t in desk.told() if t.startswith(f"#{second['id']} 草稿")]) == 1     # (told once: never again)
    assert desk.admin("1", quote=head(desk, first["id"])) == f"#{first['id']} 已经处理过了：点了不发"   # (its row is gone)


def test_the_console_says_where_the_drafts_go_and_which_were_told(desk):
    packet = waiting(desk)
    desk.her("在吗", chat=CREATOR)
    second = desk.mac(text="在的～")
    snap = desk.prm.snapshot()
    assert snap["wechat_review"] == {"on": True, "why": "", "open": 0, "unsent": 0, "unsure": 0, "older": 0}
    assert [(p["id"], p["wechat_at"]) for p in snap["pending"]] == [(second["id"], None)]
    desk.review.sweep()
    snap = desk.prm.snapshot()
    assert snap["wechat_review"]["open"] == 1 and snap["pending"][0]["wechat_at"] == desk.clock.now
    assert packet["id"] != second["id"]


def test_the_stopped_bot_says_why_nothing_goes_to_wechat(monkeypatch):
    from src.web import server

    monkeypatch.delenv("PRM_WECHAT_REVIEW", raising=False)
    assert server._wechat_review_stopped_status() == {"on": False}
    monkeypatch.setenv("PRM_WECHAT_REVIEW", "on")
    monkeypatch.setenv("ADMIN_WXID", "")
    assert server._wechat_review_stopped_status()["why"] == NO_ADMIN_WHY
    monkeypatch.setenv("ADMIN_WXID", ADMIN)
    assert server._wechat_review_stopped_status() == {"on": True, "why": "机器人没有运行：草稿发不到管理员微信里",
                                                       "open": 0, "unsent": 0, "unsure": 0, "older": 0}


def test_the_switch_is_off_by_default_and_checked_like_the_others(monkeypatch):
    from src.config import BotConfig, _validate_config, load_config

    assert BotConfig().prm_wechat_review == "off"
    for name in ("PRM_WECHAT_REVIEW",):
        monkeypatch.delenv(name, raising=False)
    assert load_config().prm_wechat_review == "off"
    monkeypatch.setenv("PRM_WECHAT_REVIEW", "yes")
    assert load_config().prm_wechat_review == "on"
    with pytest.raises(RuntimeError, match="PRM_WECHAT_REVIEW must be off or on"):
        _validate_config({"prm_wechat_review": "maybe"})
    _validate_config({"prm_wechat_review": "on", "admin_wxid": ""})      # (not refused: the console says why)


def test_the_wcdb_backend_tells_its_sent_hook_each_text_that_went_out(caplog):
    from src.config import BotConfig
    from src.wechat.wcdb_backend import WcdbBackend

    backend = WcdbBackend(bot_display_name=BOT, groups=["*"], config=BotConfig())
    heard, results = [], iter([True, False, True, True])
    backend._window_target = lambda chat, name=None: ("管理员", None)
    backend._send_and_confirm = lambda name, chat, text: next(results)
    backend.send_text(ADMIN, "不会有人听到")                              # (no hook yet: as before)
    backend.set_sent_hook(lambda chat, text: heard.append((chat, text)))
    assert backend.send_text(ADMIN, "#1 草稿 · 小美 · 私聊") is False      # (WeChat did not take it: not heard)
    assert backend._send_part("管理员", ADMIN, "#1 改成：『好的』") is True  # (a route reply's bubble)
    assert heard == [(ADMIN, "#1 改成：『好的』")]
    backend.set_sent_hook(lambda chat, text: 1 / 0)                      # a hook that breaks never breaks the send
    assert backend.send_text(ADMIN, "再来一条") is True and "The sent hook failed" in caplog.text


def test_the_bot_lets_the_review_hear_the_backends_sends_only_with_the_switch_on(tmp_path):
    from src.bot import Bot
    from src.web.server import register_prm

    for review in ("on", "off"):
        heard = []
        backend = SimpleNamespace(send_text=lambda chat, text: True, set_sent_hook=heard.append)
        relay = ClaudeRelay(config(), store=RelayStore(":memory:"), files_dir=tmp_path / review,
                            send=lambda chat, text: True)
        prm = Bot._start_prm(config(prm_wechat_review=review), SimpleNamespace(_store=None), backend, relay)
        try:
            assert heard == ([prm.wechat_review.note_sent] if review == "on" else []), review
        finally:
            register_prm(None)
            prm.stop()
            relay.stop()
