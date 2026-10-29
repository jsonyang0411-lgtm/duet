"""The edge protocol (edge-protocol.md) as the Mac sees it: one set of cases, run through the
client library against both Windows edges, the fake (src/edge/fake.py) and the real one
(src/edge/service.py behind the relay's HTTP server, WeChat replaced by a recorder), so
both answer the same way."""

import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.edge import protocol as P
from src.edge.client import EdgeClient, EdgeError
from src.edge.fake import FakeEdge
from src.edge.service import EdgeService
from src.edge.store import EdgeStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.wechat.new_friends import NewFriendService, NewFriendStore

ROOT = Path(__file__).resolve().parents[1]
CREATOR, OTHER = "wxid_creator01", "wxid_creator02"
EDGE_TOKEN, RELAY_TOKEN = "e" * 40, "r" * 40
PRIVATE_CHATS = {CREATOR: "小美", "wxid_new01": "阿花", "wxid_admin": "管理员"}
IMAGE = (".jpg", ".jpeg", ".png", ".gif")


class Side:
    """What every edge offers a test: the client, and WeChat's moves."""

    def ready(self):
        """Every case starts after the state the edge said at start (acknowledged)."""
        events = self.client.events(after=0, wait=0)
        assert [e["type"] for e in events] == [P.EDGE_STATUS]
        self.base = events[-1]["id"]
        self.client.ack(self.base)


class FakeSide(Side):
    name = "fake"

    def __init__(self, tmp_path):
        self.edge = FakeEdge(auto_send=False, reserved=("wxid_admin", "wxid_helper"),
                             private_chats=[{"wxid": w, "name": n} for w, n in PRIVATE_CHATS.items()]).start()
        self.client = EdgeClient(self.edge.url, self.edge.token)
        self.url, self.token = self.edge.url, self.edge.token
        self.ready()

    def creator_says(self, chat, text, files=None, quote=None, msg_type=1):
        self.edge.creator_says(chat, text, files=files, quote=quote, msg_type=msg_type)

    def operator_says(self, chat, text):
        self.edge.operator_says(chat, text)

    def notice(self, chat, text):
        self.edge.notice(chat, text)

    def wechat(self, online):
        self.edge.set_status(wechat=P.ONLINE if online else P.OFFLINE, detail="找不到微信主窗口" if not online else "")

    def deliver(self):
        for s in self.edge.sends:
            if s["status"] == P.QUEUED:
                self.edge.complete(s["send_id"])

    def read_back(self, send_id):
        self.edge.seen(send_id, delay_s=2)

    def deliver_read_back_at_once(self):
        for s in self.edge.sends:
            if s["status"] == P.QUEUED:
                self.edge.start_sending(s["send_id"])
                self.edge.seen(s["send_id"])
                self.edge.complete(s["send_id"])

    def pass_read_back_window(self, send_id):
        self.edge.unseen(send_id)

    def crash_while_sending(self, send_id):
        self.edge.start_sending(send_id)
        self.edge.restart()

    def friend_added(self, wxid, name, verify_text="", how="manual"):
        self.edge.friend_added(wxid, name, verify_text=verify_text, how=how)

    def wechat_gap(self, start, end, reason):
        self.edge.wechat_gap(start, end, reason)

    def rebuild_db(self):
        self.edge.rebuild_db()

    def close(self):
        self.edge.stop()


class FriendsDb:
    """WeChat's database as the new-friend scan reads it (src/wechat/new_friends.py): session rows
    without names, messages newest first, the lines the probe saw on the real machine."""

    is_open = True

    def __init__(self, clock):
        self.clock = clock
        self.chats, self.times, self.names, self.count = {}, {}, {}, 0

    def say(self, chat, sender, text, local_type=1, ago=0):
        self.count += 1
        at = int(self.clock.now - ago)
        self.chats.setdefault(chat, []).insert(0, {"sender_username": sender, "message_content": text,
                                                   "local_type": local_type, "create_time": at,
                                                   "local_id": self.count})
        self.times[chat] = max(self.times.get(chat, 0), at)

    def get_sessions(self):
        return [{"username": chat, "sort_timestamp": at} for chat, at in self.times.items()]

    def get_messages(self, talker, limit=200):
        return self.chats.get(talker, [])[:limit]

    def get_display_names(self, usernames):
        return {u: self.names[u] for u in usernames if u in self.names}


class FriendsBackend:
    """What the new-friend scan uses of the WCDB backend."""

    def __init__(self, db):
        self.db = db

    def with_client(self, fn):
        return fn(self.db)

    def own_wxid(self):
        return "wxid_bot_b6ce"


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class RealSide(Side):
    """The Windows edge as Bot._start_edge sets it up, on the relay's HTTP server."""

    name = "windows"

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.clock = Clock()
        self.texts, self.files = [], []
        self.online = True
        self.echo = False                        # True: WeChat shows a sent line before the send returns
        self.collected = {}                      # message id -> (path, name, problem)
        self.seq = 0
        self.config = SimpleNamespace(
            claude_relay="on", claude_relay_token=RELAY_TOKEN, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_prefix="cc", claude_relay_wxids=["wxid_helper"], claude_relay_db=":memory:",
            claude_relay_max_chars=1500, admin_wxid="wxid_admin", prm_edge="on", prm_edge_token=EDGE_TOKEN,
            edge_send_min_gap_sec=0, edge_send_per_hour=1000, edge_seen_text_sec=1800, edge_seen_file_sec=600,
            edge_keep_days=7)
        self.relay = ClaudeRelay(self.config, store=RelayStore(":memory:"), clock=self.clock,
                                 files_dir=tmp_path / "relay")
        self.relay.start()
        self.store = EdgeStore(":memory:")
        self.edge = self._service()
        host, port = self.relay.address
        self.url, self.token = f"http://{host}:{port}", EDGE_TOKEN
        self.client = EdgeClient(self.url, EDGE_TOKEN)
        # New friends as Bot._start_new_friends wires them for PRM_EDGE: found in WeChat's database,
        # handed to the edge (whichever edge runs now: crash_while_sending starts another).
        self.wechat_db = FriendsDb(self.clock)
        self.friends = NewFriendService(FriendsBackend(self.wechat_db), NewFriendStore(str(tmp_path / "messages.db")),
                                        on_found=[lambda event: self.edge.friend_added(event)], clock=self.clock,
                                        pause=0)
        self.friends.scan_once()                                         # its start marker
        self.ready()

    def _service(self):
        edge = EdgeService(self.config, store=self.store, clock=self.clock, files_dir=self.tmp / "edge",
                           send_text=self._send_text,
                           send_file=lambda chat, path: self.files.append((chat, os.path.basename(path))) or True,
                           collect=lambda msg, kind, n: self.collected[msg["message_id"]],
                           own_wxid=lambda: "wxid_bot", wechat_online=lambda: self.online,
                           db_readable=lambda: True, private_names=lambda: dict(PRIVATE_CHATS))
        self.relay.attach_edge(edge, EDGE_TOKEN)
        edge.start(run_thread=False)                                     # the test drives tick()
        return edge

    def _send_text(self, chat, text):
        self.texts.append((chat, text))
        if self.echo:
            self.edge.on_own_message(self._msg(chat, text, 1, own=True))
        return True

    def _msg(self, chat, content, msg_type, own=False, **extra):
        self.seq += 1
        return {"message_id": f"wcdb-{self.seq}", "chat_id": chat, "sender_id": "wxid_bot" if own else chat,
                "sender_name": "我" if own else "达人", "content": content, "msg_type": msg_type, "is_group": False,
                "timestamp": int(self.clock.now), **extra}

    def creator_says(self, chat, text, files=None, quote=None, msg_type=1):
        extra = {}
        if quote is not None:
            sender = {"us": "wxid_bot", "creator": chat}.get(quote.get("from"), "wxid_someone")
            extra = {"quoted_content": quote["text"], "quoted_sender_id": sender,
                     "quoted_server_id": quote.get("server_id", "")}
            msg_type = 49
        msg = None
        for item in files or []:                 # one attachment per WeChat message
            if isinstance(item, dict):
                msg = self._msg(chat, "[文件]", 49, file_name=item["name"], **extra)
                self.collected[msg["message_id"]] = ("", item["name"], item["missing"])
            else:
                name, data = item[0], item[1]
                path = self.tmp / f"wechat-{self.seq}-{name}"
                path.write_bytes(data)
                is_image = name.lower().endswith(IMAGE)
                msg = self._msg(chat, "[图片]" if is_image else "[文件]", 3 if is_image else 49,
                                **({} if is_image else {"file_name": name}), **extra)
                self.collected[msg["message_id"]] = (str(path), name, "")
            self.edge.handle(msg)
        if msg is None:
            self.edge.handle(self._msg(chat, text, msg_type, **extra))

    def operator_says(self, chat, text):
        self.edge.on_own_message(self._msg(chat, text, 1, own=True))

    def notice(self, chat, text):
        self.edge.handle(self._msg(chat, text, 10000, special={"kind": "system", "title": text}))

    def wechat(self, online):
        self.online = online
        self.edge._last_status_check = 0
        self.edge.tick()

    def deliver(self):
        for _ in range(50):
            if not self.store.sends_in((P.QUEUED,)):
                return
            self.edge.tick()

    def read_back(self, send_id):
        record = self.store.send(send_id)
        self.clock.now += 2
        self.edge.on_own_message(self._msg(record["chat_id"], record["text"], 1, own=True))

    def deliver_read_back_at_once(self):
        self.echo = True
        try:
            self.deliver()
        finally:
            self.echo = False

    def pass_read_back_window(self, send_id):
        self.clock.now += 1801
        self.edge.tick()

    def crash_while_sending(self, send_id):
        assert self.store.move_send(send_id, (P.QUEUED,), P.SENDING)      # the process died mid-send
        self.edge.stop()
        self.edge = self._service()                                       # and started again

    def friend_added(self, wxid, name, verify_text="", how="manual"):
        """WeChat writes what the probe saw (tools/friend_probe.py) and the scan finds it."""
        db, self.clock.now = self.wechat_db, self.clock.now + 2
        db.names[wxid] = name
        if how == "requested":
            db.say(wxid, "wxid_bot", "你好，我是品牌商务", ago=60)
            db.say(wxid, wxid, "我通过了你的朋友验证请求，现在我们可以开始聊天了")
            db.say(wxid, "", "以上是打招呼的消息", 10000)
        else:
            if verify_text:
                db.say(wxid, wxid, verify_text, ago=5)
            if how == "auto_accept":
                self.friends.accepts.note(self.clock.now - 10)          # FriendManager accepted it just before
            tail = "以上是打招呼的消息。" if how == "manual" else "现在可以开始聊天了。"
            db.say(wxid, "", f"你已添加了{name}，{tail}", 10000)
        self.friends.scan_once()

    def wechat_gap(self, start, end, reason):
        """PC WeChat came back after a gap (src/wechat/presence.py hands the gap to the edge, Bot._start_presence)."""
        self.edge.wechat_gap({"id": 7, "from": start + 0.4, "to": end + 0.6, "reason": reason, "detail": "找不到微信主窗口"})

    def rebuild_db(self):
        """edge.db made anew (reinstalled, deleted): a new store under a restarted edge."""
        self.edge.stop()
        self.store = EdgeStore(":memory:")
        self.edge = self._service()

    def close(self):
        self.edge.stop()
        self.relay.stop()


@pytest.fixture(params=[FakeSide, RealSide], ids=lambda side: side.name)
def side(request, tmp_path):
    s = request.param(tmp_path)
    yield s
    s.close()


def drain(side, after=None):
    return side.client.events(after=side.base if after is None else after, wait=0)


def types(events):
    return [e["type"] for e in events]


# ── health and the token ──────────────────────────────────────────────────

def test_health_says_the_protocol_and_the_state(side):
    h = side.client.health()
    assert h["ok"] is True and h["protocol"] == P.PROTOCOL and h["parties_version"] == 0
    assert (h["wechat"], h["db"], h["send"]) == (P.ONLINE, P.OK, P.OK) and h["events_pending"] == 0
    assert h["can_send_files"] is True


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
    assert drain(side) == []
    side.client.put_parties(1, [CREATOR])
    side.creator_says(OTHER, "不在名单里")
    side.creator_says(CREATOR, "你好")
    [event] = drain(side)
    assert event["type"] == P.CREATOR_MESSAGE and event["chat_id"] == CREATOR and event["text"] == "你好"


# ── events ────────────────────────────────────────────────────────────────

def test_events_come_in_order_and_again_until_acknowledged(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    side.creator_says(CREATOR, "第一条")
    side.creator_says(CREATOR, "第二条", quote={"text": "这是新款的色卡～", "from": "us", "server_id": "77"})
    side.operator_says(CREATOR, "我来回吧")
    side.notice(CREATOR, "对方撤回了一条消息")
    events = drain(side)
    assert types(events) == [P.CREATOR_MESSAGE, P.CREATOR_MESSAGE, P.OPERATOR_MESSAGE, P.CHAT_NOTICE]
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    first, second, operator, notice = events
    assert first["text"] == "第一条" and first["message_id"] and first["msg_type"] == 1 and first["files"] == []
    assert second["text"] == "第二条" and second["quote"] == {"text": "这是新款的色卡～", "from": "us", "server_id": "77"}
    assert operator["text"] == "我来回吧" and notice["text"] == "对方撤回了一条消息"
    assert c.health()["events_pending"] == 4
    assert drain(side) == events                                          # not acknowledged: again
    assert c.events(after=second["id"], wait=0) == [operator, notice]
    assert c.ack(second["id"])["pending"] == 2 and c.health()["events_pending"] == 2
    assert c.ack(1)["pending"] == 2                                       # acknowledgements only go up
    assert c.events(after=notice["id"], wait=0) == []


def test_a_long_poll_waits_and_wakes_up_for_a_new_event(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    started = time.monotonic()
    assert c.events(after=side.base, wait=1) == []
    assert time.monotonic() - started >= 0.9
    timer = threading.Timer(0.5, lambda: side.creator_says(CREATOR, "来了"))
    timer.start()
    started = time.monotonic()
    [event] = c.events(after=side.base, wait=10)
    assert event["text"] == "来了" and time.monotonic() - started < 5


def test_files_come_with_the_event(side, tmp_path):
    c = side.client
    c.put_parties(1, [CREATOR])
    picture = bytes(range(256)) * 40
    side.creator_says(CREATOR, "[图片]", files=[("图片-1.jpg", picture)], msg_type=3)
    side.creator_says(CREATOR, "[文件] 合同.pdf", files=[{"name": "合同.pdf", "missing": "电脑微信还没下载这个文件"}],
                      msg_type=49)
    shown, missing = drain(side)
    [entry] = shown["files"]
    assert shown["text"] == "[图片]" and shown["msg_type"] == 3
    assert entry["n"] == 1 and entry["name"] == "图片-1.jpg" and entry["size"] == len(picture) and entry["kind"] == "image"
    assert missing["text"] == "[文件] 合同.pdf"
    assert missing["files"] == [{"n": 1, "name": "合同.pdf", "missing": "电脑微信还没下载这个文件"}]
    assert c.download(shown["id"], 1, tmp_path / "pic.jpg") == len(picture)
    assert (tmp_path / "pic.jpg").read_bytes() == picture and not (tmp_path / "pic.jpg.part").exists()
    for event_id, n in ((shown["id"], 2), (missing["id"], 1)):
        with pytest.raises(EdgeError) as e:
            c.download(event_id, n, tmp_path / "x")
        assert e.value.status == 404


def test_a_new_friend_is_one_event_whoever_let_her_in(side):
    """friend_added: once per wxid, listed or not (she is not a creator yet); the greeting as it was."""
    side.friend_added("wxid_new01", "阿花", verify_text="我是阿花，电话 13800138000", how="manual")
    side.friend_added("wxid_new02", "小青", how="requested")
    side.friend_added("wxid_new03", "小红", verify_text="想合作", how="auto_accept")
    side.friend_added("wxid_new01", "阿花", verify_text="我是阿花，电话 13800138000", how="manual")   # again: once
    events = drain(side)
    assert types(events) == [P.FRIEND_ADDED] * 3
    assert [{k: e[k] for k in ("wxid", "name", "remark", "verify_text", "how")} for e in events] == [
        {"wxid": "wxid_new01", "name": "阿花", "remark": "", "verify_text": "我是阿花，电话 13800138000", "how": "manual"},
        {"wxid": "wxid_new02", "name": "小青", "remark": "", "verify_text": "", "how": "requested"},
        {"wxid": "wxid_new03", "name": "小红", "remark": "", "verify_text": "想合作", "how": "auto_accept"}]
    assert all("chat_id" not in e and e["at"] for e in events)
    # the scan's event has more for 加百应达人微信 (own_greeting, kind, added_at): the edge protocol stays as it was
    assert not any({"own_greeting", "kind", "added_at"} & set(e) for e in events)
    assert side.client.ack(events[-1]["id"])["pending"] == 0


def test_a_wechat_gap_is_an_event_the_mac_acknowledges(side):
    """wechat_gap（2026-10-28 加的，只增不改）：电脑微信从 from 到 to 不在线，reason 是稳定的英文值；
    整秒、不带聊天，确认以后不再给。"""
    side.wechat_gap(1761462000, 1761465600, P.GAP_OFFLINE_WHILE_RUNNING)
    side.wechat_gap(1761300000, 1761300600, P.GAP_STOPPED_WITH_BOT)
    events = drain(side)
    assert types(events) == [P.WECHAT_GAP] * 2
    assert [{k: e[k] for k in ("from", "to", "reason")} for e in events] == [
        {"from": 1761462000, "to": 1761465600, "reason": "offline_while_running"},
        {"from": 1761300000, "to": 1761300600, "reason": "stopped_with_bot"}]
    assert all(set(e) == {"id", "type", "at", "from", "to", "reason"} for e in events)
    assert side.client.ack(events[-1]["id"])["pending"] == 0 and drain(side, after=events[-1]["id"]) == []
    assert P.GAP_REASONS == ("offline_while_running", "stopped_with_bot") and P.WECHAT_GAP in P.EVENT_TYPES


def test_health_says_which_database_windows_keeps(side):
    """db_id: made with Windows' edge.db; another one means the database was made anew."""
    first = side.client.health()["db_id"]
    assert isinstance(first, str) and len(first) >= 16 and side.client.health()["db_id"] == first
    side.client.put_parties(1, [CREATOR])
    side.rebuild_db()
    health = side.client.health()
    assert health["db_id"] != first and health["parties_version"] == 0
    events = side.client.events(after=0, wait=0)
    assert types(events) == [P.EDGE_STATUS] and events[0]["id"] == 1   # the numbering starts again


def test_status_changes_are_events_and_in_health(side):
    side.wechat(False)
    side.wechat(False)                                                    # no change: no event
    [event] = drain(side)
    assert event["type"] == P.EDGE_STATUS and event["wechat"] == P.OFFLINE and "微信" in event["detail"]
    assert side.client.health()["wechat"] == P.OFFLINE
    side.wechat(True)
    assert drain(side)[-1]["wechat"] == P.ONLINE


# ── sending ───────────────────────────────────────────────────────────────

def test_a_send_goes_out_once_and_is_read_back(side):
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
    with pytest.raises(EdgeError) as e:
        c.send("pk-9", CREATOR, "同一个附件再用一次", uploads=[up["upload_id"]])
    assert e.value.status == 400                                         # an upload goes with one send
    side.deliver()
    side.read_back("pk-1")
    events = drain(side)
    assert types(events) == [P.SEND_RESULT, P.SEND_SEEN]
    assert events[0]["send_id"] == "pk-1" and events[0]["status"] == P.SENT and events[0]["error"] == ""
    assert events[1]["send_id"] == "pk-1" and events[1]["delay_s"] >= 0
    status = side.client.send_status("pk-1")
    assert status["status"] == P.SENT and status["sent_at"] and status["seen_at"]
    assert c.send("pk-1", CREATOR, "样品这周五前寄出～", uploads=[up["upload_id"]])["status"] == P.SENT   # still once
    assert c.send_status("pk-404") is None


def test_send_requests_are_checked(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    bad = [dict(send_id="has space", chat_id=CREATOR, text="hi"), dict(send_id="x" * 65, chat_id=CREATOR, text="hi"),
           dict(send_id="s1", chat_id="", text="hi"), dict(send_id="s1", chat_id=CREATOR, text="字" * 1501),
           dict(send_id="s1", chat_id=CREATOR, text="  "), dict(send_id="s1", chat_id=CREATOR, text="hi", ttl_sec=59),
           dict(send_id="s1", chat_id=CREATOR, text="hi", ttl_sec=86401),
           dict(send_id="s1", chat_id=CREATOR, text="hi", ttl_sec=True),
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
    for length, status in ((str(P.MAX_UPLOAD_BYTES + 1), 413), ("0", 400)):
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.putrequest("POST", "/edge/uploads?name=big.bin")
        conn.putheader("Authorization", f"Bearer {side.token}")
        conn.putheader("Content-Length", length)
        conn.endheaders()                                                # no body follows
        response = conn.getresponse()
        assert response.status == status
        assert json.loads(response.read())["error"] in ("too_large", "bad_request")
        conn.close()


def test_a_send_read_back_before_it_returns_is_seen_right_after_its_result(side):
    """WeChat shows a sent line at once and Windows can read it back before the send returns
    (Windows test, 2026-10-26): send_result first, then send_seen with delay_s 0."""
    c = side.client
    c.put_parties(1, [CREATOR])
    c.send("pk-5", CREATOR, "brief 在下面～")
    side.deliver_read_back_at_once()
    events = drain(side)
    assert types(events) == [P.SEND_RESULT, P.SEND_SEEN]
    assert events[0]["status"] == P.SENT and events[1]["send_id"] == "pk-5" and events[1]["delay_s"] == 0
    assert c.send_status("pk-5")["seen_at"]


def test_a_failed_and_an_unseen_send_are_events(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    c.send("pk-2", CREATOR, "收到～")
    side.deliver()
    side.pass_read_back_window("pk-2")
    events = drain(side)
    assert types(events) == [P.SEND_RESULT, P.SEND_UNSEEN]
    assert events[1]["send_id"] == "pk-2" and events[1]["window_s"] > 0


def test_a_send_in_flight_at_a_restart_is_indeterminate_and_never_sent_again(side):
    c = side.client
    c.put_parties(1, [CREATOR])
    c.send("pk-3", CREATOR, "在吗")
    side.crash_while_sending("pk-3")
    side.deliver()
    [event] = [e for e in drain(side) if e["type"] == P.SEND_RESULT]
    assert event["send_id"] == "pk-3" and event["status"] == P.INDETERMINATE and event["error"]
    assert c.send_status("pk-3")["status"] == P.INDETERMINATE
    assert c.send("pk-3", CREATOR, "在吗")["status"] == P.INDETERMINATE


def test_leaving_the_list_fails_the_queued_sends(side):
    c = side.client
    c.put_parties(1, [CREATOR, OTHER])
    c.send("pk-4", OTHER, "你好")
    c.put_parties(2, [CREATOR])
    [event] = [e for e in drain(side) if e["type"] == P.SEND_RESULT]
    assert event["send_id"] == "pk-4" and event["status"] == P.FAILED and "名单" in event["error"]
    with pytest.raises(EdgeError) as e:
        c.send("pk-5", OTHER, "你好")
    assert e.value.status == 403


# ── the Windows edge on the relay's server ────────────────────────────────

def test_the_cc_token_and_the_edge_token_open_one_door_each(tmp_path):
    side = RealSide(tmp_path)
    try:
        with pytest.raises(EdgeError) as e:
            EdgeClient(side.url, RELAY_TOKEN).health()
        assert e.value.status == 401
        conn = http.client.HTTPConnection(*side.relay.address, timeout=10)
        conn.request("GET", "/relay/health", headers={"Authorization": f"Bearer {EDGE_TOKEN}"})
        assert conn.getresponse().status == 401
        conn.close()
        side.relay.attach_edge(None, "")
        with pytest.raises(EdgeError) as e:
            side.client.health()
        assert e.value.status == 404                                     # PRM_EDGE off: no edge
    finally:
        side.close()


# ── the fake on its own, and the client ───────────────────────────────────

def test_the_fake_by_default_sends_at_once_and_can_fail(tmp_path):
    with FakeEdge() as edge:
        c = EdgeClient(edge.url, edge.token)
        c.put_parties(1, [CREATOR])
        assert c.send("a", CREATOR, "你好")["status"] == P.SENT
        edge.fail_next(1, "找不到聊天窗口")
        assert c.send("b", CREATOR, "在吗")["status"] == P.FAILED
        assert edge.sent() == [(CREATOR, "你好", [])]
        assert types(c.events(after=1, wait=0)) == [P.SEND_RESULT, P.SEND_SEEN, P.SEND_RESULT]
    clock = [1000.0]
    with FakeEdge(auto_send=False, clock=lambda: clock[0]) as edge:
        c = EdgeClient(edge.url, edge.token)
        c.put_parties(1, [CREATOR])
        c.send("d", CREATOR, "过时了", ttl_sec=60)
        assert edge.expire_due() == []
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
        "    print(c.events(after=1, wait=0)[0]['text'], c.send('s', 'wxid_creator01', 'hi')['status'],\n"
        "          c.upload_bytes(b'abc', 'a.txt')['size'], sys.version_info[:2])\n",
        encoding="utf-8")
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("你好 sent 3 ")


def test_the_mac_side_keeps_wechat_gaps_on_the_system_python(tmp_path):
    """wechat_gap 在 Mac 自带的 Python 3.9 上也收得下、存得下（src/wechat/edge_backend.py 的 EdgeGaps）。"""
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    script = tmp_path / "gaps.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "from src.edge.fake import FakeEdge\n"
        "from src.edge.client import EdgeClient\n"
        "from src.wechat.edge_backend import EdgeGaps\n"
        "with FakeEdge() as edge:\n"
        "    c = EdgeClient(edge.url, edge.token)\n"
        "    edge.wechat_gap(1761462000, 1761465600, 'stopped_with_bot')\n"
        "    [ev] = c.events(after=1, wait=0)\n"
        f"    gaps = EdgeGaps(Path({str(tmp_path / 'edge_gaps.json')!r}))\n"
        "    first, again = gaps.add(ev, 1.0), gaps.add(ev, 2.0)\n"
        "    print(ev['type'], ev['reason'], first['id'], again, len(gaps.unseen()), gaps.mark_seen(first['id'], 3.0),\n"
        "          gaps.unseen(), sys.version_info[:2])\n",
        encoding="utf-8")
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("wechat_gap stopped_with_bot 1 None 1 True [] ")


def test_uploads_go_block_by_block(tmp_path):
    """One send of the whole body has the timeout in all (a slow link to Windows would cut a big
    upload off); from a file object, the time limit is per block."""
    client = EdgeClient("http://127.0.0.1:9", "t" * 40)
    seen = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"upload_id": "up-1", "size": 3, "sha256": "x"}'

    def open_(req, timeout):
        seen.append((hasattr(req.data, "read"), req.get_header("Content-length")))
        return Answer()

    client._opener = SimpleNamespace(open=open_)
    client.upload_bytes(b"abc", "a.txt")
    (tmp_path / "b.txt").write_bytes(b"abcd")
    client.upload(tmp_path / "b.txt")
    assert seen == [(True, "3"), (True, "4")]


# ── an answer that is not the edge's (2026-10-28) ─────────────────────────

class Odd:
    """A server that answers every request with ``(status, headers, body)``: a proxy's page,
    a cut-off answer."""

    def __init__(self, status, headers, body):
        import http.server

        answer = (status, headers, body)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status, headers, body = answer
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True

            do_POST = do_GET

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = EdgeClient(f"http://127.0.0.1:{self.server.server_address[1]}", "t" * 40, timeout=5)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("headers, body", [
    ({"Content-Type": "text/html"}, b"<html><body>Proxy error</body></html>"),           # a proxy's page
    ({"Content-Type": "application/json", "Content-Length": "200"}, b'{"events": [{"id": 1'),  # cut off
    ({"Content-Type": "application/json"}, b'[1, 2]'),                                    # not an object
])
def test_an_answer_that_is_not_the_edges_is_an_os_error(headers, body):
    from src.edge.client import EdgeBadAnswer
    odd = Odd(200, headers, body)
    try:
        with pytest.raises(EdgeBadAnswer) as caught:
            odd.client.events(after=0, wait=0)
        assert isinstance(caught.value, OSError), "调用方按网络出错处理：退避重试"
    finally:
        odd.close()


def test_a_proxys_error_page_is_still_an_edge_error_with_its_status():
    odd = Odd(502, {"Content-Type": "text/html"}, b"<html>Bad Gateway</html>")
    try:
        with pytest.raises(EdgeError) as caught:
            odd.client.health()
        assert caught.value.status == 502 and caught.value.code == ""
    finally:
        odd.close()


def test_a_download_cut_short_is_not_kept(tmp_path):
    from src.edge.client import EdgeBadAnswer
    odd = Odd(200, {"Content-Type": "image/jpeg", "Content-Length": "1000"}, b"\xff\xd8" + b"x" * 98)
    try:
        dest = tmp_path / "图片-1.jpg"
        with pytest.raises(EdgeBadAnswer):
            odd.client.download(7, 1, dest)
        assert not dest.exists() and not (tmp_path / "图片-1.jpg.part").exists()
    finally:
        odd.close()


def test_a_download_shorter_than_the_events_size_is_not_kept(side, tmp_path):
    from src.edge.client import EdgeBadAnswer
    side.client.put_parties(1, [CREATOR])
    side.creator_says(CREATOR, "[图片]", files=[("图片-1.jpg", b"\xff\xd8picture")], msg_type=3)
    [event] = drain(side)
    dest = tmp_path / "图片-1.jpg"
    with pytest.raises(EdgeBadAnswer):
        side.client.download(event["id"], 1, dest, size=event["files"][0]["size"] + 5)
    assert not dest.exists()
    assert side.client.download(event["id"], 1, dest, size=event["files"][0]["size"]) == len(b"\xff\xd8picture")
