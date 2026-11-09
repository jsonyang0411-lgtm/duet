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

import json
import os
from unittest.mock import MagicMock, patch
from xml.sax.saxutils import escape

import pytest

from router_support import ADMIN, BOT, TraceSpy
from test_prm import CREATOR, draft
from test_prm_catchup import GROUP, ME, FakeWeChat, World, text_lines

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


def test_the_friend_add_commands_in_a_creators_chat_are_her_words_and_add_nobody(world):
    """加百应达人微信 (功能 R): 「待加达人」「加达人 1」「不加达人 1」 in a registered creator's chat only become her draft
    job: no answer, no add, the record as it was; the admin's own 加达人 1 in the admin's chat adds once."""
    world.register()
    run = world.run(prm_friend_add="on")
    manager = MagicMock()
    manager.add_friend.return_value = {"ok": True, "target": "abcdefyz", "message": "已成功向「abcdefyz」发送好友申请"}
    admin, adds = run.router._admin, run.prm.friend_adds
    admin.set_friend_manager(manager, None)
    adds.attach(admin, None, None)
    admin.set_friend_adds(adds)
    adds.spawn, adds.clear_clipboard = (lambda fn: fn()), (lambda text: False)
    record = adds.paste("dy_demo01", "小鹿", {"wechat": "abcdefyz"})
    t = world.clock.now
    for n, text in enumerate(("待加达人", f"加达人 {record['id']}", f"不加达人 {record['id']}")):
        world.wechat.say(CREATOR, CREATOR, text, t + n * 10)
        world.clock.now = t + n * 10
        run.poll()
    jobs = world.jobs()
    assert jobs and {job["kind"] for job in jobs} == {"prm"}
    assert [line for job in jobs for line in text_lines(job)] == ["待加达人", "加达人 1", "不加达人 1"]
    assert world.sent == [] and manager.add_friend.call_count == 0
    assert run.prm.store.friend_add(record["id"])["state"] == "offered"
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, f"加达人 {record['id']}", world.clock.now)      # the control
    run.poll()
    assert manager.add_friend.call_count == 1 and run.prm.store.friend_add(record["id"])["state"] == "requested"
    [(chat, said)] = world.sent
    assert chat == ADMIN and said.startswith("已向 ab****yz 发好友申请") and "abcdefyz" not in said


@pytest.mark.parametrize("on", [True, False])
def test_in_a_boss_group_taken_over_only_her_lines_become_draft_jobs(world, on):
    """老板拉的群 (PRM_BOSS_GROUPS, src/prm/boss_groups.py): the real WCDB backend reads every group (WECHAT_GROUPS *).  Once
    PRM took the boss's group over (its scan's backend calls are the fake of tests/test_prm_boss_groups.py: the backend
    part builds them) her line there is a draft job of the group and nothing more; the boss's and another member's
    lines there, and her lines in another group, get no answer and no job.  With the switch off: no job at all."""
    from test_prm_boss_groups import BOSS, BOSS_ID, Backend

    other = "room2@chatroom"
    world.register()
    world.wechat.sessions[other] = "别的群"
    run = world.run(prm_boss_groups="on" if on else "off", prm_group_with=BOSS)
    fake = Backend(own=ME)
    run.prm.boss_groups.attach(fake)
    run.prm.boss_groups.scan()                                      # the baseline
    fake.groups[GROUP] = {"name": "测试群", "members": [ME, BOSS_ID, CREATOR],
                          "joined_at": world.clock.now}           # (WeChat's line that we were added)
    run.prm.boss_groups._scanned = 0
    assert run.prm.boss_groups.scan() == (1 if on else None)
    world.clock.now += 10
    world.wechat.say(GROUP, BOSS_ID, "大家好，以后由运营跟你对接", world.clock.now)
    world.wechat.say(GROUP, "wxid_guest07", f"@{BOT} 你好", world.clock.now + 1)     # (not "wxid_me…": that is ours)
    world.wechat.say(other, CREATOR, "cc 帮我查一下订单", world.clock.now + 2)
    world.clock.now += 5
    run.poll()
    assert [job for job in world.jobs() if job["text"]] == []
    world.clock.now += 10
    world.wechat.say(GROUP, CREATOR, "好的老板", world.clock.now)
    run.poll()
    drafts = [job for job in world.jobs() if job["text"]]
    assert world.sent == [] and world.stored(GROUP) == 3 and world.stored(other) == 1
    if not on:
        assert drafts == [] and world.jobs() == []
        return
    [job] = drafts
    assert job["kind"] == "prm" and job["chat_id"] == GROUP and text_lines(job) == ["好的老板"]
    meta = json.loads(job["meta"])
    assert meta["channel"] == "group" and [x["who"] for x in meta["group"]["lines"]] == ["老板", "群成员"]


# ── PRM_WECHAT_REVIEW (微信里审批草稿): the admin answers a draft in the admin's own chat ──────────

QUOTE_TYPE = (57 << 32) | 49


def quote_xml(title, quoted, by=ME):
    """A WeChat 引用 as WeChat 4.1 writes it (tests/test_pat_and_recall.py ``QUOTE_XML``): ``title`` typed, quoting the
    line ``quoted`` of ``by``."""
    return (f'<msg><appmsg appid="" sdkver="0"><title>{escape(title)}</title><type>57</type>'
            f'<refermsg><type>1</type><svrid>777</svrid><chatusr>{by}</chatusr><displayname>我</displayname>'
            f'<content>{escape(quoted)}</content></refermsg></appmsg></msg>')


def told_draft(world, run):
    """She writes, the Mac drafts, the sender thread's round tells the admin: the notification's text."""
    world.wechat.say(CREATOR, CREATOR, "收到6件哈", world.clock.now)
    run.poll()
    world.mac_drafts(run, draft(reply_draft="收到啦～戴着还合适吗？", slots={}, message_kind="greeting"))
    run.prm.wechat_review.sweep()
    [(chat, notification)] = [(c, t) for c, t in world.sent if "草稿 ·" in t]
    assert chat == ADMIN and notification.startswith("#1 草稿 · 小美 · 私聊")
    return notification


@pytest.mark.parametrize("relay_only", ["off", "on"])
def test_the_admins_quote_of_a_draft_notification_decides_it_and_nothing_else_does(world, relay_only):
    """The real WCDB backend reads the admin's 引用 of a notification (its <refermsg>): the route's ``wechat_review``
    step sends the draft and the admin hears so once; neither that nor 「发 1」 / 「不发 1」 / 「执行 1」 / 「改 1 …」 (a draft
    told here) becomes a cc task, with CLAUDE_RELAY_ONLY on or off.  Her own 「1」 and her quote of the same words in her
    chat stay her lines for the next draft (no decision, no reply to her); the admin's quote in a group gets nothing; a
    cc task still is one."""
    world.register()
    run = world.run(prm_wechat_review="on", claude_relay_only=relay_only)
    notification = told_draft(world, run)
    world.clock.now += 10
    world.wechat.say(GROUP, ADMIN, quote_xml("1", notification), world.clock.now, local_type=QUOTE_TYPE)
    world.wechat.say(GROUP, ADMIN, "发 1", world.clock.now + 1)
    run.poll()
    assert world.prm_store.packet(1)["status"] == "pending"                  # a group: nothing
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, quote_xml("1", notification), world.clock.now, local_type=QUOTE_TYPE)
    run.poll()
    assert world.prm_store.packet(1)["status"] == "approved"
    assert world.sent[-1] == (ADMIN, "#1 已批准，正在发给 小美")
    assert run.prm.send_approved() == 1 and world.sent[-1] == (CREATOR, "收到啦～戴着还合适吗？")
    world.clock.now += 10
    for n, text in enumerate(("发 1", "不发 1", "执行 1", "改 1 好的老师")):
        world.wechat.say(ADMIN, ADMIN, text, world.clock.now + n * 5)
        world.clock.now += 5
        run.poll()
        assert world.sent[-1] == (ADMIN, "#1 已经处理过了：你在微信里回过了，已经发出去了")
    assert [job["kind"] for job in world.jobs()] == ["prm"]                  # no cc task of any of it
    assert world.prm_store.packet(1)["final_text"] == "收到啦～戴着还合适吗？"
    # her own 「1」 and her quote of the notification's words, in her chat: her lines, nothing else
    before, tasks = len(world.sent), [job["id"] for job in world.jobs() if job["kind"] == "admin"]
    world.clock.now += 10
    world.wechat.say(CREATOR, CREATOR, "1", world.clock.now)
    world.wechat.say(CREATOR, CREATOR, quote_xml("1", notification, by=CREATOR), world.clock.now + 1,
                     local_type=QUOTE_TYPE)
    world.clock.now += 5
    run.poll()
    assert len(world.sent) == before and world.prm_store.packets(("approved",)) == []
    assert [job["id"] for job in world.jobs() if job["kind"] == "admin"] == tasks
    assert (world.jobs()[-1]["kind"], world.jobs()[-1]["chat_id"]) == ("prm", CREATOR)
    # the control: the admin's cc is a task, with its receipt
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, "cc 查一下今天的订单", world.clock.now)
    run.poll()
    assert [job["kind"] for job in world.jobs()][-1] == "admin" and "已交给 Mac" in world.sent[-1][1]


def asked_in_wechat(world, run) -> str:
    """The admin's words quoting the notification: the route's reply asks about them (the last thing said)."""
    notification = told_draft(world, run)
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, quote_xml("明天给您寄～", notification), world.clock.now, local_type=QUOTE_TYPE)
    run.poll()
    question = "#1 改成：『明天给您寄～』\n回 1 发出／2 不发"
    assert world.sent[-1] == (ADMIN, question) and world.prm_store.packet(1)["status"] == "pending"
    return question


def test_the_backend_tells_the_review_what_it_said_so_a_bare_1_answers_the_question_it_asked(world):
    """The admin's words quoting a notification are only a proposal: the route's reply asks 「#1 改成：『…』」, the real
    backend's sent hook (src/bot.py) makes that question the last thing said in the admin's chat, and its read-back
    (``set_read_back_hook``) gives WeChat's time of it; a bare 1 written after that sends the proposal.  A bare 1 after
    any other line of the bot is nobody's."""
    world.register()
    run = world.run(prm_wechat_review="on")
    question = asked_in_wechat(world, run)
    world.wechat.say(ADMIN, ME, question, world.clock.now)                 # WeChat shows the bot's question (read back)
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, "1", world.clock.now)
    run.poll()
    assert run.prm.wechat_review._last_review.wechat_at == world.clock.now - 10
    assert world.sent[-1] == (ADMIN, "#1 按你改的批准了，正在发给 小美")
    assert world.prm_store.packet(1)["final_text"] == "明天给您寄～"
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, "1", world.clock.now)                   # (after that reply: nobody's answer)
    run.poll()
    assert world.sent[-1] == (ADMIN, "#1 按你改的批准了，正在发给 小美") and [j["kind"] for j in world.jobs()] == ["prm"]


def test_a_bare_1_written_before_wechat_showed_the_question_is_not_its_answer(world):
    """Second review N4: one clock for both: the bot's question shows in WeChat at T+5, the admin's 1 was written at T+4
    (it crossed the question): not an answer, the bot says so."""
    world.register()
    run = world.run(prm_wechat_review="on")
    question = asked_in_wechat(world, run)
    world.wechat.say(ADMIN, ME, question, world.clock.now + 5)
    world.wechat.say(ADMIN, ADMIN, "1", world.clock.now + 4)
    world.clock.now += 6
    run.poll()
    assert world.sent[-1] == (ADMIN, "#1 是刚发来的，你回的时候还没看到它：请引用它回，或者回「发 1」「不发 1」")
    assert world.prm_store.packet(1)["status"] == "pending"


def test_with_the_switch_off_the_admins_quote_of_a_draft_is_nothing(world):
    world.register()
    run = world.run(claude_relay_only="on")
    world.wechat.say(CREATOR, CREATOR, "收到6件哈", world.clock.now)
    run.poll()
    world.mac_drafts(run, draft(reply_draft="收到啦～戴着还合适吗？", slots={}, message_kind="greeting"))
    run.prm.tick()
    assert world.sent == []                                                  # nothing told
    world.clock.now += 10
    world.wechat.say(ADMIN, ADMIN, quote_xml("1", "#1 草稿 · 小美 · 私聊"), world.clock.now, local_type=QUOTE_TYPE)
    run.poll()
    assert world.prm_store.packet(1)["status"] == "pending" and world.sent == []


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
