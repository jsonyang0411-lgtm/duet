"""Text rules for one outgoing chat message (pure functions, no I/O).

Two groups live here:

* helpers shared with the legacy cleaner in ``summarize/base.py``
  (speaker-prefix stripping, clause-aware truncation);
* the *last-line defenses* of the structured pipeline.  In the envelope the
  decision to stay silent is carried by ``action``, so these rules only have
  to catch a model that put a silence marker or an explanation into the text
  it wants to send.  They match whole messages, never substrings, so a normal
  sentence such as "你不用回复我哦" is left alone.
"""

import re
import unicodedata
from typing import Iterable

# Labels a model sometimes puts in front of its reply ("回复：好的").
OUTPUT_LABELS = frozenset({
    "回复", "回答", "输出", "微信回复", "我", "机器人", "助手", "小助手",
    "ai", "bot", "assistant",
})
# Characters that end a sentence / a clause when a long reply must be cut.
SENTENCE_END = "。！？!?…"
CLAUSE_BREAK = "，、；,;"


def strip_speaker_prefix(text: str, names: Iterable[str] = ()) -> str:
    """Drop a leading ``name：`` label the model copied from the chat log.

    Only the speaker names we know (persona, bot, requester) and a few
    output labels are removed.  Anything else before a colon is content:
    "晚上7:30见", "https://x", "地址：科技路88号" must survive.
    """
    match = re.match(r"^\s*([^\n:：]{1,12})[：:]\s*", text)
    if not match:
        return text
    known = OUTPUT_LABELS | {
        str(name).strip().casefold() for name in names if str(name or "").strip()
    }
    if match.group(1).strip().casefold() in known:
        return text[match.end():].strip()
    return text


def truncate_at_boundary(text: str, max_chars: int) -> str:
    """Shorten ``text`` to ``max_chars`` without cutting a clause in half.

    Cuts at the last sentence end / clause break inside the budget when
    that still keeps at least half of it; otherwise falls back to a hard
    cut (e.g. a run of "哈" has no boundary to cut at).
    """
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    for index in range(len(head) - 1, -1, -1):
        char = head[index]
        if char in SENTENCE_END:
            kept = head[:index + 1]
        elif char in CLAUSE_BREAK:
            kept = head[:index]
        else:
            continue
        if len(kept) * 2 >= max_chars:
            return kept.rstrip()
        break
    return head.rstrip()


_HEX_BLOB_RE = re.compile(r"^[0-9a-fA-F]{80,}$")


def sanitize_context_content(content: object) -> str:
    """A context line as the model should see it: raw media blobs become [图片]."""
    text = str(content or "").strip()
    compact = re.sub(r"\s+", "", text)
    if _HEX_BLOB_RE.fullmatch(compact):
        return "[图片]"
    return text


# ── last-line defenses ──────────────────────────────────────────────

_PAREN_ONLY_RE = re.compile(r"(?:[（(【][^（）()【】]{1,180}[）)】]\s*)+")
_PUNCT_ONLY_RE = re.compile(r"[\s.。…!！?？,，、~～\-—_·]+")
_DANGLING_RE = re.compile(r"(?:】【|\[\])+$")
_TRIM_EDGES = " \t\r\n()（）[]【】「」『』\"'“”‘’<>《》.。!！,，、:：;；"

# A message that is nothing but one of these (after trimming brackets and
# punctuation) is a silence marker, not something to send.
_SILENCE_MARKERS = frozenset({
    "沉默", "保持沉默", "应该沉默", "继续沉默", "静默", "保持静默",
    "保持安静", "保持安静就好",
    "空白", "回复空白", "输出空白", "留空",
    "不回复", "不回应", "不回答", "无回复", "无需回复", "无需回应",
    "不用回复", "不用回应", "不需要回复", "不需要回应", "不必回复",
    "不予回复", "不予回应",
    "silent", "silence", "blank", "null", "none",
    "noreply", "no_reply", "no reply", "n/a",
})

# Explanations of why the model is not answering ("这句话不是对我说的，所以我
# 选择不说话").  They need ALL of: a first-person decision, a restraint phrase
# and a reference to the conversation being third-party.  Ordinary sentences
# ("他应该是沉默了吧", "不用回复我哈", "那我就不打扰你们了") miss at least one.
_DECISION_RE = re.compile(
    r"我(?:就|还是|选择|决定|应该|会|先)|所以我|因此我|那我|我这边"
)
_RESTRAINT_RE = re.compile(
    r"不(?:说话|打扰|插话|插嘴|回复|回应|回答|发言|出声)|保持(?:沉默|安静|静默)|"
    r"选择沉默|不予(?:回复|回应)|沉默(?:就好|为好|比较好)"
)
_THIRD_PARTY_RE = re.compile(
    r"他们|她们|这句话|这条消息|这句|不是(?:在)?对我|没有(?:明确)?(?:指向|针对)我|"
    r"不关我|与我无关|群友|大家在"
)


# Spoofed or sloppy forms of the same markers: full-width letters
# ("ＮＯ＿ＲＥＰＬＹ"), zero-width characters inside a word ("沉\u200b默").  The rules
# match on a normalised copy; the text that is sent keeps its own characters
# (apart from the zero-width ones).
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")

# A message that opens with a silence token followed by punctuation is a marker
# plus an explanation ("silent，他们在互相聊", "沉默…"), not a sentence.
_LEADING_MARKER_RE = re.compile(
    r"(?:silent|silence|no[_ ]?reply|沉默|保持沉默|不回复)\s*[，,。.…:：;；—-]", re.IGNORECASE,
)
# The model echoing its own output format instead of a message.
_PROTOCOL_ECHO_RE = re.compile(
    r"\s*(?:[{\[]\s*)?\"?(?:action|addressee|messages|reason)\"?\s*[:：=]", re.IGNORECASE,
)
# "not meant for me" + "so I do not reply" is a pair nobody says to a group.
_NOT_ME_RE = re.compile(
    r"不是(?:在)?对我|没有(?:明确)?(?:指向|针对)我|与我无关|和我无关|跟我无关|不关我(?:的事)?"
)


def _probe(text: object) -> str:
    return unicodedata.normalize("NFKC", _ZERO_WIDTH_RE.sub("", str(text or "")))


def collapse_whitespace(text: str) -> str:
    """One WeChat bubble is one line: newlines and runs of spaces become ' '."""
    return re.sub(r"\s+", " ", str(text or "").replace("\r", " ")).strip()


def strip_dangling_markers(text: str) -> str:
    """Remove a stray ``】【`` / ``[]`` some relay models leave at the end."""
    return _DANGLING_RE.sub("", text).rstrip(" ，,、;；")


def is_punctuation_only(text: str) -> bool:
    return bool(text) and _PUNCT_ONLY_RE.fullmatch(text) is not None


def is_silence_marker(text: str) -> bool:
    """Whole-message silence markers: "沉默", "(不回复)", "NO_REPLY", "[无回复]"."""
    raw = _probe(text).strip()
    if not raw:
        return False
    if _PAREN_ONLY_RE.fullmatch(raw):
        return True
    if _LEADING_MARKER_RE.match(raw):
        return True
    trimmed = raw.strip(_TRIM_EDGES).casefold()
    return trimmed in _SILENCE_MARKERS


def is_protocol_echo(text: str) -> bool:
    """The answer format written out as text: ``{"action":"silent"}``, ``action: reply``."""
    return _PROTOCOL_ECHO_RE.match(_probe(text)) is not None


def is_silence_explanation(text: str) -> bool:
    """An explanation of the silence choice instead of a message to send."""
    raw = _probe(text)
    if _NOT_ME_RE.search(raw) and _RESTRAINT_RE.search(raw):
        return True
    return bool(
        _DECISION_RE.search(raw)
        and _RESTRAINT_RE.search(raw)
        and _THIRD_PARTY_RE.search(raw)
    )


# Stage directions the persona forbids: "（低下头）好呀", "好的(点头)".  Content
# in parentheses ("（下午三点后）", "（v2.3）") has digits, numerals or Latin
# letters inside; those are kept.
_ASIDE_RE = re.compile(
    r"\s*[（(][^（）()A-Za-z0-9一二三四五六七八九十百千万两]{1,12}[）)]\s*"
)


def strip_asides(text: str) -> str:
    """Remove short stage directions at the start or end of a message."""
    out = text
    head = re.match(_ASIDE_RE.pattern, out)
    if head:
        out = out[head.end():]
    tail = re.search(_ASIDE_RE.pattern + r"$", out)
    if tail and tail.start() > 0:
        out = out[:tail.start()]
    return re.sub(r"\s+", " ", out).strip()


def finalize_message(text: str, *, max_chars: int,
                     names: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Make one message sendable, or return ``""`` when it must not be sent.

    Returns ``(text, warnings)``.  Warnings name every rule that fired so the
    caller can count them: ``silence_marker``, ``silence_explanation``,
    ``punctuation_only``, ``aside_removed``, ``prefix_removed``,
    ``dangling_removed``, ``truncated``.
    """
    warnings: list[str] = []
    out = collapse_whitespace(_ZERO_WIDTH_RE.sub("", str(text or "")))
    if not out:
        return "", warnings
    if is_protocol_echo(out):
        return "", ["protocol_echo"]
    if is_silence_marker(out):
        return "", ["silence_marker"]
    stripped = strip_speaker_prefix(out, names).strip()
    if stripped != out:
        warnings.append("prefix_removed")
        out = stripped
    cleaned = strip_dangling_markers(out)
    if cleaned != out:
        warnings.append("dangling_removed")
        out = cleaned
    without_asides = strip_asides(out)
    if without_asides != out:
        warnings.append("aside_removed")
        out = without_asides
    if not out:
        return "", warnings + ["silence_marker"]
    if is_silence_marker(out):
        return "", warnings + ["silence_marker"]
    if is_protocol_echo(out):
        return "", warnings + ["protocol_echo"]
    if is_punctuation_only(out):
        return "", warnings + ["punctuation_only"]
    if is_silence_explanation(out):
        return "", warnings + ["silence_explanation"]
    shortened = truncate_at_boundary(out, max_chars)
    if shortened != out:
        warnings.append("truncated")
        out = shortened
    return out, warnings
