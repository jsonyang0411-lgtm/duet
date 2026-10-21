"""Text helpers of memory v2 (src/memory/text.py)."""

import time

import pytest

from src.memory import text as T


# ── canon / quote_matches ───────────────────────────────────────────────

def test_canon_keeps_letters_and_digits_only_and_folds_width_and_case():
    assert T.canon("Ｈello， World！ 123 ​好") == "helloworld123好"
    assert T.canon("😀🎉 ~~") == ""
    assert T.canon(None) == ""


@pytest.mark.parametrize("quote, content", [
    ("我是次元哥哥", "大家好，我是次元哥哥！"),
    ("周六去爬山你来不", "周六去爬山，你来不？"),        # punctuation differs
    ("PYTHON 很好用", "我觉得 python很好用"),               # case and spacing differ
])
def test_a_real_excerpt_matches(quote, content):
    assert T.quote_matches(quote, content)


@pytest.mark.parametrize("quote, content", [
    ("我是次元哥哥", "我是宇肆"),                  # invented
    ("好", "好的好的"),                             # too short to prove anything
    ("好的", "好的好的"),                           # still under the 3-character minimum
    ("", "任何内容"),
    ("😀😀😀", "😀😀😀"),                           # nothing but symbols
])
def test_an_invented_or_too_short_quote_does_not_match(quote, content):
    assert not T.quote_matches(quote, content)


def test_the_minimum_length_can_be_lowered_for_names():
    assert T.quote_matches("小王", "叫他小王吧", min_chars=2)


def test_norm_key_ignores_spacing_punctuation_and_case():
    assert T.norm_key("小明", "喜欢", "Coffee!") == T.norm_key(" 小明 ", "喜欢", "coffee")
    assert T.norm_key("小明", "喜欢", "咖啡") != T.norm_key("小明", "讨厌", "咖啡")


# ── query terms ─────────────────────────────────────────────────────────

def test_question_scaffolding_is_removed_before_trigrams_are_cut():
    assert T.query_grams("上次小明说想去哪里玩") == ["小明说", "明说想", "说想去"]


def test_single_character_particles_are_not_scaffolding():
    # 吧 and 哈 are inside 酒吧 / 哈尔滨; cutting them would damage the word.
    assert "酒吧" in T.content_runs("那个酒吧")[0]
    assert T.query_grams("哈尔滨") == ["哈尔滨"]


def test_query_grams_are_distinct_ordered_and_capped():
    assert T.query_grams("爬山爬山爬山") == ["爬山爬", "山爬山"]
    assert len(T.query_grams("一二三四五六七八九十" * 10, max_grams=7)) == 7
    assert T.query_grams("") == [] and T.query_grams("好") == []


def test_ascii_words_are_lowercased_for_the_index():
    assert T.query_grams("Python3") == ["pyt", "yth", "tho", "hon", "on3"]


def test_two_character_words_are_strong_terms_and_inner_pairs_are_weak():
    strong, weak = T.short_terms("加班")
    assert strong == ["加班"] and weak == []
    strong, weak = T.short_terms("谁加班了")
    assert strong == [] and weak == ["加班"]               # 班了 / 了 contain a function character


def test_weak_terms_skip_pairs_with_function_characters_and_are_capped():
    _, weak = T.short_terms("我的火锅店")
    assert weak == ["火锅", "锅店"]
    _, weak = T.short_terms("天地玄黄宇宙洪荒日月盈昃辰宿列张" , max_weak=3)
    assert len(weak) == 3


def test_overlap_scores_the_share_of_query_pairs_found():
    query = T.bigrams("火锅好吃")
    assert T.overlap(query, "晚上吃火锅吧") > 0
    assert T.overlap(query, "完全不相关") == 0
    assert T.overlap(set(), "任何") == 0.0


# ── sensitive data ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "13812345678", "138 1234 5678", "电话 138-1234-5678", "110101199003071234",
    "me@example.com", "我的密码是abc", "验证码 482913", "api_key=sk-xxx", "银行卡号",
    "QQ 123456789",
])
def test_sensitive_text_is_recognised(text):
    assert T.looks_sensitive(text)


@pytest.mark.parametrize("text", [
    "周六九点集合", "他喜欢火锅", "2026-10-20 去爬山", "小明的生日是三月", "第 12345 次",
])
def test_ordinary_text_is_not_sensitive(text):
    assert not T.looks_sensitive(text)


# ── hostile input must stay fast (the matched text is other people's chat) ─

@pytest.mark.parametrize("hostile", [
    "a" * 100000, "哈" * 100000, ("a." * 50000) + "@", "1" * 100000, "@" * 100000,
    "​" * 100000, "上次" * 50000, ("谁" * 50000), "(" * 50000,
], ids=lambda s: f"{s[:2]}..{len(s)}")        # a short id: see _test_ids_stay_short in conftest.py
def test_hostile_inputs_are_processed_in_linear_time(hostile):
    started = time.perf_counter()
    T.canon(hostile)
    T.quote_matches(hostile, hostile)
    T.query_grams(hostile)
    T.short_terms(hostile)
    T.looks_sensitive(hostile)
    T.bigrams(hostile)
    assert time.perf_counter() - started < 1.0
