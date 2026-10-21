"""Plain data passed between the router, the engine and the summarizers."""

from dataclasses import dataclass, field
from typing import Any

from .addressing import Addressing, LEVEL_AMBIENT

STRATEGY_OFF = "off"
STRATEGY_SINGLE = "single"
STRATEGY_TWO_STEP = "two_step"
STRATEGIES = (STRATEGY_OFF, STRATEGY_SINGLE, STRATEGY_TWO_STEP)

STATUS_OK = "ok"                    # parsed first time
STATUS_REPAIRED = "repaired"        # parsed after one repair call
STATUS_EMPTY = "empty"              # the model returned nothing, twice
STATUS_PARSE_FAILED = "parse_failed"  # still not an envelope after the repair call


@dataclass
class ModelOutput:
    """What one JSON-mode model call returned."""

    text: str
    usage: dict | None = None           # {"input_tokens": n, "output_tokens": n}
    native_json: bool = False           # the provider's JSON mode was used


@dataclass(frozen=True)
class ChatRequest:
    """Everything the model needs to decide about one message."""

    message: str                        # what to answer (quote/search context included)
    context_messages: tuple = ()        # recent group messages, oldest first
    requester_name: str = ""
    bot_name: str = ""
    group_name: str = "群聊"
    is_group: bool = True
    group_memory: str = ""
    addressing: Addressing = field(default_factory=lambda: Addressing(LEVEL_AMBIENT))
    ambient: bool = False               # not called: full-feed or proactive path
    force_reply: bool = False           # the group wants a reply to every message
    mode: Any = None                    # ProactiveMode on the proactive path
    max_chars_override: int | None = None
    recent_bot_replies: tuple = ()      # what the bot said lately, oldest first
    call_name: str = "chat_v2"          # label in the model-call log
    persona: dict | None = None         # settings of the persona mapped to this chat (None: the configured one)
    declare_followup: bool = False      # ask the model whom it waits for (FOLLOWUP_MODE=declared)
    wechat_emoji: tuple = ()            # WeChat emoji codes it may use (WECHAT_EMOJI=on)
    stickers: tuple = ()                # (name, when) pairs it may pick from (SEND_STICKERS=on)
    examples: str = ""                  # rendered example dialogues of the persona (data/examples/)


@dataclass
class Decision:
    """The outcome of one pass through the engine."""

    action: str                         # "reply" | "silent"
    messages: tuple = ()
    addressee: str = "unknown"
    reason: str = ""
    status: str = STATUS_OK
    strategy: str = STRATEGY_SINGLE
    calls: int = 1                      # model calls spent (repairs and retries included)
    warnings: tuple = ()
    error_code: str = ""                # set when the answer could not be used
    follow_up: Any = None               # FollowUp the model declared, if asked for it
    sticker: str = ""                   # name of an offered sticker the model picked

    @property
    def text(self) -> str:
        return " ".join(self.messages)

    @property
    def is_reply(self) -> bool:
        return self.action == "reply" and (bool(self.messages) or bool(self.sticker))

    @property
    def failed(self) -> bool:
        return self.status in (STATUS_EMPTY, STATUS_PARSE_FAILED)

    def as_trace(self) -> dict:
        info = {
            "action": self.action, "addressee": self.addressee,
            "status": self.status, "strategy": self.strategy, "calls": self.calls,
        }
        if self.reason:
            info["reason"] = self.reason
        if self.warnings:
            info["warnings"] = list(self.warnings)
        if self.error_code:
            info["error_code"] = self.error_code
        if self.follow_up is not None:
            info["follow_up"] = {"expect": self.follow_up.expect, "seconds": self.follow_up.seconds}
        if self.sticker:
            info["sticker"] = self.sticker
        return info
