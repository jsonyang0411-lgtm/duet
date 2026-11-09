"""Platform actions on the bot's side (src/prm/service.py): a draft that carries one waits for a
person whatever its kind's gate; 批准并执行 queues one execution on the Mac; only a read-back
VERIFIED_SUCCESS sends the reply (the link filled in); everything else, a Mac that never takes
the job or goes silent, ends red without a word to the creator."""

import json
import time

import pytest

from test_prm import CREATOR, Bot, Clock, draft

from platform_fakes import (
    DY_ITEM, DY_ITEM2, DY_ITEM3, DY_LINK, DY_SKU, DY_SKU2, DY_SKU3, DY_UID, JST_ORDER, TB_ITEM, XHS_BUYER, XHS_ITEM,
)
from src.prm import platforms
from src.prm.result import DraftError, parse_draft
from src.prm.service import (APPROVED_EXPIRE_SEC, PLATFORM_LOST_GRACE_SEC, RESTART_HINT, RESTART_HINT_CREATED,
                             SETTLE_SEC, apply_deal_action, event_out)
from src.relay.service import LOST_AFTER_SEC

DESIGNATED = {"action": "dy.designated_commission", "item_id": DY_ITEM, "creator_platform_id": DY_UID,
              "commission_rate": 20}
CHANNEL = {"action": "dy.channel_goods", "item_id": DY_ITEM, "creator_platform_id": DY_UID, "price": "15"}
SAMPLE = {"action": "jst.sample_order", "shop": "dy", "item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}],
          "creator_platform": "dy", "creator_uid": DY_UID,
          "receiver": {"name": "小美", "phone": "〈手机1〉", "province": "上海市", "city": "上海市", "district": "浦东新区",
                       "address": "张江路100号3栋1201"}}


def block(raw=DESIGNATED, verdict="ready", run=None, **extra):
    """The Mac's preview of an action, as the worker hands it back."""
    action = platforms.check_action(raw)
    out = {"version": 1, "action": action, "verdict": verdict, "problems": [] if verdict == "ready" else ["没登录"],
           "facts": [{"label": "将写入", "value": "fixed commission 20%"}],
           "steps": [{"name": "designated", "label": "百应 · 建指定达人佣金策略", "execute": False,
                      "command": "node bin/dy.mjs buyin promotion-strategy-create-designated", "exit_code": 0,
                      "seconds": 2.5, "verdict": verdict, "outcome": "", "problems": [], "facts": [], "raw": "{}"}],
           "run": run if run is not None else {"steps": ["create", "bind"] if raw["action"] == "dy.channel_goods"
                                               else ["designated"]},
           "catalog": {"synced_at": 1.0, "title": "测试主品"}, "previewed_at": 2.0}
    out.update(extra)
    return out


def record(outcome="VERIFIED_SUCCESS", key="", **created):
    rec = platforms.record(outcome, [] if outcome == "VERIFIED_SUCCESS" else ["读回没证实"])
    rec.update(key=key, created=created)
    return rec


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bot(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def drafted(b, preview=None, reply="专属佣金 20% 设好啦，明天 0 点生效～", kind="money", **more):
    """The creator agrees, the Mac drafts with a platform action and previews it."""
    b.prm.handle(b.msg("好的，那就按 20% 来"))
    job = b.worker_takes()
    d = draft(message_kind=kind, reply_draft=reply, **more)
    if preview is not False:
        d["platform"] = preview if preview is not None else block()
    b.finish(job["id"], d)
    return b.prm.store.packet_for_job(job["id"])


def approve(b, packet, text=None):
    out = b.prm.decide(packet["id"], "execute", text)
    assert out["ok"], out
    return b.relay.store.get(out["job"])


def mac_runs(b, job_row, rec=None, ok=True, error=""):
    """The worker takes the execution job and answers."""
    job = b.relay.next_job(0, ("prm",))
    assert job is not None and job["id"] == job_row["id"]
    meta = job["meta"]
    if rec is not None:
        rec = {**rec, "key": rec.get("key") or meta["key"]}
    body = {"ok": ok, "text": json.dumps(rec, ensure_ascii=False) if rec is not None else "", "duration_s": 9,
            **({"error": error} if error else {})}
    assert b.relay.finish(job["id"], body)
    b.relay.tick()
    return job


def snap(b):
    return b.prm.snapshot()


# ── a platform draft waits for a person ───────────────────────────────────

def test_a_draft_with_a_platform_action_always_waits_for_a_person(bot, clock):
    bot.prm.store.set_gate("other", "auto", clock.now)          # even a kind that goes out on its own
    packet = drafted(bot, kind="other")
    assert packet["status"] == "pending" and packet["route"] == "review"
    assert packet["tool"] == "dy.designated_commission"
    assert packet["platform"]["action"]["commission_rate"] == "20" and packet["platform"]["preview"]["verdict"] == "ready"
    [shown] = snap(bot)["pending"]
    pf = shown["platform"]
    assert pf["can_execute"] and pf["label"] == "抖音百应 · 指定达人专属佣金" and pf["verdict_label"].startswith("预览通过")
    assert {"label": "佣金率（%）", "value": "20%"} in pf["params"] and pf["params"][0] == {"label": "商品", "value": "测试主品"}
    assert bot.texts == []


def test_send_and_edit_are_refused_for_a_platform_draft(bot):
    packet = drafted(bot)
    for action in ("send", "edit"):
        out = bot.prm.decide(packet["id"], action, "改一下")
        assert not out["ok"] and "批准并执行" in out["error"]
    assert bot.prm.store.packet(packet["id"])["status"] == "pending"


def test_approve_queues_one_execution_with_its_own_key(bot):
    packet = drafted(bot)
    job = approve(bot, packet)
    meta = json.loads(job["meta"])
    assert meta["op"] == "platform_execute" and meta["packet_id"] == packet["id"]
    assert meta["action"] == packet["platform"]["action"] and meta["run"] == {"steps": ["designated"]}
    assert len(meta["key"]) == 32 and job["kind"] == "prm" and job["input_closed"] == 1 and job["text"] == ""
    executing = bot.prm.store.packet(packet["id"])
    assert executing["status"] == "executing" and executing["platform"]["key"] == meta["key"]
    assert executing["platform"]["job_id"] == job["id"]
    again = bot.prm.decide(packet["id"], "execute")
    assert not again["ok"] and len(bot.relay.store.list_jobs()) == 2          # the draft job and one execution
    assert not bot.prm.decide(packet["id"], "drop")["ok"]
    assert snap(bot)["pending"][0]["status"] == "executing"
    assert any(e["kind"] == "platform_execute" for e in bot.prm.store.events(packet["deal_id"]))


def test_an_execution_that_cannot_be_queued_leaves_the_draft_waiting_to_be_approved_again(bot):
    """批准并执行 whose relay job could not be queued: nothing ran, so the draft waits as it was (no key, no decision)
    and can be approved again (it no longer goes red as 「没能排进 Mac 的任务队列」)."""
    from unittest.mock import patch

    from src.prm.service import QUEUE_FAILED

    packet = drafted(bot)
    with patch.object(bot.relay, "submit", side_effect=RuntimeError("队列坏了")):
        assert bot.prm.decide(packet["id"], "execute") == {"ok": False, "error": QUEUE_FAILED, "code": "queue_failed"}
    waiting = bot.prm.store.packet(packet["id"])
    assert (waiting["status"], waiting["decided_at"], waiting["final_text"]) == ("pending", None, None)
    assert waiting["platform"] == packet["platform"]
    job = approve(bot, packet)
    assert json.loads(job["meta"])["op"] == "platform_execute"


def test_a_re_preview_landing_between_the_checks_and_the_move_is_never_run_unseen(bot):
    """The move to executing compares the platform data the checks read: a 重新预览 started in between is refused
    (the draft waits); a stamp that is not its preview's (微信里审批草稿) too."""
    from unittest.mock import patch

    from src.prm.service import PREVIEW_CHANGED

    packet = drafted(bot)
    assert bot.prm.decide(packet["id"], "execute", stamp="f" * 16)["code"] == "preview_changed"
    real = bot.prm.store.move

    def move(packet_id, from_states, to_state, **fields):
        if to_state == "executing":
            data = bot.prm.store.packet(packet_id)["platform"]
            bot.prm.store.set_platform(packet_id, {**data, "previewing": 1.0}, ("pending",))
        return real(packet_id, from_states, to_state, **fields)

    with patch.object(bot.prm.store, "move", move):
        assert bot.prm.decide(packet["id"], "execute") == {"ok": False, "error": PREVIEW_CHANGED,
                                                          "code": "preview_changed"}
    assert bot.prm.store.packet(packet["id"])["status"] == "pending"
    assert [j for j in bot.relay.store.list_jobs(10) if '"platform_execute"' in j["meta"]] == []


def test_verified_success_sends_the_reply_with_the_link(bot):
    packet = drafted(bot, block(CHANNEL), reply="专属链接给你啦：{链接} 审核通过后就能用～")
    job = approve(bot, packet)
    mac_runs(bot, job, record(link=DY_LINK, channel_id="7200000000000001"))
    done = bot.prm.store.packet(packet["id"])
    assert done["status"] == "approved" and DY_LINK in done["final_text"] and "{链接}" not in done["final_text"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, f"专属链接给你啦：{DY_LINK} 审核通过后就能用～")]
    sent = bot.prm.store.packet(packet["id"])
    assert sent["status"] == "sent" and sent["platform"]["execution"]["created"]["link"] == DY_LINK
    [shown] = [p for p in snap(bot)["recent"] if p["id"] == packet["id"]]
    assert shown["platform"]["execution"]["outcome"] == "VERIFIED_SUCCESS"
    assert any(e["kind"] == "platform_result" and e["detail"]["link"] == DY_LINK
               for e in bot.prm.store.events(packet["deal_id"]))


def test_a_link_action_without_the_placeholder_gets_the_link_appended(bot):
    packet = drafted(bot, block(CHANNEL), reply="链接发你了")
    mac_runs(bot, approve(bot, packet), record(link=DY_LINK))
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, f"链接发你了\n{DY_LINK}")]


@pytest.mark.parametrize("outcome", ["ACCEPTED_UNVERIFIED", "INDETERMINATE", "FAILED", "STATE_CONFLICT", "PARTIAL",
                                     "ALREADY_RUN", "NOT_RUN", "UNKNOWN", "OBSERVED_DESIRED_STATE"])
def test_anything_but_verified_is_red_and_nothing_is_sent(bot, outcome):
    packet = drafted(bot)
    mac_runs(bot, approve(bot, packet), record(outcome))
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == f"platform_{outcome.lower()}"
    assert outcome in failed["error"] and "没有发微信" in failed["error"]
    bot.prm.tick()
    assert bot.texts == []
    alerts = snap(bot)["platform_alerts"]
    assert [a["id"] for a in alerts] == [packet["id"]] and alerts[0]["platform"]["execution"]["outcome"] == outcome
    assert f"#{packet['id']}" in bot.prm.store.deal(packet["deal_id"])["attention"]
    assert bot.prm.decide(packet["id"], "ack")["ok"]
    assert snap(bot)["platform_alerts"] == [] and not bot.prm.decide(packet["id"], "ack")["ok"]


def test_a_mac_that_could_not_run_it_says_so(bot):
    packet = drafted(bot)
    mac_runs(bot, approve(bot, packet), None, ok=False, error="这台 Mac 的 worker 没有平台模块")
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == "platform_not_run" and "没有平台模块" in failed["error"]


def test_a_result_that_cannot_be_read_is_unknown(bot):
    packet = drafted(bot)
    job = approve(bot, packet)
    bot.relay.next_job(0, ("prm",))
    bot.relay.finish(job["id"], {"ok": True, "text": "not json"})
    bot.relay.tick()
    assert bot.prm.store.packet(packet["id"])["outcome"] == "platform_unknown"


def test_a_lost_mac_is_waited_for_and_its_late_answer_still_counts(bot, clock):
    packet = drafted(bot)
    job = approve(bot, packet)
    taken = bot.relay.next_job(0, ("prm",))
    clock.now += LOST_AFTER_SEC + 1
    bot.relay.tick()                                          # lost: the platform write may be running
    waiting = bot.prm.store.packet(packet["id"])
    assert waiting["status"] == "executing" and waiting["platform"]["lost_at"] == clock.now
    bot.prm.sweep_executing()
    assert bot.prm.store.packet(packet["id"])["status"] == "executing"
    assert bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(record(key=taken["meta"]["key"]))})
    bot.relay.tick()
    assert bot.prm.store.packet(packet["id"])["status"] == "approved"


def test_a_mac_silent_too_long_is_given_up_on_and_a_late_answer_is_only_recorded(bot, clock):
    packet = drafted(bot)
    job = approve(bot, packet)
    taken = bot.relay.next_job(0, ("prm",))
    clock.now += LOST_AFTER_SEC + 1
    bot.relay.tick()
    clock.now += PLATFORM_LOST_GRACE_SEC + 1
    bot.prm.tick()
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == "platform_unknown" and "失联" in failed["error"]
    assert bot.prm.decide(packet["id"], "ack")["ok"]
    assert bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(record(key=taken["meta"]["key"]))})
    bot.relay.tick()
    late = bot.prm.store.packet(packet["id"])
    assert late["status"] == "failed" and late["platform"]["execution"]["late"] is True
    assert late["platform"]["acked_at"] is None and [a["id"] for a in snap(bot)["platform_alerts"]] == [packet["id"]]
    assert "晚到" in bot.prm.store.deal(packet["deal_id"])["attention"]
    bot.prm.tick()
    assert bot.texts == []


def test_a_mac_back_from_silence_keeps_its_execution(bot, clock):
    packet = drafted(bot)
    job = approve(bot, packet)
    bot.relay.next_job(0, ("prm",))
    clock.now += LOST_AFTER_SEC + 1
    bot.relay.tick()
    clock.now += 60
    bot.relay.heartbeat(job["id"])                            # the worker is back
    bot.prm.sweep_executing()
    assert bot.prm.store.packet(packet["id"])["platform"]["lost_at"] is None
    clock.now += PLATFORM_LOST_GRACE_SEC + 1
    bot.relay.heartbeat(job["id"])
    bot.prm.sweep_executing()
    assert bot.prm.store.packet(packet["id"])["status"] == "executing"


def test_an_execution_the_mac_never_takes_is_cancelled_after_30_minutes(bot, clock):
    packet = drafted(bot)
    job = approve(bot, packet)
    clock.now += APPROVED_EXPIRE_SEC - 5
    bot.prm.sweep_executing()
    assert bot.prm.store.packet(packet["id"])["status"] == "executing"
    clock.now += 10
    bot.prm.sweep_executing()
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == "platform_not_run" and "平台没有动" in failed["error"]
    assert bot.relay.store.get(job["id"])["status"] == "cancelled"
    assert bot.relay.next_job(0, ("prm",)) is None


def test_an_execution_whose_job_is_gone_is_unknown_after_a_minute(bot, clock):
    """sweep_executing: the execution's relay job cannot be found any more (the relay's database was replaced or
    cleared, say): the Mac may or may not have run it.  Within a minute of the approval it is waited for; after
    that it ends UNKNOWN (「不知道执行了没有」), red until 我已处理, and nothing goes to the creator."""
    packet = drafted(bot)
    job = approve(bot, packet)
    with bot.relay.store._lock:
        bot.relay.store._conn.execute("DELETE FROM relay_jobs WHERE id = ?", (job["id"],))
        bot.relay.store._conn.commit()
    clock.now += 59
    assert bot.prm.sweep_executing() == 0 and bot.prm.store.packet(packet["id"])["status"] == "executing"
    clock.now += 2
    assert bot.prm.sweep_executing() == 1
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and failed["outcome"] == "platform_unknown"
    assert "找不到执行任务了" in failed["platform"]["execution"]["problems"][0] and "UNKNOWN" in failed["error"]
    assert [a["id"] for a in snap(bot)["platform_alerts"]] == [packet["id"]]
    bot.prm.tick()
    assert bot.texts == []


def test_cancelled_while_running_is_never_sent(bot):
    packet = drafted(bot)
    job = approve(bot, packet)
    taken = bot.relay.next_job(0, ("prm",))
    assert bot.relay.cancel_from_console(job["id"]) == "requested"
    assert bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(record(key=taken["meta"]["key"]))})
    bot.relay.tick()
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and "中途被取消" in failed["error"]
    bot.prm.tick()
    assert bot.texts == []


def test_a_new_message_outdates_a_waiting_platform_draft_but_not_one_executing(bot, clock):
    first = drafted(bot)
    bot.prm.handle(bot.msg("等等，我再想想"))
    assert bot.prm.store.packet(first["id"])["status"] == "superseded"
    assert not bot.prm.decide(first["id"], "execute")["ok"]
    clock.now += SETTLE_SEC + 1
    second_job = bot.relay.next_job(0, ("prm",))
    bot.finish(second_job["id"], {**draft(message_kind="money", reply_draft="好的～"), "platform": block()})
    second = bot.prm.store.packet_for_job(second_job["id"])
    job = approve(bot, second)
    bot.prm.handle(bot.msg("对了，什么时候生效"))             # while it runs on the Mac
    assert bot.prm.store.packet(second["id"])["status"] == "executing"
    mac_runs(bot, job, record())
    assert bot.prm.store.packet(second["id"])["status"] == "approved"


def test_a_platform_draft_answered_after_a_newer_message_is_out_of_date(bot):
    bot.prm.handle(bot.msg("好的，那就按 20% 来"))
    job = bot.worker_takes()
    bot.prm.handle(bot.msg("还有一个问题"))                    # a newer draft job comes
    bot.finish(job["id"], {**draft(message_kind="money"), "platform": block()})
    assert bot.prm.store.packet_for_job(job["id"])["status"] == "superseded"


def test_a_job_the_mac_does_without_the_model_does_not_outdate_a_draft(bot):
    bot.prm.handle(bot.msg("我收到样品啦"))
    job = bot.worker_takes()                                  # the draft job runs on the Mac
    bot.prm.purge(bot.prm.store.deal(1), bot.clock.now)       # meanwhile a newer job of this chat: a clean-up
    assert json.loads(bot.relay.store.list_jobs(1)[0]["meta"])["op"] == "purge"
    bot.finish(job["id"], draft(message_kind="greeting", reply_draft="太好啦～"))
    assert bot.prm.store.packet_for_job(job["id"])["status"] == "pending"


# ── the preview ───────────────────────────────────────────────────────────

def test_a_preview_that_did_not_pass_cannot_be_executed_and_can_be_redone(bot):
    packet = drafted(bot, block(verdict="login"))
    pf = snap(bot)["pending"][0]["platform"]
    assert not pf["can_execute"] and pf["verdict"] == "login" and pf["problems"] == ["没登录"]
    out = bot.prm.decide(packet["id"], "execute")
    assert not out["ok"] and "没登录" in out["error"]
    assert "平台动作预览没通过" in bot.prm.store.deal(packet["deal_id"])["attention"]
    asked = bot.prm.decide(packet["id"], "repreview")
    assert asked["ok"] and not bot.prm.decide(packet["id"], "repreview")["ok"]
    assert snap(bot)["pending"][0]["platform"]["previewing"] is True
    assert not bot.prm.decide(packet["id"], "execute")["ok"]
    job = bot.relay.next_job(0, ("prm",))
    assert job["meta"]["op"] == "platform_preview" and job["meta"]["action"] == packet["platform"]["action"]
    bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(block(), ensure_ascii=False)})
    bot.relay.tick()
    pf = snap(bot)["pending"][0]["platform"]
    assert pf["verdict"] == "ready" and pf["can_execute"] and not pf["previewing"]
    assert approve(bot, packet)


def test_a_new_preview_that_fails_keeps_the_old_one(bot):
    packet = drafted(bot, block(verdict="login"))
    bot.prm.decide(packet["id"], "repreview")
    job = bot.relay.next_job(0, ("prm",))
    bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "Chrome-Debug 没开"})
    bot.relay.tick()
    pf = snap(bot)["pending"][0]["platform"]
    assert not pf["previewing"] and pf["verdict"] == "login" and "Chrome-Debug 没开" in pf["problems"][0]


def test_an_action_the_mac_did_not_preview_cannot_run(bot):
    packet = drafted(bot, False, platform_action=DESIGNATED)
    assert packet["tool"] == "dy.designated_commission"
    pf = snap(bot)["pending"][0]["platform"]
    assert pf["verdict"] == "unavailable" and not pf["can_execute"]
    assert not bot.prm.decide(packet["id"], "execute")["ok"]


TWELVE_CROWDS = {"action": "tb.campaign", "item_id": TB_ITEM, "bid_target": "display_pay", "day_budget": 50,
                 "crowd_ids": [str(90000400001 + i) for i in range(12)]}
TWENTY_FIVE_PRICES = {"action": "xhs.channel_item", "item_id": XHS_ITEM,
                      "sku_prices": [{"sku_id": f"{i:024d}", "price": "79"} for i in range(1, 26)]}


@pytest.mark.parametrize("raw, mac_limits, run, word", [
    (TWELVE_CROWDS, {"max_crowds": 20}, {"steps": ["create"], "name": "达人合作1_10271000"}, "人群包"),
    (TWENTY_FIVE_PRICES, {"max_sku_prices": 30}, {"steps": ["create"]}, "SKU 专属价"),
], ids=["12-crowds", "25-sku-prices"])
def test_a_preview_past_the_bots_limits_keeps_the_reply_without_the_action(bot, clock, raw, mac_limits, run, word):
    """A Mac whose config.json raised max_crowds / max_sku_prices past FORM_LIMITS: the bot does not take
    its action, but the reply is no longer lost with it (「草稿格式不对」): a plain draft for a person."""
    bot.prm.store.set_gate("other", "auto", clock.now)                      # even a kind that goes out on its own
    action = platforms.check_action(raw, limits=mac_limits)                 # what that Mac previewed, ready
    packet = drafted(bot, {**block(), "action": action, "run": run}, reply="计划建好啦～", kind="other")
    assert packet is not None and packet["status"] == "pending" and packet["route"] == "review"
    assert packet["tool"] == "message.send" and packet["needs_attention"] and word in packet["reason"]
    attention = bot.prm.store.deal(packet["deal_id"])["attention"]
    assert "没过机器人这边的检查" in attention and word in attention and "格式不对" not in attention
    assert not bot.prm.decide(packet["id"], "execute")["ok"]                  # nothing can run
    assert [j for j in bot.relay.store.list_jobs(10) if '"platform_execute"' in j["meta"]] == []
    assert bot.texts == [] and bot.prm.decide(packet["id"], "edit", "好的，我确认一下再回你～")["ok"]


def test_a_new_preview_the_bot_does_not_take_keeps_the_old_one_and_says_why(bot):
    packet = drafted(bot, block(verdict="login"))
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    job = bot.relay.next_job(0, ("prm",))
    over = {**block(), "action": {**platforms.check_action(DESIGNATED), "commission_rate": "26"}}
    bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(over, ensure_ascii=False)})
    bot.relay.tick()
    pf = snap(bot)["pending"][0]["platform"]
    assert pf["verdict"] == "login" and not pf["previewing"] and not pf["can_execute"]
    assert "没过机器人这边的检查" in pf["problems"][0] and "上限 25%" in pf["problems"][0]


def test_a_preview_block_that_is_not_one_is_no_draft(bot):
    packet = drafted(bot, {"verdict": "great"})
    assert packet is None
    assert "格式不对" in bot.prm.store.deal(1)["attention"]
    with pytest.raises(DraftError):
        parse_draft(json.dumps({**draft(), "platform": block(run={"steps": ["bind"]})}))


# ── the reply is checked before anything runs ─────────────────────────────

@pytest.mark.parametrize("text, why", [
    ("链接：{链接}", "用不了的占位符"),
    ("你好 {姓名}", "用不了的占位符"),
    ("打 〈手机9〉", "占位符"),
])
def test_the_reply_is_checked_before_the_platform_is_touched(bot, text, why):
    packet = drafted(bot)
    out = bot.prm.decide(packet["id"], "execute", text)
    assert not out["ok"] and why in out["error"]
    assert bot.prm.store.packet(packet["id"])["status"] == "pending" and len(bot.relay.store.list_jobs()) == 1


def test_an_empty_reply_is_refused_except_for_a_commission(bot):
    channel = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    out = bot.prm.decide(channel["id"], "execute", "")
    assert not out["ok"] and "回复是空的" in out["error"]
    commission = drafted(bot, reply="")              # several products: only the last one says something
    assert bot.prm.decide(commission["id"], "execute", "")["ok"]


def test_nothing_runs_when_the_reply_could_not_go_out(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm._send = None
    b.prm.register(CREATOR, "小美")
    packet = drafted(b)
    out = b.prm.decide(packet["id"], "execute")
    assert not out["ok"] and "连着微信" in out["error"] and len(b.relay.store.list_jobs()) == 1
    b.prm.stop()
    b.relay.stop()


def test_an_edited_reply_is_what_goes_out(bot):
    packet = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    job = approve(bot, packet, "宝子，你的专属链接：{链接}")
    mac_runs(bot, job, record(link=DY_LINK))
    bot.prm.send_approved()
    sent = bot.prm.store.packet(packet["id"])
    assert sent["edited"] and bot.texts == [(CREATOR, f"宝子，你的专属链接：{DY_LINK}")]


# ── the gates and the audit trail ─────────────────────────────────────────

def test_platform_replies_never_earn_their_kind_the_auto_gate(bot, clock):
    for item in (DY_ITEM, DY_ITEM2, DY_ITEM3):                  # one product each (never the same one twice)
        packet = drafted(bot, block({**DESIGNATED, "item_id": item}), kind="other")
        mac_runs(bot, approve(bot, packet), record())
        bot.prm.send_approved()
        for job in bot.relay.store.list_jobs(20):                  # the next 专属佣金 step: not this test
            if "system_note" in job["meta"]:
                bot.relay.store.cancel_if_pending(job["id"], clock.now)
        clock.now += 60
    assert bot.prm.store.reviewed("other", 0, 20) == []
    assert bot.prm.store.gate("other")["route"] == "review"


def test_an_answer_for_another_approval_is_ignored(bot):
    packet = drafted(bot)
    job = approve(bot, packet)
    bot.relay.next_job(0, ("prm",))
    bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(record(key="f" * 32))})
    with bot.relay.store._lock:                               # the job's own key, rewritten: an answer to another
        bot.relay.store._conn.execute("UPDATE relay_jobs SET meta = ? WHERE id = ?",
                                      (json.dumps({**json.loads(job["meta"]), "key": "f" * 32}), job["id"]))
        bot.relay.store._conn.commit()
    bot.relay.tick()
    assert bot.prm.store.packet(packet["id"])["status"] == "executing"


def test_a_placeholder_the_platform_gave_no_value_for_keeps_the_reply_back(bot):
    raw = {"action": "xhs.direct_plan", "item_id": XHS_ITEM, "creator_platform_id": XHS_BUYER, "commission_rate": 15}
    plan = {"steps": ["create"], "buyer_id": XHS_BUYER, "name": "达人合作1-1026101500"}
    packet = drafted(bot, block(raw, run=plan), reply="定向计划「{计划名}」开好啦，编号 {计划ID}")
    mac_runs(bot, approve(bot, packet), record(plan_name="达人合作1-1026101500"))      # no group id came back
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and "{计划ID}" in failed["error"] and "已经核验成功" in failed["error"]
    bot.prm.tick()
    assert bot.texts == []
    same = drafted(bot, block(raw, run=plan), reply="定向计划「{计划名}」开好啦，编号 {计划ID}")
    out = bot.prm.decide(same["id"], "execute")                 # it is open on the platform: not a second plan
    assert not out["ok"] and f"#{failed['id']}" in out["error"] and "开过了" in out["error"]
    other_item = {**raw, "item_id": "64f1a2b3c4d5e6f7a8b9c0d3"}
    other = drafted(bot, block(other_item, run=plan), reply="定向计划「{计划名}」开好啦，编号 {计划ID}")
    mac_runs(bot, approve(bot, other), record(plan_name="达人合作1-1026101500", group_id="6a0000000000000000000a01"))
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, "定向计划「达人合作1-1026101500」开好啦，编号 6a0000000000000000000a01")]


def test_a_direct_plan_draft_carries_the_previewed_buyer(bot):
    raw = {"action": "xhs.direct_plan", "item_id": XHS_ITEM, "creator_platform_id": "小美的号", "commission_rate": 15}
    packet = drafted(bot, block(raw, run={"steps": ["create"], "buyer_id": XHS_BUYER, "name": "达人合作1-1026101500"}))
    job = approve(bot, packet)
    assert json.loads(job["meta"])["run"] == {"steps": ["create"], "buyer_id": XHS_BUYER, "name": "达人合作1-1026101500"}


def test_parse_draft_reads_the_platform_block():
    d = parse_draft(json.dumps({**draft(), "platform": block()}, ensure_ascii=False))
    assert d.platform["verdict"] == "ready" and d.platform["action"]["item_id"] == DY_ITEM
    assert parse_draft(json.dumps(draft())).platform is None
    unpreviewed = parse_draft(json.dumps({**draft(), "platform_action": DESIGNATED}))
    assert unpreviewed.platform["verdict"] == "unavailable"
    over = {**block(), "action": {**platforms.check_action(DESIGNATED), "commission_rate": "26"}}
    refused = parse_draft(json.dumps({**draft(), "platform": over}, ensure_ascii=False))
    assert refused.platform is None and "上限 25%" in refused.platform_refused and refused.reply_draft
    assert parse_draft(json.dumps(draft())).platform_refused == ""


# ── 聚水潭 sample orders: the number goes to the Mac only ──────────────────

def test_a_sample_order_goes_to_the_mac_with_the_number_and_stays_masked_here(bot, clock):
    packet = drafted(bot, preview=block(SAMPLE, run={"steps": ["create"]}), reply="寄样单建好啦，单号 {寄样单号}～")
    assert bot.prm.store.token_for(packet["party_id"], "phone", "手机", "13900001111", clock.now) == "〈手机1〉"
    [shown] = snap(bot)["pending"]
    assert {"label": "收货信息", "value": "小美 〈手机1〉 上海市上海市浦东新区 张江路100号3栋1201"} in shown["platform"]["params"]
    job = approve(bot, packet)
    meta = json.loads(job["meta"])
    assert meta["action"]["receiver"]["phone"] == "13900001111"             # for the Mac that sends the order
    stored = bot.prm.store.packet(packet["id"])["platform"]
    assert stored["action"]["receiver"]["phone"] == "〈手机1〉"              # the packet keeps the placeholder
    assert "13900001111" not in json.dumps(snap(bot), ensure_ascii=False)
    mac_runs(bot, job, record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.texts == [(packet["chat_id"], f"寄样单建好啦，单号 {JST_ORDER}～")]
    assert any(e["kind"] == "platform_result" and e["detail"]["sample_order_no"] == JST_ORDER
               for e in bot.prm.store.events(packet["deal_id"]))


def test_an_unverified_sample_order_names_its_number_in_the_red_mark(bot):
    packet = drafted(bot, preview=block({k: v for k, v in SAMPLE.items() if k != "receiver"}, run={"steps": ["create"]}),
                     reply="寄样单建好啦")
    job = approve(bot, packet)
    rec = record("ACCEPTED_UNVERIFIED", sample_order_no=JST_ORDER)
    rec["problems"] = [f"聚水潭寄样单 {JST_ORDER}；建了 {JST_ORDER}，但读回核对对不上：快递读回是 顺丰陆运到付"]
    mac_runs(bot, job, rec)
    failed = bot.prm.store.packet(packet["id"])
    assert failed["status"] == "failed" and JST_ORDER in failed["error"] and bot.texts == []


def test_a_sample_order_whose_placeholder_has_no_number_is_not_approved(bot):
    packet = drafted(bot, preview=block({**SAMPLE, "receiver": {**SAMPLE["receiver"], "phone": "〈手机9〉"}},
                                        run={"steps": ["create"]}), reply="寄样单建好啦")
    out = bot.prm.decide(packet["id"], "execute", None)
    assert not out["ok"] and "〈手机9〉" in out["error"]
    assert bot.prm.store.packet(packet["id"])["status"] == "pending"
    assert [j for j in bot.relay.store.list_jobs(10) if '"platform_execute"' in j["meta"]] == []


# ── a number the Mac read off a picture (win review F5) ──────────────────

READ_OFF = "13900001111"


def test_a_number_the_mac_read_off_a_picture_becomes_her_placeholder_before_it_is_stored(bot, clock):
    """The creator sends her address as a screenshot: the Mac reads the number off it and writes it out in
    the reply, the slots, the reason and the sample order.  Before anything is stored it becomes her
    〈手机N〉 like a number she types: the console shows it masked (显示完整号码 is written down), the
    Mac executing the order and the creator get the number back."""
    bot.prm.handle(bot.msg("[图片]", msg_type=3))
    job = bot.worker_takes()
    d = draft(message_kind="address_confirm", reply_draft=f"确认一下：小美，手机 {READ_OFF}，寄样单这就建",
              slots={"收件人": "小美", "手机": READ_OFF}, intent=f"寄样，电话 {READ_OFF}",
              needs_attention=True, reason=f"截图里的电话 {READ_OFF}")
    d["platform"] = block({**SAMPLE, "receiver": {**SAMPLE["receiver"], "phone": READ_OFF}}, run={"steps": ["create"]})
    bot.finish(job["id"], d)
    packet = bot.prm.store.packet_for_job(job["id"])
    token = "〈手机1〉"
    assert bot.prm.store.tokens(packet["party_id"]) == {token: ("phone", READ_OFF)}
    stored = json.dumps([packet, bot.prm.store.deal(packet["deal_id"])], ensure_ascii=False)
    shown = json.dumps(snap(bot), ensure_ascii=False)
    assert READ_OFF not in stored and READ_OFF not in shown
    [p] = snap(bot)["pending"]
    assert p["text"] == f"确认一下：小美，手机 {token}，寄样单这就建" and p["slots"]["手机"] == token
    assert {"label": "收货信息", "value": f"小美 {token} 上海市上海市浦东新区 张江路100号3栋1201"} in p["platform"]["params"]
    assert p["legend"] == [{"token": token, "masked": "139****1111"}]
    assert packet["platform"]["preview"]["action"]["receiver"]["phone"] == token
    [party] = snap(bot)["parties"]
    assert party["deal"]["slots"]["手机"] == token and party["deal"]["legend"] == [{"token": token, "masked": "139****1111"}]
    assert token in party["deal"]["attention"]                               # the reason, masked too
    assert bot.prm.reveal(packet["party_id"])["numbers"] == [{"token": token, "value": READ_OFF, "cleared": False}]
    assert [e["detail"]["tokens"] for e in bot.prm.store.events(packet["deal_id"]) if e["kind"] == "reveal"] == [[token]]
    execution = approve(bot, packet)
    assert json.loads(execution["meta"])["action"]["receiver"]["phone"] == READ_OFF   # for the Mac that sends it
    mac_runs(bot, execution, record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, f"确认一下：小美，手机 {READ_OFF}，寄样单这就建")]


def test_the_mac_gets_every_number_of_the_receiver_back_and_an_unknown_one_stops_it():
    from src.prm.service import action_for_mac
    tokens = {"〈手机1〉": ("phone", "13900001111"), "〈手机2〉": ("phone", "13900002222")}
    action = platforms.check_action({**SAMPLE, "receiver": {**SAMPLE["receiver"], "address": "张江路100号，备用〈手机2〉"}})
    out, problem = action_for_mac(action, tokens)
    assert problem == "" and out["receiver"]["phone"] == "13900001111"
    assert out["receiver"]["address"] == "张江路100号，备用13900002222" and action["receiver"]["address"].endswith("〈手机2〉")
    out, problem = action_for_mac(platforms.check_action(
        {**SAMPLE, "receiver": {**SAMPLE["receiver"], "address": "张江路100号 〈手机7〉"}}), tokens)
    assert "〈手机7〉" in problem


# ── a sample order → its 专属佣金 (drafted, then approved like any platform write) ──
# PRM_COMMISSION_AFTER: delivered (the default: when the creator signed for it) or created.

def ordered(b, clock, reply="寄样单建好啦"):
    """A sample order drafted, its receiver's phone known (〈手机1〉)."""
    packet = drafted(b, preview=block(SAMPLE, run={"steps": ["create"]}), reply=reply)
    b.prm.store.token_for(packet["party_id"], "phone", "手机", "13900001111", clock.now)
    return packet


def test_set_to_created_a_sample_order_asks_for_a_draft_of_its_commission_at_once(bot, clock):
    bot.prm._commission_after = "created"
    packet = ordered(bot, clock, reply="寄样单建好啦，单号 {寄样单号}～")
    mac_runs(bot, approve(bot, packet), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    job = bot.worker_takes()
    meta = job["meta"]
    assert job["text"] == "" and meta["deal_id"] == packet["deal_id"] and meta["name"] == "小美"
    note = meta["system_note"]
    assert JST_ORDER in note and DY_ITEM in note and "dy.designated_commission" in note and "抖音的商品" in note
    bot.finish(job["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    commission = bot.prm.store.packet_for_job(job["id"])
    assert commission["status"] == "pending" and commission["tool"] == "dy.designated_commission"   # a person approves
    mac_runs(bot, approve(bot, commission), record())
    bot.prm.send_approved()
    assert bot.texts == [(packet["chat_id"], f"寄样单建好啦，单号 {JST_ORDER}～")]   # the empty reply sends nothing
    nxt = bot.worker_takes()
    assert "专属佣金（20%）已经在平台上开好了" in nxt["meta"]["system_note"] and DY_ITEM in nxt["meta"]["system_note"]
    bot.finish(nxt["id"], draft(message_kind="other", reply_draft="", slots={}))   # all done: nothing more
    assert bot.prm.store.packet_for_job(nxt["id"])["status"] == "no_reply"
    assert bot.worker_takes() is None


def test_a_sample_order_not_verified_asks_for_nothing_more(bot, clock):
    bot.prm._commission_after = "created"
    packet = ordered(bot, clock)
    mac_runs(bot, approve(bot, packet), record("ACCEPTED_UNVERIFIED", sample_order_no=JST_ORDER))
    assert bot.worker_takes() is None


def test_a_sample_order_done_during_a_takeover_leaves_the_commission_to_a_person(bot, clock):
    bot.prm._commission_after = "created"
    packet = ordered(bot, clock)
    job = approve(bot, packet)
    bot.prm.deal_action("mode", {"deal_id": packet["deal_id"], "mode": "human"})
    mac_runs(bot, job, record(sample_order_no=JST_ORDER))
    assert bot.worker_takes() is None
    assert "该开专属佣金了" in bot.prm.store.deal(packet["deal_id"])["attention"]


@pytest.mark.parametrize("after", ["created", "delivered"])
def test_no_commission_step_is_drafted_for_a_creator_the_operators_disabled(bot, clock, after):
    """停用: the creator is off the list; the order made (or signed for) does not start the 专属佣金
    drafts behind the operators' back: the deal says it is theirs to decide."""
    bot.prm._commission_after = after
    order = ordered(bot, clock)
    execution = approve(bot, order)
    bot.prm.set_active(order["party_id"], False)                             # taken off the list meanwhile
    mac_runs(bot, execution, record(sample_order_no=JST_ORDER))
    if after == "delivered":
        assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    assert bot.worker_takes() is None
    assert all("system_note" not in j["meta"] for j in bot.relay.store.list_jobs(20))
    attention = bot.prm.store.deal(order["deal_id"])["attention"]
    assert f"#{order['id']} 之后该开专属佣金了" in attention and "停用" in attention


@pytest.mark.parametrize("how", [
    {"action": "stage", "stage": "delivered"},
    {"action": "shipment_status", "status": "delivered"},
])
def test_by_default_the_commission_waits_until_the_creator_signed_for_the_sample(bot, clock, how):
    packet = ordered(bot, clock)
    mac_runs(bot, approve(bot, packet), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.worker_takes() is None                                       # not when the order is made
    if how["action"] == "shipment_status":
        assert bot.prm.deal_action("shipment", {"deal_id": packet["deal_id"], "carrier": "中通",
                                                "waybill": "ZT1234567890"})["ok"]
        for pending in bot.prm.store.packets(("pending",)):
            bot.prm.decide(pending["id"], "drop")                           # the shipping notice: not this test
    action = {k: v for k, v in how.items() if k != "action"}
    assert bot.prm.deal_action(how["action"], {"deal_id": packet["deal_id"], **action})["ok"]
    job = bot.worker_takes()
    note = job["meta"]["system_note"]
    assert "达人已经签收了寄样单" in note and JST_ORDER in note and DY_ITEM in note and "dy.designated_commission" in note
    bot.relay.store.cancel_if_pending(job["id"], clock.now)
    assert bot.prm.deal_action("stage", {"deal_id": packet["deal_id"], "stage": "delivered"})["ok"]
    assert bot.worker_takes() is None                                       # signed for once: asked once


def test_the_signature_finds_its_sample_order_however_much_was_said_since(bot, clock):
    """mac review F9: the order was looked for among the deal's newest 200 events only.  A person had the
    chat between the order and the signature (every line of hers an event): 已签收 asked for nothing and said
    nothing.  The order is found by its own execution record now."""
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "paused"})["ok"]
    for i in range(250):                                                    # the days until she signs for it
        clock.now += 60
        bot.prm.handle(bot.msg(f"第 {i} 句"))
    assert len(bot.prm.store.events(order["deal_id"], limit=1000)) > 250
    assert bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "copilot"})["ok"]
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    step = [j for j in bot.relay.store.list_jobs(500) if "system_note" in j["meta"]]
    assert len(step) == 1
    note = json.loads(step[0]["meta"])["system_note"]
    assert "达人已经签收了寄样单" in note and JST_ORDER in note and DY_ITEM in note


def test_a_sample_order_verified_late_is_followed_by_its_commission_too(bot, clock):
    """The Mac went silent while it made the order, the bot gave up on it (red), then its answer came:
    VERIFIED_SUCCESS, the order is in 聚水潭.  Signed for, its 专属佣金 is asked for like any other's."""
    order = ordered(bot, clock)
    job = approve(bot, order)
    taken = bot.relay.next_job(0, ("prm",))
    clock.now += LOST_AFTER_SEC + 1
    bot.relay.tick()
    clock.now += PLATFORM_LOST_GRACE_SEC + 1
    bot.prm.tick()
    assert bot.prm.store.packet(order["id"])["status"] == "failed"
    assert bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(record(sample_order_no=JST_ORDER,
                                                                             key=taken["meta"]["key"]))})
    bot.relay.tick()
    assert bot.prm.store.packet(order["id"])["platform"]["execution"]["late"] is True
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    step = bot.worker_takes()
    assert step is not None and JST_ORDER in step["meta"]["system_note"]


def system_jobs(b):
    """The 专属佣金 step jobs the bot asked for (``meta.system_note``), newest first."""
    return [j for j in b.relay.store.list_jobs(500) if "system_note" in j["meta"]]


def signed_for_order(b, clock):
    """A sample order made, verified and its reply sent; the deal waits for the signature."""
    order = ordered(b, clock)
    mac_runs(b, approve(b, order), record(sample_order_no=JST_ORDER))
    b.prm.send_approved()
    clock.now += 600
    return order


def stopped_console(b, clock, action, **data):
    """The console with no running service writes the database itself (src/web/server.py _prm_deal)."""
    assert apply_deal_action(b.prm.store, action, {"deal_id": 1, **data}, now=clock.now, takeover_sec=7200)["ok"]


@pytest.mark.parametrize("when", ["start", "tick"])
def test_signed_for_while_the_bot_was_stopped_is_asked_for_once_it_runs(bot, clock, when):
    """mac review F2: marked 已签收 while the bot was stopped, the chain never started, and marking it again
    did nothing either (the signature was known).  The bot looks at a signature it has not seen when it
    starts and every round after: asked for once."""
    order = signed_for_order(bot, clock)
    stopped_console(bot, clock, "stage", stage="delivered")
    assert system_jobs(bot) == []
    if when == "start":
        bot.prm.start()
        bot.prm.stop()
    else:
        bot.prm.tick()
    [step] = system_jobs(bot)
    note = json.loads(step["meta"])["system_note"]
    assert "达人已经签收了寄样单" in note and JST_ORDER in note and DY_ITEM in note
    bot.prm.tick()
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]   # marked again
    assert len(system_jobs(bot)) == 1
    [asked] = bot.prm.store.events(order["deal_id"], kinds=("next_step",))
    assert event_out(asked)["name"] == f"派开专属佣金的任务 #{step['id']}（商品 {DY_ITEM}）"
    assert asked["detail"]["after"] == order["id"] and asked["detail"]["item"] == DY_ITEM


def test_signed_for_while_stopped_during_a_takeover_is_owed_until_it_ends(bot, clock):
    """At start a person has the deal: the step is owed like any other (mac review F1), asked for once
    the deal is back in copilot mode."""
    signed_for_order(bot, clock)
    stopped_console(bot, clock, "mode", mode="paused")
    stopped_console(bot, clock, "stage", stage="delivered")
    bot.prm.tick()
    assert system_jobs(bot) == [] and "之后该开专属佣金了：现在不在副驾驶模式" in bot.prm.store.deal(1)["attention"]
    assert bot.prm.deal_action("mode", {"deal_id": 1, "mode": "copilot"})["ok"]
    bot.prm.tick()
    bot.prm.tick()
    assert len(system_jobs(bot)) == 1 and "该开专属佣金了" not in bot.prm.store.deal(1)["attention"]


def test_marked_again_a_signature_whose_step_never_went_out_is_asked_for(bot, clock, monkeypatch):
    """The step could not be queued when the deal was marked 已签收 (the console's action is done all the
    same): marking it 已签收 again asks for it; and again does not ask twice."""
    order = signed_for_order(bot, clock)
    queue = bot.relay.submit

    def broken(*args, **kwargs):
        raise OSError("database is locked")

    monkeypatch.setattr(bot.relay, "submit", broken)
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    monkeypatch.setattr(bot.relay, "submit", queue)
    bot.prm.tick()
    assert system_jobs(bot) == []                                           # the signature is known
    for _ in range(2):
        assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    assert len(system_jobs(bot)) == 1


@pytest.mark.parametrize("left", ["drafted", "opened"])
def test_a_chain_a_bot_from_before_started_is_not_asked_again(bot, clock, left):
    """A database from before ``deal.chain`` (a bot that recorded no ``next_step``): its signatures are looked
    at once at start; one whose step was drafted, or whose products are all open, asks nothing."""
    order = signed_for_order(bot, clock)
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    step = bot.worker_takes()
    bot.finish(step["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    if left == "opened":
        mac_runs(bot, approve(bot, bot.prm.store.packet_for_job(step["id"])), record())
        bot.prm.send_approved()
        final = bot.worker_takes()                                          # 「都开完了」: nothing to fill
        bot.finish(final["id"], draft(message_kind="other", reply_draft="", slots={}))
    with bot.prm.store._lock:                                               # what the bot before left
        bot.prm.store._conn.execute("DELETE FROM deal_events WHERE kind = 'next_step'")
        bot.prm.store._conn.execute("UPDATE deals SET chain = '{}'")
        bot.prm.store._conn.commit()
    jobs = len(system_jobs(bot))
    clock.now += 60
    bot.prm.start()
    bot.prm.stop()
    assert len(system_jobs(bot)) == jobs
    assert bot.prm.store.deal(order["deal_id"])["chain"]["seen"] == bot.prm.store.deal(order["deal_id"])["delivered_at"]


def test_a_signature_from_before_the_order_waits_for_the_next_one(bot, clock):
    """Marked 已签收 before the sample order was made (an earlier shipment): not this order's signature.  Set
    back to 运输中 and signed for again, it is."""
    stopped_console(bot, clock, "stage", stage="delivered")
    clock.now += 60
    signed_for_order(bot, clock)
    bot.prm.tick()
    assert system_jobs(bot) == []
    for stage in ("transit", "delivered"):
        assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": stage})["ok"]
    assert len(system_jobs(bot)) == 1


def test_adopting_the_ais_signed_for_proposal_asks_for_the_commission_too(bot, clock):
    """kb/合作政策.md 2.1 (5): the creator says the sample came, her draft proposes 已签收, the operator adopts it
    (采纳 AI 建议): that is a signature like marking it by hand, the 专属佣金 step is asked for (once)."""
    order = signed_for_order(bot, clock)
    bot.prm.handle(bot.msg("收到样品啦，很喜欢"))
    job = bot.worker_takes()
    bot.finish(job["id"], draft(message_kind="greeting", reply_draft="太好啦～", slots={}, stage_proposal="delivered"))
    assert bot.prm.store.deal(order["deal_id"])["stage_proposal"] == "delivered" and system_jobs(bot) == []
    assert bot.prm.deal_action("adopt", {"deal_id": order["deal_id"]})["ok"]
    deal = bot.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "delivered" and deal["delivered_at"] == clock.now and deal["stage_proposal"] == ""
    [step] = system_jobs(bot)
    note = json.loads(step["meta"])["system_note"]
    assert note.startswith(f"达人已经签收了寄样单 {JST_ORDER}") and f"这一份草稿只填商品 {DY_ITEM}" in note
    assert any(e["kind"] == "stage" and e["detail"]["to"] == "delivered" for e in bot.prm.store.events(order["deal_id"]))
    bot.prm.tick()
    assert len(system_jobs(bot)) == 1


def test_a_delivered_deal_without_a_sample_order_asks_for_nothing(bot, clock):
    packet = drafted(bot, preview=False, reply="好的")
    assert bot.prm.deal_action("stage", {"deal_id": packet["deal_id"], "stage": "delivered"})["ok"]
    assert all("system_note" not in j["meta"] for j in bot.relay.store.list_jobs(10))


def test_only_a_sample_order_and_a_commission_have_a_next_step():
    from src.prm.service import next_step_note
    order = platforms.check_action({**SAMPLE, "shop": "xhs", "item_id": XHS_ITEM, "skus": [{"sku_id": "a" * 24, "qty": 1}],
                                    "creator_platform": "xhs"})
    assert "xhs.direct_plan" in next_step_note(order, record(sample_order_no=JST_ORDER))
    assert next_step_note(platforms.check_action(CHANNEL), record(link=DY_LINK)) == ""


def test_a_commission_step_and_a_reply_do_not_put_each_other_out_of_date(bot, clock):
    """The bot's next step (meta.system_note) is a lane of its own: the creator's message is not
    merged into it, and neither draft supersedes the other (only one of its own lane)."""
    bot.prm._commission_after = "created"
    packet = ordered(bot, clock)
    execution = approve(bot, packet)
    bot.prm.handle(bot.msg("好的，等你们寄"))                               # a reply job, queued first
    mac_runs(bot, execution, record(sample_order_no=JST_ORDER))            # → the 专属佣金 step is asked for
    jobs = {("system_note" in j["meta"]): j for j in bot.relay.store.list_jobs(10) if j["status"] == "pending"}
    assert jobs[True]["text"] == "" and "好的，等你们寄" in jobs[False]["text"]   # not merged into the step
    reply_job = bot.worker_takes()
    assert reply_job["id"] == jobs[False]["id"]
    step_job = bot.relay.next_job(0, ("prm",))                              # both run at once
    bot.finish(reply_job["id"], draft(message_kind="other", reply_draft="好的老师，寄出后发单号给您", slots={}))
    reply = bot.prm.store.packet_for_job(reply_job["id"])
    assert reply["status"] == "pending"                                     # the newer step job does not outdate it
    bot.finish(step_job["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    step = bot.prm.store.packet_for_job(step_job["id"])
    assert step["status"] == "pending" and step["meta"]["lane"] == "system"
    assert bot.prm.store.packet(reply["id"])["status"] == "pending"         # nor does the step's draft
    bot.prm.handle(bot.msg("对了，能寄黑色吗"))                              # the creator writes again
    assert bot.prm.store.packet(reply["id"])["status"] == "superseded"      # her reply is redrafted
    assert bot.prm.store.packet(step["id"])["status"] == "pending"          # the 专属佣金 step stays


def test_a_douyin_order_tells_the_commission_draft_the_uid_it_found(bot, clock):
    packet = ordered(bot, clock)
    mac_runs(bot, approve(bot, packet), record(sample_order_no=JST_ORDER, creator_uid="5100000000000099"))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": packet["deal_id"], "stage": "delivered"})["ok"]
    note = bot.worker_takes()["meta"]["system_note"]
    assert "抖音 UID 是 5100000000000099" in note and f"这一份草稿只填商品 {DY_ITEM}" in note   # it may know only the 抖音号


def test_signed_for_again_after_a_step_back_picks_up_what_is_left(bot, clock):
    """The chain cut off (say a newer draft took a step's place): 运输中, then 已签收 again, asks
    again, and the draft starts at the first product not done yet (kb/合作政策.md 2.1)."""
    packet = ordered(bot, clock)
    mac_runs(bot, approve(bot, packet), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": packet["deal_id"], "stage": "delivered"})["ok"]
    first = bot.worker_takes()
    bot.finish(first["id"], draft(message_kind="other", reply_draft="", slots={}))
    for stage in ("transit", "delivered"):
        assert bot.prm.deal_action("stage", {"deal_id": packet["deal_id"], "stage": stage})["ok"]
    again = bot.worker_takes()
    assert again is not None and again["id"] != first["id"]
    assert "达人已经签收了寄样单" in again["meta"]["system_note"] and "开过的不要再开" in again["meta"]["system_note"]


def test_only_a_douyin_order_names_the_uid_and_a_commission_names_its_creator():
    from src.prm.service import next_step_note
    dy = platforms.check_action(SAMPLE)
    assert f"抖音 UID 是 {DY_UID}" in next_step_note(dy, record(sample_order_no=JST_ORDER, creator_uid=DY_UID))
    assert "UID" not in next_step_note(dy, record(sample_order_no=JST_ORDER))
    xhs = platforms.check_action({**SAMPLE, "shop": "xhs", "item_id": XHS_ITEM, "skus": [{"sku_id": "a" * 24, "qty": 1}],
                                  "creator_platform": "xhs"})
    assert "UID" not in next_step_note(xhs, record(sample_order_no=JST_ORDER, creator_uid=DY_UID))
    commission = platforms.check_action({"action": "dy.designated_commission", "item_id": DY_ITEM,
                                         "creator_platform_id": DY_UID, "commission_rate": 20})
    assert f"开好了（达人 {DY_UID}）" in next_step_note(commission, record())


TWO = {"action": "jst.sample_order", "shop": "dy", "creator_platform": "dy", "creator_uid": DY_UID,
       "receiver": SAMPLE["receiver"], "items": [{"item_id": DY_ITEM, "skus": [{"sku_id": DY_SKU, "qty": 1}]},
                                                 {"item_id": DY_ITEM2, "skus": [{"sku_id": DY_SKU2, "qty": 1}]}]}


def test_a_two_product_order_opens_each_commission_in_turn_and_only_the_last_one_speaks(bot, clock):
    """The whole chain (kb/合作政策.md 2.1): signed for → 专属佣金 of the first product (an empty reply:
    nothing goes out) → of the second (the reply that says both are done) → nothing left: no action,
    no reply, and no further step."""
    order = drafted(bot, preview=block(TWO, run={"steps": ["create"]}), reply="寄样单建好啦，单号 {寄样单号}～")
    bot.prm.store.token_for(order["party_id"], "phone", "手机", "13900001111", clock.now)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER, creator_uid=DY_UID))
    bot.prm.send_approved()
    told = [(order["chat_id"], f"寄样单建好啦，单号 {JST_ORDER}～")]
    assert bot.texts == told and bot.worker_takes() is None                # PRM_COMMISSION_AFTER=delivered
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    first = bot.worker_takes()
    note = first["meta"]["system_note"]
    assert DY_ITEM in note and DY_ITEM2 in note and "共 2 个" in note and first["text"] == ""
    bot.finish(first["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    step1 = bot.prm.store.packet_for_job(first["id"])
    assert step1["status"] == "pending" and step1["meta"]["lane"] == "system" and step1["text"] == ""
    mac_runs(bot, approve(bot, step1), record())
    bot.prm.send_approved()
    assert bot.prm.store.packet(step1["id"])["status"] == "sent" and bot.texts == told   # nothing went out
    second = bot.worker_takes()
    assert f"商品 {DY_ITEM} 的专属佣金（20%）已经在平台上开好了" in second["meta"]["system_note"]
    last = "两款的专属佣金都开好啦，明天 0 点生效～"
    bot.finish(second["id"], {**draft(message_kind="money", reply_draft=last, slots={}),
                              "platform": block({**DESIGNATED, "item_id": DY_ITEM2})})
    step2 = bot.prm.store.packet_for_job(second["id"])
    assert step2["status"] == "pending" and step2["platform"]["action"]["item_id"] == DY_ITEM2
    mac_runs(bot, approve(bot, step2), record())
    bot.prm.send_approved()
    assert bot.texts == told + [(order["chat_id"], last)]                  # the last one carries the reply
    third = bot.worker_takes()
    assert f"商品 {DY_ITEM2} 的专属佣金" in third["meta"]["system_note"]
    bot.finish(third["id"], draft(message_kind="other", reply_draft="", slots={}))   # all done: no action
    assert bot.prm.store.packet_for_job(third["id"])["status"] == "no_reply"
    assert bot.worker_takes() is None and bot.texts == told + [(order["chat_id"], last)]
    assert sorted(p["platform"]["action"]["item_id"] for p in bot.prm.store.deal_packets(
        order["deal_id"], tools=("dy.designated_commission",), statuses=("sent",))) == [DY_ITEM, DY_ITEM2]


# ── the bot keeps the chain's record (mac review F5) ─────────────────────
# Before, only the Mac's case file knew which products were open: a product whose 专属佣金 could not
# be opened stopped the ones after it, and 运输中 → 已签收 started from that same product again.

THREE = {**TWO, "items": [*TWO["items"], {"item_id": DY_ITEM3, "skus": [{"sku_id": DY_SKU3, "qty": 1}]}]}


def three_signed_for(b, clock):
    """A three-product sample order made, verified, and signed for: its first step is asked for."""
    order = drafted(b, preview=block(THREE, run={"steps": ["create"]}), reply="寄样单建好啦")
    b.prm.store.token_for(order["party_id"], "phone", "手机", "13900001111", clock.now)
    mac_runs(b, approve(b, order), record(sample_order_no=JST_ORDER, creator_uid=DY_UID))
    b.prm.send_approved()
    assert b.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    return order


def step_drafts(b, item, verdict="ready", reply=""):
    """The Mac takes the step job and drafts the 专属佣金 of ``item``: (the job, its draft)."""
    job = b.worker_takes()
    assert job is not None and "system_note" in job["meta"]
    b.finish(job["id"], {**draft(message_kind="money", reply_draft=reply, slots={}),
                         "platform": block({**DESIGNATED, "item_id": item}, verdict=verdict)})
    return job, b.prm.store.packet_for_job(job["id"])


def chain_attention(b):
    return next((n["text"] for n in b.prm.store.deal(1)["notices"] if n["key"] == "chain"), "")


@pytest.mark.parametrize("how, label", [
    ("dropped", "这一轮不开（运营点了「不发」）："), ("not-run", "没开成："),
    ("uncertain", "结果不确定（运营会先到平台后台核对）："), ("refused", "没开成："),
], ids=["dropped", "mac-never-took-it", "uncertain", "refused"])
def test_a_product_that_cannot_be_opened_does_not_hold_up_the_next(bot, clock, how, label):
    """Its preview never passed and the operator dropped it, the Mac never took the execution (NOT_RUN after 30
    minutes), the result is uncertain, the platform refused it: that product is skipped for this round, the next
    one is asked for, and the system note says which is which (the bot's record, not the case file's)."""
    three_signed_for(bot, clock)
    job, first = step_drafts(bot, DY_ITEM, verdict="blocked" if how == "dropped" else "ready")
    assert f"这一份草稿只填商品 {DY_ITEM} 的 dy.designated_commission" in job["meta"]["system_note"]
    assert "这一轮这之后还要开 2 个" in job["meta"]["system_note"]
    if how == "dropped":
        assert f"在待审批 {DY_ITEM}（#{first['id']}）：预览没通过，可以「重新预览」，或者点「不发」跳过它" in chain_attention(bot)
        assert bot.prm.decide(first["id"], "drop")["ok"]
    elif how == "not-run":
        approve(bot, first)
        clock.now += APPROVED_EXPIRE_SEC + 1
        bot.prm.sweep_executing()
    else:
        mac_runs(bot, approve(bot, first), record("UNKNOWN" if how == "uncertain" else "FAILED"))
    second = bot.worker_takes()
    note = second["meta"]["system_note"]
    assert f"这一份草稿只填商品 {DY_ITEM2} 的" in note and "这一轮这之后还要开 1 个" in note
    assert label + DY_ITEM in note and f"还没开：{DY_ITEM2}、{DY_ITEM3}" in note
    assert note.index("这一份草稿只填") < note.index("机器人的记录")                # what to do first, the list last
    for item, job in ((DY_ITEM2, second), (DY_ITEM3, None)):
        if job is None:
            job = bot.worker_takes()
            assert f"这一份草稿只填商品 {item} 的" in job["meta"]["system_note"] and "最后一个要开的商品" in job["meta"]["system_note"]
        bot.finish(job["id"], {**draft(message_kind="money", reply_draft="", slots={}),
                               "platform": block({**DESIGNATED, "item_id": item})})
        mac_runs(bot, approve(bot, bot.prm.store.packet_for_job(job["id"])), record())
        bot.prm.send_approved()
    final = bot.worker_takes()                                              # the round is done: the case file hears it
    note = final["meta"]["system_note"]
    assert "这一轮没有要开的商品了：这一份不要填平台动作" in note and f"已开好：{DY_ITEM2}、{DY_ITEM3}" in note
    bot.finish(final["id"], draft(message_kind="other", reply_draft="", slots={}))
    assert bot.worker_takes() is None
    attention = bot.prm.store.deal(1)["attention"]
    if how == "dropped":                                                    # a person's decision: nothing to say
        assert chain_attention(bot) == ""
    else:                                                                   # red: said until 「我已处理」
        assert f"专属佣金（寄样单 {JST_ORDER}）：开好 2/3 个" in attention and RESTART_HINT in attention
        assert bot.prm.decide(first["id"], "ack")["ok"] and chain_attention(bot) == ""


def test_the_deal_says_where_the_chain_stands(bot, clock):
    """The deal's notice follows the chain: what is drafted now, what is open, what is left; gone once all are open."""
    three_signed_for(bot, clock)
    [job] = system_jobs(bot)
    assert chain_attention(bot) == (f"专属佣金（寄样单 {JST_ORDER}）：开好 0/3 个；正在起草 {DY_ITEM}（任务 #{job['id']}）；"
                                    f"还没开 {DY_ITEM2}、{DY_ITEM3}")
    for item in (DY_ITEM, DY_ITEM2, DY_ITEM3):
        _, step = step_drafts(bot, item)
        assert f"在待审批 {item}（#{step['id']}）" in chain_attention(bot)
        mac_runs(bot, approve(bot, step), record())
        bot.prm.send_approved()
    assert chain_attention(bot) == ""                                       # all three open


def test_signed_for_again_asks_again_only_for_what_is_not_open(bot, clock):
    """kb 2.1: 运输中 → 已签收 starts a new round: the product dropped in the last one is asked for again, the open
    ones never are; one product of the round, its note says it is the last."""
    three_signed_for(bot, clock)
    _, first = step_drafts(bot, DY_ITEM)
    assert bot.prm.decide(first["id"], "drop")["ok"]
    for item in (DY_ITEM2, DY_ITEM3):
        _, step = step_drafts(bot, item)
        mac_runs(bot, approve(bot, step), record())
        bot.prm.send_approved()
    bot.finish(bot.worker_takes()["id"], draft(message_kind="other", reply_draft="", slots={}))
    assert bot.worker_takes() is None
    for stage in ("transit", "delivered"):
        assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": stage})["ok"]
    again = bot.worker_takes()
    note = again["meta"]["system_note"]
    assert note.startswith(f"达人已经签收了寄样单 {JST_ORDER}") and f"这一份草稿只填商品 {DY_ITEM} 的" in note
    assert "这是这一轮最后一个要开的商品" in note and f"已开好：{DY_ITEM2}、{DY_ITEM3}" in note
    bot.finish(again["id"], {**draft(message_kind="money", reply_draft="三款都开好啦", slots={}),
                             "platform": block({**DESIGNATED, "item_id": DY_ITEM})})
    mac_runs(bot, approve(bot, bot.prm.store.packet_for_job(again["id"])), record())
    bot.prm.send_approved()
    final = bot.worker_takes()
    assert "这一轮没有要开的商品了" in final["meta"]["system_note"]
    bot.finish(final["id"], draft(message_kind="other", reply_draft="", slots={}))
    assert bot.worker_takes() is None and chain_attention(bot) == ""
    opened = [p["platform"]["action"]["item_id"] for p in bot.prm.store.deal_packets(1, tools=("dy.designated_commission",))
              if p["status"] == "sent"]
    assert sorted(opened) == [DY_ITEM, DY_ITEM2, DY_ITEM3]                    # each once


def test_an_uncertain_product_asked_again_is_not_opened_before_it_is_checked(bot, clock):
    """The product whose result was uncertain is asked for again in the next round (its note says so), and its
    draft waits for the operator's check like any second one (mac review F6)."""
    three_signed_for(bot, clock)
    _, first = step_drafts(bot, DY_ITEM)
    mac_runs(bot, approve(bot, first), record("ACCEPTED_UNVERIFIED"))
    for item in (DY_ITEM2, DY_ITEM3):
        _, step = step_drafts(bot, item)
        mac_runs(bot, approve(bot, step), record())
        bot.prm.send_approved()
    bot.finish(bot.worker_takes()["id"], draft(message_kind="other", reply_draft="", slots={}))
    for stage in ("transit", "delivered"):
        assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": stage})["ok"]
    again, second = step_drafts(bot, DY_ITEM)
    assert f"它上次在 #{first['id']} 的结果不确定" in again["meta"]["system_note"]
    jobs = len(bot.relay.store.list_jobs(100))
    out = bot.prm.decide(second["id"], "execute")
    assert not out["ok"] and "结果不确定" in out["error"] and len(bot.relay.store.list_jobs(100)) == jobs


def test_each_product_is_asked_for_once_a_round(bot, clock):
    """A step whose draft opens nothing (no action: the Mac thought it done, or could not fill it) moves the chain on;
    that product is not asked for again in this round (no loop), and the deal says what was left, until the deal
    moves on past the sample."""
    three_signed_for(bot, clock)
    for item in (DY_ITEM, DY_ITEM2, DY_ITEM3):
        job = bot.worker_takes()
        assert f"这一份草稿只填商品 {item} 的" in job["meta"]["system_note"]
        bot.finish(job["id"], draft(message_kind="other", reply_draft="", slots={}))
    assert bot.worker_takes() is None and len(system_jobs(bot)) == 3
    assert f"派过、草稿没开 {DY_ITEM}、{DY_ITEM2}、{DY_ITEM3}。{RESTART_HINT}" in chain_attention(bot)
    bot.prm.tick()
    assert len(system_jobs(bot)) == 3
    assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": "brief"})["ok"]   # 素材已发: moved on
    assert chain_attention(bot) == ""


def test_a_long_order_keeps_the_notice_short_and_the_instruction_first(bot, clock):
    """A 专场 order of many products: the deal's notice counts what it cannot name, the note still starts with
    what to do."""
    many = [str(30000000000000000000000000000000 + i) for i in range(50)]     # 50 products of 32-digit ids
    order = {**THREE, "items": [{"item_id": i, "skus": [{"sku_id": str(31000000000000000000000000000000 + n), "qty": 1}]}
                                for n, i in enumerate(many)]}
    action = platforms.check_action(order, limits=platforms.FORM_LIMITS)
    packet = drafted(bot, preview={**block(THREE, run={"steps": ["create"]}), "action": action}, reply="寄样单建好啦")
    bot.prm.store.token_for(packet["party_id"], "phone", "手机", "13900001111", clock.now)
    mac_runs(bot, approve(bot, packet), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": "delivered"})["ok"]
    [job] = system_jobs(bot)
    note = json.loads(job["meta"])["system_note"]
    assert note.index(f"这一份草稿只填商品 {many[0]}") < 200 and len(note) < 4000 and many[-1] in note
    text = chain_attention(bot)
    assert len(text) <= 300 and "等 49 个" in text and text.startswith(f"专属佣金（寄样单 {JST_ORDER}）：开好 0/50 个")


@pytest.mark.parametrize("with_action", [False, True], ids=["reply-only", "with-action"])
def test_a_commission_step_always_waits_for_a_person_whatever_its_kinds_gate(bot, clock, with_action):
    """The system lane (meta.system_note) is never sent by a gate: a 专属佣金 step whose draft forgot
    (or could not fill) the platform action and says 「开好啦」 would tell the creator something
    nothing on the platform did.  Such a draft waits, and says that it carries no action."""
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    told = list(bot.texts)
    bot.prm.store.set_gate("other", "auto", clock.now)                       # "other" has earned auto
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    step = bot.worker_takes()
    assert "system_note" in step["meta"]
    d = draft(message_kind="other", reply_draft="合作这边我给你开好专属佣金啦，明天 0 点生效～", slots={},
              stage_proposal="", needs_attention=False, reason="开好了")
    if with_action:
        d["platform"] = block()
    bot.finish(step["id"], d)
    packet = bot.prm.store.packet_for_job(step["id"])
    assert (packet["status"], packet["route"]) == ("pending", "review") and packet["meta"]["lane"] == "system"
    if not with_action:
        assert packet["needs_attention"] and "没带平台动作" in packet["reason"]
        assert f"#{step['id']} 开专属佣金这一步的草稿没带平台动作" in bot.prm.store.deal(order["deal_id"])["attention"]
    bot.prm.send_approved()
    assert bot.texts == told                                                  # nothing went out on its own


def test_a_reply_of_the_same_kind_still_goes_out_by_its_gate(bot, clock):
    """The control: only the system lane is held back, a reply of an auto kind goes out as before."""
    bot.prm.store.set_gate("other", "auto", clock.now)
    packet = drafted(bot, preview=False, reply="好的老师～", kind="other")
    assert (packet["status"], packet["route"]) == ("approved", "auto")


def test_a_commission_step_drafted_while_a_person_took_over_is_left_to_them(bot, clock):
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    step = bot.worker_takes()                                               # the Mac drafts the 专属佣金 …
    assert bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "human"})["ok"]   # … a person takes over
    bot.finish(step["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    packet = bot.prm.store.packet_for_job(step["id"])
    assert packet["status"] == "superseded" and packet["meta"]["lane"] == "system"
    assert f"#{step['id']} 开专属佣金这一步的草稿没进待审批" in bot.prm.store.deal(order["deal_id"])["attention"]
    assert snap(bot)["pending"] == [] and not bot.prm.decide(packet["id"], "execute")["ok"]


def test_set_to_created_signing_for_the_sample_later_asks_nothing_more(bot, clock):
    """PRM_COMMISSION_AFTER=created: the 专属佣金 is asked for when the order is made; opened then, marking the
    deal 已签收 later asks nothing more (nothing is opened twice)."""
    bot.prm._commission_after = "created"
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    step, commission = step_drafts(bot, DY_ITEM)
    assert "已经在聚水潭建好" in step["meta"]["system_note"]
    mac_runs(bot, approve(bot, commission), record())
    bot.prm.send_approved()
    final = bot.worker_takes()                                              # 「都开完了」
    bot.finish(final["id"], draft(message_kind="other", reply_draft="", slots={}))
    for stage in ("delivered", "transit", "delivered"):
        assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": stage})["ok"]
    assert bot.worker_takes() is None and len(system_jobs(bot)) == 2


def test_set_to_created_signing_for_it_picks_up_what_the_chain_left(bot, clock):
    """mac review F3: with PRM_COMMISSION_AFTER=created the remedy (运输中 → 已签收) did nothing, the chain
    only started when the order was made.  In this mode too a signature after the order starts a new round:
    what is not open is asked for again, at the first signature and at each one after setting it back; the
    deal's notice says so in this mode's words."""
    bot.prm._commission_after = "created"
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    first = bot.worker_takes()
    bot.finish(first["id"], draft(message_kind="other", reply_draft="", slots={}))   # the chain left it
    assert f"派过、草稿没开 {DY_ITEM}。{RESTART_HINT_CREATED}" in chain_attention(bot)
    for stage in ("transit", "delivered"):
        assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": stage})["ok"]
    again = bot.worker_takes()
    assert again is not None and again["id"] != first["id"]
    note = again["meta"]["system_note"]
    assert note.startswith(f"达人已经签收了寄样单 {JST_ORDER}") and f"这一份草稿只填商品 {DY_ITEM}" in note
    bot.finish(again["id"], draft(message_kind="other", reply_draft="", slots={}))
    for stage in ("transit", "delivered"):                                  # signed for already: set back, again
        assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": stage})["ok"]
    assert len(system_jobs(bot)) == 3


def test_a_commission_step_waiting_in_review_sends_the_creator_no_holding_reply(bot, clock):
    """「稍等」answers a creator who is waiting; a 专属佣金 step (the system lane) waiting for a
    person answers nothing she asked, so she hears nothing.  Her own message still gets one."""
    bot.prm._holding_sec = 600
    packet = ordered(bot, clock)
    mac_runs(bot, approve(bot, packet), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": packet["deal_id"], "stage": "delivered"})["ok"]
    step_job = bot.worker_takes()
    bot.finish(step_job["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    assert bot.prm.store.packet_for_job(step_job["id"])["meta"]["lane"] == "system"
    told = list(bot.texts)
    clock.now += 3600                                                       # an hour in 待审批
    assert bot.prm.send_holding_replies() == 0 and bot.texts == told
    bot.prm.handle(bot.msg("收到啦，很好看"))
    reply_job = bot.worker_takes()
    bot.finish(reply_job["id"], draft(message_kind="other", reply_draft="戴上效果怎么样～", slots={}))
    clock.now += 3600
    assert bot.prm.send_holding_replies() == 1 and bot.texts[-1] == (packet["chat_id"], bot.prm._holding_text)


# ── a 专属 action runs once per product and creator ───────────────────────
# The Mac's ledger only stops one approval from running twice; a second draft of the same
# 专属佣金 / 定向计划 / 渠道品 (a chain picked up again while a step runs, a reply that carries
# one) must not open a second one on the platform.

def no_next_step(b, clock):
    """Cancel the next-step draft job a verified 专属佣金 asks for (not what these tests are about)."""
    for job in b.relay.store.list_jobs(20):
        if "system_note" in job["meta"]:
            b.relay.store.cancel_if_pending(job["id"], clock.now)


def opened(b, clock, raw=DESIGNATED, run=None, rec=None, reply="专属佣金 20% 设好啦，明天 0 点生效～"):
    """A draft of this action approved, verified on the platform and its reply sent."""
    packet = drafted(b, block(raw, run=run), reply=reply)
    mac_runs(b, approve(b, packet), rec or record())
    b.prm.send_approved()
    no_next_step(b, clock)
    return b.prm.store.packet(packet["id"])


def date_of(at):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


def test_a_commission_opened_once_is_not_opened_again(bot, clock):
    first = opened(bot, clock)
    [event] = [e for e in bot.prm.store.events(first["deal_id"]) if e["kind"] == "platform_result"]
    assert event["detail"]["item_id"] == DY_ITEM and event["detail"]["creator_platform_id"] == DY_UID
    clock.now += 3 * 86400
    again = drafted(bot, reply="")                             # the same product for the same creator
    jobs = len(bot.relay.store.list_jobs(100))
    out = bot.prm.decide(again["id"], "execute")
    assert not out["ok"]
    assert f"#{first['id']}（{date_of(first['sent_at'])}）" in out["error"] and DY_ITEM in out["error"]
    assert "开过了" in out["error"]
    assert out["error"].endswith("点「不发」就行（寄样单的专属佣金：系统会接着开还没开的商品）")
    assert bot.prm.store.packet(again["id"])["status"] == "pending"
    assert len(bot.relay.store.list_jobs(100)) == jobs                      # nothing goes to the Mac
    assert bot.prm.decide(again["id"], "drop")["ok"]


def test_picking_the_chain_up_again_while_a_step_runs_does_not_open_it_twice(bot, clock):
    """The operator sets the deal back to 运输中 and 已签收 while the 专属佣金 step is still running on
    the Mac: the bot knows that product is running and asks for nothing more meanwhile; a draft of the
    same product that comes anyway (a reply carrying it) is refused while it runs and after it opened."""
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    step = bot.worker_takes()
    bot.finish(step["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    first = bot.prm.store.packet_for_job(step["id"])
    running = approve(bot, first)
    bot.relay.next_job(0, ("prm",))                                         # the Mac is on it
    for stage in ("transit", "delivered"):
        assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": stage})["ok"]
    assert len(system_jobs(bot)) == 1                                       # nothing asked while it runs
    assert "正在执行 3000000000000000101" in bot.prm.store.deal(order["deal_id"])["attention"]
    second = drafted(bot, reply="")                                         # the same product, from a reply
    assert second["status"] == "pending" and bot.prm.store.packet(first["id"])["status"] == "executing"
    out = bot.prm.decide(second["id"], "execute")
    approved_at = bot.prm.store.packet(first["id"])["platform"]["approved_at"]
    assert not out["ok"] and "正在执行" in out["error"] and f"#{first['id']}" in out["error"]
    assert date_of(approved_at) in out["error"] and "点「不发」" in out["error"]
    assert bot.relay.finish(running["id"], {"ok": True, "text": json.dumps(record(key=json.loads(running["meta"])["key"]))})
    bot.relay.tick()
    assert bot.prm.store.packet(first["id"])["status"] == "approved"
    out = bot.prm.decide(second["id"], "execute")                          # done now: still not twice
    assert not out["ok"] and "开过了" in out["error"] and f"#{first['id']}" in out["error"]
    assert len([j for j in bot.relay.store.list_jobs(100) if '"platform_execute"' in j["meta"]]) == 2   # order + step


def test_another_product_or_another_creator_is_still_allowed(bot, clock):
    first = opened(bot, clock)
    other_item = opened(bot, clock, {**DESIGNATED, "item_id": DY_ITEM2})
    other_creator = opened(bot, clock, {**DESIGNATED, "creator_platform_id": "5100000000000003"})
    assert [p["status"] for p in (first, other_item, other_creator)] == ["sent", "sent", "sent"]
    running = drafted(bot, block({**DESIGNATED, "item_id": DY_ITEM3}))
    approve(bot, running)
    bot.relay.next_job(0, ("prm",))                                         # on the Mac now
    channel = drafted(bot, block(CHANNEL), reply="链接：{链接}")              # a channel product is not a commission
    assert approve(bot, channel)


def test_only_a_verified_or_running_one_counts(bot, clock):
    tried = drafted(bot)
    mac_runs(bot, approve(bot, tried), record("FAILED"))                     # the platform refused it: try again
    retry = drafted(bot)
    mac_runs(bot, approve(bot, retry), record())
    no_next_step(bot, clock)
    assert bot.prm.store.packet(retry["id"])["status"] == "approved"
    out = bot.prm.decide(drafted(bot)["id"], "execute")
    assert not out["ok"] and f"#{retry['id']}" in out["error"]
    bot.prm._send = lambda chat, text: False                                 # verified, but the reply failed
    bot.prm.send_approved()
    assert bot.prm.store.packet(retry["id"])["status"] == "failed"
    out = bot.prm.decide(drafted(bot)["id"], "execute")
    assert not out["ok"] and f"#{retry['id']}" in out["error"]


def write_step(name, outcome):
    """One step of an execution record that ran with --execute."""
    return {"name": name, "label": name, "execute": True, "command": "node bin/dy.mjs", "exit_code": 0,
            "seconds": 1.0, "verdict": "", "outcome": outcome, "problems": [], "facts": [], "raw": "{}"}


def made_then_refused():
    """dy.channel_goods: the 渠道品 was made (VERIFIED_SUCCESS), binding the creator was refused: FAILED."""
    rec = record("FAILED", channel_id="7000000001")
    rec["steps"] = [write_step("create", "VERIFIED_SUCCESS"), write_step("bind", "FAILED")]
    return rec


def unknown_after_the_ledger():
    """The Mac took the approval's ledger entry, then something went wrong: UNKNOWN, whatever its steps say."""
    rec = record("UNKNOWN")
    rec["problems"] = ["Mac 上执行出错：拿到台账以后出错了；先到平台后台核对"]
    rec["steps"] = [{**write_step("designated", ""), "execute": False}]
    return rec


MAYBE_OPENED = [(DESIGNATED, lambda: record("ACCEPTED_UNVERIFIED")), (DESIGNATED, lambda: record("INDETERMINATE")),
                (DESIGNATED, lambda: record("UNKNOWN")), (DESIGNATED, unknown_after_the_ledger),
                (CHANNEL, lambda: record("PARTIAL", channel_id="7000000001")), (CHANNEL, made_then_refused)]
MAYBE_IDS = ["accepted-unverified", "indeterminate", "unknown", "unknown-after-the-ledger", "partial", "made-then-refused"]


def maybe_opened(b, clock, raw, rec):
    """A draft of this 专属 action approved; the platform may have taken it, the read-back did not prove it."""
    reply = "链接：{链接}" if raw is CHANNEL else "专属佣金 20% 设好啦，明天 0 点生效～"
    first = drafted(b, block(raw), reply=reply)
    mac_runs(b, approve(b, first), rec)
    no_next_step(b, clock)
    return b.prm.store.packet(first["id"]), reply


@pytest.mark.parametrize("raw, rec", MAYBE_OPENED, ids=MAYBE_IDS)
def test_a_result_that_may_have_opened_it_stops_a_second_one_until_it_is_checked(bot, clock, raw, rec):
    """mac review F6: only a verified (or running) one stopped a second approval.  One that ended ACCEPTED_UNVERIFIED,
    INDETERMINATE, UNKNOWN or PARTIAL (or wrote a step before a later one failed) may well be on the platform: a
    second draft of the same product and creator is refused, its first's red mark comes back saying what to check,
    and only 「我已处理」 after that (the operator found it is not there) lets that draft run."""
    first, reply = maybe_opened(bot, clock, raw, rec())
    assert first["status"] == "failed" and "平台上可能已经开了" in first["error"]
    assert bot.prm.decide(first["id"], "ack")["ok"]                          # seen to before: not a check
    second = drafted(bot, block(raw), reply=reply)
    jobs = len(bot.relay.store.list_jobs(100))
    out = bot.prm.decide(second["id"], "execute")
    assert not out["ok"] and "结果不确定" in out["error"] and "请先到平台后台核对" in out["error"]
    assert f"#{first['id']}" in out["error"] and "点这一份的「不发」" in out["error"]
    assert len(bot.relay.store.list_jobs(100)) == jobs                       # nothing goes to the Mac
    [alert] = snap(bot)["platform_alerts"]                                   # red again, saying what to do now
    assert alert["id"] == first["id"] and alert["error"].startswith(f"#{second['id']} 要再开同一个商品：先到平台后台核对")
    assert alert["platform"]["recheck_for"] == second["id"] and alert["platform"]["acked_at"] is None
    assert f"#{first['id']} 的结果不确定，#{second['id']} 要再开同一个商品" in bot.prm.store.deal(1)["attention"]
    assert not bot.prm.decide(second["id"], "execute")["ok"]                 # not checked yet
    assert bot.prm.decide(first["id"], "ack") == {"ok": True, "released": second["id"]}   # it is not there
    assert snap(bot)["platform_alerts"] == [] and "结果不确定" not in bot.prm.store.deal(1)["attention"]
    job = approve(bot, second)
    assert json.loads(job["meta"])["op"] == "platform_execute"
    names = [event_out(e)["name"] for e in bot.prm.store.events(1, kinds=("platform_recheck",))]
    assert names == [f"#{first['id']} 的结果不确定，#{second['id']} 要再开同一个商品：先核对平台"] * 2


def test_a_check_lets_only_the_draft_it_was_asked_for_run(bot, clock):
    """「我已处理」 answers the draft that asked: another draft of the same product asks again (and takes the check
    over); once one of them runs, the other is refused as running."""
    first, reply = maybe_opened(bot, clock, DESIGNATED, record("UNKNOWN"))
    second = drafted(bot, reply=reply)
    assert not bot.prm.decide(second["id"], "execute")["ok"]
    assert bot.prm.decide(first["id"], "ack")["released"] == second["id"]
    third = drafted(bot, reply=reply)
    out = bot.prm.decide(third["id"], "execute")
    assert not out["ok"] and "结果不确定" in out["error"]
    assert [a["id"] for a in snap(bot)["platform_alerts"]] == [first["id"]]
    assert bot.prm.decide(first["id"], "ack")["released"] == third["id"]
    approve(bot, third)
    out = bot.prm.decide(second["id"], "execute")
    assert not out["ok"] and "正在执行" in out["error"] and f"#{third['id']}" in out["error"]


def test_a_check_that_finds_it_on_the_platform_lets_nothing_run(bot, clock):
    """It is there: the operator drops the draft that asked, then marks the red one 「我已处理」 as it says.  A later
    draft of the same product is asked about again; nothing reaches the Mac."""
    first, reply = maybe_opened(bot, clock, DESIGNATED, record("ACCEPTED_UNVERIFIED"))
    second = drafted(bot, reply=reply)
    assert not bot.prm.decide(second["id"], "execute")["ok"]
    assert bot.prm.decide(second["id"], "drop")["ok"] and bot.prm.decide(first["id"], "ack")["ok"]
    third = drafted(bot, reply=reply)
    jobs = len(bot.relay.store.list_jobs(100))
    out = bot.prm.decide(third["id"], "execute")
    assert not out["ok"] and "结果不确定" in out["error"] and len(bot.relay.store.list_jobs(100)) == jobs


def test_not_run_says_the_platform_was_not_touched_and_may_be_approved_again(bot, clock):
    """The Mac cancelled the execution while it waited for the platform command lock (NOT_RUN, nothing ran): the red
    mark says the platform was not touched and a draft of it may be approved again, not 「请到平台后台核对」; and a
    second draft of the same 专属 action is approved at once (nothing to check)."""
    rec = record("NOT_RUN")
    rec["problems"] = ["等平台命令锁的时候任务被取消了，没有执行"]
    first, reply = maybe_opened(bot, clock, DESIGNATED, rec)
    assert first["status"] == "failed" and first["outcome"] == "platform_not_run"
    assert "平台没有动" in first["error"] and "可以重新批准" in first["error"] and "等平台命令锁" in first["error"]
    assert "请到平台后台核对" not in first["error"] and "平台上可能已经开了" not in first["error"]
    assert f"#{first['id']} 平台动作没有执行" in bot.prm.store.deal(1)["attention"]
    assert approve(bot, drafted(bot, reply=reply))


@pytest.mark.parametrize("rec", [lambda: record("FAILED"), lambda: record("STATE_CONFLICT"), lambda: record("NOT_RUN")],
                         ids=["failed", "state-conflict", "not-run"])
def test_a_result_that_wrote_nothing_does_not_stop_a_second_one(bot, clock, rec):
    """The platform refused it, it was not sent, it did not run: nothing to check, a second draft may run."""
    first, reply = maybe_opened(bot, clock, DESIGNATED, rec())
    assert "平台上可能已经开了" not in first["error"]
    assert approve(bot, drafted(bot, reply=reply))


@pytest.mark.parametrize("raw, again", [
    ({"action": "dy.channel_goods", "item_id": DY_ITEM, "creator_platform_id": DY_UID, "price": "15"},
     {"action": "dy.channel_goods", "item_id": DY_ITEM, "creator_platform_id": DY_UID, "price": "14"}),
    ({"action": "xhs.direct_plan", "item_id": XHS_ITEM, "creator_platform_id": "小美的号", "commission_rate": 15},
     {"action": "xhs.direct_plan", "item_id": XHS_ITEM, "creator_platform_id": XHS_BUYER, "commission_rate": 12}),
], ids=["channel-goods", "xhs-plan-by-name-then-by-id"])
def test_a_channel_product_or_a_plan_is_not_made_twice_either(bot, clock, raw, again):
    run = {"steps": ["create"], "buyer_id": XHS_BUYER, "name": "达人合作1-1027100000"} if raw["action"] == "xhs.direct_plan" \
        else None
    first = opened(bot, clock, raw, run=run, rec=record(link=DY_LINK, plan_name="达人合作1-1027100000",
                                                        group_id="6a0000000000000000000a01"), reply="好啦")
    assert first["status"] == "sent"
    second = drafted(bot, block(again, run=run and {**run, "name": "达人合作1-1027100500"}), reply="好啦")
    out = bot.prm.decide(second["id"], "execute")
    assert not out["ok"] and f"#{first['id']}" in out["error"]


# ── a product the invite card opened already (邀约卡的 25% 专属佣金，2026-10-29) ──
# douyin-engineering's `buyin author-invite --commission-rate 25` (the batch invite tool, tools/creator_invite) makes
# the platform give the creator a long-term 指定达人佣金 strategy for the card's products at once.  The chain must not
# open those again: the Mac's preview reads the invite ledger and says so (``covered``).

COVER = {"by": "invite", "rate": 25, "at": "2026-10-28T15:00:00+08:00", "aweme_id": "test_daren_01",
         "attempt": "9f1c00000001", "outcome": "VERIFIED_SUCCESS"}


def covered(item, cover=COVER):
    """The Mac's preview of a 专属佣金 the invite card opened already: blocked, ``covered`` says by what."""
    return block({**DESIGNATED, "item_id": item}, verdict="blocked", run={}, covered=cover,
                 problems=[f"这位达人的商品 {item} 已经由邀约卡开过 25% 专属佣金，不用再开"])


def test_a_product_the_invite_card_opened_counts_as_opened_and_the_chain_goes_on(bot, clock):
    """衔接：寄样单签收，开佣金的链条派到一款邀约卡（带 25% 专属佣金）已经开过的商品，Mac 的预览认出来：这份草稿不进
    待审批、平台上什么都不做，链条当它开好了、马上派下一款；都开过了这一轮照常收尾；合作单写明是邀约卡开的。"""
    order = three_signed_for(bot, clock)
    job = bot.worker_takes()
    bot.finish(job["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": covered(DY_ITEM)})
    first = bot.prm.store.packet_for_job(job["id"])
    assert first["status"] == "dropped" and first["outcome"] == "covered" and first["decided_at"] == clock.now
    assert "邀约卡" in first["reason"] and bot.prm.store.packets(("pending",)) == []
    assert not bot.prm.decide(first["id"], "execute")["ok"]
    second = bot.worker_takes()                                              # the chain goes on at once
    note = second["meta"]["system_note"]
    assert f"商品 {DY_ITEM} 已经由邀约卡开过 25% 专属佣金" in note and f"这一份草稿只填商品 {DY_ITEM2} 的" in note
    assert f"邀约卡开过（当作开好）：{DY_ITEM}" in note
    assert f"开好 1/3 个（其中邀约卡开过 {DY_ITEM}）" in chain_attention(bot)
    ev = next(e for e in bot.prm.store.events(order["deal_id"], limit=30) if e["kind"] == "commission_covered")
    assert ev["detail"]["item"] == DY_ITEM and ev["actor"] == "mac"
    assert event_out(ev)["name"] == f"商品 {DY_ITEM} 邀约卡开过 25% 专属佣金，当作开好"
    bot.finish(second["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block(
        {**DESIGNATED, "item_id": DY_ITEM2})})
    step2 = bot.prm.store.packet_for_job(second["id"])
    mac_runs(bot, approve(bot, step2), record())                             # one opened the usual way
    bot.prm.send_approved()
    third = bot.worker_takes()
    bot.finish(third["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": covered(DY_ITEM3)})
    final = bot.worker_takes()                                               # all open: the round is done, once
    assert "这一轮没有要开的商品了" in final["meta"]["system_note"]
    assert f"已开好：{DY_ITEM2}" in final["meta"]["system_note"]
    bot.finish(final["id"], draft(message_kind="other", reply_draft="", slots={}))
    assert bot.worker_takes() is None and chain_attention(bot) == ""
    executions = [j for j in bot.relay.store.list_jobs(100) if '"platform_execute"' in j["meta"]]
    assert len(executions) == 2                                              # the order and DY_ITEM2 only


def test_a_reply_that_carries_a_commission_the_invite_card_opened_keeps_its_words_without_the_action(bot, clock):
    packet = drafted(bot, preview=covered(DY_ITEM), reply="专属佣金给你开好啦～")
    assert packet["tool"] == "message.send" and packet["status"] == "pending" and packet["needs_attention"]
    assert "邀约卡开过 25%" in packet["reason"] and "发之前请核对" in packet["reason"]
    assert "邀约卡开过 25%" in bot.prm.store.deal(packet["deal_id"])["attention"]


def test_the_creators_douyin_id_goes_with_her_jobs_so_the_mac_can_look_her_up(bot, clock):
    order = ordered(bot, clock)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER, creator_uid=DY_UID,
                                              creator_account="test_daren_01"))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    step = bot.worker_takes()
    assert step["meta"]["creator"] == {"douyin_ids": ["test_daren_01"], "uid": DY_UID}
    bot.relay.store.cancel_if_pending(step["id"], clock.now)
    bot.prm.handle(bot.msg("佣金什么时候生效呀"))
    assert bot.worker_takes()["meta"]["creator"]["douyin_ids"] == ["test_daren_01"]


def test_an_order_from_before_finds_her_douyin_id_in_the_clis_own_answer(bot, clock):
    from platform_fakes import jst_done
    order = ordered(bot, clock)
    rec = record(sample_order_no=JST_ORDER, creator_uid=DY_UID)                  # no creator_account (an older worker)
    rec["steps"] = [{"name": "create", "label": "聚水潭 · 建寄样单", "execute": True, "command": "node bin/jst.mjs",
                     "exit_code": 0, "seconds": 3, "verdict": "", "outcome": "VERIFIED_SUCCESS", "problems": [],
                     "facts": [], "raw": json.dumps(jst_done(), ensure_ascii=False, indent=2)}]
    mac_runs(bot, approve(bot, order), rec)
    assert bot.prm._creator_ids(order["deal_id"]) == {"douyin_ids": ["test_daren_01"], "uid": DY_UID}


def test_an_approval_run_after_the_invite_card_went_out_is_not_executed_and_counts_as_opened(bot, clock):
    """The preview passed (no card yet); the card went out before the operator approved: the Mac finds it in the
    ledger when it would execute, runs nothing (NOT_RUN with ``covered``), and the bot counts the product as opened."""
    three_signed_for(bot, clock)
    _, first = step_drafts(bot, DY_ITEM)
    job = approve(bot, first)
    assert json.loads(job["meta"])["creator"]["uid"] == DY_UID
    rec = platforms.record("NOT_RUN", ["这位达人的商品已经由邀约卡开过 25% 专属佣金，没有执行"])
    rec["covered"] = COVER
    mac_runs(bot, job, rec)
    done = bot.prm.store.packet(first["id"])
    assert done["status"] == "dropped" and done["outcome"] == "covered"
    assert bot.prm.snapshot()["platform_alerts"] == []                       # not red: nothing to check
    second = bot.worker_takes()
    assert f"这一份草稿只填商品 {DY_ITEM2} 的" in second["meta"]["system_note"]
    assert f"开好 1/3 个（其中邀约卡开过 {DY_ITEM}）" in chain_attention(bot)


def test_a_linked_douyin_id_lets_the_mac_find_her_invite_card_when_the_order_had_only_her_uid(bot, clock, tmp_path):
    """加百应达人微信 (功能 R, design §8.4): her sample order gave only her UID; the 抖音号 an operator linked her to
    goes with the 专属佣金 step (meta.creator), so the Mac's look at the invite ledger finds the card that opened it."""
    assert bot.prm.register(CREATOR, "小美", link={"account": "test_daren_01", "source": "friend_add"})["linked"]
    three_signed_for(bot, clock)
    creator = bot.worker_takes()["meta"]["creator"]
    assert creator == {"douyin_ids": ["test_daren_01"], "uid": DY_UID}
    ledger = tmp_path / "creator-invites" / "invites.jsonl"
    ledger.parent.mkdir()
    rows = [{"at": "2026-10-28T15:00:00+08:00", "event": "attempt", "attempt": "9f1c00000001", "plan_id": "P1",
             "aweme_id": "test_daren_01", "products": [DY_ITEM, DY_ITEM2], "commission_rate": 25, "category": "small"},
            {"at": "2026-10-28T15:00:04+08:00", "event": "result", "attempt": "9f1c00000001", "plan_id": "P1",
             "aweme_id": "test_daren_01", "exit": 0, "outcome": "VERIFIED_SUCCESS", "sent": True}]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    cover = platforms.invite_cover(ledger.parent, DY_ITEM, douyin_ids=creator["douyin_ids"], uid=creator["uid"])
    assert cover["state"] == "covered" and cover["rate"] == 25
    assert platforms.invite_cover(ledger.parent, DY_ITEM, douyin_ids=[], uid=creator["uid"]) is None   # without it
