"""三方群 (src/prm/three_party.py, 2026-10-30): 「拉三方群」 in a deal makes the group of her, this account and the people of
PRM_GROUP_WITH in the WeChat window (the friend manager's ``create_group`` through the admin handler, faked here), and
the database must show it; 「先试一下」 makes nothing; a run that may or may not have made it waits for a person.  The
backend's lookups run on the fake WeChat database of tests/test_new_friends.py.  Every name and id is made up."""

import json
import logging
import time

import pytest

from test_prm import CREATOR, Bot, Clock

from src.prm import three_party as tp
from src.prm.service import apply_deal_action, event_out
from src.prm.store import PrmStore

BOSS = "老板测试"
HER = "小美测试"            # her chat's name in WeChat (the backend's chat_target)
NEW_GROUP = "123456789@chatroom"


class Admin:
    """The admin handler as 三方群 uses it: ``create_group_checked`` (the WeChat window, faked) makes the group in the
    backend's database when it says ok."""

    can_add_friends = True

    def __init__(self, backend, result=None):
        self.backend = backend
        self.result = result or {"ok": True, "message": "群建好了"}
        self.calls = []

    def create_group_checked(self, members, *, dry_run=False):
        self.calls.append((list(members), dry_run))
        if self.result.get("ok") and not dry_run:
            self.backend.made = {"chat": NEW_GROUP, "name": f"{BOSS}、{HER}", "members": [CREATOR, "wxid_boss", "wxid_me"]}
        return dict(self.result, **({"dry_run": True} if dry_run else {}))


class Backend:
    def __init__(self, name=HER, refused="", readable=True, shows_after=0):
        self.name, self.refused, self.readable, self.shows_after = name, refused, readable, shows_after
        self.made = None
        self.looks = 0

    def chat_target(self, wxid):
        return self.name, self.refused

    def group_ids(self):
        return {"old@chatroom"} if self.readable else None

    def find_group_with(self, member, known):
        self.looks += 1
        made = self.made
        if made is None or made["chat"] in known or member not in made["members"] or self.looks <= self.shows_after:
            return None
        return {"chat": made["chat"], "name": made["name"], "members": len(made["members"])}


class Auto:
    def __init__(self, paused=""):
        self.paused = paused

    def paused_why(self):
        return self.paused


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 10, 30, 14, 30, 0, 0, 0, -1)))


def make(clock, tmp_path, *, with_=BOSS, result=None, backend=None, paused="", **prm):
    b = Bot(clock, tmp_path, prm_group_with=with_, prm_friend_add_brand="品牌测试", **prm)
    party = b.prm.register(CREATOR, "小美", "测试达人")
    b.deal_id = b.prm.store.deal_of(party["id"])["id"]
    b.party = party
    b.backend = backend or Backend()
    b.admin = Admin(b.backend, result)
    b.slept = []
    b.prm.three_party.attach(b.admin, Auto(paused), b.backend)
    b.prm.three_party.spawn = lambda fn: fn()
    b.prm.three_party._sleep = b.slept.append
    return b


def group(b):
    return b.prm.store.deal(b.deal_id)["three_party"]


def shown(b):
    snap = b.prm.snapshot()
    return next(p for p in snap["parties"] if p["id"] == b.party["id"])["deal"]["three_party"], snap["three_party"]


def test_one_click_makes_the_group_and_every_later_draft_job_hears_of_it(clock, tmp_path):
    b = make(clock, tmp_path)
    assert b.prm.deal_action("group", {"deal_id": b.deal_id}) == {"ok": True, "state": "creating"}
    assert b.admin.calls == [([HER, BOSS], False)]                    # her chat's name, then PRM_GROUP_WITH
    assert group(b) == {"state": "created", "at": clock.now, "by": "console", "chat": NEW_GROUP,
                        "name": f"{BOSS}、{HER}", "members": 3}
    deal_group, status = shown(b)
    assert deal_group == {"state": "created", "label": "已建好", "at": clock.now, "by": "console", "why": "",
                          "chat_name": f"{BOSS}、{HER}", "made": True}
    assert status == {"enabled": True, "with": [BOSS], "why": "", "busy": None, "name_template": "品牌测试×{昵称}"}
    names = [event_out(e)["name"] for e in b.prm.store.events(b.deal_id, kinds=(tp.EVENT,))]
    assert names == ["三方群建好了（运营点的）", "开始拉三方群（运营点的）"]
    assert b.prm.store.deal(b.deal_id)["attention"] == ""
    b.prm.handle(b.msg("好的，我在群里了"))                              # her next line: its job tells the Mac
    job = b.relay.store.latest_open(CREATOR, drafts_only=True, system=False)
    assert json.loads(job["meta"])["three_party"] == {"at": "10-30 14:30"}
    assert b.prm.deal_action("group", {"deal_id": b.deal_id}) == {"ok": False, "error": "这位达人的三方群已经建好了"}
    assert len(b.admin.calls) == 1


def test_a_try_picks_them_and_makes_nothing(clock, tmp_path):
    b = make(clock, tmp_path)
    assert b.prm.deal_action("group_try", {"deal_id": b.deal_id}) == {"ok": True, "state": "trying"}
    assert b.admin.calls == [([HER, BOSS], True)]
    assert group(b) == {} and b.backend.made is None
    deal_group, _ = shown(b)
    assert deal_group == {"tried": {"at": clock.now, "ok": True, "message": "群建好了"}}
    assert b.prm.store.events(b.deal_id, kinds=(tp.EVENT,)) == []
    assert b.prm.deal_action("group_try", {"deal_id": b.deal_id})["ok"] is True      # may be tried again and again


@pytest.mark.parametrize("case, error", [
    ("off", "没设要一起拉进群的人（PRM_GROUP_WITH）"),
    ("no_manager", "这台电脑没有好友管理组件（需要 Windows 版微信窗口），拉不了群"),
    ("no_backend", "这台电脑的微信读法认不出达人的聊天名字，拉不了群"),
    ("stopped", "这位达人已经停用（不在达人名单里），不拉群"),
    ("namesake", "不能拉群：和另一个聊天同名"),
    ("no_name", "不知道她在微信里叫什么，没法在「发起群聊」里搜她"),
    ("her_name_is_his", "她在微信里的名字和要一起拉进群的人（PRM_GROUP_WITH）一样，分不清，不拉群"),
    ("paused", "微信提示过操作太频繁，加好友和拉群都先停着（风控暂停）：管理员发「自动同意好友 开」以后再拉"),
    ("busy", "另一个群正在建（或正在试）：等它好了再点"),
    ("unsure", "上次不确定建没建成：先在微信里看一眼，点「建好了」或「没建成」"),
    ("unknown", "没有找到这个合作单"),
])
def test_what_stands_in_the_way_is_said_at_once_and_nothing_is_clicked(clock, tmp_path, case, error):
    b = make(clock, tmp_path, with_="" if case == "off" else BOSS, paused="风控暂停" if case == "paused" else "",
             backend=Backend(name="" if case == "no_name" else BOSS if case == "her_name_is_his" else HER,
                             refused="和另一个聊天同名" if case == "namesake" else ""))
    if case == "no_manager":
        b.admin.can_add_friends = False
    if case == "no_backend":
        b.prm.three_party.attach(b.admin, Auto(), object())
    if case == "stopped":
        b.prm.set_active(b.party["id"], False)
    if case == "busy":
        b.prm.three_party._busy = 999
    if case == "unsure":
        b.prm.store.update_deal(b.deal_id, clock.now, three_party={"state": "unsure", "at": clock.now})
    deal_id = 4242 if case == "unknown" else b.deal_id
    for action in (("group", "group_try") if case not in ("unsure",) else ("group",)):
        assert b.prm.deal_action(action, {"deal_id": deal_id}) == {"ok": False, "error": error}, action
    assert b.admin.calls == []


def test_nothing_made_is_failed_and_may_be_tried_again(clock, tmp_path):
    b = make(clock, tmp_path, result={"ok": False, "reason": "not_found",
                                       "message": f"在「发起群聊」里没搜到「{BOSS}」，没有建群"})
    assert b.prm.deal_action("group", {"deal_id": b.deal_id})["ok"] is True
    assert group(b) == {"state": "failed", "at": clock.now, "by": "console", "why": f"在「发起群聊」里没搜到「{BOSS}」，没有建群"}
    assert b.prm.store.deal(b.deal_id)["attention"] == ""               # nothing to look at: the block says why
    b.admin.result = {"ok": True}
    assert b.prm.deal_action("group", {"deal_id": b.deal_id})["ok"] is True
    assert group(b)["state"] == "created"


@pytest.mark.parametrize("reason", ["not_created", "risk", "error"])
def test_a_done_that_may_have_happened_waits_for_a_person(clock, tmp_path, reason):
    b = make(clock, tmp_path, result={"ok": False, "reason": reason, "message": "群可能没建成，请在微信里看一眼",
                                       **({"paused": "（自动同意好友已暂停）"} if reason == "risk" else {})})
    b.prm.deal_action("group", {"deal_id": b.deal_id})
    g = group(b)
    assert g["state"] == "unsure"
    assert g["why"] == "群可能没建成，请在微信里看一眼" + ("（自动同意好友已暂停）" if reason == "risk" else "")
    assert b.prm.store.deal(b.deal_id)["attention"] == "三方群" + g["why"]
    assert b.prm.deal_action("group", {"deal_id": b.deal_id})["ok"] is False      # not again until a person says
    assert b.prm.deal_action("group_made", {"deal_id": b.deal_id}) == {"ok": True, "state": "created"}
    assert group(b) == {"state": "created", "at": clock.now, "by": "console"}
    assert b.prm.store.deal(b.deal_id)["attention"] == ""
    assert b.prm.three_party.job_meta(b.prm.store.deal(b.deal_id)) == {"at": "10-30 14:30"}


def test_a_group_the_database_does_not_show_is_unsure(clock, tmp_path):
    for backend in (Backend(shows_after=99), Backend(readable=False)):
        b = make(clock, tmp_path, backend=backend)
        b.prm.deal_action("group", {"deal_id": b.deal_id})
        assert group(b)["state"] == "unsure"
        assert "数据库里没看到有她的新群" in group(b)["why"]
        assert backend.looks == (tp.FIND_LOOKS if backend.readable else 0)
        assert len(b.slept) == (tp.FIND_LOOKS - 1 if backend.readable else 0)
        assert b.prm.deal_action("group_none", {"deal_id": b.deal_id}) == {"ok": True, "state": ""}
        assert group(b) == {} and b.prm.store.deal(b.deal_id)["attention"] == ""


def test_a_group_that_shows_up_a_moment_later_is_found(clock, tmp_path):
    b = make(clock, tmp_path, backend=Backend(shows_after=2))
    b.prm.deal_action("group", {"deal_id": b.deal_id})
    assert group(b)["state"] == "created" and b.backend.looks == 3 and b.slept == [tp.FIND_STEP_SEC] * 2


def test_marks_by_hand_and_while_making(clock, tmp_path):
    b = make(clock, tmp_path)
    assert b.prm.deal_action("group_none", {"deal_id": b.deal_id}) == {"ok": False, "error": "三方群已经是这个状态了"}
    assert b.prm.deal_action("group_made", {"deal_id": b.deal_id}) == {"ok": True, "state": "created"}  # made by hand
    assert group(b) == {"state": "created", "at": clock.now, "by": "console"}
    assert shown(b)[0]["made"] is False
    assert b.prm.deal_action("group_made", {"deal_id": b.deal_id})["ok"] is False
    assert b.prm.deal_action("group_none", {"deal_id": b.deal_id})["ok"] is True      # 「群已经不在了」
    b.prm.store.update_deal(b.deal_id, clock.now, three_party={"state": "creating", "at": clock.now})
    for action in ("group_made", "group_none"):
        assert b.prm.deal_action(action, {"deal_id": b.deal_id}) == {"ok": False, "error": "这个群正在建，等它好了再说"}


def test_a_bot_stopped_while_making_finds_it_unsure_at_start(clock, tmp_path):
    b = make(clock, tmp_path)
    b.prm.store.update_deal(b.deal_id, clock.now, three_party={"state": "creating", "at": clock.now - 60, "by": "console"})
    assert b.prm.three_party.settle() == 1
    assert group(b)["state"] == "unsure" and "机器人在建群的时候停了" in group(b)["why"]
    assert "三方群机器人在建群的时候停了" in b.prm.store.deal(b.deal_id)["attention"]
    assert b.prm.three_party.settle() == 0


def test_an_error_in_the_making_is_unsure_and_the_next_click_is_not_blocked(clock, tmp_path, caplog):
    b = make(clock, tmp_path)

    def broken(members, *, dry_run=False):
        raise RuntimeError("the window went away")
    b.admin.create_group_checked = broken
    with caplog.at_level(logging.ERROR):
        b.prm.deal_action("group", {"deal_id": b.deal_id})
    assert group(b)["state"] == "unsure" and b.prm.three_party._busy is None


def test_the_stopped_bot_cannot_make_one(clock, tmp_path):
    store = PrmStore(":memory:")
    for action in tp_actions():
        assert apply_deal_action(store, action, {"deal_id": 1}, now=clock.now, takeover_sec=7200) == {
            "ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能拉群"}


def tp_actions():
    from src.prm.service import GROUP_ACTIONS
    return GROUP_ACTIONS


def test_the_purge_clears_wechats_name_for_the_group(clock, tmp_path):
    b = make(clock, tmp_path)
    b.prm.deal_action("group", {"deal_id": b.deal_id})
    b.prm.purge(b.prm.store.deal(b.deal_id), clock.now)
    assert group(b) == {"state": "created", "at": clock.now, "by": "console"}


def test_the_suggested_name_and_the_names_setting():
    assert tp.suggested_name("{品牌}×{昵称}", "品牌测试", "小美") == "品牌测试×小美"
    assert tp.suggested_name("{品牌}×{昵称}", "", " 小  美 ") == "小 美"
    assert tp.suggested_name("{昵称}的三方群", "", "小美") == "小美的三方群"
    assert len(tp.suggested_name("{品牌}×{昵称}", "品牌", "很" * 40)) == tp.NAME_MAX_CHARS
    assert tp.group_with(" 老板测试 ， 王总、老板测试;李 四 ") == ["老板测试", "王总", "李 四"]
    assert tp.group_with("") == []


def test_no_name_is_ever_logged(clock, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        b = make(clock, tmp_path)
        b.prm.deal_action("group_try", {"deal_id": b.deal_id})
        b.prm.deal_action("group", {"deal_id": b.deal_id})
        failing = make(clock, tmp_path, result={"ok": False, "reason": "not_found", "message": f"没搜到「{BOSS}」"})
        failing.prm.deal_action("group", {"deal_id": failing.deal_id})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "三方群" in text
    for name in (BOSS, HER, "小美"):
        assert name not in text


# ── the WCDB backend's lookups (the fake WeChat database of tests/test_new_friends.py) ──

def test_the_backend_finds_her_new_group_and_her_chat_name(tmp_path, monkeypatch):
    from test_new_friends import Clock as DbClock, WeChatDb

    from src.config import BotConfig
    from src.db import MessageStore, initialize_db
    from src.wechat.wcdb_backend import WcdbBackend

    monkeypatch.chdir(tmp_path)
    db = WeChatDb(DbClock())
    db.say(CREATOR, CREATOR, "你好", 1)
    db.names[CREATOR] = HER
    members = {}
    db.get_group_members = lambda chat: [{"username": u} for u in members.get(chat, [])]
    db.times["old@chatroom"] = 5
    members["old@chatroom"] = [CREATOR, "wxid_x"]
    db.names[NEW_GROUP] = f"{BOSS}、{HER}"
    config = BotConfig(private_chat_enabled=True, private_chats="*", prm_copilot="on")
    backend = WcdbBackend(bot_display_name="机器人", groups=["*"], poll_sec=0,
                          store=MessageStore(initialize_db(str(tmp_path / "m.db"))), config=config)
    backend._client = db
    backend._chat_names[CREATOR] = HER
    known = backend.group_ids()
    assert known == {"old@chatroom"}
    assert backend.find_group_with(CREATOR, known) is None                # the old group is not new
    db.times[NEW_GROUP] = 9
    members[NEW_GROUP] = ["wxid_boss", CREATOR, "wxid_me"]
    assert backend.find_group_with(CREATOR, known) == {"chat": NEW_GROUP, "name": f"{BOSS}、{HER}", "members": 3}
    assert backend.find_group_with("wxid_somebody", known) is None
    assert backend.chat_target(CREATOR) == (HER, "")
    backend._chat_names["wxid_twin"] = HER                                # another chat of her name: refused
    name, refused = backend.chat_target(CREATOR)
    assert name == HER and refused
    backend._client = None
    assert backend.group_ids() is None and backend.find_group_with(CREATOR, known) is None
