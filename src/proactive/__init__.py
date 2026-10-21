"""Proactive chat participation — rate-based multi-mode reply system."""

from .modes import ProactiveMode, get_modes, lookup_mode
from .rate_tracker import RateTracker
from .gate import ProactiveGate
from .followup import FollowupListenTracker, FollowupMatch

__all__ = [
    "ProactiveMode",
    "get_modes",
    "lookup_mode",
    "RateTracker",
    "ProactiveGate",
    "FollowupListenTracker",
    "FollowupMatch",
]
