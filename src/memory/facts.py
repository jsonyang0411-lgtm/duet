"""Lightweight structured fact extraction for stable group memories.

The nicknames it looks for are data, not code: ``AliasRules`` comes from
``data/alias_rules.json`` (yours) or ``src/personas/alias_rules.json`` (the
shipped default), so another group's nicknames need no code change.  Memory v2
learns nicknames from the chat itself and does not use this extractor.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..datafile import DataFileError, read_json

logger = logging.getLogger(__name__)

_WECHAT_SENDER_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9_]+:\s*")

SHIPPED_RULES = Path(__file__).resolve().parents[1] / "personas" / "alias_rules.json"
USER_RULES = Path("data") / "alias_rules.json"
MAX_ENTRIES = 50
MAX_WORD_CHARS = 20


@dataclass(frozen=True)
class AliasRules:
    """Which nicknames the extractor recognises.

    Plain words, never regular expressions (a pattern typed into a data file
    would run on every group message, and a careless one can freeze the bot).
    ``aliases`` are the nicknames after "叫" / "称呼"; ``bare_aliases`` those
    written right behind a name ("宇肆次元哥哥"); each entry is a ``stem`` that
    may be followed by one of its ``suffixes``.  ``derived`` adds a shorthand
    fact for nicknames that start with a prefix (the group's pinyin-initials
    habit).  The regex pieces below are built from them with ``re.escape``.
    """

    alias_pattern: str = ""
    bare_alias_pattern: str = ""
    derived: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: object) -> "AliasRules":
        if not isinstance(data, dict):
            raise ValueError("alias rules must be a JSON object")
        return cls(
            _entries(data.get("aliases", []), "aliases"),
            _entries(data.get("bare_aliases", []), "bare_aliases"),
            _derived(data.get("derived", [])),
        )


def _word(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_WORD_CHARS:
        raise ValueError(f"{what}: every word must be text of 1 to {MAX_WORD_CHARS} characters")
    return value.strip()


def _entries(value: object, key: str) -> str:
    """``[{"stem": "次元", "suffixes": ["哥哥", "哥"]}]`` as one regex alternation of escaped words."""
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        raise ValueError(f"{key} must be a list of at most {MAX_ENTRIES} entries")
    alternatives = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError(f"{key}: every entry must be an object with a stem")
        stem = re.escape(_word(entry.get("stem"), key))
        suffixes = entry.get("suffixes", [])
        if not isinstance(suffixes, list) or len(suffixes) > MAX_ENTRIES:
            raise ValueError(f"{key}: suffixes must be a list")
        tail = "|".join(re.escape(_word(s, key)) for s in suffixes)
        alternatives.append(f"{stem}(?:{tail})?" if tail else stem)
    return f"(?:{'|'.join(alternatives)})" if alternatives else ""


def _derived(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        raise ValueError("derived must be a list of {prefix, alias}")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("derived must be a list of {prefix, alias}")
        out.append((_word(item.get("prefix"), "derived").lower(), _word(item.get("alias"), "derived")))
    return tuple(out)


def load_alias_rules(user_path: Path | None = None, shipped_path: Path | None = None) -> AliasRules:
    """The user's rules if the file exists and is valid, else the shipped ones, else none."""
    for path in (user_path or USER_RULES, shipped_path or SHIPPED_RULES):
        try:
            if path.exists():
                return AliasRules.from_dict(read_json(path))
        except (DataFileError, ValueError) as exc:
            logger.error("Alias rules %s ignored: %s", path, exc)
    return AliasRules()


@dataclass(frozen=True)
class GroupFact:
    subject: str
    predicate: str
    object: str
    confidence: float = 0.8


class GroupFactExtractor:
    """Extract small durable facts such as group nicknames/call rules."""

    def __init__(self, bot_name: str = "", rules: AliasRules | None = None):
        self._bot_name = bot_name
        self._rules = rules if rules is not None else load_alias_rules()

    def extract(self, msg: dict, recent_messages: list[dict] | None = None) -> list[GroupFact]:
        text = self._clean_text(msg.get("content", ""))
        if not text:
            return []

        facts: list[GroupFact] = []
        facts.extend(self._extract_alias_facts(text))

        pronoun_alias = self._extract_pronoun_alias(text)
        if pronoun_alias:
            subject = self._find_recent_call_subject(recent_messages or [], msg)
            if subject:
                facts.append(GroupFact(subject, "alias", pronoun_alias, 0.82))

        facts = self._with_derived_aliases(facts)
        return self._dedupe(facts)

    def _clean_text(self, value: object) -> str:
        text = _WECHAT_SENDER_PREFIX.sub("", str(value or ""), count=1)
        if self._bot_name:
            text = re.sub(re.escape(f"@{self._bot_name}") + r"[^\w]*", "", text)
        text = text.replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_alias_facts(self, text: str) -> list[GroupFact]:
        facts: list[GroupFact] = []
        patterns = self._alias_patterns()
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                subject = self._clean_name(match.group("subject"))
                alias = self._clean_alias(match.group("alias"))
                if self._valid_alias_fact(subject, alias):
                    facts.append(GroupFact(subject, "alias", alias, 0.86))
        return facts

    def _alias_patterns(self) -> tuple[str, ...]:
        """The four shapes a nickname is introduced in (see AliasRules)."""
        alias = self._rules.alias_pattern
        patterns: list[str] = []
        if alias:
            patterns += [
                # 叫<名字><昵称> / 称呼<名字>为<昵称>
                rf"(?:叫|称呼)(?P<subject>@?[\u4e00-\u9fffA-Za-z0-9_]{{1,16}}?)(?:为|叫)?(?P<alias>{alias})(?:也可以|就行|吧|呀)?",
                # 看到<名字>该叫<昵称> / 见到<名字>要叫<昵称>
                rf"(?:看到|见到)?(?P<subject>@?[\u4e00-\u9fffA-Za-z0-9_]{{1,16}}?)(?:该|要|应该)?叫(?P<alias>{alias})(?:也可以|就行|吧|呀)?",
                # @<名字> 叫<昵称>
                rf"@(?P<subject>[^\s@]{{1,20}})\s*叫(?P<alias>{alias})",
            ]
        if self._rules.bare_alias_pattern:
            # <名字><昵称>
            patterns.append(
                rf"(?P<subject>[\u4e00-\u9fffA-Za-z0-9_]{{1,16}})(?P<alias>{self._rules.bare_alias_pattern})"
            )
        return tuple(patterns)

    def _extract_pronoun_alias(self, text: str) -> str:
        patterns = (
            r"叫(?:他|她|它)(?P<alias>[\u4e00-\u9fffA-Za-z0-9_]{1,16})",
            r"(?:他|她|它)(?:叫|是)(?P<alias>[\u4e00-\u9fffA-Za-z0-9_]{1,16})",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            alias = self._clean_alias(match.group("alias"))
            if alias and alias not in {"什么", "啥", "谁", "哪个"}:
                return alias
        return ""

    def _find_recent_call_subject(self, recent_messages: list[dict], current_msg: dict) -> str:
        current_id = current_msg.get("message_id", "")
        current_ts = int(current_msg.get("timestamp", 0) or 0)
        patterns = (
            r"(?:见到|看到)?(?P<subject>@?[\u4e00-\u9fffA-Za-z0-9_]{1,16}?)(?:要|该|应该)?叫(?:什么|啥)",
            r"应该叫(?P<subject>@?[\u4e00-\u9fffA-Za-z0-9_]{1,16}?)(?:什么|啥)",
        )
        for item in reversed(recent_messages):
            if current_id and item.get("message_id") == current_id:
                continue
            item_ts = int(item.get("timestamp", 0) or 0)
            if current_ts and item_ts > current_ts:
                continue
            text = self._clean_text(item.get("content", ""))
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    subject = self._clean_name(match.group("subject"))
                    if subject:
                        return subject
        return ""

    @staticmethod
    def _clean_name(value: str) -> str:
        return str(value or "").strip().strip("@ ，,。！？?!：:")

    @staticmethod
    def _clean_alias(value: str) -> str:
        alias = str(value or "").strip().strip("@ ，,。！？?!：:")
        alias = re.sub(r"(吗|嘛|么|呀|啊|呢|吧)$", "", alias)
        return alias

    @staticmethod
    def _valid_alias_fact(subject: str, alias: str) -> bool:
        if not subject or not alias:
            return False
        if subject == alias:
            return False
        if alias in {"什么", "啥", "谁", "哪个", "这个", "那个", "他", "她", "它"}:
            return False
        if subject in {"看到", "见到", "应该", "这个", "那个", "他", "她", "它"}:
            return False
        if any(
            bad in subject
            for bad in (
                "叫", "什么", "啥", "谁", "记得", "知道", "都说",
                "这个是", "那个是", "群里", "怎么", "不是", "就是",
            )
        ):
            return False
        if subject.endswith(("要", "该", "应该", "为")):
            return False
        return 1 <= len(subject) <= 20 and 1 <= len(alias) <= 20

    def _with_derived_aliases(self, facts: list[GroupFact]) -> list[GroupFact]:
        out = list(facts)
        for fact in facts:
            if fact.predicate != "alias":
                continue
            for alias in self._derived_aliases(fact.object):
                out.append(
                    GroupFact(
                        fact.subject,
                        fact.predicate,
                        alias,
                        min(fact.confidence, 0.78),
                    )
                )
        return out

    def _derived_aliases(self, alias: str) -> list[str]:
        alias = str(alias or "").strip().lower()
        return [short for prefix, short in self._rules.derived if alias.startswith(prefix)]

    @staticmethod
    def _dedupe(facts: list[GroupFact]) -> list[GroupFact]:
        seen: set[tuple[str, str, str]] = set()
        out: list[GroupFact] = []
        for fact in facts:
            key = (fact.subject, fact.predicate, fact.object)
            if key in seen:
                continue
            seen.add(key)
            out.append(fact)
        return out
