"""The console's PRM API: GET /api/prm, POST /api/prm/parties, /api/prm/packets, /api/prm/deals, /api/prm/reveal."""

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
    assert running.sent[-1] == (CREATOR, "小美你好～样品已经寄出啦：顺丰 SF1234567890123。签收之后跟我说一声哦～")
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


def test_changes_need_a_post_and_a_body():
    for path in ("/api/prm/parties", "/api/prm/packets", "/api/prm/deals", "/api/prm/reveal"):
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
                                     "creator_platform_id": "5100000000000002", "price": "15"})
    preview = {"version": 1, "action": action, "verdict": "ready", "problems": [], "steps": [],
               "facts": [{"label": "将写入", "value": "create a 达人带货 channel product"}],
               "run": {"steps": ["create", "bind"]}, "catalog": {"synced_at": 1.0, "title": "测试主品"},
               "previewed_at": 2.0}
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
        assert d["night"] == {"on": True, "window": "18:00–09:00", "inside": True, "suspended": "", "error": ""}
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
    assert call("/api/prm")[1]["night"] == {"on": False, "window": "", "inside": False, "suspended": "", "error": ""}
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
