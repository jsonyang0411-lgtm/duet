"""The console's PRM API: GET /api/prm, POST /api/prm/parties, /api/prm/packets, /api/prm/deals, /api/prm/reveal,
/api/prm/auto."""

import json
import time
from types import SimpleNamespace

import pytest

from test_web_api import _build_handler

from src.prm.service import SETTLE_SEC, PrmService
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.web import server

CREATOR = "wxid_creator01"


def call(path, method="GET", body=None):
    raw = b"" if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    _, sock = _build_handler(path, method=method, body=raw, headers=headers)
    head, text = sock.get_response_text().split("\r\n\r\n", 1)
    return int(head.split()[1]), json.loads(text)


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def running(tmp_path):
    """A running bot's PRM service with one creator and one draft waiting."""
    clock = Clock()
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                          prm_copilot="on", prm_db=":memory:")
    sent = []
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock,
                     send=lambda c, t: sent.append((c, t)) or True, send_file=lambda c, p: True)
    prm.register(CREATOR, "小美", "测试达人")
    prm.handle({"chat_id": CREATOR, "sender_id": CREATOR, "content": "收件人小美 13812345678", "msg_type": 1,
                "is_group": False, "timestamp": clock.now})
    clock.now += SETTLE_SEC + 1
    job = relay.next_job(0, ("prm",))
    draft = {"intent": "要样品", "slots": {"收件人": "小美", "手机": "〈手机1〉"}, "stage_proposal": "address",
             "message_kind": "address_confirm", "reply_draft": "确认：小美 〈手机1〉", "attachments_to_send": [],
             "needs_attention": True, "reason": "第一次合作"}
    relay.finish(job["id"], {"ok": True, "text": json.dumps(draft, ensure_ascii=False)})
    relay.tick()
    server.register_prm(prm)
    yield SimpleNamespace(prm=prm, relay=relay, sent=sent)
    server.register_prm(None)
    relay.stop()


def test_a_stopped_bot_without_a_database_shows_an_empty_page(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.setenv("PRM_COPILOT", "on")
    status, d = call("/api/prm")
    assert status == 200 and d["ok"] and d["running"] is False and d["enabled"] is True
    assert d["parties"] == [] and d["pending"] == [] and "talk" in d["stages"] and "faq" in d["kinds"]
    assert not (tmp_path / "data" / "prm.db").exists()                     # nothing is created by a look


def test_creators_can_be_registered_while_the_bot_is_stopped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.setenv("ADMIN_WXID", "wxid_admin")
    monkeypatch.setenv("CLAUDE_RELAY_WXIDS", "wxid_helper, wxid_other")
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "小美"})[1]["ok"] is True
    for wxid in ("wxid_admin", "wxid_other"):
        answer = call("/api/prm/parties", "POST", {"action": "add", "wxid": wxid, "name": "管理员"})[1]
        assert answer["ok"] is False and "管理员" in answer["error"]
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "又一次"})[1]["ok"] is False
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_noname", "name": ""})[1]["ok"] is False
    d = call("/api/prm")[1]
    assert [(p["wxid"], p["name"], p["deal"]["stage"]) for p in d["parties"]] == [(CREATOR, "小美", "talk")]
    assert call("/api/prm/parties", "POST", {"action": "disable", "id": 1})[1] == {"ok": True}
    assert call("/api/prm")[1]["parties"][0]["active"] is False
    assert call("/api/prm/parties", "POST", {"action": "disable", "id": "x"})[1]["ok"] is False
    assert call("/api/prm/packets", "POST", {"id": 1, "action": "send"})[1]["ok"] is False   # no bot, no sending


def test_the_running_bot_shows_the_draft_with_numbers_masked(running):
    d = call("/api/prm")[1]
    assert d["running"] is True and d["enabled"] is True and d["can_send"] is True
    [packet] = d["pending"]
    assert packet["party_name"] == "小美" and packet["text"] == "确认：小美 〈手机1〉"
    assert packet["legend"] == [{"token": "〈手机1〉", "masked": "138****5678"}]
    assert packet["kind_label"] == "收件信息复述" and packet["stage_proposal_label"] == "收集收件信息"
    assert packet["needs_attention"] is True and packet["reason"] == "第一次合作"
    assert "13812345678" not in json.dumps(d, ensure_ascii=False)           # never the number itself
    [party] = d["parties"]
    assert party["deal"]["attention"] == "第一次合作" and party["deal"]["slots"]["收件人"] == "小美"


def test_send_edit_and_drop_from_the_console(running):
    assert call("/api/prm/packets", "POST", {"id": 1, "action": "edit", "text": "好的〈手机1〉"})[1] == {
        "ok": True, "status": "approved"}
    assert call("/api/prm/packets", "POST", {"id": 1, "action": "drop"})[1]["ok"] is False
    running.prm.send_approved()
    assert running.sent == [(CREATOR, "好的13812345678")]
    recent = call("/api/prm")[1]["recent"]
    assert recent[0]["status"] == "sent" and recent[0]["final_text"] == "好的〈手机1〉" and recent[0]["edited"] is True
    assert call("/api/prm/packets", "POST", {"id": "x", "action": "send"})[1]["ok"] is False
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_admin", "name": "管理员"})[1]["ok"] is False
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_new01", "name": "阿花"})[1]["ok"] is True
    assert running.prm.is_party_chat("wxid_new01")


def test_deals_numbers_and_wrong_sends_from_the_running_console(running):
    d = call("/api/prm")[1]
    [party] = d["parties"]
    deal = party["deal"]
    assert party["has_numbers"] is True and deal["mode"] == "copilot" and deal["mode_label"] == "副驾驶"
    assert deal["legend"] == [{"token": "〈手机1〉", "masked": "138****5678"}]
    gates = {g["kind"]: g for g in d["gates"]}
    assert gates["money"]["locked"] and gates["address_confirm"]["min_unchanged"] == 29 and d["takeover_min"] == 120
    # the full number on request, and the look is written down (which placeholders, not the number)
    answer = call("/api/prm/reveal", "POST", {"party_id": party["id"]})[1]
    assert answer["numbers"] == [{"token": "〈手机1〉", "value": "13812345678", "cleared": False}]
    d = call("/api/prm")[1]
    [event] = [e for e in d["parties"][0]["deal"]["events"] if e["kind"] == "reveal"]
    assert event["name"] == "查看完整号码" and event["detail"] == {"tokens": ["〈手机1〉"]}
    assert "13812345678" not in json.dumps(d, ensure_ascii=False)
    assert call("/api/prm/reveal", "POST", {"party_id": "x"})[1]["ok"] is False
    # stage and mode
    assert call("/api/prm/deals", "POST", {"action": "adopt", "deal_id": deal["id"]})[1] == {"ok": True}
    assert call("/api/prm/deals", "POST", {"action": "mode", "deal_id": deal["id"], "mode": "human"})[1] == {"ok": True}
    deal = call("/api/prm")[1]["parties"][0]["deal"]
    assert deal["stage"] == "address" and deal["mode"] == "human" and deal["mode_until"]
    assert call("/api/prm")[1]["pending"][0]["status"] == "pending"          # a console switch drops nothing
    assert call("/api/prm/deals", "POST", {"action": "mode", "deal_id": deal["id"], "mode": "copilot"})[1] == {"ok": True}
    assert call("/api/prm/deals", "POST", {"action": "fly", "deal_id": deal["id"]})[1]["ok"] is False
    assert call("/api/prm/deals", "POST", {"action": "stage", "deal_id": deal["id"], "stage": "won"})[1]["ok"] is False
    # a waybill: the notice waits for review with the system's number
    answer = call("/api/prm/deals", "POST", {"action": "shipment", "deal_id": deal["id"], "carrier": "顺丰",
                                             "waybill": "SF1234567890123"})[1]
    d = call("/api/prm")[1]
    [notice] = [p for p in d["pending"] if p["id"] == answer["packet"]]
    assert notice["system"] is True and notice["waybill"] == "SF1234567890123" and notice["kind"] == "shipping_notice"
    assert d["parties"][0]["deal"]["shipment"]["waybill"] == "SF1234567890123"
    assert d["parties"][0]["deal"]["stage"] == "shipped"
    assert call("/api/prm/deals", "POST", {"action": "shipment_status", "deal_id": deal["id"], "status": "transit"})[1] == {"ok": True}
    assert call("/api/prm")[1]["parties"][0]["deal"]["shipment_status_label"] == "运输中"
    edited = call("/api/prm/packets", "POST", {"id": notice["id"], "action": "edit", "text": "单号 SF0000000000"})[1]
    assert edited["ok"] is False and "SF1234567890123" in edited["error"]
    # sent, then marked wrong: shipping notices are reviewed again from zero
    assert call("/api/prm/packets", "POST", {"id": notice["id"], "action": "send"})[1]["ok"]
    running.prm.send_approved()
    assert running.sent[-1] == (CREATOR, "老师您好～样品已经寄出啦：顺丰 SF1234567890123。签收之后跟我说一声哦～")
    assert call("/api/prm/packets", "POST", {"id": notice["id"], "action": "wrong"})[1] == {"ok": True, "status": "wrong"}
    d = call("/api/prm")[1]
    assert [p["wrong"] for p in d["recent"] if p["id"] == notice["id"]] == [True]
    assert {g["kind"]: g for g in d["gates"]}["shipping_notice"]["decided"] == 0
    assert call("/api/prm/packets", "POST", {"id": notice["id"], "action": "wrong"})[1]["ok"] is False


def test_deals_and_numbers_work_while_the_bot_is_stopped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    assert call("/api/prm/deals", "POST", {"action": "stage", "deal_id": 1, "stage": "agreed"})[1]["ok"] is False
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "小美"})[1]["ok"] is True
    assert call("/api/prm/deals", "POST", {"action": "stage", "deal_id": 1, "stage": "agreed"})[1] == {"ok": True}
    answer = call("/api/prm/deals", "POST", {"action": "shipment", "deal_id": 1, "carrier": "顺丰",
                                             "waybill": "SF1234567890123"})[1]
    assert answer["ok"] is True
    d = call("/api/prm")[1]
    assert d["running"] is False and [p["id"] for p in d["pending"]] == [answer["packet"]]
    assert d["parties"][0]["deal"]["stage"] == "shipped" and d["gates"]
    assert call("/api/prm/reveal", "POST", {"party_id": 1})[1] == {"ok": True, "numbers": []}
    kinds = [e["kind"] for e in call("/api/prm")[1]["parties"][0]["deal"]["events"]]
    assert kinds[0] == "reveal" and {"stage", "shipment"} <= set(kinds)
    assert call("/api/prm/packets", "POST", {"id": answer["packet"], "action": "wrong"})[1]["ok"] is False


def test_an_opening_is_asked_for_from_the_running_console_and_refused_while_stopped(running, tmp_path, monkeypatch):
    """POST /api/prm/deals {action: opening}: 「写开场白」 for a creator who has not written (a job with ``meta.opening``,
    its draft tagged ``opening`` in /api/prm); 小美, who wrote (``unanswered``: never had a real line from us) but has a
    draft waiting, is told why not; a stopped bot cannot ask the Mac."""
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_quiet01", "name": "小青"})[1]["ok"] is True
    deal_id = next(p["deal"]["id"] for p in call("/api/prm")[1]["parties"] if p["name"] == "小青")
    answer = call("/api/prm/deals", "POST", {"action": "opening", "deal_id": deal_id})[1]
    assert answer["ok"] is True
    running.relay._clock.now += SETTLE_SEC + 1
    job = running.relay.next_job(0, ("prm",))
    assert job["id"] == answer["job"] and job["meta"]["opening"] == {"how": "", "by": "console"}
    draft = {"intent": "开场白", "slots": {}, "stage_proposal": "", "message_kind": "greeting",
             "reply_draft": "小青你好呀～", "attachments_to_send": [], "needs_attention": False, "reason": "场景 A1"}
    running.relay.finish(job["id"], {"ok": True, "text": json.dumps(draft, ensure_ascii=False)})
    running.relay.tick()
    d = call("/api/prm")[1]
    assert [(p["party_name"], p["opening"]) for p in d["pending"]] == [("小美", False), ("小青", True)]
    events = next(p["deal"]["events"] for p in d["parties"] if p["name"] == "小青")
    assert any(e["name"] == f"派写开场白的任务 #{job['id']}（运营点的）" for e in events)
    # 小美 wrote and never had a real line from us, but her draft waits in 待审批: not offered (2026-11-02)
    deals = {p["name"]: p["deal"] for p in d["parties"]}
    assert deals["小美"]["unanswered"] is False and deals["小青"]["unanswered"] is False
    she_wrote = call("/api/prm/deals", "POST", {"action": "opening", "deal_id": 1})[1]
    assert she_wrote == {"ok": False, "error": "「待审批」里已经有她的草稿"}
    server.register_prm(None)                                       # the bot stopped
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "小美"})[1]["ok"] is True
    stopped = call("/api/prm/deals", "POST", {"action": "opening", "deal_id": 1})[1]
    assert stopped == {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能写开场白"}


def test_a_huopan_is_asked_for_from_the_running_console_and_refused_while_stopped(running, tmp_path, monkeypatch):
    """POST /api/prm/deals {action: huopan} (「补发货盘」, 2026-11-04): with 小美's draft waiting she is told why not;
    once it is dropped the click makes a job with ``meta.huopan``, its draft is tagged ``huopan`` in /api/prm and the
    deal's record names the click; a stopped bot cannot ask the Mac."""
    deal_id = call("/api/prm")[1]["parties"][0]["deal"]["id"]
    assert call("/api/prm/deals", "POST", {"action": "huopan", "deal_id": deal_id})[1] == {
        "ok": False, "error": "「待审批」里已经有她的草稿：先处理那一份"}
    [waiting] = call("/api/prm")[1]["pending"]
    assert waiting["huopan"] is False
    assert call("/api/prm/packets", "POST", {"id": waiting["id"], "action": "drop"})[1]["ok"] is True
    answer = call("/api/prm/deals", "POST", {"action": "huopan", "deal_id": deal_id})[1]
    assert answer["ok"] is True
    running.relay._clock.now += SETTLE_SEC + 1
    job = running.relay.next_job(0, ("prm",))
    assert (job["id"], job["text"], job["meta"]["huopan"]) == (answer["job"], "", {"by": "console"})
    draft = {"intent": "补发货盘", "slots": {}, "stage_proposal": "", "message_kind": "material",
             "reply_draft": "老师您好～给您补一份我们当季的货盘", "attachments_to_send": [], "needs_attention": False,
             "reason": "kb 第 3.0 节"}
    running.relay.finish(job["id"], {"ok": True, "text": json.dumps(draft, ensure_ascii=False)})
    running.relay.tick()
    d = call("/api/prm")[1]
    assert [(p["party_name"], p["huopan"], p["status"]) for p in d["pending"]] == [("小美", True, "pending")]
    assert any(e["name"] == f"派补发货盘的任务 #{job['id']}（运营点的）" for e in d["parties"][0]["deal"]["events"])
    server.register_prm(None)                                       # the bot stopped
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "小美"})[1]["ok"] is True
    stopped = call("/api/prm/deals", "POST", {"action": "huopan", "deal_id": 1})[1]
    assert stopped == {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能补发货盘"}


def test_the_draft_note_is_kept_from_the_running_console_and_refused_while_stopped(running, tmp_path, monkeypatch):
    """POST /api/prm/deals {action: draft_note, text} (起草备注, 2026-10-31): the operators' words for the Mac, kept on the
    deal (whitespace collapsed) and shown in GET /api/prm's deal; her next draft job carries them (``meta.note``); too
    long is refused, empty clears it; another site's page cannot change it; a stopped bot cannot keep one (refused like
    写开场白)."""
    words = "直接发链接和brief，感兴趣的会回复！\n勿催视频，不接语音"
    kept = "直接发链接和brief，感兴趣的会回复！ 勿催视频，不接语音"
    deal = call("/api/prm")[1]["parties"][0]["deal"]
    assert deal["draft_note"] == ""
    answer = call("/api/prm/deals", "POST", {"action": "draft_note", "deal_id": deal["id"], "text": words})[1]
    assert answer == {"ok": True, "draft_note": kept}
    deal = call("/api/prm")[1]["parties"][0]["deal"]
    assert deal["draft_note"] == kept and deal["events"][0]["name"] == f"改起草备注（{len(kept)} 字）"
    running.prm.handle({"chat_id": CREATOR, "sender_id": CREATOR, "content": "你们的帽子怎么合作", "msg_type": 1,
                        "is_group": False, "timestamp": running.relay._clock()})
    running.relay._clock.now += SETTLE_SEC + 1
    assert running.relay.next_job(0, ("prm",))["meta"]["note"] == kept
    too_long = call("/api/prm/deals", "POST", {"action": "draft_note", "deal_id": deal["id"], "text": "长" * 301})[1]
    assert too_long == {"ok": False, "error": "起草备注最多 300 个字（现在 301 个）"}
    raw = json.dumps({"action": "draft_note", "deal_id": deal["id"], "text": "别的网站写的"}).encode()
    _, sock = _build_handler("/api/prm/deals", method="POST", body=raw,
                             headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert sock.get_response_text().split("\r\n", 1)[0].split()[1] == "403"
    assert call("/api/prm/deals")[0] == 405                                 # a GET changes nothing
    assert call("/api/prm")[1]["parties"][0]["deal"]["draft_note"] == kept
    cleared = call("/api/prm/deals", "POST", {"action": "draft_note", "deal_id": deal["id"], "text": ""})[1]
    assert cleared == {"ok": True, "draft_note": ""}
    assert call("/api/prm")[1]["parties"][0]["deal"]["events"][0]["name"] == "清掉起草备注"
    server.register_prm(None)                                       # the bot stopped
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "小美"})[1]["ok"] is True
    stopped = call("/api/prm/deals", "POST", {"action": "draft_note", "deal_id": 1, "text": "不接语音"})[1]
    assert stopped == {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能改起草备注"}
    assert call("/api/prm")[1]["parties"][0]["deal"]["draft_note"] == ""


def test_changes_need_a_post_and_a_body():
    for path in ("/api/prm/parties", "/api/prm/packets", "/api/prm/deals", "/api/prm/reveal", "/api/prm/auto"):
        assert call(path)[0] == 405                                         # a GET must not change anything
    _, sock = _build_handler("/api/prm/parties", method="POST", body=b"not json",
                             headers={"Content-Type": "application/json"})
    assert '"ok": false' in sock.get_response_text()


def test_the_prm_switch_is_in_the_allow_list_and_needs_the_relay():
    from src.web import advanced_config as adv
    assert adv.FIELDS["PRM_COPILOT"]["type"] == "onoff"
    assert set(adv.validate_updates({"PRM_COPILOT": "on"}, {})) == {"PRM_COPILOT"}
    assert adv.validate_updates({"PRM_COPILOT": "on", "CLAUDE_RELAY": "on", "CLAUDE_RELAY_TOKEN": "x" * 20,
                                 "ADMIN_WXID": "wxid_admin"}, {}) == {}


# ── platform actions (src/prm/platforms.py) ───────────────────────────────

def _platform_draft(running):
    """A second draft that carries a previewed platform action (what the Mac hands back)."""
    from src.prm import platforms

    action = platforms.check_action({"action": "dy.channel_goods", "item_id": "3000000000000000101",
                                     "creator_platform_id": "5100000000000002", "title_prefix": "【小美专属】"})
    preview = {"version": 1, "action": action, "verdict": "ready", "problems": [], "steps": [],
               "facts": [{"label": "将写入", "value": "create a 专属达人带货 channel product"}],
               "run": {"steps": ["create", "bind"], "skus": [{"id": "3100000000000000201", "price": "17.00", "stock": 40,
                                                              "main_price": "19.99"}]},
               "catalog": {"synced_at": 1.0, "title": "测试主品"}, "previewed_at": 2.0}
    running.prm.handle({"chat_id": CREATOR, "sender_id": CREATOR, "content": "好的，就要专属链接", "msg_type": 1,
                        "is_group": False, "timestamp": running.relay._clock()})
    running.relay._clock.now += SETTLE_SEC + 1
    job = running.relay.next_job(0, ("prm",))
    d = {"intent": "要专属链接", "slots": {}, "stage_proposal": "agreed", "message_kind": "money",
         "reply_draft": "链接给你啦：{链接}", "attachments_to_send": [], "needs_attention": False,
         "reason": "kb/合作政策.md", "platform": preview}
    running.relay.finish(job["id"], {"ok": True, "text": json.dumps(d, ensure_ascii=False)})
    running.relay.tick()
    return running.prm.store.packet_for_job(job["id"])


def test_a_platform_draft_is_approved_with_execute_and_shown_red_when_it_fails(running):
    packet = _platform_draft(running)
    d = call("/api/prm")[1]
    [shown] = [p for p in d["pending"] if p["id"] == packet["id"]]
    pf = shown["platform"]
    assert pf["label"].startswith("抖店") and pf["verdict"] == "ready" and pf["can_execute"] and pf["makes_link"]
    assert d["platform_alerts"] == [] and d["platform_actions"]["tb.campaign"].startswith("淘宝")
    assert call("/api/prm/packets", "POST", {"id": packet["id"], "action": "send"})[1]["ok"] is False
    answer = call("/api/prm/packets", "POST", {"id": packet["id"], "action": "execute",
                                               "text": "宝子，专属链接：{链接}"})[1]
    assert answer["ok"] is True and answer["status"] == "executing"
    assert call("/api/prm/packets", "POST", {"id": packet["id"], "action": "execute"})[1]["ok"] is False
    job = running.relay.next_job(0, ("prm",))
    assert job["meta"]["op"] == "platform_execute"
    rec = {"version": 1, "op": "platform_execute", "outcome": "INDETERMINATE", "key": job["meta"]["key"],
           "problems": ["发出了，结果未知"], "facts": [], "steps": [], "created": {}, "finished_at": 3.0}
    running.relay.finish(job["id"], {"ok": True, "text": json.dumps(rec, ensure_ascii=False)})
    running.relay.tick()
    d = call("/api/prm")[1]
    [alert] = d["platform_alerts"]
    assert alert["id"] == packet["id"] and "INDETERMINATE" in alert["error"]
    assert alert["platform"]["execution"]["outcome"] == "INDETERMINATE"
    assert call("/api/prm/packets", "POST", {"id": packet["id"], "action": "ack"})[1] == {"ok": True}
    assert call("/api/prm")[1]["platform_alerts"] == []
    running.prm.tick()
    assert running.sent == []


def test_a_platform_draft_can_be_previewed_again_from_the_console(running):
    packet = _platform_draft(running)
    answer = call("/api/prm/packets", "POST", {"id": packet["id"], "action": "repreview"})[1]
    assert answer["ok"] is True and answer["job"]
    assert call("/api/prm")[1]["pending"][-1]["platform"]["previewing"] is True


def test_a_stopped_bot_lists_no_platform_alerts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    d = call("/api/prm")[1]
    assert d["platform_alerts"] == [] and "dy.channel_goods" in d["platform_actions"]


# ── new friends in 登记达人 (src/wechat/new_friends.py) ───────────────────

def _friends(tmp_path, now):
    """The bot's database with three friends found lately: one of them is a creator already."""
    from src.wechat.new_friends import NewFriendStore

    store = NewFriendStore(str(tmp_path / "messages.db"))
    for wxid, name, how, found, verify in (("wxid_new01", "阿花", "auto_accept", now - 600, "我是阿花，电话 138****8000"),
                                           (CREATOR, "小美", "manual", now - 500, ""),
                                           ("meimei_88", "小美美", "requested", now - 60, "")):
        store.add(wxid=wxid, name=name, remark="", how=how, kind="pc", added_at=found - 5, verify_from=None,
                  verify_text=verify, found_at=found)
    return store


def test_new_friends_come_first_in_the_candidates_tagged_with_greeting_and_how(running, tmp_path, monkeypatch):
    from src.wechat.new_friends import NewFriendService

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_WXID", "wxid_admin")                       # never offered
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "private_chat_names.json").write_text(json.dumps(
        {"wxid_zhang": "张三", "wxid_new01": "阿花", CREATOR: "小美", "wxid_admin": "管理员"}, ensure_ascii=False),
        encoding="utf-8")
    now = running.relay._clock()
    store = _friends(tmp_path, now)
    server.register_new_friends(NewFriendService(SimpleNamespace(), store, clock=running.relay._clock))
    try:
        d = call("/api/prm")[1]
    finally:
        server.register_new_friends(None)
    assert [c["wxid"] for c in d["candidates"]] == ["meimei_88", "wxid_new01", "wxid_zhang"]   # new, newest first
    newest, aihua, zhang = d["candidates"]
    assert (newest["new"], newest["how"], newest["how_label"], newest["verify_text"]) == (
        True, "requested", "对方通过了我们的申请", "")
    assert (aihua["name"], aihua["verify_text"], aihua["how_label"]) == ("阿花", "我是阿花，电话 138****8000", "机器人自动通过的")
    assert zhang == {"wxid": "wxid_zhang", "name": "张三"} and d["catchup_hours"] == 12
    # the bot stopped: read from its database (DB_PATH)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "messages.db"))
    assert [c["wxid"] for c in call("/api/prm")[1]["candidates"]][:2] == ["meimei_88", "wxid_new01"]


def test_registering_a_new_friend_drafts_what_she_said_since_she_was_added(running, tmp_path):
    from src.wechat.new_friends import NewFriendService

    now = running.relay._clock()

    class Backend:                                   # the WCDB backend: her chat read back on its next poll
        def catch_up_chat(self, talker, name, since, sink):
            sink([{"message_id": "m1", "chat_id": talker, "sender_id": talker, "content": "我是阿花 13800138000",
                   "msg_type": 1, "is_group": False, "timestamp": now - 605},
                  {"message_id": "m2", "chat_id": talker, "sender_id": talker, "content": "想要个样品",
                   "msg_type": 1, "is_group": False, "timestamp": now - 300}])

    service = NewFriendService(Backend(), _friends(tmp_path, now), clock=running.relay._clock)
    service.set_catch_up_sink(running.prm.take_new_friend)
    running.prm.on_registered = service.registered
    answer = call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_new01", "name": "阿花"})[1]
    assert answer["ok"] is True and answer["catch_up"] is True
    plain = call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_zhang", "name": "张三"})[1]
    assert plain["ok"] is True and plain["catch_up"] is False          # not a new friend: nothing asked for
    running.relay._clock.now += SETTLE_SEC + 1
    job = running.relay.next_job(0, ("prm",))
    assert job["meta"]["name"] == "阿花" and job["meta"]["new_friend"] == {"from": now - 605, "count": 2}
    assert [line.split("] ", 1)[1] for line in job["text"].split("\n")] == [
        "（加好友时的验证消息）我是阿花 〈手机1〉", "想要个样品"]
    assert running.prm.is_party_chat("wxid_new01")


# ── 下班时段自动回复 (PRM_NIGHT_AUTO_HOURS) ──────────────────────────────────

def test_the_night_window_and_its_notice_from_the_running_console(tmp_path):
    """The window on and it is 21:30: /api/prm says where it stands; the holding line the bot sent for a draft the
    model flagged is a notice (night_alerts, the deal's attention) until 「已处理」 (POST /api/prm/deals night_done)."""
    clock = Clock()
    clock.now = time.mktime((2026, 10, 29, 21, 30, 0, 0, 0, -1))
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                          prm_copilot="on", prm_db=":memory:", prm_night_auto_hours="18:00-09:00")
    sent = []
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock,
                     send=lambda c, t: sent.append((c, t)) or True, send_file=lambda c, p: True)
    prm.register(CREATOR, "小美", "测试达人")
    prm.handle({"chat_id": CREATOR, "sender_id": CREATOR, "content": "好呀，可以寄来看看", "msg_type": 1,
                "is_group": False, "timestamp": clock.now})
    clock.now += SETTLE_SEC + 1
    job = relay.next_job(0, ("prm",))
    line = "好呀～您的意向我都记下啦！明早我第一时间拉个小群，一起跟您敲定选款和寄样细节哈！"
    draft = {"intent": "想要样品", "slots": {}, "stage_proposal": "agreed", "message_kind": "other", "reply_draft": line,
             "attachments_to_send": [], "needs_attention": True, "reason": "已进入寄样阶段，请运营拉三方群"}
    relay.finish(job["id"], {"ok": True, "text": json.dumps(draft, ensure_ascii=False)})
    relay.tick()
    assert prm.send_approved() == 1 and sent == [(CREATOR, line)]
    server.register_prm(prm)
    try:
        d = call("/api/prm")[1]
        assert d["night"] == {"on": True, "window": "18:00–09:00", "inside": True, "suspended": "", "error": "",
                              "auto": False}
        [alert] = d["night_alerts"]
        assert alert["deal_id"] == 1 and alert["party_name"] == "小美" and "已进入寄样阶段" in alert["text"]
        [packet] = d["recent"]
        assert packet["route"] == "night" and packet["needs_attention"] is True and packet["id"] == alert["packet"]
        assert d["parties"][0]["deal"]["attention"] == "夜间已自动回了过渡的话，要注意：已进入寄样阶段，请运营拉三方群"
        done = {"action": "night_done", "deal_id": 1, "packet": alert["packet"]}
        assert call("/api/prm/deals", "POST", done)[1] == {"ok": True}
        d = call("/api/prm")[1]
        assert d["night_alerts"] == [] and d["parties"][0]["deal"]["attention"] == ""
        assert d["parties"][0]["deal"]["events"][0]["name"] == f"夜间自动回的过渡话 #{alert['packet']} 已处理"
        assert call("/api/prm/deals", "POST", done)[1]["ok"] is False
        assert call("/api/prm/deals", "POST", {**done, "packet": "x"})[1]["ok"] is False
    finally:
        server.register_prm(None)
        relay.stop()


def test_a_stopped_bot_shows_the_night_window_as_env_says_and_sees_to_its_notices(tmp_path, monkeypatch):
    from src.prm import gates

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.delenv("PRM_NIGHT_AUTO_HOURS", raising=False)
    assert call("/api/prm")[1]["night"] == {"on": False, "window": "", "inside": False, "suspended": "", "error": "",
                                            "auto": False}
    monkeypatch.setenv("PRM_NIGHT_AUTO_HOURS", "18:00-09:00")
    night = call("/api/prm")[1]["night"]
    assert night["on"] is True and night["window"] == "18:00–09:00" and night["error"] == ""
    assert night["inside"] is gates.in_night((18 * 60, 9 * 60), time.time())
    monkeypatch.setenv("PRM_NIGHT_AUTO_HOURS", "18-9")
    night = call("/api/prm")[1]["night"]
    assert night["on"] is False and "PRM_NIGHT_AUTO_HOURS" in night["error"] and "18:00-09:00" in night["error"]
    # notices the running bot left: seen to while it is stopped (straight on the database)
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "小美"})[1]["ok"] is True
    store = PrmStore(tmp_path / "data" / "prm.db")
    for packet in (7, 9):
        store.set_notice(1, f"night:{packet}", f"夜间已自动回了过渡的话，要注意：第 {packet} 份拿不准", time.time(),
                         packet=packet)
    store.close()
    d = call("/api/prm")[1]
    assert [a["packet"] for a in d["night_alerts"]] == [7, 9] and "第 7 份拿不准" in d["parties"][0]["deal"]["attention"]
    assert call("/api/prm/deals", "POST", {"action": "night_done", "deal_id": 1, "packet": 7})[1] == {"ok": True}
    assert [a["packet"] for a in call("/api/prm")[1]["night_alerts"]] == [9]
    assert call("/api/prm/deals", "POST", {"action": "night_done", "deal_id": 1})[1] == {"ok": True}   # every one left
    d = call("/api/prm")[1]
    assert d["night_alerts"] == [] and d["parties"][0]["deal"]["attention"] == ""
    assert [e["kind"] for e in d["parties"][0]["deal"]["events"]][:2] == ["night_done", "night_done"]
    assert call("/api/prm/deals", "POST", {"action": "night_done", "deal_id": 5})[1]["ok"] is False


def test_the_night_settings_are_in_the_allow_list_and_checked():
    from src.prm.service import NIGHT_HOLDING_TEXT
    from src.web import advanced_config as adv

    keys = [f["key"] for f in next(g for g in adv.GROUPS if g["id"] == "prm")["fields"]]
    assert keys.index("PRM_NIGHT_AUTO_HOURS") == keys.index("PRM_HOLDING_TEXT") + 1
    assert keys.index("PRM_NIGHT_HOLDING_TEXT") == keys.index("PRM_NIGHT_AUTO_HOURS") + 1
    hours = adv.FIELDS["PRM_NIGHT_AUTO_HOURS"]
    assert hours["type"] == "text" and hours["default"] == "" and "重启机器人" in hours["help"]
    assert "worker" in hours["help"] and "25%" in hours["help"]
    assert adv.FIELDS["PRM_NIGHT_HOLDING_TEXT"]["default"] == NIGHT_HOLDING_TEXT
    assert adv.normalize("PRM_NIGHT_HOLDING_TEXT", "  ") == NIGHT_HOLDING_TEXT and adv.normalize("PRM_NIGHT_AUTO_HOURS", "") == ""
    assert adv.validate_updates({"PRM_NIGHT_AUTO_HOURS": "18:00-09:00"}, {}) == {}
    assert adv.validate_updates({"PRM_NIGHT_AUTO_HOURS": ""}, {}) == {}
    assert set(adv.validate_updates({"PRM_NIGHT_AUTO_HOURS": "18-9"}, {})) == {"PRM_NIGHT_AUTO_HOURS"}
    assert set(adv.validate_updates({"PRM_NIGHT_HOLDING_TEXT": "好" * 61}, {})) == {"PRM_NIGHT_HOLDING_TEXT"}


# ── 自动模式 (POST /api/prm/auto, 2026-10-30) ──────────────────────────────────

def test_auto_mode_is_switched_from_the_running_console_at_once(running):
    """The running bot: POST /api/prm/auto switches it at once (PrmService.set_auto_mode); /api/prm says so, and the
    night status says it is what puts the night rules in force (the fixture sets no window)."""
    clock = running.relay._clock
    d = call("/api/prm")[1]
    assert d["auto_mode"] == {"on": False, "since": None, "by": ""} and d["night"]["auto"] is False
    status, answer = call("/api/prm/auto", "POST", {"on": True})
    assert status == 200 and answer == {"ok": True, "auto_mode": {"on": True, "since": clock.now, "by": "console"}}
    assert running.prm.auto_mode["on"] is True
    d = call("/api/prm")[1]
    assert d["auto_mode"]["on"] is True
    assert d["night"] == {"on": False, "window": "", "inside": False, "suspended": "", "error": "", "auto": True}
    made = clock.now
    running.prm.handle({"chat_id": CREATOR, "sender_id": CREATOR, "content": "好的谢谢", "msg_type": 1,
                        "is_group": False, "timestamp": made})
    clock.now += SETTLE_SEC + 1
    job = running.relay.next_job(0, ("prm",))
    assert job["meta"]["night"] == {"auto": True, "at": time.strftime("%H:%M", time.localtime(made))}
    for bad in ({}, {"on": "yes"}, {"on": 1}, {"on": None}):
        answer = call("/api/prm/auto", "POST", bad)[1]
        assert answer["ok"] is False and "on 填 true 或 false" in answer["error"], bad
    assert running.prm.auto_mode["on"] is True                               # (a bad request changes nothing)
    assert call("/api/prm/auto", "POST", {"on": False})[1]["auto_mode"]["on"] is False
    d = call("/api/prm")[1]
    assert d["auto_mode"]["on"] is False and d["night"]["auto"] is False


def test_auto_mode_is_kept_for_the_next_start_while_the_bot_is_stopped(tmp_path, monkeypatch):
    """Stopped: straight into the database, as the stopped bot's deal actions and 登记达人 (made when there is none
    yet); /api/prm reads it back and the bot that starts on that database has it on."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.delenv("PRM_NIGHT_AUTO_HOURS", raising=False)
    d = call("/api/prm")[1]
    assert d["running"] is False and d["auto_mode"] == {"on": False, "since": None, "by": ""}
    db = tmp_path / "data" / "prm.db"
    assert not db.exists()
    answer = call("/api/prm/auto", "POST", {"on": True})[1]
    assert answer["ok"] is True and answer["auto_mode"]["on"] is True and answer["auto_mode"]["by"] == "console"
    assert db.exists()
    d = call("/api/prm")[1]
    assert d["auto_mode"]["on"] is True and (d["night"]["on"], d["night"]["auto"]) == (False, True)
    monkeypatch.setenv("PRM_NIGHT_AUTO_HOURS", "18:00-09:00")
    night = call("/api/prm")[1]["night"]
    assert night["on"] is True and night["auto"] is (not night["inside"])    # inside the window it is the night's
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                          prm_copilot="on", prm_db=str(db))
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), files_dir=tmp_path / "relay")
    store = PrmStore(db)
    try:
        assert PrmService(cfg, relay, store=store).auto_mode["on"] is True   # the next start
    finally:
        store.close()
        relay.stop()
    assert call("/api/prm/auto", "POST", {"on": "off"})[1]["ok"] is False
    assert call("/api/prm/auto", "POST", {"on": False})[1]["auto_mode"]["on"] is False
    assert call("/api/prm")[1]["auto_mode"]["on"] is False


# ── PRM_AUTO_REGISTER: new friends the bot registered ─────────────────────────

def test_a_friend_the_bot_registered_is_tagged_no_candidate_and_can_be_stopped_for_good(running, tmp_path, monkeypatch):
    from src.wechat.new_friends import NewFriendService

    monkeypatch.chdir(tmp_path)
    now = running.relay._clock()
    server.register_new_friends(NewFriendService(SimpleNamespace(), _friends(tmp_path, now), clock=running.relay._clock))
    try:
        assert call("/api/prm")[1]["auto_register"] is False                 # the fixture's switch: off
        party = running.prm.auto_register_friend({"wxid": "wxid_new01", "name": "阿花", "how": "auto_accept"})
        d = call("/api/prm")[1]
        shown = {p["wxid"]: p for p in d["parties"]}
        assert (shown["wxid_new01"]["auto"], shown[CREATOR]["auto"]) == (True, False)
        assert shown["wxid_new01"]["note"] == "自动登记（新好友，机器人自动通过）" and shown["wxid_new01"]["name"] == "阿花"
        assert shown["wxid_new01"]["deal"]["events"][0]["name"] == "自动登记（新好友，机器人自动通过）"
        assert [c["wxid"] for c in d["candidates"] if c.get("new")] == ["meimei_88"]   # registered: not offered
        # 停用 works for her, and sticks: the bot never starts her again
        assert call("/api/prm/parties", "POST", {"action": "disable", "id": party["id"]})[1] == {"ok": True}
        assert not running.prm.is_party_chat("wxid_new01")
        assert running.prm.auto_register_friend({"wxid": "wxid_new01", "name": "阿花", "how": "auto_accept"}) is None
        d = call("/api/prm")[1]
        stopped = next(p for p in d["parties"] if p["wxid"] == "wxid_new01")
        assert (stopped["active"], stopped["auto"]) == (False, True)
        again = next(c for c in d["candidates"] if c["wxid"] == "wxid_new01")
        assert (again["new"], again["inactive"]) == (True, True)            # offered again, marked stopped
        assert next(c for c in d["candidates"] if c["wxid"] == "meimei_88")["inactive"] is False
        # a person registering her again: a person's creator now
        answer = call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_new01", "name": "阿花",
                                                   "note": "运营确认是达人"})[1]
        assert answer["ok"] is True
        shown = next(p for p in call("/api/prm")[1]["parties"] if p["wxid"] == "wxid_new01")
        assert (shown["active"], shown["auto"], shown["note"]) == (True, False, "运营确认是达人")
    finally:
        server.register_new_friends(None)


def test_a_stopped_bot_says_whether_new_friends_are_registered_and_tags_them(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.delenv("PRM_AUTO_REGISTER", raising=False)
    assert call("/api/prm")[1]["auto_register"] is False
    monkeypatch.setenv("PRM_AUTO_REGISTER", "on")
    assert call("/api/prm")[1]["auto_register"] is True                       # as .env says (the next start)
    store = PrmStore(tmp_path / "data" / "prm.db")
    store.add_party("wxid_new01", "阿花", "自动登记（新好友，她加我们）", auto=True)
    store.add_party(CREATOR, "小美")
    store.close()
    d = call("/api/prm")[1]
    assert [(p["wxid"], p["auto"]) for p in d["parties"]] == [("wxid_new01", True), (CREATOR, False)]
    assert call("/api/prm/parties", "POST", {"action": "disable", "id": 1})[1] == {"ok": True}
    assert call("/api/prm")[1]["parties"][0]["active"] is False


def test_the_auto_register_switch_is_in_the_allow_list_and_checked():
    from src.web import advanced_config as adv

    keys = [f["key"] for f in next(g for g in adv.GROUPS if g["id"] == "prm")["fields"]]
    assert keys.index("PRM_AUTO_REGISTER") == keys.index("PRM_COPILOT") + 1
    field = adv.FIELDS["PRM_AUTO_REGISTER"]
    assert (field["type"], field["default"], field["platform"]) == ("onoff", "off", adv.WCDB_ONLY)
    assert field["requires"] == [{"key": "PRM_COPILOT", "is": "on", "text": "达人副驾驶打开时才用"}]
    for words in ("重启机器人", "停用", "管理员", "不是达人", "等人看过再发", "下班时段自动回复"):
        assert words in field["help"]
    assert adv.normalize("PRM_AUTO_REGISTER", "") == "off" and adv.normalize("PRM_AUTO_REGISTER", "yes") == "on"
    assert adv.validate_updates({"PRM_AUTO_REGISTER": "on"}, {}) == {}            # (with the copilot off: no effect)
    assert adv.validate_updates({"PRM_AUTO_REGISTER": "on", "PRM_COPILOT": "on", "CLAUDE_RELAY": "on",
                                 "CLAUDE_RELAY_TOKEN": "x" * 20, "ADMIN_WXID": "wxid_admin"}, {}) == {}
    assert set(adv.validate_updates({"PRM_AUTO_REGISTER": "always"}, {})) == {"PRM_AUTO_REGISTER"}


# ── 加百应达人微信 (功能 R): GET /api/prm's friend_adds / friend_add_status, POST /api/prm/friend-adds ──────────
# Every value is made up.

class _Manager:
    """FriendManager.add_friend as the console's add sees it (no WeChat window; a 4.0 application window whose 「备注」
    field OCR saw)."""

    def __init__(self):
        self.calls = []
        self.remarks = []

    def add_friend(self, keyword, message="", remark=""):
        self.calls.append((keyword, message))
        self.remarks.append(remark)
        return {"ok": True, "target": keyword, "message": f"已成功向「{keyword}」发送好友申请",
                **({"remark_set": True} if remark else {})}


@pytest.fixture
def adds(running):
    """The running bot with 加百应达人微信 on (PRM_FRIEND_ADD=on) and a fake friend manager behind the real admin
    handler; the add runs at once (no thread)."""
    from src.admin import AdminCommandHandler
    from src.prm.friend_adds import FriendAdds

    prm = running.prm
    manager = _Manager()
    prm.friend_adds = FriendAdds(prm.store, SimpleNamespace(prm_friend_add="on"), clock=running.relay._clock,
                                 register=prm.register, spawn=lambda fn: fn(), clear_clipboard=lambda text: False)
    prm.friend_adds.attach(AdminCommandHandler(friend_manager=manager), None, None)
    yield SimpleNamespace(prm=prm, manager=manager, clock=running.relay._clock)


def paste(body=None):
    return call("/api/prm/friend-adds", "POST", {
        "action": "paste", "aweme_id": "dy_demo01", "nickname": "小鹿",
        "contact": {"wechat": "abcdefyz", "phone": "13800000000"}, **(body or {})})[1]


def test_the_page_shows_the_records_masked_and_the_switchs_status(adds):
    assert paste() == {"ok": True, "id": 1, "state": "offered"}
    d = call("/api/prm")[1]
    [r] = d["friend_adds"]
    assert (r["state"], r["state_label"], r["nickname"], r["aweme_id"], r["source_label"]) == (
        "offered", "待确认", "小鹿", "dy_demo01", "运营手动填的")
    assert r["masked"] == {"wechat": "ab****yz", "phone": "138****0000"} and r["has_wechat"] and r["has_phone"]
    assert r["greeting"] == "您好，百应上联系您的～" and r["listed"] and r["open"] and r["last_attempt"] is None
    assert (r["remark"], r["remark_set"]) == ("抖音-小鹿", False)
    status = d["friend_add_status"]
    assert (status["enabled"], status["can_add"], status["today"], status["cap"], status["gap_sec"]) == (
        True, True, 0, 10, 120)
    text = json.dumps(d, ensure_ascii=False)
    assert "abcdefyz" not in text and "13800000000" not in text


def test_one_add_from_the_console_and_every_full_look_is_written_down(adds):
    paste()
    out = call("/api/prm/friend-adds", "POST", {"id": 1, "action": "add", "use": "wechat",
                                                 "greeting": "您好小鹿，我们在百应上聊过～"})[1]
    assert out["ok"] and out["state"] == "requested" and out["target"] == "ab****yz"
    assert (out["remark"], out["remark_set"]) == ("抖音-小鹿", True)
    assert "abcdefyz" not in json.dumps(out, ensure_ascii=False)
    assert adds.manager.calls == [("abcdefyz", "您好小鹿，我们在百应上聊过～")] and adds.manager.remarks == ["抖音-小鹿"]
    again = call("/api/prm/friend-adds", "POST", {"id": 1, "action": "add"})[1]
    assert again["ok"] is False and "已经发过申请" in again["error"] and len(adds.manager.calls) == 1
    d = call("/api/prm")[1]
    assert d["friend_add_status"]["today"] == 1 and d["friend_add_status"]["wait_sec"] > 0
    assert d["friend_adds"][0]["last_attempt"]["by"] == "console" and d["friend_adds"][0]["remark_set"] is True
    shown = call("/api/prm/friend-adds", "POST", {"id": 1, "action": "reveal"})[1]
    assert (shown["wechat"], shown["phone"]) == ("abcdefyz", "13800000000")
    assert [x["actor"] for x in adds.prm.store.friend_add(1)["reveals"]] == ["console"]
    assert call("/api/prm/friend-adds", "POST", {"id": 1, "action": "nonsense"})[1]["ok"] is False
    assert call("/api/prm/friend-adds", "POST", {"id": 99, "action": "drop"})[1] == {
        "ok": False, "error": "没有这一条加微信的记录"}
    assert call("/api/prm/friend-adds", "POST", {"action": "paste", "aweme_id": ""})[1]["ok"] is False


def test_linking_from_the_console_registers_her_and_her_deal_shows_the_douyin_id(adds):
    paste()
    call("/api/prm/friend-adds", "POST", {"id": 1, "action": "add"})
    out = call("/api/prm/friend-adds", "POST", {"id": 1, "action": "link", "wxid": "wxid_demo"})[1]
    assert out["ok"] and out["state"] == "linked" and out["party_id"]
    assert out["remark_hint"] == "抖音-小鹿"                    # a chat picked by hand: set her remark on the phone
    d = call("/api/prm")[1]
    party = next(p for p in d["parties"] if p["wxid"] == "wxid_demo")
    assert party["name"] == "小鹿" and party["note"] == "百应邀约 · 抖音号 dy_demo01"
    [link] = party["deal"]["links"]
    assert (link["account"], link["source"], link["add_id"]) == ("dy_demo01", "friend_add", 1)
    assert [e["name"] for e in party["deal"]["events"]] == ["关联抖音号 dy_demo01（加微信 #1）"]
    [r] = d["friend_adds"]
    assert r["state"] == "linked" and r["cleared"] and not r["has_wechat"]
    assert (r["listed"], r["remark_hint"], r["remark_set"]) == (True, "抖音-小鹿", False)     # the card stays for it
    assert call("/api/prm/friend-adds", "POST", {"id": 1, "action": "remark_done"})[1]["ok"] is True
    [r] = call("/api/prm")[1]["friend_adds"]
    assert (r["listed"], r["remark_hint"], r["remark_set"]) == (False, "", True)
    again = call("/api/prm/friend-adds", "POST", {"id": 1, "action": "remark_done"})[1]
    assert again["ok"] is False and "没有要在手机上设的备注" in again["error"]
    bad = call("/api/prm/friend-adds", "POST", {"id": 1, "action": "link", "wxid": "wxid_other"})[1]
    assert bad["ok"] is False and "不能关联" in bad["error"]
    paste({"aweme_id": "dy_demo02"})
    chatroom = call("/api/prm/friend-adds", "POST", {"id": 2, "action": "link", "wxid": "room1@chatroom"})[1]
    assert chatroom["ok"] is False and "群聊" in chatroom["error"]                # PartyError, as a clean answer


def test_registering_with_a_douyin_id_and_unlinking_it(running):
    out = call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_new01", "name": "阿花",
                                            "douyin_id": "dy_demo02"})[1]
    party_id = out["party"]["id"]
    links = running.prm.store.creator_links(party_id)
    assert [(l["account"], l["source"]) for l in links] == [("dy_demo02", "register")]
    bad = call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_new02", "name": "阿青", "douyin_id": "有中文"})[1]
    assert bad["ok"] is False and "抖音号" in bad["error"] and not running.prm.is_party_chat("wxid_new02")
    assert call("/api/prm/parties", "POST", {"action": "unlink", "id": party_id, "account": "dy_demo02"})[1] == {"ok": True}
    assert running.prm.store.creator_links(party_id) == []
    again = call("/api/prm/parties", "POST", {"action": "unlink", "id": party_id, "account": "dy_demo02"})[1]
    assert again["ok"] is False
    events = [e["name"] for e in call("/api/prm")[1]["parties"][1]["deal"]["events"]]
    assert events[:2] == ["取消关联抖音号 dy_demo02", "关联抖音号 dy_demo02"]


def test_the_friend_add_endpoint_is_post_only_and_refuses_other_sites(adds):
    status, answer = call("/api/prm/friend-adds")
    assert status == 405 and answer["ok"] is False
    raw = json.dumps({"action": "paste", "aweme_id": "dy_demo01", "contact": {"wechat": "abcdefyz"}}).encode()
    _, sock = _build_handler("/api/prm/friend-adds", method="POST", body=raw,
                             headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert sock.get_response_text().split("\r\n", 1)[0].split()[1] == "403"
    assert adds.prm.store.friend_adds() == []


def test_a_stopped_bot_shows_the_records_but_adds_and_reveals_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.setenv("PRM_FRIEND_ADD", "on")
    d = call("/api/prm")[1]
    assert d["friend_adds"] == [] and d["friend_add_status"]["enabled"] is True
    assert d["friend_add_status"]["can_add"] is False and not (tmp_path / "data" / "prm.db").exists()
    store = PrmStore(tmp_path / "data" / "prm.db")
    record, _ = store.add_friend_add(state="offered", source="paste", aweme_id="dy_demo01", wechat="abcdefyz",
                                     masked={"wechat": "ab****yz"}, now=time.time())
    store.add_friend_attempt(record["id"], by="console", kind="wechat", now=time.time())
    store.close()
    d = call("/api/prm")[1]
    assert [r["masked"] for r in d["friend_adds"]] == [{"wechat": "ab****yz"}]
    assert "abcdefyz" not in json.dumps(d, ensure_ascii=False) and d["friend_add_status"]["today"] == 1
    for body in ({"action": "paste", "aweme_id": "dy_demo02"}, {"id": 1, "action": "add"}, {"id": 1, "action": "reveal"}):
        answer = call("/api/prm/friend-adds", "POST", body)[1]
        assert answer["ok"] is False and "机器人没有运行" in answer["error"], body
    out = call("/api/prm/parties", "POST", {"action": "add", "wxid": "wxid_new01", "name": "阿花", "douyin_id": "dy_demo03"})[1]
    assert out["ok"]
    store = PrmStore(tmp_path / "data" / "prm.db")
    assert [l["account"] for l in store.creator_links(out["party"]["id"])] == ["dy_demo03"]
    store.close()


def test_a_new_friend_a_record_points_at_is_marked_in_the_candidates_and_not_her_clears_it(adds, tmp_path):
    """功能 R step 5: 登记达人's new friends show 「可能是百应的小鹿（#1）」 (and 选这个 brings her 抖音号); 不是她 clears it."""
    from src.wechat.new_friends import NewFriendService

    paste()
    call("/api/prm/friend-adds", "POST", {"id": 1, "action": "add"})
    service = NewFriendService(SimpleNamespace(), _friends(tmp_path, adds.clock.now), clock=adds.clock)
    server.register_new_friends(service)
    try:
        adds.prm.friend_adds.on_new_friend({"wxid": "meimei_88", "name": "小美美", "how": "requested",
                                             "own_greeting": "您好，百应上联系您的～", "added_at": adds.clock.now})
        d = call("/api/prm")[1]
        [r] = d["friend_adds"]
        assert r["candidate"]["wxid"] == "meimei_88" and r["candidate"]["level_label"] == "确定是她"
        marked = next(c for c in d["candidates"] if c["wxid"] == "meimei_88")
        assert marked["friend_add"] == {"id": 1, "nickname": "小鹿", "aweme_id": "dy_demo01", "level": "sure",
                                        "level_label": "确定是她"}
        assert "friend_add" not in next(c for c in d["candidates"] if c["wxid"] == "wxid_new01")
        assert call("/api/prm/friend-adds", "POST", {"id": 1, "action": "not_her"})[1]["ok"] is True
        d = call("/api/prm")[1]
        assert d["friend_adds"][0]["candidate"] == {}
        assert "friend_add" not in next(c for c in d["candidates"] if c["wxid"] == "meimei_88")
    finally:
        server.register_new_friends(None)


def test_three_party_is_shown_and_made_from_the_running_console_and_refused_while_stopped(tmp_path, monkeypatch):
    """三方群 (src/prm/three_party.py, 2026-10-30): GET /api/prm's ``three_party`` (who goes in besides her, the suggested
    name with the brand filled in) and each deal's; POST /api/prm/deals group / group_try / group_made / group_none
    while running; a stopped bot shows PRM_GROUP_WITH from .env and says it cannot make one."""
    from test_three_party import BOSS, HER, NEW_GROUP, Admin, Auto, Backend

    clock = Clock()
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                          prm_copilot="on", prm_db=":memory:", prm_group_with=BOSS, prm_friend_add_brand="品牌测试")
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock, send=lambda c, t: True)
    party = prm.register(CREATOR, "小美", "测试达人")
    backend = Backend()
    admin = Admin(backend)
    prm.three_party.attach(admin, Auto(), backend)
    prm.three_party.spawn = lambda fn: fn()
    prm.three_party._sleep = lambda s: None
    server.register_prm(prm)
    try:
        def deal_group():
            d = call("/api/prm")[1]
            return d["three_party"], next(p["deal"]["three_party"] for p in d["parties"] if p["id"] == party["id"])

        deal_id = prm.store.deal_of(party["id"])["id"]
        assert deal_group() == ({"enabled": True, "with": [BOSS], "why": "", "busy": None,
                                 "name_template": "品牌测试×{昵称}"}, {})
        assert call("/api/prm/deals", "POST", {"action": "group_try", "deal_id": deal_id})[1] == {"ok": True,
                                                                                                    "state": "trying"}
        assert deal_group()[1]["tried"]["ok"] is True and admin.calls == [([HER, BOSS], True)]
        assert call("/api/prm/deals", "POST", {"action": "group", "deal_id": deal_id})[1]["ok"] is True
        shown = deal_group()[1]
        assert (shown["state"], shown["label"], shown["made"], shown["chat_name"]) == ("created", "已建好", True,
                                                                                       f"{BOSS}、{HER}")
        assert prm.store.deal(deal_id)["three_party"]["chat"] == NEW_GROUP
        assert call("/api/prm/deals", "POST", {"action": "group_none", "deal_id": deal_id})[1]["ok"] is True
        assert deal_group()[1] == {"tried": shown["tried"]}
        assert call("/api/prm/deals", "POST", {"action": "group_made", "deal_id": deal_id})[1]["ok"] is True
        assert (deal_group()[1]["state"], deal_group()[1]["made"]) == ("created", False)
    finally:
        server.register_prm(None)
        relay.stop()
    monkeypatch.chdir(tmp_path)                                    # the bot stopped
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.setenv("PRM_COPILOT", "on")
    monkeypatch.setenv("PRM_GROUP_WITH", f"{BOSS}，王总测试")
    monkeypatch.setenv("PRM_FRIEND_ADD_BRAND", "品牌测试")
    assert call("/api/prm/parties", "POST", {"action": "add", "wxid": CREATOR, "name": "小美"})[1]["ok"] is True
    d = call("/api/prm")[1]
    assert d["three_party"] == {"enabled": True, "with": [BOSS, "王总测试"], "why": "机器人没有运行", "busy": None,
                                "name_template": "品牌测试×{昵称}"}
    for action in ("group", "group_try", "group_made", "group_none"):
        assert call("/api/prm/deals", "POST", {"action": action, "deal_id": 1})[1] == {
            "ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能拉群"}


def test_the_running_console_says_how_many_messages_a_reply_may_be(tmp_path):
    """PRM_REPLY_PARTS (src/prm/service.py reply_parts, 2026-10-30): GET /api/prm's ``reply_parts`` (the 待审批 page says
    「会分 N 条发」 of a draft with blank lines); 1 for a config without it (one message, as before)."""
    for parts, shown in ((3, 3), (None, 1)):
        cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                              claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                              claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                              prm_copilot="on", prm_db=":memory:", **({} if parts is None else {"prm_reply_parts": parts}))
        relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=Clock(), files_dir=tmp_path / f"relay{shown}")
        prm = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=Clock(), send=lambda c, t: True)
        server.register_prm(prm)
        try:
            assert call("/api/prm")[1]["reply_parts"] == shown
        finally:
            server.register_prm(None)
            relay.stop()


def test_boss_groups_from_the_running_console_and_while_stopped(tmp_path, monkeypatch):
    """老板拉的群 (src/prm/boss_groups.py, 2026-10-31): GET /api/prm's ``boss_groups`` (a pending group lists only the members
    she may be, never us, the boss or the admin, with its ``size``; the waiting and linked counts) and her deal's
    ``source``; POST /api/prm/boss-groups/pick and /ignore (POST only, the local page only); 「群已经不在了」 on the deal
    of a group the bot took over stops reading it.  A stopped bot shows .env's switch and the database's groups and says
    it cannot do either."""
    from test_prm_boss_groups import BOSS, BOSS_ID, GROUP, GROUP_NAME, ME, MEMBER, OTHER, Backend

    clock = Clock()
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                          prm_copilot="on", prm_db=":memory:", prm_boss_groups="on", prm_group_with=BOSS)
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock, send=lambda c, t: True)
    backend = Backend()
    prm.boss_groups.attach(backend)
    server.register_prm(prm)
    try:
        assert prm.boss_groups.scan() == 0
        backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR, MEMBER, "wxid_admin"]}
        backend.groups[OTHER] = {"name": "另一个群", "members": [ME, BOSS_ID, "wxid_x0001", "wxid_x0002"]}
        clock.now += 61
        assert prm.boss_groups.scan() == 0
        shown = call("/api/prm")[1]["boss_groups"]
        assert (shown["enabled"], shown["why_not"], shown["warning"], shown["boss"], shown["boss_call"],
                shown["waiting"], shown["linked"]) == (True, "", "", [BOSS], "老板", 0, 0)
        group = next(g for g in shown["pending"] if g["chat"] == GROUP)
        assert group["members"] == [{"wxid": CREATOR, "name": "小美"}, {"wxid": MEMBER, "name": "测试达人"}]
        assert (group["name"], group["size"]) == (GROUP_NAME, 5)
        for path in ("/api/prm/boss-groups/pick", "/api/prm/boss-groups/ignore"):
            assert call(path)[0] == 405                                  # a GET changes nothing
            raw = json.dumps({"chat": OTHER, "wxid": "wxid_x0001"}).encode()
            _, sock = _build_handler(path, method="POST", body=raw,
                                     headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
            assert sock.get_response_text().split("\r\n", 1)[0].split()[1] == "403"
        assert prm.store.boss_group(OTHER)["state"] == "pending"
        for wxid in (BOSS_ID, ME, "wxid_admin"):
            assert call("/api/prm/boss-groups/pick", "POST", {"chat": GROUP, "wxid": wxid})[1]["ok"] is False
        out = call("/api/prm/boss-groups/pick", "POST", {"chat": GROUP, "wxid": CREATOR})[1]
        party = prm.store.party_by_wxid(CREATOR)
        assert out == {"ok": True, "party_id": party["id"], "deal_id": prm.store.deal_of(party["id"])["id"]}
        assert call("/api/prm/boss-groups/ignore", "POST", {"chat": OTHER})[1] == {"ok": True}
        assert call("/api/prm/boss-groups/ignore", "POST", {"chat": OTHER})[1]["ok"] is False
        d = call("/api/prm")[1]
        assert d["boss_groups"]["pending"] == [] and d["boss_groups"]["linked"] == 1
        her = next(p for p in d["parties"] if p["wxid"] == CREATOR)
        assert her["source"] == her["deal"]["source"] == "boss_group"
        assert (her["deal"]["three_party"]["by"], her["deal"]["three_party"]["chat_name"]) == ("boss", GROUP_NAME)
        assert call("/api/prm/deals", "POST", {"action": "group_none", "deal_id": out["deal_id"]})[1]["ok"] is True
        assert prm.store.boss_group(GROUP)["state"] == "gone" and backend.unwatched == [GROUP]
        assert not prm.is_party_group(GROUP) and call("/api/prm")[1]["boss_groups"]["linked"] == 0
    finally:
        server.register_prm(None)
        relay.stop()
    monkeypatch.chdir(tmp_path)                                    # the bot stopped
    monkeypatch.delenv("PRM_DB", raising=False)
    monkeypatch.setenv("PRM_COPILOT", "on")
    monkeypatch.setenv("PRM_BOSS_GROUPS", "on")
    monkeypatch.setenv("PRM_GROUP_WITH", BOSS)
    monkeypatch.setenv("ADMIN_WXID", "wxid_admin")
    monkeypatch.setenv("PRM_BOSS_CALL", " 大老板 ")
    assert call("/api/prm")[1]["boss_groups"] == {"enabled": True, "why_not": "机器人没有运行", "warning": "",
                                                   "boss": [BOSS], "boss_call": "大老板", "pending": [], "waiting": 0,
                                                   "linked": 0}
    store = PrmStore(tmp_path / "data" / "prm.db")
    store.add_boss_group(GROUP, name=GROUP_NAME, found_at=time.time(), state="pending", boss=[{"wxid": BOSS_ID, "name": BOSS}],
                         members=[{"wxid": BOSS_ID, "name": BOSS}, {"wxid": CREATOR, "name": "小美"},
                                  {"wxid": MEMBER, "name": "测试达人"}, {"wxid": "wxid_admin", "name": "管理员"}])
    store.close()
    [pending] = call("/api/prm")[1]["boss_groups"]["pending"]
    assert pending["members"] == [{"wxid": CREATOR, "name": "小美"}, {"wxid": MEMBER, "name": "测试达人"}]
    for path, body in (("/api/prm/boss-groups/pick", {"chat": GROUP, "wxid": CREATOR}),
                       ("/api/prm/boss-groups/ignore", {"chat": GROUP})):
        assert call(path, "POST", body)[1] == {"ok": False,
                                               "error": "机器人没有运行（或没打开达人副驾驶），现在不能处理老板拉的群"}
    monkeypatch.setenv("PRM_BOSS_GROUPS", "off")
    monkeypatch.delenv("PRM_BOSS_CALL")
    assert call("/api/prm")[1]["boss_groups"] == {"enabled": False, "why_not": "", "warning": "", "boss": [BOSS],
                                                   "boss_call": "老板", "pending": [], "waiting": 0, "linked": 0}


def test_the_console_warns_of_an_unknown_boss_name_and_still_lets_a_person_pick(tmp_path):
    """老板拉的群, second review 2026-10-31 F3: a name of PRM_GROUP_WITH no one contact has is ``boss_groups.warning`` in
    GET /api/prm (``why_not`` stays "": the card's buttons are on), the groups found wait in ``pending``, and the POST
    pick takes one over."""
    from test_prm_boss_groups import BOSS, BOSS_ID, GROUP, GROUP_NAME, ME, Backend

    clock = Clock()
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                          prm_copilot="on", prm_db=":memory:", prm_boss_groups="on", prm_group_with=f"{BOSS}，老板二")
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock, send=lambda c, t: True)
    backend = Backend(contacts={BOSS: BOSS_ID})
    prm.boss_groups.attach(backend)
    server.register_prm(prm)
    try:
        assert prm.boss_groups.scan() == 0
        backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": clock.now}
        clock.now += 61
        assert prm.boss_groups.scan() == 0
        shown = call("/api/prm")[1]["boss_groups"]
        assert shown["why_not"] == "" and shown["warning"].startswith("PRM_GROUP_WITH 里的「老板二」在这个微信里找不到")
        [pending] = shown["pending"]
        assert pending["chat"] == GROUP and pending["members"] == [{"wxid": CREATOR, "name": "小美"}]
        out = call("/api/prm/boss-groups/pick", "POST", {"chat": GROUP, "wxid": CREATOR})[1]
        assert out["ok"] is True and prm.is_party_group(GROUP)
    finally:
        server.register_prm(None)
        relay.stop()


def test_the_running_console_shows_a_group_draft_where_it_goes(tmp_path):
    """老板拉的群: a packet of her line in the group says so (``channel`` group, ``group_name``); others are ``wechat``."""
    from test_prm_boss_groups import BOSS, BOSS_ID, GROUP, GROUP_NAME, ME, Backend

    clock = Clock()
    cfg = SimpleNamespace(claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1",
                          claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
                          claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid="wxid_admin",
                          prm_copilot="on", prm_db=":memory:", prm_boss_groups="on", prm_group_with=BOSS)
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock, send=lambda c, t: True)
    backend = Backend()
    prm.boss_groups.attach(backend)
    prm.boss_groups.scan()
    backend.groups[GROUP] = {"name": GROUP_NAME, "members": [ME, BOSS_ID, CREATOR], "joined_at": clock.now}
    clock.now += 61
    prm.boss_groups.scan()
    server.register_prm(prm)
    try:
        clock.now += SETTLE_SEC + 1
        job = relay.next_job(0, ("prm",))                              # the group's 开场白
        relay.finish(job["id"], {"ok": True, "text": json.dumps({
            "intent": "打招呼", "slots": {}, "stage_proposal": "", "message_kind": "greeting",
            "reply_draft": "您好～", "attachments_to_send": [], "needs_attention": False, "reason": ""},
            ensure_ascii=False)})
        relay.tick()
        [packet] = call("/api/prm")[1]["pending"]
        assert (packet["channel"], packet["group_name"], packet["opening"]) == ("group", GROUP_NAME, True)
    finally:
        server.register_prm(None)
        relay.stop()
