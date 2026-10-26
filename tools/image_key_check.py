#!/usr/bin/env python3
"""Check that the bot can open WeChat 4 pictures (the encrypted ``.dat`` files).

Run it on the Windows computer, from the project folder, with WeChat running and logged in:

    python tools/image_key_check.py              # find the keys (once) and decode one picture
    python tools/image_key_check.py --no-scan    # only look at the files, do not search memory

What it does, step by step (it only READS WeChat's files and memory, never changes them):
  1. finds the account folders (WECHAT_DATA_DIR, else Documents\\xwechat_files\\*)
  2. counts the pictures: V1 (fixed key), V2 (key per account), other
  3. works out the XOR key from the thumbnails (*_t.dat)
  4. for V2: searches WeChat's memory for the AES key (10-60 s; saved in data\\wechat_image_keys.json,
     so the bot never needs to search again)
  5. decodes the newest picture to data\\vision_cache\\image_key_check.<ext> so you can open it

It prints counts and sizes only, and the first 4 characters of a key, never a whole key.

What a failure means:
  no account folder        set WECHAT_DATA_DIR in .env to the folder that holds the wxid_* folders
  no V2 thumbnail          open 2-3 pictures in WeChat on this computer, then run it again
  key not found            WeChat must be running and logged in; open a few pictures in WeChat
                           (the key is in memory once pictures were shown) and run it again
  cannot open the process  run the terminal the same way as WeChat (not as another user)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.wechat import image_key as K  # noqa: E402


def account_dirs() -> list[Path]:
    bases = [os.getenv("WECHAT_DATA_DIR", "").strip(), str(Path.home() / "Documents" / "xwechat_files")]
    found: list[Path] = []
    for base in filter(None, bases):
        base_path = Path(base)
        candidates = [base_path] + ([p for p in base_path.iterdir() if p.is_dir()] if base_path.is_dir() else [])
        found += [p for p in candidates if (p / "msg" / "attach").is_dir() and p not in found]
    return found


def newest_pictures(account: Path, limit: int = 60) -> list[Path]:
    files: list[Path] = []
    for dirpath, _dirs, names in os.walk(account / "msg" / "attach"):
        files += [Path(dirpath) / n for n in names if n.endswith(".dat")]
        if len(files) > 3000:
            break
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def main(argv=None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="Can the bot open WeChat 4 pictures?")
    parser.add_argument("--no-scan", action="store_true", help="do not search WeChat's memory for the key")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    os.chdir(root)                     # data\ is relative to the project folder, as for the bot

    accounts = account_dirs()
    if not accounts:
        print("FAIL no account folder (a folder with msg\\attach). Set WECHAT_DATA_DIR in .env.")
        return 1
    ok = True
    for account in accounts:
        print(f"\naccount folder: {account.name}")
        pictures = newest_pictures(account)
        versions = {0: 0, 1: 0, 2: 0}
        for p in pictures:
            with open(p, "rb") as f:
                versions[K.dat_version(f.read(6))] += 1
        print(f"  newest {len(pictures)} .dat files: V1 {versions[1]}, V2 {versions[2]}, other {versions[0]}")
        thumbs = K.thumbnails(account)
        print(f"  thumbnails (*_t.dat): {len(thumbs)}")
        if not versions[1] and not versions[2]:
            print("  no WeChat 4 pictures here (WeChat 3 pictures are decoded the old way)")
            continue

        scan = K.scan_wechat_for_key if not args.no_scan else (lambda block: None)
        keys = K.ImageKeys(scan=scan)
        started = time.monotonic()
        aes, xor = keys.keys(account, need_aes=bool(versions[2]))
        print(f"  XOR key: {'found' if xor is not None else 'NOT FOUND'}")
        if versions[2]:
            shown = (aes[:4].decode() + "…") if aes else "NOT FOUND"
            print(f"  AES key (V2): {shown}  ({time.monotonic() - started:.1f} s)")
            if not aes and not K.v2_block_from(thumbs):
                print("  no V2 thumbnail to test keys on: open 2-3 pictures in WeChat, then run this again")

        sample = next((p for p in pictures if not p.name.endswith("_t.dat") and K.dat_version(p.read_bytes()[:6])),
                      None)
        if sample is None:
            print("  no full-size picture to try")
            continue
        picture = K.decode_v4(sample.read_bytes(), aes, xor)
        ext = ("jpg" if picture and picture[:3] == b"\xff\xd8\xff" else
               "png" if picture and picture[:4] == b"\x89PNG" else
               "gif" if picture and picture[:3] == b"GIF" else "")
        if not ext:
            ok = False
            print(f"  FAIL the newest picture ({sample.stat().st_size} bytes) could not be decoded")
            continue
        out = Path("data") / "vision_cache" / f"image_key_check.{ext}"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(picture)
        size = ""
        try:
            from PIL import Image
            with Image.open(out) as image:
                size = f", {image.width}x{image.height}"
        except Exception as e:                                   # noqa: BLE001 - only for the report
            ok = False
            size = f", but it does not open as a picture ({e})"
        print(f"  OK decoded the newest picture: {out} ({len(picture)} bytes{size})")
    print("\nOK" if ok else "\nSOME CHECKS FAILED (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
