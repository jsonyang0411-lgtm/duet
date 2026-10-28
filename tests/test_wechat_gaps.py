"""电脑微信掉线提醒（2026-10-28）：记下电脑微信哪段时间不在线，控制台一直提醒到运营点「看过了」，微信回来以后再补交一次。

真机确认过：电脑微信完全退出期间达人发到手机的消息，重新登录后默认不会同步到电脑；扫码登录时在手机上勾选
「同步最近的聊天记录」才会同步过来，同步进来的行是新编号、旧时间（README 常见问题）。

- 掉线怎么认（src/wechat/presence.py ``probe``）：微信进程不在，马上算；找不到微信主窗口，连续两次才算；正在发消息
  （窗口被占着）这次不算；两次看之间微信进程重新启动过（升级）也算一段。数据库读不读得了不算（读的是文件，
  微信没登录也读得了）。
- 一段掉线：从最后一次确认在线，到再次确认在线；运行中掉线是 ``offline_while_running``。机器人和微信一起停了
  （重启电脑、升级微信）：启动时微信进程的启动时间比库里记的最后一次在线晚，补记一段到微信启动，
  ``stopped_with_bot``；只是机器人停了、微信一直登录着，不算。
- 微信回来以后：名单里 / 登记了的达人私聊每个往回读 200 条，掉线以后写的、这次运行没见过的按顺序补交；
  一小时内再有同步进来的旧行，读到就再补交一次（边缘带 written_at，不再被 5 分钟的规则当历史丢掉）。

真的 WcdbBackend（底下是按时间排序的假微信数据库）+ 真的 EdgeService / 路由、中转、PRM。
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_edge_catchup import Run as EdgeRun, Windows
from test_prm import CREATOR
from test_prm_catchup import ME, FakeWeChat, World, text_lines

from src.edge import protocol as P
from src.wechat.presence import (CHECK_SEC, LAST_ONLINE_WRITE_SEC, OFFLINE_CONFIRM, OFFLINE_WHILE_RUNNING,
                                 STOPPED_WITH_BOT, PresenceStore, WechatPresence, gap_for_console, gap_notice,
                                 probe)

HOUR = 3600


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Probe:
    """What the bot sees of WeChat, scripted: online / offline / no answer; its process and start time."""

    def __init__(self, started=1_799_990_000.0):
        self.online, self.process, self.started = True, True, started

    def __call__(self):
        if self.process is False:
            return {"online": False, "process": False, "started_at": None, "detail": "微信进程不在"}
        return {"online": self.online, "process": self.process, "started_at": self.started,
                "detail": {True: "微信在线", False: "找不到微信主窗口", None: "正在发消息"}[self.online]}


class Watch:
    def __init__(self, tmp_path, clock, probe, store=None):
        self.store = store or PresenceStore(str(tmp_path / "messages.db"))
        self.offline, self.back = [], []
        self.presence = WechatPresence(self.store, probe, clock=clock, on_offline=self.offline.append,
                                       on_back=self.back.append)

    def start(self):
        self.presence.start(run_thread=False)
        return self


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def seen():
    return Probe()


def step(clock, watch, sec=CHECK_SEC):
    clock.now += sec
    watch.presence.check()


# ── what is seen ────────────────────────────────────────────────────────────

def test_the_probe_reads_the_process_and_the_window():
    assert probe(lambda: True, lambda: []) == {"online": False, "process": False, "started_at": None,
                                                "detail": "微信进程不在（电脑微信没开或退出了）"}
    assert probe(lambda: True, lambda: [200.0, 100.0])["started_at"] == 100.0, "最早启动的那个进程"
    assert probe(lambda: True, lambda: None)["online"] is True and probe(lambda: True, lambda: None)["process"] is None
    assert probe(lambda: None, lambda: [1.0])["online"] is None, "正在发消息：这次不看"
    off = probe(lambda: False, lambda: [1.0])
    assert off["online"] is False and off["process"] is True and "主窗口" in off["detail"]
    assert probe(Mock(side_effect=OSError("gone")), lambda: [1.0])["online"] is False


# ── while the bot runs ──────────────────────────────────────────────────────

def test_a_missing_window_twice_is_a_gap_from_the_last_time_it_was_online(tmp_path, clock, seen):
    watch = Watch(tmp_path, clock, seen).start()
    step(clock, watch)
    online_at = clock.now
    seen.online = False
    step(clock, watch)
    assert watch.offline == [] and watch.store.open_gap() is None, "一次找不到不算（可能刚好在切窗口）"
    step(clock, watch)
    [gap] = watch.offline
    assert (gap["from"], gap["to"], gap["reason"]) == (online_at, None, OFFLINE_WHILE_RUNNING)
    assert "主窗口" in gap["detail"] and OFFLINE_CONFIRM == 2
    step(clock, watch)
    seen.online = True
    step(clock, watch)
    [back] = watch.back
    assert (back["id"], back["from"], back["to"]) == (gap["id"], online_at, clock.now)
    assert watch.store.open_gap() is None and [g["id"] for g in watch.store.unseen()] == [gap["id"]]


def test_no_wechat_process_is_offline_at_once(tmp_path, clock, seen):
    watch = Watch(tmp_path, clock, seen).start()
    online_at = clock.now
    seen.process = False
    step(clock, watch)
    [gap] = watch.offline
    assert gap["from"] == online_at and "进程" in gap["detail"]


def test_a_busy_window_is_no_answer(tmp_path, clock, seen):
    watch = Watch(tmp_path, clock, seen).start()
    seen.online = False
    step(clock, watch)
    seen.online = None
    for _ in range(3):
        step(clock, watch)
    assert watch.offline == [], "发消息占着窗口时问不到，不算掉线"
    seen.online = False
    step(clock, watch)
    assert len(watch.offline) == 1, "接着又找不到：两次了"


def test_wechat_restarted_between_two_probes_is_a_gap(tmp_path, clock, seen):
    """升级时微信几秒就重新启动了，两次看之间没看到它不在：进程的启动时间变了，照样记一段。"""
    watch = Watch(tmp_path, clock, seen).start()
    online_at = clock.now
    seen.started = clock.now + 10
    step(clock, watch)
    [gap] = watch.back
    assert (gap["from"], gap["to"], gap["reason"]) == (online_at, clock.now, OFFLINE_WHILE_RUNNING)
    assert watch.offline == []
    step(clock, watch)
    assert len(watch.back) == 1


def test_the_last_time_wechat_was_online_is_written_down_while_it_is(tmp_path, clock, seen):
    watch = Watch(tmp_path, clock, seen).start()
    assert watch.store.last_online() == clock.now
    first = clock.now
    step(clock, watch, LAST_ONLINE_WRITE_SEC / 2)
    assert watch.store.last_online() == first, "不是每次都写"
    step(clock, watch, LAST_ONLINE_WRITE_SEC)
    assert watch.store.last_online() == clock.now
    seen.online = False
    written = clock.now
    step(clock, watch, LAST_ONLINE_WRITE_SEC * 3)
    assert watch.store.last_online() == written, "不在线时不写"


# ── at the bot's start ──────────────────────────────────────────────────────

def stopped(tmp_path, clock, seen, *, last_online):
    """The bot ran before: it last saw WeChat online at ``last_online``."""
    store = PresenceStore(str(tmp_path / "messages.db"))
    store.set_last_online(last_online)
    return Watch(tmp_path, clock, seen, store=store)


def test_wechat_restarted_while_the_bot_was_stopped_is_a_gap_to_its_start(tmp_path, clock, seen):
    """重启电脑、升级微信：机器人也停了。启动时微信进程比最后一次确认在线晚启动：补记一段，到微信启动。"""
    last = clock.now - 2 * HOUR
    seen.started = clock.now - 10 * 60
    watch = stopped(tmp_path, clock, seen, last_online=last).start()
    [gap] = watch.back
    assert (gap["from"], gap["to"], gap["reason"]) == (last, seen.started, STOPPED_WITH_BOT)
    assert watch.offline == [] and watch.store.open_gap() is None


def test_the_bot_alone_stopped_is_no_gap(tmp_path, clock, seen):
    """只是机器人停了、微信一直登录着：不算掉线（重启补交会处理）。"""
    seen.started = clock.now - 5 * HOUR
    watch = stopped(tmp_path, clock, seen, last_online=clock.now - 2 * HOUR).start()
    assert watch.back == watch.offline == [] and watch.store.unseen() == []


def test_wechat_not_running_at_the_start_is_a_gap_until_it_is_back(tmp_path, clock, seen):
    last = clock.now - HOUR
    seen.process = False
    watch = stopped(tmp_path, clock, seen, last_online=last).start()
    [gap] = watch.offline
    assert (gap["from"], gap["to"], gap["reason"]) == (last, None, STOPPED_WITH_BOT)
    step(clock, watch)
    assert len(watch.offline) == 1, "还没回来：同一段"
    seen.process, seen.started = True, clock.now + 5
    step(clock, watch)
    seen.online = True
    [back] = watch.back
    assert (back["id"], back["from"], back["to"]) == (gap["id"], last, clock.now)


def test_wechat_started_but_not_logged_in_waits_for_the_login(tmp_path, clock, seen):
    """电脑开了、微信进程起来了但还没登录（找不到主窗口）：这段一直开着，登录了才结束。"""
    last = clock.now - HOUR
    seen.started, seen.online = clock.now - 60, False
    watch = stopped(tmp_path, clock, seen, last_online=last).start()
    [gap] = watch.offline
    assert gap["reason"] == STOPPED_WITH_BOT and gap["to"] is None
    step(clock, watch)
    seen.online = True
    step(clock, watch)
    assert watch.back[0]["to"] == clock.now


def test_a_window_missing_at_the_start_needs_a_second_look(tmp_path, clock, seen):
    """微信进程早就在、启动时找不到主窗口（比如停机时被挤下线了）：再看一次还是没有才算，从最后一次确认在线算起。"""
    last = clock.now - HOUR
    seen.started, seen.online = clock.now - 5 * HOUR, False
    watch = stopped(tmp_path, clock, seen, last_online=last).start()
    assert watch.offline == []
    step(clock, watch)
    [gap] = watch.offline
    assert (gap["from"], gap["reason"]) == (last, STOPPED_WITH_BOT)


def test_a_gap_left_open_by_the_last_run_goes_on(tmp_path, clock, seen):
    store = PresenceStore(str(tmp_path / "messages.db"))
    store.set_last_online(clock.now - HOUR)
    old = store.add_gap(clock.now - HOUR, OFFLINE_WHILE_RUNNING, "找不到微信主窗口", clock.now - HOUR + 60)
    seen.online = False
    watch = Watch(tmp_path, clock, seen, store=store).start()
    assert [g["id"] for g in watch.offline] == [old["id"]], "机器人重启了，这段还在"
    seen.online = True
    step(clock, watch)
    assert watch.back[0]["id"] == old["id"] and store.unseen()[0]["to"] == clock.now


def test_the_first_run_has_nothing_to_compare_with(tmp_path, clock, seen):
    seen.process = False
    watch = Watch(tmp_path, clock, seen).start()
    assert watch.offline == [], "库里还没有最后一次在线的时间"
    step(clock, watch)
    [gap] = watch.offline
    assert gap["from"] == clock.now


def test_a_first_look_that_fails_does_not_stop_the_bot(tmp_path, clock, seen):
    watch = Watch(tmp_path, clock, seen)
    watch.store.last_online = Mock(side_effect=[RuntimeError("database is locked")] + [None] * 5)
    watch.start()                                                    # logged, nothing raised
    seen.process = False
    step(clock, watch)
    assert len(watch.offline) == 1, "之后照常看"


# ── the console ─────────────────────────────────────────────────────────────

def test_a_gap_is_shown_until_an_operator_has_seen_it(tmp_path, clock, seen):
    watch = Watch(tmp_path, clock, seen).start()
    seen.process = False
    step(clock, watch)
    state = watch.presence.console()
    assert state["open"]["id"] == watch.offline[0]["id"] and state["gaps"] == []
    assert watch.presence.mark_seen(state["open"]["id"]) is False, "还没恢复的那段不能点掉"
    seen.process = True
    step(clock, watch)
    [gap] = watch.presence.console()["gaps"]
    assert watch.presence.console()["open"] is None
    assert watch.presence.mark_seen(gap["id"]) is True and watch.presence.mark_seen(gap["id"]) is False
    assert watch.presence.console() == {"open": None, "gaps": []}
    watch.store.prune(clock.now + 1)
    assert watch.store.gap(gap["id"]) is None, "看过的过了期限删掉"


def test_what_the_operators_read():
    at = time.mktime((2026, 10, 28, 14, 5, 0, 0, 0, -1))
    gap = {"id": 3, "from": at, "to": at + 27 * 60, "reason": OFFLINE_WHILE_RUNNING}
    notice = gap_notice(gap)
    assert notice["title"] == "电脑微信 10-28 14:05 到 14:32 不在线（约 27 分钟）"
    for words in ("掉线", "可能没收到", "手机微信上把达人的私聊看一遍", "同步最近的聊天记录"):
        assert words in notice["text"]
    other_day = gap_notice({**gap, "to": at + 26 * HOUR, "reason": STOPPED_WITH_BOT})
    assert other_day["title"] == "电脑微信 10-28 14:05 到 10-29 16:05 不在线（约 26 小时）"
    assert "重启了电脑" in other_day["text"] and "同步最近的聊天记录" in other_day["text"]
    going_on = gap_notice({**gap, "to": None})
    assert going_on["title"] == "电脑微信从 10-28 14:05 起不在线，还没恢复"
    assert "收不到" in going_on["text"] and "同步最近的聊天记录" in going_on["text"]
    assert gap_for_console(gap)["id"] == 3 and gap_for_console(gap)["title"] == notice["title"]


# ── after a gap: the creators' chats are read back ─────────────────────────

class SyncingWeChat(FakeWeChat):
    """WCDB lists a chat by time, newest first: rows WeChat brings in after logging in again (synced from
    the phone: new local ids, their old times) sit among the old ones."""

    def get_messages(self, talker, limit=50):
        rows = sorted(self.chats.get(talker, []), key=lambda r: (r["create_time"], r["local_id"]), reverse=True)
        return rows[:limit]


@pytest.fixture
def win(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = Windows(tmp_path)
    w.wechat = SyncingWeChat()
    return w


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = World(tmp_path)
    w.wechat = SyncingWeChat()
    yield w
    w.stop()


def test_rows_synced_in_after_a_gap_reach_the_mac_with_when_they_were_written(win):
    """以前：同步进来的行是新编号、旧时间，边缘按 5 分钟的规则当历史丢掉，Mac 永远收不到。"""
    run = EdgeRun(win)
    run.poll()
    gap_from = int(time.time()) - 2 * HOUR
    run.backend.note_wechat_offline(gap_from)
    run.backend.catch_up_after_gap(gap_from)
    run.poll()                                                    # 回来了，往回读：还没同步进来
    # 扫码登录时勾选了「同步最近的聊天记录」：这段时间的消息同步进来
    win.wechat.say(CREATOR, CREATOR, "在吗", gap_from + 600)
    win.wechat.say(CREATOR, ME, "手机上回的：稍等", gap_from + 900)
    win.wechat.say(CREATOR, CREATOR, "地址发你了", gap_from + 1200)
    run.poll()
    run.poll()
    creator = win.events(P.CREATOR_MESSAGE)
    assert [(e["text"], e["written_at"]) for e in creator] == [("在吗", gap_from + 600), ("地址发你了", gap_from + 1200)]
    [operator] = win.events(P.OPERATOR_MESSAGE)
    assert (operator["text"], operator["written_at"]) == ("手机上回的：稍等", gap_from + 900)
    run.stop()


def test_rows_synced_while_the_gap_is_not_over_yet_are_handed_on_too(win):
    """微信已经登录、同步进来了，机器人还没确认它在线（每 30 秒看一次）：照样交。"""
    run = EdgeRun(win)
    run.poll()
    gap_from = int(time.time()) - HOUR
    run.backend.note_wechat_offline(gap_from)
    win.wechat.say(CREATOR, CREATOR, "在吗", gap_from + 300)
    run.poll()
    assert [e["written_at"] for e in win.events(P.CREATOR_MESSAGE)] == [gap_from + 300]
    run.stop()


def test_more_rows_than_one_read_go_oldest_first(win):
    """一次同步进来 60 条（平时一轮只读最新 50 条）：往回读 200 条，按时间顺序全交。"""
    run = EdgeRun(win)
    run.poll()
    gap_from = int(time.time()) - 3 * HOUR
    run.backend.catch_up_after_gap(gap_from)
    for n in range(60):
        win.wechat.say(CREATOR, CREATOR, f"第{n}条", gap_from + 60 + n * 10)
    run.poll()
    assert win.lines(P.CREATOR_MESSAGE) == [f"第{n}条" for n in range(60)]
    run.poll()
    assert len(win.events(P.CREATOR_MESSAGE)) == 60
    run.stop()


def test_without_a_gap_an_old_row_is_still_history(win):
    run = EdgeRun(win)
    run.poll()
    win.wechat.say(CREATOR, CREATOR, "很久以前的", int(time.time()) - 2 * HOUR)
    run.poll()
    assert win.events(P.CREATOR_MESSAGE) == []
    run.stop()


def test_with_catchup_hours_zero_nothing_is_handed_on_after_a_gap(win):
    run = EdgeRun(win, catchup_hours=0)
    run.poll()
    gap_from = int(time.time()) - HOUR
    run.backend.catch_up_after_gap(gap_from)
    win.wechat.say(CREATOR, CREATOR, "在吗", gap_from + 300)
    run.poll()
    assert win.events(P.CREATOR_MESSAGE) == []
    run.stop()


def test_the_copilot_drafts_every_row_synced_in_after_a_gap_once(world):
    """副驾驶：同步进来的行本来就会按晚到的起草（PRM_CATCHUP_HOURS），可一轮只读最新 50 条；回来以后往回读 200 条，
    60 条按顺序进同一份先审的草稿，读过的不再交。"""
    world.register()
    run = world.run()
    run.poll()
    gap_from = int(time.time()) - 2 * HOUR
    run.backend.catch_up_after_gap(gap_from)
    for n in range(60):
        world.wechat.say(CREATOR, CREATOR, f"第{n}条", gap_from + 60 + n * 10)
    run.poll()
    run.poll()
    [job] = world.jobs()
    assert text_lines(job) == [f"第{n}条" for n in range(60)]
    assert json.loads(job["meta"])["late"]["from"] == gap_from + 60
    assert world.stored(CREATOR) == 60


# ── how the bot wires it ────────────────────────────────────────────────────

def test_the_bot_wires_the_watch_to_the_edge_and_the_backend(tmp_path, seen):
    from src.bot import Bot
    from src.web import server

    config = SimpleNamespace(db_path=str(tmp_path / "messages.db"))
    assert Bot._start_presence(config, SimpleNamespace(), None) is None, "不能看电脑微信的后端不看"
    backend = SimpleNamespace(wechat_presence=seen, note_wechat_offline=Mock(), catch_up_after_gap=Mock())
    edge = SimpleNamespace(wechat_gap=Mock())
    presence = Bot._start_presence(config, backend, edge)
    try:
        assert server._presence is presence
        seen.process = False
        presence.check()
        [gap] = presence.store.unseen() or [presence.store.open_gap()]
        backend.note_wechat_offline.assert_called_once_with(gap["from"])
        seen.process = True
        presence.check()
        [reported] = edge.wechat_gap.call_args.args
        assert (reported["id"], reported["reason"]) == (gap["id"], OFFLINE_WHILE_RUNNING) and reported["to"]
        backend.catch_up_after_gap.assert_called_once_with(gap["from"])
    finally:
        presence.stop()
        server.register_presence(None)


def test_the_windows_backend_only_looks_at_wechat_on_windows(monkeypatch):
    from src.config import BotConfig
    from src.wechat import presence as presence_module
    from src.wechat import wcdb_backend
    from src.wechat.wcdb_backend import WcdbBackend

    backend = WcdbBackend(bot_display_name="机器人", groups=["*"], config=BotConfig())
    assert backend.wechat_presence()["online"] is None
    monkeypatch.setattr(wcdb_backend.os, "name", "nt")
    monkeypatch.setattr(presence_module, "wechat_start_times", lambda: [])
    backend._window = SimpleNamespace(find_hwnd=lambda wake=True: 123)
    monkeypatch.setattr(presence_module, "probe", lambda found, start_times=presence_module.wechat_start_times:
                        {"online": found(), "process": True, "started_at": None, "detail": ""})
    assert backend.wechat_presence()["online"] is True
