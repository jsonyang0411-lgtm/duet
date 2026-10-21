#!/usr/bin/env python3
"""Export a de-identified "replay case set" from the bot's SQLite message DB.

A *decision point* is the end of a message burst: a text message that is
followed, in the same chat, by a gap of at least ``--burst-gap`` seconds (or by
nothing at all).  For every selected decision point one JSON object
(``replay-case/v1``) is written per line, with the preceding context, the
trigger and the messages that arrived shortly afterwards.  Everything is
pseudonymised by ``tools/replay_redaction.py`` before it reaches the file.

Quick start (run from anywhere; the DB is opened read-only)::

    python tools/export_replay_cases.py --bot-name 小助手 --dry-run
    python tools/export_replay_cases.py --bot-name 小助手 --max-cases 200 --seed 7
    python tools/export_replay_cases.py --bot-name 小助手 --chat-id 123@chatroom \\
        --since 2025-03-01 --until 2025-03-31 --blocklist words.txt \\
        --group-names data/group_names.json --out data/replay/cases.jsonl

The database is only ever opened read-only (``mode=ro`` + ``query_only``).
Reading a WAL-mode database can leave empty ``-wal``/``-shm`` files next to it;
they are harmless and the bot reuses them.  It is safe to run while the bot is
running, but stopping the bot first gives the most complete snapshot.

Defaults: ``--db data/messages.db`` and ``--out data/replay/cases.jsonl``
(relative to the project root; ``data/*`` is git-ignored, so keep the exports
under ``data/replay/``).  Run with ``--dry-run`` first: it prints how many
items of each kind would be redacted plus a few before/after samples and
writes nothing.

Determinism: sampling depends only on ``--seed`` and the data, so the same
seed gives the same cases.  Sender ids are hashed with a salt that is random
per run unless ``--salt`` is given; pass ``--salt`` as well when you need the
output to be byte-identical between runs.  The salt is never written out.

Replay-case schema (one object per line, UTF-8)::

    {"schema":"replay-case/v1","id":"exp-000001","category":"unlabeled",
     "chat":{"chat_id":"g_ab12cd34@chatroom","group_name":"示例群1","is_group":true},
     "bot_name":"小助手","now":1700000000,
     "context":[{"offset_sec":-240,"sender_id":"u_..","sender_name":"用户A",
                 "content":"..","msg_type":1}],
     "trigger":{"sender_id":"u_..","sender_name":"用户B","content":"..",
                "msg_type":1,"is_at_mentioned":false,"is_reply_to_bot":null,
                "quoted_content":"","quoted_sender_name":"","offset_sec":0},
     "next_messages":[{"offset_sec":12,"sender_id":"u_..","sender_name":"用户A",
                       "content":".."}],
     "bot_state":null,"expect":null,"source":"export"}

The bot's own replies are not stored in the DB and quote metadata is not
stored either, so ``is_reply_to_bot`` and ``bot_state`` are always ``null``
and the quoted fields are empty.  ``expect`` is filled in later with
``tools/label_replay_cases.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Hashable, NamedTuple, Sequence

_TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _TOOLS_DIR.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import replay_redaction as rr  # noqa: E402  (sibling module, see sys.path above)

SCHEMA_VERSION = "replay-case/v1"
DEFAULT_DB = PROJECT_ROOT / "data" / "messages.db"
DEFAULT_OUT = PROJECT_ROOT / "data" / "replay" / "cases.jsonl"
SAMPLE_COUNT = 5
MAX_CHAT_LINES = 25

# Characters that ``str.splitlines`` treats as line breaks but json.dumps leaves
# raw: escape them so that every case is guaranteed to be one physical line.
_LINE_BREAK_ESCAPES = {
    "\U00002028": "\\u2028",
    "\U00002029": "\\u2029",
    "\U00000085": "\\u0085",
}

_SRC_LIVE = 0
_SRC_HISTORY = 1


class ExportError(Exception):
    """Fatal, user-facing error (reported without a traceback)."""


class Row(NamedTuple):
    ts: int
    src: int           # 0 = messages, 1 = history_messages
    row_id: int
    chat_id: str
    sender_id: str
    sender_name: str
    content: str
    msg_type: int


class Candidate(NamedTuple):
    chat_id: str
    ts: int
    src: int
    row_id: int
    day: int


@dataclass
class ExportOptions:
    bot_name: str
    bot_alias: str = ""
    #: other names the group @s the bot by (its WeChat nickname, BOT_ALIASES)
    also_called: tuple[str, ...] = ()
    db: Path = DEFAULT_DB
    out: Path = DEFAULT_OUT
    chat_ids: tuple[str, ...] = ()
    since: int | None = None
    until: int | None = None
    burst_gap: float = 10
    context_count: int = 40
    context_window: int = 900
    lookahead: int = 60
    max_cases: int = 200
    seed: int = 0
    salt: str | None = None
    blocklist: tuple[str, ...] = ()
    group_names: dict[str, str] = field(default_factory=dict)
    include_history: bool = False
    include_private: bool = False
    order: str = "shuffled"
    tz_offset_hours: float = 8.0


@dataclass
class ExportResult:
    cases: list[dict] = field(default_factory=list)
    candidates: int = 0
    chats_considered: int = 0
    chats_skipped_private: int = 0
    unique_messages: int = 0
    unique_senders: int = 0
    stats: Counter = field(default_factory=Counter)
    samples: list[tuple[str, str, str]] = field(default_factory=list)
    per_chat: list[tuple[str, int, int]] = field(default_factory=list)
    days: int = 0
    warnings: list[str] = field(default_factory=list)
    salt_generated: bool = False


# --------------------------------------------------------------------------
# Argument helpers
# --------------------------------------------------------------------------


def parse_time_arg(value: str, tz_offset_hours: float = 8.0, *,
                   is_until: bool = False) -> int:
    """Epoch seconds or ISO date/datetime -> epoch seconds.

    A bare date (``2025-03-31`` / ``20250331``) is interpreted in the
    ``--tz-offset-hours`` zone; for ``--until`` it covers the whole day.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("empty time value")
    tz = timezone(timedelta(hours=tz_offset_hours))
    if text.isdigit() and len(text) != 8:
        number = int(text)
        return number // 1000 if number >= 10**12 else number
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", text):
        day = datetime.strptime(text.replace("-", ""), "%Y%m%d").replace(tzinfo=tz)
        start = int(day.timestamp())
        return start + 86400 if is_until else start
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"cannot parse time {value!r}: use epoch seconds or an ISO date/datetime"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return int(moment.timestamp())


# --------------------------------------------------------------------------
# Database access (read-only)
# --------------------------------------------------------------------------


def open_database(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open *path* read-only; never writes to the bot's database."""
    db_path = Path(path).expanduser()
    if not db_path.is_file():
        raise ExportError(f"database not found: {db_path}")
    base = db_path.resolve().as_uri()
    last_error: Exception | None = None
    for query in ("?mode=ro", "?mode=ro&immutable=1"):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(base + query, uri=True)
            conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
            conn.execute("PRAGMA query_only=1")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.Error as exc:
            last_error = exc
            if conn is not None:
                conn.close()
    raise ExportError(f"cannot open database read-only: {db_path} ({last_error})")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def resolve_tables(conn: sqlite3.Connection, include_history: bool,
                   warnings: list[str] | None = None) -> list[tuple[str, int]]:
    if not _has_table(conn, "messages"):
        raise ExportError("not a bot database: table 'messages' is missing")
    tables = [("messages", _SRC_LIVE)]
    if include_history:
        if _has_table(conn, "history_messages"):
            tables.append(("history_messages", _SRC_HISTORY))
        elif warnings is not None:
            warnings.append(
                "--include-history given but the database has no "
                "history_messages table; using messages only"
            )
    return tables


def _to_seconds(value: object) -> int:
    try:
        ts = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return ts // 1000 if ts > 10**11 else ts


def load_chat_summary(conn: sqlite3.Connection,
                      tables: Sequence[tuple[str, int]]) -> dict[str, int]:
    """chat_id -> timestamp of its first message (all chats in the DB)."""
    summary: dict[str, int] = {}
    for table, _src in tables:
        cur = conn.execute(
            f"SELECT chat_id, MIN(timestamp) FROM {table} GROUP BY chat_id"
        )
        for chat_id, first_ts in cur:
            ts = _to_seconds(first_ts)
            chat_id = str(chat_id)
            summary[chat_id] = min(ts, summary[chat_id]) if chat_id in summary else ts
    return summary


def load_senders(conn: sqlite3.Connection,
                 tables: Sequence[tuple[str, int]]) -> list[tuple[str, str, str, int]]:
    """(chat_id, sender_id, sender_name, first_ts) for every sender/name pair."""
    out: list[tuple[str, str, str, int]] = []
    for table, _src in tables:
        cur = conn.execute(
            f"SELECT chat_id, sender_id, sender_name, MIN(timestamp) "
            f"FROM {table} GROUP BY chat_id, sender_id, sender_name"
        )
        for chat_id, sender_id, sender_name, first_ts in cur:
            out.append((str(chat_id), str(sender_id), str(sender_name or ""),
                        _to_seconds(first_ts)))
    return out


def load_chat_rows(conn: sqlite3.Connection, tables: Sequence[tuple[str, int]],
                   chat_id: str) -> list[Row]:
    """All messages of one chat ordered by (timestamp, table, id)."""
    rows: list[Row] = []
    for table, src in tables:
        cur = conn.execute(
            f"SELECT id, sender_id, sender_name, content, msg_type, timestamp "
            f"FROM {table} WHERE chat_id = ?",
            (chat_id,),
        )
        for row_id, sender_id, sender_name, content, msg_type, timestamp in cur:
            ts = _to_seconds(timestamp)
            if ts <= 0:
                continue
            try:
                kind = int(msg_type) if msg_type is not None else 1
            except (TypeError, ValueError):
                kind = 1
            rows.append(Row(ts, src, int(row_id), chat_id, str(sender_id),
                            str(sender_name or ""),
                            "" if content is None else str(content), kind))
    rows.sort(key=lambda r: (r.ts, r.src, r.row_id))
    if len(tables) > 1:
        # an imported archive may overlap live capture: keep the live copy
        live = {(r.ts, r.content) for r in rows if r.src == _SRC_LIVE}
        rows = [r for r in rows if r.src == _SRC_LIVE or (r.ts, r.content) not in live]
    return rows


# --------------------------------------------------------------------------
# Decision points and windows
# --------------------------------------------------------------------------


def find_decision_points(rows: Sequence[Row], *, burst_gap: float,
                         since: int | None = None,
                         until: int | None = None) -> list[int]:
    """Indexes of burst-end text messages (trigger candidates).

    *rows* must be one chat ordered by time.  A message ends a burst when the
    next message in the chat arrives at least *burst_gap* seconds later, or
    when there is no next message.  Only ``msg_type == 1`` messages with real
    text are eligible, and the trigger time must lie in ``[since, until)``.
    """
    found: list[int] = []
    last = len(rows) - 1
    for i, row in enumerate(rows):
        if row.msg_type != 1:
            continue
        if since is not None and row.ts < since:
            continue
        if until is not None and row.ts >= until:
            continue
        if i < last and rows[i + 1].ts - row.ts < burst_gap:
            continue
        if not row.content.strip() or rr.looks_like_blob(row.content):
            continue
        found.append(i)
    return found


def window_around(rows: Sequence[Row], idx: int, *, context_count: int,
                  context_window: float, lookahead: float) -> tuple[list[Row], list[Row]]:
    """(context rows before, rows after) for the trigger at ``rows[idx]``.

    Context: at most *context_count* messages that are no more than
    *context_window* seconds older than the trigger (inclusive).  Lookahead:
    every later message at most *lookahead* seconds after it (inclusive).
    """
    trigger_ts = rows[idx].ts
    start = idx
    while (start > 0 and idx - start < context_count
           and rows[start - 1].ts >= trigger_ts - context_window):
        start -= 1
    end = idx + 1
    while end < len(rows) and rows[end].ts - trigger_ts <= lookahead:
        end += 1
    return list(rows[start:idx]), list(rows[idx + 1:end])


# --------------------------------------------------------------------------
# Deterministic stratified sampling
# --------------------------------------------------------------------------


def _rank(seed: int, *parts: object) -> int:
    payload = "|".join([str(seed), *(str(p) for p in parts)])
    digest = hashlib.sha256(payload.encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest[:8], "big")


def _allocate(capacities: dict[Hashable, int], total: int, seed: int,
              tag: str) -> dict[Hashable, int]:
    """Max-min fair split of *total* over buckets with the given capacities."""
    alloc: dict[Hashable, int] = {key: 0 for key in capacities}
    remaining = min(total, sum(capacities.values()))
    active = sorted(key for key, cap in capacities.items() if cap > 0)  # type: ignore[type-var]
    while remaining > 0 and active:
        share = remaining // len(active)
        if share == 0:
            lucky = sorted(active, key=lambda k: (_rank(seed, tag, k), str(k)))
            for key in lucky[:remaining]:
                alloc[key] += 1
            break
        for key in active:
            give = min(share, capacities[key] - alloc[key])
            alloc[key] += give
            remaining -= give
        active = [k for k in active if alloc[k] < capacities[k]]
    return alloc


def _natural_key(text: str) -> list[object]:
    """'示例群2' sorts before '示例群10'."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _candidate_key(c: Candidate) -> tuple[int, str, int, int]:
    return (c.ts, c.chat_id, c.src, c.row_id)


def stratified_sample(candidates: Sequence[Candidate], limit: int,
                      seed: int) -> list[Candidate]:
    """Pick at most *limit* candidates, evenly over chats and then over days.

    Deterministic: the result depends only on the candidates and *seed*.
    ``limit <= 0`` keeps everything.  Returned in (ts, chat, id) order.
    """
    ordered = sorted(candidates, key=_candidate_key)
    if limit <= 0 or len(ordered) <= limit:
        return ordered
    by_chat: dict[str, dict[int, list[Candidate]]] = {}
    for cand in ordered:
        by_chat.setdefault(cand.chat_id, {}).setdefault(cand.day, []).append(cand)
    chat_caps = {c: sum(len(v) for v in days.values()) for c, days in by_chat.items()}
    chat_alloc = _allocate(chat_caps, limit, seed, "chat")
    chosen: list[Candidate] = []
    for chat_id in sorted(by_chat):
        quota = chat_alloc[chat_id]
        if quota <= 0:
            continue
        days = by_chat[chat_id]
        day_alloc = _allocate({d: len(v) for d, v in days.items()}, quota, seed,
                              f"day|{chat_id}")
        for day in sorted(days):
            take = day_alloc[day]
            if take <= 0:
                continue
            pool = sorted(
                days[day],
                key=lambda c: (_rank(seed, "pick", c.chat_id, c.ts, c.src, c.row_id),
                               _candidate_key(c)),
            )
            chosen.extend(pool[:take])
    chosen.sort(key=_candidate_key)
    return chosen


# --------------------------------------------------------------------------
# Case construction
# --------------------------------------------------------------------------


class _CaseBuilder:
    """Redacts rows once (cached) and assembles case dictionaries."""

    def __init__(self, redactor: rr.Redactor, opts: ExportOptions) -> None:
        self.redactor = redactor
        self.opts = opts
        self._cache: dict[tuple[str, int, int], rr.RedactedMessage] = {}
        self._senders: set[tuple[str, str]] = set()
        self.changes: list[tuple[tuple[str, ...], str, str]] = []

    def redact(self, row: Row) -> rr.RedactedMessage:
        key = (row.chat_id, row.src, row.row_id)
        cached = self._cache.get(key)
        if cached is None:
            cached = self.redactor.redact_message(row.chat_id, row.content, row.msg_type)
            self._cache[key] = cached
            if cached.kinds:
                self.changes.append((tuple(cached.kinds), row.content, cached.text))
        return cached

    @property
    def unique_messages(self) -> int:
        return len(self._cache)

    @property
    def unique_senders(self) -> int:
        return len(self._senders)

    def _identity(self, row: Row) -> tuple[str, str]:
        red = self.redactor
        self._senders.add((row.chat_id, row.sender_id))
        return (red.sender_hash(row.chat_id, row.sender_id),
                red.sender_label(row.chat_id, row.sender_id, row.sender_name))

    def build(self, trigger: Row, context: Sequence[Row], later: Sequence[Row]) -> dict:
        opts = self.opts
        red = self.redactor
        raw_trigger = trigger.content
        normalized = rr.normalize_text(raw_trigger)
        at_mentioned = any(
            mention in raw_trigger or mention in normalized
            for mention in ("@" + name for name in (opts.bot_name, *opts.also_called))
        )

        context_items = []
        for row in context:
            sid, label = self._identity(row)
            msg = self.redact(row)
            context_items.append({
                "offset_sec": row.ts - trigger.ts,
                "sender_id": sid,
                "sender_name": label,
                "content": msg.text,
                "msg_type": msg.msg_type,
            })
        t_sid, t_label = self._identity(trigger)
        t_msg = self.redact(trigger)
        trigger_item = {
            "sender_id": t_sid,
            "sender_name": t_label,
            "content": t_msg.text,
            "msg_type": t_msg.msg_type,
            "is_at_mentioned": at_mentioned,
            "is_reply_to_bot": None,
            "quoted_content": "",
            "quoted_sender_name": "",
            "offset_sec": 0,
        }
        next_items = []
        for row in later:
            sid, label = self._identity(row)
            msg = self.redact(row)
            next_items.append({
                "offset_sec": row.ts - trigger.ts,
                "sender_id": sid,
                "sender_name": label,
                "content": msg.text,
            })
        return {
            "schema": SCHEMA_VERSION,
            "id": "",
            "category": "unlabeled",
            "chat": red.chat_info(trigger.chat_id),
            "bot_name": red.bot_alias,
            "now": trigger.ts,
            "context": context_items,
            "trigger": trigger_item,
            "next_messages": next_items,
            "bot_state": None,
            "expect": None,
            "source": "export",
        }


def pick_samples(changes: Sequence[tuple[tuple[str, ...], str, str]],
                 count: int = SAMPLE_COUNT) -> list[tuple[str, str, str]]:
    """Up to *count* (kind, before, after) samples, one per kind where possible."""
    picked: list[tuple[str, str, str]] = []
    used: set[int] = set()
    for kind in rr.KIND_ORDER:
        if len(picked) >= count:
            break
        for idx, (kinds, before, after) in enumerate(changes):
            if idx not in used and kind in kinds:
                picked.append((kind, before, after))
                used.add(idx)
                break
    for idx, (kinds, before, after) in enumerate(changes):
        if len(picked) >= count:
            break
        if idx not in used:
            picked.append((kinds[0], before, after))
            used.add(idx)
    return picked


def run_export(conn: sqlite3.Connection, opts: ExportOptions) -> ExportResult:
    """Select decision points, sample, de-identify and assemble the cases."""
    result = ExportResult()
    tables = resolve_tables(conn, opts.include_history, result.warnings)
    chat_first_ts = load_chat_summary(conn, tables)

    redactor = rr.Redactor(salt=opts.salt, bot_name=opts.bot_name,
                           bot_alias=opts.bot_alias, blocklist=opts.blocklist,
                           group_names=opts.group_names, other_names=opts.also_called)
    result.salt_generated = redactor.salt_was_generated
    # register everything up front so pseudonyms do not depend on the filters
    for chat_id, first_ts in chat_first_ts.items():
        redactor.register_chat(chat_id, first_ts)
    for chat_id, sender_id, sender_name, first_ts in load_senders(conn, tables):
        redactor.register_sender(chat_id, sender_id, sender_name, first_ts)

    wanted = list(dict.fromkeys(opts.chat_ids))
    if wanted:
        for chat_id in wanted:
            if chat_id not in chat_first_ts:
                result.warnings.append(f"--chat-id not found in database: {chat_id}")
        selected = sorted(c for c in wanted if c in chat_first_ts)
    else:
        selected = sorted(
            c for c in chat_first_ts if opts.include_private or c.endswith("@chatroom")
        )
        result.chats_skipped_private = len(chat_first_ts) - len(selected)
    result.chats_considered = len(selected)

    tz_seconds = int(round(opts.tz_offset_hours * 3600))
    candidates: list[Candidate] = []
    for chat_id in selected:
        rows = load_chat_rows(conn, tables, chat_id)
        for i in find_decision_points(rows, burst_gap=opts.burst_gap,
                                      since=opts.since, until=opts.until):
            r = rows[i]
            candidates.append(
                Candidate(chat_id, r.ts, r.src, r.row_id, (r.ts + tz_seconds) // 86400)
            )
    result.candidates = len(candidates)
    chosen = stratified_sample(candidates, opts.max_cases, opts.seed)

    builder = _CaseBuilder(redactor, opts)
    built: list[tuple[Candidate, dict]] = []
    by_chat: dict[str, list[Candidate]] = {}
    for cand in chosen:
        by_chat.setdefault(cand.chat_id, []).append(cand)
    for chat_id in sorted(by_chat):
        rows = load_chat_rows(conn, tables, chat_id)
        index = {(r.src, r.row_id): i for i, r in enumerate(rows)}
        for cand in by_chat[chat_id]:
            i = index.get((cand.src, cand.row_id))
            if i is None:  # row vanished between the two passes
                continue
            context, later = window_around(
                rows, i, context_count=opts.context_count,
                context_window=opts.context_window, lookahead=opts.lookahead,
            )
            built.append((cand, builder.build(rows[i], context, later)))

    if opts.order == "chronological":
        built.sort(key=lambda item: _candidate_key(item[0]))
    else:
        built.sort(key=lambda item: (
            _rank(opts.seed, "order", item[0].chat_id, item[0].ts,
                  item[0].src, item[0].row_id),
            _candidate_key(item[0]),
        ))
    for number, (_cand, case) in enumerate(built, 1):
        case["id"] = f"exp-{number:06d}"
    result.cases = [case for _cand, case in built]

    per_chat_selected = Counter(c.chat_id for c, _ in built)
    per_chat_candidates = Counter(c.chat_id for c in candidates)
    for chat_id in selected:
        info = redactor.chat_info(chat_id)
        result.per_chat.append((
            f"{info['group_name']} ({info['chat_id']})",
            per_chat_candidates[chat_id],
            per_chat_selected[chat_id],
        ))
    result.per_chat.sort(key=lambda item: _natural_key(item[0]))
    result.days = len({c.day for c, _ in built})
    result.unique_messages = builder.unique_messages
    result.unique_senders = builder.unique_senders
    result.stats = Counter(redactor.stats)
    result.samples = pick_samples(builder.changes)
    return result


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def dumps_case(case: dict) -> str:
    """Compact, single-physical-line JSON with ``ensure_ascii=False``."""
    text = json.dumps(case, ensure_ascii=False, separators=(",", ":"))
    for raw, escaped in _LINE_BREAK_ESCAPES.items():
        if raw in text:
            text = text.replace(raw, escaped)
    return text


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* next to *path* and atomically replace it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_cases(path: Path, cases: Sequence[dict]) -> None:
    atomic_write_text(path, "".join(dumps_case(c) + "\n" for c in cases))


# ---- "is the output inside a git work tree without being ignored?" ---------


def find_git_root(path: Path) -> Path | None:
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _gitignore_rules(root: Path) -> list[tuple[str, bool, bool, bool]]:
    rules: list[tuple[str, bool, bool, bool]] = []
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rules
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        if line:
            rules.append((line, negated, anchored, dir_only))
    return rules


def is_gitignored(rel_posix: str, rules: Sequence[tuple[str, bool, bool, bool]]) -> bool:
    """Small approximation of ``git check-ignore`` for a root .gitignore."""
    parts = [p for p in rel_posix.split("/") if p]
    ignored = False
    for pattern, negated, anchored, dir_only in rules:
        hit = False
        for k in range(1, len(parts) + 1):
            if dir_only and k == len(parts):
                continue  # the full path is a file, not a directory
            subject = "/".join(parts[:k]) if anchored else parts[k - 1]
            if fnmatchcase(subject, pattern):
                hit = True
                break
        if hit:
            ignored = not negated
    return ignored


def repo_output_warning(out_path: Path) -> str | None:
    """Warning text when *out_path* is in a git work tree and not ignored."""
    resolved = Path(out_path).expanduser().resolve()
    root = find_git_root(resolved.parent)
    if root is None:
        return None
    rel = resolved.relative_to(root).as_posix()
    if is_gitignored(rel, _gitignore_rules(root)):
        return None
    return (
        f"WARNING: {resolved} is inside the git repository {root} and is not "
        "git-ignored. De-identified chat data can still be sensitive: write it "
        "under data/replay/ (data/* is ignored) or outside the repository."
    )


# ---- reports -------------------------------------------------------------


def _clip(text: str, limit: int = 140) -> str:
    flat = text.replace("\r", "\\r").replace("\n", "\\n")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def format_report(result: ExportResult, opts: ExportOptions, *, dry_run: bool) -> str:
    lines: list[str] = []
    if dry_run:
        lines.append("== DRY RUN: nothing was written ==")
    lines.append(
        f"chats considered: {result.chats_considered}"
        + (f" (skipped {result.chats_skipped_private} non-group chats; "
           "use --include-private)" if result.chats_skipped_private else "")
    )
    lines.append(
        f"decision points: {result.candidates} found -> {len(result.cases)} "
        f"selected (max-cases={opts.max_cases or 'all'}, seed={opts.seed}, "
        f"days covered={result.days})"
    )
    for name, found, picked in result.per_chat[:MAX_CHAT_LINES]:
        lines.append(f"  {name}: {found} found, {picked} selected")
    if len(result.per_chat) > MAX_CHAT_LINES:
        lines.append(f"  ... and {len(result.per_chat) - MAX_CHAT_LINES} more chats")
    lines.append(
        f"unique messages in the selected cases: {result.unique_messages} "
        f"(from {result.unique_senders} distinct senders)"
    )
    lines.append("redactions applied (counted once per unique message):")
    any_stat = False
    for kind in rr.KIND_ORDER:
        count = result.stats.get(kind, 0)
        if count:
            any_stat = True
            lines.append(f"  {kind:<16}{count:>7}   {rr.KIND_LABELS[kind]}")
    if not any_stat:
        lines.append("  (none)")
    if dry_run and result.samples:
        lines.append(f"sample redactions ({len(result.samples)}; the BEFORE side shows "
                     "real data, do not paste it anywhere):")
        for kind, before, after in result.samples:
            lines.append(f"  [{kind}]")
            lines.append(f"    before: {_clip(before)}")
            lines.append(f"    after : {_clip(after)}")
    if result.salt_generated:
        lines.append("sender ids: salted with a random per-run salt (never written); "
                     "pass --salt for reproducible ids")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_replay_cases.py",
        description=(
            "Export de-identified replay cases (decision points at the end of "
            "message bursts) from the bot's messages.db as JSON Lines."
        ),
        epilog=(
            "Default output directory: data/replay/ (git-ignored).  Use --dry-run "
            "to preview the redaction counts and samples without writing.  "
            "Limitations: names that never appear as a sender name of the chat, "
            "addresses and other free-text facts are not detected; skim the "
            "output before sharing it (see tools/replay_redaction.py)."
        ),
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), metavar="PATH",
                        help="SQLite database, opened read-only "
                             "(default: data/messages.db in the project root)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), metavar="PATH",
                        help="output JSONL path (default: data/replay/cases.jsonl "
                             "in the project root; data/* is git-ignored)")
    parser.add_argument("--bot-name", required=True,
                        help="bot display name; a trigger containing '@<name>' is "
                             "marked is_at_mentioned")
    parser.add_argument("--also-called", action="append", default=[], metavar="NAME",
                        help="another name the group @s the bot by, e.g. its WeChat nickname "
                             "(BOT_ALIASES; repeatable): '@<NAME>' also marks is_at_mentioned "
                             "and is written as the bot's name")
    parser.add_argument("--bot-alias", default="", metavar="NAME",
                        help="write NAME instead of the real bot name (case field "
                             "bot_name and every @bot mention in the text); "
                             "is_at_mentioned still uses the real name")
    parser.add_argument("--chat-id", action="append", default=[], dest="chat_ids",
                        metavar="CHAT_ID",
                        help="restrict to this chat (repeatable); default: all group chats")
    parser.add_argument("--since", help="earliest trigger time: epoch seconds or ISO "
                                        "date/datetime (inclusive)")
    parser.add_argument("--until", help="latest trigger time: epoch seconds or ISO "
                                        "datetime (exclusive); a bare date covers the whole day")
    parser.add_argument("--burst-gap", type=float, default=10,
                        help="seconds of silence that end a burst (default: %(default)s)")
    parser.add_argument("--context-count", type=int, default=40,
                        help="max context messages before the trigger (default: %(default)s)")
    parser.add_argument("--context-window", type=int, default=900,
                        help="max age of context messages in seconds, inclusive "
                             "(default: %(default)s)")
    parser.add_argument("--lookahead", type=int, default=60,
                        help="seconds after the trigger collected as next_messages, "
                             "inclusive (default: %(default)s)")
    parser.add_argument("--max-cases", type=int, default=200,
                        help="sample at most N cases, stratified over chats and days; "
                             "0 = export every decision point (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=0,
                        help="sampling/order seed; same seed => same cases (default: %(default)s)")
    parser.add_argument("--salt", default=None,
                        help="salt for sender/chat id hashes (default: random per run; "
                             "never written to the output)")
    parser.add_argument("--blocklist", metavar="FILE",
                        help="file with one word per line, replaced by <已屏蔽> "
                             "('#' lines are comments)")
    parser.add_argument("--group-names", metavar="FILE",
                        help="JSON {chat_id: name} (e.g. data/group_names.json) so real "
                             "group names inside messages become 示例群N")
    parser.add_argument("--include-history", action="store_true",
                        help="also use the history_messages table (imported archives)")
    parser.add_argument("--include-private", action="store_true",
                        help="also export non-group (private) chats")
    parser.add_argument("--order", choices=("shuffled", "chronological"),
                        default="shuffled",
                        help="order (and numbering) of the output cases; shuffled "
                             "keeps a partially labeled file representative "
                             "(default: %(default)s)")
    parser.add_argument("--tz-offset-hours", type=float, default=8.0,
                        help="UTC offset used for day buckets and bare dates "
                             "(default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print redaction counts and %d before/after samples; "
                             "write nothing" % SAMPLE_COUNT)
    return parser


def _read_text_file(path: str, what: str) -> str:
    """UTF-8 (with or without BOM); GBK as a fallback for Windows Notepad files."""
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise ExportError(f"cannot read {what} file {path}: {exc}") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExportError(f"{what} file {path} is neither UTF-8 nor GBK text")


def options_from_args(args: argparse.Namespace,
                      parser: argparse.ArgumentParser) -> ExportOptions:
    bot_name = (args.bot_name or "").strip()
    if not bot_name:
        parser.error("--bot-name must not be empty")
    for flag in ("context_count", "context_window", "lookahead", "max_cases"):
        if getattr(args, flag) < 0:
            parser.error(f"--{flag.replace('_', '-')} must be >= 0")
    if args.burst_gap < 0:
        parser.error("--burst-gap must be >= 0")
    try:
        since = (parse_time_arg(args.since, args.tz_offset_hours)
                 if args.since else None)
        until = (parse_time_arg(args.until, args.tz_offset_hours, is_until=True)
                 if args.until else None)
    except ValueError as exc:
        parser.error(str(exc))
    if since is not None and until is not None and until <= since:
        parser.error("--until must be later than --since")

    blocklist: tuple[str, ...] = ()
    if args.blocklist:
        blocklist = tuple(rr.parse_blocklist(
            _read_text_file(args.blocklist, "blocklist").splitlines()))
    group_names: dict[str, str] = {}
    if args.group_names:
        try:
            group_names = rr.parse_group_names(
                json.loads(_read_text_file(args.group_names, "group-names")))
        except json.JSONDecodeError as exc:
            raise ExportError(f"--group-names is not valid JSON: {exc}") from exc
    return ExportOptions(
        bot_name=bot_name,
        bot_alias=(args.bot_alias or "").strip(),
        also_called=tuple(n.strip() for n in args.also_called if n.strip() and n.strip() != bot_name),
        db=Path(args.db),
        out=Path(args.out),
        chat_ids=tuple(args.chat_ids),
        since=since,
        until=until,
        burst_gap=args.burst_gap,
        context_count=args.context_count,
        context_window=args.context_window,
        lookahead=args.lookahead,
        max_cases=args.max_cases,
        seed=args.seed,
        salt=args.salt or None,
        blocklist=blocklist,
        group_names=group_names,
        include_history=args.include_history,
        include_private=args.include_private,
        order=args.order,
        tz_offset_hours=args.tz_offset_hours,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        opts = options_from_args(args, parser)
        conn = open_database(opts.db)
        try:
            result = run_export(conn, opts)
        finally:
            conn.close()
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if not result.cases:
            detail = f"{result.chats_considered} chats considered"
            if result.chats_skipped_private:
                detail += (f", {result.chats_skipped_private} non-group chats "
                           "skipped: use --include-private")
            raise ExportError(
                f"no decision points matched the filters ({detail}; check "
                "--chat-id, --since/--until, --burst-gap); nothing was written"
            )
        print(format_report(result, opts, dry_run=args.dry_run))
        if args.dry_run:
            return 0
        if opts.out.is_dir():
            raise ExportError(f"--out is a directory: {opts.out}")
        warning = repo_output_warning(opts.out)
        if warning:
            print(warning, file=sys.stderr)
        try:
            write_cases(opts.out, result.cases)
        except OSError as exc:
            raise ExportError(f"cannot write {opts.out}: {exc}") from exc
        print(f"wrote {len(result.cases)} cases to {opts.out}")
        print("de-identification is best effort: skim the file before sharing it, "
              "then label it with tools/label_replay_cases.py")
        return 0
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if callable(_reconfigure):
            try:
                _reconfigure(errors="replace")
            except Exception:  # pragma: no cover - exotic stream types
                pass
    sys.exit(main())
