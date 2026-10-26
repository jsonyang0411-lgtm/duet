"""Relay results too long for one message go as a .txt file (backends that can send files)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.relay.service import SUMMARY_CHARS, ClaudeRelay
from src.relay.store import RelayStore

ME = "wxid_me"


def make_relay(tmp_path, *, file_ok=True, long_file="on", with_file=True):
    sent, files = [], []

    def send_file(chat, path):
        files.append((chat, path, Path(path).read_text(encoding="utf-8-sig")))
        return file_ok

    cfg = SimpleNamespace(
        claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=200, admin_wxid=ME, claude_relay_only="off", claude_relay_progress_min=0,
        claude_relay_long_file=long_file,
    )
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), files_dir=tmp_path / "files",
                        send=lambda chat, text: sent.append(text) or True,
                        send_file=send_file if with_file else None)
    return relay, sent, files


def finish(relay, text, ok=True):
    relay.handle({"chat_id": ME, "sender_id": ME, "content": "cc 写个长报告", "is_group": False})
    job = relay.next_job(0)
    relay.finish(job["id"], {"ok": ok, "text": text, "duration_s": 75, "log_path": "/Users/x/log.md"})
    relay.tick()
    return job["id"]


LONG = "结论：都好了。\n" + "细节" * 400


def test_a_long_result_goes_as_a_file_then_a_short_message(tmp_path):
    relay, sent, files = make_relay(tmp_path)
    finish(relay, LONG)
    assert len(files) == 1
    chat, path, content = files[0]
    assert chat == ME and Path(path).name == "中转结果-1.txt"
    assert content.startswith("#1 完成（1分15秒）\n任务：写个长报告\n项目：Mac 的默认项目\n")
    assert LONG in content                                            # the whole result
    assert sent == [f"#1 完成（1分15秒）\n{LONG[:SUMMARY_CHARS].rstrip()}…\n"
                    "（结果太长，完整内容在上面的文件「中转结果-1.txt」里）"]
    assert Path(path).read_bytes().startswith(b"\xef\xbb\xbf")         # BOM: Notepad reads it as UTF-8


def test_a_short_result_is_a_message(tmp_path):
    relay, sent, files = make_relay(tmp_path)
    finish(relay, "好了")
    assert files == [] and sent == ["#1 完成（1分15秒）\n好了"]


def test_a_file_that_cannot_be_sent_falls_back_to_the_cut_message(tmp_path):
    relay, sent, files = make_relay(tmp_path, file_ok=False)
    finish(relay, LONG)
    assert len(files) == 1 and "已截断，完整结果在 Mac：/Users/x/log.md" in sent[0]


@pytest.mark.parametrize("kind", ["switched off", "no backend support"])
def test_without_file_sending_the_result_is_cut(tmp_path, kind):
    relay, sent, files = make_relay(tmp_path, long_file="off" if kind == "switched off" else "on",
                                    with_file=kind != "no backend support")
    finish(relay, LONG)
    assert files == [] and "已截断" in sent[0]


def test_the_file_is_not_sent_twice_when_the_message_after_it_fails(tmp_path):
    relay, sent, files = make_relay(tmp_path)
    answers = iter([False, True])
    relay._send = lambda chat, text: sent.append(text) or next(answers)
    finish(relay, LONG)
    relay._send_failed_at.clear()                                     # retry at once
    relay.tick()
    assert len(files) == 1 and len(sent) == 2 and sent[0] == sent[1]


def test_old_result_files_are_deleted(tmp_path):
    import os
    import time
    relay, sent, files = make_relay(tmp_path)
    folder = tmp_path / "files"
    folder.mkdir()
    old, keep, other = folder / "中转结果-0.txt", folder / "中转结果-9.txt", folder / "别的.txt"
    for f in (old, keep, other):
        f.write_text("x", encoding="utf-8")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    os.utime(other, (time.time() - 40 * 86400,) * 2)
    finish(relay, LONG)
    assert not old.exists() and keep.exists() and other.exists()


def test_the_wcdb_backend_sends_a_file_into_the_named_chat(tmp_path):
    from src.wechat.wcdb_backend import WcdbBackend
    backend = WcdbBackend.__new__(WcdbBackend)
    backend._talker_ids = {"文件传输助手": "filehelper", "我": ME}
    import threading
    backend._window_lock = threading.RLock()
    backend._window = MagicMock()
    backend._window.send_file_to_chat.return_value = True
    assert backend.send_file(ME, "C:/x/中转结果-1.txt") is True
    backend._window.send_file_to_chat.assert_called_once_with("我", "C:/x/中转结果-1.txt")
    assert backend.send_file("wxid_unknown", "C:/x/a.txt") is False


def test_sticker_paste_image_only_turns_pictures_into_images(tmp_path, monkeypatch):
    from src.wechat.window_controller import WeChatWindowController
    controller = WeChatWindowController(paste_files_as="image")
    calls = []
    monkeypatch.setattr(WeChatWindowController, "_set_clipboard_file",
                        staticmethod(lambda path, as_image=False, private=False: calls.append((Path(path).name, as_image)) or True))
    controller._clipboard_file(str(tmp_path / "a.gif"), 1)
    controller._clipboard_file(str(tmp_path / "中转结果-1.txt"), 1)
    assert calls == [("a.gif", True), ("中转结果-1.txt", False)]


def test_the_switch_is_checked():
    from src.config import _validate_config
    base = dict(claude_relay="on", claude_relay_token="t" * 32, admin_wxid=ME, claude_relay_prefix="cc")
    _validate_config({**base, "claude_relay_long_file": "off"})
    with pytest.raises(RuntimeError, match="CLAUDE_RELAY_LONG_FILE"):
        _validate_config({**base, "claude_relay_long_file": "maybe"})
