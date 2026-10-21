"""Addressing levels and signals (src/pipeline/addressing.py)."""

import pytest

from src.pipeline.addressing import (
    LEVEL_AMBIENT,
    LEVEL_EXPLICIT,
    LEVEL_STRONG,
    LEVEL_WEAK,
    SIGNAL_AT,
    SIGNAL_AT_OTHER,
    SIGNAL_FOLLOWUP,
    SIGNAL_FORCED,
    SIGNAL_FULL_FEED,
    SIGNAL_KEYWORD,
    SIGNAL_NAME_CALL,
    SIGNAL_PRIVATE,
    SIGNAL_QUOTE_BOT,
    SIGNAL_QUOTE_OTHER,
    SIGNAL_STICKY,
    Addressing,
    bot_names,
    calls_by_name,
    classify,
    clean_content,
    leading_mentions,
    mentions_in,
    mentions_other,
)

NAMES = bot_names("小助手", "林小满", ["小满", "Bot"])


def msg(content="你好", **flags):
    base = {"content": content, "is_at_mentioned": False, "is_reply_to_bot": False,
            "quoted_content": ""}
    base.update(flags)
    return base


def level_of(message, **kwargs):
    kwargs.setdefault("names", NAMES)
    return classify(message, **kwargs)


# ── names ───────────────────────────────────────────────────────────────

def test_bot_names_dedupes_and_drops_blank_and_one_character_names():
    assert bot_names("小助手", "小助手", " ", "我", None, ["小满", "bot", "BOT"]) == (
        "小助手", "小满", "bot",
    )


def test_calls_by_name_is_case_insensitive_and_needs_a_real_name():
    assert calls_by_name("小满在吗", NAMES)
    assert calls_by_name("hey bot 帮个忙", NAMES)
    assert not calls_by_name("今天吃什么", NAMES)
    assert not calls_by_name("今天吃什么", ())


def test_the_wechat_quote_prefix_is_not_content():
    assert clean_content("wxid_abc123:\n小满来一下") == "小满来一下"
    assert calls_by_name("wxid_abc123:\n今天吃什么", NAMES) is False


# ── mentions ────────────────────────────────────────────────────────────

def test_mentions_are_found_at_word_starts_only():
    assert mentions_in("@阿明 你吃了吗") == ["阿明"]
    assert mentions_in("看这个 @阿明 @小红 都来") == ["阿明", "小红"]
    assert mentions_in("发到 a@example.com 吧") == []


def test_mentions_other_ignores_the_bot_itself():
    assert not mentions_other("@小助手 你好", NAMES)
    assert not mentions_other("@林小满 在吗", NAMES)
    assert mentions_other("@阿明 你吃了吗", NAMES)
    assert mentions_other("@小助手 帮 @阿明 看看", NAMES)


def test_leading_mentions_stop_at_the_first_word_of_text():
    assert leading_mentions("@阿明 @小助手 来看") == ["阿明", "小助手"]
    assert leading_mentions("@阿明\u2005你别老@小助手") == ["阿明"]
    assert leading_mentions("大家好 @小助手") == []
    assert leading_mentions("wxid_abc123:\n@阿明 好") == ["阿明"]
    assert leading_mentions("") == []


# ── explicit ────────────────────────────────────────────────────────────

def test_an_at_mention_is_explicit():
    result = level_of(msg("@小助手 你好", is_at_mentioned=True))
    assert result.level == LEVEL_EXPLICIT
    assert result.signals == (SIGNAL_AT,)
    assert result.explicit


def test_a_quote_of_the_bot_is_explicit_without_a_visible_at():
    result = level_of(msg("好啊", is_at_mentioned=True, is_reply_to_bot=True))
    assert result.signals == (SIGNAL_QUOTE_BOT,)
    assert result.level == LEVEL_EXPLICIT


def test_a_quote_of_the_bot_with_a_visible_at_has_both_signals():
    result = level_of(msg("@小助手 好啊", is_at_mentioned=True, is_reply_to_bot=True))
    assert set(result.signals) == {SIGNAL_QUOTE_BOT, SIGNAL_AT}


def test_private_and_forced_routes_are_explicit():
    assert level_of(msg("在吗"), private=True).signals == (SIGNAL_PRIVATE,)
    assert level_of(msg("随便一句"), forced=True, full_feed=True).signals == (
        SIGNAL_FORCED, SIGNAL_FULL_FEED,
    )
    assert level_of(msg("随便一句"), forced=True).level == LEVEL_EXPLICIT


def test_an_at_after_some_text_is_still_a_call():
    result = level_of(msg("大家好 @小助手 帮我查一下", is_at_mentioned=True))
    assert result.level == LEVEL_EXPLICIT and result.signals == (SIGNAL_AT,)


def test_the_bot_among_the_leading_mentions_keeps_the_call_explicit():
    result = level_of(msg("@阿明 @小助手 来看看", is_at_mentioned=True))
    assert result.level == LEVEL_EXPLICIT and result.signals == (SIGNAL_AT,)
    result = level_of(msg("@小助手 帮 @阿明 看看", is_at_mentioned=True))
    assert result.level == LEVEL_EXPLICIT


def test_a_message_that_opens_by_addressing_someone_else_is_not_a_forced_call():
    # Talking about the bot to 阿明, with the bot's own @ buried in the text.
    about = level_of(msg("@阿明 你别老@小助手 它回复很怪", is_at_mentioned=True))
    assert about.level == LEVEL_STRONG
    assert {SIGNAL_AT_OTHER, SIGNAL_AT} <= set(about.signals)
    # Relaying the bot's answer: a quote of the bot, but addressed to 阿明.
    relay = level_of(msg("@阿明 听到没 她说你去订位置", is_at_mentioned=True,
                         is_reply_to_bot=True, quoted_content="我觉得阿明更适合去。"))
    assert relay.level == LEVEL_STRONG
    assert SIGNAL_QUOTE_BOT in relay.signals and SIGNAL_AT_OTHER in relay.signals


def test_private_chats_and_forced_groups_ignore_the_opening_mention():
    assert level_of(msg("@阿明 在吗"), private=True).level == LEVEL_EXPLICIT
    assert level_of(msg("@阿明 在吗"), forced=True).level == LEVEL_EXPLICIT


def test_other_signals_do_not_matter_once_explicit():
    result = level_of(msg("@阿明 @小助手 来", is_at_mentioned=True, _kw_triggered=True,
                          _sticky_triggered=True))
    assert result.signals == (SIGNAL_AT,)


# ── strong ──────────────────────────────────────────────────────────────

def test_a_name_call_without_an_at_is_strong():
    result = level_of(msg("小满你觉得呢"))
    assert result.level == LEVEL_STRONG
    assert result.signals == (SIGNAL_NAME_CALL,)


def test_keyword_and_sticky_are_strong():
    assert level_of(msg("在吗", _kw_triggered=True)).signals == (SIGNAL_KEYWORD,)
    assert level_of(msg("那明天呢", _sticky_triggered=True)).signals == (SIGNAL_STICKY,)
    assert level_of(msg("那明天呢", _sticky_triggered=True)).level == LEVEL_STRONG


def test_signals_accumulate_in_a_fixed_order():
    result = level_of(msg("小满在吗", _kw_triggered=True, _sticky_triggered=True))
    assert result.signals == (SIGNAL_KEYWORD, SIGNAL_NAME_CALL, SIGNAL_STICKY)


# ── weak ────────────────────────────────────────────────────────────────

def test_a_followup_candidate_is_weak_and_keeps_the_context():
    result = level_of(msg(
        "哪首", _followup_listen_triggered=True,
        _followup_listen_reason="short-question", _followup_bot_reply="推荐稻香",
    ))
    assert result.level == LEVEL_WEAK
    assert result.signals == (SIGNAL_FOLLOWUP,)
    assert result.followup_reason == "short-question"
    assert result.last_bot_reply == "推荐稻香"


def test_strong_beats_weak():
    result = level_of(msg("小满 哪首", _followup_listen_triggered=True))
    assert result.level == LEVEL_STRONG
    assert SIGNAL_FOLLOWUP in result.signals


# ── ambient and the negative signals ─────────────────────────────────────

def test_nothing_pointing_at_the_bot_is_ambient():
    result = level_of(msg("今天好热"))
    assert result == Addressing(LEVEL_AMBIENT)
    assert not result.explicit


def test_aiming_at_someone_else_cancels_the_inferred_signals():
    sticky = level_of(msg("@阿明 你吃了吗", _sticky_triggered=True))
    assert sticky.level == LEVEL_AMBIENT
    assert sticky.signals == (SIGNAL_AT_OTHER,)
    follow = level_of(msg("哪首", quoted_content="某人的话", _followup_listen_triggered=True))
    assert follow.level == LEVEL_AMBIENT
    assert follow.signals == (SIGNAL_QUOTE_OTHER,)


def test_a_name_call_survives_an_at_or_quote_of_someone_else():
    result = level_of(msg("@阿明 小满说得对"))
    assert result.level == LEVEL_STRONG
    assert result.signals == (SIGNAL_AT_OTHER, SIGNAL_NAME_CALL)
    quoted = level_of(msg("小助手 评价一下", quoted_content="他说的话"))
    assert quoted.level == LEVEL_STRONG
    assert SIGNAL_QUOTE_OTHER in quoted.signals


# ── trace output ────────────────────────────────────────────────────────

def test_as_trace_is_json_friendly():
    result = level_of(msg("哪首", _followup_listen_triggered=True,
                          _followup_listen_reason="short-question"))
    assert result.as_trace() == {
        "level": "weak", "signals": ["followup"], "followup_reason": "short-question",
    }
    assert level_of(msg("你好")).as_trace() == {"level": "ambient", "signals": []}


@pytest.mark.parametrize("flags,expected", [
    ({"is_at_mentioned": True}, LEVEL_EXPLICIT),
    ({"_sticky_triggered": True}, LEVEL_STRONG),
    ({"_followup_listen_triggered": True}, LEVEL_WEAK),
    ({}, LEVEL_AMBIENT),
])
def test_level_table(flags, expected):
    assert level_of(msg("随便", **flags)).level == expected
