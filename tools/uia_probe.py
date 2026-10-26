"""tools/uia_probe.py: a read-only look at what WeChat's windows tell UI Automation (Windows only).

Friend management clicks a few WeChat PC 4.0 controls by position (the (+) button, the input
boxes, sometimes the confirm button) because OCR cannot read them.  If those Qt windows expose
their controls through UI Automation, a click by position can become "find the control called X".
This tool answers that.  It lists the windows of WeChat's process and prints their control
trees.  It never clicks or types.

    1. Open the window to look at (the 添加朋友 window, then the 申请添加朋友 one).
    2. python tools/uia_probe.py                 # the windows of WeChat other than the main one
       python tools/uia_probe.py --main          # the main window too
       python tools/uia_probe.py --depth 8 --limit 600 --out probe.txt

The main window is skipped by default: its tree holds contact and chat names.  Control names can
contain whatever is on the screen, so read the output before you pass it on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MIN_SIZE = 40            # px; Qt keeps many tiny helper windows around
NAME_CHARS = 60
# What friend management would click or type into; windows and panes are only structure.
USABLE_TYPES = frozenset({
    "ButtonControl", "SplitButtonControl", "EditControl", "MenuItemControl", "ListItemControl",
    "CheckBoxControl", "RadioButtonControl", "HyperlinkControl", "ComboBoxControl", "TabItemControl",
})


def describe(control, depth: int) -> str:
    """One line for a control: type, name, automation id, class, rectangle."""
    def attr(name, default=""):
        try:
            return getattr(control, name)
        except Exception:
            return default

    rect = attr("BoundingRectangle", None)
    where = f"{rect.left},{rect.top},{rect.right},{rect.bottom}" if hasattr(rect, "left") else "?"
    return (f"{'  ' * depth}{attr('ControlTypeName') or '?'} name={str(attr('Name'))[:NAME_CHARS]!r} "
            f"id={attr('AutomationId')!r} class={attr('ClassName')!r} rect={where}")


def walk(root, max_depth: int, limit: int, walker) -> tuple[list[str], int, int]:
    """(lines, controls seen, named buttons / inputs / menu items) for the tree below ``root``."""
    lines: list[str] = []
    seen = usable = 0
    for control, depth in walker(root, includeTop=True, maxDepth=max_depth):
        if seen >= limit:
            lines.append(f"... stopped at {limit} controls (--limit)")
            break
        lines.append(describe(control, depth))
        seen += 1
        try:
            usable += control.ControlTypeName in USABLE_TYPES and bool(str(control.Name).strip())
        except Exception:
            pass
    return lines, seen, usable


def verdict(total: int, usable: int) -> str:
    if total <= 1:
        return "UI Automation shows nothing below the window itself: its controls cannot be found by name."
    if usable == 0:
        return (f"{total} controls, but only windows and panes, no named button, input or menu item: "
                "the window draws its own content and UI Automation cannot find anything to click by name.")
    return f"{usable} named button / input / menu item controls of {total}: friend management could click these by name."


# ── Windows only ────────────────────────────────────────────────────────

def find_main_window() -> tuple[int, int] | None:
    """(hwnd, pid) of WeChat's main window, or None."""
    import win32process
    from src.wechat.window_controller import WeChatWindowController
    hwnd = WeChatWindowController().find_hwnd(wake=False)
    return (hwnd, win32process.GetWindowThreadProcessId(hwnd)[1]) if hwnd else None


def windows_of_process(pid: int) -> list[dict]:
    """The visible top-level windows of a process: hwnd, class, title, width, height."""
    import win32gui
    import win32process
    found: list[dict] = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right - left >= MIN_SIZE and bottom - top >= MIN_SIZE:
                found.append({"hwnd": hwnd, "class": win32gui.GetClassName(hwnd),
                              "title": win32gui.GetWindowText(hwnd),
                              "width": right - left, "height": bottom - top})
        return True

    win32gui.EnumWindows(collect, None)
    return found


def open_uia():
    """(ControlFromHandle, WalkControl) of the uiautomation package."""
    import uiautomation
    return uiautomation.ControlFromHandle, uiautomation.WalkControl


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only UI Automation look at WeChat's windows")
    parser.add_argument("--main", action="store_true", help="also dump the main window (names of contacts and chats)")
    parser.add_argument("--depth", type=int, default=6, help="how deep below each window (default 6)")
    parser.add_argument("--limit", type=int, default=400, help="controls per window at most (default 400)")
    parser.add_argument("--out", metavar="FILE", help="also write the report to this file")
    args = parser.parse_args(argv)

    try:
        main_window = find_main_window()
        control_from_handle, walker = open_uia()
    except ImportError as e:
        print(f"This tool needs Windows with pywin32 and uiautomation ({e}).")
        return 2
    if main_window is None:
        print("WeChat's main window was not found: start WeChat and open the window to look at.")
        return 1
    main_hwnd, pid = main_window

    out = [f"WeChat main window: HWND {main_hwnd} (pid {pid})"]
    windows = [w for w in windows_of_process(pid) if args.main or w["hwnd"] != main_hwnd]
    if not windows:
        out.append("No other WeChat window is open: open the 添加朋友 window first (or use --main).")
    for w in windows:
        out.append("")
        out.append(f"--- HWND {w['hwnd']}  class={w['class']!r}  title={w['title']!r}  size={w['width']}x{w['height']}")
        try:
            lines, total, usable = walk(control_from_handle(w["hwnd"]), args.depth, args.limit, walker)
        except Exception as e:
            out.append(f"    UI Automation could not read this window: {e}")
            continue
        out.extend(lines)
        out.append(f"=> {verdict(total, usable)}")

    report = "\n".join(out)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\n(written to {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
