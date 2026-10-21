"""Set up and check the sticker library and the WeChat emoji list.

    python tools/sticker_check.py --init     # create data/stickers/ with a stickers.json to fill in
    python tools/sticker_check.py            # list the usable stickers and every problem
    python tools/sticker_check.py --emoji    # one line of all emoji codes to send in WeChat

For ``--emoji``: send the printed line to yourself (文件传输助手).  Every code
WeChat shows as plain text instead of a picture does not exist in your WeChat
version; leave it out with WECHAT_EMOJI_CODES in .env.

The folder is STICKER_DIR (from .env), or ``--dir``.  Exit status 1
means a problem was found.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.config  # noqa: E402,F401 - reads .env and .env.local like the bot does
from src.send.stickers import LIBRARY_FILE, StickerLibrary  # noqa: E402
from src.send.wechat_emoji import DEFAULT_CODES, parse_codes  # noqa: E402

TEMPLATE = {
    "stickers": [
        {"name": "害羞捂脸", "file": "shy.gif", "when": "被夸或被调侃的时候"},
        {"name": "问号", "file": "what.png", "when": "没听懂对方在说什么"},
    ]
}


def init(folder: Path) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / LIBRARY_FILE
    if target.exists():
        print(f"{target} already exists; not changed")
        return 0
    target.write_text(json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Created {target}. Put the pictures (gif, png, jpg) in {folder} and edit the list.")
    return 0


def check(folder: Path) -> int:
    library = StickerLibrary.load(folder)
    for sticker in library.stickers:
        only = f"  (only {', '.join(sticker.personas)})" if sticker.personas else ""
        print(f"ok  {sticker.name}: {sticker.path.name} — {sticker.when or '(no when)'}{only}")
    for problem in library.problems:
        print(f"!!  {problem}")
    print(f"{len(library)} usable, {len(library.problems)} problem(s)")
    return 1 if library.problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=os.getenv("STICKER_DIR", "data/stickers"), help="sticker folder")
    parser.add_argument("--init", action="store_true", help="create the folder and a stickers.json template")
    parser.add_argument("--emoji", action="store_true", help="print the WeChat emoji codes in use")
    args = parser.parse_args(argv)
    if args.emoji:
        print("".join(parse_codes(os.getenv("WECHAT_EMOJI_CODES", "")) or DEFAULT_CODES))
        return 0
    if args.init:
        return init(Path(args.dir))
    return check(Path(args.dir))


if __name__ == "__main__":
    sys.exit(main())
