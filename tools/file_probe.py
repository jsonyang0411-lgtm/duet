#!/usr/bin/env python3
"""tools/file_probe.py: look at a received file in the WeChat window before the bot learns to save it.

WeChat downloads a received file by itself only up to a size; a bigger one waits for a click.
The bot must never LEFT-click a file (that can open it, and a program would run), so it will
right-click the file and choose 另存为 (save as).  This tool shows what that looks like on this
computer, one step at a time.  It never left-clicks the file.

Before: in WeChat on this computer, open the private chat with the bot where the file is, so its
message is on the screen (scroll if needed).  The window must not be minimized.  To test with a
small file: WeChat settings → 文件管理 → turn off automatic download, then send any file.

    python tools/file_probe.py --file "名字.zip"               # look only (default)
    python tools/file_probe.py --file "名字.zip" --right-click # + right-click the file, read the menu, Esc
    python tools/file_probe.py --file "名字.zip" --save-as     # + click 另存为 in that menu, read the save
                                                                #   window, then Esc (nothing is saved)

It writes probe-file.txt and probe-file-*.png next to the project folder.  The pictures show a
strip of the chat around the file and the menu; the text has the OCR lines near the file.  Look
at them before you send them on.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.wechat.file_bubble import (  # noqa: E402
    CONVERSATION_X0, SAVE_AS_LABELS, centre, file_rows, menu_item, rows_near,
)

OUT_DIR = PROJECT_ROOT


def describe(rows) -> list[str]:
    return [f"    {r['t']!r}  x {r['x0']:.3f}-{r['x1']:.3f}  y {r['y0']:.3f}-{r['y1']:.3f}" for r in rows]


# ── Windows only ────────────────────────────────────────────────────────

def process_windows(pid: int) -> dict[int, dict]:
    import win32gui
    import win32process
    found: dict[int, dict] = {}

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right - left >= 40 and bottom - top >= 20:
                found[hwnd] = {"class": win32gui.GetClassName(hwnd), "title": win32gui.GetWindowText(hwnd),
                               "rect": (left, top, right, bottom)}
        return True

    win32gui.EnumWindows(collect, None)
    return found


def right_click(x: int, y: int) -> None:
    import ctypes
    import win32con
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


def left_click(x: int, y: int) -> None:
    import ctypes
    import win32con
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Look at a received file in WeChat (Windows)")
    parser.add_argument("--file", required=True, help="the file's name as WeChat shows it")
    parser.add_argument("--right-click", action="store_true", help="right-click the file and read the menu")
    parser.add_argument("--save-as", action="store_true", help="also click 另存为 and read the save window")
    args = parser.parse_args(argv)
    if args.save_as:
        args.right_click = True
    if sys.platform != "win32":
        print("This tool needs Windows (it looks at the WeChat window there).")
        return 2

    try:
        import win32gui
        import win32process
        from PIL import ImageGrab
        from src.wechat.keyboard import press_key
        from src.wechat.ocr.win_ocr import WindowsOcrEngine, print_window, rows_from_lines
        from src.wechat.window_controller import WeChatWindowController
    except ImportError as e:
        print(f"This tool needs Windows with pywin32 and Pillow ({e}).")
        return 2

    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    def finish(code: int) -> int:
        (OUT_DIR / "probe-file.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\n(written to {OUT_DIR / 'probe-file.txt'})")
        return code

    controller = WeChatWindowController()
    hwnd = controller.find_hwnd(wake=False)
    if not hwnd:
        say("WeChat's main window was not found: open WeChat (not minimized) with the chat on screen.")
        return finish(1)
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    say(f"WeChat main window: {width}x{height} at ({left},{top})")
    ocr = WindowsOcrEngine(window=controller)

    # 1. look: the rows that show the file, and the bubble around it
    image = print_window(hwnd)
    rows = ocr.capture(hwnd=hwnd)
    say(f"OCR rows in the window: {len(rows)}")
    matches = file_rows(rows, args.file, min_x0=CONVERSATION_X0)
    if not matches:
        matches = file_rows(rows, args.file)
        if matches:
            say(f"(the file is shown left of x={CONVERSATION_X0}: a separate chat window, or the chat list)")
    if not matches:
        say(f"No row shows {args.file!r}. Rows of the conversation area (x >= {CONVERSATION_X0}):")
        out.extend(describe([r for r in rows if r["x0"] >= CONVERSATION_X0]))
        return finish(1)
    say(f"Rows that show the file (lowest first): {len(matches)}")
    out.extend(describe(matches))
    row = matches[0]
    say("Rows around it (the bubble: size, status ...):")
    out.extend(describe(rows_near(rows, row)))
    img_w, img_h = image.size
    strip = image.crop((int(CONVERSATION_X0 * img_w * 0.8), max(0, int((row["y0"] - 0.12) * img_h)),
                        img_w, min(img_h, int((row["y1"] + 0.12) * img_h))))
    strip.save(OUT_DIR / "probe-file-1.png")
    say("Picture of the bubble: probe-file-1.png")
    if not args.right_click:
        return finish(0)

    # 2. right-click the file (never a left click on it), read the menu
    if not controller.activate(hwnd):
        say("Could not bring WeChat to the front; stopped before clicking.")
        return finish(1)
    time.sleep(0.3)
    cx, cy = centre(row)
    x, y = left + int(cx * width), top + int(cy * height)
    before = process_windows(pid)
    right_click(x, y)
    time.sleep(0.8)
    after = process_windows(pid)
    new = {h: w for h, w in after.items() if h not in before}
    say(f"Right-clicked at ({x},{y}). New WeChat windows: {len(new)}")
    menu_rows, menu_rect = [], None
    for h, w in new.items():
        say(f"  window class={w['class']!r} title={w['title']!r} rect={w['rect']}")
        try:
            try:
                picture = print_window(h)                       # real pixels, even when covered
            except Exception:                                   # noqa: BLE001 - a small popup: grab the screen
                picture = ImageGrab.grab(bbox=w["rect"])
            picture.save(OUT_DIR / "probe-file-2.png")
            say("  picture: probe-file-2.png")
            menu_rows = rows_from_lines(ocr._recognize(picture), picture)
            menu_rect = w["rect"]
        except Exception as e:                                  # noqa: BLE001 - only reported
            say(f"  could not read it: {e}")
    if not new:
        grab = ImageGrab.grab(bbox=(x - 50, y - 300, x + 300, y + 300))
        grab.save(OUT_DIR / "probe-file-2.png")
        say("  no new window: the menu may be drawn inside the main window; picture: probe-file-2.png")
        menu_rows = [r for r in ocr.capture(hwnd=hwnd) if abs(r["x0"] - cx) < 0.3]
        menu_rect = (left, top, right, bottom)
    say("Menu rows:")
    out.extend(describe(menu_rows))
    item = menu_item(menu_rows)
    say(f"Save-as item: {item['t']!r}" if item else f"No item among {SAVE_AS_LABELS} in the menu.")
    if not args.save_as or item is None or menu_rect is None:
        press_key(0x1B)                                           # Esc: close the menu
        say("Closed the menu (Esc).")
        return finish(0 if item else 1)

    # 3. click 另存为 in the menu (a menu item, not the file), read the save window, Esc
    mx, my = centre(item)
    m_left, m_top, m_right, m_bottom = menu_rect
    before = process_windows(pid)
    left_click(m_left + int(mx * (m_right - m_left)), m_top + int(my * (m_bottom - m_top)))
    time.sleep(1.5)
    dialogs = {h: w for h, w in process_windows(pid).items() if h not in before}
    say(f"Clicked {item['t']!r}. New windows: {len(dialogs)}")
    for h, w in dialogs.items():
        say(f"  window class={w['class']!r} title={w['title']!r} rect={w['rect']}")
        try:
            import uiautomation
            for control, depth in uiautomation.WalkControl(uiautomation.ControlFromHandle(h), includeTop=True,
                                                           maxDepth=6):
                if control.ControlTypeName in ("EditControl", "ButtonControl", "ComboBoxControl", "WindowControl"):
                    say(f"    {'  ' * depth}{control.ControlTypeName} name={str(control.Name)[:40]!r} "
                        f"id={control.AutomationId!r}")
        except Exception as e:                                  # noqa: BLE001 - only reported
            say(f"    UI Automation could not read it: {e}")
        try:
            ImageGrab.grab(bbox=w["rect"]).save(OUT_DIR / "probe-file-3.png")
            say("    picture: probe-file-3.png")
        except Exception:                                       # noqa: BLE001
            pass
    press_key(0x1B)                                               # Esc: cancel, nothing is saved
    say("Cancelled the save window (Esc). Nothing was saved.")
    return finish(0 if dialogs else 1)


if __name__ == "__main__":
    sys.exit(main())
