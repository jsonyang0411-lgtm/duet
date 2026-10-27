"""控制台「达人名单」页的浏览器测试：启动真实的 Web 服务，用无头浏览器点页面。

和 tests/test_functional.py 一样需要 Playwright（没装就跳过）和构建好的 ui/dist。
配置文件和数据库都是临时的：WEBOT_ENV_FILE 指向临时 .env，里面的 DB_PATH 指向临时库，
不碰真实的配置和数据库。寄样审批单 2026-10-27 起不在这一页（走「待审批」的平台动作）。
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db.schema import initialize_db
from src.influencer.store import INBOX_DRAFT, SOURCE_CONSOLE, InfluencerStore
from test_functional import _find_free_port, _start_server

UI_INDEX = Path(__file__).resolve().parent.parent / "ui" / "dist" / "index.html"


def seed(db_path):
    conn = initialize_db(str(db_path))
    conn.execute(
        "INSERT INTO messages (message_id, chat_id, sender_id, sender_name, content, timestamp)"
        " VALUES ('m1', 'wxid_jie', 'wxid_jie', '阿杰', '在吗', ?)",
        (int(time.time()),),
    )
    conn.commit()
    conn.close()
    store = InfluencerStore(str(db_path))
    store.add_contacts(["wxid_mei"], source=SOURCE_CONSOLE)
    store.save_contact(
        "wxid_keep", display_name="老朋友", xhs_account="xhs_keep", enabled=False, source=SOURCE_CONSOLE,
    )
    store.add_inbox_item("wxid_mei", INBOX_DRAFT, "老师好，这款下周上新")
    return store


@pytest.fixture(scope="module")
def console(tmp_path_factory):
    if not UI_INDEX.exists():
        pytest.skip("UI not built. Run: cd ui && npm run build")
    sync_api = pytest.importorskip("playwright.sync_api")
    tmp = tmp_path_factory.mktemp("influencer_console")
    env_file = tmp / ".env"
    env_file.write_text(
        "AI_BACKEND=deepseek\nDEEPSEEK_API_KEY=\nWECHAT_GROUPS=*\nONBOARDING_DONE=true\n"
        f"DB_PATH={tmp / 'messages.db'}\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WEBOT_ENV_FILE", str(env_file))
        store = seed(tmp / "messages.db")
        port = _find_free_port()
        _start_server(port)
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            yield SimpleNamespace(base=f"http://127.0.0.1:{port}", browser=browser, store=store)
            browser.close()


@pytest.fixture
def page(console):
    context = console.browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    yield page
    context.close()


def test_the_sidebar_leads_to_the_list_and_the_inbox(console, page):
    page.goto(console.base)
    page.locator(".nav-item", has_text="达人名单").click()
    page.get_by_role("heading", name="达人名单").wait_for()
    assert page.url.endswith("#/influencer/contacts")
    subs = page.locator(".nav-sub button")
    assert subs.all_inner_texts() == ["达人名单", "待处理"], "审批单子页撤掉了"
    subs.filter(has_text="待处理").click()
    page.get_by_role("heading", name="待处理").wait_for()


def test_an_old_approvals_link_shows_the_list(console, page):
    page.goto(f"{console.base}/#/influencer/approvals")
    page.get_by_role("heading", name="达人名单").wait_for()
    assert "审批单" not in page.inner_text("body")


def test_influencers_are_added_disabled_and_picked_from_recent_chats(console, page):
    page.goto(f"{console.base}/#/influencer/contacts")
    page.get_by_label("chat_id").fill("wxid_new")
    page.get_by_label("显示名").fill("新达人")
    page.get_by_role("button", name="加入名单").click()
    page.get_by_role("cell", name="wxid_new").wait_for()
    assert console.store.get_contact("wxid_new")["enabled"] is True

    page.get_by_role("switch", name="停用 新达人").click()
    page.get_by_text("已停用").wait_for()
    assert console.store.get_contact("wxid_new")["enabled"] is False

    recent = page.locator(".list-row", has_text="wxid_jie")
    recent.get_by_role("button", name="设为达人").click()
    recent.get_by_text("已在名单").wait_for()
    assert console.store.get_contact("wxid_jie")["enabled"] is True


def test_adding_someone_already_on_the_list_keeps_what_was_filled_in(console, page):
    page.goto(f"{console.base}/#/influencer/contacts")
    page.get_by_label("chat_id").fill("wxid_keep")
    page.get_by_role("button", name="加入名单").click()
    page.get_by_text("已加入名单").wait_for()
    contact = console.store.get_contact("wxid_keep")
    assert contact["enabled"] is True
    assert (contact["display_name"], contact["xhs_account"]) == ("老朋友", "xhs_keep")


def test_the_sidebar_shows_what_needs_attention_and_the_inbox_can_be_cleared(console, page):
    page.goto(f"{console.base}/#/influencer/inbox")
    page.locator(".ni-badge").wait_for()
    [item] = console.store.list_inbox()
    row = page.get_by_label(f"待处理事项 {item['id']}")
    row.get_by_text("老师好，这款下周上新").wait_for()
    row.get_by_text("回复草稿（没发出，等审核）").wait_for()
    row.get_by_role("button", name="已处理").click()
    page.get_by_text("已标为处理完").wait_for()
    assert console.store.list_inbox() == []


def test_a_creator_is_added_with_a_platform_number(console, page):
    page.goto(f"{console.base}/#/influencer/contacts")
    page.get_by_label("chat_id").fill("wxid_dy")
    page.get_by_role("group", name="平台").get_by_role("button", name="小红书").click()
    page.get_by_label("抖音号或小红书号").fill("xhs_no_1")
    page.get_by_role("button", name="加入名单").click()
    page.get_by_role("cell", name="wxid_dy", exact=True).wait_for()
    contact = console.store.get_contact("wxid_dy")
    assert (contact["platform"], contact["platform_no"]) == ("XHS", "xhs_no_1")
    page.get_by_text("小红书 xhs_no_1").wait_for()
