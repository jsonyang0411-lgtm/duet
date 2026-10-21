"""evals/run_live_embeddings.py with a fake embeddings model."""

import json
import math
from array import array

from evals import run_live_embeddings as E
from src.memory.embeddings import fact_text

FACTS, QUESTIONS = E.load()


def unit(index, size=len(FACTS) + 1):
    vec = array("f", [0.0] * size)
    vec[index] = 1.0
    return vec


class FakeEmbedder:
    """A fact points one way; a message points at the first fact it expects, or nowhere."""

    model = "fake"

    def __init__(self):
        self.calls = 0
        self.by_text = {fact_text(f): unit(i) for i, f in enumerate(FACTS)}
        for q in QUESTIONS:
            self.by_text[q["text"]] = unit(q["expect"][0]) if q["expect"] else unit(len(FACTS))

    def embed(self, texts, timeout=None):
        self.calls += 1
        return [self.by_text[t] for t in texts]


def test_the_set_is_what_the_report_says():
    assert len(FACTS) == 16 and len(QUESTIONS) == 21
    assert sum(1 for q in QUESTIONS if not q["expect"]) == 6
    assert all(q["people"] == ["小张"] for q in QUESTIONS if q["id"].startswith("s") and q["id"] != "s15")


def test_words_alone_miss_the_paraphrases_but_add_no_noise():
    report = E.evaluate(None, FACTS, QUESTIONS)
    assert report["words_only"] == {"recall": 0.125, "precision": 1.0, "noise_per_empty_question": 0.0}
    assert report["with_vectors"] == {} and report["rows"] == []


def test_good_vectors_find_the_facts_without_noise():
    embedder = FakeEmbedder()
    report = E.evaluate(embedder, FACTS, QUESTIONS, thresholds=(0.5,))
    assert embedder.calls == 2
    assert report["with_vectors"]["0.5"] == {"recall": 1.0, "precision": 1.0, "noise_per_empty_question": 0.0}
    row = next(r for r in report["rows"] if r["id"] == "s01")
    assert row["with_vectors"]["0.5"] == [0] and row["closest"][0] == {"fact": 0, "similarity": 1.0}


def test_the_dry_run_calls_nothing_and_a_run_writes_the_report(tmp_path, capsys):
    def no_embedder():
        raise AssertionError("the dry run must not build the embedder")

    assert E.main(["--model", "m"], embedder_factory=no_embedder) == 0
    assert "Dry run" in capsys.readouterr().out
    assert E.main(["--model", "fake/m:1", "--yes", "--out", str(tmp_path)], embedder_factory=FakeEmbedder) == 0
    report = json.loads((tmp_path / "embeddings_fake_m_1.json").read_text(encoding="utf-8"))
    assert report["schema"] == E.SCHEMA and set(report["with_vectors"]) == {str(t) for t in E.THRESHOLDS}
    assert math.isclose(report["with_vectors"]["0.7"]["recall"], 1.0)
