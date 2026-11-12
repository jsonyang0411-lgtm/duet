"""Message router — receives standardized messages and runs them through the business route.

The ordered steps are in ``src/routing/steps.py``: skip the account's own lines, hand a
listed creator's message to the Mac (PRM_EDGE), the admin's answers to the drafts told to the
admin's chat (PRM_WECHAT_REVIEW), store, ``cc`` tasks to Claude Code on the Mac (CLAUDE_RELAY),
a registered creator's message to the PRM copilot (PRM_COPILOT), the admin's account commands
in a private chat, and silence for everything else.  No step
calls a model: a group message or an unknown private chat never gets an answer.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .decision_trace import DecisionTracer
from .routing.steps import ROUTE_STEPS, run_route
from .send.bubbles import strip_separators

logger = logging.getLogger(__name__)


class MessageRouter:
    """Routes incoming messages; the relay, PRM and edge services are attached by the Bot.

    ``_claude_relay``, ``_prm`` and ``_edge`` stay ``None`` while their switch is off,
    and their step then passes the message on.
    """

    def __init__(self, store, admin_handler, config, tracer: DecisionTracer | None = None):
        self._store = store
        self._admin = admin_handler
        self._config = config
        self._tracer = tracer if tracer is not None else DecisionTracer.from_config(config)
        self._route_steps = ROUTE_STEPS
        self._claude_relay = None
        self._prm = None
        self._edge = None
        self.messages_processed = 0

    def handle(self, msg: dict) -> Optional[str]:
        """Process an incoming WeChat message.

        Returns the reply text (a relay receipt or an admin command's answer), or None.
        The decision trace only observes: the result is returned unchanged.
        """
        trace = self._tracer.begin(msg)
        try:
            reply = run_route(self, msg, trace, self._route_steps)
        except Exception as exc:
            trace.finish_error(exc)
            raise
        if reply:
            # No step answers in bubbles or with a sticker: a separator in echoed text
            # (a task name, a friend's nickname) must not split the message.
            reply = strip_separators(reply).strip() or None
        trace.finish_reply(reply)
        return reply

    def _now(self) -> int:
        """Whole seconds on the router's clock (tests swap this module's ``time``)."""
        return int(time.time())
