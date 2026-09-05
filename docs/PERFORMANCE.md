# Performance

Operating numbers for a self-hosted Palinode store, on named boxes, with the
method stated so you can re-run them on yours.

The question this page exists to answer is the one you have before installing
anything: **will this be fast enough for a store my size?** The short version —
keyword search is under 10 ms all the way to 50,000 chunks, memory stays under
70 MiB across that whole range, and vector-search latency is dominated not by
Palinode but by the round trip to your embedding model. Plan around your
embedder, not around the store.

## The box

| | |
|---|---|
| Machine | Ubuntu 24.04 LXC container, 4 vCPU (12th Gen Intel i9-12900H), 4 GB RAM |
| Embedder | `bge-m3` (1024d), GPU-resident on a separate RTX 5060 Ti, reached over LAN |
| Palinode | 0.15.0, Python 3.12.3 |
| Store | SQLite + sqlite-vec + FTS5, WAL enabled |

This is a deliberately ordinary box: four cores, four gigabytes, a container on
a shared host, talking to a GPU over the network. It is the machine a
self-hoster actually has, not a best case.

## Numbers

Real embeddings throughout — every vector below came off that GPU. 10 queries ×
20 iterations per point, warm. A "chunk" is one indexed section; the corpus
generator averages two chunks per file.

| chunks | files | index wall | chunks/s | hybrid p50 | hybrid p95 | keyword p50 | keyword p95 | RSS | DB on disk |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 500 | 122 s | 8 | 153.5 ms | 178.2 ms | 0.6 ms | 0.7 ms | 55.6 MiB | 5.8 MiB |
| 10,000 | 5,000 | 1,209 s | 8 | 149.1 ms | 165.0 ms | 1.8 ms | 3.0 ms | 57.6 MiB | 57.0 MiB |
| 50,000 | 25,000 | 7,118 s | 7 | 206.1 ms | 246.8 ms | 7.2 ms | 8.6 ms | 65.0 MiB | 280.4 MiB |

Reproduce with:

```bash
python -m bench.perf --sizes 1000,10000,50000 --label "your box, stated plainly"
```

## Reading the table

**Vector search is dominated by the embedder, not the store.** Look at the first
two rows: 153.5 ms at 1,000 chunks and 149.1 ms at 10,000 — a tenfold increase
in corpus size that does not move the number at all. That is because a hybrid
query pays one embedding round trip to build the query vector before any
searching happens, and on this host that round trip measures **125 ms at p50**
(439 ms at p95, over 200 timed calls). It is ~80% of hybrid latency at 1,000
chunks and still ~60% at 50,000.

The practical consequence: if your vector searches feel slow, the first place
to look is the network hop and the model, not the index. Co-locating the
embedder, or choosing a smaller one, buys far more than anything Palinode can
do at these corpus sizes.

That p95 of 439 ms is worth noticing too. This GPU also serves chat and a
second embedding model, so the tail is queueing behind other work — a shared
inference box has a long tail even when its median is fine.

**The scan cost underneath is real but small.** Subtracting the embedding round
trip, the 10k → 50k rise of 57 ms across 40,000 chunks puts sqlite-vec's
brute-force scan at roughly **1.4 ms per 1,000 chunks** on this box. Below about
10,000 chunks it is entirely lost in the embedder's own variance, which is why
the first two rows are indistinguishable.

**Keyword search barely notices scale** — 0.6 → 1.8 → 7.2 ms across a 50×
corpus increase, because an inverted index does work proportional to the number
of *matches*, not the size of the corpus. Note it is 20-200× faster than the
hybrid path here, entirely because it needs no query embedding.

**Memory is flat.** 55.6 → 57.6 → 65.0 MiB across that same 50× increase.
sqlite-vec streams vectors from disk instead of holding an index in RAM, so
there is no working-set cliff — a 50,000-chunk store runs comfortably inside a
4 GB container with room to spare, which is why it can live on the same box as
everything else you run.

**Disk is about 6 KB per chunk**, near-constant at every scale. Most of it is
the vector: 1024 dimensions × 4 bytes = 4,096 bytes, with the remainder being
the source text and its FTS5 index. 50,000 chunks ≈ 280 MiB, scaling linearly.
A smaller embedding model shrinks this proportionally.

## Ingest is embedder-bound, and that is the number to plan with

Indexing sustained **7–8 chunks per second** at every scale — flat, because it
is not a Palinode measurement at all. Each chunk costs one embedding, the rig
indexes serially, and one serial round trip to this GPU is ~125 ms. That is
your throughput: `1 ÷ round-trip-seconds`.

So the first index of a 50,000-chunk store took just under two hours here. To
estimate yours, time a single embed against your own Ollama and divide. To go
faster, the lever is concurrency or a closer/smaller model — the same GPU
measures ~52 embeds/s at 8 concurrent workers versus ~8 serial, and it is
nearly idle throughout the serial case.

The pipeline underneath is not the constraint. With the embedding cost removed
it sustains ~350–380 chunks/s (below), roughly 45× the rate a serial remote
embedder can feed it.

## The same store with embedding removed

To separate the store's own cost from the embedder's, the identical sweep run
on a laptop with **synthetic vectors** — the write path, the vector table and
the search path all real, only vector *content* replaced by a deterministic
hash, so no embedding calls happen at all.

**Box:** Apple M3 Max (2023), 16 cores, 64 GB, macOS 26.5 · palinode 0.15.0

| chunks | index wall | chunks/s | hybrid p50 | hybrid p95 | keyword p50 | RSS | DB on disk |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 2.6 s | 382 | 5.0 ms | 6.8 ms | 1.3 ms | 62.7 MiB | 5.9 MiB |
| 10,000 | 27.8 s | 360 | 15.6 ms | 18.0 ms | 2.5 ms | 64.7 MiB | 57.6 MiB |
| 50,000 | 143.7 s | 348 | 64.9 ms | 74.3 ms | 8.4 ms | 61.3 MiB | 284.5 MiB |

With no embedding round trip in the way, the scan cost is visible directly:
~3.3 ms fixed plus **~1.23 ms per 1,000 chunks** — the same slope the dogfood
host shows once its 125 ms is subtracted, on a machine roughly 4× faster per
core. Two very different boxes agreeing on the shape is the reason to trust it.

**This is where the ceiling lives.** sqlite-vec does a brute-force full scan:
every query compares against every vector, forever, with no index to fall back
on. Extrapolating that slope — an extrapolation, not a measurement — the scan
alone reaches 200 ms at roughly 160,000 chunks. Below 50,000 chunks it is
simply not your problem; past six figures it is, and the remedies (binary
quantization, metadata pre-filtering) are tracked and not yet implemented.

Note also what this second table does *not* say: these are the same corpus and
the same code, but recall quality is not measured in either run, because
synthetic vectors carry no meaning. That is a separate axis.

## Recall quality is not on this page

Latency and correctness are different questions and this rig deliberately
measures only the first. For quality — evidence recall, answer accuracy, the
write path measured end to end — see [BENCHMARKS.md](BENCHMARKS.md), which runs
LongMemEval with real embeddings.

## Method

The rig is [`bench/perf.py`](../bench/perf.py), built on the existing
`bench/corpus.py` generator (a pure function of seed and size, so corpora are
byte-reproducible) and `bench/harness.py` (which drives the real
parse → dedup → embed → upsert pipeline under model-call counters).

By default it runs against whatever embedder your config points at and refuses
to start if none is reachable, rather than silently degrading to a keyword-only
measurement and reporting it as a vector one. It also aborts if any scale point
indexes zero vectors — the indexer caches a failed embed probe for 30 seconds,
which is correct in production and will otherwise let a sweep report "hybrid"
latency for a store holding no vectors at all.

`--synthetic-vectors` produces the second table: deterministic hash vectors
instead of model output. Sound for latency and throughput, because sqlite-vec's
scan cost is a function of how many fixed-width vectors it walks and not of what
is in them. **Not** sound for recall quality, so the rig never reports a quality
metric and stamps `synthetic_vectors: true` into its results JSON.

Raw results:
[`perf-dogfood-lxc-bge-m3.json`](../bench/results/perf-dogfood-lxc-bge-m3.json)
(real embedder) ·
[`perf-m3max-synthetic.json`](../bench/results/perf-m3max-synthetic.json)
(synthetic).
