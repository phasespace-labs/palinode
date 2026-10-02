# Prospective-trigger phrasing study — 2026-09-20

`report.md`, `results.json` and `tables.md` are that day's run of
`python -m bench.trigger_phrasing`, against a reachable `bge-m3`. The vector
arms (options 1, 2 and 4 — description phrasing and the relative-margin rule)
are unaffected by anything since; they are what the study concluded on.

## Paired re-run, 2026-09-22 — the lexical arm (option 3)

The lexical arm normalized BM25 as `|bm25| / 25`, the same constant
`store.search_fts` used. That constant is gone: both now divide by what the
query could score against the index in front of it
(`store.bm25_query_scale`), and this rig follows the product rather than
keeping a private copy of an old formula. **The lexical and hybrid numbers in
`report.md` and `tables.md` are therefore on the old scale.**
`results-bm25-scale-hybrid-ct.json` is the paired re-run on the measurement
host, same corpus, same `bge-m3`, all arms:

| Arm | vector AUC | lexical AUC old → new | lexical best-F1 threshold | best F1 |
|---|---:|---|---:|---|
| `docs_style` | 0.893 (unchanged) | 0.740 → **0.739** | 0.090 → **0.036** | 0.465 → 0.436 |
| `user_worded` | 0.897 (unchanged) | 0.735 → **0.735** | 0.087 → **0.045** | 0.397 → 0.411 |
| `phrasings_max` | 0.951 (unchanged) | 0.895 → **0.880** | 0.185 → **0.302** | 0.577 → 0.541 |

Every vector column reproduces the published study exactly, which is the check
that nothing but the lexical scale moved.

**The study's conclusion is unchanged**: separation moves by −0.001, 0.000 and
−0.015 AUC, so a lexical arm is still "a cheap recall add-on, not a
separator", and `phrasings_max` is still the arm that separates. What moved is
where the numbers sit, and one of them moved against us: `phrasings_max` loses
0.015 of AUC. A trigger description is a very short document, so the reference
document the new scale divides by — one holding every term of the prompt — is
one no description can be, and a long prompt reads lower against it than a
short one.

## The hybrid-OR sweep, and the floor it reads

**0.20 no longer does what option 3's conclusion needs, and 0.30 does.** The
sweep fires when `vector >= t OR lexical >= floor`; both columns are recorded
per pair, so the floors below are arithmetic over the measured run rather than
a projection. At the vector thresholds where the arm is supposed to earn its
keep:

| Arm, vector threshold | vector alone | OLD scale @ 0.20 | new @ 0.20 | new @ **0.30** |
|---|---|---|---|---|
| `docs_style` 0.70 | 7/56 & 1/1120 | 12/56 & 1/1120 | 18/56 & **9**/1120 | **15/56 & 1/1120** |
| `docs_style` 0.75 | 3/56 & 0/1120 | 9/56 & 0/1120 | 17/56 & **8**/1120 | **13/56 & 0/1120** |
| `user_worded` 0.70 | 10/56 & 0/1120 | 11/56 & 0/1120 | 17/56 & **11**/1120 | **14/56 & 0/1120** |
| `phrasings_max` 0.70 | 22/56 & 2/1120 | 31/56 & 15/1120 | 28/56 & **33**/1120 | **25/56 & 9/1120** |

(on-target pairs fired & off-target pairs fired.)

At 0.20 the new scale admits a band of off-target pairs the old constant did
not: on `docs_style` at 0.70 the arm stops being free (1 → 9 false-firing
pairs), which is precisely the property the published reading rested on. At
0.30 it is free again *and* better than the old constant ever was on the two
single-description arms — three more on-target pairs than the old 0.20 bought,
with off-target unchanged. On `phrasings_max`, where the arm was never free,
0.30 costs 9 off-target pairs for 25 on-target against the old 15 for 31: a
better rate, a smaller absolute recall add, and the same conclusion — it buys
recall where the vector arm has given up and does not resolve the overlap
band.

`DEFAULT_LEXICAL_FLOOR` is therefore **0.30**, with those numbers in the
constant's own comment. The shipped `results-bm25-scale-hybrid-ct.json` was run
at 0.20 (its `parameters.lexical_floor` says so), so its `hybrid` rows are the
"new @ 0.20" column above; the 0.30 column is recomputed from the same file's
per-pair observations. A future run picks up the new default directly.
