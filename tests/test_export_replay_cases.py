"""Tests for the replay-case export / redaction / labeling tools (Phase 0).

Everything runs against synthetic data under ``tmp_path``; no real database or
runtime file of the bot is touched.
"""

import importlib.util
import itertools
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.append(str(TOOLS_DIR))

import export_replay_cases as exp  # noqa: E402
import label_replay_cases as lab  # noqa: E402
import replay_redaction as rr  # noqa: E402

T0 = 1_700_000_000                       # 2023-11-14 22:13:20 UTC
DAY0 = exp.parse_time_arg("2024-01-01")  # midnight of 2024-01-01 in UTC+8
GROUP_A = "11111111111@chatroom"
GROUP_B = "22222222222@chatroom"
PRIVATE = "wxid_privatefriend01"
BOT = "小助手"
SALT = "unit-test-salt-7"

HEX_BLOB = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" * 3

# Real-looking identifiers that must never appear in an exported file.
RAW_SECRETS = [
    "13812345678", "010-12345678", "alice@example.com", "weixin.example.com",
    "wxid_alice123", "wxid_bob456", "wxid_zzz999888", "carol_custom_id",
    "110101199003071234", "6222021234567890123", "12345678",
    "Alice陈", "老王", "Carol", "秘密项目X", "咖啡爱好者交流群",
    "11111111111", "22222222222", "wxid_privatefriend01", "私聊朋友", "Zed",
    "mid-", HEX_BLOB[:40], SALT,
]


# --------------------------------------------------------------------------
# Synthetic database helpers
# --------------------------------------------------------------------------


_FALLBACK_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
    chat_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '', msg_type INTEGER NOT NULL DEFAULT 1,
    timestamp INTEGER NOT NULL, created_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE history_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
    chat_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '', msg_type INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT '', timestamp INTEGER NOT NULL,
    imported_at INTEGER NOT NULL DEFAULT 0);
"""


def _create_bot_db(path: Path) -> sqlite3.Connection:
    """The bot's real schema module when it can be loaded, else a copy of it."""
    try:
        spec = importlib.util.spec_from_file_location(
            "replay_test_bot_schema", ROOT / "src" / "db" / "schema.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.initialize_db(str(path))
    except Exception:  # schema moved during the refactor: use the fallback
        conn = sqlite3.connect(path)
        conn.executescript(_FALLBACK_DDL)
        return conn


class Db:
    """Build a throw-away database shaped like the bot's messages.db."""

    def __init__(self, path: Path):
        self.path = path
        self.conn = _create_bot_db(path)
        self._ids = itertools.count(1)

    def add(self, chat, ts, sender_id, sender_name, content, msg_type=1):
        self.conn.execute(
            "INSERT INTO messages (message_id, chat_id, sender_id, sender_name,"
            " content, msg_type, timestamp) VALUES (?,?,?,?,?,?,?)",
            (f"mid-{next(self._ids):05d}", chat, sender_id, sender_name,
             content, msg_type, ts),
        )

    def add_history(self, chat, ts, sender_id, sender_name, content, msg_type=1):
        self.conn.execute(
            "INSERT INTO history_messages (message_id, chat_id, sender_id,"
            " sender_name, content, msg_type, source, timestamp)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"mid-h{next(self._ids):05d}", chat, sender_id, sender_name,
             content, msg_type, "wechat_backup", ts),
        )

    def close(self):
        self.conn.commit()
        self.conn.close()
        return self.path


def _pii_chat(db: Db) -> None:
    """Group A: a small conversation full of identifiers (4 decision points)."""
    A, B, C = ("wxid_alice123", "Alice陈"), ("wxid_bob456", "老王"), ("carol_custom_id", "Carol")
    db.add(GROUP_A, T0 + 0, *A, "早")
    db.add(GROUP_A, T0 + 3, *B, "早啊 我的手机13812345678 座机010-12345678")
    db.add(GROUP_A, T0 + 6, *A, "今天去吗 邮箱 alice@example.com")          # burst end
    db.add(GROUP_A, T0 + 60, *B,
           f"@{BOT} 帮我查下 https://weixin.example.com/s/abc?x=1 或 weixin.example.com/p")
    db.add(GROUP_A, T0 + 75, *C, "老王你怎么又迟到 加我 wxid_zzz999888 QQ:12345678")
    db.add(GROUP_A, T0 + 200, *A, HEX_BLOB)                              # raw blob, type 1
    db.add(GROUP_A, T0 + 205, *C, "[图片]", 3)
    db.add(GROUP_A, T0 + 400, *B,
           "咖啡爱好者交流群 秘密项目X 身份证110101199003071234 "
           "银行卡6222021234567890123 @Carol @Alice陈")


def _write_aux_files(tmp_path: Path) -> tuple[Path, Path]:
    blocklist = tmp_path / "blocklist.txt"
    blocklist.write_text("# comment\n秘密项目X\n\n", encoding="utf-8")
    group_names = tmp_path / "group_names.json"
    group_names.write_text(
        json.dumps({GROUP_A: {"name": "咖啡爱好者交流群", "member_count": 3}},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return blocklist, group_names


@pytest.fixture()
def pii_db(tmp_path):
    db = Db(tmp_path / "messages.db")
    _pii_chat(db)
    # a second group that shares one raw sender id, plus a private chat
    db.add(GROUP_B, T0 + 10, "wxid_alice123", "Alice陈", "我在另一个群")
    db.add(GROUP_B, T0 + 40, "wxid_zed999", "Zed", "收到")
    db.add(PRIVATE, T0 + 20, "wxid_privatefriend01", "私聊朋友", "在吗")
    return db.close()


def _export(db_path: Path, out: Path, *extra: str, capsys=None, salt: str | None = SALT):
    argv = ["--db", str(db_path), "--out", str(out), "--bot-name", BOT]
    if salt is not None:
        argv += ["--salt", salt]
    argv += list(extra)
    code = exp.main(argv)
    captured = capsys.readouterr() if capsys is not None else None
    return code, captured


def _read_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------
# Redaction module
# --------------------------------------------------------------------------


def test_pseudonym_letters_follow_spreadsheet_columns():
    labels = [rr.letters_label(i) for i in (0, 1, 25, 26, 27, 51, 52, 701, 702)]
    assert labels == ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"]


@pytest.mark.parametrize("raw, token", [
    ("打我电话13812345678谢谢", "<手机号>"),
    ("电话 138 1234 5678", "<手机号>"),
    ("+86 138-1234-5678", "<手机号>"),
    ("１３８１２３４５６７８", "<手机号>"),                    # full-width digits
    ("座机010-12345678", "<座机>"),
    ("0571-8888 8888", "<座机>"),
    ("邮箱a.b_c@mail.example.com吧", "<邮箱>"),
    ("看 https://a.example.org/x?y=1&z=2 吧", "<链接>"),
    ("看 www.example.org/path", "<链接>"),
    ("看 example.com", "<链接>"),
    ("加我 wxid_abc_123", "<微信号>"),
    ("微信号：zhang_san01", "<微信号>"),
    ("身份证110101199003071234", "<证件号>"),
    ("卡号6222021234567890123", "<银行卡>"),
    ("卡号 6222 0200 1234 5678", "<银行卡>"),
    ("QQ 12345", "<数字串>"),
    ("单号1234567890", "<数字串>"),
    ("ip 192.168.1.20", "<IP地址>"),
    ("电话是一三八零零一三八零零零", "<数字串>"),               # spelled-out digits
    ("key sk-ABCDEF0123456789ABCDEF0123456789abcd", "<长串>"),
])
def test_regex_scrubbing_replaces_each_kind(raw, token):
    out = rr.scrub_text(raw)
    assert token in out
    assert not re.search(r"\d{6,}", out)
    for leaked in ("example", "wxid_abc", "zhang_san01", "192.168"):
        assert leaked not in out


def test_short_numbers_prices_and_dates_survive():
    text = "价格 1,234,567 元 2024-10-16 12:30 版本 1.2.3 验证码 8888"
    assert rr.scrub_text(text) == text


def test_hex_blob_and_media_xml_become_placeholders():
    red = rr.Redactor(salt="s", bot_name=BOT)
    blob = red.redact_message("c", HEX_BLOB, 1)
    assert (blob.text, blob.msg_type) == ("[图片]", 3)   # same as the bot's own rule
    xml = '<msg><img aeskey="k" md5="abc" cdnurl="http://x.example/y"/></msg>'
    assert red.redact_message("c", xml, 3).text == "[图片]"
    assert red.redact_message("c", xml, 47).text == "[表情]"
    title = "<?xml version='1.0'?><msg><appmsg><title>好文章</title></appmsg></msg>"
    assert red.redact_message("c", title, 49).text == "好文章"
    inline = red.redact_message("c", "key=" + "aB3" * 14 + " 结束", 1).text
    assert "aB3aB3" not in inline and "<长串>" in inline


def test_names_and_mentions_use_the_same_pseudonyms_as_sender_fields():
    red = rr.Redactor(salt="s", bot_name=BOT)
    red.register_sender("c", "wxid_a", "Alice陈", 1)
    red.register_sender("c", "wxid_b", "老王", 2)
    red.register_sender("c", "wxid_c", "Tom", 3)
    assert red.sender_label("c", "wxid_a") == "用户A"
    assert red.sender_label("c", "wxid_b") == "用户B"

    en = chr(0x2005)   # WeChat puts an EN SPACE after a picked @mention
    got = red.redact_text("c", f"@老王 看下 Alice陈说的，@Tom{en}好 @{BOT} @所有人")
    assert got == f"@用户B 看下 用户A说的，@用户C{en}好 @{BOT} @所有人"
    # case-insensitive, ASCII word boundaries respected
    assert red.redact_text("c", "tom和Tomato") == "用户C和Tomato"
    # "@all" is kept, but must not swallow a name that merely starts with it
    kept = red.redact_text("c", "@all @所有人注意 @allen")
    assert kept.startswith("@all @所有人注意 @路人") and "allen" not in kept
    # unknown mentions get a stable per-chat stand-in, never the raw name
    first = red.redact_text("c", "@陌生人 你好")
    assert "陌生人" not in first and first.startswith("@路人")
    again = red.redact_text("c", "再叫一次@陌生人")
    assert again.endswith(first.split(" ")[0])


def test_bot_name_is_kept_or_swapped_for_its_alias():
    kept = rr.Redactor(salt="s", bot_name=BOT)
    assert kept.redact_text("c", f"@{BOT} 你好，{BOT}") == f"@{BOT} 你好，{BOT}"
    alias = rr.Redactor(salt="s", bot_name=BOT, bot_alias="Haru")
    assert alias.bot_alias == "Haru"
    assert alias.redact_text("c", f"@{BOT} 你好，{BOT}在吗") == "@Haru 你好，Haru在吗"


def test_other_names_of_the_bot_become_its_name():
    red = rr.Redactor(salt="s", bot_name="杨宇靖", other_names=["qier", " ", "杨宇靖"])
    assert red.redact_text("c", "@qier 在吗，qier你看这个") == "@杨宇靖 在吗，杨宇靖你看这个"
    assert red.redact_text("c", "@QIER 早") == "@杨宇靖 早"
    assert red.stats["bot_name"] >= 2


def test_an_at_by_the_bots_other_name_is_an_at_of_the_bot(tmp_path):
    db = Db(tmp_path / "bot.db")
    db.add(GROUP_A, DAY0 + 100, "wxid_a", "阿明", "今天吃啥")
    db.add(GROUP_A, DAY0 + 200, "wxid_b", "小红", "@qier 帮我选一个")
    path = db.close()
    out = tmp_path / "cases.jsonl"
    code, _ = _export(path, out, "--also-called", "qier")
    assert code == 0
    cases = _read_cases(out)
    trigger = cases[-1]["trigger"]
    assert trigger["is_at_mentioned"] and trigger["content"] == f"@{BOT} 帮我选一个"
    assert "qier" not in out.read_text(encoding="utf-8")
    code, _ = _export(path, tmp_path / "plain.jsonl")
    assert not _read_cases(tmp_path / "plain.jsonl")[-1]["trigger"]["is_at_mentioned"]


def test_short_or_generic_names_are_not_replaced_in_plain_text():
    red = rr.Redactor(salt="s", bot_name=BOT)
    red.register_sender("c", "u1", "我", 1)       # one character
    red.register_sender("c", "u2", "好的", 2)     # generic word
    red.register_sender("c", BOT, BOT, 3)         # same as the bot name
    assert red.redact_text("c", "我说好的，找小助手") == "我说好的，找小助手"
    assert red.redact_text("c", "@好的 来一下") == "@用户B 来一下"


def test_blocklist_group_names_and_system_notices():
    red = rr.Redactor(
        salt="s", bot_name=BOT, blocklist=["秘密项目X", "ACME"],
        group_names={GROUP_A: {"name": "咖啡爱好者交流群"}},
    )
    red.register_chat(GROUP_A, 1)
    out = red.redact_text(GROUP_A, "acme 的秘密项目X，来自咖啡爱好者交流群")
    assert out == "<已屏蔽> 的<已屏蔽>，来自示例群1"
    system = red.redact_message(GROUP_A, '"张三"邀请"李四"加入了群聊', 10000).text
    assert "张三" not in system and "李四" not in system


def test_sentinel_decoding_survives_many_replacements():
    text = " ".join(f"1380000{i:04d}" for i in range(30))
    assert rr.scrub_text(text) == " ".join(["<手机号>"] * 30)


def test_salt_changes_hashes_but_is_never_exposed():
    a = rr.Redactor(salt="one")
    b = rr.Redactor(salt="two")
    c = rr.Redactor(salt="one")
    ids = [x.sender_hash("c", "wxid_x") for x in (a, b, c)]
    assert ids[0] == ids[2] != ids[1]
    assert re.fullmatch(r"u_[0-9a-f]{8}", ids[0])
    assert "one" not in repr(a)
    # per chat: the same raw id gets unrelated hashes in two chats
    assert a.sender_hash("c1", "wxid_x") != a.sender_hash("c2", "wxid_x")
    assert rr.Redactor().salt_was_generated is True


def test_chat_pseudonyms_are_numbered_by_first_activity():
    red = rr.Redactor(salt="s")
    red.register_chat("b@chatroom", 200)
    red.register_chat("a@chatroom", 100)
    red.register_chat("wxid_friend", 50)
    infos = {c: red.chat_info(c) for c in ("a@chatroom", "b@chatroom", "wxid_friend")}
    assert infos["a@chatroom"]["group_name"] == "示例群1"
    assert infos["b@chatroom"]["group_name"] == "示例群2"
    assert infos["wxid_friend"]["is_group"] is False
    assert all("a@chatroom" not in str(v["chat_id"]) or v["chat_id"].startswith("g_")
               for v in infos.values())
    assert infos["a@chatroom"]["chat_id"].endswith("@chatroom")


# --------------------------------------------------------------------------
# Decision points, windows, sampling
# --------------------------------------------------------------------------


def _rows(spec):
    """spec: iterable of (ts, msg_type, content) -> Row list for one chat."""
    return [exp.Row(ts, 0, i + 1, "c", f"s{i % 3}", f"n{i % 3}", content, kind)
            for i, (ts, kind, content) in enumerate(spec)]


def test_burst_end_detection_rules():
    rows = _rows([
        (100, 1, "a"), (103, 1, "b"), (106, 1, "end of burst 1"),   # next at 120: gap 14
        (120, 1, "c"), (129, 1, "d"),                               # next at 140: gap 11
        (140, 3, "[图片]"),                                         # image: never a trigger
        (160, 1, "   "),                                            # blank: skipped
        (190, 1, "same second 1"), (190, 1, "same second 2"),       # next at 200: gap == 10
        (200, 1, "exactly the gap"), (210, 1, "last in chat"),      # gap == 10, then no next
    ])
    found = exp.find_decision_points(rows, burst_gap=10)
    assert [rows[i].content for i in found] == [
        "end of burst 1", "d", "same second 2", "exactly the gap", "last in chat"]
    # a larger gap merges bursts; the last message of a chat always ends one
    found = exp.find_decision_points(rows, burst_gap=12)
    assert [rows[i].content for i in found] == ["end of burst 1", "last in chat"]
    # since/until bound the trigger time ([since, until))
    found = exp.find_decision_points(rows, burst_gap=10, since=107, until=190)
    assert [rows[i].content for i in found] == ["d"]


def test_blob_with_type_1_is_not_a_trigger():
    rows = _rows([(100, 1, HEX_BLOB), (200, 1, "real text")])
    assert [rows[i].content for i in exp.find_decision_points(rows, burst_gap=10)] \
        == ["real text"]


def test_context_and_lookahead_windows_are_inclusive_and_bounded():
    spec = [(1000 + i, 1, f"m{i}") for i in range(50)]          # 50 messages, 1 s apart
    spec = [(1049 - 900, 1, "exactly 900s old"), (1049 - 901, 1, "901s old")] + spec
    spec.sort()
    trig_ts = 1049
    spec += [(trig_ts + 60, 1, "after +60"), (trig_ts + 61, 1, "after +61")]
    rows = _rows(spec)
    idx = next(i for i, r in enumerate(rows) if r.content == "m49")

    ctx, later = exp.window_around(rows, idx, context_count=100,
                                   context_window=900, lookahead=60)
    texts = [r.content for r in ctx]
    assert "exactly 900s old" in texts and "901s old" not in texts
    assert texts[-1] == "m48" and len(ctx) == 49 + 1
    assert [r.content for r in later] == ["after +60"]

    ctx, _ = exp.window_around(rows, idx, context_count=40, context_window=900,
                               lookahead=60)
    assert [r.content for r in ctx] == [f"m{i}" for i in range(9, 49)]   # last 40

    ctx, later = exp.window_around(rows, idx, context_count=0, context_window=900,
                                   lookahead=0)
    assert ctx == [] and later == []


def test_stratified_sampling_is_even_over_chats_and_days_and_deterministic():
    cands = []
    n = itertools.count(1)
    for day in range(6):                      # chat A: 10 per day for 6 days
        for k in range(10):
            cands.append(exp.Candidate("A", DAY0 + day * 86400 + k * 60, 0, next(n), day))
    for k in range(6):                        # chat B: 6 candidates on 2 days
        cands.append(exp.Candidate("B", DAY0 + (k % 2) * 86400 + k, 0, next(n), k % 2))
    picked = exp.stratified_sample(cands, 12, seed=3)
    assert len(picked) == 12 == len(set(picked))
    assert sum(c.chat_id == "B" for c in picked) == 6          # small chat fully used
    a_days = [c.day for c in picked if c.chat_id == "A"]
    assert sorted(a_days) == [0, 1, 2, 3, 4, 5]                # one per day
    assert exp.stratified_sample(cands, 12, seed=3) == picked
    assert exp.stratified_sample(cands, 12, seed=4) != picked
    assert len(exp.stratified_sample(cands, 0, seed=3)) == len(cands)
    assert len(exp.stratified_sample(cands, 500, seed=3)) == len(cands)


@pytest.mark.parametrize("value, expected", [
    ("1700000000", 1700000000),
    ("1700000000000", 1700000000),                       # milliseconds
    ("2024-01-01", DAY0),
    ("20240101", DAY0),
    ("2024-01-01T00:00:00", DAY0),
    ("2024-01-01T00:00:00+08:00", DAY0),
    ("2023-12-31T16:00:00Z", DAY0),
])
def test_parse_time_arg(value, expected):
    assert exp.parse_time_arg(value) == expected


def test_until_date_covers_the_whole_day():
    assert exp.parse_time_arg("2024-01-01", is_until=True) == DAY0 + 86400
    assert exp.parse_time_arg("2024-01-01T12:00:00", is_until=True) == DAY0 + 43200
    with pytest.raises(ValueError):
        exp.parse_time_arg("last tuesday")


# --------------------------------------------------------------------------
# End-to-end export
# --------------------------------------------------------------------------


def test_export_selects_burst_ends_with_correct_context_and_lookahead(
        pii_db, tmp_path, capsys):
    out = tmp_path / "cases.jsonl"
    code, _ = _export(pii_db, out, "--chat-id", GROUP_A, "--order", "chronological",
                      capsys=capsys)
    assert code == 0
    cases = _read_cases(out)
    assert [c["now"] for c in cases] == [T0 + 6, T0 + 60, T0 + 75, T0 + 400]
    assert [c["id"] for c in cases] == [f"exp-{i:06d}" for i in range(1, 5)]

    first, second, third, fourth = cases
    assert [m["offset_sec"] for m in first["context"]] == [-6, -3]
    assert [m["offset_sec"] for m in first["next_messages"]] == [54]      # +54 <= 60
    assert first["trigger"]["offset_sec"] == 0

    assert second["trigger"]["is_at_mentioned"] is True
    assert first["trigger"]["is_at_mentioned"] is False
    assert [m["offset_sec"] for m in second["next_messages"]] == [15]
    assert len(fourth["context"]) == 7                                    # everything before
    assert fourth["next_messages"] == []
    # the type-1 hex blob was normalised exactly like the bot does
    blob = next(m for m in fourth["context"] if m["offset_sec"] == -200)
    assert (blob["content"], blob["msg_type"]) == ("[图片]", 3)
    img = next(m for m in fourth["context"] if m["offset_sec"] == -195)
    assert (img["content"], img["msg_type"]) == ("[图片]", 3)


def test_export_respects_gap_window_and_count_options(pii_db, tmp_path, capsys):
    out = tmp_path / "cases.jsonl"
    _export(pii_db, out, "--chat-id", GROUP_A, "--burst-gap", "100",
            "--order", "chronological", capsys=capsys)
    assert [c["now"] for c in _read_cases(out)] == [T0 + 75, T0 + 400]

    _export(pii_db, out, "--chat-id", GROUP_A, "--context-count", "2",
            "--context-window", "70", "--lookahead", "70",
            "--order", "chronological", capsys=capsys)
    by_now = {c["now"]: c for c in _read_cases(out)}
    offsets = lambda items: [m["offset_sec"] for m in items]
    # @T0+6: window 70 s reaches back to T0-64, two messages exist
    assert offsets(by_now[T0 + 6]["context"]) == [-6, -3]
    assert offsets(by_now[T0 + 6]["next_messages"]) == [54, 69]           # <= +70
    # @T0+60: three older messages in the window, only the latest 2 are kept
    assert offsets(by_now[T0 + 60]["context"]) == [-57, -54]
    assert offsets(by_now[T0 + 60]["next_messages"]) == [15]              # +140 is out
    # @T0+75: window 70 s reaches back to T0+5
    assert offsets(by_now[T0 + 75]["context"]) == [-69, -15]
    # @T0+400: nothing within 70 s before it
    assert by_now[T0 + 400]["context"] == []


def test_no_identifier_or_pii_survives_in_the_output(pii_db, tmp_path, capsys):
    blocklist, group_names = _write_aux_files(tmp_path)
    out = tmp_path / "cases.jsonl"
    code, captured = _export(
        pii_db, out, "--include-private", "--blocklist", str(blocklist),
        "--group-names", str(group_names), "--max-cases", "0", capsys=capsys)
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert text.strip()
    for secret in RAW_SECRETS:
        assert secret not in text, secret
    assert "message_id" not in text
    for stream in (captured.out, captured.err):
        assert SALT not in stream
    # the redaction really happened (not just dropped)
    for token in ("<手机号>", "<座机>", "<邮箱>", "<链接>", "<微信号>", "<数字串>",
                  "<证件号>", "<银行卡>", "<已屏蔽>", "示例群1", "[图片]"):
        assert token in text, token
    # bot name and the at-mention are intentionally kept
    assert f"@{BOT}" in text and f'"bot_name":"{BOT}"' in text


def test_pseudonyms_are_stable_inside_a_chat_and_follow_first_appearance(
        pii_db, tmp_path, capsys):
    out = tmp_path / "cases.jsonl"
    _export(pii_db, out, "--max-cases", "0", "--include-private", capsys=capsys)
    cases = _read_cases(out)
    by_chat: dict[str, dict[str, set[str]]] = {}
    for case in cases:
        people = [*case["context"], case["trigger"], *case["next_messages"]]
        mapping = by_chat.setdefault(case["chat"]["chat_id"], {})
        for p in people:
            mapping.setdefault(p["sender_id"], set()).add(p["sender_name"])
    for chat_id, mapping in by_chat.items():
        for sender_id, names in mapping.items():
            assert len(names) == 1, (chat_id, sender_id)        # id -> one pseudonym
        assert len({n for v in mapping.values() for n in v}) == len(mapping)
        assert all(re.fullmatch(r"u_[0-9a-f]{8}", s) for s in mapping)

    group_a = next(c for c in cases if c["chat"]["group_name"] == "示例群1")
    names = {p["sender_name"] for p in
             [*group_a["context"], group_a["trigger"], *group_a["next_messages"]]}
    assert names <= {"用户A", "用户B", "用户C"}
    # first sender in time order is 用户A, then 用户B, then 用户C
    last = next(c for c in cases if c["now"] == T0 + 400)
    assert last["context"][0]["sender_name"] == "用户A"
    assert last["context"][1]["sender_name"] == "用户B"
    assert last["trigger"]["sender_name"] == "用户B"
    assert "@用户C @用户A" in last["trigger"]["content"]
    third = next(c for c in cases if c["now"] == T0 + 75)
    assert third["trigger"]["sender_name"] == "用户C"
    assert third["trigger"]["content"].startswith("用户B你怎么又迟到")

    # the same raw sender in another chat: independent label space and hash
    alice_a = last["context"][0]["sender_id"]
    group_b = next(c for c in cases if c["chat"]["group_name"] == "示例群2")
    ids_b = {p["sender_id"] for p in
             [*group_b["context"], group_b["trigger"], *group_b["next_messages"]]}
    assert alice_a not in ids_b


def test_schema_fields_and_unknowns_are_null(pii_db, tmp_path, capsys):
    out = tmp_path / "cases.jsonl"
    _export(pii_db, out, "--chat-id", GROUP_A, "--max-cases", "1", capsys=capsys)
    (case,) = _read_cases(out)
    assert list(case) == ["schema", "id", "category", "chat", "bot_name", "now",
                          "context", "trigger", "next_messages", "bot_state",
                          "expect", "source"]
    assert case["schema"] == "replay-case/v1"
    assert case["category"] == "unlabeled"
    assert case["bot_state"] is None and case["expect"] is None
    assert case["source"] == "export"
    assert case["chat"]["is_group"] is True
    assert re.fullmatch(r"g_[0-9a-f]{8}@chatroom", case["chat"]["chat_id"])
    trig = case["trigger"]
    assert trig["is_reply_to_bot"] is None
    assert trig["quoted_content"] == "" and trig["quoted_sender_name"] == ""
    assert list(trig) == ["sender_id", "sender_name", "content", "msg_type",
                          "is_at_mentioned", "is_reply_to_bot", "quoted_content",
                          "quoted_sender_name", "offset_sec"]
    assert set(case["context"][0]) == {"offset_sec", "sender_id", "sender_name",
                                       "content", "msg_type"}


def test_bot_alias_replaces_the_real_bot_name_everywhere(pii_db, tmp_path, capsys):
    out = tmp_path / "cases.jsonl"
    code, _ = _export(pii_db, out, "--chat-id", GROUP_A, "--bot-alias", "Haru",
                      "--order", "chronological", capsys=capsys)
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert BOT not in text                       # not in bot_name, not in the text
    cases = _read_cases(out)
    assert {c["bot_name"] for c in cases} == {"Haru"}
    mention = next(c for c in cases if c["now"] == T0 + 60)
    assert mention["trigger"]["is_at_mentioned"] is True       # decided on the real name
    assert mention["trigger"]["content"].startswith("@Haru 帮我查下")


def test_same_seed_and_salt_give_byte_identical_output(tmp_path, capsys):
    db = Db(tmp_path / "many.db")
    for chat in (GROUP_A, GROUP_B, "33333333333@chatroom"):
        for day in range(4):
            for k in range(12):
                db.add(chat, DAY0 + day * 86400 + k * 120, f"wxid_{k % 4}",
                       f"n{k % 4}", f"消息 {day}-{k}")
    path = db.close()

    outs = []
    for name, seed in (("a", 5), ("b", 5), ("c", 6)):
        out = tmp_path / f"{name}.jsonl"
        code, _ = _export(path, out, "--max-cases", "20", "--seed", str(seed),
                          capsys=capsys)
        assert code == 0
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    assert outs[0] != outs[2]
    assert len(outs[0].splitlines()) == 20
    # without --salt the ids are random but the sampled cases are the same
    out_x, out_y = tmp_path / "x.jsonl", tmp_path / "y.jsonl"
    _export(path, out_x, "--max-cases", "20", "--seed", "5", salt=None, capsys=capsys)
    _export(path, out_y, "--max-cases", "20", "--seed", "5", salt=None, capsys=capsys)
    nows = lambda p: [(c["now"], c["trigger"]["content"]) for c in _read_cases(p)]
    assert nows(out_x) == nows(out_y) == nows(tmp_path / "a.jsonl")
    assert out_x.read_bytes() != out_y.read_bytes()


def _child_env():
    """The environment for running a tool as a script: its output is read as UTF-8,
    so it must write UTF-8 (on a Chinese Windows it would otherwise write GBK)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_output_is_identical_across_processes_and_hash_seeds(tmp_path):
    db = Db(tmp_path / "many.db")
    for chat in (GROUP_A, GROUP_B, "33333333333@chatroom"):
        for day in range(3):
            for k in range(9):
                db.add(chat, DAY0 + day * 86400 + k * 90, f"wxid_{k % 3}",
                       f"昵称{k % 3}", f"今天第{day}天的第{k}句 @昵称{(k + 1) % 3}")
    path = db.close()
    script = TOOLS_DIR / "export_replay_cases.py"
    results = []
    for hash_seed in ("1", "2", "random"):
        env = _child_env()
        env["PYTHONHASHSEED"] = hash_seed
        out = tmp_path / f"run_{hash_seed}.jsonl"
        run = subprocess.run(
            [sys.executable, "-B", str(script), "--db", str(path), "--out", str(out),
             "--bot-name", BOT, "--salt", SALT, "--seed", "11", "--max-cases", "15"],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert run.returncode == 0, run.stderr
        results.append(out.read_bytes())
    assert len(results[0].splitlines()) == 15
    assert results[0] == results[1] == results[2]


def test_end_to_end_sampling_is_spread_over_chats_and_days(tmp_path, capsys):
    db = Db(tmp_path / "strata.db")
    for day in range(5):                       # big chat: 8 bursts per day
        for k in range(8):
            db.add(GROUP_A, DAY0 + day * 86400 + 3600 + k * 300, "wxid_a", "甲", f"a{day}{k}")
    for k in range(5):                         # small chat: 5 bursts
        db.add(GROUP_B, DAY0 + k * 86400 + 7200, "wxid_b", "乙", f"b{k}")
    path = db.close()
    out = tmp_path / "cases.jsonl"
    _export(path, out, "--max-cases", "10", "--seed", "1", capsys=capsys)
    cases = _read_cases(out)
    assert len(cases) == 10
    per_chat = {}
    for c in cases:
        per_chat.setdefault(c["chat"]["group_name"], []).append(c["now"])
    assert sorted(len(v) for v in per_chat.values()) == [5, 5]
    for stamps in per_chat.values():
        assert len({(t - DAY0) // 86400 for t in stamps}) == 5   # one per day


def test_dry_run_prints_counts_and_samples_and_writes_nothing(
        pii_db, tmp_path, capsys):
    blocklist, group_names = _write_aux_files(tmp_path)
    out_dir = tmp_path / "never_created"
    out = out_dir / "cases.jsonl"
    code, captured = _export(
        pii_db, out, "--dry-run", "--blocklist", str(blocklist),
        "--group-names", str(group_names), capsys=capsys)
    assert code == 0
    assert not out.exists() and not out_dir.exists()
    assert not list(tmp_path.glob("*.tmp"))
    text = captured.out
    assert "DRY RUN" in text
    for kind in ("phone", "email", "url", "wxid", "idcard", "bankcard", "payload"):
        assert re.search(rf"^\s+{kind}\s+\d+\s", text, re.M), kind
    assert text.count("before:") == 5 and text.count("after :") == 5
    assert SALT not in text and SALT not in captured.err


def test_filters_since_until_chat_id_private_and_history(tmp_path, capsys):
    db = Db(tmp_path / "filters.db")
    for day in range(3):
        db.add(GROUP_A, DAY0 + day * 86400 + 600, "wxid_a", "甲", f"A day {day}")
        db.add(GROUP_B, DAY0 + day * 86400 + 900, "wxid_b", "乙", f"B day {day}")
    db.add(PRIVATE, DAY0 + 1000, "wxid_p", "友", "private")
    db.add_history(GROUP_A, DAY0 - 86400, "backup-unknown", "群友", "archived hello")
    path = db.close()
    out = tmp_path / "cases.jsonl"

    def contents(*args):
        code, _ = _export(path, out, "--max-cases", "0", "--order", "chronological",
                          *args, capsys=capsys)
        assert code == 0
        return [c["trigger"]["content"] for c in _read_cases(out)]

    assert contents() == ["A day 0", "B day 0", "A day 1", "B day 1", "A day 2", "B day 2"]
    assert contents("--chat-id", GROUP_B) == ["B day 0", "B day 1", "B day 2"]
    assert contents("--chat-id", GROUP_A, "--chat-id", GROUP_B, "--since", "2024-01-02",
                    "--until", "2024-01-02") == ["A day 1", "B day 1"]     # whole day
    assert contents("--since", str(DAY0 + 86400), "--until", str(DAY0 + 2 * 86400)) \
        == ["A day 1", "B day 1"]
    assert "private" in contents("--include-private")
    assert "private" in contents("--chat-id", PRIVATE)
    assert "archived hello" not in contents()
    assert contents("--include-history", "--chat-id", GROUP_A)[0] == "archived hello"

    # unknown chat ids are reported, and an empty selection is an error
    code, captured = _export(path, out, "--chat-id", "nope@chatroom", capsys=capsys)
    assert code == 1
    assert "not found" in captured.err and "nothing was written" in captured.err


def test_history_table_is_optional(tmp_path, capsys):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT "
        "UNIQUE NOT NULL, chat_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name "
        "TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', msg_type INTEGER NOT NULL "
        "DEFAULT 1, timestamp INTEGER NOT NULL, created_at INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO messages (message_id, chat_id, sender_id, sender_name,"
                 " content, timestamp) VALUES ('m1', ?, 'u', 'n', 'hi', ?)",
                 (GROUP_A, T0))
    conn.commit()
    conn.close()
    out = tmp_path / "cases.jsonl"
    code, captured = _export(path, out, "--include-history", capsys=capsys)
    assert code == 0 and len(_read_cases(out)) == 1
    assert "no history_messages table" in captured.err


def test_database_is_opened_read_only_and_missing_db_is_an_error(
        pii_db, tmp_path, capsys):
    conn = exp.open_database(pii_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM messages")
    conn.close()
    code, captured = _export(tmp_path / "missing.db", tmp_path / "o.jsonl", capsys=capsys)
    assert code == 1 and "database not found" in captured.err


def test_each_case_is_one_physical_line_even_with_exotic_separators(tmp_path, capsys):
    db = Db(tmp_path / "lines.db")
    odd = f"第一行\n第二行\r\n{chr(0x2028)}{chr(0x2029)}{chr(0x85)}末尾"
    db.add(GROUP_A, T0, "wxid_a", "甲", odd)
    path = db.close()
    out = tmp_path / "cases.jsonl"
    _export(path, out, capsys=capsys)
    raw = out.read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    (case,) = _read_cases(out)
    assert case["trigger"]["content"] == odd
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_write_keeps_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "cases.jsonl"
    target.write_text("old\n", encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(exp.os, "replace", boom)
    with pytest.raises(OSError):
        exp.write_cases(target, [{"id": "x"}])
    assert target.read_text(encoding="utf-8") == "old\n"
    assert not (tmp_path / "cases.jsonl.tmp").exists()


def test_output_inside_a_repository_is_flagged_unless_gitignored(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text(
        "data/*\n!data/keep.txt\n*.log\nbuild/\n", encoding="utf-8")
    assert exp.repo_output_warning(repo / "data" / "replay" / "cases.jsonl") is None
    assert exp.repo_output_warning(repo / "data" / "x.jsonl") is None
    warning = exp.repo_output_warning(repo / "tools" / "cases.jsonl")
    assert warning and "not git-ignored" in warning and "data/replay/" in warning
    assert exp.repo_output_warning(repo / "data" / "keep.txt") is not None
    elsewhere = tmp_path / "elsewhere" / "cases.jsonl"
    if exp.find_git_root(elsewhere.parent) is None:    # tmp dir not inside a work tree
        assert exp.repo_output_warning(elsewhere) is None


def test_cli_warns_when_writing_inside_a_repository_outside_ignored_paths(
        pii_db, tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("data/*\n", encoding="utf-8")

    bad = repo / "tools" / "cases.jsonl"
    code, captured = _export(pii_db, bad, "--max-cases", "1", capsys=capsys)
    assert code == 0 and bad.exists()
    assert "WARNING" in captured.err and "not git-ignored" in captured.err

    good = repo / "data" / "replay" / "cases.jsonl"
    code, captured = _export(pii_db, good, "--max-cases", "1", capsys=capsys)
    assert code == 0 and good.exists()
    assert "WARNING" not in captured.err


def test_default_output_directory_is_the_ignored_data_replay():
    assert exp.DEFAULT_OUT == ROOT / "data" / "replay" / "cases.jsonl"
    rules = exp._gitignore_rules(ROOT)
    if rules:   # the repository's real .gitignore ignores data/*
        assert exp.is_gitignored("data/replay/cases.jsonl", rules)


def test_cli_runs_as_a_script_from_any_directory(pii_db, tmp_path):
    elsewhere = tmp_path / "some" / "other" / "cwd"
    elsewhere.mkdir(parents=True)
    out = tmp_path / "cli" / "cases.jsonl"
    env = _child_env()
    script = TOOLS_DIR / "export_replay_cases.py"
    run = subprocess.run(
        [sys.executable, "-B", str(script), "--db", str(pii_db), "--out", str(out),
         "--bot-name", BOT, "--salt", SALT, "--max-cases", "2"],
        cwd=elsewhere, env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=60)
    assert run.returncode == 0, run.stderr
    assert len(_read_cases(out)) == 2
    helped = subprocess.run(
        [sys.executable, "-B", str(script), "--help"], cwd=elsewhere, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert helped.returncode == 0 and "data/replay" in helped.stdout


def test_tools_do_not_import_the_bot_source_tree():
    for name in ("replay_redaction.py", "export_replay_cases.py", "label_replay_cases.py"):
        source = (TOOLS_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+src\b", source, re.M), name


# --------------------------------------------------------------------------
# Labeling CLI
# --------------------------------------------------------------------------


def _case(i: int) -> dict:
    return {
        "schema": "replay-case/v1", "id": f"exp-{i:06d}", "category": "unlabeled",
        "chat": {"chat_id": "g_0@chatroom", "group_name": "示例群1", "is_group": True},
        "bot_name": BOT, "now": T0 + i,
        "context": [{"offset_sec": -5, "sender_id": "u_1", "sender_name": "用户A",
                     "content": f"上文 {i}\n第二行", "msg_type": 1}],
        "trigger": {"sender_id": "u_2", "sender_name": "用户B", "content": f"触发 {i}",
                    "msg_type": 1, "is_at_mentioned": i % 2 == 0,
                    "is_reply_to_bot": None, "quoted_content": "",
                    "quoted_sender_name": "", "offset_sec": 0},
        "next_messages": [{"offset_sec": 7, "sender_id": "u_1", "sender_name": "用户A",
                           "content": "之后"}],
        "bot_state": None, "expect": None, "source": "export",
    }


def _write_cases(path: Path, cases) -> Path:
    path.write_text("".join(lab.dumps_case(c) + "\n" for c in cases), encoding="utf-8")
    return path


class Script:
    """Scripted ``input``; optionally inspects the output file at each prompt."""

    def __init__(self, answers, out_path: Path | None = None):
        self.answers = list(answers)
        self.out_path = out_path
        self.seen: list[int] = []        # records on disk at every prompt
        self.prompts: list[str] = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if self.out_path is not None:
            self.seen.append(
                len(self.out_path.read_text(encoding="utf-8").splitlines())
                if self.out_path.exists() else 0)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def _labeled(path: Path) -> dict[str, dict]:
    return {c["id"]: c for c in _read_cases(path)}


def test_default_input_reads_piped_answers_and_echoes_them(monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("s\nq"))
    assert lab.default_input("> ") == "s"
    assert lab.default_input("> ") == "q"            # last line without a newline
    with pytest.raises(EOFError):
        lab.default_input("> ")
    assert capsys.readouterr().out == "> s\n> q\n> "


def test_render_case_shows_context_trigger_and_next_messages():
    lines = lab.render_case(_case(2), position=2, total=9, done=1)
    text = "\n".join(lines)
    assert "[2/9] exp-000002" in text and "示例群1" in text
    assert "-5s 用户A: 上文 2" in text and "第二行" in text
    assert ">>> TRIGGER 用户B [@bot]" in text and "触发 2" in text
    assert "+7s 用户A: 之后" in text


def test_labeling_saves_after_every_answer_and_maps_keys(tmp_path):
    cases = [_case(i) for i in range(1, 6)]
    src = _write_cases(tmp_path / "cases.jsonl", cases)
    out = tmp_path / "labeled.jsonl"
    # s+bot, N+group (typed together), e then skip addressee, b (bad), n then member
    script = Script(["s", "b", "Ng", "e", "", "b", "n", "m"], out)
    printed = []
    code = lab.main([str(src), "--out", str(out)], input_fn=script, print_fn=printed.append)
    assert code == 0
    done = _labeled(out)
    assert list(done) == [f"exp-{i:06d}" for i in range(1, 6)]            # input order
    assert done["exp-000001"]["expect"] == {
        "action": "speak", "strength": "should", "addressee": "bot", "note": ""}
    assert done["exp-000002"]["expect"] == {
        "action": "silent", "strength": "must", "addressee": "group", "note": ""}
    assert done["exp-000003"]["expect"] == {
        "action": "either", "strength": "free", "addressee": None, "note": ""}
    assert done["exp-000004"]["category"] == "bad" and done["exp-000004"]["expect"] is None
    assert done["exp-000005"]["expect"]["addressee"] == "other_member"
    assert done["exp-000001"]["category"] == "unlabeled"                  # untouched
    # everything except category/expect is copied verbatim
    for i, case in enumerate(cases, 1):
        stripped = {k: v for k, v in done[f"exp-{i:06d}"].items()
                    if k not in ("category", "expect")}
        assert stripped == {k: v for k, v in case.items() if k not in ("category", "expect")}
    # records on disk at each prompt: the action key is saved before the
    # optional addressee question, and every later question sees it
    assert script.seen == [0, 1, 1, 2, 3, 3, 4, 5]
    assert "saved" in printed[-1] and "4 labeled + 1 bad" in printed[-1]


def test_resume_continues_where_it_stopped(tmp_path):
    cases = [_case(i) for i in range(1, 5)]
    src = _write_cases(tmp_path / "cases.jsonl", cases)
    out = tmp_path / "labeled.jsonl"
    first = Script(["s", "b", "n", "", "q"])
    assert lab.main([str(src), "--out", str(out)], input_fn=first, print_fn=lambda _l: None) == 0
    assert list(_labeled(out)) == ["exp-000001", "exp-000002"]

    shown = []
    second = Script(["e", "", "S", "g"])
    lab.main([str(src), "--out", str(out)], input_fn=second, print_fn=shown.append)
    done = _labeled(out)
    assert list(done) == [f"exp-{i:06d}" for i in range(1, 5)]
    assert done["exp-000001"]["expect"]["action"] == "speak"              # kept
    assert done["exp-000003"]["expect"]["action"] == "either"
    assert done["exp-000004"]["expect"] == {
        "action": "speak", "strength": "must", "addressee": "group", "note": ""}
    shown_text = "\n".join(shown)
    assert "exp-000001" not in shown_text and "exp-000003" in shown_text
    assert "[3/4]" in shown_text


def test_quit_and_end_of_input_keep_everything(tmp_path):
    src = _write_cases(tmp_path / "cases.jsonl", [_case(i) for i in range(1, 4)])
    out = tmp_path / "labeled.jsonl"
    # EOF in the middle of the addressee question: the action key was saved already
    lab.main([str(src), "--out", str(out)], input_fn=Script(["N"]), print_fn=lambda _l: None)
    assert _labeled(out)["exp-000001"]["expect"]["action"] == "silent"
    # immediate q: no file is created, nothing breaks
    other = tmp_path / "none.jsonl"
    assert lab.main([str(src), "--out", str(other)], input_fn=Script(["q"]),
                    print_fn=lambda _l: None) == 0
    assert not other.exists()


def test_undo_removes_the_last_answer_and_asks_again(tmp_path):
    src = _write_cases(tmp_path / "cases.jsonl", [_case(1), _case(2), _case(3)])
    out = tmp_path / "labeled.jsonl"
    script = Script(["s", "u", "u", "n", "g", "e", "", "u", "q"])
    # case 1: s, then u at the addressee question undoes it; asked again: u has
    # nothing to undo; n,g answers it.  case 2: e, skip.  case 3: u undoes case 2
    # (shown again), q quits.
    lab.main([str(src), "--out", str(out)], input_fn=script, print_fn=lambda _l: None)
    done = _labeled(out)
    assert list(done) == ["exp-000001"]
    assert done["exp-000001"]["expect"]["action"] == "silent"
    assert done["exp-000001"]["expect"]["addressee"] == "group"


def test_invalid_keys_reprompt_without_losing_state(tmp_path):
    src = _write_cases(tmp_path / "cases.jsonl", [_case(1)])
    out = tmp_path / "labeled.jsonl"
    shown = []
    lab.main([str(src), "--out", str(out)], input_fn=Script(["x", "", "7", "s", "z"]),
             print_fn=shown.append)
    done = _labeled(out)
    assert done["exp-000001"]["expect"]["action"] == "speak"
    assert done["exp-000001"]["expect"]["addressee"] is None              # "z" ignored
    assert sum("unknown key" in line for line in shown) == 2
    assert sum(line.startswith("[1/1]") for line in shown) == 1           # shown once


def test_no_addressee_flag_and_category_option(tmp_path):
    src = _write_cases(tmp_path / "cases.jsonl", [_case(1), _case(2)])
    out = tmp_path / "labeled.jsonl"
    script = Script(["s", "n"])
    lab.main([str(src), "--out", str(out), "--no-addressee", "--category", "reviewed"],
             input_fn=script, print_fn=lambda _l: None)
    done = _labeled(out)
    assert all(c["category"] == "reviewed" for c in done.values())
    assert all(c["expect"]["addressee"] is None for c in done.values())
    assert not any("addressee" in p for p in script.prompts)


def test_atomic_write_keeps_old_file_when_replace_fails(tmp_path, monkeypatch, capsys):
    src = _write_cases(tmp_path / "cases.jsonl", [_case(1), _case(2)])
    out = tmp_path / "labeled.jsonl"
    lab.main([str(src), "--out", str(out), "--no-addressee"], input_fn=Script(["s", "q"]),
             print_fn=lambda _l: None)
    before = out.read_bytes()

    def boom(*_a, **_k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(lab.os, "replace", boom)
    code = lab.main([str(src), "--out", str(out), "--no-addressee"],
                    input_fn=Script(["n"]), print_fn=lambda _l: None)
    monkeypatch.undo()
    assert code == 1
    assert "simulated crash" in capsys.readouterr().err
    assert out.read_bytes() == before                      # untouched, still valid
    assert not (tmp_path / "labeled.jsonl.tmp").exists()   # temp file cleaned up
    assert list(_labeled(out)) == ["exp-000001"]


def test_resume_refuses_labels_that_belong_to_different_messages(tmp_path, capsys):
    src = _write_cases(tmp_path / "cases.jsonl", [_case(1), _case(2)])
    out = tmp_path / "labeled.jsonl"
    lab.main([str(src), "--out", str(out), "--no-addressee"], input_fn=Script(["s", "q"]),
             print_fn=lambda _l: None)
    before = out.read_bytes()

    changed = _case(1)
    changed["trigger"]["content"] = "a completely different message"
    src2 = _write_cases(tmp_path / "cases2.jsonl", [changed, _case(2)])
    code = lab.main([str(src2), "--out", str(out)], input_fn=Script(["s"]),
                    print_fn=lambda _l: None)
    assert code == 2
    assert "differs from the input" in capsys.readouterr().err
    assert out.read_bytes() == before


def test_labeler_refuses_to_overwrite_its_input_and_rejects_bad_files(tmp_path, capsys):
    src = _write_cases(tmp_path / "cases.jsonl", [_case(1)])
    assert lab.main([str(src), "--out", str(src)], input_fn=Script([]),
                    print_fn=lambda _l: None) == 2
    assert "must differ" in capsys.readouterr().err
    assert lab.read_cases(src)[0]["id"] == "exp-000001"                   # intact

    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"schema":"replay-case/v1","id":"a","trigger":{}}\nnot json\n',
                      encoding="utf-8")
    assert lab.main([str(broken), "--out", str(tmp_path / "o.jsonl")],
                    input_fn=Script([]), print_fn=lambda _l: None) == 2
    assert "invalid JSON" in capsys.readouterr().err


def test_labeler_accepts_missing_schema_but_not_other_versions(tmp_path, capsys):
    plain = _case(1)
    del plain["schema"]
    ok = _write_cases(tmp_path / "plain.jsonl", [plain])
    out = tmp_path / "plain.labeled.jsonl"
    assert lab.main([str(ok), "--out", str(out), "--no-addressee"],
                    input_fn=Script(["e"]), print_fn=lambda _l: None) == 0
    assert _labeled(out)["exp-000001"]["expect"]["action"] == "either"

    future = _case(1)
    future["schema"] = "replay-case/v2"
    bad = _write_cases(tmp_path / "future.jsonl", [future])
    assert lab.main([str(bad), "--out", str(tmp_path / "x.jsonl")],
                    input_fn=Script([]), print_fn=lambda _l: None) == 2
    assert "replay-case/v2" in capsys.readouterr().err


def test_exported_file_feeds_straight_into_the_labeler(pii_db, tmp_path, capsys):
    out = tmp_path / "cases.jsonl"
    _export(pii_db, out, "--chat-id", GROUP_A, capsys=capsys)
    labeled = tmp_path / "labeled.jsonl"
    n = len(_read_cases(out))
    script = Script(["s", "b"] * n, labeled)
    assert lab.main([str(out), "--out", str(labeled)], input_fn=script,
                    print_fn=lambda _l: None) == 0
    done = _read_cases(labeled)
    assert len(done) == n
    assert all(c["expect"]["action"] == "speak" for c in done)
