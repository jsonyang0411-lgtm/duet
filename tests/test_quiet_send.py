"""SEND_QUIET: wait for the user to pause, type into a chat's own window, put focus and clipboard back."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.config import BotConfig, load_config
from src.wechat import window_controller as wc
from src.wechat.quiet_send import (
    IdleGate,
    QuietSendSettings,
    Win32Desktop,
    keep_clipboard_format,
    title_names_chat,
)
from src.wechat.window_controller import WeChatWindowController, WindowSnapshot

MAIN, OWN, USER, ELSEWHERE = 100, 500, 900, 777
CTRL = 0x11
SAVED = [(13, "用户复制的".encode("utf-16-le"))]


class FakeDesktop:
    def __init__(self, *, idle_ms=10_000, clipboard=SAVED, processed=True, forward_works=True):
        self.foreground = USER
        self.tick = 5_000_000
        self.last_input = self.tick - idle_ms
        self.clipboard = clipboard
        self.sequences = []           # values clipboard_sequence() returns in turn, then 7
        self.processed = processed
        self.forward_works = forward_works
        self.forwarded, self.written, self.pings, self.reads = [], [], [], 0

    def foreground_window(self):
        return self.foreground

    def is_window(self, hwnd):
        return bool(hwnd)

    def bring_forward(self, hwnd):
        self.forwarded.append(hwnd)
        if self.forward_works:
            self.foreground = hwnd
        return self.forward_works

    def last_input_tick(self):
        return self.last_input

    def tick_count(self):
        return self.tick

    def wait_until_processed(self, hwnd):
        self.pings.append(hwnd)
        return self.processed and bool(hwnd)

    def clipboard_sequence(self):
        return self.sequences.pop(0) if self.sequences else 7

    def read_clipboard(self):
        self.reads += 1
        return list(self.clipboard) if self.clipboard is not None else None

    def write_clipboard(self, formats):
        self.written.append(formats)
        return True


@pytest.fixture
def rig(monkeypatch):
    """A quiet controller over fake windows: main window 100, the group's own window 500."""
    desktop = FakeDesktop()
    windows = {MAIN: "微信", OWN: "读书群"}
    processes = {}
    gui = MagicMock(name="win32gui")
    gui.GetWindowText.side_effect = lambda h: windows.get(h, "")
    gui.IsWindowVisible.return_value = True
    gui.IsIconic.return_value = False
    gui.EnumWindows.side_effect = lambda callback, ctx: [callback(h, ctx) for h in list(windows)]
    gui.GetWindowRect.return_value = (0, 0, 600, 800)
    process = MagicMock(name="win32process")
    process.GetWindowThreadProcessId.side_effect = lambda h: (0, h)
    monkeypatch.setattr(wc, "win32gui", gui)
    monkeypatch.setattr(wc, "win32process", process)
    monkeypatch.setattr(wc, "_get_process_name", lambda pid: processes.get(pid, "weixin.exe"))
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)

    controller = WeChatWindowController(quiet=QuietSendSettings(enabled=True), desktop=desktop)
    keys, clip = [], []
    monkeypatch.setattr(wc, "send_combo", lambda mod, key: keys.append(("combo", mod, key)))
    monkeypatch.setattr(wc, "press_key", lambda key: keys.append(("key", key)))
    monkeypatch.setattr(controller, "_set_clipboard", lambda text, private=False: clip.append((text, private)))
    monkeypatch.setattr(controller, "_validate_hwnd", lambda h: h in windows)
    monkeypatch.setattr(controller, "_foreground_matches", lambda h: desktop.foreground == h)
    monkeypatch.setattr(controller, "_current_wechat_foreground_hwnd",
                        lambda: desktop.foreground if desktop.foreground in windows else None)
    monkeypatch.setattr(controller, "_capture_window_state", lambda h: WindowSnapshot(hwnd=h, was_visible=True))
    monkeypatch.setattr(controller, "_looks_like_blank_window", lambda h: False)
    monkeypatch.setattr(controller, "_log_failure", MagicMock())
    activate = MagicMock(return_value=False)
    monkeypatch.setattr(controller, "activate", activate)
    return SimpleNamespace(controller=controller, desktop=desktop, windows=windows, keys=keys,
                           clip=clip, gui=gui, processes=processes, activate=activate)


# ── settings ────────────────────────────────────────────────────────────────

def load(**env):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", **env}, clear=True):
        return load_config()


def test_quiet_sending_is_off_by_default_and_validated():
    config = BotConfig()
    assert (config.send_quiet, config.send_idle_sec, config.send_idle_max_wait_sec,
            config.send_keep_clipboard) == ("off", 3.0, 30.0, "on")
    loaded = load()
    assert (loaded.send_quiet, loaded.send_idle_sec, loaded.send_idle_max_wait_sec,
            loaded.send_keep_clipboard) == ("off", 3.0, 30.0, "on")
    on = load(SEND_QUIET="true", SEND_IDLE_SEC="1.5", SEND_IDLE_MAX_WAIT_SEC="0", SEND_KEEP_CLIPBOARD="no")
    assert (on.send_quiet, on.send_idle_sec, on.send_idle_max_wait_sec, on.send_keep_clipboard) == (
        "on", 1.5, 0.0, "off")
    for env, message in (
        ({"SEND_QUIET": "maybe"}, "SEND_QUIET"),
        ({"SEND_KEEP_CLIPBOARD": "later"}, "SEND_KEEP_CLIPBOARD"),
        ({"SEND_IDLE_SEC": "-1"}, "SEND_IDLE_SEC"),
        ({"SEND_IDLE_MAX_WAIT_SEC": "601"}, "SEND_IDLE_MAX_WAIT_SEC"),
    ):
        with pytest.raises(RuntimeError, match=message):
            load(**env)


def test_settings_from_config():
    assert QuietSendSettings.from_config(None) == QuietSendSettings()
    assert QuietSendSettings.from_config(None).enabled is False
    settings = QuietSendSettings.from_config(BotConfig(send_quiet="on", send_idle_sec=2.0,
                                                       send_idle_max_wait_sec=9.0, send_keep_clipboard="off"))
    assert settings == QuietSendSettings(enabled=True, idle_sec=2.0, idle_max_wait_sec=9.0, keep_clipboard=False)


def test_the_default_controller_changes_nothing():
    controller = WeChatWindowController()
    assert controller._quiet is None and controller._desktop is None and controller._idle_gate is None
    off = WeChatWindowController(quiet=QuietSendSettings(enabled=False))
    assert off._quiet is None and off._desktop is None
    with patch.object(controller, "_send_to_chat", return_value=True) as send, \
         patch.object(controller, "_begin_quiet") as begin:
        assert controller.send_to_chat("群", "hi")
    send.assert_called_once_with("群", "hi", 2, file_path=None)
    begin.assert_not_called()


def test_the_windows_calls_load_only_when_used():
    Win32Desktop()                      # no ctypes.WinDLL needed until a call is made


def test_the_wcdb_backend_passes_the_settings_and_its_running_flag():
    from src.wechat.wcdb_backend import WcdbBackend
    backend = WcdbBackend(groups=["读书群"], config=BotConfig(send_quiet="on", send_idle_sec=1.0))
    window = backend._window
    assert window._quiet == QuietSendSettings(enabled=True, idle_sec=1.0)
    assert window.alive() is False
    backend._running = True
    assert window.alive() is True
    assert WcdbBackend(groups=["读书群"], config=BotConfig())._window._quiet is None


# ── pure helpers ────────────────────────────────────────────────────────────

def test_a_window_title_names_a_chat():
    assert title_names_chat("读书群", "读书群")
    assert title_names_chat(" 读书群 ", "读书群")
    assert title_names_chat("读书群(23)", "读书群")
    assert title_names_chat("读书群 （23）", "读书群")
    assert not title_names_chat("读书群2", "读书群")
    assert not title_names_chat("读书群(23)人", "读书群")
    assert not title_names_chat("微信", "读书群")
    assert not title_names_chat("", "读书群")
    assert not title_names_chat("读书群", "")
    assert title_names_chat("a.b(c)", "a.b(c)") and not title_names_chat("axb(c)", "a.b(c)")


def test_only_memory_formats_are_copied():
    assert keep_clipboard_format(13) and keep_clipboard_format(8) and keep_clipboard_format(15)
    assert keep_clipboard_format(0xC0A1)                   # a registered format
    for gdi in (2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E, 0x200, 0x2FF, 0x300, 0x3FF):
        assert not keep_clipboard_format(gdi)


def test_the_message_area_of_an_own_window_has_no_chat_list():
    main = wc._conversation_bbox(0, 0, 1000, 800)
    own = wc._conversation_bbox(0, 0, 1000, 800, sidebar=False)
    assert main == (220, 64, 980, 616)
    assert own == (20, 64, 980, 616)


# ── waiting for a pause ─────────────────────────────────────────────────────

class Clock:
    def __init__(self, desktop, user_stops_at=None):
        self.now = 0.0
        self.desktop = desktop
        self.user_stops_at = user_stops_at
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.desktop.tick += int(seconds * 1000)
        if self.user_stops_at is None or self.now < self.user_stops_at:
            self.desktop.last_input = self.desktop.tick   # still typing


def gate(desktop, clock, idle=3.0, max_wait=30.0):
    return IdleGate(desktop, idle, max_wait, sleep=clock.sleep, clock=clock)


def test_an_idle_user_is_not_waited_for():
    desktop = FakeDesktop(idle_ms=3_500)
    clock = Clock(desktop)
    assert gate(desktop, clock).wait() is True
    assert clock.sleeps == []


def test_a_typing_user_is_waited_for_until_the_pause():
    desktop = FakeDesktop(idle_ms=0)
    clock = Clock(desktop, user_stops_at=5.0)
    assert gate(desktop, clock).wait() is True
    assert 7.7 <= clock.now <= 8.5                          # last key just before 5 s, then 3 s of quiet


def test_a_user_who_never_pauses_gets_the_message_after_the_longest_wait():
    desktop = FakeDesktop(idle_ms=0)
    clock = Clock(desktop)
    assert gate(desktop, clock, max_wait=10.0).wait() is True
    assert 10.0 <= clock.now <= 10.3


def test_no_wait_when_switched_off_or_unknown():
    desktop = FakeDesktop(idle_ms=0)
    clock = Clock(desktop)
    assert gate(desktop, clock, idle=0).wait() is True
    desktop.last_input = None
    assert gate(desktop, clock).wait() is True
    assert clock.sleeps == []


def test_the_bots_own_keystrokes_do_not_delay_the_next_send():
    desktop = FakeDesktop(idle_ms=0)
    clock = Clock(desktop)
    idle = gate(desktop, clock)
    idle.mark_own_input()                                   # our keystrokes were the last input
    assert idle.wait() is True and clock.sleeps == []
    desktop.tick += 10
    desktop.last_input = desktop.tick                       # the user moves the mouse
    assert idle.idle_seconds() == 0


def test_the_tick_counter_wrapping_around_is_harmless():
    desktop = FakeDesktop()
    desktop.last_input, desktop.tick = 0xFFFFFF00, 0x00000F00
    assert IdleGate(desktop, 3, 30).idle_seconds() == pytest.approx((0xF00 + 0x100) / 1000)


def test_a_stopped_bot_stops_waiting(rig):
    rig.desktop.last_input = rig.desktop.tick
    rig.controller.alive = lambda: False
    assert rig.controller.send_to_chat("读书群", "你好") is False
    assert rig.keys == [] and rig.desktop.forwarded == [] and rig.desktop.reads == 0


# ── sending ─────────────────────────────────────────────────────────────────

def test_a_group_in_its_own_window_is_sent_without_any_search(rig):
    with patch.object(rig.controller, "find_hwnd", side_effect=AssertionError("no main window")):
        assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.keys == [("combo", CTRL, 0x41), ("combo", CTRL, 0x56), ("key", 0x0D)]
    assert rig.clip == [("你好", True)]                     # kept out of the clipboard history
    assert rig.desktop.forwarded == [OWN, USER]             # in, and back to the user's window
    assert rig.desktop.pings == [OWN]
    assert rig.desktop.written == [SAVED]                   # the user's clipboard is back
    rig.activate.assert_not_called()                        # no Alt tap
    assert OWN in rig.controller._chat_windows


def test_without_its_own_window_the_main_window_search_is_used_without_the_alt_tap(rig):
    del rig.windows[OWN]
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.keys == [
        ("combo", CTRL, 0x32),                                   # Ctrl+2 contacts
        ("combo", CTRL, 0x46), ("combo", CTRL, 0x41), ("combo", CTRL, 0x56), ("key", 0x0D),  # search
        ("combo", CTRL, 0x56), ("key", 0x0D),                    # paste and send
    ]
    assert rig.clip == [("读书群", True), ("你好", True)]
    assert rig.desktop.forwarded == [MAIN, USER]
    assert rig.desktop.written == [SAVED]
    rig.activate.assert_not_called()


def test_the_alt_tap_activation_is_only_the_fallback(rig):
    rig.desktop.forward_works = False

    def activate(hwnd):
        rig.desktop.foreground = hwnd
        return True

    rig.activate.side_effect = activate
    assert rig.controller.send_to_chat("读书群", "你好") is True
    rig.activate.assert_called_once_with(OWN)


def test_a_minimized_own_window_is_restored_and_minimized_again(rig, monkeypatch):
    rig.gui.IsIconic.side_effect = lambda h: h == OWN
    restore = MagicMock()
    monkeypatch.setattr(rig.controller, "_capture_window_state",
                        lambda h: WindowSnapshot(hwnd=h, was_visible=True, was_iconic=True))
    monkeypatch.setattr(rig.controller, "_restore_window_state", restore)
    assert rig.controller.send_to_chat("读书群", "你好") is True
    rig.gui.ShowWindow.assert_any_call(OWN, wc.win32con.SW_RESTORE)
    restore.assert_called_once_with(WindowSnapshot(hwnd=OWN, was_visible=True, was_iconic=True), OWN)


def test_losing_the_window_before_the_paste_types_nothing_and_retries(rig, monkeypatch):
    calls = []

    def set_clipboard(text, private=False):
        calls.append(text)
        if len(calls) == 1:
            rig.desktop.foreground = ELSEWHERE                 # the user clicked away
    monkeypatch.setattr(rig.controller, "_set_clipboard", set_clipboard)
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.keys == [("combo", CTRL, 0x41), ("combo", CTRL, 0x56), ("key", 0x0D)]  # once
    rig.controller._log_failure.assert_called_once_with("读书群", "你好", "chat window send failed", OWN)


def test_a_window_of_another_program_or_a_hidden_one_is_not_a_chat_window(rig):
    rig.processes[OWN] = "notepad.exe"
    assert rig.controller.find_chat_window("读书群") is None
    rig.processes.clear()
    rig.gui.IsWindowVisible.return_value = False
    assert rig.controller.find_chat_window("读书群") is None
    rig.gui.IsIconic.return_value = True                    # minimized still counts
    assert rig.controller.find_chat_window("读书群") == OWN
    assert rig.controller.find_chat_window("微信") is None   # the main window's own title


def test_a_chat_window_is_never_taken_for_the_main_window(rig):
    rig.controller._cached_hwnd = OWN
    assert rig.controller.find_chat_window("读书群") == OWN
    assert rig.controller._cached_hwnd is None
    rig.desktop.foreground = OWN
    assert rig.controller._adopt_foreground_hwnd(MAIN, "test") == MAIN
    assert rig.controller._cached_hwnd is None


# ── giving things back ──────────────────────────────────────────────────────

def test_focus_stays_where_the_user_went_meanwhile(rig, monkeypatch):
    def press(key):
        rig.keys.append(("key", key))
        rig.desktop.foreground = ELSEWHERE                    # the user clicked another program
    monkeypatch.setattr(wc, "press_key", press)
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.desktop.forwarded == [OWN]


def test_the_clipboard_is_left_alone_when_someone_else_changed_it(rig):
    rig.desktop.sequences = [7, 8]                         # ours after the write, then a newer copy
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.desktop.written == []


def test_the_clipboard_is_left_alone_when_wechat_does_not_answer(rig):
    rig.desktop.processed = False
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.desktop.written == []                       # WeChat might still paste it
    assert rig.desktop.forwarded == [OWN, USER]            # the focus still comes back


def test_with_keep_clipboard_off_the_clipboard_is_not_read(rig):
    rig.controller._quiet = QuietSendSettings(enabled=True, keep_clipboard=False)
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.desktop.reads == 0 and rig.desktop.written == []


def test_an_unreadable_clipboard_is_not_restored(rig):
    rig.desktop.clipboard = None
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.desktop.written == []


def test_an_empty_clipboard_is_emptied_again(rig):
    rig.desktop.clipboard = []
    assert rig.controller.send_to_chat("读书群", "你好") is True
    assert rig.desktop.written == [[]]


def test_a_failed_send_still_gives_everything_back(rig, monkeypatch):
    monkeypatch.setattr(rig.controller, "_foreground_matches", lambda h: False)
    rig.desktop.forward_works = False
    assert rig.controller.send_to_chat("读书群", "你好") is False
    assert rig.keys == [] and rig.clip == []
    assert rig.desktop.foreground == USER                  # the focus never left
    assert rig.desktop.written == []                       # nothing was written, nothing to put back
    assert rig.controller._session is None
    assert rig.activate.call_count == 2                    # the Alt fallback, once per attempt


def test_private_clipboard_text_opts_out_of_the_history(monkeypatch):
    clipboard = MagicMock(name="win32clipboard")
    clipboard.RegisterClipboardFormat.side_effect = lambda name: {"Exclude": 1, "CanInclude": 2}.get(name[:7], 3)
    monkeypatch.setattr(wc, "win32clipboard", clipboard)
    WeChatWindowController._set_clipboard("你好")
    clipboard.SetClipboardData.assert_not_called()
    WeChatWindowController._set_clipboard("你好", private=True)
    names = [c.args[0] for c in clipboard.RegisterClipboardFormat.call_args_list]
    assert names == ["ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory",
                     "CanUploadToCloudClipboard"]
    assert clipboard.SetClipboardData.call_count == 3
    assert clipboard.SetClipboardText.call_count == 2


# ── screenshots for the vision fallback ─────────────────────────────────────

def test_a_screenshot_of_an_own_window_skips_the_search_and_the_missing_chat_list(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(wc, "VISION_SCREENSHOT_DIR", tmp_path)
    grab = MagicMock()
    monkeypatch.setattr(wc.ImageGrab, "grab", grab)
    with patch.object(rig.controller, "navigate_to_chat", side_effect=AssertionError("no search")):
        path = rig.controller.capture_chat_media("读书群")
    assert path.startswith(str(tmp_path))
    grab.assert_called_once_with(bbox=wc._conversation_bbox(0, 0, 600, 800, sidebar=False))
    assert rig.desktop.forwarded == [OWN, USER]
    assert rig.keys == []


def test_the_log_says_which_groups_have_their_own_window(rig, caplog):
    caplog.set_level("INFO", logger=wc.__name__)
    rig.controller.describe_chat_windows(["读书群", "饭搭子"])
    text = caplog.text
    assert "'读书群' is open in its own window" in text
    assert "'饭搭子' has no window of its own" in text
    WeChatWindowController().describe_chat_windows(["读书群"])   # quiet off: silent, no scan
