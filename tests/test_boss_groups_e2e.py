"""老板拉的群 (PRM_BOSS_GROUPS, 功能 U) end to end: the boss of PRM_GROUP_WITH makes a small WeChat group of himself, a
creator and this account, and the bot takes her over.  Each part has its own tests (the backend on the fake database:
tests/test_boss_groups_backend.py; PRM on a fake backend: tests/test_prm_boss_groups.py); here they run together.

The REAL WcdbBackend over the fake WeChat 4 database of tests/test_boss_groups_backend.py (session rows without names,
names from get_display_names, group members, messages newest first, this account's own lines from its wxid without the
data folder's suffix), the real router and route steps, the relay, PrmService and the console's API, wired as Bot.run
wires them (src/bot.py: the relay and PRM send through the backend's send_text; ``_start_prm``: the private filter, the
own-message hook ``on_own_message``, ``boss_groups.attach(backend, stored=the message store's get_messages_since)``,
``prm.start()``; then the backend's start: its chats resolved, what they show primed as seen).  The scan PRM's sender
thread runs once a minute is called directly, and so is the sender thread's round (``tick``); the Mac takes the prm jobs
and finishes them through the relay as the worker does.  WeChat's window is a fake: the search opens the chat of the
name typed (SEND_VERIFY_CHAT reads its title, a group's with its member count), and what is sent there shows up as this
account's line in the database, as WeChat writes it.

Every name and id is made up: the boss is 老板测试, the creator 小美.
"""

import json
import logging

import pytest

from router_support import ADMIN, BOT, TraceSpy
from test_boss_groups_backend import REVOKE_XML, GroupDb
from test_new_friends import ME, ME_SENDER, Clock
from test_prm import draft
from test_prm_catchup import text_lines
from test_prm_console_api import call

from src.admin import RELAY_HELP, AdminCommandHandler
from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.prm import boss_groups as bg
from src.prm.service import GROUP_UNSENT, SETTLE_SEC, PrmService
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.router import MessageRouter
from src.web import server
from src.wechat import wcdb_backend
from src.wechat.wcdb_backend import WcdbBackend

BOSS, BOSS_NAME = "wxid_boss01", "老板测试"
HER, HER_NAME = "wxid_creator01", "小美"
TESTER, TESTER_NAME = "wxid_creator02", "测试达人"
COLLEAGUE = "wxid_colleague01"
OWN_NAME = "商务号测试"
OLD_GROUP = "100000001@chatroom"            # the boss's group from before the switch: the baseline
NOTICE_GROUP = "100000002@chatroom"         # another group the bot reads, the boss and she are in it too
NOTICE_NAME = "公司通知群"
GROUP = "200000001@chatroom"                # the boss's new group with her
GROUP_NAME = "小美合作群"
OURS = "300000001@chatroom"                 # the group 「拉三方群」 makes
INTRO = "给大家介绍一下，这是小美，以后由品牌的运营和你对接"
INVITED = f'"{BOSS_NAME}"邀请你加入了群聊'    # WeChat's line when we are added (GroupDb.group writes it)
PICK = "/api/prm/boss-groups/pick"
# WECHAT_GROUPS: every group, or only one that is not hers (the watch alone makes the bot read her group)
GROUPS = pytest.mark.parametrize("groups", [("*",), (NOTICE_NAME,)], ids=["WECHAT_GROUPS=*", "WECHAT_GROUPS=notice"])


class Window:
    """WeChat's window as the backend drives it (window_controller ``send_to_chat``): the search for a name opens the
    chat of that name, SEND_VERIFY_CHAT's ``check`` reads its title (``title``: a group's shows its member count), the
    text goes out and WeChat shows it as this account's line in that chat.  ``typed``: (name searched, text)."""

    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        self.typed: list[tuple[str, str]] = []
        self.opened = ""

    def _chat(self, name):
        chats = [chat for chat, n in self.db.names.items() if n == name]
        return chats[0] if len(chats) == 1 else None

    def send_to_chat(self, name, text, max_retries=2, check=None):
        self.opened = name
        refused = check(100) if check is not None else None
        if refused is not None:
            return refused
        self.typed.append((name, text))
        chat = self._chat(name)
        if chat is not None:
            self.db.line(chat, ME_SENDER, text, self.clock.now)
        return True

    def title(self, hwnd):
        chat = self._chat(self.opened)
        if chat is not None and chat.endswith("@chatroom"):
            return f"{self.opened} ({len(self.db.members.get(chat, []))})"
        return self.opened

    def find_hwnd(self, *args, **kwargs):
        return None


class World:
    """What outlives a restart of the bot: WeChat (its database and its window), the bot's message store, the relay's
    and PRM's databases, the clock."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.clock = Clock()
        self.db = GroupDb(self.clock)
        self.db.names.update({BOSS: BOSS_NAME, HER: HER_NAME, TESTER: TESTER_NAME, COLLEAGUE: "同事测试",
                              ME_SENDER: OWN_NAME})
        t = self.clock.now
        self.db.group(OLD_GROUP, "公司老群", [BOSS, COLLEAGUE, ME_SENDER], t - 30 * 86400)
        self.db.group(NOTICE_GROUP, NOTICE_NAME, [BOSS, COLLEAGUE, HER, ME_SENDER], t - 20 * 86400)
        self.window = Window(self.db, self.clock)
        self.messages = MessageStore(initialize_db(str(tmp_path / "messages.db")))
        self.relay_store = RelayStore(":memory:")
        self.prm_store = PrmStore(":memory:")
        self.runs: list = []

    def run(self, **settings):
        run = Run(self, **settings)
        self.runs.append(run)
        return run

    def stop(self):
        for run in self.runs:
            run.stop()

    def jobs(self):
        return list(reversed(self.relay_store.list_jobs(limit=100)))           # oldest first

    def open_job(self, chat=GROUP):
        return self.relay_store.latest_open(chat, drafts_only=True, system=False)

    def stored(self, chat):
        return self.messages.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat,)).fetchone()[0]

    def events(self, deal_id, kind):
        return list(reversed(self.prm_store.events(deal_id, limit=200, kinds=(kind,))))   # oldest first


class Run:
    """One run of the bot (copilot mode: PRIVATE_CHAT_ENABLED, the admin's chat, the relay, PRM), wired as Bot.run wires
    it.  What reached the route (``routed``) and the own-message hook (``own``) is written down."""

    def __init__(self, world, *, on=True, groups=("*",), **settings):
        self.world = world
        config = BotConfig(
            private_chat_enabled=True, private_chats=ADMIN, admin_wxid=ADMIN, bot_display_name=BOT,
            claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
            claude_relay_db=":memory:", prm_copilot="on", prm_db=":memory:",
            prm_boss_groups="on" if on else "off", prm_group_with=BOSS_NAME, **settings)
        self.routed: list[dict] = []
        self.own: list[dict] = []
        self.router = MessageRouter(store=world.messages, admin_handler=AdminCommandHandler(), config=config,
                                    tracer=TraceSpy())
        backend = self.backend = WcdbBackend(bot_display_name=BOT, groups=list(groups), poll_sec=0,
                                             store=world.messages, config=config)
        backend._client = world.db
        backend._window = world.window
        backend._title_reader = world.window.title             # (SEND_VERIFY_CHAT reads the open chat's title)
        # Bot._start_claude_relay
        self.relay = ClaudeRelay(config, store=world.relay_store, clock=world.clock, files_dir=world.tmp / "relay",
                                 send=backend.send_text, send_file=backend.send_file,
                                 reserved=self.router._admin.is_command, extra_help=RELAY_HELP)
        self.router._claude_relay = self.relay
        # Bot._start_prm
        self.prm = PrmService(config, self.relay, store=world.prm_store, clock=world.clock, send=backend.send_text,
                              send_file=backend.send_file, on_parties_changed=backend.refresh_private_chats_soon,
                              scrub_messages=world.messages.rewrite_chat, pause=lambda sec: False)
        backend.set_private_filter(self.prm.is_party_chat)
        backend.set_own_message_hook(self._own)
        self.prm.boss_groups.attach(backend, stored=world.messages.get_messages_since)
        self.prm._stopping.set()             # (its sender thread ends at once: the test runs its rounds, ``tick``)
        self.prm.start()
        self.router._prm = self.prm
        server.register_prm(self.prm)
        # WcdbBackend.start
        backend._resolve_groups()
        backend._prime_known_ids()
        backend._running = True

    def _own(self, msg):
        self.own.append(dict(msg))
        self.prm.on_own_message(msg)

    def _route(self, msg):
        self.routed.append(dict(msg))
        return self.router.handle(msg)

    def poll(self):
        """One cycle of the poll loop, the bursts still held handed on too."""
        self.backend._poll_cycle(self._route)
        for msg in self.backend._burst.flush_all():
            self.backend._dispatch_message(msg, self._route)

    def scan(self):
        """The 老板拉的群 scan, a minute after the last one (PRM's sender thread runs it once a minute)."""
        self.world.clock.now += bg.BOSS_SCAN_SEC + 1
        return self.prm.boss_groups.scan()

    def tick(self):
        """One round of PRM's sender thread (approved drafts out, holding replies, the scan when due …)."""
        self.prm.tick()

    def lines(self, chat=GROUP):
        """What reached the route from a chat: (sender, words; WeChat's line: its text)."""
        return [(m["sender_id"], m["special"].get("title") if m["msg_type"] == 10000 else m["content"])
                for m in self.routed if m["chat_id"] == chat]

    def stop(self):
        self.backend._running = False
        self.prm.stop()
        self.relay.stop()
        server.register_prm(None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # data/group_names.json, data/private_chat_names.json …
    monkeypatch.setattr(wcdb_backend, "VERIFY_PAUSE_SEC", 0)
    w = World(tmp_path)
    yield w
    w.stop()


def boss_makes(world, members=(BOSS, HER, ME_SENDER), chat=GROUP, name=GROUP_NAME, intro=INTRO):
    """The boss makes his group on the phone (WeChat's line that we were added) and introduces her."""
    t = world.clock.now
    world.db.group(chat, name, list(members), t - 40)
    if intro:
        world.db.line(chat, BOSS, intro, t - 30)


def opening_asked(world, run):
    """The sender thread's round once the 开场白 may be asked for: the watch's catch-up ran (``watch_caught_up``), or,
    for a backend that cannot say so, OPENING_WAIT_SEC after the takeover."""
    world.clock.now += bg.OPENING_WAIT_SEC
    run.tick()


def taken_over(world, run):
    """The first scan keeps the baseline; the boss makes the group while the bot runs; the next scan takes her over,
    the next poll reads the group (the watch's catch-up) and the sender thread asks for the group 开场白.  Her party."""
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world)
    run.poll()
    assert run.scan() == 1
    run.poll()
    opening_asked(world, run)
    return world.prm_store.party_by_wxid(HER)


def mac_writes(run, reply, kind="faq"):
    """The Mac worker takes the next prm job and hands its draft back (the relay's result as the worker posts it):
    the job and its packet."""
    run.world.clock.now += SETTLE_SEC + 1
    job = run.relay.next_job(0, ("prm",))
    assert job is not None
    answer = draft(message_kind=kind, reply_draft=reply, slots={}, stage_proposal="", reason="kb/合作政策.md")
    assert run.relay.finish(job["id"], {"ok": True, "text": json.dumps(answer, ensure_ascii=False), "duration_s": 3})
    run.relay.tick()
    return job, run.prm.store.packet_for_job(job["id"])


def approve(packet_id):
    """「发送」 in 待审批 (the console's POST)."""
    return call("/api/prm/packets", "POST", {"id": packet_id, "action": "send"})[1]


# ── 1. the takeover ────────────────────────────────────────────────────────

@GROUPS
def test_the_scan_finds_the_bosss_new_group_through_the_backend_and_takes_her_over(world, groups):
    run = world.run(groups=groups)
    assert run.prm.boss_groups.scan() == 0                                    # the first scan: the baseline
    assert set(json.loads(world.prm_store.meta_value(bg.BASELINE))) == {OLD_GROUP, NOTICE_GROUP}
    boss_makes(world)
    run.poll()
    assert run.lines() == [] and GROUP not in run.backend._chat_names         # made after the start: not read
    found = world.clock.now + bg.BOSS_SCAN_SEC + 1
    assert run.scan() == 1

    [party] = world.prm_store.parties()
    assert (party["wxid"], party["name"], party["note"]) == (HER, HER_NAME, f"老板介绍（老板拉的群：{GROUP_NAME}）")
    deal = world.prm_store.deal_of(party["id"])
    assert deal["three_party"] == {"state": "created", "at": found, "by": "boss", "chat": GROUP, "name": GROUP_NAME,
                                   "members": 3, "why": "老板拉的群"}
    row = world.prm_store.boss_group(GROUP)
    assert (row["state"], row["party_id"], row["deal_id"]) == ("linked", party["id"], deal["id"])
    assert row["boss"] == [{"wxid": BOSS, "name": BOSS_NAME}]                 # found by contact_ids
    assert [m["wxid"] for m in row["members"]] == [BOSS, HER]                 # (us left out: own_wxid())
    assert row["made_at"] == found - bg.BOSS_SCAN_SEC - 1 - 40                # the group's oldest line
    assert GROUP in run.backend._watched and run.backend._watched[GROUP]["since"] <= row["made_at"]
    assert run.prm.is_party_group(GROUP) and not run.prm.is_party_group(NOTICE_GROUP)
    assert world.jobs() == []                                                 # the 开场白 waits for the catch-up

    # the next poll reads the group whatever WECHAT_GROUPS says: WeChat's line and the boss's introduction go through
    # the route (stored, unanswered), his words kept for her next draft
    run.poll()
    assert run.backend._chat_names[GROUP] == GROUP_NAME
    assert run.lines() == [("", INVITED), (BOSS, INTRO)]
    assert world.stored(GROUP) == 2 and world.jobs() == [] and world.window.typed == []
    assert [(x["who"], x["text"]) for x in run.prm.boss_groups.job_group(GROUP)["lines"]] == [("老板", INTRO)]
    opening_asked(world, run)
    [job] = world.jobs()                                                      # the group 开场白
    meta = json.loads(job["meta"])
    assert (job["kind"], job["chat_id"], job["text"]) == ("prm", GROUP, "")
    assert meta["opening"] == {"how": "boss_group", "by": "boss_group"} and meta["channel"] == "group"
    assert (meta["group"]["name"], meta["group"]["boss"]) == (GROUP_NAME, ["老板"])    # (his name stays on this PC)
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("老板", INTRO)]  # what he said, before it
    assert meta["three_party"]["at"] and BOSS_NAME not in job["meta"]
    shown = call("/api/prm")[1]
    assert shown["boss_groups"] == {"enabled": True, "why_not": "", "warning": "", "boss": [BOSS_NAME],
                                    "boss_call": "老板", "pending": [], "waiting": 0, "linked": 1}
    her = next(p for p in shown["parties"] if p["wxid"] == HER)
    assert her["source"] == her["deal"]["source"] == "boss_group"
    assert (her["deal"]["three_party"]["by"], her["deal"]["three_party"]["chat_name"]) == ("boss", GROUP_NAME)
    run.poll()
    assert len(run.lines()) == 2                                              # nothing twice

    # the 开场白: it waits for a person even in 自动模式, then goes into the group by the name WeChat gives it; its line
    # read back is ours
    run.prm.set_auto_mode(True)
    opening = "你好呀～我是品牌的运营，谢谢老板介绍！之后合作上的事都由我来跟你对接～"
    taken, packet = mac_writes(run, opening, kind="greeting")
    assert taken["id"] == job["id"]
    assert (packet["status"], packet["route"], packet["chat_id"], packet["party_id"]) == (
        "pending", "review", GROUP, party["id"])
    out = next(p for p in call("/api/prm")[1]["pending"] if p["id"] == packet["id"])
    assert (out["channel"], out["group_name"], out["opening"]) == ("group", GROUP_NAME, True)
    run.tick()
    assert world.window.typed == []
    assert approve(packet["id"]) == {"ok": True, "status": "approved"}
    run.tick()
    assert world.window.typed == [(GROUP_NAME, opening)]
    run.poll()
    assert [(m["chat_id"], m["content"], m["is_group"]) for m in run.own] == [(GROUP, opening, True)]
    sent = world.prm_store.packet(packet["id"])
    assert (sent["status"], sent["outcome"]) == ("sent", "sent_confirmed")
    deal = world.prm_store.deal(deal["id"])
    assert deal["mode"] == "copilot" and world.events(deal["id"], "operator_message") == []
    assert run.lines() == [("", INVITED), (BOSS, INTRO)]                      # (ours never goes through the route)


def test_the_opening_the_mac_takes_tells_the_bosss_introduction(world):
    """The boss introduced her in the group before the scan found it (his words come seconds after he makes it, the
    scan up to a minute later): the 开场白 is asked for at the sender thread's first round after the watch's catch-up
    (``watch_caught_up``), and the Mac writes our first line knowing what he said (worker ``group_context_block``)."""
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world)
    run.poll()
    assert run.scan() == 1 and world.jobs() == []
    run.poll()                                                                # the watch's catch-up
    assert run.lines() == [("", INVITED), (BOSS, INTRO)]                      # PRM has his words by now
    world.clock.now += 1
    run.tick()                                                                # (no 90 s wait)
    world.clock.now += SETTLE_SEC + 1
    job = run.relay.next_job(0, ("prm",))                                     # the Mac takes the 开场白
    assert job["meta"]["opening"]["by"] == "boss_group"
    assert [(x["who"], x["text"]) for x in job["meta"]["group"]["lines"]] == [("老板", INTRO)]


def test_a_backend_that_cannot_say_its_catch_up_ran_gets_the_opening_90_seconds_on(world):
    """This backend has no ``watch_caught_up``: the 开场白 is asked for OPENING_WAIT_SEC after the takeover (the catch-up
    ran by then), with the boss's introduction; once, never before.  (With the backend part's method it comes at the
    first round after the catch-up: the test above.)"""
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world)
    run.poll()
    assert run.scan() == 1
    run.poll()
    run.backend.watch_caught_up = None                                        # (a backend without it)
    world.clock.now += bg.OPENING_WAIT_SEC - 2
    run.tick()
    assert world.jobs() == []
    world.clock.now += 2
    run.tick()
    run.tick()
    [job] = world.jobs()
    meta = json.loads(job["meta"])
    assert meta["opening"]["by"] == "boss_group" and [x["text"] for x in meta["group"]["lines"]] == [INTRO]


def test_a_group_made_while_the_bot_was_stopped_hands_on_what_was_said_before_the_takeover_once(world):
    """The boss made the group while the bot was stopped (WECHAT_GROUPS=* reads it from the start on: its lines then are
    history) and wrote again after the start (the route kept that line and said nothing).  The takeover hands the kept
    line on from the message store (``_replay``), the watch reads the group back to its oldest line (``made_at``) and
    hands on the rest: her words are one reply job (read late: reviewed), no 开场白 at all (it waits for the catch-up,
    and she spoke), and no line reaches PRM twice."""
    first = world.run()
    assert first.prm.boss_groups.scan() == 0
    first.stop()
    t = world.clock.now
    world.db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t + 10)
    world.db.line(GROUP, BOSS, INTRO, t + 20)
    world.db.line(GROUP, HER, "老板好～我是小美", t + 30)
    world.clock.now = t + 600                                                 # started again ten minutes on
    second = world.run()
    assert GROUP in second.backend._chat_names
    more = "她主要带美妆，你们聊"
    world.db.line(GROUP, BOSS, more, world.clock.now)
    second.poll()
    assert second.lines() == [(BOSS, more)] and world.stored(GROUP) == 1 and world.jobs() == []   # not hers yet
    assert second.scan() == 1
    assert second.backend._watched[GROUP]["since"] == t + 10                  # back to its oldest line
    assert world.jobs() == []                                                 # (the 开场白 waits for the catch-up)
    second.poll()
    assert second.lines() == [(BOSS, more), ("", INVITED), (BOSS, INTRO), (HER, "老板好～我是小美")]
    [job] = world.jobs()
    meta = json.loads(job["meta"])
    assert text_lines(job) == ["老板好～我是小美"] and meta["late"] == {"from": t + 30} and meta["channel"] == "group"
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("老板", INTRO), ("老板", more)]
    opening_asked(world, second)
    second.poll()
    assert world.jobs() == [job]                                              # she spoke: no 开场白
    assert len(second.lines()) == 4 and world.window.typed == []
    _, packet = mac_writes(second, "你好呀小美～")
    assert packet["status"] == "pending" and packet["chat_id"] == GROUP      # (read late: a person decides)


def test_a_group_nobody_named_is_never_typed_into_until_it_has_a_name(world, monkeypatch):
    """A group made on the phone may have no name: WeChat's search cannot find it by its id, so a draft for it is typed
    nowhere (the backend's GROUP_NO_NAME), it fails and her deal says so; once the boss names the group the next draft
    goes into it by that name."""
    monkeypatch.setattr(wcdb_backend, "SESSION_REFRESH_SEC", 0)               # (a group's name is asked for each poll)
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world, name="")
    run.poll()
    assert run.scan() == 1
    assert world.prm_store.party_by_wxid(HER)["note"] == "老板介绍（老板拉的群：没有群名）"
    run.poll()
    assert run.backend._chat_names[GROUP] == GROUP and run.lines() == [("", INVITED), (BOSS, INTRO)]   # read by its id
    world.clock.now += 5
    world.db.line(GROUP, HER, "在吗", world.clock.now)
    run.poll()
    _, packet = mac_writes(run, "在的～")
    assert approve(packet["id"])["ok"] is True
    run.tick()
    failed = world.prm_store.packet(packet["id"])
    assert (failed["status"], failed["error"]) == ("failed", wcdb_backend.GROUP_NO_NAME) and world.window.typed == []
    notice = next(n for n in world.prm_store.deal(packet["deal_id"])["notices"] if n["key"] == GROUP_UNSENT)
    assert notice["text"].startswith(f"#{packet['id']} 没发到老板拉的群里")
    world.db.names[GROUP] = GROUP_NAME                                        # the boss names it
    run.poll()
    assert run.backend._chat_names[GROUP] == GROUP_NAME
    world.clock.now += 5
    world.db.line(GROUP, HER, "还在吗", world.clock.now)
    run.poll()
    _, again = mac_writes(run, "在的在的～")
    assert approve(again["id"])["ok"] is True
    run.tick()
    assert world.window.typed == [(GROUP_NAME, "在的在的～")]
    assert not any(n["key"] == GROUP_UNSENT for n in world.prm_store.deal(packet["deal_id"])["notices"])


def test_a_group_of_only_the_boss_and_us_waits_and_she_is_taken_over_once_he_pulls_her_in(world):
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world, members=(BOSS, ME_SENDER), intro="")
    world.db.line(GROUP, BOSS, "等下拉个达人进来", world.clock.now - 20)
    run.poll()
    assert run.scan() == 0 and world.prm_store.boss_group(GROUP)["state"] == "waiting"
    assert call("/api/prm")[1]["boss_groups"]["waiting"] == 1
    assert run.scan() == 0 and run.backend._watched == {} and world.jobs() == []      # still nobody
    joined = f'"{BOSS_NAME}"邀请"{HER_NAME}"加入了群聊'
    world.db.members[GROUP].append(HER)
    world.db.system(GROUP, joined, world.clock.now)
    world.db.line(GROUP, BOSS, INTRO, world.clock.now + 1)
    world.clock.now += 2
    assert run.scan() == 1                                                    # (group_members: she is in it now)
    party = world.prm_store.party_by_wxid(HER)
    assert party["name"] == HER_NAME and world.prm_store.boss_group(GROUP)["state"] == "linked"
    run.poll()
    assert run.lines() == [("", INVITED), (BOSS, "等下拉个达人进来"), ("", joined), (BOSS, INTRO)]
    assert [x["text"] for x in run.prm.boss_groups.job_group(GROUP)["lines"]] == ["等下拉个达人进来", INTRO]
    opening_asked(world, run)
    [opening] = world.jobs()
    meta = json.loads(opening["meta"])
    assert meta["opening"]["by"] == "boss_group" and opening["chat_id"] == GROUP
    assert [x["text"] for x in meta["group"]["lines"]] == ["等下拉个达人进来", INTRO]
    assert world.window.typed == []


# ── 2. her line: drafted, sent into the group, read back; a person's line takes over ──

@GROUPS
def test_her_line_is_drafted_sent_into_the_group_by_its_name_and_read_back_as_ours(world, groups):
    run = world.run(groups=groups)
    party = taken_over(world, run)
    [opening] = world.jobs()
    world.clock.now += 5
    asked = "老板好～你们家主要是什么产品呀"
    world.db.line(GROUP, HER, asked, world.clock.now)
    run.poll()
    assert run.lines()[-1] == (HER, asked) and run.routed[-1]["sender_name"] == HER_NAME
    assert world.relay_store.get(opening["id"])["status"] == "cancelled"       # she spoke first: no 开场白
    job = world.open_job()
    meta = json.loads(job["meta"])
    assert job["chat_id"] == GROUP and text_lines(job) == [asked] and "opening" not in meta
    assert meta["channel"] == "group" and (meta["group"]["name"], meta["group"]["boss"]) == (GROUP_NAME, ["老板"])
    assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("老板", INTRO)]

    reply = "你好～我们主要做帽子，之后由我跟你对接哈"
    taken, packet = mac_writes(run, reply)
    assert taken["id"] == job["id"]
    assert (packet["status"], packet["chat_id"], packet["party_id"]) == ("pending", GROUP, party["id"])
    assert (packet["meta"]["channel"], packet["meta"]["group_name"]) == ("group", GROUP_NAME)
    out = next(p for p in call("/api/prm")[1]["pending"] if p["id"] == packet["id"])
    assert (out["channel"], out["group_name"]) == ("group", GROUP_NAME)
    assert world.window.typed == []                                           # nothing said by itself
    assert approve(packet["id"]) == {"ok": True, "status": "approved"}
    run.tick()
    assert world.window.typed == [(GROUP_NAME, reply)]                        # the group, by its name
    run.poll()                                                                # WeChat shows our line
    assert [(m["chat_id"], m["sender_id"], m["content"]) for m in run.own] == [(GROUP, ME_SENDER, reply)]
    sent = world.prm_store.packet(packet["id"])
    assert (sent["status"], sent["outcome"]) == ("sent", "sent_confirmed")
    deal_id = packet["deal_id"]
    assert world.prm_store.deal(deal_id)["mode"] == "copilot" and world.events(deal_id, "operator_message") == []
    assert run.lines()[-1] == (HER, asked)                                    # (ours never goes through the route)

    # an operator types in the group by hand: her deal is a person's for PRM_TAKEOVER_MIN
    world.clock.now += 30
    by_hand = "小美你好，我是品牌的运营小王，有问题随时找我"
    world.db.line(GROUP, ME_SENDER, by_hand, world.clock.now)
    run.poll()
    deal = world.prm_store.deal(deal_id)
    assert deal["mode"] == "human" and deal["mode_until"] == world.clock.now + 120 * 60
    [ev] = world.events(deal_id, "operator_message")
    assert ev["detail"]["text"].endswith(by_hand)
    world.clock.now += 30
    world.db.line(GROUP, HER, "好的谢谢", world.clock.now)
    run.poll()
    assert world.open_job() is None and world.window.typed == [(GROUP_NAME, reply)]   # recorded, not drafted
    assert [ev["detail"]["text"].endswith("好的谢谢") for ev in world.events(deal_id, "creator_message")] == [True]


def test_a_reply_in_parts_goes_into_the_group_as_its_messages_each_read_back_as_ours(world):
    """PRM_REPLY_PARTS (3 by default): a draft's paragraphs go into the group by its name as messages of their own;
    WeChat shows each as this account's line, and each read back is the bot's (no takeover)."""
    run = world.run()
    taken_over(world, run)
    world.clock.now += 5
    world.db.line(GROUP, HER, "你们家的帽子多少钱呀", world.clock.now)
    run.poll()
    parts = ["你好～", "均价两三百", "你平时主要带什么呀"]
    _, packet = mac_writes(run, "\n\n".join(parts))
    assert approve(packet["id"])["ok"] is True
    run.tick()
    assert world.window.typed == [(GROUP_NAME, part) for part in parts]
    run.poll()
    assert [m["content"] for m in run.own] == parts
    sent = world.prm_store.packet(packet["id"])
    assert (sent["status"], sent["outcome"]) == ("sent", "sent_confirmed")
    deal = world.prm_store.deal(packet["deal_id"])
    assert deal["mode"] == "copilot" and world.events(deal["id"], "operator_message") == []


def test_her_recall_in_the_group_holds_her_draft_for_a_person(world):
    """WeChat takes a line back by rewriting its row: the backend hands it on as WeChat's line, PRM holds the drafts
    not sent (they may answer what she took back)."""
    run = world.run()
    party = taken_over(world, run)
    world.clock.now += 5
    world.db.line(GROUP, HER, "这个价格可以", world.clock.now)
    run.poll()
    _, packet = mac_writes(run, "好的～那我给你安排样品")
    row = next(r for r in world.db.chats[GROUP] if r["message_content"].endswith("这个价格可以"))
    row.update(local_type=10000, message_content=REVOKE_XML)                  # WeChat rewrites the row in place
    run.poll()
    run.poll()
    assert run.lines()[-2:] == [(HER, "这个价格可以"), (HER, f'"{HER_NAME}" 撤回了一条消息')]
    held = world.prm_store.packet(packet["id"])
    assert held["status"] == "pending" and held["needs_attention"] and "撤回" in held["reason"]
    deal = world.prm_store.deal_of(party["id"])
    assert [n["key"] for n in deal["notices"]].count("recall") == 1
    assert [e["detail"]["by"] for e in world.events(deal["id"], "recall")] == ["creator"]
    assert world.open_job() is None and world.window.typed == []


def test_once_she_adds_us_her_private_chat_is_the_same_deal_and_each_reply_goes_where_she_wrote(world, monkeypatch):
    monkeypatch.setattr(wcdb_backend, "SESSION_REFRESH_SEC", 0)               # (new private chats looked for each poll)
    run = world.run()
    party = taken_over(world, run)
    world.clock.now += 5
    world.db.line(GROUP, HER, "老板好", world.clock.now)
    run.poll()
    _, in_group = mac_writes(run, "你好呀～")
    world.clock.now += 30
    world.db.system(HER, f"你已添加了{HER_NAME}，现在可以开始聊天了。", world.clock.now - 1)
    world.db.say(HER, HER, "加你啦，私聊问一下佣金", world.clock.now)
    run.poll()
    assert run.backend._chat_names[HER] == HER_NAME                           # read: she is a creator
    private = world.open_job(HER)
    meta = json.loads(private["meta"])
    assert private["chat_id"] == HER and text_lines(private) == ["加你啦，私聊问一下佣金"]
    assert "channel" not in meta and "group" not in meta and meta["deal_id"] == in_group["deal_id"]
    assert world.prm_store.packet(in_group["id"])["status"] == "pending"      # not out of date by her other chat
    _, in_private = mac_writes(run, "佣金按合作政策来哈")
    assert in_private["chat_id"] == HER and "channel" not in in_private["meta"]
    for packet in (in_group, in_private):
        assert approve(packet["id"])["ok"] is True
    run.tick()
    assert world.window.typed == [(GROUP_NAME, "你好呀～"), (HER_NAME, "佣金按合作政策来哈")]
    run.poll()
    assert [world.prm_store.packet(p["id"])["outcome"] for p in (in_group, in_private)] == ["sent_confirmed"] * 2
    deal = world.prm_store.deal_of(party["id"])
    assert deal["mode"] == "copilot" and world.events(deal["id"], "operator_message") == []


def ship(world, run):
    """填运单号 on her deal in the console: the shipping notice the bot starts itself, approved and sent."""
    deal_id = world.prm_store.boss_group(GROUP)["deal_id"]
    out = call("/api/prm/deals", "POST", {"action": "shipment", "deal_id": deal_id, "carrier": "顺丰",
                                           "waybill": "SF1234567890"})[1]
    assert out["ok"] is True
    assert approve(out["packet"])["ok"] is True
    run.tick()
    return world.prm_store.packet(out["packet"])


def test_what_the_bot_starts_itself_goes_to_the_group_while_she_is_no_friend_of_ours(world):
    """A shipping notice answers no line of hers: it goes to her private chat only when WeChat lists one (the real
    backend's ``chat_target``); she is only in the boss's group, so it goes there, by its name."""
    run = world.run()
    taken_over(world, run)
    packet = ship(world, run)
    assert packet["chat_id"] == GROUP and (packet["meta"]["channel"], packet["meta"]["group_name"]) == (
        "group", GROUP_NAME)
    assert world.window.typed == [(GROUP_NAME, "老师您好～样品已经寄出啦：顺丰 SF1234567890。签收之后跟我说一声哦～")]
    run.poll()
    assert world.prm_store.packet(packet["id"])["outcome"] == "sent_confirmed"


def test_looking_for_her_private_chat_logs_no_error_and_not_her_wxid(world, caplog):
    """Review (8): where a packet the bot starts itself goes is asked of the backend's quiet ``has_private_chat`` (this
    one has none: not known), never of the send path's ``chat_target``, which logs an error with her wxid."""
    run = world.run()
    taken_over(world, run)
    caplog.set_level(logging.INFO)
    ship(world, run)
    assert world.window.typed                                                 # (it went to the group)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert HER not in "\n".join(r.getMessage() for r in caplog.records)


# ── 3. the other lines: context in her group, silence everywhere else ─────────

def test_the_bosss_lines_are_context_and_every_other_group_stays_silent(world):
    run = world.run()
    taken_over(world, run)
    [opening] = world.jobs()
    t = world.clock.now
    world.db.line(GROUP, BOSS, "寄样的事你们跟她对一下，有事打我电话 13812345678", t + 1)
    world.db.line(GROUP, BOSS, f"@{BOT} 你们先聊", t + 2)
    world.db.line(NOTICE_GROUP, HER, f"@{BOT} cc 帮我查一下订单", t + 3)     # she is a creator now: not there
    world.db.line(NOTICE_GROUP, BOSS, "明天上午开会", t + 4)
    world.db.line(NOTICE_GROUP, COLLEAGUE, f"@{BOT} 同意好友", t + 5)
    world.db.line(OLD_GROUP, BOSS, "上次说的那个事", t + 6)
    world.clock.now = t + 10
    run.poll()
    assert run.lines()[-2:] == [(BOSS, "寄样的事你们跟她对一下，有事打我电话 13812345678"), (BOSS, f"@{BOT} 你们先聊")]
    assert [sender for sender, _ in run.lines(NOTICE_GROUP)] == [HER, BOSS, COLLEAGUE]
    assert run.lines(OLD_GROUP) == [(BOSS, "上次说的那个事")]
    assert world.stored(NOTICE_GROUP) == 3 and world.stored(OLD_GROUP) == 1   # read, kept, unanswered
    assert world.jobs() == [opening] and world.open_job(NOTICE_GROUP) is None  # no job: not hers, not her group
    assert world.window.typed == []
    lines = run.prm.boss_groups.job_group(GROUP)["lines"]
    assert [(x["who"], x["text"]) for x in lines] == [("老板", INTRO), ("老板", "寄样的事你们跟她对一下，有事打我电话 〈手机1〉"),
                                                      ("老板", f"@{BOT} 你们先聊")]

    # her line there is a draft; it waits past PRM_HOLDING_MIN and the group gets no 「稍等」
    world.clock.now += 5
    world.db.line(GROUP, HER, "好的老板", world.clock.now)
    run.poll()
    _, packet = mac_writes(run, "收到～")
    assert packet["status"] == "pending" and packet["chat_id"] == GROUP
    world.clock.now += 11 * 60
    run.tick()
    assert world.window.typed == [] and world.prm_store.packet(packet["id"])["status"] == "pending"


def test_she_leaves_the_group_and_her_deal_says_so_until_she_is_back(world):
    """WeChat may show the others no line when she quits: the scan reads the linked group's members (``group_members``,
    the real backend)."""
    run = world.run()
    party = taken_over(world, run)
    deal_id = world.prm_store.deal_of(party["id"])["id"]
    world.db.members[GROUP].remove(HER)
    assert run.scan() == 0
    assert "她已经不在老板拉的群里了" in world.prm_store.deal(deal_id)["attention"]
    world.db.members[GROUP].append(HER)
    assert run.scan() == 0
    assert "老板拉的群" not in world.prm_store.deal(deal_id)["attention"]


def test_the_group_gone_stops_the_watch_and_her_lines_there_are_silent_again(world):
    """「群已经不在了」 on her deal (the console's POST): the backend stops reading the group (only the watch read it), her
    lines there are never drafted again; her deal goes on."""
    run = world.run()
    party = taken_over(world, run)
    deal_id = world.prm_store.deal_of(party["id"])["id"]
    assert call("/api/prm/deals", "POST", {"action": "group_none", "deal_id": deal_id})[1]["ok"] is True
    assert world.prm_store.boss_group(GROUP)["state"] == "gone"
    assert GROUP not in run.backend._watched and not run.prm.is_party_group(GROUP)
    world.clock.now += 5
    world.db.line(GROUP, HER, "还在吗", world.clock.now)
    run.poll()
    assert GROUP not in run.backend._chat_names and run.lines() == [("", INVITED), (BOSS, INTRO)]
    assert [job for job in world.jobs() if "还在吗" in job["text"]] == [] and world.window.typed == []
    assert world.prm_store.party_by_wxid(HER)["active"] == 1


def test_the_bosss_line_while_her_group_draft_waits_has_it_drafted_again(world):
    """Review (9): the boss says something in the group while her draft there waits in 待审批: the draft did not hear
    it, so it is drafted again with it (her line again, the boss's lines now); once a draft is approved a later line
    leaves it be, and it goes into the group; the lines its job was told are answered then, a later one is kept."""
    run = world.run()
    taken_over(world, run)
    world.clock.now += 5
    world.db.line(GROUP, HER, "你们家帽子多少钱", world.clock.now)
    run.poll()
    _, packet = mac_writes(run, "均价两三百哈")
    assert packet["status"] == "pending"
    world.clock.now += 5
    world.db.line(GROUP, BOSS, "给她按团购价", world.clock.now)
    run.poll()
    assert world.prm_store.packet(packet["id"])["status"] == "superseded"
    job = world.open_job()
    assert text_lines(job) == ["你们家帽子多少钱"]
    assert [x["text"] for x in json.loads(job["meta"])["group"]["lines"]] == [INTRO, "给她按团购价"]
    _, again = mac_writes(run, "好的～按团购价给你")
    assert approve(again["id"])["ok"] is True
    world.clock.now += 1
    world.db.line(GROUP, BOSS, "对", world.clock.now)
    run.poll()
    assert world.prm_store.packet(again["id"])["status"] == "approved" and world.open_job() is None
    run.tick()
    assert world.window.typed == [(GROUP_NAME, "好的～按团购价给你")]
    assert [x["text"] for x in run.prm.boss_groups.job_group(GROUP)["lines"]] == ["对"]


# ── 4. exclusions: our own 三方群; a group the console has to pick in ───────────

def test_an_old_group_whose_chat_was_deleted_is_not_taken_over_when_it_comes_back(world):
    """Review (4): the boss's group of a month ago whose chat was deleted on this PC is not in the baseline (WeChat's
    session list did not have it); a new line brings it back like a new group.  It began for us a month ago: kept
    ignored (「不是新拉的群」), never taken over, never read by the watch."""
    t = world.clock.now
    world.db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 30 * 86400)
    world.db.line(GROUP, BOSS, "上个月说的那个事", t - 29 * 86400)
    world.db.times.pop(GROUP)                                                 # its chat deleted on this PC
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    assert GROUP not in json.loads(world.prm_store.meta_value(bg.BASELINE))
    world.db.line(GROUP, BOSS, "小美在吗", world.clock.now)                   # the chat is back
    run.poll()
    assert run.scan() == 0
    row = world.prm_store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("ignored", "不是新拉的群")
    assert world.prm_store.parties() == [] and GROUP not in run.backend._watched and world.jobs() == []
    assert run.scan() == 0 and world.prm_store.parties() == []


def test_an_old_group_deleted_with_its_history_waits_for_the_consoles_pick(world):
    """Second review F4, repro n3: the boss's group of a month ago whose chat was deleted on this PC together with its
    local history.  A new line brings it back: its history is that one line (complete, recent, no 「邀请你加入了群聊」
    line).  It is no certainly new group: it waits for the console's 「是她」 (「拿不准是不是新拉的群」), never taken over or
    read by itself; 「是她」 takes it over."""
    t = world.clock.now
    world.db.group(GROUP, GROUP_NAME, [BOSS, HER, ME_SENDER], t - 30 * 86400)
    world.db.line(GROUP, BOSS, "上个月说的那个事", t - 29 * 86400)
    world.db.times.pop(GROUP)                                                 # its chat deleted on this PC ...
    world.db.chats.pop(GROUP)                                                 # ... with its local history
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    assert GROUP not in json.loads(world.prm_store.meta_value(bg.BASELINE))
    world.db.line(GROUP, BOSS, "小美在吗", world.clock.now)                   # the chat is back
    run.poll()
    assert run.scan() == 0
    row = world.prm_store.boss_group(GROUP)
    assert (row["state"], row["why"]) == ("pending", bg.MAYBE_WHY)
    assert world.prm_store.parties() == [] and GROUP not in run.backend._watched and world.jobs() == []
    [pending] = call("/api/prm")[1]["boss_groups"]["pending"]
    assert (pending["why"], pending["members"]) == (bg.MAYBE_WHY, [{"wxid": HER, "name": HER_NAME}])
    assert run.scan() == 0 and world.prm_store.parties() == []
    assert call(PICK, "POST", {"chat": GROUP, "wxid": HER})[1]["ok"] is True
    assert run.prm.is_party_group(GROUP) and GROUP in run.backend._watched


class Admin:
    """The admin handler as 「拉三方群」 uses it (``create_group_checked``: WeChat's 「发起群聊」 window, faked): the group of
    the names picked and this account shows up in WeChat's database; ``meanwhile`` runs while the window makes it."""

    can_add_friends = True

    def __init__(self, world, meanwhile=None):
        self.world, self.meanwhile = world, meanwhile
        self.calls: list = []

    def create_group_checked(self, members, *, dry_run=False):
        self.calls.append(list(members))
        if not dry_run:
            self.world.db.group(OURS, "", [TESTER, BOSS, ME_SENDER], self.world.clock.now)
            if self.meanwhile is not None:
                self.meanwhile()
        return {"ok": True, "message": "群建好了"}


@pytest.mark.parametrize("scan_while_made", [False, True], ids=["scanned after", "scanned while it is made"])
def test_the_group_our_own_three_party_click_made_is_never_taken_over(world, scan_while_made):
    """「拉三方群」 on her deal makes the group of her, the boss and us: new, small, the boss in it, like his own.  The real
    backend finds it for the deal (``find_group_with``) and for the scan (``boss_groups``); the scan never takes it."""
    world.db.say(TESTER, TESTER, "你好，想合作", world.clock.now - 3600)       # her private chat (WeChat names it)
    tester = world.prm_store.add_party(TESTER, TESTER_NAME, "", now=world.clock.now - 3600)
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    scans = []
    admin = Admin(world, meanwhile=(lambda: scans.append(run.scan())) if scan_while_made else None)
    run.prm.three_party.attach(admin, None, run.backend)
    run.prm.three_party.spawn = lambda fn: fn()
    run.prm.three_party._sleep = lambda sec: None
    deal_id = world.prm_store.deal_of(tester["id"])["id"]
    assert run.prm.deal_action("group", {"deal_id": deal_id}) == {"ok": True, "state": "creating"}
    assert admin.calls == [[TESTER_NAME, BOSS_NAME]]
    made = world.prm_store.deal(deal_id)["three_party"]
    assert (made["state"], made["by"], made["chat"], made["members"]) == ("created", "console", OURS, 3)
    assert scans == ([0] if scan_while_made else [])
    for _ in range(2):
        assert run.scan() == 0
    assert OURS in json.loads(world.prm_store.meta_value(bg.BASELINE))
    rows = world.prm_store.boss_groups()
    assert [(r["chat"], r["state"], r["why"]) for r in rows] == ([(OURS, "ignored", bg.HAS_GROUP_WHY)] if scan_while_made
                                                                 else [])
    # never taken over as the boss's; read only as her own 三方群 (2026-11-02: its lines go to her private drafts)
    assert OURS in run.backend._watched and not run.prm.is_party_group(OURS)
    assert run.prm.three_party.deal_of_chat(OURS)["id"] == deal_id
    assert world.prm_store.deal(deal_id)["three_party"] == made and len(world.prm_store.parties()) == 1
    # (WECHAT_GROUPS *: the backend's start resolves the chats, primes them): her words and the boss's there are kept
    # for her private drafts and never answered
    run.backend._resolve_groups()
    run.backend._prime_known_ids()
    world.clock.now += 5
    world.db.line(OURS, TESTER, "老板好，我在群里了", world.clock.now)
    world.db.line(OURS, BOSS, "你们聊", world.clock.now + 1)
    world.clock.now += 2
    run.poll()
    # (the watch's catch-up hands on WeChat's line of the group being made too: never kept, never answered)
    assert run.lines(OURS) == [("", f'"{BOSS_NAME}"邀请你加入了群聊'), (TESTER, "老板好，我在群里了"), (BOSS, "你们聊")]
    assert world.stored(OURS) == 3                                            # (the route keeps all three)
    assert [job for job in world.jobs() if job["chat_id"] == OURS] == [] and world.window.typed == []
    kept = [ev["detail"] for ev in world.prm_store.events(deal_id, limit=10, kinds=("group_line",))]
    assert sorted((d["who"], d["text"]) for d in kept) == [("boss", "你们聊"), ("creator", "老板好，我在群里了")]


def test_a_group_with_two_people_she_may_be_waits_for_the_consoles_pick(world):
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world, members=(BOSS, HER, TESTER, ME_SENDER))
    run.poll()
    assert run.scan() == 0
    assert world.prm_store.parties() == [] and run.backend._watched == {} and world.jobs() == []
    [pending] = call("/api/prm")[1]["boss_groups"]["pending"]
    assert (pending["chat"], pending["name"], pending["size"]) == (GROUP, GROUP_NAME, 4)
    assert pending["members"] == [{"wxid": HER, "name": HER_NAME}, {"wxid": TESTER, "name": TESTER_NAME}]
    for wxid in (BOSS, ME, ADMIN, "wxid_nobody01"):
        assert call(PICK, "POST", {"chat": GROUP, "wxid": wxid})[1]["ok"] is False, wxid
    assert world.prm_store.parties() == []
    out = call(PICK, "POST", {"chat": GROUP, "wxid": HER})[1]                 # 「是她」
    party = world.prm_store.party_by_wxid(HER)
    assert out == {"ok": True, "party_id": party["id"], "deal_id": world.prm_store.deal_of(party["id"])["id"]}
    assert party["name"] == HER_NAME and world.prm_store.party_by_wxid(TESTER) is None
    assert GROUP in run.backend._watched and run.prm.is_party_group(GROUP)
    assert call("/api/prm")[1]["boss_groups"]["pending"] == []
    run.poll()                                                                # the catch-up
    assert run.lines() == [("", INVITED), (BOSS, INTRO)]
    opening_asked(world, run)
    [opening] = world.jobs()
    assert json.loads(opening["meta"])["channel"] == "group" and opening["chat_id"] == GROUP
    world.clock.now += 5
    world.db.line(GROUP, TESTER, "大家好呀", world.clock.now)
    run.poll()
    assert world.open_job()["id"] == opening["id"]                            # another member: context, no job
    world.clock.now += 5
    world.db.line(GROUP, HER, "老板好", world.clock.now)
    run.poll()
    job = world.open_job()
    assert job["id"] != opening["id"] and text_lines(job) == ["老板好"]
    assert [(x["who"], x["text"]) for x in json.loads(job["meta"])["group"]["lines"]] == [
        ("老板", INTRO), ("群成员", "大家好呀")]
    assert world.window.typed == []


# ── 5. the switch off ───────────────────────────────────────────────────────

@pytest.mark.parametrize("made", ["while it runs", "before it started"])
def test_with_the_switch_off_the_same_database_gives_no_scan_no_watch_and_no_job(world, made):
    """PRM_BOSS_GROUPS=off: the boss's group, his introduction and her lines there, as the takeover above had them: no
    baseline, nothing found or watched, nothing drafted or sent; a group WECHAT_GROUPS=* reads stays silent."""
    if made == "before it started":
        boss_makes(world)
    run = world.run(on=False)
    asked = []                                                                # the backend's 老板拉的群 readers
    for name in ("group_ids", "contact_ids", "boss_groups", "group_members", "watch_group", "unwatch_group"):
        real = getattr(run.backend, name)
        setattr(run.backend, name, lambda *a, _real=real, _name=name, **k: asked.append(_name) or _real(*a, **k))
    if made == "while it runs":
        boss_makes(world)
    run.poll()
    for _ in range(3):
        assert run.scan() is None
        run.tick()
    world.clock.now += 5
    world.db.line(GROUP, HER, "老板好～你们家主要是什么产品呀", world.clock.now)
    run.poll()
    assert world.prm_store.meta_value(bg.BASELINE) is None and world.prm_store.boss_groups() == []
    assert run.backend._watched == {} and not run.prm.is_party_group(GROUP)
    assert world.prm_store.parties() == [] and world.jobs() == [] and world.window.typed == []
    if made == "before it started":
        assert run.lines() == [(HER, "老板好～你们家主要是什么产品呀")] and world.stored(GROUP) == 1   # read, silent
    else:
        assert run.lines() == [] and GROUP not in run.backend._chat_names
    assert call("/api/prm")[1]["boss_groups"] == {"enabled": False, "why_not": "", "warning": "", "boss": [BOSS_NAME],
                                                   "boss_call": "老板", "pending": [], "waiting": 0, "linked": 0}
    assert asked == []


# ── 6. a restart ─────────────────────────────────────────────────────────────

@GROUPS
def test_a_restarted_bot_watches_the_group_again_and_hands_on_only_what_came_meanwhile(world, groups):
    first = world.run(groups=groups)
    party = taken_over(world, first)
    world.clock.now += 5
    world.db.line(GROUP, HER, "在吗", world.clock.now)
    first.poll()
    last = world.prm_store.boss_group(GROUP)["last_line_at"]
    assert last == world.clock.now
    first.stop()
    t = world.clock.now                                                       # stopped: rebuilt, started again
    world.db.line(GROUP, HER, "我平时主要带美妆", t + 60)
    world.db.line(GROUP, BOSS, "她粉丝挺多的", t + 70)
    world.clock.now = t + 120
    second = world.run(groups=groups)
    assert second.backend._watched[GROUP]["since"] == last + 1               # PrmService.start: after its last line
    assert second.prm.is_party_group(GROUP)
    second.poll()
    assert second.lines() == [(HER, "我平时主要带美妆"), (BOSS, "她粉丝挺多的")]
    assert second.own == [] and world.window.typed == []
    second.poll()
    assert len(second.lines()) == 2
    assert second.scan() == 0 and len(world.prm_store.parties()) == 1
    hers = [line for job in world.jobs() if job["text"] for line in text_lines(job)]
    assert hers == ["在吗", "我平时主要带美妆"]                                  # each once (one job: not taken yet)
    assert [j for j in world.jobs() if json.loads(j["meta"]).get("opening")][0]["status"] == "cancelled"
    lines = second.prm.boss_groups.job_group(GROUP)["lines"]
    assert [(x["who"], x["text"]) for x in lines] == [("老板", INTRO), ("老板", "她粉丝挺多的")]
    assert world.prm_store.deal_of(party["id"])["mode"] == "copilot"


@GROUPS
def test_after_a_restart_our_reply_read_back_before_is_not_handed_on_again(world, groups):
    """Review (1): our own line in the group moves its last line handled on (``on_own_message``), and the restart
    watches from the second after it: the reply read back before the stop is not handed on again, and there is no
    false takeover."""
    first = world.run(groups=groups)
    party = taken_over(world, first)
    world.clock.now += 5
    world.db.line(GROUP, HER, "在吗", world.clock.now)
    first.poll()
    reply = "在的～有什么可以帮你"
    _, packet = mac_writes(first, reply)
    assert approve(packet["id"])["ok"] is True
    first.tick()
    first.poll()
    assert world.prm_store.packet(packet["id"])["outcome"] == "sent_confirmed"
    first.stop()
    world.clock.now += 120                                                    # rebuilt and started again
    second = world.run(groups=groups)
    second.poll()
    second.poll()
    deal = world.prm_store.deal_of(party["id"])
    assert deal["mode"] == "copilot" and world.events(deal["id"], "operator_message") == []   # no false takeover
    assert second.own == []                                                   # handed on (and read back) before
