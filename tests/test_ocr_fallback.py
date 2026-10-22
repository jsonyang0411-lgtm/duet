"""OCR fallback (src/wechat/ocr): new-message tracking, the on/off logic, the macOS engine and the hybrid backend."""

import json
import logging
import os
import subprocess
from types import SimpleNamespace

import pytest

from test_macos_adaptation import FakeChatlogClient, FakeMacAutomation
from test_ocr_reader import generate_mock_wechat_ocr

from src.wechat.mac_hybrid_backend import OCR_SENDER, MacHybridBackend
from src.wechat.ocr import OcrFallback
from src.wechat.ocr.mac_vision import MacVisionEngine
from src.wechat.ocr.reader import NewMessageTracker, read_rows


def screen(title, others, mine=()):
    """The OCR rows of a window showing ``others`` (and then ``mine``) from the top down."""
    messages = [(text, "other", 0.15 + 0.1 * i) for i, text in enumerate(others)]
    messages += [(text, "self", 0.15 + 0.1 * (len(others) + i)) for i, text in enumerate(mine)]
    return generate_mock_wechat_ocr(title=title, messages=messages)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeEngine:
    """Shows each screen once, then the last one for good."""

    def __init__(self, *screens):
        self.screens = list(screens)
        self.looks = 0
        self.error = None
        self.prepared = 0

    def prepare(self):
        self.prepared += 1
        return True

    def capture(self):
        self.looks += 1
        if self.error:
            raise self.error
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]


# ── reading one screen ────────────────────────────────────────────────────

def test_read_rows_tells_the_two_sides_apart():
    info = read_rows(screen("测试群（3）", ["你好", "在吗"], mine=["在的"]))
    assert info["title"] == "测试群（3）" and info["title_clean"] == "测试群"
    assert info["other"] == ["你好", "在吗"] and info["self"] == ["在的"]


# ── which messages are new ────────────────────────────────────────────────

class TestTracker:
    def test_what_is_on_screen_at_the_first_look_is_history(self):
        assert NewMessageTracker().feed("群（3）", ["a", "b"]) == []

    def test_messages_below_the_old_ones_are_new(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["a", "b"])
        assert t.feed("群（3）", ["a", "b"]) == []
        assert t.feed("群（3）", ["a", "b", "c"]) == ["c"]

    def test_a_scrolled_window_still_continues_the_memory(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["a", "b", "c"])
        assert t.feed("群（3）", ["b", "c", "d", "e"]) == ["d", "e"]

    def test_a_repeated_text_is_still_news(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["哈哈", "好"])
        assert t.feed("群（3）", ["哈哈", "好", "哈哈"]) == ["哈哈"]

    def test_a_different_chat_starts_over(self):
        t = NewMessageTracker()
        t.feed("甲群（3）", ["a"])
        assert t.feed("乙群（5）", ["x", "y"]) == []
        assert t.feed("乙群（5）", ["x", "y", "z"]) == ["z"]

    def test_the_member_count_changing_is_the_same_chat(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["a"])
        assert t.feed("群（4）", ["a", "b"]) == ["b"]

    def test_scrolling_up_and_back_reports_nothing_twice(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["c", "d", "e"])
        assert t.feed("群（3）", ["a", "b", "c", "d"]) == []         # reading older messages
        assert t.feed("群（3）", ["c", "d", "e"]) == []              # back at the bottom
        assert t.feed("群（3）", ["c", "d", "e", "f"]) == ["f"]

    def test_a_screen_that_does_not_continue_is_not_news_but_cannot_stick(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["a", "b"])
        elsewhere = ["x", "y", "z"]
        assert [t.feed("群（3）", elsewhere) for _ in range(3)] == [[], [], []]   # the third look rebases
        assert t.feed("群（3）", elsewhere + ["w"]) == ["w"]

    def test_a_chat_without_messages_yet_reports_the_first_ones(self):
        t = NewMessageTracker()
        t.feed("群（3）", [])
        assert t.feed("群（3）", ["hello"]) == ["hello"]

    def test_ocr_reading_a_bubble_slightly_differently_is_not_news(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["今天晚上一起去吃火锅吧", "好呀"])
        assert t.feed("群（3）", ["今天晚上一起去吃火锅吧。", "好 呀"]) == []

    def test_punctuation_only_lines_are_ignored(self):
        t = NewMessageTracker()
        t.feed("群（3）", ["a"])
        assert t.feed("群（3）", ["a", "…", "b"]) == ["b"]


# ── when the fallback is on ───────────────────────────────────────────────

class TestFallback:
    def make(self, *screens, after=3, interval=4.0):
        clock = Clock()
        engine = FakeEngine(*screens)
        return OcrFallback(engine, after=after, interval=interval, clock=clock), engine, clock

    def test_it_stays_off_until_enough_reads_failed_in_a_row(self):
        fb, engine, _ = self.make(screen("群（3）", ["a"]))
        assert [fb.note_db_failure() for _ in range(3)] == [False, False, True]
        assert fb.active
        fb.note_db_ok()
        assert not fb.active
        assert [fb.note_db_failure() for _ in range(2)] == [False, False]      # the count started over

    def test_a_good_read_resets_the_count(self):
        fb, *_ = self.make(screen("群（3）", ["a"]))
        fb.note_db_failure(); fb.note_db_failure()
        fb.note_db_ok()
        assert fb.note_db_failure() is False

    def test_it_looks_only_while_on_and_not_more_often_than_the_interval(self):
        fb, engine, clock = self.make(screen("测试群（3）", ["a"]), screen("测试群（3）", ["a", "b"]))
        assert fb.poll() == [] and engine.looks == 0
        for _ in range(3):
            fb.note_db_failure()
        assert fb.poll() == []                          # the first look is history
        assert fb.poll() == [] and engine.looks == 1    # too soon
        clock.now += 4
        found = fb.poll()
        assert [(m.title_clean, m.text, m.is_group) for m in found] == [("测试群", "b", True)]

    def test_a_private_chat_title_has_no_member_count(self):
        fb, engine, clock = self.make(screen("张三", ["a"]), screen("张三", ["a", "b"]))
        for _ in range(3):
            fb.note_db_failure()
        fb.poll()
        clock.now += 4
        assert [m.is_group for m in fb.poll()] == [False]

    def test_turning_on_again_starts_from_what_is_on_screen(self):
        fb, engine, clock = self.make(screen("群（3）", ["a"]), screen("群（3）", ["a", "b"]))
        for _ in range(3):
            fb.note_db_failure()
        fb.poll()
        fb.note_db_ok()
        for _ in range(3):
            fb.note_db_failure()
        clock.now += 4
        assert fb.poll() == []                           # "b" is history now

    def test_a_missing_window_is_logged_once_and_does_not_raise(self, caplog):
        fb, engine, clock = self.make(screen("群（3）", ["a"]))
        engine.error = RuntimeError("no window")
        for _ in range(3):
            fb.note_db_failure()
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                assert fb.poll() == []
                clock.now += 4
        assert sum("cannot read the WeChat window" in r.message for r in caplog.records) == 1

    def test_an_empty_window_points_to_the_screen_recording_permission(self, caplog):
        fb, engine, clock = self.make([])
        for _ in range(3):
            fb.note_db_failure()
        with caplog.at_level(logging.WARNING):
            for _ in range(7):
                fb.poll()
                clock.now += 4
        assert sum("Screen Recording" in r.message for r in caplog.records) == 1

    def test_no_chat_open_reads_nothing(self):
        fb, engine, clock = self.make(generate_mock_wechat_ocr(title="", messages=[]))
        for _ in range(3):
            fb.note_db_failure()
        assert fb.poll() == []

    def test_warm_up_builds_the_engine_once_in_the_background(self):
        fb, engine, _ = self.make(screen("群（3）", ["a"]))
        fb.warm_up()
        fb.warm_up()
        fb._warm.join(5)
        assert engine.prepared == 1


# ── the macOS engine ──────────────────────────────────────────────────────

class FakeRunner:
    """Stands in for subprocess.run; ``handlers`` maps the program name to a function."""

    def __init__(self, **handlers):
        self.handlers = handlers
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        name = os.path.basename(cmd[0])
        handler = self.handlers.get(name) or self.handlers.get("*")
        return handler(cmd, kwargs)


def done(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class TestMacEngine:
    @pytest.fixture
    def source(self, tmp_path):
        path = tmp_path / "ocr.swift"
        path.write_text("print(1)", encoding="utf-8")
        return path

    def engine(self, source, tmp_path, runner):
        return MacVisionEngine(source=source, cache_dir=tmp_path / "cache", runner=runner)

    def test_the_recognizer_is_compiled_once_and_named_after_its_source(self, source, tmp_path, monkeypatch):
        monkeypatch.setattr("src.wechat.ocr.mac_vision.sys.platform", "darwin")
        monkeypatch.setattr("src.wechat.ocr.mac_vision.shutil.which", lambda name: "/usr/bin/swiftc")

        def swiftc(cmd, kwargs):
            out = cmd[cmd.index("-o") + 1]
            open(out, "w").write("binary")
            return done()

        runner = FakeRunner(swiftc=swiftc)
        engine = self.engine(source, tmp_path, runner)
        assert engine.prepare() is True and engine.prepare() is True
        assert len(runner.calls) == 1 and runner.calls[0][:2] == ["/usr/bin/swiftc", "-O"]
        built = engine.helper_path()
        assert built.exists() and os.access(built, os.X_OK) and not list(built.parent.glob("*.partial"))
        source.write_text("print(2)", encoding="utf-8")              # an edited source is another file
        assert self.engine(source, tmp_path, runner).helper_path() != built

    def test_no_swiftc_or_a_compile_error_is_reported_not_raised(self, source, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("src.wechat.ocr.mac_vision.sys.platform", "darwin")
        monkeypatch.setattr("src.wechat.ocr.mac_vision.shutil.which", lambda name: None)
        with caplog.at_level(logging.WARNING):
            assert self.engine(source, tmp_path, FakeRunner()).prepare() is False
        assert "xcode-select" in caplog.text
        monkeypatch.setattr("src.wechat.ocr.mac_vision.shutil.which", lambda name: "/usr/bin/swiftc")
        runner = FakeRunner(swiftc=lambda cmd, kw: done(stderr="error: boom", code=1))
        assert self.engine(source, tmp_path, runner).prepare() is False

    def test_it_only_works_on_macos(self, source, tmp_path, monkeypatch):
        monkeypatch.setattr("src.wechat.ocr.mac_vision.sys.platform", "win32")
        assert self.engine(source, tmp_path, FakeRunner()).prepare() is False

    def test_a_look_returns_the_rows_and_deletes_the_screenshot(self, source, tmp_path, monkeypatch):
        monkeypatch.setattr("src.wechat.ocr.mac_vision.sys.platform", "darwin")
        shots = []
        row = {"t": "你好", "x0": 0.3, "x1": 0.4, "y0": 0.4, "y1": 0.42, "bg": [255, 255, 255]}

        def helper(cmd, kwargs):
            if cmd[1] == "wid":
                return done("4242\n")
            assert os.path.exists(cmd[1])
            return done(json.dumps(row, ensure_ascii=False) + "\n\n")

        def screencapture(cmd, kwargs):
            assert cmd[:2] == ["screencapture", "-l"] and cmd[2] == "4242"
            shots.append(cmd[-1])
            open(cmd[-1], "wb").write(b"png")
            return done()

        engine = self.engine(source, tmp_path, FakeRunner(screencapture=screencapture, **{"*": helper}))
        engine._helper = str(tmp_path / "ocr-helper")
        assert engine.capture() == [row]
        assert len(shots) == 1 and not os.path.exists(shots[0])

    def test_no_wechat_window_is_an_error_the_fallback_can_handle(self, source, tmp_path):
        engine = self.engine(source, tmp_path, FakeRunner(**{"*": lambda cmd, kw: done("")}))
        engine._helper = "/x/ocr"
        with pytest.raises(RuntimeError, match="not found"):
            engine.capture()

    def test_a_failed_screenshot_still_deletes_the_temp_file(self, source, tmp_path):
        seen = []

        def screencapture(cmd, kwargs):
            seen.append(cmd[-1])
            raise subprocess.CalledProcessError(1, cmd)

        engine = self.engine(source, tmp_path, FakeRunner(screencapture=screencapture, **{"*": lambda c, k: done("7")}))
        engine._helper = "/x/ocr"
        with pytest.raises(subprocess.CalledProcessError):
            engine.capture()
        assert seen and not os.path.exists(seen[0])


# ── the hybrid backend ────────────────────────────────────────────────────

class DownClient(FakeChatlogClient):
    """The database read fails until ``up`` is set; then it returns ``batches``."""

    def __init__(self, batches=None):
        super().__init__(batches)
        self.up = False

    def get_new_messages(self, state=None, limit=200):
        if not self.up:
            raise RuntimeError("WeFlow database not found")
        return super().get_new_messages(state, limit)


def backend(client, engine, *, groups=("摸鱼群",), after=3):
    clock = Clock()
    ocr = OcrFallback(engine, after=after, interval=0, clock=clock)
    automation = FakeMacAutomation()
    return MacHybridBackend(
        bot_display_name="群聊小助手", groups=list(groups), poll_sec=0.01,
        client=client, automation=automation, ocr=ocr,
    ), automation, ocr


def fail(be, callback, times):
    for _ in range(times):
        be.poll_once(callback)


class TestBackend:
    def test_off_by_default(self):
        be = MacHybridBackend(client=DownClient(), automation=FakeMacAutomation(), config=SimpleNamespace())
        assert be._ocr is None and be.health_status() != "ocr_fallback"
        be.poll_once(lambda msg: pytest.fail("nothing to deliver"))
        be2 = MacHybridBackend(client=DownClient(), automation=FakeMacAutomation(),
                               config=SimpleNamespace(ocr_fallback="on", ocr_fallback_after=2, ocr_poll_sec=9))
        assert be2._ocr is not None and be2._ocr._after == 2 and be2._ocr._interval == 9.0

    def test_after_enough_failed_reads_the_open_group_is_read_and_answered(self):
        engine = FakeEngine(screen("摸鱼群（5）", ["之前的话"]),
                            screen("摸鱼群（5）", ["之前的话", "@群聊小助手 总结一下"]))
        be, automation, _ = backend(DownClient(), engine)
        seen = []
        callback = lambda msg: seen.append(msg) or "收到"
        fail(be, callback, 2)
        assert engine.looks == 0 and be.health_status() != "ocr_fallback"      # not yet
        fail(be, callback, 1)                                                   # on; first look = history
        assert seen == [] and be.health_status() == "ocr_fallback"
        be.poll_once(callback)
        be.poll_once(callback)                                                  # the same screen again
        assert len(seen) == 1
        msg = seen[0]
        assert msg["content"] == "@群聊小助手 总结一下" and msg["group_name"] == "摸鱼群"
        assert msg["is_group"] and msg["is_at_mentioned"] and msg["msg_type"] == 1
        assert msg["sender_id"] == msg["sender_name"] == OCR_SENDER
        assert msg["chat_id"] == "ocr:摸鱼群" and msg["message_id"].startswith("mac-ocr-")
        assert automation.opened == ["摸鱼群"] and automation.sent == ["收到"]   # sending is the normal path

    def test_a_known_group_keeps_its_real_chat_id(self):
        engine = FakeEngine(screen("摸鱼群（5）", ["a"]), screen("摸鱼群（5）", ["a", "b"]))
        be, *_ = backend(DownClient(), engine)
        be._remember_chat_session("room1@chatroom", "摸鱼群", True)
        seen = []
        fail(be, lambda msg: seen.append(msg), 5)
        assert [m["chat_id"] for m in seen] == ["room1@chatroom"]

    def test_two_groups_with_one_title_get_the_title_based_id(self):
        engine = FakeEngine(screen("摸鱼群（5）", ["a"]), screen("摸鱼群（5）", ["a", "b"]))
        be, *_ = backend(DownClient(), engine)
        be._remember_chat_session("room1@chatroom", "摸鱼群", True)
        be._remember_chat_session("room2@chatroom", "摸鱼群", True)
        seen = []
        fail(be, lambda msg: seen.append(msg), 5)
        assert [m["chat_id"] for m in seen] == ["ocr:摸鱼群"]

    def test_groups_that_are_not_monitored_and_private_chats_are_ignored(self):
        engine = FakeEngine(screen("别的群（5）", ["a"]), screen("别的群（5）", ["a", "b"]))
        be, *_ = backend(DownClient(), engine)
        seen = []
        fail(be, lambda msg: seen.append(msg), 6)
        assert seen == []
        engine = FakeEngine(screen("张三", ["a"]), screen("张三", ["a", "b"]))
        be, *_ = backend(DownClient(), engine, groups=())
        fail(be, lambda msg: seen.append(msg), 6)
        assert seen == []

    def test_the_bots_own_bubbles_are_never_read_as_messages(self):
        engine = FakeEngine(screen("摸鱼群（5）", ["a"]), screen("摸鱼群（5）", ["a"], mine=["我的回复"]))
        be, *_ = backend(DownClient(), engine)
        seen = []
        fail(be, lambda msg: seen.append(msg), 6)
        assert seen == []

    def test_when_the_database_is_back_its_backlog_is_history(self):
        engine = FakeEngine(screen("摸鱼群（5）", ["a"]), screen("摸鱼群（5）", ["a", "b"]))
        client = DownClient([{
            "count": 1, "new_state": {"room1@chatroom": 9},
            "messages": [{"timestamp": 5, "sender": "Alice", "type": "text", "content": "断线期间的话",
                          "local_id": 7, "chat": "摸鱼群", "username": "room1@chatroom", "is_group": True}],
        }, {
            "count": 1, "new_state": {"room1@chatroom": 10},
            "messages": [{"timestamp": 6, "sender": "Alice", "type": "text", "content": "恢复后的话",
                          "local_id": 8, "chat": "摸鱼群", "username": "room1@chatroom", "is_group": True}],
        }])
        be, *_ = backend(client, engine)
        seen = []
        fail(be, lambda msg: seen.append(msg["content"]), 5)
        assert seen == ["b"] and be.health_status() == "ocr_fallback"
        client.up = True
        be.poll_once(lambda msg: seen.append(msg["content"]))                   # the backlog: not delivered
        assert seen == ["b"] and be.health_status() != "ocr_fallback"
        be.poll_once(lambda msg: seen.append(msg["content"]))
        assert seen == ["b", "恢复后的话"]

    def test_with_the_database_up_nothing_changes(self):
        engine = FakeEngine(screen("摸鱼群（5）", ["a"]))
        client = DownClient([{
            "count": 1, "new_state": {"room1@chatroom": 9},
            "messages": [{"timestamp": 5, "sender": "Alice", "type": "text", "content": "你好",
                          "local_id": 7, "chat": "摸鱼群", "username": "room1@chatroom", "is_group": True}],
        }])
        client.up = True
        be, *_ = backend(client, engine)
        seen = []
        be.poll_once(lambda msg: seen.append(msg["content"]))
        assert seen == ["你好"] and engine.looks == 0

    def test_start_warms_the_engine_up(self):
        engine = FakeEngine(screen("摸鱼群（5）", ["a"]))
        be, automation, ocr = backend(DownClient(), engine)
        be._prime_message_state = lambda: be.stop()
        be.start(lambda msg: None)
        ocr._warm.join(5)
        assert engine.prepared == 1


# ── configuration and the console switch ──────────────────────────────────

class TestSettings:
    def test_defaults_and_validation(self):
        from src.config import BotConfig, _validate_config
        cfg = BotConfig()
        assert (cfg.ocr_fallback, cfg.ocr_fallback_after, cfg.ocr_poll_sec) == ("off", 3, 4.0)
        for kwargs in ({"ocr_fallback": "maybe"}, {"ocr_fallback_after": 0}, {"ocr_poll_sec": 0.5}):
            with pytest.raises(RuntimeError):
                _validate_config(kwargs)
        _validate_config({"ocr_fallback": "on", "ocr_fallback_after": 20, "ocr_poll_sec": 60})

    def test_the_environment_is_read(self):
        from unittest.mock import patch
        from src.config import load_config
        env = {"ANTHROPIC_API_KEY": "k", "OCR_FALLBACK": "true", "OCR_FALLBACK_AFTER": "5", "OCR_POLL_SEC": "2.5"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert (cfg.ocr_fallback, cfg.ocr_fallback_after, cfg.ocr_poll_sec) == ("on", 5, 2.5)

    def test_the_console_gets_a_switch_for_it(self):
        from src.web import advanced_config as adv
        group = next(g for g in adv.GROUPS if g["id"] == "ocr")
        fields = {f["key"]: f for f in group["fields"]}
        assert fields["OCR_FALLBACK"]["type"] == "onoff" and fields["OCR_FALLBACK"]["default"] == "off"
        assert fields["OCR_FALLBACK"]["platform_backends"] == ["mac_hybrid", "wcdb"]
        assert fields["OCR_FALLBACK_AFTER"]["requires"][0]["key"] == "OCR_FALLBACK"
        assert adv.validate_updates({"OCR_FALLBACK": "on"}, {}) == {}
        assert "OCR_FALLBACK_AFTER" in adv.validate_updates({"OCR_FALLBACK_AFTER": "0"}, {})
        # what the console receives: the value as the bot reads it, and the platform tag's text
        sent = next(g for g in adv.describe({"OCR_FALLBACK": "true"}, lambda v: v) if g["id"] == "ocr")
        shown = {f["key"]: f for f in sent["fields"]}["OCR_FALLBACK"]
        assert shown["value"] == "on" and shown["platform_label"] == "macOS / Windows"
