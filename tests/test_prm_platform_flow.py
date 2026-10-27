"""Platform actions on the bot's side (src/prm/service.py): a draft that carries one waits for a
person whatever its kind's gate; 批准并执行 queues one execution on the Mac; only a read-back
VERIFIED_SUCCESS sends the reply (the link filled in); everything else, a Mac that never takes
the job or goes silent, ends red without a word to the creator."""

import json

import pytest

from test_prm import CREATOR, Bot, Clock, draft

from platform_fakes import DY_ITEM, DY_LINK, DY_SKU, DY_UID, JST_ORDER, XHS_BUYER, XHS_ITEM
from src.prm import platforms
from src.prm.result import DraftError, parse_draft
from src.prm.service import APPROVED_EXPIRE_SEC, PLATFORM_LOST_GRACE_SEC, SETTLE_SEC
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
    for _ in range(3):
        packet = drafted(bot, kind="other")
        mac_runs(bot, approve(bot, packet), record())
        bot.prm.send_approved()
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
    other = drafted(bot, block(raw, run=plan), reply="定向计划「{计划名}」开好啦，编号 {计划ID}")
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


# ── a created sample order → its 专属佣金 (drafted, then approved like any platform write) ──

def ordered(b, clock, reply="寄样单建好啦"):
    """A sample order drafted, its receiver's phone known (〈手机1〉)."""
    packet = drafted(b, preview=block(SAMPLE, run={"steps": ["create"]}), reply=reply)
    b.prm.store.token_for(packet["party_id"], "phone", "手机", "13900001111", clock.now)
    return packet


def test_a_created_sample_order_asks_for_a_draft_of_its_commission(bot, clock):
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
    packet = ordered(bot, clock)
    mac_runs(bot, approve(bot, packet), record("ACCEPTED_UNVERIFIED", sample_order_no=JST_ORDER))
    assert bot.worker_takes() is None


def test_a_sample_order_done_during_a_takeover_leaves_the_commission_to_a_person(bot, clock):
    packet = ordered(bot, clock)
    job = approve(bot, packet)
    bot.prm.deal_action("mode", {"deal_id": packet["deal_id"], "mode": "human"})
    mac_runs(bot, job, record(sample_order_no=JST_ORDER))
    assert bot.worker_takes() is None
    assert "该开专属佣金了" in bot.prm.store.deal(packet["deal_id"])["attention"]


def test_only_a_sample_order_and_a_commission_have_a_next_step():
    from src.prm.service import next_step_note
    order = platforms.check_action({**SAMPLE, "shop": "xhs", "item_id": XHS_ITEM, "skus": [{"sku_id": "a" * 24, "qty": 1}],
                                    "creator_platform": "xhs"})
    assert "xhs.direct_plan" in next_step_note(order, record(sample_order_no=JST_ORDER))
    assert next_step_note(platforms.check_action(CHANNEL), record(link=DY_LINK)) == ""
