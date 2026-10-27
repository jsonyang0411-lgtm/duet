"""Client of the Windows WeChat edge (edge-protocol.md) for the Mac business service.

Standard library only, Python 3.9+.  Proxies are ignored: a proxy on the Mac (Clash and
the like) must not carry LAN traffic.  Network errors (``OSError``: the Windows PC is off,
Wi-Fi changed) are left to the caller, who retries with a back-off; refusals of the edge
are ``EdgeError`` with the HTTP status and the edge's reason.  When the Mac and Windows are
not on one LAN, the address is Windows' Tailscale one, the same everywhere (cross-network.md).

The usual loop::

    edge = EdgeClient("http://<Windows 的局域网 IP 或 Tailscale 地址>:8790", token)
    edge.put_parties(version, wxids)            # whenever the list changes, and when
                                                # edge.health()["parties_version"] differs
    cursor = load_cursor()                      # the last event id the Mac has stored
    while True:
        events = edge.events(after=cursor, wait=25)
        for ev in events:
            for f in ev.get("files", []):
                if not f.get("missing"):
                    edge.download(ev["id"], f["n"], folder / f["name"])
            store(ev)                           # idempotent by ev["id"]: events can come again
            cursor = ev["id"]
        if events:
            save_cursor(cursor)
            edge.ack(cursor)                    # only after the events and files are stored

Sending::

    up = edge.upload("/path/brief.pdf")
    edge.send("pk-88", "wxid_a", "样品这周五前寄出～", uploads=[up["upload_id"]])
    # then watch for send_result / send_seen / send_unseen events with send_id "pk-88"
"""

from __future__ import annotations

import io
import json
import os
import shutil
import urllib.error
import urllib.request
from typing import Iterable, Optional
from urllib.parse import quote, urlencode

from . import protocol as P


class EdgeError(Exception):
    """The edge refused a request: ``status`` (HTTP), ``code`` and ``detail`` (its reason), ``body``."""

    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.code = str(self.body.get("error") or "")
        self.detail = str(self.body.get("detail") or "")
        super().__init__(f"HTTP {status} {self.code}: {self.detail}")


class EdgeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ── plumbing ──────────────────────────────────────────────────────────

    def _open(self, method: str, path: str, data=None, headers: Optional[dict] = None,
              timeout: Optional[float] = None):
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            return self._opener.open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                body = {}
            raise EdgeError(e.code, body) from None

    def _call(self, method: str, path: str, body: Optional[dict] = None,
              timeout: Optional[float] = None) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else None
        with self._open(method, path, data, headers, timeout) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}

    # ── the protocol ──────────────────────────────────────────────────────

    def health(self) -> dict:
        """``{"ok", "protocol", "parties_version", "wechat", "db", "send", "detail", ...}``."""
        return self._call("GET", "/edge/health")

    def events(self, after: int = 0, wait: float = P.DEFAULT_WAIT_SEC, limit: int = P.DEFAULT_LIMIT) -> list:
        """Events with an id above ``after``, oldest first; waits up to ``wait`` seconds for one."""
        query = urlencode({"after": int(after), "wait": wait, "limit": int(limit)})
        answer = self._call("GET", f"/edge/events?{query}", timeout=float(wait) + self.timeout)
        return list(answer.get("events") or [])

    def ack(self, upto: int) -> dict:
        """The Mac has stored every event up to ``upto`` (and its files): Windows may let them go."""
        return self._call("POST", "/edge/events/ack", {"upto": int(upto)})

    def download(self, event_id: int, n: int, dest) -> int:
        """Save attachment ``n`` of an event to ``dest`` (written through a .part file); its size."""
        dest = os.fspath(dest)
        part = dest + ".part"
        with self._open("GET", f"/edge/events/{int(event_id)}/files/{int(n)}", timeout=self.timeout * 8) as r:
            with open(part, "wb") as out:
                shutil.copyfileobj(r, out, 1024 * 1024)
        os.replace(part, dest)
        return os.path.getsize(dest)

    def put_parties(self, version: int, wxids: Iterable[str]) -> dict:
        """Replace the list of creators: ``{"ok", "version", "rejected": [{"wxid", "reason"}]}``.
        A version not above Windows' one (unless the very same push again) is EdgeError 409
        whose ``body["current_version"]`` tells the version to go above."""
        return self._call("PUT", "/edge/parties", {"version": int(version), "wxids": list(wxids)})

    def private_chats(self) -> list:
        """``[{"wxid", "name"}]``: the bot account's recent private chats (for registering a creator)."""
        return list(self._call("GET", "/edge/private-chats").get("chats") or [])

    def upload(self, path, name: Optional[str] = None) -> dict:
        """Upload a file for a later send: ``{"upload_id", "size", "sha256"}``."""
        path = os.fspath(path)
        size = os.path.getsize(path)
        filename = name or os.path.basename(path)
        with open(path, "rb") as f:
            with self._open("POST", f"/edge/uploads?{urlencode({'name': filename})}", data=f,
                            headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
                            timeout=self.timeout * 8) as response:
                return json.loads(response.read().decode("utf-8"))

    def upload_bytes(self, data: bytes, name: str) -> dict:
        """``upload`` of bytes in memory, also sent block by block (a slow link does not hit the
        timeout as one send of everything would)."""
        with self._open("POST", f"/edge/uploads?{urlencode({'name': name})}", data=io.BytesIO(bytes(data)),
                        headers={"Content-Length": str(len(data)), "Content-Type": "application/octet-stream"},
                        timeout=self.timeout * 8) as response:
            return json.loads(response.read().decode("utf-8"))

    def send(self, send_id: str, chat_id: str, text: str = "", uploads: Iterable[str] = (),
             ttl_sec: int = P.TTL_DEFAULT_SEC) -> dict:
        """Ask Windows to send; ``{"send_id", "status"}``.  The same ``send_id`` again is never
        sent twice (the same content: its status; other content: EdgeError 409)."""
        return self._call("POST", "/edge/sends", {"send_id": send_id, "chat_id": chat_id, "text": text,
                                                  "uploads": list(uploads), "ttl_sec": int(ttl_sec)})

    def send_status(self, send_id: str) -> Optional[dict]:
        """``{"send_id", "status", "error", "sent_at", "seen_at"}``, or None when Windows has no such send."""
        try:
            return self._call("GET", f"/edge/sends/{quote(send_id, safe='')}")
        except EdgeError as e:
            if e.status == 404:
                return None
            raise
