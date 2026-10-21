"""Sticker pictures the bot may send now and then (``SEND_STICKERS=on``).

The library is a folder, ``data/stickers/`` by default, with the pictures and a
``stickers.json`` that names them and says when each fits::

    {"stickers": [
        {"name": "害羞捂脸", "file": "shy.gif", "when": "被夸或被调侃的时候"},
        {"name": "问号", "file": "what.png", "when": "没听懂", "personas": ["杨宇靖"]}
    ]}

``personas`` (optional) limits a sticker to those persona names or ids.  The
structured pipeline lists the stickers in its prompt and the model may name one
in the optional ``sticker`` field.  The router then appends a sticker bubble:
``STICKER_MARK`` followed by the name.  Like the bubble separator, only the
router may put that mark in a reply; :class:`src.send.bubbles.BubbleSender`
looks the name up here and hands the file to the backend.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..datafile import DataFileError, read_json

logger = logging.getLogger(__name__)

STICKER_MARK = "⁤"          # INVISIBLE PLUS: starts a sticker bubble
LIBRARY_FILE = "stickers.json"
SUFFIXES = {".gif", ".png", ".jpg", ".jpeg"}
MAX_STICKERS = 60
MAX_NAME_CHARS = 12
MAX_WHEN_CHARS = 40
MAX_FILE_BYTES = 10 * 1024 * 1024
_NAME_RE = re.compile(r"[\w·\-]{1,%d}" % MAX_NAME_CHARS)


@dataclass(frozen=True)
class Sticker:
    name: str
    path: Path
    when: str = ""
    personas: tuple[str, ...] = ()

    def fits(self, *personas: str) -> bool:
        return not self.personas or any(p in self.personas for p in personas if p)


@dataclass
class StickerLibrary:
    stickers: tuple[Sticker, ...] = ()
    problems: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_name = {sticker.name: sticker for sticker in self.stickers}

    def __len__(self) -> int:
        return len(self.stickers)

    def get(self, name: str) -> Sticker | None:
        return self._by_name.get(str(name or ""))

    def choices(self, *personas: str) -> tuple[Sticker, ...]:
        """The stickers this persona may use (give its name and id)."""
        return tuple(s for s in self.stickers if s.fits(*personas))

    @classmethod
    def load(cls, folder) -> "StickerLibrary":
        """The usable stickers in ``folder``; every skipped entry is in ``problems``."""
        folder = Path(folder)
        source = folder / LIBRARY_FILE
        if not source.is_file():
            return cls(problems=[f"{source} not found"])
        try:
            data = read_json(source)
        except DataFileError as exc:
            return cls(problems=[str(exc)])
        entries = data.get("stickers") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return cls(problems=[f'{source}: expected {{"stickers": [...]}}'])
        root = folder.resolve()
        stickers: list[Sticker] = []
        problems: list[str] = []
        for index, entry in enumerate(entries, start=1):
            try:
                sticker = _sticker(entry, root)
            except ValueError as exc:
                problems.append(f"entry {index}: {exc}")
                continue
            if any(s.name == sticker.name for s in stickers):
                problems.append(f"entry {index}: the name {sticker.name!r} is used twice")
                continue
            if len(stickers) >= MAX_STICKERS:
                problems.append(f"entry {index}: more than {MAX_STICKERS} stickers; the rest are ignored")
                break
            stickers.append(sticker)
        for problem in problems:
            logger.warning("Sticker library: %s", problem)
        return cls(tuple(stickers), problems)


def _sticker(entry, root: Path) -> Sticker:
    if not isinstance(entry, dict):
        raise ValueError("must be an object")
    name = str(entry.get("name", "") or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"name must be 1 to {MAX_NAME_CHARS} letters, digits, - or · (got {name!r})")
    when = re.sub(r"\s+", " ", str(entry.get("when", "") or "")).strip()
    if len(when) > MAX_WHEN_CHARS:
        raise ValueError(f"when is longer than {MAX_WHEN_CHARS} characters")
    personas = entry.get("personas", [])
    if not isinstance(personas, list) or not all(isinstance(p, str) for p in personas):
        raise ValueError("personas must be a list of names")
    file_name = str(entry.get("file", "") or "").strip()
    if not file_name:
        raise ValueError("file is required")
    path = (root / file_name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"{file_name!r} is outside the sticker folder")
    if path.suffix.lower() not in SUFFIXES:
        raise ValueError(f"{file_name!r} is not a gif, png or jpg file")
    if not path.is_file():
        raise ValueError(f"{file_name!r} not found")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{file_name!r} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    return Sticker(name, path, when, tuple(p.strip() for p in personas if p.strip()))


def sticker_token(name: str) -> str:
    """The bubble that stands for a sticker in a reply string."""
    return STICKER_MARK + name


def sticker_in(part: str) -> str | None:
    """The sticker name when this bubble is a sticker, else None."""
    if part.startswith(STICKER_MARK):
        return part[len(STICKER_MARK):].strip()
    return None


_MENTION_RE = re.compile(r"[\[【]表情包[：:]\s*([^\[\]【】\r\n]{1,20})[\]】]")


def pop_sticker_mentions(text: str) -> tuple[str, list[str]]:
    """Remove ``[表情包：name]`` written into a reply; return the text and the names.

    The bot remembers its stickers in that form, and a model copies it from
    "what you said lately"; sent as text it would read like a bot.
    """
    names: list[str] = []

    def take(match) -> str:
        names.append(match.group(1).strip())
        return ""

    text = _MENTION_RE.sub(take, str(text or ""))
    return (re.sub(r"\s{2,}", " ", text).strip() if names else text), names


def describe(part: str) -> str:
    """A bubble as the bot remembers it: a sticker becomes ``[表情包：name]``."""
    name = sticker_in(part)
    return part if name is None else f"[表情包：{name}]"


_library = StickerLibrary()


def get_stickers() -> StickerLibrary:
    """The library in use (empty until ``configure_stickers``)."""
    return _library


def configure_stickers(library: StickerLibrary) -> StickerLibrary:
    """Replace the process-wide library; the bot calls this once at start-up."""
    global _library
    _library = library
    return _library
