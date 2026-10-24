"""控制台「达人审批」页的浏览器测试：启动真实的 Web 服务，用无头浏览器点页面。

和 tests/test_functional.py 一样需要 Playwright（没装就跳过）和构建好的 ui/dist。
配置文件和数据库都是临时的：WEBOT_ENV_FILE 指向临时 .env，里面的 DB_PATH 指向临时库；
并且打开模拟模式，不碰真实的配置、数据库、聚水潭和小红书。
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db.schema import initialize_db
from src.influencer.store import SOURCE_BOT, SOURCE_CONSOLE, InfluencerStore
from src.influencer.types import GateStatus
from test_functional import _find_free_port, _start_server
from test_influencer_store import NEXT_YEAR, make_packet

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
    for packet_id, xhs_account in (("GATE-OK", "xhs_mei"), ("GATE-NO", "xhs_mei"), ("GATE-NOXHS", "")):
        store.add_approval(
            make_packet(packet_id, chat_id="wxid_mei", xhs_account=xhs_account,
                        qualification="达标", expires_at=NEXT_YEAR),
            source=SOURCE_BOT,
        )
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
        mp.setenv("INFLUENCER_MOCK", "1")
        store = seed(tmp / "messages.db")
        port = _find_free_port()
        _start_server(port)
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            yield SimpleNamespace(base=f"http://127.0.0.1:{port}", browser=browser, store=store)
            browser.close()


@pytest.fixture
def page(console):
    context = console.browser.new_context(
        viewport={"width": 1440, "height": 900}, permissions=["clipboard-read", "clipboard-write"],
    )
    page = context.new_page()
    yield page
    context.close()


def card(page, packet_id):
    return page.get_by_role("region", name=f"审批单 {packet_id}", exact=True)


def test_the_sidebar_leads_to_both_sub_pages(console, page):
    page.goto(console.base)
    page.get_by_role("button", name="达人审批").click()
    page.get_by_role("heading", name="达人审批").wait_for()
    assert page.url.endswith("#/influencer/approvals")
    page.get_by_role("button", name="达人名单").click()
    page.get_by_role("heading", name="达人名单").wait_for()


def test_an_approval_runs_and_its_message_can_be_copied(console, page):
    page.goto(f"{console.base}/#/influencer/approvals")
    card(page, "GATE-OK").wait_for()
    assert "模拟模式已打开" in page.inner_text("body")
    assert "想申请眼霜样品" in card(page, "GATE-OK").inner_text(), "达人原话要显示出来（R5）"

    card(page, "GATE-OK").get_by_role("button", name="通过").click()
    page.get_by_role("button", name="确认通过并执行").click()
    page.get_by_text("已执行：出库单").wait_for()
    assert console.store.get_approval("GATE-OK").status == GateStatus.FULFILLED

    page.get_by_role("button", name="已执行").click()
    done = card(page, "GATE-OK")
    done.get_by_text("发给达人的确认消息").wait_for()
    done.get_by_role("button", name="复制").click()
    page.get_by_text("已复制").wait_for()
    tracking = console.store.get_approval("GATE-OK").tracking_number
    assert tracking in page.evaluate("navigator.clipboard.readText()")


def test_a_rejection_keeps_the_reason(console, page):
    page.goto(f"{console.base}/#/influencer/approvals")
    card(page, "GATE-NO").get_by_role("button", name="驳回").click()
    dialog = page.get_by_role("dialog")
    dialog.locator("textarea").fill("粉丝画像不匹配")
    dialog.get_by_role("button", name="驳回").click()
    page.get_by_text("已驳回。记得").wait_for()
    assert console.store.get_approval("GATE-NO").status == GateStatus.REJECTED
    assert console.store.events_for(["GATE-NO"])["GATE-NO"][-1]["detail"] == "粉丝画像不匹配"


def test_approval_waits_for_an_xhs_account(console, page):
    page.goto(f"{console.base}/#/influencer/approvals")
    pending = card(page, "GATE-NOXHS")
    pending.get_by_text("缺小红书账号").wait_for()
    assert pending.get_by_role("button", name="通过").is_disabled()
    pending.get_by_label("小红书账号").fill("xhs_late")
    assert pending.get_by_role("button", name="通过").is_enabled()
    assert console.store.get_approval("GATE-NOXHS").status == GateStatus.PENDING


def test_influencers_are_added_disabled_and_picked_from_recent_chats(console, page):
    page.goto(f"{console.base}/#/influencer/contacts")
    page.get_by_label("chat_id").fill("wxid_new")
    page.get_by_label("显示名").fill("新达人")
    page.get_by_role("button", name="加入名单").click()
    page.get_by_role("cell", name="wxid_new").wait_for()
    assert console.store.is_influencer("wxid_new")

    page.get_by_role("switch", name="停用 新达人").click()
    page.get_by_text("已停用").wait_for()
    assert not console.store.is_influencer("wxid_new")

    recent = page.locator(".list-row", has_text="wxid_jie")
    recent.get_by_role("button", name="设为达人").click()
    recent.get_by_text("已在名单").wait_for()
    assert console.store.is_influencer("wxid_jie")
