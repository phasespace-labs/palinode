# Re-measurement: the slate is the evidence, not the limit

Same rig, same corpus (v1) and question set (v1) as the two runs before it —
[`../relevance-abstention-baseline-2026-09-20`](../relevance-abstention-baseline-2026-09-20)
(the recorded baseline) and
[`../relevance-footer-not-indexed-2026-09-21`](../relevance-footer-not-indexed-2026-09-21)
(the footer fix, which is what `main` does today and therefore the **before**
column here). The only difference is the working tree: two bounds now decide
how much of `top_k` is filled.

- `search.lexical_fts_threshold` (new, default **0.35**) — the keyword arm's
  relative floor when it is the only arm. That path used to hard-set the FTS
  floor to `0.0`, so every query came back with `top_k` results whatever the
  evidence was.
- `search.max_chunks_per_file` (new, default **1**) — how many chunks of one
  file may sit ahead of other files' competitive chunks. Overflow is deferred,
  not dropped: it still fills a slate that would otherwise be short.

Both are relative to the best candidate in the same result set, so the top
result always survives. **Neither is an abstention floor** and neither
produced a new empty slate: the two questions that returned nothing here are
the same two that returned nothing before, for the same reason (no keyword
candidate at all).

## What ran

| Arm | Baseline 2026-09-20 | Footer fix 2026-09-21 | This run |
|---|---|---|---|
| lexical (keyword-only) | ran | ran | ran |
| hybrid @ 0.40 | ran | **NOT RUN** | **NOT RUN** |
| hybrid @ 0.50 | ran | **NOT RUN** | **NOT RUN** |

The hybrid arms did not run: no embedding endpoint is reachable **from the
interpreter** on this machine (the same condition recorded on 2026-09-21 —
`curl` reaches the embed host, the Python process does not). The rig refuses
to fabricate query vectors. **Every hybrid number below is therefore
unmeasured, not unchanged.** Re-run on a host whose interpreter can reach the
model before quoting one.

What that leaves unmeasured is specific, and it is the larger half of the
baseline's finding: the keyword floor above only applies where the keyword arm
is alone. In hybrid mode the vector arm still admits `top_k × 2` candidates on
an absolute cosine floor with no relative cutoff, so the padding the baseline
measured there (226/304 injections at cosine 0.40) is untouched except by the
per-file cap. A relative floor for the vector arm is the obvious next step and
is deliberately **not** in this change: its default cannot be picked by
measurement from a rig that cannot run the arm.

The lexical arm is deterministic. Before making any change, the unchanged tree
was re-run on this machine and reproduced the 2026-09-21 figures exactly
(recall 92.3 %, top-1 32/48, injections 197/268, same-file 10/268, isolation
25/211, useful 6022/31159), so the comparison below is a change comparison and
not a machine comparison.

## Lexical arm, before → after

62 questions, `top_k` 5, snippet cap 400 chars. Denominators move because the
change is *about* the denominator: 268 results delivered before, 200 after.

| Metric | before | after | |
|---|---:|---:|---|
| Relevant-hit recall@k | 48/52 (92.3 %) | **49/52 (94.2 %)** | **better** |
| Answerable questions with a relevant hit | 45/48 | **46/48** | better |
| Top-1 correct | 32/48 (66.7 %) | 32/48 (66.7 %) | unchanged |
| Results delivered | 268 | **200** | −25 % |
| Irrelevant injections | 197/268 (73.5 %) | **128/200 (64.0 %)** | −69 results |
| Footer-only hits | 0/268 | 0/200 | unchanged |
| Redundant results (near-duplicate records) | 9/268 (3.4 %) | 7/200 (**3.5 %**) | −2 results, **+0.1 pt rate** |
| Repeat slots for one source file | 10/268 (3.7 %) | **2/200 (1.0 %)** | better |
| Project-isolation violations | 25/211 (11.8 %) | 18/153 (11.8 %) | −7 results, rate flat |
| Correct abstention (no-answer) | 2/14 | 2/14 | unchanged (out of scope) |
| No-answer questions returning something | 12/14 | 12/14 | unchanged (out of scope) |
| Recency trap ranked first | 6/17 (35.3 %) | 6/17 (35.3 %) | unchanged |
| Useful-context tokens | 6022/31159 (19.3 %) | **6140/23389 (26.3 %)** | +7.0 pt |
| Payload tokens per question | 502.6 | **377.2** | −25 % |
| p50 / p95 latency (ms) | 1.52 / 1.89 | 1.34 / 1.61 | noise |

Per class (injections / results): release_state 51/72 → 34/55, rejected_approach
48/65 → 17/34, changed_decision 41/74 → 30/64, no_answer 57/57 → 47/47.
Held-out splits: paraphrase 49/60 → 33/44 injections at unchanged 80.0 % recall;
identifier 1/11 → 1/11 at unchanged 100 % recall.

### Nothing got worse, per question

Every question was compared individually across the two runs on relevant hits
found, rank of the first relevant hit, top-1, injections, isolation
violations, same-file repeats, redundancy, recency-trap-first and abstention.
**No question regressed on any of them.** One improved: `chg-02` ("Did the
Harborlight retention policy change, and from what to what?"), the lexical
recall miss carried since the baseline. A four-section weekly note took 3 of
its 5 slots; capped to one, `hl-retention-new` — a record that answers it —
entered the slate (recall 0/2 → 1/2).

The one number that moved the wrong way is a rate, not a count: redundant
results fell 9 → 7 while the denominator fell 268 → 200, so the *rate* rose
0.1 pt. Near-duplicate distinct records are a different defect from same-file
repeats and this change does not address them.

Slate sizes, questions by results returned:

| results | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---:|---:|---:|---:|---:|---:|
| before | 2 | 6 | 2 | 1 | 0 | 51 |
| after | 2 | 15 | 6 | 8 | 6 | 25 |

That is the whole point of the change: 51 of 62 questions used to get exactly
five results regardless of how much the store had to say; now 25 do.

## Why 0.35, and why 1

Both defaults are taken from the recorded baseline's per-question data, not
from taste.

**0.35 is the last floor below the weakest true hit.** Across the 48 relevant
results delivered in the baseline, the lowest keyword score relative to the
top match in its own slate was **0.387** (`chg-05`, rank 3); the next two were
0.495 and 0.498. Irrelevant results sit far lower — 10 % of them score 0.0
against the top match, 30 % are below 0.276. Swept on the rig (cap fixed at
the shipped value):

| floor | delivered | recall | same-file | useful-context | tokens/question |
|---:|---:|---:|---:|---:|---:|
| 0.0 (no floor) | 268 | 49/52 | 0 | 19.8 % | 500.7 |
| 0.20 | 231 | 49/52 | 2 | 22.7 % | 436.9 |
| 0.30 | 207 | 49/52 | 1 | 25.4 % | 389.6 |
| **0.35** | **200** | **49/52** | **2** | **26.3 %** | **377.2** |
| 0.40 | 194 | 48/52 | 3 | 26.6 % | 366.5 |
| 0.50 | 175 | 46/52 | 4 | 28.7 % | 329.3 |

0.40 — the two-arm `fts_threshold` — is where recall starts paying: it drops
the 0.387 hit. 0.50 costs three. The single-arm floor is a separate key for
exactly that reason: with no vector arm to re-admit a candidate, the floor has
to be the looser of the two.

**1 beat 2 on every counted outcome.** At the 0.35 floor:

| cap | delivered | recall | injections | same-file | useful-context |
|---:|---:|---:|---:|---:|---:|
| 0 (old, unlimited) | 200 | 48/52 | 129 | 9 | 25.7 % |
| **1** | **200** | **49/52** | **128** | **2** | **26.3 %** |
| 2 | 200 | 48/52 | 129 | 6 | 25.7 % |
| 3 | 200 | 48/52 | 129 | 8 | 25.7 % |

Delivered count is identical across every cap — the cap defers, it never
discards — so this row is pure quality-per-slot. The recall difference is
`chg-02` above; a cap of 2 leaves the weekly note holding two slots and the
record that answers the question stays out.

The two same-file repeats that survive at cap 1 are the backfill working as
designed: a capped chunk is delivered rather than leaving a slate short.

## Still open after this change

- **Abstention is untouched and deliberately so.** 12 of 14 no-answer
  questions still return something (47 results), and every one of them is an
  injection by construction. A relative floor cannot abstain — the top
  candidate always clears it. That is the next change and needs its own
  measurement.
- **The hybrid arm is unmeasured here**, including the vector arm's missing
  relative floor (above).
- **Injections are still the majority of delivered results** (64.0 %). The
  remaining share is ranking and project isolation, not slate length.
- **Project isolation did not improve as a rate** (11.8 % either way) and
  recency traps did not move at all (6/17). Both were already named as
  separate ranking work in the baseline.

## Hybrid arms — measured 2026-09-22 on a host that reaches the embedder

The MBP's venv interpreter cannot reach the embedding host, so the hybrid arms
above were NOT RUN there. They were re-run the same day from a Linux host on the
same network against real `bge-m3` (1024-dim), Python 3.11.2, this branch's tree,
once with the two knobs at their shipped defaults (`report-hybrid-ct.md`) and
once with both knobs off — `lexical_fts_threshold: 0.0`,
`max_chunks_per_file: 0` (`report-hybrid-ct-knobs-off.md`). The lexical arm on
that host reproduced this README's before/after figures exactly.

| hybrid, knobs off → on | @ cosine floor 0.40 | @ cosine floor 0.50 |
|---|---|---|
| recall@k | 100.0% → 100.0% | 100.0% → 100.0% |
| top-1 | 37/48 → 37/48 | 36/48 → 36/48 |
| irrelevant injections | 224/304 → 224/304 | 198/277 → 198/277 |
| repeat slots for one file | 7/304 → 0/304 | 7/277 → 0/277 |
| useful-context tokens | 20.0% → 20.1% | 21.4% → 21.5% |
| payload tokens / question | 505.0 → 502.0 | 472.4 → 469.4 |

Read: the per-file cap works on the shipped default mode (same-file repeats go to
zero, no recall lost); the slate does **not** shrink, because the keyword floor
only governs the keyword-only path and the vector arm still admits `top_k × 2`
candidates on an absolute cosine floor with no relative cutoff. On hybrid the
injection rate is unchanged. That is the vector-arm relative floor named above
as the next change, and it can now be measured on this host.
