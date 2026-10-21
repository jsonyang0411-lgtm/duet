"""Inspect and correct the bot's memory (memory v2).

    python tools/memory_admin.py chats
    python tools/memory_admin.py stats [--chat CHAT]
    python tools/memory_admin.py facts --chat CHAT [--all]
    python tools/memory_admin.py show FACT_ID
    python tools/memory_admin.py episodes --chat CHAT
    python tools/memory_admin.py retract --chat CHAT FACT_ID
    python tools/memory_admin.py retract-episode --chat CHAT EPISODE_ID
    python tools/memory_admin.py legacy-facts --chat CHAT [--delete ID]
    python tools/memory_admin.py forget-sender --chat CHAT --sender WXID [--yes]
    python tools/memory_admin.py backfill --chat CHAT --windows 5 [--yes]
    python tools/memory_admin.py drop-index --yes

``retract`` keeps the fact but stops it from being used or re-learned;
``retract-episode`` deletes a summary; ``legacy-facts`` lists (and with
``--delete`` removes) the alias lines the old regex extractor stored, which are
still shown while memory v2 is on.  ``forget-sender`` removes every fact that
rests only on one person's messages and every summary they appear in (the raw
messages stay).  ``backfill`` calls the AI backend from .env on the last N
windows of a chat, so a group does not start with an empty memory; it prints an
estimate first and does nothing without ``--yes``.  ``--reset`` moves the read
mark to those last N windows even if the bot already read further; run it with
the bot stopped, the bot's own job would read the same windows.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.schema import initialize_db  # noqa: E402
from src.db.store import MessageStore  # noqa: E402
from src.memory.store import STATUS_RETRACTED, MemoryStore  # noqa: E402
from src.memory.text import quote_matches  # noqa: E402
from src.memory.worker import MemoryWorker  # noqa: E402

CHARS_PER_TOKEN = 1.6          # rough for Chinese text, used only for the estimate


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "-"


def cmd_chats(ms: MemoryStore, args, out) -> int:
    rows = ms.conn.execute(
        """SELECT m.chat_id, COUNT(*) AS msgs,
                  (SELECT COUNT(*) FROM memory_facts f WHERE f.chat_id = m.chat_id) AS facts,
                  (SELECT COUNT(*) FROM memory_episodes e WHERE e.chat_id = m.chat_id) AS episodes
           FROM messages m GROUP BY m.chat_id ORDER BY msgs DESC"""
    ).fetchall()
    print(f"{'chat_id':<44} {'messages':>9} {'facts':>6} {'episodes':>9}", file=out)
    for row in rows:
        print(f"{row['chat_id']:<44} {row['msgs']:>9} {row['facts']:>6} {row['episodes']:>9}", file=out)
    return 0


def cmd_stats(ms: MemoryStore, args, out) -> int:
    for key, value in ms.stats(args.chat).items():
        print(f"{key}: {value}", file=out)
    if args.chat:
        progress = ms.get_progress(args.chat)
        pending = ms.count_pending(args.chat, int(progress["last_row_id"])) if progress else None
        print(f"progress: {progress}", file=out)
        print(f"pending messages: {pending[0] if pending else 'not started'}", file=out)
    return 0


def cmd_facts(ms: MemoryStore, args, out) -> int:
    facts = ms.list_facts(args.chat, status=None if args.all else "active")
    if not facts:
        print("(no facts)", file=out)
    for f in facts:
        expires = f" expires {_when(f['expires_at'])}" if f["expires_at"] else ""
        print(
            f"#{f['id']:<5} [{f['status']}] {f['kind']:<12} {f['subject']} | {f['predicate']} | "
            f"{f['object']}  conf={f['confidence']:.2f} support={f['support_count']}{expires}",
            file=out,
        )
    return 0


def cmd_show(ms: MemoryStore, args, out) -> int:
    fact = ms.get_fact(args.fact_id)
    if fact is None:
        print(f"no fact #{args.fact_id}", file=out)
        return 1
    print(f"#{fact['id']} [{fact['status']}] {fact['kind']}: {fact['subject']} {fact['predicate']} "
          f"{fact['object']}  (chat {fact['chat_id']})", file=out)
    print(f"confidence={fact['confidence']:.2f} support={fact['support_count']} "
          f"first seen {_when(fact['created_at'])}, last {_when(fact['last_seen_at'])}", file=out)
    for label, kind in (("", "fact"), ("retired because ", "retire")):
        for ev in ms.get_evidence(kind, fact["id"]):
            row = ms.conn.execute(
                "SELECT content, sender_name FROM messages WHERE message_id = ?", (ev["message_id"],),
            ).fetchone()
            if row is None:
                status = "message no longer stored"
            elif quote_matches(ev["quote"], row["content"], min_chars=2):
                status = "verified against the stored message"
            else:
                status = "QUOTE NOT FOUND in the stored message"
            who = ev.get("sender_name") or (row["sender_name"] if row else ev["sender_id"])
            print(f"  {label}{_when(ev['ts'])} {who}: 「{ev['quote']}」  <{status}>", file=out)
    return 0


def cmd_episodes(ms: MemoryStore, args, out) -> int:
    for ep in ms.recent_episodes(args.chat, limit=args.limit):
        print(f"#{ep['id']} {_when(ep['start_ts'])} ~ {_when(ep['end_ts'])} "
              f"({ep['message_count']} msgs) {ep['topics']}\n    {ep['summary']}", file=out)
    return 0


def cmd_retract_episode(ms: MemoryStore, args, out) -> int:
    ok = ms.delete_episode(args.chat, args.episode_id)
    print("deleted" if ok else "no such episode in that chat", file=out)
    return 0 if ok else 1


def cmd_legacy_facts(ms: MemoryStore, args, out) -> int:
    """The alias lines of the old regex extractor (table group_facts)."""
    if args.delete is not None:
        with ms._lock:
            with ms.conn:
                cur = ms.conn.execute(
                    "DELETE FROM group_facts WHERE id = ? AND chat_id = ?", (args.delete, args.chat),
                )
        print("deleted" if cur.rowcount else "no such legacy fact in that chat", file=out)
        return 0 if cur.rowcount else 1
    rows = ms.conn.execute(
        "SELECT id, subject, predicate, object, confidence FROM group_facts WHERE chat_id = ? "
        "ORDER BY id", (args.chat,),
    ).fetchall()
    if not rows:
        print("(no legacy facts)", file=out)
    for row in rows:
        print(f"#{row['id']:<5} {row['subject']} | {row['predicate']} | {row['object']}  "
              f"conf={row['confidence']:.2f}", file=out)
    return 0


def cmd_retract(ms: MemoryStore, args, out) -> int:
    ok = ms.set_fact_status(args.chat, args.fact_id, STATUS_RETRACTED)
    print("retracted" if ok else "no such fact in that chat", file=out)
    return 0 if ok else 1


def cmd_forget_sender(ms: MemoryStore, args, out) -> int:
    count = ms.conn.execute(
        "SELECT COUNT(*) AS n FROM memory_evidence WHERE sender_id = ?", (args.sender,),
    ).fetchone()["n"]
    if not args.yes:
        print(f"{count} evidence rows rest on {args.sender}'s messages; run again with --yes to forget them", file=out)
        return 0
    print(ms.forget_sender(args.chat, args.sender), file=out)
    return 0


def cmd_drop_index(ms: MemoryStore, args, out) -> int:
    if not args.yes:
        print("this removes the search index and its triggers; add --yes to confirm", file=out)
        return 1
    ms.drop_search_index()
    print("search index removed", file=out)
    return 0


def cmd_backfill(ms: MemoryStore, args, out, summarizer_factory=None) -> int:
    windows = max(1, args.windows)
    worker_args = dict(window_messages=args.window_size, max_window=args.window_size,
                       first_window=windows * args.window_size)
    if args.reset:
        progress = ms.reset_progress(args.chat, keep_last=windows * args.window_size)
    else:
        progress = ms.get_progress(args.chat) or ms.init_progress(args.chat, keep_last=windows * args.window_size)
    pending = ms.pending_messages(args.chat, int(progress["last_row_id"]), limit=windows * args.window_size)
    chars = sum(len(r["content"]) for r in pending)
    est_windows = -(-len(pending) // args.window_size)
    est_in = int(chars / CHARS_PER_TOKEN) + est_windows * 900
    print(f"{len(pending)} messages in {est_windows} window(s); about {est_in} input tokens "
          f"and {est_windows * 600} output tokens", file=out)
    if not args.yes:
        print("nothing sent; add --yes to run it", file=out)
        return 0
    summarizer = summarizer_factory() if summarizer_factory else _default_summarizer()
    worker = MemoryWorker(ms, summarizer, **worker_args)
    if not worker.usable:
        print("the configured AI backend cannot run JSON tasks", file=out)
        return 1
    for i in range(est_windows):
        result = worker.run_window(args.chat, time.time())
        print(f"window {i + 1}/{est_windows}: {result.status} messages={result.messages} {result.detail or ''}", file=out)
        if result.status in ("failed",):
            return 1
    return 0


def _default_summarizer():
    from src.config import load_config
    from src.summarize import create_models
    return create_models(load_config()).memory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="data/messages.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("chats")
    p = sub.add_parser("stats"); p.add_argument("--chat")
    p = sub.add_parser("facts"); p.add_argument("--chat", required=True); p.add_argument("--all", action="store_true")
    p = sub.add_parser("show"); p.add_argument("fact_id", type=int)
    p = sub.add_parser("episodes"); p.add_argument("--chat", required=True); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("retract"); p.add_argument("--chat", required=True); p.add_argument("fact_id", type=int)
    p = sub.add_parser("retract-episode"); p.add_argument("--chat", required=True); p.add_argument("episode_id", type=int)
    p = sub.add_parser("legacy-facts"); p.add_argument("--chat", required=True); p.add_argument("--delete", type=int)
    p = sub.add_parser("forget-sender"); p.add_argument("--chat", required=True)
    p.add_argument("--sender", required=True); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("backfill"); p.add_argument("--chat", required=True)
    p.add_argument("--windows", type=int, default=5); p.add_argument("--window-size", type=int, default=120)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--reset", action="store_true", help="start from the last N windows even if the bot read further")
    p = sub.add_parser("drop-index"); p.add_argument("--yes", action="store_true")
    return parser


COMMANDS = {
    "chats": cmd_chats, "stats": cmd_stats, "facts": cmd_facts, "show": cmd_show,
    "episodes": cmd_episodes, "retract": cmd_retract,
    "retract-episode": cmd_retract_episode, "legacy-facts": cmd_legacy_facts, "forget-sender": cmd_forget_sender,
    "drop-index": cmd_drop_index,
}


def main(argv=None, *, out=None, summarizer_factory=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    db = Path(args.db)
    if not db.exists():
        print(f"database not found: {db}", file=out)
        return 2
    conn = initialize_db(str(db))
    try:
        ms = MemoryStore(MessageStore(conn))
        if args.command == "backfill":
            return cmd_backfill(ms, args, out, summarizer_factory)
        return COMMANDS[args.command](ms, args, out)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
