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
