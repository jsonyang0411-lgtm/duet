"""PRM copilot storage (``PRM_COPILOT=on``), one SQLite file (``PRM_DB``, data/prm.db).

``parties``         the creators the operators registered (their private chat is their wxid)
``deals``           one collaboration per creator: stage, mode, the latest slot values,
                    the stage the AI suggests, and why a person should look (``attention``)
``action_packets``  every draft reply: what would be sent (text with placeholders,
                    attachments), why, and what became of it
``pii_tokens``      the phone and ID numbers behind the placeholders (〈手机1〉): the Mac
                    and its model only ever see the placeholder

A packet's life: ``pending`` (waits in the console) -> ``approved`` (an operator said
send) -> ``sending`` -> ``sent`` | ``failed``; or ``pending`` -> ``dropped`` (not sent) |
``superseded`` (the creator wrote again, a newer draft follows).  ``no_reply`` is a
draft that says nothing needs to be sent.  Moves are compare-and-set (``move``), so a
double click or two threads never send one packet twice.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

PENDING, APPROVED, SENDING, SENT, FAILED = "pending", "approved", "sending", "sent", "failed"
DROPPED, SUPERSEDED, NO_REPLY = "dropped", "superseded", "no_reply"
PACKET_STATES = (PENDING, APPROVED, SENDING, SENT, FAILED, DROPPED, SUPERSEDED, NO_REPLY)

# The stages of a collaboration (docs: 合作单状态机); the AI may only suggest these.
STAGES = {
    "new": "待建联", "talk": "意向沟通", "agreed": "意向确认", "hold": "搁置",
    "address": "收集收件信息", "to_ship": "待寄出", "shipped": "已寄出", "transit": "运输中",
    "delivered": "已签收", "ship_error": "物流异常", "brief": "素材已发", "draft": "初稿已交",
    "revise": "修改中", "published": "已发布", "closed": "结案",
}
MODES = ("auto", "copilot", "human", "paused")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wxid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    authorized_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'talk',
    mode TEXT NOT NULL DEFAULT 'copilot',
    slots TEXT NOT NULL DEFAULT '{}',
    stage_proposal TEXT NOT NULL DEFAULT '',
    attention TEXT NOT NULL DEFAULT '',
    last_message_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deals_party ON deals(party_id, id);
CREATE TABLE IF NOT EXISTS action_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    party_id INTEGER NOT NULL,
    job_id INTEGER,
    chat_id TEXT NOT NULL,
    tool TEXT NOT NULL DEFAULT 'message.send',
    kind TEXT NOT NULL DEFAULT 'other',
    route TEXT NOT NULL DEFAULT 'review',
    text TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    intent TEXT NOT NULL DEFAULT '',
    slots TEXT NOT NULL DEFAULT '{}',
    stage_proposal TEXT NOT NULL DEFAULT '',
    needs_attention INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    final_text TEXT,
    edited INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS packets_status ON action_packets(status, id);
CREATE INDEX IF NOT EXISTS packets_deal ON action_packets(deal_id, id);
CREATE TABLE IF NOT EXISTS pii_tokens (
    party_id INTEGER NOT NULL,
    token TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (party_id, token)
);
"""


class PartyError(ValueError):
    """A creator cannot be registered or changed (the message says why, in Chinese)."""


def _loads(text: str, empty):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return empty
    return value if isinstance(value, type(empty)) else empty


def packet_view(row: dict) -> dict:
    """A packet with its JSON columns read."""
    out = dict(row)
    out["attachments"] = _loads(row.get("attachments"), [])
    out["slots"] = _loads(row.get("slots"), {})
    out["needs_attention"] = bool(row.get("needs_attention"))
    out["edited"] = bool(row.get("edited"))
    return out


def deal_view(row: dict) -> dict:
    out = dict(row)
    out["slots"] = _loads(row.get("slots"), {})
    return out


class PrmStore:
    def __init__(self, path: str | Path = "data/prm.db"):
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

    # ── creators ──────────────────────────────────────────────────────────

    def add_party(self, wxid: str, name: str, note: str = "", now: Optional[float] = None) -> dict:
        """Register a creator (a removed one comes back) and open their deal."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT * FROM parties WHERE wxid = ?", (wxid,)).fetchone()
            if row is not None and row["active"]:
                raise PartyError(f"{wxid} 已经登记过了（{row['name']}）")
            if row is not None:
                self._conn.execute(
                    "UPDATE parties SET name = ?, note = ?, active = 1, authorized_at = ?, updated_at = ?"
                    " WHERE id = ?", (name, note, now, now, row["id"]),
                )
                party_id = int(row["id"])
            else:
                cur = self._conn.execute(
                    "INSERT INTO parties (wxid, name, note, authorized_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)", (wxid, name, note, now, now, now),
                )
                party_id = int(cur.lastrowid)
            if self._conn.execute("SELECT 1 FROM deals WHERE party_id = ?", (party_id,)).fetchone() is None:
                self._conn.execute(
                    "INSERT INTO deals (party_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (party_id, now, now),
                )
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone())

    def update_party(self, party_id: int, *, name: Optional[str] = None, note: Optional[str] = None,
                     active: Optional[bool] = None, now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE parties SET name = ?, note = ?, active = ?, updated_at = ? WHERE id = ?",
                (row["name"] if name is None else name, row["note"] if note is None else note,
                 row["active"] if active is None else int(bool(active)), now, party_id),
            )
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone())

    def party(self, party_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
        return dict(row) if row else None

    def party_by_wxid(self, wxid: str) -> Optional[dict]:
        """The registered (active) creator with this wxid."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM parties WHERE wxid = ? AND active = 1", (wxid,)
            ).fetchone()
        return dict(row) if row else None

    def parties(self, include_inactive: bool = False) -> list[dict]:
        sql = "SELECT * FROM parties" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY id"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def active_wxids(self) -> set[str]:
        with self._lock:
            return {r["wxid"] for r in self._conn.execute("SELECT wxid FROM parties WHERE active = 1")}

    # ── deals ─────────────────────────────────────────────────────────────

    def deal_of(self, party_id: int, now: Optional[float] = None) -> dict:
        """The creator's (newest) deal, made when there is none."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deals WHERE party_id = ? ORDER BY id DESC LIMIT 1", (party_id,)
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO deals (party_id, created_at, updated_at) VALUES (?, ?, ?)", (party_id, now, now),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM deals WHERE id = ?", (cur.lastrowid,)).fetchone()
        return deal_view(dict(row))

    def deal(self, deal_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        return deal_view(dict(row)) if row else None

    def deals(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM deals ORDER BY id").fetchall()
        return [deal_view(dict(r)) for r in rows]

    def update_deal(self, deal_id: int, now: Optional[float] = None, *, merge_slots: Optional[dict] = None,
                    **fields) -> None:
        """Set ``fields`` (stage, mode, stage_proposal, attention, last_message_at) and merge slot values."""
        allowed = {"stage", "mode", "stage_proposal", "attention", "last_message_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a deal field: {sorted(unknown)}")
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT slots FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None:
                return
            if merge_slots:
                slots = _loads(row["slots"], {})
                slots.update(merge_slots)
                fields["slots"] = json.dumps(slots, ensure_ascii=False)
            fields["updated_at"] = now
            names = sorted(fields)
            self._conn.execute(
                f"UPDATE deals SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                (*[fields[n] for n in names], deal_id),
            )
            self._conn.commit()

    # ── drafts ────────────────────────────────────────────────────────────

    def add_packet(self, *, deal_id: int, party_id: int, job_id: Optional[int], chat_id: str, kind: str,
                   text: str, attachments: list, intent: str, slots: dict, stage_proposal: str,
                   needs_attention: bool, reason: str, status: str = PENDING, route: str = "review",
                   now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO action_packets (deal_id, party_id, job_id, chat_id, kind, route, text, attachments,"
                " intent, slots, stage_proposal, needs_attention, reason, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (deal_id, party_id, job_id, chat_id, kind, route, text,
                 json.dumps(attachments, ensure_ascii=False), intent, json.dumps(slots, ensure_ascii=False),
                 stage_proposal, int(bool(needs_attention)), reason, status, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def packet(self, packet_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM action_packets WHERE id = ?", (packet_id,)).fetchone()
        return packet_view(dict(row)) if row else None

    def packets(self, statuses: Optional[tuple] = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM action_packets", []
        if statuses:
            sql += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            args += list(statuses)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, int(limit))).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def packet_for_job(self, job_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM action_packets WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
        return packet_view(dict(row)) if row else None

    def move(self, packet_id: int, from_states: tuple, to_state: str, **fields) -> bool:
        """Compare-and-set: the packet goes to ``to_state`` only if it is in one of ``from_states``."""
        allowed = {"final_text", "edited", "outcome", "error", "decided_at", "sent_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a packet field: {sorted(unknown)}")
        names = sorted(fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE action_packets SET status = ?{''.join(f', {n} = ?' for n in names)}"
                f" WHERE id = ? AND status IN ({', '.join('?' * len(from_states))})",
                (to_state, *[fields[n] for n in names], packet_id, *from_states),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def supersede(self, deal_id: int, now: Optional[float] = None, keep: Optional[int] = None) -> int:
        """Pending drafts of this deal (but ``keep``) are out of date: ``superseded``; returns how many."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "UPDATE action_packets SET status = ?, decided_at = ? WHERE deal_id = ? AND status = ?"
                " AND id != ?", (SUPERSEDED, now, deal_id, PENDING, -1 if keep is None else keep),
            )
            self._conn.commit()
            return cur.rowcount

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM action_packets GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    # ── placeholders for phone and ID numbers ─────────────────────────────

    def token_for(self, party_id: int, kind: str, label: str, value: str, now: Optional[float] = None) -> str:
        """The placeholder of ``value`` for this creator (the same value keeps its placeholder)."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT token FROM pii_tokens WHERE party_id = ? AND kind = ? AND value = ?",
                (party_id, kind, value),
            ).fetchone()
            if row is not None:
                return str(row["token"])
            count = self._conn.execute(
                "SELECT COUNT(*) FROM pii_tokens WHERE party_id = ? AND kind = ?", (party_id, kind),
            ).fetchone()[0]
            token = f"〈{label}{count + 1}〉"
            self._conn.execute(
                "INSERT INTO pii_tokens (party_id, token, kind, value, created_at) VALUES (?, ?, ?, ?, ?)",
                (party_id, token, kind, value, now),
            )
            self._conn.commit()
            return token

    def tokens(self, party_id: int) -> dict[str, tuple[str, str]]:
        """``{placeholder: (kind, value)}`` of this creator."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, kind, value FROM pii_tokens WHERE party_id = ?", (party_id,)
            ).fetchall()
        return {r["token"]: (r["kind"], r["value"]) for r in rows}
