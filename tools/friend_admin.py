"""CLI tool to test and control WeChat friend operations on Windows.

Usage:
    # Immediately check and accept pending friend requests
    python tools/friend_admin.py --accept

    # Search and send friend request to a wxid or phone number
    python tools/friend_admin.py --add 13800000000 --message "你好，请通过一下"

    # ... with a remark (备注) typed into WeChat 4.0's application window when OCR finds its field
    python tools/friend_admin.py --add 13800000000 --message "你好" --remark "抖音-测试"

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
    parser.add_argument("--remark", default="", help="Remark (备注) for the new friend (WeChat 4.0 only, found by OCR)")
    parser.add_argument("--status", action="store_true", help="Check WeChat window and OCR state")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    win_ctrl = WeChatWindowController()
    ocr_engine = WindowsOcrEngine(window=win_ctrl) if WindowsOcrEngine else None
    mgr = FriendManager(window_controller=win_ctrl, ocr_engine=ocr_engine)

    if args.status:
        hwnd = win_ctrl.find_hwnd()
        print(f"WeChat main window HWND: {hwnd}")
        if hwnd:
            fg_info = win_ctrl.get_foreground_info()
            print(f"Foreground info: {fg_info}")
            if ocr_engine:
                try:
                    rows = ocr_engine.capture()
                except Exception as e:
                    print(f"OCR failed: {e}")
                    sys.exit(1)
                print(f"Captured {len(rows)} OCR text elements:")
                for r in rows[:15]:
                    print(f"  [{r.get('x0'):.3f}, {r.get('y0'):.3f}] {r.get('t')}")
                if len(rows) > 15:
                    print(f"  ... and {len(rows) - 15} more")
            else:
                print("No OCR engine: the Windows OCR is not available here")
        return

    if args.accept:
        print("Checking 'New Friends' list and accepting pending requests...")
        accepted = mgr.accept_friend_requests()
        if accepted:
            print(f"Successfully accepted {len(accepted)} friend request(s):")
            for item in accepted:
                print(f"  - Accepted at {item.get('timestamp')}")
        else:
            print("Nothing accepted (no pending requests, or the window / OCR failed: see the log above).")
        return

    if args.add:
        target = args.add.strip()
        msg = args.message.strip()
        remark = args.remark.strip()
        print(f"Sending friend request to: {target} (message: {msg!r}" + (f", remark: {remark!r}" if remark else "") + ")...")
        res = mgr.add_friend(target, message=msg, remark=remark) if remark else mgr.add_friend(target, message=msg)
        print(f"Result: {res.get('message')}")
        if remark:
            print("Remark: " + ("typed into the 备注 field" if res.get("remark_set") else
                                "NOT typed (no 备注 field seen: send the 'Friend management' log lines above)"))
        if not res.get("ok"):
            sys.exit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
