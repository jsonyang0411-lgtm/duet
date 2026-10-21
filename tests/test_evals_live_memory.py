"""evals/run_live_memory.py with a scripted model (no network, no key)."""

import json
import re

from evals import run_live_memory as L
from evals.run_memory import load_cases
from src.pipeline.types import ModelOutput


class Reader:
    """Pretends to be a model: turns two kinds of lines into facts, and lies once per window."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, *, schema=None, max_tokens=None, call_name=""):
        self.calls += 1
        facts = []
        for ref, text in re.findall(r"^\[(m\d+)\] \S+ \S+ \S+: (.*)$", user, re.M):
            sender = re.search(rf"\[{ref}\] \S+ \S+ (\S+):", user).group(1)
            clause = re.split("[，。！？,.]", text)[0]          # a quote must stay inside one sentence
            if "爬山" in text:
                facts.append({"kind": "plan", "subject": sender, "predicate": "约了", "object": "爬山",
                              "evidence": [{"id": ref, "quote": clause}], "confidence": 0.9, "ttl_days": 0})
            if "加班" in text:
                facts.append({"kind": "other", "subject": sender, "predicate": "最近在", "object": "加班",
                              "evidence": [{"id": ref, "quote": clause}], "confidence": 0.8, "ttl_days": 0})
        facts.append({"kind": "other", "subject": "路人", "predicate": "是", "object": "骗子",
                      "evidence": [{"id": "m1", "quote": "他就是个骗子啊"}], "confidence": 0.9, "ttl_days": 0})
        answer = {"facts": facts, "episode": {"summary": "闲聊", "topics": [], "evidence": ["m1"]}, "retire": []}
        return ModelOutput(text=json.dumps(answer, ensure_ascii=False),
                           usage={"input_tokens": 1000, "output_tokens": 200})


def test_the_plan_is_printed_and_nothing_runs_without_yes(capsys):
    assert L.main([], summarizer_factory=lambda: (_ for _ in ()).throw(AssertionError("no model")),
                  ) == 0
    out = capsys.readouterr().out
    assert '"windows"' in out and "nothing sent" in out


def test_estimate_counts_windows_per_scenario():
    scenarios, _ = load_cases()
    plan = L.estimate(scenarios, window=120)
    assert plan["scenarios"] == 5 and plan["windows"] == 5
    assert plan["messages"] == sum(len(s["chat"]) for s in scenarios)
    assert L.estimate(scenarios, window=10)["windows"] > plan["windows"]


def test_a_scripted_run_measures_acceptance_recall_and_traceability(tmp_path, capsys):
    reader = Reader()
    code = L.main(["--yes", "--out", str(tmp_path), "--tag", "t", "--price-in", "1", "--price-out", "2"],
                  summarizer_factory=lambda: reader)
    assert code == 0 and reader.calls == 5
    report = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    s = report["summary"]
    assert s["windows"] == {"ok": 5}
    assert s["facts_rejected"] == 5 and s["reject_reasons"] == {"quote_not_in_message": 5}
    assert s["facts_accepted"] >= 2
    assert s["evidence_traceable"] == 1.0                      # re-checked against the stored messages
    assert s["input_tokens"] == 5000 and s["output_tokens"] == 1000
    assert s["cost"] == round(5000 / 1e6 * 1 + 1000 / 1e6 * 2, 4)
    # the reader only knows 爬山 and 加班: those two expected facts exist, the rest are missed
    assert 0 < s["expected_found"] < s["expected_facts"]
    text = (tmp_path / "t.md").read_text(encoding="utf-8")
    assert "not found:" in text and "Rejected because: quote_not_in_message x5" in text
    assert "wrote" in capsys.readouterr().out


def test_expected_fact_matching_uses_subject_and_any_content_keyword():
    facts = [{"subject": "阿红", "predicate": "对", "object": "海鲜过敏"}]
    assert L.found(["阿红", "海鲜|虾"], facts)
    assert L.found(["阿红", "虾|过敏"], facts)
    assert not L.found(["小明", "海鲜"], facts)
    assert not L.found(["阿红", "麻辣烫"], facts)


def test_a_backend_that_cannot_do_json_is_reported(capsys):
    assert L.main(["--yes"], summarizer_factory=lambda: object()) == 1
    assert "cannot run JSON tasks" in capsys.readouterr().out
