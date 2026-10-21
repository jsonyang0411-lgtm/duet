"""Several bubbles from one reply (src/send/bubbles.py)."""

import threading
import time

import pytest

from src.send.bubbles import (
    BUBBLE_SEP, MAX_BUBBLES, BubbleSender, join_bubbles, plain_text, split_bubbles, typing_delay,
)


def test_parts_are_joined_without_ever_containing_the_separator():
    assert join_bubbles(["好呀", " 一起去 "]) == f"好呀{BUBBLE_SEP}一起去"
    assert join_bubbles([f"a{BUBBLE_SEP}b", "", "  ", "c"]) == f"ab{BUBBLE_SEP}c"
    assert join_bubbles([]) == "" and join_bubbles(["x"]) == "x"


def test_a_reply_splits_into_its_non_empty_bubbles():
    assert split_bubbles(f"好呀{BUBBLE_SEP}一起去") == ["好呀", "一起去"]
    assert split_bubbles("一句话") == ["一句话"]
    assert split_bubbles(f"{BUBBLE_SEP} {BUBBLE_SEP}") == [] and split_bubbles("") == [] and split_bubbles(None) == []


def test_more_than_the_limit_is_merged_into_the_last_bubble():
    reply = join_bubbles(["一", "二", "三", "四", "五"])
    assert split_bubbles(reply) == ["一", "二", "三 四 五"] and len(split_bubbles(reply)) == MAX_BUBBLES
    assert split_bubbles(reply, limit=1) == ["一 二 三 四 五"]


def test_plain_text_is_the_reply_on_one_line():
    assert plain_text(f"好呀{BUBBLE_SEP}一起去") == "好呀 一起去" and plain_text("单条") == "单条"


def test_the_typing_delay_grows_with_the_text_and_is_capped():
    assert typing_delay("") == pytest.approx(0.4)
    assert typing_delay("好呀") < typing_delay("好呀" * 10) <= 3.5
    assert typing_delay("字" * 1000) == 3.5


class Recorder:
    def __init__(self, results=None):
        self.sent, self.results = [], list(results or [])

    def __call__(self, text):
        self.sent.append(text)
        return self.results.pop(0) if self.results else True


def sender(**kw):
    sleeps = []
    return BubbleSender(sleep=sleeps.append, **kw), sleeps


def test_a_single_bubble_is_one_plain_call_without_a_pause():
    s, sleeps = sender()
    send = Recorder()
    assert s.deliver("chat", "好呀", send) is True
    assert send.sent == ["好呀"] and sleeps == []


def test_several_bubbles_are_sent_in_order_with_a_pause_before_each_later_one():
    s, sleeps = sender()
    send = Recorder()
    assert s.deliver("chat", join_bubbles(["好呀", "我们周末一起去爬山吧"]), send)
    assert send.sent == ["好呀", "我们周末一起去爬山吧"]
    assert sleeps == [typing_delay("我们周末一起去爬山吧")]


def test_typing_mode_also_waits_before_the_first_bubble():
    s, sleeps = sender(typing=True)
    send = Recorder()
    s.deliver("chat", "好呀", send)
    assert sleeps == [typing_delay("好呀")] and send.sent == ["好呀"]


def test_a_failed_bubble_ends_the_reply_and_reports_failure():
    s, sleeps = sender()
    send = Recorder([True, False, True])
    assert s.deliver("chat", join_bubbles(["一", "二", "三"]), send) is False
    assert send.sent == ["一", "二"]
    assert s.deliver("chat", "单条", Recorder([False])) is False


def test_an_empty_reply_sends_nothing():
    s, _ = sender()
    send = Recorder()
    assert s.deliver("chat", BUBBLE_SEP, send) is False and send.sent == []


def test_bubbles_of_one_chat_are_not_interleaved_but_other_chats_are_not_blocked():
    s = BubbleSender(sleep=lambda seconds: time.sleep(0.02))
    order, lock = [], threading.Lock()

    def make(tag):
        def send(text):
            with lock:
                order.append((tag, text))
            return True
        return send

    threads = [
        threading.Thread(target=s.deliver, args=("A", join_bubbles(["a1", "a2"]), make("first"))),
        threading.Thread(target=s.deliver, args=("A", join_bubbles(["b1", "b2"]), make("second"))),
        threading.Thread(target=s.deliver, args=("B", join_bubbles(["c1", "c2"]), make("other"))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    chat_a = [text for tag, text in order if tag in ("first", "second")]
    assert chat_a in (["a1", "a2", "b1", "b2"], ["b1", "b2", "a1", "a2"])
    assert {text for tag, text in order if tag == "other"} == {"c1", "c2"}


def test_the_chat_lock_table_is_bounded():
    s, _ = sender()
    for i in range(700):
        s._chat_lock(f"chat{i}")
    assert len(s._locks) == 500


# ── review fixes ────────────────────────────────────────────────────────────

def test_the_separator_is_an_escape_in_the_source_not_an_invisible_character():
    import pathlib
    import src.send.bubbles as module
    assert BUBBLE_SEP == "⁣" and BUBBLE_SEP != ""
    assert "⁣" not in pathlib.Path(module.__file__).read_text(encoding="utf-8")


def test_strip_separators_removes_every_one():
    from src.send.bubbles import strip_separators
    assert strip_separators(f"a{BUBBLE_SEP}b{BUBBLE_SEP}") == "ab" and strip_separators(None) == ""


def test_a_stopped_backend_sends_nothing_more():
    s, sleeps = sender()
    send = Recorder()
    assert s.deliver("chat", join_bubbles(["一", "二"]), send, alive=lambda: False) is False
    assert send.sent == [] and sleeps == []


def test_a_backend_stopped_during_the_pause_skips_the_next_bubble():
    state = {"on": True}
    sent = []
    s = BubbleSender(sleep=lambda seconds: state.update(on=False))
    assert s.deliver("chat", join_bubbles(["一", "二", "三"]), lambda t: sent.append(t) or True,
                     alive=lambda: state["on"]) is False
    assert sent == ["一"]


def test_a_single_plain_reply_is_sent_even_if_alive_says_no():
    # the caller (the backend) checks before it asks for a reply; the one call is the old path
    s, _ = sender()
    send = Recorder()
    assert s.deliver("chat", "好呀", send, alive=lambda: False) is True and send.sent == ["好呀"]


def test_the_chat_lock_table_drops_the_oldest_chat_and_keeps_a_recent_one():
    s, _ = sender()
    kept = s._chat_lock("chat0")
    for i in range(1, 500):
        s._chat_lock(f"chat{i}")
    assert s._chat_lock("chat0") is kept                    # touching it makes it the most recent
    s._chat_lock("chat500")
    assert "chat0" in s._locks and "chat1" not in s._locks and len(s._locks) == 500
