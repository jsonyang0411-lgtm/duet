"""WeChat 4 picture files: AES-128, the .dat layout, finding the keys, and decoding (src/wechat/media.py)."""

import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.wechat import image_key as K

AES_KEY = b"ab3f9c0d1e2f4a5b"          # 16 of a 32-character run in WeChat's memory
XOR = 0x37


def jpeg(color=(200, 30, 30), size=(64, 48)) -> bytes:
    """A JPEG of noise on ``color``: big enough for the 1 KB AES head and the 1 KB XOR tail."""
    out = io.BytesIO()
    noise = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    Image.blend(Image.new("RGB", size, color), noise, 0.5).save(out, "JPEG", quality=95)
    data = out.getvalue()
    assert len(data) > 2048 or size[0] * size[1] < 100, len(data)
    return data


def account(tmp_path, name="wxid_abc_1234") -> Path:
    folder = tmp_path / "xwechat_files" / name
    (folder / "msg" / "attach" / "f00d" / "2026-10" / "Img").mkdir(parents=True)
    return folder


def write_dat(acc: Path, name: str, picture: bytes, version=2, key=AES_KEY, xor=XOR) -> Path:
    path = acc / "msg" / "attach" / "f00d" / "2026-10" / "Img" / name
    path.write_bytes(K.encode_v4(picture, key, xor, version=version))
    return path


# ── AES-128 ───────────────────────────────────────────────────────────────

def test_aes_matches_fips_197():
    key = bytes(range(16))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    assert K.aes_ecb_encrypt(key, plain) == cipher
    assert K.aes_ecb_decrypt(key, cipher) == plain
    key2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")     # FIPS-197 appendix B
    assert K.aes_ecb_encrypt(key2, bytes.fromhex("3243f6a8885a308d313198a2e0370734")).hex() == \
        "3925841d02dc09fbdc118597196a0b32"
    with pytest.raises(ValueError):
        K.aes_ecb_decrypt(b"short", cipher)


# ── the file layout ───────────────────────────────────────────────────────

@pytest.mark.parametrize("version", [1, 2])
def test_a_picture_survives_the_round_trip(version):
    picture = jpeg()
    raw = K.encode_v4(picture, AES_KEY, XOR, version=version)
    assert K.dat_version(raw) == version
    assert K.decode_v4(raw, AES_KEY if version == 2 else None, XOR) == picture


def test_sizes_at_the_edges():
    small = jpeg(size=(4, 4))
    for aes_size in (16, 17, 1024, len(small)):
        raw = K.encode_v4(small, AES_KEY, XOR, aes_size=aes_size)
        assert K.decode_v4(raw, AES_KEY, XOR) == small, aes_size
    assert K.decode_v4(K.encode_v4(small, AES_KEY, XOR, xor_size=0), AES_KEY, None) == small


def test_what_cannot_be_decoded():
    raw = K.encode_v4(jpeg(), AES_KEY, XOR)
    assert K.decode_v4(raw, None, XOR) is None                       # V2 without its key
    assert K.decode_v4(raw, AES_KEY, None) is None                   # an XOR tail without its key
    assert K.decode_v4(b"\xff\xd8\xff not a dat", AES_KEY, XOR) is None
    assert K.decode_v4(raw[:20], AES_KEY, XOR) is None               # cut short
    assert K.dat_version(b"\x07\x08V3\x08\x07") == 0


# ── finding the keys ──────────────────────────────────────────────────────

def test_the_xor_key_comes_from_the_thumbnails(tmp_path):
    acc = account(tmp_path)
    for i in range(3):
        write_dat(acc, f"t{i}_t.dat", jpeg((i * 40, 0, 0)))
    (acc / "msg" / "attach" / "f00d" / "2026-10" / "Img" / "odd_t.dat").write_bytes(b"\x00\x01")   # outvoted
    assert K.xor_key_from(K.thumbnails(acc)) == XOR
    assert K.v2_block_from(K.thumbnails(acc)) is not None
    assert K.xor_key_from([]) is None and K.v2_block_from([tmp_path / "missing"]) is None
    odd = tmp_path / "odd.dat"
    odd.write_bytes(b"\x00\x01")                                    # not the end of a JPEG under any key
    assert K.xor_key_from([odd, odd]) is None


def test_the_aes_key_is_found_in_memory():
    block = K.encode_v4(jpeg(), AES_KEY, XOR)[15:31]
    run = AES_KEY + b"7c1e5d9a0b3f6e2d"                               # the 32-character run
    memory = [b"\x00" * 50 + b"0123456789abcdef0123456789abcdef" + b"\x00",       # a different run
              b"x" + b"\x00" + run + b"\x01" + b"zz" + b"\x00" + run + b"\x00",
              b"glued" + run + b"\x00"]                                   # inside a longer word: no
    assert K.find_aes_key(memory, block) == AES_KEY
    assert K.find_aes_key([b"glued" + run + b"\x00"], block) is None
    assert K.find_aes_key([b"\x00" + run + b"abcdefgh\x00"], block) is None    # a 40-character run
    assert K.find_aes_key([b"\x00" + run.upper() + b"\x00"], block) is None


def test_the_search_gives_up_at_the_deadline():
    block = K.encode_v4(jpeg(), AES_KEY, XOR)[15:31]
    run = AES_KEY + b"7c1e5d9a0b3f6e2d"
    chunks = [b"\x00" + os.urandom(16).hex().encode() + b"\x00", b"\x00" + run + b"\x00"]
    assert K.find_aes_key(chunks, block, deadline=time.monotonic() - 1) is None   # stopped after chunk 1
    assert K.find_aes_key(chunks, block) == AES_KEY


def test_the_keys_are_cached_and_a_failed_search_waits(tmp_path):
    acc = account(tmp_path)
    write_dat(acc, "a_t.dat", jpeg())
    scans = []

    def scan(block):
        scans.append(block)
        return AES_KEY if len(scans) > 1 else None

    clock = SimpleNamespace(now=0.0)
    keys = K.ImageKeys(tmp_path / "keys.json", scan=scan, clock=lambda: clock.now)
    assert keys.keys(acc, need_aes=True) == (None, XOR)                 # the first search fails
    assert keys.keys(acc, need_aes=True) == (None, XOR) and len(scans) == 1   # no new search yet
    clock.now += K.SCAN_RETRY_SEC + 1
    assert keys.keys(acc, need_aes=True) == (AES_KEY, XOR)
    assert json.loads((tmp_path / "keys.json").read_text())["wxid_abc_1234"] == {"xor": XOR, "aes": AES_KEY.decode()}
    again = K.ImageKeys(tmp_path / "keys.json", scan=lambda b: pytest.fail("cached: no search"))
    assert again.keys(acc, need_aes=True) == (AES_KEY, XOR)
    assert K.ImageKeys(tmp_path / "other.json", scan=scan).keys(acc, need_aes=False) == (None, XOR)


def test_the_account_folder_of_a_picture(tmp_path):
    acc = account(tmp_path)
    path = write_dat(acc, "p.dat", jpeg())
    assert K.account_dir_of(path) == acc
    assert K.account_dir_of(tmp_path / "loose.dat") is None


# ── through media.py and the relay ──────────────────────────────────────────

@pytest.fixture
def known_keys(tmp_path, monkeypatch):
    keys = K.ImageKeys(tmp_path / "keys.json", scan=lambda block: AES_KEY if K.key_opens(AES_KEY, block) else None)
    monkeypatch.setattr(K, "_keys", keys)
    return keys


def test_media_decodes_a_wechat_4_picture(tmp_path, known_keys):
    from src.wechat.media import decode_wechat_dat_image, readable_image
    acc = account(tmp_path)
    write_dat(acc, "x_t.dat", jpeg((10, 10, 10)))
    picture = jpeg((0, 200, 0))
    path = write_dat(acc, "big.dat", picture)
    out = decode_wechat_dat_image(path, tmp_path / "cache")
    assert out is not None and out.suffix == ".jpg" and out.read_bytes() == picture
    assert readable_image(str(path), cache_dir=tmp_path / "cache") == str(out)
    v1 = write_dat(acc, "old.dat", picture, version=1)
    assert decode_wechat_dat_image(v1, tmp_path / "cache").read_bytes() == picture


def test_a_picture_whose_key_cannot_be_found(tmp_path, monkeypatch):
    from src.wechat.media import decode_wechat_dat_image
    monkeypatch.setattr(K, "_keys", K.ImageKeys(tmp_path / "keys.json", scan=lambda block: None))
    acc = account(tmp_path)
    path = write_dat(acc, "big.dat", jpeg())
    assert decode_wechat_dat_image(path, tmp_path / "cache") is None


def test_the_relay_sends_the_original_picture(tmp_path, known_keys):
    from src.relay.attachments import Held, resolve
    acc = account(tmp_path)
    write_dat(acc, "x_t.dat", jpeg((10, 10, 10)))
    picture = jpeg((0, 0, 200), (320, 240))
    path = write_dat(acc, "big.dat", picture)
    msg = {"msg_type": 3, "media_path": str(path), "timestamp": time.time(),
           "_capture_media_screenshot": lambda: pytest.fail("no screenshot needed")}
    found, name, problem = resolve(SimpleNamespace(wechat_data_dir=""), Held("image", msg, 0), 1)
    assert name == "图片-1.jpg" and problem == "" and Path(found).read_bytes() == picture


# ── the check tool ────────────────────────────────────────────────────────

def load_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("image_key_check", Path(__file__).resolve().parents[1]
                                                  / "tools/image_key_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_check_tool_decodes_a_picture(tmp_path, monkeypatch, capsys):
    tool = load_tool()
    monkeypatch.chdir(tmp_path)
    acc = account(tmp_path)
    write_dat(acc, "a_t.dat", jpeg(), version=1)
    picture = jpeg((0, 0, 255), (200, 100))
    write_dat(acc, "big.dat", picture, version=1)
    monkeypatch.setenv("WECHAT_DATA_DIR", str(tmp_path / "xwechat_files"))
    monkeypatch.setattr(K, "scan_wechat_for_key", lambda block: pytest.fail("V1 needs no search"))
    assert tool.main([], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "V1 2, V2 0" in out and "XOR key: found" in out and "200x100" in out
    assert (tmp_path / "data/media_cache/image_key_check.jpg").read_bytes() == picture
    assert AES_KEY.decode() not in out


def test_the_check_tool_reports_a_missing_v2_key(tmp_path, monkeypatch, capsys):
    tool = load_tool()
    monkeypatch.chdir(tmp_path)
    acc = account(tmp_path)
    write_dat(acc, "a_t.dat", jpeg())
    write_dat(acc, "big.dat", jpeg())
    monkeypatch.setenv("WECHAT_DATA_DIR", str(tmp_path / "xwechat_files"))
    assert tool.main(["--no-scan"], root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "AES key (V2): NOT FOUND" in out and "could not be decoded" in out
    monkeypatch.setenv("WECHAT_DATA_DIR", str(tmp_path / "nothing"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    assert tool.main([], root=tmp_path) == 1 and "no account folder" in capsys.readouterr().out

