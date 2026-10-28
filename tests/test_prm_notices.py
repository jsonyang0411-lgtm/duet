"""What a deal says to a person stays until it is dealt with (mac review F1 and its addition).

The deal's ``attention`` is its notices (src/prm/store.py): the latest draft's own notes, which the
next draft replaces, and the system's, which only go when what they say is seen to.  A draft job that
gave no draft keeps the creator's lines for her next draft job, so her message is answered, not lost;
a 专属佣金 step that came up while a person had the deal (or the creator was off the list) is owed and
asked for when that ends; a red platform action stays until 已处理; her recall until an operator decided.
"""

import json
import sqlite3

import pytest

from test_prm import CREATOR, Bot, Clock, draft
from test_prm_platform_flow import DY_ITEM, JST_ORDER, approve, block, mac_runs, ordered, record

from src.prm.service import event_out
from src.prm.store import PrmStore

HOUR = 3600


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


def attention(b, deal_id=1):
    return b.prm.store.deal(deal_id)["attention"]


def lines_of(job):
    """A job's lines without their "[10-27 09:15] " time."""
    return [line.split("] ", 1)[1] for line in job["text"].split("\n")]


def failed(b, text="佣金能给到多少？另外样品能寄两顶吗", error="claude 没有给出结构化结果"):
    """The creator writes, the Mac takes the job and fails it."""
    b.prm.handle(b.msg(text))
    job = b.worker_takes()
    b.relay.finish(job["id"], {"ok": False, "text": "", "error": error})
    b.relay.tick()
    return job


# ── a draft that was not written ──────────────────────────────────────────

def test_a_message_whose_draft_was_not_written_is_answered_with_her_next_one(bot, clock):
    """The Mac failed on her question; ten minutes later she writes 「在吗」: the new job carries the
    question before it, and its draft sees to the reminder.  Before, the job had only 「在吗」 and its
    draft's notes wiped 「草稿没写成」: the question was gone."""
    first = failed(bot)
    assert attention(bot).startswith(f"#{first['id']} 草稿没写成：claude 没有给出结构化结果")
    assert "会并进她下一条消息的草稿" in attention(bot)
    [ev] = bot.prm.store.events(1, kinds=("draft_failed",))
    assert event_out(ev)["name"] == f"#{first['id']} 草稿没写成"
    clock.now += 600
    bot.prm.handle(bot.msg("在吗"))
    job = bot.worker_takes()
    assert lines_of(job) == ["佣金能给到多少？另外样品能寄两顶吗", "在吗"] and job["id"] != first["id"]
    assert f"已经并进 #{job['id']} 一起起草" in attention(bot)
    bot.finish(job["id"], draft(message_kind="money", reply_draft="佣金 20%，样品可以寄两顶～", slots={}))
    assert "草稿没写成" not in attention(bot)
    assert f"#{job['id']} 的草稿也回了 #{first['id']} 没写成时她的消息" in attention(bot)
    bot.prm.handle(bot.msg("好的"))
    later = bot.worker_takes()
    assert lines_of(later) == ["好的"]                                     # carried once


def test_a_draft_of_something_else_does_not_wipe_the_reminder(bot, clock, tmp_path):
    """A draft that did not take her lines along (its job was made before the failure, a picture's job)
    leaves the reminder; her next message takes the lines."""
    from test_prm import a_picture
    bot.prm.handle(bot.msg("佣金能给到多少？"))
    first = bot.worker_takes()
    with a_picture(tmp_path):
        bot.prm.handle(bot.msg("[图片]", msg_type=3))                       # a job of its own, made now
    bot.relay.finish(first["id"], {"ok": False, "text": "", "error": "超时"})
    bot.relay.tick()
    picture = bot.worker_takes()
    bot.finish(picture["id"], draft(message_kind="other", reply_draft="图片收到啦", slots={}, needs_attention=True,
                                    reason="图片看不清"))
    assert attention(bot) == (f"#{first['id']} 草稿没写成：超时；她这几条消息会并进她下一条消息的草稿一起回，"
                              "也可以直接在微信里回她；图片看不清")             # the system's first
    clock.now += 60
    bot.prm.handle(bot.msg("在吗"))
    assert lines_of(bot.worker_takes()) == ["佣金能给到多少？", "在吗"]


@pytest.mark.parametrize("how", ["failed", "lost", "unusable"])
def test_whatever_kept_the_draft_from_being_written_her_lines_are_not_lost(bot, clock, how):
    bot.prm.handle(bot.msg("样品什么时候寄"))
    job = bot.worker_takes()
    if how == "lost":
        clock.now += 1000                                                    # no heartbeat: lost
    elif how == "failed":
        bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "claude 退出码 1"})
    else:
        bot.relay.finish(job["id"], {"ok": True, "text": "我来回复：在的！"})
    bot.relay.tick()
    word = {"failed": "草稿没写成：claude 退出码 1", "lost": "草稿没写成：Mac 失联了", "unusable": "草稿格式不对（not_json）"}[how]
    assert word in attention(bot)
    bot.prm.handle(bot.msg("？"))
    assert lines_of(bot.worker_takes()) == ["样品什么时候寄", "？"]


def test_what_the_failed_job_was_told_goes_along_too(bot, clock):
    """A job told what both sides said during a takeover (``history``) and read late (``late``): its
    failure keeps that for the next job."""
    bot.prm.deal_action("mode", {"deal_id": 1, "mode": "paused"})
    bot.prm.handle(bot.msg("暂停时说的"))
    bot.prm.deal_action("mode", {"deal_id": 1, "mode": "copilot"})
    bot.prm.handle(bot.msg("一小时前的话", ts=clock.now - HOUR))
    job = bot.worker_takes()
    assert job["meta"]["history"] and job["meta"]["late"]
    bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "超时"})
    bot.relay.tick()
    clock.now += 60
    bot.prm.handle(bot.msg("现在的话"))
    nxt = bot.worker_takes()
    assert [h["text"].split("] ", 1)[1] for h in nxt["meta"]["history"]] == ["暂停时说的"]
    assert nxt["meta"]["late"] == job["meta"]["late"] and lines_of(nxt) == ["一小时前的话", "现在的话"]
    bot.finish(nxt["id"], draft(message_kind="other", reply_draft="好的", slots={}))
    assert bot.prm.store.packet_for_job(nxt["id"])["status"] == "pending"   # read late: reviewed


def test_follow_ups_the_mac_read_go_along_and_the_ones_it_never_saw_are_a_job_of_their_own(bot, clock):
    bot.prm.handle(bot.msg("第一句"))
    job = bot.worker_takes()
    bot.prm.handle(bot.msg("第二句"))                                       # a follow-up of the running job
    [seen] = bot.relay.store.followups(job["id"], 0)
    bot.relay.store.followups(job["id"], seen["id"])                        # the Mac handed it on
    bot.prm.handle(bot.msg("第三句"))                                       # not seen before the end
    bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "超时"})
    bot.relay.tick()
    leftover = bot.worker_takes()
    assert lines_of(leftover) == ["第三句"]                                 # the relay's own leftover job
    bot.finish(leftover["id"], draft(message_kind="other", reply_draft="收到", slots={}))
    assert "草稿没写成" in attention(bot)                                    # it did not take the first two
    bot.prm.handle(bot.msg("第四句"))
    assert lines_of(bot.worker_takes()) == ["第一句", "第二句", "第四句"]


def test_a_failed_job_back_after_its_lines_went_on_is_out_of_date(bot, clock):
    """Lost, her lines went into the next job, whose draft came first; then the Mac came back with the
    first job's draft: it does not replace the newer one."""
    bot.prm.handle(bot.msg("样品什么时候寄"))
    first = bot.worker_takes()
    clock.now += 1000
    bot.relay.tick()                                                         # lost
    bot.prm.handle(bot.msg("在吗"))
    second = bot.worker_takes()
    bot.finish(second["id"], draft(message_kind="other", reply_draft="明天寄哈", slots={}))
    newer = bot.prm.store.packet_for_job(second["id"])
    assert bot.relay.finish(first["id"], {"ok": True, "text": json.dumps(draft(reply_draft="在的"), ensure_ascii=False)})
    bot.relay.tick()
    assert bot.prm.store.packet_for_job(first["id"])["status"] == "superseded"
    assert bot.prm.store.packet(newer["id"])["status"] == "pending"


def test_a_lost_job_back_before_her_next_message_sees_to_its_own_reminder(bot, clock):
    bot.prm.handle(bot.msg("样品什么时候寄"))
    first = bot.worker_takes()
    clock.now += 1000
    bot.relay.tick()
    assert "Mac 失联了" in attention(bot)
    bot.relay.heartbeat(first["id"])                                         # the Mac is back
    bot.finish(first["id"], draft(message_kind="other", reply_draft="明天寄哈", slots={}))
    assert bot.prm.store.packet_for_job(first["id"])["status"] == "pending" and "草稿没写成" not in attention(bot)
    bot.prm.handle(bot.msg("好的"))
    assert lines_of(bot.worker_takes()) == ["好的"]


def test_an_operator_answering_her_by_hand_puts_the_reminder_away(bot, clock):
    """Seen to by a person: not shown any more; her lines still go to the next draft job (the case file
    must hear of them)."""
    failed(bot, "样品什么时候寄")
    clock.now += 60
    bot.prm.on_own_message({"chat_id": CREATOR, "sender_id": "wxid_me", "content": "今天下午寄", "msg_type": 1,
                            "is_group": False, "timestamp": clock.now})
    assert "草稿没写成" not in attention(bot) and bot.prm.store.deal(1)["mode"] == "human"
    bot.prm.deal_action("mode", {"deal_id": 1, "mode": "copilot"})
    bot.prm.handle(bot.msg("好的"))
    job = bot.worker_takes()
    assert lines_of(job) == ["样品什么时候寄", "好的"]
    assert [h["who"] for h in job["meta"]["history"]] == ["operator"]


@pytest.mark.parametrize("when", ["queued", "running"])
def test_a_job_an_operator_cancelled_leaves_her_lines_for_the_next_one(bot, clock, when):
    """Cancelled in the console (before the Mac took it, or while it ran): the lines it took along wait
    for her next draft job again."""
    failed(bot, "样品什么时候寄")
    bot.prm.handle(bot.msg("在吗"))
    carrying = bot.relay.store.latest_open(CREATOR)
    if when == "queued":
        assert bot.relay.cancel_from_console(carrying["id"]) == "cancelled"
    else:
        assert bot.worker_takes()["id"] == carrying["id"]
        assert bot.relay.cancel_from_console(carrying["id"]) == "requested"
        assert bot.relay.finish(carrying["id"], {"ok": False, "text": "", "error": "cancelled"})
        bot.relay.tick()
        assert "会并进她下一条消息的草稿" in attention(bot)
    clock.now += 60
    bot.prm.handle(bot.msg("？"))
    job = bot.worker_takes()
    assert lines_of(job)[0] == "样品什么时候寄" and lines_of(job)[-1] == "？"
    assert f"已经并进 #{job['id']} 一起起草" in attention(bot)


# ── a 专属佣金 step owed ──────────────────────────────────────────────────

def signed_for(b, clock):
    """A sample order made and verified, its reply sent: the deal only waits for the signature."""
    order = ordered(b, clock)
    mac_runs(b, approve(b, order), record(sample_order_no=JST_ORDER))
    b.prm.send_approved()
    return order


def step_jobs(b):
    return [j for j in b.relay.store.list_jobs(50) if "system_note" in j["meta"]]


def test_a_step_owed_during_a_takeover_is_asked_for_when_it_runs_out(bot, clock):
    """mac review F1 case A: signed for while a person had the chat.  The reminder outlives the drafts
    after it; when the takeover runs out the step is asked for (before, nobody asked again)."""
    order = signed_for(bot, clock)
    assert bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "human"})["ok"]
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    assert step_jobs(bot) == [] and f"#{order['id']} 之后该开专属佣金了：现在不在副驾驶模式" in attention(bot)
    bot.prm.tick()
    assert step_jobs(bot) == []                                             # still a person's
    clock.now += bot.prm._takeover_sec + 60
    bot.prm.handle(bot.msg("收到啦，很好看"))
    reply = bot.worker_takes()
    bot.finish(reply["id"], draft(message_kind="other", reply_draft="戴上效果怎么样～", slots={}))
    assert "该开专属佣金了" in attention(bot)                                # the reply's draft does not wipe it
    bot.prm.tick()
    [step] = step_jobs(bot)
    meta = json.loads(step["meta"])
    assert "达人已经签收了寄样单" in meta["system_note"]
    assert meta["step"] == {"packet": order["id"], "delivered": True, "item": DY_ITEM}
    assert "该开专属佣金了" not in attention(bot)
    bot.prm.tick()
    assert len(step_jobs(bot)) == 1                                         # asked once


@pytest.mark.parametrize("back", ["mode", "active"])
def test_a_step_owed_is_asked_for_when_the_deal_is_back(bot, clock, back):
    """Switched back to 副驾驶 in the console, or the creator on the list again."""
    order = signed_for(bot, clock)
    if back == "mode":
        bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "paused"})
    else:
        bot.prm.set_active(order["party_id"], False)
    assert bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    bot.prm.tick()
    assert step_jobs(bot) == [] and "该开专属佣金了" in attention(bot)
    if back == "mode":
        bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "copilot"})
    else:
        bot.prm.set_active(order["party_id"], True)
    bot.prm.tick()
    assert len(step_jobs(bot)) == 1 and "该开专属佣金了" not in attention(bot)


def test_a_step_waiting_for_the_signature_is_not_owed_once_the_deal_is_set_back(bot, clock):
    order = signed_for(bot, clock)
    bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "human"})
    bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})
    bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "transit"})   # signed for by mistake
    bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "copilot"})
    bot.prm.tick()
    assert step_jobs(bot) == [] and "该开专属佣金了" not in attention(bot)


def test_a_step_drafted_while_a_person_took_over_is_asked_for_again_afterwards(bot, clock):
    order = signed_for(bot, clock)
    bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})
    step = bot.worker_takes()
    bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "human"})           # meanwhile
    bot.finish(step["id"], {**draft(message_kind="money", reply_draft="", slots={}), "platform": block()})
    assert bot.prm.store.packet_for_job(step["id"])["status"] == "superseded"
    assert f"#{step['id']} 开专属佣金这一步的草稿没进待审批：现在不在副驾驶模式" in attention(bot)
    bot.prm.deal_action("mode", {"deal_id": order["deal_id"], "mode": "copilot"})
    bot.prm.tick()
    again = [j for j in step_jobs(bot) if j["id"] != step["id"]]
    assert len(again) == 1 and json.loads(again[0]["meta"])["system_note"] == step["meta"]["system_note"]


def test_a_step_that_was_not_written_says_so_until_the_chain_goes_on(bot, clock):
    order = signed_for(bot, clock)
    bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})
    step = bot.worker_takes()
    bot.relay.finish(step["id"], {"ok": False, "text": "", "error": "超时"})
    bot.relay.tick()
    assert f"#{step['id']} 开专属佣金这一步的草稿没写成：超时" in attention(bot)
    bot.prm.handle(bot.msg("收到啦"))
    reply = bot.worker_takes()
    assert lines_of(reply) == ["收到啦"]                                    # a step has no lines of hers
    bot.finish(reply["id"], draft(message_kind="other", reply_draft="好的～", slots={}))
    assert "开专属佣金这一步的草稿没写成" in attention(bot)
    for stage in ("transit", "delivered"):                                  # kb 2.1: picked up again
        bot.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": stage})
    assert "开专属佣金这一步的草稿没写成" not in attention(bot) and len(step_jobs(bot)) == 2


# ── a red platform action, her recall ─────────────────────────────────────

def test_a_red_platform_action_stays_on_the_deal_until_it_is_dealt_with(bot, clock):
    from test_prm_platform_flow import drafted
    packet = drafted(bot)
    mac_runs(bot, approve(bot, packet), record("FAILED"))
    assert f"#{packet['id']} 平台动作没有核验成功" in attention(bot)
    bot.prm.handle(bot.msg("好了吗"))
    bot.finish(bot.worker_takes()["id"], draft(message_kind="other", reply_draft="我看一下", slots={}))
    assert f"#{packet['id']} 平台动作没有核验成功" in attention(bot)
    assert bot.prm.decide(packet["id"], "ack")["ok"]
    assert "平台动作没有核验成功" not in attention(bot)


def test_her_recall_stays_until_an_operator_decided_and_holds_the_drafts_meanwhile(bot, clock):
    bot.prm.store.set_gate("other", "auto", clock.now)
    recall = bot.msg("[消息类型:10000]", msg_type=10000, special={"kind": "system", "title": '"小美" 撤回了一条消息'})
    bot.prm.handle(recall)
    assert attention(bot).startswith("达人撤回了一条消息")
    clock.now += 60
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    bot.finish(job["id"], draft(message_kind="other", reply_draft="你好呀", slots={}))
    first = bot.prm.store.packet_for_job(job["id"])
    assert first["status"] == "pending" and first["needs_attention"]        # nobody has seen to it yet
    assert attention(bot).startswith("达人撤回了一条消息")
    assert bot.prm.decide(first["id"], "send")["ok"]
    assert "撤回" not in attention(bot)
    clock.now += 60
    bot.prm.handle(bot.msg("在吗"))
    later = bot.worker_takes()
    bot.finish(later["id"], draft(message_kind="other", reply_draft="在的", slots={}))
    assert bot.prm.store.packet_for_job(later["id"])["status"] == "approved"   # its gate again


# ── the clean-up, an older database ───────────────────────────────────────

def test_the_clean_up_cuts_what_a_notice_keeps(bot, clock):
    failed(bot, "地址：上海市浦东新区张江路100号3栋1201，电话 13800138000")
    deal = bot.prm.store.deal(1)
    [notice] = [n for n in deal["notices"] if n["key"].startswith("failed:")]
    assert "张江路100号" in notice["lines"] and "13800138000" not in notice["lines"]
    bot.prm.purge(deal, clock.now)
    [notice] = [n for n in bot.prm.store.deal(1)["notices"] if n["key"].startswith("failed:")]
    assert "张江路" not in notice["lines"] and "上海市" in notice["lines"]


def test_a_database_from_before_the_notices_keeps_its_attention(tmp_path):
    path = tmp_path / "prm.db"
    store = PrmStore(path)
    party = store.add_party(CREATOR, "小美", now=1)
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE deals SET attention = '旧版写的提醒', notices = '[]'")
    conn.commit()
    conn.close()
    store = PrmStore(path)
    deal = store.deal_of(party["id"])
    assert deal["attention"] == "旧版写的提醒" and deal["notices"][0]["text"] == "旧版写的提醒"
    store.set_notice(deal["id"], "missed", "两条没起草", now=2)
    assert store.deal(deal["id"])["attention"] == "两条没起草；旧版写的提醒"
    store.set_notice(deal["id"], "draft:reply", "", now=3)                  # the next draft's (no notes)
    assert store.deal(deal["id"])["attention"] == "两条没起草"
