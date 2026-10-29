"""抖音百应私信（PRM_BUYIN_SYNC_MIN，src/prm/service.py，机器人这一侧）：机器人定时请 Mac 读一次百应私信（不跑模型、只读，
Mac 端见 tests/test_prm_buyin_worker.py），登记达人的新话（按达人名单的抖音号 / 平台 UID、核验成功的寄样单里的抖音号认人，
不按昵称）打码后交给 Mac 起草，草稿在待审批里是一张「百应私信 · 运营手动发」：复制（带完整号码，记一笔）/ 已在百应发了 /
不发，机器人哪儿都不发它。陷阱：不过门槛、不回微信「稍等」、不能标发错了、和微信草稿互不顶替、不带进微信的草稿任务、
人工接管时只记不起草、手动发不算接管、清理覆盖百应的任务；没登记的不建达人；开关默认关；读不了时说一次、照间隔再问。
Mac 交回来的是 ``platforms.im_result`` 的形状。"""

import json
from types import SimpleNamespace

import pytest

from test_prm import CREATOR, Bot, Clock, config, draft
from test_prm_sample_sync import verified_order

from src.config import BotConfig, _validate_config
from src.prm import deals, gates, platforms, retention
from src.prm.service import (BUYIN_CHAT, BUYIN_FIRST_SEC, BUYIN_MAX_AGE_SEC, BUYIN_RISK_PAUSE_SEC, BUYIN_SYNC_CHAT,
                             BUYIN_UNMATCHED_DAYS, PrmService, event_out)
from src.prm.store import BUYIN_TOOL, PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import PRM, RelayStore

EPOCH = 1_790_000_000.0        # 百应's times before 2001 are dropped (platforms.jst_time)
UID = "5100000000000002"
AWEME = "xiaomei_dy"
CID = "conv_7001"
PHONE = "13812345678"
INTERVAL = 600


@pytest.fixture
def clock():
    return Clock(EPOCH)


def listed(*rows):
    """The 达人名单 as Bot._wire_prm_directory hands it to the PRM."""
    return lambda: [dict(r) for r in rows]


@pytest.fixture
def bot(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm._buyin_sec = INTERVAL                          # PRM_BUYIN_SYNC_MIN=10 (the tests' config objects: off)
    b.prm.register(CREATOR, "小美", "测试达人")
    b.prm.creator_directory = listed({"wxid": CREATOR, "platform": "DY", "platform_no": AWEME, "platform_uid": UID})
    yield b
    b.prm.stop()
    b.relay.stop()


def line(mid, text, at, frm="creator", kind="text"):
    return {"id": mid, "at": at, "from": frm, "kind": kind, "text": text}


def conv(*messages, cid=CID, uid=UID, aweme="", nickname="小美的抖音"):
    return {"conversationId": cid, "creator": {"uid": uid, "awemeId": aweme, "nickname": nickname},
            "messages": list(messages)}


def jobs_of(b, chat):
    return [j for j in b.relay.store.list_jobs(500) if j["chat_id"] == chat]


def next_round(b, clock, advance=INTERVAL + 1):
    """The clock moves on, the sender thread ticks: the round's poll job, or None when none was queued."""
    before = {j["id"] for j in jobs_of(b, BUYIN_SYNC_CHAT)}
    clock.now += advance
    b.prm.tick()
    new = [j for j in jobs_of(b, BUYIN_SYNC_CHAT) if j["id"] not in before]
    return new[0] if new else None


def claim(b, job_id):
    """The worker takes this very job (whatever else is queued)."""
    with b.relay.store._lock:
        b.relay.store._conn.execute("UPDATE relay_jobs SET status = 'running', claimed_at = ?, heartbeat_at = ?"
                                    " WHERE id = ? AND status = 'pending'", (b.clock.now, b.clock.now, job_id))
        b.relay.store._conn.commit()


def mac_reads(b, job, *convs, cursor=None, stopped=None, ok=True, error=""):
    """The worker hands back what `dy buyin im-inbox` said."""
    claim(b, job["id"])
    result = platforms.im_result(conversations=[platforms.im_conversation(c) for c in convs], cursor=cursor,
                                 stopped=stopped)
    body = {"ok": ok, "text": json.dumps(result, ensure_ascii=False) if ok else "", "duration_s": 1,
            **({"error": error} if error else {})}
    assert b.relay.finish(job["id"], body)
    b.relay.tick()


def round_says(b, clock, *convs, **kw):
    job = next_round(b, clock)
    assert job is not None
    mac_reads(b, job, *convs, **kw)
    return job


def mac_drafts(b, job, **overrides):
    """The Mac drafts the 百应 reply of a draft job: its packet."""
    claim(b, job["id"])
    b.finish(job["id"], draft(**{"intent": "问佣金", "slots": {}, "stage_proposal": "", "message_kind": "faq",
                                 "reply_draft": "你好呀，这款佣金是 20% 哦", "reason": "kb/合作政策.md", **overrides}))
    return b.prm.store.packet_for_job(job["id"])


def buyin_draft(b, clock, text=f"你好，佣金多少？我电话 {PHONE}", mid="m2", **overrides):
    """A round with her new line, and the Mac's draft of it: (draft job, packet)."""
    round_says(b, clock, conv(line(mid, text, clock.now)))
    [job] = [j for j in jobs_of(b, BUYIN_CHAT + CID) if j["status"] == "pending"]
    return job, mac_drafts(b, job, **overrides)


def deal(b):
    party = next(p for p in b.prm.store.parties(include_inactive=True) if p["wxid"] == CREATOR)   # (off the list too)
    return b.prm.store.deal_of(party["id"])


def buyin_events(b):
    return b.prm.store.events(deal(b)["id"], limit=None, kinds=("buyin_message",))


def wechat_job(b, text="在吗"):
    """Her WeChat message: its draft job (taken by the worker)."""
    b.prm.handle(b.msg(text))
    job = b.relay.store.latest_open(CREATOR, drafts_only=True)
    claim(b, job["id"])
    return job


# ── the switch ────────────────────────────────────────────────────────────

def test_the_switch_is_off_by_default_and_then_nothing_is_ever_queued(clock, tmp_path):
    assert BotConfig().prm_buyin_sync_min == 0
    _validate_config({"prm_buyin_sync_min": 0})
    _validate_config({"prm_buyin_sync_min": 5})
    with pytest.raises(RuntimeError, match="PRM_BUYIN_SYNC_MIN"):
        _validate_config({"prm_buyin_sync_min": 3})
    b = Bot(clock, tmp_path)                                         # a config object without the switch: off
    b.prm.register(CREATOR, "小美")
    for _ in range(5):
        clock.now += 3 * 3600
        b.prm.tick()
    assert jobs_of(b, BUYIN_SYNC_CHAT) == [] and b.prm.snapshot()["buyin_sync_min"] == 0
    assert b.prm.sweep_buyin() is None
    on = PrmService(SimpleNamespace(**vars(config()), prm_buyin_sync_min=15), b.relay, store=PrmStore(":memory:"),
                    clock=clock)
    assert on._buyin_sec == 900


def test_once_an_interval_one_read_only_job_asks_and_the_cursor_goes_with_the_next(bot, clock):
    assert next_round(bot, clock, BUYIN_FIRST_SEC - 10) is None                 # not right after the start
    job = next_round(bot, clock, 11)
    assert json.loads(job["meta"]) == {"op": "buyin_inbox", "cursor": None, "limit": platforms.IM_LIMIT}
    assert job["kind"] == "prm" and job["input_closed"] == 1 and CREATOR not in job["text"]
    mac_reads(bot, job, cursor={"after": "t1"})
    assert next_round(bot, clock, 60) is None                                   # not before the interval
    again = next_round(bot, clock)
    assert json.loads(again["meta"])["cursor"] == {"after": "t1"}
    assert next_round(bot, clock, 4 * INTERVAL) is None                         # the last one still open: none


# ── a registered creator's new lines ─────────────────────────────────────

def test_her_new_lines_are_recorded_masked_and_become_a_buyin_draft_job(bot, clock):
    poll = round_says(bot, clock, conv(line("m1", "邀约卡片：秋冬帽子专场", clock.now - 3600, "shop", "card"),
                                       line("m2", f"你好，佣金多少？我电话 {PHONE}", clock.now - 60)))
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    meta = json.loads(job["meta"])
    assert meta["channel"] == "buyin" and meta["conversation"] == CID and meta["buyin_name"] == "小美的抖音"
    assert meta["lines"] == 1 and "op" not in meta and job["input_closed"] == 1
    assert "〈手机1〉" in job["text"] and PHONE not in job["text"] and "佣金多少" in job["text"]
    assert meta["history"] == [{"who": "operator", "text": meta["history"][0]["text"]}]
    assert "[卡片] 邀约卡片" in meta["history"][0]["text"]
    texts = [ev["detail"]["text"] for ev in reversed(buyin_events(bot))]
    assert len(texts) == 2 and PHONE not in "".join(texts) and "〈手机1〉" in texts[1]
    assert {ev["actor"] for ev in buyin_events(bot)} == {"creator", "operator"}
    d = deal(bot)
    assert d["last_message_at"] is None and d["updated_at"] == clock.now      # a sign of life, not her WeChat round
    # the poll job keeps only a summary of what it read
    kept = bot.relay.store.get(poll["id"])["result_text"]
    assert PHONE not in kept and "佣金" not in kept and json.loads(kept)["summary"]["drafts"] == 1
    assert PHONE not in json.dumps(bot.prm.snapshot(), ensure_ascii=False)
    assert bot.texts == [] and bot.relay_sent == []                             # nothing goes anywhere


def test_the_draft_is_a_buyin_packet_that_always_waits_whatever_the_gate(bot, clock, monkeypatch):
    bot.prm.store.set_gate("faq", gates.AUTO, clock.now)                        # faq drafts go out by themselves
    monkeypatch.setattr(deals, "route_for", lambda *a, **k: pytest.fail("a 百应 draft never asks the gates"))
    _, packet = buyin_draft(bot, clock, platform_action={"action": "dy.designated_commission"},
                            attachments_to_send=["assets/brief.txt"], attachments_named=["assets/brief.txt"])
    assert packet["tool"] == BUYIN_TOOL and packet["status"] == "pending" and packet["route"] == "review"
    assert packet["chat_id"] == BUYIN_CHAT + CID and packet["platform"] == {} and packet["attachments"] == []
    assert packet["meta"]["channel"] == "buyin" and packet["meta"]["lane"] == "buyin"
    assert packet["meta"]["conversation"] == CID and "〈手机1〉" in packet["meta"]["said"]
    assert packet["needs_attention"] and "不带平台动作" in packet["reason"] and "不带附件" in packet["reason"]
    out = next(p for p in bot.prm.snapshot()["pending"] if p["id"] == packet["id"])
    assert out["channel"] == "buyin" and out["platform"] is None and out["buyin"]["name"] == "小美的抖音"
    assert out["legend"] == [{"token": "〈手机1〉", "masked": "138****5678"}]
    bot.prm.send_approved()
    assert bot.texts == [] and bot.prm.store.packet(packet["id"])["status"] == "pending"


# ── what an operator does with it ────────────────────────────────────────

def test_copy_gives_the_full_numbers_back_and_is_written_down(bot, clock):
    _, packet = buyin_draft(bot, clock, reply_draft="收到，你的电话是〈手机1〉对吧？")
    answer = bot.prm.decide(packet["id"], "copy")
    assert answer == {"ok": True, "status": "pending", "text": f"收到，你的电话是{PHONE}对吧？"}
    [ev] = bot.prm.store.events(packet["deal_id"], kinds=("buyin_copy",))
    assert ev["detail"] == {"packet": packet["id"], "edited": False, "tokens": ["〈手机1〉"]} and ev["actor"] == "console"
    edited = bot.prm.decide(packet["id"], "copy", "改一下：〈手机1〉，我加你")
    assert edited["text"] == f"改一下：{PHONE}，我加你"
    kept = bot.prm.store.packet(packet["id"])
    assert kept["final_text"] == "改一下：〈手机1〉，我加你" and kept["edited"] and kept["status"] == "pending"
    assert PHONE not in json.dumps(bot.prm.snapshot(), ensure_ascii=False)     # the console: placeholders only
    assert not bot.prm.decide(packet["id"], "copy", "打给〈手机9〉")["ok"]      # a placeholder that is not hers


def test_sent_in_baiying_by_hand_is_sent_and_takes_nothing_over(bot, clock):
    _, packet = buyin_draft(bot, clock)
    assert bot.prm.decide(packet["id"], "manual_sent", "你好呀，佣金 20%～") == {"ok": True, "status": "sent"}
    assert bot.prm.store.buyin_thread(CID)["answered_upto"] == packet["meta"]["upto"]   # her lines are answered
    done = bot.prm.store.packet(packet["id"])
    assert (done["status"], done["outcome"], done["final_text"]) == ("sent", "sent_by_hand", "你好呀，佣金 20%～")
    assert done["sent_at"] == clock.now and done["edited"]
    d = deal(bot)
    assert d["mode"] == "copilot" and not bot.prm.store.events(d["id"], kinds=("takeover", "operator_message"))
    assert bot.prm.store.events(d["id"], kinds=("buyin_sent",))[0]["detail"]["packet"] == packet["id"]
    assert bot.texts == [] and bot.files == []
    recent = next(p for p in bot.prm.snapshot()["recent"] if p["id"] == packet["id"])
    assert recent["channel"] == "buyin" and recent["outcome"] == "sent_by_hand"
    # our own line in 百应 (an operator there) is no takeover either
    round_says(bot, clock, conv(line("m3", "你好呀，佣金 20%～", clock.now, "shop")))
    assert deal(bot)["mode"] == "copilot" and not jobs_of(bot, CREATOR)


def test_her_lines_a_manual_send_answered_do_not_come_again(bot, clock):
    _, packet = buyin_draft(bot, clock, text="佣金多少", mid="m1")
    bot.prm.decide(packet["id"], "manual_sent")
    round_says(bot, clock, conv(line("m2", "那寄样呢", clock.now)))             # (our line not read back yet)
    newest = jobs_of(bot, BUYIN_CHAT + CID)[0]
    assert newest["text"].endswith("那寄样呢") and "佣金多少" not in newest["text"]


def test_the_bot_never_sends_it_and_it_cannot_be_marked_wrong(bot, clock):
    _, packet = buyin_draft(bot, clock)
    for action in ("send", "edit", "execute", "repreview", "ack"):
        answer = bot.prm.decide(packet["id"], action, "x")
        assert not answer["ok"] and "百应" in answer["error"]
    assert bot.prm.store.packet(packet["id"])["status"] == "pending" and bot.texts == []
    bot.prm.decide(packet["id"], "manual_sent")
    gate_before = bot.prm.store.gate("faq")
    answer = bot.prm.decide(packet["id"], "wrong")
    assert not answer["ok"] and "发错了" in answer["error"]
    assert bot.prm.store.gate("faq") == gate_before and not bot.prm.store.packet(packet["id"])["wrong"]
    # and the WeChat drafts know nothing of copy / manual_sent
    job = wechat_job(bot)
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="在的"))
    wechat = bot.prm.store.packet_for_job(job["id"])
    assert not bot.prm.decide(wechat["id"], "copy")["ok"] and not bot.prm.decide(wechat["id"], "manual_sent")["ok"]


def test_drop_answers_her_lines_and_her_next_line_comes_alone(bot, clock):
    _, packet = buyin_draft(bot, clock, text="在吗", mid="m1")
    assert bot.prm.decide(packet["id"], "drop") == {"ok": True, "status": "dropped"}
    round_says(bot, clock, conv(line("m1", "在吗", clock.now - 700), line("m2", "佣金多少", clock.now)))
    newest = jobs_of(bot, BUYIN_CHAT + CID)[0]
    assert newest["text"].endswith("佣金多少") and "在吗" not in newest["text"]
    assert "在吗" in json.loads(newest["meta"])["history"][0]["text"]


def test_buyin_drafts_never_count_for_the_wechat_gates(bot, clock):
    for n in range(3):
        _, packet = buyin_draft(bot, clock, text=f"问题 {n}", mid=f"m{n}")
        bot.prm.decide(packet["id"], "manual_sent")
    faq = next(g for g in bot.prm.snapshot()["gates"] if g["kind"] == "faq")
    assert faq["decided"] == 0 and bot.prm.store.reviewed("faq", 0, 50) == []
    assert bot.prm.store.gate("faq")["route"] == gates.REVIEW


def test_no_wechat_holding_reply_is_sent_for_a_buyin_draft(bot, clock):
    bot.prm._holding_sec = 60
    buyin_draft(bot, clock)
    clock.now += 3600
    assert bot.prm.send_holding_replies() == 0 and bot.texts == []


def test_a_buyin_line_brings_no_second_wechat_holding_reply(bot, clock):
    bot.prm._holding_sec = 600
    job = wechat_job(bot, "微信里问的")
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="微信的草稿"))
    clock.now += 601
    assert bot.prm.send_holding_replies() == 1 and len(bot.texts) == 1        # her WeChat round: once
    round_says(bot, clock, conv(line("m1", "百应里又问了一句", clock.now)))
    clock.now += 601
    bot.prm.tick()
    assert len(bot.texts) == 1                                                  # 百应 is no new WeChat round


def test_the_sender_thread_never_sends_a_buyin_draft_even_if_one_were_approved(bot, clock, monkeypatch):
    _, packet = buyin_draft(bot, clock)
    assert bot.prm.store.move(packet["id"], ("pending",), "approved", decided_at=clock.now)
    real_deliver = bot.prm._deliver
    monkeypatch.setattr(bot.prm, "_deliver", lambda p: pytest.fail("send_approved handed a 百应 draft on"))
    assert bot.prm.send_approved() == 0
    assert bot.prm.store.packet(packet["id"])["status"] == "pending" and bot.texts == []
    # and _deliver itself refuses one (the second guard)
    assert bot.prm.store.move(packet["id"], ("pending",), "sending")
    assert real_deliver(bot.prm.store.packet(packet["id"])) == 0
    assert bot.prm.store.packet(packet["id"])["status"] == "pending" and bot.texts == [] and bot.files == []


def test_platform_alerts_and_the_outbound_record_leave_buyin_drafts_out(bot, clock):
    _, packet = buyin_draft(bot, clock)
    d = deal(bot)
    bot.prm.store.add_packet(deal_id=d["id"], party_id=d["party_id"], job_id=None, chat_id=BUYIN_CHAT + CID, kind="faq",
                             text="x", attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False,
                             reason="", status="failed", tool=BUYIN_TOOL, decided_at=clock.now)
    assert bot.prm.store.platform_failures() == [] and bot.prm.snapshot()["platform_alerts"] == []
    bot.prm.decide(packet["id"], "manual_sent", "百应里说的话")
    job = wechat_job(bot, "那我在微信里问一下")
    told = json.loads(job["meta"]).get("sent") or []
    assert all("百应里说的话" not in str(item) for item in told)
    assert all(p["tool"] != BUYIN_TOOL for p in bot.prm.store.outcomes(d["id"]))


# ── WeChat and 百应 drafts side by side ──────────────────────────────────

def test_wechat_and_buyin_drafts_never_put_each_other_out_of_date(bot, clock):
    _, buyin = buyin_draft(bot, clock, text="百应里问的佣金")
    job = wechat_job(bot, "微信里问的寄样")
    assert "百应里问的佣金" not in job["text"] and "百应" not in json.dumps(json.loads(job["meta"]), ensure_ascii=False)
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="寄样的话…"))
    wechat = bot.prm.store.packet_for_job(job["id"])
    assert wechat["status"] == "pending" and bot.prm.store.packet(buyin["id"])["status"] == "pending"
    # her next 百应 line: the newer 百应 draft puts the older one out of date, not the WeChat one
    round_says(bot, clock, conv(line("m3", "还有寄样吗", clock.now)))
    newer = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["status"] == "pending"][0]
    assert "百应里问的佣金" in newer["text"] and "还有寄样吗" in newer["text"]      # her lines not answered yet
    second = mac_drafts(bot, newer)
    assert bot.prm.store.packet(buyin["id"])["status"] == "superseded"
    assert bot.prm.store.packet(wechat["id"])["status"] == "pending" and second["status"] == "pending"
    # another WeChat message puts out of date the WeChat draft only
    bot.prm.handle(bot.msg("还在吗"))
    assert bot.prm.store.packet(wechat["id"])["status"] == "superseded"
    assert bot.prm.store.packet(second["id"])["status"] == "pending"


def test_a_newer_round_cancels_the_queued_job_and_another_conversation_is_its_own(bot, clock):
    round_says(bot, clock, conv(line("m1", "第一句", clock.now)))
    [first] = jobs_of(bot, BUYIN_CHAT + CID)
    round_says(bot, clock, conv(line("m2", "第二句", clock.now)), conv(line("x1", "别的会话", clock.now), cid="conv_8002"))
    assert bot.relay.store.get(first["id"])["status"] == "cancelled"
    [latest] = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["status"] == "pending"]
    assert "第一句" in latest["text"] and "第二句" in latest["text"]
    [other] = jobs_of(bot, BUYIN_CHAT + "conv_8002")
    b2 = mac_drafts(bot, other)
    a = mac_drafts(bot, latest)                                              # (drafted after the other conversation's)
    assert bot.prm.store.packet(a["id"])["status"] == "pending"
    assert bot.prm.store.packet(b2["id"])["status"] == "pending"              # two conversations, two drafts
    # her next line in conversation A: only A's draft is out of date
    round_says(bot, clock, conv(line("m3", "第三句", clock.now)))
    [newest] = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["status"] == "pending"]
    a2 = mac_drafts(bot, newest)
    assert bot.prm.store.packet(a["id"])["status"] == "superseded" and a2["status"] == "pending"
    assert bot.prm.store.packet(b2["id"])["status"] == "pending"


def test_the_buyin_lane_supersedes_only_the_conversation_it_is_given():
    store = PrmStore(":memory:")
    party = store.add_party(CREATOR, "小美")
    d = store.deal_of(party["id"])

    def add(chat, job, tool=BUYIN_TOOL):
        return store.add_packet(deal_id=d["id"], party_id=party["id"], job_id=job, chat_id=chat, kind="faq", text="x",
                                attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False,
                                reason="", tool=tool)

    a_old, b, wechat, a_new = add(BUYIN_CHAT + "a", 1), add(BUYIN_CHAT + "b", 2), add(CREATOR, 3, "message.send"), \
        add(BUYIN_CHAT + "a", 4)
    assert store.supersede(d["id"], keep=a_new, lane="buyin", chat_id=BUYIN_CHAT + "a") == 1
    assert [store.packet(i)["status"] for i in (a_old, b, wechat, a_new)] == ["superseded", "pending", "pending", "pending"]
    assert store.supersede(d["id"], lane="reply") == 1                         # the WeChat reply lane: WeChat only
    assert [store.packet(i)["status"] for i in (b, wechat, a_new)] == ["pending", "superseded", "pending"]


def test_an_older_draft_coming_back_after_the_newer_one_never_puts_it_out_of_date(bot, clock):
    round_says(bot, clock, conv(line("m1", "第一句", clock.now)))
    [first] = jobs_of(bot, BUYIN_CHAT + CID)
    claim(bot, first["id"])                                                    # the Mac is drafting the first
    round_says(bot, clock, conv(line("m2", "第二句", clock.now)))
    [second] = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["id"] != first["id"]]
    newer = mac_drafts(bot, second, reply_draft="两句都回了")                 # the newer one comes back first
    older = mac_drafts(bot, first, reply_draft="只回了第一句")
    assert older["status"] == "superseded"
    assert bot.prm.store.packet(newer["id"])["status"] == "pending"
    bot.prm.decide(newer["id"], "manual_sent")                                 # decided: the older one is still none
    round_says(bot, clock, conv(line("m3", "第三句", clock.now)))
    [third] = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["status"] == "pending"]
    assert "第一句" not in third["text"] and "第二句" not in third["text"]      # her answered lines stay answered


def test_a_running_job_that_comes_back_after_a_newer_one_is_out_of_date(bot, clock):
    round_says(bot, clock, conv(line("m1", "第一句", clock.now)))
    [first] = jobs_of(bot, BUYIN_CHAT + CID)
    claim(bot, first["id"])                                                    # the Mac is drafting it
    round_says(bot, clock, conv(line("m2", "第二句", clock.now)))
    bot.finish(first["id"], draft(message_kind="faq", reply_draft="旧的"))
    assert bot.prm.store.packet_for_job(first["id"])["status"] == "superseded"


def held(b):
    return [n for n in deal(b)["notices"] if n["key"] == f"buyin_held:{CID}"]


def test_while_a_person_has_the_deal_her_buyin_lines_wait_with_a_notice_and_are_drafted_after(bot, clock):
    d = deal(bot)
    assert bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "paused"})["ok"]
    round_says(bot, clock, conv(line("m1", "暂停时在百应问的", clock.now)))
    assert jobs_of(bot, BUYIN_CHAT + CID) == [] and len(buyin_events(bot)) == 1
    [notice] = held(bot)
    assert "暂停期间她在百应私信里发了 1 条消息" in notice["text"] and "恢复副驾驶后会起草" in deal(bot)["attention"]
    for _ in range(3):
        clock.now += 5
        bot.prm.tick()                                                          # paused: still nothing
    assert jobs_of(bot, BUYIN_CHAT + CID) == [] and held(bot)
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "copilot"})
    clock.now += 5
    bot.prm.tick()                                                              # back in copilot: one draft
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    assert "暂停时在百应问的" in job["text"] and held(bot) == []
    clock.now += 5
    bot.prm.tick()
    assert len(jobs_of(bot, BUYIN_CHAT + CID)) == 1                              # once
    # a draft that comes back while a person has the deal again (a WeChat takeover too) is held back
    bot.prm.on_own_message(bot.msg("运营亲手回的"))
    assert deal(bot)["mode"] == "human"
    assert mac_drafts(bot, job)["status"] == "superseded"


def test_a_wechat_takeover_holds_her_buyin_question_until_it_runs_out(bot, clock):
    bot.prm.on_own_message(bot.msg("运营在微信里亲手回的"))                     # 120 minutes a person's
    round_says(bot, clock, conv(line("q1", "佣金多少？", clock.now)))
    assert jobs_of(bot, BUYIN_CHAT + CID) == [] and "人工接管期间她在百应私信里发了 1 条消息" in held(bot)[0]["text"]
    clock.now += bot.prm._takeover_sec + 1
    bot.prm.tick()                                                              # the takeover ran out
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    assert "佣金多少？" in job["text"] and held(bot) == [] and deal(bot)["mode"] == "copilot"


def test_held_lines_answered_in_baiying_meanwhile_are_not_drafted(bot, clock):
    d = deal(bot)
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "human"})
    round_says(bot, clock, conv(line("m1", "接管时问的", clock.now)))
    round_says(bot, clock, conv(line("m2", "运营在百应里回了", clock.now, "shop")))
    assert held(bot) == []                                                      # answered: the notice goes
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "copilot"})
    clock.now += 5
    bot.prm.tick()
    assert jobs_of(bot, BUYIN_CHAT + CID) == []


def test_her_wechat_recall_does_not_hold_buyin_drafts_and_a_buyin_decision_does_not_see_to_it(bot, clock):
    _, packet = buyin_draft(bot, clock)
    before = bot.prm.store.packet(packet["id"])
    bot.prm.on_chat_notice(CREATOR, "\"小美\" 撤回了一条消息", clock.now)
    held = bot.prm.store.packet(packet["id"])
    assert held["reason"] == before["reason"] and held["needs_attention"] == before["needs_attention"]
    assert any(n["key"] == "recall" for n in deal(bot)["notices"])
    bot.prm.decide(packet["id"], "drop")
    assert any(n["key"] == "recall" for n in deal(bot)["notices"])          # still for a person to see to


# ── who it is ─────────────────────────────────────────────────────────────

def test_nobody_on_the_list_makes_no_party_and_the_page_says_how_many_wrote(bot, clock):
    parties = len(bot.prm.store.parties(include_inactive=True))
    round_says(bot, clock, conv(line("u1", f"我是新来的 {PHONE}", clock.now), cid="conv_9", uid="5199999999999999",
                                nickname="陌生达人"),
               conv(line("u2", "我也是", clock.now), cid="conv_10", uid="", aweme="nobody_dy", nickname=""))
    assert len(bot.prm.store.parties(include_inactive=True)) == parties and bot.prm.store.active_wxids() == {CREATOR}
    assert jobs_of(bot, BUYIN_CHAT + "conv_9") == [] and buyin_events(bot) == []
    snap = bot.prm.snapshot()["buyin"]
    assert snap["unmatched_count"] == 2 and sorted(u["name"] for u in snap["unmatched"]) == ["", "陌生达人"]
    thread = bot.prm.store.buyin_thread("conv_9")
    assert thread["party_id"] is None and "新来的" not in json.dumps(thread, ensure_ascii=False)
    # our side answered her in 百应: no longer waiting
    round_says(bot, clock, conv(line("u3", "你好，请先登记", clock.now, "shop"), cid="conv_9", uid="5199999999999999"))
    assert bot.prm.snapshot()["buyin"]["unmatched_count"] == 1
    # quiet for a week: forgotten
    clock.now += BUYIN_UNMATCHED_DAYS * 86400 + 1
    mac_reads(bot, next_round(bot, clock))
    assert bot.prm.store.buyin_thread("conv_9") is None and bot.prm.snapshot()["buyin"]["unmatched_count"] == 0


def test_her_douyin_id_from_a_verified_sample_order_is_enough_and_a_nickname_never_is(bot, clock):
    bot.prm.creator_directory = None
    verified_order(bot.prm, account="Test_Daren_01")
    round_says(bot, clock, conv(line("n1", "我叫小美", clock.now), uid="5122222222222222", nickname="小美"))
    assert jobs_of(bot, BUYIN_CHAT + CID) == []                                 # the nickname alone: nobody
    round_says(bot, clock, conv(line("n2", "抖音号对上了", clock.now), cid="conv_11", uid="", aweme="test_daren_01"))
    assert len(jobs_of(bot, BUYIN_CHAT + "conv_11")) == 1


def test_a_conversation_two_registered_creators_fit_is_nobodys(bot, clock):
    bot.prm.register("wxid_creator02", "小丽")
    bot.prm.creator_directory = listed({"wxid": CREATOR, "platform": "DY", "platform_no": AWEME, "platform_uid": ""},
                                       {"wxid": "wxid_creator02", "platform": "DY", "platform_no": "", "platform_uid": UID},
                                       {"wxid": "wxid_creator02", "platform": "XHS", "platform_no": "xhs_only",
                                        "platform_uid": ""})
    round_says(bot, clock, conv(line("a1", "谁的", clock.now), uid=UID, aweme=AWEME.upper()))
    assert jobs_of(bot, BUYIN_CHAT + CID) == [] and bot.prm.store.buyin_thread(CID)["party_id"] is None
    round_says(bot, clock, conv(line("a2", "小红书号不算", clock.now), cid="conv_12", uid="", aweme="xhs_only"))
    assert jobs_of(bot, BUYIN_CHAT + "conv_12") == []


def test_each_message_is_taken_once_by_its_id(bot, clock):
    same = conv(line("m1", "只算一次", clock.now))
    round_says(bot, clock, same)
    round_says(bot, clock, same)
    assert len(buyin_events(bot)) == 1 and len(jobs_of(bot, BUYIN_CHAT + CID)) == 1


def test_her_lines_read_a_day_late_are_not_drafted_and_the_deal_says_so(bot, clock):
    round_says(bot, clock, conv(line("old", "上周问的", clock.now - BUYIN_MAX_AGE_SEC - 3600)))
    assert jobs_of(bot, BUYIN_CHAT + CID) == [] and len(buyin_events(bot)) == 1
    [notice] = [n for n in deal(bot)["notices"] if n["key"] == f"buyin_old:{CID}"]
    assert "1 条消息读到时已经过了 24 小时" in notice["text"] and "到百应里看" in deal(bot)["attention"]
    # her new line is drafted (the old one as its history), the notice stays until she is answered
    round_says(bot, clock, conv(line("new", "在吗", clock.now)))
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    assert "在吗" in job["text"] and "上周问的" not in job["text"] and "上周问的" in json.dumps(json.loads(job["meta"]),
                                                                                         ensure_ascii=False)
    packet = mac_drafts(bot, job)
    assert any(n["key"] == f"buyin_old:{CID}" for n in deal(bot)["notices"])
    bot.prm.decide(packet["id"], "manual_sent")
    assert not any(n["key"].startswith("buyin_") for n in deal(bot)["notices"])


def test_a_conversation_whose_job_could_not_be_queued_comes_again_with_the_next_round(bot, clock, monkeypatch):
    real = bot.relay.submit

    def broken(chat, *a, **k):
        if chat.startswith(BUYIN_CHAT):
            raise RuntimeError("the relay database is locked")
        return real(chat, *a, **k)

    monkeypatch.setattr(bot.relay, "submit", broken)
    round_says(bot, clock, conv(line("m1", "这句不能丢", clock.now)), cursor={"t": "after-m1"})
    assert jobs_of(bot, BUYIN_CHAT + CID) == []
    assert "m1" not in (bot.prm.store.buyin_thread(CID) or {}).get("seen", [])
    assert bot.prm.store.meta_value("buyin_cursor") is None                   # the same is read again
    monkeypatch.setattr(bot.relay, "submit", real)
    poll = next_round(bot, clock)
    assert json.loads(poll["meta"])["cursor"] is None
    mac_reads(bot, poll, conv(line("m1", "这句不能丢", clock.now - 700)), cursor={"t": "after-m1"})
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    assert "这句不能丢" in job["text"] and len(buyin_events(bot)) == 1          # recorded once
    assert "m1" in bot.prm.store.buyin_thread(CID)["seen"]
    assert json.loads(bot.prm.store.meta_value("buyin_cursor")) == {"t": "after-m1"}


# ── when it cannot be read ───────────────────────────────────────────────

def test_a_mac_without_the_reader_says_so_once_and_is_asked_only_once_an_interval(bot, clock):
    stopped = {"verdict": "unavailable", "exit_code": 2, "message": "Mac 上的抖音命令行还没有读百应私信的命令（buyin im-inbox）"}
    round_says(bot, clock, stopped=stopped, cursor={"ignored": True})
    status = bot.prm.snapshot()["buyin"]["status"]
    assert status["verdict"] == "unavailable" and "百应私信读不了" in status["text"] and "buyin im-inbox" in status["text"]
    assert bot.prm.store.meta_value("buyin_cursor") is None                   # a stopped round keeps the cursor
    for _ in range(10):                                                          # the sender thread ticks every 5 s
        assert next_round(bot, clock, 5) is None
    round_says(bot, clock, stopped=stopped)
    assert len(jobs_of(bot, BUYIN_SYNC_CHAT)) == 2
    round_says(bot, clock)                                                       # it came through: nothing to say
    assert bot.prm.snapshot()["buyin"]["status"] is None


def test_an_old_worker_a_login_lapse_a_risk_wall_and_hiccups(bot, clock):
    job = next_round(bot, clock)
    mac_reads(bot, job, ok=False, error="任务里没有达人编号")
    assert "worker 还不会读百应私信" in bot.prm.snapshot()["buyin"]["status"]["text"]
    round_says(bot, clock, stopped={"verdict": "login", "exit_code": 77, "message": "[NOT_LOGGED_IN]"})
    assert "退出码 77" in bot.prm.snapshot()["buyin"]["status"]["text"]
    round_says(bot, clock, stopped={"verdict": "wall", "exit_code": 76, "message": "[RISK]"})
    assert "退出码 76" in bot.prm.snapshot()["buyin"]["status"]["text"]
    assert next_round(bot, clock) is None                                       # paused past one interval
    assert next_round(bot, clock, BUYIN_RISK_PAUSE_SEC) is not None
    bot.prm._buyin_errors = 0
    bot.prm.store.set_meta_value("buyin_status", "")
    for n in range(2):
        clock.now += INTERVAL + 1
        bot.prm._on_buyin_inbox({"id": 999 + n, "status": "done", "result_text": "不是 JSON"}, {})
        assert bot.prm.snapshot()["buyin"]["status"] is None                    # a hiccup is not said at once
    bot.prm._on_buyin_inbox({"id": 1001, "status": "done", "result_text": "{}"}, {})
    assert "连续 3 次" in bot.prm.snapshot()["buyin"]["status"]["text"]


def test_a_cancelled_or_lost_inbox_job_keeps_none_of_the_words(bot, clock):
    job = next_round(bot, clock)
    claim(bot, job["id"])
    assert bot.relay.store.cancel(job["id"]) == "requested"                    # cancelled while the Mac finishes
    raw = platforms.im_result(conversations=[platforms.im_conversation(
        conv(line("u1", f"没登记的人 {PHONE}", clock.now), cid="conv_x", uid="5199999999999999"))])
    assert bot.relay.finish(job["id"], {"ok": True, "text": json.dumps(raw, ensure_ascii=False), "duration_s": 1})
    bot.relay.tick()
    kept = bot.relay.store.get(job["id"])
    assert kept["status"] == "cancelled" and PHONE not in kept["result_text"] and "没登记" not in kept["result_text"]
    assert bot.prm.store.meta_value("buyin_cursor") is None and bot.prm.store.buyin_thread("conv_x") is None


def test_an_inbox_answer_left_as_it_came_is_cut_to_a_summary_by_the_hourly_clean_up(bot, clock):
    job = next_round(bot, clock)
    raw = json.dumps(platforms.im_result(conversations=[platforms.im_conversation(
        conv(line("u1", f"留下来的原话 {PHONE}", clock.now)))]), ensure_ascii=False)
    with bot.relay.store._lock:                                                 # (a bot from before the rule)
        bot.relay.store._conn.execute("UPDATE relay_jobs SET status = 'done', result_text = ?, delivered = 1"
                                      " WHERE id = ?", (raw, job["id"]))
        bot.relay.store._conn.commit()
    pending = next_round(bot, clock)                                            # not handed on yet: left alone
    with bot.relay.store._lock:
        bot.relay.store._conn.execute("UPDATE relay_jobs SET status = 'done', result_text = ?, delivered = 0"
                                      " WHERE id = ?", (raw, pending["id"]))
        bot.relay.store._conn.commit()
    bot.prm.purge_due()
    assert PHONE not in bot.relay.store.get(job["id"])["result_text"]
    assert bot.relay.store.get(pending["id"])["result_text"] == raw


# ── what goes wrong with a draft, and the 90-day clean-up ────────────────

def test_a_buyin_draft_job_that_gave_no_draft_says_so_and_nothing_of_it_goes_to_wechat(bot, clock):
    round_says(bot, clock, conv(line("m1", "百应里没写成的那句", clock.now)))
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    claim(bot, job["id"])
    assert bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "claude 超时", "duration_s": 1})
    bot.relay.tick()
    d = deal(bot)
    assert "百应私信的草稿没写成" in d["attention"] and not any(n["key"].startswith("failed:") for n in d["notices"])
    wechat = wechat_job(bot, "微信里的话")
    assert "百应里没写成的那句" not in wechat["text"]
    assert event_out(bot.prm.store.events(d["id"], kinds=("buyin_draft_failed",))[0])["name"] == "百应私信的草稿没写成"
    # her next 百应 line carries it, and its draft sees to the notice
    round_says(bot, clock, conv(line("m2", "再问一次", clock.now)))
    newer = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["status"] == "pending"][0]
    assert "百应里没写成的那句" in newer["text"]
    mac_drafts(bot, newer)
    assert "百应私信的草稿没写成" not in deal(bot)["attention"]


def test_the_clean_up_covers_the_buyin_drafts_their_jobs_and_her_lines(bot, clock):
    address = "收货地址：浙江省杭州市西湖区文三路100号3栋1201"
    job, packet = buyin_draft(bot, clock, text=f"{address} 电话 {PHONE}", reply_draft=f"好的，寄到{address[5:]}，电话〈手机1〉")
    assert "文三路" in bot.relay.store.get(job["id"])["text"]
    bot.prm.purge(deal(bot), clock.now)
    assert "文三路" not in bot.relay.store.get(job["id"])["text"]
    kept = bot.prm.store.packet(packet["id"])
    assert "文三路" not in kept["text"] and "文三路" not in kept["meta"]["said"] and retention.CLEARED in kept["meta"]["said"]
    assert all("文三路" not in ev["detail"]["text"] for ev in buyin_events(bot))
    assert bot.prm.store.tokens(packet["party_id"]) == {}                      # the numbers are cleared too
    assert not bot.prm.decide(packet["id"], "copy")["ok"]                      # nothing to put back any more


def test_the_events_read_well_in_the_deal(bot, clock):
    _, packet = buyin_draft(bot, clock)
    round_says(bot, clock, conv(line("m3", "我方回的", clock.now, "shop")))
    names = {event_out(ev)["name"] for ev in buyin_events(bot)}
    assert names == {"百应私信：达人", "百应私信：我方（运营在百应里回的）"}
    bot.prm.decide(packet["id"], "copy")
    assert event_out(bot.prm.store.events(packet["deal_id"], kinds=("buyin_copy",))[0])["name"].startswith("复制百应私信")


# ── the wiring and the contract's shape ──────────────────────────────────

def test_the_bot_wires_the_creator_list_by_douyin_id_and_uid(bot, clock):
    from src.bot import Bot as WeChatBot
    from src.influencer.store import InfluencerStore

    contacts = InfluencerStore(":memory:")
    contacts.save_contact(CREATOR, source="控制台", display_name="小美", platform="DY", platform_no="Xiaomei_DY")
    contacts.record_daren_profile(CREATOR, tier="", channel="", sales_label="", level=None, note="", uid=UID)
    contacts.save_contact("wxid_off", source="控制台", platform="DY", platform_no="off_dy", enabled=False)
    bot.prm.creator_directory = None
    WeChatBot._wire_prm_directory(bot.prm, contacts)
    assert bot.prm.creator_directory() == [{"wxid": CREATOR, "platform": "DY", "platform_no": "Xiaomei_DY",
                                            "platform_uid": UID}]
    round_says(bot, clock, conv(line("w1", "按抖音号认", clock.now), uid="", aweme="xiaomei_dy"))
    assert len(jobs_of(bot, BUYIN_CHAT + CID)) == 1
    WeChatBot._wire_prm_directory(None, contacts)                                # no PRM: nothing to wire


def test_the_answer_is_cut_to_the_contract_on_both_sides():
    raw = {"command": "im-inbox", "cursor": {"t": "x" * 10}, "conversations": [
        {"conversationId": "c 1", "messages": []},                                       # not an id: dropped
        {"conversationId": "c1", "creator": {"uid": "abc", "awemeId": "-bad", "nickname": "小美\x00" + "长" * 60},
         "messages": [{"id": "m1", "at": 1790000000000, "from": "creator", "text": "你好\x07\n第二行"},
                      {"id": "m1", "at": 1790000001, "from": "creator", "text": "重复的编号"},
                      {"id": "m2", "at": "2026-10-29 10:00:00", "from": "system", "text": "系统提示"},
                      {"id": "m3", "at": 1790000002, "from": "shop", "kind": "sticker", "text": "x" * 2000}]}]}
    [c] = [platforms.im_conversation(r) for r in raw["conversations"] if platforms.im_conversation(r)]
    assert c["creator"] == {"uid": "", "aweme_id": "", "nickname": "小美" + "长" * 38, "buyin_id": ""}
    assert c["invited"] is None
    assert [m["id"] for m in c["messages"]] == ["m1", "m3"]
    assert c["messages"][0] == {"id": "m1", "at": 1790000000.0, "from": "creator", "kind": "text", "text": "你好\n第二行"}
    assert c["messages"][1]["kind"] == "other" and len(c["messages"][1]["text"]) == platforms.IM_TEXT_CHARS
    assert platforms.im_cursor({"t": "x" * platforms.IM_CURSOR_CHARS}) is None and platforms.im_cursor("") is None
    checked = platforms.check_im_result(platforms.im_result(conversations=[c], cursor=raw["cursor"],
                                                            stopped={"verdict": "weird", "exit_code": "2"}))
    assert checked["conversations"] == [c] and checked["cursor"] == raw["cursor"]
    assert checked["stopped"] == {"verdict": "error", "exit_code": None, "message": ""}
    with pytest.raises(platforms.ActionError):
        platforms.check_im_result({"op": "sample_status"})
    cfg = {"node": "/n", "dy_expect_shop_id": "7000000000000001"}
    argv = platforms.im_inbox_argv(cfg, {"t": 1}, 500)
    assert argv == ["/n", "bin/dy.mjs", "buyin", "im-inbox", '--after={"t":1}', f"--limit={platforms.IM_LIMIT}",
                    "--names", "--expect-shop-id=7000000000000001"]
    assert "--execute" not in argv and platforms.im_inbox_argv({"node": "/n"})[4:] == ["--limit=50", "--names"]


# ── re-review 2026-10-29 ─────────────────────────────────────────────────

def test_a_takeover_while_her_draft_is_written_holds_it_and_it_is_drafted_after(bot, clock):
    round_says(bot, clock, conv(line("m1", "佣金多少？", clock.now)))
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    bot.prm.on_own_message(bot.msg("运营在微信里亲手回了一句"))               # a takeover before the draft is back
    packet = mac_drafts(bot, job)
    assert packet["status"] == "superseded"
    [notice] = held(bot)
    assert notice["upto"] == packet["meta"]["upto"] and "人工接管期间她在百应私信里发了 1 条消息" in notice["text"]
    clock.now += bot.prm._takeover_sec + 1
    bot.prm.tick()                                                              # the takeover ran out: drafted once
    again = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["id"] != job["id"]]
    assert len(again) == 1 and "佣金多少？" in again[0]["text"] and held(bot) == []
    assert mac_drafts(bot, again[0])["status"] == "pending"


def test_a_conversation_failing_every_round_is_said_then_skipped_and_the_cursor_moves(bot, clock, monkeypatch):
    real = bot.prm._buyin_draft_job

    def broken(party, deal_, cid, *a, **k):
        if cid == CID:
            raise RuntimeError("这个会话坏了")
        return real(party, deal_, cid, *a, **k)

    monkeypatch.setattr(bot.prm, "_buyin_draft_job", broken)
    bad = conv(line("m1", "坏会话里的话", clock.now))
    good = conv(line("g1", "好会话", clock.now), cid="conv_ok")
    round_says(bot, clock, bad, good, cursor={"t": 1})
    assert len(jobs_of(bot, BUYIN_CHAT + "conv_ok")) == 1                        # the others are taken in
    assert bot.prm.store.meta_value("buyin_cursor") is None and bot.prm.snapshot()["buyin"]["status"] is None
    round_says(bot, clock, bad, good, cursor={"t": 1})
    status = bot.prm.snapshot()["buyin"]["status"]
    assert "1 个会话连续 2 轮没处理成" in status["text"] and "这个会话坏了" in status["text"]
    assert bot.prm.store.meta_value("buyin_cursor") is None
    round_says(bot, clock, bad, good, cursor={"t": 1})                           # the third: skipped
    assert json.loads(bot.prm.store.meta_value("buyin_cursor")) == {"t": 1}
    assert "m1" in bot.prm.store.buyin_thread(CID)["seen"] and bot.prm.snapshot()["buyin"]["status"] is None
    [skipped] = [n for n in deal(bot)["notices"] if n["key"] == f"buyin_skipped:{CID}"]
    assert "连续 3 轮都处理不了" in skipped["text"] and "到百应里看" in deal(bot)["attention"]
    monkeypatch.setattr(bot.prm, "_buyin_draft_job", real)
    round_says(bot, clock, conv(line("m2", "我方回她了", clock.now, "shop")))  # answered: the notice goes
    assert not any(n["key"] == f"buyin_skipped:{CID}" for n in deal(bot)["notices"])


def test_a_held_notice_says_honestly_why_nothing_is_drafted(bot, clock):
    d = deal(bot)
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "paused"})
    round_says(bot, clock, conv(line("m1", "暂停时问的", clock.now)))
    assert "恢复副驾驶后会起草一份" in held(bot)[0]["text"]
    party = bot.prm.store.party_by_wxid(CREATOR)
    bot.prm.set_active(party["id"], False)
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "copilot"})
    clock.now += 5
    bot.prm.tick()
    assert "这位达人已停用，不会起草" in held(bot)[0]["text"] and jobs_of(bot, BUYIN_CHAT + CID) == []
    bot.prm._buyin_sec = 0                                                      # the switch off
    bot.prm.set_active(party["id"], True)
    clock.now += 5
    bot.prm.tick()
    assert "百应私信的起草已经关了" in held(bot)[0]["text"] and jobs_of(bot, BUYIN_CHAT + CID) == []
    bot.prm._buyin_sec = INTERVAL                                               # on again: drafted once
    clock.now += 5
    bot.prm.tick()
    assert len(jobs_of(bot, BUYIN_CHAT + CID)) == 1 and held(bot) == []


def test_a_held_conversation_that_cannot_be_drafted_is_logged_seldom(bot, clock, monkeypatch, caplog):
    import logging

    d = deal(bot)
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "paused"})
    round_says(bot, clock, conv(line("m1", "暂停时问的", clock.now)))
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "copilot"})
    monkeypatch.setattr(bot.prm, "_buyin_draft_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("排不进去")))
    caplog.set_level(logging.ERROR, logger="src.prm.service")

    def logged():
        return [r for r in caplog.records if "could not be drafted" in r.getMessage()]

    for _ in range(10):                                                         # every 5 s for 50 s
        clock.now += 5
        bot.prm.sweep_buyin_held()
    assert len(logged()) == 1 and held(bot)                                     # tried every round, said once
    clock.now += 600
    bot.prm.sweep_buyin_held()
    assert len(logged()) == 2


# ── 联系方式卡片、邀约台账认人（2026-10-29：真的 `dy buyin im-inbox`） ─────────
# 百应聊天页只有达人的百应编号（buyinId），没有抖音号 / UID：Mac 用邀约台账认出我们邀约的会话（creator.aweme_id + invited）。
# 达人发的联系方式卡片（kind contact）带完整的微信号 / 手机号：机器人只记打码的一行，完整的只在这一轮的内存里和
# on_buyin_contact 的调用里。下面的号码、编号、名字都是编的。

WECHAT = "zq_test_wx_83"                 # her contact card's WeChat id (made up)
CARD_PHONE = "13700001234"               # and its phone (made up)
BUYIN_ID = "1777000000000000042"
INVITE_AT = "2026-10-29T15:00:00+08:00"
INVITED = {"aweme_id": AWEME, "invited_at": INVITE_AT, "plan_id": "PTEST-plan-0001", "attempt": "a1b2c3d4e5f6",
           "category": "small", "products": ["3000000000000000101"], "commission_rate": 25}


def card_line(mid, at, contact=None, frm="creator"):
    """Her contact card as the CLI prints it (its own text holds the numbers: never kept)."""
    return {"id": mid, "at": at, "from": frm, "kind": "contact", "text": f"[联系方式卡片] 微信 {WECHAT} 电话 {CARD_PHONE}",
            "contact": {"wechat": WECHAT, "phone": CARD_PHONE} if contact is None else contact}


def card_conv(*messages, cid="conv_inv", buyin=BUYIN_ID, aweme="", invited=None, nickname="邀约的达人"):
    """A conversation as the worker hands it back: her 百应 id, and when the ledger matched it, 抖音号 + invited."""
    return {"conversationId": cid, "creator": {"buyinId": buyin, "awemeId": aweme, "nickname": nickname},
            "messages": list(messages), **({"invited": invited} if invited else {})}


class Hook:
    """on_buyin_contact as the wechat-add feature wires it: the events it got; its answer (or what it raises)."""

    def __init__(self, answer=None, error=None):
        self.events, self.answer, self.error = [], answer, error

    def __call__(self, event):
        self.events.append(json.loads(json.dumps(event, ensure_ascii=False)))
        if self.error is not None:
            raise self.error
        return self.answer


def everything_kept(b, caplog=None) -> str:
    """Every row of prm.db and of the relay's jobs, the snapshot, the events and notices, the captured log: one text."""
    parts = []
    for store in (b.prm.store, b.relay.store):
        with store._lock:
            tables = [r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for table in tables:
                parts += [json.dumps([tuple(r) for r in store._conn.execute(f"SELECT * FROM {table}")],
                                     ensure_ascii=False, default=str)]
    parts.append(json.dumps(b.prm.snapshot(), ensure_ascii=False, default=str))
    for party in b.prm.store.parties(include_inactive=True):
        d = b.prm.store.deal_of(party["id"])
        parts.append(json.dumps(b.prm.store.events(d["id"], limit=None), ensure_ascii=False, default=str))
        parts.append(json.dumps(d.get("notices"), ensure_ascii=False, default=str))
    if caplog is not None:
        parts.append(caplog.text)
    return "\n".join(parts)


def test_the_cli_kinds_card_and_contact_are_taken_with_their_own_fields():
    at = 1793237400123                                                          # milliseconds, as the CLI prints
    c = platforms.im_conversation(card_conv(
        {"id": "7400000000000000001", "at": at, "from": "shop", "kind": "card", "card": "alliance_invite_card",
         "text": "邀约卡：秋冬帽子专场"},
        {"id": "7400000000000000002", "at": at + 1, "from": "shop", "kind": "card", "card": "contact_card",
         "text": "[我方的联系方式卡片]"},
        {"id": "7400000000000000003", "at": at + 2, "from": "shop", "kind": "card", "card": "Bad-Card!", "text": "x"},
        card_line("7400000000000000004", at + 3),
        card_line("7400000000000000005", at + 4, {"unavailable": True, "why": "older"}),
        card_line("7400000000000000006", at + 5, {"unavailable": True, "why": "made-up"}),
        card_line("7400000000000000007", at + 6, {"wechat": "x" * 65, "phone": 13700001234}),   # too long, no text
        card_line("7400000000000000008", at + 7, {"wechat": "a\nb", "phone": "  "}),
        card_line("7400000000000000009", at + 8, "微信 zq"),
        card_line("7400000000000000010", at + 9, {"phone": f"  {CARD_PHONE} "})))
    assert c["creator"] == {"uid": "", "aweme_id": "", "nickname": "邀约的达人", "buyin_id": BUYIN_ID}
    assert c["invited"] is None
    msgs = {m["id"][-2:]: m for m in c["messages"]}
    assert msgs["01"] == {"id": "7400000000000000001", "at": 1793237400.123, "from": "shop", "kind": "card",
                          "text": "邀约卡：秋冬帽子专场", "card": platforms.INVITE_CARD}
    assert msgs["02"]["card"] == "contact_card" and msgs["03"]["card"] == ""
    assert msgs["04"]["contact"] == {"wechat": WECHAT, "phone": CARD_PHONE}
    assert msgs["04"]["text"] == platforms.IM_CONTACT_TEXT and WECHAT not in msgs["04"]["text"]
    assert msgs["05"]["contact"] == {"unavailable": True, "why": "older"}
    for key in ("06", "07", "08", "09"):
        assert msgs[key]["contact"] == {"unavailable": True, "why": "unknown"}, key
    assert msgs["10"]["contact"] == {"phone": CARD_PHONE}
    assert "card" not in msgs["04"] and "contact" not in msgs["01"]
    for bad in ("abc", "-1", "1" * 31, True):
        assert platforms.im_conversation(card_conv(buyin=bad))["creator"]["buyin_id"] == ""
    assert platforms.im_conversation({"conversationId": "c", "creator": {"buyin_id": 42}})["creator"]["buyin_id"] == "42"


def test_invited_is_checked_and_everything_survives_the_bots_check():
    assert platforms.im_invited(INVITED) == INVITED
    assert platforms.im_invited(None) is None and platforms.im_invited({**INVITED, "aweme_id": "-bad"}) is None
    odd = platforms.im_invited({**INVITED, "commission_rate": 81, "products": ["1", 2, "x", 3.0, True, *["9"] * 12],
                                "plan_id": "P" * 60, "category": "c" * 30, "invited_at": "\x00" + "t" * 50})
    assert odd["commission_rate"] is None and odd["products"] == ["1", "2", *["9"] * 8]
    assert len(odd["plan_id"]) == 40 and len(odd["category"]) == 20 and len(odd["invited_at"]) == 40
    assert platforms.im_invited({**INVITED, "commission_rate": True})["commission_rate"] is None
    assert platforms.im_invited({**INVITED, "commission_rate": 0})["commission_rate"] == 0
    c = platforms.im_conversation(card_conv(
        {"id": "m1", "at": 1793237400000, "from": "shop", "kind": "card", "card": "alliance_invite_card", "text": "邀约"},
        card_line("m2", 1793237460000), card_line("m3", 1793237470000, {"unavailable": True, "why": "own"}),
        aweme=AWEME, invited=INVITED))
    checked = platforms.check_im_result(json.loads(json.dumps(platforms.im_result(conversations=[c], cursor={"v": 1, "t": 1}),
                                                              ensure_ascii=False)))
    assert checked["conversations"] == [c] and c["invited"] == INVITED and c["creator"]["buyin_id"] == BUYIN_ID
    assert [m.get("contact") for m in c["messages"]] == [None, {"wechat": WECHAT, "phone": CARD_PHONE},
                                                          {"unavailable": True, "why": "own"}]


def test_the_contact_card_line_is_masked_the_same_way_every_time():
    from src.prm.service import buyin_contact_line, buyin_mask_phone, buyin_mask_wechat

    assert buyin_mask_wechat("zq_test_wx_83") == "zq****83" and buyin_mask_wechat("abcd") == "****"
    assert buyin_mask_phone("13700001234") == "137****1234" and buyin_mask_phone("1234567") == "****"
    assert buyin_contact_line({"wechat": WECHAT, "phone": CARD_PHONE}, 12) == \
        "[联系方式卡片] 微信号 zq****83、手机 137****1234（加微信 #12）"
    assert buyin_contact_line({"phone": CARD_PHONE}) == "[联系方式卡片] 手机 137****1234"
    assert buyin_contact_line({"unavailable": True, "why": "older"}) == "[联系方式卡片]（这次没读到号码）"
    assert buyin_contact_line({"unavailable": True, "why": "own"}).startswith("[联系方式卡片]（卡片上只有店铺自己的")
    assert buyin_contact_line(None, 3) == "[联系方式卡片]（这次没读到号码）（加微信 #3）"


def test_our_invite_not_on_the_list_tells_the_hook_and_the_page_marks_it(bot, clock):
    parties = len(bot.prm.store.parties(include_inactive=True))
    hook = bot.prm.on_buyin_contact = Hook({"id": 41, "state": "pending", "masked": {"wechat": "zq****83"}})
    other = dict(INVITED, aweme_id="invited_not_listed")
    sent_at = clock.now - 20
    poll = round_says(bot, clock, card_conv(
        line("i0", "邀约卡：秋冬帽子专场", clock.now - 600, "shop", "card"),
        line("i1", "你好，加我微信吧", clock.now - 30), card_line("i2", sent_at),
        aweme="invited_not_listed", invited=other))
    assert len(bot.prm.store.parties(include_inactive=True)) == parties          # no party is made
    assert hook.events == [{
        "source": "buyin_card", "conversation_id": "conv_inv", "message_id": "i2", "at": sent_at,
        "creator": {"aweme_id": "invited_not_listed", "uid": BUYIN_ID, "nickname": "邀约的达人"},
        "contact": {"wechat": WECHAT, "phone": CARD_PHONE, "unavailable": False, "why": ""},
        "party_id": None, "invited": other}]
    thread = bot.prm.store.buyin_thread("conv_inv")
    assert thread["invited"] == other and thread["buyin_id"] == BUYIN_ID and thread["party_id"] is None
    assert thread["contacts"] == 0 and thread["unanswered"] == 2
    summary = json.loads(bot.relay.store.get(poll["id"])["result_text"])["summary"]
    assert (summary["invited"], summary["contacts"], summary["others_contacts"], summary["unmatched"]) == (1, 1, 0, 1)
    snap = bot.prm.snapshot()["buyin"]
    [entry] = snap["unmatched"]
    assert entry["name"] == "邀约的达人" and entry["count"] == 2 and snap["others_contacts"] == 0
    assert entry["invited"] == {"aweme_id": "invited_not_listed", "invited_at": INVITE_AT, "plan_id": INVITED["plan_id"]}
    # the next round has the same messages (the CLI's window overlaps) and her card has scrolled out of what it
    # read: nobody is told twice, the conversation is still our invite
    round_says(bot, clock, card_conv(line("i1", "你好，加我微信吧", clock.now - 30), card_line("i2", clock.now - 20),
                                     line("i3", "在吗", clock.now)))
    assert len(hook.events) == 1
    assert bot.prm.store.buyin_thread("conv_inv")["invited"] == other
    assert bot.prm.snapshot()["buyin"]["unmatched"][0]["invited"]["aweme_id"] == "invited_not_listed"
    assert WECHAT not in everything_kept(bot) and CARD_PHONE not in everything_kept(bot)


def test_a_registered_creator_found_by_the_ledgers_douyin_id_gets_the_masked_line_with_the_hooks_id(bot, clock):
    hook = bot.prm.on_buyin_contact = Hook({"id": 7, "state": "pending", "masked": {}})
    party = next(p for p in bot.prm.store.parties() if p["wxid"] == CREATOR)
    # the page has no 抖音号: the Mac's ledger match filled it in (her 抖音号 is on the 达人名单), no invited needed
    round_says(bot, clock, card_conv(line("r1", "我的联系方式发你了", clock.now - 10), card_line("r2", clock.now - 5),
                                     cid=CID, aweme=AWEME))
    [event] = hook.events
    assert event["party_id"] == party["id"] and event["invited"] is None
    assert event["creator"] == {"aweme_id": AWEME, "uid": BUYIN_ID, "nickname": "邀约的达人"}
    assert event["contact"] == {"wechat": WECHAT, "phone": CARD_PHONE, "unavailable": False, "why": ""}
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    assert "[联系方式卡片] 微信号 zq****83、手机 137****1234（加微信 #7）" in job["text"]
    texts = [ev["detail"]["text"] for ev in buyin_events(bot)]
    assert any("（加微信 #7）" in t for t in texts) and len(texts) == 2
    assert [ev["detail"]["kind"] for ev in reversed(buyin_events(bot))] == ["text", "contact"]
    # a later round without her 抖音号 (the card scrolled out): the conversation still knows it, she is still hers
    round_says(bot, clock, card_conv(line("r3", "收到了吗", clock.now), cid=CID))
    assert len([j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["status"] == "pending"]) == 1
    assert "收到了吗" in jobs_of(bot, BUYIN_CHAT + CID)[0]["text"] and len(hook.events) == 1
    kept = everything_kept(bot)
    assert WECHAT not in kept and CARD_PHONE not in kept


def test_a_contact_card_that_could_not_be_read_or_was_our_own_is_said_so(bot, clock):
    hook = bot.prm.on_buyin_contact = Hook()                                     # it answers None: no id
    round_says(bot, clock, card_conv(card_line("u1", clock.now - 5, {"unavailable": True, "why": "read-failed"}),
                                     card_line("u2", clock.now, {"unavailable": True, "why": "own"}), cid=CID, aweme=AWEME))
    assert [e["contact"] for e in hook.events] == [
        {"wechat": "", "phone": "", "unavailable": True, "why": "read-failed"},
        {"wechat": "", "phone": "", "unavailable": True, "why": "own"}]
    texts = [ev["detail"]["text"] for ev in reversed(buyin_events(bot))]
    assert texts[0].endswith("[联系方式卡片]（这次没读到号码）") and "店铺自己的" in texts[1]
    assert not any("加微信" in t for t in texts)


def test_somebody_elses_conversation_never_reaches_the_hook_and_only_counts(bot, clock):
    hook = bot.prm.on_buyin_contact = Hook({"id": 1, "state": "pending", "masked": {}})
    poll = round_says(bot, clock, card_conv(line("o1", "我是别人邀约的", clock.now - 5), card_line("o2", clock.now),
                                            cid="conv_other", buyin="1777000000000000099", nickname="别人的达人"))
    assert hook.events == []
    summary = json.loads(bot.relay.store.get(poll["id"])["result_text"])["summary"]
    assert summary["others_contacts"] == 1 and summary["contacts"] == 0
    snap = bot.prm.snapshot()["buyin"]
    assert snap["others_contacts"] == 1 and snap["unmatched"][0]["invited"] is None
    round_says(bot, clock, card_conv(card_line("o2", clock.now - 1), card_line("o3", clock.now), cid="conv_other",
                                     buyin="1777000000000000099"))
    assert bot.prm.snapshot()["buyin"]["others_contacts"] == 2 and hook.events == []      # each card once
    assert WECHAT not in everything_kept(bot) and CARD_PHONE not in everything_kept(bot)


def test_a_hook_that_raises_does_not_stop_the_round_and_logs_no_value(bot, clock, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    hook = bot.prm.on_buyin_contact = Hook(error=ValueError(f"could not add {WECHAT} / {CARD_PHONE}"))
    poll = round_says(bot, clock, card_conv(line("e1", "加我", clock.now - 5), card_line("e2", clock.now), cid=CID,
                                            aweme=AWEME),
                      card_conv(card_line("e3", clock.now), cid="conv_inv2", aweme="invited_two",
                                invited=dict(INVITED, aweme_id="invited_two")))
    assert len(hook.events) == 2                                                 # the second was still told
    [job] = jobs_of(bot, BUYIN_CHAT + CID)                                       # her draft job all the same
    assert "[联系方式卡片] 微信号 zq****83" in job["text"] and "加微信" not in job["text"]
    assert json.loads(bot.relay.store.get(poll["id"])["result_text"])["summary"]["failed"] == 0
    assert any("on_buyin_contact failed" in r.getMessage() for r in caplog.records)
    assert bot.prm.snapshot()["buyin"]["status"] is None
    kept = everything_kept(bot, caplog)
    assert WECHAT not in kept and CARD_PHONE not in kept


def test_without_a_hook_a_contact_card_is_only_the_masked_line(bot, clock, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    # merged with 加百应达人微信 (功能 R): the hook is its offer_from_buyin, which answers None while PRM_FRIEND_ADD is off
    assert bot.prm.on_buyin_contact == bot.prm.friend_adds.offer_from_buyin and not bot.prm.friend_adds.enabled
    del bot.prm.on_buyin_contact                                                 # (missing altogether: the same)
    round_says(bot, clock, card_conv(card_line("h1", clock.now), cid=CID, aweme=AWEME))
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    assert job["text"].endswith("[联系方式卡片] 微信号 zq****83、手机 137****1234")
    kept = everything_kept(bot, caplog)
    assert WECHAT not in kept and CARD_PHONE not in kept


def test_the_full_numbers_are_nowhere_after_the_rounds_the_drafts_and_the_clean_ups(bot, clock, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    bot.prm.on_buyin_contact = Hook({"id": 9, "state": "pending", "masked": {}})
    job, packet = buyin_draft(bot, clock)                                         # a plain line first
    poll = round_says(bot, clock, card_conv(card_line("f1", clock.now), cid=CID, aweme=AWEME),
                      card_conv(card_line("f2", clock.now), cid="conv_f", aweme="invited_f",
                                invited=dict(INVITED, aweme_id="invited_f")),
                      card_conv(card_line("f3", clock.now), cid="conv_g", buyin="1777000000000000077"))
    summary = json.loads(bot.relay.store.get(poll["id"])["result_text"])
    assert set(summary) == {"op", "summary", "stopped"}
    assert {k: summary["summary"][k] for k in ("contacts", "others_contacts", "invited")} == \
        {"contacts": 2, "others_contacts": 1, "invited": 1}
    [draft_job] = [j for j in jobs_of(bot, BUYIN_CHAT + CID) if j["status"] == "pending"]
    mac_drafts(bot, draft_job, reply_draft="收到你的联系方式啦")
    # an answer kept as it came by a slip before (the hourly clean-up cuts it to counts)
    raw = json.dumps(platforms.im_result(conversations=[platforms.im_conversation(
        card_conv(card_line("f9", clock.now), cid=CID))]), ensure_ascii=False)
    left = next_round(bot, clock)
    with bot.relay.store._lock:
        bot.relay.store._conn.execute("UPDATE relay_jobs SET status = 'done', result_text = ?, delivered = 1"
                                      " WHERE id = ?", (raw, left["id"]))
        bot.relay.store._conn.commit()
    assert WECHAT in bot.relay.store.get(left["id"])["result_text"]
    assert bot.prm._buyin_scrub_jobs() == 1
    assert json.loads(bot.relay.store.get(left["id"])["result_text"]) == {"op": "buyin_inbox", "summary": None,
                                                                          "status": "done"}
    kept = everything_kept(bot, caplog)
    assert WECHAT not in kept and CARD_PHONE not in kept
    assert "zq****83" in kept                                                    # (the masked line is there)


def test_the_relay_database_file_keeps_no_trace_of_a_replaced_inbox_answer(tmp_path):
    # The bot holds a 百应 inbox answer (her words, contact numbers in full) only until it replaces it with its summary:
    # SQLite's secure_delete overwrites what was replaced, so the database file itself has no copy left in free pages.
    path = tmp_path / "relay.db"
    store = RelayStore(path)
    with store._lock:
        assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    job_id = store.add("prm:buyin-inbox", "bot", "百应私信", kind=PRM, input_closed=True)
    with store._lock:
        store._conn.execute("UPDATE relay_jobs SET status = 'running' WHERE id = ?", (job_id,))
        store._conn.commit()
    marker = "made_up_wx_trace_" + "x" * 20000                         # big enough to need overflow pages
    assert store.finish(job_id, ok=True, text=json.dumps({"op": "buyin_inbox", "w": marker})) is True
    assert store.replace_result(job_id, json.dumps({"op": "buyin_inbox", "summary": {"messages": 1}}))
    store.close()
    assert b"made_up_wx_trace_" not in path.read_bytes()
