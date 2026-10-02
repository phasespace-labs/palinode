# Palinode relevance & abstention baseline

Measured against whatever the working tree does — the rig changes nothing itself, so a run is a baseline or a re-measurement depending only on when it was taken. Production search defaults changed by this run: `False`.

- Generated: 2026-09-22T16:23:27.364731+00:00
- Palinode: 0.21.0 · Python 3.11.2 · Linux-7.0.14-14-pve-x86_64-with-glibc2.36
- Embedding model: bge-m3 (1024 dimensions) · reachable: **yes**
- Corpus v1 · question set v1
- 39 records → 39 files → 56 chunks, 56 embedded (0 keyword-only)
- 62 questions · top-k 5 · fts_threshold 0.40 · hybrid_weight 0.50 · snippet cap 400 chars
- Keyword-only floor 0.35 · max chunks per file 1 (0 = unlimited)
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
- **hybrid @ cosine floor 0.40** — ran.
- **hybrid @ cosine floor 0.40 + abstain** — ran.
- **hybrid @ cosine floor 0.50** — ran.
- **hybrid @ cosine floor 0.50 + abstain** — ran.

## Headline

| Metric | lexical (keyword-only) | lexical (keyword-only) + abstain | hybrid @ cosine floor 0.40 | hybrid @ cosine floor 0.40 + abstain | hybrid @ cosine floor 0.50 | hybrid @ cosine floor 0.50 + abstain |
|---|---:|---:|---:|---:|---:|---:|
| Relevant-hit recall@k | 94.2% | 94.2% | 100.0% | 100.0% | 100.0% | 100.0% |
| Answerable questions with a relevant hit | 46/48 | 46/48 | 48/48 | 48/48 | 48/48 | 48/48 |
| Top-1 correct | 32/48 (66.7%) | 32/47 (68.1%) | 37/48 (77.1%) | 37/48 (77.1%) | 36/48 (75.0%) | 36/48 (75.0%) |
| Irrelevant injections (of results delivered) | 128/200 (64.0%) | 110/181 (60.8%) | 224/304 (73.7%) | 217/297 (73.1%) | 198/277 (71.5%) | 198/277 (71.5%) |
| Footer-only hits | 0/200 (0.0%) | 0/181 (0.0%) | 0/304 (0.0%) | 0/297 (0.0%) | 0/277 (0.0%) | 0/277 (0.0%) |
| Redundant results (near-duplicate records) | 7/200 (3.5%) | 7/181 (3.9%) | 15/304 (4.9%) | 15/297 (5.1%) | 13/277 (4.7%) | 13/277 (4.7%) |
| Repeat slots for one source file | 2/200 (1.0%) | 2/181 (1.1%) | 0/304 (0.0%) | 0/297 (0.0%) | 0/277 (0.0%) | 0/277 (0.0%) |
| Project-isolation violations | 18/153 (11.8%) | 18/150 (12.0%) | 15/240 (6.2%) | 15/240 (6.2%) | 11/220 (5.0%) | 11/220 (5.0%) |
| Correct abstention (no-answer questions) | 2/14 (14.3%) | 6/14 (42.9%) | 0/14 (0.0%) | 2/14 (14.3%) | 2/14 (14.3%) | 2/14 (14.3%) |
| Inappropriate confident match | 12/14 (85.7%) | 8/14 (57.1%) | 14/14 (100.0%) | 12/14 (85.7%) | 12/14 (85.7%) | 12/14 (85.7%) |
| Confidence band (median top score of a correct top-1) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| Distinct top scores behind that band | 1 | 1 | 1 | 1 | 1 | 1 |
| Recency trap ranked first | 6/17 (35.3%) | 5/16 (31.2%) | 5/17 (29.4%) | 5/17 (29.4%) | 5/17 (29.4%) | 5/17 (29.4%) |
| Useful-context tokens (of delivered payload) | 6140/23289 (26.4%) | 6140/21358 (28.7%) | 6253/31125 (20.1%) | 6253/30658 (20.4%) | 6269/29105 (21.5%) | 6269/29105 (21.5%) |
| Payload tokens per question | 375.6 | 344.5 | 502.0 | 494.5 | 469.4 | 469.4 |
| p50 latency (ms) | 1.54 | 1.53 | 85.93 | 85.17 | 87.01 | 86.85 |
| p95 latency (ms) | 1.84 | 1.82 | 108.23 | 101.52 | 107.15 | 96.29 |
| Keyword-fallback rate (of calls) | 0/186 (0.0%) | 0/186 (0.0%) | 0/186 (0.0%) | 0/186 (0.0%) | 0/186 (0.0%) | 0/186 (0.0%) |
| Calls with no BM25 candidate | 0 | 0 | 6 | 6 | 6 | 6 |

Latency covers the whole client-visible path: the query embedding where there is one, the store search, and rendering the delivered payload. The lexical arm builds no query vector, so its keyword-fallback count is zero by construction rather than by measurement.

Read *inappropriate confident match* together with the row above it. The band is the median score a correct top-1 carries; when only one distinct score sits behind it, the delivered score is a function of rank rather than of relevance and the band cannot separate anything. In that case the number to act on is the abstention rate, not the confident-match rate.

No project filter, category filter or context was passed on any call: these are the arguments an unadorned `palinode_search` sends, which is what makes project isolation a property of ranking here rather than of a filter the caller remembered to set.

## Confidence verdict

The delivery-level verdict, read from the pre-fusion arm scores, crossed with whether the question had an answer in the corpus. The two cells to read are `confident` under *no-answer* (a confident wrong answer) and `none` under *answerable* (a refusal to answer something the store holds) — the second is qualified by how many of those slates contained a relevant record at all.

### lexical (keyword-only)

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 25 | 25 | 0 |
| weak | 22 | 21 | 8 |
| none | 1 | 0 | 6 |

Graded deliveries: 62.

### lexical (keyword-only) + abstain

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 25 | 25 | 0 |
| weak | 22 | 21 | 8 |
| none | 1 | 0 | 6 |

Graded deliveries: 62.

### hybrid @ cosine floor 0.40

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 41 | 41 | 0 |
| weak | 7 | 7 | 12 |
| none | 0 | 0 | 2 |

Graded deliveries: 62.

### hybrid @ cosine floor 0.40 + abstain

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 41 | 41 | 0 |
| weak | 7 | 7 | 12 |
| none | 0 | 0 | 2 |

Graded deliveries: 62.

### hybrid @ cosine floor 0.50

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 41 | 41 | 0 |
| weak | 7 | 7 | 12 |
| none | 0 | 0 | 2 |

Graded deliveries: 62.

### hybrid @ cosine floor 0.50 + abstain

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 41 | 41 | 0 |
| weak | 7 | 7 | 12 |
| none | 0 | 0 | 2 |

Graded deliveries: 62.

## Per class

### lexical (keyword-only)

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 94.1% | 12/17 | 34/55 | 0 | 3 | 1 | 8 | 0/0 | 4/8 |
| rejected_approach | 13 | 100.0% | 10/13 | 17/34 | 0 | 4 | 0 | 5 | 0/0 | 0/0 |
| changed_decision | 18 | 90.5% | 10/18 | 30/64 | 0 | 0 | 0 | 5 | 0/0 | 2/9 |
| no_answer | 14 | n/a | 0/0 | 47/47 | 0 | 0 | 1 | 0 | 2/14 | 0/0 |

### lexical (keyword-only) + abstain

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 94.1% | 12/17 | 34/55 | 0 | 3 | 1 | 8 | 0/0 | 4/8 |
| rejected_approach | 13 | 100.0% | 10/13 | 17/34 | 0 | 4 | 0 | 5 | 0/0 | 0/0 |
| changed_decision | 18 | 90.5% | 10/17 | 28/61 | 0 | 0 | 0 | 5 | 0/0 | 1/8 |
| no_answer | 14 | n/a | 0/0 | 31/31 | 0 | 0 | 1 | 0 | 6/14 | 0/0 |

### hybrid @ cosine floor 0.40

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 100.0% | 11/17 | 60/85 | 0 | 7 | 0 | 8 | 0/0 | 2/8 |
| rejected_approach | 13 | 100.0% | 13/13 | 47/65 | 0 | 6 | 0 | 5 | 0/0 | 0/0 |
| changed_decision | 18 | 100.0% | 13/18 | 53/90 | 0 | 2 | 0 | 2 | 0/0 | 3/9 |
| no_answer | 14 | n/a | 0/0 | 64/64 | 0 | 0 | 0 | 0 | 0/14 | 0/0 |

### hybrid @ cosine floor 0.40 + abstain

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 100.0% | 11/17 | 60/85 | 0 | 7 | 0 | 8 | 0/0 | 2/8 |
| rejected_approach | 13 | 100.0% | 13/13 | 47/65 | 0 | 6 | 0 | 5 | 0/0 | 0/0 |
| changed_decision | 18 | 100.0% | 13/18 | 53/90 | 0 | 2 | 0 | 2 | 0/0 | 3/9 |
| no_answer | 14 | n/a | 0/0 | 57/57 | 0 | 0 | 0 | 0 | 2/14 | 0/0 |

### hybrid @ cosine floor 0.50

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 100.0% | 11/17 | 51/76 | 0 | 5 | 0 | 4 | 0/0 | 2/8 |
| rejected_approach | 13 | 100.0% | 13/13 | 47/65 | 0 | 6 | 0 | 5 | 0/0 | 0/0 |
| changed_decision | 18 | 100.0% | 12/18 | 43/79 | 0 | 2 | 0 | 2 | 0/0 | 3/9 |
| no_answer | 14 | n/a | 0/0 | 57/57 | 0 | 0 | 0 | 0 | 2/14 | 0/0 |

### hybrid @ cosine floor 0.50 + abstain

| Class | questions | recall@k | top-1 | injections / results | footer-only | redundant | same-file | isolation | abstained | trap first |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| release_state | 17 | 100.0% | 11/17 | 51/76 | 0 | 5 | 0 | 4 | 0/0 | 2/8 |
| rejected_approach | 13 | 100.0% | 13/13 | 47/65 | 0 | 6 | 0 | 5 | 0/0 | 0/0 |
| changed_decision | 18 | 100.0% | 12/18 | 43/79 | 0 | 2 | 0 | 2 | 0/0 | 3/9 |
| no_answer | 14 | n/a | 0/0 | 57/57 | 0 | 0 | 0 | 0 | 2/14 | 0/0 |

## Base versus held-out

### lexical (keyword-only)

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 97.1% | 20/30 | 94/145 |
| identifier | 10 | 100.0% | 7/8 | 1/11 |
| paraphrase | 12 | 80.0% | 5/10 | 33/44 |

### lexical (keyword-only) + abstain

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 97.1% | 20/30 | 78/129 |
| identifier | 10 | 100.0% | 7/8 | 1/11 |
| paraphrase | 12 | 80.0% | 5/9 | 31/41 |

### hybrid @ cosine floor 0.40

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 100.0% | 24/30 | 144/197 |
| identifier | 10 | 100.0% | 6/8 | 35/47 |
| paraphrase | 12 | 100.0% | 7/10 | 45/60 |

### hybrid @ cosine floor 0.40 + abstain

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 100.0% | 24/30 | 144/197 |
| identifier | 10 | 100.0% | 6/8 | 28/40 |
| paraphrase | 12 | 100.0% | 7/10 | 45/60 |

### hybrid @ cosine floor 0.50

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 100.0% | 24/30 | 145/197 |
| identifier | 10 | 100.0% | 5/8 | 8/20 |
| paraphrase | 12 | 100.0% | 7/10 | 45/60 |

### hybrid @ cosine floor 0.50 + abstain

| Variant | questions | recall@k | top-1 | injections / results |
|---|---:|---:|---:|---:|
| base | 40 | 100.0% | 24/30 | 145/197 |
| identifier | 10 | 100.0% | 5/8 | 8/20 |
| paraphrase | 12 | 100.0% | 7/10 | 45/60 |

## Where it failed, by question

### lexical (keyword-only)

- No relevant hit at all (2): rel-01-p, chg-04-p
- Recency trap ranked first (6): rel-01, rel-09, chg-04, rel-04-p, chg-04-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (18): rel-01, rel-04, rel-07, rel-08, rel-09, rej-01, rej-02, rej-07, rej-09, rej-10, chg-01, chg-02, chg-03, rel-01-p, rel-07-p, chg-01-p, chg-08-p, idn-02
- No-answer question that returned something (12): non-01, non-02, non-03, non-04, non-05, non-06, non-07, non-08, non-09, non-10, non-01-p, non-05-p

### lexical (keyword-only) + abstain

- No relevant hit at all (2): rel-01-p, chg-04-p
- Recency trap ranked first (5): rel-01, rel-09, chg-04, rel-04-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (18): rel-01, rel-04, rel-07, rel-08, rel-09, rej-01, rej-02, rej-07, rej-09, rej-10, chg-01, chg-02, chg-03, rel-01-p, rel-07-p, chg-01-p, chg-08-p, idn-02
- No-answer question that returned something (8): non-01, non-02, non-03, non-04, non-08, non-09, non-01-p, non-05-p

### hybrid @ cosine floor 0.40

- No relevant hit at all (0): none
- Recency trap ranked first (5): chg-04, rel-04-p, chg-04-p, chg-06-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (12): rel-04, rel-09, rej-01, rej-02, rej-06, rej-09, rej-10, chg-01, chg-03, rel-07-p, idn-02, idn-06
- No-answer question that returned something (14): non-01, non-02, non-03, non-04, non-05, non-06, non-07, non-08, non-09, non-10, non-01-p, non-05-p, idn-09, idn-10

### hybrid @ cosine floor 0.40 + abstain

- No relevant hit at all (0): none
- Recency trap ranked first (5): chg-04, rel-04-p, chg-04-p, chg-06-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (12): rel-04, rel-09, rej-01, rej-02, rej-06, rej-09, rej-10, chg-01, chg-03, rel-07-p, idn-02, idn-06
- No-answer question that returned something (12): non-01, non-02, non-03, non-04, non-05, non-06, non-07, non-08, non-09, non-10, non-01-p, non-05-p

### hybrid @ cosine floor 0.50

- No relevant hit at all (0): none
- Recency trap ranked first (5): chg-04, rel-04-p, chg-04-p, chg-06-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (11): rel-04, rel-09, rej-01, rej-02, rej-06, rej-09, rej-10, chg-01, chg-03, rel-07-p, idn-02
- No-answer question that returned something (12): non-01, non-02, non-03, non-04, non-05, non-06, non-07, non-08, non-09, non-10, non-01-p, non-05-p

### hybrid @ cosine floor 0.50 + abstain

- No relevant hit at all (0): none
- Recency trap ranked first (5): chg-04, rel-04-p, chg-04-p, chg-06-p, idn-02
- Footer-only hit in the slate (0): none
- Cross-project result (11): rel-04, rel-09, rej-01, rej-02, rej-06, rej-09, rej-10, chg-01, chg-03, rel-07-p, idn-02
- No-answer question that returned something (12): non-01, non-02, non-03, non-04, non-05, non-06, non-07, non-08, non-09, non-10, non-01-p, non-05-p
