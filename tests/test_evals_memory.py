"""The memory evaluation (evals/run_memory.py): fixtures are sound, numbers are stable."""

import json

import pytest

from evals import run_memory as R


@pytest.fixture(scope="module")
def report():
    return R.build_report(with_bench=False)


def test_fixtures_are_consistent():
    scenarios, facts = R.load_cases()
    ids = set()
    for scenario in scenarios:
        for question in scenario["questions"]:
            assert question["id"] not in ids
            ids.add(question["id"])
            assert question["kind"] in R.KINDS
            for source, pos in question["expect"]:
                size = len(scenario["chat"] if source == "chat" else scenario["history"])
                assert 0 <= pos < size
            assert (question["kind"] == "no_answer") == (question["expect"] == [])
    assert len(ids) >= 40
    assert {q["kind"] for s in scenarios for q in s["questions"]} == set(R.KINDS)
    for question in facts["questions"]:
        assert all(0 <= i < len(facts["facts"]) for i in question["expect"])


def test_the_evaluation_is_deterministic(report):
    again = R.build_report(with_bench=False)
    assert json.dumps(R.strip_rows(again), sort_keys=True) == json.dumps(R.strip_rows(report), sort_keys=True)


def test_the_new_search_beats_the_old_one_on_answerable_questions(report):
    old = report["retrieval"]["legacy"]["by_kind"]["ALL_ANSWERABLE"]
    new = report["retrieval"]["v2"]["by_kind"]["ALL_ANSWERABLE"]
    assert new["hit@3"] >= 0.9 and old["hit@3"] <= 0.65
    assert new["hit@3"] - old["hit@3"] >= 0.25


def test_no_kind_of_question_got_worse(report):
    old, new = report["retrieval"]["legacy"]["by_kind"], report["retrieval"]["v2"]["by_kind"]
    for kind in ("exact", "paraphrase", "two_char", "distractor", "old"):
        assert new[kind]["hit@3"] >= old[kind]["hit@3"], kind


def test_the_fallback_without_an_index_is_nearly_as_good(report):
    like = report["retrieval"]["v2_like_only"]["by_kind"]["ALL_ANSWERABLE"]
    old = report["retrieval"]["legacy"]["by_kind"]["ALL_ANSWERABLE"]
    assert like["hit@3"] > old["hit@3"] + 0.15


def test_questions_without_an_answer_do_not_flood_the_prompt(report):
    # Known cost of lexical matching: a generic two-character word can still hit.
    # The measured rate is part of the baseline; it must not creep up.
    for system in ("v2", "v2_like_only"):
        assert report["retrieval"][system]["by_kind"]["no_answer"]["false_alarm"] <= 0.2


def test_fact_selection_keeps_the_expected_facts_and_is_more_precise(report):
    facts = report["facts"]
    assert facts["v2"]["recall"] == 1.0
    assert facts["v2"]["precision"] > facts["legacy"]["precision"] * 2
    assert facts["v2"]["injected_per_message"] <= 3


def test_unrelated_small_talk_selects_no_facts_for_people_without_facts(report):
    row = next(r for r in report["facts"]["rows"] if r["id"] == "f6")
    assert row["expected"] == []


def test_known_limits_are_documented_not_hidden(report):
    # These need meaning, not words: the baseline records that they are missed.
    missed = {r["id"] for r in report["retrieval"]["v2"]["rows"] if r["answerable"] and r["rank"] is None}
    assert missed == {"food.q3", "food.q8", "archive.q5"}


def test_the_latency_bench_runs_and_reports_both_searches():
    result = R.bench(n_messages=3000, queries=10)
    assert result["messages"] == 3000
    assert result["v2_ms"]["p95"] < 500 and result["legacy_ms"]["p95"] < 2000


def test_the_markdown_report_names_every_system(report):
    text = R.render_md(report, "t")
    for needle in ("old LIKE search", "no index", "FTS5 + LIKE + recency", "Fact selection"):
        assert needle in text
