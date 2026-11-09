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
                     {"platform": "XHS", "no": "xhs_test_01"}]}


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
    assert job["meta"]["draft_context"] == {"orders": [], "verified": [], "accounts": []}
    bot.prm.deal_action("huopan", {"deal_id": deal_of(bot, OTHER)["id"]})
    bot.prm.deal_action("opening", {"deal_id": deal_of(bot, OTHER)["id"]})
    metas = [json.loads(j["meta"]) for j in bot.relay.store.list_jobs(10)]
    assert all("draft_context" in m for m in metas) and len(metas) >= 2
    with patch.object(draft_context, "job_context", side_effect=RuntimeError("bad")):
        assert bot.prm._draft_context_meta(deal_of(bot)["id"]) == {"orders": [], "verified": [], "accounts": []}
    assert bot.prm._draft_context_meta(None) == {"orders": [], "verified": [], "accounts": []}


def test_a_new_preview_of_a_sample_order_carries_it_and_the_draft_note(bot):
    """重新预览: a sample order's job carries the context and the deal's 起草备注 (the codes it confirms, 「确认 <编码>」);
    another action's does not."""
    deal = deal_of(bot)
    assert bot.prm.deal_action("draft_note", {"deal_id": deal["id"], "text": "确认 A7269901001B-F（她要深棕）"})["ok"]
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
    assert meta["op"] == "platform_preview" and meta["draft_context"] == {"orders": [], "verified": [], "accounts": []}
    assert meta["note"] == "确认 A7269901001B-F（她要深棕）"


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
GATED = {"v": 1, "gated": True}             # the Mac's mark next to a sample order it checked by photo


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
        assert now["meta"]["draft_context"] == {"v": 1, "gated": False} and draft_context.unchecked(now)

        bots.append(make(clock, tmp_path / "held"))
        _, held = sheet_draft(bots[-1], with_brief=False, verdict="ready", gated=False)
        preview = held["platform"]["preview"]
        assert preview["verdict"] == "blocked" and preview["problems"] == ["Mac 没有对图：worker 旧了或没重启"]
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
    finally:
        server.register_prm(None)
    # the bot stopped: read from its database, closed again after the request
    db = tmp_path / "prm.db"
    stored = PrmStore(db)
    stored.add_party(CREATOR, "小美", now=1)
    pid = stored.add_packet(deal_id=1, party_id=1, job_id=job["id"], chat_id=CREATOR, kind="faq", text="好的",
                            attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
                            meta={"operator_files": packet["meta"]["operator_files"]}, now=1)
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
    folder = tmp_path / "relay" / "outfiles" / "job-4"
    folder.mkdir(parents=True)
    for name in ("a.jpg", "a.txt"):
        (folder / name).write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")

    def packet(path):
        return {"id": 1, "meta": {"operator_files": [{"name": "f", "path": str(path), "why": ""}]}}

    assert draft_context.operator_file(packet(folder / "a.jpg"), 0) == (folder / "a.jpg", "image/jpeg")
    for path in (folder / "a.txt", tmp_path / "b.jpg", folder / "gone.jpg", ""):
        assert draft_context.operator_file(packet(path), 0) is None, path
    assert draft_context.operator_file({"id": 1, "meta": {"operator_files": "junk"}}, 0) is None
    assert draft_context.operator_files_out(packet(folder / "gone.jpg"))[0]["kept"] is False
    assert draft_context.picture(packet(folder / "a.jpg"), "attachment", 0) is None       # (not one of its attachments)
    assert draft_context.picture(packet(folder / "a.jpg"), "operator", -1) is None
    card = {"id": 2, "attachments": ["junk", {"name": "b.jpg", "path": str(tmp_path / "b.jpg")}]}
    assert draft_context.picture(card, "attachment", 1) == (tmp_path / "b.jpg", "image/jpeg")   # (n as the console counts)
    assert draft_context.picture(card, "attachment", 0) is None and draft_context.picture(card, "operator", 1) is None


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
