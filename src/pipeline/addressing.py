"""Addressing: is this message meant for the bot?

The router already knows a lot — an @, a quote of the bot, a keyword, a
sender who was just answered — but only as scattered ``msg["_..."]`` flags.
This module turns them into one value, ``Addressing``, that carries a *level*
and the *signals* behind it.  The level decides how much freedom the model
gets (must it reply? may it stay silent?), the signals are written into the
prompt as facts the program has already verified.

Pure functions only: no I/O, no clock, no model.
"""

import re
from dataclasses import dataclass
from typing import Iterable

LEVEL_EXPLICIT = "explicit"   # @bot, quote of the bot, private chat, forced group
LEVEL_STRONG = "strong"       # name call, keyword, sender the bot just answered
LEVEL_WEAK = "weak"           # short question / answer right after a bot reply
LEVEL_AMBIENT = "ambient"     # nothing points at the bot
LEVELS = (LEVEL_EXPLICIT, LEVEL_STRONG, LEVEL_WEAK, LEVEL_AMBIENT)

SIGNAL_AT = "at"
SIGNAL_QUOTE_BOT = "quote_bot"
SIGNAL_PRIVATE = "private"
SIGNAL_FORCED = "forced"
SIGNAL_KEYWORD = "keyword"
SIGNAL_NAME_CALL = "name_call"
SIGNAL_STICKY = "sticky"
SIGNAL_FOLLOWUP = "followup"
SIGNAL_FULL_FEED = "full_feed"
SIGNAL_AT_OTHER = "at_other"          # negative: @ someone else
SIGNAL_QUOTE_OTHER = "quote_other"    # negative: quote of another member

_EXPLICIT_SIGNALS = (SIGNAL_AT, SIGNAL_QUOTE_BOT, SIGNAL_PRIVATE, SIGNAL_FORCED)
_STRONG_SIGNALS = (SIGNAL_KEYWORD, SIGNAL_NAME_CALL, SIGNAL_STICKY)
_WECHAT_SENDER_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9_]+:\s*")
# "@张三 ..." — the @ must start a word so e-mail addresses do not count.
_MENTION_RE = re.compile(r"(?:^|[\s，。！？,.!?:：（(「])@([^\s@  ​]{1,24})")
MIN_NAME_CHARS = 2


@dataclass(frozen=True)
class Addressing:
    level: str
    signals: tuple[str, ...] = ()
    followup_reason: str = ""
    last_bot_reply: str = ""

    @property
    def explicit(self) -> bool:
        return self.level == LEVEL_EXPLICIT

    def has(self, signal: str) -> bool:
        return signal in self.signals

    def as_trace(self) -> dict:
        info = {"level": self.level, "signals": list(self.signals)}
        if self.followup_reason:
            info["followup_reason"] = self.followup_reason
        return info


def bot_names(*candidates: object) -> tuple[str, ...]:
    """The names that count as calling the bot: display name, persona, aliases.

    Accepts strings and iterables of strings; blanks, one-character names and
    duplicates (case-insensitively) are dropped.
    """
    seen: dict[str, str] = {}
    for item in candidates:
        if isinstance(item, str):
            values = [item]
        elif isinstance(item, (list, tuple, set, frozenset)):
            values = list(item)
        else:
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            name = value.strip()
            if len(name) >= MIN_NAME_CHARS:
                seen.setdefault(name.casefold(), name)
    return tuple(seen.values())


def clean_content(content: object) -> str:
    """Message text without the WeChat ``wxid_...:`` quote prefix."""
    return _WECHAT_SENDER_PREFIX.sub("", str(content or ""), count=1).strip()


def mentions_in(content: object) -> list[str]:
    """Names @-mentioned in ``content`` (without the @)."""
    return _MENTION_RE.findall(clean_content(content))


_LEADING_MENTION_RE = re.compile(r"\s*@([^\s@\u2005\u2006\u200b]{1,24})[\s\u2005\u2006\u200b]*")


def leading_mentions(content: object) -> list[str]:
    """The run of @-mentions at the very start of the message, in order.

    "@阿明 @小助手 来看" -> [阿明, 小助手]; "@阿明 你别老@小助手" -> [阿明];
    "大家好 @小助手" -> [] (the text does not open with a mention).
    """
    text = clean_content(content)
    found: list[str] = []
    position = 0
    while True:
        match = _LEADING_MENTION_RE.match(text, position)
        if not match:
            return found
        found.append(match.group(1))
        position = match.end()


def _is_own_name(mentioned: str, names: Iterable[str]) -> bool:
    folded = mentioned.casefold()
    for name in names:
        own = name.casefold()
        if folded.startswith(own) or own.startswith(folded):
            return True
    return False


def mentions_other(content: object, names: Iterable[str]) -> bool:
    """True when the message @s somebody who is not the bot."""
    own = bot_names(names)
    return any(not _is_own_name(m, own) for m in mentions_in(content))


def calls_by_name(content: object, names: Iterable[str]) -> bool:
    """True when a bot name appears in the text (an @-mention is not needed)."""
    text = clean_content(content).casefold()
    return bool(text) and any(name.casefold() in text for name in bot_names(names))


def classify(msg: dict, *, names: Iterable[str] = (), private: bool = False,
             forced: bool = False, full_feed: bool = False) -> Addressing:
    """Work out how directly ``msg`` points at the bot.

    ``names`` are the bot's own names (see :func:`bot_names`).  ``private``,
    ``forced`` and ``full_feed`` describe the route the router chose.  The
    message dict supplies the flags the backend and the router already set.
    """
    signals: list[str] = []

    def add(signal: str) -> None:
        if signal not in signals:
            signals.append(signal)

    if private:
        add(SIGNAL_PRIVATE)
    if forced:
        add(SIGNAL_FORCED)
    if full_feed:
        add(SIGNAL_FULL_FEED)

    content = msg.get("content", "")
    quoted_bot = bool(msg.get("is_reply_to_bot"))
    if quoted_bot:
        add(SIGNAL_QUOTE_BOT)
    if msg.get("is_at_mentioned") and not quoted_bot:
        add(SIGNAL_AT)
    elif msg.get("is_at_mentioned") and quoted_bot:
        # The backend sets is_at_mentioned for a quote of the bot too; only a
        # visible @bot in the text is a separate, additional signal.
        if any(_is_own_name(m, bot_names(names)) for m in mentions_in(content)):
            add(SIGNAL_AT)

    # A message that opens by addressing somebody else ("@阿明 听到没，她说…")
    # is aimed at that person even if it also quotes or mentions the bot: the
    # bot is being talked about, or its answer relayed.
    opens = leading_mentions(content)
    own = bot_names(names)
    opens_with_other = (
        bool(opens)
        and not any(_is_own_name(m, own) for m in opens)
        and SIGNAL_PRIVATE not in signals and SIGNAL_FORCED not in signals
    )
    if opens_with_other:
        add(SIGNAL_AT_OTHER)

    explicit = any(s in signals for s in _EXPLICIT_SIGNALS) and not opens_with_other

    if not explicit and not opens_with_other and mentions_other(content, names):
        add(SIGNAL_AT_OTHER)
    if (
        msg.get("quoted_content") and not quoted_bot and not explicit
    ):
        add(SIGNAL_QUOTE_OTHER)

    if not explicit:
        # Saying the bot's name (or a configured keyword) is a call even when
        # the message also @s or quotes someone else: the model sorts out
        # "talking to the bot" from "talking about the bot".
        if msg.get("_kw_triggered"):
            add(SIGNAL_KEYWORD)
        if calls_by_name(content, names):
            add(SIGNAL_NAME_CALL)
        # The inferred signals (just answered this sender, short follow-up) are
        # not enough when the message is visibly aimed at somebody else.
        aimed_elsewhere = SIGNAL_AT_OTHER in signals or SIGNAL_QUOTE_OTHER in signals
        if not aimed_elsewhere:
            if msg.get("_sticky_triggered"):
                add(SIGNAL_STICKY)
            if msg.get("_followup_listen_triggered"):
                add(SIGNAL_FOLLOWUP)

    if explicit:
        level = LEVEL_EXPLICIT
    elif any(s in signals for s in _STRONG_SIGNALS) or (
        opens_with_other and (SIGNAL_AT in signals or SIGNAL_QUOTE_BOT in signals)
    ):
        level = LEVEL_STRONG
    elif SIGNAL_FOLLOWUP in signals:
        level = LEVEL_WEAK
    else:
        level = LEVEL_AMBIENT

    return Addressing(
        level=level,
        signals=tuple(signals),
        followup_reason=str(msg.get("_followup_listen_reason", "") or "")
        if SIGNAL_FOLLOWUP in signals else "",
        last_bot_reply=str(msg.get("_followup_bot_reply", "") or "")
        if SIGNAL_FOLLOWUP in signals else "",
    )
