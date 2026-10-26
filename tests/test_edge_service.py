"""The Windows WeChat edge itself (src/edge/service.py, store.py, text.py) and how the bot
wires it: what the protocol tests cannot see from the Mac."""

import io
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_routing_steps import StepCase

from src.config import BotConfig, _validate_config
from src.edge import protocol as P
from src.edge import text as T
from src.edge.service import EdgeService
from src.edge.store import EdgeStore
from src.routing import steps as S
from src.routing.context import RouteContext, Stop
from src.wechat.wcdb_backend import WcdbBackend, _special_info

CREATOR = "wxid_creator01"
DAY = 86400


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class WeChat:
    """The backend's side of the edge: sends recorded, a picture or file found per message."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.texts, self.files = [], []
        self.text_ok = self.file_ok = True
        self.online = True
        self.readable = True
        self.found = {}

    def send_text(self, chat, text):
        self.texts.append((chat, text))
        return self.text_ok

    def send_file(self, chat, path):
        self.files.append((chat, Path(path).name))
        return self.file_ok

    def collect(self, msg, kind, n):
        return self.found[msg["message_id"]]


def make_edge(tmp_path, clock, wechat, store=None, **config):
    values = dict(prm_edge="on", prm_edge_token="e" * 40, admin_wxid="wxid_admin", claude_relay_wxids=["wxid_helper"],
                  edge_send_min_gap_sec=0, edge_send_per_hour=1000, edge_seen_text_sec=1800,
                  edge_seen_file_sec=600, edge_keep_days=7)
    values.update(config)
    edge = EdgeService(SimpleNamespace(**values), store=store or EdgeStore(":memory:"), clock=clock,
                       files_dir=tmp_path / "edge", send_text=wechat.send_text, send_file=wechat.send_file,
                       collect=wechat.collect, own_wxid=lambda: "wxid_bot", wechat_online=lambda: wechat.online,
                       db_readable=lambda: wechat.readable, private_names=lambda: {})
    edge.start(run_thread=False)
    edge.put_parties(1, [CREATOR])
    return edge


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def wechat(tmp_path):
    return WeChat(tmp_path)


@pytest.fixture
def edge(tmp_path, clock, wechat):
    e = make_edge(tmp_path, clock, wechat)
    yield e
    e.stop()


SEQ = [0]


def msg(clock, content, msg_type=1, own=False, chat=CREATOR, age=0, **extra):
    SEQ[0] += 1
    return {"message_id": f"m{SEQ[0]}", "chat_id": chat, "sender_id": "wxid_bot" if own else chat,
            "sender_name": "我" if own else "小美", "content": content, "msg_type": msg_type, "is_group": False, "timestamp": int(clock.now - age), **extra}


def events(edge, *kinds):
    found = edge.store.events_after(0, 500)
    return [e for e in found if not kinds or e["type"] in kinds]


def sent(edge, send_id, text="", uploads=(), ttl=1800):
    return edge.send({"send_id": send_id, "chat_id": CREATOR, "text": text, "uploads": list(uploads), "ttl_sec": ttl})


# ── what a message becomes ────────────────────────────────────────────────

@pytest.mark.parametrize("message, line", [
    ({"msg_type": 1, "content": "你好"}, "你好"),
    ({"msg_type": 3, "content": "[图片]"}, "[图片]"),
    ({"msg_type": 34, "content": "[语音] 我想要样品"}, "[语音] 我想要样品"),
    ({"msg_type": 34, "content": "[语音]"}, "[语音]（没转出文字）"),
    ({"msg_type": 43, "content": "[视频]"}, "[视频]"),
    ({"msg_type": 47, "content": "[表情]"}, "[表情]"),
    ({"msg_type": 42, "special": {"kind": "card", "title": "小美的同事"}}, "[名片] 小美的同事"),
    ({"msg_type": 48, "special": {"kind": "location", "title": "某某咖啡"}}, "[位置] 某某咖啡"),
    ({"msg_type": 49, "special": {"kind": "appmsg", "type": "5", "title": "新品测评"}}, "[链接] 新品测评"),
    ({"msg_type": 49, "special": {"kind": "appmsg", "type": "33", "title": "店铺"}}, "[小程序] 店铺"),
    ({"msg_type": 49, "special": {"kind": "appmsg", "type": "19", "title": "群聊的聊天记录"}}, "[聊天记录] 群聊的聊天记录"),
    ({"msg_type": 49, "special": {"kind": "appmsg", "type": "2000", "title": "微信转账"}}, "[转账]"),
    ({"msg_type": 49, "special": {"kind": "appmsg", "type": "2001", "title": "恭喜发财"}}, "[红包]"),
    ({"msg_type": 49, "special": {"kind": "appmsg", "type": "999", "title": "新东西"}}, "[消息] 新东西"),
    ({"msg_type": 49, "file_name": "合同.pdf", "content": "[文件]"}, "[文件] 合同.pdf"),
    ({"msg_type": 49, "content": "这个颜色还有吗", "quoted_content": "色卡", "quoted_server_id": "7"}, "这个颜色还有吗"),
    ({"msg_type": 10000, "special": {"kind": "system", "title": "对方撤回了一条消息"}}, "对方撤回了一条消息"),
    ({"msg_type": 50, "content": "[消息类型:50]"}, "[消息类型:50]"),
], ids=lambda v: str(v)[:40] if isinstance(v, dict) else None)
def test_every_kind_of_message_has_a_line(message, line):
    assert T.message_text(message) == line


def test_a_reply_says_whose_line_it_quotes():
    reply = {"msg_type": 49, "content": "好的", "quoted_content": "色卡", "quoted_server_id": "7"}
    assert T.quote_of({**reply, "quoted_sender_id": "wxid_bot"}, "wxid_bot", CREATOR)["from"] == "us"
    assert T.quote_of({**reply, "quoted_sender_id": CREATOR}, "wxid_bot", CREATOR)["from"] == "creator"
    assert T.quote_of({**reply, "quoted_sender_id": "wxid_x"}, "wxid_bot", CREATOR) == {
        "text": "色卡", "from": "other", "server_id": "7"}
    assert T.quote_of({"msg_type": 1, "content": "hi"}, "wxid_bot", CREATOR) is None


def test_the_backend_reads_titles_out_of_the_wechat_xml():
    link = "<msg><appmsg><title>新品测评</title><type>5</type><url>https://x</url></appmsg></msg>"
    mini = "<msg><appmsg><title>店铺</title><type>33</type></appmsg></msg>"
    card = '<msg username="wxid_c" nickname="小美的同事" alias="" />'
    place = '<msg><location x="31.2" y="121.5" label="上海市浦东新区" poiname="某某咖啡" /></msg>'
    recall = ('<sysmsg type="revokemsg"><revokemsg><session>wxid_creator01</session><msgid>1</msgid>'
              '<newmsgid>99</newmsgid><replacemsg><![CDATA["小美" 撤回了一条消息]]></replacemsg></revokemsg></sysmsg>')
    assert _special_info(link, 49) == {"kind": "appmsg", "type": "5", "title": "新品测评"}
    assert _special_info(mini, 49)["type"] == "33"
    assert _special_info(card, 42) == {"kind": "card", "title": "小美的同事"}
    assert _special_info(place, 48) == {"kind": "location", "title": "某某咖啡"}
    assert _special_info(recall, 10000) == {"kind": "system", "title": '"小美" 撤回了一条消息'}
    assert _special_info("对方撤回了一条消息", 10000) == {"kind": "system", "title": "对方撤回了一条消息"}
    assert _special_info("<msg><broken", 49) == {} and _special_info("plain", 42) == {}


class FakeClient:
    _config = {"myWxid": "wxid_bot"}

    def __init__(self, messages):
        self.messages = messages

    def get_messages(self, talker, limit=50):
        return self.messages

    def resolve_nickname(self, wxid):
        return {"wxid_bot": "机器人号", CREATOR: "小美"}.get(wxid, wxid)

    def get_display_names(self, usernames):
        return {u: self.resolve_nickname(u) for u in usernames}


def test_the_backend_hands_the_title_on_with_the_message():
    now = int(time.time())
    raw = [{"sender_username": CREATOR, "local_type": 49, "create_time": now, "local_id": "a1",
            "message_content": "<msg><appmsg><title>新品测评</title><type>5</type></appmsg></msg>"}]
    backend = WcdbBackend(bot_display_name="机器人", groups=["*"],
                          config=BotConfig(private_chat_enabled=True))
    backend._client = FakeClient(raw)
    backend._chat_is_group[CREATOR] = False
    backend._running = True
    backend.set_private_filter(lambda username: True)
    seen = []
    backend._dispatch_message = lambda m, callback: seen.append(m)
    backend._poll_group("小美", CREATOR, Mock())
    for m in backend._burst.flush_all():
        seen.append(m)
    [message] = seen
    assert message["special"] == {"kind": "appmsg", "type": "5", "title": "新品测评"}
    assert T.message_text(message) == "[链接] 新品测评"
    assert backend.own_wxid() == "wxid_bot" and backend.db_readable() is True
    backend._db_failures = 3
    assert backend.db_readable() is False


def test_the_window_is_not_asked_while_a_send_holds_it():
    backend = WcdbBackend(bot_display_name="机器人", groups=["*"], config=BotConfig())
    backend._window = SimpleNamespace(find_hwnd=lambda wake=True: 123)
    assert backend.wechat_window_found() is True
    with backend._window_lock:
        assert backend.wechat_window_found() is None
    backend._window = SimpleNamespace(find_hwnd=Mock(side_effect=OSError("gone")))
    assert backend.wechat_window_found() is False


# ── a creator's message ───────────────────────────────────────────────────

def test_a_listed_creators_message_becomes_one_event(edge, clock):
    first = msg(clock, "你好")
    assert edge.handle(first) is True and edge.handle(dict(first)) is True        # the same message twice
    assert [e["text"] for e in events(edge, P.CREATOR_MESSAGE)] == ["你好"]
    assert edge.handle(msg(clock, "别人", chat="wxid_stranger")) is False
    assert edge.handle({**msg(clock, "群里"), "is_group": True}) is False
    assert edge.handle(msg(clock, "十分钟前的", age=600)) is True                     # history: not forwarded
    assert len(events(edge, P.CREATOR_MESSAGE)) == 1


def test_pictures_are_copied_here_and_files_stay_where_wechat_put_them(edge, clock, wechat, tmp_path):
    picture = tmp_path / "wechat-cache.jpg"
    picture.write_bytes(b"jpg" * 100)
    document = tmp_path / "WeChat Files" / "合同.pdf"
    document.parent.mkdir()
    document.write_bytes(b"%PDF" * 10)
    shown, filed, lost, broken = (msg(clock, "[图片]", 3), msg(clock, "[文件]", 49, file_name="合同.pdf"),
                                  msg(clock, "[图片]", 3), msg(clock, "[图片]", 3))
    wechat.found = {shown["message_id"]: (str(picture), "图片-1.jpg", ""),
                    filed["message_id"]: (str(document), "合同.pdf", ""),
                    lost["message_id"]: ("", "图片-1.jpg", "图片在电脑上没找到")}
    for m in (shown, filed, lost, broken):                # broken: the lookup itself fails
        edge.handle(m)
    a, b, c, d = events(edge, P.CREATOR_MESSAGE)
    copy = Path(edge.event_file(a["id"], 1))
    assert copy.read_bytes() == picture.read_bytes() and copy != picture and str(tmp_path / "edge") in str(copy)
    assert edge.event_file(b["id"], 1) == str(document) and b["files"][0]["kind"] == "file"
    assert c["files"] == [{"n": 1, "name": "图片-1.jpg", "missing": "图片在电脑上没找到"}]
    assert d["files"] == [{"n": 1, "name": "图片-1.jpg", "missing": "附件出错了，没取到"}]


# ── read back, or an operator ─────────────────────────────────────────────

def test_our_own_lines_are_read_back_and_anything_else_is_an_operator(edge, clock, wechat):
    sent(edge, "s1", "样品这周五前寄出～")
    edge.tick()
    clock.now += 3
    edge.on_own_message(msg(clock, "样品这周五前寄出～", own=True))
    [seen] = events(edge, P.SEND_SEEN)
    assert seen["send_id"] == "s1" and seen["delay_s"] == 3
    edge.on_own_message(msg(clock, "样品这周五前寄出～", own=True))                # typed again by a person
    edge.on_own_message(msg(clock, "我是运营，我来", own=True))
    assert [e["text"] for e in events(edge, P.OPERATOR_MESSAGE)] == ["样品这周五前寄出～", "我是运营，我来"]
    edge.on_own_message(msg(clock, "上周的话", own=True, age=600))                    # history
    edge.on_own_message(msg(clock, "x", own=True, chat="wxid_stranger"))             # not listed
    edge.on_own_message(msg(clock, "", 10000, own=True, special={"kind": "system", "title": "你撤回了一条消息"}))
    assert len(events(edge, P.OPERATOR_MESSAGE)) == 2
    assert [e["text"] for e in events(edge, P.CHAT_NOTICE)] == ["你撤回了一条消息"]


def test_files_we_sent_are_ours_and_a_picture_by_hand_is_an_operators(edge, clock, wechat, tmp_path):
    for name in ("a.pdf", "b.pdf"):
        (tmp_path / name).write_bytes(b"x")
    up_a = edge.upload("a.pdf", (tmp_path / "a.pdf").open("rb").read, 1)
    up_b = edge.upload("b.pdf", (tmp_path / "b.pdf").open("rb").read, 1)
    sent(edge, "only-file", uploads=[up_a["upload_id"]])
    sent(edge, "text-and-file", "附件在下面", uploads=[up_b["upload_id"]])
    edge.tick()
    edge.tick()
    assert wechat.files == [(CREATOR, "a.pdf"), (CREATOR, "b.pdf")] and wechat.texts == [(CREATOR, "附件在下面")]
    clock.now += 5
    edge.on_own_message(msg(clock, "[文件]", 49, own=True))                          # a.pdf shows up
    edge.on_own_message(msg(clock, "附件在下面", own=True))
    edge.on_own_message(msg(clock, "[文件]", 49, own=True))                          # b.pdf: ours, no event
    assert sorted(e["send_id"] for e in events(edge, P.SEND_SEEN)) == ["only-file", "text-and-file"]
    edge.on_own_message(msg(clock, "[图片]", 3, own=True))                           # a picture by hand
    assert [e["text"] for e in events(edge, P.OPERATOR_MESSAGE)] == ["[图片]"]
    (tmp_path / "c.pdf").write_bytes(b"x")
    up_c = edge.upload("c.pdf", (tmp_path / "c.pdf").open("rb").read, 1)
    sent(edge, "late-file", uploads=[up_c["upload_id"]])
    edge.tick()
    clock.now += 601                                                                   # past the file window
    edge.on_own_message(msg(clock, "[图片]", 3, own=True))
    assert len(events(edge, P.OPERATOR_MESSAGE)) == 2 and "late-file" not in [
        e["send_id"] for e in events(edge, P.SEND_SEEN)]


def test_a_line_read_back_late_is_still_ours(edge, clock):
    sent(edge, "s1", "收到～")
    edge.tick()
    clock.now += 1801
    edge.tick()
    assert [e["send_id"] for e in events(edge, P.SEND_UNSEEN)] == ["s1"]
    clock.now += 3600
    edge.on_own_message(msg(clock, "收到～", own=True))
    assert [e["send_id"] for e in events(edge, P.SEND_SEEN)] == ["s1"] and not events(edge, P.OPERATOR_MESSAGE)
    edge.tick()
    assert len(events(edge, P.SEND_UNSEEN)) == 1                                      # said once


def test_a_line_read_back_before_its_send_returns_is_ours(edge, clock, wechat, tmp_path):
    """WeChat writes a sent line at once and the backend can read it back before the send
    returns (Windows test, 2026-10-26): each part is noted before it goes, and send_seen
    follows the send_result."""
    def text_seen_at_once(chat, text):
        wechat.texts.append((chat, text))
        edge.on_own_message(msg(clock, text, own=True))
        return True

    def file_seen_at_once(chat, path):
        wechat.files.append((chat, Path(path).name))
        edge.on_own_message(msg(clock, "[文件]", 49, own=True))
        return True

    edge._send_text, edge._send_file = text_seen_at_once, file_seen_at_once
    ups = []
    for name in ("brief.pdf", "sizes.pdf"):
        (tmp_path / name).write_bytes(b"x")
        ups.append(edge.upload(name, (tmp_path / name).open("rb").read, 1)["upload_id"])
    sent(edge, "text-and-file", "brief 在下面～", uploads=[ups[0]])
    sent(edge, "file-only", uploads=[ups[1]])
    edge.tick()
    edge.tick()
    assert len(wechat.texts) == 1 and len(wechat.files) == 2 and not events(edge, P.OPERATOR_MESSAGE)
    told = [(e["type"], e["send_id"]) for e in events(edge, P.SEND_RESULT, P.SEND_SEEN)]
    assert told == [(P.SEND_RESULT, "text-and-file"), (P.SEND_SEEN, "text-and-file"),
                    (P.SEND_RESULT, "file-only"), (P.SEND_SEEN, "file-only")]
    assert [e["delay_s"] for e in events(edge, P.SEND_SEEN)] == [0, 0]
    clock.now += 1801
    edge.tick()
    assert not events(edge, P.SEND_UNSEEN)


def test_a_part_that_did_not_go_out_is_not_ours(edge, clock, wechat, tmp_path):
    """Noted before it goes, taken back when it fails: the same words, or a file, showing up
    later were put there by a person; a failed send is never reported seen."""
    wechat.text_ok = False
    sent(edge, "refused", "这句没发出去")
    edge.tick()

    def broken(chat, text):
        raise RuntimeError("the window went away")

    edge._send_text = broken
    sent(edge, "broken", "这句出错了")
    edge.tick()
    for name in ("a.pdf", "b.pdf"):
        (tmp_path / name).write_bytes(b"x")
    ups = [edge.upload(n, (tmp_path / n).open("rb").read, 1)["upload_id"] for n in ("a.pdf", "b.pdf")]
    goes = iter([True, False])
    edge._send_file = lambda chat, path: next(goes)                                  # a.pdf goes, b.pdf not
    sent(edge, "half", uploads=ups)
    edge.tick()
    assert [e["status"] for e in events(edge, P.SEND_RESULT)] == [P.FAILED] * 3
    clock.now += 2
    for line in ("这句没发出去", "这句出错了"):
        edge.on_own_message(msg(clock, line, own=True))
    edge.on_own_message(msg(clock, "[文件]", 49, own=True))                          # a.pdf: ours
    edge.on_own_message(msg(clock, "[文件]", 49, own=True))                          # one more: a person's
    operators = events(edge, P.OPERATOR_MESSAGE)
    assert [e["text"] for e in operators[:2]] == ["这句没发出去", "这句出错了"] and len(operators) == 3
    assert not events(edge, P.SEND_SEEN)


def test_after_a_restart_only_the_earlier_runs_unseen_sends_count(tmp_path, clock, wechat):
    store = EdgeStore(":memory:")
    first = make_edge(tmp_path, clock, wechat, store=store)
    sent(first, "seen", "好的呀")
    sent(first, "pending", "稍等哈")
    first.tick()
    first.tick()
    first.on_own_message(msg(clock, "好的呀", own=True))
    first.stop()
    clock.now += 60
    again = make_edge(tmp_path, clock, wechat, store=store)
    again.on_own_message(msg(clock, "稍等哈", own=True))
    assert [e["send_id"] for e in events(again, P.SEND_SEEN)] == ["seen", "pending"]
    again.on_own_message(msg(clock, "好的呀", own=True))                              # seen already: a person
    assert [e["text"] for e in events(again, P.OPERATOR_MESSAGE)] == ["好的呀"]
    again.stop()


# ── sending ───────────────────────────────────────────────────────────────

def test_the_pace_the_hourly_limit_and_an_offline_wechat_hold_the_queue(tmp_path, clock, wechat):
    edge = make_edge(tmp_path, clock, wechat, edge_send_min_gap_sec=10, edge_send_per_hour=2)
    for n in range(3):
        sent(edge, f"s{n}", f"第{n}条", ttl=7200)
    edge.tick()
    edge.tick()
    assert [t for _, t in wechat.texts] == ["第0条"]
    clock.now += 10
    edge.tick()
    clock.now += 10
    edge.tick()
    assert [t for _, t in wechat.texts] == ["第0条", "第1条"]                        # two an hour
    clock.now += 3600
    wechat.online = False
    edge._last_status_check = 0
    edge.tick()
    assert len(wechat.texts) == 2 and edge.health()["wechat"] == P.OFFLINE           # held, not failed
    wechat.online = True
    edge._last_status_check = 0
    edge.tick()
    assert [t for _, t in wechat.texts] == ["第0条", "第1条", "第2条"]
    edge.stop()


def test_a_send_taken_by_one_round_is_not_sent_by_another(edge, clock, wechat):
    sent(edge, "once", "只发一次")
    [record] = edge.store.sends_in((P.QUEUED,))
    edge._deliver(record)
    edge._deliver(record)                                 # a second round with the same old snapshot
    assert wechat.texts == [(CREATOR, "只发一次")] and len(events(edge, P.SEND_RESULT)) == 1
    assert edge.store.mark_unseen("once", clock.now) and not edge.store.mark_unseen("once", clock.now)


def test_a_send_too_late_expires(edge, clock, wechat):
    wechat.online = False
    edge._last_status_check = 0
    sent(edge, "late", "过时了", ttl=60)
    clock.now += 61
    edge.tick()
    [result] = events(edge, P.SEND_RESULT)
    assert result["status"] == P.EXPIRED and wechat.texts == []


def test_what_failed_is_said_and_failures_in_a_row_mark_the_state(edge, clock, wechat, tmp_path):
    (tmp_path / "brief.pdf").write_bytes(b"x")
    up = edge.upload("brief.pdf", (tmp_path / "brief.pdf").open("rb").read, 1)
    wechat.file_ok = False
    sent(edge, "half", "附件在下面", uploads=[up["upload_id"]])
    edge.tick()
    wechat.text_ok = False
    for n in range(2):
        sent(edge, f"f{n}", "在吗")
        edge.tick()
    results = {e["send_id"]: e for e in events(edge, P.SEND_RESULT)}
    assert results["half"]["error"] == "文字已发出；文件「brief.pdf」没发出去"
    assert results["f0"]["error"] == "文字没发出去"
    [failing] = [e for e in events(edge, P.EDGE_STATUS) if e["send"] == P.FAILING]
    assert "连续 3 条没发出去" in failing["detail"] and edge.health()["send"] == P.FAILING
    wechat.text_ok = True
    sent(edge, "ok", "好了")
    edge.tick()
    assert events(edge, P.EDGE_STATUS)[-1]["send"] == P.OK


def test_a_file_that_vanished_or_a_backend_without_files_fails_cleanly(tmp_path, clock, wechat):
    edge = make_edge(tmp_path, clock, wechat)
    (tmp_path / "a.pdf").write_bytes(b"x")
    up = edge.upload("a.pdf", (tmp_path / "a.pdf").open("rb").read, 1)
    os.remove(edge.store.upload(up["upload_id"])["path"])
    sent(edge, "gone", uploads=[up["upload_id"]])
    edge.tick()
    assert events(edge, P.SEND_RESULT)[-1]["error"] == "文件「a.pdf」在 Windows 上找不到了"
    edge._send_file = None
    up2 = edge.upload("b.pdf", io.BytesIO(b"y").read, 1)
    sent(edge, "nofiles", uploads=[up2["upload_id"]])
    edge.tick()
    assert events(edge, P.SEND_RESULT)[-1]["error"] == "这个微信后端不能发文件"
    edge.stop()


# ── state ─────────────────────────────────────────────────────────────────

def test_the_state_follows_wechat_and_the_database(edge, wechat):
    [startup] = events(edge, P.EDGE_STATUS)
    assert startup["detail"] == "Windows 边缘启动了"
    wechat.online = None                                   # busy sending: unknown, nothing changes
    edge._last_status_check = 0
    edge.tick()
    assert len(events(edge, P.EDGE_STATUS)) == 1
    wechat.readable = False
    edge._last_status_check = 0
    edge.tick()
    assert events(edge, P.EDGE_STATUS)[-1]["db"] == P.UNREADABLE
    wechat.readable = True
    edge._last_status_check = 0
    edge.tick()
    assert events(edge, P.EDGE_STATUS)[-1]["db"] == P.OK and events(edge, P.EDGE_STATUS)[-1]["detail"] == "恢复正常"


# ── keeping nothing for long ──────────────────────────────────────────────

def test_events_files_uploads_and_send_texts_go_after_their_time(edge, clock, wechat, tmp_path):
    picture = tmp_path / "wechat-cache.jpg"
    picture.write_bytes(b"jpg")
    document = tmp_path / "合同.pdf"
    document.write_bytes(b"pdf")
    shown, filed = msg(clock, "[图片]", 3), msg(clock, "[文件]", 49, file_name="合同.pdf")
    wechat.found = {shown["message_id"]: (str(picture), "图片-1.jpg", ""), filed["message_id"]: (str(document), "合同.pdf", "")}
    edge.handle(shown)
    edge.handle(filed)
    a, b = events(edge, P.CREATOR_MESSAGE)
    copy = edge.event_file(a["id"], 1)
    edge.ack(a["id"])
    (tmp_path / "x.pdf").write_bytes(b"x")
    unused = edge.upload("x.pdf", (tmp_path / "x.pdf").open("rb").read, 1)
    sent(edge, "s1", "收件人：小美，电话 13800001234")
    edge.tick()
    clock.now += 2 * DAY
    edge.cleanup()
    assert edge.store.upload(unused["upload_id"]) is None                            # unused: a day
    assert os.path.exists(copy)
    clock.now += 6 * DAY
    edge.cleanup()
    assert edge.event_file(a["id"], 1) is None and not os.path.exists(copy)          # acked + 7 days
    assert os.path.exists(document) and picture.exists()                             # WeChat's own: kept
    assert edge.event_file(b["id"], 1) == str(document)                              # not acked yet
    record = edge.store.send("s1")
    assert record["text"] == "" and record["cleared"] == 1                           # no creator text kept
    assert edge.send({"send_id": "s1", "chat_id": CREATOR, "text": "收件人：小美，电话 13800001234",
                      "uploads": [], "ttl_sec": 1800})["status"] == P.SENT            # a retry still finds it
    clock.now += 30 * DAY
    edge.cleanup()
    assert edge.event_file(b["id"], 1) is None and edge.store.send("s1") is None     # unacked: 30 days
    assert document.exists()                                                         # still WeChat's file


# ── the route step, the settings, the bot ─────────────────────────────────

class Trace:
    def __init__(self):
        self.redacted, self.reasons = None, []

    def redact(self, content):
        self.redacted = content

    def reason(self, code, **info):
        self.reasons.append(code)


def test_the_route_step_swallows_listed_chats_even_when_the_edge_breaks(edge, clock):
    trace = Trace()
    stop = S.edge(RouteContext(router=SimpleNamespace(_edge=edge), msg=msg(clock, "你好"), trace=trace))
    assert stop == Stop(None) and trace.redacted == "[已登记达人的私聊，内容不记录]" and trace.reasons == ["prm_edge"]
    edge.handle = Mock(side_effect=RuntimeError("db locked"))
    assert S.edge(RouteContext(router=SimpleNamespace(_edge=edge), msg=msg(clock, "在吗"), trace=Trace())) == Stop(None)
    assert S.edge(RouteContext(router=SimpleNamespace(_edge=edge), msg=msg(clock, "x", chat="wxid_stranger"),
                               trace=Trace())) is None
    assert S.edge(RouteContext(router=SimpleNamespace(), msg=msg(clock, "x"), trace=Trace())) is None


class EdgeRouteTests(StepCase):
    def test_a_creators_message_is_neither_stored_nor_answered(self):
        router = self.router()
        redacted = []
        self.trace.redact = redacted.append                  # the real traces have it; the spy does not
        clock, wechat = Clock(time.time()), WeChat(Path("."))
        edge = EdgeService(SimpleNamespace(prm_edge_token="e" * 40, edge_send_min_gap_sec=0), store=EdgeStore(":memory:"),
                           clock=clock, send_text=wechat.send_text, private_names=lambda: {})
        edge.put_parties(1, [CREATOR])
        router._edge = edge
        message = msg(clock, "你好，我想申请寄样")
        assert router.handle(message) is None
        assert self.stored(router, message["message_id"]) == 0
        assert [e["text"] for e in edge.store.events_after(0, 10)] == ["你好，我想申请寄样"]
        assert redacted == ["[已登记达人的私聊，内容不记录]"]


def test_the_edge_settings_are_checked():
    good = dict(prm_edge="on", claude_relay="on", private_chat_enabled=True, prm_edge_token="e" * 40,
                claude_relay_token="r" * 40, claude_relay_prefix="cc", admin_wxid="wxid_admin")
    _validate_config(good)
    for change, words in ((dict(claude_relay="off"), "needs CLAUDE_RELAY=on"),
                          (dict(private_chat_enabled=False), "needs PRIVATE_CHAT_ENABLED=true"),
                          (dict(prm_edge_token="short"), "PRM_EDGE_TOKEN of at least 32"),
                          (dict(prm_edge_token="r" * 40), "must differ from CLAUDE_RELAY_TOKEN"),
                          (dict(prm_copilot="on"), "cannot both be on"),
                          (dict(prm_edge="maybe"), "PRM_EDGE must be off or on"),
                          (dict(edge_send_per_hour=0), "EDGE_SEND_PER_HOUR must be between"),
                          (dict(edge_keep_days=31), "EDGE_KEEP_DAYS must be between")):
        with pytest.raises(RuntimeError) as e:
            _validate_config({**good, **change})
        assert words in str(e.value)
    config = BotConfig()
    assert (config.prm_edge, config.edge_db, config.edge_send_min_gap_sec, config.edge_send_per_hour,
            config.edge_seen_text_sec, config.edge_seen_file_sec, config.edge_keep_days) == (
        "off", "data/edge.db", 3, 60, 1800, 600, 7)


def test_the_bot_wires_the_edge_into_the_backend_and_the_relay(tmp_path, monkeypatch):
    from src.bot import Bot
    monkeypatch.chdir(tmp_path)
    config = SimpleNamespace(prm_edge="on", prm_edge_token="e" * 40, edge_db=str(tmp_path / "edge.db"),
                             admin_wxid="wxid_admin", claude_relay_wxids=[])
    backend = SimpleNamespace(send_text=Mock(return_value=True), send_file=Mock(return_value=True),
                              own_wxid=lambda: "wxid_bot", wechat_window_found=lambda: True, db_readable=lambda: True,
                              refresh_private_chats_soon=Mock(), set_private_filter=Mock(), set_own_message_hook=Mock())
    relay = SimpleNamespace(attach_edge=Mock())
    router = SimpleNamespace()
    assert Bot._start_edge(SimpleNamespace(prm_edge="off"), router, backend, relay) is None
    assert Bot._start_edge(config, router, backend, None) is None
    edge = Bot._start_edge(config, router, backend, relay)
    try:
        assert router._edge is edge
        backend.set_private_filter.assert_called_once_with(edge.is_party_chat)
        backend.set_own_message_hook.assert_called_once_with(edge.on_own_message)
        relay.attach_edge.assert_called_once_with(edge, "e" * 40)
        assert edge.health()["wechat"] == P.ONLINE
    finally:
        edge.stop()
