"""Short-window listener for messages that look like replies to the bot."""

import logging
import threading
import re
import time
from dataclasses import dataclass

from ._sync import locked

logger = logging.getLogger(__name__)

_WECHAT_SENDER_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9_]+:\s*")
_MEDIA_ONLY_RE = re.compile(r"^\[(?:表情|图片|语音|视频|文件|位置|动画表情)\]$")
_STOP_TOKENS = {
    "the", "and", "for", "with", "you", "your", "are", "was", "were",
    "that", "this", "of", "to", "in", "on", "a", "an", "is",
}
_QUESTION_MARKERS = (
    "?", "？", "吗", "嘛", "么", "什么", "啥", "哪个", "哪首",
    "哪一个", "是不是", "咋", "怎么", "啥意思", "什么意思",
)
_ASKING_MARKERS = (
    "吗", "？", "?", "哪", "什么", "喜欢", "要不要", "可以吗",
    "好不好", "想听", "想要", "要哪个", "要哪", "推荐",
)
_SHORT_ANSWER_MARKERS = {
    "都可以", "随便", "随便推荐", "都行", "都好", "可以", "行",
    "要", "不要", "不用", "没有", "没", "英文", "中文",
}
_ACK_ONLY = {"嗯", "哦", "噢", "好", "好的", "没事", "谢谢", "谢了", "ok"}


@dataclass
class FollowupMatch:
    bot_reply: str
    reason: str


@dataclass
class _FollowupEntry:
    bot_reply: str
    expiry: float
    sender_id: str = ""             # set when the model said it waits for this person only


class FollowupListenTracker:
    """Track the bot's latest group reply and match likely follow-ups.

    This is intentionally local and heuristic.  It decides whether a message is
    cheap enough and plausible enough to ask the model about; the model still
    gets the final "reply or stay silent" decision.
    """

    def __init__(self, ttl_sec: int = 60, max_message_chars: int = 30):
        self._ttl = max(5, int(ttl_sec or 60))
        self._max_chars = max(1, int(max_message_chars or 30))
        self._entries: dict[str, _FollowupEntry] = {}
        self._call_count = 0
        # Shared by the message-pool threads: every public method holds this.
        self._lock = threading.RLock()

    @locked
    def register(self, chat_id: str, bot_reply: str, *, expect: str = "",
                 sender_id: str = "", seconds: int | None = None) -> None:
        """Remember the bot's latest reply for a group.

        ``expect`` is what the model declared (FOLLOWUP_MODE=declared):
        ``none`` stops listening after this reply, ``sender`` listens only for
        ``sender_id`` (any short text counts as an answer), ``anyone`` keeps
        the heuristics; ``seconds`` replaces the default window.
        """
        self._maybe_cleanup()
        reply = self._clean_text(bot_reply)
        if not chat_id or not reply:
            return
        if expect == "none":
            self._entries.pop(chat_id, None)
            return
        ttl = max(5, int(seconds)) if seconds else self._ttl
        self._entries[chat_id] = _FollowupEntry(
            bot_reply=reply,
            expiry=time.time() + ttl,
            sender_id=sender_id if expect == "sender" else "",
        )
        logger.debug(
            "Followup listener registered: chat=%s ttl=%ds reply=%.40s",
            chat_id[:20], self._ttl, reply,
        )

    @locked
    def match(self, msg: dict) -> FollowupMatch | None:
        """Return match info if the incoming message likely continues the bot."""
        self._maybe_cleanup()

        chat_id = str(msg.get("chat_id", "") or "")
        entry = self._entries.get(chat_id)
        if entry is None:
            return None
        remaining = entry.expiry - time.time()
        if remaining <= 0:
            self._entries.pop(chat_id, None)
            return None

        content = self._clean_text(msg.get("content", ""))
        if entry.sender_id:
            # The model said whom it waits for: only that person, any answer.
            if str(msg.get("sender_id", "") or "") != entry.sender_id:
                return None
            if not content or _MEDIA_ONLY_RE.fullmatch(content) or len(content) > self._max_chars * 3:
                return None
            logger.info(
                "Followup listener matched (declared): chat=%s sender=%s remaining=%.1fs",
                chat_id[:20], entry.sender_id[:20], remaining,
            )
            # One answer is what the model waited for.  Its next reply may ask
            # again (and re-arm this), but a chatty person cannot keep the bot
            # listening to every message for minutes.
            self._entries.pop(chat_id, None)
            return FollowupMatch(bot_reply=entry.bot_reply, reason="declared-sender")
        if not self._is_candidate_shape(content):
            return None

        reason = self._match_reason(content, entry.bot_reply)
        if not reason:
            return None

        logger.info(
            "Followup listener matched: chat=%s sender=%s reason=%s remaining=%.1fs",
            chat_id[:20], str(msg.get("sender_id", ""))[:20], reason, remaining,
        )
        return FollowupMatch(bot_reply=entry.bot_reply, reason=reason)

    @locked
    def clear(self, chat_id: str) -> None:
        self._entries.pop(chat_id, None)

    def _is_candidate_shape(self, content: str) -> bool:
        if not content:
            return False
        if len(content) > self._max_chars:
            return False
        if _MEDIA_ONLY_RE.fullmatch(content):
            return False
        if content.lower() in _ACK_ONLY:
            return False
        return True

    def _match_reason(self, content: str, bot_reply: str) -> str:
        if "塔罗" in bot_reply and re.search(
            r"(我也要|也给我|也帮我|测|算|看|财运|感情|事业|考试|钱|桃花|运势)",
            content,
        ):
            return "tarot-followup"
        if self._shares_bot_reply_token(content, bot_reply):
            return "shares-bot-reply-token"
        if self._looks_like_question(content):
            return "short-question"
        if self._bot_reply_asked_question(bot_reply) and self._looks_like_answer(content):
            return "answers-bot-question"
        return ""

    @staticmethod
    def _clean_text(value: object) -> str:
        text = _WECHAT_SENDER_PREFIX.sub("", str(value or ""), count=1).strip()
        text = text.replace("\r", " ").replace("\n", " ")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _compact(value: str) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value).lower()

    @classmethod
    def _shares_bot_reply_token(cls, content: str, bot_reply: str) -> bool:
        compact_content = cls._compact(content)
        if not compact_content:
            return False

        tokens: set[str] = set()
        tokens.update(re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", bot_reply.lower()))
        for title in re.findall(r"[《「“\"']([^》」”\"']{2,40})[》」”\"']", bot_reply):
            compact_title = cls._compact(title)
            if len(compact_title) >= 2:
                tokens.add(compact_title)
            tokens.update(re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", title.lower()))

        for token in tokens:
            token = cls._compact(token)
            if token in _STOP_TOKENS:
                continue
            if len(token) >= 3 and token in compact_content:
                return True
            if re.search(r"[\u4e00-\u9fff]", token) and len(token) >= 2:
                if token in compact_content:
                    return True
        return False

    @staticmethod
    def _looks_like_question(content: str) -> bool:
        compact = content.strip().lower()
        if compact in {"?", "？", "吗", "嘛", "么"}:
            return True
        return any(marker in compact for marker in _QUESTION_MARKERS)

    @staticmethod
    def _bot_reply_asked_question(bot_reply: str) -> bool:
        return any(marker in bot_reply for marker in _ASKING_MARKERS)

    @staticmethod
    def _looks_like_answer(content: str) -> bool:
        compact = content.strip().lower()
        if compact in _SHORT_ANSWER_MARKERS:
            return True
        return 1 <= len(compact) <= 8 and compact not in _ACK_ONLY

    def _maybe_cleanup(self) -> None:
        self._call_count += 1
        if self._call_count % 200:
            return
        now = time.time()
        stale = [chat_id for chat_id, entry in self._entries.items() if entry.expiry < now]
        for chat_id in stale:
            self._entries.pop(chat_id, None)
