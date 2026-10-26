#!/usr/bin/env python3
"""Check the OCR fallback on this computer: can it read the WeChat window?

Run it from the project folder (macOS or Windows, WeChat open and not minimized):

    python tools/ocr_check.py                       # three looks, counts only
    python tools/ocr_check.py --watch 90 --group 测试群
    python tools/ocr_check.py --save-rows rows.json

By default it prints NUMBERS only (rows, messages, chat area), never what the chats say.
``--watch N --group NAME`` runs N seconds the way the bot does (a look every ``--interval``
seconds, only new messages count) and prints the new lines of that ONE chat, so use it with
a test group: open the group on this computer, post a message from your phone, and it should
show up within a few seconds.  ``--save-rows`` writes the raw rows, which contain the text on
the screen: do not post that file anywhere.

What a failure means:
  macOS    0 rows            Screen Recording is missing for the program running this
                             (System Settings > Privacy & Security > Screen Recording)
  macOS    no recognizer     Xcode Command Line Tools missing: xcode-select --install
  Windows  no OCR language   Add-WindowsCapability -Online -Name Language.OCR~~~zh-CN~0.0.1.0
                             (PowerShell as administrator)
  both     window not found  WeChat is closed or minimized
  both     rows but no title The window is too small (an independent chat window dragged small):
                             the bot reads the FRONT WeChat window; make it about 600x500 or bigger
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.wechat.ocr.engines import default_engine  # noqa: E402
from src.wechat.ocr.reader import (  # noqa: E402
    NewMessageTracker, clean_group_name, read_rows, resolve_chat_geometry,
)


def look(engine, tracker, show_for: str | None):
    """One look: ``(summary line, new messages of the chat named ``show_for``)``."""
    started = time.monotonic()
    rows = engine.capture()
    took = time.monotonic() - started
    info = read_rows(rows)
    geom = resolve_chat_geometry(rows)
    fresh = tracker.feed(info["title"], info["sequence"]) if info["title"] else []
    shown = fresh if show_for and clean_group_name(info["title"]) == show_for else []
    summary = (
        f"{took:.2f}s rows={len(rows)} title={'yes' if info['title'] else 'NO'} "
        f"group_like={'yes' if info['title_clean'] != info['title'] else 'no'} "
        f"other={len(info['other'])} self={len(info['self'])} new={len(fresh)} "
        f"chat_area=x{geom.chat_x0:.2f}-{geom.chat_x1:.2f} y{geom.chat_y0:.2f}-{geom.chat_y1:.2f}"
    )
    return summary, shown, rows, info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check that the OCR fallback can read the WeChat window.")
    ap.add_argument("--looks", type=int, default=3, help="how many looks in the default mode")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between looks")
    ap.add_argument("--watch", type=float, default=0, metavar="SECONDS", help="watch for new messages this long")
    ap.add_argument("--group", default="", help="with --watch: print the new lines of this chat (name without the member count)")
    ap.add_argument("--save-rows", default="", metavar="FILE", help="write the raw OCR rows of the last look (contains the text)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        engine = default_engine()
        if not engine.prepare():
            print("FAIL: the recognizer is not available (see the message above)")
            return 1
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1

    tracker = NewMessageTracker()
    last_rows = []
    deadline = time.monotonic() + args.watch if args.watch else None
    count = 0
    while True:
        count += 1
        try:
            summary, shown, last_rows, info = look(engine, tracker, args.group or None)
        except Exception as exc:                                    # noqa: BLE001
            print(f"look {count}: FAIL: {exc}")
            return 1
        print(f"look {count}: {summary}")
        for text in shown:
            print(f"  NEW in {args.group}: {text}")
        if not last_rows:
            print("  no text on the screen at all: check the permission / that WeChat is not minimized")
        elif not info["title"]:
            print("  text was read but no chat title: open a chat, and make the window bigger "
                  "(the bot reads the FRONT WeChat window, the main one or an independent chat window; "
                  "about 600x500 or more)")
        if deadline is None and count >= args.looks:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(args.interval)

    if args.save_rows:
        Path(args.save_rows).write_text(json.dumps(last_rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"raw rows of the last look written to {args.save_rows} (contains the text on the screen)")
    return 0 if last_rows else 1


if __name__ == "__main__":
    sys.exit(main())
