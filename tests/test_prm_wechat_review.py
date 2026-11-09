"""微信里审批草稿 (PRM_WECHAT_REVIEW, 2026-11-09, src/prm/wechat_review.py): the drafts waiting in 待审批 are told, once
each, to the admin's private chat, and the admin decides them there (quoting a notification: 1 / 2 / 改 …, or 「发 N」
「不发 N」「改 N …」, a bare 1 / 2 while one draft waits).  The same decision as the console's; nothing of it with the
switch off.

The relay, the PRM service and the router as Bot.run wires them (``Desk``), with a fake WeChat that records every
message the bot sends.  Names, numbers and addresses are made up.
"""

import itertools
import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from router_support import BOT, TraceSpy
from test_prm import ADMIN, CREATOR, Clock, config

from src.admin import RELAY_HELP, AdminCommandHandler
from src.db import MessageStore, initialize_db
from src.prm import deals, gates
from src.prm.service import SETTLE_SEC, PrmService
from src.prm.store import BUYIN_TOOL, PrmStore
from src.prm.wechat_review import (NO_ADMIN_WHY, RETRY_SEC, SEND_TRIES, VOICE_TEXT, parse)
from src.relay.service import MAX_AGE_SEC, ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter

OTHER = "wxid_creator02"            # a second creator (小雨)
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
    order (the PRM's sends and the route's replies).  A send to a chat in ``refuse`` fails."""

    def __init__(self, clock, tmp_path, *, review="on", relay_only="off", admin=ADMIN, store=None, relay_store=None,
                 **prm):
        self.clock = clock
        self.sent: list = []
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
                              send_file=lambda chat, path: True)
        self.router._prm = self.prm
        self.review = self.prm.wechat_review

    def _send(self, chat, text):
        self.sent.append((chat, text))
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
        """The admin writes in the admin's chat (``quote``: the text of the message quoted); the route's reply."""
        msg = {"message_id": f"a{next(IDS)}", "chat_id": chat, "group_name": "管理员", "sender_id": sender,
               "sender_name": "管理员", "content": content, "msg_type": msg_type or (QUOTE if quote else 1),
               "timestamp": self.clock.now if at is None else at, "is_group": group, "quoted_content": quote or "",
               **extra}
        return self.router.handle(msg)

    def told(self) -> list[str]:
        """What the PRM sent the admin (the notifications, a failed send told)."""
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
    desk.prm.store.add_packet(deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="a",
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
    assert desk.review.status() == {"on": False}
    assert desk.admin(f"发 {packet['id']}") is None                      # (silence, as before)
    assert desk.admin("1", quote=f"#{packet['id']} 草稿 · 小美 · 私聊") is None
    assert desk.router._tracer.notes["ended_by"] == "silence"
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.prm.store.wechat_reviews_open() == [] and desk.prm.store.latest_wechat_review() is None
    desk.stop()


def test_without_an_admin_nothing_is_told_and_the_console_says_why(clock, tmp_path):
    desk = Desk(clock, tmp_path, admin="")
    desk.prm.register(CREATOR, "小美", "")
    desk.her("收到6件哈")
    packet = desk.mac()
    assert desk.review.sweep() == 0 and desk.sent == []
    assert desk.review.status() == {"on": True, "why": NO_ADMIN_WHY, "open": 0, "unsent": 0}
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    desk.stop()


def test_on_the_macs_edge_side_it_is_off_and_says_so(clock, tmp_path):
    desk = Desk(clock, tmp_path, wechat_backend="edge")
    assert desk.review.on is False and "WECHAT_BACKEND=edge" in desk.review.status()["why"]
    desk.stop()


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
    desk.prm.store.add_packet(deal_id=1, party_id=1, job_id=None, chat_id="buyin:conv1", kind="other", text="您好～",
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
        deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="greeting", text="您好～我是品牌测试的运营",
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
        meta={"opening": True, "auto_at": clock.now + 1800})
    assert desk.review.sweep() == 0 and desk.told() == []
    desk.prm.store.hold_for_review(1, "她撤回了一条消息")                   # (only the Mac's drafts: none here)
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


def test_a_group_draft_says_it_goes_into_the_boss_group(desk):
    number = desk.prm.store.add_packet(
        deal_id=1, party_id=1, job_id=None, chat_id=GROUP, kind="other", text="在的～群里也可以直接问我",
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
        meta={"channel": "group", "group_name": "小美合作群"})
    desk.review.sweep()
    lines = desk.told()[0].split("\n")
    assert lines[0] == f"#{number} 草稿 · 小美 · 群里「小美合作群」"
    assert lines[1] == "（发在老板拉的群里，不是她的私聊：老板和群里的人都看得到）"
    assert "回 1 发到群里｜2 不发｜改 + 你的话（直接发你改的）" in lines


def test_a_shipping_notice_shows_its_waybill(desk):
    _, number = deals.add_shipment(desk.prm.store, 1, "中通快递", "ZT1234567890", now=desk.clock.now, review=True)
    desk.review.sweep()
    text = desk.told()[0]
    assert text.startswith(f"#{number} 草稿 · 小美 · 私聊") and "她：" not in text
    assert "运单：中通快递 ZT1234567890（发出的内容里必须是这个单号）" in text
    assert "草稿：老师您好～样品已经寄出啦：中通快递 ZT1234567890。签收之后跟我说一声哦～" in text


def test_a_draft_with_files_names_them(desk):
    number = desk.prm.store.add_packet(
        deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="material", text="",
        attachments=[{"name": "货盘.jpg", "path": "/nowhere/货盘.jpg", "size": 10}], intent="", slots={},
        stage_proposal="", needs_attention=False, reason="")
    desk.review.sweep()
    text = desk.told()[0]
    assert f"#{number} 草稿" in text and "草稿：（只发附件，没有文字）" in text and "（附 1 个文件：货盘.jpg）" in text


def platform_packet(desk, verdict="ready") -> int:
    return desk.prm.store.add_packet(
        deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="寄样单建好啦～",
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
    for answer in ("1", "改 好的"):
        assert desk.admin(answer, quote=head(desk, number)) == (
            f"#{number} 带平台动作：微信里不能执行，请在控制台点「批准并执行」；不要的话回 2")
    assert desk.admin(f"发 {number}").startswith(f"#{number} 带平台动作")
    assert desk.admin("就这样吧", quote=head(desk, number)).startswith(f"#{number} 带平台动作")
    assert desk.prm.store.packet(number)["status"] == "pending" and desk.jobs("prm") == []
    assert desk.admin("2", quote=head(desk, number)) == f"#{number} 不发了（她再发消息时会有新的草稿）"
    assert desk.prm.store.packet(number)["status"] == "dropped"


def test_a_platform_draft_whose_preview_failed_says_so(desk):
    platform_packet(desk, verdict="login")
    desk.review.sweep()
    assert "；预览没通过（" in desk.told()[0]


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
    assert desk.prm.store.wechat_review(packet["id"]) is None
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
    assert len(desk.told()) == 2 and desk.prm.store.wechat_review(packet["id"]) is not None
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
    desk.prm.store.add_packet(deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="a",
                              attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="")
    desk.prm.store.add_packet(deal_id=1, party_id=1, job_id=None, chat_id=CREATOR, kind="other", text="b",
                              attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="")
    desk.refuse.add(ADMIN)
    assert desk.review.sweep() == 0 and len(desk.told()) == 1           # one try, then the round ends
    assert desk.review.sweep() == 0 and len(desk.told()) == 1           # the chat waits (10 s)
    desk.refuse.clear()
    desk.clock.now += 10
    assert desk.review.sweep() == 3 and len(desk.told()) == 4           # PER_ROUND
    assert desk.review.sweep() == 1 and len(desk.prm.store.wechat_reviews_open()) == 4



# ── the admin's answers ─────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["1", "发", "发送", "好", "好的", "可以", "行", "ok", "OK", "１", "好的！"])
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
    assert desk.prm.store.packet(number)["status"] == "pending"
    assert desk.admin("改 收到啦～\n明天给您开佣金哈", quote=head(desk, number)) == f"#{number} 按你改的批准了，正在发给 小美"
    packet = desk.prm.store.packet(number)
    assert (packet["status"], packet["final_text"], packet["edited"]) == ("approved", "收到啦～\n明天给您开佣金哈", True)
    assert desk.prm.store.wechat_review(number)["decision"] == "edit"


def test_改_alone_asks_for_the_words(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("改", quote=head(desk, number)) == f"#{number} 要改成什么？引用通知回「改 你的话」，或者回「改 {number} 你的话」"
    assert desk.admin(f"改 {number}") == f"#{number} 要改成什么？回「改 {number} 你的话」"
    assert desk.prm.store.packet(number)["status"] == "pending"


def test_a_shipping_notice_edited_in_wechat_keeps_the_systems_waybill(desk):
    _, number = deals.add_shipment(desk.prm.store, 1, "中通快递", "ZT1234567890", now=desk.clock.now, review=True)
    desk.review.sweep()
    reply = desk.admin("改 样品寄出啦：中通快递 ZT0000000000", quote=head(desk, number))
    assert reply == f"#{number} 没发：发货通知里的单号和系统记录对不上（系统记录：ZT1234567890）"
    assert desk.admin("改 老师，样品寄出啦：中通快递 ZT1234567890", quote=head(desk, number)).endswith("正在发给 小美")


def test_other_words_quoting_a_notification_are_kept_as_its_proposed_edit_until_1(desk):
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


@pytest.mark.parametrize("how", ["quoting the notification", "bare"])
def test_a_proposed_edit_is_sent_by_a_1_quoting_the_notification_or_a_bare_1(desk, how):
    packet = waiting(desk)
    number = packet["id"]
    desk.admin("明天给您寄～", quote=head(desk, number))
    reply = desk.admin("1", quote=head(desk, number) if how != "bare" else None)
    assert reply == f"#{number} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "明天给您寄～"


def test_a_proposed_edit_is_checked_when_it_is_made(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("电话〈手机2〉对吗", quote=head(desk, number)) == f"#{number} 不能这样改：草稿里有对不上的占位符：〈手机2〉"
    assert desk.prm.store.wechat_review(number)["state"] == "notified"


def test_a_2_after_a_proposed_edit_drops_the_draft(desk):
    packet = waiting(desk)
    desk.admin("明天给您寄～", quote=head(desk, packet["id"]))
    assert desk.admin("2").endswith("不发了（她再发消息时会有新的草稿）")
    assert desk.prm.store.packet(packet["id"])["status"] == "dropped"


def test_without_a_quote_the_number_says_which_draft(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")
    third = waiting(desk, "那我等你", chat=CREATOR, text="好的～")       # (#1 out of date: her new line)
    assert desk.admin(f"发 {second['id']}") == f"#{second['id']} 已批准，正在发给 小雨"
    assert desk.admin(f"改 {third['id']} 好的老师～明天见") == f"#{third['id']} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(third["id"])["final_text"] == "好的老师～明天见"
    assert desk.admin(f"不发 {first['id']}") == f"#{first['id']} 已过时：她又发了新消息，新的草稿 #{third['id']} 已经处理过了"
    assert desk.admin("发 99") == "没有找到草稿 #99"
    assert desk.jobs() == []


def test_a_burst_of_numbered_answers_is_each_answered(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")
    reply = desk.admin(f"发 {first['id']}\n不发 {second['id']}\n谢谢")         # (three bubbles, one burst)
    assert reply.split("\n") == [f"#{first['id']} 已批准，正在发给 小美", f"#{second['id']} 不发了（她再发消息时会有新的草稿）",
                                 "其余的话没有当成回复：要改草稿请回「改 编号 你的话」"]


def test_a_numbered_edit_of_a_number_never_told_goes_on_as_before(desk):
    """「改 3 处错别字」 with no notification #3: not a draft's answer (a cc task in relay-only mode, say)."""
    waiting(desk)
    assert desk.admin("改 3 处错别字") is None and desk.router._tracer.notes["ended_by"] == "silence"


def test_a_numbered_edit_quoting_a_notification_is_for_the_draft_it_quotes(desk):
    packet = waiting(desk)
    number = packet["id"]
    assert desk.admin("改 3 件都收到了吗", quote=head(desk, number)) == f"#{number} 按你改的批准了，正在发给 小美"
    assert desk.prm.store.packet(number)["final_text"] == "3 件都收到了吗"


def test_a_bare_1_or_2_works_only_while_exactly_one_told_draft_waits(desk):
    desk.prm.register(OTHER, "小雨", "")
    first = waiting(desk)
    second = waiting(desk, "多少佣金呀", chat=OTHER, text="25% 哦～")
    assert desk.admin("1") == (f"现在有 2 份在等你回：#{first['id']} 小美、#{second['id']} 小雨；回「发 {first['id']}」"
                               "这样带编号，或者引用那条通知回 1")
    assert {p["status"] for p in desk.prm.store.packets(("pending", "approved"))} == {"pending"}
    desk.admin(f"不发 {first['id']}")
    assert desk.admin("1") == f"#{second['id']} 已批准，正在发给 小雨"
    # nothing open: about the last one, while it is recent; long after, a bare 1 is no answer at all (as before)
    assert desk.admin("2") == f"#{second['id']} 已经处理过了：你在微信里回过了，已经批准，正在发"
    desk.clock.now += 3601
    assert desk.admin("1") is None and desk.router._tracer.notes["ended_by"] == "silence"


def test_a_bare_answer_never_takes_a_draft_told_after_it_was_written(desk):
    packet = waiting(desk)
    assert desk.admin("1", at=desk.clock.now - 30) == (
        f"现在有 1 份在等你回：#{packet['id']} 小美；回「发 {packet['id']}」这样带编号，或者引用那条通知回 1")
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"


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


def test_a_group_draft_answered_1_is_confirmed_for_the_group(desk):
    number = desk.prm.store.add_packet(
        deal_id=1, party_id=1, job_id=None, chat_id=GROUP, kind="other", text="在的～",
        attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
        meta={"channel": "group", "group_name": "小美合作群"})
    desk.review.sweep()
    assert desk.admin("1", quote=head(desk, number)) == f"#{number} 已批准，正在发给 小美（群里「小美合作群」）"
    assert desk.prm.store.packet(number)["status"] == "approved"


def test_an_answer_read_late_is_history_and_gets_no_reply(desk):
    packet = waiting(desk)
    assert desk.admin("1", quote=head(desk, packet["id"]), at=desk.clock.now - MAX_AGE_SEC - 60) is None
    assert desk.router._tracer.notes["ended_by"] == "wechat_review"
    assert desk.prm.store.packet(packet["id"])["status"] == "pending" and desk.jobs() == []


def test_a_voice_answer_never_counts(desk):
    packet = waiting(desk)
    for said in (f"[语音] 发 {packet['id']}。", "[语音] 1"):
        assert desk.admin(said, msg_type=34) == VOICE_TEXT
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.admin("[语音] 帮我看看今天的订单", msg_type=34) is None      # (no answer: as before)


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
    assert desk.admin("好", quote="这是新款的色卡") is None
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
    assert desk.admin(f"发 {packet['id']}", quote="这是新款的色卡") == f"#{packet['id']} 已批准，正在发给 小美"


def test_a_number_never_told_says_so(desk):
    number = desk.prm.store.add_packet(deal_id=1, party_id=1, job_id=None, chat_id="buyin:conv1", kind="other",
                                       text="您好～", attachments=[], intent="", slots={}, stage_proposal="",
                                       needs_attention=False, reason="", tool=BUYIN_TOOL, meta={"channel": "buyin"})
    assert desk.admin(f"发 {number}") == f"#{number} 没有发到微信里审批（只有发到这里的草稿能在微信里回）：请在控制台处理"
    assert desk.prm.store.packet(number)["status"] == "pending"


def test_a_voice_message_is_taken_only_when_its_words_typed_would_be(desk):
    packet = waiting(desk)
    assert desk.admin("[语音] 改 3 处错别字", msg_type=34) is None        # (no notification #3: as before)
    assert desk.admin("[语音] 1", msg_type=34) == VOICE_TEXT             # (one open: a 1 would be its answer)
    desk.prm.decide(packet["id"], "drop")
    desk.clock.now += 3601
    assert desk.admin("[语音] 1", msg_type=34) is None                  # (nothing open, nothing told lately)


def test_the_parse_reads_only_typed_answers():
    assert parse({"msg_type": 1, "content": "发12"}).lines == (("send", 12),)
    assert parse({"msg_type": 1, "content": "发 12个样品"}) is None
    assert parse({"msg_type": 1, "content": "改12个地方"}) is None
    assert parse({"msg_type": 1, "content": "改天再说"}) is None
    assert parse({"msg_type": 1, "content": "改 12：好的"}).text == "好的"
    assert parse({"msg_type": 3, "content": "[图片]"}) is None
    assert parse({"msg_type": 49, "content": "我的主页"}) is None                  # a link: no quote
    quoted = parse({"msg_type": 49, "content": "改天再说", "quoted_content": "#12 草稿 · 小美 · 私聊\n她：…"})
    assert (quoted.kind, quoted.number, quoted.text) == ("quote", 12, "改天再说")
    assert parse({"msg_type": 49, "content": "1", "quoted_content": "wxid_x:\n#7 改成：『…』"}).number == 7


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
    for content, quote in (("好的", head(desk, second["id"])), (f"改 {second['id']} 好的老师", None),
                           (f"不发 {second['id']}", None), ("查状态", head(desk, second["id"]))):
        desk.admin(content, quote=quote)
    assert len(desk.jobs()) == 1                                         # only the cc task: nothing else went to the Mac
    assert desk.relay.store.get_ask(ask["ask_id"])["status"] == "pending"
    # the controls: every other line of the admin works as before
    assert desk.admin("好").startswith(f"好，已批准 #{job['id']}")          # (the relay's question, plain 好)
    assert desk.admin("查状态").startswith("账号状态")
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
    assert desk.admin(f"发 {number}", sender="wxid_helper", chat="wxid_helper") is None    # somebody else
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


# ── the log, the clean-up, the switch ─────────────────────────────────────

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
