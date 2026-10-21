"""No text rule may take long on hostile input.

Python's regex engine holds the GIL, so a pattern that backtracks exponentially
freezes the whole bot for as long as it runs.  Group chat is full of long runs
("哈哈哈…", "。。。", "@@@@") followed by ordinary text, and anyone in the group
can type them.  Every rule that reads message text must stay fast on them.
"""

import json
import time

import pytest

from src.pipeline import text as T
from src.pipeline.addressing import (
    calls_by_name,
    classify,
    leading_mentions,
    mentions_in,
    mentions_other,
)
from src.pipeline.envelope import EnvelopeError, parse_envelope
from src.proactive import willingness as W

N = 4000
LIMIT_SEC = 0.5

HOSTILE = [
    "哈" * N + "你太搞笑了",
    "哈" * N + "x",
    "笑死" * (N // 2) + "x",
    "6" * N + "x",
    "2" + "3" * N + "x",
    "w" * N + "!",
    "草" * N + "。",
    "。" * N + "x",
    "，" * N + "x",
    " " * N + "x",
    "\n" * N + "x",
    "(" * N,
    "（" * (N // 2) + "x" * 10,
    "（x" * (N // 3),
    "（" + "a" * N + "）",
    "【" * N + "x",
    "@" * N,
    "@a " * (N // 3),
    "@a " * (N // 3) + "x",
    "wxid_" * (N // 5),
    "wxid_abc:" * (N // 9),
    "知道" * (N // 2) + "x",
    "谁" + "会" * 16000,
    "谁" + "知道" * 8000,
    "不" * N + "回复",
    "我" * N + "选择不说话",
    "：" * N,
    "梁" * N,
    '{"action":"reply","messages":["' + "哈" * N + '"]}',
    '{"a":' * 600 + "1" + "}" * 600,
    "[" * 2000 + "]" * 2000,
    '{"action":"reply","messages":[' + '"x",' * 900 + '"y"]}',
]


def timed(function, *args, **kwargs):
    started = time.perf_counter()
    try:
        function(*args, **kwargs)
    except EnvelopeError:
        pass
    return time.perf_counter() - started


@pytest.mark.parametrize("index", range(len(HOSTILE)))
def test_text_rules_stay_fast_on_hostile_input(index):
    text = HOSTILE[index]
    slow = []
    for name, function, args in [
        ("compact", W.compact, (text,)),
        ("looks_like_question", W.looks_like_question, (text,)),
        ("looks_info_seeking", W.looks_info_seeking, (text,)),
        ("is_media_only", W.is_media_only, (text,)),
        ("is_too_short", W.is_too_short, (text,)),
        ("is_laughter", W.is_laughter, (W.compact(text),)),
        ("finalize_message", T.finalize_message, (text,)),
        ("is_silence_marker", T.is_silence_marker, (text,)),
        ("is_silence_explanation", T.is_silence_explanation, (text,)),
        ("strip_asides", T.strip_asides, (text,)),
        ("strip_speaker_prefix", T.strip_speaker_prefix, (text, ("林小满",))),
        ("truncate_at_boundary", T.truncate_at_boundary, (text, 30)),
        ("sanitize_context_content", T.sanitize_context_content, (text,)),
        ("mentions_in", mentions_in, (text,)),
        ("leading_mentions", leading_mentions, (text,)),
        ("mentions_other", mentions_other, (text, ("小助手",))),
        ("calls_by_name", calls_by_name, (text, ("小助手", "林小满"))),
        ("classify", classify, ({"content": text, "is_at_mentioned": True}, )),
        ("parse_envelope", parse_envelope, (text,)),
    ]:
        kwargs = {"max_chars": 30} if function is parse_envelope else {}
        if function is T.finalize_message:
            kwargs = {"max_chars": 30}
        elapsed = timed(function, *args, **kwargs)
        if elapsed > LIMIT_SEC:
            slow.append(f"{name} {elapsed:.2f}s")
    assert not slow, (text[:20], slow)


def test_the_gate_stays_fast_on_hostile_messages():
    from src.config import BotConfig
    from src.proactive.gate import ProactiveGate

    gate = ProactiveGate(BotConfig(proactive_enabled=True, proactive_gate_mode="score"))
    started = time.perf_counter()
    for index, text in enumerate(HOSTILE):
        gate.should_speak({"chat_id": "g@chatroom", "sender_id": f"u{index % 3}",
                           "content": text, "msg_type": 1})
    assert time.perf_counter() - started < 3.0


def test_the_laughter_rule_still_means_laughter():
    for text in ("哈哈哈", "笑死我了", "哈哈哈哈哈哈 笑死我了", "xswl", "233333", "666", "草", "wwww", "nb"):
        assert W.is_too_short(text), text
    for text in ("哈哈哈哈这个太好笑了", "今天去哪", "666号房间在哪", "笑死我了你怎么还不睡"):
        assert not W.is_too_short(text), text
