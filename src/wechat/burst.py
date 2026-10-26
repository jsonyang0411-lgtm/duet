"""Small burst coalescing for WeChat message bubbles.

WeChat users often split one thought across several short bubbles.  The bot
handles the thought (one relay task, one creator turn), not each bubble.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import time


@dataclass
class _PendingBurst:
    messages: list[dict] = field(default_factory=list)
    updated_at: float = 0.0


class MessageBurstBuffer:
    """Coalesce consecutive messages from the same sender in one chat."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        window_sec: float = 2.2,
        max_messages: int = 5,
        max_chars: int = 200,
    ) -> None:
        self.enabled = bool(enabled)
        self.window_sec = max(0.0, float(window_sec))
        self.max_messages = max(1, int(max_messages))
        self.max_chars = max(1, int(max_chars))
        self._pending: dict[tuple[str, str, bool], _PendingBurst] = {}

    def push(self, message: dict, *, now: float | None = None) -> list[dict]:
        """Add one standardized message and return messages ready to handle.

        Only text is held; a picture, file or voice message is handed on at once.
        """
        if now is None:
            now = time.monotonic()
        ready = self.flush_due(now=now)

        if not self.enabled or not self._can_buffer(message):
            ready.append(message)
            return ready

        content_len = self._message_len(message)
        if content_len > self.max_chars:
            ready.append(message)
            return ready

        key = self._key(message)
        pending = self._pending.get(key)
        if pending and not self._fits(pending.messages, message):
            ready.append(self._merge(pending.messages))
            pending = None
            self._pending.pop(key, None)

        if pending is None:
            pending = _PendingBurst(messages=[], updated_at=now)
            self._pending[key] = pending

        pending.messages.append(message)
        pending.updated_at = now

        if (
            len(pending.messages) >= self.max_messages
            or self._total_len(pending.messages) >= self.max_chars
        ):
            ready.append(self._merge(pending.messages))
            self._pending.pop(key, None)

        return ready

    def flush_due(self, *, now: float | None = None) -> list[dict]:
        """Return bursts that have been quiet for the configured window."""
        if now is None:
            now = time.monotonic()
        ready: list[dict] = []
        due_keys = [
            key for key, pending in self._pending.items()
            if now - pending.updated_at >= self.window_sec
        ]
        for key in due_keys:
            pending = self._pending.pop(key)
            ready.append(self._merge(pending.messages))
        return ready

    def flush_all(self) -> list[dict]:
        """Return all pending bursts immediately."""
        ready = [self._merge(pending.messages) for pending in self._pending.values()]
        self._pending.clear()
        return ready

    def _key(self, message: dict) -> tuple[str, str, bool]:
        return (
            str(message.get("chat_id", "")),
            str(message.get("sender_id", "")),
            bool(message.get("is_group", True)),
        )

    def _can_buffer(self, message: dict) -> bool:
        if message.get("is_system_join"):
            return False
        try:
            if int(message.get("msg_type", 1) or 1) != 1:
                return False
        except (TypeError, ValueError):
            return False
        if not str(message.get("chat_id", "")).strip():
            return False
        if not str(message.get("sender_id", "")).strip():
            return False
        return bool(str(message.get("content", "")).strip())

    def _fits(self, messages: list[dict], message: dict) -> bool:
        if len(messages) + 1 > self.max_messages:
            return False
        return self._total_len(messages) + self._message_len(message) <= self.max_chars

    def _message_len(self, message: dict) -> int:
        return len(str(message.get("content", "")).strip())

    def _total_len(self, messages: list[dict]) -> int:
        return sum(self._message_len(message) for message in messages)

    def _merge(self, messages: list[dict]) -> dict:
        if not messages:
            return {}
        if len(messages) == 1:
            return dict(messages[0])

        merged = dict(messages[-1])
        ids = [str(message.get("message_id", "")) for message in messages]
        contents = [
            str(message.get("content", "")).strip()
            for message in messages
            if str(message.get("content", "")).strip()
        ]

        merged["message_id"] = self._burst_id(merged, ids)
        merged["content"] = "\n".join(contents)
        merged["timestamp"] = max(int(message.get("timestamp", 0) or 0) for message in messages)
        merged["is_at_mentioned"] = any(bool(message.get("is_at_mentioned")) for message in messages)
        merged["is_reply_to_bot"] = any(bool(message.get("is_reply_to_bot")) for message in messages)
        merged["_burst_count"] = len(messages)
        merged["_burst_message_ids"] = ids

        quote_source = next(
            (
                message for message in reversed(messages)
                if str(message.get("quoted_content", "")).strip()
            ),
            None,
        )
        if quote_source is not None:
            for key in (
                "quoted_sender_id",
                "quoted_sender_name",
                "quoted_content",
                "quoted_server_id",
            ):
                merged[key] = quote_source.get(key, "")

        return merged

    def _message_type(self, message: dict) -> int:
        try:
            return int(message.get("msg_type", 1) or 1)
        except (TypeError, ValueError):
            return 1

    def _burst_id(self, message: dict, ids: list[str]) -> str:
        raw = "|".join(
            [
                "burst",
                str(message.get("chat_id", "")),
                str(message.get("sender_id", "")),
                *ids,
            ]
        )
        return hashlib.md5(raw.encode("utf-8")).hexdigest()
