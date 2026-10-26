"""Stickers and WeChat emoji on the sending side (src/send): the library, the sender, the window paste.

No route of the business gateway writes a sticker part or an emoji code; these
are the low-level pieces the WeChat backends still carry.
"""

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from test_macos_adaptation import FakeChatlogClient, FakeMacAutomation
from test_quiet_send import CTRL, OWN, rig  # noqa: F401 - the fixture
from test_wcdb_backend_locking import _Client, _message

from src.config import BotConfig
from src.send.bubbles import (
    BUBBLE_SEP, BubbleSender, configure_sender, join_bubbles, plain_text, split_bubbles, strip_separators,
)
from src.send.stickers import (
    MAX_STICKERS, STICKER_MARK, StickerLibrary, configure_stickers, sticker_in, sticker_token,
)
from src.send import stickers as stickers_module
from src.send.wechat_emoji import DEFAULT_CODES, keep_known, parse_codes
from src.wechat import window_controller as wc
from src.wechat.wcdb_backend import WcdbBackend
from src.wechat.window_controller import WeChatWindowController


@pytest.fixture(autouse=True)
def _clean_globals():
    configure_sender(typing=False, sleep=lambda s: None)
    configure_stickers(StickerLibrary())
    yield
    configure_sender(typing=False)
    configure_stickers(StickerLibrary())


def make_library(folder, entries, files=("shy.gif", "what.png")):
    folder.mkdir(parents=True, exist_ok=True)
    for name in files:
        Image.new("RGBA", (8, 8), (255, 0, 0, 128)).save(folder / name)
    (folder / "stickers.json").write_text(json.dumps({"stickers": entries}, ensure_ascii=False), encoding="utf-8")
    return StickerLibrary.load(folder)


TWO = [
    {"name": "害羞捂脸", "file": "shy.gif", "when": "被夸的时候"},
    {"name": "问号", "file": "what.png", "when": "没听懂", "personas": ["service"]},
]


def test_codes_are_read_in_any_usual_spelling():
    assert parse_codes("[捂脸][旺柴]") == ("[捂脸]", "[旺柴]")
    assert parse_codes("捂脸, 旺柴，吃瓜") == ("[捂脸]", "[旺柴]", "[吃瓜]")
    assert parse_codes(["捂脸", "[旺柴]", "捂脸"]) == ("[捂脸]", "[旺柴]")
    assert parse_codes("") == () and parse_codes(None) == ()
    assert parse_codes("太长了的一个表情名字啊") == ()
    assert all(code.startswith("[") and code.endswith("]") and len(code) <= 10 for code in DEFAULT_CODES)
    assert len(set(DEFAULT_CODES)) == len(DEFAULT_CODES)


def test_only_offered_codes_survive():
    allowed = ("[捂脸]", "[Emm]", "[旺柴]")
    assert keep_known("好呀[捂脸]", allowed) == "好呀[捂脸]"
    assert keep_known("好呀[开心]", allowed) == "好呀"               # WeChat would show "[开心]" as text
    assert keep_known("嗯[emm]", allowed) == "嗯[Emm]"
    assert keep_known("【捂脸】好", allowed) == "[捂脸]好"
    assert keep_known("【通知】明天开会", allowed) == "【通知】明天开会"  # other full-width brackets stay
    assert keep_known("[捂脸][旺柴][Emm]", allowed) == "[捂脸][旺柴]"   # two at most
    assert keep_known("好呀[捂", allowed) == "好呀"                  # cut by the length limit
    assert keep_known("a [x] b", allowed) == "a [x] b"              # no Chinese: ordinary text
    assert keep_known("选项[A]和[北京]", allowed) == "选项[A]和"
    assert keep_known("【捂脸】哈哈【旺柴】", allowed) == "[捂脸]哈哈[旺柴]"   # each counted once
    assert keep_known("见[Smi", allowed) == "见[Smi"


def test_a_library_loads_and_filters_by_persona(tmp_path):
    library = make_library(tmp_path / "s", TWO)
    assert len(library) == 2 and library.problems == []
    assert library.get("问号").path == (tmp_path / "s" / "what.png").resolve()
    assert [s.name for s in library.choices("客服", "service")] == ["害羞捂脸", "问号"]
    assert [s.name for s in library.choices("销售", "sales")] == ["害羞捂脸"]
    assert library.get("没有") is None


def test_bad_entries_are_skipped_with_a_reason(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    (tmp_path / "outside.png").write_bytes(b"x")
    library = make_library(folder, [
        {"name": "好", "file": "shy.gif"},
        {"name": "好", "file": "what.png"},                         # duplicate name
        {"name": "坏 名字", "file": "what.png"},                    # space
        {"name": "外面", "file": "../outside.png"},                 # outside the folder
        {"name": "文本", "file": "note.txt"},
        {"name": "没有", "file": "missing.gif"},
        {"name": "无文件"},
        {"name": "限定", "file": "what.png", "personas": "客服"},
        "just a string",
    ])
    assert [s.name for s in library.stickers] == ["好"]
    text = "\n".join(library.problems)
    for fragment in ("used twice", "letters, digits", "outside the sticker folder", "not a gif",
                     "not found", "file is required", "personas must be", "must be an object"):
        assert fragment in text
    monkeypatch.setattr(stickers_module, "MAX_FILE_BYTES", 10)
    assert "larger than" in make_library(tmp_path / "t", TWO[:1]).problems[0]


def test_a_missing_or_broken_file_gives_an_empty_library(tmp_path, monkeypatch):
    assert "not found" in StickerLibrary.load(tmp_path / "none").problems[0]
    (tmp_path / "stickers.json").write_text("{oops", encoding="utf-8")
    assert len(StickerLibrary.load(tmp_path)) == 0
    (tmp_path / "stickers.json").write_text('["a"]', encoding="utf-8")
    assert "expected" in StickerLibrary.load(tmp_path).problems[0]
    monkeypatch.setattr(stickers_module, "MAX_STICKERS", 1)
    assert len(make_library(tmp_path / "u", TWO)) == 1
    assert MAX_STICKERS == 60


def test_a_sticker_is_a_bubble_of_its_own():
    reply = join_bubbles(["好呀", sticker_token("问号")])
    assert split_bubbles(reply) == ["好呀", STICKER_MARK + "问号"]
    assert sticker_in(split_bubbles(reply)[1]) == "问号" and sticker_in("问号") is None
    assert plain_text(reply) == "好呀 [表情包：问号]"
    assert strip_separators(reply) == "好呀问号"
    too_many = BUBBLE_SEP.join(["一", "二", "三", sticker_token("问号")])
    assert split_bubbles(too_many) == ["一", "二", "三"]          # never merged into text


def test_the_sender_hands_the_sticker_file_to_the_backend(tmp_path):
    configure_stickers(make_library(tmp_path / "s", TWO))
    texts, files = [], []
    sender = BubbleSender(sleep=lambda s: None)
    reply = join_bubbles(["好呀", sticker_token("问号")])
    assert sender.deliver("c", reply, lambda t: texts.append(t) or True, send_file=lambda p: files.append(p) or True)
    assert texts == ["好呀"] and files == [str((tmp_path / "s" / "what.png").resolve())]
    files.clear()
    assert sender.deliver("c", sticker_token("问号"), lambda t: texts.append(t) or True,
                          send_file=lambda p: files.append(p) or True)
    assert len(files) == 1 and texts == ["好呀"]                 # a sticker alone is never sent as text


def test_a_sticker_the_backend_cannot_send_is_skipped(tmp_path, caplog):
    configure_stickers(make_library(tmp_path / "s", TWO))
    texts = []
    sender = BubbleSender(sleep=lambda s: None)
    reply = join_bubbles(["好呀", sticker_token("问号")])
    assert sender.deliver("c", reply, lambda t: texts.append(t) or True) is True
    assert sender.deliver("c", sticker_token("问号"), lambda t: texts.append(t) or True) is False
    assert sender.deliver("c", sticker_token("没有"), lambda t: texts.append(t) or True,
                          send_file=lambda p: True) is False
    assert texts == ["好呀"]
    assert "cannot send pictures" in caplog.text and "not in the library" in caplog.text


def test_a_failed_sticker_fails_the_reply(tmp_path):
    configure_stickers(make_library(tmp_path / "s", TWO))
    sender = BubbleSender(sleep=lambda s: None)
    reply = join_bubbles(["好呀", sticker_token("问号")])
    assert sender.deliver("c", reply, lambda t: True, send_file=lambda p: False) is False


def test_the_file_list_has_the_shape_windows_expects():
    data = wc._dropfiles_bytes(r"C:\s\shy.gif")
    assert struct.unpack("<IiiII", data[:20]) == (20, 0, 0, 0, 1)
    assert data[20:].decode("utf-16-le") == "C:\\s\\shy.gif\0\0"
    relative = wc._dropfiles_bytes("data/x.txt")[20:].decode("utf-16-le").rstrip("\0")
    assert relative == str((Path.cwd() / "data/x.txt").resolve())             # WeChat needs the full path


def test_a_still_picture_is_the_first_frame_on_white(tmp_path):
    path = tmp_path / "a.gif"
    frames = [Image.new("RGB", (4, 3), color) for color in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    dib = wc._dib_bytes(str(path))
    assert struct.unpack("<IiiHH", dib[:16]) == (40, 4, 3, 1, 24)   # BITMAPINFOHEADER, 4×3, 24 bit
    clear = tmp_path / "c.png"
    Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(clear)
    assert wc._dib_bytes(str(clear))[40:43] == b"\xff\xff\xff"      # transparent becomes white


def test_the_clipboard_gets_the_file_or_the_picture(tmp_path, monkeypatch):
    path = tmp_path / "a.png"
    Image.new("RGB", (2, 2)).save(path)
    clipboard = MagicMock(name="win32clipboard")
    clipboard.RegisterClipboardFormat.side_effect = lambda name: {"Preferred DropEffect": 50}.get(name, 60)
    monkeypatch.setattr(wc, "win32clipboard", clipboard)
    assert WeChatWindowController._set_clipboard_file(str(path))
    formats = [c.args[0] for c in clipboard.SetClipboardData.call_args_list]
    assert formats == [wc.win32con.CF_HDROP, 50]
    clipboard.reset_mock()
    assert WeChatWindowController._set_clipboard_file(str(path), as_image=True, private=True)
    formats = [c.args[0] for c in clipboard.SetClipboardData.call_args_list]
    assert formats[0] == wc.win32con.CF_DIB and len(formats) == 4         # plus the three history opt-outs
    assert WeChatWindowController._set_clipboard_file(str(tmp_path / "gone.png"), as_image=True) is False


def test_a_sticker_goes_into_the_groups_own_window(rig, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "a.gif"
    path.write_bytes(b"GIF89a")
    files = []
    monkeypatch.setattr(rig.controller, "_set_clipboard_file",
                        lambda p, as_image=False, private=False: files.append((p, as_image, private)) or True)
    assert rig.controller.send_file_to_chat("读书群", str(path)) is True
    assert files == [(str(path), False, True)] and rig.clip == []
    assert rig.keys == [("combo", CTRL, 0x41), ("combo", CTRL, 0x56), ("key", 0x0D)]
    assert rig.desktop.pings == [OWN]                              # the clipboard is put back afterwards
    assert rig.controller.send_file_to_chat("读书群", str(tmp_path / "gone.gif")) is False


def test_a_picture_that_cannot_go_on_the_clipboard_is_not_pasted():
    controller = WeChatWindowController(paste_files_as="image")
    assert controller._paste_files_as == "image"
    assert WeChatWindowController(paste_files_as="weird")._paste_files_as == "file"
    with (
        patch.object(controller, "_validate_hwnd", return_value=True),
        patch.object(controller, "_foreground_matches", return_value=True),
        patch.object(controller, "_set_clipboard_file", return_value=False) as put,
        patch.object(controller, "_set_clipboard") as set_text,
        patch("src.wechat.window_controller.send_combo") as combo,
        patch("src.wechat.window_controller.press_key") as press,
    ):
        assert controller.send_message(1, "[file] a.gif", file_path="a.gif") is False
    put.assert_called_once_with("a.gif", as_image=True, private=False)
    set_text.assert_not_called()
    combo.assert_not_called() and press.assert_not_called()


def test_wcdb_sends_the_sticker_under_the_window_lock(tmp_path):
    configure_stickers(make_library(tmp_path / "s", TWO))
    backend = WcdbBackend(bot_display_name="小助手", groups=["测试群"], config=BotConfig(sticker_paste="image"))
    backend._client = _Client()
    backend._running = True
    assert backend._window._paste_files_as == "image"
    sent, held = [], []
    backend._send_and_confirm = lambda group, talker, text: sent.append(text) or True

    def send_file(group, path):
        held.append(backend._window_lock.locked())
        sent.append((group, path))
        return True

    backend._window.send_file_to_chat = send_file
    reply = join_bubbles(["好呀", sticker_token("问号")])
    backend._handle_message("测试群", "room1@chatroom", _message(), lambda msg: reply)
    assert sent == ["好呀", ("测试群", str((tmp_path / "s" / "what.png").resolve()))] and held == [True]


def test_mac_backends_skip_the_sticker_and_send_the_text(tmp_path):
    from src.wechat.mac_hybrid_backend import MacHybridBackend
    configure_stickers(make_library(tmp_path / "s", TWO))
    client = FakeChatlogClient([{
        "count": 1, "new_state": {"room1@chatroom": 1780727000},
        "messages": [{"timestamp": 1780726999, "sender": "Alice", "type": "text", "content": "@群聊小助手 在吗",
                      "local_id": 7, "chat": "摸鱼群", "username": "room1@chatroom", "is_group": True}],
    }])
    automation = FakeMacAutomation()
    backend = MacHybridBackend(bot_display_name="群聊小助手", groups=["摸鱼群"], poll_sec=0.01,
                               client=client, automation=automation)
    backend.poll_once(lambda msg: join_bubbles(["收到", sticker_token("问号")]))
    assert automation.sent == ["收到"]


def test_a_symlink_out_of_the_folder_is_refused(tmp_path):
    outside = tmp_path / "outside.png"
    Image.new("RGB", (2, 2)).save(outside)
    folder = tmp_path / "s"
    folder.mkdir()
    try:
        (folder / "link.png").symlink_to(outside)
    except OSError as exc:                                     # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    (folder / "stickers.json").write_text(json.dumps({"stickers": [{"name": "链接", "file": "link.png"}]}),
                                          encoding="utf-8")
    library = StickerLibrary.load(folder)
    assert len(library) == 0 and "outside the sticker folder" in library.problems[0]


def test_a_sticker_through_the_main_window_pastes_the_file(rig, tmp_path, monkeypatch):  # noqa: F811
    del rig.windows[OWN]
    path = tmp_path / "a.gif"
    path.write_bytes(b"GIF89a")
    files = []
    monkeypatch.setattr(rig.controller, "_set_clipboard_file",
                        lambda p, as_image=False, private=False: files.append(p) or True)
    assert rig.controller.send_file_to_chat("读书群", str(path)) is True
    assert files == [str(path)] and rig.clip == [("读书群", True)]           # the search, then the file
    assert rig.keys[-2:] == [("combo", CTRL, 0x56), ("key", 0x0D)]


def test_a_failed_picture_is_not_pasted_and_the_clipboard_comes_back(rig, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "a.gif"
    path.write_bytes(b"GIF89a")
    monkeypatch.setattr(rig.controller, "_set_clipboard_file", lambda p, as_image=False, private=False: False)
    assert rig.controller.send_file_to_chat("读书群", str(path), max_retries=1) is False
    assert ("combo", CTRL, 0x56) not in rig.keys and ("key", 0x0D) not in rig.keys
    from test_quiet_send import SAVED
    assert rig.desktop.written == [SAVED]                                     # possibly emptied: put back


def test_a_clipboard_that_never_opens_reports_failure(tmp_path, monkeypatch):
    path = tmp_path / "a.png"
    Image.new("RGB", (2, 2)).save(path)
    clipboard = MagicMock(name="win32clipboard")
    clipboard.OpenClipboard.side_effect = OSError("busy")
    monkeypatch.setattr(wc, "win32clipboard", clipboard)
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)
    assert WeChatWindowController._set_clipboard_file(str(path)) is False
    assert clipboard.OpenClipboard.call_count == 3
