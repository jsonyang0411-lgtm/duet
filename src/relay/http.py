"""The relay's HTTP side: the Mac worker asks for jobs and sends results.

Every request carries ``Authorization: Bearer <CLAUDE_RELAY_TOKEN>``.

    GET  /relay/health                 {"ok": true}           (the worker's start-up check)
    GET  /relay/next?wait=25           a job, or 204 after ``wait`` seconds without one
                                       (headers X-Relay-Projects: JSON list of aliases, X-Relay-Default)
    GET  /relay/jobs/<id>/files/<n>    a picture or file sent with the job (bytes; 404: not one of its files)
    POST /relay/jobs/<id>/heartbeat    {"steps", "doing", "plan"} (what Claude is doing, optional)
                                       -> {"cancel": bool}    (404: unknown job)
    POST /relay/jobs/<id>/ask          {"tool", "detail", "timeout_s"} -> {"ask_id"}  (an approval for
                                       WeChat; 404: the job is not running)
    GET  /relay/asks/<n>?wait=25       {"status": pending|allow|deny|expired|cancelled}
    POST /relay/jobs/<id>/result       {"ok", "text", "session_id", "duration_s", "cost_usd",
                                        "log_path", "error"} -> {"accepted": bool}

A separate server on its own port (not the console's), standard library only.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000
_JOB_PATH = re.compile(r"^/relay/jobs/(\d+)/(heartbeat|result|ask)$")
_ASK_PATH = re.compile(r"^/relay/asks/(\d+)$")
_FILE_PATH = re.compile(r"^/relay/jobs/(\d+)/files/(\d+)$")


class _RelayHandler(BaseHTTPRequestHandler):
    server_version = "webot-relay/1"
    relay = None            # set per server by serve()
    token = b""

    def log_message(self, fmt, *args):
        logger.debug("relay http %s - " + fmt, self.client_address[0], *args)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        given = header[7:].strip().encode() if header.startswith("Bearer ") else b""
        if given and hmac.compare_digest(given, self.token):
            return True
        logger.warning("CLAUDE_RELAY: refused %s %s from %s (bad token)",
                       self.command, urlsplit(self.path).path, self.client_address[0])
        self._json(401, {"error": "unauthorized"})
        return False

    def _json(self, status: int, data: dict | None = None) -> None:
        body = b"" if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if data is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 <= length <= MAX_BODY_BYTES:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        if not self._authorized():
            return
        url = urlsplit(self.path)
        if url.path == "/relay/health":
            self._json(200, {"ok": True})
        elif _ASK_PATH.match(url.path):
            try:
                wait = float(parse_qs(url.query).get("wait", ["25"])[0])
            except ValueError:
                wait = 25.0
            answer = self.relay.ask_status(int(_ASK_PATH.match(url.path).group(1)), wait)
            if answer is None:
                self._json(404, {"error": "unknown request"})
            else:
                self._json(200, answer)
        elif _FILE_PATH.match(url.path):
            m = _FILE_PATH.match(url.path)
            path = self.relay.file_path(int(m.group(1)), int(m.group(2)))
            if path is None:
                self._json(404, {"error": "unknown file"})
                return
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/relay/next":
            try:
                wait = float(parse_qs(url.query).get("wait", ["25"])[0])
            except ValueError:
                wait = 25.0
            projects = self.headers.get("X-Relay-Projects")
            if projects is not None:
                try:
                    listed = json.loads(projects)
                    default = json.loads(self.headers.get("X-Relay-Default") or '""')
                    if isinstance(listed, list):
                        self.relay.note_worker(listed, default if isinstance(default, str) else "")
                except ValueError:
                    pass
            job = self.relay.next_job(wait)
            if job is None:
                self._json(204)
            else:
                self._json(200, job)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        m = _JOB_PATH.match(urlsplit(self.path).path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        job_id, action = int(m.group(1)), m.group(2)
        body = self._body()
        if body is None:
            self._json(400, {"error": "bad json"})
            return
        if action == "heartbeat":
            answer = self.relay.heartbeat(job_id, body)
        elif action == "ask":
            answer = self.relay.ask(job_id, body)
        else:
            taken = self.relay.finish(job_id, body)
            answer = None if taken is None else {"accepted": taken}
        if answer is None:
            self._json(404, {"error": "unknown job"})
        else:
            self._json(200, answer)


def serve(relay, host: str, port: int, token: str) -> ThreadingHTTPServer:
    """Start the server in a daemon thread; ``port=0`` picks a free port (tests)."""
    handler = type("RelayHandler", (_RelayHandler,), {"relay": relay, "token": token.encode()})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="claude-relay-http", daemon=True).start()
    return server
