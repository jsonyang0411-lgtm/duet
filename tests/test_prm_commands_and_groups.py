"""合作单指令 (PRM_WECHAT_COMMANDS) and the 2026-11-11 group switches together (merge 2026-11-11, branch merge-1111).

Two features landed on one day and meet in the admin's chat and in where the bot's own messages go:

- 群自己接上 / 她那边的人 / 发在她那边最近说话的地方 / 群里我们自己的人 (PRM_GROUPS_AUTO_LINK, PRM_GROUP_HER_SIDE,
  PRM_REACH_LAST_SPOKE, PRM_GROUP_OURS): 「确认接群 <群名>」, 「不接群 <群名>」 for groups not linked, the hold of a draft in
  a group the scan linked that nobody confirmed (``boss_groups.confirmed_at``), the hold of her side's lines, and
  ``PrmService._reaching``, the one decision of where what the bot starts itself goes;
- 合作单指令: 「做手卡 / 补发货盘 / 写开场白 / 签收 / 备注 / 接管 / 副驾驶 / 合作单 <达人>」「指令」「选 N」 typed by the
  admin, run as the console's own deal actions with the actor ``admin``.

Here, with every switch on: (a) each line of the admin reaches the handler it is for and only that one, names holding
command words included; (b) 「合作单 <名字>」 says where what the bot starts goes from that same decision, and the
commands' jobs go where the console's click goes, the switch on and off; (c) a 开场白 ordered from WeChat still waits
when it goes to a group, and a group's 开场白 never goes by itself; (d) nothing a command makes slips past the holds, and
only a draft a person sent confirms a group the scan linked; (e) 「合作单」 says a group of hers waits for 「确认接群」;
(f) the admin's command is a person's act exactly where a console click is; (g) the helps list both features once.

The relay, the PRM service and the router as Bot.run wires them (test_prm_wechat_review's ``Desk``), the WeChat backend
test_prm_groups_auto_link's fake (her team's group: her, her 商务, a colleague, us).  Every name, id and number is made up.
"""

import json
import time
from types import SimpleNamespace

import pytest

from test_group_link import BD, GROUP, GROUP_NAME, TESTER, gmsg, groups
from test_prm import ADMIN, CREATOR, Clock
from test_prm_boss_groups import BOSS, ME, on_wechats_clock
from test_prm_groups_auto_link import AutoBackend
from test_prm_wechat_review import Desk, head, reply_draft

from src.admin import HELP_COMMANDS, HELP_TEXT, RELAY_HELP, AdminCommandHandler, help_text
from src.prm import admin_commands as ac
from src.prm import boss_groups as bg
from src.prm.service import AUTO_UNCONFIRMED_HELD, HER_SIDE_HELD, OPENING_HELD, OPERATOR_ACTORS, SETTLE_SEC

HELPER_NAME = "备注小助手"                 # a creator whose name holds a command word
HELPER_WXID = "wxid_creator06"
SIGN_GROUP = "400000006@chatroom"         # a group whose name holds a command word
SIGN_GROUP_NAME = "签收 群"
PHONE_GROUP = "400000007@chatroom"
PHONE_GROUP_NAME = "小美13800001111对接"   # a group whose name holds a phone number (masked in the admin's chat)
EVERY_SWITCH = {"prm_wechat_commands": "on", "prm_groups_auto_link": "on", "prm_group_her_side": "on",
                "prm_reach_last_spoke": "on", "prm_linked_groups_auto": "on", "prm_boss_groups": "on",
                "prm_group_with": BOSS, "prm_handcards": "on", "prm_opening_auto_min": 30}


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 11, 14, 0, 0, 0, 0, -1)))


def make(clock, tmp_path, *, relay_only="off", **prm):
    """The desk with every switch on (``prm`` overrides), the group backend attached, the admin handler given the
    group commands and the deal commands as Bot._wire_friend_adds hands them over; 小美 registered."""
    d = Desk(clock, tmp_path, review="on", relay_only=relay_only, **{**EVERY_SWITCH, **prm})
    d.backend = on_wechats_clock(AutoBackend(), clock)
    d.prm.boss_groups.attach(d.backend)
    d.prm._pause = lambda sec: False
    d.router._admin.set_group_links(d.prm.group_links)
    if d.prm.deal_commands.on:
        d.router._admin.set_deal_commands(d.prm.deal_commands)
    groups(d)
    d.her_party = d.prm.register(CREATOR, "小美", "测试达人")
    return d


@pytest.fixture
def desk(clock, tmp_path):
    d = make(clock, tmp_path)
    yield d
    d.stop()


def deal_id(d, wxid=CREATOR):
    return d.prm.store.deal_of(d.prm.store.party_by_wxid(wxid)["id"])["id"]


def open_job(d, chat):
    job = d.relay.store.latest_open(chat, drafts_only=True, system=False)
    return None if job is None else {**job, "meta": json.loads(job["meta"] or "{}")}


def quiet(d, *chats):
    """Her lines' reply jobs put away (as if done with): a bot-started job is asked for only when none of hers is open."""
    for chat in chats or (CREATOR, GROUP):
        job = open_job(d, chat)
        if job is not None:
            assert d.relay.store.cancel_if_pending(job["id"], d.clock.now)


def drafted(d, job_id, text="老师您好～", kind="faq"):
    """The Mac takes job ``job_id`` and hands back a draft: its packet."""
    d.clock.now += SETTLE_SEC + 1
    assert d.relay.next_job(0, ("prm",))["id"] == job_id
    d.relay.finish(job_id, {"ok": True, "text": json.dumps(reply_draft(text=text, message_kind=kind),
                                                             ensure_ascii=False), "duration_s": 3})
    d.relay.tick()
    return d.prm.store.packet_for_job(job_id)


def link_by_hand(d):
    """「接群」「接群 1」「是 1」 from the admin's chat: her team's group linked to 小美 by hand."""
    d.admin("接群")
    d.admin("接群 1")
    answer = d.admin("是 1")
    assert answer.startswith("接手了") and d.prm.boss_groups.is_linked(GROUP), answer
    return answer


def spoke(d, where, sender=CREATOR):
    """Her side writes (``where``: "private" or "group"); the reply job it makes is put away."""
    if where == "private":
        d.her("私聊问一下")
    else:
        d.prm.handle(gmsg(d, "群里说一句", sender=sender))
    d.clock.now += 30
    quiet(d)


def status(d, name="小美"):
    return d.admin(f"合作单 {name}").split("\n")


# ── (a) every line reaches its own handler, and only it ─────────────────────────

def spy(d, monkeypatch) -> list:
    """Who handled each line: the deal commands (a command, a 「选 N」), the group commands (by method), the review's
    answer (matched), the relay (a new job of the admin's chat)."""
    hits: list = []
    commands, links, review = d.prm.deal_commands, d.prm.group_links, d.review

    def wrap(obj, name, label):
        real = getattr(obj, name)

        def call(*args, **kw):
            hits.append(label)
            return real(*args, **kw)

        monkeypatch.setattr(obj, name, call)

    wrap(commands, "_command", "deal")
    wrap(commands, "_choose", "deal:choose")
    for name in ("admin_list", "admin_ask", "admin_yes", "admin_cancel", "admin_stop", "admin_confirm"):
        wrap(links, name, f"group:{name[6:]}")
    real_answer = review.answer

    def answer(msg, **kw):
        out = real_answer(msg, **kw)
        if out[0]:
            hits.append("review")
        return out

    monkeypatch.setattr(review, "answer", answer)
    return hits


def handled(d, hits, line, **kw):
    """The admin's line: (who handled it, the reply)."""
    del hits[:]
    jobs = len(d.jobs("admin"))
    reply = d.admin(line, **kw)
    who = list(hits) + (["relay"] if len(d.jobs("admin")) > jobs else [])
    if line in HELP_COMMANDS and reply == help_text(ac.HELP_LINE):
        who.append("help")
    return who, reply


def test_with_every_switch_on_each_line_goes_to_its_own_handler_only(desk, monkeypatch):
    """Group names and creator names that hold command words (a group 「签收 群」, a creator 「备注小助手」) never send a
    line to the other feature: 「签收 签收 群」 is a deal command naming nobody, 「接群 合作单 小美」 a group command naming
    no group, and every line reaches one handler."""
    d = desk
    d.prm.register(HELPER_WXID, HELPER_NAME, "测试达人")
    d.backend.groups[SIGN_GROUP] = {"name": SIGN_GROUP_NAME, "members": [ME, TESTER]}
    d.backend.active[SIGN_GROUP] = d.clock.now - 60
    hits = spy(d, monkeypatch)
    helper_deal = deal_id(d, HELPER_WXID)
    cases = [
        ("接群", ["group:list"], "最近有动静"),
        (f"接群 {SIGN_GROUP_NAME}", ["group:ask"], f"群「{SIGN_GROUP_NAME}」（2 人）里谁是达人"),
        (f"签收 {SIGN_GROUP_NAME}", ["deal"], "没有达人对得上"),         # (the group's name: no creator's)
        ("不接", ["group:cancel"], "不接了"),
        (f"接群 {SIGN_GROUP_NAME}", ["group:ask"], "谁是达人"),
        ("是 1", ["group:yes"], "接手了"),
        (f"确认接群 {SIGN_GROUP_NAME}", ["group:confirm"], "没有机器人自动接的"),   # (linked by hand: nothing to confirm)
        (f"不接群 {SIGN_GROUP_NAME}", ["group:stop"], "不接了"),
        (f"做手卡 {HELPER_NAME}", ["deal"], f"{HELPER_NAME}："),
        (f"备注 {HELPER_NAME} 只发链接", ["deal"], "起草备注换好了"),
        ("接管 备注小", ["deal"], "选 1"),                                 # (a piece of her name: listed)
        ("选 1", ["deal:choose"], "人工接管了"),
        ("合作单 小美", ["deal"], "小美（合作单 "),
        ("接群 合作单 小美", ["group:ask"], "没找到"),                    # (no group of that name: said so)
        ("指令", ["deal"], "合作单指令"),
        ("帮助", ["help"], "可用命令"),
        ("cc 签收 小美", ["relay"], "收到"),
        (HELPER_NAME, [], None),                                          # (her name alone: no command at all)
        (SIGN_GROUP_NAME, ["deal"], "名字至少写两个字"),                  # (「签收 群」 typed alone is a 签收 of 「群」)
    ]
    for line, want, said in cases:
        who, reply = handled(d, hits, line)
        assert who == want, (line, who, reply)
        if said is None:
            assert reply is None, (line, reply)
        else:
            assert said in (reply or ""), (line, reply)
    assert d.prm.store.deal(helper_deal)["draft_note"] == "只发链接"
    assert d.prm.store.deal(deal_id(d))["stage"] == "talk"                # (nobody's 签收 ran)
    assert d.prm.store.boss_group(SIGN_GROUP)["state"] != bg.LINKED      # (stopped again)


def test_the_reviews_answers_stay_the_reviews_with_every_switch_on(desk, monkeypatch):
    """1 / 2 / 发 N to the drafts told in WeChat are the review's (never a deal or a group command); 「执行」 and 「对过图 1」
    with nothing of theirs waiting reach neither feature's commands either."""
    d = desk
    hits = spy(d, monkeypatch)
    for line in ("1", "发 {n}", "2"):
        d.clock.now += 120
        d.her("这款还有货吗")
        packet = d.mac(text="有的老师～")
        d.review.sweep()
        who, reply = handled(d, hits, line.format(n=packet["id"]))
        assert who == ["review"], (line, who, reply)
        status_now = d.prm.store.packet(packet["id"])["status"]
        assert status_now == ("dropped" if line == "2" else "approved"), (line, status_now)
        if status_now == "approved":
            d.prm.send_approved()
    for line in ("执行", "对过图 1"):
        who, reply = handled(d, hits, line)
        assert not [w for w in who if w.startswith(("deal", "group"))], (line, who)


def test_relay_only_still_keeps_every_command_of_both_features_from_the_mac(clock, tmp_path, monkeypatch):
    """CLAUDE_RELAY_ONLY on: the admin's other lines go to the Mac's Claude, never a command of either feature; a
    「是 1」 or 「选 1」 with nothing asked is the admin's words for the Mac, as before."""
    d = make(clock, tmp_path, relay_only="on")
    try:
        d.backend.groups[SIGN_GROUP] = {"name": SIGN_GROUP_NAME, "members": [ME, TESTER]}
        d.backend.active[SIGN_GROUP] = d.clock.now - 60
        hits = spy(d, monkeypatch)
        for line, want in ((f"签收 {SIGN_GROUP_NAME}", ["deal"]), (f"确认接群 {SIGN_GROUP_NAME}", ["group:confirm"]),
                           (f"不接群 {SIGN_GROUP_NAME}", ["group:stop"]), ("指令", ["deal"]),
                           ("合作单 小美", ["deal"]), ("是 1", ["relay"]), ("选 1", ["relay"]),
                           (HELPER_NAME, ["relay"])):
            who, reply = handled(d, hits, line)
            assert who == want, (line, who, reply)
    finally:
        d.stop()


# ── (b) 「合作单」 tells where from the sends' own decision; the commands go where the console's click goes ──

@pytest.mark.parametrize("reach", ["on", "off"])
def test_where_the_status_says_and_where_the_commands_go_is_the_consoles_decision(clock, tmp_path, reach):
    d = make(clock, tmp_path, prm_reach_last_spoke=reach)
    try:
        link_by_hand(d)
        deal = deal_id(d)
        spoke(d, "private")
        spoke(d, "group", sender=BD)                                      # (her 商务: her side, PRM_GROUP_HER_SIDE)
        line = next(x for x in status(d) if x.startswith("回复："))
        if reach == "on":
            assert f"机器人主动发的（开场白、货盘、手卡、发货通知）发到群「{GROUP_NAME}」（她那边最近是在这个群里说的话）" in line
            assert d.prm._reach_out(d.prm.store.party_by_wxid(CREATOR), d.prm.store.deal(deal)) == {
                "where": "group", "name": GROUP_NAME, "why": "last_spoke"}
        else:
            assert line.endswith("机器人主动发的（开场白、货盘、手卡、发货通知）发私聊")
        want = GROUP if reach == "on" else CREATOR
        for console, typed in ((lambda: d.prm.ask_huopan(deal), "补发货盘 小美"),
                               (lambda: d.prm.ask_handcards(deal, None), "做手卡 小美"),
                               (lambda: d.prm.ask_opening(deal), "写开场白 小美")):
            out = console()
            assert out["ok"], out
            clicked = open_job(d, want)
            assert clicked is not None and clicked["id"] == out["job"], typed
            assert d.relay.store.cancel_if_pending(clicked["id"], d.clock.now)
            answer = d.admin(typed)
            assert "已派" in answer, (typed, answer)
            ordered = open_job(d, want)
            assert ordered is not None and ordered["chat_id"] == clicked["chat_id"], typed
            assert ordered["meta"].get("reach") == clicked["meta"].get("reach"), typed
            assert ordered["meta"].get("channel") == clicked["meta"].get("channel"), typed
            assert d.relay.store.cancel_if_pending(ordered["id"], d.clock.now)
        # her latest line in private again: the status and the commands follow it (with the switch on)
        spoke(d, "private")
        line = next(x for x in status(d) if x.startswith("回复："))
        assert line.endswith("发私聊（她最近是在私聊里说的话）" if reach == "on" else "发私聊"), line
        assert "已派" in d.admin("补发货盘 小美")
        assert open_job(d, CREATOR) is not None and open_job(d, GROUP) is None
    finally:
        d.stop()


def test_with_no_private_chat_the_status_says_so_and_names_the_group(clock, tmp_path):
    d = make(clock, tmp_path, prm_reach_last_spoke="off")
    try:
        link_by_hand(d)
        spoke(d, "group")                                                 # (she never wrote in private)
        line = next(x for x in status(d) if x.startswith("回复："))
        assert f"发到群「{GROUP_NAME}」（她没有和我们的私聊）" in line, line
    finally:
        d.stop()


# ── (c) a 开场白 ordered from WeChat waits in a group too; a group's 开场白 never goes by itself ──

def test_a_wechat_opening_to_her_group_waits_and_no_group_opening_goes_by_itself(clock, tmp_path):
    d = make(clock, tmp_path)
    try:
        link_by_hand(d)
        spoke(d, "group")
        d.prm.set_auto_mode(True)                                          # (自动模式: the night's rules at any hour)
        answer = d.admin("写开场白 小美")
        assert "已派写开场白" in answer, answer
        job = open_job(d, GROUP)
        assert job["meta"]["opening"].get(OPENING_HELD) is True and job["meta"]["reach"] == "last_spoke"
        assert job["meta"]["channel"] == "group" and "opening_auto" not in job["meta"]
        packet = drafted(d, job["id"], text="老师您好，我是品牌的运营～", kind="greeting")
        assert packet["status"] == "pending" and packet["chat_id"] == GROUP
        assert "auto_at" not in packet["meta"]
        d.clock.now += 3 * 3600
        assert d.prm.send_due_openings() == 0 and d.prm.send_approved() == 0
        assert d.prm.store.packet(packet["id"])["status"] == "pending" and d.to(GROUP) == []
        # the console's click (not held): to the group as well, and it does not go by itself either
        d.prm.decide(packet["id"], "drop")
        out = d.prm.ask_opening(deal_id(d))
        assert out["ok"], out
        job = open_job(d, GROUP)
        assert OPENING_HELD not in job["meta"]["opening"]
        packet = drafted(d, job["id"], text="老师您好～", kind="greeting")
        assert packet["status"] == "pending" and "auto_at" not in packet["meta"]
        d.clock.now += 3 * 3600
        assert d.prm.send_due_openings() == 0 and d.prm.send_approved() == 0 and d.to(GROUP) == []
    finally:
        d.stop()


# ── (d) the holds hold for what a command makes; only a person's sent draft confirms ──

def auto_linked(d):
    """The scan links her team's group by itself (exactly one registered creator in it): not confirmed yet."""
    out = d.prm.boss_groups.auto_scan()
    assert out["linked"] == 1, out
    assert d.prm.boss_groups.unconfirmed(GROUP)


def test_nothing_a_command_does_confirms_a_group_the_scan_linked_but_a_draft_a_person_sends(desk):
    d = desk
    auto_linked(d)
    spoke(d, "group")
    for typed in ("接管 小美", "副驾驶 小美", "备注 小美 群里说话", "合作单 小美", "签收 小美", "指令"):
        d.admin(typed)
        assert d.prm.boss_groups.unconfirmed(GROUP), typed
    assert "已派补发货盘" in d.admin("补发货盘 小美")
    job = open_job(d, GROUP)
    assert job["meta"]["reach"] == "last_spoke"
    packet = drafted(d, job["id"], text="老师，货盘再发您一份～")
    assert packet["status"] == "pending" and packet["meta"]["hold"] == AUTO_UNCONFIRMED_HELD
    assert d.prm.boss_groups.unconfirmed(GROUP)                           # (a draft waiting is no confirmation)
    d.review.sweep()
    reply = d.admin("1", quote=head(d, packet["id"]))                     # the admin sends it from WeChat
    assert "已批准" in reply, reply
    d.prm.send_approved()
    assert d.to(GROUP) == ["老师，货盘再发您一份～"]
    assert not d.prm.boss_groups.unconfirmed(GROUP)
    [ev] = [e for e in d.prm.store.events(deal_id(d), kinds=(bg.EVENT,)) if e["detail"].get("confirmed")]
    assert ev["detail"]["how"] == "sent"


def test_a_line_of_her_side_joining_a_commands_job_keeps_its_draft_waiting(desk):
    """The group confirmed and 「手动接的群也自动发」 on: still, her assistant's line that joins the job a 「补发货盘」 made
    keeps the draft waiting for a person (she is anybody not known to be ours)."""
    d = desk
    auto_linked(d)
    assert "确认是 小美 的群" in d.admin(f"确认接群 {GROUP_NAME}")
    spoke(d, "group")
    assert "已派补发货盘" in d.admin("补发货盘 小美")
    job = open_job(d, GROUP)
    d.prm.handle(gmsg(d, "帮我看下链接", sender=BD))                       # (before the Mac took the job)
    joined = open_job(d, GROUP)
    assert joined["id"] == job["id"] and bg.HER_SIDE_MARK in (joined["text"] or "")    # (her side's line joined it)
    assert joined["meta"]["huopan"] == {"by": "console"}
    packet = drafted(d, joined["id"], text="老师，货盘和链接都发您～")
    assert packet["status"] == "pending" and packet["meta"]["hold"] == HER_SIDE_HELD


# ── (e) 「合作单」 says her auto-linked group waits for 「确认接群」 ─────────────────

def test_the_status_says_an_unconfirmed_group_waits_for_its_confirmation(desk):
    d = desk
    auto_linked(d)
    lines = status(d)
    assert (f"群「{GROUP_NAME}」是机器人自动接的，还没人确认是她的群：那里的草稿都等你看；对的话回「确认接群 {GROUP_NAME}」"
            in lines), lines
    d.admin(f"确认接群 {GROUP_NAME}")
    assert not any("还没人确认" in x for x in status(d))


def test_a_masked_group_name_points_to_the_console_never_shows_the_number(clock, tmp_path):
    d = make(clock, tmp_path)
    try:
        d.backend.groups.pop(GROUP)
        d.backend.groups[PHONE_GROUP] = {"name": PHONE_GROUP_NAME, "members": [ME, CREATOR, BD]}
        d.backend.active[PHONE_GROUP] = d.clock.now - 60
        assert d.prm.boss_groups.auto_scan()["linked"] == 1 and d.prm.boss_groups.unconfirmed(PHONE_GROUP)
        reply = d.admin("合作单 小美")
        line = next(x for x in reply.split("\n") if "还没人确认" in x)
        assert "13800001111" not in reply
        assert line.endswith("对的话到控制台她的合作单上点「是她的群」") and "确认接群" not in line
        told = [t for t in d.told() if "已自动接群" in t]
        assert told and "13800001111" not in told[-1] and "确认接群" not in told[-1]   # (the notice says the same)
    finally:
        d.stop()


# ── (f) the admin's command is a person's act where the console's click is, and nothing automatic is ──

def test_a_person_treated_her_is_the_consoles_or_the_admins_act_only(desk):
    d = desk
    groups_ = d.prm.boss_groups
    deal = d.prm.store.deal(deal_id(d))
    party = d.prm.store.party_by_wxid(CREATOR)
    assert not groups_._treated(deal)
    for actor, treated in (("system", False), ("creator", False), ("mac", False), ("operator", False),
                           ("console", True), ("admin", True)):
        fresh = d.prm.store.add_party(f"wxid_t_{actor}", f"测试{actor}", "测试", auto=True)
        fresh_deal = d.prm.store.deal_of(fresh["id"])
        d.prm.store.add_event(fresh_deal["id"], fresh["id"], "stage", {"from": "talk", "to": "agreed"}, actor=actor,
                              now=d.clock.now)
        assert groups_._treated(d.prm.store.deal(fresh_deal["id"])) is treated, actor
    assert set(OPERATOR_ACTORS) == set(bg.PERSON_ACTORS) == {"console", ac.ACTOR}
    assert party["id"]


def test_the_admins_note_from_wechat_makes_an_auto_registered_friend_a_certain_creator(clock, tmp_path):
    """The scan asks about a group whose one registered member is a friend the bot registered by itself; the admin's
    typed 「备注」 counts as a person treating her as a creator, as the console's 起草备注 does (her 「接管」 does not, as
    the console's 人工接管 does not)."""
    d = make(clock, tmp_path)
    try:
        d.backend.groups.pop(GROUP)
        friend = d.prm.store.add_party("wxid_newfriend09", "新好友测试", "自动登记（新好友）", auto=True)
        d.backend.names["wxid_newfriend09"] = "新好友测试"
        new = "400000009@chatroom"
        d.backend.groups[new] = {"name": "内部运营群", "members": [ME, "wxid_newfriend09", "wxid_guest01"]}
        d.backend.active[new] = d.clock.now - 60
        assert d.prm.boss_groups.auto_scan()["linked"] == 0
        d.admin("接管 新好友测试")
        d.clock.now += bg.BOSS_SCAN_SEC + 1
        assert d.prm.boss_groups.auto_scan()["linked"] == 0              # (a mode is no treatment, as the console's)
        assert "起草备注换好了" in d.admin("备注 新好友测试 她是做直播的")
        d.clock.now += bg.BOSS_SCAN_SEC + 1
        assert d.prm.boss_groups.auto_scan()["linked"] == 1
        assert d.prm.store.boss_group(new)["party_id"] == friend["id"]
    finally:
        d.stop()


# ── (g) the helps list both features' commands once ───────────────────────────

def test_the_admins_help_lists_each_command_once_in_one_list(desk):
    helped = desk.admin("帮助")
    assert helped == help_text(ac.HELP_LINE)
    for word in ("确认接群", "不接群", "做手卡", "合作单指令", "帮助 / help"):
        assert helped.count(word) == 1, word
    lines = helped.split("\n")
    assert lines.index(ac.HELP_LINE.strip("\n")) == lines.index("  帮助 / help                 显示本帮助") - 1
    assert help_text("") == HELP_TEXT
    listed = desk.admin("指令")
    assert listed == ac.HELP_TEXT and "发「帮助」看" in listed and "确认接群" in listed


def test_the_relays_help_lists_each_command_once(monkeypatch):
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
    config = BotConfig(claude_relay="on", prm_copilot="on", prm_wechat_commands="on", admin_wxid=ADMIN)
    Bot._start_claude_relay(config, SimpleNamespace(_admin=AdminCommandHandler()),
                            SimpleNamespace(send_text=lambda chat, text: True))
    helped = seen["extra_help"]
    assert helped == RELAY_HELP + ac.RELAY_LINE
    for word in ("确认接群 群名", "不接群 群名", "做手卡", "合作单指令"):
        assert helped.count(word) == 1, word
    assert not any(line.startswith(" ") for line in helped.split("\n"))     # (one style: no indented line)


def test_a_group_command_without_the_copilot_is_still_a_command(clock, tmp_path):
    """「确认接群 群名」 like 「接群」 and 「不接群」: with no copilot it says so, never a task for the Mac."""
    from src.admin import NO_GROUP_LINKS

    handler = AdminCommandHandler()
    for line in ("接群", "不接群 小美群", f"确认接群 {GROUP_NAME}"):
        assert handler.handle(line) == NO_GROUP_LINKS and handler.is_command(line), line


def test_the_group_answers_name_a_deal_as_the_commands_do_never_like_a_draft(clock, tmp_path):
    """In the admin's chat 「#N」 is a draft's number (「发 N」 acts on it): the 接群 answer and the scan's 「已自动接群」
    write her deal as 合作单指令 does, 「合作单 N 号」."""
    d = make(clock, tmp_path)
    try:
        answer = link_by_hand(d)
        assert f"合作单 {deal_id(d)} 号" in answer and "#" not in answer
        assert d.admin("不接群 " + GROUP_NAME).startswith("不接了")
    finally:
        d.stop()
    d = make(clock, tmp_path)
    try:
        auto_linked(d)
        [notice] = [t for t in d.told() if "已自动接群" in t]
        assert f"合作单 {deal_id(d)} 号" in notice and "#" not in notice
    finally:
        d.stop()


# ── merge of e907574 (re-review N1–N6): a target that changes after the approval, with 微信里审批草稿 ─────────

def she_leaves(d, chat=GROUP):
    """She leaves the group; a round of the scan says so on her deal (her side may not be reached there).  (With
    PRM_BOSS_GROUPS on the boss scan's first round only keeps its baseline: a minute later it looks at the groups.)"""
    d.backend.groups[chat]["members"].remove(CREATOR)
    key = bg.LEFT_NOTICE + chat
    for _ in range(3):
        d.clock.now += bg.BOSS_SCAN_SEC + 1
        d.prm.boss_groups.sweep()
        if any(n["key"] == key for n in d.prm.store.deal(deal_id(d))["notices"]):
            return
    raise AssertionError("her leaving the group was not seen")


def notices_of(d, number) -> list:
    return [t for t in d.told() if t.startswith(f"#{number} 草稿")]


def test_a_draft_approved_in_wechat_whose_target_changed_is_told_again_with_the_new_target(clock, tmp_path):
    """Her side last spoke in her team's group: a 补发货盘 the admin ordered in WeChat goes there and is approved there
    (quoting its notice); she leaves the group before it goes.  Nothing is sent; the draft is back in 待审批 aimed at her
    private chat, and the admin is told again (「第 2 次通知」) with the new target and why; the old notice's 1 sends
    nothing, the fresh one's sends it where it says."""
    d = make(clock, tmp_path)
    try:
        link_by_hand(d)
        spoke(d, "private")
        spoke(d, "group")
        assert "已派补发货盘" in d.admin("补发货盘 小美")
        packet = drafted(d, open_job(d, GROUP)["id"], text="老师，货盘再发您一份～")
        d.review.sweep()
        old = head(d, packet["id"])
        assert "已批准" in d.admin("1", quote=old)
        assert d.prm.store.packet(packet["id"])["meta"]["decided_chat"] == GROUP    # (from WeChat too)
        she_leaves(d)
        assert d.prm.send_approved() == 0 and d.to(GROUP) == [] and d.to(CREATOR) == []
        back = d.prm.store.packet(packet["id"])
        assert (back["status"], back["route"], back["chat_id"]) == ("pending", "review", CREATOR)
        assert back["meta"]["hold"] == "发送对象变了：现在会发到她的私聊，请再看一眼" and back["meta"]["huopan"] is True
        assert d.prm.store.wechat_review(packet["id"])["state"] == "notified"
        stale = d.admin("1", quote=old)                                   # (before the fresh notice is out)
        assert "这份草稿在通知以后有变化" in stale and d.prm.store.packet(packet["id"])["status"] == "pending"
        assert d.review.sweep() == 1
        [first, fresh] = notices_of(d, packet["id"])
        assert fresh.startswith(f"#{packet['id']} 草稿（第 2 次通知）")
        assert "发送对象变了：现在会发到她的私聊" in fresh.split("\n")[1] and "私聊" in fresh.split("\n")[0]
        assert "这份草稿在通知以后有变化" in d.admin("1", quote=old)       # (the old notice: nothing)
        assert d.prm.store.packet(packet["id"])["status"] == "pending"
        assert "已批准" in d.admin("1", quote=fresh.split("\n")[0])
        d.prm.send_approved()
        assert d.to(CREATOR) == ["老师，货盘再发您一份～"] and d.to(GROUP) == []
    finally:
        d.stop()


def test_a_draft_approved_in_the_console_whose_target_changed_is_told_again_too(clock, tmp_path):
    d = make(clock, tmp_path)
    try:
        link_by_hand(d)
        spoke(d, "private")
        spoke(d, "group")
        assert "已派补发货盘" in d.admin("补发货盘 小美")
        packet = drafted(d, open_job(d, GROUP)["id"])
        d.review.sweep()
        assert d.prm.decide(packet["id"], "send")["ok"]
        assert d.prm.store.packet(packet["id"])["meta"]["decided_chat"] == GROUP
        she_leaves(d)
        assert d.prm.send_approved() == 0
        assert d.review.sweep() == 1
        assert notices_of(d, packet["id"])[-1].startswith(f"#{packet['id']} 草稿（第 2 次通知）")
    finally:
        d.stop()


@pytest.mark.parametrize("typed", ["写开场白 小美", "做手卡 小美", "补发货盘 小美"])
def test_what_a_command_ordered_keeps_waiting_when_it_comes_back(clock, tmp_path, typed):
    """A 开场白 / 做手卡 / 补发货盘 ordered in WeChat, approved for her group, bounced by the target change: back in
    待审批 it still never goes by itself (自动模式, 开场白自动发 on; hours later), and keeps what it was made with."""
    from test_prm_admin_commands import cards_drafted

    d = make(clock, tmp_path)
    try:
        link_by_hand(d)
        spoke(d, "private")
        spoke(d, "group")
        d.prm.set_auto_mode(True)
        assert "已派" in d.admin(typed), typed
        job = open_job(d, GROUP)
        if typed.startswith("做手卡"):
            packet = cards_drafted(d, job)
        else:
            packet = drafted(d, job["id"], text="老师您好～", kind="greeting" if typed.startswith("写开场白") else "faq")
        assert packet["status"] == "pending" and packet["chat_id"] == GROUP and "auto_at" not in packet["meta"]
        d.review.sweep()
        assert "已批准" in d.admin("1", quote=head(d, packet["id"]))
        she_leaves(d)
        assert d.prm.send_approved() == 0
        back = d.prm.store.packet(packet["id"])
        assert (back["status"], back["chat_id"]) == ("pending", CREATOR)
        assert "auto_at" not in back["meta"] and back["meta"]["hold"].startswith("发送对象变了")
        kind = {"写开场白": "opening", "做手卡": "handcards", "补发货盘": "huopan"}[typed.split()[0]]
        assert back["meta"].get(kind), back["meta"]
        assert back["attachments"] == packet["attachments"]
        d.clock.now += 3 * 3600
        assert d.prm.send_due_openings() == 0 and d.prm.send_approved() == 0
        assert d.prm.store.packet(packet["id"])["status"] == "pending" and d.to(CREATOR) == [] and d.to(GROUP) == []
    finally:
        d.stop()


def test_a_platform_drafts_reply_never_comes_back_to_be_approved_again(clock, tmp_path):
    """A 专属佣金 step's reply, its action verified, approved for her group: she left the group before it went.  Back
    in 待审批 it would offer the platform action once more: it fails instead, the reply for a person to send by hand."""
    d = make(clock, tmp_path)
    try:
        link_by_hand(d)
        spoke(d, "private")
        spoke(d, "group")
        deal = deal_id(d)
        number = d.prm.store.add_packet(
            deal_id=deal, party_id=d.her_party["id"], job_id=None, chat_id=GROUP, kind="other", text="老师专属佣金开好啦～",
            attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
            status="approved", route="review", decided_at=d.clock.now, tool="dy.designated_commission",
            platform={"execution": {"outcome": "VERIFIED_SUCCESS"}},
            meta={"channel": "group", "group_name": GROUP_NAME, "group_manual": True, "reach": "last_spoke",
                  "lane": "system"}, now=d.clock.now)
        she_leaves(d)
        assert d.prm.send_approved() == 0 and d.to(GROUP) == [] and d.to(CREATOR) == []
        packet = d.prm.store.packet(number)
        assert packet["status"] == "failed"
        assert packet["error"].startswith("平台动作已经核验成功，但回复没发出去：发送对象变了：现在会发到她的私聊")
    finally:
        d.stop()


def test_the_status_says_it_cannot_tell_when_the_backend_cannot_say_whether_she_has_a_private_chat(clock, tmp_path):
    d = make(clock, tmp_path, prm_reach_last_spoke="off")
    try:
        link_by_hand(d)
        spoke(d, "group")                                                 # (no line of hers in private)
        d.backend.readable = False
        assert d.prm.boss_groups.knows_private(CREATOR) is None
        line = next(x for x in status(d) if x.startswith("回复："))
        assert line.endswith("机器人主动发的（开场白、货盘、手卡、发货通知）现在说不准发到哪：机器人现在读不出她有没有私聊，先按私聊算")
        assert d.prm._reach_out(d.prm.store.party_by_wxid(CREATOR), d.prm.store.deal(deal_id(d))) == {
            "where": "private", "why": "unknown"}
        d.backend.readable = True
        line = next(x for x in status(d) if x.startswith("回复："))
        assert f"发到群「{GROUP_NAME}」（她没有和我们的私聊）" in line, line
    finally:
        d.stop()
