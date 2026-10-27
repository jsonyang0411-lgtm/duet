"""HTTP of the edge protocol (``/edge/*``), one implementation for every server.

The Windows relay server hands its ``/edge/`` requests to ``handle`` (src/relay/http.py);
the fake Windows server (fake.py) runs ``serve``.  Both answer through the same code, so a
behaviour the protocol tests see on the fake is the one the Mac gets from Windows.

The ``service`` behind it has: ``health()``, ``events(after, wait, limit)``, ``ack(upto)``,
``event_file(event_id, n)`` (a path or None), ``put_parties(version, wxids)``,
``private_chats()``, ``upload(name, read, length)``, ``send(request)``, ``send_status(send_id)``
(a dict or None).  A refusal is an ``EdgeError``.

Standard library only, Python 3.9 compatible.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import protocol as P

logger = logging.getLogger(__name__)

_EVENT_FILE = re.compile(r"^/edge/events/(\d+)/files/(\d+)$")
_SEND = re.compile(r"^/edge/sends/([^/]+)$")


def _json(handler: BaseHTTPRequestHandler, status: int, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _authorized(handler: BaseHTTPRequestHandler, token: bytes) -> bool:
    header = handler.headers.get("Authorization", "")
    given = header[7:].strip().encode() if header.startswith("Bearer ") else b""
    return bool(token) and bool(given) and hmac.compare_digest(given, token)


def _body(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise P.bad_request("Content-Length 不对") from None
    if not 0 <= length <= P.MAX_BODY_BYTES:
        raise P.EdgeError(413, "too_large", "请求体太大", close=True)
    try:
        data = json.loads(handler.rfile.read(length).decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise P.bad_request("请求不是有效的 JSON") from None
    if not isinstance(data, dict):
        raise P.bad_request("请求要是 JSON 对象")
    return data


def _stream(handler: BaseHTTPRequestHandler, path: str) -> None:
    with open(path, "rb") as f:                  # streamed: a file can be large
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
        handler.end_headers()
        shutil.copyfileobj(f, handler.wfile, 1024 * 1024)


def handle(handler: BaseHTTPRequestHandler, method: str, service, token: bytes) -> None:
    """Answer one ``/edge/*`` request; ``service`` None: the edge is not on (404)."""
    url = urlsplit(handler.path)
    if service is None:
        _json(handler, 404, {"error": "not_found", "detail": "这台机器没有打开 PRM_EDGE"})
        return
    if not _authorized(handler, token):
        logger.warning("EDGE: refused %s %s from %s (bad token)", method, url.path, handler.client_address[0])
        handler.close_connection = True
        _json(handler, 401, {"error": "unauthorized", "detail": "口令不对"})
        return
    note_client = getattr(service, "note_client", None)   # the Windows edge logs where the Mac is
    if note_client is not None:
        note_client(handler.client_address[0])
    try:
        _route(handler, method, url.path, parse_qs(url.query), service)
    except P.EdgeError as e:
        if e.close:
            handler.close_connection = True
        _json(handler, e.status, e.body())
    except BrokenPipeError:
        pass                                     # the Mac went away mid-answer
    except Exception as e:                       # never a traceback to the other side
        logger.exception("EDGE: %s %s failed", method, url.path)
        handler.close_connection = True
        _json(handler, 500, {"error": "internal", "detail": f"Windows 这边出错：{type(e).__name__}"})


def _route(handler, method: str, path: str, query: dict, service) -> None:
    def one(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    def need(*methods: str) -> None:
        if method not in methods:
            raise P.EdgeError(405, "method_not_allowed", f"{path} 只接受 {'/'.join(methods)}")

    if path == "/edge/health":
        need("GET")
        _json(handler, 200, service.health())
    elif path == "/edge/events":
        need("GET")
        after = int(P.clamp(one("after", "0"), 0, 2 ** 62, 0))
        wait = P.clamp(one("wait", str(P.DEFAULT_WAIT_SEC)), 0, P.MAX_WAIT_SEC, P.DEFAULT_WAIT_SEC)
        limit = int(P.clamp(one("limit", str(P.DEFAULT_LIMIT)), 1, P.MAX_LIMIT, P.DEFAULT_LIMIT))
        _json(handler, 200, {"events": service.events(after, wait, limit)})
    elif path == "/edge/events/ack":
        need("POST")
        body = _body(handler)
        upto = body.get("upto")
        if isinstance(upto, bool) or not isinstance(upto, int) or upto < 0:
            raise P.bad_request("upto 要是不小于 0 的整数")
        _json(handler, 200, service.ack(upto))
    elif _EVENT_FILE.match(path):
        need("GET")
        m = _EVENT_FILE.match(path)
        found = service.event_file(int(m.group(1)), int(m.group(2)))
        if not found or not os.path.isfile(found):
            raise P.EdgeError(404, "not_found", "没有这个附件（或已经删了）")
        _stream(handler, found)
    elif path == "/edge/parties":
        need("PUT")
        version, wxids = P.check_parties(_body(handler))
        _json(handler, 200, service.put_parties(version, wxids))
    elif path == "/edge/private-chats":
        need("GET")
        _json(handler, 200, {"chats": service.private_chats()})
    elif path == "/edge/uploads":
        need("POST")
        name = one("name").strip()
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > P.MAX_UPLOAD_BYTES:
            raise P.EdgeError(413, "too_large", f"文件最大 {P.MAX_UPLOAD_BYTES // 1024 // 1024} MB", close=True)
        if not name or length <= 0:
            raise P.EdgeError(400, "bad_request", "要给出 name 和 Content-Length", close=True)
        _json(handler, 200, service.upload(name, handler.rfile.read, length))
    elif path == "/edge/sends":
        need("POST")
        _json(handler, 200, service.send(P.check_send(_body(handler))))
    elif _SEND.match(path):
        need("GET")
        status = service.send_status(unquote(_SEND.match(path).group(1)))
        if status is None:
            raise P.EdgeError(404, "not_found", "没有这个发送请求")
        _json(handler, 200, status)
    else:
        raise P.EdgeError(404, "not_found", f"没有 {path}")


def serve(service, token: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """A server for ``/edge/*`` only (the fake Windows edge), in a daemon thread; ``port=0`` picks one."""
    secret = token.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "webot-edge/1"

        def log_message(self, fmt, *args):
            logger.debug("edge http %s - " + fmt, self.client_address[0], *args)

        def _any(self, method: str) -> None:
            if urlsplit(self.path).path.startswith(P.PATH_PREFIX):
                handle(self, method, service, secret)
            else:
                _json(self, 404, {"error": "not_found", "detail": "这里只有 /edge/"})

        def do_GET(self):
            self._any("GET")

        def do_POST(self):
            self._any("POST")

        def do_PUT(self):
            self._any("PUT")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="edge-http", daemon=True).start()
    return server
