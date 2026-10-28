"""WeChat's own lines in a registered creator's chat (win review F1), and what she shares (F2).

A recall, 拍一拍, a red packet or transfer notice is a line of type 10000 whose sender is empty, so the Windows
backend hands it on as the creator's message.  It is nobody's words: never a draft («[消息类型:10000]»), and the
drafts waiting are not out of date for it.  Her recall puts the drafts not sent yet before a person (they may
answer what she took back) and the deal says so; our own recall (「你撤回了一条消息」) is only recorded; WeChat's
lines about becoming friends keep their own handling (src/wechat/new_friends.py).  The integrated tests run the
real WCDB backend, router, relay and PRM on a fake WeChat database (test_prm_catchup.World); the Mac's side (the
edge's ``chat_notice``) is in tests/test_prm_over_edge.py.  A link, a mini program, a transfer, a location or a
card reaches the draft job as the edge's line (src/edge/text.py), not as 「[文件]」 or 「[消息类型:48]」.
"""

import pytest

from test_prm import CREATOR, Bot, Clock, draft
from test_prm_catchup import World

from src.prm import gates
from src.prm.service import event_out, system_line

RECALL_XML = ('<sysmsg type="revokemsg"><revokemsg><session>wxid_creator01</session><msgid>1</msgid>'
              '<newmsgid>99</newmsgid><replacemsg><![CDATA["小美" 撤回了一条消息]]></replacemsg></revokemsg></sysmsg>')


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # data/group_names.json and friends go here
    w = World(tmp_path)
    yield w
    w.stop()


@pytest.fixture
def bot(tmp_path):
    b = Bot(Clock(), tmp_path)
    b.prm.register(CREATOR, "小美", "测试达人")
    yield b
    b.prm.stop()
    b.relay.stop()


def _waiting_draft(world, gate=None):
    """The creator sent her address, the Mac drafted the reply: it waits (or, with ``gate`` auto, it is
    approved and about to go)."""
    world.register()
    run = world.run()
    if gate:
        world.prm_store.set_gate(gate, gates.AUTO, world.clock.now)
    world.wechat.say(CREATOR, CREATOR, "地址：上海市徐汇区某路1号 13800138000", world.clock.now)
    run.poll()
    world.mac_drafts(run, draft(reply_draft="好的，收到地址了", message_kind="other", slots={}))
    [packet] = world.prm_store.packets(("pending", "approved"))
    return run, packet


# ── the real backend, router and PRM ──────────────────────────────────────

@pytest.mark.parametrize("line", ['"小美" 拍了拍我', "收到红包，请在手机上查看", "你撤回了一条消息"],
                         ids=["pat", "red-packet-notice", "our-recall"])
def test_wechats_own_lines_are_no_draft_and_outdate_nothing(world, line):
    run, packet = _waiting_draft(world)
    world.clock.now += 30
    world.wechat.say(CREATOR, "", line, world.clock.now, local_type=10000)
    run.poll()
    assert len(world.jobs()) == 1                                    # no 「[消息类型:10000]」 job
    waiting = world.prm_store.packet(packet["id"])
    assert waiting["status"] == "pending" and not waiting["needs_attention"]
    deal = world.prm_store.deal(1)
    assert deal["mode"] == "copilot" and deal["attention"] == ""
    assert [e["detail"]["by"] for e in world.events("recall")] == (["operator"] if "撤回" in line else [])
    assert world.events("operator_message") == [] and world.events("creator_message") == [] and world.sent == []


def test_her_recall_puts_the_waiting_draft_before_a_person(world):
    """She took a message back: the draft may answer what she recalled.  It stays (a recall is no new
    message), is marked for the person, and the deal says why."""
    run, packet = _waiting_draft(world)
    world.clock.now += 30
    world.wechat.say(CREATOR, "", RECALL_XML, world.clock.now, local_type=10000)
    run.poll()
    assert len(world.jobs()) == 1
    held = world.prm_store.packet(packet["id"])
    assert held["status"] == "pending" and held["needs_attention"] and held["reason"].startswith("达人撤回了一条消息")
    assert world.prm_store.deal(1)["attention"].startswith("达人撤回了一条消息")
    [recall] = world.events("recall")
    assert (recall["detail"]["by"], recall["actor"]) == ("creator", "creator")
    assert event_out(recall)["name"] == "达人撤回了一条消息" and world.sent == []


def test_her_recall_holds_back_a_draft_its_gate_approved(world):
    """A draft its kind's gate approved, not sent yet, goes back to a person."""
    run, packet = _waiting_draft(world, gate="other")
    assert (packet["status"], packet["route"]) == ("approved", "auto")
    world.wechat.say(CREATOR, "", RECALL_XML, world.clock.now, local_type=10000)
    run.poll()
    held = world.prm_store.packet(packet["id"])
    assert (held["status"], held["route"], held["decided_at"], held["needs_attention"]) == ("pending", "review", None, True)
    run.prm.send_approved()
    assert world.sent == []


# ── PRM on its own ────────────────────────────────────────────────────────

def recall_line(b, text='"小美" 撤回了一条消息', **extra):
    """WeChat's line as the Windows backend hands it on (type 10000, its words in ``special``)."""
    return b.msg("[消息类型:10000]", msg_type=10000, special={"kind": "system", "title": text}, **extra)


def test_the_words_of_a_system_line():
    assert system_line({"msg_type": 10000, "content": "[消息类型:10000]",
                        "special": {"kind": "system", "title": "对方撤回了一条消息"}}) == "对方撤回了一条消息"
    assert system_line({"msg_type": 10002, "content": "[消息类型:10002]"}) == "[系统消息]"
    assert system_line({"msg_type": 1, "content": "撤回了一条消息"}) is None   # her own words about it


def test_a_draft_still_being_written_when_she_recalled_waits_for_a_person(bot):
    """The job was running when she took a message back: its draft may answer it, so it waits (even for
    a kind that goes out on its own) and says why; a job made after the recall is drafted as usual."""
    bot.prm.store.set_gate("other", "auto", bot.clock.now)
    bot.prm.handle(bot.msg("寄到张江路100号"))
    job = bot.worker_takes()
    assert bot.prm.handle(recall_line(bot)) is True
    assert len(bot.relay.store.list_jobs()) == 1                            # the recall is no job
    bot.finish(job["id"], draft(message_kind="other", reply_draft="好的", slots={}))
    packet = bot.prm.store.packet_for_job(job["id"])
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert packet["needs_attention"] and packet["reason"].startswith("达人撤回了一条消息")
    bot.clock.now += 60
    bot.prm.handle(bot.msg("改成寄到浦东"))
    later = bot.worker_takes()
    bot.finish(later["id"], draft(message_kind="other", reply_draft="好的，改成浦东", slots={}))
    assert bot.prm.store.packet_for_job(later["id"])["status"] == "approved"


def test_an_own_system_line_is_never_an_operators_reply(bot):
    """「你撤回了一条消息」 handed on as one of this account's own lines: recorded, no takeover, the
    waiting draft stays."""
    bot.prm.handle(bot.msg("你好"))
    bot.finish(bot.worker_takes()["id"], draft())
    bot.prm.on_own_message({"chat_id": CREATOR, "sender_id": "wxid_me", "content": "[消息类型:10000]",
                            "msg_type": 10000, "special": {"kind": "system", "title": "你撤回了一条消息"},
                            "is_group": False, "timestamp": bot.clock.now})
    assert bot.prm.store.deal(1)["mode"] == "copilot" and bot.prm.store.packet(1)["status"] == "pending"
    [recall] = bot.prm.store.events(1)
    assert (recall["kind"], recall["detail"]["by"]) == ("recall", "operator")
    assert event_out(recall)["name"] == "运营撤回了一条消息"


def test_a_new_friends_lines_keep_their_own_handling(bot):
    """WeChat's friend lines (is_friend_notice): taken, nothing recorded, nothing drafted, as before."""
    for title in ("你已添加了小美，现在可以开始聊天了。", "你已添加了小美，以上是打招呼的消息。"):
        assert bot.prm.handle(recall_line(bot, title)) is True
    assert bot.prm.handle(bot.msg("我通过了你的朋友验证请求，现在我们可以开始聊天了")) is True
    assert bot.relay.store.list_jobs() == [] and bot.prm.store.events(1) == []


def test_a_new_friends_catch_up_leaves_wechats_lines_out(bot):
    taken = bot.prm.take_new_friend(CREATOR, [bot.msg("你好，我是小美"), recall_line(bot, '"小美" 拍了拍我')])
    [job] = bot.relay.store.list_jobs()
    assert taken == 1 and "拍了拍" not in job["text"]


# ── links, transfers, locations, cards (win review F2) ───────────────────

LINK_XML = ('<msg><appmsg appid="" sdkver="0"><title>我的小红书主页</title><des>点开看看</des><type>5</type>'
            '<url>https://www.xiaohongshu.com/user/profile/abc</url></appmsg></msg>')
TRANSFER_XML = '<msg><appmsg><title>微信转账</title><type>2000</type></appmsg></msg>'
RED_PACKET_XML = '<msg><appmsg><title>恭喜发财，大吉大利</title><type>2001</type></appmsg></msg>'
LOCATION_XML = '<msg><location x="31.2" y="121.4" label="上海市徐汇区某路1号" poiname="某某大厦"/></msg>'
CARD_XML = '<msg username="wxid_other" nickname="另一个达人"/>'


def test_what_she_shares_reaches_the_draft_job_by_its_title(world):
    """Read by the real WCDB backend: the job says 「[链接] 我的小红书主页」, 「[转账]」, 「[位置] 某某大厦」…, as
    the edge's events do (src/edge/text.py), not 「[文件]」 or 「[消息类型:48]」.  (A link's address follows
    once the backend takes ``url`` out of the XML, wcdb_backend._special_info: not changed here.)"""
    world.register()
    run = world.run()
    t = world.clock.now
    for n, (content, local_type) in enumerate(((LINK_XML, 49), (TRANSFER_XML, 49), (RED_PACKET_XML, 49),
                                               (LOCATION_XML, 48), (CARD_XML, 42))):
        world.wechat.say(CREATOR, CREATOR, content, t + n, local_type=local_type)
    run.poll()
    [job] = world.jobs()
    assert [line.split("] ", 1)[1] for line in job["text"].split("\n")] == [
        "[链接] 我的小红书主页", "[转账]", "[红包]", "[位置] 某某大厦", "[名片] 另一个达人"]
