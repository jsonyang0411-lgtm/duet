"""The OCR fallback: when the database cannot be read, read the open chat from the screen.

A backend that reads WeChat from its database tells this object how every read went
(``note_db_failure`` / ``note_db_ok``).  After ``after`` failures in a row the
fallback turns on; ``poll()`` then looks at the WeChat window (at most every
``interval`` seconds) and returns the other side's messages that are new.  The first
successful database read turns it off again.

What OCR cannot do, and the backend has to live with: it sees only the chat that is
open on the screen and only the part of it that is shown; it cannot tell who spoke in
a group (the bubble text has no name); pictures, voice and stickers are not text.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .reader import NewMessageTracker, clean_group_name, read_rows, resolve_chat_geometry

MAIN_WINDOW_MIN_CHAT_X0 = 0.10       # the chat area starts right of the session list; an independent window has none

logger = logging.getLogger(__name__)

EMPTY_LOOKS_BEFORE_WARNING = 5       # looks without any text: probably no Screen Recording permission


@dataclass(frozen=True)
class OcrMessage:
    title: str          # as read, with the member count: "测试群（3）"
    title_clean: str    # "测试群"
    text: str
    is_group: bool      # the title carries a member count


class OcrFallback:
    def __init__(self, engine, *, after: int = 3, interval: float = 4.0,
                 clock: Callable[[], float] = time.monotonic):
        self._engine = engine
        self._after = max(1, int(after))
        self._interval = float(interval)
        self._clock = clock
        self._tracker = NewMessageTracker()
        self._failures = 0
        self._active = False
        self._last_look: float | None = None
        self._errors = 0
        self._empty = 0
        self._warm: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._active

    def warm_up(self) -> None:
        """Build the recognizer in the background so it is ready when the database fails."""
        if self._warm is None:
            self._warm = threading.Thread(target=self._engine.prepare, name="ocr-warm-up", daemon=True)
            self._warm.start()

    # ── how the database reads go ─────────────────────────────────────────

    def note_db_failure(self) -> bool:
        """Count a failed database read; True when the fallback is (now) on."""
        self._failures += 1
        if not self._active and self._failures >= self._after:
            self._active = True
            self._tracker.reset()
            self._last_look = None
            self._errors = self._empty = 0
            logger.warning(
                "The WeChat database cannot be read (%d times in a row): reading the open chat "
                "from the screen (OCR) instead. Groups only, the speaker is not known.",
                self._failures,
            )
        return self._active

    def note_db_ok(self) -> bool:
        """A database read worked; True when this turned the fallback off."""
        self._failures = 0
        if not self._active:
            return False
        self._active = False
        self._tracker.reset()
        logger.info("The WeChat database can be read again: the OCR fallback is off.")
        return True

    def chat_is_open(self, name: str) -> bool:
        """One fresh look (not throttled): is the chat called ``name`` open in the MAIN window right now?

        Used to send in place instead of searching for the chat.  An independent chat window does not
        count (the sender types into the main window), and a title that does not match exactly does not either.
        """
        rows = self._engine.capture()
        info = read_rows(rows)
        if not info["title"] or resolve_chat_geometry(rows).chat_x0 < MAIN_WINDOW_MIN_CHAT_X0:
            return False

        def norm(value: str) -> str:
            return "".join(str(value).split()).lower()

        return norm(info["title_clean"]) == norm(name)

    # ── one look ──────────────────────────────────────────────────────────

    def poll(self) -> list[OcrMessage]:
        """New messages in the open chat since the last look (empty while the fallback is off)."""
        if not self._active:
            return []
        now = self._clock()
        if self._last_look is not None and now - self._last_look < self._interval:
            return []
        self._last_look = now
        try:
            rows = self._engine.capture()
        except Exception as exc:                          # noqa: BLE001 - the screen is not always there
            self._errors += 1
            if self._errors == 1 or self._errors % 20 == 0:
                logger.warning("OCR fallback: cannot read the WeChat window (%d times): %s", self._errors, exc)
            return []
        self._errors = 0
        if not rows:
            self._empty += 1
            if self._empty == EMPTY_LOOKS_BEFORE_WARNING:
                logger.warning(
                    "OCR fallback: the WeChat window shows no text. Check System Settings > Privacy & "
                    "Security > Screen Recording for the program that runs the bot; WeChat must not be minimized."
                )
            return []
        self._empty = 0
        info = read_rows(rows)
        if not info["title"]:
            return []                                     # no chat is open (settings, moments ...)
        title = info["title"]
        clean = clean_group_name(title)
        is_group = clean != title.strip()
        return [OcrMessage(title=title, title_clean=clean, text=text, is_group=is_group)
                for text in self._tracker.feed(title, info["other"])]
