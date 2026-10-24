"""WeChat 4 image files (``.dat``): their keys and how to decode them.

WeChat 4 keeps pictures in ``<account>/msg/attach/…/Img/*.dat`` in this layout
(little-endian)::

    [0:6)    signature 07 08 56 32 08 07 ("V2"; V1 = 07 08 56 31 08 07)
    [6:10)   aes_size
    [10:14)  xor_size
    [14]     one pad byte
    [15:15+aligned)            AES-128-ECB, aligned = aes_size rounded up to 16 (+16 if exact)
    [.. : len - xor_size)      plain bytes
    [len - xor_size : len)     each byte XOR xor_key

V1 uses a fixed AES key; V2 a key per account that only the running WeChat
holds: a run of 32 lowercase letters/digits in its memory whose first 16
characters decrypt a thumbnail (``*_t.dat``) to a JPEG start (FF D8 FF).  The
XOR key follows from the thumbnails' last two bytes (a JPEG ends FF D9).
Found keys are cached in ``data/wechat_image_keys.json`` per account folder.
The memory is only read, never written.

AES is done here in plain Python (no extra dependency): one block per tried
key, and at most a few dozen blocks per picture.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

SIG_V1 = b"\x07\x08\x56\x31\x08\x07"
SIG_V2 = b"\x07\x08\x56\x32\x08\x07"
V1_KEY = b"cfcd208495d565ef"                     # md5("0")[:16]
KEYS_FILE = Path("data") / "wechat_image_keys.json"
SCAN_RETRY_SEC = 600                             # after a failed memory scan, wait this long
SCAN_TIME_LIMIT_SEC = 180
MAX_REGION_BYTES = 100 * 1024 * 1024
_CANDIDATE = re.compile(rb"(?<![a-z0-9])([a-z0-9]{32})(?![a-z0-9])")


# ── AES-128 (ECB, one block at a time) ────────────────────────────────────

def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _tables():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i], log[x] = x, i
        x ^= _xtime(x)                                   # times 3, a generator
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    sbox = [0] * 256
    for a in range(256):
        s = 0 if a == 0 else exp[255 - log[a]]
        r = s
        for _ in range(4):
            s = ((s << 1) | (s >> 7)) & 0xFF
            r ^= s
        sbox[a] = r ^ 0x63
    inv = [0] * 256
    for a, s in enumerate(sbox):
        inv[s] = a

    def mul(a, b):
        return 0 if a == 0 or b == 0 else exp[log[a] + log[b]]
    return sbox, inv, {n: [mul(a, n) for a in range(256)] for n in (2, 3, 9, 11, 13, 14)}


_SBOX, _INV_SBOX, _MUL = _tables()
# State index = row + 4 * column.  ShiftRows moves row r left by r.
_SHIFT = [(i % 4) + 4 * (((i // 4) + (i % 4)) % 4) for i in range(16)]
_INV_SHIFT = [(i % 4) + 4 * (((i // 4) - (i % 4)) % 4) for i in range(16)]


def _round_keys(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 needs a 16-byte key")
    w = list(key)
    rcon = 1
    while len(w) < 176:
        t = w[-4:]
        if len(w) % 16 == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w += [a ^ b for a, b in zip(w[-16:-12], t)]
    return [w[i:i + 16] for i in range(0, 176, 16)]


def aes_decrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    m9, m11, m13, m14 = _MUL[9], _MUL[11], _MUL[13], _MUL[14]
    s = [b ^ k for b, k in zip(block, round_keys[10])]
    for rnd in range(9, 0, -1):
        s = [_INV_SBOX[s[_INV_SHIFT[i]]] ^ round_keys[rnd][i] for i in range(16)]
        out = []
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c:c + 4]
            out += [m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3], m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3],
                    m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3], m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]]
        s = out
    return bytes(_INV_SBOX[s[_INV_SHIFT[i]]] ^ round_keys[0][i] for i in range(16))


def aes_encrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    m2, m3 = _MUL[2], _MUL[3]
    s = [b ^ k for b, k in zip(block, round_keys[0])]
    for rnd in range(1, 10):
        s = [_SBOX[s[_SHIFT[i]]] for i in range(16)]
        out = []
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c:c + 4]
            out += [m2[a0] ^ m3[a1] ^ a2 ^ a3, a0 ^ m2[a1] ^ m3[a2] ^ a3,
                    a0 ^ a1 ^ m2[a2] ^ m3[a3], m3[a0] ^ a1 ^ a2 ^ m2[a3]]
        s = [b ^ k for b, k in zip(out, round_keys[rnd])]
    return bytes(_SBOX[s[_SHIFT[i]]] ^ round_keys[10][i] for i in range(16))


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    rk = _round_keys(key)
    return b"".join(aes_decrypt_block(rk, data[i:i + 16]) for i in range(0, len(data) - len(data) % 16, 16))


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    rk = _round_keys(key)
    return b"".join(aes_encrypt_block(rk, data[i:i + 16]) for i in range(0, len(data) - len(data) % 16, 16))


# ── the file format ───────────────────────────────────────────────────────

def dat_version(raw: bytes) -> int:
    """1 or 2 for a WeChat 4 picture, 0 for anything else."""
    return {SIG_V1: 1, SIG_V2: 2}.get(raw[:6], 0)


def _aligned(n: int) -> int:
    return n + (16 - n % 16) if n % 16 else n + 16


def decode_v4(raw: bytes, aes_key: Optional[bytes], xor_key: Optional[int]) -> Optional[bytes]:
    """The picture inside a WeChat 4 ``.dat`` (None: not one, or a key is missing)."""
    version = dat_version(raw)
    if not version or len(raw) < 15:
        return None
    key = V1_KEY if version == 1 else aes_key
    if not key:
        return None
    aes_size = int.from_bytes(raw[6:10], "little")
    xor_size = int.from_bytes(raw[10:14], "little")
    head_end = 15 + _aligned(aes_size)
    if head_end > len(raw) or xor_size > len(raw) - head_end:
        return None
    if xor_size and xor_key is None:
        return None
    head = aes_ecb_decrypt(key, raw[15:head_end])
    pad = head[-1] if head else 0
    if 0 < pad <= 16 and head.endswith(bytes([pad]) * pad):
        head = head[:-pad]
    tail_start = len(raw) - xor_size
    tail = bytes(b ^ xor_key for b in raw[tail_start:]) if xor_size else b""
    return head + raw[head_end:tail_start] + tail


def encode_v4(picture: bytes, aes_key: bytes, xor_key: int, version: int = 2,
              aes_size: int = 1024, xor_size: int = 1024) -> bytes:
    """The reverse of ``decode_v4`` (tests build sample files with it)."""
    aes_size = min(aes_size, len(picture))
    xor_size = min(xor_size, len(picture) - aes_size)
    head = picture[:aes_size]
    pad = 16 - len(head) % 16
    head += bytes([pad]) * pad
    key = V1_KEY if version == 1 else aes_key
    tail_start = len(picture) - xor_size
    return ((SIG_V1 if version == 1 else SIG_V2) + aes_size.to_bytes(4, "little") + xor_size.to_bytes(4, "little")
            + b"\x00" + aes_ecb_encrypt(key, head) + picture[aes_size:tail_start]
            + bytes(b ^ xor_key for b in picture[tail_start:]))


# ── finding the keys ──────────────────────────────────────────────────────

def account_dir_of(path: Path) -> Optional[Path]:
    """``…/xwechat_files/<account>`` for a file under ``<account>/msg/…``."""
    parts = Path(path).parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].lower() == "msg":
            return Path(*parts[:i])
    return None


def thumbnails(account: Path, limit: int = 16, look_at: int = 400) -> list[Path]:
    """The newest ``*_t.dat`` thumbnails of an account (they are small JPEGs)."""
    found: list[Path] = []
    root = account / "msg" / "attach"
    for dirpath, _dirs, files in os.walk(root):
        found += [Path(dirpath) / f for f in files if f.endswith("_t.dat")]
        if len(found) >= look_at:
            break

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(found, key=mtime, reverse=True)[:limit]


def xor_key_from(files: Iterable[Path]) -> Optional[int]:
    """The XOR key: a JPEG ends FF D9, so the last two bytes give it (the most common answer)."""
    votes: Counter = Counter()
    for path in files:
        try:
            with open(path, "rb") as f:
                f.seek(-2, os.SEEK_END)
                a, b = f.read(2)
        except (OSError, ValueError):
            continue
        if a ^ 0xFF == b ^ 0xD9:
            votes[a ^ 0xFF] += 1
    return votes.most_common(1)[0][0] if votes else None


def v2_block_from(files: Iterable[Path]) -> Optional[bytes]:
    """The first AES block of a V2 thumbnail: what a candidate key must turn into FF D8 FF."""
    for path in files:
        try:
            with open(path, "rb") as f:
                head = f.read(31)
        except OSError:
            continue
        if len(head) == 31 and head[:6] == SIG_V2:
            return head[15:31]
    return None


def key_opens(key: bytes, block: bytes) -> bool:
    try:
        return aes_decrypt_block(_round_keys(key), block).startswith(b"\xff\xd8\xff")
    except ValueError:
        return False


def find_aes_key(chunks: Iterable[bytes], block: bytes, deadline: Optional[float] = None) -> Optional[bytes]:
    """The key among the 32-character runs in ``chunks`` (memory), tried once each."""
    tried: set[bytes] = set()
    for chunk in chunks:
        for m in _CANDIDATE.finditer(chunk):
            key = m.group(1)[:16]
            if key in tried:
                continue
            tried.add(key)
            if key_opens(key, block):
                logger.info("Image key found after %d candidate(s)", len(tried))
                return key
        if deadline is not None and time.monotonic() > deadline:
            logger.warning("Image key search stopped after %d s (%d candidates)", SCAN_TIME_LIMIT_SEC, len(tried))
            return None
    logger.info("Image key not found among %d candidate(s)", len(tried))
    return None


def wechat_memory(pid: int) -> Iterable[bytes]:  # pragma: no cover - Windows only
    """The readable memory of the WeChat process, region by region (private memory first)."""
    import ctypes as ct
    from ctypes import wintypes

    k32 = ct.WinDLL("kernel32", use_last_error=True)

    class MBI(ct.Structure):
        _fields_ = [("BaseAddress", ct.c_void_p), ("AllocationBase", ct.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                    ("RegionSize", ct.c_size_t), ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]

    k32.OpenProcess.restype = wintypes.HANDLE
    k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ct.c_void_p, ct.POINTER(MBI), ct.c_size_t]
    k32.VirtualQueryEx.restype = ct.c_size_t
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ct.c_void_p, ct.c_void_p, ct.c_size_t,
                                      ct.POINTER(ct.c_size_t)]
    handle = k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        raise OSError(f"cannot open the WeChat process (error {ct.get_last_error()})")
    readable = 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80
    try:
        regions, address, info = [], 0, MBI()
        while k32.VirtualQueryEx(handle, ct.c_void_p(address), ct.byref(info), ct.sizeof(info)):
            size = int(info.RegionSize)
            if (info.State == 0x1000 and info.Protect & readable and not info.Protect & 0x100
                    and 0 < size <= MAX_REGION_BYTES):
                regions.append((info.Type != 0x20000, int(info.BaseAddress or 0), size))   # private first
            address = int(info.BaseAddress or 0) + size
            if address >= 0x7FFFFFFFFFFF:
                break
        for _, base, size in sorted(regions):
            buf = ct.create_string_buffer(size)
            got = ct.c_size_t(0)
            if k32.ReadProcessMemory(handle, ct.c_void_p(base), buf, size, ct.byref(got)) and got.value:
                yield buf.raw[:got.value]
    finally:
        k32.CloseHandle(handle)


def scan_wechat_for_key(block: bytes) -> Optional[bytes]:  # pragma: no cover - Windows only
    if sys.platform != "win32":
        return None
    from .extract_key import _find_wechat_pid
    pid = _find_wechat_pid()
    if not pid:
        logger.warning("Image key: WeChat is not running")
        return None
    started = time.monotonic()
    key = find_aes_key(wechat_memory(pid), block, deadline=started + SCAN_TIME_LIMIT_SEC)
    logger.info("Image key search took %.1f s (%s)", time.monotonic() - started, "found" if key else "not found")
    return key


class ImageKeys:
    """The keys of each account folder: cached on disk, found when first needed."""

    def __init__(self, path: Path = KEYS_FILE, scan: Callable[[bytes], Optional[bytes]] = scan_wechat_for_key,
                 clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self.scan = scan
        self.clock = clock
        self.lock = threading.Lock()
        self.failed_at: dict[str, float] = {}

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def keys(self, account: Path, need_aes: bool, allow_scan: bool = True) -> tuple[Optional[bytes], Optional[int]]:
        """``(aes_key, xor_key)`` of ``account``; the AES key only when ``need_aes`` (a V2 file)."""
        with self.lock:
            data = self._load()
            entry = data.get(account.name) or {}
            changed = False
            if entry.get("xor") is None:
                xor = xor_key_from(thumbnails(account))
                if xor is not None:
                    entry["xor"], changed = xor, True
            if need_aes and not entry.get("aes") and allow_scan:
                last_failure = self.failed_at.get(account.name)
                if last_failure is None or self.clock() - last_failure > SCAN_RETRY_SEC:
                    block = v2_block_from(thumbnails(account))
                    key = self.scan(block) if block else None
                    if key:
                        entry["aes"], changed = key.decode("ascii"), True
                        self.failed_at.pop(account.name, None)
                    else:
                        self.failed_at[account.name] = self.clock()
                        logger.warning("Image key for %s not found%s", account.name,
                                       "" if block else " (no V2 thumbnail to test keys on)")
            if changed:
                data[account.name] = entry
                self._save(data)
        aes = entry.get("aes")
        return (aes.encode("ascii") if aes else None), entry.get("xor")


_keys = ImageKeys()


def decode_file(path: Path, raw: Optional[bytes] = None, keys: Optional[ImageKeys] = None) -> Optional[bytes]:
    """The picture of a WeChat 4 ``.dat`` file, finding the account's keys if needed."""
    raw = Path(path).read_bytes() if raw is None else raw
    version = dat_version(raw)
    account = account_dir_of(Path(path))
    if not version or account is None:
        return None
    aes, xor = (keys or _keys).keys(account, need_aes=version == 2)
    return decode_v4(raw, aes, xor)
