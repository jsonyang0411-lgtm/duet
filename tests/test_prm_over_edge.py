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


def test_the_operator_typing_our_words_a_minute_after_the_read_back_takes_the_chat_over(mac, fake):
    """win review F4 (a)，边缘模式：我们发的那句读回来了（send_seen）；一分钟后运营在手机上打了一模一样的话，
    Windows 报 operator_message（另一行，另一个 message_id）：运营亲手回复，切人工接管。以前 Mac 在 120 秒内
    按文字当成同一行又读了一次，吞掉了。"""
    _, packet = drafted(mac, fake)
    mac.prm.decide(packet["id"], "send")
    mac.prm.send_approved()
    assert wait_for(lambda: fake.sends)                    # (发送线程也可能先发了)
    [send] = fake.sends
    fake.complete(send["send_id"])
    fake.seen(send["send_id"])
    assert wait_for(lambda: mac.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed")
    mac.clock.now += 60
    fake.operator_says(CREATOR, send["text"], message_id="row-typed-by-hand")
    assert wait_for(lambda: mac.prm.store.deal(1)["mode"] == "human"), "一模一样的话也是运营亲手发的"
    assert [e["kind"] for e in mac.prm.store.events(1, kinds=("operator_message", "takeover"))] == [
        "takeover", "operator_message"]


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
    assert send["text"] == "收到，我确认一下马上回您"
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


def test_a_recall_windows_reports_puts_the_waiting_draft_before_a_person(mac, fake):
    """Windows 把私聊里的系统提示报成 chat_notice（不当达人的话）：达人撤回了，等着的草稿改成人审、
    合作单写提醒；运营撤回只记一笔、不接管；新好友的提示照旧不管（审查 win F1）。"""
    _, packet = drafted(mac, fake)
    fake.notice(CREATOR, "对方撤回了一条消息")
    assert wait_for(lambda: mac.prm.store.packet(packet["id"])["needs_attention"])
    held = mac.prm.store.packet(packet["id"])
    assert held["status"] == "pending" and held["reason"].startswith("达人撤回了一条消息")
    assert mac.prm.store.deal(1)["attention"].startswith("达人撤回了一条消息")
    fake.notice(CREATOR, "以上是打招呼的消息")
    fake.notice(CREATOR, "你撤回了一条消息")
    assert wait_for(lambda: len(mac.prm.store.events(1, kinds=("recall",))) == 2)
    assert [e["detail"]["by"] for e in mac.prm.store.events(1, kinds=("recall",))] == ["operator", "creator"]
    assert mac.prm.store.deal(1)["mode"] == "copilot" and len(mac.relay.store.list_jobs()) == 1
    assert [e["kind"] for e in mac.prm.store.events(1)] == ["recall", "recall"]


def test_a_recall_windows_found_rewritten_in_place_is_handled_the_same(mac, fake):
    """微信原地改写的撤回（2026-10-28）：Windows 报的 chat_notice 多带 recalled_message_id（被撤回那条的编号），
    老的 Mac 不认识这个字段也照样按文字处理。"""
    _, packet = drafted(mac, fake)
    [message] = [e for e in fake.events_list if e["type"] == "creator_message"]
    event = fake.notice(CREATOR, '"小美" 撤回了一条消息', recalled_message_id=message["message_id"])
    assert event["recalled_message_id"] == message["message_id"]
    assert wait_for(lambda: mac.prm.store.packet(packet["id"])["needs_attention"])
    assert mac.prm.store.packet(packet["id"])["reason"].startswith("达人撤回了一条消息")
    assert wait_for(lambda: fake.acked_upto >= event["id"])


def test_a_recall_handed_on_after_a_windows_restart_goes_by_when_it_was_written(mac, fake):
    """Windows 重启后补交的 chat_notice 带 written_at：按写的时间算晚了多久，不按转过来的时间。
    13 小时前的撤回超过 PRM_CATCHUP_HOURS，只当历史，等着的草稿不动；同一个事件流里后面那条刚写的照常处理。"""
    _, packet = drafted(mac, fake)
    fake.notice(CREATOR, "对方撤回了一条消息", written_at=int(mac.clock.now) - 13 * 3600)
    fake.notice(CREATOR, "你撤回了一条消息")
    assert wait_for(lambda: any(e["detail"]["by"] == "operator" for e in mac.prm.store.events(1, kinds=("recall",))))
    assert [e["detail"]["by"] for e in mac.prm.store.events(1, kinds=("recall",))] == ["operator"]
    assert not mac.prm.store.packet(packet["id"])["needs_attention"]
    assert mac.prm.store.packet(packet["id"])["status"] == "pending"


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


def test_a_file_windows_could_not_send_marks_the_draft_failed_though_its_text_went(mac, fake):
    """2026-10-28 审查：素材文件在 Windows 发送失败，草稿原来一直显示「已发出」。现在改成没发出去，写明文字发了。"""
    import io

    from test_prm import draft as prm_draft
    fake.creator_says(CREATOR, "我是小美，手机 13812345678，资料发我看看")
    assert wait_for(lambda: mac.relay.store.list_jobs())
    mac.clock.now += SETTLE_SEC + 1
    job = mac.relay.next_job(0, ("prm",))
    assert mac.relay.outfile(job["id"], "合作说明.pdf", io.BytesIO(b"%PDF brief").read, 10)
    d = prm_draft(attachments_to_send=["assets/合作说明.pdf"])
    assert mac.relay.finish(job["id"], {"ok": True, "text": json.dumps(d, ensure_ascii=False), "duration_s": 3})
    mac.relay.tick()
    packet = mac.prm.store.packet_for_job(job["id"])
    assert mac.prm.decide(packet["id"], "send") == {"ok": True, "status": "approved"}, packet
    assert mac.prm.send_approved() == 1, mac.prm.store.packet(packet["id"])
    text_send, file_send = fake.sends
    assert file_send["uploads"] == ["合作说明.pdf"]
    fake.complete(text_send["send_id"])
    fake.seen(text_send["send_id"])
    assert wait_for(lambda: mac.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed")
    fake.complete(file_send["send_id"], P.FAILED, "文件发送超时")
    assert wait_for(lambda: mac.prm.store.packet(packet["id"])["status"] == "failed")
    error = mac.prm.store.packet(packet["id"])["error"]
    assert "文件发送超时" in error and "文字已经发出去了" in error


def test_the_sample_order_sync_runs_on_the_mac_side_too(mac, fake):
    """边缘模式（PRM 在 Mac 上）：聚水潭寄样单状态照样定时交给本机的 worker（不跑模型）；有了运单号照控制台的路填进
    合作单，发货通知一律等人审，审过以后经 Windows 发出、换成真实的话；已签收照控制台的路标上，开专属佣金的链条派出
    第一个商品的任务。"""
    from test_prm_sample_sync import info, verified_order

    from platform_fakes import DY_ITEM, JST_ORDER
    from src.prm import platforms
    from src.prm.service import SAMPLE_SYNC_CHAT, SAMPLE_SYNC_FIRST_SEC

    mac.prm._sample_sync_sec = 3600
    order = verified_order(mac.prm)

    def a_round(answer):
        job_id = mac.prm.sweep_sample_status()
        assert job_id is not None
        job = mac.relay.next_job(0, ("prm",))
        assert job["id"] == job_id and job["meta"] == {"op": "sample_status", "orders": [JST_ORDER], "mine": True}
        result = platforms.sample_result([JST_ORDER], orders={JST_ORDER: answer})
        assert mac.relay.finish(job_id, {"ok": True, "text": json.dumps(result, ensure_ascii=False), "duration_s": 1})
        mac.relay.tick()

    mac.clock.now += SAMPLE_SYNC_FIRST_SEC + 1
    a_round(info("SENT", "SF1234567890"))
    assert mac.relay.store.get(mac.relay.store.list_jobs(1)[0]["id"])["chat_id"] == SAMPLE_SYNC_CHAT
    deal = mac.prm.store.deal(order["deal_id"])
    assert deal["stage"] == "shipped" and mac.prm.store.latest_shipment(deal["id"])["waybill"] == "SF1234567890"
    [notice] = [p for p in mac.prm.store.packets(("pending",)) if p["kind"] == "shipping_notice"]
    assert fake.sends == [], "发货通知等运营审"
    assert mac.prm.decide(notice["id"], "send")["ok"]
    mac.prm.send_approved()
    [send] = fake.sends
    assert send["chat_id"] == CREATOR and "SF1234567890" in send["text"], "审过以后经 Windows 发出"
    mac.clock.now += 3601
    a_round(info("SIGNED", "SF1234567890"))
    assert mac.prm.store.deal(order["deal_id"])["stage"] == "delivered"
    step = mac.relay.next_job(0, ("prm",))
    assert step is not None and "达人已经签收了寄样单" in step["meta"]["system_note"] and DY_ITEM in step["meta"]["system_note"]

