"""微信里审批草稿 (PRM_WECHAT_REVIEW, 2026-11-09, src/prm/wechat_review.py): the drafts waiting in 待审批 are told, once
each, to the admin's private chat; nothing of it with the switch off.

The relay and the PRM service as Bot.run wires them (``Desk``), with a fake WeChat that records every message the bot
sends.  Names, numbers and addresses are made up.
"""

import itertools
import json
import logging
from unittest.mock import patch

import pytest

from test_prm import ADMIN, CREATOR, Clock, config

from src.prm import deals, gates
from src.prm.service import SETTLE_SEC, PrmService
from src.prm.store import BUYIN_TOOL, PrmStore
from src.prm.wechat_review import NO_ADMIN_WHY, RETRY_SEC, SEND_TRIES
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

OTHER = "wxid_creator02"            # a second creator (小雨)
GROUP = "200000009@chatroom"        # a 老板拉的群 (its name is made up)
IDS = itertools.count(1)


def reply_draft(text="收到啦～戴着还合适吗？", **extra):
    values = {"intent": "收到样品", "slots": {}, "stage_proposal": "", "message_kind": "greeting", "reply_draft": text,
              "attachments_to_send": [], "needs_attention": False, "reason": "kb/合作政策.md"}
    values.update(extra)
    return values


class Desk:
    """The relay and the PRM service as Bot.run wires them; ``sent``: every message the bot sent, in order.  A send to a
    chat in ``refuse`` fails."""

    def __init__(self, clock, tmp_path, *, review="on", admin=ADMIN, store=None, relay_store=None, **prm):
        self.clock = clock
        self.sent: list = []
        self.refuse: set = set()
        cfg = config(claude_relay_wxids=[], admin_wxid=admin, prm_wechat_review=review, **prm)
        self.relay = ClaudeRelay(cfg, store=relay_store or RelayStore(":memory:"), clock=clock,
                                 files_dir=tmp_path / "relay", send=self._send)
        self.prm = PrmService(cfg, self.relay, store=store or PrmStore(":memory:"), clock=clock, send=self._send,
                              send_file=lambda chat, path: True)
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

    def told(self) -> list[str]:
        """What the PRM sent the admin (the notifications)."""
        return [text for chat, text in self.sent if chat == ADMIN]

    def to(self, chat=CREATOR) -> list[str]:
        return [text for c, text in self.sent if c == chat]


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


def test_with_the_switch_off_nothing_is_told(clock, tmp_path):
    desk = Desk(clock, tmp_path, review="off")
    desk.prm.register(CREATOR, "小美", "")
    desk.her("收到6件哈")
    packet = desk.mac()
    desk.prm.tick()
    assert desk.told() == [] and desk.prm.store.wechat_review(packet["id"]) is None
    assert desk.review.status() == {"on": False} and desk.prm.store.wechat_reviews_open() == []
    assert desk.prm.store.packet(packet["id"])["status"] == "pending"
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
    assert text.startswith(f"#{number} 草稿 · 小美 · 私聊") and "平台动作：聚水潭 · 达人寄样单（测试款贝雷帽）" in text
    assert text.endswith("这份带平台动作，请在控制台「批准并执行」；微信里只能回 2 不发")


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


# ── the log, the clean-up, the switch ─────────────────────────────────────

def test_the_log_has_ids_and_counts_only(desk, caplog):
    caplog.set_level(logging.DEBUG)
    desk.her("我的地址：广东省佛山市南海区某某路100号")
    packet = desk.mac(text="收到地址啦～明天寄出")
    desk.review.sweep()
    desk.prm.tick()
    logged = caplog.text
    assert f"draft {packet['id']} told to the admin's chat" in logged
    for words in ("小美", "某某路", "收到地址啦", "她："):
        assert words not in logged, words


def test_the_clean_up_drops_the_closed_rows_and_keeps_a_waiting_one(desk):
    first = waiting(desk, "我的地址：广东省佛山市南海区某某路100号", text="收到地址～")
    desk.prm.decide(first["id"], "drop")
    second = waiting(desk, "就寄这里", text="好的～")
    counts = desk.prm.purge(desk.prm.store.deal(1), desk.clock.now)
    assert counts["wechat_reviews"] == 1
    assert desk.prm.store.wechat_review(first["id"]) is None and desk.prm.store.wechat_review(second["id"]) is not None
    desk.review.sweep()
    assert len([x for x in desk.told() if x.startswith(f"#{second['id']} 草稿")]) == 1     # (told once: never again)


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
