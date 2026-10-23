"""Pictures and files sent with a relay task: found on this PC, copied for the Mac.

An allowed person's private picture or file is held for ``HOLD_SEC``; the next
task takes everything held (``ClaudeRelay._queue``).  ``resolve`` finds it on
disk: a picture through the image-vision helpers (WeChat's ``.dat`` decoded; a
screenshot of the chat when it cannot be found), a file in WeChat's download
folder (``msg/file/<YYYY-MM>/`` in WeChat 4, ``FileStorage/File/<YYYY-MM>/`` in 3;
a second file of the same name is ``name(1).ext``).  WeChat downloads a file only
when it is small enough or someone opens it, so a file may not be there yet.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

HOLD_SEC = 600                       # a held picture or file waits this long for a task
MAX_FILE_BYTES = 50 * 1024 * 1024    # larger ones are not sent to the Mac
KINDS = {"image": "图片", "file": "文件"}


@dataclass
class Held:
    kind: str           # "image" or "file"
    msg: dict
    at: float


def attachment_kind(msg: dict) -> str:
    """"image", "file" or "" (not something to forward)."""
    try:
        msg_type = int(msg.get("msg_type", 1) or 1)
    except (TypeError, ValueError):
        return ""
    if msg_type == 3:
        return "image"
    if msg_type == 49 and str(msg.get("file_name") or "").strip():
        return "file"
    return ""


def resolve(config, held: Held, number: int) -> tuple[str, str, str]:
    """``(path, name, problem)``: the file on this PC and the name the Mac gets, or why not."""
    if held.kind == "image":
        from ..vision import find_recent_image_path
        vision = SimpleNamespace(
            wechat_data_dir=str(getattr(config, "wechat_data_dir", "") or ""),
            image_vision_max_bytes=MAX_FILE_BYTES,
            image_vision_lookback_sec=int(getattr(config, "image_vision_lookback_sec", 900) or 900),
        )
        path = find_recent_image_path(vision, [held.msg])
        if path:
            return path, f"图片-{number}{Path(path).suffix.lower() or '.jpg'}", ""
        capture = held.msg.get("_capture_media_screenshot")
        if callable(capture):
            try:
                path = str(capture() or "")
            except Exception:
                path = ""
            if path:
                return path, f"聊天截图-{number}.jpg", ""
        return "", "", "一张图片在电脑上没找到（可能还没下载完），没带上"
    name = str(held.msg.get("file_name") or "").strip()
    try:
        size = int(held.msg.get("file_size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size > MAX_FILE_BYTES:
        return "", "", f"文件「{name}」超过 {MAX_FILE_BYTES // 1024 // 1024} MB，没带上"
    found = locate_received_file(_data_dirs(config, held.msg), name, size,
                                 float(held.msg.get("timestamp", 0) or 0))
    if found is None:
        return "", "", f"文件「{name}」在电脑上没找到（要先在电脑微信里点一下它，下载完再发任务），没带上"
    return str(found), name, ""


def _data_dirs(config, msg: dict) -> list[Path]:
    """Folders holding WeChat accounts (``wxid_*``): the configured one, then the usual places."""
    dirs = [str(msg.get("media_base_dir") or ""), str(getattr(config, "wechat_data_dir", "") or "")]
    home = Path.home()
    dirs += [str(home / "Documents" / "xwechat_files"), str(home / "Documents" / "WeChat Files")]
    seen, out = set(), []
    for d in dirs:
        if d and d.lower() not in seen:
            seen.add(d.lower())
            out.append(Path(d))
    return out


def _accounts(base: Path) -> list[Path]:
    """``base`` itself (it may be an account folder) and every folder in it."""
    try:
        return [base] + [child for child in base.iterdir() if child.is_dir()] if base.is_dir() else []
    except OSError:
        return [base]


def locate_received_file(data_dirs: list[Path], name: str, size: int = 0,
                         timestamp: float = 0) -> Path | None:
    """The downloaded copy of a file someone sent, the newest of that name (and size, when known)."""
    if not name or Path(name).name != name:
        return None
    stem, suffix = Path(name).stem, Path(name).suffix
    numbered = re.compile(re.escape(stem) + r"\(\d+\)" + re.escape(suffix) + "$")
    when = timestamp or time.time()
    # The message's month; a few days back too (sent on the 31st, asked about on the 1st).
    months = {time.strftime("%Y-%m", time.localtime(t)) for t in (when, when - 3 * 86400, time.time())}
    best: tuple[float, Path] | None = None
    for base in data_dirs:
        for account in _accounts(base):
            for sub in (("msg", "file"), ("FileStorage", "File")):
                for month in months:
                    folder = account.joinpath(*sub, month)
                    try:
                        entries = list(folder.iterdir()) if folder.is_dir() else []
                    except OSError:
                        continue
                    for path in entries:
                        if path.name != name and not numbered.match(path.name):
                            continue
                        try:
                            stat = path.stat()
                        except OSError:
                            continue
                        if not path.is_file() or (size and stat.st_size != size):
                            continue                     # still downloading, or another file
                        if best is None or stat.st_mtime > best[0]:
                            best = (stat.st_mtime, path)
    return best[1] if best else None
