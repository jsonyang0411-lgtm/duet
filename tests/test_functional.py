"""Functional/E2E tests — verify real HTTP + browser behavior.

These tests start the web server, then verify the full stack (React frontend
+ Python backend) works correctly via real HTTP requests and headless Chromium.

Requirements:  pip install playwright && python -m playwright install chromium
"""

import http.client
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────

SERVER_HOST = "127.0.0.1"
SERVER_PORT = None  # discovered at fixture time
BASE_URL = None


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port):
    """Start the web server in a daemon thread. Returns when ready."""
    import traceback
    from src.web.server import _run_server

    start_errors = []

    def _run_catching():
        try:
            _run_server(SERVER_HOST, port)
        except Exception:
            start_errors.append(traceback.format_exc())

    t = threading.Thread(target=_run_catching, daemon=True)
    t.start()

    for _ in range(50):
        if start_errors:
            raise RuntimeError(f"Server thread crashed:\n{start_errors[0]}")
        try:
            conn = http.client.HTTPConnection(SERVER_HOST, port, timeout=0.5)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return t
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"Server did not start within 5 seconds on port {port}")


def _api_get(path):
    """GET request to test server, bypassing system proxy."""
    conn = http.client.HTTPConnection(SERVER_HOST, SERVER_PORT, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _api_post(path, body_dict=None, raw_body=None, timeout=5):
    """POST request to test server, bypassing system proxy."""
    conn = http.client.HTTPConnection(SERVER_HOST, SERVER_PORT, timeout=timeout)
    body = raw_body if raw_body is not None else json.dumps(body_dict or {}).encode()
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    try:
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data
    except (TimeoutError, ConnectionError, OSError):
        conn.close()
        raise


@pytest.fixture(scope="module")
def server():
    """Module-level fixture: start the web server once for all tests."""
    global SERVER_PORT, BASE_URL

    ui_dist = Path(__file__).resolve().parent.parent / "ui" / "dist" / "index.html"
    if not ui_dist.exists():
        pytest.skip("UI not built. Run: cd ui && npm run build")

    SERVER_PORT = _find_free_port()
    BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    env_existed = env_path.exists()
    if not env_existed:
        env_path.write_text(
            "WECHAT_GROUPS=*\n"
            "ONBOARDING_DONE=true\n"  # skip onboarding → dashboard loads directly
        )

    t = _start_server(SERVER_PORT)
    yield
    if not env_existed and env_path.exists():
        env_path.unlink()


# ── API-level functional tests ──────────────────────────────────────────────

class TestDiagnoseApi:
    """Verify /api/onboarding/diagnose returns correct structure."""

    def test_diagnose_returns_200(self, server):
        status, data = _api_get("/api/onboarding/diagnose")
        assert status == 200
        assert data["ok"] is True
        diag = data["diagnostics"]
        for key in ("python", "requirements", "wechat", "env", "db"):
            assert key in diag, f"Missing diagnostics key: {key}"
            assert "ok" in diag[key]
            assert "value" in diag[key]

    def test_diagnose_python_ok(self, server):
        _, data = _api_get("/api/onboarding/diagnose")
        assert data["diagnostics"]["python"]["ok"] is True
        assert "Python 3" in data["diagnostics"]["python"]["value"]


class TestPostAllowlist:
    """Only the gateway's endpoints take a POST: the chat bot's AI sandbox is gone."""

    def test_the_sandbox_is_gone(self, server):
        conn = http.client.HTTPConnection(SERVER_HOST, SERVER_PORT, timeout=5)
        conn.request("POST", "/api/sandbox/test", body=b"{}", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        conn.close()
        assert resp.status == 405

    def test_unknown_post_path_returns_405(self, server):
        conn = http.client.HTTPConnection(SERVER_HOST, SERVER_PORT, timeout=5)
        conn.request("POST", "/api/unknown-path", body=b"{}",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        conn.close()
        assert resp.status == 405


# ── Playwright browser-level functional tests ───────────────────────────────

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


@pytest.fixture(scope="module")
def browser():
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("Playwright not installed. Run: pip install playwright")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, server):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    yield page
    context.close()


class TestPageLoads:
    def test_index_html_loads(self, page):
        page.goto(BASE_URL, timeout=10000)
        assert page.title() is not None


class TestThemeToggle:
    # The 2026-10 redesign (明日方舟风) is light by default with a dark variant.
    def test_default_theme_is_light(self, page):
        page.goto(BASE_URL, timeout=10000)
        page.wait_for_timeout(1000)
        html_classes = page.evaluate("document.documentElement.className")
        assert "dark" not in html_classes, f"Expected no 'dark' class, got: {html_classes}"

    def test_toggle_switches_to_dark_and_is_remembered(self, page):
        page.goto(BASE_URL, timeout=10000)
        page.wait_for_timeout(1000)
        toggle_btn = page.locator('button[title*="切换"]')
        assert toggle_btn.count() > 0
        toggle_btn.first.click()
        page.wait_for_timeout(300)
        assert "dark" in page.evaluate("document.documentElement.className")
        assert page.evaluate("localStorage.getItem('theme')") == "dark"
        page.reload()
        page.wait_for_timeout(1000)
        assert "dark" in page.evaluate("document.documentElement.className")


class TestSidebarNavigation:
    """The company console: the bot as a relay to Claude Code, plus the PRM copilot's two pages."""

    def test_sidebar_has_the_agent_pages_only(self, page):
        page.goto(BASE_URL, timeout=10000)
        page.wait_for_timeout(1000)
        nav = page.locator("nav.rail").inner_text()
        for label in ("运行状态", "中转代理", "待审批", "合作单", "微信连接", "运行日志"):
            assert label in nav
        for gone in ("系统配置", "功能模块", "高级开关", "回复记录", "群友昵称", "群聊待办"):
            assert gone not in nav

    def test_old_links_land_on_the_status_page(self, page):
        page.goto(f"{BASE_URL}/#/modules/sticky", timeout=10000)
        page.wait_for_timeout(1000)
        text = page.inner_text("body")
        assert "最近的中转任务" in text and "STICKY_MENTION_TTL_SEC" not in text


class TestAgentConsolePages:
    @pytest.fixture
    def relay_with_jobs(self):
        """A running relay (not listening) with one finished and one queued job."""
        import time
        from types import SimpleNamespace
        from src.relay.service import ClaudeRelay
        from src.relay.store import RelayStore
        from src.web import server as web_server

        cfg = SimpleNamespace(
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
            claude_relay_max_chars=1500, admin_wxid="wxid_me",
        )
        relay = ClaudeRelay(cfg, store=RelayStore(":memory:"))
        for text in ("cc 只回复：收到", "cc @webot 跑一下测试"):
            relay.handle({"chat_id": "wxid_me", "sender_id": "wxid_me", "content": text,
                          "is_group": False, "timestamp": time.time()})
        first = relay.next_job(0)
        relay.finish(first["id"], {"ok": True, "text": "收到", "duration_s": 9.5})
        web_server.register_relay(relay)
        yield relay
        web_server.register_relay(None)

    def test_status_page_shows_the_relay(self, page):
        page.goto(BASE_URL, timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("运行状态", "中转 ·", "Mac worker", "最近的中转任务"):
            assert expected in text

    def test_relay_page_lists_jobs_and_cancels_a_queued_one(self, page, relay_with_jobs):
        page.goto(f"{BASE_URL}/#/relay", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("中转代理", "现在的状态", "Mac 端怎么接", "relay_url", "#1", "#2", "跑一下测试", "完成", "排队中"):
            assert expected in text
        page.locator("tr", has_text="跑一下测试").locator("button:has-text('取消')").click()
        page.wait_for_timeout(300)
        page.locator("[role=dialog] button:has-text('取消任务')").click()
        page.wait_for_timeout(1500)
        assert relay_with_jobs.store.get(2)["status"] == "cancelled"
        assert "已取消" in page.inner_text("body")

    def test_relay_page_says_how_the_mac_is_reached(self, page, relay_with_jobs, monkeypatch):
        """cross-network.md: the Mac's network on the state card, and what each address is for."""
        from src.relay import link

        monkeypatch.setattr(link, "this_pc_addresses", lambda: [{"ip": "192.168.1.20", "kind": "lan"},
                                                                {"ip": "100.101.102.103", "kind": "tailscale"}])
        relay_with_jobs.note_worker(None, address="100.70.1.2")
        page.goto(f"{BASE_URL}/#/relay", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("在线 · 经 Tailscale\n100.70.1.2", "http://192.168.1.20:8790", "同一个局域网时用",
                         "http://100.101.102.103:8790", "Tailscale，在不在同一个网络都能用，推荐", "100.64.0.0/10"):
            assert expected in text
        assert "两台都装 Tailscale" not in text                              # there is a Tailscale address
        monkeypatch.setattr(link, "this_pc_addresses", lambda: [{"ip": "192.168.1.20", "kind": "lan"}])
        page.locator("button:has-text('刷新')").first.click()
        page.wait_for_timeout(1000)
        assert "两台都装 Tailscale" in page.inner_text("body")

    def test_relay_settings_generate_a_token_and_show_it_once(self, page):
        page.goto(f"{BASE_URL}/#/relay", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("Claude 中转", "打开中转", "只做中转", "CLAUDE_RELAY_ONLY", "CLAUDE_RELAY_TOKEN", "ADMIN_WXID", "CLAUDE_RELAY_PORT"):
            assert expected in text
        page.locator("button:has-text('随机生成')").click()
        page.wait_for_timeout(300)
        text = page.inner_text("body")
        assert "新 token" in text and "项修改未保存" in text
        page.locator("button:has-text('放弃修改')").click()
        page.wait_for_timeout(300)
        assert "新 token" not in page.inner_text("body")

    def test_wechat_page_has_private_chats_and_the_ocr_switch(self, page):
        page.goto(f"{BASE_URL}/#/wechat", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("微信连接", "微信后端", "读取私聊", "读取哪些群", "从不在群里说话", "OCR_FALLBACK",
                         "数据库读不了时用 OCR", "macOS / Windows"):
            assert expected in text
        # a main setting: the bottom save bar
        page.locator(".toggle-row", has_text="读取私聊").locator('button[role="switch"]').click()
        page.wait_for_timeout(300)
        assert "项修改未保存" in page.inner_text("body")
        page.locator("button:has-text('放弃修改')").first.click()
        page.wait_for_timeout(300)
        assert "项修改未保存" not in page.inner_text("body")
        # an advanced switch: its own save bar
        page.locator(".adv-field", has_text="数据库读不了时用 OCR").locator('button[role="switch"]').click()
        page.wait_for_timeout(300)
        assert "项修改未保存" in page.inner_text("body")
        page.locator("button:has-text('放弃修改')").first.click()
        page.wait_for_timeout(300)
        assert "项修改未保存" not in page.inner_text("body")


class TestPrmPages:
    """待审批 and 合作单 against a running PRM service (in memory: nothing is written to data/)."""

    @pytest.fixture
    def prm(self):
        import json as _json
        import time as _time
        from types import SimpleNamespace
        from src.prm.service import SETTLE_SEC, PrmService
        from src.prm.store import PrmStore
        from src.relay.service import ClaudeRelay
        from src.relay.store import RelayStore
        from src.web import server as web_server

        class Clock:
            now = _time.time()

            def __call__(self):
                return self.now

        clock = Clock()
        cfg = SimpleNamespace(
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
            claude_relay_max_chars=1500, admin_wxid="wxid_admin", prm_copilot="on", prm_db=":memory:",
        )
        sent = []
        relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock)
        service = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock,
                             send=lambda c, t: sent.append((c, t)) or True, send_file=lambda c, p: True)
        service.register("wxid_creator01", "小美", "618 防晒")
        for text, reply in (("收件人小美 13812345678", "确认一下：小美，手机〈手机1〉"), ("在吗", "在的，有什么可以帮你？")):
            service.handle({"chat_id": "wxid_creator01", "sender_id": "wxid_creator01", "content": text,
                            "msg_type": 1, "is_group": False, "timestamp": clock.now})
            clock.now += SETTLE_SEC + 1
            job = relay.next_job(0, ("prm",))
            draft = {"intent": "要样品", "slots": {"收件人": "小美"}, "stage_proposal": "address",
                     "message_kind": "address_confirm", "reply_draft": reply, "attachments_to_send": [],
                     "needs_attention": False, "reason": "收件信息齐了"}
            relay.finish(job["id"], {"ok": True, "text": _json.dumps(draft, ensure_ascii=False)})
            relay.tick()
        web_server.register_prm(service)
        yield SimpleNamespace(service=service, sent=sent, relay=relay, clock=clock)
        web_server.register_prm(None)
        relay.stop()

    def test_review_page_shows_the_draft_and_sends_it(self, page, prm):
        # the first draft was superseded by the second message; one waits
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("待审批", "小美 · 收件信息复述", "在的，有什么可以帮你？", "建议阶段：收集收件信息", "收件人：小美", "已过时"):
            assert expected in text
        assert "13812345678" not in text
        page.locator(".prm-draft button:has-text('改了再发')").click()
        page.locator("textarea[aria-label='修改要发的内容']").fill("在的，小美你好～")
        page.locator(".prm-draft button:has-text('确认发送')").click()
        page.wait_for_timeout(1500)
        packet = prm.service.store.packet(2)
        assert packet["status"] == "approved" and packet["final_text"] == "在的，小美你好～" and packet["edited"]
        assert "发送中" in page.inner_text("body")

    def test_review_page_says_a_draft_goes_out_as_several_messages(self, page, prm):
        """PRM_REPLY_PARTS (2026-10-30): a draft with blank lines says 「会分 N 条发」; while it is edited the line says
        how the text will go; a draft of one paragraph says nothing."""
        import json as _json
        from src.prm.service import SETTLE_SEC

        prm.service._reply_parts = 3                                       # (PRM_REPLY_PARTS=3)
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        assert "条发（空一行就是分开发" not in page.inner_text("body")       # 「在的，有什么可以帮你？」: one message
        prm.service.handle({"chat_id": "wxid_creator01", "sender_id": "wxid_creator01", "content": "均价多少呀",
                            "msg_type": 1, "is_group": False, "timestamp": prm.clock.now})
        prm.clock.now += SETTLE_SEC + 1
        job = prm.relay.next_job(0, ("prm",))
        d = {"intent": "问价", "slots": {}, "stage_proposal": "", "message_kind": "faq",
             "reply_draft": "好呀～\n\n贝雷帽 399 元，小礼帽 269 元\n\n你平时主要在抖音还是小红书呀？",
             "attachments_to_send": [], "needs_attention": False, "reason": "kb/products.md"}
        prm.relay.finish(job["id"], {"ok": True, "text": _json.dumps(d, ensure_ascii=False)})
        prm.relay.tick()
        page.reload()
        page.wait_for_timeout(1500)
        card = page.locator(".prm-draft", has_text="贝雷帽 399 元")
        assert "会分 3 条发（空一行就是分开发，最多 3 条）" in card.inner_text()
        card.locator("button:has-text('改了再发')").click()
        card.locator("textarea[aria-label='修改要发的内容']").fill("好呀～贝雷帽 399 元")
        page.wait_for_timeout(300)
        assert "整段一条发（空一行就是分开发，最多 3 条）" in card.inner_text()

    def test_review_page_runs_a_platform_action_once_and_shows_it_red_when_unverified(self, page, prm):
        import json as _json
        from src.prm import platforms
        from src.prm.service import SETTLE_SEC

        action = platforms.check_action({"action": "dy.designated_commission", "item_id": "3000000000000000101",
                                         "creator_platform_id": "5100000000000002", "commission_rate": 20})
        preview = {"version": 1, "action": action, "verdict": "ready", "problems": [],
                   "facts": [{"label": "将写入", "value": "fixed commission 20%; ends never"}],
                   "steps": [{"name": "designated", "label": "百应 · 建指定达人佣金策略", "execute": False,
                              "command": "node bin/dy.mjs buyin promotion-strategy-create-designated --to=20",
                              "exit_code": 0, "seconds": 2.1, "verdict": "ready", "outcome": "", "problems": [],
                              "facts": [], "raw": '{"overall": "PREVIEW_ONLY"}'}],
                   "run": {"steps": ["designated"]}, "catalog": {"synced_at": 1.0, "title": "测试主品"},
                   "previewed_at": prm.clock.now}
        prm.service.handle({"chat_id": "wxid_creator01", "sender_id": "wxid_creator01", "content": "好的就按 20%",
                            "msg_type": 1, "is_group": False, "timestamp": prm.clock.now})
        prm.clock.now += SETTLE_SEC + 1
        job = prm.relay.next_job(0, ("prm",))
        d = {"intent": "谈妥专属佣金", "slots": {"佣金": "20%"}, "stage_proposal": "agreed", "message_kind": "money",
             "reply_draft": "专属佣金 20% 设好啦～", "attachments_to_send": [], "needs_attention": False,
             "reason": "kb/合作政策.md", "platform": preview}
        prm.relay.finish(job["id"], {"ok": True, "text": _json.dumps(d, ensure_ascii=False)})
        prm.relay.tick()
        packet = prm.service.store.packet_for_job(job["id"])
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        card = page.locator(".prm-draft", has_text="平台动作")
        text = card.inner_text()
        for expected in ("平台动作：抖音百应 · 指定达人专属佣金", "预览通过，可以批准执行", "佣金率（%）", "20%", "测试主品",
                         "fixed commission 20%", "平台上核验成功以后才发给达人"):
            assert expected in text
        assert card.locator("button:has-text('发送')").count() == 0
        card.locator("button:has-text('看预览明细')").click()
        assert "promotion-strategy-create-designated" in card.inner_text()
        card.locator("button:has-text('批准并执行')").click()
        page.wait_for_timeout(300)
        dialog = page.locator("[role=dialog]")
        assert "只执行一次，不会自动重试" in dialog.inner_text()
        dialog.locator("button:has-text('执行')").click()
        page.wait_for_timeout(1500)
        assert prm.service.store.packet(packet["id"])["status"] == "executing"
        assert "平台执行中" in page.inner_text("body")
        # the Mac: the write went out, the read-back did not prove it -> red, nothing sent
        mac_job = prm.relay.next_job(0, ("prm",))
        assert mac_job["meta"]["op"] == "platform_execute"
        rec = {"version": 1, "op": "platform_execute", "outcome": "ACCEPTED_UNVERIFIED", "key": mac_job["meta"]["key"],
               "problems": ["读回没证实"], "facts": [], "steps": [], "created": {}, "finished_at": prm.clock.now}
        prm.relay.finish(mac_job["id"], {"ok": True, "text": _json.dumps(rec, ensure_ascii=False)})
        prm.relay.tick()
        page.wait_for_timeout(5000)                               # the page polls every 4 s
        alerts = page.locator(".prm-alerts").inner_text()
        assert "要人工处理" in alerts and "ACCEPTED_UNVERIFIED" in alerts and "没有发微信" in alerts
        page.locator(".prm-alerts button:has-text('我已处理')").click()
        page.wait_for_timeout(1500)
        assert page.locator(".prm-alerts").count() == 0
        assert prm.sent == []

    def test_review_page_drops_a_draft_after_asking(self, page, prm):
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator(".prm-draft button:has-text('不发')").click()
        page.wait_for_timeout(300)
        page.locator("[role=dialog] button:has-text('不发')").click()
        page.wait_for_timeout(1500)
        assert prm.service.store.packet(2)["status"] == "dropped"
        assert "现在没有要审批的草稿" in page.inner_text("body") and prm.sent == []

    def test_review_page_copies_a_baiying_draft_and_marks_it_sent_by_hand(self, page, prm):
        """抖音百应私信（PRM_BUYIN_SYNC_MIN）：草稿卡片是「百应私信 · 运营手动发」，号码只显示占位符；复制 / 改了再复制
        拿到带完整号码的字（记一笔），已在百应发了 → 已发出（运营手动），不发要确认；机器人在微信里什么都不发。"""
        import json as _json
        import re as _re
        from src.prm import platforms
        from src.prm.service import BUYIN_SYNC_CHAT

        service, relay, clock = prm.service, prm.relay, prm.clock
        service.creator_directory = lambda: [{"wxid": "wxid_creator01", "platform": "DY", "platform_no": "xiaomei_dy",
                                              "platform_uid": ""}]

        def baiying(cid, mid, text, reply):
            relay.submit(BUYIN_SYNC_CHAT, BUYIN_SYNC_CHAT, "读抖音百应私信（精选联盟）", kind="prm",
                         meta={"op": "buyin_inbox"}, fresh=True, input_closed=True)
            poll = relay.next_job(0, ("prm",))
            convo = platforms.im_conversation({"conversationId": cid, "creator": {"awemeId": "xiaomei_dy",
                                                                                   "nickname": "小美的抖音"},
                                               "messages": [{"id": mid, "at": clock.now, "from": "creator", "text": text}]})
            relay.finish(poll["id"], {"ok": True, "text": _json.dumps(platforms.im_result(conversations=[convo]),
                                                                       ensure_ascii=False)})
            relay.tick()
            job = relay.next_job(0, ("prm",))
            assert job["meta"]["channel"] == "buyin"
            d = {"intent": "问佣金", "slots": {}, "stage_proposal": "", "message_kind": "faq", "reply_draft": reply,
                 "attachments_to_send": [], "needs_attention": False, "reason": "kb/合作政策.md"}
            relay.finish(job["id"], {"ok": True, "text": _json.dumps(d, ensure_ascii=False)})
            relay.tick()
            return service.store.packet_for_job(job["id"])

        first = baiying("conv_a", "a1", "佣金多少？我电话 13900001111", "你好呀，佣金 20%，电话〈手机2〉是你的吗？")
        second = baiying("conv_b", "b1", "第二个会话里问的", "好的收到～")
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        card = page.locator(".prm-buyin", has_text="佣金多少")
        text = card.inner_text()
        for expected in ("百应私信 · 运营手动发", "小美 · 百应私信", "她在百应说", "百应里的昵称：小美的抖音", "〈手机2〉",
                         "你好呀，佣金 20%", "机器人不能在百应里发消息"):
            assert expected in text, expected
        assert "13900001111" not in page.inner_text("body")
        assert card.locator("button:has-text('发送')").count() == 0
        head = card.locator(".card-head h2").bounding_box()            # 宽屏：两个标签和标题在同一行
        assert all(abs(t.bounding_box()["y"] - head["y"]) < head["height"] for t in card.locator(".card-head .tag").all())
        page.set_viewport_size({"width": 390, "height": 844})          # 手机宽度：右上角的两个标签折到下一行，不被截掉
        page.wait_for_timeout(300)
        box = card.bounding_box()
        for tag in card.locator(".card-head .tag").all():
            t = tag.bounding_box()
            assert box["x"] <= t["x"] and t["x"] + t["width"] <= min(box["x"] + box["width"], 390), (t, box)
        page.set_viewport_size({"width": 1440, "height": 900})
        card.locator("button").filter(has_text=_re.compile(r"^\s*复制\s*$")).click()
        page.wait_for_timeout(1000)
        assert "13900001111" in card.locator("textarea[aria-label='已复制的内容']").input_value()
        assert service.store.events(first["deal_id"], kinds=("buyin_copy",))
        card.locator("button:has-text('改了再复制')").click()
        card.locator("textarea[aria-label='修改要复制的内容']").fill("改过的：电话〈手机2〉")
        card.locator("button:has-text('复制改好的')").click()
        page.wait_for_timeout(1000)
        assert card.locator("textarea[aria-label='已复制的内容']").input_value() == "改过的：电话13900001111"
        card.locator("button:has-text('已在百应发了')").click()
        page.wait_for_timeout(1500)
        done = service.store.packet(first["id"])
        assert (done["status"], done["outcome"], done["final_text"]) == ("sent", "sent_by_hand", "改过的：电话〈手机2〉")
        row = page.locator(".prm-recent tr", has_text=f"#{first['id']}")
        assert "已在百应手动发" in row.inner_text() and row.locator("button:has-text('标为发错了')").count() == 0
        other = page.locator(".prm-buyin", has_text="第二个会话里问的")
        other.locator("button:has-text('不发')").click()
        page.wait_for_timeout(300)
        page.locator("[role=dialog] button:has-text('不发')").click()
        page.wait_for_timeout(1500)
        assert service.store.packet(second["id"])["status"] == "dropped"
        assert page.locator(".prm-buyin").count() == 0 and prm.sent == []

    def test_review_page_marks_our_baiying_invites_and_counts_others_contact_cards(self, page, prm):
        """百应私信：没登记的达人里我们邀约的（Mac 按邀约台账认出来的）写「我们邀约的 · 抖音号」和邀约时间；别人邀约的会话里
        的联系方式卡片只说有几张、不归我们处理；店里同事邀约的（Mac 说 ``other_invite``）不列名字、只说有几位；号码哪儿都不
        显示。号码、抖音号、名字都是编的。"""
        import json as _json
        from src.prm import platforms
        from src.prm.service import BUYIN_SYNC_CHAT

        service, relay, clock = prm.service, prm.relay, prm.clock
        service._buyin_sec = 600                                     # PRM_BUYIN_SYNC_MIN=10
        relay.submit(BUYIN_SYNC_CHAT, BUYIN_SYNC_CHAT, "读抖音百应私信（精选联盟）", kind="prm",
                     meta={"op": "buyin_inbox"}, fresh=True, input_closed=True)
        poll = relay.next_job(0, ("prm",))
        card = {"wechat": "zq_test_wx_83", "phone": "13700001234"}
        convs = [platforms.im_conversation(c) for c in (
            {"conversationId": "conv_inv", "creator": {"buyinId": "1777000000000000042", "awemeId": "invited_test_dy",
                                                       "nickname": "邀约的测试达人"},
             "invited": {"aweme_id": "invited_test_dy", "invited_at": "2026-10-29T15:00:00+08:00",
                         "plan_id": "PTEST-plan-0001"},
             "messages": [{"id": "i1", "at": clock.now, "from": "creator", "kind": "contact", "contact": card}]},
            {"conversationId": "conv_other", "creator": {"buyinId": "1777000000000000043", "nickname": "别人的测试达人"},
             "messages": [{"id": "o1", "at": clock.now, "from": "creator", "kind": "contact", "contact": card}]},
            {"conversationId": "conv_colleague", "creator": {"buyinId": "1777000000000000044", "nickname": "同事邀约的测试达人"},
             "other_invite": True,
             "messages": [{"id": "c1", "at": clock.now, "from": "creator", "kind": "text", "text": "你好"}]})]
        relay.finish(poll["id"], {"ok": True, "text": _json.dumps(platforms.im_result(conversations=convs),
                                                                   ensure_ascii=False)})
        relay.tick()
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("百应里有 2 位没登记的达人发了消息",
                         "邀约的测试达人（我们邀约的 · 抖音号 invited_test_dy · 10-29 15:00 邀约）", "别人的测试达人",
                         "别人邀约的会话里有 1 张联系方式卡片，不归我们处理",
                         "另有 1 位是店里同事邀约的（卡片不是我们这边发的），她们的回复不归我们管"):
            assert expected in text, expected
        assert "同事邀约的测试达人" not in text
        assert "zq_test_wx_83" not in text and "13700001234" not in text

    def test_deals_page_registers_and_disables_a_creator(self, page, prm):
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("合作单", "登记达人", "已登记的达人", "小美", "wxid_creator01", "意向沟通", "AI 建议：收集收件信息",
                         "达人副驾驶", "PRM_COPILOT"):
            assert expected in text
        page.locator("input[aria-label='达人 wxid']").fill("wxid_admin")
        page.locator("input[aria-label='称呼']").fill("管理员")
        page.locator(".prm-register button:has-text('登记')").click()
        page.wait_for_timeout(800)
        assert "管理员或能用 cc 中转的人" in page.inner_text("body")
        page.locator("input[aria-label='达人 wxid']").fill("wxid_newcreator")
        page.locator("input[aria-label='称呼']").fill("阿花")
        page.locator(".prm-register button:has-text('登记')").click()
        page.wait_for_timeout(1500)
        assert prm.service.is_party_chat("wxid_newcreator") and "阿花" in page.inner_text("body")
        page.locator("tr", has_text="阿花").locator("button:has-text('停用')").click()
        page.wait_for_timeout(1500)
        assert not prm.service.is_party_chat("wxid_newcreator") and "已停用" in page.inner_text("body")

    def test_deals_page_offers_new_friends_first_and_registering_one_drafts_her_messages(self, page, prm, tmp_path):
        """登记达人: new friends first, tagged, with their greeting (masked) and how; registering one is
        still a click of a person, and her messages since she was added become one draft job."""
        from src.prm.service import SETTLE_SEC
        from src.wechat.new_friends import NewFriendService, NewFriendStore
        from src.web import server as web_server

        now = prm.clock.now
        store = NewFriendStore(str(tmp_path / "messages.db"))
        store.add(wxid="wxid_new01", name="阿花", remark="", how="auto_accept", kind="pc", added_at=now - 60,
                  verify_from=now - 65, verify_text="我是阿花，电话 138****8000", found_at=now - 50)

        class Backend:                          # the WCDB backend reads her chat back on its next poll
            def catch_up_chat(self, talker, name, since, sink):
                sink([{"message_id": "m1", "chat_id": talker, "sender_id": talker, "content": "我是阿花，电话 13800138000",
                       "msg_type": 1, "is_group": False, "timestamp": now - 65},
                      {"message_id": "m2", "chat_id": talker, "sender_id": talker, "content": "想合作",
                       "msg_type": 1, "is_group": False, "timestamp": now - 30}])

        service = NewFriendService(Backend(), store, clock=prm.clock)
        service.set_catch_up_sink(prm.service.take_new_friend)
        prm.service.on_registered = service.registered
        web_server.register_new_friends(service)
        try:
            page.goto(f"{BASE_URL}/#/deals", timeout=10000)
            page.wait_for_timeout(1500)
            card = page.locator(".prm-new-friend", has_text="阿花")
            text = card.inner_text()
            for expected in ("新好友", "阿花", "wxid_new01", "机器人自动通过的", "验证消息：我是阿花，电话 138****8000"):
                assert expected in text
            assert "13800138000" not in page.inner_text("body")
            options = page.locator("select[aria-label='从最近的私聊里选'] option").all_inner_texts()
            assert options[1] == "新好友 · 阿花（wxid_new01）"
            assert prm.relay.next_job(0, ("prm",)) is None and not prm.service.is_party_chat("wxid_new01")   # nothing yet
            card.locator("button:has-text('选这个')").click()
            assert page.locator("input[aria-label='达人 wxid']").input_value() == "wxid_new01"
            assert page.locator("input[aria-label='称呼']").input_value() == "阿花"
            register = page.locator(".prm-register").inner_text()
            assert "阿花 是新加的好友" in register and "最多 12 小时内的" in register and "不要登记" in register
            page.locator(".prm-register button:has-text('登记')").click()
            page.wait_for_timeout(1200)
            assert "加好友以来对方发的消息会合成一份草稿" in page.inner_text("body")
            assert prm.service.is_party_chat("wxid_new01") and page.locator(".prm-new-friend").count() == 0
            prm.clock.now += SETTLE_SEC + 1
            job = prm.relay.next_job(0, ("prm",))
            assert job["meta"]["new_friend"] == {"from": now - 65, "count": 2}
            assert [line.split("] ", 1)[1] for line in job["text"].split("\n")] == [
                "（加好友时的验证消息）我是阿花，电话 〈手机1〉", "想合作"]
        finally:
            web_server.register_new_friends(None)

    def test_deals_page_tags_a_friend_the_bot_registered_and_stops_her_for_good(self, page, prm, tmp_path):
        """PRM_AUTO_REGISTER: the register card says the switch is on, the friend the bot registered is no candidate
        and is tagged 自动登记 in the list; 停用 takes her off, she comes back among the new friends marked 已停用,
        and the bot never registers her again."""
        from types import SimpleNamespace
        from src.wechat.new_friends import NewFriendService, NewFriendStore
        from src.web import server as web_server

        now = prm.clock.now
        store = NewFriendStore(str(tmp_path / "messages.db"))
        store.add(wxid="wxid_new02", name="阿青", remark="", how="requested", kind="requested", added_at=now - 60,
                  verify_from=None, verify_text="", found_at=now - 50)
        web_server.register_new_friends(NewFriendService(SimpleNamespace(), store, clock=prm.clock))
        prm.service._auto_register = True                       # PRM_AUTO_REGISTER=on, as the console shows it
        try:
            assert prm.service.auto_register_friend({"wxid": "wxid_new02", "name": "阿青", "how": "requested"})
            page.goto(f"{BASE_URL}/#/deals", timeout=10000)
            page.wait_for_timeout(1500)
            assert "新好友自动登记已打开" in page.locator(".prm-register").inner_text()
            assert page.locator(".prm-new-friend").count() == 0              # registered: not offered
            row = page.locator(".prm-parties tr", has_text="阿青")
            assert row.locator(".tag", has_text="自动登记").count() == 1
            assert "自动登记（新好友，我们加她）" in row.inner_text()
            assert page.locator(".prm-parties tr", has_text="小美").locator(".tag", has_text="自动登记").count() == 0
            row.locator("button:has-text('停用')").click()
            page.wait_for_timeout(1500)
            assert not prm.service.is_party_chat("wxid_new02") and "已停用" in row.inner_text()
            again = page.locator(".prm-new-friend", has_text="阿青")
            assert again.locator(".tag", has_text="已停用").count() == 1    # offered again, marked stopped
            assert prm.service.auto_register_friend({"wxid": "wxid_new02", "name": "阿青", "how": "requested"}) is None
            assert prm.service.store.party_by_wxid("wxid_new02", include_inactive=True)["active"] == 0
        finally:
            web_server.register_new_friends(None)

    def test_deals_page_asks_for_an_opening_and_the_review_page_tags_it(self, page, prm):
        """开场白: a creator who has not written has 「写开场白」 in her deal (小美, who wrote, has none); a click asks
        the Mac for our first line, whose draft waits in 待审批 tagged 开场白; asked again meanwhile, it says why not."""
        import json as _json
        from src.prm.service import SETTLE_SEC

        prm.service.register("wxid_quiet01", "小青")
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator("button[aria-label='展开 小美']").click()
        page.wait_for_timeout(300)
        assert page.locator(".prm-opening").count() == 0                    # she wrote: her messages are drafted
        page.locator("button[aria-label='展开 小青']").click()
        page.wait_for_timeout(300)
        block = page.locator(".prm-opening")
        assert "她还没跟我们说过话" in block.inner_text()
        block.locator("button:has-text('写开场白')").click()
        page.wait_for_timeout(1200)
        assert "已请 Mac 写开场白" in page.inner_text("body")
        prm.clock.now += SETTLE_SEC + 1
        job = prm.relay.next_job(0, ("prm",))
        assert job["meta"]["opening"] == {"how": "", "by": "console"} and job["text"] == ""
        block.locator("button:has-text('写开场白')").click()                # while it is being written
        page.wait_for_timeout(1200)
        assert "她的草稿正在写" in page.inner_text("body")
        draft = {"intent": "开场白：先打招呼", "slots": {}, "stage_proposal": "", "message_kind": "greeting",
                 "reply_draft": "小青你好呀～我是 ACE7CN 的", "attachments_to_send": [], "needs_attention": False,
                 "reason": "kb/合作政策.md 场景 A1"}
        prm.relay.finish(job["id"], {"ok": True, "text": _json.dumps(draft, ensure_ascii=False)})
        prm.relay.tick()
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        card = page.locator(".prm-draft", has_text="小青你好呀")
        assert card.locator(".tag", has_text="开场白").count() == 1
        assert "这一条：开场白：先打招呼" in card.inner_text()
        assert page.locator(".prm-draft", has_text="在的，有什么可以帮你？").locator(".tag", has_text="开场白").count() == 0

    def test_deals_page_offers_an_opening_to_a_creator_who_wrote_but_was_never_answered(self, page, prm):
        """2026-11-02: 小美 wrote, her waiting draft is dropped and nothing of ours ever went out: her deal shows 「开场白」
        all the same, saying why, and a click asks the Mac for an opening that knows she wrote (``wrote``)."""
        from src.prm.service import SETTLE_SEC

        [waiting] = prm.service.store.packets(("pending",))
        assert prm.service.decide(waiting["id"], "drop")["ok"]
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator("button[aria-label='展开 小美']").click()
        page.wait_for_timeout(300)
        block = page.locator(".prm-opening")
        text = block.inner_text()
        assert "她说过话" in text and "可我们还没正式回过她" in text and "她还没跟我们说过话" not in text
        block.locator("button:has-text('写开场白')").click()
        page.wait_for_timeout(1200)
        assert "已请 Mac 写开场白" in page.inner_text("body")
        prm.clock.now += SETTLE_SEC + 1
        job = prm.relay.next_job(0, ("prm",))
        assert job["meta"]["opening"] == {"how": "", "by": "console", "wrote": True} and job["text"] == ""

    def test_deals_page_edits_saves_and_shows_the_draft_note(self, page, prm):
        """起草备注 (2026-10-31): an opened deal shows its note (none yet) with the hint; 「写备注」 opens the field, 「保存」
        keeps it through the deal action (one line, as the bot keeps it) and her next draft job carries it; more than
        300 characters, or the bot refusing, says why right there and keeps nothing; saved empty, it is cleared."""
        from src.prm.service import SETTLE_SEC

        store = prm.service.store
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator("button[aria-label='展开 小美']").click()
        page.wait_for_timeout(300)
        block = page.locator(".prm-draft-note")
        text = block.inner_text()
        assert "起草备注" in text and "（没有）" in text
        assert "写给 Mac 起草时看的，比如她签名里的要求：直接发链接和 brief、不接语音" in text
        block.locator("button:has-text('写备注')").click()
        field = block.locator("textarea[aria-label='小美 的起草备注']")
        field.fill("长" * 301)
        assert "301/300 字" in block.inner_text()
        block.locator("button:has-text('保存')").click()
        page.wait_for_timeout(500)
        assert "起草备注最多 300 个字（现在 301 个）" in block.inner_text() and store.deal(1)["draft_note"] == ""
        field.fill("直接发链接和brief，感兴趣的会回复！\n勿催视频，不接语音")
        block.locator("button:has-text('保存')").click()
        page.wait_for_timeout(1500)
        kept = "直接发链接和brief，感兴趣的会回复！ 勿催视频，不接语音"
        assert store.deal(1)["draft_note"] == kept
        assert kept in block.inner_text() and block.locator("textarea").count() == 0
        assert "改起草备注" in page.locator(".prm-deal").inner_text()            # the deal's record
        prm.service.handle({"chat_id": "wxid_creator01", "sender_id": "wxid_creator01", "content": "佣金多少",
                            "msg_type": 1, "is_group": False, "timestamp": prm.clock.now})
        prm.clock.now += SETTLE_SEC + 1
        assert prm.relay.next_job(0, ("prm",))["meta"]["note"] == kept
        # the bot says no: said in the field, the note kept as it was
        prm.service.set_draft_note = lambda *a, **k: {"ok": False, "error": "测试：现在不能改"}
        block.locator("button:has-text('改备注')").click()
        block.locator("textarea[aria-label='小美 的起草备注']").fill("")
        block.locator("button:has-text('保存')").click()
        page.wait_for_timeout(800)
        assert "测试：现在不能改" in block.inner_text() and store.deal(1)["draft_note"] == kept
        del prm.service.set_draft_note
        block.locator("button:has-text('保存')").click()                    # saved empty: cleared
        page.wait_for_timeout(1500)
        assert store.deal(1)["draft_note"] == "" and "（没有）" in block.inner_text()

    def test_deals_page_makes_a_three_party_group_after_asking_and_tries_one_first(self, page, prm):
        """三方群 (src/prm/three_party.py, 2026-10-30): with PRM_GROUP_WITH set, a deal has 「拉三方群」 and 「先试一下」; the
        group is made only after the confirmation, which names who goes in and the suggested name; made, the deal
        shows it with 「复制群名」; 「群已经不在了」 takes it back; one the bot is unsure of asks 「建好了 / 没建成」.
        Every name is made up."""
        from types import SimpleNamespace
        from test_three_party import Admin, Auto, Backend
        from src.prm.three_party import ThreeParty

        service = prm.service
        service.three_party = ThreeParty(service.store, SimpleNamespace(prm_group_with="老板测试",
                                                                         prm_friend_add_brand="品牌测试"))
        backend = Backend()
        admin = Admin(backend)
        service.three_party.attach(admin, Auto(), backend)
        service.three_party.spawn = lambda fn: fn()
        service.three_party._sleep = lambda s: None
        deal_id = service.store.deal_of(service.store.party_by_wxid("wxid_creator01")["id"])["id"]
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator("button[aria-label='展开 小美']").click()
        page.wait_for_timeout(300)
        block = page.locator(".prm-group")
        assert "勾上她和 老板测试" not in block.inner_text() and "老板测试" in block.inner_text()
        block.locator("button:has-text('先试一下')").click()
        page.wait_for_timeout(1200)
        assert admin.calls == [(["小美测试", "老板测试"], True)]
        assert "试过：" in block.inner_text()
        block.locator("button:has-text('拉三方群')").click()
        page.wait_for_timeout(300)
        dialog = page.locator(".dialog, [role='dialog']").last
        text = dialog.inner_text()
        assert "老板测试" in text and "「品牌测试×小美」" in text and "不会自动重试" in text
        assert len(admin.calls) == 1                                      # nothing before the confirmation
        dialog.locator("button:has-text('拉群')").click()
        page.wait_for_timeout(1500)
        assert admin.calls[-1] == (["小美测试", "老板测试"], False)
        text = block.inner_text()
        assert "三方群已建好" in text and "「品牌测试×小美」" in text and block.locator("button:has-text('复制群名')").count() == 1
        assert block.locator("button:has-text('拉三方群')").count() == 0
        block.locator("button:has-text('群已经不在了')").click()
        page.wait_for_timeout(1200)
        assert block.locator("button:has-text('拉三方群')").count() == 1
        service.store.update_deal(deal_id, None, three_party={"state": "unsure", "at": prm.clock.now,
                                                             "why": "点了「完成」以后「发起群聊」窗口还在"})
        page.reload(timeout=10000)
        page.wait_for_timeout(1500)
        page.locator("button[aria-label='展开 小美']").click()
        page.wait_for_timeout(300)
        block = page.locator(".prm-group")
        assert "不确定建没建成：点了「完成」以后「发起群聊」窗口还在" in block.inner_text()
        block.locator("button:has-text('建好了')").click()
        page.wait_for_timeout(1200)
        assert "三方群已建好" in block.inner_text() and "手动标的" in block.inner_text()
        assert service.store.deal(deal_id)["three_party"]["state"] == "created"

    def test_deals_page_runs_a_deal_stage_mode_waybill_and_numbers(self, page, prm):
        store = prm.service.store
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator("button[aria-label='展开 小美']").click()
        page.wait_for_timeout(300)
        detail = page.locator(".prm-deal")
        text = detail.inner_text()
        for expected in ("谁来回复", "副驾驶：Mac 起草", "寄样", "还没有运单号", "号码", "显示完整号码"):
            assert expected in text
        assert "13812345678" not in page.inner_text("body")
        # the AI's stage, then a person takes the chat and gives it back
        detail.locator("button:has-text('采纳 AI 建议：收集收件信息')").click()
        page.wait_for_timeout(1200)
        assert store.deal(1)["stage"] == "address"
        assert detail.locator("select[aria-label='小美 的阶段']").input_value() == "address"
        detail.locator("[aria-label='小美 的模式'] button:has-text('人工接管')").click()
        page.wait_for_timeout(1200)
        assert store.deal(1)["mode"] == "human" and "人工接管中" in detail.inner_text()
        detail.locator("button:has-text('恢复副驾驶')").click()
        page.wait_for_timeout(1200)
        assert store.deal(1)["mode"] == "copilot"
        # a waybill: the shipping notice waits in 待审批; then the parcel moves
        detail.locator("input[aria-label='小美 的快递公司']").fill("顺丰")
        detail.locator("input[aria-label='小美 的运单号']").fill("SF1234567890123")
        detail.locator("button:has-text('填运单号并起草发货通知')").click()
        page.wait_for_timeout(1500)
        assert store.latest_shipment(1)["waybill"] == "SF1234567890123" and store.deal(1)["stage"] == "shipped"
        assert "SF1234567890123 · 已寄出" in detail.inner_text()
        detail.locator("select[aria-label='小美 的物流状态']").select_option("delivered")
        detail.locator("button:has-text('更新物流')").click()
        page.wait_for_timeout(1500)
        text = detail.inner_text()
        assert store.deal(1)["delivered_at"] and "后自动清除收件信息" in text and "已签收" in text
        assert "控制台 更新物流：SF1234567890123 已签收" in text
        # the full number, written down; and hidden again
        detail.locator("button:has-text('显示完整号码')").click()
        page.wait_for_timeout(800)
        assert "〈手机1〉 13812345678" in detail.inner_text()
        assert [e["kind"] for e in store.events(1, limit=1)] == ["reveal"]
        detail.locator("button:has-text('隐藏')").click()
        assert "13812345678" not in page.inner_text("body")
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        notice = page.locator(".prm-draft", has_text="寄出、物流通知")
        assert "系统里的运单号：SF1234567890123" in notice.inner_text() and "系统生成" in notice.inner_text()

    def test_review_page_marks_a_sent_message_wrong_and_shows_the_gates(self, page, prm):
        assert prm.service.decide(2, "send")["ok"]
        prm.service.send_approved()
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        gates = page.locator(".prm-gates").inner_text()
        for expected in ("自动发送门槛", "收件信息复述", "先审后发", "29/30", "一直先审"):
            assert expected in gates
        recent = page.locator(".prm-recent")
        assert "已发出 · 未确认" in recent.inner_text()
        recent.locator("button:has-text('标为发错了')").click()
        page.wait_for_timeout(300)
        page.locator("[role=dialog] button:has-text('标为发错了')").click()
        page.wait_for_timeout(1500)
        assert prm.service.store.packet(2)["wrong"] is True
        assert "发错了" in recent.inner_text() and "标为发错了" not in recent.inner_text()
        assert [e["kind"] for e in prm.service.store.events(1, limit=1)] == ["wrong"]

    def test_review_page_switches_auto_mode_on_after_asking_and_off_with_one_press(self, page, prm):
        """自动模式 (2026-10-30): the switch at the top of 待审批 asks first and says what happens; on, 待审批 and 合作单 say
        since when, the night status line says it is what applies, the draft waiting already still waits, and what it
        sent by itself is tagged 自动模式发出 (its 标为发错了 says so); off is one press, no question."""
        import json as _json
        import time as _time
        from src.prm.service import SETTLE_SEC

        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        bar = page.locator(".prm-auto")
        bar.get_by_text("自动模式关着").wait_for()
        bar.locator("button:has-text('打开自动模式')").click()
        dialog = page.locator("[role=dialog]")
        text = dialog.inner_text()
        for expected in ("打开自动模式？", "不经你审批直接发出", "拿不准的只回一句过渡的话",
                         "投诉、平台动作、新好友的第一份草稿、开场白", "再按一下「关掉自动模式」就关掉"):
            assert expected in text
        dialog.locator("button:has-text('再想想')").click()
        dialog.wait_for(state="detached")
        assert prm.service.auto_mode["on"] is False                           # nothing until it is confirmed
        bar.locator("button:has-text('打开自动模式')").click()
        dialog.locator("button:has-text('打开自动模式')").click()
        since = _time.strftime("%H:%M", _time.localtime(prm.clock.now))
        bar.get_by_text(f"自动模式开着（{since} 打开）").wait_for()
        assert prm.service.auto_mode["on"] is True and bar.locator("button:has-text('关掉自动模式')").count() == 1
        assert "下班时段自动回复：关着（自动模式开着，照下班时段的规则自动发）" in page.locator(".prm-gates").inner_text()
        assert prm.service.store.packet(2)["status"] == "pending"              # the draft waiting already still waits
        # her next message is drafted in 自动模式 and goes out by itself
        prm.service.handle({"chat_id": "wxid_creator01", "sender_id": "wxid_creator01", "content": "好的谢谢",
                            "msg_type": 1, "is_group": False, "timestamp": prm.clock.now})
        prm.clock.now += SETTLE_SEC + 1
        job = prm.relay.next_job(0, ("prm",))
        assert job["meta"]["night"]["auto"] is True
        draft = {"intent": "道谢", "slots": {}, "stage_proposal": "", "message_kind": "greeting",
                 "reply_draft": "不客气呀～有事随时找我哈", "attachments_to_send": [], "needs_attention": False,
                 "reason": "kb/合作政策.md"}
        prm.relay.finish(job["id"], {"ok": True, "text": _json.dumps(draft, ensure_ascii=False)})
        prm.relay.tick()
        assert prm.service.send_approved() == 1 and prm.sent[-1] == ("wxid_creator01", "不客气呀～有事随时找我哈")
        page.reload()
        row = page.locator(".prm-recent tr", has_text="不客气呀")
        row.locator(".tag", has_text="自动模式发出").wait_for()
        assert "夜间自动发出" not in page.locator(".prm-recent").inner_text()
        row.locator("button:has-text('标为发错了')").click()
        assert "这条是自动模式自动发出的" in dialog.inner_text()
        dialog.locator("button:has-text('不标')").click()
        dialog.wait_for(state="detached")
        # 合作单 says it is on too
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.locator(".prm-auto-note").get_by_text(f"自动模式开着（{since} 打开）").wait_for()
        # off: one press, no question
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        bar.locator("button:has-text('关掉自动模式')").click()
        bar.get_by_text("自动模式关着").wait_for()
        assert dialog.count() == 0 and prm.service.auto_mode["on"] is False


    @pytest.fixture
    def night(self):
        """A running PRM service at 21:30 (this machine's clock) with the 下班时段 18:00-09:00 on: a flagged draft went
        out as one holding line, a plain 20% one went out, a 30% one waits."""
        import json as _json
        import time as _time
        from types import SimpleNamespace
        from src.prm.service import SETTLE_SEC, PrmService
        from src.prm.store import PrmStore
        from src.relay.service import ClaudeRelay
        from src.relay.store import RelayStore
        from src.web import server as web_server

        class Clock:
            now = _time.mktime((2026, 10, 29, 21, 30, 0, 0, 0, -1))

            def __call__(self):
                return self.now

        clock = Clock()
        cfg = SimpleNamespace(
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
            claude_relay_max_chars=1500, admin_wxid="wxid_admin", prm_copilot="on", prm_db=":memory:",
            prm_night_auto_hours="18:00-09:00",
        )
        sent = []
        relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock)
        service = PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock,
                             send=lambda c, t: sent.append((c, t)) or True, send_file=lambda c, p: True)
        service.register("wxid_creator01", "小美", "测试达人")
        line = "好呀～您的意向我都记下啦！明早我第一时间拉个小群，一起跟您敲定选款和寄样细节哈！"
        for text, more in (("好呀，可以寄来看看", {"reply_draft": line, "needs_attention": True,
                                                   "reason": "已进入寄样阶段，请运营拉三方群"}),
                           ("佣金多少呀", {"reply_draft": "我们达人合作的佣金是 20%～", "message_kind": "money"}),
                           ("能给 30% 吗", {"reply_draft": "可以给到 30%～", "message_kind": "money"})):
            service.handle({"chat_id": "wxid_creator01", "sender_id": "wxid_creator01", "content": text,
                            "msg_type": 1, "is_group": False, "timestamp": clock.now})
            clock.now += SETTLE_SEC + 1
            job = relay.next_job(0, ("prm",))
            draft = {"intent": "问合作", "slots": {}, "stage_proposal": "", "message_kind": "other",
                     "attachments_to_send": [], "needs_attention": False, "reason": "kb/合作政策.md", **more}
            relay.finish(job["id"], {"ok": True, "text": _json.dumps(draft, ensure_ascii=False)})
            relay.tick()
            service.send_approved()
        web_server.register_prm(service)
        yield SimpleNamespace(service=service, sent=sent, relay=relay, clock=clock)
        web_server.register_prm(None)
        relay.stop()

    def test_review_page_shows_the_night_window_its_sends_and_the_notice_until_seen_to(self, page, night):
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.get_by_text("下班时段自动回复：18:00–09:00（现在是下班时段）").wait_for()
        alerts = page.locator(".prm-night-alerts").inner_text()
        assert "小美 · 夜间自动回了过渡的话（#1）" in alerts and "要注意：已进入寄样阶段，请运营拉三方群" in alerts
        recent = page.locator(".prm-recent")
        assert recent.inner_text().count("夜间自动发出") == 2 and "要注意" in recent.inner_text()
        waiting = page.locator(".prm-draft", has_text="可以给到 30%").inner_text()
        assert "夜间没有自动发：说到了「30%」（超过 25%）" in waiting
        assert [t for _, t in night.sent] == ["好呀～您的意向我都记下啦！明早我第一时间拉个小群，一起跟您敲定选款和寄样细节哈！",
                                              "我们达人合作的佣金是 20%～"]
        page.locator(".prm-night-alerts button:has-text('已处理')").click()
        page.locator(".prm-night-alerts").wait_for(state="detached")
        assert night.service.store.deal(1)["attention"] == ""
        assert night.service.store.events(1, limit=1)[0]["kind"] == "night_done"
        recent.locator("tr", has_text="佣金是 20%").locator("button:has-text('标为发错了')").click()
        dialog = page.locator("[role=dialog]")
        assert "这条是下班时段自动发出的" in dialog.inner_text()
        dialog.locator("button:has-text('标为发错了')").click()
        page.wait_for_timeout(1500)
        assert night.service.store.packet(2)["wrong"] is True
        assert night.service.store.events(1, limit=1)[0]["detail"]["night"] is True

    # ── 加百应达人微信 (PRM_FRIEND_ADD, 功能 R): the 加微信 cards, the paste form, 登记达人's 抖音号 ───────────
    @staticmethod
    def _friend_adds_on(prm):
        """Switch 加百应达人微信 on for this in-memory service, a fake friend manager behind the real admin handler."""
        from types import SimpleNamespace
        from src.admin import AdminCommandHandler
        from src.prm.friend_adds import FriendAdds

        class Manager:                                  # (a 4.0 application window whose 「备注」 field OCR saw)
            def __init__(self):
                self.calls = []
                self.remarks = []

            def add_friend(self, keyword, message="", remark=""):
                self.calls.append((keyword, message))
                self.remarks.append(remark)
                return {"ok": True, "target": keyword, "message": f"已成功向「{keyword}」发送好友申请",
                        **({"remark_set": True} if remark else {})}

        manager = Manager()
        adds = FriendAdds(prm.service.store, SimpleNamespace(prm_friend_add="on"), clock=prm.clock,
                          register=prm.service.register, spawn=lambda fn: fn(), clear_clipboard=lambda text: False)
        adds.attach(AdminCommandHandler(friend_manager=manager), None, None)
        prm.service.friend_adds = adds
        prm.service.on_buyin_contact = adds.offer_from_buyin              # (the 百应 inbox's hook, as PrmService wires it)
        return adds, manager

    def test_review_page_adds_a_baiying_contact_once_after_its_confirmation(self, page, prm):
        adds, manager = self._friend_adds_on(prm)
        adds.paste("dy_demo01", "小鹿", {"wechat": "abcdefyz", "phone": "13800000000"})
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        card = page.locator(".prm-friend-add", has_text="小鹿")
        text = card.inner_text()
        for expected in ("加微信 · 小鹿", "抖音号 dy_demo01", "运营手动填的", "微信号 ab****yz", "手机 138****0000", "待确认",
                         "添加该达人微信", "我在手机上加了", "请她加我们", "不加"):
            assert expected in text, expected
        assert "今天控制台已加 0/10" in page.inner_text("body") and "abcdefyz" not in page.inner_text("body")
        assert card.locator("input[aria-label='#1 的附言']").input_value() == "您好小鹿，百应上联系您的～"
        remark = card.locator(".prm-remark").inner_text()                        # the remark, next to the greeting
        assert "备注：抖音-小鹿" in remark and "认出「备注」栏就填进去" in remark
        card.locator("button:has-text('显示完整')").click()                      # written down, this page only
        page.wait_for_timeout(800)
        assert "abcdefyz" in card.inner_text() and len(adds._store.friend_add(1)["reveals"]) == 1
        card.locator("button:has-text('隐藏')").click()
        card.locator("[aria-label='#1 用哪个加'] button:has-text('微信号')").click()
        card.locator("button:has-text('添加该达人微信')").click()
        page.wait_for_timeout(300)
        dialog = page.locator("[role=dialog]")
        said = dialog.inner_text()
        assert "搜索 ab****yz" in said and "不会自动重试" in said and "别动键盘鼠标" in said
        assert "附言：您好小鹿，百应上联系您的～；备注：抖音-小鹿" in said
        assert manager.calls == []                                               # nothing before 确定添加
        dialog.locator("button:has-text('确定添加')").click()
        page.wait_for_timeout(1500)
        assert manager.calls == [("abcdefyz", "您好小鹿，百应上联系您的～")] and manager.remarks == ["抖音-小鹿"]
        assert adds._store.friend_add(1)["state"] == "requested"
        text = page.locator(".prm-friend-add", has_text="小鹿").inner_text()
        assert "已发申请" in text and "她已经通过了，我来选是哪个聊天" in text and "添加该达人微信" not in text
        assert "备注：抖音-小鹿（已填进好友申请）" in text
        assert "今天控制台已加 1/10" in page.inner_text("body")

    def test_review_page_drops_a_contact_after_asking_and_links_another_to_her_chat(self, page, prm):
        adds, manager = self._friend_adds_on(prm)
        adds.paste("dy_demo01", "小鹿", {"wechat": "abcdefyz"})
        adds.paste("dy_demo02", "小青", {"phone": "13900000000"})
        adds.confirm(2)                                                          # (sent: waits for her)
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator(".prm-friend-add", has_text="小鹿").locator("button:has-text('不加')").click()
        page.wait_for_timeout(300)
        assert "号码会马上清除" in page.locator("[role=dialog]").inner_text()
        page.locator("[role=dialog] button:has-text('不加')").click()
        page.wait_for_timeout(1500)
        assert adds._store.friend_add(1)["state"] == "dropped" and adds._store.friend_add(1)["wechat"] == ""
        assert page.locator(".prm-friend-add", has_text="小鹿").count() == 0
        page.locator(".prm-friend-add", has_text="小青").locator("button:has-text('她已经通过了，我来选是哪个聊天')").click()
        page.wait_for_timeout(300)
        dialog = page.locator("[role=dialog]")
        assert "dy_demo02" in dialog.inner_text()
        dialog.locator("input[aria-label='#2 关联的 wxid']").fill("wxid_xiaoqing")
        dialog.locator("button:has-text('是她，登记并关联')").click()
        page.wait_for_timeout(1500)
        party = prm.service.store.party_by_wxid("wxid_xiaoqing")
        assert party["name"] == "小青" and adds._store.friend_add(2)["state"] == "linked"
        assert [l["account"] for l in prm.service.store.creator_links(party["id"])] == ["dy_demo02"]
        # a chat picked by hand (how she came is not known): the card stays and suggests her remark on the phone
        card = page.locator(".prm-friend-add", has_text="小青")
        assert "已登记并关联" in card.inner_text()
        assert "建议在手机微信里给她设备注：抖音-小青" in card.locator(".prm-remark-hint").inner_text()
        card.locator("button:has-text('复制备注')").click()
        page.wait_for_timeout(500)
        assert page.evaluate("navigator.clipboard.readText()") == "抖音-小青"
        card.locator("button:has-text('备注设好了')").click()
        page.wait_for_timeout(1500)
        assert adds._store.friend_add(2)["remark_set"] is True
        assert page.locator(".prm-friend-add").count() == 0 and len(manager.calls) == 1

    def test_deals_page_pastes_a_contact_and_registers_a_creator_with_her_douyin_id(self, page, prm):
        adds, _ = self._friend_adds_on(prm)
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        form = page.locator(".prm-friend-paste")
        assert "手动登记百应达人留的联系方式" in form.inner_text() and "可能记一次「查看」" in form.inner_text()
        assert form.locator("button:has-text('记下来')").is_disabled()          # the 抖音号 is required
        form.locator("input[aria-label='百应达人的抖音号']").fill("有中文")
        form.locator("button:has-text('记下来')").click()
        page.wait_for_timeout(800)
        assert "要填她的抖音号" in page.inner_text("body") and adds._store.friend_adds() == []
        form.locator("input[aria-label='百应达人的抖音号']").fill("dy_demo03")
        form.locator("input[aria-label='她留的微信号']").fill("1abc")
        form.locator("button:has-text('记下来')").click()
        page.wait_for_timeout(800)
        assert "字母开头" in page.inner_text("body") and adds._store.friend_adds() == []
        form.locator("input[aria-label='她留的微信号']").fill("abcdefgh")
        form.locator("input[aria-label='百应昵称']").fill("小白")
        form.locator("button:has-text('记下来')").click()
        page.wait_for_timeout(1200)
        [record] = adds._store.friend_adds()
        assert (record["aweme_id"], record["nickname"], record["wechat"], record["state"]) == (
            "dy_demo03", "小白", "abcdefgh", "offered")
        assert "到「待审批 › 加微信」确认添加" in page.inner_text("body")
        register = page.locator(".prm-register")
        register.locator("input[aria-label='达人 wxid']").fill("wxid_xiaohua")
        register.locator("input[aria-label='称呼']").fill("小花")
        register.locator("input[aria-label='达人抖音号（可选）']").fill("dy_demo05")
        register.locator("button:has-text('登记')").click()
        page.wait_for_timeout(1500)
        party = prm.service.store.party_by_wxid("wxid_xiaohua")
        assert [(l["account"], l["source"]) for l in prm.service.store.creator_links(party["id"])] == [("dy_demo05", "register")]
        page.locator("button[aria-label='展开 小花']").click()
        page.wait_for_timeout(500)
        detail = page.locator(".prm-deal")
        assert "登记时填的：抖音号 dy_demo05" in detail.inner_text()
        detail.locator(".prm-links button:has-text('取消关联')").click()
        page.wait_for_timeout(1500)
        assert prm.service.store.creator_links(party["id"]) == []

    def test_review_page_shows_who_may_be_her_and_only_an_operator_links_her(self, page, prm, tmp_path):
        """功能 R step 5: a new friend that fits a record is shown on its card (how sure, why); 不是她 clears it;
        是她，登记并关联 registers her; 登记达人's list marks the new friend and 选这个 brings her 抖音号."""
        from src.wechat.new_friends import NewFriendService, NewFriendStore
        from src.web import server as web_server

        adds, _ = self._friend_adds_on(prm)
        adds.paste("dy_demo01", "小鹿", {"wechat": "abcdefyz"})
        adds.confirm(1)
        now = prm.clock.now
        store = NewFriendStore(str(tmp_path / "messages.db"))
        for wxid, name in (("wxid_wrong01", "路人甲"), ("wxid_xiaolu", "小鹿🌸")):
            store.add(wxid=wxid, name=name, remark="", how="requested", kind="requested", added_at=now, verify_from=None,
                      verify_text="", found_at=now)
        web_server.register_new_friends(NewFriendService(object(), store, clock=prm.clock))
        try:
            adds.on_new_friend({"wxid": "wxid_wrong01", "name": "路人甲", "how": "requested", "own_greeting": "",
                                "added_at": now})
            page.goto(f"{BASE_URL}/#/review", timeout=10000)
            page.wait_for_timeout(1500)
            card = page.locator(".prm-friend-add", has_text="小鹿")
            note = card.locator(".prm-candidate").inner_text()
            assert "新好友「路人甲」" in note and "很可能是她" in note and "只有这一条在等她通过" in note
            card.locator(".prm-candidate button:has-text('不是她')").click()
            page.wait_for_timeout(1200)
            assert adds._store.friend_add(1)["rejected"] == ["wxid_wrong01"] and card.locator(".prm-candidate").count() == 0
            assert prm.service.store.parties()[1:] == []                  # nobody registered by itself
            adds.on_new_friend({"wxid": "wxid_xiaolu", "name": "小鹿🌸", "how": "requested",
                                "own_greeting": "您好小鹿，百应上联系您的～", "added_at": now + 5})
            page.goto(f"{BASE_URL}/#/deals", timeout=10000)
            page.wait_for_timeout(1500)
            row = page.locator(".prm-new-friend", has_text="wxid_xiaolu")
            assert "确定是她：百应的小鹿（#1）" in row.inner_text()
            row.locator("button:has-text('选这个')").click()
            assert page.locator("input[aria-label='达人抖音号（可选）']").input_value() == "dy_demo01"
            page.goto(f"{BASE_URL}/#/review", timeout=10000)
            page.wait_for_timeout(1500)
            card = page.locator(".prm-friend-add", has_text="小鹿")
            assert "确定是她" in card.locator(".prm-candidate").inner_text()
            card.locator(".prm-candidate button:has-text('是她，登记并关联')").click()
            page.wait_for_timeout(1500)
            party = prm.service.store.party_by_wxid("wxid_xiaolu")
            assert party["name"] == "小鹿" and adds._store.friend_add(1)["state"] == "linked"
            assert [l["account"] for l in prm.service.store.creator_links(party["id"])] == ["dy_demo01"]
            assert adds._store.friend_add(1)["remark_set"] is True       # she accepted our request, which had it
            assert page.locator(".prm-friend-add").count() == 0          # (so nothing to set on the phone)
        finally:
            web_server.register_new_friends(None)

    def test_with_the_switch_off_the_pages_have_nothing_of_it(self, page, prm):
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.wait_for_timeout(1500)
        assert page.locator(".prm-friend-paste").count() == 0
        assert page.locator("input[aria-label='达人抖音号（可选）']").count() == 0
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        assert page.locator(".prm-friend-adds").count() == 0

    def test_review_page_says_who_left_a_contact_and_the_baiying_draft_names_its_record(self, page, prm):
        """加百应达人微信第 7 步：「待审批」页百应那一行说没登记的达人里有几位留了联系方式（见下面的「加微信」），我们邀约的
        达人发的联系方式卡片成了一张加微信卡；百应草稿卡上说这个会话留过的联系方式是哪一条加微信、到哪一步了；完整号码哪儿都
        不显示。号码、抖音号、名字都是编的。"""
        import json as _json
        from src.prm import platforms
        from src.prm.service import BUYIN_SYNC_CHAT

        self._friend_adds_on(prm)
        service, relay, clock = prm.service, prm.relay, prm.clock
        service._buyin_sec = 600                                     # PRM_BUYIN_SYNC_MIN=10
        service.creator_directory = lambda: [{"wxid": "wxid_creator01", "platform": "DY", "platform_no": "xiaomei_dy",
                                              "platform_uid": ""}]
        # she left her card in 百应 before she was registered, and was linked since
        early, _ = service.store.add_friend_add(state="linked", now=clock.now, source="buyin_card",
                                                conversation_id="conv_reg", message_id="r0", aweme_id="xiaomei_dy",
                                                nickname="小美的抖音", masked={"wechat": "zq****83"})
        relay.submit(BUYIN_SYNC_CHAT, BUYIN_SYNC_CHAT, "读抖音百应私信（精选联盟）", kind="prm",
                     meta={"op": "buyin_inbox"}, fresh=True, input_closed=True)
        poll = relay.next_job(0, ("prm",))
        card = {"wechat": "zq_test_wx_83", "phone": "13700001234"}
        convs = [platforms.im_conversation(c) for c in (
            {"conversationId": "conv_inv", "creator": {"buyinId": "1777000000000000042", "awemeId": "invited_test_dy",
                                                       "nickname": "邀约的测试达人"},
             "invited": {"aweme_id": "invited_test_dy", "invited_at": "2026-10-29T15:00:00+08:00",
                         "plan_id": "PTEST-plan-0001"},
             "messages": [{"id": "i1", "at": clock.now, "from": "creator", "kind": "contact", "contact": card}]},
            {"conversationId": "conv_reg", "creator": {"awemeId": "xiaomei_dy", "nickname": "小美的抖音"},
             "messages": [{"id": "r1", "at": clock.now, "from": "creator", "text": "样品什么时候到？"}]})]
        relay.finish(poll["id"], {"ok": True, "text": _json.dumps(platforms.im_result(conversations=convs),
                                                                   ensure_ascii=False)})
        relay.tick()
        job = relay.next_job(0, ("prm",))
        assert job["meta"]["channel"] == "buyin" and job["meta"]["creator"]["douyin_ids"] == ["xiaomei_dy"]
        d = {"intent": "问样品", "slots": {}, "stage_proposal": "", "message_kind": "faq", "reply_draft": "这两天就到哦",
             "attachments_to_send": [], "needs_attention": False, "reason": "kb/合作政策.md"}
        relay.finish(job["id"], {"ok": True, "text": _json.dumps(d, ensure_ascii=False)})
        relay.tick()
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        for expected in ("百应里有 1 位没登记的达人发了消息", "其中 1 位留了联系方式，见下面的「加微信」",
                         "加微信 · 邀约的测试达人", "微信号 zq****83", "手机 137****1234"):
            assert expected in text, expected
        draft = page.locator(".prm-buyin", has_text="样品什么时候到")
        assert (f"她在这个会话里留了联系方式（微信号 zq****83）→「加微信 #{early['id']}」：已登记并关联"
                in draft.inner_text())
        assert "zq_test_wx_83" not in text and "13700001234" not in text

    # ── 老板拉的群 (PRM_BOSS_GROUPS, 功能 U): the fields the PRM side adds to /api/prm (``boss_groups``, a packet's
    # ``channel`` "group" / ``group_name``, ``source`` "boss_group", ``three_party.by`` "boss") and its two POSTs are
    # served here by interception; everything else is the running in-memory service. Names and ids are made up. ──────
    @staticmethod
    def _serve_prm(page, change):
        """GET /api/prm as the running service answers it, then changed in place by ``change(state)``."""
        def answer(route):
            response = route.fetch()
            state = response.json()
            change(state)
            route.fulfill(response=response, json=state)

        page.route("**/api/prm", answer)

    def test_review_page_tags_a_group_draft_and_asks_before_it_goes_into_the_group(self, page, prm):
        """A draft of her line in the boss's group (``channel`` "group") is tagged 「群里 · <群名>」 (a long name ends in …
        inside the card at phone width); 发送 and 确认发送 ask first and say it goes in that group, not her private chat,
        where the boss sees it; 再想想 sends nothing; the history row says 群里."""
        name = "小美的测试群（老板测试、测试达人、品牌测试的运营号）"

        def group(state):
            for p in state["pending"] + state["recent"]:
                if p["id"] == 2:
                    p.update(channel="group", group_name=name)

        self._serve_prm(page, group)
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        card = page.locator(".prm-draft", has=page.locator(".prm-group-tag"))
        tag = card.locator(".tag", has_text="群里 · 小美的测试群")
        tag.wait_for()
        assert "在的，有什么可以帮你？" in card.inner_text() and page.locator(".prm-group-tag").count() == 1
        page.set_viewport_size({"width": 390, "height": 844})                     # 手机宽度：长群名不把标签挤出卡片
        page.wait_for_timeout(300)
        box, t = card.bounding_box(), tag.bounding_box()
        assert box["x"] <= t["x"] and t["x"] + t["width"] <= min(box["x"] + box["width"], 390), (t, box)
        page.set_viewport_size({"width": 1440, "height": 900})
        card.locator("button:has-text('发送')").click()
        dialog = page.locator("[role=dialog]")
        text = dialog.inner_text()
        for expected in ("#2 发到群里？", f"老板拉的群「{name}」", "不是她的私聊", "老板和群里的人都看得到"):
            assert expected in text, expected
        dialog.locator("button:has-text('再想想')").click()
        dialog.wait_for(state="detached")
        assert prm.service.store.packet(2)["status"] == "pending"                # nothing before 发到群里
        card.locator("button:has-text('改了再发')").click()
        card.locator("textarea[aria-label='修改要发的内容']").fill("在的～群里也可以直接问我")
        card.locator("button:has-text('确认发送')").click()
        assert "老板和群里的人都看得到" in dialog.inner_text()
        assert prm.service.store.packet(2)["status"] == "pending"
        dialog.locator("button:has-text('发到群里')").click()
        page.get_by_text("已确认，正在发到群里").wait_for()
        packet = prm.service.store.packet(2)
        assert packet["status"] == "approved" and packet["final_text"] == "在的～群里也可以直接问我" and packet["edited"]
        assert prm.service.send_approved() == 1
        page.reload()
        row = page.locator(".prm-recent tr", has_text="群里也可以直接问我")
        row.wait_for()
        assert "群里 · 收件信息复述" in row.inner_text()
        assert "群里" not in page.locator(".prm-recent tr", has_text="#1").inner_text()   # another one: as before

    @pytest.mark.parametrize("channel", [None, "private"], ids=["no-channel", "private"])
    def test_review_page_shows_and_sends_a_private_draft_exactly_as_before(self, page, prm, channel):
        """A draft without ``channel`` (a bot from before) or for her private chat looks as it did and 发送 sends it at
        once, with no question."""
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        card = page.locator(".prm-draft", has_text="在的，有什么可以帮你？")
        card.wait_for()
        before = card.inner_text()

        def private(state):
            for p in state["pending"]:
                p.pop("group_name", None)
                if channel is None:
                    p.pop("channel", None)
                else:
                    p["channel"] = channel

        self._serve_prm(page, private)
        page.reload()
        card.wait_for()
        assert card.inner_text() == before and "群里" not in before
        card.locator("button:has-text('发送')").click()
        page.get_by_text("已确认，正在发送").wait_for()
        assert page.locator("[role=dialog]").count() == 0
        assert prm.service.store.packet(2)["status"] == "approved"

    def test_deals_page_tags_a_creator_the_boss_introduced_and_her_group_block_says_so(self, page, prm):
        """A creator taken over from a boss's group (``source`` "boss_group", on her party or her deal) is tagged 老板介绍
        in the list (one registered by hand is not); her 三方群 block says 老板拉的群 (``by`` "boss") and what the bot does
        there, without the suggested name and 「复制群名」 of a group we made."""
        import time as _time

        prm.service.register("wxid_quiet01", "小青")
        prm.service.register("wxid_test_red", "小红")
        now = prm.clock.now

        def boss(state):
            state["three_party"] = {"enabled": True, "with": ["老板测试"], "why": "", "busy": None,
                                    "name_template": "品牌测试×{昵称}"}
            for p in state["parties"]:
                if p["wxid"] == "wxid_creator01":
                    p["source"] = "boss_group"
                    p["deal"]["three_party"] = {"state": "created", "label": "已建好", "at": now, "by": "boss",
                                                "why": "老板拉的群", "chat_name": "小美的测试群", "made": True}
                if p["wxid"] == "wxid_test_red" and p.get("deal"):
                    p["deal"]["source"] = "boss_group"

        self._serve_prm(page, boss)
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        page.locator(".prm-parties tr", has_text="wxid_creator01").locator(".tag", has_text="老板介绍").wait_for()
        assert page.locator(".prm-parties tr", has_text="wxid_test_red").locator(".tag", has_text="老板介绍").count() == 1
        assert page.locator(".prm-parties tr", has_text="wxid_quiet01").locator(".tag", has_text="老板介绍").count() == 0
        page.locator("button[aria-label='展开 小美']").click()
        block = page.locator(".prm-group")
        since = _time.strftime("%H:%M", _time.localtime(now))
        text = block.inner_text()
        for expected in (f"老板拉的群（{since} 发现）：微信里叫「小美的测试群」", "之后由我们跟她对接", "她在群里说的话照常起草",
                         "回复发在群里，老板也看得到", "老板和别人说的话不起草"):
            assert expected in text, expected
        assert "三方群已建好" not in text and "品牌测试×小美" not in text
        assert block.locator("button:has-text('复制群名')").count() == 0 and block.locator("button:has-text('拉三方群')").count() == 0
        assert block.locator("button:has-text('群已经不在了')").count() == 1
        page.locator("button[aria-label='展开 小红']").click()                    # a deal with no group: as before
        assert page.locator(".prm-group button:has-text('拉三方群')").count() == 1

    def test_deals_page_asks_which_member_she_is_in_a_boss_group_and_drops_one_after_asking(self, page, prm):
        """The 「老板拉的群」 card at the top of 合作单 lists the boss's groups with more than one person who may be her:
        是她 posts {chat, wxid} at once (taken over, the group leaves the list and is counted); 这个群不是达人 asks first
        and only then posts {chat}; one line counts the groups taken over and those waiting for her; 待审批 says a group
        waits for a person."""
        now = prm.clock.now
        bg = {"enabled": True, "why_not": "", "boss": ["老板测试"], "waiting": 1, "linked": 2, "pending": [
            {"chat": "test_group_01@chatroom", "name": "老板测试的小群", "found_at": now,
             "members": [{"wxid": "wxid_test_a", "name": "测试达人"}, {"wxid": "wxid_test_b", "name": "测试助理"}]},
            {"chat": "test_group_02@chatroom", "name": "老板测试的家人群", "found_at": now,
             "members": [{"wxid": "wxid_test_c", "name": "测试亲戚"}, {"wxid": "wxid_test_d", "name": ""}]}]}
        posted = []

        def answer(route):
            action = route.request.url.rsplit("/", 1)[1]
            body = route.request.post_data_json
            posted.append((route.request.method, action, body))
            bg["pending"] = [g for g in bg["pending"] if g["chat"] != body["chat"]]
            if action == "pick":
                bg["linked"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, **({"party_id": 9, "deal_id": 9} if action == "pick" else {})}))

        self._serve_prm(page, lambda state: state.update(boss_groups=json.loads(json.dumps(bg))))
        page.route("**/api/prm/boss-groups/*", answer)
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.get_by_text("老板拉了 2 个新群，群里不止一个人可能是她：到「合作单」页顶上的「老板拉的群」里选哪个是她").wait_for()
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        card = page.locator(".prm-boss-groups")
        card.wait_for()
        assert card.bounding_box()["y"] < page.locator(".prm-register").bounding_box()["y"]    # at the top
        text = card.inner_text()
        for expected in ("老板拉的群", "老板（老板测试）", "老板测试的小群", "测试达人", "wxid_test_a", "测试助理",
                         "老板测试的家人群", "测试亲戚", "（没有名字）", "wxid_test_d",
                         "已接手 2 个群；1 个群在等她进群或者等群里有消息（机器人每分钟再看一次）", "2 个群要你选哪个是她"):
            assert expected in text, expected
        first = card.locator(".prm-boss-group", has_text="老板测试的小群")
        assert first.locator("button:has-text('是她')").count() == 2
        for row in (first, card.locator(".prm-boss-group", has_text="老板测试的家人群")):   # (no ``why``: as before)
            assert "群里不止一个人可能是她，点哪个是她" in row.inner_text() and row.locator(".note").count() == 0
        assert "群里不止一个人可能是她时，在下面点哪个是她。" in text
        first.locator(".prm-boss-member", has_text="测试达人").locator("button:has-text('是她')").click()
        page.get_by_text("已接手「老板测试的小群」：测试达人 登记成达人").wait_for()
        assert posted == [("POST", "pick", {"chat": "test_group_01@chatroom", "wxid": "wxid_test_a"})]
        assert page.locator("[role=dialog]").count() == 0                         # 是她: one press, no question
        card.get_by_text("已接手 3 个群", exact=False).wait_for()
        assert card.locator(".prm-boss-group", has_text="老板测试的小群").count() == 0
        second = card.locator(".prm-boss-group", has_text="老板测试的家人群")
        second.locator("button:has-text('这个群不是达人')").click()
        dialog = page.locator("[role=dialog]")
        assert "「老板测试的家人群」不是达人的群？" in dialog.inner_text() and "以后不再问" in dialog.inner_text()
        dialog.locator("button:has-text('再想想')").click()
        dialog.wait_for(state="detached")
        assert len(posted) == 1                                                    # nothing before the confirmation
        second.locator("button:has-text('这个群不是达人')").click()
        dialog.locator("button:has-text('不是达人')").click()
        page.get_by_text("记下了：「老板测试的家人群」不是达人的群").wait_for()
        assert posted[1:] == [("POST", "ignore", {"chat": "test_group_02@chatroom"})]
        card.locator(".prm-boss-group").first.wait_for(state="detached")
        assert "要你选" not in card.inner_text() and "已接手 3 个群；1 个群在等她进群" in card.inner_text()
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.locator(".prm-draft").first.wait_for()
        assert "老板拉了" not in page.inner_text("body")

    def test_deals_page_says_why_the_bot_did_not_take_a_boss_group_over(self, page, prm):
        """A pending group the bot did not take over by itself shows its own ``why`` (she was stopped, registering
        failed, she has a 三方群 already…) and 是她 still works there; 「群里不止一个人可能是她」 only for a group with no
        ``why`` and more than one member listed (the hint above the groups too); ``boss_call`` leaves the card's name."""
        import time as _time

        now = prm.clock.now
        found = f"{_time.strftime('%H:%M', _time.localtime(now))} 发现"
        stopped = "她在达人名单里停用过：要接手的话请在这里点「是她」"
        failed = "没登记成：测试用的原因"
        has_group = "她已经有三方群：测试用的说明"

        def group(n, why, *members):
            return {"chat": f"test_group_1{n}@chatroom", "name": f"测试群{n}", "found_at": now, "size": len(members) + 2,
                    "why": why, "members": [{"wxid": f"wxid_test_{n}{m}", "name": f"测试成员{n}{m}"} for m in members]}

        bg = {"enabled": True, "why_not": "", "boss": ["老板测试"], "boss_call": "测试总", "waiting": 0, "linked": 0,
              "pending": [group(1, stopped, "a"), group(2, failed, "a", "b"), group(3, "", "a", "b"), group(4, "", "a"),
                          group(5, has_group)]}
        posted = []

        def answer(route):
            body = route.request.post_data_json
            posted.append((route.request.url.rsplit("/", 1)[1], body))
            bg["pending"] = [g for g in bg["pending"] if g["chat"] != body["chat"]]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "party_id": 9, "deal_id": 9}))

        self._serve_prm(page, lambda state: state.update(boss_groups=json.loads(json.dumps(bg))))
        page.route("**/api/prm/boss-groups/*", answer)
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        card = page.locator(".prm-boss-groups")
        card.wait_for()
        assert card.locator(".card-head h2").inner_text() == "老板拉的群"            # (whatever boss_call is)
        rows = {n: card.locator(".prm-boss-group", has_text=f"测试群{n}") for n in range(1, 6)}
        for n, why in ((1, stopped), (2, failed), (5, has_group)):
            assert rows[n].locator(".note").inner_text() == why, n
            assert "不止一个人" not in rows[n].inner_text() and found in rows[n].inner_text(), n
        assert rows[3].locator(".note").count() == 0
        assert f"{found}：群里不止一个人可能是她，点哪个是她" in rows[3].inner_text()
        assert rows[4].locator(".note").count() == 0 and "不止一个人" not in rows[4].inner_text()
        assert found in rows[4].inner_text() and rows[4].locator("button:has-text('是她')").count() == 1
        assert rows[5].locator("button:has-text('是她')").count() == 0
        assert "群里不止一个人可能是她时，在下面点哪个是她。" in card.inner_text()
        buttons = card.locator("button").all()
        assert len(buttons) == 11 and not any(b.is_disabled() for b in buttons)    # 6 是她, 5 这个群不是达人
        rows[1].locator("button:has-text('是她')").click()                         # (her own why says so)
        page.get_by_text("已接手「测试群1」：测试成员1a 登记成达人").wait_for()
        assert posted == [("pick", {"chat": "test_group_11@chatroom", "wxid": "wxid_test_1a"})]
        rows[1].wait_for(state="detached")
        rows[3].locator(".prm-boss-member", has_text="测试成员3b").locator("button:has-text('是她')").click()
        rows[3].wait_for(state="detached")
        assert posted[1:] == [("pick", {"chat": "test_group_13@chatroom", "wxid": "wxid_test_3b"})]
        # no group left that more than one may be her: nothing says so, the hint neither
        assert card.locator(".prm-boss-group").count() == 3 and "不止一个人" not in card.inner_text()
        # 待审批's line above the drafts: groups with a why of their own are not 「不止一个人可能是她」
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.get_by_text("老板拉了 3 个新群，要你看一眼：到「合作单」页顶上的「老板拉的群」里选哪个是她").wait_for()
        assert "不止一个人" not in page.locator("body").inner_text()

    def test_deals_page_warns_of_an_unknown_boss_name_and_its_buttons_stay_on(self, page, prm):
        """Second review 2026-10-31 F3: ``warning`` (a name of PRM_GROUP_WITH no one contact has: no group is taken over
        by itself) is a warning note on the card while every 是她 and 这个群不是达人 stays on (only ``why_not`` turns them
        off); a group held for that very reason does not say it again, one held for its own reason (「拿不准」) does;
        是她 posts {chat, wxid}; 待审批 says the groups wait for a look."""
        now = prm.clock.now
        warning = ("PRM_GROUP_WITH 里的「老板二」在这个微信里找不到 / 有重名：认出来以前，老板拉的群不会自动接手"
                   "（找到的群在这里等你点「是她」）")
        maybe = "拿不准是不是新拉的群：是的话点「是她」"

        def group(n, why):
            return {"chat": f"test_group_2{n}@chatroom", "name": f"测试群2{n}", "found_at": now, "size": 3, "why": why,
                    "members": [{"wxid": f"wxid_test_2{n}a", "name": f"测试成员2{n}a"}]}

        bg = {"enabled": True, "why_not": "", "warning": warning, "boss": ["老板测试", "老板二"], "boss_call": "老板",
              "waiting": 1, "linked": 0, "pending": [group(1, warning), group(2, maybe)]}
        posted = []

        def answer(route):
            body = route.request.post_data_json
            posted.append((route.request.url.rsplit("/", 1)[1], body))
            bg["pending"] = [g for g in bg["pending"] if g["chat"] != body["chat"]]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "party_id": 9, "deal_id": 9}))

        self._serve_prm(page, lambda state: state.update(boss_groups=json.loads(json.dumps(bg))))
        page.route("**/api/prm/boss-groups/*", answer)
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        card = page.locator(".prm-boss-groups")
        card.locator(".note", has_text="「老板二」").first.wait_for()
        notes = card.locator(".note").all_inner_texts()
        assert notes.count(warning) == 1 and notes.count(maybe) == 1, notes          # (said once, above the groups)
        rows = {n: card.locator(".prm-boss-group", has_text=f"测试群2{n}") for n in (1, 2)}
        assert rows[1].locator(".note").count() == 0 and rows[2].locator(".note").inner_text() == maybe
        assert "1 个群在等她进群或者等群里有消息（机器人每分钟再看一次）" in card.inner_text()
        buttons = card.locator("button").all()
        assert len(buttons) == 4 and not any(b.is_disabled() for b in buttons)    # 2 是她, 2 这个群不是达人
        rows[1].locator("button:has-text('是她')").click()
        page.get_by_text("已接手「测试群21」：测试成员21a 登记成达人").wait_for()
        assert posted == [("pick", {"chat": "test_group_21@chatroom", "wxid": "wxid_test_21a"})]
        rows[1].wait_for(state="detached")
        assert card.locator(".note", has_text="「老板二」").count() == 1            # (still warned of: still not known)
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.get_by_text("老板拉了 1 个新群，要你看一眼：到「合作单」页顶上的「老板拉的群」里选哪个是她").wait_for()

    def test_deals_page_says_why_boss_groups_cannot_run_and_has_nothing_of_them_when_off(self, page, prm):
        """On but unable to run, the card says why (and its buttons are off: a stopped bot lists the groups it found);
        off, or a bot without the field, neither page has anything of it."""
        variant = {}

        def serve(state):
            state.pop("boss_groups", None)                                         # (a bot from before has none)
            state.update(variant)

        self._serve_prm(page, serve)
        variant["boss_groups"] = {"enabled": True, "why_not": "先在设置里填 PRM_GROUP_WITH（老板在这个微信里的名字）",
                                  "boss": [], "pending": [], "waiting": 0, "linked": 0}
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        card = page.locator(".prm-boss-groups")
        card.get_by_text("先在设置里填 PRM_GROUP_WITH（老板在这个微信里的名字）").wait_for()
        assert "还没有发现老板新拉的群" in card.inner_text() and card.locator("button").count() == 0
        variant["boss_groups"] = {"enabled": True, "why_not": "机器人没有运行", "boss": ["老板测试"], "waiting": 0,
                                  "linked": 1, "pending": [
                                      {"chat": "test_group_01@chatroom", "name": "老板测试的小群", "found_at": prm.clock.now,
                                       "members": [{"wxid": "wxid_test_a", "name": "测试达人"},
                                                   {"wxid": "wxid_test_b", "name": "测试助理"}]}]}
        page.reload()
        card.get_by_text("机器人没有运行").wait_for()
        buttons = card.locator("button").all()
        assert len(buttons) == 3 and all(b.is_disabled() for b in buttons)
        for off in ({"boss_groups": {"enabled": False, "why_not": "", "boss": ["老板测试"], "waiting": 0, "linked": 0,
                                     "pending": [{"chat": "test_group_01@chatroom", "name": "老板测试的小群",
                                                  "found_at": prm.clock.now, "members": []}]}},
                    {}):                                                           # off; a bot from before
            variant.clear()
            variant.update(off)
            page.goto(f"{BASE_URL}/#/review", timeout=10000)                 # (another page: read again)
            page.locator(".prm-draft").first.wait_for()
            assert "老板拉了" not in page.inner_text("body")
            page.goto(f"{BASE_URL}/#/deals", timeout=10000)
            page.locator(".prm-parties tr", has_text="wxid_creator01").wait_for()   # (read)
            above_the_settings = "".join(page.locator(".page .card:not(.adv-group), .page .note").all_inner_texts())
            assert card.count() == 0 and "老板拉的群" not in above_the_settings

    def test_deals_page_shows_the_boss_group_settings(self, page, prm):
        """老板拉的群's two settings (src/web/advanced_config.py, the PRM side of 功能 U) show in 合作单's settings: the
        switch, and the group size with its range. Runs once that side is merged (src/prm/boss_groups.py exists)."""
        import importlib.util

        if importlib.util.find_spec("src.prm.boss_groups") is None:
            pytest.skip("功能 U 的 PRM 部分（src/prm/boss_groups.py、advanced_config 的两项设置）还没合进来")
        page.goto(f"{BASE_URL}/#/deals", timeout=10000)
        group = page.locator("#adv-prm")
        switch = group.locator(".adv-field").filter(has=page.get_by_text("PRM_BOSS_GROUPS", exact=True))
        switch.wait_for()
        assert switch.locator("button[role=switch]").count() == 1
        size = group.locator(".adv-field").filter(has=page.get_by_text("PRM_BOSS_GROUP_MAX", exact=True))
        assert "范围 3–20" in size.inner_text()

class TestWechatGapAlert:
    """电脑微信掉线的提醒（Windows：机器人自己记的，副驾驶模式的控制台也有）：每页顶上一直显示到运营点「看过了」，
    还没恢复的那段没有按钮（src/wechat/presence.py，真的 /api/wechat-gaps）。"""

    @pytest.fixture
    def presence(self, tmp_path):
        from types import SimpleNamespace
        from src.wechat.presence import PresenceStore, WechatPresence
        from src.web import server as web_server

        store = PresenceStore(str(tmp_path / "messages.db"))
        service = WechatPresence(store, lambda: {"online": None})
        web_server.register_presence(service)
        yield SimpleNamespace(store=store, service=service)
        web_server.register_presence(None)
        store.close()

    def test_a_gap_shows_until_seen_and_the_one_going_on_has_no_button(self, page, presence):
        now = time.time()
        closed = presence.store.add_gap(now - 3600, "offline_while_running", "找不到微信主窗口", now - 3500)
        presence.store.close_gap(closed["id"], now - 1800)
        presence.store.add_gap(now - 600, "stopped_with_bot", "机器人启动时电脑微信没开", now - 500)
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.get_by_text("不在线（约 30 分钟）", exact=False).wait_for()
        alerts = page.locator(".gap-alerts").inner_text()
        for words in ("还没恢复", "机器人在运行，发现电脑微信不在线了", "可能没收到", "手机微信上把达人的私聊看一遍",
                      "同步最近的聊天记录"):
            assert words in alerts
        assert page.locator(".gap-alerts button:has-text('看过了')").count() == 1, "还没恢复的那段不能点掉"
        page.locator(".gap-alerts button:has-text('看过了')").click()
        page.get_by_text("不在线（约 30 分钟）", exact=False).wait_for(state="detached")
        assert presence.store.gap(closed["id"])["seen_at"] is not None
        assert "还没恢复" in page.locator(".gap-alerts").inner_text()


class TestOnboardingForTheAgentConsole:
    def test_three_steps_end_with_the_relay(self, page):
        """Every request of the wizard is answered here: nothing is written to .env."""
        import json as _json
        posted = {}

        def ok(route, body=None):
            req = route.request
            if req.method == "POST":
                posted[req.url.split("/api/", 1)[1]] = req.post_data_json if req.post_data else None
            route.fulfill(status=200, content_type="application/json", body=_json.dumps(body or {"ok": True}))

        diag = {k: {"ok": True, "value": "ok"} for k in ("python", "requirements", "wechat", "env", "db")}
        page.route("**/api/onboarding/status", lambda r: ok(r, {"ok": True, "onboarding_done": False, "steps": {}}))
        page.route("**/api/onboarding/diagnose", lambda r: ok(r, {"ok": True, **diag}))
        page.route("**/api/onboarding/step*", lambda r: ok(r))
        page.route("**/api/advanced", lambda r: ok(r))
        page.goto(BASE_URL, timeout=10000)
        page.wait_for_timeout(1000)
        nav = page.locator("nav.rail").inner_text()
        assert "中转设置" in nav and "AI 服务" not in nav and "功能" not in nav

        page.locator("button:has-text('我已经有密钥，手动填写')").click()
        page.locator("input[type=password]").fill("a" * 64)
        page.locator("button:has-text('下一步')").click()
        page.wait_for_timeout(300)
        page.locator("input[placeholder='例如 商务号']").fill("机器人账号")
        page.locator("button:has-text('下一步')").click()
        page.wait_for_timeout(500)
        text = page.inner_text("body")
        assert "第 3 步，共 3 步" in text and "你的 wxid" in text
        page.locator("input[placeholder='wxid_…']").fill("wxid_owner123")
        page.locator("button:has-text('完成')").click()
        page.wait_for_timeout(1500)

        step3 = posted["onboarding/step3"]
        assert step3["private_chat_enabled"] is True and step3["private_chats"] == "wxid_owner123"
        assert set(step3) == {"message_burst_enabled", "private_chat_enabled", "private_chats"}   # no chat switch
        assert "onboarding/step4" not in posted
        values = posted["advanced"]["values"]
        assert values["CLAUDE_RELAY"] == "on" and values["ADMIN_WXID"] == "wxid_owner123"
        assert values["CLAUDE_RELAY_ONLY"] == "on"                     # the default of the agent console
        assert len(values["CLAUDE_RELAY_TOKEN"]) >= 40 and values["CLAUDE_RELAY_PORT"] == "8790"
