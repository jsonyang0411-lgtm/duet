"""Mac 端的 PRM：同事的副驾驶（src/prm）挂在 edge 后端上（计划第 3 步）。

用假 Windows 边缘（src/edge/fake.py）跑真实的 HTTP，中转和 PRM 都在内存里，worker 由测试代替
（领任务、交回草稿）。看的是：登记的达人推给 Windows；达人消息变成起草任务（号码换成占位符）；
批准后通过 Windows 发出、换回真实号码；读回确认草稿；运营亲手回复切人工接管；Windows 没发出去的，
草稿改成没发出去；达人发的图片带进起草任务。
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from test_prm import CREATOR, TOKEN, Clock, draft

from src.bot import Bot
from src.edge import protocol as P
from src.edge.client import EdgeClient
from src.edge.fake import FakeEdge
from src.prm.service import SETTLE_SEC
from src.relay import ClaudeRelay
from src.relay.store import RelayStore
from src.wechat.edge_backend import EdgeBackend


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def mac_config(tmp_path, **overrides):
    values = dict(
        wechat_backend="edge", claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1",
        claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid="", prm_copilot="on", prm_db=str(tmp_path / "prm.db"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class Mac:
    """Mac 上的业务服务：edge 后端、中转、PRM，按 Bot.run 的接法接起来。"""

    def __init__(self, fake, tmp_path):
        self.clock = Clock(time.time())
        config = mac_config(tmp_path)
        self.backend = EdgeBackend(client=EdgeClient(fake.url, fake.token), state_path=str(tmp_path / "edge.json"),
                                   files_dir=str(tmp_path / "edge_files"), events_wait=0.3)
        self.relay = ClaudeRelay(config, store=RelayStore(":memory:"), clock=self.clock,
                                 files_dir=tmp_path / "relay", send=self.backend.send_text)
        router = SimpleNamespace(_store=None)
        self.prm = Bot._start_prm(config, router, self.backend, self.relay)
        self.prm._clock = self.clock                    # 和中转用同一个时钟，测试才能拨时间
        Bot._wire_edge_prm(self.backend, self.prm)
        # 路由的 prm 步骤：登记达人的消息交给 PRM，不回复
        self.thread = threading.Thread(target=self.backend.start, args=(lambda msg: self.prm.handle(msg) and None,),
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        from src.web.server import register_prm
        register_prm(None)
        self.backend.stop()
        self.thread.join(timeout=5)
        self.prm.stop()
        self.relay.stop()

    def worker_drafts(self, d):
        """本机 worker 领到任务、交回草稿（真实的 worker 在沙箱里跑 claude，见 tools/claude_relay/worker.py）。"""
        self.clock.now += SETTLE_SEC + 1
        job = self.relay.next_job(0, ("prm",))
        assert self.relay.finish(job["id"], {"ok": True, "text": json.dumps(d, ensure_ascii=False), "duration_s": 3})
        self.relay.tick()
        return job, self.prm.store.packet_for_job(job["id"])


@pytest.fixture
def fake():
    with FakeEdge(auto_send=False) as edge:
        yield edge


@pytest.fixture
def mac(fake, tmp_path):
    with Mac(fake, tmp_path) as m:
        m.prm.register(CREATOR, "小美", "测试达人")
        assert wait_for(lambda: CREATOR in fake._parties), "PRM 登记的达人推给了 Windows"
        yield m


def drafted(mac, fake, text="我是小美，手机 13812345678"):
    fake.creator_says(CREATOR, text)
    assert wait_for(lambda: mac.relay.store.list_jobs())
    return mac.worker_drafts(draft())


def test_a_creator_message_becomes_a_draft_job_with_numbers_masked(mac, fake):
    job, packet = drafted(mac, fake)
    assert "13812345678" not in job["text"] and "〈手机" in job["text"], "给 Claude 的只有占位符"
    assert job["meta"]["party_id"] == 1
    assert packet["status"] == "pending" and fake.sends == [], "草稿等运营审核，不自动发"


def test_an_approved_draft_goes_out_through_windows_and_its_read_back_confirms_it(mac, fake):
    _, packet = drafted(mac, fake)
    mac.prm.decide(packet["id"], "send")
    mac.prm.send_approved()
    [send] = fake.sends
    assert (send["chat_id"], send["text"]) == (CREATOR, "确认一下：小美，手机13812345678。"), "发出前换回真实号码"
    assert mac.prm.store.packet(packet["id"])["outcome"] == "sent_unconfirmed"
    fake.complete(send["send_id"])
    fake.seen(send["send_id"])
    assert wait_for(lambda: mac.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed")
    assert mac.prm.store.deal(1)["mode"] == "copilot", "我们自己发的读回来了，不算运营接手"


def test_an_operator_replying_by_hand_takes_the_chat_over(mac, fake):
    _, packet = drafted(mac, fake)
    fake.operator_says(CREATOR, "确认一下：小美，手机13812345678。")   # 和草稿一字不差，也是运营亲手发的
    assert wait_for(lambda: mac.prm.store.deal(1)["mode"] == "human")
    assert mac.prm.store.packet(packet["id"])["status"] == "superseded"


def test_a_send_windows_could_not_make_marks_the_draft_failed(mac, fake):
    _, packet = drafted(mac, fake)
    mac.prm.decide(packet["id"], "send")
    mac.prm.send_approved()
    [send] = fake.sends
    fake.complete(send["send_id"], P.FAILED, "找不到聊天窗口")
    assert wait_for(lambda: mac.prm.store.packet(packet["id"])["status"] == "failed")
    assert "找不到聊天窗口" in mac.prm.store.packet(packet["id"])["error"]


def test_a_holding_reply_goes_through_windows_too(mac, fake):
    drafted(mac, fake)
    mac.clock.now += 11 * 60
    mac.prm.send_holding_replies()                  # 发送线程也可能先发了：只发一次
    assert wait_for(lambda: fake.sends)
    [send] = fake.sends
    assert send["text"] == "收到，我确认一下马上回你"
    fake.complete(send["send_id"])
    fake.seen(send["send_id"])
    time.sleep(0.5)
    assert mac.prm.store.deal(1)["mode"] == "copilot", "「稍等」读回来了也不算运营接手"


def test_a_picture_from_windows_goes_with_the_draft_job(mac, fake):
    fake.creator_says(CREATOR, "[图片]", msg_type=3, files=[("图片-1.jpg", b"\xff\xd8picture")])
    assert wait_for(lambda: mac.relay.store.list_jobs())
    [job] = mac.relay.store.list_jobs()
    files = mac.relay.store.files(job["id"])
    assert len(files) == 1 and open(files[0]["path"], "rb").read() == b"\xff\xd8picture"


def test_a_file_from_the_draft_is_uploaded_to_windows(mac, fake, tmp_path):
    brief = tmp_path / "合作说明.pdf"
    brief.write_bytes(b"%PDF brief")
    assert mac.backend.send_file(CREATOR, str(brief)) is True
    [send] = fake.sends
    assert send["uploads"] == ["合作说明.pdf"] and send["text"] == ""


# ---- 配置 ----

def test_the_mac_relay_only_listens_on_this_mac():
    from src.config import _validate_config

    base = dict(wechat_backend="edge", edge_url="http://10.0.0.2:8790", prm_edge_token="x" * 32,
                claude_relay="on", claude_relay_token=TOKEN, claude_relay_wxids=[], admin_wxid="",
                claude_relay_prefix="cc")
    with pytest.raises(RuntimeError, match="CLAUDE_RELAY_HOST=127.0.0.1"):
        _validate_config({**base, "claude_relay_host": "0.0.0.0"})
    _validate_config({**base, "claude_relay_host": "127.0.0.1"})   # Mac 端不用登记 cc 用户


def test_a_failed_send_takes_back_the_newest_of_two_same_lines(tmp_path):
    """同一句话发了两次：第二次没发出去，第一次的读回还要对到第一条草稿上（不能把它的登记删掉）。"""
    from test_prm import Bot as PrmBot

    side = PrmBot(Clock(), tmp_path)
    try:
        side.prm.register(CREATOR, "小美")
        packets = []
        for _ in range(2):
            side.prm.handle(side.msg("在吗"))
            job = side.worker_takes()
            side.finish(job["id"], draft(slots={}, reply_draft="好的，收到～", message_kind="greeting"))
            packet = side.prm.store.packet_for_job(job["id"])
            side.prm.decide(packet["id"], "send")
            side.prm.send_approved()
            packets.append(packet["id"])
        side.prm.on_send_failed(CREATOR, "好的，收到～", "Windows 没发出去")
        first, second = (side.prm.store.packet(i) for i in packets)
        assert (first["status"], second["status"]) == ("sent", "failed")
        side.prm.on_own_message({"chat_id": CREATOR, "content": "好的，收到～", "msg_type": 1, "is_group": False,
                                 "timestamp": side.clock.now, "edge_seen": True})
        assert side.prm.store.packet(packets[0])["outcome"] == "sent_confirmed"
        assert side.prm.store.deal(1)["mode"] == "copilot"
    finally:
        side.prm.stop()
        side.relay.stop()


def test_messages_windows_kept_while_the_mac_was_away_are_not_history(tmp_path):
    """Mac 离线超过 5 分钟：Windows 排队的达人消息、运营亲手回复照常处理（edge 事件不按年龄当历史丢掉）。
    PRM_CATCHUP_HOURS（默认 12 小时）以内本机微信后端补交的旧消息也一样起草（草稿一律先审，
    tests/test_prm_catchup.py）；PRM_CATCHUP_HOURS=0 时本机的旧消息照旧当历史，edge 的照常处理。"""
    from test_prm import Bot as PrmBot

    side = PrmBot(Clock(), tmp_path)
    try:
        side.prm.register(CREATOR, "小美")
        an_hour_ago = side.clock.now - 3600
        assert side.prm.handle(side.msg("一小时前发的", ts=an_hour_ago)) is True
        [late] = side.relay.store.list_jobs()                              # 没在读时收到的：起草，一律先审
        assert json.loads(late["meta"])["late"] == {"from": an_hour_ago}
        assert side.prm.handle(side.msg("我到家啦", ts=an_hour_ago, edge_event_id=7)) is True
        job = side.worker_takes()
        assert job is not None and "我到家啦" in job["text"]
        side.prm.on_own_message({"chat_id": CREATOR, "content": "我来回", "msg_type": 1, "is_group": False,
                                 "timestamp": an_hour_ago, "edge_operator": True, "edge_event_id": 8})
        assert side.prm.store.deal(1)["mode"] == "human"                    # 运营亲手回复：接管
        assert side.prm.handle(side.msg("那什么时候寄", ts=an_hour_ago, edge_event_id=9)) is True
        kinds = [e["kind"] for e in side.prm.store.events(1, limit=20)]
        assert "operator_message" in kinds and "creator_message" in kinds   # 接管期间的也记下了
    finally:
        side.prm.stop()
        side.relay.stop()


def test_a_send_that_failed_after_a_restart_is_matched_by_its_digest(tmp_path):
    """重启以前发的回复，过了很久 Windows 才报失败：内存里的原文和登记都没了，按摘要对上草稿。"""
    import hashlib

    from test_prm import Bot as PrmBot

    side = PrmBot(Clock(), tmp_path)
    try:
        side.prm.register(CREATOR, "小美")
        side.prm.handle(side.msg("在吗"))
        job = side.worker_takes()
        side.finish(job["id"], draft(slots={}, reply_draft="好的，收到～", message_kind="greeting"))
        packet = side.prm.store.packet_for_job(job["id"])
        side.prm.decide(packet["id"], "send")
        side.prm.send_approved()
        sent_at = side.clock.now
        side.prm._sent_lines.clear()                                        # 重启：内存里的发送登记没了
        side.clock.now += 3 * 3600                                          # 过了 OWN_MATCH_SEC 很久
        digest = hashlib.sha256("好的，收到～".encode("utf-8")).hexdigest()
        side.prm.on_send_failed(CREATOR, "", "Windows 没发出去", digest=hashlib.sha256(b"x").hexdigest(),
                                sent_at=sent_at)
        assert side.prm.store.packet(packet["id"])["status"] == "sent"      # 摘要对不上的不动
        side.prm.on_send_failed(CREATOR, "", "在 Windows 上排队太久，过期没发", digest=digest, sent_at=sent_at)
        failed = side.prm.store.packet(packet["id"])
        assert failed["status"] == "failed" and "过期" in failed["error"]
    finally:
        side.prm.stop()
        side.relay.stop()
