"""Mac 端控制台和 Windows 状态（计划第 5 步）：/api/edge、每页顶上的提醒条、Mac 端隐藏「微信连接」页、
审批卡上通知已进「待审批」时不再让运营复制。

浏览器部分沿用达人审批页的测试服务（临时配置和数据库），/api/edge 用 Playwright 拦截，模拟 Windows 的各种状态。
"""

import json

import pytest

from test_edge_backend import CREATOR
from test_influencer_console import console, page  # noqa: F401  （夹具）

from src.edge import protocol as P
from src.edge.client import EdgeClient
from src.edge.fake import FakeEdge
from src.wechat.edge_backend import EdgeBackend, edge_problems


# ---- 状态和提醒 ----

def test_problems_are_listed_in_the_order_to_fix_them():
    assert edge_problems({"online": False, "last_contact_sec_ago": None})[0].startswith("连不上 Windows")
    assert "10 分钟前" in edge_problems({"online": False, "last_contact_sec_ago": 600})[0]
    assert edge_problems({"online": False, "wechat": P.OFFLINE}) == edge_problems({"online": False}), "连不上时只说这一条"
    found = edge_problems({"online": True, "wechat": P.OFFLINE, "db": P.UNREADABLE, "send": P.FAILING,
                           "detail": "找不到聊天窗口"})
    assert [p[:11] for p in found] == ["Windows 上的微", "Windows 读不了", "Windows 连续几"]
    assert "找不到聊天窗口" in found[2]
    assert edge_problems({"online": True, "wechat": P.ONLINE, "db": P.OK, "send": P.OK}) == []


def test_the_backend_reports_what_windows_says(tmp_path):
    with FakeEdge(auto_send=False) as fake:
        backend = EdgeBackend(client=EdgeClient(fake.url, fake.token), state_path=str(tmp_path / "s.json"))
        backend.add_parties_source(lambda: [CREATOR])
        assert backend.status()["problems"][0].startswith("连不上 Windows"), "还没联系上"
        backend._maybe_check_health()
        backend._sync_parties()
        backend._mark_online(True)
        fake.set_status(wechat="offline", detail="找不到微信主窗口")
        backend._handle_events(fake.events(0, 0, 50), lambda msg: None)
        status = backend.status()
    assert (status["online"], status["wechat"], status["parties"]) == (True, "offline", 1)
    assert status["problems"] == [edge_problems({"online": True, "wechat": "offline"})[0]]


def test_the_console_api_reads_the_running_backend(tmp_path, monkeypatch):
    from src.web import server

    monkeypatch.setenv("WECHAT_BACKEND", "edge")
    assert server._edge_state() == {"mode": "edge", "running": False, "problems": []}, "机器人没在运行"
    backend = EdgeBackend(client=EdgeClient("http://127.0.0.1:9", "x" * 32), state_path=str(tmp_path / "s.json"))
    server.register_edge_client(backend)
    try:
        state = server._edge_state()
    finally:
        server.register_edge_client(None)
    assert state["mode"] == "edge" and state["online"] is False and state["problems"]
    monkeypatch.setenv("WECHAT_BACKEND", "wcdb")
    assert server._edge_state()["mode"] == ""


def test_an_approval_card_knows_its_notice_went_to_review(tmp_path, monkeypatch):
    from test_influencer_acceptance import FakeERP, FakeXHS
    from test_influencer_store import NEXT_YEAR, make_packet

    from src.influencer import console as influencer_console
    from src.influencer.handler import InfluencerBusinessHandler
    from src.influencer.store import SOURCE_BOT, InfluencerStore

    store = InfluencerStore(str(tmp_path / "messages.db"))
    for packet_id in ("GATE-A", "GATE-B"):
        store.add_approval(make_packet(packet_id, chat_id="wxid_mei", xhs_account="xhs_mei", expires_at=NEXT_YEAR),
                           source=SOURCE_BOT)
    monkeypatch.setattr(influencer_console, "InfluencerBusinessHandler",
                        lambda s: InfluencerBusinessHandler(s, erp_client=FakeERP(), xhs_client=FakeXHS()))
    plain = influencer_console.act_on_approval(store, {"packet_id": "GATE-A", "action": "reject"})
    assert plain["packet"]["notice"] and plain["packet"]["notice_queued"] is False, "没开 PRM：照旧复制"
    queued = []
    influencer_console.register_decision_hook(lambda packet, approved, result: queued.append(packet.packet_id))
    try:
        result = influencer_console.act_on_approval(store, {"packet_id": "GATE-B", "action": "reject"})
    finally:
        influencer_console._decision_hooks.clear()
    assert queued == ["GATE-B"] and result["notice_queued"] and result["packet"]["notice_queued"] is True


# ---- 浏览器 ----

def fake_edge_api(page, **state):
    page.route("**/api/edge", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(state, ensure_ascii=False)))


def test_windows_trouble_shows_on_every_page(page, console):  # noqa: F811
    fake_edge_api(page, mode="edge", running=True, online=False, problems=["连不上 Windows：达人的消息收不到"])
    for hash_ in ("#/status", "#/influencer/approvals"):
        page.goto(f"{console.base}/{hash_}")
        page.get_by_text("Windows 那边有问题").wait_for()
        assert page.get_by_text("连不上 Windows：达人的消息收不到").is_visible()


def test_the_mac_side_has_no_wechat_page_and_no_alert_when_all_is_well(page, console):  # noqa: F811
    fake_edge_api(page, mode="edge", running=True, online=True, parties=3, problems=[])
    page.goto(f"{console.base}/#/status")
    page.get_by_role("button", name="达人审批").wait_for()
    page.wait_for_timeout(500)
    assert page.get_by_role("button", name="微信连接").count() == 0
    assert page.get_by_text("Windows 那边有问题").count() == 0


def test_the_windows_side_keeps_its_wechat_page(page, console):  # noqa: F811
    page.goto(f"{console.base}/#/status")
    page.get_by_role("button", name="微信连接").wait_for()
