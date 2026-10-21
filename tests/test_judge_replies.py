"""evals/judge_replies.py: blind, position-balanced comparison of two live runs."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals import judge_replies as J
from evals import run_offline
from src.send.bubbles import BUBBLE_SEP
from src.send.stickers import sticker_token

CASES = {c["id"]: c for c in run_offline._load_cases([run_offline.DEFAULT_CASES])[0]}
IDS = ["scn-0001", "scn-0002", "scn-0003"]
PERSONA = {"persona_name": "林小满", "persona_prompt": "大一学生，说话软软的。", "max_chars": 40}
SLEPT = []


class FakeJudge:
    model = "judge-x"

    def __init__(self, policy):
        self.policy = policy
        self.prompts = []

    def _chat_persona_settings(self):
        return dict(PERSONA)

    def _call_json_api(self, system, messages, max_tokens=None):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        answer = self.policy(prompt)
        return SimpleNamespace(text=answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False))


def shown(prompt, n):
    return prompt.split(f"回复{n}：", 1)[1].split("\n", 1)[0]


def prefers(text):
    return lambda prompt: {"winner": "1" if text in shown(prompt, 1) else "2", "reason": "更自然"}


def row(case_id, reply, **extra):
    return {"id": case_id, "category": CASES[case_id]["category"], "reply": reply,
            "expect": CASES[case_id].get("expect"), "model_errors": 0, **extra}


def judged(policy, replies_a=("好呀",) * 3, replies_b=("嗯",) * 3, seed=7):
    judge = FakeJudge(policy)
    paired = J.pair_up([row(i, r) for i, r in zip(IDS, replies_a)],
                       [row(i, r) for i, r in zip(IDS, replies_b)])
    results = J.run(judge, CASES, PERSONA, paired["pairs"], seed=seed, sleep=SLEPT.append)
    return judge, J.build_report(results, paired, labels={"a": "local", "b": "cloud"}, judge_model="judge-x")


def test_the_better_reply_wins_in_either_position():
    judge, report = judged(prefers("好呀"))
    assert report["overall"]["score_a"] == 1.0 and report["overall"]["consistency"] == 1.0
    assert report["overall"]["a_better"] == 3 and len(judge.prompts) == 6
    firsts = {shown(p, 1) for p in judge.prompts}
    assert firsts == {"好呀", "嗯"}                         # A was shown first and second
    _, report = judged(prefers("嗯"))
    assert report["overall"]["score_a"] == 0.0


def test_a_judge_that_always_picks_the_first_reply_cancels_out():
    _, report = judged(lambda prompt: {"winner": "1"})
    assert report["overall"]["score_a"] == 0.5 and report["overall"]["consistency"] == 0.0


def test_ties_and_flaws_go_back_to_the_right_side():
    def policy(prompt):
        long_one = "1" if "很长很长" in shown(prompt, 1) else "2"
        return {"winner": "tie", f"flaws_{long_one}": ["too_long", "not_a_flaw"], "reason": "差不多"}

    _, report = judged(policy, replies_a=("很长很长的回复",) * 3)
    overall = report["overall"]
    assert overall["score_a"] == 0.5 and overall["equal"] == 3
    assert overall["flaws_a"] == {"any": 1.0, "too_long": 1.0} and overall["flaws_b"] == {"any": 0.0}


def test_the_prompt_is_blind_and_has_what_the_judge_needs():
    reply_a = "好呀" + BUBBLE_SEP + sticker_token("害羞捂脸")
    judge, _ = judged(prefers("好呀"), replies_a=(reply_a,) * 3)
    prompt = judge.prompts[0]
    case = CASES[IDS[0]]
    assert "林小满" in prompt and "大一学生" in prompt and "40 个字" in prompt
    assert case["context"][0]["content"] in prompt and case["trigger"]["content"] in prompt
    assert "好呀 / [表情包：害羞捂脸]" in prompt and BUBBLE_SEP not in prompt
    assert "local" not in prompt and "cloud" not in prompt and "judge-x" not in prompt
    assert all(flaw in prompt for flaw in J.FLAWS)


def test_only_pairs_where_both_spoke_are_judged():
    rows_a = [row("scn-0001", "好呀"), row("scn-0002", ""), row("scn-0003", "在的", model_errors=1)]
    rows_b = [row("scn-0001", "嗯"), row("scn-0002", "我来"), row("scn-0003", "在")]
    paired = J.pair_up(rows_a, rows_b)
    assert [p[0] for p in paired["pairs"]] == ["scn-0001"]
    assert paired["only_b"] == [{"id": "scn-0002", "expect": CASES["scn-0002"]["expect"]["action"]}]
    assert paired["only_a"] == []                           # an errored case is left out


def test_a_broken_verdict_is_asked_again_then_recorded():
    answers = iter(["不是 JSON", {"winner": "回复1"}] + [{"winner": "2"}] * 5)   # 3 pairs x 2, one retry
    _, report = judged(lambda prompt: next(answers))
    assert report["overall"]["n_pairs"] == 3 and not report["errors"]

    flaky = iter(["坏"] * 2 + [{"winner": "tie"}] * 4)                   # the first judgment fails twice
    _, report = judged(lambda prompt: next(flaky))
    assert len(report["errors"]) == 1 and report["overall"]["n_pairs"] == 2

    with pytest.raises(J.JudgeAborted):
        judged(lambda prompt: "坏")


def test_a_failed_call_is_asked_again_after_a_pause():
    calls = iter([TimeoutError("slow"), RuntimeError("429 Too Many Requests")] + [{"winner": "tie"}] * 6)

    def policy(prompt):
        answer = next(calls)
        if isinstance(answer, Exception):
            raise answer
        return answer

    SLEPT.clear()
    _, report = judged(policy)
    assert report["overall"]["n_pairs"] == 3 and not report["errors"]
    assert SLEPT == list(J.RETRY_WAITS)


def test_an_aborted_run_still_reports_what_was_judged(tmp_path, monkeypatch):
    monkeypatch.setattr(J.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(J, "ABORT_AFTER_ERRORS", 2)
    a = write_report(tmp_path / "local.json", ["好呀"] * 3)
    b = write_report(tmp_path / "cloud.json", ["嗯"] * 3)
    answers = iter([{"winner": "1"}, {"winner": "2"}])

    def policy(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise RuntimeError("503") from None

    out = tmp_path / "out"
    code = J.main(["--a", a, "--b", b, "--yes", "--out", str(out), "--tag", "t"],
                  judge_factory=lambda: FakeJudge(policy))
    report = json.loads((out / "t.json").read_text(encoding="utf-8"))
    assert code == 3 and report["overall"]["n_pairs"] == 1 and len(report["errors"]) == 1


@pytest.mark.parametrize("raw, winner", [
    ('{"winner": "1"}', "1"), ('结果：{"winner": "TIE", "flaws_2": "repetitive"}', "tie"),
    ('{"winner": "回复2"}', "2"),
])
def test_verdicts_are_read_leniently(raw, winner):
    assert J.parse_verdict(raw)["winner"] == winner


@pytest.mark.parametrize("raw", ['{"winner": "3"}', "没有 JSON", '{"reason": "x"}'])
def test_unusable_verdicts_are_refused(raw):
    with pytest.raises(ValueError):
        J.parse_verdict(raw)


def write_report(path, replies, strategy="single"):
    path.write_text(json.dumps({"rows": {strategy: [row(i, r) for i, r in zip(IDS, replies)]}},
                               ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_the_dry_run_calls_nothing(tmp_path, capsys):
    a = write_report(tmp_path / "local.json", ["好呀", "", "嗯"])
    b = write_report(tmp_path / "cloud.json", ["嗯", "在", "好"])

    def no_judge():
        raise AssertionError("the dry run must not build the judge")

    assert J.main(["--a", a, "--b", b], judge_factory=no_judge) == 0
    assert "2 pairs to judge (4 judge calls" in capsys.readouterr().out


def test_a_run_writes_the_report(tmp_path):
    a = write_report(tmp_path / "local.json", ["好呀"] * 3)
    b = write_report(tmp_path / "cloud.json", ["嗯"] * 3, strategy="two_step")
    out = tmp_path / "out"
    code = J.main(["--a", a, "--b", b, "--yes", "--out", str(out), "--tag", "t"],
                  judge_factory=lambda: FakeJudge(prefers("嗯")))
    assert code == 0
    report = json.loads((out / "t.json").read_text(encoding="utf-8"))
    assert report["a"] == "local (single)" and report["b"] == "cloud (two_step)"
    assert report["overall"]["score_a"] == 0.0
    markdown = (out / "t.md").read_text(encoding="utf-8")
    assert "**score of A** | **0.0%**" in markdown and "Pairs B won in both orderings" in markdown


def test_a_report_with_several_strategies_needs_one_named(tmp_path):
    path = tmp_path / "both.json"
    path.write_text(json.dumps({"rows": {"single": [], "two_step": []}}), encoding="utf-8")
    with pytest.raises(ValueError, match="name one"):
        J.main(["--a", str(path), "--b", str(path)])
    assert J.main(["--a", str(path), "--b", str(path), "--a-strategy", "single",
                   "--b-strategy", "two_step"]) == 0


# ── batched judging ─────────────────────────────────────────────────────

def batch_items(prompt):
    """(number, reply 1, reply 2) of every item in a batched prompt."""
    items = []
    for block in prompt.split("### 第 ")[1:]:
        number = int(block.split(" ", 1)[0])
        items.append((number, block.split("回复1：", 1)[1].split("\n", 1)[0],
                      block.split("回复2：", 1)[1].split("\n", 1)[0]))
    return items


class BatchJudge(FakeJudge):
    def __init__(self, decide, skip=()):
        super().__init__(lambda prompt: None)
        self.decide, self.skip, self.calls = decide, set(skip), []

    def _call_json_api(self, system, messages, max_tokens=None):
        prompt = messages[0]["content"]
        items = batch_items(prompt)
        self.calls.append([(first, second) for _, first, second in items])
        verdicts = [{"item": n, "winner": self.decide(first, second), "reason": "x"}
                    for n, first, second in items if (first, second) not in self.skip]
        self.skip.clear()                                  # skipped once only
        return SimpleNamespace(text=json.dumps({"verdicts": verdicts}, ensure_ascii=False))


def batched(judge, replies_a, replies_b, batch):
    ids = [c for c in CASES][: len(replies_a)]
    paired = J.pair_up([row(i, r) for i, r in zip(ids, replies_a)], [row(i, r) for i, r in zip(ids, replies_b)])
    results = J.run_batched(judge, CASES, PERSONA, paired["pairs"], seed=7, batch=batch, sleep=SLEPT.append)
    return J.build_report(results, paired, labels={"a": "local", "b": "cloud"}, judge_model="judge-x")


def test_batches_score_like_single_judgments_and_cost_fewer_calls():
    a = [f"好呀{i}" for i in range(7)]
    b = [f"嗯{i}" for i in range(7)]
    judge = BatchJudge(lambda first, second: "1" if first.startswith("好呀") else "2")
    report = batched(judge, a, b, batch=3)
    assert report["overall"]["score_a"] == 1.0 and report["overall"]["n_pairs"] == 7
    assert len(judge.calls) == 2 * 3                         # 7 pairs, 3 per call, two passes
    for call in judge.calls:                                 # never both orderings of a pair in one call
        texts = [t for pair in call for t in pair]
        assert len(texts) == len(set(texts))
    report = batched(BatchJudge(lambda first, second: "1"), a, b, batch=3)
    assert report["overall"]["score_a"] == 0.5 and report["overall"]["consistency"] == 0.0


def test_the_batch_prompt_keeps_each_chat_apart():
    ids = list(CASES)[:2]
    text = J.build_batch_prompt([(CASES[ids[0]], "甲", "乙"), (CASES[ids[1]], "丙", "丁")], PERSONA)
    first, second = text.split("### 第 2 题")
    assert CASES[ids[0]]["trigger"]["content"] in first and CASES[ids[1]]["trigger"]["content"] in second
    assert "林小满" in text and text.count("## 怎么判") == 1 and '"verdicts"' in text


def test_a_skipped_item_is_asked_again_and_a_bare_list_is_read():
    judge = BatchJudge(lambda first, second: "1" if first == "好呀" else "2", skip={("好呀", "嗯")})
    report = batched(judge, ["好呀", "在的"], ["嗯", "在"], batch=2)
    assert report["overall"]["n_pairs"] == 2 and not report["errors"]
    assert [len(call) for call in judge.calls][:2] == [2, 1]  # the missing item alone
    assert J.parse_batch('[{"item": 1, "winner": "tie"}, {"item": "x"}]') == {
        1: {"winner": "tie", "flaws_1": [], "flaws_2": [], "reason": ""}}


def test_the_cli_plans_batched_calls(tmp_path, capsys):
    a = write_report(tmp_path / "local.json", ["好呀", "在的", "嗯嗯"])
    b = write_report(tmp_path / "cloud.json", ["嗯", "在", "好"])
    assert J.main(["--a", a, "--b", b, "--batch", "2"]) == 0
    assert "3 pairs to judge (4 judge calls" in capsys.readouterr().out


# ── persona cards ───────────────────────────────────────────────────────

def test_the_judge_plays_the_card_both_runs_used(tmp_path):
    from evals.persona_cards import HELD_OUT
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    for path, reply in ((a, "好呀"), (b, "嗯")):
        path.write_text(json.dumps({"persona": "eval-coder",
                                    "rows": {"single": [row(i, reply) for i in IDS]}}, ensure_ascii=False),
                        encoding="utf-8")
    judge = FakeJudge(prefers("好呀"))
    code = J.main(["--a", str(a), "--b", str(b), "--yes", "--out", str(tmp_path / "o"), "--tag", "t",
                   "--persona-cards", str(HELD_OUT), "--persona-id", "eval-coder"], judge_factory=lambda: judge)
    assert code == 0 and "阿K" in judge.prompts[0] and "林小满" not in judge.prompts[0]
    assert json.loads((tmp_path / "o" / "t.json").read_text(encoding="utf-8"))["persona"] == "eval-coder"


def test_runs_with_different_personas_are_not_compared(tmp_path):
    from evals.persona_cards import HELD_OUT
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"persona": "eval-coder", "rows": {"single": []}}), encoding="utf-8")
    b.write_text(json.dumps({"persona": "eval-otaku", "rows": {"single": []}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="same persona"):
        J.main(["--a", str(a), "--b", str(b), "--persona-cards", str(HELD_OUT), "--persona-id", "eval-coder"])
    b.write_text(json.dumps({"persona": "eval-coder", "rows": {"single": []}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="same persona"):
        J.main(["--a", str(a), "--b", str(b)])                     # the judge would use .env's persona
