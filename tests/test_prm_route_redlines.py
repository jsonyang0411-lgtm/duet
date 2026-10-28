"""Route red lines for a registered creator, end to end (二轮审查 win C4: the behaviour was right, nothing
guarded it).

A creator's private chat is for drafts only.  Whatever she writes there - 「cc …」 (a relay task, for the
allowed people), 「同意」 / 「同意好友」 (the admin's friend commands), a picture, a voice message, even one
that says 「cc …」 - never gets a reply and never becomes a relay (cc) task: with PRM_COPILOT it is a silent
draft job for the Mac (src/prm), with PRM_EDGE an event the Mac pulls (src/edge); CLAUDE_RELAY_ONLY on or
off alike.  Her @ of the bot in a group gets nothing either.  PRM_COPILOT and PRM_EDGE are never on
together: the bot does not start.

The real WCDB backend over a fake WeChat database, the router, the relay and PRM (or the edge), wired as
Bot.run wires them (tests/test_prm_catchup.py ``World``; the edge as tests/test_same_name_chats.py wires it).
"""

import os
from unittest.mock import patch

import pytest

from router_support import ADMIN, BOT, TraceSpy
from test_prm import CREATOR
from test_prm_catchup import GROUP, FakeWeChat, World, text_lines

from src.admin import RELAY_HELP, AdminCommandHandler
from src.config import BotConfig, _validate_config, load_config
from src.db import MessageStore, initialize_db
from src.edge import protocol as P
from src.edge.service import EdgeService
from src.edge.store import EdgeStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter
from src.wechat.wcdb_backend import WcdbBackend

VOICE_SAID = "cc 帮我把订单导出来"


def her_lines(wechat, at):
    """What she sends in her private chat, ten seconds apart: a cc task, the admin's friend commands, a
    picture, a voice message that was not heard and one that says 「cc …」 (``heard``)."""
    lines = [("cc 帮我查一下订单", 1), ("同意", 1), ("同意好友", 1), ("", 3), ("", 34), (VOICE_SAID, 34)]
    for n, (text, local_type) in enumerate(lines):
        yield (lambda text=text, local_type=local_type, n=n:
               wechat.say(CREATOR, CREATOR, text, at + n * 10, local_type=local_type)), at + n * 10


def heard(backend):
    """The backend's voice recognition: a voice row of the fake WeChat says what its content says (a
    real one has none); and there is no WeChat window to take a picture's screenshot from."""
    backend._try_voice = lambda msg, talker="": str(msg.get("message_content") or "") or None
    backend._capture_media_screenshot = lambda name: ""


def group_at(wechat, clock, poll):
    """She @-mentions the bot in a group: a relay task, then a friend command (each read on its own)."""
    for text in (f"@{BOT} cc 帮我查一下", f"@{BOT} 同意好友"):
        clock.now += 10
        wechat.say(GROUP, CREATOR, text, clock.now)
        poll()


# ── PRM_COPILOT (Windows drafts through the Mac) ───────────────────────────────

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = World(tmp_path)
    yield w
    w.stop()


@pytest.mark.parametrize("relay_only", ["off", "on"])
def test_whatever_a_registered_creator_writes_only_becomes_a_draft_job(world, relay_only):
    world.register()
    run = world.run(claude_relay_only=relay_only)
    heard(run.backend)
    t = world.clock.now
    for say, at in her_lines(world.wechat, t):
        say()
        world.clock.now = at
        run.poll()
    group_at(world.wechat, world.clock, run.poll)

    jobs = world.jobs()
    assert jobs and {job["kind"] for job in jobs} == {"prm"}           # drafts only, no cc (admin) task
    lines = [line for job in jobs for line in text_lines(job)]
    assert lines[:3] == ["cc 帮我查一下订单", "同意", "同意好友"]
    assert lines[3].startswith("[图片]") and lines[4].startswith("[语音]（没转出文字）")
    assert lines[5] == f"[语音] {VOICE_SAID}" and len(lines) == 6
    assert world.sent == []                                           # no reply, to her or anyone
    assert all(job["chat_id"] == CREATOR for job in jobs)
    assert world.stored(GROUP) == 2                                   # the group's @ was read: no answer, no job
    assert world.events("operator_message") == [] and world.prm_store.deal(1)["mode"] == "copilot"

    # the control: the admin's own cc in the admin's chat is a relay task with its receipt; with
    # CLAUDE_RELAY_ONLY the admin may leave 「cc」 out
    world.wechat.say(ADMIN, ADMIN, "cc 查一下今天的订单", world.clock.now)
    run.poll()
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, "导出今天的订单", world.clock.now)
    run.poll()
    admin_jobs = ["admin"] * (2 if relay_only == "on" else 1)
    assert [job["kind"] for job in world.jobs()] == ["prm"] * len(jobs) + admin_jobs
    assert {chat for chat, _ in world.sent} == {ADMIN}


# ── PRM_EDGE (Windows is only the WeChat edge; the Mac drafts) ─────────────────

class Edge:
    """One run of the Windows edge, as Bot._start_edge wires it, with the relay on (the admin's cc)."""

    def __init__(self, tmp_path, clock, wechat, relay_only):
        config = BotConfig(
            private_chat_enabled=True, private_chats=ADMIN, admin_wxid=ADMIN, bot_display_name=BOT,
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_db=":memory:", claude_relay_only=relay_only, prm_edge="on", prm_edge_token="e" * 40)
        self.sent = []
        self.messages = MessageStore(initialize_db(":memory:"))
        self.router = MessageRouter(store=self.messages, admin_handler=AdminCommandHandler(), config=config,
                                    tracer=TraceSpy())
        self.backend = WcdbBackend(bot_display_name=BOT, groups=["*"], store=self.messages, config=config)
        send = lambda chat, text: self.sent.append((chat, text)) or True        # noqa: E731
        self.relay = ClaudeRelay(config, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay",
                                 send=send, reserved=self.router._admin.is_command, extra_help=RELAY_HELP)
        self.router._claude_relay = self.relay
        self.edge = EdgeService(config, store=EdgeStore(":memory:"), clock=clock, files_dir=tmp_path / "edge",
                                send_text=send, send_file=lambda chat, path: True, own_wxid=self.backend.own_wxid,
                                db_readable=self.backend.db_readable,
                                on_parties_changed=self.backend.refresh_private_chats_soon)
        self.backend.set_private_filter(self.edge.is_party_chat)
        self.backend.set_own_message_hook(self.edge.on_own_message)
        self.router._edge = self.edge
        self.edge.start(run_thread=False)
        self.edge.put_parties(1, [CREATOR])                        # the Mac's list
        self.backend._client = wechat
        self.backend._send_and_confirm = lambda name, chat, text: send(chat, text)
        heard(self.backend)
        self.backend._resolve_groups()
        self.backend._prime_known_ids()
        self.backend._running = True

    def poll(self):
        for chat, name in list(self.backend._chat_names.items()):
            self.backend._poll_group(name, chat, self.router.handle)
        for msg in self.backend._burst.flush_all():
            self.backend._dispatch_message(msg, self.router.handle)

    def events(self):
        return [e for e in self.edge.store.events_after(0, 100) if e["type"] != P.EDGE_STATUS]

    def stop(self):
        self.backend._running = False
        self.edge.stop()
        self.relay.stop()


@pytest.mark.parametrize("relay_only", ["off", "on"])
def test_on_the_edge_whatever_a_listed_creator_writes_only_goes_to_the_mac(tmp_path, monkeypatch, relay_only):
    monkeypatch.chdir(tmp_path)
    world = World(tmp_path)                                         # (its clock and fake WeChat)
    edge = Edge(tmp_path, world.clock, world.wechat, relay_only)
    try:
        t = world.clock.now
        for say, at in her_lines(world.wechat, t):
            say()
            world.clock.now = at
            edge.poll()
        group_at(world.wechat, world.clock, edge.poll)

        events = edge.events()
        assert [e["type"] for e in events] == [P.CREATOR_MESSAGE] * 6 and {e["chat_id"] for e in events} == {CREATOR}
        texts = [e["text"] for e in events]
        assert texts[:3] == ["cc 帮我查一下订单", "同意", "同意好友"]
        assert texts[3] == "[图片]" and texts[4] == "[语音]（没转出文字）" and texts[5] == f"[语音] {VOICE_SAID}"
        assert edge.relay.store.list_jobs() == [] and edge.sent == []       # no cc task, no reply
        stored = edge.messages.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (GROUP,))
        assert stored.fetchone()[0] == 2                                  # the group's @ was read: no answer

        world.wechat.say(ADMIN, ADMIN, "cc 查一下今天的订单", world.clock.now)   # the control
        edge.poll()
        assert [job["kind"] for job in edge.relay.store.list_jobs()] == ["admin"]
        assert {chat for chat, _ in edge.sent} == {ADMIN}
    finally:
        edge.stop()


# ── never both ──────────────────────────────────────────────────────────────

def test_prm_copilot_and_prm_edge_are_never_on_together():
    """The config check (also in tests/test_edge_service.py) and the start itself: load_config refuses."""
    both = dict(prm_copilot="on", prm_edge="on", claude_relay="on", private_chat_enabled=True,
                prm_edge_token="e" * 40, claude_relay_token="r" * 40, claude_relay_prefix="cc", admin_wxid=ADMIN)
    with pytest.raises(RuntimeError, match="PRM_EDGE and PRM_COPILOT cannot both be on"):
        _validate_config(both)
    _validate_config({**both, "prm_edge": "off"})
    _validate_config({**both, "prm_copilot": "off"})
    env = {"PRM_COPILOT": "on", "PRM_EDGE": "on", "CLAUDE_RELAY": "on", "CLAUDE_RELAY_TOKEN": "r" * 40,
           "PRM_EDGE_TOKEN": "e" * 40, "PRIVATE_CHAT_ENABLED": "true", "ADMIN_WXID": ADMIN}
    with patch.dict(os.environ, env, clear=True), pytest.raises(RuntimeError, match="cannot both be on"):
        load_config()
    with patch.dict(os.environ, {**env, "PRM_EDGE": "off"}, clear=True):
        assert (load_config().prm_copilot, load_config().prm_edge) == ("on", "off")
