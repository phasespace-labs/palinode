# Re-measurement: the auto-footer is no longer indexed

Same rig, same corpus (v1) and question set (v1) as
[`../relevance-abstention-baseline-2026-09-20`](../relevance-abstention-baseline-2026-09-20).
The only difference is the working tree: the indexer now projects the generated
`## See also` footer out of the text it derives a chunk from, and a section left
with no text gets no chunk row.

## What ran

| Arm | Baseline 2026-09-20 | This run |
|---|---|---|
| lexical (keyword-only) | ran | ran |
| hybrid @ 0.40 | ran | **NOT RUN** |
| hybrid @ 0.50 | ran | **NOT RUN** |

The hybrid arms did not run because no embedding endpoint was reachable **from
the interpreter**: on this machine `curl` reaches the embed host and the Python
process does not (`OSError 65, No route to host` on a direct socket, with and
without the harness sandbox). The rig refuses to fabricate query vectors, so the
arms are recorded as NOT RUN rather than filled. **The hybrid deltas below are
therefore unmeasured**, not unchanged — re-run on a host whose interpreter can
reach the model before quoting any hybrid number.

The lexical arm is deterministic: it was re-run on this machine against the
unchanged tree first and reproduced every recorded baseline figure exactly
(recall 92.3 %, top-1 32/48, injections 197/268, footer-only 3/268), so the
before/after comparison below is a change comparison and not a machine
comparison.

## Index

| | before | after |
|---|---:|---:|
| Records → files | 39 → 39 | 39 → 39 |
| Chunks | 60 | 56 |

The four missing chunks are the four footer-only sections: the corpus's four
sectioned records with entities each carried a body over the parser's 2000-char
threshold, which gives the trailing `## See also` block a section of its own.

## Lexical arm, before → after

| Metric | before | after | |
|---|---:|---:|---|
| Footer-only hits | 3/268 (1.1 %) | **0/268 (0.0 %)** | the target |
| Relevant-hit recall@k | 92.3 % | 92.3 % | unchanged |
| Answerable questions with a relevant hit | 45/48 | 45/48 | unchanged |
| Top-1 correct | 32/48 (66.7 %) | 32/48 (66.7 %) | unchanged |
| Irrelevant injections | 197/268 (73.5 %) | 197/268 (73.5 %) | unchanged |
| Redundant results | 9/268 | 9/268 | unchanged |
| Repeat slots for one source file | 12/268 (4.5 %) | 10/268 (3.7 %) | better |
| Project-isolation violations | 24/211 (11.4 %) | **25/211 (11.8 %)** | **worse by one** |
| Correct abstention (no-answer) | 2/14 | 2/14 | unchanged |
| Recency trap ranked first | 6/17 | 6/17 | unchanged |
| Useful-context tokens | 6022/30929 (19.5 %) | **6022/31159 (19.3 %)** | **worse by 0.14 pt** |
| Payload tokens per question | 498.9 | 502.6 | +0.7 % |
| p50 / p95 latency (ms) | 1.51 / 2.02 | 1.52 / 1.89 | noise |

Five of 62 slates changed at all. Three are the questions that had a footer-only
hit; two are reorderings below the cut.

## The three footer-only hits, individually

`top_k` is filled unconditionally (baseline finding 2), so removing a result does
not shorten the slate — it promotes whatever was next. Every freed slot was
refilled:

- **chg-02** (changed decision, lexical recall miss before *and* after): the
  rank-1 `hl-weekly-notes/see-also` chunk is gone. Ranks 1–3 move up, `…/chores`
  fills rank 4, and `td-retention-decision` — a record from the *other* project —
  fills rank 5. That single result is the entire project-isolation regression:
  24 → 25 violations. One irrelevant result was exchanged for a differently
  irrelevant one; total injections did not move. This question needs the ranking
  and no-confident-match work, not footer handling.
- **non-04** (no answer): freed slot refilled by `hl-retention-new/root`.
- **non-09** (no answer): freed slot refilled by `hl-schema-v2-old/root`.

For a no-answer question every delivered result is an injection by definition, so
the injection count for those two is unchanged by construction. What improved is
*what kind* of wrong result is delivered: a record that says something, rather
than a list of wikilinks that says nothing. What did not improve is that anything
is delivered at all — that is the abstention half of the workstream.

The useful-context fraction dips because the body chunks that backfilled the
freed slots are longer than the footer chunks they replaced: useful tokens are
identical (6022), the payload grew by 230 tokens. Deleting the cheapest useless
result makes the remaining uselessness cost slightly more — an argument for the
budget work, not against this change.
