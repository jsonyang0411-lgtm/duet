"""加百应达人微信's storage in prm.db (src/prm/store.py, 功能 R): the records (``friend_adds``: the full values only
here, cleared later, their masked form kept), every add_friend call (``friend_add_attempts``) and who a registered
creator is on 抖音 (``creator_links``).  Every value is made up."""

import sqlite3

import pytest

from src.prm.store import PrmStore

AWEME = "dy_demo01"


@pytest.fixture
def store():
    s = PrmStore(":memory:")
    yield s
    s.close()


def offered(store, now=100.0, **fields):
    values = dict(source="paste", aweme_id=AWEME, nickname="小鹿", wechat="abcdefyz", phone="13800000000",
                  masked={"wechat": "ab****yz", "phone": "138****0000"}, greeting="您好小鹿，百应上联系您的～",
                  invited={"aweme_id": AWEME, "plan_id": "P1"})
    values.update(fields)
    return store.add_friend_add(state="offered", now=now, **values)


def test_a_new_database_and_an_old_one_get_the_three_tables(tmp_path):
    path = tmp_path / "prm.db"
    old = sqlite3.connect(str(path))                     # a prm.db from before 功能 R: only an older table
    old.execute("CREATE TABLE parties (id INTEGER PRIMARY KEY AUTOINCREMENT, wxid TEXT NOT NULL UNIQUE, name TEXT NOT NULL,"
                " note TEXT NOT NULL DEFAULT '', authorized_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,"
                " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    old.commit()
    old.close()
    for _ in range(2):                                   # opening it twice changes nothing more
        s = PrmStore(path)
        s.close()
    conn = sqlite3.connect(str(path))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    conn.close()
    assert {"friend_adds", "friend_add_attempts", "creator_links", "friend_adds_msg", "friend_adds_state"} <= names


def test_a_record_keeps_its_values_and_its_json_columns(store):
    record, made = offered(store)
    assert made and record["id"] == 1 and record["state"] == "offered" and record["created_at"] == 100.0
    assert (record["wechat"], record["phone"]) == ("abcdefyz", "13800000000")
    assert record["masked"] == {"wechat": "ab****yz", "phone": "138****0000"}
    assert record["invited"] == {"aweme_id": AWEME, "plan_id": "P1"}
    assert record["candidate"] == {} and record["reveals"] == [] and record["rejected"] == []
    assert record["unavailable"] is False and record["link_on_accept"] is True
    assert store.friend_add(1) == record and store.friend_add(99) is None


def test_one_baiying_message_is_one_record_but_pasted_ones_are_each_their_own(store):
    first, made = offered(store, source="buyin_card", conversation_id="c1", message_id="m1")
    again, made_again = offered(store, source="buyin_card", conversation_id="c1", message_id="m1", wechat="other1")
    assert made and not made_again and again["id"] == first["id"] and again["wechat"] == "abcdefyz"
    assert store.friend_add_by_message("c1", "m1")["id"] == first["id"]
    assert store.friend_add_by_message("c1", "m2") is None
    other, made_other = offered(store, source="buyin_card", conversation_id="c2", message_id="m1")
    assert made_other and other["id"] != first["id"]
    pasted = [offered(store)[0]["id"], offered(store)[0]["id"]]       # no message id: never folded together
    assert len(set(pasted)) == 2


def test_listing_by_state_and_by_douyin_id_newest_first(store):
    a = offered(store, now=1.0)[0]
    b = offered(store, now=2.0, aweme_id="dy_other02")[0]
    assert store.move_friend_add(a["id"], ("offered",), "dropped", now=3.0, closed_at=3.0)
    assert [r["id"] for r in store.friend_adds()] == [b["id"], a["id"]]
    assert [r["id"] for r in store.friend_adds(states=("offered",))] == [b["id"]]
    assert [r["id"] for r in store.friend_adds(aweme_id="DY_OTHER02")] == [b["id"]]        # (case is not looked at)
    assert [r["id"] for r in store.friend_adds(limit=1)] == [b["id"]]


def test_a_state_moves_only_from_the_states_given(store):
    record = offered(store)[0]
    assert store.move_friend_add(record["id"], ("offered", "failed"), "adding", now=5.0, reason="")
    assert not store.move_friend_add(record["id"], ("offered", "failed"), "adding", now=6.0)   # a second click
    moved = store.friend_add(record["id"])
    assert moved["state"] == "adding" and moved["updated_at"] == 5.0
    assert store.move_friend_add(record["id"], ("adding",), "requested", now=7.0, reason="",
                                 candidate={"wxid": "wxid_demo", "level": "sure"}, requested_at=7.0)
    moved = store.friend_add(record["id"])
    assert moved["candidate"] == {"wxid": "wxid_demo", "level": "sure"} and moved["requested_at"] == 7.0
    with pytest.raises(ValueError):
        store.move_friend_add(record["id"], ("requested",), "linked", state="x")
    with pytest.raises(ValueError):
        store.move_friend_add(record["id"], ("requested",), "linked", not_a_field=1)


def test_fields_change_in_place_and_only_in_the_states_given(store):
    record = offered(store)[0]
    assert store.update_friend_add(record["id"], now=8.0, greeting="改过的附言", reveals=[{"at": 8.0, "actor": "console"}])
    assert not store.update_friend_add(record["id"], states=("requested",), greeting="不会写进去")
    changed = store.friend_add(record["id"])
    assert changed["greeting"] == "改过的附言" and changed["reveals"] == [{"at": 8.0, "actor": "console"}]
    assert changed["updated_at"] == 8.0 and changed["state"] == "offered"


def test_clearing_the_values_keeps_the_masked_form(store):
    record = offered(store)[0]
    assert store.clear_friend_add_values(record["id"], now=9.0)
    cleared = store.friend_add(record["id"])
    assert (cleared["wechat"], cleared["phone"], cleared["cleared_at"]) == ("", "", 9.0)
    assert cleared["masked"] == {"wechat": "ab****yz", "phone": "138****0000"}
    assert not store.clear_friend_add_values(record["id"], now=10.0)       # nothing left to clear
    assert store.friend_add(record["id"])["cleared_at"] == 9.0


def test_attempts_are_counted_and_finished(store):
    record = offered(store)[0]
    first = store.add_friend_attempt(record["id"], by="console", kind="wechat", now=10.0)
    second = store.add_friend_attempt(record["id"], by="admin", kind="phone", now=20.0)
    store.finish_friend_attempt(first, "ok", now=11.0)
    rows = store.friend_attempts()
    assert [(r["id"], r["by"], r["kind"], r["reason"], r["finished_at"]) for r in rows] == [
        (second, "admin", "phone", "", None), (first, "console", "wechat", "ok", 11.0)]
    assert [r["id"] for r in store.friend_attempts(since=15.0)] == [second]
    assert [r["id"] for r in store.friend_attempts(unfinished=True)] == [second]
    assert [r["id"] for r in store.friend_attempts(add_id=record["id"])] == [second, first]


def test_creator_links_are_kept_once_per_account(store):
    party = store.add_party("wxid_demo", "小鹿", now=1.0)
    assert store.add_creator_link(party["id"], AWEME, nickname="小鹿", source="friend_add", add_id=3, now=2.0)
    assert not store.add_creator_link(party["id"], AWEME, source="register", now=3.0)     # the first one stays
    assert store.add_creator_link(party["id"], "dy_demo02", uid="7000000001", uid_source="buyin_im", source="register",
                                  now=4.0)
    links = store.creator_links(party["id"])
    assert [(l["account"], l["source"], l["add_id"], l["uid"], l["uid_source"]) for l in links] == [
        (AWEME, "friend_add", 3, "", ""), ("dy_demo02", "register", None, "7000000001", "buyin_im")]
    assert [l["party_id"] for l in store.creator_links(account="DY_DEMO01")] == [party["id"]]
    assert store.creator_links(store.add_party("wxid_other", "别人", now=5.0)["id"]) == []
    assert store.delete_creator_link(party["id"], "dy_demo02")
    assert not store.delete_creator_link(party["id"], "dy_demo02")
    assert [l["account"] for l in store.creator_links(party["id"])] == [AWEME]


def test_ended_records_are_deleted_with_their_attempts_after_the_retention(store):
    old = offered(store, now=1.0)[0]
    store.add_friend_attempt(old["id"], by="console", kind="wechat", now=2.0)
    store.move_friend_add(old["id"], ("offered",), "dropped", now=3.0, closed_at=3.0)
    recent = offered(store, now=4.0)[0]
    store.move_friend_add(recent["id"], ("offered",), "linked", now=50.0, closed_at=50.0)
    live = offered(store, now=5.0)[0]
    assert store.delete_friend_adds(closed_before=10.0) == 1
    assert [r["id"] for r in store.friend_adds()] == [live["id"], recent["id"]]
    assert store.friend_attempts(add_id=old["id"]) == []


def test_the_clean_up_of_a_creator_clears_her_records_and_masks_their_nicknames(store):
    party = store.add_party("wxid_demo", "小鹿", now=1.0)
    mine = offered(store, nickname="小鹿 13800000000", party_id=party["id"])[0]
    other = offered(store, nickname="别人 13900000000")[0]
    store.add_creator_link(party["id"], AWEME, nickname="小鹿 13800000000", source="friend_add", now=2.0)
    assert store.scrub_friend_adds(party["id"], lambda text: text.replace("13800000000", "138****0000"), now=9.0) == 2
    cleaned = store.friend_add(mine["id"])
    assert (cleaned["wechat"], cleaned["phone"], cleaned["nickname"], cleaned["cleared_at"]) == (
        "", "", "小鹿 138****0000", 9.0)
    assert store.creator_links(party["id"])[0]["nickname"] == "小鹿 138****0000"
    assert store.friend_add(other["id"])["wechat"] == "abcdefyz"           # someone else's stays as it is
