"""选款网站 on the Mac's side (tools/claude_relay/sample_site.py and the worker's three jobs), against the real site
(``sample_site/`` served on 127.0.0.1), the real relay and PRM service, and a fake ``node`` that answers like the 聚水潭
CLI: the catalogue by rule, a creator's link, her submission taken and acked, its sample order made AND audited after
one approval, with no photo check.  Nothing here leaves this machine; names, numbers and addresses are made up.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from platform_fakes import JST_ORDER, JST_SHOP_DY, JST_UID, FakeNode, jst_done, jst_preview, rule
from sample_site_fakes import Client, png_bytes, token_from_url
from test_prm_worker import CREATOR, TOKEN, fake_claude, run_one, settings, worker, workspace  # noqa: F401
from test_sample_site import submit

from sample_site.app import create_app, make_wsgi_server
from sample_site.config import Config
from src.prm import platforms
from src.prm.service import PrmService
from src.prm.store import APPROVED, EXECUTING, PENDING, SENT, PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

SITE_TOKEN = "site-admin-token-" + "s" * 32
ITEM_A, SKU_A1, SKU_A2 = "3000000000000000301", "3100000000000000311", "3100000000000000312"
ITEM_B, SKU_B1, SKU_B2 = "3000000000000000302", "3100000000000000321", "3100000000000000322"
ITEM_X, SKU_X1 = "3000000000000000309", "3100000000000000391"
ACCOUNT = "xiaomei_test01"
PHONE = "13800000000"


def products():
    """kb/products.json: the 抖店 products, each SKU with its 聚水潭 item code; one code is also sold by a 专属 product."""
    return {"version": 1, "synced_at": time.time(), "platforms": {"dy": {"ok": True, "items": [
        {"id": ITEM_X, "title": "【小美专属】测试渔夫帽", "price": "179.00", "skus": [
            {"id": SKU_X1, "name": "黑色/均码", "price": "179.00", "code": "T1001A-F"}]},
        {"id": ITEM_A, "title": "测试渔夫帽 软顶可折叠", "price": "199.00", "skus": [
            {"id": SKU_A1, "name": "黑色/均码", "price": "199.00", "code": "T1001A-F"},
            {"id": SKU_A2, "name": "米白/均码", "price": "199.00", "code": "T1001B-F"}]},
        {"id": ITEM_B, "title": "测试棒球帽 弯檐六片", "price": "159.00", "skus": [
            {"id": SKU_B1, "name": "藏青/M", "price": "159.00", "code": "T1002A-M"},
            {"id": SKU_B2, "name": "藏青/L", "price": "159.00", "code": "T1002A-L"}]}]}}}


def sku(code, name, colour, spec, stock, virtual=0):
    return {"itemCode": code, "name": name, "colour": colour, "spec": spec, "supplyPrice": 50, "stock": stock,
            "virtualQty": virtual, "availableStock": stock}


HOT = {"command": "goods", "total": 4, "hasNext": False, "rows": [
    {"styleCode": "T1001", "name": "测试渔夫帽", "sale30d": 50, "availableStock": 2017},
    {"styleCode": "T1010", "name": "测试不上架的帽", "sale30d": 90, "availableStock": 9},
    {"styleCode": "T1002", "name": "测试棒球帽", "sale30d": 80, "availableStock": 11},
    {"styleCode": "T1009", "name": "测试只有虚拟库存的帽", "sale30d": 30, "availableStock": 1000}]}
DETAILS = {
    "T1001": [sku("T1001A-F", "测试渔夫帽", "黑色", "均码", 1012, 1000),     # 12 really there
              sku("T1001B-F", "测试渔夫帽", "米白", "均码", 1000, 1000),     # only the platform's virtual 1000
              sku("T1001C-F", "测试渔夫帽", "雾蓝", "均码", 5)],             # in stock, but no 抖店 product sells it
    "T1002": [sku("T1002A-M", "测试棒球帽", "藏青", "M", 8), sku("T1002A-L", "测试棒球帽", "藏青", "L", 3)],
    "T1009": [sku("T1009A-F", "测试只有虚拟库存的帽", "黑色", "均码", 1000, 1000)],
}


def jst_rules(*more):
    return [rule(["goods", "--sort=hot30"], HOT),
            *[rule(["goods-detail", f"--style-code={code}"], {"command": "goods-detail", "styleCode": code,
                                                              "found": True, "skus": skus})
              for code, skus in DETAILS.items()],
            rule(["sample-create"], jst_preview(), execute=False),
            rule(["sample-create"], jst_done(), execute=True),
            *more]


AUDITED = {"command": "sample-audit", "outcome": "VERIFIED_SUCCESS", "willAudit": [JST_ORDER],
           "note": "读回 1 张都是待发货（或之后的状态）：审核通过了，仓库会发货"}


@pytest.fixture
def site(tmp_path):
    app = create_app(Config(admin_token=SITE_TOKEN, db_path=str(tmp_path / "site.db")))
    server = make_wsgi_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield SimpleNamespace(app=app, base="http://127.0.0.1:%d" % server.server_address[1], creator=Client(app),
                          admin=lambda method, path, payload=None: Client(app).api(method, path, payload,
                                                                                    token=SITE_TOKEN))
    server.shutdown()
    server.server_close()
    app.close()


@pytest.fixture
def bot(tmp_path):
    """The relay and the PRM service with the 选款网站 on and PRM_DRAFT_CONTEXT on too (the photo check that must not
    stand in a site order's way)."""
    cfg = SimpleNamespace(
        claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:", claude_relay_max_chars=1500,
        admin_wxid="wxid_admin", prm_copilot="on", prm_db=":memory:", prm_sample_site_min=5, prm_draft_context="on")
    texts = []
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), files_dir=tmp_path / "relay",
                        send=lambda c, t: True)
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), send=lambda c, t: texts.append((c, t)) or True,
                     send_file=lambda c, p: True)
    prm.register(CREATOR, "小美")
    relay.start()
    host, port = relay.address
    yield SimpleNamespace(relay=relay, prm=prm, url=f"http://{host}:{port}", texts=texts)
    relay.stop()


@pytest.fixture
def mac(bot, fake_claude, site, worker, workspace, tmp_path, monkeypatch):  # noqa: F811
    node = FakeNode(tmp_path / "mac", monkeypatch)
    node.rules(jst_rules(rule(["sample-audit", f"--order-no={JST_ORDER}"], AUDITED, execute=True)))
    (workspace / "kb" / "products.json").write_text(json.dumps(products(), ensure_ascii=False), encoding="utf-8")
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace),
                 **node.settings(jst_expect_uid=JST_UID, jst_shops={"dy": JST_SHOP_DY}, jst_logistics=["SF"]),
                 sample_site={"url": site.base, "token": SITE_TOKEN,
                              "catalog": {"source": "jst_hot30", "top_n": 3, "excluded": ["T1010"]}})
    module = worker.sample_site
    monkeypatch.setattr(module, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(module, "JST_GAP_SEC", 0)
    return SimpleNamespace(settings=s, node=node, claude=fake_claude, home=tmp_path / "home", site=module,
                           workspace=workspace)


def build(mac, worker, **conf):  # noqa: F811
    client, settings_ = mac.site.client_of(mac.settings)
    settings_["catalog"].update(conf)
    cfg = worker.platforms.config(mac.settings)
    built = mac.site.build_catalog(settings_, jst=mac.site.jst_runner(worker.platforms, cfg), products=products(),
                                   fetch=lambda url: png_bytes((30, 40, 90), 16), sleep=lambda sec: None)
    return client, built


def uploaded(mac, worker):  # noqa: F811
    client, built = build(mac, worker)
    return mac.site.upload_catalog(client, built, mac.home)


def deal_id(bot):
    return bot.prm.store.deal_of(bot.prm.store.party_by_wxid(CREATOR)["id"])["id"]


def round_now(bot, worker, mac):  # noqa: F811
    """The bot asks for a round now and the Mac's worker does it; the bot takes the answer in."""
    bot.prm.store.set_meta_value("site_sync_at", 0)
    job_id = bot.prm.sample_site.sweep()
    assert job_id is not None
    run_one(worker, bot, mac.settings)
    return bot.relay.store.get(job_id)


def waiting(bot):
    return bot.prm.store.deal_packets(deal_id(bot), statuses=(PENDING,))


# ── settings and the site's API ────────────────────────────────────────────

def test_the_settings_are_checked_and_the_key_never_goes_over_plain_http(worker, tmp_path):  # noqa: F811
    site_ = worker.sample_site
    with pytest.raises(site_.SiteError, match="还没配选款网站"):
        site_.settings_of({})
    with pytest.raises(site_.SiteError, match="https"):
        site_.settings_of({"sample_site": {"url": "http://xuan.example.test"}})
    conf = site_.settings_of({"sample_site": {"url": "https://xuan.example.test/"}})
    assert conf["url"] == "https://xuan.example.test" and conf["owner"] == "main" and conf["max_items"] == 3
    assert conf["audit"] is True and conf["catalog"]["source"] == "jst_hot30" and conf["catalog"]["top_n"] == 20
    for bad in ({"max_items": 11}, {"link_text": "没有链接的话"}, {"order_reply": "单号 {寄样单号}"}, {"owner": "两个人"},
                {"shop": "xhs"}):
        with pytest.raises(site_.SiteError):
            site_.settings_of({"sample_site": {"url": "https://xuan.example.test", **bad}})
    with pytest.raises(site_.SiteError, match="管理密钥"):
        site_.read_token(conf)
    key = tmp_path / "site.token"
    key.write_text("k" * 40 + "\n", encoding="utf-8")
    assert site_.read_token({**conf, "token_file": str(key)}) == "k" * 40
    # the words a creator reads keep to the kb's rules
    for text in (site_.LINK_TEXT, site_.ORDER_REPLY):
        assert "老师" in text and "您" in text
        for word in ("送", "免费", "包邮", "缺货", "没货", "补货", "查一下", "聚水潭", "寄样单", "佣金", "顶"):
            assert word not in text


def test_a_wrong_key_and_a_site_that_is_away_are_said_as_they_are(site, worker):  # noqa: F811
    site_ = worker.sample_site
    with pytest.raises(site_.SiteError) as e:
        site_.SiteClient(site.base, "w" * 40).status()
    assert e.value.verdict == "unauthorized"
    with pytest.raises(site_.SiteError) as e:
        site_.SiteClient("http://127.0.0.1:9", SITE_TOKEN, timeout=2).status()
    assert e.value.verdict == "unreachable"
    assert site_.SiteClient(site.base, SITE_TOKEN).status()["submissions"] == {"new": 0, "taken": 0, "acked": 0}


def _serving(handler):
    import http.server

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_the_client_never_follows_a_redirect_so_the_key_goes_nowhere_else(worker, code):  # noqa: F811
    """Security review 2026-11-13 #9: urllib follows a redirect and sends the request again with its Authorization
    header, to another host over plain http too.  The client refuses every redirect: the other server hears nothing."""
    import http.server

    heard = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            heard.append((self.command, self.path, self.headers.get("Authorization")))
            body = b'{"ok": true, "submissions": [], "catalog": {}, "links": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = do_PUT = _answer

        def log_message(self, *args):
            pass

    other = _serving(Elsewhere)

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(code)
            self.send_header("Location", "http://127.0.0.1:%d%s" % (other.server_address[1], self.path))
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_GET = do_POST = do_PUT = _answer

        def log_message(self, *args):
            pass

    first = _serving(Redirecting)
    try:
        site_ = worker.sample_site
        client = site_.SiteClient("http://127.0.0.1:%d" % first.server_address[1], SITE_TOKEN, timeout=5)
        for call in (client.status, lambda: client.pull("main"), lambda: client.ack("main", [1]),
                     lambda: client.put_image("a" * 32, b"x")):
            with pytest.raises(site_.SiteError) as e:
                call()
            assert e.value.verdict == "refused" and "跳转" in str(e.value)
        with pytest.raises(site_.SiteError):
            client.picture("a" * 32)
        assert site_.fetch_picture("https://127.0.0.1:%d/x.jpg" % first.server_address[1], ("127.0.0.1",), 2) == b""
        assert heard == []                                                   # nobody else ever saw the key
    finally:
        for server in (first, other):
            server.shutdown()
            server.server_close()


# ── the catalogue ──────────────────────────────────────────────────────────

def test_the_catalogue_is_the_top_sellers_really_in_stock_that_the_shop_sells(mac, worker, site):  # noqa: F811
    """聚水潭's 近 30 天热卖 by sales, the excluded style out; of each style only the SKUs with real stock (库存 − 虚拟库存)
    that a 抖店 product sells, from the plain product rather than a 专属 one; a style with nothing left is not shown.
    Only read-only commands run, each with --expect-uid."""
    client, built = build(mac, worker)
    assert [(s["code"], s["name"]) for s in built["doc"]["styles"]] == [("T1002", "测试棒球帽"), ("T1001", "测试渔夫帽")]
    by_code = {s["code"]: s for s in built["doc"]["styles"]}
    assert [(c["name"], [(z["label"], z["sku"]) for z in c["sizes"]]) for c in by_code["T1002"]["colours"]] == [
        ("藏青", [("M", "T1002A-M"), ("L", "T1002A-L")])]
    assert [(c["name"], [z["sku"] for z in c["sizes"]]) for c in by_code["T1001"]["colours"]] == [("黑色", ["T1001A-F"])]
    assert built["map"]["T1001A-F"] == {"shop": "dy", "item_id": ITEM_A, "sku_id": SKU_A1, "style": "T1001",
                                        "name": "测试渔夫帽", "colour": "黑色", "size": "均码"}
    assert set(built["map"]) == {"T1001A-F", "T1002A-M", "T1002A-L"}
    notes = " ".join(built["notes"])
    assert "T1001：1 个编码抖店商品目录里没有" in notes and "T1009" in notes
    text = json.dumps(built["doc"], ensure_ascii=False)
    for private in ("stock", "sale", "price", ITEM_A, SKU_A1, "1012"):       # nothing of stock, sales, prices, shop ids
        assert private not in text
    calls = mac.node.calls()
    assert [c["argv"][1] for c in calls] == ["goods", "goods-detail", "goods-detail", "goods-detail"]
    assert all(f"--expect-uid={JST_UID}" in c["argv"] and "--execute" not in c["argv"] for c in calls)
    assert not any("T1010" in " ".join(c["argv"]) for c in calls)            # the excluded style is not even read
    assert (mac.home / "platform" / "cli.lock").exists()
    assert not (mac.home / "platform" / "sample-site" / "catalog-map.json").exists()      # nothing written by a build


def test_pinned_styles_come_first_and_a_hand_kept_list_needs_no_ranking(mac, worker):  # noqa: F811
    _, built = build(mac, worker, pinned=["T1001"], top_n=1)
    assert [s["code"] for s in built["doc"]["styles"]] == ["T1001", "T1002"]
    mac.node.log.unlink()
    _, built = build(mac, worker, source="manual", styles=["T1001"], names={"T1001": "渔夫帽·经典款"},
                     descs={"T1001": "软顶可折叠"})
    assert [(s["code"], s["name"], s.get("desc")) for s in built["doc"]["styles"]] == [
        ("T1001", "渔夫帽·经典款", "软顶可折叠")]
    assert [c["argv"][1] for c in mac.node.calls()] == ["goods-detail"]      # no ranking was read
    site_ = worker.sample_site
    with pytest.raises(site_.SiteError, match="tb_sycm"):
        build(mac, worker, source="tb_sycm")                                 # said plainly: not readable today


def test_a_size_reads_as_a_person_would_say_it(worker):  # noqa: F811
    label = worker.sample_site.size_label
    assert label("尺码:F(52-60cm)") == "均码(52-60cm)" and label("尺码：M(56-58cm)") == "M(56-58cm)"
    assert label("F") == "均码" and label("") == "均码" and label(None) == "均码" and label("L") == "L"
    assert label("Free") == "Free" and label("规格: 均码") == "均码"


def test_real_stock_is_stock_minus_the_virtual_stock(worker):  # noqa: F811
    real = worker.sample_site.real_stock
    assert real({"stock": 1012, "virtualQty": 1000, "availableStock": 1012}) == 12
    assert real({"stock": 1000, "virtualQty": 1000}) == 0
    assert real({"stock": 5, "virtualQty": None}) == 5 and real({"availableStock": 7}) == 7
    assert real({"stock": None, "virtualQty": 3}) is None and real({}) is None


def test_the_upload_sends_each_picture_once_and_writes_the_map(mac, worker, site):  # noqa: F811
    answer = uploaded(mac, worker)
    assert answer["styles"] == 2 and answer["skus"] == 3 and answer["images_sent"] == 1     # (one picture, three uses)
    again = uploaded(mac, worker)
    assert again["images_sent"] == 0 and again["version"] == answer["version"] + 1
    path = mac.home / "platform" / "sample-site" / "catalog-map.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert set(mac.site.load_map(mac.home)) == {"T1001A-F", "T1002A-M", "T1002A-L"}
    shown = site.admin("GET", "catalog").json()
    assert [s["name"] for s in shown["styles"]] == ["测试棒球帽", "测试渔夫帽"]


def test_her_choice_becomes_the_shops_products_and_sku_ids(mac, worker):  # noqa: F811
    site_ = worker.sample_site
    uploaded(mac, worker)
    conf = site_.settings_of(mac.settings)
    order = {"id": 7, "account": ACCOUNT, "items": [{"sku": "T1001A-F"}, {"sku": "T1002A-M"}, {"sku": "T1002A-L"}],
             "receiver": {"name": "小美", "phone": "〈手机1〉", "province": "浙江省", "city": "杭州市", "district": "西湖区",
                          "address": "测试路 1 号"}}
    action = site_.order_action(order, site_.load_map(mac.home), conf)
    assert action["items"] == [{"item_id": ITEM_A, "skus": [{"sku_id": SKU_A1, "qty": 1}]},
                               {"item_id": ITEM_B, "skus": [{"sku_id": SKU_B1, "qty": 1}, {"sku_id": SKU_B2, "qty": 1}]}]
    assert action["creator_account"] == ACCOUNT and action["receiver"]["phone"] == "〈手机1〉"
    assert platforms.check_action(action, catalog=products())["shop"] == "dy"        # the Mac's own check takes it
    for broken, why in (({**order, "account": ""}, "抖音号"), ({**order, "items": [{"sku": "T9999Z-F"}]}, "目录表里没有"),
                        ({**order, "items": []}, "没有选款")):
        with pytest.raises(site_.SiteError, match=why):
            site_.order_action(broken, site_.load_map(mac.home), conf)


# ── the audit step (src/prm/platforms.py) ──────────────────────────────────

ACTION = {"action": "jst.sample_order", "shop": "dy", "creator_platform": "dy", "creator_account": ACCOUNT,
          "items": [{"item_id": ITEM_A, "skus": [{"sku_id": SKU_A1, "qty": 1}]}],
          "receiver": {"name": "小美", "phone": PHONE, "province": "浙江省", "city": "杭州市", "district": "西湖区",
                       "address": "测试路 1 号"}}


def run_plan(mac, worker, tmp_path, key, *audit_rules, steps=("create", "audit")):  # noqa: F811
    mac.node.rules(jst_rules(*audit_rules))
    cfg = worker.platforms.config(mac.settings)
    return worker.platforms.execute(ACTION, cfg, plan={"steps": list(steps)}, key=key, ledger=tmp_path / "ledger",
                                    catalog=products())


def test_only_a_preview_told_to_plans_the_audit(mac, worker):  # noqa: F811
    cfg = worker.platforms.config(mac.settings)
    plain = worker.platforms.preview(ACTION, cfg, catalog=products())
    assert plain["verdict"] == "ready" and plain["run"] == {"steps": ["create"]}
    audited = worker.platforms.preview(ACTION, cfg, catalog=products(), context={"audit": True})
    assert audited["verdict"] == "ready" and audited["run"] == {"steps": ["create", "audit"]}
    assert {"label": "执行时还会做", "value": platforms.AUDIT_FACT} in audited["facts"]
    assert platforms.check_block(audited)["run"] == {"steps": ["create", "audit"]}     # the bot takes the plan
    with pytest.raises(platforms.ActionError):
        platforms.check_plan(platforms.check_action(ACTION), {"steps": ["audit"]})


def test_the_order_is_made_then_audited_once_each(mac, worker, tmp_path):  # noqa: F811
    record = run_plan(mac, worker, tmp_path, "a" * 32, rule(["sample-audit", f"--order-no={JST_ORDER}"], AUDITED,
                                                            execute=True))
    assert record["outcome"] == platforms.VERIFIED and record["created"]["sample_order_no"] == JST_ORDER
    assert [(s["name"], s["outcome"]) for s in record["steps"]] == [("create", platforms.VERIFIED),
                                                                    ("audit", platforms.VERIFIED)]
    writes = [c["argv"] for c in mac.node.calls() if "--execute" in c["argv"]]
    assert [a[1] for a in writes] == ["sample-create", "sample-audit"]
    assert f"--order-no={JST_ORDER}" in writes[1] and f"--expect-uid={JST_UID}" in writes[1]
    again = worker.platforms.execute(ACTION, worker.platforms.config(mac.settings), plan={"steps": ["create", "audit"]},
                                     key="a" * 32, ledger=tmp_path / "ledger", catalog=products())
    assert again["outcome"] == "ALREADY_RUN" and len([c for c in mac.node.calls() if "--execute" in c["argv"]]) == 2


def test_an_order_audited_already_needs_nothing_more(mac, worker, tmp_path):  # noqa: F811
    record = run_plan(mac, worker, tmp_path, "b" * 32, rule(["sample-audit"], {"outcome": "ALREADY_DONE"}, execute=True))
    assert record["outcome"] == platforms.VERIFIED and record["steps"][1]["outcome"] == "NO_CHANGE_NEEDED"


@pytest.mark.parametrize("answer, exit_code, outcome", [
    ({"outcome": "FAILED", "note": "平台没有审核通过：没有审核权限"}, 1, "PARTIAL"),
    (None, 77, "PARTIAL"), ({"outcome": "ACCEPTED_UNVERIFIED", "note": "读回时出错"}, 1, "ACCEPTED_UNVERIFIED"),
    ({"outcome": "INDETERMINATE", "note": "没拿到应答"}, 1, "INDETERMINATE")])
def test_an_audit_that_did_not_go_through_says_the_order_exists_and_never_to_make_another(mac, worker, tmp_path,  # noqa: F811
                                                                                         answer, exit_code, outcome):
    record = run_plan(mac, worker, tmp_path, "c" * 32, rule(["sample-audit"], answer, execute=True, exit=exit_code,
                                                            stderr="" if answer else "[NOT_LOGGED_IN] 登录失效\n"))
    assert record["outcome"] == outcome and record["created"]["sample_order_no"] == JST_ORDER
    said = " ".join(record["problems"])
    assert f"聚水潭寄样单 {JST_ORDER} 已经建好" in said and "不要再建一张" in said
    assert platforms.check_record(record)["outcome"] == outcome              # the bot reads it as it is


def test_an_order_that_was_not_made_is_never_audited(mac, worker, tmp_path):  # noqa: F811
    mac.node.rules([rule(["sample-create"], {**jst_done("FAILED", order=""), "blocked": ["寄过这个商品"]}, execute=True,
                         exit=1)])
    record = worker.platforms.execute(ACTION, worker.platforms.config(mac.settings),
                                      plan={"steps": ["create", "audit"]}, key="d" * 32, ledger=tmp_path / "ledger",
                                      catalog=products())
    assert record["outcome"] != platforms.VERIFIED
    assert [c["argv"][1] for c in mac.node.calls()] == ["sample-create"]


# ── end to end ─────────────────────────────────────────────────────────────

def test_from_her_link_to_an_audited_order_with_one_approval_and_no_photo_check(mac, worker, bot, site):  # noqa: F811
    uploaded(mac, worker)
    party = bot.prm.store.party_by_wxid(CREATOR)
    assert bot.prm.store.add_creator_link(party["id"], ACCOUNT, source="console")

    # 1. the operator asks for her link: the Mac makes it on the site, no model; its sentence waits for a person
    assert bot.prm.deal_action("site_link", {"deal_id": deal_id(bot)})["ok"]
    run_one(worker, bot, mac.settings)
    assert mac.claude.calls() == []
    [link_packet] = waiting(bot)
    assert link_packet["text"].startswith("老师，我们把在售的款式放在这个页面了") and site.base + "/s/" in link_packet["text"]
    token = token_from_url(link_packet["text"].split()[-1])
    [link] = site.admin("GET", "links?owner=main").json()["links"]
    assert link["ref"] == f"p{party['id']}" and link["max_items"] == 3 and link["state"] == "unused"
    assert bot.prm.decide(link_packet["id"], "send")["ok"]
    bot.prm.send_approved()
    assert token in bot.texts[-1][1]

    # 2. she opens it on her phone, picks two hats and types the receiver
    page = site.creator.get(f"/s/{token}")
    assert page.code == 200 and "最多选 3 顶" in page.text and "测试渔夫帽" in page.text and "抖音号" not in page.text
    done = submit(site.creator, f"/s/{token}", ["T1001A-F", "T1002A-M"])
    assert done.code in (200, 303)
    # (whoever holds the link sees only the phone's last digits and the city, before anybody took it too: review #10)
    mine = site.creator.get(f"/s/{token}").text
    assert "尾号 0000 · 浙江省杭州市" in mine and PHONE not in mine and "测试路" not in mine and "小美" not in mine

    # 3. a round: the Mac takes it (she can no longer change it), the bot queues its order's job; the round's job
    #    keeps nothing of the receiver
    pulled = round_now(bot, worker, mac)
    assert PHONE not in pulled["result_text"] and "测试路" not in pulled["result_text"]
    assert site.admin("GET", "status").json()["submissions"] == {"new": 0, "taken": 1, "acked": 0}
    assert "已经在安排" in site.creator.get(f"/s/{token}").text

    # 4. the Mac drafts the order without the model and without any photo check; one draft waits, ready
    run_one(worker, bot, mac.settings)
    assert mac.claude.calls() == []
    [packet] = waiting(bot)
    assert packet["tool"] == "jst.sample_order" and packet["job_id"] is None
    assert packet["meta"]["site_order"]["photo_check"] is False and "draft_context" not in packet["meta"]
    preview = packet["platform"]["preview"]
    assert preview["verdict"] == "ready" and preview["run"] == {"steps": ["create", "audit"]}
    assert packet["platform"]["action"]["items"] == [{"item_id": ITEM_A, "skus": [{"sku_id": SKU_A1, "qty": 1}]},
                                                     {"item_id": ITEM_B, "skus": [{"sku_id": SKU_B1, "qty": 1}]}]
    assert packet["platform"]["action"]["receiver"]["phone"].startswith("〈手机")
    sheets = packet["meta"].get("operator_files") or []
    assert [f["name"].startswith("选款单-") for f in sheets] == [True]       # her hats' pictures, for the operators
    spec = [c for c in mac.node.calls() if c["argv"][1] == "sample-create"][-1]["spec"]
    assert spec["receiver"]["phone"] is None and spec["daren"] == {"platform": "DY", "account": ACCOUNT}
    assert not list((mac.home / "platform" / "sample-site" / "sheets").glob("*"))        # the Mac keeps no copy

    # 5. one approval: the order is made with the number put back, then audited; only then she hears from us
    before = len(bot.texts)
    assert bot.prm.decide(packet["id"], "execute")["ok"]
    assert bot.prm.store.packet(packet["id"])["status"] == EXECUTING
    run_one(worker, bot, mac.settings)
    writes = [c for c in mac.node.calls() if "--execute" in c["argv"]]
    assert [c["argv"][1] for c in writes] == ["sample-create", "sample-audit"]
    assert writes[0]["spec"]["receiver"] == {"name": "小美", "phone": PHONE, "state": "浙江省", "city": "杭州市",
                                             "district": "西湖区", "address": "测试路 1 号"}
    assert [(g["product"], g["sku"], g["qty"]) for g in writes[0]["spec"]["goods"]] == [(ITEM_A, SKU_A1, 1),
                                                                                         (ITEM_B, SKU_B1, 1)]
    assert bot.prm.store.packet(packet["id"])["status"] == APPROVED
    bot.prm.send_approved()
    assert bot.prm.store.packet(packet["id"])["status"] == SENT
    assert bot.texts[before:] == [(CREATOR, "老师，您选的款我们收到啦，已经在安排了～")]

    # 6. the next round tells the site the bot has it: the site keeps only masked remnants of the receiver
    round_now(bot, worker, mac)
    assert site.admin("GET", "status").json()["submissions"] == {"new": 0, "taken": 0, "acked": 1}
    assert bot.prm.store.site_order(1)["acked"] == 1
    page = site.creator.get(f"/s/{token}").text
    assert PHONE not in page and "测试路" not in page and "小美" not in page and "西湖区" not in page
    assert "尾号 0000 · 浙江省杭州市" in page                       # (after the ack: the site itself keeps no more than this)
    [kept] = [tuple(r) for r in __import__("sqlite3").connect(site.app.config.db_path).execute(
        "SELECT receiver FROM submissions")]
    assert json.loads(kept[0]) == {"name": "", "phone": "0000", "province": "浙江省", "city": "杭州市", "district": "",
                                   "address": ""}
    # and nothing of hers was written to the worker's outbox on the way
    assert not any(PHONE in p.read_text(encoding="utf-8", errors="ignore")
                   for p in (mac.home).rglob("*") if p.is_file() and p.suffix in (".json", ".log", ".txt"))


def test_a_site_that_cannot_be_reached_stops_the_round_and_nothing_is_lost(mac, worker, bot, site):  # noqa: F811
    mac.settings["sample_site"] = {**mac.settings["sample_site"], "token": "w" * 40}
    job = round_now(bot, worker, mac)
    assert json.loads(job["result_text"])["stopped"]["verdict"] == "unauthorized"
    assert bot.prm.sample_site.status()["last"]["stopped"]["verdict"] == "unauthorized"


def test_a_link_asked_of_a_mac_without_the_site_fails_without_a_draft(mac, worker, bot):  # noqa: F811
    del mac.settings["sample_site"]
    assert bot.prm.deal_action("site_link", {"deal_id": deal_id(bot)})["ok"]
    run_one(worker, bot, mac.settings)
    assert waiting(bot) == [] and mac.claude.calls() == []
    assert "选款链接没建成" in " ".join(n["text"] for n in bot.prm.store.deal(deal_id(bot))["notices"])
