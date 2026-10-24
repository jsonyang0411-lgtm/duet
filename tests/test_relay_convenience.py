"""Relay conveniences: no "cc" in relay-only mode, the current project, the worker's project list."""

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from test_claude_relay_worker import ROOT, TOKEN, bot, fake_claude, run_one, settings, worker  # noqa: F401  (fixtures)

from src.relay import commands as C
from src.relay.service import VOICE_NOT_HEARD, ClaudeRelay
from src.relay.store import RelayStore

ME = "wxid_me"


def config(**overrides):
    values = dict(
        claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME, claude_relay_only="off",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def relay(**overrides):
    return ClaudeRelay(config(**overrides), store=RelayStore(":memory:"))


def pm(content, *, sender=ME, msg_type=1, group=False):
    return {"chat_id": sender, "sender_id": sender, "content": content, "is_group": group,
            "msg_type": msg_type, "timestamp": time.time()}


# ── commands ──────────────────────────────────────────────────────────────

def test_project_words_and_switching():
    assert C.parse("项目") == C.RelayCommand("projects")
    assert C.parse("当前项目") == C.RelayCommand("projects")
    assert C.parse("切到 douyin") == C.RelayCommand("switch", project="douyin")
    assert C.parse("切换到@relay") == C.RelayCommand("switch", project="relay")
    assert C.parse("cd webot") == C.RelayCommand("switch", project="webot")
    assert C.parse("切换一下思路，先看测试") == C.RelayCommand("task", text="切换一下思路，先看测试")
    assert C.parse("切到 不存在的") == C.RelayCommand("switch", project="不存在的")
    assert C.parse("切回 webot") == C.RelayCommand("switch", project="webot")


# ── no "cc" when the bot only relays ──────────────────────────────────────

class TestNoPrefix:
    def test_relay_only_takes_every_private_text_of_an_allowed_person(self):
        r = relay(claude_relay_only="on")
        matched, reply = r.handle(pm("跑一下测试"))
        assert matched and "#1" in reply and r.store.get(1)["text"] == "跑一下测试"
        assert r.handle(pm("cc 也照样可以"))[0] is True                       # the prefix still works
        assert r.handle(pm("状态"))[1].startswith("Mac：")                     # control words without cc

    def test_others_groups_and_non_text_are_not_tasks(self):
        r = relay(claude_relay_only="on")
        assert r.handle(pm("跑一下测试", sender="wxid_other")) == (False, None)
        assert r.handle(pm("跑一下测试", group=True)) == (False, None)
        assert r.handle(pm("[表情]", msg_type=47)) == (False, None)              # pictures: test_relay_attachments
        assert r.handle(pm("[视频]", msg_type=43)) == (False, None)
        assert r.handle(pm("   ")) == (False, None)

    def test_without_relay_only_the_prefix_is_needed(self):
        r = relay()
        assert r.handle(pm("跑一下测试")) == (False, None)
        assert r.handle(pm("cc 跑一下测试"))[0] is True

    def test_the_help_drops_the_prefix_when_it_is_not_needed(self):
        assert "不用打 cc" in relay(claude_relay_only="on").help_text()
        assert "cc 状态" in relay().help_text() and "不用打 cc" not in relay().help_text()


# ── the current project ───────────────────────────────────────────────────

class TestProjects:
    def test_before_the_mac_reports_its_projects(self):
        r = relay()
        assert "Mac 还没连上过" in r.handle(pm("cc 项目"))[1]
        assert "之后的任务都在 douyin" in r.handle(pm("cc 切到 douyin"))[1]     # cannot check: accepted

    def test_switching_checks_the_list_and_tasks_follow_it(self):
        r = relay()
        r.note_worker(["douyin", "relay", "webot"], "webot")
        text = r.handle(pm("cc 项目"))[1]
        assert "webot（Mac 的默认）" in text and "douyin、relay、webot" in text
        assert "没有叫 nope 的项目" in r.handle(pm("cc 切到 nope"))[1]
        r.handle(pm("cc 第一个"))
        r.handle(pm("cc 切到 douyin"))
        r.handle(pm("cc 第二个"))
        r.handle(pm("cc @relay 第三个"))                                       # one task elsewhere
        r.handle(pm("cc 第四个"))
        assert [r.store.get(i)["project"] for i in (1, 2, 3, 4)] == ["", "douyin", "relay", "douyin"]
        assert "项目：douyin" in r.handle(pm("cc 状态"))[1]

    def test_switching_starts_a_new_session(self):
        r = relay()
        r.handle(pm("cc 一"))
        job = r.next_job(0)
        r.finish(job["id"], {"ok": True, "text": "", "session_id": "s1"})
        r.handle(pm("cc 切到 douyin"))
        r.handle(pm("cc 二"))
        assert r.next_job(0)["session_id"] is None

    def test_a_database_from_before_gets_the_column(self, tmp_path):
        path = tmp_path / "relay.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE relay_sessions (chat_id TEXT PRIMARY KEY, session_id TEXT, updated_at REAL NOT NULL)")
        con.execute("INSERT INTO relay_sessions VALUES ('wxid_me', 's-old', 1)")
        con.commit()
        con.close()
        store = RelayStore(path)
        assert store.current_project(ME) == ""
        store.add(ME, ME, "x")
        assert store.claim_next()["session_id"] == "s-old"                     # kept
        store.set_project(ME, "douyin")
        assert store.current_project(ME) == "douyin"


# ── the worker reports its projects and follows config.json ──────────────

def test_the_worker_tells_the_bot_its_projects(worker, bot, fake_claude, tmp_path):
    s = settings(bot, fake_claude, tmp_path)
    s["projects"]["抖音"] = str(tmp_path / "proj")
    bot.ask("cc 只回复：收到")
    run_one(worker, bot, s)
    assert bot.relay._worker_projects == ["proj", "抖音"] and bot.relay._worker_default == "proj"
    assert "proj、抖音" in bot.ask("cc 项目")[1]


def test_a_switched_project_is_where_the_worker_runs(worker, bot, fake_claude, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    s = settings(bot, fake_claude, tmp_path)
    s["projects"]["other"] = str(other)
    bot.ask("cc 切到 other")
    bot.ask("cc 看看这里")
    run_one(worker, bot, s)
    assert fake_claude.calls()[0]["cwd"] == str(other.resolve())


def test_config_json_is_read_again_when_it_changes(worker, tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"relay_url": "http://x", "token": "a" * 32, "projects": {"a": "/tmp"}}), encoding="utf-8")
    f = worker.SettingsFile(path, worker.load_settings(path))
    assert sorted(f.current()["projects"]) == ["a"]
    path.write_text(json.dumps({"relay_url": "http://x", "token": "a" * 32, "projects": {"a": "/tmp", "b": "/tmp"}}),
                    encoding="utf-8")
    import os
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    assert sorted(f.current()["projects"]) == ["a", "b"]
    path.write_text("{broken", encoding="utf-8")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 20_000_000))
    assert sorted(f.current()["projects"]) == ["a", "b"]                         # a broken file keeps the old one


# ── voice messages (VOICE_ASR_ENABLED on Windows) ─────────────────────────

class TestVoice:
    def voice(self, text, **kw):
        return pm(f"[语音] {text}" if text else "[语音]", msg_type=34, **kw)

    def test_relay_only_takes_a_voice_task_and_says_what_it_heard(self):
        r = relay(claude_relay_only="on")
        matched, reply = r.handle(self.voice("帮我跑一下测试。"))
        assert matched and "#1" in reply and reply.endswith("\n听到的是：帮我跑一下测试")
        assert r.store.get(1)["text"] == "帮我跑一下测试"
        assert r.handle(self.voice("状态。"))[1].startswith("Mac：")            # a control word by voice
        assert "听到的是" not in r.handle(self.voice("状态"))[1]

    def test_with_the_prefix_a_voice_needs_it_too(self):
        r = relay()
        assert r.handle(self.voice("跑一下测试")) == (False, None)
        matched, reply = r.handle(self.voice("CC 跑一下测试。"))
        assert matched and r.store.get(1)["text"] == "跑一下测试" and "听到的是：跑一下测试" in reply

    def test_a_voice_without_text(self):
        r = relay(claude_relay_only="on")
        assert r.handle(self.voice("")) == (True, VOICE_NOT_HEARD)
        assert r.handle(self.voice("  。")) == (True, VOICE_NOT_HEARD)
        old = self.voice("")
        old["timestamp"] = time.time() - 3600
        assert r.handle(old) == (True, None)                                  # replayed history: quiet
        assert r.handle(self.voice("", sender="wxid_other")) == (False, None)
        assert relay().handle(self.voice("")) == (False, None)                 # not relay only: normal message
        assert r.store.get(1) is None

    def test_others_voices_and_groups_are_not_tasks(self):
        r = relay(claude_relay_only="on")
        assert r.handle(self.voice("跑测试", sender="wxid_other")) == (False, None)
        assert r.handle(self.voice("跑测试", group=True)) == (False, None)


# ── the admin's management commands are not tasks ─────────────────────────

def test_only_the_admins_management_commands_are_left_alone():
    reserved = lambda text: text.startswith("添加好友")                     # noqa: E731
    r = ClaudeRelay(config(claude_relay_only="on", claude_relay_wxids=[ME, "wxid_two"]),
                    store=RelayStore(":memory:"), reserved=reserved, extra_help="管理命令：添加好友")
    assert r.handle(pm("添加好友 abc")) == (False, None)                    # the route's relay_only step answers it
    assert r.handle(pm("添加好友 abc", sender="wxid_two"))[0] is True        # not the admin: a task
    assert "管理命令：添加好友" in r.help_text()
    assert "管理命令" not in ClaudeRelay(config(), store=RelayStore(":memory:"), reserved=reserved,
                                        extra_help="管理命令：添加好友").help_text()   # not relay only


def test_a_broken_check_does_not_stop_the_relay():
    def broken(text):
        raise RuntimeError("boom")
    r = ClaudeRelay(config(claude_relay_only="on"), store=RelayStore(":memory:"), reserved=broken)
    assert r.handle(pm("跑一下测试"))[0] is True


def test_admin_commands_are_recognised_without_running_them():
    from unittest.mock import MagicMock
    from src.admin import AdminCommandHandler
    nicks, friend = MagicMock(), MagicMock()
    admin = AdminCommandHandler(nicks, friend_manager=friend)
    for text in ("添加好友 abc 你好", "加好友 abc", "同意好友", "自动同意好友 开", "关闭自动同意",
                 "改名 wxid_a = 张三", "删除昵称 wxid_a", "刷新昵称", "  同意好友  "):
        assert admin.is_command(text), text
    for text in ("帮助", "help", "跑一下测试", "添加好友的功能怎么用", "同意", ""):
        assert not admin.is_command(text), text
    assert nicks.mock_calls == [] and friend.mock_calls == []                # nothing ran

