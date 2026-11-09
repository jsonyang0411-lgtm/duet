"""PRM_DRAFT_CONTEXT, the bot's half (src/prm/draft_context.py and its hooks): ``meta.draft_context`` on a creator's
draft jobs (the 聚水潭 orders her deal knows, her platform accounts), the operator-only files of a draft (a sample order's
photo sheet: kept on the packet, never sent to her), the console's view of them and GET /api/prm/file, the sweep after
30 days, the switch.  The Mac's half is tests/test_prm_draft_context_worker.py.  Every creator, account, order and code
here is made up."""

import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
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
        "orders": [CREATOR_ORDER, HAND_ORDER],
        "accounts": [{"platform": "DY", "no": ACCOUNT}, {"platform": "XHS", "no": "xhs_test_02"},
                     {"platform": "XHS", "no": "xhs_test_01"}]}


def test_every_kind_of_wechat_draft_job_carries_it_and_a_broken_list_still_does(bot):
    """Her message's job, 补发货盘's, 写开场白's: all through ``_reply_meta``.  The 达人名单 failing to be read (or
    anything else going wrong) leaves the key with what could be found: the Mac still checks a sample order by photo."""
    def broken():
        raise RuntimeError("the list is gone")

    bot.prm.creator_directory = broken
    job = her_job(bot)
    assert job["meta"]["draft_context"] == {"orders": [], "accounts": []}
    bot.prm.deal_action("huopan", {"deal_id": deal_of(bot, OTHER)["id"]})
    bot.prm.deal_action("opening", {"deal_id": deal_of(bot, OTHER)["id"]})
    metas = [json.loads(j["meta"]) for j in bot.relay.store.list_jobs(10)]
    assert all("draft_context" in m for m in metas) and len(metas) >= 2
    with patch.object(draft_context, "job_context", side_effect=RuntimeError("bad")):
        assert bot.prm._draft_context_meta(deal_of(bot)["id"]) == {"orders": [], "accounts": []}
    assert bot.prm._draft_context_meta(None) == {"orders": [], "accounts": []}


def test_a_new_preview_of_a_sample_order_carries_it_and_the_draft_note(bot):
    """重新预览: a sample order's job carries the context and the deal's 起草备注 (codes it confirms); another action's
    does not."""
    deal = deal_of(bot)
    assert bot.prm.deal_action("draft_note", {"deal_id": deal["id"], "text": "她要的是 A7269901001B-F"})["ok"]
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
    assert meta["op"] == "platform_preview" and meta["draft_context"] == {"orders": [], "accounts": []}
    assert meta["note"] == "她要的是 A7269901001B-F"


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


def sheet_draft(b, *, with_brief=True, verdict=None):
    """Her draft whose job sent back a brief (an attachment) and a sheet (the Mac's operator file); with ``verdict``, a
    draft of a sample order previewed so."""
    job = her_job(b, "就这顶")
    if with_brief:
        assert b.relay.outfile(job["id"], "brief.txt", io.BytesIO(b"brief v1").read, 8)
    answer = b.relay.outfile(job["id"], f"对图单-{job['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    extra = {}
    if verdict is not None:
        extra = {"message_kind": "money", "platform": {
            "version": 1, "action": SAMPLE_ACTION, "verdict": verdict, "problems": [] if verdict == "ready" else ["登录失效"],
            "facts": [], "steps": [], "run": {"steps": ["create"]} if verdict == "ready" else {}, "previewed_at": 1.0}}
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


def test_the_console_shows_the_sheet_and_serves_it_while_the_bot_runs_and_after(bot, tmp_path, monkeypatch):
    job, packet = sheet_draft(bot, with_brief=False)
    snap = bot.prm.snapshot()
    [shown] = [p for p in snap["pending"] if p["id"] == packet["id"]]
    assert shown["operator_files"] == [{"n": 0, "name": f"对图单-{job['id']}.jpg", "why": "并排对比，只给运营看",
                                        "url": f"/api/prm/file?packet={packet['id']}&n=0", "kept": True}]
    assert shown["attachments"] == []
    server.register_prm(bot.prm)
    try:
        status, kind, body = fetch(f"/api/prm/file?packet={packet['id']}&n=0")
        assert (status, kind, body) == (200, "image/jpeg", SHEET)
        for bad in (f"/api/prm/file?packet={packet['id']}&n=1", f"/api/prm/file?packet={packet['id']}&n=x",
                    "/api/prm/file?packet=999&n=0", "/api/prm/file"):
            assert fetch(bad)[0] == 404, bad
        assert fetch(f"/api/prm/file?packet={packet['id']}&n=0", origin="https://evil.example")[0] == 403
        assert fetch(f"/api/prm/file?packet={packet['id']}&n=0", method="POST")[0] == 405
    finally:
        server.register_prm(None)
    # the bot stopped: read from its database
    db = tmp_path / "prm.db"
    stored = PrmStore(db)
    stored.add_party(CREATOR, "小美", now=1)
    pid = stored.add_packet(deal_id=1, party_id=1, job_id=job["id"], chat_id=CREATOR, kind="faq", text="好的",
                            attachments=[], intent="", slots={}, stage_proposal="", needs_attention=False, reason="",
                            meta={"operator_files": packet["meta"]["operator_files"]}, now=1)
    stored.close()
    monkeypatch.setenv("PRM_DB", str(db))
    assert fetch(f"/api/prm/file?packet={pid}&n=0")[:2] == (200, "image/jpeg")


def fetch(path, method="GET", origin=None):
    headers = {"Origin": origin} if origin else {}
    _, sock = _build_handler(path, method=method, headers=headers)
    raw = b"".join(sock._sent_chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    kind = next((line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("content-type:")), "")
    return int(lines[0].split()[1]), kind, body


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


def test_a_new_previews_sheet_replaces_the_drafts(bot):
    job, packet = sheet_draft(bot, with_brief=False, verdict="login")
    assert packet["tool"] == "jst.sample_order"
    first = packet["meta"]["operator_files"][0]["path"]
    action = SAMPLE_ACTION
    out = bot.prm.decide(packet["id"], "repreview")
    assert out["ok"], out
    again = bot.relay.next_job(0, ("prm",))
    answer = bot.relay.outfile(again["id"], f"对图单-{again['id']}.jpg", io.BytesIO(SHEET + b"2").read, len(SHEET) + 1)
    block = {"version": 1, "action": action, "verdict": "ready", "problems": [], "facts": [], "steps": [],
             "run": {"steps": ["create"]}, "previewed_at": 2.0,
             "operator_files": [{"name": f"对图单-{again['id']}.jpg", "why": "新的对图单", "sha256": answer["sha256"]}]}
    bot.finish(again["id"], text=json.dumps(block, ensure_ascii=False))
    now = bot.prm.store.packet(packet["id"])
    [kept] = now["meta"]["operator_files"]
    assert kept["why"] == "新的对图单" and kept["path"] != first and Path(kept["path"]).read_bytes() == SHEET + b"2"
    assert now["platform"]["preview"]["verdict"] == "ready"


def test_the_sheets_go_after_thirty_days(bot, clock):
    job, packet = sheet_draft(bot, with_brief=False)
    path = Path(packet["meta"]["operator_files"][0]["path"])
    assert draft_context.sweep(bot.prm.store, clock.now + 29 * 86400) == 0 and path.exists()
    assert draft_context.sweep(bot.prm.store, clock.now + 31 * 86400) == 1 and not path.exists()
    kept = bot.prm.store.packet(packet["id"])["meta"]["operator_files"]
    assert kept[0]["path"] == "" and kept[0]["name"] == f"对图单-{job['id']}.jpg"
    assert bot.prm.store.operator_file_packets(clock.now + 31 * 86400) == []          # looked at once
    assert draft_context.operator_files_out(bot.prm.store.packet(packet["id"]))[0]["kept"] is False


def test_the_hourly_clean_up_runs_the_sweep(bot, clock):
    job, packet = sheet_draft(bot, with_brief=False)
    path = Path(packet["meta"]["operator_files"][0]["path"])
    clock.now += 31 * 86400
    bot.prm.purge_due()
    assert not path.exists()


def test_a_new_preview_that_cannot_be_used_keeps_the_drafts_sheet(bot):
    """The Mac's new preview is not a preview (an answer the bot cannot read): the old preview stays, marked, and so does
    the draft's sheet, whatever files that job sent."""
    job, packet = sheet_draft(bot, with_brief=False, verdict="login")
    first = packet["meta"]["operator_files"]
    out = bot.prm.decide(packet["id"], "repreview")
    assert out["ok"], out
    again = bot.relay.next_job(0, ("prm",))
    answer = bot.relay.outfile(again["id"], f"对图单-{again['id']}.jpg", io.BytesIO(SHEET).read, len(SHEET))
    bot.finish(again["id"], text=json.dumps({"operator_files": [{"name": f"对图单-{again['id']}.jpg", "why": "新的",
                                                                 "sha256": answer["sha256"]}]}))
    now = bot.prm.store.packet(packet["id"])
    assert now["meta"]["operator_files"] == first
    assert any("重新预览没做成" in p for p in now["platform"]["preview"]["problems"])
