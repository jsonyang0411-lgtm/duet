"""Pure metric functions for the offline harness.

Nothing in here imports production code or touches the filesystem, so the
definitions can be unit-tested on tiny hand-made inputs and re-used unchanged
when a later phase swaps the router implementation behind the replay.

Vocabulary
----------
Routing outcome (what the *current router* did with the trigger message, with
a stub model that always answers "好"):

``engaged_chat``       the chat model was called (@, quote of bot, keyword,
                       sticky, follow-up, private chat, full-feed persona)
``engaged_vision``     the vision model was called (image question / image
                       follow-up)
``engaged_proactive``  the proactive (ambient) path would call the model if
                       its probability roll succeeded (the roll is forced
                       open in the replay; the probability is reported)
``handled_locally``    answered without a model (todo / lots / tarot / help /
                       canned fallbacks)
``none``               nothing happened
``error``              the replay itself raised (should never happen)
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

OUTCOMES = (
    "engaged_chat", "engaged_vision", "engaged_proactive",
    "handled_locally", "none", "error",
)

#: Outcomes that count as "the bot did respond to an addressed message".
ADDRESSED_OK = frozenset({"engaged_chat", "engaged_vision", "handled_locally"})
#: Probability-gated ambient path (content-blind; fires with p = mode probability).
PROACTIVE = "engaged_proactive"
#: Any engagement at all (used for the "false engagement" upper bound).
ENGAGED_ANY = ADDRESSED_OK | {PROACTIVE}

#: Categories whose expected handler is a local command, not the chat model.
LOCAL_CATEGORIES = frozenset({"todo_command", "lots_command", "help_command"})

#: A cleaned reply ending in one of these was cut (or written) at a sentence end.
SENTENCE_END = tuple("。！？!?…~～.”」’）)》")
# Clause breaks: a cut right before / after one of these is not "mid-sentence".
CLAUSE_BREAK = tuple("，、；,;")


#: Addressing levels of the structured pipeline, strongest first.
LEVELS = ("explicit", "strong", "weak", "ambient")
#: Levels at which the model is asked and may decline ("silent").
MODEL_DECIDES = frozenset({"strong", "weak", "ambient"})
#: Fewest cases per side before a separation AUC is reported.
MIN_AUC_CASES = 5


# ── small helpers ────────────────────────────────────────────────────────

def ratio(k: int, n: int) -> dict:
    """``{"k": hits, "n": total, "rate": k/n or None}`` (rate rounded to 4 dp)."""
    return {"k": int(k), "n": int(n), "rate": None if not n else round(k / n, 4)}


def fmt_ratio(value: Mapping[str, Any]) -> str:
    """Render a :func:`ratio` dict as ``k/n (xx.x%)`` (``k/0 (n/a)`` if empty)."""
    n = value.get("n", 0)
    if not n:
        return f"{value.get('k', 0)}/0 (n/a)"
    return f"{value['k']}/{n} ({100.0 * value['k'] / n:.1f}%)"


def distribution(counter: Mapping[str, int], order: Sequence[str]) -> dict:
    """Normalise ``counter`` to fractions over ``order`` (rounded to 4 dp)."""
    total = sum(counter.get(k, 0) for k in order)
    return {
        k: (round(counter.get(k, 0) / total, 4) if total else 0.0) for k in order
    }


def _action(result: Mapping[str, Any]) -> str:
    """``speak`` / ``silent`` / ``either``, or ``unlabeled`` (``expect`` null)."""
    expect = result.get("expect")
    return expect["action"] if expect else "unlabeled"


def _strength(result: Mapping[str, Any]) -> str:
    expect = result.get("expect")
    return expect["strength"] if expect else ""


# ── routing replay ───────────────────────────────────────────────────────

def routing_summary(
    results: Sequence[Mapping[str, Any]],
    *,
    local_categories: Iterable[str] = LOCAL_CATEGORIES,
) -> dict:
    """Aggregate per-case routing results into the Phase 0 headline metrics.

    Each result needs ``id``, ``category``, ``outcome``, ``route`` and
    ``expect = {action, strength, addressee}`` (``None`` for an unlabeled
    case: it only contributes to the outcome counts and the ``unlabeled``
    engagement block); ``addressing`` (``explicit`` = @ or quote of the bot,
    ``implicit`` = neither, ``private`` = one-to-one chat) and ``local_empty``
    are optional.

    Definitions (see evals/README.md):

    * ``addressing_recall``  - of cases with ``expect.action == speak`` and
      ``strength == must``, the share whose outcome is ``engaged_chat``,
      ``engaged_vision`` or ``handled_locally``.
    * ``false_engagement``   - of cases with ``expect.action == silent``, the
      share where the router engaged anyway.  ``rate_deterministic`` counts the
      always-on routes only; ``rate_any`` also counts ``engaged_proactive``
      (an upper bound: that path fires with probability <= 0.7, not 1).
    * ``cannot_engage``      - ``speak`` cases whose outcome is ``none``: the
      router has no route at all for them (implicit addressing).  Cases where
      a local handler matched but replied with nothing are reported separately
      as ``local_empty_reply``.
    * ``unexpected_local``   - cases handled by a local command handler that
      are not in a command category (possible command hijacks).
    """
    local_cats = frozenset(local_categories)
    results = list(results)

    speak = [r for r in results if _action(r) == "speak"]
    must_speak = [r for r in speak if _strength(r) == "must"]
    silent = [r for r in results if _action(r) == "silent"]
    silent_must = [r for r in silent if _strength(r) == "must"]
    either = [r for r in results if _action(r) == "either"]
    unlabeled = [r for r in results if _action(r) == "unlabeled"]

    def count(rows: Iterable[Mapping[str, Any]], outcomes: Iterable[str]) -> int:
        wanted = frozenset(outcomes)
        return sum(1 for r in rows if r["outcome"] in wanted)

    outcome_counts = Counter(r["outcome"] for r in results)
    # A local handler that matched but sent nothing is a bug, not a missing
    # route: keep it out of the "no route" list and report it on its own.
    local_empty = [r for r in results if r.get("local_empty")]
    cannot_engage = [
        r for r in speak if r["outcome"] == "none" and not r.get("local_empty")
    ]
    chance_only = [r for r in speak if r["outcome"] == PROACTIVE]
    unexpected_local = [
        r for r in results
        if r["outcome"] == "handled_locally" and r["category"] not in local_cats
    ]

    def _ids(rows: Iterable[Mapping[str, Any]]) -> list[str]:
        return [r["id"] for r in rows]

    summary = {
        "n_cases": len(results),
        "outcomes": {k: outcome_counts.get(k, 0) for k in OUTCOMES},
        "expect_counts": {
            "speak_must": len(must_speak),
            "speak_should": len(speak) - len(must_speak),
            "silent_must": len(silent_must),
            "silent_should": len(silent) - len(silent_must),
            "either": len(either),
            "unlabeled": len(unlabeled),
        },
        "unlabeled": {
            "n": len(unlabeled),
            "engaged_always_on": ratio(count(unlabeled, ADDRESSED_OK), len(unlabeled)),
            "engaged_incl_proactive": ratio(count(unlabeled, ENGAGED_ANY), len(unlabeled)),
        },
        "addressing_recall": {
            "speak_must": ratio(count(must_speak, ADDRESSED_OK), len(must_speak)),
            "speak_any": ratio(count(speak, ADDRESSED_OK), len(speak)),
        },
        "false_engagement": {
            "silent_any": {
                "deterministic": ratio(count(silent, ADDRESSED_OK), len(silent)),
                "incl_proactive": ratio(count(silent, ENGAGED_ANY), len(silent)),
                "proactive_open_only": count(silent, [PROACTIVE]),
            },
            "silent_must": {
                "deterministic": ratio(count(silent_must, ADDRESSED_OK), len(silent_must)),
                "incl_proactive": ratio(count(silent_must, ENGAGED_ANY), len(silent_must)),
                "proactive_open_only": count(silent_must, [PROACTIVE]),
            },
            "either_engaged": ratio(count(either, ENGAGED_ANY), len(either)),
        },
        "cannot_engage": {
            "speak_must": ratio(
                sum(1 for r in cannot_engage if _strength(r) == "must"),
                len(must_speak),
            ),
            "speak_any": ratio(len(cannot_engage), len(speak)),
            "ids": _ids(cannot_engage),
        },
        "chance_only": {
            "speak_any": ratio(len(chance_only), len(speak)),
            "ids": _ids(chance_only),
        },
        "unexpected_local": {"n": len(unexpected_local), "ids": _ids(unexpected_local)},
        "local_empty_reply": {"n": len(local_empty), "ids": _ids(local_empty)},
        "false_engagement_ids": _ids(
            r for r in silent if r["outcome"] in ENGAGED_ANY
        ),
        "missed_must_ids": _ids(r for r in must_speak if r["outcome"] not in ADDRESSED_OK),
    }

    # expect.action x outcome matrix
    matrix: dict[str, dict[str, int]] = {}
    for action in ("speak", "silent", "either", "unlabeled"):
        if action == "unlabeled" and not unlabeled:
            continue
        row = Counter(r["outcome"] for r in results if _action(r) == action)
        matrix[action] = {k: row.get(k, 0) for k in OUTCOMES}
    summary["expect_vs_outcome"] = matrix

    # Does the model's context end with the message it is answering?  Only
    # cases where a chat / proactive model call was made carry the flag.
    with_context = [r for r in results if r.get("context_has_trigger") is not None]
    summary["context"] = {
        "latest_in_context": ratio(
            sum(1 for r in with_context if r["context_has_trigger"]),
            len(with_context),
        ),
        "missing_ids": _ids(r for r in with_context if not r["context_has_trigger"]),
    }

    pipeline = pipeline_summary(results)
    if pipeline is not None:
        summary["pipeline"] = pipeline

    # per-route view of every engagement
    routes: dict[str, dict[str, int]] = {}
    for r in results:
        if r["outcome"] not in ENGAGED_ANY:
            continue
        slot = routes.setdefault(
            r["route"],
            {"total": 0, "wanted": 0, "unwanted": 0, "neutral": 0, "unlabeled": 0},
        )
        slot["total"] += 1
        action = _action(r)
        slot["wanted" if action == "speak" else
             "unwanted" if action == "silent" else
             "neutral" if action == "either" else "unlabeled"] += 1
    summary["by_route"] = {k: routes[k] for k in sorted(routes)}

    # explicit @ / quote-of-bot vs implicit addressing vs private chat
    by_addressing: dict[str, dict] = {}
    for kind in ("explicit", "implicit", "private"):
        rows = [r for r in results if r.get("addressing") == kind]
        if not rows:
            continue
        k_must = [r for r in rows if _action(r) == "speak" and _strength(r) == "must"]
        k_speak = [r for r in rows if _action(r) == "speak"]
        k_silent = [r for r in rows if _action(r) == "silent"]
        by_addressing[kind] = {
            "n": len(rows),
            "speak_must": ratio(count(k_must, ADDRESSED_OK), len(k_must)),
            "speak_any": ratio(count(k_speak, ADDRESSED_OK), len(k_speak)),
            "silent_engaged_deterministic": ratio(
                count(k_silent, ADDRESSED_OK), len(k_silent),
            ),
            "silent_engaged_incl_proactive": ratio(
                count(k_silent, ENGAGED_ANY), len(k_silent),
            ),
        }
    summary["by_addressing"] = by_addressing

    # per-category breakdown
    by_category: dict[str, dict] = {}
    for cat in sorted({r["category"] for r in results}):
        rows = [r for r in results if r["category"] == cat]
        cat_must = [r for r in rows if _action(r) == "speak" and _strength(r) == "must"]
        cat_silent = [r for r in rows if _action(r) == "silent"]
        cat_counts = Counter(r["outcome"] for r in rows)
        by_category[cat] = {
            "n": len(rows),
            "outcomes": {k: cat_counts.get(k, 0) for k in OUTCOMES if cat_counts.get(k, 0)},
            "speak_must": ratio(count(cat_must, ADDRESSED_OK), len(cat_must)),
            "silent_engaged": ratio(count(cat_silent, ENGAGED_ANY), len(cat_silent)),
        }
    summary["by_category"] = by_category
    return summary


# ── cleaning replay ──────────────────────────────────────────────────────

def classify_clean_row(
    *,
    expect_silent: bool,
    expect_text: str | None,
    out: str,
    full: str,
    max_chars: int,
) -> dict:
    """Classify one cleaner output against its expectation.

    ``out`` is the cleaner result with ``max_chars``; ``full`` is the same
    cleaner run with an unreachable limit (what would have been sent without
    the hard cut).

    status:
      ``ok``          silent row stayed blank / legit row equals ``expect_text``
      ``leak``        silent row produced visible text (meta leak)
      ``false_kill``  legit row was blanked
      ``truncated``   legit row differs from ``expect_text`` and was hard-cut
      ``mangled``     legit row differs from ``expect_text`` for another reason
    """
    truncated = len(full) > max_chars
    # Since Phase 1 the cleaner cuts at clause breaks, so "mid-sentence" means
    # the cut landed INSIDE a clause: the output does not end at a sentence end
    # or a clause break, and the dropped text does not start with one either.
    boundary = SENTENCE_END + CLAUSE_BREAK
    following = full[len(out):len(out) + 1] if full.startswith(out) else ""
    mid_sentence = bool(
        truncated and out
        and not out.endswith(boundary)
        and not (following and following in boundary)
    )
    if expect_silent:
        status = "ok" if out == "" else "leak"
    elif out == expect_text:
        status = "ok"
    elif out == "":
        status = "false_kill"
    elif truncated and out != full:
        status = "truncated"
    else:
        status = "mangled"
    return {
        "status": status,
        "truncated": truncated,
        "mid_sentence_truncation": mid_sentence,
    }


def clean_summary(rows: Sequence[Mapping[str, Any]]) -> dict:
    """Aggregate classified cleaning rows.

    Each row needs ``kind``, ``expect_silent``, ``status``, ``truncated`` and
    ``mid_sentence_truncation`` (as produced by :func:`classify_clean_row`).
    """
    rows = list(rows)
    silent_rows = [r for r in rows if r["expect_silent"]]
    legit_rows = [r for r in rows if not r["expect_silent"]]

    def n_status(subset: Iterable[Mapping[str, Any]], *statuses: str) -> int:
        return sum(1 for r in subset if r["status"] in statuses)

    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        slot = by_kind.setdefault(r["kind"], {"n": 0})
        slot["n"] += 1
        slot[r["status"]] = slot.get(r["status"], 0) + 1

    return {
        "n_rows": len(rows),
        "n_should_be_blank": len(silent_rows),
        "n_legit": len(legit_rows),
        "leak_rate": ratio(n_status(silent_rows, "leak"), len(silent_rows)),
        "false_kill_rate": ratio(n_status(legit_rows, "false_kill"), len(legit_rows)),
        "mangled_rate": ratio(
            n_status(legit_rows, "mangled", "truncated"), len(legit_rows),
        ),
        "exact_match_rate": ratio(n_status(rows, "ok"), len(rows)),
        "truncated_rows": sum(1 for r in rows if r["truncated"]),
        "mid_sentence_truncations": sum(
            1 for r in rows if r["mid_sentence_truncation"]
        ),
        "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
        # Blank output that is *not* recognised as intentional silence: for a
        # direct @ the router answers with a canned "didn't catch that".
        "blank_without_silence_flag": {
            "n": sum(1 for r in rows if r.get("out") == "" and not r.get("silence_flag")),
            "ids": [
                r["id"] for r in rows
                if r.get("out") == "" and not r.get("silence_flag")
            ],
        },
        "leak_ids": [r["id"] for r in silent_rows if r["status"] == "leak"],
        "false_kill_ids": [r["id"] for r in legit_rows if r["status"] == "false_kill"],
        "mangled_ids": [
            r["id"] for r in legit_rows if r["status"] in ("mangled", "truncated")
        ],
    }


__all__ = [
    "OUTCOMES", "ADDRESSED_OK", "PROACTIVE", "ENGAGED_ANY", "LOCAL_CATEGORIES",
    "SENTENCE_END", "ratio", "fmt_ratio", "distribution", "routing_summary",
    "classify_clean_row", "clean_summary",
]


# ── structured pipeline ──────────────────────────────────────────────────

def _gate_score(result: Mapping[str, Any]) -> float | None:
    gate = result.get("gate")
    if gate and gate.get("score") is not None:
        return float(gate["score"])
    value = result.get("gate_score")
    return None if value is None else float(value)


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float | None:
    """Chance that a random positive outscores a random negative (ties count half)."""
    if not positives or not negatives:
        return None
    wins = sum(
        1.0 if p > n else 0.5 if p == n else 0.0
        for p in positives for n in negatives
    )
    return round(wins / (len(positives) * len(negatives)), 4)


def pipeline_summary(results: Sequence[Mapping[str, Any]]) -> dict | None:
    """Metrics that exist only when the router ran the structured pipeline.

    A result carries ``route_level`` when it did: the addressing level at
    which the model was asked (``explicit`` / ``strong`` / ``weak`` /
    ``ambient``), ``local`` when a command handler answered, ``none`` when the
    router did nothing.  The replay's stub model always answers, so these
    numbers say *how much freedom the model gets*, not what it will do with it:

    * ``levels``               model-asking cases per level.
    * ``must_speak``           where speak/must cases ended: ``guaranteed``
      (explicit level: the bot answers unless the model says the message was
      for someone else), ``model_decides`` (strong / weak / ambient),
      ``local`` (a command), ``none`` (no route).
    * ``silent_guaranteed``    of silent-expected cases, those at the explicit
      level: a hard false positive, the bot will answer.
    * ``silent_model_asked``   of silent-expected cases, those the model is
      asked about and must decline (cost and risk, to be measured live).
    * ``addressee``            expected addressee x level reached.
    * ``gate_scores``          how well the willingness score separates cases
      that should be spoken to from those that should not (AUC, ambient).
    """
    results = list(results)
    if not any("route_level" in r for r in results):
        return None

    def level(r: Mapping[str, Any]) -> str:
        return r.get("route_level", "none")

    asked = [r for r in results if level(r) in LEVELS]
    speak_must = [
        r for r in results if _action(r) == "speak" and _strength(r) == "must"
    ]
    silent = [r for r in results if _action(r) == "silent"]

    def bucket(r: Mapping[str, Any]) -> str:
        lv = level(r)
        if lv == "explicit":
            return "guaranteed"
        if lv in MODEL_DECIDES:
            return "model_decides"
        if r["outcome"] == "engaged_vision":
            return "vision"
        return "local" if lv == "local" else "none"

    must_counts = Counter(bucket(r) for r in speak_must)
    confusion: dict[str, dict[str, int]] = {}
    for r in results:
        expect = r.get("expect")
        if not expect or not expect.get("addressee"):
            continue
        slot = confusion.setdefault(expect["addressee"], {})
        slot[level(r)] = slot.get(level(r), 0) + 1

    speak_scores = [s for r in results if _action(r) == "speak"
                    and r["category"].startswith("ambient") for s in [_gate_score(r)] if s is not None]
    silent_scores = [s for r in results if _action(r) == "silent"
                     and r["category"].startswith(("ambient", "between", "short")) for s in [_gate_score(r)]
                     if s is not None]

    return {
        "strategy": next((r["strategy"] for r in results if r.get("strategy")), None),
        "levels": {lv: sum(1 for r in asked if level(r) == lv) for lv in LEVELS},
        "must_speak": {
            "n": len(speak_must),
            **{k: must_counts.get(k, 0)
               for k in ("guaranteed", "model_decides", "vision", "local", "none")},
        },
        "silent_guaranteed": ratio(
            sum(1 for r in silent if level(r) == "explicit"), len(silent),
        ),
        "silent_model_asked": ratio(
            sum(1 for r in silent if level(r) in MODEL_DECIDES), len(silent),
        ),
        "silent_guaranteed_ids": [r["id"] for r in silent if level(r) == "explicit"],
        "addressee": {k: confusion[k] for k in sorted(confusion)},
        "gate_scores": {
            "speak_n": len(speak_scores),
            "silent_n": len(silent_scores),
            "speak_mean": round(sum(speak_scores) / len(speak_scores), 4) if speak_scores else None,
            "silent_mean": round(sum(silent_scores) / len(silent_scores), 4) if silent_scores else None,
            # An AUC over a handful of cases is noise: report it from 5 each.
            "auc": (
                auc(speak_scores, silent_scores)
                if min(len(speak_scores), len(silent_scores)) >= MIN_AUC_CASES else None
            ),
        },
    }


# ── envelope parsing ─────────────────────────────────────────────────────

def classify_envelope_row(expect: Mapping[str, Any], got: Mapping[str, Any]) -> str:
    """Compare what a malformed-or-not model answer should yield with what it did.

    ``expect`` / ``got`` are ``{"action": "reply", "text": ...}``,
    ``{"action": "silent"}`` or ``{"action": "fail", "code": ...}`` ("fail" =
    not a usable envelope; nothing is sent, the engine would repair or stay
    silent).  Verdicts: ``ok``, ``leak`` (text produced where none was due),
    ``false_kill`` (a reply was due and nothing was sent), ``mangled`` (wrong
    text), ``wrong_failure`` (rejected, but for another reason than expected).
    """
    want, have = expect["action"], got["action"]
    if want == "reply":
        if have != "reply":
            return "false_kill"
        return "ok" if got.get("text") == expect.get("text") else "mangled"
    if have == "reply":
        return "leak"
    if want == "silent":
        return "ok" if have == "silent" else "wrong_failure"
    if have != "fail":
        return "wrong_failure"
    code = expect.get("code")
    return "ok" if not code or code == got.get("code") else "wrong_failure"


def envelope_summary(results: Sequence[Mapping[str, Any]]) -> dict:
    """Aggregate :func:`classify_envelope_row` verdicts.

    ``leak_rate`` is over rows that must not produce text (silent or failure),
    ``false_kill_rate`` over rows that must produce a reply.
    """
    results = list(results)
    no_text = [r for r in results if r["expect"]["action"] != "reply"]
    due = [r for r in results if r["expect"]["action"] == "reply"]
    verdicts = Counter(r["verdict"] for r in results)
    by_kind: dict[str, dict[str, int]] = {}
    for r in results:
        slot = by_kind.setdefault(r["kind"], {"n": 0, "ok": 0})
        slot["n"] += 1
        slot["ok"] += r["verdict"] == "ok"
    return {
        "n_rows": len(results),
        "exact": ratio(verdicts.get("ok", 0), len(results)),
        "leak_rate": ratio(verdicts.get("leak", 0), len(no_text)),
        "false_kill_rate": ratio(verdicts.get("false_kill", 0), len(due)),
        "mangled": verdicts.get("mangled", 0),
        "wrong_failure": verdicts.get("wrong_failure", 0),
        "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
        "mismatches": [r["id"] for r in results if r["verdict"] != "ok"],
        # Rows that document a limit of the text rules on purpose.
        "known_gaps": [r["id"] for r in results if r.get("known_gap")],
    }
