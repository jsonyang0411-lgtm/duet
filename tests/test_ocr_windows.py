"""OCR fallback on Windows: the engine's Python side and the WCDB backend's use of the fallback.

The PowerShell part (Windows.Media.Ocr) and PrintWindow can only run on Windows; here they are
replaced by fakes.  ``tools/ocr_check.py`` is the check to run on a real Windows PC.
"""

import os
import subprocess
import threading
import time

import pytest
from PIL import Image

from test_ocr_fallback import FakeEngine, FakeRunner, done, screen

from src.config import BotConfig
from src.wechat import wcdb_backend
from src.wechat.ocr import OcrFallback, OcrMessage
from src.wechat.ocr import win_ocr
from src.wechat.ocr.reader import clean_group_name
from src.wechat.ocr.win_ocr import WindowsOcrEngine, fix_spacing, rows_from_lines
from src.wechat.wcdb_backend import OCR_SENDER, WcdbBackend


def OcrMessage_for(title, text, is_group=True):
    return OcrMessage(title=title, title_clean=clean_group_name(title), text=text, is_group=is_group)


# ── text and rows ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,fixed", [
    ("你 好 世 界", "你好世界"),
    ("你 好，吗 ？", "你好，吗？"),
    ("ok 好的", "ok 好的"),
    ("你好 hello world", "你好 hello world"),
    ("  hi  there ", "hi  there"),
])
def test_spaces_between_chinese_characters_are_taken_out(raw, fixed):
    assert fix_spacing(raw) == fixed


def test_rows_are_relative_and_carry_the_bubble_colour():
    image = Image.new("RGB", (200, 100), (255, 255, 255))
    image.paste((149, 236, 105), (0, 40, 200, 60))                 # a green band: own bubbles
    rows = rows_from_lines([
        {"t": "你 好", "x": 50, "y": 10, "w": 40, "h": 10},
        {"t": "我的回复", "x": 100, "y": 45, "w": 60, "h": 10},
        {"t": "贴边", "x": 3, "y": 70, "w": 20, "h": 10},          # the sample point would be off the left edge
        {"t": "  ", "x": 1, "y": 1, "w": 1, "h": 1},
    ], image)
    assert [r["t"] for r in rows] == ["你好", "我的回复", "贴边"]
    first, second, third = rows
    assert (first["x0"], first["x1"], first["y0"], first["y1"]) == (0.25, 0.45, 0.1, 0.2)
    assert first["bg"] == [255, 255, 255] and second["bg"] == [149, 236, 105] and third["bg"] == [255, 255, 255]


def test_the_rows_read_like_a_wechat_window():
    """Own bubble green, other side white: the reader (made for the Mac) separates them on Windows colours too."""
    from src.wechat.ocr.reader import bubble_kind
    assert bubble_kind([149, 236, 105]) == "self" and bubble_kind([255, 255, 255]) == "other"


def test_the_script_is_ascii_and_uses_the_windows_ocr_api():
    assert win_ocr.SCRIPT.isascii()
    for needle in ("Windows.Media.Ocr.OcrEngine", "RecognizeAsync", "WEBOT_OCR_IMAGE", "NO_OCR_LANGUAGE",
                   "ConvertTo-Json"):
        assert needle in win_ocr.SCRIPT


def test_the_script_has_the_empty_lines_that_made_it_run_on_a_real_windows_pc():
    """Found on a real Windows 10/11 PC (PowerShell 5.1): ``powershell -Command -`` reads standard input like an
    interactive session, and a block that is still unfinished at the end of the input is dropped: the script
    printed nothing and exited with 0 (rows=0).  With an empty line after these six places it runs (0.5 s per
    look, 34 rows).  Which of them is the one that matters is not known, so all six are kept."""
    lines = win_ocr.SCRIPT.split("\n")

    def blank_after(index):
        return index + 1 < len(lines) and lines[index + 1].strip() == ""

    def first(prefix):
        return next(i for i, line in enumerate(lines) if line.startswith(prefix))

    function_end = next(i for i in range(first("function Await"), len(lines)) if lines[i] == "}")
    last_block_end = max(i for i, line in enumerate(lines) if line == "}")
    anchors = {
        "the type imports": first("$null = [Windows.Graphics.Imaging.SoftwareBitmap"),
        "the AsTask lookup": next(i for i, line in enumerate(lines) if line.rstrip().endswith("})[0]")),
        "function Await": function_end,
        "the no-language exit": first("if (-not $engine) { [Console]"),
        "the recognition": first("$result = Await"),
        "the last block": last_block_end,
    }
    missing = [name for name, index in anchors.items() if not blank_after(index)]
    assert missing == [], f"no empty line after: {missing}"
    assert win_ocr.SCRIPT.endswith("}\n\n")


# ── the engine ────────────────────────────────────────────────────────────

class FakeWindow:
    def __init__(self, hwnd=4242):
        self.hwnd = hwnd
        self.calls = []

    def find_hwnd(self, force=False, wake=True):
        self.calls.append(dict(force=force, wake=wake))
        return self.hwnd


def engine_with(runner, window=None, grabber=None):
    image = Image.new("RGB", (800, 600), (255, 255, 255))        # a main window: read at its own size
    return WindowsOcrEngine(window=window or FakeWindow(), runner=runner,
                            grabber=grabber or (lambda hwnd: image), powershell="powershell.exe")


class TestEngine:
    def test_a_small_window_is_read_at_twice_its_size(self):
        seen = {}

        def powershell(cmd, kwargs):
            seen["size"] = Image.open(kwargs["env"]["WEBOT_OCR_IMAGE"]).size
            return done('{"t": "添加到通讯录", "x": 200, "y": 600, "w": 200, "h": 40}\n')

        small = Image.new("RGB", (376, 568), (255, 255, 255))
        rows = engine_with(FakeRunner(**{"*": powershell}), grabber=lambda hwnd: small).capture(777)
        assert seen["size"] == (752, 1136)
        assert abs(rows[0]["y0"] - 600 / 1136) < 1e-6                      # fractions of the read picture

    def test_a_main_window_is_read_at_its_own_size(self):
        seen = {}

        def powershell(cmd, kwargs):
            seen["size"] = Image.open(kwargs["env"]["WEBOT_OCR_IMAGE"]).size
            return done("")

        big = Image.new("RGB", (996, 574), (255, 255, 255))
        engine_with(FakeRunner(**{"*": powershell}), grabber=lambda hwnd: big).capture()
        assert seen["size"] == (996, 574)

    def test_a_look_returns_rows_and_deletes_the_picture(self):
        seen = {}

        def powershell(cmd, kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            seen["image"] = kwargs["env"]["WEBOT_OCR_IMAGE"]
            assert os.path.exists(seen["image"])
            lines = ('\ufeff{"t": "你 好", "x": 80, "y": 30, "w": 80, "h": 20}\n'
                     "warning: not json\n"
                     '{"t": "在吗", "x": 40, "y": 90, "w": 80, "h": 20}\n')
            return done(lines)

        window = FakeWindow()
        rows = engine_with(FakeRunner(**{"*": powershell}), window).capture()
        assert [r["t"] for r in rows] == ["你好", "在吗"] and rows[0]["x0"] == 0.1
        assert window.calls == [dict(force=False, wake=False)]              # never wakes a tray window
        assert seen["cmd"] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"]
        assert seen["kwargs"]["input"] == win_ocr.SCRIPT                    # fed over stdin: no file, no policy change
        assert seen["kwargs"]["env"]["WEBOT_OCR_LANGS"] == win_ocr.LANGUAGES
        assert not os.path.exists(seen["image"])

    def test_no_chinese_recognizer_says_how_to_install_it(self):
        engine = engine_with(FakeRunner(**{"*": lambda cmd, kw: done("", "NO_OCR_LANGUAGE", 3)}))
        with pytest.raises(RuntimeError, match="Add-WindowsCapability"):
            engine.capture()

    def test_a_failing_script_is_an_error_with_its_message(self):
        engine = engine_with(FakeRunner(**{"*": lambda cmd, kw: done("", "boom: no such type", 1)}))
        with pytest.raises(RuntimeError, match="boom"):
            engine.capture()

    def test_no_wechat_window_is_an_error_the_fallback_can_handle(self):
        engine = engine_with(FakeRunner(), FakeWindow(hwnd=None))
        with pytest.raises(RuntimeError, match="not found"):
            engine.capture()

    def test_a_grab_error_still_leaves_no_picture_behind_and_runs_no_script(self):
        def grabber(hwnd):
            raise RuntimeError("WeChat is minimized")
        runner = FakeRunner()
        with pytest.raises(RuntimeError, match="minimized"):
            engine_with(runner, grabber=grabber).capture()
        assert runner.calls == []

    def test_the_script_failing_to_start_still_deletes_the_picture(self):
        paths = []

        def powershell(cmd, kwargs):
            paths.append(kwargs["env"]["WEBOT_OCR_IMAGE"])
            raise subprocess.TimeoutExpired(cmd, 30)

        with pytest.raises(subprocess.TimeoutExpired):
            engine_with(FakeRunner(**{"*": powershell})).capture()
        assert paths and not os.path.exists(paths[0])

    def test_prepare_needs_windows_and_powershell(self, monkeypatch):
        monkeypatch.setattr(win_ocr.sys, "platform", "darwin")
        assert WindowsOcrEngine().prepare() is False
        monkeypatch.setattr(win_ocr.sys, "platform", "win32")
        monkeypatch.setattr(win_ocr.shutil, "which", lambda name: None)
        assert WindowsOcrEngine().prepare() is False
        monkeypatch.setattr(win_ocr.shutil, "which", lambda name: r"C:\Windows\powershell.exe" if name == "powershell" else None)
        engine = WindowsOcrEngine()
        assert engine.prepare() is True and engine._powershell.endswith("powershell.exe")

    def test_the_engine_for_this_platform(self, monkeypatch):
        from src.wechat.ocr import engines
        monkeypatch.setattr(engines.sys, "platform", "win32")
        assert type(engines.default_engine(window="w")).__name__ == "WindowsOcrEngine"
        monkeypatch.setattr(engines.sys, "platform", "darwin")
        assert type(engines.default_engine()).__name__ == "MacVisionEngine"
        monkeypatch.setattr(engines.sys, "platform", "linux")
        with pytest.raises(RuntimeError):
            engines.default_engine()


# ── the WCDB backend ──────────────────────────────────────────────────────

def backend(groups=("测试群",), engine=None, **config):
    ocr = OcrFallback(engine or FakeEngine(screen("测试群（5）", ["之前的话"])), after=3, interval=0)
    be = WcdbBackend(bot_display_name="商务助手", groups=list(groups),
                     config=BotConfig(message_burst_enabled=False, **config), ocr=ocr)
    be._running = True
    return be, ocr


class FakeClient:
    """Enough of the WCDB client for _poll_group: one message per call."""
    _config = {"myWxid": "wxid_me"}

    def __init__(self):
        self.batches = []

    def get_messages(self, talker, limit=50):
        return self.batches.pop(0) if self.batches else []

    def resolve_nickname(self, wxid):
        return wxid

    def get_group_members(self, username):
        return []

    def get_display_names(self, usernames):
        return {u: u for u in usernames}


class TestBackendGlue:
    def test_off_by_default(self):
        be = WcdbBackend(bot_display_name="x", groups=["群"], config=BotConfig())
        assert be._ocr is None
        be = WcdbBackend(bot_display_name="x", groups=["群"],
                         config=BotConfig(ocr_fallback="on", ocr_fallback_after=2, ocr_poll_sec=9.0))
        assert be._ocr is not None and be._ocr._after == 2 and be._ocr._interval == 9.0

    def test_without_the_database_groups_get_ids_that_lead_back_to_their_names(self):
        be, _ = backend(groups=("测试群", "*", "游戏群"))
        be._register_groups_without_database()
        assert be._chat_names == {"ocr:测试群": "测试群", "ocr:游戏群": "游戏群"}
        assert be._talker_to_name("ocr:游戏群") == "游戏群" and be._chat_is_group["ocr:测试群"] is True

    def test_the_standard_message_has_every_key_of_a_database_message(self):
        be, _ = backend()
        be._client = FakeClient()
        be._register_groups_without_database()
        real = be._standardize({"sender_username": "wxid_u", "message_content": "你好", "local_type": 1,
                                "create_time": int(time.time()), "local_id": "m1"},
                               "测试群", "ocr:测试群", is_group=True)
        name, talker, ocr_msg = be._message_from_ocr(OcrMessage_for("测试群（5）", "@商务助手 你好"))
        assert (name, talker) == ("测试群", "ocr:测试群")
        assert set(ocr_msg) == set(real)
        assert ocr_msg["sender_id"] == ocr_msg["sender_name"] == OCR_SENDER
        assert ocr_msg["is_at_mentioned"] is True and ocr_msg["is_group"] is True and ocr_msg["msg_type"] == 1

    def test_which_chats_are_answered(self):
        be, _ = backend(groups=("测试群", "测试群二", "一个名字非常非常长的游戏交流群"))
        be._register_groups_without_database()
        assert be._message_from_ocr(OcrMessage_for("测试群（5）", "a"))[0] == "测试群"
        assert be._message_from_ocr(OcrMessage_for("测试群二（5）", "a"))[0] == "测试群二"      # not "测试群"
        assert be._message_from_ocr(OcrMessage_for("测 试 群（5）", "a"))[0] == "测试群"
        assert be._message_from_ocr(OcrMessage_for("一个名字非常非常长的…（8）", "a"))[0] == "一个名字非常非常长的游戏交流群"
        assert be._message_from_ocr(OcrMessage_for("别的群（5）", "a")) is None
        assert be._message_from_ocr(OcrMessage_for("测试群", "a", is_group=False)) is None     # a private chat
        assert be._message_from_ocr(OcrMessage_for("测试群（5）", "  ")) is None
        assert be._message_from_ocr(OcrMessage_for("测试…（5）", "a")) is None                # a cut-off title that is too short to tell

    def test_a_title_two_groups_have_is_not_guessed_and_private_chats_are_never_titles(self):
        be, _ = backend()
        be._add_talker("测试群", "room1@chatroom", is_group=True)
        be._add_talker("测试群", "room2@chatroom", is_group=True)     # both read (by their ids)
        be._add_talker("游戏群", "wxid_friend", is_group=False)       # a friend with a group's name
        assert be._message_from_ocr(OcrMessage_for("测试群（5）", "a")) is None    # which of the two?
        assert be._message_from_ocr(OcrMessage_for("游戏群（5）", "a")) is None
        be._add_talker("游戏群", "room3@chatroom", is_group=True)
        name, talker, _ = be._message_from_ocr(OcrMessage_for("游戏群（5）", "a"))
        assert (name, talker) == ("游戏群", "room3@chatroom")

    def test_a_cut_off_title_that_fits_two_groups_is_not_guessed(self):
        be, _ = backend(groups=("一个名字非常长的群甲", "一个名字非常长的群乙"))
        be._register_groups_without_database()
        assert be._message_from_ocr(OcrMessage_for("一个名字非常长的…（8）", "a")) is None

    def test_ocr_lines_go_straight_to_the_bot_not_through_the_burst_buffer(self):
        engine = FakeEngine(screen("测试群（5）", ["之前的话"]),
                            screen("测试群（5）", ["之前的话", "第一句", "第二句"]))
        be, ocr = backend(engine=engine)
        be._register_groups_without_database()
        for _ in range(3):
            ocr.note_db_failure()
        seen = []
        be._dispatch_message = lambda msg, callback: seen.append(msg["content"])
        be._poll_ocr(None)                                  # history
        be._poll_ocr(None)
        be._poll_ocr(None)                                  # the same screen: nothing new
        assert seen == ["第一句", "第二句"]                   # two lines, two messages, none held back

    def test_the_first_cycle_after_ocr_marks_the_backlog_seen(self):
        be, ocr = backend()
        be._client = FakeClient()
        be._chat_names = {"room1@chatroom": "测试群"}
        now = int(time.time())
        row = lambda i, text: {"sender_username": "wxid_u", "message_content": text, "local_type": 1,
                               "create_time": now, "local_id": i}
        seen = []
        be._dispatch_message = lambda msg, callback: seen.append(msg["content"])
        be._client.batches = [[row("m1", "断线期间的话")]]
        be._ocr_priming = True
        be._poll_group("测试群", "room1@chatroom", None)
        assert seen == []
        be._ocr_priming = False
        be._client.batches = [[row("m1", "断线期间的话"), row("m2", "恢复后的话")]]
        be._poll_group("测试群", "room1@chatroom", None)
        assert seen == ["恢复后的话"]                         # the backlog is not answered later either

    def test_a_poll_with_no_database_fails_loudly_so_the_loop_counts_it(self):
        be, _ = backend()
        be._client = None
        with pytest.raises(RuntimeError, match="not open"):
            be._poll_cycle(lambda msg: None)

    def test_sleep_reads_the_screen_only_while_the_fallback_is_on(self):
        engine = FakeEngine(screen("测试群（5）", ["a"]))
        be, ocr = backend(engine=engine)
        be._register_groups_without_database()
        be._sleep_polling_ocr(0.05, lambda msg: None)
        assert engine.looks == 0
        for _ in range(3):
            ocr.note_db_failure()
        be._sleep_polling_ocr(0.05, lambda msg: None)
        assert engine.looks >= 1

    def test_the_backoff_is_unchanged(self):
        assert [wcdb_backend._backoff_sec(n) for n in range(1, 7)] == [2, 4, 8, 16, 1, 2]


# ── the whole loop with a database that never opens ───────────────────────

def test_start_without_a_database_answers_from_the_screen(monkeypatch):
    class BrokenClient:
        def init(self):
            raise RuntimeError("wcdb_api.dll cannot be loaded")

        def close(self):
            pass

    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", BrokenClient)
    monkeypatch.setattr(wcdb_backend, "_backoff_sec", lambda n: 0.05)
    engine = FakeEngine(screen("测试群（5）", ["之前的话"]),
                        screen("测试群（5）", ["之前的话", "@商务助手 总结一下"]))
    be, ocr = backend(engine=engine)
    be._window.find_hwnd = lambda *a, **k: None
    be._window.describe_chat_windows = lambda *a, **k: None
    got = []
    done_event = threading.Event()

    def callback(msg):
        got.append(msg)
        done_event.set()
        return None

    thread = threading.Thread(target=be.start, args=(callback,), daemon=True)
    thread.start()
    assert done_event.wait(20), "the loop never read the screen"
    be.stop()
    thread.join(10)
    assert not thread.is_alive()
    assert got[0]["content"] == "@商务助手 总结一下" and got[0]["group_name"] == "测试群"
    assert got[0]["chat_id"] == "ocr:测试群" and got[0]["sender_id"] == OCR_SENDER


def test_start_without_a_database_and_without_ocr_still_stops_as_before(monkeypatch):
    class BrokenClient:
        def init(self):
            raise RuntimeError("wcdb_api.dll cannot be loaded")

    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", BrokenClient)
    be = WcdbBackend(bot_display_name="商务助手", groups=["测试群"], config=BotConfig())
    assert be._ocr is None
    be.start(lambda msg: None)                       # returns at once, nothing was started
    assert be._pool is None and not be._running
