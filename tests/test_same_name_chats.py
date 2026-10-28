"""Chats with the same name: a group and a creator both called 「小美」, two creators, a creator and a
friend.  Every chat is read under its wxid, so a creator's words reach the route and PRM whoever else
has her name.  The WeChat window finds a chat only by the name typed into WeChat's search (Enter opens
the first result), so a chat whose name another chat has is never sent to, nor screenshotted: the
send is refused with a reason a person can act on, and PRM marks the draft as not sent.

The real WCDB backend over a fake WeChat 4 database, the router, the relay and PRM, wired as Bot.run
wires them: PRM and the relay send through ``backend.send_text`` / ``send_file``; the window itself
is a fake that writes down what it would have typed into which chat.
"""

import json
import logging
import time

import pytest

from router_support import ADMIN, BOT, TraceSpy
from test_prm import CREATOR, Clock, draft

from src.admin import RELAY_HELP, AdminCommandHandler
from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.edge import protocol as P
from src.edge.service import EdgeService
from src.edge.store import EdgeStore
from src.prm.service import SETTLE_SEC, PrmService
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter
from src.wechat.wcdb_backend import WcdbBackend

ME = "wxid_me"
CREATOR2 = "wxid_creator02"
FRIEND = "wxid_friend01"
GROUP = "room1@chatroom"
NAMESAKE_GROUP = "room9@chatroom"


class WeChat:
    """WeChat 4's database as WcdbBackend reads it (tools/friend_probe.py, 2026-10-27): session rows
    carry no names, the names come from get_display_names; each chat's messages newest first."""

    is_open = True

    def __init__(self, clock):
        self._config = {"myWxid": ME}
        self.clock = clock
        self.names: dict[str, str] = {}
        self.chats: dict[str, list[dict]] = {}
        self.hidden: set[str] = set()            # chats not in the session list (deleted from WeChat's)
        self.count = 0

    def chat(self, wxid, name):
        """A session WeChat lists (the order of the calls is the session list's)."""
        self.names[wxid] = name
        self.chats.setdefault(wxid, [])

    def say(self, chat, sender, text, local_type=1):
        self.count += 1
        self.chats.setdefault(chat, []).insert(0, {
            "sender_username": sender, "message_content": text, "local_type": local_type,
            "create_time": int(self.clock.now), "local_id": self.count, "server_id": f"svr{self.count}"})

    # what WcdbNativeClient offers
    def get_sessions(self, limit=500):
        return [{"username": chat} for chat in self.chats if chat not in self.hidden]

    def get_messages(self, talker, limit=50, offset=0):
        return [dict(m) for m in self.chats.get(talker, [])[:limit]]

    def get_display_names(self, usernames):
        return {u: self.names[u] for u in usernames if u in self.names}

    def resolve_nickname(self, wxid):
        return self.names.get(wxid, wxid)

    def get_group_members(self, chat):
        return []


class Window:
    """The WeChat window: what it would have typed or pasted, into the chat it searched for by name."""

    def __init__(self):
        self.typed: list[tuple[str, str]] = []
        self.screenshots: list[str] = []

    def send_file_to_chat(self, name, path):
        self.typed.append((name, f"<文件 {path}>"))
        return True

    def capture_chat_media(self, name):
        self.screenshots.append(name)
        return "shot.png"


class Gateway:
    """One run of the bot in copilot mode, as Bot.run wires it."""

    def __init__(self, tmp_path, clock, wechat, private_chats="wxid_admin"):
        config = BotConfig(
            private_chat_enabled=True, private_chats=private_chats, admin_wxid=ADMIN, bot_display_name=BOT,
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_db=":memory:", prm_copilot="on", prm_db=":memory:", prm_catchup_hours=12)
        self.clock = clock
        self.messages = MessageStore(initialize_db(":memory:"))
        self.router = MessageRouter(store=self.messages, admin_handler=AdminCommandHandler(), config=config,
                                    tracer=TraceSpy())
        self.backend = WcdbBackend(bot_display_name=BOT, groups=["*"], poll_sec=0, store=self.messages,
                                   config=config)
        self.relay = ClaudeRelay(config, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay",
                                 send=self.backend.send_text, send_file=self.backend.send_file,
                                 reserved=self.router._admin.is_command, extra_help=RELAY_HELP)
        self.router._claude_relay = self.relay
        self.prm = PrmService(config, self.relay, store=PrmStore(":memory:"), clock=clock,
                              send=self.backend.send_text, send_file=self.backend.send_file,
                              on_parties_changed=self.backend.refresh_private_chats_soon)
        self.router._prm = self.prm
        self.window = Window()
        self.backend._client = wechat
        self.backend._window = self.window
        self.backend._send_and_confirm = lambda name, chat, text: self.window.typed.append((name, text)) or True
        self.backend.set_private_filter(self.prm.is_party_chat)
        self.backend.set_own_message_hook(self.prm.on_own_message)

    def start(self):
        self.backend._resolve_groups()
        self.backend._prime_known_ids()
        self.backend._running = True
        return self

    def poll(self):
        """One cycle of the poll loop (as WcdbBackend.start runs it), the waiting bursts handed on too."""
        self.backend._poll_cycle(self.router.handle)
        for msg in self.backend._burst.flush_all():
            self.backend._dispatch_message(msg, self.router.handle)

    def jobs(self):
        return list(reversed(self.relay.store.list_jobs(limit=100)))

    def mac_drafts(self, reply="好的，我们看一下"):
        """The Mac worker takes the next prm job and hands back a draft (review: it waits)."""
        self.clock.now += SETTLE_SEC + 1
        job = self.relay.next_job(0, ("prm",))
        assert job is not None
        d = draft(message_kind="faq", stage_proposal="talk", slots={}, reply_draft=reply)
        self.relay.finish(job["id"], {"ok": True, "text": json.dumps(d, ensure_ascii=False), "duration_s": 3})
        self.relay.tick()
        return job

    def approve_and_send(self):
        [packet] = self.prm.store.packets(("pending",))
        assert self.prm.decide(packet["id"], "send")["ok"]
        self.prm.send_approved()
        return self.prm.store.packet(packet["id"])

    def stop(self):
        self.backend._running = False
        self.prm.stop()
        self.relay.stop()


@pytest.fixture
def clock():
    return Clock(float(int(time.time())))         # the backend's first-read rule uses the real time


@pytest.fixture
def wechat(clock):
    w = WeChat(clock)
    w.chat(GROUP, "测试群")
    w.chat(ADMIN, "管理员")
    return w


@pytest.fixture
def gateway(tmp_path, clock, wechat, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # data/group_names.json, data/private_chat_names.json
    made = []

    def make(**kw):
        g = Gateway(tmp_path, clock, wechat, **kw)
        made.append(g)
        return g

    yield make
    for g in made:
        g.stop()


def lines(job):
    return [line.split("] ", 1)[1] for line in job["text"].split("\n")]


# ── reading: every chat under its wxid ───────────────────────────────────────

def test_a_creator_named_like_a_group_is_read_and_drafted(gateway, wechat):
    wechat.chat(NAMESAKE_GROUP, "小美")                 # WECHAT_GROUPS=* reads every group, this one first
    wechat.chat(CREATOR, "小美")
    g = gateway()
    g.prm.register(CREATOR, "小美")
    g.start()
    wechat.say(CREATOR, CREATOR, "在吗，想合作")
    wechat.say(NAMESAKE_GROUP, "wxid_qunyou01", "wxid_qunyou01:\n大家好")
    g.poll()
    [job] = g.jobs()
    assert job["chat_id"] == CREATOR and lines(job) == ["在吗，想合作"]
    assert g.backend._chat_names[CREATOR] == g.backend._chat_names[NAMESAKE_GROUP] == "小美"
    stored = {row[0] for row in g.messages.conn.execute("SELECT chat_id FROM messages")}
    assert stored == {CREATOR, NAMESAKE_GROUP}           # the group is still read (and never answered)


def test_two_creators_and_a_friend_with_the_same_name_are_each_read(gateway, wechat):
    for chat in (CREATOR, FRIEND, CREATOR2):
        wechat.chat(chat, "小美")
    g = gateway(private_chats="*")                       # the friend is read too
    g.prm.register(CREATOR, "小美")
    g.prm.register(CREATOR2, "小美B")
    g.start()
    wechat.say(CREATOR, CREATOR, "我是第一个小美")
    wechat.say(FRIEND, FRIEND, "周末吃饭吗")
    wechat.say(CREATOR2, CREATOR2, "我是第二个小美")
    g.poll()
    jobs = g.jobs()
    assert sorted((job["chat_id"], lines(job)[0]) for job in jobs) == [
        (CREATOR, "我是第一个小美"), (CREATOR2, "我是第二个小美")]   # the friend's words: stored, not drafted
    assert g.messages.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (FRIEND,)).fetchone()[0] == 1


def test_a_creator_registered_later_is_read_although_a_group_has_her_name(gateway, wechat):
    wechat.chat(NAMESAKE_GROUP, "小美")
    wechat.chat(CREATOR, "小美")
    g = gateway().start()
    assert CREATOR not in g.backend._chat_names           # not a creator yet: not read
    g.prm.register(CREATOR, "小美")                         # the next poll refreshes the private chats
    wechat.say(CREATOR, CREATOR, "你好")
    g.poll()
    g.poll()
    assert [lines(job) for job in g.jobs()] == [["你好"]]


# ── sending: a name two chats have is never typed into WeChat's search ───────

def test_a_draft_for_a_creator_whose_name_a_group_has_is_refused_with_a_reason(gateway, wechat, caplog):
    wechat.chat(NAMESAKE_GROUP, "小美")
    wechat.chat(CREATOR, "小美")
    g = gateway()
    g.prm.register(CREATOR, "小美")
    g.start()
    wechat.say(CREATOR, CREATOR, "样品什么时候寄")
    g.poll()
    g.mac_drafts()
    caplog.set_level(logging.WARNING, logger="src.wechat.wcdb_backend")
    packet = g.approve_and_send()
    assert packet["status"] == "failed"                   # PRM's failure handling: the draft shows red
    assert g.window.typed == []                           # nothing typed into either 小美
    refused = g.backend.send_text(CREATOR, "你好")
    assert not refused
    assert "「小美」" in refused.reason and "没法确定发给谁" in refused.reason and "备注" in refused.reason
    assert refused.reason in caplog.text
    assert not g.backend.send_text(NAMESAKE_GROUP, "大家好") and g.window.typed == []
    assert not g.backend.send_file(CREATOR, "C:/x/样品清单.txt") and g.window.typed == []


def test_two_private_chats_with_one_name_are_not_sent_to_either(gateway, wechat):
    wechat.chat(CREATOR, "小美")
    wechat.chat(FRIEND, "小美")                           # a friend the bot does not read (PRIVATE_CHATS)
    g = gateway()
    g.prm.register(CREATOR, "小美")
    g.start()
    assert FRIEND not in g.backend._chat_names
    refused = g.backend.send_text(CREATOR, "你好")
    assert not refused and "「小美」" in refused.reason and g.window.typed == []


@pytest.mark.parametrize("namesake", [False, True], ids=["own name", "a group has her name"])
def test_a_first_send_to_a_creator_not_read_yet_starts_reading_her(gateway, wechat, tmp_path, namesake):
    """PRM or the WeChat edge sends to a creator whose chat is not read (WeChat does not list it: the
    chat was deleted from the list): the name comes from data/private_chat_names.json and her chat is
    read from the next poll on, so her answer is drafted (fd9419b).  A name a group has too: the send
    is refused, and her chat is read all the same."""
    if namesake:
        wechat.chat(NAMESAKE_GROUP, "小美")
    wechat.chat(CREATOR, "小美")
    wechat.hidden.add(CREATOR)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "private_chat_names.json").write_text(
        json.dumps({CREATOR: "小美"}, ensure_ascii=False), encoding="utf-8")
    g = gateway()
    g.prm.register(CREATOR, "小美")
    g.start()
    g.poll()
    assert CREATOR not in g.backend._chat_names                 # not in the session list: not read
    sent = g.backend.send_text(CREATOR, "你好，我是品牌方小王")
    if namesake:
        assert not sent and "「小美」" in sent.reason and g.window.typed == []
    else:
        assert sent is True and g.window.typed == [("小美", "你好，我是品牌方小王")]
    assert g.backend._chat_names[CREATOR] == "小美"              # read from the next poll on
    wechat.say(CREATOR, CREATOR, "你好呀，在的")
    g.poll()
    assert [lines(job) for job in g.jobs()] == [["你好呀，在的"]]


def test_a_name_only_one_chat_has_is_sent_to_as_before(gateway, wechat):
    wechat.chat(NAMESAKE_GROUP, "小美粉丝群")              # (a longer name; whether WeChat's search can mix
    wechat.chat(CREATOR, "小美")                            #  them up is only seen on a real machine)
    g = gateway()
    g.prm.register(CREATOR, "小美")
    g.start()
    wechat.say(CREATOR, CREATOR, "样品什么时候寄")
    g.poll()
    g.mac_drafts("明天寄出")
    packet = g.approve_and_send()
    assert (packet["status"], packet["outcome"]) == ("sent", "sent_unconfirmed")
    assert g.window.typed == [("小美", "明天寄出")]
    assert g.backend.send_file(CREATOR, "C:/x/清单.txt") is True
    assert g.window.typed[-1] == ("小美", "<文件 C:/x/清单.txt>")


def test_a_unique_remark_and_a_restart_make_her_reachable(gateway, wechat):
    """The way out the refusal names: WeChat gives the remark as her name; the bot asks for the names
    when it starts (the real client keeps them for the run)."""
    wechat.chat(NAMESAKE_GROUP, "小美")
    wechat.chat(CREATOR, "小美")
    first = gateway()
    first.prm.register(CREATOR, "小美")
    first.start()
    assert not first.backend.send_text(CREATOR, "你好")
    first.stop()
    wechat.names[CREATOR] = "小美-寄样达人"                  # the operator gives her a remark
    second = gateway()
    second.prm.register(CREATOR, "小美")
    second.start()
    assert second.backend.send_text(CREATOR, "你好") is True
    assert second.window.typed == [("小美-寄样达人", "你好")]


def test_a_reply_in_a_chat_whose_name_a_group_has_is_not_typed(gateway, wechat, caplog):
    wechat.chat(NAMESAKE_GROUP, "管理员")
    g = gateway().start()
    caplog.set_level(logging.WARNING, logger="src.wechat.wcdb_backend")
    wechat.say(ADMIN, ADMIN, "帮助")
    g.poll()
    assert g.window.typed == [] and "「管理员」" in caplog.text
    del wechat.names[NAMESAKE_GROUP]                      # the control: the same reply without a namesake
    wechat.chats.pop(NAMESAKE_GROUP)
    h = gateway().start()
    wechat.say(ADMIN, ADMIN, "帮助")
    h.poll()
    assert [name for name, _ in h.window.typed] == ["管理员"]


def test_a_picture_in_a_chat_whose_name_another_chat_has_is_not_screenshotted(gateway, wechat):
    wechat.chat(NAMESAKE_GROUP, "小美")
    wechat.chat(CREATOR, "小美")
    wechat.chat(CREATOR2, "小青")
    g = gateway()
    g.prm.register(CREATOR, "小美")
    g.prm.register(CREATOR2, "小青")
    g.start()
    picture = {"msg_type": 3, "content": "[图片]"}
    for chat, name in ((CREATOR, "小美"), (CREATOR2, "小青")):
        msg = dict(picture, chat_id=chat, group_name=name)
        g.backend._handle_message(name, chat, msg, lambda m: m.update(shot=m["_capture_media_screenshot"]()))
        assert msg["shot"] == ("" if chat == CREATOR else "shot.png")
    assert g.window.screenshots == ["小青"]


# ── the WeChat edge (PRM_EDGE): the same backend under the Mac's business service ─────

def test_edge_mode_a_listed_creator_named_like_a_group_is_heard_and_a_send_to_her_refused(
        tmp_path, clock, wechat, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wechat.chat(NAMESAKE_GROUP, "小美")
    wechat.chat(CREATOR, "小美")
    config = BotConfig(
        private_chat_enabled=True, private_chats="wxid_admin", admin_wxid=ADMIN, bot_display_name=BOT,
        claude_relay="on", claude_relay_token="t" * 32, prm_edge="on", prm_edge_token="e" * 40)
    messages = MessageStore(initialize_db(":memory:"))
    router = MessageRouter(store=messages, admin_handler=AdminCommandHandler(), config=config, tracer=TraceSpy())
    backend = WcdbBackend(bot_display_name=BOT, groups=["*"], poll_sec=0, store=messages, config=config)
    edge = EdgeService(config, store=EdgeStore(":memory:"), clock=clock, files_dir=tmp_path / "edge",
                       send_text=backend.send_text, send_file=backend.send_file, own_wxid=backend.own_wxid,
                       db_readable=backend.db_readable, on_parties_changed=backend.refresh_private_chats_soon)
    backend.set_private_filter(edge.is_party_chat)               # as Bot._start_edge wires them
    backend.set_own_message_hook(edge.on_own_message)
    router._edge = edge
    edge.start(run_thread=False)
    edge.put_parties(1, [CREATOR])                                # the Mac's list
    window = Window()
    backend._client, backend._window = wechat, window
    backend._send_and_confirm = lambda name, chat, text: window.typed.append((name, text)) or True
    backend._resolve_groups()
    backend._prime_known_ids()
    backend._running = True
    try:
        wechat.say(CREATOR, CREATOR, "在吗，想合作")
        backend._poll_cycle(router.handle)
        for msg in backend._burst.flush_all():
            backend._dispatch_message(msg, router.handle)
        found = edge.store.events_after(0, 50)
        assert [(e["chat_id"], e["text"]) for e in found if e["type"] == P.CREATOR_MESSAGE] == [
            (CREATOR, "在吗，想合作")]
        edge.send({"send_id": "mac-1", "chat_id": CREATOR, "text": "你好", "uploads": [], "ttl_sec": 1800})
        edge.tick()
        [result] = [e for e in edge.store.events_after(0, 50) if e["type"] == P.SEND_RESULT]
        assert result["status"] == P.FAILED and "「小美」" in result["error"] and "备注" in result["error"]
        assert window.typed == []
    finally:
        backend._running = False
        edge.stop()
