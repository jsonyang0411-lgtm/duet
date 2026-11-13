"""老板拉的群里机器人自己选人 (PRM_BOSS_GROUP_PICK, 2026-11-13; src/prm/boss_groups.py ``_pick_pending``).

The real case: the boss pulled this account into a small group with a creator's team (us, the boss, the creator, her
assistant, one more).  The bot saw it was a 老板拉的群, but with more than one person who may be her it waited for the
console's 「是她」; meanwhile her assistant asked for an exclusive link and a 手卡 and nothing was drafted.  The user: 「不是
改为了群里所有的人都当达人吗」, 「我是觉得有点多余」; and 「一个群里全是他的不同助手负责不同工作也要专门指定一个是达人吗」.

With the switch on (and PRM_BOSS_GROUPS, PRM_GROUP_HER_SIDE) the scan takes such a group over itself: the deal hangs on
the one member whose name the group's name has (two of them: no match), else on the first of her side to speak there,
else it waits until one speaks.  The deal's 称呼 is the group's name, cleaned; the member is only its hook.  Every draft
there waits for a person until one confirms (as a group the scan linked by itself); the admin is told once; a wrong hook
is moved with 「接群 <群名>」 + 「是 N」 or the console card's 「改挂」, and when the hook leaves the group the deal moves to
another of her side by the same rules.  Groups with a registered creator in them, the switch off, or her side off: as
before.

The backend is the auto-link tests' fake (session list, members, lines); the relay, the PRM service and the admin
handler are real.  Every name, id and number is made up: the creator is 小美, the boss 老板测试, our brand 测试品牌.
"""

import json
import time

import pytest

from test_prm import ADMIN, CREATOR, Clock, draft
from test_prm_boss_groups import BOSS, BOSS_ID, ME, make, restart
from test_prm_groups_auto_link import AutoBackend

from src.admin import AdminCommandHandler
from src.prm import boss_groups as bg
from src.prm.service import HER_SIDE_HELD, PICK_UNCONFIRMED_HELD, SETTLE_SEC

PG = "500000001@chatroom"
TITLE = "小美&测试品牌 抖音"
ASSIST = "wxid_assist05"                   # her assistant
OTHER = "wxid_other05"                     # one more of her team
BRAND = "测试品牌"


class PickBackend(AutoBackend):
    """The auto-link tests' fake backend; its boss scan gives the members' names as this fake knows them."""

    def boss_groups(self, members_any, known, max_members):
        self.calls.append("boss_groups")
        if not self.readable:
            return None
        found = []
        for chat, g in self.groups.items():
            if chat in known:
                continue
            if chat in self.unreadable:
                found.append({"chat": chat, "unreadable": True})
                continue
            if set(g["members"]) & set(members_any) and len(g["members"]) <= max_members:
                found.append({"chat": chat, "name": g["name"], "created_at": g.get("created_at"),
                              "members": [{"wxid": w, "name": self.names.get(w, "")} for w in g["members"]],
                              **{k: g[k] for k in ("joined_at", "history_complete") if k in g}})
        return list(reversed(found))


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 13, 14, 30, 0, 0, 0, -1)))


def setup(clock, tmp_path, *, pick=True, side=True, backend=None, **prm):
    b = make(clock, tmp_path, backend=backend or PickBackend(), prm_boss_group_pick="on" if pick else "off",
             prm_friend_add_brand=BRAND, **({"prm_group_her_side": "on"} if side else {}), **prm)
    b.backend.names.update({CREATOR: "小美", ASSIST: "测试助理", OTHER: "路人测试"})
    wire(b)
    return b


def wire(b):
    handler = AdminCommandHandler()
    handler.set_group_links(b.prm.group_links)
    b.say = handler.handle


@pytest.fixture
def bot(clock, tmp_path):
    b = setup(clock, tmp_path)
    yield b
    stop(b)


def stop(b):
    b.prm.stop()
    b.relay.stop()


def boss_makes(b, members=(ME, BOSS_ID, CREATOR, ASSIST, OTHER), name=TITLE, lines=(), chat=PG):
    """The first scan keeps the baseline; the boss then makes the group (WeChat's line that we were added), ``lines``
    ``(sender, text)`` are said in it, one second apart; the next scan sees it: what it took over."""
    if b.prm.store.meta_value(bg.BASELINE) is None:
        assert b.prm.boss_groups.scan() == 0
    t = b.clock.now
    b.backend.groups[chat] = {"name": name, "members": list(members), "joined_at": t, "history_complete": True,
                              "created_at": t}
    b.backend.active[chat] = t
    for n, (sender, text) in enumerate(lines, 1):
        b.backend.say(chat, sender, text, t + n)
    b.clock.now += bg.BOSS_SCAN_SEC + 1
    return b.prm.boss_groups.scan()


def scan(b):
    b.clock.now += bg.BOSS_SCAN_SEC + 1
    return b.prm.boss_groups.scan()


def told(b):
    return [text for chat, text in b.texts if chat == ADMIN]


def gmsg(b, text, *, sender=CREATOR, chat=PG):
    """A live line of the group as the watch hands it on (said in the fake's lines too)."""
    line = b.backend.say(chat, sender, text, b.clock.now)
    return {**line, "message_id": line["message_id"] + "w"}


def job_of(b, chat=PG):
    return b.relay.store.latest_open(chat, drafts_only=True, system=False)


def drafted(b, reply="您好～我看一下", chat=PG):
    job = job_of(b, chat)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    b.finish(job["id"], draft(message_kind="faq", reply_draft=reply, slots={}, stage_proposal="",
                              reason="kb/合作政策.md"))
    return job, b.prm.store.packet_for_job(job["id"])


def no_opening(b, chat=PG):
    job = job_of(b, chat)
    assert job is not None and json.loads(job["meta"])["opening"] == {"how": "boss_group", "by": "boss_group"}
    assert b.relay.store.cancel_if_pending(job["id"], b.clock.now)


def hook(b, chat=PG):
    row = b.prm.store.boss_group(chat)
    return b.prm.store.party(int(row["party_id"])) if row and row.get("party_id") else None


# ── how a group's name is read ───────────────────────────────────────────────

def test_a_group_name_is_cleaned_of_our_brand_the_boss_and_the_platform_words():
    drop = [BRAND, BOSS, "商务号"]
    assert bg.title_parts(TITLE, drop) == ["小美"]
    assert bg.title_parts("测试品牌×小美·老板测试（抖音达人合作群）", drop) == ["小美"]
    assert bg.title_parts("ＬＩＬＹ和测试品牌【小红书】", drop) == ["LILY"]          # (full-width letters: as half-width)
    assert bg.title_parts("测 试 品 牌 | 小美 冬季直播对接", drop) == ["小美", "冬季"]
    assert bg.title_parts("", drop) == [] and bg.title_parts("抖音合作群", drop) == []
    assert bg.title_call(["小美"]) == "小美" and bg.title_call(["小美", "冬季"]) == "小美 冬季"
    for nothing in ([], ["美"], ["2026", "11"], ["8"]):
        assert bg.title_call(nothing) == "", nothing
    assert bg.title_call(["一二三四五六七八九十"] * 4) == " ".join(["一二三四五六七八九十"] * 4)[:bg.NAME_MAX]


def test_a_member_matches_by_a_shared_run_of_two_characters_never_of_digits_only():
    drop = [BRAND, BOSS]
    parts = bg.title_parts("小美 2026 冬季", drop)
    assert bg.title_has(parts, "小美") and bg.title_has(parts, "测试品牌·小美的号", drop)
    assert bg.title_has(bg.title_parts("Lily&测试品牌", drop), "lily_official")
    assert not bg.title_has(parts, "美丽")                  # (one character in common is no match)
    assert not bg.title_has(parts, "2026")                  # (digits only)
    assert not bg.title_has(parts, "美冬")                   # (never across two pieces of the name)
    assert not bg.title_has(parts, "老板测试", drop) and not bg.title_has(parts, "")


# ── (a) the group's name has her name ────────────────────────────────────────

def test_the_group_name_says_whose_group_it_is(bot):
    """Nobody said anything yet: the deal hangs on 小美 (the only one the name has), named after the group, unconfirmed;
    the group 开场白 is drafted (it always waits); the admin is told once, and how to change it."""
    assert boss_makes(bot) == 1
    row = bot.prm.store.boss_group(PG)
    party = hook(bot)
    deal = bot.prm.store.deal_of(party["id"])
    assert (row["state"], row["picked"], party["wxid"]) == ("linked", "title", CREATOR)
    assert (party["name"], party["note"]) == ("小美", f"老板介绍（老板拉的群：{TITLE}）")
    assert deal["three_party"]["chat"] == PG and deal["three_party"]["by"] == "boss"
    assert bot.prm.boss_groups.unconfirmed(PG) and bot.prm.boss_groups.picked(PG) == "title"
    assert json.loads(job_of(bot)["meta"])["opening"] == {"how": "boss_group", "by": "boss_group"}
    [ev] = bot.prm.store.events(deal["id"], kinds=(bg.EVENT,))
    assert ev["detail"] == {"name": TITLE, "chat": PG, "new": True, "picked": "title"} and ev["actor"] == "system"
    assert told(bot) == [
        f"已自动接手老板拉的群「{TITLE}」（5 人）：合作单「小美」（{deal['id']} 号，新开的）挂在「小美」身上，因为群名里有她的名字；"
        "群里她那边的 3 个人说的话都一样起草（我们自己、老板说的不起草），私聊只算「小美」的；确认以前这个群的草稿都等你看。"
        f"对的话回「确认接群 {TITLE}」；挂错了人回「接群 {TITLE}」改挂；不是合作群就回「不接群 {TITLE}」"]
    assert scan(bot) == 0 and len(told(bot)) == 1                   # (said once)
    shown = bot.prm.snapshot()
    assert shown["boss_groups"]["pick"] is True and shown["boss_groups"]["pending"] == []
    assert shown["boss_groups"]["picked"] == [{
        "chat": PG, "name": TITLE, "size": 5, "deal_id": deal["id"], "call": "小美", "how": "群名里有她的名字",
        "hook": {"wxid": CREATOR, "name": "小美"},
        "members": [{"wxid": ASSIST, "name": "测试助理"}, {"wxid": OTHER, "name": "路人测试"}]}]
    [card] = [p for p in shown["parties"] if p["id"] == party["id"]]
    assert card["deal"]["unconfirmed_groups"] == [{"chat": PG, "name": TITLE, "picked": "群名里有她的名字"}]


@pytest.mark.parametrize("name, other", [("测试品牌&小美", "测试品牌粉丝"), ("老板测试·小美", "老板测试助理")])
def test_our_brand_and_the_bosss_name_never_make_a_match(clock, tmp_path, name, other):
    """Without taking them out, the brand (or the boss's name) in the group's name would match the other member too:
    two matches, no match.  Taken out, only 小美 matches."""
    b = setup(clock, tmp_path)
    try:
        b.backend.names[OTHER] = other
        assert boss_makes(b, members=(ME, BOSS_ID, CREATOR, OTHER), name=name) == 1
        assert (hook(b)["wxid"], hook(b)["name"], b.prm.store.boss_group(PG)["picked"]) == (CREATOR, "小美", "title")
    finally:
        stop(b)


# ── (b) the first of her side to speak ───────────────────────────────────────

def test_two_names_in_the_group_name_is_no_match_the_first_to_speak_is(clock, tmp_path):
    """The real case: 小美 and 小美助理 both match the group's name; the boss introduced them, her assistant spoke first,
    then she did.  The deal hangs on the assistant, still named 小美 (the group's name); what waited is one draft that
    waits for a person, the boss's introduction its context."""
    b = setup(clock, tmp_path)
    try:
        b.backend.names[ASSIST] = "小美助理"
        assert boss_makes(b, lines=[(BOSS_ID, "介绍一下，这是小美老师的团队"), (ASSIST, "老师好，要个专属链接和手卡"),
                                    (CREATOR, "辛苦啦")]) == 1
        party = hook(b)
        assert (party["wxid"], party["name"], b.prm.store.boss_group(PG)["picked"]) == (ASSIST, "小美", "first")
        job = job_of(b)
        meta = json.loads(job["meta"])
        assert meta["link_first"] is True and "opening" not in meta
        assert job["text"].split("\n")[0].endswith("老师好，要个专属链接和手卡")
        assert job["text"].split("\n")[1].endswith(bg.HER_SIDE_MARK + "辛苦啦")    # (小美 is her side now)
        assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("老板", "介绍一下，这是小美老师的团队")]
        deal = b.prm.store.deal_of(party["id"])
        assert told(b) == [
            f"已自动接手老板拉的群「{TITLE}」（5 人）：合作单「小美」（{deal['id']} 号，新开的）挂在「小美助理」身上，因为她那边第一个在"
            "群里说话的人；群里她那边的 3 个人说的话都一样起草（我们自己、老板说的不起草），私聊只算「小美助理」的；群里最近 1 句记成了背景；"
            "她那边最后 2 句还没人回，派了一份草稿；确认以前这个群的草稿都等你看。"
            f"对的话回「确认接群 {TITLE}」；挂错了人回「接群 {TITLE}」改挂；不是合作群就回「不接群 {TITLE}」"]
        _, packet = drafted(b, "好的老师，链接和手卡马上安排")
        assert (packet["status"], packet["route"]) == ("pending", "review") and packet["chat_id"] == PG
        assert packet["meta"]["hold"] == HER_SIDE_HELD                 # (a line of her side in it: that reason first)
    finally:
        stop(b)


def test_nobody_of_her_side_spoke_it_waits_and_is_taken_when_one_does(bot):
    """No name in the group's name: it waits (the card and 「接群」 say why); its lines are not read again while nothing is
    said there; the first of her side to speak is the hook, and the 称呼 is that member's name (the group's name has
    nothing usable)."""
    assert boss_makes(bot, name="抖音合作群", lines=[(BOSS_ID, "拉个群方便沟通")]) == 0
    assert bot.prm.store.boss_group(PG)["state"] == "pending" and bot.prm.store.parties() == []
    [pending] = bot.prm.snapshot()["boss_groups"]["pending"]
    assert pending["why"] == bg.PICK_WAIT_WHY
    assert bot.say("接群 抖音合作群") == (
        "群「抖音合作群」是老板拉的群，还没接手：群里不止一个人可能是她，群名里也认不出是谁：等她那边有人在群里说话，机器人就自己接手"
        "（合作单挂在第一个说话的人身上）。要现在接就到控制台「合作单」页顶上的「老板拉的群」卡片里点她的「是她」")
    reads = bot.backend.calls.count(f"lines:{PG}")
    assert scan(bot) == 0 and bot.backend.calls.count(f"lines:{PG}") == reads      # (nothing said since: not read)
    bot.backend.say(PG, OTHER, "老师们好", bot.clock.now)
    assert scan(bot) == 1
    party = hook(bot)
    assert (party["wxid"], party["name"]) == (OTHER, "路人测试") and told(bot)[0].startswith(
        "已自动接手老板拉的群「抖音合作群」（5 人）：合作单「路人测试」")


@pytest.mark.parametrize("why", ["off", "side_off", "registered", "stopped"])
def test_otherwise_it_waits_for_the_console_as_before_and_jie_qun_says_why(clock, tmp_path, why):
    b = setup(clock, tmp_path, pick=why != "off", side=why != "side_off")
    try:
        if why in ("registered", "stopped"):
            other = b.prm.register(OTHER, "路人测试", "测试")
            if why == "stopped":
                b.prm.set_active(other["id"], False)
        assert boss_makes(b, lines=[(ASSIST, "老师好")]) == 0
        row = b.prm.store.boss_group(PG)
        assert (row["state"], row["picked"]) == ("pending", "") and told(b) == []
        reason = {"off": bg.PICK_OFF_WHY, "side_off": bg.PICK_SIDE_WHY, "registered": bg.PICK_REGISTERED_WHY,
                  "stopped": bg.PICK_REGISTERED_WHY}[why]
        assert b.say(f"接群 {TITLE}") == (f"群「{TITLE}」是老板拉的群，还没接手：{reason}。要现在接就到控制台「合作单」页顶上的"
                                       "「老板拉的群」卡片里点她的「是她」")
        out = b.prm.snapshot()["boss_groups"]
        assert ("pick" in out) is (why in ("registered", "stopped")) and out["pending"][0]["why"] == ""
        assert b.prm.boss_groups.pick(PG, ASSIST)["ok"]                # (the console's 「是她」 as before)
        assert b.prm.store.boss_group(PG)["picked"] == "" and not b.prm.boss_groups.unconfirmed(PG)
    finally:
        stop(b)


def test_a_reason_of_its_own_still_waits_for_the_console(bot):
    """A group not certainly new (no line that we were added) waits for 「是她」 as before, whatever its name says."""
    assert bot.prm.boss_groups.scan() == 0
    bot.backend.groups[PG] = {"name": TITLE, "members": [ME, BOSS_ID, CREATOR, ASSIST], "created_at": bot.clock.now}
    assert scan(bot) == 0
    assert bot.prm.store.boss_group(PG)["why"] == bg.MAYBE_WHY
    assert bot.say(f"接群 {TITLE}").startswith(f"群「{TITLE}」是老板拉的群，还没接手：拿不准是不是新拉的群。")


# ── unconfirmed until a person says so ───────────────────────────────────────

def test_every_draft_waits_until_a_person_confirms_then_the_boss_group_rules(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.prm.store.set_gate("faq", "auto", clock.now)
        boss_makes(b)
        no_opening(b)
        b.clock.now += 30
        b.prm.handle(gmsg(b, "在吗"))
        _, packet = drafted(b)
        assert (packet["status"], packet["route"], packet["meta"]["hold"]) == ("pending", "review",
                                                                              PICK_UNCONFIRMED_HELD)
        [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == packet["id"]]
        assert shown["hold"] == PICK_UNCONFIRMED_HELD
        assert b.say(f"确认接群 {TITLE}") == (f"好的：群「{TITLE}」确认是 小美 的群了，之后的草稿和老板拉的群一样照门槛、下班时段、"
                                           "自动模式发")
        assert not b.prm.boss_groups.unconfirmed(PG)
        b.prm.decide(packet["id"], "drop")
        b.clock.now += 30
        b.prm.handle(gmsg(b, "还在吗"))
        _, packet = drafted(b, "在的～")
        assert (packet["status"], packet["route"]) == ("approved", "auto")
        b.prm.send_approved()
        assert (PG, "在的～") in b.texts
        names = [ev["detail"] for ev in b.prm.store.events(packet["deal_id"], kinds=(bg.EVENT,))]
        assert {"name": TITLE, "chat": PG, "picked": "title", "confirmed": True} in names
    finally:
        stop(b)


def test_a_draft_a_person_sends_into_the_group_confirms_it(bot):
    boss_makes(bot, lines=[(ASSIST, "老师好")])
    _, packet = drafted(bot)
    assert packet["meta"]["hold"] == HER_SIDE_HELD                  # (小美 is the hook by the name; her assistant spoke)
    assert bot.prm.decide(packet["id"], "send")["ok"]
    bot.prm.send_approved()
    assert (PG, "您好～我看一下") in bot.texts and not bot.prm.boss_groups.unconfirmed(PG)


def test_after_a_restart_it_is_still_unconfirmed_and_a_line_wechat_refused_is_sent_then(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.send_ok = False
        boss_makes(b)
        assert told(b) and json.loads(b.prm.store.meta_value(bg.AUTO_STATE))["owed"]
        b.send_ok = True
        backend = PickBackend()
        backend.groups, backend.names, backend.lines, backend.active = (b.backend.groups, b.backend.names,
                                                                        b.backend.lines, b.backend.active)
        restart(b, backend)
        wire(b)
        assert b.prm.boss_groups.unconfirmed(PG)
        b.texts.clear()
        scan(b)
        assert [t[:12] for t in told(b)] == ["已自动接手老板拉的群「小"]
    finally:
        stop(b)


def test_another_deal_of_the_same_name_is_kept_apart_and_the_notice_says_so(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        before = b.prm.register("wxid_samename09", "小美", "测试")
        boss_makes(b)
        party = hook(b)
        assert party["id"] != before["id"] and party["name"] == before["name"] == "小美"
        assert f"另有合作单也叫「小美」（{b.prm.store.deal_of(before['id'])['id']} 号），没合并，两边分开记" in told(b)[0]
    finally:
        stop(b)


# ── a wrong hook: moved by a person, or by the scan when the hook leaves ────────

def test_jie_qun_then_shi_n_moves_the_deal_to_another_member(bot):
    boss_makes(bot)
    party = hook(bot)
    deal = bot.prm.store.deal_of(party["id"])
    changed = bot.changed
    question = bot.say(f"接群 {TITLE}").split("\n")
    assert question == [
        f"群「{TITLE}」是机器人自己接手的老板拉的群（还没确认）：合作单「小美」（{deal['id']} 号）挂在「小美」身上（群名里有她的名字）。"
        "改挂到谁？回「是 编号」，「不接」作罢（30 分钟内有效）：", "1. 测试助理", "2. 路人测试", f"挂得对的话回「确认接群 {TITLE}」"]
    assert bot.say("是 1") == (f"改好了：群「{TITLE}」的合作单「小美」改挂在「测试助理」身上，私聊只算 TA 的；群里她那边的人照旧都起草。"
                              "这个群也算确认过了")
    moved = hook(bot)
    assert (moved["id"], moved["wxid"], moved["name"]) == (party["id"], ASSIST, "小美")
    assert bot.prm.store.deal_of(moved["id"])["id"] == deal["id"] and bot.changed > changed
    assert not bot.prm.boss_groups.unconfirmed(PG) and bot.prm.store.party_by_wxid(CREATOR) is None
    assert bot.prm.snapshot()["boss_groups"].get("picked") is None
    # (confirmed: 「接群」 still asks 「改挂到谁」; 「不接」 leaves it as it is and refuses nothing)
    assert bot.say(f"接群 {TITLE}").startswith(f"群「{TITLE}」是机器人自己接手的老板拉的群（确认过了）：合作单「小美」")
    assert bot.say("不接") == f"好的，不改了：群「{TITLE}」的合作单还挂在原来的人身上"
    assert hook(bot)["wxid"] == ASSIST and not bot.prm.boss_groups.refused(PG)


def test_the_console_card_moves_it_and_never_onto_a_registered_member(bot):
    boss_makes(bot)
    for wxid in (BOSS_ID, ME, ADMIN, CREATOR, "wxid_nobody"):
        assert not bot.prm.boss_groups.pick(PG, wxid)["ok"], wxid
    out = bot.prm.boss_groups.pick(PG, OTHER)
    assert out["ok"] and (out["was"], out["now"], out["name"]) == ("小美", "路人测试", "小美")
    assert hook(bot)["wxid"] == OTHER and not bot.prm.boss_groups.unconfirmed(PG)
    deal_id = bot.prm.store.boss_group(PG)["deal_id"]
    names = [ev["detail"] for ev in bot.prm.store.events(deal_id, kinds=(bg.EVENT,))]
    assert {"name": TITLE, "chat": PG, "picked": "title", "rehooked": "console"} in names


def test_not_moved_while_the_hooks_private_chat_has_a_draft_on_its_way(bot):
    boss_makes(bot)
    no_opening(bot)
    bot.prm.handle(bot.msg("私聊问一下"))                             # (the hook writes in private)
    out = bot.prm.boss_groups.pick(PG, ASSIST)
    assert not out["ok"] and "正在写" in out["error"] and hook(bot)["wxid"] == CREATOR
    drafted(bot, chat=CREATOR)
    out = bot.prm.boss_groups.pick(PG, ASSIST)
    assert not out["ok"] and "草稿在等" in out["error"]


def test_when_the_hook_leaves_the_deal_moves_to_another_of_her_side(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        b.backend.names[ASSIST] = "小美助理"
        boss_makes(b, lines=[(ASSIST, "老师好"), (CREATOR, "你好呀")])
        assert hook(b)["wxid"] == ASSIST
        b.texts.clear()
        b.backend.groups[PG]["members"].remove(ASSIST)
        scan(b)
        party = hook(b)
        assert party["wxid"] == CREATOR and party["name"] == "小美"
        deal = b.prm.store.deal_of(party["id"])
        assert not any(n["key"] == bg.LEFT_NOTICE + PG for n in deal["notices"])
        assert told(b) == [
            f"群「{TITLE}」（4 人）里合作单「小美」（{deal['id']} 号）挂着的「小美助理」退群了：改挂在「小美」身上（群名里有她的名字）；"
            "群里她那边的人说的话照旧都起草，私聊只算「小美」的。"
            f"对的话回「确认接群 {TITLE}」；挂错了人回「接群 {TITLE}」改挂；不是合作群就回「不接群 {TITLE}」"]
        assert b.prm.boss_groups.unconfirmed(PG)                       # (the scan moved it: still a person's to confirm)
    finally:
        stop(b)


def test_when_nobody_else_can_be_chosen_the_deal_says_so_until_one_speaks(clock, tmp_path):
    b = setup(clock, tmp_path)
    try:
        boss_makes(b, name="抖音合作群", lines=[(ASSIST, "老师好")])
        assert hook(b)["wxid"] == ASSIST
        b.backend.groups[PG]["members"].remove(ASSIST)
        scan(b)
        deal = b.prm.store.deal_of(hook(b)["id"])
        [notice] = [n for n in deal["notices"] if n["key"] == bg.LEFT_NOTICE + PG]
        assert notice["text"] == bg.PICK_LEFT_TEXT and hook(b)["wxid"] == ASSIST
        b.clock.now += 30
        assert b.prm.handle(gmsg(b, "样品到了", sender=OTHER)) is True     # (her side: still drafted)
        assert job_of(b) is not None
        scan(b)
        assert hook(b)["wxid"] == OTHER
        assert not any(n["key"] == bg.LEFT_NOTICE + PG for n in b.prm.store.deal_of(hook(b)["id"])["notices"])
    finally:
        stop(b)


def test_a_line_of_her_side_from_before_prm_catchup_hours_is_not_dropped_silently(clock, tmp_path):
    """The group waited on the card for a day: what her assistant asked then is read all the same (from when we were
    added, not only PRM_CATCHUP_HOURS back): too old to draft, it is recorded and her deal asks a person to look."""
    b = setup(clock, tmp_path)
    try:
        assert b.prm.boss_groups.scan() == 0
        t = b.clock.now
        b.backend.groups[PG] = {"name": "抖音合作群", "members": [ME, BOSS_ID, CREATOR, ASSIST], "joined_at": t,
                                "history_complete": True, "created_at": t}
        b.backend.say(PG, ASSIST, "老师好，要个专属链接", t + 5)
        b.backend.active[PG] = t + 5
        b.clock.now += 13 * 3600
        assert b.prm.boss_groups.scan() == 1
        assert hook(b)["wxid"] == ASSIST and b.backend.asked_since == t
        deal = b.prm.store.deal_of(hook(b)["id"])
        assert any(n["key"] == "missed" for n in deal["notices"])
        job = job_of(b)                                                # (no reply job: only the group 开场白, which waits)
        assert job is not None and json.loads(job["meta"])["opening"] == {"how": "boss_group", "by": "boss_group"}
    finally:
        stop(b)
