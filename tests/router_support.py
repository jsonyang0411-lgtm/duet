"""A real MessageRouter on an in-memory database, with a trace that remembers what it was told.

Shared by the route tests (test_routing_steps.py, test_router_business.py, test_relay_only.py).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.admin import AdminCommandHandler
from src.db import MessageStore, initialize_db
from src.router import MessageRouter

ADMIN = "wxid_admin"
BOT = "商务助手"
GROUP = "room1@chatroom"
NOW = 1_760_000_000


class TraceSpy:
    """The router's tracer and every message's trace at once: it remembers what it was told.

    ``reasons`` and ``steps`` collect over all messages; ``notes`` and ``reply`` are the
    last message's (``begin`` clears the notes).
    """

    def __init__(self):
        self.steps: list[str] = []
        self.reasons: list[str] = []
        self.ignored: list[str] = []
        self.notes: dict = {}
        self.redacted = None
        self.reply = "not finished"
        self.error = None
        self.begun = 0

    # the tracer
    def begin(self, msg):
        self.begun += 1
        self.notes = {}
        self.reply = "not finished"
        return self

    def current(self):
        return self

    # the trace
    def step(self, code, **info):
        self.steps.append(code)

    def reason(self, code, **info):
        self.reasons.append(code)

    def ignore(self, code, **info):
        self.ignored.append(code)
        self.reasons.append(code)

    def note(self, **info):
        self.notes.update(info)

    def redact(self, content):
        self.redacted = content

    def finish_reply(self, reply):
        self.reply = reply

    def finish_error(self, exc):
        self.error = exc


def group_msg(message_id, content, *, chat_id=GROUP, sender_id="wxid_member", sender_name="群友", **extra):
    msg = {
        "message_id": message_id, "chat_id": chat_id, "group_name": "测试群", "sender_id": sender_id,
        "sender_name": sender_name, "content": content, "msg_type": 1, "timestamp": NOW,
        "is_at_mentioned": False, "is_group": True,
    }
    msg.update(extra)
    return msg


def private_msg(message_id, content, *, sender_id="wxid_stranger", sender_name="陌生人", **extra):
    msg = {
        "message_id": message_id, "chat_id": sender_id, "group_name": sender_name, "sender_id": sender_id,
        "sender_name": sender_name, "content": content, "msg_type": 1, "timestamp": NOW,
        "is_at_mentioned": False, "is_group": False,
    }
    msg.update(extra)
    return msg


def admin_msg(message_id, content, **extra):
    return private_msg(message_id, content, sender_id=ADMIN, sender_name="管理员", **extra)


def make_router(admin=None, **config):
    """``(router, trace)``: a router with a real store and (unless given) a real admin handler."""
    cfg = SimpleNamespace(bot_display_name=BOT, admin_wxid=ADMIN)
    for key, value in config.items():
        setattr(cfg, key, value)
    trace = TraceSpy()
    router = MessageRouter(
        store=MessageStore(initialize_db(":memory:")),
        admin_handler=admin if admin is not None else AdminCommandHandler(),
        config=cfg, tracer=trace,
    )
    return router, trace


def stored(router, message_id) -> int:
    return router._store.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE message_id = ?", (message_id,)).fetchone()[0]


class RouterCase(unittest.TestCase):
    """``self.router()`` builds a router; ``self.trace`` is its TraceSpy."""

    def router(self, admin=None, **config):
        router, self.trace = make_router(admin, **config)
        return router

    stored = staticmethod(stored)
