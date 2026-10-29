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
from src.relay.store import RelayStore

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
    return b.prm.store.deal_of(b.prm.store.party_by_wxid(CREATOR)["id"])


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
    assert deal(bot)["last_message_at"] == clock.now - (INTERVAL + 1) - 60     # her line (when she wrote it): a sign of life
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
    a = mac_drafts(bot, latest)
    [other] = jobs_of(bot, BUYIN_CHAT + "conv_8002")
    b2 = mac_drafts(bot, other)
    assert a["status"] == "pending" and b2["status"] == "pending"            # two conversations, two drafts


def test_a_running_job_that_comes_back_after_a_newer_one_is_out_of_date(bot, clock):
    round_says(bot, clock, conv(line("m1", "第一句", clock.now)))
    [first] = jobs_of(bot, BUYIN_CHAT + CID)
    claim(bot, first["id"])                                                    # the Mac is drafting it
    round_says(bot, clock, conv(line("m2", "第二句", clock.now)))
    bot.finish(first["id"], draft(message_kind="faq", reply_draft="旧的"))
    assert bot.prm.store.packet_for_job(first["id"])["status"] == "superseded"


def test_while_a_person_has_the_deal_her_buyin_lines_are_only_recorded(bot, clock):
    d = deal(bot)
    assert bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "human"})["ok"]
    round_says(bot, clock, conv(line("m1", "接管时在百应问的", clock.now)))
    assert jobs_of(bot, BUYIN_CHAT + CID) == [] and len(buyin_events(bot)) == 1
    bot.prm.deal_action("mode", {"deal_id": d["id"], "mode": "copilot"})
    round_says(bot, clock, conv(line("m2", "接管结束后又问", clock.now)))
    [job] = jobs_of(bot, BUYIN_CHAT + CID)
    assert "接管时在百应问的" in job["text"] and "接管结束后又问" in job["text"]
    # a draft that comes back while a person has the deal again (a WeChat takeover too) is held back
    bot.prm.on_own_message(bot.msg("运营亲手回的"))
    assert deal(bot)["mode"] == "human"
    assert mac_drafts(bot, job)["status"] == "superseded"


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


def test_her_lines_read_a_day_late_are_recorded_not_drafted(bot, clock):
    round_says(bot, clock, conv(line("old", "上周问的", clock.now - BUYIN_MAX_AGE_SEC - 3600)))
    assert jobs_of(bot, BUYIN_CHAT + CID) == [] and len(buyin_events(bot)) == 1


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
    assert c["creator"] == {"uid": "", "aweme_id": "", "nickname": "小美" + "长" * 38}
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
