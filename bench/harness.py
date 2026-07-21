"""Measurement primitives for the ingestion & recall benchmark.

All functions operate against whatever ``PALINODE_DIR`` the module-level
:data:`palinode.core.config.config` currently points at. Use
:func:`point_config_at` to aim the harness at a throwaway directory before
generating a corpus.

Nothing here mocks the database or the store — it drives the real
``index_file`` pipeline, the real SQLite-vec + FTS5 index, and the real
search functions, exactly as a production save would. The only things it
*measures* are model calls (by wrapping the embed + chat entry points),
wall-clock, and the resulting on-disk / in-DB state.
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterator


# --------------------------------------------------------------------------
# Config pointing
# --------------------------------------------------------------------------

def point_config_at(palinode_dir: str) -> None:
    """Aim the global config + store at a throwaway *palinode_dir*.

    Mirrors what the CLI/API do at startup, but in-process, so a single
    benchmark process can prepare and measure a clean store. Resets the
    store's one-shot first-run check and allows a fresh DB alongside the
    corpus files (the documented ``PALINODE_ALLOW_FRESH_DB`` escape hatch —
    files are written before the DB exists, which the store otherwise treats
    as a misconfiguration).
    """
    from palinode.core import store
    from palinode.core.config import config

    os.makedirs(palinode_dir, exist_ok=True)
    config.memory_dir = palinode_dir
    config.db_path = os.path.join(palinode_dir, ".palinode.db")
    # No git in the benchmark: the corpus is written directly, not committed.
    config.git.auto_commit = False
    store._db_checked = False
    os.environ["PALINODE_ALLOW_FRESH_DB"] = "1"


# --------------------------------------------------------------------------
# Model-call instrumentation
# --------------------------------------------------------------------------

@dataclass
class Counters:
    """Model calls observed during an operation."""

    embed_calls: int = 0          # document/section embeds (ingest path)
    embed_query_calls: int = 0    # query embeds (recall path)
    embed_input_chars: int = 0
    chat_llm_calls: int = 0


@contextlib.contextmanager
def _count_model_calls() -> Iterator[Counters]:
    """Wrap the embed + chat entry points to count calls during a block.

    ``index_file`` reaches the embedder through ``embedder.embed`` and the
    query path through ``embedder.embed_query`` — two distinct module
    attributes, both looked up at call time. **Both** are wrapped:
    ``embed_query`` delegates to the private ``_embed_local``, not to
    ``embed``, so wrapping only ``embed`` would silently under-count every
    query-time embedding as zero. Chat/completions are wrapped on the shared
    Ollama client to *prove* the ingest and recall paths make zero chat-LLM
    calls — the headline cost claim.
    """
    from palinode.core import embedder as embedder_mod
    from palinode.core.ollama_client import get_ollama_client

    counters = Counters()

    orig_embed = embedder_mod.embed
    orig_embed_query = embedder_mod.embed_query

    def counting_embed(text: str, backend: str = "local") -> list[float]:
        counters.embed_calls += 1
        counters.embed_input_chars += len(text)
        return orig_embed(text, backend)

    def counting_embed_query(text: str, backend: str = "local") -> list[float]:
        counters.embed_query_calls += 1
        counters.embed_input_chars += len(text)
        return orig_embed_query(text, backend)

    client = get_ollama_client()
    orig_chat = client.chat
    orig_generate = client.generate
    orig_completions = client.chat_completions

    def counting_chat(*args: Any, **kwargs: Any) -> Any:
        counters.chat_llm_calls += 1
        return orig_chat(*args, **kwargs)

    def counting_generate(*args: Any, **kwargs: Any) -> Any:
        counters.chat_llm_calls += 1
        return orig_generate(*args, **kwargs)

    def counting_completions(*args: Any, **kwargs: Any) -> Any:
        counters.chat_llm_calls += 1
        return orig_completions(*args, **kwargs)

    embedder_mod.embed = counting_embed  # type: ignore[assignment]
    embedder_mod.embed_query = counting_embed_query  # type: ignore[assignment]
    client.chat = counting_chat  # type: ignore[method-assign]
    client.generate = counting_generate  # type: ignore[method-assign]
    client.chat_completions = counting_completions  # type: ignore[method-assign]
    try:
        yield counters
    finally:
        embedder_mod.embed = orig_embed  # type: ignore[assignment]
        embedder_mod.embed_query = orig_embed_query  # type: ignore[assignment]
        client.chat = orig_chat  # type: ignore[method-assign]
        client.generate = orig_generate  # type: ignore[method-assign]
        client.chat_completions = orig_completions  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------

@dataclass
class IngestResult:
    """Outcome + cost of indexing a corpus."""

    num_files: int
    num_facts: int                  # total indexed chunks (a "fact" == a chunk)
    embed_calls: int                # per-section embed calls (cost-per-fact numerator)
    embed_input_chars: int
    embed_input_tokens_approx: int  # chars/4 heuristic, clearly an estimate
    chat_llm_calls: int             # must be 0 on the ingest path
    wall_clock_s: float
    embedded: bool                  # True iff vectors landed (else FTS-only)
    num_vectors: int
    chunks_written: int
    chunks_unchanged: int

    @property
    def embed_calls_per_fact(self) -> float:
        return self.embed_calls / self.num_facts if self.num_facts else 0.0

    @property
    def chat_calls_per_fact(self) -> float:
        return self.chat_llm_calls / self.num_facts if self.num_facts else 0.0


def _glob_md(palinode_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(palinode_dir, "**", "*.md"), recursive=True))


def init_store() -> None:
    """Create the fresh SQLite schema for the pointed-at store."""
    from palinode.core import store

    store.init_db()


def _counts(palinode_dir: str) -> tuple[int, int]:
    from palinode.core import store

    db = store.get_db()
    try:
        n_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_vec = db.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    finally:
        db.close()
    return int(n_chunks), int(n_vec)


def index_all(palinode_dir: str) -> IngestResult:
    """Index every ``.md`` file under *palinode_dir*, measuring cost.

    Drives the canonical ``index_file`` pipeline (parse → dedup → embed →
    upsert) file-by-file in sorted order (deterministic), wrapped in the
    model-call counters.
    """
    from palinode.indexer.index_file import index_file

    files = _glob_md(palinode_dir)
    chunks_written = 0
    chunks_unchanged = 0
    embedded_any = False

    t0 = perf_counter()
    with _count_model_calls() as counters:
        for fp in files:
            res = index_file(fp)
            chunks_written += res["chunks_written"]
            chunks_unchanged += res["chunks_unchanged"]
            if res["embedded"]:
                embedded_any = True
    wall = perf_counter() - t0

    num_facts, num_vectors = _counts(palinode_dir)
    return IngestResult(
        num_files=len(files),
        num_facts=num_facts,
        embed_calls=counters.embed_calls,
        embed_input_chars=counters.embed_input_chars,
        embed_input_tokens_approx=round(counters.embed_input_chars / 4),
        chat_llm_calls=counters.chat_llm_calls,
        wall_clock_s=wall,
        embedded=embedded_any,
        num_vectors=num_vectors,
        chunks_written=chunks_written,
        chunks_unchanged=chunks_unchanged,
    )


# --------------------------------------------------------------------------
# State fingerprint (determinism axis)
# --------------------------------------------------------------------------

@dataclass
class StateFingerprint:
    """A byte-diffable summary of the resulting memory state."""

    files_sha256: str          # over source files (path-relative)
    db_logical_sha256: str     # over normalized chunk rows, timestamps excluded
    num_facts: int
    num_vectors: int
    vec_ids_sha256: str        # over the sorted set of embedded chunk ids


def _hash_files(palinode_dir: str) -> str:
    """SHA-256 over ``(relpath, sha256(bytes))`` for every source ``.md`` file.

    Path-relative, so the fingerprint is independent of where the corpus
    lives on disk — two clean ingests of the same corpus at different roots
    still produce identical source bytes.
    """
    h = hashlib.sha256()
    for fp in _glob_md(palinode_dir):
        rel = os.path.relpath(fp, palinode_dir)
        body = Path(fp).read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(body).hexdigest().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def fingerprint_state(palinode_dir: str) -> StateFingerprint:
    """Compute a determinism fingerprint of the current memory state.

    The DB fingerprint is a *logical* dump — chunk rows ordered by id, with
    volatile timestamp columns (``created_at`` / ``last_updated``) excluded so
    the fingerprint reflects "identical modulo timestamps". The raw ``.db``
    file is deliberately not byte-diffed: SQLite's page layout, freelists and
    WAL state are implementation noise, not memory state.

    Note: chunk ids are derived from the absolute file path, so a meaningful
    DB comparison requires the same ``palinode_dir`` across runs (as a
    production re-ingest into a fixed store would use).
    """
    from palinode.core import store

    db = store.get_db()
    try:
        rows = db.execute(
            "SELECT id, file_path, section_id, category, content, content_hash "
            "FROM chunks ORDER BY id"
        ).fetchall()
        vec_rows = db.execute("SELECT id FROM chunks_vec ORDER BY id").fetchall()
    finally:
        db.close()

    logical = hashlib.sha256()
    for r in rows:
        rel = os.path.relpath(r["file_path"], palinode_dir)
        fields = [
            r["id"],
            rel,
            r["section_id"] or "",
            r["category"] or "",
            r["content"],
            r["content_hash"] or "",
        ]
        logical.update("\t".join(fields).encode("utf-8"))
        logical.update(b"\n")

    vec_hash = hashlib.sha256()
    for vr in vec_rows:
        vec_hash.update(vr["id"].encode("utf-8"))
        vec_hash.update(b"\n")

    return StateFingerprint(
        files_sha256=_hash_files(palinode_dir),
        db_logical_sha256=logical.hexdigest(),
        num_facts=len(rows),
        num_vectors=len(vec_rows),
        vec_ids_sha256=vec_hash.hexdigest(),
    )


# --------------------------------------------------------------------------
# Recall latency (axes 3 + 4)
# --------------------------------------------------------------------------

@dataclass
class RecallResult:
    """Latency + hit summary for one recall mode."""

    mode: str                          # "hybrid" | "keyword" | "grep"
    # Two separate counters, deliberately not summed into one "model calls"
    # number. The differentiator claim is specifically that recall invokes no
    # *chat/synthesis* model; hybrid search still costs exactly one *embedding*
    # per query to build the query vector, and reporting that as zero would be
    # dishonest on a cost axis.
    query_time_chat_llm_calls: int     # chat/synthesis model calls (the claim: 0)
    query_time_embed_calls: int        # query-vector embeddings (1/query for hybrid)
    n_queries: int
    cold_p50_ms: float
    cold_p95_ms: float
    warm_p50_ms: float
    warm_p95_ms: float
    warm_mean_ms: float
    total_hits: int
    embedder_available: bool
    # True iff the embedder was *actively disabled* for this measurement (the
    # degradation-floor experiment), as opposed to merely being unused or
    # absent on the host. Callers must not describe a run as "embedder
    # disabled" unless this is True.
    embedder_disabled: bool = False


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank percentile.
    rank = max(0, min(len(ordered) - 1, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[rank]


def _time_queries(fn: Callable[[str], int], queries: tuple[str, ...], iters: int) -> tuple[list[float], int]:
    samples: list[float] = []
    hits = 0
    for q in queries:
        for _ in range(iters):
            t0 = perf_counter()
            hits += fn(q)
            samples.append((perf_counter() - t0) * 1000.0)
    return samples, hits


def embedder_available() -> bool:
    """True iff a probe embed succeeds against the configured Ollama."""
    from palinode.core.ollama_client import get_ollama_client

    try:
        return bool(get_ollama_client().probe_embed(timeout=2.0))
    except Exception:
        return False


@contextlib.contextmanager
def embedder_disabled() -> Iterator[None]:
    """Actively disable the embedder for the duration of the block.

    Every embed entry point returns an empty vector and the reachability probe
    reports failure — the same degraded state a host with no Ollama presents.
    This makes the degradation-floor measurement a real experiment rather than
    an assumed one: recall measured inside this block genuinely had no embedder
    to reach, on any host, whether or not one was running.
    """
    from palinode.core import embedder as embedder_mod
    from palinode.core.ollama_client import get_ollama_client

    client = get_ollama_client()
    orig_embed = embedder_mod.embed
    orig_embed_query = embedder_mod.embed_query
    orig_probe = client.probe_embed

    embedder_mod.embed = lambda text, backend="local": []  # type: ignore[assignment]
    embedder_mod.embed_query = lambda text, backend="local": []  # type: ignore[assignment]
    client.probe_embed = lambda **kwargs: False  # type: ignore[method-assign]
    try:
        yield
    finally:
        embedder_mod.embed = orig_embed  # type: ignore[assignment]
        embedder_mod.embed_query = orig_embed_query  # type: ignore[assignment]
        client.probe_embed = orig_probe  # type: ignore[method-assign]


def measure_recall_keyword(
    queries: tuple[str, ...], *, iters: int = 20, top_k: int = 10, disable_embedder: bool = False
) -> RecallResult:
    """BM25/FTS5-only recall latency.

    The keyword path never builds a query vector, so its query-time embed and
    chat counts are *measured* (not assumed) via the same counters the ingest
    path uses. With ``disable_embedder=True`` the embedder is additionally torn
    down for the measurement, so the result can honestly be labelled
    "embedder disabled" on any host.
    """
    from palinode.core import store

    def run(q: str) -> int:
        return len(store.search_fts(q, top_k=top_k))

    available = embedder_available()
    with contextlib.ExitStack() as stack:
        if disable_embedder:
            stack.enter_context(embedder_disabled())
        counters = stack.enter_context(_count_model_calls())
        cold, _ = _time_queries(run, queries, iters=1)
        warm, hits = _time_queries(run, queries, iters=iters)

    return RecallResult(
        mode="keyword",
        query_time_chat_llm_calls=counters.chat_llm_calls,
        query_time_embed_calls=counters.embed_calls + counters.embed_query_calls,
        n_queries=len(queries),
        cold_p50_ms=_percentile(cold, 50),
        cold_p95_ms=_percentile(cold, 95),
        warm_p50_ms=_percentile(warm, 50),
        warm_p95_ms=_percentile(warm, 95),
        warm_mean_ms=statistics.fmean(warm) if warm else 0.0,
        total_hits=hits,
        embedder_available=available,
        embedder_disabled=disable_embedder,
    )


def measure_recall_hybrid(queries: tuple[str, ...], *, iters: int = 20, top_k: int = 10) -> RecallResult | None:
    """Hybrid (vector + BM25, RRF) recall latency.

    Returns ``None`` when no embedder is reachable — hybrid search needs a
    query embedding, and fabricating one would misreport the number. The
    keyword-only measurement (which is the honest floor here) covers that case.

    Two costs are reported separately and never conflated: chat/synthesis model
    calls (0 — recall never invokes one) and query-vector embeddings (exactly
    one per query, measured). "No LLM at query time" means no chat model, not
    no model at all.
    """
    from palinode.core import embedder, store

    if not embedder_available():
        return None

    with _count_model_calls() as counters:
        def run(q: str) -> int:
            vec = embedder.embed_query(q)
            return len(store.search_hybrid(q, vec, top_k=top_k))

        cold, _ = _time_queries(run, queries, iters=1)
        warm, hits = _time_queries(run, queries, iters=iters)

    return RecallResult(
        mode="hybrid",
        query_time_chat_llm_calls=counters.chat_llm_calls,
        query_time_embed_calls=counters.embed_calls + counters.embed_query_calls,
        n_queries=len(queries),
        cold_p50_ms=_percentile(cold, 50),
        cold_p95_ms=_percentile(cold, 95),
        warm_p50_ms=_percentile(warm, 50),
        warm_p95_ms=_percentile(warm, 95),
        warm_mean_ms=statistics.fmean(warm) if warm else 0.0,
        total_hits=hits,
        embedder_available=True,
    )


def measure_recall_grep(palinode_dir: str, queries: tuple[str, ...], *, iters: int = 5) -> RecallResult:
    """The bottom of the degradation floor: no DB, no service — grep the files.

    Pure-Python substring scan over the source ``.md`` files: what a human (or
    ``grep -ri``) still gets when the API, the index, and the embedder are all
    down. Files are the source of truth, so recall never reaches zero.
    """
    files = _glob_md(palinode_dir)
    cache = {fp: Path(fp).read_text(encoding="utf-8").lower() for fp in files}

    def run(q: str) -> int:
        tokens = [t for t in q.lower().split() if t]
        return sum(1 for text in cache.values() if all(t in text for t in tokens))

    cold, _ = _time_queries(run, queries, iters=1)
    warm, hits = _time_queries(run, queries, iters=iters)
    return RecallResult(
        mode="grep",
        query_time_chat_llm_calls=0,
        query_time_embed_calls=0,
        n_queries=len(queries),
        cold_p50_ms=_percentile(cold, 50),
        cold_p95_ms=_percentile(cold, 95),
        warm_p50_ms=_percentile(warm, 50),
        warm_p95_ms=_percentile(warm, 95),
        warm_mean_ms=statistics.fmean(warm) if warm else 0.0,
        total_hits=hits,
        embedder_available=embedder_available(),
    )
