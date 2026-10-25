"""达人私聊在原项目各表里的内容 90 天后清除（B27，2026-10-25 用户定："达人消息都按 90 天清除"）。

用原项目真实的建表语句（src/db/schema.py）和真实的全文索引，确认删得干净、索引没有残影，
也不碰名单以外的聊天。
"""

import sqlite3
import time

import pytest

from src.db.schema import initialize_db
from src.influencer.settings import PERSONAL_INFO_RETENTION_SECONDS
from src.influencer.store import SOURCE_CONSOLE, InfluencerStore

NOW = time.time()
OLD = int(NOW - PERSONAL_INFO_RETENTION_SECONDS - 3600)
NEW = int(NOW - 3600)
ADDRESS = "张三 13812345678 上海市浦东新区世纪大道100号"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "messages.db")
    conn = initialize_db(path)
    conn.execute(
        "CREATE VIRTUAL TABLE fts_messages USING fts5("
        "content, content='messages', content_rowid='id', tokenize='trigram')"
    )
    rows = [
        ("m-old", "wxid_mei", ADDRESS, OLD),
        ("m-new", "wxid_mei", "这款什么时候上新", NEW),
        ("m-group", "123@chatroom", f"群里有人发了 {ADDRESS}", OLD),
        ("m-friend", "wxid_friend", f"朋友发的 {ADDRESS}", OLD),
    ]
    for message_id, chat_id, content, ts in rows:
        conn.execute(
            "INSERT INTO messages (message_id, chat_id, sender_id, sender_name, content, timestamp)"
            " VALUES (?, ?, ?, '某人', ?, ?)", (message_id, chat_id, chat_id, content, ts),
        )
    conn.execute("INSERT INTO fts_messages(rowid, content) SELECT id, content FROM messages")
    conn.execute("INSERT INTO memory_state (key, value) SELECT 'fts_messages.cursor', MAX(id) FROM messages")
    conn.execute("INSERT INTO bot_replies (chat_id, text, timestamp) VALUES ('wxid_mei', '收到老师的地址啦', ?)", (OLD,))
    conn.execute("INSERT INTO bot_replies (chat_id, text, timestamp) VALUES ('wxid_mei', '好的老师', ?)", (NEW,))
    conn.execute(
        "INSERT INTO message_media (message_id, chat_id, sender_id, sender_name, msg_type, timestamp)"
        " VALUES ('img-old', 'wxid_mei', 'wxid_mei', '小美', 3, ?)", (OLD,),
    )
    # 记忆 v2：一条旧片段、一条还在用的事实（引用了一条旧原话和一条新原话）
    conn.execute(
        "INSERT INTO memory_episodes (chat_id, summary, end_ts) VALUES ('wxid_mei', '小美给了收件地址', ?)", (OLD,),
    )
    episode_id = conn.execute("SELECT MAX(id) FROM memory_episodes").fetchone()[0]
    conn.execute(
        "INSERT INTO memory_facts (chat_id, kind, subject, predicate, object, norm_key, last_seen_at)"
        " VALUES ('wxid_mei', 'pref', '小美', '喜欢', '精华液', 'k1', ?)", (NEW,),
    )
    fact_id = conn.execute("SELECT MAX(id) FROM memory_facts").fetchone()[0]
    for kind, owner, message_id, quote, ts in (
        ("episode", episode_id, "m-old", ADDRESS, OLD),
        ("fact", fact_id, "m-old", ADDRESS, OLD),
        ("fact", fact_id, "m-new", "喜欢精华液", NEW),
    ):
        conn.execute(
            "INSERT INTO memory_evidence (owner_kind, owner_id, message_id, quote, ts) VALUES (?, ?, ?, ?, ?)",
            (kind, owner, message_id, quote, ts),
        )
    conn.commit()
    conn.close()
    store = InfluencerStore(path)
    store.add_contacts(["wxid_mei"], source=SOURCE_CONSOLE)
    store.save_contact("wxid_mei", enabled=False, source=SOURCE_CONSOLE)  # 停用的达人也要清
    return path, store


def rows(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def test_old_influencer_messages_are_deleted_and_the_rest_kept(db):
    path, store = db
    store.run_maintenance(now=NOW)
    assert rows(path, "SELECT message_id FROM messages ORDER BY message_id") == [
        ("m-friend",), ("m-group",), ("m-new",),
    ], "名单以外的聊天、90 天以内的达人消息都不动"
    assert rows(path, "SELECT text FROM bot_replies") == [("好的老师",)]
    assert rows(path, "SELECT COUNT(*) FROM message_media") == [(0,)]


def test_the_search_index_has_no_leftovers(db):
    path, store = db
    store.run_maintenance(now=NOW)
    hits = rows(path, "SELECT rowid FROM fts_messages WHERE fts_messages MATCH ?", "世纪大道")
    remaining = {r[0] for r in rows(path, "SELECT id FROM messages WHERE content LIKE '%世纪大道%'")}
    assert {h[0] for h in hits} == remaining, "索引里只剩下没删的那几条"
    rows(path, "INSERT INTO fts_messages(fts_messages) VALUES('integrity-check')")


def test_memories_built_from_old_messages_go_too(db):
    path, store = db
    store.run_maintenance(now=NOW)
    assert rows(path, "SELECT COUNT(*) FROM memory_episodes") == [(0,)]
    assert rows(path, "SELECT subject FROM memory_facts") == [("小美",)], "还在用的事实留着"
    assert rows(path, "SELECT owner_kind, quote FROM memory_evidence") == [("fact", "喜欢精华液")], \
        "引用的旧原话删掉"


def test_running_it_again_changes_nothing(db):
    path, store = db
    store.run_maintenance(now=NOW)
    before = rows(path, "SELECT message_id FROM messages ORDER BY message_id")
    store.run_maintenance(now=NOW)
    assert rows(path, "SELECT message_id FROM messages ORDER BY message_id") == before


def test_a_database_without_the_original_tables_is_fine():
    store = InfluencerStore(":memory:")
    store.add_contacts(["wxid_mei"], source=SOURCE_CONSOLE)
    store.run_maintenance(now=NOW)
