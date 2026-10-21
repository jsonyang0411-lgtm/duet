"""Characterization tests for chat-reply cleaning (Phase 0 safety net).

``AbstractSummarizer._clean_chat_reply`` is the last gate before a model answer
is sent to WeChat.  These tests pin what it does TODAY with realistic raw model
output -- including the parts that are known to be wrong -- so that later
phases change the behaviour on purpose and visibly.

Convention: a case marked ``# PINNED (known issue, expected to change in Phase
N): <why>`` documents behaviour that is deliberately NOT endorsed.  When the
behaviour is fixed, edit the expectation in the same change.

Order of operations inside ``_clean_chat_reply`` (read from the code and relied
on by several cases below):

1. ``str().strip()``; silence check on the raw text
2. drop ONE leading "speaker：" prefix, but only when it is a known name
   (persona / bot / requester) or an output label such as "回复" (Phase 1:
   before, ANY 1..12 chars before the first colon were dropped)
3. newlines become spaces, whitespace runs collapse to one space
4. drop trailing "】【" / "[]" markers, then trailing ``' ，,、;；'``
5. silence check, strip parenthetical asides, silence check again
6. drop punctuation-only output
7. over-long text is cut at the last sentence end / clause break inside the
   budget when that keeps at least half of it, else hard-cut (Phase 1)
"""

import pytest

from src.persona import DEFAULT_CHAT_MAX_CHARS
from src.summarize.base import AbstractSummarizer

clean = AbstractSummarizer._clean_chat_reply
is_silence = AbstractSummarizer._is_silence_meta_reply
strip_asides = AbstractSummarizer._strip_parenthetical_asides

MAX = DEFAULT_CHAT_MAX_CHARS  # 36, what chat() uses when nothing is configured

# Speaker names chat() / proactive_chat() pass to the cleaner.
NAMES = ("林小满", "杨宇靖", "小助手", "阿明")


def _cases(*rows):
    """(id, raw, expected) rows -> pytest.param list (ASCII ids keep reports readable)."""
    return [pytest.param(raw, expected, id=case_id) for case_id, raw, expected in rows]


def _raws(*rows):
    """(id, raw) rows -> pytest.param list."""
    return [pytest.param(raw, id=case_id) for case_id, raw in rows]


# ── plain text ─────────────────────────────────────────────────────────

class TestPlainText:
    @pytest.mark.parametrize("raw, expected", _cases(
        ("short-chinese-sentence", "好呀，我试试看", "好呀，我试试看"),
        ("question", "啊？什么意思呀", "啊？什么意思呀"),
        ("ellipsis-inside", "嗯……我再想想", "嗯……我再想想"),
        ("ascii-only", "hello there", "hello there"),
        ("mixed-cjk-ascii-digits", "今晚8点开黑吗", "今晚8点开黑吗"),
        ("single-character", "嗯", "嗯"),
        ("outer-whitespace-trimmed", "  \n 好哦 \t ", "好哦"),
        ("trailing-fullwidth-period-kept", "知道啦。", "知道啦。"),
    ))
    def test_replies_pass_through(self, raw, expected):
        assert clean(raw, MAX) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "\n\t \r\n", 0],
                             ids=["none", "empty", "spaces", "only-newlines", "zero"])
    def test_empty_like_input_is_blank(self, raw):
        assert clean(raw, MAX) == ""

    def test_non_string_input_is_stringified(self):
        assert clean(12345, MAX) == "12345"

    # PINNED (known issue, expected to change in Phase 2): the prompt forbids
    # emoji/kaomoji but the cleaner never enforces it; only the part of a
    # kaomoji that sits inside round brackets disappears (as a "parenthetical
    # aside"), which can leave debris behind.
    @pytest.mark.parametrize("raw, expected", _cases(
        ("emoji-kept", "好的😂", "好的😂"),
        ("emoji-only-kept", "😂", "😂"),
        ("ascii-kaomoji-kept", "^_^", "^_^"),
        ("ascii-smiley-kept", ":)", ":)"),
        ("bracketed-kaomoji-dropped", "(￣▽￣)", ""),
        ("kaomoji-with-arms-leaves-debris", "好哒~(≧▽≦)/", "好哒~ /"),
    ))
    def test_emoji_and_kaomoji_are_not_filtered(self, raw, expected):
        assert clean(raw, MAX) == expected


# ── silence explanations that must never reach WeChat ──────────────────

class TestSilenceDetection:
    """``_is_silence_meta_reply`` rule by rule, then the cleaner on top."""

    # Rule 1: the whole reply is one or more round-bracket groups (1..180
    # chars each, no nesting).
    @pytest.mark.parametrize("raw", _raws(
        ("fullwidth", "（沉默）"),
        ("ascii", "(沉默)"),
        ("stage-direction", "（低下头）"),
        ("two-groups", "（轻轻点头）（脸红）"),
        ("long-explanation", "（这句话不是在对我说的，所以我选择沉默）"),
        ("ascii-two-groups-with-space", "(没敢接话) (悄悄看了一眼)"),
        ("exactly-180-inside", "（" + "啊" * 180 + "）"),
    ))
    def test_rule1_whole_reply_in_round_brackets(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, 400) == ""

    # Rule 2: after removing whitespace, lower-casing and stripping bracket /
    # quote characters from both ends, the text is one of nine exact tokens.
    @pytest.mark.parametrize("raw", _raws(
        ("token-kongbai", "空白"),
        ("token-chenmo", "沉默"),
        ("token-blank", "blank"),
        ("token-null", "null"),
        ("token-none", "none"),
        ("token-reply-blank", "回复空白"),
        ("token-output-blank", "输出空白"),
        ("token-no-reply", "不回复"),
        ("token-no-response", "不回应"),
        ("lenticular-brackets", "【空白】"),
        ("corner-brackets", "「沉默」"),
        ("curly-quotes", "“沉默”"),
        ("square-brackets", "[blank]"),
        ("upper-case", "BLANK"),
        ("capitalised", "Null"),
        ("none-upper", "NONE"),
        ("inner-spaces", " 空 白 "),
        ("spaced-four-chars", "回 复 空 白"),
        ("inner-newline", "空\n白"),
        ("unclosed-open-bracket", "（空白"),
        ("unclosed-close-bracket", "空白）"),
    ))
    def test_rule2_exact_tokens_after_normalisation(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, MAX) == ""

    # Rule 3: any of eleven "no need to reply" markers anywhere in the text.
    @pytest.mark.parametrize("raw", _raws(
        ("need-not-reply", "这条不需要回复。"),
        ("no-need-reply", "无需回复，保持沉默"),
        ("no-use-reply", "我觉得不用回复了"),
        ("do-not-reply", "这个不要回复比较好"),
        ("should-not-reply", "不该回复"),
        ("ought-not-reply", "我不应该回复这种话"),
        ("need-not-respond", "不需要回应"),
        ("no-need-respond", "无需回应"),
        ("no-use-respond", "不用回应他"),
        ("should-not-respond", "不该回应他"),
        ("ought-not-respond", "不应该回应"),
    ))
    def test_rule3_hard_markers(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, MAX) == ""

    # Rule 4: "沉默" together with one of four qualifiers.
    @pytest.mark.parametrize("raw", _raws(
        ("with-uncertain", "我不确定该不该说，先沉默"),
        ("with-not-addressed", "他们不是在对我说话，我沉默"),
        ("with-no-clear", "没有明确的话题，沉默"),
        ("with-should", "我应该保持沉默"),
    ))
    def test_rule4_silence_word_plus_qualifier(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, MAX) == ""

    # Rule 5: "没有明确" plus a pointer to "me/you" (no 沉默 needed).
    @pytest.mark.parametrize("raw", _raws(
        ("points-to-me", "这句话没有明确指向我"),
        ("points-to-you", "没有明确指向你"),
        ("says-to-me", "他没有明确对我说什么"),
        ("says-to-you", "他没有明确对你说"),
    ))
    def test_rule5_no_clear_addressee(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, MAX) == ""

    # Rule 6: "不是在对我/你" plus 回复/回应/沉默.
    @pytest.mark.parametrize("raw", _raws(
        ("not-to-me-reply", "他不是在对我说话，所以不回复"),
        ("not-to-you-respond", "他不是在对你说话，不回应"),
        ("not-to-me-short", "不是在对我说，没必要回复"),
    ))
    def test_rule6_not_addressed_to_me(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, MAX) == ""

    @pytest.mark.parametrize("raw", _raws(
        ("empty", ""),
        ("none", None),
        ("plain-reply", "好呀"),
        ("silence-word-alone-in-proverb", "沉默是金"),
        ("silence-proverb-in-sentence", "我觉得沉默是金"),
        ("blank-inside-sentence", "这个文档是空白的"),
        ("no-clear-answer", "没有明确的答案"),
        ("not-to-me-without-reply-verb", "他不是在对我说话"),
    ))
    def test_ordinary_text_is_not_silence(self, raw):
        assert is_silence(raw) is False

    # PINNED (known issue, expected to change in Phase 2): the markers are
    # plain substring tests, so a legitimate reply that merely contains one is
    # blanked and the user gets no answer at all.
    @pytest.mark.parametrize("raw", _raws(
        ("goodnight-contains-no-use-reply", "你不用回复我也没关系，我先去睡了"),
        ("advice-contains-do-not-reply", "不要回复那个人了，他在骗你"),
        ("casual-contains-need-not-respond", "不需要回应也行啦"),
        ("advice-silence-plus-should", "你们吵架的时候应该沉默一下"),
        ("encouragement-silence-plus-should", "你应该多说说话，别总是沉默"),
    ))
    def test_false_kills_legitimate_reply_is_blanked(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, MAX) == ""

    # PINNED (known issue, expected to change in Phase 2): any reply that is
    # nothing but one round-bracket group is treated as a stage direction, even
    # when it is a deliberate "reaction" message.
    @pytest.mark.parametrize("raw", _raws(
        ("laugh", "（笑）"),
        ("ascii-facepalm", "(捂脸)"),
        ("long-laugh", "（哈哈哈哈哈）"),
    ))
    def test_false_kills_pure_parenthetical_reaction(self, raw):
        assert is_silence(raw) is True
        assert clean(raw, MAX) == ""

    # PINNED (known issue, expected to change in Phase 2): silence explanations
    # that slip past the rules are sent to the group verbatim.
    @pytest.mark.parametrize("raw", _raws(
        ("chenmo-period", "沉默。"),
        ("chenmo-dots", "沉默..."),
        ("kongbai-period", "空白。"),
        ("blank-period", "blank."),
        ("null-period", "Null."),
        ("reply-blank-period", "回复空白。"),
        ("keep-silent", "保持沉默"),
        ("no-reply-period", "不回复。"),
        ("choose-no-reply", "选择不回复"),
        ("need-not-me-reply", "这条消息不需要我回复"),
        ("not-to-you-statement", "他不是在对你说话"),
        ("angle-blank", "<blank>"),
        ("empty-lenticular", "【】"),
        ("spaced-dots", "…… 沉默 ……"),
        ("unclosed-aside", "好的（笑"),
    ))
    def test_leaks_meta_text_that_is_still_sent(self, raw):
        assert is_silence(raw) is False
        assert clean(raw, MAX) == raw

    # PINNED (known issue, expected to change in Phase 2): these are blanked
    # only AFTER the speaker prefix / asides / punctuation have been removed, so
    # ``is_silence(raw)`` is False.  chat() derives its "intentional silence"
    # flag from the raw text, so these count as an *unexplained empty reply*.
    @pytest.mark.parametrize("raw", _raws(
        ("name-prefix-plus-kongbai", "林小满：空白"),
        ("label-plus-chenmo", "输出：沉默"),
        ("label-plus-kongbai", "回复：空白"),
        ("no-reply-plus-aside", "不回复（沉默）"),
        ("chenmo-trailing-comma", "沉默，"),
        ("dots-plus-aside", "……（沉默）"),
        ("empty-square-brackets", "[]"),
    ))
    def test_blank_only_after_cleanup_is_not_flagged_as_silence_meta(self, raw):
        assert clean(raw, MAX, names=NAMES) == ""
        assert is_silence(raw) is False


# ── parenthetical asides ────────────────────────────────────────────────

class TestParentheticalAsides:
    @pytest.mark.parametrize("raw, expected", _cases(
        ("suffix", "好的（点头）", "好的"),
        ("prefix", "（点头）好的", "好的"),
        ("both-ends", "（脸红）你不要乱说（低头）", "你不要乱说"),
        ("ascii-brackets", "好的(笑)", "好的"),
        ("after-line-break", "好呀\n\n（轻轻点头）", "好呀"),
        ("before-reply", "（轻轻点头）好呀，我试试", "好呀，我试试"),
        ("after-speaker-prefix", "林小满：（脸红）嗯", "嗯"),
        ("silence-word-inside-aside-before-answer", "（空白）好的", "好的"),
        ("silence-word-inside-aside-after-answer", "好的（空白）", "好的"),
    ))
    def test_asides_are_removed(self, raw, expected):
        assert clean(raw, MAX, names=NAMES) == expected

    # PINNED (known issue, expected to change in Phase 2): an aside in the
    # middle of a Chinese sentence leaves a visible space behind.
    def test_aside_in_the_middle_leaves_a_space(self):
        assert clean("我（小声）觉得可以", MAX) == "我 觉得可以"

    # PINNED (known issue, expected to change in Phase 2): the aside regex has
    # no notion of nesting, so the inner group goes and the outer half stays.
    def test_nested_brackets_are_mangled(self):
        assert clean("好的（笑（大））", MAX) == "好的（笑 ）"

    # PINNED (known issue, expected to change in Phase 2): groups longer than
    # 180 characters are not recognised, neither as an aside nor as a whole
    # silence reply.
    def test_aside_longer_than_180_chars_is_not_recognised(self):
        long_aside = "（" + "啊" * 181 + "）"
        assert clean("好的" + long_aside, 400) == "好的" + long_aside
        assert is_silence(long_aside) is False
        assert clean(long_aside, 400) == long_aside

    def test_asides_are_removed_before_the_character_limit_is_applied(self):
        # the 8 kept characters are "你好呀" + the space left by the aside +
        # "今天天气"; the aside itself does not use up any of the limit.
        assert clean("你好呀（笑）今天天气真好啊", 8) == "你好呀 今天天气"

    def test_strip_parenthetical_asides_helper(self):
        assert strip_asides("") == ""
        assert strip_asides(None) == ""
        assert strip_asides("没有括号") == "没有括号"
        assert strip_asides("（a）b（c）") == "b"
        assert strip_asides("a   b\t c") == "a b c"
        assert strip_asides("  ( x ) y ") == "y"


# ── speaker prefixes ────────────────────────────────────────────────────

class TestSpeakerPrefix:
    @pytest.mark.parametrize("raw, expected", _cases(
        ("persona-fullwidth-colon", "林小满：你好呀", "你好呀"),
        ("bot-ascii-colon", "小助手: hello", "hello"),
        ("requester-name", "阿明：你好", "你好"),
        ("case-insensitive-label", "Assistant: hi", "hi"),
        ("output-label", "回复：好的", "好的"),
        ("prefix-then-line-break", "林小满：\n你好", "你好"),
        ("only-first-prefix-is-removed", "林小满：阿明：你好", "阿明：你好"),
        ("prefix-only", "林小满：", ""),
        ("second-line-prefix-is-kept", "你好\n林小满：再见", "你好 林小满：再见"),
    ))
    def test_known_speaker_labels_are_stripped(self, raw, expected):
        assert clean(raw, MAX, names=NAMES) == expected

    # Fixed in Phase 1: only known speaker names and output labels are
    # removed.  Everything else before a colon is content: times, scores,
    # URLs, lead-ins, smileys and unknown names must survive untouched.
    @pytest.mark.parametrize("raw", _raws(
        ("clock-time", "12:30 见"),
        ("time-after-words", "晚上8:30见"),
        ("score", "比分 2:1 赢了"),
        ("url", "https://example.com"),
        ("address-label", "地址：科技路88号"),
        ("reminder-label", "提醒：明天不上课"),
        ("lead-in", "我想说：好的"),
        ("smiley-after-text", "好的:)"),
        ("unknown-name", "小明：你好呀"),
        ("english-name", "Bob: hello"),
    ))
    def test_other_text_before_a_colon_is_content(self, raw):
        assert clean(raw, MAX, names=NAMES) == raw

    def test_without_names_only_output_labels_are_stripped(self):
        assert clean("林小满：好呀", MAX) == "林小满：好呀"
        assert clean("回复：好呀", MAX) == "好呀"

    # PINNED (known issue, expected to change in Phase 2): only "name：" labels
    # are recognised; a bracketed label such as "【林小满】" is sent as part of
    # the message.
    def test_bracketed_speaker_label_is_not_stripped(self):
        assert clean("【林小满】好呀", MAX) == "【林小满】好呀"

    def test_thirteen_chars_before_colon_is_not_a_prefix(self):
        raw = "一二三四五六七八九十甲乙丙：你好"
        assert clean(raw, MAX) == raw

    def test_long_lead_in_survives_by_accident(self):
        raw = "我今天真的特别想跟你说一件事：好的"
        assert clean(raw, MAX) == raw


# ── whitespace and newlines ─────────────────────────────────────────────

class TestWhitespace:
    @pytest.mark.parametrize("raw, expected", _cases(
        ("two-lines-are-joined-with-a-space", "第一行\n第二行", "第一行 第二行"),
        ("crlf", "第一行\r\n第二行", "第一行 第二行"),
        ("blank-lines-collapse", "\n\n你好\n\n\n世界\n\n", "你好 世界"),
        ("tabs-and-runs-of-spaces", "好   呀  \t 嗯", "好 呀 嗯"),
        ("ideographic-space", "你好　世界", "你好 世界"),
        ("question-then-answer", "啊？\n什么意思呀", "啊？ 什么意思呀"),
    ))
    def test_whitespace_is_collapsed_to_one_line(self, raw, expected):
        assert clean(raw, MAX) == expected


# ── dangling markers and trailing punctuation ───────────────────────────

class TestDanglingMarkers:
    @pytest.mark.parametrize("raw, expected", _cases(
        ("repeated-lenticular-pair", "好的】【】【", "好的"),
        ("empty-square-brackets", "好的[]", "好的"),
        ("marker-after-comma", "好的，】【", "好的"),
        ("fullwidth-period-is-kept", "好的。】【", "好的。"),
        ("marker-in-the-middle-is-kept", "好的】【再见", "好的】【再见"),
        ("marker-only", "】【", ""),
    ))
    def test_trailing_marker_removal(self, raw, expected):
        assert clean(raw, MAX) == expected

    @pytest.mark.parametrize("raw, expected", _cases(
        ("fullwidth-comma", "好的，", "好的"),
        ("enumeration-comma", "好的、", "好的"),
        ("semicolon", "好的；", "好的"),
        ("ascii-comma-and-space", "好的, ", "好的"),
        ("period-is-not-stripped", "好的。", "好的。"),
        ("exclamation-is-not-stripped", "好的！", "好的！"),
    ))
    def test_trailing_separators_are_stripped_even_without_a_marker(self, raw, expected):
        assert clean(raw, MAX) == expected


# ── punctuation-only output ─────────────────────────────────────────────

class TestPunctuationOnly:
    @pytest.mark.parametrize("raw", _raws(
        ("tildes", "~~~"),
        ("exclamations", "！！！"),
        ("mixed-ascii", "?!"),
        ("em-dash", "—"),
        ("underscore", "_"),
        ("enumeration-comma", "、"),
        ("fullwidth-tildes", "～～"),
        ("hyphen", "-"),
        ("mixed-fullwidth", "。，！"),
        ("ellipsis-period", "……。"),
        # still blank when longer than the limit: checked before the cut
        ("sixty-periods", "。" * 60),
    ))
    def test_punctuation_only_is_blank(self, raw):
        assert clean(raw, MAX) == ""

    @pytest.mark.parametrize("raw", _raws(
        ("marks-then-text", "？？？什么"),
        ("period-then-letter", "。a"),
        ("one-char-plus-mark", "啊？"),
    ))
    def test_anything_with_a_real_character_is_kept(self, raw):
        assert clean(raw, MAX) == raw

    # PINNED (known issue, expected to change in Phase 2): a space between the
    # marks defeats the punctuation-only check (whitespace is collapsed, not
    # removed), so a bare "。 。" is sent.
    def test_space_separated_punctuation_is_still_sent(self):
        assert clean("。 。", MAX) == "。 。"


# ── hard truncation ─────────────────────────────────────────────────────

class TestHardTruncation:
    def test_text_at_the_limit_is_untouched(self):
        assert clean("1234567890", 10) == "1234567890"

    def test_one_over_the_limit_is_cut(self):
        assert clean("1234567890a", 10) == "1234567890"

    def test_limit_counts_characters_not_bytes(self):
        assert clean("一二三四五六七八九十甲乙", 10) == "一二三四五六七八九十"

    # Phase 1: an over-long reply is cut at the last sentence end / clause
    # break that fits and keeps at least half of the budget; otherwise it is
    # hard-cut (there is nothing better to cut at).
    @pytest.mark.parametrize("raw, limit, expected", [
        pytest.param("一二三，四五六", 4, "一二三", id="drops-the-trailing-comma"),
        pytest.param("你好呀。我明天下午三点在学校门口等你", 8, "你好呀。",
                     id="keeps-the-sentence-end"),
        pytest.param("你好呀！我明天去学校，你呢，来不来", 14, "你好呀！我明天去学校，你呢",
                     id="prefers-the-latest-boundary-that-fits"),
        pytest.param("我觉得周末去爬山挺好的，不过要看天气，如果下雨的话我们可以改成去看电影", 30,
                     "我觉得周末去爬山挺好的，不过要看天气", id="cuts-at-the-last-comma"),
        pytest.param("好的呀那我们就这么定了，周六下午三点在老地方见面，不见不散哦", 20,
                     "好的呀那我们就这么定了", id="cuts-at-the-comma-within-budget"),
    ])
    def test_over_long_reply_is_cut_at_a_clause_boundary(self, raw, limit, expected):
        assert clean(raw, limit) == expected

    @pytest.mark.parametrize("raw, limit, expected", [
        pytest.param("我明天下午三点在学校门口等你，你记得带伞，不然会淋湿的哦", 12,
                     "我明天下午三点在学校门口", id="no-boundary-in-budget"),
        pytest.param("他说「今天天气真好，我们去公园玩吧」", 8, "他说「今天天气真",
                     id="leaves-open-quote"),
        pytest.param("好，这个问题我觉得需要从好几个方面慢慢来考虑才行", 20,
                     "好，这个问题我觉得需要从好几个方面慢慢来", id="boundary-too-early-to-use"),
        pytest.param("哈" * 40, 30, "哈" * 30, id="run-of-laughter"),
    ])
    def test_without_a_usable_boundary_the_cut_is_hard(self, raw, limit, expected):
        assert clean(raw, limit) == expected

    def test_trailing_space_at_the_cut_is_removed(self):
        # the cut gives "12345 " (6 chars) and rstrip() makes it 5
        assert clean("12345 67890", 6) == "12345"

    # PINNED (known issue, expected to change in Phase 2): a SLEEP-mode style
    # ``max_chars == 0`` blanks every reply (see the proactive_chat tests).
    def test_zero_limit_blanks_everything(self):
        assert clean("好的", 0) == ""

    def test_realistic_long_model_reply_is_cut_to_default_limit(self):
        raw = ("好呀，那我们明天中午十二点在学校南门的奶茶店见面，"
               "你记得带上那本书，我还想借你的笔记本看看")
        out = clean(raw, MAX)
        assert len(out) <= MAX
        assert out == "好呀，那我们明天中午十二点在学校南门的奶茶店见面"


# ── a few realistic end-to-end raw outputs ──────────────────────────────

class TestRealisticOutputs:
    # PINNED (known issue, expected to change in Phase 2): Markdown and list
    # markup are forbidden by the prompt but only flattened to one line here.
    @pytest.mark.parametrize("raw, expected", _cases(
        ("persona-prefix-and-stage-direction",
         "林小满：（轻轻低下头）嗯……我不太会看这个，要不你再说一遍？\n（小声）",
         "嗯……我不太会看这个，要不你再说一遍？"),
        ("markdown-bold-is-not-removed", "**好的**，我知道了", "**好的**，我知道了"),
        ("list-style-is-flattened", "1. 先吃饭\n2. 再睡觉", "1. 先吃饭 2. 再睡觉"),
        ("thought-then-answer", "（想了想）我觉得可以呀", "我觉得可以呀"),
        ("thought-then-question", "（想了想）这个我真的不知道，你要不要问问别人？",
         "这个我真的不知道，你要不要问问别人？"),
        ("aside-inside-a-sentence", "我觉得……（沉默了一会儿）还是算了吧", "我觉得…… 还是算了吧"),
        ("trailing-aside-after-hesitation", "嗯……我不太确定呢（轻轻摇头）", "嗯……我不太确定呢"),
        ("fullwidth-tilde-is-kept", "好呀好呀～", "好呀好呀～"),
        ("trailing-blank-lines", "好的\n\n\n", "好的"),
        ("dangling-marker-after-question", "你吃饭了吗？】【", "你吃饭了吗？"),
        ("reply-label-prefix", "回复：好的呀", "好的呀"),
        ("book-title-colon-beyond-12-chars", "你说的是《关于我转生变成史莱姆这档事》吗：我好像看过",
         "你说的是《关于我转生变成史莱姆这档事》吗：我好像看过"),
    ))
    def test_realistic_raw_outputs(self, raw, expected):
        assert clean(raw, MAX, names=NAMES) == expected

    def test_long_laughter_is_cut_to_the_limit(self):
        assert clean("哈" * 60, MAX) == "哈" * MAX

    def test_persona_prefix_and_short_limit(self):
        # the ambient persona uses 30 characters
        assert clean("杨宇靖：哪个\n", 30, names=("杨宇靖",)) == "哪个"
        assert clean("嗯嗯，我知道啦，不过你下次别再这样了好不好，我真的会担心的，呜呜", 30) == (
            "嗯嗯，我知道啦，不过你下次别再这样了好不好，我真的会担心的"
        )
