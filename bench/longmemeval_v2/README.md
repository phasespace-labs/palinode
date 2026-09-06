# bench/longmemeval_v2 — LongMemEval-V2 × Palinode

Runs [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) (Wu et al., 2026) with a
real Palinode store as the memory backend. V2 is **not** V1 with more questions: the haystack is
web-agent trajectories (accessibility trees + actions + agent thoughts, 1,870 of them), and the
451 questions test static state recall, dynamic state tracking, workflow knowledge, environment
gotchas and premise awareness (`-abs` abstention traps). `bench/longmemeval/` is V1 and untouched.

The upstream harness owns the reader (pinned `Qwen3.5-9B`), judge (`gpt-5.2` for recorded
numbers), haystacks, scoring and the leaderboard packaging. This package supplies one thing: a
`memory_modules` backend, `memory_type = "palinode"`, plus a runner that registers it. Nothing
upstream is modified.

## Layout

| file | role |
|---|---|
| `corpus.py` | trajectory → markdown: `## Outline` (ordered actions + thought clauses) and one `## State N` section per state with the a11y tree fenced; trees past the embedder window become `### State N (part k)` sub-chunks, never truncated |
| `adapter.py` | `PalinodeMemory`: `insert` writes + indexes through the canonical `index_file` pipeline (LLM-free); `query` is hybrid recall; save/load moves the store into the harness's `memory_state` |
| `extract.py` | the notes pool: `specs/prompts/trajectory-extraction.md` → fact / transition / procedure / gotcha notes per trajectory at insert time, written through `save_memory` as `Insight` files (`LME_EXTRACT_*` endpoint) |
| `run.py` | superset of upstream `evaluation/run_eval.py`: same materialisation and baseline configs, plus `--method palinode`, `--palinode-*` params, `--evaluator-base-url`, memory save/load/skip, a 1 h reader timeout and a keep-alive-free reader client |
| `report.py` | per-type table across runs (`--combine` weights domains the way the leaderboard does) |
| `results.py` | export a run to `bench/results/` scrubbed and compact |

## Setup

```bash
git clone https://github.com/xiaowu0162/LongMemEval-V2 ~/Code/LongMemEval-V2
cd ~/Code/LongMemEval-V2 && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e . torch torchvision
uv pip install --python .venv/bin/python -e <this repo>      # palinode into the same venv
# data (skip the 5.9 GB trajectory screenshots — a text backend doesn't use them)
python -c "from huggingface_hub import snapshot_download as s; s('xiaowu0162/longmemeval-v2', repo_type='dataset', local_dir='~/.cache/longmemeval-v2', ignore_patterns=['trajectory_screenshots/*'])"
```

The harness's context-truncation tokenizer is the `Qwen/Qwen3.5-9B` processor (a VL model —
torch is needed just to load it).

## Running

```bash
export LME_V2_HOME=~/Code/LongMemEval-V2 PYTHONPATH=<this repo>
export PALINODE_DIR=/tmp/no-config     # keep ~/palinode's config out; the adapter points its own store
export OLLAMA_URL=http://<host>:11434  # bge-m3, Palinode's embedder

# build the web haystack once and save it (small tier: one 100-trajectory haystack per domain)
$LME_V2_HOME/.venv/bin/python -m bench.longmemeval_v2.run --domain web --tier small --method palinode \
    --output-dir runs/palinode_web_small --save-memory --skip-evaluation
# answer + judge against the saved store
$LME_V2_HOME/.venv/bin/python -m bench.longmemeval_v2.run --domain web --tier small --method palinode \
    --output-dir runs/palinode_web_small_eval --load-memory-dir runs/palinode_web_small/memory_state \
    --reader-base-url http://<reader>/v1 --reader-model qwen3.5-9b \
    --evaluator-base-url http://<judge>/v1 --evaluator-model <judge>     # omit for gpt-5.2 via OPENAI_API_KEY
# upstream baselines under the same reader/judge
... --method rag_query_to_slice --controller-base-url ... --embedding-base-url ...
```

`--question-ids a b c` / `--limit N` select a subset; the harness requires question and haystack
ids to match exactly, which the runner materialises for you.

## Adapter params

Query-time (may change when loading a saved store): `top_k`, `notes_top_k` (notes returned ahead of
the slices), `neighbor_radius` (states N±r around each hit — upstream's slice radius is 1), `images`
(each hit state's screenshot as an image item), `fts_mode`, `threshold`, `hybrid_weight`.
Insert-time (a different value means a different store): `slice_max_chars`, `extract`. The
measured configuration is `--palinode-extract --palinode-notes-top-k 6 --palinode-top-k 6
--palinode-neighbor-radius 1 --memory-context-max-tokens 40000`; results in `docs/BENCHMARKS.md`.

## Adapter choices worth knowing

- **Save-only, LLM-free.** The store is the raw state-slice pool. Query latency — what the
  leaderboard's LAFS metric scores — is one embed call plus two SQLite queries (~0.1 s).
- **BM25 arm is OR-joined** (`fts_mode: "or"`, `bm25_or()`). FTS5 treats whitespace as implicit
  AND and `sanitize_fts_query` strips `OR`, so `store.search_hybrid` returns an empty BM25 slate
  for almost any question-shaped query and silently runs vector-only. The adapter runs its own
  any-content-word MATCH and fuses through the store's pure `rank_hybrid`. `fts_mode: "and"` is
  the stock path, kept so the difference can be measured — the defect was documented and was
  closed on V1 evidence that the vector arm carries recall; V2's static-recall questions (exact
  UI labels inside a11y trees) are where that conclusion gets tested.
- **Per-file dedup off** (`dedup_score_gap: 1e9`). The ranker keeps a second chunk from the same
  file only within 0.2 of the file's best; several states of one trajectory are legitimately the
  evidence for a dynamic-tracking question.
- **Vectorless stores are refused** when `hybrid` is on. The embedder's circuit breaker *defers*
  embeds rather than raising, so an unreachable embedder would otherwise produce a keyword-only
  store that reports as hybrid.
- Context items are numbered and self-delimited (`[k] Trajectory <id> — …`, goal, then the chunk)
  because the harness concatenates items with no separator.

## Running it for hours — what bit us

- **Reader timeouts shorter than a queued long-thinking request compound the queue.** The
  OpenAI client re-sends on timeout; each retry is another 20k-token request behind the ones
  already waiting. `run.py` uses a 1 h timeout, few retries, and no keep-alive.
- **Context cap.** At ~58k-token prompts only two requests fit the reader's KV cache and 8-way
  concurrency collapses to 2. `--memory-context-max-tokens 40000` for every row.
- **Serialise evals** (one reader eval at a time; a queue script is trivial): two evals overlap past a
  proxy's concurrency limit and 429.
- **A thinking model is not a judge**: it spends the 4,096-token budget thinking, returns empty
  content, and the harness raises in scoring — after generation, whose outputs live only in memory.
- **The extractor can hang a request forever**; the drain is bounded (daemon threads) and a
  trajectory without notes is recorded, not fatal.
- **Load the harness's processor offline** (`HF_HUB_OFFLINE=1`) — it is loaded per worker thread.

## Measured (2026-09-04, small tier)

- 5,095 states over both domains; a11y tree median ≈ 4.5k tokens, p90 ≈ 11k, max ≈ 80k.
- bge-m3 via Ollama: ≈ 9k tok/s → one domain (~18M tokens) indexes in ~30 min; extraction with a
  local 27B, thinking off: 3.5 h (web) / 12.8 h (enterprise) per domain, ~7 notes per trajectory.
- Hybrid query: 0.16–0.35 s on the notes+slices rows.
