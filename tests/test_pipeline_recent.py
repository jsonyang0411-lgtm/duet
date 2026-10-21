"""The in-memory log of what the bot said lately."""

import threading

from src.pipeline.recent import MAX_CHATS, RecentReplyLog


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_recent_returns_the_last_replies_oldest_first():
    log = RecentReplyLog(clock=Clock())
    for text in ("一", "二", "三", "四"):
        log.add("g1", text)
    assert log.recent("g1", limit=3) == ("二", "三", "四")
    assert log.recent("g1", limit=1) == ("四",)
    assert log.recent("g1", limit=0) == ()
    assert log.recent("other") == ()


def test_blank_replies_and_chats_are_ignored():
    log = RecentReplyLog(clock=Clock())
    log.add("", "有话")
    log.add("g1", "   ")
    log.add("g1", None)
    assert log.recent("g1") == ()


def test_entries_expire_after_the_ttl():
    clock = Clock()
    log = RecentReplyLog(ttl_sec=60, clock=clock)
    log.add("g1", "旧的")
    clock.now += 61
    log.add("g1", "新的")
    assert log.recent("g1") == ("新的",)


def test_each_chat_keeps_only_its_newest_entries():
    log = RecentReplyLog(per_chat=3, clock=Clock())
    for index in range(10):
        log.add("g1", str(index))
    assert log.recent("g1", limit=10) == ("7", "8", "9")


def test_the_number_of_chats_is_bounded():
    log = RecentReplyLog(clock=Clock())
    for index in range(MAX_CHATS + 20):
        log.add(f"g{index}", "hi")
    assert log.recent("g0") == () and log.recent(f"g{MAX_CHATS + 19}") == ("hi",)


def test_the_log_is_only_touched_under_its_lock():
    class Counting:
        def __init__(self):
            self.taken = 0
            self._lock = threading.Lock()

        def __enter__(self):
            self._lock.acquire()
            self.taken += 1

        def __exit__(self, *exc):
            self._lock.release()

    log = RecentReplyLog(clock=Clock())
    log._lock = lock = Counting()
    log.add("g1", "好")
    log.recent("g1")
    assert lock.taken == 2


def test_concurrent_writers_do_not_lose_the_structure():
    log = RecentReplyLog(clock=Clock())

    def work(name):
        for index in range(200):
            log.add(name, f"{name}-{index}")
            log.recent(name)

    threads = [threading.Thread(target=work, args=(f"g{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(len(log.recent(f"g{i}", limit=10)) == 6 for i in range(8))


def test_only_the_first_line_of_a_long_reply_is_kept():
    log = RecentReplyLog(clock=Clock())
    log.add("g1", "🔮 塔罗三张牌\n\n过去：愚者\n现在：高塔\n未来：太阳")
    log.add("g1", "好" * 300)
    log.add("g1", "\n\n")
    first, second = log.recent("g1", limit=5)
    assert first == "🔮 塔罗三张牌" and second == "好" * 80
