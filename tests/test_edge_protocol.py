"""The edge protocol (edge-protocol.md) as the Mac sees it: one set of cases, run through the
client library against every Windows edge there is (the fake now; the real one is added
with its own side below), so both answer the same way."""

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.edge import protocol as P
from src.edge.client import EdgeClient, EdgeError
from src.edge.fake import FakeEdge

ROOT = Path(__file__).resolve().parents[1]
CREATOR, OTHER = "wxid_creator01", "wxid_creator02"


class FakeSide:
    """The fake Windows edge, driven the way the real one is driven by WeChat."""

    name = "fake"

    def __init__(self, tmp_path):
        self.edge = FakeEdge(auto_send=False, reserved=("wxid_admin", "wxid_helper"),
                             private_chats=[{"wxid": CREATOR, "name": "小美"}, {"wxid": "wxid_new01", "name": "阿花"},
                                            {"wxid": "wxid_admin", "name": "管理员"}]).start()
        self.client = EdgeClient(self.edge.url, self.edge.token)
        self.url, self.token = self.edge.url, self.edge.token

    def creator_says(self, chat, text, files=None, quote=None, msg_type=1):
        self.edge.creator_says(chat, text, files=files, quote=quote, msg_type=msg_type)

    def operator_says(self, chat, text):
        self.edge.operator_says(chat, text)

    def notice(self, chat, text):
        self.edge.notice(chat, text)

    def wechat(self, online, detail=""):
        self.edge.set_status(wechat=P.ONLINE if online else P.OFFLINE, detail=detail)

    def deliver(self):
        for s in self.edge.sends:
            if s["status"] == P.QUEUED:
                self.edge.complete(s["send_id"])

    def read_back(self, send_id):
        self.edge.seen(send_id, delay_s=2)

    def pass_read_back_window(self, send_id):
        self.edge.unseen(send_id)

    def crash_while_sending(self, send_id):
        self.edge.start_sending(send_id)
        self.edge.restart()

    def close(self):
        self.edge.stop()


SIDES = [FakeSide]


@pytest.fixture(params=SIDES, ids=lambda side: side.name)
def side(request, tmp_path):
    s = request.param(tmp_path)
    yield s
    s.close()


def drain(client, after=0):
    return client.events(after=after, wait=0)


def types(events):
    return [e["type"] for e in events]


# ── health and the token ──────────────────────────────────────────────────

def test_health_says_the_protocol_and_the_state(side):
    h = side.client.health()
    assert h["ok"] is True and h["protocol"] == P.PROTOCOL and h["parties_version"] == 0
    assert (h["wechat"], h["db"], h["send"]) == (P.ONLINE, P.OK, P.OK) and h["events_pending"] == 0


def test_every_path_wants_the_edge_token(side):
    stranger = EdgeClient(side.url, "x" * 40)
    calls = [lambda: stranger.health(), lambda: stranger.events(wait=0), lambda: stranger.ack(1),
             lambda: stranger.put_parties(1, [CREATOR]), lambda: stranger.private_chats(),
             lambda: stranger.send("s1", CREATOR, "hi"), lambda: stranger.upload_bytes(b"x", "a.txt")]
    for call in calls:
        with pytest.raises(EdgeError) as e:
            call()
        assert e.value.status == 401 and e.value.code == "unauthorized"
    with pytest.raises(EdgeError) as e:
        side.client._call("GET", "/edge/nothing")
    assert e.value.status == 404
    with pytest.raises(EdgeError) as e:
        side.client._call("POST", "/edge/health", {})
    assert e.value.status == 405


# ── the list of creators ──────────────────────────────────────────────────

def test_the_list_goes_by_version_and_a_retry_is_safe(side):
    c = side.client
    answer = c.put_parties(1, [CREATOR, "wxid_admin", "12345@chatroom", "bad id", CREATOR])
    assert answer["ok"] and answer["version"] == 1
    assert {r["wxid"]: r["reason"] for r in answer["rejected"]} == {
        "wxid_admin": "管理员或 cc 用户不能当达人", "12345@chatroom": "群聊不能当达人", "bad id": "wxid 格式不对"}
    assert c.health()["parties_version"] == 1
    assert c.put_parties(1, [CREATOR, "wxid_admin", "12345@chatroom", "bad id"])["version"] == 1   # a retry
    with pytest.raises(EdgeError) as e:
        c.put_parties(1, [CREATOR, OTHER])                               # same version, another list
    assert e.value.status == 409 and e.value.body["current_version"] == 1
    with pytest.raises(EdgeError) as e:
        c.put_parties(0, [CREATOR])                                      # versions start at 1
    assert e.value.status == 400
    assert c.put_parties(2, [CREATOR, OTHER])["version"] == 2
    with pytest.raises(EdgeError) as e:
        c.put_parties(1, [CREATOR])                                      # an older push
    assert e.value.status == 409 and e.value.body["current_version"] == 2


def test_private_chats_leave_out_the_list_and_the_reserved(side):
    side.client.put_parties(1, [CREATOR])
    assert side.client.private_chats() == [{"wxid": "wxid_new01", "name": "阿花"}]


def test_only_listed_chats_are_forwarded(side):
    side.creator_says(CREATOR, "还没登记的时候说的")
    assert drain(side.client) == []
    side.client.put_parties(1, [CREATOR])
    side.creator_says(OTHER, "不在名单里")
    side.creator_says(CREATOR, "你好")
    [event] = drain(side.client)
    assert event["type"] == P.CREATOR_MESSAGE and event["chat_id"] == CREATOR and event["text"] == "你好"


# ── events ────────────────────────────────────────────────────────────────

def test_events_come_in_order_and_again_until_acknowledged(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    side.creator_says(CREATOR, "第一条")
    side.creator_says(CREATOR, "第二条", quote={"text": "这是新款的色卡～", "from": "us", "server_id": "77"})
    side.operator_says(CREATOR, "我来回吧")
    side.notice(CREATOR, "对方撤回了一条消息")
    events = drain(c)
    assert types(events) == [P.CREATOR_MESSAGE, P.CREATOR_MESSAGE, P.OPERATOR_MESSAGE, P.CHAT_NOTICE]
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    first, second, operator, notice = events
    assert first["text"] == "第一条" and first["message_id"] and first["msg_type"] == 1 and first["files"] == []
    assert second["quote"] == {"text": "这是新款的色卡～", "from": "us", "server_id": "77"}
    assert operator["text"] == "我来回吧" and notice["text"] == "对方撤回了一条消息"
    assert c.health()["events_pending"] == 4
    assert drain(c) == events                                             # not acknowledged: again
    assert c.events(after=second["id"], wait=0) == [operator, notice]
    assert c.ack(second["id"])["pending"] == 2 and c.health()["events_pending"] == 2
    assert c.ack(1)["pending"] == 2                                       # acknowledgements only go up
    assert c.events(after=notice["id"], wait=0) == []


def test_a_long_poll_waits_and_wakes_up_for_a_new_event(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    started = time.monotonic()
    assert c.events(after=0, wait=1) == []
    assert time.monotonic() - started >= 0.9
    timer = threading.Timer(0.5, lambda: side.creator_says(CREATOR, "来了"))
    timer.start()
    started = time.monotonic()
    [event] = c.events(after=0, wait=10)
    assert event["text"] == "来了" and time.monotonic() - started < 5


def test_files_come_with_the_event(side, tmp_path):
    c = side.client
    c.put_parties(1, [CREATOR])
    picture = bytes(range(256)) * 40
    side.creator_says(CREATOR, "[图片]", files=[("图片-1.jpg", picture), {"name": "合同.pdf", "missing": "电脑微信还没下载这个文件"}],
                      msg_type=3)
    [event] = drain(c)
    first, second = event["files"]
    assert first["n"] == 1 and first["name"] == "图片-1.jpg" and first["size"] == len(picture) and first["kind"] == "image"
    assert second == {"n": 2, "name": "合同.pdf", "missing": "电脑微信还没下载这个文件"}
    assert c.download(event["id"], 1, tmp_path / "pic.jpg") == len(picture)
    assert (tmp_path / "pic.jpg").read_bytes() == picture and not (tmp_path / "pic.jpg.part").exists()
    for n in (2, 3):
        with pytest.raises(EdgeError) as e:
            c.download(event["id"], n, tmp_path / "x")
        assert e.value.status == 404


def test_status_changes_are_events_and_in_health(side):
    side.wechat(False, "找不到微信主窗口")
    side.wechat(False, "找不到微信主窗口")                                 # no change: no event
    [event] = drain(side.client)
    assert event["type"] == P.EDGE_STATUS and event["wechat"] == P.OFFLINE and event["detail"] == "找不到微信主窗口"
    assert side.client.health()["wechat"] == P.OFFLINE
    side.wechat(True)
    assert drain(side.client)[-1]["wechat"] == P.ONLINE


# ── sending ───────────────────────────────────────────────────────────────

def test_a_send_goes_out_once_and_is_read_back(side, tmp_path):
    c = side.client
    c.put_parties(1, [CREATOR])
    up = c.upload_bytes(b"%PDF-1.4 brief", "brief.pdf")
    assert up["size"] == 14 and len(up["sha256"]) == 64
    first = c.send("pk-1", CREATOR, "样品这周五前寄出～", uploads=[up["upload_id"]])
    assert first == {"send_id": "pk-1", "status": P.QUEUED}
    assert c.send("pk-1", CREATOR, "样品这周五前寄出～", uploads=[up["upload_id"]])["status"] == P.QUEUED
    with pytest.raises(EdgeError) as e:
        c.send("pk-1", CREATOR, "改了一个字", uploads=[up["upload_id"]])
    assert e.value.status == 409
    side.deliver()
    side.read_back("pk-1")
    events = drain(c)
    assert types(events) == [P.SEND_RESULT, P.SEND_SEEN]
    assert events[0]["send_id"] == "pk-1" and events[0]["status"] == P.SENT and events[0]["error"] == ""
    assert events[1]["send_id"] == "pk-1" and events[1]["delay_s"] >= 0
    status = c.send_status("pk-1")
    assert status["status"] == P.SENT and status["sent_at"] and status["seen_at"]
    assert c.send("pk-1", CREATOR, "样品这周五前寄出～", uploads=[up["upload_id"]])["status"] == P.SENT   # still once
    assert c.send_status("pk-404") is None


def test_send_requests_are_checked(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    bad = [dict(send_id="has space", chat_id=CREATOR, text="hi"), dict(send_id="x" * 65, chat_id=CREATOR, text="hi"),
           dict(send_id="s1", chat_id="", text="hi"), dict(send_id="s1", chat_id=CREATOR, text="字" * 1501),
           dict(send_id="s1", chat_id=CREATOR, text="  "), dict(send_id="s1", chat_id=CREATOR, text="hi", ttl_sec=59),
           dict(send_id="s1", chat_id=CREATOR, text="hi", ttl_sec=86401), dict(send_id="s1", chat_id=CREATOR, text="hi", ttl_sec=True),
           dict(send_id="s1", chat_id=CREATOR, text="hi", uploads=["up-nothing"]),
           dict(send_id="s1", chat_id=CREATOR, text="hi", uploads=["u"] * 10)]
    for body in bad:
        with pytest.raises(EdgeError) as e:
            c._call("POST", "/edge/sends", body)
        assert e.value.status == 400, body
    with pytest.raises(EdgeError) as e:
        c.send("s2", OTHER, "不在名单里")
    assert e.value.status == 403 and e.value.code == "not_party"
    assert c.send("s3", CREATOR, "字" * 1500, ttl_sec=60)["status"] == P.QUEUED


def test_uploads_need_a_length_and_stay_under_the_limit(side):
    host, port = side.url.replace("http://", "").split(":")
    for headers in ({"Content-Length": str(P.MAX_UPLOAD_BYTES + 1)}, {"Content-Length": "0"}):
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.putrequest("POST", "/edge/uploads?name=big.bin")
        conn.putheader("Authorization", f"Bearer {side.token}")
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()                                                # no body follows
        response = conn.getresponse()
        assert response.status == (413 if headers["Content-Length"] != "0" else 400)
        assert json.loads(response.read())["error"] in ("too_large", "bad_request")
        conn.close()


def test_a_failed_and_an_unseen_send_are_events(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    c.send("pk-2", CREATOR, "收到～")
    side.deliver()
    side.pass_read_back_window("pk-2")
    events = drain(c)
    assert types(events) == [P.SEND_RESULT, P.SEND_UNSEEN]
    assert events[1]["send_id"] == "pk-2" and events[1]["window_s"] > 0


def test_a_send_in_flight_at_a_restart_is_indeterminate_and_never_sent_again(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    c.send("pk-3", CREATOR, "在吗")
    side.crash_while_sending("pk-3")
    side.deliver()
    [event] = [e for e in drain(c) if e["type"] == P.SEND_RESULT]
    assert event["send_id"] == "pk-3" and event["status"] == P.INDETERMINATE and event["error"]
    assert c.send_status("pk-3")["status"] == P.INDETERMINATE
    assert c.send("pk-3", CREATOR, "在吗")["status"] == P.INDETERMINATE


def test_leaving_the_list_fails_the_queued_sends(side):
    c = side.client
    c.put_parties(1, [CREATOR, OTHER])
    c.send("pk-4", OTHER, "你好")
    c.put_parties(2, [CREATOR])
    [event] = [e for e in drain(c) if e["type"] == P.SEND_RESULT]
    assert event["send_id"] == "pk-4" and event["status"] == P.FAILED and "名单" in event["error"]
    with pytest.raises(EdgeError) as e:
        c.send("pk-5", OTHER, "你好")
    assert e.value.status == 403


# ── the fake on its own, and the client ───────────────────────────────────

def test_the_fake_by_default_sends_at_once_and_can_fail(tmp_path):
    with FakeEdge() as edge:
        c = EdgeClient(edge.url, edge.token)
        c.put_parties(1, [CREATOR])
        assert c.send("a", CREATOR, "你好")["status"] == P.SENT
        edge.fail_next(1, "找不到聊天窗口")
        assert c.send("b", CREATOR, "在吗")["status"] == P.FAILED
        assert edge.sent() == [(CREATOR, "你好", [])]
        assert types(drain(c)) == [P.SEND_RESULT, P.SEND_SEEN, P.SEND_RESULT]
        edge.auto_send = False
        c.send("c", CREATOR, "稍等", ttl_sec=60)
        assert edge.expire_due() == []
    clock = [1000.0]
    with FakeEdge(auto_send=False, clock=lambda: clock[0]) as edge:
        c = EdgeClient(edge.url, edge.token)
        c.put_parties(1, [CREATOR])
        c.send("d", CREATOR, "过时了", ttl_sec=60)
        clock[0] += 61
        assert edge.expire_due() == ["d"] and c.send_status("d")["status"] == P.EXPIRED


def test_the_client_ignores_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    with FakeEdge() as edge:
        assert EdgeClient(edge.url, edge.token).health()["ok"] is True


def test_the_mac_side_runs_on_the_system_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from src.edge.fake import FakeEdge\n"
        "from src.edge.client import EdgeClient\n"
        "with FakeEdge() as edge:\n"
        "    c = EdgeClient(edge.url, edge.token)\n"
        "    c.put_parties(1, ['wxid_creator01'])\n"
        "    edge.creator_says('wxid_creator01', '你好')\n"
        "    print(c.events(wait=0)[0]['text'], c.send('s', 'wxid_creator01', 'hi')['status'], sys.version_info[:2])\n",
        encoding="utf-8")
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("你好 sent")
