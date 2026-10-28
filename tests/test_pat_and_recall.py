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

