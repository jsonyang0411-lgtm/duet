"""The relay's job queue (``CLAUDE_RELAY=on``), one SQLite file.

A job is one request from WeChat for Claude Code on the Mac.  Its life:
``pending`` (queued) -> ``running`` (a worker took it) -> ``done`` / ``failed``
(the worker sent the result) or ``cancelled``.  A running job whose worker
stops sending heartbeats becomes ``lost``; a result that arrives later still
finishes it.  ``delivered`` says whether WeChat has been told about the last
state change, so a failed send is retried and a restart loses nothing.

The router thread adds jobs, the HTTP threads hand them out and take the
results, the delivery thread marks them sent: one connection, one lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

FINAL_STATES = ("done", "failed", "cancelled", "lost")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    claimed_at REAL,
    heartbeat_at REAL,
    finished_at REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    result_text TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    delivered INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS relay_jobs_status ON relay_jobs(status, id);
CREATE TABLE IF NOT EXISTS relay_sessions (
    chat_id TEXT PRIMARY KEY,
    session_id TEXT,
    updated_at REAL NOT NULL,
    project TEXT NOT NULL DEFAULT ''
);
"""


class RelayStore:
    def __init__(self, path: str | Path = "data/claude_relay.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(relay_sessions)")}
            if "project" not in columns:              # a database from before the current project
                self._conn.execute("ALTER TABLE relay_sessions ADD COLUMN project TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── from WeChat ───────────────────────────────────────────────────────

    def add(self, chat_id: str, sender_id: str, text: str, project: str = "",
            now: Optional[float] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO relay_jobs (chat_id, sender_id, text, project, status, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (chat_id, sender_id, text, project, time.time() if now is None else now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get(self, job_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, chat_id: str, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_jobs WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_jobs(self, limit: int = 30) -> list[dict]:
        """The newest jobs of every chat (the console's task list)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_jobs ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict:
        """How many jobs are in each state."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM relay_jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def pending_before(self, job_id: int) -> int:
        """Jobs that will run before this one (queued earlier or running now)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM relay_jobs WHERE id < ? AND status IN ('pending', 'running')",
                (job_id,),
            ).fetchone()
        return int(row[0])

    def latest_open(self, chat_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relay_jobs WHERE chat_id = ? AND status IN ('pending', 'running')"
                " ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def cancel(self, job_id: int, now: Optional[float] = None) -> str:
        """Cancel a job: returns what happened ("cancelled", "requested", or its final state).

        A queued job is cancelled at once; a running one is asked to stop and
        the worker sees it in its next heartbeat.
        """
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT status FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return "missing"
            status = row["status"]
            if status == "pending":
                self._conn.execute(
                    "UPDATE relay_jobs SET status = 'cancelled', finished_at = ?, delivered = 1"
                    " WHERE id = ?", (now, job_id),
                )
                self._conn.commit()
                return "cancelled"
            if status == "running":
                self._conn.execute("UPDATE relay_jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
                self._conn.commit()
                return "requested"
            return status

    def current_project(self, chat_id: str) -> str:
        """The project later tasks of this chat run in ("" = the Mac's default)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT project FROM relay_sessions WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return str(row["project"]) if row else ""

    def set_project(self, chat_id: str, project: str, now: Optional[float] = None) -> None:
        """Switch the chat to ``project``; the next task starts a new Claude session there."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO relay_sessions (chat_id, session_id, updated_at, project) VALUES (?, NULL, ?, ?)"
                " ON CONFLICT(chat_id) DO UPDATE SET session_id = NULL, updated_at = excluded.updated_at,"
                " project = excluded.project",
                (chat_id, time.time() if now is None else now, project),
            )
            self._conn.commit()

    def reset_session(self, chat_id: str, now: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO relay_sessions (chat_id, session_id, updated_at) VALUES (?, NULL, ?)"
                " ON CONFLICT(chat_id) DO UPDATE SET session_id = NULL, updated_at = excluded.updated_at",
                (chat_id, time.time() if now is None else now),
            )
            self._conn.commit()

    # ── from the worker ───────────────────────────────────────────────────

    def claim_next(self, now: Optional[float] = None) -> Optional[dict]:
        """Hand the oldest queued job to a worker.

        The Claude session is looked up now, not when the job was queued: the
        worker runs one job at a time, so a follow-up queued behind another job
        of the same chat continues the session that job just left.
        """
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relay_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            session = self._conn.execute(
                "SELECT session_id FROM relay_sessions WHERE chat_id = ?", (row["chat_id"],)
            ).fetchone()
            session_id = session["session_id"] if session else None
            self._conn.execute(
                "UPDATE relay_jobs SET status = 'running', claimed_at = ?, heartbeat_at = ?,"
                " session_id = ? WHERE id = ?",
                (now, now, session_id, row["id"]),
            )
            self._conn.commit()
            job = dict(row)
        job.update(status="running", claimed_at=now, heartbeat_at=now, session_id=session_id)
        return job

    def has_pending(self) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM relay_jobs WHERE status = 'pending' LIMIT 1"
            ).fetchone() is not None

    def heartbeat(self, job_id: int, now: Optional[float] = None) -> Optional[dict]:
        """Note that the worker is alive; returns ``{"cancel": bool}``, or None for an unknown job.

        A job marked lost while the worker was unreachable is running again.
        """
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT status, cancel_requested FROM relay_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] in ("running", "lost"):
                self._conn.execute(
                    "UPDATE relay_jobs SET status = 'running', heartbeat_at = ? WHERE id = ?",
                    (now, job_id),
                )
                self._conn.commit()
            return {"cancel": bool(row["cancel_requested"]) or row["status"] == "cancelled"}

    def finish(self, job_id: int, *, ok: bool, text: str, session_id: str = "",
               payload: Optional[dict] = None, now: Optional[float] = None) -> Optional[bool]:
        """Take a worker's result.  True = taken, False = already finished, None = unknown job.

        Sending the same result twice is harmless (the second is not taken).
        """
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_id, status, cancel_requested FROM relay_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] not in ("running", "lost"):
                return False
            if row["cancel_requested"]:
                status = "cancelled"
            else:
                status = "done" if ok else "failed"
            self._conn.execute(
                "UPDATE relay_jobs SET status = ?, finished_at = ?, result_text = ?, result_json = ?,"
                " delivered = 0 WHERE id = ?",
                (status, now, text, json.dumps(payload or {}, ensure_ascii=False), job_id),
            )
            if session_id:
                self._conn.execute(
                    "INSERT INTO relay_sessions (chat_id, session_id, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(chat_id) DO UPDATE SET session_id = excluded.session_id,"
                    " updated_at = excluded.updated_at",
                    (row["chat_id"], session_id, now),
                )
            self._conn.commit()
            return True

    # ── housekeeping ──────────────────────────────────────────────────────

    def mark_lost(self, older_than: float) -> list[int]:
        """Running jobs whose last heartbeat is before ``older_than`` become lost."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM relay_jobs WHERE status = 'running' AND heartbeat_at < ?",
                (older_than,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                self._conn.executemany(
                    "UPDATE relay_jobs SET status = 'lost', delivered = 0 WHERE id = ?",
                    [(i,) for i in ids],
                )
                self._conn.commit()
        return ids

    def undelivered(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_jobs WHERE delivered = 0 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_delivered(self, job_id: int, status: str) -> None:
        """Only if the job is still in ``status`` (a later change needs its own message)."""
        with self._lock:
            self._conn.execute(
                "UPDATE relay_jobs SET delivered = 1 WHERE id = ? AND status = ?", (job_id, status),
            )
            self._conn.commit()
