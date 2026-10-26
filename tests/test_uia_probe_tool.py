"""tools/uia_probe.py: the read-only UI Automation look at WeChat's windows (the Windows calls are faked)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN, DIALOG = 100, 200


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("uia_probe_tool", ROOT / "tools" / "uia_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def control(kind, name="", auto_id="", cls="", rect=(0, 0, 10, 10)):
    return SimpleNamespace(ControlTypeName=kind, Name=name, AutomationId=auto_id, ClassName=cls,
                           BoundingRectangle=SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3]))


def tree(*items):
    """A walker over [(control, depth), ...] that records the arguments it was called with."""
    calls = []

    def walker(root, **kwargs):
        calls.append(kwargs)
        return iter(items)
    walker.calls = calls
    return walker


def install(tool, monkeypatch, trees, main=(MAIN, 7)):
    """trees: {hwnd: walker}; the windows are the main one and a dialog."""
    windows = [{"hwnd": MAIN, "class": "Qt", "title": "微信", "width": 1000, "height": 800},
               {"hwnd": DIALOG, "class": "Qt", "title": "添加朋友", "width": 420, "height": 560}]
    monkeypatch.setattr(tool, "find_main_window", lambda: main)
    monkeypatch.setattr(tool, "windows_of_process", lambda pid: windows)
    monkeypatch.setattr(tool, "open_uia", lambda: (lambda hwnd: hwnd, lambda root, **kw: trees[root](root, **kw)))


def test_describe_shows_type_name_id_class_and_rectangle(tool):
    line = tool.describe(control("ButtonControl", "添加到通讯录", "btn_add", "QPushButton", (5, 6, 50, 60)), 2)
    assert line == "    ButtonControl name='添加到通讯录' id='btn_add' class='QPushButton' rect=5,6,50,60"


def test_describe_survives_a_control_that_cannot_be_read(tool):
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("gone")
    assert tool.describe(Broken(), 0).startswith("? name=''")


def test_long_names_are_cut(tool):
    assert len(tool.describe(control("TextControl", "字" * 500), 0)) < 200


def test_walk_counts_named_buttons_and_inputs_and_honours_the_limit(tool):
    items = [(control("WindowControl", "添加朋友"), 0), (control("PaneControl", "Weixin"), 1),
             (control("ButtonControl", "确定"), 2), (control("EditControl", "  "), 2)]
    lines, total, usable = tool.walk("root", 6, 3, tree(*items))
    assert (total, usable) == (3, 1)                                 # windows and panes do not count
    assert lines[-1].startswith("... stopped at 3 controls")
    assert lines[1].startswith("  PaneControl")                      # indented by depth


def test_a_blank_named_input_is_not_usable(tool):
    _, total, usable = tool.walk("root", 6, 10, tree((control("EditControl", " "), 0), (control("ButtonControl"), 1)))
    assert (total, usable) == (2, 0)


def test_walk_asks_for_the_top_control_and_the_depth(tool):
    walker = tree()
    tool.walk("root", 4, 10, walker)
    assert walker.calls == [{"includeTop": True, "maxDepth": 4}]


@pytest.mark.parametrize("total, usable, expect", [
    (1, 0, "nothing below the window"),
    (30, 0, "only windows and panes"),
    (30, 12, "12 named button / input / menu item controls of 30"),
])
def test_verdict(tool, total, usable, expect):
    assert expect in tool.verdict(total, usable)


def test_what_the_real_wechat_4_dialogs_showed_is_not_called_promising(tool):
    """Round 2 on a real machine: the window, a 2 px pane and the render surface, all with names."""
    real = tree((control("WindowControl", "添加朋友", cls="Qt51514QWindowIcon"), 0),
                (control("PaneControl", "Weixin", cls="Qt51514QWindowIcon", rect=(2276, 1588, 2278, 1590)), 1),
                (control("PaneControl", "MMUIRenderSubWindowHW", cls="MMUIRenderSubWindowHW"), 1))
    lines, total, usable = tool.walk("root", 6, 400, real)
    assert (total, usable) == (3, 0)
    assert "only windows and panes" in tool.verdict(total, usable)


def test_the_main_window_is_skipped_by_default(tool, monkeypatch, capsys):
    secret = "张三：我的银行卡密码是123456"
    install(tool, monkeypatch, {
        MAIN: tree((control("WindowControl", "微信"), 0), (control("TextControl", secret), 1)),
        DIALOG: tree((control("WindowControl", "添加朋友"), 0), (control("EditControl", "微信号/手机号"), 1)),
    })
    assert tool.main([]) == 0
    out = capsys.readouterr().out
    assert "title='添加朋友'" in out and "微信号/手机号" in out
    assert secret not in out and f"HWND {MAIN}  class" not in out


def test_main_flag_includes_the_main_window(tool, monkeypatch, capsys):
    install(tool, monkeypatch, {MAIN: tree((control("WindowControl", "微信"), 0)),
                                DIALOG: tree((control("WindowControl", "添加朋友"), 0))})
    tool.main(["--main"])
    assert f"--- HWND {MAIN}" in capsys.readouterr().out


def test_report_goes_to_the_file_too(tool, monkeypatch, tmp_path, capsys):
    install(tool, monkeypatch, {DIALOG: tree((control("WindowControl", "添加朋友"), 0))})
    target = tmp_path / "probe.txt"
    tool.main(["--out", str(target)])
    assert "添加朋友" in target.read_text(encoding="utf-8")


def test_a_window_uia_cannot_read_is_reported_not_fatal(tool, monkeypatch, capsys):
    def broken(root, **kw):
        raise OSError("access denied")
    install(tool, monkeypatch, {DIALOG: broken})
    assert tool.main([]) == 0
    assert "could not read this window: access denied" in capsys.readouterr().out


def test_no_other_window_open_says_what_to_do(tool, monkeypatch, capsys):
    install(tool, monkeypatch, {})
    monkeypatch.setattr(tool, "windows_of_process", lambda pid: [
        {"hwnd": MAIN, "class": "Qt", "title": "微信", "width": 1000, "height": 800}])
    tool.main([])
    assert "open the 添加朋友 window first" in capsys.readouterr().out


def test_no_wechat_window(tool, monkeypatch, capsys):
    monkeypatch.setattr(tool, "find_main_window", lambda: None)
    monkeypatch.setattr(tool, "open_uia", lambda: (None, None))
    assert tool.main([]) == 1
    assert "main window was not found" in capsys.readouterr().out


def test_without_windows_libraries_it_explains_instead_of_crashing(tool, monkeypatch, capsys):
    def no_win32():
        raise ImportError("No module named 'win32process'")
    monkeypatch.setattr(tool, "find_main_window", no_win32)
    assert tool.main([]) == 2
    assert "needs Windows" in capsys.readouterr().out
