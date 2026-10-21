"""Local Whisper uses a model the console already downloaded, wherever it is."""

import sys
import types
from pathlib import Path

import pytest

try:
    import opencc  # noqa: F401
except ImportError:                    # not needed here: only the model loading is tested
    sys.modules.setdefault("opencc", types.SimpleNamespace(OpenCC=object))
from src.voice import asr  # noqa: E402


def downloaded(root: Path, size="small"):
    snapshot = root / f"models--Systran--faster-whisper-{size}" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"x")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def loaded(monkeypatch):
    roots = []

    class WhisperModel:
        def __init__(self, size, device, compute_type, download_root):
            roots.append(download_root)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    return roots


def test_the_bots_own_folder_comes_first(home, tmp_path, loaded):
    own = tmp_path / "data" / "models"
    downloaded(own)
    downloaded(home / ".cache" / "huggingface")
    assert asr.find_downloaded_whisper("small", str(own)) == str(own)
    asr.LocalWhisperASR("small", model_dir=str(own))._get_model()
    assert loaded == [str(own)]


@pytest.mark.parametrize("where", [".cache/huggingface", ".cache/huggingface/hub"])
def test_a_console_download_is_used_where_it_is(home, tmp_path, loaded, where):
    downloaded(home / where)
    own = tmp_path / "data" / "models"
    asr.LocalWhisperASR("small", model_dir=str(own))._get_model()
    assert loaded == [str(home / where)]


def test_nothing_downloaded_goes_to_the_bots_folder(home, tmp_path, loaded):
    downloaded(home / ".cache" / "huggingface", size="medium")       # another size does not count
    own = tmp_path / "data" / "models"
    assert asr.find_downloaded_whisper("small", str(own)) is None
    asr.LocalWhisperASR("small", model_dir=str(own))._get_model()
    assert loaded == [str(own)]
