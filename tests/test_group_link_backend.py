"""接手任何合作群 (2026-11-09, the backend's part): an operator links any group to a creator from WeChat (「接群」,
src/prm/group_link.py).  The WCDB backend lists the groups to choose from (``recent_groups``: the session list's group
chats, the one active last first, with their names, sizes and session times; by name too) and reads what was said in
the one chosen once (``group_lines``: its lines since a time, oldest first, standardized as a watched group's are, this
account's own lines marked ``is_own``), before PRM watches it from the second after the newest line it read.

The real WcdbBackend over the fake WeChat 4 database of tests/test_boss_groups_backend.py (session rows without names,
names from get_display_names, group members, messages newest first, this account's own lines from its wxid without the
data folder's suffix).  Every name and id is made up.
"""

import logging

import pytest

from test_boss_groups_backend import (BOSS, BOSS_NAME, GROUP, GROUP_NAME, HER, HER_NAME, NOTICE_GROUP, NOTICE_NAME,
                                      OLD_GROUP, TESTER, GroupDb, Run)
from test_new_friends import ME, ME_SENDER, Clock

from src.wechat import wcdb_backend
from src.wechat.wcdb_backend import CATCHUP_READ
from src.wechat.wcdb_client import WcdbCallError

BD = "wxid_bd00000000000001"                # her agent (商务), who may be the one who talks
BD_NAME = "测试商务"
BIG = "1000000007@chatroom"                 # a big group of the brand's
NAMELESS = "1000000008@chatroom"            # a group nobody named


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(clock):
    d = GroupDb(clock)
    d.names.update({BOSS: BOSS_NAME, HER: HER_NAME, BD: BD_NAME, ME_SENDER: "商务号测试"})
    t = clock.now
    d.group(OLD_GROUP, "公司老群", [BOSS, ME_SENDER], t - 30 * 86400)
    d.group(NOTICE_GROUP, NOTICE_NAME, [BOSS, TESTER, ME_SENDER], t - 20 * 86400)
    return d


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # data/group_names.json, data/group_members.json …
    monkeypatch.setattr(wcdb_backend, "VERIFY_PAUSE_SEC", 0)


# ── recent_groups: the groups to choose from ─────────────────────────────────

def test_recent_groups_are_the_session_lists_groups_the_one_active_last_first(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [HER, BD, ME_SENDER], t - 3600)
    db.line(GROUP, HER, "这款还有吗", t - 60)                            # active last
    db.group(BIG, "品牌测试大群", [ME_SENDER] + [f"wxid_member{i:010d}" for i in range(24)], t - 600)
    db.group(NAMELESS, "", [HER, ME_SENDER], t - 1200)
    db.say(HER, HER, "私聊的话", t - 30)                                  # a private chat: never a group
    run = Run(tmp_path, db)
    assert run.backend.recent_groups() == [
        {"chat": GROUP, "name": GROUP_NAME, "size": 3, "last_at": float(int(t - 60))},
        {"chat": BIG, "name": "品牌测试大群", "size": 25, "last_at": float(int(t - 600))},
        {"chat": NAMELESS, "name": "", "size": 2, "last_at": float(int(t - 1200))},
        {"chat": NOTICE_GROUP, "name": NOTICE_NAME, "size": 3, "last_at": float(int(t - 20 * 86400))},
        {"chat": OLD_GROUP, "name": "公司老群", "size": 2, "last_at": float(int(t - 30 * 86400))}]
    assert [g["chat"] for g in run.backend.recent_groups(2)] == [GROUP, BIG]
    assert [g["chat"] for g in run.backend.recent_groups(2, skip={GROUP})] == [BIG, NAMELESS]   # (linked already)
    assert [g["chat"] for g in run.backend.recent_groups(0, skip=[GROUP, BIG])] == [NAMELESS, NOTICE_GROUP, OLD_GROUP]


def test_recent_groups_by_name_whenever_they_were_active_spaces_and_case_aside(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, "Mei 合作群", [HER, ME_SENDER], t - 60)
    twin = "1000000009@chatroom"
    db.group(twin, "mei合作群", [HER, BD, ME_SENDER], t - 90 * 86400)        # long ago, the same name all the same
    run = Run(tmp_path, db)
    assert [g["chat"] for g in run.backend.recent_groups(10, name=" MEI 合作群 ")] == [GROUP, twin]
    assert [g["chat"] for g in run.backend.recent_groups(10, skip={GROUP}, name="Mei合作群")] == [twin]
    assert run.backend.recent_groups(10, name="没有这个群") == []
    assert [g["chat"] for g in run.backend.recent_groups(1, name="mei合作群")] == [GROUP]


def test_recent_groups_a_groups_members_not_readable_has_no_size_and_none_while_the_database_cannot_be_read(
        tmp_path, db, clock):
    db.group(GROUP, GROUP_NAME, [HER, ME_SENDER], clock.now - 60)
    run = Run(tmp_path, db)
    real = db.get_group_members

    def members(chat):
        if chat == GROUP:
            raise WcdbCallError("wcdb_get_group_members", -3)
        return real(chat)

    db.get_group_members = members                                       # an error code for this group only
    listed = run.backend.recent_groups()
    assert listed[0] == {"chat": GROUP, "name": GROUP_NAME, "size": None, "last_at": float(int(clock.now - 60))}
    assert [g["size"] for g in listed[1:]] == [3, 2]
    db.fail = True
    assert run.backend.recent_groups() is None
    db.fail = False
    run.backend._client = None                                           # the database is not open
    assert run.backend.recent_groups() is None


def test_recent_groups_takes_a_sessions_last_line_before_its_order_key(tmp_path, db, clock):
    """The order is when each group's last line was written (``last_timestamp``), not WeChat's sort key (a draft may
    move it); milliseconds read as seconds."""
    t = clock.now
    db.group(GROUP, GROUP_NAME, [HER, ME_SENDER], t - 600)
    db.group(BIG, "品牌测试大群", [BD, ME_SENDER], t - 300)
    real = db.get_sessions

    def sessions(limit=500):
        rows = real(limit)
        for row in rows:
            if row["username"] == GROUP:
                row.update(last_timestamp=int(t - 10) * 1000)                # its last line: 10 s ago (ms)
            if row["username"] == BIG:
                row.update(sort_timestamp=t + 3600, last_timestamp=t - 300)  # a draft moved its sort key
        return rows

    db.get_sessions = sessions
    assert [(g["chat"], g["last_at"]) for g in Run(tmp_path, db).backend.recent_groups(2)] == [
        (GROUP, float(int(t - 10))), (BIG, float(t - 300))]


def test_recent_groups_logs_no_name(tmp_path, db, clock, caplog):
    db.group(GROUP, GROUP_NAME, [HER, ME_SENDER], clock.now - 60)
    caplog.set_level(logging.DEBUG)
    Run(tmp_path, db).backend.recent_groups(name=GROUP_NAME)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "接手的群" in text and GROUP_NAME not in text and HER_NAME not in text and GROUP not in text


# ── group_lines: what was said in the group chosen ───────────────────────────

def test_group_lines_are_the_lines_since_a_time_oldest_first_ours_marked(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [HER, BD, ME_SENDER], t - 7200)
    db.line(GROUP, HER, "上周说的事", t - 5000)                            # before ``since``
    db.line(GROUP, HER, "这款还有吗", t - 600)
    db.line(GROUP, ME_SENDER, "有的，我看一下", t - 500)                   # ours: by the wxid alone
    db.line(GROUP, BD, "谢谢邀请，这是我们家达人", t - 400)                   # 「邀请」: kept (no keyword filter)
    db.line(GROUP, HER, "那寄样怎么弄", t - 300)
    run = Run(tmp_path, db)                                              # (the group is not read, not watched)
    lines = run.backend.group_lines(GROUP, t - 600)
    assert [(m["sender_id"], m["content"], m.get("is_own", False)) for m in lines] == [
        (HER, "这款还有吗", False), (ME_SENDER, "有的，我看一下", True), (BD, "谢谢邀请，这是我们家达人", False),
        (HER, "那寄样怎么弄", False)]
    hers = lines[0]
    assert {k: hers[k] for k in ("chat_id", "group_name", "is_group", "sender_name", "msg_type", "timestamp")} == {
        "chat_id": GROUP, "group_name": GROUP_NAME, "is_group": True, "sender_name": HER_NAME, "msg_type": 1,
        "timestamp": int(t - 600)}
    assert hers["message_id"] and hers["_not_self_by_id"] is True and "_not_self_by_id" not in lines[1]
    assert [m["content"] for m in run.backend.group_lines(GROUP, t - 400)] == ["谢谢邀请，这是我们家达人", "那寄样怎么弄"]
    assert run.backend.group_lines(GROUP, t + 1) == []


def test_group_lines_have_the_ids_the_watch_gives_and_mark_nothing_seen(tmp_path, db, clock):
    """The same ids as the watch hands on (PRM tells a line once by its id), and nothing is marked seen or handed on:
    a watch from the same time hands every line on all the same."""
    t = clock.now
    db.group(GROUP, GROUP_NAME, [HER, ME_SENDER], t - 900)
    db.line(GROUP, HER, "在吗", t - 200)
    db.line(GROUP, ME_SENDER, "在的", t - 100)
    run = Run(tmp_path, db).start()
    read = run.backend.group_lines(GROUP, t - 300)
    run.backend.watch_group(GROUP, since=t - 300)
    run.poll()
    assert run.lines() == [(HER, "在吗")] and [m["content"] for m in run.own] == ["在的"]
    assert [m["message_id"] for m in read] == [run.routed[-1]["message_id"], run.own[-1]["message_id"]]


def test_group_lines_has_wechats_lines_and_cuts_a_custom_ids_prefix(tmp_path, db, clock):
    custom = "meimei_88"
    t = clock.now
    db.group(GROUP, GROUP_NAME, [BOSS, custom, ME_SENDER], t - 60)        # WeChat's 「邀请你加入了群聊」
    db.line(GROUP, custom, "在的", t - 30)
    lines = Run(tmp_path, db).backend.group_lines(GROUP, t - 60)
    assert [(m["sender_id"], m["msg_type"], m["content"] if m["msg_type"] == 1 else m["special"].get("title"))
            for m in lines] == [("", 10000, f'"{BOSS_NAME}"邀请你加入了群聊'), (custom, 1, "在的")]
    assert not lines[0].get("is_own")                                    # (no sender: nobody's)


def test_group_lines_recognize_only_the_voices_asked_for(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [HER, BD, ME_SENDER], t - 60)
    db.say(GROUP, HER, "", t - 30, local_type=34)
    db.say(GROUP, BD, "", t - 20, local_type=34)
    run = Run(tmp_path, db)
    heard = []

    def voice(msg, talker=""):
        heard.append(msg["sender_username"])
        return "这款多少钱"

    run.backend._try_voice = voice
    lines = run.backend.group_lines(GROUP, t - 30, voices=(HER,))
    assert [m["content"] for m in lines] == ["[语音] 这款多少钱", "[语音]"] and heard == [HER]
    assert lines[0]["message_id"] == run.backend.group_lines(GROUP, t - 30)[0]["message_id"]   # (as the watch's)


def test_group_lines_reads_the_latest_rows_only(tmp_path, db, clock):
    t = clock.now
    db.group(GROUP, GROUP_NAME, [HER, ME_SENDER], t - 9000)
    for n in range(CATCHUP_READ + 5):
        db.line(GROUP, HER, f"第{n}句", t - 8000 + n)
    lines = Run(tmp_path, db).backend.group_lines(GROUP, 0)
    assert len(lines) == CATCHUP_READ and lines[-1]["content"] == f"第{CATCHUP_READ + 4}句"


def test_group_lines_none_when_they_cannot_be_read(tmp_path, db, clock):
    db.group(GROUP, GROUP_NAME, [HER, ME_SENDER], clock.now - 60)
    run = Run(tmp_path, db)
    assert run.backend.group_lines(HER, 0) is None                       # no group
    real = db.get_messages

    def broken(talker, limit=200, offset=0):
        if talker == GROUP:
            raise WcdbCallError("wcdb_get_messages", -3)
        return real(talker, limit, offset)

    db.get_messages = broken
    assert run.backend.group_lines(GROUP, 0) is None                     # an error code
    db.get_messages = real
    db._config = {}                                                      # this account's wxid not known yet
    assert run.backend.group_lines(GROUP, 0) is None
    db._config = {"myWxid": ME}
    run.backend._client = None                                           # the database is not open
    assert run.backend.group_lines(GROUP, 0) is None


def test_group_lines_logs_the_ids_first_characters_and_a_count_only(tmp_path, db, clock, caplog):
    db.group(GROUP, GROUP_NAME, [HER, ME_SENDER], clock.now - 60)
    db.line(GROUP, HER, "我的电话 13800000001", clock.now - 30)
    caplog.set_level(logging.DEBUG)
    Run(tmp_path, db).backend.group_lines(GROUP, 0)
    text = "\n".join(r.getMessage() for r in caplog.records if "接手的群" in r.getMessage())
    assert f"{GROUP[:8]}…: 2 line(s)" in text
    for secret in (GROUP_NAME, HER_NAME, HER, "13800000001", GROUP):
        assert secret not in text, secret


def test_a_standardized_line_is_as_before_unless_it_is_read_as_a_watched_groups():
    """``_standardize``'s ``watched``: None (every caller before) reads by the watch; True reads a group as watched
    before it is (``group_lines``): a member's words with 「邀请」 are not dropped then."""
    from src.wechat.wcdb_backend import WcdbBackend

    db = GroupDb(Clock())
    db.names[HER] = HER_NAME
    be = WcdbBackend(bot_display_name="商务助手", groups=[], poll_sec=0)
    be._client = db
    row = {"sender_username": HER, "message_content": f"{HER}:\n谢谢邀请～", "local_type": 1, "create_time": 1,
           "local_id": 1}
    assert be._standardize(row, GROUP_NAME, GROUP, is_group=True) is None              # the keyword filter, as before
    assert be._standardize(row, GROUP_NAME, GROUP, is_group=True, watched=None) is None
    assert be._standardize(row, GROUP_NAME, GROUP, is_group=True, watched=True)["content"] == "谢谢邀请～"
