"""Which messages ask about a recent picture (src/vision.py:is_image_question)."""

import pytest

from src.vision import is_image_question

ABOUT_A_PICTURE = [
    "看看这张图是什么", "这张图里写的什么", "看看这个表情包什么意思", "表情包什么意思",
    "这张图是什么意思", "这张图是谁画的？", "看这张图", "这是什么图片", "这是什么表情包",
    "你发的这张图是什么", "来看看这张图", "求解读这张图", "给我看看这张图",
]
ASKING_FOR_ONE = [
    "发个害羞的表情包", "来个表情包", "给我发个表情包", "发张图", "再来一张图",
    "整个表情包看看", "要个表情包", "你有表情包吗",
]


@pytest.mark.parametrize("text", ABOUT_A_PICTURE)
def test_a_question_about_a_picture(text):
    assert is_image_question(text)


@pytest.mark.parametrize("text", ASKING_FOR_ONE)
def test_asking_for_a_sticker_is_not_a_question_about_a_picture(text):
    """Real-machine round 2, BUG-02: these were routed to the image answer ("我没找到刚才那张图")."""
    assert not is_image_question(text)


def test_the_bot_name_is_ignored():
    assert not is_image_question("@林小满 发个害羞的表情包", bot_name="林小满")
    assert is_image_question("@林小满 这张图是什么意思", bot_name="林小满")
