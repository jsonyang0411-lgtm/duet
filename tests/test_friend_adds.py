"""加百应达人微信 (src/prm/friend_adds.py, PRM_FRIEND_ADD; CODEBASE_REFERENCE 功能 R): a contact a creator left in 百应
becomes ONE friend request after an operator's confirmation, through the admin handler's add_friend_checked (the
same risk breaker as the admin's 添加好友).  The friend manager is a fake (no WeChat window); the admin handler, the
auto-accept service and the PRM service are the real ones.  Every value is made up."""

import json
import logging
import time

import pytest

from test_prm import ADMIN, CREATOR, Clock, config

from src.admin import AdminCommandHandler
from src.prm import friend_adds as FA
from src.prm.friend_adds import FriendAddError, check_greeting, greeting_for
from src.prm.service import PrmService
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.wechat.friend_manager import AcceptResult, AutoFriendService

WECHAT, PHONE = "abcdefyz", "13800000000"
AWEME = "dy_demo01"
DAY = 86400


class FakeManager:
    """FriendManager as the add path sees it: each add_friend answered from ``answers`` (a reason, a dict, an
    exception); its ``message`` holds the number, as the real one's does."""

    def __init__(self):
        self.answers = []
        self.calls = []

    def add_friend(self, keyword, message=""):
        self.calls.append((keyword, message))
        answer = self.answers.pop(0) if self.answers else "ok"
        if isinstance(answer, Exception):
            raise answer
        if answer == "ok":
            return {"ok": True, "target": keyword, "message": f"已成功向「{keyword}」发送好友申请（附言：{message}）"}
        return {"ok": False, "target": keyword, "reason": answer, "message": f"「{keyword}」：{answer}"}

    def look_for_news(self):                         # (the auto-accept service never runs here)
        return "changed"

    def accept_friend_requests(self, limit=None):
        return AcceptResult()


class World:
    def __init__(self, tmp_path, *, manager=True, run_now=True, **settings):
        self.clock = Clock(time.mktime((2026, 10, 29, 10, 0, 0, 0, 0, -1)))      # 10:00 local time
        self.relay = ClaudeRelay(config(), store=RelayStore(":memory:"), clock=self.clock, files_dir=tmp_path / "relay")
        self.prm = PrmService(config(**{"prm_friend_add": "on", **settings}), self.relay, store=PrmStore(":memory:"),
                              clock=self.clock, send=lambda chat, text: True)
        self.store = self.prm.store
        self.adds = self.prm.friend_adds
        self.manager = FakeManager()
        self.auto = AutoFriendService(self.manager, watch=False, enabled=False, wall_clock=self.clock)
        self.admin = AdminCommandHandler(friend_manager=self.manager if manager else None, auto_friend_service=self.auto)
        self.told, self.cleared, self.queued = [], [], []
        self.adds.spawn = (lambda fn: fn()) if run_now else self.queued.append
        self.adds.clear_clipboard = lambda text: self.cleared.append(text) or True
        self.adds.attach(self.admin, self.auto, notify=self.told.append)
        self.admin.set_friend_adds(self.adds)

    def paste(self, aweme=AWEME, wechat=WECHAT, phone=PHONE, nickname="小鹿", **kw):
        return self.adds.paste(aweme, nickname, {"wechat": wechat, "phone": phone}, **kw)["id"]

    def record(self, add_id):
        return self.store.friend_add(add_id)

    def later(self, seconds):
        self.clock.now += seconds

    def stop(self):
        self.auto.stop()
        self.prm.stop()
        self.relay.stop()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.stop()


def buyin_event(**over):
    """The 百应 inbox's call (dy-im-inbox, agreed with webot-04 on 2026-10-29)."""
    event = {"source": "buyin_card", "conversation_id": "conv1", "message_id": "7000000000000000001", "at": 1.0,
             "creator": {"aweme_id": AWEME, "uid": "1000000000000001", "nickname": "小鹿"},
             "contact": {"wechat": WECHAT, "phone": PHONE},
             "party_id": None,
             "invited": {"aweme_id": AWEME, "invited_at": "2026-10-29T11:00:00+08:00", "plan_id": "P20261029-1100-abcd",
                         "attempt": "9f1c00000001", "category": "small", "products": ["3000000000000000101"],
                         "commission_rate": 0}}
    event.update(over)
    return event


# ── the switch ────────────────────────────────────────────────────────────────

def test_the_switch_is_off_by_default_and_then_nothing_is_kept_or_added(tmp_path):
    w = World(tmp_path, prm_friend_add="off")
    try:
        assert not w.adds.enabled and w.adds.offer_from_buyin(buyin_event()) is None
        with pytest.raises(FriendAddError, match="没打开"):
            w.paste()
        assert w.store.friend_adds() == [] and w.manager.calls == []
        assert "没打开" in w.admin.handle("待加达人") and "没打开" in w.admin.handle("加达人 1")
        bare = PrmService(config(), w.relay, store=PrmStore(":memory:"), clock=w.clock)   # no setting at all: off
        assert not bare.friend_adds.enabled and bare.friend_adds.status()["enabled"] is False
    finally:
        w.stop()


# ── one confirmation, one request ─────────────────────────────────────────────

def test_a_pasted_contact_waits_and_one_confirmation_is_one_request(world, caplog):
    caplog.set_level(logging.DEBUG)
    add_id = world.paste()
    record = world.record(add_id)
    assert record["state"] == "offered" and record["greeting"] == "您好小鹿，百应上联系您的～"
    assert world.manager.calls == []                                      # nothing until somebody confirms
    out = world.adds.confirm(add_id)
    assert out["state"] == "requested" and out["target"] == "ab****yz"
    assert world.manager.calls == [(WECHAT, "您好小鹿，百应上联系您的～")]
    record = world.record(add_id)
    assert record["requested_at"] == world.clock.now and record["reason"] == "ok"
    [attempt] = world.store.friend_attempts()
    assert (attempt["by"], attempt["kind"], attempt["reason"]) == ("console", "wechat", "ok")
    assert world.cleared == [WECHAT]                                      # the clipboard: emptied if it still has it
    assert world.adds.status()["today"] == 1
    assert WECHAT not in caplog.text and PHONE not in caplog.text and "ab****yz" in caplog.text


def test_the_phone_number_is_used_when_asked_and_when_there_is_no_wechat_id(world):
    first = world.paste(wechat="")
    world.adds.confirm(first)
    world.later(200)
    second = world.paste(aweme="dy_demo02", phone="13900000000")
    world.adds.confirm(second, use="phone", greeting="  您好，  百应上联系您的 ")
    assert world.manager.calls == [(PHONE, "您好小鹿，百应上联系您的～"), ("13900000000", "您好， 百应上联系您的")]
    assert [a["kind"] for a in world.store.friend_attempts()] == ["phone", "phone"]


@pytest.mark.parametrize("reason, state", [
    ("ok", "requested"), ("not_found", "not_found"), ("already_friend", "known"),
    ("invalid", "failed"), ("no_ocr", "failed"), ("no_window", "failed"), ("aborted", "failed"),
    ("no_search_box", "failed"), ("no_search_entry", "failed"), ("no_add_button", "failed"),
    ("not_sent", "unsure"), ("no_dialog", "unsure"), ("error", "unsure"), ("risk", "blocked"),
    (RuntimeError("the window handle went away"), "unsure"),
])
def test_each_answer_of_add_friend_gives_its_state(world, reason, state):
    world.manager.answers = [reason]
    add_id = world.paste()
    world.adds.confirm(add_id)
    record = world.record(add_id)
    assert record["state"] == state and len(world.manager.calls) == 1
    assert record["reason"] == ("error" if isinstance(reason, Exception) else reason)
    assert world.store.friend_attempts()[0]["finished_at"] == world.clock.now


def test_a_risk_prompt_pauses_the_auto_accept_once_and_stops_every_add_until_the_admin_switches_it_on(world):
    world.manager.answers = ["risk"]
    first, second = world.paste(), world.paste(aweme="dy_demo02")
    world.adds.confirm(first)
    assert world.record(first)["state"] == "blocked"
    assert "已暂停" in world.auto.status_note() and "操作太频繁" in world.auto.paused_why()
    assert len(world.told) == 1 and "操作过于频繁" in world.told[0] and WECHAT not in world.told[0]
    world.later(3600)
    for add_id in (second, first):                                       # nothing is added while it is paused
        with pytest.raises(FriendAddError, match="暂停"):
            world.adds.confirm(add_id)
    assert len(world.manager.calls) == 1 and "暂停" in world.adds.status()["blocked_why"]
    assert "已开启" in world.admin.handle("自动同意好友 开")                # the admin lifts it
    world.admin.handle("自动同意好友 关")
    assert world.adds.confirm(first)["state"] == "requested"            # the blocked one can be added again
    assert len(world.manager.calls) == 2 and len(world.told) == 1


def test_a_second_click_or_a_second_page_never_adds_twice(tmp_path):
    w = World(tmp_path, run_now=False)                                   # the add waits in its thread
    try:
        first, second = w.paste(), w.paste(aweme="dy_demo02")
        assert w.adds.confirm(first)["state"] == "adding"
        with pytest.raises(FriendAddError, match="已经在加了"):
            w.adds.confirm(first)
        with pytest.raises(FriendAddError, match=f"另一个加好友正在进行（#{first}）"):
            w.adds.confirm(second)
        assert w.adds.status()["busy_id"] == first
        [run] = w.queued
        run()
        assert w.record(first)["state"] == "requested" and len(w.manager.calls) == 1
        with pytest.raises(FriendAddError, match="已经发过申请"):
            w.adds.confirm(first)
        assert len(w.manager.calls) == 1
    finally:
        w.stop()


def test_two_adds_are_at_least_the_gap_apart(world):
    first, second = world.paste(), world.paste(aweme="dy_demo02")
    world.adds.confirm(first)
    world.later(60)
    with pytest.raises(FriendAddError, match="再等 61 秒"):
        world.adds.confirm(second)
    assert world.adds.status()["next_at"] == world.clock.now + 60
    world.later(61)
    assert world.adds.confirm(second)["state"] == "requested"


def test_at_most_the_days_cap_a_day(tmp_path):
    w = World(tmp_path, prm_friend_add_daily_max=2, prm_friend_add_gap_sec=30)
    try:
        ids = [w.paste(aweme=f"dy_demo0{n}") for n in range(1, 4)]
        for add_id in ids[:2]:
            w.adds.confirm(add_id)
            w.later(31)
        with pytest.raises(FriendAddError, match="上限 2"):
            w.adds.confirm(ids[2])
        w.later(DAY)                                                      # the next day
        assert w.adds.confirm(ids[2])["state"] == "requested" and w.adds.status()["today"] == 1
    finally:
        w.stop()


def test_failed_attempts_count_too_and_a_failed_one_can_be_tried_again(world):
    world.manager.answers = ["no_window"]
    add_id = world.paste()
    world.adds.confirm(add_id)
    assert world.record(add_id)["state"] == "failed"
    world.later(121)
    assert world.adds.confirm(add_id)["state"] == "requested" and world.adds.status()["today"] == 2


def test_a_bot_stopped_while_adding_finds_the_record_unsure_at_its_start(tmp_path):
    w = World(tmp_path, run_now=False)
    try:
        add_id = w.paste()
        w.adds.confirm(add_id)                                           # the bot stops before the add returns
        again = PrmService(config(prm_friend_add="on"), w.relay, store=w.store, clock=w.clock)
        again.start()
        record = w.record(add_id)
        assert record["state"] == "unsure" and record["reason"] == "restart"
        assert w.store.friend_attempts()[0]["reason"] == "restart"
        again.stop()
        with pytest.raises(FriendAddError, match="不确定"):
            w.adds.confirm(add_id)
        assert w.adds.act(add_id, "not_sent")["state"] == "failed"      # an operator looked in WeChat
        w.later(121)
        assert w.adds.confirm(add_id)["state"] == "adding"
    finally:
        w.stop()


def test_without_the_friend_manager_nothing_is_added_but_the_number_can_be_copied(tmp_path):
    w = World(tmp_path, manager=False)                                   # the Mac's console, a PC without the window
    try:
        add_id = w.paste()
        assert w.adds.status()["can_add"] is False
        with pytest.raises(FriendAddError, match="没有好友管理组件"):
            w.adds.confirm(add_id)
        shown = w.adds.reveal(add_id, actor="console")
        assert (shown["wechat"], shown["phone"]) == (WECHAT, PHONE)
        assert [r["actor"] for r in w.record(add_id)["reveals"]] == ["console"]
        assert w.adds.act(add_id, "sent_by_phone")["state"] == "requested"
        assert w.record(add_id)["reason"] == "by_phone" and w.store.friend_attempts() == []   # not ours: not counted
    finally:
        w.stop()


# ── where a record comes from ─────────────────────────────────────────────────

def test_the_baiying_inbox_hook_keeps_only_our_invited_conversations(world):
    assert world.prm.on_buyin_contact == world.adds.offer_from_buyin            # the seam dy-im-inbox calls
    assert world.adds.offer_from_buyin(buyin_event(invited=None)) is None      # another person's invite
    assert world.adds.offer_from_buyin(buyin_event(invited={"plan_id": "P1"})) is None   # no 抖音号: not ours
    out = world.adds.offer_from_buyin(buyin_event())
    assert out == {"id": 1, "state": "offered", "masked": {"wechat": "ab****yz", "phone": "138****0000"}}
    record = world.record(1)
    assert (record["source"], record["conversation_id"], record["aweme_id"], record["creator_uid"]) == (
        "buyin_card", "conv1", AWEME, "1000000000000001")
    assert record["invited"]["plan_id"] == "P20261029-1100-abcd" and record["invited"]["commission_rate"] == 0
    assert world.adds.offer_from_buyin(buyin_event()) == out                    # read twice: the same record
    assert world.adds.offer_from_buyin(buyin_event(message_id="7000000000000000002"))["id"] == 1   # one open per 抖音号
    assert len(world.store.friend_adds()) == 1 and world.manager.calls == []


def test_a_card_whose_number_was_not_read_needs_an_operator_and_our_own_number_is_nothing(world):
    assert world.adds.offer_from_buyin(buyin_event(contact={"unavailable": True, "why": "own"})) is None
    out = world.adds.offer_from_buyin(buyin_event(contact={"unavailable": True, "why": "read-failed"}))
    record = world.record(out["id"])
    assert record["state"] == "needs_view" and record["unavailable"] and not record["wechat"]
    with pytest.raises(FriendAddError, match="还没有号码"):
        world.adds.confirm(out["id"])
    assert world.adds.act(out["id"], "edit", {"contact": {"wechat": WECHAT}})["state"] == "offered"
    assert world.record(out["id"])["masked"] == {"wechat": "ab****yz"}
    assert world.adds.offer_from_buyin(buyin_event(contact={"wechat": "1not-an-id"})) is None    # nothing usable


def test_a_registered_creator_who_left_her_contact_is_not_added_and_nothing_is_kept(world, caplog):
    """She is a WeChat friend already: no record, no number, no event of ours (the 百应 inbox records the masked
    line in her deal itself, tests/test_prm_buyin.py), and the answer None leaves 「加微信 #N」 out of that line."""
    party = world.prm.register(CREATOR, "小美")
    for invited in (None, buyin_event()["invited"]):
        assert world.adds.offer_from_buyin(buyin_event(party_id=party["id"], invited=invited)) is None
        assert world.adds.offer_from_buyin(buyin_event(party_id=party["id"], invited=invited, source="buyin_text",
                                                       contact={"wechat": WECHAT})) is None
    assert world.store.friend_adds() == [] and world.manager.calls == []
    events = world.store.events(world.store.deal_of(party["id"])["id"], limit=None)
    assert WECHAT not in json.dumps(events, ensure_ascii=False) and WECHAT not in caplog.text


def test_a_paste_needs_her_douyin_id_and_valid_numbers(world):
    for aweme, contact, word in (("", {}, "抖音号"), ("有中文", {}, "抖音号"), (AWEME, {"wechat": "1abc"}, "字母开头"),
                                 (AWEME, {"phone": "123"}, "11 位")):
        with pytest.raises(FriendAddError, match=word):
            world.adds.paste(aweme, "", contact)
    waiting = world.adds.paste(AWEME, "小鹿", {})                         # no number: she adds us
    assert waiting["state"] == "waiting"
    with pytest.raises(FriendAddError, match="已经有一条没处理完"):
        world.paste()
    party = world.prm.register("wxid_linked01", "小青", link={"account": "dy_demo02", "source": "register"})
    with pytest.raises(FriendAddError, match="已经关联了登记达人「小青」"):
        world.paste(aweme="DY_DEMO02")
    assert party["id"]


# ── the greeting ──────────────────────────────────────────────────────────────

def test_the_greeting_has_her_nickname_and_the_brand_from_this_pcs_env():
    template = FA.GREETING_DEFAULT
    assert greeting_for(template, "小鹿", "") == "您好小鹿，百应上联系您的～"
    assert greeting_for(template, "小鹿", "测试品牌") == "您好小鹿，我是测试品牌，百应上联系您的～"
    assert greeting_for(template, "小鹿 138****0000", "测试品牌") == "您好，我是测试品牌，百应上联系您的～"
    assert greeting_for(template, "一二三四五六七八九十", "") == "您好一二三四五六七八，百应上联系您的～"
    long_brand = "很长很长很长很长的品牌名字一二三"
    assert len(greeting_for(template, "一二三四五六七八", long_brand)) <= 30
    assert greeting_for("你好{昵称}", "", "") == "你好"
    assert check_greeting("  你好\n小鹿 ") == "你好 小鹿"
    with pytest.raises(FriendAddError, match="最多 30 个字"):
        check_greeting("长" * 31)


def test_the_brand_and_the_greeting_come_from_the_settings(tmp_path):
    w = World(tmp_path, prm_friend_add_brand="测试品牌", prm_friend_add_greeting="{昵称}你好，{品牌}的商务")
    try:
        assert w.record(w.paste())["greeting"] == "小鹿你好，测试品牌的商务"
        assert w.adds.status()["default_greeting"] == "你好，测试品牌的商务"
    finally:
        w.stop()


# ── an operator's other answers ───────────────────────────────────────────────

def test_drop_clears_the_numbers_and_nothing_can_be_done_after(world):
    add_id = world.paste()
    assert world.adds.act(add_id, "drop")["state"] == "dropped"
    record = world.record(add_id)
    assert (record["wechat"], record["phone"]) == ("", "") and record["closed_at"] == world.clock.now
    assert record["masked"] == {"wechat": "ab****yz", "phone": "138****0000"}
    for action in ("add", "reveal", "drop", "sent_by_phone"):
        with pytest.raises(FriendAddError):
            world.adds.act(add_id, action)


def test_unsure_is_settled_by_an_operator_who_looked_in_wechat(world):
    world.manager.answers = ["not_sent"]
    add_id = world.paste()
    attempt_at = world.clock.now
    world.adds.confirm(add_id)
    world.later(300)
    assert world.adds.act(add_id, "sent_already")["state"] == "requested"
    assert world.record(add_id)["requested_at"] == attempt_at
    with pytest.raises(FriendAddError):
        world.adds.act(add_id, "not_sent")


def test_not_found_is_given_another_number_or_she_is_asked_to_add_us(world):
    world.manager.answers = ["not_found"]
    add_id = world.paste()
    world.adds.confirm(add_id)
    with pytest.raises(FriendAddError, match="搜不到"):
        world.adds.confirm(add_id)
    assert world.adds.act(add_id, "wait_her")["state"] == "waiting"
    assert world.adds.act(add_id, "edit", {"contact": {"wechat": "abcdefgh"}})["state"] == "offered"
    record = world.record(add_id)
    assert (record["wechat"], record["phone"]) == ("abcdefgh", PHONE)


def test_link_registers_her_with_her_douyin_id_and_ends_the_record(world):
    add_id = world.paste()
    world.adds.confirm(add_id)
    out = world.adds.link(add_id, "wxid_demo")
    party = world.store.party(out["party_id"])
    assert (party["wxid"], party["name"], party["note"]) == ("wxid_demo", "小鹿", f"百应邀约 · 抖音号 {AWEME}")
    [link] = world.store.creator_links(party["id"])
    assert (link["account"], link["source"], link["add_id"]) == (AWEME, "friend_add", add_id)
    record = world.record(add_id)
    assert (record["state"], record["wxid"], record["party_id"]) == ("linked", "wxid_demo", party["id"])
    assert (record["wechat"], record["phone"]) == ("", "") and record["deal_id"] == world.store.deal_of(party["id"])["id"]
    with pytest.raises(FriendAddError, match="不能关联"):
        world.adds.link(add_id, "wxid_demo")


def test_a_record_not_to_be_registered_only_keeps_which_chat_she_is(world):
    add_id = world.paste()
    world.adds.act(add_id, "link_on_accept", {"on": False})
    out = world.adds.link(add_id, "wxid_demo")
    assert out["party_id"] is None and world.store.parties() == []
    assert world.record(add_id)["state"] == "linked" and world.record(add_id)["wxid"] == "wxid_demo"


def test_linking_needs_her_chat_and_a_good_wxid(world):
    from src.prm.store import PartyError
    add_id = world.paste()
    with pytest.raises(FriendAddError, match="要选是哪个聊天"):
        world.adds.link(add_id, "")
    with pytest.raises(PartyError):
        world.adds.link(add_id, "room@chatroom")
    assert world.record(add_id)["state"] == "offered" and world.store.parties() == []


# ── the numbers' time ─────────────────────────────────────────────────────────

def test_the_numbers_are_cleared_on_time_and_the_records_forgotten_after_the_retention(world):
    requested = world.paste()
    world.adds.confirm(requested)
    untouched = world.paste(aweme="dy_demo02")
    waiting = world.adds.paste("dy_demo03", "小青", {})["id"]
    world.store.update_friend_add(waiting, now=world.clock.now, wechat=WECHAT)      # (a number kept while waiting)
    world.later(7 * DAY + 1)
    world.adds.sweep(force=True)
    assert world.record(untouched)["state"] == "expired" and world.record(untouched)["reason"] == "untouched_days"
    assert world.record(untouched)["wechat"] == "" and world.record(untouched)["masked"]["wechat"] == "ab****yz"
    assert world.record(waiting)["state"] == "waiting" and world.record(waiting)["wechat"] == ""
    assert world.record(requested)["state"] == "requested" and world.record(requested)["wechat"] == WECHAT
    world.later(7 * DAY)
    world.adds.sweep(force=True)
    assert world.record(requested)["state"] == "expired" and world.record(requested)["reason"] == "requested_days"
    assert world.record(requested)["wechat"] == ""
    world.later(90 * DAY + 10)
    world.adds.sweep(force=True)
    assert world.record(requested) is None and world.record(untouched) is None
    assert world.store.friend_attempts() == []


def test_at_most_30_days_whatever_happens(world):
    add_id = world.paste()
    for _ in range(6):                                                   # touched every 5 days
        world.later(5 * DAY)
        world.store.update_friend_add(add_id, now=world.clock.now, greeting="改一下")
        world.adds.sweep(force=True)
    record = world.record(add_id)
    assert record["state"] == "expired" and record["reason"] == "max_days" and record["wechat"] == ""


def test_the_service_sweeps_with_its_hourly_clean_up(world):
    add_id = world.paste()
    world.later(7 * DAY + 1)
    world.prm.purge_due()
    assert world.record(add_id)["state"] == "expired"


# ── the admin's commands ──────────────────────────────────────────────────────

def test_the_admins_commands_list_add_and_drop_with_masked_numbers(world):
    first, second = world.paste(), world.paste(aweme="dy_demo02", wechat="", phone="13900000000")
    listed = world.admin.handle("待加达人")
    assert f"#{first} 小鹿 {AWEME} 微信号 ab****yz、手机 138****0000 待确认" in listed
    assert "手机 139****0000" in listed and WECHAT not in listed and PHONE not in listed
    answer = world.admin.handle(f"加达人 {first}")
    assert answer == f"已向 ab****yz 发好友申请（#{first}，附言：您好小鹿，百应上联系您的～）；今天已加 1/10"
    assert world.store.friend_attempts()[0]["by"] == "admin"
    assert world.admin.handle(f"加达人 {second}").startswith("没有加：离上一次加好友")
    assert world.admin.handle(f"不加达人 #{second}") == f"#{second} 记成不加了（号码已清除）"
    assert world.admin.handle("加达人 99") == "没有加：没有这一条加微信的记录"
    assert "已发申请" in world.admin.handle("待加达人")
    for command in ("待加达人", "加达人 3", "不加达人 3", "加达人#3"):
        assert world.admin.is_command(command), command
    assert not world.admin.is_command("加达人们") and not world.admin.is_command("加达人 abc")


def test_a_risk_prompt_during_the_admins_add_is_answered_to_the_admin_not_told_twice(world):
    world.manager.answers = ["risk"]
    add_id = world.paste()
    answer = world.admin.handle(f"加达人 {add_id}")
    assert answer.startswith(f"#{add_id} 微信提示操作频繁，已暂停：") and world.told == []
    assert "已暂停" in world.auto.status_note()


# ── the logs never hold a full number ─────────────────────────────────────────

def test_no_log_line_ever_holds_a_full_number(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.adds.offer_from_buyin(buyin_event())
    add_id = world.paste(aweme="dy_demo02")
    world.manager.answers = ["not_found", RuntimeError("the window handle went away"), "risk"]
    world.adds.confirm(add_id)                                            # not found
    world.adds.act(add_id, "edit", {"contact": {"wechat": WECHAT}})
    world.later(121)
    world.adds.confirm(add_id)                                            # an error: unsure
    world.adds.act(add_id, "not_sent")
    world.later(121)
    world.adds.confirm(add_id)                                            # the risk prompt: blocked
    world.adds.reveal(1)
    world.adds.link(1, "wxid_demo")
    world.admin.handle("待加达人")
    world.later(7 * DAY + 1)
    world.adds.sweep(force=True)
    text = caplog.text                                                    # (tracebacks too)
    assert WECHAT not in text and PHONE not in text and "ab****yz" in text


def test_an_add_that_could_not_be_started_is_failed_not_stuck(world):
    def broken(fn):
        raise RuntimeError("can't start new thread")

    world.adds.spawn = broken
    add_id = world.paste()
    assert world.adds.confirm(add_id)["state"] == "failed"
    assert world.record(add_id)["reason"] == "not_started" and world.manager.calls == []
    assert world.store.friend_attempts()[0]["reason"] == "not_started"


# ── who she is once she accepted (design §7): candidates, never a link ────────

def friend(wxid="wxid_demo", how="requested", own_greeting="", verify_text="", name="小鹿🌸", added_at=None, **more):
    """A new friend as the scan hands her on (src/wechat/new_friends.py ``_report``)."""
    return {"wxid": wxid, "name": name, "remark": "", "verify_text": verify_text, "how": how,
            "own_greeting": own_greeting, "kind": "requested" if how == "requested" else "pc",
            "added_at": added_at, **more}


def sent(world, aweme, nickname, wechat=WECHAT, greeting=None):
    """A record pasted and added (a request went out); the gap passes."""
    add_id = world.paste(aweme=aweme, nickname=nickname, wechat=wechat, phone="")
    world.adds.confirm(add_id, greeting=greeting)
    world.later(121)
    return add_id


def test_our_own_greeting_word_for_word_says_which_request_she_accepted(world):
    first = sent(world, "dy_demo01", "小鹿")
    second = sent(world, "dy_demo02", "小青", wechat="abcdefgh")
    candidate = world.adds.on_new_friend(friend(own_greeting="您好小青，百应上联系您的～", added_at=world.clock.now))
    assert candidate["level"] == "sure" and candidate["level_label"] == "确定是她"
    assert candidate["evidence"] == ["她通过了我们的申请，新私聊里我方的打招呼和这份附言一字不差"]
    assert world.record(second)["candidate"]["wxid"] == "wxid_demo" and world.record(first)["candidate"] == {}
    assert (world.record(second)["state"], world.store.parties()) == ("requested", [])   # never linked by itself


def test_the_same_greeting_twice_or_two_requests_waiting_point_at_nobody(world):
    first = sent(world, "dy_demo01", "小鹿", greeting="您好，百应上联系您的～")
    second = sent(world, "dy_demo02", "小青", wechat="abcdefgh", greeting="您好，百应上联系您的～")
    assert world.adds.on_new_friend(friend(own_greeting="您好，百应上联系您的～")) is None
    assert world.record(first)["candidate"] == {} and world.record(second)["candidate"] == {}


def test_the_only_request_waiting_is_likely_her(world):
    add_id = sent(world, "dy_demo01", "小鹿")
    candidate = world.adds.on_new_friend(friend(own_greeting="", added_at=world.clock.now))      # (greeting not read)
    assert (candidate["level"], world.record(add_id)["candidate"]["level"]) == ("likely", "likely")


def test_the_wechat_id_she_gave_being_the_new_friends_wxid_is_sure(world):
    add_id = world.adds.paste("dy_demo01", "小鹿", {"wechat": "meimei_88"})["id"]
    candidate = world.adds.on_new_friend(friend(wxid="MeiMei_88", how="manual"))
    assert candidate["level"] == "sure" and candidate["evidence"] == ["她给的微信号就是这个新好友的微信号"]
    assert world.record(add_id)["candidate"]["wxid"] == "MeiMei_88"


def test_she_added_us_naming_her_baiying_nickname_or_baiying_is_possible(world):
    add_id = world.adds.paste("dy_demo01", "小鹿", {})["id"]                                     # waiting for her
    assert world.adds.on_new_friend(friend(how="manual", verify_text="想合作", name="阿花")) is None
    candidate = world.adds.on_new_friend(friend(wxid="wxid_other", how="manual", verify_text="你好，我是百应上的小鹿"))
    assert candidate["level"] == "possible" and candidate["evidence"] == ["她加我们时的验证消息里有「小鹿」"]
    assert world.record(add_id)["candidate"]["wxid"] == "wxid_other"


def test_a_friend_right_after_an_add_with_no_application_window_is_likely(world):
    world.manager.answers = ["no_dialog"]
    add_id = world.paste()
    attempt_at = world.clock.now
    world.adds.confirm(add_id)
    assert world.record(add_id)["state"] == "unsure"
    candidate = world.adds.on_new_friend(friend(how="manual", added_at=attempt_at + 60))
    assert candidate["level"] == "likely" and "2 分钟内" in candidate["evidence"][0]


def test_not_her_clears_it_and_that_friend_is_not_offered_for_the_record_again(world):
    add_id = sent(world, "dy_demo01", "小鹿")
    world.adds.on_new_friend(friend())
    assert world.adds.act(add_id, "not_her")["state"] == "requested"
    record = world.record(add_id)
    assert record["candidate"] == {} and record["rejected"] == ["wxid_demo"]
    assert world.adds.on_new_friend(friend()) is None
    with pytest.raises(FriendAddError, match="没有可能是她的新好友"):
        world.adds.act(add_id, "not_her")
    assert world.adds.on_new_friend(friend(wxid="wxid_next"))["wxid"] == "wxid_next"


def test_a_better_candidate_replaces_a_weaker_one_never_the_other_way(world):
    add_id = sent(world, "dy_demo01", "小鹿")
    world.adds.on_new_friend(friend(wxid="wxid_maybe", own_greeting=""))                       # likely
    world.adds.on_new_friend(friend(wxid="wxid_sure", own_greeting="您好小鹿，百应上联系您的～"))    # sure
    assert world.record(add_id)["candidate"]["wxid"] == "wxid_sure"
    assert world.adds.on_new_friend(friend(wxid="wxid_later", own_greeting="")) is None
    assert world.record(add_id)["candidate"]["wxid"] == "wxid_sure"


def test_a_chat_linked_already_is_never_offered_again(world):
    add_id = sent(world, "dy_demo01", "小鹿")
    world.adds.link(add_id, "wxid_demo")
    other = sent(world, "dy_demo02", "小青", wechat="abcdefgh")
    assert world.adds.on_new_friend(friend(own_greeting="")) is None
    assert world.record(other)["candidate"] == {}


def test_a_record_made_after_she_became_a_friend_looks_back(world):
    class Scan:                                                      # the new-friend scan's recent()
        def recent(self):
            return [friend(wxid="wxid_early", how="manual", verify_text="百应邀约来的", added_at=world.clock.now - 60)]

    world.adds.set_new_friends(Scan())
    add_id = world.adds.paste("dy_demo01", "小鹿", {})["id"]
    assert world.record(add_id)["candidate"]["wxid"] == "wxid_early"
    assert world.record(add_id)["candidate"]["level"] == "possible"


def test_registering_her_by_hand_with_the_douyin_id_closes_the_record(world):
    add_id = sent(world, "dy_demo01", "小鹿")
    party = world.prm.register("wxid_demo", "小鹿", link={"account": "DY_DEMO01", "source": "register"})
    record = world.record(add_id)
    assert (record["state"], record["reason"], record["party_id"], record["wxid"]) == (
        "linked", "registered_by_hand", party["id"], "wxid_demo")
    assert record["wechat"] == "" and record["masked"]["wechat"] == "ab****yz"


def test_the_records_console_view_carries_the_candidate_and_lists_an_expired_one_with_one(world):
    from src.prm.friend_adds import friend_adds_out
    add_id = sent(world, "dy_demo01", "小鹿")
    world.later(14 * DAY + 10)
    world.adds.sweep(force=True)
    assert world.record(add_id)["state"] == "expired"
    [item] = friend_adds_out(world.store, world.clock.now)
    assert not item["listed"] and item["can_link"]                       # expired, linkable for 30 days
    world.adds.on_new_friend(friend(own_greeting="您好小鹿，百应上联系您的～"))
    [item] = friend_adds_out(world.store, world.clock.now)
    assert item["listed"] and item["candidate"]["level"] == "sure"
    assert world.adds.link(add_id, "wxid_demo")["state"] == "linked"


def test_with_the_switch_off_no_new_friend_is_looked_at(tmp_path):
    w = World(tmp_path, prm_friend_add="off")
    try:
        assert w.adds.on_new_friend(friend()) is None and w.adds.match_recent() == 0
    finally:
        w.stop()
