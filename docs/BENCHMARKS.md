# Benchmarks

Palinode's memory layer measured on a public long-term-memory benchmark, with the
methodology and the losses stated. Everything here is reproducible from `bench/longmemeval/`;
raw per-question results live in `bench/results/`.

## LongMemEval (S)

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., ICLR 2025): 500 questions,
each with its own haystack of ~40 chat sessions (~115k tokens), across seven abilities —
single-session user / assistant / preference, multi-session reasoning, knowledge updates,
temporal reasoning, and abstention.

### What was measured

- **Memory layer (constant across rows):** every haystack session is saved as a dated markdown
  note through Palinode's normal indexer — no chat-LLM call at ingest — and recalled with
  hybrid search (BM25 + `bge-m3` vectors, RRF), top-10, one chunk per session. The answerer
  sees only those ten sessions plus the question date.
- **Evidence recall@10:** whether a session containing the answer is in the top-10. This is
  the memory layer's own number.
- **Accuracy:** upstream's judge, `gpt-4o-2024-08-06`, upstream's per-type prompts verbatim,
  temperature 0. Comparable to the numbers in the LongMemEval paper and to vendor tables that
  used the same judge.
- **Answerer (the only thing that varies between rows):** a different reader in each row,
  so the delta between rows isolates reader quality against a fixed retrieval ceiling.

### Results

| | evidence recall@10 | **A** local 30B | **B** Gemini 3 Flash | **C** GPT-5.5 | **D** GPT-4o |
|---|---|---|---|---|---|
| single-session-assistant (56) | 1.000 | 0.929 | 0.982 | 0.982 | 1.000 |
| single-session-user (64) | 0.938 | 0.812 | 0.891 | 0.891 | 0.938 |
| knowledge-update (72) | 1.000 | 0.514 | 0.875 | 0.958 | 0.847 |
| abstention (30) | — | 0.700 | 0.900 | 0.933 | 0.700 |
| multi-session (121) | 0.992 | 0.306 | 0.736 | 0.826 | 0.620 |
| single-session-preference (30) | 0.933 | 0.233 | 0.733 | 0.733 | 0.433 |
| temporal-reasoning (127) | 0.984 | 0.276 | 0.732 | 0.866 | 0.732 |
| **overall (500)** | **0.981** | **0.482** | **0.812** | **0.882** | **0.758** |

Row answerers: **A** `qwen3-coder-30b-a3b-instruct` (4-bit, local, LM Studio) · **B**
`gemini-3-flash-preview` (thinking off) · **C** `gpt-5.5` via the Codex CLI (`codex exec`, read-only
sandbox; its reported token count includes Codex's own system prompt) · **D** `gpt-4o-2024-08-06`
— the reader Zep and Supermemory report with, so row D is the directly comparable number. Embedder in all
rows: `bge-m3` via Ollama. Evidence recall is reported from row B (0.981); row A measured
0.951 on the same haystacks with the same retrieval — the difference is 13 questions that
fell back to keyword-only retrieval during a transient embedder outage, documented in
`bench/results/longmemeval-s-rowA-2026-08-27/`.

### Cost

Per question, mean: ingest **10–12 s** with **zero chat-LLM calls**; retrieval **0.4 s**;
answerer prompt **~22–24k tokens** (ten full sessions). Answer latency: B 2 s, C 6 s (p50).

### Reading the table

- **Retrieval is not the limiter.** The evidence is in the top-10 for 98 % of questions and
  for ≥99 % of multi-session and knowledge-update questions. What a comparison table reports
  is mostly the reader: the same memory layer scores 0.48 with a local 30B model, 0.76 with
  GPT-4o, 0.81 with Gemini 3 Flash, and 0.88 with GPT-5.5.
- **Where we lose, with the evidence in hand:** multi-session and temporal reasoning sit at
  0.73 (B) / 0.83–0.87 (C) despite ≥0.98 recall — the reader has the sessions and still
  fails to combine or compute. Preference (0.73 in both B and C, recall 0.93) is the one type
  where a stronger reader didn't help: the rubric wants the answer *personalised* to facts in
  the sessions, and neither reader reliably does that from ten raw transcripts. Fixing either
  is not a retrieval change. Abstention (0.90 / 0.93) is graded by whether the reader
  declines; retrieval can't help or hurt it.
- **Token cost is the lever we haven't pulled.** Ten full sessions is ~23k tokens per answer.
  A tiered read (abstract → overview → full) or a smaller top-k is the obvious next row.

### Against published numbers

Row D is the configuration other systems report with on LongMemEval_S — `gpt-4o` as both
reader and judge — so it is the only row that belongs in the same table as theirs. Vendor
numbers are self-reported and single-run; ours is too.

| system | reader | overall |
|---|---|---|
| Full-context GPT-4o, no memory system (LongMemEval paper) | GPT-4o | 0.602 |
| Zep | GPT-4o | 0.712 |
| **Palinode, memory layer only (row D)** | GPT-4o | **0.758** |
| Supermemory | GPT-4o | 0.816 |
| Oracle retrieval (paper upper bound) | GPT-4o | ~0.87–0.92 |

Two things to keep in view. Zep and Supermemory report after an extraction + consolidation
step; row D is raw session transcripts with no write-time processing — Palinode's own
consolidation layer is not in the table yet. And the reader dominates the number: rows B and
C, on the same memory, land at 0.81 and 0.88, level with or above the best published rows on
matched-class readers (Supermemory reports 0.852 with Gemini 3 and 0.846 with GPT-5).
Row E below puts Palinode's own extraction + consolidation into the comparison.

### Row E — the production write path (extraction + consolidation)

Rows A–D measure the memory layer *before* any write-time intelligence. Zep and Supermemory
report *after* an extraction + consolidation step. Row E is the apples-to-apples row: it replays
what an agent does in production at the end of every session, including Palinode's real
session-end and consolidation paths.

**Write path, per question, sessions in chronological order.** (1) An extraction model
(`gemini-3-flash-preview`, thinking off) plays the agent at session end and produces the
`session_end` payload: a summary, dated facts about the user, preferences. This is a chat-LLM
call at ingest — ~50 per question, reported below; rows A–D made none. (2) The payload goes
through Palinode's real `session_end` function, in-process, with the clock set to the session's
date: a dated `daily/` note tagged `project/user` and an indexed twin, exactly as production
writes them. (3) The facts are appended to a seeded `projects/user.md` profile, one section per
session, and tagged with the real fact-id bootstrap so the executor can address them. (4)
Everything is indexed through the canonical indexer. (5) *E1 only:* the real `run_consolidation`
runs over the question's notes with the same Gemini model behind the runner's `llm_fn` seam; it
proposes KEEP / UPDATE / MERGE / SUPERSEDE / ARCHIVE / RETRACT against the profile's facts and
the executor applies them — SUPERSEDE strikes the stale fact through and adds its successor;
compacted daily notes move to `archive/`, still indexed.

**Inference path** is rows A–D's, over a much smaller store: hybrid BM25 + `bge-m3` (RRF) over
notes, twins, and profile sections; over-fetch 20 and keep the top-10 *distinct* excerpts (the
daily note and its indexed twin are the same text); reader `gemini-3-flash-preview` on answer
prompt v2; judge `gpt-4o-2024-08-06`. Reader and judge are held identical to row B so the delta
is attributable to the write path.

Sub-rows: **E0** extraction only (compact notes instead of transcripts, no consolidation) ·
**E1** E0 + consolidation · **E1noarch** E1 with the production `consolidation.allowed_ops`
knob narrowed to KEEP/UPDATE/MERGE/SUPERSEDE (the controlled test of the ARCHIVE finding
below) · **E1xv2** E1noarch with extraction prompt v2, the *event ledger* (one bullet per
countable event with amount and absolute date; every member of an assistant-recommended list).
A planned E1+raw sub-row (transcripts indexed alongside notes) was dropped: the ledger prompt
recovered the extraction losses it was designed to catch, by a better mechanism.

#### Results — 100-question stratified subset

The same 100 questions (every 5th) as the row B-v2 control, so every cell below is on identical
questions under an identical reader and judge. B-v2 on this subset: **0.750**.

All Gemini-read rows use the same reader (`gemini-3-flash-preview`, prompt v2) and judge:

| type (n) | B-v2 (raw transcripts) | E0 | E1 | E1noarch | E1xv2 |
|---|---|---|---|---|---|
| single-session-user (13) | 0.923 | 1.000 | 0.923 | 1.000 | 0.923 |
| single-session-assistant (11) | 1.000 | 0.818 | 0.818 | 0.727 | 0.818 |
| single-session-preference (6) | 0.500 | 0.667 | 0.500 | 1.000 | 0.667 |
| knowledge-update (14) | 0.857 | 0.929 | 0.857 | 0.929 | 0.857 |
| multi-session (25) | 0.640 | 0.760 | 0.640 | 0.800 | 0.720 |
| temporal-reasoning (25) | 0.680 | 0.760 | 0.720 | 0.680 | 0.720 |
| abstention (6) | 0.667 | 0.667 | 0.833 | 0.833 | 0.833 |
| **overall (100)** | **0.750** | **0.810** | **0.750** | **0.820** | **0.780** |
| evidence recall@10 | 0.968 | 0.989 | 0.989 | 0.989 | 0.989 |
| answer string in context | — | 0.553 | 0.521 | 0.543 | 0.564 |
| prompt tokens / answer | 21,850 | 2,782 | 2,708 | 2,731 | 3,803 |

#### The same write path, fully local

The compact-notes rows also run end to end on one consumer GPU — extraction, session-end
writes, hybrid recall, *and* the reader on an RTX 5090 (`Qwen3.8-27B` AWQ-INT4 under vLLM);
only the judge is a metered API call (~$0.15/run). ~80–90 s per question.

| type (n) | E0-local (extract v1) | E1noarch-local | E0-local (extract v2, ledger) |
|---|---|---|---|
| single-session-user (13) | 1.000 | 1.000 | 1.000 |
| single-session-assistant (11) | 0.455 | 0.545 | 0.909 |
| single-session-preference (6) | 0.333 | 0.667 | 0.833 |
| knowledge-update (14) | 0.714 | 0.714 | 0.786 |
| multi-session (25) | 0.720 | 0.680 | 0.880 |
| temporal-reasoning (25) | 0.840 | 0.800 | 0.800 |
| abstention (6) | 0.833 | 0.833 | 0.833 |
| **overall (100)** | **0.740** | **0.750** | **0.860** |

Two results carry the story. **The ledger extraction prompt is worth +12 points** (0.740 →
0.860; 14 wins / 2 losses head-to-head; assistant 5→10 of 11, preference 2→5 of 6,
multi-session 18→22 of 25): the misses it fixed were exactly the extraction losses in the
taxonomy below. And **a local reader over compact notes (0.860) outscores a frontier API
reader over raw transcripts (0.750)** — compare row A, where a local 30B reading ~23k-token
transcripts managed 0.482. Compact notes are what make local readers viable. Caveats as
everywhere here: single runs, 100-question subset, and not directly comparable to vendors'
published full-500 gpt-4o-reader numbers.

*Per-question rows in `bench/results/longmemeval-s-rowE*-subset100-*/`. A sixth
measurement — E1 + the v2 prompt with ARCHIVE still active — sits in
`…rowE1v2arch…` (0.805 on 87 judged; 13 questions lost to quota): the ledger prompt
recovered about half of ARCHIVE's damage even without the filter.*

**Cost, E1, per question (100 questions):** extraction 47.5 calls, 121k prompt + 15k
completion tokens, ~16 s at 8-way parallel; session-end writes + indexing ~45 s; consolidation
one call, 22 s; ingest 67 s in all; **reader prompt 2,708 tokens** against 21,850 for the raw
row. On Gemini 3 Flash pricing the whole write path is ≈ $0.04 per question — roughly the cost
of two raw-transcript answers. Two of the 4,748 sessions were refused by the extraction model's
content filter (`PROHIBITED_CONTENT`, ShareGPT material); they got a stub note saying nothing
was recorded, which is what production would have.

**Consolidation ops, E1 (100 questions):** 25,650 KEEP · 154 MERGE · 109 UPDATE ·
39 SUPERSEDE · 1,058 ARCHIVE · 9 RETRACT · **5 unmatched** (ids the model invented; the
executor dropped them) · 0 rejected. 98 of 100 profiles were compacted; on the other two the
model returned no parseable operations and the runner reports that as a quiet pass, not a
failure. SUPERSEDE is rare: the compaction prompt says "default to KEEP; SUPERSEDE requires
evidence", and it sees at most 6,000 characters of recent notes (about four), so most of the
evidence for an update has to come from the dated facts themselves.

#### Reading row E

- **Compact notes beat raw transcripts; consolidation gave the gain back.** E0 — session-end
  notes alone — scores **0.810** where the same reader over raw transcripts scores 0.750, at
  2,782 prompt tokens per answer instead of 21,850. E1 (E0 + the full consolidation pass)
  falls back to exactly B-v2's 0.750: 75 = 75, not a coincidence of averages — E1 wins 14
  questions B misses and loses 14 B gets. The write-time extraction is worth +6; the ARCHIVE
  behaviour documented below cost the same 6.
- **Retrieval is still not the limiter.** Evidence recall 0.989; every evidence session was
  in the top-10 for every multi-session question, and the profile was in the top-10 on 99 of
  100.
- **Where extraction loses and wins.** Multi-session is level (16/25 each) but not on the
  same questions: five misses are shared (the reader miscounts with the evidence in hand — 4
  citrus fruits for a gold of 3, "3 years 9 months" in a role); four are E1-only, each a
  countable detail that did not survive extraction (a backpack's purchase and arrival dates, a
  $500 workshop in a $720 total, a third dinner party); four are E1-only *wins*, where compact
  dated facts made a count easy that the reader got wrong from ten full transcripts.
  Single-session-assistant is the one type that regressed (0.818 vs 1.000): both misses are
  the extractor keeping one item from a list the assistant recommended (one hostel of three,
  one mindfulness site of several). Temporal reasoning (0.720 vs 0.680) and abstention (0.833
  vs 0.667) moved the other way — dated facts with resolved absolute dates, and less
  distracting context. The v2 *ledger* extraction prompt targets exactly these losses without
  touching the reader — and its effect turned out to be **reader-dependent**: +12 points for
  the local reader (table above), but 0.780 vs E1noarch's 0.820 under Gemini (E1xv2 column) —
  for a strong reader whose v1 extractions were already good, ~485 facts per question adds
  more noise than signal. Choose the extraction prompt for the reader that will consume it.
- **ARCHIVE is where consolidation loses.** On the questions where E0 (no consolidation) beat
  E1, the fact the reader needed had been ARCHIVEd by the compaction pass (1,058 ARCHIVEs
  across the E1 run, ~10.6 per ~300-fact profile): archived facts move to a history file whose
  `status: archived` frontmatter is excluded from default recall, so ARCHIVE is the only
  operation that removes information from the reader's world — a false positive is
  unrecoverable at read time. The compaction prompt's age rule ("stale > 60 days") is sound
  for project-status documents and a category error for facts about a person, and the
  benchmark's compressed timeline (a 2023 haystack compacted in one pass) triggers it
  maximally. E1noarch — the same pass with ARCHIVE/RETRACT filtered by the production
  `allowed_ops` config — ran as the controlled test and confirmed it: **0.820**, above both
  E1 (0.750) and E0 (0.810), with consolidation's useful ops intact (109 UPDATE, 143 MERGE,
  38 SUPERSEDE across 100 compacted profiles). The asymmetry to keep in view is that KEEP
  errs cheap (top-k ranking filters a useless fact) while ARCHIVE errs expensive (a deleted
  fact cannot be ranked back in).
- **Knowledge updates: level with raw transcripts at 0.857, with 39 SUPERSEDEs applied
  across the run.** The smoke question that motivated the fixes below — yoga "twice a week"
  superseded by "Tuesday, Thursday and Friday" — was answered wrongly (stale fact) until the
  profile was retrievable, and correctly once one SUPERSEDE had been applied. Whether
  consolidation is doing the work or the reader is resolving the conflict from two dated facts
  is what E0 (same notes, no consolidation) separates.

#### What the harness does that production leaves to the agent

Stated so the row is honest. The clock `session_end` stamps a note with is patched to the
haystack session's date. The fact bullets are appended to `projects/user.md` by the harness —
`session_end` itself appends only a one-line index entry to `projects/<p>-status.md`, which is
deliberately not seeded so consolidation targets the profile. `config.consolidation.keyword_map`
is set so the runner groups session-end entries under `project/user` (production notes carry
the ref in their body or via that same config). Evidence recall for summaries is defined by
tracing a hit to its session through the `Session ID` line `session_end` writes into every
entry, or the profile section's heading; *answer string in context* (the gold answer,
case-folded, verbatim in the reader's prompt) is reported alongside as the summary-era signal,
and is weak for bare numeric answers.

Two harness defects the smoke exposed, both fixed before the subset run and recorded in the
commit history: a single 300-bullet profile section never reached the top-10 (now one section
per session), and the profile's preamble was a deterministic `bge-m3` NaN input (a known embedder defect) that
made the indexer drop the whole file — for the first three smokes the profile was keyword-only.
Files that still trip that defect are re-indexed keyword-only and counted (`fts_only_files`; 0 so far).

### Reader sensitivity to the answer prompt

`gpt-4o-2024-08-06` scored **0.578** under the original answer instruction ("say clearly that
the information is not available in memory — do not guess"): it declined on all 30 abstention
questions and on 152 of its 183 other misses, with the answer-bearing session in the prompt for
143 of them. Re-asked with the same context under a softened instruction ("read all of them
carefully; only if none contain relevant information, say so") it answered those correctly. The
other readers were not affected: Gemini 3 Flash scored 0.760 vs 0.750 on the same 100-question
stratified subset under the two instructions. Row D therefore uses the softened prompt (v2);
rows A–C used v1. Both are in `bench/longmemeval/adapter.py`, versioned and recorded in each
run's metadata; the v1 gpt-4o run is kept in
`bench/results/longmemeval-s-rowD-v1prompt-2026-08-29/` so the effect is reproducible.

### Judge choice

Row A was originally judged with `gemini-2.5-flash` and re-scored with `gpt-4o-2024-08-06`
(`bench/longmemeval/rejudge`): agreement 0.950 on 500 items, with Gemini the stricter judge
(21 flips to correct, 4 to incorrect, concentrated on rubric-graded preference questions).
All numbers above are under the gpt-4o judge.

### Caveats

- Single run per row; no confidence intervals yet.
- LongMemEval's answer key is community-maintained; we used `longmemeval_s_cleaned.json`
  unmodified.
- The answerer prompt is a single generic instruction (v1 for rows A–C, v2 for row D — see
  *Reader sensitivity* above). No per-type prompting.
- Session-level chunks only in rows A–D: they measure the memory layer **before** any
  write-time intelligence — no extraction, no consolidation, no reranking. Row E is the
  production write path (`session_end` → project document → consolidation ops) on a 100-question
  subset; its full-500 run and its gpt-4o-reader variant (the directly comparable configuration
  to Zep / Supermemory) are the next rows.

### Reproduce

```bash
python -m bench.longmemeval.run --variant s --out bench/results/<name>        # needs LME_ANSWER_* / LME_JUDGE_*
python -m bench.longmemeval.rejudge bench/results/<name> --out bench/results/<name>/rejudge-gpt4o
```

See `bench/longmemeval/README.md` for endpoints, the supervisor for multi-hour runs, and the
fallbacks the harness applies when the embedder misbehaves.

## LongMemEval-V2 (small tier)

[LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) (Wu et al., 2026) is not V1 with
more questions. The haystack is **web-agent trajectories** — accessibility trees, actions and agent
thoughts from customized shopping / forum sites (`web`, 240 questions) and a ServiceNow-style
portal (`enterprise`, 211) — and the questions test static state recall, dynamic state tracking,
workflow knowledge, environment gotchas and premise awareness (`-abs` abstention traps). The small
tier gives every question in a domain the same 100-trajectory haystack (~18M tokens per domain).

### What was measured

- **Harness, reader, judge: upstream's.** The evaluation harness, the pinned reader
  (`Qwen3.5-9B`, thinking on, 20k completion budget) and the judge (`gpt-5.2`, medium
  reasoning) are LongMemEval-V2's own. Palinode is a `memory_modules` backend
  (`bench/longmemeval_v2/`); nothing upstream is modified.
- **Same conditions for every row, including the baseline.** Every row below ran on the same
  reader, the same judge and the same **40k-token memory-context budget**. Upstream's
  `rag_query_to_slice` baseline was rerun by us under exactly those conditions rather than
  copied from the paper (whose reader setup and 200k budget we could not reproduce on one 32 GB
  GPU — see *Caveats*).
- **Palinode rows.** *Raw slices*: every trajectory saved as one markdown file, one chunk per
  state (the accessibility tree, fenced), indexed by Palinode's normal indexer with no LLM
  call, recalled by hybrid search (BM25 + `bge-m3`, RRF). *Notes*: an extraction LLM
  (`Qwen3.8-27B`, local, thinking off) proposes fact / transition / procedure / gotcha notes per
  trajectory at insert time (`specs/prompts/trajectory-extraction.md`); the adapter validates
  them and writes each through `save_memory` as an `Insight`. Notes are recalled from their own
  pool and placed ahead of the slices. *+r1*: each retrieved state is returned with its
  neighbouring states (upstream's slice radius). Everything at query time is LLM-free.
- **Query latency** is what the leaderboard's LAFS metric scores. Ours is one embed call plus
  two SQLite queries.

### Results

Small tier, all 451 questions, `overall_full_set` (abstention questions included).

| | both (451) | web (240) | enterprise (211) | query s | ctx tokens |
|---|---|---|---|---|---|
| upstream `rag_query_to_slice` — 6 hits ±1, with screenshots | 38.4 | 36.2 | 40.8 | 0.98 | 29k |
| Palinode raw slices, top-10 (LLM-free save) | 36.1 | 37.1 | 35.1 | 0.35 | 25k |
| Palinode raw slices, top-6 ±1 | — | 38.8 | 31.3 | 0.18 | 35k |
| Palinode notes 6 + slices 6 | 40.6 | 44.2 | 36.5 | 0.21 | 15k |
| Palinode notes 10 + slices 4 | 42.6 | 44.6 | 40.3 | 0.22 | 11k |
| **Palinode notes 6 + slices 6 ±1** | **44.8** | **48.3** | **40.8** | 0.25 | 34k |

Per type, web, the four rows that matter:

| web | static | dynamic | procedure | gotchas | abstention (72) |
|---|---|---|---|---|---|
| upstream slice baseline | 38.5 | 38.9 | 32.3 | 26.7 | 15.3 |
| raw slices top-10 | 41.8 | 31.9 | 40.3 | 20.0 | 19.4 |
| notes 6 + slices 6 | 46.2 | 37.5 | 51.6 | 33.3 | 27.8 |
| notes 6 + slices 6 ±1 | 49.5 | 45.8 | 54.8 | 26.7 | 34.7 |

Per type, enterprise:

| enterprise | static | dynamic | procedure | gotchas | abstention (56) |
|---|---|---|---|---|---|
| upstream slice baseline | 43.9 | 38.2 | 40.9 | 28.6 | 17.9 |
| raw slices top-10 | 33.7 | 29.1 | 47.7 | 28.6 | 5.4 |
| notes 6 + slices 6 | 38.8 | 25.5 | 45.5 | 35.7 | 3.6 |
| notes 10 + slices 4 | 41.8 | 27.3 | 54.5 | 35.7 | 10.7 |
| notes 6 + slices 6 ±1 | 42.9 | 34.5 | 45.5 | 35.7 | 8.9 |

### Reading the table

- **The raw store is at parity with a purpose-built slice RAG.** Palinode's LLM-free store,
  text only, scores 36.1 against the baseline's 38.4 with screenshots, at a third of the query
  latency. Slightly ahead on web, behind on enterprise.
- **Notes are worth +12 on web and +1 on enterprise.** At an identical 32k budget, identical
  slices and identical reader, putting six extracted notes ahead of the slices moves web from
  38.8 to 48.3. Procedure questions are where it shows most (32 → 55): with the evidence in
  context the reader answers a written procedure 89 % of the time and never says UNKNOWN,
  where it hedged on raw trees. Abstention doubles (15 → 35): a note that says what a page
  contains lets the reader say what it doesn't.
- **Neighbouring states are the dynamic-tracking lever — once the budget allows them.** "What
  happens after X" is answered by the state *after* the retrieved action: ±1 takes web dynamic
  from 31.9 to 44.4–45.8. On raw enterprise slices the same expansion *hurts* (35.1 → 31.3):
  ServiceNow trees run ~8k tokens a state and at 40k the expansion crowds out distinct hits.
  With notes carrying the static facts, the neighbours fit and enterprise recovers to 40.8 —
  level with the baseline. Ten notes and four slices reach 40.3 at a third of the tokens: on
  enterprise, more notes are the cheaper route to the same place.
- **Enterprise is a compression problem the notes don't solve yet.** On enterprise static
  questions the gold string is in the retrieved context 91 % of the time, yet the reader
  answers only 48 % and returns UNKNOWN 37 %. Seven notes per trajectory cannot enumerate a
  ServiceNow page's hundreds of labels, and the slices are too large for a 9B reader to search.
  Finer-grained facts (per field, per option list) are the next write-time row, not more
  retrieval.
- **What the reader does with what it's given dominates.** Raw-slice rows differing only in
  context cap (60k vs 40k) moved 43.3 → 38.8 on web. The 9B reader with a 20k thinking budget
  is the constant every system in the table shares, and it is the largest source of variance.

### Against the leaderboard

LME-V2's leaderboard scores LAFS gain over a fixed reference frontier — accuracy integrated
over log-scaled *query* latency (1–200 s), insert cost unmeasured. The frontier's fast point is
upstream's slice + notes RAG at 51.0 % / 0.2 s; everything above it pays 27–177 s of LLM per
query. Palinode's best both-domain point is 44.8 % at 0.25 s: on the frontier's latency, below
its accuracy, so ΔLAFS = 0 and **no leaderboard submission** — the gate this program set for
itself (≥55 % on the small tier). The per-type table is the deliverable: against the same baseline
under the same conditions the write path is worth **+6.4 overall** (+12 on web, level on
enterprise), and the per-type rows say where the next points are.

### Cost

Per domain: slice indexing ~30–40 min (`bge-m3` on a 5060 Ti); extraction 3.5 h (web) /
12.8 h (enterprise, 100-state trajectories) on an M-series box, 743 / 666 notes; reader
~30–60 min per 240-question row; judge ≈ $2 per row (156 of 451 questions are LLM-judged,
the rest deterministic).

### Caveats

- Single run per row, no confidence intervals. Two raw-slice rows differing only in context
  cap swung gotchas (15 questions) by 7 points — treat per-type deltas under ±5 as noise and
  overall under ±2–3 as noise.
- Reader window. Our reader served 262k context at the end but 131k for most rows; the Palinode
  rows are capped at 40k and unaffected. The baseline's first run at its 200k default overflowed
  on 63/240 questions and was discarded; the 40k rerun is the number reported. The paper's own
  slice baseline (42.8 % both domains, 200k budget) is the reference for what that method does
  with more room.
- Baseline embedder: `Qwen3-Embedding-8B` served as Ollama's 4-bit `qwen3-embedding:8b`; its
  0.98 s query latency is that endpoint, not the method.
- Extraction quality was not tuned: one prompt, one pass, one model. 1 of 100 web and 2 of 100
  enterprise trajectories have no notes (the extractor never answered; bounded drain).
- Palinode's stock BM25 arm returns nothing for question-shaped queries (FTS5 implicit AND;
  a known, documented limitation). The adapter runs an OR-joined BM25 arm through the store's own ranker; `fts_mode:
  "and"` is retained for measuring the difference, which was not run in this round.

### Reproduce

```bash
# upstream checkout + venv, data, reader/judge endpoints: bench/longmemeval_v2/README.md
python -m bench.longmemeval_v2.run --domain web --tier small --method palinode \
    --output-dir runs/web --save-memory --skip-evaluation --palinode-extract      # build + notes
python -m bench.longmemeval_v2.run --domain web --tier small --method palinode \
    --output-dir runs/web-eval --load-memory-dir runs/web/memory_state \
    --palinode-extract --palinode-notes-top-k 6 --palinode-top-k 6 --palinode-neighbor-radius 1 \
    --memory-context-max-tokens 40000
python -m bench.longmemeval_v2.report runs/web-eval …                              # the tables above
```

Run artifacts: `bench/results/longmemeval-v2-*-2026-09-04/`.
