"""End-to-end benchmark orchestrator.

Runs the four axes against a throwaway ``PALINODE_DIR`` and emits a JSON
results object (consumed by :mod:`bench.report`).

    python -m bench.run --out /tmp/bench-results.json
    python -m bench.run --size 60 --runs 5 --iters 20 --out results.json

Axis 2 (determinism) spawns one subprocess per run so each ingest starts from
a genuinely clean process (fresh config, fresh caches), re-using a single
fixed store path so chunk identities line up for the byte-diff.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench import corpus, harness

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Environment metadata
# --------------------------------------------------------------------------

def _palinode_version() -> str:
    try:
        from importlib.metadata import version

        return version("palinode")
    except Exception:
        return "unknown"


def _env_block() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "palinode_version": _palinode_version(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "embedder_available": harness.embedder_available(),
    }


# --------------------------------------------------------------------------
# Single-ingest fingerprint (subprocess entry point for the determinism axis)
# --------------------------------------------------------------------------

def _fingerprint_run(seed: int, size: int) -> None:
    """Generate + index a corpus at ``$PALINODE_DIR`` and print its fingerprint.

    Invoked as a subprocess by :func:`run_determinism_axis`. ``PALINODE_DIR``
    is already set in the environment (before palinode imported), so the
    config resolved on import points at the clean store.
    """
    pd = os.environ["PALINODE_DIR"]
    harness.point_config_at(pd)
    corpus.generate(pd, seed=seed, size=size)
    harness.init_store()
    ingest = harness.index_all(pd)
    fp = harness.fingerprint_state(pd)
    print(json.dumps({"fingerprint": asdict(fp), "ingest": asdict(ingest)}))


# --------------------------------------------------------------------------
# Axes
# --------------------------------------------------------------------------

def run_cost_axis(palinode_dir: str, *, seed: int, size: int) -> dict[str, Any]:
    """Axis 1 — cost per remembered fact (measured, ours).

    Ingests the corpus once (primary cost), then re-ingests unchanged to show
    the SHA-256 dedup path does zero model work on a no-op pass.
    """
    corpus.generate(palinode_dir, seed=seed, size=size)
    harness.init_store()
    primary = harness.index_all(palinode_dir)
    dedup = harness.index_all(palinode_dir)  # second pass over unchanged files
    return {
        "primary_ingest": asdict(primary),
        "primary_ingest_derived": {
            "embed_calls_per_fact": primary.embed_calls_per_fact,
            "chat_calls_per_fact": primary.chat_calls_per_fact,
            "ms_per_fact": (primary.wall_clock_s * 1000.0 / primary.num_facts)
            if primary.num_facts else 0.0,
        },
        "reingest_unchanged": asdict(dedup),
    }


def run_determinism_axis(*, seed: int, size: int, n_runs: int) -> dict[str, Any]:
    """Axis 2 — determinism: N clean-state ingests, byte-diff the state."""
    det_dir = tempfile.mkdtemp(prefix="pnbench-det-")
    fingerprints: list[dict[str, Any]] = []
    try:
        for _ in range(n_runs):
            # Clean state: wipe the store dir before each subprocess ingest.
            shutil.rmtree(det_dir, ignore_errors=True)
            os.makedirs(det_dir, exist_ok=True)
            env = os.environ.copy()
            env["PALINODE_DIR"] = det_dir
            env["PALINODE_ALLOW_FRESH_DB"] = "1"
            proc = subprocess.run(
                [sys.executable, "-m", "bench.run", "--fingerprint-run",
                 "--seed", str(seed), "--size", str(size)],
                capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"fingerprint subprocess failed (rc={proc.returncode}):\n{proc.stderr}"
                )
            # The fingerprint JSON is the last non-empty stdout line (config
            # banners go to stderr, but be defensive).
            line = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
            fingerprints.append(json.loads(line)["fingerprint"])
    finally:
        shutil.rmtree(det_dir, ignore_errors=True)

    files_hashes = {fp["files_sha256"] for fp in fingerprints}
    db_hashes = {fp["db_logical_sha256"] for fp in fingerprints}
    vec_hashes = {fp["vec_ids_sha256"] for fp in fingerprints}
    return {
        "n_runs": n_runs,
        "files_identical": len(files_hashes) == 1,
        "db_logical_identical": len(db_hashes) == 1,
        "vec_ids_identical": len(vec_hashes) == 1,
        "distinct_files_hashes": sorted(files_hashes),
        "distinct_db_hashes": sorted(db_hashes),
        "fingerprints": fingerprints,
    }


def run_recall_axis(palinode_dir: str, queries: tuple[str, ...], *, iters: int) -> dict[str, Any]:
    """Axis 3 — LLM-free recall latency (hybrid if an embedder is up)."""
    hybrid = harness.measure_recall_hybrid(queries, iters=iters)
    keyword = harness.measure_recall_keyword(queries, iters=iters)
    return {
        "hybrid": asdict(hybrid) if hybrid is not None else None,
        "hybrid_unavailable_reason": None if hybrid is not None else "no embedder reachable",
        "keyword": asdict(keyword),
    }


def run_degradation_axis(palinode_dir: str, queries: tuple[str, ...], *, iters: int) -> dict[str, Any]:
    """Axis 4 — degradation floor: keyword-only recall, then grep-over-files.

    The keyword measurement runs with the embedder **actively disabled**, not
    merely unused — otherwise, on an embedder-up host, the report would assert
    a condition that never held (and would be identical to the axis-3 keyword
    numbers by construction).
    """
    keyword = harness.measure_recall_keyword(queries, iters=iters, disable_embedder=True)
    grep = harness.measure_recall_grep(palinode_dir, queries, iters=max(1, iters // 4))
    return {
        "keyword_only_embedder_disabled": asdict(keyword),
        "grep_over_files": asdict(grep),
    }


# --------------------------------------------------------------------------
# Full suite
# --------------------------------------------------------------------------

def run_all(*, seed: int, size: int, n_runs: int, iters: int) -> dict[str, Any]:
    """Run all four axes; return the full results object."""
    primary_dir = tempfile.mkdtemp(prefix="pnbench-main-")
    try:
        harness.point_config_at(primary_dir)
        cost = run_cost_axis(primary_dir, seed=seed, size=size)
        queries = corpus.TOPICS
        recall = run_recall_axis(primary_dir, queries, iters=iters)
        degradation = run_degradation_axis(primary_dir, queries, iters=iters)
    finally:
        shutil.rmtree(primary_dir, ignore_errors=True)

    determinism = run_determinism_axis(seed=seed, size=size, n_runs=n_runs)

    return {
        "environment": _env_block(),
        "parameters": {"seed": seed, "size": size, "runs": n_runs, "iters": iters},
        "axis1_cost_per_fact": cost,
        "axis2_determinism": determinism,
        "axis3_recall_latency": recall,
        "axis4_degradation_floor": degradation,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Palinode ingestion & recall benchmark")
    parser.add_argument("--fingerprint-run", action="store_true",
                        help="internal: index $PALINODE_DIR and print a state fingerprint")
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--size", type=int, default=60, help="corpus size (files)")
    parser.add_argument("--runs", type=int, default=5, help="determinism runs (N>=5)")
    parser.add_argument("--iters", type=int, default=20, help="recall iterations per query")
    parser.add_argument("--out", type=str, default=None, help="write results JSON to this path")
    args = parser.parse_args(argv)

    if args.fingerprint_run:
        _fingerprint_run(args.seed, args.size)
        return 0

    results = run_all(seed=args.seed, size=args.size, n_runs=args.runs, iters=args.iters)
    payload = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
