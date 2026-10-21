"""The message pool calls the router from several threads: shared state is locked."""

import random
import sys
import threading
import time

import pytest

from src.proactive.followup import FollowupListenTracker
from src.proactive.gate import ProactiveGate
from src.proactive.rate_tracker import RateTracker
from src.proactive.sticky import StickyMentionTracker


class CountingRLock:
    """An RLock that counts how often it was taken."""

    def __init__(self):
        self._lock = threading.RLock()
        self.taken = 0

    def __enter__(self):
        self._lock.acquire()
        self.taken += 1
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def make_gate(mode="dice"):
    from src.config import BotConfig
    return ProactiveGate(BotConfig(proactive_enabled=True, proactive_gate_mode=mode))


def calls(kind):
    """Every public method of each shared object, with arguments that work."""
    if kind == "sticky":
        obj = StickyMentionTracker(ttl_sec=60)
        return obj, [lambda: obj.register("c", "s"), lambda: obj.consume("c", "s"),
                     lambda: obj.clear("c", "s"), lambda: len(obj)]
    if kind == "followup":
        obj = FollowupListenTracker()
        msg = {"chat_id": "c", "sender_id": "s", "content": "好的吗"}
        return obj, [lambda: obj.register("c", "你想喝什么？"), lambda: obj.match(msg), lambda: obj.clear("c")]
    if kind == "rate":
        obj = RateTracker(120)
        return obj, [lambda: obj.record("c"), lambda: obj.rate("c"), lambda: obj.count("c"),
                     lambda: obj.cleanup(), lambda: obj.clear("c")]
    obj = make_gate(kind.split("-")[1])
    msg = {"chat_id": "c", "sender_id": "s", "content": "各位怎么把列表去重", "msg_type": 1, "sender_name": "s"}
    return obj, [lambda: obj.should_speak(msg), lambda: obj.record_eval("c"), lambda: obj.record_silence("c"),
                 lambda: obj.record_speech("c"), lambda: obj.get_consecutive_silence("c")]


KINDS = ["sticky", "followup", "rate", "gate-dice", "gate-score"]


@pytest.mark.parametrize("kind", KINDS)
def test_every_public_method_takes_the_lock(kind):
    obj, operations = calls(kind)
    obj._lock = counting = CountingRLock()
    for operation in operations:
        before = counting.taken
        operation()
        assert counting.taken > before, operation


@pytest.mark.parametrize("kind", KINDS)
def test_many_threads_can_use_one_object_at_once(kind, monkeypatch):
    monkeypatch.setattr(sys, "getswitchinterval", lambda: 1e-6, raising=False)
    sys.setswitchinterval(1e-6)
    try:
        obj, operations = calls(kind)
        errors = []

        def work(seed):
            rng = random.Random(seed)
            try:
                for _ in range(600):
                    rng.choice(operations)()
            except Exception as exc:                    # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == [] and not any(t.is_alive() for t in threads)
    finally:
        sys.setswitchinterval(0.005)


def test_a_concurrent_cleanup_cannot_break_the_iteration_of_another_thread():
    tracker = StickyMentionTracker(ttl_sec=60)
    for i in range(2000):
        tracker.register(f"c{i}", "s")
    errors = []

    def churn(offset):
        try:
            for i in range(2000):
                tracker.register(f"d{offset}-{i}", "s")
                tracker.consume(f"c{i}", "s")          # every 200th call runs the cleanup scan
        except Exception as exc:                        # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=churn, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []


def test_the_gate_never_lets_two_threads_pass_the_same_interval():
    gate = make_gate("dice")
    passed = []
    msg = {"chat_id": "c", "sender_id": "s", "content": "各位怎么把列表去重", "msg_type": 1, "sender_name": "s"}
    for _ in range(40):
        gate._tracker.record("c")                      # a busy chat

    def work():
        for _ in range(50):
            ok, mode, _ = gate.should_speak(msg)
            if ok:
                passed.append(time.time())

    import src.proactive.gate as gate_module
    gate_module.random.seed(1)
    original = gate_module.random.random
    # The dice always say yes; they also take a moment, which is exactly the window in
    # which an unlocked gate would let a second thread through the interval check.
    gate_module.random.random = lambda: (time.sleep(0.002), 0.0)[1]
    try:
        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        gate_module.random.random = original
    assert len(passed) == 1                            # one evaluation per interval, however many threads ask


def test_a_new_public_method_cannot_be_added_without_the_lock():
    """The per-method test above lists calls by hand; this one looks at the classes themselves."""
    import inspect
    exempt = {}                                         # {"ClassName.method": "why it needs no lock"}
    for cls in (StickyMentionTracker, FollowupListenTracker, RateTracker, ProactiveGate):
        for name, member in inspect.getmembers(cls, callable):
            if name.startswith("_") and name != "__len__":
                continue
            if f"{cls.__name__}.{name}" in exempt:
                continue
            assert hasattr(member, "__wrapped__"), f"{cls.__name__}.{name} is public but not @locked"
