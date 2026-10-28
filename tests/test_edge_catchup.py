"""Windows 边缘模式（PRM_EDGE）重启补交：机器人停着的时候达人发的消息、运营在手机上回的话。

原来 Windows 重启时把微信里已有的消息都当历史，停机超过 5 分钟期间的话 Mac 永远收不到
（2026-10-28 同事审查）。现在和副驾驶模式一样按 PRM_CATCHUP_HOURS 补交，只是「哪些没转过」
按 Windows 自己的 edge.db 算：这个私聊最后一次转出事件、或者我们最后一次在这里发出/读回的时间
（EdgeStore.last_activity），比它晚写的都转给 Mac，带 written_at（写的时间）；同一条消息编号只转一次。
Mac 收到后照 PRM 的规则办：PRM_CATCHUP_HOURS 以内的起草（一律先审），更早的只记录、提醒运营。

真的 WcdbBackend（底下是假的微信数据库，test_prm_catchup.FakeWeChat）+ 真的 EdgeService，
按 Bot._start_edge 的接法接起来；重启时留下的是微信的消息和 edge.db。
"""

import time

import pytest

from test_prm_catchup import CREATOR, GROUP, ME, STRANGER, FakeWeChat

from src.config import BotConfig
from src.db import MessageStore, initialize_db
from src.edge import protocol as P
from src.edge.service import EdgeService
from src.edge.store import EdgeStore
from src.wechat.edge_backend import event_to_message
from src.wechat.wcdb_backend import WcdbBackend

HOUR = 3600
BOT = "机器人"


class Windows:
    """重启以后还在的：微信、机器人的聊天记录库、edge.db。"""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.now = float(int(time.time()))
        self.wechat = FakeWeChat()
        self.messages = MessageStore(initialize_db(":memory:"))
        self.edge_store = EdgeStore(":memory:")
        self.sent = []

    def clock(self):
        return self.now

    def events(self, *kinds):
        return [e for e in self.edge_store.events_after(0, 500) if not kinds or e["type"] in kinds]

    def lines(self, *kinds):
        return [e.get("text") for e in self.events(*kinds)]


class Run:
    """一次 Windows 上的运行：微信后端读库，名单里达人的话交给边缘（路由的 edge 这一步）。"""

    def __init__(self, win, catchup_hours=12):
        self.win = win
        config = BotConfig(private_chat_enabled=True, private_chats="wxid_admin", bot_display_name=BOT,
                           prm_edge="on", prm_edge_token="e" * 40, prm_catchup_hours=catchup_hours,
                           edge_send_min_gap_sec=0)
        self.edge = EdgeService(config, store=win.edge_store, clock=win.clock, files_dir=win.tmp / "edge",
                                send_text=self._send_text, own_wxid=lambda: ME)
        self.edge.start(run_thread=False)
        if not win.edge_store.parties():
            self.edge.put_parties(1, [CREATOR])
        self.backend = WcdbBackend(bot_display_name=BOT, groups=["*"], store=win.messages, config=config)
        self.backend._client = win.wechat
        self.backend.set_private_filter(self.edge.is_party_chat)
        self.backend.set_catchup_floor(self.edge.catchup_floor)
        self.backend.set_own_message_hook(self.edge.on_own_message)
        self.backend._resolve_groups()
        self.backend._prime_known_ids()
        self.backend._running = True

    def _send_text(self, chat_id, text):
        self.win.wechat.say(chat_id, ME, text, self.win.now)           # 微信里马上就有这一行
        self.win.sent.append((chat_id, text))
        return True

    def _route(self, msg):
        self.edge.handle(msg)                                          # 路由的 edge 这一步，不回复
        return None

    def poll(self):
        for name, chat in list(self.backend._talker_ids.items()):
            self.backend._poll_group(name, chat, self._route)
        for msg in self.backend._burst.flush_all():
            self.backend._dispatch_message(msg, self._route)

    def stop(self):
        self.backend._running = False
        self.edge.stop()


@pytest.fixture
def win(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # data/group_names.json and friends go here
    return Windows(tmp_path)


def test_what_came_in_while_windows_was_stopped_is_handed_on_once_with_when_it_was_written(win):
    first = Run(win)
    win.wechat.say(CREATOR, CREATOR, "你好", win.now)
    first.poll()
    assert win.lines(P.CREATOR_MESSAGE) == ["你好"]
    first.stop()

    win.now += 2 * HOUR                                                # 停了两个小时
    win.wechat.say(CREATOR, CREATOR, "在吗", win.now - 90 * 60)
    win.wechat.say(CREATOR, ME, "稍等，我看一下", win.now - 80 * 60)    # 运营在手机上回的
    win.wechat.say(CREATOR, CREATOR, "地址：东莞市滨海湾新区…", win.now - 30 * 60)
    win.wechat.say(STRANGER, STRANGER, "陌生人的", win.now - 60 * 60)
    win.wechat.say(GROUP, "wxid_member", "群里的", win.now - 60 * 60)

    again = Run(win)
    again.poll()
    again.poll()                                                       # 再读一遍也不重复
    creator = win.events(P.CREATOR_MESSAGE)
    assert [e["text"] for e in creator] == ["你好", "在吗", "地址：东莞市滨海湾新区…"]
    assert "written_at" not in creator[0], "按时读到的不带"
    assert [e["written_at"] for e in creator[1:]] == [int(win.now - 90 * 60), int(win.now - 30 * 60)]
    [operator] = win.events(P.OPERATOR_MESSAGE)
    assert operator["text"] == "稍等，我看一下" and operator["written_at"] == int(win.now - 80 * 60)
    assert event_to_message(creator[1], [])["timestamp"] == int(win.now - 90 * 60), "Mac 按写的时间算晚了多久"
    again.stop()


def test_our_line_read_back_before_the_stop_is_not_an_operators_after_it(win):
    first = Run(win)
    win.wechat.say(CREATOR, CREATOR, "你好", win.now)
    first.poll()
    assert first.edge.send({"send_id": "pk-1", "chat_id": CREATOR, "text": "老师好～", "uploads": [],
                            "ttl_sec": 600})["status"] == P.QUEUED
    first.edge.tick()
    first.poll()                                                       # 读回来了
    assert win.sent == [(CREATOR, "老师好～")] and win.events(P.SEND_SEEN)
    first.stop()

    win.now += HOUR
    win.wechat.say(CREATOR, CREATOR, "收到", win.now - 10 * 60)
    again = Run(win)
    again.poll()
    assert win.lines(P.CREATOR_MESSAGE) == ["你好", "收到"]
    assert win.events(P.OPERATOR_MESSAGE) == [], "停机前就读回过的那句是我们发的"
    again.stop()


def test_a_chat_the_edge_kept_nothing_of_stays_history(win):
    win.wechat.say(CREATOR, CREATOR, "很早以前的", win.now - 3 * HOUR)
    run = Run(win)
    run.poll()
    assert win.events(P.CREATOR_MESSAGE) == []
    win.wechat.say(CREATOR, CREATOR, "新的", win.now)
    run.poll()
    assert win.lines(P.CREATOR_MESSAGE) == ["新的"]
    run.stop()


def test_with_catchup_hours_zero_the_gap_stays_history(win):
    first = Run(win, catchup_hours=0)
    win.wechat.say(CREATOR, CREATOR, "你好", win.now)
    first.poll()
    first.stop()
    win.now += HOUR
    win.wechat.say(CREATOR, CREATOR, "在吗", win.now - 30 * 60)
    again = Run(win, catchup_hours=0)
    again.poll()
    assert win.lines(P.CREATOR_MESSAGE) == ["你好"]
    again.stop()
