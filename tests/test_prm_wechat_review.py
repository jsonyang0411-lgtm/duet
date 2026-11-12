"""微信里审批草稿 (PRM_WECHAT_REVIEW, 2026-11-09, src/prm/wechat_review.py): the drafts waiting in 待审批 are told, once
each, to the admin's private chat, and the admin decides them there: quoting a notification (1 / 2 / 改 … / 执行), or
「发 N」「不发 N」「改 N …」「执行 N」, a bare 1 / 2 to the last thing the bot said there.  The same decision as the
console's; nothing of it with the switch off.  Each finding of the 2026-11-09 review (1–10) has its tests here, and so
does 「执行」 (a platform action approved from WeChat), and the integrated review of 2026-11-10 (the end of this file: a
draft that changes after its notice, a cut 要注意, her ids and addresses, ADMIN_WXID).

The relay, the PRM service and the router as Bot.run wires them (``Desk``), with a fake WeChat that records every
message the bot sends and tells the review each one, as the WCDB backend's sent hook does (src/bot.py).  Names,
numbers, ids and addresses are made up.
"""

import io
import itertools
import json
import logging
import sqlite3
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from platform_fakes import DY_ITEM, DY_ITEM2, DY_SKU, DY_SKU2, DY_SKU2B, DY_UID, JST_ORDER, XHS_BUYER, XHS_ITEM
from router_support import BOT, TraceSpy
from test_prm import ADMIN, CREATOR, Clock, config
from test_prm_platform_flow import CHANNEL, DESIGNATED, block, record

from src.admin import RELAY_HELP, AdminCommandHandler
from src.db import MessageStore, initialize_db
from src.prm import deals, draft_context, gates, platforms
from src.prm import handcards as hc
from src.prm import wechat_review as wr
from src.prm.service import SETTLE_SEC, UNSURE_ERROR, PrmService, platform_state
from src.prm.store import BUYIN_TOOL, PrmStore
from src.prm.wechat_review import (BACKLOG_SEC, BARE_UNSURE, CC_HINT, CLOSED_RACE_SEC, HELP_YES, HINT_SEC, NO_ADMIN_WHY,
                                   NOTICE_CHARS, ON_AT, PICTURES_MISSING, QUEUE_RETRY, QUEUE_UNSURE, QUOTED_ELSEWHERE, QUOTED_NUMBERED,
                                   READ_BACK_EARLY_SEC, REFUSE_TEXT, RELAY_QUIET_SEC, UNCERTAIN, WHICH_EXECUTE,
                                   RETRY_SEC, SEND_TRIES, UNSURE_GRACE_SEC, VOICE_TEXT, read)
from src.relay.service import MAX_AGE_SEC, ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter

OTHER = "wxid_creator02"            # a second creator (小雨)
THIRD = "wxid_creator03"            # a third (小云)
GROUP = "200000009@chatroom"        # a 老板拉的群 (its name is made up)
IDS = itertools.count(1)
QUOTE = 49                          # a WeChat 引用 (local_type 57 << 32 | 49: the backend gives 49 and the quote)
ANSWER_DELAY = 1                    # the admin writes a second after what the bot said (WeChat's whole seconds)


def reply_draft(text="收到啦～戴着还合适吗？", **extra):
    values = {"intent": "收到样品", "slots": {}, "stage_proposal": "", "message_kind": "greeting", "reply_draft": text,
              "attachments_to_send": [], "needs_attention": False, "reason": "kb/合作政策.md"}
    values.update(extra)
    return values


class Desk:
    """The relay, the PRM service and the router as Bot.run wires them; ``sent``: every message the bot sent, in
    order (the PRM's and the relay's sends; the route's replies are what ``admin`` gives back), ``files`` every file.
    Each text that goes out is told to the review as the WCDB backend tells it (src/bot.py): sent (``set_sent_hook``)
    and read back with WeChat's time, the test's clock (``set_read_back_hook``).  The admin's lines are written
    ANSWER_DELAY after the clock (a person reads first).  A send to a chat in ``refuse`` fails, and every file while
    ``files_fail``; ``order`` is what went out, texts and files, in order."""

    def __init__(self, clock, tmp_path, *, review="on", relay_only="off", admin=ADMIN, store=None, relay_store=None,
                 **prm):
        self.clock = clock
        self.sent: list = []
        self.files: list = []
        self.order: list = []
        self.refuse: set = set()
        self.files_fail = False
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
        self.order.append(("text", chat, text))
        self._said(chat, text)
        return True

    def _said(self, chat, text):
        self.review.note_sent(chat, text)                       # (the backend's sent hook)
        self.review.note_read_back(chat, text, self.clock.now)  # (... and its line read back, WeChat's time)

    def _send_file(self, chat, path):
        self.files.append((chat, path))
        if chat in self.refuse or self.files_fail:
            return False
        self.order.append(("file", chat, path))
        return True

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
               "timestamp": self.clock.now + ANSWER_DELAY if at is None else at, "is_group": group,
               "quoted_content": quote or "", **extra}
        reply = self.router.handle(msg)
        if reply:
            self._said(chat, reply)
        return reply

    def told(self) -> list[str]:
        """What the bot sent the admin besides the route's replies (the notifications, a reply told first, what came of
        a decision, the relay's messages)."""
        return [text for chat, text in self.sent if chat == ADMIN]

    def to(self, chat=CREATOR) -> list[str]:
        return [text for c, text in self.sent if c == chat]

    def jobs(self, kind="admin") -> list[dict]:
        return [j for j in self.relay.store.list_jobs(limit=100) if j.get("kind", "admin") == kind]

    def executions(self) -> list[dict]:
        return [j for j in self.relay.store.list_jobs(limit=100) if '"platform_execute"' in str(j.get("meta") or "")]


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


# 聚水潭寄样单 as the Mac previews it (platforms._jst_facts): two products of one shop, a receiver whose number is a
# placeholder (the notification shows only the province and the city)
SAMPLE = {"action": "jst.sample_order", "shop": "dy", "creator_platform": "dy", "creator_uid": DY_UID,
          "items": [{"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]},
                    {"item_id": DY_ITEM2, "skus": [{"sku_id": DY_SKU2, "qty": 1}, {"sku_id": DY_SKU2B, "qty": 2}]}],
          "receiver": {"name": "小美", "phone": "〈手机1〉", "province": "浙江省", "city": "杭州市", "district": "西湖区",
                       "address": "测试路100号3栋1201"},
          "logistics": "SF"}
JST_FACTS = [
    {"label": "达人", "value": "小美（dy xm_test01，粉丝 12000）；归这个子账号跟进"},
    {"label": "收货", "value": "小* 139****1111 浙江省杭州市西湖区 测试路***（达人名下的地址）"},
    {"label": "快递", "value": "顺丰速运（SF）"},
    {"label": "商品", "value": "测试款贝雷帽（测试抖店）：HAT-RED-01 红色 × 1（售价 59，平台库存 30）"},
    {"label": "商品", "value": "测试款渔夫帽（测试抖店）：FSH-GRY-02 灰色 × 1（售价 79，平台库存 5）；"
                               "FSH-BLK-02 黑色 × 2（售价 79，平台库存 8）"},
    {"label": "查重", "value": "这位达人没寄过这个商品"},
]


def jst_block(verdict="ready", **extra):
    step = {"name": "preview", "label": "聚水潭 · 预览寄样单", "execute": False, "command": "node bin/jst.mjs sample-create",
            "exit_code": 0, "seconds": 3.0, "verdict": verdict, "outcome": "",
            "problems": [] if verdict == "ready" else ["平台没登录"], "facts": JST_FACTS, "raw": "{}"}
    return block(SAMPLE, verdict=verdict, run={"steps": ["create"]},
                 **{"steps": [step], "facts": [], "catalog": {"synced_at": 1.0, "title": "测试款贝雷帽"}, **extra})


def platform_waiting(desk, preview=None, *, text="寄样单建好啦，单号 {寄样单号}～", group=False, files=None) -> dict:
    """She agrees, the Mac drafts a reply with a platform action and previews it (a sample order unless ``preview``);
    the draft waits for a person and is told (the sender thread's round).  ``group``: it goes into her 老板拉的群;
    ``files``: the pictures for the operator (``meta.operator_files``)."""
    desk.prm.store.token_for(1, "phone", "手机", "13900001111", desk.clock.now)     # 〈手机1〉: the receiver's number
    desk.her("好的，寄吧")
    packet = desk.mac(text=text, message_kind="money", platform=preview or jst_block())
    assert packet["status"] == "pending" and packet["tool"] != "message.send"
    if group:
        desk.prm.store.aim_packet(packet["id"], GROUP, {"channel": "group", "group_name": "小美合作群"})
    if files is not None:
        desk.prm.store.aim_packet(packet["id"], GROUP if group else CREATOR, {"operator_files": files})
    desk.review.sweep()
    return desk.prm.store.packet(packet["id"])


def her_uid_known(desk, uid=DY_UID) -> int:
    """A verified 抖店 sample order of hers for this UID, straight into the store: a 专属链接 binding it has no doubt about
    her UID, its 要注意 short enough for a notification to show whole (only the 专属佣金 the bot did not open): 「执行」
    can run it from WeChat (a cut 要注意 cannot: integrated review 2026-11-10 A-L1)."""
    action = {"action": "jst.sample_order", "creator_platform": "dy", "creator_uid": uid}
    return desk.prm.store.add_packet(
        deal_id=desk.prm.store.deal_of(1)["id"], party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="",
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="", status="sent",
        tool="jst.sample_order", platform={"action": action, "execution": {"outcome": "VERIFIED_SUCCESS",
                                                                           "created": {"creator_uid": uid}}})


def mac_runs(desk, rec):
    """The Mac takes the execution and answers with ``rec`` (``record``), under the approval's key."""
    job = desk.relay.next_job(0, ("prm",))
    assert job is not None and job["meta"]["op"] == "platform_execute"
    assert desk.relay.finish(job["id"], {"ok": True, "text": json.dumps({**rec, "key": job["meta"]["key"]}),
                                         "duration_s": 9})
    desk.relay.tick()


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
    for content, quote in ((f"发 {packet['id']}", None), (f"执行 {packet['id']}", None),
                           ("1", f"#{packet['id']} 草稿 · 小美 · 私聊"), ("执行", f"#{packet['id']} 草稿 · 小美 · 私聊")):
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
    proposal is gone; a short remark built around a yes (「对」) is never a proposal at all."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("这个价格对吗？", quote=head(desk, number)) == f"#{number} {HELP_YES}"
    assert desk.admin("这个价格要不要再降一点", quote=head(desk, number)).startswith(f"#{number} 改成：『这个价格要不要再降一点』")
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
        assert reply == f"#{number} {HELP_YES}"
        assert desk.jobs() == []
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "收到啦～戴着还合适吗？"


@pytest.mark.parametrize("words", ["发吧", "发出", "照发", "可以发", "同意发送", "确认", "对", "是的", "好的呀", "嗯嗯", "可以的",
                                   "没问题", "好滴", "行吧", "OK的", "好的👌", "嗯嗯，发吧～",
                                   "妥", "妥了", "yes", "Y", "冲", "安排", "就这样", "没毛病", "要得", "go", "棒", "赞", "就按这个",
                                   "好好好好好好好好好", "嗯嗯嗯嗯嗯嗯嗯嗯嗯", "对对对对对对对对对对", "可以可以可以可以可以"])
def test_a_yes_in_other_words_quoting_a_notification_is_never_sent_to_her(desk, words):
    """Second review, finding 1 (probe 3, 16 of 16): an approval in words that are no yes word became the proposal, and
    the natural next bare 1 sent 「好的呀」 to her.  A short text built around a yes is never a proposal: the bot says
    how to answer, and the next bare 1 sends nothing.  Third verification item 1 (15 of 15): words outside the yes
    stems (「妥」「冲」「安排」「go」「就按这个」…: any quoted text of SHORT_CHARS letters or fewer) and a yes said over and over
    (「好好好好好好好好好」: yes words only, at any length) became proposals too: never now."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin(words, quote=head(desk, number)) == f"#{number} {HELP_YES}"
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["proposed"]) == ("notified", "")
    assert desk.admin("1").startswith("机器人后来又说了别的，单独一个 1 / 2 不算")
    packet = desk.prm.store.packet(number)
    assert packet["status"] == "pending" and packet["final_text"] is None
    question = desk.admin("那就明天给您寄出哈", quote=head(desk, number))     # (real words for her: a proposal)
    assert question.startswith(f"#{number} 改成：『那就明天给您寄出哈』")
    assert desk.admin("好的呀", quote=question.split("\n")[0]) == (
        f"#{number} 要发这份改稿：引用那一问回 1；{HELP_YES}")
    assert desk.admin("1", quote=question.split("\n")[0]) == f"#{number} 按你改的批准了，正在发给 小美"


def test_a_short_quoted_text_is_never_a_proposal_and_a_short_改_asks_for_the_words(desk):
    """Third verification item 1: quoting a notification, a text of at most SHORT_CHARS letters is the admin reacting
    (an edit says 「改 …」): even real words (「在的」) only get the hint, and a short one starting with 改 (「改一下」) asks
    what to.  Five letters or more are a proposal, as before."""
    packet = waiting(desk)
    number = packet["id"]
    for words in ("在的", "在的～", "收到！"):
        assert desk.admin(words, quote=head(desk, number)) == f"#{number} {HELP_YES}", words
    for words in ("改一下", "改下", "改改吧", "改成在的"):
        assert desk.admin(words, quote=head(desk, number)) == (
            f"#{number} 要改成什么？引用通知回「改 你的话」，或者回「改 {number} 你的话」"), words
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["proposed"]) == ("notified", "")
    assert desk.admin("在的老师您说", quote=head(desk, number)).startswith(f"#{number} 改成：『在的老师您说』")
    assert desk.admin("改 在的", quote=head(desk, number)) == f"#{number} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "在的"


def test_unquoted_发_n_sends_the_draft_as_drafted_whatever_was_proposed(desk):
    packet = waiting(desk)
    number = packet["id"]
    desk.admin("明天给您寄～", quote=head(desk, number))
    assert desk.admin(f"发 {number}") == f"#{number} 已批准，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "收到啦～戴着还合适吗？"


def test_a_proposed_edit_is_checked_when_it_is_made(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("麻烦核对下电话〈手机2〉哈", quote=head(desk, number)) == (
        f"#{number} 不能这样改：草稿里有对不上的占位符：〈手机2〉")
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
    still waits, one told here and no longer waiting gets what became of it (second review N5)."""
    packet = waiting(desk)
    for words in ("发 99", "不发 99", "执行 99", "改 99 好的", "改 3 处错别字"):
        assert desk.admin(words) is None and desk.router._tracer.notes["ended_by"] == "silence", words
    assert desk.prm.decide(packet["id"], "drop")["ok"]
    assert desk.admin(f"改 {packet['id']} 好的老师") == f"#{packet['id']} 已经处理过了：点了不发"
    assert desk.admin(f"改 {packet['id']}") == f"#{packet['id']} 已经处理过了：点了不发"
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
    """Second review N2 (probe 5): with several told drafts waiting, a bare 1 approved the last told one even when the
    admin was reading the first.  It counts only when the last one told is the only one waiting; else the list."""
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    desk.clock.now += 120
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")     # told last
    desk.clock.now += 5
    listed = (f"现在有 2 份在等你回：#{first['id']} 小美、#{second['id']} 小雨；单独一个 1 / 2 不算：请引用那条通知回，"
              f"或者回「发 {first['id']}」这样带编号")
    assert desk.admin("1") == listed
    assert {desk.prm.store.packet(p["id"])["status"] for p in (first, second)} == {"pending"}
    assert desk.prm.decide(first["id"], "drop")["ok"]                     # the console takes the first
    desk.clock.now += CLOSED_RACE_SEC + 1
    desk.prm.register(THIRD, "小云", "")
    third = waiting(desk, "在吗", chat=THIRD, text="在的～")
    assert desk.admin("1").startswith("现在有 2 份在等你回")              # (#second waits too)
    assert desk.admin(f"不发 {second['id']}").endswith("不发了（她再发消息时会有新的草稿）")
    desk.clock.now += 1
    desk.review.note_sent(ADMIN, head(desk, third["id"]))                  # (its notification the last thing said again)
    desk.review.note_read_back(ADMIN, head(desk, third["id"]), desk.clock.now)
    assert desk.admin("2") == f"#{third['id']} 不发了（她再发消息时会有新的草稿）"
    for word in ("1", "2"):                                             # the last thing said: the bot's reply
        desk.clock.now += HINT_SEC + 1
        assert desk.admin(word) is None and desk.router._tracer.notes["ended_by"] == "silence"


def test_three_drafts_told_in_one_round_are_never_taken_by_a_bare_1(clock, tmp_path):
    """Second review N2 (probe 5, t1): three notifications seconds apart; the admin reads the first and types 1."""
    desk = Desk(clock, tmp_path)
    ids = []
    for chat, name in ((CREATOR, "小美"), (OTHER, "小雨"), (THIRD, "小云")):
        desk.prm.register(chat, name, "")
        desk.her("在吗", chat=chat)
        ids.append(desk.mac(text="在的～")["id"])
    assert desk.review.sweep() == 3
    clock.now += 20
    assert desk.admin("1").startswith("现在有 3 份在等你回：")
    assert {desk.prm.store.packet(n)["status"] for n in ids} == {"pending"}
    desk.stop()


def test_a_bare_answer_after_any_other_message_of_the_bot_is_nobodys(desk):
    """A bare 1 / 2 after the bot said something else (the relay's receipt): never the draft's.  Shortly after the
    notification the bot says so (second review N5: not silence); later it goes on as before."""
    packet = waiting(desk)
    number = packet["id"]
    assert "已交给 Mac" in desk.admin("cc 帮我查一下今天的订单")            # (the relay's receipt: the last thing said)
    hint = f"机器人后来又说了别的，单独一个 1 / 2 不算：#{number} 还在等，引用那条通知回 1，或者回「发 {number}」「不发 {number}」"
    assert desk.admin("1") == hint and desk.admin("２") == hint
    desk.clock.now += HINT_SEC + 1
    for word in ("1", "2"):
        assert desk.admin(word) is None
    assert desk.prm.store.packet(number)["status"] == "pending"


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
    assert desk.admin("2").startswith("机器人后来又说了别的，单独一个 1 / 2 不算")
    assert desk.prm.decide(n, "drop")["ok"]
    desk.clock.now += CLOSED_RACE_SEC + 1
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")     # told right after the task ended
    assert desk.admin("1").startswith(f"Claude 那边有任务在做或者刚做完，单独一个 1 / 2 不算：#{second['id']}")
    assert desk.prm.decide(second["id"], "drop")["ok"]
    desk.clock.now += RELAY_QUIET_SEC
    third = waiting(desk, "在吗", chat=THIRD, text="在的～")
    assert desk.admin("1") == f"#{third['id']} 已批准，正在发给 小云"


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


def test_the_admins_own_decision_a_moment_ago_never_blocks_a_bare_answer(desk):
    """Second review N5: the 60 s 「刚有一份处理掉了」 rule fired for the admin's own decision made in WeChat."""
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")
    assert desk.admin(f"不发 {first['id']}").endswith("不发了（她再发消息时会有新的草稿）")    # (the admin, a moment ago)
    desk.review.note_sent(ADMIN, head(desk, second["id"]))
    desk.review.note_read_back(ADMIN, head(desk, second["id"]), desk.clock.now)
    assert desk.admin("1") == f"#{second['id']} 已批准，正在发给 小雨"


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


@pytest.mark.parametrize("burst", [None, 3])
def test_a_cc_line_after_other_words_goes_on_first(desk, burst):
    """Second review N1 (probe 4): 「发 N\\n谢谢\\ncc …」: the relay looks at the start of the message only, the cc line
    after 「谢谢」 was lost.  It goes on first."""
    packet = waiting(desk)
    number = packet["id"]
    extra = {"_burst_count": burst} if burst else {}
    reply = desk.admin(f"发 {number}\n谢谢\ncc 查一下今天的订单", **extra)
    assert "已交给 Mac" in reply and [job["text"] for job in desk.jobs()] == ["查一下今天的订单\n谢谢"]
    assert desk.told()[-1] == f"#{number} 已批准，正在发给 小美"


@pytest.mark.parametrize("burst", [None, 2])
def test_an_admin_command_with_other_words_refuses_the_whole_message(desk, burst):
    """Second review N1: 「谢谢\\n不发 N\\n查状态」: the admin step matches a command whole; with other words it cannot
    go on as one: the whole message is refused (nothing done)."""
    packet = waiting(desk)
    number = packet["id"]
    extra = {"_burst_count": burst} if burst else {}
    assert desk.admin(f"谢谢\n不发 {number}\n查状态", **extra) == REFUSE_TEXT
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin(f"不发 {number}\n查状态", _burst_count=2).startswith("账号状态")    # (alone: it goes on)
    assert desk.prm.store.packet(number)["status"] == "dropped"


def test_a_quote_with_other_words_and_a_cc_line_sends_nothing_and_the_task_goes_on(desk):
    packet = waiting(desk)
    number = packet["id"]
    reply = desk.admin("好的\n辛苦\ncc 查一下今天的订单", quote=head(desk, number))
    assert "已交给 Mac" in reply and [job["text"] for job in desk.jobs()] == ["查一下今天的订单"]
    assert desk.told()[-1] == f"#{number} {HELP_YES}"
    assert desk.prm.store.packet(number)["status"] == "pending"


def test_a_quote_with_an_admin_command_and_other_words_is_refused_whole(desk):
    """Third verification item 5 (probe i5): 「谢谢\\n查状态」 quoting a notification ran the command and kept 「谢谢」 as
    the proposed edit.  As without a quote, a command with other words refuses the whole message; with an answer (1, 2,
    执行, 改 …) the command goes on alone, as before."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("谢谢\n查状态", quote=head(desk, number)) == REFUSE_TEXT
    assert desk.router._tracer.notes["ended_by"] == "wechat_review"
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["proposed"]) == ("notified", "")
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin("这样回她吧：明天给您寄\n查状态", quote=head(desk, number)) == REFUSE_TEXT
    assert desk.prm.store.wechat_review(number)["proposed"] == ""
    assert desk.admin("2\n查状态", quote=head(desk, number)).startswith("账号状态")
    assert desk.told()[-1] == f"#{number} 不发了（她再发消息时会有新的草稿）"


def test_a_burst_edit_over_several_bubbles_is_proposed_whole(desk):
    packet = waiting(desk)
    number = packet["id"]
    reply = desk.admin(f"改 {number} 好的老师\n明天一早给您寄", _burst_count=2)
    assert reply == f"#{number} 改成：『好的老师\n明天一早给您寄』\n回 1 发出／2 不发"
    assert desk.admin("1") == f"#{number} 按你改的批准了，正在发给 小美"


def test_a_bare_1_after_a_burst_whose_cc_line_got_the_relays_receipt_says_what_happened(desk):
    """Second review N5 (P10): an edit proposed in a merged burst with a cc line; the relay's receipt is the last thing
    said; the natural bare 1 got silence."""
    packet = waiting(desk)
    number = packet["id"]
    assert "已交给 Mac" in desk.admin(f"改 {number} 好的老师～明天发\ncc 查一下今天的订单", _burst_count=2)
    assert desk.admin("1") == (f"机器人后来又说了别的，单独一个 1 / 2 不算：#{number} 的改稿还在等你确认，引用「#{number} 改成」"
                               f"那一问回 1，或者回「发 {number}」发原稿、「不发 {number}」")
    assert desk.prm.store.packet(number)["status"] == "pending"


def test_an_unnumbered_改_while_a_notification_is_the_last_thing_said_asks_which(desk):
    """Second review N5: 「改 …」 with no number and no quote passed on in silence; while a notification (or question)
    is the last thing said it asks which, else it goes on as before (a cc follow-up)."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("改 好的老师明天发") == f"要改哪一份？引用那条通知回「改 你的话」，或者回「改 {number} 你的话」"
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin("改天再说") is None                                 # (no 改 + words: nothing of it)
    desk.admin("查状态")                                                 # (the last thing said: not a notification)
    assert desk.admin("改 好的老师明天发") is None


def test_an_unnumbered_改_near_a_claude_task_stays_claudes(clock, tmp_path):
    """Third verification item 13 (probe i13): in CLAUDE_RELAY_ONLY an unnumbered 「改 …」 meant for Claude's task (「改
    一下第二段」) was kept from Claude after a notification: it goes on there now.  Fourth verification item 2 (probe r5):
    in the normal mode it went on too while a relay job of this chat ran, where nobody hears it (no cc, no relay): it is
    asked about again, with how to give it to Claude; the draft is untouched."""
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("帮我写一个周报")
    job = desk.relay.next_job(0, ("admin",))
    packet = waiting(desk)
    clock.now += 30
    assert "已转给正在做的" in desk.admin("改 一下第二段，短一点")
    assert [f["text"] for f in desk.relay.store.followups(job["id"], 0)] == ["改 一下第二段，短一点"]
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    desk.stop()
    desk = Desk(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("cc 帮我写一个周报")
    job = desk.relay.next_job(0, ("admin",))
    packet = waiting(desk)
    clock.now += 30
    assert desk.admin("改 明天上午给您寄出") == (
        f"要改哪一份？引用那条通知回「改 你的话」，或者回「改 {packet['id']} 你的话」（要交给 Claude 请在前面加 cc）")
    assert desk.relay.store.followups(job["id"], 0) == [] and desk.prm.store.packet(packet["id"])["status"] == "pending"
    desk.relay.finish(job["id"], {"ok": True, "text": "周报写好了", "duration_s": 3})
    desk.relay.tick()
    clock.now += RELAY_QUIET_SEC + 1
    second = waiting(desk, "那佣金多少呀")                              # (a notification the last thing said again)
    assert desk.admin("改 好的老师明天发") == (
        f"要改哪一份？引用那条通知回「改 你的话」，或者回「改 {second['id']} 你的话」")
    desk.stop()


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
    for said in (f"[语音] 发 {packet['id']}。", f"[语音] 执行 {packet['id']}", "[语音] 1"):
        assert desk.admin(said, msg_type=34) == VOICE_TEXT
        desk.review.note_sent(ADMIN, head(desk, packet["id"]))          # (a notification the last thing said again)
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.admin("[语音] 帮我看看今天的订单", msg_type=34) is None      # (no answer: as before)


def test_a_voice_message_is_taken_only_when_its_words_typed_would_be(desk):
    waiting(desk)
    assert desk.admin("[语音] 改 3 处错别字", msg_type=34) is None        # (no notification #3: as before)
    assert desk.admin("[语音] 1", msg_type=34) == VOICE_TEXT             # (a 1 would answer the last notification)
    assert desk.admin("[语音] 1", msg_type=34) == VOICE_TEXT             # (... or get the hint: typed, it counts too)
    desk.clock.now += HINT_SEC + 1
    assert desk.admin("[语音] 1", msg_type=34) is None                  # (the last thing said: that reply, long ago)


def test_an_answer_handed_on_twice_counts_once(desk):
    packet = waiting(desk)
    msg = {"message_id": "dup1", "chat_id": ADMIN, "group_name": "管理员", "sender_id": ADMIN, "sender_name": "管理员",
           "content": "1", "msg_type": QUOTE, "timestamp": desk.clock.now, "is_group": False,
           "quoted_content": head(desk, packet["id"])}
    assert desk.router.handle(dict(msg)).endswith("正在发给 小美")
    assert desk.router.handle(dict(msg)) is None


def test_a_message_with_rest_lines_handed_on_twice_runs_once(desk):
    """Second review (P11): a 「发 N」 + cc burst handed on again: answered once, the cc task made once."""
    packet = waiting(desk)
    number = packet["id"]
    msg = {"message_id": "dup2", "chat_id": ADMIN, "group_name": "管理员", "sender_id": ADMIN, "sender_name": "管理员",
           "content": f"发 {number}\ncc 查一下今天的订单", "msg_type": 1, "timestamp": desk.clock.now + 1,
           "is_group": False, "quoted_content": "", "_burst_count": 2}
    assert "已交给 Mac" in desk.router.handle(dict(msg))
    assert desk.router.handle(dict(msg)) is None
    assert len(desk.jobs()) == 1 and desk.prm.store.packet(number)["status"] == "approved"


def test_a_quote_of_another_message_is_no_draft_answer_but_a_number_still_says_which(desk):
    """A quote of another message is no draft answer (a bare 1 / 2, a yes).  A numbered answer under it used to act;
    the final check of 7eaa0dc: it never does now, in either mode (``test_a_numbered_answer_quoting_another_message_
    never_acts``): one line says how."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("1", quote="#1 完成（3分钟）\n结果在这里") is None      # (a relay result: its job number)
    desk.review.note_sent(ADMIN, head(desk, number))
    assert desk.admin("2", quote="这是新款的色卡") is None                  # (quoting something else: never a bare 2)
    assert desk.admin("好", quote="这是新款的色卡") is None
    assert desk.admin(f"发 {number}", quote="这是新款的色卡") == QUOTED_NUMBERED.format(
        number=number, direct=f"发 {number}", quoted="发")
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin(f"发 {number}") == f"#{number} 已批准，正在发给 小美"   # (without the quote: as always)


def test_a_number_never_told_says_so_only_when_a_notification_is_quoted(desk):
    packet = waiting(desk)
    number = desk.prm.store.add_packet(now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id="buyin:conv1",
                                       kind="other", text="您好～", attachments=[], intent="", slots={},
                                       stage_proposal="", needs_attention=False, reason="", tool=BUYIN_TOOL,
                                       meta={"channel": "buyin"})
    assert desk.admin(f"发 {number}") is None                            # (not told here: it goes on, as before)
    assert desk.admin("1", quote=f"#{number} 草稿 · 小美 · 私聊") == (
        f"#{number} 没有发到微信里审批（只有发到这里的草稿能在微信里回）：请在控制台处理")
    assert desk.admin("1", quote="#99 草稿 · 小美 · 私聊") == "没有找到草稿 #99"
    assert desk.admin("发 99", quote=head(desk, packet["id"])).startswith(f"你引用的是 #{packet['id']}，写的却是「发 99」")
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


# ── 执行: a platform action approved from WeChat ─────────────────────────

def test_a_sample_orders_notification_lists_every_product_the_courier_and_only_province_and_city(desk):
    packet = platform_waiting(desk)
    number = packet["id"]
    lines = notice_of(desk, number)
    assert lines[0] == f"#{number} 草稿 · 小美 · 私聊" and "草稿：寄样单建好啦，单号 {寄样单号}～" in lines
    assert lines[lines.index("草稿：寄样单建好啦，单号 {寄样单号}～") + 1:] == [
        "平台动作：聚水潭 · 达人寄样单（预览通过，可以批准执行）",
        "· 寄样商品：测试款贝雷帽（测试抖店）：HAT-RED-01 红色 × 1",
        "· 寄样商品：测试款渔夫帽（测试抖店）：FSH-GRY-02 灰色 × 1；FSH-BLK-02 黑色 × 2",
        "· 快递：顺丰速运（SF）",
        "· 收货：浙江省杭州市（只写到市）",
        "· 达人：小美（dy …st01，粉丝 12000）；归这个子账号跟进",
        "· 查重：这位达人没寄过这个商品",
        "引用这条回「执行」在平台上执行（核验成功才把上面的回复发给她）｜2 不发",
        f"（不引用就回「执行 {number}」「不发 {number}」）",
    ]
    text = "\n".join(lines)
    for private in ("13900001111", "139****1111", "〈手机1〉", "测试路", "西湖区", "xm_test01", DY_UID, "售价", "平台库存"):
        assert private not in text, private


def test_a_preview_without_its_products_lists_the_products_ids_and_a_receiver_kept_in_jushuitan(desk):
    bare = jst_block(steps=[], catalog={"synced_at": 1.0, "title": "测试款贝雷帽"})
    bare["action"] = {k: v for k, v in bare["action"].items() if k not in ("receiver", "logistics")}
    packet = platform_waiting(desk, bare)
    lines = notice_of(desk, packet["id"])
    assert "· 商品：测试款贝雷帽（等 2 款）" in lines
    assert f"· 寄样商品：SKU × 数量：{DY_ITEM}：{DY_SKU} × 1；{DY_ITEM2}：{DY_SKU2} × 1、{DY_SKU2B} × 2" in lines
    assert "· 快递：按这台 Mac 配的" in lines and "· 收货：聚水潭里她名下的地址" in lines


def test_a_commission_and_channel_goods_list_their_products_rate_price_and_stock(desk):
    packet = platform_waiting(desk, block(DESIGNATED), text="专属佣金 20% 设好啦，明天 0 点生效～")
    lines = notice_of(desk, packet["id"])
    assert "平台动作：抖音百应 · 指定达人专属佣金（预览通过，可以批准执行）" in lines
    assert {"· 商品：测试主品", f"· 商品 ID：{DY_ITEM}", "· 达人平台账号：…0002", "· 佣金率（%）：20%",
            "· 将写入：fixed commission 20%"} <= set(lines)
    assert DY_UID not in "\n".join(lines)                               # (her UID: its last 4 only)
    assert desk.prm.decide(packet["id"], "drop")["ok"]
    sku = {"name": "create", "label": "抖店 · 建渠道品", "execute": False, "command": "node bin/dy.mjs", "exit_code": 0,
           "seconds": 2.0, "verdict": "ready", "outcome": "", "problems": [], "raw": "{}",
           "facts": [{"label": f"SKU {DY_SKU} 红色", "value": "主品价 39 元 → 渠道价 15；库存 50（可划 80）"}]}
    # (合并 channel-goods-convention 2026-11-09: the shop's way, 「【小美专属】」 and stock_per_sku; the link's doubt about
    # her UID asks for attention, and that line too shows her UID by its last 4 only)
    packet = platform_waiting(desk, block({**CHANNEL, "stock_per_sku": 50}, steps=[sku]),
                              text="专属链接给你啦：{链接} 审核通过后就能用～")
    lines = notice_of(desk, packet["id"])
    assert "平台动作：抖店 · 达人专属链接（渠道品）并绑定达人（预览通过，可以批准执行）" in lines
    assert {"· 标题前缀：【小美专属】（10 个字符，汉字算 2 个；这个商品最多几个看预览）",
            "· 每个颜色的库存（件）：50 件（主商品这个颜色可分的不够就全分）",
            "· 渠道价（元）：每个 SKU 日常价的 9 折，取整到元（每个 SKU 的数在预览里）",
            f"· SKU {DY_SKU} 红色：主品价 39 元 → 渠道价 15；库存 50（可划 80）"} <= set(lines)
    assert any(line.startswith("要注意：") and f"…{DY_UID[-4:]}" in line for line in lines)
    assert DY_UID not in "\n".join(lines)


def test_a_platform_draft_whose_preview_failed_says_it_cannot_run_from_wechat(desk):
    packet = platform_waiting(desk, jst_block(verdict="login"))
    number = packet["id"]
    lines = notice_of(desk, number)
    assert "平台动作：聚水潭 · 达人寄样单（平台没登录或登录失效（退出码 77））" in lines
    assert "· 问题：没登录" in lines and "· 问题：平台没登录" in lines
    assert lines[-1] == "预览没通过，微信里不能执行：请到控制台处理（「重新预览」）；不要的话回 2"
    assert desk.admin("执行", quote=head(desk, number)) == (
        f"#{number} 通知里的预览没通过（或者正在重新预览），微信里不能执行：请到控制台处理")
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []
    assert desk.admin("2", quote=head(desk, number)) == f"#{number} 不发了（她再发消息时会有新的草稿）"


def test_the_pictures_for_the_operator_follow_its_notification(desk, tmp_path, caplog):
    """``meta.operator_files`` as the draft-context writes it ({name, path, why}, the path one of the relay's files):
    sent right after the notification, named in it; a path outside the relay's files, or not a picture, never (a
    picture of the relay's that is not on this PC: the draft is the console's, the tests of 2026-11-10 below)."""
    folder = desk.relay.files_dir / "outfiles" / "job-1"
    folder.mkdir(parents=True)
    sheet, photo = folder / "对图单-1.jpg", folder / "帽子.png"
    sheet.write_bytes(b"\xff\xd8 not really")
    photo.write_bytes(b"\x89PNG not really")
    (folder / "说明.txt").write_text("不是图片")
    elsewhere = tmp_path / "别处.png"
    elsewhere.write_bytes(b"\x89PNG not really")
    files = [{"name": "对图单-1.jpg", "path": str(sheet), "why": "核对颜色和款式"}, str(photo),
             {"name": "别处.png", "path": str(elsewhere), "why": ""}, str(folder / "说明.txt"), {"name": "空的"}]
    packet = platform_waiting(desk, files=files)
    number = packet["id"]
    assert desk.files == [(ADMIN, str(sheet)), (ADMIN, str(photo))]
    assert desk.sent[-1][1].startswith(f"#{number} 草稿")                # (right after its notification)
    assert "（后面附 2 张图给你看：对图单-1.jpg：核对颜色和款式、帽子.png）" in notice_of(desk, number)
    assert caplog.text.count(f"a picture for the operator of draft {number} is not one of the relay's pictures") == 3
    assert str(elsewhere) not in caplog.text
    assert desk.prm.store.wechat_review(number)["pictures_ok"] == 1     # (every one went: answered from here)
    desk.review.sweep()
    assert len(desk.files) == 2                                          # (once, with the notification)


def test_执行_quoting_the_notification_runs_it_and_its_end_is_told(desk):
    packet = platform_waiting(desk)
    number = packet["id"]
    assert desk.admin("执行", quote=head(desk, number)) == (
        f"#{number} 已批准执行：Mac 正在平台上执行，做完了告诉你结果（核验成功才把回复发给她）")
    assert desk.prm.store.packet(number)["status"] == "executing"
    assert desk.prm.store.wechat_review(number)["decision"] == "execute"
    [job] = desk.executions()
    assert json.loads(job["meta"])["action"]["receiver"]["phone"] == "13900001111"   # (for the Mac, as from the console)
    before = len(desk.told())
    desk.prm.tick()
    assert len(desk.told()) == before                                   # (nothing to tell while it runs)
    mac_runs(desk, record(sample_order_no=JST_ORDER))
    desk.prm.tick()                                                     # the reply goes to her, then the admin hears
    assert desk.to(CREATOR) == [f"寄样单建好啦，单号 {JST_ORDER}～"]
    assert desk.told()[-1] == f"#{number} 已执行：聚水潭 · 达人寄样单（平台核验成功；寄样单号 {JST_ORDER}），回复已发出"
    desk.prm.tick()
    assert len([t for t in desk.told() if t.startswith(f"#{number} 已执行")]) == 1


def test_执行_n_runs_it_too_and_a_red_end_is_told_in_the_consoles_words(desk):
    packet = platform_waiting(desk)
    number = packet["id"]
    assert desk.admin(f"执行 {number}").startswith(f"#{number} 已批准执行")
    mac_runs(desk, record("FAILED"))
    desk.prm.tick()
    assert desk.to(CREATOR) == []
    assert desk.told()[-1] == (f"#{number} 执行没成功：平台拒绝了，或者没有发出（FAILED）：读回没证实，平台没有动，回复没有发；"
                               "请到控制台看")


def test_a_verified_action_whose_reply_did_not_go_is_told_so(desk):
    packet = platform_waiting(desk)
    number = packet["id"]
    desk.admin("执行", quote=head(desk, number))
    desk.refuse.add(CREATOR)
    mac_runs(desk, record(sample_order_no=JST_ORDER))
    desk.prm.tick()
    assert desk.told()[-1] == (f"#{number} 已执行：聚水潭 · 达人寄样单（平台核验成功；寄样单号 {JST_ORDER}），"
                               "但回复没有发出去（文字没发出去）；需要的话请在微信里手动发给 小美")


@pytest.mark.parametrize("row, told", [
    (dict(status="dropped", outcome="covered", error="邀约卡已经开了这个商品的专属佣金（20%），没有执行",
          platform={"execution": {"outcome": "NOT_RUN"}}),
     "#7 没有执行：邀约卡已经开了这个商品的专属佣金（20%）；回复没有发"),
    (dict(status="failed", outcome="indeterminate", error=UNSURE_ERROR,
          platform={"execution": {"outcome": "VERIFIED_SUCCESS", "created": {}}}),
     "#7 已执行：抖音百应 · 指定达人专属佣金（平台核验成功），但回复不确定发出去没有（发的时候机器人停了），"
     "请看一眼微信再决定要不要手动发给 小美"),
    (dict(status="failed", outcome="platform_not_run", error="Mac 一直没接这个任务，平台没有动", platform={}),
     "#7 执行没成功：没有执行（NOT_RUN），平台没有动，回复没有发；请到控制台看"),
    (dict(status="failed", outcome="platform_failed", error="平台动作没有核验成功（FAILED…）",
          platform={"execution": {"outcome": "FAILED", "problems": ["绑定被拒"],
                                  "steps": [{"name": "create", "execute": True, "outcome": "VERIFIED_SUCCESS"},
                                            {"name": "bind", "execute": True, "outcome": "FAILED"}]}}),
     f"#7 {UNCERTAIN}（平台拒绝了，或者没有发出（FAILED）：绑定被拒）；回复没有发"),
])
def test_what_came_of_an_execution_is_told_in_the_consoles_words(desk, row, told):
    desk.clock.now += UNSURE_GRACE_SEC                                   # (an unsure one: told only after the grace)
    row = {"packet_id": 7, "party_id": 1, "decision": "execute", "tool": "dy.designated_commission",
           **{**row, "platform": json.dumps(row["platform"])}}
    assert desk.review._outcome_text(row) == told


def test_the_consoles_checks_hold_for_执行(desk):
    preview = jst_block()
    preview["action"]["receiver"]["phone"] = "〈手机9〉"                    # (a placeholder with no number)
    packet = platform_waiting(desk, preview)
    number = packet["id"]
    reply = desk.admin("执行", quote=head(desk, number))
    assert reply.startswith(f"#{number} 没执行：") and "〈手机9〉" in reply
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []


@pytest.mark.parametrize("answer", ["1", "好", "发", "ok", "[强]", "同意"])
def test_a_yes_never_runs_a_platform_action(desk, answer):
    packet = platform_waiting(desk)
    number = packet["id"]
    hint = f"#{number} 带平台动作：引用那条通知回「执行」（或者回「执行 {number}」）才在平台上执行；不要的话回 2"
    assert desk.admin("1") == hint                                     # (the bare 1 to its notification)
    assert desk.admin(answer, quote=head(desk, number)) == hint
    assert desk.admin(f"发 {number}") == hint
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []


def test_the_reply_of_a_platform_draft_is_edited_in_the_console_only(desk):
    packet = platform_waiting(desk)
    number = packet["id"]
    refused = f"#{number} 带平台动作：回复要改的话请在控制台「改了再执行」；不要的话回 2"
    hint = f"#{number} 带平台动作：引用那条通知回「执行」（或者回「执行 {number}」）才在平台上执行；不要的话回 2"
    assert desk.admin("改 寄样单马上建～", quote=head(desk, number)) == refused
    assert desk.admin(f"改 {number} 寄样单马上建～") == refused
    for words in ("这样回她可以吗", "就这样吧", "执行吧", "确认执行", "好的呀"):
        assert desk.admin(words, quote=head(desk, number)) == hint, words
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["proposed"]) == ("notified", "")
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []
    assert desk.admin("2", quote=head(desk, number)) == f"#{number} 不发了（她再发消息时会有新的草稿）"
    assert desk.prm.store.packet(number)["status"] == "dropped"


PREVIEWING_STOP = "正在重新预览，微信里先不能执行：好了再发一条通知，看那一条再回；不要的话回 2"
STALE = "这份草稿在通知以后有变化，看最新那条通知再回"


def test_执行_runs_only_the_preview_its_notification_showed(desk):
    """「执行」 runs the preview the quoted notice showed, never another.  While a 重新预览 runs it says so; once the new
    preview is in, the notice told before is out of date (2026-11-10: it used to send the admin to the console) and
    the draft is told again on the same number with the new preview (「第 2 次通知」): 「执行」 quoting that one runs it."""
    packet = platform_waiting(desk)
    number = packet["id"]
    first = head(desk, number)
    assert desk.prm.decide(number, "repreview")["ok"]                   # the console: 重新预览
    assert desk.admin("执行", quote=first) == f"#{number} {PREVIEWING_STOP}"
    assert desk.review.sweep() == 0                                     # (nothing told while it runs)
    job = desk.relay.next_job(0, ("prm",))                              # the Mac previews again
    assert desk.relay.finish(job["id"], {"ok": True, "text": json.dumps(jst_block(previewed_at=3.0)), "duration_s": 3})
    desk.relay.tick()
    assert desk.prm.store.packet(number)["platform"]["preview"]["previewed_at"] == 3.0
    for answer, quote in ((f"执行 {number}", None), ("执行", first)):
        assert desk.admin(answer, quote=quote) == f"#{number} {STALE}（新的通知马上发过来）"
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []
    desk.clock.now += 5
    assert desk.review.sweep() == 1
    lines = latest_notice(desk, number)
    assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
    assert lines[1] == f"上一条 #{number} 的通知以后有变化：重新预览过了（下面是新的预览）。上一条作废了，请看这一条再回"
    assert lines[-2].startswith("引用这条回「执行」")
    assert desk.review.sweep() == 0                                     # (told once)
    assert desk.admin("执行", quote=first) == f"#{number} {STALE}"       # (the notice of the old preview: never)
    assert desk.executions() == []
    assert desk.admin("执行", quote=lines[0]).startswith(f"#{number} 已批准执行")
    assert len(desk.executions()) == 1


def test_执行_after_an_identical_re_preview_runs_from_its_fresh_notice(desk):
    """Probe i7g: a 重新预览 that finished leaves 「previewing: null」 in the platform data, and 执行 still runs.  Since
    2026-11-10 every new preview that ended is told again, the same preview too (the admin was told 「好了再发一条
    通知」): the fresh notice is the one 「执行」 quotes."""
    packet = platform_waiting(desk)
    number = packet["id"]
    assert desk.prm.decide(number, "repreview")["ok"]
    job = desk.relay.next_job(0, ("prm",))
    assert job["meta"]["op"] == "platform_preview"
    desk.relay.finish(job["id"], {"ok": True, "text": json.dumps(jst_block(), ensure_ascii=False), "duration_s": 3})
    desk.relay.tick()
    data = desk.prm.store.packet(number)["platform"]
    assert "previewing" in data and data["previewing"] is None and data["previews"] == 1
    assert desk.review.sweep() == 1
    fresh = latest_notice(desk, number)
    assert fresh[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
    assert desk.prm.store.wechat_review(number)["preview"] == wr.preview_stamp(data["preview"])
    assert desk.admin("执行", quote=fresh[0]).startswith(f"#{number} 已批准执行")
    assert desk.prm.store.packet(number)["status"] == "executing" and len(desk.executions()) == 1


def test_the_console_approves_after_a_re_preview_that_finished(desk):
    """Probe i7f: the console's 批准并执行 after a 重新预览 that finished (a new preview) runs."""
    packet = platform_waiting(desk)
    number = packet["id"]
    assert desk.prm.decide(number, "repreview")["ok"]
    job = desk.relay.next_job(0, ("prm",))
    desk.relay.finish(job["id"], {"ok": True, "text": json.dumps(jst_block(previewed_at=5.0)), "duration_s": 3})
    desk.relay.tick()
    assert desk.prm.decide(number, "execute")["ok"] and len(desk.executions()) == 1


def test_执行_while_a_re_preview_runs_says_so(desk):
    """A 重新预览 started after the notification, the preview itself still the one told: 「正在重新预览」, not 「预览变过」
    (third verification, the console's words too); its result is told in a fresh notice (2026-11-10)."""
    from src.prm.service import PREVIEWING

    packet = platform_waiting(desk)
    number = packet["id"]
    data = desk.prm.store.packet(number)["platform"]
    desk.prm.store.set_platform(number, {**data, "previewing": desk.clock.now}, ("pending",))
    assert desk.admin(f"执行 {number}") == f"#{number} {PREVIEWING_STOP}"
    assert desk.admin(f"执行 {number} 了吗") == f"#{number}（小美）还没执行：正在重新预览，好了再发一条通知"
    assert desk.prm.decide(number, "execute") == {"ok": False, "error": PREVIEWING, "code": "previewing"}
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []


def test_执行_of_a_draft_without_a_platform_action_sends_nothing(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("执行", quote=head(desk, number)) == f"#{number} 没有平台动作：回「发 {number}」就行"
    assert desk.admin(f"执行 {number}") == f"#{number} 没有平台动作：回「发 {number}」就行"
    assert desk.prm.store.packet(number)["status"] == "pending"


def test_a_group_platform_draft_runs_only_by_a_quote(desk):
    packet = platform_waiting(desk, group=True)
    number = packet["id"]
    lines = notice_of(desk, number)
    assert lines[0] == f"#{number} 草稿 · 小美 · 群里「小美合作群」"
    assert lines[-1] == f"（发在群里：一定要引用这条回「执行」；不发也可以回「不发 {number}」）"
    assert desk.admin(f"执行 {number}") == f"#{number} 是发在群「小美合作群」里的，请引用那条通知回「执行」确认"
    assert desk.executions() == []
    assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 已批准执行")
    assert len(desk.executions()) == 1


# 「执行」 with words around it (third verification item 2: the probe's words), never a command, never a question; and the
# questions about 执行 (fourth verification item 3, the probe's words): never run, they get a status
EXECUTE_SAID = ["执行吧", "执行一下", "可以执行", "去执行", "执行啊", "那就执行吧", "执行[强]", "执行…"]
EXECUTE_ASKED = ["执行了吗", "执行了没", "执行吗", "执行？", "执行了?", "执行了", "执行了没有", "执行没", "执行?", "执行吗？",
                 "执行好了吗", "要执行吗", "可以执行了吗", "执行了吗？", "执行了么", "执行？？", "执行吗?!", "执行吧？", "执行了吧",
                 "执行了吧？", "那执行了吗", "执行呢", "执行呢？", "执行...？"]


@pytest.mark.parametrize("word", ["执行", "执行！", "执行。", " 执行 ", *EXECUTE_SAID])
def test_a_lone_执行_is_never_run_and_never_passed_on_while_a_platform_draft_waits(desk, word):
    """Second review B1 (P1): a lone 「执行」 got silence.  It gets the hint now; nothing runs.  Third verification item 2:
    「执行」 with words around it (「执行吧」「执行一下」「可以执行」…) got silence too: the same hint (a question about 执行
    gets a status: ``test_a_question_about_执行_never_runs_and_says_what_became_of_the_draft``)."""
    packet = platform_waiting(desk)
    reply = desk.admin(word)
    assert reply == f"{WHICH_EXECUTE}（在等的：#{packet['id']}）"
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.executions() == []


def test_a_lone_执行_with_no_platform_draft_told_goes_on_as_before(clock, tmp_path):
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    waiting(desk)                                                       # (a draft with no platform action)
    assert "已交给 Mac" in desk.admin("执行")                            # relay only: Claude's task, as before
    assert [job["text"] for job in desk.jobs()] == ["执行"]
    desk.stop()


@pytest.mark.parametrize("word", ["执行", "执行！", *EXECUTE_SAID])
@pytest.mark.parametrize("ended", [False, True])
def test_relay_only_a_lone_执行_is_never_claudes_go_ahead_while_a_platform_draft_waits(clock, tmp_path, ended, word):
    """Second review B1 (P2, P6): in CLAUDE_RELAY_ONLY a lone 「执行」 meant for the platform draft just told became
    Claude's follow-up (its job running) or a new task (its job ended): a go-ahead for whatever Claude had proposed.
    Third verification item 2: 「执行吧」「执行一下」「可以执行」「去执行」「执行啊」 still did.  Final check item 4: the hint
    says how it reaches Claude instead (CC_HINT)."""
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("帮我把旧分支的清理方案列出来，等我说执行再删")
    job = desk.relay.next_job(0, ("admin",))
    if ended:
        desk.relay.finish(job["id"], {"ok": True, "text": "方案如下……回「执行」我就删", "duration_s": 3})
        desk.relay.tick()
    packet = platform_waiting(desk)
    clock.now += 30
    assert desk.admin(word) == f"{WHICH_EXECUTE}（在等的：#{packet['id']}）{CC_HINT}"
    assert desk.relay.store.followups(job["id"], 0) == [] and len(desk.jobs()) == 1
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.executions() == []
    desk.stop()


@pytest.mark.parametrize("word", ["执行吧", "执行一下", "可以执行"])
def test_a_执行_with_words_around_it_gets_the_hint_with_a_cc_task_running_too(desk, word):
    """Third verification item 2 (normal mode): with a cc task running, 「执行吧」 got silence; the hint now, and the task
    hears nothing of it."""
    desk.admin("cc 帮我把旧分支的清理方案列出来，等我说执行再删")
    job = desk.relay.next_job(0, ("admin",))
    packet = platform_waiting(desk)
    desk.clock.now += 30
    assert desk.admin(word) == f"{WHICH_EXECUTE}（在等的：#{packet['id']}）"
    assert desk.relay.store.followups(job["id"], 0) == []
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.executions() == []


def test_relay_only_words_that_only_mention_执行_stay_claudes(clock, tmp_path):
    """What is no 「执行」 alone stays Claude's in CLAUDE_RELAY_ONLY: a 「不执行」, a sentence about something to execute.
    Fourth verification item 5 (probe r5b): a 「执行吧」 quoting Claude's own message got silence (the relay takes no
    quote in CLAUDE_RELAY_ONLY): one line says so now, with the words to send (final check item 4: 「cc 执行吧」, not
    「不引用直接发」, which the review would catch); nothing runs; sent as it says, Claude has it."""
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("帮我把旧分支的清理方案列出来，等我说执行再删")
    job = desk.relay.next_job(0, ("admin",))
    packet = platform_waiting(desk)
    clock.now += 30
    for words in ("不执行", "执行这个方案吧"):
        assert "已转给正在做的" in desk.admin(words), words
    assert [f["text"] for f in desk.relay.store.followups(job["id"], 0)] == ["不执行", "执行这个方案吧"]
    desk.relay.finish(job["id"], {"ok": True, "text": "方案如下……回「执行」我就删", "duration_s": 3, "session_id": "s1"})
    desk.relay.tick()
    claude_said = desk.told()[-1]
    jobs = len(desk.jobs())
    for words in ("执行吧", "执行", "执行了吗"):
        assert desk.admin(words, quote=claude_said) == QUOTED_ELSEWHERE.format(words=words), words
    assert len(desk.jobs()) == jobs
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.executions() == []
    assert "已交给 Mac" in desk.admin("cc 执行吧") and desk.jobs()[0]["text"] == "执行吧"   # (as the line says)
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.executions() == []
    desk.stop()


def test_relay_only_a_numbered_执行_quoting_claude_never_runs(clock, tmp_path):
    """Final check item 2: in CLAUDE_RELAY_ONLY, 「执行 1」 quoting Claude's message ran draft #1 (a platform write), while
    the admin most likely meant Claude's 「方案 1」.  Never run: one line says how to run the draft (QUOTED_NUMBERED, and
    how to give it to Claude); a number that is no draft told here is Claude's words (QUOTED_ELSEWHERE).  Quoting the
    notification it runs, as always."""
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("帮我列两个清理方案")
    job = desk.relay.next_job(0, ("admin",))
    desk.relay.finish(job["id"], {"ok": True, "text": "方案 1：删旧分支；方案 2：归档", "duration_s": 3})
    desk.relay.tick()
    claude_said = desk.told()[-1]
    packet = platform_waiting(desk)
    number = packet["id"]
    clock.now += 30
    assert desk.admin(f"执行 {number}", quote=claude_said) == (
        QUOTED_NUMBERED.format(number=number, direct=f"执行 {number}", quoted="执行") + CC_HINT)
    assert desk.admin("执行 9", quote=claude_said) == QUOTED_ELSEWHERE.format(words="执行 9")
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []
    assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 已批准执行")
    assert len(desk.executions()) == 1
    desk.stop()


# numbered answers under a quote of another message (final check of 7eaa0dc, probe a)
QUOTED_FORMS = [("执行 {P}", "执行 {P}", "执行"), ("执行{P}", "执行 {P}", "执行"), ("执行 #{P}", "执行 {P}", "执行"),
                ("执行 {P}！", "执行 {P}", "执行"), ("执行 ＃{P}", "执行 {P}", "执行"), ("执行 {P}。", "执行 {P}", "执行"),
                ("执行 {P} 吧", "执行 {P}", "执行"), ("执行{P}吗", "执行 {P} 了吗", "执行了吗"),
                ("发 {M}", "发 {M}", "发"), ("发送 {M}", "发送 {M}", "发送"), ("发 {M}！", "发 {M}", "发"),
                ("发#{M}", "发 {M}", "发"), ("不发 {M}", "不发 {M}", "不发"), ("不发 {P}", "不发 {P}", "不发"),
                ("改 {M} 好的老师明天发", "改 {M} 你的话", "改 你的话"), ("改{M} 好的", "改 {M} 你的话", "改 你的话"),
                ("改 {M}", "改 {M} 你的话", "改 你的话"),
                ("执行 {P}\n谢谢", "执行 {P}", "执行"), ("好的\n执行 {P}", "执行 {P}", "执行"),
                ("执行 {P}\n发 {M}", "执行 {P}", "执行"), ("发 {M}\n谢谢", "发 {M}", "发"),
                ("改 {M}\n好的老师", "改 {M} 你的话", "改 你的话")]


@pytest.mark.parametrize("relay_only", ["on", "off"])
@pytest.mark.parametrize("form, direct, quoted", QUOTED_FORMS)
def test_a_numbered_answer_quoting_another_message_never_acts(clock, tmp_path, form, direct, quoted, relay_only):
    """Final check of 7eaa0dc, items 1 + 2 (probe a): quoting a message that is not one of our notifications (Claude's
    「方案 1 … 方案 2 …」), a numbered answer acted: 「执行 1\\n谢谢」 ran #1, 「好的\\n执行 1」 ran it and gave 「好的」 to the
    relay, 「发 2」「不发 2」「改 2 …」 sent, dropped, sent the admin's words to her, in either mode.  Any numbered line of
    such a message: nothing of it is done nor passed on, one line says how to answer the draft."""
    desk = Desk(clock, tmp_path, relay_only=relay_only)
    desk.prm.register(CREATOR, "小美", "")
    desk.prm.register(OTHER, "小雨", "")
    desk.admin("帮我把旧分支清理方案列出来" if relay_only == "on" else "cc 帮我把旧分支清理方案列出来")
    job = desk.relay.next_job(0, ("admin",))
    p = platform_waiting(desk)["id"]
    m = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")["id"]
    clock.now += 30
    claude_said = "#1 进行中：方案 1 先删旧分支，方案 2 先备份。回「执行 1」或「执行 2」"
    reply = desk.admin(form.format(P=p, M=m), quote=claude_said)
    number = int(direct.format(P=p, M=m).split()[1])
    expected = QUOTED_NUMBERED.format(number=number, direct=direct.format(P=p, M=m), quoted=quoted)
    assert reply == expected + (CC_HINT if relay_only == "on" else "")
    assert [desk.prm.store.packet(n)["status"] for n in (p, m)] == ["pending", "pending"] and desk.executions() == []
    assert desk.relay.store.followups(job["id"], 0) == [] and len(desk.jobs()) == 1
    assert desk.prm.store.wechat_review(m)["proposed"] == ""
    desk.stop()


def test_a_quote_of_another_message_without_a_draft_number_is_as_before(clock, tmp_path):
    """What stays: unnumbered answers to Claude under its quote (its approval words, a cc line), and numbers that are no
    draft told here (Claude's 「方案 9」), in CLAUDE_RELAY_ONLY."""
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("帮我把旧分支清理方案列出来")
    job = desk.relay.next_job(0, ("admin",))
    ask = desk.relay.ask(job["id"], {"tool": "Bash", "detail": "git branch -D old", "timeout_s": 600})
    desk.relay.tick()                                                   # (the approval request goes to the admin)
    claude_said = desk.told()[-1]
    packet = platform_waiting(desk)
    clock.now += 30
    assert desk.admin("同意", quote=claude_said).startswith("好，已批准")
    assert desk.relay.store.get_ask(ask["ask_id"])["status"] == "allow"
    assert "已转给正在做的" in desk.admin("cc 用方案 9", quote=claude_said)
    assert [f["text"] for f in desk.relay.store.followups(job["id"], 0)] == ["用方案 9"]
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.executions() == []
    desk.stop()


@pytest.mark.parametrize("how", ["quoted", "unquoted", "numbered"])
@pytest.mark.parametrize("words", EXECUTE_ASKED)
def test_a_question_about_执行_never_runs_and_says_what_became_of_the_draft(desk, words, how):
    """Fourth verification item 3 (probe q): 「执行了吗」「执行吗」「执行？」「执行了」… unquoted got 「执行哪一份？」 (a push to
    run it), 「执行了没」「执行了么」「要执行吗」「执行好了吗」 nothing at all.  A question about 执行, quoted, unquoted or with
    the draft's number, never runs anything: it says what became of the draft (her 称呼 after its number)."""
    packet = platform_waiting(desk)
    number = packet["id"]
    if how == "quoted":
        reply = desk.admin(words, quote=head(desk, number))
    else:
        reply = desk.admin(words.replace("执行", f"执行 {number}", 1) if how == "numbered" else words)
    assert reply == f"#{number}（小美）还没执行（在等你确认）：要执行请引用那条通知回「执行」（或者回「执行 {number}」）"
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []


def test_a_question_about_执行_after_it_ran_says_how_far_it_got(desk):
    """Fourth verification item 3: after the 「执行」, 「执行了吗？」 got no answer; it says how far it got now (running, done
    and the result as told when it ended), and a red end in the console's words."""
    packet = platform_waiting(desk)
    number = packet["id"]
    desk.admin("执行", quote=head(desk, number))
    assert desk.admin("执行了吗？") == f"#{number}（小美）已经批准，正在平台上执行（做完了告诉你结果）"
    assert desk.admin("执行好了没有", quote=head(desk, number)) == (
        f"#{number}（小美）已经批准，正在平台上执行（做完了告诉你结果）")
    mac_runs(desk, record(sample_order_no=JST_ORDER))
    assert desk.admin("执行了吗") == (f"#{number}（小美）已经执行完了：聚水潭 · 达人寄样单（平台核验成功；寄样单号 {JST_ORDER}），"
                                     "回复正在发")
    desk.prm.tick()
    assert desk.admin(f"执行 {number} 了没") == (
        f"#{number}（小美）已执行：聚水潭 · 达人寄样单（平台核验成功；寄样单号 {JST_ORDER}），回复已发出")
    assert len(desk.executions()) == 1


def test_a_question_about_执行_lists_a_few_drafts_and_is_not_the_reviews_without_one(clock, tmp_path):
    """Unquoted, a question about 执行 answers for the platform drafts told here that wait or were told recently,
    STATUS_LISTED at most: final check item 5, the ones running first, then the decided or ended ones (the latest first:
    the one that ran is never hidden behind 「还有 N 份」), then the waiting ones, each with her 称呼; the console's red
    end in its words; in CLAUDE_RELAY_ONLY with how to give it to Claude (CC_HINT); without one it is not the review's
    (in CLAUDE_RELAY_ONLY: Claude's)."""
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    desk.admin("帮我把旧分支的清理方案列出来")
    job = desk.relay.next_job(0, ("admin",))
    waiting(desk)                                                       # (a draft without a platform action)
    assert "已转给正在做的" in desk.admin("执行了吗")                      # (no platform draft told: Claude's)
    assert [f["text"] for f in desk.relay.store.followups(job["id"], 0)] == ["执行了吗"]
    numbers = []
    for n, chat in enumerate(("wxid_creator11", "wxid_creator12", "wxid_creator13", "wxid_creator14")):
        desk.prm.register(chat, f"测试{n}", "")
        desk.her("好的，寄吧", chat=chat)
        drafted = desk.mac(text="寄样单建好啦～", message_kind="money", platform=jst_block())
        desk.prm.store.token_for(drafted["party_id"], "phone", "手机", "13900001111", desk.clock.now)   # 〈手机1〉
        numbers.append(drafted["id"])
        desk.review.sweep()
    desk.prm.decide(numbers[0], "execute")                              # (the oldest runs: the console's)
    mac_runs(desk, record("FAILED"))
    lines = desk.admin("执行了没？").split("\n")
    assert lines == [
        f"#{numbers[0]}（测试0）执行没成功：平台拒绝了，或者没有发出（FAILED）：读回没证实，平台没有动，回复没有发；请到控制台看",
        *[f"#{n}（测试{k}）还没执行（在等你确认）：要执行请引用那条通知回「执行」（或者回「执行 {n}」）"
          for k, n in ((3, numbers[3]), (2, numbers[2]))],
        f"（还有 1 份，见控制台）{CC_HINT}"]
    assert len(desk.executions()) == 1
    desk.stop()


def test_a_result_that_came_long_after_its_approval_is_still_listed(clock, tmp_path):
    """Final check item 5: the window took ``decided_at`` first (COALESCE), so a draft approved more than STATUS_SEC ago
    whose result came just now was left out; the later of ``decided_at`` and ``sent_at`` counts now."""
    desk = Desk(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "")
    packet = platform_waiting(desk)
    number = packet["id"]
    desk.prm.decide(number, "execute")                                  # (the console's)
    clock.now += wr.STATUS_SEC + 3600
    mac_runs(desk, record("FAILED"))                                    # (the Mac's answer, just now)
    assert desk.admin("执行了吗") == (
        f"#{number}（小美）执行没成功：平台拒绝了，或者没有发出（FAILED）：读回没证实，平台没有动，回复没有发；请到控制台看")
    desk.stop()


@pytest.mark.parametrize("words", ["执行…", "执行...", "执行。。。", "执行……", "执行.", "执 行 …", "执行。。", "执行．"])
def test_an_ellipsis_is_never_a_command(desk, words):
    """Fourth verification item 1 (probe e1): 「执行…」「执行...」「执行。。。」 quoting a notification ran it (NFKC made 「…」
    「...」 and the dots were taken off as an ending).  An ending that hesitates is no command: nothing runs."""
    packet = platform_waiting(desk)
    number = packet["id"]
    assert desk.admin(words, quote=head(desk, number)) == (
        f"#{number} 带平台动作：引用那条通知回「执行」（或者回「执行 {number}」）才在平台上执行；不要的话回 2")
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []


@pytest.mark.parametrize("words", ["执行～～", "执行!!!", "执行！~", "执行 。", "执行~"])
def test_a_command_takes_one_full_stop_or_exclamation_marks_and_tildes(desk, words):
    """The endings a command takes: a single 「。」, or one or more of 「！!～~」."""
    packet = platform_waiting(desk)
    assert desk.admin(words, quote=head(desk, packet["id"])).startswith(f"#{packet['id']} 已批准执行")
    assert len(desk.executions()) == 1


def test_a_1_with_an_ellipsis_never_sends(desk):
    """Fourth verification item 1: 「1…」 sent a draft the same way, quoting its notification or bare.  Final check item
    3: bare, it got silence; one line says to answer 1 alone now (BARE_UNSURE)."""
    packet = waiting(desk)
    number = packet["id"]
    for words in ("1…", "1...", "1。。", "发…"):
        assert desk.admin(words, quote=head(desk, number)) == f"#{number} {HELP_YES}", words
    assert desk.admin("1…") == BARE_UNSURE and desk.admin("2...") == BARE_UNSURE
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin(f"发 {number}…") is None and desk.admin(f"不发 {number}...") is None
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin("1。", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"


@pytest.mark.parametrize("words", ["1.", "1…", "1...", "1。。", "1．", "1｡", "2.", "２…", "１。。", "2．"])
def test_a_bare_1_with_a_hesitating_ending_gets_a_line_and_does_nothing(desk, words):
    """Final check item 3 (36 forms): 「1.」「1…」「1。。」「1．」「1｡」… bare got silence (2ad168e sent or refused them): one
    line now, nothing done."""
    packet = waiting(desk)
    assert desk.admin(words) == BARE_UNSURE
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"


def test_relay_only_a_bare_1_with_a_hesitating_ending_stays_claudes(clock, tmp_path):
    """In CLAUDE_RELAY_ONLY a bare 1 / 2 is Claude's (``_bare_claims``): with a hesitating ending too, nothing of it
    here."""
    desk = Desk(clock, tmp_path, relay_only="on")
    desk.prm.register(CREATOR, "小美", "")
    packet = waiting(desk)
    assert "已交给 Mac" in desk.admin("1…") and [job["text"] for job in desk.jobs()] == ["1…"]
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    desk.stop()


@pytest.mark.parametrize("words, status", [("1~。", "approved"), ("1。！", "approved"), ("1！。", "approved"),
                                           ("2~。", "dropped"), ("２！。", "dropped")])
def test_one_full_stop_with_exclamation_marks_or_tildes_is_an_ending(desk, words, status):
    """Final check item 3: at most one 「。」 with any 「！!～~」, in either order, is an ending, not hesitating."""
    packet = waiting(desk)
    desk.admin(words)
    assert desk.prm.store.packet(packet["id"])["status"] == status


@pytest.mark.parametrize("words", ["👍…", "👍.", "[强]…", "[强]。。", "…", "。", "？", "！", "~", "……", "👍👍…"])
def test_emoji_or_punctuation_only_quoting_a_notification_is_never_an_edit(desk, words):
    """Final check item 1: a quoted 「👍…」「[强]。。」 (and, older, 「…」「？」「！」) became the proposed edit, and the bare 1
    after it sent 「👍…」 to her.  A text without a letter is never words for her: the bot says how to answer."""
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin(words, quote=head(desk, number)) == f"#{number} {HELP_YES}"
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["proposed"]) == ("notified", "")
    desk.admin("1")
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.to(CREATOR) == []
    assert desk.admin("👍~。", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"   # (an ending: 👍 is a yes)
    assert desk.prm.store.packet(number)["final_text"] == "收到啦～戴着还合适吗？"


def test_a_numbered_answer_takes_the_same_endings(clock, tmp_path):
    """Fourth verification item 6: 「执行 12。」「执行 12！」「发 12。」 got nothing; the endings of item 1 are taken after the
    number, nothing else.  Final check item 6: 「执行 12。。」「执行 12…」「执行 12 吧」 (no command) got no answer: what
    became of #12, never run."""
    desk = Desk(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "")
    packet = platform_waiting(desk)
    number = packet["id"]
    waiting_line = f"#{number}（小美）还没执行（在等你确认）：要执行请引用那条通知回「执行」（或者回「执行 {number}」）"
    for words in (f"执行 {number}。。", f"执行 {number}…", f"执行 {number} 吧"):
        assert desk.admin(words) == waiting_line, words
    assert desk.executions() == []
    assert desk.admin(f"执行 {number}！").startswith(f"#{number} 已批准执行")
    desk.stop()
    for words, told in (("发 {n}。", "已批准，正在发给 小美"), ("不发 {n}～", "不发了（她再发消息时会有新的草稿）"),
                        ("发送 {n}!!", "已批准，正在发给 小美"), ("发 {n}~。", "已批准，正在发给 小美")):
        desk = Desk(clock, tmp_path)
        desk.prm.register(CREATOR, "小美", "")
        number = waiting(desk)["id"]
        assert desk.admin(words.format(n=number)) == f"#{number} {told}", words
        desk.stop()


@pytest.mark.parametrize("verb", ["执行", "发", "不发"])
def test_a_number_quoting_another_drafts_notification_does_nothing_and_asks_which(desk, verb):
    """Second review B3 (P3, Q2): quoting #M's notification, 「执行 N」 / 「发 N」 / 「不发 N」 acted on N."""
    desk.prm.register(OTHER, "小雨", "")
    first = platform_waiting(desk)
    desk.her("好的", chat=OTHER)
    second = desk.mac(text="专属佣金 20% 设好啦～", message_kind="money", platform=block(DESIGNATED))
    desk.review.sweep()
    reply = desk.admin(f"{verb} {second['id']}", quote=head(desk, first["id"]))
    assert reply == (f"你引用的是 #{first['id']}，写的却是「{verb} {second['id']}」：什么都没做。要处理哪一份？"
                     f"引用那一份的通知回，或者不引用回「{verb} 编号」")
    assert [desk.prm.store.packet(p["id"])["status"] for p in (first, second)] == ["pending", "pending"]
    assert desk.executions() == []
    assert desk.admin(f"{verb} {first['id']}", quote=head(desk, first["id"])).startswith(f"#{first['id']}")


@pytest.mark.parametrize("outcome", ["INDETERMINATE", "ACCEPTED_UNVERIFIED", "UNKNOWN", "PARTIAL", "OBSERVED_DESIRED_STATE"])
def test_an_uncertain_execution_is_never_told_as_not_done(desk, outcome):
    """Second review B2 (P4): the outcomes that may have written on the platform were told 「执行没成功」 (a redo
    invitation).  As the console classes them (``written_state``): 「结果不确定…别重做」."""
    packet = platform_waiting(desk)
    number = packet["id"]
    desk.admin("执行", quote=head(desk, number))
    mac_runs(desk, record(outcome))
    desk.prm.tick()
    told = desk.told()[-1]
    assert told.startswith(f"#{number} {UNCERTAIN}（") and told.endswith("；回复没有发")
    assert f"（{outcome}）" in told and "执行没成功" not in told and desk.to(CREATOR) == []


def test_a_verified_result_that_comes_after_the_bot_gave_up_is_told_in_a_second_line(desk):
    """Second review B2 (P5, Q1): the bot gave up on a silent Mac (UNKNOWN: told as uncertain), then the Mac's
    VERIFIED came: the admin heard nothing of it."""
    from src.prm.service import PLATFORM_LOST_GRACE_SEC
    from src.relay.service import LOST_AFTER_SEC

    packet = platform_waiting(desk)
    number = packet["id"]
    desk.admin("执行", quote=head(desk, number))
    job = desk.relay.next_job(0, ("prm",))
    desk.clock.now += LOST_AFTER_SEC + 1
    desk.relay.tick()                                                   # lost: the platform write may be running
    desk.clock.now += PLATFORM_LOST_GRACE_SEC + 1
    desk.prm.tick()                                                     # given up on: UNKNOWN
    assert desk.told()[-1].startswith(f"#{number} {UNCERTAIN}")
    assert desk.relay.finish(job["id"], {"ok": True, "text": json.dumps({**record(sample_order_no=JST_ORDER),
                                                                         "key": job["meta"]["key"]})})
    desk.relay.tick()
    desk.prm.tick()
    assert desk.told()[-1] == (f"#{number} 平台的结果晚到了：聚水潭 · 达人寄样单（平台核验成功；寄样单号 {JST_ORDER}）；"
                               "回复没有自动发，需要的话请在微信里手动回 小美")
    assert desk.to(CREATOR) == []
    desk.prm.tick()
    assert len([t for t in desk.told() if "晚到了" in t]) == 1


def test_a_preview_changed_between_the_check_and_the_move_never_runs_unseen(desk):
    """Second review B4 (P8): the stamp was checked before ``decide``; a 重新预览 landing in between ran the new preview.
    ``decide`` checks the stamp again in the step that moves the draft."""
    packet = platform_waiting(desk)
    number = packet["id"]
    real = desk.review._decide

    def racing(n, action, text=None, **kw):
        data = desk.prm.store.packet(n)["platform"]                     # the console's 重新预览 lands right now
        new_preview = {**data["preview"], "previewed_at": 99.0, "steps": [{**data["preview"]["steps"][0], "facts": [
            {"label": "商品", "value": "别的款（测试抖店）：OTHER-01 黑色 × 9（售价 1，平台库存 30）"}]}]}
        desk.prm.store.set_platform(n, {**data, "preview": new_preview}, ("pending",))
        return real(n, action, text, **kw)

    desk.review._decide = racing
    assert desk.admin("执行", quote=head(desk, number)) == f"#{number} {STALE}（新的通知马上发过来）"
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []
    desk.review._decide = real
    assert desk.review.sweep() == 1                                     # (the new preview, told again: 2026-11-10)
    fresh = latest_notice(desk, number)
    assert fresh[0].startswith(f"#{number} 草稿（第 2 次通知）") and any("OTHER-01 黑色 × 9" in line for line in fresh)
    assert desk.admin("执行", quote=fresh[0]).startswith(f"#{number} 已批准执行")


def test_the_move_to_executing_checks_the_platform_data_it_read(desk):
    """``decide`` "execute" (the console's too): a 重新预览 landing between its checks and its move is refused, the
    draft waits; a stamp that is not the preview's is refused before anything.  Third verification (probe i7d): one
    that only started in between says so (「正在重新预览」), not 「预览刚刚变过」."""
    from src.prm.service import PREVIEW_CHANGED, PREVIEWING

    packet = platform_waiting(desk)
    number = packet["id"]
    assert desk.prm.decide(number, "execute", stamp="0" * 16) == {"ok": False, "error": PREVIEW_CHANGED,
                                                                 "code": "preview_changed"}
    real = desk.prm.store.move
    landing = {}

    def move(packet_id, from_states, to_state, **fields):
        if to_state == "executing":                                      # (right before the move)
            data = desk.prm.store.packet(packet_id)["platform"]
            desk.prm.store.set_platform(packet_id, {**data, **landing}, ("pending",))
        return real(packet_id, from_states, to_state, **fields)

    with patch.object(desk.prm.store, "move", move):
        landing.update(previewing=desk.clock.now)                        # a re-preview starts
        assert desk.prm.decide(number, "execute") == {"ok": False, "error": PREVIEWING, "code": "previewing"}
        data = desk.prm.store.packet(number)["platform"]
        desk.prm.store.set_platform(number, {**data, "previewing": None}, ("pending",))
        landing.clear()
        landing.update(preview={**data["preview"], "previewed_at": 99.0})    # one lands
        assert desk.prm.decide(number, "execute") == {"ok": False, "error": PREVIEW_CHANGED, "code": "preview_changed"}
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []


def test_a_long_platform_action_lists_every_line_or_cannot_run_from_wechat(desk):
    """Second review B5 (P6): a channel-goods link with many SKUs was cut at 8 lines without a word, and its 价格检查 /
    渠道类型 / 有效期 never shown.  Every line now (a 10-SKU link in full); past LINES_MAX: 「还有 N 项」 and no 执行.
    Third verification (probe i8): 45 lines made a notification of 2,027 characters; at most NOTICE_CHARS (the relay's
    send limit) now, the lines that do not fit counted in 「还有 N 项」."""
    def preview(skus):
        facts = [{"label": "渠道类型", "value": "达人专属（只给这一位达人）"},
                 *[{"label": f"SKU {DY_SKU}{i} 颜色{i}", "value": f"日常价 315.00 元 → 渠道价 283.00 元；库存 {i} 件（主商品可分 500 件）"}
                   for i in range(skus)],
                 {"label": "有效期", "value": "30 天"}, {"label": "价格检查", "value": "pass 价格没有低于底线"},
                 {"label": "商品名前缀", "value": "达人专享｜（6 个字）"}]
        step = {"name": "create", "label": "抖店 · 建渠道品", "execute": False, "command": "node bin/dy.mjs", "exit_code": 0,
                "seconds": 2.0, "verdict": "ready", "outcome": "", "problems": [], "raw": "{}", "facts": facts}
        return block({**CHANNEL, "stock_per_sku": 100, "exclusive": True}, steps=[step])

    her_uid_known(desk)                                                 # (its 要注意 shown whole: her_uid_known)
    packet = platform_waiting(desk, preview(10), text="专属链接给你啦：{链接}")
    number = packet["id"]
    lines = notice_of(desk, number)
    assert len([line for line in lines if line.startswith("· SKU ")]) == 10
    for fact in ("· 渠道类型：达人专属（只给这一位达人）", "· 有效期：30 天", "· 价格检查：pass 价格没有低于底线",
                 "· 商品名前缀：达人专享｜（6 个字）", f"· SKU {DY_SKU}9 颜色9：日常价 315.00 元 → 渠道价 283.00 元；库存 9 件（主商品可分 500 件）"):
        assert fact in lines, fact
    assert lines[-2].startswith("引用这条回「执行」")
    every = len([line for line in lines if line.startswith("· ")])       # (with 45 SKUs: the step's 40 facts, not 14)
    assert desk.prm.decide(number, "drop")["ok"]
    packet = platform_waiting(desk, preview(45), text="专属链接给你啦：{链接}")
    number = packet["id"]
    lines = notice_of(desk, number)
    assert len("\n".join(lines)) <= NOTICE_CHARS
    [more] = [line for line in lines if line.startswith("· 还有 ")]
    assert more.endswith(" 项，见控制台")
    listed = len([line for line in lines if line.startswith("· ") and line != more])
    assert listed + int(more.split()[2]) == every - 14 + 40   # (a step's facts: 40 at most, platforms._facts)
    assert lines[-1] == "通知里没列全，微信里不能执行：请到控制台看了再「批准并执行」；不要的话回 2"
    assert desk.admin("执行", quote=head(desk, number)) == (
        f"#{number} 的平台动作通知里没列全，微信里不能执行：请到控制台看了再「批准并执行」；不要的话回 2")
    assert desk.admin(f"执行 {number}") == (
        f"#{number} 的平台动作通知里没列全，微信里不能执行：请到控制台看了再「批准并执行」；不要的话回 2")
    assert desk.executions() == []


def test_a_notification_too_long_in_characters_cannot_run_from_wechat_either(desk):
    """Under LINES_MAX lines, but longer than NOTICE_CHARS: the lines that fit, 「还有 N 项」, and 「执行」 is the console's."""
    facts = [{"label": f"SKU {DY_SKU}{i:02d} 颜色{i}", "value": "日常价 315.00 元 → 渠道价 283.00 元；" + "库存充足，" * 8}
             for i in range(30)]
    step = {"name": "create", "label": "抖店 · 建渠道品", "execute": False, "command": "node bin/dy.mjs", "exit_code": 0,
            "seconds": 2.0, "verdict": "ready", "outcome": "", "problems": [], "raw": "{}", "facts": facts}
    her_uid_known(desk)                                                 # (its 要注意 shown whole: her_uid_known)
    packet = platform_waiting(desk, block({**CHANNEL, "stock_per_sku": 100, "exclusive": True}, steps=[step]),
                              text="专属链接给你啦：{链接}")
    number = packet["id"]
    text = "\n".join(notice_of(desk, number))
    assert len(text) <= NOTICE_CHARS and "引用这条回「执行」" not in text
    shown = len([line for line in text.split("\n") if line.startswith("· SKU ")])
    assert 0 < shown < 30 and f"· 还有 {30 - shown} 项，见控制台" in text
    assert desk.prm.store.wechat_review(number)["preview"] == ""
    assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 的平台动作通知里没列全")
    assert desk.executions() == []


def test_the_creators_platform_ids_show_only_their_last_characters(desk):
    """Second review B6 (P7): the notification showed her UID / 抖音号 in full; the invite card's words too."""
    invite = {"action": "dy.creator_invite", "item_id": DY_ITEM, "creator_douyin_id": "xm_test01",
              "invite_text": "您好，想邀请您带一下我们的新款帽子～有问题加微信 shop_test01 聊"}
    step = {"name": "invite", "label": "百应 · 发邀约", "execute": False, "command": "node bin/dy.mjs", "exit_code": 0,
            "seconds": 2.0, "verdict": "ready", "outcome": "", "problems": [], "raw": "{}",
            "facts": [{"label": "达人昵称（请核对是不是聊天里这位）", "value": "小美"},
                      {"label": "达人 xm_test01", "value": "可以邀约"}]}
    packet = platform_waiting(desk, block(invite, run={"steps": ["invite"], "status": 1, "popwindow": 0}, steps=[step]),
                              text="邀约卡发给您啦～")
    text = "\n".join(notice_of(desk, packet["id"]))
    assert "· 达人抖音号：…st01" in text and "· 达人 …st01：可以邀约" in text
    assert f"· 邀约信息：（{len(invite['invite_text'])} 字，见控制台）" in text
    for private in ("xm_test01", "shop_test01", "想邀请您"):
        assert private not in text, private


@pytest.mark.parametrize("name, shown", [(f"小美 {DY_UID}", f"小美 …{DY_UID[-4:]}"), ("小美13900001111", "小美139****1111"),
                                         ("小美 微信 abc123456", "小美 微信 ab****56")])
def test_her_name_is_masked_in_the_notification_head_and_the_status_line(clock, tmp_path, name, shown):
    """Final check of 7eaa0dc, item 3 (probe b): her 称呼 showed a platform id (her UID) in full in the status line, and
    the notification head did not mask it at all: both as everything else now (``_name_of``)."""
    desk = Desk(clock, tmp_path)
    desk.prm.register(CREATOR, name, "")
    packet = platform_waiting(desk)
    number = packet["id"]
    assert head(desk, number) == f"#{number} 草稿 · {shown} · 私聊"
    reply = desk.admin("执行了吗")
    assert reply.startswith(f"#{number}（{shown}）还没执行") and name not in reply
    desk.stop()


def test_the_ids_in_a_previews_problems_and_its_plan_show_only_their_last_characters(desk):
    """Third verification item 9 (probes i9a, i9b, i9c): a problem line named her UID in full, the 小红书 买手 line her
    userId (``preview.run.buyer_id``), a 买手 not found problem her 小红书号: by their last characters now."""
    problem = f"平台说达人 {DY_UID} 不能加进这个策略：达人已在其他策略中"
    preview = block(DESIGNATED, verdict="blocked", problems=[problem],
                    facts=[{"label": f"达人 {DY_UID}", "value": "不能加：达人已在其他策略中"}])
    packet = platform_waiting(desk, preview, text="专属佣金 20% 设好啦～")
    text = "\n".join(notice_of(desk, packet["id"]))
    assert DY_UID not in text and f"· 问题：平台说达人 …{DY_UID[-4:]} 不能加进这个策略：达人已在其他策略中" in text
    assert desk.prm.decide(packet["id"], "drop")["ok"]
    raw = {"action": "xhs.direct_plan", "item_id": XHS_ITEM, "creator_platform_id": "xhs_test_88",
           "commission_rate": 20}
    preview = block(raw, run={"steps": ["create"], "buyer_id": XHS_BUYER, "name": "达人合作1-1027100000"},
                    facts=[{"label": "买手", "value": f"测试（粉丝 100，笔记 5），userId {XHS_BUYER}：请核对是不是这位达人"}])
    packet = platform_waiting(desk, preview, text="专属佣金设好啦～")
    text = "\n".join(notice_of(desk, packet["id"]))
    assert XHS_BUYER not in text and f"userId …{XHS_BUYER[-4:]}：请核对是不是这位达人" in text
    assert "· 达人平台账号：…t_88" in text and "xhs_test_88" not in text
    assert desk.prm.decide(packet["id"], "drop")["ok"]
    preview = block(raw, verdict="blocked", problems=["千帆没找到「xhs_test_88」这个买手；请让达人给小红书 userId"])
    packet = platform_waiting(desk, preview, text="专属佣金设好啦～")
    text = "\n".join(notice_of(desk, packet["id"]))
    assert "xhs_test_88" not in text and "· 问题：千帆没找到「…t_88」这个买手；请让达人给小红书 userId" in text


def test_the_line_that_tells_an_executions_end_shows_her_ids_by_their_last_characters(desk):
    """Third verification item 9 (probe i9d): the line that told a PARTIAL end quoted its first problem raw, her UID in
    full; and a red draft's error quoted when it is answered again."""
    her_uid_known(desk)                                                 # (its 要注意 shown whole: her_uid_known)
    # (the shop's way, 100 a colour: 合并 fix1110-s, a 专属链接 off it asks for attention, convention_text)
    packet = platform_waiting(desk, block({**CHANNEL, "stock_per_sku": platforms.CHANNEL_STOCK_PER_SKU, "exclusive": True}),
                              text="专属链接给你啦：{链接}")
    number = packet["id"]
    desk.admin("执行", quote=head(desk, number))
    rec = platforms.record("PARTIAL", [f"渠道品 7200000000000001 建好了，但绑定达人的预览没通过，没有绑定：达人 {DY_UID} 绑定被拒：测试"])
    rec.update(key="", created={"channel_id": "7200000000000001"})
    mac_runs(desk, rec)
    desk.prm.tick()
    told = desk.told()[-1]
    assert told.startswith(f"#{number} {UNCERTAIN}") and DY_UID not in told and f"达人 …{DY_UID[-4:]} 绑定被拒" in told
    assert desk.prm.store.move(number, ("failed",), "failed", error=f"达人 {DY_UID} 绑定被拒：请到平台后台核对")
    reply = desk.admin("执行", quote=head(desk, number))
    assert reply.startswith(f"#{number} 已经处理过了") and DY_UID not in reply and f"…{DY_UID[-4:]}" in reply


def test_a_job_that_cannot_be_queued_leaves_the_draft_waiting_and_says_执行_again(desk):
    """Second review B7 (P9): the relay's queue failing after the move to executing said 「已经处理过了」 (the draft
    had gone red).  When the job is certainly not there (third verification: looked up by its approval key), the draft
    waits again, as it was, and the admin can answer 「执行」 once more."""
    packet = platform_waiting(desk)
    number = packet["id"]
    with patch.object(desk.relay, "submit", side_effect=RuntimeError("队列坏了")):
        assert desk.admin("执行", quote=head(desk, number)) == f"#{number} {QUEUE_RETRY}"
    waiting_again = desk.prm.store.packet(number)
    assert waiting_again["status"] == "pending" and waiting_again["final_text"] is None
    assert "key" not in waiting_again["platform"] and desk.prm.store.wechat_review(number)["state"] == "notified"
    desk.prm.tick()
    assert not desk.told()[-1].startswith(f"#{number} 执行没成功")
    assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 已批准执行")
    assert len(desk.executions()) == 1


def test_a_job_queued_before_the_relay_raised_runs_once_and_is_never_offered_again(desk):
    """Third verification item 1 (probe i10a): the relay raised after the job was written; the draft went back to
    waiting without its key, the Mac's answer was then ignored, and a second 「执行」 queued a second write.  The job is
    looked up by its approval key: there, it runs as approved (the draft executing, its key and job kept)."""
    packet = platform_waiting(desk)
    number = packet["id"]
    real = desk.relay.submit

    def queued_then_raises(*args, **kwargs):
        real(*args, **kwargs)
        raise sqlite3.OperationalError("database is locked")

    with patch.object(desk.relay, "submit", side_effect=queued_then_raises):
        assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 已批准执行")
    [job] = desk.executions()
    running = desk.prm.store.packet(number)
    assert running["status"] == "executing" and running["platform"]["job_id"] == job["id"]
    assert running["platform"]["key"] == json.loads(job["meta"])["key"]
    assert desk.admin("执行", quote=head(desk, number)) == (
        f"#{number} 已经处理过了：你在微信里回过了，已经批准，正在平台上执行")
    mac_runs(desk, record(sample_order_no=JST_ORDER))
    desk.prm.tick()
    assert desk.to(CREATOR) == [f"寄样单建好啦，单号 {JST_ORDER}～"] and len(desk.executions()) == 1
    assert desk.told()[-1].startswith(f"#{number} 已执行：")


class CommitFailsOnce:
    """A relay store's connection whose next commit fails, as SQLite's busy timeout does (probes i10b / i10c)."""

    def __init__(self, conn):
        self._conn, self.armed = conn, True

    def commit(self):
        if self.armed:
            self.armed = False
            raise sqlite3.OperationalError("database is locked")
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_job_whose_commit_failed_never_reaches_the_mac_and_执行_again_runs_once(desk):
    """Third verification item 1 (probes i10b, i10c): the relay store's commit failed after its INSERT and nothing
    rolled it back: the Mac's next request got the job (or the next write committed it) while the draft waited again.
    The store rolls a failed write back now: nothing for the Mac, 「执行」 again runs it once."""
    packet = platform_waiting(desk)
    number = packet["id"]
    store = desk.relay.store
    real = store._conn
    store._conn = CommitFailsOnce(real)
    try:
        assert desk.admin("执行", quote=head(desk, number)) == f"#{number} {QUEUE_RETRY}"
    finally:
        store._conn = real
    assert not real.in_transaction
    assert desk.relay.next_job(0, ("prm",)) is None                     # (the Mac's next request: nothing)
    store.add(ADMIN, ADMIN, "随便一个任务", now=desk.clock.now)           # (any later write commits: still nothing)
    assert desk.executions() == []
    waiting_again = desk.prm.store.packet(number)
    assert waiting_again["status"] == "pending" and "key" not in waiting_again["platform"]
    assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 已批准执行")
    assert len(desk.executions()) == 1


def test_a_job_not_known_to_be_queued_is_red_never_offered_again_and_its_late_result_is_told(desk):
    """Third verification item 1: when the job cannot be looked up either, nobody knows whether it was queued: the draft
    goes red as a result not known (its key kept), the admin is never told to answer again, and a result that comes
    after all is marked late and told in a second line."""
    packet = platform_waiting(desk)
    number = packet["id"]
    real = desk.relay.submit

    def queued_then_raises(*args, **kwargs):
        real(*args, **kwargs)
        raise sqlite3.OperationalError("disk I/O error")

    with patch.object(desk.relay, "submit", side_effect=queued_then_raises), \
            patch.object(desk.relay.store, "job_with_key", side_effect=sqlite3.OperationalError("disk I/O error")):
        reply = desk.admin("执行", quote=head(desk, number))
    assert reply == f"#{number} {QUEUE_UNSURE}" and "再回一次" not in reply
    red = desk.prm.store.packet(number)
    assert (red["status"], red["outcome"]) == ("failed", "platform_unknown") and platform_state(red) == "uncertain"
    assert red["platform"]["key"] and red["platform"]["execution"]["outcome"] == "UNKNOWN"
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["decision"]) == ("answered", "execute") and row["told_at"] is not None
    told = len(desk.told())
    desk.prm.tick()
    assert len(desk.told()) == told                                     # (the reply said it: not told twice)
    assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 已经处理过了")
    mac_runs(desk, record(sample_order_no=JST_ORDER))                  # it was queued after all
    desk.prm.tick()
    late = desk.prm.store.packet(number)
    assert late["status"] == "failed" and late["platform"]["execution"]["late"] is True
    assert desk.told()[-1] == (f"#{number} 平台的结果晚到了：聚水潭 · 达人寄样单（平台核验成功；寄样单号 {JST_ORDER}）；"
                               "回复没有自动发，需要的话请在微信里手动回 小美")
    assert desk.to(CREATOR) == []


def test_执行_never_comes_from_her_chat_a_group_or_anybody_else(desk):
    packet = platform_waiting(desk)
    number = packet["id"]
    notice = head(desk, number)
    for where in (dict(group=True, chat="room1@chatroom"), dict(sender="wxid_helper", chat="wxid_helper"),
                  dict(sender="wxid_helper", chat=ADMIN)):
        assert desk.admin(f"执行 {number}", **where) is None
        assert desk.admin("执行", quote=notice, **where) is None
    assert desk.executions() == [] and desk.prm.store.packet(number)["status"] == "pending"
    for content, quote in ((f"执行 {number}", None), ("执行", notice)):  # her own chat: her words for the next draft
        msg = {"message_id": f"h{next(IDS)}", "chat_id": CREATOR, "group_name": "小美", "sender_id": CREATOR,
               "sender_name": "小美", "content": content, "msg_type": QUOTE if quote else 1,
               "timestamp": desk.clock.now, "is_group": False, "quoted_content": quote or ""}
        assert desk.router.handle(msg) is None and desk.router._tracer.notes["ended_by"] == "prm"
    assert desk.executions() == [] and desk.prm.store.packet(number)["status"] == "superseded"
    assert desk.to(CREATOR) == []


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
                           (f"不发 {second['id']}", None), (f"执行 {second['id']}", None)):
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


def test_a_database_from_the_first_round_gets_the_new_columns(tmp_path):
    """Second review (info): a prm.db made by the first commits has ``wechat_reviews`` without ``told_at`` /
    ``preview`` / ``late_told_at`` (and ``pictures_ok``, ``notice``, ``seen``, 2026-11-10, ``pictures_sent``, the final
    check): they are added (as every column added to an old database), the sweep works."""
    import sqlite3 as sql

    path = tmp_path / "prm.db"
    PrmStore(str(path)).close()
    conn = sql.connect(str(path))
    conn.execute("DROP TABLE wechat_reviews")
    conn.execute("CREATE TABLE wechat_reviews (packet_id INTEGER PRIMARY KEY, deal_id INTEGER NOT NULL, state TEXT NOT"
                 " NULL DEFAULT 'notified', notified_at REAL NOT NULL, proposed TEXT NOT NULL DEFAULT '', proposed_at"
                 " REAL, decision TEXT NOT NULL DEFAULT '', answered_at REAL, told_failed_at REAL, updated_at REAL NOT"
                 " NULL)")
    conn.execute("INSERT INTO wechat_reviews (packet_id, deal_id, notified_at, updated_at) VALUES (5, 1, 1, 1)")
    conn.commit()
    conn.close()
    store = PrmStore(str(path))
    columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(wechat_reviews)")}
    assert {"told_at", "preview", "late_told_at", "pictures_ok", "notice", "seen", "pictures_sent"} <= columns
    assert store.wechat_review(5)["preview"] == "" and store.wechat_reviews_to_tell() == []
    assert store.wechat_review(5)["pictures_ok"] == 1          # (2026-11-10: a row from before had its pictures sent)
    assert (store.wechat_review(5)["notice"], store.wechat_review(5)["seen"]) == (1, "")   # (its first notice, not known)
    assert store.wechat_review(5)["pictures_sent"] == ""      # (not known: a fresh notice sends its pictures once more)
    store.close()


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


def test_the_stopped_bot_checks_admin_wxid_as_the_running_one_does(monkeypatch, tmp_path):
    """合并 fix1110-w (2026-11-10): the stopped bot's console says why nothing goes to WeChat as the running bot's does
    (``wechat_review.admin_problem``): ADMIN_WXID a group's id, or a registered creator's wxid in the stopped bot's
    database (an inactive one too), not only an empty one."""
    from src.web import server

    db = tmp_path / "prm.db"
    store = PrmStore(str(db))
    party = store.add_party(CREATOR, "小美", now=1)
    store.update_party(party["id"], active=False, now=2)            # (an inactive creator's wxid counts too)
    store.close()
    server.register_prm(None)
    monkeypatch.setenv("PRM_DB", str(db))
    monkeypatch.setenv("PRM_WECHAT_REVIEW", "on")
    for admin, why in (("12345678@chatroom", wr.GROUP_ADMIN_WHY), (CREATOR, wr.CREATOR_ADMIN_WHY),
                       (ADMIN, "机器人没有运行：草稿发不到管理员微信里"), ("", NO_ADMIN_WHY)):
        monkeypatch.setenv("ADMIN_WXID", admin)
        assert server._prm_state()["wechat_review"]["why"] == why, admin
    monkeypatch.setenv("ADMIN_WXID", CREATOR)
    assert server._wechat_review_stopped_status()["why"] == "机器人没有运行：草稿发不到管理员微信里"   # (no database: not looked up)
    assert wr.admin_problem(" 12345678@chatroom ", None) == wr.GROUP_ADMIN_WHY


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
    backend.set_sent_hook(lambda chat, text: heard.append((chat, text, backend._window_lock.locked())))
    assert backend.send_text(ADMIN, "#1 草稿 · 小美 · 私聊") is False      # (WeChat did not take it: not heard)
    assert backend._send_part("管理员", ADMIN, "#1 改成：『好的』") is True  # (a route reply's bubble)
    assert heard == [(ADMIN, "#1 改成：『好的』", True)]                   # (under the window lock: in send order)
    backend.set_sent_hook(lambda chat, text: 1 / 0)                      # a hook that breaks never breaks the send
    assert backend.send_text(ADMIN, "再来一条") is True and "The sent hook failed" in caplog.text


def test_the_wcdb_backend_tells_its_read_back_hook_its_own_text_lines_with_wechats_time(caplog):
    from src.config import BotConfig
    from src.wechat.wcdb_backend import WcdbBackend

    backend = WcdbBackend(bot_display_name=BOT, groups=["*"], config=BotConfig())
    heard = []
    line = {"chat_id": ADMIN, "content": "#1 草稿 · 小美 · 私聊", "msg_type": 1, "is_group": False, "timestamp": 1234.0}
    backend._tell_read_back(line)                                       # (no hook: nothing)
    backend.set_read_back_hook(lambda chat, text, written: heard.append((chat, text, written)))
    backend._tell_read_back(line)
    backend._tell_read_back({**line, "is_group": True})                 # (a group's: never)
    backend._tell_read_back({**line, "msg_type": 3, "content": "[图片]"})  # (only text lines)
    assert heard == [(ADMIN, "#1 草稿 · 小美 · 私聊", 1234.0)]
    backend.set_read_back_hook(lambda chat, text, written: 1 / 0)
    backend._tell_read_back(line)
    assert "The read-back hook failed" in caplog.text


def test_the_bot_lets_the_review_hear_the_backends_sends_only_with_the_switch_on(tmp_path):
    from src.bot import Bot
    from src.web.server import register_prm

    for review in ("on", "off"):
        heard = []
        backend = SimpleNamespace(send_text=lambda chat, text: True, set_sent_hook=heard.append,
                                  set_read_back_hook=heard.append)
        relay = ClaudeRelay(config(), store=RelayStore(":memory:"), files_dir=tmp_path / review,
                            send=lambda chat, text: True)
        prm = Bot._start_prm(config(prm_wechat_review=review), SimpleNamespace(_store=None), backend, relay)
        try:
            expected = [prm.wechat_review.note_sent, prm.wechat_review.note_read_back] if review == "on" else []
            assert heard == expected, review
        finally:
            register_prm(None)
            prm.stop()
            relay.stop()


def test_a_bare_answer_is_compared_with_the_notification_on_one_clock(clock, tmp_path):
    """Second review N4: the margin compared this PC's time with WeChat's.  WeChat's time of the notification (its
    read-back) against the answer's; without it, this PC's clock as measured against WeChat's (and a margin); with
    nothing measured, a minute."""
    desk = Desk(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "")
    desk.her("收到6件哈")
    packet = desk.mac()
    desk.review.note_read_back = lambda chat, text, written: None       # (this backend gives no read-back)
    desk.review.sweep()
    told_at = clock.now
    review = desk.review
    last = review._last
    assert last.wechat_at is None
    assert not review._seen_after(last, told_at + 30)                    # (nothing measured: a minute)
    assert review._seen_after(last, told_at + 61)
    review._skew = lambda: 3600.0                                        # (this PC an hour ahead of WeChat)
    assert not review._seen_after(last, told_at - 3600 + 1)              # (WeChat's time: 1 s after, the margin not)
    assert review._seen_after(last, told_at - 3600 + 3)
    read_back = replace(last, wechat_at=told_at - 3600 + 2)              # (WeChat showed it at this time)
    assert not review._seen_after(read_back, told_at - 3600 + 2) and review._seen_after(read_back, told_at - 3600 + 3)
    n = packet["id"]
    review._skew = lambda: None
    assert desk.admin("1", at=told_at + 30) == f"#{n} 是刚发来的，你回的时候还没看到它：请引用它回，或者回「发 {n}」「不发 {n}」"
    assert desk.prm.store.packet(n)["status"] == "pending"
    desk.stop()


class NoReadBack(Desk):
    """A backend that gives no read-back (another backend, a slow poll): only the sent hook (probe i12)."""

    def _said(self, chat, text):
        self.review.note_sent(chat, text)


@pytest.mark.parametrize("after, skew, taken", [(30, None, False), (61, None, True), (1, 0.4, False), (3, 0.4, True),
                                                (3, -100.0, False), (3, 100.0, True)])
def test_without_a_read_back_a_bare_1_counts_on_the_measured_clock_only(clock, tmp_path, after, skew, taken):
    """Probe i12: with no read-back, a bare 1 written ``after`` seconds after the notification went out counts only on
    this PC's clock as measured against WeChat's (``skew``: plus BARE_MARGIN_SEC), or, nothing measured, a minute."""
    desk = NoReadBack(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "")
    packet = waiting(desk)
    number = packet["id"]
    if skew is not None:
        desk.prm._clock_samples.append(skew)
    assert desk.review._last.wechat_at is None
    reply = desk.admin("1", at=desk.review._last.at + after)
    if taken:
        assert reply == f"#{number} 已批准，正在发给 小美"
    else:
        assert reply == f"#{number} 是刚发来的，你回的时候还没看到它：请引用它回，或者回「发 {number}」「不发 {number}」"
        assert desk.prm.store.packet(number)["status"] == "pending"
    desk.stop()


def test_the_read_back_of_the_last_thing_said_gives_it_wechats_time(desk):
    packet = waiting(desk)
    last = desk.review._last
    assert last.number == packet["id"] and last.wechat_at == desk.clock.now
    desk.review.note_read_back(ADMIN, "别的话", desk.clock.now + 50)        # (not the last thing said: nothing)
    desk.review.note_read_back("wxid_other", last.text, desk.clock.now + 50)
    assert desk.review._last.wechat_at == desk.clock.now


@pytest.mark.parametrize("same_thread", [True, False], ids=["re-entered", "another-thread"])
def test_a_send_noted_while_a_read_back_is_noted_is_never_undone(desk, same_thread):
    """Third verification item 11 (probe i11): ``note_read_back`` read ``_last``, and a send noted before it wrote it
    back was undone (the notification's read-back put the notification back as the last thing said).  Under the lock
    ``note_sent`` takes, and only onto the very thing it read."""
    packet = waiting(desk)
    number = packet["id"]
    review = desk.review
    notice = next(t for t in desk.told() if t.startswith(f"#{number} 草稿"))
    review._last = review._last_review = wr._Last(desk.clock.now, number, "草稿", None, notice)   # (not read back yet)
    real = wr.replace
    sender = []

    def racing(said, **fields):
        if not sender:                                                  # (the sender thread notes a send right now)
            if same_thread:
                sender.append(None)
                review.note_sent(ADMIN, "收到，已交给 Mac（#9）。")
            else:
                thread = threading.Thread(target=review.note_sent, args=(ADMIN, "收到，已交给 Mac（#9）。"))
                sender.append(thread)
                thread.start()
                thread.join(0.2)                                        # (it waits for the lock meanwhile)
        return real(said, **fields)

    with patch.object(wr, "replace", racing):
        review.note_read_back(ADMIN, notice, desk.clock.now + 1)
    if sender[0] is not None:
        sender[0].join(5)
    assert review._last.text == "收到，已交给 Mac（#9）。" and review._last.number is None
    assert review._last_review.number == number and review._last_review.wechat_at == desk.clock.now + 1


def test_a_line_read_back_before_its_send_was_noted_still_gives_it_wechats_time(desk):
    """Third verification item 11: a line the poll read back before the backend noted its send was lost (nothing said
    matched it yet): remembered now, READ_BACK_EARLY_SEC at most, taken once."""
    review = desk.review
    review.note_read_back(ADMIN, "#99 草稿 · 测试达人 · 私聊", desk.clock.now - 1)
    review.note_sent(ADMIN, "#99 草稿 · 测试达人 · 私聊")
    assert review._last.wechat_at == desk.clock.now - 1 and review._last_review.wechat_at == desk.clock.now - 1
    review.note_sent(ADMIN, "#99 草稿 · 测试达人 · 私聊")                 # (sent again: that read-back is taken)
    assert review._last.wechat_at is None
    review.note_read_back(ADMIN, "收到", desk.clock.now)
    desk.clock.now += READ_BACK_EARLY_SEC + 1
    review.note_sent(ADMIN, "收到")                                       # (too long ago to be this one)
    assert review._last.wechat_at is None


# ── 合并 draft-context (2026-11-10): a sample order the photo check holds is the console's ───────────────────────────

CODE = "A7269901001B-F"             # a made-up 聚水潭 item code
GATED = {"v": 3, "gated": True}     # the Mac's mark next to a sample order it checked by photo (PRM_DRAFT_CONTEXT)


def photo_desk(clock, tmp_path) -> Desk:
    d = Desk(clock, tmp_path, prm_draft_context="on")
    d.prm.register(CREATOR, "小美", "测试达人")
    return d


def checked_waiting(desk, *, held=(CODE,), used=(), verdict="blocked", mark=True) -> dict:
    """She agrees; the Mac drafts a sample order and checks it by photo (``mark``: the answer says so), holding ``held``
    for photos (its preview not ready) and letting ``used`` go only by an operator's 「对过图了」; the draft is told."""
    desk.prm.store.token_for(1, "phone", "手机", "13900001111", desk.clock.now)     # 〈手机1〉: the receiver's number
    desk.her("好的，寄吧")
    problems = [f"{c}（均码咖啡）对图没把握（款有把握，颜色没把握）：先核对再建；{draft_context.CONFIRM_HOW}" for c in held]
    preview = {**jst_block(), "verdict": verdict, "problems": problems,
               "run": {"steps": ["create"]} if verdict == "ready" else {}}
    packet = desk.mac(text="寄样单建好啦，单号 {寄样单号}～", message_kind="money", platform=preview,
                      **({"draft_context": GATED} if mark else {}), **({"photo_held": list(held)} if held else {}),
                      **({"photo_confirmed": list(used)} if used else {}))
    assert packet["status"] == "pending" and packet["tool"] == "jst.sample_order"
    desk.review.sweep()
    return desk.prm.store.packet(packet["id"])


def test_a_sample_order_with_hats_held_for_photos_says_so_and_执行_stays_the_consoles(clock, tmp_path):
    """The notification of a sample order the Mac held hats of (PRM_DRAFT_CONTEXT: no sure picture behind them) with no
    sheet that shows them says how many and which, and that 「对过图了」 and 「重新预览」 are the console's (a hat a sheet
    it sent shows is numbered and confirmed by an answer quoting it: the tests of 2026-11-10 below); 「执行」 quoting
    it, 「执行 N」 and 「执行 N 了吗」 say the same and run nothing, and 「对过图 1」 quoting it confirms nothing; confirmed
    for the deal but not previewed again, it says to preview again; confirmed in the console, the bot previews it
    again by itself and says so.  The switch off (the console has no 「对过图了」 then): the usual line."""
    desk = photo_desk(clock, tmp_path)
    try:
        packet = checked_waiting(desk)
        number = packet["id"]
        stop = (f"有 1 顶帽子扣着（没对上图：{CODE}），微信里不能执行：请到控制台对过图，在这顶旁边点「对过图了」，"
                "再点「重新预览」；不要的话回 2")
        lines = notice_of(desk, number)
        assert lines[-1] == stop and not any("回「执行」" in line for line in lines)
        assert any(f"{CODE}（均码咖啡）对图没把握" in line for line in lines)        # (the problem itself is listed too)
        assert desk.prm.store.wechat_review(number)["preview"] in ("", None)        # (no stamp: never run from here)
        quote = head(desk, number)
        assert desk.admin("执行", quote=quote) == f"#{number} {stop}"
        assert desk.admin(f"执行 {number}") == f"#{number} {stop}"
        assert desk.admin(f"执行 {number} 了吗") == f"#{number}（小美）还没执行（在等你确认）：{stop}"
        assert desk.executions() == [] and desk.prm.store.packet(number)["status"] == "pending"
        assert desk.prm.store.wechat_review(number)["hats"] == ""                   # (no sheet shows it: not numbered)
        for answer in ("对过图 1", "对过图", "对过图 全部"):
            assert desk.admin(answer, quote=quote) == f"#{number} {stop}", answer
        deal_id = packet["deal_id"]
        assert desk.prm.store.photo_confirmations(deal_id) == []
        assert desk.prm.store.confirm_photo(deal_id, CODE, 0, clock.now)            # (for the deal, by another draft)
        assert desk.admin("执行", quote=quote) == (f"#{number} 扣着的帽子都点过「对过图了」，微信里不能执行：请到控制台点"
                                                   "「重新预览」按确认再核；不要的话回 2")
        assert desk.prm.store.unconfirm_photo(deal_id, CODE)
        done = desk.prm.deal_action("photo_confirm", {"deal_id": deal_id, "packet": number, "code": CODE})
        assert done["ok"] and done["repreview"] is True                             # (the console: previewed again)
        assert desk.admin("执行", quote=quote) == f"#{number} {PREVIEWING_STOP}"
        assert desk.admin("对过图 1", quote=quote) == f"#{number} 正在重新预览，好了再发一条通知（现在不用回「对过图」）"
        assert desk.executions() == []
        desk.review._draft_context = False
        assert desk.review.notification_text(desk.prm.store.packet(number)).split("\n")[-1] == PREVIEWING_STOP
    finally:
        desk.stop()


def test_a_sample_order_whose_confirmation_was_taken_back_is_not_run_from_wechat(clock, tmp_path):
    """A sample order its preview let a hat go only by a 「对过图了」 is told with 「执行」 (ready, checked); taken back in
    the console (「撤销」), 「执行」 quoting that notification says why and runs nothing, and ``decide`` with the
    notification's stamp refuses it too (the console's ``_approve_execute``); confirmed again, it runs."""
    desk = photo_desk(clock, tmp_path)
    try:
        assert desk.prm.store.confirm_photo(1, CODE, 0, clock.now)                  # (an earlier draft's 「对过图了」)
        packet = checked_waiting(desk, held=(), used=(CODE,), verdict="ready")
        number, deal_id = packet["id"], packet["deal_id"]
        lines = notice_of(desk, number)
        assert lines[-2].startswith("引用这条回「执行」") and lines[-1] == f"（不引用就回「执行 {number}」「不发 {number}」）"
        stamp = desk.prm.store.wechat_review(number)["preview"]
        assert stamp
        assert desk.prm.deal_action("photo_unconfirm", {"deal_id": deal_id, "code": CODE})["ok"]
        why = f"撤销了 {CODE} 的「对过图了」：点「重新预览」再核"
        quote = head(desk, number)
        assert desk.admin("执行", quote=quote) == f"#{number} {why}；微信里不能执行，请到控制台处理；不要的话回 2"
        assert desk.prm.decide(number, "execute", None, stamp=stamp) == {"ok": False, "error": why}
        assert desk.executions() == [] and desk.prm.store.packet(number)["status"] == "pending"
        assert desk.prm.deal_action("photo_confirm", {"deal_id": deal_id, "packet": number, "code": CODE})["ok"]
        assert desk.admin("执行", quote=quote).startswith(f"#{number} 已批准执行")
        assert len(desk.executions()) == 1
    finally:
        desk.stop()


def test_a_held_hat_nobody_confirmed_is_never_offered_执行_the_switch_off_too(clock, tmp_path):
    """合并 fix1110-s (2026-11-10): a sample order whose last preview held a hat nobody confirmed is never approved, the
    switch on or off (``_approve_execute``, review A-L4), even when its preview says ready: its notification offers no
    「执行」 then either, and says why.  The switch off, the console has no 「对过图了」 (``photo_confirm`` refuses it):
    the line says 「重新预览」 alone, whose new preview no photo check holds (final check 2026-11-10 A4)."""
    desk = Desk(clock, tmp_path)                                        # (PRM_DRAFT_CONTEXT off)
    desk.prm.register(CREATOR, "小美", "测试达人")
    try:
        packet = checked_waiting(desk, held=(CODE,), verdict="ready")
        number = packet["id"]
        why = f"{CODE} 还扣着（没对过图），不能执行：点「重新预览」再核"
        assert draft_context.held_reason([CODE], confirm=False) == why and "对过图了" not in why
        stop = f"{why}；微信里不能执行；不要的话回 2"
        assert notice_of(desk, number)[-1] == stop
        assert desk.prm.store.wechat_review(number)["preview"] in ("", None)
        assert desk.admin("执行", quote=head(desk, number)) == f"#{number} {stop}"
        assert desk.executions() == []
        assert shown(desk, number)["wechat_execute"] is False
    finally:
        desk.stop()


def test_a_sample_order_the_mac_did_not_check_by_photo_is_never_offered_执行(clock, tmp_path):
    """An older Mac worker's sample order (no mark: the bot holds it, ``ungated``): its notification says what the
    console says and offers no 「执行」."""
    desk = photo_desk(clock, tmp_path)
    try:
        packet = checked_waiting(desk, held=(), verdict="ready", mark=False)
        number = packet["id"]
        stop = f"{draft_context.NOT_GATED}；微信里不能执行，请到控制台处理；不要的话回 2"
        assert notice_of(desk, number)[-1] == stop
        assert desk.admin("执行", quote=head(desk, number)) == f"#{number} {stop}"
        assert desk.executions() == []
    finally:
        desk.stop()



# ── 2026-11-10: the pictures a draft is to be looked at with go after its notification; until they all came, it is the
# console's in WeChat ───────────────────────────────────────────────────────────────────────────────────────────────

CARD_NAMES = ("手卡_01_测试帽.png", "手卡_02_测试帽二.png")
CARD_ZIP = "ACE7CN直播手卡.zip"


def cards_desk(clock, tmp_path) -> Desk:
    d = Desk(clock, tmp_path, prm_handcards="on")
    d.prm.register(CREATOR, "小美", "测试达人")
    return d


def cards_waiting(desk, *, promo_until="", text="老师，手卡整理好啦～", chat=CREATOR) -> dict:
    """She asks for 直播手卡; the Mac makes two cards (and the skill's zip, and a brief) and hands them back with the
    draft (PRM_HANDCARDS on); it waits and is told (the sender thread's round)."""
    desk.her("老师手卡发一下", chat=chat)
    desk.clock.now += SETTLE_SEC + 1
    job = desk.relay.next_job(0, ("prm",))
    names = [*CARD_NAMES, CARD_ZIP]
    for name, body in ((CARD_NAMES[0], b"\x89PNG card one"), (CARD_NAMES[1], b"\x89PNG card two"),
                       (CARD_ZIP, b"PK the cards"), ("brief.txt", b"brief")):
        assert desk.relay.outfile(job["id"], name, io.BytesIO(body).read, len(body))
    made = hc.made_block(True, cards=names, note="做了 2 张手卡", promo_until=promo_until)
    draft = reply_draft(text, message_kind="material", attachments_to_send=[*names, "brief.txt"],
                        attachments_named=[*names, "brief.txt"], handcards_made=made)
    desk.relay.finish(job["id"], {"ok": True, "text": json.dumps(draft, ensure_ascii=False), "duration_s": 3})
    desk.relay.tick()
    packet = desk.prm.store.packet_for_job(job["id"])
    assert packet["status"] == "pending" and packet["meta"]["handcards"]["ok"] is True
    desk.review.sweep()
    return desk.prm.store.packet(packet["id"])


def card_paths(packet) -> list:
    return [a["path"] for a in packet["attachments"] if a["name"] in CARD_NAMES]


def shown(desk, number) -> dict:
    return next(p for p in desk.prm.snapshot()["pending"] if p["id"] == number)


def test_handcards_go_right_after_their_notification_and_only_then_is_it_answered(clock, tmp_path):
    """A draft with 直播手卡 is told with 「附了 2 张手卡（图在下面）：看过再回 1」 and its two card PNGs go right after the
    notification (never the skill's zip, nor the brief); then it may be answered from WeChat."""
    from src.prm.service import HANDCARDS
    from src.prm.wechat_review import CARDS_META

    assert CARDS_META == HANDCARDS                                      # (the review reads the service's key)
    desk = cards_desk(clock, tmp_path)
    try:
        packet = cards_waiting(desk)
        number = packet["id"]
        assert "附了 2 张手卡（图在下面）：看过再回 1" in notice_of(desk, number)
        notice = next(i for i, event in enumerate(desk.order) if event[0] == "text"
                      and event[2].startswith(f"#{number} 草稿"))
        assert desk.order[notice + 1:] == [("file", ADMIN, path) for path in card_paths(packet)]
        assert desk.prm.store.wechat_review(number)["pictures_ok"] == 1
        assert shown(desk, number)["wechat_console"] is False
        assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"
    finally:
        desk.stop()


def test_handcards_out_of_date_never_go_to_the_admin_either(clock, tmp_path):
    """Cards whose 双11 line is past its end date (Beijing time) never go: not to her (``decide`` refuses them as they
    are), nor to the admin's chat; the notification has no cards' line.  Another creator's cards still on time go."""
    desk = cards_desk(clock, tmp_path)
    desk.prm.register(OTHER, "小雨", "测试达人二")
    try:
        packet = cards_waiting(desk, promo_until="2026-11-11")                # (the clock: 2027-01-15)
        number = packet["id"]
        assert desk.files == []
        assert not any("张手卡（图在下面）" in line for line in notice_of(desk, number))
        assert desk.admin("1", quote=head(desk, number)) == f"#{number} 没发：{hc.EXPIRED_TEXT}"
        on_time = cards_waiting(desk, promo_until="2027-12-31", chat=OTHER)
        assert desk.files == [(ADMIN, path) for path in card_paths(on_time)]
        assert "附了 2 张手卡（图在下面）：看过再回 1" in notice_of(desk, on_time["id"])
    finally:
        desk.stop()


def test_cards_that_did_not_reach_the_admin_make_the_draft_the_consoles_until_they_go_again(clock, tmp_path):
    """The first card WeChat does not take: one line says so (PICTURES_MISSING), and 1 / 发 N / 改 / other words / a
    bare 1 for it do nothing (2 still drops it); the console is unaffected.  Sent again later (the draft's own wait):
    one line says they came, and it may be answered from WeChat again."""
    desk = cards_desk(clock, tmp_path)
    try:
        desk.files_fail = True
        packet = cards_waiting(desk)
        number = packet["id"]
        stop = f"#{number} {PICTURES_MISSING}"
        assert desk.told()[-1] == stop                                    # (right after the card that did not go)
        assert desk.prm.store.wechat_review(number)["pictures_ok"] == 0
        quote = head(desk, number)
        for answer in ({"content": "1", "quote": quote}, {"content": f"发 {number}"}, {"content": "改 你好呀", "quote": quote},
                       {"content": f"改 {number} 你好呀"}, {"content": "这顶我看行，就这么发吧", "quote": quote},
                       {"content": "执行", "quote": quote}):
            assert desk.admin(**answer) == stop, answer
        assert desk.admin("1") == f"{stop}（不要的话回「不发 {number}」）"   # (the bot said that line last)
        assert desk.prm.store.packet(number)["status"] == "pending"
        assert desk.prm.store.wechat_review(number)["state"] == "notified"   # (no proposal kept either)
        assert not any("手卡整理好啦" in text for text in desk.to(CREATOR))
        view = shown(desk, number)
        assert view["wechat_console"] is True and view["status"] == "pending"
        tried = len(desk.files)
        desk.review.sweep()                                               # (too soon: the draft waits its turn)
        assert len(desk.files) == tried
        desk.files_fail = False
        desk.clock.now += RETRY_SEC[1] + 1
        desk.review.sweep()
        assert desk.order[-3:-1] == [("file", ADMIN, path) for path in card_paths(packet)]
        assert desk.told()[-1] == f"#{number} 的图补发过来了（在上面）：看过再引用那条通知回 1（或者回「发 {number}」）"
        assert desk.prm.store.wechat_review(number)["pictures_ok"] == 1 and shown(desk, number)["wechat_console"] is False
        assert desk.admin("1", quote=quote) == f"#{number} 已批准，正在发给 小美"
    finally:
        desk.stop()


def test_a_photo_sheet_that_did_not_reach_the_admin_keeps_执行_the_consoles_until_it_goes_again(desk):
    """The 对图单 of a platform draft the same way: WeChat did not take it, 「执行」 (quoting, numbered) does nothing and
    「执行 N 了吗」 says why; the console can still approve it.  Sent again: 「执行」 quoting the notification runs it."""
    folder = desk.relay.files_dir / "outfiles" / "job-1"
    folder.mkdir(parents=True)
    sheet = folder / "对图单-1.jpg"
    sheet.write_bytes(b"\xff\xd8 not really")
    desk.files_fail = True
    packet = platform_waiting(desk, files=[{"name": "对图单-1.jpg", "path": str(sheet), "why": "核对颜色和款式"}])
    number = packet["id"]
    stop = f"#{number} {PICTURES_MISSING}"
    assert desk.told()[-1] == stop
    quote = head(desk, number)
    assert desk.admin("执行", quote=quote) == stop and desk.admin(f"执行 {number}") == stop
    assert desk.admin(f"执行 {number} 了吗") == f"#{number}（小美）还没执行（在等你确认）：{PICTURES_MISSING}"
    assert desk.executions() == []
    view = shown(desk, number)
    assert view["wechat_console"] is True and view["wechat_execute"] is False and view["platform"]["can_execute"] is True
    desk.files_fail = False
    desk.clock.now += RETRY_SEC[1] + 1
    desk.review.sweep()
    assert desk.order[-2] == ("file", ADMIN, str(sheet))
    assert desk.told()[-1] == (f"#{number} 的图补发过来了（在上面）：看过再引用那条通知回「执行」（或者回「执行 {number}」）")
    assert shown(desk, number)["wechat_execute"] is True
    assert desk.admin("执行", quote=quote).startswith(f"#{number} 已批准执行")
    assert len(desk.executions()) == 1


def test_a_picture_not_on_this_pc_or_too_big_makes_the_notification_say_so_and_none_goes(clock, tmp_path, monkeypatch):
    """Known before the notification goes: a 对图单 not on this PC (or cards too big, PICTURE_MAX_BYTES): it says so
    where it would say how to answer, no picture goes, nothing but 2 / 不发 is done from WeChat, and it is not tried
    again (the console's for good)."""
    desk = Desk(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "测试达人")
    try:
        gone = desk.relay.files_dir / "outfiles" / "job-1" / "对图单-1.jpg"
        packet = platform_waiting(desk, files=[{"name": "对图单-1.jpg", "path": str(gone), "why": "核对颜色和款式"}])
        number = packet["id"]
        lines = notice_of(desk, number)
        assert lines[-1] == f"{PICTURES_MISSING}；不要的话回 2" and "（要看的图 1 张：对图单-1.jpg）" in lines
        assert not any("回「执行」" in line for line in lines) and desk.files == []
        assert desk.admin("执行", quote=head(desk, number)) == f"#{number} {PICTURES_MISSING}"
        desk.clock.now += 600
        desk.review.sweep()
        assert desk.files == [] and desk.told()[-1].startswith(f"#{number} 草稿")
        assert desk.admin("2", quote=head(desk, number)) == f"#{number} 不发了（她再发消息时会有新的草稿）"
    finally:
        desk.stop()
    monkeypatch.setattr(wr, "PICTURE_MAX_BYTES", 10)                      # (the cards: 14 bytes each)
    desk = cards_desk(clock, tmp_path / "big")
    try:
        packet = cards_waiting(desk)
        number = packet["id"]
        lines = notice_of(desk, number)
        assert lines[-1] == f"{PICTURES_MISSING}；不要的话回 2（或者「不发 {number}」）"
        assert f"（要看的图 2 张：{'、'.join(CARD_NAMES)}）" in lines and desk.files == []
        assert desk.admin("1", quote=head(desk, number)) == f"#{number} {PICTURES_MISSING}"
        assert desk.prm.store.packet(number)["status"] == "pending"
    finally:
        desk.stop()


def test_a_draft_with_pictures_is_never_answered_before_they_went(desk):
    """While its pictures are still on the way (``pictures_ok`` PICTURES_SENDING, written before the notification, until
    every one went), an answer written meanwhile does nothing: never a draft approved from WeChat its pictures never
    reached.  It hears 「图还在发，等图到了再回」 (final check 2026-11-10 A6: not 「图没发过来：…在控制台看过再批」, they
    were coming), and once they went one line says they are there and how to answer, as a retry's 「补发过来了」."""
    folder = desk.relay.files_dir / "outfiles" / "job-1"
    folder.mkdir(parents=True)
    sheet = folder / "对图单-1.jpg"
    sheet.write_bytes(b"\xff\xd8 not really")
    answers = []
    real = desk._send_file

    def meanwhile(chat, path):
        number = desk.prm.store.packets(("pending",))[0]["id"]
        assert desk.prm.store.wechat_review(number)["pictures_ok"] == wr.PICTURES_SENDING
        answers.append(desk.admin(f"执行 {number}"))
        answers.append(desk.admin(f"执行 {number} 了吗"))
        return real(chat, path)

    desk.review._send_file = meanwhile
    packet = platform_waiting(desk, files=[{"name": "对图单-1.jpg", "path": str(sheet), "why": "核对颜色和款式"}])
    number = packet["id"]
    assert answers == [f"#{number} {wr.PICTURES_COMING}",
                       f"#{number}（小美）还没执行（在等你确认）：{wr.PICTURES_COMING}"] and desk.executions() == []
    assert desk.prm.store.wechat_review(number)["pictures_ok"] == 1     # (then it went: answered from here now)
    assert desk.order[-2:] == [("file", ADMIN, str(sheet)), (
        "text", ADMIN, f"#{number} 的图发过来了（在上面）：看过再引用那条通知回「执行」（或者回「执行 {number}」）")]
    assert desk.admin("执行", quote=head(desk, number)).startswith(f"#{number} 已批准执行")


# ── the integrated review of 2026-11-10 (merge-1109): a draft that changes after its notice, ids, addresses ──────

def her_recall(desk, chat=CREATOR):
    """She takes a line back (WeChat's own line in her chat, as the WCDB backend hands it on)."""
    desk.prm.handle({"message_id": f"r{next(IDS)}", "chat_id": chat, "sender_id": chat, "sender_name": "她",
                     "content": "[消息类型:10000]", "msg_type": 10000, "is_group": False, "timestamp": desk.clock.now,
                     "special": {"kind": "system", "title": '"小美" 撤回了一条消息'}})


def latest_notice(desk, number) -> list[str]:
    """The lines of the latest notification of draft ``number`` (a fresh one after a change: 「#N 草稿（第 2 次通知）」)."""
    return [t for t in desk.told() if t.startswith(f"#{number} 草稿")][-1].split("\n")


def test_a_recall_after_the_notification_tells_the_admin_again_and_the_old_one_does_nothing(desk):
    """Review A-F3 (probe A4): a draft told here, then she took a line back.  The service holds the drafts not sent
    (``hold_for_review``: the console says 「撤回…发之前请核对」); the admin's chat never heard of it and a 「1」 quoting
    the earlier notification sent the draft as it was.  Now the draft is told again on the same number, saying what
    changed; an answer to the old notification does nothing (before the fresh one went too), one to the fresh one
    acts."""
    packet = waiting(desk, said="我家地址是测试路1号")
    number = packet["id"]
    first = head(desk, number)
    her_recall(desk)
    held = desk.prm.store.packet(number)
    assert held["status"] == "pending" and held["needs_attention"] and "撤回" in held["reason"]
    stale = f"#{number} 这份草稿在通知以后有变化，看最新那条通知再回"
    assert desk.admin("1", quote=first) == f"{stale}（新的通知马上发过来）"     # (before the sweep told it again)
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.review.sweep() == 1
    lines = latest_notice(desk, number)
    assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
    assert lines[1] == f"上一条 #{number} 的通知以后有变化：她撤回了一条消息（见「要注意」）。上一条作废了，请看这一条再回"
    assert any(line.startswith("要注意：达人撤回了一条消息") for line in lines)
    assert lines[-1] == f"（引用这条回；不引用就回「发 {number}」「不发 {number}」「改 {number} 你的话」）"
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["notice"]) == ("notified", 2)
    for answer in ("1", "发", "改 好的老师～", "好的，就这样发"):
        assert desk.admin(answer, quote=first) == stale, answer
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.prm.store.wechat_review(number)["proposed"] == ""
    desk.prm.send_approved()
    assert desk.to(CREATOR) == []
    desk.review.sweep()
    assert len([t for t in desk.told() if t.startswith(f"#{number} 草稿")]) == 2       # (told again once)
    assert desk.admin("1", quote=lines[0]) == f"#{number} 已批准，正在发给 小美"
    desk.prm.send_approved()
    assert desk.to(CREATOR) == ["收到啦～戴着还合适吗？"]


def test_an_answer_to_the_old_notification_without_a_quote_does_nothing_until_the_fresh_one_was_read(desk):
    """A bare 「1」 to the old notification, or an unquoted 「发 N」 written before the fresh one could be read, does
    nothing; written after it, it acts (a bare 「1」 answers the fresh one, the last thing the bot said)."""
    packet = waiting(desk)
    number = packet["id"]
    her_recall(desk)
    stale = f"#{number} 这份草稿在通知以后有变化，看最新那条通知再回"
    assert desk.admin("1") == f"{stale}（新的通知马上发过来）"
    assert desk.admin(f"发 {number}") == f"{stale}（新的通知马上发过来）"
    desk.clock.now += 5
    desk.review.sweep()
    told_at = desk.clock.now
    assert desk.admin(f"发 {number}", at=told_at - 2) == stale            # (written before the fresh one showed)
    assert desk.admin(f"执行 {number}", at=told_at - 2) == stale
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin(f"发 {number}") == f"#{number} 已批准，正在发给 小美"


def test_a_bare_1_after_the_fresh_notice_answers_it(desk):
    packet = waiting(desk)
    number = packet["id"]
    her_recall(desk)
    desk.review.sweep()
    assert desk.admin("1") == f"#{number} 已批准，正在发给 小美"            # (the fresh notice: the last thing said)


def test_an_unquoted_answer_after_the_fresh_notice_counts_on_the_measured_clock(clock, tmp_path):
    """Without its read-back (the backend did not read the fresh notice back, a restart): on this PC's time it went out,
    a minute later with no clock measured."""
    desk = NoReadBack(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "测试达人")
    try:
        packet = waiting(desk)
        number = packet["id"]
        her_recall(desk)
        desk.review.sweep()
        told_at = desk.clock.now
        stale = f"#{number} 这份草稿在通知以后有变化，看最新那条通知再回"
        assert desk.admin(f"发 {number}", at=told_at + 30) == stale       # (nothing measured: a minute)
        assert desk.admin(f"发 {number}", at=told_at + 61) == f"#{number} 已批准，正在发给 小美"
    finally:
        desk.stop()


def test_a_proposed_edit_written_before_the_change_is_never_sent(desk):
    """Other words quoting the old notification were kept as a proposed edit (「#N 改成：『…』」); the draft changed: the
    fresh notice drops the proposal, a 「1」 quoting that question does nothing; one proposed after the fresh notice is
    asked about and sent as before."""
    packet = waiting(desk)
    number = packet["id"]
    question = desk.admin("好的呀，下周一就给您寄过去", quote=head(desk, number))
    assert question.startswith(f"#{number} 改成：『好的呀，下周一就给您寄过去』")
    her_recall(desk)
    desk.review.sweep()
    assert desk.prm.store.wechat_review(number)["proposed"] == ""
    stale = f"#{number} 这份草稿在通知以后有变化，看最新那条通知再回"
    assert desk.admin("1", quote=question.split("\n")[0]) == stale
    fresh = latest_notice(desk, number)[0]
    asked = desk.admin("那就下周二寄吧", quote=fresh)
    assert asked.startswith(f"#{number} 改成：『那就下周二寄吧』")
    assert desk.admin("1", quote=asked.split("\n")[0]) == f"#{number} 按你改的批准了，正在发给 小美"


def test_2_quoting_the_old_notification_still_drops_the_draft(desk):
    """Dropping sends nothing: 2 / 不发 to the old notification are done (as when its pictures did not come)."""
    packet = waiting(desk)
    number = packet["id"]
    her_recall(desk)
    desk.review.sweep()
    assert desk.admin("2", quote=head(desk, number)) == f"#{number} 不发了（她再发消息时会有新的草稿）"
    assert desk.prm.store.packet(number)["status"] == "dropped"


def test_a_fresh_notice_wechat_does_not_take_keeps_the_answers_stale_and_is_tried_again(desk):
    packet = waiting(desk)
    number = packet["id"]
    her_recall(desk)
    desk.refuse.add(ADMIN)
    assert desk.review.sweep() == 0
    row = desk.prm.store.wechat_review(number)
    assert (row["state"], row["notice"]) == ("notified", 1)               # (put back as it was)
    desk.refuse.clear()
    assert desk.admin("1", quote=head(desk, number)).startswith(f"#{number} 这份草稿在通知以后有变化")
    desk.clock.now += RETRY_SEC[0] + 1
    assert desk.review.sweep() == 1
    assert latest_notice(desk, number)[0].startswith(f"#{number} 草稿（第 2 次通知）")
    desk.review.sweep()
    went = [t for kind, chat, t in desk.order if kind == "text" and chat == ADMIN and t.startswith(f"#{number} 草稿")]
    assert len(went) == 2 and went[1].startswith(f"#{number} 草稿（第 2 次通知）")


def test_a_second_change_is_told_as_the_third_notice(desk):
    packet = waiting(desk)
    number = packet["id"]
    her_recall(desk)
    desk.review.sweep()
    second = latest_notice(desk, number)[0]
    desk.clock.now += 60
    her_recall(desk)
    desk.review.sweep()
    assert latest_notice(desk, number)[0] == f"#{number} 草稿（第 3 次通知） · 小美 · 私聊"
    assert desk.admin("1", quote=second) == f"#{number} 这份草稿在通知以后有变化，看最新那条通知再回"
    assert desk.admin("1", quote=latest_notice(desk, number)[0]) == f"#{number} 已批准，正在发给 小美"


def test_what_a_notice_shows_of_attention_is_its_fingerprint(desk):
    """Only what a notification shows counts as a change: whether it asks for attention and its reason, whether lines of
    the group came it did not hear (more of them later: the same news); never a reason it does not show."""
    base = {"needs_attention": False, "reason": "kb/合作政策.md", "meta": {}}
    seen = wr.attention_seen(base)
    assert wr.attention_seen({**base, "reason": "kb/别的.md"}) == seen
    assert wr.attention_seen({**base, "needs_attention": True}) != seen
    flagged = {**base, "needs_attention": True}
    assert wr.attention_seen({**flagged, "reason": "达人撤回了一条消息；kb/合作政策.md"}) != wr.attention_seen(flagged)
    assert wr.attention_seen({**base, "meta": {"unheard": 3}}) != seen
    assert wr.attention_seen({**base, "meta": {"unheard": 5}}) == wr.attention_seen({**base, "meta": {"unheard": 3}})
    from src.prm.service import recall_note                              # (a recall's note: what the notice says)
    assert wr.RECALL_WORDS in recall_note(desk.clock.now)
    recalled = json.loads(wr.attention_seen({**flagged, "reason": f"{recall_note(desk.clock.now)}；kb/合作政策.md"}))
    assert recalled["k"] == 1 and json.loads(wr.attention_seen(flagged))["k"] == 0
    assert wr._notice_no("#12 草稿（第 2 次通知） · 小美 · 私聊") == 2 and wr._notice_no("#12 草稿 · 小美 · 私聊") == 1
    assert read({"msg_type": QUOTE, "content": "1", "quoted_content": "#12 草稿（第 3 次通知） · 小美"}).notice == 3


def test_a_row_from_before_the_fingerprint_is_never_told_again_for_what_it_may_have_shown(desk):
    """A row told by a bot from before ``seen`` was kept: the draft as it is now counts as seen (no fresh notice for
    what the old notice may have said already), a later change is told."""
    packet = waiting(desk)
    number = packet["id"]
    with desk.prm.store._lock:
        desk.prm.store._conn.execute("UPDATE wechat_reviews SET seen = ''")
    her_recall(desk)
    assert desk.review.sweep() == 0 and desk.prm.store.wechat_review(number)["seen"]
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"


def test_a_platform_draft_whose_要注意_is_cut_runs_only_in_the_console(desk):
    """Review A-L1 (probe A5): a 专属链接 whose UID the bot cannot tie to her and whose 专属佣金 it never opened: its
    要注意 (the console shows it whole) was cut to NOTE_CHARS in WeChat, and 「执行」 still ran it.  A cut 要注意 on a
    platform draft lets nothing run from WeChat now (no stamp), and the notice says why."""
    packet = platform_waiting(desk, block({**CHANNEL, "stock_per_sku": 50, "exclusive": True}),
                              text="专属链接给你啦：{链接}")
    number = packet["id"]
    assert packet["needs_attention"] and len(desk.review._reason(packet)) > wr.NOTE_CHARS
    lines = notice_of(desk, number)
    attention = next(line for line in lines if line.startswith("要注意："))
    assert attention.endswith("…") and len(attention) == len("要注意：") + wr.NOTE_CHARS + 1
    assert lines[-1] == "要注意的话太长，请在控制台看完再批；不要的话回 2"
    assert not any("回「执行」" in line for line in lines)
    assert desk.prm.store.wechat_review(number)["preview"] == ""
    refused = f"#{number} 要注意的话太长，请在控制台看完再批；不要的话回 2"
    assert desk.admin("执行", quote=head(desk, number)) == refused and desk.admin(f"执行 {number}") == refused
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.executions() == []
    assert shown_pending(desk, number)["wechat_execute"] is False
    assert desk.admin("2", quote=head(desk, number)) == f"#{number} 不发了（她再发消息时会有新的草稿）"


def shown_pending(desk, number) -> dict:
    return next(p for p in desk.prm.snapshot()["pending"] if p["id"] == number)


def test_a_recall_that_makes_a_platform_drafts_要注意_too_long_takes_执行_back_to_the_console(desk):
    """The first notice let 「执行」 run (its 要注意 short); her recall puts its note first: the fresh notice cuts it, so
    「执行」 is the console's, quoting either notice."""
    her_uid_known(desk)
    # (the shop's way, 100 a colour: 合并 fix1110-s, a 专属链接 off it asks for attention, convention_text)
    packet = platform_waiting(desk, block({**CHANNEL, "stock_per_sku": platforms.CHANNEL_STOCK_PER_SKU, "exclusive": True}),
                              text="专属链接给你啦：{链接}")
    number = packet["id"]
    assert desk.prm.store.wechat_review(number)["preview"]
    her_recall(desk)
    desk.review.sweep()
    fresh = latest_notice(desk, number)
    assert fresh[0].startswith(f"#{number} 草稿（第 2 次通知）") and fresh[-1] == "要注意的话太长，请在控制台看完再批；不要的话回 2"
    assert desk.prm.store.wechat_review(number)["preview"] == ""
    assert desk.admin("执行", quote=head(desk, number)) == f"#{number} 这份草稿在通知以后有变化，看最新那条通知再回"
    assert desk.admin("执行", quote=fresh[0]) == f"#{number} 要注意的话太长，请在控制台看完再批；不要的话回 2"
    assert desk.executions() == []


# B-M1 / B-M2: her full ids in the admin's chat (made-up ids)
DRAFT_UID = "11112222333344440001"        # the UID a 专属链接 draft binds
ROUND_UID = "99998888777766660002"        # her UID on the round's verified sample order (``meta.chain_uid``)
AWEME = "test_dy.4321abc"                 # her 抖音号 (the invite ledger's)


def test_a_refused_执行_of_a_chain_step_shows_her_uids_by_their_last_characters(desk):
    """Review B-M1 (probe_wr_uid): a 专属链接 step of the chain binding another UID than its round's sample order's: the
    notice masked both, but 「执行」's refusal (``wrong_uid_text``) showed the round's UID in full (only the action's ids
    were masked).  Every id of the draft now: the round's UID (``meta.chain_uid``) and her sample orders' too."""
    her_uid_known(desk, ROUND_UID)
    action = {"action": "dy.channel_goods", "item_id": DY_ITEM, "creator_platform_id": DRAFT_UID,
              "title_prefix": "【小美专属】", "exclusive": True, "convention": 1}
    preview = {"verdict": "ready", "facts": [{"label": "批准以后", "value": f"绑定达人 UID {DRAFT_UID}"}], "problems": [],
               "run": {}}
    from src.prm.service import wrong_uid_text
    number = desk.prm.store.add_packet(
        now=desk.clock.now, deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other",
        text="老师专属链接给您～ {链接}", attachments=[], intent="", slots={}, stage_proposal="", needs_attention=True,
        reason=wrong_uid_text(DRAFT_UID, ROUND_UID), tool="dy.channel_goods", meta={"chain_uid": ROUND_UID},
        platform={"action": action, "preview": preview})
    desk.review.sweep()
    text = "\n".join(notice_of(desk, number))
    assert DRAFT_UID not in text and ROUND_UID not in text and f"…{ROUND_UID[-4:]}" in text
    reply = desk.admin("执行", quote=head(desk, number))
    assert reply.startswith(f"#{number} 没执行：链接要绑的 UID 不是这一轮寄样单上的")
    assert DRAFT_UID not in reply and ROUND_UID not in reply and f"寄样单上的 UID：…{ROUND_UID[-4:]}" in reply
    assert desk.executions() == []


def test_the_invite_ledgers_douyin_id_shows_only_its_last_characters(desk):
    """Review B-M2 (probe_wr_lines 1, 2): a 专属佣金's note of an invite card that may have gone out (a fact) and one that
    did (a problem) wrote 「抖音号 <aweme_id>」 in full (``invite_cover_text``)."""
    unsure = {"state": "unsure", "rate": 25, "at": "2026-11-08T10:00", "aweme_id": AWEME}
    preview = block(DESIGNATED, facts=[{"label": "注意（邀约卡）", "value": platforms.invite_cover_text(unsure, DY_ITEM)}])
    packet = platform_waiting(desk, preview, text="专属佣金设好啦～")
    text = "\n".join(notice_of(desk, packet["id"]))
    assert AWEME not in text and f"抖音号 …{AWEME[-4:]}）" in text
    assert desk.prm.decide(packet["id"], "drop")["ok"]
    sent = {"state": "sent", "rate": 25, "at": "2026-11-08T10:00", "aweme_id": AWEME}
    step = {"name": "invite_ledger", "label": "邀约卡台账（这台 Mac，只读）", "execute": False, "command": "",
            "exit_code": None, "seconds": 0, "verdict": "blocked", "outcome": "", "facts": [], "raw": "",
            "problems": [platforms.invite_cover_text(sent, DY_ITEM)]}
    packet = platform_waiting(desk, block(DESIGNATED, verdict="blocked", steps=[step]), text="专属佣金设好啦～")
    text = "\n".join(notice_of(desk, packet["id"]))
    assert AWEME not in text and f"抖音号 …{AWEME[-4:]}）" in text


def test_her_linked_douyin_id_and_a_friend_add_name_show_only_their_last_characters(clock, tmp_path):
    """Her 抖音号 an operator linked (``creator_links``) in her words and in the 要注意, a 称呼 that is the friend add's
    「抖音-<抖音号>」, and a 直播手卡's 场次 cut to it (「称呼截成「…专属」」): by their last characters."""
    desk = Desk(clock, tmp_path)
    try:
        desk.prm.register(CREATOR, f"抖音-{AWEME}", "测试达人")
        desk.prm.store.add_creator_link(1, "test_dy_linked01", source="register", now=clock.now)
        desk.her("我另一个号是 test_dy_linked01，也可以发")
        packet = desk.mac(needs_attention=True,
                          reason=f"手卡 2 张附在草稿里，要人看过再发（截短了：称呼截成「{AWEME[:12]}专属」）；她提到 test_dy_linked01")
        desk.review.sweep()
        text = desk.told()[0]
        for private in (AWEME, AWEME[:12], "test_dy_linked01"):
            assert private not in text, private
        assert text.startswith(f"#{packet['id']} 草稿 · 抖音-…{AWEME[-4:]} · 私聊")
        assert f"她：我另一个号是 …ed01，也可以发" in text
        assert f"称呼截成「…{AWEME[8:12]}专属」" in text and "她提到 …ed01" in text
    finally:
        desk.stop()


@pytest.mark.parametrize("text, shown", [
    ("UID：99998888777766660002；草稿", "UID：…0002；草稿"),
    ("达人 uid 1234567890 绑定被拒", "达人 uid …7890 绑定被拒"),
    ("抖音号test_dy.4321abc，粉丝", "抖音号…1abc，粉丝"),
    ("aweme_id=test_dy.4321abc", "aweme_id=…1abc"),
    ("小红书号是 xhs_test_88 的买手", "小红书号是 …t_88 的买手"),
    ("uid_source 不对", "uid_source 不对"),                 # (one word: no id after a label)
    ("UID …0002 是她的", "UID …0002 是她的"),               # (masked already)
    ("抖音号 abc12 太短", "抖音号 abc12 太短"),               # (shorter than 6: never an id)
])
def test_an_id_after_its_label_is_masked_whatever_list_it_is_in(desk, text, shown):
    """Review B-M1 / B-M2 (the fallback beside the draft's ids): any run of 6 or more letters, digits, 「_」「.」 after
    「UID」「抖音号」「uid」「aweme」… keeps its last 4 only, in an error or a problem told (``_hide``) as in a line."""
    assert desk.review._hide({"platform": {}}, text) == shown
    assert wr.WechatReview._masked("问题", text, [])[1] == shown


def test_an_address_in_要注意_or_a_jushuitan_line_is_cut_to_province_and_city(desk):
    """Review B-L1 (probe_wr_lines 3): the 要注意 line and a sample order's 聚水潭 facts and problems only had their
    numbers masked: an address they quote is cut to province and city now (``retention.scrub`` with the deal's
    addresses and the receiver's), as her own lines are."""
    street = "某某路100号3栋1201室"
    desk.prm.store.update_deal(1, merge_slots={"地址": f"浙江省杭州市西湖区{street}"})
    preview = jst_block(verdict="blocked")
    preview["action"]["receiver"]["address"] = street
    preview["steps"][0]["problems"] = [f"地址不全：浙江省杭州市西湖区{street}，缺小区名"]
    preview["steps"][0]["facts"] = [*JST_FACTS, {"label": "核对", "value": f"收件地址 西湖区{street}（草稿）"}]
    desk.prm.store.token_for(1, "phone", "手机", "13900001111", desk.clock.now)
    desk.her("好的，寄吧")
    packet = desk.mac(text="寄样单建好啦，单号 {寄样单号}～", message_kind="money", platform=preview, needs_attention=True,
                      reason=f"她给的地址是 浙江省杭州市西湖区{street}，请核对")
    desk.review.sweep()
    text = "\n".join(notice_of(desk, packet["id"]))
    for private in (street, "某某路", "1201"):
        assert private not in text, private
    assert any(line.startswith("要注意：她给的地址是 浙江省杭州市（详细地址已清除）") for line in text.split("\n"))
    assert "· 问题：地址不全：浙江省杭州市（详细地址已清除）" in text


def test_an_admin_wxid_that_is_a_group_or_a_creators_tells_nothing_and_the_console_says_why(clock, tmp_path):
    """Review B-L8: ADMIN_WXID was never checked: a group's id, or the wxid of a registered creator (her own chat!), got
    every draft.  Nothing is told now, nothing there is an answer, and the console says why."""
    desk = Desk(clock, tmp_path, admin="200000001@chatroom")
    desk.prm.register(CREATOR, "小美", "测试达人")
    try:
        waiting(desk)
        assert desk.told() == [] and desk.review.ready is False
        assert desk.review.status() == {"on": True, "why": wr.GROUP_ADMIN_WHY, "open": 0, "unsent": 0, "unsure": 0,
                                        "older": 0}
    finally:
        desk.stop()
    store = PrmStore(":memory:")
    first = Desk(clock, tmp_path / "a", store=store)                   # (she was registered with another ADMIN_WXID)
    first.prm.register(OTHER, "小雨", "测试达人")
    first.stop()
    desk = Desk(clock, tmp_path / "b", admin=OTHER, store=store)
    try:
        desk.prm.register(CREATOR, "小美", "测试达人")
        waiting(desk)
        assert [t for c, t in desk.sent if c == OTHER] == [] and desk.review.ready is False
        assert desk.review.status()["why"] == wr.CREATOR_ADMIN_WHY
        msg = {"message_id": "x1", "chat_id": OTHER, "sender_id": OTHER, "content": "1", "msg_type": 1,
               "is_group": False, "timestamp": clock.now}
        assert desk.review.is_admin_line(msg) is False and desk.review.answer(msg) == (False, None, None)
    finally:
        desk.stop()


# ── the final check of 2026-11-10 (merge-1109): a fresh notice's pictures, answers while they or it are on the way ──

def test_an_answer_while_the_cards_are_still_going_waits_for_them_and_hears_when_they_came(clock, tmp_path):
    """Final check 2026-11-10 A6 (probe R1): 1 quoting the notification while its first card is being sent (the answer
    handled inside the send) hears 「图还在发，等图到了再回」 and nothing goes to her; once both cards went, one line says
    so and how to answer (only then: a draft nobody answered meanwhile gets none), and 1 sends it."""
    desk = cards_desk(clock, tmp_path)
    try:
        replies = []
        real = desk.review._send_file

        def racing(chat, path):
            if not replies:
                number = desk.prm.store.packets(("pending",))[0]["id"]
                replies.append(desk.admin("1", quote=head(desk, number)))
            return real(chat, path)

        desk.review._send_file = racing
        packet = cards_waiting(desk)
        number = packet["id"]
        assert replies == [f"#{number} {wr.PICTURES_COMING}"]
        desk.prm.send_approved()
        assert desk.to(CREATOR) == [] and desk.prm.store.packet(number)["status"] == "pending"
        assert desk.order[-3:] == [*(("file", ADMIN, path) for path in card_paths(packet)), (
            "text", ADMIN, f"#{number} 的图发过来了（在上面）：看过再引用那条通知回 1（或者回「发 {number}」）")]
        assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美"
    finally:
        desk.stop()


def test_a_fresh_notice_whose_pictures_went_already_sends_none_again(clock, tmp_path):
    """A fresh notice (her recall) of a draft whose cards went after the first one: they are above, the same ones; none
    goes again, and it may be answered at once."""
    desk = cards_desk(clock, tmp_path)
    try:
        packet = cards_waiting(desk)
        number = packet["id"]
        sent = list(desk.files)
        her_recall(desk)
        desk.review.sweep()
        lines = latest_notice(desk, number)
        assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
        assert f"（要看的 2 张图在前面的 #{number} 通知后面，没有变）" in lines and desk.files == sent
        assert desk.admin("1", quote=lines[0]) == f"#{number} 已批准，正在发给 小美"
    finally:
        desk.stop()


def test_a_fresh_notice_after_a_new_preview_sends_its_new_sheet_and_waits_for_it(clock, tmp_path):
    """Final check 2026-11-10 A1 (probe R10): the first notice of a sample order that held a hat went with its 对图单 v1
    (no 「执行」); 「对过图了」 in the console (the bot previews it again by itself now) made a ready preview with another
    sheet (v2).  Her recall told it again: the fresh notice offered 「执行」 for the new preview, said its pictures were
    「在第一条通知后面」 (v1), and v2 never went.  A fresh notice whose pictures are not the ones that went (or whose
    preview is another) sends the current ones after it, as a first notice does; until they came the draft is the
    console's in WeChat (an answer meanwhile hears 「图还在发」), and one line says when they came.  (Since 2026-11-10 the
    new preview is told again by itself; her recall meanwhile is told in the same notice.)"""
    desk = photo_desk(clock, tmp_path)
    try:
        desk.prm.store.token_for(1, "phone", "手机", "13900001111", desk.clock.now)
        desk.her("好的，寄吧")
        problems = [f"{CODE}（均码咖啡）对图没把握（款有把握，颜色没把握）：先核对再建；{draft_context.CONFIRM_HOW}"]
        held = {**jst_block(), "verdict": "blocked", "problems": problems, "run": {}}
        desk.clock.now += SETTLE_SEC + 1
        job = desk.relay.next_job(0, ("prm",))
        first = f"对图单-{job['id']}.jpg"
        v1 = desk.relay.operator_file(job["id"], first, io.BytesIO(b"\xff\xd8 v1").read, len(b"\xff\xd8 v1"))
        body = reply_draft(text="寄样单建好啦，单号 {寄样单号}～", message_kind="money", platform=held, draft_context=GATED,
                           photo_held=[CODE], operator_files=[{"name": first, "why": "并排对比", "sha256": v1["sha256"]}])
        desk.relay.finish(job["id"], {"ok": True, "text": json.dumps(body, ensure_ascii=False), "duration_s": 3})
        desk.relay.tick()
        packet = desk.prm.store.packet_for_job(job["id"])
        number = packet["id"]
        desk.review.sweep()
        assert [path.rsplit("/", 1)[-1] for chat, path in desk.files if chat == ADMIN] == [first]
        assert desk.prm.deal_action("photo_confirm", {"deal_id": packet["deal_id"], "packet": number,
                                                      "code": CODE})["repreview"] is True
        again = desk.relay.next_job(0, ("prm",))
        second = f"对图单-{again['id']}.jpg"
        v2 = desk.relay.operator_file(again["id"], second, io.BytesIO(b"\xff\xd8 v2!").read, len(b"\xff\xd8 v2!"))
        ready = {**jst_block(), "draft_context": GATED, "photo_confirmed": [CODE],
                 "operator_files": [{"name": second, "why": "并排对比 v2", "sha256": v2["sha256"]}]}
        desk.relay.finish(again["id"], {"ok": True, "text": json.dumps(ready, ensure_ascii=False), "duration_s": 2})
        desk.relay.tick()
        desk.clock.now += 30
        her_recall(desk)
        answers = []
        real = desk.review._send_file

        def meanwhile(chat, path):
            answers.append(desk.admin("执行", quote=latest_notice(desk, number)[0]))
            return real(chat, path)

        desk.review._send_file = meanwhile
        desk.review.sweep()
        lines = latest_notice(desk, number)
        assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
        assert lines[1] == (f"上一条 #{number} 的通知以后有变化：她撤回了一条消息（见「要注意」）；重新预览过了（下面是新的预览）。"
                            "上一条作废了，请看这一条再回")
        assert f"（后面附 1 张图给你看：{second}：并排对比 v2）" in lines and not any("没有变" in line for line in lines)
        assert [path.rsplit("/", 1)[-1] for chat, path in desk.files if chat == ADMIN] == [first, second]
        assert answers == [f"#{number} {wr.PICTURES_COMING}"] and desk.executions() == []
        assert desk.told()[-1] == (f"#{number} 的图发过来了（在上面）：看过再引用最新那条通知回「执行」"
                                   f"（或者回「执行 {number}」）")
        assert desk.prm.store.wechat_review(number)["pictures_ok"] == 1
        assert desk.admin("执行", quote=lines[0]).startswith(f"#{number} 已批准执行")
        assert len(desk.executions()) == 1
    finally:
        desk.stop()


def test_an_unquoted_answer_while_the_fresh_notice_is_being_sent_does_nothing(desk):
    """Final check 2026-11-10 A3 (probe R8): her recall, and the sweep sends 「#N 草稿（第 2 次通知）」; while that send is
    under way (the row TELLING, its notice counted up, its ``seen`` the new one, ``notified_at`` still the first
    notice's), an unquoted 「发 N」 written before it showed was taken (``_read_latest`` compared it with the first
    notice's time).  It does nothing now; one written after the fresh notice acts."""
    packet = waiting(desk)
    number = packet["id"]
    desk.clock.now += 5
    her_recall(desk)
    replies = []
    real = desk.review._send

    def racing(chat, text):
        if chat == ADMIN and "第 2 次通知" in text and not replies:
            assert desk.prm.store.wechat_review(number)["state"] == "telling"
            replies.append(desk.admin(f"发 {number}", at=desk.clock.now - 1))
        return real(chat, text)

    desk.review._send = racing
    desk.review._skew = lambda: 0.0                 # (this PC's clock measured against WeChat's: as in production)
    desk.clock.now += 30                            # (the recall came a while after the first notice)
    desk.review.sweep()
    assert replies == [f"#{number} 这份草稿在通知以后有变化，看最新那条通知再回"]
    desk.prm.send_approved()
    assert desk.to(CREATOR) == [] and desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin(f"发 {number}") == f"#{number} 已批准，正在发给 小美"


# ── 2026-11-10: 「对过图 1」 quoting the notice confirms a held hat; the last one confirmed, the bot previews the order
# again by itself and tells the new preview in a fresh notice, whose own 「执行」 makes the order ───────────────────────

CODE_2, CODE_3 = "A7269901002A-F", "A7269901003A-F"          # more made-up 聚水潭 item codes
HOW = draft_context.CONFIRM_HOW
UNDO = "要撤销请到控制台"
COMING = "正在重新预览，好了再发一条通知"
PHOTO_HOW = "对上了就引用这条通知回「对过图 1」这样（几顶就写「对过图 1 2」），后面别带「…」「.」「？」"
CONSOLE = "请到控制台在这顶旁边点「对过图了」"


def held_row(code, row, why="同款别的颜色（她的图最像的是驼色）", right="这个编码自己的图"):
    """A row of the held hats' sheet as the Mac's preview says it (tools/claude_relay/draft_context.py ``_held_sheet``)."""
    return {"label": "对图（扣着的那几顶）",
            "value": f"{code}：扣着（{why}）；扣着的那张对图单第 {row} 行，左边是她这次发的 图片-1.jpg，右边是{right}"}


def hold(code, name="均码咖啡", no_photo=False):
    """A held hat's problem as the Mac writes it (``_gate``)."""
    return (f"{code}（{name}）对图没把握（只在没把握的图里，或者不是图里最像的那一款、那个颜色）：先核对再建；"
            + ("没有这个编码的图，请自己找图核对；" if no_photo else "") + HOW)


def pair_row(code, note=""):
    """A row of the side-by-side sheet (a hat with a sure picture of hers)."""
    return {"label": "对图", "value": f"{code} 驼色：对的是这次发的 图片-1.jpg（图库里第 1 款、第 1 个颜色）"
                                      + (f"；{note}" if note else "")}


def sheets_of(desk, job_id, kinds) -> list:
    """The Mac sends its sheets back on the operator-only channel: the draft's ``operator_files``."""
    names = {"pair": f"对图单-{job_id}.jpg", "held": f"对图单-{job_id}-扣着的.jpg"}
    files = []
    for kind in kinds:
        body = f"\xff\xd8 {kind} sheet of job {job_id}".encode()
        sent = desk.relay.operator_file(job_id, names[kind], io.BytesIO(body).read, len(body))
        files.append({"name": names[kind], "why": "并排对比" if kind == "pair" else "扣着的那几顶", "sha256": sent["sha256"]})
    return files


def photo_order(desk, *, held, facts, problems, sheets=("held",), told=True, hats=None) -> dict:
    """She agrees; the Mac drafts a sample order, checks it by photo (``held`` for photos, its preview's ``facts`` and
    ``problems`` as the Mac writes them) and sends its ``sheets`` back; the draft waits and is told with them.  ``hats``:
    the Mac's ``photo_hats`` (where each held hat is seen, as data: a worker since 2026-11-10; None: an older one, the
    bot reads the facts)."""
    desk.prm.store.token_for(1, "phone", "手机", "13900001111", desk.clock.now)
    desk.her("好的，寄吧")
    desk.clock.now += SETTLE_SEC + 1
    job = desk.relay.next_job(0, ("prm",))
    preview = {**jst_block(), "verdict": "blocked", "facts": list(facts), "problems": list(problems), "run": {}}
    body = reply_draft(text="寄样单建好啦，单号 {寄样单号}～", message_kind="money", platform=preview, draft_context=GATED,
                       photo_held=list(held), operator_files=sheets_of(desk, job["id"], sheets),
                       **({} if hats is None else {"photo_hats": hats}))
    desk.relay.finish(job["id"], {"ok": True, "text": json.dumps(body, ensure_ascii=False), "duration_s": 3})
    desk.relay.tick()
    packet = desk.prm.store.packet_for_job(job["id"])
    assert packet["status"] == "pending" and packet["tool"] == "jst.sample_order"
    if told:
        desk.review.sweep()
    return desk.prm.store.packet(packet["id"])


def two_held(desk, **kw) -> dict:
    """A sample order with two hats held, both on the held hats' sheet (rows 1 and 2), and a sure one on the
    side-by-side sheet: both sheets follow its notification."""
    return photo_order(desk, held=(CODE, CODE_2), sheets=("pair", "held"),
                       facts=[pair_row(CODE_3), held_row(CODE, 1), held_row(CODE_2, 2, why="颜色没把握")],
                       problems=[hold(CODE), hold(CODE_2, "均码粉色")], **kw)


def one_held(desk, **kw) -> dict:
    return photo_order(desk, held=(CODE,), facts=[held_row(CODE, 1)], problems=[hold(CODE)], **kw)


def previewed_again(desk, number, *, held=(), used=(), facts=(), problems=(), sheets=("pair",), verdict="ready"):
    """The Mac takes the new preview's job and answers: the new preview, its marks and its sheets."""
    job = desk.relay.next_job(0, ("prm",))
    assert job is not None and job["meta"]["op"] == "platform_preview" and job["meta"]["packet_id"] == number
    body = {**jst_block(), "verdict": verdict, "facts": list(facts), "problems": list(problems),
            "run": {"steps": ["create"]} if verdict == "ready" else {}, "draft_context": GATED,
            **({"photo_held": list(held)} if held else {}), **({"photo_confirmed": list(used)} if used else {}),
            "operator_files": sheets_of(desk, job["id"], sheets)}
    assert desk.relay.finish(job["id"], {"ok": True, "text": json.dumps(body, ensure_ascii=False), "duration_s": 3})
    desk.relay.tick()
    return job


def confirmed_codes(desk, deal_id=1) -> list:
    return [r["code"] for r in desk.prm.store.photo_confirmations(deal_id)]


def previewing(desk, number) -> bool:
    return bool(desk.prm.store.packet(number)["platform"].get("previewing"))


@pytest.fixture
def pdesk(clock, tmp_path):
    d = photo_desk(clock, tmp_path)
    yield d
    d.stop()


def test_the_notice_numbers_the_held_hats_as_the_held_sheet_has_them_and_says_how_to_confirm(pdesk):
    """The notice of a sample order with held hats a sheet shows: 「第 1 顶 <编码>（为什么扣着；哪张对图单第几行）」 in the
    held sheet's row order (whatever order the preview held them in), then how to confirm, quoting it; its sheets go
    right after it; the row keeps the hats as numbered; no stamp (never run from it)."""
    packet = photo_order(pdesk, held=(CODE_2, CODE), sheets=("pair", "held"),
                         facts=[pair_row(CODE_3), held_row(CODE, 1), held_row(CODE_2, 2, why="颜色没把握")],
                         problems=[hold(CODE_2, "均码粉色"), hold(CODE)])
    number = packet["id"]
    lines = notice_of(pdesk, number)
    assert lines[-4:] == [
        "有 2 顶帽子扣着（没对上图），对上了才能建这张寄样单：",
        f"第 1 顶 {CODE}（同款别的颜色（她的图最像的是驼色）；「扣着的」那张对图单第 1 行）",
        f"第 2 顶 {CODE_2}（颜色没把握；「扣着的」那张对图单第 2 行）",
        "看过下面的对图单，对上了就引用这条回「对过图 1」（几顶就写「对过图 1 2」，都对上了写「对过图 全部」）；"
        "都确认完机器人自己重新预览、再发一条通知；不要的话回 2"]
    assert not any("回「执行」" in line for line in lines)
    # (each hold is listed once, without the Mac's 「…在控制台…点「对过图了」」: the last lines say how, from here)
    assert f"· 问题：{hold(CODE).removesuffix('；' + HOW)}" in lines and not any("在控制台这份草稿上" in line for line in lines)
    row = pdesk.prm.store.wechat_review(number)
    assert json.loads(row["hats"]) == [{"code": CODE, "ok": True}, {"code": CODE_2, "ok": True}]
    assert row["preview"] == "" and row["pictures_ok"] == 1 and row["shown"]
    job = packet["job_id"]
    assert [path.rsplit("/", 1)[-1] for kind, chat, path in pdesk.order if kind == "file"] == [
        f"对图单-{job}.jpg", f"对图单-{job}-扣着的.jpg"]
    assert pdesk.order[-3][0] == "text" and pdesk.order[-3][2].startswith(f"#{number} 草稿")   # (the sheets after it)
    assert draft_context.held_hats(packet) == [
        {"code": CODE, "why": "同款别的颜色（她的图最像的是驼色）", "sheet": "held", "row": 1, "no_photo": False},
        {"code": CODE_2, "why": "颜色没把握", "sheet": "held", "row": 2, "no_photo": False}]


def test_对过图_quoting_the_notice_confirms_that_hat_only_and_never_runs_anything(pdesk):
    """「对过图 1」 quoting the notice: the console's 「对过图了」 for the first hat (the event says it came from WeChat);
    the other stays held, nothing is previewed again, nothing runs; undo stays the console's, and the answer says so."""
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert pdesk.admin("对过图 1", quote=quote) == (
        f"#{number} 已确认第 1 顶 {CODE}（{UNDO}）；还扣着第 2 顶 {CODE_2}：对上了再引用这条通知回「对过图 2」")
    assert confirmed_codes(pdesk) == [CODE] and not previewing(pdesk, number)
    [event] = pdesk.prm.store.events(packet["deal_id"], kinds=("photo_confirm",))
    assert event["actor"] == "wechat" and event["detail"] == {"code": CODE, "packet": number}
    from src.prm.service import event_out
    assert event_out(event)["name"] == f"对过图了，用 {CODE}（#{number} 的寄样单）（在微信里回的）"
    assert pdesk.executions() == [] and pdesk.prm.store.packet(number)["status"] == "pending"
    assert pdesk.jobs("prm") == [j for j in pdesk.jobs("prm") if j["status"] == "done"]      # (no new preview asked)
    # 「执行」 is still its own explicit answer, and the held hat still holds it
    assert pdesk.admin("执行", quote=quote) == (
        f"#{number} 有 1 顶帽子扣着（没对上图：{CODE_2}），还不能执行：看过对图单，对上了就引用那条通知回「对过图 2」这样确认"
        "（或者到控制台点「对过图了」）；不要的话回 2")
    assert pdesk.admin(f"执行 {number} 了吗").startswith(f"#{number}（小美）还没执行（在等你确认）：有 1 顶帽子扣着")
    assert pdesk.executions() == []
    # the console's 「撤销」 takes it back; the same answer confirms it again
    assert pdesk.prm.deal_action("photo_unconfirm", {"deal_id": packet["deal_id"], "code": CODE})["ok"]
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number)
    assert pdesk.admin("对过图了 1。", quote=quote).startswith(f"#{number} 已确认第 1 顶 {CODE}")


@pytest.mark.parametrize("answer", ["对过图 1 2", "对过图了 1 2", "对过图 全部", "对过图了 全部", "对过图1、2", "对过图 2 1",
                                    "对过图 第 1 顶 第 2 顶", "对过图 １ ２", "对过图 1 2。", "对过图 全部！", "对过图 1 2～",
                                    "对过图了，1 和 2"])
def test_the_last_held_hat_confirmed_in_wechat_starts_the_new_preview_and_执行_needs_the_fresh_notice(pdesk, answer):
    """Every well-formed form of the answer.  Both hats confirmed: the bot asks for the new preview itself and says a
    notice follows; the Mac's new preview (ready, the hats let go by the confirmations, a new sheet) is told on the
    same number (「第 2 次通知」) with its sheet; until that sheet came 「执行」 does nothing; the confirmation itself ran
    nothing, 「执行」 quoting the first notice never does, and 「执行」 quoting the fresh one makes the order."""
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    reply = pdesk.admin(answer, quote=quote)
    order = (2, 1) if answer == "对过图 2 1" else (1, 2)
    named = "、".join(f"第 {i} 顶 {(CODE, CODE_2)[i - 1]}" for i in order)
    assert reply == f"#{number} 已确认{named}（{UNDO}）；{COMING}"
    assert sorted(confirmed_codes(pdesk)) == sorted([CODE, CODE_2]) and previewing(pdesk, number)
    assert pdesk.executions() == []
    assert pdesk.admin("执行", quote=quote) == f"#{number} {PREVIEWING_STOP}"
    assert pdesk.admin("对过图 1", quote=quote) == f"#{number} {COMING}（现在不用回「对过图」）"
    job = previewed_again(pdesk, number, used=(CODE, CODE_2), facts=[pair_row(CODE_3)])
    assert job["meta"]["draft_context"]["confirmed"] == confirmed_codes(pdesk)
    assert pdesk.admin("执行", quote=quote) == f"#{number} {STALE}（新的通知马上发过来）"
    answers, real = [], pdesk.review._send_file

    def meanwhile(chat, path):
        answers.append(pdesk.admin("执行", quote=latest_notice(pdesk, number)[0]))
        return real(chat, path)

    pdesk.review._send_file = meanwhile
    assert pdesk.review.sweep() == 1
    pdesk.review._send_file = real
    lines = latest_notice(pdesk, number)
    sheet = f"对图单-{job['id']}.jpg"
    assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
    assert lines[1] == f"上一条 #{number} 的通知以后有变化：重新预览过了（下面是新的预览）。上一条作废了，请看这一条再回"
    assert f"（后面附 1 张图给你看：{sheet}：并排对比）" in lines
    assert lines[-2].startswith("引用这条回「执行」") and not any("对过图" in line for line in lines[2:])
    assert pdesk.files[-1][1].endswith(sheet) and answers == [f"#{number} {wr.PICTURES_COMING}"]
    row = pdesk.prm.store.wechat_review(number)
    assert (row["notice"], row["pictures_ok"], row["hats"]) == (2, 1, "") and row["preview"]
    assert pdesk.executions() == []                                      # (nothing ran by itself, at any point)
    assert pdesk.admin("执行", quote=quote) == f"#{number} {STALE}"
    assert pdesk.admin("1", quote=lines[0]).startswith(f"#{number} 带平台动作")
    assert pdesk.executions() == []
    assert pdesk.admin("执行", quote=lines[0]).startswith(f"#{number} 已批准执行")
    assert len(pdesk.executions()) == 1 and pdesk.prm.store.packet(number)["status"] == "executing"


def test_with_one_held_hat_a_plain_对过图_counts_and_with_more_it_asks_which(pdesk):
    """「对过图」 / 「对过图了」 alone: the one hat the notice numbers; with two hats it confirms nothing and lists them."""
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    listed = f"扣着的是：第 1 顶 {CODE}；第 2 顶 {CODE_2}"
    for answer in ("对过图", "对过图了", "对过图了。"):
        assert pdesk.admin(answer, quote=quote) == (
            f"#{number} 扣着 2 顶，要写第几顶（「对过图 1 2」这样，或者「对过图 全部」）：什么都没确认。{listed}"), answer
    assert confirmed_codes(pdesk) == []
    assert pdesk.admin("2", quote=quote) == f"#{number} 不发了（她再发消息时会有新的草稿）"
    pdesk.clock.now += 60
    single = one_held(pdesk)
    n = single["id"]
    lines = notice_of(pdesk, n)
    assert lines[-3:] == [
        "有 1 顶帽子扣着（没对上图），对上了才能建这张寄样单：",
        f"第 1 顶 {CODE}（同款别的颜色（她的图最像的是驼色）；「扣着的」那张对图单第 1 行）",
        "看过下面的对图单，对上了就引用这条回「对过图 1」（只回「对过图」也行）；都确认完机器人自己重新预览、再发一条通知；"
        "不要的话回 2"]
    # (one hat: what is no command still confirms nothing, a question least of all)
    for answer in ("对过图…", "对过图.", "对过图。。", "对过图了...", "对过图？", "对过图了吗", "对过图了没", "对过图呢", "对过图了吧？",
                   "对过图 1…", "对过图了，应该对", "对过图 2", "对过图 0"):
        reply = pdesk.admin(answer, quote=lines[0])
        assert not reply.startswith(f"#{n} 已确认"), answer
        assert confirmed_codes(pdesk) == [] and not previewing(pdesk, n), answer
    assert shown(pdesk, n)["wechat_photo"] is True and shown(pdesk, n)["wechat_execute"] is False
    assert pdesk.admin("对过图了", quote=lines[0]) == f"#{n} 已确认第 1 顶 {CODE}（{UNDO}）；{COMING}"
    assert confirmed_codes(pdesk) == [CODE] and previewing(pdesk, n) and pdesk.executions() == []
    assert "wechat_photo" not in shown(pdesk, n)                         # (confirmed: nothing left to confirm there)


@pytest.mark.parametrize("answer", ["对过图了吗", "对过图了没", "对过图？", "对过图 1 吗", "对过图 1？", "对过图了么", "对过图了呢",
                                    "对过图 全部了吗", "对过图了没有"])
def test_a_question_about_the_photos_says_how_the_hats_stand_and_confirms_nothing(pdesk, answer):
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert pdesk.admin(answer, quote=quote) == (
        f"#{number} 扣着 2 顶：第 1 顶 {CODE} 还没确认；第 2 顶 {CODE_2} 还没确认。对上了就引用这条通知回「对过图 1」")
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number)
    assert pdesk.admin("对过图 1", quote=quote).startswith(f"#{number} 已确认第 1 顶")
    assert pdesk.admin(answer, quote=quote) == (
        f"#{number} 扣着 2 顶：第 1 顶 {CODE} 已确认；第 2 顶 {CODE_2} 还没确认。对上了就引用这条通知回「对过图 2」")
    assert confirmed_codes(pdesk) == [CODE]


@pytest.mark.parametrize("answer", ["对过图 1…", "对过图 1.", "对过图…", "对过图 1。。", "对过图 1...", "对过图 全部…", "对过图。。",
                                    "对过图了，第一顶颜色对", "对过图 1 应该吧", "对过图了，1 没问题 2 再看看", "对过图 A7269901001B-F"])
def test_a_hesitating_or_wordy_answer_confirms_nothing(pdesk, answer):
    """The ending rule of every command (``_command_word``): 「…」「.」「。。」 after it and it does nothing; so do other
    words around the numbers (never read as a confirmation)."""
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert pdesk.admin(answer, quote=quote) == f"#{number} 没看懂，什么都没确认：{PHOTO_HOW}"
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number) and pdesk.executions() == []


@pytest.mark.parametrize("answer", ["对过图 0", "对过图 3", "对过图 1 1", "对过图 12 1", "对过图 1 2 3", "对过图 2 2 1", "对过图 99"])
def test_a_number_the_notice_does_not_have_refuses_the_whole_answer(pdesk, answer):
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert pdesk.admin(answer, quote=quote) == (
        f"#{number} 编号不对（这条通知里只有第 1–2 顶，每顶写一次）：什么都没确认。扣着的是：第 1 顶 {CODE}；第 2 顶 {CODE_2}")
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number)


def test_对过图_never_counts_without_a_quote_of_the_notice(pdesk):
    """Unquoted (a draft number in front too), quoting Claude's words, a picture, the bot's own line that the pictures
    came, a voice message: nothing is confirmed, the bot says to quote the notice, and none of it goes on to the relay
    or an admin command."""
    packet = two_held(pdesk)
    number = packet["id"]
    waiting = f"（扣着帽子在等的：#{number}）"
    hint = f"要确认对过图：请引用那条列着「第 1 顶 …」的草稿通知回「对过图 1」（不引用不算）{waiting}"
    elsewhere = f"你引用的不是草稿通知：要确认对过图，请引用那条列着「第 1 顶 …」的草稿通知回「对过图 1」{waiting}"
    for answer in ("对过图 1", f"对过图 {number} 1", "对过图 12 1", "对过图", "对过图了 全部", "对过图 1 2"):
        assert pdesk.admin(answer) == hint, answer
    for quoted in ("方案 1：先核对两顶的颜色", "[图片]", f"#{number} 的图发过来了（在上面）：看过再回", "对图单-1-扣着的.jpg"):
        assert pdesk.admin("对过图 1", quote=quoted) == elsewhere, quoted
        assert pdesk.admin("对过图 全部", quote=quoted) == elsewhere, quoted
    assert pdesk.admin("[语音] 对过图 1", msg_type=34) == VOICE_TEXT
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number)
    assert pdesk.jobs() == [] and pdesk.executions() == []
    # (a 改成 question is no notice of the hats either; a sample order's reply is never proposed anyway)
    assert pdesk.admin("对过图 1", quote=f"#{number} 改成：『好的』") == f"#{number} {hint[:hint.index('（扣着')]}"
    assert confirmed_codes(pdesk) == []


def test_对过图_with_no_held_sample_order_waiting_goes_on_as_any_other_line(clock, tmp_path):
    """Nothing told here waits with a held hat: a line that starts with 「对过图」 is not the review's (in
    CLAUDE_RELAY_ONLY it is Claude's task, as before)."""
    desk = Desk(clock, tmp_path, relay_only="on", prm_draft_context="on")
    desk.prm.register(CREATOR, "小美", "测试达人")
    try:
        platform_waiting(desk)                                           # (a sample order nothing holds)
        desk.admin("对过图 1")
        assert [j["text"] for j in desk.jobs()] == ["对过图 1"]
    finally:
        desk.stop()


def test_relay_only_对过图_is_never_claudes_while_a_held_order_waits_and_the_quote_still_confirms(clock, tmp_path):
    """CLAUDE_RELAY_ONLY: an unquoted 「对过图 1」, or one quoting Claude's message, confirms nothing and is not handed to
    Claude while the held sample order waits (the hint says how to give it to Claude instead); quoting the notice
    confirms, as in the other mode."""
    desk = Desk(clock, tmp_path, relay_only="on", prm_draft_context="on")
    desk.prm.register(CREATOR, "小美", "测试达人")
    try:
        packet = two_held(desk)
        number = packet["id"]
        waiting = f"（扣着帽子在等的：#{number}）"
        assert desk.admin("对过图 1") == (
            f"要确认对过图：请引用那条列着「第 1 顶 …」的草稿通知回「对过图 1」（不引用不算）{waiting}{CC_HINT}")
        assert desk.admin("对过图 全部", quote="好的，我先核对一下第 1 顶") == (
            f"你引用的不是草稿通知：要确认对过图，请引用那条列着「第 1 顶 …」的草稿通知回「对过图 1」{waiting}{CC_HINT}")
        assert desk.jobs() == [] and confirmed_codes(desk) == []
        assert desk.admin("对过图 1 2", quote=head(desk, number)) == (
            f"#{number} 已确认第 1 顶 {CODE}、第 2 顶 {CODE_2}（{UNDO}）；{COMING}")
        assert desk.jobs() == [] and desk.executions() == [] and previewing(desk, number)
    finally:
        desk.stop()


def test_an_answer_to_an_older_notice_confirms_nothing(pdesk):
    """The quoted notice must be the draft's latest: after her recall (before the fresh notice went, and after), 「对过图
    1」 quoting the first one does nothing; quoting the fresh one (the hats as it numbers them, its pictures the ones
    sent before) confirms."""
    packet = two_held(pdesk)
    number, first = packet["id"], head(pdesk, packet["id"])
    sent = list(pdesk.files)
    her_recall(pdesk)
    assert pdesk.admin("对过图 1", quote=first) == f"#{number} {STALE}（新的通知马上发过来）"
    assert pdesk.admin("对过图了吗", quote=first) == f"#{number} {STALE}（新的通知马上发过来）"
    assert pdesk.review.sweep() == 1
    lines = latest_notice(pdesk, number)
    assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊" and pdesk.files == sent
    assert f"（要看的 2 张图在前面的 #{number} 通知后面，没有变）" in lines
    assert lines[-1].startswith("看过前面发过的对图单，对上了就引用这条回「对过图 1」")
    for answer in ("对过图 1", "对过图 全部", "对过图 1 2"):
        assert pdesk.admin(answer, quote=first) == f"#{number} {STALE}", answer
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number)
    assert pdesk.admin("对过图 2", quote=lines[0]) == (
        f"#{number} 已确认第 2 顶 {CODE_2}（{UNDO}）；还扣着第 1 顶 {CODE}：对上了再引用这条通知回「对过图 1」")
    assert confirmed_codes(pdesk) == [CODE_2]


def test_a_fresh_notice_shows_which_hats_are_confirmed_already(pdesk):
    """One hat confirmed in the console, then her recall: the fresh notice numbers the same hats, says which is
    confirmed, and asks for the other one by its number; 「对过图 全部」 quoting it confirms what is left."""
    packet = two_held(pdesk)
    number = packet["id"]
    done = pdesk.prm.deal_action("photo_confirm", {"deal_id": packet["deal_id"], "packet": number, "code": CODE})
    assert done["ok"] and done["repreview"] is False and done["left"] == [CODE_2]
    assert pdesk.review.sweep() == 0                                     # (a confirmation alone is no fresh notice)
    her_recall(pdesk)
    pdesk.review.sweep()
    lines = latest_notice(pdesk, number)
    assert lines[-4:-1] == ["有 1 顶帽子扣着（没对上图），对上了才能建这张寄样单：", f"第 1 顶 {CODE}（已确认）",
                            f"第 2 顶 {CODE_2}（颜色没把握；「扣着的」那张对图单第 2 行）"]
    assert lines[-1].startswith("看过前面发过的对图单，对上了就引用这条回「对过图 2」；")
    assert pdesk.admin("对过图 全部", quote=lines[0]) == f"#{number} 已确认第 2 顶 {CODE_2}（{UNDO}）；{COMING}"
    assert previewing(pdesk, number) and pdesk.executions() == []


def test_对过图_counts_only_once_the_sheets_reached_the_admin(pdesk):
    """While the sheets are still going an answer waits for them (「图还在发」); a sheet WeChat did not take: nothing is
    confirmed from WeChat (「图没发过来」) until it went again.  A question says the same."""
    answers, real = [], pdesk._send_file

    def meanwhile(chat, path):
        if not answers:
            number = pdesk.prm.store.packets(("pending",))[0]["id"]
            quote = head(pdesk, number)
            answers.append(pdesk.admin("对过图 1", quote=quote))
            answers.append(pdesk.admin("对过图了吗", quote=quote))
        return real(chat, path)

    pdesk.review._send_file = meanwhile
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert answers == [f"#{number} {wr.PICTURES_COMING}",
                       f"#{number} 扣着 2 顶：第 1 顶 {CODE} 还没确认；第 2 顶 {CODE_2} 还没确认。{wr.PICTURES_COMING}"]
    assert confirmed_codes(pdesk) == []
    assert pdesk.told()[-1] == f"#{number} 的图发过来了（在上面）：对上了就引用那条通知回「对过图 1」这样确认；不要的话回「不发 {number}」"
    assert pdesk.admin("2", quote=quote).startswith(f"#{number} 不发了")
    pdesk.review._send_file = real
    pdesk.files_fail = True
    pdesk.clock.now += 60
    other = two_held(pdesk)
    n, quote = other["id"], head(pdesk, other["id"])
    assert pdesk.told()[-1] == f"#{n} {PICTURES_MISSING}"
    for answer in ("对过图 1", "对过图 全部", "对过图 1 2"):
        assert pdesk.admin(answer, quote=quote) == f"#{n} {PICTURES_MISSING}", answer
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, n)
    pdesk.files_fail = False
    pdesk.clock.now += RETRY_SEC[1] + 1
    pdesk.review.sweep()
    assert pdesk.told()[-1] == f"#{n} 的图补发过来了（在上面）：对上了就引用那条通知回「对过图 1」这样确认；不要的话回「不发 {n}」"
    assert pdesk.admin("对过图 1", quote=quote).startswith(f"#{n} 已确认第 1 顶 {CODE}")


def test_对过图_counts_only_for_the_very_sheets_that_went_with_the_notice(pdesk):
    """Second line: the pictures the notice went with are remembered (``pictures_sent``); should the draft's sheets be
    others now without a fresh notice, nothing is confirmed from WeChat."""
    packet = one_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert pdesk.prm.store.set_wechat_review(number, (), pdesk.clock.now, pictures_sent="0" * 16)
    assert pdesk.admin("对过图", quote=quote) == f"#{number} 对图单和这条通知后面发的不是同一份了，微信里确认不了：{CONSOLE}"
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number)


def test_a_sheet_not_on_this_pc_leaves_the_hats_to_the_console(pdesk):
    """A sheet that cannot go (deleted from this PC before the notice): the notice numbers no hat, says the pictures did
    not come, and 「对过图 1」 quoting it confirms nothing."""
    packet = two_held(pdesk, told=False)
    for entry in packet["meta"]["operator_files"]:
        if "扣着的" in entry["name"]:
            from pathlib import Path
            Path(entry["path"]).unlink()
    pdesk.review.sweep()
    number = packet["id"]
    lines = notice_of(pdesk, number)
    assert lines[-1].startswith("有 2 顶帽子扣着") and "请到控制台对过图" in lines[-1]
    assert pdesk.prm.store.wechat_review(number)["hats"] == "" and pdesk.files == []
    assert pdesk.admin("对过图 1", quote=lines[0]) == f"#{number} {lines[-1]}"
    assert confirmed_codes(pdesk) == []


def test_a_hat_no_sheet_shows_is_confirmed_in_the_console_only_and_is_no_part_of_全部(pdesk):
    """Three held hats: one on the held sheet with its own photo; one whose row has no picture of its code (「没有这个
    编码的图」); one with no row at all (the sheet was full).  Only the first is confirmed from WeChat: by its number,
    or by 「全部」, which says the others are the console's; their own numbers confirm nothing.  The last one confirmed
    in the console, the bot previews the order again."""
    packet = photo_order(
        pdesk, held=(CODE, CODE_2, CODE_3),
        facts=[held_row(CODE, 1), held_row(CODE_2, 2, why="她的图里都没对出这一款，左边放的是她最新发的那张", right="没有这个编码的图"),
               {"label": "对图（扣着的那几顶）", "value": f"{CODE_3}：扣着的那张对图单最多 2 行，这一顶放不下了，请自己找图核对"}],
        problems=[hold(CODE), hold(CODE_2, "均码粉色", no_photo=True),
                  f"{CODE_3}（均码灰色）这顶没有照片对过，先核对再建；{HOW}"])
    number, quote = packet["id"], head(pdesk, packet["id"])
    lines = notice_of(pdesk, number)
    assert lines[-5:-1] == [
        "有 3 顶帽子扣着（没对上图），对上了才能建这张寄样单：",
        f"第 1 顶 {CODE}（同款别的颜色（她的图最像的是驼色）；「扣着的」那张对图单第 1 行）",
        f"第 2 顶 {CODE_2}（她的图里都没对出这一款，左边放的是她最新发的那张；没有这个编码的图，要到控制台确认）",
        f"第 3 顶 {CODE_3}（没有照片对过；对图单上没有这一顶，要到控制台确认）"]
    assert lines[-1].startswith("看过下面的对图单，对上了就引用这条回「对过图 1」；")
    assert json.loads(pdesk.prm.store.wechat_review(number)["hats"]) == [
        {"code": CODE, "ok": True}, {"code": CODE_2, "ok": False}, {"code": CODE_3, "ok": False}]
    for answer, which in (("对过图 2", f"第 2 顶 {CODE_2}"), ("对过图 3", f"第 3 顶 {CODE_3}"),
                          ("对过图 2 3", f"第 2 顶 {CODE_2}、第 3 顶 {CODE_3}")):
        assert pdesk.admin(answer, quote=quote) == f"#{number} {which} 没有能对的图，微信里确认不了：{CONSOLE}", answer
    assert confirmed_codes(pdesk) == []
    assert pdesk.admin("对过图 全部", quote=quote) == (
        f"#{number} 已确认第 1 顶 {CODE}（{UNDO}）；第 2 顶 {CODE_2}、第 3 顶 {CODE_3} 没有能对的图，微信里确认不了：{CONSOLE}")
    assert confirmed_codes(pdesk) == [CODE] and not previewing(pdesk, number)
    assert pdesk.admin("对过图 全部", quote=quote) == (
        f"#{number} 第 2 顶 {CODE_2}、第 3 顶 {CODE_3} 没有能对的图，微信里确认不了：{CONSOLE}")
    assert pdesk.admin("对过图了吗", quote=quote) == (
        f"#{number} 扣着 3 顶：第 1 顶 {CODE} 已确认；第 2 顶 {CODE_2} 还没确认（没有能对的图，要到控制台确认）；"
        f"第 3 顶 {CODE_3} 还没确认（没有能对的图，要到控制台确认）。剩下的{CONSOLE}")
    assert confirmed_codes(pdesk) == [CODE] and pdesk.executions() == []
    for code in (CODE_2, CODE_3):                                        # (the console's button: the last one previews)
        done = pdesk.prm.deal_action("photo_confirm", {"deal_id": packet["deal_id"], "packet": number, "code": code})
        assert done["ok"] and done["repreview"] is (code == CODE_3)
    assert previewing(pdesk, number)


def test_a_hat_the_side_by_side_sheet_says_to_look_at_again_is_numbered_by_its_row_there(pdesk):
    """A hat with a sure picture whose row of the side-by-side sheet says 「要再看」: numbered after the held sheet's
    rows, by its row on that sheet; one whose row has no picture of the code is the console's."""
    packet = photo_order(
        pdesk, held=(CODE_3, CODE_2, CODE), sheets=("pair", "held"),
        facts=[pair_row(CODE_3, "！要再看：颜色：图最像 深棕"), pair_row(CODE_2), held_row(CODE, 1)],
        problems=[f"{CODE_3} 对图要再看（！要再看：颜色：图最像 深棕）：先核对再建；{HOW}",
                  f"{CODE_2} 对图要再看（没有这个编码的图，请自己找图核对）：先核对再建；{HOW}", hold(CODE)])
    number, quote = packet["id"], head(pdesk, packet["id"])
    lines = notice_of(pdesk, number)
    assert lines[-4:-1] == [
        f"第 1 顶 {CODE}（同款别的颜色（她的图最像的是驼色）；「扣着的」那张对图单第 1 行）",
        f"第 2 顶 {CODE_3}（对图单说要再看：要再看：颜色：图最像 深棕；对图单第 1 行）",
        f"第 3 顶 {CODE_2}（对图单说要再看：没有这个编码的图，请自己找图核对；没有这个编码的图，要到控制台确认）"]
    assert pdesk.admin("对过图 2", quote=quote).startswith(f"#{number} 已确认第 2 顶 {CODE_3}（{UNDO}）；还扣着第 1 顶 {CODE}")
    assert pdesk.admin("对过图 全部", quote=quote) == (
        f"#{number} 已确认第 1 顶 {CODE}（{UNDO}）；第 3 顶 {CODE_2} 没有能对的图，微信里确认不了：{CONSOLE}")
    assert sorted(confirmed_codes(pdesk)) == sorted([CODE, CODE_3]) and not previewing(pdesk, number)


def test_a_held_sheet_that_never_reached_windows_leaves_its_hats_to_the_console(pdesk):
    """The preview names a row of the held hats' sheet, but only the side-by-side sheet came back (the upload was lost):
    that hat is on no sheet the admin got, so it is not numbered and 「对过图 1」 confirms nothing."""
    packet = photo_order(pdesk, held=(CODE,), sheets=("pair",), facts=[pair_row(CODE_3), held_row(CODE, 1)],
                         problems=[hold(CODE)])
    number = packet["id"]
    lines = notice_of(pdesk, number)
    assert lines[-1] == (f"有 1 顶帽子扣着（没对上图：{CODE}），微信里不能执行：请到控制台对过图，在这顶旁边点「对过图了」，"
                         "再点「重新预览」；不要的话回 2")
    assert pdesk.prm.store.wechat_review(number)["hats"] == ""
    for answer in ("对过图 1", "对过图", "对过图 全部"):
        assert pdesk.admin(answer, quote=lines[0]) == f"#{number} {lines[-1]}", answer
    assert confirmed_codes(pdesk) == []


def test_a_second_draft_of_the_deal_is_never_confirmed_previewed_or_run_by_the_firsts_answer(pdesk):
    """Two sample orders of one deal wait (her private chat and her group): each notice numbers its own hats.  A number
    of the other draft's list quoting this notice confirms nothing; 「对过图 1」 quoting the first confirms its hat for
    the deal and previews the first again, never the second: that one keeps waiting for its own 「重新预览」, runs
    nothing, and its own notice (no sheet of its own: nothing numbered) sends the admin to the console."""
    first = one_held(pdesk)
    held = {**jst_block(), "verdict": "blocked", "run": {}, "problems": [hold(CODE), hold(CODE_2, "均码粉色")]}
    second = pdesk.prm.store.add_packet(
        now=pdesk.clock.now, deal_id=first["deal_id"], party_id=1, job_id=None, chat_id=GROUP, kind="money",
        text="寄样单建好啦，单号 {寄样单号}～", attachments=[], intent="", slots={}, stage_proposal="",
        needs_attention=False, reason="", tool="jst.sample_order", platform={"action": SAMPLE, "preview": held},
        meta={"channel": "group", "group_name": "小美合作群", "draft_context": GATED, "photo_held": [CODE, CODE_2]})
    pdesk.review.sweep()
    one, two = head(pdesk, first["id"]), head(pdesk, second)
    assert pdesk.prm.store.wechat_review(second)["hats"] == ""
    assert pdesk.admin("对过图 2", quote=one) == (
        f"#{first['id']} 编号不对（这条通知里只有第 1 顶，每顶写一次）：什么都没确认。扣着的是：第 1 顶 {CODE}")
    assert pdesk.admin("对过图 1 2", quote=one).startswith(f"#{first['id']} 编号不对")
    console = (f"有 2 顶帽子扣着（没对上图：{CODE}、{CODE_2}），微信里不能执行：请到控制台对过图，在这顶旁边点「对过图了」，"
               "再点「重新预览」；不要的话回 2")
    assert pdesk.admin("对过图 1", quote=two) == f"#{second} {console}"
    assert confirmed_codes(pdesk, first["deal_id"]) == []
    assert pdesk.admin("对过图 1", quote=one) == f"#{first['id']} 已确认第 1 顶 {CODE}（{UNDO}）；{COMING}"
    assert previewing(pdesk, first["id"]) and not previewing(pdesk, second)
    assert pdesk.admin("执行", quote=two) == (
        f"#{second} 有 1 顶帽子扣着（没对上图：{CODE_2}），微信里不能执行：请到控制台对过图，在这顶旁边点「对过图了」，"
        "再点「重新预览」；不要的话回 2")
    assert pdesk.prm.decide(second, "execute")["ok"] is False
    assert pdesk.executions() == [] and pdesk.prm.store.packet(second)["status"] == "pending"
    previews = [j for j in pdesk.jobs("prm") if '"platform_preview"' in str(j.get("meta") or "")]
    assert len(previews) == 1 and f'"packet_id": {first["id"]}' in str(previews[0]["meta"])


def test_对过图_never_comes_from_her_chat_a_group_or_anybody_else(pdesk):
    """Only the admin's own one-to-one chat: the creator quoting (or typing) the notice's head in her chat, anybody in a
    group, another sender confirm nothing."""
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    pdesk.admin("对过图 1", quote=quote, sender=CREATOR, chat=CREATOR)
    pdesk.admin("对过图 全部", quote=quote, sender=ADMIN, chat=GROUP, group=True)
    pdesk.admin("对过图 1 2", quote=quote, sender=OTHER, chat=OTHER)
    pdesk.admin("对过图 1", quote=quote, sender=OTHER, chat=ADMIN)
    assert confirmed_codes(pdesk) == [] and not previewing(pdesk, number) and pdesk.executions() == []


def test_对过图_for_a_draft_no_longer_waiting_or_with_nothing_held_says_so(pdesk):
    """A sample order nothing holds, one dropped meanwhile, a plain reply's notice: nothing to confirm."""
    ready = checked_waiting(pdesk, held=(), verdict="ready")              # (checked by photo, nothing held)
    assert pdesk.admin("对过图 1", quote=head(pdesk, ready["id"])) == f"#{ready['id']} 没有扣着等确认的帽子，不用回「对过图」"
    assert pdesk.admin("对过图 1") is None                                # (unquoted, nothing held: not the review's)
    assert pdesk.admin("2", quote=head(pdesk, ready["id"])).startswith(f"#{ready['id']} 不发了")
    pdesk.clock.now += 60
    packet = one_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert pdesk.prm.decide(number, "drop")["ok"]
    assert pdesk.admin("对过图 1", quote=quote) == f"#{number} 已经处理过了：点了不发"
    assert confirmed_codes(pdesk) == []
    pdesk.clock.now += 60
    plain = waiting(pdesk)                                               # (a reply with no platform action)
    asked = pdesk.admin("对过图了，颜色没问题的", quote=head(pdesk, plain["id"]))
    assert asked.startswith(f"#{plain['id']} 改成：『对过图了，颜色没问题的』")       # (her reply's words, as ever)


def test_with_the_photo_switch_off_对过图_confirms_nothing(clock, tmp_path):
    """PRM_DRAFT_CONTEXT off (the console has no 「对过图了」 then): a held sample order's notice numbers nothing and the
    answer only hears why it does not run."""
    desk = Desk(clock, tmp_path)
    desk.prm.register(CREATOR, "小美", "测试达人")
    try:
        packet = one_held(desk)
        number = packet["id"]
        lines = notice_of(desk, number)
        assert desk.prm.store.wechat_review(number)["hats"] == "" and not any("对过图 1" in line for line in lines)
        assert desk.admin("对过图 1", quote=lines[0]) == (
            f"#{number} {CODE} 还扣着（没对过图），不能执行：点「重新预览」再核；微信里不能执行；不要的话回 2")
        assert confirmed_codes(desk) == [] and not previewing(desk, number)
    finally:
        desk.stop()


def test_a_new_preview_that_still_holds_a_hat_is_told_again_with_its_own_numbers(pdesk):
    """The console's 「重新预览」 (no confirmation) of a told draft: the new preview, whatever it brought, is told in a
    fresh notice with its own sheets and numbers (here it holds only the second hat now, as its 「第 1 顶」); the old
    notice's numbers mean nothing any more, the fresh one's are confirmed once its sheet came."""
    packet = two_held(pdesk)
    number, first = packet["id"], head(pdesk, packet["id"])
    assert pdesk.prm.decide(number, "repreview")["ok"]
    job = previewed_again(pdesk, number, held=(CODE_2,), sheets=("pair", "held"), verdict="blocked",
                          facts=[pair_row(CODE_3), pair_row(CODE), held_row(CODE_2, 1, why="颜色没把握")],
                          problems=[hold(CODE_2, "均码粉色")])
    assert pdesk.admin("对过图 2", quote=first) == f"#{number} {STALE}（新的通知马上发过来）"
    assert pdesk.review.sweep() == 1
    lines = latest_notice(pdesk, number)
    assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
    assert lines[-3:-1] == ["有 1 顶帽子扣着（没对上图），对上了才能建这张寄样单：",
                            f"第 1 顶 {CODE_2}（颜色没把握；「扣着的」那张对图单第 1 行）"]
    assert [path.rsplit("/", 1)[-1] for chat, path in pdesk.files[-2:]] == [
        f"对图单-{job['id']}.jpg", f"对图单-{job['id']}-扣着的.jpg"]
    assert pdesk.admin("对过图 2", quote=first) == f"#{number} {STALE}"
    assert pdesk.admin("对过图 1", quote=first) == f"#{number} {STALE}"
    assert confirmed_codes(pdesk) == []
    assert pdesk.admin("对过图", quote=lines[0]) == f"#{number} 已确认第 1 顶 {CODE_2}（{UNDO}）；{COMING}"
    assert confirmed_codes(pdesk) == [CODE_2] and pdesk.executions() == []


def test_a_new_preview_that_was_not_made_is_told_too_and_runs_nothing(pdesk):
    """The bot's own new preview after the last 「对过图」 fails on the Mac: the fresh notice says so (the preview's
    problem) and sends the admin to the console; nothing runs from WeChat."""
    packet = one_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    assert pdesk.admin("对过图", quote=quote) == f"#{number} 已确认第 1 顶 {CODE}（{UNDO}）；{COMING}"
    job = pdesk.relay.next_job(0, ("prm",))
    pdesk.relay.finish(job["id"], {"ok": False, "text": "", "error": "聚水潭没登录", "duration_s": 1})
    pdesk.relay.tick()
    assert not previewing(pdesk, number)
    assert pdesk.review.sweep() == 1
    lines = latest_notice(pdesk, number)
    assert lines[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊"
    assert any("重新预览没做成" in line for line in lines)
    assert lines[-1] == "扣着的帽子都点过「对过图了」，微信里不能执行：请到控制台点「重新预览」按确认再核；不要的话回 2"
    assert pdesk.admin("执行", quote=lines[0]) == f"#{number} {lines[-1]}"
    assert pdesk.executions() == [] and pdesk.prm.store.packet(number)["status"] == "pending"


def test_the_new_texts_show_her_ids_by_their_last_characters(pdesk):
    """A held hat's reason that names her UID (the Mac's words), and a refusal that does: only the last characters reach
    the admin's chat, in the notice and in every answer."""
    packet = photo_order(pdesk, held=(CODE,), facts=[held_row(CODE, 1, why=f"UID {DY_UID} 的图没把握")], problems=[hold(CODE)])
    number, quote = packet["id"], head(pdesk, packet["id"])
    text = "\n".join(notice_of(pdesk, number))
    assert DY_UID not in text and f"第 1 顶 {CODE}（UID …{DY_UID[-4:]} 的图没把握；" in text
    pdesk.review._confirm = lambda deal_id, packet_id, code: {"ok": False, "error": f"UID {DY_UID} 对不上"}
    reply = pdesk.admin("对过图", quote=quote)
    assert reply == f"#{number} 第 1 顶 {CODE} 没确认成：UID …{DY_UID[-4:]} 对不上" and DY_UID not in reply
    assert all(DY_UID not in t for t in pdesk.told())


def test_an_older_database_gets_the_two_columns_and_its_rows_number_no_hat(pdesk):
    """A row told before the hats were numbered (``hats`` and ``shown`` empty): 「对过图 1」 quoting its notice confirms
    nothing (the console's), and the draft as it is now counts as shown (no fresh notice for it)."""
    packet = two_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    with pdesk.prm.store._lock:
        pdesk.prm.store._conn.execute("UPDATE wechat_reviews SET hats = '', shown = ''")
        pdesk.prm.store._conn.commit()
    assert pdesk.review.sweep() == 0 and pdesk.prm.store.wechat_review(number)["shown"]
    assert pdesk.admin("对过图 1", quote=quote) == (
        f"#{number} 有 2 顶帽子扣着（没对上图：{CODE}、{CODE_2}），微信里不能执行：请到控制台对过图，在这顶旁边点「对过图了」，"
        "再点「重新预览」；不要的话回 2")
    assert confirmed_codes(pdesk) == []


def test_a_new_preview_that_cannot_be_queued_is_said_so_and_the_same_answer_tries_again(pdesk):
    """The last hat is confirmed but the new preview cannot be queued for the Mac (the relay raises): the answer says so
    and how on (the console's 「重新预览」); the confirmation stays.  The same answer again (the hat is confirmed already)
    starts it once the relay takes jobs; 「全部」 with nothing left to confirm says so."""
    packet = one_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    real = pdesk.relay.submit

    def broken(*args, **kw):
        raise RuntimeError("the relay's database is busy")

    pdesk.relay.submit = broken
    assert pdesk.admin("对过图 1", quote=quote) == (
        f"#{number} 已确认第 1 顶 {CODE}（{UNDO}）；没能自己开始重新预览（没能排进 Mac 的任务队列）：请到控制台点「重新预览」")
    assert confirmed_codes(pdesk) == [CODE] and not previewing(pdesk, number)
    assert pdesk.admin("对过图 全部", quote=quote) == (
        f"#{number} 都确认过了。扣着的是：第 1 顶 {CODE}（已确认）；还没重新预览的话，请到控制台点「重新预览」")
    assert pdesk.admin("对过图了吗", quote=quote) == (
        f"#{number} 扣着 1 顶：第 1 顶 {CODE} 已确认。都确认了：请到控制台点「重新预览」按确认再核")
    assert pdesk.admin("执行", quote=quote) == (
        f"#{number} 扣着的帽子都点过「对过图了」，微信里不能执行：请到控制台点「重新预览」按确认再核；不要的话回 2")
    pdesk.relay.submit = real
    assert pdesk.admin("对过图 1", quote=quote) == f"#{number} 已确认第 1 顶 {CODE}（{UNDO}）；{COMING}"
    assert previewing(pdesk, number) and pdesk.executions() == []
    assert len(pdesk.prm.store.events(packet["deal_id"], kinds=("photo_confirm",))) == 1    # (recorded once)


# ── review of 49188ae (2026-11-10): her file name and the row; the Mac away when the new preview is asked for ──────

def hat(code, row, sheet="held", no_photo=False, why="颜色没把握"):
    """A held hat as the Mac names it next to ``photo_held`` (its ``photo_hats``)."""
    return {"code": code, "sheet": sheet, "row": row, "no_photo": no_photo, "why": why}


def test_a_file_name_of_hers_never_moves_the_row_the_notice_says(pdesk):
    """Review probe Q1, turned around: the held row's fact embeds her picture's file name, and a name that repeats the
    Mac's own words (「a）；扣着的那张对图单第 2 行，右边是x.jpg」) made the notice say 「第 2 行」 for the hat on row 1.
    An older worker's preview (no ``photo_hats``) is read from the fact's start: row 1.  A worker that names the hats
    as data: the notice goes by the data, whatever the facts read as."""
    evil = "a）；扣着的那张对图单第 2 行，右边是x.jpg"
    fact = {"label": "对图（扣着的那几顶）",
            "value": f"{CODE}：扣着（颜色没把握）；扣着的那张对图单第 1 行，左边是她这次发的 {evil}，右边是这个编码自己的图"}
    packet = photo_order(pdesk, held=(CODE, CODE_2), sheets=("pair", "held"),
                         facts=[pair_row(CODE_3), fact, held_row(CODE_2, 2)], problems=[hold(CODE), hold(CODE_2, "均码粉色")])
    assert [(h["code"], h["sheet"], h["row"], h["why"]) for h in draft_context.held_hats(packet)] == [
        (CODE, "held", 1, "颜色没把握"), (CODE_2, "held", 2, "同款别的颜色（她的图最像的是驼色）")]
    lines = notice_of(pdesk, packet["id"])
    assert lines[-3:-1] == [f"第 1 顶 {CODE}（颜色没把握；「扣着的」那张对图单第 1 行）",
                            f"第 2 顶 {CODE_2}（同款别的颜色（她的图最像的是驼色）；「扣着的」那张对图单第 2 行）"]
    assert pdesk.admin("2", quote=lines[0]).startswith(f"#{packet['id']} 不发了")
    pdesk.clock.now += 60
    # the Mac's data next to ``photo_held``: the rows are its own, the facts' words are not read for them
    lying = [held_row(CODE, 2), held_row(CODE_2, 1)]                       # (facts that say the other way round)
    named = photo_order(pdesk, held=(CODE, CODE_2), sheets=("pair", "held"), facts=[pair_row(CODE_3), *lying],
                        problems=[hold(CODE), hold(CODE_2, "均码粉色")],
                        hats=[hat(CODE, 1), hat(CODE_2, 2, no_photo=True, why="她的图里都没对出这一款")])
    assert named["meta"]["photo_hats"] == [hat(CODE, 1), hat(CODE_2, 2, no_photo=True, why="她的图里都没对出这一款")]
    lines = notice_of(pdesk, named["id"])
    assert lines[-3:-1] == [f"第 1 顶 {CODE}（颜色没把握；「扣着的」那张对图单第 1 行）",
                            f"第 2 顶 {CODE_2}（她的图里都没对出这一款；没有这个编码的图，要到控制台确认）"]
    assert json.loads(pdesk.prm.store.wechat_review(named["id"])["hats"]) == [
        {"code": CODE, "ok": True}, {"code": CODE_2, "ok": False}]
    assert pdesk.admin("对过图 全部", quote=lines[0]) == (
        f"#{named['id']} 已确认第 1 顶 {CODE}（{UNDO}）；第 2 顶 {CODE_2} 没有能对的图，微信里确认不了：{CONSOLE}")


def test_a_new_previews_hats_replace_the_ones_before_and_an_older_workers_fall_back_to_its_words(pdesk):
    """The new preview's ``photo_hats`` are written with it (one step); a new preview of a worker without them clears
    the older ones, and the bot reads that preview's facts."""
    packet = photo_order(pdesk, held=(CODE,), facts=[held_row(CODE, 1)], problems=[hold(CODE)], hats=[hat(CODE, 1)])
    number = packet["id"]
    assert pdesk.prm.decide(number, "repreview")["ok"]
    job = pdesk.relay.next_job(0, ("prm",))
    body = {**jst_block(), "verdict": "blocked", "facts": [held_row(CODE_2, 1)], "problems": [hold(CODE_2)], "run": {},
            "draft_context": GATED, "photo_held": [CODE_2], "photo_hats": [hat(CODE_2, 1, why="同款别的颜色")],
            "operator_files": sheets_of(pdesk, job["id"], ("held",))}
    pdesk.relay.finish(job["id"], {"ok": True, "text": json.dumps(body, ensure_ascii=False), "duration_s": 3})
    pdesk.relay.tick()
    assert pdesk.prm.store.packet(number)["meta"]["photo_hats"] == [hat(CODE_2, 1, why="同款别的颜色")]
    pdesk.review.sweep()
    assert latest_notice(pdesk, number)[-2] == f"第 1 顶 {CODE_2}（同款别的颜色；「扣着的」那张对图单第 1 行）"
    assert pdesk.prm.decide(number, "repreview")["ok"]
    previewed_again(pdesk, number, held=(CODE,), facts=[held_row(CODE, 1, why="颜色没比过")], problems=[hold(CODE)],
                    sheets=("held",), verdict="blocked")                  # (an older worker: no ``photo_hats``)
    assert pdesk.prm.store.packet(number)["meta"]["photo_hats"] is None
    pdesk.review.sweep()
    assert latest_notice(pdesk, number)[-2] == f"第 1 顶 {CODE}（颜色没比过；「扣着的」那张对图单第 1 行）"


def test_the_mac_away_when_the_last_hat_is_confirmed_is_said_and_a_long_wait_is_told_once(pdesk):
    """Review 2026-11-10: the last held hat confirmed while no worker on the Mac was heard from lately: the new preview
    waits in the queue, and the answer says so instead of 「正在重新预览」 alone.  Ten minutes without a result: one
    line, once for that preview (kept in the row: a restart does not say it again).  The Mac back: the new preview is
    told as ever.  A later 重新预览 that waits as long is said once more."""
    packet = one_held(pdesk)
    number, quote = packet["id"], head(pdesk, packet["id"])
    pdesk.clock.now += 200                                               # (no worker heard from for 180 s)
    assert pdesk.admin("对过图", quote=quote) == (
        f"#{number} 已确认第 1 顶 {CODE}（{UNDO}）；Mac 现在不在线，重新预览要等它上线，好了再发一条通知")
    assert confirmed_codes(pdesk) == [CODE] and previewing(pdesk, number) and pdesk.executions() == []
    assert pdesk.admin("执行", quote=quote) == f"#{number} {PREVIEWING_STOP}"
    waited = f"#{number} 重新预览等了 10 分钟还没结果（Mac 可能不在线）：好了会再发通知；急的话到控制台看"
    pdesk.clock.now += 599
    assert pdesk.review.sweep() == 0 and waited not in pdesk.told()
    pdesk.clock.now += 2
    assert pdesk.review.sweep() == 1 and pdesk.told()[-1] == waited
    key = pdesk.prm.store.packet(number)["platform"]["preview_key"]
    assert pdesk.prm.store.wechat_review(number)["waited"] == key
    for _ in range(3):                                                   # (once)
        pdesk.clock.now += 600
        assert pdesk.review.sweep() == 0
    assert pdesk.told().count(waited) == 1
    job = previewed_again(pdesk, number, used=(CODE,))                   # (the Mac is back)
    assert pdesk.review.sweep() == 1
    fresh = latest_notice(pdesk, number)
    assert fresh[0] == f"#{number} 草稿（第 2 次通知） · 小美 · 私聊" and pdesk.files[-1][1].endswith(f"对图单-{job['id']}.jpg")
    assert pdesk.prm.decide(number, "repreview")["ok"]                   # (the console's, later: its own wait)
    pdesk.clock.now += 601
    assert pdesk.review.sweep() == 1 and pdesk.told().count(waited) == 2
    pdesk.clock.now += 601
    assert pdesk.review.sweep() == 0


def test_a_long_wait_line_wechat_does_not_take_is_tried_again_and_a_plain_draft_never_gets_one(pdesk):
    """The line about a new preview that takes long is sent like any other (tried again when WeChat does not take it,
    marked told only once it went); a draft that is not being previewed again, a decided one, gets none."""
    plain = waiting(pdesk)                                               # (a reply: no platform action)
    pdesk.clock.now += 60
    packet = one_held(pdesk)
    number = packet["id"]
    assert pdesk.prm.decide(number, "repreview")["ok"]
    pdesk.clock.now += 601
    pdesk.refuse.add(ADMIN)
    assert pdesk.review.sweep() == 0 and pdesk.prm.store.wechat_review(number)["waited"] == ""
    pdesk.refuse.clear()
    pdesk.clock.now += RETRY_SEC[0] + 1
    assert pdesk.review.sweep() == 1
    assert pdesk.told()[-1].startswith(f"#{number} 重新预览等了 10 分钟还没结果")
    assert not any(t.startswith(f"#{plain['id']} 重新预览") for t in pdesk.told())
    assert pdesk.prm.decide(number, "drop")["ok"]
    pdesk.clock.now += 3600
    assert pdesk.review.sweep() == 0
