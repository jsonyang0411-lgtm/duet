"""Blind comparison of two live runs' replies, judged by a model.

    python -m evals.judge_replies --a data/replay/live/local.json --b data/replay/live/cloud.json
    python -m evals.judge_replies --a ... --b ... --yes        # call the judge (costs API credit)
    python -m evals.judge_replies --a ... --b ... --yes --batch 10   # 10 judgments per call

Each call to a relay can carry a fixed overhead (one relay adds about 4400
prompt tokens to every request), so ``--batch N`` puts N judgments into one
call.  The two orderings of a pair are then judged in different calls, and
every judgment still sees its own chat.

``--a`` and ``--b`` are reports written by ``evals.run_live``.  For every
scenario where both runs spoke, the judge (the cloud model in ``.env``, or
``--judge-model`` on the same endpoint) sees the persona, the chat before the
trigger, the trigger and the two replies under the neutral names 回复1 and
回复2, and says which one a group member with this persona would rather have
sent, plus the flaws of each (from a fixed list).  Every pair is judged twice
with the sides swapped, so a judge that prefers a position cancels out.

A's score: a win 1, a tie 0.5, a loss 0, averaged over both judgments of every
pair; 50% means as good as B.  A pair whose two judgments name the same reply
is "consistent"; a low consistency means the judge mostly guesses.

Cases where only one run spoke are listed, not judged: whether to speak is
what ``evals.run_live`` measures.

The judge sees only the scenarios' invented chats.  When B is the judge's own
model, the judge may favour it; A's score is then, if anything, too low.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # `python evals/judge_replies.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from evals import run_offline  # noqa: E402
from src.pipeline.envelope import EnvelopeError, extract_envelope_dict  # noqa: E402
from src.send.bubbles import BUBBLE_SEP  # noqa: E402
from src.send.stickers import sticker_in  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "data" / "replay" / "judge"
JUDGE_SCHEMA = "judge-eval/v1"
JUDGE_MAX_TOKENS = 400
JUDGE_TIMEOUT_SEC = 180.0
#: Stop when this many judgments in a row fail (bad key, outage).
ABORT_AFTER_ERRORS = 3
#: Seconds to wait before asking again after a failed call (a timeout, "429 Too Many Requests").
RETRY_WAITS = (5, 30)

FLAWS = {
    "ignored_request": "没接住对方的请求或问题（比如叫讲笑话没讲、让选一个没选）",
    "off_topic": "答非所问，和上下文对不上",
    "out_of_persona": "不像这个人设，或者像 AI 助手在说话",
    "too_long": "太长，不像群聊里随手发的",
    "repetitive": "重复自己或别人刚说过的话",
    "made_up": "编造了聊天里没有、也不可能知道的事",
}
WINNERS = ("1", "2", "tie")
_MEDIA = {3: "[图片]", 47: "[表情]", 34: "[语音]", 43: "[视频]", 49: "[链接或文件]"}

JUDGE_SYSTEM = "你是严格、公正的评委，比较两条微信群聊回复的好坏。只输出一个 JSON 对象，不要输出别的。"
#: Answer tokens per judgment in a batched call, and on top of all of them.
BATCH_TOKENS_PER_ITEM = 150
BATCH_TOKENS_EXTRA = 100


class JudgeAborted(RuntimeError):
    """The judge endpoint is failing: continuing would only measure the outage."""

    def __init__(self, message: str, results: list | None = None) -> None:
        super().__init__(message)
        self.results = list(results or [])     # what was judged before, still worth a report


# ── the prompt ───────────────────────────────────────────────────────────

def render_reply(reply: str) -> str:
    """A reply as the group sees it: bubbles separated by " / ", stickers named."""
    parts = []
    for part in str(reply or "").split(BUBBLE_SEP):
        name = sticker_in(part)
        if name is not None:
            parts.append(f"[表情包：{name}]")
        elif part.strip():
            parts.append(part.strip())
    return " / ".join(parts)


def _line(message: Mapping[str, Any]) -> str:
    content = str(message.get("content") or "")
    msg_type = int(message.get("msg_type", 1) or 1)
    if msg_type == 49:                                   # a link or file: its title is worth showing
        content = f"{_MEDIA[49]} {content}".strip()
    elif msg_type in _MEDIA:
        content = _MEDIA[msg_type]
    return f"{message.get('sender_name') or message.get('sender_id') or '某人'}：{content}"


def _criteria() -> str:
    flaws = "\n".join(f"- {key}：{text}" for key, text in FLAWS.items())
    return f"""站在群里其他人的角度：哪条更像这个人设的真人群友会发的，并且更好地接住了最新消息？
依次看：1. 有没有接住请求或问题；2. 和上下文对不对得上；3. 像不像这个人设、像不像真人；4. 简短自然。
两条差不多就判 tie。不要因为更长、更礼貌或用了表情包就加分。
硬伤只能从下面选（没有就留空）：
{flaws}"""


def _chat_parts(case: Mapping[str, Any]) -> tuple[str, str, str]:
    """(where, the earlier chat, the latest message) of one scenario."""
    trigger = case.get("trigger") or {}
    chat = case.get("chat") or {}
    context = [_line(m) for m in case.get("context") or []]
    quoted = ""
    if trigger.get("quoted_content"):
        quoted = f"\n（引用了 {trigger.get('quoted_sender_name') or '某人'}：{trigger['quoted_content']}）"
    where = f"群聊「{chat.get('group_name') or '某群'}」" if chat.get("is_group", True) else "私聊"
    earlier = chr(10).join(context) if context else "（没有更早的消息）"
    return where, earlier, _line(trigger) + quoted


def build_prompt(case: Mapping[str, Any], persona: Mapping[str, Any], first: str, second: str) -> str:
    """The judge's question for one ordering of a pair."""
    where, earlier, latest = _chat_parts(case)
    return f"""## 人设
名字：{persona.get('persona_name') or case.get('bot_name') or '机器人'}（在群里被叫作 {case.get('bot_name') or '机器人'}）
{persona.get('persona_prompt') or ''}
回复一般不超过 {persona.get('max_chars') or 60} 个字。

## {where}的最近聊天
{earlier}

## 最新消息（两条回复都是在回应这条）
{latest}

## 两条候选回复
回复1：{first}
回复2：{second}

## 怎么判
{_criteria()}

输出格式：{{"winner": "1" 或 "2" 或 "tie", "flaws_1": [...], "flaws_2": [...], "reason": "一句话说明"}}"""


def build_batch_prompt(items: Sequence[tuple[Mapping[str, Any], str, str]], persona: Mapping[str, Any]) -> str:
    """Several independent judgments in one question: (case, reply 1, reply 2) each."""
    blocks = []
    for number, (case, first, second) in enumerate(items, 1):
        where, earlier, latest = _chat_parts(case)
        blocks.append(f"""### 第 {number} 题（{where}，你在这里被叫作 {case.get('bot_name') or '机器人'}）
最近聊天：
{earlier}
最新消息（两条回复都是在回应这条）：{latest}
回复1：{first}
回复2：{second}""")
    return f"""## 人设
名字：{persona.get('persona_name') or '机器人'}
{persona.get('persona_prompt') or ''}
回复一般不超过 {persona.get('max_chars') or 60} 个字。

下面有 {len(items)} 道题，每道是一段群聊和两条候选回复。各题互相独立，分别判断。

{chr(10).join(blocks)}

## 怎么判（每道题都这样判）
{_criteria()}

输出格式：{{"verdicts": [{{"item": 题号, "winner": "1" 或 "2" 或 "tie", "flaws_1": [...], "flaws_2": [...], "reason": "一句话说明"}}, ...]}}，每道题一项。"""


def parse_verdict(raw: str) -> dict:
    """The judge's answer, checked; raises ValueError when it is not usable."""
    try:
        data = extract_envelope_dict(raw)
    except EnvelopeError as exc:
        raise ValueError(f"no JSON: {exc}") from exc
    return _verdict_from(data)


def parse_batch(raw: str) -> dict[int, dict]:
    """The usable verdicts of a batched answer, by item number (broken ones left out)."""
    text = str(raw or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = extract_envelope_dict(text)
        except EnvelopeError as exc:
            raise ValueError(f"no JSON: {exc}") from exc
    items = data if isinstance(data, list) else (data.get("verdicts") or data.get("items") or [])
    verdicts = {}
    for item in items if isinstance(items, list) else []:
        try:
            verdicts[int(item.get("item"))] = _verdict_from(item)
        except (AttributeError, TypeError, ValueError):
            continue
    return verdicts


def _verdict_from(data: Mapping[str, Any]) -> dict:
    winner = str(data.get("winner", "")).strip().lower()
    winner = {"回复1": "1", "回复2": "2", "平": "tie", "平局": "tie"}.get(winner, winner)
    if winner not in WINNERS:
        raise ValueError(f"bad winner {data.get('winner')!r}")

    def flaws(key):
        value = data.get(key) or []
        if isinstance(value, str):
            value = [value]
        return sorted({str(v).strip() for v in value if str(v).strip() in FLAWS})

    return {"winner": winner, "flaws_1": flaws("flaws_1"), "flaws_2": flaws("flaws_2"),
            "reason": str(data.get("reason") or "")[:200]}


# ── pairing the runs ─────────────────────────────────────────────────────

def strategy_rows(report: Mapping[str, Any], strategy: str | None) -> tuple[str, list[dict]]:
    rows = report.get("rows") or {}
    if strategy is None:
        if len(rows) != 1:
            raise ValueError(f"the report has the strategies {sorted(rows)}: name one")
        strategy = next(iter(rows))
    if strategy not in rows:
        raise ValueError(f"the report has no strategy {strategy!r} (it has {sorted(rows)})")
    return strategy, list(rows[strategy])


def pair_up(rows_a: Sequence[Mapping], rows_b: Sequence[Mapping]) -> dict:
    """Scenarios both runs answered, and the ones only one of them did."""
    a = {r["id"]: r for r in rows_a if not r.get("model_errors")}
    b = {r["id"]: r for r in rows_b if not r.get("model_errors")}
    common = [k for k in a if k in b]

    def expect(row):
        return (row.get("expect") or {}).get("action")

    return {
        "pairs": [(k, a[k], b[k]) for k in common if a[k].get("reply") and b[k].get("reply")],
        "only_a": [{"id": k, "expect": expect(a[k])} for k in common if a[k].get("reply") and not b[k].get("reply")],
        "only_b": [{"id": k, "expect": expect(a[k])} for k in common if b[k].get("reply") and not a[k].get("reply")],
    }


# ── judging ──────────────────────────────────────────────────────────────

def _ask(judge, prompt: str, max_tokens: int, sleep: Callable[[float], None] | None = None) -> str:
    """The judge's raw answer; a failed call (a timeout, a rate limit) is asked
    again after a pause, twice (RETRY_WAITS)."""
    sleep = sleep or time.sleep
    waits = list(RETRY_WAITS)
    while True:
        try:
            output = judge._call_json_api(JUDGE_SYSTEM, [{"role": "user", "content": prompt}],
                                          max_tokens=max_tokens)
            return str(getattr(output, "text", output) or "")
        except Exception:                                 # noqa: BLE001 - wait and try again
            if not waits:
                raise
            sleep(waits.pop(0))


def _judge_once(judge, prompt: str, sleep: Callable[[float], None] | None = None) -> dict:
    """One judgment; a broken answer is asked once more."""
    last: Exception | None = None
    for _ in range(2):
        try:
            return parse_verdict(_ask(judge, prompt, JUDGE_MAX_TOKENS, sleep))
        except ValueError as exc:
            last = exc
        except Exception as exc:                          # noqa: BLE001 - the call failed for good
            raise ValueError(f"{type(exc).__name__}: {exc}") from exc
    raise ValueError(f"{type(last).__name__}: {last}")


def _fold(verdict: Mapping[str, Any], a_is_1: bool) -> dict:
    """A verdict about 回复1 / 回复2, told from A's side."""
    slot_a, slot_b = ("1", "2") if a_is_1 else ("2", "1")
    return {
        "a_shown_as": slot_a,
        "winner": {"tie": "tie", slot_a: "a", slot_b: "b"}[verdict["winner"]],
        "flaws_a": verdict[f"flaws_{slot_a}"], "flaws_b": verdict[f"flaws_{slot_b}"],
        "reason": verdict["reason"],
    }


def _combine(judgments: Sequence[Mapping]) -> dict:
    points = [{"a": 1.0, "tie": 0.5, "b": 0.0}[j["winner"]] for j in judgments]
    return {
        "score_a": sum(points) / len(points),
        "consistent": judgments[0]["winner"] == judgments[1]["winner"],
        "judgments": list(judgments),
    }


def judge_pair(judge, case, persona, reply_a: str, reply_b: str, *, a_first: bool,
               sleep: Callable[[float], None] | None = None) -> dict:
    """Both orderings of one pair, folded back to A's point of view."""
    text_a, text_b = render_reply(reply_a), render_reply(reply_b)
    judgments = []
    for a_is_1 in ((True, False) if a_first else (False, True)):
        first, second = (text_a, text_b) if a_is_1 else (text_b, text_a)
        judgments.append(_fold(_judge_once(judge, build_prompt(case, persona, first, second), sleep), a_is_1))
    return _combine(judgments)


def run_batched(judge, cases: Mapping[str, Mapping], persona: Mapping, pairs: Sequence, *, seed: int,
                batch: int, progress: Callable[[str], None] | None = None,
                sleep: Callable[[float], None] | None = None) -> list[dict]:
    """Like ``run``, ``batch`` judgments per call.  The first pass shows every pair
    in one order, the second in the other, each pass in its own shuffled order,
    so the two judgments of a pair never share a call.  Items the judge skipped
    or garbled are asked again once, in a call of their own."""
    rng = random.Random(seed)
    for case_id, _, _ in pairs:
        if case_id not in cases:
            raise ValueError(f"scenario {case_id!r} is not in --cases")
    a_first = {case_id: rng.random() < 0.5 for case_id, _, _ in pairs}
    by_id = {case_id: (row_a, row_b) for case_id, row_a, row_b in pairs}
    judgments: dict[str, list] = {case_id: [] for case_id in by_id}
    errors: dict[str, str] = {}

    def results():
        out = []
        for case_id, (row_a, row_b) in by_id.items():
            if len(judgments[case_id]) == 2:
                out.append({"id": case_id, "category": row_a.get("category"),
                            "reply_a": row_a["reply"], "reply_b": row_b["reply"],
                            **_combine(judgments[case_id])})
            elif case_id in errors or judgments[case_id]:
                out.append({"id": case_id, "error": errors.get(case_id, "a judgment is missing")[:200]})
        return out

    def ask(jobs):
        """Judge (case_id, a_is_1) jobs in one call; return the jobs left unjudged."""
        items = []
        for case_id, a_is_1 in jobs:
            row_a, row_b = by_id[case_id]
            text_a, text_b = render_reply(row_a["reply"]), render_reply(row_b["reply"])
            items.append((cases[case_id], *((text_a, text_b) if a_is_1 else (text_b, text_a))))
        max_tokens = BATCH_TOKENS_PER_ITEM * len(items) + BATCH_TOKENS_EXTRA
        try:
            verdicts = parse_batch(_ask(judge, build_batch_prompt(items, persona), max_tokens, sleep))
        except Exception as exc:                          # noqa: BLE001 - recorded below
            for case_id, _ in jobs:
                errors[case_id] = f"{type(exc).__name__}: {exc}"
            return list(jobs)
        left = []
        for number, (case_id, a_is_1) in enumerate(jobs, 1):
            if number in verdicts:
                judgments[case_id].append(_fold(verdicts[number], a_is_1))
            else:
                left.append((case_id, a_is_1))
        return left

    failures_in_a_row = 0
    done = 0
    for first_pass in (True, False):
        order = list(by_id)
        rng.shuffle(order)
        jobs = [(case_id, a_first[case_id] if first_pass else not a_first[case_id]) for case_id in order]
        for start in range(0, len(jobs), batch):
            chunk = jobs[start:start + batch]
            left = ask(chunk)
            if left:
                left = ask(left)                          # once more, just the missing ones
            for case_id, _ in left:
                errors.setdefault(case_id, "the judge left this item out")
            failures_in_a_row = failures_in_a_row + 1 if len(left) == len(chunk) else 0
            if failures_in_a_row >= ABORT_AFTER_ERRORS:
                raise JudgeAborted(f"the last {ABORT_AFTER_ERRORS} calls failed: "
                                   f"{errors.get(chunk[-1][0], '')}", results())
            done += len(chunk)
            if progress:
                progress(f"{done}/{2 * len(jobs)} judgments")
    return results()


def _flaw_rates(results: Sequence[Mapping], side: str) -> dict:
    """How often each flaw was named, per pair (each of the two judgments counts half)."""
    counts: dict[str, float] = defaultdict(float)
    any_flaw = 0.0
    for result in results:
        for judgment in result["judgments"]:
            share = 1 / len(result["judgments"])
            for flaw in judgment[f"flaws_{side}"]:
                counts[flaw] += share
            if judgment[f"flaws_{side}"]:
                any_flaw += share
    n = len(results)
    return {
        "any": round(any_flaw / n, 4) if n else None,
        **{flaw: round(counts[flaw] / n, 4) for flaw in FLAWS if counts.get(flaw)},
    }


def summarize(results: Sequence[Mapping]) -> dict:
    n = len(results)
    if not n:
        return {"n_pairs": 0, "score_a": None, "consistency": None, "flaws_a": {}, "flaws_b": {}}
    return {
        "n_pairs": n,
        "score_a": round(sum(r["score_a"] for r in results) / n, 4),
        "a_better": sum(1 for r in results if r["score_a"] > 0.5),
        "equal": sum(1 for r in results if r["score_a"] == 0.5),
        "b_better": sum(1 for r in results if r["score_a"] < 0.5),
        "consistency": round(sum(1 for r in results if r["consistent"]) / n, 4),
        "flaws_a": _flaw_rates(results, "a"),
        "flaws_b": _flaw_rates(results, "b"),
    }


def run(judge, cases: Mapping[str, Mapping], persona: Mapping, pairs: Sequence, *, seed: int,
        progress: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] | None = None) -> list[dict]:
    rng = random.Random(seed)
    results = []
    failures_in_a_row = 0
    for index, (case_id, row_a, row_b) in enumerate(pairs, 1):
        case = cases.get(case_id)
        if case is None:
            raise ValueError(f"scenario {case_id!r} is not in --cases")
        try:
            result = judge_pair(judge, case, persona, row_a["reply"], row_b["reply"],
                                a_first=rng.random() < 0.5, sleep=sleep)
            failures_in_a_row = 0
        except Exception as exc:                          # noqa: BLE001 - recorded, run goes on
            failures_in_a_row += 1
            if failures_in_a_row >= ABORT_AFTER_ERRORS:
                raise JudgeAborted(f"the last {ABORT_AFTER_ERRORS} judgments failed: {exc}",
                                   results) from exc
            results.append({"id": case_id, "error": str(exc)[:200]})
            continue
        results.append({"id": case_id, "category": row_a.get("category"),
                        "reply_a": row_a["reply"], "reply_b": row_b["reply"], **result})
        if progress:
            progress(f"{index}/{len(pairs)} {case_id}")
    return results


def build_report(results: Sequence[Mapping], paired: Mapping, *, labels: Mapping[str, str],
                 judge_model: str) -> dict:
    judged = [r for r in results if "error" not in r]
    by_category = defaultdict(list)
    for result in judged:
        by_category[result.get("category") or "?"].append(result)
    return {
        "schema": JUDGE_SCHEMA,
        "a": labels["a"], "b": labels["b"], "judge_model": judge_model,
        "overall": summarize(judged),
        "by_category": {k: summarize(v) for k, v in sorted(by_category.items())},
        "errors": [r for r in results if "error" in r],
        "only_a_spoke": paired["only_a"],
        "only_b_spoke": paired["only_b"],
        "results": list(results),
    }


def _pct(value) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: Mapping) -> str:
    overall = report["overall"]
    lines = [
        f"# Blind reply comparison: A = {report['a']}, B = {report['b']}",
        "",
        f"Judge: `{report['judge_model']}`. Each pair judged twice with the sides swapped. "
        "Score of A: win 1, tie 0.5, loss 0 (50% = as good as B).",
        "",
        "| | value |",
        "| --- | ---: |",
        f"| pairs judged | {overall['n_pairs']} |",
        f"| **score of A** | **{_pct(overall['score_a'])}** |",
    ]
    if overall["n_pairs"]:
        lines += [
            f"| A better / equal / B better | {overall['a_better']} / {overall['equal']} / {overall['b_better']} |",
            f"| judge consistency (both orderings agree) | {_pct(overall['consistency'])} |",
            f"| pairs where A has a flaw | {_pct(overall['flaws_a'].get('any'))} |",
            f"| pairs where B has a flaw | {_pct(overall['flaws_b'].get('any'))} |",
        ]
    lines += [f"| judge errors | {len(report['errors'])} |",
              f"| only A spoke / only B spoke (not judged) | {len(report['only_a_spoke'])} / {len(report['only_b_spoke'])} |",
              "", "## Flaws (share of pairs)", "", "| flaw | A | B |", "| --- | ---: | ---: |"]
    for flaw in FLAWS:
        lines.append(f"| {flaw} | {_pct(overall['flaws_a'].get(flaw, 0.0 if overall['n_pairs'] else None))} "
                     f"| {_pct(overall['flaws_b'].get(flaw, 0.0 if overall['n_pairs'] else None))} |")
    lines += ["", "## By category", "", "| category | pairs | score of A | consistency |",
              "| --- | ---: | ---: | ---: |"]
    for category, summary in report["by_category"].items():
        lines.append(f"| {category} | {summary['n_pairs']} | {_pct(summary['score_a'])} "
                     f"| {_pct(summary['consistency'])} |")
    lost = [r for r in report["results"] if "error" not in r and r["score_a"] == 0.0]
    if lost:
        lines += ["", "## Pairs B won in both orderings", ""]
        for result in lost:
            reason = result["judgments"][0]["reason"]
            lines.append(f"- `{result['id']}` A: {render_reply(result['reply_a'])} | "
                         f"B: {render_reply(result['reply_b'])} | {reason}")
    return "\n".join(lines) + "\n"


# ── running ──────────────────────────────────────────────────────────────

def _load_report(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None, judge_factory: Callable[[], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.judge_replies",
        description="Blind comparison of two evals.run_live reports, judged by a model (costs API credit).",
    )
    parser.add_argument("--a", required=True, help="the run_live report under test (e.g. the local model)")
    parser.add_argument("--b", required=True, help="the run_live report to compare with (e.g. the cloud model)")
    parser.add_argument("--a-strategy", help="which strategy of --a (needed when it has several)")
    parser.add_argument("--b-strategy", help="which strategy of --b")
    parser.add_argument("--a-label", help="name of A in the report (default: the file name)")
    parser.add_argument("--b-label", help="name of B in the report")
    parser.add_argument("--cases", nargs="+", default=[str(run_offline.DEFAULT_CASES)],
                        help="the scenario files the runs used")
    parser.add_argument("--judge-model", help="another model name on the cloud endpoint in .env")
    parser.add_argument("--limit", type=int, default=0, help="only the first N pairs")
    parser.add_argument("--batch", type=int, default=1,
                        help="judgments per call (default 1; 10 is much cheaper on a relay with a fixed overhead)")
    parser.add_argument("--seed", type=int, default=run_offline.DEFAULT_SEED)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--tag", default="judge")
    parser.add_argument("--persona-cards", help="the persona card file both runs used (evals/persona_cards.py)")
    parser.add_argument("--persona-id", help="which card of --persona-cards")
    parser.add_argument("--yes", action="store_true",
                        help="actually call the judge (without it only the plan is printed)")
    args = parser.parse_args(argv)

    report_a, report_b = _load_report(args.a), _load_report(args.b)
    personas = {report_a.get("persona"), report_b.get("persona")}
    card = None
    if args.persona_cards:
        from evals.persona_cards import load_cards, pick_card
        card = pick_card(load_cards(args.persona_cards), args.persona_id)
    if len(personas) > 1 or (card is not None and personas != {card["id"]}) or (card is None and personas != {None}):
        raise SystemExit(f"the runs and the judge must use the same persona: A {report_a.get('persona')!r}, "
                         f"B {report_b.get('persona')!r}, judge {card['id'] if card else None!r}")
    strategy_a, rows_a = strategy_rows(report_a, args.a_strategy)
    strategy_b, rows_b = strategy_rows(report_b, args.b_strategy)
    labels = {"a": args.a_label or f"{Path(args.a).stem} ({strategy_a})",
              "b": args.b_label or f"{Path(args.b).stem} ({strategy_b})"}
    loaded, _skipped = run_offline._load_cases([Path(p).resolve() for p in args.cases])
    cases = {c["id"]: c for c in loaded}
    paired = pair_up(rows_a, rows_b)
    pairs = paired["pairs"][: args.limit] if args.limit else paired["pairs"]
    batch = max(1, args.batch)
    calls = 2 * len(pairs) if batch == 1 else 2 * -(-len(pairs) // batch)
    print(f"A = {labels['a']}, B = {labels['b']}: {len(pairs)} pairs to judge "
          f"({calls} judge calls, plus retries); only A spoke in {len(paired['only_a'])}, "
          f"only B in {len(paired['only_b'])}")
    if not args.yes:
        print("Dry run: nothing was sent. Add --yes to call the judge configured in .env.")
        return 0

    if judge_factory is None:
        from src.config import load_config
        from src.summarize import create_summarizer

        judge_factory = lambda: create_summarizer(load_config())  # noqa: E731  (the cloud model)
    judge = judge_factory()
    if args.judge_model:
        judge.model = args.judge_model
    # A batch of long replies takes the judge a while to answer; the bot's own
    # timeout cut such calls off (round of 2026-10-21: 8 of 20 judgments lost).
    judge.timeout_floor = max(float(getattr(judge, "timeout_floor", 0) or 0), JUDGE_TIMEOUT_SEC)
    persona = dict(card) if card is not None else judge._chat_persona_settings()
    aborted = None
    try:
        progress = lambda text: print(f"\r{text}    ", end="", file=sys.stderr)  # noqa: E731
        if batch == 1:
            results = run(judge, cases, persona, pairs, seed=args.seed, progress=progress)
        else:
            results = run_batched(judge, cases, persona, pairs, seed=args.seed, batch=batch,
                                  progress=progress)
    except JudgeAborted as error:
        print(f"\nAborted: {error}", file=sys.stderr)
        aborted, results = error, error.results
        if not any("error" not in r for r in results):
            return 3
        print("The report below covers only the pairs judged before the stop.", file=sys.stderr)
    print(file=sys.stderr)
    report = build_report(results, paired, labels=labels, judge_model=str(getattr(judge, "model", "?")))
    if card is not None:
        report["persona"] = card["id"]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{args.tag}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
    markdown = render_markdown(report)
    (out_dir / f"{args.tag}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out_dir / (args.tag + '.json')} and .md")
    return 3 if aborted else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
