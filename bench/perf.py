"""Operating-numbers sweep: search latency, index throughput, RAM and disk at scale.

This is the rig behind ``docs/PERFORMANCE.md``. It answers the one question a
prospective self-hoster asks before installing anything — *will this be fast
enough for a store my size* — at 1k / 10k / 50k indexed chunks.

It deliberately reuses :mod:`bench.harness` and :mod:`bench.corpus` rather than
standing up a second rig: the corpus generator is already a pure function of
``(seed, size)``, ``index_all`` already drives the canonical
parse → dedup → embed → upsert pipeline under model-call counters, and
``measure_recall_*`` already reports cold/warm p50/p95. What is new here is the
scale axis, the resource measurements, and the host record.

Two measurement modes, and the difference is load-bearing:

**Real embedder** (default when one is reachable) — the honest end-to-end
number. Both index throughput and hybrid-search latency are real. Costs one
embedding per chunk, so 50k chunks is a real GPU bill.

**Synthetic vectors** (``--synthetic-vectors``) — the write path, the vector
table and the search path are all real; only the *content* of each vector is a
deterministic hash of the text rather than a model output. Vector-scan latency
is a function of how many fixed-width vectors sqlite-vec walks, not of what is
in them, so **latency and throughput numbers stay valid** while the embedding
bill goes to zero. What becomes meaningless is **recall quality**, so this
module never reports a quality metric, in either mode — that is
``bench/run.py``'s job, and conflating the two is how a benchmark starts
lying. Any run in this mode is stamped ``synthetic_vectors: true`` in the JSON
and must be labelled as such wherever it is published.
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import json
import math
import os
import platform
import shutil
import struct
import subprocess  # nosec B404 — fixed argv, no shell, used only to read RSS
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from bench import corpus as corpus_mod
from bench import harness

#: Chunk-count targets the published table reports.
DEFAULT_TARGETS: tuple[int, ...] = (1_000, 10_000, 50_000)

#: Files generated to calibrate the chunks-per-file ratio before scaling up.
_PROBE_FILES: int = 100


# ─────────────────────────────────────────────────────────────────────────────
# Resource measurement
# ─────────────────────────────────────────────────────────────────────────────


def current_rss_bytes() -> int | None:
    """Resident set size of this process, or ``None`` if it cannot be read.

    Peak RSS (``getrusage``) is deliberately not used: it is monotonic across
    the whole process, so in a sweep it would report the largest scale point's
    footprint for every earlier one. This reads *current* RSS instead, which is
    what "RAM at N chunks" means.
    """
    statm = Path("/proc/self/statm")
    if statm.exists():  # Linux
        try:
            resident_pages = int(statm.read_text().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    try:  # macOS and other POSIX
        out = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
            ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return int(out.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, name))
    return total


def db_size_bytes() -> int:
    """Size of the SQLite store including its WAL and shm sidecars."""
    from palinode.core.config import config

    total = 0
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            total += os.path.getsize(f"{config.db_path}{suffix}")
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic vectors
# ─────────────────────────────────────────────────────────────────────────────


def _deterministic_vector(text: str, dimensions: int) -> list[float]:
    """A unit vector derived from the text's digest.

    Deterministic (the same text always yields the same vector, so dedup and
    re-index behave as they do in production) and normalised (so distances land
    in the range sqlite-vec actually sees). Semantically meaningless by
    construction — see the module docstring.
    """
    raw = b""
    counter = 0
    needed = dimensions * 4
    while len(raw) < needed:
        raw += hashlib.sha256(f"{text}|{counter}".encode("utf-8")).digest()
        counter += 1
    # Unpack as unsigned ints and map to [-1, 1] rather than reinterpreting the
    # digest bytes as IEEE-754 floats directly: a good fraction of random 4-byte
    # patterns are NaN or infinity, which poisons the norm and lands unusable
    # vectors in the store. Caught by the normalisation assertion in
    # tests/test_bench_perf.py after a full sweep had already run on them.
    ints = struct.unpack(f"{dimensions}I", raw[:needed])
    values = [(i / 2_147_483_648.0) - 1.0 for i in ints]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


@contextlib.contextmanager
def synthetic_embedder() -> Iterator[None]:
    """Replace the embedder with the deterministic hash vector for this block.

    Mirrors :func:`bench.harness.embedder_disabled` — same seam, opposite
    intent: that one proves the no-embedder floor, this one exercises the full
    vector path without paying an embedding bill.
    """
    from palinode.core import embedder as embedder_mod
    from palinode.core.config import config
    from palinode.core.ollama_client import get_ollama_client

    from palinode.indexer import reconcile

    dimensions = int(config.embeddings.primary.dimensions)
    client = get_ollama_client()
    orig_embed = embedder_mod.embed
    orig_probe = client.probe_embed
    orig_cache = dict(reconcile._probe_cache)

    embedder_mod.embed = lambda text: _deterministic_vector(text, dimensions)  # type: ignore[assignment]
    client.probe_embed = lambda **kwargs: True  # type: ignore[method-assign]
    # The indexer caches a FAILED probe for `_PROBE_TTL_S` (30 s) so a cold or
    # absent embedder does not make every section pay a full timeout. That is
    # correct in production and a trap here: without clearing it, a probe that
    # failed before this block was entered keeps the whole first scale point on
    # the FTS-only deferred path, and the run silently reports vector-search
    # latency for a store holding no vectors. Found exactly that way — the
    # first two points of a sweep came back with num_vectors=0 while the third,
    # which happened to outlast the TTL, embedded normally.
    reconcile._probe_cache.update({"ts": 0.0, "ok": True})
    try:
        yield
    finally:
        embedder_mod.embed = orig_embed  # type: ignore[assignment]
        client.probe_embed = orig_probe  # type: ignore[method-assign]
        reconcile._probe_cache.clear()
        reconcile._probe_cache.update(orig_cache)


# ─────────────────────────────────────────────────────────────────────────────
# Result shapes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HostInfo:
    """The box, stated plainly, so a reader can place the numbers."""

    label: str
    platform: str
    machine: str
    processor: str
    cpu_count: int | None
    total_ram_bytes: int | None
    python_version: str
    palinode_version: str
    embedding_model: str
    embedding_dimensions: int
    virtualization: str = "unknown"


@dataclass
class ScalePoint:
    """One row of the published table."""

    target_chunks: int
    actual_chunks: int
    num_files: int
    num_vectors: int
    # Ingest
    index_wall_s: float
    files_per_s: float
    chunks_per_s: float
    embed_calls: int
    embeds_per_s: float
    chat_llm_calls: int
    # Recall (warm; cold reported alongside for the first-query story)
    hybrid_cold_p50_ms: float | None
    hybrid_cold_p95_ms: float | None
    hybrid_p50_ms: float | None
    hybrid_p95_ms: float | None
    keyword_p50_ms: float
    keyword_p95_ms: float
    n_queries: int
    iters: int
    # Footprint
    rss_bytes: int | None
    db_bytes: int
    corpus_bytes: int
    db_bytes_per_chunk: float


@dataclass
class PerfRun:
    host: HostInfo
    synthetic_vectors: bool
    embedder_reachable: bool
    seed: int
    points: list[ScalePoint] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Measurement
# ─────────────────────────────────────────────────────────────────────────────


def _source_tree_version() -> str:
    """Version from the checkout's ``pyproject.toml``, not installed metadata.

    ``bench/`` only ever runs from a source tree, and an editable install's
    recorded version goes stale the moment ``pyproject.toml`` is bumped without
    a reinstall. A published performance table attributing its numbers to the
    wrong release is worse than one saying "unknown", so the source tree wins
    and installed metadata is only the fallback.
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with contextlib.suppress(OSError, ValueError, StopIteration):
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    with contextlib.suppress(Exception):
        from importlib.metadata import version as _pkg_version

        return _pkg_version("palinode")
    return "unknown"


def _total_ram_bytes() -> int | None:
    """Total RAM, preferring ``/proc/meminfo`` over ``sysconf``.

    Inside an LXC container ``sysconf("SC_PHYS_PAGES")`` reports the *host's*
    memory, not the container's: measured on a 4 GiB container that returns
    62.5 GiB from sysconf while lxcfs-backed ``/proc/meminfo`` correctly says
    4 GiB. Publishing the hypervisor's RAM as the box's would misdescribe the
    machine the numbers came from, so meminfo wins where it exists.
    """
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    with contextlib.suppress(OSError, ValueError, AttributeError):
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return None


def _virtualization() -> str:
    """"lxc", "kvm", "none"… — part of describing the box honestly."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
            ["systemd-detect-virt"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.stdout.strip():
            return out.stdout.strip()
    return "unknown"


def _cpu_brand() -> str:
    """A human-recognisable CPU name. ``platform.processor()`` says "arm"."""
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if out.stdout.strip():
                return out.stdout.strip()
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def collect_host_info(label: str) -> HostInfo:
    from palinode.core.config import config

    palinode_version = _source_tree_version()

    total_ram = _total_ram_bytes()

    return HostInfo(
        label=label,
        platform=platform.platform(),
        machine=platform.machine(),
        processor=_cpu_brand(),
        cpu_count=os.cpu_count(),
        total_ram_bytes=total_ram,
        python_version=platform.python_version(),
        palinode_version=palinode_version,
        embedding_model=str(config.embeddings.primary.model),
        embedding_dimensions=int(config.embeddings.primary.dimensions),
        virtualization=_virtualization(),
    )


def _chunks_per_file(seed: int) -> float:
    """Calibrate the corpus generator's chunks-per-file ratio.

    The generator makes every fourth file multi-section, so the ratio is a
    property of the generator rather than a constant worth hard-coding. Probing
    it keeps the scale targets honest if the generator ever changes.
    """
    with tempfile.TemporaryDirectory(prefix="palinode-perf-probe-") as tmp:
        harness.point_config_at(tmp)
        corpus_mod.generate(tmp, seed=seed, size=_PROBE_FILES)
        harness.init_store()
        # Calibration counts chunks, and chunk count does not depend on what is
        # in a vector — so it runs on synthetic vectors in BOTH modes. Using
        # `embedder_disabled()` here instead is a trap on a host that has a
        # working embedder: the reachability check earlier in the sweep sets
        # `has_embedded_ok`, so the indexer stops deferring, embeds the empty
        # vector the disabled embedder returns, writes nothing, and calibration
        # comes back as zero chunks per file.
        with synthetic_embedder():
            result = harness.index_all(tmp)
        ratio = result.num_facts / _PROBE_FILES if _PROBE_FILES else 0.0
        if ratio <= 0:
            raise SystemExit(
                f"calibration indexed {result.num_facts} chunks from "
                f"{_PROBE_FILES} files — cannot size the scale points. The "
                "corpus generator or the index pipeline is not writing chunks."
            )
        return ratio


def measure_point(
    target_chunks: int,
    *,
    seed: int,
    iters: int,
    files_for_target: int,
    use_synthetic: bool,
) -> ScalePoint:
    """Generate, index and measure one scale point in an isolated store."""
    tmp = tempfile.mkdtemp(prefix=f"palinode-perf-{target_chunks}-")
    try:
        harness.point_config_at(tmp)
        generated = corpus_mod.generate(tmp, seed=seed, size=files_for_target)
        harness.init_store()

        with contextlib.ExitStack() as stack:
            if use_synthetic:
                stack.enter_context(synthetic_embedder())
            ingest = harness.index_all(tmp)
            # Recall is measured inside the same stack so the synthetic
            # embedder also serves the query vector — otherwise the hybrid
            # path would be measured against an embedder that is not there.
            hybrid = harness.measure_recall_hybrid(
                generated.queries, iters=iters, top_k=10
            )
            keyword = harness.measure_recall_keyword(
                generated.queries, iters=iters, top_k=10
            )

        # A run that reports "hybrid p50" for a store with no vectors in it is
        # the worst failure this rig can have: the number looks plausible and
        # is measuring FTS alone. Refuse to return such a point.
        if ingest.num_vectors == 0:
            raise SystemExit(
                f"{target_chunks:,}-chunk point indexed {ingest.num_facts:,} "
                "chunks but wrote 0 vectors — every section took the deferred "
                "FTS-only path, so no vector-search number from this run is "
                "meaningful. Check embedder reachability and the indexer's "
                "probe cache before trusting any output."
            )

        gc.collect()
        rss = current_rss_bytes()
        db_bytes = db_size_bytes()
        corpus_bytes = dir_size_bytes(tmp)

        wall = ingest.wall_clock_s or 0.0
        return ScalePoint(
            target_chunks=target_chunks,
            actual_chunks=ingest.num_facts,
            num_files=ingest.num_files,
            num_vectors=ingest.num_vectors,
            index_wall_s=round(wall, 3),
            files_per_s=round(ingest.num_files / wall, 2) if wall else 0.0,
            chunks_per_s=round(ingest.num_facts / wall, 2) if wall else 0.0,
            embed_calls=ingest.embed_calls,
            embeds_per_s=round(ingest.embed_calls / wall, 2) if wall else 0.0,
            chat_llm_calls=ingest.chat_llm_calls,
            hybrid_cold_p50_ms=round(hybrid.cold_p50_ms, 3) if hybrid else None,
            hybrid_cold_p95_ms=round(hybrid.cold_p95_ms, 3) if hybrid else None,
            hybrid_p50_ms=round(hybrid.warm_p50_ms, 3) if hybrid else None,
            hybrid_p95_ms=round(hybrid.warm_p95_ms, 3) if hybrid else None,
            keyword_p50_ms=round(keyword.warm_p50_ms, 3),
            keyword_p95_ms=round(keyword.warm_p95_ms, 3),
            n_queries=len(generated.queries),
            iters=iters,
            rss_bytes=rss,
            db_bytes=db_bytes,
            corpus_bytes=corpus_bytes,
            db_bytes_per_chunk=(
                round(db_bytes / ingest.num_facts, 1) if ingest.num_facts else 0.0
            ),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_sweep(
    targets: tuple[int, ...],
    *,
    label: str,
    seed: int,
    iters: int,
    use_synthetic: bool,
) -> PerfRun:
    reachable = harness.embedder_available()
    if not use_synthetic and not reachable:
        raise SystemExit(
            "No embedder is reachable, so hybrid-search latency and embedding "
            "throughput cannot be measured. Either point the config at an "
            "Ollama host, or re-run with --synthetic-vectors for the "
            "latency-only sweep (see the module docstring for what that does "
            "and does not measure)."
        )

    run = PerfRun(
        host=collect_host_info(label),
        synthetic_vectors=use_synthetic,
        embedder_reachable=reachable,
        seed=seed,
    )

    ratio = _chunks_per_file(seed)
    for target in targets:
        files = max(1, round(target / ratio))
        print(
            f"→ {target:,} chunks (~{files:,} files, {ratio:.2f} chunks/file)…",
            file=sys.stderr,
            flush=True,
        )
        run.points.append(
            measure_point(
                target,
                seed=seed,
                iters=iters,
                files_for_target=files,
                use_synthetic=use_synthetic,
            )
        )
    return run


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


def _mib(value: int | None) -> str:
    return "n/a" if value is None else f"{value / (1024 * 1024):.1f} MiB"


def _gib(value: int | None) -> str:
    return "n/a" if value is None else f"{value / (1024 ** 3):.0f} GiB"


def render_markdown(run: PerfRun) -> str:
    lines: list[str] = []
    host = run.host
    lines.append(f"**Box:** {host.label}")
    lines.append("")
    lines.append(
        f"- {host.processor}, {host.cpu_count} cores, "
        f"{_gib(host.total_ram_bytes)} RAM, {host.platform}"
        + (f", {host.virtualization}" if host.virtualization not in ("unknown", "none") else "")
    )
    lines.append(
        f"- palinode {host.palinode_version}, Python {host.python_version}, "
        f"embedding model `{host.embedding_model}` ({host.embedding_dimensions}d)"
    )
    if run.synthetic_vectors:
        lines.append(
            "- **Synthetic vectors** — latency and throughput are real; recall "
            "quality is not measured and must not be inferred from this run."
        )
    lines.append("")
    lines.append(
        "| chunks | files | index wall | chunks/s | hybrid p50 | hybrid p95 "
        "| keyword p50 | keyword p95 | RSS | DB on disk | bytes/chunk |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in run.points:
        hybrid_p50 = "n/a" if p.hybrid_p50_ms is None else f"{p.hybrid_p50_ms:.1f} ms"
        hybrid_p95 = "n/a" if p.hybrid_p95_ms is None else f"{p.hybrid_p95_ms:.1f} ms"
        lines.append(
            f"| {p.actual_chunks:,} | {p.num_files:,} | {p.index_wall_s:.1f} s "
            f"| {p.chunks_per_s:,.0f} | {hybrid_p50} | {hybrid_p95} "
            f"| {p.keyword_p50_ms:.1f} ms | {p.keyword_p95_ms:.1f} ms "
            f"| {_mib(p.rss_bytes)} | {_mib(p.db_bytes)} | {p.db_bytes_per_chunk:,.0f} |"
        )
    lines.append("")
    lines.append(
        f"Latency percentiles are warm, over {run.points[0].n_queries} queries "
        f"× {run.points[0].iters} iterations each."
        if run.points
        else ""
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench.perf",
        description="Measure search latency, index throughput, RAM and disk at scale.",
    )
    parser.add_argument(
        "--sizes",
        type=str,
        default=",".join(str(t) for t in DEFAULT_TARGETS),
        help="comma-separated chunk-count targets (default: 1000,10000,50000)",
    )
    parser.add_argument(
        "--label",
        type=str,
        required=True,
        help='how to name this box in the published table (e.g. "Mac mini M2, 16 GB")',
    )
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument(
        "--iters", type=int, default=20, help="warm iterations per query"
    )
    parser.add_argument(
        "--synthetic-vectors",
        action="store_true",
        help=(
            "use deterministic hash vectors instead of a real embedder — "
            "latency/throughput stay valid, recall quality is not measured"
        ),
    )
    parser.add_argument("--out", type=str, default=None, help="write results JSON here")
    parser.add_argument(
        "--markdown", action="store_true", help="print a markdown table to stdout"
    )
    args = parser.parse_args(argv)

    targets = tuple(int(s.strip()) for s in args.sizes.split(",") if s.strip())
    run = run_sweep(
        targets,
        label=args.label,
        seed=args.seed,
        iters=args.iters,
        use_synthetic=args.synthetic_vectors,
    )

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(asdict(run), indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out}", file=sys.stderr)

    if args.markdown or not args.out:
        print(render_markdown(run))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
