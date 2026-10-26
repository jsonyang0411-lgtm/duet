"""SILK/AMR audio decoder — converts WeChat voice files to WAV.

Uses pysilk (silk-python) for SILK v3 decoding and ffmpeg for AMR.
WeChat wraps SILK data with a custom header that must be stripped first.
"""

import io
import logging
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# WeChat SILK sample rate (standard for WeChat voice messages)
WECHAT_SILK_SAMPLE_RATE = 24000

# SILK magic bytes in WeChat's custom header
_SILK_MAGIC = b"#!SILK_V3"


class DecodeError(Exception):
    """Raised when audio decoding fails."""


def _strip_wechat_silk_header(data: bytes) -> bytes:
    """Ensure SILK data has standard SILK v3 header.

    WeChat prepends a 1-byte flag (0x02) before "#!SILK_V3", or in WCDB
    stores frames without the magic header.  pysilk requires data to start
    with "#!SILK_V3".
    """
    idx = data.find(_SILK_MAGIC)
    if idx != -1:
        return data[idx:]
    return _SILK_MAGIC + data


class SilkDecoder:
    """Decode WeChat SILK/AMR voice files to WAV using pysilk + ffmpeg."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decode(self, audio_path: Path) -> Path:
        """Decode a voice file to a temporary WAV file.

        Args:
            audio_path: Path to .silk or .amr file.

        Returns:
            Path to the decoded .wav temporary file.

        Raises:
            DecodeError: If decoding fails.
        """
        suffix = audio_path.suffix.lower()
        if suffix == ".silk":
            return self._decode_silk(audio_path)
        elif suffix == ".amr":
            return self._decode_amr(audio_path)
        else:
            raise DecodeError(f"Unsupported audio format: {suffix}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def decode_bytes(self, raw: bytes) -> Path:
        """Decode raw SILK bytes to a temporary WAV file."""
        if not raw:
            raise DecodeError("Empty SILK bytes")

        silk_data = _strip_wechat_silk_header(raw)
        if not silk_data:
            raise DecodeError("SILK payload empty after header strip")

        try:
            from pysilk import decode as silk_decode
        except ImportError:
            raise DecodeError(
                "pysilk not installed. Run: pip install silk-python"
            )

        inp = io.BytesIO(silk_data)
        out = io.BytesIO()
        try:
            silk_decode(inp, out, WECHAT_SILK_SAMPLE_RATE)
        except Exception as exc:
            raise DecodeError(f"pysilk decode failed: {exc}") from exc

        pcm_data = out.getvalue()
        if not pcm_data:
            raise DecodeError("pysilk produced empty PCM")

        return self._pcm_to_wav(pcm_data, WECHAT_SILK_SAMPLE_RATE)

    def _decode_silk(self, silk_path: Path) -> Path:
        """Decode .silk → .wav via pysilk."""
        raw = silk_path.read_bytes()
        if not raw:
            raise DecodeError(f"Empty SILK file: {silk_path}")
        return self.decode_bytes(raw)

    def _decode_amr(self, amr_path: Path) -> Path:
        """Decode .amr → .wav via ffmpeg subprocess."""
        wav_path = Path(tempfile.mkstemp(suffix=".wav")[1])
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(amr_path),
                    "-ar", "16000", "-ac", "1", "-f", "wav",
                    str(wav_path),
                ],
                capture_output=True,
                check=True,
                timeout=10,
            )
        except FileNotFoundError:
            raise DecodeError("ffmpeg not found — install ffmpeg to decode AMR files")
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            raise DecodeError(f"ffmpeg AMR decode failed: {stderr[:200]}")
        except subprocess.TimeoutExpired:
            raise DecodeError("ffmpeg AMR decode timed out")

        return wav_path

    # ------------------------------------------------------------------
    # PCM → WAV helper
    # ------------------------------------------------------------------

    @staticmethod
    def _pcm_to_wav(pcm_data: bytes, sample_rate: int,
                    channels: int = 1, bits_per_sample: int = 16) -> Path:
        """Wrap raw PCM bytes in a WAV container → temporary file."""
        wav_path = Path(tempfile.mkstemp(suffix=".wav")[1])
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(bits_per_sample // 8)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        logger.debug("Wrote temp WAV: %s (%.1f KB)", wav_path.name, len(pcm_data) / 1024)
        return wav_path
