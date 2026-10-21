"""Chat personas as data.

A persona is a JSON file: who the bot plays, how it talks, how long a reply may
be and, optionally, a ``full_feed`` block for a persona that reads every message
of some groups.  The built-in persona 杨宇靖 lives in
``src/personas/yang_yujing.json`` and is required: if it cannot be read the
program refuses to start with a message naming the file (a silent fallback
would make the bot someone else).

``data/personas/*.json`` next to the database holds your own personas.  They
are chosen per chat in ``data/persona_map.json``; they do not replace the
persona configured in ``.env`` (``CHAT_PERSONA_*``), which stays the default
for every chat the map does not name.  Both files are read once, when the
router is built: restart the bot after editing them.

The module-level ``DEFAULT_*`` constants and ``persona_defaults_for_profile``
keep their old names (they are read from the built-in file).  The editable
``custom`` profile has no file of its own: it starts from 杨宇靖's text.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .datafile import DataFileError, read_json

logger = logging.getLogger(__name__)

PERSONA_PROFILE_YANG_YUJING = "yang_yujing"
PERSONA_PROFILE_CUSTOM = "custom"
PERSONA_PROFILES = (
    PERSONA_PROFILE_YANG_YUJING,
    PERSONA_PROFILE_CUSTOM,
)

BUILTIN_DIR = Path(__file__).with_name("personas")
USER_DIR = Path("data") / "personas"         # relative to the working directory, like data/*.db

MAX_PROMPT_CHARS = 20000
MAX_SKIP_WORDS = 200
MAX_SKIP_WORD_CHARS = 40


class PersonaError(ValueError):
    """A persona file that cannot be used."""


@dataclass(frozen=True)
class FullFeed:
    """A persona that sees every new message of some groups, not only its mentions."""

    groups: tuple[str, ...] = ()               # groups it reads message by message (exact name or chat id)
    always_reply_groups: tuple[str, ...] = ()  # the same, but it always answers
    ambient_cooldown_sec: int = 45
    skip_reactions: frozenset[str] = frozenset()   # one-word reactions it does not answer
    skip_words: tuple[str, ...] = ()           # talk about the bot itself it stays out of (plain words, any case)
    min_chars: int = 3                         # shorter messages are not answered


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    prompt: str
    reply_rules: str
    max_chars: int = 36
    context_window_sec: int = 900
    context_count: int = 40
    full_feed: FullFeed | None = None
    source: str = ""

    def settings(self) -> dict:
        """The dict shape the prompt builders use (see AbstractSummarizer._chat_persona_settings)."""
        return {
            "persona_name": self.name,
            "persona_prompt": self.prompt,
            "reply_rules": self.reply_rules,
            "max_chars": self.max_chars,
            "context_count": self.context_count,
        }


def _text(data: dict, key: str, *, required: bool = True) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise PersonaError(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise PersonaError(f"{key} is required")
    if len(value) > MAX_PROMPT_CHARS:
        raise PersonaError(f"{key} is longer than {MAX_PROMPT_CHARS} characters")
    return value


def _int(data: dict, key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PersonaError(f"{key} must be a whole number from {low} to {high}")
    return value


def _names(data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PersonaError(f"full_feed.{key} must be a list of names")
    return tuple(v.strip() for v in value if v.strip())


def _words(data: dict, key: str, *, limit: int, max_chars: int) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PersonaError(f"full_feed.{key} must be a list of words")
    words = tuple(v.strip() for v in value if v.strip())
    if len(words) > limit or any(len(w) > max_chars for w in words):
        raise PersonaError(f"full_feed.{key} may hold at most {limit} words of {max_chars} characters")
    return words


def _full_feed(data: object) -> FullFeed | None:
    if data in (None, {}):
        return None
    if not isinstance(data, dict):
        raise PersonaError("full_feed must be an object")
    if "skip_pattern" in data:
        raise PersonaError('full_feed.skip_pattern was replaced by "skip_words" (a list of plain words)')
    return FullFeed(
        groups=_names(data, "groups"),
        always_reply_groups=_names(data, "always_reply_groups"),
        ambient_cooldown_sec=_int(data, "ambient_cooldown_sec", 45, 0, 3600),
        skip_reactions=frozenset(_words(data, "skip_reactions", limit=MAX_SKIP_WORDS, max_chars=MAX_SKIP_WORD_CHARS)),
        skip_words=_words(data, "skip_words", limit=MAX_SKIP_WORDS, max_chars=MAX_SKIP_WORD_CHARS),
        min_chars=_int(data, "min_chars", 3, 0, 100),
    )


def persona_from_dict(data: object, *, source: str = "") -> Persona:
    if not isinstance(data, dict):
        raise PersonaError("a persona file must hold one JSON object")
    persona_id = _text(data, "id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", persona_id):
        raise PersonaError("id may only use letters, digits, - and _")
    return Persona(
        id=persona_id,
        name=_text(data, "name"),
        prompt=_text(data, "prompt"),
        reply_rules=_text(data, "reply_rules"),
        max_chars=_int(data, "max_chars", 36, 8, 300),
        context_window_sec=_int(data, "context_window_sec", 900, 60, 7200),
        context_count=_int(data, "context_count", 40, 1, 120),
        full_feed=_full_feed(data.get("full_feed")),
        source=source,
    )


def load_persona_file(path: Path) -> Persona:
    try:
        return persona_from_dict(read_json(path), source=str(path))
    except DataFileError as exc:
        raise PersonaError(str(exc)) from None
    except PersonaError as exc:
        raise PersonaError(f"{path}: {exc}") from None


def load_personas(*dirs: Path) -> dict[str, Persona]:
    """Every ``*.json`` persona in ``dirs``; a later directory replaces an earlier id.

    A broken file is logged and skipped, never fatal: one typo in a user file
    must not stop the bot.
    """
    found: dict[str, Persona] = {}
    for directory in dirs:
        try:
            files = sorted(Path(directory).glob("*.json"))
        except OSError:
            continue
        for path in files:
            if path.name == "alias_rules.json":
                continue
            try:
                persona = load_persona_file(path)
            except PersonaError as exc:
                logger.error("Persona file skipped: %s", exc)
                continue
            found[persona.id] = persona
    return found


def _load_builtin(persona_id: str) -> Persona:
    """A built-in persona, or an error that says exactly what is wrong."""
    path = BUILTIN_DIR / f"{persona_id}.json"
    try:
        persona = load_persona_file(path)
    except PersonaError as exc:
        raise PersonaError(
            f"built-in persona {persona_id!r} cannot be loaded ({exc}). The folder src/personas must be "
            "installed with the program (PyInstaller: datas=[('src/personas', 'src/personas')])."
        ) from None
    if persona.id != persona_id:
        raise PersonaError(f"{path} must have id {persona_id!r}, not {persona.id!r}")
    return persona


_BUILTIN = {persona_id: _load_builtin(persona_id) for persona_id in (PERSONA_PROFILE_YANG_YUJING,)}
_YANG = _BUILTIN[PERSONA_PROFILE_YANG_YUJING]
_BUILTIN_FEED = _YANG.full_feed or FullFeed()

# The editable ``custom`` persona starts as 杨宇靖 (without reading every message).
DEFAULT_CHAT_PERSONA_NAME = _YANG.name
DEFAULT_CHAT_PERSONA_PROMPT = _YANG.prompt
DEFAULT_CHAT_REPLY_RULES = _YANG.reply_rules
DEFAULT_CHAT_MAX_CHARS = _YANG.max_chars
DEFAULT_CHAT_CONTEXT_WINDOW_SEC = _YANG.context_window_sec
DEFAULT_CHAT_CONTEXT_COUNT = _YANG.context_count
DEFAULT_YANG_YUJING_CONTEXT_COUNT = _YANG.context_count

# The full-feed preset only listens in the groups its file names.
DEFAULT_YANG_YUJING_GROUP_NAME = _BUILTIN_FEED.groups[0] if _BUILTIN_FEED.groups else ""
# An optional second group that is read message by message and always answered.
DEFAULT_YANG_YUJING_ALWAYS_REPLY_GROUP_NAME = (
    _BUILTIN_FEED.always_reply_groups[0] if _BUILTIN_FEED.always_reply_groups else ""
)
DEFAULT_YANG_YUJING_PERSONA_NAME = _YANG.name
DEFAULT_YANG_YUJING_PERSONA_PROMPT = _YANG.prompt
DEFAULT_YANG_YUJING_REPLY_RULES = _YANG.reply_rules


def normalize_persona_profile(profile: str | None) -> str:
    """Return a supported persona profile, falling back to the first one."""
    normalized = str(profile or "").strip().lower()
    if normalized in PERSONA_PROFILES:
        return normalized
    return PERSONA_PROFILE_YANG_YUJING


def persona_defaults_for_profile(profile: str | None) -> dict[str, object] | None:
    """Built-in settings for a fixed preset, or None for the editable ``custom`` persona."""
    if normalize_persona_profile(profile) != PERSONA_PROFILE_YANG_YUJING:
        return None
    return {
        "name": _YANG.name,
        "prompt": _YANG.prompt,
        "reply_rules": _YANG.reply_rules,
        "max_chars": _YANG.max_chars,
        "context_window_sec": _YANG.context_window_sec,
        "context_count": _YANG.context_count,
    }


def builtin_personas() -> dict[str, Persona]:
    return dict(_BUILTIN)


# ── which persona speaks in which chat ───────────────────────────────────────

PERSONA_MAP_FILE = Path("data") / "persona_map.json"

SOURCE_GROUP = "group"          # the map names this group
SOURCE_WILDCARD = "wildcard"    # the "*" entry
SOURCE_DEFAULT = "default"      # the "default" entry
SOURCE_PRIVATE = "private"      # the "private" entry


def _fold(text: object) -> str:
    """Case, width and compatibility forms ignored ("ＡＢＣ" matches "abc")."""
    return unicodedata.normalize("NFKC", str(text or "")).casefold().strip()


@dataclass(frozen=True)
class PersonaMap:
    """``data/persona_map.json``: group name / chat id fragment -> persona id.

    ``{"groups": {"读书群": "bookworm", "游戏": "yang_yujing"}, "private": "me", "default": null}``

    Groups are tried in file order (substring of the group name or chat id, case
    and width ignored; ``*`` matches every group).  ``private`` is the persona
    of one-to-one chats and ``default`` the one for every group that is not
    listed; leave them out to keep the configured persona there.
    """

    groups: tuple[tuple[str, str], ...] = ()
    private: str = ""
    default: str = ""

    @property
    def active(self) -> bool:
        return bool(self.groups or self.private or self.default)


def parse_persona_map(data: object) -> PersonaMap:
    if not isinstance(data, dict):
        raise PersonaError("the persona map must be a JSON object")
    groups = data.get("groups", {})
    if not isinstance(groups, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in groups.items()
    ):
        raise PersonaError('"groups" must map group names to persona ids')
    single: list[str] = []
    for key in ("private", "default"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise PersonaError(f'"{key}" must be a persona id or null')
        single.append((value or "").strip())
    return PersonaMap(
        groups=tuple((_fold(k), v.strip()) for k, v in groups.items() if _fold(k) and v.strip()),
        private=single[0], default=single[1],
    )


def load_persona_map(path: Path | None = None) -> PersonaMap:
    path = Path(path) if path is not None else PERSONA_MAP_FILE
    if not path.exists():
        return PersonaMap()
    try:
        return parse_persona_map(read_json(path))
    except (DataFileError, PersonaError) as exc:
        logger.error("Persona map %s ignored: %s", path, exc)
        return PersonaMap()


class PersonaRegistry:
    """Looks up the persona for a message.  Without a map it never answers."""

    def __init__(self, personas: dict[str, Persona] | None = None,
                 persona_map: PersonaMap | None = None) -> None:
        self.personas = dict(personas or {})
        self.map = persona_map or PersonaMap()
        for pid in self.unknown_ids():
            logger.error("Persona map names %r but no such persona is defined", pid)

    @classmethod
    def from_files(cls, user_dir: Path | None = None, map_path: Path | None = None) -> "PersonaRegistry":
        pmap = load_persona_map(map_path)
        personas = load_personas(BUILTIN_DIR, Path(user_dir) if user_dir is not None else USER_DIR) if pmap.active else {}
        return cls(personas, pmap)

    @property
    def active(self) -> bool:
        return self.map.active

    def unknown_ids(self) -> list[str]:
        wanted = {pid for _, pid in self.map.groups} | {p for p in (self.map.private, self.map.default) if p}
        return sorted(wanted - set(self.personas))

    def match(self, msg: dict) -> tuple[Persona | None, str]:
        """``(persona, how)`` for this message's chat; ``(None, "")`` keeps the configured persona."""
        if not self.map.active:
            return None, ""
        if not msg.get("is_group", True):
            pid, how = self.map.private, SOURCE_PRIVATE
        else:
            pid, how = "", ""
            places = (_fold(msg.get("group_name")), _fold(msg.get("chat_id")))
            for key, candidate in self.map.groups:
                if key == "*":
                    pid, how = candidate, SOURCE_WILDCARD
                    break
                if any(key in place for place in places if place):
                    pid, how = candidate, SOURCE_GROUP
                    break
            if not pid:
                pid, how = self.map.default, SOURCE_DEFAULT
        persona = self.personas.get(pid) if pid else None
        return (persona, how) if persona is not None else (None, "")

    def persona_for(self, msg: dict) -> Persona | None:
        """The persona mapped to this message's chat, or None (keep the configured one)."""
        return self.match(msg)[0]
