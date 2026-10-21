"""Characterization tests for the message router's priority chain.

Covers src/router.py (``MessageRouter.handle`` and its helpers) with a real
in-memory ``MessageStore``, a recording stub summarizer and a stub nickname
service.  tests/test_router.py already asserts the main happy paths; this file
adds the branches it does not reach: the order of the checks inside
``handle``, the guards (own message, duplicate, stale mention, quote of another
member), the keyword trigger, the command chain (help / fun / admin / todo /
chat), private chats, image routing branches, welcome messages, the Yang Yujing
full-feed branches, the proactive path, the fallback strings and the pure
helpers (``_finish_visible_reply``, ``_history_recall_terms``,
``_get_group_facts_text``, ``_message_for_ai_chat``).

Phase 0 safety net: the tests pin what the router does TODAY -- including the
parts that are known to be wrong -- so that a later refactor shows up as an
intentional diff.  Behaviour that is deliberately NOT endorsed is marked::

    # PINNED (known issue, expected to change in Phase N): <why>

When such behaviour is fixed, edit the expectation in the same change.

Determinism and isolation
-------------------------
* The ``time`` reference of src/router.py, src/db/store.py and the proactive
  trackers is replaced by one fake clock; nothing sleeps.
* ``MemoryConsolidator.check_and_consolidate_async`` is replaced by a call to
  its synchronous twin, so no background thread is ever started (and the check
  below fails a test that leaves one running).
* Every test runs inside a temporary working directory with an empty ``data/``
  folder and ``db_path`` pointing into it, so nothing is written under the
  repository and cwd-relative reads (``data/group_names.json``) see no file.
* The welcome templates come from a manager rooted in that directory, the web
  search client is a stub, and ``src.fun.draw_lots`` / ``draw_tarot`` are
  patched wherever a command is expected to run.
"""

import dataclasses
import json
import logging
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.db.store as store_module
import src.proactive.followup as followup_module
import src.proactive.gate as gate_module
import src.proactive.image_followup as image_followup_module
import src.proactive.sticky as sticky_module
import src.router as router_module
import src.welcome as welcome_module
from src.admin import AdminCommandHandler
from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.memory.consolidator import MemoryConsolidator
from src.persona import PERSONA_PROFILE_YANG_YUJING
from src.proactive.modes import ProactiveMode
from src.router import (
    ALL_MESSAGES_MAX_AGE_SEC,
    AT_MENTION_MAX_AGE_SEC,
    WELCOME_MAX_AGE_SEC,
    AMBIENT_COOLDOWN_SEC,
    MessageRouter,
)
from src.trigger import TriggerDetector
from src.web_search import WebSearchClient
from src.welcome import WelcomeManager

NOW = 1_800_000_000
BOT = "林小满"
CHAT = "chat1@chatroom"
YANG_BOT = "杨宇靖"
YANG_GROUP = "群主应该是pyc"

# Keyword arguments the router passes to ``summarizer.chat`` today.
CHAT_CALL_KEYS = [
    "ambient", "bot_name", "context_messages", "force_reply", "group_memory",
    "group_name", "is_group", "message", "requester_name",
]
IMAGE_CALL_KEYS = [
    "bot_name", "image_path", "max_bytes", "max_chars", "question",
    "requester_name",
]

# These config fields only exist once the decision-trace work lands; they are
# switched off when present so the router never writes trace files.
_QUIET_CONFIG = {"decision_trace_enabled": False, "llm_call_log_enabled": False}
_CONFIG_FIELDS = {field.name for field in dataclasses.fields(BotConfig)}


# ── stubs ──────────────────────────────────────────────────────────────

class FakeClock:
    """Stand-in for the ``time`` module (``time`` and ``monotonic`` only)."""

    def __init__(self, now: float = NOW, mono: float = 1000.0):
        self.now = float(now)
        self.mono = float(mono)

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.mono += seconds


class StubSummarizer:
    """Records every call the router makes; never talks to a model."""

    def __init__(self, reply="嗯，在。"):
        self.reply = reply
        self.proactive_reply = "接一句"
        self.image_reply = "是一张测试图"
        self.error = None
        self.chat_calls = []
        self.proactive_calls = []
        self.image_calls = []
        self.summarize_calls = []
        self.consolidate_calls = []
        self.events = []
        self.last_chat_reply_was_intentional_silence = False

    def chat(self, **kwargs):
        self.events.append("chat")
        self.chat_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.reply

    def proactive_chat(self, **kwargs):
        self.events.append("proactive_chat")
        self.proactive_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.proactive_reply

    def describe_image(self, **kwargs):
        self.events.append("describe_image")
        self.image_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.image_reply

    def summarize(self, messages, requester_name):
        self.events.append("summarize")
        self.summarize_calls.append((list(messages), requester_name))
        return SimpleNamespace(summary_text="S")

    def format_summary_for_reply(self, result, requester_name):
        return f"@{requester_name} 总结：{result.summary_text}"

    def consolidate_memory(self, existing_memory, new_messages):
        self.consolidate_calls.append((existing_memory, list(new_messages)))
        return existing_memory


class SummarizerWithoutVision(StubSummarizer):
    describe_image = None


class StubNicks:
    def __init__(self, names=None):
        self.names = dict(names or {})

    def resolve_name(self, value):
        return self.names.get(value, value)

    def resolve_wxids(self, text):
        for wxid, name in self.names.items():
            text = text.replace(wxid, name)
        return text


class NoWebSearch:
    """Never asks for a search, so the real client is never constructed."""

    reply_max_chars = 120

    def extract_query(self, text, *, bot_name=""):
        return ""

    def search(self, query):  # pragma: no cover - must not run
        raise AssertionError("web search must not run")

    def format_for_prompt(self, query, results):  # pragma: no cover
        raise AssertionError("web search must not run")


class StubWebSearch:
    reply_max_chars = 99

    def __init__(self, *, fail=False):
        self.fail = fail
        self.extract_calls = []
        self.queries = []

    def extract_query(self, text, *, bot_name=""):
        self.extract_calls.append((text, bot_name))
        return "查询词" if "查" in text else ""

    def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("network down")
        return ["result"]

    def format_for_prompt(self, query, results):
        return f"RESULTS[{query}]"


class MinimalStore:
    """The smallest store the router can run against (no optional methods)."""

    def insert_message(self, msg):
        return True

    def get_group_memory(self, chat_id):
        return None

    def get_new_message_count(self, chat_id, since_message_id):
        return 0

    def get_messages_since(self, chat_id, since_ts, until_ts=None, limit=500):
        return []


_STUB = object()   # make_router(web=_STUB) -> NoWebSearch; web=None -> real


class RouterTestCase(unittest.TestCase):
    """Builds routers on a frozen clock, in a throw-away cwd, without threads."""

    def setUp(self):
        threads_before = set(threading.enumerate())

        def assert_no_thread_left_running():
            leftovers = set(threading.enumerate()) - threads_before
            self.assertEqual(leftovers, set(), "test left a thread running")

        self.addCleanup(assert_no_thread_left_running)

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "data").mkdir()
        previous_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, previous_cwd)

        self.clock = FakeClock()
        for module in (
            router_module, store_module, sticky_module, followup_module,
            image_followup_module, gate_module,
        ):
            patcher = patch.object(module, "time", self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)

        # The router asks the consolidator, once per stored message, whether a
        # background consolidation is due.  Run the synchronous twin inline and
        # record the chat ids so no thread is ever started.
        self.consolidation_checks = []

        def check_inline(consolidator, chat_id):
            self.consolidation_checks.append(chat_id)
            return consolidator.check_and_consolidate(chat_id)

        consolidation = patch.object(
            MemoryConsolidator, "check_and_consolidate_async", check_inline,
        )
        consolidation.start()
        self.addCleanup(consolidation.stop)

        # Quiet and fast: nothing in this file asserts on log records.
        previous_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_disable)

        self.welcome_manager = WelcomeManager(self.tmp / "welcome_templates.json")
        welcome = patch.object(
            welcome_module, "get_welcome_manager",
            return_value=self.welcome_manager,
        )
        welcome.start()
        self.addCleanup(welcome.stop)

        # The real lookup walks WeChat's media folders under the user's home
        # directory; a test that reaches it must patch it explicitly.
        def forbid_media_scan(config, media_records):
            raise AssertionError("find_recent_image_path must be patched")

        media_scan = patch.object(
            router_module, "find_recent_image_path", forbid_media_scan,
        )
        media_scan.start()
        self.addCleanup(media_scan.stop)

    # ── builders ──────────────────────────────────────────────────────

    def now(self) -> int:
        return int(self.clock.now)

    def make_config(self, **overrides):
        values = {
            "bot_display_name": BOT,
            "proactive_enabled": False,
            "todo_enabled": False,
            "db_path": str(self.tmp / "messages.db"),
        }
        values.update(
            {k: v for k, v in _QUIET_CONFIG.items() if k in _CONFIG_FIELDS},
        )
        values.update(overrides)
        return BotConfig(**values)

    def new_store(self):
        conn = initialize_db(":memory:")
        self.addCleanup(conn.close)
        return MessageStore(conn)

    def make_router(self, *, store=None, keywords=(), nicks=None, admin=None,
                    feishu=None, web=_STUB, summarizer=None, **config):
        cfg = self.make_config(**config)
        summarizer = summarizer if summarizer is not None else StubSummarizer()
        router = MessageRouter(
            store=store if store is not None else self.new_store(),
            detector=TriggerDetector(list(keywords), cfg.bot_display_name),
            summarizer=summarizer,
            admin_handler=(
                admin if admin is not None
                else MagicMock(handle=MagicMock(return_value=None))
            ),
            nickname_service=nicks if nicks is not None else StubNicks(),
            config=cfg,
            feishu_export_service=feishu,
            web_search_client=NoWebSearch() if web is _STUB else web,
        )
        return router, summarizer

    def group_msg(self, message_id, content, *, at=True, **overrides):
        message = {
            "message_id": message_id,
            "chat_id": CHAT,
            "group_name": "测试群",
            "sender_id": "wxid_user1",
            "sender_name": "小明",
            "content": content,
            "msg_type": 1,
            "timestamp": self.now(),
            "is_at_mentioned": at,
            "is_group": True,
        }
        message.update(overrides)
        return message

    def other_msg(self, message_id, content, *, at=False, **overrides):
        overrides.setdefault("sender_id", "wxid_user2")
        overrides.setdefault("sender_name", "土豆")
        return self.group_msg(message_id, content, at=at, **overrides)

    def private_msg(self, message_id, content, **overrides):
        message = {
            "message_id": message_id,
            "chat_id": "wxid_friend",
            "group_name": "私聊好友",
            "chat_name": "私聊好友",
            "sender_id": "wxid_friend",
            "sender_name": "私聊好友",
            "content": content,
            "msg_type": 1,
            "timestamp": self.now(),
            "is_at_mentioned": False,
            "is_group": False,
        }
        message.update(overrides)
        return message

    def sticker_msg(self, message_id, **overrides):
        overrides.setdefault("msg_type", 47)
        overrides.setdefault("media_path", "/media/sticker.gif")
        return self.group_msg(message_id, "[表情]", at=False, **overrides)

    def join_msg(self, message_id, *, member="小红", **overrides):
        overrides.setdefault("sender_id", "wxid_inviter")
        overrides.setdefault("sender_name", "系统消息")
        overrides.setdefault("is_system_join", True)
        overrides.setdefault("new_member_id", member)
        return self.group_msg(
            message_id, f'"{member}"加入了群聊', at=False, **overrides,
        )

    # ── inspection helpers ────────────────────────────────────────────

    @staticmethod
    def stored_ids(router):
        rows = router._store.conn.execute(
            "SELECT message_id FROM messages ORDER BY id"
        )
        return [row["message_id"] for row in rows]

    def gate(self, router, *, allow=False, mode=None):
        """Patch the proactive gate; returns the patched ``should_speak``."""
        patcher = patch.object(
            router._proactive, "should_speak",
            return_value=(allow, mode, "stub"),
        )
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked


def casual_mode(context_count=3):
    return ProactiveMode(
        name="CASUAL", label="闲聊", description="desc", instruction="instr",
        min_rate=4.0, eval_interval_sec=120, reply_probability=0.25,
        max_chars=50, context_count=context_count,
    )


# ── constants ──────────────────────────────────────────────────────────

class RouterConstantsTests(unittest.TestCase):
    def test_age_limits_and_cooldown(self):
        self.assertEqual(AT_MENTION_MAX_AGE_SEC, 300)
        self.assertEqual(WELCOME_MAX_AGE_SEC, 300)
        self.assertEqual(ALL_MESSAGES_MAX_AGE_SEC, 300)
        self.assertEqual(AMBIENT_COOLDOWN_SEC, 45)


# ── own message, duplicates, storage ───────────────────────────────────

class OwnMessageAndStorageTests(RouterTestCase):
    def test_own_message_is_ignored_before_it_is_stored(self):
        router, summarizer = self.make_router()

        reply = router.handle(
            self.group_msg("own-1", "@林小满 你好", sender_name=BOT),
        )

        self.assertIsNone(reply)
        self.assertEqual(self.stored_ids(router), [])
        self.assertEqual(router.messages_processed, 0)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(self.consolidation_checks, [])

    def test_own_message_guard_matches_a_substring_of_the_display_name(self):
        router, summarizer = self.make_router()

        decorated = router.handle(
            self.group_msg("own-2", "你好", sender_name="  林小满（机器人）  "),
        )
        # PINNED (known issue, expected to change in Phase N): the guard is
        # ``bot_name in sender_name``, so any member whose nickname merely
        # contains the bot's name is silently ignored as well.
        fan = router.handle(
            self.group_msg("fan-1", "@林小满 你好", sender_name="小林小满粉丝"),
        )

        self.assertIsNone(decorated)
        self.assertIsNone(fan)
        self.assertEqual(self.stored_ids(router), [])
        self.assertEqual(summarizer.chat_calls, [])

    def test_empty_bot_name_disables_the_own_message_guard(self):
        router, summarizer = self.make_router(bot_display_name="")

        reply = router.handle(self.group_msg("m1", "你好", sender_name=""))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "你好")

    def test_duplicate_message_is_dropped_without_a_second_reply(self):
        router, summarizer = self.make_router()
        message = self.group_msg("dup-1", "@林小满 你好")

        first = router.handle(message)
        second = router.handle(dict(message))

        self.assertEqual(first, "嗯，在。")
        self.assertIsNone(second)
        self.assertEqual(len(summarizer.chat_calls), 1)
        self.assertEqual(self.consolidation_checks, [CHAT])

    def test_messages_processed_counts_stored_messages_only(self):
        router, _ = self.make_router()

        router.handle(self.group_msg("m1", "a", at=False))
        router.handle(self.group_msg("m1", "a", at=False))       # duplicate
        router.handle(self.group_msg("own", "a", sender_name=BOT))
        router.handle(self.group_msg("m2", "b", at=False))

        self.assertEqual(router.messages_processed, 2)

    def test_store_failure_is_treated_like_a_duplicate(self):
        router, summarizer = self.make_router()

        with patch.object(
            router._store, "insert_message", side_effect=RuntimeError("db down"),
        ):
            reply = router.handle(self.group_msg("m1", "@林小满 你好"))

        self.assertIsNone(reply)
        self.assertEqual(router.messages_processed, 0)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(self.consolidation_checks, [])

    def test_every_stored_message_checks_memory_consolidation(self):
        router, _ = self.make_router()

        router.handle(self.group_msg("m1", "@林小满 你好"))
        router.handle(self.group_msg("m2", "闲聊", at=False, sender_id="wxid_x"))
        router.handle(self.private_msg("p1", "私聊一句"))

        self.assertEqual(
            self.consolidation_checks, [CHAT, CHAT, "wxid_friend"],
        )

    def test_first_message_of_a_chat_triggers_a_consolidation_attempt(self):
        router, summarizer = self.make_router()

        router.handle(self.group_msg("m1", "第一条", at=False))

        # The consolidator measures its time threshold from the epoch when a
        # chat has no memory yet, so the very first stored message already
        # asks the model to consolidate.
        self.assertEqual(len(summarizer.consolidate_calls), 1)
        existing, new_messages = summarizer.consolidate_calls[0]
        self.assertEqual(existing, "")
        self.assertEqual([m["message_id"] for m in new_messages], ["m1"])

    def test_group_messages_query_the_store_for_fact_extraction(self):
        router, _ = self.make_router()
        store = router._store

        with patch.object(
            store, "get_messages_since", wraps=store.get_messages_since,
        ) as since, patch.object(
            store, "upsert_group_fact", wraps=store.upsert_group_fact,
        ) as upsert:
            router.handle(self.group_msg("m1", "今天天气不错", at=False))

        # PINNED (known issue, expected to change in Phase N): every stored
        # group message costs one DB query for fact extraction, even plain
        # chatter that can never contain a fact.
        since.assert_called_once_with(CHAT, NOW - 300, until_ts=NOW, limit=16)
        upsert.assert_not_called()

    def test_fact_extraction_falls_back_to_the_clock_for_a_zero_timestamp(self):
        router, _ = self.make_router()
        store = router._store

        with patch.object(
            store, "get_messages_since", wraps=store.get_messages_since,
        ) as since:
            router.handle(self.group_msg("m1", "x", at=False, timestamp=0))

        since.assert_called_once_with(CHAT, NOW - 300, until_ts=NOW, limit=16)

    def test_private_messages_do_not_run_fact_extraction(self):
        router, _ = self.make_router()
        store = router._store

        with patch.object(
            store, "get_messages_since", wraps=store.get_messages_since,
        ) as since:
            router.handle(self.private_msg("p1", "叫宇肆次元哥哥"))

        # Only the chat-context fetch happens (limit == chat_context_count).
        self.assertEqual(len(since.call_args_list), 1)
        self.assertEqual(since.call_args.kwargs["limit"], 14)
        self.assertEqual(store.get_group_facts("wxid_friend"), [])

    def test_missing_is_group_key_means_group(self):
        router, _ = self.make_router()
        message = self.group_msg("m1", "叫宇肆次元哥哥", at=False)
        del message["is_group"]

        router.handle(message)

        facts = router._store.get_group_facts(CHAT)
        self.assertIn(("宇肆", "次元哥哥"), [(f["subject"], f["object"]) for f in facts])

    def test_routing_flags_are_written_into_the_callers_message_dict(self):
        router, _ = self.make_router(keywords=["总结一下"])
        router.handle(self.group_msg("m0", "@林小满"))   # arms sticky
        sticky = self.group_msg("m1", "继续说", at=False)
        keyword = self.other_msg("m2", "总结一下")
        private = self.private_msg("p1", "你好")

        for message in (sticky, keyword, private):
            router.handle(message)

        # PINNED (known issue, expected to change in Phase N): routing
        # decisions are communicated by mutating the input dict with private
        # "_..." keys that later code (and the summarizer) read back.
        self.assertTrue(sticky["_sticky_triggered"])
        self.assertTrue(keyword["_kw_triggered"])
        self.assertTrue(private["_private_triggered"])

    def test_group_message_without_is_at_mentioned_key_raises(self):
        router, _ = self.make_router()
        message = self.group_msg("m1", "你好")
        del message["is_at_mentioned"]

        # PINNED (known issue, expected to change in Phase N): handle() uses
        # msg["is_at_mentioned"] (not .get) once a text message reaches the
        # sticky stage, so the backend contract is "always set the key".
        with self.assertRaises(KeyError):
            router.handle(message)


# ── stale mention guard ────────────────────────────────────────────────

class StaleMentionGuardTests(RouterTestCase):
    def test_mention_exactly_at_the_age_limit_is_answered(self):
        router, _ = self.make_router()

        reply = router.handle(self.group_msg(
            "m1", "@林小满 你好", timestamp=NOW - AT_MENTION_MAX_AGE_SEC,
        ))

        self.assertEqual(reply, "嗯，在。")

    def test_mention_one_second_past_the_limit_is_stored_but_ignored(self):
        router, summarizer = self.make_router()

        reply = router.handle(self.group_msg(
            "m1", "@林小满 你好", timestamp=NOW - AT_MENTION_MAX_AGE_SEC - 1,
        ))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(self.stored_ids(router), ["m1"])
        self.assertEqual(router.messages_processed, 1)

    def test_stale_empty_mention_does_not_arm_sticky_listening(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)

        reply = router.handle(self.group_msg(
            "m1", "@林小满", timestamp=NOW - AT_MENTION_MAX_AGE_SEC - 1,
        ))
        follow_up = router.handle(self.group_msg("m2", "你好呀", at=False))

        self.assertIsNone(reply)
        self.assertIsNone(follow_up)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(len(router._sticky), 0)

    def test_stale_keyword_message_does_not_arm_sticky_listening(self):
        router, summarizer = self.make_router(
            keywords=["总结一下"], followup_listen_enabled=False,
        )

        stale = router.handle(self.group_msg(
            "m1", "总结一下", at=False, timestamp=NOW - 400,
        ))
        follow_up = router.handle(self.group_msg("m2", "你好呀", at=False))

        # Fixed in Phase 1: a replayed (stale) keyword message is ignored and
        # no longer makes the sender's next, fresh message count as addressed
        # to the bot.
        self.assertIsNone(stale)
        self.assertIsNone(follow_up)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(len(router._sticky), 0)

    def test_fresh_keyword_message_still_arms_sticky_listening(self):
        router, summarizer = self.make_router(
            keywords=["总结一下"], followup_listen_enabled=False,
        )

        first = router.handle(self.group_msg("m1", "总结一下", at=False))
        follow_up = router.handle(self.group_msg("m2", "你好呀", at=False))

        self.assertEqual(first, "嗯，在。")
        self.assertEqual(follow_up, "嗯，在。")
        self.assertIn("连续聊天", summarizer.chat_calls[1]["message"])

    def test_stale_sticky_candidate_is_dropped_and_consumes_the_entry(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        router.handle(self.group_msg("m1", "@林小满"))
        self.assertEqual(len(router._sticky), 1)

        stale = router.handle(self.group_msg(
            "m2", "很久以前的话", at=False, timestamp=NOW - 400,
        ))
        fresh = router.handle(self.group_msg("m3", "现在才说", at=False))

        self.assertIsNone(stale)
        self.assertIsNone(fresh)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_future_timestamp_passes_the_guard(self):
        router, _ = self.make_router()

        reply = router.handle(self.group_msg(
            "m1", "@林小满 你好", timestamp=NOW + 1000,
        ))

        self.assertEqual(reply, "嗯，在。")

    def test_mention_without_timestamp_counts_as_stale(self):
        router, summarizer = self.make_router()
        message = self.group_msg("m1", "@林小满 你好")
        del message["timestamp"]

        reply = router.handle(message)

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_ambient_messages_have_no_age_guard(self):
        router, _ = self.make_router()
        gate = self.gate(router)

        reply = router.handle(self.group_msg(
            "m1", "一天前的闲聊", at=False, timestamp=NOW - 86400,
        ))

        # PINNED (known issue, expected to change in Phase N): only the @/
        # keyword/sticky/follow-up path has a startup-replay guard; replayed
        # old chatter still reaches the proactive gate (and its rate tracker).
        self.assertIsNone(reply)
        gate.assert_called_once()

    def test_private_messages_have_no_age_guard(self):
        router, summarizer = self.make_router()

        reply = router.handle(self.private_msg(
            "p1", "一天前的私聊", timestamp=NOW - 86400,
        ))

        # PINNED (known issue, expected to change in Phase N): the private
        # branch returns before the stale-mention guard, so replayed history
        # is answered.
        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 1)


# ── quote of another member / reply-to-bot flag ───────────────────────

class QuoteGuardTests(RouterTestCase):
    def test_reply_to_bot_flag_alone_does_not_trigger_a_reply(self):
        router, summarizer = self.make_router()

        reply = router.handle(self.group_msg(
            "q1", "都可以", at=False, is_reply_to_bot=True,
            quoted_content="你喜欢哪种风格的呀", quoted_sender_name=BOT,
        ))

        # PINNED (known issue, expected to change in Phase N): the router only
        # reads is_reply_to_bot for prompt wording; the trigger itself relies
        # on the backend also setting is_at_mentioned.
        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_quote_of_another_member_is_dropped_before_sticky_is_consumed(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        router.handle(self.group_msg("m1", "@林小满"))

        quoted = router.handle(self.group_msg(
            "m2", "哈哈", at=False, quoted_content="某句话",
            quoted_sender_name="土豆",
        ))

        self.assertIsNone(quoted)
        self.assertEqual(len(router._sticky), 1)           # still armed
        follow_up = router.handle(self.group_msg("m3", "继续", at=False))
        self.assertEqual(follow_up, "嗯，在。")
        self.assertIn("连续聊天", summarizer.chat_calls[-1]["message"])

    def test_quote_of_another_member_is_dropped_before_keyword_matching(self):
        router, summarizer = self.make_router(keywords=["总结一下"])

        reply = router.handle(self.group_msg(
            "q1", "总结一下", at=False, quoted_content="某句话",
            quoted_sender_name="土豆",
        ))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_bot_name_in_the_text_lets_a_quote_reach_the_ambient_gate(self):
        router, summarizer = self.make_router()
        gate = self.gate(router)

        reply = router.handle(self.group_msg(
            "q1", "林小满，你怎么看", at=False, quoted_content="大概是吧",
            quoted_sender_name="刘俊威",
        ))

        # Without the @ flag the name alone is not a trigger; the message
        # simply gets past the guard and is offered to the proactive gate.
        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])
        gate.assert_called_once()

    def test_private_chat_is_not_subject_to_the_quote_guard(self):
        router, summarizer = self.make_router()

        reply = router.handle(self.private_msg(
            "p1", "好", quoted_content="某句话", quoted_sender_name="某人",
        ))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "好")


# ── empty mention / sticky listening ───────────────────────────────────

class StickyAndEmptyMentionTests(RouterTestCase):
    def test_mention_is_removed_wherever_it_appears(self):
        router, summarizer = self.make_router()

        router.handle(self.group_msg("m1", "你觉得@林小满\u2005怎么样"))
        router.handle(self.group_msg("m2", "@林小满 你好 @林小满 再见"))

        self.assertEqual(
            [c["message"] for c in summarizer.chat_calls],
            ["你觉得怎么样", "你好 再见"],
        )

    def test_sender_prefix_with_an_underscore_survives_the_mention_route(self):
        router, summarizer = self.make_router()

        router.handle(self.group_msg("m1", "wxid_ab12:\n@林小满 你好"))
        router.handle(self.other_msg("m2", "wxid_a_b:\n@林小满 你好", at=True))

        # PINNED (known issue, expected to change in Phase N): the prefix
        # regexes in handle() accept only [a-zA-Z0-9] after "wxid_", while the
        # shared _WECHAT_SENDER_PREFIX (used by the Yang route and the
        # follow-up tracker) also accepts "_".
        self.assertEqual(
            [c["message"] for c in summarizer.chat_calls],
            ["你好", "wxid_a_b:\n你好"],
        )

    def test_mention_reduced_to_separators_counts_as_empty(self):
        router, summarizer = self.make_router()

        for index, content in enumerate((
            "@林小满\u2005",
            "@林小满，",
            "@林小满 ！！",
            "wxid_other:\n@林小满\u200b",
            "   ",
        )):
            with self.subTest(content=content):
                reply = router.handle(
                    self.group_msg(f"m{index}", content),
                )
                self.assertEqual(reply, "嗯，在。")
                self.assertEqual(summarizer.chat_calls[-1]["message"], "在吗")

    def test_sticky_chain_continues_while_the_bot_replies_and_ends_on_silence(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)

        replies = [
            router.handle(self.group_msg("m1", "@林小满 你好")),
            router.handle(self.group_msg("m2", "第二句", at=False)),
            router.handle(self.group_msg("m3", "第三句", at=False)),
        ]
        summarizer.reply = ""
        silent = router.handle(self.group_msg("m4", "第四句", at=False))
        after_silence = router.handle(self.group_msg("m5", "第五句", at=False))

        # Every visible reply re-registers the sticky entry, so the "one-shot"
        # window lasts as long as the bot keeps answering.
        self.assertEqual(replies, ["嗯，在。"] * 3)
        self.assertIsNone(silent)
        self.assertIsNone(after_silence)
        self.assertEqual(len(summarizer.chat_calls), 4)
        self.assertEqual(len(router._sticky), 0)

    def test_sticky_is_scoped_to_chat_and_sender(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        router.handle(self.group_msg("m1", "@林小满 你好"))

        other_sender = router.handle(self.other_msg("m2", "路人说话啦"))
        other_chat = router.handle(
            self.group_msg("m3", "别的群说话", at=False, chat_id="other@chatroom"),
        )
        same_sender = router.handle(self.group_msg("m4", "还是我", at=False))

        self.assertIsNone(other_sender)
        self.assertIsNone(other_chat)
        self.assertEqual(same_sender, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 2)

    def test_sticky_entry_lives_for_the_configured_ttl(self):
        router, _ = self.make_router(
            followup_listen_enabled=False, sticky_mention_ttl_sec=60,
        )
        router.handle(self.group_msg("m1", "@林小满 你好"))

        self.clock.advance(59)
        answered = router.handle(self.group_msg("m2", "还在吗呀", at=False))

        self.assertEqual(answered, "嗯，在。")

    def test_sticky_entry_is_expired_exactly_at_the_ttl(self):
        router, summarizer = self.make_router(
            followup_listen_enabled=False, sticky_mention_ttl_sec=60,
        )
        router.handle(self.group_msg("m1", "@林小满 你好"))

        self.clock.advance(60)
        reply = router.handle(self.group_msg("m2", "还在吗呀", at=False))

        self.assertIsNone(reply)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_without_sticky_mode_follow_ups_are_not_answered(self):
        router, summarizer = self.make_router(
            sticky_mention_enabled=False, followup_listen_enabled=False,
        )

        first = router.handle(self.group_msg("m1", "@林小满 你好"))
        second = router.handle(self.group_msg("m2", "继续聊聊", at=False))

        self.assertEqual(first, "嗯，在。")
        self.assertIsNone(second)
        self.assertIsNone(router._sticky)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_sticky_is_checked_before_the_follow_up_listener(self):
        router, summarizer = self.make_router(followup_listen_enabled=True)
        router.handle(self.group_msg("m1", "@林小满 推荐几首歌"))
        same_sender = self.group_msg("m2", "什么歌呀", at=False)
        other_sender = self.other_msg("m3", "什么歌呀")

        router.handle(same_sender)
        router.handle(other_sender)

        # The same text is a sticky turn for the person the bot just answered
        # and a follow-up-listener turn for everybody else.
        self.assertTrue(same_sender["_sticky_triggered"])
        self.assertNotIn("_followup_listen_triggered", same_sender)
        self.assertTrue(other_sender["_followup_listen_triggered"])
        self.assertTrue(
            summarizer.chat_calls[1]["message"].startswith("这条消息没有直接@你"),
        )
        self.assertTrue(
            summarizer.chat_calls[2]["message"].startswith("你刚才在群里回复："),
        )

    def test_image_messages_do_not_consume_sticky_listening(self):
        router, summarizer = self.make_router(
            followup_listen_enabled=False, image_followup_enabled=False,
        )
        router.handle(self.group_msg("m1", "@林小满"))

        sticker = router.handle(self.sticker_msg("s1"))
        follow_up = router.handle(self.group_msg("m2", "继续说", at=False))

        self.assertIsNone(sticker)
        self.assertEqual(follow_up, "嗯，在。")
        self.assertIn("连续聊天", summarizer.chat_calls[1]["message"])

    def test_sticky_follow_up_runs_the_full_command_chain(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        router.handle(self.group_msg("m1", "@林小满"))

        with patch("src.fun.draw_lots", return_value="@小明 抽签结果：大吉\n好运"):
            reply = router.handle(self.group_msg("m2", "抽签", at=False))

        self.assertEqual(reply, "@小明 抽签结果：大吉\n好运")
        self.assertEqual(len(summarizer.chat_calls), 1)


# ── keyword trigger ────────────────────────────────────────────────────

class KeywordTriggerTests(RouterTestCase):
    def test_keyword_routes_to_ai_chat_and_never_to_summary(self):
        router, summarizer = self.make_router(
            keywords=["总结一下", "summarize"],
        )

        reply = router.handle(
            self.group_msg("k1", "总结一下", at=False),
        )

        # The summary branch is gone (removed in Phase 5): a keyword trigger
        # is answered by plain AI chat and never reaches summarizer.summarize.
        self.assertFalse(hasattr(router, "_handle_summary"))
        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "总结一下")
        self.assertEqual(summarizer.summarize_calls, [])

    def test_mention_plus_keyword_is_also_plain_ai_chat(self):
        router, summarizer = self.make_router(keywords=["总结一下"])

        reply = router.handle(
            self.group_msg("k1", "@林小满 总结一下"),
        )

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "总结一下")
        self.assertEqual(summarizer.summarize_calls, [])
        self.assertEqual(summarizer.summarize_calls, [])

    def test_keyword_matching_ignores_the_sender_prefix_and_case(self):
        router, summarizer = self.make_router(keywords=["summarize"])

        reply = router.handle(self.group_msg(
            "k1", "wxid_abc123:\nPlease SUMMARIZE this", at=False,
        ))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(
            summarizer.chat_calls[0]["message"], "Please SUMMARIZE this",
        )

    def test_keyword_trigger_arms_sticky_listening(self):
        router, summarizer = self.make_router(
            keywords=["总结一下"], followup_listen_enabled=False,
        )

        router.handle(self.group_msg("k1", "总结一下", at=False))
        follow_up = router.handle(self.group_msg("k2", "再说一点", at=False))

        self.assertEqual(follow_up, "嗯，在。")
        self.assertIn("连续聊天", summarizer.chat_calls[1]["message"])

    def test_keyword_trigger_gets_direct_fallbacks(self):
        router, summarizer = self.make_router(keywords=["总结一下"])
        summarizer.reply = ""
        blank = router.handle(self.group_msg("k1", "总结一下", at=False))

        router, summarizer = self.make_router(keywords=["总结一下"])
        summarizer.error = RuntimeError("boom")
        failed = router.handle(self.group_msg("k2", "总结一下", at=False))

        self.assertEqual(blank, "我刚才没接住，再说一次好不好")
        self.assertEqual(failed, "我刚才有点卡，再说一次好不好")

    def test_an_armed_sticky_entry_wins_over_the_keyword_marking(self):
        router, summarizer = self.make_router(
            keywords=["总结一下"], followup_listen_enabled=False,
        )
        router.handle(self.group_msg("k1", "@林小满"))
        summarizer.error = RuntimeError("boom")
        message = self.group_msg("k2", "总结一下", at=False)

        reply = router.handle(message)

        # Sticky is consumed before the keyword check, so the message is
        # handled (and failed) as an indirect turn, not as a keyword trigger.
        self.assertEqual(reply, "大脑短路了，稍等再试～")
        self.assertTrue(message["_sticky_triggered"])
        self.assertNotIn("_kw_triggered", message)

    def test_message_without_keyword_or_mention_is_not_a_trigger(self):
        router, summarizer = self.make_router(keywords=["总结一下"])

        reply = router.handle(self.group_msg("k1", "今天天气不错", at=False))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])


# ── command priority: feishu / help / fun / admin / todo / chat ───────

class CommandPriorityTests(RouterTestCase):
    def test_help_words_go_to_the_admin_handler_for_any_sender(self):
        admin = MagicMock(handle=MagicMock(return_value="HELP TEXT"))
        router, summarizer = self.make_router(admin=admin)

        for index, word in enumerate(("帮助", "help", "命令", "  帮助  ")):
            with self.subTest(word=word):
                admin.handle.reset_mock()
                reply = router.handle(self.other_msg(
                    f"h{index}", f"@林小满 {word}", at=True,
                ))

                self.assertEqual(reply, "HELP TEXT")
                admin.handle.assert_called_once_with(word.strip(), "土豆")
        self.assertEqual(summarizer.chat_calls, [])

    def test_help_match_is_exact_after_stripping(self):
        admin = MagicMock(handle=MagicMock(return_value="HELP TEXT"))
        router, summarizer = self.make_router(admin=admin)

        reply = router.handle(self.group_msg("h1", "@林小满 帮助!"))

        # PINNED (known issue, expected to change in Phase N): "帮助!" or
        # "帮助 一下" is plain chat, not the help command.
        self.assertEqual(reply, "嗯，在。")
        admin.handle.assert_not_called()
        self.assertEqual(summarizer.chat_calls[0]["message"], "帮助!")

    def test_real_help_text_is_flattened_by_the_reply_cleaner(self):
        admin = AdminCommandHandler(StubNicks())
        raw = admin.handle("帮助", "小明")
        router, _ = self.make_router(admin=admin)

        reply = router.handle(self.group_msg("h1", "@林小满 帮助"))

        # PINNED (known issue, expected to change in Phase N): the visible-reply
        # cleaner collapses every whitespace run of two or more characters
        # (blank lines, indentation, column padding) into one space, so the
        # aligned help text becomes a run-on paragraph.
        self.assertIn("\n\n", raw)
        self.assertEqual(reply, re.sub(r"\s{2,}", " ", raw).strip())
        self.assertNotIn("\n\n", reply)
        self.assertTrue(reply.startswith("@小明 可用命令：\n━━━━━━━━━━━━━━\n"))

    def test_lots_command_calls_draw_lots_and_nothing_else(self):
        admin = MagicMock(handle=MagicMock(return_value=None))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_user1",
        )

        with patch("src.fun.draw_lots", return_value="@小明 抽签结果：大吉\n好运") as lots:
            reply = router.handle(self.group_msg("f1", "@林小满 抽签"))

        self.assertEqual(reply, "@小明 抽签结果：大吉\n好运")
        lots.assert_called_once_with("小明")
        admin.handle.assert_not_called()
        self.assertEqual(summarizer.chat_calls, [])

    def test_tarot_command_passes_the_question_to_draw_tarot(self):
        router, summarizer = self.make_router()

        with patch("src.fun.draw_tarot", return_value="@小明 塔罗三张：\nX") as tarot:
            reply = router.handle(self.group_msg("f1", "@林小满 塔罗牌 感情"))

        self.assertEqual(reply, "@小明 塔罗三张：\nX")
        tarot.assert_called_once_with("小明", "感情")
        self.assertEqual(summarizer.chat_calls, [])

    def test_tarot_follow_up_from_another_member_reuses_the_tarot_reply(self):
        router, summarizer = self.make_router()

        with patch("src.fun.draw_tarot", side_effect=[
            "@小明 塔罗三张：\nA", "@土豆 塔罗三张：\nB",
        ]) as tarot:
            first = router.handle(self.group_msg("f1", "@林小满 塔罗牌"))
            second = router.handle(self.other_msg("f2", "感情呢"))

        # "感情呢" is not a tarot command by itself; it is routed through the
        # follow-up listener because the bot's last reply mentioned 塔罗.
        self.assertEqual((first, second), (
            "@小明 塔罗三张：\nA", "@土豆 塔罗三张：\nB",
        ))
        self.assertEqual(
            [call.args for call in tarot.call_args_list],
            [("小明", ""), ("土豆", "感情呢")],
        )
        self.assertEqual(summarizer.chat_calls, [])

    def test_fun_commands_can_be_disabled(self):
        router, summarizer = self.make_router(fun_enabled=False)

        with patch("src.fun.draw_lots") as lots:
            reply = router.handle(self.group_msg("f1", "@林小满 抽签"))

        self.assertEqual(reply, "嗯，在。")
        lots.assert_not_called()
        self.assertEqual(summarizer.chat_calls[0]["message"], "抽签")

    def test_fun_command_wins_over_the_admin_handler_for_the_admin(self):
        admin = MagicMock(handle=MagicMock(return_value="ADMIN"))
        router, _ = self.make_router(admin=admin, admin_wxid="wxid_user1")

        with patch("src.fun.draw_lots", return_value="LOTS"):
            reply = router.handle(self.group_msg("f1", "@林小满 抽签"))

        self.assertEqual(reply, "LOTS")
        admin.handle.assert_not_called()

    def test_export_service_sees_the_cleaned_text_and_a_copy_of_the_message(self):
        feishu = MagicMock()
        feishu.is_export_command.return_value = True
        feishu.export_recent_chat.return_value = MagicMock(
            reply_text="@小明 已同步",
        )
        router, _ = self.make_router(feishu=feishu)
        message = self.group_msg("f1", "wxid_x:\n@林小满 同步到飞书")

        reply = router.handle(message)

        self.assertEqual(reply, "@小明 已同步")
        feishu.is_export_command.assert_called_once_with("同步到飞书")
        exported = feishu.export_recent_chat.call_args.args[0]
        self.assertIsNot(exported, message)
        self.assertEqual(exported["content"], "同步到飞书")
        self.assertEqual(message["content"], "wxid_x:\n@林小满 同步到飞书")

    def test_export_failure_gets_a_fixed_apology_naming_the_sender(self):
        feishu = MagicMock()
        feishu.is_export_command.return_value = True
        feishu.export_recent_chat.side_effect = RuntimeError("feishu down")
        router, summarizer = self.make_router(feishu=feishu)

        reply = router.handle(self.group_msg("f1", "@林小满 同步到飞书"))

        self.assertEqual(
            reply, "@小明 飞书同步失败了：AI 总结或飞书写入临时不可用，稍后再试一次。",
        )
        self.assertEqual(summarizer.chat_calls, [])

    def test_feishu_export_precedes_help_admin_todo_and_chat(self):
        feishu = MagicMock()
        feishu.is_export_command.return_value = True
        feishu.export_recent_chat.return_value = MagicMock(
            reply_text="@小明 已同步",
        )
        admin = MagicMock(handle=MagicMock(return_value="ADMIN"))
        router, summarizer = self.make_router(
            admin=admin, feishu=feishu, admin_wxid="wxid_user1",
            todo_enabled=True,
        )

        reply = router.handle(self.group_msg("f1", "@林小满 帮助"))

        self.assertEqual(reply, "@小明 已同步")
        admin.handle.assert_not_called()
        self.assertEqual(summarizer.chat_calls, [])

    def test_fun_command_overrides_a_feishu_export_reply(self):
        feishu = MagicMock()
        feishu.is_export_command.return_value = True
        feishu.export_recent_chat.return_value = MagicMock(
            reply_text="@小明 已同步",
        )
        router, _ = self.make_router(feishu=feishu)

        with patch("src.fun.draw_lots", return_value="LOTS"):
            reply = router.handle(self.group_msg("f1", "@林小满 抽签"))

        # PINNED (known issue, expected to change in Phase N): the fun branch
        # has no ``reply is None`` guard, so it discards a reply that an
        # earlier stage already produced (the export itself has already run).
        self.assertEqual(reply, "LOTS")
        feishu.export_recent_chat.assert_called_once()

    def test_admin_sender_commands_go_to_the_admin_handler_first(self):
        admin = MagicMock(handle=MagicMock(return_value="ADMIN OK"))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_user1",
        )

        reply = router.handle(
            self.group_msg("a1", "@林小满 改名 wxid_a = 张三"),
        )

        self.assertEqual(reply, "ADMIN OK")
        admin.handle.assert_called_once_with("改名 wxid_a = 张三", "小明")
        self.assertEqual(summarizer.chat_calls, [])

    def test_admin_handler_returning_none_falls_through_to_chat(self):
        admin = MagicMock(handle=MagicMock(return_value=None))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_user1",
        )

        reply = router.handle(self.group_msg("a1", "@林小满 今天天气不错"))

        self.assertEqual(reply, "嗯，在。")
        admin.handle.assert_called_once_with("今天天气不错", "小明")
        self.assertEqual(summarizer.chat_calls[0]["message"], "今天天气不错")

    def test_admin_commands_from_other_senders_are_plain_chat(self):
        admin = MagicMock(handle=MagicMock(return_value="ADMIN OK"))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_boss",
        )

        reply = router.handle(
            self.group_msg("a1", "@林小满 改名 wxid_a = 张三"),
        )

        # PINNED (known issue, expected to change in Phase N): a non-admin's
        # admin command is not rejected, it is sent to the model verbatim.
        self.assertEqual(reply, "嗯，在。")
        admin.handle.assert_not_called()
        self.assertEqual(
            summarizer.chat_calls[0]["message"], "改名 wxid_a = 张三",
        )

    def test_empty_admin_wxid_never_matches_a_sender(self):
        admin = MagicMock(handle=MagicMock(return_value="ADMIN OK"))
        router, _ = self.make_router(admin=admin, admin_wxid="")

        reply = router.handle(self.group_msg(
            "a1", "@林小满 改名 wxid_a = 张三", sender_id="",
        ))

        self.assertEqual(reply, "嗯，在。")
        admin.handle.assert_not_called()

    def test_todo_command_is_answered_without_the_model(self):
        router, summarizer = self.make_router(todo_enabled=True)

        added = router.handle(self.group_msg("t1", "@林小满 记一下 周五聚餐"))
        listed = router.handle(self.group_msg("t2", "@林小满 查看待办"))

        self.assertIn("周五聚餐", added)
        self.assertIn("共 1 项待办", added)
        self.assertEqual(listed, added)
        self.assertEqual(summarizer.chat_calls, [])

    def test_todo_reply_loses_its_blank_lines_in_the_cleaner(self):
        router, _ = self.make_router(todo_enabled=True)

        reply = router.handle(self.group_msg("t1", "@林小满 记一下 周五聚餐"))

        # PINNED (known issue, expected to change in Phase N): format_todo_reply
        # separates sections with blank lines, which the visible-reply cleaner
        # collapses into single spaces.
        self.assertEqual(
            reply,
            '当前群待办内容有： 1. 周五聚餐 共 1 项待办。\n'
            '回复"完成 N"或"删除 N"可操作指定事项。',
        )

    def test_todo_runs_after_the_admin_handler_and_before_chat(self):
        admin = MagicMock(handle=MagicMock(return_value=None))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_user1", todo_enabled=True,
        )

        reply = router.handle(self.group_msg("t1", "@林小满 记一下 周五聚餐"))

        self.assertIn("周五聚餐", reply)
        admin.handle.assert_called_once_with("记一下 周五聚餐", "小明")
        self.assertEqual(summarizer.chat_calls, [])

    def test_todo_is_skipped_for_groups_outside_the_allow_list(self):
        router, summarizer = self.make_router(
            todo_enabled=True, todo_groups=["别的群"],
        )

        reply = router.handle(self.group_msg("t1", "@林小满 记一下 买菜"))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "记一下 买菜")

    def test_todo_commands_are_plain_chat_when_the_feature_is_off(self):
        router, summarizer = self.make_router(todo_enabled=False)

        reply = router.handle(self.group_msg("t1", "@林小满 记一下 买菜"))

        self.assertIsNone(router._todo_handler)
        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "记一下 买菜")

    def test_todo_failure_is_logged_and_falls_through_to_chat(self):
        router, summarizer = self.make_router(todo_enabled=True)

        with patch.object(
            router._todo_handler, "handle", side_effect=RuntimeError("bad"),
        ):
            reply = router.handle(self.group_msg("t1", "@林小满 记一下 买菜"))

        # PINNED (known issue, expected to change in Phase N): a crashing todo
        # handler silently hands the command to the AI.
        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "记一下 买菜")

    def test_todo_cleanup_runs_for_every_mention_that_reaches_the_todo_stage(self):
        router, _ = self.make_router(
            todo_enabled=True, todo_completed_retention_days=7,
            todo_deleted_retention_days=3,
        )

        with patch.object(router._todo_store, "cleanup") as cleanup:
            router.handle(self.group_msg("t1", "@林小满 今天天气不错"))
            router.handle(self.group_msg("t2", "@林小满 记一下 买菜"))

        # PINNED (known issue, expected to change in Phase N): the retention
        # sweep (two DELETEs) runs for every @mention, todo command or not.
        self.assertEqual(cleanup.call_count, 2)
        cleanup.assert_called_with(CHAT, 7, 3)

    def test_generic_text_from_the_admin_consults_admin_then_todo_then_chat(self):
        admin = MagicMock(handle=MagicMock(return_value=None))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_user1", todo_enabled=True,
        )
        events = summarizer.events
        admin.handle.side_effect = lambda *args: events.append("admin")
        todo_patch = patch.object(
            router._todo_handler, "handle",
            side_effect=lambda *args: events.append("todo"),
        )

        with todo_patch:
            router.handle(self.group_msg("o1", "@林小满 今天天气不错"))

        self.assertEqual(events, ["admin", "todo", "chat"])

    def test_a_produced_reply_short_circuits_the_later_stages(self):
        admin = MagicMock(handle=MagicMock(return_value=None))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_user1", todo_enabled=True,
        )
        events = summarizer.events
        admin.handle.side_effect = lambda *args: events.append("admin") or "ADMIN"
        todo_patch = patch.object(
            router._todo_handler, "handle",
            side_effect=lambda *args: events.append("todo"),
        )

        with todo_patch:
            reply = router.handle(self.group_msg("o1", "@林小满 改名 a = b"))

        self.assertEqual(reply, "ADMIN")
        self.assertEqual(events, ["admin"])


# ── todo group matching ────────────────────────────────────────────────

class TodoGroupMatchingTests(RouterTestCase):
    def router_for(self, groups):
        router, _ = self.make_router(todo_enabled=True, todo_groups=groups)
        return router

    def write_group_names(self, mapping):
        (self.tmp / "data" / "group_names.json").write_text(
            json.dumps(mapping, ensure_ascii=False), encoding="utf-8",
        )

    def test_wildcard_and_empty_lists_allow_every_group(self):
        for groups in (["*"], []):
            with self.subTest(groups=groups):
                router = self.router_for(groups)
                self.assertTrue(router._is_todo_group("any@chatroom"))

    def test_wildcard_is_only_recognised_as_the_sole_entry(self):
        router = self.router_for(["*", "x"])

        # PINNED (known issue, expected to change in Phase N): "*" next to
        # another entry is compared as a literal substring and matches nothing.
        self.assertFalse(router._is_todo_group("room1@chatroom"))

    def test_matches_chat_id_exactly_or_by_case_insensitive_substring(self):
        router = self.router_for(["room1@chatroom"])
        self.assertTrue(router._is_todo_group("room1@chatroom"))
        self.assertFalse(router._is_todo_group("room2@chatroom"))

        router = self.router_for(["ROOM1"])
        self.assertTrue(router._is_todo_group("room1@chatroom"))

    def test_matches_display_names_from_group_names_json(self):
        self.write_group_names({"room1@chatroom": "摸鱼交流群"})

        exact = self.router_for(["摸鱼交流群"])
        partial = self.router_for(["摸鱼"])
        other = self.router_for(["别的群"])

        self.assertTrue(exact._is_todo_group("room1@chatroom"))
        self.assertTrue(partial._is_todo_group("room1@chatroom"))
        self.assertFalse(partial._is_todo_group("room2@chatroom"))
        self.assertFalse(other._is_todo_group("room1@chatroom"))

    def test_empty_entry_matches_every_group(self):
        router = self.router_for([""])

        # PINNED (known issue, expected to change in Phase N): "" is a
        # substring of every chat id, so a stray empty entry opens the todo
        # feature everywhere.
        self.assertTrue(router._is_todo_group("room1@chatroom"))

    def test_unreadable_group_names_file_is_treated_as_empty(self):
        (self.tmp / "data" / "group_names.json").write_text("{not json")
        router = self.router_for(["摸鱼"])

        self.assertFalse(router._is_todo_group("room1@chatroom"))

    def test_group_names_file_is_loaded_once_per_router(self):
        router = self.router_for(["摸鱼"])
        self.assertFalse(router._is_todo_group("room1@chatroom"))

        self.write_group_names({"room1@chatroom": "摸鱼交流群"})

        self.assertFalse(router._is_todo_group("room1@chatroom"))
        self.assertTrue(
            self.router_for(["摸鱼"])._is_todo_group("room1@chatroom"),
        )

    def test_group_names_file_with_a_non_object_value_breaks_matching(self):
        (self.tmp / "data" / "group_names.json").write_text("[1, 2]")
        router, summarizer = self.make_router(
            todo_enabled=True, todo_groups=["摸鱼"],
        )

        # PINNED (known issue, expected to change in Phase N): valid JSON of
        # the wrong type is not guarded, so every @mention raises.
        with self.assertRaises(AttributeError):
            router.handle(self.group_msg("t1", "@林小满 记一下 买菜"))
        self.assertEqual(summarizer.chat_calls, [])


# ── private chat ───────────────────────────────────────────────────────

class PrivateChatTests(RouterTestCase):
    def test_blank_private_message_is_ignored(self):
        router, summarizer = self.make_router()

        reply = router.handle(self.private_msg("p1", "   "))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(self.stored_ids(router), ["p1"])

    def test_sender_prefix_is_removed_and_the_message_is_flagged(self):
        router, summarizer = self.make_router()
        message = self.private_msg("p1", "wxid_friend:\n你好呀")

        reply = router.handle(message)

        self.assertEqual(reply, "嗯，在。")
        call = summarizer.chat_calls[0]
        self.assertEqual(call["message"], "你好呀")
        self.assertFalse(call["is_group"])
        self.assertFalse(call["ambient"])
        self.assertEqual(call["group_name"], "私聊好友")
        self.assertTrue(message["_private_triggered"])

    def test_private_failures_use_the_direct_fallbacks(self):
        router, summarizer = self.make_router()
        summarizer.error = TimeoutError("slow")

        failed = router.handle(self.private_msg("p1", "你怎么看"))
        presence = router.handle(self.private_msg("p2", "在不在"))

        self.assertEqual(failed, "我刚才有点卡，再说一次好不好")
        self.assertEqual(presence, "嗯，在的")

    def test_private_help_lots_and_admin_commands(self):
        admin = MagicMock(handle=MagicMock(return_value="ADMIN TEXT"))
        router, summarizer = self.make_router(
            admin=admin, admin_wxid="wxid_friend",
        )

        with patch("src.fun.draw_lots", return_value="LOTS"):
            lots = router.handle(self.private_msg("p1", "抽签"))
        help_reply = router.handle(self.private_msg("p2", "帮助"))
        admin_reply = router.handle(self.private_msg("p3", "刷新昵称"))

        self.assertEqual(lots, "LOTS")
        self.assertEqual(help_reply, "ADMIN TEXT")
        self.assertEqual(admin_reply, "ADMIN TEXT")
        self.assertEqual(
            [call.args for call in admin.handle.call_args_list],
            [("帮助", "私聊好友"), ("刷新昵称", "私聊好友")],
        )
        self.assertEqual(summarizer.chat_calls, [])

    def test_private_tarot_follow_up_needs_a_pre_annotated_message(self):
        router, summarizer = self.make_router()
        annotated = self.private_msg(
            "p1", "感情呢", _followup_bot_reply="塔罗三张：...",
        )
        plain = self.private_msg("p2", "感情呢")

        with patch("src.fun.draw_tarot", return_value="TAROT") as tarot:
            first = router.handle(annotated)
            second = router.handle(plain)

        # PINNED (known issue, expected to change in Phase N): the private
        # branch inherits the group branch's "塔罗 in previous reply" rule,
        # but the follow-up tracker only annotates group messages, so only a
        # dict that already carries "_followup_bot_reply" reaches it.
        self.assertEqual(first, "TAROT")
        tarot.assert_called_once_with("私聊好友", "感情呢")
        self.assertEqual(second, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_todo_commands_are_not_handled_in_private_chat(self):
        router, summarizer = self.make_router(todo_enabled=True)

        reply = router.handle(self.private_msg("p1", "记一下 买菜"))

        # PINNED (known issue, expected to change in Phase N): the todo stage
        # only exists on the group @mention path.
        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "记一下 买菜")

    def test_private_reply_arms_only_the_image_follow_up(self):
        router, _ = self.make_router()

        router.handle(self.private_msg("p1", "你好"))

        self.assertEqual(len(router._sticky), 0)
        self.assertEqual(router._followup._entries, {})
        self.assertEqual(
            list(router._image_followup._entries), [("wxid_friend", "wxid_friend")],
        )

    def test_private_chat_answers_image_questions(self):
        router, summarizer = self.make_router()
        router._store.insert_message(self.private_msg(
            "img", "[图片]", msg_type=3, server_id="S1", media_path="/p/a.jpg",
            timestamp=NOW - 10,
        ))

        with patch.object(
            router_module, "find_recent_image_path", return_value="/p/a.jpg",
        ):
            reply = router.handle(self.private_msg("p1", "这张图里是什么"))

        self.assertEqual(reply, "是一张测试图")
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(len(summarizer.image_calls), 1)


# ── image routing branches ─────────────────────────────────────────────

class ImageQuestionTests(RouterTestCase):
    IMAGE = "/media/recent.jpg"

    def test_recent_media_is_queried_with_the_documented_arguments(self):
        router, summarizer = self.make_router(image_vision_lookback_sec=600)
        records = [{"media_path": self.IMAGE, "timestamp": NOW - 5}]

        with patch.object(
            router._store, "get_recent_media", return_value=records,
        ) as recent, patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ) as find:
            reply = router.handle(
                self.group_msg("q1", "@林小满 这张图里是什么", timestamp=NOW - 10),
            )

        self.assertEqual(reply, "是一张测试图")
        recent.assert_called_once_with(
            CHAT, before_ts=NOW - 10, lookback_sec=600, limit=8,
        )
        self.assertIs(find.call_args.args[0], router._config)
        self.assertEqual(find.call_args.args[1], records)
        call = summarizer.image_calls[0]
        self.assertEqual(sorted(call), IMAGE_CALL_KEYS)
        self.assertEqual(call["image_path"], self.IMAGE)
        self.assertEqual(call["question"], "这张图里是什么")
        self.assertEqual(call["requester_name"], "小明")
        self.assertEqual(call["bot_name"], BOT)
        self.assertEqual(call["max_chars"], 80)
        self.assertEqual(call["max_bytes"], 5 * 1024 * 1024)

    def test_requester_is_resolved_through_the_nickname_service(self):
        router, summarizer = self.make_router(
            nicks=StubNicks({"wxid_user1": "明明"}),
        )

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            router.handle(self.group_msg("q1", "@林小满 这张图里是什么"))

        self.assertEqual(summarizer.image_calls[0]["requester_name"], "明明")

    def test_missing_media_asks_to_resend_even_when_the_lookup_fails(self):
        router, summarizer = self.make_router()

        with patch.object(
            router._store, "get_recent_media", side_effect=RuntimeError("db"),
        ), patch.object(
            router_module, "find_recent_image_path", return_value="",
        ):
            reply = router.handle(
                self.group_msg("q1", "@林小满 这张图里是什么"),
            )

        self.assertEqual(reply, "我没找到刚才那张图……再发一次好不好")
        self.assertEqual(summarizer.image_calls, [])

    def test_summarizer_without_vision_cannot_describe_images(self):
        router, _ = self.make_router(summarizer=SummarizerWithoutVision())

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(
                self.group_msg("q1", "@林小满 这张图里是什么"),
            )

        self.assertEqual(reply, "我现在还看不了图……")

    def test_blank_description_and_vision_errors_have_fixed_replies(self):
        router, summarizer = self.make_router()
        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            summarizer.image_reply = "   "
            blank = router.handle(
                self.group_msg("q1", "@林小满 这张图里是什么"),
            )
            summarizer.error = RuntimeError("vision down")
            failed = router.handle(
                self.group_msg("q2", "@林小满 这张图里是什么"),
            )

        self.assertEqual(blank, "我看不太清……")
        self.assertEqual(failed, "我这边看图有点卡，再发一次好不好")

    def test_vision_disabled_sends_the_question_to_plain_chat(self):
        router, summarizer = self.make_router(image_vision_enabled=False)

        reply = router.handle(
            self.group_msg("q1", "@林小满 这张图里是什么"),
        )

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["message"], "这张图里是什么")
        self.assertEqual(summarizer.image_calls, [])

    def test_image_question_needs_a_trigger(self):
        router, summarizer = self.make_router()

        reply = router.handle(
            self.group_msg("q1", "这张图里是什么", at=False),
        )

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])
        self.assertEqual(summarizer.image_calls, [])

    def test_sticky_follow_up_can_ask_about_an_image(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        router.handle(self.group_msg("m1", "@林小满"))

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(
                self.group_msg("m2", "这张图里是什么", at=False),
            )

        self.assertEqual(reply, "是一张测试图")
        self.assertEqual(len(summarizer.image_calls), 1)
        self.assertEqual(len(summarizer.chat_calls), 1)       # only the "在吗"

    def test_vision_reply_is_cleaned_and_arms_sticky_listening(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        summarizer.image_reply = "**一张** 猫猫图 😀"

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(
                self.group_msg("q1", "@林小满 这张图里是什么"),
            )

        self.assertEqual(reply, "一张 猫猫图")
        self.assertEqual(len(router._sticky), 1)


class QuotedImageQuestionTests(RouterTestCase):
    IMAGE = "/media/quoted.jpg"
    NOT_FOUND = "我没找到你引用的那张图……再发一次好不好"

    _sequence = 0

    def quoted(self, content="林小满，这张图是什么意思", **overrides):
        QuotedImageQuestionTests._sequence += 1
        overrides.setdefault("quoted_server_id", "srv-1")
        overrides.setdefault("quoted_content", "[图片]")
        return self.group_msg(
            f"quoted-{self._sequence}", content, at=False, **overrides,
        )

    def with_media(self, router):
        router._store.insert_message(self.group_msg(
            "img-1", "[图片]", at=False, msg_type=3, server_id="srv-1",
            sender_id="wxid_user2", sender_name="土豆", timestamp=NOW - 30,
        ))

    def test_media_is_looked_up_by_the_quoted_server_id(self):
        router, summarizer = self.make_router()
        self.with_media(router)

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ) as find:
            reply = router.handle(self.quoted())

        self.assertEqual(reply, "是一张测试图")
        record = find.call_args.args[1][0]
        self.assertEqual(
            (record["message_id"], record["server_id"]), ("img-1", "srv-1"),
        )
        call = summarizer.image_calls[0]
        self.assertEqual(sorted(call), IMAGE_CALL_KEYS)
        self.assertEqual(call["question"], "这张图是什么意思")
        self.assertEqual(call["requester_name"], "小明")

    def test_question_is_the_text_without_the_bot_name(self):
        router, summarizer = self.make_router()
        self.with_media(router)

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            router.handle(self.quoted("林小满 这是什么图片"))
            router.handle(self.quoted("林小满：这张图是谁画的？"))
            router.handle(self.quoted("@林小满\u2005这张图是什么意思"))

        # PINNED (known issue, expected to change in Phase N): only the bare
        # name is removed, so a WeChat "@name" mention leaves "@" plus the
        # invisible separator at the front of the question sent to the model.
        # (The "这张图是什么意思？" default is unreachable: a name-only message
        # is not an image question in the first place.)
        self.assertEqual(
            [c["question"] for c in summarizer.image_calls],
            ["这是什么图片", "这张图是谁画的？", "@\u2005这张图是什么意思"],
        )

    def test_name_only_message_is_not_an_image_question(self):
        router, summarizer = self.make_router()
        self.with_media(router)

        reply = router.handle(self.quoted("林小满"))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.image_calls, [])

    def test_unknown_quoted_media_asks_to_resend(self):
        router, summarizer = self.make_router()

        reply = router.handle(self.quoted())

        self.assertEqual(reply, self.NOT_FOUND)
        self.assertEqual(summarizer.image_calls, [])

    def test_missing_image_file_asks_to_resend(self):
        router, _ = self.make_router()
        self.with_media(router)

        with patch.object(
            router_module, "find_recent_image_path", return_value="",
        ):
            reply = router.handle(self.quoted())

        self.assertEqual(reply, self.NOT_FOUND)

    def test_lookup_failure_asks_to_resend(self):
        router, _ = self.make_router()

        with patch.object(
            router._store, "get_media_by_server_id",
            side_effect=RuntimeError("db"),
        ):
            reply = router.handle(self.quoted())

        self.assertEqual(reply, self.NOT_FOUND)

    def test_store_without_media_lookup_asks_to_resend(self):
        router, _ = self.make_router(store=MinimalStore())

        reply = router.handle(self.quoted())

        self.assertEqual(reply, self.NOT_FOUND)

    def test_summarizer_without_vision_cannot_describe_the_quote(self):
        router, _ = self.make_router(summarizer=SummarizerWithoutVision())
        self.with_media(router)

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(self.quoted())

        self.assertEqual(reply, "我现在还看不了图……")

    def test_blank_description_and_vision_errors_have_fixed_replies(self):
        router, summarizer = self.make_router()
        self.with_media(router)
        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            summarizer.image_reply = ""
            blank = router.handle(self.quoted())
            summarizer.error = RuntimeError("vision down")
            failed = router.handle(self.quoted())

        self.assertEqual(blank, "我看不太清……")
        self.assertEqual(failed, "我这边看图有点卡，再发一次好不好")

    def test_branch_needs_a_quoted_server_id(self):
        router, summarizer = self.make_router()

        reply = router.handle(self.quoted(quoted_server_id=""))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.image_calls, [])

    def test_vision_disabled_skips_the_branch(self):
        router, summarizer = self.make_router(image_vision_enabled=False)
        self.with_media(router)

        reply = router.handle(self.quoted())

        self.assertIsNone(reply)
        self.assertEqual(summarizer.image_calls, [])

    def test_quoted_image_reply_is_cleaned_like_any_visible_reply(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        self.with_media(router)
        summarizer.image_reply = "**像是** 😀 在夸你"

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(self.quoted())

        self.assertEqual(reply, "像是 在夸你")
        self.assertEqual(len(router._sticky), 1)


class ImageFollowupTests(RouterTestCase):
    IMAGE = "/media/sticker.gif"

    def arm(self, router, message_id="m0"):
        reply = router.handle(self.group_msg(message_id, "@林小满 你在吗"))
        self.assertEqual(reply, "嗯，在。")

    def test_image_without_a_prior_bot_reply_is_ignored(self):
        router, summarizer = self.make_router()

        with patch.object(router_module, "find_recent_image_path") as find:
            reply = router.handle(self.sticker_msg("s1"))

        self.assertIsNone(reply)
        find.assert_not_called()
        self.assertEqual(summarizer.image_calls, [])

    def test_text_from_the_same_sender_closes_the_window(self):
        router, summarizer = self.make_router()
        self.arm(router)
        summarizer.reply = ""
        summarizer.last_chat_reply_was_intentional_silence = True
        router.handle(self.group_msg("t1", "有人吗", at=False))   # silent turn

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(self.sticker_msg("s1"))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.image_calls, [])

    def test_text_from_another_sender_leaves_the_window_open(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        self.arm(router)
        router.handle(self.other_msg("t1", "别人说话"))

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(self.sticker_msg("s1"))

        self.assertEqual(reply, "是一张测试图")

    def test_window_expires_after_the_configured_ttl(self):
        router, summarizer = self.make_router(image_followup_ttl_sec=90)
        self.arm(router)

        self.clock.advance(91)
        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(self.sticker_msg("s1"))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.image_calls, [])

    def test_disabled_vision_or_disabled_follow_up_never_describes(self):
        for overrides in (
            {"image_vision_enabled": False},
            {"image_followup_enabled": False},
        ):
            with self.subTest(**overrides):
                router, summarizer = self.make_router(**overrides)
                self.arm(router)

                with patch.object(
                    router_module, "find_recent_image_path",
                    return_value=self.IMAGE,
                ):
                    reply = router.handle(self.sticker_msg("s1"))

                self.assertIsNone(reply)
                self.assertEqual(summarizer.image_calls, [])

    def test_follow_up_description_call_carries_the_previous_reply(self):
        router, summarizer = self.make_router()
        self.arm(router)

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ) as find:
            router.handle(self.sticker_msg("s1"))

        call = summarizer.image_calls[0]
        self.assertEqual(
            sorted(call),
            sorted(IMAGE_CALL_KEYS + ["is_chat_screenshot", "previous_bot_reply"]),
        )
        self.assertEqual(call["question"], "这张图片或表情包是在接你刚才的话吗？")
        self.assertEqual(call["previous_bot_reply"], "嗯，在。")
        self.assertFalse(call["is_chat_screenshot"])
        self.assertEqual(find.call_args.args[1][0]["message_id"], "s1")

    def test_screen_capture_is_not_used_when_the_fallback_is_disabled(self):
        router, summarizer = self.make_router(
            image_screen_fallback_enabled=False,
        )
        self.arm(router)
        capture = MagicMock(return_value="/tmp/capture.jpg")

        with patch.object(
            router_module, "find_recent_image_path", return_value="",
        ):
            reply = router.handle(
                self.sticker_msg("s1", _capture_media_screenshot=capture),
            )

        self.assertIsNone(reply)
        capture.assert_not_called()

    def test_unusable_screen_capture_means_no_reply(self):
        scenarios = {
            "capture raises": MagicMock(side_effect=RuntimeError("no window")),
            "capture returns empty": MagicMock(return_value=""),
            "no capture callable": None,
        }
        for name, capture in scenarios.items():
            with self.subTest(name):
                router, summarizer = self.make_router()
                self.arm(router)
                extra = (
                    {} if capture is None
                    else {"_capture_media_screenshot": capture}
                )

                with patch.object(
                    router_module, "find_recent_image_path", return_value="",
                ):
                    reply = router.handle(self.sticker_msg("s1", **extra))

                self.assertIsNone(reply)
                self.assertEqual(summarizer.image_calls, [])

    def test_captured_screenshot_is_deleted_after_use(self):
        router, summarizer = self.make_router()
        self.arm(router)
        capture_file = self.tmp / "capture.jpg"
        capture_file.write_bytes(b"x")
        capture = MagicMock(return_value=str(capture_file))

        with patch.object(
            router_module, "find_recent_image_path", return_value="",
        ):
            reply = router.handle(
                self.sticker_msg("s1", _capture_media_screenshot=capture),
            )

        self.assertEqual(reply, "是一张测试图")
        self.assertFalse(capture_file.exists())
        call = summarizer.image_calls[0]
        self.assertEqual(call["image_path"], str(capture_file))
        self.assertTrue(call["is_chat_screenshot"])

    def test_a_capture_that_cannot_be_deleted_does_not_lose_the_reply(self):
        router, _ = self.make_router()
        self.arm(router)
        undeletable = self.tmp / "capture-dir"
        undeletable.mkdir()           # unlink() on a directory raises OSError

        with patch.object(
            router_module, "find_recent_image_path", return_value="",
        ):
            reply = router.handle(self.sticker_msg(
                "s1", _capture_media_screenshot=MagicMock(
                    return_value=str(undeletable),
                ),
            ))

        self.assertEqual(reply, "是一张测试图")
        self.assertTrue(undeletable.exists())

    def test_captured_screenshot_is_deleted_even_when_vision_fails(self):
        router, summarizer = self.make_router()
        self.arm(router)
        capture_file = self.tmp / "capture.jpg"
        capture_file.write_bytes(b"x")
        summarizer.error = RuntimeError("vision down")

        with patch.object(
            router_module, "find_recent_image_path", return_value="",
        ):
            reply = router.handle(self.sticker_msg(
                "s1", _capture_media_screenshot=MagicMock(
                    return_value=str(capture_file),
                ),
            ))

        self.assertIsNone(reply)
        self.assertFalse(capture_file.exists())

    def test_blank_description_and_missing_vision_mean_no_reply(self):
        router, summarizer = self.make_router()
        self.arm(router)
        summarizer.image_reply = "   "
        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            blank = router.handle(self.sticker_msg("s1"))

        router, _ = self.make_router(summarizer=SummarizerWithoutVision())
        self.arm(router)
        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            no_vision = router.handle(self.sticker_msg("s2"))

        self.assertIsNone(blank)
        self.assertIsNone(no_vision)

    def test_follow_up_reply_is_cleaned_and_does_not_reopen_the_window(self):
        router, summarizer = self.make_router(followup_listen_enabled=False)
        self.arm(router)
        summarizer.image_reply = "**像是** 😀 在接话"

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            first = router.handle(self.sticker_msg("s1"))
            second = router.handle(self.sticker_msg("s2"))

        self.assertEqual(first, "像是 在接话")
        self.assertIsNone(second)
        self.assertEqual(len(summarizer.image_calls), 1)
        # The reply still arms sticky listening for the sender's next text.
        self.assertEqual(len(router._sticky), 1)

    def test_images_never_reach_the_ambient_gate(self):
        router, _ = self.make_router()
        gate = self.gate(router)

        reply = router.handle(self.sticker_msg("s1"))

        # PINNED (known issue, expected to change in Phase N): image messages
        # return before the proactive gate, so they never feed its rate
        # tracker either.
        self.assertIsNone(reply)
        gate.assert_not_called()

    def test_msg_type_detection_is_tolerant(self):
        check = MessageRouter._is_image_message

        self.assertTrue(check({"msg_type": 3}))
        self.assertTrue(check({"msg_type": "47"}))
        self.assertFalse(check({"msg_type": 49}))
        self.assertFalse(check({"msg_type": "abc"}))
        self.assertFalse(check({"msg_type": None}))
        self.assertFalse(check({}))

    def test_private_image_follow_up_works_after_a_private_reply(self):
        router, summarizer = self.make_router()
        router.handle(self.private_msg("p1", "你好"))
        sticker = self.private_msg(
            "p2", "[表情]", msg_type=47, media_path="/media/s.gif",
        )

        with patch.object(
            router_module, "find_recent_image_path", return_value=self.IMAGE,
        ):
            reply = router.handle(sticker)

        self.assertEqual(reply, "是一张测试图")
        self.assertEqual(
            summarizer.image_calls[0]["previous_bot_reply"], "嗯，在。",
        )


# ── welcome messages ───────────────────────────────────────────────────

class WelcomeTests(RouterTestCase):
    def test_fresh_join_gets_the_default_template(self):
        router, summarizer = self.make_router(welcome_enabled=True)

        reply = router.handle(self.join_msg("j1"))

        # PINNED (known issue, expected to change in Phase N): the default
        # templates already contain "@{new_member}" and the manager prefixes
        # another "@", producing "@@小红"; and the welcome text bypasses the
        # visible-reply cleaner, so the emoji survives.
        self.assertEqual(reply, "欢迎 @@小红 加入群聊！🎉")
        self.assertEqual(summarizer.chat_calls, [])

    def test_join_at_the_age_limit_is_welcomed_and_older_is_not(self):
        router, summarizer = self.make_router(welcome_enabled=True)

        fresh = router.handle(self.join_msg(
            "j1", timestamp=NOW - WELCOME_MAX_AGE_SEC,
        ))
        stale = router.handle(self.join_msg(
            "j2", timestamp=NOW - WELCOME_MAX_AGE_SEC - 1,
        ))

        self.assertEqual(fresh, "欢迎 @@小红 加入群聊！🎉")
        self.assertIsNone(stale)
        self.assertEqual(summarizer.chat_calls, [])

    def test_stale_join_falls_through_to_the_rest_of_the_chain(self):
        router, summarizer = self.make_router(
            welcome_enabled=True, followup_listen_enabled=False,
        )
        # An armed sticky entry shows the stale join is NOT answered before
        # the sticky/keyword stages: it is consumed there.
        router.handle(self.group_msg(
            "m0", "@林小满", sender_id="wxid_inviter",
        ))

        stale = router.handle(self.join_msg(
            "j1", timestamp=NOW - WELCOME_MAX_AGE_SEC - 1,
        ))

        self.assertIsNone(stale)
        self.assertEqual(len(router._sticky), 0)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_join_is_ignored_when_welcome_is_disabled(self):
        router, summarizer = self.make_router(welcome_enabled=False)

        reply = router.handle(self.join_msg("j1"))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_join_without_new_member_id_is_ignored(self):
        router, _ = self.make_router(welcome_enabled=True)

        reply = router.handle(self.join_msg("j1", new_member_id=""))

        self.assertIsNone(reply)

    def test_group_can_opt_out_through_the_group_mapping(self):
        self.welcome_manager.save({
            "templates": [
                {"id": "t1", "name": "n", "message": "欢迎 {new_member}"},
            ],
            "group_mapping": {CHAT: "__disabled__"},
            "default_template": "t1",
        })
        router, _ = self.make_router(welcome_enabled=True)

        self.assertIsNone(router.handle(self.join_msg("j1")))

    def test_custom_template_is_returned_verbatim(self):
        self.welcome_manager.save({
            "templates": [
                {"id": "t1", "name": "n",
                 "message": "欢迎 {new_member} **来了** 😀"},
            ],
            "group_mapping": {},
            "default_template": "t1",
        })
        router, _ = self.make_router(welcome_enabled=True)

        reply = router.handle(self.join_msg("j1", member="小红"))

        # No markdown/emoji cleaning is applied to welcome texts.
        self.assertEqual(reply, "欢迎 @小红 **来了** 😀")

    def test_redelivered_fresh_join_is_welcomed_again(self):
        router, _ = self.make_router(welcome_enabled=True)

        first = router.handle(self.join_msg("j1"))
        second = router.handle(self.join_msg("j1"))

        # PINNED (known issue, expected to change in Phase N): a duplicate that
        # failed to store is still welcomed (so a DB outage cannot suppress the
        # greeting), which also means a re-delivered event greets twice.
        self.assertEqual(first, second)
        self.assertEqual(second, "欢迎 @@小红 加入群聊！🎉")
        self.assertEqual(router.messages_processed, 1)

    def test_redelivered_stale_join_is_dropped(self):
        router, _ = self.make_router(welcome_enabled=True)
        router.handle(self.join_msg("j1", timestamp=NOW - 1000))

        again = router.handle(self.join_msg("j1", timestamp=NOW - 1000))

        self.assertIsNone(again)

    def test_join_is_welcomed_even_if_the_store_is_down(self):
        router, _ = self.make_router(welcome_enabled=True)

        with patch.object(
            router._store, "insert_message", side_effect=RuntimeError("db"),
        ):
            reply = router.handle(self.join_msg("j1"))

        self.assertEqual(reply, "欢迎 @@小红 加入群聊！🎉")

    def test_welcome_does_not_arm_sticky_or_follow_up_listening(self):
        router, _ = self.make_router(welcome_enabled=True)

        router.handle(self.join_msg("j1"))

        self.assertEqual(len(router._sticky), 0)
        self.assertEqual(router._followup._entries, {})

    def test_welcome_is_decided_before_sticky_listening_is_consumed(self):
        router, summarizer = self.make_router(
            welcome_enabled=True, followup_listen_enabled=False,
        )
        router.handle(self.group_msg(
            "m0", "@林小满", sender_id="wxid_inviter", sender_name="邀请人",
        ))
        self.assertEqual(len(router._sticky), 1)

        reply = router.handle(self.join_msg("j1"))

        self.assertEqual(reply, "欢迎 @@小红 加入群聊！🎉")
        self.assertEqual(len(router._sticky), 1)          # not consumed
        self.assertEqual(len(summarizer.chat_calls), 1)   # only the "在吗"

    def test_join_in_the_yang_target_group_is_welcomed_not_chatted(self):
        router, summarizer = self.make_router(
            welcome_enabled=True, bot_display_name=YANG_BOT,
            chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
            yang_yujing_group_name=YANG_GROUP,
        )

        reply = router.handle(self.join_msg("j1", group_name=YANG_GROUP))

        self.assertEqual(reply, "欢迎 @@小红 加入群聊！🎉")
        self.assertEqual(summarizer.chat_calls, [])


# ── Yang Yujing full-feed branches (beyond tests/test_router.py) ──────

class YangFullFeedTests(RouterTestCase):
    def yang_router(self, *, always_reply="", **overrides):
        overrides.setdefault("bot_display_name", YANG_BOT)
        overrides.setdefault("chat_persona_name", YANG_BOT)
        return self.make_router(
            chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
            yang_yujing_group_name=YANG_GROUP,
            yang_yujing_always_reply_group_name=always_reply,
            **overrides,
        )

    def yang_msg(self, message_id, content, **overrides):
        overrides.setdefault("group_name", YANG_GROUP)
        overrides.setdefault("at", False)
        return self.group_msg(message_id, content, **overrides)

    def test_short_reactions_are_skipped_without_using_the_cooldown(self):
        router, summarizer = self.yang_router()

        for index, content in enumerate(("嗯", "666", "笑死", "好的", "[图片]")):
            with self.subTest(content=content):
                self.assertIsNone(
                    router.handle(self.yang_msg(f"s{index}", content)),
                )
        self.assertEqual(summarizer.chat_calls, [])

        # None of the skips reserved the cooldown, so a real turn still goes.
        reply = router.handle(self.yang_msg("real", "今天打游戏吗朋友们"))
        self.assertEqual(reply, "嗯，在。")

    def test_media_placeholders_are_skipped_after_stripping_the_prefix(self):
        router, summarizer = self.yang_router()

        reply = router.handle(
            self.yang_msg("m1", "wxid_abc123:\n[表情][图片]"),
        )

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_quotes_are_skipped_unless_the_bot_is_addressed(self):
        router, summarizer = self.yang_router()

        quoted = router.handle(self.yang_msg(
            "q1", "今天打游戏吗朋友们", quoted_content="某句",
            quoted_sender_name="土豆",
        ))
        to_bot = router.handle(self.yang_msg(
            "q2", "嗯", is_reply_to_bot=True, quoted_content="你好",
            quoted_sender_name=YANG_BOT,
        ))

        self.assertIsNone(quoted)
        self.assertEqual(to_bot, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_at_name_in_the_text_counts_as_direct_address(self):
        router, summarizer = self.yang_router()
        router.handle(self.yang_msg("a0", "今天打游戏吗朋友们"))   # uses cooldown

        direct = router.handle(self.yang_msg("a1", "@杨宇靖 嗯"))

        # Direct address bypasses both the short-reaction and the cooldown
        # filters even though the platform flag is_at_mentioned is False.
        self.assertEqual(direct, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[-1]["message"], "@杨宇靖 嗯")
        self.assertTrue(summarizer.chat_calls[-1]["ambient"])

    def test_persona_name_also_counts_as_direct_address(self):
        router, summarizer = self.yang_router(chat_persona_name="宇靖")
        router.handle(self.yang_msg("a0", "今天打游戏吗朋友们"))

        direct = router.handle(self.yang_msg("a1", "@宇靖 在吗"))

        self.assertEqual(direct, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 2)

    def test_meta_discussion_words_are_matched_case_insensitively(self):
        router, summarizer = self.yang_router()

        for index, content in enumerate((
            "ChatGPT 比你强", "你是机器人吧", "这提示词写得不错", "人设崩了",
            "上下文没看懂", "这人机味太重了",
        )):
            with self.subTest(content=content):
                self.assertIsNone(router.handle(
                    self.yang_msg(f"meta{index}", content),
                ))
        self.assertEqual(summarizer.chat_calls, [])

    def test_cooldown_lasts_45_seconds_and_is_per_chat(self):
        router, summarizer = self.yang_router()
        router.handle(self.yang_msg("c0", "今天打游戏吗朋友们"))

        self.clock.advance(AMBIENT_COOLDOWN_SEC - 0.1)
        early = router.handle(self.yang_msg("c1", "今天打游戏吗朋友们"))
        other_chat = router.handle(self.yang_msg(
            "c2", "今天打游戏吗朋友们", chat_id="other@chatroom",
            group_name=YANG_GROUP,
        ))
        self.clock.advance(0.1)
        on_time = router.handle(self.yang_msg("c3", "今天打游戏吗朋友们"))

        self.assertIsNone(early)
        self.assertEqual(other_chat, "嗯，在。")
        self.assertEqual(on_time, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 3)

    def test_a_silent_model_turn_still_consumes_the_cooldown(self):
        router, summarizer = self.yang_router()
        summarizer.reply = ""
        summarizer.last_chat_reply_was_intentional_silence = True
        silent = router.handle(self.yang_msg("c0", "今天打游戏吗朋友们"))

        summarizer.reply = "好呀"
        summarizer.last_chat_reply_was_intentional_silence = False
        blocked = router.handle(self.yang_msg("c1", "今天打游戏吗朋友们"))

        # PINNED (known issue, expected to change in Phase N): the cooldown is
        # reserved BEFORE the model call, so staying silent costs the window.
        self.assertIsNone(silent)
        self.assertIsNone(blocked)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_always_reply_group_skips_every_ambient_filter(self):
        router, summarizer = self.yang_router(always_reply="强制群")

        first = router.handle(self.yang_msg("f1", "嗯", group_name="强制群"))
        second = router.handle(self.yang_msg("f2", "666", group_name="强制群"))

        self.assertEqual((first, second), ("嗯，在。", "嗯，在。"))
        self.assertEqual(
            [c["force_reply"] for c in summarizer.chat_calls], [True, True],
        )

    def test_direct_address_with_no_text_is_ignored_in_the_ambient_group(self):
        router, summarizer = self.yang_router()

        reply = router.handle(self.yang_msg("a1", "   ", at=True))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.chat_calls, [])

    def test_always_reply_group_fallback_strings(self):
        router, summarizer = self.yang_router(always_reply="强制群")
        summarizer.reply = ""
        blank = router.handle(self.yang_msg("f1", "嗯", group_name="强制群"))

        summarizer.error = RuntimeError("boom")
        failed = router.handle(self.yang_msg("f2", "嗯", group_name="强制群"))

        self.assertEqual(blank, "啊？")
        self.assertEqual(failed, "刚没看懂")

    def test_always_reply_group_sends_a_placeholder_for_empty_content(self):
        router, summarizer = self.yang_router(always_reply="强制群")

        router.handle(self.yang_msg("f1", "   ", group_name="强制群"))

        self.assertEqual(summarizer.chat_calls[0]["message"], "[空消息]")

    def test_target_group_is_matched_by_chat_id_and_always_reply_wins(self):
        router, _ = self.yang_router(always_reply="强制群")

        by_chat_id = router._full_feed_mode(
            self.yang_msg("m1", "x", group_name="别名", chat_id="强制群"),
        )
        router, _ = self.yang_router(always_reply=YANG_GROUP)
        both = router._full_feed_mode(self.yang_msg("m2", "x"))

        self.assertEqual(by_chat_id, "always_reply")
        self.assertEqual(both, "always_reply")

    def test_full_feed_mode_exclusions(self):
        router, _ = self.yang_router()
        cases = {
            "private chat": self.yang_msg("m1", "x", is_group=False),
            "system join": self.yang_msg("m2", "x", is_system_join=True),
            "blank sender id": self.yang_msg("m3", "x", sender_id=" "),
            "other group": self.yang_msg("m4", "x", group_name="别的群"),
            "no timestamp": self.yang_msg("m5", "x", timestamp=0),
            "stale by one second": self.yang_msg(
                "m6", "x", timestamp=NOW - ALL_MESSAGES_MAX_AGE_SEC - 1,
            ),
        }

        for name, message in cases.items():
            with self.subTest(name):
                self.assertIsNone(router._full_feed_mode(message))

        at_limit = self.yang_msg(
            "m7", "x", timestamp=NOW - ALL_MESSAGES_MAX_AGE_SEC,
        )
        self.assertEqual(router._full_feed_mode(at_limit), "ambient")
        self.assertTrue(router._full_feed_mode(at_limit) is not None)

    def test_no_configured_group_disables_the_mode(self):
        router, _ = self.make_router(
            chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
            yang_yujing_group_name="", yang_yujing_always_reply_group_name="",
        )

        self.assertIsNone(
            router._full_feed_mode(self.yang_msg("m1", "x")),
        )

    def test_full_feed_route_precedes_help_fun_admin_and_todo(self):
        admin = MagicMock(handle=MagicMock(return_value="HELP"))
        router, summarizer = self.yang_router(
            admin=admin, admin_wxid="wxid_user1", todo_enabled=True,
        )

        with patch("src.fun.draw_lots", return_value="LOTS") as lots:
            lots_reply = router.handle(
                self.yang_msg("y1", "@杨宇靖 抽签", at=True),
            )
        help_reply = router.handle(
            self.yang_msg("y2", "@杨宇靖 帮助", at=True),
        )
        todo_reply = router.handle(
            self.yang_msg("y3", "@杨宇靖 记一下 买菜", at=True),
        )

        # PINNED (known issue, expected to change in Phase N): in the Yang
        # target group every command word goes to the model, because the
        # full-feed branch returns before the command chain.
        self.assertEqual((lots_reply, help_reply, todo_reply), ("嗯，在。",) * 3)
        lots.assert_not_called()
        admin.handle.assert_not_called()
        self.assertEqual(
            [c["message"] for c in summarizer.chat_calls],
            ["@杨宇靖 抽签", "@杨宇靖 帮助", "@杨宇靖 记一下 买菜"],
        )

    def test_full_feed_route_precedes_the_quoted_image_branch(self):
        router, summarizer = self.yang_router()
        router._store.insert_message(self.yang_msg(
            "img-1", "[图片]", msg_type=3, server_id="srv-1",
            timestamp=NOW - 30,
        ))

        with patch.object(
            router_module, "find_recent_image_path", return_value="/media/a.jpg",
        ):
            router.handle(self.yang_msg(
                "q1", "杨宇靖，这张图是什么意思", quoted_server_id="srv-1",
                quoted_content="[图片]", at=True,
            ))

        self.assertEqual(summarizer.image_calls, [])
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_sticker_is_replaced_by_the_media_placeholder(self):
        router, summarizer = self.yang_router(always_reply="强制群")

        router.handle(self.yang_msg(
            "s1", "deadbeef" * 20, msg_type=47, group_name="强制群",
        ))

        self.assertEqual(summarizer.chat_calls[0]["message"], "[图片]")

    def test_blank_reply_to_a_direct_address_uses_the_direct_fallback(self):
        router, summarizer = self.yang_router()
        summarizer.reply = ""

        reply = router.handle(
            self.yang_msg("y1", "@杨宇靖 在吗", at=True),
        )

        # The Yang route does not strip "@name", so the prompt is "@杨宇靖 在吗"
        # and the "在吗" presence shortcut does not apply.
        self.assertEqual(reply, "我刚才没接住，再说一次好不好")

    def test_ambient_model_errors_never_produce_a_visible_message(self):
        router, summarizer = self.yang_router()
        summarizer.error = RuntimeError("boom")

        ambient = router.handle(self.yang_msg("y1", "今天打游戏吗朋友们"))
        direct = router.handle(self.yang_msg("y2", "@杨宇靖 你觉得呢", at=True))

        self.assertIsNone(ambient)
        self.assertIsNone(direct)

    def test_private_chat_in_the_yang_profile_uses_the_normal_private_route(self):
        router, summarizer = self.yang_router()

        reply = router.handle(self.private_msg("p1", "你好"))

        self.assertEqual(reply, "嗯，在。")
        self.assertFalse(summarizer.chat_calls[0]["ambient"])

    def test_duplicates_are_still_dropped_and_consolidation_still_checked(self):
        router, summarizer = self.yang_router()
        message = self.yang_msg("y1", "今天打游戏吗朋友们")

        first = router.handle(message)
        again = router.handle(dict(message))

        self.assertEqual(first, "嗯，在。")
        self.assertIsNone(again)
        self.assertEqual(len(summarizer.chat_calls), 1)
        self.assertEqual(self.consolidation_checks, [CHAT])

    def test_ambient_reply_still_registers_listening_state_nobody_reads(self):
        router, _ = self.yang_router()

        router.handle(self.yang_msg("y1", "今天打游戏吗朋友们"))

        # The full-feed branch returns before sticky/follow-up consumption, so
        # these entries only ever expire.
        self.assertEqual(len(router._sticky), 1)
        self.assertEqual(len(router._followup._entries), 1)


# ── proactive path ─────────────────────────────────────────────────────

class ProactivePathTests(RouterTestCase):
    def proactive_router(self, **config):
        # Follow-up listening is switched off so that only the ambient gate
        # decides whether a plain message is answered.
        config.setdefault("proactive_enabled", True)
        config.setdefault("followup_listen_enabled", False)
        return self.make_router(**config)

    def seed_context(self, router):
        store = router._store
        store.insert_message(self.other_msg(
            "pre-1", "前面的话1", timestamp=NOW - 10,
        ))
        store.insert_message(self.other_msg(
            "pre-2", "前面的话2", timestamp=NOW - 9,
        ))

    def test_summarizer_is_not_called_while_the_gate_says_no(self):
        router, summarizer = self.proactive_router()
        gate = self.gate(router, allow=False)

        reply = router.handle(self.group_msg("m1", "闲聊一句", at=False))

        self.assertIsNone(reply)
        gate.assert_called_once()
        self.assertEqual(summarizer.proactive_calls, [])
        self.assertEqual(len(router._sticky), 0)

    def test_gate_passing_without_a_mode_is_treated_as_no(self):
        router, summarizer = self.proactive_router()
        self.gate(router, allow=True, mode=None)

        reply = router.handle(self.group_msg("m1", "闲聊一句", at=False))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.proactive_calls, [])

    def test_allowed_turn_calls_the_summarizer_with_the_mode_and_context(self):
        router, summarizer = self.proactive_router()
        self.seed_context(router)
        router._store.upsert_group_memory(CHAT, "群日记", 1, "m")
        router._store.upsert_group_fact(CHAT, "宇肆", "alias", "次元")
        mode = casual_mode(context_count=10)
        self.gate(router, allow=True, mode=mode)

        reply = router.handle(self.group_msg("m1", "闲聊一句", at=False))

        self.assertEqual(reply, "接一句")
        call = summarizer.proactive_calls[0]
        self.assertEqual(
            sorted(call),
            ["bot_name", "context_messages", "group_memory", "group_name",
             "mode"],
        )
        self.assertIs(call["mode"], mode)
        self.assertEqual(call["bot_name"], BOT)
        self.assertEqual(call["group_name"], "测试群")
        self.assertEqual(
            [m["content"] for m in call["context_messages"]],
            ["前面的话1", "前面的话2", "闲聊一句"],
        )
        self.assertEqual(
            call["group_memory"],
            "群日记\n\n## 固定事实/称呼记忆（优先级高于普通聊天日记）\n"
            "- 宇肆 在这个群里的固定称呼/别名是「次元」。",
        )
        self.assertEqual(summarizer.chat_calls, [])

    def test_context_window_is_the_rate_window_and_mode_limit(self):
        router, summarizer = self.proactive_router(
            proactive_rate_window_sec=120,
        )
        store = router._store
        store.insert_message(self.other_msg("w0", "太早", timestamp=NOW - 121))
        store.insert_message(self.other_msg("w1", "刚好", timestamp=NOW - 120))
        store.insert_message(self.other_msg("w2", "较新", timestamp=NOW - 60))
        self.gate(router, allow=True, mode=casual_mode(context_count=2))

        router.handle(self.group_msg("m1", "现在", at=False))

        # Fixed in Phase 1: the store limit keeps the NEWEST rows of the
        # window, so a busy group's context ends with the latest messages
        # (including the one that triggered the turn).
        self.assertEqual(
            [m["content"] for m in summarizer.proactive_calls[0]["context_messages"]],
            ["较新", "现在"],
        )

    def test_reply_is_cleaned_and_arms_listening_for_the_last_speaker(self):
        router, summarizer = self.proactive_router()
        self.seed_context(router)
        summarizer.proactive_reply = "**好**呀 😀"
        self.gate(router, allow=True, mode=casual_mode())

        reply = router.handle(self.group_msg("m1", "闲聊一句", at=False))
        follow_up = router.handle(self.group_msg("m2", "我又说话了", at=False))

        # PINNED (known issue, expected to change in Phase N): an ambient
        # remark registers sticky listening for whoever spoke last, so that
        # person's next message is treated as addressed to the bot.
        self.assertEqual(reply, "好呀")
        self.assertEqual(follow_up, "嗯，在。")
        self.assertEqual(len(summarizer.proactive_calls), 1)
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_speech_and_silence_are_reported_to_the_gate(self):
        router, summarizer = self.proactive_router()
        self.seed_context(router)
        self.gate(router, allow=True, mode=casual_mode())
        gate = router._proactive

        with patch.object(gate, "record_speech") as speech, \
                patch.object(gate, "record_eval") as evaluated, \
                patch.object(gate, "record_silence") as silence:
            router.handle(self.group_msg("m1", "第一句", at=False))
            self.assertEqual(speech.call_args_list[0].args, (CHAT,))
            evaluated.assert_not_called()
            silence.assert_not_called()

            summarizer.proactive_reply = ""
            router.handle(self.other_msg("m2", "第二句"))
            evaluated.assert_called_once_with(CHAT)
            silence.assert_called_once_with(CHAT)
            self.assertEqual(speech.call_count, 1)

    def test_model_failure_is_silent_and_not_reported(self):
        router, summarizer = self.proactive_router()
        self.seed_context(router)
        summarizer.error = RuntimeError("boom")
        self.gate(router, allow=True, mode=casual_mode())
        gate = router._proactive

        with patch.object(gate, "record_speech") as speech, \
                patch.object(gate, "record_eval") as evaluated, \
                patch.object(gate, "record_silence") as silence:
            reply = router.handle(self.group_msg("m1", "闲聊一句", at=False))

        self.assertIsNone(reply)
        speech.assert_not_called()
        evaluated.assert_not_called()
        silence.assert_not_called()

    def test_empty_context_skips_the_model(self):
        router, summarizer = self.proactive_router()
        self.gate(router, allow=True, mode=casual_mode())

        with patch.object(router._store, "get_messages_since", return_value=[]):
            reply = router.handle(self.group_msg("m1", "闲聊一句", at=False))

        self.assertIsNone(reply)
        self.assertEqual(summarizer.proactive_calls, [])

    def test_context_names_are_resolved_without_touching_the_database(self):
        router, summarizer = self.proactive_router(
            nicks=StubNicks({"wxid_user2": "土豆哥"}),
        )
        self.seed_context(router)
        self.gate(router, allow=True, mode=casual_mode())

        router.handle(self.group_msg("m1", "闲聊一句", at=False))

        names = [
            m["sender_name"]
            for m in summarizer.proactive_calls[0]["context_messages"]
        ]
        self.assertEqual(names, ["土豆哥", "土豆哥", "小明"])
        stored = router._store.conn.execute(
            "SELECT DISTINCT sender_name FROM messages WHERE sender_id = ?",
            ("wxid_user2",),
        ).fetchall()
        self.assertEqual([row[0] for row in stored], ["土豆"])

    def test_gate_is_consulted_once_per_ambient_group_message_only(self):
        router, summarizer = self.make_router(
            keywords=["总结一下"], followup_listen_enabled=False,
        )
        gate = self.gate(router)

        router.handle(self.group_msg("a1", "路过", at=False))
        router.handle(self.other_msg("a2", "再路过"))
        # None of these may reach the gate:
        router.handle(self.group_msg("b1", "@林小满 你好"))             # mention
        router.handle(self.group_msg("b2", "继续", at=False))           # sticky
        router.handle(self.other_msg("b3", "总结一下"))                  # keyword
        router.handle(self.private_msg("b4", "私聊"))                    # private
        router.handle(self.sticker_msg("b5", sender_id="wxid_user3"))   # image
        router.handle(self.other_msg(                                    # quote
            "b6", "哈", quoted_content="某句", quoted_sender_name="某人",
        ))

        self.assertEqual(gate.call_count, 2)
        self.assertEqual(summarizer.proactive_calls, [])

    def test_feishu_auto_export_runs_for_ambient_messages_and_may_fail(self):
        feishu = MagicMock()
        feishu.is_export_command.return_value = False
        feishu.maybe_auto_export.side_effect = RuntimeError("feishu down")
        router, _ = self.make_router(feishu=feishu)
        gate = self.gate(router)

        reply = router.handle(self.group_msg("m1", "闲聊", at=False))

        self.assertIsNone(reply)
        feishu.maybe_auto_export.assert_called_once()
        gate.assert_called_once()                    # failure does not stop it

    def test_feishu_auto_export_is_not_called_for_mentions(self):
        feishu = MagicMock()
        feishu.is_export_command.return_value = False
        router, _ = self.make_router(feishu=feishu)

        router.handle(self.group_msg("m1", "@林小满 你好"))

        feishu.maybe_auto_export.assert_not_called()


# ── fallback strings ───────────────────────────────────────────────────

class FallbackReplyTests(RouterTestCase):
    def test_presence_prompts_get_the_short_acknowledgement(self):
        router, summarizer = self.make_router()
        summarizer.reply = ""

        for index, content in enumerate(("在吗", "在不在", "在")):
            with self.subTest(content=content):
                reply = router.handle(
                    self.group_msg(f"m{index}", f"@林小满 {content}"),
                )
                self.assertEqual(reply, "嗯，在的")

    def test_failures_on_a_presence_prompt_also_get_the_short_acknowledgement(self):
        router, summarizer = self.make_router()
        summarizer.error = RuntimeError("boom")

        self.assertEqual(
            router.handle(self.group_msg("m1", "@林小满")), "嗯，在的",
        )

    def test_whitespace_or_none_counts_as_a_blank_reply(self):
        router, summarizer = self.make_router()

        for index, blank in enumerate(("   \n ", None)):
            with self.subTest(blank=blank):
                summarizer.reply = blank
                reply = router.handle(
                    self.group_msg(f"m{index}", "@林小满 你怎么看"),
                )
                self.assertEqual(reply, "我刚才没接住，再说一次好不好")

    def test_intentional_silence_beats_the_direct_fallback(self):
        router, summarizer = self.make_router()
        summarizer.reply = ""
        summarizer.last_chat_reply_was_intentional_silence = True

        reply = router.handle(self.group_msg("m1", "@林小满 你怎么看"))

        self.assertIsNone(reply)

    def test_failures_without_a_direct_address_use_the_generic_message(self):
        router, summarizer = self.make_router(followup_listen_enabled=True)
        router.handle(self.group_msg("m1", "@林小满 推荐几首歌"))
        summarizer.error = RuntimeError("boom")

        sticky = router.handle(self.group_msg("m2", "你怎么看", at=False))
        followup = router.handle(self.other_msg("m3", "什么歌呀"))

        # PINNED (known issue, expected to change in Phase N): a model error on
        # an indirect (sticky / follow-up) turn is answered with a visible
        # error string, although such turns stay silent when the model merely
        # returns nothing.
        self.assertEqual(sticky, "大脑短路了，稍等再试～")
        self.assertEqual(followup, "大脑短路了，稍等再试～")

    def test_blank_replies_without_a_direct_address_stay_silent(self):
        router, summarizer = self.make_router(followup_listen_enabled=True)
        router.handle(self.group_msg("m1", "@林小满 推荐几首歌"))
        summarizer.reply = ""

        sticky = router.handle(self.group_msg("m2", "你怎么看", at=False))
        router.handle(self.group_msg("m3", "@林小满 再来一个"))
        summarizer.reply = ""
        followup = router.handle(self.other_msg("m4", "什么歌呀"))

        self.assertIsNone(sticky)
        self.assertIsNone(followup)

    def test_needs_direct_fallback_flags(self):
        check = MessageRouter._needs_direct_fallback

        self.assertFalse(check({}))
        self.assertTrue(check({"is_at_mentioned": True}))
        self.assertTrue(check({"_kw_triggered": True}))
        self.assertTrue(check({"_private_triggered": True}))
        self.assertFalse(check({"_sticky_triggered": True}))
        self.assertFalse(check({"_followup_listen_triggered": True}))


# ── _finish_visible_reply / reply cleaning ────────────────────────────

class FinishVisibleReplyTests(RouterTestCase):
    def finish(self, text, **kwargs):
        router, _ = self.make_router()
        return router._finish_visible_reply(
            self.group_msg("m1", "@林小满 在吗"), text, **kwargs,
        )

    def test_markdown_markers_are_removed(self):
        self.assertEqual(
            self.finish("**加粗** 和 *斜体* 以及 ~~删除~~ 和 `代码`"),
            "加粗 和 斜体 以及 删除 和 代码",
        )

    def test_unmatched_markers_and_block_syntax_are_left_alone(self):
        self.assertEqual(self.finish("**未闭合"), "**未闭合")
        self.assertEqual(self.finish("# 标题\n- 列表项"), "# 标题\n- 列表项")

    def test_asterisks_between_numbers_are_eaten_as_italics(self):
        # PINNED (known issue, expected to change in Phase N): the italic
        # regex also fires on arithmetic, so "2*3*4" becomes "234".
        self.assertEqual(self.finish("2*3*4"), "234")

    def test_blank_lines_and_runs_of_spaces_collapse_to_one_space(self):
        # PINNED (known issue, expected to change in Phase N): any whitespace
        # run of two or more characters becomes a single space, which also
        # flattens paragraph breaks and indentation.
        self.assertEqual(self.finish("行1\n\n行2"), "行1 行2")
        self.assertEqual(self.finish("缩进    四个空格"), "缩进 四个空格")
        self.assertEqual(self.finish("行1\n行2"), "行1\n行2")

    def test_space_before_chinese_punctuation_is_removed(self):
        self.assertEqual(self.finish("你好 ，世界 ！"), "你好，世界！")

    def test_emoji_and_kaomoji_removal_boundaries(self):
        cases = {
            "星星⭐月亮🌙": "星星月亮",
            "✅完成": "完成",
            "哈哈 xD 好": "哈哈 好",
            "T_T 呜呜": "呜呜",
            "（≧▽≦）好": "好",
            "答案是 :P": "答案是",
            "时间 8:30 见": "时间 8:30 见",
            "我8点去": "我8点去",
            "（下午）见": "（下午）见",
            "(笑)": "(笑)",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.finish(raw), expected)

    def test_emoticon_glued_to_chinese_text_is_kept(self):
        # PINNED (known issue, expected to change in Phase N): the ASCII
        # kaomoji pattern refuses to match when it touches a CJK character.
        self.assertEqual(self.finish("好的:)"), "好的:)")

    def test_reply_that_cleans_to_nothing_is_dropped_and_registers_nothing(self):
        router, _ = self.make_router()
        message = self.group_msg("m1", "@林小满 在吗")

        for raw in ("😀", "😀 😀", "(￣▽￣)", "   ", "", None):
            with self.subTest(raw=raw):
                self.assertIsNone(router._finish_visible_reply(message, raw))

        self.assertEqual(len(router._sticky), 0)
        self.assertEqual(router._followup._entries, {})
        self.assertEqual(router._image_followup._entries, {})

    def test_group_reply_registers_sticky_follow_up_and_image_window(self):
        router, _ = self.make_router()
        message = self.group_msg("m1", "@林小满 在吗")

        reply = router._finish_visible_reply(message, "**好**呀 😀")

        self.assertEqual(reply, "好呀")
        self.assertEqual(len(router._sticky), 1)
        self.assertEqual(router._followup._entries[CHAT].bot_reply, "好呀")
        entry = router._image_followup._entries[(CHAT, "wxid_user1")]
        self.assertEqual(entry.bot_reply, "好呀")
        self.assertEqual(entry.source_timestamp, NOW)

    def test_private_reply_registers_only_the_image_window(self):
        router, _ = self.make_router()
        message = self.private_msg("p1", "你好")

        router._finish_visible_reply(message, "好呀")

        self.assertEqual(len(router._sticky), 0)
        self.assertEqual(router._followup._entries, {})
        self.assertEqual(len(router._image_followup._entries), 1)

    def test_image_window_can_be_left_closed(self):
        router, _ = self.make_router()
        message = self.group_msg("m1", "@林小满 在吗")

        router._finish_visible_reply(message, "好呀", open_image_followup=False)

        self.assertEqual(router._image_followup._entries, {})
        self.assertEqual(len(router._sticky), 1)

    def test_listeners_can_be_disabled_independently(self):
        router, _ = self.make_router(
            sticky_mention_enabled=False, followup_listen_enabled=False,
            image_followup_enabled=False,
        )

        reply = router._finish_visible_reply(
            self.group_msg("m1", "@林小满 在吗"), "好呀",
        )

        self.assertEqual(reply, "好呀")
        self.assertIsNone(router._sticky)
        self.assertIsNone(router._followup)
        self.assertIsNone(router._image_followup)

    def test_static_cleaners_can_be_used_directly(self):
        self.assertEqual(MessageRouter._strip_markdown("  **x**  "), "x")
        self.assertEqual(MessageRouter._strip_emoticons("a  b ，c"), "a b，c")


# ── _history_recall_terms ──────────────────────────────────────────────

class HistoryRecallTermsTests(RouterTestCase):
    def test_extracts_content_terms_only_from_a_recall_question(self):
        router, _ = self.make_router()
        cases = [
            ("之前谁说过 cy 是南通人？", ["cy", "南通人"]),
            ("上次讲的那个游戏叫什么", ["游戏叫"]),
            ("还记得我们吃过的火锅店名字吗", ["我们吃过", "火锅店名字", "锅店名字"]),
            ("聊天记录里有没有提到过北京烤鸭和上海小笼包",
             ["提到过北京烤鸭和上海小笼包", "海小笼包"]),
            ("记不记得谁提过一个特别特别长的中文句子用来测试",
             ["一个特别特别长", "别特别长", "中文句子用来测试", "用来测试"]),
        ]

        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(router._history_recall_terms(text), expected)

    def test_text_without_recall_markers_or_terms_yields_nothing(self):
        router, _ = self.make_router()

        for text in ("今天天气", "", None, "   ", "之前", "谁说过", "之前说过 a"):
            with self.subTest(text=text):
                self.assertEqual(router._history_recall_terms(text), [])

    def test_ascii_terms_are_deduplicated_case_insensitively(self):
        router, _ = self.make_router()

        terms = router._history_recall_terms(
            "原话是什么 abc_def hello WORLD hello",
        )

        self.assertEqual(terms, ["abc_def", "hello", "WORLD"])

    def test_at_most_six_terms_are_returned(self):
        router, _ = self.make_router()

        terms = router._history_recall_terms(
            "以前说过 aa bb cc dd ee ff gg hh",
        )

        self.assertEqual(terms, ["aa", "bb", "cc", "dd", "ee", "ff"])


# ── history recall context ─────────────────────────────────────────────

class HistoryRecallContextTests(RouterTestCase):
    class HistoryStore(MinimalStore):
        def __init__(self, records=None, fail=False):
            self.records = records or []
            self.fail = fail
            self.calls = []

        def search_chat_history(self, chat_id, terms, limit=8):
            self.calls.append((chat_id, list(terms), limit))
            if self.fail:
                raise RuntimeError("search down")
            return self.records

    def test_records_are_formatted_one_per_line(self):
        records = [
            {"sender_id": "wxid_a", "sender_name": "阿明",
             "content": "cy   是\n南通人", "timestamp": 1760000000},
            {"sender_id": "wxid_b", "sender_name": "",
             "content": "无名氏说的", "timestamp": 0},
            {"sender_id": "wxid_c", "sender_name": "x", "content": "   ",
             "timestamp": 5},
            {"sender_id": "wxid_d", "sender_name": "老D",
             "content": "长" * 300, "timestamp": 1760000100},
            {"sender_id": "wxid_e", "sender_name": "原名",
             "content": "改过名字", "timestamp": 1760000200},
        ]
        store = self.HistoryStore(records)
        router, _ = self.make_router(
            store=store, nicks=StubNicks({"wxid_e": "新名"}),
        )

        context = router._get_history_recall_context(
            self.group_msg("m1", "x"), "之前谁说过 cy 是南通人",
        )

        def when(ts):
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        self.assertEqual(store.calls, [(CHAT, ["cy", "南通人"], 8)])
        self.assertEqual(context.split("\n")[0], "【历史聊天检索结果】")
        self.assertEqual(
            context.split("\n")[2:],
            [
                f"- {when(1760000000)} 阿明：cy 是 南通人",
                "- 时间未知 群友：无名氏说的",
                f"- {when(1760000100)} 老D：" + "长" * 180,
                f"- {when(1760000200)} 新名：改过名字",
            ],
        )

    def test_records_that_are_all_blank_yield_no_context(self):
        store = self.HistoryStore([
            {"sender_id": "wxid_a", "sender_name": "阿明", "content": "  "},
            {"sender_id": "wxid_b", "sender_name": "x", "content": ""},
        ])
        router, _ = self.make_router(store=store)

        context = router._get_history_recall_context(
            self.group_msg("m1", "x"), "之前谁说过 cy 是南通人",
        )

        self.assertEqual(context, "")
        self.assertEqual(len(store.calls), 1)

    def test_private_chats_never_search_history(self):
        store = self.HistoryStore([{"content": "x"}])
        router, _ = self.make_router(store=store)

        context = router._get_history_recall_context(
            self.private_msg("p1", "x"), "之前谁说过 cy 是南通人",
        )

        self.assertEqual(context, "")
        self.assertEqual(store.calls, [])

    def test_non_recall_text_does_not_search(self):
        store = self.HistoryStore([{"content": "x"}])
        router, _ = self.make_router(store=store)

        context = router._get_history_recall_context(
            self.group_msg("m1", "x"), "今天吃什么",
        )

        self.assertEqual(context, "")
        self.assertEqual(store.calls, [])

    def test_empty_results_and_search_errors_yield_no_context(self):
        for store in (self.HistoryStore([]), self.HistoryStore(fail=True)):
            with self.subTest(fail=store.fail):
                router, _ = self.make_router(store=store)
                context = router._get_history_recall_context(
                    self.group_msg("m1", "x"), "之前谁说过 cy 是南通人",
                )
                self.assertEqual(context, "")

    def test_store_without_search_support_yields_no_context(self):
        router, _ = self.make_router(store=MinimalStore())

        context = router._get_history_recall_context(
            self.group_msg("m1", "x"), "之前谁说过 cy 是南通人",
        )

        self.assertEqual(context, "")

    def test_context_is_appended_to_the_model_message(self):
        store = self.HistoryStore([
            {"sender_id": "wxid_a", "sender_name": "阿明",
             "content": "cy 是南通人", "timestamp": 0},
        ])
        router, summarizer = self.make_router(store=store)

        router.handle(self.group_msg("m1", "@林小满 之前谁说过 cy 是南通人？"))

        message = summarizer.chat_calls[0]["message"]
        self.assertTrue(message.startswith("之前谁说过 cy 是南通人？\n\n【历史聊天检索结果】"))
        self.assertTrue(message.endswith("- 时间未知 阿明：cy 是南通人"))


# ── group facts / group memory text ────────────────────────────────────

class GroupFactsTextTests(RouterTestCase):
    class FactsStore(MinimalStore):
        def __init__(self, facts, memory=None, fail=False):
            self.facts = facts
            self.memory = memory
            self.fail = fail
            self.fact_limits = []

        def get_group_facts(self, chat_id, limit=30):
            self.fact_limits.append(limit)
            if self.fail:
                raise RuntimeError("facts down")
            return self.facts

        def get_group_memory(self, chat_id):
            return self.memory

    HEADER = "## 固定事实/称呼记忆（优先级高于普通聊天日记）"

    def test_only_alias_facts_with_subject_and_object_are_rendered(self):
        facts = [
            {"subject": "", "predicate": "alias", "object": "x"},
            {"subject": "甲", "predicate": "alias", "object": ""},
            {"subject": "乙", "predicate": "home", "object": "南通"},
            {"subject": " 丙 ", "predicate": " alias ", "object": " 丙丙 "},
            {"subject": "丁", "predicate": "alias", "object": "丁丁"},
        ]
        store = self.FactsStore(facts)
        router, _ = self.make_router(store=store)

        text = router._get_group_facts_text(CHAT)

        self.assertEqual(
            text,
            self.HEADER + "\n"
            "- 丙 在这个群里的固定称呼/别名是「丙丙」。\n"
            "- 丁 在这个群里的固定称呼/别名是「丁丁」。",
        )
        self.assertEqual(store.fact_limits, [30])

    def test_at_most_twenty_lines_are_rendered(self):
        facts = [
            {"subject": f"人{i}", "predicate": "alias", "object": f"名{i}"}
            for i in range(25)
        ]
        router, _ = self.make_router(store=self.FactsStore(facts))

        lines = router._get_group_facts_text(CHAT).split("\n")

        self.assertEqual(len(lines), 21)             # header + 20 facts
        self.assertTrue(lines[-1].startswith("- 人19 "))

    def test_no_renderable_facts_or_a_failing_store_give_an_empty_string(self):
        scenarios = {
            "no facts": self.FactsStore([]),
            "only other predicates": self.FactsStore(
                [{"subject": "a", "predicate": "home", "object": "b"}],
            ),
            "store raises": self.FactsStore([], fail=True),
            "no fact support": MinimalStore(),
        }
        for name, store in scenarios.items():
            with self.subTest(name):
                router, _ = self.make_router(store=store)
                self.assertEqual(router._get_group_facts_text(CHAT), "")

    def test_group_memory_combines_diary_and_facts(self):
        facts = [{"subject": "a", "predicate": "alias", "object": "b"}]
        line = "- a 在这个群里的固定称呼/别名是「b」。"
        cases = {
            "diary+facts": (
                {"memory_text": "日记"}, facts,
                f"日记\n\n{self.HEADER}\n{line}",
            ),
            "diary only": ({"memory_text": "日记"}, [], "日记"),
            "facts only": (None, facts, f"{self.HEADER}\n{line}"),
            "nothing": (None, [], ""),
        }
        for name, (memory, stored_facts, expected) in cases.items():
            with self.subTest(name):
                router, _ = self.make_router(
                    store=self.FactsStore(stored_facts, memory=memory),
                )
                self.assertEqual(router._get_group_memory(CHAT), expected)

    def test_ambient_chat_only_drops_the_diary_for_the_yang_profile(self):
        facts = [{"subject": "a", "predicate": "alias", "object": "b"}]
        store = self.FactsStore(facts, memory={"memory_text": "DIARY-BODY"})
        custom, _ = self.make_router(store=store, chat_persona_profile="custom")
        yang, _ = self.make_router(
            store=store, chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
        )

        self.assertEqual(
            custom._get_group_memory_for_chat(CHAT, ambient=True),
            custom._get_group_memory(CHAT),
        )
        self.assertEqual(
            yang._get_group_memory_for_chat(CHAT, ambient=True),
            yang._get_group_facts_text(CHAT),
        )
        self.assertNotIn(
            "DIARY-BODY", yang._get_group_memory_for_chat(CHAT, ambient=True),
        )
        self.assertIn(
            "DIARY-BODY", yang._get_group_memory_for_chat(CHAT, ambient=False),
        )


# ── _message_for_ai_chat and helpers ───────────────────────────────────

class MessageForAiChatTests(unittest.TestCase):
    build = staticmethod(MessageRouter._message_for_ai_chat)

    FOLLOWUP_TAIL = (
        "请结合最近上下文判断这是不是在对你说；"
        "如果不是，回复空白；如果是，只接这一句自然回复。"
    )

    def test_follow_up_triggered_message_names_the_previous_reply(self):
        message = self.build({
            "_followup_listen_triggered": True,
            "_followup_bot_reply": " 好呀 ",
            "_followup_listen_reason": "short-question",
            "sender_name": "土豆",
        }, "吗")

        self.assertEqual(
            message,
            "你刚才在群里回复：「好呀」。"
            "现在 土豆 发了一句可能是在接你话的短消息：「吗」。"
            "本地候选原因：short-question。" + self.FOLLOWUP_TAIL,
        )

    def test_follow_up_defaults_when_reason_sender_and_reply_are_missing(self):
        message = self.build({"_followup_listen_triggered": True}, "吗")

        self.assertEqual(
            message,
            "你刚才在群里回复：「」。"
            "现在 群友 发了一句可能是在接你话的短消息：「吗」。"
            "本地候选原因：像是在接上一句。" + self.FOLLOWUP_TAIL,
        )

    def test_sticky_triggered_message_wraps_the_latest_text(self):
        message = self.build({"_sticky_triggered": True}, "你怎么看")

        self.assertEqual(
            message,
            "这条消息没有直接@你，但来自刚才正在和你连续聊天的人。"
            "请结合最近上下文判断是不是还在对你说；如果只是和别人说，回复空白。"
            "最新消息：你怎么看",
        )

    def test_follow_up_wins_over_sticky(self):
        message = self.build({
            "_followup_listen_triggered": True, "_sticky_triggered": True,
        }, "吗")

        self.assertTrue(message.startswith("你刚才在群里回复："))

    def test_plain_text_is_passed_through(self):
        self.assertEqual(self.build({}, "你好"), "你好")
        self.assertEqual(
            self.build({"quoted_content": "某句"}, "你好"), "你好",
        )

    def test_reply_to_bot_without_quoted_text(self):
        msg = {"is_reply_to_bot": True}

        self.assertEqual(
            self.build(msg, "都可以"), "对方引用了你上一句话，然后说：都可以",
        )
        self.assertEqual(self.build(msg, ""), "对方引用了你上一句话，然后说：")

    def test_reply_to_bot_with_quoted_text(self):
        msg = {"is_reply_to_bot": True, "quoted_content": "你喜欢哪种"}

        self.assertEqual(
            self.build(msg, "都可以"),
            "对方引用了你上一句话「你喜欢哪种」，然后说：都可以",
        )
        self.assertEqual(self.build(msg, ""), "对方引用了你上一句话「你喜欢哪种」")

    def test_long_quote_is_cut_at_120_characters(self):
        exact = self.build(
            {"is_reply_to_bot": True, "quoted_content": "长" * 120}, "好",
        )
        longer = self.build(
            {"is_reply_to_bot": True, "quoted_content": "长" * 130}, "好",
        )

        self.assertEqual(exact, f"对方引用了你上一句话「{'长' * 120}」，然后说：好")
        self.assertEqual(
            longer, f"对方引用了你上一句话「{'长' * 120}...」，然后说：好",
        )

    def test_prior_direct_request_is_woven_into_the_prompt(self):
        context = [
            {"message_id": "a", "sender_id": "u1", "timestamp": 10,
             "content": "wxid_x:\n@林小满 推荐几首英文歌"},
            {"message_id": "b", "sender_id": "u2", "timestamp": 11,
             "content": "林小满 别人说"},
            {"message_id": "c", "sender_id": "u1", "timestamp": 12,
             "content": "闲聊"},
            {"message_id": "cur", "sender_id": "u1", "timestamp": 13,
             "content": "都可以"},
        ]
        msg = {
            "is_reply_to_bot": True, "quoted_content": "你喜欢哪种",
            "sender_id": "u1", "message_id": "cur", "timestamp": 13,
            "content": "都可以",
        }

        with_quote = self.build(msg, "都可以", context, BOT)
        without_quote = self.build(
            dict(msg, quoted_content=""), "都可以", context, BOT,
        )

        self.assertEqual(
            with_quote,
            "对方前面直接对你说：「推荐几首英文歌」。"
            "你随后回复/追问：「你喜欢哪种」。"
            "现在对方引用这句话回答：「都可以」。"
            "回复必须延续前面请求里的硬约束，比如语言、数量、主题和格式；"
            "如果前面是推荐几首/几个，就直接给2到3个结果，少解释，尽量用短名字；"
            "不要被中间别人的聊天带偏。",
        )
        self.assertEqual(
            without_quote,
            "对方前面直接对你说：「推荐几首英文歌」。"
            "现在对方引用了你上一句话，然后说：都可以。"
            "回复必须延续前面请求里的硬约束，不要被中间别人的聊天带偏。",
        )

    def test_prior_request_search_rules(self):
        find = MessageRouter._find_prior_direct_request
        context = [
            {"message_id": "a", "sender_id": "u1", "timestamp": 10,
             "content": "@林小满 推荐几首英文歌"},
            {"message_id": "cur", "sender_id": "u1", "timestamp": 13,
             "content": "都可以"},
        ]
        msg = {"sender_id": "u1", "message_id": "cur", "timestamp": 13,
               "content": "都可以"}

        self.assertEqual(find(msg, context, BOT), "推荐几首英文歌")
        self.assertEqual(find(msg, context, ""), "")
        self.assertEqual(find(msg, None, BOT), "")
        self.assertEqual(find(dict(msg, sender_id="u9"), context, BOT), "")
        # A message that is newer than the current one is skipped.
        self.assertEqual(find(dict(msg, timestamp=5), context, BOT), "")

    def test_prior_request_search_skips_empty_and_repeated_text(self):
        find = MessageRouter._find_prior_direct_request
        context = [
            {"message_id": "a", "sender_id": "u1", "timestamp": 10,
             "content": "@林小满 推荐几首英文歌"},
            {"message_id": "b", "sender_id": "u1", "timestamp": 11,
             "content": "@林小满\u2005"},                       # cleans to ""
            {"message_id": "c", "sender_id": "u1", "timestamp": 12,
             "content": "@林小满 再推荐一遍"},                  # same as current
        ]
        msg = {"sender_id": "u1", "message_id": "cur", "timestamp": 13,
               "content": "@林小满 再推荐一遍"}

        self.assertEqual(find(msg, context, BOT), "推荐几首英文歌")

    def test_prior_request_search_accepts_any_sender_when_the_id_is_missing(self):
        context = [
            {"message_id": "a", "sender_id": "u2", "timestamp": 10,
             "content": "林小满 别人说的"},
        ]

        # PINNED (known issue, expected to change in Phase N): with no
        # sender_id on the current message the sender filter is skipped, so
        # another member's request to the bot can be attributed to this one.
        self.assertEqual(
            MessageRouter._find_prior_direct_request(
                {"message_id": "cur", "timestamp": 13, "content": "好"},
                context, BOT,
            ),
            "林小满 别人说的",
        )

    def test_clean_context_text(self):
        clean = MessageRouter._clean_context_text

        self.assertEqual(clean("wxid_abc:\n@林小满，你好", BOT), "你好")
        self.assertEqual(clean(None, BOT), "")
        self.assertEqual(clean("x" * 160, ""), "x" * 160)
        self.assertEqual(clean("x" * 200, ""), "x" * 160 + "...")


# ── chat call construction (real store) ────────────────────────────────

class ChatCallTests(RouterTestCase):
    def test_summarizer_chat_receives_exactly_these_keywords(self):
        router, summarizer = self.make_router()

        router.handle(self.group_msg("m1", "@林小满 你好"))

        call = summarizer.chat_calls[0]
        self.assertEqual(sorted(call), CHAT_CALL_KEYS)
        self.assertEqual(call["message"], "你好")
        self.assertEqual(call["requester_name"], "小明")
        self.assertEqual(call["bot_name"], BOT)
        self.assertEqual(call["group_name"], "测试群")
        self.assertTrue(call["is_group"])
        self.assertEqual(call["group_memory"], "")
        self.assertFalse(call["ambient"])
        self.assertFalse(call["force_reply"])

    def test_group_name_falls_back_to_the_chat_id(self):
        router, summarizer = self.make_router()
        message = self.group_msg("m1", "@林小满 你好")
        del message["group_name"]

        router.handle(message)

        self.assertEqual(summarizer.chat_calls[0]["group_name"], CHAT)

    def test_context_is_the_newest_rows_of_the_window(self):
        router, summarizer = self.make_router(
            chat_context_count=3, chat_context_window_sec=600,
        )
        store = router._store
        store.insert_message(self.other_msg("old", "窗口外", timestamp=NOW - 700))
        for index in range(6):
            store.insert_message(self.other_msg(
                f"c{index}", f"旧消息{index}", timestamp=NOW - 100 + index,
            ))

        router.handle(self.group_msg("t1", "@林小满 你好"))

        context = summarizer.chat_calls[0]["context_messages"]
        # Fixed in Phase 1: the limit keeps the NEWEST rows of the window, so
        # the latest chatter and the triggering message itself are in context.
        self.assertEqual(
            [m["content"] for m in context],
            ["旧消息4", "旧消息5", "@林小满 你好"],
        )

    def test_bot_replies_are_never_stored(self):
        router, summarizer = self.make_router()

        first = router.handle(self.group_msg("m1", "@林小满 你好"))
        self.clock.advance(1)
        router.handle(self.group_msg("m2", "第二句", at=False))
        self.clock.advance(1)
        # The backend also re-delivers the bot's own message; it is dropped by
        # the own-message guard before it could be stored.
        router.handle(self.group_msg(
            "m3", first, at=False, sender_id="wxid_bot", sender_name=BOT,
        ))

        # PINNED (known issue, expected to change in Phase N): the bot's turns
        # never enter the `messages` table, so the model's context contains
        # only what the humans said, never what the bot itself answered.
        self.assertEqual(self.stored_ids(router), ["m1", "m2"])
        context = summarizer.chat_calls[-1]["context_messages"]
        self.assertEqual(
            [m["content"] for m in context], ["@林小满 你好", "第二句"],
        )

    def test_messages_from_the_same_second_reach_the_model_in_order(self):
        router, summarizer = self.make_router()
        router.handle(self.group_msg("m1", "第一条", at=False))

        router.handle(self.group_msg("m2", "@林小满 第二条"))

        # Fixed in Phase 1: same-second rows keep their insertion order (see
        # tests/test_message_store.py).
        context = summarizer.chat_calls[0]["context_messages"]
        self.assertEqual(
            [m["content"] for m in context], ["第一条", "@林小满 第二条"],
        )

    def test_context_includes_the_trigger_when_the_window_is_not_full(self):
        router, summarizer = self.make_router()
        router._store.insert_message(
            self.other_msg("c0", "之前的话", timestamp=NOW - 5),
        )

        router.handle(self.group_msg("t1", "@林小满 你好"))

        context = summarizer.chat_calls[0]["context_messages"]
        self.assertEqual(
            [m["content"] for m in context], ["之前的话", "@林小满 你好"],
        )

    def test_context_window_starts_at_now_minus_the_configured_seconds(self):
        router, summarizer = self.make_router(chat_context_window_sec=600)
        store = router._store
        store.insert_message(self.other_msg("a", "刚过期", timestamp=NOW - 601))
        store.insert_message(self.other_msg("b", "刚好在内", timestamp=NOW - 600))

        router.handle(self.group_msg("t1", "@林小满 你好"))

        contents = [
            m["content"] for m in summarizer.chat_calls[0]["context_messages"]
        ]
        self.assertEqual(contents, ["刚好在内", "@林小满 你好"])

    def test_follow_up_turns_use_the_smaller_context_limit(self):
        router, summarizer = self.make_router(
            chat_context_count=40, followup_listen_context_count=2,
        )
        summarizer.reply = "《Counting Stars》《Love Story》还有《Shape of You》"
        router.handle(self.group_msg("m1", "@林小满 推荐几首英文歌"))
        for index in range(5):
            router._store.insert_message(self.other_msg(
                f"c{index}", f"聊天{index}", timestamp=NOW - 50 + index,
            ))

        router.handle(self.other_msg("m2", "shapeofyou"))

        self.assertEqual(len(summarizer.chat_calls[1]["context_messages"]), 2)

    def test_nicknames_are_applied_to_the_copy_not_to_the_database(self):
        nicks = StubNicks({"wxid_user1": "明明", "wxid_user2": "土豆哥"})
        router, summarizer = self.make_router(nicks=nicks)
        router._store.insert_message(
            self.other_msg("c0", "我是土豆", timestamp=NOW - 5),
        )

        router.handle(self.group_msg("m1", "@林小满 你好"))

        call = summarizer.chat_calls[0]
        self.assertEqual(call["requester_name"], "明明")
        self.assertEqual(
            [(m["sender_name"], m["content"]) for m in call["context_messages"]],
            [("土豆哥", "我是土豆"), ("明明", "@林小满 你好")],
        )
        stored = [
            row["sender_name"] for row in router._store.conn.execute(
                "SELECT sender_name FROM messages ORDER BY id"
            )
        ]
        self.assertEqual(stored, ["土豆", "小明"])

    def test_reply_has_known_wxids_replaced_by_nicknames(self):
        router, summarizer = self.make_router(
            nicks=StubNicks({"wxid_user2": "土豆哥"}),
        )
        summarizer.reply = "wxid_user2 说得对"

        reply = router.handle(self.group_msg("m1", "@林小满 你觉得"))

        self.assertEqual(reply, "土豆哥 说得对")

    def test_group_memory_is_diary_plus_facts(self):
        router, summarizer = self.make_router()
        store = router._store
        store.upsert_group_memory(CHAT, "我是群日记", 3, "x")
        store.upsert_group_fact(CHAT, "宇肆", "alias", "次元哥哥")
        store.upsert_group_fact(CHAT, "宇肆", "nickname", "不输出")

        router.handle(self.group_msg("m1", "@林小满 在不在呀"))

        self.assertEqual(
            summarizer.chat_calls[0]["group_memory"],
            "我是群日记\n\n## 固定事实/称呼记忆（优先级高于普通聊天日记）\n"
            "- 宇肆 在这个群里的固定称呼/别名是「次元哥哥」。",
        )

    def test_default_web_search_client_is_built_from_the_config(self):
        router, summarizer = self.make_router(
            web=None, web_search_enabled=False, web_search_reply_max_chars=77,
        )

        reply = router.handle(
            self.group_msg("m1", "@林小满 帮我查一下鸣潮最新活动"),
        )

        # A disabled real client never extracts a query, so no network use.
        self.assertIsInstance(router._web_search, WebSearchClient)
        self.assertFalse(router._web_search.enabled)
        self.assertEqual(router._web_search.reply_max_chars, 77)
        self.assertEqual(reply, "嗯，在。")
        self.assertNotIn("max_chars_override", summarizer.chat_calls[0])

    def test_failed_web_search_is_reported_to_the_model(self):
        web = StubWebSearch(fail=True)
        router, summarizer = self.make_router(web=web)

        reply = router.handle(self.group_msg("m1", "@林小满 帮我查一下"))

        self.assertEqual(reply, "嗯，在。")
        call = summarizer.chat_calls[0]
        self.assertEqual(
            call["message"],
            "帮我查一下\n\n联网搜索请求：查询词\n"
            "联网搜索失败。请不要编造实时信息，可以告诉对方暂时没查到。",
        )
        self.assertEqual(call["max_chars_override"], 99)
        self.assertEqual(web.extract_calls, [("帮我查一下", BOT)])

    def test_successful_web_search_appends_formatted_results(self):
        web = StubWebSearch()
        router, summarizer = self.make_router(web=web)

        router.handle(self.group_msg("m1", "@林小满 帮我查一下"))

        self.assertEqual(web.queries, ["查询词"])
        self.assertEqual(
            summarizer.chat_calls[0]["message"],
            "帮我查一下\n\nRESULTS[查询词]\n"
            "这次用户需要联网信息；如果搜索结果不足，请明确说没查到，不要编。",
        )

    def test_web_search_is_skipped_for_ambient_turns(self):
        web = StubWebSearch()
        router, summarizer = self.make_router(
            web=web, bot_display_name=YANG_BOT,
            chat_persona_profile=PERSONA_PROFILE_YANG_YUJING,
            yang_yujing_group_name=YANG_GROUP,
        )

        router.handle(self.group_msg(
            "m1", "帮我查一下", at=False, group_name=YANG_GROUP,
        ))

        self.assertEqual(web.queries, [])
        self.assertEqual(web.extract_calls, [])
        self.assertNotIn("max_chars_override", summarizer.chat_calls[0])

    def test_web_search_is_available_in_private_chat(self):
        web = StubWebSearch()
        router, _ = self.make_router(web=web)

        router.handle(self.private_msg("p1", "帮我查一下"))

        self.assertEqual(web.queries, ["查询词"])

    def test_alias_facts_are_persisted_with_resolved_names(self):
        router, _ = self.make_router(nicks=StubNicks({"wxid_abc": "老王"}))

        router.handle(self.group_msg("m1", "叫wxid_abc次元哥哥", at=False))

        facts = router._store.get_group_facts(CHAT)
        self.assertEqual(
            sorted((f["subject"], f["object"], f["confidence"]) for f in facts),
            [("老王", "cy", 0.78), ("老王", "次元哥哥", 0.86)],
        )
        self.assertEqual({f["source_message_id"] for f in facts}, {"m1"})

    def test_fact_persistence_failures_do_not_stop_the_reply(self):
        router, summarizer = self.make_router()

        with patch.object(
            router._store, "upsert_group_fact", side_effect=RuntimeError("db"),
        ), patch.object(
            router._store, "get_messages_since", side_effect=[
                RuntimeError("db"), [],
            ],
        ):
            reply = router.handle(self.group_msg("m1", "@林小满 叫宇肆次元哥哥"))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(len(summarizer.chat_calls), 1)

    def test_store_without_optional_methods_still_routes(self):
        router, summarizer = self.make_router(store=MinimalStore())

        reply = router.handle(self.group_msg("m1", "@林小满 叫宇肆次元哥哥"))

        self.assertEqual(reply, "嗯，在。")
        self.assertEqual(summarizer.chat_calls[0]["context_messages"], [])
if __name__ == "__main__":
    unittest.main()
