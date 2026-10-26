"""The OCR reader on layouts found by reading a real WeChat window (WeChat 4.x for Mac, light mode).

The rows reproduce the coordinates and colours of an independent chat window of a group; the
texts are made up.  Before the fixes the title was not found (it starts at x=0.014) and the time
lines, the name above a bubble and a video's length were read as the other side's messages.
"""

import pytest

from src.wechat.ocr import reader as R

BACKGROUND = [251, 251, 251]          # WeChat 4.x light mode: the chat itself is near-white


def row(text, x0, y0, bg, x1=None, y1=None):
    return {"t": text, "x0": x0, "x1": x1 or x0 + 0.04 * len(text), "y0": y0, "y1": y1 or y0 + 0.03, "bg": bg}


def independent_window():
    """No session list, no search box: the title is at the far left, bubbles on a near-white background."""
    return [
        row("测试群 (3)", 0.014, 0.066, BACKGROUND, x1=0.063),
        row("13:01", 0.484, 0.167, BACKGROUND),
        row("小明", 0.065, 0.217, BACKGROUND),
        row("对方的第一句话", 0.077, 0.277, [241, 241, 243]),
        row("0:25", 0.166, 0.549, [208, 209, 198]),                  # a video thumbnail's length
        row("14:41", 0.485, 0.616, BACKGROUND),
        row("我发的话", 0.860, 0.680, [187, 240, 181]),
        row("小明", 0.065, 0.745, BACKGROUND),
        row("对方的第二句话", 0.077, 0.783, [241, 241, 243]),
        row("[表情]", 0.020, 0.946, BACKGROUND),                      # the input box area
    ]


def test_an_independent_chat_window_has_a_title_at_the_far_left():
    rows = independent_window()
    geom = R.resolve_chat_geometry(rows)
    assert geom.chat_x0 == 0.0
    assert str(R.chat_title(rows)) == "测试群 (3)"
    assert R.read_rows(rows)["title_clean"] == "测试群"


def test_text_on_the_chat_background_is_not_a_message():
    info = R.read_rows(independent_window())
    assert info["other"] == ["对方的第一句话", "对方的第二句话"]      # no time lines, no names, no video length
    assert info["self"] == ["我发的话"]


def test_the_background_colours_are_the_title_bar_and_the_time_lines():
    plain = R.plain_backgrounds(independent_window())
    assert plain == [BACKGROUND, [208, 209, 198]]                    # the video length counts as a time line


def test_without_a_background_reference_the_kits_reading_is_unchanged():
    assert R.bubble_kind(BACKGROUND) == "other"                      # the kit's rule: near-white is a bubble
    assert R.bubble_kind(BACKGROUND, [BACKGROUND]) == "none"
    assert R.bubble_kind([187, 240, 181], [BACKGROUND]) == "self"
    assert R.bubble_kind([241, 241, 243], [BACKGROUND]) == "other"   # 10 away from the background: a bubble
    assert R.bubble_kind([253, 253, 253], [BACKGROUND]) == "none"    # 2 away: still the background


def test_with_a_search_box_the_left_limit_stays_where_the_kit_had_it():
    rows = independent_window()
    rows.append(row("搜索", 0.03, 0.02, [220, 220, 220], x1=0.2))
    rows.append(row("某个联系人", 0.025, 0.3, [255, 255, 255], x1=0.2))      # a session list entry
    geom = R.resolve_chat_geometry(rows)
    assert geom.chat_x0 >= 0.04


@pytest.mark.parametrize("text,is_clock", [
    ("13:01", True), ("9:05", True), ("0:25", True), ("12:30:59", True),
    ("昨天 12:30", True), ("星期三 08:00", True), ("周五 21:15", True),
    ("10月21日 08:00", True), ("2025年10月21日 08:00", True), ("15:08.", True), ("15:08。", True),
    ("下午3点", False), ("12:3", False), ("会议 13:00 开始", False), ("13:01 到", False), ("", False),
])
def test_time_lines_are_recognized(text, is_clock):
    assert R.is_clock_text(text) is is_clock


def main_window():
    """The main window: session list on the left, a search box whose magnifier OCR reads as the letter Q."""
    sidebar = [238, 238, 238]
    return [
        row("Q 搜索", 0.090, 0.030, [236, 236, 236], x1=0.130),
        row("测试群（4）", 0.217, 0.025, BACKGROUND, x1=0.290),
        # session list: names, previews and times, all on a near-white grey
        row("小明", 0.109, 0.090, sidebar, x1=0.150),
        row("14:47", 0.168, 0.090, sidebar),
        row("[文件] training...", 0.109, 0.140, sidebar, x1=0.230),
        row("测试群", 0.109, 0.210, sidebar, x1=0.150),
        row("小明: @群聊...", 0.109, 0.250, sidebar, x1=0.230),
        row("公众号", 0.109, 0.300, sidebar, x1=0.160),
        # the chat
        row("你邀请小明加入了群聊", 0.400, 0.150, BACKGROUND, x1=0.600),
        row("小明", 0.320, 0.200, BACKGROUND),
        row("你好", 0.330, 0.240, [241, 241, 243]),
        row("14:56", 0.590, 0.440, BACKGROUND),
        row("@群聊小助手 你好", 0.700, 0.490, [187, 240, 181], x1=0.900),
        row("小明", 0.320, 0.600, BACKGROUND),
        row("@群聊小助手 请介绍一下你自己", 0.330, 0.640, [241, 241, 243], x1=0.700),
    ]


@pytest.mark.parametrize("text,is_box", [
    ("搜索", True), ("Q 搜索", True), ("q搜索", True), ("Search", True), ("Q Search", True), ("搜索框", True),
    ("测试群", False), ("Quality 群", False), ("搜索技巧交流群", True),
])
def test_the_search_box_is_recognized_with_its_icon(text, is_box):
    # "startswith 搜索" was the kit's rule, so a group named 搜索… at the title position is not told apart here;
    # the check that matters is that the icon letter does not hide the box
    assert R._is_search_box(text) is is_box


def test_the_main_window_title_is_not_the_search_box():
    rows = main_window()
    assert str(R.chat_title(rows)) == "测试群（4）"
    assert R.read_rows(rows)["title_clean"] == "测试群"


def test_the_session_list_is_not_part_of_the_chat():
    rows = main_window()
    geom = R.resolve_chat_geometry(rows)
    assert 0.15 <= geom.chat_x0 <= 0.22
    info = R.read_rows(rows)
    assert info["other"] == ["你好", "@群聊小助手 请介绍一下你自己"]     # not the names, previews or times on the left
    assert info["self"] == ["@群聊小助手 你好"]


def test_a_message_scrolled_up_under_the_title_bar_is_still_a_bubble():
    """Found live: a bubble at y0=0.075 is in the title band; its colour must not become the "background"."""
    rows = main_window()
    rows.append(row("被顶到标题栏下面的消息", 0.330, 0.075, [241, 241, 243], x1=0.700))
    info = R.read_rows(rows)
    assert "被顶到标题栏下面的消息" in info["other"]
    assert "你好" in info["other"]                                       # the others are bubbles too
    assert R.plain_backgrounds(rows) == [BACKGROUND]                    # only the title's own colour
    assert str(R.chat_title(rows)) == "测试群（4）"


def test_the_sequence_keeps_both_sides_and_time_lines_in_screen_order():
    seq = R.read_rows(independent_window())["sequence"]
    assert seq == [("time", "13:01"), ("other", "对方的第一句话"), ("time", "0:25"), ("time", "14:41"),
                   ("self", "我发的话"), ("other", "对方的第二句话")]
