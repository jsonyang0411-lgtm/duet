"""Characterization tests for chat prompt assembly (Phase 0 safety net).

Pins what ``AbstractSummarizer.chat()`` / ``proactive_chat()`` put into the
system and user prompts today, how the reply is post-processed, how the
"intentional silence" flag and the retry loop behave, and how ``summarize()``
picks a strategy.  The prompt frames below are deliberate literal COPIES of the
production templates: a wording change in ``src/summarize/base.py`` must show up
here as an edit, never silently.

Convention: ``# PINNED (known issue, expected to change in Phase N): <why>``
marks behaviour that is documented here but NOT endorsed.

The summarize()/format_summary_for_reply() tests live in this file because the
Phase 0 layout has no dedicated test module for them; they only need the same
fake ``AbstractSummarizer`` subclass.

The fake subclass follows ``tests/test_persona_chat.py``: a concrete
``AbstractSummarizer`` whose ``_call_chat_api`` captures
``(system_prompt, messages)``.
"""

import datetime
import re
import time

import pytest

from src.persona import (
    DEFAULT_CHAT_CONTEXT_COUNT,
    DEFAULT_CHAT_CONTEXT_WINDOW_SEC,
    DEFAULT_CHAT_MAX_CHARS,
    DEFAULT_CHAT_PERSONA_NAME,
    DEFAULT_CHAT_PERSONA_PROMPT,
    DEFAULT_CHAT_REPLY_RULES,
    DEFAULT_YANG_YUJING_CONTEXT_COUNT,
    DEFAULT_YANG_YUJING_GROUP_NAME,
    DEFAULT_YANG_YUJING_PERSONA_NAME,
    DEFAULT_YANG_YUJING_PERSONA_PROMPT,
    DEFAULT_YANG_YUJING_REPLY_RULES,
    PERSONA_PROFILES,
    normalize_persona_profile,
    persona_defaults_for_profile,
)
from src.proactive.modes import ProactiveMode
from src.summarize import base as base_module
from src.summarize.base import AbstractSummarizer, _sanitize_context_content
from src.summarize.models import ParticipantContribution, SummaryResult

NO_HISTORY = "（没有可用的最近聊天记录）"
GROUP_MEMORY_PLACEHOLDER = "（你刚进这个群，还没有形成对这个群的印象）"
PRIVATE_MEMORY_PLACEHOLDER = "（这段私聊还没有形成长期记忆）"


# ── fakes and fixtures ─────────────────────────────────────────────────

class TransientError(Exception):
    """Stands in for a backend's retryable API error."""


class FakeSummarizer(AbstractSummarizer):
    """Concrete AbstractSummarizer that records every call it receives.

    ``replies`` are consumed one per chat API call and the LAST one repeats;
    an exception instance in the list is raised instead of returned.
    """

    retry_exceptions = (TransientError,)

    def __init__(self, *replies):
        self.replies = list(replies) or ["好呀"]
        self.calls = []          # [(system_prompt, messages), ...]
        self.summary_log = []    # strategy calls made by summarize()

    # chat API ---------------------------------------------------------
    def _call_chat_api(self, system_prompt: str, messages: list[dict]) -> str:
        self.calls.append((system_prompt, messages))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def system_prompt(self) -> str:
        return self.calls[-1][0]

    @property
    def user_prompt(self) -> str:
        messages = self.calls[-1][1]
        assert len(messages) == 1 and messages[0]["role"] == "user"
        return messages[0]["content"]

    # summarisation strategy hooks ---------------------------------------
    def _summarize_direct(self, messages: list[dict], requester_name: str) -> SummaryResult:
        self.summary_log.append(("direct", len(messages), requester_name))
        return SummaryResult(summary_text=f"direct:{len(messages)}", topics=[], participants=[])

    def _summarize_chunk(self, chunk: list[dict], chunk_num: int, total: int,
                         requester_name: str) -> str:
        self.summary_log.append(("chunk", len(chunk), chunk_num, total, requester_name))
        return f"chunk{chunk_num}/{total}"

    def _merge_chunk_summaries(self, chunk_summaries: list[str],
                               requester_name: str) -> SummaryResult:
        self.summary_log.append(("merge", tuple(chunk_summaries), requester_name))
        return SummaryResult(
            summary_text="merged(" + "|".join(chunk_summaries) + ")",
            topics=["t"],
            participants=[],
        )

    def consolidate_memory(self, existing_memory: str, new_messages: list[dict]) -> str:
        return existing_memory


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Never really sleep; also isolate the CHAT_API_MAX_ATTEMPTS env var.

    Returns the list of requested sleep durations.
    """
    monkeypatch.delenv("CHAT_API_MAX_ATTEMPTS", raising=False)
    recorded = []
    monkeypatch.setattr(base_module.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def frozen_clock(monkeypatch):
    """Make ``datetime.datetime.now()`` return 2026-01-02 03:04:05."""
    monkeypatch.setattr(datetime, "datetime", _FrozenDateTime)


STANDARD_PERSONA = dict(
    persona_name="测试人设",
    persona_prompt="PERSONA_PROMPT",
    reply_rules="REPLY_RULES",
    max_chars=20,
    context_count=40,
)

CONTEXT = [
    {"sender_name": "张三", "content": "今天吃什么"},
    {"sender_name": "李四", "content": "随便"},
]

CHAT_KWARGS = dict(
    message="今天吃什么",
    context_messages=CONTEXT,
    requester_name="张三",
    bot_name="机器人",
    group_name="测试群",
    group_memory="MEMORY",
)


def make_summarizer(*replies, **persona):
    summarizer = FakeSummarizer(*replies)
    summarizer.configure_chat_persona(**{**STANDARD_PERSONA, **persona})
    return summarizer


def section(prompt: str, title: str) -> str:
    """Body of the ``## title`` section of a system prompt."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=\n## |\Z)", prompt, re.S | re.M)
    assert match, f"no section {title!r}"
    return match.group(1)


# ── golden prompt frames (literal copies of the production templates) ────

CHAT_FRAME = """\
你是{persona_name}，{chat_scene}。你不是客服，不是问答机，也不要用 AI 助手口吻。

## 可配置人设
{persona_prompt}

## 回复规则
{reply_rules}

## 场景理解铁律
{scene_rules}

## 输出硬限制
只输出要发到微信里的那句话。最多 {max_chars} 个字。禁止分段、列表、Markdown、解释规则。
绝对禁止输出任何颜文字、emoji 或符号表情，例如 ^_^、(￣▽￣)、:)、😂。
绝对禁止输出括号里的心理活动、动作描写、旁白或判断过程，例如（沉默）（低下头）（不知道该不该接话）。你不是在写小说。
如果不该回复，只输出空白。

## 长期记忆
{group_memory}
如果长期记忆里有“固定事实/称呼记忆”，回答“谁是谁”“应该叫什么”“你记不记得”这类问题时必须优先使用，不要说不认识或不知道。
如果问题里的缩写或外号没有出现在固定事实/称呼记忆里，不要凭感觉猜成当前说话的人；应该说不确定或轻轻反问。

## 联网搜索
如果最新消息里包含“联网搜索结果”，这些是刚搜索到的外部资料。优先依据它们回答，不要编造结果里没有的实时信息；结果不足就说没查到。可以保留人设语气，但不要为了撒娇牺牲事实准确性。

## 当前
聊天：{group_name}  时间：{current_time}
你在这里的名字：{bot_name}
最新消息发送人：{sender_name}

最近聊天记录（按时间顺序，最后一条通常就是最新消息）：
{recent_messages}

最新消息：
{current_message}

{final_instruction}"""

PROACTIVE_FRAME = """\
你是{persona_name}，微信群「{group_name}」里的普通群友。你正在旁听群聊，只在自然适合时插一句。

## 可配置人设
{persona_prompt}

## 回复规则
{reply_rules}

## 插话判断
- 必须先看懂大家当前在聊什么，再决定要不要说。
- 只接当前话题，不要突然换话题，不要把群友之间的话误当成对你的指令。
- 如果最新几句是在评价 AI/机器人/某个“她”的表现，除非明确点你，否则尽量沉默。
- 如果只是“？”“哈哈”“笑死”“嗯”“不要”这类短反应，不确定主语就沉默。
- 群聊不需要你每次都在场；沉默是正常选择。

## 沉默规则
重大打击、心理危机、激烈冲突、隐私泄露、违法敏感内容、群公告刷屏、排队复读 → 沉默。

## 当前群聊状态
氛围模式：{mode_label}（{mode_description}）
你应该：{mode_instruction}

## 输出硬限制
最多 {max_chars} 个字。禁止分段、列表、Markdown、AI助手语气。
绝对禁止输出括号里的心理活动、动作描写、旁白或判断过程，例如（沉默）（低下头）（不知道该不该接话）。
如果当前不适合插话，只回复空白。

## 禁止用词
根据上下文、综上所述、首先其次最后、需要注意的是、值得一提的是、可谓是、不得不说、从某种角度来说、建议您、希望对你有所帮助

## 你在这个群里的长期记忆
{group_memory}

群：{group_name}  时间：{current_time}
你在群里的名字：{bot_name}

最近群聊：
{recent_messages}

只输出你要发的那句话。如果不适合说话，只回复空白。"""

GROUP_DIRECT = dict(
    chat_scene="微信群「测试群」里的普通群友",
    scene_rules="""\
你看到的是群聊截片，不是单人私聊。先判断最新消息是不是在对你说。
- 明确 @你、叫你的名字、回复你的上一句、向你提问/下指令 → 可以回复。
- 群友在互相说话、评价“她/它/AI/机器人”、吐槽你的表现给别人听 → 多半不是在和你说，回复空白。
- 最新消息只有“？”“哈哈”“笑死”“不要”“嗯”等短反应时，必须结合上一两句判断主语；不确定就沉默，或只轻轻反问一句。
- 不要把别人名字和自己绑定，不要乱叫哥哥/大王，除非上下文明确是对你的新称呼。
- 如果大家正在讨论你回复怪、上下文差、人设乱，你可以承认刚才没接住，但不要装傻重复问“是在说我吗”。
- 用户引用你的上一句话回复时，要连着看前面的原始请求；必须保留原请求里的硬约束，比如英文/中文、数量、主题、格式。
""",
    memory_when_empty=GROUP_MEMORY_PLACEHOLDER,
    user_instruction="请先判断这是不是在对你说。判断过程不要输出。该沉默就回复空白；该回复就只输出一句自然微信回复。",
    final_instruction="只输出一句自然微信回复；如果不是在对你说，只输出空白；不要输出括号、心理活动或动作描写。",
)

GROUP_AMBIENT = dict(
    chat_scene="微信群「测试群」里会看到每条新消息的普通群友",
    scene_rules="""\
你会看到这个指定群的每一条新消息，但这不代表每句话都是在叫你。
- 先判断这句话和最近上下文有没有自然的接话点；有才说一句，没有就沉默。
- 其他群友彼此问答、话题已经聊完整、只有无意义刷屏时，不要硬插话。
- 可以对当前话题表示认同、补充、轻度吐槽、追问或给直接建议；不需要等 @你。
- 不要把看到全量消息说出来，也不要声称自己一直在监听群聊。
- 用户引用你的上一句话回复时，要连着看前面的原始请求；必须保留原请求里的硬约束，比如英文/中文、数量、主题、格式。
""",
    memory_when_empty=GROUP_MEMORY_PLACEHOLDER,
    user_instruction="这是指定群里的一条新消息。根据最近上下文决定自然接话或沉默；不适合说话就只输出空白。",
    final_instruction="只输出一句自然微信回复；不适合说话只输出空白；不要输出括号、心理活动或动作描写。",
)

GROUP_FORCED = dict(
    chat_scene="微信群「测试群」里会逐条回复的普通群友",
    scene_rules="""\
你会看到这个指定群的每一条新消息，并且每一条可读消息都必须回复。
- 本场景的强制回复要求优先于人设中任何关于自然接话或沉默的描述。
- 不要判断要不要接话，也不要输出空白、沉默或任何“不回复”的说明。
- 消息含义不完整、看不懂或只是表情/图片时，也用测试人设的短句自然回应或追问，例如“啊？”“哪个”“什么情况”。
- 可以对当前话题表示认同、补充、轻度吐槽、追问或给直接建议；不需要等 @你。
- 不要把逐条回复说出来，也不要声称自己一直在监听群聊。
- 用户引用你的上一句话回复时，要连着看前面的原始请求；必须保留原请求里的硬约束，比如英文/中文、数量、主题、格式。
""",
    memory_when_empty=GROUP_MEMORY_PLACEHOLDER,
    user_instruction="这是指定群里的一条新消息，必须输出一句自然微信回复。",
    final_instruction="必须只输出一句自然微信回复，不能输出空白、括号、心理活动或动作描写。",
)

PRIVATE = dict(
    chat_scene="和「张三」的一对一微信私聊对象",
    scene_rules="""\
你看到的是一对一微信私聊，不是群聊截片。对方发来的最新消息默认就是在对你说。
- 私聊不需要 @你；只要内容正常，就像真人微信私聊一样自然接话。
- 如果对方在评价 AI/机器人/你的回复，可以轻声承认没接好，但不要说自己是 AI 助手。
- 最新消息很短时，结合上一两句判断；不确定就轻轻反问一句，不要沉默。
- 不要把私聊误判成群友互相说话，也不要提“群里大家”。
""",
    memory_when_empty=PRIVATE_MEMORY_PLACEHOLDER,
    user_instruction="这是私聊，对方是在和你说话。只输出一句自然微信回复，不要输出心理活动或动作描写。",
    final_instruction="只输出一句自然微信私聊回复；不要输出括号、心理活动或动作描写。",
)

SCENES = [
    pytest.param(GROUP_DIRECT, {}, id="group-direct"),
    pytest.param(GROUP_AMBIENT, {"ambient": True}, id="group-ambient"),
    pytest.param(GROUP_FORCED, {"ambient": True, "force_reply": True}, id="group-ambient-forced"),
    pytest.param(PRIVATE, {"is_group": False}, id="private"),
]


def expected_chat_prompt(scene: dict, **fields) -> str:
    values = dict(
        persona_name="测试人设",
        persona_prompt="PERSONA_PROMPT",
        reply_rules="REPLY_RULES",
        chat_scene=scene["chat_scene"],
        scene_rules=scene["scene_rules"],
        max_chars=20,
        group_memory="MEMORY",
        group_name="测试群",
        current_time="2026-01-02 03:04",
        bot_name="机器人",
        sender_name="张三",
        recent_messages="张三: 今天吃什么\n李四: 随便",
        current_message="今天吃什么",
        final_instruction=scene["final_instruction"],
    )
    values.update(fields)
    return CHAT_FRAME.format(**values)


def make_mode(**overrides) -> ProactiveMode:
    values = dict(
        name="CASUAL",
        label="闲聊",
        description="正常聊天节奏",
        instruction="自然接话",
        min_rate=1.0,
        eval_interval_sec=120,
        reply_probability=0.25,
        max_chars=30,
        context_count=50,
    )
    values.update(overrides)
    return ProactiveMode(**values)


# ── src/persona.py ──────────────────────────────────────────────────────

class TestPersonaModule:
    def test_default_numbers_and_names(self):
        assert DEFAULT_CHAT_PERSONA_NAME == "杨宇靖"
        assert DEFAULT_CHAT_MAX_CHARS == 30
        assert DEFAULT_CHAT_CONTEXT_WINDOW_SEC == 900
        assert DEFAULT_CHAT_CONTEXT_COUNT == 14
        assert DEFAULT_YANG_YUJING_CONTEXT_COUNT == 14
        assert PERSONA_PROFILES == ("yang_yujing", "custom")

    @pytest.mark.parametrize("value, expected", [
        pytest.param(None, "yang_yujing", id="none"),
        pytest.param("", "yang_yujing", id="empty"),
        pytest.param("custom", "custom", id="custom"),
        pytest.param(" Custom ", "custom", id="custom-padded"),
        pytest.param("yang_yujing", "yang_yujing", id="yang"),
        pytest.param("  YANG_YUJING  ", "yang_yujing", id="yang-padded-upper"),
        pytest.param("unknown-profile", "yang_yujing", id="unknown-falls-back"),
    ])
    def test_normalize_persona_profile(self, value, expected):
        assert normalize_persona_profile(value) == expected

    def test_custom_profile_has_no_fixed_preset(self):
        assert persona_defaults_for_profile("custom") is None

    @pytest.mark.parametrize("profile", [None, "", "unknown-profile"])
    def test_a_missing_profile_is_the_yang_preset(self, profile):
        assert persona_defaults_for_profile(profile) == persona_defaults_for_profile("yang_yujing")

    def test_yang_profile_preset(self):
        assert persona_defaults_for_profile("YANG_YUJING") == {
            "name": DEFAULT_YANG_YUJING_PERSONA_NAME,
            "prompt": DEFAULT_YANG_YUJING_PERSONA_PROMPT,
            "reply_rules": DEFAULT_YANG_YUJING_REPLY_RULES,
            "max_chars": 30,
            "context_window_sec": DEFAULT_CHAT_CONTEXT_WINDOW_SEC,
            "context_count": 14,
        }
        assert DEFAULT_YANG_YUJING_PERSONA_NAME == "杨宇靖"

    # PINNED (known issue, expected to change in Phase 3): one specific group's
    # name and one specific group's alias joke are baked into source/prompt text.
    def test_group_specific_facts_are_hard_coded_in_source(self):
        assert DEFAULT_YANG_YUJING_GROUP_NAME == "群主应该是pyc"
        assert "次元和 cy 是同一个人" in DEFAULT_YANG_YUJING_PERSONA_PROMPT


class TestConfigureChatPersona:
    def test_unconfigured_summarizer_uses_module_defaults(self):
        summarizer = FakeSummarizer()
        assert summarizer._chat_persona_settings() == {
            "persona_name": DEFAULT_CHAT_PERSONA_NAME,
            "persona_prompt": DEFAULT_CHAT_PERSONA_PROMPT,
            "reply_rules": DEFAULT_CHAT_REPLY_RULES,
            "max_chars": DEFAULT_CHAT_MAX_CHARS,
            "context_count": DEFAULT_CHAT_CONTEXT_COUNT,
        }
        # the window is only stored by configure_chat_persona(); nothing in
        # base.py reads it back (the router uses its own config value)
        assert not hasattr(summarizer, "chat_context_window_sec")

    def test_configured_values_are_stored(self):
        summarizer = FakeSummarizer()
        summarizer.configure_chat_persona(
            persona_name="A", persona_prompt="B", reply_rules="C",
            max_chars=50, context_window_sec=300, context_count=7,
        )
        assert summarizer._chat_persona_settings() == {
            "persona_name": "A", "persona_prompt": "B", "reply_rules": "C",
            "max_chars": 50, "context_count": 7,
        }
        assert summarizer.chat_context_window_sec == 300

    @pytest.mark.parametrize("given, stored", [
        pytest.param(0, DEFAULT_CHAT_MAX_CHARS, id="zero-means-default"),
        pytest.param(None, DEFAULT_CHAT_MAX_CHARS, id="none-means-default"),
        pytest.param(3, 8, id="below-min-clamped-to-8"),
        pytest.param(8, 8, id="min"),
        pytest.param(200, 200, id="max"),
        pytest.param(999, 200, id="above-max-clamped-to-200"),
        pytest.param("50", 50, id="numeric-string"),
    ])
    def test_max_chars_clamp(self, given, stored):
        summarizer = FakeSummarizer()
        summarizer.configure_chat_persona(max_chars=given)
        assert summarizer.chat_max_chars == stored

    @pytest.mark.parametrize("given, stored", [
        pytest.param(0, DEFAULT_CHAT_CONTEXT_COUNT, id="zero-means-default"),
        pytest.param(-5, 1, id="negative-clamped-to-1"),
        pytest.param(1, 1, id="min"),
        pytest.param(120, 120, id="max"),
        pytest.param(500, 120, id="above-max-clamped-to-120"),
    ])
    def test_context_count_clamp(self, given, stored):
        summarizer = FakeSummarizer()
        summarizer.configure_chat_persona(context_count=given)
        assert summarizer.chat_context_count == stored

    @pytest.mark.parametrize("given, stored", [
        pytest.param(0, DEFAULT_CHAT_CONTEXT_WINDOW_SEC, id="zero-means-default"),
        pytest.param(10, 60, id="below-min-clamped-to-60"),
        pytest.param(60, 60, id="min"),
        pytest.param(7200, 7200, id="max"),
        pytest.param(99999, 7200, id="above-max-clamped-to-7200"),
    ])
    def test_context_window_clamp(self, given, stored):
        summarizer = FakeSummarizer()
        summarizer.configure_chat_persona(context_window_sec=given)
        assert summarizer.chat_context_window_sec == stored

    def test_falsy_text_settings_fall_back_to_defaults(self):
        summarizer = FakeSummarizer()
        summarizer.configure_chat_persona(persona_name="", persona_prompt="", reply_rules="")
        assert summarizer.chat_persona_name == DEFAULT_CHAT_PERSONA_NAME
        assert summarizer.chat_persona_prompt == DEFAULT_CHAT_PERSONA_PROMPT
        assert summarizer.chat_reply_rules == DEFAULT_CHAT_REPLY_RULES


# ── recent-message block ────────────────────────────────────────────────

class TestRecentMessageFormatting:
    fmt = staticmethod(AbstractSummarizer._format_recent_messages)

    @pytest.mark.parametrize("history", [None, []], ids=["none", "empty-list"])
    def test_no_history_placeholder(self, history):
        assert self.fmt(history, 10) == NO_HISTORY

    def test_lines_are_sender_colon_content_in_order(self):
        assert self.fmt(CONTEXT, 10) == "张三: 今天吃什么\n李四: 随便"

    def test_only_the_last_limit_messages_are_used(self):
        history = [{"sender_name": f"u{i}", "content": f"c{i}"} for i in range(6)]
        assert self.fmt(history, 3) == "u3: c3\nu4: c4\nu5: c5"

    def test_blank_messages_are_dropped_after_the_window_is_cut(self):
        history = [
            {"sender_name": "a", "content": "1"},
            {"sender_name": "b", "content": "2"},
            {"sender_name": "c", "content": ""},
            {"sender_name": "d", "content": "   "},
            {"sender_name": "e", "content": None},
        ]
        # window = [c, d, e]: all blank, even though a and b exist
        assert self.fmt(history, 3) == NO_HISTORY
        assert self.fmt(history, 4) == "b: 2"
        assert self.fmt(history, 5) == "a: 1\nb: 2"

    def test_missing_sender_is_a_question_mark(self):
        assert self.fmt([{"content": "hi"}], 5) == "?: hi"

    # PINNED (known issue, expected to change in Phase 2): an explicit None
    # sender is rendered literally.
    def test_none_sender_is_rendered_as_the_word_none(self):
        assert self.fmt([{"sender_name": None, "content": "hi"}], 5) == "None: hi"

    # PINNED (known issue, expected to change in Phase 2): ``[-0:]`` is the
    # whole list, so a limit of 0 shows EVERYTHING instead of nothing.
    # (reachable: ProactiveMode "SLEEP" has context_count == 0)
    def test_limit_zero_returns_every_message(self):
        history = [{"sender_name": f"u{i}", "content": f"c{i}"} for i in range(4)]
        assert self.fmt(history, 0) == "u0: c0\nu1: c1\nu2: c2\nu3: c3"

    # PINNED (known issue, expected to change in Phase 2): message text is not
    # flattened, so one message can forge extra "sender: text" lines.
    def test_newlines_inside_a_message_can_forge_other_speakers(self):
        history = [{"sender_name": "张三", "content": "好的\n李四: 我同意"}]
        assert self.fmt(history, 5) == "张三: 好的\n李四: 我同意"


class TestSanitizeContextContent:
    @pytest.mark.parametrize("value, expected", [
        pytest.param("  hello  ", "hello", id="trimmed"),
        pytest.param(None, "", id="none"),
        pytest.param(12345, "12345", id="int"),
        pytest.param("a" * 79, "a" * 79, id="79-hex-chars-kept"),
        pytest.param("a" * 80, "[图片]", id="80-hex-chars-replaced"),
        pytest.param("ABCDEF0123" * 8, "[图片]", id="uppercase-hex"),
        pytest.param(("0123456789abcdef" * 3) + " \n " + ("0123456789abcdef" * 3), "[图片]",
                     id="whitespace-inside-blob"),
        pytest.param("g" * 80, "g" * 80, id="non-hex-80-chars-kept"),
        pytest.param("看这张图 " + "a" * 80, "看这张图 " + "a" * 80, id="hex-with-text-kept"),
    ])
    def test_sanitize(self, value, expected):
        assert _sanitize_context_content(value) == expected


# ── chat(): scene selection and golden prompts ──────────────────────────

class TestChatScenes:
    @pytest.mark.parametrize("scene, kwargs", SCENES)
    def test_system_and_user_prompt_match_the_golden_text(self, frozen_clock, scene, kwargs):
        summarizer = make_summarizer("好")

        summarizer.chat(**CHAT_KWARGS, **kwargs)

        assert summarizer.system_prompt == expected_chat_prompt(scene)
        assert summarizer.user_prompt == f"最新消息来自 张三：今天吃什么\n{scene['user_instruction']}"

    def test_class_templates_are_pinned(self):
        assert AbstractSummarizer.CHAT_SYSTEM_PROMPT == CHAT_FRAME
        assert AbstractSummarizer.PROACTIVE_SYSTEM_PROMPT == PROACTIVE_FRAME

    def test_force_reply_without_ambient_is_ignored(self, frozen_clock):
        plain = make_summarizer("好")
        forced = make_summarizer("好")

        plain.chat(**CHAT_KWARGS)
        forced.chat(**CHAT_KWARGS, force_reply=True)

        assert forced.calls == plain.calls

    def test_ambient_is_ignored_in_private_chats(self, frozen_clock):
        plain = make_summarizer("好")
        ambient = make_summarizer("好")

        plain.chat(**CHAT_KWARGS, is_group=False)
        ambient.chat(**CHAT_KWARGS, is_group=False, ambient=True, force_reply=True)

        assert ambient.calls == plain.calls

    def test_private_scene_falls_back_to_group_name_when_requester_is_unknown(self):
        summarizer = make_summarizer("好")

        summarizer.chat("你好", is_group=False, group_name="某人", requester_name="")

        prompt = summarizer.system_prompt
        assert prompt.splitlines()[0].startswith("你是测试人设，和「某人」的一对一微信私聊对象。")
        assert "最新消息发送人：群友" in prompt
        assert summarizer.user_prompt.startswith("最新消息来自 群友：你好\n")

    def test_group_defaults_when_names_are_omitted(self):
        summarizer = make_summarizer("好")

        summarizer.chat("你好")

        prompt = summarizer.system_prompt
        assert "微信群「群聊」里的普通群友" in prompt
        assert "你在这里的名字：群聊小助手" in prompt
        assert "最新消息发送人：群友" in prompt
        assert section(prompt, "当前").splitlines()[0].startswith("聊天：群聊  时间：")

    # The forced-reply scene rules used to hard-code the persona name "杨宇靖"
    # whatever persona was configured (fixed in Phase 4: they name the
    # configured persona).  PINNED (known issue, expected to change in Phase 5):
    # the shared template still tells the model to answer with blank when it
    # should not reply -- the opposite of the scene rules.
    def test_forced_scene_names_the_configured_persona_and_keeps_the_blank_rule(self):
        summarizer = make_summarizer("好", persona_name="完全不同的人设")

        summarizer.chat(**CHAT_KWARGS, ambient=True, force_reply=True)

        prompt = summarizer.system_prompt
        assert "用完全不同的人设的短句自然回应或追问" in section(prompt, "场景理解铁律")
        assert "杨宇靖" not in prompt
        assert "如果不该回复，只输出空白。" in section(prompt, "输出硬限制")
        assert prompt.startswith("你是完全不同的人设，")


# ── chat(): prompt details ──────────────────────────────────────────────

class TestChatPromptDetails:
    def test_unconfigured_summarizer_injects_default_persona(self):
        summarizer = FakeSummarizer("好")

        summarizer.chat(**CHAT_KWARGS)

        prompt = summarizer.system_prompt
        assert prompt.startswith(f"你是{DEFAULT_CHAT_PERSONA_NAME}，")
        assert section(prompt, "可配置人设") == DEFAULT_CHAT_PERSONA_PROMPT + "\n"
        assert section(prompt, "回复规则") == DEFAULT_CHAT_REPLY_RULES + "\n"
        assert f"最多 {DEFAULT_CHAT_MAX_CHARS} 个字" in prompt

    # ---- {} in user-controlled text ----------------------------------

    def test_braces_do_not_break_str_format(self):
        summarizer = make_summarizer(
            "好",
            persona_prompt="用 {name} 称呼对方",
            reply_rules="回复里不要出现 {}",
        )

        reply = summarizer.chat(
            message="发送 {message} 和 {0} {}",
            context_messages=[{"sender_name": "{name}", "content": "内容{x}}"}],
            requester_name="{who}",
            bot_name="{bot}",
            group_name="{group}",
            group_memory="{memory}",
        )

        assert reply == "好"

    # PINNED (known issue, expected to change in Phase 2): ``_esc()`` doubles
    # braces in VALUES, but str.format never re-parses substituted values, so
    # the doubled braces reach the model verbatim.  The user prompt is not
    # escaped and keeps single braces.
    def test_escaped_braces_are_doubled_in_the_system_prompt_only(self):
        summarizer = make_summarizer(
            "好",
            persona_prompt="用 {name} 称呼对方",
            reply_rules="回复里不要出现 {}",
        )

        summarizer.chat(
            message="发送 {message} 和 {0} {}",
            context_messages=[{"sender_name": "{name}", "content": "内容{x}}"}],
            requester_name="{who}",
            bot_name="{bot}",
            group_name="{group}",
            group_memory="{memory}",
        )

        prompt = summarizer.system_prompt
        for expected in (
            "微信群「{{group}}」里的普通群友",
            "用 {{name}} 称呼对方",
            "回复里不要出现 {{}}",
            "{{memory}}",
            "聊天：{{group}}  时间：",
            "你在这里的名字：{{bot}}",
            "最新消息发送人：{{who}}",
            "{{name}}: 内容{{x}}}}",
            "发送 {{message}} 和 {{0}} {{}}",
        ):
            assert expected in prompt, expected
        assert summarizer.user_prompt.startswith("最新消息来自 {who}：发送 {message} 和 {0} {}\n")

    # ---- max_chars ---------------------------------------------------

    @pytest.mark.parametrize("persona_max, override, in_prompt", [
        pytest.param(None, None, DEFAULT_CHAT_MAX_CHARS, id="unconfigured-default"),
        pytest.param(50, None, 50, id="persona-value"),
        pytest.param(3, None, 8, id="persona-clamped-up-to-8"),
        pytest.param(999, None, 200, id="persona-clamped-down-to-200"),
        pytest.param(20, 12, 12, id="override-below-persona"),
        pytest.param(20, 100, 100, id="override-above-persona"),
        pytest.param(20, 5, 8, id="override-clamped-up-to-8"),
        pytest.param(20, 0, 8, id="override-zero-is-not-none"),
        pytest.param(20, 250, 250, id="override-can-exceed-persona-ceiling"),
        pytest.param(20, 300, 300, id="override-ceiling"),
        pytest.param(20, 500, 300, id="override-clamped-down-to-300"),
    ])
    def test_max_chars_in_prompt_and_in_reply_cut(self, persona_max, override, in_prompt):
        # a 400-character reply shows the cut length
        long_reply = "字" * 400
        summarizer = FakeSummarizer(long_reply)
        if persona_max is not None:
            summarizer.configure_chat_persona(max_chars=persona_max)

        reply = summarizer.chat("你好", max_chars_override=override)

        assert f"最多 {in_prompt} 个字" in summarizer.system_prompt
        assert reply == "字" * in_prompt

    # ---- context window ---------------------------------------------

    def test_context_count_limits_the_recent_block(self):
        history = [{"sender_name": f"u{i}", "content": f"c{i}"} for i in range(6)]
        summarizer = make_summarizer("好", context_count=3)

        summarizer.chat("你好", context_messages=history)

        block = section(summarizer.system_prompt, "当前")
        assert "u2: c2" not in block
        assert "u3: c3\nu4: c4\nu5: c5\n" in block

    def test_no_context_uses_the_placeholder(self):
        summarizer = make_summarizer("好")

        summarizer.chat("你好", context_messages=None)

        assert f"\n{NO_HISTORY}\n" in summarizer.system_prompt

    def test_message_already_in_context_appears_twice(self):
        # the caller decides whether the latest message is part of the history;
        # chat() never de-duplicates.
        summarizer = make_summarizer("好")

        summarizer.chat("今天吃什么", context_messages=CONTEXT[:1], requester_name="张三")

        assert summarizer.system_prompt.count("今天吃什么") == 2

    # ---- long-term memory --------------------------------------------

    @pytest.mark.parametrize("scene, kwargs", SCENES)
    def test_empty_memory_placeholder_depends_on_scene(self, scene, kwargs):
        summarizer = make_summarizer("好")

        summarizer.chat(**{**CHAT_KWARGS, "group_memory": ""}, **kwargs)

        body = section(summarizer.system_prompt, "长期记忆")
        assert body.splitlines()[0] == scene["memory_when_empty"]

    def test_non_empty_memory_is_injected_verbatim(self):
        memory = "【固定事实/称呼记忆】\n- 宇肆：次元哥哥\n我记得大家爱聊游戏。"
        summarizer = make_summarizer("好")

        summarizer.chat(**{**CHAT_KWARGS, "group_memory": memory})

        assert section(summarizer.system_prompt, "长期记忆").startswith(memory + "\n如果长期记忆里有")

    # PINNED (known issue, expected to change in Phase 3): emptiness is a plain
    # truthiness test, so a whitespace-only memory replaces the placeholder.
    def test_whitespace_only_memory_is_not_treated_as_empty(self):
        summarizer = make_summarizer("好")

        summarizer.chat(**{**CHAT_KWARGS, "group_memory": "   "})

        body = section(summarizer.system_prompt, "长期记忆")
        assert GROUP_MEMORY_PLACEHOLDER not in body
        assert body.splitlines()[0] == "   "

    # ---- time ---------------------------------------------------------

    def test_current_time_comes_from_the_real_clock_in_minute_resolution(self):
        fmt = "%Y-%m-%d %H:%M"
        before = datetime.datetime.now().strftime(fmt)
        summarizer = make_summarizer("好")

        summarizer.chat(**CHAT_KWARGS)

        after = datetime.datetime.now().strftime(fmt)
        match = re.search(r"聊天：测试群  时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\n", summarizer.system_prompt)
        assert match
        assert match.group(1) in {before, after}

    # ---- user prompt --------------------------------------------------

    def test_user_prompt_keeps_the_raw_message_including_newlines(self):
        summarizer = make_summarizer("好")

        summarizer.chat("第一行\n第二行", requester_name="张三")

        assert summarizer.user_prompt.startswith("最新消息来自 张三：第一行\n第二行\n")
        assert summarizer.calls[-1][1] == [{"role": "user", "content": summarizer.user_prompt}]


# ── chat(): reply post-processing and the silence flag ──────────────────

class TestSilenceFlag:
    def test_attribute_does_not_exist_before_the_first_chat_call(self):
        # PINNED (known issue, expected to change in Phase 2): the router has
        # to use getattr(..., False) because only chat() creates the flag.
        assert not hasattr(FakeSummarizer(), "last_chat_reply_was_intentional_silence")

    @pytest.mark.parametrize("raw, reply, flag", [
        pytest.param("好呀", "好呀", False, id="normal-reply"),
        pytest.param("", "", False, id="model-returned-nothing"),
        pytest.param("   ", "", False, id="whitespace-only"),
        pytest.param("(沉默)", "", True, id="bracketed-silence"),
        pytest.param("不回复", "", True, id="token"),
        pytest.param("无需回应他", "", True, id="hard-marker"),
        pytest.param("~~~", "", False, id="punctuation-only-is-not-silence"),
        pytest.param("嗯（沉默）", "嗯", False, id="aside-stripped-but-reply-survives"),
        # PINNED (known issue, expected to change in Phase 2): the flag is
        # computed on the RAW text, so silence that only becomes visible after
        # cleanup is reported as an unexplained empty reply.
        pytest.param("杨宇靖：空白", "", False, id="pinned-name-prefix-plus-blank"),
        pytest.param("输出：沉默", "", False, id="pinned-label-plus-silence"),
        pytest.param("不回复（沉默）", "", False, id="pinned-token-plus-aside"),
    ])
    def test_flag_and_reply(self, raw, reply, flag):
        summarizer = FakeSummarizer(raw)

        assert summarizer.chat("测试") == reply
        assert summarizer.last_chat_reply_was_intentional_silence is flag

    def test_flag_is_reset_at_the_start_of_every_call(self):
        summarizer = FakeSummarizer("不回复", "好呀")

        summarizer.chat("一")
        assert summarizer.last_chat_reply_was_intentional_silence is True
        summarizer.chat("二")
        assert summarizer.last_chat_reply_was_intentional_silence is False

    def test_flag_is_false_when_the_api_call_fails(self, monkeypatch):
        monkeypatch.setenv("CHAT_API_MAX_ATTEMPTS", "1")
        summarizer = FakeSummarizer("不回复", TransientError("down"))
        summarizer.chat("一")
        assert summarizer.last_chat_reply_was_intentional_silence is True

        with pytest.raises(RuntimeError):
            summarizer.chat("二")

        assert summarizer.last_chat_reply_was_intentional_silence is False

    # PINNED (known issue, expected to change in Phase 2): the flag is one
    # attribute on the shared summarizer instance, not part of chat()'s return
    # value.  The WCDB backend runs router callbacks on a 4-thread pool, so a
    # second message that finishes between chat() returning and the router
    # reading the flag overwrites the first message's answer.
    def test_flag_only_reflects_the_most_recent_call_on_the_instance(self):
        summarizer = FakeSummarizer("不回复", "好呀")

        assert summarizer.chat("消息A") == ""     # intentional silence, flag True
        assert summarizer.chat("消息B") == "好呀"  # "another thread" finishes next
        # ... and message A's caller now reads the flag:
        assert summarizer.last_chat_reply_was_intentional_silence is False

    def test_proactive_chat_never_touches_the_flag(self):
        summarizer = FakeSummarizer("不回复", "好呀")
        summarizer.chat("消息")
        assert summarizer.last_chat_reply_was_intentional_silence is True

        assert summarizer.proactive_chat(make_mode(), CONTEXT) == "好呀"

        assert summarizer.last_chat_reply_was_intentional_silence is True


class TestChatRetry:
    def test_retries_once_by_default_and_backs_off_two_seconds(self, sleeps):
        summarizer = FakeSummarizer(TransientError("busy"), "好呀")

        assert summarizer.chat("测试") == "好呀"

        assert len(summarizer.calls) == 2
        assert sleeps == [2]
        assert summarizer.last_api_call_time > 0

    def test_failure_after_the_last_attempt_raises_runtime_error(self, sleeps):
        summarizer = FakeSummarizer(TransientError("busy"))

        with pytest.raises(RuntimeError) as excinfo:
            summarizer.chat("测试")

        assert str(excinfo.value) == "Failed after 2 attempt(s) on 'AI chat': busy"
        assert len(summarizer.calls) == 2
        assert sleeps == [2]
        assert summarizer.last_api_call_time == 0.0

    @pytest.mark.parametrize("env, attempts, waits", [
        pytest.param("1", 1, [], id="one-attempt"),
        pytest.param("3", 3, [2, 4], id="three-attempts"),
        pytest.param("4", 4, [2, 4, 8], id="four-attempts"),
        pytest.param("", 2, [2], id="empty-string-means-default"),
        pytest.param("0", 1, [], id="zero-is-one-attempt"),
        pytest.param("-5", 1, [], id="negative-is-one-attempt"),
        pytest.param(" 4 ", 4, [2, 4, 8], id="whitespace-is-tolerated"),
    ])
    def test_attempts_follow_chat_api_max_attempts(self, monkeypatch, sleeps, env, attempts, waits):
        monkeypatch.setenv("CHAT_API_MAX_ATTEMPTS", env)
        summarizer = FakeSummarizer(TransientError("down"))

        with pytest.raises(RuntimeError, match=rf"Failed after {attempts} attempt\(s\)"):
            summarizer.chat("测试")

        assert len(summarizer.calls) == attempts
        assert sleeps == waits

    # PINNED (known issue, expected to change in Phase 2): a malformed env value
    # is not caught, so every chat() raises ValueError before any API call.
    @pytest.mark.parametrize("env", ["abc", "2.5"])
    def test_non_integer_env_value_raises_value_error(self, monkeypatch, env):
        monkeypatch.setenv("CHAT_API_MAX_ATTEMPTS", env)
        summarizer = FakeSummarizer("好呀")

        with pytest.raises(ValueError):
            summarizer.chat("测试")

        assert summarizer.calls == []

    def test_non_retryable_exception_propagates_unchanged(self, sleeps):
        summarizer = FakeSummarizer(ValueError("bad request"), "好呀")

        with pytest.raises(ValueError, match="bad request"):
            summarizer.chat("测试")

        assert len(summarizer.calls) == 1
        assert sleeps == []

    # PINNED (known issue, expected to change in Phase 2): the base class
    # default is ``retry_exceptions = ()``; a backend that forgets to override
    # it silently gets no retry at all.
    def test_default_retry_exceptions_retry_nothing(self, sleeps):
        class NoRetry(FakeSummarizer):
            retry_exceptions = ()

        summarizer = NoRetry(TransientError("busy"), "好呀")

        with pytest.raises(TransientError):
            summarizer.chat("测试")

        assert len(summarizer.calls) == 1
        assert sleeps == []

    # PINNED (known issue, expected to change in Phase 2): chat() always passes
    # its own attempt count, so the class-level ``max_retries`` knob is dead for
    # chat (it only drives proactive_chat and the summary strategies).
    def test_class_max_retries_is_ignored_by_chat(self):
        summarizer = FakeSummarizer(TransientError("down"))
        summarizer.max_retries = 5

        with pytest.raises(RuntimeError):
            summarizer.chat("测试")

        assert len(summarizer.calls) == 2


# ── proactive_chat() ────────────────────────────────────────────────────

class TestProactiveChat:
    def test_prompt_matches_the_golden_text(self, frozen_clock):
        summarizer = make_summarizer("哈哈")

        reply = summarizer.proactive_chat(
            make_mode(), CONTEXT, bot_name="机器人", group_name="测试群", group_memory="MEMORY",
        )

        assert reply == "哈哈"
        assert summarizer.system_prompt == PROACTIVE_FRAME.format(
            persona_name="测试人设",
            group_name="测试群",
            persona_prompt="PERSONA_PROMPT",
            reply_rules="REPLY_RULES",
            mode_label="闲聊",
            mode_description="正常聊天节奏",
            mode_instruction="自然接话",
            max_chars=20,
            group_memory="MEMORY",
            current_time="2026-01-02 03:04",
            bot_name="机器人",
            recent_messages="张三: 今天吃什么\n李四: 随便",
        )
        assert summarizer.calls[-1][1] == [
            {"role": "user", "content": "先判断是否适合插话。适合就发一句；不适合就回复空白。"},
        ]

    def test_names_default_like_chat_does(self):
        summarizer = make_summarizer("哈哈")

        summarizer.proactive_chat(make_mode(), CONTEXT)

        prompt = summarizer.system_prompt
        assert "微信群「群聊」里的普通群友" in prompt
        assert "群：群聊  时间：" in prompt
        assert "你在群里的名字：群聊小助手" in prompt

    @pytest.mark.parametrize("history", [
        pytest.param(None, id="none"),
        pytest.param([], id="empty"),
        pytest.param([{"sender_name": "张三", "content": ""}, {"sender_name": "李四", "content": "  "}],
                     id="all-blank"),
    ])
    def test_blank_without_usable_context_and_no_api_call(self, history):
        summarizer = make_summarizer("哈哈")

        assert summarizer.proactive_chat(make_mode(), history) == ""

        assert summarizer.calls == []

    @pytest.mark.parametrize("mode_max, mode_count, persona_max, persona_count, in_prompt, lines", [
        pytest.param(20, 2, 36, 40, 20, 2, id="mode-is-smaller"),
        pytest.param(50, 60, 30, 14, 30, 14, id="persona-is-smaller"),
        pytest.param(30, 50, 30, 50, 30, 50, id="equal"),
    ])
    def test_limits_are_the_smaller_of_mode_and_persona(
        self, mode_max, mode_count, persona_max, persona_count, in_prompt, lines,
    ):
        history = [{"sender_name": f"u{i}", "content": f"c{i}"} for i in range(60)]
        summarizer = make_summarizer(
            "字" * 100, max_chars=persona_max, context_count=persona_count,
        )

        reply = summarizer.proactive_chat(
            make_mode(max_chars=mode_max, context_count=mode_count), history,
        )

        prompt = summarizer.system_prompt
        assert f"最多 {in_prompt} 个字" in prompt
        assert reply == "字" * in_prompt
        block = prompt.split("最近群聊：\n")[1].split("\n\n只输出你要发的那句话")[0]
        assert len(block.splitlines()) == lines
        assert block.splitlines()[-1] == "u59: c59"

    # PINNED (known issue, expected to change in Phase 2): the SLEEP mode has
    # max_chars == 0 and context_count == 0.  proactive_chat() still calls the
    # model, shows the WHOLE history (``[-0:]``), tells the model "最多 0 个字"
    # and then truncates whatever comes back to "".
    def test_zero_limits_call_the_model_show_everything_and_blank_the_reply(self):
        history = [{"sender_name": f"u{i}", "content": f"c{i}"} for i in range(3)]
        summarizer = make_summarizer("好呀")

        reply = summarizer.proactive_chat(make_mode(max_chars=0, context_count=0), history)

        assert reply == ""
        assert len(summarizer.calls) == 1
        assert "最多 0 个字" in summarizer.system_prompt
        assert "u0: c0\nu1: c1\nu2: c2" in summarizer.system_prompt

    def test_empty_memory_placeholder_and_verbatim_memory(self):
        summarizer = make_summarizer("好")

        summarizer.proactive_chat(make_mode(), CONTEXT)
        assert section(summarizer.system_prompt, "你在这个群里的长期记忆").startswith(
            GROUP_MEMORY_PLACEHOLDER + "\n",
        )

        summarizer.proactive_chat(make_mode(), CONTEXT, group_memory="大家爱聊游戏")
        assert section(summarizer.system_prompt, "你在这个群里的长期记忆").startswith("大家爱聊游戏\n")

    @pytest.mark.parametrize("raw, expected", [
        pytest.param("（笑）好哦", "好哦", id="aside-stripped"),
        pytest.param("不回复", "", id="silence-token"),
        pytest.param("空白", "", id="blank-token"),
        pytest.param("", "", id="empty-reply"),
        pytest.param("测试人设：哈哈哈", "哈哈哈", id="persona-name-prefix"),
        pytest.param("群聊小助手：哈哈哈", "哈哈哈", id="bot-name-prefix"),
        pytest.param("小明：哈哈哈", "小明：哈哈哈", id="unknown-name-is-content"),
    ])
    def test_reply_goes_through_the_same_cleaner(self, raw, expected):
        summarizer = make_summarizer(raw)

        assert summarizer.proactive_chat(make_mode(), CONTEXT) == expected

    def test_runtime_error_from_exhausted_retries_returns_blank(self, sleeps):
        summarizer = make_summarizer(TransientError("down"))

        assert summarizer.proactive_chat(make_mode(), CONTEXT) == ""

        assert len(summarizer.calls) == 3          # class max_retries
        assert sleeps == [2, 4]

    def test_chat_api_max_attempts_env_is_not_used(self, monkeypatch):
        monkeypatch.setenv("CHAT_API_MAX_ATTEMPTS", "1")
        summarizer = make_summarizer(TransientError("down"))

        summarizer.proactive_chat(make_mode(), CONTEXT)

        assert len(summarizer.calls) == 3

    def test_retry_then_success(self, sleeps):
        summarizer = make_summarizer(TransientError("busy"), "哈哈")

        assert summarizer.proactive_chat(make_mode(), CONTEXT) == "哈哈"
        assert sleeps == [2]

    # PINNED (known issue, expected to change in Phase 2): ANY RuntimeError is
    # swallowed, including one raised by the backend itself or a programming
    # error that happens to be a RuntimeError subclass.
    def test_any_runtime_error_is_swallowed(self, sleeps):
        summarizer = make_summarizer(RuntimeError("backend bug"))

        assert summarizer.proactive_chat(make_mode(), CONTEXT) == ""

        assert len(summarizer.calls) == 1
        assert sleeps == []

    def test_other_exceptions_propagate(self):
        summarizer = make_summarizer(ValueError("bad request"))

        with pytest.raises(ValueError):
            summarizer.proactive_chat(make_mode(), CONTEXT)

    # PINNED (known issue, expected to change in Phase 2): same doubled-brace
    # effect as in chat(); the mode texts are passed through untouched.
    def test_braces_are_doubled_for_user_text_but_not_for_mode_text(self):
        summarizer = make_summarizer("好", persona_prompt="称呼 {name}")
        mode = make_mode(label="{L}", description="{D}", instruction="{I}")

        summarizer.proactive_chat(
            mode,
            [{"sender_name": "{who}", "content": "{what}"}],
            bot_name="{bot}", group_name="{group}", group_memory="{memory}",
        )

        prompt = summarizer.system_prompt
        for expected in (
            "微信群「{{group}}」里的普通群友",
            "称呼 {{name}}",
            "{{who}}: {{what}}",
            "{{memory}}",
            "你在群里的名字：{{bot}}",
            "氛围模式：{L}（{D}）",
            "你应该：{I}",
        ):
            assert expected in prompt, expected


# ── summarize() strategy selection ──────────────────────────────────────

def numbered_messages(count: int) -> list[dict]:
    return [
        {"message_id": f"m{i}", "sender_name": "s", "content": "x" * 10}
        for i in range(count)
    ]


class TestTokenEstimate:
    est = staticmethod(AbstractSummarizer._estimate_tokens)

    def test_formula(self):
        assert self.est([]) == 500
        # (len("ab") + len("cdef") + 40) / 1.5 = 30.67 -> 30, plus 500
        assert self.est([{"sender_name": "ab", "content": "cdef"}]) == 530
        # missing keys count as empty strings: 40 / 1.5 = 26
        assert self.est([{}]) == 526
        # 3 * (1 + 10 + 40) / 1.5 = 102
        assert self.est(numbered_messages(3)) == 602

    def test_counts_characters_not_tokens(self):
        assert self.est([{"sender_name": "张三", "content": "你好" * 5}]) == int((2 + 10 + 40) / 1.5) + 500

    # PINNED (known issue, expected to change in Phase 2): a None content (the
    # DB column is NOT NULL, but other sources may differ) is a TypeError.
    def test_none_content_raises_type_error(self):
        with pytest.raises(TypeError):
            self.est([{"sender_name": "s", "content": None}])


class TestChunking:
    def test_split_into_chunks(self):
        summarizer = FakeSummarizer()
        summarizer.chunk_size = 2

        chunks = summarizer._split_into_chunks(numbered_messages(5))

        assert [[m["message_id"] for m in c] for c in chunks] == [["m0", "m1"], ["m2", "m3"], ["m4"]]
        assert summarizer._split_into_chunks([]) == []

    def test_class_defaults(self):
        assert AbstractSummarizer.token_budget == 100_000
        assert AbstractSummarizer.chunk_size == 400
        assert AbstractSummarizer.merge_batch_size == 5
        assert AbstractSummarizer.max_retries == 3


class TestSummarizeStrategy:
    def test_empty_list_short_circuits(self):
        summarizer = FakeSummarizer()

        result = summarizer.summarize([], "张三")

        assert result == SummaryResult(summary_text="没有找到新消息。", topics=[], participants=[])
        assert summarizer.summary_log == []

    def test_small_conversation_is_summarised_directly(self):
        summarizer = FakeSummarizer()

        result = summarizer.summarize(numbered_messages(5), "张三")

        assert result.summary_text == "direct:5"
        assert summarizer.summary_log == [("direct", 5, "张三")]

    def test_estimate_equal_to_budget_is_still_direct(self):
        messages = numbered_messages(3)
        summarizer = FakeSummarizer()
        summarizer.token_budget = summarizer._estimate_tokens(messages)
        summarizer.chunk_size = 1

        assert summarizer.summarize(messages, "张三").summary_text == "direct:3"

    def test_estimate_one_over_budget_goes_map_reduce(self):
        messages = numbered_messages(3)
        summarizer = FakeSummarizer()
        summarizer.token_budget = summarizer._estimate_tokens(messages) - 1
        summarizer.chunk_size = 1

        result = summarizer.summarize(messages, "张三")

        assert result.summary_text == "merged(chunk1/3|chunk2/3|chunk3/3)"

    def test_map_reduce_calls_each_chunk_then_one_merge(self):
        summarizer = FakeSummarizer()
        summarizer.token_budget = 0
        summarizer.chunk_size = 2
        summarizer.merge_batch_size = 5

        result = summarizer.summarize(numbered_messages(5), "张三")

        assert result.summary_text == "merged(chunk1/3|chunk2/3|chunk3/3)"
        assert summarizer.summary_log == [
            ("chunk", 2, 1, 3, "张三"),
            ("chunk", 2, 2, 3, "张三"),
            ("chunk", 1, 3, 3, "张三"),
            ("merge", ("chunk1/3", "chunk2/3", "chunk3/3"), "张三"),
        ]

    def test_chunks_equal_to_batch_size_still_use_plain_map_reduce(self):
        summarizer = FakeSummarizer()
        summarizer.token_budget = 0
        summarizer.chunk_size = 1
        summarizer.merge_batch_size = 3

        summarizer.summarize(numbered_messages(3), "张三")

        merges = [entry for entry in summarizer.summary_log if entry[0] == "merge"]
        assert merges == [("merge", ("chunk1/3", "chunk2/3", "chunk3/3"), "张三")]

    def test_more_chunks_than_batch_size_goes_multi_level(self):
        summarizer = FakeSummarizer()
        summarizer.token_budget = 0
        summarizer.chunk_size = 2
        summarizer.merge_batch_size = 2

        result = summarizer.summarize(numbered_messages(5), "张三")

        assert [entry for entry in summarizer.summary_log if entry[0] == "merge"] == [
            ("merge", ("chunk1/3", "chunk2/3"), "张三（第1/2批）"),
            ("merge", ("chunk3/3",), "张三（第2/2批）"),
            ("merge", ("merged(chunk1/3|chunk2/3)", "merged(chunk3/3)"), "张三"),
        ]
        assert result.summary_text == "merged(merged(chunk1/3|chunk2/3)|merged(chunk3/3))"
        assert result.topics == ["t"]

    # PINNED (known issue, expected to change in Phase 2): with a single batch
    # both branches of the final step are identical, so the lone batch summary
    # is merged AGAIN (one wasted model call).  Unreachable through summarize()
    # (it needs more chunks than merge_batch_size) but live for direct callers.
    def test_single_batch_is_merged_twice(self):
        summarizer = FakeSummarizer()

        result = summarizer._multi_level_map_reduce([numbered_messages(2)], "张三")

        assert summarizer.summary_log == [
            ("chunk", 2, 1, 1, "张三"),
            ("merge", ("chunk1/1",), "张三（第1/1批）"),
            ("merge", ("merged(chunk1/1)",), "张三"),
        ]
        assert result.summary_text == "merged(merged(chunk1/1))"

    @pytest.mark.parametrize("method", ["_summarize_map_reduce", "_multi_level_map_reduce"])
    def test_defensive_empty_chunk_list(self, method):
        summarizer = FakeSummarizer()

        result = getattr(summarizer, method)([], "张三")

        assert result == SummaryResult(summary_text="无法生成总结。", topics=[], participants=[])
        assert summarizer.summary_log == []


class TestFormatSummaryForReply:
    @staticmethod
    def fmt(**fields):
        values = dict(summary_text="", topics=[], participants=[])
        values.update(fields)
        return FakeSummarizer().format_summary_for_reply(SummaryResult(**values), "张三")

    def test_header_blank_line_and_trimmed_text(self):
        text = self.fmt(summary_text="  1. 主题A\n2. 主题B  ", topics=["ignored"])
        assert text == "@张三 你错过的：\n\n1. 主题A\n2. 主题B"

    def test_topics_are_numbered_only_when_text_is_empty(self):
        assert self.fmt(topics=["话题一", "话题二"]) == "@张三 你错过的：\n\n1. 话题一\n2. 话题二"

    def test_nothing_to_show_is_just_the_header_and_a_blank_line(self):
        assert self.fmt() == "@张三 你错过的：\n"

    # PINNED (known issue, expected to change in Phase 2): a whitespace-only
    # summary_text is truthy, so the topics fallback is skipped and the reply
    # is an empty body.
    def test_whitespace_only_text_hides_the_topic_fallback(self):
        assert self.fmt(summary_text="   ", topics=["话题一"]) == "@张三 你错过的：\n\n"

    def test_participants_are_not_rendered(self):
        text = self.fmt(
            summary_text="内容",
            participants=[ParticipantContribution(name="李四", contributions="很多")],
        )
        assert text == "@张三 你错过的：\n\n内容"


class TestRetryWithBackoff:
    def test_success_records_the_call_time(self):
        summarizer = FakeSummarizer()
        before = time.time()

        assert summarizer._retry_with_backoff(lambda: "ok", "label") == "ok"

        assert before <= summarizer.last_api_call_time <= time.time()

    def test_exponential_backoff_between_attempts(self, sleeps):
        summarizer = FakeSummarizer()
        outcomes = [TransientError("1"), TransientError("2"), "done"]

        def flaky():
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        assert summarizer._retry_with_backoff(flaky, "label") == "done"
        assert sleeps == [2, 4]

    def test_exhaustion_names_label_attempts_and_last_error(self, sleeps):
        summarizer = FakeSummarizer()

        def always():
            raise TransientError("still down")

        with pytest.raises(RuntimeError) as excinfo:
            summarizer._retry_with_backoff(always, "my label", max_attempts=4)

        assert str(excinfo.value) == "Failed after 4 attempt(s) on 'my label': still down"
        assert sleeps == [2, 4, 8]

    # PINNED (known issue, expected to change in Phase 2): ``max_attempts=0``
    # means "use the class default (3)", not "do not try".
    def test_zero_max_attempts_falls_back_to_the_class_default(self, sleeps):
        summarizer = FakeSummarizer()

        def always():
            raise TransientError("down")

        with pytest.raises(RuntimeError, match=r"Failed after 3 attempt\(s\)"):
            summarizer._retry_with_backoff(always, "label", max_attempts=0)

        assert sleeps == [2, 4]
