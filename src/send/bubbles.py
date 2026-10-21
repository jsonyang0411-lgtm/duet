"""Several chat bubbles from one reply, and the pause between them.

The router turns the model's messages into one string; with ``SEND_BUBBLES=on``
it puts ``BUBBLE_SEP`` between them.  Every WeChat backend hands that string to
:class:`BubbleSender`, which sends the parts one after another.  A reply without
the separator is sent exactly as before, in one call and without any pause
(unless ``SEND_TYPING_DELAY=on``).

Why a separate class: the backends share the one WeChat window, so the window
lock must be held per bubble, not while waiting between them, or every other
group would stall behind a slow typist.  A per-chat lock keeps the bubbles of
one reply together instead.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Callable

from .stickers import STICKER_MARK, describe, get_stickers, sticker_in

logger = logging.getLogger(__name__)

# An invisible separator (U+2063) that a model has no reason to write.  Only
# the router puts it in a reply (it removes it from every other reply), so a
# nickname or a quoted message cannot split one.
BUBBLE_SEP = "\u2063"
MAX_BUBBLES = 3
MAX_CHAT_LOCKS = 500


def join_bubbles(parts) -> str:
    """The parts as one string with separators; the separator itself cannot appear in a part."""
    cleaned = [str(p).replace(BUBBLE_SEP, "").strip() for p in parts]
    return BUBBLE_SEP.join(p for p in cleaned if p)


def strip_separators(text: str) -> str:
    """The text without any bubble separator or sticker mark."""
    return str(text or "").replace(BUBBLE_SEP, "").replace(STICKER_MARK, "")


def split_bubbles(reply: str, *, limit: int = MAX_BUBBLES) -> list[str]:
    """The non-empty bubbles of a reply (at most ``limit``; the rest is merged into the last)."""
    parts = [p.strip() for p in str(reply or "").split(BUBBLE_SEP)]
    parts = [p for p in parts if p]
    if len(parts) > limit:
        # A sticker is never merged into text; one that does not fit is dropped.
        tail = " ".join(p for p in parts[limit - 1:] if sticker_in(p) is None)
        parts = parts[:limit - 1] + ([tail] if tail else [])
    return parts


def plain_text(reply: str) -> str:
    """The reply as one line for logs, memory and the follow-up listener (a sticker as ``[表情包：name]``)."""
    return " ".join(describe(part) for part in split_bubbles(reply, limit=10 ** 6))


def typing_delay(text: str, *, base: float = 0.4, per_char: float = 0.09, cap: float = 3.5) -> float:
    """Seconds a person would need to type ``text`` (a short, capped estimate)."""
    return min(cap, base + per_char * len(str(text or "")))


def send_bubble(part: str, send: Callable[[str], bool],
                send_file: Callable[[str], bool] | None = None) -> bool | None:
    """Send one bubble: text through ``send``, a sticker's file through ``send_file``.

    None when a sticker bubble was skipped (not in the library, or a backend
    that cannot send pictures); that is not a failure.
    """
    name = sticker_in(part)
    if name is None:
        return bool(send(part))
    sticker = get_stickers().get(name)
    if sticker is None or send_file is None:
        logger.warning("Sticker %r skipped: %s", name,
                       "not in the library" if sticker is None else "this backend cannot send pictures")
        return None
    return bool(send_file(str(sticker.path)))


class BubbleSender:
    """Send a reply bubble by bubble through ``send(text) -> bool``."""

    def __init__(self, *, typing: bool = False, sleep: Callable[[float], None] = time.sleep) -> None:
        self.typing = bool(typing)
        self._sleep = sleep
        self._guard = threading.Lock()
        self._locks: "OrderedDict[str, threading.Lock]" = OrderedDict()

    def _chat_lock(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
                while len(self._locks) > MAX_CHAT_LOCKS:
                    self._locks.popitem(last=False)
            else:
                self._locks.move_to_end(key)
            return lock

    def deliver(
        self,
        chat_key: str,
        reply: str,
        send: Callable[[str], bool],
        *,
        alive: Callable[[], bool] | None = None,
        send_file: Callable[[str], bool] | None = None,
    ) -> bool:
        """True when every bubble was sent.  A failed bubble ends the reply.

        ``alive`` tells whether the backend is still running; once it says no, the
        remaining bubbles and pauses are skipped (the single-call path never waits).
        ``send_file(path)`` sends a sticker picture; without it (or when the
        sticker is not in the library) a sticker bubble is skipped, not failed.
        """
        if reply and BUBBLE_SEP not in reply and STICKER_MARK not in reply and not self.typing:
            return bool(send(reply))                          # exactly the old single call, text untouched
        parts = split_bubbles(reply)
        if not parts:
            return False
        if len(parts) == 1 and not self.typing and sticker_in(parts[0]) is None:
            return bool(send(parts[0]))
        sent_any = False
        with self._chat_lock(chat_key):
            for index, part in enumerate(parts):
                if alive is not None and not alive():
                    logger.info("Backend stopped: %d bubble(s) not sent", len(parts) - index)
                    return False
                if index > 0 or self.typing:
                    self._sleep(typing_delay(part))
                    if alive is not None and not alive():
                        logger.info("Backend stopped: %d bubble(s) not sent", len(parts) - index)
                        return False
                ok = send_bubble(part, send, send_file)
                if ok is None:
                    continue
                if not ok:
                    if index:
                        logger.error("Reply cut short: bubble %d of %d failed", index + 1, len(parts))
                    return False
                sent_any = True
        return sent_any


_sender = BubbleSender()


def get_sender() -> BubbleSender:
    """The sender every backend uses (typing pauses off until ``configure_sender``)."""
    return _sender


def configure_sender(*, typing: bool = False, sleep: Callable[[float], None] = time.sleep) -> BubbleSender:
    """Replace the process-wide sender; the bot calls this once at start-up."""
    global _sender
    _sender = BubbleSender(typing=typing, sleep=sleep)
    return _sender
