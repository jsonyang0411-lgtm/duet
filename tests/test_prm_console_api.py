"""The console's PRM API: GET /api/prm, POST /api/prm/parties, POST /api/prm/packets."""

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


def test_changes_need_a_post_and_a_body():
    for path in ("/api/prm/parties", "/api/prm/packets"):
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
