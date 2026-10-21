"""The reply envelope: parsing, validation and the "never leaks" property."""

import json
import random

import pytest

from src.pipeline.envelope import (
    ACTION_REPLY,
    ACTION_SILENT,
    MAX_RAW_CHARS,
    Envelope,
    EnvelopeError,
    extract_envelope_dict,
    iter_json_objects,
    parse_envelope,
)


def parse(raw, **kwargs):
    kwargs.setdefault("max_chars", 30)
    return parse_envelope(raw, **kwargs)


def reply_json(*messages, **extra):
    body = {"addressee": "me", "action": "reply", "messages": list(messages), "reason": "测试"}
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


# ── well-formed answers ──────────────────────────────────────────────────

def test_a_plain_reply():
    envelope = parse(reply_json("好呀"))
    assert envelope == Envelope(ACTION_REPLY, "me", ("好呀",), "测试", ())
    assert envelope.is_reply and envelope.text == "好呀"


@pytest.mark.parametrize("messages, sent", [
    (["我大一啊，刚满十八", "reason被@确认年龄"], ("我大一啊，刚满十八",)),     # seen from qwen3.5:4b
    (["因为他知道你已经饿不动了。 reason"], ("因为他知道你已经饿不动了。",)),  # seen from the cloud model
    (["好呀", '"addressee": "me"'], ("好呀",)),
    (["好呀", "Action: reply"], ("好呀",)),
])
def test_a_field_name_written_into_the_messages_is_not_sent(messages, sent):
    envelope = parse(reply_json(*messages))
    assert envelope.messages == sent and "field_name_removed" in envelope.warnings


@pytest.mark.parametrize("text", ["这部 action 片不错", "reasonable 这个词怎么读", "stickers 好可爱"])
def test_ordinary_english_words_stay(text):
    envelope = parse(reply_json(text))
    assert envelope.messages == (text,) and "field_name_removed" not in envelope.warnings


def test_a_reply_that_is_only_a_field_name_stays_a_silence_marker():
    envelope = parse(reply_json("reason: 他们在聊天"))                    # as env-106 in the replay
    assert envelope.action == ACTION_SILENT and "field_name_removed" not in envelope.warnings


def test_a_silent_envelope():
    envelope = parse('{"addressee":"others","action":"silent","messages":[],"reason":"他们在互相聊"}')
    assert envelope.action == ACTION_SILENT
    assert envelope.addressee == "others"
    assert envelope.messages == ()
    assert not envelope.is_reply


def test_code_fences_and_surrounding_prose_are_tolerated():
    wrapped = "好的，答案如下：\n```json\n" + reply_json("稻香吧") + "\n```\n希望有帮助"
    assert parse(wrapped).messages == ("稻香吧",)


def test_braces_inside_strings_do_not_confuse_the_extractor():
    envelope = parse('{"action":"reply","messages":["这是{括号}和\\"引号\\""],"addressee":"me"}')
    assert envelope.messages == ('这是{括号}和"引号"',)


def test_python_style_dict_and_trailing_comma_are_accepted():
    assert parse("{'action': 'reply', 'messages': ['好的'], 'addressee': 'me',}").messages == ("好的",)


def test_answer_nested_under_a_wrapper_key():
    assert parse('{"result": {"action": "reply", "messages": ["哈哈"]}}').messages == ("哈哈",)


def test_doubly_encoded_answer():
    inner = reply_json("在的")
    assert parse(json.dumps(inner, ensure_ascii=False)).messages == ("在的",)


def test_first_object_with_an_action_wins():
    raw = '示例 {"foo": 1} 实际 ' + reply_json("好")
    assert parse(raw).messages == ("好",)


@pytest.mark.parametrize("alias,expected", [
    ("speak", ACTION_REPLY), ("Reply", ACTION_REPLY), ("SAY", ACTION_REPLY),
    ("silence", ACTION_SILENT), ("NO_REPLY", ACTION_SILENT), ("skip", ACTION_SILENT),
    ("沉默", ACTION_SILENT),
])
def test_action_aliases(alias, expected):
    raw = json.dumps({"action": alias, "messages": ["好"] if expected == ACTION_REPLY else []})
    assert parse(raw).action == expected


@pytest.mark.parametrize("key", ["reply", "message", "text", "content"])
def test_message_key_aliases_and_a_bare_string(key):
    assert parse(json.dumps({"action": "reply", key: "好的"})).messages == ("好的",)


@pytest.mark.parametrize("value,expected", [
    ("me", "me"), ("bot", "me"), ("others", "others"), ("别人", "others"),
    ("group", "group"), ("大家", "group"), ("???", "unknown"), (None, "unknown"),
])
def test_addressee_aliases_and_unknown(value, expected):
    envelope = parse(json.dumps({"action": "silent", "addressee": value}))
    assert envelope.addressee == expected


def test_missing_addressee_is_a_warning_not_an_error():
    envelope = parse('{"action":"reply","messages":["好"]}')
    assert envelope.addressee == "unknown"
    assert "addressee_missing" in envelope.warnings


def test_reason_is_trimmed_and_may_be_any_type():
    assert len(parse(reply_json("好", reason="很长" * 100)).reason) == 60
    assert parse(reply_json("好", reason=None)).reason == ""
    assert parse(reply_json("好", reason=42)).reason == "42"


# ── silence is carried by action only ────────────────────────────────────

def test_text_on_a_silent_envelope_is_never_sent():
    envelope = parse('{"action":"silent","messages":["沉默，因为他们在互相聊"],"addressee":"others"}')
    assert envelope.messages == ()
    assert "text_ignored_on_silent" in envelope.warnings


@pytest.mark.parametrize("leak", [
    "沉默", "（沉默，不需要回复）", "NO_REPLY", "无回复", "[无回复]", "…",
    "这句话不是对我说的，所以我选择不说话。", "看起来是他们在聊天，我就不打扰了",
])
def test_a_reply_that_is_a_silence_marker_becomes_silence(leak):
    envelope = parse(reply_json(leak))
    assert envelope.action == ACTION_SILENT
    assert envelope.messages == ()
    assert "reply_text_rejected" in envelope.warnings


@pytest.mark.parametrize("sentence", [
    "不用回复我哈 你先忙", "他一直沉默 我感觉不太对劲", "这封邮件不需要回复",
])
def test_normal_sentences_that_mention_silence_are_sent(sentence):
    assert parse(reply_json(sentence)).messages == (sentence,)


def test_a_speaker_label_is_removed_with_known_names():
    assert parse(reply_json("林小满：好呀"), names=("林小满",)).messages == ("好呀",)
    assert parse(reply_json("7:30见")).messages == ("7:30见",)


# ── length handling ─────────────────────────────────────────────────────

def test_a_long_message_is_cut_at_a_clause_boundary():
    long_text = "我觉得周末去爬山挺好的，不过要看天气，如果下雨的话我们可以改成去看电影"
    envelope = parse(reply_json(long_text), max_chars=20)
    assert envelope.messages == ("我觉得周末去爬山挺好的，不过要看天气",)
    assert "truncated" in envelope.warnings


def test_two_messages_share_one_budget():
    envelope = parse(reply_json("第一句", "第二句"), max_chars=30)
    assert envelope.messages == ("第一句", "第二句")
    assert envelope.text == "第一句 第二句"


def test_a_second_message_that_does_not_fit_is_dropped_whole():
    envelope = parse(reply_json("第一句话在这里", "第二句话也很长很长很长很长很长"), max_chars=20)
    assert envelope.messages == ("第一句话在这里",)
    assert "message_dropped" in envelope.warnings


def test_more_than_two_messages_are_cut():
    envelope = parse(reply_json("甲", "乙", "丙"), max_chars=30)
    assert envelope.messages == ("甲", "乙")
    assert "too_many_messages" in envelope.warnings


# ── what is rejected ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,code", [
    ("", "empty"), ("   \n", "empty"), (None, "empty"),
    ("好的，我知道了", "no_json"),
    ("[1, 2, 3]", "no_json"),
    ('{"action":"reply","messages":["好', "no_json"),            # truncated
    ('{"action": }', "bad_json"),
    ('{"messages":["好"]}', "missing_action"),
    ('{"action":"maybe","messages":["好"]}', "bad_action"),
    ('{"action":"reply","messages":123}', "bad_messages"),
    ('{"action":"reply","messages":[{"text":"好"}]}', "bad_messages"),
    ('{"action":"reply","messages":[]}', "reply_without_text"),
    ('{"action":"reply","messages":["", "  "]}', "reply_without_text"),
    ('{"action":"reply"}', "reply_without_text"),
    ("{" * 10, "no_json"),
])
def test_unusable_answers_raise_a_countable_error(raw, code):
    with pytest.raises(EnvelopeError) as excinfo:
        parse(raw)
    assert excinfo.value.code == code


def test_a_rambling_answer_is_rejected_before_parsing():
    with pytest.raises(EnvelopeError) as excinfo:
        parse("好" * (MAX_RAW_CHARS + 1))
    assert excinfo.value.code == "too_long"


def test_pathologically_nested_output_is_rejected_not_crashed_on():
    deep = '{"action":"reply","messages":' + "[" * 2000 + "]" * 2000 + "}"
    with pytest.raises(EnvelopeError):
        parse(deep)
    sets = "{'action': 'reply', 'messages': {[1]}}"
    with pytest.raises(EnvelopeError):
        parse(sets)


def test_the_extractor_helpers():
    assert list(iter_json_objects('a {"x": 1} b {"y": {"z": 2}}')) == ['{"x": 1}', '{"y": {"z": 2}}']
    assert extract_envelope_dict('x {"action":"silent"} y') == {"action": "silent"}


# ── the property that matters: text leaves only through a reply envelope ──

def _random_garbage(rng):
    pieces = [
        '{"action":"reply"', '"messages":', '["好"]', '[', ']', '{', '}', ',', ':',
        '"action":"silent"', '"沉默"', "NO_REPLY", "```json", "```", "（沉默）", "好呀", "\n",
        '"addressee":"me"', "null", "true", "123", "'", '"', "\\",
    ]
    return "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))


def test_fuzzed_outputs_never_crash_and_never_leak_text_from_a_silent_envelope():
    rng = random.Random(20261016)
    for _ in range(3000):
        raw = _random_garbage(rng)
        try:
            envelope = parse(raw)
        except EnvelopeError:
            continue
        if envelope.action == ACTION_SILENT:
            assert envelope.messages == ()
        else:
            assert envelope.messages and all(m.strip() for m in envelope.messages)
            assert all("\n" not in m for m in envelope.messages)


def test_silent_envelopes_with_arbitrary_text_never_produce_messages():
    rng = random.Random(7)
    for _ in range(300):
        text = _random_garbage(rng)
        raw = json.dumps({"action": "silent", "messages": [text], "reason": text})
        assert parse(raw).messages == ()
