"""直播手卡 files on this PC (review B-L3, 2026-11-10): the cards (「手卡_01_….png」, her 称呼 printed on them) and the
skill's zip the Mac sends back stay in the relay's ``outfiles/job-*/``.  A draft that closes without sending them
(dropped, out of date, failed, past its 双11 line, the cards left out by 改了再发, a verdict whose cards did not all come)
has them deleted, at once or at the hourly clean-up; any card file or zip older than KEEP_FILES_DAYS (30) goes, sent or
not, but a draft's still waiting or going out (final check 2026-11-10 B); nothing else of the relay's folder.  A draft
whose card is gone all the same is not sent (CARDS_GONE_TEXT).  Names are made up."""

import io
import os
import time

from test_prm_handcards import (CARDS, CREATOR, LINE, asks, beijing, bot, card_draft, clock, drafted,  # noqa: F401
                                hc, new_bot)

from src.prm.service import CARDS_GONE_TEXT
from src.relay.service import KEEP_FILES_DAYS


def card_paths(packet):
    return [a["path"] for a in packet["attachments"] if hc.is_card_file(a["name"])]


def there(paths):
    return [os.path.exists(p) for p in paths]


def test_a_dropped_drafts_cards_are_deleted_at_once_a_sent_ones_kept(bot):
    dropped = drafted(bot, asks(bot))
    paths = card_paths(dropped)
    assert len(paths) == 2 and there(paths) == [True, True]
    assert bot.prm.decide(dropped["id"], "drop")["ok"]
    assert there(paths) == [False, False]
    sent = drafted(bot, asks(bot, "老师手卡再发我一下"))
    assert bot.prm.decide(sent["id"], "send")["ok"] and bot.prm.send_approved() == 1
    bot.prm.purge_due()
    assert there(card_paths(sent)) == [True, True]                 # (sent: the 30 days' sweep's)


def test_an_out_of_date_or_failed_drafts_cards_go_at_the_hourly_clean_up(bot, clock):
    stale = drafted(bot, asks(bot))
    bot.prm.handle(bot.msg("算了，先不用手卡了"))                    # her new line: the waiting draft is out of date
    assert bot.prm.store.packet(stale["id"])["status"] == "superseded"
    failing = drafted(bot, bot.worker_takes())
    bot.files_ok = False                                           # (WeChat does not take the card)
    assert bot.prm.decide(failing["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.prm.store.packet(failing["id"])["status"] == "failed"
    paths = card_paths(stale) + card_paths(failing)
    assert there(paths) == [True] * 4
    bot.prm.purge_due()
    assert there(paths) == [False] * 4


def test_cards_past_their_promo_line_or_left_out_by_an_edit_are_deleted(bot, clock):
    clock.now = beijing(11, 23, 50)
    made = hc.made_block(True, cards=[n for n, _ in CARDS], promo_until="2026-11-11")
    expired = drafted(bot, asks(bot), card_draft(made=made))
    assert bot.prm.decide(expired["id"], "send")["ok"]
    clock.now = beijing(12, 0, 5)
    assert bot.prm.send_approved() == 0
    assert bot.prm.store.packet(expired["id"])["error"] == hc.EXPIRED_TEXT and there(card_paths(expired)) == [False] * 2
    clock.now = beijing(11, 14)
    edited = drafted(bot, asks(bot, "老师手卡发一下"), card_draft(made=made))
    clock.now = beijing(12, 9)
    assert bot.prm.decide(edited["id"], "edit", "老师，双11 的价过了，手卡我重新整理再发您～")["ok"]
    assert there(card_paths(edited)) == [False] * 2


def test_the_cards_of_a_verdict_that_did_not_all_arrive_are_never_kept(bot):
    packet = drafted(bot, asks(bot), files=CARDS[:1])
    assert packet["attachments"] == []
    folder = bot.relay.files_dir / "outfiles" / f"job-{packet['job_id']}"
    assert not (folder / CARDS[0][0]).exists()


def test_card_files_and_zips_older_than_thirty_days_go_sent_or_not_and_nothing_else(bot, clock):
    """Probe probe_windows_retention.py: the cards and the zip of a job 60 days ago stayed (the sweeps took only the photo
    sheets, the relay's prunes other folders)."""
    folder = bot.relay.files_dir / "outfiles" / "job-7"
    folder.mkdir(parents=True)
    old = time.time() - 60 * 86400
    files = {"手卡_01_小帽.png": True, "ACE7CN直播手卡.zip": True, "手卡_02_失败的.png": True, "对图单-7.jpg": True,
             "货盘_秋冬.png": False, "brief.txt": False}
    for name in files:
        (folder / name).write_bytes(b"x")
        os.utime(folder / name, (old, old))
    (folder / "手卡_03_新的.png").write_bytes(b"x")                 # (a card of today)
    clock.now = time.time()
    bot.prm.purge_due()
    for name, gone in files.items():
        assert (folder / name).exists() is not gone, name
    assert (folder / "手卡_03_新的.png").exists()
    assert hc.sweep_files(bot.relay.files_dir, time.time() + (KEEP_FILES_DAYS + 1) * 86400) == 1


def test_a_waiting_or_approved_drafts_cards_stay_however_old_and_one_gone_stops_it(bot, clock):
    """Final check 2026-11-10 B (probe_card_sweeps_vs_waiting): a draft nobody decided for a month had its cards taken by
    the 30 days' sweep, and 「发送」 then sent her the words alone (「手卡整理好啦」 without the cards).  A draft still
    waiting, or approved and not sent yet, keeps them; a card gone all the same (by hand) and it is not sent."""
    assert CARDS_GONE_TEXT == "手卡文件已清理：重新做手卡"
    old = time.time() - (KEEP_FILES_DAYS + 1) * 86400
    waiting = drafted(bot, asks(bot))
    for path in card_paths(waiting):
        os.utime(path, (old, old))
    clock.now = time.time()
    bot.prm.purge_due()
    assert there(card_paths(waiting)) == [True, True] and bot.prm.store.packet(waiting["id"])["status"] == "pending"
    os.remove(card_paths(waiting)[1])
    assert bot.prm.decide(waiting["id"], "send") == {"ok": False, "error": CARDS_GONE_TEXT}
    assert bot.prm.store.packet(waiting["id"])["status"] == "pending" and bot.texts == [] and bot.files == []
    approved = drafted(bot, asks(bot, "老师手卡再发我一下"))
    assert bot.prm.decide(approved["id"], "send")["ok"]                   # (approved: it goes out next round)
    for path in card_paths(approved):
        os.utime(path, (old, old))
    bot.prm.purge_due()
    assert there(card_paths(approved)) == [True, True]
    assert bot.prm.send_approved() == 1
    assert [os.path.basename(path) for _, path in bot.files] == [name for name, _ in CARDS]
