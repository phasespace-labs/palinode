"""Tests for the operating-numbers sweep (``bench/perf.py``, docs/PERFORMANCE.md).

The rig's job is to publish numbers, so the failure that matters is not a
crash — it is a plausible-looking number that measures the wrong thing. Two
such traps are covered directly:

  * a sweep that reports vector-search latency for a store holding no vectors
    (the indexer's 30-second failed-probe cache makes this the *default*
    outcome unless the synthetic embedder clears it), and
  * a run whose synthetic vectors are not actually reaching the vector table.

Everything runs against the real store on a tmp PALINODE_DIR — no DB mocking.
"""
from __future__ import annotations

import os
import shutil

import pytest

from bench import harness, perf


@pytest.fixture(autouse=True)
def _restore_global_config():
    """Snapshot + restore the mutated global config, store flag and env var.

    ``harness.point_config_at`` sets ``PALINODE_ALLOW_FRESH_DB=1`` and does not
    unset it, which disables the store's misconfiguration guard for every test
    that runs afterwards in the same process. Same shape as the fixture in
    ``test_bench_harness.py`` / ``test_bench_abstention.py``.
    """
    from palinode.core import store as store_mod
    from palinode.core.config import config

    snap = (config.memory_dir, config.db_path, store_mod._db_checked)
    fresh_db = os.environ.get("PALINODE_ALLOW_FRESH_DB")
    try:
        yield
    finally:
        config.memory_dir, config.db_path, store_mod._db_checked = snap
        if fresh_db is None:
            os.environ.pop("PALINODE_ALLOW_FRESH_DB", None)
        else:
            os.environ["PALINODE_ALLOW_FRESH_DB"] = fresh_db


# ── the synthetic vector ─────────────────────────────────────────────────────


def test_deterministic_vector_is_stable_and_normalised():
    a = perf._deterministic_vector("caching strategy", 1024)
    b = perf._deterministic_vector("caching strategy", 1024)
    assert a == b, "same text must yield the same vector, or dedup misbehaves"
    assert len(a) == 1024
    assert abs(sum(v * v for v in a) ** 0.5 - 1.0) < 1e-5


def test_deterministic_vector_differs_by_text():
    assert perf._deterministic_vector("alpha", 64) != perf._deterministic_vector(
        "beta", 64
    )


def test_deterministic_vector_honours_dimensions():
    for dims in (8, 64, 384, 1024):
        assert len(perf._deterministic_vector("x", dims)) == dims


# ── the probe-cache trap ─────────────────────────────────────────────────────


def test_synthetic_embedder_clears_the_indexer_probe_cache():
    """The bug this rig shipped with, pinned.

    ``reconcile._embeds_deferred`` caches a failed probe for 30 s. Without
    clearing it, the first scale point of a sweep indexes FTS-only and the run
    reports vector latency for a store with zero vectors.
    """
    from palinode.indexer import reconcile

    reconcile._probe_cache.update({"ts": 9e9, "ok": False})
    with perf.synthetic_embedder():
        assert reconcile._probe_cache["ok"] is True
        assert not reconcile._embeds_deferred(
            __import__(
                "palinode.core.ollama_client", fromlist=["get_ollama_client"]
            ).get_ollama_client()
        )


def test_synthetic_embedder_restores_the_probe_cache():
    from palinode.indexer import reconcile

    sentinel = {"ts": 123.0, "ok": False}
    reconcile._probe_cache.clear()
    reconcile._probe_cache.update(sentinel)
    with perf.synthetic_embedder():
        pass
    assert reconcile._probe_cache == sentinel


def test_synthetic_embedder_restores_the_real_embedder():
    from palinode.core import embedder as embedder_mod

    original = embedder_mod.embed
    with perf.synthetic_embedder():
        assert embedder_mod.embed is not original
    assert embedder_mod.embed is original


# ── the sweep ────────────────────────────────────────────────────────────────


def test_measure_point_writes_vectors_and_reports_hybrid(tmp_path, monkeypatch):
    """A tiny real sweep: vectors must land, and hybrid latency must be real."""
    monkeypatch.setattr(perf.tempfile, "mkdtemp", lambda **kw: str(tmp_path / "store"))
    os.makedirs(tmp_path / "store", exist_ok=True)
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **kw: None)

    point = perf.measure_point(
        40, seed=7, iters=2, files_for_target=20, use_synthetic=True
    )

    assert point.actual_chunks > 0
    assert point.num_vectors > 0, "synthetic run wrote no vectors"
    assert point.hybrid_p50_ms is not None and point.hybrid_p50_ms > 0
    assert point.keyword_p50_ms > 0
    assert point.chat_llm_calls == 0, "the ingest path must never call a chat model"
    assert point.db_bytes > 0
    assert point.db_bytes_per_chunk > 0


def test_measure_point_refuses_a_zero_vector_result(tmp_path, monkeypatch):
    """The guard that stops an FTS-only run being published as vector latency."""
    monkeypatch.setattr(
        perf.tempfile, "mkdtemp", lambda **kw: str(tmp_path / "store-novec")
    )
    os.makedirs(tmp_path / "store-novec", exist_ok=True)
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **kw: None)

    # use_synthetic=False with no embedder reachable → deferred FTS-only path.
    with harness.embedder_disabled():
        with pytest.raises(SystemExit, match="0 vectors"):
            perf.measure_point(
                40, seed=7, iters=1, files_for_target=20, use_synthetic=False
            )


def test_run_sweep_refuses_a_real_run_with_no_embedder():
    with harness.embedder_disabled():
        with pytest.raises(SystemExit, match="No embedder is reachable"):
            perf.run_sweep(
                (10,), label="x", seed=1, iters=1, use_synthetic=False
            )


# ── host description ─────────────────────────────────────────────────────────


def test_total_ram_prefers_meminfo_over_sysconf(tmp_path, monkeypatch):
    """Inside LXC, sysconf reports the *host's* RAM. meminfo must win.

    Measured on a 4 GiB container whose sysconf claimed 62.5 GiB — publishing
    the hypervisor's memory as the box's would misdescribe the machine the
    numbers came from.
    """
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        4194304 kB\nMemFree: 1 kB\n", encoding="utf-8")

    real_path = perf.Path
    monkeypatch.setattr(
        perf, "Path", lambda p="": meminfo if str(p) == "/proc/meminfo" else real_path(p)
    )
    assert perf._total_ram_bytes() == 4194304 * 1024


def test_total_ram_falls_back_when_meminfo_is_absent(monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    real_path = perf.Path
    monkeypatch.setattr(
        perf, "Path", lambda p="": missing if str(p) == "/proc/meminfo" else real_path(p)
    )
    value = perf._total_ram_bytes()
    assert value is None or value > 0


# ── calibration ──────────────────────────────────────────────────────────────


def test_chunks_per_file_refuses_a_zero_ratio(monkeypatch):
    """Calibration returning zero chunks must fail loudly, not divide by zero.

    The original failure: `embedder_disabled()` during calibration on a host
    that *has* an embedder writes no chunks at all, and the sweep died with a
    ZeroDivisionError several layers away from the cause.
    """
    from bench.harness import IngestResult

    def _empty(_dir):
        return IngestResult(
            num_files=100, num_facts=0, embed_calls=0, embed_input_chars=0,
            embed_input_tokens_approx=0, chat_llm_calls=0, wall_clock_s=0.1,
            embedded=False, num_vectors=0, chunks_written=0, chunks_unchanged=0,
        )

    monkeypatch.setattr(perf.harness, "index_all", _empty)
    with pytest.raises(SystemExit, match="cannot size the scale points"):
        perf._chunks_per_file(1337)


# ── reporting ────────────────────────────────────────────────────────────────


def _point(**over) -> perf.ScalePoint:
    base = dict(
        target_chunks=1000, actual_chunks=1000, num_files=500, num_vectors=1000,
        index_wall_s=2.2, files_per_s=227.0, chunks_per_s=450.0, embed_calls=1000,
        embeds_per_s=450.0, chat_llm_calls=0, hybrid_cold_p50_ms=7.0,
        hybrid_cold_p95_ms=8.0, hybrid_p50_ms=5.9, hybrid_p95_ms=6.7,
        keyword_p50_ms=1.3, keyword_p95_ms=1.4, n_queries=10, iters=20,
        rss_bytes=65_000_000, db_bytes=6_200_000, corpus_bytes=1_000_000,
        db_bytes_per_chunk=6193.0,
    )
    base.update(over)
    return perf.ScalePoint(**base)


def _host() -> perf.HostInfo:
    return perf.HostInfo(
        label="Test box", platform="linux", machine="x86_64", processor="Test CPU",
        cpu_count=8, total_ram_bytes=17_179_869_184, python_version="3.12.0",
        palinode_version="0.15.0", embedding_model="bge-m3", embedding_dimensions=1024,
    )


def test_markdown_flags_a_synthetic_run():
    out = perf.render_markdown(
        perf.PerfRun(
            host=_host(), synthetic_vectors=True, embedder_reachable=False,
            seed=1, points=[_point()],
        )
    )
    assert "Synthetic vectors" in out
    assert "recall quality is not measured" in out
    assert "5.9 ms" in out


def test_markdown_omits_the_flag_for_a_real_run():
    out = perf.render_markdown(
        perf.PerfRun(
            host=_host(), synthetic_vectors=False, embedder_reachable=True,
            seed=1, points=[_point()],
        )
    )
    assert "Synthetic vectors" not in out


def test_markdown_renders_a_missing_hybrid_number_as_na():
    out = perf.render_markdown(
        perf.PerfRun(
            host=_host(), synthetic_vectors=False, embedder_reachable=False,
            seed=1, points=[_point(hybrid_p50_ms=None, hybrid_p95_ms=None)],
        )
    )
    assert "n/a" in out


def test_source_tree_version_matches_pyproject():
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert perf._source_tree_version() == expected


def test_current_rss_is_positive_or_unavailable():
    rss = perf.current_rss_bytes()
    assert rss is None or rss > 0
