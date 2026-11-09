"""A sample order (``jst.sample_order``) on the bot's side, the reviews of 2026-11-10 (review A): a new preview and its photo
marks are written in one step (A-L3); a preview that held a hat for photos no operator confirmed never runs from the
console, whatever its verdict says (A-L4); a second sample order for one deal (接群 gives her a private and a group lane,
each may have one waiting) is refused once, until an operator says 「我已处理」 on the other one, so a 补寄 stays
possible (A-L5).  Every creator, number and code is made up."""

import json
import time

import pytest

from test_group_link import GROUP, gmsg, job_of, linked, setup
from test_prm import CREATOR, Clock, draft
from test_prm_draft_context import CODE_B, GATED, deal_of, held_draft, her_job, make, sample_block, shown_of
from test_prm_platform_flow import SAMPLE, approve, block, mac_runs, ordered, record

from platform_fakes import JST_ORDER
from src.prm import draft_context
from src.prm.service import SECOND_ORDER_DAYS, SETTLE_SEC, event_out


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 9, 14, 30, 0, 0, 0, -1)))


@pytest.fixture
def bot(clock, tmp_path):
    b = make(clock, tmp_path)                       # (PRM_DRAFT_CONTEXT on, 小美 and another creator registered)
    yield b
    b.prm.stop()
    b.relay.stop()


# ── A-L3: a new preview and its marks in one step ──────────────────────────

def test_a_new_preview_and_its_photo_marks_are_written_in_one_step(bot):
    """The new preview's platform JSON was written first and its marks merged after: an approval landing in between
    paired the new ready preview with the old marks.  The old preview matched every hat by photo; the new one (ready)
    lets CODE_B go only by a 「对过图了」 there is no more.  An approval right after the new preview is written sees its
    marks: refused (as after the merge), nothing runs."""
    packet = held_draft(bot, held=(), verdict="ready")
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    job = bot.relay.next_job(0, ("prm",))
    store, real, answers = bot.prm.store, bot.prm.store.set_platform, []

    def written(packet_id, platform, statuses, **kw):
        done = real(packet_id, platform, statuses, **kw)
        if not answers and packet_id == packet["id"] and statuses == ("pending",):
            answers.append(bot.prm.decide(packet["id"], "execute"))     # (an approval in the window)
        return done

    store.set_platform = written
    bot.finish(job["id"], text=json.dumps({**sample_block(previewed_at=2.0), "draft_context": GATED,
                                           "photo_confirmed": [CODE_B]}, ensure_ascii=False))
    assert answers == [{"ok": False, "error": draft_context.withdrawn_reason([CODE_B])}]
    after = store.packet(packet["id"])
    assert after["status"] == "pending" and after["meta"]["photo_confirmed"] == [CODE_B]
    assert after["platform"]["preview"]["previewed_at"] == 2.0 and not after["platform"].get("previewing")


# ── A-L4: a held hat nobody confirmed ──────────────────────────────────────

def test_a_sample_order_whose_preview_held_a_hat_nobody_confirmed_never_runs(bot):
    """``photo_held``: the hats the Mac's check held for photos in the draft's last preview (its verdict is not ready
    then; ``photo_confirmed``, the ones it let go only by a confirmation, is ``withdrawn``'s).  A preview that says ready
    all the same with such a hat no operator confirmed since is never approved (the console's button off, why), the
    switch on or off; once 「对过图了」 is there for it, it may run."""
    packet = held_draft(bot, verdict="ready")
    reason = f"{CODE_B} 还扣着（没对过图），不能执行：{draft_context.CONFIRM_HOW}，再点「重新预览」"
    assert bot.prm.decide(packet["id"], "execute") == {"ok": False, "error": reason}
    shown = shown_of(bot, packet["id"])
    assert shown["platform"]["can_execute"] is False and reason in shown["platform"]["problems"]
    bot.prm._draft_context = False
    assert bot.prm.decide(packet["id"], "execute") == {"ok": False, "error": reason}
    bot.prm._draft_context = True
    deal = deal_of(bot)
    assert bot.prm.deal_action("photo_confirm", {"deal_id": deal["id"], "packet": packet["id"], "code": CODE_B})["ok"]
    assert shown_of(bot, packet["id"])["platform"]["can_execute"] is True
    out = bot.prm.decide(packet["id"], "execute")
    assert out["ok"] and out["status"] == "executing", out


# ── A-L5: a second sample order for one deal ───────────────────────────────

def two_lanes(b):
    """She asks for samples in the group linked by hand and in her private chat: a sample order drafted in each (the
    lanes of a creator with a group: neither puts the other out of date)."""
    linked(b, member=1)
    packets = []
    for chat, msg in ((GROUP, gmsg(b, "那寄两顶给我吧")), (CREATOR, b.msg("私聊说一下：寄到这个地址"))):
        b.clock.now += 30
        b.prm.handle(msg)
        job = job_of(b, chat)
        b.clock.now += SETTLE_SEC + 1
        assert b.relay.next_job(0, ("prm",))["id"] == job["id"]
        b.finish(job["id"], draft(message_kind="money", reply_draft="寄样单建好啦", platform=block(SAMPLE, run={"steps": ["create"]})))
        packets.append(b.prm.store.packet_for_job(job["id"]))
    b.prm.store.token_for(packets[0]["party_id"], "phone", "手机", "13900001111", b.clock.now)
    assert [p["status"] for p in packets] == ["pending", "pending"]
    return packets


def alerts(b):
    return {p["id"]: p["error"] for p in b.prm.snapshot()["platform_alerts"]}


def test_two_sample_orders_waiting_in_two_lanes_are_never_both_approved_unasked(clock, tmp_path):
    """Probe A5 / review A-L5: a sample order waiting in the group linked by hand and one in her private chat could
    both be approved and both made.  The first approval is refused while the other waits; the other is red on
    「待审批」 with 「我已处理」 (its ``recheck``), which lets the first run; the other's approval is then refused while the
    first runs, until 「我已处理」 on it: two orders only when a person said so twice."""
    b = setup(clock, tmp_path)
    try:
        group, private = two_lanes(b)
        out = b.prm.decide(private["id"], "execute")
        assert out["ok"] is False and out["error"].startswith(f"这位达人还有一份寄样单 #{group['id']} 在等：")
        assert "先在「待审批」最上面" in out["error"]
        asked = alerts(b)
        assert list(asked) == [group["id"]] and asked[group["id"]].startswith(f"#{private['id']} 要给她再建一张寄样单")
        assert b.prm.store.packet(group["id"])["error"] == ""                # (its own error: none, it is not red)
        assert b.prm.decide(private["id"], "execute")["ok"] is False           # (not said yet)
        assert b.prm.decide(group["id"], "ack") == {"ok": True, "released": private["id"]}
        assert alerts(b) == {} and b.prm.decide(group["id"], "ack")["ok"] is False
        first = approve(b, private)
        out = b.prm.decide(group["id"], "execute")
        assert out["ok"] is False and out["error"].startswith(f"这位达人有一张寄样单 #{private['id']} 正在建：")
        assert list(alerts(b)) == [private["id"]]
        assert b.prm.decide(private["id"], "ack") == {"ok": True, "released": group["id"]}
        second = approve(b, group)                                             # a person said so twice: two orders
        assert first["id"] != second["id"]
        names = [event_out(e)["name"] for e in b.prm.store.events(group["deal_id"], kinds=("platform_recheck",))]
        assert f"#{private['id']} 要给她再建一张寄样单（#{group['id']} 已经有了）：先问过运营" in names
    finally:
        b.prm.stop()
        b.relay.stop()


@pytest.mark.parametrize("days, asks", [(SECOND_ORDER_DAYS - 1, True), (SECOND_ORDER_DAYS + 1, False)])
def test_a_sample_order_verified_lately_asks_once_before_another_and_a_resend_stays_possible(clock, tmp_path, days, asks):
    """One verified within SECOND_ORDER_DAYS: the next one of the deal is refused once; 「我已处理」 on the verified one
    (sent: its own state and error stay as they were) lets it run, a 补寄.  Older: as before."""
    from test_prm import Bot

    b = Bot(clock, tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    try:
        first = ordered(b, clock)
        mac_runs(b, approve(b, first), record(sample_order_no=JST_ORDER))
        b.prm.send_approved()
        assert b.prm.store.packet(first["id"])["status"] == "sent"
        clock.now += days * 86400
        second = ordered(b, clock, reply="补寄的寄样单建好啦")
        out = b.prm.decide(second["id"], "execute")
        if not asks:
            assert out["ok"], out
            return
        assert out == {"ok": False, "error": (
            f"这位达人 {SECOND_ORDER_DAYS} 天内已经建过寄样单 #{first['id']}：确定要再建一张，先在「待审批」最上面 "
            f"#{first['id']} 的红色提醒上点「我已处理」，再回来批准这一份；不用再建，点这一份的「不发」")}
        assert list(alerts(b)) == [first["id"]]
        assert b.prm.decide(first["id"], "ack")["ok"]
        sent = b.prm.store.packet(first["id"])
        assert (sent["status"], sent["error"]) == ("sent", "") and alerts(b) == {}
        assert b.prm.decide(second["id"], "execute")["ok"]
    finally:
        b.prm.stop()
        b.relay.stop()
