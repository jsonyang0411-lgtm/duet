"""Willingness score and the score mode of the proactive gate."""

import pytest

import src.proactive.gate as gate_module
import src.proactive.rate_tracker as tracker_module
from src.config import BotConfig
from src.proactive import willingness as W
from src.proactive.gate import ProactiveGate
from src.proactive.modes import reset_modes

CHAT = "g1@chatroom"


class Clock:
    def __init__(self, now=1_760_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class NoDice:
    """Score mode must never roll a die."""

    def random(self):
        raise AssertionError("the score mode rolled a die")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(gate_module, "time", fake)
    monkeypatch.setattr(tracker_module, "time", fake)
    reset_modes()
    yield fake
    reset_modes()


def make_gate(mode="score", budget=6, threshold=0.35, **overrides):
    # Rates of 0.5 / 1 / 1.5 / 2 messages per minute map to QUIET / CASUAL /
    # LIVELY / BURST, so a few messages inside the 120 s window pick the mode.
    config = BotConfig(
        proactive_enabled=True, proactive_gate_mode=mode,
        proactive_hourly_budget=budget, proactive_score_threshold=threshold,
        proactive_rate_quiet=0.5, proactive_rate_casual=1.0,
        proactive_rate_lively=1.5, proactive_rate_burst=2.0,
        **overrides,
    )
    return ProactiveGate(config)


def say(gate, clock, text, sender="u1", advance=1.0, chat=CHAT, scored=None, **extra):
    clock.now += advance
    msg = {"chat_id": chat, "sender_id": sender, "content": text, "msg_type": 1}
    msg.update(extra)
    return gate.should_speak(msg) if scored is None else gate.should_speak(msg, scored=scored)


def warm(gate, clock, count, sender="x"):
    """Fill the rate window with ``count`` stickers.

    A sticker counts towards the message rate but can never pass the gate, so
    it does not use up the evaluation interval.
    """
    verdict = None
    for index in range(count):
        verdict = say(gate, clock, "[表情]", sender=f"{sender}{index}")
    return verdict


# ── the score itself ────────────────────────────────────────────────────

def test_base_willingness_is_the_modes_probability():
    for base in (0.10, 0.25, 0.5, 0.7):
        value, contributions = W.score(W.Features(base=base))
        assert value == pytest.approx(base, abs=1e-3)
        assert list(contributions) == ["base"]


def test_every_feature_moves_the_score_in_its_direction():
    plain, _ = W.score(W.Features(base=0.25))
    for field, term, direction in [
        ("is_question", "question", 1), ("info_seeking", "info_seeking", 1),
        ("open_question", "open_question", 1),
        ("substantial", "substantial", 1),
        ("too_short", "too_short", -1), ("repeat", "repeat", -1),
        ("media_only", "media_only", -1),
    ]:
        value, contributions = W.score(W.Features(base=0.25, **{field: True}))
        assert (value - plain) * direction > 0, field
        assert term in contributions, field


def test_a_short_message_is_not_substantial_even_if_flagged():
    value, contributions = W.score(W.Features(base=0.5, substantial=True, too_short=True))
    assert "substantial" not in contributions and "too_short" in contributions


def test_speaking_recently_lowers_the_score_in_two_steps():
    base = W.Features(base=0.5)
    recent, c1 = W.score(W.Features(base=0.5, seconds_since_bot_spoke=30))
    lately, c2 = W.score(W.Features(base=0.5, seconds_since_bot_spoke=200))
    long_ago, c3 = W.score(W.Features(base=0.5, seconds_since_bot_spoke=900))
    assert recent < lately < long_ago == pytest.approx(W.score(base)[0])
    assert "spoke_recently" in c1 and "spoke_lately" in c2 and "spoke" not in "".join(c3)


def test_explain_lists_the_strongest_terms_without_the_base():
    _, contributions = W.score(W.Features(base=0.25, is_question=True, info_seeking=True, repeat=True))
    text = W.explain(contributions, limit=2)
    assert text == "repeat-2.0 info_seeking+1.0"


@pytest.mark.parametrize("text,expected", [
    ("各位推荐个适合新手的机械键盘吧", True),
    ("今晚吃什么？", True), ("你去不去", False), ("有没有人要", True), ("是不是他", True),
    ("好的", False), ("为什么会这样", True),
])
def test_question_detection(text, expected):
    assert W.looks_like_question(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("各位 Python 里怎么把列表去重又保持顺序啊", True),
    ("有没有人懂 excel 怎么把两列合并成一列", True),
    ("有人知道西湖边哪家餐厅不用排队吗", True),
    ("谁知道这个怎么办", True),
    ("晚上有人打游戏吗", False),
    ("有人一起拼车去机场吗 周日早上", False),
    ("周末大家一起去爬山怎么样", False),
    ("今天几号来着", False),
    ("怎么把列表去重", True),                               # a how-to phrase needs no question mark
])
def test_information_seeking_questions(text, expected):
    assert W.looks_info_seeking(text) is expected


@pytest.mark.parametrize("text,msg_type,expected", [
    ("[图片]", 1, True), ("[表情]", 1, True), ("看这个", 3, True), ("看这个", 47, True),
    ("看图说话", 1, False),
])
def test_media_detection(text, msg_type, expected):
    assert W.is_media_only(text, msg_type) is expected


@pytest.mark.parametrize("text,expected", [
    ("嗯", True), ("哈哈", True), ("666", True), ("?", True), ("ok", True), ("", True),
    ("好吧", True), ("明天去爬山吧", False), ("确实", True),
    ("哈哈哈哈哈哈 笑死我了", True), ("xswl", True), ("草", True), ("233333", True),
    ("哈哈哈哈这个太好笑了", False),
])
def test_short_reaction_detection(text, expected):
    assert W.is_too_short(text) is expected



# ── the gate in score mode ──────────────────────────────────────────────

def test_score_mode_never_rolls_a_die(monkeypatch):
    monkeypatch.setattr(gate_module, "random", NoDice())
    gate = make_gate()
    clock = gate_module.time
    for index in range(40):
        say(gate, clock, f"话题{index}你们觉得怎么样呢", sender=f"u{index % 5}", advance=2.0)


def test_the_same_stream_always_gives_the_same_verdicts():
    def run():
        gate_module.time.now = 1_760_000_000.0
        tracker_module.time.now = gate_module.time.now
        reset_modes()
        gate = make_gate()
        verdicts = []
        for index in range(60):
            verdict = say(gate, gate_module.time, ("你们觉得呢？" if index % 4 == 0 else "嗯"),
                          sender=f"u{index % 3}", advance=3.0)
            verdicts.append((verdict[0], verdict[1].name if verdict[1] else None, verdict[2]))
        return verdicts

    assert run() == run()


def test_a_quiet_group_needs_a_strong_reason():
    gate = make_gate()
    clock = gate_module.time
    plain = say(gate, clock, "今天天气不错我们出去走走吧", sender="a")
    assert plain[0] is False and "score" in plain[2]
    asked = say(gate, clock, "各位 怎么把列表去重又保持顺序啊", sender="b", advance=0.0)
    # the second message makes the window 2 msgs => CASUAL; a how-to question is plenty
    assert asked[0] is True and asked[1].name == "CASUAL"
    assert "info_seeking+1.0" in asked[2] and "score=" in asked[2]


def test_a_lively_group_takes_substantial_statements_but_not_reactions():
    gate = make_gate()
    clock = gate_module.time
    warm(gate, clock, 2)
    statement = say(gate, clock, "周末大家一起去爬山怎么样", sender="a")
    assert statement[0] is True and statement[1].name in ("LIVELY", "BURST")
    clock.now += 600                                    # a new evaluation window
    warm(gate, clock, 3, sender="y")
    reaction = say(gate, clock, "哈哈", sender="b")
    assert reaction[0] is False and "too_short" in reaction[2]


def test_a_low_score_does_not_use_up_the_evaluation_interval():
    gate = make_gate()
    clock = gate_module.time
    warm(gate, clock, 3)
    miss = say(gate, clock, "嗯", sender="a")
    assert miss[0] is False and miss[2].startswith("score")
    hit = say(gate, clock, "有人想一起去吃火锅吗", sender="b")
    assert hit[0] is True


def test_after_a_pass_the_interval_applies_again():
    gate = make_gate()
    clock = gate_module.time
    warm(gate, clock, 3)
    first = say(gate, clock, "有人想一起去吃火锅吗", sender="a")
    assert first[0] is True
    second = say(gate, clock, "还有人要去玩密室逃脱吗", sender="b")
    assert second[0] is False and "eval interval" in second[2]


def test_silence_backoff_still_stretches_the_interval():
    gate = make_gate()
    clock = gate_module.time
    warm(gate, clock, 3)
    assert say(gate, clock, "有人想一起去吃火锅吗", sender="a")[0] is True
    gate.record_eval(CHAT)
    gate.record_silence(CHAT)
    gate.record_silence(CHAT)
    clock.now += 80                                      # 4x backoff of a 30-60 s interval
    warm(gate, clock, 3, sender="z")
    verdict = say(gate, clock, "有人想一起去看电影吗", sender="b")
    assert verdict[0] is False and "backoff=4x" in verdict[2]


def test_the_hourly_budget_caps_proactive_speech():
    gate = make_gate(budget=2)
    clock = gate_module.time
    for _ in range(2):
        gate.record_speech(CHAT)
        clock.now += 5
    warm(gate, clock, 3)
    blocked = say(gate, clock, "有人想一起去吃火锅吗", sender="a")
    assert blocked == (False, None, "hourly budget 2/2")
    clock.now += 3601
    warm(gate, clock, 3, sender="q")
    assert say(gate, clock, "有人想一起去吃火锅吗", sender="a")[0] is True


def test_a_budget_of_zero_means_no_limit():
    gate = make_gate(budget=0)
    clock = gate_module.time
    for _ in range(50):
        gate.record_speech(CHAT)
    warm(gate, clock, 3)
    clock.now += 700
    warm(gate, clock, 3, sender="w")
    assert say(gate, clock, "有人想一起去吃火锅吗", sender="a")[0] is True


def test_having_just_spoken_lowers_the_chance_of_speaking_again():
    # Two stickers plus the question make three messages in the window: LIVELY.
    fresh = make_gate()
    clock = gate_module.time
    warm(fresh, clock, 2)
    assert say(fresh, clock, "周末有什么安排吗", sender="a")[0] is True

    clock.now += 600
    gate = make_gate()
    warm(gate, clock, 2)
    gate.record_speech(CHAT)
    clock.now += 30
    verdict = say(gate, clock, "周末有什么安排吗", sender="a")
    assert verdict[0] is False and "spoke_recently-1.5" in verdict[2]


def test_an_unanswered_question_from_someone_else_tips_a_borderline_message():
    gate = make_gate()
    clock = gate_module.time
    asked = say(gate, clock, "晚上去不去？", sender="asker")
    assert asked[0] is False                              # a quiet group: a question alone is not enough
    verdict = say(gate, clock, "我也饿了", sender="other", advance=20)
    assert verdict[0] is True and verdict[1].name == "CASUAL"
    assert "open_question+1.2" in verdict[2]


def test_the_same_message_without_a_question_before_it_stays_unanswered_by_the_bot():
    gate = make_gate()
    clock = gate_module.time
    say(gate, clock, "附近有家新开的店", sender="asker")
    verdict = say(gate, clock, "我也饿了", sender="other", advance=20)
    assert verdict[0] is False and verdict[2].startswith("score")


def test_open_question_feature_is_present_until_somebody_answers():
    gate = make_gate()
    clock = gate_module.time
    say(gate, clock, "谁知道附近有什么好吃的？", sender="asker")
    clock.now += 20
    observed = gate._observe(CHAT, {"sender_id": "bystander", "content": "好困啊", "msg_type": 1}, clock.now)
    assert observed["open_question"] is True
    answered = gate._observe(CHAT, {"sender_id": "bystander2", "content": "好困", "msg_type": 1}, clock.now + 1)
    assert answered["open_question"] is False                 # bystander's message answered it


def test_a_question_is_not_open_for_its_own_asker_or_when_stale():
    gate = make_gate()
    clock = gate_module.time
    say(gate, clock, "谁知道附近有什么好吃的？", sender="asker")
    own = gate._observe(CHAT, {"sender_id": "asker", "content": "有人吗", "msg_type": 1}, clock.now + 20)
    assert own["open_question"] is False
    gate2 = make_gate()
    say(gate2, clock, "谁知道附近有什么好吃的？", sender="asker")
    stale = gate2._observe(CHAT, {"sender_id": "b", "content": "好困啊", "msg_type": 1}, clock.now + 200)
    assert stale["open_question"] is False


def test_echoing_someone_else_is_penalised_but_repeating_yourself_is_not():
    gate = make_gate()
    clock = gate_module.time
    say(gate, clock, "牛啊", sender="a")
    own = gate._observe(CHAT, {"sender_id": "a", "content": "牛啊", "msg_type": 1}, clock.now)
    assert own["repeat"] is False
    echo = gate._observe(CHAT, {"sender_id": "b", "content": "牛啊", "msg_type": 1}, clock.now)
    assert echo["repeat"] is True


def test_media_messages_never_reach_the_model():
    gate = make_gate()
    clock = gate_module.time
    warm(gate, clock, 3)
    assert say(gate, clock, "[图片]", sender="a") == (False, None, "media only")
    assert say(gate, clock, "看这个", sender="a", msg_type=47) == (False, None, "media only")


def test_the_reason_keeps_the_rate_the_replay_harness_parses():
    import re

    gate = make_gate()
    clock = gate_module.time
    warm(gate, clock, 3)
    verdict = say(gate, clock, "有人想一起去吃火锅吗", sender="a")
    assert verdict[0] is True
    assert re.search(r"rate=([0-9.]+)/min", verdict[2])


def test_a_disabled_gate_stays_disabled_in_score_mode():
    gate = make_gate()
    gate._config.proactive_enabled = False
    assert say(gate, gate_module.time, "你们觉得AI怎么样？") == (False, None, "disabled")


# ── dice mode is untouched ──────────────────────────────────────────────

def test_dice_mode_still_rolls_the_probability(monkeypatch):
    class Dice:
        def __init__(self, value):
            self.value = value

        def random(self):
            return self.value

    gate = make_gate(mode="dice")
    clock = gate_module.time
    monkeypatch.setattr(gate_module, "random", Dice(0.99))
    miss = say(gate, clock, "有人想一起去吃火锅吗", sender="a")
    assert miss[0] is False and "probability miss" in miss[2]
    monkeypatch.setattr(gate_module, "random", Dice(0.0))
    hit = say(gate, clock, "有人想一起去吃火锅吗", sender="a", advance=600)
    assert hit[0] is True and hit[1].name == "QUIET"


class CountingLock:
    """A real lock that counts how often it was taken."""

    def __init__(self):
        import threading

        self._lock = threading.Lock()
        self.taken = 0

    def __enter__(self):
        self._lock.acquire()
        self.taken += 1
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def test_the_score_state_is_only_touched_under_the_lock():
    gate = make_gate()
    gate._score_lock = lock = CountingLock()
    say(gate, gate_module.time, "各位 怎么把列表去重又保持顺序啊", sender="a")
    assert lock.taken >= 2                                   # observe, then the gate
    before = lock.taken
    gate.record_speech(CHAT)
    assert lock.taken == before + 1


def test_a_concurrent_message_cannot_pass_the_same_interval_twice():
    gate = make_gate()
    clock = gate_module.time
    warm(gate, clock, 2)
    observed = gate._observe(CHAT, {"sender_id": "a", "content": "各位 怎么把列表去重又保持顺序啊",
                                    "msg_type": 1}, clock.now)
    mode = gate_module.lookup_mode(2.0, gate._config)
    stale = gate._last_eval.get(CHAT, 0)                     # what both threads saw before the lock
    first = gate._score_gate(CHAT, mode, 2.0, clock.now, observed, stale)
    second = gate._score_gate(CHAT, mode, 2.0, clock.now, observed, stale)
    assert first[0] is True
    assert second == (False, None, "eval interval (taken by a concurrent message)")


def test_only_the_score_mode_has_the_finer_lock_inside_the_state_lock():
    import threading

    def locks(gate):
        return [v for v in vars(gate).values() if isinstance(v, type(threading.Lock()))]

    def state_locks(gate):
        return [v for v in vars(gate).values() if isinstance(v, type(threading.RLock()))]

    assert len(locks(make_gate(mode="score"))) == 1
    assert locks(make_gate(mode="dice")) == []
    assert len(state_locks(make_gate(mode="score"))) == 1 and len(state_locks(make_gate(mode="dice"))) == 1


def test_concurrent_messages_do_not_break_the_score_state():
    gate = make_gate(budget=0)
    errors = []

    def work(name):
        try:
            for index in range(300):
                gate.should_speak({"chat_id": CHAT, "sender_id": name,
                                   "content": f"问题{index}怎么办？", "msg_type": 1})
                gate.record_speech(CHAT)
        except Exception as exc:                      # noqa: BLE001
            errors.append(exc)

    import threading

    threads = [threading.Thread(target=work, args=(f"u{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_a_message_can_opt_out_of_the_score_mode(monkeypatch):
    class Dice:
        def random(self):
            return 0.0                                  # the die always says yes

    gate = make_gate()
    clock = gate_module.time
    monkeypatch.setattr(gate_module, "random", Dice())
    # With the die: a plain statement in a quiet group passes.
    dice_verdict = say(gate, clock, "今天天气不错我们出去走走吧", sender="a", scored=False)
    assert dice_verdict[0] is True and "probability" not in dice_verdict[2] and "score" not in dice_verdict[2]
    # The very same text through the score mode is turned away.
    clock.now += 600
    scored_verdict = say(gate, clock, "今天天气不错我们出去走走吧", sender="a", scored=True)
    assert scored_verdict[0] is False and scored_verdict[2].startswith("score")
    # An opted-out message leaves no trace in the score state.
    assert gate._question == {} and len(gate._recent_texts) == 1


def test_opting_out_is_harmless_when_the_gate_is_in_dice_mode():
    gate = make_gate(mode="dice")
    assert say(gate, gate_module.time, "嗯", sender="a", scored=False)[0] in (True, False)
