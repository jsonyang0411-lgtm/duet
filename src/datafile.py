"""Reading small JSON data files that a person edits by hand.

The sticker list (``data/stickers/stickers.json``) is such a file.  People save
them with Notepad (a BOM, or the system code page instead of UTF-8) and make
typos, so reading them has to fail in one predictable way: ``DataFileError``,
which callers log and skip.  A file is never allowed to stop the bot from
starting or to be arbitrarily large.
"""

from __future__ import annotations

import json
from pathlib import Path

MAX_DATA_FILE_BYTES = 256 * 1024


class DataFileError(ValueError):
    """The file is missing, too large, not UTF-8 text, or not valid JSON."""


def read_json(path: Path | str, *, max_bytes: int = MAX_DATA_FILE_BYTES) -> object:
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise DataFileError(f"{path}: larger than {max_bytes // 1024} KB")
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)          # a special file reports size 0
    except OSError as exc:
        raise DataFileError(f"{path}: {exc}") from None
    if len(raw) > max_bytes:
        raise DataFileError(f"{path}: larger than {max_bytes // 1024} KB")
    try:
        text = raw.decode("utf-8-sig")                 # a BOM is fine
    except UnicodeDecodeError:
        raise DataFileError(f"{path}: not UTF-8 text (save it as UTF-8)") from None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DataFileError(f"{path}: {exc}") from None
