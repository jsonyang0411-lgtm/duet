"""The silent look at WeChat's left rail: red badges found on a window picture, and what counts as news."""

import pytest
from PIL import Image, ImageDraw

from src.wechat.rail_watch import RailWatcher, red_badges

WECHAT_RED = (250, 81, 81)
DARK_MODE_RED = (230, 67, 64)


def window(*badges, size=(1000, 800), background=(240, 240, 240)):
    """A window picture with round badges: (x, y, diameter, colour) in pixels."""
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    for x, y, diameter, colour in badges:
        draw.ellipse((x, y, x + diameter, y + diameter), fill=colour)
    return image


def centre(x, y, diameter):
    return x + diameter / 2, y + diameter / 2


def close(found, expected, tolerance=6):
    return abs(found[0] - expected[0]) <= tolerance and abs(found[1] - expected[1]) <= tolerance


# ── red_badges ──────────────────────────────────────────────────────────

def test_a_picture_without_red_has_no_badges():
    assert red_badges(window()) == []


def test_a_badge_in_the_rail_is_found_where_it_is():
    found = red_badges(window((40, 120, 16, WECHAT_RED)))
    assert len(found) == 1 and close(found[0], centre(40, 120, 16))


@pytest.mark.parametrize("colour", [WECHAT_RED, DARK_MODE_RED])
def test_the_reds_wechat_uses_are_recognised(colour):
    assert len(red_badges(window((40, 120, 16, colour)))) == 1


def test_two_badges_are_two_blobs():
    assert len(red_badges(window((40, 120, 16, WECHAT_RED), (40, 220, 16, WECHAT_RED)))) == 2


def test_a_badge_with_a_number_in_it_is_still_one_badge():
    image = window((40, 120, 20, WECHAT_RED))
    ImageDraw.Draw(image).rectangle((47, 125, 52, 135), fill=(255, 255, 255))      # the digit
    found = red_badges(image)
    assert len(found) == 1 and close(found[0], centre(40, 120, 20))


def test_red_outside_the_rail_is_not_looked_at():
    assert red_badges(window((500, 120, 16, WECHAT_RED))) == []        # in the chat area
    assert red_badges(window((40, 700, 16, WECHAT_RED))) == []         # below the icons


def test_a_speck_is_noise():
    assert red_badges(window((40, 120, 3, WECHAT_RED))) == []


@pytest.mark.parametrize("colour", [(255, 150, 0), (7, 193, 96), (60, 60, 60)])
def test_other_colours_are_not_badges(colour):
    assert red_badges(window((40, 120, 16, colour))) == []


def test_the_size_of_the_window_picture_does_not_matter():
    found = red_badges(window((60, 200, 30, WECHAT_RED), size=(2000, 1600)))
    assert len(found) == 1 and close(found[0], centre(60, 200, 30))


# ── RailWatcher ─────────────────────────────────────────────────────────

class Screen:
    """What grab() shows; swap .image to change what is on the screen."""

    def __init__(self, image=None):
        self.image = image or window()
        self.fail = None

    def grab(self):
        if self.fail:
            raise self.fail
        return self.image


def test_the_first_look_has_nothing_to_compare_with():
    assert RailWatcher(Screen().grab).look() == RailWatcher.CHANGED


def test_nothing_new_after_settling_is_unchanged():
    screen = Screen(window((40, 120, 16, WECHAT_RED)))        # an unread chat badge, say
    watcher = RailWatcher(screen.grab)
    watcher.settle()
    assert watcher.look() == RailWatcher.UNCHANGED


def test_a_new_badge_is_news():
    screen = Screen(window((40, 120, 16, WECHAT_RED)))
    watcher = RailWatcher(screen.grab)
    watcher.settle()
    screen.image = window((40, 120, 16, WECHAT_RED), (40, 190, 16, WECHAT_RED))
    assert watcher.look() == RailWatcher.CHANGED


def test_a_badge_that_went_away_is_not_news():
    screen = Screen(window((40, 120, 16, WECHAT_RED)))
    watcher = RailWatcher(screen.grab)
    watcher.settle()
    screen.image = window()
    assert watcher.look() == RailWatcher.UNCHANGED


def test_a_badge_that_moved_a_little_is_the_same_badge_but_a_far_one_is_new():
    screen = Screen(window((40, 120, 16, WECHAT_RED)))
    watcher = RailWatcher(screen.grab)
    watcher.settle()
    screen.image = window((43, 123, 16, WECHAT_RED))
    assert watcher.look() == RailWatcher.UNCHANGED
    screen.image = window((40, 180, 16, WECHAT_RED))
    assert watcher.look() == RailWatcher.CHANGED


def test_a_window_that_cannot_be_pictured_is_unknown_not_unchanged():
    screen = Screen()
    watcher = RailWatcher(screen.grab)
    watcher.settle()
    screen.fail = RuntimeError("WeChat is minimized")
    assert watcher.look() == RailWatcher.UNKNOWN


def test_settling_on_a_failed_picture_forgets_the_baseline_so_the_next_look_checks():
    screen = Screen()
    watcher = RailWatcher(screen.grab)
    watcher.settle()
    screen.fail = RuntimeError("blank")
    watcher.settle()
    screen.fail = None
    assert watcher.look() == RailWatcher.CHANGED


def test_settle_takes_a_new_picture_not_the_last_look():
    """The request's badge was on the screen at the last look; once accepted it is gone.  Settling on the
    old picture would keep it in the baseline, and the next request would look like the same badge."""
    request_badge = window((40, 190, 16, WECHAT_RED))
    screen = Screen(request_badge)
    watcher = RailWatcher(screen.grab)
    assert watcher.look() == RailWatcher.CHANGED              # a request is waiting
    screen.image = window()                                    # it is accepted: the badge is gone
    watcher.settle()
    screen.image = request_badge                               # the next request, same icon
    assert watcher.look() == RailWatcher.CHANGED
