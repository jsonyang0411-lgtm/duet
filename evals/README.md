# evals - offline evaluation harness (Phase 0 safety net, used by every phase)

A deterministic, network-free safety net for refactoring the group-chat bot.
It replays hand-written scenarios through the **current** router, proactive
gate and reply cleaner with stubs, and prints / stores numbers that later
phases must not make worse (or must improve, where the baseline shows a gap).

Nothing here calls an LLM, a search engine or WeChat. Production code under
`src/` is imported and treated as read-only.

```
python -m evals.run_offline --no-write      # print the numbers, write nothing
python -m evals.run_offline --tag phase3_after   # record a baseline: evals/baseline/phase3_after.{json,md}
python -m evals.run_offline --out /tmp/out --seed 7
python -m evals.run_offline --pipeline single --gate score --no-write   # the structured pipeline
pytest tests/test_evals_harness.py -q
```

`--pipeline off|single|two_step` and `--gate dice|score` select the code path
under test (the *profile*). The default profile is the production default;
reports for it are byte-identical to the committed `phase1_after` baseline,
which is how a refactor proves it left the legacy path alone. A non-default
profile adds a `profile` block, the pipeline metrics and the envelope replay.

Without `--tag` the output goes to `evals/baseline/current.{json,md}`, which is
git-ignored, so a casual run can never overwrite a committed baseline.

One pass takes well under a second; the CLI runs everything twice for the
determinism check (about 1-2 s in total). The budget is 15 s.

## Contents

| path | what |
|---|---|
| `evals/__init__.py` | `replay-case/v1` schema constants, `validate_case`, `normalize_case`, `load_jsonl` (pure Python, no `src` imports) |
| `evals/metrics.py` | pure metric functions (unit-testable on hand-made input) |
| `evals/run_offline.py` | replay engine, report builder, markdown renderer, CLI |
| `evals/cases/scenarios_v1.jsonl` | 149 hand-written synthetic scenarios, 22 categories (Phase 1 added the 4 `busy_group` cases; Phase 2 added 24 ambient ones, see below) |
| `evals/cases/raw_outputs_v1.jsonl` | 64 raw model outputs for the cleaning replay (`names`: the persona / bot / requester names the production call passes to the cleaner) |
| `evals/cases/envelopes_v1.jsonl` | 113 model answers for the envelope replay of the structured pipeline: well-formed, sloppy, schema drift, the 19 legacy "should be silent" strings inside a reply and written instead of JSON, truncated / broken JSON, and 3 `known_gap` rows that document limits of the text rules |
| `evals/run_live.py` | opt-in comparison against a real model (see "Live evaluation") |
| `evals/run_live_embeddings.py` | opt-in check of meaning-based fact search (MEMORY_EMBEDDINGS) with a real embeddings model, by default the local Ollama: recall, precision and noise per MEMORY_EMBEDDING_MIN_SIM on `evals/cases/memory_semantic_v1.jsonl` (16 invented facts, 21 messages) |
| `evals/persona_cards.py`, `evals/cases/personas_eval_v1.jsonl` | persona cards (a persona as data: name, prompt, rules, character limit); six hand-written evaluation personas that training data must never use. `run_live` and `judge_replies` take `--persona-cards FILE --persona-id ID`; the judge refuses runs made with another persona |
| `evals/judge_replies.py` | blind, position-balanced comparison of two live runs' replies by a judge model (see "Blind reply comparison") |
| `evals/cases/quality_v1.jsonl` | 60 invented chats where the bot must answer, for judging reply quality (not in the offline baselines) |
| `evals/baseline/phase0_baseline.json` / `.md` | the very first baseline: Phase 0 code, 121 scenarios, original harness. **Historical, never regenerate** |
| `evals/baseline/phase0_code.json` / `.md` | Phase 0 code (commit d3cfd44) replayed with today's harness and scenario set |
| `evals/baseline/phase1_after.json` / `.md` | Phase 1 code, same harness and scenarios. **Diff this against `phase0_code` to see exactly what Phase 1 changed** |
| `evals/baseline/phase2_after.json` / `.md` | Phase 2 code with `--pipeline single --gate score`, same scenarios. Diff against `phase1_after` for the effect of the structured pipeline and the scored gate |
| `evals/run_memory.py` | offline evaluation of the memory rework (Phase 3), see "Memory evaluation" |
| `evals/run_live_memory.py` | opt-in run of the memory extraction job against a real model |
| `evals/cases/memory_v1.jsonl` | 5 invented chats (60-80 messages each, one with an imported archive), 46 recall questions in 6 kinds, expected facts, and a fact-selection set |
| `evals/baseline/phase3_after.json` / `.md` | memory evaluation on the Phase 3 code, including the 100 000-message latency bench (timings vary by machine; the rest is deterministic) |
| `tests/test_evals_harness.py` | schema, determinism, metrics and hygiene tests |
| `tests/test_evals_memory.py`, `tests/test_evals_live_memory.py` | fixtures, determinism and the claims the memory baseline makes |

The scenarios are **invented** casual group chat: no real people, no real
chat data. Real exports can be replayed with `--cases` (see below).

## What is replayed

### 1. Routing replay

For every case a fresh `MessageRouter` is built with real `TriggerDetector`,
`AdminCommandHandler`, `TodoStore` (temp file), `MessageStore`
(`initialize_db(":memory:")`), real sticky / follow-up / image-follow-up
trackers and the real `ProactiveGate`. Only the model, nickname lookup, web
search and memory consolidation are stubbed.

Steps:

1. `context` messages are fed through `router.handle` in order. A **fake clock**
   (the module-level `time` name of the production modules that use it is
   swapped; global `time.time` is untouched) is set to each message's own
   timestamp, `now + offset_sec`.
2. `bot_state` is primed the way production registers it: for every id in
   `sticky_for` and finally `last_bot_reply.to_sender_id`, a synthetic
   `@bot 在吗` message is handled whose stub reply is `last_bot_reply.text`, at
   clock `now + last_bot_reply.offset_sec`. The real trackers therefore
   register naturally and expire naturally (sticky 60 s, follow-up 60 s,
   image follow-up 90 s). Priming events are merged into the timeline
   chronologically.
3. The trigger is handled at clock `now`; its own timestamp is
   `now + trigger.offset_sec` (negative offsets exercise the 300 s freshness
   guard).
4. The stub summarizer records which entry point the router called (`chat`,
   `proactive_chat`, `describe_image`) and answers `好` (a non-empty reply, so
   the router's visible-reply bookkeeping runs like in production). It also
   records the tail of the context it was handed, so the replay can tell
   whether the message being answered is actually *in* the model's context
   (`context_has_trigger`, see the metrics below).

Details worth knowing:

* `is_at_mentioned` for `context` rows is derived like the WCDB backend does
  (`f"@{bot_name}" in content`); for the trigger the case's value is used.
* The ambient (proactive) path is evaluated **only for the trigger** and with
  the probability roll forced open, so the outcome `engaged_proactive` means
  "all deterministic gates passed; the model would be called with probability
  `reply_probability` of the reported mode". Rate = messages in the case's
  context within the 120 s window, so it depends on the context density.
* Media messages (`msg_type` 3/47) get a path to a tiny PNG so that vision
  routes are reachable.
* The replay runs inside a temp working directory with logging disabled and the
  observability sinks (if present) pointed at temp files: nothing is written
  under the repo's `data/`.
* `config_overrides` are applied to `BotConfig`; unknown keys are an error.

Outcome per case (what the router did with the trigger):

| outcome | meaning |
|---|---|
| `engaged_chat` | chat model called: @, quote of bot, keyword, sticky, follow-up, private chat, full-feed persona |
| `engaged_vision` | vision model called: image question or image follow-up |
| `engaged_proactive` | ambient path open: the model would be called with probability p (see above) |
| `handled_locally` | answered with no model: todo / lots / tarot / help / canned fallbacks |
| `none` | nothing happened (also: a local handler matched but replied with an empty string, flagged `local_empty`) |
| `error` | the replay itself raised (should never happen) |

### 2. Cleaning replay

Each row of `raw_outputs_v1.jsonl` goes through
`AbstractSummarizer._clean_chat_reply(raw, max_chars, names=row["names"])`
(`max_chars` default 30) and is compared with `expect_text` / `expect_silent`.
Expectations describe what a good cleaner should send, not what the current one
does. A checkout whose cleaner has no `names` parameter (Phase 0) is called
without it, so the same harness can replay old commits for a before/after.

### 3. Gate streams

Seeded Poisson message streams at 0.5, 1, 2, 4, 6, 8, 10 and 12 msgs/min for
120 simulated minutes go through the real `ProactiveGate` (enabled, default
thresholds, fake clock, one group). Every pass is then fed to a hypothetical
model that is either *always silent* (router calls `record_eval` +
`record_silence`, i.e. the gate's exponential backoff applies; passes are
wasted model calls) or *always speaks* (`record_speech`, no backoff; passes are
bot messages).

### 4. Structured pipeline (non-default profile)

With `--pipeline single|two_step` the router asks `decide_and_reply` instead of
`chat` / `proactive_chat`. The stub answers like `chat` does (`好`), and
records the **addressing level** the router computed (`explicit` / `strong` /
`weak` / `ambient`) and its signals. Because the stub never declines, the
numbers say how much freedom the model is given, not what it does with it:

* *reply guaranteed* = explicit level: the bot answers unless the model says the
  message was for someone else;
* *model decides* = strong / weak / ambient: the model may stay silent.

With `--gate score` the proactive gate uses the willingness score instead of
the dice, and the gate streams get a synthetic message mix (invented shares,
see `_STREAM_MIX`) so the score gate can be compared with the dice gate on the
same messages: calls per hour, the share of calls spent on messages worth
speaking about, and how many of those messages were reached.

The **envelope replay** feeds `evals/cases/envelopes_v1.jsonl` through
`parse_envelope`. The rows were written by hand next to the rules they test, so
it is a robustness check (a coded-up model that ignores the format or puts
silence markers into a reply must never get text out), not a measure of a real
model. `known_gap` rows record limits on purpose; the report lists them as
mismatches.

### Ambient scenarios added in Phase 2

`scn-0126`..`scn-0137` (12 how-to / recommendation questions to the whole group that
nobody answered: should speak) and `scn-0138`..`scn-0149` (12 invitations,
acknowledgements, apologies, laughter, birthday wishes, a complaint about the bot:
should stay silent) were written before the score gate was run on them. First look:
11 of 12 speak-worthy reached the model, 1 of 12 silent ones did. Two adjustments were
made after seeing that (a recommendation request counts as a question; talk *about*
bots no longer raises the score), after which the batch is no longer held out: the
reported 12/12 and 0/12 are fitted numbers. A fresh batch from someone else (or real
labeled exports, see `tools/label_replay_cases.py`) is the honest test.

## Live evaluation (`python -m evals.run_live`)

The offline harness cannot say what a real model does. `run_live` replays the same
scenarios with the summarizer configured in your `.env` behind the trigger message,
for the strategies `off` / `single` / `two_step`, and reports balanced accuracy
(speaks when it should, stays silent when it should), leaks, unusable-answer and
repair rates, calls per case, tokens, latency and an optional cost estimate, plus the
cases where two strategies disagree.

```
python -m evals.run_live                       # plan only: how many cases reach a model
python -m evals.run_live --yes                 # run it (costs API credit)
python -m evals.run_live --yes --strategies off single --limit 40 --price-in 0.27 --price-out 1.1
```

Nothing is sent to WeChat. The proactive gate is forced open for every strategy so the
model's judgement is what is measured. A case where a model call fails (API error) is
left out of the scores and listed under "errored"; three failures in a row abort the run
(exit 3), and so does an error share above 20%: the router would otherwise turn an
outage into apology replies that score as "spoke". Reports go to `data/replay/live/`
(git-ignored); they contain the scenario text.

## Blind reply comparison (`python -m evals.judge_replies`)

`run_live` says whether a model spoke when it should; it cannot say whether the
reply was any good. `judge_replies` takes two `run_live` reports (say the local
model and the cloud model) and, for every scenario where both spoke, asks a judge
model (the cloud model in `.env`, or `--judge-model` on the same endpoint) which
reply a group member with the persona would rather have sent, and which flaws each
has (`ignored_request`, `off_topic`, `out_of_persona`, `too_long`, `repetitive`,
`made_up`). The replies are shown as 回复1 / 回复2, never by origin, and every pair
is judged twice with the sides swapped, so a judge that prefers a position cancels
out. A's score: win 1, tie 0.5, loss 0 (50% = as good as B); "consistency" is the
share of pairs whose two judgments agree.

```
python -m evals.run_live --yes --strategies single --cases evals/cases/quality_v1.jsonl --tag local_q
python -m evals.judge_replies --a data/replay/live/local_q.json --b data/replay/live/cloud_q.json \
    --cases evals/cases/quality_v1.jsonl            # plan only
python -m evals.judge_replies ... --yes             # call the judge (2 calls per pair)
python -m evals.judge_replies ... --yes --batch 10  # 10 judgments per call
```

`--batch N` puts N judgments into one call, for a relay that adds a fixed overhead
to every request (one adds about 4400 prompt tokens). The first pass shows every pair
in one order, the second in the other, so the two judgments of a pair never share a
call; items the judge skipped are asked again once. A failed call (a timeout, "429")
is retried after 5 and 30 seconds; when the run stops anyway, what was judged so far
is still written (exit code 3).

`evals/cases/quality_v1.jsonl` holds 60 invented chats where the bot is addressed
and must answer (a joke, a choice, an explanation, advice, an opinion, questions
about itself, follow-ups on its own earlier answer, comfort, banter, a
recommendation, sums, a plan, good news, a simple fact, an unclear request): what they
measure is the answer, not the decision. They are not part of the offline
baselines. When the judge is also B's model it may favour B; A's score is then,
if anything, too low. Reports go to `data/replay/judge/` (git-ignored).

## Case schema (`replay-case/v1`)

One JSON object per line, UTF-8, `ensure_ascii=False`.

```json
{"schema":"replay-case/v1","id":"scn-0001","category":"direct_at",
 "chat":{"chat_id":"g_demo@chatroom","group_name":"示例群","is_group":true},
 "bot_name":"小助手","now":1760000000,
 "context":[{"offset_sec":-240,"sender_id":"u_a","sender_name":"阿明","content":"...","msg_type":1}],
 "trigger":{"sender_id":"u_b","sender_name":"小红","content":"@小助手 ...","msg_type":1,
            "is_at_mentioned":true,"is_reply_to_bot":false,
            "quoted_content":"","quoted_sender_name":"","offset_sec":0},
 "next_messages":[],
 "bot_state":{"last_bot_reply":{"to_sender_id":"u_b","text":"...","offset_sec":-25},"sticky_for":["u_b"]},
 "expect":{"action":"speak","strength":"must","addressee":"bot","note":"why"},
 "config_overrides":{},"source":"handwritten"}
```

| field | notes |
|---|---|
| `id`, `category`, `source` | free strings; `category` matches `[a-z][a-z0-9_]*`; `bad` cases are skipped by the harness |
| `chat` | `is_group=false` replays through the private-chat path |
| `bot_name` | becomes `BotConfig.bot_display_name`; the bot's own messages in `context` (sender name == bot name) are dropped by the router like in production |
| `now` | unix seconds the bot "wakes up" to handle the trigger |
| `context[*].offset_sec` | seconds relative to `now` (negative = past); `msg_type` 1 text, 3 image, 47 sticker, 34 voice, 43 video |
| `trigger.is_at_mentioned` | as the backend computes it: `is_reply_to_bot or f"@{bot_name}" in content`; the bot name **without** `@` is *not* a mention |
| `trigger.offset_sec` | usually 0; older than -300 exercises the stale guard |
| `next_messages` | what happened after the trigger (not used by the replay; context for labeling) |
| `bot_state` | `null`, or `last_bot_reply` (who the bot answered, when) and `sticky_for` (senders in sticky mode) |
| `expect.action` | `speak` / `silent` / `either` - what a **good** bot should do (intended product behaviour, not what the current code does) |
| `expect.strength` | `must` = clear failure otherwise, `should` = better that way, `free` (with `either`) |
| `expect.addressee` | `bot` / `other_member` / `group` / `nobody` |
| `config_overrides` | `BotConfig` field overrides, e.g. `{"chat_persona_profile":"yang_yujing","yang_yujing_group_name":"示例群"}` for the full-feed persona |

`evals.validate_case(case)` returns a list of problems. Exports from
`tools/export_replay_cases.py` are looser (null `is_reply_to_bot`, no
`config_overrides`, no `msg_type` in `next_messages`, `expect: null` until
labeled, nullable `addressee`); the loader runs `evals.normalize_case` and
`validate_case(..., allow_unlabeled=True)` on every row, so exports and labeled
exports replay directly:

```
python -m evals.run_offline --cases data/replay/labeled.jsonl --raw-outputs "" \
    --out data/replay/report --tag real_baseline
```

Unlabeled cases only contribute to the outcome counts and to an "unlabeled
engagement rate". The generated report embeds a message snippet per case only
when `source == "handwritten"`; reports built from real exports carry no chat
text, but they still contain ids and categories - keep them out of git.

## Metrics

All rates are `k/n`; a rate over an empty set is reported as `n/a`.

Routing (over cases with `expect`):

| metric | definition |
|---|---|
| addressing recall (speak/must) | of cases with `expect.action == speak` and `strength == must`: share whose outcome is `engaged_chat`, `engaged_vision` or `handled_locally` |
| addressing recall (speak any) | same over `must` + `should` |
| false engagement, always-on | of `expect.action == silent`: share whose outcome is `engaged_chat`, `engaged_vision` or `handled_locally` |
| false engagement, incl. proactive | adds `engaged_proactive` (upper bound: that path fires with probability <= 0.7) |
| cannot engage | `speak` cases whose outcome is `none` and not `local_empty`: the router has no route at all (implicit addressing). Reported for must and must+should |
| chance only | `speak` cases whose only route is the proactive path |
| unexpected local | `handled_locally` outside the command categories `todo_command`, `lots_command`, `help_command` (possible command hijacks) |
| pipeline: must-speak ends | of speak/must cases: `guaranteed` (explicit level), `model_decides`, `vision`, `local` (a command), `none` |
| pipeline: reply guaranteed on silent | silent-expected cases at the explicit level: hard false positives |
| pipeline: model asked on silent | silent-expected cases the model must decline (strong / weak / proactive): cost and risk, measured live |
| pipeline: score AUC | chance a speak-worthy ambient case outscores a silent one (reported from 5 cases per side) |
| local empty reply | a local handler matched but sent nothing |
| model context ends with the trigger | over cases that call the model: share whose context handed to the model includes the message being answered. The `busy_group` cases (60 messages in the last 10 minutes) catch a store that returns the *oldest* N messages instead of the newest |
| by addressing | recall / false engagement split by `explicit` (@ or quote of bot), `implicit`, `private` |
| by route / by category | where engagements come from; per-category counts |

Cleaning (over `raw_outputs_v1.jsonl`):

| metric | definition |
|---|---|
| leak rate | of rows that should end blank (`expect_silent`): share that still produced text |
| false-kill rate | of legitimate rows: share cleaned to blank |
| mangled rate | legitimate rows whose output differs from `expect_text` (not blank) |
| truncated rows / cuts inside a clause | rows that exceeded `max_chars` and were shortened; of those, cuts that land inside a clause (the result does not end at a sentence end or a clause break such as a comma) |
| exact-match rate | rows whose output equals the expectation (blank for silent rows) |
| blank without silence flag | rows cleaned to blank that `chat()` would *not* treat as intentional silence (the router then sends its canned fallback to a direct @) |

Gate (per stream rate): messages, share of messages evaluated per mode, passes
per hour and mode of every pass for the two hypothetical models;
`wasted_model_calls` for the always-silent model equals its passes. In score
mode each row also has `dice_reference` (the dice gate on the same messages)
and `worthy_share_of_passes` / `worthy_recall`.

Envelopes: `leak_rate` (text produced where none is due, over rows that must
not produce text), `false_kill_rate` (a reply was due and nothing was sent),
`mangled`, `wrong_failure` (rejected for another reason than expected), per
kind counts.

Determinism: the CLI builds the whole report twice and records
`determinism.identical`; the exit code is non-zero if the runs differ or a
replay raised.

## Reading and using the baseline

* Baselines are named after what they measure: `phaseN_after` is the code at
  the end of phase N. To compare two commits, run the *same* harness on both:
  `git worktree add --detach <dir> <old-commit>`, copy the current `evals/` over
  `<dir>/evals`, run with `--tag <name> --out <elsewhere>`, then diff the two
  JSON files (this is how `phase0_code` was produced).
* `*.md` is the human view; `*.json` has per-case records (`outcome`, `route`,
  `verdict`, `context_has_trigger`, gate mode/probability) so a refactor can be
  compared case by case.
* A regression is a change in a case's `outcome`/`route` that is not an
  intended improvement, or a drop in recall / rise in false engagement.
* Many of today's gaps are visible as `expect` vs `outcome` mismatches on
  purpose (implicit addressing, command hijacks, cleaner leaks): the baseline
  documents them, it does not hide them.

## What is NOT measured offline

* Whether the real LLM stays silent when it should. `engaged_*` only means the
  router *asked* the model; a live run with an API key and a real chat is
  needed to measure how often the model then returns blank.
* Reply quality, persona fit, factual accuracy, latency, cost.
* The backend layer: WeChat dedup, message-burst coalescing, real mention /
  quote detection (taken from the case as given).
* Vision, web search results and memory consolidation content.

No live mode is implemented on purpose.

## Known limits

* 149 hand-written scenarios are a regression net, not a sample of production
  traffic; percentages are not accuracy estimates.
* The score gate is tuned by hand and checked on the same invented material it
  is reported on; the live runner and a week of real traces are the real test.
* The proactive rate sees only the messages in a case's `context`, so ambient
  cases are sensitive to how dense the snippet is. Gate streams are synthetic
  Poisson arrivals (real groups are burstier).
* Expectations are one author's judgement; ambiguous situations use
  `either`/`free`. Strength `must`/`should` can be argued - edit the case, not
  the harness.
* Context messages answered by the stub leave the same sticky / follow-up
  state a real reply would. A real model might have stayed silent there, so
  counterfactual state can differ from what really happened.
* `reply_head` / route labels for local handlers are derived from the reply
  text and are diagnostic only.

## Memory evaluation

```
python -m evals.run_memory --no-write --rows      # numbers + the rank of the right message for every question
python -m evals.run_memory --bench --tag phase3_after   # also time 100 000 messages
python -m evals.run_live_memory                   # plan only: windows and a token estimate
python -m evals.run_live_memory --yes --price-in 0.27 --price-out 1.10   # real model, costs credit
```

No network and no model for `run_memory`. For every invented chat it asks the old
search (`MessageStore.search_chat_history` with the terms the router extracts) and
the new one (FTS5 trigram + LIKE + recency, `src.memory.search.search_messages`,
with and without the index) for the message that answers each question.

| kind | what it tests |
|---|---|
| `exact` | the question shares a phrase of three or more characters with the answer |
| `paraphrase` | different wording, shared content words (two-character words are the usual link) |
| `two_char` | a two-character topic ("加班"), which a trigram index cannot see |
| `distractor` | a second message about a similar topic; the right one must win |
| `old` | the answer is only in the imported archive, a year older |
| `no_answer` | nothing in the chat answers it; reported as the share that still returned something (false alarm) |

`hit@k` is "an expected message is among the first k results". The recall gate
(the router searches only when the message looks like a question about the past)
is the same in both versions and not measured. Fact selection compares what would be
injected for a message: the old code puts every alias line it has in every prompt.

Honest limits, also pinned by `tests/test_evals_memory.py`:

* The three questions the new search still misses (`food.q3`, `food.q8`, `archive.q5`)
  need meaning, not shared words ("不能吃虾" vs "对海鲜过敏"). That is what embeddings
  would add; the first version has none on purpose.
* Questions with no answer still get a hit for a generic two-character word
  ("想吃") about one time in six; the old search was silent there. The prompt tells
  the model to use the lines only if they answer the question.
* The fixtures were written before the first run and the retrieval was not tuned
  afterwards. The thresholds (coverage 0.2, fact score 0.3) come from the unit
  tests that were written first, by the same author who wrote the chats, so this is
  still a small in-sample measurement: 46 questions on invented chats, not a
  forecast for real ones.
* Latency numbers in the baseline come from one machine and change from run to run.

`run_live_memory` feeds the same chats through the real `MemoryWorker` and the model
configured in `.env`. It reports how much of what the model proposed passed the
validator and why the rest did not, how many of the labelled facts it found, whether
every stored quote still matches its message, tokens and cost. Nothing is sent to WeChat.
