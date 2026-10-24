"""The relay's HTTP side: the Mac worker asks for jobs and sends results.

Every request carries ``Authorization: Bearer <CLAUDE_RELAY_TOKEN>``.

    GET  /relay/health                 {"ok": true}           (the worker's start-up check)
    GET  /relay/next?wait=25           a job, or 204 after ``wait`` seconds without one
                                       (headers X-Relay-Projects: JSON list of aliases, X-Relay-Default;
                                       X-Relay-Kinds: "admin,prm" = the job kinds this worker does,
                                       without it only admin jobs; the job says its "kind" and "meta")
    POST /relay/jobs/<id>/outfiles?name=<file name>   the bytes of a file a running prm job sends
                                       back (Content-Length, at most 100 MB, 9 per job)
                                       -> {"id", "size", "sha256"}  (404: not a running prm job)
    GET  /relay/jobs/<id>/files/<n>    a picture or file sent with the job (bytes; 404: not one of its files)
    POST /relay/jobs/<id>/heartbeat    {"steps", "doing", "plan"} (what Claude is doing, optional)
                                       -> {"cancel": bool}    (404: unknown job)
    POST /relay/jobs/<id>/ask          {"tool", "detail", "timeout_s"} -> {"ask_id"}  (an approval for
                                       WeChat; 404: the job is not running)
    GET  /relay/asks/<n>?wait=25       {"status": pending|allow|deny|expired|cancelled}
    GET  /relay/jobs/<id>/messages?after=<n>&wait=25   {"messages": [{"id", "text"}]}  (what the person
                                       sent while the job runs, past follow-up <n>)
    POST /relay/jobs/<id>/close        {"upto": n} -> {"messages": [...]}  ([] = the job takes no more)
    POST /relay/jobs/<id>/result       {"ok", "text", "session_id", "duration_s", "cost_usd",
                                        "log_path", "error"} -> {"accepted": bool}

A separate server on its own port (not the console's), standard library only.
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
from urllib.parse import parse_qs, urlsplit

from .store import ADMIN, KINDS

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000
_JOB_PATH = re.compile(r"^/relay/jobs/(\d+)/(heartbeat|result|ask|close)$")
_MESSAGES_PATH = re.compile(r"^/relay/jobs/(\d+)/messages$")
_ASK_PATH = re.compile(r"^/relay/asks/(\d+)$")
_FILE_PATH = re.compile(r"^/relay/jobs/(\d+)/files/(\d+)$")
_OUTFILE_PATH = re.compile(r"^/relay/jobs/(\d+)/outfiles$")


def worker_kinds(header: str | None) -> tuple[str, ...]:
    """The job kinds a worker asks for (X-Relay-Kinds); an older worker sends none: admin only."""
    if not header:
        return (ADMIN,)
    kinds = tuple(dict.fromkeys(k.strip() for k in header.split(",") if k.strip() in KINDS))
    return kinds or (ADMIN,)


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
        elif _MESSAGES_PATH.match(url.path):
            query = parse_qs(url.query)
            try:
                after = int(query.get("after", ["0"])[0])
                wait = float(query.get("wait", ["25"])[0])
            except ValueError:
                after, wait = 0, 25.0
            answer = self.relay.followups(int(_MESSAGES_PATH.match(url.path).group(1)), after, wait)
            if answer is None:
                self._json(404, {"error": "unknown job"})
            else:
                self._json(200, answer)
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
            with open(path, "rb") as f:               # streamed: a file can be a gigabyte
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
                self.end_headers()
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
        elif url.path == "/relay/next":
            try:
                wait = float(parse_qs(url.query).get("wait", ["25"])[0])
            except ValueError:
                wait = 25.0
            kinds = worker_kinds(self.headers.get("X-Relay-Kinds"))
            self.relay.note_worker(None, kinds=kinds)
            projects = self.headers.get("X-Relay-Projects")
            if projects is not None:
                try:
                    listed = json.loads(projects)
                    default = json.loads(self.headers.get("X-Relay-Default") or '""')
                    if isinstance(listed, list):
                        self.relay.note_worker(listed, default if isinstance(default, str) else "")
                except ValueError:
                    pass
            job = self.relay.next_job(wait, kinds)
            if job is None:
                self._json(204)
            else:
                self._json(200, job)
        else:
            self._json(404, {"error": "not found"})

    def _outfile(self, job_id: int, query: str) -> None:
        """POST /relay/jobs/<id>/outfiles?name=…: the raw bytes of one file (not JSON)."""
        name = parse_qs(query).get("name", [""])[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not name or length <= 0:
            self._json(400, {"error": "name and Content-Length are needed"})
            return
        try:
            answer = self.relay.outfile(job_id, name, self.rfile.read, length)
        except OSError as e:
            logger.warning("CLAUDE_RELAY: a file for #%d did not arrive whole: %s", job_id, e)
            self.close_connection = True
            self._json(400, {"error": "incomplete upload"})
            return
        if answer is None:
            self.close_connection = True               # the body may not have been read
            self._json(404, {"error": "not a running prm job, too big, or too many files"})
        else:
            self._json(200, answer)

    def do_POST(self):
        if not self._authorized():
            return
        url = urlsplit(self.path)
        m = _OUTFILE_PATH.match(url.path)
        if m:
            self._outfile(int(m.group(1)), url.query)
            return
        m = _JOB_PATH.match(url.path)
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
        elif action == "close":
            try:
                upto = int(body.get("upto") or 0)
            except (TypeError, ValueError):
                upto = 0
            answer = self.relay.close_input(job_id, upto)
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
