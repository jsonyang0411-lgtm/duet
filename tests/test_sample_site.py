"""Tests for sample_site/ (the creator sample-picking website).

All in-process against the WSGI app (no sockets) except the server and
script tests at the end, which use 127.0.0.1 only. Data are fake.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import http.client
from urllib.parse import urlsplit

import pytest

from sample_site import app as app_module
from sample_site import pages
from sample_site.app import create_app, make_wsgi_server
from sample_site.catalog import sniff_image
from sample_site.config import Config, ConfigError
from sample_site.forms import MESSAGES, PROVINCES, mask_receiver, normalise_phone
from sample_site.security import safe_path, scrub

from sample_site_fakes import (TOKEN, Client, checked_picks, details, fake_catalogue, hidden_fields,
                               key_of, png_bytes, upload_fake_catalogue, visible_text)

SITE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_site")
DAY = 86400.0
CSP = ("default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


class Clock(object):
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_app(tmp_path, clock):
    made = []

    def _make(**kw):
        kw.setdefault("db_path", str(tmp_path / "data" / "site.db"))
        app = create_app(Config(admin_token=TOKEN, clock=clock, **kw))
        made.append(app)
        return app

    yield _make
    for app in made:
        app.close()


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return Client(app)


@pytest.fixture
def shop(client):
    client.keys = upload_fake_catalogue(client)
    return client


def new_link(client, **body):
    body.setdefault("ref", "deal:1")
    resp = client.api("POST", "links", body)
    assert resp.code == 200, resp.text
    return resp.json()


def path_of(link):
    return urlsplit(link["url"]).path


def review(client, path, picks, ip=None, **fields):
    form = [("action", "review")] + [("pick", p) for p in picks] + list(details(**fields).items())
    return client.post_form(path, form, ip=ip)


def submit(client, path, picks, **fields):
    resp = review(client, path, picks, **fields)
    assert resp.code == 200 and "确认提交" in resp.text, resp.text[:400]
    return client.post_form(path, hidden_fields(resp.text) + [("action", "submit")])


def pull(client, owner="main"):
    resp = client.api("POST", "submissions/pull", {"owner": owner})
    assert resp.code == 200, resp.text
    return resp.json()["submissions"]


def db_rows(app, sql, args=()):
    conn = sqlite3.connect(app.config.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# -- configuration -------------------------------------------------------------


def test_config_refuses_missing_or_short_token(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_env({})
    with pytest.raises(ConfigError):
        Config.from_env({"SAMPLE_SITE_ADMIN_TOKEN": "short"})
    with pytest.raises(ConfigError):
        Config.from_env({"SAMPLE_SITE_ADMIN_TOKEN": "a b" * 20})
    with pytest.raises(ValueError):
        create_app(Config(admin_token="x" * 31, db_path=str(tmp_path / "x.db")))
    assert not (tmp_path / "x.db").exists()


def test_config_from_env_and_token_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    cfg = Config.from_env({"SAMPLE_SITE_ADMIN_TOKEN_FILE": str(token_file)})
    assert cfg.admin_token == TOKEN
    assert (cfg.db_path, cfg.host, cfg.port, cfg.retention_days, cfg.trust_proxy, cfg.brand) == (
        "./data/sample_site.db", "127.0.0.1", 8080, 90, False, "ACE7CN")
    cfg = Config.from_env({
        "SAMPLE_SITE_ADMIN_TOKEN": TOKEN, "SAMPLE_SITE_DB": "/srv/x.db", "SAMPLE_SITE_HOST": "0.0.0.0",
        "SAMPLE_SITE_PORT": "9000", "SAMPLE_SITE_PUBLIC_URL": "https://xuan.example.com/",
        "SAMPLE_SITE_RETENTION_DAYS": "30", "SAMPLE_SITE_TRUST_PROXY": "1", "SAMPLE_SITE_BRAND": "TESTBRAND",
    })
    assert (cfg.db_path, cfg.host, cfg.port, cfg.public_url, cfg.retention_days, cfg.trust_proxy,
            cfg.brand, cfg.https) == ("/srv/x.db", "0.0.0.0", 9000, "https://xuan.example.com", 30, True,
                                      "TESTBRAND", True)
    assert TOKEN not in repr(cfg)
    with pytest.raises(ConfigError):
        Config.from_env({"SAMPLE_SITE_ADMIN_TOKEN": TOKEN, "SAMPLE_SITE_PUBLIC_URL": "xuan.example.com"})
    with pytest.raises(ConfigError):
        Config.from_env({"SAMPLE_SITE_ADMIN_TOKEN": TOKEN, "SAMPLE_SITE_PORT": "eighty"})


def test_db_file_is_private_and_wal(app):
    if sys.platform != "win32":
        assert os.stat(app.config.db_path).st_mode & 0o777 == 0o600
    assert db_rows(app, "PRAGMA journal_mode")[0][0] == "wal"


# -- public basics and headers -------------------------------------------------


def test_index_healthz_robots_and_unknown(client):
    index = client.get("/")
    assert index.code == 200 and "请使用运营发给您的专属链接" in index.text
    assert "<a " not in index.text  # nothing is listed
    assert client.get("/healthz").text == "ok"
    robots = client.get("/robots.txt")
    assert robots.text == "User-agent: *\nDisallow: /\n"
    assert client.get("/favicon.ico").code == 204
    assert client.get("/nope").code == 404
    assert client.get("/static/nope.js").code == 404
    assert client.request("POST", "/healthz").code == 405
    assert client.request("HEAD", "/healthz").body == b""


def _check_common_headers(resp):
    assert resp.header("Content-Security-Policy") == CSP
    assert resp.header("X-Content-Type-Options") == "nosniff"
    assert resp.header("Referrer-Policy") == "no-referrer"
    assert resp.header("X-Robots-Tag") == "noindex, nofollow"


def test_security_headers_everywhere(shop):
    link = new_link(shop)
    token_page = shop.get(path_of(link))
    admin = shop.api("GET", "status")
    img = shop.get("/img/" + shop.keys["a"])
    css = shop.get(shop.app.static_urls["css"])
    for resp in (shop.get("/"), token_page, admin, img, css, shop.get("/healthz"), shop.get("/nope")):
        _check_common_headers(resp)
        assert resp.header("Strict-Transport-Security") is None
    assert token_page.header("Cache-Control") == "no-store"
    assert admin.header("Cache-Control") == "no-store"
    assert img.header("Cache-Control") == "public, max-age=31536000, immutable"
    assert img.header("Content-Type") == "image/png"
    assert css.header("Cache-Control") == "public, max-age=31536000, immutable"
    assert css.header("Content-Type").startswith("text/css")
    # no inline scripts or styles anywhere (strict CSP)
    for text in (token_page.text, shop.get("/").text):
        assert "<style" not in text and " style=" not in text
        assert re.findall(r"<script(?![^>]*\bsrc=)", text) == []
    assert shop.get("/img/" + shop.keys["a"], headers={"If-None-Match": '"%s"' % shop.keys["a"]}).code == 304


def test_hsts_when_public_url_is_https(make_app):
    app = make_app(public_url="https://xuan.example.com")
    resp = Client(app).get("/")
    assert resp.header("Strict-Transport-Security") == "max-age=31536000"


# -- admin authentication ------------------------------------------------------


def test_admin_requires_the_token(client):
    missing = client.api("GET", "status", token=None)
    wrong = client.api("GET", "status", token="wrong-token-" + "y" * 30)
    assert missing.code == wrong.code == 401
    assert missing.body == wrong.body
    assert missing.json() == {"ok": False, "error": "unauthorized", "message": "missing or wrong admin token"}
    right = client.api("GET", "status")
    assert right.code == 200
    body = right.json()
    assert body["ok"] is True
    assert body["catalog"] == {"version": 0, "styles": 0, "updated_at": None}
    assert body["links"] == {"active": 0}
    assert body["submissions"] == {"new": 0, "taken": 0, "acked": 0}
    lower = client.request("GET", "/admin/api/status", headers={"Authorization": "bearer " + TOKEN})
    assert lower.code == 200
    basic = client.request("GET", "/admin/api/status", headers={"Authorization": "Basic " + TOKEN})
    assert basic.code == 401
    assert client.get("/admin/api/status?token=" + TOKEN).code == 401
    assert client.api("GET", "nope").code == 404
    assert client.api("DELETE", "status").code == 405


def test_admin_token_compared_in_constant_time(client, monkeypatch):
    calls = []
    real = app_module.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(app_module.hmac, "compare_digest", spy)
    assert client.api("GET", "status").code == 200
    assert client.api("GET", "status", token="z" * 40).code == 401
    assert client.api("GET", "status", token=None).code == 401
    assert len(calls) == 3
    assert all(b == TOKEN.encode() for _a, b in calls)


def test_admin_failures_are_rate_limited_per_ip(make_app):
    client = Client(make_app(rate_admin_fail=(3, 60)))
    for _ in range(3):
        assert client.api("GET", "status", token="w" * 40).code == 401
    blocked = client.api("GET", "status")
    assert blocked.code == 429 and blocked.json()["error"] == "rate_limited"
    assert client.api("GET", "status", ip="198.51.100.9").code == 200


# -- images --------------------------------------------------------------------


def _put_image(client, key, data):
    return client.request("PUT", "/admin/api/images/" + key, body=data,
                          headers={"Authorization": "Bearer " + TOKEN}, content_type="image/png")


def test_image_upload_rules(client):
    data = png_bytes((1, 2, 3))
    key = key_of(data)
    first = _put_image(client, key, data)
    assert first.code == 200 and first.json() == {"ok": True, "key": key, "created": True}
    assert _put_image(client, key, data).json()["created"] is False
    assert client.api("GET", "images").json() == {"ok": True, "keys": [key]}
    served = client.get("/img/" + key)
    assert served.code == 200 and served.body == data
    other = png_bytes((9, 9, 9))
    mismatch = _put_image(client, "0" * 32, other)
    assert mismatch.code == 400 and mismatch.json()["error"] == "key_mismatch"
    gif = b"GIF89a" + b"\x00" * 40
    assert _put_image(client, key_of(gif), gif).json()["error"] == "bad_image"
    assert _put_image(client, "NOT-A-KEY", data).json()["error"] == "bad_key"
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (400 * 1024)
    too_big = _put_image(client, key_of(big), big)
    assert too_big.code == 413 and too_big.json()["error"] == "too_large"
    assert client.get("/img/" + "f" * 32).code == 404
    assert client.get("/img/../etc").code == 404


def test_sniff_image_formats():
    assert sniff_image(png_bytes(size=12)) == ("image/png", 12, 12)
    jfif = b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    jpeg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + jfif + b"\xff\xc0"
            + struct.pack(">HBHH", 17, 8, 300, 400) + b"\x03" + b"\x00" * 9 + b"\xff\xd9")
    assert sniff_image(jpeg) == ("image/jpeg", 400, 300)
    webp = (b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
            + (599).to_bytes(3, "little") + (449).to_bytes(3, "little"))
    assert sniff_image(webp) == ("image/webp", 600, 450)
    assert sniff_image(b"<svg></svg>") is None


# -- catalogue -----------------------------------------------------------------


def test_catalog_replace_and_read_back(shop):
    got = shop.api("GET", "catalog").json()
    assert got["ok"] and got["version"] == 1 and isinstance(got["updated_at"], float)
    assert [s["code"] for s in got["styles"]] == ["T1001", "T1002", "T1003"]
    assert got["styles"][0]["desc"] == "软顶，可折叠" and "desc" not in got["styles"][1]
    again = shop.api("PUT", "catalog", {"styles": got["styles"]})
    assert again.json() == {"ok": True, "version": 2, "styles": 3, "skus": 7}
    status = shop.api("GET", "status").json()
    assert status["catalog"]["version"] == 2 and status["catalog"]["styles"] == 3


def _bad_catalogues(keys):
    good = fake_catalogue(keys)

    def variant(fn):
        body = json.loads(json.dumps(good))
        fn(body)
        return body

    one_style = good["styles"][0]
    many = [dict(one_style, code="S%04d" % i, colours=[{"name": "黑", "image": keys["a"],
                                                         "sizes": [{"label": "均码", "sku": "K%05d" % i}]}])
            for i in range(201)]
    return [
        ("not-object", []),
        ("no-styles", {"styles": "x"}),
        ("too-many-styles", {"styles": many}),
        ("bad-code", variant(lambda b: b["styles"][0].update(code="x"))),
        ("dup-code", variant(lambda b: b["styles"][1].update(code="T1001"))),
        ("empty-name", variant(lambda b: b["styles"][0].update(name="  "))),
        ("long-name", variant(lambda b: b["styles"][0].update(name="帽" * 61))),
        ("long-desc", variant(lambda b: b["styles"][0].update(desc="帽" * 121))),
        ("control-char", variant(lambda b: b["styles"][0].update(name="测试\x07帽"))),
        ("no-colours", variant(lambda b: b["styles"][0].update(colours=[]))),
        ("too-many-colours", variant(lambda b: b["styles"][0].update(colours=[
            {"name": "色%d" % i, "image": keys["a"], "sizes": [{"label": "均码", "sku": "C%04d" % i}]}
            for i in range(41)]))),
        ("too-many-sizes", variant(lambda b: b["styles"][0]["colours"][0].update(sizes=[
            {"label": "S%d" % i, "sku": "Z%04d" % i} for i in range(13)]))),
        ("bad-sku", variant(lambda b: b["styles"][0]["colours"][0]["sizes"][0].update(sku="a b"))),
        ("dup-sku", variant(lambda b: b["styles"][2]["colours"][0]["sizes"][0].update(sku="T1001A-F"))),
        ("bad-image", variant(lambda b: b["styles"][0]["colours"][0].update(image="cat.jpg"))),
        ("dup-colour", variant(lambda b: b["styles"][0]["colours"][1].update(name="黑色"))),
    ]


def test_catalog_validation(shop):
    for label, body in _bad_catalogues(shop.keys):
        resp = shop.api("PUT", "catalog", body)
        assert resp.code == 400, label
        assert resp.json()["error"] in ("invalid_catalog", "bad_json"), label
    assert shop.api("GET", "catalog").json()["version"] == 1


def test_catalog_rejects_pictures_not_uploaded(shop):
    body = fake_catalogue(dict(shop.keys, e="e" * 32))
    resp = shop.api("PUT", "catalog", body)
    assert resp.code == 400 and resp.json()["error"] == "unknown_image"
    assert shop.api("GET", "catalog").json()["version"] == 1


def test_catalog_body_cap(shop):
    resp = shop.api("PUT", "catalog", raw=b"{" + b" " * (2 * 1024 * 1024) + b"}")
    assert resp.code == 413


def test_catalog_replace_prunes_pictures_but_keeps_snapshots(shop):
    link = new_link(shop)
    assert submit(shop, path_of(link), ["T1003A-F"]).code == 303
    fresh = png_bytes((5, 200, 5))
    _put_image(shop, key_of(fresh), fresh)
    body = {"styles": [{"code": "T2001", "name": "新款", "colours": [
        {"name": "绿", "image": key_of(fresh), "sizes": [{"label": "均码", "sku": "T2001A-F"}]}]}]}
    assert shop.api("PUT", "catalog", body).code == 200
    keys = shop.api("GET", "images").json()["keys"]
    assert sorted(keys) == sorted([key_of(fresh), shop.keys["e"]])
    page = shop.get(path_of(link))
    assert "测试贝雷帽" in page.text  # the snapshot still shows the old hat
    assert shop.get("/img/" + shop.keys["e"]).code == 200


# -- links ---------------------------------------------------------------------


def test_create_personal_link_defaults(shop, clock):
    link = new_link(shop)
    assert set(link) == {"ok", "id", "token", "url", "expires_at", "max_items"}
    assert link["max_items"] == 3
    assert link["expires_at"] == clock.now + 14 * DAY
    assert link["url"] == "http://xuan.example.test/s/" + link["token"]
    assert re.match(r"^[A-Za-z0-9_-]{32}$", link["token"])
    stored = db_rows(shop.app, "SELECT token_hash FROM links")
    assert stored == [(hashlib.sha256(link["token"].encode()).hexdigest(),)]
    listing = shop.api("GET", "links?owner=main")
    assert link["token"] not in listing.text
    row = listing.json()["links"][0]
    assert {k: row[k] for k in ("id", "ref", "owner", "kind", "max_items", "revoked", "state", "uses")} == {
        "id": link["id"], "ref": "deal:1", "owner": "main", "kind": "personal", "max_items": 3,
        "revoked": False, "state": "unused", "uses": 0}


def test_link_url_uses_public_url(make_app):
    client = Client(make_app(public_url="https://xuan.example.com"))
    link = new_link(client, kind="general")
    assert link["url"] == "https://xuan.example.com/g/" + link["token"]


def test_link_url_behind_trusted_proxy(make_app):
    client = Client(make_app(trust_proxy=True))
    resp = client.api("POST", "links", {"ref": "deal:9"},
                      headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "xuan.example.org"})
    assert resp.json()["url"].startswith("https://xuan.example.org/s/")


@pytest.mark.parametrize("body", [
    {}, {"ref": ""}, {"ref": "has space"}, {"ref": "x" * 65}, {"ref": "d", "owner": "bad owner"},
    {"ref": "d", "kind": "group"}, {"ref": "d", "max_items": 0}, {"ref": "d", "max_items": 11},
    {"ref": "d", "max_items": "3"}, {"ref": "d", "max_items": True}, {"ref": "d", "expires_days": 0},
    {"ref": "d", "expires_days": 91}, {"ref": "d", "ask_account": "yes"},
    {"ref": "d", "kind": "general", "max_uses": 0}, {"ref": "d", "kind": "general", "max_uses": 1001},
], ids=lambda b: json.dumps(b)[:40])
def test_create_link_validation(client, body):
    resp = client.api("POST", "links", body)
    assert resp.code == 400 and resp.json()["error"] == "invalid"


def test_link_listing_states(shop, clock):
    a = new_link(shop, ref="deal:a")
    b = new_link(shop, ref="deal:b", expires_days=1)
    c = new_link(shop, ref="deal:c")
    g = new_link(shop, ref="deal:g", kind="general", max_uses=5)
    other = new_link(shop, ref="deal:o", owner="colleague")

    def states():
        return {r["ref"]: r["state"] for r in shop.api("GET", "links").json()["links"]}

    assert states() == {"deal:a": "unused", "deal:b": "unused", "deal:c": "unused", "deal:g": "unused"}
    submit(shop, path_of(a), ["T1001A-F"])
    assert shop.api("POST", "links/%d/revoke" % c["id"]).json() == {"ok": True}
    assert states()["deal:a"] == "submitted" and states()["deal:c"] == "revoked"
    sub_id = pull(shop)[0]["id"]
    assert states()["deal:a"] == "taken"
    shop.api("POST", "submissions/ack", {"owner": "main", "ids": [sub_id]})
    assert states()["deal:a"] == "acked"
    clock.now += 2 * DAY
    assert states()["deal:b"] == "expired"
    rows = {r["ref"]: r for r in shop.api("GET", "links").json()["links"]}
    assert rows["deal:g"]["kind"] == "general" and rows["deal:g"]["max_uses"] == 5
    assert rows["deal:g"]["ask_account"] is True and rows["deal:a"]["uses"] == 1
    assert [r["ref"] for r in shop.api("GET", "links?owner=colleague").json()["links"]] == ["deal:o"]
    assert other["id"] not in [r["id"] for r in shop.api("GET", "links").json()["links"]]
    assert shop.api("POST", "links/9999/revoke").code == 404
    assert shop.api("GET", "links?owner=bad%20owner").code == 400


# -- the creator flow ----------------------------------------------------------


def test_full_personal_flow(shop):
    link = new_link(shop, max_items=2)
    path = path_of(link)
    page = shop.get(path)
    assert page.code == 200 and page.header("Cache-Control") == "no-store"
    text = visible_text(page.text)
    assert "最多选 2 顶" in text and "老师您好" in text
    assert "测试渔夫帽" in text and "测试棒球帽" in text and "软顶，可折叠" in text
    assert 'action="%s"' % path in page.text
    assert 'loading="lazy"' in page.text and 'width="8" height="8"' in page.text
    assert "f-account" not in page.text  # not asked on this link
    assert len(re.findall(r"<option ", page.text)) == 35  # placeholder + 34 provinces
    confirm = review(shop, path, ["T1001B-F", "T1002A-L"])
    ctext = visible_text(confirm.text)
    assert "请核对一下" in ctext and "米白 · 均码" in ctext and "藏青 · L" in ctext
    assert "13800000000" in ctext and "浙江省杭州市西湖区测试路 1 号" in ctext
    assert "您填的收货信息只用于这次样品的寄出，我们会妥善保管。" in ctext
    assert shop.api("GET", "status").json()["submissions"]["new"] == 0  # nothing saved yet
    done = shop.post_form(path, hidden_fields(confirm.text) + [("action", "submit")])
    assert done.code == 303 and done.header("Location") == path
    summary = shop.get(path)
    stext = visible_text(summary.text)
    assert "已收到您的选款，谢谢老师～" in stext
    assert "138****0000" in stext and "13800000000" not in summary.text
    assert "修改" in stext and path + "?edit=1" in summary.text
    assert shop.api("GET", "status").json()["submissions"] == {"new": 1, "taken": 0, "acked": 0}


def test_submit_needs_a_nonce_from_the_site(shop):
    link = new_link(shop)
    path = path_of(link)
    form = [("action", "submit"), ("pick", "T1001A-F"), ("nonce", "abc.def")] + list(details().items())
    resp = shop.post_form(path, form)
    assert resp.code == 200 and "确认提交" in resp.text
    other = new_link(shop, ref="deal:2")
    confirm = review(shop, path_of(other), ["T1001A-F"])
    stolen = dict(hidden_fields(confirm.text))["nonce"]  # bound to the other link
    resp = shop.post_form(path, [("action", "submit"), ("nonce", stolen), ("pick", "T1001A-F")]
                          + list(details().items()))
    assert resp.code == 200 and "确认提交" in resp.text
    assert db_rows(shop.app, "SELECT COUNT(*) FROM submissions")[0][0] == 0


def test_validation_errors_keep_what_was_typed(shop):
    path = path_of(new_link(shop))
    resp = review(shop, path, ["T1001A-F"], phone="12345", province="浙江", city="杭",
                  address="ab", name="小美同学")
    assert resp.code == 200
    text = visible_text(resp.text)
    for key in ("phone", "province", "city", "address"):
        assert 'data-field="%s"' % key in resp.text
        assert re.search(r'class="field has-err" data-field="%s"' % key, resp.text), key
    assert MESSAGES["phone"] in text and MESSAGES["province"] in text and MESSAGES["address"] in text
    assert 'value="小美同学"' in resp.text and 'value="12345"' in resp.text
    assert checked_picks(resp.text) == ["T1001A-F"]
    assert 'data-start="details"' in resp.text
    assert db_rows(shop.app, "SELECT COUNT(*) FROM submissions")[0][0] == 0


@pytest.mark.parametrize("field,value,ok", [
    ("name", "", False), ("name", "名" * 31, False), ("name", "名" * 30, True),
    ("phone", "12345678901", False), ("phone", "+86 138-0000-0000", True), ("phone", "0086 13800000000", True),
    ("phone", "１３８００００００００", True), ("phone", "1380000000", False),
    ("province", "浙江", False), ("province", "内蒙古自治区", True),
    ("city", "杭", False), ("city", "市" * 21, False), ("district", "区", False),
    ("address", "abc", False), ("address", "路" * 121, False), ("address", "测试路\x00号", False),
    ("name", "小‮美", False), ("address", "测试路\n1 号", False),
], ids=lambda v: str(v)[:12])
def test_field_rules(shop, field, value, ok):
    resp = review(shop, path_of(new_link(shop)), ["T1001A-F"], **{field: value})
    assert ("确认提交" in resp.text) is ok
    if not ok:
        assert 'class="field has-err" data-field="%s"' % field in resp.text


def test_municipality_city_may_equal_province(shop):
    resp = review(shop, path_of(new_link(shop)), ["T1001A-F"], province="上海市", city="上海市",
                  district="浦东新区")
    assert "确认提交" in resp.text
    assert "上海市浦东新区测试路 1 号" in visible_text(resp.text)


def test_phone_normalising():
    assert normalise_phone(" +86 138-0000-0000 ") == "13800000000"
    assert normalise_phone("8613800000000") == "13800000000"
    assert normalise_phone("(138) 0000 0000") == "13800000000"
    assert len(PROVINCES) == 34 and len(set(PROVINCES)) == 34


@pytest.mark.parametrize("picks,message", [
    ([], MESSAGES["pick_none"]),
    (["T1001A-F", "T1001B-F", "T1003A-F", "T1002A-M"], MESSAGES["pick_many"] % 3),
    (["T9999X-F"], MESSAGES["pick_stale"]),
    (["T1001A-F", "T1001A-F"], MESSAGES["pick_again"]),
], ids=["none", "over-cap", "unknown", "duplicate"])
def test_pick_rules(shop, picks, message):
    resp = review(shop, path_of(new_link(shop)), picks)
    assert "确认提交" not in resp.text
    assert message in visible_text(resp.text)
    assert 'data-start="pick"' in resp.text


def test_account_asked_and_checked(shop):
    path = path_of(new_link(shop, ask_account=True))
    page = shop.get(path)
    assert 'id="f-account"' in page.text and "抖音号" in visible_text(page.text)
    assert MESSAGES["account"] in visible_text(review(shop, path, ["T1001A-F"]).text)
    assert MESSAGES["account"] in visible_text(review(shop, path, ["T1001A-F"], account="has space").text)
    assert submit(shop, path, ["T1001A-F"], account="@xiaomei.test").code == 303
    assert pull(shop)[0]["account"] == "xiaomei.test"


def test_double_submit_makes_one_submission(shop):
    path = path_of(new_link(shop))
    confirm = review(shop, path, ["T1001A-F"])
    form = hidden_fields(confirm.text) + [("action", "submit")]
    assert shop.post_form(path, form).code == 303
    assert shop.post_form(path, form).code == 303  # back button / double tap
    assert db_rows(shop.app, "SELECT COUNT(*), MAX(rev) FROM submissions") == [(1, 1)]


def test_edit_before_taken_replaces_the_submission(shop):
    path = path_of(new_link(shop))
    submit(shop, path, ["T1001A-F", "T1002B-M"])
    edit = shop.get(path + "?edit=1")
    assert sorted(checked_picks(edit.text)) == ["T1001A-F", "T1002B-M"]
    assert 'value="小美"' in edit.text and 'value="13800000000"' in edit.text
    assert '<option value="浙江省" selected>' in edit.text
    assert "款式有更新" not in edit.text
    assert submit(shop, path, ["T1003A-F"], name="小美美").code == 303
    subs = pull(shop)
    assert len(subs) == 1 and subs[0]["rev"] == 2
    assert [i["sku"] for i in subs[0]["items"]] == ["T1003A-F"]
    assert subs[0]["receiver"]["name"] == "小美美"
    # "返回修改" on the confirm page re-renders the form with what was typed
    other = path_of(new_link(shop, ref="deal:x"))
    back = shop.post_form(other, [("action", "edit"), ("pick", "T1001B-F")] + list(details().items()))
    assert checked_picks(back.text) == ["T1001B-F"] and 'value="测试路 1 号"' in back.text


def test_edit_after_catalogue_change_shows_neutral_line(shop):
    path = path_of(new_link(shop))
    submit(shop, path, ["T1001A-F", "T1003A-F"])
    body = fake_catalogue(shop.keys)
    body["styles"] = body["styles"][:2]  # T1003 is gone
    assert shop.api("PUT", "catalog", body).code == 200
    edit = shop.get(path + "?edit=1")
    assert checked_picks(edit.text) == ["T1001A-F"]
    assert "款式有更新，请重新确认您的选择" in visible_text(edit.text)
    summary = shop.get(path)
    assert "测试贝雷帽" in visible_text(summary.text)  # the snapshot is unchanged


def test_locked_after_pull(shop):
    path = path_of(new_link(shop))
    submit(shop, path, ["T1001A-F"])
    first = pull(shop)
    assert first[0]["rev"] == 1 and first[0]["kind"] == "personal" and first[0]["taken_at"]
    page = shop.get(path)
    text = visible_text(page.text)
    assert "您的选款我们已经在安排了，如需调整请直接微信联系运营" in text
    assert "?edit=1" not in page.text
    assert "您的选款我们已经在安排了" in shop.get(path + "?edit=1").text
    late = review(shop, path, ["T1003A-F"])
    assert late.code == 409 and "您的选款我们已经在安排了" in late.text
    form = [("action", "submit"), ("nonce", "x.y"), ("pick", "T1003A-F")] + list(details().items())
    assert shop.post_form(path, form).code == 409
    again = pull(shop)
    assert again == first  # idempotent, rev unchanged


def test_pull_returns_unacked_and_ack_masks(shop, clock):
    p1 = path_of(new_link(shop, ref="deal:1"))
    p2 = path_of(new_link(shop, ref="deal:2"))
    submit(shop, p1, ["T1001A-F"])
    first = pull(shop)
    clock.now += 10
    submit(shop, p2, ["T1002A-M"], name="小丽", phone="13900000000")
    second = pull(shop)
    assert [s["ref"] for s in second] == ["deal:1", "deal:2"]
    assert second[0] == first[0]
    ids = [s["id"] for s in second]
    resp = shop.api("POST", "submissions/ack", {"owner": "main", "ids": [ids[0], 4242]})
    assert resp.json() == {"ok": True, "acked": [ids[0]], "unknown": [4242]}
    assert shop.api("POST", "submissions/ack", {"owner": "main", "ids": [ids[0]]}).json()["acked"] == [ids[0]]
    assert shop.api("POST", "submissions/ack", {"owner": "other", "ids": [ids[1]]}).json()["unknown"] == [ids[1]]
    assert [s["id"] for s in pull(shop)] == [ids[1]]
    receiver = json.loads(db_rows(shop.app, "SELECT receiver FROM submissions WHERE id=?", (ids[0],))[0][0])
    assert receiver == {"name": "小＊＊", "phone": "138****0000", "province": "浙江省", "city": "杭州市",
                        "district": "", "address": ""}
    page = shop.get(p1)
    assert "小＊＊" in page.text and "测试路" not in page.text and "西湖区" not in page.text
    assert shop.api("GET", "status").json()["submissions"] == {"new": 0, "taken": 1, "acked": 1}
    bad = shop.api("POST", "submissions/ack", {"owner": "main", "ids": ["1"]})
    assert bad.code == 400


def test_ack_of_a_new_submission_is_unknown_and_drops_account(shop):
    path = path_of(new_link(shop, ask_account=True))
    submit(shop, path, ["T1001A-F"], account="xiaomei.test")
    sub_id = db_rows(shop.app, "SELECT id FROM submissions")[0][0]
    assert shop.api("POST", "submissions/ack", {"owner": "main", "ids": [sub_id]}).json()["unknown"] == [sub_id]
    pull(shop)
    shop.api("POST", "submissions/ack", {"owner": "main", "ids": [sub_id]})
    assert db_rows(shop.app, "SELECT account, state FROM submissions") == [(None, "acked")]


def test_pull_is_per_owner(shop):
    submit(shop, path_of(new_link(shop, owner="main")), ["T1001A-F"])
    submit(shop, path_of(new_link(shop, owner="colleague", ref="deal:c")), ["T1003A-F"])
    assert [s["ref"] for s in pull(shop, "colleague")] == ["deal:c"]
    assert [s["ref"] for s in pull(shop, "main")] == ["deal:1"]
    assert shop.api("POST", "submissions/pull", {"owner": "x y"}).code == 400


def test_general_link_flow(shop):
    general = new_link(shop, kind="general", ref="group:1", max_items=2)
    gpath = path_of(general)
    page = shop.get(gpath)
    assert page.code == 200 and 'id="f-account"' in page.text
    assert MESSAGES["account"] in visible_text(review(shop, gpath, ["T1001A-F"]).text)
    done = submit(shop, gpath, ["T1001A-F"], account="xiaomei.test")
    assert done.code == 303
    child_path = done.header("Location")
    assert child_path.startswith("/s/") and child_path != "/s/" + general["token"]
    cookie = done.header("Set-Cookie")
    assert cookie.startswith("sg%d=" % general["id"])
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/g/" in cookie
    assert "Secure" not in cookie
    mine = shop.get(child_path)
    text = visible_text(mine.text)
    assert "已收到您的选款，谢谢老师～" in text and "请收藏这个页面" in text and "xiaomei.test" in text
    again = shop.get(gpath)
    assert again.code == 303 and again.header("Location") == child_path
    stranger = Client(shop.app, ip="198.51.100.20")
    assert stranger.get(gpath).code == 200  # another browser gets the pick page
    subs = pull(shop)
    assert len(subs) == 1 and subs[0]["kind"] == "general" and subs[0]["ref"] == "group:1"
    assert subs[0]["account"] == "xiaomei.test"
    rows = {r["id"]: r for r in shop.api("GET", "links").json()["links"]}
    assert rows[general["id"]]["uses"] == 1 and rows[general["id"]]["state"] == "submitted"
    child = [r for r in rows.values() if r["parent_id"] == general["id"]]
    assert len(child) == 1 and child[0]["state"] == "taken" and child[0]["max_items"] == 2


def test_general_link_double_submit_one_personal_link(shop):
    gpath = path_of(new_link(shop, kind="general"))
    confirm = review(shop, gpath, ["T1001A-F"], account="xiaomei.test")
    form = hidden_fields(confirm.text) + [("action", "submit")]
    a = shop.post_form(gpath, form)
    shop.cookies.clear()
    b = shop.post_form(gpath, form)
    assert a.code == b.code == 303 and a.header("Location") == b.header("Location")
    assert db_rows(shop.app, "SELECT COUNT(*) FROM submissions")[0][0] == 1


def test_general_link_cap_and_secure_cookie(make_app):
    client = Client(make_app(public_url="https://xuan.example.com"))
    upload_fake_catalogue(client)
    gpath = path_of(new_link(client, kind="general", max_uses=1))
    done = submit(client, gpath, ["T1001A-F"], account="xiaomei.test")
    assert "Secure" in done.header("Set-Cookie")
    other = Client(client.app, ip="198.51.100.30")
    full = other.get(gpath)
    assert full.code == 404 and "这个链接已经失效了" in full.text
    assert client.get(gpath).code == 303  # the first creator still reaches her page


def test_invalid_links_look_the_same(shop, clock):
    expiring = new_link(shop, expires_days=1)
    revoked = new_link(shop, ref="deal:r")
    general = new_link(shop, kind="general", ref="deal:g")
    personal = new_link(shop, ref="deal:p")
    shop.api("POST", "links/%d/revoke" % revoked["id"])
    clock.now += 2 * DAY
    bodies = set()
    for url in ("/s/" + "A" * 32, "/s/short", "/s/" + "%3Cx%3E", "/s/a/b", path_of(expiring),
                path_of(revoked), "/s/" + general["token"], "/g/" + personal["token"]):
        resp = shop.get(url)
        assert resp.code == 404, url
        assert "这个链接已经失效了，" in resp.text and "请联系运营重新获取" in resp.text
        bodies.add(resp.body)
    assert len(bodies) == 1
    post = shop.post_form(path_of(expiring), [("action", "review")])
    assert post.code == 404 and post.body in bodies


def test_purge_after_retention(shop, clock):
    path = path_of(new_link(shop, ask_account=True, expires_days=1))
    submit(shop, path, ["T1001A-F"], account="xiaomei.test")
    unused = new_link(shop, ref="deal:unused", expires_days=1)
    clock.now += 32 * DAY
    shop.get("/healthz")  # the purge runs lazily on requests
    assert unused["id"] not in [r[0] for r in db_rows(shop.app, "SELECT id FROM links")]
    assert db_rows(shop.app, "SELECT purged FROM submissions") == [(0,)]
    clock.now += 60 * DAY
    shop.get("/healthz")
    row = db_rows(shop.app, "SELECT receiver, account, purged FROM submissions")[0]
    assert json.loads(row[0]) == mask_receiver(json.loads(json.dumps(details(phone="13800000000"))))
    assert row[1] is None and row[2] == 1
    subs = pull(shop)
    assert subs[0]["purged"] is True and subs[0]["receiver"]["address"] == ""
    assert [i["sku"] for i in subs[0]["items"]] == ["T1001A-F"]  # choices stay


def test_purge_runs_at_most_hourly(app, clock):
    clock.now += 10
    app._next_purge_check = 0
    assert app.maybe_purge() is True
    app._next_purge_check = 0
    assert app.maybe_purge() is False
    clock.now += 3601
    app._next_purge_check = 0
    assert app.maybe_purge() is True


# -- limits ----------------------------------------------------------------------


def test_post_rate_limit(make_app):
    client = Client(make_app(rate_post=(3, 600)))
    upload_fake_catalogue(client)
    path = path_of(new_link(client))
    for _ in range(3):
        assert review(client, path, ["T1001A-F"]).code == 200
    busy = review(client, path, ["T1001A-F"])
    assert busy.code == 429 and "操作有点频繁" in busy.text
    assert review(client, path, ["T1001A-F"], ip="198.51.100.40").code == 200


def test_page_rate_limit_and_assets_separate(make_app):
    client = Client(make_app(rate_page=(3, 60)))
    for _ in range(3):
        assert client.get("/").code == 200
    assert client.get("/").code == 429
    assert client.get(client.app.static_urls["css"]).code == 200
    assert client.get("/healthz").code == 200


def test_body_caps_and_form_shape(shop):
    path = path_of(new_link(shop))
    big = shop.request("POST", path, body=b"a=" + b"x" * (32 * 1024),
                       content_type="application/x-www-form-urlencoded")
    assert big.code == 413 and "提交的内容太长了" in big.text
    no_length = shop.request("POST", path, body=b"", content_type="application/x-www-form-urlencoded",
                             headers={"Content-Length": None})
    assert no_length.code == 411
    wrong_type = shop.request("POST", path, body=b"{}", content_type="application/json")
    assert wrong_type.code == 400
    bad_utf8 = shop.request("POST", path, body=b"name=%FF%FE", content_type="application/x-www-form-urlencoded")
    assert bad_utf8.code == 400
    assert shop.api("POST", "links", raw=b"not json").json()["error"] == "bad_json"
    assert shop.api("POST", "links", raw=b"[1]").json()["error"] == "bad_json"
    assert shop.api("POST", "links", raw=b"{" + b" " * (65 * 1024) + b"}").code == 413


# -- wording, privacy, escaping ----------------------------------------------------

FORBIDDEN = ["没货", "缺货", "补货", "查一下", "库存", "送", "免费", "包邮", "尽快", "马上", "立即", "立刻",
             "今天", "明天", "天内", "小时内", "聚水潭", "寄样单", "ERP", "SKU", "sku", "Sku", "款号", "佣金",
             "价格", "价钱", "¥", "￥", "元", "你"]


def _assert_clean(text, where):
    for word in FORBIDDEN:
        assert word not in text, "%r found in %s" % (word, where)


def test_forbidden_words_in_every_creator_text(shop, clock, make_app):
    for key, value in pages.TEXT.items():
        _assert_clean(value, "pages.TEXT[%r]" % key)
    for key, value in MESSAGES.items():
        _assert_clean(value, "forms.MESSAGES[%r]" % key)
    with open(os.path.join(SITE_DIR, "static", "site.js"), encoding="utf-8") as fh:
        js = fh.read()
    msg_block = re.search(r"var MSG = \{(.*?)\};", js, re.S).group(1)
    for literal in re.findall(r"'([^']*)'", msg_block):
        _assert_clean(literal, "site.js MSG")
    for literal in re.findall(r"'([^'\n]*[一-鿿][^'\n]*)'", js):
        _assert_clean(literal, "site.js string")

    rendered = {}
    personal = new_link(shop, ask_account=True)
    path = path_of(personal)
    rendered["index"] = shop.get("/")
    rendered["invalid"] = shop.get("/s/" + "B" * 32)
    rendered["pick"] = shop.get(path)
    rendered["errors"] = review(shop, path, ["T1001A-F", "T1002A-M", "T1003A-F", "T1001B-F"], name="",
                                phone="1", province="", city="", district="", address="", account="!")
    rendered["unknown"] = review(shop, path, ["T9999Z-F"])
    rendered["confirm"] = review(shop, path, ["T1001A-F"], account="xiaomei.test")
    shop.post_form(path, hidden_fields(rendered["confirm"].text) + [("action", "submit")])
    rendered["summary"] = shop.get(path)
    rendered["edit"] = shop.get(path + "?edit=1")
    body = fake_catalogue(shop.keys)
    body["styles"] = body["styles"][1:]
    shop.api("PUT", "catalog", body)
    rendered["stale"] = shop.get(path + "?edit=1")
    gen = path_of(new_link(shop, kind="general", ref="g:1"))
    rendered["general"] = shop.get(gen)
    child = submit(shop, gen, ["T1002A-M"], account="xiaomei.test").header("Location")
    rendered["child"] = shop.get(child)
    ids = [s["id"] for s in pull(shop)]
    rendered["locked"] = shop.get(path)
    shop.api("POST", "submissions/ack", {"owner": "main", "ids": ids})
    rendered["acked"] = shop.get(path)
    rendered["404"] = shop.get("/nothing-here")
    tight = Client(make_app(db_path=os.path.join(os.path.dirname(shop.app.config.db_path), "b.db"),
                            rate_page=(1, 60)))
    tight.get("/")
    rendered["busy"] = tight.get("/")
    rendered["too_big"] = shop.request("POST", path_of(new_link(shop, ref="big")), body=b"a=" + b"x" * 40000,
                                       content_type="application/x-www-form-urlencoded")
    empty = Client(make_app(db_path=os.path.join(os.path.dirname(shop.app.config.db_path), "c.db")))
    rendered["empty"] = empty.get(path_of(new_link(empty)))
    for name, resp in rendered.items():
        text = visible_text(resp.text)
        assert text.strip(), name
        _assert_clean(text, name)
    for name in ("pick", "confirm", "summary", "locked", "general", "child"):
        assert "老师" in visible_text(rendered[name].text) or "您" in visible_text(rendered[name].text), name
    for name in ("pick", "errors", "confirm", "general"):
        assert "您填的收货信息只用于这次样品的寄出，我们会妥善保管。" in visible_text(rendered[name].text), name


def test_hostile_input_is_escaped(shop):
    keys = shop.keys
    body = fake_catalogue(keys)
    body["styles"][0]["name"] = '<img src=x onerror=alert(1)>'
    body["styles"][0]["desc"] = '"><script>alert(2)</script>'
    assert shop.api("PUT", "catalog", body).code == 200
    path = path_of(new_link(shop))
    page = shop.get(path)
    assert "<img src=x" not in page.text and "<script>alert" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
    hostile = '<script>alert("x")</script>'
    confirm = review(shop, path, ["T1001A-F"], name=hostile[:30], address="测试路" + hostile)
    assert "<script>alert" not in confirm.text and "&lt;script&gt;" in confirm.text
    shop.post_form(path, hidden_fields(confirm.text) + [("action", "submit")])
    summary = shop.get(path)
    assert "<script>alert" not in summary.text and "&lt;script&gt;" in summary.text
    errors = review(shop, path, ["T1001A-F"], phone='"><b>1</b>')
    assert '"><b>1</b>' not in errors.text


# -- logs ------------------------------------------------------------------------


def test_logs_hold_no_personal_data(shop, caplog):
    caplog.set_level(logging.DEBUG)
    secrets = ["小美珍", "13812345678", "138 1234 5678", "测试路 99 号", "滨江区", "xiaomei.private", TOKEN,
               shop.ip]
    personal = new_link(shop, ask_account=True)
    general = new_link(shop, kind="general", ref="g:9")
    secrets += [personal["token"], general["token"]]
    path = path_of(personal)
    fields = dict(name="小美珍", phone="138 1234 5678", district="滨江区", address="测试路 99 号",
                  account="xiaomei.private")
    review(shop, path, ["T1001A-F"], **dict(fields, phone="bad"))
    submit(shop, path, ["T1001A-F"], **fields)
    shop.get(path + "?edit=1")
    child = submit(shop, path_of(general), ["T1002A-L"], **fields).header("Location")
    secrets.append(child[3:])
    shop.get(child)
    shop.get(path_of(general))
    shop.api("GET", "status", token="wrong-" + "q" * 40)
    ids = [s["id"] for s in pull(shop)]
    shop.api("POST", "submissions/ack", {"owner": "main", "ids": ids})
    text = caplog.text
    assert "submission saved" in text and "pull (owner main)" in text  # something was logged
    for secret in secrets:
        assert secret not in text, secret


def test_safe_path_and_scrub():
    assert safe_path("/s/abcDEF123_-xyz789012345678901?edit=1") == "/s/…"
    assert safe_path("/g/abcDEF123_-xyz7890123456789") == "/g/…"
    assert safe_path("/admin/api/links/12/revoke") == "/admin/api/links/12/revoke"
    assert safe_path("/img/" + "a" * 32) == "/img/" + "a" * 32
    assert safe_path("/S/sometoken") == "/<other>"
    assert safe_path("/static/site.css?v=1") == "/static/site.css"
    assert "tok" not in scrub('code 400, message Bad request syntax ("GET /s/tok123 HTTP/1.1")')


def test_server_access_log_has_no_tokens(shop, caplog):
    caplog.set_level(logging.INFO)
    link = new_link(shop)
    server = make_wsgi_server(shop.app, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        for path in (path_of(link), path_of(link) + "?edit=1", "/g/" + link["token"], "/healthz"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            assert resp.getheader("Server") == "sample-site"
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        raw.sendall(("GET /s/%s junk junk HTTP/1.1\r\nHost: x\r\n\r\n" % link["token"]).encode())
        assert raw.recv(4096).startswith(b"HTTP/1.")
        raw.close()
    finally:
        server.shutdown()
        server.server_close()
    access = [r.getMessage() for r in caplog.records if r.name == "sample_site.access"]
    assert any(m.startswith("GET /s/… 200") for m in access), access
    assert "client error 400" in access
    assert link["token"] not in caplog.text


# -- concurrency -------------------------------------------------------------------


def test_concurrent_submits_and_pulls_stay_consistent(make_app):
    app = make_app(rate_post=(10000, 60), rate_page=(10000, 60))
    admin = Client(app)
    upload_fake_catalogue(admin)
    path = path_of(new_link(admin))
    picks = [["T1001A-F"], ["T1001B-F"], ["T1003A-F"], ["T1002A-M"]]
    outcomes = []

    def creator():
        c = Client(app, ip="198.51.100.50")
        for i in range(24):
            resp = review(c, path, picks[i % 4])
            if resp.code == 409:
                outcomes.append("locked")
                continue
            done = c.post_form(path, hidden_fields(resp.text) + [("action", "submit")])
            outcomes.append(done.code)

    pulled = []

    def puller():
        time.sleep(0.02)
        for _ in range(6):
            pulled.append([(s["id"], s["rev"], tuple(i["sku"] for i in s["items"])) for s in pull(admin)])
            time.sleep(0.005)

    threads = [threading.Thread(target=creator), threading.Thread(target=puller)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    seen = [p for p in pulled if p]
    final = pull(admin)
    if seen:
        assert all(p == seen[0] for p in seen)  # once taken, nothing changes
        assert [(s["id"], s["rev"], tuple(i["sku"] for i in s["items"])) for s in final] == seen[0]
    assert db_rows(app, "SELECT COUNT(*) FROM submissions")[0][0] == 1
    rev = db_rows(app, "SELECT rev FROM submissions")[0][0]
    assert rev == outcomes.count(303)


# -- running it as a script ---------------------------------------------------------


def _interpreters():
    found = [sys.executable]
    if os.path.exists("/usr/bin/python3"):
        try:
            out = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2] >= (3, 9))"],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            out = ""
        if out == "True":
            found.append("/usr/bin/python3")
    return found


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and file modes")
@pytest.mark.parametrize("python", _interpreters(),
                         ids=lambda p: "this-python" if p == sys.executable else "usr-bin-python3")
def test_app_py_runs_from_any_folder_name(tmp_path, python):
    folder = tmp_path / "xuan-site 2"
    shutil.copytree(SITE_DIR, str(folder), ignore=shutil.ignore_patterns("__pycache__", "tools", "data"))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}
    refused = subprocess.run([python, "app.py"], cwd=str(folder), env=env, capture_output=True, text=True,
                             timeout=30)
    assert refused.returncode == 2 and "SAMPLE_SITE_ADMIN_TOKEN" in refused.stderr
    env.update({"SAMPLE_SITE_ADMIN_TOKEN": TOKEN, "SAMPLE_SITE_PORT": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    wsgi = subprocess.run([python, "-c", "import wsgi, sys; sys.exit(0 if callable(wsgi.application) else 1)"],
                          cwd=str(folder), env=env, capture_output=True, text=True, timeout=30)
    assert wsgi.returncode == 0, wsgi.stderr
    proc = subprocess.Popen([python, "app.py"], cwd=str(folder), env=env, stderr=subprocess.PIPE, text=True)
    try:
        port = None
        deadline = time.time() + 20
        lines = []
        while time.time() < deadline:
            line = proc.stderr.readline()
            if not line:
                break
            lines.append(line)
            found = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
            if found:
                port = int(found.group(1))
                break
        assert port, "".join(lines)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/healthz")
        assert conn.getresponse().read() == b"ok"
        conn.close()
        assert os.stat(str(folder / "data" / "sample_site.db")).st_mode & 0o777 == 0o600
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        finally:
            if proc.poll() is None:
                proc.kill()
    assert proc.returncode == 0
