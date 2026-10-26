"""tools/ocr_check.py: the check to run on a real Mac or Windows PC.  It must not print the chats by default."""

import importlib.util
import json
from pathlib import Path

import pytest

from test_ocr_fallback import FakeEngine, screen

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("ocr_check_tool", ROOT / "tools" / "ocr_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return module


def use_engine(tool, monkeypatch, engine):
    monkeypatch.setattr(tool, "default_engine", lambda window=None: engine)


SECRET = "我的银行卡密码是123456"


def test_by_default_it_prints_numbers_not_what_the_chats_say(tool, monkeypatch, capsys):
    use_engine(tool, monkeypatch, FakeEngine(screen("张三", ["你好", SECRET], mine=["收到"])))
    assert tool.main(["--looks", "2"]) == 0
    out = capsys.readouterr().out
    assert "look 1:" in out and "look 2:" in out and "rows=" in out and "other=2" in out and "self=1" in out
    assert SECRET not in out and "张三" not in out and "收到" not in out


def test_watch_prints_new_lines_of_the_named_group_only(tool, monkeypatch, capsys):
    engine = FakeEngine(
        screen("测试群（5）", ["之前的话"]),
        screen("测试群（5）", ["之前的话", "新消息一"]),
        screen("别的群（9）", ["别人的话"]),
        screen("别的群（9）", ["别人的话", SECRET]),
    )
    use_engine(tool, monkeypatch, engine)
    clock = iter(range(0, 1000))
    monkeypatch.setattr(tool.time, "monotonic", lambda: next(clock) / 10)
    assert tool.main(["--watch", "0.4", "--group", "测试群", "--interval", "0"]) == 0
    out = capsys.readouterr().out
    assert "NEW in 测试群: 新消息一" in out
    assert SECRET not in out and "别人的话" not in out        # the other group is counted, not shown
    assert "new=1" in out


def test_save_rows_writes_the_raw_rows(tool, monkeypatch, tmp_path, capsys):
    use_engine(tool, monkeypatch, FakeEngine(screen("测试群（5）", ["你好"])))
    target = tmp_path / "rows.json"
    assert tool.main(["--looks", "1", "--save-rows", str(target)]) == 0
    rows = json.loads(target.read_text(encoding="utf-8"))
    assert any(r["t"] == "你好" for r in rows)
    assert "contains the text" in capsys.readouterr().out


def test_an_empty_window_fails_with_a_hint(tool, monkeypatch, capsys):
    use_engine(tool, monkeypatch, FakeEngine([]))
    assert tool.main(["--looks", "1"]) == 1
    assert "no text on the screen" in capsys.readouterr().out


def test_a_missing_window_or_recognizer_fails_cleanly(tool, monkeypatch, capsys):
    engine = FakeEngine(screen("测试群（5）", ["a"]))
    engine.error = RuntimeError("WeChat's main window was not found")
    use_engine(tool, monkeypatch, engine)
    assert tool.main(["--looks", "1"]) == 1
    assert "FAIL: WeChat's main window was not found" in capsys.readouterr().out

    engine = FakeEngine(screen("测试群（5）", ["a"]))
    engine.prepare = lambda: False
    use_engine(tool, monkeypatch, engine)
    assert tool.main(["--looks", "1"]) == 1
    assert "recognizer is not available" in capsys.readouterr().out

    def no_engine(window=None):
        raise RuntimeError("no OCR engine for linux")
    monkeypatch.setattr(tool, "default_engine", no_engine)
    assert tool.main([]) == 1
    assert "no OCR engine" in capsys.readouterr().out


def test_a_window_too_small_to_show_the_title_is_explained(tool, monkeypatch, capsys):
    rows = [{"t": "字", "x0": 0.3, "x1": 0.4, "y0": 0.5, "y1": 0.52, "bg": [255, 255, 255]}]
    use_engine(tool, monkeypatch, FakeEngine(rows))
    assert tool.main(["--looks", "1"]) == 0
    out = capsys.readouterr().out
    assert "title=NO" in out and "make the window bigger" in out and "FRONT WeChat window" in out
