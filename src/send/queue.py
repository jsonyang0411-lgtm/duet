"""One thread that sends every reply (``SEND_QUEUE=on``, WCDB backend).

Without the queue a reply is sent from the worker thread that wrote it.  Typing
pauses (``SEND_TYPING_DELAY``) and the wait for the user to pause before a send
(``SEND_QUIET``, up to ``SEND_IDLE_MAX_WAIT_SEC``) then keep that worker busy,
and with every worker busy no other message gets handled.  With the queue the
worker hands the reply over and is free at once.  (A screenshot of a picture
for the relay still runs on a worker and can still wait for the user.)

The queue sends bubble by bubble: within a chat strictly in order (a reply
starts after the chat's previous reply is done), across chats whichever bubble
is due first, so one chat's typing pause lets another chat's bubble through.
A failed bubble ends its reply; a stopped backend (``alive`` false) drops what
is left.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .bubbles import BUBBLE_SEP, send_bubble, split_bubbles, typing_delay
from .stickers import STICKER_MARK

logger = logging.getLogger(__name__)


@dataclass
class _Job:
    seq: int
    chat_key: str
    parts: list
    send: Callable[[str], bool]
    send_file: Callable[[str], bool] | None
    on_done: Callable[[bool], None] | None
    due: float
    index: int = 0
    sent_any: bool = False


class SendQueue:
    """Bubbles waiting to be sent, and the thread that sends them."""

    def __init__(self, *, typing: bool = False, clock: Callable[[], float] = time.monotonic,
                 alive: Callable[[], bool] | None = None) -> None:
        self.typing = bool(typing)
        self._clock = clock
        self._alive = alive
        self._cond = threading.Condition()
        self._jobs: list[_Job] = []
        self._seq = 0
        self._stopping = False
        self._thread: threading.Thread | None = None

    # ── from the worker threads ─────────────────────────────────────

    def submit(self, chat_key: str, reply: str, send: Callable[[str], bool], *,
               send_file: Callable[[str], bool] | None = None,
               on_done: Callable[[bool], None] | None = None) -> bool:
        """Queue a reply; ``on_done(sent)`` runs on the queue thread once it is finished.

        False when nothing was queued: an empty reply, or the queue was stopped.
        """
        if reply and BUBBLE_SEP not in reply and STICKER_MARK not in reply:
            parts = [reply]                                   # sent exactly as written, like BubbleSender
        else:
            parts = split_bubbles(reply)
        if not parts:
            return False
        with self._cond:
            if self._stopping:
                logger.info("Send queue: stopped, a reply for %s is not sent", chat_key)
                return False
            self._seq += 1
            due = self._clock() + (typing_delay(parts[0]) if self.typing else 0.0)
            self._jobs.append(_Job(self._seq, chat_key, parts, send, send_file, on_done, due))
            self._cond.notify()
        return True

    def pending(self) -> int:
        with self._cond:
            return len(self._jobs)

    # ── the queue thread ────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive():
                return                     # running, or an old one still finishing a send
            self._thread = None
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="send-queue", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()
        if self._thread is not None:
            self._thread.join(timeout)
            if not self._thread.is_alive():
                self._thread = None        # else start() waits for it: never two senders
        self._drop("queue stopped")

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
                wait = self._wait_seconds()
                if wait is None or wait > 0:
                    self._cond.wait(timeout=wait)
                    continue
            try:
                self.run_once()
            except Exception:                                  # noqa: BLE001 - the thread must survive
                logger.exception("Send queue: unexpected error")

    def _head(self) -> _Job | None:
        """Among each chat's oldest reply, the one whose next bubble is due first."""
        heads: dict[str, _Job] = {}
        for job in self._jobs:
            heads.setdefault(job.chat_key, job)
        if not heads:
            return None
        return min(heads.values(), key=lambda job: (job.due, job.seq))

    def _wait_seconds(self) -> float | None:
        job = self._head()
        return None if job is None else max(0.0, job.due - self._clock())

    def run_once(self) -> bool:
        """Send the bubble that is due first; False when none is due yet."""
        with self._cond:
            job = self._head()
            if job is None or job.due > self._clock():
                return False
            part = job.parts[job.index]
        if self._alive is not None and not self._alive():
            self._drop("backend stopped")
            return True
        try:
            ok = send_bubble(part, job.send, job.send_file)
        except Exception:                                      # noqa: BLE001 - a send that blew up failed
            logger.exception("Send queue: sending to %s failed", job.chat_key)
            ok = False
        finished = None
        with self._cond:
            if job not in self._jobs:                          # dropped meanwhile (stop)
                return True
            if ok is False:
                if job.index:
                    logger.error("Reply cut short: bubble %d of %d failed", job.index + 1, len(job.parts))
                self._jobs.remove(job)
                finished = False
                self._next_reply_types(job.chat_key)
            else:
                job.sent_any = job.sent_any or bool(ok)
                job.index += 1
                if job.index >= len(job.parts):
                    self._jobs.remove(job)
                    finished = job.sent_any
                    self._next_reply_types(job.chat_key)
                else:
                    job.due = self._clock() + typing_delay(job.parts[job.index])
        if finished is not None and job.on_done is not None:
            try:
                job.on_done(finished)
            except Exception:                                  # noqa: BLE001
                logger.exception("Send queue: on_done failed")
        return True

    def _next_reply_types(self, chat_key: str) -> None:
        """With typing pauses, the chat's next reply is typed after this one, not alongside it."""
        if not self.typing:
            return
        for job in self._jobs:
            if job.chat_key == chat_key:
                job.due = max(job.due, self._clock() + typing_delay(job.parts[0]))
                return

    def _drop(self, why: str) -> None:
        with self._cond:
            dropped, self._jobs = self._jobs, []
        if dropped:
            logger.info("Send queue: %s, %d reply(s) not sent", why, len(dropped))
