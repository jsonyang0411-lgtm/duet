"""De-identification helpers for the replay-case exporter (Phase 0 tooling).

Standalone module: standard library only, no import of the bot's ``src``
package, so ``tools/export_replay_cases.py`` runs wherever Python 3.12 and a
copy of ``messages.db`` are available.

What the :class:`Redactor` does
-------------------------------
* Senders become per-chat pseudonyms in order of first appearance
  ("用户A", "用户B", ..., "用户Z", "用户AA", ...) and ``u_`` + a short salted
  hash for the id.  Chats become ``g_<hash>@chatroom`` / ``p_<hash>`` ids with
  display names "示例群N" / "示例私聊N".  The salt is random per run unless one
  is supplied, and is never written to any output.
* Free text is scrubbed with regular expressions: mobile numbers, landlines,
  e-mails, URLs and bare domains, IP addresses, ``wxid_`` tokens, QQ numbers,
  ID-card / bank-card / long digit strings and long token-like blobs.  Hex
  blobs and raw media XML are replaced wholesale (``[图片]`` and friends).
* Names of the chat's known senders (and ``@mentions``) are replaced by the
  same pseudonyms used for the sender fields; unknown ``@xxx`` mentions become
  ``@路人XYZ`` (stable inside a chat).  The bot's own ``@bot_name`` mention,
  ``@所有人`` and ``@all`` are kept because replay behaviour depends on them.
* Optional extras: a blocklist of words (``<已屏蔽>``), a table of real group
  names (``示例群N``) and an alias for the bot's own name.

Replacement tokens are inserted through private-use sentinels and restored at
the very end, so one rule can never rewrite the output of another.

What it cannot do: see :data:`LIMITATIONS`.
"""

from __future__ import annotations

import hashlib
import html
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, NamedTuple

__all__ = [
    "KIND_LABELS",
    "KIND_ORDER",
    "LIMITATIONS",
    "PLACEHOLDER_IMAGE",
    "RedactedMessage",
    "Redactor",
    "TOKENS",
    "letters_label",
    "looks_like_blob",
    "normalize_text",
    "parse_group_names",
    "parse_blocklist",
    "scrub_text",
]

PLACEHOLDER_IMAGE = "[图片]"

#: Replacement token for each regex-based redaction kind.
TOKENS: dict[str, str] = {
    "phone": "<手机号>",
    "landline": "<座机>",
    "email": "<邮箱>",
    "url": "<链接>",
    "ip": "<IP地址>",
    "wxid": "<微信号>",
    "chatroom_id": "<群号>",
    "idcard": "<证件号>",
    "bankcard": "<银行卡>",
    "qq": "<数字串>",
    "digits": "<数字串>",
    "long_token": "<长串>",
    "blocklist": "<已屏蔽>",
}

#: Human readable description of every redaction kind (dry-run report).
KIND_LABELS: dict[str, str] = {
    "payload": "hex blob / raw media XML -> [图片]/[表情]/...",
    "email": "e-mail -> <邮箱>",
    "url": "URL / domain -> <链接>",
    "ip": "IPv4 address -> <IP地址>",
    "wxid": "wxid / WeChat id -> <微信号>",
    "chatroom_id": "chatroom id -> <群号>",
    "idcard": "15/18-digit ID number -> <证件号>",
    "bankcard": "bank-card-like number -> <银行卡>",
    "phone": "mobile number -> <手机号>",
    "landline": "landline -> <座机>",
    "qq": "QQ number -> <数字串>",
    "long_token": "long hex/base64/token string -> <长串>",
    "digits": "digit string >= 6 -> <数字串>",
    "name": "known sender name in text -> 用户X",
    "mention": "@known member -> @用户X",
    "mention_unknown": "@unknown name -> @路人XYZ",
    "group_name": "group name -> 示例群N",
    "blocklist": "blocklist word -> <已屏蔽>",
    "bot_name": "bot name -> --bot-alias",
    "system_name": "quoted name in system text -> \"某成员\"",
}

#: Order in which kinds are listed in reports.
KIND_ORDER: tuple[str, ...] = tuple(KIND_LABELS)

LIMITATIONS: tuple[str, ...] = (
    "Real names, nicknames or aliases that never appear as a sender_name of the "
    "same chat are only caught when written as @mention or listed in --blocklist.",
    "Addresses, employers, schools, license plates, dates of birth and other "
    "free-text facts are not detected; skim the output before sharing it.",
    "Numbers shorter than 6 digits and deliberately obfuscated identifiers "
    "(letters or digits split by spaces or symbols, e.g. 'v x 1 2 3') are not "
    "detected; only runs of 7+ Chinese numerals are caught.",
    "Message timing (epoch of every message) and message content semantics are "
    "kept as they are: the data is pseudonymous, not anonymous.",
    "Images and stickers are never exported, only their [图片]/[表情] placeholders.",
)

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def letters_label(index: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA', 27 -> 'AB', 701 -> 'ZZ', 702 -> 'AAA'."""
    if index < 0:
        raise ValueError("index must be >= 0")
    n = index + 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _fixed_letters(value: int, width: int) -> str:
    return "".join(chr(65 + (value // 26 ** k) % 26) for k in range(width))


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def normalize_text(text: str) -> str:
    """Fold full-width digits/letters/@ to ASCII (what the regexes see)."""
    return text.translate(_NORMALIZE_TABLE)


def _same_length_lower(text: str) -> str:
    """``str.lower`` that never changes the string length (index safe)."""
    low = text.lower()
    if len(low) == len(text):
        return low
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


_INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")

# Full-width digits / @ / . / _ / - / + and full-width Latin letters are folded
# to ASCII so that "１３８…" or "ｗｘｉｄ＿…" cannot slip through the regexes.
_NORMALIZE_TABLE: dict[int, int] = {
    **{0xFF10 + i: ord("0") + i for i in range(10)},
    **{0xFF21 + i: ord("A") + i for i in range(26)},
    **{0xFF41 + i: ord("a") + i for i in range(26)},
    0xFF20: ord("@"),
    0xFF0E: ord("."),
    0xFF3F: ord("_"),
    0xFF0D: ord("-"),
    0xFF0B: ord("+"),
}

# ---- replacement sentinels (Supplementary Private Use Area-A) -------------
_S_OPEN = "\U000f0000"
_S_CLOSE = "\U000f0001"
_S_DIGIT0 = 0xF0100
_SENT_RE = re.compile("\U000f0000([\U000f0100-\U000f0109]+)\U000f0001")
_SENT_ANY_RE = re.compile("[\U000f0000-\U000f01ff]")


class _Masker:
    """Swap finished replacements for opaque sentinels until the very end."""

    __slots__ = ("items",)

    def __init__(self) -> None:
        self.items: list[str] = []

    def mask(self, replacement: str) -> str:
        idx = len(self.items)
        self.items.append(replacement)
        return _S_OPEN + "".join(chr(_S_DIGIT0 + int(d)) for d in str(idx)) + _S_CLOSE

    def restore(self, text: str) -> str:
        if _S_OPEN not in text:
            return text

        def _sub(m: re.Match[str]) -> str:
            idx = int("".join(str(ord(c) - _S_DIGIT0) for c in m.group(1)))
            return self.items[idx]

        return _SENT_RE.sub(_sub, text)


# --------------------------------------------------------------------------
# Regular expressions
# --------------------------------------------------------------------------

_URL_BODY = (
    r"[^\s<>\"'`\u3000-\u303f\u4e00-\u9fff\uff00-\uffef\U000f0000-\U000f01ff]*"
)
_URL_TRAIL = ".,;:!?)]}'\"\u2019\u201d"

_DATA_URI_RE = re.compile(r"(?i)data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
_SCHEME_URL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])[a-z][a-z0-9+.\-]{1,19}://" + _URL_BODY
)
_WWW_RE = re.compile(r"(?i)(?<![A-Za-z0-9@._-])www\." + _URL_BODY)
_TLDS = (
    "com|cn|net|org|edu|gov|io|co|me|cc|tv|top|xyz|info|biz|app|vip|club|site|"
    "online|shop|store|tech|ai|dev|cloud|link|live|fun|wang|ink|pro|mobi|hk|tw|"
    "jp|kr|us|uk|ru|de|fr"
)
_DOMAIN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9@._-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:" + _TLDS + r")(?![A-Za-z0-9_-])(?::\d{1,5})?(?:/" + _URL_BODY + r")?"
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_IP_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?!\d|\.\d)")
_WXID_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])wxid_[A-Za-z0-9_-]+")
_GH_RE = re.compile(r"(?<![A-Za-z0-9_])gh_[0-9a-f]{10,}")
_CHATROOM_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{4,}@chatroom(?![A-Za-z0-9_])")

_WX_KEY = r"(?:微信号?|威信|薇信|v信|vx|wx|weixin|wechat|加v)"
_WXID_KW_DELIM_RE = re.compile(
    r"(?i)(" + _WX_KEY + r"\s*[:：=是为]\s*)([A-Za-z][A-Za-z0-9_-]{5,19})(?![A-Za-z0-9_-])"
)
_WXID_KW_BARE_RE = re.compile(
    r"(?i)(" + _WX_KEY + r"\s*)"
    r"([A-Za-z](?=[A-Za-z0-9_-]*[0-9_])[A-Za-z0-9_-]{5,19})(?![A-Za-z0-9_-])"
)
_QQ_RE = re.compile(r"(?i)((?:qq|扣扣|企鹅)(?:号码?|群)?\s*[:：=是为]?\s*)(\d{5,12})(?!\d)")

_IDCARD_RE = re.compile(r"(?<![0-9A-Za-z])(?:\d{17}[\dXx]|\d{15})(?![0-9A-Za-z])")
_BANK_GROUPED_RE = re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{1,7}(?!\d)")
_BANK_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d(?:[ .-]?\d){8}(?!\d)")
_LANDLINE_RE = re.compile(
    r"(?<!\d)(?:\(?0\d{2,3}\)?[ -]?\d{3,4}[ -]?\d{4}(?:-\d{1,5})?"
    r"|[48]00(?:[ -]?\d{3,4}){2})(?!\d)"
)
_DIGITS_RE = re.compile(r"\d{6,}|[零〇一二三四五六七八九幺]{7,}")
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")

# whole-message payload detection
_HEX_FULL_RE = re.compile(r"[0-9a-fA-F]+")
_B64_FULL_RE = re.compile(r"[A-Za-z0-9+/=_-]{120,}")
_XML_START_RE = re.compile(
    r"(?i)^\s*<(?:\?xml|msg|appmsg|emoji|img|voicemsg|videomsg|sysmsg|refermsg)\b"
)
_XML_TAG_RE = re.compile(
    r"(?i)<(?:msg|appmsg|emoji|img|voicemsg|videomsg|sysmsg|refermsg)\b[^>]*>"
)
_XML_HINT_RE = re.compile(r"(?i)\b(?:md5|cdnurl|aeskey|fromusername|cdnthumburl|encryver)\b")
_XML_TITLE_RE = re.compile(r"(?is)<title>\s*(?:<!\[CDATA\[(.*?)\]\]>|(.*?))\s*</title>")
_TYPE_PLACEHOLDERS = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]", 49: "[链接]"}

# system notices carry quoted nicknames: "A"邀请"B"加入了群聊, "A"拍了拍"B", ...
_SYSTEM_TEXT_RE = re.compile(
    r"加入了群聊|加入群聊|邀请.{0,40}加入|拍了拍|移出了群聊|移出群聊|退出了群聊|"
    r"撤回了一条消息|修改群名为|成为.{0,6}群主|设为群管理员|群聊邀请确认"
)
_QUOTED_RE = re.compile(r"[\u201c\"]([^\u201d\"\n]{1,60})[\u201d\"]")

# mention handling
_MENTION_STOP = frozenset(
    " \t\r\n\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u3000"
    "@,;:!?()[]{}<>\"'`/\\|~"
    "，。！？；：、（）【】《》“”‘’…～—·"
)
_MENTION_MAX = 40
_GENERIC_MENTIONS = ("所有人", "全体成员", "everyone", "all")

# Names that are too generic to replace outside an explicit @mention.
_GENERIC_NAMES = frozenset({
    "群友", "群主", "管理员", "匿名", "微信用户", "用户", "大家", "所有人", "全体成员",
    "机器人", "我", "你", "他", "她", "它", "本人", "自己",
    "好的", "好吧", "哈哈", "哈哈哈", "呵呵", "嘿嘿", "嗯嗯", "哦哦", "收到", "谢谢",
    "晚安", "早安", "你好", "在吗", "ok", "okay", "hi", "hello", "bot", "admin",
})


def looks_like_blob(text: str, min_hex_len: int = 80) -> bool:
    """True if *text* is a raw hex payload (the bot uses the same 80-char rule)."""
    compact = re.sub(r"\s+", "", text or "")
    if not compact or not _HEX_FULL_RE.fullmatch(compact):
        return False
    if compact.lower().startswith("28b52ffd") and len(compact) >= 16:
        return True  # zstd frame magic, as produced by WCDB for quote messages
    return len(compact) >= min_hex_len


def _unwrap_payload(text: str, msg_type: int) -> tuple[str, bool] | None:
    """Detect whole-message raw payloads.

    Returns ``(new_text, final)``: *final* means ``new_text`` is a finished
    placeholder, otherwise it is extracted text that still needs scrubbing.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if looks_like_blob(stripped):
        return PLACEHOLDER_IMAGE, True
    compact = re.sub(r"\s+", "", stripped)
    if _B64_FULL_RE.fullmatch(compact):
        return PLACEHOLDER_IMAGE, True
    if _XML_START_RE.match(stripped) or (
        _XML_TAG_RE.search(stripped) and _XML_HINT_RE.search(stripped)
    ):
        title = _XML_TITLE_RE.search(stripped)
        if title:
            extracted = html.unescape((title.group(1) or title.group(2) or "").strip())
            if extracted:
                return extracted, False
        return _TYPE_PLACEHOLDERS.get(msg_type, PLACEHOLDER_IMAGE), True
    return None


def parse_blocklist(lines: Iterable[str]) -> list[str]:
    """One word per line; blank lines and ``#`` comments are ignored."""
    words: list[str] = []
    for line in lines:
        word = line.strip().lstrip("\ufeff")
        if word and not word.startswith("#"):
            words.append(word)
    return words


def parse_group_names(data: object) -> dict[str, str]:
    """Accept ``{chat_id: "name"}`` or ``{chat_id: {"name": ...}}`` (group_names.json)."""
    result: dict[str, str] = {}
    if not isinstance(data, Mapping):
        return result
    for chat_id, value in data.items():
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("nickname") or ""
        if isinstance(value, str) and value.strip():
            result[str(chat_id)] = value.strip()
    return result


# --------------------------------------------------------------------------
# Context-free regex scrubbing (shared by Redactor and scrub_text)
# --------------------------------------------------------------------------


def _mask_all(regex: re.Pattern[str], text: str, masker: _Masker,
              kinds: Counter, kind: str) -> str:
    token = TOKENS[kind]

    def _sub(_m: re.Match[str]) -> str:
        kinds[kind] += 1
        return masker.mask(token)

    return regex.sub(_sub, text)


def _mask_url_like(regex: re.Pattern[str], text: str, masker: _Masker,
                   kinds: Counter) -> str:
    def _sub(m: re.Match[str]) -> str:
        raw = m.group(0)
        core = raw.rstrip(_URL_TRAIL)
        kinds["url"] += 1
        return masker.mask(TOKENS["url"]) + raw[len(core):]

    return regex.sub(_sub, text)


def _mask_keyword_value(regex: re.Pattern[str], text: str, masker: _Masker,
                        kinds: Counter, kind: str) -> str:
    token = TOKENS[kind]

    def _sub(m: re.Match[str]) -> str:
        kinds[kind] += 1
        return m.group(1) + masker.mask(token)

    return regex.sub(_sub, text)


def _mask_long_tokens(text: str, masker: _Masker, kinds: Counter) -> str:
    def _sub(m: re.Match[str]) -> str:
        raw = m.group(0)
        if any(c.isdigit() for c in raw) and any(c.isalpha() for c in raw):
            kinds["long_token"] += 1
            return masker.mask(TOKENS["long_token"])
        return raw

    return _LONG_TOKEN_RE.sub(_sub, text)


def _pre_rules(text: str, masker: _Masker, kinds: Counter) -> str:
    """Rules that must run before name replacement (URLs, e-mails, ids)."""

    def _data(_m: re.Match[str]) -> str:
        kinds["payload"] += 1
        return masker.mask(PLACEHOLDER_IMAGE)

    text = _DATA_URI_RE.sub(_data, text)
    text = _mask_url_like(_SCHEME_URL_RE, text, masker, kinds)
    text = _mask_all(_EMAIL_RE, text, masker, kinds, "email")
    text = _mask_url_like(_WWW_RE, text, masker, kinds)
    text = _mask_url_like(_DOMAIN_RE, text, masker, kinds)
    text = _mask_all(_IP_RE, text, masker, kinds, "ip")
    text = _mask_all(_WXID_RE, text, masker, kinds, "wxid")
    text = _mask_all(_GH_RE, text, masker, kinds, "wxid")
    text = _mask_all(_CHATROOM_RE, text, masker, kinds, "chatroom_id")
    text = _mask_keyword_value(_WXID_KW_DELIM_RE, text, masker, kinds, "wxid")
    text = _mask_keyword_value(_WXID_KW_BARE_RE, text, masker, kinds, "wxid")
    return text


def _post_rules(text: str, masker: _Masker, kinds: Counter) -> str:
    """Numeric rules; they run after name replacement so nicknames survive."""
    # long tokens first: digits inside a hex/base64/API-key blob must not be
    # carved up by the shorter numeric rules.
    text = _mask_long_tokens(text, masker, kinds)
    text = _mask_all(_IDCARD_RE, text, masker, kinds, "idcard")
    text = _mask_all(_BANK_GROUPED_RE, text, masker, kinds, "bankcard")
    text = _mask_all(_BANK_RE, text, masker, kinds, "bankcard")
    text = _mask_all(_MOBILE_RE, text, masker, kinds, "phone")
    text = _mask_all(_LANDLINE_RE, text, masker, kinds, "landline")
    text = _mask_keyword_value(_QQ_RE, text, masker, kinds, "qq")
    text = _mask_all(_DIGITS_RE, text, masker, kinds, "digits")
    return text


def scrub_text(text: str, kinds: Counter | None = None) -> str:
    """Regex-only scrubbing (no roster, no mentions); handy for unit tests."""
    local = kinds if kinds is not None else Counter()
    masker = _Masker()
    out = _SENT_ANY_RE.sub("", str(text or "")).translate(_NORMALIZE_TABLE)
    out = _pre_rules(out, masker, local)
    out = _post_rules(out, masker, local)
    return masker.restore(out)


# --------------------------------------------------------------------------
# Redactor
# --------------------------------------------------------------------------


class RedactedMessage(NamedTuple):
    """Result of :meth:`Redactor.redact_message`."""

    text: str
    msg_type: int
    kinds: Counter


class _Entry(NamedTuple):
    name: str          # lower-cased match string
    replacement: str   # what the match turns into
    kind: str          # stats key for a bare occurrence
    mention_kind: str  # stats key for an @occurrence
    bare_ok: bool      # may be replaced outside an @mention
    guard: bool        # require ASCII word boundaries at ASCII edges


@dataclass
class _Chat:
    chat_id: str
    is_group: bool
    index: int
    first_ts: int
    labels: dict[str, str] = field(default_factory=dict)         # sender_id -> 用户X
    names: dict[str, str] = field(default_factory=dict)          # lower name -> 用户X
    id_hashes: dict[str, str] = field(default_factory=dict)      # sender_id -> u_xxx
    id_owner: dict[str, str] = field(default_factory=dict)       # u_xxx -> sender_id
    unknown: dict[str, str] = field(default_factory=dict)        # lower name -> 路人X
    unknown_used: dict[str, str] = field(default_factory=dict)   # 路人X -> lower name
    next_label: int = 0
    index_cache: dict[str, list[_Entry]] | None = None


class Redactor:
    """Stateful de-identifier; one instance per export run."""

    def __init__(self, *, salt: str | None = None, bot_name: str = "",
                 bot_alias: str = "", blocklist: Iterable[str] = (),
                 group_names: Mapping[str, str] | None = None,
                 other_names: Iterable[str] = ()) -> None:
        self.salt_was_generated = not salt
        self._salt = str(salt) if salt else secrets.token_hex(16)
        self.bot_name = (bot_name or "").strip()
        #: name written to the output; the real name when no alias is given
        self.bot_alias = (bot_alias or "").strip() or self.bot_name
        self._bot_lower = _same_length_lower(self.bot_name)
        self._bot_re = (
            re.compile("@" + re.escape(self.bot_name), re.IGNORECASE)
            if self.bot_name else None
        )
        # Other names the group calls the bot by (its WeChat nickname, BOT_ALIASES):
        # "@qier" is "@<bot>" in the output, and a bare one is the bot's name too.
        others = sorted({n.strip() for n in other_names if n and n.strip() and n.strip() != self.bot_name},
                        key=len, reverse=True)
        self._other_lower = {_same_length_lower(n) for n in others}
        self._other_re = (
            re.compile("@(?:" + "|".join(re.escape(n) for n in others) + ")", re.IGNORECASE)
            if others else None
        )
        words = {_same_length_lower(_INVISIBLE_RE.sub("", w).strip())
                 for w in blocklist}
        self._blocklist = sorted(w for w in words if w)
        self._group_names = {
            cid: _same_length_lower(_INVISIBLE_RE.sub("", name).strip())
            for cid, name in parse_group_names(group_names or {}).items()
        }
        self._chats: dict[str, _Chat] = {}
        self._pending_chats: dict[str, int] = {}
        self._pending_senders: list[tuple[int, str, str, str]] = []
        self._next_group = 1
        self._next_private = 1
        self.stats: Counter = Counter()

    def __repr__(self) -> str:  # never leak the salt through a repr
        return f"Redactor(chats={len(self._chats)}, bot_name={self.bot_name!r})"

    # ----------------------------------------------------------- hashing
    def _digest(self, *parts: str, n: int = 8) -> str:
        h = hashlib.sha256()
        h.update(self._salt.encode("utf-8"))
        for part in parts:
            h.update(b"\x1f")
            h.update(part.encode("utf-8", "surrogatepass"))
        return h.hexdigest()[:n]

    # ---------------------------------------------------- registration
    def register_chat(self, chat_id: str, first_ts: int = 0) -> None:
        """Declare a chat (``first_ts`` fixes the order of 示例群N)."""
        chat_id = str(chat_id)
        if chat_id in self._chats:
            return
        known = self._pending_chats.get(chat_id)
        self._pending_chats[chat_id] = (
            first_ts if known is None else min(known, first_ts)
        )

    def register_sender(self, chat_id: str, sender_id: str, sender_name: str = "",
                        first_ts: int = 0) -> None:
        """Declare a sender (``first_ts`` fixes the order of 用户A, 用户B, ...)."""
        chat_id = str(chat_id)
        self.register_chat(chat_id, first_ts)
        self._pending_senders.append(
            (int(first_ts), chat_id, str(sender_id), str(sender_name or ""))
        )

    def _flush(self) -> None:
        if self._pending_chats:
            pending = sorted((ts, cid) for cid, ts in self._pending_chats.items())
            self._pending_chats = {}
            for first_ts, cid in pending:
                if cid not in self._chats:
                    self._create_chat(cid, first_ts)
        if self._pending_senders:
            senders = sorted(self._pending_senders)
            self._pending_senders = []
            for _ts, cid, sid, name in senders:
                st = self._chats[cid]
                self._ensure_label(st, sid)
                self._remember_name(st, sid, name)

    def _create_chat(self, chat_id: str, first_ts: int) -> _Chat:
        is_group = chat_id.endswith("@chatroom")
        if is_group:
            index, self._next_group = self._next_group, self._next_group + 1
        else:
            index, self._next_private = self._next_private, self._next_private + 1
        st = _Chat(chat_id=chat_id, is_group=is_group, index=index, first_ts=first_ts)
        self._chats[chat_id] = st
        # group-name entries refer to chat indexes: invalidate every scan index
        for other in self._chats.values():
            other.index_cache = None
        return st

    def _chat(self, chat_id: str) -> _Chat:
        chat_id = str(chat_id)
        self._flush()
        st = self._chats.get(chat_id)
        if st is None:
            st = self._create_chat(chat_id, 0)
        return st

    def _ensure_label(self, st: _Chat, sender_id: str) -> str:
        label = st.labels.get(sender_id)
        if label is None:
            label = "用户" + letters_label(st.next_label)
            st.next_label += 1
            st.labels[sender_id] = label
            st.index_cache = None  # sender ids are part of the scan index
        return label

    # -------------------------------------------------- public identities
    def chat_info(self, chat_id: str) -> dict[str, object]:
        """Pseudonymous ``{"chat_id", "group_name", "is_group"}`` for a chat."""
        st = self._chat(chat_id)
        if st.is_group:
            pseudo_id = "g_" + self._digest("chat", st.chat_id) + "@chatroom"
            name = f"示例群{st.index}"
        else:
            pseudo_id = "p_" + self._digest("chat", st.chat_id)
            name = f"示例私聊{st.index}"
        return {"chat_id": pseudo_id, "group_name": name, "is_group": st.is_group}

    def sender_label(self, chat_id: str, sender_id: str, sender_name: str = "") -> str:
        """Stable per-chat pseudonym ('用户A') for one sender."""
        st = self._chat(chat_id)
        sender_id = str(sender_id)
        label = self._ensure_label(st, sender_id)
        self._remember_name(st, sender_id, sender_name)
        return label

    def _remember_name(self, st: _Chat, sender_id: str, sender_name: str) -> None:
        clean = _INVISIBLE_RE.sub("", sender_name or "").strip()
        if not clean:
            return
        key = _same_length_lower(clean)
        if key not in st.names:
            st.names[key] = st.labels[sender_id]
            st.index_cache = None

    def sender_hash(self, chat_id: str, sender_id: str) -> str:
        """``u_`` + short salted hash of the raw sender id (per chat)."""
        st = self._chat(chat_id)
        sender_id = str(sender_id)
        cached = st.id_hashes.get(sender_id)
        if cached is not None:
            return cached
        width = 8
        while True:
            candidate = "u_" + self._digest("sender", st.chat_id, sender_id, n=width)
            owner = st.id_owner.get(candidate)
            if owner is None or owner == sender_id:
                break
            width += 2
        st.id_hashes[sender_id] = candidate
        st.id_owner[candidate] = sender_id
        return candidate

    # ------------------------------------------------------ name scanning
    def _unknown_label(self, st: _Chat, key: str) -> str:
        label = st.unknown.get(key)
        if label is not None:
            return label
        width = 3
        while True:
            value = int(self._digest("mention", st.chat_id, key, n=16), 16)
            label = "路人" + _fixed_letters(value, width)
            if st.unknown_used.get(label, key) == key:
                break
            width += 1
        st.unknown[key] = label
        st.unknown_used[label] = key
        return label

    def _scan_index(self, st: _Chat) -> dict[str, list[_Entry]]:
        if st.index_cache is not None:
            return st.index_cache
        entries: dict[str, _Entry] = {}

        def put(name: str, replacement: str, kind: str, mention_kind: str,
                bare_ok: bool, guard: bool) -> None:
            if name:
                entries[name] = _Entry(name, replacement, kind, mention_kind,
                                       bare_ok, guard)

        # real group names -> 示例群N (chats unknown to the export are blocked)
        for cid, gname in self._group_names.items():
            other = self._chats.get(cid)
            if other is not None:
                repl = ("示例群" if other.is_group else "示例私聊") + str(other.index)
                put(gname, repl, "group_name", "group_name", len(gname) >= 2, True)
            else:
                put(gname, TOKENS["blocklist"], "group_name", "group_name",
                    len(gname) >= 2, True)
        # raw sender ids typed into text
        for sid in st.labels:
            low = _same_length_lower(_INVISIBLE_RE.sub("", sid).strip())
            if len(low) >= 5:
                put(low, TOKENS["wxid"], "wxid", "wxid", True, True)
        # known sender names (nicknames)
        for name, label in st.names.items():
            bare_ok = (
                len(name) >= 2
                and any(c.isalpha() for c in name)
                and name not in _GENERIC_NAMES
                and name != self._bot_lower
                and name not in self._other_lower
            )
            put(name, label, "name", "mention", bare_ok, True)
        # the bot's own name is kept, or swapped for its alias when one is set
        if self._bot_lower and self.bot_alias != self.bot_name:
            put(self._bot_lower, self.bot_alias, "bot_name", "bot_name", True, True)
        for other in self._other_lower:
            put(other, self.bot_alias, "bot_name", "bot_name", True, True)
        # blocklist wins over everything else
        for word in self._blocklist:
            put(word, TOKENS["blocklist"], "blocklist", "blocklist", True, False)

        index: dict[str, list[_Entry]] = {}
        for entry in entries.values():
            index.setdefault(entry.name[0], []).append(entry)
        for bucket in index.values():
            bucket.sort(key=lambda e: (-len(e.name), e.name))
        st.index_cache = index
        return index

    @staticmethod
    def _is_ascii_word(ch: str) -> bool:
        return ch.isascii() and (ch.isalnum() or ch == "_")

    def _match(self, index: dict[str, list[_Entry]], low: str, text: str,
               pos: int, mention: bool) -> tuple[_Entry, int] | None:
        if pos >= len(low):
            return None
        for entry in index.get(low[pos], ()):
            if not low.startswith(entry.name, pos):
                continue
            if not mention and not entry.bare_ok:
                continue
            end = pos + len(entry.name)
            if entry.guard:
                if (not mention and pos > 0
                        and self._is_ascii_word(entry.name[0])
                        and self._is_ascii_word(text[pos - 1])):
                    continue
                if (end < len(text) and self._is_ascii_word(entry.name[-1])
                        and self._is_ascii_word(text[end])):
                    continue
            return entry, end
        return None

    def _scan_names(self, st: _Chat, text: str, masker: _Masker,
                    kinds: Counter) -> str:
        if not text:
            return text
        index = self._scan_index(st)
        if not index and "@" not in text:
            return text
        low = _same_length_lower(text)
        n = len(text)
        out: list[str] = []
        i = 0
        while i < n:
            ch = text[i]
            if ch == "@":
                hit = self._match(index, low, text, i + 1, mention=True)
                if hit is not None:
                    entry, end = hit
                    kinds[entry.mention_kind] += 1
                    out.append("@")
                    out.append(masker.mask(entry.replacement))
                    i = end
                    continue
                j = i + 1
                limit = min(n, i + 1 + _MENTION_MAX)
                while j < limit and text[j] not in _MENTION_STOP \
                        and not (_S_OPEN <= text[j] <= "\U000f01ff"):
                    j += 1
                while j > i + 1 and text[j - 1] in ".-_":
                    j -= 1
                if j > i + 1:
                    name_low = low[i + 1:j]
                    # "@all" must not swallow "@allen"; CJK tokens may be followed
                    # directly by text ("@所有人注意"), ASCII ones may not.
                    generic = next(
                        (g for g in _GENERIC_MENTIONS
                         if name_low == g or (not g.isascii() and name_low.startswith(g))),
                        None,
                    )
                    if generic is not None:
                        end = i + 1 + len(generic)
                        out.append(text[i:end])
                        i = end
                        continue
                    kinds["mention_unknown"] += 1
                    out.append("@")
                    out.append(masker.mask(self._unknown_label(st, name_low)))
                    i = j
                    continue
                out.append("@")
                i += 1
                continue
            hit = self._match(index, low, text, i, mention=False)
            if hit is not None:
                entry, end = hit
                kinds[entry.kind] += 1
                out.append(masker.mask(entry.replacement))
                i = end
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    # ----------------------------------------------------- text redaction
    def redact_message(self, chat_id: str, content: object,
                       msg_type: object = 1) -> RedactedMessage:
        """De-identify one message body; also normalises blob types to 3."""
        kinds: Counter = Counter()
        out_type = _as_int(msg_type, 1)
        raw = "" if content is None else str(content)
        text = _SENT_ANY_RE.sub("", raw).translate(_NORMALIZE_TABLE)

        payload = _unwrap_payload(text, out_type)
        if payload is not None:
            text, final = payload
            kinds["payload"] += 1
            if final:
                if out_type == 1 and text == PLACEHOLDER_IMAGE:
                    out_type = 3  # same normalisation the bot applies to blobs
                self.stats.update(kinds)
                return RedactedMessage(text, out_type, kinds)

        st = self._chat(chat_id)
        masker = _Masker()

        if out_type == 10000 or _SYSTEM_TEXT_RE.search(text):
            def _quoted(m: re.Match[str]) -> str:
                kinds["system_name"] += 1
                return masker.mask("“某成员”")
            text = _QUOTED_RE.sub(_quoted, text)

        text = _pre_rules(text, masker, kinds)
        if self._bot_re is not None:
            # "@bot" is what the replay keys on: keep it, or write the alias
            aliased = self.bot_alias != self.bot_name

            def _bot_mention(m: re.Match[str]) -> str:
                if aliased:
                    kinds["bot_name"] += 1
                    return masker.mask("@" + self.bot_alias)
                return masker.mask(m.group(0))

            text = self._bot_re.sub(_bot_mention, text)
        if self._other_re is not None:
            def _other_mention(m: re.Match[str]) -> str:
                kinds["bot_name"] += 1
                return masker.mask("@" + self.bot_alias)

            text = self._other_re.sub(_other_mention, text)
        text = self._scan_names(st, text, masker, kinds)
        text = _post_rules(text, masker, kinds)
        text = masker.restore(text)

        self.stats.update(kinds)
        return RedactedMessage(text, out_type, kinds)

    def redact_text(self, chat_id: str, content: object) -> str:
        """Convenience wrapper returning only the redacted text."""
        return self.redact_message(chat_id, content, 1).text
