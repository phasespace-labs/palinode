# A relative floor for the vector arm

Same rig, same corpus (v1) and question set (v1) as the three runs before it —
[`../relevance-abstention-baseline-2026-09-20`](../relevance-abstention-baseline-2026-09-20),
[`../relevance-footer-not-indexed-2026-09-21`](../relevance-footer-not-indexed-2026-09-21)
and
[`../relevance-slate-not-limit-2026-09-22`](../relevance-slate-not-limit-2026-09-22)
(the **before** column here). One new setting:

- `search.vector_relative_floor` (new, default **0.85**) — the vector arm's
  floor relative to the best cosine in the same candidate set: a candidate is
  delivered only when `cosine >= 0.85 x top_cosine`. Applied before fusion, on
  the arm's own scores, exactly where `fts_threshold` is applied to the keyword
  arm's. `0.0` restores the previous behaviour.

The absolute cosine floors are untouched: `mcp_threshold` 0.40 and
`api_threshold` 0.50 remain what they were and mean what they meant. They
answer *is this candidate plausible at all*. Nothing answered *is it plausible
beside the match this query actually found*, so the arm handed fusion every one
of its `top_k x 2` candidates above the floor and weak neighbours filled every
slot the strong match left.

**This is not an abstention floor.** It is measured against the best candidate
in the arm, so the top match always clears it. It cannot return an empty slate
that would otherwise have had a top hit.

## What the previous run left unmeasured

The slate-not-limit run shipped two bounds and measured them on the keyword
arm: results delivered 268 → 200, injections 197/268 → 128/200, payload 502.6 →
377.2 tokens per question. On **hybrid — the shipped default mode** — the same
two knobs moved almost nothing, because the keyword floor there only governs the
keyword-only path and the per-file cap only re-orders:

| hybrid, knobs off → on | @ cosine floor 0.40 | @ cosine floor 0.50 |
|---|---|---|
| results delivered | 304 → 304 | 277 → 277 |
| irrelevant injections | 224/304 → 224/304 | 198/277 → 198/277 |
| useful-context tokens | 20.0% → 20.1% | 21.4% → 21.5% |

That is what this change is for.

## What ran here

| Arm | Where | Files |
|---|---|---|
| lexical (keyword-only) | this machine, before **and** after | [`results.json`](results.json) · [`report.md`](report.md) |
| hybrid @ 0.40 and @ 0.50, floor **on** | the host that reaches the embedder | [`results-hybrid-ct.json`](results-hybrid-ct.json) · [`report-hybrid-ct.md`](report-hybrid-ct.md) |
| hybrid @ 0.40 and @ 0.50, floor **off** | same host, same commit | [`results-hybrid-ct-floor-off.json`](results-hybrid-ct-floor-off.json) · [`report-hybrid-ct-floor-off.md`](report-hybrid-ct-floor-off.md) |

No embedding endpoint is reachable **from the interpreter** on the machine the
change was written on — the same condition recorded on 2026-09-21 and
2026-09-22, where `curl` reaches the embed host and the Python process does
not. The rig refuses to fabricate query vectors, so the default was picked
from *recorded* per-result cosines (below) and the hybrid arms were then run
on a host whose interpreter reaches real `bge-m3` (Python 3.11.2, this
branch's tree, once at the shipped 0.85 and once at 0.0). Those runs are the
measured numbers quoted from here on; the calibration stands as what was
predicted before them.

## Calibration: choosing the floor from recorded rows

The two hybrid arms were recorded per result, on the measurement host against
real `bge-m3`, with each row's raw cosine, its normalized BM25 (`null` when the
keyword arm never retrieved it) and the fixture's relevance label. Those 581
rows travel with this directory as
[`calibration-rows.json`](calibration-rows.json);
[`calibration.py`](calibration.py) is the arithmetic over them and reproduces
every table in this section with no store, no embedder and no network.

Per question, each delivered row's cosine was expressed as a ratio of the best
cosine in the same slate. Split by label, at the 0.40 floor:

| label | n | min | p10 | p25 | median |
|---|---:|---:|---:|---:|---:|
| relevant | 52 | **0.884** | 0.932 | 0.981 | 1.000 |
| tolerated | 28 | 0.748 | 0.800 | 0.930 | 0.969 |
| irrelevant | 184 | 0.675 | 0.773 | 0.851 | 0.920 |

The distributions overlap — an irrelevant result at the median sits at 0.920 of
the top match — so a relative floor is a blunt instrument here and the sweep
below is what decides how far it can be pushed. The floor cannot be set on the
irrelevant side of that overlap without paying recall.

### The sweep, bracketed

A floor applied before fusion changes more than the rows it removes, and the
recorded rows cannot say how the re-ranked slate refills. Two simulations
bracket it:

- **cut A** — every delivered row below `floor x best` leaves the vector arm.
  The upper bound on the cut: it ignores that a row the keyword arm *also*
  retrieved is re-admitted by that arm, since a candidate needs only one arm.
- **cut B** — only rows the vector arm alone admitted leave the candidate set.
  The lower bound: it ignores that freed slots can refill from candidates
  ranked outside the delivered slate.

Hybrid @ 0.40, cut A (the conservative side — this is what the default is set
from):

| floor | delivered | recall@k | injections | est. tokens/q | est. useful | relevant lost |
|---:|---:|---:|---:|---:|---:|---|
| 0.00 (today) | 304 | 52/52 (100%) | 224 | 502.7 | 20.1% | — |
| 0.80 | 269 | 52/52 (100%) | 192 | 439.4 | 23.0% | — |
| **0.85** | **254** | **52/52 (100%)** | **178** | **413.3** | **24.4%** | **—** |
| 0.88 | 235 | 52/52 (100%) | 161 | 381.5 | 26.4% | — |
| 0.90 | 221 | 50/52 (96.2%) | 149 | 359.6 | 27.6% | 2 |
| 0.95 | 161 | 44/52 (84.6%) | 98 | 264.4 | 34.1% | 8 |

Hybrid @ 0.40, cut B:

| floor | delivered | recall@k | injections | est. tokens/q | est. useful | relevant lost |
|---:|---:|---:|---:|---:|---:|---|
| 0.00 (today) | 304 | 52/52 (100%) | 224 | 502.7 | 20.1% | — |
| 0.80 | 274 | 52/52 (100%) | 195 | 448.7 | 22.5% | — |
| **0.85** | **259** | **52/52 (100%)** | **181** | **422.6** | **23.9%** | **—** |
| 0.88 | 249 | 52/52 (100%) | 172 | 405.1 | 24.9% | — |
| 0.90 | 245 | 52/52 (100%) | 168 | 399.0 | 25.3% | — |
| 0.95 | 213 | 52/52 (100%) | 137 | 349.5 | 28.9% | — |

At the 0.50 floor the same shape holds: cut A goes 277 → 233 delivered and
198 → 157 injections at 0.85, with the same two relevant results lost at 0.90
and the same eight at 0.95.

Token columns are **estimates** — each question's recorded payload scaled by
the share of its rows that survive — because the recorded rows carry no
per-row token count. The measured payload comes from the re-run.

### Why 0.85

**0.884 is the weakest relevant result in the set**, and 0.85 is the last round
value below it. The two values above it are not free: 0.88 sits 0.004 from
losing a relevant result on a 62-question fixture, which is not a margin, and
0.90 loses two outright. The margin at 0.85 (about 4 points of ratio, ~10% of
the gap to the next relevant row at 0.911) is the same shape of headroom the
keyword arm's floor keeps — 0.35 against a weakest true hit of 0.387.

The cost side is modest and real: on cut A, 50 of 304 delivered results go
(−16%), injections fall 224 → 178, estimated payload 503 → 413 tokens per
question, estimated useful-context 20.1% → 24.4%. On cut B, 45 rows and the
same direction. Not the 25% cut the keyword arm saw, because the arms overlap:
128 of the 304 rows were retrieved by *both* arms and the keyword arm keeps
admitting them.

## Which questions this risks

Every relevant result that sits far below the top of its own slate. These are
the rows a stricter floor would take, in order:

| ratio | question | ask | relevant record | cosine | BM25 |
|---:|---|---|---|---:|---:|
| 0.884 | `chg-06-p` | What envelope format is live for Harborlight customers? | `hl-schema-v3-new` | 0.528 | 0.118 |
| 0.886 | `chg-02` | Did the Harborlight retention policy change, and from what to what? | `hl-retention-old` | 0.566 | 0.083 |
| 0.911 | `chg-03` | What was the Harborlight retention window before it changed? | `hl-retention-old` | 0.628 | 0.134 |
| 0.924 | `chg-07` | What happened to Harborlight schema version 2? | `hl-schema-v3-new` | 0.595 | 0.359 |
| 0.927 | `rel-08` | What shipped in the latest Harborlight release? | `hl-release-notes-4-2-0` | 0.624 | 0.219 |
| 0.932 | `rel-01` | What is the current Harborlight release? | `hl-release-4-2-0` | 0.670 | 0.182 |
| 0.938 | `rel-09` | What is the current release state of Harborlight? | `hl-release-4-2-0` | 0.660 | 0.182 |
| 0.948 | `chg-06` | Which schema version does Harborlight production run? | `hl-schema-v3-new` | 0.671 | 0.370 |

Two things to read here. First, the shape is consistent: a changed-decision
question wants *both* sides of the change, and the side that is no longer
current is always the weaker vector match — `chg-02` and `chg-03` both want
`hl-retention-old`, which the query phrasing matches least. A floor set too
high stops answering "what did it used to be".

Second, **every one of these rows also carries a BM25 score**, so the keyword
arm retrieved them too and re-admits them independently of this floor. That is
why cut B loses nothing even at 0.95, and it is the safety margin the arms buy
each other. The default is still set from cut A: the row is only re-admitted if
the keyword arm's own relative floor keeps it, and that is not something the
recorded rows can settle.

## The lexical arm is unchanged, and that is measured

The change is vector-only, so the keyword arm must not move at all. It did not.
The rig was run on this branch on this machine and compared **per question**
against the recorded run of the previous change (same machine, same fixture,
the tree this branch forked from):

| Metric | before (`../relevance-slate-not-limit-2026-09-22`) | after (this run) |
|---|---:|---:|
| Results delivered | 200 | 200 |
| Relevant-hit recall@k | 49/52 (94.2%) | 49/52 (94.2%) |
| Top-1 correct | 32/48 (66.7%) | 32/48 (66.7%) |
| Irrelevant injections | 128/200 (64.0%) | 128/200 (64.0%) |
| Same-file repeats | 2/200 | 2/200 |
| Project-isolation violations | 18/153 | 18/153 |
| Correct abstention | 2/14 | 2/14 |
| Recency trap ranked first | 6/17 | 6/17 |
| Useful-context tokens | 6140/23389 (26.3%) | 6140/23389 (26.3%) |
| Payload tokens per question | 377.2 | 377.2 |

Every aggregate section of the results object compares equal, and **all 62
questions deliver the same records, in the same order, with the same
per-question score object** — zero differences. `results.json` and `report.md`
here are that run.

The measurement host says the same thing independently: its two hybrid runs
(floor off, floor on) carry the lexical arm unchanged at 200 delivered, 128
injections, recall 49/52, same-file 2/200, isolation 18/153. Its payload count
for that arm is 375.6 tokens per question rather than 377.2 — the rendered
payload contains each hit's file path and the two machines' temporary store
directories are different lengths, which is the whole of the difference. Same
slate, same records, same order.

## Hybrid arms — measured, floor off → on

Both columns are the same commit on the same host, the only difference being
`vector_relative_floor`: `0.0` then the shipped `0.85`. 62 questions, `top_k`
5, real `bge-m3` (1024-dim), 3 timing passes.

| hybrid @ cosine floor 0.40 | floor off | floor 0.85 | |
|---|---:|---:|---|
| results delivered | 304 | **262** | −14% |
| relevant-hit recall@k | 52/52 (100%) | 52/52 (100%) | unchanged |
| answerable questions with a relevant hit | 48/48 | 48/48 | unchanged |
| top-1 correct | 37/48 | 37/48 | unchanged |
| irrelevant injections | 224/304 (73.7%) | **186/262 (71.0%)** | −38 results |
| redundant results | 15/304 (4.9%) | 11/262 (4.2%) | −4 results |
| repeat slots for one source file | 0/304 | 1/262 | +1 result |
| project-isolation violations | 15/240 (6.3%) | 11/198 (5.6%) | −4 results |
| correct abstention (no-answer) | 0/14 | 0/14 | unchanged |
| recency trap ranked first | 5/17 | 5/17 | unchanged |
| useful-context tokens | 6253/31125 (20.1%) | **6253/26600 (23.5%)** | +3.4 pt |
| payload tokens per question | 502.0 | **429.0** | −15% |
| p50 / p95 latency (ms) | 86.4 / 104.7 | 85.9 / 101.8 | noise |

| hybrid @ cosine floor 0.50 | floor off | floor 0.85 | |
|---|---:|---:|---|
| results delivered | 277 | **240** | −13% |
| relevant-hit recall@k | 52/52 (100%) | 52/52 (100%) | unchanged |
| top-1 correct | 36/48 | 36/48 | unchanged |
| irrelevant injections | 198/277 (71.5%) | **164/240 (68.3%)** | −34 results |
| redundant results | 13/277 (4.7%) | 10/240 (4.2%) | −3 results |
| repeat slots for one source file | 0/277 | 1/240 | +1 result |
| project-isolation violations | 11/220 (5.0%) | 11/183 (6.0%) | rate up, count flat |
| correct abstention (no-answer) | 2/14 | 2/14 | unchanged |
| useful-context tokens | 6269/29105 (21.5%) | **6269/24994 (25.1%)** | +3.6 pt |
| payload tokens per question | 469.4 | **403.1** | −14% |

**The useful-token count is identical on both sides** — 6253 and 6269 — so the
payload shrank by exactly the part of it that was not the answer. And **no
question regressed**: compared individually across relevant hits found, rank of
the first relevant hit and top-1, not one of the 62 got worse in either arm.

Slate sizes, questions by results returned (@ 0.40):

| results | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---:|---:|---:|---:|---:|---:|
| floor off | 0 | 0 | 2 | 0 | 0 | 60 |
| floor 0.85 | 0 | 6 | 3 | 6 | 3 | 44 |

60 of 62 questions used to get exactly five results whatever the store had to
say; 44 do now. That is the same shape the keyword arm showed, on the mode that
actually ships.

The one number that moves the wrong way is the single same-file repeat. The
per-file cap defers rather than discards, so a slate shortened by this floor
can be backfilled by a capped second chunk of a file already in it — the cap
working as designed, surfacing only now that slates are short enough to
backfill at all.

### Where the measurement landed against the bracket

262 delivered @ 0.40 (240 @ 0.50) sits **just outside the bracket on the
delivered side** — 3 results above cut B's 259 (and 237), where cut A predicted
254 (233). The prediction that mattered held exactly: **nothing relevant was
lost**, which both cuts agreed on at 0.85, and the measured cut (42 and 37
results) is within 20% of the 45–50 and 40–44 the bracket spanned.

The miss is in the direction cut B names as its own blind spot: it assumed a
freed slot stays empty, and in fact freed slots refill from candidates that
were ranked outside the delivered five. So the true cut is slightly *smaller*
than even the lower-bound simulation, and both bounds were conservative in the
same direction rather than straddling. Worth carrying into the next
calibration of this shape: on a `top_k`-limited slate the refill is not a
second-order effect.

## Still open after this change

- **Abstention is untouched, again and deliberately** — and now measured to be:
  correct abstention stays 0/14 at the 0.40 floor and 2/14 at 0.50, exactly as
  before. All 14 no-answer questions still return something at 0.40; they
  return *less* of it, which is a payload result and not an abstention one. A
  relative floor cannot abstain: its top candidate always clears it. The
  no-confident-match signal is the separate change for that, and it reports
  rather than selects.
- **Injections are still the majority of delivered results** at the calibrated
  floor — 186/262 (71.0%) at the 0.40 floor, 164/240 (68.3%) at 0.50. The
  remaining share is ranking and project isolation, not slate length.
- **Recency traps did not move at all** (5/17 either way in both hybrid arms,
  6/17 lexical), and project isolation improved only as a count, not as a rate.
  Both remain separate ranking work.
- **One fixture, one embedding model, one corpus size.** The ratio a relevant
  result carries against the best match is a property of the embedder and the
  corpus; a different model will want this re-checked, which is what
  `calibration.py` is for.
