"""Real example dialogues for a persona: how it talks, shown to the model.

A character description ("shy, polite, says sorry a lot") leaves the model
guessing at the actual wording.  A few real exchanges show length, tone and
word choice directly.  The examples are a plain text file per persona,
``data/examples/<persona id or name>.txt`` (``PERSONA_EXAMPLES_DIR``)::

    # a line starting with # is a comment
    群友：鸣潮新角色你抽了吗
    杨宇靖：没抽，等复刻
    ---
    群友：晚饭吃啥
    杨宇靖：食堂，懒得出去

Blocks are separated by a line of ``---``.  Every line is ``speaker：text``
(full- or half-width colon).  A block needs at least one line by the persona
itself: its name, its id, or ``我``.  For each reply the structured pipeline
picks the ``PERSONA_EXAMPLES`` blocks (default 4, 0 = none) closest to the
current message and lists them in the prompt as a reference for style, not
content.  Other speakers are shown as 群友 / 群友A, so their real names never
reach the model.  Without a file nothing changes.  The file is re-read when it
changes; no restart needed.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .memory.text import bigrams

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1024 * 1024
MAX_EXAMPLES = 1000
MAX_EXAMPLE_CHARS = 300
MAX_PICK = 8
SELF_NAMES = ("我",)
_SEPARATOR = re.compile(r"^-{3,}$")
_LINE = re.compile(r"^(?P<who>[^：:\n]{1,20})[：:]\s*(?P<text>.+)$")


@dataclass(frozen=True)
class Example:
    lines: tuple[tuple[bool, str, str], ...]      # (by the persona, speaker, text)
    grams: frozenset = frozenset()


@dataclass
class ExampleBook:
    examples: tuple[Example, ...] = ()
    problems: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.examples)

    def pick(self, query: str, count: int, *, seed: str = "") -> list[Example]:
        """The ``count`` examples closest to ``query``; ties broken by ``seed`` so replies vary."""
        count = max(0, min(int(count), MAX_PICK))
        if not count or not self.examples:
            return []
        q = bigrams(query)

        def rank(item):
            index, example = item
            score = len(q & example.grams) / len(q) if q else 0.0
            shuffle = hashlib.sha1(f"{seed}|{index}".encode("utf-8")).hexdigest()
            return (-score, shuffle)

        return [example for _, example in sorted(enumerate(self.examples), key=rank)[:count]]


def parse_examples(text: str, persona_names) -> ExampleBook:
    """Blocks of ``speaker：text`` lines; a skipped block or line is listed in ``problems``."""
    names = {n.strip() for n in persona_names if n and n.strip()} | set(SELF_NAMES)
    examples: list[Example] = []
    problems: list[str] = []
    block: list[tuple[bool, str, str]] = []
    start = 1

    def close(end: int) -> None:
        nonlocal block
        if not block:
            return
        where = f"lines {start}-{end}"
        if not any(own for own, _, _ in block):
            problems.append(f"{where}: no line by the persona ({'/'.join(sorted(names))})")
        elif sum(len(t) for _, _, t in block) > MAX_EXAMPLE_CHARS:
            problems.append(f"{where}: longer than {MAX_EXAMPLE_CHARS} characters")
        elif len(examples) >= MAX_EXAMPLES:
            problems.append(f"{where}: more than {MAX_EXAMPLES} examples; the rest are ignored")
        else:
            others = " ".join(t for own, _, t in block if not own)
            mine = " ".join(t for own, _, t in block if own)
            examples.append(Example(tuple(block), frozenset(bigrams(others or mine))))
        block = []

    lines = str(text or "").lstrip("﻿").splitlines()
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if _SEPARATOR.match(line):
            close(number - 1)
            start = number + 1
            continue
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            problems.append(f"line {number}: expected 'speaker：text'")
            continue
        who = match.group("who").strip()
        if not block:
            start = number
        block.append((who in names, who, re.sub(r"\s+", " ", match.group("text")).strip()))
    close(len(lines))
    return ExampleBook(tuple(examples), problems)


def render_examples(examples, persona_name: str) -> str:
    """The picked examples as prompt text; other speakers become 群友 / 群友A, 群友B."""
    blocks = []
    for number, example in enumerate(examples, start=1):
        others = list(dict.fromkeys(who for own, who, _ in example.lines if not own))
        label = {who: ("群友" if len(others) == 1 else f"群友{chr(ord('A') + i)}")
                 for i, who in enumerate(others[:26])}
        lines = [f"{persona_name if own else label.get(who, '群友')}：{text}" for own, who, text in example.lines]
        blocks.append(f"例{number}\n" + "\n".join(lines))
    return "\n".join(blocks)


class ExampleLibrary:
    """Example files by persona, re-read when a file changes."""

    def __init__(self, folder) -> None:
        self.folder = Path(folder)
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[float, ExampleBook]] = {}

    def book_for(self, *persona_names: str) -> ExampleBook | None:
        """The examples of the first name (id or display name) that has a file."""
        for name in persona_names:
            if not name or any(c in name for c in "/\\") or name.startswith("."):
                continue
            path = self.folder / f"{name}.txt"
            try:
                stat = path.stat()
            except OSError:
                continue
            with self._lock:
                cached = self._cache.get(path)
                if cached and cached[0] == stat.st_mtime:
                    return cached[1]
            book = self._load(path, stat.st_size, persona_names)
            with self._lock:
                self._cache[path] = (stat.st_mtime, book)
            return book
        return None

    @staticmethod
    def _load(path: Path, size: int, persona_names) -> ExampleBook:
        if size > MAX_FILE_BYTES:
            logger.warning("Persona examples: %s is larger than %d KB; not used", path, MAX_FILE_BYTES // 1024)
            return ExampleBook(problems=[f"{path}: larger than {MAX_FILE_BYTES // 1024} KB"])
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Persona examples: cannot read %s (%s); save it as UTF-8", path, exc)
            return ExampleBook(problems=[f"{path}: {exc}"])
        book = parse_examples(raw, persona_names)
        logger.info("Persona examples: %d from %s%s", len(book), path,
                    f" ({len(book.problems)} skipped)" if book.problems else "")
        for problem in book.problems[:10]:
            logger.warning("Persona examples: %s: %s", path.name, problem)
        return book
