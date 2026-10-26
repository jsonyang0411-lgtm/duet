"""The router's priority chain, one function per decision.

``ROUTE_STEPS`` is the order in which a message is looked at.  Each step either
ends the route with ``Stop(value)`` or returns ``None`` to pass the message on.
The code is the old ``MessageRouter._handle`` cut at its decision points: the
handlers it calls (``_handle_chat``, ``_handle_welcome`` ...) are still the
router's methods, so patching them in tests works as before.

Steps get the clock from ``router._now()`` and log under the router's own logger
name, so fake clocks and log assertions that target ``src.router`` keep working.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..pipeline.addressing import calls_by_name
from ..todo.handler import format_todo_reply
from .context import (
    AT_MENTION_MAX_AGE_SEC, WELCOME_MAX_AGE_SEC, RouteContext, RouteStep, Stop,
)

logger = logging.getLogger("src.router")

_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
HELP_WORDS = ("帮助", "help", "命令")


def _commands_ok(ctx: RouteContext) -> bool:
    """Saying the bot's name (structured pipeline) is a call to chat, not to the
    command handlers: "杨宇靖上次塔罗算得好准" is a comment, not a tarot request."""
    return not ctx.msg.get("_name_call_triggered")


# ── before the message is stored ──────────────────────────────────────────

def self_message(ctx: RouteContext) -> Optional[Stop]:
    """Skip messages from the bot itself (prevents infinite loops).

    The match is forgiving: WeChat display names can differ slightly (extra
    spaces, punctuation, emoji suffixes) from what is in .env.  A message the
    backend has already checked against the account's wxid
    (``_not_self_by_id``) is not judged by its name: a member may be named
    like the bot (a persona named after a real member).
    """
    if ctx.msg.get("_not_self_by_id"):
        return None
    bot_name = ctx.router._config.bot_display_name.strip()
    if bot_name and (
        ctx.msg["sender_name"].strip() == bot_name or bot_name in ctx.msg["sender_name"]
    ):
        ctx.trace.ignore("self_message")
        return Stop(None)
    return None


def persist(ctx: RouteContext) -> Optional[Stop]:
    """Always store the message.  A database error must not block the welcome."""
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    try:
        stored = router._store.insert_message(msg)
    except Exception:
        logger.exception(
            "Failed to persist message (msg_id=%s, sender=%s)",
            msg.get("message_id", "?"), msg.get("sender_id", "?"),
        )
        stored = False

    logger.debug(
        "Router stored=%s is_at=%s msg_id=%.12s",
        stored, msg.get("is_at_mentioned"), str(msg.get("message_id", ""))[:12],
    )
    if not stored:
        # If it's a join event, still try to welcome — don't let a DB write
        # failure suppress the welcome message.
        if msg.get("is_system_join") and router._config.welcome_enabled:
            msg_age = router._now() - msg.get("timestamp", 0)
            if msg_age <= WELCOME_MAX_AGE_SEC:
                trace.reason("welcome", stored=False)
                return Stop(router._handle_welcome(msg))
            logger.info(
                "Ignoring stale join event for '%s' (age=%ds, max=%ds)",
                msg.get("new_member_id", "?"), msg_age, WELCOME_MAX_AGE_SEC,
            )
            trace.step("stale_join_ignored", age_sec=msg_age)
        trace.ignore("duplicate_or_store_error")
        return Stop(None)         # duplicate or DB error: nothing more to do
    trace.step("stored")
    router.messages_processed += 1
    return None


def claude_relay(ctx: RouteContext) -> Optional[Stop]:
    """``cc ...`` in an allowed person's private chat is a job for Claude Code on
    the Mac (CLAUDE_RELAY=on, src/relay): the reply is the receipt, the result
    comes later.  Before the memory hooks: orders are not chat to remember."""
    relay = getattr(ctx.router, "_claude_relay", None)
    if relay is None:
        return None
    matched, reply = relay.handle(ctx.msg)
    if not matched:
        return None
    ctx.trace.reason("claude_relay")
    return Stop(reply)


def edge(ctx: RouteContext) -> Optional[Stop]:
    """PRM_EDGE=on: a listed creator's private message goes to the Mac business service
    (src/edge: an event the Mac pulls) and is never answered here.  Before ``persist``:
    the creator's words are not kept in this PC's chat archive."""
    service = getattr(ctx.router, "_edge", None)
    if service is None:
        return None
    try:
        handled = service.handle(ctx.msg)
    except Exception:
        logger.exception("EDGE: could not take a creator's message; it is not answered")
        handled = not ctx.msg.get("is_group", True) and service.is_party_chat(ctx.msg.get("chat_id", ""))
    if not handled:
        return None
    ctx.trace.redact("[已登记达人的私聊，内容不记录]")
    ctx.trace.reason("prm_edge")
    return Stop(None)


def prm(ctx: RouteContext) -> Optional[Stop]:
    """PRM_COPILOT=on: a registered creator's private message goes, silently, into a
    draft job for the Mac (src/prm); the draft waits in the console.  Nothing is
    ever said to the creator here, so the route ends without a reply.  After
    ``claude_relay`` (an admin's cc stays the relay's), before ``relay_only``."""
    service = getattr(ctx.router, "_prm", None)
    if service is None:
        return None
    try:
        handled = service.handle(ctx.msg)
    except Exception:
        logger.exception("PRM: could not take a creator's message; it is not answered")
        handled = not ctx.msg.get("is_group", True) and service.is_party_chat(ctx.msg.get("chat_id", ""))
    if not handled:
        return None
    ctx.trace.redact("[已登记达人的私聊，内容不记录]")      # phone numbers, addresses: not in the log
    ctx.trace.reason("prm_copilot")
    return Stop(None)


def relay_only(ctx: RouteContext) -> Optional[Stop]:
    """CLAUDE_RELAY_ONLY=on: the bot only relays.  Whatever ``claude_relay`` did not
    take ends here, silently: no reply, no proactive line, no welcome, no memory job,
    no model call.  The message is stored (``persist`` ran) so the logs show it.

    Exception: admin management commands in private chat (e.g. friend manager,
    admin help) are answered so the admin can still control the host bot without
    enabling conversational AI or group responses.
    """
    if getattr(ctx.router._config, "claude_relay_only", "off") != "on":
        return None
    router, msg = ctx.router, ctx.msg
    if not msg.get("is_group", True) and (
        router._config.admin_wxid and msg.get("sender_id") == router._config.admin_wxid
    ):
        raw_content = re.sub(r'^\s*wxid_[a-zA-Z0-9]+:\s*', '', msg.get("content", "")).strip()
        if raw_content and hasattr(router, "_admin") and router._admin is not None:
            admin_reply = router._admin.handle(raw_content, msg.get("sender_name", ""))
            if admin_reply:
                ctx.trace.reason("admin_command_relay_only")
                return Stop(router._finish_visible_reply(msg, admin_reply))

    ctx.trace.ignore("relay_only")
    return Stop(None)


def memory_hooks(ctx: RouteContext) -> Optional[Stop]:
    """Hand the stored message to the memory machinery (never delays the reply)."""
    router, msg = ctx.router, ctx.msg
    memory_v2 = router._memory_v2_for(msg)
    if msg.get("is_group", True) and not memory_v2:
        try:
            router._remember_structured_facts(msg)
        except Exception:
            logger.exception("Alias extraction failed; the message is handled anyway")
    if memory_v2:
        # The window job decides when to read; this is a set insertion.
        router._memory_worker.note_message(msg["chat_id"])
    else:
        # May call the AI API in a thread; must not delay the visible reply.
        router._memory.check_and_consolidate_async(msg["chat_id"])
    return None


# ── routes that never reach the @-mention logic ───────────────────────────

def full_feed(ctx: RouteContext) -> Optional[Stop]:
    """A persona that reads every new message of one specified group.

    Deliberately before the mention, quote, image and proactive gates; every
    other persona and group keeps the route below unchanged.
    """
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    mode = router._full_feed_mode(msg)
    if not mode:
        return None
    trace.step("route_full_feed", mode=mode)
    if mode == "ambient" and not router._should_consider_ambient_turn(msg):
        trace.reason("full_feed_local_skip")
        return Stop(None)
    trace.reason("full_feed_chat")
    return Stop(router._finish_visible_reply(
        msg,
        router._handle_full_feed_chat(msg, force_reply=mode == "always_reply"),
    ))


def quoted_image(ctx: RouteContext) -> Optional[Stop]:
    reply = ctx.router._handle_quoted_image_question(ctx.msg)
    if reply is None:
        return None
    ctx.trace.reason("quoted_image_question")
    return Stop(ctx.router._finish_visible_reply(ctx.msg, reply))


def image_message(ctx: RouteContext) -> Optional[Stop]:
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    if not router._is_image_message(msg):
        return None
    image_reply = router._handle_image_followup(msg)
    if image_reply:
        trace.reason("image_followup")
        return Stop(router._finish_visible_reply(msg, image_reply, open_image_followup=False))
    trace.reason("image_message_no_followup")
    return Stop(None)


def close_image_window(ctx: RouteContext) -> Optional[Stop]:
    """The automatic image window only covers an immediately following image or
    sticker.  A later text message starts a different turn."""
    router, msg = ctx.router, ctx.msg
    if router._image_followup is not None:
        router._image_followup.clear(
            msg["chat_id"], msg["sender_id"], int(msg.get("timestamp", 0) or 0),
        )
    return None


def private_chat(ctx: RouteContext) -> Optional[Stop]:
    if ctx.msg.get("is_group", True):
        return None
    ctx.trace.step("route_private")
    return Stop(ctx.router._handle_private_chat(ctx.msg))


def quote_of_other_member(ctx: RouteContext) -> Optional[Stop]:
    """A quote of another member is strong evidence the text is addressed to
    that person.  Decided before sticky/follow-up/proactive routing so a short
    reply like "厉害" cannot be mistaken for a turn directed at the bot."""
    router, msg = ctx.router, ctx.msg
    if (
        msg.get("quoted_content")
        and not msg.get("is_reply_to_bot")
        and not msg.get("is_at_mentioned")
        and router._config.bot_display_name not in str(msg.get("content", ""))
    ):
        logger.info(
            "Ignoring reply to another member: quoted_sender=%r content=%r",
            msg.get("quoted_sender_name", ""), str(msg.get("content", ""))[:60],
        )
        ctx.trace.reason("quote_of_other_member")
        return Stop(None)
    return None


def welcome(ctx: RouteContext) -> Optional[Stop]:
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    if msg.get("is_system_join") and router._config.welcome_enabled:
        msg_age = router._now() - msg.get("timestamp", 0)
        if msg_age <= WELCOME_MAX_AGE_SEC:
            trace.reason("welcome")
            return Stop(router._handle_welcome(msg))
        logger.info(
            "Ignoring stale join event for '%s' (age=%ds, max=%ds)",
            msg.get("new_member_id", "?"), msg_age, WELCOME_MAX_AGE_SEC,
        )
        trace.step("stale_join_ignored", age_sec=msg_age)
    return None


# ── is this message a call to the bot? ────────────────────────────────────

def detect_trigger(ctx: RouteContext) -> Optional[Stop]:
    """@, sticky, keyword, follow-up listening and (pipeline) name calls all
    decide ``ctx.is_at``; whichever fired is written to the trace as ``route``."""
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    sticky_triggered = False
    if not msg["is_at_mentioned"] and router._sticky is not None:
        sticky_triggered = router._sticky.consume(msg["chat_id"], msg["sender_id"])
        if sticky_triggered:
            msg["_sticky_triggered"] = True
    is_at = msg["is_at_mentioned"] or sticky_triggered
    # Keyword trigger without @mention: treated as a chat-only @mention.
    raw = _QUOTE_PREFIX.sub("", msg["content"])
    kw_match = router._detector.is_trigger(
        content=raw, is_at_mentioned=False, sender_name=msg["sender_name"],
    )
    logger.debug("Keyword route: raw=%r is_at=%s kw_match=%s", raw[:30], is_at, kw_match)
    if not is_at and kw_match:
        is_at = True
        msg["_kw_triggered"] = True  # the keyword counts as an @
        # Sticky listening: the same sender can continue without keyword/@ for a
        # while.  A stale (replayed) keyword message is ignored later, so it
        # must not arm listening for that sender either.
        if router._sticky is not None and (
            router._now() - msg.get("timestamp", 0) <= AT_MENTION_MAX_AGE_SEC
        ):
            router._sticky.register(msg["chat_id"], msg["sender_id"])

    if not is_at and router._followup is not None:
        followup_match = router._followup.match(msg)
        if followup_match is not None:
            is_at = True
            msg["_followup_listen_triggered"] = True
            msg["_followup_listen_reason"] = followup_match.reason
            msg["_followup_bot_reply"] = followup_match.bot_reply

    # Structured pipeline: saying the bot's name is a call even without an @ or
    # a configured keyword; the model decides whether it was meant.
    if (
        not is_at
        and router._pipeline_applies(msg)
        and calls_by_name(raw, router._names_for(msg))
    ):
        is_at = True
        msg["_name_call_triggered"] = True

    trace.note(route=(
        "at" if msg["is_at_mentioned"]
        else "sticky" if sticky_triggered
        else "keyword" if msg.get("_kw_triggered")
        else "followup" if msg.get("_followup_listen_triggered")
        else "name_call" if msg.get("_name_call_triggered")
        else "ambient"
    ))
    logger.debug(
        "Router route: is_at=%s age=%ds content=%.50s",
        is_at, router._now() - msg.get("timestamp", 0), msg.get("content", "")[:50],
    )
    ctx.raw, ctx.is_at, ctx.sticky_triggered = raw, is_at, sticky_triggered
    return None


# ── the @-mention path ───────────────────────────────────────────────────

def mention_gate(ctx: RouteContext) -> Optional[Stop]:
    """Drop stale mentions, cut the @bot out of the text, wake on a bare @."""
    if not ctx.is_at:
        return None
    router, msg, trace = ctx.router, ctx.msg, ctx.trace

    # Ignore stale @mentions, e.g. history replayed on startup when the dedup
    # cache is cold.  WeChat message timestamps are Unix seconds.
    msg_age_sec = router._now() - msg.get("timestamp", 0)
    if msg_age_sec > AT_MENTION_MAX_AGE_SEC:
        logger.info(
            "Ignoring stale @mention from '%s' (age=%ds, max=%ds)",
            msg["sender_name"], msg_age_sec, AT_MENTION_MAX_AGE_SEC,
        )
        trace.reason("stale_mention_ignored", age_sec=msg_age_sec)
        return Stop(None)

    logger.info(
        "Trigger in %s by '%s': %s",
        msg["chat_id"], msg["sender_name"], msg["content"][:80],
    )

    clean_content = msg["content"]
    if router._config.bot_display_name:
        # WeChat @mentions use @wxid<invisible_separator>text.  The separator
        # (U+2005, U+200B, U+FEFF...) is not removed by str.strip(): strip
        # @bot_name and any trailing non-word characters in one pass.
        clean_content = re.sub(
            re.escape(f"@{router._config.bot_display_name}") + r"[^\w]*",
            "", msg["content"],
        ).strip()

    # Strip the reply-quote prefix: replying to a message and then @mentioning
    # the bot makes WeChat prepend "wxid_<replied_user_id>:\n".
    clean_content = _QUOTE_PREFIX.sub("", clean_content)

    # A bare @ visibly wakes the bot, and the user's next message continues
    # without another @.
    if not clean_content.strip():
        if router._sticky is not None:
            router._sticky.register(msg["chat_id"], msg["sender_id"])
        logger.info(
            "Empty @mention from '%s' in %s; replying and keeping sticky active for %ds",
            msg["sender_name"], msg["chat_id"][:20], router._config.sticky_mention_ttl_sec,
        )
        trace.step("empty_mention")
        clean_content = "在吗"
    ctx.clean_content = clean_content
    return None


def feishu_export(ctx: RouteContext) -> Optional[Stop]:
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    if not (
        ctx.is_at and ctx.reply is None and _commands_ok(ctx)
        and router._feishu_export is not None
        and router._feishu_export.is_export_command(ctx.clean_content)
    ):
        return None
    trace.reason("feishu_export")
    try:
        export_msg = dict(msg)
        export_msg["content"] = ctx.clean_content
        result = router._feishu_export.export_recent_chat(export_msg)
        ctx.reply = result.reply_text
    except Exception:
        logger.exception("Manual Feishu knowledge sync failed")
        ctx.reply = (
            f"@{msg['sender_name']} 飞书同步失败了："
            "AI 总结或飞书写入临时不可用，稍后再试一次。"
        )
    return None


def help_command(ctx: RouteContext) -> Optional[Stop]:
    if (
        ctx.is_at and ctx.reply is None and _commands_ok(ctx)
        and ctx.clean_content.strip() in HELP_WORDS
    ):
        ctx.trace.reason("help_command")
        ctx.reply = ctx.router._admin.handle(ctx.clean_content, ctx.msg["sender_name"])
    return None


def run_fun_command(router, msg: dict, clean_content: str, trace) -> tuple[bool, Optional[str]]:
    """Lots and tarot.  ``(True, reply)`` when one of them matched.

    Shared by the group @-path and the private chat; a match replaces whatever
    reply an earlier command produced.
    """
    from ..fun import (
        draw_lots,
        draw_tarot,
        is_lots_command,
        parse_tarot_followup_question,
        parse_tarot_question,
    )
    if is_lots_command(clean_content, router._config.bot_display_name):
        trace.reason("lots_command")
        return True, draw_lots(msg["sender_name"])
    tarot_question = parse_tarot_question(clean_content, router._config.bot_display_name)
    if tarot_question is None and "塔罗" in str(msg.get("_followup_bot_reply", "")):
        tarot_question = parse_tarot_followup_question(clean_content)
    if tarot_question is not None:
        trace.reason("tarot_command")
        return True, draw_tarot(msg["sender_name"], tarot_question)
    return False, None


def fun_commands(ctx: RouteContext) -> Optional[Stop]:
    if ctx.is_at and _commands_ok(ctx) and ctx.router._config.fun_enabled:
        matched, reply = run_fun_command(ctx.router, ctx.msg, ctx.clean_content, ctx.trace)
        if matched:
            ctx.reply = reply
    return None


def admin_command(ctx: RouteContext) -> Optional[Stop]:
    router, msg = ctx.router, ctx.msg
    if ctx.is_at and ctx.reply is None and _commands_ok(ctx) and (
        router._config.admin_wxid and msg["sender_id"] == router._config.admin_wxid
    ):
        ctx.trace.reason("admin_command")
        ctx.reply = router._admin.handle(ctx.clean_content, msg["sender_name"])
    return None


def todo_command(ctx: RouteContext) -> Optional[Stop]:
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    if not (
        ctx.is_at and ctx.reply is None and _commands_ok(ctx)
        and router._todo_handler is not None
        and router._is_todo_group(msg["chat_id"])
    ):
        return None
    is_admin = (
        bool(router._config.admin_wxid) and msg["sender_id"] == router._config.admin_wxid
    )
    try:
        result = router._todo_handler.handle(
            ctx.clean_content, msg["chat_id"], msg["sender_id"], msg["sender_name"], is_admin,
        )
        if result is not None:
            trace.reason("todo_command")
            ctx.reply = format_todo_reply(result, msg["sender_name"])
        # automatic clean-up of finished and deleted items
        router._todo_store.cleanup(
            msg["chat_id"],
            router._config.todo_completed_retention_days,
            router._config.todo_deleted_retention_days,
        )
    except Exception:
        logger.exception("Todo command failed")
    return None


def chat_request(ctx: RouteContext) -> Optional[Stop]:
    """No command took it: the AI answers.  (The summary command is disabled.)"""
    if ctx.is_at and ctx.reply is None and ctx.clean_content:
        ctx.trace.reason("chat_request")
        ctx.reply = ctx.router._handle_chat(ctx.msg, ctx.clean_content)
    return None


# ── everything that is not a call: ambient participation ──────────────────

def ambient(ctx: RouteContext) -> Optional[Stop]:
    """Rate-based proactive participation; closed gate ends the route in silence."""
    if ctx.is_at:
        return None
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    if router._feishu_export is not None:
        try:
            router._feishu_export.maybe_auto_export(msg)
        except Exception:
            logger.exception("Automatic Feishu knowledge sync failed")

    scored = router._scored_gate_for(msg)
    if scored is None:
        should_speak, mode, reason = router._proactive.should_speak(msg)
    else:
        should_speak, mode, reason = router._proactive.should_speak(msg, scored=scored)
    if should_speak and mode is not None:
        trace.step("proactive_gate_open", mode=mode.name, why=reason)
        ctx.reply = router._handle_proactive_chat(msg, mode)
        return None
    trace.reason("proactive_gate_closed", gate=reason)
    return Stop(None)


def finish(ctx: RouteContext) -> Optional[Stop]:
    """Clean the reply, open the follow-up windows, remember it."""
    return Stop(ctx.router._finish_visible_reply(ctx.msg, ctx.reply))


ROUTE_STEPS: tuple[RouteStep, ...] = (
    RouteStep("self_message", self_message, "the bot's own lines are never handled"),
    RouteStep("edge", edge, "a listed creator's message goes to the Mac (PRM_EDGE), not stored, not answered"),
    RouteStep("persist", persist, "store the message; duplicates stop here"),
    RouteStep("claude_relay", claude_relay, "cc ... from an allowed private chat goes to the Mac"),
    RouteStep("prm", prm, "a registered creator's message: a silent draft job, the console decides"),
    RouteStep("relay_only", relay_only, "CLAUDE_RELAY_ONLY: nothing else is answered"),
    RouteStep("memory_hooks", memory_hooks, "memory job / diary trigger"),
    RouteStep("full_feed", full_feed, "a persona that reads every message of a group"),
    RouteStep("quoted_image", quoted_image, "a quoted picture the user asks about"),
    RouteStep("image_message", image_message, "a picture or sticker right after a bot reply"),
    RouteStep("close_image_window", close_image_window, "a text message ends the image window"),
    RouteStep("private_chat", private_chat, "one-to-one chats answer directly"),
    RouteStep("quote_of_other_member", quote_of_other_member, "a reply to someone else is not for the bot"),
    RouteStep("welcome", welcome, "a new member joined"),
    RouteStep("detect_trigger", detect_trigger, "@, sticky, keyword, follow-up, name call"),
    RouteStep("mention_gate", mention_gate, "stale guard, clean text, bare @"),
    RouteStep("feishu_export", feishu_export, "manual Feishu knowledge sync"),
    RouteStep("help_command", help_command, "help / 命令"),
    RouteStep("fun_commands", fun_commands, "lots and tarot"),
    RouteStep("admin_command", admin_command, "the admin's commands"),
    RouteStep("todo_command", todo_command, "group todo list"),
    RouteStep("chat_request", chat_request, "the AI answers"),
    RouteStep("ambient", ambient, "proactive participation or silence"),
    RouteStep("finish", finish, "clean and register the reply"),
)


def run_route(router, msg: dict, trace, steps: tuple[RouteStep, ...] = ROUTE_STEPS) -> Optional[str]:
    """Run ``steps`` in order until one ends the route; return what it returned."""
    ctx = RouteContext(router=router, msg=msg, trace=trace)
    for step in steps:
        outcome = step.run(ctx)
        if outcome is not None:
            trace.note(ended_by=step.name)
            return outcome.value
    return None
