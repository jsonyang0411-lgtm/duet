"""发得慢一点 (PRM_SEND_PACE, 2026-11-04): a reply's messages go out at a person's pace, not all at once.

The user: 「回消息的时候尽量别一口气发一堆过去」: a reply was up to 3 text messages 0.4-3.5 s apart, then its pictures one
right after another, an opening's two lines and three pictures in a dozen seconds.  On (the default): 2-5 s between its
text messages by length, 3-6 s before its first file after the text, 2-4 s between files; off: as before.  The pauses
are the service's ``_pause`` (here: recorded, no real waiting).  Names and numbers here are made up.
"""

import io
import os
import unittest
from unittest.mock import patch

import pytest

from test_prm import CREATOR, Bot, Clock, draft

from src.prm.service import PACE_FIRST_FILE, PACE_NEXT_FILE, PACE_TEXT
from src.send.bubbles import typing_delay

FIRST = "老师您好～我是 ACE7CN 的运营"
SECOND = "先发两款当季现货给您看看，有喜欢的款可以跟我说～"
PICTURES = (("dy-3000000000000000101.jpg", b"beret"), ("dy-3000000000000000102.jpg", b"pillbox"))


def make(tmp_path, **prm):
    b = Bot(Clock(), tmp_path, prm_reply_parts=3, **prm)
    b.prm.register(CREATOR, "小美", "测试达人")
    b.pauses = []
    b.prm._pause = lambda sec: b.pauses.append(sec) or False       # (no real waiting; False: not stopping)
    return b


def approved(b, text=f"{FIRST}\n\n{SECOND}", files=PICTURES):
    """Her message, the Mac's draft (two paragraphs, two pictures), an operator's 「发送」."""
    b.prm.handle(b.msg("你好"))
    job = b.worker_takes()
    for name, body in files:
        assert b.relay.outfile(job["id"], name, io.BytesIO(body).read, len(body))
    b.finish(job["id"], draft(message_kind="greeting", reply_draft=text, slots={}, stage_proposal="",
                              attachments_to_send=[f"assets/products/{name}" for name, _ in files]))
    packet = b.prm.store.packet_for_job(job["id"])
    assert b.prm.decide(packet["id"], "send")["ok"]
    return packet


@pytest.fixture
def paced(tmp_path):
    b = make(tmp_path, prm_send_pace="on")
    yield b
    b.prm.stop()
    b.relay.stop()


def test_on_text_messages_and_files_go_out_at_a_persons_pace(paced):
    """Two text messages a slower typing pause apart (2-5 s by length), the first picture 3-6 s after the text, the
    next 2-4 s after it; all of it goes, in order."""
    packet = approved(paced)
    paced.prm.send_approved()
    assert paced.texts == [(CREATOR, FIRST), (CREATOR, SECOND)] and len(paced.files) == 2
    assert paced.prm.store.packet(packet["id"])["status"] == "sent"
    text, first, second = paced.pauses
    assert text == typing_delay(SECOND, **PACE_TEXT) and 2.0 <= text <= 5.0 and text > typing_delay(SECOND)
    assert PACE_FIRST_FILE[0] <= first <= PACE_FIRST_FILE[1] and PACE_NEXT_FILE[0] <= second <= PACE_NEXT_FILE[1]


def test_the_text_pause_grows_with_the_words_from_two_to_five_seconds(paced):
    assert paced.prm._send_pause("好") == pytest.approx(2.1)
    assert paced.prm._send_pause("好" * 20) == pytest.approx(4.0)
    assert paced.prm._send_pause("好" * 200) == 5.0
    for _ in range(50):                                            # at random, always inside the range
        assert 3.0 <= paced.prm._send_pause(after_text=True, file_n=0) <= 6.0
        assert 2.0 <= paced.prm._send_pause(after_text=True, file_n=1) <= 4.0


def test_a_draft_of_files_only_sends_its_first_file_at_once(paced):
    approved(paced, text="")
    paced.prm.send_approved()
    assert paced.texts == [] and len(paced.files) == 2
    [between] = paced.pauses
    assert PACE_NEXT_FILE[0] <= between <= PACE_NEXT_FILE[1]


def test_one_message_with_one_picture_waits_only_before_the_picture(paced):
    approved(paced, text=FIRST, files=PICTURES[:1])
    paced.prm.send_approved()
    assert paced.texts == [(CREATOR, FIRST)] and len(paced.files) == 1
    [before] = paced.pauses
    assert PACE_FIRST_FILE[0] <= before <= PACE_FIRST_FILE[1]


def test_stopping_in_the_pause_before_a_file_sends_no_more(paced):
    """The bot stops while it waits before the first picture: the text went, no picture goes, the draft stays sending
    (the next start settles it, as a stop between two text messages does)."""
    packet = approved(paced)
    paced.prm._pause = lambda sec: paced.pauses.append(sec) or len(paced.pauses) > 1   # (the text's passes, the file's stops)
    paced.prm.send_approved()
    assert paced.texts == [(CREATOR, FIRST), (CREATOR, SECOND)] and paced.files == []
    assert paced.prm.store.packet(packet["id"])["status"] == "sending"


def test_off_and_for_an_older_caller_it_is_as_before(tmp_path):
    """Off (or a config object without it): the 0.4-3.5 s typing pause between text messages, the files at once."""
    for name, prm in (("off", {"prm_send_pace": "off"}), ("none", {})):
        b = make(tmp_path / name, **prm)
        approved(b)
        b.prm.send_approved()
        assert b.texts == [(CREATOR, FIRST), (CREATOR, SECOND)] and len(b.files) == 2
        assert b.pauses == [typing_delay(SECOND)]
        b.prm.stop()
        b.relay.stop()


class SendPaceConfigTests(unittest.TestCase):
    """PRM_SEND_PACE: on by default; off turns it off; anything else is refused."""

    def test_defaults_and_what_the_env_says(self):
        from src.config import BotConfig, load_config

        self.assertEqual(BotConfig().prm_send_pace, "on")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config().prm_send_pace, "on")
        with patch.dict(os.environ, {"PRM_SEND_PACE": "off"}, clear=True):
            self.assertEqual(load_config().prm_send_pace, "off")

    def test_bad_values_are_refused(self):
        from src.config import _validate_config

        for good in ("on", "off"):
            _validate_config({"prm_send_pace": good})
        with self.assertRaises(RuntimeError) as ctx:
            _validate_config({"prm_send_pace": "maybe"})
        self.assertIn("PRM_SEND_PACE", str(ctx.exception))
