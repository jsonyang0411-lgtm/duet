"""The background extraction job: when it runs, what it writes, how it fails."""

import json
import threading

from memory_support import CHAT, T0, ScriptedJson, add_messages, fact, make_stores, payload

from src.memory.worker import MAX_FAILS_PER_WINDOW, RETRY_BACKOFF_SEC, MemoryWorker

LONG = "今天天气不错我们周末一起去爬山吧"        # long enough that a window is worth a model call


def lines(n, who="小明"):
    return [(who, f"{LONG}第{i}次") for i in range(n)]


def good_answer():
    return payload(
        facts=[fact("plan", "小明", "约了", "周末爬山", [("m1", "我们周末一起去爬山")])],
        episode={"summary": "小明约大家周末爬山", "topics": ["爬山"], "evidence": ["m1"]},
    )


def make_worker(summarizer, **kw):
    conn, store, mstore = make_stores()
    worker = MemoryWorker(mstore, summarizer, clock=lambda: T0 + 100_000, **kw)
    return conn, store, mstore, worker


def test_nothing_runs_below_the_threshold():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=40)
    add_messages(store, lines(39))
    worker.note_message(CHAT)
    # right after the 39th message: neither enough messages nor idle long enough
    assert worker.tick(now=T0 + 39 * 60 + 10) == [] and model.calls == []
    conn.close()


def test_a_full_window_runs_once_and_writes_validated_memory():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=40)
    add_messages(store, lines(40))
    worker.note_message(CHAT)

    results = worker.tick(now=T0 + 41 * 60)

    assert [(r.status, r.messages) for r in results] == [("ok", 40)]
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["call_name"] == "memory_extract" and call["max_tokens"] == 3000
    assert call["schema"]["required"] == ["facts", "episode", "retire"]
    [stored] = mstore.list_facts(CHAT)
    assert (stored["subject"], stored["object"], stored["kind"]) == ("小明", "周末爬山", "plan")
    assert stored["expires_at"] is not None
    assert mstore.recent_episodes(CHAT)[0]["message_count"] == 40
    assert mstore.get_progress(CHAT)["last_row_id"] == 40
    assert worker.tick(now=T0 + 41 * 60) == []                      # nothing pending any more
    conn.close()


def test_a_quiet_chat_is_processed_after_the_idle_time_if_enough_was_said():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=40, idle_minutes=20, min_window=8)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    last = T0 + 9 * 60
    assert worker.tick(now=last + 19 * 60) == []                      # 19 minutes: still chatting
    assert [r.status for r in worker.tick(now=last + 21 * 60)] == ["ok"]
    conn.close()


def test_a_handful_of_messages_never_triggers_the_idle_rule():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, min_window=8)
    add_messages(store, lines(5))
    worker.note_message(CHAT)
    assert worker.tick(now=T0 + 10 * 3600) == [] and model.calls == []
    conn.close()


def test_chats_that_were_never_noted_are_left_alone():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model)
    add_messages(store, lines(60))
    assert worker.tick(now=T0 + 10_000) == [] and model.calls == []
    conn.close()


def test_a_big_backlog_is_read_in_bounded_windows_per_tick():
    model = ScriptedJson(payload())
    conn, store, mstore, worker = make_worker(model, window_messages=40, max_window=50, first_window=500)
    add_messages(store, lines(400))
    worker.note_message(CHAT)
    results = worker.tick(now=T0 + 400 * 60 + 10)
    assert len(results) == 3 and all(r.messages == 50 for r in results)       # MAX_WINDOWS_PER_TICK
    assert mstore.count_pending(CHAT, mstore.get_progress(CHAT)["last_row_id"])[0] == 250
    conn.close()


def test_the_first_window_of_a_long_stored_chat_starts_near_its_end():
    model = ScriptedJson(payload())
    conn, store, mstore, worker = make_worker(model, window_messages=40, first_window=60)
    add_messages(store, lines(500))
    worker.note_message(CHAT)
    results = worker.tick(now=T0 + 500 * 60 + 10)
    assert sum(r.messages for r in results) == 60                      # not 500
    conn.close()


def test_tiny_windows_cost_no_model_call_but_are_marked_read():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, [("小明", "哈哈哈")] * 12)
    worker.note_message(CHAT)
    results = worker.tick(now=T0 + 20 * 60)
    assert [r.status for r in results] == ["skipped_short"] and model.calls == []
    assert mstore.get_progress(CHAT)["last_row_id"] == 12
    conn.close()


def test_a_model_failure_backs_off_without_losing_the_window():
    model = ScriptedJson(RuntimeError("503"), good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    now = T0 + 20 * 60
    assert [r.status for r in worker.tick(now=now)] == ["failed"]
    progress = mstore.get_progress(CHAT)
    assert progress["last_row_id"] == 0 and progress["fail_count"] == 1
    assert progress["retry_after"] == now + RETRY_BACKOFF_SEC
    assert worker.tick(now=now + RETRY_BACKOFF_SEC - 1) == [] and len(model.calls) == 1
    assert [r.status for r in worker.tick(now=now + RETRY_BACKOFF_SEC + 1)] == ["ok"]
    assert mstore.get_progress(CHAT)["fail_count"] == 0
    conn.close()


def test_the_pause_grows_with_every_failure_and_is_capped():
    model = ScriptedJson(RuntimeError("503"))
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    now, pauses = T0 + 20 * 60, []
    for _ in range(8):
        worker.tick(now=now)
        retry_after = mstore.get_progress(CHAT)["retry_after"]
        pauses.append(retry_after - now)
        now = retry_after + 1
    assert pauses[:5] == [600, 1200, 2400, 4800, 9600] and max(pauses) == 9600
    conn.close()


def test_an_outage_never_costs_the_window_even_when_the_group_keeps_talking():
    model = ScriptedJson(RuntimeError("503"))
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    now = T0 + 20 * 60
    for round_ in range(6):                                # far more attempts than MAX_FAILS_PER_WINDOW
        worker.tick(now=now)
        add_messages(store, lines(5), start=T0 + 10_000 + round_ * 1_000, prefix=f"r{round_}")
        worker.note_message(CHAT)
        now = mstore.get_progress(CHAT)["retry_after"] + 1
    assert mstore.get_progress(CHAT)["last_row_id"] == 0   # nothing was skipped
    assert mstore.get_progress(CHAT)["parse_fail_count"] == 0
    conn.close()


def test_an_answer_that_is_not_json_counts_as_a_failure():
    model = ScriptedJson("抱歉，我无法处理这个请求")
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    assert [r.status for r in worker.tick(now=T0 + 20 * 60)] == ["failed"]
    assert mstore.list_facts(CHAT) == []
    conn.close()


def test_a_window_whose_answers_are_never_usable_is_skipped_after_three_tries():
    model = ScriptedJson("not json at all")
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    now, statuses = T0 + 20 * 60, []
    for _ in range(MAX_FAILS_PER_WINDOW):
        statuses += [r.status for r in worker.tick(now=now)]
        now = mstore.get_progress(CHAT)["retry_after"] + 1 if statuses[-1] == "failed" else now
    assert statuses == ["failed"] * (MAX_FAILS_PER_WINDOW - 1) + ["dropped"]
    assert mstore.get_progress(CHAT)["last_row_id"] == 10
    conn.close()


def test_a_bigger_window_restarts_the_count_so_unseen_messages_are_never_dropped():
    model = ScriptedJson("not json at all")
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    now = T0 + 20 * 60
    for round_ in range(5):
        worker.tick(now=now)
        add_messages(store, lines(5), start=T0 + 10_000 + round_ * 1_000, prefix=f"g{round_}")
        worker.note_message(CHAT)
        now = mstore.get_progress(CHAT)["retry_after"] + 1
    progress = mstore.get_progress(CHAT)
    assert progress["last_row_id"] == 0 and progress["parse_fail_count"] == 1
    conn.close()


def test_a_backend_without_json_support_fails_cleanly():
    class NoJson:
        pass

    conn, store, mstore, worker = make_worker(NoJson(), window_messages=10)
    assert not worker.usable
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    assert [r.status for r in worker.tick(now=T0 + 20 * 60)] == ["failed"]
    conn.close()


def test_a_hallucinating_model_writes_nothing_but_the_window_is_still_consumed():
    model = ScriptedJson(payload(facts=[fact("alias", "小明", "称呼是", "大王", [("m1", "编的原话啊")])]))
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    [result] = worker.tick(now=T0 + 20 * 60)
    assert result.status == "ok" and result.detail["rejected"] == 1
    assert result.detail["rejected_reasons"] == ["quote_not_in_message"]
    assert mstore.list_facts(CHAT) == [] and mstore.get_progress(CHAT)["last_row_id"] == 10
    conn.close()


def test_bot_replies_in_the_window_are_context_only():
    model = ScriptedJson(payload())
    conn, store, mstore, worker = make_worker(model, window_messages=10, bot_name="林小满")
    add_messages(store, lines(10))
    mstore.add_bot_reply(CHAT, "我也想去爬山呀", T0 + 100)
    mstore.add_bot_reply(CHAT, "窗口之外的回复", T0 + 999_999)
    worker.note_message(CHAT)
    worker.tick(now=T0 + 20 * 60)
    user = model.calls[0]["user"]
    assert "[机器人]" in user and "林小满: 我也想去爬山呀" in user and "窗口之外" not in user
    conn.close()


def test_known_facts_are_offered_to_the_model_with_their_ids():
    model = ScriptedJson(payload())
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    fid, _ = mstore.upsert_fact(CHAT, kind="other", subject="阿红", predicate="住在", obj="上海",
                                confidence=0.9, evidence=[], now=T0)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    worker.tick(now=T0 + 20 * 60)
    assert f"#{fid} 阿红 住在 上海" in model.calls[0]["user"]
    conn.close()


def test_two_ticks_never_run_at_once():
    started, release = threading.Event(), threading.Event()

    class Slow(ScriptedJson):
        def complete_json(self, *a, **kw):
            started.set()
            release.wait(5)
            return super().complete_json(*a, **kw)

    model = Slow(payload())
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    first = threading.Thread(target=lambda: worker.tick(now=T0 + 20 * 60))
    first.start()
    assert started.wait(5)
    assert worker.tick(now=T0 + 20 * 60) == []              # refused immediately, not queued
    release.set()
    first.join(5)
    assert len(model.calls) == 1
    conn.close()


def test_the_thread_builds_the_index_and_stops():
    model = ScriptedJson(payload())
    conn, store, mstore, worker = make_worker(model, interval_sec=0.01)
    add_messages(store, lines(5))
    worker.start()
    worker.start()                                           # second start is a no-op
    deadline = 100
    while not mstore.search_index_ready() and deadline:
        threading.Event().wait(0.05)
        deadline -= 1
    assert mstore.search_index_ready()
    worker.stop()
    assert not worker._thread.is_alive()
    conn.close()


def test_a_crash_while_storing_rolls_everything_back_and_backs_off():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    real = mstore.add_episode

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    mstore.add_episode = explode                           # after the fact has been written
    now = T0 + 20 * 60
    [result] = worker.tick(now=now)
    assert result.status == "failed" and result.detail == {"error": "RuntimeError"}
    assert mstore.list_facts(CHAT, status=None) == []     # the fact written before the crash is gone
    progress = mstore.get_progress(CHAT)
    assert progress["last_row_id"] == 0 and progress["retry_after"] == now + RETRY_BACKOFF_SEC

    mstore.add_episode = real                              # next try: nothing is counted twice
    [result] = worker.tick(now=progress["retry_after"] + 1)
    assert result.status == "ok"
    [fact] = mstore.list_facts(CHAT)
    assert fact["support_count"] == 1
    conn.close()


def test_overflowing_numbers_from_the_model_cannot_loop_the_job():
    huge = payload(facts=[{**fact("other", "小明", "约了", "周末爬山", [("m1", "我们周末一起去爬山")]),
                           "confidence": 10 ** 400, "ttl_days": 10 ** 400}],
                   retire=[{"fact_id": 10 ** 400, "reason": "x", "evidence": []}])
    model = ScriptedJson(json.dumps(huge).replace("1" + "0" * 400, "1e999"))
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10))
    worker.note_message(CHAT)
    [result] = worker.tick(now=T0 + 20 * 60)
    assert result.status == "ok" and result.detail["rejected"] >= 1      # refused, not crashed
    assert mstore.get_progress(CHAT)["last_row_id"] == 10 and len(model.calls) == 1
    conn.close()


def test_one_failing_chat_does_not_stop_the_others():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(10), chat="a@chatroom")
    add_messages(store, lines(10), chat="b@chatroom")
    real = mstore.get_progress

    def flaky(chat_id):
        if chat_id == "a@chatroom":
            raise RuntimeError("boom")
        return real(chat_id)

    mstore.get_progress = flaky
    worker.note_message("a@chatroom")
    worker.note_message("b@chatroom")
    results = worker.tick(now=T0 + 20 * 60)
    assert [r.chat_id for r in results] == ["b@chatroom"] and results[0].status == "ok"
    conn.close()


def test_a_chat_whose_message_arrives_while_it_is_checked_is_not_forgotten():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=10)
    add_messages(store, lines(3))
    worker.note_message(CHAT)
    original = mstore.count_pending

    def count_then_new_message(chat_id, after):
        result = original(chat_id, after)
        worker.note_message(chat_id)                       # a new message lands right after the count
        return result

    mstore.count_pending = count_then_new_message
    worker.tick(now=T0 + 20 * 60)
    assert CHAT in worker._dirty                            # it will be looked at again
    conn.close()


def test_lines_sent_under_the_bots_own_name_are_context_not_evidence():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model, window_messages=10, bot_names=("林小满",))
    add_messages(store, lines(9))
    add_messages(store, [("林小满小号", "我也想去爬山呀大家一起")], start=T0 + 5000, prefix="bot")
    worker.note_message(CHAT)
    worker.tick(now=T0 + 20 * 60)
    user = model.calls[0]["user"]
    assert "[机器人]" in user and "我也想去爬山呀大家一起" in user
    assert user.count("[m") == 9                           # nine citable human messages
    assert mstore.get_progress(CHAT)["last_row_id"] == 10  # but the bot's line was read too
    conn.close()


def test_a_window_with_nothing_left_is_a_quiet_no_op():
    model = ScriptedJson(good_answer())
    conn, store, mstore, worker = make_worker(model)
    mstore.init_progress(CHAT, keep_last=5)
    assert worker.run_window(CHAT, T0).status == "skipped_short" and model.calls == []
    conn.close()


def test_the_thread_survives_failures_in_setup_and_in_ticks():
    model = ScriptedJson(payload())
    conn, store, mstore, worker = make_worker(model, interval_sec=0.01)
    calls = {"ticks": 0}

    def bad_setup():
        raise RuntimeError("no fts5 here")

    def bad_tick(now=None):
        calls["ticks"] += 1
        raise RuntimeError("tick failed")

    mstore.enable_search_index = bad_setup
    worker.tick = bad_tick
    worker.start()
    deadline = 100
    while calls["ticks"] < 3 and deadline:
        threading.Event().wait(0.02)
        deadline -= 1
    worker.stop()
    assert calls["ticks"] >= 3 and not worker._thread.is_alive()      # it kept ticking after both errors
    conn.close()
