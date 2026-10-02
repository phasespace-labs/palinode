# bench/ — ingestion & recall benchmark

A small, dependency-light harness (Python stdlib + the `palinode` package only) that measures
four properties of Palinode's memory pipeline on a fixed synthetic corpus:

1. **Cost per remembered fact** — model calls, approximate tokens, and wall-clock to ingest a
   corpus. Palinode makes **zero chat-LLM calls** on the ingest path and one embed call per changed
   section (skipped on unchanged content via SHA-256 dedup).
2. **Determinism** — ingest the same corpus N times from clean state and byte-diff the resulting
   memory state (source files + a normalized logical DB dump). Target: identical modulo timestamps.
3. **LLM-free recall latency** — p50/p95 for hybrid (vector + BM25, RRF) and keyword-only search,
   cold and warm. "LLM-free" means **no chat/synthesis model** is invoked at query time; hybrid
   search still costs exactly one *embedding* per query to build the query vector. The harness
   counts chat calls and embed calls separately and never sums them, so neither cost can be read
   as zero when it isn't. The keyword-only path measurably costs zero model calls of any kind.
4. **Degradation floor** — recall with the embedder **actively disabled** (every embed entry point
   torn down for the measurement, so the condition holds on any host) and with the API + index down
   (grep over the source files). Because files are the source of truth, recall never reaches zero.

Everything runs against a throwaway `PALINODE_DIR`; nothing touches a real store. The corpus is
synthetic fixture material (`my-project` / `other-project`, Alice / Bob) generated from a fixed
seed — no real memories.

## Run

```bash
# full suite → JSON results
python -m bench.run --size 60 --runs 5 --iters 20 --out results.json

# render a Markdown report from the results
python -m bench.report results.json > report.md
```

If a `bge-m3` embedder is reachable (e.g. the Docker Compose stack), the harness measures the
embedded/hybrid path. If not, it **degrades gracefully to keyword-only** and reports those numbers
honestly — which is itself axis 4.

## Standalone abstention evaluation

The abstention evaluation is intentionally separate from the four-axis runner while its protocol
is still evolving. It requires a real configured embedding endpoint and runs the real SQLite-vec,
FTS5, and hybrid-ranking paths against a throwaway store. It sweeps the per-arm relevance floor
across three corpus seeds, measuring query-level false positives for 26 no-answer queries alongside
20 answer-present controls per seed. It does not change production search defaults.

```bash
# complete observations + aggregate summaries
python -m bench.abstention --out abstention.json

# reviewable aggregate table
python -m bench.abstention --format markdown --out abstention.md
```

The pinned query shapes are natural-language questions, short keywords, absent identifiers/codes,
exact-topic controls, and natural-language paraphrase controls. Each false-positive observation
records both the fused score exposed to callers and the underlying raw cosine when available.

## Relevance & abstention — what lands in the payload, and what should not

`relevance/` measures the other half of the abstention question: not only "does
search decline when it should", but "when it answers, how much of what it hands
the agent is the answer". Both retrieval modes are scored on one versioned,
sanitized fixture of 39 synthetic records and 62 labelled questions across four
classes — current release state, rejected approach and reason, changed decision
(old versus new, where recency alone must not settle it), and no-answer /
unrelated-project — plus held-out paraphrases and exact-identifier variants.

The corpus carries the noise the failure reports name, not just the answers:
old context notes that still read as present tense, auto-generated `See also`
footers that get their own chunk (so a footer-only hit is reachable rather than
hypothetical), near-duplicate restatements, and a second project that shares the
first one's vocabulary.

```bash
python -m bench.relevance --out results.json --report report.md
python -m bench.relevance --coverage-only        # fixture gate, no store needed
python -m bench.relevance --thresholds 0.4,0.5   # the shipped MCP and API floors
```

Per question and in aggregate, for the keyword-only and the hybrid arm:
relevant-hit recall@k, irrelevant injections, footer-only hits, redundant
results (both near-duplicate records and repeat slots for one file),
project-isolation violations, correct abstention versus inappropriate confident
match, and whether a retired value or an undecided proposal ranked first.
Useful-context tokens and p50/p95 latency are measured **through the payload the
client actually receives** — `_enrich_with_snippets` plus
`palinode.mcp._format_results`, not a reconstruction.

The hybrid arm needs a real embedding endpoint. When none is reachable the
lexical arm runs alone and the hybrid rows are printed as **NOT RUN** with the
reason; synthetic vectors are never substituted. Production defaults are never
changed — the shipped floors are recorded, not tuned.

The recorded baseline lives in
`results/relevance-abstention-baseline-2026-09-20/`.

```bash
python -m pytest tests/test_bench_relevance.py -q   # no model, no network
```

## Prospective-trigger phrasing measurement

`trigger_phrasing.py` answers a question the trigger thresholds had only been
guessed at: does *any* of the proposed options separate a prompt that is about a
trigger from one that is not? The corpus (`trigger_phrasing.yaml`, versioned,
entirely fictional) holds 14 triggers written three ways — third-person
"User is asking how to…" as the docs teach, the same intent in the words a user
would type, and 3–4 user-worded phrasings scored on their max — plus stratified
prompts per trigger (on-target terse, on-target verbose/session-handoff-shaped,
near-miss off-target, unrelated off-target). Every prompt is scored against
every trigger, so the labels are per (prompt, trigger) pair and a fire on
someone else's trigger counts as a false fire, not a near miss.

Scores come from the real embedder, the real `store.add_trigger` /
`store.check_triggers` path and the shipped distance-to-score conversion; the
lexical arm is a real FTS5 index scored with the product's own BM25
normalization. It reports score distributions, AUC, best-threshold
precision/recall and fire / false-fire rates at candidate thresholds, for
absolute floors, a relative "must lead the runner-up" rule and a vector-OR-BM25
hybrid. It changes no production default and refuses to run without an
embedding endpoint rather than inventing vectors.

```bash
python -m bench.trigger_phrasing --out results.json
python -m bench.trigger_phrasing --format markdown --out tables.md
```

Latest run: `results/trigger-phrasing-2026-09-20/` (report, generated tables,
raw observations).

## Current-state recall — from memory transitions to agent decisions

`current_state/` measures the thing retrieval scores cannot: after a record is
replaced, retracted, archived, consolidated or expired, does the delivered
context let an agent act correctly? Four stages are scored **separately**, so a
wrong answer is attributed rather than counted:

1. **Detection** — did the arm surface the correction or the conflicting record?
2. **Disposition** — current / contested / unknown, decided correctly?
3. **Presentation** — did the text keep a retired value out of the current slot,
   keep both sides of a conflict, and carry the qualifiers?
4. **Behavior** — did a reader consuming only that text choose right, or abstain?

```bash
python -m bench.current_state --full --out results.json --report report.md
python -m bench.current_state --slice 10              # the bounded CI shape
python -m bench.current_state --coverage-only         # just the family gate
python -m bench.current_state --require-coverage ...  # gate → exit code
```

**The corpus** (`episodes.yaml`, versioned) is an event stream, not a Q&A set:
each episode is a sequence of saves, explicit replacements, retractions,
archives, consolidation passes, clock advances, delayed imports and restores,
followed by questions with a **deterministic oracle** — the expected
disposition, the expected value, the retired values that must never appear as
current, and the evidence refs. Every family carries a positive control (the
transition happened) and a negative one (nothing changed), and the coverage
gate fails if either side of any family disappears. A held-out split is derived
at load time by substituting unseen project names and paraphrasing the
questions under a fixed seed.

Nothing is simulated. Episodes replay through the real `index_file` pipeline,
the real archive/retract operations, the real deterministic executor and the
real consolidation runner — only the runner's proposal seam is replaced, with a
constant, so the corpus contains no model output.

**The arms** all answer the same questions against the same store from the same
seed retrieval, under one token accounting (`packing.estimate_tokens`):

| Arm | What it delivers |
|---|---|
| `baseline` | Top-k hits rendered from the **raw file** — pre-projection semantics. |
| `projection` | The same hits rendered from the indexed current-text projection. |
| `bounded_evidence` | Bounded evidence around each hit plus the resolution policy. |
| `bundle` | The whole bounded-resolution operation under the per-turn budget. |
| `matched_budget` | `baseline` with top-k raised until it costs what the bundle cost. |

Two more controls: **raw-evidence retention versus consolidation** (matched
episodes, one arm never consolidates) and **repeated consolidation** (three
cycles plus a delayed import and an explicit restore).

**The reader** is a rule-following program, not a model — pick the value marked
current; if contested, abstain and list the sides; if unknown, abstain — so
"agent behavior" is a measured number. Unmarked hits have no markers to follow,
so the reader does what an agent does with a ranked list and takes the top hit,
which is how an unmarked arm scores a stale-current answer at all. A
model-backed reader exists behind `--llm-reader`, off by default; it reads its
endpoint and model from `PALINODE_BENCH_LLM_URL` / `PALINODE_BENCH_LLM_MODEL`
(no host is compiled in), runs over a family-stratified sample, and reports
**no number at all** if any scored family had zero coverage.

**Hard invariants**, asserted rather than scored: no read arm moves `HEAD` or
dirties the tree; no delivery carries a hidden record's title or content; no
mechanical case presents a retired value as current; and every store, rebuilt
from its files alone with the index deleted, re-derives the same delivery at
the same source revisions with a receipt that names them.

**Release fixtures** run the *actual* shipped `UserPromptSubmit` hook script
against a live API server on an ephemeral port, so what the reader sees is what
a session would have been handed.

```bash
python -m pytest tests/test_bench_current_state.py -q          # bounded slice
python -m pytest tests/test_bench_current_state.py -q -m slow  # the full corpus
```

## Operating-numbers sweep

`perf.py` measures what `docs/PERFORMANCE.md` publishes: search latency p50/p95,
index throughput, RAM and disk across chunk-count targets.

```bash
python -m bench.perf --sizes 1000,10000,50000 \
  --label "your box, stated plainly" --synthetic-vectors --markdown
```

`--synthetic-vectors` swaps the embedder for deterministic hash vectors. The write
path, the vector table and the search path stay real, so latency and throughput are
valid; only vector *content* is fake, so **recall quality is not measured** and the
module never reports one. Omit the flag to run against a real embedder — the rig
refuses rather than silently degrading if none is reachable, and aborts if any scale
point indexes zero vectors.

## Layout

| File | Purpose |
|---|---|
| `corpus.py` | Deterministic synthetic corpus generator (pure function of `(seed, size)`). |
| `harness.py` | Ingest, state-fingerprint, and recall-latency measurement primitives. |
| `run.py` | End-to-end orchestrator (the four axes) + CLI. |
| `report.py` | Renders a results JSON object as a Markdown report. |
| `abstention.py` | Standalone no-answer/control threshold sweep + JSON/Markdown output. |
| `relevance/` | Relevance & abstention: labelled fixture, per-question scorer, both retrieval modes, delivered-payload accounting. |
| `trigger_phrasing.py` | Labelled prospective-trigger separation: description styles, absolute / relative / hybrid decision rules. |
| `current_state/` | Current-state recall: event corpus, five arms, deterministic reader, invariants. |
| `perf.py` | Scale sweep behind `docs/PERFORMANCE.md` — latency, throughput, RAM, disk at 1k/10k/50k chunks. |

## Tests

```bash
python -m pytest tests/test_bench_harness.py -q
python -m pytest tests/test_bench_abstention.py -q
python -m pytest tests/test_bench_relevance.py -q
python -m pytest tests/test_bench_trigger_phrasing.py -q
python -m pytest tests/test_bench_perf.py -q
```
