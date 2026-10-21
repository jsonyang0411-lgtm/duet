"""Import a WeChat WCDB group backup into the bot's searchable history.

The import reads the backup database only.  Records are stored in the
separate ``history_messages`` table, so they cannot be replayed as new
messages or trigger automatic replies.

Example:
    python tools/import_wechat_history.py \
      --source E:\\BaiduNetdiskDownload\\wechatDataBackup_<chat>@chatroom\\User\\<wxid>\\Msg\\Multi\\MSG.db
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

# Let this standalone tool import the application package when invoked as
# ``python tools/import_wechat_history.py`` from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db import MessageStore, initialize_db


def iter_text_records(source: Path, chat_id: str, owner_wxid: str,
                      owner_name: str):
    """Yield non-empty text rows from one exported group-chat WCDB database."""
    source_uri = source.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(source_uri, uri=True)
    try:
        rows = conn.execute(
            """SELECT localId, IsSender, CreateTime, Type, StrContent
               FROM MSG
               WHERE StrTalker = ?
                 AND Type = 1
                 AND CreateTime > 0
                 AND TRIM(COALESCE(StrContent, '')) != ''
               ORDER BY CreateTime ASC, localId ASC""",
            (chat_id,),
        )
        source_key = hashlib.sha256(
            str(source.resolve()).casefold().encode("utf-8"),
        ).hexdigest()[:16]
        for local_id, is_sender, timestamp, msg_type, content in rows:
            is_owner = bool(is_sender)
            yield {
                "message_id": f"wechat-backup:{source_key}:{local_id}",
                "chat_id": chat_id,
                "sender_id": owner_wxid if is_owner else "backup-unknown",
                "sender_name": owner_name if is_owner else "群友",
                "content": str(content).strip(),
                "msg_type": int(msg_type),
                "source": "wechat_backup",
                "timestamp": int(timestamp),
            }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a WeChat group backup as searchable bot history.",
    )
    parser.add_argument("--source", required=True, help="Path to MSG.db")
    parser.add_argument("--db", default="data/messages.db", help="Bot SQLite DB")
    parser.add_argument("--chat-id", required=True, help="e.g. 1234567890@chatroom")
    parser.add_argument("--owner-wxid", required=True, help="the wxid of the account the backup belongs to")
    parser.add_argument("--owner-name", required=True, help="that account's display name in the group")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        parser.error(f"Backup database was not found: {source}")

    try:
        records = list(iter_text_records(
            source, args.chat_id, args.owner_wxid, args.owner_name,
        ))
    except sqlite3.DatabaseError as exc:
        parser.error(f"Unable to read WeChat backup database: {exc}")

    conn = initialize_db(args.db)
    try:
        inserted = MessageStore(conn).upsert_history_messages(records)
    finally:
        conn.close()

    print(f"Read {len(records)} text messages; imported {inserted} new records.")
    print("Archive records are searchable only and will not trigger replies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
