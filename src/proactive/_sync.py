"""One tiny helper: run a method while holding ``self._lock``.

The WeChat backends call the router from a thread pool, so the proactive
trackers and the gate (shared, plain-dict state) are used by several threads at
once.  Every public method takes the instance's re-entrant lock; the critical
sections are a few dict operations, never a model call or a sleep.
"""

import functools


def locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper
