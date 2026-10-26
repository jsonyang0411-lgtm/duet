"""Telling that something new may be waiting in WeChat without touching the screen.

WeChat draws a red badge on an icon of its left rail when something waits: on the Contacts
icon for a friend request, on the Chat icon for unread messages.  A picture of the window taken
with PrintWindow works while the window is covered and does not move the focus, so looking
costs the user nothing.

The watcher does not try to know which icon is which (no position was read off a real machine).
It remembers the red badges that were there after the last check that found nothing to accept,
and calls the rail "changed" when a badge shows up somewhere new.  An unread message then costs
a needless check; a badge that is not recognised cannot hide a request for longer than the
forced check of AutoFriendService.
"""

from __future__ import annotations

import logging
from typing import Callable

from PIL import Image, ImageChops

logger = logging.getLogger(__name__)

RAIL_MAX_X = 0.09        # the rail is the left strip of the window ...
RAIL_MAX_Y = 0.60        # ... and its icons sit in the upper part
CELL = 4                 # px; the red mask is looked at in cells this big
MIN_COVER = 0.30         # a cell counts as red when this much of it is
MIN_CELLS = 3            # a blob smaller than this is noise
SAME_BADGE_PX = 16       # a badge that moved less than this is the same badge
RED_MIN, GREEN_MAX, BLUE_MAX = 200, 110, 110

_BOX = getattr(getattr(Image, "Resampling", Image), "BOX")


def red_badges(image) -> list[tuple[float, float]]:
    """The centres (px) of the red blobs in the left rail of a window picture."""
    width, height = image.size
    region = image.convert("RGB").crop((0, 0, max(1, int(width * RAIL_MAX_X)), max(1, int(height * RAIL_MAX_Y))))
    red, green, blue = region.split()
    mask = ImageChops.multiply(
        ImageChops.multiply(red.point(lambda v: 255 if v >= RED_MIN else 0),
                            green.point(lambda v: 255 if v <= GREEN_MAX else 0)),
        blue.point(lambda v: 255 if v <= BLUE_MAX else 0),
    )
    cols, rows = max(1, region.width // CELL), max(1, region.height // CELL)
    coverage = mask.resize((cols, rows), _BOX)
    cells = {(x, y) for y in range(rows) for x in range(cols) if coverage.getpixel((x, y)) >= 255 * MIN_COVER}
    return [_centre(blob) for blob in _blobs(cells) if len(blob) >= MIN_CELLS]


def _blobs(cells: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    seen: set[tuple[int, int]] = set()
    blobs = []
    for start in sorted(cells):
        if start in seen:
            continue
        seen.add(start)
        stack, blob = [start], []
        while stack:
            x, y = stack.pop()
            blob.append((x, y))
            for neighbour in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbour in cells and neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        blobs.append(blob)
    return blobs


def _centre(blob: list[tuple[int, int]]) -> tuple[float, float]:
    return (sum(x for x, _ in blob) / len(blob) * CELL + CELL / 2,
            sum(y for _, y in blob) / len(blob) * CELL + CELL / 2)


def _near(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < SAME_BADGE_PX and abs(a[1] - b[1]) < SAME_BADGE_PX


class RailWatcher:
    """``look()`` says whether a red badge has appeared since the last ``settle()``.

    ``grab()`` returns a picture of the WeChat window or raises (minimized, not found, blank).
    """

    CHANGED, UNCHANGED, UNKNOWN = "changed", "unchanged", "unknown"

    def __init__(self, grab: Callable[[], object]):
        self._grab = grab
        self._baseline: list[tuple[float, float]] | None = None

    def look(self) -> str:
        try:
            badges = red_badges(self._grab())
        except Exception as e:
            logger.debug("Rail watch: no picture of the window (%s)", e)
            return self.UNKNOWN
        if self._baseline is None:
            return self.CHANGED
        fresh = [b for b in badges if not any(_near(b, known) for known in self._baseline)]
        return self.CHANGED if fresh else self.UNCHANGED

    def settle(self) -> None:
        """Take the badges on the screen now as "nothing to do" (a new picture, not the last look's:
        the badge of a request that was just accepted may still have been there then)."""
        try:
            self._baseline = red_badges(self._grab())
        except Exception as e:
            logger.debug("Rail watch: no picture to settle on (%s)", e)
            self._baseline = None
