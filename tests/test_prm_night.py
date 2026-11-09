"""下班时段自动回复（PRM_NIGHT_AUTO_HOURS，2026-10-29）and 自动模式 (the console's switch, 2026-10-30).

The operators go home at 18:00; evenings are when creators answer most.  Inside the window (this PC's local time)
a creator-reply draft whose kind has not earned ``auto`` still goes out by itself, but only when every safety rule
holds (src/prm/gates.py ``night_reason``, src/prm/service.py ``_night_judge``); anything in doubt waits as by day.
Empty (the default) the bot is what it was.  自动模式 puts the same rules in force at any hour, until it is switched
off (at the end of this file).  The names and numbers here are made up.
"""

import json
import logging
import time

import pytest

from test_prm import CREATOR, Bot, Clock, a_picture, config, draft, pending_packet
from test_prm_platform_flow import DESIGNATED, block

from src.config import BotConfig, _validate_config
from src.prm import gates, platforms, service
from src.prm.service import AUTO_HOLDING_TEXT, HOLDING_TEXT, NIGHT_HOLDING_TEXT, SETTLE_SEC, PrmService, event_out
from src.prm.store import BUYIN_TOOL
from src.relay.service import ClaudeRelay
from src.relay.store import PRM, RelayStore

HOURS = "18:00-09:00"
# kb 合作政策 第 0.5 节「下班时段」的那句（寄样阶段的过渡话）
SAMPLE_LINE = ("好呀～您的意向我都记下啦！现在时间稍晚怕打扰您，明早我第一时间拉个小群，把我们品牌负责人也请进来，"
               "一起跟您敲定选款和寄样细节哈！")
SAMPLE_REASON = "已进入寄样阶段，请运营拉老板与达人建三方群"
HOLD_LINE = "好的～这个我记下啦，明早跟负责人确认后第一时间回您哈！"       # kb 第 0.6 节的例子


def at(hour, minute=0, day=29):
    """A time of day on this machine's clock (the window goes by local time)."""
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


def make_bot(clock, tmp_path, **prm):
    b = Bot(clock, tmp_path, **prm)
    b.prm.register(CREATOR, "小美", "测试达人")
    return b


@pytest.fixture
def clock():
    return Clock(at(21, 30))


@pytest.fixture
def bot(clock, tmp_path):
    b = make_bot(clock, tmp_path, prm_night_auto_hours=HOURS)
    yield b
    b.prm.stop()
    b.relay.stop()


def reply(kind="other", text="好的呀～", **more):
    """A draft that says only ``text`` (no placeholder, no slots)."""
    return draft(message_kind=kind, reply_draft=text, slots={}, **more)


def drafted(b, d=None, text="佣金能给多少呀"):
    """The creator writes, the Mac drafts: the packet."""
    b.prm.handle(b.msg(text))
    job = b.worker_takes()
    b.finish(job["id"], d or reply())
    return b.prm.store.packet_for_job(job["id"])


def state(packet):
    return packet["status"], packet["route"]


GOES = ("approved", "night")
WAITS = ("pending", "review")


# ── the window ────────────────────────────────────────────────────────────

def test_the_window_is_hours_of_this_pcs_day_and_may_pass_midnight():
    window = gates.night_window("18:00-09:00")
    assert window == (18 * 60, 9 * 60)
    inside = {hm: gates.in_night(window, at(*hm)) for hm in ((17, 59), (18, 0), (23, 59), (0, 0), (8, 59), (9, 0),
                                                              (12, 0))}
    assert inside == {(17, 59): False, (18, 0): True, (23, 59): True, (0, 0): True, (8, 59): True, (9, 0): False,
                      (12, 0): False}
    noon = gates.night_window("12:00-13:30")                                  # not past midnight
    assert [gates.in_night(noon, at(*hm)) for hm in ((11, 59), (12, 0), (13, 29), (13, 30))] == [False, True, True, False]
    assert gates.night_window("18:00-24:00") == (1080, 1440) and gates.in_night((1080, 1440), at(23, 59))
    assert gates.night_window("１８：００－０９：００") == window == gates.night_window(" 18:00 ~ 09:00 ")
    assert gates.night_window("") is None and gates.night_window("  ") is None and gates.night_window(None) is None
    assert gates.night_window_text(window) == "18:00–09:00" and gates.night_window_text(window, "-") == HOURS


@pytest.mark.parametrize("value", ["18-9", "18:00", "18:00-09", "25:00-09:00", "18:60-09:00", "24:00-09:00",
                                   "18:00-24:30", "18:00-18:00", "00:00-24:00", "下班", "18:00-09:00-10:00"])
def test_a_window_that_is_none_is_refused(value):
    with pytest.raises(ValueError):
        gates.night_window(value)
    with pytest.raises(RuntimeError) as e:
        _validate_config({"prm_night_auto_hours": value})
    assert "PRM_NIGHT_AUTO_HOURS" in str(e.value)


def test_the_settings_default_to_off_and_the_night_holding_text_is_checked_like_the_days():
    config = BotConfig()
    assert config.prm_night_auto_hours == "" and config.prm_night_holding_text == NIGHT_HOLDING_TEXT
    _validate_config({"prm_night_auto_hours": "", "prm_night_holding_text": NIGHT_HOLDING_TEXT})
    _validate_config({"prm_night_auto_hours": "22:30-07:00", "prm_night_holding_text": "好"})
    for text in ("", "  ", "字" * 61):
        with pytest.raises(RuntimeError) as e:
            _validate_config({"prm_night_holding_text": text})
        assert "PRM_NIGHT_HOLDING_TEXT" in str(e.value)


@pytest.mark.parametrize("hm, goes", [((18, 0), True), ((8, 59), True), ((9, 0), False), ((17, 59), False)])
def test_the_edges_of_the_window(clock, tmp_path, hm, goes):
    clock.now = at(*hm)
    b = make_bot(clock, tmp_path, prm_night_auto_hours=HOURS)
    packet = drafted(b, reply("greeting", "好的呀～很高兴认识你"), text="你好")       # made and finished in that minute
    assert state(packet) == (GOES if goes else WAITS)
    b.relay.stop()


# ── off: the bot as it was ────────────────────────────────────────────────

def test_with_the_setting_empty_nothing_changes(clock, tmp_path):
    """Empty (the default) or a config object without it: at 21:30 a draft waits for a person, its job is told
    nothing, the holding reply is the day's, the console says 关着; the same as at 14:00."""
    def run(name, when, **prm):
        clock.now = when
        b = make_bot(clock, tmp_path / name, **prm)
        b.prm.handle(b.msg("佣金能给多少呀"))
        job = b.worker_takes()
        b.finish(job["id"], reply("money", "我们达人合作的佣金是 20%～"))
        packet = b.prm.store.packet_for_job(job["id"])
        clock.now += 11 * 60
        b.prm.send_holding_replies()
        seen = (job["meta"], {k: packet[k] for k in ("status", "route", "meta", "needs_attention", "reason")},
                b.texts, b.prm.store.deal(1)["attention"], [e["kind"] for e in b.prm.store.events(1)],
                b.prm.snapshot()["night"], b.prm.snapshot()["night_alerts"])
        b.relay.stop()
        return seen

    empty = run("empty", at(21, 30), prm_night_auto_hours="")
    assert run("without", at(21, 30)) == empty
    assert run("day", at(14, 0, day=30), prm_night_auto_hours="")[:5] == empty[:5]
    meta, packet, texts, attention, events, status, alerts = empty
    assert "night" not in meta and packet == {"status": "pending", "route": "review", "meta": {},
                                              "needs_attention": False, "reason": "收件信息齐了"}
    assert texts == [(CREATOR, HOLDING_TEXT)] and events == ["holding_reply"] and attention == "" and alerts == []
    assert status == {"on": False, "window": "", "inside": False, "suspended": "", "error": "", "auto": False}


# ── rule 1: inside the window, by this PC's clock ─────────────────────────

def test_a_plain_draft_goes_out_at_night_and_waits_by_day(bot, clock):
    packet = drafted(bot, reply("greeting", "好的呀～很高兴认识你"), text="你好")
    assert state(packet) == GOES and packet["decided_at"] == clock.now and not packet["needs_attention"]
    assert bot.prm.store.gate("greeting")["route"] == "review"              # its kind has not earned auto
    assert bot.prm.send_approved() == 1 and bot.texts == [(CREATOR, "好的呀～很高兴认识你")]
    [ev] = bot.prm.store.events(1)
    assert ev["kind"] == "night_send" and event_out(ev)["name"] == f"夜间自动发出 #{packet['id']}"
    assert bot.prm.store.deal(1)["attention"] == ""                         # a plain one leaves no notice
    clock.now = at(14, 0, day=30)                                           # the next day at 14:00
    day = drafted(bot, reply("greeting", "好的呀～"), text="在吗")
    assert state(day) == WAITS and day["meta"] == {}                        # the gate decides, nothing is said
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, "好的呀～很高兴认识你")]


def test_the_job_is_told_it_is_night(bot, clock):
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    assert job["meta"]["night"] == {"window": HOURS, "at": "21:30"}
    bot.finish(job["id"], reply())
    clock.now = at(10, 0, day=30)
    bot.prm.handle(bot.msg("早上好"))
    assert "night" not in bot.worker_takes()["meta"]                        # by day: nothing


def test_only_drafts_made_and_finished_inside_the_window_are_judged(clock, tmp_path):
    """A draft waiting when the window starts stays waiting; a job made before 18:00 whose draft comes at 18:00
    waits (the Mac did not write by the night rules), and says why; one finished after 09:00 is the day's."""
    other = "wxid_creator02"
    clock.now = at(17, 50)
    b = make_bot(clock, tmp_path, prm_night_auto_hours=HOURS)
    b.prm.register(other, "小兰", "测试达人")
    waiting = drafted(b, reply(text="好的呀～"), text="你好")
    assert state(waiting) == WAITS
    clock.now = at(17, 59) + 45
    b.prm.handle(b.msg("在吗", chat=other))                                   # a job made at 17:59
    job = b.worker_takes()
    assert "night" not in job["meta"]
    clock.now = at(18, 0) + 5
    b.finish(job["id"], reply(text="在的呀～"))
    late = b.prm.store.packet_for_job(job["id"])
    assert state(late) == WAITS and "任务建的时候没算作下班时段（还没到点" in late["meta"]["night_held"]
    clock.now = at(18, 30)
    b.prm.tick()                                                             # the sender thread's round
    assert state(b.prm.store.packet(waiting["id"])) == WAITS and state(b.prm.store.packet(late["id"])) == WAITS
    assert b.texts == [(CREATOR, NIGHT_HOLDING_TEXT), (other, NIGHT_HOLDING_TEXT)]   # only the holding replies
    clock.now = at(8, 59, day=30) + 50
    b.prm.handle(b.msg("早", chat=other))
    morning = b.worker_takes()
    assert morning["meta"]["night"]["at"] == "08:59"
    clock.now = at(9, 0, day=30) + 2
    b.finish(morning["id"], reply(text="早呀～"))
    by_day = b.prm.store.packet_for_job(morning["id"])
    assert state(by_day) == WAITS and by_day["meta"] == {}
    b.relay.stop()


def own(b, text, ts):
    """This WeChat account's own line read back (written at ``ts`` by WeChat's clock)."""
    return {"message_id": f"own{time.time_ns()}", "chat_id": CREATOR, "sender_id": "wxid_bot", "sender_name": "我",
            "content": text, "msg_type": 1, "is_group": False, "timestamp": ts}


def test_while_this_pcs_clock_is_off_wechats_nothing_goes_out_by_itself_at_night(bot, clock):
    first = drafted(bot, reply(text="好的呀～"), text="你好")
    assert state(first) == GOES and bot.prm.send_approved() == 1
    bot.prm.on_own_message(own(bot, "好的呀～", clock.now - 3 * 3600))          # read back: this PC is 3 hours fast
    assert "比微信快 3 小时" in bot.prm.store.deal(1)["attention"]
    status = bot.prm.snapshot()["night"]
    assert status["on"] and status["inside"] and "快 3 小时" in status["suspended"] and "不自动发" in status["suspended"]
    bot.prm.handle(bot.msg("在吗"))
    job = bot.worker_takes()
    assert "night" not in job["meta"]                                       # not told: it may well be day
    bot.finish(job["id"], reply(text="在的呀～"))
    held = bot.prm.store.packet_for_job(job["id"])
    assert state(held) == WAITS and "时间和微信对不上" in held["meta"]["night_held"]
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1 and bot.texts[-1] == (CREATOR, HOLDING_TEXT)   # the day's words
    bot.prm.on_own_message(own(bot, HOLDING_TEXT, clock.now - 2))            # put right: measured again
    assert bot.prm.snapshot()["night"]["suspended"] == "" and "电脑的时间" not in bot.prm.store.deal(1)["attention"]
    assert state(drafted(bot, reply(text="好的呀～"), text="好的")) == GOES


def test_a_clock_notice_left_by_a_run_before_suspends_until_measured(bot, clock):
    """Restarted with a deal still saying this PC's clock was off (nothing measured it right since): suspended
    until the bot's own line read back says it is right.  A job made while it was suspended was not told it is
    night: its draft waits even when the clock is right by the time it comes, and says why."""
    bot.prm.store.set_notice(1, "clock", service.clock_note(2 * 3600), clock.now, touch=False)
    assert "上次运行" in bot.prm.snapshot()["night"]["suspended"]
    held = drafted(bot, reply(text="好的呀～"), text="你好")
    assert state(held) == WAITS and "时间和微信对不上" in held["meta"]["night_held"]
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1 and bot.texts == [(CREATOR, HOLDING_TEXT)]   # the day's words
    bot.prm.handle(bot.msg("在吗"))                                          # a job made while suspended
    job = bot.worker_takes()
    assert "night" not in job["meta"]
    bot.prm.on_own_message(own(bot, HOLDING_TEXT, clock.now - 1))            # measured: the clock is right
    assert bot.prm.snapshot()["night"]["suspended"] == ""
    bot.finish(job["id"], reply(text="在的呀～"))
    late = bot.prm.store.packet_for_job(job["id"])
    assert state(late) == WAITS and "电脑时间和微信对不上" in late["meta"]["night_held"]
    assert state(drafted(bot, reply(text="好的呀～"), text="好的")) == GOES


# ── rule 2: only the last step; everything before it stays as by day ──────

def _platform_action(b):
    b.prm.handle(b.msg("好的，那就按 20% 来"))
    job = b.worker_takes()
    b.finish(job["id"], {**reply("money", "专属佣金 20% 设好啦～"), "platform": block()})
    return b.prm.store.packet_for_job(job["id"])


def _taken_over(b):
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    b.prm.on_own_message(own(b, "我是运营小王，我来回", b.clock.now))
    b.finish(job["id"], reply())
    return b.prm.store.packet_for_job(job["id"])


def _paused(b):
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    b.prm.deal_action("mode", {"deal_id": 1, "mode": "paused"})
    b.finish(job["id"], reply())
    return b.prm.store.packet_for_job(job["id"])


def _newer_job(b):
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    with a_picture(b.tmp):
        b.prm.handle(b.msg("[图片]", msg_type=3))                            # a job of its own while #1 runs
    b.finish(job["id"], reply())
    return b.prm.store.packet_for_job(job["id"])


def _read_late(b):
    b.prm.handle(b.msg("你好", ts=b.clock.now - 3600))                       # came in while the bot was stopped
    job = b.worker_takes()
    assert "late" in job["meta"] and "night" not in job["meta"]
    b.finish(job["id"], reply())
    return b.prm.store.packet_for_job(job["id"])


def _recalled(b):
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    b.prm.handle(b.msg("[消息类型:10000]", msg_type=10000, special={"kind": "system", "title": '"小美" 撤回了一条消息'}))
    b.finish(job["id"], reply())
    return b.prm.store.packet_for_job(job["id"])


def _system_lane(b):
    _, job_id = b.relay.submit(CREATOR, CREATOR, "", kind=PRM, fresh=True, meta={
        "party_id": 1, "deal_id": 1, "name": "小美", "system_note": "寄样单建好了，请起草开专属佣金这一步",
        "night": {"window": HOURS, "at": "21:30"}})
    job = b.worker_takes()
    assert job["id"] == job_id
    b.finish(job_id, reply("money", "专属佣金开好啦～"))
    return b.prm.store.packet_for_job(job_id)


def _buyin(b):
    """A 百应私信 draft (``meta.channel``): its own path (``BUYIN_TOOL``, PRM_BUYIN_SYNC_MIN), copied and sent by an
    operator in 百应, never by the bot, so never by the window either (it is not judged by it at all)."""
    _, job_id = b.relay.submit(CREATOR, CREATOR, "[10-29 21:30] 你好", kind=PRM, fresh=True, meta={
        "party_id": 1, "deal_id": 1, "name": "小美", "channel": "buyin", "night": {"window": HOURS, "at": "21:30"}})
    job = b.worker_takes()
    b.finish(job["id"], reply())
    packet = b.prm.store.packet_for_job(job_id)
    assert packet["tool"] == BUYIN_TOOL and "night_held" not in packet["meta"]
    return packet


def _new_friend(b):
    taken = b.prm.take_new_friend(CREATOR, [b.msg("你好呀，看到你们的帽子了", ts=b.clock.now - 60)])
    assert taken == 1
    job = b.worker_takes()
    assert "new_friend" in job["meta"] and "night" not in job["meta"]
    b.finish(job["id"], reply())
    return b.prm.store.packet_for_job(job["id"])


def _handed_on(b):
    b.prm.handle(b.msg("样品什么时候寄"))
    first = b.worker_takes()
    b.clock.now += 1000
    b.relay.tick()                                                          # lost
    b.prm.handle(b.msg("在吗"))
    second = b.worker_takes()
    b.finish(second["id"], reply(text="稍等哈～"))
    assert b.relay.finish(first["id"], {"ok": True, "text": json.dumps(reply(text="在的"), ensure_ascii=False)})
    b.relay.tick()
    return b.prm.store.packet_for_job(first["id"])


def _opening(b):
    """A 开场白 (our first line to a creator who has not written): always reviewed, its job never told it is night."""
    party = b.prm.register("wxid_quiet01", "小青", "测试达人")
    answer = b.prm.ask_opening(b.prm.store.deal_of(party["id"])["id"])
    job = b.worker_takes()
    assert job["id"] == answer["job"] and "night" not in job["meta"]
    b.finish(job["id"], reply("greeting", "小青你好呀～很高兴认识你"))
    return b.prm.store.packet_for_job(job["id"])


# What waits for a person whatever the hour: the night window's and 自动模式's tests alike
WAITING = [
    (_platform_action, "pending"), (_taken_over, "superseded"), (_paused, "superseded"), (_newer_job, "superseded"),
    (_read_late, "pending"), (_recalled, "pending"), (_system_lane, "pending"), (_buyin, "pending"),
    (_new_friend, "pending"), (_handed_on, "superseded"), (_opening, "pending"),
]


@pytest.mark.parametrize("make, status", WAITING, ids=lambda v: getattr(v, "__name__", v))
def test_what_waits_for_a_person_by_day_waits_at_night_too(bot, tmp_path, make, status):
    bot.tmp = tmp_path
    packet = make(bot)
    assert packet["status"] == status and packet["route"] == "review"
    bot.prm.send_approved()
    assert packet["text"] not in [text for _, text in bot.texts]


# ── rules 3–5: complaints, the plain-draft checks, the red line ───────────

def test_a_complaint_never_goes_out_by_itself(bot):
    words = "非常抱歉给您添麻烦了，这个情况我记下啦，明早第一时间回复您"
    complaint = drafted(bot, reply("complaint", words, needs_attention=True, reason="达人投诉"), text="帽子坏了")
    assert state(complaint) == WAITS and complaint["meta"]["night_held"] == "投诉一律等运营"
    other = drafted(bot, reply("other", words, needs_attention=True, reason="达人说帽子坏了"), text="怎么办")
    assert state(other) == GOES


def test_what_plain_reason_checks_holds_at_night_too(bot, clock, tmp_path):
    unknown = drafted(bot, reply("other", "好的，打这个电话：〈手机3〉"))
    assert state(unknown) == WAITS and unknown["meta"]["night_held"] == "有对不上的占位符"
    known = drafted(bot, draft(), text="我是小美，手机 13812345678")             # 〈手机1〉 is hers
    assert state(known) == GOES
    no_source = drafted(bot, reply("faq", "都是包邮的哦～", reason="我觉得"))
    assert state(no_source) == WAITS and no_source["meta"]["night_held"] == "回答没有注明 kb/ 出处"
    assert state(drafted(bot, reply("faq", "都是包邮的哦～", reason="kb/合作政策.md 第 1 节"))) == GOES
    wrong_waybill = drafted(bot, reply("shipping_notice", "样品已经寄出啦：顺丰 SF1234567890123"))
    assert state(wrong_waybill) == WAITS and wrong_waybill["meta"]["night_held"] == "单号和系统记录对不上"
    bot.prm.deal_action("shipment", {"deal_id": 1, "carrier": "顺丰", "waybill": "SF1234567890123"})
    assert state(drafted(bot, reply("shipping_notice", "样品已经寄出啦：顺丰 SF1234567890123"), text="寄了吗")) == GOES
    no_files = drafted(bot, reply("material", "素材发你啦～", reason="kb/素材规范.md"))
    assert state(no_files) == WAITS and no_files["meta"]["night_held"] == "素材文件没有收到"


SHORT = reply("material", "两份资料发你啦～", reason="kb/素材规范.md", attachments_to_send=["assets/a.txt"],
              attachments_named=["assets/a.txt", "assets/b.txt"],
              attachments_missing=[{"name": "assets/b.txt", "why": "超过 100 MB"}])


def test_a_draft_short_of_its_files_or_without_its_platform_action_waits(bot):
    short = pending_packet(bot, SHORT, files=[("a.txt", b"a")])
    assert state(short) == WAITS and short["meta"]["night_held"].startswith("草稿要附 2 个文件，只收到 1 个")
    whole = pending_packet(bot, {**SHORT, "attachments_to_send": ["assets/a.txt", "assets/b.txt"],
                                 "attachments_missing": []}, files=[("a.txt", b"a"), ("b.txt", b"b")])
    assert state(whole) == GOES
    over = {**block(), "action": {**platforms.check_action(DESIGNATED), "commission_rate": "26"}}
    refused = drafted(bot, {**reply("money", "专属佣金设好啦～"), "platform": over})
    assert state(refused) == WAITS and refused["tool"] == "message.send"
    assert refused["meta"]["night_held"] == "平台动作被去掉了，回复是按执行成功写的"


def test_a_draft_that_never_had_an_action_waits_for_its_own_reason(bot):
    """review 2026-11-09 F7: a plain draft whose reply still says {链接} waits saying so, not 「平台动作被去掉了」 (it
    never had one)."""
    plain = drafted(bot, reply("money", "专属链接给您：{链接}", reason="kb/合作政策.md"))
    assert state(plain) == WAITS and plain["meta"]["night_held"].startswith("回复里还有 {链接}（专属推广链接）")


@pytest.mark.parametrize("words, goes", [
    ("我们达人合作的佣金是 20%，走抖音百应 / 小红书千帆的专属佣金，订单平台自动结算～", True),
    ("数据好的老师可以给到 25%～", True),
    ("这款是 100%羊毛的，秋冬戴很暖和～", True),
    ("可以给到 30%～", False),
    ("这边能给到三十个点～", False),
    ("能给到百分之三十哦", False),
    ("坑位费的话我们是没有的～", False),
    ("佣金 20%，另外返现 50 元～", False),
], ids=["20%", "25%", "100%羊毛", "30%", "三十个点", "百分之三十", "坑位", "返现"])
def test_the_red_line_at_night(bot, words, goes):
    packet = drafted(bot, reply("money", words))
    assert state(packet) == (GOES if goes else WAITS)
    if not goes:
        assert packet["meta"]["night_held"].startswith("说到了")


def test_night_red_line():
    for words in ("26%", "30 %", "３０％", "25.5%", "百分之三十", "百分之二十到三十", "三十个点", "30个点", "30 个点",
                  "二十多个点", "三十个百分点", "三成", "100%满意", "20-30%"):
        assert "超过 25%" in gates.night_red_line(f"佣金{words}"), words
    for words in ("20%", "25%", "百分之二十", "二十五个点", "两个点", "十多个点", "100%羊毛", "100% 棉", "100%拉菲草",
                  "１００％真丝", "100% 桑蚕丝", "100%麻", "100% 聚酯纤维", "100% 羊绒", "百分之百羊毛", "一成不变", "成本"):
        assert gates.night_red_line(f"这款{words}") == "", words
    for word in gates.NIGHT_RED_WORDS:
        assert gates.night_red_line(f"关于{word}的事") == f"说到了「{word}」"
    assert gates.night_red_line("专属佣金开好啦") == ""                      # 专属价 is the red word, not 专属佣金


# ── rule 6: a draft the model flagged goes only as one holding line ───────

def test_a_flagged_draft_goes_as_one_holding_line_and_the_deal_says_so_until_seen_to(bot, clock):
    packet = drafted(bot, reply("other", SAMPLE_LINE, needs_attention=True, reason=SAMPLE_REASON,
                                stage_proposal="agreed"), text="好呀，可以寄来看看")
    assert state(packet) == GOES and packet["needs_attention"]               # the console keeps its 要注意
    assert bot.prm.store.deal(1)["attention"] == SAMPLE_REASON                # (sent: the notice says it)
    assert bot.prm.send_approved() == 1 and bot.texts == [(CREATOR, SAMPLE_LINE)]
    notice = f"夜间已自动回了过渡的话，要注意：{SAMPLE_REASON}"
    assert bot.prm.store.deal(1)["attention"] == notice
    [alert] = bot.prm.snapshot()["night_alerts"]
    assert alert == {"deal_id": 1, "party_id": 1, "party_name": "小美", "packet": packet["id"], "text": notice,
                     "at": clock.now, "auto": False}
    assert event_out(bot.prm.store.events(1, limit=1)[0])["name"] == (
        f"夜间自动发出 #{packet['id']}（模型要求注意，发的是一句过渡的话）")
    clock.now += 600
    later = drafted(bot, reply(text="好的呀～"), text="好的")                 # her next draft: the notice stays
    assert state(later) == GOES and bot.prm.store.deal(1)["attention"] == notice
    assert bot.prm.deal_action("night_done", {"deal_id": 1, "packet": packet["id"]}) == {"ok": True}
    assert bot.prm.store.deal(1)["attention"] == "" and bot.prm.snapshot()["night_alerts"] == []
    assert bot.prm.deal_action("night_done", {"deal_id": 1, "packet": packet["id"]})["ok"] is False
    done = next(e for e in bot.prm.store.events(1) if e["kind"] == "night_done")
    assert done["actor"] == "console" and event_out(done)["name"] == f"夜间自动回的过渡话 #{packet['id']} 已处理"


@pytest.mark.parametrize("words, why", [
    ("好的～明早给您寄 3 顶，记下啦", "有数字"),
    ("好的～明早给您寄三顶，记下啦", "有数量或日期「三顶」"),
    ("好的，两天内回复您", "有数量或日期「两天」"),
    ("可以的，明早给您两个点", "有数量或日期「两个点」"),
    ("可以的，明早给您三十个点", "说到了「三十个点」"),
    ("好的～坑位的事我记下啦", "说到了「坑位」"),
    ("这个超出我能直接定的范围啦，我跟老板申请一下，尽快给你答复哈～", "没有说会记下、明早确认或第一时间回她"),
    ("记下啦" + "，" * 120, "超过 120 个字"),
], ids=["digit", "三顶", "两天", "两个点", "三十个点", "坑位", "no-cue", "long"])
def test_a_flagged_draft_that_is_no_safe_holding_line_waits(bot, words, why):
    packet = drafted(bot, reply("other", words, needs_attention=True, reason="拿不准"))
    assert state(packet) == WAITS and why in packet["meta"]["night_held"]
    bot.prm.send_approved()
    assert bot.texts == [] and bot.prm.snapshot()["night_alerts"] == []


def test_a_flagged_draft_with_files_waits(bot):
    packet = pending_packet(bot, reply("material", HOLD_LINE, reason="kb/素材规范.md 拿不准", needs_attention=True,
                                       attachments_to_send=["assets/a.txt"]), files=[("a.txt", b"a")])
    assert state(packet) == WAITS and "带着文件" in packet["meta"]["night_held"]


def test_night_holding_problem():
    for line in (SAMPLE_LINE, HOLD_LINE, NIGHT_HOLDING_TEXT, "我确认一下马上回您", "好的～晚一点回您", "收到，稍后回复您～"):
        assert gates.night_holding_ok(line), line
    for line, why in (("明早给您30%", "有数字"), ("〈手机1〉记下啦", "有数字"), ("１０点前回您", "有数字"),
                      ("十一点前回您", "有数量或日期「十一点」"), ("我先拉一个小群，明早聊", "有数量或日期「一个」"),
                      ("给您三万，明早确认", "有数量或日期「三万」"), ("百分之二十，明早确认", "有数量或日期「百分之」"),
                      ("半价给您，明早确认", "有数量或日期「半价」"), ("好的～", "没有说会记下"), ("", "没有文字")):
        assert why in gates.night_holding_problem(line), line
        assert not gates.night_holding_ok(line)


def test_her_recall_holds_a_night_send_not_gone_yet(bot):
    packet = drafted(bot, reply(text="好的呀～"), text="你好")
    assert state(packet) == GOES
    bot.prm.handle(bot.msg("[消息类型:10000]", msg_type=10000, special={"kind": "system", "title": '"小美" 撤回了一条消息'}))
    held = bot.prm.store.packet(packet["id"])
    assert state(held) == WAITS and held["needs_attention"] and held["decided_at"] is None
    assert bot.prm.send_approved() == 0 and bot.texts == []


# ── rule 7: no person decided on a night send ──────────────────────────────

def test_night_sends_do_not_count_toward_a_kinds_gate_and_one_can_be_marked_wrong(bot, clock):
    for i in range(20):
        assert state(drafted(bot, reply("greeting", f"第{i}条：好的呀～"), text=f"消息{i}")) == GOES
        bot.prm.send_approved()
    assert bot.prm.store.reviewed("greeting", 0, 20) == []
    gate = next(g for g in bot.prm.snapshot()["gates"] if g["kind"] == "greeting")
    assert (gate["route"], gate["decided"], gate["unchanged"]) == ("review", 0, 0)
    sent = bot.prm.store.packets(("sent",), limit=1)[0]
    clock.now += 60
    assert bot.prm.decide(sent["id"], "wrong") == {"ok": True, "status": "wrong"}
    assert bot.prm.store.gate("greeting")["reset_at"] == clock.now           # as for an auto send: from zero
    wrong = bot.prm.store.events(1, limit=1)[0]
    assert wrong["detail"] == {"packet": sent["id"], "kind": "greeting", "night": True}
    assert event_out(wrong)["name"] == "标为发错了（夜间自动发出的）"
    recent = next(p for p in bot.prm.snapshot()["recent"] if p["id"] == sent["id"])
    assert recent["route"] == "night" and recent["wrong"]


# ── rule 8: the holding reply says the night's words ──────────────────────

def test_the_holding_reply_inside_the_window_says_the_nights_words(clock, tmp_path):
    b = make_bot(clock, tmp_path, prm_night_auto_hours=HOURS, prm_night_holding_text="收到啦～明早回复您")
    held = drafted(b, reply("money", "可以给到 30%～"))
    assert state(held) == WAITS
    clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 1 and b.texts == [(CREATOR, "收到啦～明早回复您")]
    assert b.prm.store.events(1, limit=1)[0]["detail"]["text"] == "收到啦～明早回复您"
    clock.now = at(14, 0, day=30)
    drafted(b, reply("money", "可以给到 30%～"), text="那 30% 呢")
    clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 1 and b.texts[-1] == (CREATOR, HOLDING_TEXT)
    b.relay.stop()
    default = make_bot(clock, tmp_path / "default", prm_night_auto_hours=HOURS)
    clock.now = at(22, 0, day=30)
    drafted(default, reply("money", "可以给到 30%～"))
    clock.now += 11 * 60
    assert default.prm.send_holding_replies() == 1 and default.texts == [(CREATOR, NIGHT_HOLDING_TEXT)]
    default.relay.stop()


# ── the console ───────────────────────────────────────────────────────────

def test_the_console_says_where_the_window_stands(bot, clock):
    assert bot.prm.snapshot()["night"] == {"on": True, "window": "18:00–09:00", "inside": True, "suspended": "",
                                           "error": "", "auto": False}
    clock.now = at(9, 30, day=30)
    assert bot.prm.snapshot()["night"]["inside"] is False
    held = drafted(bot, reply("money", "可以给到 30%～"))
    assert next(p for p in bot.prm.snapshot()["pending"] if p["id"] == held["id"])["night_held"] == ""
    clock.now = at(20, 0, day=30)
    held = drafted(bot, reply("money", "可以给到 30%～"), text="那 30% 呢")
    shown = next(p for p in bot.prm.snapshot()["pending"] if p["id"] == held["id"])
    assert shown["night_held"] == "说到了「30%」（超过 25%）" and shown["route"] == "review"


# ── 自动模式 (the console's switch, 2026-10-30): the night rules at any hour, until switched off ──────────

# A holding line by 自动模式's rules (kb): no 「明早」, no number
AUTO_LINE = "好的～这个我记下啦，稍后跟负责人确认好第一时间回您哈！"


@pytest.fixture
def auto_bot(tmp_path):
    """No 下班时段 set, 14:30, 自动模式 switched on in the console."""
    b = make_bot(Clock(at(14, 30, day=30)), tmp_path)
    assert b.prm.set_auto_mode(True)["auto_mode"]["on"] is True
    yield b
    b.prm.stop()
    b.relay.stop()


def test_auto_mode_is_off_until_switched_on_and_the_switch_outlives_a_restart(clock, tmp_path, caplog):
    clock.now = at(14, 30, day=30)
    b = make_bot(clock, tmp_path)
    off = {"on": False, "since": None, "by": ""}
    assert b.prm.auto_mode == off and b.prm.snapshot()["auto_mode"] == off
    since = clock.now
    with caplog.at_level(logging.INFO, logger="src.prm.service"):
        assert b.prm.set_auto_mode(True) == {"ok": True, "auto_mode": {"on": True, "since": since, "by": "console"}}
        clock.now += 60
        assert b.prm.set_auto_mode(True)["auto_mode"]["since"] == since      # a double click: the time it went on stays
    said = [r.getMessage() for r in caplog.records if "auto mode" in r.getMessage()]
    assert len(said) == 1 and said[0].startswith("PRM: auto mode switched on (console)")
    assert json.loads(b.prm.store.meta_value(service.AUTO_MODE)) == {"on": True, "since": since, "by": "console"}
    # the bot restarted on the same database: still on; switched off there, this one reads it at once
    again = PrmService(config(), ClaudeRelay(config(), store=RelayStore(":memory:"), clock=clock), store=b.prm.store,
                       clock=clock)
    assert again.auto_mode == {"on": True, "since": since, "by": "console"}
    assert again.set_auto_mode(False)["auto_mode"] == {"on": False, "since": clock.now, "by": "console"}
    assert b.prm.auto_mode["on"] is False and b.prm.snapshot()["night"]["auto"] is False
    for raw in ("not json", '{"on": "yes"}', '{"on": 1}', "[]", ""):
        b.prm.store.set_meta_value(service.AUTO_MODE, raw)
        assert b.prm.auto_mode["on"] is False, raw
    b.relay.stop()


def test_a_plain_draft_goes_out_by_itself_in_auto_mode_and_says_so(auto_bot):
    b = auto_bot
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    assert job["meta"]["night"] == {"auto": True, "at": "14:30"}              # no window: by day, and the Mac is told
    b.finish(job["id"], reply("greeting", "好的呀～很高兴认识你"))
    packet = b.prm.store.packet_for_job(job["id"])
    assert state(packet) == GOES and packet["meta"] == {"auto_mode": True} and not packet["needs_attention"]
    assert b.prm.store.gate("greeting")["route"] == "review"                 # its kind has not earned auto
    assert b.prm.send_approved() == 1 and b.texts == [(CREATOR, "好的呀～很高兴认识你")]
    [ev] = b.prm.store.events(1)
    assert ev["detail"] == {"packet": packet["id"], "kind": "greeting", "holding": False, "auto": True}
    assert event_out(ev)["name"] == f"自动模式发出 #{packet['id']}"
    shown = next(p for p in b.prm.snapshot()["recent"] if p["id"] == packet["id"])
    assert (shown["route"], shown["auto_mode"], shown["night_held"]) == ("night", True, "")
    assert b.prm.store.reviewed("greeting", 0, 20) == [] and b.prm.store.deal(1)["attention"] == ""
    b.clock.now += 60
    assert b.prm.decide(packet["id"], "wrong") == {"ok": True, "status": "wrong"}
    wrong = b.prm.store.events(1, limit=1)[0]
    assert wrong["detail"] == {"packet": packet["id"], "kind": "greeting", "night": True, "auto": True}
    assert event_out(wrong)["name"] == "标为发错了（自动模式发出的）"
    assert b.prm.store.gate("greeting")["reset_at"] == b.clock.now


def test_a_flagged_draft_goes_out_in_auto_mode_only_as_one_holding_line_and_the_notice_says_so(auto_bot):
    b = auto_bot
    packet = drafted(b, reply("other", AUTO_LINE, needs_attention=True, reason=SAMPLE_REASON, stage_proposal="agreed"),
                     text="好呀，可以寄来看看")
    assert state(packet) == GOES and packet["needs_attention"] and packet["meta"] == {"auto_mode": True}
    assert b.prm.send_approved() == 1 and b.texts == [(CREATOR, AUTO_LINE)]
    notice = f"自动模式下自动回了过渡的话，要注意：{SAMPLE_REASON}"
    assert b.prm.store.deal(1)["attention"] == notice
    [alert] = b.prm.snapshot()["night_alerts"]
    assert alert == {"deal_id": 1, "party_id": 1, "party_name": "小美", "packet": packet["id"], "text": notice,
                     "at": b.clock.now, "auto": True}
    assert event_out(b.prm.store.events(1, limit=1)[0])["name"] == (
        f"自动模式发出 #{packet['id']}（模型要求注意，发的是一句过渡的话）")
    assert b.prm.deal_action("night_done", {"deal_id": 1, "packet": packet["id"]}) == {"ok": True}
    done = b.prm.store.events(1, limit=1)[0]
    assert done["detail"] == {"packet": packet["id"], "auto": True}
    assert event_out(done)["name"] == f"自动模式自动回的过渡话 #{packet['id']} 已处理"
    assert b.prm.store.deal(1)["attention"] == "" and b.prm.snapshot()["night_alerts"] == []


@pytest.mark.parametrize("words, why", [
    ("好的～稍后给您寄 3 顶，记下啦", "有数字"),
    ("好的～坑位的事我记下啦", "说到了「坑位」"),
    ("这个我跟老板申请一下哈～", "没有说会记下"),
], ids=["digit", "坑位", "no-cue"])
def test_a_flagged_draft_that_is_no_safe_holding_line_waits_and_says_auto_mode(auto_bot, words, why):
    packet = drafted(auto_bot, reply("other", words, needs_attention=True, reason="拿不准"))
    assert state(packet) == WAITS and why in packet["meta"]["night_held"] and packet["meta"]["auto_mode"] is True
    shown = next(p for p in auto_bot.prm.snapshot()["pending"] if p["id"] == packet["id"])
    assert shown["auto_mode"] is True and why in shown["night_held"]
    auto_bot.prm.send_approved()
    assert auto_bot.texts == [] and auto_bot.prm.snapshot()["night_alerts"] == []


def test_a_flagged_draft_with_files_waits_and_the_reason_names_auto_mode(auto_bot):
    packet = pending_packet(auto_bot, reply("material", AUTO_LINE, reason="kb/素材规范.md 拿不准", needs_attention=True,
                                            attachments_to_send=["assets/a.txt"]), files=[("a.txt", b"a")])
    assert state(packet) == WAITS
    assert packet["meta"]["night_held"] == "模型要求注意，草稿还带着文件（自动模式下只发一句过渡的话）"


def test_drafts_waiting_when_it_is_switched_on_wait_and_a_job_from_before_says_why(clock, tmp_path):
    clock.now = at(14, 30, day=30)
    b = make_bot(clock, tmp_path)
    b.prm.register("wxid_creator02", "小兰", "测试达人")
    waiting = drafted(b, reply("greeting", "好的呀～"), text="你好")
    b.prm.handle(b.msg("在吗", chat="wxid_creator02"))                       # a job made before the switch
    job = b.worker_takes()
    assert "night" not in job["meta"] and state(waiting) == WAITS
    b.prm.set_auto_mode(True)
    b.prm.tick()                                                             # the sender thread's round
    assert state(b.prm.store.packet(waiting["id"])) == WAITS and b.texts == []
    b.finish(job["id"], reply("greeting", "在的呀～"))
    held = b.prm.store.packet_for_job(job["id"])
    assert state(held) == WAITS and held["meta"]["auto_mode"] is True
    assert held["meta"]["night_held"] == ("草稿任务建的时候自动模式还没打开（或者电脑时间和微信对不上），"
                                          "Mac 起草时不知道是自动模式")
    assert state(drafted(b, reply("greeting", "好的呀～"), text="好的")) == GOES      # the next job is told
    b.relay.stop()


def test_switched_off_the_drafts_wait_for_a_person_again(auto_bot):
    """Pressed again: a draft whose job was made while it was on waits as by day, its packet says nothing of the night
    rules, the next job is told nothing, the holding reply says the day's words."""
    b = auto_bot
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    assert job["meta"]["night"]["auto"] is True
    assert b.prm.set_auto_mode(False)["auto_mode"]["on"] is False
    b.finish(job["id"], reply("greeting", "好的呀～"))
    packet = b.prm.store.packet_for_job(job["id"])
    assert state(packet) == WAITS and packet["meta"] == {}
    b.prm.handle(b.msg("在吗"))
    later = b.worker_takes()
    assert "night" not in later["meta"]
    b.finish(later["id"], reply("greeting", "在的呀～"))
    assert state(b.prm.store.packet_for_job(later["id"])) == WAITS
    b.clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 1 and b.texts == [(CREATOR, HOLDING_TEXT)]
    assert b.prm.snapshot()["night"] == {"on": False, "window": "", "inside": False, "suspended": "", "error": "",
                                         "auto": False}


def test_while_this_pcs_clock_is_off_auto_mode_sends_nothing_by_itself(auto_bot):
    b = auto_bot
    first = drafted(b, reply(text="好的呀～"), text="你好")
    assert state(first) == GOES and b.prm.send_approved() == 1
    b.prm.on_own_message(own(b, "好的呀～", b.clock.now - 3 * 3600))          # read back: this PC is 3 hours fast
    status = b.prm.snapshot()["night"]
    assert status["auto"] is True and status["on"] is False
    assert "比微信快 3 小时：自动模式先不自动发" in status["suspended"]
    b.prm.handle(b.msg("在吗"))
    job = b.worker_takes()
    assert "night" not in job["meta"]                                        # not told
    b.finish(job["id"], reply(text="在的呀～"))
    held = b.prm.store.packet_for_job(job["id"])
    assert state(held) == WAITS and held["meta"]["night_held"] == "这台电脑的时间和微信对不上，自动模式先不自动发"
    b.clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 1 and b.texts[-1] == (CREATOR, HOLDING_TEXT)    # the day's words
    b.prm.on_own_message(own(b, HOLDING_TEXT, b.clock.now - 2))              # put right: measured again
    assert b.prm.snapshot()["night"]["suspended"] == ""
    assert state(drafted(b, reply(text="好的呀～"), text="好的")) == GOES


def test_a_clock_notice_left_by_a_run_before_suspends_auto_mode_too(auto_bot):
    b = auto_bot
    b.prm.store.set_notice(1, "clock", service.clock_note(2 * 3600), b.clock.now, touch=False)
    suspended = b.prm.snapshot()["night"]["suspended"]
    assert "上次运行" in suspended and "自动模式先不自动发" in suspended
    held = drafted(b, reply(text="好的呀～"), text="你好")
    assert state(held) == WAITS and held["meta"]["night_held"] == "这台电脑的时间和微信对不上，自动模式先不自动发"


def test_inside_the_window_auto_mode_speaks_the_nights_words_and_by_day_its_own(clock, tmp_path):
    clock.now = at(21, 30)
    b = make_bot(clock, tmp_path, prm_night_auto_hours=HOURS)
    b.prm.set_auto_mode(True)
    assert b.prm.snapshot()["night"] == {"on": True, "window": "18:00–09:00", "inside": True, "suspended": "",
                                         "error": "", "auto": False}
    b.prm.handle(b.msg("好呀，可以寄来看看"))
    job = b.worker_takes()
    assert job["meta"]["night"] == {"window": HOURS, "at": "21:30"}
    b.finish(job["id"], reply("other", SAMPLE_LINE, needs_attention=True, reason=SAMPLE_REASON))
    night = b.prm.store.packet_for_job(job["id"])
    assert state(night) == GOES and night["meta"] == {}
    assert b.prm.send_approved() == 1
    assert b.prm.store.deal(1)["attention"] == f"夜间已自动回了过渡的话，要注意：{SAMPLE_REASON}"
    assert b.prm.snapshot()["night_alerts"][0]["auto"] is False
    assert event_out(b.prm.store.events(1, limit=1)[0])["name"].startswith(f"夜间自动发出 #{night['id']}")
    clock.now += 60
    assert state(drafted(b, reply("money", "可以给到 30%～"), text="能给 30% 吗")) == WAITS
    clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 1 and b.texts[-1] == (CREATOR, NIGHT_HOLDING_TEXT)
    # the next day at 14:00, still on: 自动模式's own words
    clock.now = at(14, 0, day=30)
    status = b.prm.snapshot()["night"]
    assert (status["inside"], status["auto"]) == (False, True)
    b.prm.handle(b.msg("那 25% 呢"))
    job = b.worker_takes()
    assert job["meta"]["night"] == {"auto": True, "at": "14:00"}
    b.finish(job["id"], reply("money", "可以给到 30%～"))
    day = b.prm.store.packet_for_job(job["id"])
    assert state(day) == WAITS and day["meta"] == {"auto_mode": True, "night_held": "说到了「30%」（超过 25%）"}
    clock.now += 11 * 60
    assert b.prm.send_holding_replies() == 1 and b.texts[-1] == (CREATOR, AUTO_HOLDING_TEXT)
    b.relay.stop()


def test_the_holding_reply_in_auto_mode_by_day_says_no_mingzao(auto_bot):
    assert "明早" not in AUTO_HOLDING_TEXT and gates.night_holding_ok(AUTO_HOLDING_TEXT)
    assert gates.night_holding_ok(AUTO_LINE)
    held = drafted(auto_bot, reply("money", "可以给到 30%～"))
    assert state(held) == WAITS
    auto_bot.clock.now += 11 * 60
    assert auto_bot.prm.send_holding_replies() == 1 and auto_bot.texts == [(CREATOR, AUTO_HOLDING_TEXT)]
    assert auto_bot.prm.store.events(1, limit=1)[0]["detail"]["text"] == AUTO_HOLDING_TEXT


@pytest.mark.parametrize("make, status", WAITING, ids=lambda v: getattr(v, "__name__", v))
def test_what_waits_for_a_person_by_day_waits_in_auto_mode_too(auto_bot, tmp_path, make, status):
    auto_bot.tmp = tmp_path
    packet = make(auto_bot)
    assert packet["status"] == status and packet["route"] == "review"
    auto_bot.prm.send_approved()
    assert packet["text"] not in [text for _, text in auto_bot.texts]


def test_the_console_says_when_auto_mode_is_what_puts_the_night_rules_in_force(clock, tmp_path):
    clock.now = at(14, 30, day=30)
    b = make_bot(clock, tmp_path, prm_night_auto_hours=HOURS)
    b.prm.set_auto_mode(True)
    snap = b.prm.snapshot()
    assert snap["auto_mode"] == {"on": True, "since": clock.now, "by": "console"}
    assert snap["night"] == {"on": True, "window": "18:00–09:00", "inside": False, "suspended": "", "error": "",
                             "auto": True}
    clock.now = at(21, 30, day=30)
    assert b.prm.snapshot()["night"]["auto"] is False                        # inside the window: the night's own
    b.relay.stop()
    plain = make_bot(clock, tmp_path / "plain")                              # no window at all
    plain.prm.set_auto_mode(True)
    assert plain.prm.snapshot()["night"] == {"on": False, "window": "", "inside": False, "suspended": "", "error": "",
                                             "auto": True}
    plain.relay.stop()


# ── review 2026-10-30: a draft of the window's back by day; the switch read once per judgement ──

def test_a_draft_written_in_the_window_and_back_by_day_waits_in_auto_mode(clock, tmp_path):
    """Its job was made at 08:55, inside the window: the Mac wrote by the night's words (its holding line may promise
    「明早」).  It came back at 09:05 with 自动模式 on: it waits for a person, it never goes out by day with them."""
    clock.now = at(8, 55, day=30)
    b = make_bot(clock, tmp_path, prm_night_auto_hours=HOURS)
    b.prm.set_auto_mode(True)
    b.prm.handle(b.msg("好呀，可以寄来看看"))
    job = b.worker_takes()
    assert job["meta"]["night"] == {"window": HOURS, "at": "08:55"}
    clock.now = at(9, 5, day=30)
    b.finish(job["id"], reply("other", HOLD_LINE))
    packet = b.prm.store.packet_for_job(job["id"])
    assert state(packet) == WAITS
    assert packet["meta"] == {"auto_mode": True,
                              "night_held": "草稿任务是下班时段建的，Mac 按夜里的说法写的（过渡的话可能说「明早」），白天的自动模式不自动发"}
    # a job of 自动模式's own (made at 09:05): its draft goes
    b.prm.handle(b.msg("那什么时候寄呀"))
    job = b.worker_takes()
    assert job["meta"]["night"]["auto"] is True
    b.finish(job["id"], reply("other", "好的～我确认好第一时间回您哈"))
    assert state(b.prm.store.packet_for_job(job["id"])) == GOES
    b.prm.stop()
    b.relay.stop()


def switched_off_after_one_read(b, monkeypatch):
    """The console switches 自动模式 off right after the next read of it (a click between two reads)."""
    real = b.prm.store.meta_value
    reads = []

    def meta_value(key):
        value = real(key)
        if key == service.AUTO_MODE:
            reads.append(value)
            if len(reads) == 1:
                b.prm.store.set_meta_value(service.AUTO_MODE, json.dumps({"on": False, "since": b.clock.now,
                                                                          "by": "console"}))
        return value

    monkeypatch.setattr(b.prm.store, "meta_value", meta_value)
    return reads


def test_one_judgement_reads_the_switch_once(auto_bot, monkeypatch):
    """Switched off between two reads of one judgement, a draft that went by 自动模式 was tagged as the night's (its
    record: 夜间自动发出, by day), and a holding reply by day said the night's 「明早」: each reads the switch once."""
    b = auto_bot
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    reads = switched_off_after_one_read(b, monkeypatch)
    b.finish(job["id"], reply("greeting", "好的呀～很高兴认识你"))
    packet = b.prm.store.packet_for_job(job["id"])
    assert state(packet) == GOES and packet["meta"] == {"auto_mode": True} and len(reads) == 1
    monkeypatch.undo()
    b.prm.set_auto_mode(True)
    held = drafted(b, reply("money", "可以给到 30%～"), text="能给 30% 吗")
    assert state(held) == WAITS
    b.clock.now += 11 * 60
    reads = switched_off_after_one_read(b, monkeypatch)
    assert b.prm.send_holding_replies() == 1 and b.texts[-1] == (CREATOR, AUTO_HOLDING_TEXT)
    assert len(reads) == 1
