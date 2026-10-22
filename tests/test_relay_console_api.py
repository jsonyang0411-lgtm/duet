"""The agent console's relay API (GET /api/relay, POST /api/relay/cancel) and the onboarding's private chats."""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from test_web_api import _build_handler

from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.web import server

ME = "wxid_me"


def call(path, method="GET", body=None):
    raw = b"" if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    _, sock = _build_handler(path, method=method, body=raw, headers=headers)
    head, text = sock.get_response_text().split("\r\n\r\n", 1)
    return int(head.split()[1]), json.loads(text)


@pytest.fixture
def relay():
    cfg = SimpleNamespace(
        claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME,
    )
    r = ClaudeRelay(cfg, store=RelayStore(":memory:"))
    server.register_relay(r)
    yield r
    server.register_relay(None)


def ask(relay, text):
    return relay.handle({"chat_id": ME, "sender_id": ME, "content": text, "is_group": False,
                         "timestamp": time.time()})


def test_without_a_running_relay_the_state_is_empty_but_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_RELAY_DB", raising=False)
    status, d = call("/api/relay")
    assert status == 200 and d["ok"] and d["running"] is False and d["jobs"] == []
    assert not (tmp_path / "data" / "claude_relay.db").exists()            # nothing is created


def test_a_stopped_bot_still_shows_the_jobs_from_the_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = RelayStore(tmp_path / "data" / "claude_relay.db")
    store.add(ME, ME, "跑一下测试", "webot")
    store.close()
    status, d = call("/api/relay")
    assert d["running"] is False and [j["text"] for j in d["jobs"]] == ["跑一下测试"]
    assert d["counts"] == {"pending": 1}


def test_the_running_relay_shows_jobs_newest_first_without_wxids(relay):
    ask(relay, "cc 第一个")
    ask(relay, "cc @webot 第二个")
    job = relay.next_job(0)
    relay.finish(job["id"], {"ok": True, "text": "做完了", "duration_s": 12.5})
    status, d = call("/api/relay")
    assert d["running"] is True and d["mac_online"] is True and d["counts"] == {"done": 1, "pending": 1}
    assert [(j["id"], j["status"], j["project"]) for j in d["jobs"]] == [(2, "pending", "webot"), (1, "done", "")]
    assert d["jobs"][1]["result"] == "做完了" and d["jobs"][1]["duration_s"] == 12.5
    assert ME not in json.dumps(d)                                           # no chat id / sender
    assert isinstance(d["lan_addresses"], list)


def test_cancel_from_the_console(relay):
    ask(relay, "cc 一")
    ask(relay, "cc 二")
    relay.next_job(0)                                                        # #1 is running
    assert call("/api/relay/cancel", "POST", {"id": 2})[1] == {"ok": True, "outcome": "cancelled"}
    assert call("/api/relay/cancel", "POST", {"id": 1})[1] == {"ok": True, "outcome": "requested"}
    assert relay.heartbeat(1) == {"cancel": True}
    assert call("/api/relay/cancel", "POST", {"id": 9})[1]["ok"] is False
    assert call("/api/relay/cancel", "POST", {"nope": 1})[1]["ok"] is False


def test_cancel_needs_the_running_relay_and_a_post():
    server.register_relay(None)
    assert call("/api/relay/cancel", "POST", {"id": 1})[1]["ok"] is False
    status, _ = call("/api/relay/cancel")                                   # a GET must not cancel
    assert status == 405


def test_onboarding_step4_takes_the_private_chats_for_the_relay():
    with patch("src.web.server._find_or_create_env", return_value=MagicMock()), \
            patch("src.web.server._write_onboarding_to_env"), \
            patch.dict(server._onboarding_data):                        # restored afterwards
        status, d = call("/api/onboarding/step4", "POST", {
            "fun_enabled": False, "private_chat_enabled": True, "private_chats": "wxid_me"})
        assert d["ok"] is True
        assert server._onboarding_data["private_chat_enabled"] is True
        assert server._onboarding_data["private_chats"] == "wxid_me"


def test_the_relay_settings_are_in_the_allow_list():
    from src.web import advanced_config as adv
    group = next(g for g in adv.GROUPS if g["id"] == "relay")
    keys = [f["key"] for f in group["fields"]]
    assert keys[:5] == ["CLAUDE_RELAY", "CLAUDE_RELAY_ONLY", "ADMIN_WXID", "CLAUDE_RELAY_WXIDS", "CLAUDE_RELAY_TOKEN"]
    assert adv.FIELDS["CLAUDE_RELAY_TOKEN"]["type"] == "secret" and adv.FIELDS["CLAUDE_RELAY_TOKEN"]["generate"]
    errors = adv.validate_updates({"CLAUDE_RELAY": "on"}, {})
    assert set(errors) == {"CLAUDE_RELAY_TOKEN", "CLAUDE_RELAY_WXIDS"}       # both reasons, each on its field
    assert adv.validate_updates({"CLAUDE_RELAY": "on", "CLAUDE_RELAY_TOKEN": "x" * 20, "ADMIN_WXID": ME}, {}) == {}
    # what the console gets: the token masked
    shown = {f["key"]: f for g in adv.describe({"CLAUDE_RELAY_TOKEN": "secret-token-123456"}, server._mask_key)
             for f in g["fields"]}
    assert "secret-token-123456" not in json.dumps(shown["CLAUDE_RELAY_TOKEN"]["value"])
