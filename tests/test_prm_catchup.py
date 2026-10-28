"""Messages that came in while the bot was not reading WeChat (PRM_CATCHUP_HOURS).

Stopping the bot (updating the Windows EXE: stop -> rebuild -> start) or re-opening the
WeChat database after read errors leaves a gap.  A registered creator's messages from it,
and this account's own lines (an operator typing on the phone), must still reach PRM, once:
the WCDB backend (src/wechat/wcdb_backend.py) hands on only what is newer than the last
message stored for the chat and not stored itself; PRM (src/prm/service.py) drafts what is
up to PRM_CATCHUP_HOURS old (always reviewed) and records the rest.  Groups, strangers and
cc tasks from the gap stay history.

The integrated tests run the real backend (a fake WeChat database under it), router, relay
and PRM, wired as Bot.run wires them; what outlives a restart (WeChat's messages, the
message store, the relay and PRM databases) is kept in ``World``.
"""

import io
import json
import threading
import time

import pytest

from router_support import ADMIN, BOT, TraceSpy
from test_prm import CREATOR, Bot, Clock, config as prm_config, draft

from src.admin import RELAY_HELP, AdminCommandHandler
from src.config import BotConfig, _validate_config
from src.db import MessageStore, initialize_db
from src.prm import gates
from src.prm.service import (MISSED_EVENT_NAME, SETTLE_SEC, PrmService, event_out, late_note, missed_note)
from src.prm.store import PrmStore
from src.relay.service import MAX_AGE_SEC, ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter
from src.wechat import wcdb_backend
from src.wechat.wcdb_backend import WcdbBackend

ME = "wxid_me"                     # this WeChat account
STRANGER = "wxid_stranger"
GROUP = "room1@chatroom"
HOUR = 3600
NAMES = {CREATOR: "小美", STRANGER: "路人", ADMIN: "管理员", GROUP: "测试群", ME: BOT, "wxid_member": "群友"}


def clock_text(at):
    return time.strftime("%m-%d %H:%M", time.localtime(at))


class FakeWeChat:
    """WeChat's database as WcdbBackend reads it: the sessions, each chat's messages newest first."""

    is_open = True

    def __init__(self):
        self._config = {"myWxid": ME}
        self.chats: dict[str, list[dict]] = {}
        self.sessions = {chat: NAMES[chat] for chat in (GROUP, CREATOR, STRANGER, ADMIN)}
        self.count = 0

    def say(self, chat, sender, text, at, local_type=1):
        self.count += 1
        self.chats.setdefault(chat, []).insert(0, {
            "sender_username": sender, "message_content": text, "local_type": local_type,
            "create_time": int(at), "local_id": self.count, "server_id": f"svr{self.count}"})

    # what WcdbNativeClient offers
    def init(self):
        pass

    def open(self):
        pass

    def close(self):
        pass

    def get_messages(self, talker, limit=50):
        return self.chats.get(talker, [])[:limit]

    def get_sessions(self):
        return [{"username": chat, "displayName": name} for chat, name in self.sessions.items()]

    def resolve_nickname(self, wxid):
        return NAMES.get(wxid, wxid)

    def get_group_members(self, username):
        return []

    def get_display_names(self, usernames):
        return {u: self.resolve_nickname(u) for u in usernames}


class World:
    """What outlives a restart: WeChat, the bot's message store, the relay and PRM databases."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.clock = Clock(float(int(time.time())))
        self.wechat = FakeWeChat()
        self.messages = MessageStore(initialize_db(":memory:"))
        self.relay_store = RelayStore(":memory:")
        self.prm_store = PrmStore(":memory:")
        self.sent: list = []
        self.runs: list = []

    def run(self, **settings):
        run = Run(self, **settings)
        self.runs.append(run)
        return run

    def stop(self):
        for run in self.runs:
            run.stop()

    def register(self):
        self.prm_store.add_party(CREATOR, "小美", "", now=self.clock.now)

    def jobs(self):
        return list(reversed(self.relay_store.list_jobs(limit=100)))       # oldest first

    def stored(self, chat):
        return self.messages.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat,)).fetchone()[0]

    def events(self, kind=None):
        """The deal's events, oldest first."""
        return [e for e in reversed(self.prm_store.events(1, limit=200)) if kind is None or e["kind"] == kind]

    def mac_drafts(self, run, d=None):
        """The Mac worker takes the next prm job and hands back a draft."""
        self.clock.now += SETTLE_SEC + 1
        job = run.relay.next_job(0, ("prm",))
        assert job is not None
        run.relay.finish(job["id"], {"ok": True, "text": json.dumps(d or draft(), ensure_ascii=False),
                                     "duration_s": 3})
        run.relay.tick()
        return job


class Run:
    """One run of the bot, wired as Bot.run does: the WCDB backend reads WeChat and hands every
    message to the router (self → edge → persist → relay → prm → admin → silence)."""

    def __init__(self, world, catchup_hours=12, private_chats="wxid_admin"):
        self.world = world
        config = BotConfig(
            private_chat_enabled=True, private_chats=private_chats, admin_wxid=ADMIN, bot_display_name=BOT,
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_db=":memory:", prm_copilot="on", prm_db=":memory:", prm_catchup_hours=catchup_hours)
        self.router = MessageRouter(store=world.messages, admin_handler=AdminCommandHandler(), config=config,
                                    tracer=TraceSpy())
        self.relay = ClaudeRelay(config, store=world.relay_store, clock=world.clock, files_dir=world.tmp / "relay",
                                 send=lambda chat, text: world.sent.append((chat, text)) or True,
                                 reserved=self.router._admin.is_command, extra_help=RELAY_HELP)
        self.router._claude_relay = self.relay
        self.backend = WcdbBackend(bot_display_name=BOT, groups=["*"], store=world.messages, config=config)
        self.prm = PrmService(config, self.relay, store=world.prm_store, clock=world.clock,
                              send=lambda chat, text: world.sent.append((chat, text)) or True,
                              on_parties_changed=self.backend.refresh_private_chats_soon)
        self.router._prm = self.prm
        self.backend._client = world.wechat
        self.backend.set_private_filter(self.prm.is_party_chat)
        self.backend.set_own_message_hook(self.prm.on_own_message)
        self.backend._send_and_confirm = lambda name, chat, text: world.sent.append((chat, text)) or True
        self.backend._resolve_groups()
        self.backend._prime_known_ids()
        self.backend._running = True

    def poll(self, flush=True):
        """One poll of every chat; ``flush``: the bursts that wait are handed on too."""
        for chat, name in list(self.backend._chat_names.items()):
            self.backend._poll_group(name, chat, self.router.handle)
        if flush:
            for msg in self.backend._burst.flush_all():
                self.backend._dispatch_message(msg, self.router.handle)

    def stop(self):
        self.backend._running = False
        self.prm.stop()
        self.relay.stop()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # data/group_names.json and friends go here
    w = World(tmp_path)
    yield w
    w.stop()


def text_lines(job):
    """A job's lines without their "[10-27 09:15] " time."""
    return [line.split("] ", 1)[1] for line in job["text"].split("\n")]


# ── a restart: what the creator wrote meanwhile is one reviewed draft ─────────

def test_a_restart_hands_on_what_the_creator_wrote_meanwhile_as_one_reviewed_draft(world):
    world.register()
    t0 = world.clock.now
    first = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好，想合作", t0)
    first.poll()
    world.mac_drafts(first)                                         # read while running: drafted as always
    [waiting] = world.prm_store.packets(("pending",))
    first.stop()
    # stop -> rebuild the EXE -> start: an hour in which the creator writes three times
    for minutes, text in ((10, "在吗"), (20, "地址：上海市浦东新区测试路1号"), (30, "电话 13800001111")):
        world.wechat.say(CREATOR, CREATOR, text, t0 + minutes * 60)
    world.clock.now = t0 + HOUR
    second = world.run()
    second.poll()

    jobs = world.jobs()
    assert len(jobs) == 2                                           # the three missed messages: one job
    assert text_lines(jobs[1]) == ["在吗", "地址：上海市浦东新区测试路1号", "电话 〈手机1〉"]
    assert json.loads(jobs[1]["meta"])["late"] == {"from": t0 + 600}
    assert world.stored(CREATOR) == 4                               # each stored under its own id
    assert world.prm_store.packet(waiting["id"])["status"] == "superseded"   # the creator wrote again
    assert world.sent == []                                         # the creator hears nothing

    # even a kind that has earned "auto" waits for a person
    world.prm_store.set_gate("address_confirm", gates.AUTO, world.clock.now)
    world.mac_drafts(second)
    [packet] = world.prm_store.packets(("pending", "approved"))
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert world.prm_store.deal(1)["attention"].startswith(late_note(t0 + 600))
    assert "机器人没在读微信时收到" in late_note(t0 + 600) and clock_text(t0 + 600) in late_note(t0 + 600)

    # a third start finds nothing new: no second job, nothing stored twice
    second.stop()
    world.clock.now += HOUR
    third = world.run()
    third.poll()
    assert len(world.jobs()) == 2 and world.stored(CREATOR) == 4


def test_the_same_draft_read_on_time_still_goes_out_by_its_gate(world):
    """The control of the test above: only being read late makes the review."""
    world.register()
    run = world.run()
    world.prm_store.set_gate("address_confirm", gates.AUTO, world.clock.now)
    world.wechat.say(CREATOR, CREATOR, "电话 13800001111", world.clock.now)
    run.poll()
    world.mac_drafts(run)
    [packet] = world.prm_store.packets(("pending", "approved"))
    assert (packet["status"], packet["route"]) == ("approved", "auto")
    assert world.prm_store.deal(1)["attention"] == ""


def test_messages_older_than_the_catchup_hours_are_recorded_not_drafted(world):
    world.register()
    t0 = world.clock.now
    first = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好", t0)
    first.poll()
    world.mac_drafts(first)
    first.stop()
    world.wechat.say(CREATOR, CREATOR, "样品寄出了吗", t0 + HOUR)
    world.wechat.say(CREATOR, CREATOR, "收到了吗？", t0 + 2 * HOUR)
    world.clock.now = t0 + 16 * HOUR                                # both more than 12 hours old when read
    second = world.run()
    second.poll()

    assert len(world.jobs()) == 1                                   # nothing drafted from them
    deal = world.prm_store.deal(1)
    assert deal["attention"] == missed_note(2, t0 + HOUR)
    assert deal["attention"] == f"2 条消息在机器人没在读微信时收到（最早 {clock_text(t0 + HOUR)}），没有起草，请看一下"
    recorded = world.events("creator_message")
    assert [e["detail"]["text"].split("] ", 1)[1] for e in recorded] == ["样品寄出了吗", "收到了吗？"]
    assert all(e["detail"]["missed"] for e in recorded) and event_out(recorded[0])["name"] == MISSED_EVENT_NAME
    assert world.stored(CREATOR) == 3                               # stored: never handed on again

    # the next message read on time is drafted, told about them, and reviewed
    world.prm_store.set_gate("address_confirm", gates.AUTO, world.clock.now)
    world.wechat.say(CREATOR, CREATOR, "在吗", world.clock.now)
    second.poll()
    job = world.jobs()[-1]
    meta = json.loads(job["meta"])
    assert [h["text"].split("] ", 1)[1] for h in meta["history"]] == ["样品寄出了吗", "收到了吗？"]
    assert all(h["missed"] for h in meta["history"]) and "late" not in meta
    assert meta["missed"] == {"count": 2, "from": t0 + HOUR}
    world.mac_drafts(second)
    packet = world.prm_store.packet_for_job(job["id"])
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert world.prm_store.deal(1)["attention"].startswith(missed_note(2, t0 + HOUR))


def test_with_catchup_hours_zero_the_old_rule_holds(world):
    world.register()
    t0 = world.clock.now
    first = world.run(catchup_hours=0)
    world.wechat.say(CREATOR, CREATOR, "你好", t0)
    first.poll()
    first.stop()
    world.wechat.say(CREATOR, CREATOR, "在吗", t0 + 600)
    world.wechat.say(CREATOR, ME, "我是运营，在的", t0 + 900)
    world.clock.now = t0 + HOUR
    second = world.run(catchup_hours=0)
    second.poll()
    assert len(world.jobs()) == 1 and world.stored(CREATOR) == 1   # the gap is history, as before
    assert world.events() == [] and world.prm_store.deal(1)["mode"] == "copilot"


def test_with_catchup_hours_zero_prm_drops_what_is_older_than_five_minutes(tmp_path):
    clock = Clock()
    bot = Bot(clock, tmp_path)
    bot.prm = PrmService(prm_config(prm_catchup_hours=0), bot.relay, store=PrmStore(":memory:"), clock=clock,
                         send=bot._send)
    try:
        bot.prm.register(CREATOR, "小美")
        assert bot.prm.handle(bot.msg("十分钟前的", ts=clock.now - MAX_AGE_SEC - 300)) is True
        bot.prm.on_own_message({"chat_id": CREATOR, "content": "我亲手回的", "msg_type": 1, "is_group": False,
                                "timestamp": clock.now - MAX_AGE_SEC - 300})
        assert bot.relay.store.list_jobs() == [] and bot.prm.store.events(1) == []
        # Windows' edge queue never replays history: such a message is drafted as it always was
        bot.prm.handle(bot.msg("排队转来的", ts=clock.now - 3 * HOUR, edge_event_id=7))
        [job] = bot.relay.store.list_jobs()
        assert "排队转来的" in job["text"] and "late" not in json.loads(job["meta"])
    finally:
        bot.prm.stop()
        bot.relay.stop()


# ── what else came in meanwhile ───────────────────────────────────────────

def test_groups_strangers_and_cc_tasks_from_the_gap_stay_history(world):
    world.register()
    t0 = world.clock.now
    first = world.run(private_chats="*")                            # strangers' private chats are read too
    first.poll()
    first.stop()
    world.wechat.say(GROUP, "wxid_member", f"@{BOT} 在吗", t0 + 600)
    world.wechat.say(GROUP, ADMIN, "cc 跑一下测试", t0 + 610)
    world.wechat.say(STRANGER, STRANGER, "你好，在吗", t0 + 620)
    world.wechat.say(ADMIN, ADMIN, "cc 跑一下测试", t0 + 630)
    world.wechat.say(ADMIN, ADMIN, "帮助", t0 + 640)
    world.clock.now = t0 + HOUR
    second = world.run(private_chats="*")
    second.poll()
    assert world.sent == [] and world.jobs() == []                  # no reply, no relay task
    assert [world.stored(chat) for chat in (GROUP, STRANGER, ADMIN)] == [0, 0, 0]
    # an old cc task that reached the relay anyway is still not run (tools/claude_relay/README.md)
    old_cc = {"message_id": "x1", "chat_id": ADMIN, "sender_id": ADMIN, "content": "cc 跑一下测试",
              "msg_type": 1, "is_group": False, "timestamp": t0 + 630}
    assert second.relay.handle(old_cc) == (True, None) and world.jobs() == []
    # while running, the same chats are answered as before (the admin's cc gets its receipt)
    world.wechat.say(ADMIN, ADMIN, "cc 跑一下测试", world.clock.now)
    second.poll()
    assert [job["kind"] for job in world.jobs()] == ["admin"] and world.sent[0][0] == ADMIN


def test_an_operators_line_from_the_gap_is_recorded_and_a_recent_one_takes_the_chat_over(world):
    world.register()
    t0 = world.clock.now
    first = world.run()
    world.wechat.say(CREATOR, CREATOR, "样品什么时候寄", t0)
    first.poll()
    world.mac_drafts(first)
    first.stop()
    world.wechat.say(CREATOR, ME, "我是运营小王，样品明天寄，电话 13900001111", t0 + 20 * 60)   # typed on the phone
    world.wechat.say(CREATOR, CREATOR, "好的谢谢", t0 + 25 * 60)
    world.clock.now = t0 + HOUR
    second = world.run()
    second.poll()

    [line] = world.events("operator_message")
    assert line["detail"]["text"] == f"[{clock_text(t0 + 20 * 60)}] 我是运营小王，样品明天寄，电话 〈手机1〉"
    deal = world.prm_store.deal(1)
    assert deal["mode"] == "human" and deal["mode_until"] == t0 + 20 * 60 + 120 * 60   # from when it was typed
    assert len(world.jobs()) == 1                                   # the creator's reply: recorded, a person has it
    assert [e["detail"]["text"].split("] ", 1)[1] for e in world.events("creator_message")] == ["好的谢谢"]
    assert world.prm_store.packets(("pending",)) == []              # the waiting draft is out of date

    second.stop()                                                   # started again: nothing recorded twice
    third = world.run()
    third.poll()
    assert len(world.events("operator_message")) == 1 and len(world.events("takeover")) == 1


def test_an_operators_line_from_long_ago_is_recorded_without_a_takeover(world):
    world.register()
    t0 = world.clock.now
    first = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好", t0)
    first.poll()
    first.stop()
    world.wechat.say(CREATOR, ME, "你好呀，我是运营", t0 + 10 * 60)
    world.clock.now = t0 + 5 * HOUR                                 # longer ago than PRM_TAKEOVER_MIN
    second = world.run()
    second.poll()
    assert [e["kind"] for e in world.events()] == ["operator_message"]
    assert world.prm_store.deal(1)["mode"] == "copilot"


def test_without_a_read_mark_own_lines_stay_history(world):
    """A chat this version never kept track of (the first start after the update): its own lines
    cannot be told from ones handled already, so they are history; the creator's are handed on."""
    world.register()
    t0 = world.clock.now
    first = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好", t0)
    first.poll()
    first.stop()
    world.messages.conn.execute("DELETE FROM chat_read_marks")      # as an older version left it
    world.messages.conn.commit()
    world.wechat.say(CREATOR, ME, "我亲手回一句", t0 + 600)
    world.wechat.say(CREATOR, CREATOR, "好的", t0 + 900)
    world.clock.now = t0 + HOUR
    second = world.run()
    second.poll()
    assert world.events("operator_message") == [] and world.prm_store.deal(1)["mode"] == "copilot"
    assert text_lines(world.jobs()[-1]) == ["好的"]


def test_a_pc_clock_ten_minutes_ahead_still_drafts(world):
    world.register()
    run = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好", world.clock.now - 600)   # WeChat's time: this PC is 10 min ahead
    run.poll()
    [job] = world.jobs()
    assert text_lines(job) == ["你好"]
    world.wechat.say(CREATOR, ME, "我来回", world.clock.now - 590)
    run.poll()
    deal = world.prm_store.deal(1)
    assert deal["mode"] == "human" and deal["mode_until"] > world.clock.now   # a person's, as when typed


def test_the_first_read_of_a_creator_registered_later_keeps_the_old_rule(world):
    """A chat the bot never read (registered while running, not in PRIVATE_CHATS): its history
    is history, only the last five minutes are handed on, as they always were."""
    now = world.clock.now = float(int(time.time()))                # the backend's own clock decides here
    world.wechat.say(CREATOR, CREATOR, "上个月聊的", now - 30 * 24 * HOUR)
    world.wechat.say(CREATOR, ME, "运营：好的", now - 30 * 24 * HOUR + 60)
    world.wechat.say(CREATOR, CREATOR, "昨天的话", now - 20 * HOUR)
    world.wechat.say(CREATOR, CREATOR, "你好，我想合作", now - 60)
    run = world.run()
    assert CREATOR not in run.backend._chat_names                   # not a creator yet: not read
    run.prm.register(CREATOR, "小美")                               # tells the backend
    run.backend._refresh_private_chats_if_due()
    assert run.backend._chat_names.get(CREATOR) == "小美"
    run.poll()
    [job] = world.jobs()
    assert text_lines(job) == ["你好，我想合作"] and world.events() == []


def test_the_first_read_ignores_what_was_stored_of_the_chat_long_ago(world):
    """Read once (PRIVATE_CHATS listed it), then not for months, then registered: what came in
    meanwhile was never the bot's to handle, so it stays history on the first read."""
    now = world.clock.now = float(int(time.time()))
    world.messages.insert_message({"message_id": "old-1", "chat_id": CREATOR, "sender_id": CREATOR,
                                   "sender_name": "小美", "content": "春天的话", "msg_type": 1,
                                   "timestamp": int(now - 200 * 24 * HOUR)})
    for days in (90, 30, 2):
        world.wechat.say(CREATOR, CREATOR, f"{days} 天前的话", now - days * 24 * HOUR)
    world.wechat.say(CREATOR, CREATOR, "你好，我想合作", now - 60)
    run = world.run()
    run.prm.register(CREATOR, "小美")
    run.backend._refresh_private_chats_if_due()
    run.poll()
    [job] = world.jobs()
    assert text_lines(job) == ["你好，我想合作"] and world.events() == []


def test_a_creators_chat_found_only_after_startup_still_gets_what_came_in_meanwhile(world):
    """Read as a creator's chat before (it has a read mark), missing when the bot started (WeChat
    listed it later): its first read hands on what came in while the bot was not reading."""
    world.register()
    t0 = world.clock.now
    first = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好", t0)
    first.poll()
    first.stop()
    world.wechat.say(CREATOR, CREATOR, "在吗", t0 + 600)
    del world.wechat.sessions[CREATOR]                              # not listed when the bot starts
    world.clock.now = t0 + HOUR
    second = world.run()
    second.poll()
    assert CREATOR not in second.backend._chat_names and len(world.jobs()) == 1
    world.wechat.sessions[CREATOR] = "小美"
    second.backend.refresh_private_chats_soon()
    second.backend._refresh_private_chats_if_due()
    second.poll()
    late = world.jobs()[-1]
    assert text_lines(late) == ["在吗"] and json.loads(late["meta"])["late"] == {"from": t0 + 600}


def test_a_picture_read_late_is_not_taken_from_the_chat_window(world):
    """The window shows the chat as it is now, not the picture sent while the bot was not reading."""
    world.register()
    t0 = world.clock.now
    first = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好", t0)
    first.poll()
    first.stop()
    world.wechat.say(CREATOR, CREATOR, "", t0 + 600, local_type=3)
    world.clock.now = t0 + HOUR
    second = world.run()
    shots = []
    second.backend._capture_media_screenshot = lambda name: shots.append(name) or ""
    second.poll()
    assert shots == [] and text_lines(world.jobs()[-1]) == ["[图片]（一张图片在电脑上没找到（可能还没下载完），没带上）"]
    world.wechat.say(CREATOR, CREATOR, "", world.clock.now, local_type=3)   # read on time: the window may help
    second.poll()
    assert shots == ["小美"]


def test_a_busy_account_never_hands_a_creators_history_on_again(world):
    """More chats than the old shared set of 5000 seen ids could hold: a quiet chat's history
    must not come back as new (with PRM_CATCHUP_HOURS it would be drafted)."""
    world.register()
    t0 = world.clock.now
    for n in range(120):
        chat = f"9{n:03d}@chatroom"
        world.wechat.sessions[chat] = f"群{n}"
        for i in range(50):
            world.wechat.say(chat, "wxid_member", f"群{n} 第{i}句", t0 - HOUR + i)
    for i in range(50):
        world.wechat.say(CREATOR, CREATOR, f"以前的第{i}句", t0 - 2 * HOUR + i)
    run = world.run()
    for _ in range(2):
        run.poll()
    assert world.jobs() == [] and world.messages.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_after_the_database_is_reopened_the_gap_is_handed_on_once(world, monkeypatch):
    """Read errors in a row re-open WCDB (_reinitialize): what came in meanwhile is handed on, a
    burst still held is left to the buffer (no message twice)."""
    monkeypatch.setattr(wcdb_backend, "WcdbNativeClient", lambda: world.wechat)
    world.register()
    t0 = world.clock.now
    run = world.run()
    monkeypatch.setattr(run.backend._window, "find_hwnd", lambda *args, **kwargs: None)
    world.wechat.say(CREATOR, CREATOR, "你好", t0)
    run.poll()                                                      # stored
    world.wechat.say(CREATOR, CREATOR, "第一句", t0 + 10)
    run.poll(flush=False)                                           # read, still held as a burst
    world.wechat.say(CREATOR, CREATOR, "第二句", t0 + 60)          # meanwhile the database cannot be read
    world.clock.now = t0 + 120
    run.backend._reinitialize()
    run.poll()
    [job] = world.jobs()
    assert sorted(text_lines(job)) == sorted(["你好", "第一句", "第二句"])
    assert world.stored(CREATOR) == 3


def test_after_an_ocr_stretch_a_creators_messages_are_handed_on(world):
    """OCR never reads a private chat: the first database read after it marks a group's backlog
    seen (part of it was answered from the screen), a creator's messages are handed on."""
    world.register()
    run = world.run()
    world.wechat.say(GROUP, "wxid_member", "断线期间的话", world.clock.now)
    world.wechat.say(CREATOR, CREATOR, "在吗", world.clock.now)
    run.backend._ocr_priming = True
    run.poll()
    assert world.stored(GROUP) == 0 and [text_lines(j) for j in world.jobs()] == [["在吗"]]


# ── PRM on its own (either WeChat backend: the Windows database or the Mac's edge) ──

@pytest.fixture
def bot(tmp_path):
    b = Bot(Clock(), tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def test_messages_read_late_are_one_job_of_their_own(bot):
    now = bot.clock.now
    bot.prm.handle(bot.msg("刚才的话"))                             # read on time: job 1, waiting
    bot.prm.handle(bot.msg("一小时前的话", ts=now - HOUR))          # read late: a job of its own ...
    bot.prm.handle(bot.msg("五十分钟前的话", ts=now - 50 * 60))     # ... the next ones join it
    first, late = bot.relay.store.list_jobs()[::-1]
    assert "late" not in json.loads(first["meta"]) and json.loads(late["meta"])["late"] == {"from": now - HOUR}
    assert text_lines(late) == ["一小时前的话", "五十分钟前的话"]
    job = bot.worker_takes()                                        # job 1 runs; then job 2 ...
    assert job["id"] == first["id"]
    job = bot.worker_takes()
    bot.prm.handle(bot.msg("四十分钟前的话", ts=bot.clock.now - 40 * 60))
    assert bot.relay.store.followups(job["id"], 0)[0]["text"].endswith("四十分钟前的话")   # ... takes this


def test_messages_handed_on_at_once_from_many_threads_are_one_job(bot):
    now = bot.clock.now
    msgs = [bot.msg(f"第{i}句", ts=now - HOUR + i) for i in range(8)]
    threads = [threading.Thread(target=bot.prm.handle, args=(m,)) for m in msgs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    [job] = bot.relay.store.list_jobs()
    assert sorted(text_lines(job)) == sorted(f"第{i}句" for i in range(8))


def test_a_message_windows_queued_follows_the_catchup_rule_too(bot):
    """The Mac's edge backend: Windows' queue never replays history, but what it kept while the Mac
    was away follows PRM_CATCHUP_HOURS like any other message: never dropped by age."""
    now = bot.clock.now
    bot.prm.handle(bot.msg("排队太久的", ts=now - 20 * HOUR, edge_event_id=5))
    assert bot.relay.store.list_jobs() == []
    assert bot.prm.store.deal(1)["attention"] == missed_note(1, now - 20 * HOUR)
    bot.prm.handle(bot.msg("排队了一小时", ts=now - HOUR, edge_event_id=6))
    [job] = bot.relay.store.list_jobs()
    meta = json.loads(job["meta"])
    assert meta["late"] == {"from": now - HOUR} and meta["missed"] == {"count": 1, "from": now - 20 * HOUR}
    bot.finish(bot.worker_takes()["id"], draft())
    [packet] = bot.prm.store.packets()
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert bot.prm.store.deal(1)["attention"].startswith(f"{late_note(now - HOUR)}；{missed_note(1, now - 20 * HOUR)}")


def test_a_failed_draft_of_late_messages_still_says_they_were_late(bot):
    now = bot.clock.now
    bot.prm.handle(bot.msg("在吗", ts=now - HOUR))
    job = bot.worker_takes()
    bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "claude 没有给出结构化结果"})
    bot.relay.tick()
    attention = bot.prm.store.deal(1)["attention"]
    assert "草稿没写成" in attention and late_note(now - HOUR) in attention


def test_a_takeover_from_a_line_read_late_never_cuts_a_later_one_short(bot):
    now = bot.clock.now
    own = {"chat_id": CREATOR, "msg_type": 1, "is_group": False, "sender_id": ME}
    bot.prm.on_own_message({**own, "content": "刚回的", "timestamp": now})
    bot.prm.on_own_message({**own, "content": "一小时前回的", "timestamp": now - HOUR})
    deal = bot.prm.store.deal(1)
    assert deal["mode"] == "human" and deal["mode_until"] == now + 120 * 60
    assert [e["kind"] for e in bot.prm.store.events(1)].count("operator_message") == 2


def test_a_reply_sent_just_before_a_stop_and_read_back_after_it_is_the_bots(bot):
    """Read back hours later (the bot was stopped right after sending): found by the time the
    line was written, so no takeover."""
    bot.prm.handle(bot.msg("收件人小美 13812345678"))
    job = bot.worker_takes()
    bot.finish(job["id"], draft())
    packet = bot.prm.store.packet_for_job(job["id"])
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    sent_at = bot.clock.now
    bot.clock.now += 2 * HOUR
    restarted = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=bot.clock, send=bot._send)
    restarted.on_own_message({"chat_id": CREATOR, "content": "确认一下：小美，手机13812345678。", "msg_type": 1,
                              "is_group": False, "timestamp": sent_at})
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.deal(1)["mode"] == "copilot" and "operator_message" not in [
        e["kind"] for e in bot.prm.store.events(1)]


def test_a_file_sent_just_before_a_stop_and_read_back_after_it_is_the_bots(bot):
    bot.prm.handle(bot.msg("要素材"))
    job = bot.worker_takes()
    assert bot.relay.outfile(job["id"], "brief.txt", io.BytesIO(b"brief").read, 5)
    bot.finish(job["id"], draft(message_kind="material", reply_draft="", attachments_to_send=["assets/brief.txt"]))
    packet = bot.prm.store.packet_for_job(job["id"])
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    sent_at = bot.clock.now
    bot.clock.now += 2 * HOUR
    restarted = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=bot.clock, send=bot._send)
    restarted.on_own_message({"chat_id": CREATOR, "content": "[文件]", "msg_type": 49, "is_group": False,
                              "timestamp": sent_at - 2})
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    # a file an operator sends later is a person's
    restarted.on_own_message({"chat_id": CREATOR, "content": "[图片]", "msg_type": 3, "is_group": False,
                              "timestamp": bot.clock.now})
    assert bot.prm.store.deal(1)["mode"] == "human"


def test_catchup_hours_are_checked():
    assert BotConfig().prm_catchup_hours == 12
    for good in (0, 12, 72):
        _validate_config({"prm_catchup_hours": good})
    for bad in (-1, 73):
        with pytest.raises(RuntimeError, match="PRM_CATCHUP_HOURS"):
            _validate_config({"prm_catchup_hours": bad})
