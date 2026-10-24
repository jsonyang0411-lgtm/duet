"""Finding a file someone sent in the WeChat window, from OCR rows (WeChat 4, Windows).

WeChat downloads a received file by itself only up to a size (1 GB at most); a
bigger one waits for a click.  A left click on a file bubble can OPEN the file
(and a downloaded program would run), so the bot never left-clicks a file: it
right-clicks the bubble and chooses 另存为 (save as) in the menu.  These helpers
read OCR rows (``{"t", "x0", "x1", "y0", "y1"}``, fractions of the window, see
``ocr/win_ocr.py``); ``tools/file_probe.py`` uses them to look first.
"""

from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Iterable, Optional

ELLIPSIS = re.compile(r"\.{2,}|…+")
SAVE_AS_LABELS = ("另存为", "另存", "保存到电脑", "保存")
CONVERSATION_X0 = 0.30          # in the main window, the chat list ends about here


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def name_matches(text: str, name: str) -> bool:
    """Whether an OCR row shows the file name: whole, or cut in the middle ("长文件名...结尾.zip"),
    or the first line of a name that wraps (at least 6 characters of its start)."""
    text, name = _squash(text), _squash(PureWindowsPath(name).name)    # \ and / both split
    if not text or not name:
        return False
    if name in text:
        return True
    parts = [p for p in ELLIPSIS.split(text) if p]
    if len(parts) >= 2 and ELLIPSIS.search(text):
        head, tail = parts[0], parts[-1]
        return len(head) >= 2 and name.startswith(head) and name.endswith(tail)
    return len(text) >= 6 and name.startswith(text)


def file_rows(rows: Iterable[dict], name: str, min_x0: float = 0.0) -> list[dict]:
    """The rows that show ``name``, lowest (newest message) first."""
    found = [r for r in rows if r.get("x0", 0.0) >= min_x0 and name_matches(r.get("t", ""), name)]
    return sorted(found, key=lambda r: r.get("y0", 0.0), reverse=True)


def rows_near(rows: Iterable[dict], row: dict, span: float = 0.08) -> list[dict]:
    """The rows around ``row`` (the rest of the bubble: size, status), top to bottom."""
    top, bottom = row["y0"] - span, row["y1"] + span
    left = row["x0"] - 0.15
    near = [r for r in rows if r.get("y1", 0) >= top and r.get("y0", 1) <= bottom and r.get("x1", 0) >= left]
    return sorted(near, key=lambda r: (r.get("y0", 0.0), r.get("x0", 0.0)))


def menu_item(rows: Iterable[dict], labels: tuple = SAVE_AS_LABELS) -> Optional[dict]:
    """The menu row for the first of ``labels`` that is there (exact text, spaces ignored)."""
    rows = list(rows)
    for label in labels:
        for row in rows:
            if _squash(row.get("t", "")) == label:
                return row
    return None


def centre(row: dict) -> tuple[float, float]:
    return (row["x0"] + row["x1"]) / 2, (row["y0"] + row["y1"]) / 2
