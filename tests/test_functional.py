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
            "AI_BACKEND=deepseek\n"
            "DEEPSEEK_API_KEY=\n"  # empty → sandbox returns error immediately without retrying real API
            "DEEPSEEK_MODEL=deepseek-v4-flash\n"
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


class TestSandboxApi:
    """Verify /api/sandbox/test endpoint is routed correctly.

    Note: sending a real message triggers an AI API call which may take 10–30s.
    These tests only verify the endpoint accepts POST requests and returns JSON.
    """

    def test_sandbox_invalid_json_returns_error(self, server):
        status, data = _api_post("/api/sandbox/test", raw_body=b"not json")
        assert status == 200
        assert "ok" in data


class TestConfigDeepseekBaseUrl:
    """Verify deepseek_base_url is loaded and has a default."""

    def test_default_value(self):
        from src.config import BotConfig
        assert BotConfig.deepseek_base_url == "https://api.deepseek.com"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://custom-proxy.example.com")
        monkeypatch.setenv("AI_BACKEND", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        from src.config import load_config
        config = load_config()
        assert config.deepseek_base_url == "https://custom-proxy.example.com"


class TestConfigAnthropicBaseUrl:
    """Verify anthropic_base_url is loaded and has a default."""

    def test_default_value(self):
        from src.config import BotConfig
        assert BotConfig.anthropic_base_url == "https://api.anthropic.com"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude-proxy.example.com")
        monkeypatch.setenv("AI_BACKEND", "claude")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        from src.config import load_config
        config = load_config()
        assert config.anthropic_base_url == "https://claude-proxy.example.com"


class TestSummarizerBaseUrl:
    """Verify AI factory forwards provider-specific Base URLs."""

    def test_claude_factory_passes_anthropic_base_url(self, monkeypatch):
        from src.config import BotConfig
        import src.summarize as summarize

        captured = {}

        class FakeClaudeSummarizer:
            def __init__(self, api_key, model, base_url, chunk_size, chat_model=None):
                captured.update({
                    "api_key": api_key,
                    "model": model,
                    "base_url": base_url,
                    "chunk_size": chunk_size,
                    "chat_model": chat_model,
                })

        monkeypatch.setattr(summarize, "ClaudeSummarizer", FakeClaudeSummarizer)

        config = BotConfig(
            ai_backend="claude",
            anthropic_api_key="sk-ant-test",
            anthropic_base_url="https://claude-proxy.example.com",
            summarize_model="claude-test",
            chunk_size=123,
        )

        summarize.create_summarizer(config)

        assert captured["api_key"] == "sk-ant-test"
        assert captured["model"] == "claude-test"
        assert captured["base_url"] == "https://claude-proxy.example.com"
        assert captured["chunk_size"] == 123


class TestPostAllowlist:
    """Verify /api/sandbox/test is in POST allowlist (doesn't return 405)."""

    def test_sandbox_post_not_405(self, server):
        """Invalid JSON still returns 200 (not 405), proving the route is registered."""
        status, _ = _api_post("/api/sandbox/test", raw_body=b"not json")
        assert status == 200

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
        for expected in ("微信连接", "微信后端", "读取私聊", "监听哪些群", "OCR_FALLBACK", "数据库读不了时用 OCR", "macOS / Windows"):
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
        yield SimpleNamespace(service=service, sent=sent)
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

    def test_review_page_drops_a_draft_after_asking(self, page, prm):
        page.goto(f"{BASE_URL}/#/review", timeout=10000)
        page.wait_for_timeout(1500)
        page.locator(".prm-draft button:has-text('不发')").click()
        page.wait_for_timeout(300)
        page.locator("[role=dialog] button:has-text('不发')").click()
        page.wait_for_timeout(1500)
        assert prm.service.store.packet(2)["status"] == "dropped"
        assert "现在没有要审批的草稿" in page.inner_text("body") and prm.sent == []

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


class TestOnboardingForTheAgentConsole:
    def test_three_steps_end_with_the_relay_and_quiet_defaults(self, page):
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
        page.locator("input[placeholder='例如 小助手']").fill("机器人账号")
        page.locator("button:has-text('下一步')").click()
        page.wait_for_timeout(500)
        text = page.inner_text("body")
        assert "第 3 步，共 3 步" in text and "你的 wxid" in text
        page.locator("input[placeholder='wxid_…']").fill("wxid_owner123")
        page.locator("button:has-text('完成')").click()
        page.wait_for_timeout(1500)

        assert posted["onboarding/step3"]["ai_backend"] == "deepseek"
        step4 = posted["onboarding/step4"]
        assert step4["private_chat_enabled"] is True and step4["private_chats"] == "wxid_owner123"
        assert not any(step4[k] for k in ("fun_enabled", "proactive_enabled", "web_search_enabled",
                                          "sticky_mention_enabled", "followup_listen_enabled"))
        values = posted["advanced"]["values"]
        assert values["CLAUDE_RELAY"] == "on" and values["ADMIN_WXID"] == "wxid_owner123"
        assert values["CLAUDE_RELAY_ONLY"] == "on"                     # the default of the agent console
        assert len(values["CLAUDE_RELAY_TOKEN"]) >= 40 and values["CLAUDE_RELAY_PORT"] == "8790"
