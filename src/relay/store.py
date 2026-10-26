"""The relay's job queue (``CLAUDE_RELAY=on``), one SQLite file.

A job is one request from WeChat for Claude Code on the Mac.  Its life:
``pending`` (queued) -> ``running`` (a worker took it) -> ``done`` / ``failed``
(the worker sent the result) or ``cancelled``.  A running job whose worker
stops sending heartbeats becomes ``lost``; a result that arrives later still
finishes it.  ``delivered`` says whether WeChat has been told about the last
state change, so a failed send is retried and a restart loses nothing.
While a job runs, every heartbeat brings what Claude is doing (``progress``);
``progress_sent_at`` / ``progress_sent`` say when WeChat last heard about it.
``relay_files`` are the pictures and files sent with a job (copies the bot
keeps; the worker downloads them).  ``relay_asks`` are Claude's requests for an
approval: ``pending`` until the person answers in WeChat (``allow`` / ``deny``),
the time runs out (``expired``) or the job ends (``cancelled``).
``relay_followups`` are what the person sends while their job runs: the worker
reads them past a cursor (``input_upto``) and hands them to the running Claude;
``input_closed`` means the job takes no more (later messages are new jobs).

A job has a ``kind``: ``admin`` (the allowed people's ``cc`` tasks, the default)
or ``prm`` (a registered creator's messages for a draft, src/prm; nothing goes
back to WeChat on its own).  A worker only gets the kinds it says it can do
(``claim_next``), so an older worker never runs a creator's text as a task.
``meta`` is what the worker needs besides the text (JSON; for ``prm`` the
creator's party id).  ``ready_at`` holds a job back a little: a creator's
messages that come in quick succession are added to it (``append_pending``)
instead of becoming jobs of their own.  ``relay_outfiles`` are the files a
``prm`` job's worker sends back (pictures from the material folder).

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
ADMIN = "admin"             # an allowed person's cc task
PRM = "prm"                 # a registered creator's messages, for a draft (src/prm)
KINDS = (ADMIN, PRM)

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
    delivered INTEGER NOT NULL DEFAULT 1,
    progress TEXT NOT NULL DEFAULT '{}',
    progress_sent_at REAL,
    progress_sent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS relay_jobs_status ON relay_jobs(status, id);
CREATE TABLE IF NOT EXISTS relay_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS relay_files_job ON relay_files(job_id, id);
CREATE TABLE IF NOT EXISTS relay_followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS relay_followups_job ON relay_followups(job_id, id);
CREATE TABLE IF NOT EXISTS relay_asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    answered_at REAL,
    sent INTEGER NOT NULL DEFAULT 0,
    noted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS relay_asks_chat ON relay_asks(chat_id, status, id);
CREATE TABLE IF NOT EXISTS relay_sessions (
    chat_id TEXT PRIMARY KEY,
    session_id TEXT,
    updated_at REAL NOT NULL,
    project TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS relay_outfiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS relay_outfiles_job ON relay_outfiles(job_id, id);
"""


# Columns added after the first release: (table, column, type).
_ADDED_COLUMNS = (
    ("relay_sessions", "project", "TEXT NOT NULL DEFAULT ''"),
    ("relay_jobs", "progress", "TEXT NOT NULL DEFAULT '{}'"),
    ("relay_jobs", "progress_sent_at", "REAL"),
    ("relay_jobs", "progress_sent", "TEXT NOT NULL DEFAULT ''"),
    ("relay_jobs", "input_closed", "INTEGER NOT NULL DEFAULT 0"),
    ("relay_jobs", "input_upto", "INTEGER NOT NULL DEFAULT 0"),
    ("relay_jobs", "kind", "TEXT NOT NULL DEFAULT 'admin'"),
    ("relay_jobs", "meta", "TEXT NOT NULL DEFAULT '{}'"),
    ("relay_jobs", "ready_at", "REAL"),
)


class RelayStore:
    def __init__(self, path: str | Path = "data/claude_relay.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for table, column, kind in _ADDED_COLUMNS:  # a database from before these columns
                columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── from WeChat ───────────────────────────────────────────────────────

    def add(self, chat_id: str, sender_id: str, text: str, project: str = "",
            now: Optional[float] = None, files: tuple = (), *, kind: str = ADMIN,
            meta: Optional[dict] = None, ready_at: Optional[float] = None,
            input_closed: bool = False) -> int:
        """Queue a job; ``files`` (``(name, path, size)``) go with it in the same transaction,
        so a worker never takes the job without them.  ``ready_at``: not handed out before.
        ``input_closed``: no later message joins it (a job the worker does without the model)."""
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO relay_jobs (chat_id, sender_id, text, project, status, created_at, kind, meta,"
                " ready_at, input_closed) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (chat_id, sender_id, text, project, time.time() if now is None else now, kind,
                 json.dumps(meta or {}, ensure_ascii=False), ready_at, int(bool(input_closed))),
            )
            job_id = int(cur.lastrowid)
            self._conn.executemany(
                "INSERT INTO relay_files (job_id, name, path, size) VALUES (?, ?, ?, ?)",
                [(job_id, name, path, int(size)) for name, path, size in files],
            )
            self._conn.commit()
            return job_id

    def append_pending(self, chat_id: str, kind: str, text: str, files: tuple = (),
                       ready_at: Optional[float] = None) -> Optional[int]:
        """Add ``text`` (and ``files``) to this chat's newest job of ``kind`` that no worker
        has taken yet, and hold it until ``ready_at``; None when there is no such job.

        One step under the lock: the job is either still queued and gets the text, or a
        worker took it first and the caller makes a follow-up or a new job instead.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id, text FROM relay_jobs WHERE chat_id = ? AND kind = ? AND status = 'pending'"
                " AND input_closed = 0 ORDER BY id DESC LIMIT 1", (chat_id, kind),
            ).fetchone()
            if row is None:
                return None
            joined = f"{row['text']}\n{text}" if row["text"] and text else (row["text"] or text)
            self._conn.execute("UPDATE relay_jobs SET text = ?, ready_at = ? WHERE id = ?",
                               (joined, ready_at, row["id"]))
            self._conn.executemany(
                "INSERT INTO relay_files (job_id, name, path, size) VALUES (?, ?, ?, ?)",
                [(row["id"], name, path, int(size)) for name, path, size in files],
            )
            self._conn.commit()
            return int(row["id"])

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

    def files(self, job_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_files WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def file(self, job_id: int, file_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relay_files WHERE job_id = ? AND id = ?", (job_id, file_id)
            ).fetchone()
        return dict(row) if row else None

    # ── files a prm job's worker sends back ───────────────────────────────

    def add_outfile(self, job_id: int, name: str, path: str, size: int, sha256: str,
                    limit: int) -> Optional[int]:
        """Record a file the worker sent for a running ``prm`` job (None: not such a job,
        or it has ``limit`` files already)."""
        with self._lock:
            row = self._conn.execute("SELECT kind, status FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["kind"] != PRM or row["status"] not in ("running", "lost"):
                return None
            count = self._conn.execute("SELECT COUNT(*) FROM relay_outfiles WHERE job_id = ?",
                                       (job_id,)).fetchone()[0]
            if count >= limit:
                return None
            cur = self._conn.execute(
                "INSERT INTO relay_outfiles (job_id, name, path, size, sha256) VALUES (?, ?, ?, ?, ?)",
                (job_id, name, path, int(size), sha256),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def outfiles(self, job_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_outfiles WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def rewrite_chat(self, chat_id: str, kind: str, rewrite) -> int:
        """Rewrite what the jobs of ``kind`` in this chat hold (their text, results, follow-ups):
        the PRM clean-up of a creator's details (8.2).  Returns how many rows changed."""
        changed = 0
        with self._lock:
            jobs = self._conn.execute(
                "SELECT id, text, result_text, result_json FROM relay_jobs WHERE chat_id = ? AND kind = ?",
                (chat_id, kind),
            ).fetchall()
            for job in jobs:
                new = (rewrite(job["text"] or ""), rewrite(job["result_text"] or ""), rewrite(job["result_json"] or "{}"))
                if new != (job["text"] or "", job["result_text"] or "", job["result_json"] or "{}"):
                    self._conn.execute("UPDATE relay_jobs SET text = ?, result_text = ?, result_json = ? WHERE id = ?",
                                       (*new, job["id"]))
                    changed += 1
                for row in self._conn.execute("SELECT id, text FROM relay_followups WHERE job_id = ?",
                                              (job["id"],)).fetchall():
                    text = rewrite(row["text"] or "")
                    if text != row["text"]:
                        self._conn.execute("UPDATE relay_followups SET text = ? WHERE id = ?", (text, row["id"]))
                        changed += 1
            self._conn.commit()
        return changed

    # ── follow-ups: what the person sends while their job runs ─────────────

    def add_followup(self, chat_id: str, text: str, now: float,
                     kind: str = ADMIN) -> Optional[tuple[int, int]]:
        """``(job_id, followup_id)`` when this chat has a running job of ``kind`` that still takes input."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM relay_jobs WHERE chat_id = ? AND kind = ? AND status = 'running'"
                " AND input_closed = 0 AND cancel_requested = 0 ORDER BY id DESC LIMIT 1", (chat_id, kind),
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "INSERT INTO relay_followups (job_id, text, created_at) VALUES (?, ?, ?)",
                (row["id"], text, now),
            )
            self._conn.commit()
            return int(row["id"]), int(cur.lastrowid)

    def followups(self, job_id: int, after: int) -> Optional[list[dict]]:
        """Follow-ups past ``after`` (the worker has handed on everything up to it); None: no job."""
        with self._lock:
            if self._conn.execute("SELECT 1 FROM relay_jobs WHERE id = ?", (job_id,)).fetchone() is None:
                return None
            self._conn.execute("UPDATE relay_jobs SET input_upto = MAX(input_upto, ?) WHERE id = ?",
                               (int(after), job_id))
            rows = self._conn.execute(
                "SELECT id, text FROM relay_followups WHERE job_id = ? AND id > ? ORDER BY id",
                (job_id, int(after)),
            ).fetchall()
            self._conn.commit()
        return [dict(r) for r in rows]

    def close_input(self, job_id: int, upto: int) -> Optional[list[dict]]:
        """A turn ended and the worker has handed on everything up to ``upto``: the follow-ups
        past it, or (none left, or the job is being cancelled) ``[]`` and the job takes no
        more.  One step under the lock, so a message is either returned here or a new job."""
        with self._lock:
            job = self._conn.execute("SELECT cancel_requested FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return None
            self._conn.execute("UPDATE relay_jobs SET input_upto = MAX(input_upto, ?) WHERE id = ?",
                               (int(upto), job_id))
            rows = [] if job["cancel_requested"] else self._conn.execute(
                "SELECT id, text FROM relay_followups WHERE job_id = ? AND id > ? ORDER BY id",
                (job_id, int(upto)),
            ).fetchall()
            if not rows:
                self._conn.execute("UPDATE relay_jobs SET input_closed = 1 WHERE id = ?", (job_id,))
            self._conn.commit()
        return [dict(r) for r in rows]

    def add_result_note(self, job_id: int, **fields) -> None:
        """Merge ``fields`` into the job's ``result_json`` (what the result message mentions)."""
        with self._lock:
            row = self._conn.execute("SELECT result_json FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            try:
                data = json.loads(row["result_json"] or "{}")
            except ValueError:
                data = {}
            data.update(fields)
            self._conn.execute("UPDATE relay_jobs SET result_json = ? WHERE id = ?",
                               (json.dumps(data, ensure_ascii=False), job_id))
            self._conn.commit()

    def take_leftovers(self, job_id: int) -> list[dict]:
        """Follow-ups the worker never handed on (the job ended first); they count as taken."""
        with self._lock:
            row = self._conn.execute("SELECT input_upto FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return []
            rows = self._conn.execute(
                "SELECT id, text FROM relay_followups WHERE job_id = ? AND id > ? ORDER BY id",
                (job_id, row["input_upto"]),
            ).fetchall()
            self._conn.execute("UPDATE relay_jobs SET input_closed = 1, input_upto = MAX(input_upto, ?)"
                               " WHERE id = ?", (rows[-1]["id"] if rows else 0, job_id))
            self._conn.commit()
        return [dict(r) for r in rows]

    # ── approvals ─────────────────────────────────────────────────────────

    def add_ask(self, job_id: int, tool: str, detail: str, now: float, expires_at: float) -> Optional[int]:
        """A request of a running job (None: no such job, or it is not running)."""
        with self._lock:
            row = self._conn.execute("SELECT chat_id, status FROM relay_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] not in ("running", "lost"):
                return None
            cur = self._conn.execute(
                "INSERT INTO relay_asks (job_id, chat_id, tool, detail, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, row["chat_id"], tool, detail, now, expires_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_ask(self, ask_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM relay_asks WHERE id = ?", (ask_id,)).fetchone()
        return dict(row) if row else None

    def pending_ask(self, chat_id: str, job_id: Optional[int] = None) -> Optional[dict]:
        """The newest request of this chat that waits for an answer (of job ``job_id`` when given)."""
        sql = "SELECT * FROM relay_asks WHERE chat_id = ? AND status = 'pending'"
        args: tuple = (chat_id,)
        if job_id is not None:
            sql += " AND job_id = ?"
            args += (job_id,)
        with self._lock:
            row = self._conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None

    def settle_ask(self, ask_id: int, status: str, now: float) -> bool:
        """Answer / expire / cancel a pending request; False when it was settled already."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE relay_asks SET status = ?, answered_at = ? WHERE id = ? AND status = 'pending'",
                (status, now, ask_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def unsent_asks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_asks WHERE status = 'pending' AND sent = 0 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_ask_sent(self, ask_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE relay_asks SET sent = 1 WHERE id = ?", (ask_id,))
            self._conn.commit()

    def expire_asks(self, now: float) -> int:
        """Pending requests whose time is up become ``expired``; returns how many."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE relay_asks SET status = 'expired', answered_at = ? WHERE status = 'pending' AND expires_at < ?",
                (now, now),
            )
            self._conn.commit()
            return cur.rowcount

    def expired_unnoted(self) -> list[dict]:
        """Expired requests WeChat was asked about but not yet told that they ran out."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_asks WHERE status = 'expired' AND sent = 1 AND noted = 0 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_ask_noted(self, ask_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE relay_asks SET noted = 1 WHERE id = ?", (ask_id,))
            self._conn.commit()

    def cancel_asks(self, job_id: int, now: float) -> int:
        """The job ended or was cancelled: its open requests are ``cancelled``."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE relay_asks SET status = 'cancelled', answered_at = ? WHERE job_id = ? AND status = 'pending'",
                (now, job_id),
            )
            self._conn.commit()
            return cur.rowcount

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

    def claim_next(self, now: Optional[float] = None, kinds: tuple = (ADMIN,)) -> Optional[dict]:
        """Hand the oldest queued job of one of ``kinds`` that is ready to a worker.

        The Claude session is looked up now, not when the job was queued: the
        worker runs one job at a time, so a follow-up queued behind another job
        of the same chat continues the session that job just left.  Only ``admin``
        jobs have sessions; a ``prm`` job always starts afresh.
        """
        now = time.time() if now is None else now
        kinds = tuple(k for k in kinds if k in KINDS)
        if not kinds:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relay_jobs WHERE status = 'pending' AND kind IN"
                f" ({', '.join('?' * len(kinds))}) AND (ready_at IS NULL OR ready_at <= ?)"
                " ORDER BY id LIMIT 1", (*kinds, now),
            ).fetchone()
            if row is None:
                return None
            session = self._conn.execute(
                "SELECT session_id FROM relay_sessions WHERE chat_id = ?", (row["chat_id"],)
            ).fetchone() if row["kind"] == ADMIN else None
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

    def heartbeat(self, job_id: int, now: Optional[float] = None,
                  progress: Optional[dict] = None) -> Optional[dict]:
        """Note that the worker is alive; returns ``{"cancel": bool}``, or None for an unknown job.

        A job marked lost while the worker was unreachable is running again.
        ``progress`` (``{"steps", "doing", "plan"}``) replaces the last one when given.
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
                if progress:
                    self._conn.execute(
                        "UPDATE relay_jobs SET progress = ? WHERE id = ?",
                        (json.dumps(progress, ensure_ascii=False), job_id),
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
                "SELECT chat_id, status, cancel_requested, kind FROM relay_jobs WHERE id = ?", (job_id,)
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
            if session_id and row["kind"] == ADMIN:           # a prm job never carries on a session
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

    def running(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM relay_jobs WHERE status = 'running' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def note_progress_sent(self, job_id: int, line: str, now: Optional[float] = None) -> None:
        """WeChat was told that the job started (``line`` empty) or how far it is."""
        with self._lock:
            self._conn.execute(
                "UPDATE relay_jobs SET progress_sent_at = ?, progress_sent = ? WHERE id = ?",
                (time.time() if now is None else now, line, job_id),
            )
            self._conn.commit()

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
