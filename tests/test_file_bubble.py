"""Finding a received file in the WeChat window from OCR rows (src/wechat/file_bubble.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

from src.wechat import file_bubble as F


def row(text, x0=0.5, y0=0.5, x1=None, y1=None):
    return {"t": text, "x0": x0, "x1": x1 if x1 is not None else x0 + 0.2, "y0": y0,
            "y1": y1 if y1 is not None else y0 + 0.03}


@pytest.mark.parametrize("text, ok", [
    ("项目资料备份2026.zip", True),
    ("项目资料备份 2026.zip", True),                     # OCR put a space in
    ("项目资料...2026.zip", True),                       # cut in the middle
    ("项目资料…2026.zip", True),
    ("项目资料备份", True),                               # the first line of a wrapped name
    ("项目资", False),                                   # too short to tell
    ("别的文件...2026.zip", False),
    ("项目资料...2025.zip", False),
    ("1.2 GB", False),
    ("", False),
])
def test_a_row_that_shows_the_name(text, ok):
    assert F.name_matches(text, "项目资料备份2026.zip") is ok


def test_only_the_file_name_counts():
    assert F.name_matches("report.pdf", r"C:\files\report.pdf") is True
    assert F.name_matches("report.pdf", "") is False


def test_the_newest_bubble_in_the_conversation_comes_first():
    rows = [row("报告.pdf", x0=0.1, y0=0.6),           # the chat list on the left
            row("报告.pdf", y0=0.3),
            row("报告.pdf", y0=0.7),
            row("1.2GB", y0=0.74)]
    found = F.file_rows(rows, "报告.pdf", min_x0=F.CONVERSATION_X0)
    assert [r["y0"] for r in found] == [0.7, 0.3]
    near = F.rows_near(rows, found[0])
    assert [r["t"] for r in near] == ["报告.pdf", "1.2GB"]


def test_the_save_as_item_of_a_menu():
    menu = [row("复制"), row("转发"), row("另存为"), row("删除")]
    assert F.menu_item(menu)["t"] == "另存为"
    assert F.menu_item([row("保存"), row("另 存 为")])["t"] == "另 存 为"      # the first label wins
    assert F.menu_item([row("另存为图片的说明")]) is None
    assert F.centre(row("x", x0=0.2, y0=0.4, x1=0.4, y1=0.5)) == (pytest.approx(0.3), pytest.approx(0.45))


def test_the_probe_says_what_it_needs_off_windows(capsys, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("on Windows the probe would look at the real WeChat window")
    spec = importlib.util.spec_from_file_location("file_probe", Path(__file__).resolve().parents[1]
                                                  / "tools/file_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--file", "a.zip"]) == 2
    assert "needs Windows" in capsys.readouterr().out
