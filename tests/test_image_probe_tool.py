"""tools/image_probe.py: the read-only look at WeChat 4's picture files (a synthetic tree stands in for the PC)."""

import importlib.util
import json
from pathlib import Path

import pytest

from test_image_lookup import ACCOUNT, ADMIN, GROUP, Tree, jpeg, md5  # noqa: F401  (the fixtures' helpers)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("image_probe_tool", ROOT / "tools" / "image_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tree(tmp_path, monkeypatch):
    t = Tree(tmp_path, monkeypatch)
    monkeypatch.setenv("WECHAT_DATA_DIR", str(t.root))
    monkeypatch.setenv("ADMIN_WXID", ADMIN)
    return t


def probe(tool, tree, capsys, *argv):
    code = tool.main(list(argv), root=tree.root.parent)
    return code, capsys.readouterr().out


def test_it_lists_the_files_of_the_chat_and_what_each_decodes_to(tool, tree, capsys):
    tree.put(ADMIN, "aaaa_h.dat", jpeg(1750, 1020), age=20)
    tree.put(ADMIN, "aaaa_t.dat", jpeg(160, 100), age=18)
    tree.put(ADMIN, "bbbb.dat", b"", age=5, wxgf=True)
    code, out = probe(tool, tree, capsys)
    assert code == 0
    assert md5(ADMIN) in out and "EXISTS" in out
    assert "HD (_h)" in out and "thumbnail (_t)" in out and "plain" in out
    assert "OK 1750x1020 jpeg" in out and "OK 160x100 jpeg" in out
    assert "wxgf" in out and "not supported" in out


def test_the_bots_pick_is_reported(tool, tree, capsys):
    tree.put(ADMIN, "cccc_h.dat", jpeg(1750, 1020), age=10)
    _, out = probe(tool, tree, capsys)
    assert "OK, the original: 1750x1020" in out


def test_a_thumbnail_alone_is_not_the_original(tool, tree, capsys):
    tree.put(ADMIN, "dddd_t.dat", jpeg(160, 100), age=10)
    _, out = probe(tool, tree, capsys)
    assert "NOTHING" in out and "screenshot" in out


def test_a_sticker_nearer_in_time_does_not_hide_the_picture(tool, tree, capsys):
    tree.put(ADMIN, "eeee_h.dat", jpeg(1750, 1020), age=40)
    tree.put(ADMIN, "ffff.dat", b"", age=2, wxgf=True)
    _, out = probe(tool, tree, capsys)
    assert "NOTHING" not in out or "OK, the original" in out
    assert "wxgf" in out


def test_a_missing_key_is_said_not_hidden(tool, tree, capsys):
    (tree.root.parent / "data" / "wechat_image_keys.json").write_text("{}", encoding="utf-8")
    tree.put(ADMIN, "gggg_h.dat", jpeg(1750, 1020), age=10)
    _, out = probe(tool, tree, capsys)
    assert "AES key missing" in out and "key file:" in out


def test_without_a_key_file_it_says_where_it_looked(tool, tree, capsys):
    (tree.root.parent / "data" / "wechat_image_keys.json").unlink()
    tree.put(ADMIN, "hhhh_h.dat", jpeg(1750, 1020), age=10)
    _, out = probe(tool, tree, capsys)
    assert "NOT THERE" in out and "wechat_image_keys.json" in out


def test_a_chat_folder_that_is_not_there_is_said(tool, tree, capsys):
    tree.put("someone_else", "iiii_h.dat", jpeg(640, 480), age=10)
    _, out = probe(tool, tree, capsys, "--chat", "wxid_nobody")
    assert "NOT THERE" in out and md5("wxid_nobody") in out


def test_without_a_chat_every_folder_is_listed_with_a_short_name(tool, tree, capsys, monkeypatch):
    monkeypatch.delenv("ADMIN_WXID")
    tree.put(GROUP, "jjjj_h.dat", jpeg(800, 800), age=10)
    _, out = probe(tool, tree, capsys)
    assert md5(GROUP)[:8] in out and "no --chat given" in out
    assert GROUP not in out                                   # the chat id is not guessed, only the folder name shown


def test_it_only_reads(tool, tree, capsys, monkeypatch):
    from src.wechat import image_key
    monkeypatch.setattr(image_key, "scan_wechat_for_key", lambda block: pytest.fail("it must not search memory"))
    tree.put(ADMIN, "kkkk_h.dat", jpeg(1750, 1020), age=10)
    before = sorted(p.name for p in tree.root.parent.rglob("*"))
    probe(tool, tree, capsys)
    assert sorted(p.name for p in tree.root.parent.rglob("*")) == before        # nothing written, not even the decoded picture


def test_no_account_folder(tool, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WECHAT_DATA_DIR", str(tmp_path / "nothing"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert tool.main([], root=tmp_path) == 1
    assert "no account folder" in capsys.readouterr().out
