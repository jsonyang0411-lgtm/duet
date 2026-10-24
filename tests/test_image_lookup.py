"""Finding the file of a picture message in WeChat 4's folders (the lookup behind the relay's pictures and "look at this").

A synthetic WeChat 4 tree: ``xwechat_files/<account>/msg/attach/<md5 of the chat id>/<YYYY-MM>/Img/<md5>[_h|_t].dat``,
encrypted with the real format code (``image_key.encode_v4``) and a key cached the way the bot keeps it.
"""

import hashlib
import io
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src import vision
from src.wechat import image_key as K

AES = b"0123456789abcdef"
XOR = 0x8B
ACCOUNT = "wxid_test_abcd"
ADMIN = "wxid_admin"
GROUP = "1234567@chatroom"


def jpeg(width, height, color=(200, 30, 30)):
    out = io.BytesIO()
    image = Image.new("RGB", (width, height), color)
    for x in range(0, width, 40):
        for y in range(0, height, 40):
            image.putpixel((x, y), (0, 0, 0))
    image.save(out, "JPEG")
    return out.getvalue()


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


class Tree:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "wechat_image_keys.json").write_text(
            json.dumps({ACCOUNT: {"aes": AES.decode(), "xor": XOR}}), encoding="utf-8")
        self.root = tmp_path / "xwechat_files"
        self.account = self.root / ACCOUNT
        (self.account / "db_storage").mkdir(parents=True)
        self.now = int(time.time())
        self.cache = tmp_path / "cache"

    def put(self, chat, name, payload, age=0, month=None, wxgf=False):
        folder = self.account / "msg" / "attach" / md5(chat) / (month or time.strftime("%Y-%m")) / "Img"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(K.encode_v4(b"wxgf" + os.urandom(3000) if wxgf else payload, AES, XOR, version=2))
        os.utime(path, (self.now - age, self.now - age))
        return path

    def locate(self, chat=ADMIN, **kw):
        kw.setdefault("timestamp", self.now)
        return vision.locate_recent_image_file(wechat_data_dir=str(self.root), lookback_sec=900,
                                               cache_dir=self.cache, chat_id=chat, **kw)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    return Tree(tmp_path, monkeypatch)


def size(path):
    if not path:
        return None
    with Image.open(path) as image:
        return image.size


def test_the_picture_of_the_chat_is_found_and_decoded(tree):
    tree.put(ADMIN, "aaaa_h.dat", jpeg(1750, 1020), age=5)
    assert size(tree.locate()) == (1750, 1020)


def test_the_hd_file_beats_the_plain_one_and_the_thumbnail_of_the_same_picture(tree):
    tree.put(ADMIN, "bbbb_h.dat", jpeg(1750, 1020), age=9)
    tree.put(ADMIN, "bbbb.dat", jpeg(800, 460), age=5)
    tree.put(ADMIN, "bbbb_t.dat", jpeg(160, 100), age=1)               # written last: nearest in time
    assert size(tree.locate()) == (1750, 1020)


def test_without_the_hd_file_the_plain_one_beats_the_thumbnail(tree):
    tree.put(ADMIN, "cccc.dat", jpeg(800, 460), age=9)
    tree.put(ADMIN, "cccc_t.dat", jpeg(160, 100), age=1)
    assert size(tree.locate()) == (800, 460)


def test_a_thumbnail_alone_is_returned_only_when_allowed(tree):
    tree.put(ADMIN, "dddd_t.dat", jpeg(160, 100), age=3)
    assert size(tree.locate()) == (160, 100)
    assert tree.locate(allow_thumbnail=False) == ""


def test_another_chats_picture_that_is_closer_in_time_is_never_taken(tree):
    tree.put(ADMIN, "eeee_h.dat", jpeg(1750, 1020), age=40)
    tree.put(GROUP, "ffff_h.dat", jpeg(800, 800), age=1)
    assert size(tree.locate(ADMIN)) == (1750, 1020)
    assert size(tree.locate(GROUP)) == (800, 800)


def test_a_chat_with_a_folder_but_nothing_in_the_window_gets_nothing_not_another_chats_picture(tree):
    tree.put(ADMIN, "gggg_h.dat", jpeg(1750, 1020), age=5000)          # far outside the window
    tree.put(GROUP, "hhhh_h.dat", jpeg(800, 800), age=1)
    assert tree.locate(ADMIN) == ""


def test_a_sticker_in_the_same_chat_does_not_hide_the_picture(tree):
    tree.put(ADMIN, "iiii_h.dat", jpeg(1750, 1020), age=30)
    tree.put(ADMIN, "jjjj.dat", b"", age=1, wxgf=True)                  # closer in time, but not a picture
    assert size(tree.locate()) == (1750, 1020)


def test_without_a_folder_of_the_chat_everything_is_searched_as_before(tree):
    """WeChat 3's layout has no folder per chat; so does a guess about the folder name that is wrong."""
    tree.put("someone_else", "kkkk_h.dat", jpeg(640, 480), age=4)
    assert size(tree.locate(chat="wxid_no_folder_for_this_chat")) == (640, 480)
    assert size(tree.locate(chat="")) == (640, 480)                      # no chat id at all: the old behaviour


def test_the_md5_of_the_message_picks_its_file_even_when_a_newer_one_exists(tree):
    wanted = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
    tree.put(ADMIN, f"{wanted}_h.dat", jpeg(1750, 1020), age=800)
    tree.put(ADMIN, "ffffeeeeffffeeeeffffeeeeffffeeee_h.dat", jpeg(800, 800), age=1)
    assert size(tree.locate(media_hint=wanted)) == (1750, 1020)
    assert size(tree.locate(media_hint="7312345678901234567")) == (800, 800)     # a server id matches no file


def test_months_far_from_the_message_are_not_entered_so_old_pictures_cannot_use_up_the_budget(tree, monkeypatch):
    monkeypatch.setattr(vision, "MAX_SCANNED_FILES", 40)
    old = tree.account / "msg" / "attach" / ("0" * 32) / "2024-01" / "Img"          # sorts before everything
    old.mkdir(parents=True)
    for i in range(200):
        (old / f"{i}.bin").write_bytes(b"")
    tree.put("someone_else", "llll_h.dat", jpeg(640, 480), age=4)
    assert size(tree.locate(chat="wxid_no_folder_for_this_chat")) == (640, 480)


def test_a_picture_from_the_previous_month_is_still_in_reach(tree):
    last_month = time.strftime("%Y-%m", time.localtime(tree.now - 20 * 86400))
    tree.put(ADMIN, "mmmm_h.dat", jpeg(1750, 1020), age=5, month=last_month)
    assert size(tree.locate()) == (1750, 1020)


def test_why_nothing_was_found_is_in_the_log(tree, caplog):
    tree.put(ADMIN, "nnnn.dat", b"", age=1, wxgf=True)
    with caplog.at_level(logging.INFO):
        assert tree.locate() == ""
    text = caplog.text
    assert "No picture found" in text and "wxgf" in text and "nnnn.dat" in text


def test_where_a_picture_was_found_is_in_the_log(tree, caplog):
    tree.put(ADMIN, "oooo_h.dat", jpeg(1750, 1020), age=2)
    with caplog.at_level(logging.INFO):
        tree.locate()
    assert "Picture found in the chat's folder" in caplog.text and "HD" in caplog.text


def test_the_picture_of_a_message_goes_through_find_recent_image_path(tree):
    tree.put(ADMIN, "pppp_h.dat", jpeg(1750, 1020), age=3)
    tree.put(ADMIN, "pppp_t.dat", jpeg(160, 100), age=1)
    config = SimpleNamespace(wechat_data_dir=str(tree.root), image_vision_max_bytes=50 * 1024 * 1024,
                             image_vision_lookback_sec=900)
    msg = {"timestamp": tree.now, "media_base_dir": str(tree.root), "chat_id": ADMIN, "media_hint": ""}
    assert size(vision.find_recent_image_path(config, [msg])) == (1750, 1020)
    tree.account.joinpath("msg", "attach", md5(ADMIN), time.strftime("%Y-%m"), "Img", "pppp_h.dat").unlink()
    assert size(vision.find_recent_image_path(config, [msg])) == (160, 100)         # the thumbnail is all there is
    config.image_allow_thumbnail = False
    assert vision.find_recent_image_path(config, [msg]) == ""


# ── the relay ────────────────────────────────────────────────────────────

def test_the_relay_asks_for_the_chats_folder_and_no_thumbnail(tree):
    from src.relay.attachments import Held, resolve
    tree.put(ADMIN, "qqqq_h.dat", jpeg(1750, 1020), age=3)
    tree.put(ADMIN, "qqqq_t.dat", jpeg(160, 100), age=1)
    tree.put(GROUP, "rrrr_h.dat", jpeg(800, 800), age=1)
    msg = {"timestamp": tree.now, "media_base_dir": str(tree.root), "chat_id": ADMIN, "media_hint": ""}
    path, name, problem = resolve(SimpleNamespace(wechat_data_dir=str(tree.root)), Held("image", msg, tree.now), 1)
    assert name == "图片-1.jpg" and not problem and size(path) == (1750, 1020)


def test_the_relay_says_why_it_falls_back_to_a_screenshot(tree, caplog):
    from src.relay.attachments import Held, resolve
    tree.put(ADMIN, "ssss_t.dat", jpeg(160, 100), age=1)                  # only a thumbnail: not good enough
    shots = tree.root / "shot.jpg"
    shots.write_bytes(jpeg(500, 400))
    msg = {"timestamp": tree.now, "media_base_dir": str(tree.root), "chat_id": ADMIN, "media_hint": "",
           "_capture_media_screenshot": lambda: str(shots)}
    with caplog.at_level(logging.INFO):
        path, name, problem = resolve(SimpleNamespace(wechat_data_dir=str(tree.root)), Held("image", msg, tree.now), 2)
    assert name == "聊天截图-2.jpg" and path == str(shots)
    assert "no picture file found for the message" in caplog.text and "screenshot of the chat window" in caplog.text
    assert "only a thumbnail" in caplog.text


# ── the md5 of the picture in the message ────────────────────────────────

def test_the_md5_is_taken_from_plain_and_from_compressed_xml():
    zstandard = pytest.importorskip("zstandard")
    from src.wechat.wcdb_backend import _extract_media_hint
    xml = '<msg><img aeskey="x" md5="0123456789abcdef0123456789abcdef" length="12345" /></msg>'
    blob = zstandard.ZstdCompressor().compress(xml.encode()).hex()
    assert _extract_media_hint(xml) == "0123456789abcdef0123456789abcdef"
    assert _extract_media_hint(blob) == "0123456789abcdef0123456789abcdef"
    assert _extract_media_hint("28b52ffd" + "00" * 8) == ""                    # not a valid frame: no md5, no crash
    assert _extract_media_hint("") == ""


# ── the key file ─────────────────────────────────────────────────────────

def test_the_log_says_which_key_file_is_used_and_whether_the_keys_are_there(tmp_path, caplog):
    account = tmp_path / "xwechat_files" / ACCOUNT
    keys = K.ImageKeys(path=tmp_path / "keys.json", scan=lambda block: None)
    with caplog.at_level(logging.INFO):
        keys.keys(account, need_aes=False)
        keys.keys(account, need_aes=False)
    lines = [r.getMessage() for r in caplog.records if "Image keys of" in r.getMessage()]
    assert len(lines) == 1                                                       # once per account
    assert str((tmp_path / "keys.json").resolve()) in lines[0] and "AES MISSING" in lines[0]
