# memory evaluation - phase3_after

5 invented chats, 46 recall questions.

## Recall questions

### old LIKE search

| kind | n | hit@1 | hit@3 | hit@8 |
|---|---|---|---|---|
| exact | 14 | 0.714 | 0.714 | 0.714 |
| paraphrase | 14 | 0.357 | 0.357 | 0.357 |
| two_char | 6 | 1.0 | 1.0 | 1.0 |
| distractor | 3 | 0.0 | 0.0 | 0.0 |
| old | 3 | 1.0 | 1.0 | 1.0 |
| no_answer (false alarm) | 6 | 0.0 | | |
| ALL_ANSWERABLE | 40 | 0.6 | 0.6 | 0.6 |

### new, no index (LIKE fallback)

| kind | n | hit@1 | hit@3 | hit@8 |
|---|---|---|---|---|
| exact | 14 | 0.929 | 1.0 | 1.0 |
| paraphrase | 14 | 0.714 | 0.714 | 0.714 |
| two_char | 6 | 1.0 | 1.0 | 1.0 |
| distractor | 3 | 0.667 | 0.667 | 0.667 |
| old | 3 | 1.0 | 1.0 | 1.0 |
| no_answer (false alarm) | 6 | 0.167 | | |
| ALL_ANSWERABLE | 40 | 0.85 | 0.875 | 0.875 |

### new, FTS5 + LIKE + recency

| kind | n | hit@1 | hit@3 | hit@8 |
|---|---|---|---|---|
| exact | 14 | 1.0 | 1.0 | 1.0 |
| paraphrase | 14 | 0.857 | 0.857 | 0.857 |
| two_char | 6 | 1.0 | 1.0 | 1.0 |
| distractor | 3 | 0.667 | 0.667 | 0.667 |
| old | 3 | 1.0 | 1.0 | 1.0 |
| no_answer (false alarm) | 6 | 0.167 | | |
| ALL_ANSWERABLE | 40 | 0.925 | 0.925 | 0.925 |

## Fact selection

| | recall | injected / message | precision |
|---|---|---|---|
| old (every alias line) | 1.0 | 2.0 | 0.125 |
| new (relevance-ranked) | 1.0 | 2.25 | 0.444 |

_old extractor stores aliases only; recall counted over alias facts_

## Latency

100000 messages (load 0.6 s, index build 0.3 s), 60 queries.

| | p50 ms | p95 ms |
|---|---|---|
| old | 51.42 | 376.39 |
| new | 5.73 | 111.33 |

