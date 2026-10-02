# Calibration: the no-confident-match verdict

Same rig, same corpus (v1) and question set (v1) as
[`../relevance-abstention-baseline-2026-09-20`](../relevance-abstention-baseline-2026-09-20)
and the footer re-measurement next to it. Two things are new in the working
tree this ran against:

1. Every delivered result now carries its **pre-fusion arm scores** —
   `raw_score` (cosine) and `keyword_score` (normalized BM25) — and the rig
   records both per result, so a verdict is re-derivable from a results file
   without a store. The baseline recorded only the fused score, which is a
   rank and cannot be calibrated against.
2. A delivery carries a **confidence verdict** (`confident` / `weak` /
   `none`) computed from those arm scores, and an opt-in switch
   (`search.abstain_on_no_confident_match`, default **off**) that empties the
   slate on the MCP surface when the verdict is `none`. The switch is the
   second arm below.

## What ran, and on which host

Three files. **`results-hybrid-ct-recalibrated.json` is the run of record**:
all six arms, on a host that reaches a real bge-m3, against the tree as it
now stands (corroboration included, and the merged `origin/main` — so
the slate-filling change (keyword-only floor and per-file cap) is in it too). Quote that one.

| Arm | `results.json` (macOS, no embedder) | `results-hybrid-ct.json` (before corroboration) | `…-recalibrated.json` (run of record) |
|---|---|---|---|
| lexical (keyword-only) | ran | ran | ran |
| lexical + abstain | ran | ran | ran |
| hybrid @ 0.40 | **NOT RUN** | ran | ran |
| hybrid @ 0.40 + abstain | **NOT RUN** | not present | ran |
| hybrid @ 0.50 | **NOT RUN** | ran | ran |
| hybrid @ 0.50 + abstain | **NOT RUN** | not present | ran |

The first two files are kept because they are the evidence: `results.json`
is where the keyword marks were calibrated with no embedder in reach, and
`results-hybrid-ct.json` is the run that showed the carried cosine marks
calling three no-answer questions confident — the measurement that produced
the corroboration rule.

Two things make columns non-comparable across the files, both stated rather
than smoothed over:

- **Host.** The lexical arm reaches the confident mark on 26 answerable
  questions on macOS and 25 on the Linux host — one question across the mark.
  A one-question difference in a lexical column is a host difference.
- **Tree.** The recalibrated run is post-merge, so it includes the slate-filling change
  (`lexical_fts_threshold`, `max_chunks_per_file`). That changes which rows
  are *delivered*, so every delivered-slate metric moved for a reason
  unrelated to this change: the lexical arm now delivers 200 results rather
  than 268, at 94.2 % recall rather than 92.3 %. Do not read those deltas as
  the verdict's doing — the verdict never selects.

## The verdict's confusion matrices

Measured, all six arms, run of record. The switch does not change a verdict —
it acts on one — so each abstain arm reads identically to its own default arm
and is not repeated here.

### lexical (keyword-only)

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 25 | 25 | 0 |
| weak | 22 | 21 | 8 |
| none | 1 | 0 | 6 |

### hybrid, both floors (0.40 and 0.50 read identically)

| Verdict | answerable | of those, slate held a relevant record | no-answer |
|---|---:|---:|---:|
| confident | 41 | 41 | 0 |
| weak | 7 | 7 | 12 |
| none | 0 | 0 | 2 |

- **Zero confident wrong answers, in both modes.** No no-answer question
  reaches the confident mark — the measured failure the signal exists to
  catch, and the one the pre-corroboration run failed in hybrid.
- **Every confident delivery held a relevant record** — 25 of 25 lexical,
  41 of 41 hybrid.
- **One answerable question reads `none`, and only in lexical** (`chg-04-p`,
  best keyword 0.124): its slate contained no relevant record at all, so the
  verdict is right about that delivery, which failed for a ranking reason
  this change does not touch. In hybrid, no answerable question reaches
  `none`.

**Before corroboration, the same hybrid data read:** confident 42 answerable
/ **3 no-answer**, weak 6 / 9, none 0 / 2 (`results-hybrid-ct.json`). Those
three are the reason the rule exists. The re-run reproduced the replay
prediction exactly.

## Where the marks come from

`keyword` marks — confident ≥ 0.22, weak ≥ 0.13 — are calibrated here.

- **0.22** is above the highest normalized BM25 any no-answer question
  produced (0.207, `non-08`) and is reached by 26 of 48 answerable ones.
- **0.13** sits just below the lowest normalized BM25 the 54-pair threshold
  study (recorded in `SearchConfig`) measured for a genuine single-identifier
  hit, 0.131 — the known worst case for this arm. On this fixture it puts
  6 of 14 no-answer questions at `none` and only the one answerable question
  above. Moving it to 0.15 would take no-answer `none` to 11/14 but would also
  label five *correct top-1* answers `none`; 0.13 is the knee.

`vector` marks — confident ≥ 0.60, weak ≥ 0.50 — are unchanged, and come from
two measurements already in the tree: the 54-pair study (true match clears
0.5 in 98 % and 0.6 in 74 % of pairs; the same query's hardest wrong answer
clears 0.6 in 0 %), and the production case behind `describe_match` (32
searches whose answer was absent from the corpus, cosine 0.402–0.459).

**The hybrid run showed those marks do not hold on a whole corpus.** Three
no-answer questions reached a best cosine above 0.60:

| Question | best cosine | best keyword |
|---|---:|---:|
| `non-05` | 0.635 | 0.000 |
| `non-07` | 0.629 | 0.000 |
| `non-10` | 0.607 | 0.106 |

and six answerable questions sat below it:

| Question | best cosine | best keyword | top-1 relevant |
|---|---:|---:|---|
| `chg-06-p` | 0.597 | 0.185 | no |
| `rej-05-p` | 0.591 | 0.141 | yes |
| `idn-07` | 0.510 | 0.147 | yes |
| `idn-04` | 0.481 | 0.146 | yes |
| `idn-05` | 0.478 | 0.158 | yes |
| `idn-03` | 0.460 | 0.135 | yes |

The two groups **overlap in cosine and separate cleanly in keyword**: every
one of the three false confidents is a query the keyword arm barely registers
(0.000, 0.000, 0.106 — all under its 0.13 weak mark), and every answerable
question in the band clears it. The 54-pair study's distractors were *the
same query's* wrong answers; a whole corpus contains topically-near records
that no query was written against, and those embed high.

### The candidate marks, and why corroboration won

| Rule | answerable confident (with a relevant record) | no-answer confident |
|---|---:|---:|
| cosine ≥ 0.60 (as shipped before) | 42 (42) | **3** |
| cosine ≥ 0.62 | 37 (37) | 2 |
| cosine ≥ 0.64 | 36 (36) | 0 |
| cosine ≥ 0.66 | 32 (32) | 0 |
| **cosine ≥ 0.60 + keyword ≥ 0.13 (shipped)** | **41 (41)** | **0** |
| cosine ≥ 0.60 + keyword ≥ 0.13, or cosine ≥ 0.64 alone | 42 (42) | 0 |

Raising the cosine mark alone reaches zero false confidents only at 0.64, and
costs six correct confident answers to get there. Requiring the keyword arm
to at least reach its own weak mark reaches zero while keeping 41 — five more
than the best cosine-only mark — and changes no carried threshold. The last
row keeps one more (the paraphrase `chg-04-p`, cosine 0.681 / keyword 0.124,
which corroboration demotes to `weak`) by adding a solo-cosine tier at 0.64:
a constant fitted to three points with 0.005 of headroom over `non-05`. That
is the numerology this calibration refuses on the keyword side, and the
lesson of the three false confidents is precisely that cosine alone is not
reliable about absence, so it was not taken.

The demotion is one-directional. The keyword arm produced no confident wrong
answer in either mode, and requiring *it* to be corroborated would make
`confident` unreachable in lexical mode, where there is no vector arm to ask.
It also only applies where both arms actually ran: a `hybrid=false` (vector
only) delivery has no second arm, so its vector verdict stands alone.

## What the switch costs, off → on, in all three modes

Measured, not projected. Hybrid @ 0.40 is what the MCP surface actually
sends, so that is the column the decision hangs on.

### lexical (keyword-only)

| Metric | off (default) | on | |
|---|---:|---:|---|
| Relevant-hit recall@k | 94.2 % | 94.2 % | unchanged |
| Answerable questions with a relevant hit | 46/48 | 46/48 | unchanged |
| Top-1 correct | 32/48 | 32/47 | the count is unchanged; one slate is gone |
| Correct abstention (no-answer) | 2/14 | **6/14** | the target |
| Irrelevant injections | 128/200 (64.0 %) | **110/181 (60.8 %)** | 19 fewer results, all of them wrong |
| Recency trap ranked first | 6/17 | 5/16 | one trap slate withheld |
| Payload tokens per question | 375.6 | **344.5** | −8.3 % |
| Useful-context tokens | 26.4 % | **28.7 %** | better |

Five slates are withheld: `non-05`, `non-06`, `non-07`, `non-10` — all
no-answer — and `chg-04-p`, the one answerable question, whose slate held no
relevant record in the first place. **The switch costs no correct recall on
this fixture.**

### hybrid @ cosine floor 0.40 — the MCP surface's own floor

| Metric | off (default) | on | |
|---|---:|---:|---|
| Relevant-hit recall@k | 100.0 % | 100.0 % | unchanged |
| Answerable questions with a relevant hit | 48/48 | 48/48 | unchanged |
| Top-1 correct | 37/48 | 37/48 | unchanged |
| Correct abstention (no-answer) | 0/14 | **2/14** | the target |
| Irrelevant injections | 224/304 (73.7 %) | **217/297 (73.1 %)** | 7 fewer results, all of them wrong |
| Payload tokens per question | 502.0 | 494.5 | −1.5 % |
| Useful-context tokens | 20.1 % | 20.4 % | marginally better |

Two slates are withheld, `idn-09` and `idn-10` — **both no-answer**, and
nothing else moves. In the shipped default mode the switch withholds two
wrong slates and nothing correct.

### hybrid @ cosine floor 0.50 — the REST default

Every column is identical with the switch on and off. The two questions the
verdict calls `none` at this floor already delivered nothing: the higher
cosine floor had removed their rows before the verdict was asked. **The
switch changes literally nothing here** — which is the honest reading, not a
recommendation for the floor.

The wider point the three tables make together: the stricter the retrieval
floor, the less an abstention switch has left to do. It earns its keep where
recall is loosest, and it is off by default because a 62-question fixture on
a 39-record corpus cannot license withholding a store's contents in general.

## The keyword arm under-rates exact identifiers

On the keyword-only arm all eight answerable identifier questions land on
`weak` (best keyword 0.135–0.209) — never `confident`, and never `none`.
Normalized BM25 is `|bm25| / 25`, so a score grows with the *number* of
matching query terms: a single-token identifier caps low however exact it is.
`SearchConfig`'s docstring records the same defect from the other side.

In hybrid, four of the eight (`idn-01`, `idn-02`, `idn-06`, `idn-08`) do
reach `confident`: the vector arm carries them over 0.60 and their keyword
score clears the 0.13 weak mark, so corroboration is satisfied rather than
blocking. The other four have a cosine of 0.46–0.51 and are weak on both
arms' own terms. This is the clearest argument for where the keyword weak
mark was put: at 0.15 it would have withdrawn corroboration from exactly the
hits that most deserve it. The two no-answer identifier queries retrieve
nothing in either mode and land on `none`.

## The marks are absolute on a scale that moves

Normalized BM25 carries IDF, so its magnitude drifts with corpus size. These
marks are calibrated on 39 records / 56 chunks. On a much smaller store the
same query scores lower and the verdict reads pessimistically (measured: a
four-record store puts every query below 0.13); on a much larger one it reads
optimistically. That is the same caveat `search.fts_threshold` carries, and
it is a reason to keep the verdict advisory — which, with the switch off, it
is.

Corroboration propagates that drift into the vector arm's verdict: on a store
of a handful of records, no hybrid delivery will be called `confident`,
however well it matches, because the keyword arm cannot reach 0.13 there
(measured on the test fixture: a three-record store scores an exact
identifier at 0.021, a twenty-record store at 0.105). The failure direction
is the safe one, and the cure is a keyword scale that does not move with
corpus size — a rescale of `search_fts`'s `/25`, which is a larger change
than this one and is not made here.
