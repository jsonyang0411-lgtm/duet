"""微信边缘（Mac）：Windows 报来的新好友，PRM_AUTO_REGISTER=on 时认出平台的自动登记（PrmService.edge_new_friend）。

认平台（edge_identity）：备注「抖音-昵称」「小红书-昵称」；她的微信名、验证消息里有我们发过卡的抖音昵称（邀约台账
PRM_INVITE_LEDGER）；验证消息里说了平台。认不出的不登记，留在「登记达人」的新加的好友里。抖音昵称在台账里恰好对上一位
就关联她的抖音号。测试里的达人、抖音号都是编的。
"""

import json

from test_prm import Clock, config

from src.prm.service import PrmService, edge_identity, invite_nicknames
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

LEDGER_ROWS = [
    {"dy": "huahua_01", "nickname": "阿花", "outcome": "ACCEPTED"},
    {"dy": "xiaomei88", "nickname": "小美 Mei", "outcome": "ACCEPTED"},
    {"dy": "dup_a", "nickname": "同名", "outcome": "ACCEPTED"},
    {"dy": "dup_b", "nickname": "同名", "outcome": "ACCEPTED"},
    {"dy": "never_sent", "nickname": "没发出去", "outcome": "REJECTED"},
]


def ledger(tmp_path):
    path = tmp_path / "invites.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in LEDGER_ROWS) + "\n坏行\n", encoding="utf-8")
    return str(path)


def test_the_ledger_gives_nicknames_only_of_cards_the_platform_took_and_none_for_a_shared_name(tmp_path):
    names = invite_nicknames(ledger(tmp_path))
    assert names["阿花"] == ("huahua_01", "阿花")
    assert names["小美mei"] == ("xiaomei88", "小美 Mei")           # spaces and case do not count
    assert names["同名"][0] == "" and "没发出去" not in names
    assert invite_nicknames("") == {} and invite_nicknames(str(tmp_path / "none.jsonl")) == {}


def test_the_platform_is_read_from_the_remark_the_name_or_the_greeting(tmp_path):
    names = invite_nicknames(ledger(tmp_path))
    who = edge_identity({"remark": "抖音-阿花", "name": "花花"}, names)
    assert who == {"platform": "抖音", "name": "阿花", "account": "huahua_01", "note": "抖音-阿花"}
    assert edge_identity({"remark": "抖音-阿花主播"}, names)["account"] == "huahua_01"   # a ledger name inside it
    assert edge_identity({"remark": "小红书-阿花"}, names) == {
        "platform": "小红书", "name": "阿花", "account": "", "note": "小红书-阿花"}
    said = edge_identity({"name": "Rose", "verify_text": "你好我是小美Mei"}, names)
    assert (said["platform"], said["name"], said["account"]) == ("抖音", "小美 Mei", "xiaomei88")
    assert edge_identity({"name": "阿花"}, names)["account"] == "huahua_01"
    told = edge_identity({"name": "Rose", "verify_text": "在抖音上看到你们的帽子"}, names)
    assert (told["platform"], told["name"], told["account"], told["note"]) == ("抖音", "Rose", "", "抖音-Rose")
    assert edge_identity({"name": "Rose", "verify_text": "我是同名"}, names) is None     # two 抖音号: not sure
    assert edge_identity({"name": "快递小哥", "verify_text": "你好"}, names) is None
    assert edge_identity({"remark": "朋友-老王"}, names) is None


def service(tmp_path, clock, auto="on"):
    cfg = config(prm_auto_register=auto, prm_invite_ledger=ledger(tmp_path))
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), clock=clock, files_dir=tmp_path / "relay")
    return PrmService(cfg, relay, store=PrmStore(":memory:"), clock=clock, send=lambda c, t: True)


def test_a_new_friend_on_a_platform_is_registered_and_linked_and_one_unknown_is_left(tmp_path):
    clock = Clock()
    prm = service(tmp_path, clock)
    prm.edge_new_friend({"type": "friend_added", "wxid": "wxid_hua01", "name": "花花", "remark": "抖音-阿花",
                         "how": "requested"})
    prm.edge_new_friend({"type": "friend_added", "wxid": "wxid_rose1", "name": "Rose", "verify_text": "在吗"})
    assert prm.register_found() == 1
    party = prm.store.party_by_wxid("wxid_hua01")
    assert (party["name"], party["note"]) == ("阿花", "抖音-阿花")
    assert [link["account"] for link in prm.store.creator_links(party["id"])] == ["huahua_01"]
    assert prm.store.party_by_wxid("wxid_rose1") is None, "认不出平台：等人来点"


def test_a_new_friend_queued_before_a_restart_is_registered_after_it_with_what_was_recognised(tmp_path):
    clock = Clock()
    prm = service(tmp_path, clock)
    prm.edge_new_friend({"type": "friend_added", "wxid": "wxid_hua01", "name": "花花", "verify_text": "我是阿花"})
    cfg = config(prm_auto_register="on", prm_invite_ledger=ledger(tmp_path))
    again = PrmService(cfg, prm._relay, store=prm.store, clock=clock, send=lambda c, t: True)
    assert again.resume_found() == 1 and again.register_found() == 1
    party = again.store.party_by_wxid("wxid_hua01")
    assert party["note"] == "抖音-阿花" and again.store.creator_links(party["id"])[0]["account"] == "huahua_01"


def test_off_nobody_is_registered_and_a_registered_or_stopped_creator_is_left_alone(tmp_path):
    clock = Clock()
    off = service(tmp_path, clock, auto="off")
    off.edge_new_friend({"type": "friend_added", "wxid": "wxid_hua01", "remark": "抖音-阿花"})
    assert off.register_found() == 0 and off.store.party_by_wxid("wxid_hua01") is None
    prm = service(tmp_path, clock)
    prm.register("wxid_hua01", "阿花")
    prm.edge_new_friend({"type": "friend_added", "wxid": "wxid_hua01", "remark": "抖音-阿花"})
    assert prm.register_found() == 0
