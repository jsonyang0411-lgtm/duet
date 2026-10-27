"""Mac 端的 edge 后端（src/wechat/edge_backend.py）：通过 Windows 的微信边缘收发（D18）。

用同事写的假 Windows 边缘（src/edge/fake.py）跑真实的 HTTP：名单推送、达人消息交给路由、
回复存成草稿不直接发（D19）、send_text 才发、读到哪接着读、Windows 掉线时退避重试。
"""

import threading
import time

import pytest

from src.edge.client import EdgeClient
from src.edge.fake import FakeEdge
from src.wechat.edge_backend import EdgeBackend, EdgeState, event_to_message

CREATOR = "wxid_creator01"


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def fake():
    with FakeEdge(auto_send=False) as edge:
        yield edge


class Running:
    """在线程里跑 backend.start(callback)，记下路由收到的消息和存下的草稿。"""

    def __init__(self, fake, tmp_path, *, reply="好的老师", parties=(CREATOR,), name="state.json"):
        self.seen, self.drafts, self.events = [], [], []
        self.parties = list(parties)
        self.backend = EdgeBackend(
            client=EdgeClient(fake.url, fake.token), state_path=str(tmp_path / name),
            files_dir=str(tmp_path / "files"), events_wait=0.5,
        )
        self.backend.add_parties_source(lambda: self.parties)
        self.backend.set_reply_sink(lambda chat_id, text, msg: self.drafts.append((chat_id, text)))
        self.backend.add_event_hook(self.events.append)
        self.reply = reply
        self.thread = threading.Thread(target=self.backend.start, args=(self._route,), daemon=True)

    def _route(self, msg):
        self.seen.append(msg)
        return self.reply

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.backend.stop()
        self.thread.join(timeout=5)


def test_the_list_is_pushed_and_a_creator_message_reaches_the_router(fake, tmp_path):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties), "名单推给了 Windows"
        fake.creator_says(CREATOR, "你好，我想申请寄样")
        assert wait_for(lambda: run.seen)
    msg = run.seen[0]
    assert (msg["chat_id"], msg["content"], msg["is_group"]) == (CREATOR, "你好，我想申请寄样", False)


def test_a_reply_becomes_a_draft_and_is_not_sent(fake, tmp_path):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        fake.creator_says(CREATOR, "你好")
        assert wait_for(lambda: run.drafts)
    assert run.drafts == [(CREATOR, "好的老师")]
    assert fake.sent() == [], "回复要人审（D19），不能自动发"


def test_send_text_asks_windows_to_send(fake, tmp_path):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        assert run.backend.send_text(CREATOR, "审核过的回复", send_id="pk-1") is True
        assert run.backend.send_text(CREATOR, "审核过的回复", send_id="pk-1") is True, "同一个编号重复提交是安全的"
        fake.complete("pk-1")
        fake.seen("pk-1")
        assert wait_for(lambda: {e["type"] for e in run.events} >= {"send_result", "send_seen"})
    assert fake.sent() == [(CREATOR, "审核过的回复", [])]


def test_a_send_to_someone_not_on_the_list_is_refused(fake, tmp_path):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        assert run.backend.send_text("wxid_stranger", "你好") is False


def test_other_events_go_to_the_hooks(fake, tmp_path):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        fake.operator_says(CREATOR, "我来回")
        fake.notice(CREATOR, "对方撤回了一条消息")
        fake.set_status(wechat="offline", detail="找不到微信主窗口")
        assert wait_for(lambda: {"operator_message", "chat_notice"} <= {e["type"] for e in run.events}
                        and run.backend.edge_status.get("wechat") == "offline")
    assert run.seen == [], "运营亲手回复、撤回提示不当成达人消息交给路由"


def test_the_list_follows_changes(fake, tmp_path):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: fake._parties == {CREATOR})
        run.parties.append("wxid_creator02")
        assert wait_for(lambda: fake._parties == {CREATOR, "wxid_creator02"})
        run.parties.remove(CREATOR)
        assert wait_for(lambda: fake._parties == {"wxid_creator02"})


def test_after_a_restart_it_continues_where_it_stopped(fake, tmp_path):
    with Running(fake, tmp_path) as first:
        assert wait_for(lambda: CREATOR in fake._parties)
        fake.creator_says(CREATOR, "第一条")
        assert wait_for(lambda: first.seen)
    fake.creator_says(CREATOR, "第二条")
    with Running(fake, tmp_path) as second:
        assert wait_for(lambda: second.seen)
        time.sleep(0.6)
    assert [m["content"] for m in second.seen] == ["第二条"], "处理过的不再处理"
    state = EdgeState(tmp_path / "state.json")
    assert state.cursor == fake._events[-1]["id"] and fake._acked == state.cursor


def test_a_lost_state_file_catches_up_with_the_windows_list_version(fake, tmp_path):
    with Running(fake, tmp_path, name="a.json"):
        assert wait_for(lambda: fake._parties_version == 1)
    with Running(fake, tmp_path, parties=(CREATOR, "wxid_creator02"), name="b.json"):
        assert wait_for(lambda: fake._parties == {CREATOR, "wxid_creator02"})
    assert fake._parties_version == 2, "Windows 回 409 时从它的版本往后接着推"


def test_attachments_are_downloaded_before_the_router_sees_the_message(fake, tmp_path):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        fake.creator_says(CREATOR, "[图片]", msg_type=3, files=[("图片-1.jpg", b"\xff\xd8picture")])
        assert wait_for(lambda: run.seen)
    [path] = run.seen[0]["edge_files"]
    assert open(path, "rb").read() == b"\xff\xd8picture"
    assert str(tmp_path / "files") in path


def test_windows_being_down_is_retried_not_fatal(tmp_path):
    slept = []
    backend = EdgeBackend(
        client=EdgeClient("http://127.0.0.1:9", "x" * 32), state_path=str(tmp_path / "s.json"),
        sleep=lambda s: (slept.append(s), backend.stop() if len(slept) >= 3 else None),
        events_wait=0.1,
    )
    backend.start(lambda msg: None)
    assert slept == [2, 4, 8] and backend.online is False


def test_a_quote_and_the_message_fields():
    msg = event_to_message({"id": 7, "type": "creator_message", "chat_id": CREATOR, "message_id": "m1",
                            "msg_type": 1, "text": "这个颜色还有吗", "at": 1761462000,
                            "quote": {"text": "这是新款的色卡～", "from": "us"}}, [])
    assert msg["quoted_content"] == "这是新款的色卡～" and msg["is_reply_to_bot"] is True
    assert (msg["message_id"], msg["timestamp"], msg["edge_event_id"]) == ("m1", 1761462000, 7)


# ---- 配置和接线 ----

def test_the_config_checks_the_mac_side_settings():
    from src.config import _validate_config

    with pytest.raises(RuntimeError, match="EDGE_URL"):
        _validate_config({"wechat_backend": "edge", "edge_url": "", "prm_edge_token": "x" * 32})
    with pytest.raises(RuntimeError, match="PRM_EDGE_TOKEN"):
        _validate_config({"wechat_backend": "edge", "edge_url": "http://10.0.0.2:8790", "prm_edge_token": "short"})


def test_the_bot_wires_the_list_and_drafts_to_the_influencer_module(tmp_path, fake):
    from src.bot import Bot
    from src.influencer.store import INBOX_DRAFT, SOURCE_CONSOLE, InfluencerStore

    store = InfluencerStore(str(tmp_path / "messages.db"))
    store.add_contacts([CREATOR, "wxid_off"], source=SOURCE_CONSOLE)
    store.save_contact("wxid_off", enabled=False, source=SOURCE_CONSOLE)
    backend = EdgeBackend(client=EdgeClient(fake.url, fake.token), state_path=str(tmp_path / "s.json"))
    Bot._wire_edge_backend(backend, store)
    assert backend.parties() == [CREATOR], "停用的达人不推"
    backend._reply_sink(CREATOR, "好的老师", {})
    [item] = store.list_inbox()
    assert (item["kind"], item["content"]) == (INBOX_DRAFT, "好的老师")
