"""Tests for the ingestion & recall benchmark harness (bench/, #650).

Covers the three things the harness must guarantee:
  * the synthetic corpus generator is deterministic (same seed → same bytes),
  * the ingest + fingerprint + recall primitives run on a tiny corpus, and
  * the report generator renders a full results object.

These exercise the real store (SQLite-vec + FTS5) against a tmp PALINODE_DIR —
no DB mocking. The embedded path is driven with a deterministic fake embedder
so it is verifiable without a running Ollama; a separate test forces the
keyword-only (FTS-only) degradation path, which is what a real embedder-less
host actually does.
"""
from __future__ import annotations

import hashlib
import os
import random
import shutil
from pathlib import Path

import pytest

from bench import corpus, harness, report, run


@pytest.fixture(autouse=True)
def _restore_global_config():
    """Snapshot + restore the mutated global config and store flag per test."""
    from palinode.core import store
    from palinode.core.config import config

    snap = (config.memory_dir, config.db_path, store._db_checked)
    fresh_env = os.environ.get("PALINODE_ALLOW_FRESH_DB")
    try:
        yield
    finally:
        config.memory_dir, config.db_path, store._db_checked = snap
        if fresh_env is None:
            os.environ.pop("PALINODE_ALLOW_FRESH_DB", None)
        else:
            os.environ["PALINODE_ALLOW_FRESH_DB"] = fresh_env


@pytest.fixture
def bench_dir(tmp_path):
    d = str(tmp_path / "store")
    harness.point_config_at(d)
    return d


@pytest.fixture
def fake_embed(monkeypatch):
    """Deterministic content-addressed fake embedder (1024-dim)."""
    from palinode.core.config import config

    dims = int(config.embeddings.primary.dimensions)

    def _fake(text: str, backend: str = "local") -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        rng = random.Random(seed)
        return [rng.uniform(-1.0, 1.0) for _ in range(dims)]

    monkeypatch.setattr("palinode.core.embedder.embed", _fake)
    return _fake


# --- corpus determinism ---------------------------------------------------

def test_corpus_deterministic(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    ca = corpus.generate(a, seed=123, size=10)
    cb = corpus.generate(b, seed=123, size=10)

    assert ca.num_files == cb.num_files == 10
    a_map = {os.path.relpath(f, a): Path(f).read_bytes() for f in ca.files}
    b_map = {os.path.relpath(f, b): Path(f).read_bytes() for f in cb.files}
    assert a_map == b_map  # byte-identical, path-relative


def test_corpus_seed_changes_content(tmp_path):
    ca = corpus.generate(str(tmp_path / "a"), seed=1, size=12)
    cb = corpus.generate(str(tmp_path / "b"), seed=2, size=12)
    a_bodies = sorted(Path(f).read_text() for f in ca.files)
    b_bodies = sorted(Path(f).read_text() for f in cb.files)
    assert a_bodies != b_bodies


def test_corpus_produces_multichunk_files(tmp_path, bench_dir, fake_embed):
    # Long files (index % 4 == 0) split into multiple sections, so facts > files.
    corpus.generate(bench_dir, seed=7, size=8)
    harness.init_store()
    res = harness.index_all(bench_dir)
    assert res.num_facts > res.num_files


# --- ingest smoke ---------------------------------------------------------

def test_ingest_embedded_smoke(bench_dir, fake_embed):
    corpus.generate(bench_dir, seed=7, size=5)
    harness.init_store()
    res = harness.index_all(bench_dir)

    assert res.num_files == 5
    assert res.num_facts > 0
    assert res.chat_llm_calls == 0            # headline: zero chat-LLM on ingest
    assert res.embed_calls == res.num_facts   # one embed per fact on a cold store
    assert res.embedded is True
    assert res.num_vectors == res.num_facts
    assert res.wall_clock_s >= 0.0

    # SHA-256 dedup: a second pass over unchanged files does no model work.
    res2 = harness.index_all(bench_dir)
    assert res2.chunks_written == 0
    assert res2.embed_calls == 0
    assert res2.chat_llm_calls == 0


def test_ingest_fts_only_degradation(bench_dir, monkeypatch):
    # Force the keyword-only path (what an embedder-less host really does).
    from palinode.indexer import index_file as ifmod

    monkeypatch.setattr(ifmod, "_embeds_deferred", lambda client: True)
    corpus.generate(bench_dir, seed=9, size=4)
    harness.init_store()
    res = harness.index_all(bench_dir)

    assert res.num_facts > 0
    assert res.embed_calls == 0
    assert res.chat_llm_calls == 0
    assert res.embedded is False
    assert res.num_vectors == 0

    # Keyword recall still serves hits with no embedder at all.
    kw = harness.measure_recall_keyword(corpus.TOPICS, iters=2)
    assert kw.total_hits > 0
    assert kw.query_time_chat_llm_calls == 0
    assert kw.query_time_embed_calls == 0


# --- determinism ----------------------------------------------------------

def test_fingerprint_stable_same_path(bench_dir, fake_embed):
    from palinode.core import store

    corpus.generate(bench_dir, seed=42, size=6)
    harness.init_store()
    harness.index_all(bench_dir)
    fp1 = harness.fingerprint_state(bench_dir)

    # Clean state, same store path, ingest again.
    shutil.rmtree(bench_dir)
    os.makedirs(bench_dir)
    store._db_checked = False
    corpus.generate(bench_dir, seed=42, size=6)
    harness.init_store()
    harness.index_all(bench_dir)
    fp2 = harness.fingerprint_state(bench_dir)

    assert fp1.files_sha256 == fp2.files_sha256
    assert fp1.db_logical_sha256 == fp2.db_logical_sha256
    assert fp1.vec_ids_sha256 == fp2.vec_ids_sha256
    assert fp1.num_facts == fp2.num_facts > 0


# --- recall / degradation floor -------------------------------------------

def test_recall_keyword_and_grep(bench_dir, fake_embed):
    corpus.generate(bench_dir, seed=5, size=6)
    harness.init_store()
    harness.index_all(bench_dir)

    kw = harness.measure_recall_keyword(corpus.TOPICS, iters=3)
    assert kw.mode == "keyword"
    assert kw.query_time_chat_llm_calls == 0
    assert kw.query_time_embed_calls == 0
    assert kw.total_hits > 0

    grep = harness.measure_recall_grep(bench_dir, corpus.TOPICS, iters=2)
    assert grep.mode == "grep"
    assert grep.total_hits > 0


def test_hybrid_counts_query_embeddings(bench_dir, monkeypatch, fake_embed):
    """Query-time embeds must be counted, not silently reported as zero.

    ``embed_query`` delegates to the private local-embed helper rather than to
    ``embed``, so an instrument that wraps only ``embed`` under-counts every
    hybrid query as 0 model calls — a false zero on a cost axis.
    """
    corpus.generate(bench_dir, seed=11, size=5)
    harness.init_store()
    harness.index_all(bench_dir)

    monkeypatch.setattr("palinode.core.embedder.embed_query", fake_embed)
    monkeypatch.setattr(harness, "embedder_available", lambda: True)

    iters = 3
    res = harness.measure_recall_hybrid(corpus.TOPICS, iters=iters)
    assert res is not None
    # One query vector per executed query: a cold pass (1) plus `iters` warm.
    assert res.query_time_embed_calls == len(corpus.TOPICS) * (1 + iters)
    # The actual differentiator claim: no chat/synthesis model at query time.
    assert res.query_time_chat_llm_calls == 0


def test_degradation_axis_actually_disables_embedder(bench_dir, fake_embed):
    """Axis 4 must disable the embedder, not merely decline to use it."""
    corpus.generate(bench_dir, seed=13, size=5)
    harness.init_store()
    harness.index_all(bench_dir)

    res = harness.measure_recall_keyword(corpus.TOPICS, iters=2, disable_embedder=True)
    assert res.embedder_disabled is True
    assert res.query_time_embed_calls == 0
    assert res.total_hits > 0  # recall survives with the embedder torn down

    # Not disabled unless asked — the flag never lies about the condition.
    plain = harness.measure_recall_keyword(corpus.TOPICS, iters=1)
    assert plain.embedder_disabled is False


def test_embedder_disabled_restores_entry_points(fake_embed):
    """The disable context manager must fully restore the embed entry points."""
    from palinode.core import embedder

    before = (embedder.embed, embedder.embed_query)
    with harness.embedder_disabled():
        assert embedder.embed("x") == []
        assert embedder.embed_query("x") == []
    assert (embedder.embed, embedder.embed_query) == before


# --- report generation ----------------------------------------------------

def test_report_generation(fake_embed):
    results = run.run_all(seed=99, size=4, n_runs=2, iters=2)
    md = report.render_report(results)

    assert "# Palinode ingestion & recall benchmark" in md
    assert "Axis 1 — cost per remembered fact" in md
    assert "Axis 2 — determinism" in md
    assert "Axis 3 — LLM-free recall latency" in md
    assert "Axis 4 — degradation floor" in md
    # Vendor neutrality is enforced repo-wide by the shipping-leak scrub, which
    # owns the vendor-name list in a dev-only script — naming a vendor here, in
    # a file that ships, would itself be the leak. What this test asserts is the
    # report's structural promise: measured axes only, no comparative framing.
    assert " vs " not in md
    assert "compared to" not in md.lower()


def test_determinism_axis_reports_identical(fake_embed):
    # The determinism axis spawns subprocesses (real FTS-only ingest); across
    # clean-state runs at a fixed path the logical state must be identical.
    out = run.run_determinism_axis(seed=1337, size=4, n_runs=3)
    assert out["files_identical"] is True
    assert out["db_logical_identical"] is True
    assert out["n_runs"] == 3
