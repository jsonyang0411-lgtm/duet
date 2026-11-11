"""接手任何合作群 (2026-11-09, src/prm/group_link.py and the manual part of src/prm/boss_groups.py): the operator links any
group to a creator by hand, from the admin's WeChat chat (「接群」, 「接群 N / 群名」, 「是 N」, 「不接」, 「不接群 群名」) or the
console's 「是她」 on the open question.  From then on it works like a 老板拉的群 the bot took over (her lines drafted and
sent in the group, everybody else's context only, our own lines typed by a person take her deal over), whatever
PRM_BOSS_GROUPS says, with no 开场白: what was said there lately is context, and her lines nobody answered are one draft.

The WeChat backend is test_prm_boss_groups' fake with what 接群 asks besides (``recent_groups``, ``group_lines``); the
admin handler, the relay and the PRM service are real.  Every name, id and number is made up: she is 小美, her 商务
测试商务, the boss 老板测试.
"""

import itertools
import json
import logging
import time

import pytest

from test_prm import ADMIN, CREATOR, draft
from test_prm_boss_groups import BOSS, BOSS_ID, HELPER, ME, NAMES, Backend, make, restart

from src.admin import NO_GROUP_LINKS, AdminCommandHandler
from src.prm import boss_groups as bg
from src.prm import group_link as gl
from src.prm.service import (GROUP_CLOSED_HEAD, GROUP_CLOSED_TEXT, GROUP_UNSENT, HER_RECALL_SEC, LINK_TAIL,
                              REDRAFT_GAP_SEC, REDRAFT_MAX, SETTLE_SEC, UNREACHABLE_TEXT, event_out)
from src.wechat.window_controller import SendRefused

GROUP = "400000001@chatroom"              # a group her 商务 made with her and us
GROUP_NAME = "小美合作对接群"
BIG = "400000002@chatroom"                # a big group of the brand's
NAMELESS = "400000003@chatroom"           # a group nobody named
OURS = "400000004@chatroom"               # her deal's own 三方群 (「拉三方群」 made it)
OTHER = "400000005@chatroom"
BD = "wxid_bd01"                          # her 商务
COLLEAGUE = "wxid_colleague01"            # one of the brand's people
TESTER = "wxid_tester02"
_IDS = itertools.count(1)


class LinkBackend(Backend):
    """test_prm_boss_groups' fake backend, plus what 接群 asks: the session list's groups, the one active last first
    (``recent_groups``: ``active`` per group), and a group's lines since a time (``group_lines``: the lines a test wrote
    with ``say``, this account's marked ``is_own``)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.groups = {}
        self.names = dict(NAMES, **{BD: "测试商务", COLLEAGUE: "品牌同事", TESTER: "测试达人二号"})
        self.lines: dict[str, list] = {}
        self.active: dict[str, float] = {}

    def group_members(self, chat):
        self.calls.append("group_members")
        g = self.groups.get(chat)
        return (None if not self.readable or g is None else
                [{"wxid": w, "name": self.names.get(w, "")} for w in g["members"]])

    def recent_groups(self, limit=10, skip=(), name=""):
        self.calls.append("recent_groups")
        if not self.readable:
            return None
        skip, out = set(skip or ()), []
        for chat in sorted(self.groups, key=lambda c: -self.active.get(c, 0)):
            g = self.groups[chat]
            if chat in skip or (name and bg._key(g["name"]) != bg._key(name)):
                continue
            out.append({"chat": chat, "name": g["name"], "size": len(g["members"]), "last_at": self.active.get(chat)})
            if limit and len(out) >= limit:
                break
        return out

    def group_lines(self, chat, since, voices=()):
        self.calls.append(f"lines:{chat}")
        if not self.readable or chat in self.unreadable:
            return None
        self.asked_since = since
        return [dict(m) for m in self.lines.get(chat, []) if m["timestamp"] >= since]

    def say(self, chat, sender, text, ts, **extra):
        """A line of a group, as the backend standardizes it (this account's: ``is_own``)."""
        line = {"message_id": f"l{next(_IDS)}", "chat_id": chat, "group_name": self.groups[chat]["name"],
                "sender_id": sender, "sender_name": self.names.get(sender, ""), "content": text, "msg_type": 1,
                "is_group": True, "timestamp": ts, **extra}
        line["is_own" if sender == ME else "_not_self_by_id"] = True
        self.lines.setdefault(chat, []).append(line)
        self.active[chat] = max(self.active.get(chat, 0), ts)
        return line


@pytest.fixture
def clock():
    from test_prm import Clock

    return Clock(time.mktime((2026, 11, 9, 14, 30, 0, 0, 0, -1)))


def groups(b):
    """The session list: her 商务's group (her, her 商务, a colleague of ours, us), a big group, a nameless one."""
    t = b.clock.now
    b.backend.groups.update({
        GROUP: {"name": GROUP_NAME, "members": [ME, CREATOR, BD, COLLEAGUE]},
        BIG: {"name": "品牌测试大群", "members": [ME, BOSS_ID, CREATOR, *(f"wxid_guest{n:02d}" for n in range(9))]},
        NAMELESS: {"name": "", "members": [ME, TESTER]}})
    b.backend.active.update({GROUP: t - 600, BIG: t - 3600, NAMELESS: t - 7200})


def setup(clock, tmp_path, **prm):
    """The bot with PRM_BOSS_GROUPS off (the default: only a link by hand reads a group), the admin handler wired as
    Bot._wire_friend_adds does; ``say(text)`` is the admin's line in his chat, the answer back."""
    prm.setdefault("on", False)
    b = make(clock, tmp_path, backend=LinkBackend(), **prm)
    handler = AdminCommandHandler()
    handler.set_group_links(b.prm.group_links)
    b.admin = handler
    b.say = handler.handle
    groups(b)
    return b


@pytest.fixture
def bot(clock, tmp_path):
    b = setup(clock, tmp_path)
    yield b
    b.prm.stop()
    b.relay.stop()


def job_of(b, chat=GROUP):
    return b.relay.store.latest_open(chat, drafts_only=True, system=False)


def drafted(b, reply="好的～我看一下", chat=GROUP):
    """The Mac takes the newest job of ``chat`` and drafts it: the job and its packet."""
    job = job_of(b, chat)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    b.finish(job["id"], draft(message_kind="faq", reply_draft=reply, slots={}, stage_proposal="",
                              reason="kb/合作政策.md"))
    return job, b.prm.store.packet_for_job(job["id"])


def gmsg(b, text, *, sender=CREATOR, chat=GROUP, ts=None, **extra):
    """A live line of a linked group as the watch hands it to the route."""
    return {"message_id": f"g{next(_IDS)}", "chat_id": chat, "group_name": GROUP_NAME, "sender_id": sender,
            "sender_name": b.backend.names.get(sender, ""), "content": text, "msg_type": 1, "is_group": True,
            "timestamp": b.clock.now if ts is None else ts, **extra}


def own(b, text, *, chat=GROUP, ts=None):
    return {"message_id": f"o{next(_IDS)}", "chat_id": chat, "sender_id": ME, "sender_name": "商务号", "content": text,
            "msg_type": 1, "is_group": True, "timestamp": b.clock.now if ts is None else ts}


def linked(b, n=1, member=1, chat_arg=None):
    """「接群」, 「接群 n」 (or 「接群 <chat_arg>」), 「是 member」: the answer to the last."""
    b.say("接群")
    b.say(f"接群 {chat_arg or n}")
    return b.say(f"是 {member}")


def names(b, deal_id):
    return [event_out(e)["name"] for e in reversed(b.prm.store.events(deal_id, limit=50))]


# ── 「接群」: the groups to choose from ──────────────────────────────────────

def test_jie_qun_lists_the_groups_active_last_that_are_not_linked_yet(bot):
    when = time.strftime("%m-%d %H:%M", time.localtime(bot.clock.now - 600))
    assert bot.say("接群").split("\n") == [
        "最近有动静、还没接手的群（回「接群 编号」接手；也可以「接群 群名」）：",
        f"1. {GROUP_NAME} · 4 人 · {when}",
        f"2. 品牌测试大群 · 12 人 · {time.strftime('%m-%d %H:%M', time.localtime(bot.clock.now - 3600))}",
        f"3. （没有群名） · 2 人 · {time.strftime('%m-%d %H:%M', time.localtime(bot.clock.now - 7200))}"]
    linked(bot)
    assert GROUP not in bot.say("接群") and "1. 品牌测试大群" in bot.say("接群")      # linked: not listed again
    bot.backend.readable = False
    assert bot.say("接群") == gl.NO_READ
    bot.backend.readable = True
    bot.backend.groups.clear()
    assert bot.say("接群") == "最近没有还没接手的群"


def test_jie_qun_on_a_backend_that_cannot_list_groups_says_so(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.prm.boss_groups.attach(object())                                # the Mac's edge backend
    assert b.say("接群") == gl.NO_BACKEND and b.say("接群 小美合作对接群") == gl.NO_BACKEND


def test_the_question_lists_who_may_be_her_the_registered_first(bot):
    bot.prm.register(BD, "小美的商务", "测试")
    bot.backend.groups[GROUP]["members"] += [ADMIN, HELPER, BOSS_ID, "wxid_twin09"]
    bot.backend.names["wxid_twin09"] = BOSS                           # named like the boss: never her
    bot.say("接群")
    assert bot.say("接群 1").split("\n") == [
        f"群「{GROUP_NAME}」（8 人）里谁是达人？回「是 编号」，「不接」作罢（30 分钟内有效）：",
        "1. 测试商务（已登记）", "2. 小美", "3. 品牌同事", "这个群有 8 人，比 PRM_BOSS_GROUP_MAX（6）多，也能接："
        "只起草你选的那个人的话，别人的话只当背景"]
    offer = bot.prm.boss_groups.offered(GROUP)
    assert [m["wxid"] for m in offer["members"]] == [BD, CREATOR, COLLEAGUE]
    assert offer["boss"] == [{"wxid": BOSS_ID, "name": BOSS}]


def test_jie_qun_by_name_spaces_and_case_aside_and_several_of_a_name_are_listed(bot):
    assert bot.say(" 接群  小美合作 对接群 ").startswith(f"群「{GROUP_NAME}」（4 人）里谁是达人？")
    assert bot.prm.boss_groups.offered(GROUP) is not None
    bot.backend.groups[OTHER] = {"name": "小美合作对接群", "members": [ME, CREATOR, TESTER]}
    bot.backend.active[OTHER] = bot.clock.now - 60
    listed = bot.say("接群 小美合作对接群").split("\n")
    assert listed[0] == "有 2 个群叫「小美合作对接群」：回「接群 编号」选一个" and listed[1].startswith(f"1. {GROUP_NAME} · 3 人")
    assert bot.say("接群 2").startswith(f"群「{GROUP_NAME}」（4 人）里谁是达人？")       # (the one active before)
    assert bot.prm.boss_groups.offered(GROUP) is not None and bot.prm.boss_groups.offered(OTHER) is None
    assert bot.say("接群 没有这个群") == "没找到叫「没有这个群」的群（要和微信里的群名一样，不管空格和大小写）：发「接群」看最近的群"
    assert bot.say("接群 9") == "没有第 9 个：回「接群 1」到「接群 2」，或者重新发「接群」"


def test_a_number_without_a_list_is_a_name_and_says_to_list_first(clock, tmp_path):
    b = setup(clock, tmp_path)
    assert b.say("接群 3") == "先发「接群」看最近的群，再回「接群 编号」"
    b.clock.now += gl.OFFER_SEC + 1
    b.say("接群")
    b.clock.now += gl.OFFER_SEC + 1                                   # the list counts OFFER_SEC long
    assert b.say("接群 1") == "先发「接群」看最近的群，再回「接群 编号」"


def test_a_group_with_nobody_she_may_be_and_one_whose_members_cannot_be_read(bot):
    bot.backend.groups[NAMELESS]["members"] = [ME, ADMIN, BOSS_ID]
    bot.say("接群")
    assert bot.say("接群 3") == "群「（没有群名）」里除了我们自己、老板、管理员和能用 cc 的人，没有别人：没有谁能当达人"
    bot.backend.readable = False
    assert bot.say("接群 1") == f"群「{GROUP_NAME}」的成员现在读不出来：稍后再试"
    assert bot.prm.boss_groups.offered() is None


def test_a_member_without_a_name_is_listed_by_the_last_4_of_her_wxid(bot):
    """Review B-L7 (integrated review 2026-11-10): a member WeChat gives no name for was listed in the admin's chat by
    her whole wxid; 「（没有名字）…<last 4>」 now (the console's card has the rest)."""
    bot.backend.names[TESTER] = ""
    bot.say("接群")
    question = bot.say("接群 3")
    assert question.split("\n")[1] == f"1. （没有名字）…{TESTER[-4:]}" and TESTER not in question
    assert bot.say("是 1").startswith("接手了：群「（没有群名）」")


def test_a_nameless_group_says_it_needs_a_name_before_anything_is_sent(bot):
    bot.say("接群")
    question = bot.say("接群 3")
    assert question.split("\n")[-1] == "群还没有名字：接手以后在微信里给群起个名字，机器人才能把回复发进去"
    assert bot.say("是 1").endswith("群还没有名字：在微信里给群起个名字以后机器人才能发进去")


# ── 「是 N」: the takeover by hand ─────────────────────────────────────────

def test_shi_n_registers_her_and_links_the_group_whatever_prm_boss_groups_says(bot):
    assert not bot.prm.boss_groups.on
    answer = linked(bot, member=1)                                    # 1: 小美
    party = bot.prm.store.party_by_wxid(CREATOR)
    deal = bot.prm.store.deal_of(party["id"])
    assert answer == (f"接手了：群「{GROUP_NAME}」→ 小美（新登记的达人），合作单 {deal['id']} 号；之后她在群里说的话会起草，"
                      "在「待审批」里等你看；她最近没有在等我们回：等她在群里说话再起草")
    assert (party["name"], party["note"]) == ("小美", f"手动接的群（{GROUP_NAME}）")
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["manual"], row["party_id"], row["deal_id"]) == ("linked", 1, party["id"], deal["id"])
    # no boss in it: no 三方群 (kb 第 0.5 节); the deal lists the group (「拉三方群」 still makes her one)
    assert deal["three_party"] == {} and bot.prm.three_party.job_meta(deal) is None
    shown = bot.prm.snapshot()["parties"][0]["deal"]
    assert shown["linked_groups"] == [{"chat": GROUP, "name": GROUP_NAME, "at": bot.clock.now}]
    assert shown["three_party"] == {}
    assert bot.prm.is_party_group(GROUP) and GROUP in bot.backend.watched
    assert names(bot, deal["id"]) == [f"手动接的群（{GROUP_NAME}）"]
    assert job_of(bot) is None                                        # no 开场白: the talk is going on
    assert bot.prm.boss_groups.offered() is None                     # the question is answered
    assert bot.say("是 2") is None                                    # (no question open: free words again)


def test_a_registered_creator_only_gets_the_group_linked(bot):
    party = bot.prm.register(CREATOR, "小美美", "达人名单里的")
    bot.say("接群")
    assert bot.say("接群 1").split("\n")[1] == "1. 小美（已登记）"            # (as this account sees her)
    answer = bot.say("是 1")
    assert answer.startswith(f"接手了：群「{GROUP_NAME}」→ 小美美（已登记），合作单 ")
    again = bot.prm.store.party_by_wxid(CREATOR)
    assert (again["id"], again["name"], again["note"]) == (party["id"], "小美美", "达人名单里的")
    [ev] = bot.prm.store.events(bot.prm.store.deal_of(party["id"])["id"], kinds=(bg.EVENT,))
    assert ev["detail"] == {"name": GROUP_NAME, "chat": GROUP, "new": False, "manual": True} and ev["actor"] == "admin"


def test_a_creator_an_operator_stopped_is_registered_again_by_hand(bot):
    party = bot.prm.register(CREATOR, "小美", "测试")
    bot.prm.set_active(party["id"], False)
    bot.say("接群")
    assert "1. 小美（停用过）" in bot.say("接群 1").split("\n")
    bot.say("是 1")
    assert bot.prm.store.party_by_wxid(CREATOR)["active"] == 1 and bot.prm.is_party_group(GROUP)


def test_shi_n_out_of_range_and_a_registration_refused_change_nothing(bot):
    bot.say("接群")
    bot.say("接群 1")
    assert bot.say("是 7") == "没有第 7 位：回「是 1」到「是 3」，「不接」作罢"
    bot.prm._reserved.add(CREATOR)                                    # (registering her is refused now)
    assert bot.say("是 1") == "没接手：没登记成：这是管理员或能用 cc 中转的人，不能同时登记成达人"
    assert bot.prm.store.boss_group(GROUP) is None and not bot.prm.is_party_group(GROUP)
    assert bot.prm.boss_groups.offered(GROUP) is not None             # (the question stays open)


def test_bu_jie_drops_the_question_and_it_expires_by_itself(bot):
    bot.say("接群")
    bot.say("接群 1")
    assert bot.say("不接") == f"好的，群「{GROUP_NAME}」不接了，以后也不自动接（要接再发「接群 {GROUP_NAME}」）"
    assert bot.prm.boss_groups.offered() is None and bot.prm.store.boss_group(GROUP) is None
    assert bot.say("是 1") == f"群「{GROUP_NAME}」这次不接了（已经作罢）：要接的话重新发「接群」"
    bot.say("接群 1")
    bot.clock.now += gl.OFFER_SEC + 1
    assert bot.say("是 1") == "刚才那个「接群」已经过了 30 分钟，作废了：要接的话重新发「接群」"
    assert bot.say("不接") == "刚才那个「接群」已经过了 30 分钟，作废了：要接的话重新发「接群」"
    assert bot.prm.store.boss_group(GROUP) is None
    bot.clock.now += gl.ASKED_KEEP_SEC
    assert bot.say("是 1") is None and bot.say("不接") is None       # long after: not commands at all


def test_shi_and_bu_jie_are_no_commands_without_a_question(bot):
    for text in ("是 1", "是1", "不接", "是"):
        assert bot.say(text) is None and not bot.admin.is_command(text), text
    for text in ("接群", "接群 1", "接群 小美合作对接群", "不接群 小美合作对接群", "接群3"):
        assert bot.admin.is_command(text), text
    bot.say("接群")
    bot.say("接群 1")
    assert bot.admin.is_command("是 1") and bot.admin.is_command("不接")


def test_bot_hands_the_admins_jie_qun_commands_over(bot):
    """Bot._wire_friend_adds (src/bot.py) gives the admin handler the PRM's 接群 commands, as Bot.run wires them."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.bot import Bot
    from src.config import BotConfig

    handler = AdminCommandHandler()
    prm = SimpleNamespace(friend_adds=MagicMock(), three_party=MagicMock(), group_links=bot.prm.group_links)
    Bot(BotConfig(admin_wxid=ADMIN))._wire_friend_adds(BotConfig(admin_wxid=ADMIN), SimpleNamespace(), handler, prm)
    assert handler.handle("接群").startswith("最近有动静、还没接手的群")


def test_without_the_copilot_jie_qun_says_what_to_turn_on():
    handler = AdminCommandHandler()
    for text in ("接群", "接群 3", "不接群 小美合作对接群"):
        assert handler.handle(text) == NO_GROUP_LINKS, text
    assert handler.handle("是 1") is None and handler.handle("不接") is None


# ── what was said there before: context, and one draft when she waits ──────

def test_the_lines_before_the_link_are_context_and_her_unanswered_ones_one_draft(bot):
    t = bot.clock.now
    say = bot.backend.say
    say(GROUP, CREATOR, "你们家这款还有货吗", t - 3000)
    say(GROUP, ME, "有的，我发您看看", t - 2900)                      # an operator answered her by hand then
    say(GROUP, BD, "她这周想先看样", t - 2000)
    say(GROUP, CREATOR, "那寄样怎么弄呀", t - 900)
    say(GROUP, CREATOR, "地址发你吗", t - 840)
    say(GROUP, "", "\"小美\" 拍了拍我", t - 30, msg_type=10000, special={"kind": "system", "title": "\"小美\" 拍了拍我"})
    answer = linked(bot, member=1)
    assert answer.endswith("群里最近 3 句记成了背景；她最后 2 句还没人回，派了一份草稿")
    job = job_of(bot)
    meta = json.loads(job["meta"])
    assert job["chat_id"] == GROUP and "opening" not in meta and meta["channel"] == "group"
    assert job["text"].split("\n") == [f"[{time.strftime('%m-%d %H:%M', time.localtime(t - 900))}] 那寄样怎么弄呀",
                                       f"[{time.strftime('%m-%d %H:%M', time.localtime(t - 840))}] 地址发你吗"]
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [
        ("她", "你们家这款还有货吗"), ("我们", "有的，我发您看看"), ("群成员", "她这周想先看样")]
    assert meta["group"]["manual"] is True and meta["group"]["others"] == 2 and meta["group"]["boss"] == []
    assert meta["late"]                                               # read late: the draft waits for a person
    deal = bot.prm.store.deal_of(bot.prm.store.party_by_wxid(CREATOR)["id"])
    assert deal["mode"] == "copilot"                                  # our line before the link took nothing over
    assert bot.backend.watched[GROUP] == t - 30 + 1                   # the watch: after the newest line read
    assert bot.backend.asked_since == t - 12 * 3600                   # (PRM_CATCHUP_HOURS back)
    # no boss in it: no 三方群, so her private drafts are not told its lines as her 三方群's (review 2026-11-09)
    assert bot.prm._group_lines(deal["id"]) == []


def test_when_we_spoke_last_nothing_is_drafted_until_she_writes(bot):
    t = bot.clock.now
    bot.backend.say(GROUP, CREATOR, "好的我看看", t - 300)
    bot.backend.say(GROUP, ME, "好嘞，有问题随时说", t - 200)
    assert linked(bot, member=1).endswith("群里最近 2 句记成了背景；她最近没有在等我们回：等她在群里说话再起草")
    assert job_of(bot) is None
    bot.prm.handle(gmsg(bot, "我选好了，要这两款"))
    job = job_of(bot)
    assert job["text"].endswith("我选好了，要这两款") and "late" not in json.loads(job["meta"])
    assert [x["who"] for x in json.loads(job["meta"])["group"]["lines"]] == ["她", "我们"]


def test_only_her_newest_waiting_lines_are_the_draft_the_older_ones_context(bot):
    t = bot.clock.now
    for n in range(LINK_TAIL + 3):
        bot.backend.say(GROUP, CREATOR, f"第{n + 1}句", t - 600 + n)
    linked(bot, member=1)
    job = job_of(bot)
    assert [line.split("] ", 1)[1] for line in job["text"].split("\n")] == [f"第{n}句" for n in range(4, 4 + LINK_TAIL)]
    assert [x["text"] for x in json.loads(job["meta"])["group"]["lines"]] == ["第1句", "第2句", "第3句"]


def test_a_draft_of_hers_waiting_ages_by_the_catch_up_when_she_just_wrote(bot):
    """A line she wrote a moment before the link is no late line; its draft waits for a person all the same (it answers
    what waited at 接群, ``meta.link_first``: 2026-11-10, tests/test_prm_linked_groups.py)."""
    bot.backend.say(GROUP, CREATOR, "在吗", bot.clock.now - 20)
    linked(bot, member=1)
    meta = json.loads(job_of(bot)["meta"])
    assert "late" not in meta and meta["link_first"] is True


def test_a_group_whose_lines_cannot_be_read_is_linked_and_read_from_now_on(bot):
    bot.backend.unreadable.add(GROUP)
    assert linked(bot, member=1).endswith("群里之前的话这次读不出来：从现在起她说的话照常起草")
    assert bot.prm.is_party_group(GROUP) and bot.backend.watched[GROUP] == bot.clock.now and job_of(bot) is None


def test_a_person_having_her_deal_has_her_waiting_lines_only_recorded(bot):
    party = bot.prm.register(CREATOR, "小美", "测试")
    deal = bot.prm.store.deal_of(party["id"])
    bot.prm.store.update_deal(deal["id"], bot.clock.now, mode="human", mode_until=bot.clock.now + 3600)
    bot.backend.say(GROUP, CREATOR, "在吗", bot.clock.now - 60)
    assert linked(bot, member=1).endswith("她最后 1 句还没人回：现在人工接管或暂停，只记下了，没起草")
    assert job_of(bot) is None
    assert any("在吗" in ev["detail"]["text"] for ev in bot.prm.store.events(deal["id"], kinds=("creator_message",)))


# ── from then on: as a 老板拉的群 the bot took over ─────────────────────────

def test_her_lines_are_drafted_and_sent_in_the_group_the_others_only_context(bot):
    linked(bot, member=1)
    assert bot.prm.handle(gmsg(bot, "我是她的商务，有事找我", sender=BD)) is True
    assert bot.prm.handle(gmsg(bot, "收到", sender=COLLEAGUE)) is True
    assert job_of(bot) is None and bot.texts == []                    # never answered, no job
    bot.clock.now += 30
    bot.prm.handle(gmsg(bot, "你们家帽子多少钱"))
    job, packet = drafted(bot, "您好～均价两三百")
    meta = json.loads(job["meta"])
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("群成员", "我是她的商务，有事找我"), ("群成员", "收到")]
    assert (packet["chat_id"], packet["meta"]["channel"], packet["meta"]["group_manual"]) == (GROUP, "group", True)
    out = bot.prm.snapshot()["pending"][0]
    assert (out["channel"], out["group_name"], out["group_manual"]) == ("group", GROUP_NAME, True)
    bot.prm.decide(packet["id"], "send")
    assert bot.prm.send_approved() == 1                               # PRM_BOSS_GROUPS off: sent all the same
    assert bot.texts == [(GROUP, "您好～均价两三百")]
    bot.prm.on_own_message(own(bot, "您好～均价两三百"))                # read back: ours, no takeover
    deal = bot.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "copilot" and bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.boss_group(GROUP)["lines"] == []             # answered: the lines told go


def test_an_operators_own_line_in_the_group_takes_her_deal_over(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    bot.clock.now += 5
    bot.prm.on_own_message(own(bot, "在的在的，我是品牌的运营"))
    deal = bot.prm.store.deal(packet["deal_id"])
    assert deal["mode"] == "human" and bot.prm.store.packet(packet["id"])["status"] == "superseded"
    bot.prm.handle(gmsg(bot, "好的"))
    assert job_of(bot) is None


def test_no_holding_reply_goes_to_the_group_and_the_gates_apply(clock, tmp_path):
    # (PRM_LINKED_GROUPS_AUTO on: the gates as a 老板拉的群's; off, the default, its drafts always wait: 2026-11-10,
    # tests/test_prm_linked_groups.py)
    b = setup(clock, tmp_path, prm_holding_min=10, prm_linked_groups_auto="on")
    b.prm.store.set_gate("faq", "auto", clock.now)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    _, packet = drafted(b, "在的～")
    assert (packet["status"], packet["route"]) == ("approved", "auto")
    b.prm.send_approved()
    assert b.texts == [(GROUP, "在的～")]
    b.prm.handle(gmsg(b, "价格呢"))
    drafted(b)
    clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 0 and b.texts == [(GROUP, "在的～")]
    b.prm.stop()
    b.relay.stop()


def test_a_send_the_backend_refuses_says_so_without_the_word_boss(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    bot.send_ok = SendRefused("这个群只知道编号、不知道群名，机器人找不到它")
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    notice = next(n for n in bot.prm.store.deal(packet["deal_id"])["notices"] if n["key"] == GROUP_UNSENT)
    assert notice["text"].startswith(f"#{packet['id']} 没发到群里（") and "老板" not in notice["text"]


def test_our_replies_told_back_say_they_went_to_a_group_linked_by_hand(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot, "在的")
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    bot.clock.now += 60
    bot.prm.handle(bot.msg("私聊问一下"))
    sent = json.loads(job_of(bot, CREATOR)["meta"])["sent"]
    assert [(s["where"], s.get("manual"), s["text"]) for s in sent] == [("group", True, "在的")]


def test_a_boss_in_a_group_linked_by_hand_is_called_as_prm_boss_call_and_counted_apart(clock, tmp_path):
    b = setup(clock, tmp_path, prm_boss_call="测试总")
    b.backend.groups[GROUP]["members"].append(BOSS_ID)
    b.backend.say(GROUP, BOSS_ID, "我是老板测试，你们聊", clock.now - 100)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "老板测试说的那款呢"))
    meta = json.loads(job_of(b)["meta"])
    assert meta["group"]["boss"] == ["测试总"] and meta["group"]["others"] == 2
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("老板", "我是测试总，你们聊")]
    assert BOSS not in job_of(b)["text"] and BOSS not in job_of(b)["meta"]
    # the boss is in it: it is her 三方群 from now on (her private drafts hear it is made)
    deal = b.prm.store.deal(b.prm.store.boss_group(GROUP)["deal_id"])
    assert (deal["three_party"]["by"], deal["three_party"]["chat"]) == ("manual", GROUP)
    assert "linked_groups" not in b.prm.snapshot()["parties"][0]["deal"]
    b.prm.handle(b.msg("私聊说一句"))
    assert json.loads(job_of(b, CREATOR)["meta"])["three_party"]
    b.prm.stop()
    b.relay.stop()


def test_she_left_the_group_her_deal_says_so_with_the_switch_off_too(bot):
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    bot.backend.groups[GROUP]["members"].remove(CREATOR)
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.sweep()
    assert bg.MANUAL_LEFT_TEXT in bot.prm.store.deal(deal_id)["attention"]
    bot.backend.groups[GROUP]["members"].append(CREATOR)
    bot.clock.now += 5
    bot.prm.boss_groups.sweep()                                       # (once a minute)
    assert bg.MANUAL_LEFT_TEXT in bot.prm.store.deal(deal_id)["attention"]
    bot.clock.now += bg.BOSS_SCAN_SEC
    bot.prm.boss_groups.sweep()
    assert bg.MANUAL_LEFT_TEXT not in bot.prm.store.deal(deal_id)["attention"]


def test_what_the_bot_starts_itself_goes_to_the_group_when_she_has_no_private_chat(bot):
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    out = bot.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"})
    notice = bot.prm.store.packet(out["packet"])
    assert notice["chat_id"] == GROUP and notice["meta"]["group_manual"] is True


def test_after_a_restart_the_group_is_read_again_from_its_last_line(bot):
    t = bot.clock.now
    bot.backend.say(GROUP, CREATOR, "好的", t - 100)
    bot.backend.say(GROUP, ME, "嗯嗯", t - 90)
    linked(bot, member=1)
    bot.clock.now += 30
    bot.prm.handle(gmsg(bot, "在吗", ts=bot.clock.now - 5))
    restart(bot, LinkBackend())
    assert bot.prm.boss_groups.start() == 1 and bot.backend.watched == {GROUP: bot.clock.now - 5 + 1}
    assert bot.prm.is_party_group(GROUP)                              # (the switch is still off)


# ── the switch: the scan's groups as before, the ones by hand always ───────

def test_off_a_scanned_group_from_before_stays_silent_and_its_drafts_fail_but_one_by_hand_works(clock, tmp_path):
    b = setup(clock, tmp_path, on=True)
    party = b.prm.register(TESTER, "测试达人二号", "")
    store = b.prm.store
    store.add_boss_group(OTHER, name="老板拉的群", found_at=clock.now, state="linked", members=[], boss=[])
    store.update_boss_group(OTHER, clock.now, party_id=party["id"], deal_id=store.deal_of(party["id"])["id"])
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    _, mine = drafted(b, "在的")
    b.prm._config.prm_boss_groups = "off"
    restart(b, LinkBackend())
    groups(b)
    b.prm._stopping.set()
    b.prm.start()
    assert b.prm.store.packet(mine["id"])["status"] == "pending"      # (by hand: not the switch's)
    assert not b.prm.is_party_group(OTHER) and b.prm.is_party_group(GROUP)
    assert set(b.backend.watched) == {GROUP}
    assert b.prm.handle(gmsg(b, "在吗", chat=OTHER, sender=TESTER)) is False
    b.prm.decide(mine["id"], "send")
    assert b.prm.send_approved() == 1 and b.texts == [(GROUP, "在的")]
    b.relay.stop()


def test_nobody_linked_anything_the_status_is_as_before(bot):
    assert bot.prm.snapshot()["boss_groups"] == {"enabled": False, "why_not": "", "warning": "", "boss": [BOSS],
                                                 "boss_call": "老板", "pending": [], "waiting": 0, "linked": 0}
    bot.say("接群")
    bot.say("接群 1")
    shown = bot.prm.snapshot()["boss_groups"]
    assert shown["manual"] == [{"chat": GROUP, "name": GROUP_NAME, "size": 4, "asked_at": bot.clock.now,
                                "until": bot.clock.now + gl.OFFER_SEC, "three_party": False,
                                "members": [{"wxid": CREATOR, "name": "小美", "registered": False, "stopped": False},
                                            {"wxid": BD, "name": "测试商务", "registered": False, "stopped": False},
                                            {"wxid": COLLEAGUE, "name": "品牌同事", "registered": False,
                                             "stopped": False}]}]
    bot.say("是 1")
    shown = bot.prm.snapshot()["boss_groups"]
    assert "manual" not in shown and shown["manual_linked"] == 1 and shown["linked"] == 0
    assert bot.prm.snapshot()["parties"][0]["source"] == ""           # (no 老板介绍)


def test_the_consoles_pick_takes_the_open_question_and_its_drop_withdraws_it(bot):
    bot.say("接群")
    bot.say("接群 1")
    assert bot.prm.boss_groups.pick(GROUP, ME)["ok"] is False          # only a member she may be
    out = bot.prm.boss_groups.pick(GROUP, CREATOR)
    party = bot.prm.store.party_by_wxid(CREATOR)
    assert out["ok"] is True and out["party_id"] == party["id"] and out["caught"]["read"] is True
    assert bot.prm.is_party_group(GROUP)
    [ev] = bot.prm.store.events(out["deal_id"], kinds=(bg.EVENT,))
    assert ev["actor"] == "console"
    assert bot.say("是 2") == f"群「{GROUP_NAME}」已经接手了"
    assert bot.say("接群 1") == f"群「{GROUP_NAME}」已经接手了：她在群里说的话会起草"   # (the list from before)
    bot.say("接群")
    bot.say("接群 1")                                                 # (1 is the big group now)
    assert bot.prm.boss_groups.ignore(BIG) == {"ok": True} and bot.prm.boss_groups.offered() is None
    assert bot.prm.store.boss_group(BIG) is None


# ── her deal's own 三方群 ───────────────────────────────────────────────────

def test_jie_qun_on_her_own_three_party_group_only_asks_to_confirm(bot):
    party = bot.prm.register(CREATOR, "小美", "测试")
    deal = bot.prm.store.deal_of(party["id"])
    bot.backend.groups[OURS] = {"name": "品牌测试×小美", "members": [ME, BOSS_ID, CREATOR]}
    bot.backend.active[OURS] = bot.clock.now - 10
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party={"state": "created", "at": bot.clock.now - 86400,
                                                                      "by": "console", "chat": OURS,
                                                                      "name": "品牌测试×小美", "members": 3})
    bot.prm._group_line(party, OURS, "boss", "寄 3 顶给她", bot.clock.now - 50, bot.clock.now)   # read only so far
    bot.backend.say(OURS, BOSS_ID, "寄 3 顶给她", bot.clock.now - 50)
    bot.backend.say(OURS, CREATOR, "收到啦，什么时候寄", bot.clock.now - 20)
    assert "1. 品牌测试×小美 · 3 人" in bot.say("接群") and "小美 的三方群" in bot.say("接群")
    assert bot.say("接群 1") == (f"群「品牌测试×小美」是 小美 的三方群（合作单 {deal['id']} 号，「拉三方群」建的，现在只读）："
                                "接手以后她在群里说的话会起草，回复发在群里，群里的人都看得到。回「是 1」接手，「不接」作罢（30 分钟内有效）")
    answer = bot.say("是 1")
    assert answer.startswith(f"接手了：群「品牌测试×小美」→ 小美（已登记），合作单 {deal['id']} 号")
    assert "她原来的三方群保留" not in answer and "派了一份草稿" in answer
    group = bot.prm.store.deal(deal["id"])["three_party"]
    assert (group["by"], group["chat"], group["why"], group["at"]) == ("manual", OURS, "手动接的群", bot.clock.now)
    assert bot.prm.three_party.deal_of_chat(OURS) is None and bot.prm.three_party.watch_all() == 0
    assert bot.prm.is_party_group(OURS) and job_of(bot, OURS)["text"].endswith("收到啦，什么时候寄")
    kept = [ev["detail"]["text"] for ev in bot.prm.store.events(deal["id"], limit=50, kinds=("group_line",))]
    assert kept.count("寄 3 顶给她") == 1                              # (kept once, before; not again)
    # 「群已经不在了」 on her deal: the group is gone, its lines silent again
    assert bot.prm.deal_action("group_none", {"deal_id": deal["id"]})["ok"] is True
    assert bot.prm.store.boss_group(OURS)["state"] == "gone" and OURS in bot.backend.unwatched
    assert "手动接的群不接了（运营说的），群里她的消息不再起草" in names(bot, deal["id"])


def test_a_deal_with_its_own_three_party_group_keeps_it_when_another_group_is_linked(bot):
    party = bot.prm.register(CREATOR, "小美", "测试")
    deal = bot.prm.store.deal_of(party["id"])
    made = {"state": "created", "at": bot.clock.now, "by": "console", "chat": OURS, "name": "品牌测试×小美", "members": 3}
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party=made)
    assert linked(bot, member=1).split("；")[-1] == "她原来的三方群保留"
    assert bot.prm.store.deal(deal["id"])["three_party"] == made
    assert f"手动接的群（{GROUP_NAME}），她原来的三方群保留" in names(bot, deal["id"])
    shown = next(p for p in bot.prm.snapshot()["parties"] if p["id"] == party["id"])["deal"]
    assert shown["linked_groups"] == [{"chat": GROUP, "name": GROUP_NAME, "at": bot.clock.now}]
    other = bot.prm.register(TESTER, "测试达人二号", "")
    assert "linked_groups" not in next(p for p in bot.prm.snapshot()["parties"] if p["id"] == other["id"])["deal"]


# ── 「不接群 群名」 ─────────────────────────────────────────────────────────

def test_bu_jie_qun_stops_a_linked_group_as_the_group_gone_does(bot):
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    assert bot.say("不接群 别的群") == "没有叫「别的群」的群（要和微信里的群名一样，不管空格和大小写）"
    assert bot.say(f"不接群 {GROUP_NAME}") == (f"不接了：群「{GROUP_NAME}」里她的话不再起草，群里的话和别的群一样不回"
                                             "（小美的合作单和私聊照旧）")
    row = bot.prm.store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("gone", bg.MANUAL_GONE_WHY) and GROUP in bot.backend.unwatched
    assert bot.prm.store.deal(deal_id)["three_party"] == {} and not bot.prm.is_party_group(GROUP)
    assert "linked_groups" not in bot.prm.snapshot()["parties"][0]["deal"]
    assert bot.prm.handle(gmsg(bot, "还在吗")) is False                 # silent again
    assert bot.prm.handle(bot.msg("我在私聊")) is True                   # the deal and her private chat go on
    assert "手动接的群不接了（运营说的），群里她的消息不再起草" in names(bot, deal_id)
    assert GROUP_NAME in bot.say("接群")                               # it may be linked again
    bot.say("接群 1")
    assert bot.say("是 1").startswith(f"接手了：群「{GROUP_NAME}」→ 小美（已登记）")
    assert bot.prm.is_party_group(GROUP) and bot.prm.store.boss_group(GROUP)["state"] == "linked"


def test_bu_jie_qun_of_a_group_that_is_not_her_deals_three_party_group(bot):
    party = bot.prm.register(CREATOR, "小美", "测试")
    deal = bot.prm.store.deal_of(party["id"])
    made = {"state": "created", "at": bot.clock.now, "by": "console", "chat": OURS, "name": "品牌测试×小美", "members": 3}
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party=made)
    linked(bot, member=1)
    assert bot.say(f"不接群 {GROUP_NAME}").startswith(f"不接了：群「{GROUP_NAME}」")
    assert bot.prm.store.boss_group(GROUP)["state"] == "gone" and bot.prm.store.deal(deal["id"])["three_party"] == made


def test_the_consoles_bu_jie_le_stops_a_group_linked_to_the_deal(bot):
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    assert bot.prm.deal_action("group_unlink", {"deal_id": deal_id, "chat": OTHER})["ok"] is False
    assert bot.prm.deal_action("group_unlink", {"deal_id": deal_id + 1, "chat": GROUP})["ok"] is False
    assert bot.prm.deal_action("group_unlink", {"deal_id": deal_id, "chat": GROUP}) == {
        "ok": True, "three_party": False, "reading": False, "unsent": 0, "sending": 0}
    assert bot.prm.store.boss_group(GROUP)["state"] == "gone" and not bot.prm.is_party_group(GROUP)
    assert "linked_groups" not in bot.prm.snapshot()["parties"][0]["deal"]
    assert bot.prm.deal_action("group_unlink", {"deal_id": deal_id, "chat": GROUP})["ok"] is False
    from src.prm.service import apply_deal_action

    out = apply_deal_action(bot.prm.store, "group_unlink", {"deal_id": deal_id, "chat": GROUP}, now=bot.clock.now,
                            takeover_sec=7200)                        # (the stopped bot's console)
    assert out == {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能停掉接手的群"}


# ── review 2026-11-09: a draft for a group goes out only while the bot drafts in it ──

def test_a_draft_job_still_queued_when_the_link_ends_is_cancelled(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    job = job_of(bot)
    assert bot.say(f"不接群 {GROUP_NAME}").startswith("不接了")
    assert bot.relay.store.get(job["id"])["status"] == "cancelled" and job_of(bot) is None
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",)) is None                    # the Mac gets nothing to write for it


def test_a_draft_written_while_the_link_ends_fails_when_it_comes_back(clock, tmp_path):
    """Before, a draft the Mac was writing when 「不接群」 came went into the group all the same (its gate auto)."""
    b = setup(clock, tmp_path)
    b.prm.store.set_gate("faq", "auto", clock.now)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    job = job_of(b)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]           # the Mac is writing it
    assert b.say(f"不接群 {GROUP_NAME}").startswith("不接了")
    assert b.relay.store.get(job["id"])["status"] == "running"        # (one being written is never cut off)
    b.finish(job["id"], draft(message_kind="faq", reply_draft="在的～", slots={}, stage_proposal="",
                              reason="kb/合作政策.md"))
    packet = b.prm.store.packet_for_job(job["id"])
    # (failed, and shown as one: not 「自动发送」, review 2026-11-09)
    assert (packet["status"], packet["error"], packet["route"]) == ("failed", GROUP_CLOSED_TEXT, "review")
    assert b.prm.send_approved() == 0 and b.texts == []
    b.prm.stop()
    b.relay.stop()


def test_the_drafts_waiting_or_approved_when_the_link_ends_fail_and_the_answer_says_so(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, approved = drafted(bot, "在的")
    assert bot.prm.decide(approved["id"], "send")["ok"] is True       # approved, not sent yet
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "价格呢"))
    _, waiting = drafted(bot, "两三百")
    assert bot.say(f"不接群 {GROUP_NAME}") == (f"不接了：群「{GROUP_NAME}」里她的话不再起草，群里的话和别的群一样不回"
                                             "（小美的合作单和私聊照旧）；还没发的 2 份群里的草稿不发了")
    for p in (approved, waiting):
        p = bot.prm.store.packet(p["id"])
        assert (p["status"], p["error"]) == ("failed", GROUP_CLOSED_TEXT)
    assert bot.prm.decide(waiting["id"], "send")["ok"] is False
    assert bot.prm.send_approved() == 0 and bot.texts == []


def test_a_packet_aimed_at_the_group_as_the_link_ends_is_not_sent(bot, monkeypatch):
    """The rule holds after ``_aim`` too: the link ended while a packet the bot started itself was aimed at it."""
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    out = bot.prm.deal_action("shipment", {"deal_id": deal_id, "carrier": "顺丰", "waybill": "SF1234567890"})
    aim = bot.prm._aim

    def aimed_and_the_link_ends(packet):
        packet = aim(packet)
        bot.say(f"不接群 {GROUP_NAME}")
        return packet

    monkeypatch.setattr(bot.prm, "_aim", aimed_and_the_link_ends)
    bot.prm.decide(out["packet"], "send")
    assert bot.prm.send_approved() == 0 and bot.texts == []
    p = bot.prm.store.packet(out["packet"])
    assert (p["status"], p["chat_id"], p["error"]) == ("failed", GROUP, GROUP_CLOSED_TEXT)


def test_at_start_a_draft_for_a_group_not_linked_any_more_fails(clock, tmp_path):
    b = setup(clock, tmp_path)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    _, packet = drafted(b)
    b.prm.store.update_boss_group(GROUP, clock.now, states=("linked",), state="gone")   # (ended by an older bot)
    restart(b, LinkBackend())
    b.prm._stopping.set()
    b.prm.start()
    p = b.prm.store.packet(packet["id"])
    assert (p["status"], p["error"]) == ("failed", GROUP_CLOSED_TEXT)
    b.relay.stop()


def test_what_the_bot_starts_itself_never_goes_to_a_scanned_group_it_does_not_read(clock, tmp_path):
    """PRM_BOSS_GROUPS off: a group the scan took over before (still linked in prm.db) is no place to reach her."""
    b = setup(clock, tmp_path)
    party = b.prm.register(CREATOR, "小美", "测试")
    store = b.prm.store
    deal = store.deal_of(party["id"])
    store.add_boss_group(OTHER, name="老板拉的老群", found_at=clock.now - 86400, state="linked", members=[], boss=[])
    store.update_boss_group(OTHER, clock.now, party_id=party["id"], deal_id=deal["id"])
    out = b.prm.deal_action("shipment", {"deal_id": deal["id"], "carrier": "顺丰", "waybill": "SF1234567890"})
    linked(b, member=1)                                               # GROUP linked to her by hand
    store.set_notice(deal["id"], bg.LEFT_NOTICE + GROUP, bg.MANUAL_LEFT_TEXT, clock.now)   # and she left it
    assert b.prm.boss_groups.reach_group(party["id"]) is None
    b.prm.decide(out["packet"], "send")
    assert b.prm.send_approved() == 0 and b.texts == []
    p = store.packet(out["packet"])
    assert p["status"] == "failed" and p["error"].startswith(UNREACHABLE_TEXT) and p["chat_id"] != OTHER
    b.prm.stop()
    b.relay.stop()


# ── review 2026-11-09: 「不接群」 on her 三方群 gives it back, read only ──

def test_bu_jie_qun_on_her_own_three_party_group_puts_it_back_read_only(bot):
    bot.prm.three_party.attach(backend=bot.backend)                   # (this PC reads groups: Bot.run wires it so)
    party = bot.prm.register(CREATOR, "小美", "测试")
    deal = bot.prm.store.deal_of(party["id"])
    bot.backend.groups[OURS] = {"name": "品牌测试×小美", "members": [ME, BOSS_ID, CREATOR]}
    bot.backend.active[OURS] = bot.clock.now - 10
    made = {"state": "created", "at": bot.clock.now - 86400, "by": "console", "chat": OURS, "name": "品牌测试×小美",
            "members": 3}
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party=made)
    bot.say("接群")
    bot.say("接群 1")
    bot.say("是 1")
    assert bot.prm.store.deal(deal["id"])["three_party"]["by"] == "manual"
    assert bot.say("不接群 品牌测试×小美") == ("不接了：群「品牌测试×小美」里她的话不再起草；它还是小美的三方群，机器人只读、"
                                           "不在群里说话，群里的话照旧交给她私聊的草稿（合作单和私聊照旧）")
    assert bot.prm.store.deal(deal["id"])["three_party"] == made          # as it was: never 「群已经不在了」
    assert bot.prm.three_party.deal_of_chat(OURS)["id"] == deal["id"] and OURS in bot.backend.watched
    assert not bot.prm.is_party_group(OURS) and bot.prm.store.boss_group(OURS)["state"] == "gone"
    assert "不接这个群了（运营说的），群里她的消息不再起草：它还是她的三方群，机器人只读" in names(bot, deal["id"])
    assert bot.prm.handle(gmsg(bot, "什么时候寄呀", chat=OURS)) is True and job_of(bot, OURS) is None   # read only
    kept = [ev["detail"]["text"] for ev in bot.prm.store.events(deal["id"], limit=50, kinds=("group_line",))]
    assert "什么时候寄呀" in kept
    assert bot.say("接群 品牌测试×小美").startswith("群「品牌测试×小美」是 小美 的三方群")      # it may be linked again


def test_bu_jie_qun_on_a_boss_group_that_became_her_three_party_group_by_the_link(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.backend.groups[GROUP]["members"].append(BOSS_ID)
    linked(b, member=1)
    deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
    linked_as = b.prm.store.deal(deal_id)["three_party"]
    assert (linked_as["by"], linked_as["chat"]) == ("manual", GROUP)  # (a boss in it: her 三方群 by the link)
    # this PC's 三方群 cannot read groups (not wired): the answer says so
    assert b.say(f"不接群 {GROUP_NAME}") == (f"不接了：群「{GROUP_NAME}」里她的话不再起草；它还是小美的三方群，这台电脑读不了群，"
                                           "群里的话交不到她私聊的草稿（合作单和私聊照旧）")
    group = b.prm.store.deal(deal_id)["three_party"]
    assert group == {**{k: v for k, v in linked_as.items() if k != "why"}, "by": "console"}   # as one 「建好了」
    assert b.prm.three_party.deal_of_chat(GROUP)["id"] == deal_id and not b.prm.is_party_group(GROUP)
    # linked again (her 三方群 now: only 「是 1」), and the console's 「不接了」 (no chat: her 三方群's)
    b.prm.three_party.attach(backend=b.backend)
    b.say(f"接群 {GROUP_NAME}")
    assert b.say("是 1").startswith(f"接手了：群「{GROUP_NAME}」→ 小美（已登记）")
    assert b.prm.deal_action("group_unlink", {"deal_id": deal_id}) == {"ok": True, "three_party": True,
                                                                      "reading": True, "unsent": 0, "sending": 0}
    assert b.prm.store.deal(deal_id)["three_party"]["by"] == "console" and GROUP in b.backend.watched
    assert b.prm.deal_action("group_unlink", {"deal_id": deal_id})["ok"] is False      # (nothing linked now)
    b.prm.stop()
    b.relay.stop()


# ── review 2026-11-09: her lines for her private drafts, her recall, a big group ──

def test_a_group_without_a_boss_is_never_told_to_her_private_drafts_as_her_three_party_group(bot):
    bot.backend.say(GROUP, BD, "她这周想先看样", bot.clock.now - 200)
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "寄样怎么弄", sender=BD))
    bot.prm.handle(bot.msg("私聊问一下寄样"))
    meta = json.loads(job_of(bot, CREATOR)["meta"])
    assert "three_party_chat" not in meta and "three_party" not in meta


def test_a_group_with_a_boss_linked_by_hand_is_told_to_her_private_drafts(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.backend.groups[GROUP]["members"].append(BOSS_ID)
    b.backend.say(GROUP, BOSS_ID, "寄 3 顶给她", clock.now - 200)
    linked(b, member=1)
    b.prm.handle(b.msg("私聊问一下寄样"))
    told = json.loads(job_of(b, CREATOR)["meta"])["three_party_chat"]
    assert [(x["who"], x["text"]) for x in told] == [("老板", "寄 3 顶给她")]
    b.prm.stop()
    b.relay.stop()


def recall(b, name, **extra):
    """WeChat's line of a recall in the group, naming ``name``."""
    line = f'"{name}" 撤回了一条消息'
    return gmsg(b, line, sender="", msg_type=10000, special={"kind": "system", "title": line}, **extra)


def held(b, packet):
    return bool(b.prm.store.packet(packet["id"])["needs_attention"])


def test_in_a_group_linked_by_hand_the_recall_of_somebody_else_we_know_holds_nothing(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    for name in ("测试商务", "品牌同事", BOSS):                        # her 商务, a colleague, the boss
        assert bot.prm.handle(recall(bot, name)) is True
    assert not held(bot, packet)
    assert not any(n["key"] == "recall" for n in bot.prm.store.deal(packet["deal_id"])["notices"])
    assert bot.prm.handle(recall(bot, "小美")) is True
    p = bot.prm.store.packet(packet["id"])
    assert p["status"] == "pending" and p["needs_attention"] and "撤回" in p["reason"]


def test_in_a_group_linked_by_hand_a_recall_under_a_name_we_do_not_know_is_hers(bot):
    """Review 2026-11-09: her recall under her group nickname was missed (the bot knows her by another name; a 百应
    friend's remark is 「抖音-…」): a name that is nobody else we know is taken for hers, the drafts wait for a person."""
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    assert bot.prm.handle(recall(bot, "美美呀")) is True
    assert held(bot, packet)


def test_a_recall_wechat_wrote_over_a_line_is_whoever_wrote_that_line(bot):
    """A recall in place (the backend's ``recalled_id``: the row of the line taken back) is hers only when that line
    was hers, whatever name it shows."""
    linked(bot, member=1)
    theirs = gmsg(bot, "我撤回一下", sender=BD)
    bot.prm.handle(theirs)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    assert bot.prm.handle(recall(bot, "美美呀", recalled_id=theirs["message_id"])) is True
    assert not held(bot, packet)                                      # (her 商务's line, under a name we do not know)
    hers = gmsg(bot, "我发错了")
    bot.prm.handle(hers)
    _, packet = drafted(bot)
    assert bot.prm.handle(recall(bot, "测试商务", recalled_id=hers["message_id"])) is True
    assert held(bot, packet)


def test_in_a_group_of_her_us_and_the_boss_any_recall_but_the_bosses_is_hers(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.backend.groups[GROUP]["members"] = [ME, CREATOR, BOSS_ID]
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    _, packet = drafted(b)
    line = f'"{BOSS}" 撤回了一条消息'
    b.prm.handle(gmsg(b, line, sender="", msg_type=10000, special={"kind": "system", "title": line}))
    assert b.prm.store.packet(packet["id"])["needs_attention"] is False
    line = '"美美呀" 撤回了一条消息'                                   # (her group nickname, not known here)
    b.prm.handle(gmsg(b, line, sender="", msg_type=10000, special={"kind": "system", "title": line}))
    assert b.prm.store.packet(packet["id"])["needs_attention"] is True
    b.prm.stop()
    b.relay.stop()


def test_in_a_group_linked_by_hand_others_redraft_her_waiting_draft_at_most_every_two_minutes(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, first = drafted(bot)
    bot.prm.handle(gmsg(bot, "我是她的商务", sender=BD))              # her draft did not hear it: drafted again
    redraft = job_of(bot)
    assert bot.prm.store.packet(first["id"])["status"] == "superseded" and redraft is not None
    _, second = drafted(bot)
    bot.clock.now += 10
    bot.prm.handle(gmsg(bot, "收到", sender=COLLEAGUE))               # within two minutes: owed, not now
    assert job_of(bot) is None and bot.prm.store.packet(second["id"])["status"] == "pending"
    assert bot.prm.sweep_owed_redrafts() == 0
    bot.clock.now += REDRAFT_GAP_SEC
    assert bot.prm.sweep_owed_redrafts() == 1
    assert bot.prm.store.packet(second["id"])["status"] == "superseded"
    lines = json.loads(job_of(bot)["meta"])["group"]["lines"]
    assert ("群成员", "收到") in [(x["who"], x["text"]) for x in lines]
    assert bot.prm.sweep_owed_redrafts() == 0                         # (owed once)


def test_in_a_group_linked_by_hand_the_bosss_line_redrafts_at_once(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.backend.groups[GROUP]["members"].append(BOSS_ID)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    drafted(b)
    b.prm.handle(gmsg(b, "我是她的商务", sender=BD))
    _, second = drafted(b)
    b.clock.now += 10
    b.prm.handle(gmsg(b, "给她寄 3 顶", sender=BOSS_ID))              # the boss: at once, whatever the gap
    assert b.prm.store.packet(second["id"])["status"] == "superseded" and job_of(b) is not None
    b.prm.stop()
    b.relay.stop()


# ── review 2026-11-09: the watch after a restart, 「是 N」, a group the scan found ──

def test_a_group_linked_while_unreadable_is_read_after_a_restart_from_the_link_on(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.backend.say(GROUP, CREATOR, "早上问的那句", clock.now - 200)
    b.backend.unreadable.add(GROUP)
    linked(b, member=1)
    at = clock.now
    assert b.backend.watched[GROUP] == at
    clock.now += 60
    restart(b, LinkBackend())
    b.prm.boss_groups.start()
    assert b.backend.watched[GROUP] == at                             # never the lines from before the link
    b.prm.stop()
    b.relay.stop()


def test_shi_n_counts_only_the_members_the_question_listed(bot):
    guests = [f"wxid_guest{n:02d}" for n in range(32)]
    bot.backend.groups[BIG]["members"] = [ME, *guests]
    bot.say("接群")
    question = bot.say("接群 2").split("\n")
    # (30 of 32 listed; nameless: their wxid's last 4 only, integrated review 2026-11-10 B-L7)
    assert question[30] == "30. （没有名字）…st29" and not any("…st30" in line for line in question)
    assert "人太多，只列了前 30 位：别的到控制台「合作单」页顶上的卡片里选" in question
    assert bot.say("是 31") == ("没有第 31 位：回「是 1」到「是 30」，「不接」作罢；只列了前 30 位，"
                              "别的到控制台「合作单」页顶上的卡片里选")
    assert bot.prm.store.boss_group(BIG) is None and len(bot.prm.boss_groups.offered(BIG)["members"]) == 32
    assert bot.say("是 30").startswith("接手了：群「品牌测试大群」")


def test_a_group_the_scan_found_waiting_on_the_boss_group_card_is_chosen_there(clock, tmp_path):
    b = setup(clock, tmp_path, on=True)
    b.prm.store.add_boss_group(BIG, name="品牌测试大群", found_at=clock.now, state="pending",
                               members=[{"wxid": CREATOR, "name": "小美"}, {"wxid": TESTER, "name": "测试达人二号"}],
                               boss=[{"wxid": BOSS_ID, "name": BOSS}])
    assert "2. 品牌测试大群 · 12 人" in b.say("接群") and "老板拉的群，在「老板拉的群」卡片里选" in b.say("接群")
    assert b.say("接群 2") == "群「品牌测试大群」是老板拉的群，在控制台「合作单」页顶上的「老板拉的群」卡片里等你选她：到那里点「是她」"
    assert b.prm.boss_groups.offered() is None and b.prm.store.boss_group(BIG)["state"] == "pending"
    b.prm.boss_groups._on = False                                     # (switched off: no card, it may be asked about)
    assert b.say("接群 品牌测试大群").startswith("群「品牌测试大群」（12 人）里谁是达人？")
    b.prm.stop()
    b.relay.stop()


# ── the second check (2026-11-09, after 46ef006) ───────────────────────────

def test_a_reply_going_out_in_parts_stops_when_the_link_ends(clock, tmp_path):
    """「不接群」 while a reply in two messages went out: the second went into the group all the same.  The group is
    looked at again before each message: the rest is not sent, the card says what went out (never 「请手动补发」), no
    notice asks to send it by hand, and the answer to 「不接群」 counts it."""
    b = setup(clock, tmp_path, prm_reply_parts=3)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    _, packet = drafted(b, "第一段\n\n第二段")
    said, real = [], b.prm._send

    def send(chat, text):
        result = real(chat, text)
        if len(b.texts) == 1:
            said.append(b.say(f"不接群 {GROUP_NAME}"))
        return result

    b.prm._send = send
    assert b.prm.decide(packet["id"], "send")["ok"]
    assert b.prm.send_approved() == 0 and b.texts == [(GROUP, "第一段")]
    p = b.prm.store.packet(packet["id"])
    assert (p["status"], p["error"]) == ("failed", f"分 2 条发，前 1 条发出去了；{GROUP_CLOSED_HEAD}，剩下的没发")
    assert not any(n["key"] == GROUP_UNSENT for n in b.prm.store.deal(p["deal_id"])["notices"])
    assert said == [f"不接了：群「{GROUP_NAME}」里她的话不再起草，群里的话和别的群一样不回（小美的合作单和私聊照旧）"
                    "；正在往群里发的 1 份不再往下发（已经发出去的留在群里）"]
    b.prm.stop()
    b.relay.stop()


def test_a_reply_with_a_file_stops_before_the_file_when_the_link_ends(bot, tmp_path):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "有图吗"))
    _, packet = drafted(bot, "发您看看")
    picture = tmp_path / "款式.png"
    picture.write_bytes(b"png")
    bot.prm.store._conn.execute("UPDATE action_packets SET attachments = ? WHERE id = ?", (json.dumps(
        [{"name": "款式.png", "path": str(picture), "size": 3, "sha256": "0" * 64}]), packet["id"]))
    bot.prm.store._conn.commit()
    real = bot.prm._send
    bot.prm._send = lambda chat, text: real(chat, text) and bot.say(f"不接群 {GROUP_NAME}") is not None
    assert bot.prm.decide(packet["id"], "send")["ok"]
    assert bot.prm.send_approved() == 0
    assert bot.texts == [(GROUP, "发您看看")] and bot.files == []
    assert bot.prm.store.packet(packet["id"])["error"] == f"文字发出去了，1 个文件发出去 0 个；{GROUP_CLOSED_HEAD}，剩下的没发"


def test_a_draft_back_after_the_link_ended_answers_nothing(bot):
    """It failed, but it saw to her notices (a 签收后问一句's), took the lines it carried and merged its slots and stage
    into her deal, as a reply sent would: now nothing of the deal changes, the lines it carried wait again."""
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    store = bot.prm.store
    store.set_notice(deal_id, "failed:999", "聚水潭显示签收后问她一句的草稿没写成", bot.clock.now, job=999, lane="reply",
                     signed=True, asked_at=bot.clock.now)
    bot.prm.handle(gmsg(bot, "寄样怎么弄"))
    first = job_of(bot)
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == first["id"]
    bot.finish(first["id"], ok=False, text="boom")                     # no draft: her line waits
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "在吗"))
    job = job_of(bot)                                                 # (it takes the first one's line along)
    assert next(n for n in store.deal(deal_id)["notices"] if n["key"] == f"failed:{first['id']}")["into"] == job["id"]
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == job["id"]
    bot.say(f"不接群 {GROUP_NAME}")
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="在的～", slots={"收件人": "小美"}, stage_proposal="address",
                                reason="kb/合作政策.md"))
    assert store.packet_for_job(job["id"])["status"] == "failed"
    deal = store.deal(deal_id)
    notices = {n["key"]: n for n in deal["notices"]}
    assert "failed:999" in notices                                    # (her 签收后问一句 not seen to)
    # her two lines (the first job's it carried, and its own) are the group's: counted on the deal, never carried on
    assert f"failed:{first['id']}" not in notices
    assert notices[f"group_left:{GROUP}"]["text"] == (f"她在群「{GROUP_NAME}」里还有 2 句话没回（{GROUP_CLOSED_HEAD}），"
                                                      "要回的话请到群里直接回")
    assert not deal["slots"] and not deal["stage_proposal"]          # (nothing of it merged)
    assert not any(GROUP_CLOSED_TEXT in n["text"] for n in deal["notices"])


def test_a_draft_of_hers_never_goes_into_the_group_once_it_is_linked_to_another_creator(clock, tmp_path):
    b = setup(clock, tmp_path)
    b.prm.store.set_gate("faq", "auto", clock.now)
    linked(b, member=1)                                               # 小美
    b.prm.handle(gmsg(b, "在吗"))
    job = job_of(b)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]           # her draft is being written
    b.say(f"不接群 {GROUP_NAME}")
    b.say("接群")
    b.say("接群 1")
    assert b.say("是 2").startswith(f"接手了：群「{GROUP_NAME}」→ 测试商务")   # linked again: to her 商务
    her, bd = (b.prm.store.party_by_wxid(w)["id"] for w in (CREATOR, BD))
    assert b.prm._group_closed(GROUP, her) == GROUP_CLOSED_TEXT and b.prm._group_closed(GROUP, bd) == ""
    b.finish(job["id"], draft(message_kind="faq", reply_draft="在的～", slots={}, stage_proposal="",
                              reason="kb/合作政策.md"))
    packet = b.prm.store.packet_for_job(job["id"])
    assert (packet["status"], packet["error"]) == ("failed", GROUP_CLOSED_TEXT)
    assert b.prm.send_approved() == 0 and b.texts == []
    b.prm.stop()
    b.relay.stop()


def test_a_platform_reply_verified_after_the_link_ended_leaves_a_notice_on_the_deal(bot):
    """The action was done (a sample order, say), its reply cannot go into the group: the card said so, the deal
    did not; now a ``platform:<id>`` notice says it, until 「我已处理」."""
    from src.prm.store import EXECUTING

    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    store = bot.prm.store
    store._conn.execute("UPDATE action_packets SET status = ?, tool = 'jst.sample_order', platform = ? WHERE id = ?",
                        (EXECUTING, json.dumps({"action": {"action": "jst.sample_order"}, "key": "k1"}), packet["id"]))
    store._conn.commit()
    bot.say(f"不接群 {GROUP_NAME}")
    assert store.move(packet["id"], (EXECUTING,), "approved", decided_at=bot.clock.now)   # verified on the platform
    assert bot.prm.send_approved() == 0 and bot.texts == []
    p = store.packet(packet["id"])
    want = f"平台动作已经核验成功，但回复没发出去：{GROUP_CLOSED_TEXT}；请在微信里手动发给达人"
    assert (p["status"], p["error"]) == ("failed", want)
    notice = next(n for n in store.deal(p["deal_id"])["notices"] if n["key"] == f"platform:{p['id']}")
    assert notice["text"] == f"#{p['id']} {want}"
    assert bot.prm.decide(p["id"], "ack")["ok"] is True
    assert not any(n["key"] == f"platform:{p['id']}" for n in store.deal(p["deal_id"])["notices"])


def chatter(b, n, start=0):
    """``n`` lines of a colleague in the group, REDRAFT_GAP_SEC apart; how many of them had her draft drafted again (the
    Mac writes each redraft at once)."""
    jobs = 0
    for i in range(start, start + n):
        b.clock.now += REDRAFT_GAP_SEC + 1
        b.prm.handle(gmsg(b, f"群里闲聊 {i}", sender=COLLEAGUE))
        if job_of(b) is not None:
            jobs += 1
            drafted(b)
    return jobs


def waiting(b):
    return [p for p in b.prm.snapshot()["pending"] if p["channel"] == "group"]


def test_others_lines_redraft_her_waiting_draft_at_most_three_times_then_her_card_counts_them(bot):
    """A chatty group made a Mac job every two minutes while her draft waited (about 28 an hour): now at most
    REDRAFT_MAX, then her draft's card says how many lines it did not hear; her next line starts again."""
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    drafted(bot)
    assert chatter(bot, 6) == REDRAFT_MAX == 3
    [card] = waiting(bot)
    assert card["unheard"] == 3                                       # (the lines after the last redraft)
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "还在吗"))                               # her line: a draft of its own
    drafted(bot)
    assert "unheard" not in waiting(bot)[0]
    assert chatter(bot, 1, start=6) == 1                              # (counted from 0 again)


def test_the_consoles_redraft_drafts_her_group_draft_again_whatever_the_cap(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    drafted(bot)
    chatter(bot, 4)
    [card] = waiting(bot)
    assert card["unheard"] == 1
    assert bot.prm.decide(card["id"], "redraft") == {"ok": True, "status": "redrafted"}
    assert bot.prm.store.packet(card["id"])["status"] == "superseded" and job_of(bot) is not None
    drafted(bot)
    [fresh] = waiting(bot)
    assert "unheard" not in fresh
    assert bot.prm.decide(card["id"], "redraft")["ok"] is False        # (not waiting any more)
    assert chatter(bot, 1, start=4) == 1                              # (the cap counts from 0 again)
    bot.prm.handle(bot.msg("私聊问一下"))
    _, private = drafted(bot, chat=CREATOR)
    assert bot.prm.decide(private["id"], "redraft") == {"ok": False, "error": "只有群里还在「待审批」等着的草稿能重新起草"}


def test_a_group_the_scan_found_is_asked_about_here_when_the_boss_group_card_cannot_take_it(clock, tmp_path):
    """PRM_BOSS_GROUPS on but the scan cannot run (``why_not``: no boss named): the card's buttons are off, so 「接群」
    asks about the group itself (else it could be linked nowhere)."""
    b = setup(clock, tmp_path, on=True, prm_group_with="")
    assert b.prm.boss_groups.why_not()
    b.prm.store.add_boss_group(BIG, name="品牌测试大群", found_at=clock.now, state="pending",
                               members=[{"wxid": CREATOR, "name": "小美"}, {"wxid": TESTER, "name": "测试达人二号"}],
                               boss=[])
    assert "老板拉的群，在「老板拉的群」卡片里选" not in b.say("接群")
    assert b.say("接群 2").startswith("群「品牌测试大群」（12 人）里谁是达人？")
    assert b.prm.boss_groups.offered(BIG) is not None
    b.prm.stop()
    b.relay.stop()


def test_a_job_cancelled_as_the_link_ends_gives_back_what_it_took(bot):
    """Its lines and the records it was told (what she said while a person had her deal) were lost: now they wait for
    her next draft job, as a job's that gave no draft."""
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    assert bot.prm.deal_action("mode", {"deal_id": deal_id, "mode": "human"})["ok"]
    bot.prm.handle(gmsg(bot, "我先问问老板"))                          # a person has her deal: only recorded
    assert bot.prm.deal_action("mode", {"deal_id": deal_id, "mode": "copilot"})["ok"]
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "在吗"))
    job = job_of(bot)
    assert any("我先问问老板" in h["text"] for h in json.loads(job["meta"])["history"])
    bot.say(f"不接群 {GROUP_NAME}")
    assert bot.relay.store.get(job["id"])["status"] == "cancelled"
    notice = next(n for n in bot.prm.store.deal(deal_id)["notices"] if n["key"] == f"failed:{job['id']}")
    assert notice["text"].startswith(f"#{job['id']} 的草稿任务取消了（{GROUP_CLOSED_HEAD}）")
    bot.clock.now += 5
    bot.prm.handle(bot.msg("私聊问一下"))
    meta = json.loads(job_of(bot, CREATOR)["meta"])
    assert any("我先问问老板" in h["text"] for h in meta["history"])  # (given back: her next job hears it)


def test_a_commission_step_cancelled_as_the_link_ends_says_so_and_the_chain_is_looked_at_again(bot, monkeypatch):
    linked(bot, member=1)
    row = bot.prm.store.boss_group(GROUP)
    refreshed = []
    monkeypatch.setattr(bot.prm, "_chain_refresh", lambda deal_id, now: refreshed.append(deal_id))
    _, job_id = bot.relay.submit(GROUP, GROUP, "", kind="prm", fresh=True, meta={
        "party_id": row["party_id"], "deal_id": row["deal_id"], "name": "小美", "system_note": "开专属佣金：测试帽子",
        "channel": "group", "group": bot.prm.boss_groups.job_group(GROUP)})
    bot.say(f"不接群 {GROUP_NAME}")
    assert bot.relay.store.get(job_id)["status"] == "cancelled"
    notice = next(n for n in bot.prm.store.deal(row["deal_id"])["notices"] if n["key"] == f"failed:{job_id}")
    assert notice["lane"] == "system" and notice["text"].endswith("系统不会自己再起草这一步，请运营处理")
    assert refreshed == [row["deal_id"]]


@pytest.mark.parametrize("how", ["cancelled", "failed", "private"])
@pytest.mark.parametrize("action, word", [("", "开专属佣金"), ("dy.channel_goods", "建专属链接")])
def test_a_chain_step_of_a_group_that_closes_says_which_step_it_was(bot, monkeypatch, action, word, how):
    """合并 channel-goods-convention (2026-11-09): a step of the chain is a 专属佣金's or a 专属链接's
    (``meta.step.action``); the notice of one cancelled as the link ends, or failed after it (it was being written),
    names it once (``step_word``; the failed one's said 「开专属佣金这一步的开专属佣金这一步的草稿没写成」 before); one
    that failed in her private chat names its step too (a 专属链接's said 「开专属佣金这一步」)."""
    linked(bot, member=1)
    row = bot.prm.store.boss_group(GROUP)
    monkeypatch.setattr(bot.prm, "_chain_refresh", lambda deal_id, now: None)
    step = {"packet": None, "delivered": False, "item": "3000000000000000001", **({"action": action} if action else {})}
    chat = CREATOR if how == "private" else GROUP
    _, job_id = bot.relay.submit(chat, chat, "", kind="prm", fresh=True, meta={
        "party_id": row["party_id"], "deal_id": row["deal_id"], "name": "小美", "system_note": f"{word}：测试帽子",
        "step": step, **({} if how == "private" else {"channel": "group", "group": bot.prm.boss_groups.job_group(GROUP)})})
    if how != "cancelled":
        bot.clock.now += SETTLE_SEC + 1
        assert bot.relay.next_job(0, ("prm",))["id"] == job_id
    if how != "private":
        bot.say(f"不接群 {GROUP_NAME}")
    if how != "cancelled":
        bot.finish(job_id, ok=False, text="出错了")
    assert bot.relay.store.get(job_id)["status"] == ("cancelled" if how == "cancelled" else "failed")
    notice = next(n for n in bot.prm.store.deal(row["deal_id"])["notices"] if n["key"] == f"failed:{job_id}")
    said = {"cancelled": f"草稿任务取消了（{GROUP_CLOSED_HEAD}）", "failed": f"草稿没写成（{GROUP_CLOSED_HEAD}）",
            "private": "草稿没写成："}[how]
    assert notice["text"].startswith(f"#{job_id} {word}这一步的{said}")
    assert notice["text"].count("这一步的") == 1


def test_a_group_with_a_boss_linked_while_her_deal_keeps_another_three_party_group_is_told_apart(clock, tmp_path):
    """Its lines were told to her private drafts mixed with her 三方群's, then not at all: each group's lines say their
    group now (``group``), the Mac's prompt tells them apart."""
    b = setup(clock, tmp_path)
    party = b.prm.register(CREATOR, "小美", "测试")
    deal = b.prm.store.deal_of(party["id"])
    b.prm.store.update_deal(deal["id"], clock.now, three_party={"state": "created", "at": clock.now, "by": "console",
                                                                "chat": OURS, "name": "品牌测试×小美", "members": 3})
    b.prm._group_line(party, OURS, "boss", "三方群里说的", clock.now - 30, clock.now)
    b.backend.groups[GROUP]["members"].append(BOSS_ID)
    b.backend.say(GROUP, BOSS_ID, "另一个群里说的", clock.now - 20)
    assert linked(b, member=1).split("；")[-1] == "她原来的三方群保留"
    b.prm.handle(gmsg(b, "群里又说一句", sender=BOSS_ID))
    b.prm.handle(b.msg("私聊问一下"))
    told = json.loads(job_of(b, CREATOR)["meta"])["three_party_chat"]
    assert [(x["group"], x["text"]) for x in told] == [("三方群「品牌测试×小美」", "三方群里说的"),
                                                       (f"群「{GROUP_NAME}」", "另一个群里说的"),
                                                       (f"群「{GROUP_NAME}」", "群里又说一句")]
    b.prm.stop()
    b.relay.stop()


# ── the third check (2026-11-09, after 0663be7) ────────────────────────────

def test_her_group_line_is_never_answered_in_her_private_chat_once_the_link_ends(clock, tmp_path):
    """Her group job was cancelled at 「不接群」: its line went into her private draft, and with the gate on auto out to
    her private chat unreviewed.  Lines she wrote in a group are never carried into another chat's draft: the deal says
    how many wait in the group, until 「已处理」 (also with the bot stopped)."""
    b = setup(clock, tmp_path)
    b.prm.store.set_gate("faq", "auto", clock.now)
    linked(b, member=1)
    deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
    b.prm.handle(gmsg(b, "群里问：寄样怎么弄"))
    b.say(f"不接群 {GROUP_NAME}")
    b.clock.now += 5
    b.prm.handle(b.msg("私聊：在吗"))
    assert "寄样怎么弄" not in job_of(b, CREATOR)["text"]
    notice = next(n for n in b.prm.store.deal(deal_id)["notices"] if n["key"] == f"group_left:{GROUP}")
    assert notice["text"] == f"她在群「{GROUP_NAME}」里还有 1 句话没回（{GROUP_CLOSED_HEAD}），要回的话请到群里直接回"
    assert not any("并进" in n["text"] for n in b.prm.store.deal(deal_id)["notices"])
    [alert] = b.prm.snapshot()["group_alerts"]
    assert (alert["deal_id"], alert["chat"], alert["text"]) == (deal_id, GROUP, notice["text"])
    assert b.prm.deal_action("group_left_done", {"deal_id": deal_id, "chat": GROUP}) == {"ok": True}
    assert b.prm.snapshot()["group_alerts"] == []
    assert b.prm.deal_action("group_left_done", {"deal_id": deal_id, "chat": GROUP})["ok"] is False
    assert "不接的群里她没回的话已处理" in names(b, deal_id)
    from src.prm.service import apply_deal_action

    b.prm._note_group_left(deal_id, GROUP, 1, clock.now)
    assert apply_deal_action(b.prm.store, "group_left_done", {"deal_id": deal_id, "chat": GROUP}, now=clock.now,
                             takeover_sec=7200) == {"ok": True}            # (the stopped bot's console)
    b.prm.stop()
    b.relay.stop()


def test_her_lines_a_closed_group_left_unanswered_are_seen_to_when_she_writes_there_again(bot):
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    bot.prm.handle(gmsg(bot, "群里问：寄样怎么弄"))
    bot.say(f"不接群 {GROUP_NAME}")
    assert any(n["key"] == f"group_left:{GROUP}" for n in bot.prm.store.deal(deal_id)["notices"])
    bot.say("接群")
    bot.say("接群 1")
    bot.say("是 1")                                                   # linked again
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "我又来了"))
    assert not any(n["key"] == f"group_left:{GROUP}" for n in bot.prm.store.deal(deal_id)["notices"])


def test_the_lines_of_a_failed_group_job_wait_for_nothing_once_the_link_ends(clock, tmp_path):
    """A group job that gave no draft keeps her lines for the group's next draft; with another group linked, her
    private draft never took them and its notice promised 「会并进她下一条消息的草稿」 for ever."""
    b = setup(clock, tmp_path)
    linked(b, member=1)
    deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
    b.prm.handle(gmsg(b, "群里问：寄样怎么弄"))
    job = job_of(b)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    b.finish(job["id"], ok=False, text="boom")                       # no draft: the line waits for the group's next one
    assert next(n for n in b.prm.store.deal(deal_id)["notices"] if n["key"] == f"failed:{job['id']}")["lines"]
    b.say(f"不接群 {GROUP_NAME}")
    notices = {n["key"]: n for n in b.prm.store.deal(deal_id)["notices"]}
    assert f"failed:{job['id']}" not in notices and notices[f"group_left:{GROUP}"]["count"] == 1
    b.clock.now += 5
    b.prm.handle(b.msg("私聊：在吗"))
    assert job_of(b, CREATOR)["text"].endswith("私聊：在吗") and "寄样" not in job_of(b, CREATOR)["text"]
    b.prm.stop()
    b.relay.stop()


def took_over_then_her_group_line(b):
    """A person had her deal (an operator's own line in the group, then hers there: recorded), then copilot again and
    her next group line: its job is told the records (``meta.history``)."""
    linked(b, member=1)
    deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
    b.prm.on_own_message(own(b, "我是运营，我看一下哈"))
    b.clock.now += 5
    b.prm.handle(gmsg(b, "好的我等你"))
    assert b.prm.store.deal(deal_id)["mode"] == "human"
    b.prm.store.update_deal(deal_id, b.clock.now, mode="copilot", mode_until=None)
    b.clock.now += 60
    b.prm.handle(gmsg(b, "寄样怎么弄"))
    job = job_of(b)
    assert json.loads(job["meta"])["history"]
    return deal_id, job


@pytest.mark.parametrize("written", [False, True])
def test_the_records_a_group_job_took_go_to_her_next_draft_when_the_link_ends(clock, tmp_path, written):
    """Cancelled in the queue, or written and back after the link ended: its records (the takeover's lines) reached no
    draft again; now her next one is told them, her group line not."""
    b = setup(clock, tmp_path)
    deal_id, job = took_over_then_her_group_line(b)
    if written:
        b.clock.now += SETTLE_SEC + 1
        assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    b.say(f"不接群 {GROUP_NAME}")
    if written:
        b.finish(job["id"], draft(message_kind="faq", reply_draft="在的", slots={}, stage_proposal="",
                                  reason="kb/合作政策.md"))
    b.clock.now += 5
    b.prm.handle(b.msg("私聊问一下"))
    private = job_of(b, CREATOR)
    texts = [h["text"] for h in json.loads(private["meta"])["history"]]
    assert any("我是运营，我看一下哈" in t for t in texts) and any("好的我等你" in t for t in texts)
    assert "寄样怎么弄" not in private["text"]
    b.prm.stop()
    b.relay.stop()


@pytest.mark.parametrize("written", [False, True])
def test_a_handcards_job_of_a_group_that_closes_leaves_no_records_it_was_only_told(clock, tmp_path, written):
    """合并 handcard-auto (2026-11-09): a 「做手卡」 job (``meta.handcards``, PRM_HANDCARDS) is told the takeover's
    records, it does not take them (``_ask_handcards``, like a 补发货盘's).  Cancelled as the link ends, or written and
    back after it: it leaves no notice of records (``_group_job_closed``), so her next draft hears them once, from the
    backlog, not twice."""
    b = setup(clock, tmp_path, prm_handcards="on")
    linked(b, member=1)
    deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
    b.prm.on_own_message(own(b, "我是运营，我看一下哈"))
    b.clock.now += 5
    b.prm.handle(gmsg(b, "好的我等你"))                                # a person has her deal: only recorded
    assert b.prm.store.deal(deal_id)["mode"] == "human"
    b.prm.store.update_deal(deal_id, b.clock.now, mode="copilot", mode_until=None)
    b.clock.now += 60
    asked = b.prm.deal_action("handcards", {"deal_id": deal_id})
    assert asked["ok"], asked
    job = b.relay.store.get(asked["job"])
    meta = json.loads(job["meta"])
    assert job["chat_id"] == GROUP and isinstance(meta["handcards"], dict)       # (no private chat: the group)
    assert any("好的我等你" in h["text"] for h in meta["history"])
    if written:
        b.clock.now += SETTLE_SEC + 1
        assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    b.say(f"不接群 {GROUP_NAME}")
    if written:
        b.finish(job["id"], draft(message_kind="faq", reply_draft="老师手卡在这里～", slots={}, stage_proposal="",
                                  reason="kb/合作政策.md"))
    else:
        assert b.relay.store.get(job["id"])["status"] == "cancelled"
    assert not any(n["key"] == f"failed:{job['id']}" for n in b.prm.store.deal(deal_id)["notices"])
    b.clock.now += 5
    b.prm.handle(b.msg("私聊问一下"))
    texts = [h["text"] for h in json.loads(job_of(b, CREATOR)["meta"])["history"]]
    assert sum("好的我等你" in t for t in texts) == 1
    b.prm.stop()
    b.relay.stop()


def test_a_draft_of_a_group_linked_by_hand_told_in_wechat_names_no_boss(clock, tmp_path):
    """合并 wechat-review (2026-11-09): 微信里审批草稿 tells the admin a draft for a group linked by hand goes to that
    group, where everybody sees it: not 「老板拉的群」 (the console and the Mac's prompt do not say it either)."""
    b = setup(clock, tmp_path, prm_wechat_review="on")
    linked(b, member=1)
    b.prm.handle(gmsg(b, "这款多少钱呀"))
    drafted(b, reply="您好～这款日常价 299 哦")
    b.prm.wechat_review.sweep()
    [told] = [t for chat, t in b.texts if chat == ADMIN and "草稿 ·" in t]
    first, second = told.split("\n")[:2]
    assert first.endswith(f"群里「{GROUP_NAME}」") and second == "（发在群里，不是她的私聊：群里的人都看得到）"
    assert "老板" not in told
    b.prm.stop()
    b.relay.stop()


@pytest.mark.parametrize("step", [False, True])
def test_a_draft_the_mac_could_not_ask_the_bot_about_back_after_the_link_ended_is_a_closed_groups(bot, monkeypatch, step):
    """合并 channel-goods-convention 小修 (2026-11-09): the Mac left a 专属链接 out, not able to ask the bot
    (``platform_held`` unasked, ``_unasked``), and the job's group closed meanwhile: as any draft back after its link
    ended (``_group_job_closed``).  Her reply with nothing left: her group line is the group's (``group_left``), never a
    notice waiting for a draft of that group that will not come; a 专属链接 step: not owed again for later."""
    linked(bot, member=1)
    row = bot.prm.store.boss_group(GROUP)
    deal_id = row["deal_id"]
    monkeypatch.setattr(bot.prm, "_chain_refresh", lambda deal_id, now: None)
    if step:
        _, job_id = bot.relay.submit(GROUP, GROUP, "", kind="prm", fresh=True, meta={
            "party_id": row["party_id"], "deal_id": deal_id, "name": "小美", "system_note": "建专属链接：测试帽子",
            "step": {"packet": None, "delivered": False, "item": "3000000000000000001", "action": "dy.channel_goods"},
            "channel": "group", "group": bot.prm.boss_groups.job_group(GROUP)})
    else:
        bot.prm.handle(gmsg(bot, "专属链接能弄吗"))
        job_id = job_of(bot)["id"]
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == job_id
    bot.say(f"不接群 {GROUP_NAME}")
    bot.finish(job_id, draft(message_kind="faq", reply_draft="", slots={}, stage_proposal="", platform_held="unasked",
                             reason="这台 Mac 连着几次都问不到 Windows 上的机器人…"))
    notices = {n["key"]: n for n in bot.prm.store.deal(deal_id)["notices"]}
    owed = [n for n in notices.values() if n.get("step")]
    assert not owed and not any(n.get("retry_at") for n in notices.values())          # (not asked again later)
    assert bot.prm.store.packet_for_job(job_id) is None
    if step:
        assert notices[f"failed:{job_id}"]["text"].startswith(f"#{job_id} 建专属链接这一步的草稿写好时{GROUP_CLOSED_HEAD}")
    else:
        assert f"failed:{job_id}" not in notices and notices[f"group_left:{GROUP}"]["count"] == 1
        bot.clock.now += 5
        bot.prm.handle(bot.msg("私聊：在吗"))
        assert "专属链接能弄吗" not in job_of(bot, CREATOR)["text"]


def test_a_line_missed_by_a_group_job_back_after_the_link_ended_is_told_to_her_next_draft(bot):
    """Her line read too late (``missed``) went with a group job that came back closed: it reached no draft, its notice
    stayed for ever.  Now her next draft is told it, and its notice goes once that draft is written."""
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    bot.prm.handle(gmsg(bot, "很久以前的一句", ts=bot.clock.now - 20 * 3600))
    assert any(n["key"] == "missed" for n in bot.prm.store.deal(deal_id)["notices"])
    bot.clock.now += 5
    bot.prm.handle(gmsg(bot, "在吗"))
    job = job_of(bot)
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == job["id"]
    bot.say(f"不接群 {GROUP_NAME}")
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="在的", slots={}, stage_proposal="",
                                reason="kb/合作政策.md"))
    bot.clock.now += 5
    bot.prm.handle(bot.msg("私聊问一下"))
    meta = json.loads(job_of(bot, CREATOR)["meta"])
    assert meta["missed"]["count"] == 1 and any("很久以前的一句" in h["text"] for h in meta["history"])
    drafted(bot, chat=CREATOR)
    notices = bot.prm.store.deal(deal_id)["notices"]
    assert not any(n["key"] == "missed" for n in notices) and not any("#None" in n["text"] for n in notices)


@pytest.mark.parametrize("how", ["night", "auto_mode"])
def test_a_draft_the_bot_approved_itself_that_never_went_out_is_no_persons_decision(clock, tmp_path, how):
    """Closed after the night window (or 自动模式) approved it: it kept its ``decided_at`` and counted as a person's
    unchanged decision for the gate.  「不算进门槛统计」."""
    from src.prm import gates

    b = setup(clock, tmp_path, prm_linked_groups_auto="on",            # (off: nothing approves it by itself)
              **({"prm_night_auto_hours": "14:00-15:00"} if how == "night" else {}))
    if how == "auto_mode":
        b.prm.set_auto_mode(True)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "在吗"))
    job = job_of(b)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    b.say(f"不接群 {GROUP_NAME}")
    b.finish(job["id"], draft(message_kind="faq", reply_draft="在的，稍后详细回复您～", slots={}, stage_proposal="",
                              reason="kb/合作政策.md"))
    p = b.prm.store.packet_for_job(job["id"])
    assert (p["status"], p["route"], p["decided_at"]) == ("failed", "review", None)
    assert p["id"] not in [r["id"] for r in b.prm.store.reviewed("faq", 0, 50)]
    b.prm.stop()
    b.relay.stop()


@pytest.mark.parametrize("name", ["老板昵称", "路人甲", "测试商务", "老板测试"])
def test_a_recall_in_a_group_linked_by_hand_she_did_not_write_in_lately_holds_nothing(clock, tmp_path, name):
    """A stranger's recall, the boss's under his nickname, held every draft of her deal (her private one approved by
    its gate went back to waiting); her 商务's and the boss's by the names we know never did."""
    b = setup(clock, tmp_path)
    b.backend.groups[GROUP]["members"].append(BOSS_ID)
    linked(b, member=1)
    b.prm.store.set_gate("faq", "auto", clock.now)
    b.prm.handle(b.msg("私聊：在吗"))
    _, private = drafted(b, chat=CREATOR)
    assert private["status"] == "approved"
    b.prm.handle(recall(b, name))
    p = b.prm.store.packet(private["id"])
    assert (p["status"], p["needs_attention"]) == ("approved", False)
    assert not any(n["key"] == "recall" for n in b.prm.store.deal(p["deal_id"])["notices"])
    b.prm.stop()
    b.relay.stop()


def test_a_recall_under_a_name_we_do_not_know_right_after_her_line_there_is_hers(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)                                         # (her line SETTLE_SEC ago)
    bot.prm.handle(recall(bot, "美美呀"))
    assert held(bot, packet)


def test_the_members_of_a_group_linked_by_hand_are_read_again_once_a_minute(bot):
    linked(bot, member=1)
    bot.backend.names[BD] = "她的商务新名字"
    bot.clock.now += bg.BOSS_SCAN_SEC + 1
    bot.prm.boss_groups.sweep()
    assert {"wxid": BD, "name": "她的商务新名字"} in bot.prm.store.boss_group(GROUP)["members"]
    bot.prm.handle(gmsg(bot, "在吗"))
    _, packet = drafted(bot)
    bot.clock.now += HER_RECALL_SEC + 1
    bot.prm.handle(recall(bot, "她的商务新名字"))                    # (her 商务 by the name she has now)
    assert not held(bot, packet)


def test_an_opening_that_would_not_be_written_again_is_not_dropped_by_a_redraft(bot):
    """The console's 「写开场白」 into a group she spoke in: the next member line (or 「重新起草」) put it out of date and
    the group 开场白 was refused, the API said ok all the same.  It stays, the API says why."""
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    bot.prm.handle(gmsg(bot, "在吗"))
    _, first = drafted(bot)
    assert bot.prm.decide(first["id"], "drop")["ok"]                # never answered: 「写开场白」 is offered
    bot.clock.now += 60
    out = bot.prm.ask_opening(deal_id)
    assert out["ok"] is True
    job = bot.relay.store.get(out["job"])
    assert job["chat_id"] == GROUP
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == job["id"]
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="您好～我是品牌的运营", slots={}, stage_proposal="",
                                reason="kb/合作政策.md"))
    opening = bot.prm.store.packet_for_job(job["id"])
    assert opening["status"] == "pending"
    bot.clock.now += REDRAFT_GAP_SEC + 1
    bot.prm.handle(gmsg(bot, "大家好", sender=COLLEAGUE))
    assert bot.prm.store.packet(opening["id"])["status"] == "pending" and job_of(bot) is None
    assert bot.prm.decide(opening["id"], "redraft") == {
        "ok": False, "error": "这份开场白没重新起草：她已经在群里说过话了：她的话会自己起草，不用开场白"}
    assert bot.prm.store.packet(opening["id"])["status"] == "pending"


def test_redraft_takes_only_her_reply_drafts_in_the_group(bot):
    """A shipping notice in the group answered ok and had her reply draft drafted again."""
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "群里：在吗"))
    _, reply = drafted(bot)
    store = bot.prm.store
    notice = store.add_packet(deal_id=reply["deal_id"], party_id=reply["party_id"], job_id=None, chat_id=GROUP,
                              kind="shipping_notice", text="已发货～", attachments=[], intent="", slots={},
                              stage_proposal="", needs_attention=False, reason="", meta={"channel": "group"},
                              now=bot.clock.now)
    assert bot.prm.decide(notice, "redraft") == {
        "ok": False, "error": "只有她在群里的回复草稿能重新起草（发货通知、开专属佣金这一步不行）"}
    assert store.packet(reply["id"])["status"] == "pending" and job_of(bot) is None
    first = bot.prm.decide(reply["id"], "redraft")
    assert first["ok"] is True and bot.prm.decide(reply["id"], "redraft")["ok"] is False
    assert len([j for j in bot.relay.store.recent(GROUP, limit=20) if j["status"] == "pending"]) == 1


def test_the_last_redraft_allowed_says_what_came_while_it_was_written(bot):
    """The cap's last redraft was being written when another line came: its card did not count it."""
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    drafted(bot)
    for n in range(REDRAFT_MAX):
        bot.clock.now += REDRAFT_GAP_SEC + 1
        bot.prm.handle(gmsg(bot, f"闲聊 {n}", sender=COLLEAGUE))
        if n < REDRAFT_MAX - 1:
            drafted(bot)
    job = job_of(bot)                                                # the last redraft allowed, being written
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == job["id"]
    bot.clock.now += 10
    bot.prm.handle(gmsg(bot, "写的时候又说了一句", sender=COLLEAGUE))
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="在的", slots={}, stage_proposal="",
                                reason="kb/合作政策.md"))
    [card] = waiting(bot)
    assert card["unheard"] == 1


def test_the_cap_is_one_waiting_drafts_an_operators_decision_starts_it_again(bot):
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "在吗"))
    drafted(bot)
    chatter(bot, REDRAFT_MAX)
    [card] = waiting(bot)
    assert bot.prm._auto_redrafts[GROUP] == REDRAFT_MAX
    assert bot.prm.decide(card["id"], "drop")["ok"]                  # decided: the next draft counts from 0
    assert GROUP not in bot.prm._auto_redrafts and GROUP not in bot.prm._lines_since


# ── the final check (2026-11-09, after 00e9cac) ────────────────────────────

@pytest.mark.parametrize("skew", [3 * 3600, -3 * 3600])
@pytest.mark.parametrize("later", [0, 600])
def test_the_recall_window_reads_both_times_on_wechats_clock(clock, tmp_path, skew, later):
    """Her last line there is on WeChat's clock, the recall was compared on this PC's: with the clock 3 hours off, her
    recall right after her line was missed (or one ten minutes after it taken for hers)."""
    b = setup(clock, tmp_path)
    linked(b, member=1)
    b.prm._skew = float(skew)                                         # (this PC minus WeChat, as measured)
    wechat = lambda: b.clock.now - skew                               # noqa: E731  (WeChat's clock)
    b.prm.handle(gmsg(b, "在吗", ts=wechat()))
    _, packet = drafted(b)
    b.clock.now += later
    b.prm.handle(recall(b, "美美呀", ts=wechat()))                    # (a name we do not know: her group nickname?)
    assert held(b, packet) is (later == 0)
    b.prm.stop()
    b.relay.stop()


@pytest.mark.parametrize("how", ["gate", "night"])
def test_the_mac_hears_a_draft_the_bot_approved_did_not_go_out_as_its_group_closed(clock, tmp_path, how):
    """Its ``decided_at`` cleared (no person decided it: not in the gate's count), the Mac was not told any more that
    it did not go out (``outcomes``)."""
    b = setup(clock, tmp_path, prm_linked_groups_auto="on",            # (off: nothing approves it by itself)
              **({"prm_night_auto_hours": "14:00-15:00"} if how == "night" else {}))
    if how == "gate":
        b.prm.store.set_gate("faq", "auto", clock.now)
    linked(b, member=1)
    b.prm.handle(gmsg(b, "寄样怎么弄"))
    job = job_of(b)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    b.say(f"不接群 {GROUP_NAME}")
    b.finish(job["id"], draft(message_kind="faq", reply_draft="寄样的话您发一下地址～", slots={}, stage_proposal="",
                              reason="kb/合作政策.md"))
    packet = b.prm.store.packet_for_job(job["id"])
    assert (packet["status"], packet["decided_at"]) == ("failed", None)
    assert packet["id"] not in [r["id"] for r in b.prm.store.reviewed("faq", 0, 50)]
    b.clock.now += 5
    b.prm.handle(b.msg("私聊：在吗"))
    sent = json.loads(job_of(b, CREATOR)["meta"])["sent"]
    assert [(s["what"], s["text"]) for s in sent] == [("failed", "寄样的话您发一下地址～")]
    b.prm.stop()
    b.relay.stop()


@pytest.mark.parametrize("order", ["lost_then_unlink", "unlink_then_lost"])
@pytest.mark.parametrize("back", ["done", "failed"])
def test_a_group_job_lost_and_back_across_the_unlink_counts_her_line_once(clock, tmp_path, order, back):
    """Lost, the link ended, the Mac back (with a draft, or failed): her one line was counted twice."""
    b = setup(clock, tmp_path)
    linked(b, member=1)
    deal_id = b.prm.store.boss_group(GROUP)["deal_id"]
    b.prm.handle(gmsg(b, "寄样怎么弄"))
    job = job_of(b)
    b.clock.now += SETTLE_SEC + 1
    assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
    if order == "unlink_then_lost":
        b.say(f"不接群 {GROUP_NAME}")
    b.clock.now += 1000
    b.relay.tick()                                                    # (the Mac went silent: lost)
    if order == "lost_then_unlink":
        b.say(f"不接群 {GROUP_NAME}")
    b.relay.heartbeat(job["id"])                                      # (it is back)
    if back == "done":
        b.finish(job["id"], draft(message_kind="faq", reply_draft="在的", slots={}, stage_proposal="",
                                  reason="kb/合作政策.md"))
    else:
        b.finish(job["id"], ok=False, text="出错了")
    [left] = [n for n in b.prm.store.deal(deal_id)["notices"] if n["key"] == f"group_left:{GROUP}"]
    assert left["count"] == 1 and "还有 1 句话没回" in left["text"]
    b.prm.stop()
    b.relay.stop()


def test_after_the_group_gone_the_notice_does_not_say_to_answer_in_the_group(bot):
    """「群已经不在了」: there is no group to answer her in (not 「请到群里直接回」); 「不接群」 says it as before."""
    party = bot.prm.register(CREATOR, "小美", "测试")
    deal = bot.prm.store.deal_of(party["id"])
    bot.backend.groups[OURS] = {"name": "品牌测试×小美", "members": [ME, BOSS_ID, CREATOR]}
    bot.backend.active[OURS] = bot.clock.now - 10
    bot.prm.store.update_deal(deal["id"], bot.clock.now, three_party={"state": "created", "at": bot.clock.now - 86400,
                                                                      "by": "console", "chat": OURS,
                                                                      "name": "品牌测试×小美", "members": 3})
    bot.say("接群")
    bot.say("接群 1")
    bot.say("是 1")
    bot.prm.handle(gmsg(bot, "什么时候寄呀", chat=OURS))
    assert bot.prm.deal_action("group_none", {"deal_id": deal["id"]})["ok"] is True
    row = bot.prm.store.boss_group(OURS)
    assert (row["state"], row["why"]) == ("gone", bg.GONE_WHY)
    [left] = [n for n in bot.prm.store.deal(deal["id"])["notices"] if n["key"] == f"group_left:{OURS}"]
    assert left["text"] == "她在群「品牌测试×小美」里还有 1 句话没回（群已经不在了），需要的话请在微信里找她"


@pytest.mark.parametrize("state", ["no_reply", "superseded"])
def test_a_closed_groups_draft_not_to_be_sent_anyway_answers_nothing_either(bot, state):
    """Back after the unlink with nothing to say (no reply needed), or out of date (a person has her deal): it still
    merged its slots and stage into her deal and saw to her notices."""
    linked(bot, member=1)
    deal_id = bot.prm.store.boss_group(GROUP)["deal_id"]
    store = bot.prm.store
    store.set_notice(deal_id, "failed:999", "聚水潭显示签收后问她一句的草稿没写成", bot.clock.now, job=999, lane="reply",
                     signed=True, asked_at=bot.clock.now)
    bot.prm.handle(gmsg(bot, "好的收到"))
    job = job_of(bot)
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0, ("prm",))["id"] == job["id"]
    bot.say(f"不接群 {GROUP_NAME}")
    if state == "superseded":
        store.update_deal(deal_id, bot.clock.now, mode="human", mode_until=bot.clock.now + 3600)
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="" if state == "no_reply" else "好的～",
                                slots={"收件人": "小美"}, stage_proposal="address", reason="kb/合作政策.md"))
    assert store.packet_for_job(job["id"])["status"] == state
    deal = store.deal(deal_id)
    assert any(n["key"] == "failed:999" for n in deal["notices"])
    assert not deal["slots"] and not deal["stage_proposal"]
    left = [n for n in deal["notices"] if n["key"] == f"group_left:{GROUP}"]
    assert [n["count"] for n in left] == ([] if state == "no_reply" else [1])   # (no reply needed: none to count)


# ── logs ─────────────────────────────────────────────────────────────────────

def test_the_logs_have_no_names_and_no_lines(bot, caplog):
    caplog.set_level(logging.DEBUG)
    bot.backend.say(GROUP, CREATOR, "我电话 13812345678", bot.clock.now - 60)
    linked(bot, member=1)
    bot.prm.handle(gmsg(bot, "地址是测试路 1 号", sender=BD))
    bot.say(f"不接群 {GROUP_NAME}")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "group 40000000…" in text
    for secret in ("小美", GROUP_NAME, GROUP, CREATOR, BD, "测试商务", "13812345678", "测试路"):
        assert secret not in text, secret


# ── 合并 draft-context (2026-11-10): a photo sheet never outlives the link of the group its job was for ──

def test_a_photo_sheet_of_a_group_job_goes_with_a_draft_the_ended_link_never_makes(clock, tmp_path):
    """PRM_DRAFT_CONTEXT: the Mac sends a sample order's photo sheet (her picture is on it) for the operators while it
    writes a group draft; the group's link ends meanwhile (「不接群」).  Whatever comes back makes no draft that waits:
    the job failed, its answer cannot be read, the Mac held a 专属链接 back for a bot it could not ask and nothing is
    left, or the draft came back and failed at once: the sheet is deleted then, as for any job that leaves no draft
    waiting (it never stays on the failed draft)."""
    import io
    from pathlib import Path
    from test_prm_draft_context import GATED, SHEET, sample_block

    b = setup(clock, tmp_path, prm_draft_context="on")
    try:
        def written_while_the_link_ends(text):
            linked(b, member=1)
            b.prm.handle(gmsg(b, text))
            job = job_of(b)
            b.clock.now += SETTLE_SEC + 1
            assert b.relay.next_job(0, ("prm",))["id"] == job["id"]          # the Mac is writing it
            row = b.relay.operator_file(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
            [kept] = b.relay.store.operator_files(job["id"])
            assert b.say(f"不接群 {GROUP_NAME}").startswith("不接了")
            b.clock.now += 5
            return job, row, Path(kept["path"])

        named = lambda job, row: [{"name": f"对图单-{job['id']}.jpg", "why": "并排对比", "sha256": row["sha256"]}]
        job, row, sheet = written_while_the_link_ends("就要这顶，寄吧")
        assert sheet.exists()
        b.finish(job["id"], draft(message_kind="money", reply_draft="寄样单建好啦", reason="kb", slots={},
                                  stage_proposal="", platform=sample_block("ready"), draft_context=GATED,
                                  operator_files=named(job, row)))
        packet = b.prm.store.packet_for_job(job["id"])
        assert packet["status"] == "failed" and packet["error"] == f"{GROUP_CLOSED_TEXT}，平台没有动"
        assert "operator_files" not in packet["meta"] and not sheet.exists()
        job, _, sheet = written_while_the_link_ends("在吗")
        b.finish(job["id"], ok=False, text="boom")
        assert not sheet.exists()
        job, _, sheet = written_while_the_link_ends("价格呢")
        b.finish(job["id"], text="这不是一份草稿")
        assert b.prm.store.packet_for_job(job["id"]) is None and not sheet.exists()
        job, row, sheet = written_while_the_link_ends("链接呢")
        b.finish(job["id"], draft(message_kind="faq", reply_draft="", reason="这台 Mac 连着几次都问不到 Windows 上的机器人",
                                  slots={}, stage_proposal="", platform_held="unasked", operator_files=named(job, row)))
        assert b.prm.store.packet_for_job(job["id"]) is None and not sheet.exists()
        assert b.texts == []
    finally:
        b.prm.stop()
        b.relay.stop()


def test_lines_of_the_group_her_waiting_draft_did_not_hear_are_told_to_the_admin_once(clock, tmp_path):
    """Review A-F3 (``meta.unheard``): her draft for a group linked by hand was told to the admin's chat, then others
    went on there past the redrafts allowed: her card counted the lines it did not hear, the admin's chat heard nothing
    and a 「1」 quoting the notice sent it.  It is told again (once: more lines later are the same news) and says so;
    an answer to the old notice does nothing, one to the fresh one acts."""
    b = setup(clock, tmp_path, prm_wechat_review="on")
    review = b.prm.wechat_review

    def told():
        return [t for chat, t in b.texts if chat == ADMIN and "草稿" in t.split("\n")[0]]

    def answer(words, quote):
        return review.answer({"message_id": f"q{next(_IDS)}", "chat_id": ADMIN, "sender_id": ADMIN, "content": words,
                              "msg_type": 49, "quoted_content": quote, "is_group": False, "timestamp": b.clock.now})

    try:
        linked(b, member=1)
        b.prm.handle(gmsg(b, "在吗"))
        drafted(b)
        assert chatter(b, 3) == REDRAFT_MAX
        [card] = waiting(b)
        assert "unheard" not in card
        review.sweep()
        [first] = [t for t in told() if t.startswith(f"#{card['id']} 草稿")]
        chatter(b, 1, start=3)
        assert waiting(b)[0]["unheard"] == 1
        assert answer("1", first.split("\n")[0])[1] == (f"#{card['id']} 这份草稿在通知以后有变化，看最新那条通知再回"
                                                       "（新的通知马上发过来）")
        review.sweep()
        fresh = [t for t in told() if t.startswith(f"#{card['id']} 草稿")][-1].split("\n")
        assert fresh[0] == f"#{card['id']} 草稿（第 2 次通知） · 小美 · 群里「{GROUP_NAME}」"
        assert fresh[1] == (f"上一条 #{card['id']} 的通知以后有变化：群里后来又有 1 句话这份草稿没听到。上一条作废了，"
                            "请看这一条再回")
        assert "（群里后来又有 1 句话这份草稿没听到：发之前请核对；要按群里的新话重写，到控制台点「重新起草」）" in fresh
        chatter(b, 2, start=4)
        assert waiting(b)[0]["unheard"] == 3
        review.sweep()
        assert len([t for t in told() if t.startswith(f"#{card['id']} 草稿")]) == 2     # (the same news: told once)
        assert answer("1", first.split("\n")[0])[1] == f"#{card['id']} 这份草稿在通知以后有变化，看最新那条通知再回"
        assert answer("1", fresh[0])[1] == f"#{card['id']} 已批准，正在发给 小美（群里「{GROUP_NAME}」）"
    finally:
        b.prm.stop()
        b.relay.stop()
