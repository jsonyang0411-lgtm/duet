"""开场白没人处理就自动发 (PRM_OPENING_AUTO_MIN, 2026-11-02) and 同一张图不重复发 (the same day).

The user: a new friend who added us on a Saturday at 17:29, after the operators had gone home, heard nothing from us
until Monday.  Our first line to a new friend (a 开场白, or the first draft of a new friend's messages from before she
was registered) now goes out by itself when it has waited PRM_OPENING_AUTO_MIN minutes in 待审批 with nobody deciding
on it (at once in the 下班时段 or 自动模式), if it passes the night checks and says nothing of samples (kb 第 0.5 节).
0, the default: as before, it waits for a person.  And the 开场白's 3 pictures went again with her 货盘 an hour later:
a file she got from us within 7 days is left out of a new draft, which says so.  Names and numbers here are made up.
"""

import importlib.util
import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from test_prm import CREATOR, Bot, Clock, config, draft
from test_prm_lifecycle import own

from src.config import BotConfig, _validate_config
from src.prm import service as prm_service
from src.prm.result import parse_draft
from src.prm.service import (OPENING_AUTO_GRACE_SEC, OPENING_ROUTE, REPEAT_FILE_SEC, PrmService, ack_only,
                             snapshot)
from src.prm.store import PrmStore

ROOT = Path(__file__).resolve().parents[1]
MINUTES = 30
HOURS = "18:00-09:00"
OPENING = "你好呀～我是 ACE7CN 的运营，看你的穿搭风格特别适合我们家的帽子，先发几款当季现货给你看看～有兴趣了解一下吗？"
PICTURES = (("dy-3000000000000000101.jpg", b"beret"), ("dy-3000000000000000102.jpg", b"pillbox"),
            ("dy-3000000000000000103.jpg", b"equestrian"))


def at(hour, minute=0, day=2):
    """A time of day on this machine's clock (a Monday, 2026-11-02)."""
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


def make(clock, tmp_path, **prm):
    b = Bot(clock, tmp_path, **prm)
    b.prm.register(CREATOR, "小美", "测试达人")
    return b


@pytest.fixture
def clock():
    return Clock(at(14, 0))


@pytest.fixture
def bot(clock, tmp_path):
    b = make(clock, tmp_path, prm_opening_auto_min=MINUTES)
    yield b
    b.prm.stop()
    b.relay.stop()


def first_line(text=OPENING, files=PICTURES, **more):
    """The Mac's 开场白: a greeting with 3 pictures."""
    return draft(message_kind="greeting", reply_draft=text, slots={}, stage_proposal="", intent="开场白",
                 reason="附图：3.1、3.2、3.7", attachments_to_send=[f"assets/products/{name}" for name, _ in files],
                 **more)


def deal_id(b):
    return b.prm.store.deal_of(b.prm.store.party_by_wxid(CREATOR)["id"])["id"]


def drafted(b, job, d, files=()):
    for name, body in files:
        assert b.relay.outfile(job["id"], name, io.BytesIO(body).read, len(body))
    b.finish(job["id"], d)
    return b.prm.store.packet_for_job(job["id"])


def opening(b, d=None, files=PICTURES):
    """The console's 「写开场白」 for her, and the Mac's draft of it: (job, packet)."""
    assert b.prm.ask_opening(deal_id(b))["ok"]
    job = b.worker_takes()
    return job, drafted(b, job, d or first_line(files=files), files)


def wait(b, seconds):
    """Time passes; the sender thread's two steps run (``tick``'s order)."""
    b.clock.now += seconds
    went = b.prm.send_due_openings()
    b.prm.send_approved()
    return went


def view(b, packet_id):
    return next(p for p in [*snapshot(b.prm.store, running=True)["pending"], *snapshot(b.prm.store, running=True)["recent"]]
                if p["id"] == packet_id)


# ── the setting ────────────────────────────────────────────────────────────

def test_the_setting_is_off_by_default_and_checked():
    assert BotConfig().prm_opening_auto_min == 0
    _validate_config({"prm_opening_auto_min": 0})
    _validate_config({"prm_opening_auto_min": 1440})
    for value in (-1, 1441):
        with pytest.raises(RuntimeError) as e:
            _validate_config({"prm_opening_auto_min": value})
        assert "PRM_OPENING_AUTO_MIN" in str(e.value)


def test_off_an_opening_waits_for_a_person_as_before(clock, tmp_path):
    """0 (or a config object without it): the job is told nothing, the draft is not marked, hours later it still waits."""
    for name, prm in (("zero", {"prm_opening_auto_min": 0}), ("none", {})):
        b = make(clock, tmp_path / name, **prm)
        job, packet = opening(b)
        assert "opening_auto" not in job["meta"]
        assert packet["status"] == "pending" and "auto_at" not in packet["meta"] and "auto_held" not in packet["meta"]
        assert wait(b, 5 * 3600) == 0
        assert b.prm.store.packet(packet["id"])["status"] == "pending" and b.texts == [] and b.files == []
        shown = view(b, packet["id"])
        assert shown["auto_at"] is None and shown["auto_held"] == ""
        assert b.prm.snapshot()["opening_auto_min"] == 0
        b.prm.stop()
        b.relay.stop()


# ── by day: it waits its minutes, then goes as it is ───────────────────────

def test_an_opening_nobody_decides_on_goes_out_by_itself_after_its_minutes(bot, clock):
    job, packet = opening(bot)
    assert job["meta"]["opening_auto"] == MINUTES, "the Mac hears its draft may go out by itself"
    made = packet["created_at"]
    assert packet["status"] == "pending" and packet["route"] == "review"
    assert packet["meta"]["auto_at"] == made + MINUTES * 60 and "auto_held" not in packet["meta"]
    assert view(bot, packet["id"])["auto_at"] == made + MINUTES * 60
    assert bot.prm.snapshot()["opening_auto_min"] == MINUTES
    assert wait(bot, MINUTES * 60 - 61) == 0                         # a minute before: it waits
    assert bot.prm.store.packet(packet["id"])["status"] == "pending" and bot.texts == []
    clock.now = made + MINUTES * 60
    bot.prm.tick()                                                  # the sender thread: approved, then sent
    sent = bot.prm.store.packet(packet["id"])
    assert (sent["status"], sent["route"], sent["decided_at"]) == ("sent", OPENING_ROUTE, made + MINUTES * 60)
    assert bot.texts == [(CREATOR, OPENING)] and [p.rsplit("/", 1)[-1] for _, p in bot.files] == [n for n, _ in PICTURES]
    [ev] = [e for e in bot.prm.store.events(deal_id(bot), limit=20) if e["kind"] == "opening_auto"]
    assert ev["detail"] == {"packet": packet["id"], "waited_min": MINUTES} and ev["actor"] == "system"
    shown = view(bot, packet["id"])
    assert shown["route"] == OPENING_ROUTE and shown["status"] == "sent"
    assert wait(bot, 3600) == 0 and len(bot.texts) == 1, "never twice"


def test_a_person_who_decides_first_decides(bot):
    _, sent = opening(bot)
    assert bot.prm.decide(sent["id"], "edit", "您好呀～我是 ACE7CN 的运营～")["ok"]
    bot.prm.send_approved()
    assert wait(bot, MINUTES * 60 + 60) == 0
    assert bot.texts == [(CREATOR, "您好呀～我是 ACE7CN 的运营～")]
    assert bot.prm.store.packet(sent["id"])["route"] == "review", "the operator's send, not the timer's"
    bot.texts.clear()
    # one dropped stays dropped
    bot.clock.now += 3600
    _, dropped = opening(bot)
    assert bot.prm.decide(dropped["id"], "drop")["ok"]
    assert wait(bot, MINUTES * 60 + 60) == 0
    assert bot.prm.store.packet(dropped["id"])["status"] == "dropped" and bot.texts == []


def test_her_message_before_it_goes_puts_it_out_of_date(bot):
    _, packet = opening(bot)
    bot.clock.now += 600
    bot.prm.handle(bot.msg("你好呀，看到你们的邀约了"))
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    assert wait(bot, MINUTES * 60) == 0 and bot.texts == []
    # her message's own job is coming: its draft is a reply, not a first line, nothing marks it
    job = bot.worker_takes()
    reply = drafted(bot, job, draft(message_kind="greeting", reply_draft="你好呀～", slots={}, stage_proposal=""))
    assert "opening_auto" not in job["meta"] and "auto_at" not in reply["meta"] and "auto_held" not in reply["meta"]


def test_a_draft_of_hers_on_its_way_holds_it(bot):
    """She wrote and her draft job is not done yet (the waiting draft is out of date already, but were it not)."""
    _, packet = opening(bot)
    bot.clock.now += MINUTES * 60 + 60
    bot.relay.submit(CREATOR, CREATOR, "[11-02 14:31] 在吗", kind="prm", meta={"party_id": 1, "deal_id": deal_id(bot)},
                     fresh=True)
    assert bot.prm.send_due_openings() == 0
    assert bot.prm.store.packet(packet["id"])["status"] == "pending"


def test_while_a_person_has_the_chat_it_waits(bot):
    _, packet = opening(bot)
    bot.clock.now += 60
    bot.prm.on_own_message(own(bot, "你好～我是运营小林"))                  # an operator greets her by hand
    assert bot.prm.store.deal(deal_id(bot))["mode"] == "human"
    assert wait(bot, MINUTES * 60) == 0
    assert bot.texts == [] and bot.prm.store.packet(packet["id"])["status"] in ("pending", "superseded")


@pytest.mark.parametrize("change, why", [
    ({"needs_attention": True, "reason": "不知道她的风格"}, "模型要求运营注意"),
    ({"reply_draft": "你好呀～可以免费寄一顶给你试试～"}, "说到了「免费」"),
    ({"reply_draft": "你好呀～我们家帽子都包邮哦～"}, "说到了「包邮」"),
    ({"reply_draft": "你好呀～想送你一顶试戴～"}, "说到了「送你」"),
    ({"reply_draft": "你好呀～看中的款可以寄几顶给您～"}, "说到了「寄几」"),
    ({"reply_draft": "你好呀～样品邮费我们出～"}, "说到了「邮费」"),
    ({"reply_draft": "你好呀～合作佣金可以给到 30%～"}, "30%"),
    ({"reply_draft": "你好呀～我们没有坑位费～"}, "坑位"),
    ({"message_kind": "complaint"}, "投诉一律等运营"),
    ({"reply_draft": "你好呀，〈手机9〉是我的电话"}, "占位符"),
])
def test_what_does_not_pass_waits_and_says_why(bot, change, why):
    _, packet = opening(bot, {**first_line(), **change})
    assert packet["status"] == "pending" and "auto_at" not in packet["meta"]
    assert why in packet["meta"]["auto_held"]
    assert why in view(bot, packet["id"])["auto_held"]
    assert wait(bot, 3 * 3600) == 0 and bot.texts == []


def test_files_short_waits(bot):
    job = None
    assert bot.prm.ask_opening(deal_id(bot))["ok"]
    job = bot.worker_takes()
    packet = drafted(bot, job, first_line(), PICTURES[:2])                     # one picture did not come
    assert packet["meta"]["auto_held"].startswith("草稿要附 3 个文件，只收到 2 个")
    assert wait(bot, 3 * 3600) == 0 and bot.texts == []


def test_her_recall_after_it_is_marked_holds_it(bot):
    _, packet = opening(bot)
    bot.prm.store.hold_for_review(deal_id(bot), "她撤回了一条消息")           # (as her recall does)
    held = bot.prm.store.packet(packet["id"])
    assert held["needs_attention"] and held["meta"]["auto_at"]
    assert wait(bot, MINUTES * 60 + 60) == 0 and bot.texts == []


# ── at night, in 自动模式, and after a start ────────────────────────────────

def test_in_the_night_window_it_goes_at_once(clock, tmp_path):
    clock.now = at(21, 30)
    b = make(clock, tmp_path, prm_opening_auto_min=MINUTES, prm_night_auto_hours=HOURS)
    _, packet = opening(b)
    assert packet["meta"]["auto_at"] == packet["created_at"]
    assert wait(b, 0) == 0, f"not in the first {OPENING_AUTO_GRACE_SEC} s of a start"
    assert wait(b, OPENING_AUTO_GRACE_SEC) == 1
    assert b.texts == [(CREATOR, OPENING)] and b.prm.store.packet(packet["id"])["route"] == OPENING_ROUTE
    b.prm.stop()
    b.relay.stop()


def test_in_auto_mode_by_day_it_goes_at_once(clock, tmp_path):
    b = make(clock, tmp_path, prm_opening_auto_min=MINUTES)
    assert b.prm.set_auto_mode(True)["auto_mode"]["on"] is True
    clock.now += OPENING_AUTO_GRACE_SEC
    _, packet = opening(b)
    assert packet["meta"]["auto_at"] == packet["created_at"]
    assert wait(b, 1) == 1 and b.texts == [(CREATOR, OPENING)]
    b.prm.stop()
    b.relay.stop()


def test_a_restart_keeps_the_time_and_the_setting_turned_off_keeps_it_for_a_person(clock, tmp_path):
    b = make(clock, tmp_path, prm_opening_auto_min=MINUTES)
    _, packet = opening(b)
    store = b.prm.store
    b.prm.stop()
    clock.now += MINUTES * 60 + 60
    # started again with the setting 0: it waits for a person
    off = PrmService(config(prm_opening_auto_min=0), b.relay, store=store, clock=clock, send=b._send,
                     send_file=b._send_file)
    assert off.send_due_openings() == 0 and store.packet(packet["id"])["status"] == "pending"
    off.stop()
    # started again with it on: after the first minutes it goes
    again = PrmService(config(prm_opening_auto_min=MINUTES), b.relay, store=store, clock=clock, send=b._send,
                       send_file=b._send_file)
    assert again.send_due_openings() == 0, "the catch-up after a start comes first"
    clock.now += OPENING_AUTO_GRACE_SEC
    assert again.send_due_openings() == 1
    again.send_approved()
    assert b.texts == [(CREATOR, OPENING)]
    again.stop()
    b.relay.stop()


# ── a new friend's first draft (she wrote when she added us) ──────────────

def test_a_new_friends_first_draft_goes_by_itself_and_gets_no_holding_reply(bot, clock):
    """The case of 2026-10-31: she added us with 「我是…」, her draft waited all weekend; a 稍等 went out at night."""
    added = clock.now
    bot.prm.take_new_friend(CREATOR, [bot.msg("我是测试小美的助理", ts=added)], verify_until=added, how="manual")
    job = bot.worker_takes()
    assert job["meta"]["new_friend"]["count"] == 1 and job["meta"]["opening_auto"] == MINUTES
    packet = drafted(bot, job, first_line(text="你好呀～谢谢加我，我是 ACE7CN 的运营～"), PICTURES)
    assert packet["meta"]["auto_at"] == packet["created_at"] + MINUTES * 60
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 0, "it goes by itself soon: no 稍等 first"
    assert wait(bot, MINUTES * 60) == 1
    assert bot.texts == [(CREATOR, "你好呀～谢谢加我，我是 ACE7CN 的运营～")]


def test_a_reply_or_a_draft_read_late_is_not_a_first_line(bot):
    """Not in play: a reply to her message, a 老板拉的群's 开场白 (the boss reads it), anything read late or missed."""
    d = parse_draft(json.dumps(first_line()))
    judge = lambda meta: bot.prm._opening_auto(meta, d, refused="", short="", unknown=[], waybill="", attachments=[],
                                               named=0, mode="copilot", recall="", lane="reply", now=1.0, auto_on=False)
    assert judge({}) is None
    assert judge({"opening": {"how": "", "by": "console"}, "channel": "group"}) is None
    assert judge({"new_friend": {"from": 1, "count": 1}, "late": {"from": 1}}) is None
    assert judge({"new_friend": {"from": 1, "count": 1}, "missed": {"from": 1, "count": 1}}) is None
    assert judge({"opening": {"how": "", "by": "console"}}) == (1.0 + MINUTES * 60, "")
    assert bot.prm._opening_auto({"opening": {}}, d, refused="", short="", unknown=[], waybill="", attachments=[],
                                 named=0, mode="copilot", recall="", lane="system", now=1.0, auto_on=False) is None


# ── 同一张图不重复发 ───────────────────────────────────────────────────────

def test_a_picture_she_got_lately_is_left_out_of_the_next_draft(bot, clock):
    _, first = opening(bot)
    assert bot.prm.decide(first["id"], "send")["ok"]
    bot.prm.send_approved()
    assert len(bot.files) == 3
    # an hour later she asks for the 货盘; the Mac attaches the same 3 pictures and one new one
    clock.now += 3600
    bot.prm.handle(bot.msg("辛苦发一下货盘"))
    job = bot.worker_takes()
    assert job["meta"]["sent"][-1]["files"] == [n for n, _ in PICTURES], "the Mac hears which pictures she got"
    new = ("dy-3000000000000000104.jpg", b"bucket")
    names = [n for n, _ in (*PICTURES, new)]
    packet = drafted(bot, job, draft(message_kind="faq", reply_draft="好的～这是我们家当季现货的货盘：…", slots={},
                                     stage_proposal="", reason="kb/合作政策.md 第 3 节",
                                     attachments_to_send=[f"assets/products/{n}" for n in names]), (*PICTURES, new))
    assert [a["name"] for a in packet["attachments"]] == [new[0]]
    assert f"{'、'.join(n for n, _ in PICTURES)} 她" in packet["reason"] and "这次没再发（同一张图 7 天内不重复发）" in packet["reason"]
    assert "只收到" not in packet["reason"] and not packet["needs_attention"]
    assert bot.prm.decide(packet["id"], "send")["ok"]
    bot.files.clear()
    bot.prm.send_approved()
    assert [p.rsplit("/", 1)[-1] for _, p in bot.files] == [new[0]]


def test_the_same_name_with_other_bytes_or_a_week_later_goes(bot, clock):
    _, first = opening(bot)
    assert bot.prm.decide(first["id"], "send")["ok"]
    bot.prm.send_approved()
    clock.now += 3600
    bot.prm.handle(bot.msg("还有别的颜色吗"))
    job = bot.worker_takes()
    other = ((PICTURES[0][0], b"another beret picture"),)
    packet = drafted(bot, job, draft(message_kind="faq", reply_draft="有的～", slots={}, stage_proposal="",
                                     reason="kb/合作政策.md 第 3 节",
                                     attachments_to_send=[f"assets/products/{PICTURES[0][0]}"]), other)
    assert [a["name"] for a in packet["attachments"]] == [PICTURES[0][0]] and "没再发" not in packet["reason"]
    assert bot.prm.decide(packet["id"], "drop")["ok"]
    clock.now += REPEAT_FILE_SEC + 60
    bot.prm.handle(bot.msg("图再发我一下"))
    job = bot.worker_takes()
    packet = drafted(bot, job, draft(message_kind="faq", reply_draft="好的～", slots={}, stage_proposal="",
                                     reason="kb/合作政策.md 第 3 节",
                                     attachments_to_send=[f"assets/products/{n}" for n, _ in PICTURES]), PICTURES)
    assert len(packet["attachments"]) == 3 and "没再发" not in packet["reason"]


def test_a_draft_of_only_pictures_she_has_sends_nothing(bot, clock):
    _, first = opening(bot)
    assert bot.prm.decide(first["id"], "send")["ok"]
    bot.prm.send_approved()
    clock.now += 600
    bot.prm.handle(bot.msg("图呢"))
    job = bot.worker_takes()
    packet = drafted(bot, job, draft(message_kind="faq", reply_draft="", slots={}, stage_proposal="",
                                     reason="kb/合作政策.md 第 3 节",
                                     attachments_to_send=[f"assets/products/{n}" for n, _ in PICTURES]), PICTURES)
    assert packet["status"] == "no_reply" and packet["attachments"] == []


# ── 她只回「好滴」这类: no holding reply ──────────────────────────────────────

@pytest.mark.parametrize("words, holds", [
    (["好滴"], False), (["好的，我先看看哈"], False), (["[表情]"], False), (["👌"], False), (["嗯嗯好的谢谢"], False),
    (["收到啦！"], False), (["好的 我考虑一下"], False),
    (["在吗"], True), (["好滴，哪款适合我呀"], True), (["好滴", "辛苦发一下货盘"], True),
])
def test_a_round_that_only_says_she_got_it_gets_no_holding_reply(bot, clock, words, holds):
    """2026-11-02: after our 货盘 she said 「好滴」; its draft waited and 10 minutes later 「收到，我确认一下马上回你」 went
    out.  A round of nothing but 好滴 / 收到 / 谢谢 / a sticker has nothing to hold; anything more gets it as before."""
    for text in words:
        bot.prm.handle(bot.msg(text))
        clock.now += 1
    job = bot.worker_takes()
    drafted(bot, job, draft(message_kind="other", reply_draft="好的～您慢慢看", slots={}, stage_proposal="",
                            needs_attention=True, reason="不确定要不要回"))
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == (1 if holds else 0)
    assert bot.texts == ([(CREATOR, "收到，我确认一下马上回您")] if holds else [])


def test_which_lines_only_say_she_got_it():
    for text in ("[11-02 15:29] 好滴", "[11-02 15:29] 好滴～", "[11-02 15:29] OK", "[11-02 15:29] 谢谢啦[抱拳]",
                 "[11-02 15:29] 好哒\n[11-02 15:30] [表情]", "[11-02 15:29] 哈哈"):
        assert ack_only(text), text
    for text in ("", "[11-02 15:29] [图片]", "[11-02 15:29] 好滴？多少钱", "[11-02 15:29] 你好", "[11-02 15:29] [文件] brief.pdf",
                 "[11-02 15:29] 可以寄样吗"):
        assert not ack_only(text), text


# ── the Mac's prompt ───────────────────────────────────────────────────────

def test_a_first_line_may_say_samples_can_be_arranged_and_still_goes_by_itself(bot):
    """2026-11-10, the user: an opening may say 「看中的款可以安排寄样给您试戴」; 寄样 / 样品 / 试戴 no longer hold it (a
    promise still does: free, postage, a gift, how many, the cases above)."""
    text = "老师您好～我是 ACE7CN 的运营，先把我们秋冬当季的货盘发您看看，看中哪几款可以安排寄样给您试戴～"
    _, packet = opening(bot, first_line(text))
    assert packet["status"] == "pending" and packet["meta"].get("auto_at") and "auto_held" not in packet["meta"]
    assert wait(bot, MINUTES * 60 + 60) == 1
    assert bot.texts and bot.texts[0][1] == text
    for word in ("寄样", "样品", "试戴"):
        assert word not in prm_service.OPENING_AUTO_WORDS


@pytest.fixture
def worker():
    spec = importlib.util.spec_from_file_location("claude_relay_worker_opening_auto", ROOT / "tools/claude_relay/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mac_hears_its_first_line_may_go_by_itself_and_which_pictures_she_got(worker):
    line = worker.opening_auto_line({"opening_auto": 30})
    assert line.startswith("这是我们给新好友的第一句话：运营 30 分钟内没处理，机器人就把你写的这份直接发给她")
    assert "寄样只说到「看中的款可以安排寄样」" in line and "不报寄几顶" in line and "needs_attention" in line
    for odd in (None, 0, 1441, "30", True, 1.5):
        assert worker.opening_auto_line({"opening_auto": odd}) == ""
    job = {"id": 3, "text": "", "meta": {"opening": {"how": "requested", "by": "new_friend"}, "opening_auto": 30}}
    prompt = worker.prm_prompt(job, 1, "小美", "deals/1.md", [], []).split("\n")
    assert prompt.index(line) == prompt.index(worker.opening_line(job["meta"])) + 1
    plain = {"id": 4, "text": "", "meta": {"opening": {"how": "requested", "by": "new_friend"}}}
    assert "第一句话：运营" not in worker.prm_prompt(plain, 1, "小美", "deals/1.md", [], [])
    block = worker.sent_block([{"at": "11-02 14:38", "kind": "greeting", "what": "sent", "text": "你好呀～",
                                "files": ["dy-1.jpg", "dy-2.jpg"]},
                               {"at": "11-02 15:27", "kind": "faq", "what": "sent", "text": "货盘～"}])
    assert "这一条还附了 2 个文件：dy-1.jpg、dy-2.jpg（她已经收到了，同样的图别再附）" in block
    assert block.count("这一条还附了") == 1
    for odd in ("dy-1.jpg", [1, None], {"a": 1}):
        assert "这一条还附了" not in worker.sent_block([{"at": "", "what": "sent", "text": "x", "files": odd}])


def test_the_lines_work_on_the_macs_own_python(tmp_path):
    """The worker stays standard library and Python 3.9 (macOS's /usr/bin/python3)."""
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(ROOT / 'tools/claude_relay/worker.py')!r})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        "job = {'id': 1, 'text': '', 'meta': {'opening': {'how': 'requested', 'by': 'new_friend'}, 'opening_auto': 30,\n"
        "       'sent': [{'at': '11-02 14:38', 'what': 'sent', 'text': 'x', 'files': ['dy-1.jpg']}]}}\n"
        "prompt = w.prm_prompt(job, 1, 'x', 'deals/1.md', [], [])\n"
        f"assert {worker_line()!a} in prompt and {'这一条还附了 1 个文件：dy-1.jpg'!a} in prompt\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")


def worker_line():
    return "运营 30 分钟内没处理，机器人就把你写的这份直接发给她"
