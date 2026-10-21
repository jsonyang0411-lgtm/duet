"""Text rules of the structured pipeline (src/pipeline/text.py)."""

import json
from pathlib import Path

import pytest

from src.pipeline import text as T
from src.summarize.base import AbstractSummarizer

RAW_OUTPUTS = Path(__file__).resolve().parents[1] / "evals" / "cases" / "raw_outputs_v1.jsonl"


def _fixture_rows():
    rows = []
    for line in RAW_OUTPUTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# ── the legacy fixture, judged by the v2 rules ──────────────────────────

@pytest.mark.parametrize("row", _fixture_rows(), ids=lambda row: row["id"])
def test_every_legacy_fixture_row_comes_out_exactly_as_expected(row):
    # The same 64 raw model outputs the legacy cleaner is measured on, but with
    # the text sitting in an envelope's ``messages``: silence markers and
    # explanations must vanish, normal sentences ("不用回复我哈 你先忙") survive.
    out, _ = T.finalize_message(
        row["raw"], max_chars=int(row.get("max_chars") or 30),
        names=tuple(row.get("names") or ()),
    )
    expected = "" if row["expect_silent"] else row["expect_text"]
    assert out == expected


# ── silence markers: whole message only ─────────────────────────────────

@pytest.mark.parametrize("text", [
    "沉默", "沉默。", "（沉默）", "(沉默，不需要回复)", "[无回复]", "【不回复】",
    "空白", "回复空白", "NO_REPLY", "no reply", "Silent", "不需要回复", "无需回应",
    "应该沉默", "保持安静就好", "（低下头）", "（这条消息不是在对我说，保持沉默）",
])
def test_whole_message_silence_markers_are_recognised(text):
    assert T.is_silence_marker(text)


@pytest.mark.parametrize("text", [
    "", "好呀", "我不傻的", "不用回复我哈 你先忙", "这封邮件不需要回复",
    "他一直沉默 我感觉不太对劲", "这段空白期我们补一下", "无需回应 他只是随口说说",
    "pass", "跳过", "安静", "略", "我周五有空（下午三点后）",
])
def test_ordinary_messages_are_not_silence_markers(text):
    assert not T.is_silence_marker(text)


@pytest.mark.parametrize("text", [
    "这句话不是对我说的，所以我选择不说话。",
    "看起来是他们在聊天，我就不打扰了",
    "这条消息和群友的对话有关，我决定保持沉默",
])
def test_explanations_of_the_silence_choice_are_recognised(text):
    assert T.is_silence_explanation(text)


@pytest.mark.parametrize("text", [
    "那我就不打扰你们了",                 # a goodbye to the person addressed
    "他应该是沉默了吧",
    "不是在对我说话也没关系 我回复你一下",
    "他没有明确指向我的意思",
    "我选择周六",
    "他们在聊什么",
])
def test_sentences_that_only_resemble_explanations_survive(text):
    assert not T.is_silence_explanation(text)


# ── finalize_message ────────────────────────────────────────────────────

def test_finalize_reports_every_rule_that_fired():
    text, warnings = T.finalize_message(
        "林小满：（点头）好的】【", max_chars=30, names=("林小满",),
    )
    assert text == "好的"
    assert set(warnings) == {"prefix_removed", "dangling_removed", "aside_removed"}


def test_finalize_collapses_newlines_into_one_line():
    assert T.finalize_message("好的\n那就这样", max_chars=30)[0] == "好的 那就这样"


def test_finalize_blank_and_punctuation_only_messages_are_not_sent():
    assert T.finalize_message("   ", max_chars=30) == ("", [])
    for noise in ("…", "。。。", "~", "......"):
        assert T.finalize_message(noise, max_chars=30) == ("", ["punctuation_only"])


def test_finalize_marker_names_the_rule():
    assert T.finalize_message("（沉默）", max_chars=30) == ("", ["silence_marker"])
    assert T.finalize_message("NO_REPLY", max_chars=30) == ("", ["silence_marker"])
    text, warnings = T.finalize_message(
        "这句话不是对我说的，所以我选择不说话。", max_chars=30)
    assert (text, warnings) == ("", ["silence_explanation"])


def test_finalize_cuts_at_a_clause_boundary_and_says_so():
    text, warnings = T.finalize_message(
        "我觉得周末去爬山挺好的，不过要看天气，如果下雨的话我们可以改成去看电影", max_chars=20,
    )
    assert text == "我觉得周末去爬山挺好的，不过要看天气"
    assert warnings == ["truncated"]


def test_asides_keep_content_in_parentheses():
    assert T.strip_asides("我周五有空（下午三点后）") == "我周五有空（下午三点后）"
    assert T.strip_asides("版本是（v2.3）") == "版本是（v2.3）"
    assert T.strip_asides("好的(点头)") == "好的"
    assert T.strip_asides("（笑）好呀") == "好呀"
    assert T.strip_asides("中间（笑）还有话") == "中间（笑）还有话"      # only the edges


# ── helpers shared with the legacy cleaner ───────────────────────────────

def test_the_legacy_cleaner_uses_the_same_helpers():
    assert AbstractSummarizer._truncate_reply("哈" * 50, 10) == T.truncate_at_boundary("哈" * 50, 10)
    assert AbstractSummarizer._strip_speaker_prefix("小助手: 嗯", ["小助手"]) == "嗯"
    assert AbstractSummarizer._OUTPUT_LABELS is T.OUTPUT_LABELS


def test_speaker_prefix_only_removes_known_names():
    assert T.strip_speaker_prefix("晚上7:30见", ()) == "晚上7:30见"
    assert T.strip_speaker_prefix("地址：科技路88号", ()) == "地址：科技路88号"
    assert T.strip_speaker_prefix("回复：好的", ()) == "好的"
    assert T.strip_speaker_prefix("阿明：好的", ("阿明",)) == "好的"


# ── leak variants found in review ───────────────────────────────────────

@pytest.mark.parametrize("text", [
    '{"action":"silent","messages":[]}', '{"addressee":"me","action":"reply"}',
    "action: silent", "action：reply", "reason: 他们在聊天", "messages = []",
])
def test_the_answer_format_written_out_as_text_is_not_sent(text):
    assert T.is_protocol_echo(text)
    assert T.finalize_message(text, max_chars=30)[0] == ""


@pytest.mark.parametrize("text", [
    "silent，他们在互相聊", "沉默…", "沉默，因为他们在聊", "No_Reply。", "不回复：不是对我说的",
    "ＮＯ＿ＲＥＰＬＹ", "ｓｉｌｅｎｔ", "沉\u200b默", "N\u200bO_REPLY",
])
def test_spoofed_and_decorated_silence_markers_are_not_sent(text):
    assert T.finalize_message(text, max_chars=30)[0] == ""


@pytest.mark.parametrize("text", [
    "这不是对我说的，不回复", "这条消息和我无关，不回复", "跟我无关 所以不回应",
])
def test_not_for_me_plus_not_replying_is_an_explanation(text):
    assert T.is_silence_explanation(text)


@pytest.mark.parametrize("text", [
    "沉默是金", "silent night 好听", "reasonable 的", "actions speak louder", "他没有明确指向我的意思",
    "不是在对我说话也没关系 我回复你一下", "不用回复我哈 你先忙", "与我无关的事别问我",
])
def test_sentences_that_begin_like_a_marker_or_mention_one_survive(text):
    assert T.finalize_message(text, max_chars=30)[0] == text


def test_zero_width_characters_are_removed_from_what_is_sent():
    assert T.finalize_message("好\u200b呀", max_chars=30)[0] == "好呀"
