"""MemoryStore: facts, evidence, episodes, progress, bot replies, search index."""

import sqlite3

import pytest

from memory_support import CHAT, T0, add_history, add_messages, make_stores

from src.memory.store import (
    STATUS_ACTIVE, STATUS_RETRACTED, STATUS_SUPERSEDED, MemoryStore,
)


def ev(message_id, quote="原话", sender="wxid_a", ts=T0):
    return {"message_id": message_id, "quote": quote, "sender_id": sender, "ts": ts}


@pytest.fixture
def stores():
    conn, store, mstore = make_stores()
    yield store, mstore
    conn.close()


# ── facts ───────────────────────────────────────────────────────────────

def test_a_new_fact_is_stored_with_its_evidence(stores):
    _store, mstore = stores
    fact_id, created = mstore.upsert_fact(
        CHAT, kind="alias", subject="宇肆", predicate="称呼是", obj="次元哥哥",
        confidence=0.9, evidence=[ev("m1", "我是次元哥哥")], now=T0,
    )
    assert created
    fact = mstore.get_fact(fact_id)
    assert (fact["subject"], fact["object"], fact["status"]) == ("宇肆", "次元哥哥", STATUS_ACTIVE)
    assert fact["support_count"] == 1 and fact["expires_at"] is None
    assert [e["message_id"] for e in mstore.get_evidence("fact", fact_id)] == ["m1"]


def test_the_same_fact_again_adds_support_and_evidence_not_a_row(stores):
    _store, mstore = stores
    first, _ = mstore.upsert_fact(
        CHAT, kind="preference", subject="小明", predicate="喜欢", obj="咖啡",
        confidence=0.7, evidence=[ev("m1")], now=T0,
    )
    again, created = mstore.upsert_fact(
        CHAT, kind="preference", subject=" 小明 ", predicate="喜欢", obj="咖啡！",
        confidence=0.9, evidence=[ev("m2"), ev("m1")], now=T0 + 10,
    )
    assert again == first and not created
    fact = mstore.get_fact(first)
    assert fact["support_count"] == 2 and fact["confidence"] == 0.9
    assert len(mstore.get_evidence("fact", first)) == 2          # m1 is not duplicated
    assert len(mstore.list_facts(CHAT)) == 1


def test_a_lower_confidence_repeat_never_lowers_the_stored_one(stores):
    _store, mstore = stores
    fid, _ = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                                confidence=0.95, evidence=[ev("m1")], now=T0)
    mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                       confidence=0.5, evidence=[ev("m2")], now=T0)
    assert mstore.get_fact(fid)["confidence"] == 0.95


def test_a_retracted_fact_stays_retracted_when_the_model_repeats_it(stores):
    _store, mstore = stores
    fid, _ = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                                confidence=0.9, evidence=[ev("m1")], now=T0)
    assert mstore.set_fact_status(CHAT, fid, STATUS_RETRACTED)
    again, created = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                                        confidence=0.9, evidence=[ev("m2")], now=T0 + 5)
    assert again == fid and not created
    assert mstore.get_fact(fid)["status"] == STATUS_RETRACTED
    assert mstore.active_facts(CHAT, now=T0) == []
    assert len(mstore.get_evidence("fact", fid)) == 1             # nothing added either


def test_a_superseded_fact_comes_back_when_it_is_confirmed_again(stores):
    _store, mstore = stores
    fid, _ = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                                confidence=0.9, evidence=[ev("m1")], now=T0)
    mstore.set_fact_status(CHAT, fid, STATUS_SUPERSEDED)
    mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                       confidence=0.9, evidence=[ev("m2")], now=T0 + 5)
    assert mstore.get_fact(fid)["status"] == STATUS_ACTIVE


def test_facts_are_per_chat(stores):
    _store, mstore = stores
    mstore.upsert_fact("g1", kind="other", subject="a", predicate="b", obj="c",
                       confidence=0.9, evidence=[ev("m1")], now=T0)
    mstore.upsert_fact("g2", kind="other", subject="a", predicate="b", obj="c",
                       confidence=0.9, evidence=[ev("m2")], now=T0)
    assert len(mstore.list_facts("g1")) == 1 and len(mstore.list_facts("g2")) == 1


def test_active_facts_skip_expired_low_confidence_and_non_active(stores):
    _store, mstore = stores
    def add(obj, **kw):
        return mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj=obj,
                                  evidence=[ev("m" + obj)], now=T0, **kw)[0]
    add("fresh", confidence=0.9, expires_at=T0 + 1000)
    add("forever", confidence=0.9)
    add("expired", confidence=0.9, expires_at=T0 - 1)
    add("weak", confidence=0.3)
    gone = add("gone", confidence=0.9)
    mstore.set_fact_status(CHAT, gone, STATUS_RETRACTED)
    objects = {f["object"] for f in mstore.active_facts(CHAT, now=T0, min_confidence=0.5)}
    assert objects == {"fresh", "forever"}


def test_status_can_only_be_changed_inside_the_facts_own_chat(stores):
    _store, mstore = stores
    fid, _ = mstore.upsert_fact("g1", kind="other", subject="a", predicate="b", obj="c",
                                confidence=0.9, evidence=[ev("m1")], now=T0)
    assert not mstore.set_fact_status("g2", fid, STATUS_RETRACTED)
    assert mstore.get_fact(fid)["status"] == STATUS_ACTIVE
    assert mstore.fact_exists("g1", fid) and not mstore.fact_exists("g2", fid)


# ── episodes, evidence, forgetting ─────────────────────────────────────

def test_episodes_come_back_newest_first_with_their_evidence(stores):
    _store, mstore = stores
    for i in range(4):
        mstore.add_episode(CHAT, summary=f"第{i}段", topics=["爬山", "天气"], start_ts=T0 + i * 100,
                           end_ts=T0 + i * 100 + 50, message_count=10, evidence=[ev(f"m{i}")], now=T0)
    recent = mstore.recent_episodes(CHAT, limit=2)
    assert [e["summary"] for e in recent] == ["第3段", "第2段"]
    assert recent[0]["topics"] == "爬山、天气"
    assert [e["message_id"] for e in mstore.get_evidence("episode", recent[0]["id"])] == ["m3"]


def test_forgetting_a_sender_retracts_what_only_they_supported(stores):
    _store, mstore = stores
    only_a, _ = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="x", obj="1",
                                   confidence=0.9, evidence=[ev("m1", sender="wxid_a")], now=T0)
    both, _ = mstore.upsert_fact(CHAT, kind="other", subject="b", predicate="x", obj="2",
                                 confidence=0.9, evidence=[ev("m2", sender="wxid_a"),
                                                           ev("m3", sender="wxid_b")], now=T0)
    only_b, _ = mstore.upsert_fact(CHAT, kind="other", subject="c", predicate="x", obj="3",
                                   confidence=0.9, evidence=[ev("m4", sender="wxid_b")], now=T0)
    ep = mstore.add_episode(CHAT, summary="s", topics=[], start_ts=T0, end_ts=T0, message_count=1,
                            evidence=[ev("m5", sender="wxid_a")], now=T0)
    other_chat, _ = mstore.upsert_fact("other", kind="other", subject="a", predicate="x", obj="1",
                                       confidence=0.9, evidence=[ev("m9", sender="wxid_a")], now=T0)

    counts = mstore.forget_sender(CHAT, "wxid_a")

    assert counts == {"evidence": 3, "facts": 1, "episodes": 1}
    assert mstore.get_fact(only_a)["status"] == STATUS_RETRACTED
    assert mstore.get_fact(both)["status"] == STATUS_ACTIVE
    assert [e["message_id"] for e in mstore.get_evidence("fact", both)] == ["m3"]
    assert mstore.get_fact(only_b)["status"] == STATUS_ACTIVE
    assert mstore.recent_episodes(CHAT) == []
    assert mstore.get_fact(other_chat)["status"] == STATUS_ACTIVE      # another group is untouched


def test_has_memory_is_true_after_any_fact_or_episode(stores):
    _store, mstore = stores
    assert not mstore.has_memory(CHAT)
    mstore.add_episode(CHAT, summary="s", topics=[], start_ts=1, end_ts=2, message_count=1,
                       evidence=[], now=T0)
    assert mstore.has_memory(CHAT) and not mstore.has_memory("other")


# ── progress ────────────────────────────────────────────────────────────

def test_first_sight_of_a_chat_starts_near_its_end(stores):
    store, mstore = stores
    add_messages(store, [("a", f"消息{i}号内容") for i in range(30)])
    progress = mstore.init_progress(CHAT, keep_last=10)
    assert mstore.count_pending(CHAT, progress["last_row_id"])[0] == 10
    assert mstore.init_progress(CHAT, keep_last=3)["last_row_id"] == progress["last_row_id"]   # not reset


def test_first_sight_of_a_short_chat_starts_at_the_beginning(stores):
    store, mstore = stores
    add_messages(store, [("a", "你好呀大家")] * 3)
    assert mstore.init_progress(CHAT, keep_last=50)["last_row_id"] == 0


def test_pending_messages_are_text_only_and_in_order(stores):
    store, mstore = stores
    add_messages(store, [("a", "第一句话"), ("a", "[图片]", 3), ("b", "第二句话")])
    rows = mstore.pending_messages(CHAT, 0)
    assert [r["content"] for r in rows] == ["第一句话", "第二句话"]
    assert mstore.count_pending(CHAT, rows[0]["row_id"]) == (1, T0 + 120)


def test_progress_records_success_failure_and_backoff(stores):
    _store, mstore = stores
    mstore.set_progress(CHAT, ok=False, now=100, retry_after=700)
    mstore.set_progress(CHAT, ok=False, now=200, retry_after=800)
    row = mstore.get_progress(CHAT)
    assert (row["fail_count"], row["retry_after"]) == (2, 800)
    mstore.set_progress(CHAT, last_row_id=42, ok=True, now=300)
    row = mstore.get_progress(CHAT)
    assert (row["last_row_id"], row["fail_count"], row["retry_after"]) == (42, 0, 0)


# ── bot replies ─────────────────────────────────────────────────────────

def test_bot_replies_are_kept_apart_from_the_group_messages(stores):
    store, mstore = stores
    add_messages(store, [("a", "你好呀")])
    mstore.add_bot_reply(CHAT, "你好", T0 + 5, trigger_message_id="m1", mode="chat")
    assert store.get_messages_since(CHAT, T0 - 1, until_ts=T0 + 1000)[0]["content"] == "你好呀"
    assert len(store.get_messages_since(CHAT, T0 - 1, until_ts=T0 + 1000)) == 1
    assert store.get_new_message_count(CHAT, None) == 1                 # the diary trigger is unaffected


def test_recent_bot_replies_return_the_newest_in_order(stores):
    _store, mstore = stores
    for i in range(8):
        mstore.add_bot_reply(CHAT, f"回复{i}", T0 + i)
    mstore.add_bot_reply("other", "别的群", T0 + 3)
    assert mstore.recent_bot_replies(CHAT, T0, limit=3) == ["回复5", "回复6", "回复7"]
    assert mstore.recent_bot_replies(CHAT, T0 + 6, limit=6) == ["回复6", "回复7"]
    assert [r["text"] for r in mstore.bot_replies_between(CHAT, T0 + 2, T0 + 4)] == ["回复2", "回复3", "回复4"]


# ── search index ────────────────────────────────────────────────────────

def test_the_index_is_absent_until_enabled_and_search_falls_back(stores):
    store, mstore = stores
    add_messages(store, [("a", "周末去爬山好不好")])
    assert not mstore.search_index_ready()
    assert mstore.fts_search(CHAT, ["爬山好"]) is None            # "no index", not "no match"
    assert mstore.fts_search(CHAT, []) == []


def test_the_index_is_built_in_slices_and_new_rows_are_picked_up_by_a_sync():
    conn, store, mstore = make_stores()
    add_messages(store, [("a", f"第{i}条关于火锅的闲聊") for i in range(25)])
    assert mstore.enable_search_index()
    assert not mstore.search_index_ready()
    steps = 0
    while not mstore.sync_index(batch=10):
        steps += 1
    assert steps >= 2 and mstore.search_index_ready()
    add_messages(store, [("b", "新来的一条关于火锅的消息")], start=T0 + 10_000, prefix="new")
    assert len(mstore.fts_search(CHAT, ["火锅的"])) == 26         # a search syncs the tail first
    conn.close()


def test_rows_added_while_the_index_is_being_built_are_indexed_exactly_once():
    conn, store, mstore = make_stores()
    add_messages(store, [("a", "旧消息提到火锅的事")])
    mstore.enable_search_index()
    add_messages(store, [("a", "中间消息提到火锅的事")], start=T0 + 999, prefix="mid")
    while not mstore.sync_index(batch=1):
        pass
    assert len(mstore.fts_search(CHAT, ["火锅的"])) == 2
    conn.execute("INSERT INTO fts_messages(fts_messages) VALUES('integrity-check')")
    conn.close()


def test_the_message_table_has_no_triggers_so_ingestion_cannot_depend_on_the_index():
    conn, store, mstore = make_stores(index=True)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
    conn.execute("DROP TABLE fts_messages")                        # e.g. dropped by another process
    assert store.insert_message({"message_id": "x1", "chat_id": CHAT, "sender_id": "s", "sender_name": "n",
                                 "content": "照常入库", "msg_type": 1, "timestamp": T0})
    assert mstore.fts_search(CHAT, ["照常入"]) is None             # the search notices and says "no index"
    assert not mstore.search_index_ready()
    conn.close()


def test_enabling_twice_is_harmless(stores):
    _store, mstore = stores
    assert mstore.enable_search_index() and mstore.enable_search_index()
    while not mstore.sync_index():
        pass
    assert mstore.enable_search_index()                         # starts over as "not caught up" ...
    assert not mstore.search_index_ready()
    assert mstore.sync_index() and mstore.search_index_ready()  # ... and one sync later it is ready


def test_fts_search_covers_live_and_imported_history_of_this_chat_only():
    conn, store, mstore = make_stores()
    add_messages(store, [("a", "live 说到南通人家的饭")])
    add_messages(store, [("a", "别的群提到南通人家")], chat="other")
    add_history(store, [("b", "很久以前聊过南通人家的事")])
    mstore.enable_search_index()
    while not mstore.backfill_step():
        pass
    rows = mstore.fts_search(CHAT, ["南通人"])
    assert sorted(r["source"] for r in rows) == ["history", "live"]
    assert all(r["sender_id"].startswith("wxid_") for r in rows)
    conn.close()


def test_deleted_messages_disappear_from_results_and_edited_ones_keep_their_old_index_entry():
    # The index is append-only (rows are only ever inserted by the bot); a deleted
    # row is hidden by the join with the message table.
    conn, store, mstore = make_stores(index=True)
    add_messages(store, [("a", "原来的内容是火锅店")])
    assert len(mstore.fts_search(CHAT, ["火锅店"])) == 1
    conn.execute("DELETE FROM messages")
    assert mstore.fts_search(CHAT, ["火锅店"]) == []
    conn.close()


def test_grams_with_quotes_cannot_break_the_match_expression():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, [("a", '他说 "火锅" 很好吃')])
    assert mstore.fts_search(CHAT, ['"火锅"', "火锅 OR", "火锅店"]) == []
    assert len(mstore.fts_search(CHAT, ["很好吃", "火锅 "])) == 1
    conn.close()


def test_like_search_escapes_wildcards_and_covers_both_tables():
    conn, store, mstore = make_stores()
    add_messages(store, [("a", "涨了100%真好"), ("a", "下划线_在这里"), ("a", "普通一句话")])
    add_history(store, [("b", "历史里也有100%的说法")])
    assert [r["content"] for r in mstore.like_search(CHAT, ["100%"])] == \
        ["涨了100%真好", "历史里也有100%的说法"]
    assert len(mstore.like_search(CHAT, ["_在"])) == 1               # "_" is not a wildcard
    assert len(mstore.like_search(CHAT, ["%"])) == 2                 # "%" matches only a literal %
    assert mstore.like_search(CHAT, []) == []
    conn.close()


def test_like_search_only_scans_the_newest_rows():
    conn, store, mstore = make_stores()
    add_messages(store, [("a", "最老的加班消息")] + [("a", f"填充消息第{i}号") for i in range(20)])
    assert mstore.like_search(CHAT, ["加班"], scan=10) == []
    assert len(mstore.like_search(CHAT, ["加班"], scan=100)) == 1
    conn.close()


def test_an_unsupported_sqlite_is_reported_not_raised(monkeypatch):
    conn, store, mstore = make_stores()

    class Failing:
        def __init__(self, real):
            self._real = real
        def execute(self, sql, *args):
            if "VIRTUAL TABLE" in sql:
                raise sqlite3.OperationalError("no such tokenizer: trigram")
            return self._real.execute(sql, *args)
        def __enter__(self):
            return self._real.__enter__()
        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    mstore.conn = Failing(conn)
    assert mstore.enable_search_index() is False
    assert "trigram" in mstore.index_error and not mstore.search_index_ready()
    conn.close()


def test_dropping_the_index_removes_tables_and_triggers():
    conn, store, mstore = make_stores(index=True)
    mstore.drop_search_index()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not {n for n in names if n.startswith("fts_")}
    assert add_messages(store, [("a", "之后还能正常入库")])
    assert not mstore.search_index_ready()
    conn.close()


def test_stats_summarise_the_tables(stores):
    _store, mstore = stores
    mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                       confidence=0.9, evidence=[ev("m1")], now=T0)
    stats = mstore.stats(CHAT)
    assert stats["facts"] == 1 and stats["facts_by_status"] == {"active": 1}
    assert stats["episodes"] == 0 and stats["index_ready"] is False
    assert mstore.stats()["facts"] == 1


def test_history_import_counts_only_its_own_rows_even_with_the_index_on():
    # total_changes would include the rows the FTS sync triggers write.
    for index in (False, True):
        conn, store, _mstore = make_stores(index=index)
        rows = [{"message_id": f"h{i}", "chat_id": CHAT, "sender_id": "s", "sender_name": "n",
                 "content": f"第{i}条很长的历史消息", "timestamp": i} for i in range(3)]
        assert store.upsert_history_messages(rows) == 3
        assert store.upsert_history_messages(rows) == 0
        conn.close()


def test_backfill_before_enabling_does_nothing(stores):
    _store, mstore = stores
    assert mstore.backfill_step() is False and not mstore.search_index_ready()


def test_an_fts_failure_at_query_time_switches_the_index_off_for_the_process():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, [("a", "周末去爬山好不好")])
    assert len(mstore.fts_search(CHAT, ["爬山好"])) == 1

    class Broken:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if "MATCH" in sql:
                raise sqlite3.OperationalError("database disk image is malformed")
            return self._real.execute(sql, *args)

        def __enter__(self):
            return self._real.__enter__()

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    mstore.conn = Broken(conn)
    assert mstore.fts_search(CHAT, ["爬山好"]) is None
    assert not mstore.index_enabled and "malformed" in mstore.index_error
    assert mstore.fts_search(CHAT, ["爬山好"]) is None             # and it stays off
    conn.close()


def test_a_repeat_with_a_later_expiry_extends_it_and_an_earlier_one_does_not(stores):
    _store, mstore = stores
    fid, _ = mstore.upsert_fact(CHAT, kind="plan", subject="a", predicate="b", obj="c",
                                confidence=0.9, evidence=[ev("m1")], expires_at=T0 + 100, now=T0)
    mstore.upsert_fact(CHAT, kind="plan", subject="a", predicate="b", obj="c", confidence=0.9,
                       evidence=[ev("m2")], expires_at=T0 + 50, now=T0)
    assert mstore.get_fact(fid)["expires_at"] == T0 + 100
    mstore.upsert_fact(CHAT, kind="plan", subject="a", predicate="b", obj="c", confidence=0.9,
                       evidence=[ev("m3")], expires_at=T0 + 900, now=T0)
    assert mstore.get_fact(fid)["expires_at"] == T0 + 900
    mstore.upsert_fact(CHAT, kind="plan", subject="a", predicate="b", obj="c", confidence=0.9,
                       evidence=[ev("m4")], expires_at=None, now=T0)
    assert mstore.get_fact(fid)["expires_at"] == T0 + 900


def test_the_same_claim_with_different_symbols_stays_two_facts(stores):
    _store, mstore = stores
    ids = set()
    for obj in ("C++", "C#", "3.5万", "35万", "-5", "5"):
        fid, created = mstore.upsert_fact(CHAT, kind="other", subject="小明", predicate="用",
                                          obj=obj, confidence=0.9, evidence=[ev("m" + obj)], now=T0)
        assert created, obj
        ids.add(fid)
    assert len(ids) == 6


def test_support_counts_distinct_messages_so_a_reread_window_adds_nothing(stores):
    _store, mstore = stores
    for _ in range(3):
        fid, _created = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                                           confidence=0.9, evidence=[ev("m1"), ev("m2")], now=T0)
    assert mstore.get_fact(fid)["support_count"] == 2
    mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                       confidence=0.9, evidence=[ev("m3")], now=T0)
    assert mstore.get_fact(fid)["support_count"] == 3


def test_a_transaction_commits_all_or_nothing(stores):
    _store, mstore = stores
    with pytest.raises(RuntimeError):
        with mstore.transaction():
            mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                               confidence=0.9, evidence=[ev("m1")], now=T0)
            mstore.add_episode(CHAT, summary="s", topics=[], start_ts=1, end_ts=2, message_count=1,
                               evidence=[], now=T0)
            mstore.set_progress(CHAT, last_row_id=9, ok=True)
            raise RuntimeError("crash before the commit")
    assert mstore.list_facts(CHAT, status=None) == [] and mstore.recent_episodes(CHAT) == []
    assert mstore.get_progress(CHAT) is None
    with mstore.transaction():
        mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                           confidence=0.9, evidence=[ev("m1")], now=T0)
    assert len(mstore.list_facts(CHAT)) == 1


def test_the_model_cannot_demote_a_fact_the_operator_retracted(stores):
    _store, mstore = stores
    fid, _ = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c",
                                confidence=0.9, evidence=[ev("m1")], now=T0)
    mstore.set_fact_status(CHAT, fid, STATUS_RETRACTED)
    assert not mstore.set_fact_status(CHAT, fid, STATUS_SUPERSEDED, only_if=STATUS_ACTIVE)
    assert mstore.get_fact(fid)["status"] == STATUS_RETRACTED


def test_evidence_remembers_who_said_it(stores):
    _store, mstore = stores
    fid, _ = mstore.upsert_fact(CHAT, kind="other", subject="a", predicate="b", obj="c", confidence=0.9,
                                evidence=[{**ev("m1"), "sender_name": "张\u200b三\n"}], now=T0)
    assert mstore.get_evidence("fact", fid)[0]["sender_name"] == "张三"


def test_an_episode_goes_when_the_sender_is_any_of_its_evidence(stores):
    _store, mstore = stores
    mstore.add_episode(CHAT, summary="A 说了不想让人知道的事", topics=[], start_ts=T0, end_ts=T0, message_count=2,
                       evidence=[ev("m1", sender="wxid_b"), ev("m2", sender="wxid_a")], now=T0)
    counts = mstore.forget_sender(CHAT, "wxid_a")
    assert counts["episodes"] == 1 and mstore.recent_episodes(CHAT) == []
    assert mstore.conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0] == 0


def test_an_episode_can_be_deleted_by_the_operator(stores):
    _store, mstore = stores
    eid = mstore.add_episode(CHAT, summary="s", topics=[], start_ts=T0, end_ts=T0, message_count=1,
                             evidence=[ev("m1")], now=T0)
    assert not mstore.delete_episode("other", eid)
    assert mstore.delete_episode(CHAT, eid) and mstore.recent_episodes(CHAT) == []
    assert mstore.get_evidence("episode", eid) == []


def test_parse_failures_are_counted_per_window_end(stores):
    _store, mstore = stores
    mstore.set_progress(CHAT, ok=False, now=1, retry_after=5, window_end=10, deterministic=True)
    mstore.set_progress(CHAT, ok=False, now=2, retry_after=6, window_end=10, deterministic=True)
    assert mstore.get_progress(CHAT)["parse_fail_count"] == 2
    mstore.set_progress(CHAT, ok=False, now=3, retry_after=7, window_end=15, deterministic=True)
    assert mstore.get_progress(CHAT)["parse_fail_count"] == 1          # a bigger window starts over
    mstore.set_progress(CHAT, ok=False, now=4, retry_after=8)           # a transport error changes nothing
    row = mstore.get_progress(CHAT)
    assert (row["parse_fail_count"], row["fail_count"], row["fail_end_row_id"]) == (1, 4, 15)


def test_reset_progress_moves_the_mark_to_the_requested_distance_from_the_end(stores):
    store, mstore = stores
    add_messages(store, [("a", f"消息{i}号内容") for i in range(30)])
    mstore.set_progress(CHAT, last_row_id=29, ok=True)
    assert mstore.count_pending(CHAT, mstore.get_progress(CHAT)["last_row_id"])[0] == 1
    mstore.reset_progress(CHAT, keep_last=12)
    assert mstore.count_pending(CHAT, mstore.get_progress(CHAT)["last_row_id"])[0] == 12
