"""WECHAT_EMOJI and SEND_STICKERS: WeChat's own emoji codes and sticker pictures."""

import json
import logging
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from test_macos_adaptation import FakeChatlogClient, FakeMacAutomation
from test_quiet_send import CTRL, OWN, rig  # noqa: F401 - the fixture
from test_router_pipeline import PipelineSummarizer, TraceSpy
from test_router_priority import BOT, CHAT, RouterTestCase, StubWebSearch
from test_wcdb_backend_locking import _Client, _message

from src.config import BotConfig, load_config
from src.pipeline import engine
from src.pipeline.addressing import Addressing, LEVEL_EXPLICIT
from src.pipeline.envelope import (
    ENVELOPE_JSON_SCHEMA_FOLLOWUP,
    ENVELOPE_JSON_SCHEMA_FOLLOWUP_STICKER,
    ENVELOPE_JSON_SCHEMA_STICKER,
    EnvelopeError,
    envelope_schema,
    parse_envelope,
)
from src.pipeline.prompts import STAGE_DECIDE, STAGE_SINGLE, build_prompts, output_format
from src.pipeline.types import ChatRequest, Decision
from src.send.bubbles import (
    BUBBLE_SEP, BubbleSender, configure_sender, join_bubbles, plain_text, split_bubbles, strip_separators,
)
from src.send.stickers import (
    MAX_STICKERS, STICKER_MARK, StickerLibrary, configure_stickers, get_stickers, sticker_in, sticker_token,
)
from src.send import stickers as stickers_module
from src.send.wechat_emoji import DEFAULT_CODES, keep_known, parse_codes, prompt_line
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
    {"name": "问号", "file": "what.png", "when": "没听懂", "personas": ["yang_yujing"]},
]


# ── WeChat emoji codes ──────────────────────────────────────────────────────

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
    assert "[捂脸]" in prompt_line(allowed) and "一句最多一个" in prompt_line(allowed)


# ── the library ─────────────────────────────────────────────────────────────

def test_a_library_loads_and_filters_by_persona(tmp_path):
    library = make_library(tmp_path / "s", TWO)
    assert len(library) == 2 and library.problems == []
    assert library.get("问号").path == (tmp_path / "s" / "what.png").resolve()
    assert [s.name for s in library.choices("杨宇靖", "yang_yujing")] == ["害羞捂脸", "问号"]
    assert [s.name for s in library.choices("林小满", "custom")] == ["害羞捂脸"]
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
        {"name": "人设", "file": "what.png", "personas": "杨宇靖"},
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


# ── bubbles ─────────────────────────────────────────────────────────────────

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


# ── the envelope and the prompt ─────────────────────────────────────────────

OFFERED = ("害羞捂脸", "问号")


def envelope(raw, **kwargs):
    return parse_envelope(json.dumps(raw, ensure_ascii=False), max_chars=30, stickers=OFFERED, **kwargs)


def test_the_model_may_add_an_offered_sticker():
    got = envelope({"addressee": "me", "action": "reply", "messages": ["好呀"], "reason": "", "sticker": "问号"})
    assert got.messages == ("好呀",) and got.sticker == "问号" and got.is_reply
    for spelling in ("[问号]", "表情包：问号", " 问号 ", "“问号”"):
        assert envelope({"action": "reply", "messages": ["好"], "sticker": spelling}).sticker == "问号"


def test_a_sticker_alone_is_a_reply():
    got = envelope({"addressee": "me", "action": "reply", "messages": [], "reason": "", "sticker": "害羞捂脸"})
    assert got.is_reply and got.messages == () and got.sticker == "害羞捂脸"


def test_an_unknown_or_unoffered_sticker_is_ignored():
    got = envelope({"action": "reply", "messages": ["好"], "sticker": "大笑"})
    assert got.sticker == "" and "sticker_unknown" in got.warnings
    with pytest.raises(EnvelopeError, match="sticker_unknown"):    # the repair call is told why
        envelope({"action": "reply", "messages": [], "sticker": "大笑"})
    with pytest.raises(EnvelopeError, match="reply_without_text"):
        envelope({"action": "reply", "messages": []})
    plain = parse_envelope('{"action":"reply","messages":["好"],"sticker":"问号"}', max_chars=30)
    assert plain.sticker == "" and plain.warnings == ("addressee_missing",)   # not offered: not even looked at
    silent = envelope({"action": "silent", "messages": [], "sticker": "问号"})
    assert not silent.is_reply and "sticker_ignored_on_silent" in silent.warnings
    assert envelope({"action": "reply", "messages": [], "sticker": "问号"}, decide_only=True).sticker == ""


def test_a_silence_marker_with_a_sticker_is_still_silence():
    got = envelope({"action": "reply", "messages": ["沉默"], "sticker": "问号"})
    assert not got.is_reply


def test_the_schemas_are_fixed_objects():
    assert envelope_schema(follow_up=False, sticker=False) is None
    assert envelope_schema(follow_up=True, sticker=False) is ENVELOPE_JSON_SCHEMA_FOLLOWUP
    assert envelope_schema(follow_up=False, sticker=True) is ENVELOPE_JSON_SCHEMA_STICKER
    assert envelope_schema(follow_up=True, sticker=True) is ENVELOPE_JSON_SCHEMA_FOLLOWUP_STICKER
    assert "sticker" in ENVELOPE_JSON_SCHEMA_FOLLOWUP_STICKER["properties"]
    assert "follow_up" in ENVELOPE_JSON_SCHEMA_FOLLOWUP_STICKER["properties"]
    assert "sticker" not in ENVELOPE_JSON_SCHEMA_STICKER["required"]


SETTINGS = {"persona_name": "杨宇靖", "persona_prompt": "人设", "reply_rules": "规则",
            "max_chars": 30, "context_count": 10}


def request(**fields):
    return ChatRequest(message="在吗", bot_name=BOT, addressing=Addressing(LEVEL_EXPLICIT), **fields)


def test_the_prompt_lists_them_only_when_offered():
    base_system, _ = build_prompts(SETTINGS, request())
    assert "sticker" not in base_system and "微信自带表情" not in base_system
    assert output_format(30, STAGE_SINGLE) == output_format(30, STAGE_SINGLE, emoji=(), stickers=())
    system, _ = build_prompts(SETTINGS, request(wechat_emoji=("[捂脸]",), stickers=(("问号", "没听懂"),)))
    assert "[捂脸]" in system and '"sticker":"问号"' in system and "- 问号：没听懂" in system
    decide, _ = build_prompts(SETTINGS, request(wechat_emoji=("[捂脸]",), stickers=(("问号", "没听懂"),)),
                              stage=STAGE_DECIDE)
    assert "sticker" not in decide and "[捂脸]" not in decide


class JsonModel:
    """Enough of a summarizer for the engine: records the schema of every call."""

    pipeline_repair = True

    def __init__(self, answer):
        self.answer, self.schemas = answer, []

    def _chat_persona_settings(self):
        return SETTINGS

    def _retry_with_backoff(self, fn, label, max_attempts=2):
        return fn()

    def _call_json_api(self, system, messages, **kwargs):
        self.schemas.append(kwargs.get("schema", "default"))
        return SimpleNamespace(text=self.answer, usage=None, native_json=True)

    def _record_call(self, **fields):
        pass


def test_the_engine_passes_the_offer_through():
    model = JsonModel('{"addressee":"me","action":"reply","messages":["好"],"reason":"","sticker":"问号"}')
    decision = engine.decide(model, request(stickers=(("问号", "没听懂"),)))
    assert decision.sticker == "问号" and decision.as_trace()["sticker"] == "问号"
    assert model.schemas == [ENVELOPE_JSON_SCHEMA_STICKER]
    model = JsonModel('{"addressee":"me","action":"reply","messages":["好"],"reason":"","sticker":"问号"}')
    assert engine.decide(model, request()).sticker == ""
    assert model.schemas == ["default"]                          # the old call, unchanged


# ── the router ──────────────────────────────────────────────────────────────

class RouterExpressions(RouterTestCase):
    def build(self, decision, **config):
        model = PipelineSummarizer(decision)
        router, _ = self.make_router(summarizer=model, pipeline_v2="single", web=StubWebSearch(), **config)
        router._tracer = TraceSpy()
        return router, model

    def library(self):
        return configure_stickers(make_library(self.tmp / "stickers", TWO))

    def test_a_picked_sticker_becomes_the_last_bubble(self):
        self.library()
        router, model = self.build(Decision(action="reply", messages=("好呀",), addressee="me", sticker="问号"),
                                   send_stickers="on")
        reply = router.handle(self.group_msg("m1", f"@{BOT} 你懂吗"))
        assert reply == join_bubbles(["好呀", sticker_token("问号")])
        assert model.decide_requests[0].stickers == (("害羞捂脸", "被夸的时候"), ("问号", "没听懂"))
        assert router._recent_replies.recent(CHAT) == ("好呀 [表情包：问号]",)

    def test_a_sticker_alone_is_sent_alone(self):
        self.library()
        router, _ = self.build(Decision(action="reply", messages=(), addressee="me", sticker="害羞捂脸"),
                               send_stickers="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 你真可爱")) == sticker_token("害羞捂脸")

    def test_after_a_sticker_the_chat_waits_before_the_next_offer(self):
        self.library()
        router, model = self.build(Decision(action="reply", messages=("好呀",), addressee="me", sticker="问号"),
                                   send_stickers="on", sticker_min_interval_sec=600)
        router.handle(self.group_msg("m1", f"@{BOT} 一"))
        router.handle(self.group_msg("m2", f"@{BOT} 二"))
        assert model.decide_requests[0].stickers and model.decide_requests[1].stickers == ()
        router._sticker_last_at[CHAT] -= 601
        router.handle(self.group_msg("m3", f"@{BOT} 三"))
        assert model.decide_requests[2].stickers

    def test_only_the_personas_stickers_are_offered(self):
        self.library()
        router, model = self.build(Decision(action="reply", messages=("好",), addressee="me"),
                                   send_stickers="on", chat_persona_profile="custom", chat_persona_name="林小满")
        router.handle(self.group_msg("m1", f"@{BOT} 在吗"))
        assert model.decide_requests[0].stickers == (("害羞捂脸", "被夸的时候"),)

    def test_switched_off_nothing_is_offered_or_sent(self):
        self.library()
        router, model = self.build(Decision(action="reply", messages=("好呀[开心]",), addressee="me", sticker="问号"))
        assert router.handle(self.group_msg("m1", f"@{BOT} 在吗")) == "好呀[开心]"
        request = model.decide_requests[0]
        assert request.stickers == () and request.wechat_emoji == ()

    def test_an_empty_library_offers_nothing(self):
        router, model = self.build(Decision(action="reply", messages=("好",), addressee="me"), send_stickers="on")
        router.handle(self.group_msg("m1", f"@{BOT} 在吗"))
        assert model.decide_requests[0].stickers == ()

    def test_emoji_codes_are_offered_and_checked(self):
        router, model = self.build(Decision(action="reply", messages=("好呀[捂脸][开心]",), addressee="me"),
                                   wechat_emoji="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 在吗")) == "好呀[捂脸]"
        assert model.decide_requests[0].wechat_emoji == DEFAULT_CODES

    def test_own_emoji_codes_replace_the_list(self):
        router, model = self.build(Decision(action="reply", messages=("嗯[捂脸]",), addressee="me"),
                                   wechat_emoji="on", wechat_emoji_codes=["[旺柴]"])
        assert router.handle(self.group_msg("m1", f"@{BOT} 在吗")) == "嗯"
        assert model.decide_requests[0].wechat_emoji == ("[旺柴]",)

    def test_emoji_and_bubbles_together(self):
        router, _ = self.build(Decision(action="reply", messages=("好呀[开心]", "走[耶]"), addressee="me"),
                               wechat_emoji="on", send_bubbles="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 走吗")) == join_bubbles(["好呀", "走[耶]"])

    def test_a_mark_that_did_not_come_from_the_router_is_removed(self):
        router, _ = self.build(Decision(action="reply", messages=(f"好{STICKER_MARK}呀",), addressee="me"))
        assert router.handle(self.group_msg("m1", f"@{BOT} 在吗")) == "好呀"
        self.library()
        router, _ = self.build(Decision(action="reply", messages=(f"{STICKER_MARK}问号",), addressee="me"),
                               send_stickers="on")
        assert router.handle(self.group_msg("m2", f"@{BOT} 在吗")) == "问号"


# ── settings and start-up ───────────────────────────────────────────────────

def load(**env):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", **env}, clear=True):
        return load_config()


def test_the_settings_are_off_by_default_and_validated():
    config = load()
    assert (config.wechat_emoji, config.wechat_emoji_codes, config.send_stickers, config.sticker_dir,
            config.sticker_min_interval_sec, config.sticker_paste) == ("off", [], "off", "data/stickers", 600, "file")
    on = load(WECHAT_EMOJI="yes", WECHAT_EMOJI_CODES="捂脸,旺柴", SEND_STICKERS="on", STICKER_DIR="d",
              STICKER_MIN_INTERVAL_SEC="0", STICKER_PASTE="IMAGE")
    assert (on.wechat_emoji, on.wechat_emoji_codes, on.send_stickers, on.sticker_dir,
            on.sticker_min_interval_sec, on.sticker_paste) == ("on", ["[捂脸]", "[旺柴]"], "on", "d", 0, "image")
    for env, message in (({"WECHAT_EMOJI": "lots"}, "WECHAT_EMOJI"), ({"SEND_STICKERS": "x"}, "SEND_STICKERS"),
                         ({"STICKER_MIN_INTERVAL_SEC": "-1"}, "STICKER_MIN_INTERVAL_SEC"),
                         ({"STICKER_PASTE": "gif"}, "STICKER_PASTE")):
        with pytest.raises(RuntimeError, match=message):
            load(**env)


def test_the_bot_loads_the_library_and_warns_when_it_cannot_work(tmp_path, caplog):
    from src.bot import Bot
    make_library(tmp_path / "s", TWO)
    caplog.set_level(logging.INFO)
    Bot._configure_expressions(BotConfig(send_stickers="on", sticker_dir=str(tmp_path / "s"), pipeline_v2="single"))
    assert len(get_stickers()) == 2 and "only work with PIPELINE_V2" not in caplog.text
    Bot._configure_expressions(BotConfig(wechat_emoji="on"))
    assert len(get_stickers()) == 0 and "WECHAT_EMOJI only work with PIPELINE_V2" in caplog.text


# ── Windows: pasting the picture ────────────────────────────────────────────

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


# ── the backends ────────────────────────────────────────────────────────────

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


# ── review findings and the gaps it found in these tests ────────────────────

def test_a_sticker_only_reply_keeps_its_follow_up():
    got = envelope({"action": "reply", "messages": [], "sticker": "问号",
                    "follow_up": {"expect": "sender", "seconds": 30}}, follow_up=True)
    assert got.sticker == "问号" and got.follow_up.expect == "sender" and got.follow_up.seconds == 30


def test_two_steps_offer_the_stickers_only_when_writing():
    from src.pipeline.addressing import LEVEL_STRONG
    answers = iter(['{"addressee":"me","action":"reply","messages":[],"reason":""}',
                    '{"addressee":"me","action":"reply","messages":[],"reason":"","sticker":"问号"}'])
    model = JsonModel("")
    model._call_json_api = lambda system, messages, **kw: (
        model.schemas.append(kw.get("schema", "default")),
        SimpleNamespace(text=next(answers), usage=None, native_json=True))[1]
    decision = engine.decide(model, ChatRequest(message="懂吗", bot_name=BOT, addressing=Addressing(LEVEL_STRONG),
                                                stickers=(("问号", "没听懂"),)), strategy="two_step")
    assert model.schemas == ["default", ENVELOPE_JSON_SCHEMA_STICKER] and decision.sticker == "问号"


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


class RouterReviewFixes(RouterExpressions):
    def test_a_mark_in_the_text_never_becomes_a_sticker(self):
        self.library()
        router, _ = self.build(Decision(action="reply", messages=("好的", f"{STICKER_MARK}问号"), addressee="me"),
                               send_bubbles="on", send_stickers="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 懂吗")) == join_bubbles(["好的", "问号"])
        router, _ = self.build(Decision(action="reply", messages=("好的", f"{STICKER_MARK}哈哈"), addressee="me"),
                               send_bubbles="on")                              # stickers off: the bubble stays
        assert router.handle(self.group_msg("m2", f"@{BOT} 懂吗")) == join_bubbles(["好的", "哈哈"])

    def test_a_separator_in_the_text_never_splits_it(self):
        router, _ = self.build(Decision(action="reply", messages=(f"好{BUBBLE_SEP}的[捂脸]",), addressee="me"),
                               wechat_emoji="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 懂吗")) == "好的[捂脸]"

    def test_a_mark_in_a_nickname_never_splits_or_adds_a_sticker(self):
        self.library()
        router, _ = self.build(Decision(action="reply", messages=("@wxid_a 好[捂脸]",), addressee="me",
                                        sticker="害羞捂脸"), wechat_emoji="on", send_stickers="on")
        router._nicks.resolve_wxids = lambda text: text.replace("wxid_a", f"小{BUBBLE_SEP}李{STICKER_MARK}问号")
        assert router.handle(self.group_msg("m1", f"@{BOT} 对吗")) == join_bubbles(
            ["@小李问号 好[捂脸]", sticker_token("害羞捂脸")])

    def test_two_replies_at_once_send_one_sticker(self):
        self.library()
        router, _ = self.build(Decision(action="reply", messages=("好",), addressee="me"), send_stickers="on")
        msg = self.group_msg("m1", f"@{BOT} 懂吗")
        assert router._take_sticker(dict(msg), "问号") == "问号"
        assert router._take_sticker(dict(msg), "问号") == ""                     # the other thread loses

    def test_a_sticker_written_as_text_is_sent_as_a_sticker_or_not_at_all(self):
        self.library()
        router, _ = self.build(Decision(action="reply", messages=("好呀[表情包：问号]",), addressee="me"),
                               send_stickers="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 懂吗")) == join_bubbles(["好呀", sticker_token("问号")])
        assert router.handle(self.group_msg("m2", f"@{BOT} 懂吗")) == "好呀"     # cooling: just dropped
        router, _ = self.build(Decision(action="reply", messages=("好呀【表情包：害羞捂脸】",), addressee="me"))
        assert router.handle(self.group_msg("m3", f"@{BOT} 懂吗")) == "好呀"     # switched off: dropped too

    def test_a_sticker_named_like_an_emoticon_survives_the_cleaning(self):
        configure_stickers(make_library(self.tmp / "k", [{"name": "QAQ", "file": "shy.gif"}], files=("shy.gif",)))
        router, _ = self.build(Decision(action="reply", messages=(), addressee="me", sticker="QAQ"),
                               send_stickers="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 哭了")) == sticker_token("QAQ")
        assert router._recent_replies.recent(CHAT) == ("[表情包：QAQ]",)

    def test_an_answer_emptied_by_the_check_still_answers_an_at(self):
        router, _ = self.build(Decision(action="reply", messages=("[开心]",), addressee="me"), wechat_emoji="on")
        assert router.handle(self.group_msg("m1", f"@{BOT} 今天怎么样")) == "我刚才没接住，再说一次好不好"
        assert "ai_reply" not in router._tracer.reasons

    def test_emoji_checks_come_before_nicknames(self):
        router, _ = self.build(Decision(action="reply", messages=("@wxid_a 说得对",), addressee="me"),
                               wechat_emoji="on")
        router._nicks.resolve_wxids = lambda text: text.replace("wxid_a", "[北京]小李")
        assert router.handle(self.group_msg("m1", f"@{BOT} 对吗")) == "@[北京]小李 说得对"

    def test_the_proactive_path_offers_them_too(self):
        from test_router_pipeline import MODE
        self.library()
        router, model = self.build(Decision(action="reply", messages=("哈哈",), addressee="group", sticker="问号"),
                                   send_stickers="on", wechat_emoji="on", proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        reply = router.handle(self.group_msg("m1", "决赛太精彩了", at=False))
        request = model.decide_requests[0]
        assert request.call_name == "proactive_v2" and request.stickers and request.wechat_emoji
        assert reply == join_bubbles(["哈哈", sticker_token("问号")])

    def test_a_proactive_answer_emptied_by_the_check_is_silence(self):
        from test_router_pipeline import MODE
        router, _ = self.build(Decision(action="reply", messages=("[开心]",), addressee="group"),
                               wechat_emoji="on", proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        assert router.handle(self.group_msg("m1", "决赛太精彩了", at=False)) is None
        assert router._proactive.get_consecutive_silence(CHAT) == 1

    def test_a_mac_backend_is_offered_no_sticker(self):
        self.library()
        router, model = self.build(Decision(action="reply", messages=("好",), addressee="me", sticker="问号"),
                                   send_stickers="on", wechat_backend="mac_hybrid")
        assert router.handle(self.group_msg("m1", f"@{BOT} 懂吗")) == "好"
        assert model.decide_requests[0].stickers == ()

    def test_the_last_guard_removes_a_stray_mark(self):
        router, _ = self.build(Decision(action="reply", messages=("好",), addressee="me"))
        router._handle = lambda msg: f"欢迎{STICKER_MARK}新人"
        assert router.handle(self.group_msg("m1", "x")) == "欢迎新人"


def test_the_bot_warns_that_a_mac_backend_sends_no_sticker(tmp_path, caplog):
    from src.bot import Bot
    caplog.set_level(logging.INFO)
    Bot._configure_expressions(BotConfig(send_stickers="on", sticker_dir=str(tmp_path), pipeline_v2="single",
                                         wechat_backend="mac_ui"))
    assert "mac_ui backend cannot send pictures" in caplog.text


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


class StickerRequests(RouterExpressions):
    def test_asking_for_a_sticker_reaches_the_model_and_gets_one(self):
        """Real-machine round 2, BUG-02: it was answered with "我没找到刚才那张图"."""
        self.library()
        router, model = self.build(Decision(action="reply", messages=(), addressee="me", sticker="害羞捂脸"),
                                   send_stickers="on")
        reply = router.handle(self.group_msg("m1", f"@{BOT} 发个害羞的表情包"))
        assert reply == sticker_token("害羞捂脸")
        assert model.decide_requests and model.decide_requests[0].stickers
