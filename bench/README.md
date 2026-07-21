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

## Layout

| File | Purpose |
|---|---|
| `corpus.py` | Deterministic synthetic corpus generator (pure function of `(seed, size)`). |
| `harness.py` | Ingest, state-fingerprint, and recall-latency measurement primitives. |
| `run.py` | End-to-end orchestrator (the four axes) + CLI. |
| `report.py` | Renders a results JSON object as a Markdown report. |

## Tests

```bash
python -m pytest tests/test_bench_harness.py -q
```
