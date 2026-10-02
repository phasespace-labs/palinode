# The keyword arm, priced per query

Same rig, same corpus (v1) and question set (v1) as
[`../relevance-no-confident-match-2026-09-22`](../relevance-no-confident-match-2026-09-22),
which is the run these numbers are compared against. One thing changed in the
working tree: `search_fts` no longer normalizes BM25 as `|bm25| / 25`. It
divides by what *that query* could score in *that index* — the sum of its
units' IDFs, which is the BM25 of a document holding every query term once at
average length (`store.bm25_query_scale`). 1.0 is that reference document; a
denser chunk can pass it. The keyword confidence marks were re-calibrated on
this fixture against the new scale and moved 0.22 → **0.60** (confident) and
0.13 → **0.22** (weak).

## What ran, and on which host

| File | What it is |
|---|---|
| `results-hybrid-ct.json` / `report-hybrid-ct.md` | **The run of record**: all six arms, on the Linux host that reaches a real `bge-m3`, against this branch at its pushed head. Quote this one. |
| `results.json` / `report.md` | The same tree on macOS with no embedder reachable — the lexical arms, where the marks were calibrated. |
| `results-legacy-scale.json` / `report-legacy-scale.md` | The same rig, same macOS host, with the old scale put back: the before column. |

The before column is not a quote from the older directory: it is a fresh run of
the same tree with `store.bm25_query_scale` replaced by a function returning
`25.0` (which *is* `|bm25| / 25`) and the keyword marks put back to 0.22/0.13.
Nothing else differs between the two files, so every difference between them is
this change and not the two weeks of ranking work in between. It reproduces the
older directory's macOS lexical column exactly (confident 26 / weak 21 / none 1).

The hybrid arms could not run on the macOS host — the interpreter cannot reach
an embedding endpoint, and a fabricated vector would make every number in that
column meaningless — so they were run on the measurement host instead and the
replay that stood here in the meantime has been replaced by what it measured.
The replay was exact: same five questions moved, same counts, at both floors.
The two hosts differ by a hair on the lexical arm's payload accounting (375.6
vs 377.2 tokens per question, useful-context 26.4% vs 26.3%) and not at all on
any counted outcome or on any verdict.

## Corpus-size invariance, which is the point

One record holding `PLNC-4821`, filler around it, three store sizes, the same
query. Measured through `search_fts` against real SQLite/FTS5
(`tests/test_bm25_query_scale.py` pins it):

| Store | raw `bm25()` | old `\|bm25\| / 25` | per-query scale |
|---|---:|---:|---:|
| 3 records | 0.53 | 0.021 | **1.031** |
| 20 records | 2.67 | 0.107 | **1.043** |
| 200 records | 5.11 | 0.204 | **1.045** |

The quantity the constant 25 was dividing grows 9.7× across those three
stores; what the arm reports now moves by 1.4%. The 0.021 and 0.107 are the
two numbers the previous calibration recorded (as 0.021 and 0.105, on its own
fixture) as the reason its marks could not be trusted on a small store.

(Above 1.0 because that record is shorter than the fixture's average document —
1.0 is a reference, not a ceiling. The arithmetic ceiling is FTS5's `k1 + 1`,
2.2.)

## What did **not** move: the delivered slate

The rescale divides every candidate of one query by the same positive number,
so each candidate's ratio against the best one — the only quantity
`fts_threshold` (0.40) and `lexical_fts_threshold` (0.35) read — is unchanged,
and RRF fuses ranks. That is an argument; this is the measurement. Lexical arm,
before → after, every delivery metric the rig records:

| Metric | legacy scale | per-query scale |
|---|---:|---:|
| Results delivered | 200 | 200 |
| Relevant-hit recall@k | 94.2% | 94.2% |
| Answerable questions with a relevant hit | 46/48 | 46/48 |
| Top-1 correct | 32/48 | 32/48 |
| Irrelevant injections | 128/200 (64.0%) | 128/200 (64.0%) |
| Redundant results | 7/200 | 7/200 |
| Project-isolation violations | 18/153 | 18/153 |
| Recency trap ranked first | 6/17 | 6/17 |
| Payload tokens per question | 377.2 | 377.2 |
| Useful-context tokens | 26.3% | 26.3% |

Identical, metric for metric. The only column that moves is the verdict.

**Cost:** one `count(*)` per query unit, on the index the search just ran
against. p50 latency 1.82 ms → 1.89 ms on this 56-chunk store (p95 2.47 → 2.18,
i.e. inside the noise of a 186-call sample). On the measurement host the
lexical arm runs at 1.66 ms p50 against the hybrid arm's 88.9 ms, so the cost
is a rounding error beside an embedding call and a real fraction of a
keyword-only search.

The hybrid arms have no before column — there is no legacy-scale run with an
embedder, and `main` moved under the older directory's numbers — so they are
reported as measured, and what carries the "delivery is unchanged" claim for
them is the argument plus the lexical measurement of it. For the record, the
run of record's hybrid figures: recall@k 100% at both floors, top-1 37/48 at
0.40 and 36/48 at 0.50, injections 186/262 and 164/240, useful-context 23.5%
and 25.1%, payload 429.0 and 403.1 tokens per question — every one of them the
same as the `vector_relative_floor` measurement that landed just before this
branch, which is the comparison that matters.

**The abstain switch**, measured here on all six arms (off → on): correct
abstention lexical 2/14 → 8/14, hybrid @0.40 0/14 → 2/14, hybrid @0.50 2/14 →
2/14; irrelevant injections lexical 128/200 → 103/175, hybrid @0.40 186/262 →
179/255, hybrid @0.50 unchanged; recall@k, answerable questions with a
relevant hit and top-1 unchanged in every arm. The lexical column is where the
re-calibration shows: it withholds six no-answer slates rather than the old
marks' five, and none of them is a question the store can answer.

## The verdict matrix

### lexical (keyword-only), measured

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident (legacy 0.22) | 26 | 26 | 0 |
| confident (**0.60**) | **33** | **33** | **0** |
| weak (legacy) | 21 | 20 | 8 |
| weak (**new**) | 15 | 13 | 6 |
| none (legacy 0.13) | 1 | 0 | 6 |
| none (**0.22**) | **0** | 0 | **8** |

Seven answerable questions move up and no no-answer question does. Two more
no-answer questions reach `none`, and the one answerable question the old marks
called `none` (`chg-04-p`, whose slate held no relevant record at all) is no
longer called that.

### hybrid — measured, real `bge-m3`, and identical in all four hybrid arms

Both cosine floors (0.40 and 0.50) and the abstain switch off or on give the
same matrix, so it is written once:

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident (as shipped) | 41 | 41 | 0 |
| **confident (this change)** | **46** | **46** | **0** |
| weak (as shipped) | 7 | 7 | 12 |
| weak (this change) | 2 | 2 | 12 |
| none | 0 | 0 | 2 |

Five questions move weak → confident: `idn-03`, `idn-04`, `idn-05`, `idn-07` —
four of the eight exact-identifier questions, the four whose cosine (0.46–0.51)
never let the vector arm vouch for them — and `chg-04-p`, the held-out
paraphrase that corroboration used to demote. Nothing moves down. The two
answerable questions still `weak` are `rej-05-p` and `chg-06-p`; the two
`none` are `idn-09` and `idn-10`, both no-answer and both delivering nothing.
**No no-answer question becomes confident**, and corroboration still fires on
the three it was built for (`non-05`, `non-07`, `non-10`: best keyword 0.000,
0.000, 0.199 against cosine 0.635, 0.629, 0.607 — under the new weak mark as
they were under the old one).

This is what the pre-measurement replay predicted, question for question: the
replay is in the git history of this file, and the measurement changed none of
it.

## Where the marks come from

Both were read off this fixture, the same way the vector marks were read off
theirs.

**Confident, 0.60.** The highest score any no-answer question produced is
0.557. The number of answerable questions above the mark is 33 at every value
from 0.56 to 0.62, and each of those 33 slates held a relevant record — so the
mark is taken from the middle of a plateau rather than fitted to the 0.557. It
costs six answerable questions to go to 0.65 and none to move within the
plateau.

| Mark | answerable confident (with a relevant record) | no-answer confident |
|---|---:|---:|
| 0.50 | 39 (38) | 1 |
| 0.55 | 33 (33) | 1 |
| **0.56 – 0.62** | **33 (33)** | **0** |
| 0.65 | 31 (31) | 0 |
| 0.80 | 28 (28) | 0 |

**Weak, 0.22.** The last value that calls no answerable question `none`, and
the best of the plateau either side of it.

| Mark | no-answer `none` (of 14) | answerable `none` (of 48) | hybrid confident |
|---|---:|---:|---:|
| 0.20 | 7 | 0 | 46 |
| **0.22** | **8** | **0** | **46** |
| 0.24 | 8 | 1 (slate held nothing relevant) | 45 |
| 0.27 | 9 | 3 (2 of them held a relevant record) | 44 |
| 0.30 | 11 | 4 | — |

The 0.22 row is the measured run; the other marks were not run, so their
hybrid column is computed from the same recorded per-row keyword scores. The
one row that was computed and then measured came out identical.

The weak mark is what corroboration spends, so the hybrid column is the one
that decides it: raising it past 0.22 buys no extra abstention until 0.27 and
costs confident answers on the way.

That 0.60 is also the vector arm's confident mark is a coincidence of two
calibrations. The arms are still on unrelated scales.

## What the new scale cannot do

Coverage is not discrimination. A query whose terms are all *common* in the
store is fully covered by any chunk holding them, and scores near 1.0 — true,
and no evidence at all. The old scale had the mirror-image defect (it called a
perfect identifier hit weak), and this fixture contains no question of the
first kind: the highest a no-answer question reaches is 0.557. On a store of
two documents, where FTS5 rates *every* term as common, the arm can only report
coverage; that is arithmetic in `bm25()`, not a choice made here.

A denominator built only from the terms the corpus actually holds was tried and
rejected on this fixture: it puts five of the fourteen no-answer questions at a
perfect 1.0, because a query full of words the store does not know is trivially
easy to "cover" completely. Units the store has never seen are priced at the
rarest a term can be (`df = 1`) for exactly that reason.

## Reproducing

```bash
python -m bench.relevance --out results.json --report report.md
```

For the before column, run the same thing with `store.bm25_query_scale`
monkeypatched to `lambda db, units, table="chunks_fts": 25.0` and
`confidence.KEYWORD_CONFIDENT` / `KEYWORD_WEAK` set to 0.22 / 0.13.
