"""Prepare and check a persona's example dialogues (data/examples/<persona>.txt).

    python tools/persona_examples.py check data/examples/yang_yujing.txt --persona 杨宇靖
    python tools/persona_examples.py extract --speaker 阿明 --as 杨宇靖 --out data/examples/yang_yujing.txt

``check`` lists how many examples the bot will use and every skipped block
(exit status 1 when something was skipped).

``extract`` drafts examples from the bot's own database (live and imported
messages): each text message of ``--speaker`` with the one or two messages
by others just before it (within ``--gap`` seconds) becomes a block, the
speaker written as ``--as`` and everyone else as 群友 / 群友A.  It opens the
database read-only; still, run it on the machine the bot runs on, not over a
network share.  Read the draft before using it: delete private details and
lines that do not sound like the persona.  Imitating a real person needs that
person's consent.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.persona_examples import MAX_EXAMPLE_CHARS, parse_examples  # noqa: E402

MIN_CHARS, MAX_CHARS = 2, 80
CONTEXT = 2


def check(path: Path, personas: list[str], out) -> int:
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"!!  cannot read {path}: {exc} (save it as UTF-8)", file=out)
        return 1
    book = parse_examples(text, personas or [path.stem])
    for problem in book.problems:
        print(f"!!  {problem}", file=out)
    print(f"{len(book)} example(s) usable, {len(book.problems)} skipped", file=out)
    return 1 if book.problems else 0


def _rows(conn: sqlite3.Connection, chat: str | None):
    """Text messages of the live and the imported table, per chat in time order."""
    tables = [t for (t,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('messages', 'history_messages')")]
    if not tables:
        return []
    where = "WHERE msg_type = 1" + (" AND chat_id = ?" if chat else "")
    union = " UNION ALL ".join(
        f"SELECT chat_id, sender_name, content, timestamp FROM {table} {where}" for table in tables
    )
    params = tuple(chat for _ in tables) if chat else ()
    return conn.execute(f"SELECT * FROM ({union}) ORDER BY chat_id, timestamp", params).fetchall()


def draft(rows, speaker: str, persona: str, *, gap: int, limit: int) -> list[str]:
    """Blocks of text, newest ``limit`` of them, oldest first."""
    blocks, seen = [], set()
    previous: list[tuple[str, str, str, int]] = []
    for chat_id, sender, content, ts in rows:
        content = " ".join(str(content or "").split())
        if previous and previous[-1][0] != chat_id:
            previous = []
        if sender == speaker and MIN_CHARS <= len(content) <= MAX_CHARS and "http" not in content \
                and content not in seen:
            context = []
            for p_chat, p_sender, p_text, p_ts in reversed(previous):
                if p_sender == speaker or ts - p_ts > gap or len(context) >= CONTEXT:
                    break
                context.insert(0, (p_sender, p_text))
            if context and sum(len(t) for _, t in context) + len(content) <= MAX_EXAMPLE_CHARS:
                others = list(dict.fromkeys(s for s, _ in context))
                label = {s: ("群友" if len(others) == 1 else f"群友{chr(ord('A') + i)}") for i, s in enumerate(others)}
                lines = [f"{label[s]}：{t}" for s, t in context] + [f"{persona}：{content}"]
                blocks.append("\n".join(lines))
                seen.add(content)
        previous.append((chat_id, sender, content, ts))
        previous = previous[-10:]
    return blocks[-limit:] if limit else blocks


def extract(args, out) -> int:
    target = Path(args.out)
    if target.exists() and not args.force:
        print(f"{target} already exists; use --force to replace it", file=out)
        return 1
    db = Path(args.db)
    if not db.is_file():
        print(f"no database at {db}", file=out)
        return 1
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        rows = _rows(conn, args.chat)
    finally:
        conn.close()
    blocks = draft(rows, args.speaker, args.as_name, gap=args.gap, limit=args.limit)
    if not blocks:
        print(f"no messages by {args.speaker!r} with a reply context were found", file=out)
        return 1
    header = (
        f"# {args.speaker} 在群里的真实发言，作为人设「{args.as_name}」的示例草稿（由 tools/persona_examples.py 生成）。\n"
        "# 用之前逐条看一遍：删掉隐私（真名、电话、地址、单位……）和不像这个人设的句子，错字可以改。\n"
        "# 模仿真实的人之前，先征得本人同意。块之间用 --- 分隔。\n\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(header + "\n---\n".join(blocks) + "\n", encoding="utf-8")
    print(f"wrote {len(blocks)} example(s) to {target}", file=out)
    return 0


def main(argv=None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="parse an examples file and list problems")
    c.add_argument("file")
    c.add_argument("--persona", action="append", default=[], help="persona name or id (repeatable)")
    e = sub.add_parser("extract", help="draft examples from the chat database")
    e.add_argument("--speaker", required=True, help="the member whose messages become the persona's lines")
    e.add_argument("--as", dest="as_name", required=True, help="the persona name to write for them")
    e.add_argument("--out", required=True)
    e.add_argument("--db", default="data/messages.db")
    e.add_argument("--chat", help="only this chat id")
    e.add_argument("--gap", type=int, default=120, help="seconds a reply may follow the message before it")
    e.add_argument("--limit", type=int, default=60, help="keep the newest N examples (0 = all)")
    e.add_argument("--force", action="store_true", help="replace an existing file")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check(Path(args.file), args.persona, out)
    return extract(args, out)


if __name__ == "__main__":
    sys.exit(main())
