"""Browser test of the creator flow on the sample site (Playwright, chromium,
phone viewport). Skipped when Playwright or its browser is missing.

The app is served on a random 127.0.0.1 port; the admin API is called
in-process on the same app object.
"""

import threading
from urllib.parse import urlsplit

import pytest

from sample_site.app import create_app, make_wsgi_server
from sample_site.config import Config

from sample_site_fakes import TOKEN, Client, upload_fake_catalogue

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the machine
    sync_playwright = None


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("Playwright not installed")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.skip("chromium for Playwright is missing: %s" % str(exc).splitlines()[0])
        yield b
        b.close()


@pytest.fixture
def site(tmp_path):
    app = create_app(Config(admin_token=TOKEN, db_path=str(tmp_path / "site.db")))
    server = make_wsgi_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    admin = Client(app)
    admin.keys = upload_fake_catalogue(admin)
    admin.base = "http://127.0.0.1:%d" % server.server_address[1]
    yield admin
    server.shutdown()
    server.server_close()
    app.close()


def _link(site, **body):
    body.setdefault("ref", "deal:1")
    resp = site.api("POST", "links", body)
    assert resp.code == 200, resp.text
    return site.base + urlsplit(resp.json()["url"]).path


def _phone(browser, **kw):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              device_scale_factor=2, locale="zh-CN", **kw)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    return ctx, page, problems


def test_creator_flow_in_a_phone_browser(browser, site):
    url = _link(site, max_items=2)
    ctx, page, problems = _phone(browser)
    try:
        page.goto(url)
        cards = page.locator("[data-card]")
        assert cards.count() == 3
        assert page.locator(".fallback").first.is_hidden()  # the script took over
        assert page.locator("[data-count]").inner_text() == "0"

        # card 1: switch to the second colour (the picture follows), pick it
        first = cards.nth(0)
        first.locator("[data-colours] .chip").nth(1).click()
        assert first.locator(".ph-img").get_attribute("src") == "/img/" + site.keys["b"]
        first.locator("[data-pick]").click()
        assert page.locator("[data-count]").inner_text() == "1"
        assert "米白" in first.locator("[data-chosen]").inner_text()

        # card 2 has M/L: picking without a size asks for one
        second = cards.nth(1)
        second.locator("[data-pick]").click()
        assert page.locator("[data-toast]").inner_text() == "请先选尺码"
        second.locator("[data-sizes] .chip", has_text="L").click()
        second.locator("[data-pick]").click()
        assert page.locator("[data-count]").inner_text() == "2"

        # the cap holds
        cards.nth(2).locator("[data-pick]").click()
        assert "最多选 2 顶" in page.locator("[data-toast]").inner_text()
        assert page.locator("[data-count]").inner_text() == "2"

        # details step, with the paste helper
        page.click("[data-next]")
        page.wait_for_selector("#details", state="visible")
        assert page.locator("#bar").is_hidden()
        assert page.locator("[data-picked] li").count() == 2
        page.fill("#paste", "小美 138-0000-0000 浙江省杭州市西湖区测试路 1 号")
        page.click("[data-paste]")
        assert page.input_value("#f-name") == "小美"
        assert page.input_value("#f-phone") == "13800000000"
        assert page.input_value("#f-province") == "浙江省"
        assert page.input_value("#f-city") == "杭州市"
        assert page.input_value("#f-district") == "西湖区"
        assert page.input_value("#f-address") == "测试路 1 号"

        # the back button of the phone returns to the picker
        page.go_back()
        page.wait_for_selector("#step-pick", state="visible")
        assert page.locator("#details").is_hidden()
        page.click("[data-next]")
        page.wait_for_selector("#details", state="visible")
        page.click("[data-submit]")
        page.wait_for_load_state()
        body = page.inner_text("body")
        assert "请核对一下" in body and "米白 · 均码" in body and "藏青 · L" in body
        page.click("button[value=submit]")
        page.wait_for_load_state()
        assert "已收到您的选款，谢谢老师～" in page.inner_text("body")

        # reopening the link shows the summary; editing replaces the choice
        page.goto(url)
        body = page.inner_text("body")
        assert "已收到您的选款" in body and "138****0000" in body
        page.click(".actions a.btn")
        page.wait_for_load_state()
        assert page.locator("[data-count]").inner_text() == "2"
        page.locator("[data-card]").nth(0).locator("[data-pick]").click()
        assert page.locator("[data-count]").inner_text() == "1"
        page.click("[data-next]")
        page.wait_for_selector("#details", state="visible")
        assert page.input_value("#f-address") == "测试路 1 号"
        page.click("[data-submit]")
        page.wait_for_load_state()
        page.click("button[value=submit]")
        page.wait_for_load_state()
        body = page.inner_text("body")
        assert "测试棒球帽" in body and "测试渔夫帽" not in body

        subs = site.api("POST", "submissions/pull", {"owner": "main"}).json()["submissions"]
        assert len(subs) == 1 and subs[0]["rev"] == 2
        assert [i["sku"] for i in subs[0]["items"]] == ["T1002A-L"]

        # after the pull the page is locked
        page.goto(url)
        body = page.inner_text("body")
        assert "您的选款我们已经在安排了，如需调整请直接微信联系运营" in body
        assert page.locator(".actions a.btn").count() == 0
        assert problems == []
    finally:
        ctx.close()


def test_the_form_works_without_javascript(browser, site):
    url = _link(site, max_items=3, ask_account=True)
    ctx, page, _problems = _phone(browser, java_script_enabled=False)
    try:
        page.goto(url)
        assert page.locator(".fallback").first.is_visible()
        page.check("input[name=pick][value='T1001A-F']")
        page.check("input[name=pick][value='T1002B-M']")
        page.fill("#f-name", "小美")
        page.fill("#f-phone", "13800000000")
        page.select_option("#f-province", "上海市")
        page.fill("#f-city", "上海市")
        page.fill("#f-district", "浦东新区")
        page.fill("#f-address", "测试路 2 号")
        page.fill("#f-account", "xiaomei.test")
        page.click("[data-submit]")
        page.wait_for_load_state()
        assert "请核对一下" in page.inner_text("body")
        page.click("button[value=submit]")
        page.wait_for_load_state()
        assert "已收到您的选款，谢谢老师～" in page.inner_text("body")
        subs = site.api("POST", "submissions/pull", {"owner": "main"}).json()["submissions"]
        assert sorted(i["sku"] for i in subs[0]["items"]) == ["T1001A-F", "T1002B-M"]
        assert subs[0]["account"] == "xiaomei.test"
    finally:
        ctx.close()


def test_general_link_in_a_phone_browser(browser, site):
    url = _link(site, kind="general", ref="group:1")
    ctx, page, problems = _phone(browser)
    try:
        page.goto(url)
        page.locator("[data-card]").nth(2).locator("[data-pick]").click()
        page.click("[data-next]")
        page.wait_for_selector("#details", state="visible")
        page.fill("#f-name", "小美")
        page.fill("#f-phone", "13800000000")
        page.select_option("#f-province", "浙江省")
        page.fill("#f-city", "杭州市")
        page.fill("#f-district", "西湖区")
        page.fill("#f-address", "测试路 3 号")
        page.fill("#f-account", "xiaomei.test")
        page.click("[data-submit]")
        page.wait_for_load_state()
        page.click("button[value=submit]")
        page.wait_for_load_state()
        own = page.url
        assert "/s/" in own and "请收藏这个页面" in page.inner_text("body")
        page.goto(url)  # the cookie brings her back to her own page
        assert page.url == own
        assert problems == []
    finally:
        ctx.close()
