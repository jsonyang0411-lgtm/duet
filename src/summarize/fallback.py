"""CLOUD_FALLBACK=local: when the cloud model fails, the local model answers.

A cloud model that is out of quota, rate limited or unreachable would turn
every reply into an error.  ``install_fallback`` wraps the cloud model's two
chat calls (``_call_chat_api`` for the original chat path and
``_call_json_api`` for the structured pipeline): a call that raises is asked
of the local model instead, with the same prompt.  After a failure the cloud
is skipped for ``FALLBACK_COOLDOWN_SEC``, so an outage costs one slow
failure, not one per message; then it is tried again.

Only the chat calls are covered: summaries, vision and the rest keep failing
as before, since the local model may not be able to do them.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

FALLBACK_COOLDOWN_SEC = 180
CHAT_CALLS = ("_call_chat_api", "_call_json_api")


class CloudFallback:
    """The state of one cloud model's fallback (installed by ``install_fallback``)."""

    def __init__(self, cloud, local, *, cooldown: float = FALLBACK_COOLDOWN_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cloud = cloud
        self.local = local
        self.cooldown = cooldown
        self.clock = clock
        self.skip_cloud_until = 0.0
        self.fallbacks = 0

    def wrap(self, name: str):
        primary = getattr(self.cloud, name)
        backup = getattr(self.local, name)

        def call(*args, **kwargs):
            if self.clock() < self.skip_cloud_until:
                self.fallbacks += 1
                return backup(*args, **kwargs)
            try:
                return primary(*args, **kwargs)
            except Exception as exc:                      # noqa: BLE001 - the local model answers
                self.skip_cloud_until = self.clock() + self.cooldown
                self.fallbacks += 1
                logger.warning(
                    "Cloud model %s failed (%s: %s); the local model %s answers, "
                    "and the cloud is skipped for %ds",
                    getattr(self.cloud, "model", "?"), type(exc).__name__, str(exc)[:120],
                    getattr(self.local, "model", "?"), self.cooldown,
                )
                return backup(*args, **kwargs)

        call.__name__ = name
        return call


def install_fallback(cloud, local, **kwargs) -> CloudFallback:
    """Make ``cloud``'s chat calls fall back to ``local``; returns the state."""
    fallback = CloudFallback(cloud, local, **kwargs)
    for name in CHAT_CALLS:
        setattr(cloud, name, fallback.wrap(name))
    cloud.cloud_fallback = fallback
    return fallback
