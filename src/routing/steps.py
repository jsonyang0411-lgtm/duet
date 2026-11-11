"""The business gateway's route: one function per decision, in ``ROUTE_STEPS`` order.

This bot is a business WeChat account's gateway, not a chat partner.  It relays the
allowed people's ``cc`` tasks to Claude Code on the Mac (src/relay), hands the
registered creators' private messages to the PRM copilot or to the Mac business
service (src/prm, src/edge), takes the admin's answers to the drafts told to the admin's
private chat (PRM_WECHAT_REVIEW, src/prm/wechat_review.py), and answers the admin's
account commands in a private chat (src/admin.py).  Each step either ends the route
with ``Stop(value)`` or returns ``None`` to pass the message on.

Whatever no step took ends in ``silence``: every group message, every other private
message.  There is no model to call and no path to one, whatever the settings say.

Steps get the clock from ``router._now()`` and log under the router's logger name.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .context import RouteContext, RouteStep, Stop

logger = logging.getLogger("src.router")

_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")


def self_message(ctx: RouteContext) -> Optional[Stop]:
    """Skip the account's own lines (no loops).

    A message the backend has already checked against the account's wxid
    (``_not_self_by_id``) is not judged by its name; without the wxid the
    configured display name is the test (forgiving: WeChat display names can
    differ by spaces or a suffix).
    """
    if ctx.msg.get("_not_self_by_id"):
        return None
    bot_name = str(getattr(ctx.router._config, "bot_display_name", "") or "").strip()
    sender = str(ctx.msg.get("sender_name", "") or "").strip()
    if bot_name and (sender == bot_name or bot_name in sender):
        ctx.trace.ignore("self_message")
        return Stop(None)
    return None


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


def wechat_review(ctx: RouteContext) -> Optional[Stop]:
    """PRM_WECHAT_REVIEW=on (微信里审批草稿, src/prm/wechat_review.py): the admin's answer to a draft notification in the
    admin's own private chat (quoting it: 1 / 2 / 改 … / 执行; 「发 12」「不发 12」「改 12 …」「执行 12」; a bare 1 / 2 to
    the last notification) is the draft's decision, with one reply saying what came of it.  Before ``persist`` (her
    words quoted, a draft edited: not kept in this PC's chat archive) and before ``claude_relay`` and ``admin_command``:
    such an answer never becomes a cc task, a relay approval or an admin command; a cc or command line is never an
    answer.  Lines of the message that are no answer (a merged burst's cc task, say) go on to the later steps as the
    message, after the answer's reply.  Only ADMIN_WXID in a one-to-one chat; a group, anyone else, a creator's chat,
    and every other line of the admin pass on as before."""
    review = getattr(getattr(ctx.router, "_prm", None), "wechat_review", None)
    if review is None or getattr(review, "on", False) is not True:      # (the switch off: not a step at all)
        return None
    is_command = getattr(getattr(ctx.router, "_admin", None), "is_command", None)
    try:
        matched, reply, rest = review.answer(ctx.msg, is_command=is_command)
    except Exception:
        logger.exception("PRM: the admin's answer to a draft notification could not be handled")
        try:
            matched = review.claims(ctx.msg, is_command=is_command)
        except Exception:
            matched = False
        reply, rest = ("处理这条草稿回复时出错了（详见日志）：请在控制台处理" if matched else None), None
    if not matched:
        return None
    ctx.trace.redact("[管理员在微信里审批草稿，内容不记录]")
    if rest is not None:                               # its other lines: the message for the later steps
        review.tell(reply)
        ctx.msg = {**ctx.msg, "content": rest}
        ctx.trace.step("wechat_review")
        return None
    ctx.trace.reason("wechat_review")
    return Stop(reply)


def persist(ctx: RouteContext) -> Optional[Stop]:
    """Store the message; a duplicate (or a database error) ends the route."""
    router, msg, trace = ctx.router, ctx.msg, ctx.trace
    try:
        stored = router._store.insert_message(msg)
    except Exception:
        logger.exception(
            "Failed to persist message (msg_id=%s, sender=%s)",
            msg.get("message_id", "?"), msg.get("sender_id", "?"),
        )
        stored = False
    if not stored:
        trace.ignore("duplicate_or_store_error")
        return Stop(None)
    trace.step("stored")
    router.messages_processed += 1
    return None


def claude_relay(ctx: RouteContext) -> Optional[Stop]:
    """``cc ...`` in an allowed person's private chat is a job for Claude Code on the Mac
    (CLAUDE_RELAY=on, src/relay): the reply is the receipt, the result comes later."""
    relay = getattr(ctx.router, "_claude_relay", None)
    if relay is None:
        return None
    matched, reply = relay.handle(ctx.msg)
    if not matched:
        return None
    ctx.trace.reason("claude_relay")
    return Stop(reply)


def prm(ctx: RouteContext) -> Optional[Stop]:
    """PRM_COPILOT=on: a registered creator's private message goes, silently, into a
    draft job for the Mac (src/prm); the draft waits in the console.  Nothing is
    ever said to the creator here, so the route ends without a reply.  After
    ``claude_relay``: an admin's cc stays the relay's.

    The groups the copilot takes: a 老板拉的群 it took over (PRM_BOSS_GROUPS,
    src/prm/boss_groups.py).  Every line there ends here, silently: hers becomes a draft
    job like a private one, the boss's and the other members' are kept for her next
    draft.  And her own 三方群 (the one 「拉三方群」 made, 2026-11-02): read only, every line
    ends here, silently, kept for her private drafts; never a draft job.  Every other group
    passes on to silence."""
    service = getattr(ctx.router, "_prm", None)
    if service is None:
        return None
    group = ctx.msg.get("is_group", True)
    try:
        handled = service.handle(ctx.msg)
    except Exception:
        logger.exception("PRM: could not take a creator's message; it is not answered")
        chat = ctx.msg.get("chat_id", "")
        handled = service.is_party_group(chat) if group else service.is_party_chat(chat)
    if not handled:
        return None
    # phone numbers, addresses: not in the log
    if not group:
        ctx.trace.redact("[已登记达人的私聊，内容不记录]")
    elif service.is_party_group(ctx.msg.get("chat_id", "")):
        ctx.trace.redact("[老板拉的群（已接手），内容不记录]")
    else:                                              # her own 三方群: read only, its lines kept for her private drafts
        ctx.trace.redact("[她的三方群（只读），内容不记录]")
    ctx.trace.reason("prm_copilot")
    return Stop(None)


def admin_command(ctx: RouteContext) -> Optional[Stop]:
    """The admin's account commands (friends, help) in the admin's private chat.

    Only ``ADMIN_WXID`` in a one-to-one chat; a group is never answered, an @ there
    included.  Text that is not a command passes on (to silence).  The handler gets the
    message too (合作单指令, PRM_WECHAT_COMMANDS: typed text only)."""
    router, msg = ctx.router, ctx.msg
    admin = str(getattr(router._config, "admin_wxid", "") or "")
    handler = getattr(router, "_admin", None)
    if msg.get("is_group", True) or not admin or msg.get("sender_id") != admin or handler is None:
        return None
    content = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
    if not content:
        return None
    reply = handler.handle(content, msg=msg)
    if not reply:
        return None
    ctx.trace.reason("admin_command")
    return Stop(reply)


def silence(ctx: RouteContext) -> Optional[Stop]:
    """Everything else ends here without a word: any group message, a private message
    that is not a relay task, a creator's or the admin's command.  Not a setting."""
    ctx.trace.ignore("silent_end")
    return Stop(None)


ROUTE_STEPS: tuple[RouteStep, ...] = (
    RouteStep("self_message", self_message, "the account's own lines are never handled"),
    RouteStep("edge", edge, "a listed creator's message goes to the Mac (PRM_EDGE), not stored, not answered"),
    RouteStep("wechat_review", wechat_review,
              "the admin's answer to a draft notification (PRM_WECHAT_REVIEW), private chat only, not stored"),
    RouteStep("persist", persist, "store the message; duplicates stop here"),
    RouteStep("claude_relay", claude_relay, "cc ... from an allowed private chat goes to the Mac"),
    RouteStep("prm", prm, "a registered creator's message: a silent draft job, the console decides"),
    RouteStep("admin_command", admin_command, "the admin's account commands, private chat only"),
    RouteStep("silence", silence, "everything else: no reply, no model"),
)


def run_route(router, msg: dict, trace, steps: tuple[RouteStep, ...] = ROUTE_STEPS) -> Optional[str]:
    """Run ``steps`` in order until one ends the route; return what it returned."""
    ctx = RouteContext(router=router, msg=msg, trace=trace)
    for step in steps:
        outcome = step.run(ctx)
        if outcome is not None:
            trace.note(ended_by=step.name)
            return outcome.value
    trace.ignore("silent_end")
    return None
