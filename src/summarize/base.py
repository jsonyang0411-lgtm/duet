"""Abstract base class for AI summarization backends.

Implementations: ClaudeSummarizer, DeepSeekSummarizer.
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Callable, Iterable, TypeVar

from .models import SummaryResult
from src.pipeline import text as reply_text
from src.pipeline.types import STRATEGY_SINGLE, ChatRequest, Decision, ModelOutput
from src.persona import (
    DEFAULT_CHAT_CONTEXT_COUNT,
    DEFAULT_CHAT_CONTEXT_WINDOW_SEC,
    DEFAULT_CHAT_MAX_CHARS,
    DEFAULT_CHAT_PERSONA_NAME,
    DEFAULT_CHAT_PERSONA_PROMPT,
    DEFAULT_CHAT_REPLY_RULES,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
# Shared with the structured pipeline (src/pipeline/text.py).
_sanitize_context_content = reply_text.sanitize_context_content


class AbstractSummarizer(ABC):
    """Abstract summarizer with shared logic for chunking, retry, and formatting.

    Subclasses must implement:
      - _summarize_direct(messages, requester_name) -> SummaryResult
      - _summarize_chunk(chunk, chunk_num, total, requester_name) -> str
      - _merge_chunk_summaries(chunk_summaries, requester_name) -> SummaryResult
      - consolidate_memory(existing_memory, new_messages) -> str
      - _call_chat_api(system_prompt, messages) -> str

    They may override:
      - token_budget (default 100K)
      - chunk_size (default 400)
      - retry_exceptions (tuple of exception types to retry on)
    """

    # Override in subclass
    token_budget: int = 100_000
    chunk_size: int = 400
    merge_batch_size: int = 5
    max_retries: int = 3
    retry_exceptions: tuple = ()

    # Health monitoring: track last successful API call timestamp
    last_api_call_time: float = 0.0

    # ── Conversational chat (non-summary @bot mentions) ────────────

    # Chat prompt template — supports {placeholders}
    CHAT_SYSTEM_PROMPT = """\
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

    def configure_chat_persona(self,
                                persona_name: str = DEFAULT_CHAT_PERSONA_NAME,
                                persona_prompt: str = DEFAULT_CHAT_PERSONA_PROMPT,
                                reply_rules: str = DEFAULT_CHAT_REPLY_RULES,
                                max_chars: int = DEFAULT_CHAT_MAX_CHARS,
                                context_window_sec: int = DEFAULT_CHAT_CONTEXT_WINDOW_SEC,
                                context_count: int = DEFAULT_CHAT_CONTEXT_COUNT) -> None:
        """Attach runtime persona settings loaded from BotConfig."""
        self.chat_persona_name = persona_name or DEFAULT_CHAT_PERSONA_NAME
        self.chat_persona_prompt = persona_prompt or DEFAULT_CHAT_PERSONA_PROMPT
        self.chat_reply_rules = reply_rules or DEFAULT_CHAT_REPLY_RULES
        self.chat_max_chars = max(8, min(200, int(max_chars or DEFAULT_CHAT_MAX_CHARS)))
        self.chat_context_window_sec = max(
            60,
            min(7200, int(context_window_sec or DEFAULT_CHAT_CONTEXT_WINDOW_SEC)),
        )
        self.chat_context_count = max(
            1,
            min(120, int(context_count or DEFAULT_CHAT_CONTEXT_COUNT)),
        )

    # ── Observability (observe only) ───────────────────────────────

    # Optional callable(**fields) installed by the router's DecisionTracer.
    _call_recorder: Callable[..., None] | None = None

    def set_call_recorder(self, recorder) -> None:
        """Install (or clear with None) the model-call recorder."""
        self._call_recorder = recorder

    def _record_call(self, **fields) -> None:
        """Hand one model-call snapshot to the recorder; never raises."""
        recorder = self._call_recorder
        if recorder is None:
            return
        try:
            fields.setdefault("backend", type(self).__name__)
            fields.setdefault("model", str(getattr(self, "model", "") or ""))
            recorder(**fields)
        except Exception:
            logger.debug("model-call recorder failed", exc_info=True)

    def _chat_persona_settings(self) -> dict:
        return {
            "persona_name": getattr(self, "chat_persona_name", DEFAULT_CHAT_PERSONA_NAME),
            "persona_prompt": getattr(self, "chat_persona_prompt", DEFAULT_CHAT_PERSONA_PROMPT),
            "reply_rules": getattr(self, "chat_reply_rules", DEFAULT_CHAT_REPLY_RULES),
            "max_chars": getattr(self, "chat_max_chars", DEFAULT_CHAT_MAX_CHARS),
            "context_count": getattr(self, "chat_context_count", DEFAULT_CHAT_CONTEXT_COUNT),
        }

    @staticmethod
    def _is_silence_meta_reply(text: str) -> bool:
        """Detect model explanations/asides that must not be sent to WeChat."""
        if not text:
            return False
        raw = str(text or "").strip()
        # A full reply wrapped only in parentheses is almost always chain-of-
        # thought, a stage direction, or novel-style narration in this app.
        if re.fullmatch(r"(?:[（(][^（）()]{1,180}[）)]\s*)+", raw):
            return True

        normalized = re.sub(r"\s+", "", raw).lower()
        normalized = normalized.strip("()（）[]【】「」『』\"'“”‘’")
        if normalized in {
            "空白", "沉默", "blank", "null", "none",
            "回复空白", "输出空白", "不回复", "不回应",
        }:
            return True

        hard_markers = (
            "不需要回复", "无需回复", "不用回复", "不要回复",
            "不该回复", "不应该回复", "不需要回应", "无需回应",
            "不用回应", "不该回应", "不应该回应",
        )
        if any(marker in normalized for marker in hard_markers):
            return True

        if "沉默" in normalized and (
            "不确定" in normalized
            or "不是在对" in normalized
            or "没有明确" in normalized
            or "应该" in normalized
        ):
            return True

        if "没有明确" in normalized and (
            "指向我" in normalized
            or "指向你" in normalized
            or "对我说" in normalized
            or "对你说" in normalized
        ):
            return True

        if (
            ("不是在对我" in normalized or "不是在对你" in normalized)
            and ("回复" in normalized or "回应" in normalized or "沉默" in normalized)
        ):
            return True

        return False

    @staticmethod
    def _strip_parenthetical_asides(text: str) -> str:
        """Remove parenthetical asides from an otherwise valid chat reply."""
        if not text:
            return ""
        text = re.sub(r"\s*[（(][^（）()]{1,180}[）)]\s*", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    # Labels a model sometimes puts in front of its reply ("回复：好的").
    _OUTPUT_LABELS = reply_text.OUTPUT_LABELS
    # Characters that end a sentence / a clause when a long reply must be cut.
    _SENTENCE_END = reply_text.SENTENCE_END
    _CLAUSE_BREAK = reply_text.CLAUSE_BREAK

    # Shared with the structured pipeline (src/pipeline/text.py).
    _strip_speaker_prefix = staticmethod(reply_text.strip_speaker_prefix)
    _truncate_reply = staticmethod(reply_text.truncate_at_boundary)

    @staticmethod
    def _clean_chat_reply(text: str, max_chars: int,
                          names: Iterable[str] = ()) -> str:
        """Normalize model output into one short WeChat message or blank.

        ``names`` are the speaker names (persona, bot, requester) whose
        ``name：`` label may be stripped from the front of the reply.
        """
        if not text:
            return ""
        text = str(text).strip()
        if AbstractSummarizer._is_silence_meta_reply(text):
            return ""
        text = AbstractSummarizer._strip_speaker_prefix(text, names).strip()
        text = text.replace("\r", " ").replace("\n", " ").strip()
        text = re.sub(r"\s+", " ", text)
        # Some relay models occasionally leave a dangling closing/opening
        # bracket marker at the end of an otherwise normal reply.
        text = re.sub(r"(?:】【|\[\])+$", "", text).rstrip(" ，,、;；")
        if AbstractSummarizer._is_silence_meta_reply(text):
            return ""
        text = AbstractSummarizer._strip_parenthetical_asides(text)
        if AbstractSummarizer._is_silence_meta_reply(text):
            return ""
        if re.fullmatch(r"[.。…!！?？,，、~～\-—_]+", text):
            return ""
        return AbstractSummarizer._truncate_reply(text, max_chars)

    @staticmethod
    def _format_recent_messages(context_messages: list[dict] | None,
                                limit: int) -> str:
        if not context_messages:
            return "（没有可用的最近聊天记录）"
        lines = []
        for m in context_messages[-limit:]:
            sender = m.get("sender_name", "?")
            content = _sanitize_context_content(m.get("content", ""))
            if content:
                lines.append(f"{sender}: {content}")
        return "\n".join(lines) if lines else "（没有可用的最近聊天记录）"

    def chat(self, message: str,
             context_messages: list[dict] | None = None,
             requester_name: str = "",
             bot_name: str = "群聊小助手",
             group_name: str = "群聊",
             is_group: bool = True,
             group_memory: str = "",
             ambient: bool = False,
             force_reply: bool = False,
             max_chars_override: int | None = None,
             persona_settings: dict | None = None) -> str:
        """Conversational response for a direct request or an ambient feed.

        ``persona_settings`` is the persona mapped to this chat (see
        ``src/persona.py``); without it the configured persona speaks.
        """
        import datetime
        import os

        def _esc(s: str) -> str:
            return str(s or "").replace("{", "{{").replace("}", "}}")

        settings = persona_settings or self._chat_persona_settings()
        max_chars = settings["max_chars"]
        if max_chars_override is not None:
            max_chars = max(8, min(300, int(max_chars_override)))
        recent_messages = self._format_recent_messages(
            context_messages,
            settings["context_count"],
        )
        if is_group and ambient and force_reply:
            chat_scene = f"微信群「{group_name}」里会逐条回复的普通群友"
            scene_rules = f"""\
你会看到这个指定群的每一条新消息，并且每一条可读消息都必须回复。
- 本场景的强制回复要求优先于人设中任何关于自然接话或沉默的描述。
- 不要判断要不要接话，也不要输出空白、沉默或任何“不回复”的说明。
- 消息含义不完整、看不懂或只是表情/图片时，也用{settings["persona_name"]}的短句自然回应或追问，例如“啊？”“哪个”“什么情况”。
- 可以对当前话题表示认同、补充、轻度吐槽、追问或给直接建议；不需要等 @你。
- 不要把逐条回复说出来，也不要声称自己一直在监听群聊。
- 用户引用你的上一句话回复时，要连着看前面的原始请求；必须保留原请求里的硬约束，比如英文/中文、数量、主题、格式。
"""
            memory_display = (
                group_memory if group_memory
                else "（你刚进这个群，还没有形成对这个群的印象）"
            )
            user_instruction = "这是指定群里的一条新消息，必须输出一句自然微信回复。"
            final_instruction = "必须只输出一句自然微信回复，不能输出空白、括号、心理活动或动作描写。"
        elif is_group and ambient:
            chat_scene = f"微信群「{group_name}」里会看到每条新消息的普通群友"
            scene_rules = """\
你会看到这个指定群的每一条新消息，但这不代表每句话都是在叫你。
- 先判断这句话和最近上下文有没有自然的接话点；有才说一句，没有就沉默。
- 其他群友彼此问答、话题已经聊完整、只有无意义刷屏时，不要硬插话。
- 可以对当前话题表示认同、补充、轻度吐槽、追问或给直接建议；不需要等 @你。
- 不要把看到全量消息说出来，也不要声称自己一直在监听群聊。
- 用户引用你的上一句话回复时，要连着看前面的原始请求；必须保留原请求里的硬约束，比如英文/中文、数量、主题、格式。
"""
            memory_display = (
                group_memory if group_memory
                else "（你刚进这个群，还没有形成对这个群的印象）"
            )
            user_instruction = (
                "这是指定群里的一条新消息。根据最近上下文决定自然接话或沉默；"
                "不适合说话就只输出空白。"
            )
            final_instruction = "只输出一句自然微信回复；不适合说话只输出空白；不要输出括号、心理活动或动作描写。"
        elif is_group:
            chat_scene = f"微信群「{group_name}」里的普通群友"
            scene_rules = """\
你看到的是群聊截片，不是单人私聊。先判断最新消息是不是在对你说。
- 明确 @你、叫你的名字、回复你的上一句、向你提问/下指令 → 可以回复。
- 群友在互相说话、评价“她/它/AI/机器人”、吐槽你的表现给别人听 → 多半不是在和你说，回复空白。
- 最新消息只有“？”“哈哈”“笑死”“不要”“嗯”等短反应时，必须结合上一两句判断主语；不确定就沉默，或只轻轻反问一句。
- 不要把别人名字和自己绑定，不要乱叫哥哥/大王，除非上下文明确是对你的新称呼。
- 如果大家正在讨论你回复怪、上下文差、人设乱，你可以承认刚才没接住，但不要装傻重复问“是在说我吗”。
- 用户引用你的上一句话回复时，要连着看前面的原始请求；必须保留原请求里的硬约束，比如英文/中文、数量、主题、格式。
"""
            memory_display = (
                group_memory if group_memory
                else "（你刚进这个群，还没有形成对这个群的印象）"
            )
            user_instruction = (
                "请先判断这是不是在对你说。判断过程不要输出。该沉默就回复空白；该回复就只输出一句自然微信回复。"
            )
            final_instruction = "只输出一句自然微信回复；如果不是在对你说，只输出空白；不要输出括号、心理活动或动作描写。"
        else:
            chat_scene = f"和「{requester_name or group_name}」的一对一微信私聊对象"
            scene_rules = """\
你看到的是一对一微信私聊，不是群聊截片。对方发来的最新消息默认就是在对你说。
- 私聊不需要 @你；只要内容正常，就像真人微信私聊一样自然接话。
- 如果对方在评价 AI/机器人/你的回复，可以轻声承认没接好，但不要说自己是 AI 助手。
- 最新消息很短时，结合上一两句判断；不确定就轻轻反问一句，不要沉默。
- 不要把私聊误判成群友互相说话，也不要提“群里大家”。
"""
            memory_display = (
                group_memory if group_memory
                else "（这段私聊还没有形成长期记忆）"
            )
            user_instruction = "这是私聊，对方是在和你说话。只输出一句自然微信回复，不要输出心理活动或动作描写。"
            final_instruction = "只输出一句自然微信私聊回复；不要输出括号、心理活动或动作描写。"

        system_prompt = self.CHAT_SYSTEM_PROMPT.format(
            bot_name=_esc(bot_name),
            group_name=_esc(group_name),
            chat_scene=_esc(chat_scene),
            sender_name=_esc(requester_name or "群友"),
            persona_name=_esc(settings["persona_name"]),
            persona_prompt=_esc(settings["persona_prompt"]),
            reply_rules=_esc(settings["reply_rules"]),
            scene_rules=_esc(scene_rules),
            max_chars=max_chars,
            current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            recent_messages=_esc(recent_messages),
            current_message=_esc(message),
            group_memory=_esc(memory_display),
            final_instruction=_esc(final_instruction),
        )

        user_prompt = (
            f"最新消息来自 {requester_name or '群友'}：{message}\n"
            f"{user_instruction}"
        )

        self.last_chat_reply_was_intentional_silence = False
        started = time.monotonic()
        try:
            raw_reply = self._retry_with_backoff(
                lambda: self._call_chat_api(
                    system_prompt,
                    [{"role": "user", "content": user_prompt}],
                ),
                "AI chat",
                max_attempts=max(1, int(os.getenv("CHAT_API_MAX_ATTEMPTS", "2") or "2")),
            )
        except Exception as exc:
            self._record_call(
                call="chat", system_prompt=system_prompt, user_prompt=user_prompt,
                raw_reply="", cleaned_reply="", error=repr(exc)[:300],
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                max_chars=max_chars, ambient=ambient, force_reply=force_reply,
                is_group=is_group,
            )
            raise
        cleaned = self._clean_chat_reply(
            raw_reply, max_chars,
            names=(settings["persona_name"], bot_name, requester_name),
        )
        self.last_chat_reply_was_intentional_silence = bool(
            raw_reply and not cleaned and self._is_silence_meta_reply(raw_reply)
        )
        self._record_call(
            call="chat", system_prompt=system_prompt, user_prompt=user_prompt,
            raw_reply=raw_reply, cleaned_reply=cleaned,
            intentional_silence=self.last_chat_reply_was_intentional_silence,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            max_chars=max_chars, ambient=ambient, force_reply=force_reply,
            is_group=is_group,
        )
        return cleaned

    # ── Proactive chat (ambient participation, no @mention) ────────

    PROACTIVE_SYSTEM_PROMPT = """\
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

    def proactive_chat(self, mode, context_messages: list[dict],
                       bot_name: str = "群聊小助手",
                       group_name: str = "群聊",
                       group_memory: str = "",
                       persona_settings: dict | None = None) -> str:
        """Generate a spontaneous chat reply based on conversation context."""
        import datetime

        def _esc(s: str) -> str:
            return str(s or "").replace("{", "{{").replace("}", "}}")

        settings = persona_settings or self._chat_persona_settings()
        max_chars = min(mode.max_chars, settings["max_chars"])
        memory_display = (
            group_memory if group_memory
            else "（你刚进这个群，还没有形成对这个群的印象）"
        )
        limit = min(mode.context_count, settings["context_count"])
        recent_messages = self._format_recent_messages(context_messages, limit)
        if recent_messages.startswith("（没有可用"):
            return ""

        system_prompt = self.PROACTIVE_SYSTEM_PROMPT.format(
            bot_name=_esc(bot_name),
            group_name=_esc(group_name),
            persona_name=_esc(settings["persona_name"]),
            persona_prompt=_esc(settings["persona_prompt"]),
            reply_rules=_esc(settings["reply_rules"]),
            mode_label=mode.label,
            mode_description=mode.description,
            mode_instruction=mode.instruction,
            max_chars=max_chars,
            current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            recent_messages=_esc(recent_messages),
            group_memory=_esc(memory_display),
        )

        user_prompt = "先判断是否适合插话。适合就发一句；不适合就回复空白。"

        started = time.monotonic()
        try:
            reply = self._retry_with_backoff(
                lambda: self._call_chat_api(
                    system_prompt,
                    [{"role": "user", "content": user_prompt}],
                ),
                "proactive chat",
            )
            cleaned = self._clean_chat_reply(
                reply, max_chars, names=(settings["persona_name"], bot_name),
            )
            self._record_call(
                call="proactive_chat", system_prompt=system_prompt,
                user_prompt=user_prompt, raw_reply=reply, cleaned_reply=cleaned,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                max_chars=max_chars, mode=getattr(mode, "name", ""),
            )
            return cleaned
        except RuntimeError as e:
            logger.warning("Proactive chat API call failed: %s", e)
            self._record_call(
                call="proactive_chat", system_prompt=system_prompt,
                user_prompt=user_prompt, raw_reply="", cleaned_reply="",
                error=repr(e)[:300],
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                max_chars=max_chars, mode=getattr(mode, "name", ""),
            )
            return ""

    # ── Structured decisions (pipeline v2) ─────────────────────────

    # Set by configure_pipeline() from PIPELINE_REPAIR / PIPELINE_JSON_MODE /
    # PIPELINE_REWRITE_LONG.
    pipeline_repair: bool = True
    json_mode: bool = True
    pipeline_rewrite_long: bool = False
    # Remembered after a provider refused its native JSON mode once.
    _json_mode_unsupported: bool = False
    # LOCAL_TASKS=decide (src/summarize/__init__.py:create_models): the model
    # that answers the "should I speak?" step of the two-step strategy.
    decide_model: "AbstractSummarizer | None" = None

    def configure_pipeline(self, *, repair: bool = True, json_mode: bool = True,
                           rewrite_long: bool = False) -> None:
        """Attach the structured-pipeline switches loaded from BotConfig."""
        self.pipeline_repair = bool(repair)
        self.json_mode = bool(json_mode)
        self.pipeline_rewrite_long = bool(rewrite_long)

    def _call_json_api(self, system_prompt: str, messages: list[dict], *,
                       schema: dict | None = None,
                       max_tokens: int | None = None) -> ModelOutput:
        """One chat call whose answer should be a JSON envelope.

        The default relies on the prompt alone, which works with any backend;
        backends override it to switch on the provider's JSON mode and to
        report token usage.  ``schema`` and ``max_tokens`` only matter to
        those overrides (see ``complete_json``).
        """
        return ModelOutput(text=self._call_chat_api(system_prompt, messages))

    def complete_json(self, system_prompt: str, user_prompt: str, *,
                      schema: dict | None = None,
                      max_tokens: int | None = None,
                      call_name: str = "json_task") -> ModelOutput:
        """One background JSON task (memory extraction); transport errors propagate.

        Unlike ``decide_and_reply`` this is not a chat turn: no persona, a
        caller-chosen schema and output budget.  The call is logged like the
        others, so its cost shows up next to the chat calls.
        """
        started = time.monotonic()
        messages = [{"role": "user", "content": user_prompt}]
        try:
            output = self._retry_with_backoff(
                lambda: self._call_json_api(
                    system_prompt, messages, schema=schema, max_tokens=max_tokens,
                ),
                call_name, max_attempts=2,
            )
        except Exception as exc:
            self._record_call(
                call=call_name, system_prompt=system_prompt,
                user_prompt=user_prompt, raw_reply="", cleaned_reply="",
                error=repr(exc)[:300],
                latency_ms=round((time.monotonic() - started) * 1000, 1),
            )
            raise
        self._record_call(
            call=call_name, system_prompt=system_prompt, user_prompt=user_prompt,
            raw_reply=str(output.text or ""), cleaned_reply="",
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            usage=output.usage, native_json=bool(output.native_json),
        )
        return output

    def decide_and_reply(self, request: ChatRequest, *,
                         strategy: str = STRATEGY_SINGLE) -> Decision:
        """Decide whether to speak and what to say (see src/pipeline/engine.py)."""
        from src.pipeline.engine import decide

        return decide(self, request, strategy=strategy)

    @abstractmethod
    def _call_chat_api(self, system_prompt: str,
                        messages: list[dict]) -> str:
        """Execute the chat API call. Backend-specific.

        Claude backend: uses client.messages.create() with system param.
        DeepSeek backend: uses client.chat.completions.create() with
                          system role in messages list.
        """
        ...

    # ── Public API ─────────────────────────────────────────────────

    def summarize(self, messages: list[dict],
                  requester_name: str) -> SummaryResult:
        """Generate a structured summary from a list of chat messages.

        Strategy:
          - ≤200 messages        → direct (single call)
          - 201~2000 messages    → map-reduce (chunks → merge)
          - >2000 messages       → multi-level map-reduce (chunks → batches → merge)
        """
        if not messages:
            return SummaryResult(
                summary_text="没有找到新消息。",
                topics=[],
                participants=[],
            )

        estimated = self._estimate_tokens(messages)
        logger.info(
            "[%s] Summarizing %d messages (est. %s tokens, budget=%s)",
            self.__class__.__name__, len(messages),
            f"{estimated:,}", f"{self.token_budget:,}",
        )

        if estimated <= self.token_budget:
            logger.info("Using direct summarization")
            return self._summarize_direct(messages, requester_name)

        chunks = self._split_into_chunks(messages)
        if len(chunks) <= self.merge_batch_size:
            logger.info(
                "Using map-reduce: %d chunks of ~%d messages each",
                len(chunks), self.chunk_size,
            )
            return self._summarize_map_reduce(chunks, requester_name)

        logger.info(
            "Using multi-level map-reduce: %d chunks of ~%d messages each "
            "→ batches of %d",
            len(chunks), self.chunk_size, self.merge_batch_size,
        )
        return self._multi_level_map_reduce(chunks, requester_name)

    def _multi_level_map_reduce(self, chunks: list[list[dict]],
                                 requester_name: str) -> SummaryResult:
        """Handle very large conversations with multi-level merging.

        Level 1 (Map):    Summarize every chunk → chunk_summaries
        Level 2 (Batch):  Group chunk_summaries into batches of merge_batch_size,
                          merge each batch → batch_summaries
        Level 3 (Final):  If >1 batch summary remains, merge them → final result
        """
        total = len(chunks)
        chunk_summaries: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            logger.info("Map phase: chunk %d/%d (%d messages)", i, total, len(chunk))
            summary = self._summarize_chunk(chunk, i, total, requester_name)
            chunk_summaries.append(summary)

        if not chunk_summaries:
            return SummaryResult(
                summary_text="无法生成总结。",
                topics=[],
                participants=[],
            )

        # Level 2: Batch merge
        batches = [
            chunk_summaries[j:j + self.merge_batch_size]
            for j in range(0, len(chunk_summaries), self.merge_batch_size)
        ]
        logger.info(
            "Reduce: merging %d chunk summaries in %d batches",
            len(chunk_summaries), len(batches),
        )
        batch_summaries: list[str] = []
        for b, batch in enumerate(batches, 1):
            summary = self._merge_chunk_summaries(
                batch, f"{requester_name}（第{b}/{len(batches)}批）"
            )
            batch_summaries.append(summary.summary_text)

        # Level 3: Final merge
        if len(batch_summaries) == 1:
            return self._merge_chunk_summaries(batch_summaries, requester_name)

        logger.info("Final merge: %d batch summaries", len(batch_summaries))
        return self._merge_chunk_summaries(batch_summaries, requester_name)

    def format_summary_for_reply(self, result: SummaryResult,
                                  requester_name: str) -> str:
        """Format a SummaryResult into a WeChat reply.

        Trusts the AI's output formatting — no forced renumbering.
        The new detailed system prompt already instructs the model
        to produce well-structured summaries with numbered topics.
        """
        parts = [f"@{requester_name} 你错过的：", ""]

        # Use the summary_text directly — AI is instructed to format it well
        if result.summary_text:
            parts.append(result.summary_text.strip())

        # Fallback: if AI gave topics list instead
        if result.topics and not result.summary_text:
            for i, t in enumerate(result.topics, 1):
                parts.append(f"{i}. {t}")

        return "\n".join(parts)

    # ── Abstract methods ──────────────────────────────────────────

    @abstractmethod
    def _summarize_direct(self, messages: list[dict],
                           requester_name: str) -> SummaryResult:
        """Summarize all messages in a single call."""
        ...

    def _summarize_map_reduce(self, chunks: list[list[dict]],
                               requester_name: str) -> SummaryResult:
        """Summarize by splitting into chunks, extracting per chunk,
        then merging.

        Calls self._summarize_chunk() and self._merge_chunk_summaries(),
        both of which are abstract and backend-specific.
        """
        total = len(chunks)

        chunk_summaries: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            logger.info(
                "Map phase: chunk %d/%d (%d messages)", i, total, len(chunk)
            )
            summary = self._summarize_chunk(chunk, i, total, requester_name)
            chunk_summaries.append(summary)

        if not chunk_summaries:
            return SummaryResult(
                summary_text="无法生成总结。",
                topics=[],
                participants=[],
            )

        logger.info("Reduce phase: merging %d chunk summaries", len(chunk_summaries))
        return self._merge_chunk_summaries(chunk_summaries, requester_name)

    @abstractmethod
    def _summarize_chunk(self, chunk: list[dict], chunk_num: int,
                         total: int, requester_name: str) -> str:
        """Summarize a single chunk into plain text.

        Used by both _summarize_map_reduce and _multi_level_map_reduce.
        """
        ...

    @abstractmethod
    def _merge_chunk_summaries(self, chunk_summaries: list[str],
                                requester_name: str) -> SummaryResult:
        """Merge chunk summaries into a final SummaryResult."""
        ...

    @abstractmethod
    def consolidate_memory(self, existing_memory: str,
                           new_messages: list[dict]) -> str:
        """Update group memory by incorporating new messages.

        Must be implemented by every backend so that memory consolidation
        works regardless of which AI provider is configured.

        Args:
            existing_memory: Current memory text (empty string if first time).
            new_messages: List of new message dicts to incorporate.

        Returns:
            Updated first-person diary-style memory text, or existing_memory
            unchanged on failure.
        """
        ...

    # ── Shared helpers ────────────────────────────────────────────

    def _split_into_chunks(self, messages: list[dict]) -> list[list[dict]]:
        """Split messages into roughly equal-sized chunks."""
        chunks = []
        for i in range(0, len(messages), self.chunk_size):
            chunks.append(messages[i:i + self.chunk_size])
        return chunks

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        """Estimate total token count for a list of messages.

        Conservative heuristic: ~1.5 characters per token (Chinese-heavy text),
        plus XML overhead (~40 chars per message), plus system prompt (~500).
        """
        total_chars = 0
        for msg in messages:
            sender = msg.get("sender_name", "")
            content = msg.get("content", "")
            total_chars += len(sender) + len(content) + 40
        return int(total_chars / 1.5) + 500

    def _retry_with_backoff(self, call_fn: Callable[[], T],
                             label: str,
                             max_attempts: int | None = None) -> T:
        """Execute call_fn with retry + exponential backoff.

        Args:
            call_fn: Zero-argument callable that makes the API request.
            label: Human-readable label for logging.

        Returns:
            The return value of call_fn().

        Raises:
            RuntimeError: If all retries are exhausted.
        """
        last_error: Exception | None = None
        attempts = max(1, int(max_attempts or self.max_retries))
        for attempt in range(1, attempts + 1):
            try:
                result = call_fn()
                self.last_api_call_time = time.time()
                return result
            except self.retry_exceptions as e:
                last_error = e
                if attempt >= attempts:
                    break
                wait = 2 ** attempt
                logger.warning(
                    "Transient error on '%s' (attempt %d/%d). "
                    "Waiting %ds... (%s)",
                    label, attempt, attempts, wait, e,
                )
                time.sleep(wait)

        raise RuntimeError(
            f"Failed after {attempts} attempt(s) on '{label}': "
            f"{last_error}"
        )
