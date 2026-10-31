"""SEND_VERIFY_CHAT (win review F10): before anything is pasted, the chat the search opened is read from the
screen and compared with the one asked for.

A send types the chat's name into WeChat's search and opens the first result; WeChat 4 cannot be asked which
chat that is (UIA shows a skeleton only).  A name inside another one (「Amy」 and the contact 「Amy陈」 or the
group 「Amy粉丝群」), or a chat the bot does not read, could open someone else's chat, and the approved draft
went there.  Now the title is read by OCR after the search: another chat the bot knows of, or a group's title
when a private chat was asked for, is refused (nothing pasted, the reason given); a title that cannot be read or
judged changes nothing (sent as before), so an OCR problem never stops the sends.

A group asked for (老板拉的群, tests/test_boss_groups_backend.py): a title without a member count is refused only when
it reads as a private chat the bot knows (``NOT_A_GROUP``); a count OCR cut off (「测试群(35」) is a group's title all
the same, and a title the bot does not know goes ahead as before (second review 2026-10-31, F10).  A refusal is logged
with the chat id's first 8 characters and the verdict, no name (F12).
"""

import logging
import os
from unittest.mock import patch

import pytest

from src.config import BotConfig, _validate_config, load_config
from src.wechat import wcdb_backend
from src.wechat.wcdb_backend import NOT_A_GROUP, VERIFY_LOOKS, WRONG_CHAT, WcdbBackend, _ocr_key, title_verdict
from src.wechat.window_controller import SendRefused, WeChatWindowController

AMY = "wxid_amy"
OTHERS = {_ocr_key("Amy陈"), _ocr_key("Amy粉丝群")}
PRIVATE = {_ocr_key("Amy"), _ocr_key("Amy陈")}           # the private chats the bot knows (a group asked for)
ROOM = "room1@chatroom"


@pytest.mark.parametrize("title, name, is_group, verdict", [
    ("Amy", "Amy", False, "ok"),
    (" amy ", "Amy", False, "ok"),                        # spaces and case
    ("测试群 (35)", "测试群", True, "ok"),                 # a group's member count
    ("测试群（35）", "测试群", True, "ok"),
    ("小美", "小美🌸", False, "ok"),                       # OCR does not read the emoji
    ("Amy陈", "Amy", False, "other"),                     # a contact whose name holds hers
    ("Amy粉丝群 (120)", "Amy", False, "other"),           # a known group
    ("Amy同学群 (8)", "Amy", False, "group"),             # a group the bot does not know, for a private chat
    ("Amy同学", "Amy", False, "unknown"),                 # someone the bot does not know: no judgement
    ("", "Amy", False, "unknown"),                        # nothing read
    ("……", "Amy", False, "unknown"),
    ("一个名字非常长的…", "一个名字非常长的达人号", False, "unknown"),   # cut off on the screen
    ("测试群二 (5)", "测试群", True, "unknown"),           # a group asked for, another group's title unknown
    ("Amy同学群 (8", "Amy", False, "unknown"),            # a private chat asked for: a count cut off is none
    ("Amy(3", "Amy", False, "unknown"),
])
def test_what_a_title_says(title, name, is_group, verdict):
    assert title_verdict(title, name, OTHERS, is_group) == verdict
    if not is_group:                                      # (the private chats known play no part in a private send)
        assert title_verdict(title, name, OTHERS, is_group, PRIVATE | {_ocr_key(title)}) == verdict


@pytest.mark.parametrize("title, verdict", [
    ("测试群 (35)", "ok"),
    ("测试群(35", "ok"), ("测试群（35", "ok"), ("测试群 (35人", "ok"), ("测试群(", "ok"),   # OCR cut the count off
    ("Amy陈", "private"), (" amy ", "private"),           # a private chat the bot knows: not the group
    ("Amy粉丝群", "other"), ("Amy陈 (3)", "other"),        # another chat it knows (a group's count lost; a count)
    ("测试群", "unknown"),                                 # its own name, its count lost: sent as before
    ("Amy同学", "unknown"), ("Amy同学群 (8)", "unknown"),    # a contact, a group, the bot does not know
    ("", "unknown"), ("……", "unknown"),
])
def test_what_a_title_says_when_a_group_was_asked_for(title, verdict):
    assert title_verdict(title, "测试群", OTHERS | {_ocr_key("Amy")}, True, PRIVATE) == verdict


# ── the window: a refusal means nothing is pasted ───────────────────────────────

def controller_sending(check, navigate_calls):
    controller = WeChatWindowController()
    restored = []
    with patch.object(controller, "find_hwnd", return_value=100), \
         patch.object(controller, "activate", return_value=True), \
         patch.object(controller, "_looks_like_blank_window", return_value=False), \
         patch.object(controller, "navigate_to_chat", side_effect=lambda h, n: navigate_calls.append(n) or 100), \
         patch.object(controller, "_adopt_foreground_hwnd", return_value=100), \
         patch.object(controller, "send_message", return_value=True) as send_message, \
         patch.object(controller, "_capture_window_state", return_value="state"), \
         patch.object(controller, "_restore_window_state", side_effect=lambda s, h: restored.append(s)), \
         patch.object(controller, "_log_failure") as log_failure:
        result = controller.send_to_chat("Amy", "寄样信息", max_retries=2, check=check)
    return result, send_message, restored, log_failure


def test_the_window_pastes_nothing_into_another_chat_and_does_not_search_again():
    refusal = SendRefused("微信搜索「Amy」打开的是「Amy陈」")
    navigated = []
    result, send_message, restored, log_failure = controller_sending(lambda hwnd: refusal, navigated)
    assert result is refusal and not result
    send_message.assert_not_called()
    assert navigated == ["Amy"] and restored == ["state"]
    log_failure.assert_called_with("Amy", "寄样信息", "another chat is open", 100)


def test_the_window_sends_when_the_check_lets_it():
    navigated = []
    result, send_message, _, _ = controller_sending(lambda hwnd: None, navigated)
    assert result is True
    send_message.assert_called_once_with(100, "寄样信息")


# ── the backend: what it knows of the chats decides ─────────────────────────────

class Window:
    """The window as the backend drives it: the search opens a chat, ``check`` looks at it."""

    def __init__(self):
        self.typed = []
        self.shots = []

    def send_to_chat(self, name, text, max_retries=2, check=None):
        refused = check(100) if check is not None else None
        if refused is not None:
            return refused
        self.typed.append((name, text))
        return True

    def send_file_to_chat(self, name, path, max_retries=2, check=None):
        return self.send_to_chat(name, f"<文件 {path}>", max_retries, check)

    def capture_chat_media(self, name, check=None):
        if check is not None and check(100) is not None:
            return ""
        self.shots.append(name)
        return "shot.png"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wcdb_backend, "VERIFY_PAUSE_SEC", 0)

    def make(titles, **config):
        backend = WcdbBackend(bot_display_name="小助手", groups=["*"], config=BotConfig(**config))
        backend._chat_names = {AMY: "Amy", "room1@chatroom": "测试群"}
        backend._chat_is_group = {AMY: False, "room1@chatroom": True}
        backend._session_names = {AMY: "Amy", "wxid_amychen": "Amy陈", "room2@chatroom": "Amy粉丝群"}
        backend._window = Window()
        reads = []
        titles = list(titles)

        def reader(hwnd):
            reads.append(hwnd)
            title = titles.pop(0) if len(titles) > 1 else titles[0]
            if isinstance(title, Exception):
                raise title
            return title
        backend._title_reader = reader
        return backend, reads
    return make


def test_the_chat_asked_for_is_sent_to(setup):
    backend, reads = setup(["Amy"])
    assert backend.send_text(AMY, "寄样信息") is True
    assert backend._window.typed == [("Amy", "寄样信息")] and reads == [100]


def test_another_known_chat_is_refused_and_nothing_is_typed(setup):
    backend, reads = setup(["Amy陈"])
    result = backend.send_text(AMY, "寄样信息")
    assert isinstance(result, SendRefused) and not result
    assert "「Amy陈」" in result.reason and "「Amy」" in result.reason
    assert backend._window.typed == []
    assert len(reads) == VERIFY_LOOKS                       # read again before giving up


def test_a_screen_that_still_shows_the_chat_before_is_read_again(setup):
    backend, reads = setup(["Amy陈", "Amy"])
    assert backend.send_text(AMY, "寄样信息") is True
    assert len(reads) == 2


def test_a_group_opened_for_a_private_chat_is_refused(setup):
    backend, _ = setup(["Amy同学群 (8)"])
    assert not backend.send_text(AMY, "寄样信息")
    assert backend._window.typed == []


def test_a_title_that_cannot_be_judged_changes_nothing(setup):
    for title in ("", "Amy同学", "A…"):
        backend, _ = setup([title])
        assert backend.send_text(AMY, "寄样信息") is True


def test_a_screen_that_cannot_be_read_is_not_tried_again_for_a_while(setup):
    backend, reads = setup([RuntimeError("Windows has no OCR language installed")])
    assert backend.send_text(AMY, "第一句") is True
    assert backend.send_text(AMY, "第二句") is True
    assert len(reads) == 1
    assert backend._window.typed == [("Amy", "第一句"), ("Amy", "第二句")]


def test_switched_off_the_screen_is_not_read(setup):
    backend, reads = setup(["Amy陈"], send_verify_chat="off")
    assert backend.send_text(AMY, "寄样信息") is True
    assert reads == []


def test_files_and_screenshots_are_checked_too(setup, tmp_path):
    backend, _ = setup(["Amy陈"])
    assert not backend.send_file(AMY, str(tmp_path / "a.png"))
    assert backend._screenshot_of("Amy", AMY) == ""
    assert backend._window.typed == [] and backend._window.shots == []


def test_a_group_is_checked_against_its_own_title(setup):
    backend, _ = setup(["测试群 (35)"])
    assert backend.send_text("room1@chatroom", "大家好") is True


@pytest.mark.parametrize("title", ["测试群(35", "测试群（35", "测试群", "Amy同学", "Amy同学群 (8)"])
def test_a_group_send_goes_ahead_with_a_cut_count_or_a_title_the_bot_does_not_know(setup, title):
    """(F10) OCR lost the closing bracket of the group's count, or the title is one the bot does not know (the group's
    own name with its count lost, a contact or a group never seen): sent as before."""
    backend, reads = setup([title])
    assert backend.send_text(ROOM, "大家好") is True
    assert backend._window.typed == [("测试群", "大家好")] and len(reads) == 1


@pytest.mark.parametrize("title, reason", [("Amy陈", NOT_A_GROUP), ("Amy", NOT_A_GROUP), ("Amy粉丝群", WRONG_CHAT)])
def test_a_group_send_into_a_known_private_chat_or_another_chat_is_refused(setup, caplog, title, reason):
    """A private chat the bot knows (the session list, a chat read) opened for the group: ``NOT_A_GROUP``; another known
    group, its count lost: ``WRONG_CHAT`` as before.  Read again first; nothing pasted; the log line has the group id's
    first 8 characters and the verdict only (the reason, with the names, is the caller's)."""
    backend, reads = setup([title])
    caplog.set_level(logging.WARNING, logger="src.wechat.wcdb_backend")
    refused = backend.send_text(ROOM, "大家好")
    assert isinstance(refused, SendRefused) and refused.reason == reason.format(name="测试群", title=title)
    assert backend._window.typed == [] and len(reads) == VERIFY_LOOKS
    [line] = [r.getMessage() for r in caplog.records]
    assert line.startswith(f"Not sending to {ROOM[:8]}…:") and ("NOT_A_GROUP" if reason == NOT_A_GROUP
                                                                 else "WRONG_CHAT") in line
    assert ROOM not in line and "测试群" not in line and title not in line


# ── the setting ───────────────────────────────────────────────────────────────

def test_the_setting_is_on_by_default_and_can_be_switched_off():
    assert BotConfig().send_verify_chat == "on"
    with patch.dict(os.environ, {"SEND_VERIFY_CHAT": "off"}, clear=True):
        assert load_config().send_verify_chat == "off"
    with pytest.raises(RuntimeError):
        _validate_config({"send_verify_chat": "maybe"})
