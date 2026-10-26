"""Pictures WeChat keeps on disk: find the one a message refers to, and make it readable.

The relay and the PRM copilot forward a picture a person sent (src/relay/attachments.py):
``find_recent_image_path`` looks for the message's picture file (WeChat 4 keeps a chat's
pictures in ``msg/attach/<md5 of the chat id>/<YYYY-MM>/Img``) and ``readable_image``
turns WeChat's ``.dat`` caches into an ordinary picture (``decode_wechat_dat_image``:
WeChat 3's XOR, WeChat 4's AES + XOR through ``src/wechat/image_key.py``).  Decoded
pictures go to ``data/media_cache``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_MSG_TYPES = {3, 47}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DAT_EXTS = {".dat"}
MEDIA_EXTS = IMAGE_EXTS | DAT_EXTS
MIN_MEDIA_BYTES = 128
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SCANNED_FILES = 30000          # per search root
MAX_PICTURES_TRIED = 6             # candidate pictures (not files) tried before giving up
CACHE_DIR = Path("data") / "media_cache"
_MONTH_DIR = re.compile(r"^(\d{4})-(\d{2})$")


def find_existing_media_path_from_message(
    raw_msg: dict[str, Any],
    wechat_data_dir: str = "",
) -> str:
    """Find an explicit local media path inside a raw WCDB message, if any."""
    bases = _candidate_bases(wechat_data_dir)
    for value in _iter_string_values(raw_msg):
        value = value.strip().strip('"').strip("'")
        if not _looks_like_media_path_hint(value):
            continue
        for path in _expand_path_hint(value, bases):
            if _is_candidate_media_file(path):
                return str(path)
    return ""


def find_recent_image_path(config: Any, media_records: list[dict]) -> str:
    """Resolve the best local image path for recent media DB rows."""
    max_bytes = int(
        getattr(config, "image_vision_max_bytes", DEFAULT_MAX_IMAGE_BYTES)
        or DEFAULT_MAX_IMAGE_BYTES
    )
    cache_dir = CACHE_DIR
    wechat_data_dir = str(getattr(config, "wechat_data_dir", "") or "")
    lookback_sec = int(getattr(config, "image_vision_lookback_sec", 900) or 900)
    allow_thumbnail = bool(getattr(config, "image_allow_thumbnail", True))

    for record in media_records:
        media_path = str(record.get("media_path", "") or "").strip()
        prepared = readable_image(
            media_path, cache_dir=cache_dir, max_bytes=max_bytes,
        )
        if prepared:
            return prepared

    for record in media_records:
        timestamp = int(record.get("timestamp", 0) or 0)
        media_hint = str(record.get("media_hint", "") or "")
        bases = [
            str(record.get("media_base_dir", "") or "").strip(),
            wechat_data_dir,
        ]
        seen_bases: set[str] = set()
        for base in bases:
            key = base.lower()
            if not base or key in seen_bases:
                continue
            seen_bases.add(key)
            located = locate_recent_image_file(
                wechat_data_dir=base,
                timestamp=timestamp,
                lookback_sec=lookback_sec,
                max_bytes=max_bytes,
                cache_dir=cache_dir,
                media_hint=media_hint,
                chat_id=str(record.get("chat_id", "") or ""),
                allow_thumbnail=allow_thumbnail,
            )
            if located:
                return located

    return ""


@dataclass
class _Picture:
    """One file that may be (a version of) the picture asked for."""
    path: Path
    mtime: float
    diff: float            # seconds from the message
    stem: str              # the picture's name without the _h / _t of WeChat 4's HD / thumbnail
    rank: int              # 0 HD (_h), 1 the plain file, 2 thumbnail (_t)


def _stem_and_rank(name: str) -> tuple[str, int]:
    stem = Path(name).stem.lower()
    if stem.endswith("_h"):
        return stem[:-2], 0
    if stem.endswith("_t"):
        return stem[:-2], 2
    return stem, 1


def _month_index(ts: float) -> int:
    t = time.localtime(ts)
    return t.tm_year * 12 + t.tm_mon


def _walk_pictures(root: Path, *, target_ts: int, lookback_sec: int, hint: str, budget: list[int]) -> list[_Picture]:
    """The picture files below ``root`` that fit the message's time (or whose name carries its md5).

    WeChat 4 keeps a month per folder (``2026-10``): months more than one away from the message's are
    not entered, so years of older pictures cannot use up the file budget before the new ones are seen.
    """
    found: list[_Picture] = []
    month = _month_index(target_ts)
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            if d in {"cache", "Cache", "Backup", "backup"}:
                continue
            m = _MONTH_DIR.match(d)
            if m and abs(int(m.group(1)) * 12 + int(m.group(2)) - month) > 1:
                continue
            keep.append(d)
        dirnames[:] = sorted(keep, key=lambda d: (0 if d.lower() in {"emoji", "customemotion", "image", "img"} else 1,
                                                 d.lower()))
        for filename in filenames:
            budget[0] -= 1
            if budget[0] < 0:
                return found
            path = Path(dirpath) / filename
            if path.suffix.lower() not in MEDIA_EXTS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size < MIN_MEDIA_BYTES:
                continue
            diff = abs(float(stat.st_mtime) - target_ts)
            if not (hint and hint in path.name.lower()) and diff > lookback_sec:
                continue
            stem, rank = _stem_and_rank(filename)
            found.append(_Picture(path, float(stat.st_mtime), diff, stem, rank))
    return found


def locate_recent_image_file(
    *,
    wechat_data_dir: str,
    timestamp: int,
    lookback_sec: int = 900,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    cache_dir: Path | None = None,
    media_hint: str = "",
    chat_id: str = "",
    allow_thumbnail: bool = True,
) -> str:
    """Find a recent image-like file in WeChat's local media cache and make it readable.

    WeChat 4 keeps a conversation's pictures in ``msg/attach/<md5 of the chat id>/<YYYY-MM>/Img``.
    With ``chat_id`` that folder is searched alone (another chat's picture that happens to be
    closer in time is never taken); only when no such folder exists, as with WeChat 3, is
    everything searched.  The files of one picture (``x_h.dat`` HD, ``x.dat``, ``x_t.dat``
    thumbnail) are one candidate: the nearest in time (or the one whose name carries the message's
    md5) is tried first, its best version first; a candidate that cannot be decoded (a sticker in
    WeChat's own wxgf format, a missing key) is skipped for the next one.  ``allow_thumbnail=False``
    never returns a thumbnail.
    """
    if not wechat_data_dir:
        return ""
    target_ts = int(timestamp or 0)
    if target_ts <= 0:
        return ""

    hint = re.sub(r"[^0-9a-zA-Z]", "", media_hint).lower()
    cache = cache_dir or CACHE_DIR
    base = Path(wechat_data_dir)
    budget = [MAX_SCANNED_FILES]
    pictures: list[_Picture] = []

    chat_folders = []
    if chat_id:
        chat_md5 = hashlib.md5(chat_id.encode("utf-8")).hexdigest()
        chat_folders = [a / "msg" / "attach" / chat_md5 for a in _candidate_bases(str(base))
                        if (a / "msg" / "attach" / chat_md5).is_dir()]
    if chat_folders:
        for folder in chat_folders:
            pictures += _walk_pictures(folder, target_ts=target_ts, lookback_sec=lookback_sec, hint=hint, budget=budget)
        where = f"the chat's folder ({len(chat_folders)})"
    else:
        for root in _candidate_media_roots(base):
            if root.exists() and root.is_dir():
                budget = [MAX_SCANNED_FILES]
                pictures += _walk_pictures(root, target_ts=target_ts, lookback_sec=lookback_sec, hint=hint,
                                           budget=budget)
        where = "all of WeChat's media folders" + (" (this chat has no folder of its own)" if chat_id else "")

    groups: dict[str, list[_Picture]] = {}
    for picture in pictures:
        groups.setdefault(picture.stem, []).append(picture)

    def nearness(files: list[_Picture]) -> tuple[int, float]:
        exact = bool(hint) and any(hint in f.path.name.lower() for f in files)
        return (0 if exact else 1,
                min(f.diff for f in files) + (30 if all(f.path.suffix.lower() in DAT_EXTS for f in files) else 0))

    problems: list[str] = []
    for files in sorted(groups.values(), key=nearness)[:MAX_PICTURES_TRIED]:
        for picture in sorted(files, key=lambda f: (f.rank, f.diff)):
            if picture.rank == 2 and not allow_thumbnail:
                problems.append(f"{picture.path.name}: only a thumbnail, not wanted")
                continue
            prepared = readable_image(str(picture.path), cache_dir=cache, max_bytes=max_bytes)
            if prepared:
                logger.info("Picture found in %s: %s (%s, %.0f s from the message)", where, picture.path.name,
                            ("HD", "plain", "thumbnail")[picture.rank], picture.diff)
                return prepared
            problems.append(f"{picture.path.name}: could not be read")
    logger.info("No picture found in %s for %s (message time %d, window %d s): %d file(s) fit%s", where,
                chat_id or "an unknown chat", target_ts, lookback_sec, len(pictures),
                "; " + "; ".join(problems[:4]) if problems else "")
    return ""


def readable_image(
    path: str,
    *,
    cache_dir: Path,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> str:
    """An ordinary picture file for ``path`` (a WeChat .dat decoded into ``cache_dir``), or ""."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    try:
        if p.stat().st_size > max_bytes:
            return ""
    except OSError:
        return ""

    suffix = p.suffix.lower()
    if suffix in IMAGE_EXTS:
        return str(p)
    if suffix in DAT_EXTS:
        decoded = decode_wechat_dat_image(p, cache_dir)
        if decoded and decoded.exists():
            try:
                if decoded.stat().st_size <= max_bytes:
                    return str(decoded)
            except OSError:
                return ""
    return ""


def decode_wechat_dat_image(path: Path, cache_dir: Path) -> Path | None:
    """Decode WeChat .dat image caches: the XOR-obfuscated ones of WeChat 3, and WeChat 4's
    AES + XOR ones (``src/wechat/image_key.py``; the first V2 picture may take a memory search)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < MIN_MEDIA_BYTES:
        return None

    from .image_key import dat_version, decode_file
    direct_ext = _ext_from_magic(raw)
    if dat_version(raw):
        try:
            decoded = decode_file(path, raw)
        except Exception:
            logger.exception("Decoding the WeChat 4 picture %s failed", path)
            decoded = None
        ext = _ext_from_magic(decoded or b"")
        if not ext:
            logger.info("WeChat 4 picture %s (V%d) not decoded: %s", path.name, dat_version(raw), _dat_problem(decoded))
            return None
    elif direct_ext:
        decoded = raw
        ext = direct_ext
    else:
        key = _infer_xor_key(raw)
        if key is None:
            return None
        decoded = bytes(b ^ key for b in raw)
        ext = _ext_from_magic(decoded)
        if not ext:
            return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(decoded).hexdigest()
    out = cache_dir / f"{digest}.{ext}"
    if not out.exists():
        out.write_bytes(decoded)
    return out


def _dat_problem(decoded: bytes | None) -> str:
    if decoded is None:
        return "no key for it (V2: the AES key was not found) or the file is cut short"
    if decoded.startswith(b"wxgf"):
        return "WeChat's own wxgf format (a sticker or an HEVC picture); not supported"
    return f"decrypted, but it is not a picture it starts with {decoded[:4].hex()}"


def _candidate_bases(wechat_data_dir: str) -> list[Path]:
    if not wechat_data_dir:
        return []
    base = Path(wechat_data_dir)
    bases = [base]
    try:
        if base.exists() and base.is_dir():
            bases.extend(
                child for child in base.iterdir()
                if child.is_dir() and child.name.startswith("wxid_")
            )
    except OSError:
        pass
    return bases


def _candidate_media_roots(base: Path) -> list[Path]:
    accounts = _candidate_bases(str(base))
    roots: list[Path] = []
    for account in accounts:
        roots.extend([
            account / "msg" / "image",
            account / "msg" / "emoji",
            account / "msg" / "file",
            account / "msg" / "attach",
            account / "FileStorage" / "Image",
            account / "FileStorage" / "CustomEmotion",
            account / "FileStorage" / "Temp",
            account / "MsgAttach",
            account,
        ])
    # Some Windows WeChat builds keep downloaded custom emojis outside the
    # account DB directory.
    home = Path.home()
    roots.extend([
        home / "Documents" / "WeChat Files",
        home / "Documents" / "xwechat_files",
        home / "AppData" / "Roaming" / "Tencent" / "WeChat",
        home / "AppData" / "Local" / "Tencent" / "WeChat",
    ])
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _iter_string_values(value: Any, depth: int = 0):
    if depth > 3:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_string_values(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_string_values(item, depth + 1)


def _looks_like_media_path_hint(value: str) -> bool:
    if not value or len(value) > 500:
        return False
    lower = value.lower().replace("/", "\\")
    return any(ext in lower for ext in MEDIA_EXTS)


def _expand_path_hint(value: str, bases: list[Path]):
    p = Path(value)
    if p.is_absolute():
        yield p
    for base in bases:
        yield base / value
        yield base / value.replace("/", "\\")


def _is_candidate_media_file(path: Path) -> bool:
    try:
        return (
            path.exists()
            and path.is_file()
            and path.suffix.lower() in MEDIA_EXTS
            and path.stat().st_size >= MIN_MEDIA_BYTES
        )
    except OSError:
        return False


def _infer_xor_key(raw: bytes) -> int | None:
    for magic in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"RIFF"):
        key = raw[0] ^ magic[0]
        sample = bytes(b ^ key for b in raw[: max(len(magic), 12)])
        if sample.startswith(magic):
            if magic == b"RIFF" and sample[8:12] != b"WEBP":
                continue
            return key
    return None


def _ext_from_magic(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"GIF8"):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    return ""
