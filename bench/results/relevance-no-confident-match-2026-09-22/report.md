# Palinode relevance & abstention baseline

Measured against whatever the working tree does — the rig changes nothing itself, so a run is a baseline or a re-measurement depending only on when it was taken. Production search defaults changed by this run: `False`.

- Generated: 2026-09-22T15:48:25.148528+00:00
- Palinode: 0.21.0 · Python 3.12.13 · macOS-26.5-arm64-arm-64bit
- Embedding model: bge-m3 (1024 dimensions) · reachable: **no**
- Corpus v1 · question set v1
- 39 records → 39 files → 56 chunks, 0 embedded (56 keyword-only)
- 62 questions · top-k 5 · fts_threshold 0.40 · hybrid_weight 0.50 · snippet cap 400 chars
- Timing passes per question: 3

## Question set

| Class | base | paraphrase | identifier | total |
|---|---:|---:|---:|---:|
| release_state | 10 | 3 | 4 | 17 |
| rejected_approach | 10 | 3 | 0 | 13 |
| changed_decision | 10 | 4 | 4 | 18 |
| no_answer | 10 | 2 | 2 | 14 |

Paraphrases and identifier variants are the held-out split: a paraphrase shares few content words with the record that answers it, and an identifier query is a bare tag, config key or filed number.

## What ran, and what did not

- **lexical (keyword-only)** — ran.
- **lexical (keyword-only) + abstain** — ran.
- **hybrid @ cosine floor 0.40** — **NOT RUN**: no embedding endpoint reachable; the hybrid arm needs real query vectors and synthetic ones would not measure retrieval quality
- **hybrid @ cosine floor 0.40 + abstain** — **NOT RUN**: no embedding endpoint reachable; the hybrid arm needs real query vectors and synthetic ones would not measure retrieval quality
- **hybrid @ cosine floor 0.50** — **NOT RUN**: no embedding endpoint reachable; the hybrid arm needs real query vectors and synthetic ones would not measure retrieval quality
- **hybrid @ cosine floor 0.50 + abstain** — **NOT RUN**: no embedding endpoint reachable; the hybrid arm needs real query vectors and synthetic ones would not measure retrieval quality

## Headline

| Metric | lexical (keyword-only) | lexical (keyword-only) + abstain |
|---|---:|---:|
| Relevant-hit recall@k | 92.3% | 92.3% |
| Answerable questions with a relevant hit | 45/48 | 45/48 |
| Top-1 correct | 32/48 (66.7%) | 32/47 (68.1%) |
| Irrelevant injections (of results delivered) | 197/268 (73.5%) | 173/243 (71.2%) |
| Footer-only hits | 0/268 (0.0%) | 0/243 (0.0%) |
| Redundant results (near-duplicate records) | 9/268 (3.4%) | 9/243 (3.7%) |
| Repeat slots for one source file | 10/268 (3.7%) | 10/243 (4.1%) |
| Project-isolation violations | 25/211 (11.8%) | 25/206 (12.1%) |
| Correct abstention (no-answer questions) | 2/14 (14.3%) | 6/14 (42.9%) |
| Inappropriate confident match | 12/14 (85.7%) | 8/14 (57.1%) |
| Confidence band (median top score of a correct top-1) | 1.000 | 1.000 |
| Distinct top scores behind that band | 1 | 1 |
| Recency trap ranked first | 6/17 (35.3%) | 5/16 (31.2%) |
| Useful-context tokens (of delivered payload) | 6022/31159 (19.3%) | 6022/28510 (21.1%) |
| Payload tokens per question | 502.6 | 459.8 |
| p50 latency (ms) | 1.98 | 1.92 |
| p95 latency (ms) | 2.67 | 2.47 |
| Keyword-fallback rate (of calls) | 0/186 (0.0%) | 0/186 (0.0%) |
| Calls with no BM25 candidate | 0 | 0 |

Latency covers the whole client-visible path: the query embedding where there is one, the store search, and rendering the delivered payload. The lexical arm builds no query vector, so its keyword-fallback count is zero by construction rather than by measurement.

Read *inappropriate confident match* together with the row above it. The band is the median score a correct top-1 carries; when only one distinct score sits behind it, the delivered score is a function of rank rather than of relevance and the band cannot separate anything. In that case the number to act on is the abstention rate, not the confident-match rate.

No project filter, category filter or context was passed on any call: these are the arguments an unadorned `palinode_search` sends, which is what makes project isolation a property of ranking here rather than of a filter the caller remembered to set.

## Confidence verdict

The delivery-level verdict, read from the pre-fusion arm scores, crossed with whether the question had an answer in the corpus. The two cells to read are `confident` under *no-answer* (a confident wrong answer) and `none` under *answerable* (a refusal to answer something the store holds) — the second is qualified by how many of those slates contained a relevant record at all.

### lexical (keyword-only)

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 26 | 26 | 0 |
| weak | 21 | 19 | 8 |
| none | 1 | 0 | 6 |

Graded deliveries: 62.

### lexical (keyword-only) + abstain

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 26 | 26 | 0 |
| weak | 21 | 19 | 8 |
| none | 1 | 0 | 6 |

Graded deliveries: 62.

## Per class

### lexical (keyword-only)

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 94.1% | 12/17 | 51/72 | 0 | 3 | 2 | 10 | 0/0 | 4/8 |
| rejected_approach | 13 | 100.0% | 10/13 | 48/65 | 0 | 5 | 0 | 10 | 0/0 | 0/0 |
| changed_decision | 18 | 85.7% | 10/18 | 41/74 | 0 | 1 | 3 | 5 | 0/0 | 2/9 |
| no_answer | 14 | n/a | 0/0 | 57/57 | 0 | 0 | 5 | 0 | 2/14 | 0/0 |

### lexical (keyword-only) + abstain

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 94.1% | 12/17 | 51/72 | 0 | 3 | 2 | 10 | 0/0 | 4/8 |
| rejected_approach | 13 | 100.0% | 10/13 | 48/65 | 0 | 5 | 0 | 10 | 0/0 | 0/0 |
| changed_decision | 18 | 85.7% | 10/17 | 37/69 | 0 | 1 | 3 | 5 | 0/0 | 1/8 |
| no_answer | 14 | n/a | 0/0 | 37/37 | 0 | 0 | 5 | 0 | 6/14 | 0/0 |

## Base versus held-out

### lexical (keyword-only)

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 94.1% | 20/30 | 147/197 |
| identifier | 10 | 100.0% | 7/8 | 1/11 |
| paraphrase | 12 | 80.0% | 5/10 | 49/60 |

### lexical (keyword-only) + abstain

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 94.1% | 20/30 | 127/177 |
| identifier | 10 | 100.0% | 7/8 | 1/11 |
| paraphrase | 12 | 80.0% | 5/9 | 45/55 |

## Where it failed, by question

### lexical (keyword-only)

- No relevant hit at all (3): chg-02, rel-01-p, chg-04-p
- Recency trap ranked first (6): rel-01, rel-09, chg-04, rel-04-p, chg-04-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (24): rel-01, rel-04, rel-05, rel-06, rel-07, rel-08, rel-09, rej-01, rej-02, rej-05, rej-06, rej-07, rej-08, rej-09, rej-10, chg-01, chg-02, chg-03, rel-01-p, rel-07-p, rej-03-p, chg-01-p, chg-08-p, idn-02
- No-answer question that returned something (12): non-01, non-02, non-03, non-04, non-05, non-06, non-07, non-08, non-09, non-10, non-01-p, non-05-p

### lexical (keyword-only) + abstain

- No relevant hit at all (3): chg-02, rel-01-p, chg-04-p
- Recency trap ranked first (5): rel-01, rel-09, chg-04, rel-04-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (24): rel-01, rel-04, rel-05, rel-06, rel-07, rel-08, rel-09, rej-01, rej-02, rej-05, rej-06, rej-07, rej-08, rej-09, rej-10, chg-01, chg-02, chg-03, rel-01-p, rel-07-p, rej-03-p, chg-01-p, chg-08-p, idn-02
- No-answer question that returned something (8): non-01, non-02, non-03, non-04, non-08, non-09, non-01-p, non-05-p
