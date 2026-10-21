"""tools/memory_admin.py against a real SQLite file."""

import importlib.util
import io
from pathlib import Path

import pytest

from memory_support import CHAT, T0, ScriptedJson, add_messages, fact, payload

from src.db.schema import initialize_db
from src.db.store import MessageStore
from src.memory.store import MemoryStore

TOOL = Path(__file__).resolve().parents[1] / "tools" / "memory_admin.py"
spec = importlib.util.spec_from_file_location("memory_admin_tool", TOOL)
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "messages.db"
    conn = initialize_db(str(path))
    store = MessageStore(conn)
    add_messages(store, [("宇肆", "大家好，我是次元哥哥，以后叫我次元就行"),
                         ("小明", "我最喜欢喝美式咖啡，每天都要来一杯")] * 30)
    mstore = MemoryStore(store)
    fid, _ = mstore.upsert_fact(CHAT, kind="alias", subject="宇肆", predicate="称呼是", obj="次元哥哥",
                                confidence=0.9, now=T0, evidence=[{
                                    "message_id": f"m{CHAT}{T0}-0", "quote": "我是次元哥哥",
                                    "sender_id": "wxid_宇肆", "ts": T0}])
    conn.close()
    return path, fid


def run(db_path, *argv, **kw):
    out = io.StringIO()
    code = admin.main(["--db", str(db_path), *argv], out=out, **kw)
    return code, out.getvalue()


def test_a_missing_database_is_reported(tmp_path):
    code, text = run(tmp_path / "nope.db", "chats")
    assert code == 2 and "not found" in text


def test_chats_and_stats(db):
    path, _ = db
    code, text = run(path, "chats")
    assert code == 0 and CHAT in text and "60" in text
    code, text = run(path, "stats", "--chat", CHAT)
    assert "facts: 1" in text and "pending messages: not started" in text


def test_facts_lists_active_by_default_and_all_on_request(db):
    path, fid = db
    assert f"#{fid}" in run(path, "facts", "--chat", CHAT)[1]
    run(path, "retract", "--chat", CHAT, str(fid))
    assert "(no facts)" in run(path, "facts", "--chat", CHAT)[1]
    assert "[retracted]" in run(path, "facts", "--chat", CHAT, "--all")[1]


def test_show_checks_every_quote_against_the_stored_message(db):
    path, fid = db
    code, text = run(path, "show", str(fid))
    assert code == 0 and "「我是次元哥哥」" in text and "verified against the stored message" in text
    conn = initialize_db(str(path))
    conn.execute("UPDATE memory_evidence SET quote = '我是宇宙大王'")
    conn.commit()
    conn.close()
    assert "QUOTE NOT FOUND" in run(path, "show", str(fid))[1]
    assert run(path, "show", "999")[0] == 1


def test_retract_only_touches_the_named_chat(db):
    path, fid = db
    code, text = run(path, "retract", "--chat", "other@chatroom", str(fid))
    assert code == 1 and "no such fact" in text
    assert run(path, "retract", "--chat", CHAT, str(fid))[0] == 0


def test_forget_sender_asks_first_and_then_does_it(db):
    path, fid = db
    code, text = run(path, "forget-sender", "--chat", CHAT, "--sender", "wxid_宇肆")
    assert "1 evidence rows" in text and "--yes" in text
    assert "(no facts)" not in run(path, "facts", "--chat", CHAT)[1]
    code, text = run(path, "forget-sender", "--chat", CHAT, "--sender", "wxid_宇肆", "--yes")
    assert "'facts': 1" in text
    assert "(no facts)" in run(path, "facts", "--chat", CHAT)[1]


def test_episodes(db):
    path, _ = db
    conn = initialize_db(str(path))
    MemoryStore(MessageStore(conn)).add_episode(CHAT, summary="大家认识了一下", topics=["自我介绍"],
                                                start_ts=T0, end_ts=T0 + 60, message_count=2,
                                                evidence=[], now=T0)
    conn.close()
    assert "大家认识了一下" in run(path, "episodes", "--chat", CHAT)[1]


def test_drop_index_needs_confirmation(db):
    path, _ = db
    conn = initialize_db(str(path))
    ms = MemoryStore(MessageStore(conn))
    ms.enable_search_index()
    conn.close()
    assert run(path, "drop-index")[0] == 1
    assert run(path, "drop-index", "--yes")[0] == 0
    conn = initialize_db(str(path))
    assert not [r for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'fts_%'")]
    conn.close()


def test_backfill_estimates_first_and_sends_nothing_without_yes(db):
    path, _ = db
    boom = lambda: (_ for _ in ()).throw(AssertionError("must not build a summarizer"))
    code, text = run(path, "backfill", "--chat", CHAT, "--windows", "1", "--window-size", "40",
                     summarizer_factory=boom)
    assert code == 0 and "40 messages in 1 window(s)" in text and "nothing sent" in text


def test_backfill_runs_the_windows_it_announced(db):
    path, _ = db
    model = ScriptedJson(payload(
        facts=[fact("preference", "小明", "喜欢", "美式咖啡", [("m2", "我最喜欢喝美式咖啡")])]))
    code, text = run(path, "backfill", "--chat", CHAT, "--windows", "2", "--window-size", "20",
                     "--yes", summarizer_factory=lambda: model)
    assert code == 0 and text.count("window ") == 2 and "ok" in text
    assert len(model.calls) == 2
    assert "美式咖啡" in run(path, "facts", "--chat", CHAT)[1]


def test_backfill_stops_at_the_first_failure(db):
    path, _ = db
    model = ScriptedJson(RuntimeError("503"))
    code, text = run(path, "backfill", "--chat", CHAT, "--windows", "3", "--window-size", "20",
                     "--yes", summarizer_factory=lambda: model)
    assert code == 1 and "failed" in text and len(model.calls) == 1


def test_backfill_with_a_backend_that_cannot_do_json_says_so(db):
    path, _ = db
    code, text = run(path, "backfill", "--chat", CHAT, "--windows", "1", "--window-size", "20",
                     "--yes", summarizer_factory=lambda: object())
    assert code == 1 and "cannot run JSON tasks" in text


def test_retract_episode_and_legacy_facts(db):
    path, _ = db
    conn = initialize_db(str(path))
    ms = MemoryStore(MessageStore(conn))
    eid = ms.add_episode(CHAT, summary="大家认识了一下", topics=[], start_ts=T0, end_ts=T0, message_count=2,
                         evidence=[], now=T0)
    conn.execute("INSERT INTO group_facts(chat_id, subject, predicate, object) VALUES (?, '宇肆', 'alias', '次元哥哥')",
                 (CHAT,))
    conn.commit()
    conn.close()
    assert run(path, "retract-episode", "--chat", "other@chatroom", str(eid))[0] == 1
    assert run(path, "retract-episode", "--chat", CHAT, str(eid))[0] == 0
    assert "大家认识了一下" not in run(path, "episodes", "--chat", CHAT)[1]
    code, text = run(path, "legacy-facts", "--chat", CHAT)
    assert "宇肆 | alias | 次元哥哥" in text
    assert run(path, "legacy-facts", "--chat", "other@chatroom", "--delete", "1")[0] == 1
    assert run(path, "legacy-facts", "--chat", CHAT, "--delete", "1")[0] == 0
    assert "(no legacy facts)" in run(path, "legacy-facts", "--chat", CHAT)[1]


def test_backfill_reset_goes_back_to_the_requested_windows_even_if_the_bot_read_further(db):
    path, _ = db
    conn = initialize_db(str(path))
    ms = MemoryStore(MessageStore(conn))
    ms.set_progress(CHAT, last_row_id=59, ok=True)               # the bot has read all but one message
    conn.close()
    code, text = run(path, "backfill", "--chat", CHAT, "--windows", "2", "--window-size", "20")
    assert "1 messages in 1 window(s)" in text
    code, text = run(path, "backfill", "--chat", CHAT, "--windows", "2", "--window-size", "20", "--reset")
    assert "40 messages in 2 window(s)" in text and "nothing sent" in text


def test_show_names_the_speaker_and_the_retirement_trail(db):
    path, fid = db
    conn = initialize_db(str(path))
    ms = MemoryStore(MessageStore(conn))
    ms.add_evidence("retire", fid, [{"message_id": f"m{CHAT}{T0}-1", "quote": "我最喜欢喝美式咖啡",
                                     "sender_id": "wxid_小明", "sender_name": "小明", "ts": T0}])
    conn.close()
    text = run(path, "show", str(fid))[1]
    assert "retired because" in text and "小明" in text
