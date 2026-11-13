"""The sample picking site: WSGI app, creator pages, admin API and a small server.

Run it from inside its folder (whatever the folder is called)::

    SAMPLE_SITE_ADMIN_TOKEN_FILE=/path/to/token python3 app.py

or build it in-process::

    from sample_site.app import create_app
    from sample_site.config import Config
    app = create_app(Config(admin_token=secrets.token_urlsafe(32), db_path="/tmp/site.db"))
"""

if __name__ == "__main__" and not __package__:
    # Run as a script: load this folder as the package "sample_site", whatever
    # the folder is named, so the relative imports below work.
    import importlib.util as _ilu
    import os as _os
    import sys as _sys

    _here = _os.path.dirname(_os.path.abspath(__file__))
    _sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or ".") != _here]
    _spec = _ilu.spec_from_file_location("sample_site", _os.path.join(_here, "__init__.py"),
                                         submodule_search_locations=[_here])
    _pkg = _ilu.module_from_spec(_spec)
    _sys.modules["sample_site"] = _pkg
    _spec.loader.exec_module(_pkg)
    from sample_site.app import main as _main

    _sys.exit(_main())

import hashlib
import hmac
import http
import json
import logging
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer, make_server

from . import pages
from .catalog import IMAGE_KEY_RE, Catalog, CatalogError, image_key, sniff_image, validate_catalog
from .config import Config, ConfigError
from .forms import check_form
from .security import (RateLimiter, Signer, client_key, is_internal_peer, is_loopback_host, new_token,
                       safe_path, scrub, token_hash, token_ok)
from .store import Store

log = logging.getLogger("sample_site")
access_log = logging.getLogger("sample_site.access")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}

CSP = ("default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

REF_RE = re.compile(r"^[0-9A-Za-z:_-]{1,64}$")
OWNER_RE = re.compile(r"^[0-9A-Za-z_-]{1,32}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")


class HttpError(Exception):
    def __init__(self, status, code, message):
        Exception.__init__(self, message)
        self.status = status
        self.code = code
        self.message = message


class Response(object):
    def __init__(self, status=200, body=b"", content_type="text/html; charset=utf-8",
                 headers=None, cache=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.content_type = content_type
        self.headers = list(headers or [])
        self.cache = cache or "no-store"


BODY_CHUNK = 16384


class Request(object):
    def __init__(self, environ, body_deadline=10.0):
        self.environ = environ
        self.body_deadline = body_deadline
        self.method = (environ.get("REQUEST_METHOD") or "GET").upper()
        raw = environ.get("PATH_INFO") or "/"
        try:
            self.path = raw.encode("latin-1").decode("utf-8")
        except (UnicodeError, AttributeError):
            self.path = raw
        try:
            self.query = parse_qs(environ.get("QUERY_STRING") or "", max_num_fields=20)
        except ValueError:
            self.query = {}

    def header(self, name):
        return self.environ.get("HTTP_" + name.upper().replace("-", "_")) or ""

    def arg(self, name):
        values = self.query.get(name) or [""]
        return values[0]

    def cookie(self, name):
        for part in self.header("Cookie").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == name:
                    return v.strip()
        return ""

    def read_body(self, cap):
        length = (self.environ.get("CONTENT_LENGTH") or "").strip()
        if not length:
            raise HttpError(411, "length_required", "Content-Length is required")
        try:
            n = int(length)
        except ValueError:
            raise HttpError(400, "bad_request", "bad Content-Length")
        if n < 0:
            raise HttpError(400, "bad_request", "bad Content-Length")
        if n > cap:
            raise HttpError(413, "too_large", "body larger than %d bytes" % cap)
        if not n:
            return b""
        # Read in chunks under one total deadline, so a client sending a byte
        # now and then cannot hold a server thread (the per-read socket timeout
        # alone would let it). Our own server hands over the socket, so a
        # single read cannot block past the deadline either.
        stream = self.environ["wsgi.input"]
        reader = getattr(stream, "read1", None) or stream.read
        sock = self.environ.get("sample_site.socket")
        deadline = time.monotonic() + self.body_deadline
        parts = []
        got = 0
        try:
            while got < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise HttpError(408, "timeout", "the request body did not arrive in time")
                if sock is not None:
                    sock.settimeout(max(0.05, left))
                chunk = reader(min(BODY_CHUNK, n - got))
                if not chunk:
                    break
                parts.append(chunk)
                got += len(chunk)
        except (socket.timeout, OSError):
            raise HttpError(408, "timeout", "the request body did not arrive in time")
        finally:
            if sock is not None:
                try:
                    sock.settimeout(self.environ.get("sample_site.socket_timeout"))
                except OSError:
                    pass
        if got != n:
            raise HttpError(400, "bad_request", "body shorter than Content-Length")
        return b"".join(parts)


def _load_static():
    files = {}
    for name in ("boot.js", "site.js", "site.css"):
        with open(os.path.join(STATIC_DIR, name), "rb") as fh:
            data = fh.read()
        files[name] = (data, STATIC_TYPES[os.path.splitext(name)[1]],
                       hashlib.sha256(data).hexdigest()[:10])
    return files


def _log_exception(where):
    """Log an unexpected error without its message (it could hold typed text)."""
    etype, _value, tb = sys.exc_info()
    frames = traceback.extract_tb(tb)[-4:]
    trail = " <- ".join("%s:%d" % (os.path.basename(f.filename), f.lineno) for f in reversed(frames))
    log.error("unexpected %s in %s at %s", etype.__name__ if etype else "error", where, trail)


class App(object):
    """The WSGI application. Build it with :func:`create_app`."""

    def __init__(self, config):
        config.validate()
        self.config = config
        self.clock = config.clock
        self.store = Store(config.db_path, config.clock)
        self.signer = Signer(self.store.meta("secret"))
        self.limiter = RateLimiter()
        self._admin_token = config.admin_token.encode("utf-8")
        self._catalog = None
        self._catalog_lock = threading.Lock()
        self._next_purge_check = 0.0
        self._purge_lock = threading.Lock()
        self.static = _load_static()
        self.static_urls = {
            "css": "/static/site.css?v=" + self.static["site.css"][2],
            "js": "/static/site.js?v=" + self.static["site.js"][2],
            "boot": "/static/boot.js?v=" + self.static["boot.js"][2],
        }

    # -- WSGI ----------------------------------------------------------------

    def __call__(self, environ, start_response):
        req = Request(environ, self.config.body_deadline)
        try:
            self.maybe_purge()
        except Exception:
            _log_exception("purge")
        try:
            resp = self.dispatch(req)
        except HttpError as err:
            resp = self._error_response(req, err)
        except Exception:
            _log_exception("%s %s" % (req.method, safe_path(req.path)))
            resp = self._error_response(req, HttpError(500, "server_error", "internal error"))
        return self._send(req, resp, start_response)

    def _send(self, req, resp, start_response):
        headers = [
            ("Content-Type", resp.content_type),
            ("Content-Length", str(len(resp.body))),
            ("Cache-Control", resp.cache),
            ("Content-Security-Policy", CSP),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Robots-Tag", "noindex, nofollow"),
            ("X-Frame-Options", "DENY"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
        ]
        if resp.cache == "no-store":
            headers.append(("Pragma", "no-cache"))
        if self.config.https:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        headers.extend(resp.headers)
        status = "%d %s" % (resp.status, http.HTTPStatus(resp.status).phrase)
        start_response(status, headers)
        return [b"" if req.method == "HEAD" else resp.body]

    def _error_response(self, req, err):
        if req.path.startswith("/admin/"):
            return self._json(err.status, {"ok": False, "error": err.code, "message": err.message})
        text = {
            413: pages.TEXT["too_big"], 429: pages.TEXT["busy"], 404: pages.TEXT["not_found"],
            500: pages.TEXT["server_error"], 408: pages.TEXT["slow"],
        }.get(err.status, pages.TEXT["bad_request"])
        return Response(err.status, pages.message_page(self.config.brand, self.static_urls, text))

    def dispatch(self, req):
        path = req.path
        method = req.method
        if path.startswith("/admin/api/"):
            return self.admin(req, path[len("/admin/api/"):])
        if path.startswith("/s/") or path.startswith("/g/"):
            token = path[3:]
            if "/" in token:
                return self._invalid()
            return self.creator(req, token, general=path.startswith("/g/"))
        if method not in ("GET", "HEAD"):
            raise HttpError(405, "method_not_allowed", "method not allowed")
        if path.startswith("/static/") or path.startswith("/img/") or path == "/favicon.ico":
            self._limit(req, "asset", self.config.rate_asset)
            if path == "/favicon.ico":
                return Response(204, b"", "text/plain", cache="public, max-age=86400")
            if path.startswith("/img/"):
                return self.image(req, path[5:])
            return self.static_file(req, path[8:])
        if path == "/healthz":
            return Response(200, b"ok", "text/plain; charset=utf-8")
        if path == "/robots.txt":
            return Response(200, b"User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8",
                            cache="public, max-age=86400")
        self._limit(req, "page", self.config.rate_page)
        if path == "/":
            return Response(200, pages.page_index(self.config.brand, self.static_urls))
        raise HttpError(404, "not_found", "not found")

    # -- helpers -------------------------------------------------------------

    def _proxied(self, req):
        """Forwarded headers count only with TRUST_PROXY and when the peer is
        loopback / private (the reverse proxy), never from the internet."""
        return self.config.trust_proxy and is_internal_peer(req.environ.get("REMOTE_ADDR"))

    def client_ip(self, req):
        """The rate-limit key of the client (IPv6 by its /64)."""
        if self._proxied(req):
            last = req.header("X-Forwarded-For").split(",")[-1].strip()
            if last:
                return client_key(last)
        return client_key(req.environ.get("REMOTE_ADDR") or "-")

    def _limit(self, req, bucket, rule):
        ip = self.client_ip(req)
        if not self.limiter.hit(bucket, ip, rule[0], rule[1]):
            log.warning("rate limited: %s (client %s)", bucket, self.signer.ip_tag(ip))
            raise HttpError(429, "rate_limited", "too many requests, try again later")

    def _is_https(self, req):
        if self.config.public_url:
            return self.config.https
        if self._proxied(req) and req.header("X-Forwarded-Proto").split(",")[-1].strip() == "https":
            return True
        return req.environ.get("wsgi.url_scheme") == "https"

    def base_url(self, req):
        if self.config.public_url:
            return self.config.public_url
        scheme = "https" if self._is_https(req) else "http"
        host = ""
        if self._proxied(req):
            host = req.header("X-Forwarded-Host").split(",")[-1].strip()
        host = host or req.header("Host")
        if not host or not HOST_RE.match(host):
            host = "%s:%s" % (req.environ.get("SERVER_NAME", "localhost"),
                              req.environ.get("SERVER_PORT", "80"))
        return "%s://%s" % (scheme, host)

    def _json(self, status, obj):
        return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def _html(self, body, status=200, headers=None):
        return Response(status, body, headers=headers)

    def _invalid(self):
        return Response(404, pages.page_invalid(self.config.brand, self.static_urls))

    def _redirect(self, location, headers=None):
        return Response(303, b"", "text/plain; charset=utf-8",
                        headers=[("Location", location)] + list(headers or []))

    def catalog(self):
        """The current catalogue (cached per version; safe across processes)."""
        version = self.store.catalog_version()
        cached = self._catalog
        if cached is not None and cached[0].version == version:
            return cached[0], cached[1]
        with self._catalog_lock:
            version, updated, styles = self.store.catalog_state()
            cat = Catalog(version, updated, styles)
            keys = [c["image"] for s in styles for c in s["colours"]]
            sizes = self.store.image_sizes(keys)
            self._catalog = (cat, sizes)
            return cat, sizes

    # -- purge ---------------------------------------------------------------

    def maybe_purge(self):
        """Run the purge job when it is due (at most once per interval)."""
        now = self.clock()
        if now < self._next_purge_check:
            return False
        with self._purge_lock:
            if now < self._next_purge_check:
                return False
            self._next_purge_check = now + min(60, self.config.purge_interval)
            if not self.store.try_claim_purge(self.config.purge_interval):
                return False
            return self.run_purge()

    def run_purge(self):
        stripped, deleted = self.store.purge(self.config.retention_days)
        if stripped or deleted:
            log.info("purge: %d submission(s) reduced to remnants, %d dead link(s) deleted",
                     stripped, deleted)
        return True

    # -- static and pictures -------------------------------------------------

    def static_file(self, req, name):
        entry = self.static.get(name)
        if entry is None:
            raise HttpError(404, "not_found", "not found")
        data, ctype, digest = entry
        cache = ("public, max-age=31536000, immutable" if req.arg("v") == digest
                 else "public, max-age=300")
        return Response(200, data, ctype, cache=cache)

    def image(self, req, key):
        if not IMAGE_KEY_RE.match(key):
            raise HttpError(404, "not_found", "not found")
        etag = '"%s"' % key
        cache = "public, max-age=31536000, immutable"
        if req.header("If-None-Match") == etag:
            return Response(304, b"", "text/plain", headers=[("ETag", etag)], cache=cache)
        found = self.store.get_image(key)
        if found is None:
            raise HttpError(404, "not_found", "not found")
        return Response(200, found[1], found[0], headers=[("ETag", etag)], cache=cache)

    # -- creator pages -------------------------------------------------------

    def _live_link(self, token, kind):
        """The link for a token when it may be used now, else None (one answer
        for unknown, expired, revoked and wrong-kind tokens)."""
        if not token_ok(token):
            return None
        link = self.store.link_by_hash(token_hash(token))
        if link is None or link["revoked_at"] is not None or link["kind"] != kind:
            return None
        if link.get("parent_revoked_at") is not None:  # its general link was revoked
            return None
        if self.clock() >= link["expires_at"]:
            return None
        return link

    def creator(self, req, token, general):
        if req.method in ("GET", "HEAD"):
            self._limit(req, "page", self.config.rate_page)
        elif req.method == "POST":
            self._limit(req, "post", self.config.rate_post)
        else:
            raise HttpError(405, "method_not_allowed", "method not allowed")
        link = self._live_link(token, "general" if general else "personal")
        if link is None:
            return self._invalid()
        prefix = "/g/" if general else "/s/"
        action = prefix + token
        if general:
            if req.method in ("GET", "HEAD") and req.arg("new") != "1":
                child = self._child_from_cookie(req, link)
                if child is not None:  # she (or someone on her phone) submitted before
                    return self._html(pages.page_general_choice(
                        self.config.brand, self.static_urls, "/s/" + child, action + "?new=1"))
            if link["max_uses"] is not None and link["uses"] >= link["max_uses"]:
                return self._invalid()
            sub = None
        else:
            sub = self.store.submission_for_link(link["id"])
        ask = bool(link["ask_account"]) or general
        if req.method in ("GET", "HEAD"):
            if sub is None:
                return self._pick_page(action, link, ask)
            editable = sub["state"] == "new" and not sub["purged"]
            if editable and req.arg("edit") == "1":
                return self._pick_from_submission(action, link, ask, sub)
            return self._summary(sub, editable, action, link)
        # POST
        if sub is not None and (sub["state"] != "new" or sub["purged"]):
            return self._summary(sub, False, action, link, status=409)
        form = self._read_form(req)
        return self._post(req, link, token, action, form, ask, general)

    def _child_from_cookie(self, req, general_link):
        value = req.cookie("sg%d" % general_link["id"])
        if not token_ok(value):
            return None
        child = self.store.link_by_hash(token_hash(value))
        if (child is None or child["parent_id"] != general_link["id"] or child["revoked_at"] is not None
                or self.clock() >= child["expires_at"]):
            return None
        return value

    def _read_form(self, req):
        ctype = req.environ.get("CONTENT_TYPE") or ""
        if not ctype.lower().startswith("application/x-www-form-urlencoded"):
            raise HttpError(400, "bad_request", "form expected")
        data = req.read_body(self.config.max_form_bytes)
        try:
            text = data.decode("utf-8")
            return parse_qs(text, keep_blank_values=True, max_num_fields=600,
                            encoding="utf-8", errors="strict")
        except (UnicodeError, ValueError):
            raise HttpError(400, "bad_request", "form not readable")

    def _pick_page(self, action, link, ask, typed=None, errors=None, stale=False, start="pick",
                   status=200, refill=False):
        cat, sizes = self.catalog()
        body = pages.page_pick(self.config.brand, self.static_urls, action, cat, sizes,
                               link["max_items"], ask, typed=typed, errors=errors, stale=stale,
                               start=start, refill=refill)
        return self._html(body, status)

    def _pick_from_submission(self, action, link, ask, sub):
        """The edit form: her picks and the coarse address are filled in; the
        phone, street address and 抖音号 are typed again (the link may have
        been forwarded)."""
        cat, _sizes = self.catalog()
        picks = [it["sku"] for it in sub["items"] if it["sku"] in cat.by_sku]
        stale = len(picks) != len(sub["items"])
        receiver = sub["receiver"]
        typed = {k: receiver.get(k, "") for k in ("name", "province", "city", "district")}
        typed["pick"] = picks
        return self._pick_page(action, link, ask, typed=typed, stale=stale, refill=True)

    def _summary(self, sub, editable, action, link, status=200):
        keys = [it["image"] for it in sub["items"]]
        sizes = self.store.image_sizes(keys)
        body = pages.page_summary(self.config.brand, self.static_urls, sub, sizes, editable,
                                  action + "?edit=1", keep_link=link["parent_id"] is not None)
        return self._html(body, status)

    def _post(self, req, link, token, action_url, form, ask, general):
        action = (form.get("action") or [""])[0]
        cat, sizes = self.catalog()
        if action == "edit":
            typed = {k: (form.get(k) or [""])[0][:200] for k in
                     ("name", "phone", "province", "city", "district", "address", "account")}
            picks = [p for p in form.get("pick", []) if p in cat.by_sku]
            stale = len(picks) != len(form.get("pick", []))
            typed["pick"] = picks
            return self._pick_page(action_url, link, ask, typed=typed, stale=stale)
        if general and self._general_capped(req, link):
            return self._html(pages.message_page(self.config.brand, self.static_urls,
                                                 pages.TEXT["per_client"]), 429)
        clean, errors, typed = check_form(form, cat, link["max_items"], ask)
        if errors:
            typed["pick"] = [p for p in typed.get("pick", []) if p in cat.by_sku]
            start = "pick" if "pick" in errors else "details"
            return self._pick_page(action_url, link, ask, typed=typed, errors=errors, start=start)
        nonce = (form.get("nonce") or [""])[0]
        if action != "submit" or not self.signer.nonce_ok(link["id"], nonce):
            body = pages.page_confirm(self.config.brand, self.static_urls, action_url,
                                      clean["items"], clean["receiver"], clean["account"], typed,
                                      self.signer.nonce(link["id"]), self.store.image_sizes(
                                          [it["image"] for it in clean["items"]]))
            return self._html(body)
        if general:
            return self._submit_general(req, link, clean, nonce)
        result = self.store.save_submission(link, clean, nonce)
        if result == "locked":
            sub = self.store.submission_for_link(link["id"])
            return self._summary(sub, False, action_url, link, status=409)
        if result == "saved":
            log.info("submission saved for link %d", link["id"])
        return self._redirect("/s/" + token)

    def _general_capped(self, req, link):
        rule = self.config.general_per_client
        return self.limiter.over("general%d" % link["id"], self.client_ip(req), rule[0], rule[1])

    def _cookie_secure(self, req):
        """Secure on every cookie except for a plain-http loopback host (local tests)."""
        if self._is_https(req):
            return True
        host = req.header("Host") or req.environ.get("SERVER_NAME", "")
        return not is_loopback_host(host)

    def _submit_general(self, req, link, clean, nonce):
        now = self.clock()
        life = max(7 * 86400.0, min(90 * 86400.0, link["expires_at"] - link["created_at"]))
        child = new_token()  # random; only its hash is stored
        result = self.store.submit_general(link, token_hash(child), clean, nonce, now + life)
        if result == "full":
            return self._invalid()
        if result == "duplicate":
            # A double tap or a replayed form: the first submit won. No link here.
            return self._html(pages.page_duplicate(self.config.brand, self.static_urls))
        rule = self.config.general_per_client
        self.limiter.hit("general%d" % link["id"], self.client_ip(req), rule[0], rule[1])
        log.info("general link %d: new personal link made with a submission", link["id"])
        cookie = "sg%d=%s; Path=/g/; Max-Age=%d; HttpOnly; SameSite=Lax" % (link["id"], child, int(life))
        if self._cookie_secure(req):
            cookie += "; Secure"
        return self._redirect("/s/" + child, headers=[("Set-Cookie", cookie)])

    # -- admin API -----------------------------------------------------------

    def _auth_ok(self, req):
        header = req.header("Authorization")
        if header[:7].lower() != "bearer ":
            presented = b""
        else:
            presented = header[7:].strip().encode("utf-8", "replace")
        # Constant-time comparison; also run it for a missing header.
        return hmac.compare_digest(presented, self._admin_token) and bool(presented)

    def admin(self, req, sub):
        # The token is checked first: the right token is never locked out by
        # someone else's failures; only failures are counted and limited.
        if not self._auth_ok(req):
            ip = self.client_ip(req)
            rule = self.config.rate_admin_fail
            if not self.limiter.hit("admin_fail", ip, rule[0], rule[1]):
                raise HttpError(429, "rate_limited", "too many failed attempts, try again later")
            log.warning("admin: authentication failed (client %s)", self.signer.ip_tag(ip))
            raise HttpError(401, "unauthorized", "missing or wrong admin token")
        m = req.method
        if sub == "status":
            return self._only(m, "GET") or self.api_status()
        if sub == "images":
            return self._only(m, "GET") or self._json(200, {"ok": True, "keys": self.store.image_keys()})
        if sub.startswith("images/"):
            return self._only(m, "PUT") or self.api_put_image(req, sub[len("images/"):])
        if sub == "catalog":
            if m == "PUT":
                return self.api_put_catalog(req)
            return self._only(m, "GET", "PUT") or self.api_get_catalog()
        if sub == "links":
            if m == "POST":
                return self.api_create_link(req)
            return self._only(m, "GET", "POST") or self.api_list_links(req)
        found = re.match(r"^links/(\d{1,12})/revoke$", sub)
        if found:
            return self._only(m, "POST") or self.api_revoke(int(found.group(1)))
        if sub == "submissions/pull":
            return self._only(m, "POST") or self.api_pull(req)
        if sub == "submissions/ack":
            return self._only(m, "POST") or self.api_ack(req)
        raise HttpError(404, "not_found", "no such endpoint")

    def _only(self, method, *allowed):
        if method in allowed or (method == "HEAD" and "GET" in allowed):
            return None
        resp = self._json(405, {"ok": False, "error": "method_not_allowed",
                                "message": "use %s" % "/".join(allowed)})
        resp.headers.append(("Allow", ", ".join(allowed)))
        return resp

    def _json_body(self, req, cap=None):
        data = req.read_body(cap or self.config.max_admin_json_bytes)
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise HttpError(400, "bad_json", "body is not valid JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "bad_json", "body must be a JSON object")
        return body

    @staticmethod
    def _int(body, key, default, lo, hi):
        value = body.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
            raise HttpError(400, "invalid", "%s must be an integer from %d to %d" % (key, lo, hi))
        return value

    @staticmethod
    def _owner(value):
        if value is None:
            value = "main"
        if not isinstance(value, str) or not OWNER_RE.match(value):
            raise HttpError(400, "invalid", "owner must match %s" % OWNER_RE.pattern)
        return value

    def api_status(self):
        cat, _sizes = self.catalog()
        active, counts = self.store.status()
        return self._json(200, {
            "ok": True,
            "catalog": {"version": cat.version, "styles": len(cat.styles), "updated_at": cat.updated_at},
            "links": {"active": active},
            "submissions": {"new": counts.get("new", 0), "taken": counts.get("taken", 0),
                            "acked": counts.get("acked", 0)},
            "time": self.clock(),
        })

    def api_put_image(self, req, key):
        if not IMAGE_KEY_RE.match(key):
            raise HttpError(400, "bad_key", "image key must be 32 lowercase hex characters")
        data = req.read_body(self.config.max_image_bytes)
        sniffed = sniff_image(data)
        if sniffed is None:
            raise HttpError(400, "bad_image", "only JPEG, PNG or WebP pictures are accepted")
        if image_key(data) != key:
            raise HttpError(400, "key_mismatch", "key must be the first 32 hex characters of the sha256")
        created = self.store.put_image(key, sniffed[0], sniffed[1], sniffed[2], data)
        return self._json(200, {"ok": True, "key": key, "created": created})

    def api_get_catalog(self):
        cat, _sizes = self.catalog()
        return self._json(200, {"ok": True, "version": cat.version, "updated_at": cat.updated_at,
                                "styles": cat.styles})

    def api_put_catalog(self, req):
        body = self._json_body(req, self.config.max_catalog_bytes)
        try:
            styles, keys = validate_catalog(body)
        except CatalogError as exc:
            raise HttpError(400, "invalid_catalog", str(exc))
        version, missing = self.store.replace_catalog(styles, keys)
        if missing:
            raise HttpError(400, "unknown_image", "image(s) not uploaded yet: %s%s" % (
                ", ".join(missing[:5]), " …" if len(missing) > 5 else ""))
        with self._catalog_lock:
            self._catalog = None
        skus = sum(len(c["sizes"]) for s in styles for c in s["colours"])
        log.info("catalogue replaced: version %d, %d style(s), %d sku(s)", version, len(styles), skus)
        return self._json(200, {"ok": True, "version": version, "styles": len(styles), "skus": skus})

    def api_create_link(self, req):
        body = self._json_body(req)
        ref = body.get("ref")
        if not isinstance(ref, str) or not REF_RE.match(ref):
            raise HttpError(400, "invalid", "ref must match %s" % REF_RE.pattern)
        owner = self._owner(body.get("owner"))
        kind = body.get("kind", "personal")
        if kind not in ("personal", "general"):
            raise HttpError(400, "invalid", "kind must be personal or general")
        max_items = self._int(body, "max_items", 3, 1, 10)
        days = self._int(body, "expires_days", 14, 1, 90)
        ask = body.get("ask_account", False)
        if not isinstance(ask, bool):
            raise HttpError(400, "invalid", "ask_account must be true or false")
        max_uses = None
        if kind == "general":
            ask = True
            max_uses = self._int(body, "max_uses", 200, 1, 1000)
        token = new_token()
        expires_at = self.clock() + days * 86400.0
        link_id = self.store.create_link(token_hash(token), ref, owner, kind, max_items, ask,
                                         max_uses, expires_at)
        url = self.base_url(req) + ("/g/" if kind == "general" else "/s/") + token
        log.info("link %d created (%s, owner %s)", link_id, kind, owner)
        return self._json(200, {"ok": True, "id": link_id, "token": token, "url": url,
                                "expires_at": expires_at, "max_items": max_items})

    def api_list_links(self, req):
        owner = self._owner(req.arg("owner") or None)
        now = self.clock()
        out = []
        for link in self.store.list_links(owner):
            out.append({
                "id": link["id"], "ref": link["ref"], "owner": link["owner"], "kind": link["kind"],
                "max_items": link["max_items"], "created_at": link["created_at"],
                "expires_at": link["expires_at"], "revoked": link["revoked_at"] is not None,
                "state": link_state(link, now),
                "uses": link["uses"] if link["kind"] == "general" else (link["sub_rev"] or 0),
                "ask_account": bool(link["ask_account"]), "max_uses": link["max_uses"],
                "parent_id": link["parent_id"],
            })
        return self._json(200, {"ok": True, "links": out})

    def api_revoke(self, link_id):
        if not self.store.revoke_link(link_id):
            raise HttpError(404, "not_found", "no such link")
        log.info("link %d revoked", link_id)
        return self._json(200, {"ok": True})

    def api_pull(self, req):
        body = self._json_body(req)
        owner = self._owner(body.get("owner"))
        subs = self.store.pull(owner)
        out = []
        for sub in subs:
            out.append({
                "id": sub["id"], "rev": sub["rev"], "link_id": sub["link_id"], "ref": sub["ref"],
                "kind": "general" if sub["parent_id"] is not None else "personal",
                "account": sub["account"], "items": sub["items"], "receiver": sub["receiver"],
                "submitted_at": sub["submitted_at"], "taken_at": sub["taken_at"],
                "purged": bool(sub["purged"]),
            })
        log.info("pull (owner %s): %d un-acked submission(s) returned", owner, len(out))
        return self._json(200, {"ok": True, "submissions": out})

    def api_ack(self, req):
        body = self._json_body(req)
        owner = self._owner(body.get("owner"))
        ids = body.get("ids")
        if (not isinstance(ids, list) or len(ids) > 1000
                or any(isinstance(i, bool) or not isinstance(i, int) for i in ids)):
            raise HttpError(400, "invalid", "ids must be a list of at most 1000 integers")
        acked, unknown = self.store.ack(owner, list(dict.fromkeys(ids)))
        log.info("ack (owner %s): %d acked, %d unknown", owner, len(acked), len(unknown))
        return self._json(200, {"ok": True, "acked": acked, "unknown": unknown})

    def close(self):
        self.store.close()


def link_state(link, now):
    if link["revoked_at"] is not None:
        return "revoked"
    if link["kind"] == "general":
        if now >= link["expires_at"]:
            return "expired"
        return "submitted" if link["uses"] else "unused"
    state = link.get("sub_state")
    if state:
        return {"new": "submitted", "taken": "taken", "acked": "acked"}.get(state, state)
    if now >= link["expires_at"]:
        return "expired"
    return "unused"


def create_app(config):
    """Build the WSGI application for ``config`` (a :class:`Config`)."""
    return App(config)


# -- the small stdlib server ---------------------------------------------------


class _ServerHandler(ServerHandler):
    server_software = "sample-site"
    # wsgiref copies the whole process environment (admin token included) into
    # every request environ; nothing here reads configuration from environ.
    os_environ = {}


class _Handler(WSGIRequestHandler):
    """Request handler that logs paths without tokens and no client data."""

    timeout = 20
    server_version = "sample-site"
    sys_version = ""

    def handle(self):
        # Same as wsgiref's, with a Server header that names no Python version.
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_error(414)
            return
        if not self.parse_request():
            return
        handler = _ServerHandler(self.rfile, self.wfile, self.get_stderr(), self.get_environ(),
                                 multithread=False)
        handler.request_handler = self
        handler.run(self.server.get_app())

    def address_string(self):
        return "-"

    def get_environ(self):
        env = WSGIRequestHandler.get_environ(self)
        env["sample_site.socket"] = self.connection  # lets read_body keep its deadline
        env["sample_site.socket_timeout"] = self.timeout
        return env

    def log_request(self, code="-", size="-"):
        access_log.info("%s %s %s", getattr(self, "command", None) or "-",
                        safe_path(getattr(self, "path", "") or ""), code)

    def log_error(self, format, *args):
        code = args[0] if args and isinstance(args[0], int) else "-"
        access_log.info("client error %s", code)

    def log_message(self, format, *args):
        try:
            text = format % args
        except Exception:
            text = format
        access_log.info("%s", scrub(text)[:200])


class _Server(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 64
    max_threads = 64

    def server_bind(self):
        WSGIServer.server_bind(self)
        self._slots = threading.BoundedSemaphore(self.max_threads)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            socketserver.ThreadingMixIn.process_request(self, request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            socketserver.ThreadingMixIn.process_request_thread(self, request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        log.warning("connection error: %s", sys.exc_info()[0].__name__)


def make_wsgi_server(app, host, port):
    """A threaded stdlib server for ``app`` (port 0 picks a free one)."""
    return make_server(host, port, app, server_class=_Server, handler_class=_Handler)


def _purge_loop(app, stop):
    while not stop.wait(min(600, app.config.purge_interval)):
        try:
            app.maybe_purge()
        except Exception:
            _log_exception("purge thread")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as exc:
        sys.stderr.write("sample_site: %s\n" % exc)
        return 2
    app = create_app(config)
    server = make_wsgi_server(app, config.host, config.port)
    stop = threading.Event()
    threading.Thread(target=_purge_loop, args=(app, stop), daemon=True).start()

    def _stop(signum, frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    host, port = server.server_address[:2]
    log.info("sample site listening on http://%s:%d (db %s)", host, port, config.db_path)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        app.close()
        log.info("sample site stopped")
    return 0


if __name__ == "__main__":  # python -m sample_site.app
    sys.exit(main())
