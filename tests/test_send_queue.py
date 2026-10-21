"""SEND_QUEUE: one thread sends every reply, in order per chat, interleaved across chats."""

import json
import os
import threading
import time
from unittest.mock import patch

import pytest
from PIL import Image

from test_wcdb_backend_locking import _Client, _message

from src.config import BotConfig, load_config
from src.send.bubbles import BUBBLE_SEP, configure_sender, join_bubbles
from src.send.queue import SendQueue
from src.send.stickers import StickerLibrary, configure_stickers, sticker_token
from src.wechat.wcdb_backend import WcdbBackend


@pytest.fixture(autouse=True)
def _clean_globals():
    configure_sender(typing=False, sleep=lambda s: None)
    configure_stickers(StickerLibrary())
    yield
    configure_sender(typing=False)
    configure_stickers(StickerLibrary())


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def drain(queue, clock, step=0.1, limit=1000):
    """Run the queue on a fake clock until it is empty."""
    for _ in range(limit):
        if not queue.pending():
            return
        if not queue.run_once():
            clock.now += step
    raise AssertionError("queue did not drain")


def recorder(log, label=""):
    return lambda text: log.append((label, text)) or True


def test_a_plain_reply_is_sent_exactly_as_written():
    clock, sent, done = Clock(), [], []
    queue = SendQueue(clock=clock)
    queue.submit("a", "  两行\n文字  ", recorder(sent), on_done=done.append)
    assert queue.run_once() and sent == [("", "  两行\n文字  ")] and done == [True]
    assert queue.pending() == 0 and queue.run_once() is False


def test_one_chat_is_in_order_and_another_chat_slips_into_a_typing_pause():
    clock, sent = Clock(), []
    queue = SendQueue(clock=clock)
    queue.submit("a", BUBBLE_SEP.join(["a1", "a2 这一句比较长一点"]), recorder(sent, "a"))
    queue.submit("a", "a3", recorder(sent, "a"))                     # waits for the first reply of a
    queue.submit("b", "b1", recorder(sent, "b"))
    drain(queue, clock)
    assert [text for _, text in sent] == ["a1", "b1", "a2 这一句比较长一点", "a3"]


def test_typing_mode_waits_before_the_first_bubble_too():
    clock, sent = Clock(), []
    queue = SendQueue(clock=clock, typing=True)
    queue.submit("a", "好呀", recorder(sent))
    assert queue.run_once() is False                                  # not due yet
    clock.now += 0.4 + 0.09 * 2
    assert queue.run_once() and sent == [("", "好呀")]


def test_a_failed_bubble_ends_its_reply_and_the_next_reply_goes_on():
    clock, sent, done = Clock(), [], []
    queue = SendQueue(clock=clock)

    def fail_second(text):
        sent.append(text)
        return text != "二"

    queue.submit("a", BUBBLE_SEP.join(["一", "二", "三"]), fail_second, on_done=done.append)
    queue.submit("a", "四", fail_second, on_done=done.append)
    drain(queue, clock)
    assert sent == ["一", "二", "四"] and done == [False, True]


def test_a_send_that_raises_is_a_failure_not_a_dead_queue():
    clock, done = Clock(), []
    queue = SendQueue(clock=clock)

    def boom(text):
        raise RuntimeError("window gone")

    queue.submit("a", "x", boom, on_done=done.append)
    queue.submit("b", "y", lambda t: True, on_done=done.append)
    drain(queue, clock)
    assert done == [False, True]


def test_stickers_go_through_send_file_and_a_skipped_one_is_not_a_failure(tmp_path):
    folder = tmp_path / "s"
    folder.mkdir()
    Image.new("RGB", (2, 2)).save(folder / "a.png")
    (folder / "stickers.json").write_text(json.dumps({"stickers": [{"name": "问号", "file": "a.png"}]}),
                                          encoding="utf-8")
    configure_stickers(StickerLibrary.load(folder))
    clock, sent, files, done = Clock(), [], [], []
    queue = SendQueue(clock=clock)
    reply = join_bubbles(["好呀", sticker_token("问号")])
    queue.submit("a", reply, recorder(sent), send_file=lambda p: files.append(p) or True,
                 on_done=lambda ok: done.append(("a", ok)))
    queue.submit("b", reply, recorder(sent), on_done=lambda ok: done.append(("b", ok)))  # cannot send pictures
    queue.submit("c", sticker_token("问号"), recorder(sent),
                 on_done=lambda ok: done.append(("c", ok)))                           # nothing it could send
    drain(queue, clock)
    assert len(files) == 1 and [t for _, t in sent] == ["好呀", "好呀"]
    assert sorted(done) == [("a", True), ("b", True), ("c", False)]


def test_a_stopped_backend_drops_what_is_left():
    clock, sent, done = Clock(), [], []
    alive = [True]
    queue = SendQueue(clock=clock, alive=lambda: alive[0])
    queue.submit("a", BUBBLE_SEP.join(["一", "二"]), recorder(sent), on_done=done.append)
    assert queue.run_once()
    alive[0] = False
    clock.now += 10
    assert queue.run_once() and queue.pending() == 0
    assert [t for _, t in sent] == ["一"] and done == []


def test_an_empty_reply_is_not_queued():
    queue = SendQueue(clock=Clock())
    assert queue.submit("a", BUBBLE_SEP, lambda t: True) is False and queue.pending() == 0


def test_the_thread_sends_and_stops():
    sent, done = [], threading.Event()
    queue = SendQueue()
    queue.start()
    try:
        queue.submit("a", "一", lambda t: sent.append(t) or True)
        queue.submit("b", "二", lambda t: sent.append(t) or True, on_done=lambda ok: done.set())
        assert done.wait(5)
        assert sorted(sent) == ["一", "二"]
    finally:
        queue.stop()
    queue.submit("a", "三", lambda t: sent.append(t) or True)
    time.sleep(0.05)
    assert "三" not in sent                                            # stopped: nothing is sent


# ── the backend and the settings ────────────────────────────────────────────

def backend(**config):
    b = WcdbBackend(bot_display_name="小助手", groups=["测试群"], config=BotConfig(**config))
    b._client = _Client()
    b._running = True
    return b


def test_with_the_queue_the_worker_only_hands_the_reply_over(caplog):
    b = backend(send_queue="on")
    sent = []
    b._send_and_confirm = lambda group, talker, text: sent.append(text) or True
    b._handle_message("测试群", "room1@chatroom", _message(), lambda msg: BUBBLE_SEP.join(["好呀", "走"]))
    assert sent == [] and b._send_queue.pending() == 1                # nothing sent on the worker
    caplog.set_level("INFO")
    while b._send_queue.pending():
        b._send_queue.run_once()
    assert sent == ["好呀", "走"] and "Reply sent: group='测试群'" in caplog.text


def test_without_the_queue_nothing_changes():
    b = backend()
    assert b._send_queue is None
    sent = []
    b._send_and_confirm = lambda group, talker, text: sent.append(text) or True
    b._handle_message("测试群", "room1@chatroom", _message(), lambda msg: "好呀")
    assert sent == ["好呀"]


def test_the_queue_follows_the_typing_setting_and_the_backend_lifecycle():
    configure_sender(typing=True)
    b = backend(send_queue="on")
    assert b._send_queue.typing is True
    b._send_queue.start()
    b.stop()
    assert b._send_queue._thread is None


def test_the_setting_is_off_by_default_and_validated():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        assert load_config().send_queue == "off"
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "SEND_QUEUE": "yes"}, clear=True):
        assert load_config().send_queue == "on"
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "SEND_QUEUE": "later"}, clear=True):
        with pytest.raises(RuntimeError, match="SEND_QUEUE"):
            load_config()


# ── review findings ─────────────────────────────────────────────────────────

def test_after_stop_nothing_is_accepted_and_the_backend_says_so(caplog):
    queue = SendQueue(clock=Clock())
    queue.stop()
    assert queue.submit("a", "x", lambda t: True) is False and queue.pending() == 0
    b = backend(send_queue="on")
    b._send_queue.stop()
    caplog.set_level("INFO")
    b._handle_message("测试群", "room1@chatroom", _message(), lambda msg: "好呀")
    assert "Reply FAILED: group='测试群'" in caplog.text


def test_typing_pauses_between_two_replies_of_one_chat():
    clock, times = Clock(), []
    queue = SendQueue(clock=clock, typing=True)
    queue.submit("a", "第一句", lambda t: times.append((t, clock.now)) or True)
    queue.submit("a", "第二句", lambda t: times.append((t, clock.now)) or True)
    drain(queue, clock)
    (first, t1), (second, t2) = times
    assert t2 - t1 >= 0.4 + 0.09 * 3 - 0.11                    # typed after the first, not alongside


def test_stop_drops_what_is_left_and_wakes_the_thread_at_once():
    queue = SendQueue()
    queue.start()
    started = time.monotonic()
    queue.stop()
    assert time.monotonic() - started < 1.0
    queue2 = SendQueue(clock=Clock(), typing=True)
    queue2.submit("a", "x", lambda t: True)
    queue2.stop()
    assert queue2.pending() == 0


def test_a_reply_dropped_during_its_send_is_left_alone():
    clock, done = Clock(), []
    queue = SendQueue(clock=clock)
    queue.submit("a", BUBBLE_SEP.join(["一", "二"]), lambda t: queue._drop("test") or True, on_done=done.append)
    assert queue.run_once() is True and queue.pending() == 0 and done == []
    queue.submit("a", "一", lambda t: queue._drop("test") or True, on_done=done.append)    # its last bubble
    assert queue.run_once() is True and queue.pending() == 0 and done == []


def test_a_raising_bubble_ends_its_reply():
    clock, sent = Clock(), []

    def send(text):
        if text == "一":
            raise RuntimeError("boom")
        sent.append(text)
        return True

    queue = SendQueue(clock=clock)
    queue.submit("a", BUBBLE_SEP.join(["一", "二"]), send)
    drain(queue, clock)
    assert sent == []


def test_a_broken_on_done_does_not_stop_the_queue():
    clock, sent = Clock(), []
    queue = SendQueue(clock=clock)

    def broken(ok):
        raise ValueError("log failed")

    queue.submit("a", "一", recorder(sent), on_done=broken)
    queue.submit("b", "二", recorder(sent))
    drain(queue, clock)
    assert [t for _, t in sent] == ["一", "二"]


def test_never_two_sending_threads():
    release, entered = threading.Event(), threading.Event()
    queue = SendQueue()
    queue.start()
    queue.submit("a", "x", lambda t: entered.set() or release.wait(5) or True)
    assert entered.wait(5)
    queue.stop(timeout=0.05)                                  # the send is still running
    old = queue._thread
    assert old is not None and old.is_alive()
    queue.start()
    assert queue._thread is old                               # no second thread
    release.set()
    old.join(5)


def test_the_backend_starts_the_queue_and_keeps_four_workers():
    configure_sender(typing=True)
    b = backend(send_queue="on")
    b._start_workers()
    try:
        assert b._pool._max_workers == 4 and b._send_queue._thread.is_alive()
    finally:
        b._pool.shutdown(wait=False)
        b._send_queue.stop()
    plain = backend()
    plain._start_workers()
    try:
        assert plain._pool._max_workers == 8
    finally:
        plain._pool.shutdown(wait=False)


def test_the_backend_sends_stickers_through_the_queue(tmp_path):
    folder = tmp_path / "s"
    folder.mkdir()
    Image.new("RGB", (2, 2)).save(folder / "a.png")
    (folder / "stickers.json").write_text(json.dumps({"stickers": [{"name": "问号", "file": "a.png"}]}),
                                          encoding="utf-8")
    configure_stickers(StickerLibrary.load(folder))
    b = backend(send_queue="on")
    files = []
    b._send_and_confirm = lambda group, talker, text: True
    b._window.send_file_to_chat = lambda group, path: files.append(path) or True
    b._handle_message("测试群", "room1@chatroom", _message(), lambda msg: join_bubbles(["好", sticker_token("问号")]))
    while b._send_queue.pending():
        b._send_queue.run_once()
    assert files == [str((folder / "a.png").resolve())]
