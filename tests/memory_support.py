"""Shared builders for the memory v2 tests (real in-memory SQLite, no mocks)."""

import json
import time

from src.db.schema import initialize_db
from src.db.store import MessageStore
from src.memory.store import MemoryStore

CHAT = "123@chatroom"
T0 = 1_760_000_000          # a fixed "now" for deterministic timestamps


def make_stores(*, index=False):
    conn = initialize_db(":memory:")
    store = MessageStore(conn)
    mstore = MemoryStore(store)
    if index:
        assert mstore.enable_search_index(), mstore.index_error
        while not mstore.backfill_step():
            pass
    return conn, store, mstore


def add_messages(store, rows, *, chat=CHAT, start=T0, step=60, prefix="m"):
    """rows: (sender, text) or (sender, text, msg_type).  Returns message ids."""
    ids = []
    for i, row in enumerate(rows):
        sender, text, *rest = row
        msg_type = rest[0] if rest else 1
        message_id = f"{prefix}{chat}{start}-{i}"
        ok = store.insert_message({
            "message_id": message_id, "chat_id": chat, "sender_id": f"wxid_{sender}",
            "sender_name": sender, "content": text, "msg_type": msg_type,
            "timestamp": start + i * step,
        })
        assert ok
        ids.append(message_id)
    return ids


def add_history(store, rows, *, chat=CHAT, start=T0 - 86400 * 30, step=60):
    store.upsert_history_messages([
        {"message_id": f"h{start}-{i}", "chat_id": chat, "sender_id": f"wxid_{s}",
         "sender_name": s, "content": t, "msg_type": 1, "source": "test",
         "timestamp": start + i * step}
        for i, (s, t) in enumerate(rows)
    ])


class ScriptedJson:
    """A summarizer stand-in whose complete_json returns scripted text."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete_json(self, system, user, *, schema=None, max_tokens=None, call_name=""):
        from src.pipeline.types import ModelOutput
        self.calls.append({"system": system, "user": user, "schema": schema,
                           "max_tokens": max_tokens, "call_name": call_name})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if not isinstance(answer, str):
            answer = json.dumps(answer, ensure_ascii=False)
        return ModelOutput(text=answer, usage={"input_tokens": 100, "output_tokens": 20})


def payload(facts=(), episode=None, retire=()):
    return {
        "facts": list(facts),
        "episode": episode or {"summary": "", "topics": [], "evidence": []},
        "retire": list(retire),
    }


def fact(kind, subject, predicate, obj, evidence, confidence=0.9, ttl_days=0):
    return {"kind": kind, "subject": subject, "predicate": predicate, "object": obj,
            "evidence": [{"id": i, "quote": q} for i, q in evidence],
            "confidence": confidence, "ttl_days": ttl_days}
