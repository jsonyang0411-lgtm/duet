"""CLI tool to test and control WeChat friend operations on Windows.

Usage:
    # Immediately check and accept pending friend requests
    python tools/friend_admin.py --accept

    # Search and send friend request to a wxid or phone number
    python tools/friend_admin.py --add 13800000000 --message "你好，请通过一下"

    # Inspect current WeChat window and OCR preview
    python tools/friend_admin.py --status
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config
from src.wechat.friend_manager import FriendManager
from src.wechat.window_controller import WeChatWindowController

try:
    from src.wechat.ocr.win_ocr import WindowsOcrEngine
except ImportError:
    WindowsOcrEngine = None


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat Friend Management CLI")
    parser.add_argument("--accept", action="store_true", help="Accept all pending friend requests")
    parser.add_argument("--add", metavar="TARGET", help="Search and add a friend by phone number or wxid")
    parser.add_argument("--message", default="", help="Greeting message / reason for adding friend")
    parser.add_argument("--status", action="store_true", help="Check WeChat window and OCR state")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    config = load_config()
    win_ctrl = WeChatWindowController()
    ocr_engine = WindowsOcrEngine(window=win_ctrl) if WindowsOcrEngine else None
    mgr = FriendManager(window_controller=win_ctrl, ocr_engine=ocr_engine, config=config)

    if args.status:
        hwnd = win_ctrl.find_hwnd()
        print(f"WeChat main window HWND: {hwnd}")
        if hwnd:
            fg_info = win_ctrl.get_foreground_info()
            print(f"Foreground info: {fg_info}")
            if ocr_engine:
                rows = ocr_engine.capture()
                print(f"Captured {len(rows)} OCR text elements:")
                for r in rows[:15]:
                    print(f"  [{r.get('x0'):.3f}, {r.get('y0'):.3f}] {r.get('t')}")
                if len(rows) > 15:
                    print(f"  ... and {len(rows) - 15} more")
        return

    if args.accept:
        print("Checking 'New Friends' list and accepting pending requests...")
        accepted = mgr.accept_friend_requests()
        if accepted:
            print(f"Successfully accepted {len(accepted)} friend request(s):")
            for item in accepted:
                print(f"  - Accepted at {item.get('timestamp')}")
        else:
            print("No pending friend requests found.")
        return

    if args.add:
        target = args.add.strip()
        msg = args.message.strip()
        print(f"Sending friend request to: {target} (message: {msg!r})...")
        res = mgr.add_friend(target, message=msg)
        print(f"Result: {res.get('message')}")
        if not res.get("ok"):
            sys.exit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
