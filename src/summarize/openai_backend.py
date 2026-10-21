"""OpenAI summarization backend.

Uses OpenAI-compatible Chat Completions over raw HTTP so official OpenAI and
OpenAI-compatible relay services behave the same way.
"""

import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx

from src.pipeline.types import ModelOutput

from .base import AbstractSummarizer
from .deepseek_backend import STORE_SUMMARY_TOOL
from .models import SummaryResult
from .prompts import (
    SYSTEM_PROMPT,
    CHUNK_SYSTEM_PROMPT,
    MERGE_SYSTEM_PROMPT,
    MEMORY_CONSOLE_PROMPT,
    build_summary_prompt,
    build_chunk_summary_prompt,
    build_merge_prompt,
)
from src.vision import image_to_data_url

logger = logging.getLogger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"
CHAT_TIMEOUT_SEC = float(os.getenv("OPENAI_CHAT_TIMEOUT_SEC", "25.0") or "25.0")
DEFAULT_TIMEOUT_SEC = 45.0
MEMORY_TIMEOUT_SEC = 25.0
VISION_TIMEOUT_SEC = float(os.getenv("OPENAI_VISION_TIMEOUT_SEC", "35.0") or "35.0")
JSON_MAX_TOKENS = 600     # a 300-character reply is up to ~600 tokens


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """The answer without a reasoning model's ``<think>…</think>`` part.

    Also handles a template that opens the block in the prompt (only
    ``</think>`` in the answer: everything before it goes) and an answer cut
    off while still thinking (an unclosed ``<think>``: everything after it goes).
    """
    text = _THINK_BLOCK.sub("", str(text or ""))
    lowered = text.lower()
    if "</think>" in lowered:
        text = text[lowered.rindex("</think>") + len("</think>"):]
        lowered = text.lower()
    if "<think>" in lowered:
        text = text[:lowered.index("<think>")]
    return text.strip()


def _is_official_openai_base(base_url: str) -> bool:
    host = urlparse(base_url or OPENAI_BASE_URL).netloc.lower()
    return host in {"api.openai.com", "www.api.openai.com"}


def _extract_text(response_json: dict) -> str:
    choices = response_json.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _parse_summary_response(response_json: dict) -> SummaryResult:
    choices = response_json.get("choices") or []
    message = ((choices[0] or {}).get("message") or {}) if choices else {}

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        function = (tool_calls[0] or {}).get("function") or {}
        args_json = function.get("arguments") or "{}"
        data = json.loads(args_json)
        participants = []
        for p in data.get("participants", []):
            if isinstance(p, dict):
                participants.append(p)
            elif isinstance(p, str):
                participants.append({"name": p, "contributions": ""})
        return SummaryResult(
            summary_text=data.get("summary_text", ""),
            topics=data.get("topics", []),
            participants=participants,
        )

    content = _extract_text(response_json).strip()
    if content:
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "summary_text" in data:
                return SummaryResult(**{
                    k: v for k, v in data.items()
                    if k in ("summary_text", "topics", "participants")
                })
        except (json.JSONDecodeError, Exception):
            pass
        return SummaryResult(summary_text=content[:2000], topics=[], participants=[])

    raise RuntimeError("OpenAI-compatible backend returned empty response")


class OpenAISummarizer(AbstractSummarizer):
    """Summarization and persona chat via OpenAI models."""

    MODEL_BALANCED = "gpt-5.6-terra"

    # Keep this conservative for group-chat cost and latency. Larger OpenAI
    # contexts can still be handled through the existing map-reduce path.
    token_budget = 120_000

    retry_exceptions = (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
        httpx.HTTPStatusError,
    )

    # Set by create_local_summarizer for a model on your own machine
    # (MODEL_ROUTE): extra request fields (e.g. switching thinking off), the
    # <think> part removed from answers, and more tokens and time per call,
    # because a local model is slower and may still think.  The defaults leave
    # every request exactly as before.
    extra_body: dict | None = None
    strip_reasoning: bool = False
    token_floor: int = 0
    timeout_floor: float = 0.0
    # True for the model on your own machine (create_local_summarizer).
    is_local: bool = False

    def __init__(
        self,
        api_key: str,
        model: str = MODEL_BALANCED,
        base_url: str = "",
        chunk_size: int = 400,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.model = model
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        proxy = os.getenv("OPENAI_PROXY", "").strip()
        if not proxy and _is_official_openai_base(self.base_url):
            proxy = (
                os.getenv("HTTPS_PROXY", "").strip()
                or os.getenv("HTTP_PROXY", "").strip()
                or os.getenv("ALL_PROXY", "").strip()
            )
        self.client = httpx.Client(
            proxy=proxy or None,
            timeout=DEFAULT_TIMEOUT_SEC,
            trust_env=False,
        )

    def _create_chat_completion(
        self,
        messages: list[dict],
        max_tokens: int,
        timeout: float | None = None,
        **kwargs,
    ):
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max(int(max_tokens), int(self.token_floor or 0)),
        }
        payload.update(self.extra_body or {})
        payload.update(kwargs)
        if self.timeout_floor:
            timeout = max(float(timeout or DEFAULT_TIMEOUT_SEC), float(self.timeout_floor))
        response = self.client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        if response.status_code in (429,) or response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            error_text = response.text[:500]
            raise RuntimeError(
                f"OpenAI-compatible API error {response.status_code}: {error_text}"
            )
        data = response.json()
        if self.strip_reasoning and isinstance(data, dict):
            for choice in data.get("choices") or []:
                message = (choice or {}).get("message") if isinstance(choice, dict) else None
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    message["content"] = strip_reasoning(message["content"])
        return data

    # ── Conversational chat API call (called by base class) ─────

    def _call_chat_api(self, system_prompt: str, messages: list[dict]) -> str:
        api_messages = [{"role": "system", "content": system_prompt}] + messages
        response = self._create_chat_completion(
            api_messages,
            max_tokens=260,
            timeout=CHAT_TIMEOUT_SEC,
        )
        return _extract_text(response)

    def _call_json_api(self, system_prompt: str, messages: list[dict], *,
                       schema: dict | None = None,
                       max_tokens: int | None = None) -> ModelOutput:
        """Chat call for a JSON envelope, with ``response_format: json_object``.

        Relay services do not all support it: a 400/422 answer repeats the call
        without it and turns the mode off for this process.
        """
        api_messages = [{"role": "system", "content": system_prompt}] + list(messages)
        native = self.json_mode and not self._json_mode_unsupported
        extra = {"response_format": {"type": "json_object"}} if native else {}
        try:
            response = self._create_chat_completion(
                api_messages, max_tokens=max_tokens or JSON_MAX_TOKENS, timeout=CHAT_TIMEOUT_SEC,
                **extra,
            )
        except RuntimeError as exc:
            rejected = str(exc).startswith(
                ("OpenAI-compatible API error 400", "OpenAI-compatible API error 422"),
            )
            if not (native and rejected):
                raise
            logger.warning(
                "OpenAI-compatible endpoint rejected response_format (%s); "
                "using prompt-only JSON", exc,
            )
            response = self._create_chat_completion(
                api_messages, max_tokens=max_tokens or JSON_MAX_TOKENS, timeout=CHAT_TIMEOUT_SEC,
            )
            self._json_mode_unsupported = True
            native = False
        usage = response.get("usage") or {}
        return ModelOutput(
            text=_extract_text(response),
            usage={
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            } if usage else None,
            native_json=native,
        )

    def describe_image(
        self,
        image_path: str,
        question: str,
        requester_name: str = "",
        bot_name: str = "",
        max_chars: int = 80,
        max_bytes: int = 5 * 1024 * 1024,
        previous_bot_reply: str = "",
        is_chat_screenshot: bool = False,
        persona_settings: dict | None = None,
    ) -> str:
        """Reply to an explicit image question or a recent image follow-up.

        ``persona_settings`` is the persona mapped to this chat (see
        ``src/persona.py``); without it the configured persona speaks.
        """
        max_chars = max(20, min(200, int(max_chars or 80)))
        data_url = image_to_data_url(image_path, max_bytes=max_bytes, still=self.is_local)
        settings = persona_settings or self._chat_persona_settings()
        persona_name = (persona_settings or {}).get("persona_name") or bot_name or settings["persona_name"]
        requester = requester_name or "对方"
        if previous_bot_reply:
            screenshot_instruction = (
                "这是一张裁剪后的聊天消息区截图，重点看最新的图片或表情包，"
                "不要把截图里的其他文字当成需要回复的新消息。"
                if is_chat_screenshot else ""
            )
            system_prompt = (
                f"你是{persona_name}，正在微信里和人自然聊天。"
                f"你的人设：{settings['persona_prompt']}"
                "对方刚在你说完话后发来了一张图片或表情包，未必是在接你。"
                f"{screenshot_instruction}"
                "先结合图片和你上一句判断它是否明显是在回应你。"
                "若不是在回应你，或看不清而无法判断，必须只输出“无需回复”。"
                "若是在回应你，只输出一句自然微信回复，不要分段、列表、Markdown，"
                "不要输出括号里的心理活动、动作描写、旁白或分析过程。"
                f"最多{max_chars}字。"
            )
            user_text = (
                f"你刚才对{requester}说：“{previous_bot_reply}”\n"
                f"{requester}随后发来这张图片或表情包。请判断并接话。"
            )
        else:
            system_prompt = (
                f"你是{persona_name}，正在微信里和人自然聊天。"
                f"你的人设：{settings['persona_prompt']}"
                "对方明确让你看刚才发的图片或表情包。"
                "只输出一句要发到微信里的回复，不要分段，不要列表，不要Markdown。"
                "不要输出括号里的心理活动、动作描写、旁白或分析过程。"
                f"最多{max_chars}字。"
                "看不清就轻轻说看不太清，不要编。"
            )
            user_text = (
                f"{requester}问：{question}\n"
                "请结合图片内容回答，像真人微信短句一样自然一点。"
            )
        kwargs = {}
        # OPENAI_VISION_MODEL names a cloud model; the local model reads images itself.
        vision_model = "" if self.is_local else os.getenv("OPENAI_VISION_MODEL", "").strip()
        if vision_model:
            kwargs["model"] = vision_model

        def call():
            response = self._create_chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            },
                        ],
                    },
                ],
                max_tokens=220,
                timeout=VISION_TIMEOUT_SEC,
                **kwargs,
            )
            return _extract_text(response)

        raw_reply = self._retry_with_backoff(
            call,
            "image understanding",
            max_attempts=max(
                1,
                int(os.getenv("OPENAI_VISION_MAX_ATTEMPTS", "1") or "1"),
            ),
        )
        return self._clean_chat_reply(
            raw_reply, max_chars,
            names=(settings["persona_name"], bot_name, requester_name),
        )

    # ── Direct summarization ──────────────────────────────────────

    def _summarize_direct(
        self,
        messages: list[dict],
        requester_name: str,
    ) -> SummaryResult:
        user_prompt = build_summary_prompt(messages, requester_name)

        def call():
            response = self._create_chat_completion(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=8192,
                tools=[STORE_SUMMARY_TOOL],
                tool_choice="auto",
            )
            return _parse_summary_response(response)

        return self._retry_with_backoff(call, "direct summarization")

    # ── Map-Reduce ────────────────────────────────────────────────

    def _summarize_chunk(
        self,
        chunk: list[dict],
        chunk_num: int,
        total: int,
        requester_name: str,
    ) -> str:
        user_prompt = build_chunk_summary_prompt(
            chunk, chunk_num, total, requester_name
        )

        def call():
            response = self._create_chat_completion(
                [
                    {"role": "system", "content": CHUNK_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1024,
            )
            return _extract_text(response)

        return self._retry_with_backoff(call, f"chunk {chunk_num}/{total}")

    def _merge_chunk_summaries(
        self,
        chunk_summaries: list[str],
        requester_name: str,
    ) -> SummaryResult:
        user_prompt = build_merge_prompt(chunk_summaries, requester_name)

        def call():
            response = self._create_chat_completion(
                [
                    {"role": "system", "content": MERGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=8192,
                tools=[STORE_SUMMARY_TOOL],
                tool_choice="auto",
            )
            return _parse_summary_response(response)

        return self._retry_with_backoff(call, "merge chunk summaries")

    # ── Memory consolidation ───────────────────────────────────────

    def consolidate_memory(
        self,
        existing_memory: str,
        new_messages: list[dict],
    ) -> str:
        if not new_messages:
            return existing_memory

        msg_lines = []
        for m in new_messages[-200:]:
            sender = m.get("sender_name", "?")
            content = m.get("content", "")
            if content:
                msg_lines.append(f"{sender}: {content}")

        if not msg_lines:
            return existing_memory

        existing_display = (
            existing_memory if existing_memory else "（暂无，这是第一次整理记忆）"
        )
        escaped_memory = existing_display.replace("{", "{{").replace("}", "}}")
        escaped_msgs = "\n".join(msg_lines).replace("{", "{{").replace("}", "}}")

        system_prompt = MEMORY_CONSOLE_PROMPT.format(
            existing_memory=escaped_memory,
            new_messages=escaped_msgs,
        )

        def call():
            response = self._create_chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "请输出更新后的完整记忆日记。"},
                ],
                max_tokens=2048,
                timeout=MEMORY_TIMEOUT_SEC,
            )
            text = _extract_text(response)
            if len(text) > 2000:
                text = text[:2000]
            return text.strip()

        try:
            return self._retry_with_backoff(
                call, "memory consolidation", max_attempts=1,
            )
        except RuntimeError as e:
            logger.warning("Memory consolidation failed: %s", e)
            return existing_memory
