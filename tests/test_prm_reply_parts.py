"""PRM_REPLY_PARTS (2026-10-30, 「回复一下子发一大段很怪」): a reply's paragraphs (a blank line between two) go out as
WeChat messages of their own, a typing pause apart; a part that fails ends the reply and says how many went out; each
part read back is the bot's own line (no takeover), after a restart too and for a reply cut off by a stop; the Mac's
draft jobs say how many messages a reply may be.  Off (1), and on the Mac's edge side, a reply is one message as before."""

import itertools

import pytest

from test_prm import CREATOR, Bot, Clock, config, draft

from src.prm.service import PART_LATE_SEC, PrmService, reply_parts
from src.send.bubbles import typing_delay

ME = "wxid_me"
REPLY = "好呀～\n\n早上发你的这三款：北欧慵懒贝雷帽 399 元\n无檐圆顶小礼帽 269 元\n\n你平时主要在抖音还是小红书呀？"
PARTS = ["好呀～", "早上发你的这三款：北欧慵懒贝雷帽 399 元\n无檐圆顶小礼帽 269 元", "你平时主要在抖音还是小红书呀？"]
_ROWS = itertools.count(1)


@pytest.fixture
def bot(tmp_path):
    b = Bot(Clock(), tmp_path, prm_reply_parts=3)
    b.prm.register(CREATOR, "小美", "测试达人")
    b.pauses = []
    b.prm._pause = lambda sec: b.pauses.append(sec) or False       # (no real waiting; False: not stopping)
    yield b
    b.prm.stop()
    b.relay.stop()


def own(b, text, ts=None):
    """A line this WeChat account sent in her chat, as the backend hands it on (a row of its own)."""
    return {"message_id": f"own{next(_ROWS)}", "chat_id": CREATOR, "sender_id": ME, "sender_name": "我",
            "content": text, "msg_type": 1, "is_group": False, "timestamp": b.clock.now if ts is None else ts}


def drafted(b, reply=REPLY):
    """She asks, the Mac drafts ``reply``: the job and the packet."""
    b.prm.handle(b.msg("均价多少呀，你家"))
    job = b.worker_takes()
    b.finish(job["id"], draft(reply_draft=reply, slots={}, message_kind="faq", stage_proposal="", reason="问价"))
    return job, b.prm.store.packet_for_job(job["id"])


def sent(b, reply=REPLY):
    _, packet = drafted(b, reply)
    b.prm.decide(packet["id"], "send")
    b.prm.send_approved()
    return b.prm.store.packet(packet["id"])


def kinds(b):
    return [e["kind"] for e in b.prm.store.events(1, limit=50)]


def test_a_reply_is_split_at_its_blank_lines():
    assert reply_parts(REPLY, 3) == PARTS
    assert reply_parts("在的～\n 　\n稍等哈", 3) == ["在的～", "稍等哈"]              # white space on the blank line
    assert reply_parts("在的～\r\n\r\n稍等哈", 3) == ["在的～", "稍等哈"]
    assert reply_parts("\n\n在的～\n\n\n\n稍等哈\n\n", 3) == ["在的～", "稍等哈"]
    assert reply_parts("一\n\n二\n\n三\n\n四", 3) == ["一", "二", "三\n四"]                  # past the most: into the last
    assert reply_parts("一行\n又一行", 3) == ["一行\n又一行"]                               # a line break is no new message
    assert reply_parts(REPLY, 1) == [REPLY]                                                # 1: one message, as it is
    assert reply_parts(" \n\n ", 3) == [] and reply_parts("", 1) == []


def test_a_reply_in_paragraphs_goes_out_as_messages_a_typing_pause_apart(bot):
    packet = sent(bot)
    assert bot.texts == [(CREATOR, part) for part in PARTS]
    assert bot.pauses == [typing_delay(PARTS[1]), typing_delay(PARTS[2])]                 # none before the first
    assert (packet["status"], packet["outcome"]) == ("sent", "sent_unconfirmed")
    for part in PARTS:                                                                     # WeChat shows each one
        bot.prm.on_own_message(own(bot, part))
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    assert bot.prm.store.deal(1)["mode"] == "copilot" and "operator_message" not in kinds(bot)
    bot.clock.now += 60
    bot.prm.on_own_message(own(bot, PARTS[0]))                                             # the same words typed later
    assert bot.prm.store.deal(1)["mode"] == "human"


def test_a_part_that_fails_ends_the_reply_and_says_how_many_went_out(bot):
    calls = []

    def send(chat, text):
        calls.append(text)
        return len(calls) < 2                                                              # the second one fails

    bot.prm._send = send
    packet = sent(bot)
    assert calls == PARTS[:2]                                                              # never the third, nor again
    assert (packet["status"], packet["outcome"]) == ("failed", "failed")
    assert packet["error"] == "分 3 条发，前 1 条发出去了，第 2 条没发出去（文字没发出去）；剩下的请在微信里手动补发"
    bot.prm.on_own_message(own(bot, PARTS[0]))                                             # the one that went out
    assert bot.prm.store.deal(1)["mode"] == "copilot" and "operator_message" not in kinds(bot)


def test_the_bot_stopping_between_two_parts_sends_no_more_and_the_restart_knows_the_part_that_went(bot):
    """The bot stops (its WeChat backend first) in the pause after the first part: the rest is not sent and the draft
    stays sending; the next start makes it 「不确定发出去没有」, and the first part read back after it is the bot's
    (no takeover) while the draft stays red: the others never showed."""
    bot.prm._pause = lambda sec: True                                                      # (stop() meanwhile)
    packet = sent(bot)
    assert bot.texts == [(CREATOR, PARTS[0])]
    assert packet["status"] == "sending"
    written = bot.clock.now
    bot.clock.now += 60
    restarted = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=bot.clock, send=bot._send)
    restarted.start()
    restarted.stop()
    restarted.on_own_message(own(bot, PARTS[0], ts=written))
    p = bot.prm.store.packet(packet["id"])
    assert (p["status"], p["outcome"]) == ("failed", "indeterminate") and "不确定发出去没有" in p["error"]
    assert bot.prm.store.deal(1)["mode"] == "copilot" and "operator_message" not in kinds(bot)


@pytest.mark.parametrize("prm", [{}, {"prm_reply_parts": 1}, {"prm_reply_parts": 3, "wechat_backend": "edge"}],
                         ids=["not set", "1", "the Mac's edge side"])
def test_otherwise_a_reply_is_one_message_blank_lines_and_all(tmp_path, prm):
    b = Bot(Clock(), tmp_path, **prm)
    b.prm.register(CREATOR, "小美", "测试达人")
    b.prm._pause = lambda sec: pytest.fail("no pause for one message")
    try:
        job, packet = drafted(b)
        assert "reply_parts" not in job["meta"]                                          # the Mac writes one message
        b.prm.decide(packet["id"], "send")
        b.prm.send_approved()
        assert b.texts == [(CREATOR, REPLY)]
        assert b.prm.snapshot()["reply_parts"] == 1
    finally:
        b.prm.stop()
        b.relay.stop()


def test_the_macs_reply_jobs_say_how_many_messages_a_reply_may_be(bot):
    """Her message's job, a 开场白's (a creator who has not written) and a 专属佣金 step's: WeChat replies, all of them."""
    job, packet = drafted(bot)
    assert job["meta"]["reply_parts"] == 3
    assert bot.prm.snapshot()["reply_parts"] == 3
    bot.prm.register("wxid_creator02", "小兰", "测试达人")
    asked = bot.prm.ask_opening(2)
    assert asked["ok"]
    opening = bot.worker_takes()
    assert opening["id"] == asked["job"] and opening["meta"]["reply_parts"] == 3
    deal = bot.prm.store.deal(1)
    step = bot.prm._dispatch_step(deal, bot.prm.store.party(deal["party_id"]), "样品签收了，开专属佣金", packet["id"],
                                  True, bot.clock.now)
    taken = bot.worker_takes()
    assert taken["id"] == step and taken["meta"]["reply_parts"] == 3


def test_parts_read_back_after_a_restart_are_still_the_bots(bot):
    """The first part was read back (it confirmed the packet), then the bot restarted: the other parts, read after the
    restart, are still the bot's, each once; the same words written after the send returned are a person's."""
    packet = sent(bot)
    sent_at = packet["sent_at"]
    bot.prm.on_own_message(own(bot, PARTS[0], ts=sent_at))
    assert bot.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
    bot.clock.now += 3600
    restarted = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=bot.clock, send=bot._send)
    restarted.on_own_message(own(bot, PARTS[1], ts=sent_at))
    assert bot.prm.store.deal(1)["mode"] == "copilot" and "operator_message" not in kinds(bot)
    restarted.on_own_message(own(bot, PARTS[2], ts=sent_at + PART_LATE_SEC + 1))           # written later: by hand
    assert "operator_message" in kinds(bot)


def test_each_part_read_back_after_a_restart_counts_once(bot):
    packet = sent(bot)
    bot.clock.now += 3600
    restarted = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=bot.clock, send=bot._send)
    for part in PARTS:
        restarted.on_own_message(own(bot, part, ts=packet["sent_at"]))
    assert "operator_message" not in kinds(bot)
    restarted.on_own_message(own(bot, PARTS[2], ts=packet["sent_at"]))                     # one more of them
    assert "operator_message" in kinds(bot)


def test_a_reply_in_parts_cut_off_by_a_stop_is_sent_once_every_part_is_seen(bot):
    _, packet = drafted(bot)
    bot.prm.decide(packet["id"], "send")
    bot.prm.store.move(packet["id"], ("approved",), "sending")                             # stopped while sending it
    written = bot.clock.now
    bot.clock.now += 60
    restarted = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=bot.clock, send=bot._send)
    restarted.start()
    restarted.stop()
    for part in PARTS:
        p = bot.prm.store.packet(packet["id"])
        assert (p["status"], p["outcome"]) == ("failed", "indeterminate")                  # not every part seen yet
        restarted.on_own_message(own(bot, part, ts=written))
    p = bot.prm.store.packet(packet["id"])
    assert (p["status"], p["outcome"], p["error"]) == ("sent", "sent_confirmed", "")
    assert bot.prm.store.deal(1)["mode"] == "copilot" and "operator_message" not in kinds(bot)


def test_a_reply_sent_in_one_message_before_the_switch_is_still_the_bots(tmp_path):
    """Sent as one message (PRM_REPLY_PARTS not set yet), read back after a restart with it on: the bot's."""
    b = Bot(Clock(), tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    try:
        packet = sent(b)
        assert b.texts == [(CREATOR, REPLY)]
        b.clock.now += 3600
        restarted = PrmService(config(prm_reply_parts=3), b.relay, store=b.prm.store, clock=b.clock, send=b._send)
        restarted.on_own_message(own(b, REPLY, ts=packet["sent_at"]))
        assert b.prm.store.packet(packet["id"])["outcome"] == "sent_confirmed"
        assert b.prm.store.deal(1)["mode"] == "copilot" and "operator_message" not in kinds(b)
    finally:
        b.prm.stop()
        b.relay.stop()
