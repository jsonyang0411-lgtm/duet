"""补发货盘 (2026-11-04): the console's 「补发货盘」 sends a creator we talked to the 货盘 again.

The user: the colleague closed a few deals, we none; creators who asked for the 货盘 (or said 「我看一下」) went quiet.
One click asks the Mac for one draft (``meta.huopan``) that sends her the kb's 货盘 (the picture, the Excel for an
agency's 商务 or an assistant) with a line or two; it always waits for a person (never by a gate, by night, in 自动模式 or as a 开场白 that goes by itself), no
「稍等」 goes for it unless she wrote meanwhile, and her message joins the job or outdates its draft.  Names and numbers
here are made up.
"""

import io
import time

import pytest

from test_prm import CREATOR, Bot, Clock, draft

from src.prm.service import HUOPAN_NOTE, apply_deal_action, snapshot

PICTURE, SHEET = "ACE7CN秋冬货盘.jpg", "ACE7CN秋冬货盘.xlsx"
FILES = ((PICTURE, b"jpg"), (SHEET, b"xlsx"))
LINE = "老师您好～给您补一份我们秋冬当季的货盘，佣金都是 25%，您看看有没有喜欢的款～"
ANSWER = "好的～这几款现货您先看看：贝雷帽 399、小礼帽 269"
HOURS = "18:00-09:00"


def at(hour, minute=0, day=4):
    """A time of day on this machine's clock (a Wednesday, 2026-11-04)."""
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
    b = make(clock, tmp_path)
    yield b
    b.prm.stop()
    b.relay.stop()


def deal_id(b):
    return b.prm.store.deal_of(b.prm.store.party_by_wxid(CREATOR)["id"])["id"]


def reply(text, **more):
    return draft(**{"message_kind": "other", "reply_draft": text, "slots": {}, "stage_proposal": "", "reason": "kb", **more})


def talked(b):
    """She asked for the 货盘, our reply went out (an operator sent the Mac's draft); two days pass without a word."""
    b.prm.handle(b.msg("辛苦发一下货盘"))
    job = b.worker_takes()
    b.finish(job["id"], reply(ANSWER))
    assert b.prm.decide(b.prm.store.packet_for_job(job["id"])["id"], "send")["ok"]
    b.prm.send_approved()
    assert b.texts == [(CREATOR, ANSWER)]
    b.texts.clear()
    b.clock.now += 2 * 86400


def huopan_draft(text=LINE, files=FILES, **more):
    """The Mac's 补发货盘: a line or two with the 货盘's picture and Excel."""
    return draft(**{"message_kind": "material", "reply_draft": text, "slots": {}, "stage_proposal": "",
                    "intent": "补发货盘", "reason": "kb 第 3.0 节：货盘图和 Excel",
                    "attachments_to_send": [f"assets/货盘/{name}" for name, _ in files], **more})


def ask(b):
    """「补发货盘」 in the console, and the Mac taking its job."""
    answer = b.prm.deal_action("huopan", {"deal_id": deal_id(b)})
    assert answer["ok"] is True, answer
    job = b.worker_takes()
    assert job["id"] == answer["job"]
    return job


def drafted(b, job, d=None, files=FILES):
    for name, body in files:
        assert b.relay.outfile(job["id"], name, io.BytesIO(body).read, len(body))
    b.finish(job["id"], d or huopan_draft(files=files))
    return b.prm.store.packet_for_job(job["id"])


def shown(b):
    return snapshot(b.prm.store, running=True)


# ── one click: one job, one draft that waits for a person ────────────────

def test_one_click_asks_the_mac_for_the_huopan_and_its_draft_waits_for_a_person(bot, clock):
    """The job has no line of hers and says what it is (``meta.huopan``), what we sent her (the Mac sees whether the
    货盘 went out within 7 days), nothing of the night or of a 开场白; its draft waits in 待审批 with both files, tagged
    「补发货盘」, the deal says a person must look first and records the click; sent, the line and the files go out."""
    talked(bot)
    job = ask(bot)
    meta = job["meta"]
    assert job["text"] == "" and meta["huopan"] == {"by": "console"}
    assert [(s["what"], s["text"]) for s in meta["sent"]] == [("sent", ANSWER)]
    for key in ("opening", "opening_auto", "night", "late", "missed", "new_friend"):
        assert key not in meta
    packet = drafted(bot, job)
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert packet["meta"]["huopan"] is True and [a["name"] for a in packet["attachments"]] == [PICTURE, SHEET]
    assert bot.prm.store.deal(deal_id(bot))["attention"] == HUOPAN_NOTE
    [card] = [p for p in shown(bot)["pending"] if p["id"] == packet["id"]]
    assert (card["huopan"], card["opening"]) == (True, False)
    names = [e["name"] for e in shown(bot)["parties"][0]["deal"]["events"]]
    assert f"派补发货盘的任务 #{job['id']}（运营点的）" in names
    assert bot.prm.decide(packet["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, LINE)] and len(bot.files) == 2
    assert bot.prm.store.packet(packet["id"])["status"] == "sent"


def test_a_plain_reply_draft_is_not_tagged(bot):
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    assert "huopan" not in job["meta"]
    bot.finish(job["id"], reply("在的～"))
    packet = bot.prm.store.packet_for_job(job["id"])
    assert "huopan" not in packet["meta"]
    assert [p["huopan"] for p in shown(bot)["pending"]] == [False]


@pytest.mark.parametrize("how", ["gate", "night", "auto_mode", "opening_auto"])
def test_its_draft_never_goes_out_by_itself(clock, tmp_path, how):
    """Whatever sends a draft by itself (its kind's gate at auto, the 下班时段, 自动模式, PRM_OPENING_AUTO_MIN) leaves a
    补发货盘 for a person: the job is told nothing of it, the draft waits, hours later nothing went out."""
    prm = {"night": {"prm_night_auto_hours": HOURS}, "opening_auto": {"prm_opening_auto_min": 30}}.get(how, {})
    b = make(clock, tmp_path, **prm)
    try:
        talked(b)
        if how == "gate":
            b.prm.store.set_gate("material", "auto", clock.now)
        elif how == "night":
            clock.now = at(21, 30, day=6)
        elif how == "auto_mode":
            assert b.prm.set_auto_mode(True)["ok"]
        job = ask(b)
        assert "night" not in job["meta"] and "opening_auto" not in job["meta"]
        packet = drafted(b, job)
        assert (packet["status"], packet["route"]) == ("pending", "review")
        assert "auto_at" not in packet["meta"] and "night_held" not in packet["meta"]
        clock.now += 3 * 3600
        assert b.prm.send_due_openings() == 0
        b.prm.send_approved()
        assert b.prm.send_holding_replies() == 0
        assert b.texts == [] and b.files == []
        assert b.prm.store.packet(packet["id"])["status"] == "pending"
    finally:
        b.prm.stop()
        b.relay.stop()


# ── 稍等, her messages ─────────────────────────────────────────────────────

def test_no_holding_reply_goes_for_it_after_her_unanswered_ok(bot, clock):
    """She said 「好滴」 to our reply and nothing more (the kb: that needs no answer, so it stays unanswered); a day later
    an operator asks for the 货盘 again: its draft waits past PRM_HOLDING_MIN and no 「稍等」 goes to her, she is waiting
    for nothing.  Her next line is a round of its own: its draft gets one as before."""
    talked(bot)
    bot.prm.handle(bot.msg("好滴"))
    job = bot.worker_takes()
    bot.finish(job["id"], reply("", reason="她只是回个收到，不用回"))
    assert bot.prm.store.packet_for_job(job["id"])["status"] == "no_reply"
    clock.now += 86400
    packet = drafted(bot, ask(bot))
    clock.now += 3600
    assert bot.prm.send_holding_replies() == 0 and bot.texts == []
    bot.prm.handle(bot.msg("老师有黑色的吗"))
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    later = bot.worker_takes()
    bot.finish(later["id"], reply("有的～", needs_attention=True, reason="要看库存"))
    clock.now += 3600
    assert bot.prm.send_holding_replies() == 1 and len(bot.texts) == 1


def test_her_message_outdates_the_waiting_draft_and_her_job_hears_it_never_went_out(bot, clock):
    talked(bot)
    packet = drafted(bot, ask(bot))
    clock.now += 600
    bot.prm.handle(bot.msg("老师有黑色的吗"))
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    job = bot.worker_takes()
    assert "huopan" not in job["meta"] and "老师有黑色的吗" in job["text"]
    assert [(s["what"], s["text"]) for s in job["meta"]["sent"] if s["what"] == "failed"] == [("failed", LINE)]


def test_her_message_joins_the_job_the_mac_has_not_taken_and_gets_its_holding_reply(bot, clock):
    """She writes before the Mac takes the job: her line joins it (the Mac answers her and sends the 货盘); her line is
    waiting for an answer, so the draft that waits gets the 「稍等」 like any reply's."""
    talked(bot)
    answer = bot.prm.deal_action("huopan", {"deal_id": deal_id(bot)})
    bot.prm.handle(bot.msg("老师在吗"))
    job = bot.worker_takes()
    assert job["id"] == answer["job"] and job["meta"]["huopan"] == {"by": "console"} and "老师在吗" in job["text"]
    packet = drafted(bot, job, huopan_draft(text="在的～" + LINE))
    assert packet["status"] == "pending" and packet["meta"]["huopan"] is True
    clock.now += 3600
    assert bot.prm.send_holding_replies() == 1 and len(bot.texts) == 1


# ── why not, and what the deal says when there is no draft ──────────────

def test_why_no_huopan_is_asked_for_now(bot, clock):
    talked(bot)
    deal = deal_id(bot)
    for odd in (999, "x", None):
        assert bot.prm.deal_action("huopan", {"deal_id": odd}) == {"ok": False, "error": "没有找到这个合作单"}
    bot.prm.handle(bot.msg("在吗"))                                # her line is being drafted
    assert bot.prm.deal_action("huopan", {"deal_id": deal}) == {
        "ok": False, "error": "她的草稿正在写，写好会出现在「待审批」里"}
    job = bot.worker_takes()
    bot.finish(job["id"], reply("在的～"))
    assert bot.prm.deal_action("huopan", {"deal_id": deal}) == {
        "ok": False, "error": "「待审批」里已经有她的草稿：先处理那一份"}
    assert bot.prm.decide(bot.prm.store.packet_for_job(job["id"])["id"], "drop")["ok"]
    assert bot.prm.deal_action("mode", {"deal_id": deal, "mode": "human"})["ok"]
    assert bot.prm.deal_action("huopan", {"deal_id": deal}) == {
        "ok": False, "error": "现在不在副驾驶模式（人工接管或暂停），不补发货盘"}
    assert bot.prm.deal_action("mode", {"deal_id": deal, "mode": "copilot"})["ok"]
    assert bot.prm.deal_action("huopan", {"deal_id": deal})["ok"] is True
    assert bot.prm.deal_action("huopan", {"deal_id": deal}) == {      # a second click: the first job waits for the Mac
        "ok": False, "error": "她的草稿正在写，写好会出现在「待审批」里"}
    bot.prm.set_active(bot.prm.store.party_by_wxid(CREATOR)["id"], False)
    assert bot.prm.deal_action("huopan", {"deal_id": deal}) == {
        "ok": False, "error": "这位达人已经停用（不在达人名单里），不补发货盘"}
    # the bot stopped: the console's deal action cannot ask the Mac
    assert apply_deal_action(bot.prm.store, "huopan", {"deal_id": deal}, now=clock.now, takeover_sec=7200) == {
        "ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能补发货盘"}


def test_a_huopan_the_mac_did_not_write_says_why_on_the_deal(bot, clock):
    """The Mac wrote nothing (she had the 货盘 within 7 days): the deal says why, not 「要人看过再发」.  The Mac failed: a
    notice says so and how to go on, until the next click."""
    talked(bot)
    job = ask(bot)
    bot.finish(job["id"], huopan_draft(text="", files=(), reason="11-03 已经发过货盘（7 天内不重发）"))
    assert bot.prm.store.packet_for_job(job["id"])["status"] == "no_reply"
    assert bot.prm.store.deal(deal_id(bot))["attention"] == "「补发货盘」这次没写：11-03 已经发过货盘（7 天内不重发）"
    failed = ask(bot)
    bot.relay.finish(failed["id"], {"ok": False, "text": "", "error": "claude 没有给出结构化结果"})
    bot.relay.tick()
    attention = bot.prm.store.deal(deal_id(bot))["attention"]
    assert "补发货盘的草稿没写成" in attention and "再点一次「补发货盘」" in attention
    assert ask(bot)["meta"]["huopan"] == {"by": "console"}
    assert not any(n["key"] == f"failed:{failed['id']}" for n in bot.prm.store.deal(deal_id(bot))["notices"])
