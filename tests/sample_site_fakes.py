"""Helpers for the sample site tests: an in-process WSGI client, tiny
pictures and a fake catalogue. All data here is obviously fake."""

import hashlib
import io
import json
import re
import struct
import sys
import zlib
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

TOKEN = "test-admin-token-" + "x" * 32


def png_bytes(rgb=(200, 80, 60), size=8):
    """A small solid-colour PNG (valid, decodable by browsers)."""
    width = height = size
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def key_of(data):
    return hashlib.sha256(data).hexdigest()[:32]


class Resp(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def code(self):
        return int(self.status.split()[0])

    @property
    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def header(self, name):
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def headers_all(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]


class Client(object):
    """Calls a WSGI app in-process; keeps cookies like a browser would."""

    def __init__(self, app, ip="203.0.113.7", host="xuan.example.test"):
        self.app = app
        self.ip = ip
        self.host = host
        self.cookies = {}

    def request(self, method, url, body=b"", headers=None, content_type=None, ip=None):
        parts = urlsplit(url)
        environ = {}
        setup_testing_defaults(environ)
        environ["REQUEST_METHOD"] = method
        environ["PATH_INFO"] = parts.path.encode("utf-8").decode("latin-1")
        environ["QUERY_STRING"] = parts.query
        environ["REMOTE_ADDR"] = ip or self.ip
        environ["HTTP_HOST"] = self.host
        environ["SERVER_NAME"] = self.host
        environ["wsgi.input"] = io.BytesIO(body)
        environ["wsgi.errors"] = sys.stderr
        if body or method in ("POST", "PUT"):
            environ["CONTENT_LENGTH"] = str(len(body))
        if content_type:
            environ["CONTENT_TYPE"] = content_type
        jar = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        if jar:
            environ["HTTP_COOKIE"] = jar
        for k, v in (headers or {}).items():
            key = k.upper().replace("-", "_")
            if key in ("CONTENT_LENGTH", "CONTENT_TYPE"):
                if v is None:
                    environ.pop(key, None)
                else:
                    environ[key] = v
            else:
                environ["HTTP_" + key] = v
        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = response_headers

        chunks = self.app(environ, start_response)
        data = b"".join(chunks)
        resp = Resp(captured["status"], captured["headers"], data)
        for cookie in resp.headers_all("Set-Cookie"):
            name, _, rest = cookie.partition("=")
            self.cookies[name] = rest.split(";", 1)[0]
        return resp

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post_form(self, url, fields, **kw):
        body = urlencode(fields, doseq=True).encode("utf-8")
        return self.request("POST", url, body=body,
                            content_type="application/x-www-form-urlencoded", **kw)

    def api(self, method, path, payload=None, token=TOKEN, raw=None, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        body = raw if raw is not None else (json.dumps(payload).encode("utf-8")
                                            if payload is not None else b"")
        return self.request(method, "/admin/api/" + path, body=body, headers=headers,
                            content_type="application/json", **kw)


def fake_catalogue(images):
    """Three styles; ``images`` maps a label to an uploaded image key."""
    return {"styles": [
        {"code": "T1001", "name": "测试渔夫帽", "desc": "软顶，可折叠",
         "colours": [
             {"name": "黑色", "image": images["a"], "sizes": [{"label": "均码", "sku": "T1001A-F"}]},
             {"name": "米白", "image": images["b"], "sizes": [{"label": "均码", "sku": "T1001B-F"}]},
         ]},
        {"code": "T1002", "name": "测试棒球帽",
         "colours": [
             {"name": "藏青", "image": images["c"], "sizes": [
                 {"label": "M", "sku": "T1002A-M"}, {"label": "L", "sku": "T1002A-L"}]},
             {"name": "卡其", "image": images["d"], "sizes": [
                 {"label": "M", "sku": "T1002B-M"}, {"label": "L", "sku": "T1002B-L"}]},
         ]},
        {"code": "T1003", "name": "测试贝雷帽", "desc": "羊毛混纺",
         "colours": [
             {"name": "酒红", "image": images["e"], "sizes": [{"label": "均码", "sku": "T1003A-F"}]},
         ]},
    ]}


def upload_fake_catalogue(client):
    colours = {"a": (20, 20, 20), "b": (240, 235, 220), "c": (30, 40, 90), "d": (190, 160, 110),
               "e": (130, 20, 40)}
    keys = {}
    for label, rgb in colours.items():
        data = png_bytes(rgb)
        keys[label] = key_of(data)
        resp = client.request("PUT", "/admin/api/images/" + keys[label], body=data,
                              headers={"Authorization": "Bearer " + TOKEN},
                              content_type="image/png")
        assert resp.code == 200, resp.text
    resp = client.api("PUT", "catalog", fake_catalogue(keys))
    assert resp.code == 200, resp.text
    return keys


RECEIVER = {"name": "小美", "phone": "138 0000 0000", "province": "浙江省", "city": "杭州市",
            "district": "西湖区", "address": "测试路 1 号"}


def details(**over):
    fields = dict(RECEIVER)
    fields.update(over)
    return fields


class _Inputs(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.inputs = []
        self.text = []
        self.attrs_text = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in ("input", "button", "select", "textarea", "option"):
            self.inputs.append((tag, d))
        for a in ("placeholder", "title", "alt", "aria-label"):
            if d.get(a):
                self.attrs_text.append(d[a])
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.text.append(data.strip())


def parse_html(text):
    p = _Inputs()
    p.feed(text)
    return p


def hidden_fields(html_text):
    """The hidden inputs of a confirm page as a list of (name, value)."""
    out = []
    for tag, attrs in parse_html(html_text).inputs:
        if tag == "input" and attrs.get("type") == "hidden":
            out.append((attrs.get("name"), attrs.get("value", "")))
    return out


def visible_text(html_text):
    """What a creator can read on a page (text nodes and visible attributes)."""
    p = parse_html(html_text)
    return "\n".join(p.text + p.attrs_text)


def checked_picks(html_text):
    return [a.get("value") for t, a in parse_html(html_text).inputs
            if t == "input" and a.get("name") == "pick" and "checked" in a]


def token_from_url(url):
    return re.search(r"/[sg]/([A-Za-z0-9_-]+)$", url).group(1)
