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


# ── 同事代理复现的问题（2026-10-27）────────────────────────────────────────

def test_an_attachment_windows_no_longer_has_does_not_stop_the_events(fake, tmp_path):
    """Windows 回了错误（文件没了）的附件不要了，消息照常交给路由，后面的消息也照常来，不报连不上。"""
    fake.put_parties(1, [CREATOR])
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        event = fake.creator_says(CREATOR, "[图片]", msg_type=3, files=[("图片-1.jpg", b"\xff\xd8picture")])
        fake._files.pop((event["id"], 1))                                  # Windows 那边文件没了
        fake.creator_says(CREATOR, "在吗")
        assert wait_for(lambda: len(run.seen) == 2)
        assert run.backend.online and run.backend.status()["problems"] == []
    assert run.seen[0]["edge_files"] == [] and run.seen[1]["content"] == "在吗"


def test_a_download_cut_off_is_retried_and_then_given_up_without_going_offline(fake, tmp_path, monkeypatch):
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        calls = []

        def broken(event_id, n, dest):
            calls.append(event_id)
            raise ConnectionResetError("网络断了")

        monkeypatch.setattr(run.backend.client, "download", broken)
        monkeypatch.setattr("src.wechat.edge_backend.BACKOFF_SEC", (0.01,) * 6)
        fake.creator_says(CREATOR, "[图片]", msg_type=3, files=[("图片-1.jpg", b"\xff\xd8picture")])
        assert wait_for(lambda: run.seen, timeout=8)
        assert run.backend.online, "附件下载失败不算连不上 Windows"
    assert len(calls) == 3 and run.seen[0]["edge_files"] == []           # 试 3 次，不要这个附件，消息照常走


def test_events_of_a_rebuilt_windows_event_store_are_read_from_the_start(fake, tmp_path):
    """Windows 的 edge.db 重建了（编号从 1 重新开始），Mac 记着的位置比它大：要从头读，不能静默跳过。"""
    state = EdgeState(tmp_path / "state.json")
    state.cursor = 500
    state.save()
    fake.put_parties(1, [CREATOR])
    fake.creator_says(CREATOR, "重建以后的第一条")
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: run.seen)
        assert any("事件库重建" in p for p in run.backend.status()["problems"])
    assert run.seen[0]["content"] == "重建以后的第一条"
    assert EdgeState(tmp_path / "state.json").cursor == fake.events_list[-1]["id"] < 500


def test_events_read_but_not_yet_acknowledged_are_acknowledged_not_read_again(fake, tmp_path):
    fake.put_parties(1, [CREATOR])
    fake.creator_says(CREATOR, "已经处理过的")
    last = fake.events_list[-1]["id"]
    state = EdgeState(tmp_path / "state.json")
    state.cursor = last                                                    # 处理完、存好了，确认前停了
    state.save()
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: fake.acked_upto == last)
        assert wait_for(lambda: run.backend.online)
        assert run.backend.status()["problems"] == []
    assert run.seen == [] and EdgeState(tmp_path / "state.json").cursor == last


def test_a_send_that_fails_after_a_restart_still_reaches_the_failure_hook(fake, tmp_path):
    """重启以前发的回复，之后 Windows 报失败：内存里没有了，用写盘的摘要（不是原文）交给钩子。"""
    import json as _json

    from src.wechat.edge_backend import text_digest
    with Running(fake, tmp_path) as run:
        assert wait_for(lambda: CREATOR in fake._parties)
        assert run.backend.send_text(CREATOR, "地址收到啦 13900001111", send_id="pk-1")
    saved = (tmp_path / "edge_sends.json").read_text(encoding="utf-8")
    assert "13900001111" not in saved and "地址" not in saved              # 原文不写盘
    assert _json.loads(saved)["pk-1"]["sha256"] == text_digest("地址收到啦 13900001111")
    failures = []
    with Running(fake, tmp_path) as again:                                 # 重启：内存里的发送记录没了
        again.backend.set_send_failure_hook(lambda chat, text, why, **more: failures.append((chat, text, why, more)))
        fake.complete("pk-1", "failed", "微信窗口找不到")
        assert wait_for(lambda: failures)
    chat, text, why, more = failures[0]
    assert (chat, text) == (CREATOR, "") and "微信窗口找不到" in why
    assert more["digest"] == text_digest("地址收到啦 13900001111") and more["sent_at"]
