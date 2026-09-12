"""Standalone measurement of search abstention on a seeded corpus.

The evaluation asks two questions together:

* How often does search return at least one result when the seeded store has
  no true answer?
* How often do answer-present controls retain their true result as the
  per-arm relevance threshold increases?

It deliberately uses the real index, SQLite-vec, FTS5, ranking pipeline, and
configured embedding endpoint. Production search defaults are never changed.

Protocol provenance: this pinned query set supersedes the exploratory run at
https://github.com/phasespace-labs/palinode/issues/73#issuecomment-5421219548.
Packaging the experiment replaced all 26 no-answer strings and reworded all
10 paraphrase controls; corpus seeds, size, exact controls, top-k, and thresholds
stayed the same. The 2026-08-26 14:24 UTC BGE-M3 run reported in
https://github.com/phasespace-labs/palinode/pull/151 uses this pinned set: at 0.45
it returned hits for 25/78 no-answer queries, not the exploratory set's 3/78.

Run JSON and Markdown forms independently of the stable benchmark runner::

    python -m bench.abstention --out abstention.json
    python -m bench.abstention --format markdown --out abstention.md
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Sequence

from bench import corpus, harness


ABSENT_NATURAL_QUERIES: tuple[str, ...] = (
    "Which region hosts the disaster recovery control plane?",
    "Who approved the quantum-resistant signing rollout?",
    "When does the mobile release enter beta?",
    "What is the payroll retention policy?",
    "How many GPU nodes are reserved for training?",
    "Which vendor supplies the observability pipeline?",
    "Why was the message broker replaced?",
    "Where are customer encryption keys rotated?",
    "What caused the invoice reconciliation outage?",
    "Who owns the Kubernetes cluster upgrade?",
)

ABSENT_KEYWORD_QUERIES: tuple[str, ...] = (
    "disaster recovery",
    "quantum signing",
    "mobile beta",
    "payroll retention",
    "gpu reservation",
    "observability vendor",
    "message broker",
    "encryption keys",
    "invoice outage",
    "kubernetes upgrade",
)

ABSENT_IDENTIFIER_QUERIES: tuple[str, ...] = (
    "INC-9472",
    "RFC-8841",
    "JIRA-PLAT-611",
    "customer_eu_2048",
    "sha256:deadbeefcafefeed",
    "urn:palinode:absent:42",
)

PARAPHRASE_CONTROLS: tuple[tuple[str, str], ...] = (
    ("moving persistent data to a new schema", "database migration"),
    ("plan for reusing cached responses", "caching strategy"),
    ("renewing authentication credentials safely", "auth token rotation"),
    ("ordering retrieval results by relevance", "search ranking"),
    ("checking configuration values before startup", "config validation"),
    ("spacing repeated attempts after failures", "retry backoff"),
    ("recreating the search index from source files", "index rebuild"),
    ("controlling how many requests are accepted", "rate limiting"),
    ("tracking changes to stored data formats", "schema versioning"),
    ("finishing queued work before shutdown", "queue draining"),
)

ABSENT_KINDS = frozenset(
    {"absent_natural", "absent_keyword", "absent_identifier"}
)
CONTROL_KINDS = frozenset({"control_exact", "control_paraphrase"})
QUERY_KINDS = ABSENT_KINDS | CONTROL_KINDS
MODES: tuple[str, ...] = ("vector", "hybrid")


@dataclass(frozen=True)
class QueryCase:
    """One pinned query and, for controls, the topic that must be retrieved."""

    case_id: str
    kind: str
    query: str
    expected_topic: str | None = None


DEFAULT_QUERY_CASES: tuple[QueryCase, ...] = (
    *(
        QueryCase(f"absent-natural-{index:02d}", "absent_natural", query)
        for index, query in enumerate(ABSENT_NATURAL_QUERIES, start=1)
    ),
    *(
        QueryCase(f"absent-keyword-{index:02d}", "absent_keyword", query)
        for index, query in enumerate(ABSENT_KEYWORD_QUERIES, start=1)
    ),
    *(
        QueryCase(f"absent-identifier-{index:02d}", "absent_identifier", query)
        for index, query in enumerate(ABSENT_IDENTIFIER_QUERIES, start=1)
    ),
    *(
        QueryCase(f"control-exact-{index:02d}", "control_exact", topic, topic)
        for index, topic in enumerate(corpus.TOPICS, start=1)
    ),
    *(
        QueryCase(
            f"control-paraphrase-{index:02d}",
            "control_paraphrase",
            query,
            topic,
        )
        for index, (query, topic) in enumerate(PARAPHRASE_CONTROLS, start=1)
    ),
)


def query_kind_counts(cases: Sequence[QueryCase]) -> dict[str, int]:
    """Return the number of pinned cases in each query shape."""
    return dict(Counter(case.kind for case in cases))


def _validate_protocol(cases: Sequence[QueryCase]) -> None:
    if not cases:
        raise ValueError("at least one query case is required")
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("query case ids must be unique")
    for case in cases:
        if case.kind not in QUERY_KINDS:
            raise ValueError(f"unsupported query kind: {case.kind}")
        if not case.query.strip():
            raise ValueError(f"query case {case.case_id} is empty")
        if case.kind in ABSENT_KINDS and case.expected_topic is not None:
            raise ValueError(f"absent case {case.case_id} cannot have a true topic")
        if case.kind in CONTROL_KINDS and not case.expected_topic:
            raise ValueError(f"control case {case.case_id} needs a true topic")


def _score_stats(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def summarize_observations(
    observations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize query-level false positives and answer-present controls."""
    absent = [row for row in observations if row["kind"] in ABSENT_KINDS]
    false_positives = [row for row in absent if row["returned_count"] > 0]

    by_kind: dict[str, dict[str, int | float]] = {}
    for kind in sorted(ABSENT_KINDS):
        rows = [row for row in absent if row["kind"] == kind]
        if not rows:
            continue
        returned = sum(row["returned_count"] > 0 for row in rows)
        by_kind[kind] = {
            "returned": returned,
            "total": len(rows),
            "false_positive_rate": returned / len(rows),
        }

    controls: dict[str, dict[str, int]] = {}
    for label, kind in (
        ("exact", "control_exact"),
        ("paraphrase", "control_paraphrase"),
    ):
        rows = [row for row in observations if row["kind"] == kind]
        controls[label] = {
            "true_hits": sum(row["true_match"] is True for row in rows),
            "total": len(rows),
        }

    returned = len(false_positives)
    total = len(absent)
    return {
        "no_answer": {
            "returned": returned,
            "total": total,
            "false_positive_rate": returned / total if total else 0.0,
        },
        "no_answer_by_kind": by_kind,
        "controls": controls,
        # Results that reached the merged set through BM25 alone. Zero in the
        # vector arm by construction, and the number that says whether the
        # hybrid arm added anything at this floor.
        "fts_only_results": sum(row.get("fts_only_count") or 0 for row in observations),
        "false_positive_scores": {
            "fused": _score_stats(
                [
                    float(row["top_score"])
                    for row in false_positives
                    if row["top_score"] is not None
                ]
            ),
            "raw": _score_stats(
                [
                    float(row["top_raw_score"])
                    for row in false_positives
                    if row["top_raw_score"] is not None
                ]
            ),
        },
    }


def _matches_topic(result: dict[str, Any], expected_topic: str) -> bool:
    return expected_topic.casefold() in str(result.get("content", "")).casefold()


def _result_key(result: dict[str, Any]) -> str:
    """The identity the ranker fuses and dedups on."""
    return f"{result['file_path']}#{result.get('section_id', 'root')}"


def _observation(case: QueryCase, results: list[dict[str, Any]]) -> dict[str, Any]:
    top = results[0] if results else None
    true_match_rank: int | None = None
    best_true_raw_score: float | None = None
    if case.expected_topic is not None:
        for rank, result in enumerate(results, start=1):
            if not _matches_topic(result, case.expected_topic):
                continue
            if true_match_rank is None:
                true_match_rank = rank
            raw_score = result.get("raw_score")
            if raw_score is not None:
                raw_score = float(raw_score)
                best_true_raw_score = (
                    raw_score
                    if best_true_raw_score is None
                    else max(best_true_raw_score, raw_score)
                )

    return {
        "case_id": case.case_id,
        "kind": case.kind,
        "query": case.query,
        "expected_topic": case.expected_topic,
        "returned_count": len(results),
        "top_score": float(top["score"]) if top is not None else None,
        "top_raw_score": (
            float(top["raw_score"])
            if top is not None and top.get("raw_score") is not None
            else None
        ),
        "true_match": true_match_rank is not None
        if case.expected_topic is not None
        else None,
        "true_match_rank": true_match_rank,
        "best_true_raw_score": best_true_raw_score,
        # What the arms actually returned, in order. The counted metrics above
        # are blind to a reordering, so without these the vector and hybrid
        # rows are identical whenever BM25 changes rank but not membership.
        "result_keys": [_result_key(result) for result in results],
        # rank_hybrid attaches raw_score only to candidates the vector arm
        # produced, so a result without one reached the merged set through
        # BM25 alone.
        "fts_only_count": sum(1 for result in results if result.get("raw_score") is None),
    }


def _measure(
    cases: Sequence[QueryCase],
    query_vectors: dict[str, list[float]],
    *,
    threshold: float,
    top_k: int,
    mode: str,
) -> dict[str, Any]:
    from palinode.core import store

    use_fts = mode == "hybrid"
    observations = []
    for case in cases:
        results = store.search_hybrid(
            case.query,
            query_vectors[case.case_id],
            top_k=top_k,
            threshold=threshold,
            use_fts=use_fts,
            record_access=False,
        )
        observations.append(_observation(case, results))
    return {
        "mode": mode,
        "threshold": threshold,
        "summary": summarize_observations(observations),
        "observations": observations,
    }


def _fts_candidate_stats(
    cases: Sequence[QueryCase], *, top_k: int
) -> dict[str, Any]:
    """The BM25 candidate scores the per-arm floor is applied to.

    ``search_fts`` normalizes BM25 as ``min(abs(rank) / 25.0, 1.0)``, a scale
    with no relation to the cosine similarity the vector arm is scored on.
    One threshold is applied to both, so this records where BM25 actually
    lands on that shared axis.
    """
    from palinode.core import store

    scores: list[float] = []
    cases_with_candidates = 0
    for case in cases:
        rows = store.search_fts(case.query, top_k=top_k * 2)
        if not rows:
            continue
        cases_with_candidates += 1
        scores.extend(float(row.get("score", 0.0)) for row in rows)
    return {
        "cases": len(cases),
        "cases_with_candidates": cases_with_candidates,
        "candidates": len(scores),
        "score_stats": _score_stats(scores),
    }


def compare_arms(
    vector_measurement: dict[str, Any], hybrid_measurement: dict[str, Any]
) -> dict[str, Any]:
    """How the hybrid arm differs from the vector arm at one floor.

    The counted metrics cannot see this. Two arms that return the same number
    of results for every case, with the same control hits, produce identical
    summary rows whether BM25 changed the ordering or was dropped entirely.
    """
    vector_rows = {row["case_id"]: row for row in vector_measurement["observations"]}
    hybrid_rows = {row["case_id"]: row for row in hybrid_measurement["observations"]}
    shared = sorted(vector_rows.keys() & hybrid_rows.keys())

    differing = 0
    reordered = 0
    membership_changed = 0
    for case_id in shared:
        vector_keys = vector_rows[case_id]["result_keys"]
        hybrid_keys = hybrid_rows[case_id]["result_keys"]
        if vector_keys == hybrid_keys:
            continue
        differing += 1
        if set(vector_keys) == set(hybrid_keys):
            reordered += 1
        else:
            membership_changed += 1

    return {
        "threshold": vector_measurement["threshold"],
        "cases": len(shared),
        "cases_differing": differing,
        "cases_reordered_only": reordered,
        "cases_membership_changed": membership_changed,
        "fts_only_results": hybrid_measurement["summary"]["fts_only_results"],
    }


def _arm_comparisons(
    measurements: Sequence[dict[str, Any]], thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    comparisons = []
    for threshold in thresholds:
        vector_measurement = next(
            row
            for row in measurements
            if row["mode"] == "vector" and row["threshold"] == threshold
        )
        hybrid_measurement = next(
            row
            for row in measurements
            if row["mode"] == "hybrid" and row["threshold"] == threshold
        )
        comparisons.append(compare_arms(vector_measurement, hybrid_measurement))
    return comparisons


def _aggregate(
    runs: Sequence[dict[str, Any]], thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    aggregate = []
    for mode in MODES:
        for threshold in thresholds:
            observations = []
            for run in runs:
                row = next(
                    measurement
                    for measurement in run["measurements"]
                    if measurement["mode"] == mode
                    and measurement["threshold"] == threshold
                )
                observations.extend(row["observations"])
            aggregate.append(
                {
                    "mode": mode,
                    "threshold": threshold,
                    "summary": summarize_observations(observations),
                }
            )
    return aggregate


def _aggregate_arm_comparisons(
    runs: Sequence[dict[str, Any]], thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    """Sum the per-seed arm comparisons at each floor."""
    aggregate = []
    for threshold in thresholds:
        rows = [
            row
            for run in runs
            for row in run["arm_comparison"]
            if row["threshold"] == threshold
        ]
        aggregate.append(
            {
                "threshold": threshold,
                "cases": sum(row["cases"] for row in rows),
                "cases_differing": sum(row["cases_differing"] for row in rows),
                "cases_reordered_only": sum(row["cases_reordered_only"] for row in rows),
                "cases_membership_changed": sum(
                    row["cases_membership_changed"] for row in rows
                ),
                "fts_only_results": sum(row["fts_only_results"] for row in rows),
            }
        )
    return aggregate


def _package_version() -> str:
    try:
        return version("palinode")
    except PackageNotFoundError:
        return "unknown"


def evaluate(
    *,
    seeds: Sequence[int] = (1337, 2026, 9001),
    size: int = 60,
    thresholds: Sequence[float] = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60),
    top_k: int = 5,
    cases: Sequence[QueryCase] = DEFAULT_QUERY_CASES,
) -> dict[str, Any]:
    """Run the abstention protocol against real embedded SQLite stores."""
    from palinode.core import embedder
    from palinode.core.config import config

    seeds = tuple(seeds)
    thresholds = tuple(float(value) for value in thresholds)
    cases = tuple(cases)
    _validate_protocol(cases)
    if not seeds:
        raise ValueError("at least one corpus seed is required")
    if size <= 0:
        raise ValueError("corpus size must be positive")
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if not thresholds or any(value < 0.0 or value > 1.0 for value in thresholds):
        raise ValueError("thresholds must contain values between 0.0 and 1.0")
    dimensions = int(config.embeddings.primary.dimensions)
    query_vectors: dict[str, list[float]] = {}
    try:
        for case in cases:
            vector = embedder.embed(case.query)
            if len(vector) != dimensions:
                raise RuntimeError(
                    f"embedder returned {len(vector)} dimensions; expected {dimensions}"
                )
            query_vectors[case.case_id] = vector
    except embedder.EmbeddingUnavailable as exc:
        raise RuntimeError(
            "abstention evaluation requires the configured embedding endpoint"
        ) from exc

    runs = []
    for seed in seeds:
        with tempfile.TemporaryDirectory(
            prefix=f"pnbench-abstention-{seed}-"
        ) as palinode_dir:
            harness.point_config_at(palinode_dir)
            generated = corpus.generate(palinode_dir, seed=seed, size=size)
            harness.init_store()
            indexed = harness.index_all(palinode_dir)
            if indexed.num_facts == 0 or indexed.num_vectors != indexed.num_facts:
                raise RuntimeError(
                    "abstention evaluation requires a fully embedded corpus "
                    f"({indexed.num_vectors}/{indexed.num_facts} chunks embedded)"
                )

            measurements = []
            for threshold in thresholds:
                for mode in MODES:
                    measurements.append(
                        _measure(
                            cases,
                            query_vectors,
                            threshold=threshold,
                            top_k=top_k,
                            mode=mode,
                        )
                    )
            runs.append(
                {
                    "seed": seed,
                    "num_files": generated.num_files,
                    "num_chunks": indexed.num_facts,
                    "measurements": measurements,
                    "arm_comparison": _arm_comparisons(measurements, thresholds),
                    "fts_candidates": _fts_candidate_stats(cases, top_k=top_k),
                }
            )

    results = {
        "schema_version": 1,
        "environment": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "palinode_version": _package_version(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "embedding_model": config.embeddings.primary.model,
            "embedding_dimensions": dimensions,
        },
        "parameters": {
            "seeds": list(seeds),
            "size": size,
            "thresholds": list(thresholds),
            "top_k": top_k,
            "modes": list(MODES),
            "query_counts": query_kind_counts(cases),
            "production_defaults_changed": False,
        },
        "runs": runs,
    }
    results["aggregate"] = _aggregate(runs, thresholds)
    results["arm_comparison"] = _aggregate_arm_comparisons(runs, thresholds)
    return results


def _ratio(row: dict[str, Any]) -> str:
    return f"{row['returned']}/{row['total']} ({row['false_positive_rate']:.1%})"


def _control_ratio(row: dict[str, Any]) -> str:
    return f"{row['true_hits']}/{row['total']}"


def _format_score_stats(stats: dict[str, float] | None) -> str:
    if stats is None:
        return "n/a"
    return f"{stats['min']:.3f} / {stats['median']:.3f} / {stats['max']:.3f}"


def render_markdown(results: dict[str, Any]) -> str:
    """Render the aggregate threshold sweep as a reviewable Markdown table."""
    env = results["environment"]
    params = results["parameters"]
    counts = params["query_counts"]
    lines = [
        "# Palinode abstention evaluation",
        "",
        f"- Generated: {env['generated_at']}",
        f"- Palinode: {env['palinode_version']}",
        f"- Embedder: {env['embedding_model']} ({env['embedding_dimensions']} dimensions)",
        f"- Corpus seeds: {', '.join(str(seed) for seed in params['seeds'])}",
        f"- Corpus size: {params['size']} files per seed; top-k: {params['top_k']}",
        "- Query protocol: "
        f"{sum(counts.get(kind, 0) for kind in ABSENT_KINDS)} no-answer queries "
        f"and {sum(counts.get(kind, 0) for kind in CONTROL_KINDS)} answer-present controls per seed",
        "- Production defaults changed: **no**",
        "",
        "A false positive is counted once per no-answer query when search returns one or more hits. "
        "Control recall requires the result set to contain the seeded topic, not merely any hit.",
        "",
    ]

    for mode in MODES:
        lines.extend(
            [
                f"## {mode.title()} search",
                "",
                "| Threshold | No-answer queries returning a hit | Exact controls with true hit | Paraphrase controls with true hit | False-positive fused score min / median / max | False-positive raw cosine min / median / max |",
                "|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in results["aggregate"]:
            if row["mode"] != mode:
                continue
            summary = row["summary"]
            lines.append(
                f"| {row['threshold']:.2f} "
                f"| {_ratio(summary['no_answer'])} "
                f"| {_control_ratio(summary['controls']['exact'])} "
                f"| {_control_ratio(summary['controls']['paraphrase'])} "
                f"| {_format_score_stats(summary['false_positive_scores']['fused'])} "
                f"| {_format_score_stats(summary['false_positive_scores']['raw'])} |"
            )
        lines.extend(["", "Scores describe only false-positive result sets.", ""])

    lines.extend(
        [
            "## BM25 arm contribution",
            "",
            "The two arms are scored on different axes and share one floor: real cosine "
            "similarity for the vector arm, `min(abs(bm25) / 25.0, 1.0)` for BM25. This "
            "table is what separates the arms; the counted metrics above cannot, because "
            "they are blind to a reordering.",
            "",
            "| Threshold | Cases where hybrid differs from vector | Reordered only | Membership changed | Results from BM25 alone |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for row in results.get("arm_comparison", []):
        lines.append(
            f"| {row['threshold']:.2f} "
            f"| {row['cases_differing']}/{row['cases']} "
            f"| {row['cases_reordered_only']} "
            f"| {row['cases_membership_changed']} "
            f"| {row['fts_only_results']} |"
        )

    runs_with_candidates = [
        run for run in results.get("runs", []) if run.get("fts_candidates")
    ]
    if runs_with_candidates:
        lines.extend(
            [
                "",
                "Normalized BM25 candidate scores per seed, before any floor is applied "
                "(min / median / max):",
                "",
                "| Seed | Queries with a BM25 candidate | Candidates | Score min / median / max |",
                "|---:|---:|---:|---:|",
            ]
        )
        for run in runs_with_candidates:
            candidates = run["fts_candidates"]
            lines.append(
                f"| {run['seed']} "
                f"| {candidates['cases_with_candidates']}/{candidates['cases']} "
                f"| {candidates['candidates']} "
                f"| {_format_score_stats(candidates['score_stats'])} |"
            )
        lines.append("")

    return "\n".join(lines)


def _csv_ints(raw: str) -> tuple[int, ...]:
    try:
        return tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc


def _csv_floats(raw: str) -> tuple[float, ...]:
    try:
        return tuple(float(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated numbers") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure weak-match abstention without changing search defaults"
    )
    parser.add_argument("--seeds", type=_csv_ints, default=(1337, 2026, 9001))
    parser.add_argument("--size", type=int, default=60)
    parser.add_argument(
        "--thresholds",
        type=_csv_floats,
        default=(0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60),
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    try:
        results = evaluate(
            seeds=args.seeds,
            size=args.size,
            thresholds=args.thresholds,
            top_k=args.top_k,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = (
        render_markdown(results)
        if args.format == "markdown"
        else json.dumps(results, indent=2, sort_keys=True)
    )
    if args.out is None:
        print(payload)
    else:
        args.out.write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
