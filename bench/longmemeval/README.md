# bench/longmemeval — LongMemEval × Palinode

Runs [LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., ICLR 2025) against a
real Palinode store. 500 questions, six types plus abstention (`_abs` ids). Each question ships
its own haystack (~40 sessions / ~115k tokens for `_s`), so **each question gets a fresh store**:
sessions → dated `daily/` notes → canonical `index_file` → hybrid recall → external answerer →
upstream judge prompt, verbatim.

## Configuration rows

| row | flag | what it measures |
|---|---|---|
| save-only | *(default, `--pipeline raw`)* | LLM-free ingest + hybrid search. Zero chat-LLM calls before the answerer. Rows A–D. |
| retrieval-only | `--no-answer` | No LLM at all. Reports **evidence recall@k** (was any `answer_session_id` in the top-k?) — the retrieval ceiling. |
| session-end (E0) | `--pipeline session-end` | The production write path: an extraction model plays the agent at every session end (`LME_EXTRACT_*`), and the payload goes through Palinode's real `session_end` — dated `daily/` note tagged `project/user` + indexed twin — with the facts appended to a seeded `projects/user.md` profile and tagged by the real fact-id bootstrap. Chat-LLM calls at ingest, reported per question. |
| session-end + consolidate (E1) | `--pipeline session-end+consolidate` | E0, then the real `run_consolidation` (`LME_CONSOLIDATE_*` via the runner's `llm_fn` seam) updates `projects/user.md`; compacted daily notes are archived but remain indexed. Operations histogram per question. |
| … + raw (E1+raw) | `… --keep-raw` | E1 with the raw transcripts indexed as well — does anything get lost in extraction? |

### Extraction prompt (row E only)

`LME_EXTRACT_PROMPT_VERSION` selects the prompt the extraction model runs;
`v1` is the default. **Choose it for the reader that will consume the notes** —
the effect is reader-dependent, not a general improvement:

| prompt | shape | local reader (RTX 5090) | `gemini-3-flash-preview` |
|---|---|---|---|
| `v1` | summary + facts | 0.740 (E0-local) | **0.820** (E1noarch) |
| `v2` | exhaustive event ledger | **0.860** | 0.780 (E1xv2) |
| `v1t` | v1, terse output | — | — |

`v2` is worth +12 points to a small local model that was dropping countable
events and collapsing recommended lists, and costs a strong reader 4 points at
3,803 prompt tokens per answer against 2,731 — roughly 485 facts per question is
more noise than signal once extraction was already good enough. `v1` therefore
stays the default: the published rows all use the Gemini-family reader, and a
default that makes them worse is the wrong default.

Selecting automatically per reader was considered and rejected — it would make
two runs incomparable without reading their meta. **Any published cross-row
comparison must state which extraction prompt each row used**; every run records
it in its meta.

Row E is the apples-to-apples row against systems that report after an
extraction + consolidation step. Things `pipeline.py` does that production leaves to the
agent, stated so the row is honest: the clock session-end stamps a note with is patched to the
haystack session's date; the fact bullets are appended to the profile by the harness (session-end
itself appends only a one-line index entry to `projects/<p>-status.md`, which is deliberately
not seeded so consolidation targets `user.md`); `config.consolidation.keyword_map` is set to
group the session-end entries under `project/user` for the pass.

Retrieval over-fetches `2 × top_k` and keeps the first `top_k` *distinct* excerpts — session-end
writes each entry to `daily/` and to an indexed twin, and without this the duplicates took four
of the ten slots. Rows A–D have no duplicates, so their top-k is unchanged.

Evidence recall in row E: a hit is traced to a haystack session through the `Session ID` line
session-end stamps into every entry (daily note or indexed twin), or a raw transcript's filename.
Profile facts carry no session id, so `evidence_recall` is a lower bound there; every row also
reports **`answer_in_context`** — the gold answer string, case/punctuation-folded, appears verbatim
in the reader's context.

## Models — different vendors, on purpose

```bash
export LME_ANSWER_BASE_URL=https://api.anthropic.com/v1  LME_ANSWER_MODEL=claude-sonnet-5  LME_ANSWER_API_KEY=…
export LME_JUDGE_BASE_URL=https://api.openai.com/v1      LME_JUDGE_MODEL=gpt-4o-2024-08-06   LME_JUDGE_API_KEY=…
```

Upstream judges with `gpt-4o-2024-08-06`, temperature 0, `max_tokens=10`, label = `"yes" in text.lower()`.
Keep that judge for comparability; never judge with the answerer's family. The runner warns if
they match. Embeddings are whatever the pointed-at Palinode config uses (bge-m3 via Ollama by
default); if no embedder is reachable it **degrades to keyword-only and says so** in `meta`.

## Answerer via the Codex CLI (no API key)

`LME_ANSWER_BASE_URL=codex://local LME_ANSWER_MODEL=gpt-5.5` routes the answerer through
`codex exec` on the ChatGPT-subscription OAuth session — read-only sandbox, `--ephemeral`,
`--ignore-user-config --ignore-rules`, empty cwd, prompt on stdin, reply via
`--output-last-message`. Codex prepends its own system prompt (~9k tokens on an empty prompt),
so `prompt_tokens` is Codex's reported total, not ours. Answerer only: the judge must stay the
upstream `gpt-4o-2024-08-06` on the metered API for comparability.

## Re-judging a finished run

```bash
LME_JUDGE_MODEL=gpt-4o-2024-08-06 LME_JUDGE_API_KEY=… \
  python -m bench.longmemeval.rejudge bench/results/<run> --out bench/results/<run>/rejudge-gpt4o
```

Reads `results.json` (or `hypotheses.jsonl` + `--data`), judges every hypothesis with the
upstream prompts, writes the same `results.json`/`report.md` shape plus `summary.agreement`
against the original labels — the judge-agreement number for a row judged with something else.
Resumable via `rows.jsonl`. A run made with `--no-judge` is judged the same way.

## Run

```bash
# smoke: 20 questions, retrieval only — no API keys needed
python -m bench.longmemeval.run --variant s --limit 20 --no-answer --out bench/results/lme-smoke

# full _s, save-only row
python -m bench.longmemeval.run --variant s --out bench/results/lme-s-save-only

# row E: production write path (extraction + session-end + consolidation), 100-question subset
export LME_EXTRACT_BASE_URL=… LME_EXTRACT_MODEL=gemini-3-flash-preview LME_EXTRACT_API_KEY=… \
       LME_EXTRACT_EXTRA_JSON='{"reasoning_effort":"none"}' LME_EXTRACT_WORKERS=8
export LME_CONSOLIDATE_BASE_URL=… LME_CONSOLIDATE_MODEL=gemini-3-flash-preview LME_CONSOLIDATE_API_KEY=… LME_CONSOLIDATE_TIMEOUT_S=300
python -m bench.longmemeval.run --variant s --pipeline session-end+consolidate --ids "$(cat subset100.ids)" --out bench/results/lme-s-rowE1

# hypotheses only, judge with the upstream script instead
python -m bench.longmemeval.run --variant s --no-judge --out bench/results/lme-s
python LongMemEval/src/evaluation/evaluate_qa.py gpt-4o bench/results/lme-s/hypotheses.jsonl ~/.cache/longmemeval/longmemeval_s_cleaned.json
```

Outputs per run dir: `results.json` (meta + summary + per-question rows incl. retrieved session
ids, token usage, judge raw text), `hypotheses.jsonl` (upstream format), `report.md`.

Dataset is fetched once to `~/.cache/longmemeval/` (`LONGMEMEVAL_DATA` to override). Scratch
store defaults to `/tmp/lme-palinode-store` (`--store-dir` / `LME_STORE_DIR`); it is wiped per
question and never touches a real `PALINODE_DIR`.

## Running for hours without babysitting

Two real failure modes hit the first full run: an unhandled backend exception killed the
process, and a *deterministic* per-input failure (bge-m3 NaN → HTTP 500) was retried
through minutes of backoff per question with nothing outside noticing. The harness now has
three layers against that:

1. **Inside the run** — every finished question is appended to `rows.jsonl`; `--resume` skips
   them, `--retry-errors` re-runs the ones that ended in an error. Deterministic failures
   (NaN, context-length, 4xx) are never retried; transient ones back off 15/45/90 s, dropping
   the pooled Ollama client between tries. Embed-failed files are re-indexed keyword-only;
   a query that won't embed is retried as content words, then keyword-only.
2. **Heartbeat** — `status.json` next to `rows.jsonl` is rewritten at every question start
   and end (`phase`, `qid`, `done`, `total`, `updated_at`).
   Keep `2 × LME_<ROLE>_TIMEOUT_S` under the supervisor's stall threshold: the client makes
   one retry, so a hung backend call costs at most two timeouts before the row is recorded as
   an error — longer than `STALL_MIN` and the watchdog restarts the whole process instead.
3. **Supervisor** — `supervise.sh` runs the command under `caffeinate -i` (no idle sleep),
   appends `--resume --retry-errors`, and restarts it whenever it exits before
   `phase=done` or the heartbeat goes stale for `STALL_MIN` minutes (default 12).
   Gives up after `MAX_RESTARTS` (25). Logs to `<out>/supervise.log`, run output to
   `<out>/run.log`.

```bash
nohup bench/longmemeval/supervise.sh bench/results/lme-s -- \
  python -m bench.longmemeval.run --variant s --out bench/results/lme-s &
```

## Publishing rules

Three runs per row, mean ± CI; report per-type accuracy, evidence recall, prompt tokens per
answer, wall-clock; pin every model version; include the types where we lose.
