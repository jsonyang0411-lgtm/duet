"""Claude relay, Windows side (src/relay): commands, queue, receipts, delivery, HTTP, route step."""

import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.relay import commands as C
from src.relay.service import (
    LOST_AFTER_SEC, MAX_AGE_SEC, ONLINE_WINDOW_SEC, RETRY_SEND_SEC, ClaudeRelay, format_duration,
)
from src.relay.store import RelayStore
from src.routing import steps as S
from src.routing.context import RouteContext, Stop

ME = "wxid_me"
TOKEN = "t" * 32


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def config(**overrides):
    values = dict(
        claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1",
        claude_relay_port=0, claude_relay_prefix="cc", claude_relay_wxids=[],
        claude_relay_db=":memory:", claude_relay_max_chars=1500, admin_wxid=ME,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sent():
    return []


@pytest.fixture
def relay(clock, sent):
    def send(chat_id, text):
        sent.append((chat_id, text))
        return True
    r = ClaudeRelay(config(), store=RelayStore(":memory:"), send=send, clock=clock)
    yield r
    r.stop()


def pm(content, *, sender=ME, ts=None, group=False, clock=None):
    return {
        "message_id": "m", "chat_id": sender, "sender_id": sender, "sender_name": "我",
        "content": content, "is_group": group,
        "timestamp": ts if ts is not None else (clock.now if clock else 0),
    }


# ── commands ──────────────────────────────────────────────────────────────

class TestCommands:
    @pytest.mark.parametrize("text,body", [
        ("cc 跑一下测试", "跑一下测试"), ("CC：跑一下测试", "跑一下测试"), ("/cc 跑", "跑"),
        ("  cc,跑", "跑"), ("cc", ""), ("cc\n第一行\n第二行", "第一行\n第二行"),
    ])
    def test_the_prefix_starts_a_relay_message(self, text, body):
        m = C.prefix_pattern("cc").match(text)
        assert m and m.group(1).strip() == body

    @pytest.mark.parametrize("text", ["ccc 跑", "cctv 节目", "accc", "你好 cc 跑", "c c 跑"])
    def test_other_text_is_not_a_relay_message(self, text):
        assert C.prefix_pattern("cc").match(text) is None

    def test_control_words_and_tasks(self):
        assert C.parse("").kind == C.parse("帮助").kind == "help"
        assert C.parse("状态").kind == "status"
        assert C.parse("新对话").kind == "reset"
        assert C.parse("取消") == C.RelayCommand("cancel")
        assert C.parse("取消 #17") == C.RelayCommand("cancel", job_id=17)
        assert C.parse("重试17") == C.RelayCommand("retry", job_id=17)
        assert C.parse("重试") == C.RelayCommand("retry")
        assert C.parse("取消一下那个提交") == C.RelayCommand("task", text="取消一下那个提交")
        assert C.parse("@douyin 跑 测试") == C.RelayCommand("task", text="跑 测试", project="douyin")
        assert C.parse("@ 不是项目") == C.RelayCommand("task", text="@ 不是项目")


# ── the queue ─────────────────────────────────────────────────────────────

class TestStore:
    def test_jobs_go_out_oldest_first_and_continue_the_chats_session(self):
        store = RelayStore(":memory:")
        a = store.add("chat", ME, "一", now=1)
        b = store.add("chat", ME, "二", now=2)
        first = store.claim_next(now=3)
        assert first["id"] == a and first["session_id"] is None
        assert store.finish(a, ok=True, text="好了", session_id="s1", now=4) is True
        second = store.claim_next(now=5)
        assert second["id"] == b and second["session_id"] == "s1"   # looked up when claimed
        assert store.claim_next(now=6) is None

    def test_a_result_is_taken_once_and_unknown_jobs_are_none(self):
        store = RelayStore(":memory:")
        job = store.add("chat", ME, "x")
        store.claim_next()
        assert store.finish(job, ok=True, text="a") is True
        assert store.finish(job, ok=True, text="b") is False
        assert store.get(job)["result_text"] == "a"
        assert store.finish(999, ok=True, text="") is None

    def test_a_silent_job_is_lost_and_a_heartbeat_or_result_brings_it_back(self):
        store = RelayStore(":memory:")
        job = store.add("chat", ME, "x", now=0)
        store.claim_next(now=10)
        assert store.mark_lost(older_than=5) == []
        assert store.mark_lost(older_than=11) == [job]
        assert store.get(job)["status"] == "lost" and store.undelivered()[0]["id"] == job
        assert store.heartbeat(job, now=20) == {"cancel": False}
        assert store.get(job)["status"] == "running"
        store.mark_lost(older_than=30)
        assert store.finish(job, ok=True, text="还是做完了") is True
        assert store.get(job)["status"] == "done"

    def test_cancel_a_queued_job_at_once_and_a_running_one_through_the_heartbeat(self):
        store = RelayStore(":memory:")
        running = store.add("chat", ME, "a")
        queued = store.add("chat", ME, "b")
        store.claim_next()
        assert store.cancel(queued) == "cancelled"
        assert store.cancel(running) == "requested"
        assert store.heartbeat(running) == {"cancel": True}
        store.finish(running, ok=False, text="停了")
        assert store.get(running)["status"] == "cancelled"
        assert store.cancel(running) == "cancelled" and store.cancel(42) == "missing"

    def test_delivered_only_for_the_state_that_was_sent(self):
        store = RelayStore(":memory:")
        job = store.add("chat", ME, "x", now=0)
        store.claim_next(now=0)
        store.mark_lost(older_than=1)
        store.finish(job, ok=True, text="ok")        # done before "lost" went out
        store.mark_delivered(job, "lost")            # the lost notice must not hide the result
        assert [j["status"] for j in store.undelivered()] == ["done"]
        store.mark_delivered(job, "done")
        assert store.undelivered() == []

    def test_reset_session(self):
        store = RelayStore(":memory:")
        job = store.add("chat", ME, "x")
        store.claim_next()
        store.finish(job, ok=True, text="", session_id="s1")
        store.reset_session("chat")
        store.add("chat", ME, "y")
        assert store.claim_next()["session_id"] is None


# ── WeChat side ───────────────────────────────────────────────────────────

class TestHandle:
    def test_only_allowed_private_messages_with_the_prefix_are_taken(self, relay, clock):
        assert relay.handle(pm("你好", clock=clock)) == (False, None)
        assert relay.handle(pm("cc 跑", group=True, clock=clock)) == (False, None)
        assert relay.handle(pm("cc 跑", sender="wxid_other", clock=clock)) == (False, None)
        matched, reply = relay.handle(pm("cc 跑一下测试", clock=clock))
        assert matched and "#1" in reply

    def test_configured_wxids_replace_the_admin(self, clock):
        r = ClaudeRelay(config(claude_relay_wxids=["wxid_a"]), store=RelayStore(":memory:"), clock=clock)
        assert r.handle(pm("cc x", clock=clock))[0] is False
        assert r.handle(pm("cc x", sender="wxid_a", clock=clock))[0] is True

    def test_a_quoted_prefix_is_removed_first(self, relay, clock):
        assert relay.handle(pm("wxid_abc123: cc 跑", clock=clock))[0] is True

    def test_old_requests_replayed_at_startup_are_not_run(self, relay, clock):
        assert relay.handle(pm("cc 跑", ts=clock.now - MAX_AGE_SEC - 1)) == (True, None)
        assert relay._store.claim_next() is None

    def test_receipt_says_whether_the_mac_is_online_and_what_is_ahead(self, relay, clock):
        _, reply = relay.handle(pm("cc 一", clock=clock))
        assert reply.startswith("收到，已交给 Mac（#1）") and "不在线" in reply
        relay.next_job(0)                                 # the Mac asked: online, #1 running
        _, reply = relay.handle(pm("cc 二", clock=clock))
        assert "#2" in reply and "前面还有 1 个任务" in reply and "不在线" not in reply
        clock.now += ONLINE_WINDOW_SEC + 1
        assert "不在线" in relay.handle(pm("cc 三", clock=clock))[1]

    def test_status_cancel_retry_reset_help(self, relay, clock):
        relay.handle(pm("cc 第一个任务", clock=clock))
        relay.handle(pm("cc 第二个任务", clock=clock))
        relay.next_job(0)
        status = relay.handle(pm("cc 状态", clock=clock))[1]
        assert "Mac：在线" in status and "#2 排队中 · 第二个任务" in status and "#1 进行中" in status
        assert "还没开始，已取消" in relay.handle(pm("cc 取消", clock=clock))[1]          # the latest: #2
        assert "已通知 Mac 停下来" in relay.handle(pm("cc 取消 1", clock=clock))[1]
        assert "没有找到 #9" in relay.handle(pm("cc 取消 9", clock=clock))[1]
        assert "还在进行中" in relay.handle(pm("cc 重试 1", clock=clock))[1]
        reply = relay.handle(pm("cc 重试 2", clock=clock))[1]
        assert "#3，重试 #2" in reply and relay._store.get(3)["text"] == "第二个任务"
        assert "写编号" in relay.handle(pm("cc 重试", clock=clock))[1]
        assert "新对话" in relay.handle(pm("cc 新对话", clock=clock))[1]
        assert "cc 状态" in relay.handle(pm("cc", clock=clock))[1]

    def test_jobs_of_another_chat_are_not_found(self, clock):
        r = ClaudeRelay(config(claude_relay_wxids=[ME, "wxid_b"]), store=RelayStore(":memory:"), clock=clock)
        r.handle(pm("cc 我的", clock=clock))
        assert "没有找到 #1" in r.handle(pm("cc 取消 1", sender="wxid_b", clock=clock))[1]

    def test_a_store_error_is_a_short_reply(self, relay, clock):
        with patch.object(relay._store, "add", side_effect=RuntimeError("disk")):
            assert relay.handle(pm("cc 跑", clock=clock)) == (True, "转给 Mac 失败了，详情看 bot.log。")


# ── back to WeChat ────────────────────────────────────────────────────────

class TestDelivery:
    def test_a_result_is_sent_to_its_chat_once(self, relay, clock, sent):
        relay.handle(pm("cc 跑", clock=clock))
        job = relay.next_job(0)
        assert relay.finish(job["id"], {"ok": True, "text": "全过了", "duration_s": 252,
                                        "session_id": "s1"}) is True
        relay.tick()
        relay.tick()
        assert sent == [(ME, "#1 完成（4分12秒）\n全过了")]

    def test_a_failed_send_is_retried_later(self, clock):
        calls = []
        r = ClaudeRelay(config(), store=RelayStore(":memory:"), clock=clock,
                        send=lambda chat, text: calls.append(text) or len(calls) > 1)
        r.handle(pm("cc 跑", clock=clock))
        r.finish(r.next_job(0)["id"], {"ok": False, "error": "boom"})
        r.tick()
        r.tick()                                           # too early for a retry
        assert len(calls) == 1
        clock.now += RETRY_SEND_SEC
        r.tick()
        assert len(calls) == 2 and calls[1] == "#1 失败\nboom"
        r.tick()
        assert len(calls) == 2

    def test_a_lost_job_is_reported_with_the_retry_command(self, relay, clock, sent):
        relay.handle(pm("cc 跑", clock=clock))
        relay.next_job(0)
        clock.now += LOST_AFTER_SEC + 1
        relay.tick()
        assert "失去联系" in sent[0][1] and "cc 重试 1" in sent[0][1]

    def test_cancelled_and_long_results(self, clock, sent):
        r = ClaudeRelay(config(claude_relay_max_chars=100), store=RelayStore(":memory:"), clock=clock,
                        send=lambda chat, text: sent.append(text) or True)
        r.handle(pm("cc 一", clock=clock))
        r.handle(pm("cc 二", clock=clock))
        one = r.next_job(0)["id"]
        r.handle(pm("cc 取消 1", clock=clock))
        r.finish(one, {"ok": False, "text": "已按你的要求停下。"})
        two = r.next_job(0)["id"]
        r.finish(two, {"ok": True, "text": "长" * 300, "log_path": "/Users/x/.claude-relay/logs/2.md"})
        r.tick()
        assert sent[0] == "#1 已取消\n已按你的要求停下。"
        assert sent[1].startswith("#2 完成\n" + "长" * 100) and "完整结果在 Mac：/Users/x" in sent[1]

    def test_no_sender_keeps_results_queued(self, clock):
        r = ClaudeRelay(config(), store=RelayStore(":memory:"), clock=clock)
        r.handle(pm("cc 跑", clock=clock))
        r.finish(r.next_job(0)["id"], {"ok": True, "text": "x"})
        r.tick()
        assert len(r._store.undelivered()) == 1

    def test_durations(self):
        assert [format_duration(s) for s in (5, 60, 252, 3600, 3725)] == [
            "5秒", "1分钟", "4分12秒", "1小时0分", "1小时2分"]


# ── HTTP ──────────────────────────────────────────────────────────────────

class TestHttp:
    @pytest.fixture
    def server(self, relay):
        relay.start()
        host, port = relay.address
        return relay, f"http://{host}:{port}"

    @staticmethod
    def call(url, method="GET", body=None, token=TOKEN, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as resp:
                text = resp.read()
                return resp.status, json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_without_the_token_nothing_is_served(self, server):
        _, url = server
        assert self.call(url + "/relay/health", token=None)[0] == 401
        assert self.call(url + "/relay/health", token="wrong")[0] == 401
        assert self.call(url + "/relay/jobs/1/result", "POST", {}, token="x" * 32)[0] == 401
        assert self.call(url + "/relay/health") == (200, {"ok": True})

    def test_a_job_round_trip(self, server, clock, sent):
        relay, url = server
        assert self.call(url + "/relay/next?wait=0") == (204, None)
        relay.handle(pm("cc @webot 跑测试", clock=clock))
        status, job = self.call(url + "/relay/next?wait=0")
        assert status == 200 and job == {"id": 1, "text": "跑测试", "project": "webot",
                                          "session_id": None, "created_at": clock.now}
        assert self.call(url + "/relay/jobs/1/heartbeat", "POST", {}) == (200, {"cancel": False})
        result = {"ok": True, "text": "好了", "session_id": "s1", "duration_s": 3}
        assert self.call(url + "/relay/jobs/1/result", "POST", result) == (200, {"accepted": True})
        assert self.call(url + "/relay/jobs/1/result", "POST", result) == (200, {"accepted": False})
        relay.tick()
        assert sent == [(ME, "#1 完成（3秒）\n好了")]

    def test_bad_requests(self, server):
        _, url = server
        assert self.call(url + "/relay/jobs/7/heartbeat", "POST", {})[0] == 404
        assert self.call(url + "/relay/jobs/7/result", "POST", {"ok": True})[0] == 404
        assert self.call(url + "/relay/jobs/7/result", "POST", raw=b"not json")[0] == 400
        assert self.call(url + "/relay/nothing")[0] == 404
        assert self.call(url + "/relay/jobs/x/result", "POST", {})[0] == 404

    def test_a_waiting_worker_gets_a_job_queued_meanwhile(self, server, clock):
        import threading
        relay, url = server
        got = {}
        t = threading.Thread(target=lambda: got.update(r=self.call(url + "/relay/next?wait=5")))
        t.start()
        relay.handle(pm("cc 跑", clock=clock))
        t.join(10)
        assert got["r"][0] == 200 and got["r"][1]["id"] == 1


# ── the route step and the config ─────────────────────────────────────────

class TestRouteStepAndConfig:
    class Trace:
        def __init__(self):
            self.reasons = []

        def reason(self, code, **info):
            self.reasons.append(code)

    def test_the_step_answers_relay_messages_and_passes_the_rest_on(self, relay, clock):
        trace = self.Trace()
        router = SimpleNamespace(_claude_relay=relay)
        stop = S.claude_relay(RouteContext(router=router, msg=pm("cc 跑", clock=clock), trace=trace))
        assert isinstance(stop, Stop) and "#1" in stop.value and trace.reasons == ["claude_relay"]
        assert S.claude_relay(RouteContext(router=router, msg=pm("你好", clock=clock), trace=trace)) is None
        assert S.claude_relay(RouteContext(router=SimpleNamespace(_claude_relay=None),
                                           msg=pm("cc 跑", clock=clock), trace=trace)) is None

    def test_off_by_default(self):
        from src.config import BotConfig, load_config
        assert BotConfig().claude_relay == "off"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=True):
            cfg = load_config()
        assert (cfg.claude_relay, cfg.claude_relay_port, cfg.claude_relay_prefix) == ("off", 8790, "cc")

    def test_on_needs_a_token_and_someone_allowed(self):
        from src.config import load_config
        env = {"ANTHROPIC_API_KEY": "k", "CLAUDE_RELAY": "on"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(RuntimeError) as err:
            load_config()
        assert "CLAUDE_RELAY_TOKEN" in str(err.value) and "CLAUDE_RELAY_WXIDS" in str(err.value)
        env.update(CLAUDE_RELAY_TOKEN=TOKEN, CLAUDE_RELAY_WXIDS="wxid_a, wxid_b", CLAUDE_RELAY_PORT="9000")
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.claude_relay == "on" and cfg.claude_relay_wxids == ["wxid_a", "wxid_b"]
        assert cfg.claude_relay_port == 9000
