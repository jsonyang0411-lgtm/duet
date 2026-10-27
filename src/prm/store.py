"""PRM copilot storage (``PRM_COPILOT=on``), one SQLite file (``PRM_DB``, data/prm.db).

``parties``         the creators the operators registered (their private chat is their wxid)
``deals``           one collaboration per creator: stage, mode (``copilot`` / ``human`` until
                    ``mode_until`` / ``paused``), the latest slot values, the stage the AI
                    suggests, why a person should look (``attention``), when it was
                    delivered and when its personal data was cleared (8.2)
``action_packets``  every draft reply: what would be sent (text with placeholders,
                    attachments), why, and what became of it
``pii_tokens``      the phone and ID numbers behind the placeholders (〈手机1〉): the Mac
                    and its model only ever see the placeholder; ``masked`` stays when the
                    number itself is cleared
``shipments``       the samples sent: carrier, waybill, logistics status
``send_gates``      per message kind: does a draft wait for a person (``review``) or go out
                    at once (``auto``), and from when its count starts (8.1)
``deal_events``     what happened to a deal, in order: stage and mode changes, shipments,
                    the operators' own WeChat replies, a creator's messages while a person
                    has the chat, holding replies, numbers shown in full, clean-ups.  The
                    audit trail, and the backlog the next draft job is told about.

A packet's life: ``pending`` (waits in the console) -> ``approved`` (an operator said
send, or the kind's gate is ``auto``) -> ``sending`` -> ``sent`` | ``failed``; or
``pending`` -> ``dropped`` (not sent) | ``superseded`` (the creator wrote again, a newer
draft follows).  ``no_reply`` is a draft that says nothing needs to be sent.  Moves are
compare-and-set (``move``), so a double click or two threads never send one packet twice.

A packet with a platform action (``tool`` is the action's name, ``platform`` the JSON:
the action, the Mac's preview, the execution; src/prm/platforms.py) is always reviewed:
``pending`` -> ``executing`` (an operator said 批准并执行; the Mac runs the platform CLI
with --execute, once) -> ``approved`` (read back VERIFIED_SUCCESS: the reply, with the
link filled in, goes out) | ``failed`` (anything less: nothing is sent to the creator).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

PENDING, APPROVED, SENDING, SENT, FAILED = "pending", "approved", "sending", "sent", "failed"
DROPPED, SUPERSEDED, NO_REPLY = "dropped", "superseded", "no_reply"
EXECUTING = "executing"      # a platform action an operator approved, running on the Mac
PACKET_STATES = (PENDING, APPROVED, SENDING, SENT, FAILED, DROPPED, SUPERSEDED, NO_REPLY, EXECUTING)
MESSAGE_TOOL = "message.send"   # ``tool`` of a plain reply (a platform action's name otherwise)

# The stages of a collaboration (docs: 合作单状态机); the AI may only suggest these.
STAGES = {
    "new": "待建联", "talk": "意向沟通", "agreed": "意向确认", "hold": "搁置",
    "address": "收集收件信息", "to_ship": "待寄出", "shipped": "已寄出", "transit": "运输中",
    "delivered": "已签收", "ship_error": "物流异常", "brief": "素材已发", "draft": "初稿已交",
    "revise": "修改中", "published": "已发布", "closed": "结案",
}
COPILOT, HUMAN, PAUSED = "copilot", "human", "paused"
MODES = {COPILOT: "副驾驶", HUMAN: "人工接管", PAUSED: "暂停"}
# Logistics status of a shipment -> the stage it puts the deal in.
SHIPMENT_STATES = {"shipped": "已寄出", "transit": "运输中", "delivered": "已签收", "ship_error": "物流异常"}

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
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    carrier TEXT NOT NULL,
    waybill TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'shipped',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS shipments_deal ON shipments(deal_id, id);
CREATE TABLE IF NOT EXISTS send_gates (
    kind TEXT PRIMARY KEY,
    route TEXT NOT NULL DEFAULT 'review',
    window INTEGER NOT NULL,
    min_unchanged INTEGER NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    reset_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    party_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    forwarded INTEGER NOT NULL DEFAULT 1,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deal_events_deal ON deal_events(deal_id, id);
"""

# Columns added after the first version: (table, column, type).
_ADDED_COLUMNS = (
    ("deals", "mode_until", "REAL"),
    ("deals", "delivered_at", "REAL"),
    ("deals", "purged_at", "REAL"),
    ("deals", "holding_round", "REAL"),
    ("action_packets", "meta", "TEXT NOT NULL DEFAULT '{}'"),
    ("action_packets", "wrong", "INTEGER NOT NULL DEFAULT 0"),
    ("pii_tokens", "masked", "TEXT NOT NULL DEFAULT ''"),
    ("pii_tokens", "purged_at", "REAL"),
    ("action_packets", "platform", "TEXT NOT NULL DEFAULT '{}'"),
)

# What the creators wrote, or our side's own words, waiting for the next draft job.
BACKLOG_KINDS = ("creator_message", "operator_message")


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
    out["meta"] = _loads(row.get("meta"), {})
    out["platform"] = _loads(row.get("platform"), {})
    out["needs_attention"] = bool(row.get("needs_attention"))
    out["edited"] = bool(row.get("edited"))
    out["wrong"] = bool(row.get("wrong"))
    return out


def deal_view(row: dict) -> dict:
    out = dict(row)
    out["slots"] = _loads(row.get("slots"), {})
    return out


def event_view(row: dict) -> dict:
    out = dict(row)
    out["detail"] = _loads(row.get("detail"), {})
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
            for table, column, kind in _ADDED_COLUMNS:   # a database from before these columns
                columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            self._conn.commit()

    def seed_gates(self, defaults: dict) -> None:
        """``{kind: (window, min_unchanged, locked)}``: the gates a database does not have yet."""
        with self._lock:
            for kind, (window, minimum, locked) in defaults.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO send_gates (kind, window, min_unchanged, locked) VALUES (?, ?, ?, ?)",
                    (kind, int(window), int(minimum), int(bool(locked))),
                )
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
                    replace_slots: Optional[dict] = None, **fields) -> None:
        """Set ``fields`` and merge (or replace) the slot values."""
        allowed = {"stage", "mode", "mode_until", "stage_proposal", "attention", "last_message_at",
                   "delivered_at", "purged_at", "holding_round"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a deal field: {sorted(unknown)}")
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT slots FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None:
                return
            if replace_slots is not None:
                fields["slots"] = json.dumps(replace_slots, ensure_ascii=False)
            elif merge_slots:
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

    def deals_to_purge(self, delivered_before: float) -> list[dict]:
        """Deals delivered before that time whose personal data is still there (8.2)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM deals WHERE delivered_at IS NOT NULL AND delivered_at <= ? AND purged_at IS NULL",
                (delivered_before,),
            ).fetchall()
        return [deal_view(dict(r)) for r in rows]

    # ── drafts ────────────────────────────────────────────────────────────

    def add_packet(self, *, deal_id: int, party_id: int, job_id: Optional[int], chat_id: str, kind: str,
                   text: str, attachments: list, intent: str, slots: dict, stage_proposal: str,
                   needs_attention: bool, reason: str, status: str = PENDING, route: str = "review",
                   meta: Optional[dict] = None, decided_at: Optional[float] = None,
                   tool: str = MESSAGE_TOOL, platform: Optional[dict] = None,
                   now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO action_packets (deal_id, party_id, job_id, chat_id, tool, kind, route, text, attachments,"
                " intent, slots, stage_proposal, needs_attention, reason, status, meta, platform, decided_at,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (deal_id, party_id, job_id, chat_id, tool, kind, route, text,
                 json.dumps(attachments, ensure_ascii=False), intent, json.dumps(slots, ensure_ascii=False),
                 stage_proposal, int(bool(needs_attention)), reason, status,
                 json.dumps(meta or {}, ensure_ascii=False), json.dumps(platform or {}, ensure_ascii=False),
                 decided_at, now),
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

    def outcomes(self, deal_id: int, limit: int = 6) -> list[dict]:
        """A deal's latest drafts a person (or a gate) decided on, newest first: sent (as drafted
        or edited), not sent (dropped), failed.  What the creator really got from us."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE deal_id = ? AND status IN (?, ?, ?) AND decided_at IS NOT NULL"
                " ORDER BY COALESCE(sent_at, decided_at) DESC, id DESC LIMIT ?",
                (deal_id, SENT, DROPPED, FAILED, int(limit)),
            ).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def platform_failures(self, limit: int = 50) -> list[dict]:
        """Packets with a platform action that ended ``failed`` (the platform write was not
        verified, or the reply did not go out), newest first: the console marks them red
        until an operator says they are dealt with (``platform.acked_at``)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE tool != ? AND status = ? ORDER BY id DESC LIMIT ?",
                (MESSAGE_TOOL, FAILED, int(limit)),
            ).fetchall()
        return [p for p in (packet_view(dict(r)) for r in rows) if not p["platform"].get("acked_at")]

    def packet_for_job(self, job_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM action_packets WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
        return packet_view(dict(row)) if row else None

    def sent_since(self, chat_id: str, since: float) -> list[dict]:
        """Packets sent (or being sent) to this chat after ``since`` (the bot's own lines)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE chat_id = ? AND status IN (?, ?) AND sent_at >= ?"
                " ORDER BY id DESC", (chat_id, SENT, SENDING, since),
            ).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def reviewed(self, kind: str, since: float, limit: int) -> list[dict]:
        """The newest drafts of ``kind`` a person decided on after ``since`` (8.1's window).
        Plain replies only: a platform action is always reviewed and earns no kind ``auto``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_packets WHERE kind = ? AND route = 'review' AND tool = ? AND status IN (?, ?, ?)"
                " AND decided_at IS NOT NULL AND decided_at > ? ORDER BY decided_at DESC, id DESC LIMIT ?",
                (kind, MESSAGE_TOOL, SENT, FAILED, DROPPED, since, int(limit)),
            ).fetchall()
        return [packet_view(dict(r)) for r in rows]

    def move(self, packet_id: int, from_states: tuple, to_state: str, **fields) -> bool:
        """Compare-and-set: the packet goes to ``to_state`` only if it is in one of ``from_states``.
        ``platform`` (a dict) replaces the packet's platform JSON in the same step."""
        allowed = {"final_text", "edited", "outcome", "error", "decided_at", "sent_at", "platform"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a packet field: {sorted(unknown)}")
        if "platform" in fields:
            fields["platform"] = json.dumps(fields["platform"] or {}, ensure_ascii=False)
        names = sorted(fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE action_packets SET status = ?{''.join(f', {n} = ?' for n in names)}"
                f" WHERE id = ? AND status IN ({', '.join('?' * len(from_states))})",
                (to_state, *[fields[n] for n in names], packet_id, *from_states),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def set_platform(self, packet_id: int, platform: dict, statuses: tuple, *,
                     expect: Optional[Callable[[dict], bool]] = None) -> bool:
        """Replace the platform JSON of a packet that is in one of ``statuses`` (and, with
        ``expect``, whose platform JSON now passes it): one step under the lock."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT platform FROM action_packets WHERE id = ? AND status IN ({', '.join('?' * len(statuses))})",
                (packet_id, *statuses),
            ).fetchone()
            if row is None or (expect is not None and not expect(_loads(row["platform"], {}))):
                return False
            self._conn.execute("UPDATE action_packets SET platform = ? WHERE id = ?",
                               (json.dumps(platform or {}, ensure_ascii=False), packet_id))
            self._conn.commit()
            return True

    def set_outcome(self, packet_id: int, outcome: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE action_packets SET outcome = ? WHERE id = ?", (outcome, packet_id))
            self._conn.commit()

    def confirm_sent(self, packet_id: int) -> bool:
        """Its line was read back from WeChat: ``sent_confirmed`` while it is going out or sent
        unconfirmed, never over a failure (True: newly confirmed)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE action_packets SET outcome = 'sent_confirmed' WHERE id = ?"
                " AND COALESCE(outcome, '') != 'sent_confirmed'"
                " AND (status = ? OR (status = ? AND COALESCE(outcome, 'sent_unconfirmed') = 'sent_unconfirmed'))",
                (packet_id, SENDING, SENT),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_sent(self, packet_id: int, now: float) -> bool:
        """Sending → sent: ``sent_unconfirmed``, unless its line was read back already."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE action_packets SET status = ?, sent_at = ?, outcome = CASE WHEN outcome = 'sent_confirmed'"
                " THEN outcome ELSE 'sent_unconfirmed' END WHERE id = ? AND status = ?",
                (SENT, now, packet_id, SENDING),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_wrong(self, packet_id: int) -> bool:
        """An operator says this sent packet should not have gone out (True: newly marked)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE action_packets SET wrong = 1 WHERE id = ? AND status = ? AND wrong = 0", (packet_id, SENT),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def supersede(self, deal_id: int, now: Optional[float] = None, keep: Optional[int] = None,
                  system_kind: Optional[str] = None) -> int:
        """Pending drafts of this deal (but ``keep``) are out of date: ``superseded``; returns how many.
        The Mac's drafts (they answer the creator), or with ``system_kind`` the system's own drafts of
        that kind (a shipping notice is not out of date because the creator wrote)."""
        now = time.time() if now is None else now
        which = "job_id IS NOT NULL" if system_kind is None else "job_id IS NULL AND kind = ?"
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE action_packets SET status = ?, decided_at = ? WHERE deal_id = ? AND status = ?"
                f" AND id != ? AND {which}",
                (SUPERSEDED, now, deal_id, PENDING, -1 if keep is None else keep,
                 *(() if system_kind is None else (system_kind,))),
            )
            self._conn.commit()
            return cur.rowcount

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM action_packets GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def rewrite_packets(self, deal_id: int, rewrite: Callable[[str], str]) -> int:
        """Rewrite the text of every packet of a deal, its platform action too (8.2 clean-up);
        returns how many changed."""
        changed = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, final_text, slots, intent, reason, platform FROM action_packets WHERE deal_id = ?",
                (deal_id,),
            ).fetchall()
            for row in rows:
                old = (row["text"] or "", row["final_text"], row["slots"] or "{}", row["intent"] or "",
                       row["reason"] or "", row["platform"] or "{}")
                new = tuple(None if value is None else rewrite(value) for value in old)
                if new != old:
                    self._conn.execute(
                        "UPDATE action_packets SET text = ?, final_text = ?, slots = ?, intent = ?, reason = ?,"
                        " platform = ? WHERE id = ?", (*new, row["id"]),
                    )
                    changed += 1
            self._conn.commit()
        return changed

    # ── placeholders for phone and ID numbers ─────────────────────────────

    def token_for(self, party_id: int, kind: str, label: str, value: str, now: Optional[float] = None) -> str:
        """The placeholder of ``value`` for this creator (the same value keeps its placeholder)."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT token FROM pii_tokens WHERE party_id = ? AND kind = ? AND value = ? AND purged_at IS NULL",
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
        """``{placeholder: (kind, value)}`` of this creator, the numbers that are still kept."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, kind, value FROM pii_tokens WHERE party_id = ? AND purged_at IS NULL", (party_id,)
            ).fetchall()
        return {r["token"]: (r["kind"], r["value"]) for r in rows}

    def token_rows(self, party_id: int) -> list[dict]:
        """Every placeholder of this creator, cleared ones too (``masked`` is what the console shows)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, kind, value, masked, purged_at FROM pii_tokens WHERE party_id = ? ORDER BY created_at",
                (party_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_tokens(self, party_id: int, masked: Callable[[str, str], str], now: Optional[float] = None) -> int:
        """Clear the numbers of this creator, keep their masked form; returns how many."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, kind, value FROM pii_tokens WHERE party_id = ? AND purged_at IS NULL", (party_id,)
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE pii_tokens SET masked = ?, value = '', purged_at = ? WHERE party_id = ? AND token = ?",
                    (masked(row["kind"], row["value"]), now, party_id, row["token"]),
                )
            self._conn.commit()
        return len(rows)

    # ── shipments ─────────────────────────────────────────────────────────

    def add_shipment(self, deal_id: int, carrier: str, waybill: str, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO shipments (deal_id, carrier, waybill, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (deal_id, carrier, waybill, now, now),
            )
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM shipments WHERE id = ?", (cur.lastrowid,)).fetchone())

    def latest_shipment(self, deal_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shipments WHERE deal_id = ? ORDER BY id DESC LIMIT 1", (deal_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_shipment_status(self, shipment_id: int, status: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute("UPDATE shipments SET status = ?, updated_at = ? WHERE id = ?",
                               (status, now, shipment_id))
            self._conn.commit()

    # ── send gates (8.1) ──────────────────────────────────────────────────

    def gates(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM send_gates ORDER BY kind").fetchall()
        return {r["kind"]: dict(r) for r in rows}

    def gate(self, kind: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM send_gates WHERE kind = ?", (kind,)).fetchone()
        return dict(row) if row else None

    def set_gate(self, kind: str, route: str, now: Optional[float] = None, reset: bool = False) -> None:
        now = time.time() if now is None else now
        with self._lock:
            if reset:
                self._conn.execute("UPDATE send_gates SET route = ?, reset_at = ?, updated_at = ? WHERE kind = ?",
                                   (route, now, now, kind))
            else:
                self._conn.execute("UPDATE send_gates SET route = ?, updated_at = ? WHERE kind = ?", (route, now, kind))
            self._conn.commit()

    # ── events (the audit trail and the backlog for the next draft) ───────

    def add_event(self, deal_id: int, party_id: int, kind: str, detail: Optional[dict] = None, *,
                  actor: str = "", forwarded: bool = True, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO deal_events (deal_id, party_id, kind, detail, actor, forwarded, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (deal_id, party_id, kind, json.dumps(detail or {}, ensure_ascii=False), actor, int(bool(forwarded)), now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def events(self, deal_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM deal_events", []
        if deal_id is not None:
            sql += " WHERE deal_id = ?"
            args.append(deal_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
        return [event_view(dict(r)) for r in rows]

    def take_backlog(self, deal_id: int) -> list[dict]:
        """The creator's and the operators' messages the next draft job has not heard of, oldest
        first; they count as told."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM deal_events WHERE deal_id = ? AND forwarded = 0 AND kind IN"
                f" ({', '.join('?' * len(BACKLOG_KINDS))}) ORDER BY id", (deal_id, *BACKLOG_KINDS),
            ).fetchall()
            if rows:
                self._conn.executemany("UPDATE deal_events SET forwarded = 1 WHERE id = ?", [(r["id"],) for r in rows])
                self._conn.commit()
        return [event_view(dict(r)) for r in rows]

    def rewrite_events(self, deal_id: int, rewrite: Callable[[str], str]) -> int:
        changed = 0
        with self._lock:
            rows = self._conn.execute("SELECT id, detail FROM deal_events WHERE deal_id = ?", (deal_id,)).fetchall()
            for row in rows:
                new = rewrite(row["detail"] or "{}")
                if new != row["detail"]:
                    self._conn.execute("UPDATE deal_events SET detail = ? WHERE id = ?", (new, row["id"]))
                    changed += 1
            self._conn.commit()
        return changed
