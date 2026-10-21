"""Willingness score: is this a good moment to join the conversation?

A deterministic stand-in for the old random dice.  The group's activity mode
gives a base willingness (the probability the dice used to have); features of
the message and of the bot's own recent behaviour move it up or down in
log-odds space.  The result is a probability-like score in (0, 1) that the gate
compares with a threshold.

No I/O, no clock, no randomness: the same inputs always give the same score,
and ``contributions`` says why.
"""

import math
import re
from dataclasses import dataclass
from typing import Optional

# Log-odds weights.  Hand-set and deliberately small in number.
W_QUESTION = 0.8          # this message asks something
W_INFO_SEEKING = 1.0      # ... and wants information the bot may be able to give
W_OPEN_QUESTION = 1.2     # someone else's question is still unanswered
W_SUBSTANTIAL = 0.4       # long enough to carry content
W_TOO_SHORT = -1.5        # "嗯", "哈哈", "?"
W_REPEAT = -2.0           # echoing what someone just said (复读)
W_SPOKE_RECENTLY = -1.5   # the bot spoke within RECENT_SHORT_SEC
W_SPOKE_LATELY = -0.7     # ... or within RECENT_LONG_SEC
W_MEDIA_ONLY = -3.0       # picture / sticker / voice without text

RECENT_SHORT_SEC = 90.0
RECENT_LONG_SEC = 300.0
SUBSTANTIAL_CHARS = 10
SHORT_CHARS = 2

_QUESTION_RE = re.compile(
    r"[?？]|吗$|嘛$|么$|什么|怎么|怎样|为什么|为啥|哪个|哪里|哪儿|谁|多少|几点|"
    r"是不是|有没有|能不能|可不可以|要不要|好不好|行不行|对不对|咋|推荐"
)
# A question about facts or how-to ("怎么把列表去重", "谁知道哪家好") — something
# the bot can help with — as opposed to a social one ("晚上有人打游戏吗").
_INFO_RE = re.compile(
    r"怎么(?!样)|如何|为什么|为啥|什么意思|是什么|是啥|哪个|哪家|哪里|哪儿|多少|区别|推荐|"
    r"谁知道|谁懂|有没有人(?:知道|懂|会)|(?:知道|懂|会).*[吗么嘛？?]"
)
_MEDIA_RE = re.compile(r"^\s*\[(?:图片|表情|视频|语音|动画表情|文件|位置)\]\s*$")
_REACTIONS = frozenset({
    "嗯", "哦", "噢", "啊", "额", "呃", "哈", "哈哈", "哈哈哈", "呵呵", "笑死", "绷不住",
    "牛", "6", "66", "666", "对", "是", "好", "行", "ok", "okay", "确实", "同意",
    "不是", "不要", "可以", "没事", "寄", "草", "艹", "。。。", "...", "？", "?",
})
# Laughter is recognised by deleting known tokens and seeing whether anything is
# left, never with a repeated group around a repeated class: "(?:哈+|…)+" backtracks
# exponentially on "哈哈哈…哈你太搞笑了", and one such message would freeze the bot.
_LAUGH_WORDS_RE = re.compile(r"笑死我了|笑死|绷不住了|绷不住|xswl|[哈呵嘿嘻草艹牛]")
_LAUGH_REPEAT_RE = re.compile(r"2?3{2,}|6{2,}|w{2,}|h{2,}|nb")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def compact(text: str) -> str:
    """Lower-cased text without spaces and punctuation, for comparisons."""
    return _PUNCT_RE.sub("", str(text or "")).casefold()


# The question rules are scanned with patterns that are quadratic in the worst
# case ("谁" followed by thousands of "会"); a question fits in a few hundred
# characters, so that is all they read.
MAX_FEATURE_CHARS = 300


def looks_like_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(str(text or "")[:MAX_FEATURE_CHARS]))


def looks_info_seeking(text: str) -> bool:
    """A question the bot could actually help with."""
    text = str(text or "")[:MAX_FEATURE_CHARS]
    return bool(_QUESTION_RE.search(text) and _INFO_RE.search(text))


def is_media_only(text: str, msg_type: int = 1) -> bool:
    return msg_type in (3, 34, 43, 47) or bool(_MEDIA_RE.match(str(text or "")))


def is_laughter(flat: str) -> bool:
    """Only laughter: "哈哈哈", "笑死我了", "xswl", "233333", "666"."""
    if not flat:
        return False
    if _LAUGH_REPEAT_RE.fullmatch(flat):
        return True
    return not _LAUGH_WORDS_RE.sub("", flat)


def is_too_short(text: str) -> bool:
    flat = compact(text)
    if not flat:
        return True
    return (
        len(flat) <= SHORT_CHARS
        or flat in _REACTIONS
        or str(text).strip().casefold() in _REACTIONS
        or is_laughter(flat)
    )


@dataclass(frozen=True)
class Features:
    base: float                                   # the mode's base willingness, 0..1
    is_question: bool = False
    info_seeking: bool = False
    open_question: bool = False
    too_short: bool = False
    media_only: bool = False
    repeat: bool = False
    substantial: bool = False
    seconds_since_bot_spoke: Optional[float] = None


def _logit(probability: float) -> float:
    p = min(max(probability, 0.01), 0.99)
    return math.log(p / (1.0 - p))


def score(features: Features) -> tuple[float, dict[str, float]]:
    """Return ``(score, contributions)``; ``contributions`` are log-odds terms."""
    contributions: dict[str, float] = {"base": round(_logit(features.base), 3)}

    def add(name: str, active: bool, weight: float) -> None:
        if active:
            contributions[name] = weight

    add("question", features.is_question, W_QUESTION)
    add("info_seeking", features.info_seeking, W_INFO_SEEKING)
    add("open_question", features.open_question, W_OPEN_QUESTION)
    add("substantial", features.substantial and not features.too_short, W_SUBSTANTIAL)
    add("too_short", features.too_short, W_TOO_SHORT)
    add("repeat", features.repeat, W_REPEAT)
    add("media_only", features.media_only, W_MEDIA_ONLY)
    since = features.seconds_since_bot_spoke
    if since is not None:
        if since < RECENT_SHORT_SEC:
            contributions["spoke_recently"] = W_SPOKE_RECENTLY
        elif since < RECENT_LONG_SEC:
            contributions["spoke_lately"] = W_SPOKE_LATELY

    z = sum(contributions.values())
    return 1.0 / (1.0 + math.exp(-z)), contributions


def explain(contributions: dict[str, float], limit: int = 3) -> str:
    """Short text for logs: the strongest non-base terms, e.g. ``question+0.8``."""
    terms = sorted(
        ((name, value) for name, value in contributions.items() if name != "base"),
        key=lambda item: -abs(item[1]),
    )[:limit]
    return " ".join(f"{name}{value:+.1f}" for name, value in terms)
