"""名单合一（src/influencer/prm_bridge.py）：达人模块的名单为准，PRM 的登记跟着它；合作单页的改动写回名单。

前半段用内存里的中转和 PRM（test_prm 的 Bot），后半段用假 Windows 边缘跑真实的 HTTP：
在名单页加的达人推给了 Windows，消息变成 PRM 的起草任务，不再走达人模块的直接回复。
"""

import logging
from types import SimpleNamespace

import pytest

from test_prm import Bot as PrmBot, Clock
from test_prm_over_edge import Mac, wait_for

from src.bot import Bot
from src.edge.client import EdgeClient
from src.edge.fake import FakeEdge
from src.influencer.prm_bridge import SOURCE_PRM
from src.influencer.store import SOURCE_CONSOLE, InfluencerStore
from src.wechat.edge_backend import EdgeBackend

A, B = "wxid_creator_a", "wxid_creator_b"


@pytest.fixture
def wired(tmp_path):
    """名单、PRM、edge 后端（不启动），按 Bot._wire_edge_prm 接好；返回 (名单, PRM, 后端)。"""
    contacts = InfluencerStore(str(tmp_path / "messages.db"))
    side = PrmBot(Clock(), tmp_path)
    backend = EdgeBackend(client=EdgeClient("http://127.0.0.1:9", "x" * 32), state_path=str(tmp_path / "s.json"))
    side.prm.on_parties_changed = backend.refresh_private_chats_soon      # 和 _start_prm 一样
    contacts.save_contact(A, source=SOURCE_CONSOLE, display_name="小美", note="家居博主")
    Bot._wire_edge_prm(backend, side.prm, SimpleNamespace(store=contacts))
    yield contacts, side.prm, backend
    side.prm.stop()
    side.relay.stop()


def test_the_list_registers_its_creators_in_prm(wired):
    contacts, prm, backend = wired
    party = prm.store.party_by_wxid(A)
    assert (party["name"], party["note"]) == ("小美", "家居博主"), "启动时就同步了"
    contacts.save_contact(B, source=SOURCE_CONSOLE)
    assert backend.parties() == [A, B], "推给 Windows 的就是同步后的名单"
    assert prm.store.party_by_wxid(B)["name"] == B, "没填称呼的用 wxid"


def test_disabling_on_the_list_unregisters_and_enabling_brings_back(wired):
    contacts, prm, backend = wired
    contacts.save_contact(A, source=SOURCE_CONSOLE, enabled=False)
    assert backend.parties() == [] and prm.store.party_by_wxid(A) is None
    contacts.save_contact(A, source=SOURCE_CONSOLE, enabled=True)
    assert backend.parties() == [A]
    assert len(prm.store.parties(include_inactive=True)) == 1, "回来的还是原来那位，合作单不丢"


def test_the_deals_page_writes_back_to_the_list(wired):
    contacts, prm, backend = wired
    prm.register(B, "阿青", "合作单页登记的")
    contact = contacts.get_contact(B)
    assert contact["enabled"] and (contact["display_name"], contact["note"]) == ("阿青", "合作单页登记的")
    prm.set_active(prm.store.party_by_wxid(A)["id"], False)
    assert contacts.get_contact(A)["enabled"] is False, "合作单页停用的，名单里也停用"
    assert backend.parties() == [B], "再同步也不会被名单加回来"
    import sqlite3
    with sqlite3.connect(contacts._db_path) as conn:
        logged = conn.execute("SELECT chat_id, action FROM influencer_events WHERE source = ?", (SOURCE_PRM,)).fetchall()
    assert logged == [(B, "加入名单"), (A, "停用")], "名单的操作记录里写明是合作单页改的"


def test_on_start_the_list_wins_a_disagreement(tmp_path):
    contacts = InfluencerStore(str(tmp_path / "messages.db"))
    side = PrmBot(Clock(), tmp_path)
    side.prm.register(A, "小美")                                    # PRM 里登记着
    contacts.save_contact(A, source=SOURCE_CONSOLE, enabled=False)   # 名单里停用了（比如 Bot 停着时改的）
    backend = EdgeBackend(client=EdgeClient("http://127.0.0.1:9", "x" * 32), state_path=str(tmp_path / "s.json"))
    Bot._wire_edge_prm(backend, side.prm, SimpleNamespace(store=contacts))
    assert side.prm.store.party_by_wxid(A) is None and contacts.get_contact(A)["enabled"] is False
    side.prm.stop()
    side.relay.stop()


def test_a_chat_id_prm_refuses_is_reported_once(wired, caplog):
    contacts, prm, backend = wired
    contacts.save_contact("12345@chatroom", source=SOURCE_CONSOLE)
    with caplog.at_level(logging.WARNING):
        backend.parties()
        backend.parties()
    assert [r for r in caplog.records if "登记不进 PRM" in r.getMessage()].__len__() == 1
    assert backend.parties() == [A], "别的达人照常"


def test_a_failing_source_does_not_push_a_short_list(tmp_path):
    with FakeEdge(auto_send=False) as fake:
        backend = EdgeBackend(client=EdgeClient(fake.url, fake.token), state_path=str(tmp_path / "s.json"))
        backend.add_parties_source(lambda: [A])
        backend._sync_parties()
        backend.add_parties_source(lambda: 1 / 0)
        backend.state.parties_digest = ""
        backend._sync_parties()
        assert fake._parties == {A} and fake._parties_version == 1


# ---- 通过假 Windows 跑一遍 ----

def test_a_creator_added_on_the_list_page_is_drafted_by_prm(tmp_path):
    contacts = InfluencerStore(str(tmp_path / "messages.db"))
    with FakeEdge(auto_send=False) as fake:
        mac = Mac(fake, tmp_path)
        Bot._wire_edge_prm(mac.backend, mac.prm, SimpleNamespace(store=contacts))
        with mac:
            contacts.save_contact(A, source=SOURCE_CONSOLE, display_name="小美")
            assert wait_for(lambda: A in fake._parties), "名单页加的达人推给了 Windows"
            fake.creator_says(A, "想申请样品")
            assert wait_for(lambda: mac.relay.store.list_jobs())
            [job] = mac.relay.store.list_jobs()
            assert job["chat_id"] == A and job["kind"] == "prm"
        assert contacts.list_inbox() == [], "不再走达人模块的直接回复"
