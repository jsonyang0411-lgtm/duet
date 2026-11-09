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
from src.prm.service import GROUP_UNSENT, LINK_TAIL, SETTLE_SEC, event_out
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
    assert answer == (f"接手了：群「{GROUP_NAME}」→ 小美（新登记的达人），合作单 #{deal['id']}；之后她在群里说的话会起草，"
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
    assert answer.startswith(f"接手了：群「{GROUP_NAME}」→ 小美美（已登记），合作单 #")
    again = bot.prm.store.party_by_wxid(CREATOR)
    assert (again["id"], again["name"], again["note"]) == (party["id"], "小美美", "达人名单里的")
    [ev] = bot.prm.store.events(bot.prm.store.deal_of(party["id"])["id"], kinds=(bg.EVENT,))
    assert ev["detail"] == {"name": GROUP_NAME, "new": False, "manual": True} and ev["actor"] == "admin"


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
    assert bot.say("不接") == f"好的，群「{GROUP_NAME}」不接了"
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
    # her private drafts hear the group too (三方群里的话), in order, WeChat's line not
    told = bot.prm._group_lines(deal["id"])
    assert [(x["who"], x["text"]) for x in told] == [("她", "你们家这款还有货吗"), ("我们", "有的，我发您看看"),
                                                    ("群里的人", "她这周想先看样"), ("她", "那寄样怎么弄呀"), ("她", "地址发你吗")]


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
    """A line she wrote a moment before the link is no late line: its draft follows the gates as any."""
    bot.backend.say(GROUP, CREATOR, "在吗", bot.clock.now - 20)
    linked(bot, member=1)
    assert "late" not in json.loads(job_of(bot)["meta"])


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
    b = setup(clock, tmp_path, prm_holding_min=10)
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
    assert bot.say("接群 1") == (f"群「品牌测试×小美」是 小美 的三方群（合作单 #{deal['id']}，「拉三方群」建的，现在只读）："
                                "接手以后她在群里说的话会起草，回复发在群里，群里的人都看得到。回「是 1」接手，「不接」作罢（30 分钟内有效）")
    answer = bot.say("是 1")
    assert answer.startswith(f"接手了：群「品牌测试×小美」→ 小美（已登记），合作单 #{deal['id']}")
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
    assert bot.say("不接群 别的群") == "没有接手叫「别的群」的群"
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
    assert bot.prm.deal_action("group_unlink", {"deal_id": deal_id, "chat": GROUP}) == {"ok": True}
    assert bot.prm.store.boss_group(GROUP)["state"] == "gone" and not bot.prm.is_party_group(GROUP)
    assert "linked_groups" not in bot.prm.snapshot()["parties"][0]["deal"]
    assert bot.prm.deal_action("group_unlink", {"deal_id": deal_id, "chat": GROUP})["ok"] is False
    from src.prm.service import apply_deal_action

    out = apply_deal_action(bot.prm.store, "group_unlink", {"deal_id": deal_id, "chat": GROUP}, now=bot.clock.now,
                            takeover_sec=7200)                        # (the stopped bot's console)
    assert out == {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能停掉接手的群"}


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
