"""One WeChat row, one message id; the bot's own line read back twice is not an operator's.

Seen on Windows (WeChat 4.1.15): WeChat writes a line this account sends with no server id
and fills one in about a second later.  The WCDB backend made the message id from the server
id, so the same row got a second id and was handed on twice: PRM_COPILOT matched the bot's
reply on the first read, took the second for an operator typing and gave the deal to a
person; the WeChat edge reported ``send_seen`` and then ``operator_message`` for one row.

Now the id is made from the chat, the row's ``local_id`` and its time
(src/wechat/wcdb_backend.py ``_message_ids``), and PRM remembers the own lines it matched for
READ_BACK_SEC (src/prm/service.py ``_match_own``).  The first start after the update must not
hand on again what the message store holds under the old ids (``_legacy_ids``).

The integrated tests run the real backend over a fake WeChat database, with the router, the
relay and PRM, as tests/test_prm_catchup.py does.
"""

import hashlib
import time
from unittest.mock import Mock

import pytest

from test_prm import CREATOR, Bot, Clock, draft
from test_prm_catchup import ADMIN, BOT, GROUP, ME, FakeWeChat, World, text_lines

from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.prm.service import READ_BACK_SEC
from src.wechat import wcdb_backend
from src.wechat.burst import MessageBurstBuffer
from src.wechat.wcdb_backend import WcdbBackend

SERVER_ID = "7520000000000000015"
ABSENT = object()


def old_message_ids(msg, talker, sender, content, ts):
    """How the backend made a message id up to 2026-10-27: from the server id once there is one."""
    raw = str(msg.get("server_id", "")) or str(msg.get("local_id", "")) or f"{sender}|{content}|{ts}"
    return hashlib.md5(f"{raw}|{ts}".encode()).hexdigest(), []


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # data/group_names.json and friends go here
    w = World(tmp_path)
    yield w
    w.stop()


def reader(private=(CREATOR,)):
    """A backend over a fake WeChat that reads the creator's private chat."""
    backend = WcdbBackend(bot_display_name=BOT, groups=["*"], config=BotConfig(private_chat_enabled=True))
    backend._client = FakeWeChat()
    backend._chat_is_group[CREATOR] = False
    backend._running = True
    backend.set_private_filter(lambda username: username in private)
    return backend


def standardize(backend, row, talker=CREATOR, **kwargs):
    return backend._standardize(row, "小美", talker, is_group=False, **kwargs)


# ── the id of a row ────────────────────────────────────────────────────────

def test_a_rows_id_stays_the_same_when_wechat_fills_in_its_server_id():
    backend = reader()
    row = {"sender_username": ME, "message_content": "好的，收到～", "local_type": 1,
           "create_time": 1_761_000_000, "local_id": 15}
    ids = {standardize(backend, row if server_id is ABSENT else {**row, "server_id": server_id})["message_id"]
           for server_id in (ABSENT, "", 0, "0", None, SERVER_ID)}
    assert len(ids) == 1
    [msg_id] = ids
    assert msg_id == hashlib.md5(f"{CREATOR}|15|1761000000".encode()).hexdigest()
    # local ids are counted per chat: the same number at the same second elsewhere is another row
    assert standardize(backend, row, talker="wxid_creator02")["message_id"] != msg_id
    assert standardize(backend, {**row, "local_id": 16})["message_id"] != msg_id
    # what the versions before made of it, read before and after the server id came in
    filled = standardize(backend, {**row, "server_id": SERVER_ID})
    assert filled["_legacy_ids"] == [old_message_ids({**row, "server_id": SERVER_ID}, CREATOR, ME, "", 1761000000)[0],
                                     old_message_ids({**row, "server_id": ""}, CREATOR, ME, "", 1761000000)[0]]


def test_a_row_without_a_local_id_falls_back_to_sender_text_and_time_never_the_server_id():
    backend = reader()
    row = {"sender_username": ME, "message_content": "好的，收到～", "local_type": 1, "create_time": 1_761_000_000}
    ids = {standardize(backend, {**row, "server_id": server_id})["message_id"] for server_id in ("", 0, SERVER_ID)}
    assert ids == {hashlib.md5(f"{ME}|好的，收到～|1761000000|1761000000".encode()).hexdigest()}


def test_priming_and_polling_give_a_row_the_same_id_even_a_voice_message():
    """Priming reads without recognising voice; the poll recognises it: the id must not care."""
    backend = reader()
    backend._try_voice = lambda msg, talker="": "我到家了"
    for row in ({"sender_username": CREATOR, "message_content": "", "local_type": 34, "create_time": 1_761_000_000,
                 "local_id": 9},
                {"sender_username": CREATOR, "message_content": "", "local_type": 34, "create_time": 1_761_000_000}):
        primed = standardize(backend, row, resolve_voice=False)
        polled = standardize(backend, {**row, "server_id": SERVER_ID})
        assert (primed["content"], polled["content"]) == ("[语音]", "[语音] 我到家了")
        assert primed["message_id"] == polled["message_id"]


# ── read before and after the server id: handed on once ────────────────────

def test_a_row_read_before_and_after_its_server_id_is_handed_on_once():
    backend = reader()
    heard, handed = [], []
    backend.set_own_message_hook(heard.append)
    backend._dispatch_message = lambda msg, callback: handed.append(msg)
    now = int(time.time())
    backend._client.say(CREATOR, CREATOR, "在吗", now)
    backend._client.say(CREATOR, ME, "在的，您说", now)
    rows = backend._client.chats[CREATOR]

    def poll():
        backend._poll_group("小美", CREATOR, Mock())
        handed.extend(backend._burst.flush_all())

    for row in rows:
        row["server_id"] = 0                       # written, not acknowledged by the server yet
    poll()
    for n, row in enumerate(rows):
        row["server_id"] = f"{SERVER_ID}{n}"       # a second later
    poll()
    poll()
    assert [m["content"] for m in heard] == ["在的，您说"]          # the own-line hook (PRM, the edge): once
    assert [m["content"] for m in handed] == ["在吗"]               # the route: once


def test_a_row_primed_before_its_server_id_is_not_new_after_it():
    backend = reader()
    heard, handed = [], []
    backend.set_own_message_hook(heard.append)
    backend._dispatch_message = lambda msg, callback: handed.append(msg)
    backend._client.say(CREATOR, ME, "刚发的", int(time.time()))
    row = backend._client.chats[CREATOR][0]
    row["server_id"] = 0
    backend._prime_chat("小美", CREATOR, backend._client.get_messages(CREATOR), catch_up=False)
    row["server_id"] = SERVER_ID
    backend._poll_group("小美", CREATOR, Mock())
    assert heard == [] and handed == [] and backend._burst.flush_all() == []


def test_the_bots_reply_read_before_and_after_its_server_id_confirms_it_and_takes_nothing_over(world):
    """PRM_COPILOT on Windows, end to end: the second read of the bot's line used to take the deal over."""
    world.register()
    run = world.run()
    heard = []
    run.backend.set_own_message_hook(lambda msg: heard.append(msg["message_id"]) or run.prm.on_own_message(msg))
    world.wechat.say(CREATOR, CREATOR, "收件人小美 13812345678", world.clock.now)
    run.poll()
    world.mac_drafts(run)
    [packet] = world.prm_store.packets(("pending",))
    run.prm.decide(packet["id"], "send")
    run.prm.send_approved()
    chat, text = world.sent[-1]
    assert (chat, text) == (CREATOR, "确认一下：小美，手机13812345678。")
    world.wechat.say(CREATOR, ME, text, world.clock.now)       # WeChat shows it at once ...
    row = world.wechat.chats[CREATOR][0]
    row["server_id"] = 0
    run.poll()
    world.clock.now += 1
    row["server_id"] = SERVER_ID                               # ... and gets its server id a second later
    run.poll()
    assert len(heard) == 1
    assert world.prm_store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert world.prm_store.deal(1)["mode"] == "copilot"
    assert world.events("operator_message") == [] and world.events("takeover") == []


# ── the first start after the update ──────────────────────────────────────

def counts(world):
    return (len(world.jobs()), [world.stored(chat) for chat in (CREATOR, ADMIN, GROUP)],
            [e["kind"] for e in world.events()], list(world.sent),
            [(p["id"], p["status"]) for p in world.prm_store.packets()])


def test_the_first_start_after_the_update_hands_nothing_on_again(world, monkeypatch):
    stable = wcdb_backend._message_ids
    monkeypatch.setattr(wcdb_backend, "_message_ids", old_message_ids)          # the version before
    world.register()
    t0 = world.clock.now
    old = world.run()
    world.wechat.say(CREATOR, CREATOR, "你好，想合作，电话 13800001111", t0)
    world.wechat.say(ADMIN, ADMIN, "cc 跑一下测试", t0)
    world.wechat.say(GROUP, "wxid_member", "大家好", t0)
    old.poll()
    world.mac_drafts(old)                                      # a draft waits in the console
    world.wechat.say(CREATOR, CREATOR, "在吗", world.clock.now)
    world.wechat.say(CREATOR, CREATOR, "看到回我一下", world.clock.now)   # one burst with the line above
    world.wechat.say(CREATOR, ME, "我是运营，我来回", world.clock.now)    # typed on the phone: a takeover
    old.poll()
    old.stop()
    before = counts(world)
    assert len(world.jobs()) == 2 and world.events("takeover")
    stored = {r[0] for r in world.messages.conn.execute("SELECT message_id FROM messages")}

    monkeypatch.setattr(wcdb_backend, "_message_ids", stable)                   # the update
    world.clock.now += 60                                      # started again a minute later
    new = world.run()
    new.poll()
    new.poll()
    assert counts(world) == before                             # no job, draft, event, reply or row again
    # the same rows have other ids now: the store holds only the old ones
    first_row = world.wechat.chats[CREATOR][-1]
    fresh = new.backend._standardize(first_row, "小美", CREATOR, is_group=False)
    assert fresh["message_id"] not in stored and fresh["_legacy_ids"][0] in stored

    world.wechat.say(CREATOR, CREATOR, "地址我发你", world.clock.now)     # a new message is handled as ever
    new.poll()
    assert world.stored(CREATOR) == before[1][0] + 1
    assert [e["kind"] for e in world.events()][-1] == "creator_message"   # (a person has the deal)


@pytest.mark.parametrize("lines", [["cc 跑一下测试"], ["cc 整理一下", "今天的订单"]], ids=["a line", "a burst"])
def test_a_chat_first_read_after_the_update_does_not_run_an_old_task_again(world, monkeypatch, lines):
    """A chat the first start could not prime (its read failed then) is read for the first time
    later: every line WeChat shows is handed on, as ever, and the message store has to know the
    ones handled before the update (a burst merged the same way too)."""
    stable = wcdb_backend._message_ids
    monkeypatch.setattr(wcdb_backend, "_message_ids", old_message_ids)
    t0 = world.clock.now
    old = world.run()
    for n, line in enumerate(lines):
        world.wechat.say(ADMIN, ADMIN, line, t0 + n)
    old.poll()
    old.stop()
    assert len(world.jobs()) == 1 and len(world.sent) == 1 and world.stored(ADMIN) == 1

    monkeypatch.setattr(wcdb_backend, "_message_ids", stable)
    world.clock.now = t0 + 60                                  # within the relay's five minutes
    reads = world.wechat.get_messages

    def unreadable_admin_chat(talker, limit=50):
        if talker == ADMIN:
            raise RuntimeError("database is locked")
        return reads(talker, limit)

    monkeypatch.setattr(world.wechat, "get_messages", unreadable_admin_chat)
    new = world.run()                                          # primes every chat but the admin's
    monkeypatch.setattr(world.wechat, "get_messages", reads)
    assert ADMIN in new.backend._talker_ids.values() and ADMIN not in new.backend._known
    new.poll()
    assert len(world.jobs()) == 1 and len(world.sent) == 1 and world.stored(ADMIN) == 1
    world.wechat.say(ADMIN, ADMIN, "cc 再跑一次", world.clock.now)
    new.poll()
    assert len(world.jobs()) == 2 and world.stored(ADMIN) == 2


def test_a_creator_registered_after_the_update_gets_no_draft_of_what_was_handled_before(world, monkeypatch):
    """The catch-up's first read of a chat it never followed hands on the last five minutes,
    except what is stored already: under the old id too."""
    stable = wcdb_backend._message_ids
    monkeypatch.setattr(wcdb_backend, "_message_ids", old_message_ids)
    now = world.clock.now = float(int(time.time()))           # the backend's own clock decides here
    old = world.run(private_chats="*")                         # read as anyone's chat: stored, not answered
    world.wechat.say(CREATOR, CREATOR, "你好，我想合作", now - 120)
    old.poll()
    old.stop()
    assert world.stored(CREATOR) == 1 and world.jobs() == []

    monkeypatch.setattr(wcdb_backend, "_message_ids", stable)
    world.wechat.say(CREATOR, CREATOR, "在吗？", now - 30)    # came in while the bot was updated
    new = world.run()                                          # PRIVATE_CHATS lists the admin only
    new.prm.register(CREATOR, "小美")
    new.backend._refresh_private_chats_if_due()
    latest, handled = (new.backend._standardize(m, "小美", CREATOR, is_group=False)
                       for m in world.wechat.chats[CREATOR])
    assert new.backend._catchup_plan(CREATOR, [handled, latest], first_read=True) == {latest["message_id"]}
    new.poll()
    [job] = world.jobs()
    assert text_lines(job) == ["在吗？"] and world.stored(CREATOR) == 2


# ── the store and the burst know the older ids ─────────────────────────────

def test_the_store_takes_a_message_stored_under_an_older_id_for_a_duplicate():
    store = MessageStore(initialize_db(":memory:"))
    base = {"chat_id": CREATOR, "sender_id": CREATOR, "sender_name": "小美", "content": "你好", "msg_type": 1,
            "timestamp": 1}
    assert store.insert_message({**base, "message_id": "old"})
    assert not store.insert_message({**base, "message_id": "new", "_legacy_ids": ["old"]})
    # in another chat the same older id was another message
    assert store.insert_message({**base, "chat_id": "wxid_other", "message_id": "new2", "_legacy_ids": ["old"]})
    assert store.stored_ids(["old", "new", "new2"]) == {"old", "new2"}
    assert store.stored_ids(["old", "new2"], chat_id=CREATOR) == {"old"}


def test_a_burst_gets_the_id_an_older_version_made_of_it():
    buffer = MessageBurstBuffer()
    msgs = [{"message_id": f"n{i}", "chat_id": ADMIN, "sender_id": ADMIN, "content": text, "msg_type": 1,
             "is_group": False, "timestamp": i, "_legacy_ids": [f"o{i}", f"local{i}"]}
            for i, text in enumerate(["cc 整理一下", "今天的订单"])]
    merged = buffer._merge(msgs)
    assert merged["message_id"] == buffer._burst_id(merged, ["n0", "n1"])
    assert merged["_legacy_ids"] == [buffer._burst_id(merged, ["o0", "o1"])]
    msgs[1]["_legacy_ids"] = []                                 # (a message from another backend)
    assert buffer._merge(msgs)["_legacy_ids"] == []


# ── PRM: our line read back under a second id ──────────────────────────────

@pytest.fixture
def bot(tmp_path):
    b = Bot(Clock(), tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def own(bot, text, message_id, chat=CREATOR):
    """What a backend hands on for a line this account wrote."""
    return {"message_id": message_id, "chat_id": chat, "sender_id": "wxid_bot", "sender_name": "我",
            "content": text, "msg_type": 1, "is_group": False, "timestamp": bot.clock.now}


def send_a_draft(bot, **d):
    bot.prm.handle(bot.msg("我是小美，手机 13812345678"))
    job = bot.worker_takes()
    bot.finish(job["id"], draft(**d))
    packet = bot.prm.store.packet_for_job(job["id"])
    bot.prm.decide(packet["id"], "send")
    bot.prm.send_approved()
    return packet, bot.texts[-1][1]


def kinds(bot, deal_id=1):
    return [e["kind"] for e in bot.prm.store.events(deal_id, limit=50)]


def test_the_bots_line_read_twice_under_two_ids_is_one_read_back(bot):
    """As the backend did before: one row handed on before and after its server id came in."""
    packet, text = send_a_draft(bot)
    bot.prm.on_own_message(own(bot, text, "row15-no-server-id"))      # matched: confirms its packet
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    bot.clock.now += 1
    assert bot.prm._match_own(CREATOR, own(bot, text, "row15-server-id"), bot.clock.now) == (True, None)
    bot.prm.on_own_message(own(bot, text, "row15-server-id"))         # the same row under a second id
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    assert "operator_message" not in kinds(bot) and "takeover" not in kinds(bot)


def test_the_same_words_typed_after_the_read_back_window_are_a_persons(bot):
    packet, text = send_a_draft(bot)
    bot.prm.on_own_message(own(bot, text, "row15-no-server-id"))      # read back: confirms the packet
    bot.clock.now += 1
    bot.prm.on_own_message(own(bot, text, "row15-server-id"))         # the same row again: nothing
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    bot.clock.now += READ_BACK_SEC
    bot.prm.on_own_message(own(bot, text, "row16"))                   # 121 s after the match: typed by hand
    deal = bot.prm.store.deal(1)
    assert deal["mode"] == "human" and deal["mode_until"] == bot.clock.now + 120 * 60
    assert kinds(bot).count("operator_message") == 1 and kinds(bot).count("takeover") == 1


def test_a_second_send_of_the_same_words_is_still_confirmed_and_other_chats_are_not_affected(bot):
    greeting = {"message_kind": "greeting", "reply_draft": "好的呀", "slots": {}}
    first, text = send_a_draft(bot, **greeting)
    bot.prm.on_own_message(own(bot, text, "a"))
    second, again = send_a_draft(bot, **greeting)                     # the same words, a new send
    assert again == text
    bot.prm.on_own_message(own(bot, text, "b"))
    assert [bot.prm.store.packet(p["id"])["outcome"] for p in (first, second)] == ["sent_confirmed"] * 2
    assert bot.prm.store.deal(1)["mode"] == "copilot"
    # another creator's chat never sent those words: there they are a person's
    bot.prm.register("wxid_creator02", "小丽")
    bot.prm.on_own_message(own(bot, text, "c", chat="wxid_creator02"))
    assert bot.prm.store.deal(2)["mode"] == "human"
