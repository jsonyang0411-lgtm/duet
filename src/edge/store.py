"""SQLite for the Windows WeChat edge (``EDGE_DB``, default ``data/edge.db``).

- ``edge_events`` / ``edge_files``: what the Mac pulls (``/edge/events``) and the files of
  each event; a creator's or operator's message is there once (chat + WeChat message id).
- ``edge_sends``: the Mac's send requests; one row per ``send_id``, so it is sent once.
  After ``EDGE_KEEP_DAYS`` the text is blanked, the row stays (a retry still finds it).
- ``edge_uploads``: files the Mac uploaded for a send (one send each).
- ``edge_parties`` / ``edge_meta``: the list of creators and its version.

One connection behind a lock: the HTTP threads, the route step and the sender share it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from . import protocol as P

_SCHEMA = """
CREATE TABLE IF NOT EXISTS edge_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    at REAL NOT NULL,
    chat_id TEXT NOT NULL DEFAULT '',
    message_id TEXT,
    body TEXT NOT NULL DEFAULT '{}',
    acked_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS edge_events_message ON edge_events(chat_id, message_id)
    WHERE message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS edge_files (
    event_id INTEGER NOT NULL,
    n INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    kind TEXT NOT NULL,
    owned INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (event_id, n)
);
CREATE TABLE IF NOT EXISTS edge_sends (
    send_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    uploads TEXT NOT NULL DEFAULT '[]',
    ttl_sec INTEGER NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    deadline REAL NOT NULL,
    sent_at REAL,
    seen_at REAL,
    unseen_at REAL,
    finished_at REAL,
    cleared INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS edge_sends_status ON edge_sends(status, created_at);
CREATE TABLE IF NOT EXISTS edge_uploads (
    upload_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    send_id TEXT
);
CREATE TABLE IF NOT EXISTS edge_parties (wxid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS edge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _event_view(row: sqlite3.Row) -> dict:
    """An event as the Mac gets it: id, type, at, then its own fields."""
    out = {"id": int(row["id"]), "type": row["type"], "at": round(float(row["at"]), 3)}
    if row["chat_id"]:
        out["chat_id"] = row["chat_id"]
    if row["message_id"] is not None:
        out["message_id"] = row["message_id"]
    try:
        body = json.loads(row["body"] or "{}")
    except ValueError:
        body = {}
    out.update(body if isinstance(body, dict) else {})
    return out


def _send_view(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["uploads"] = json.loads(out.get("uploads") or "[]")
    return out


class EdgeStore:
    def __init__(self, path: str | Path = "data/edge.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── events ────────────────────────────────────────────────────────────

    def add_event(self, kind: str, at: float, chat_id: str = "", message_id: Optional[str] = None,
                  files: Iterable[dict] = (), **fields) -> Optional[dict]:
        """Store an event (with its files: ``{"n", "name", "path", "size", "kind", "owned"}``, or
        ``{"n", "name", "missing"}``); None when this chat's message is there already."""
        files = list(files)
        public = [({"n": f["n"], "name": f["name"], "missing": f["missing"]} if f.get("missing") else
                   {"n": f["n"], "name": f["name"], "size": int(f["size"]), "kind": f["kind"]}) for f in files]
        body = dict(fields)
        if kind == P.CREATOR_MESSAGE:
            body["files"] = public
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO edge_events (type, at, chat_id, message_id, body) VALUES (?, ?, ?, ?, ?)",
                    (kind, at, chat_id, message_id, json.dumps(body, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None
            event_id = int(cur.lastrowid)
            self._conn.executemany(
                "INSERT INTO edge_files (event_id, n, name, path, size, kind, owned) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(event_id, f["n"], f["name"], f["path"], int(f["size"]), f["kind"], int(bool(f.get("owned", True))))
                 for f in files if not f.get("missing")])
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM edge_events WHERE id = ?", (event_id,)).fetchone()
        return _event_view(row)

    def events_after(self, after: int, limit: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM edge_events WHERE id > ? ORDER BY id LIMIT ?",
                                      (int(after), int(limit))).fetchall()
        return [_event_view(r) for r in rows]

    def ack(self, upto: int, now: float) -> int:
        with self._lock:
            self._conn.execute("UPDATE edge_events SET acked_at = ? WHERE id <= ? AND acked_at IS NULL",
                               (now, int(upto)))
            self._conn.commit()
        return self.pending()

    def pending(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM edge_events WHERE acked_at IS NULL").fetchone()[0])

    def event_file(self, event_id: int, n: int) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT path FROM edge_files WHERE event_id = ? AND n = ?",
                                     (int(event_id), int(n))).fetchone()
        return row["path"] if row else None

    def drop_events(self, acked_before: float, unacked_before: float) -> list[str]:
        """Delete events acknowledged before ``acked_before`` and unacknowledged ones from
        before ``unacked_before``; the paths of the files the edge made for them (to delete)."""
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM edge_events WHERE (acked_at IS NOT NULL AND acked_at < ?)"
                " OR (acked_at IS NULL AND at < ?)", (acked_before, unacked_before)).fetchall()]
            paths: list[str] = []
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                marks = ",".join("?" * len(part))
                paths += [r["path"] for r in self._conn.execute(
                    f"SELECT path FROM edge_files WHERE owned = 1 AND event_id IN ({marks})", part).fetchall()]
                self._conn.execute(f"DELETE FROM edge_files WHERE event_id IN ({marks})", part)
                self._conn.execute(f"DELETE FROM edge_events WHERE id IN ({marks})", part)
            self._conn.commit()
        return paths

    # ── sends ─────────────────────────────────────────────────────────────

    def send(self, send_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM edge_sends WHERE send_id = ?", (send_id,)).fetchone()
        return _send_view(row) if row else None

    def add_send(self, request: dict, digest: str, now: float) -> bool:
        """Queue a send; False when the send_id is there already (the caller looks at it)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO edge_sends (send_id, chat_id, text, uploads, ttl_sec, digest, status, created_at,"
                    " deadline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (request["send_id"], request["chat_id"], request["text"], json.dumps(request["uploads"]),
                     request["ttl_sec"], digest, P.QUEUED, now, now + request["ttl_sec"]))
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            self._conn.executemany("UPDATE edge_uploads SET send_id = ? WHERE upload_id = ?",
                                   [(request["send_id"], u) for u in request["uploads"]])
            self._conn.commit()
        return True

    def move_send(self, send_id: str, from_states: tuple, to_state: str, **fields) -> bool:
        """Compare-and-set: only a send in one of ``from_states`` moves (a send never goes twice)."""
        allowed = {"error", "sent_at", "finished_at"}
        if set(fields) - allowed:
            raise ValueError(f"not a send field: {sorted(set(fields) - allowed)}")
        names = sorted(fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE edge_sends SET status = ?{''.join(f', {n} = ?' for n in names)}"
                f" WHERE send_id = ? AND status IN ({','.join('?' * len(from_states))})",
                (to_state, *[fields[n] for n in names], send_id, *from_states))
            self._conn.commit()
            return cur.rowcount == 1

    def sends_in(self, states: tuple, chat_id: Optional[str] = None, limit: int = 500) -> list[dict]:
        query = f"SELECT * FROM edge_sends WHERE status IN ({','.join('?' * len(states))})"
        args: list = list(states)
        if chat_id is not None:
            query += " AND chat_id = ?"
            args.append(chat_id)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY created_at, rowid LIMIT ?", (*args, limit)).fetchall()
        return [_send_view(r) for r in rows]

    def sent_since(self, since: float) -> int:
        """How many sends went out since ``since`` (the hourly limit)."""
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM edge_sends WHERE sent_at IS NOT NULL AND sent_at >= ?",
                                          (since,)).fetchone()[0])

    def mark_seen(self, send_id: str, now: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE edge_sends SET seen_at = ? WHERE send_id = ? AND status = ? AND seen_at IS NULL",
                (now, send_id, P.SENT))
            self._conn.commit()
            return cur.rowcount == 1

    def mark_unseen(self, send_id: str, now: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE edge_sends SET unseen_at = ? WHERE send_id = ? AND seen_at IS NULL AND unseen_at IS NULL",
                (now, send_id))
            self._conn.commit()
            return cur.rowcount == 1

    def waiting_read_back(self) -> list[dict]:
        """Sends that went out and were neither read back nor reported unseen yet."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM edge_sends WHERE status = ? AND seen_at IS NULL AND unseen_at IS NULL"
                " AND sent_at IS NOT NULL ORDER BY sent_at", (P.SENT,)).fetchall()
        return [_send_view(r) for r in rows]

    def unconfirmed(self, chat_id: str, since: float) -> list[dict]:
        """Sends to this chat that went out after ``since`` and were not read back (newest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM edge_sends WHERE chat_id = ? AND status = ? AND seen_at IS NULL AND sent_at >= ?"
                " ORDER BY sent_at DESC", (chat_id, P.SENT, since)).fetchall()
        return [_send_view(r) for r in rows]

    def clear_sends(self, blank_before: float, drop_before: float) -> tuple[int, int]:
        """Blank the text of sends finished before ``blank_before`` (the row stays: a retry of
        that send_id still finds it) and delete those finished before ``drop_before``."""
        with self._lock:
            blanked = self._conn.execute(
                "UPDATE edge_sends SET text = '', cleared = 1 WHERE cleared = 0 AND finished_at IS NOT NULL"
                " AND finished_at < ?", (blank_before,)).rowcount
            dropped = self._conn.execute("DELETE FROM edge_sends WHERE finished_at IS NOT NULL AND finished_at < ?",
                                         (drop_before,)).rowcount
            self._conn.commit()
        return blanked, dropped

    # ── uploads ───────────────────────────────────────────────────────────

    def add_upload(self, upload_id: str, name: str, path: str, size: int, sha256: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO edge_uploads (upload_id, name, path, size, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (upload_id, name, path, size, sha256, now))
            self._conn.commit()

    def upload(self, upload_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM edge_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
        return dict(row) if row else None

    def drop_uploads(self, unused_before: float, finished_before: float) -> list[str]:
        """Delete uploads no send used since ``unused_before`` and those of sends finished before
        ``finished_before``; their paths (to delete)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.upload_id, u.path FROM edge_uploads u LEFT JOIN edge_sends s ON s.send_id = u.send_id"
                " WHERE (u.send_id IS NULL AND u.created_at < ?)"
                " OR (s.finished_at IS NOT NULL AND s.finished_at < ?)"
                " OR (u.send_id IS NOT NULL AND s.send_id IS NULL AND u.created_at < ?)",
                (unused_before, finished_before, finished_before)).fetchall()
            self._conn.executemany("DELETE FROM edge_uploads WHERE upload_id = ?", [(r["upload_id"],) for r in rows])
            self._conn.commit()
        return [r["path"] for r in rows]

    # ── the list of creators ──────────────────────────────────────────────

    def parties(self) -> set[str]:
        with self._lock:
            return {r["wxid"] for r in self._conn.execute("SELECT wxid FROM edge_parties").fetchall()}

    def set_parties(self, version: int, digest: str, wxids: Iterable[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM edge_parties")
            self._conn.executemany("INSERT OR IGNORE INTO edge_parties (wxid) VALUES (?)", [(w,) for w in wxids])
            self._conn.executemany("INSERT OR REPLACE INTO edge_meta (key, value) VALUES (?, ?)",
                                   [("parties_version", str(int(version))), ("parties_digest", digest)])
            self._conn.commit()

    def meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM edge_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def parties_version(self) -> int:
        return int(self.meta("parties_version", "0") or 0)

    def parties_digest(self) -> str:
        return self.meta("parties_digest", P.parties_digest([]))
