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


@dataclass(frozen=True)
class Stop:
    """End the route; ``value`` is the router's return value."""

    value: Optional[str] = None


@dataclass
class RouteContext:
    router: Any                       # the MessageRouter (its parts are the steps' tools)
    msg: dict
    trace: Any


RouteFn = Callable[[RouteContext], Optional[Stop]]


@dataclass(frozen=True)
class RouteStep:
    name: str                          # shown in the trace when this step ends the route
    run: RouteFn
    doc: str = ""
