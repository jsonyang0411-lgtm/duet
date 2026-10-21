"""On-demand image/sticker understanding helpers."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

IMAGE_MSG_TYPES = {3, 47}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DAT_EXTS = {".dat"}
MEDIA_EXTS = IMAGE_EXTS | DAT_EXTS
MIN_MEDIA_BYTES = 128
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024

_IMAGE_WORDS = (
    "图", "图片", "照片", "相片", "表情", "表情包", "截图",
)
_QUESTION_WORDS = (
    "什么", "啥", "看", "识别", "内容", "意思", "什么意思",
    "写了", "写的", "是谁", "在哪", "哪里", "这张", "这个",
    "里面", "里边", "图里", "图上", "解读",
)
_DIRECT_QUESTION_RE = re.compile(
    r"(这|那|刚才|上面|前面)?(张|个)?(图|图片|照片|表情包|表情)"
)
# "发个害羞的表情包" / "来张图" asks the bot to SEND one (a sticker, SEND_STICKERS),
# not to look at one.  The words in between may not cross a 这/那/看, so
# "你发的这张图是什么" and "来看看这张图" stay image questions.
_SEND_REQUEST_RE = re.compile(
    r"(?:给我发|给我来|给我整|再发|再来|发|来|整|求|要|丢|甩|扔)"
    r"(?:一)?(?:个|张|波|些|点)[^看这那]{0,8}?(?:表情包|表情|图片|照片|图)"
)


def is_image_question(text: str, bot_name: str = "") -> bool:
    """Return True when a message explicitly asks about a recent image."""
    raw = str(text or "").strip()
    if not raw:
        return False
    if bot_name:
        raw = raw.replace(f"@{bot_name}", "").replace(bot_name, "")
    compact = re.sub(r"\s+", "", raw)
    if not any(word in compact for word in _IMAGE_WORDS):
        return False
    if _SEND_REQUEST_RE.search(compact):
        return False
    if _DIRECT_QUESTION_RE.search(compact) and any(
        word in compact for word in _QUESTION_WORDS
    ):
        return True
    return False


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
    cache_dir = Path("data") / "vision_cache"
    wechat_data_dir = str(getattr(config, "wechat_data_dir", "") or "")
    lookback_sec = int(getattr(config, "image_vision_lookback_sec", 900) or 900)

    for record in media_records:
        media_path = str(record.get("media_path", "") or "").strip()
        prepared = prepare_image_for_model(
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
            )
            if located:
                return located

    return ""


def locate_recent_image_file(
    *,
    wechat_data_dir: str,
    timestamp: int,
    lookback_sec: int = 900,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    cache_dir: Path | None = None,
    media_hint: str = "",
) -> str:
    """Find a recent image-like file in WeChat's local media cache."""
    if not wechat_data_dir:
        return ""

    target_ts = int(timestamp or 0)
    if target_ts <= 0:
        return ""

    hint = re.sub(r"[^0-9a-zA-Z]", "", media_hint).lower()
    best: tuple[float, Path] | None = None
    for root in _candidate_media_roots(Path(wechat_data_dir)):
        if not root.exists() or not root.is_dir():
            continue
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                [
                d for d in dirnames
                if d not in {"cache", "Cache", "Backup", "backup"}
                ],
                key=lambda d: (
                    0 if d.lower() in {"emoji", "customemotion", "image"} else 1,
                    d.lower(),
                ),
            )
            for filename in filenames:
                scanned += 1
                if scanned > 30000:
                    break
                path = Path(dirpath) / filename
                if path.suffix.lower() not in MEDIA_EXTS:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size < MIN_MEDIA_BYTES:
                    continue
                name_matches_hint = bool(hint and hint in path.name.lower())
                diff = abs(float(stat.st_mtime) - target_ts)
                if not name_matches_hint and diff > lookback_sec:
                    continue
                score = (0 if name_matches_hint else diff) + (
                    30 if path.suffix.lower() in DAT_EXTS else 0
                )
                if best is None or score < best[0]:
                    best = (score, path)
            if scanned > 30000:
                break

    if best is None:
        return ""
    return prepare_image_for_model(
        str(best[1]),
        cache_dir=cache_dir or Path("data") / "vision_cache",
        max_bytes=max_bytes,
    )


def prepare_image_for_model(
    path: str,
    *,
    cache_dir: Path,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> str:
    """Return a model-readable image path, decoding WeChat .dat if needed."""
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


def image_to_data_url(path: str, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES, *, still: bool = False) -> str:
    """Read an image file and return a data URL for vision chat completions.

    ``still=True`` (the local model) sends anything but PNG and JPEG as a PNG
    of its first frame: Ollama refuses a GIF ("invalid image input"), and
    WeChat stickers are often GIFs.
    """
    p = Path(path)
    data = p.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"Image too large for vision request: {len(data)} bytes")
    mime = mimetypes.guess_type(str(p))[0] or _mime_from_magic(data)
    if not mime or not mime.startswith("image/"):
        raise ValueError(f"Unsupported image type: {p}")
    if still and mime not in ("image/png", "image/jpeg"):
        data, mime = first_frame_png(data), "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def first_frame_png(data: bytes) -> bytes:
    """The first frame of an image (a GIF, a WebP ...) as PNG bytes."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            frame = image.convert("RGBA" if "transparency" in image.info or image.mode in ("RGBA", "LA", "P")
                                  else "RGB")
            out = io.BytesIO()
            frame.save(out, format="PNG")
            return out.getvalue()
    except Exception as exc:                              # noqa: BLE001 - a clear error for the caller
        raise ValueError(f"Cannot convert the image to PNG: {exc}") from exc


def decode_wechat_dat_image(path: Path, cache_dir: Path) -> Path | None:
    """Decode common XOR-obfuscated WeChat .dat image caches."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < MIN_MEDIA_BYTES:
        return None

    direct_ext = _ext_from_magic(raw)
    if direct_ext:
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
    # account DB directory. These roots are only searched for a qualified
    # image request/follow-up, never for ordinary messages.
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


def _mime_from_magic(data: bytes) -> str:
    ext = _ext_from_magic(data)
    return {
        "jpg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }.get(ext, "application/octet-stream")
