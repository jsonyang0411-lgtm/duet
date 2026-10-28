"""tools/friend_probe.py: the read-only look at how a new friend shows in WeChat 4's database (a fake client)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SESSIONS = [
    {"username": "wxid_newfriend12345", "displayName": "张小美", "sort_timestamp": 1761530000},
    {"username": "meimei_88", "displayName": "小美美", "sort_timestamp": 1761520000},
    {"username": "20968749111@chatroom", "displayName": "某个群", "sort_timestamp": 1761540000},
    {"username": "gh_abcdef123456", "displayName": "公众号", "sort_timestamp": 1761540001},
    {"username": "filehelper", "displayName": "文件传输助手", "sort_timestamp": 1761540002},
    {"username": "wxid_me", "displayName": "我自己", "sort_timestamp": 1761540003},
]
MESSAGES = {
    "wxid_newfriend12345": [
        {"local_type": 1, "sender_username": "wxid_newfriend12345", "message_content": "我是张小美，电话 13800138000",
         "create_time": 1761529990, "real_sender_id": 5},
        {"local_type": 10000, "sender_username": "", "message_content": "你已添加了张小美，现在可以开始聊天了。",
         "create_time": 1761530000},
        {"local_type": 3, "sender_username": "wxid_newfriend12345", "message_content": "<img/>", "create_time": 1761530005},
    ],
    "meimei_88": [
        {"local_type": 10000, "sender_username": "", "message_content": "小美美通过了你的朋友验证请求，现在可以开始聊天了",
         "create_time": 1761520000},
        {"local_type": 1, "sender_username": "wxid_me", "message_content": "你好呀", "create_time": 1761520010,
         "real_sender_id": 2},
    ],
}


class FakeClient:
    def __init__(self):
        self._config = {"myWxid": "wxid_me_b6ce"}          # WeChat 4's data folder: the wxid plus a suffix
        self.asked = []

    def init(self):
        pass

    def open(self):
        pass

    def get_sessions(self):
        return SESSIONS

    def get_messages(self, username, limit=200):
        self.asked.append(username)
        return MESSAGES.get(username, [])


@pytest.fixture
def tool(monkeypatch):
    import src.wechat.wcdb_client as wc
    monkeypatch.setattr(wc, "WcdbNativeClient", FakeClient)
    spec = importlib.util.spec_from_file_location("friend_probe_tool", ROOT / "tools" / "friend_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_shows_the_system_lines_and_hides_ids_names_and_numbers(tool, tmp_path, capsys):
    out = tmp_path / "probe.txt"
    assert tool.main(["--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.strip() in capsys.readouterr().out                   # the screen shows what the file holds
    assert "个人私聊 2 个" in text                                   # no group, official account, helper, self
    assert "wxid_…（19 位）" in text and "自定义微信号（首字符 字母，9 位）" in text
    assert ">>>" in text and "你已添加了〈名字〉，现在可以开始聊天了。" in text
    assert "〈名字〉通过了你的朋友验证请求" in text
    assert "138****8000" in text and "（类型 3，不看内容）" in text
    assert "我（real_sender_id 2）：你好呀" in text and "对方（real_sender_id 5）：我是〈名字〉" in text   # own line by the suffixed wxid
    for secret in ("wxid_newfriend12345", "meimei_88", "13800138000", "张小美", "小美美"):
        assert secret not in text
    assert text.index("我是〈名字〉") < text.index("你已添加了")       # oldest first
    assert "会话带的字段：" in text and "消息带的字段：" in text


def test_own_lines_are_told_apart_by_the_wxid_without_the_folder_suffix(tool):
    assert tool.is_me("wxid_me", "wxid_me_b6ce") and tool.is_me("wxid_me", "wxid_me")
    assert not tool.is_me("wxid_newfriend12345", "wxid_me_b6ce") and not tool.is_me("", "wxid_me_b6ce")
    assert not tool.is_me("wxid_m", "wxid_me_b6ce")                # a shorter id is not a prefix match


def test_a_chat_that_cannot_be_read_is_said_and_the_others_still_shown(tool, tmp_path, capsys):
    """The client raises on an error code (WcdbCallError, win review F14): one chat is not the end."""
    from src.wechat.wcdb_client import WcdbCallError

    real = FakeClient.get_messages

    def failing(self, username, limit=200):
        if username == "meimei_88":
            raise WcdbCallError("wcdb_get_messages", -3)
        return real(self, username, limit)

    FakeClient.get_messages = failing
    try:
        assert tool.main(["--out", str(tmp_path / "probe.txt")]) == 0
    finally:
        FakeClient.get_messages = real
    out = (tmp_path / "probe.txt").read_text(encoding="utf-8")
    assert "读不出这个私聊：wcdb_get_messages failed: ret=-3" in out
    assert "你已添加了" in out                             # the other chat is still shown
