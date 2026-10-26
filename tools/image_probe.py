#!/usr/bin/env python3
"""Why did a picture not come through as the original?  Look at WeChat 4's picture files, read-only.

Run it on the Windows computer, from the project folder, right after sending the bot a picture
(WeChat running, the bot stopped or not, it does not matter):

    python tools/image_probe.py                       # the chat of ADMIN_WXID (from .env)
    python tools/image_probe.py --chat wxid_abc       # another chat; repeat --chat for more
    python tools/image_probe.py --minutes 30 --limit 20

What it prints (names, sizes, ages and results only; never a picture or a chat text):
  - the key file it would use (the bot looks in data\\ of ITS working folder: a tool run from the project
    folder and the EXE started elsewhere do not share one) and whether the keys are in it
  - for each chat: the name of its folder under msg\\attach (the md5 of the chat id) and whether it exists
  - the newest picture files there: age, kind (HD _h / plain / thumbnail _t), size, V1/V2, and whether they
    decode (OK widthxheight / wxgf: a sticker or HEVC picture, not supported / key missing / unreadable)
  - what the bot's lookup picks for the newest of them, as a message at that time (a thumbnail does not count)

If the chat's folder is NOT there but the files are under another folder, the folder name is not the md5 of
the chat id and the lookup has to search everything: tell us which folder the newest files are in.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.wechat import media  # noqa: E402
from src.wechat import image_key as K  # noqa: E402

KINDS = ("HD (_h)", "plain", "thumbnail (_t)")


def accounts() -> list[Path]:
    bases = [os.getenv("WECHAT_DATA_DIR", "").strip(), str(Path.home() / "Documents" / "xwechat_files")]
    found: list[Path] = []
    for base in filter(None, bases):
        b = Path(base)
        for candidate in [b] + ([p for p in b.iterdir() if p.is_dir()] if b.is_dir() else []):
            if (candidate / "msg" / "attach").is_dir() and candidate not in found:
                found.append(candidate)
    return found


def chat_md5(chat_id: str) -> str:
    return hashlib.md5(chat_id.encode("utf-8")).hexdigest()


def describe(path: Path, keys: K.ImageKeys) -> str:
    """What the file is and whether it decodes, in words (it is decoded in memory and thrown away)."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        return f"unreadable ({e})"
    version = K.dat_version(raw)
    if path.suffix.lower() != ".dat":
        return f"not a .dat ({len(raw)} bytes)"
    if not version:
        head = raw[:4]
        return "plain picture" if head[:3] == b"\xff\xd8\xff" or head == b"\x89PNG" else f"not V1/V2 (starts {head.hex()})"
    account = K.account_dir_of(path)
    aes, xor = keys.keys(account, need_aes=version == 2, allow_scan=False) if account else (None, None)
    if version == 2 and not aes:
        return "V2: AES key missing"
    if xor is None:
        return f"V{version}: XOR key missing"
    picture = K.decode_v4(raw, aes, xor)
    if picture is None:
        return f"V{version}: could not be decoded (cut short?)"
    if picture.startswith(b"wxgf"):
        return f"V{version}: wxgf (a sticker or an HEVC picture, not supported)"
    try:
        from PIL import Image
        with Image.open(io.BytesIO(picture)) as image:
            return f"V{version}: OK {image.width}x{image.height} {image.format.lower()}"
    except Exception as e:                                           # noqa: BLE001 - only for the report
        return f"V{version}: decrypted, but not a picture ({type(e).__name__})"


def kind_of(name: str) -> str:
    stem = Path(name).stem.lower()
    return KINDS[0] if stem.endswith("_h") else KINDS[2] if stem.endswith("_t") else KINDS[1]


def newest_files(folders: list[Path], seconds: float, limit: int) -> list[tuple[float, Path]]:
    now = time.time()
    found = []
    for folder in folders:
        for dirpath, _dirs, names in os.walk(folder):
            for name in names:
                path = Path(dirpath) / name
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if age <= seconds and path.suffix.lower() in media.MEDIA_EXTS:
                    found.append((age, path))
    return sorted(found, key=lambda item: item[0])[:limit]


def main(argv=None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="Why did a picture not come through as the original?")
    parser.add_argument("--chat", action="append", default=[], help="a chat id (wxid_... or ...@chatroom); repeatable")
    parser.add_argument("--minutes", type=float, default=15, help="how far back to look (default 15)")
    parser.add_argument("--limit", type=int, default=12, help="files to list per account (default 12)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="    | %(message)s")
    os.chdir(root)                                   # data\ is relative to the project folder, as for the bot
    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env")
    except ImportError:
        pass
    chats = args.chat or ([os.getenv("ADMIN_WXID", "").strip()] if os.getenv("ADMIN_WXID", "").strip() else [])

    keys = K.ImageKeys(scan=lambda block: None)      # never searches memory: this tool only reads
    print(f"key file: {keys.path.resolve()} ({'exists' if keys.path.is_file() else 'NOT THERE'})")
    found_accounts = accounts()
    if not found_accounts:
        print("FAIL no account folder (a folder with msg\\attach). Set WECHAT_DATA_DIR in .env.")
        return 1
    if not chats:
        print("no --chat given and no ADMIN_WXID: listing the newest files of every chat folder")

    for account in found_accounts:
        print(f"\naccount folder: {account.name}")
        attach = account / "msg" / "attach"
        names = {chat_md5(c): c for c in chats}
        folders = []
        for md5 in names:
            folder = attach / md5
            print(f"  chat {names[md5]}: folder {md5}  {'EXISTS' if folder.is_dir() else 'NOT THERE'}")
            if folder.is_dir():
                folders.append(folder)
        if not chats:
            folders = [attach]
        files = newest_files(folders, args.minutes * 60, args.limit)
        print(f"  files from the last {args.minutes:g} min: {len(files)}")
        for age, path in files:
            chat = path.relative_to(attach).parts[0]
            who = names.get(chat) or chat[:8] + "…"
            print(f"    {age:6.0f}s  {who:<20} {path.name:<44} {kind_of(path.name):<14} {path.stat().st_size:>9} B  "
                  f"{describe(path, keys)}")
        if not files:
            continue
        newest_age, newest = files[0]
        chat_id = names.get(newest.relative_to(attach).parts[0], "")
        print(f"\n  the bot's lookup for a message sent when the newest file was written "
              f"(chat {chat_id or 'unknown: searching everything'}):")
        with tempfile.TemporaryDirectory() as cache:
            got = media.locate_recent_image_file(
                wechat_data_dir=str(account.parent), timestamp=int(time.time() - newest_age),
                cache_dir=Path(cache), chat_id=chat_id, allow_thumbnail=False)
            if got:
                try:
                    from PIL import Image
                    with Image.open(got) as image:
                        print(f"  -> OK, the original: {image.width}x{image.height} ({Path(got).stat().st_size} bytes)")
                except Exception as e:                       # noqa: BLE001
                    print(f"  -> a file came out but does not open as a picture ({e})")
            else:
                print("  -> NOTHING: the relay would send a screenshot of the chat window (reasons in the | lines above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
