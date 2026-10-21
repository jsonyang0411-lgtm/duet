"""What the bot said lately, per chat (in memory, thread-safe).

The bot's own messages are not stored with the group's, so the model could not
see what it had just said and repeated or contradicted itself.  The router
adds every visible reply here and hands the last few to the prompt.
"""

import threading
import time
from collections import OrderedDict, deque
from typing import Callable

DEFAULT_PER_CHAT = 6
DEFAULT_TTL_SEC = 1800
MAX_CHATS = 500
MAX_ENTRY_CHARS = 80


class RecentReplyLog:
    def __init__(self, per_chat: int = DEFAULT_PER_CHAT, ttl_sec: float = DEFAULT_TTL_SEC,
                 clock: Callable[[], float] = time.time):
        self._per_chat = max(1, int(per_chat))
        self._ttl = float(ttl_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self._chats: "OrderedDict[str, deque]" = OrderedDict()

    def add(self, chat_id: str, text: str) -> None:
        # Command output (a tarot reading, a todo list) is long and multi-line;
        # the model only needs to know roughly what it said.
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        text = lines[0][:MAX_ENTRY_CHARS] if lines else ""
        if not chat_id or not text:
            return
        with self._lock:
            entries = self._chats.get(chat_id)
            if entries is None:
                entries = self._chats[chat_id] = deque(maxlen=self._per_chat)
            else:
                self._chats.move_to_end(chat_id)
            entries.append((self._clock(), text))
            while len(self._chats) > MAX_CHATS:
                self._chats.popitem(last=False)

    def recent(self, chat_id: str, limit: int = 3) -> tuple:
        """The last ``limit`` replies still within the TTL, oldest first."""
        cutoff = self._clock() - self._ttl
        with self._lock:
            entries = self._chats.get(chat_id)
            if not entries:
                return ()
            fresh = [text for stamp, text in entries if stamp >= cutoff]
        return tuple(fresh[-max(0, int(limit)):]) if limit else ()
