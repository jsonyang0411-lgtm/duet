"""One-shot image/sticker follow-up tracker for recent bot replies."""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass

from src.vision import IMAGE_MSG_TYPES


@dataclass(frozen=True)
class ImageFollowupMatch:
    bot_reply: str


@dataclass
class _ImageFollowupEntry:
    bot_reply: str
    expiry: float
    source_timestamp: int


class ImageFollowupTracker:
    """Allow one image from the same person shortly after a bot reply.

    Entries are per chat and sender.  They are consumed before the vision API
    call so a burst of stickers cannot cause repeated image requests.
    """

    def __init__(self, ttl_sec: int = 90):
        self._ttl = max(15, int(ttl_sec or 90))
        self._entries: dict[tuple[str, str], _ImageFollowupEntry] = {}
        self._lock = threading.Lock()

    def register(
        self,
        chat_id: str,
        sender_id: str,
        bot_reply: str,
        source_timestamp: int = 0,
    ) -> None:
        reply = str(bot_reply or "").strip()
        if not chat_id or not sender_id or not reply:
            return
        with self._lock:
            self._entries[(str(chat_id), str(sender_id))] = _ImageFollowupEntry(
                bot_reply=reply,
                expiry=time.time() + self._ttl,
                source_timestamp=max(0, int(source_timestamp or 0)),
            )

    def consume(self, msg: dict) -> ImageFollowupMatch | None:
        """Return and remove a matching image/sticker follow-up entry."""
        try:
            if int(msg.get("msg_type", 1) or 1) not in IMAGE_MSG_TYPES:
                return None
        except (TypeError, ValueError):
            return None

        key = (str(msg.get("chat_id", "")), str(msg.get("sender_id", "")))
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is None or entry.expiry < time.time():
            return None
        return ImageFollowupMatch(bot_reply=entry.bot_reply)

    def clear(
        self,
        chat_id: str,
        sender_id: str,
        message_timestamp: int = 0,
    ) -> None:
        """Clear only messages that are not older than the tracked turn."""
        key = (str(chat_id), str(sender_id))
        timestamp = max(0, int(message_timestamp or 0))
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and timestamp
                and entry.source_timestamp
                and timestamp < entry.source_timestamp
            ):
                return
            self._entries.pop(key, None)
