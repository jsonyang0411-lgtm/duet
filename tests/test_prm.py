"""PRM copilot, Windows side (src/prm + the relay's prm jobs): silent jobs, drafts, the console's decisions."""

import hashlib
import io
import json
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config import BotConfig, _validate_config
from src.edge import text as edge_text
from src.prm import pii
from src.prm.result import MAX_DRAFT_CHARS, DraftError, parse_draft
from src.prm.service import HOLDING_TEXT, SETTLE_SEC, PrmService, check_wxid, message_text
from src.prm.store import PartyError, PrmStore
from src.relay.service import MAX_AGE_SEC, MAX_OUTFILES, ClaudeRelay
from src.relay.store import RelayStore
from src.routing import steps as S
from src.routing.context import RouteContext, Stop
from src.wechat.wcdb_backend import WcdbBackend

ADMIN = "wxid_admin"
CREATOR = "wxid_creator01"
TOKEN = "t" * 32


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def config(**overrides):
    values = dict(
        claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=["wxid_helper"], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ADMIN, prm_copilot="on", prm_db=":memory:",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class Bot:
    """The relay and the PRM service as Bot.run wires them, with a fake WeChat.  ``prm``: settings of the PRM
    service only (``prm_night_auto_hours="18:00-09:00"``, say)."""

    def __init__(self, clock, tmp_path, send_ok=True, files_ok=True, with_files=True, **prm):
        self.clock = clock
        self.texts, self.files = [], []
        self.send_ok, self.files_ok = send_ok, files_ok
        self.relay_sent = []
        self.relay = ClaudeRelay(config(), store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay",
                                 send=lambda c, t: self.relay_sent.append((c, t)) or True)
        self.changed = 0
        self.prm = PrmService(config(**prm), self.relay, store=PrmStore(":memory:"), clock=clock,
                              send=self._send, send_file=self._send_file if with_files else None,
                              on_parties_changed=self._changed)

    def _send(self, chat, text):
        self.texts.append((chat, text))
        return self.send_ok

    def _send_file(self, chat, path):
        self.files.append((chat, path))
        return self.files_ok

    def _changed(self):
        self.changed += 1

    def msg(self, content, *, chat=CREATOR, ts=None, msg_type=1, group=False, **extra):
        return {"message_id": f"m{time.time_ns()}", "chat_id": chat, "sender_id": chat, "sender_name": "小美",
                "content": content, "msg_type": msg_type, "is_group": group,
                "timestamp": self.clock.now if ts is None else ts, **extra}

    def worker_takes(self, kinds=("admin", "prm")):
        self.clock.now += SETTLE_SEC + 1
        return self.relay.next_job(0, kinds)

    def finish(self, job_id, draft=None, ok=True, text=None):
        body = text if text is not None else json.dumps(draft, ensure_ascii=False)
        assert self.relay.finish(job_id, {"ok": ok, "text": body, "duration_s": 3})
        self.relay.tick()


def draft(**overrides):
    values = {"intent": "想要样品", "slots": {"收件人": "小美", "手机": "〈手机1〉"}, "stage_proposal": "address",
              "message_kind": "address_confirm", "reply_draft": "确认一下：小美，手机〈手机1〉。",
              "attachments_to_send": [], "needs_attention": False, "reason": "收件信息齐了"}
    values.update(overrides)
    return values


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


# ── store ─────────────────────────────────────────────────────────────────

def test_a_creator_gets_a_deal_and_comes_back_after_being_removed():
    store = PrmStore(":memory:")
    party = store.add_party(CREATOR, "小美", now=1)
    assert store.deal_of(party["id"])["stage"] == "talk"
    with pytest.raises(PartyError):
        store.add_party(CREATOR, "又一个")
    store.update_party(party["id"], active=False)
    assert store.party_by_wxid(CREATOR) is None and store.active_wxids() == set()
    again = store.add_party(CREATOR, "小美美", now=2)
    assert again["id"] == party["id"] and again["name"] == "小美美" and again["active"] == 1
    assert len(store.deals()) == 1                                         # the same deal carries on


def test_a_packet_moves_once_and_newer_drafts_supersede_older_ones():
    store = PrmStore(":memory:")
    party = store.add_party(CREATOR, "小美")
    deal = store.deal_of(party["id"])
    args = dict(deal_id=deal["id"], party_id=party["id"], job_id=1, chat_id=CREATOR, kind="faq", text="a",
                attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="")
    first, second = store.add_packet(**args), store.add_packet(**{**args, "job_id": 2})
    assert store.supersede(deal["id"], keep=second) == 1
    assert store.packet(first)["status"] == "superseded" and store.packet(second)["status"] == "pending"
    assert store.move(second, ("pending",), "approved", decided_at=5) is True
    assert store.move(second, ("pending",), "approved", decided_at=6) is False   # a double click
    with pytest.raises(ValueError):
        store.move(second, ("approved",), "sent", status="x")


def test_placeholders_are_per_creator_and_stable():
    store = PrmStore(":memory:")
    a, b = store.add_party("wxid_aaaa", "A"), store.add_party("wxid_bbbb", "B")
    assert store.token_for(a["id"], "phone", "手机", "13812345678") == "〈手机1〉"
    assert store.token_for(a["id"], "phone", "手机", "13812345678") == "〈手机1〉"
    assert store.token_for(a["id"], "phone", "手机", "13900000000") == "〈手机2〉"
    assert store.token_for(b["id"], "phone", "手机", "13900000000") == "〈手机1〉"
    assert store.tokens(a["id"]) == {"〈手机1〉": ("phone", "13812345678"), "〈手机2〉": ("phone", "13900000000")}


# ── phone and ID numbers ──────────────────────────────────────────────────

def masker():
    store = PrmStore(":memory:")
    party = store.add_party(CREATOR, "小美")
    return store, party["id"], lambda text: pii.mask(text, lambda k, label, v: store.token_for(party["id"], k, label, v))


@pytest.mark.parametrize("written", ["13812345678", "138 1234 5678", "138-1234-5678", "+86 13812345678",
                                     "+8613812345678", "电话:13812345678。"])
def test_every_way_of_writing_a_mobile_number_is_masked(written):
    store, party_id, mask = masker()
    out = mask(f"我的电话 {written} 谢谢")
    assert "〈手机1〉" in out and "1234" not in out
    assert store.tokens(party_id)["〈手机1〉"] == ("phone", "13812345678")


def test_other_numbers_stay_and_id_numbers_are_masked_first():
    store, party_id, mask = masker()
    out = mask("订单号 2023138123456789012，运单 SF1381234567812，身份证 11010119900307123X，邮编 200120")
    assert "2023138123456789012" in out and "SF1381234567812" in out and "200120" in out
    assert "〈证件1〉" in out and "11010119900307123X" not in out
    assert store.tokens(party_id)["〈证件1〉"] == ("id_card", "11010119900307123X")


def test_unmask_legend_and_unknown_placeholders():
    store, party_id, mask = masker()
    text = mask("收件人小美 13812345678")
    tokens = store.tokens(party_id)
    assert pii.unmask(text, tokens) == "收件人小美 13812345678"
    shown = pii.display(store.token_rows(party_id))
    assert pii.legend(text + text, shown) == [{"token": "〈手机1〉", "masked": "138****5678"}]
    assert pii.unknown_tokens("打给〈手机1〉或〈手机2〉", tokens) == ["〈手机2〉"]
    assert pii.masked_value("id_card", "11010119900307123X") == "1101**********123X"


# ── the draft the Mac sends back ──────────────────────────────────────────

def test_a_valid_draft_is_read_and_cleaned():
    d = parse_draft("```json\n" + json.dumps(draft(slots={"收件人": " 小美 ", "数量": 1, "bad": {"x": 1}})) + "\n```")
    assert d.message_kind == "address_confirm" and d.stage_proposal == "address"
    assert d.slots == {"收件人": "小美", "数量": "1"}                       # nested values dropped, text kept
    assert d.attachments_named is None and d.attachments_missing == []         # an older worker says nothing


def test_the_macs_report_on_the_files_is_read():
    d = parse_draft(json.dumps(draft(attachments_to_send=["assets/a.txt"],
                                     attachments_named=["assets/a.txt", " assets/b.pdf ", ""],
                                     attachments_missing=[{"name": "assets/b.pdf", "why": "超过 100 MB"}, "assets/c.png",
                                                          {"why": "no name"}, 7])))
    assert d.attachments_to_send == ["assets/a.txt"] and d.attachments_named == ["assets/a.txt", "assets/b.pdf"]
    assert d.attachments_missing == [{"name": "assets/b.pdf", "why": "超过 100 MB"}, {"name": "assets/c.png", "why": ""}]


BAD_DRAFTS = [
    ("不是 JSON", "not_json"), ("[1, 2]", "not_object"),
    (json.dumps(draft(stage_proposal="won")), "bad_stage"),
    (json.dumps(draft(message_kind="spam")), "bad_kind"),
    (json.dumps(draft(attachments_to_send="assets/a.png")), "bad_attachments"),
    (json.dumps(draft(attachments_named="assets/a.png")), "bad_attachments"),
    (json.dumps(draft(attachments_missing={"name": "assets/a.png"})), "bad_attachments"),
    (json.dumps(draft(needs_attention="yes")), "bad_needs_attention"),
    (json.dumps(draft(slots=["收件人"])), "bad_slots"),
    (json.dumps(draft(reply_draft="字" * (MAX_DRAFT_CHARS + 1))), "draft_too_long"),
]


@pytest.mark.parametrize("bad, code", BAD_DRAFTS, ids=[f"{code}-{n}" for n, (_, code) in enumerate(BAD_DRAFTS)])
def test_an_unusable_draft_is_refused_not_repaired(bad, code):
    with pytest.raises(DraftError) as e:
        parse_draft(bad)
    assert e.value.code == code


# ── a creator's messages: silent jobs ─────────────────────────────────────

def test_a_creators_message_becomes_a_silent_prm_job_with_numbers_masked(bot, clock):
    assert bot.prm.handle(bot.msg("收件人小美，电话 138 1234 5678")) is True
    job = bot.relay.store.get(1)
    assert job["kind"] == "prm" and json.loads(job["meta"]) == {"party_id": 1, "deal_id": 1, "name": "小美"}
    assert "〈手机1〉" in job["text"] and "1234" not in job["text"]
    assert job["ready_at"] == clock.now + SETTLE_SEC
    bot.relay.tick()
    assert bot.relay_sent == [] and bot.texts == []                         # no receipt, nothing at all


def test_strangers_groups_and_history_are_not_drafted(bot, clock):
    assert bot.prm.handle(bot.msg("你好", chat="wxid_stranger")) is False
    assert bot.prm.handle(bot.msg("你好", group=True)) is False
    # older than PRM_CATCHUP_HOURS (12 by default) when read: taken and recorded, not drafted
    assert bot.prm.handle(bot.msg("前天的话", ts=clock.now - 13 * 3600)) is True
    assert bot.relay.store.list_jobs() == []


def a_picture(tmp_path):
    path = tmp_path / "wechat_pic.jpg"
    path.write_bytes(b"\xff\xd8\xff fake jpeg")
    return patch("src.relay.service.resolve", return_value=(str(path), "图片-1.jpg", ""))


def test_a_burst_is_one_job_then_follow_ups_then_a_new_job_for_a_picture(bot, clock, tmp_path):
    bot.prm.handle(bot.msg("你好"))
    clock.now += 3
    bot.prm.handle(bot.msg("在吗"))
    assert [j["id"] for j in bot.relay.store.list_jobs()] == [1]
    assert bot.relay.store.get(1)["ready_at"] == clock.now + SETTLE_SEC     # held a little longer
    assert bot.relay.next_job(0, ("admin", "prm")) is None                  # not ready yet
    job = bot.worker_takes()
    assert job["id"] == 1 and job["kind"] == "prm" and "你好" in job["text"] and "在吗" in job["text"]
    bot.prm.handle(bot.msg("对了，要防晒的"))
    assert bot.relay.store.followups(1, 0)[0]["text"].endswith("对了，要防晒的")
    with a_picture(tmp_path):
        bot.prm.handle(bot.msg("[图片]", msg_type=3))                       # files need a job of their own
    second = bot.relay.store.get(2)
    assert second["kind"] == "prm" and "[图片]" in second["text"]
    assert [f["name"] for f in bot.relay.store.files(2)] == ["图片-1.jpg"]


def test_a_picture_that_cannot_be_found_is_said_in_words(bot, clock):
    with patch("src.relay.service.resolve", return_value=("", "", "一张图片在电脑上没找到")):
        bot.prm.handle(bot.msg("[图片]", msg_type=3))
    job = bot.relay.store.get(1)
    assert "[图片]（一张图片在电脑上没找到）" in job["text"] and bot.relay.store.files(1) == []


def test_an_older_worker_never_gets_a_prm_job(bot):
    bot.prm.handle(bot.msg("你好"))
    bot.clock.now += SETTLE_SEC + 1
    assert bot.relay.next_job(0) is None                                    # admin only by default
    assert bot.relay.next_job(0, ("admin",)) is None
    assert bot.relay.next_job(0, ("prm",))["kind"] == "prm"


def test_a_prm_job_never_asks_for_approval_and_never_reports_progress(bot, clock, tmp_path):
    relay = ClaudeRelay(config(claude_relay_progress_min=1), store=RelayStore(":memory:"), clock=clock,
                        files_dir=tmp_path / "r2", send=lambda c, t: bot.relay_sent.append((c, t)) or True)
    relay.register_kind("prm", lambda job: True)
    relay.submit(CREATOR, CREATOR, "你好", kind="prm", meta={"party_id": 1, "deal_id": 1})
    job = relay.next_job(0, ("prm",))
    assert relay.ask(job["id"], {"tool": "Bash", "detail": "rm -rf"}) is None
    clock.now += 600
    relay.heartbeat(job["id"], {"steps": 3, "doing": "看 kb"})
    relay.tick()
    assert bot.relay_sent == [] and relay.store.unsent_asks() == []


def test_a_prm_result_never_reaches_wechat_even_when_nobody_takes_it(clock, tmp_path):
    """PRM switched off after a job was queued, or its handler failing: the result waits, never sent."""
    sent = []
    relay = ClaudeRelay(config(), store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "r",
                        send=lambda c, t: sent.append((c, t)) or True)
    relay.store.add(CREATOR, CREATOR, "[10:00] 你好", kind="prm", meta={"party_id": 1, "deal_id": 1})
    job = relay.next_job(0, ("prm",))
    relay.finish(job["id"], {"ok": True, "text": json.dumps(draft(), ensure_ascii=False)})
    relay.tick()                                                            # no handler registered
    assert sent == [] and relay.store.get(job["id"])["delivered"] == 0
    relay.register_kind("prm", lambda j: 1 / 0)                             # a handler that breaks
    clock.now += 60
    relay.tick()
    assert sent == [] and relay.store.get(job["id"])["delivered"] == 0
    relay.register_kind("prm", lambda j: True)
    clock.now += 60
    relay.tick()
    assert sent == [] and relay.store.get(job["id"])["delivered"] == 1


def test_follow_ups_a_prm_job_missed_become_a_prm_job(bot):
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    bot.prm.handle(bot.msg("还有一件事"))                                    # a follow-up the worker never read
    bot.finish(job["id"], draft())
    second = bot.relay.store.get(2)
    assert second["kind"] == "prm" and json.loads(second["meta"])["party_id"] == 1
    assert second["text"].endswith("还有一件事")


# ── drafts back from the Mac ──────────────────────────────────────────────

def test_a_draft_becomes_a_pending_packet_and_updates_the_deal(bot):
    bot.prm.handle(bot.msg("收件人小美 13812345678"))
    job = bot.worker_takes()
    bot.finish(job["id"], draft(needs_attention=True, reason="问了佣金，kb 没写"))
    [packet] = bot.prm.store.packets()
    assert packet["status"] == "pending" and packet["route"] == "review" and packet["job_id"] == job["id"]
    assert packet["kind"] == "address_confirm" and packet["text"] == "确认一下：小美，手机〈手机1〉。"
    deal = bot.prm.store.deal(1)
    assert deal["slots"] == {"收件人": "小美", "手机": "〈手机1〉"} and deal["stage_proposal"] == "address"
    assert deal["stage"] == "talk"                                          # only a suggestion
    assert deal["attention"] == "问了佣金，kb 没写"
    assert bot.texts == [] and bot.relay_sent == []
    bot.relay.tick()                                                        # delivered once
    assert len(bot.prm.store.packets()) == 1


def test_a_new_message_makes_waiting_drafts_out_of_date(bot):
    bot.prm.handle(bot.msg("你好"))
    bot.finish(bot.worker_takes()["id"], draft())
    bot.prm.handle(bot.msg("我改主意了"))
    assert bot.prm.store.packet(1)["status"] == "superseded"
    assert bot.prm.decide(1, "send")["ok"] is False


def test_a_draft_overtaken_by_a_newer_job_is_superseded_at_once(bot, tmp_path):
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    with a_picture(tmp_path):
        bot.prm.handle(bot.msg("[图片]", msg_type=3))                       # a new job while #1 runs
    assert bot.relay.store.get(2)["status"] == "pending"
    bot.finish(job["id"], draft())
    assert bot.prm.store.packet(1)["status"] == "superseded"


@pytest.mark.parametrize("status, word", [("failed", "草稿没写成"), ("lost", "Mac 失联")])
def test_a_failed_or_lost_job_asks_a_person_to_look(bot, clock, status, word):
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    if status == "failed":
        bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "claude 没有给出结构化结果"})
    else:
        clock.now += 1000
    bot.relay.tick()
    assert word in bot.prm.store.deal(1)["attention"] and bot.prm.store.packets() == []


def test_an_unusable_answer_drafts_nothing(bot):
    bot.prm.handle(bot.msg("你好"))
    bot.finish(bot.worker_takes()["id"], text="好的我来回复：在的！")
    assert bot.prm.store.packets() == [] and "草稿格式不对（not_json）" in bot.prm.store.deal(1)["attention"]


def test_nothing_to_say_is_recorded_but_not_waiting(bot):
    bot.prm.handle(bot.msg("好的"))
    bot.finish(bot.worker_takes()["id"], draft(reply_draft="", message_kind="greeting"))
    assert bot.prm.store.packet(1)["status"] == "no_reply"


# ── the console decides, the sender thread sends ──────────────────────────

def pending_packet(bot, d=None, files=()):
    bot.prm.handle(bot.msg("收件人小美 13812345678"))
    job = bot.worker_takes()
    for name, body in files:
        assert bot.relay.outfile(job["id"], name, io.BytesIO(body).read, len(body))
    bot.finish(job["id"], d or draft())
    return bot.prm.store.packets()[0]


def test_send_puts_the_number_back_and_sends_text_then_files(bot):
    packet = pending_packet(bot, draft(attachments_to_send=["assets/brief.txt"]), files=[("brief.txt", b"brief v1")])
    assert packet["attachments"][0]["name"] == "brief.txt"
    assert packet["attachments"][0]["sha256"] == hashlib.sha256(b"brief v1").hexdigest()
    assert bot.prm.decide(packet["id"], "send") == {"ok": True, "status": "approved"}
    assert bot.prm.decide(packet["id"], "send")["ok"] is False             # once only
    assert bot.texts == []                                                  # the sender thread sends
    assert bot.prm.send_approved() == 1
    assert bot.texts == [(CREATOR, "确认一下：小美，手机13812345678。")]
    assert [c for c, _ in bot.files] == [CREATOR] and bot.files[0][1].endswith("brief.txt")
    sent = bot.prm.store.packet(packet["id"])
    assert sent["status"] == "sent" and sent["outcome"] == "sent_unconfirmed" and sent["edited"] is False
    assert bot.prm.send_approved() == 0                                     # never twice


def test_edit_then_send_keeps_the_placeholders_out_of_the_database(bot):
    packet = pending_packet(bot)
    assert "对不上的占位符" in bot.prm.decide(packet["id"], "edit", "打〈手机2〉")["error"]
    assert bot.prm.decide(packet["id"], "edit", "好的，〈手机1〉 这个号码对吗？")["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, "好的，13812345678 这个号码对吗？")]
    stored = bot.prm.store.packet(packet["id"])
    assert stored["final_text"] == "好的，〈手机1〉 这个号码对吗？" and stored["edited"] is True


def test_drop_and_the_checks_before_sending(bot, clock, tmp_path):
    packet = pending_packet(bot)
    assert bot.prm.decide(packet["id"], "drop") == {"ok": True, "status": "dropped"}
    assert bot.prm.decide(packet["id"], "send")["ok"] is False
    assert bot.prm.decide(999, "send")["error"] == "没有找到草稿 999"
    assert bot.prm.decide(packet["id"], "explode")["ok"] is False
    no_files = Bot(clock, tmp_path, with_files=False)
    no_files.prm.register(CREATOR, "小美")
    p = pending_packet(no_files, draft(attachments_to_send=["assets/a.txt"]), files=[("a.txt", b"x")])
    assert "不能发文件" in no_files.prm.decide(p["id"], "send")["error"]
    no_files.relay.stop()


def test_a_failed_send_is_recorded_and_files_stop_at_the_first_failure(clock, tmp_path):
    b = Bot(clock, tmp_path, files_ok=False)
    b.prm.register(CREATOR, "小美")
    p = pending_packet(b, draft(attachments_to_send=["assets/a.txt", "assets/b.txt"]),
                       files=[("a.txt", b"a"), ("b.txt", b"b")])
    b.prm.decide(p["id"], "send")
    assert b.prm.send_approved() == 0
    failed = b.prm.store.packet(p["id"])
    assert failed["status"] == "failed" and failed["error"] == "文件「a.txt」没发出去" and len(b.files) == 1
    b.relay.stop()


def test_after_a_restart_a_half_sent_draft_is_not_sent_again(bot, clock):
    packet = pending_packet(bot)
    bot.prm.store.move(packet["id"], ("pending",), "sending")
    other = pending_packet(bot)
    bot.prm.decide(other["id"], "send")
    clock.now += 3600                                                       # approved, but the bot was off
    bot.prm.start()
    bot.prm.stop()
    assert bot.prm.store.packet(packet["id"])["outcome"] == "indeterminate"
    assert bot.prm.store.packet(other["id"])["outcome"] == "expired" and bot.texts == []


# ── files a prm job sends back ────────────────────────────────────────────

def test_only_a_running_prm_job_can_send_files_back(bot, tmp_path):
    body = b"x" * 10
    assert bot.relay.outfile(1, "a.txt", io.BytesIO(body).read, 10) is None          # no such job
    bot.relay.store.add(ADMIN, ADMIN, "cc 任务")
    admin = bot.relay.next_job(0)
    assert bot.relay.outfile(admin["id"], "a.txt", io.BytesIO(body).read, 10) is None  # not for cc tasks
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    answer = bot.relay.outfile(job["id"], "../../evil.txt", io.BytesIO(body).read, 10)
    [stored] = bot.relay.store.outfiles(job["id"])
    assert answer["id"] == stored["id"] and stored["name"] == "_.._evil.txt"   # stays in the job's folder
    assert (tmp_path / "relay" / "outfiles" / f"job-{job['id']}" / "_.._evil.txt").read_bytes() == body
    for n in range(MAX_OUTFILES - 1):
        assert bot.relay.outfile(job["id"], f"{n}.txt", io.BytesIO(body).read, 10)
    assert bot.relay.outfile(job["id"], "one-too-many.txt", io.BytesIO(body).read, 10) is None
    with pytest.raises(OSError):
        bot.relay.outfile(job["id"], "short.txt", io.BytesIO(b"abc").read, 10)


class TestHttp:
    def call(self, url, method="GET", body=None, headers=None, token=TOKEN, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as resp:
                text = resp.read().decode()
                return resp.status, json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_the_kinds_header_decides_and_files_go_back_over_http(self, bot):
        bot.relay.start()
        host, port = bot.relay.address
        url = f"http://{host}:{port}"
        bot.prm.handle(bot.msg("你好"))
        bot.clock.now += SETTLE_SEC + 1
        assert self.call(url + "/relay/next?wait=0") == (204, None)       # an older worker
        assert self.call(url + "/relay/next?wait=0", headers={"X-Relay-Kinds": "admin, bogus"}) == (204, None)
        status, job = self.call(url + "/relay/next?wait=0", headers={"X-Relay-Kinds": "admin,prm"})
        assert status == 200 and job["kind"] == "prm" and job["meta"]["party_id"] == 1
        assert bot.relay.snapshot()["worker_kinds"] == ["admin", "prm"]
        upload = url + f"/relay/jobs/{job['id']}/outfiles?name=" + urllib.request.quote("素材.png")
        status, answer = self.call(upload, "POST", raw=b"PNGDATA")
        assert status == 200 and answer["size"] == 7
        assert self.call(upload, "POST", raw=b"PNGDATA", token="wrong" * 8)[0] == 401
        assert self.call(url + "/relay/jobs/999/outfiles?name=a.png", "POST", raw=b"x")[0] == 404
        assert self.call(url + f"/relay/jobs/{job['id']}/outfiles", "POST", raw=b"x")[0] == 400


# ── registering creators ──────────────────────────────────────────────────

@pytest.mark.parametrize("wxid, word", [("", "要填"), ("123@chatroom", "群聊"), ("wx id", "字母开头"),
                                        ("中文名", "字母开头"), (ADMIN, "管理员"), ("wxid_helper", "管理员")])
def test_the_admin_cc_users_and_odd_ids_cannot_be_creators(wxid, word):
    with pytest.raises(PartyError) as e:
        check_wxid(wxid, {ADMIN, "wxid_helper"})
    assert word in str(e.value)


def test_registering_tells_the_backend_and_disabling_stops_drafting(bot):
    assert bot.changed == 1 and bot.prm.is_party_chat(CREATOR)
    with pytest.raises(PartyError):
        bot.prm.register("wxid_new0", "", "")
    bot.prm.set_active(1, False)
    assert bot.changed == 2 and not bot.prm.is_party_chat(CREATOR)
    assert bot.prm.handle(bot.msg("你好")) is False


def test_message_text_names_pictures_files_voice_and_stickers():
    assert message_text({"msg_type": 3, "content": "x"}) == "[图片]"
    assert message_text({"msg_type": 49, "file_name": "brief.pdf"}) == "[文件] brief.pdf"
    assert message_text({"msg_type": 34, "content": "[语音] 我想要样品"}) == "[语音] 我想要样品"
    assert message_text({"msg_type": 34, "content": "[语音]"}) == "[语音]（没转出文字）"
    assert message_text({"msg_type": 47, "content": ""}) == "[表情]"
    assert message_text({"msg_type": 1, "content": "wxid_abc: 你好"}) == "你好"


LINK_SPECIAL = {"kind": "appmsg", "type": "5", "title": "我的小红书主页", "url": "https://www.xiaohongshu.com/user/profile/abc"}


@pytest.mark.parametrize("msg, line", [
    ({"msg_type": 49, "content": "[文件]", "special": LINK_SPECIAL},
     "[链接] 我的小红书主页 https://www.xiaohongshu.com/user/profile/abc"),
    ({"msg_type": 49, "content": "[文件]", "special": {"kind": "appmsg", "type": "5", "title": "新品测评"}}, "[链接] 新品测评"),
    ({"msg_type": 49, "content": "[文件]", "special": {**LINK_SPECIAL, "url": "javascript:alert(1)"}}, "[链接] 我的小红书主页"),
    ({"msg_type": 49, "content": "[文件]", "special": {"kind": "appmsg", "type": "33", "title": "店铺"}}, "[小程序] 店铺"),
    ({"msg_type": 49, "content": "[文件]", "special": {"kind": "appmsg", "type": "51", "title": "穿搭分享"}}, "[视频号] 穿搭分享"),
    ({"msg_type": 49, "content": "[文件]", "special": {"kind": "appmsg", "type": "2000", "title": "微信转账"}}, "[转账]"),
    ({"msg_type": 49, "content": "[文件]", "special": {"kind": "appmsg", "type": "2001", "title": "恭喜发财"}}, "[红包]"),
    ({"msg_type": 48, "content": "[消息类型:48]", "special": {"kind": "location", "title": "某某大厦"}}, "[位置] 某某大厦"),
    ({"msg_type": 42, "content": "[消息类型:42]", "special": {"kind": "card", "title": "另一个达人"}}, "[名片] 另一个达人"),
    ({"msg_type": 49, "content": "[文件]", "file_name": " brief.pdf "}, "[文件] brief.pdf"),
], ids=["link-url", "link", "link-not-web", "mini-program", "channels", "transfer", "red-packet", "location", "card", "file"])
def test_message_text_is_the_edges_line(msg, line):
    """win review F2: links, mini programs, 视频号, transfers and red packets were all 「[文件]」 here, a
    location and a card 「[消息类型:48/42]」; now the copilot's draft job says what the edge's event says."""
    assert message_text(msg) == line == edge_text.message_text(msg)


def test_a_line_windows_wrote_is_kept_on_the_mac():
    """The Mac's edge backend hands on Windows' line as the content (``edge_event_id``): kept as it is."""
    for line in ("[链接] 我的小红书主页 https://www.xiaohongshu.com/user/profile/abc", "[位置] 某某大厦", "[转账]"):
        assert message_text({"msg_type": 49, "content": line, "edge_event_id": 7}) == line
    assert message_text({"msg_type": 3, "content": "[图片]", "edge_event_id": 8}) == "[图片]"
    assert message_text({"msg_type": 1, "content": "", "edge_event_id": 9}) == "[消息]"


# ── the route ─────────────────────────────────────────────────────────────

def test_the_prm_step_sits_between_the_relay_and_the_admin_commands():
    names = [step.name for step in S.ROUTE_STEPS]
    assert names.index("claude_relay") < names.index("prm") < names.index("admin_command")


class Trace:
    def __init__(self):
        self.reasons = []
        self.redacted = None

    def reason(self, code, **info):
        self.reasons.append(code)

    def redact(self, content):
        self.redacted = content


def test_the_decision_log_keeps_no_creator_text(bot, tmp_path):
    from src.decision_trace import DecisionTracer, JsonlSink
    tracer = DecisionTracer(JsonlSink(tmp_path / "trace.jsonl"))
    for chat, text in ((CREATOR, "电话 13812345678，地址张江路100号"), ("wxid_other", "你好呀")):
        msg = bot.msg(text, chat=chat)
        trace = tracer.begin(msg)
        S.prm(RouteContext(router=SimpleNamespace(_prm=bot.prm), msg=msg, trace=trace))
        trace.finish_reply(None)
    creator, other = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert creator["content"] == "[已登记达人的私聊，内容不记录]" and creator["reason"] == "prm_copilot"
    assert "13812345678" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert other["content"] == "你好呀"                                    # everyone else as before


def test_the_prm_step_ends_the_route_silently_for_creators_only(bot):
    trace = Trace()
    router = SimpleNamespace(_prm=bot.prm)
    assert S.prm(RouteContext(router=router, msg=bot.msg("你好"), trace=trace)) == Stop(None)
    assert trace.reasons == ["prm_copilot"]
    assert S.prm(RouteContext(router=router, msg=bot.msg("你好", chat="wxid_other"), trace=trace)) is None
    assert S.prm(RouteContext(router=SimpleNamespace(), msg=bot.msg("你好"), trace=trace)) is None


def test_a_creators_message_is_swallowed_even_when_drafting_breaks(bot):
    trace = Trace()
    with patch.object(bot.prm, "handle", side_effect=RuntimeError("db locked")):
        stop = S.prm(RouteContext(router=SimpleNamespace(_prm=bot.prm), msg=bot.msg("你好"), trace=trace))
    assert stop == Stop(None)                                    # never handed on to a chat reply


# ── settings ──────────────────────────────────────────────────────────────

def test_prm_needs_the_relay_and_private_chats():
    with pytest.raises(RuntimeError) as e:
        _validate_config({"prm_copilot": "on", "claude_relay": "off", "private_chat_enabled": False})
    assert "PRM_COPILOT=on needs CLAUDE_RELAY=on" in str(e.value)
    assert "PRM_COPILOT=on needs PRIVATE_CHAT_ENABLED=true" in str(e.value)
    with pytest.raises(RuntimeError) as e:
        _validate_config({"prm_copilot": "maybe"})
    assert "PRM_COPILOT must be off or on" in str(e.value)


def test_prm_is_off_by_default():
    assert BotConfig().prm_copilot == "off" and BotConfig().prm_db == "data/prm.db"


# ── the WeChat backend reads the registered creators' private chats ───────

class FakeClient:
    _config = {"myWxid": "wxid_me"}

    def __init__(self, sessions):
        self.sessions = sessions

    def get_sessions(self):
        return self.sessions

    def resolve_nickname(self, wxid):
        return {"wxid_creator01": "小美", "wxid_friend": "朋友", "mei_custom": "自定义号达人"}.get(wxid, wxid)

    def get_display_names(self, usernames):
        return {u: self.resolve_nickname(u) for u in usernames}

    def get_group_members(self, username):
        return []


def test_the_backend_reads_a_registered_creator_outside_private_chats(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                              # data/private_chat_names.json goes here
    monkeypatch.setattr(WcdbBackend, "_save_group_names", staticmethod(lambda chatrooms: None))
    sessions = [{"username": "room1@chatroom", "displayName": "测试群"},
                {"username": "wxid_creator01", "displayName": "小美"},
                {"username": "wxid_friend", "displayName": "朋友"}]
    backend = WcdbBackend(bot_display_name="机器人", groups=["*"],
                          config=BotConfig(private_chat_enabled=True, private_chats="wxid_admin"))
    backend._client = FakeClient(sessions)
    backend.set_private_filter(lambda username: username in {"wxid_creator01", "mei_custom"})
    backend._resolve_groups()
    assert backend._chat_names.get("wxid_creator01") == "小美" and "wxid_friend" not in backend._chat_names
    backend._last_session_refresh = 10 ** 9
    sessions.append({"username": "mei_custom", "displayName": "自定义号达人"})
    backend._refresh_private_chats_if_due()
    assert "mei_custom" not in backend._chat_names                         # not due yet
    backend.refresh_private_chats_soon()
    backend._refresh_private_chats_if_due()
    assert backend._chat_names.get("mei_custom") == "自定义号达人"
    # every named private chat is offered to the console's register form
    names = json.loads((tmp_path / "data" / "private_chat_names.json").read_text(encoding="utf-8"))
    assert names == {"wxid_creator01": "小美", "wxid_friend": "朋友", "mei_custom": "自定义号达人"}


def test_without_prm_the_backend_writes_no_private_names(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(WcdbBackend, "_save_group_names", staticmethod(lambda chatrooms: None))
    backend = WcdbBackend(bot_display_name="机器人", groups=["*"],
                          config=BotConfig(private_chat_enabled=True))
    backend._client = FakeClient([{"username": "wxid_friend", "displayName": "朋友"}])
    backend._resolve_groups()
    assert backend._chat_names.get("wxid_friend") == "朋友"                # PRIVATE_CHATS="*" as before
    assert not (tmp_path / "data" / "private_chat_names.json").exists()


def test_a_broken_filter_reads_nothing_extra():
    backend = WcdbBackend(bot_display_name="机器人", groups=["*"],
                          config=BotConfig(private_chat_enabled=True))
    backend.set_private_filter(lambda username: 1 / 0)
    assert backend._wants_private("wxid_creator01") is False


# ── what the next draft is told about our own replies ─────────────────────

def _drafted(b, reply, text="好的"):
    b.prm.handle(b.msg(text))
    job = b.worker_takes()
    b.finish(job["id"], draft(message_kind="faq", reply_draft=reply, reason="kb/合作政策.md"))
    return b.prm.store.packet_for_job(job["id"])


def test_the_next_draft_is_told_what_really_went_out(bot, clock):
    first = _drafted(bot, "佣金 20% 哦")
    assert bot.prm.decide(first["id"], "edit", "佣金可以给你 25%，有事打 13800138000")["ok"]
    assert bot.prm.send_approved() == 1
    clock.now += 60
    second = _drafted(bot, "那给你 30%", text="能再高点吗")
    assert bot.prm.decide(second["id"], "drop")["ok"]
    clock.now += 60
    bot.prm.handle(bot.msg("那就 25% 吧"))
    job = bot.worker_takes()
    sent = json.loads(job["meta"]) if isinstance(job["meta"], str) else job["meta"]
    told = sent["sent"]
    assert [i["what"] for i in told] == ["sent", "dropped"]
    assert told[0]["text"] == "佣金可以给你 25%，有事打 〈手机1〉" and "13800138000" not in json.dumps(told)
    assert told[0]["draft"] == "佣金 20% 哦" and told[1]["draft"] == "那给你 30%"
    assert told[0]["kind"] == "faq" and told[0]["at"]


def test_a_draft_sent_as_it_was_carries_no_second_copy_and_holding_replies_are_told(bot, clock):
    first = _drafted(bot, "收到～")
    assert bot.prm.decide(first["id"], "send")["ok"] and bot.prm.send_approved() == 1
    clock.now += 60
    _drafted(bot, "稍后回复你", text="在吗")                       # waits; the holding reply goes out
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1
    bot.prm.handle(bot.msg("？"))
    job = bot.worker_takes()
    meta = json.loads(job["meta"]) if isinstance(job["meta"], str) else job["meta"]
    # (「稍后回复你」 waited and 「？」 put it out of date: told as not sent)
    assert [(i["what"], i["kind"]) for i in meta["sent"]] == [("sent", "faq"), ("failed", "faq"), ("sent", "holding")]
    assert "draft" not in meta["sent"][0] and meta["sent"][1]["text"] == "稍后回复你" and meta["sent"][2]["text"]


def test_only_the_last_replies_are_told(bot, clock):
    for n in range(8):
        packet = _drafted(bot, f"第 {n} 条", text=f"问题 {n}")
        assert bot.prm.decide(packet["id"], "send")["ok"] and bot.prm.send_approved() == 1
        clock.now += 60
    _drafted(bot, "稍后回复你", text="在吗")                       # waits; the holding reply goes out
    clock.now += 11 * 60
    assert bot.prm.send_holding_replies() == 1
    bot.prm.handle(bot.msg("最后一个问题"))
    job = bot.worker_takes()
    meta = json.loads(job["meta"]) if isinstance(job["meta"], str) else job["meta"]
    assert [i["text"] for i in meta["sent"]] == [*[f"第 {n} 条" for n in range(4, 8)], "稍后回复你", HOLDING_TEXT]
