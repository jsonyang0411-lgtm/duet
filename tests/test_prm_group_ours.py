"""我们自己的人 (PRM_GROUP_OURS, 2026-11-11; src/prm/boss_groups.py): the names of our own people (colleagues sitting in
creators' groups), resolved to contacts the way PRM_GROUP_WITH's bosses are (``contact_ids``: a name exactly one contact
has; none, or two: not known), kept in this PC's .env only.

The known limit before it: with PRM_GROUP_HER_SIDE on, a colleague of ours in her group counted as her side and had his
lines drafted as hers.  Now a member on the list is never "who is she" (the scan that links groups by itself, the admin's
「接群」 question, the boss scan), never her side; his lines are context labelled 我们 (like a boss's: they have her waiting
draft drafted again at once).  A name that is no one contact's fails safe: the console and the log say so, and no group
is linked (or taken over) by itself until it is put right: the console asks instead.  An empty list changes nothing.

The backend is test_group_link's fake (her team's group: her, her 商务, a colleague of ours named 品牌同事, us); the relay and
the PRM service are real.  Every name, id and number is made up.
"""

import json
import logging
import time

import pytest

from test_group_link import (BD, COLLEAGUE, GROUP, GROUP_NAME, drafted, gmsg, job_of, linked, recall,
                             setup as link_setup)
from test_prm import CREATOR, Clock
from test_prm_boss_groups import BOSS_ID, GROUP as BOSS_GROUP, ME, boss_makes, make
from test_prm_groups_auto_link import QUIET, setup as auto_setup, told

from src.prm import boss_groups as bg

COLLEAGUE_NAME = "品牌同事"


@pytest.fixture
def clock():
    return Clock(time.mktime((2026, 11, 11, 16, 0, 0, 0, 0, -1)))


def known(b, name=COLLEAGUE_NAME, wxid=COLLEAGUE):
    """``name`` is exactly one contact's in this WeChat (``contact_ids``)."""
    b.backend.contacts[name] = wxid


def read(b):
    """The names read again, as a round of the sender thread does when they are due (``sweep`` -> ``_read_bosses``;
    a scan of its own is not run here)."""
    b.clock.now += bg.BOSS_IDS_RETRY_SEC + 1
    groups = b.prm.boss_groups
    with groups._lock:
        groups._read_bosses(groups._names(), b.clock.now)


def stop(b):
    b.prm.stop()
    b.relay.stop()


# ── empty: nothing changes ────────────────────────────────────────────────────

def test_an_empty_list_changes_nothing(clock, tmp_path):
    b = link_setup(clock, tmp_path, prm_group_her_side="on")
    try:
        linked(b, member=1)
        status = b.prm.snapshot()["boss_groups"]
        assert "ours" not in status and "ours_warning" not in status
        row, party = b.prm.store.boss_group(GROUP), b.prm.store.party_by_wxid(CREATOR)
        assert b.prm.boss_groups.side_ids(row, party) == {BD, COLLEAGUE}           # (the known limit, as before)
        assert b.prm.boss_groups.ours_unknown() == [] and b.prm.boss_groups.ours_warning() == ""
    finally:
        stop(b)


# ── never her side, never her: context like a boss's ──────────────────────────

def test_our_colleague_is_never_her_side_and_his_lines_are_ours(clock, tmp_path, caplog):
    b = link_setup(clock, tmp_path, prm_group_her_side="on", prm_group_ours=COLLEAGUE_NAME)
    try:
        known(b)
        linked(b, member=1)
        b.clock.now += bg.BOSS_IDS_RETRY_SEC + 1
        b.prm.boss_groups.sweep()                                      # (the sender thread's round reads the names)
        row, party = b.prm.store.boss_group(GROUP), b.prm.store.party_by_wxid(CREATOR)
        assert b.prm.boss_groups.side_ids(row, party) == {BD}
        assert b.prm.boss_groups.who(row, COLLEAGUE) == bg.US
        with caplog.at_level(logging.INFO):
            assert b.prm.handle(gmsg(b, "样品今天下午寄出", sender=COLLEAGUE)) is True
        assert job_of(b) is None and COLLEAGUE_NAME not in caplog.text
        assert [(x["who"], x["text"]) for x in b.prm.store.boss_group(GROUP)["lines"]] == [("我们", "样品今天下午寄出")]
        b.clock.now += 30
        b.prm.handle(gmsg(b, "帽子到了，上一下链接", sender=BD))          # her 商务: still her side
        meta = json.loads(job_of(b)["meta"])
        assert job_of(b)["text"].endswith(f"{bg.HER_SIDE_MARK}帽子到了，上一下链接")
        assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("我们", "样品今天下午寄出")]
        assert meta["group"]["others"] == 1                            # (her 商务; our colleague counts as us)
    finally:
        stop(b)


def test_a_line_of_our_colleague_has_her_waiting_draft_drafted_again_at_once_as_the_bosss(clock, tmp_path):
    b = link_setup(clock, tmp_path, prm_group_ours=COLLEAGUE_NAME)
    try:
        known(b)
        linked(b, member=1)
        read(b)
        b.prm.handle(gmsg(b, "链接什么时候能上呀"))
        _, packet = drafted(b, "今天就能上哈")
        b.clock.now += 10
        b.prm.handle(gmsg(b, "老师，链接我刚发群里了", sender=COLLEAGUE))
        assert b.prm.store.packet(packet["id"])["status"] == "superseded"
        meta = json.loads(job_of(b)["meta"])
        assert [(x["who"], x["text"]) for x in meta["group"]["lines"]] == [("我们", "老师，链接我刚发群里了")]
    finally:
        stop(b)


def test_a_recall_of_our_colleague_is_never_hers(clock, tmp_path):
    b = link_setup(clock, tmp_path, prm_group_her_side="on", prm_group_ours=COLLEAGUE_NAME)
    try:
        known(b)
        linked(b, member=1)
        read(b)
        b.prm.handle(gmsg(b, "在吗"))
        _, packet = drafted(b)
        assert b.prm.handle(recall(b, COLLEAGUE_NAME)) is True
        assert b.prm.store.packet(packet["id"])["needs_attention"] in (0, False)    # (not her recall: nothing held)
    finally:
        stop(b)


def test_our_colleague_is_not_listed_by_the_jie_qun_question(clock, tmp_path):
    b = link_setup(clock, tmp_path, prm_group_ours=COLLEAGUE_NAME)
    try:
        known(b)
        b.say("接群")
        question = b.say("接群 1").split("\n")
        assert question[1:] == ["1. 小美", "2. 测试商务"] and COLLEAGUE_NAME not in "\n".join(question)
        assert b.say("是 3").startswith("没有第 3 位")
    finally:
        stop(b)


def test_the_scan_links_a_group_whose_other_registered_one_is_ours(clock, tmp_path):
    """Our colleague is registered too (an operator's test, a new friend the bot registered): without the list two
    registered creators are in the group (never a guess); with him on the list she is the one, the group links itself."""
    b = auto_setup(clock, tmp_path)
    try:
        b.prm.register(COLLEAGUE, COLLEAGUE_NAME, "测试")
        assert b.prm.boss_groups.auto_scan()["asked"] == 1 and b.prm.store.boss_group(GROUP) is None
    finally:
        stop(b)
    b = auto_setup(clock, tmp_path, prm_group_ours=COLLEAGUE_NAME)
    try:
        known(b)
        b.prm.register(COLLEAGUE, COLLEAGUE_NAME, "测试")
        read(b)
        assert b.prm.boss_groups.auto_scan()["linked"] == 1
        assert b.prm.store.boss_group(GROUP)["party_id"] == b.her["id"]
    finally:
        stop(b)


def test_the_boss_scan_takes_her_over_when_the_other_member_is_ours(clock, tmp_path):
    for ours in ("", COLLEAGUE_NAME):
        b = make(clock, tmp_path, **({"prm_group_ours": ours} if ours else {}))
        try:
            b.backend.contacts[COLLEAGUE_NAME] = COLLEAGUE
            taken = boss_makes(b, members=(ME, BOSS_ID, CREATOR, COLLEAGUE))
            row = b.prm.store.boss_group(BOSS_GROUP)
            if ours:
                assert taken == 1 and row["state"] == "linked"
            else:
                assert taken == 0 and row["state"] == "pending"            # (two may be her: the console asks)
        finally:
            stop(b)


# ── a name no one contact has: fail safe ──────────────────────────────────────

def test_a_name_no_contact_has_is_said_and_stops_the_scan_linking_by_itself(clock, tmp_path, caplog):
    b = auto_setup(clock, tmp_path, prm_group_ours="不存在的同事")
    try:
        with caplog.at_level(logging.INFO):
            read(b)
        assert "PRM_GROUP_OURS" in caplog.text and "不存在的同事" not in caplog.text
        warning = b.prm.snapshot()["boss_groups"]["ours_warning"]
        assert "「不存在的同事」" in warning and "不自己接" in warning
        assert b.prm.snapshot()["boss_groups"]["ours"] == ["不存在的同事"]
        out = b.prm.boss_groups.auto_scan()
        assert out["linked"] == 0 and out["asked"] == 1 and b.prm.store.boss_group(GROUP) is None
        ask = b.prm.boss_groups.asked_by_scan(GROUP)
        assert [m["wxid"] for m in ask["members"] if m["registered"]] == [CREATOR]
        b.prm.send_approved()
        line = told(b)[-1]
        assert "PRM_GROUP_OURS" in line and "没自动接" in line and f"接群 {GROUP_NAME}" in line
        # put right: read again, the next scan links it
        known(b, "不存在的同事")
        read(b)
        assert "ours_warning" not in b.prm.snapshot()["boss_groups"]
        assert b.prm.boss_groups.auto_scan()["linked"] == 1
    finally:
        stop(b)


def test_names_that_could_not_be_read_are_not_known_the_scan_asks(clock, tmp_path):
    """The names cannot be read (the backend fails): nobody is taken for ours, the console says the names are not read
    yet, the one creator's group is asked about, never linked by itself; once they are read it links itself."""
    b = auto_setup(clock, tmp_path, prm_group_ours=COLLEAGUE_NAME)
    try:
        known(b)
        works = b.backend.contact_ids

        def broken(names):
            raise OSError("the database is busy")

        b.backend.contact_ids = broken
        read(b)
        assert "还没认出来" in b.prm.snapshot()["boss_groups"]["ours_warning"]
        assert b.prm.boss_groups.auto_scan() == {**QUIET, "asked": 1}
        b.backend.contact_ids = works
        read(b)
        assert "ours_warning" not in b.prm.snapshot()["boss_groups"]
        assert scan_again(b)["linked"] == 1
    finally:
        stop(b)


def scan_again(b):
    b.clock.now += bg.BOSS_SCAN_SEC + 1
    return b.prm.boss_groups.auto_scan()


def test_the_boss_scan_waits_for_a_person_while_a_name_of_ours_is_unknown(clock, tmp_path):
    b = make(clock, tmp_path, prm_group_ours="不存在的同事")
    try:
        assert boss_makes(b) == 0
        row = b.prm.store.boss_group(BOSS_GROUP)
        assert row["state"] == "pending" and "PRM_GROUP_OURS" in row["why"]
    finally:
        stop(b)
