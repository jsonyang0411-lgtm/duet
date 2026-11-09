"""起草备注 (2026-10-31, src/prm): what the operators tell the Mac about one creator (her WeChat signature said
「直接发链接和brief，感兴趣的会回复！勿催视频，不接语音」).  Kept on her deal (``deals.draft_note``; only an operator writes
it: her party ``note`` is the bot's for a friend it registered, and may name her groups), set by the deal action
``draft_note`` (one event per change, its words in no event and no log line), and carried by every draft job of the
deal (``meta.note``, masked like her words): her messages', the 开场白's, a 专属佣金 step's, her 百应 drafts'.  No note: the
jobs as before (no key).  The 8.2 clean-up cuts it like the deal's other words.  Every name and number is made up."""

import json
import logging
import sqlite3

import pytest

from test_prm import CREATOR, Bot, Clock, draft
from test_prm_buyin import AWEME, CID, EPOCH, INTERVAL, UID, conv, jobs_of, line, listed, round_says

from src.prm.service import BUYIN_CHAT, DRAFT_NOTE, apply_deal_action, event_out
from src.prm.store import DRAFT_NOTE_CHARS, PrmStore, draft_note_text
from src.relay.service import job_meta

SIGNATURE = "直接发链接和brief，感兴趣的会回复！勿催视频，不接语音"
PHONE = "13900001111"
WECHAT_ID = "xiaomei_2026"
OTHER = "wxid_creator02"

# deals as the table was before the 起草备注 (2026-10-30: three_party the last column added)
OLD_DEALS = """CREATE TABLE deals (id INTEGER PRIMARY KEY AUTOINCREMENT, party_id INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'talk', mode TEXT NOT NULL DEFAULT 'copilot', slots TEXT NOT NULL DEFAULT '{}',
    stage_proposal TEXT NOT NULL DEFAULT '', attention TEXT NOT NULL DEFAULT '', last_message_at REAL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, mode_until REAL, delivered_at REAL, purged_at REAL,
    holding_round REAL, notices TEXT NOT NULL DEFAULT '[]', chain TEXT NOT NULL DEFAULT '{}',
    three_party TEXT NOT NULL DEFAULT '{}')"""


@pytest.fixture
def clock():
    return Clock(EPOCH)               # (百应's times before 2001 are dropped: the 百应 job below)


@pytest.fixture
def bot(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm._buyin_sec = INTERVAL                          # PRM_BUYIN_SYNC_MIN=10 (the tests' config objects: off)
    b.prm.register(CREATOR, "小美", "测试达人")
    b.prm.creator_directory = listed({"wxid": CREATOR, "platform": "DY", "platform_no": AWEME, "platform_uid": UID})
    yield b
    b.prm.stop()
    b.relay.stop()


def note(b, text, deal_id=1):
    return b.prm.deal_action(DRAFT_NOTE, {"deal_id": deal_id, "text": text})


def note_events(b, deal_id=1):
    return [e for e in b.prm.store.events(deal_id, limit=None) if e["kind"] == DRAFT_NOTE]


def every_job_kind(b, clock):
    """Her message's job, a 专属佣金 step's (after her draft), her 百应 draft's (小美, deal 1) and the 开场白's of a creator
    who has not written (小兰, deal 2): ``{kind: meta}``."""
    b.prm.handle(b.msg("你好呀，你们的帽子怎么合作"))
    message = b.worker_takes()
    b.finish(message["id"], draft(reply_draft="你好呀～", slots={}, message_kind="greeting", stage_proposal=""))
    packet = b.prm.store.packet_for_job(message["id"])
    deal = b.prm.store.deal(1)
    step_id = b.prm._dispatch_step(deal, b.prm.store.party(deal["party_id"]), "样品签收了，开专属佣金", packet["id"], True,
                                   clock.now)
    asked = b.prm.ask_opening(2)
    assert asked["ok"], asked
    round_says(b, clock, conv(line("m2", "你好，佣金多少？", clock.now)))
    [buyin] = [j for j in jobs_of(b, BUYIN_CHAT + CID) if j["status"] == "pending"]
    step, opening = (job_meta(b.relay.store.get(i)) for i in (step_id, asked["job"]))
    assert step["system_note"] == "样品签收了，开专属佣金" and opening["opening"] == {"how": "", "by": "console"}
    assert job_meta(buyin)["channel"] == "buyin"
    return {"message": message["meta"], "step": step, "buyin": job_meta(buyin), "opening": opening}


# ── the store ─────────────────────────────────────────────────────────────

def test_an_old_database_gets_the_column_and_its_deals_have_no_note(tmp_path):
    path = tmp_path / "prm.db"
    old = sqlite3.connect(str(path))
    old.execute(OLD_DEALS)
    old.execute("INSERT INTO deals (party_id, stage, created_at, updated_at) VALUES (1, 'agreed', 1, 1)")
    old.commit()
    old.close()
    for _ in range(2):                                   # opening it twice changes nothing more
        PrmStore(path).close()
    store = PrmStore(path)
    try:
        deal = store.deal(1)
        assert (deal["stage"], deal["draft_note"]) == ("agreed", "")
        store.update_deal(1, 2.0, draft_note=f"  {SIGNATURE}\n\n 勿催　视频 ")
        assert store.deal(1)["draft_note"] == f"{SIGNATURE}\n勿催 视频"           # its lines (the full-width blank too)
    finally:
        store.close()
    conn = sqlite3.connect(str(path))
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(deals)")]
    finally:
        conn.close()
    assert columns.count("draft_note") == 1


def test_the_store_keeps_a_notes_lines_tidied_and_at_most_300_characters():
    """Each line's blanks collapsed, empty lines dropped, the line breaks kept (2026-11-09: PRM_DRAFT_CONTEXT's
    「确认 <编码>」 counts only as a line of its own)."""
    assert DRAFT_NOTE_CHARS == 300
    assert draft_note_text("直接发链接\n\n\t和brief ") == "直接发链接\n和brief" and draft_note_text(None) == ""
    assert draft_note_text(" 她要深棕 \r\n确认   A7269901001B-F\r勿催\n") == "她要深棕\n确认 A7269901001B-F\n勿催"
    assert draft_note_text("长" * 150 + "\n" + "长" * 200, limit=None) == "长" * 150 + "\n" + "长" * 200
    assert draft_note_text("长" * 400) == "长" * 300
    store = PrmStore(":memory:")
    party = store.add_party(CREATOR, "小美", now=1)
    deal = store.deal_of(party["id"], now=1)
    assert deal["draft_note"] == ""
    store.update_deal(deal["id"], 2.0, draft_note="长" * 400)
    assert store.deal(deal["id"])["draft_note"] == "长" * 300


# ── the deal action ───────────────────────────────────────────────────────

def test_an_operator_writes_changes_and_clears_the_note_one_event_each_and_no_words_logged(bot, caplog):
    with caplog.at_level(logging.INFO, logger="src.prm.service"):
        assert note(bot, f"  {SIGNATURE}\n") == {"ok": True, "draft_note": SIGNATURE}
        assert note(bot, SIGNATURE) == {"ok": True, "draft_note": SIGNATURE}      # a second click: nothing changes
        assert bot.prm.store.deal(1)["draft_note"] == SIGNATURE
        assert note(bot, "不接语音") == {"ok": True, "draft_note": "不接语音"}
        assert note(bot, " \n ") == {"ok": True, "draft_note": ""}                # empty: cleared
    assert bot.prm.store.deal(1)["draft_note"] == ""
    events = note_events(bot)                                                     # (newest first)
    assert [(e["detail"], e["actor"]) for e in events] == [({"chars": 0}, "console"), ({"chars": 4}, "console"),
                                                           ({"chars": len(SIGNATURE)}, "console")]
    assert [event_out(e)["name"] for e in events] == ["清掉起草备注", "改起草备注（4 字）",
                                                      f"改起草备注（{len(SIGNATURE)} 字）"]
    assert "draft note written" in caplog.text and "draft note cleared" in caplog.text
    for words in ("勿催视频", "不接语音"):
        assert words not in caplog.text
        assert words not in json.dumps(bot.prm.store.events(1, limit=None), ensure_ascii=False)
    assert bot.prm.store.party(1)["note"] == "测试达人"                           # her 备注 is another thing


def test_a_note_too_long_or_not_text_is_refused_and_nothing_changes(bot):
    assert note(bot, "长" * 301) == {"ok": False, "error": "起草备注最多 300 个字（现在 301 个）"}
    for text in (None, 5, ["不接语音"]):
        assert note(bot, text) == {"ok": False, "error": "要给出起草备注的文字（空着就是清掉）"}
    assert bot.prm.deal_action(DRAFT_NOTE, {"text": "不接语音"}) == {"ok": False, "error": "没有找到这张合作单"}
    assert note(bot, "不接语音", deal_id=99) == {"ok": False, "error": "没有找到这张合作单"}
    assert note(bot, "不接语音", deal_id="x")["ok"] is False
    assert bot.prm.store.deal(1)["draft_note"] == "" and note_events(bot) == []
    kept = note(bot, "长" * 150 + " \n\n " + "长" * 149)                           # 300 once the blank lines go
    assert kept == {"ok": True, "draft_note": "长" * 150 + "\n" + "长" * 149}


def test_the_console_shows_the_note_as_written(bot):
    assert note(bot, SIGNATURE)["ok"]
    [party] = bot.prm.snapshot()["parties"]
    assert party["deal"]["draft_note"] == SIGNATURE and party["note"] == "测试达人"
    assert party["deal"]["events"][0]["name"] == f"改起草备注（{len(SIGNATURE)} 字）"


def test_a_stopped_bot_does_not_keep_a_note(bot):
    """The console's way while the bot is stopped (straight on the database): refused like 写开场白 and 拉三方群."""
    store = bot.prm.store
    answer = apply_deal_action(store, DRAFT_NOTE, {"deal_id": 1, "text": SIGNATURE}, now=EPOCH, takeover_sec=7200)
    assert answer == {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能改起草备注"}
    assert store.deal(1)["draft_note"] == "" and note_events(bot) == []


# ── every draft job of the deal ───────────────────────────────────────────

def test_every_draft_job_of_the_deal_carries_the_note_masked_like_her_words(bot, clock):
    """Her message's, a 专属佣金 step's, her 百应 draft's (小美) and the 开场白's (小兰, who has not written): each with its
    own deal's note; a phone becomes its placeholder (the one her words would get), a 微信号 after a label is masked."""
    assert note(bot, f"{SIGNATURE}。她签名里：合作 vx：{WECHAT_ID}，电话 {PHONE}")["ok"]
    bot.prm.register(OTHER, "小兰", "测试达人")
    assert note(bot, SIGNATURE, deal_id=2)["ok"]
    metas = every_job_kind(bot, clock)
    masked = f"{SIGNATURE}。她签名里：合作 vx：xi****26，电话 〈手机1〉"
    assert {kind: meta.get("note") for kind, meta in metas.items()} == {
        "message": masked, "step": masked, "buyin": masked, "opening": SIGNATURE}
    assert bot.prm.store.tokens(1)["〈手机1〉"] == ("phone", PHONE)                # sent with the number put back
    everything = json.dumps([bot.relay.store.list_jobs(100), bot.prm.store.events(1, limit=None)], ensure_ascii=False)
    assert PHONE not in everything and WECHAT_ID not in everything
    assert bot.prm.store.deal(1)["draft_note"] == f"{SIGNATURE}。她签名里：合作 vx：{WECHAT_ID}，电话 {PHONE}"   # as written


def test_without_a_note_no_draft_job_has_one(bot, clock):
    """No note (never written, or cleared): the jobs are as they were before the 起草备注, no ``note`` key at all."""
    bot.prm.register(OTHER, "小兰", "测试达人")
    assert note(bot, SIGNATURE, deal_id=2)["ok"] and note(bot, "", deal_id=2)["ok"]       # written, then cleared
    metas = every_job_kind(bot, clock)
    assert sorted(metas) == ["buyin", "message", "opening", "step"]
    assert [kind for kind, meta in metas.items() if "note" in meta] == []


def test_a_job_queued_before_the_note_is_not_changed_and_the_next_one_has_it(bot):
    """A job is told the note it had when it was made (like the rest of its meta): a burst that joins it does not
    change it; the next job carries the new note."""
    bot.prm.handle(bot.msg("在吗"))
    assert note(bot, SIGNATURE)["ok"]
    bot.prm.handle(bot.msg("想了解一下"))                                       # joins the queued job
    first = bot.worker_takes()
    assert "note" not in first["meta"] and "想了解一下" in first["text"]
    bot.finish(first["id"], draft(reply_draft="在的～", slots={}, message_kind="greeting", stage_proposal=""))
    bot.prm.handle(bot.msg("佣金多少"))
    assert bot.worker_takes()["meta"]["note"] == SIGNATURE


# ── the 8.2 clean-up ──────────────────────────────────────────────────────

def test_the_clean_up_cuts_the_note_like_the_deals_other_words(bot, clock):
    """Her phone masked, an address cut to province and city, a labelled 微信号 masked, in the note and in the jobs that
    carried it; the rest of the note stays (her signature's words); a second clean-up changes nothing."""
    words = f"{SIGNATURE}。签名：合作 vx：{WECHAT_ID}，电话 {PHONE}，样品寄到上海市浦东新区张江路100号3栋1201"
    assert note(bot, words)["ok"]
    bot.prm.handle(bot.msg("你好"))
    job = bot.worker_takes()
    assert "张江路100号" in job["meta"]["note"]                                  # (the model judges addresses: kept)
    counts = bot.prm.purge(bot.prm.store.deal(1), clock.now)
    assert counts["draft_note"] == 1
    assert bot.prm.store.deal(1)["draft_note"] == (f"{SIGNATURE}。签名：合作 vx：xi****26，电话 139****1111，"
                                                   "样品寄到上海市（详细地址已清除）")
    everything = json.dumps([bot.prm.store.deal(1), bot.relay.store.list_jobs(100),
                             bot.prm.store.events(1, limit=None)], ensure_ascii=False)
    for gone in (PHONE, WECHAT_ID, "张江路", "浦东新区"):
        assert gone not in everything, gone
    assert bot.prm.purge(bot.prm.store.deal(1), clock.now)["draft_note"] == 0    # once more: nothing changes


def test_a_deal_without_a_note_is_cleaned_up_as_before(bot, clock):
    counts = bot.prm.purge(bot.prm.store.deal(1), clock.now)
    assert "draft_note" not in counts and bot.prm.store.deal(1)["draft_note"] == ""
