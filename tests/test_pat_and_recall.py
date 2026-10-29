"""拍一拍和撤回：微信自己写在达人私聊里的两种行，两种部署都不能当成达人的话。

- 拍一拍（2026-10-28 Windows 真机，微信 4.1.15）：WCDB 里 local_type = 266287972401 =
  (62 << 32) | 49，内容是 appmsg 的 XML（下面的 PAT_XML，名字换掉了）。高 32 位是 appmsg 的子类型，
  ``_message_local_type`` 只留低 32 位（49），子类型 62 由 ``_message_subtype`` 拿到；以前标准化以后
  只剩 49，src/edge/text.py 的 is_notice 只认 10000 / 10002，拍一拍就被当成达人的新消息：起草，
  还顶掉了正在等审批的草稿。现在拍一拍（谁拍谁都一样）和撤回、红包提示一样是系统行：副驾驶不起草、
  不顶掉草稿，只记日志；边缘报 ``chat_notice``。别的子类型的 49（引用、链接、文件、转账）照旧。

真的 WCDB 后端（底下是假的微信数据库 test_prm_catchup.FakeWeChat），副驾驶按 test_prm_catchup.World
接上路由、中转、PRM；边缘按 test_edge_catchup.Run 接上 EdgeService。
"""

import pytest

from test_edge_catchup import Run as EdgeRun, Windows
from test_prm import CREATOR, draft
from test_prm_catchup import ME, World, text_lines

from src.edge import protocol as P
from src.edge import text as T
from src.wechat.wcdb_backend import _message_local_type, _message_subtype, _special_info

PAT_TYPE = (62 << 32) | 49                     # 266287972401，真机上的值
QUOTE_TYPE = (57 << 32) | 49
LINK_TYPE = (5 << 32) | 49
TRANSFER_TYPE = (2000 << 32) | 49

# 真机上的原文（微信 4.1.15，2026-10-28），名字和 wxid 换掉了
PAT_XML = ('<msg><appmsg appid="" sdkver="0"><title>"小美" 拍了拍我</title><des></des><type>62</type>'
           '<patinfo><fromusername>wxid_creator01</fromusername><chatusername>wxid_creator01</chatusername>'
           '<pattedusername>wxid_me</pattedusername><template>"${wxid_creator01}" 拍了拍我</template></patinfo>'
           '</appmsg></msg>')
# 我们拍她：真机上还没看到原文，按同样的格式写（认拍一拍不看标题）
OWN_PAT_XML = ('<msg><appmsg appid="" sdkver="0"><title>我拍了拍 "小美"</title><des></des><type>62</type>'
               '<patinfo><fromusername>wxid_me</fromusername><chatusername>wxid_creator01</chatusername>'
               '<pattedusername>wxid_creator01</pattedusername><template>我拍了拍 "${wxid_creator01}"</template>'
               '</patinfo></appmsg></msg>')
QUOTE_XML = ('<msg><appmsg appid="" sdkver="0"><title>这个颜色还有吗</title><type>57</type>'
             '<refermsg><type>1</type><svrid>777</svrid><chatusr>wxid_me</chatusr><displayname>我</displayname>'
             '<content>这是新款的色卡</content></refermsg></appmsg></msg>')
LINK_XML = ('<msg><appmsg appid="" sdkver="0"><title>我的小红书主页</title><type>5</type>'
            '<url>https://www.xiaohongshu.com/user/profile/abc</url></appmsg></msg>')
TRANSFER_XML = '<msg><appmsg><title>微信转账</title><type>2000</type></appmsg></msg>'


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # data/group_names.json and friends go here
    w = World(tmp_path)
    yield w
    w.stop()


@pytest.fixture
def win(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Windows(tmp_path)


def waiting_draft(world):
    """她发了地址，Mac 起草了回复，草稿在等审批。"""
    world.register()
    run = world.run()
    world.wechat.say(CREATOR, CREATOR, "地址：上海市徐汇区某路1号", world.clock.now)
    run.poll()
    world.mac_drafts(run, draft(reply_draft="好的，收到地址了", message_kind="other", slots={}))
    [packet] = world.prm_store.packets(("pending",))
    return run, packet


# ── 高 32 位在哪里去掉的 ────────────────────────────────────────────────────

def test_the_high_32_bits_of_local_type_are_the_appmsg_kind():
    """WCDB 给的是整个 64 位的数；_message_local_type 只留 49，子类型（62 = 拍一拍）在 _message_subtype。
    标准化以后的消息带上 msg_subtype，XML 里的 <type> 也是 62，两样都认。"""
    raw = {"local_type": 266287972401}
    assert (_message_local_type(raw), _message_subtype(raw)) == (49, 62)
    assert _special_info(PAT_XML, 49) == {"kind": "appmsg", "type": "62", "title": '"小美" 拍了拍我'}
    pat = {"msg_type": 49, "msg_subtype": 62, "content": "[文件]", "special": _special_info(PAT_XML, 49)}
    assert T.is_notice(pat) and T.notice_text(pat) == '"小美" 拍了拍我' and T.message_text(pat) == '"小美" 拍了拍我'
    # 只有子类型、XML 解不开：照样是拍一拍；只有 XML 的 <type>62</type>（没有高位）也是
    assert T.is_notice({"msg_type": 49, "msg_subtype": 62, "content": "[文件]"})
    assert T.notice_text({"msg_type": 49, "msg_subtype": 62, "content": "[文件]"}) == "[拍一拍]"
    assert T.is_notice({"msg_type": 49, "special": {"kind": "appmsg", "type": "62", "title": "x 拍了拍我"}})
    # 别的子类型的 49 不是系统行
    for subtype, kind in ((57, "57"), (5, "5"), (6, "6"), (2000, "2000"), (0, "4")):
        assert not T.is_notice({"msg_type": 49, "msg_subtype": subtype, "special": {"kind": "appmsg", "type": kind}})


# ── 副驾驶（PRM_COPILOT）：真的 WCDB 后端、路由、PRM ─────────────────────────

@pytest.mark.parametrize("sender, xml", [(CREATOR, PAT_XML), (ME, OWN_PAT_XML)], ids=["she-pats", "we-pat"])
def test_a_pat_is_no_draft_and_outdates_nothing(world, sender, xml):
    run, packet = waiting_draft(world)
    world.clock.now += 30
    world.wechat.say(CREATOR, sender, xml, world.clock.now, local_type=PAT_TYPE)
    run.poll()
    assert len(world.jobs()) == 1, "拍一拍不起草"
    waiting = world.prm_store.packet(packet["id"])
    assert waiting["status"] == "pending" and not waiting["needs_attention"], "也不顶掉等审批的草稿"
    deal = world.prm_store.deal(1)
    assert deal["mode"] == "copilot" and deal["attention"] == "", "我们拍她也不是运营亲手回复"
    assert world.events("creator_message") == world.events("operator_message") == world.events("recall") == []
    assert world.sent == []


def test_other_kinds_of_appmsg_with_high_bits_are_still_her_words(world):
    """引用、链接、转账在微信 4 里也把子类型放在高 32 位：照样起草，写法不变。"""
    world.register()
    run = world.run()
    t = world.clock.now
    for n, (content, local_type) in enumerate(((QUOTE_XML, QUOTE_TYPE), (LINK_XML, LINK_TYPE),
                                               (TRANSFER_XML, TRANSFER_TYPE), (PAT_XML, PAT_TYPE))):
        world.wechat.say(CREATOR, CREATOR, content, t + n, local_type=local_type)
    run.poll()
    [job] = world.jobs()
    assert text_lines(job) == ["这个颜色还有吗", "[链接] 我的小红书主页 https://www.xiaohongshu.com/user/profile/abc",
                               "[转账]"]


# ── 边缘（PRM_EDGE）：真的 WCDB 后端 + EdgeService ──────────────────────────

def test_a_pat_in_a_listed_chat_is_a_chat_notice(win):
    run = EdgeRun(win)
    win.wechat.say(CREATOR, CREATOR, "你好", win.now)
    run.poll()
    win.wechat.say(CREATOR, CREATOR, PAT_XML, win.now + 1, local_type=PAT_TYPE)
    win.wechat.say(CREATOR, ME, OWN_PAT_XML, win.now + 2, local_type=PAT_TYPE)
    win.wechat.say(CREATOR, CREATOR, QUOTE_XML, win.now + 3, local_type=QUOTE_TYPE)
    run.poll()
    assert win.lines(P.CREATOR_MESSAGE) == ["你好", "这个颜色还有吗"]
    assert win.lines(P.CHAT_NOTICE) == ['"小美" 拍了拍我', '我拍了拍 "小美"']
    assert win.events(P.OPERATOR_MESSAGE) == [], "我们拍她不是运营亲手回复"
    run.stop()


# ── 撤回：微信 4 原地改写那一行 ─────────────────────────────────────────────
#
# 真机（2026-10-28）：达人撤回时微信不新写一行，而是把原来那一行原地改掉：同一个 local_id、同一个
# create_time，local_type 从 1 变成 10000，内容换成下面的 XML（文字在 <content> 里，不在 <replacemsg>）。
# 消息编号 md5(聊天|local_id|时间) 不变，机器人以前当成读过的，完全看不到撤回。

REVOKE_XML = ('<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg><content>"小美" 撤回了一条消息</content>'
              '<revoketime>0</revoketime></revokemsg></sysmsg>')
OWN_REVOKE_XML = ('<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg><content>你撤回了一条消息</content>'
                  '<revoketime>0</revoketime></revokemsg></sysmsg>')


def take_back(wechat, chat, text, xml=REVOKE_XML):
    """微信原地改写：同一个 local_id、同一个时间，类型改成 10000，内容换成撤回的 XML。"""
    row = next(r for r in wechat.chats[chat] if r["message_content"] == text)
    row.update(local_type=10000, message_content=xml)
    return row


def test_the_words_of_a_revokemsg_are_in_its_content():
    assert _special_info(REVOKE_XML, 10000) == {"kind": "system", "title": '"小美" 撤回了一条消息'}, "不带 revoketime 的 0"
    assert _special_info(OWN_REVOKE_XML, 10000)["title"] == "你撤回了一条消息"
    old = ('<sysmsg type="revokemsg"><revokemsg><session>wxid_creator01</session><replacemsg><![CDATA["小美" '
           '撤回了一条消息]]></replacemsg></revokemsg></sysmsg>')
    assert _special_info(old, 10000)["title"] == '"小美" 撤回了一条消息', "replacemsg 照旧认"


def test_her_recall_rewritten_in_place_holds_the_waiting_draft_for_a_person(world):
    run, packet = waiting_draft(world)
    world.clock.now += 30
    take_back(world.wechat, CREATOR, "地址：上海市徐汇区某路1号")
    stored = world.stored(CREATOR)
    run.poll()
    held = world.prm_store.packet(packet["id"])
    assert held["status"] == "pending" and held["needs_attention"] and held["reason"].startswith("达人撤回了一条消息")
    assert world.prm_store.deal(1)["attention"].startswith("达人撤回了一条消息")
    [recall] = world.events("recall")
    assert (recall["detail"]["by"], recall["actor"]) == ("creator", "creator")
    assert len(world.jobs()) == 1 and world.sent == []
    assert world.stored(CREATOR) == stored + 1, "撤回这一行用自己的编号入库（不被当成重复挡掉）"
    run.poll()
    run.poll()
    assert len(world.events("recall")) == 1, "只报一次"


def test_our_own_line_rewritten_in_place_is_an_operators_recall_and_takes_nothing_over(world):
    run, packet = waiting_draft(world)
    world.clock.now += 30
    world.wechat.say(CREATOR, ME, "稍等我问一下", world.clock.now)
    run.poll()
    assert world.prm_store.deal(1)["mode"] == "human", "运营亲手回复，照旧接管"
    world.prm_store.update_deal(1, world.clock.now, mode="copilot", mode_until=None)
    take_back(world.wechat, CREATOR, "稍等我问一下", OWN_REVOKE_XML)
    run.poll()
    [recall] = world.events("recall")
    assert recall["detail"]["by"] == "operator"
    assert world.prm_store.deal(1)["mode"] == "copilot", "撤回不是运营又回了一句"
    assert len(world.events("operator_message")) == 1 and len(world.jobs()) == 1


def test_a_recall_of_a_line_the_burst_still_holds_hands_the_line_on_first(world):
    """她发完马上撤回（连发合并还没交出去）：那一行先交出去，再交撤回，草稿等人看。"""
    world.register()
    run = world.run()
    world.wechat.say(CREATOR, CREATOR, "电话 13800001111", world.clock.now)
    run.poll(flush=False)
    assert world.jobs() == [], "还在连发合并里"
    take_back(world.wechat, CREATOR, "电话 13800001111")
    run.poll(flush=False)
    [job] = world.jobs()
    assert text_lines(job) == ["电话 〈手机1〉"]
    assert [e["detail"]["by"] for e in world.events("recall")] == ["creator"]
    world.mac_drafts(run, draft(reply_draft="收到", message_kind="other", slots={}))
    [packet] = world.prm_store.packets(("pending",))
    assert packet["needs_attention"] and packet["reason"].startswith("达人撤回了一条消息")


def test_only_lines_the_bot_read_lately_in_a_watched_chat_are_looked_at(world):
    """撤回只能在 2 分钟内做：只看最近几分钟读到的行（RECALL_WINDOW_SEC），只看登记达人的私聊。"""
    from src.wechat.wcdb_backend import RECALL_WINDOW_SEC
    world.register()
    run = world.run(private_chats="*")                              # 路人的私聊也在读（只记录）
    world.wechat.say(CREATOR, CREATOR, "很早的一句", world.clock.now - RECALL_WINDOW_SEC - 60)
    world.wechat.say("wxid_stranger", "wxid_stranger", "路人的话", world.clock.now)
    run.poll()
    assert world.stored("wxid_stranger") == 1
    take_back(world.wechat, CREATOR, "很早的一句")
    take_back(world.wechat, "wxid_stranger", "路人的话", '<sysmsg type="revokemsg"><revokemsg><content>"路人" 撤回了一条消息'
                                                     '</content></revokemsg></sysmsg>')
    run.poll()
    assert world.events("recall") == [], "早就不能撤回的行不看"
    assert world.stored("wxid_stranger") == 1, "不是达人的私聊不看"


def test_a_recall_in_a_listed_chat_is_a_chat_notice_with_the_recalled_id(win):
    run = EdgeRun(win)
    win.wechat.say(CREATOR, CREATOR, "你好", win.now)
    win.wechat.say(CREATOR, ME, "在的", win.now + 1)
    run.poll()
    [hers] = win.events(P.CREATOR_MESSAGE)
    [ours] = win.events(P.OPERATOR_MESSAGE)
    take_back(win.wechat, CREATOR, "你好")
    take_back(win.wechat, CREATOR, "在的", OWN_REVOKE_XML)
    run.poll()
    run.poll()
    notices = win.events(P.CHAT_NOTICE)
    assert [n["text"] for n in notices] == ['"小美" 撤回了一条消息', "你撤回了一条消息"], "每个只报一次"
    assert [n["recalled_message_id"] for n in notices] == [hers["message_id"], ours["message_id"]]
    assert {n["message_id"] for n in notices}.isdisjoint({hers["message_id"], ours["message_id"]}), "自己的编号"
    assert win.lines(P.CREATOR_MESSAGE) == ["你好"] and len(win.events(P.OPERATOR_MESSAGE)) == 1
    run.stop()
