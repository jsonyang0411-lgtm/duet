"""What a routing step sees, and how it ends the route.

A step is a function ``step(ctx) -> Stop | None``.  ``None`` means "not mine,
carry on with the next step"; ``Stop(value)`` ends the route and ``value`` is
what ``MessageRouter.handle`` returns (the reply text, or ``None`` for silence).
Every step names itself in the decision trace when it ends the route, so
"why did it (not) answer" has a one-word answer next to the reason code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

# Messages older than this are history replayed at startup, not conversation.
AT_MENTION_MAX_AGE_SEC = 300        # ignore @mentions older than 5 minutes
ALL_MESSAGES_MAX_AGE_SEC = 300      # full-feed persona must not replay history
WELCOME_MAX_AGE_SEC = 300           # ignore join events older than 5 minutes


@dataclass(frozen=True)
class Stop:
    """End the route; ``value`` is the router's return value."""

    value: Optional[str] = None


@dataclass
class RouteContext:
    router: Any                       # the MessageRouter (its handlers are the steps' tools)
    msg: dict
    trace: Any
    raw: str = ""                     # content without a leading ``wxid_xxx:`` quote prefix
    is_at: bool = False               # @, sticky, keyword, follow-up or name call
    sticky_triggered: bool = False
    clean_content: str = ""           # the request text once the @bot is cut out
    reply: Optional[str] = None       # produced by a command or by the chat model


RouteFn = Callable[[RouteContext], Optional[Stop]]


@dataclass(frozen=True)
class RouteStep:
    name: str                          # shown in the trace when this step ends the route
    run: RouteFn
    doc: str = ""
