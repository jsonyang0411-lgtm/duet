"""Characterization tests for GroupFactExtractor (Phase 0 safety net).

``GroupFactExtractor`` turns single chat messages into small durable
"X is also called Y" facts using hard-coded regular expressions.  Those
expressions encode ONE group's running joke (call 宇肆 "次元哥哥"; "cy" is the
pinyin shorthand of "次元").  Every table below pins them exactly as they are
today so that Phase 3 can replace them on purpose.

# PINNED (known issue, expected to change in Phase 3): hard-coded group-specific alias patterns

Convention: ``# PINNED (known issue, expected to change in Phase N): <why>``
marks behaviour that is documented here but NOT endorsed.
"""

import pytest

from src.memory.facts import GroupFact, GroupFactExtractor

DIRECT_CONFIDENCE = 0.86
PRONOUN_CONFIDENCE = 0.82
DERIVED_CONFIDENCE = 0.78


# ── helpers ────────────────────────────────────────────────────────────

def extract(text, recent=None, *, bot_name="", message_id="cur", timestamp=1000):
    message = {"message_id": message_id, "timestamp": timestamp, "content": text}
    return GroupFactExtractor(bot_name).extract(message, recent)


def triples(facts):
    return [(f.subject, f.predicate, f.object) for f in facts]


def pairs(facts):
    """(subject, object) of every fact except the derived "cy" shorthand."""
    return [(f.subject, f.object) for f in facts if f.object != "cy"]


def earlier(content, *, message_id="r1", timestamp=900):
    return {"message_id": message_id, "timestamp": timestamp, "content": content}


def _cases(*rows):
    return [pytest.param(text, expected, id=case_id) for case_id, text, expected in rows]


# ── alias patterns ──────────────────────────────────────────────────────

class TestAliasPatterns:
    # PINNED (known issue, expected to change in Phase 3): hard-coded group-specific alias patterns
    @pytest.mark.parametrize("text, expected", _cases(
        # pattern 1: 叫/称呼 + subject + (为|叫)? + alias
        ("call-subject-alias-long", "叫宇肆次元哥哥", [("宇肆", "次元哥哥")]),
        ("call-subject-alias-short", "叫宇肆次元", [("宇肆", "次元")]),
        ("call-subject-wei-alias", "叫宇肆为次元", [("宇肆", "次元")]),
        ("chenghu-subject-wei-alias", "称呼宇肆为次元哥哥", [("宇肆", "次元哥哥")]),
        ("trailing-yekeyi", "叫宇肆次元哥哥也可以", [("宇肆", "次元哥哥")]),
        ("trailing-jiuxing", "叫宇肆次元哥哥就行", [("宇肆", "次元哥哥")]),
        ("trailing-ba", "叫宇肆次元哥哥吧", [("宇肆", "次元哥哥")]),
        ("trailing-ya", "叫宇肆次元哥哥呀", [("宇肆", "次元哥哥")]),
        ("niuniu-alias", "叫宇肆牛牛哥哥", [("宇肆", "牛牛哥哥")]),
        # pattern 2: (看到|见到)? + subject + (该|要|应该)? + 叫 + alias
        ("see-subject-should-call", "看到宇肆该叫次元", [("宇肆", "次元")]),
        ("meet-subject-must-call", "见到宇肆要叫次元哥哥", [("宇肆", "次元哥哥")]),
        ("see-subject-ought-to-call", "看到宇肆应该叫次元大哥", [("宇肆", "次元大哥")]),
        ("subject-calls-alias", "宇肆叫次元哥哥", [("宇肆", "次元哥哥")]),
        # pattern 3: @subject + 叫 + alias
        ("at-subject-space-call", "@宇肆 叫次元哥哥", [("宇肆", "次元哥哥")]),
        ("at-subject-call", "@宇肆叫次元哥哥", [("宇肆", "次元哥哥")]),
        ("at-subject-call-short", "@宇肆 叫次元", [("宇肆", "次元")]),
        # pattern 4: subject immediately followed by 次元(哥哥|哥|大哥)?
        ("subject-then-ciyuan", "宇肆次元", [("宇肆", "次元")]),
        ("subject-then-ciyuan-gege", "宇肆次元哥哥", [("宇肆", "次元哥哥")]),
        ("subject-then-ciyuan-ge", "宇肆次元哥", [("宇肆", "次元哥")]),
        ("subject-then-ciyuan-dage", "宇肆次元大哥", [("宇肆", "次元大哥")]),
        # several facts in one message, in pattern order
        ("two-people", "叫宇肆次元哥哥，叫小明次元哥", [("宇肆", "次元哥哥"), ("小明", "次元哥")]),
        ("two-aliases-one-person", "叫宇肆次元哥哥 叫宇肆次元", [("宇肆", "次元哥哥"), ("宇肆", "次元")]),
    ))
    def test_hard_coded_alias_patterns(self, text, expected):
        assert pairs(extract(text)) == expected

    @pytest.mark.parametrize("text", [
        pytest.param("今天天气不错", id="unrelated"),
        pytest.param("次元", id="bare-alias"),
        pytest.param("次元哥哥来了", id="alias-first"),
        pytest.param("叫次元哥哥", id="no-subject"),
        pytest.param("叫牛牛哥哥", id="niuniu-without-subject"),
        pytest.param("这个是次元", id="invalid-subject"),
        pytest.param("他叫什么", id="question"),
        pytest.param("叫他次元", id="pronoun-without-context"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="spaces"),
        pytest.param("wxid_abc123:", id="sender-prefix-only"),
    ])
    def test_texts_without_a_fact(self, text):
        assert extract(text) == []

    @pytest.mark.parametrize("message", [{}, {"content": None}, {"content": ""}],
                             ids=["no-content-key", "none-content", "empty-content"])
    def test_message_without_text_yields_nothing(self, message):
        assert GroupFactExtractor().extract(message, None) == []

    def test_fact_objects_are_frozen_alias_triples(self):
        (fact, *_) = extract("叫宇肆次元哥哥")

        assert fact == GroupFact("宇肆", "alias", "次元哥哥", DIRECT_CONFIDENCE)
        with pytest.raises(AttributeError):
            fact.subject = "别人"


# ── the "cy" shorthand ──────────────────────────────────────────────────

class TestDerivedAliases:
    # PINNED (known issue, expected to change in Phase 3): "cy" is hard-coded as
    # the pinyin initials of "次元" for this one group.
    def test_alias_starting_with_ciyuan_also_yields_cy(self):
        facts = extract("叫宇肆次元哥哥")

        assert triples(facts) == [("宇肆", "alias", "次元哥哥"), ("宇肆", "alias", "cy")]
        assert [f.confidence for f in facts] == [DIRECT_CONFIDENCE, DERIVED_CONFIDENCE]

    def test_other_aliases_have_no_derived_fact(self):
        assert triples(extract("叫宇肆牛牛哥哥")) == [("宇肆", "alias", "牛牛哥哥")]

    @pytest.mark.parametrize("alias, expected", [
        pytest.param("次元", ["cy"], id="ciyuan"),
        pytest.param("次元哥哥", ["cy"], id="ciyuan-gege"),
        pytest.param(" 次元 ", ["cy"], id="padded"),
        pytest.param("牛牛哥哥", [], id="niuniu"),
        pytest.param("小次元", [], id="contains-but-does-not-start-with"),
        pytest.param("cy", [], id="cy-itself"),
        pytest.param("", [], id="empty"),
        pytest.param(None, [], id="none"),
    ])
    def test_derived_aliases(self, alias, expected):
        assert GroupFactExtractor()._derived_aliases(alias) == expected

    def test_derived_confidence_is_capped_at_0_78_and_only_alias_facts_derive(self):
        facts = [
            GroupFact("宇肆", "alias", "次元", 0.9),
            GroupFact("小明", "alias", "次元", 0.5),
            GroupFact("小红", "likes", "次元", 0.9),
        ]

        out = GroupFactExtractor()._with_derived_aliases(facts)

        assert out[:3] == facts
        assert out[3:] == [
            GroupFact("宇肆", "alias", "cy", 0.78),
            GroupFact("小明", "alias", "cy", 0.5),
        ]


# ── rejection rules ─────────────────────────────────────────────────────

class TestValidAliasFact:
    valid = staticmethod(GroupFactExtractor._valid_alias_fact)

    @pytest.mark.parametrize("subject, alias, expected", [
        pytest.param("宇肆", "次元哥哥", True, id="normal"),
        pytest.param("", "次元", False, id="empty-subject"),
        pytest.param("宇肆", "", False, id="empty-alias"),
        pytest.param("次元", "次元", False, id="subject-equals-alias"),
        # rejected aliases
        pytest.param("宇肆", "什么", False, id="alias-shenme"),
        pytest.param("宇肆", "啥", False, id="alias-sha"),
        pytest.param("宇肆", "谁", False, id="alias-shei"),
        pytest.param("宇肆", "哪个", False, id="alias-which-one"),
        pytest.param("宇肆", "这个", False, id="alias-this-one"),
        pytest.param("宇肆", "那个", False, id="alias-that-one"),
        pytest.param("宇肆", "他", False, id="alias-he"),
        pytest.param("宇肆", "她", False, id="alias-she"),
        pytest.param("宇肆", "它", False, id="alias-it"),
        # rejected subjects (exact)
        pytest.param("看到", "次元", False, id="subject-kandao"),
        pytest.param("见到", "次元", False, id="subject-jiandao"),
        pytest.param("应该", "次元", False, id="subject-yinggai"),
        pytest.param("这个", "次元", False, id="subject-this-one"),
        pytest.param("那个", "次元", False, id="subject-that-one"),
        pytest.param("他", "次元", False, id="subject-he"),
        pytest.param("她", "次元", False, id="subject-she"),
        pytest.param("它", "次元", False, id="subject-it"),
        # rejected subjects (substring)
        pytest.param("叫宇肆", "次元", False, id="contains-jiao"),
        pytest.param("宇肆什么", "次元", False, id="contains-shenme"),
        pytest.param("宇肆啥", "次元", False, id="contains-sha"),
        pytest.param("谁", "次元", False, id="contains-shei"),
        pytest.param("记得宇肆", "次元", False, id="contains-jide"),
        pytest.param("知道宇肆", "次元", False, id="contains-zhidao"),
        pytest.param("都说宇肆", "次元", False, id="contains-doushuo"),
        pytest.param("这个是宇肆", "次元", False, id="contains-zhegeshi"),
        pytest.param("那个是宇肆", "次元", False, id="contains-nageshi"),
        pytest.param("群里宇肆", "次元", False, id="contains-qunli"),
        pytest.param("怎么宇肆", "次元", False, id="contains-zenme"),
        pytest.param("不是宇肆", "次元", False, id="contains-bushi"),
        pytest.param("就是宇肆", "次元", False, id="contains-jiushi"),
        # rejected subject endings
        pytest.param("宇肆要", "次元", False, id="ends-with-yao"),
        pytest.param("宇肆该", "次元", False, id="ends-with-gai"),
        pytest.param("宇肆应该", "次元", False, id="ends-with-yinggai"),
        pytest.param("宇肆为", "次元", False, id="ends-with-wei"),
        # length limits (1..20 each)
        pytest.param("a" * 20, "次元", True, id="subject-20-chars"),
        pytest.param("a" * 21, "次元", False, id="subject-21-chars"),
        pytest.param("宇肆", "b" * 20, True, id="alias-20-chars"),
        pytest.param("宇肆", "b" * 21, False, id="alias-21-chars"),
    ])
    def test_rules(self, subject, alias, expected):
        assert self.valid(subject, alias) is expected

    # PINNED (known issue, expected to change in Phase 3): first/second person
    # pronouns are not in the deny list, so "我" becomes a literal subject.
    @pytest.mark.parametrize("subject", ["我", "你", "我们"])
    def test_first_and_second_person_subjects_are_accepted(self, subject):
        assert self.valid(subject, "次元") is True


# ── pronoun aliases resolved from recent chat ───────────────────────────

class TestPronounAlias:
    # PINNED (known issue, expected to change in Phase 3): hard-coded group-specific alias patterns
    @pytest.mark.parametrize("text, alias", [
        pytest.param("叫他次元哥哥", "次元哥哥", id="call-him-alias"),
        pytest.param("叫她次元", "次元", id="call-her-alias"),
        pytest.param("叫它次元哥哥", "次元哥哥", id="call-it-alias"),
        pytest.param("他叫次元", "次元", id="he-is-called"),
        pytest.param("叫他次元哥哥吧", "次元哥哥", id="trailing-particle-stripped"),
        pytest.param("叫他小可爱", "小可爱", id="any-alias-not-only-ciyuan"),
    ])
    def test_pronoun_alias_takes_the_subject_from_the_last_call_question(self, text, alias):
        facts = extract(text, [earlier("看到宇肆该叫什么")])

        assert pairs(facts) == [("宇肆", alias)]
        assert facts[0].confidence == PRONOUN_CONFIDENCE

    @pytest.mark.parametrize("question", [
        pytest.param("看到宇肆该叫什么", id="see-should-call-what"),
        pytest.param("见到宇肆要叫啥", id="meet-must-call-sha"),
        pytest.param("宇肆应该叫什么", id="subject-should-call-what"),
        pytest.param("应该叫宇肆什么", id="should-call-subject-what"),
        pytest.param("宇肆叫什么", id="subject-calls-what"),
        pytest.param("wxid_abc123: 宇肆该叫啥", id="sender-prefix-is-stripped"),
    ])
    def test_recognised_call_questions(self, question):
        assert pairs(extract("叫他次元哥哥", [earlier(question)])) == [("宇肆", "次元哥哥")]

    def test_the_most_recent_question_wins(self):
        recent = [earlier("小明该叫什么", message_id="r1", timestamp=800),
                  earlier("宇肆该叫什么", message_id="r2", timestamp=900)]

        assert pairs(extract("叫他次元哥哥", recent)) == [("宇肆", "次元哥哥")]

    def test_the_current_message_itself_is_skipped_by_id(self):
        recent = [earlier("宇肆该叫什么", message_id="cur")]

        assert extract("叫他次元哥哥", recent) == []

    def test_messages_newer_than_the_current_one_are_skipped(self):
        assert extract("叫他次元哥哥", [earlier("宇肆该叫什么", timestamp=2000)]) == []

    def test_equal_timestamp_and_missing_current_timestamp_are_accepted(self):
        same = [earlier("宇肆该叫什么", timestamp=1000)]
        assert pairs(extract("叫他次元哥哥", same)) == [("宇肆", "次元哥哥")]

        newer = [earlier("宇肆该叫什么", timestamp=2000)]
        assert pairs(extract("叫他次元哥哥", newer, timestamp=0)) == [("宇肆", "次元哥哥")]

    @pytest.mark.parametrize("recent", [
        pytest.param(None, id="none"),
        pytest.param([], id="empty"),
        pytest.param([earlier("今天吃什么")], id="no-call-question"),
        pytest.param([earlier("")], id="blank"),
    ])
    def test_no_subject_means_no_fact(self, recent):
        assert extract("叫他次元哥哥", recent) == []

    @pytest.mark.parametrize("text", [
        "叫他啥", "叫他谁", "叫他哪个", "他是谁", "叫他什么呀", "叫他什么吗", "叫他啥呀", "叫他", "他叫",
    ])
    def test_question_words_are_not_aliases(self, text):
        assert extract(text, [earlier("宇肆该叫什么")]) == []

    # PINNED (known issue, expected to change in Phase 3): the question-word
    # deny list is checked AFTER ``_clean_alias`` has stripped a trailing "么",
    # so "什么" becomes "什", slips through, and a plain "what is he called?"
    # is stored as the alias "什".
    @pytest.mark.parametrize("text", ["叫他什么", "他叫什么", "她是什么", "他叫什么？"])
    def test_what_question_leaks_a_truncated_alias(self, text):
        facts = extract(text, [earlier("宇肆该叫什么")])

        assert triples(facts) == [("宇肆", "alias", "什")]
        assert facts[0].confidence == PRONOUN_CONFIDENCE

    def test_pronoun_fact_comes_after_the_direct_facts_and_gets_the_cy_shorthand(self):
        facts = extract("叫宇肆次元哥哥，他叫次元", [earlier("小明该叫什么")])

        assert triples(facts) == [
            ("宇肆", "alias", "次元哥哥"),
            ("小明", "alias", "次元"),
            ("宇肆", "alias", "cy"),
            ("小明", "alias", "cy"),
        ]
        assert [f.confidence for f in facts] == [
            DIRECT_CONFIDENCE, PRONOUN_CONFIDENCE, DERIVED_CONFIDENCE, DERIVED_CONFIDENCE,
        ]

    # PINNED (known issue, expected to change in Phase 3): the alias capture is
    # greedy up to the next punctuation, so trailing words become part of it.
    def test_alias_swallows_trailing_words(self):
        facts = extract("叫他次元哥哥谢谢", [earlier("宇肆该叫什么")])

        assert pairs(facts) == [("宇肆", "次元哥哥谢谢")]

    # PINNED (known issue, expected to change in Phase 3): the pronoun path
    # never runs ``_valid_alias_fact`` on the subject it digs out of history,
    # so a fragment such as "该" is stored as a person.
    def test_subject_found_in_history_is_not_validated(self):
        facts = extract("叫他次元哥哥", [earlier("@宇肆 该叫什么")])

        assert pairs(facts) == [("该", "次元哥哥")]


# ── known false positives of the direct patterns ────────────────────────

class TestKnownFalsePositives:
    # PINNED (known issue, expected to change in Phase 3): the last pattern
    # accepts ANY 1-16 characters in front of "次元", and nothing looks at
    # negation or at who is being talked about.
    @pytest.mark.parametrize("text, expected", _cases(
        ("greeting-becomes-subject", "你好次元", [("你好", "次元")]),
        ("first-person-subject", "叫我次元哥哥", [("我", "次元哥哥")]),
        ("copula-becomes-subject", "她是次元", [("她是", "次元")]),
        ("negation-is-ignored", "不要叫宇肆次元哥哥", [("宇肆", "次元哥哥")]),
        ("verb-prefix-yields-a-second-subject", "称呼宇肆次元大哥",
         [("宇肆", "次元大哥"), ("称呼宇肆", "次元大哥")]),
    ))
    def test_junk_facts(self, text, expected):
        assert pairs(extract(text)) == expected

    def test_junk_subject_and_pronoun_subject_can_coexist(self):
        facts = extract("他是次元哥哥呀", [earlier("宇肆该叫什么")])

        assert pairs(facts) == [("他是", "次元哥哥"), ("宇肆", "次元哥哥")]
        assert [f.confidence for f in facts if f.object != "cy"] == [DIRECT_CONFIDENCE, PRONOUN_CONFIDENCE]


# ── de-duplication ──────────────────────────────────────────────────────

class TestDedupe:
    def test_repeated_statement_is_stored_once(self):
        facts = extract("叫宇肆次元哥哥，叫宇肆次元哥哥")

        assert triples(facts) == [("宇肆", "alias", "次元哥哥"), ("宇肆", "alias", "cy")]

    def test_pronoun_duplicate_of_a_direct_fact_is_dropped(self):
        facts = extract("叫宇肆次元哥哥，他叫次元哥哥", [earlier("宇肆该叫什么")])

        assert triples(facts) == [("宇肆", "alias", "次元哥哥"), ("宇肆", "alias", "cy")]
        assert facts[0].confidence == DIRECT_CONFIDENCE      # first one wins

    def test_first_occurrence_wins_even_with_lower_confidence(self):
        facts = [
            GroupFact("宇肆", "alias", "次元", 0.5),
            GroupFact("宇肆", "alias", "次元", 0.9),
            GroupFact("宇肆", "alias", "次元哥哥", 0.9),
        ]

        assert GroupFactExtractor._dedupe(facts) == [facts[0], facts[2]]

    def test_key_is_subject_predicate_object(self):
        facts = [
            GroupFact("宇肆", "alias", "次元"),
            GroupFact("小明", "alias", "次元"),
            GroupFact("宇肆", "nickname", "次元"),
        ]

        assert GroupFactExtractor._dedupe(facts) == facts


# ── text normalisation (bot mention, wxid prefix) ───────────────────────

class TestTextCleaning:
    @pytest.mark.parametrize("raw, expected", _cases(
        ("wxid-prefix", "wxid_abc123: hello", "hello"),
        ("wxid-prefix-extra-space", "  wxid_abc123:   hello", "hello"),
        ("wxid-prefix-no-space", "wxid_abc123:hello", "hello"),
        ("wxid-prefix-newline", "wxid_abc123:\n叫宇肆次元哥哥", "叫宇肆次元哥哥"),
        ("only-the-first-prefix", "wxid_abc123:hi wxid_def:yo", "hi wxid_def:yo"),
        ("prefix-is-case-sensitive", "WXID_abc: hi", "WXID_abc: hi"),
        ("fullwidth-colon-is-not-a-prefix", "wxid_abc：hi", "wxid_abc：hi"),
        ("prefix-must-be-at-the-start", "hi wxid_abc: yo", "hi wxid_abc: yo"),
        ("newlines-and-runs-collapse", "a\nb\r\nc    d", "a b c d"),
        ("none-is-empty", None, ""),
        ("number-is-stringified", 123, "123"),
    ))
    def test_without_bot_name(self, raw, expected):
        assert GroupFactExtractor()._clean_text(raw) == expected

    @pytest.mark.parametrize("raw, expected", _cases(
        ("mention-and-space", "@林小满 你好", "你好"),
        ("mention-and-ascii-comma", "@林小满,你好", "你好"),
        ("mention-and-fullwidth-comma", "@林小满，你好", "你好"),
        ("mention-glued-to-text", "@林小满你好", "你好"),
        ("mention-and-wechat-four-per-em-space", "@林小满 你好", "你好"),
        ("mention-only", "@林小满", ""),
        ("other-mentions-are-kept", "@宇肆 你好", "@宇肆 你好"),
        ("name-without-at-is-kept", "林小满你好", "林小满你好"),
    ))
    def test_with_bot_name(self, raw, expected):
        assert GroupFactExtractor("林小满")._clean_text(raw) == expected

    # PINNED (known issue, expected to change in Phase 3): the stripping regex
    # ``@name[^\w]*`` also eats the "@" of the NEXT mention and has no word
    # boundary on the left.
    def test_mention_stripping_eats_the_next_at_sign(self):
        cleaner = GroupFactExtractor("林小满")

        assert cleaner._clean_text("@林小满 @宇肆 叫次元哥哥") == "宇肆 叫次元哥哥"
        assert cleaner._clean_text("x@林小满 y") == "xy"
        assert extract("@林小满 @宇肆 叫次元哥哥", bot_name="林小满") == []

    def test_without_stripping_the_bot_would_be_given_the_alias(self):
        assert pairs(extract("@林小满 叫次元哥哥")) == [("林小满", "次元哥哥")]
        assert extract("@林小满 叫次元哥哥", bot_name="林小满") == []

    def test_other_people_are_still_found_after_the_bot_mention_is_removed(self):
        facts = extract("@林小满 叫宇肆次元哥哥", bot_name="林小满")

        assert pairs(facts) == [("宇肆", "次元哥哥")]

    def test_wxid_prefix_is_removed_before_matching(self):
        facts = extract("wxid_abc123: 叫宇肆次元哥哥")

        assert pairs(facts) == [("宇肆", "次元哥哥")]

    def test_bot_mention_regex_metacharacters_are_escaped(self):
        cleaner = GroupFactExtractor("bot.(v2)")

        assert cleaner._clean_text("@bot.(v2) 你好") == "你好"
        assert cleaner._clean_text("@botX(v2) 你好") == "@botX(v2) 你好"
