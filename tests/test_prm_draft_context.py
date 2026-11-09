"""PRM_DRAFT_CONTEXT, the bot's half (src/prm/draft_context.py and its hooks): ``meta.draft_context`` on a creator's
draft jobs (the 聚水潭 orders her deal knows, her platform accounts), the operator-only files of a draft (a sample order's
photo sheet: kept on the packet, never sent to her), the console's view of them and GET /api/prm/file, the sweep after
30 days, the switch.  The Mac's half is tests/test_prm_draft_context_worker.py.  Every creator, account, order and code
here is made up."""

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from test_prm import CREATOR, Bot, Clock, config, draft
from test_web_api import _build_handler

from src.config import BotConfig, _validate_config
from src.prm import draft_context
from src.prm.service import PrmService
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.web import server

OTHER = "wxid_creator02"
ACCOUNT = "test_daren_01"
SHEET = b"\xff\xd8\xff a made-up sheet"
CREATOR_ORDER, HAND_ORDER, OTHERS_ORDER = "YP2611010000001", "YP2611010000002", "YP2611010000003"


@pytest.fixture
def clock():
    return Clock()


def make(clock, tmp_path, on=True, **prm):
    b = Bot(clock, tmp_path, **({"prm_draft_context": "on"} if on else {}), **prm)
    b.prm.register(CREATOR, "小美", "测试达人")
    b.prm.register(OTHER, "阿花", "另一位")
    return b


@pytest.fixture
def bot(clock, tmp_path):
    b = make(clock, tmp_path)
    yield b
    b.prm.stop()
    b.relay.stop()


def party(b, wxid=CREATOR):
    return b.prm.store.party_by_wxid(wxid)


def deal_of(b, wxid=CREATOR):
    return b.prm.store.deal_of(party(b, wxid)["id"])


def her_job(b, text="寄出了不"):
    b.prm.handle(b.msg(text))
    return b.worker_takes()


# ── the switch ────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_switch_defaults_off_and_takes_only_off_or_on():
    from src.config import load_config

    assert BotConfig().prm_draft_context == "off"
    _validate_config({"prm_draft_context": "on"})
    with pytest.raises(RuntimeError) as e:
        _validate_config({"prm_draft_context": "maybe"})
    assert "PRM_DRAFT_CONTEXT must be off or on" in str(e.value)
    with patch.dict(os.environ, {"PRM_DRAFT_CONTEXT": "yes"}, clear=True):
        assert load_config().prm_draft_context == "on"
    with patch.dict(os.environ, {}, clear=True):
        assert load_config().prm_draft_context == "off"
    relay = ClaudeRelay(config(), store=RelayStore(":memory:"))
    assert PrmService(config(prm_draft_context="on"), relay, store=PrmStore(":memory:"))._draft_context is True
    assert PrmService(config(), relay, store=PrmStore(":memory:"))._draft_context is False   # a config without it


def test_off_the_draft_jobs_are_as_before(clock, tmp_path):
    b = make(clock, tmp_path, on=False)
    job = her_job(b)
    assert "draft_context" not in job["meta"]
    b.prm.deal_action("huopan", {"deal_id": deal_of(b, OTHER)["id"]})
    assert all("draft_context" not in json.loads(j["meta"]) for j in b.relay.store.list_jobs(10))


# ── meta.draft_context ────────────────────────────────────────────────────────────────────────────────────────

def test_her_draft_jobs_carry_her_deals_orders_and_her_accounts(bot):
    """The orders her deal knows (the bot's own, then ours made by hand that went into her deal; another creator's
    never), her 抖音号 (links, the 达人名单) and 小红书号, each once, any case."""
    me, other = party(bot), party(bot, OTHER)
    deal, others = deal_of(bot), deal_of(bot, OTHER)
    bot.prm.store.add_sample_order(CREATOR_ORDER, packet_id=1, deal_id=deal["id"], party_id=me["id"], made_at=100)
    bot.prm.store.add_sample_order(OTHERS_ORDER, packet_id=2, deal_id=others["id"], party_id=other["id"], made_at=200)
    bot.prm.store.add_hand_sample(HAND_ORDER, platform="DY", account=ACCOUNT, nickname="测试达人", applied_at=300,
                                  status="SENT", state="filled")
    bot.prm.store.update_hand_sample(HAND_ORDER, party_id=me["id"], deal_id=deal["id"])
    bot.prm.store.add_creator_link(me["id"], ACCOUNT, platform="dy", source="register")
    bot.prm.store.add_creator_link(me["id"], "xhs_test_01", platform="xhs", source="register")
    bot.prm.creator_directory = lambda: [{"wxid": CREATOR, "platform": "dy", "platform_no": ACCOUNT.upper()},
                                         {"wxid": CREATOR, "platform": "XHS", "platform_no": "xhs_test_02"},
                                         {"wxid": OTHER, "platform": "DY", "platform_no": "test_daren_99"}]
    job = her_job(bot)
    assert job["meta"]["draft_context"] == {
        "orders": [CREATOR_ORDER, HAND_ORDER], "verified": [CREATOR_ORDER],       # (the bot's own: verified)
        "accounts": [{"platform": "DY", "no": ACCOUNT}, {"platform": "XHS", "no": "xhs_test_02"},
                     {"platform": "XHS", "no": "xhs_test_01"}], "confirmed": []}


def test_an_account_two_registered_creators_share_is_neither_ones(bot):
    """Her 抖音号 linked to another registered creator too (any case): neither one's job carries it (the Mac would find
    each one's orders under both: the other's waybill, the other's hats as confirmed), as the hand-made orders and the
    百应 conversations go; the other one no longer active, it is hers again."""
    me, other = party(bot), party(bot, OTHER)
    bot.prm.store.add_creator_link(me["id"], ACCOUNT, platform="dy", source="register")
    bot.prm.store.add_creator_link(me["id"], "test_daren_02", platform="dy", source="register")
    bot.prm.store.add_creator_link(other["id"], ACCOUNT.upper(), platform="dy", source="register")
    assert her_job(bot)["meta"]["draft_context"]["accounts"] == [{"platform": "DY", "no": "test_daren_02"}]
    assert bot.prm._draft_context_meta(deal_of(bot, OTHER)["id"])["accounts"] == []
    bot.prm.set_active(other["id"], False)
    assert bot.prm._draft_context_meta(deal_of(bot)["id"])["accounts"] == [
        {"platform": "DY", "no": ACCOUNT}, {"platform": "DY", "no": "test_daren_02"}]


def test_every_kind_of_wechat_draft_job_carries_it_and_a_broken_list_still_does(bot):
    """Her message's job, 补发货盘's, 写开场白's: all through ``_reply_meta``.  The 达人名单 failing to be read (or
    anything else going wrong) leaves the key with what could be found: the Mac still checks a sample order by photo."""
    def broken():
        raise RuntimeError("the list is gone")

    bot.prm.creator_directory = broken
    job = her_job(bot)
    assert job["meta"]["draft_context"] == {"orders": [], "verified": [], "accounts": [], "confirmed": []}
    bot.prm.deal_action("huopan", {"deal_id": deal_of(bot, OTHER)["id"]})
    bot.prm.deal_action("opening", {"deal_id": deal_of(bot, OTHER)["id"]})
    metas = [json.loads(j["meta"]) for j in bot.relay.store.list_jobs(10)]
    assert all("draft_context" in m for m in metas) and len(metas) >= 2
    with patch.object(draft_context, "job_context", side_effect=RuntimeError("bad")):
        assert bot.prm._draft_context_meta(deal_of(bot)["id"]) == {"orders": [], "verified": [], "accounts": [],
                                                                   "confirmed": []}
    assert bot.prm._draft_context_meta(None) == {"orders": [], "verified": [], "accounts": [], "confirmed": []}


def test_a_new_preview_of_a_sample_order_carries_it_and_the_draft_note(bot):
    """重新预览: a sample order's job carries the context, with the codes an operator confirmed for her deal in the
    console (「对过图了」), and the deal's 起草备注 (the operators' words for the Mac: they confirm no hat); another
    action's does not."""
    deal = deal_of(bot)
    assert bot.prm.deal_action("draft_note", {"deal_id": deal["id"], "text": "确认 A7269901001B-F（她要深棕）"})["ok"]
    assert bot.prm.store.confirm_photo(deal["id"], "A7269901001A-F", None, 1.0)
    job = her_job(bot)
    action = {"action": "jst.sample_order", "shop": "dy", "items": [{"item_id": "3000000000000000102",
              "skus": [{"sku_id": "3100000000000000202", "qty": 1}]}], "creator_platform": "dy",
              "creator_account": ACCOUNT}
    block = {"version": 1, "action": action, "verdict": "login", "problems": ["登录失效"], "facts": [], "steps": [],
             "run": {}, "previewed_at": 1.0}
    bot.finish(job["id"], draft(message_kind="money", platform=block))
    [packet] = [p for p in bot.prm.store.packets() if p["tool"] == "jst.sample_order"]
    out = bot.prm.decide(packet["id"], "repreview")
    assert out["ok"], out
    meta = json.loads(bot.relay.store.get(out["job"])["meta"])
    assert meta["op"] == "platform_preview" and meta["draft_context"] == {
        "orders": [], "verified": [], "accounts": [], "confirmed": ["A7269901001A-F"]}
    assert meta["note"] == "确认 A7269901001B-F（她要深棕）"            # (her note as written: it confirms nothing)


def test_a_new_preview_of_another_action_does_not(bot):
    job = her_job(bot)
    action = {"action": "dy.designated_commission", "item_id": "3000000000000000101",
              "creator_platform_id": "5100000000000002", "commission_rate": 20}
    block = {"version": 1, "action": action, "verdict": "login", "problems": ["登录失效"], "facts": [], "steps": [],
             "run": {}, "previewed_at": 1.0}
    bot.finish(job["id"], draft(message_kind="money", platform=block))
    [packet] = [p for p in bot.prm.store.packets() if p["tool"] == "dy.designated_commission"]
    out = bot.prm.decide(packet["id"], "repreview")
    assert out["ok"] and "draft_context" not in json.loads(bot.relay.store.get(out["job"])["meta"])


# ── the operators' files ──────────────────────────────────────────────────────────────────────────────────────

def test_the_operator_files_named_by_the_mac_are_never_attachments():
    files = [{"name": "brief.txt", "path": "/r/outfiles/job-1/brief.txt", "size": 3, "sha256": "a" * 64},
             {"name": "对图单-1(2).jpg", "path": "/r/outfiles/job-1/对图单-1(2).jpg", "size": 9, "sha256": "b" * 64},
             {"name": "x.jpg", "path": "/r/outfiles/job-1/x.jpg", "size": 9, "sha256": "c" * 64},
             {"name": "对图单-7.jpg", "path": "/r/outfiles/job-1/对图单-7.jpg", "size": 9, "sha256": "d" * 64}]
    named = draft_context.operator_entries([{"name": "对图单-1.jpg", "why": "并排对比", "sha256": "B" * 64},
                                            {"name": "x.jpg", "why": "另一张"}, {"name": "../escape.jpg"}, "junk"])
    assert named == [{"name": "对图单-1.jpg", "why": "并排对比", "sha256": "b" * 64}, {"name": "x.jpg", "why": "另一张",
                                                                                "sha256": ""}]
    rest, theirs = draft_context.split_operator_files(files, named)
    assert rest == files[:1]                                    # by its bytes (renamed), by its name, the sheet's name
    assert theirs == [{"name": "对图单-1(2).jpg", "path": "/r/outfiles/job-1/对图单-1(2).jpg", "why": "并排对比"},
                      {"name": "x.jpg", "path": "/r/outfiles/job-1/x.jpg", "why": "另一张"},
                      {"name": "对图单-7.jpg", "path": "/r/outfiles/job-1/对图单-7.jpg", "why": draft_context.SHEET_WHY}]
    assert draft_context.split_operator_files(files[:1], []) == (files[:1], [])


SAMPLE_ACTION = {"action": "jst.sample_order", "shop": "dy", "items": [{"item_id": "3000000000000000102",
                 "skus": [{"sku_id": "3100000000000000202", "qty": 1}]}], "creator_platform": "dy",
                 "creator_account": ACCOUNT}
GATED = {"v": 3, "gated": True}             # the Mac's mark next to a sample order it checked by photo


def sample_block(verdict="ready", **extra):
    """The Mac's preview of SAMPLE_ACTION, ``verdict`` as given."""
    return {"version": 1, "action": SAMPLE_ACTION, "verdict": verdict,
            "problems": [] if verdict == "ready" else ["登录失效"], "facts": [], "steps": [],
            "run": {"steps": ["create"]} if verdict == "ready" else {}, "previewed_at": 1.0, **extra}


def sheet_draft(b, *, with_brief=True, verdict=None, gated=True, text="就这顶"):
    """Her draft whose job sent back a brief (an attachment) and a sheet (on the relay's operator-only channel); with
    ``verdict``, a draft of a sample order previewed so, marked as checked by photo unless ``gated`` is False."""
    job = her_job(b, text)
    if with_brief:
        assert b.relay.outfile(job["id"], "brief.txt", io.BytesIO(b"brief v1").read, 8)
    answer = b.relay.operator_file(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    extra = {}
    if verdict is not None:
        extra = {"message_kind": "money", "platform": sample_block(verdict), **({"draft_context": GATED} if gated else {})}
    b.finish(job["id"], draft(**{"message_kind": "faq", "reply_draft": "好的", "reason": "kb/合作政策.md",
                                 "attachments_to_send": ["assets/brief.txt"] if with_brief else [],
                                 "operator_files": [{"name": f"对图单-{job['id']}.jpg", "why": "并排对比，只给运营看",
                                                     "sha256": answer["sha256"]}], **extra}))
    return job, [p for p in b.prm.store.packets() if p["job_id"] == job["id"]][0]


def test_a_drafts_sheet_stays_with_the_operators_and_she_gets_only_the_attachments(bot):
    job, packet = sheet_draft(bot)
    assert [a["name"] for a in packet["attachments"]] == ["brief.txt"]
    [kept] = packet["meta"]["operator_files"]
    assert kept == {"name": f"对图单-{job['id']}.jpg", "path": kept["path"], "why": "并排对比，只给运营看"}
    assert Path(kept["path"]).read_bytes() == SHEET and Path(kept["path"]).parent.name == f"job-{job['id']}"
    assert "草稿要附" not in packet["reason"]                     # (not a missing attachment either)
    assert bot.prm.decide(packet["id"], "send")["ok"]
    bot.prm.send_approved()
    assert [Path(p).name for _, p in bot.files] == ["brief.txt"]  # the sheet never goes to her


def test_the_operators_channel_is_its_own_and_only_a_running_prm_jobs(bot):
    """The relay's operator-only files (POST /relay/jobs/<id>/operator-files): next to the job's outfiles on disk, never
    among them (a job whose 9 outfiles are taken still sends its sheet), at most 4 a job, only while a prm job runs."""
    job = her_job(bot)
    for n in range(9):
        assert bot.relay.outfile(job["id"], f"手卡-{n}.png", io.BytesIO(b"x").read, 1)
    assert bot.relay.outfile(job["id"], "手卡-9.png", io.BytesIO(b"x").read, 1) is None           # (9 a job)
    for n in range(4):
        assert bot.relay.operator_file(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    assert bot.relay.operator_file(job["id"], "对图单-x.jpg", io.BytesIO(SHEET).read, len(SHEET)) is None
    folder = bot.relay.files_dir / "outfiles" / f"job-{job['id']}"
    rows = bot.relay.store.operator_files(job["id"])
    assert [r["name"] for r in rows] == [f"对图单-{job['id']}{n}.jpg" for n in ("", "(2)", "(3)", "(4)")]
    assert all(Path(r["path"]).parent == folder and Path(r["path"]).read_bytes() == SHEET for r in rows)
    assert not (folder / "对图单-x.jpg").exists() and len(bot.relay.store.outfiles(job["id"])) == 9
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="好的", reason="kb/合作政策.md"))
    assert bot.relay.operator_file(job["id"], "对图单-y.jpg", io.BytesIO(SHEET).read, len(SHEET)) is None   # (done)


def test_a_draft_keeps_at_most_four_operator_files_and_the_rest_are_deleted(bot):
    """Four sheets on the operators' channel and one more named like a sheet among the outfiles (an older worker's way):
    four are kept, the fifth deleted, none of them an attachment."""
    job = her_job(bot)
    for n in range(4):
        assert bot.relay.operator_file(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    assert bot.relay.outfile(job["id"], "对图单-0.jpg", io.BytesIO(SHEET).read, len(SHEET))
    [legacy] = [Path(f["path"]) for f in bot.relay.store.outfiles(job["id"])]
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="好的", reason="kb/合作政策.md"))
    [packet] = [p for p in bot.prm.store.packets() if p["job_id"] == job["id"]]
    assert [f["name"] for f in packet["meta"]["operator_files"]] == [r["name"] for r in
                                                                      bot.relay.store.operator_files(job["id"])]
    assert packet["attachments"] == [] and not legacy.exists()


def test_a_sheet_no_draft_will_show_is_deleted(bot):
    """Her photo is on a sheet: one whose job failed, whose answer cannot be read, that was cancelled, or whose draft
    waits for nobody (she wrote again: a newer draft is coming) is deleted at once."""
    def sheet_of(job):
        assert bot.relay.operator_file(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
        return Path(bot.relay.store.operator_files(job["id"])[0]["path"])

    job = her_job(bot)
    path = sheet_of(job)
    bot.finish(job["id"], ok=False, text="claude 出错了")
    assert not path.exists()
    job = her_job(bot, "还在吗")
    path = sheet_of(job)
    bot.finish(job["id"], text="not a draft")
    assert not path.exists()
    job = her_job(bot, "那这个呢")
    path = sheet_of(job)
    assert bot.relay.cancel_from_console(job["id"]) == "requested"          # (an operator cancelled it meanwhile)
    assert bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "cancelled"})
    bot.relay.tick()
    assert bot.relay.store.get(job["id"])["status"] == "cancelled" and not path.exists()
    job = her_job(bot, "这顶呢")
    path = sheet_of(job)
    bot.prm.handle(bot.msg("还有这个"))                           # (a newer job: this draft is superseded)
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="好的", reason="kb/合作政策.md",
                                operator_files=[{"name": path.name, "why": "并排对比"}]))
    [packet] = [p for p in bot.prm.store.packets() if p["job_id"] == job["id"]]
    assert packet["status"] != "pending" and "operator_files" not in packet["meta"] and not path.exists()


def test_a_sample_order_the_mac_did_not_check_by_photo_is_held(clock, tmp_path):
    """PRM_DRAFT_CONTEXT on: a jst.sample_order the Mac says is ready but did not mark as checked by photo (an older
    worker, one not restarted, one whose draft_context.py did not load) waits: not ready, and says why; marked, it is as
    the Mac said; a worse verdict stays; a new preview the same (the packet keeps its last preview's mark); another
    action needs no mark; off, as before, and a draft from then cannot be approved once the switch is on, until a
    new preview the Mac checked."""
    bots = []
    try:
        bots.append(make(clock, tmp_path / "checked"))
        _, checked = sheet_draft(bots[-1], with_brief=False, verdict="ready")
        assert checked["platform"]["preview"]["verdict"] == "ready" and checked["meta"]["draft_context"] == GATED
        assert bots[-1].prm.decide(checked["id"], "repreview")["ok"]
        again = bots[-1].relay.next_job(0, ("prm",))
        bots[-1].finish(again["id"], text=json.dumps(sample_block(previewed_at=2.0)))    # (no mark this time)
        now = bots[-1].prm.store.packet(checked["id"])
        assert now["platform"]["preview"]["verdict"] == "blocked"
        assert now["platform"]["preview"]["problems"] == [draft_context.NOT_GATED]
        assert now["meta"]["draft_context"] == {"v": 3, "gated": False} and draft_context.unchecked(now)

        bots.append(make(clock, tmp_path / "held"))
        _, held = sheet_draft(bots[-1], with_brief=False, verdict="ready", gated=False)
        preview = held["platform"]["preview"]
        assert preview["verdict"] == "blocked" and preview["problems"] == ["Mac 没有对图：worker 旧了或没重启，更新、重启以后点「重新预览」"]
        [shown] = [p for p in bots[-1].prm.snapshot()["pending"] if p["id"] == held["id"]]
        assert shown["platform"]["can_execute"] is False
        assert bots[-1].prm.decide(held["id"], "execute")["ok"] is False

        bots.append(make(clock, tmp_path / "login"))
        _, login = sheet_draft(bots[-1], with_brief=False, verdict="login", gated=False)
        assert login["platform"]["preview"]["verdict"] == "login"
        assert login["platform"]["preview"]["problems"] == ["登录失效", draft_context.NOT_GATED]

        bots.append(make(clock, tmp_path / "off", on=False))
        b = bots[-1]
        _, plain = sheet_draft(b, with_brief=False, verdict="ready", gated=False)
        assert plain["platform"]["preview"]["verdict"] == "ready" and "draft_context" not in plain["meta"]
        [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == plain["id"]]
        assert shown["platform"]["can_execute"] is True
        b.prm._draft_context = True                                   # the switch turned on with it waiting
        [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == plain["id"]]
        assert shown["platform"]["can_execute"] is False and shown["platform"]["problems"] == [draft_context.NOT_GATED]
        refused = b.prm.decide(plain["id"], "execute")
        assert refused["ok"] is False and draft_context.NOT_GATED in refused["error"] and "重新预览" in refused["error"]
        assert b.prm.decide(plain["id"], "repreview")["ok"]
        again = b.relay.next_job(0, ("prm",))
        b.finish(again["id"], text=json.dumps(sample_block(previewed_at=2.0, draft_context=GATED)))
        assert b.prm.store.packet(plain["id"])["meta"]["draft_context"] == GATED
        [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == plain["id"]]
        assert shown["platform"]["can_execute"] is True and shown["platform"]["problems"] == []
    finally:
        for b in bots:
            b.prm.stop()
            b.relay.stop()
    other = {"version": 1, "action": {"action": "dy.designated_commission"}, "verdict": "ready", "problems": []}
    assert draft_context.ungated(other, False) == other and draft_context.ungated(None, False) is None
    assert draft_context.gated_of({"draft_context": {"gated": "yes"}}) is False and draft_context.gated_of("{]") is False


def test_a_mark_older_than_v3_is_held_like_none(clock, tmp_path):
    """Verification 2026-11-09 and 2026-11-10: a worker from before the colour rule marks its sample orders ``{"v": 1,
    "gated": true}`` although it took a colour never compared by photo for sure, one from before 「对过图了」 ``{"v": 2,
    "gated": true}`` although it let a hat go on 「确认 X」 in the 起草备注 (its false positives too): the bot holds either
    draft like one with no mark (「Mac 没有对图：worker 旧了或没重启，更新、重启以后点「重新预览」」), and approving it is
    refused until a new preview the updated Mac checked (v3)."""
    assert draft_context.MARK_VERSION == 3
    b = make(clock, tmp_path)
    try:
        for mark in ({"v": 1, "gated": True}, {"v": 2, "gated": True}, {"gated": True}, {"v": "3", "gated": True},
                     {"v": 3, "gated": "yes"}):
            assert draft_context.gated_of({"draft_context": mark}) is False, mark
        assert draft_context.gated_of({"draft_context": GATED}) and draft_context.mark(False) == {"v": 3, "gated": False}
        for n, old in enumerate(({"v": 1, "gated": True}, {"v": 2, "gated": True})):
            job = her_job(b, f"还有这顶 {n}")
            b.finish(job["id"], draft(message_kind="money", reply_draft="寄样单建好啦", reason="kb",
                                      platform=sample_block("ready"), draft_context=old, photo_confirmed=["A7269901001B-F"]))
            [held] = [p for p in b.prm.store.packets() if p["job_id"] == job["id"]]
            assert held["platform"]["preview"]["verdict"] == "blocked", old
            assert held["platform"]["preview"]["problems"] == [draft_context.NOT_GATED]
            assert "draft_context" not in held["meta"] and "photo_confirmed" not in held["meta"]
            refused = b.prm.decide(held["id"], "execute")
            assert refused["ok"] is False and draft_context.NOT_GATED in refused["error"] and "重新预览" in refused["error"]
    finally:
        b.prm.stop()
        b.relay.stop()


def test_a_kept_draft_an_older_rule_checked_says_to_check_it_again(bot):
    """Verification 2026-11-09 and 2026-11-10: a draft kept from before a rule (its packet's mark v1 or v2, its preview
    ready, as that bot stored it): the console and approving say 「这份是旧版 Mac 按旧规则核的：更新、重启 Mac 的 worker
    以后，点「重新预览」按新规则再核」; a new preview the updated Mac checks (v3) lets it go."""
    _, packet = sheet_draft(bot, with_brief=False, verdict="ready")
    data = bot.prm.store.packet(packet["id"])["platform"]
    assert bot.prm.store.set_platform(packet["id"], {**data, "preview": sample_block(previewed_at=1.5)}, ("pending",))
    assert draft_context.OLD_RULE == "这份是旧版 Mac 按旧规则核的：更新、重启 Mac 的 worker 以后，点「重新预览」按新规则再核"
    for old in ({"v": 1, "gated": True}, {"v": 2, "gated": True}):
        assert bot.prm.store.merge_packet_meta(packet["id"], {draft_context.KEY: old})
        kept = bot.prm.store.packet(packet["id"])
        assert draft_context.unchecked(kept) and draft_context.unchecked_reason(kept) == draft_context.OLD_RULE, old
        [shown] = [p for p in bot.prm.snapshot()["pending"] if p["id"] == packet["id"]]
        assert shown["platform"]["can_execute"] is False and shown["platform"]["problems"] == [draft_context.OLD_RULE]
        assert bot.prm.decide(packet["id"], "execute") == {"ok": False, "error": draft_context.OLD_RULE}
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    again = bot.relay.next_job(0, ("prm",))
    bot.finish(again["id"], text=json.dumps(sample_block(previewed_at=2.0, draft_context=GATED), ensure_ascii=False))
    [shown] = [p for p in bot.prm.snapshot()["pending"] if p["id"] == packet["id"]]
    assert shown["platform"]["can_execute"] is True and draft_context.unchecked_reason(bot.prm.store.packet(packet["id"])) == ""
    assert draft_context.unchecked_reason({"platform": {"action": {"action": "jst.sample_order"}}, "meta": {}}) == \
        draft_context.NOT_GATED



def test_a_reply_the_mac_left_nothing_of_for_a_bot_it_could_not_ask_leaves_no_sheet(bot):
    """合并 draft-context with channel-goods-convention (2026-11-10): the Mac held a 专属链接 back, not able to ask this bot
    what it knows (``platform_held``), and nothing of her reply is left: no draft is made of it (``_unasked``: her lines
    wait for her next draft), and the sheet its job sent goes as any job's that leaves no draft."""
    job = her_job(bot, "就这顶，链接也发我")
    row = bot.relay.operator_file(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    [kept] = bot.relay.store.operator_files(job["id"])
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="", reason="这台 Mac 连着几次都问不到 Windows 上的机器人",
                                platform_held="unasked", operator_files=[{"name": f"对图单-{job['id']}.jpg",
                                                                          "why": "并排对比", "sha256": row["sha256"]}]))
    assert bot.prm.store.packet_for_job(job["id"]) is None
    assert not Path(kept["path"]).exists()

# ── 「对过图了」 (2026-11-09): a button on the draft, no longer words in the 起草备注 ──────────────────────────────────

CODE_A, CODE_B = "A7269901001A-F", "A7269901001B-F"


def held_draft(b, held=(CODE_B,), used=(), verdict="blocked", text="要深棕那顶"):
    """Her sample order the Mac checked by photo: holding ``held`` for photos (its answer's ``photo_held``), letting
    ``used`` go only by an operator's 「对过图了」 (``photo_confirmed``)."""
    job = her_job(b, text)
    block = sample_block(verdict)
    if verdict == "blocked":
        block = {**block, "problems": [f"{c}（均码咖啡）对图没把握（款有把握，颜色没把握）：先核对再建；{draft_context.CONFIRM_HOW}"
                                       for c in held]}
    b.finish(job["id"], draft(message_kind="money", reply_draft="寄样单建好啦", reason="kb", platform=block,
                              draft_context=GATED, **({"photo_held": list(held)} if held else {}),
                              **({"photo_confirmed": list(used)} if used else {})))
    return [p for p in b.prm.store.packets() if p["job_id"] == job["id"]][0]


def shown_of(b, packet_id):
    [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == packet_id]
    return shown


def test_the_held_codes_come_with_the_macs_check_only(bot):
    """The Mac's answer names the codes its check held (``photo_held``) and let go only by a confirmation
    (``photo_confirmed``): kept on the packet when the answer carries the mark (upper case, of their shape, each once);
    an answer without the mark (or anybody's words in the draft) keeps none."""
    packet = held_draft(bot, held=(CODE_B, CODE_B.lower(), "bad code!", 7), used=(CODE_A,))
    assert packet["meta"]["photo_held"] == [CODE_B] and packet["meta"]["photo_confirmed"] == [CODE_A]
    assert draft_context.confirmable(packet) == [CODE_B, CODE_A]
    job = her_job(bot, "还有这顶")
    bot.finish(job["id"], draft(message_kind="money", reply_draft="寄样单建好啦", reason="kb",
                                platform=sample_block("ready"), photo_held=[CODE_B], photo_confirmed=[CODE_A]))
    [plain] = [p for p in bot.prm.store.packets() if p["job_id"] == job["id"]]
    assert "photo_held" not in plain["meta"] and "photo_confirmed" not in plain["meta"]
    assert draft_context.codes_of('{"photo_held": "A7269901001B-F"}', "photo_held") == []
    assert draft_context.codes_of("{]", "photo_held") == [] and draft_context.code_of(" a7269901001b-f ") == CODE_B
    assert draft_context.code_of("确认 X") == "" and draft_context.code_of(None) == ""


def test_photo_confirm_takes_only_a_code_the_drafts_last_preview_held(bot):
    """「对过图了，用 <编码>」 (the deal action ``photo_confirm`` {packet, code}): only a code the draft's last preview held
    for photos, on a sample order of that deal still waiting and not being previewed again; the switch on, the bot
    running.  It is kept for the deal (code, draft, time), one event without names; a second click records nothing."""
    packet = held_draft(bot)
    deal = deal_of(bot)

    def confirm(code, packet_id=packet["id"], deal_id=deal["id"]):
        return bot.prm.deal_action("photo_confirm", {"deal_id": deal_id, "packet": packet_id, "code": code})

    for code in (CODE_A, "", "确认 X", None, 7):
        out = confirm(code)
        assert out["ok"] is False and out["error"].endswith("只能确认预览里扣着的那顶"), (code, out)
    assert confirm(CODE_B, packet_id=packet["id"] + 99) == {"ok": False, "error": "没有找到这份草稿"}
    assert confirm(CODE_B, deal_id=deal_of(bot, OTHER)["id"]) == {"ok": False, "error": "没有找到这份草稿"}
    assert confirm(CODE_B, deal_id="x") == {"ok": False, "error": "没有找到这张合作单"}
    assert confirm(CODE_B.lower()) == {"ok": True, "code": CODE_B, "confirmed": True}
    assert confirm(CODE_B) == {"ok": True, "code": CODE_B, "confirmed": True}       # (again: nothing new)
    [row] = bot.prm.store.photo_confirmations(deal["id"])
    assert row["code"] == CODE_B and row["packet_id"] == packet["id"] and row["at"] > 0
    assert bot.prm.store.photo_confirmations(deal_of(bot, OTHER)["id"]) == []
    events = bot.prm.store.events(deal["id"], kinds=("photo_confirm",))
    assert [e["detail"] for e in events] == [{"code": CODE_B, "packet": packet["id"]}]
    from src.prm.service import event_out
    assert event_out(events[0])["name"] == f"对过图了，用 {CODE_B}（#{packet['id']} 的寄样单）"
    # a draft that is no sample order, not waiting, or being previewed again
    plain = bot.prm.store.add_packet(deal_id=deal["id"], party_id=deal["party_id"], job_id=None, chat_id=CREATOR,
                                     kind="faq", text="好的", attachments=[], intent="", slots={}, stage_proposal="",
                                     needs_attention=False, reason="", meta={"photo_held": [CODE_B]})
    assert confirm(CODE_B, packet_id=plain) == {"ok": False, "error": "这份草稿不是寄样单"}
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    assert confirm(CODE_B) == {"ok": False, "error": "正在重新预览：等新预览出来再点"}
    bot.prm._draft_context = False
    assert confirm(CODE_B)["error"] == "对图没有打开（PRM_DRAFT_CONTEXT），不用点「对过图了」"
    bot.prm._draft_context = True
    assert bot.prm.decide(packet["id"], "drop")["ok"]
    assert confirm(CODE_B) == {"ok": False, "error": "这份草稿已经处理过了"}
    from src.prm.service import apply_deal_action
    for action in ("photo_confirm", "photo_unconfirm"):           # (the bot stopped: straight on the database, refused)
        out = apply_deal_action(bot.prm.store, action, {"deal_id": deal["id"], "packet": packet["id"], "code": CODE_B},
                                now=1.0, takeover_sec=60)
        assert out == {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能确认对图"}


def test_the_next_preview_takes_a_confirmation_and_its_undo_holds_the_draft_again(bot):
    """After 「对过图了」 the card says 「已确认 <编码>，点「重新预览」按确认再核」; the new preview's job carries the code
    (``meta.draft_context.confirmed``) and the Mac's answer (ready, the code let go by it) makes the draft approvable.
    「撤销」 (``photo_unconfirm``): the code is not confirmed any more, the draft cannot be approved (the console and
    approving say so) until a new preview; the code may be confirmed again.  Each step one event, without names."""
    packet = held_draft(bot)
    deal = deal_of(bot)
    assert shown_of(bot, packet["id"])["photo_held"] == [
        {"code": CODE_B, "held": True, "used": False, "confirmed": False, "at": None}]
    assert bot.prm.deal_action("photo_confirm", {"deal_id": deal["id"], "packet": packet["id"], "code": CODE_B})["ok"]
    [view] = shown_of(bot, packet["id"])["photo_held"]
    assert view["held"] and view["confirmed"] and view["at"]
    assert bot.prm.decide(packet["id"], "execute")["ok"] is False             # (its preview still holds it)
    out = bot.prm.decide(packet["id"], "repreview")
    assert out["ok"], out
    meta = json.loads(bot.relay.store.get(out["job"])["meta"])
    assert meta["draft_context"]["confirmed"] == [CODE_B]
    job = bot.relay.next_job(0, ("prm",))
    bot.finish(job["id"], text=json.dumps({**sample_block(previewed_at=2.0), "draft_context": GATED,
                                           "photo_confirmed": [CODE_B]}, ensure_ascii=False))
    now = bot.prm.store.packet(packet["id"])
    assert now["meta"]["photo_held"] == [] and now["meta"]["photo_confirmed"] == [CODE_B]
    shown = shown_of(bot, packet["id"])
    assert shown["platform"]["can_execute"] is True and shown["photo_held"] == [
        {"code": CODE_B, "held": False, "used": True, "confirmed": True, "at": view["at"]}]
    assert bot.prm.deal_action("photo_unconfirm", {"deal_id": deal["id"], "code": CODE_B.lower()}) == {
        "ok": True, "code": CODE_B, "confirmed": False}
    assert bot.prm.store.photo_confirmations(deal["id"]) == []
    shown = shown_of(bot, packet["id"])
    reason = f"撤销了 {CODE_B} 的「对过图了」：点「重新预览」再核"
    assert shown["platform"]["can_execute"] is False and reason in shown["platform"]["problems"]
    assert shown["photo_held"] == [{"code": CODE_B, "held": False, "used": True, "confirmed": False, "at": None}]
    assert bot.prm.decide(packet["id"], "execute") == {"ok": False, "error": reason}
    assert bot.prm.deal_action("photo_unconfirm", {"deal_id": deal["id"], "code": CODE_B})["ok"]   # (again: nothing)
    kinds = [e["kind"] for e in reversed(bot.prm.store.events(deal["id"], kinds=("photo_confirm", "photo_unconfirm")))]
    assert kinds == ["photo_confirm", "photo_unconfirm"]
    from src.prm.service import event_out
    assert event_out(bot.prm.store.events(deal["id"], kinds=("photo_unconfirm",))[0])["name"] == \
        f"撤销了 {CODE_B} 的「对过图了」"
    assert bot.prm.deal_action("photo_unconfirm", {"deal_id": deal["id"], "code": "x"}) == {"ok": False,
                                                                                         "error": "编码不对"}
    # confirmed again (its preview let it go only by one): approvable again, the same preview
    assert bot.prm.deal_action("photo_confirm", {"deal_id": deal["id"], "packet": packet["id"], "code": CODE_B})["ok"]
    assert shown_of(bot, packet["id"])["platform"]["can_execute"] is True
    # a confirmation for her deal goes with every later draft job of hers, and never with another creator's
    assert her_job(bot, "那这顶呢")["meta"]["draft_context"]["confirmed"] == [CODE_B]
    assert bot.prm._draft_context_meta(deal_of(bot, OTHER)["id"])["confirmed"] == []


def test_the_note_confirms_no_hat_any_more(bot):
    """Verification of 80581b7 (2026-11-09): words in the 起草备注 never let a held hat go, 「确认 <编码>」 on a line of
    its own included: the note goes to the Mac as the operators wrote it, and only the console's 「对过图了」 is sent as
    a confirmation."""
    deal = deal_of(bot)
    for n, note in enumerate((f"确认 {CODE_B}", f"她要深棕\n确认 {CODE_B}", f"对过图了 {CODE_B}", f"已确认：{CODE_B}")):
        assert bot.prm.deal_action("draft_note", {"deal_id": deal["id"], "text": note})["ok"]
        job = her_job(bot, f"这顶呢 {n}")
        assert job["meta"]["draft_context"]["confirmed"] == [] and job["meta"]["note"] == note
        bot.finish(job["id"], draft(message_kind="faq", reply_draft="好的", reason="kb"))
    assert bot.prm.store.photo_confirmations(deal["id"]) == []


def test_the_console_shows_the_sheet_and_serves_it_while_the_bot_runs_and_after(bot, tmp_path, monkeypatch):
    job, packet = sheet_draft(bot, with_brief=False)
    url = f"/api/prm/file?packet={packet['id']}&kind=operator&n=0"
    [shown] = [p for p in bot.prm.snapshot()["pending"] if p["id"] == packet["id"]]
    assert shown["operator_files"] == [{"n": 0, "name": f"对图单-{job['id']}.jpg", "why": "并排对比，只给运营看",
                                        "url": url, "kept": True}]
    assert shown["attachments"] == []
    server.register_prm(bot.prm)
    try:
        status, head, body = fetch(url)
        assert (status, head["content-type"], body) == (200, "image/jpeg", SHEET)
        assert head["content-length"] == str(len(SHEET)) and head["cache-control"] == "no-store"
        assert head["x-content-type-options"] == "nosniff"          # (review 2026-11-10: never sniffed as anything else)
    finally:
        server.register_prm(None)
    # the bot stopped: read from its database, closed again after the request; only from inside the relay's folder as a
    # stopped bot finds it (next to CLAUDE_RELAY_DB: review 2026-11-10), the running relay's folder being another
    stopped_root = tmp_path / "stopped"
    monkeypatch.setenv("CLAUDE_RELAY_DB", str(stopped_root / "claude_relay.db"))
    kept = stopped_root / "claude_relay_files" / "outfiles" / f"job-{job['id']}" / f"对图单-{job['id']}.jpg"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(SHEET)
    db = tmp_path / "prm.db"
    stored = PrmStore(db)
    stored.add_party(CREATOR, "小美", now=1)
    pid = stored.add_packet(deal_id=1, party_id=1, job_id=job["id"], chat_id=CREATOR, kind="faq", text="好的",
                            attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
                            meta={"operator_files": [{**packet["meta"]["operator_files"][0], "path": str(kept)}]}, now=1)
    elsewhere = stored.add_packet(deal_id=1, party_id=1, job_id=job["id"], chat_id=CREATOR, kind="faq", text="好的",
                                  attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False,
                                  reason="", meta={"operator_files": packet["meta"]["operator_files"]}, now=1)
    stored.close()
    monkeypatch.setenv("PRM_DB", str(db))
    opened, closed = server._prm_stopped_store, []

    def stopped():
        store = opened()
        close = store.close
        store.close = lambda: (closed.append(True), close())[1]
        return store

    monkeypatch.setattr(server, "_prm_stopped_store", stopped)
    status, head, body = fetch(f"/api/prm/file?packet={pid}&kind=operator&n=0")
    assert (status, head["content-type"], body) == (200, "image/jpeg", SHEET) and closed == [True]
    assert head["x-content-type-options"] == "nosniff"
    assert fetch(f"/api/prm/file?packet={elsewhere}&kind=operator&n=0")[0] == 404     # (not in the folder it reads)


def test_the_file_endpoint_keeps_to_its_contract(bot):
    """GET /api/prm/file?packet=&kind=&n=: ``kind`` attachment (the draft's own picture: a 直播手卡) or operator (its
    sheet); packet, kind and n all needed, the numbers plain digits; only a picture; 404 as JSON with its length and the
    CORS echo; a cross-site fetch 403 (Sec-Fetch-Site, the Origin, the Host); POST 405."""
    job = her_job(bot, "要手卡")
    card = b"\x89PNG a made-up card"
    assert bot.relay.outfile(job["id"], "手卡-1.png", io.BytesIO(card).read, len(card))
    assert bot.relay.outfile(job["id"], "brief.txt", io.BytesIO(b"brief v1").read, 8)
    sheet = bot.relay.operator_file(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    bot.finish(job["id"], draft(message_kind="faq", reply_draft="好的", reason="kb/合作政策.md",
                                attachments_to_send=["assets/手卡-1.png", "assets/brief.txt"],
                                operator_files=[{"name": f"对图单-{job['id']}.jpg", "why": "并排对比",
                                                 "sha256": sheet["sha256"]}]))
    [packet] = [p for p in bot.prm.store.packets() if p["job_id"] == job["id"]]
    assert [a["name"] for a in packet["attachments"]] == ["手卡-1.png", "brief.txt"]
    base, local = f"/api/prm/file?packet={packet['id']}", "http://localhost:5173"
    server.register_prm(bot.prm)
    try:
        status, head, body = fetch(f"{base}&kind=attachment&n=0")
        assert (status, head["content-type"], body) == (200, "image/png", card)
        assert fetch(f"{base}&n=0&kind=operator")[2] == SHEET                  # (the parameters in any order)
        for bad in (f"{base}&kind=attachment&n=1",                            # brief.txt: not a picture
                    f"{base}&kind=attachment&n=2", f"{base}&kind=operator&n=1", f"{base}&n=0", f"{base}&kind=&n=0",
                    f"{base}&kind=Operator&n=0", f"{base}&kind=file&n=0", f"{base}&kind=operator",
                    f"{base}&kind=operator&n=", f"{base}&kind=operator&n=x", f"{base}&kind=operator&n=-1",
                    f"{base}&kind=operator&n=+0", f"{base}&kind=operator&n=%200", f"{base}&kind=operator&n=0x0",
                    f"{base}&kind=operator&n=0.0", f"{base}&kind=operator&n=%EF%BC%90",       # (a full-width ０)
                    f"/api/prm/file?packet=+{packet['id']}&kind=operator&n=0", "/api/prm/file?kind=operator&n=0",
                    "/api/prm/file?packet=999&kind=operator&n=0", "/api/prm/file"):
            status, head, body = fetch(bad, origin=local)
            assert status == 404 and json.loads(body)["ok"] is False, bad
            assert head["content-length"] == str(len(body)) and head["content-type"].startswith("application/json")
            assert head["access-control-allow-origin"] == local, bad
        assert fetch(f"{base}&kind=operator&n=0", origin=local)[1]["access-control-allow-origin"] == local
        assert fetch(f"{base}&kind=operator&n=0", headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            assert fetch(f"{base}&kind=operator&n=0", headers=headers)[0] == 403, headers
        assert fetch(f"{base}&kind=operator&n=0", method="POST")[0] == 405
    finally:
        server.register_prm(None)


def fetch(path, method="GET", origin=None, headers=None):
    """One request to the console's handler: ``(status, headers by lower-case name, body)``."""
    sent = {**({"Origin": origin} if origin else {}), **(headers or {})}
    _, sock = _build_handler(path, method=method, headers=sent)
    raw = b"".join(sock._sent_chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    fields = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
    return int(lines[0].split()[1]), fields, body


def test_only_a_picture_the_relay_keeps_for_a_job_is_served(tmp_path):
    """Of either kind only a picture inside the relay's folder (``root``, links followed: review 2026-11-10, as the WeChat
    notification's pictures); no folder given, none; an operator file only from ``outfiles/job-<id>/``."""
    root = tmp_path / "relay"
    folder = root / "outfiles" / "job-4"
    folder.mkdir(parents=True)
    for name in ("a.jpg", "a.txt"):
        (folder / name).write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")                          # (outside the relay's folder)
    (root / "c.jpg").write_bytes(b"x")                              # (the relay's, but no job's)
    (folder / "link.jpg").symlink_to(tmp_path / "b.jpg")            # (inside, pointing out)

    def packet(path):
        return {"id": 1, "meta": {"operator_files": [{"name": "f", "path": str(path), "why": ""}]}}

    assert draft_context.operator_file(packet(folder / "a.jpg"), 0, root) == ((folder / "a.jpg").resolve(), "image/jpeg")
    assert draft_context.operator_file(packet(folder / "a.jpg"), 0) is None                    # (no folder: nothing)
    for path in (folder / "a.txt", tmp_path / "b.jpg", folder / "gone.jpg", "", folder / "link.jpg", root / "c.jpg",
                 folder / ".." / ".." / ".." / "b.jpg"):
        assert draft_context.operator_file(packet(path), 0, root) is None, path
    assert draft_context.operator_file({"id": 1, "meta": {"operator_files": "junk"}}, 0, root) is None
    assert draft_context.operator_files_out(packet(folder / "gone.jpg"))[0]["kept"] is False
    assert draft_context.picture(packet(folder / "a.jpg"), "attachment", 0, root) is None   # (not one of its attachments)
    assert draft_context.picture(packet(folder / "a.jpg"), "operator", -1, root) is None
    card = {"id": 2, "attachments": ["junk", {"name": "a.jpg", "path": str(folder / "a.jpg")},
                                     {"name": "b.jpg", "path": str(tmp_path / "b.jpg")},
                                     {"name": "link.jpg", "path": str(folder / "link.jpg")}]}
    assert draft_context.picture(card, "attachment", 1, root) == ((folder / "a.jpg").resolve(), "image/jpeg")
    assert draft_context.picture(card, "attachment", 1) is None                                # (no folder: nothing)
    for n in (2, 3):                                                 # outside the relay's folder, or a link out of it
        assert draft_context.picture(card, "attachment", n, root) is None, n
    assert draft_context.picture(card, "attachment", 0, root) is None
    assert draft_context.picture(card, "operator", 1, root) is None


def test_a_new_previews_sheet_replaces_the_drafts_and_the_old_one_is_deleted(bot):
    job, packet = sheet_draft(bot, with_brief=False, verdict="login")
    assert packet["tool"] == "jst.sample_order"
    first = packet["meta"]["operator_files"][0]["path"]
    out = bot.prm.decide(packet["id"], "repreview")
    assert out["ok"], out
    again = bot.relay.next_job(0, ("prm",))
    answer = bot.relay.operator_file(again["id"], f"对图单-{again['id']}.jpg", io.BytesIO(SHEET + b"2").read,
                                     len(SHEET) + 1)
    block = sample_block(previewed_at=2.0, draft_context=GATED, operator_files=[
        {"name": f"对图单-{again['id']}.jpg", "why": "新的对图单", "sha256": answer["sha256"]}])
    bot.finish(again["id"], text=json.dumps(block, ensure_ascii=False))
    now = bot.prm.store.packet(packet["id"])
    [kept] = now["meta"]["operator_files"]
    assert kept["why"] == "新的对图单" and kept["path"] != first and Path(kept["path"]).read_bytes() == SHEET + b"2"
    assert now["platform"]["preview"]["verdict"] == "ready"
    assert not Path(first).exists()                               # (her photo is on it: no orphan left behind)


def test_a_new_preview_that_cannot_be_used_keeps_the_drafts_sheet_and_drops_its_own(bot):
    """The Mac's new preview is not a preview (an answer the bot cannot read): the old preview stays, marked, and so does
    the draft's sheet; the sheet that job sent is deleted.  One that comes after the draft was dealt with: the same."""
    job, packet = sheet_draft(bot, with_brief=False, verdict="login")
    first = packet["meta"]["operator_files"]
    out = bot.prm.decide(packet["id"], "repreview")
    assert out["ok"], out
    again = bot.relay.next_job(0, ("prm",))
    answer = bot.relay.operator_file(again["id"], f"对图单-{again['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    [sent] = [Path(f["path"]) for f in bot.relay.store.operator_files(again["id"])]
    bot.finish(again["id"], text=json.dumps({"operator_files": [{"name": f"对图单-{again['id']}.jpg", "why": "新的",
                                                                 "sha256": answer["sha256"]}]}))
    now = bot.prm.store.packet(packet["id"])
    assert now["meta"]["operator_files"] == first and Path(first[0]["path"]).exists() and not sent.exists()
    assert any("重新预览没做成" in p for p in now["platform"]["preview"]["problems"])
    # too late: the draft was dropped while the Mac previewed it again
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    late = bot.relay.next_job(0, ("prm",))
    assert bot.prm.decide(packet["id"], "drop")["ok"]
    bot.relay.operator_file(late["id"], f"对图单-{late['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    [sent] = [Path(f["path"]) for f in bot.relay.store.operator_files(late["id"])]
    bot.finish(late["id"], text=json.dumps(sample_block(previewed_at=3.0, draft_context=GATED), ensure_ascii=False))
    assert not sent.exists() and bot.prm.store.packet(packet["id"])["meta"]["operator_files"] == first


def test_a_new_preview_lost_on_the_way_keeps_its_sheet_for_the_late_answer(bot, clock):
    """The Mac went quiet while it previewed again (lost): the old preview stays, marked, and the sheet that job sent is
    kept (its answer may still come); the answer comes after all: its sheet replaces the draft's."""
    from src.relay.service import LOST_AFTER_SEC

    job, packet = sheet_draft(bot, with_brief=False, verdict="login")
    first = Path(packet["meta"]["operator_files"][0]["path"])
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    again = bot.relay.next_job(0, ("prm",))
    answer = bot.relay.operator_file(again["id"], f"对图单-{again['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    [sent] = [Path(f["path"]) for f in bot.relay.store.operator_files(again["id"])]
    clock.now += LOST_AFTER_SEC + 1
    bot.relay.tick()
    assert bot.relay.store.get(again["id"])["status"] == "lost" and sent.exists() and first.exists()
    assert any("Mac 失联了" in p for p in bot.prm.store.packet(packet["id"])["platform"]["preview"]["problems"])
    bot.finish(again["id"], text=json.dumps(sample_block(previewed_at=2.0, draft_context=GATED, operator_files=[
        {"name": sent.name, "why": "新的对图单", "sha256": answer["sha256"]}]), ensure_ascii=False))
    now = bot.prm.store.packet(packet["id"])
    assert [f["path"] for f in now["meta"]["operator_files"]] == [str(sent)] and not first.exists()
    assert now["platform"]["preview"]["verdict"] == "ready"


def test_a_late_answer_of_an_earlier_new_preview_never_overwrites_a_newer_one(bot, clock):
    """Verification 2026-11-09: a 重新预览 lost on the way, a second one asked for and answered, then the first one's
    answer after all: it is not this draft's preview any more (its verdict, its sheet, its mark are dropped, its sheet
    deleted); the newer preview stays."""
    from src.relay.service import LOST_AFTER_SEC

    job, packet = sheet_draft(bot, with_brief=False, verdict="login")
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    first = bot.relay.next_job(0, ("prm",))
    bot.relay.operator_file(first["id"], f"对图单-{first['id']}.jpg", io.BytesIO(SHEET + b"1").read, len(SHEET) + 1)
    [late] = [Path(f["path"]) for f in bot.relay.store.operator_files(first["id"])]
    clock.now += LOST_AFTER_SEC + 1
    bot.relay.tick()                                              # (lost)
    assert bot.prm.decide(packet["id"], "repreview")["ok"]        # asked again
    second = bot.relay.next_job(0, ("prm",))
    answer = bot.relay.operator_file(second["id"], f"对图单-{second['id']}.jpg", io.BytesIO(SHEET + b"2").read,
                                     len(SHEET) + 1)
    bot.finish(second["id"], text=json.dumps(sample_block(previewed_at=3.0, draft_context=GATED, operator_files=[
        {"name": f"对图单-{second['id']}.jpg", "why": "第二次的", "sha256": answer["sha256"]}]), ensure_ascii=False))
    newer = bot.prm.store.packet(packet["id"])
    assert newer["platform"]["preview"]["verdict"] == "ready" and newer["meta"]["draft_context"] == GATED
    bot.finish(first["id"], text=json.dumps(sample_block("login", previewed_at=2.0, operator_files=[
        {"name": late.name, "why": "第一次的"}]), ensure_ascii=False))       # (the first one's, late, not checked)
    after = bot.prm.store.packet(packet["id"])
    assert after["platform"] == newer["platform"] and after["meta"] == newer["meta"] and not late.exists()
    assert Path(after["meta"]["operator_files"][0]["path"]).read_bytes() == SHEET + b"2"


def test_a_new_preview_without_a_sheet_clears_the_drafts(bot):
    """Verification 2026-11-09 (B2): the new preview brings no sheet (no picture behind its hats now, the 起草备注
    confirmed them): the old sheet was not made for it, it goes (the file too)."""
    job, packet = sheet_draft(bot, with_brief=False, verdict="login")
    first = Path(packet["meta"]["operator_files"][0]["path"])
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    again = bot.relay.next_job(0, ("prm",))
    bot.finish(again["id"], text=json.dumps(sample_block(previewed_at=2.0, draft_context=GATED), ensure_ascii=False))
    now = bot.prm.store.packet(packet["id"])
    assert now["platform"]["preview"]["verdict"] == "ready" and now["meta"]["operator_files"] == []
    assert not first.exists()
    [shown] = [p for p in bot.prm.snapshot()["pending"] if p["id"] == packet["id"]]
    assert shown["operator_files"] == [] and shown["platform"]["can_execute"] is True


def test_a_new_preview_nothing_checked_never_keeps_an_older_mark(bot):
    """Verification 2026-11-09 (B4): the draft was checked by photo; the switch off, a 重新预览 checks nothing: the packet
    no longer says checked, so with the switch on again it cannot be approved until a checked preview."""
    _, packet = sheet_draft(bot, with_brief=False, verdict="ready")
    assert packet["meta"]["draft_context"] == GATED
    bot.prm._draft_context = False
    assert bot.prm.decide(packet["id"], "repreview")["ok"]
    again = bot.relay.next_job(0, ("prm",))
    assert "draft_context" not in json.loads(bot.relay.store.get(again["id"])["meta"])
    bot.finish(again["id"], text=json.dumps(sample_block(previewed_at=2.0), ensure_ascii=False))      # (no mark)
    now = bot.prm.store.packet(packet["id"])
    assert now["meta"]["draft_context"] == {"v": 3, "gated": False} and now["platform"]["preview"]["verdict"] == "ready"
    bot.prm._draft_context = True
    [shown] = [p for p in bot.prm.snapshot()["pending"] if p["id"] == packet["id"]]
    assert shown["platform"]["can_execute"] is False and bot.prm.decide(packet["id"], "execute")["ok"] is False


def test_off_a_sample_order_is_approved_as_before(clock, tmp_path):
    """Verification 2026-11-09 (B1): the switch off, a sample order no Mac checked by photo is approved and goes to the
    Mac as it always did."""
    b = make(clock, tmp_path, on=False)
    try:
        _, plain = sheet_draft(b, with_brief=False, verdict="ready", gated=False)
        [shown] = [p for p in b.prm.snapshot()["pending"] if p["id"] == plain["id"]]
        assert shown["platform"]["can_execute"] is True
        out = b.prm.decide(plain["id"], "execute")
        assert out["ok"] and out["status"] == "executing", out
    finally:
        b.prm.stop()
        b.relay.stop()


def test_the_operators_channel_takes_only_a_drafts_or_a_new_previews_files_and_names_them_as_sheets(bot, clock):
    """Verification 2026-11-09 (B3): a file on the operator-only channel of a draft's job is stored with a name that
    starts with 对图单- (so it is never taken for an attachment, and the sweep knows it); an execution's job, a
    sample-status read's, a clean-up's take none; the sweep goes by the channel's rows: a file no packet keeps goes
    after 30 days too, and its row is forgotten."""
    job = her_job(bot)
    assert bot.relay.operator_file(job["id"], "photo.jpg", io.BytesIO(SHEET).read, len(SHEET))
    [row] = bot.relay.store.operator_files(job["id"])
    path = Path(row["path"])
    assert row["name"] == path.name == "对图单-photo.jpg"
    for op in ("sample_status", "purge", "platform_execute", "buyin_inbox"):
        _, other = bot.relay.submit(CREATOR, CREATOR, "", kind="prm", meta={"op": op, "party_id": 1}, fresh=True)
        assert bot.relay.next_job(0, ("prm",))["id"] == other
        assert bot.relay.operator_file(other, "对图单-1.jpg", io.BytesIO(SHEET).read, len(SHEET)) is None, op
        assert bot.relay.store.operator_files(other) == []
    assert bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "x"})     # (never ticked: no draft keeps it)
    os.utime(path, (clock.now - 31 * 86400, clock.now - 31 * 86400))
    assert draft_context.sweep(bot.prm.store, clock.now, relay_store=bot.relay.store) == 1 and not path.exists()
    assert bot.relay.store.operator_files(job["id"]) == []
    assert draft_context.sweep(bot.prm.store, clock.now, relay_store=bot.relay.store) == 0


def test_the_sweep_never_writes_over_a_sheet_that_came_meanwhile(bot, clock):
    """Verification 2026-11-09: the sweep read a packet's sheets, a new preview put its own there meanwhile: the sweep's
    write back is compare-and-set, so the new sheet stays (the old file is left for the next sweep)."""
    job, packet = sheet_draft(bot, with_brief=False)
    old = Path(packet["meta"]["operator_files"][0]["path"])
    os.utime(old, (clock.now - 31 * 86400, clock.now - 31 * 86400))
    read = bot.prm.store.operator_file_packets()                 # (what the sweep reads first)
    fresh = old.with_name("对图单-9.jpg")
    fresh.write_bytes(SHEET)
    newer = [{"name": fresh.name, "path": str(fresh), "why": "新的"}]
    assert bot.prm.store.merge_packet_meta(packet["id"], {"operator_files": newer})     # (a new preview's, meanwhile)
    with patch.object(bot.prm.store, "operator_file_packets", return_value=read):
        assert draft_context.sweep(bot.prm.store, clock.now) == 0
    assert bot.prm.store.packet(packet["id"])["meta"]["operator_files"] == newer and fresh.exists() and old.exists()
    assert draft_context.sweep(bot.prm.store, clock.now, files_dir=bot.relay.files_dir) == 1 and not old.exists()
    assert fresh.exists()


def test_the_sheets_go_after_thirty_days_by_their_own_time(bot, clock):
    job, packet = sheet_draft(bot, with_brief=False)
    path = Path(packet["meta"]["operator_files"][0]["path"])
    os.utime(path, (clock.now, clock.now))                        # (written at the test clock's time)
    assert draft_context.sweep(bot.prm.store, clock.now + 29 * 86400) == 0 and path.exists()
    assert draft_context.sweep(bot.prm.store, clock.now + 31 * 86400) == 1 and not path.exists()
    kept = bot.prm.store.packet(packet["id"])["meta"]["operator_files"]
    assert kept[0]["path"] == "" and kept[0]["name"] == f"对图单-{job['id']}.jpg"
    assert bot.prm.store.operator_file_packets() == []                               # looked at once
    assert draft_context.operator_files_out(bot.prm.store.packet(packet["id"]))[0]["kept"] is False


def test_the_sweep_takes_the_sheets_no_draft_keeps_too(bot, clock):
    """A sheet of the relay's outfiles/job-*/ no packet keeps (one a new preview replaced but could not delete, one of a
    job that never came back): named like a sheet, it goes after 30 days too; no other file is this sweep's."""
    folder = bot.relay.files_dir / "outfiles" / "job-99"
    folder.mkdir(parents=True)
    old, fresh, other = folder / "对图单-99.jpg", folder / "对图单-99(2).jpg", folder / "brief.txt"
    for path in (old, fresh, other):
        path.write_bytes(b"x")
        os.utime(path, (clock.now - 31 * 86400, clock.now - 31 * 86400))
    os.utime(fresh, (clock.now, clock.now))
    assert draft_context.sweep(bot.prm.store, clock.now) == 0 and old.exists()          # (without the relay's folder)
    assert draft_context.sweep(bot.prm.store, clock.now, files_dir=bot.relay.files_dir) == 1
    assert not old.exists() and fresh.exists() and other.exists()


def test_the_hourly_clean_up_runs_the_sweep(bot, clock):
    job, packet = sheet_draft(bot, with_brief=False)
    path = Path(packet["meta"]["operator_files"][0]["path"])
    orphan = path.with_name("对图单-0.jpg")
    orphan.write_bytes(b"x")
    for p in (path, orphan):
        os.utime(p, (clock.now, clock.now))
    clock.now += 31 * 86400
    bot.prm.purge_due()
    assert not path.exists() and not orphan.exists()
