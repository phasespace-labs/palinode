"""Build a real store from the fixture and measure both retrieval modes.

Nothing is simulated. The fixture records are written to a throwaway
``PALINODE_DIR`` and indexed through the canonical ``index_file`` pipeline —
real parser, real SHA-256 dedup, real embeddings, real SQLite-vec and FTS5.
Queries go through ``store.search_hybrid``, the same entry point the API's
``/search`` uses, with production defaults untouched.

Three kinds of arm, all scored on the same labels:

``lexical``
    ``search_hybrid(query, None, …)`` — the documented keyword-only retrieval
    path, which is also what a query falls back to when the embedder rejects
    its input. No vector arm, no cosine floor, BM25 weight forced to 1.0.

``+ abstain``
    Each mode also runs with the opt-in
    ``search.abstain_on_no_confident_match`` behaviour applied: a slate whose
    confidence verdict is ``none`` is emptied, as the MCP surface does with
    that switch on. Its point is the *cost* column — what correct recall the
    switch would throw away — which is why it runs beside its own default arm
    rather than replacing it. Both retrieval modes get the pair, hybrid
    included: hybrid is the shipped default, so a decision taken on the
    lexical column alone would be taken on the mode nobody runs.

``hybrid``
    ``search_hybrid(query, vector, …)`` at the shipped MCP and API cosine
    floors (0.4 and 0.5), with ``fts_threshold`` left at its configured value.
    Requires a reachable embedding endpoint; when none is reachable the arm is
    recorded as NOT RUN and nothing is reported for it. Fabricated vectors
    would make every number in it meaningless while still filling the table,
    so the rig refuses rather than degrading silently.

What a caller actually receives is measured, not approximated: each slate is
enriched with snippets by the same helper ``/search`` uses and rendered by
``palinode.mcp._format_results``, the function whose output the MCP tool
returns. Useful-context tokens are that same renderer run over the relevant
subset of the slate, so the fraction is "of the payload the client got, how
much of it was the answer".
"""
from __future__ import annotations

import os
import platform
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from time import perf_counter
from typing import Any

from bench import harness as bench_harness
from bench.relevance import corpus as corpus_mod
from bench.relevance.corpus import Fixture, Question
from bench.relevance.scoring import (
    Hit,
    QuestionScore,
    aggregate,
    aggregate_by,
    percentile,
    score_question,
)

#: The shipped cosine floors: ``search.mcp_threshold`` and
#: ``search.api_threshold``. Recorded, never changed — the baseline has to
#: describe the surfaces as they ship.
DEFAULT_THRESHOLDS: tuple[float, ...] = (0.4, 0.5)

#: ``limit`` a caller gets from the MCP search tool by default.
DEFAULT_TOP_K = 5

RESULTS_SCHEMA_VERSION = 1


@dataclass
class ArmResult:
    """One (mode, threshold, abstain) arm: what ran, or why it did not."""

    mode: str
    threshold: float | None
    status: str  # "ran" | "not_run"
    reason: str | None = None
    #: ``search.abstain_on_no_confident_match`` on for this arm.
    abstain: bool = False
    scores: list[QuestionScore] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: list[float] = field(default_factory=list)
    fallback: dict[str, int] = field(default_factory=dict)


def _package_version() -> str:
    try:
        return version("palinode")
    except PackageNotFoundError:
        return "unknown"


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _resolve(path_to_id: dict[str, str], results: Sequence[dict[str, Any]]) -> list[Hit]:
    hits = []
    for rank, result in enumerate(results, start=1):
        path = os.path.abspath(str(result.get("file_path", "")))
        hits.append(
            Hit(
                rank=rank,
                record_id=path_to_id.get(path),
                section_id=str(result.get("section_id") or "root"),
                content=str(result.get("content") or ""),
                score=float(result["score"]) if result.get("score") is not None else None,
                file_path=path,
                cosine=_float(result.get("raw_score")),
                keyword=_float(result.get("keyword_score")),
            )
        )
    return hits


def _render_payload(results: list[dict[str, Any]], query: str) -> str:
    """The text an MCP client would receive for this slate."""
    from palinode.api.search_helpers import _enrich_with_snippets
    from palinode.core.config import config
    from palinode.mcp import _format_results

    enriched = [dict(result) for result in results]
    _enrich_with_snippets(enriched, query, config.search.snippet_max_chars)
    return _format_results(enriched)


def _run_query(
    question: Question,
    *,
    mode: str,
    threshold: float | None,
    top_k: int,
    counters: dict[str, int],
    abstain: bool = False,
) -> tuple[list[dict[str, Any]], str, float, str | None]:
    """Run one query end to end: (results, payload, elapsed ms, verdict).

    The clock covers everything a caller waits for: the query embedding when
    there is one, the store search, and rendering the delivered payload.

    ``abstain`` measures ``search.abstain_on_no_confident_match``: the slate
    is emptied when the verdict is ``none``, exactly as the MCP surface does
    with the switch on. The verdict is read from the slate *before* it is
    emptied, so an abstaining arm still reports what it abstained from.
    """
    from palinode.core import embedder, store
    from palinode.core.confidence import assess

    started = perf_counter()
    query_vector: list[float] | None = None
    if mode == "hybrid":
        try:
            query_vector = embedder.embed(question.ask)
        except embedder.EmbeddingUnavailable:
            # Exactly what /search does with a per-input embed rejection:
            # degrade this one query to the keyword arm rather than fail it.
            counters["keyword_fallback"] += 1
            query_vector = None

    if query_vector is None:
        results = store.search_hybrid(
            question.ask, None, top_k=top_k, record_access=False
        )
    else:
        results = store.search_hybrid(
            question.ask,
            query_vector,
            top_k=top_k,
            threshold=float(threshold if threshold is not None else 0.0),
            record_access=False,
        )
    active_mode = "lexical" if query_vector is None else "hybrid"
    assessment = assess(results, active_mode=active_mode)
    verdict = assessment["confidence"] if assessment else None
    if abstain and verdict == "none":
        results = []
    payload = _render_payload(results, question.ask)
    elapsed_ms = (perf_counter() - started) * 1000.0

    if mode == "hybrid" and query_vector is not None:
        if not store.search_fts(question.ask, top_k=top_k * 2):
            counters["no_keyword_candidates"] += 1
    return results, payload, elapsed_ms, verdict


def _observation(
    question: Question, hits: Sequence[Hit], score: QuestionScore
) -> dict[str, Any]:
    return {
        "question_id": question.id,
        "cls": question.cls,
        "variant": question.variant,
        "ask": question.ask,
        "expected": list(question.relevant),
        "delivered": [
            {
                "rank": hit.rank,
                "record": hit.record_id,
                "section": hit.section_id,
                "score": hit.score,
                # Both arms, per result, on their own scales: the fused score
                # above is a rank, so it is the only one of the three a later
                # calibration cannot use.
                "cosine": hit.cosine,
                "keyword": hit.keyword,
            }
            for hit in hits
        ],
        "verdict": score.verdict,
        "score": score.to_dict(),
    }


def run_arm(
    fixture: Fixture,
    path_to_id: dict[str, str],
    *,
    mode: str,
    threshold: float | None,
    top_k: int,
    repeats: int,
    abstain: bool = False,
) -> ArmResult:
    """Run every question in one arm and score the delivered payloads."""
    from palinode.core.packing import estimate_tokens

    records = fixture.by_id
    arm = ArmResult(mode=mode, threshold=threshold, status="ran", abstain=abstain)
    arm.fallback = {"keyword_fallback": 0, "no_keyword_candidates": 0}

    for question in fixture.questions:
        results, payload, elapsed_ms, verdict = _run_query(
            question,
            mode=mode,
            threshold=threshold,
            top_k=top_k,
            counters=arm.fallback,
            abstain=abstain,
        )
        arm.latency_ms.append(elapsed_ms)
        for _ in range(max(0, repeats - 1)):
            _, _, extra_ms, _ = _run_query(
                question,
                mode=mode,
                threshold=threshold,
                top_k=top_k,
                counters=arm.fallback,
                abstain=abstain,
            )
            arm.latency_ms.append(extra_ms)

        hits = _resolve(path_to_id, results)
        relevant = set(question.relevant)
        useful_results = [
            result
            for result, hit in zip(results, hits, strict=True)
            if hit.record_id in relevant
        ]
        useful_payload = (
            _render_payload(useful_results, question.ask) if useful_results else ""
        )
        score = score_question(
            question,
            hits,
            records,
            payload_tokens=estimate_tokens(payload),
            useful_tokens=estimate_tokens(useful_payload) if useful_payload else 0,
            verdict=verdict,
        )
        arm.scores.append(score)
        arm.observations.append(_observation(question, hits, score))

    # The repeated passes are measured, so the fallback counters are per call,
    # not per question. Normalise to a rate over calls.
    return arm


def _arm_payload(arm: ArmResult, *, calls: int) -> dict[str, Any]:
    if arm.status != "ran":
        return {
            "mode": arm.mode,
            "threshold": arm.threshold,
            "abstain": arm.abstain,
            "status": arm.status,
            "reason": arm.reason,
        }
    return {
        "mode": arm.mode,
        "threshold": arm.threshold,
        "abstain": arm.abstain,
        "status": "ran",
        "summary": aggregate(arm.scores),
        "by_class": aggregate_by(arm.scores, "cls"),
        "by_variant": aggregate_by(arm.scores, "variant"),
        "latency_ms": {
            "samples": len(arm.latency_ms),
            "p50": percentile(arm.latency_ms, 50),
            "p95": percentile(arm.latency_ms, 95),
        },
        "fallback": {
            "calls": calls,
            "keyword_fallback": arm.fallback.get("keyword_fallback", 0),
            "keyword_fallback_rate": (
                arm.fallback.get("keyword_fallback", 0) / calls if calls else None
            ),
            "no_keyword_candidates": arm.fallback.get("no_keyword_candidates", 0),
        },
        "observations": arm.observations,
    }


def evaluate(
    *,
    fixture: Fixture | None = None,
    top_k: int = DEFAULT_TOP_K,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    repeats: int = 3,
    keep_store: bool = False,
) -> dict[str, Any]:
    """Build the store, run both modes, and return the full results object."""
    from palinode.core.config import config

    fixture = fixture or corpus_mod.load_fixture()
    thresholds = tuple(float(value) for value in thresholds)
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if any(value < 0.0 or value > 1.0 for value in thresholds):
        raise ValueError("thresholds must be between 0.0 and 1.0")

    embedder_up = bench_harness.embedder_available()

    context = tempfile.TemporaryDirectory(prefix="pnbench-relevance-")
    palinode_dir = context.name
    try:
        bench_harness.point_config_at(palinode_dir)
        path_to_id = {
            path: record_id
            for record_id, path in corpus_mod.materialize(fixture, palinode_dir).items()
        }
        bench_harness.init_store()
        ingest = bench_harness.index_all(palinode_dir)

        arms: list[ArmResult] = [
            run_arm(
                fixture,
                path_to_id,
                mode="lexical",
                threshold=None,
                top_k=top_k,
                repeats=repeats,
            ),
            # The same retrieval with `search.abstain_on_no_confident_match`
            # on, so the switch's cost is a measured column beside the default
            # rather than a claim. Production defaults are still untouched:
            # the arm emulates the MCP surface's suppression on its own slate.
            run_arm(
                fixture,
                path_to_id,
                mode="lexical",
                threshold=None,
                top_k=top_k,
                repeats=repeats,
                abstain=True,
            ),
        ]
        # Each hybrid floor runs twice, with the abstain switch off and on, for
        # the same reason the lexical pair does — and more pressingly, because
        # hybrid is the shipped default and is where the switch will actually
        # be decided.
        for threshold in thresholds:
            for abstain in (False, True):
                if not embedder_up:
                    arms.append(
                        ArmResult(
                            mode="hybrid",
                            threshold=threshold,
                            abstain=abstain,
                            status="not_run",
                            reason=(
                                "no embedding endpoint reachable; the hybrid arm needs "
                                "real query vectors and synthetic ones would not measure "
                                "retrieval quality"
                            ),
                        )
                    )
                    continue
                arms.append(
                    run_arm(
                        fixture,
                        path_to_id,
                        mode="hybrid",
                        threshold=threshold,
                        top_k=top_k,
                        repeats=repeats,
                        abstain=abstain,
                    )
                )
    finally:
        if keep_store:
            print(f"store kept at {palinode_dir}")
        else:
            context.cleanup()

    calls = len(fixture.questions) * repeats
    return {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "environment": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "palinode_version": _package_version(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "embedding_model": config.embeddings.primary.model,
            "embedding_dimensions": int(config.embeddings.primary.dimensions),
            "embedder_reachable": embedder_up,
        },
        "parameters": {
            "corpus_version": fixture.corpus_version,
            "question_set_version": fixture.question_set_version,
            "records": len(fixture.records),
            "questions": len(fixture.questions),
            "class_counts": corpus_mod.class_counts(fixture),
            "variant_counts": corpus_mod.variant_counts(fixture),
            "role_counts": corpus_mod.role_counts(fixture),
            "top_k": top_k,
            "thresholds": list(thresholds),
            "fts_threshold": float(config.search.fts_threshold),
            # The three bounds that decide how much of `top_k` is actually
            # filled — one per arm, plus the per-file cap. Recorded because a
            # run is only comparable to another run that had the same ones in
            # effect.
            "lexical_fts_threshold": float(config.search.lexical_fts_threshold),
            "max_chunks_per_file": int(config.search.max_chunks_per_file),
            "vector_relative_floor": float(config.search.vector_relative_floor),
            "hybrid_weight": float(config.search.hybrid_weight),
            "snippet_max_chars": int(config.search.snippet_max_chars),
            "repeats": repeats,
            "production_defaults_changed": False,
        },
        "index": {
            "files": ingest.num_files,
            "chunks": ingest.num_facts,
            "vectors": ingest.num_vectors,
            # A chunk with no vector is keyword-only. Expected to be zero: the
            # indexer projects the auto-footer out before deriving a chunk, so
            # a footer-only section gets no row at all rather than a row with
            # nothing to embed.
            "chunks_without_vector": ingest.num_facts - ingest.num_vectors,
            "embedded": ingest.embedded,
        },
        "arms": [_arm_payload(arm, calls=calls) for arm in arms],
    }


__all__ = [
    "DEFAULT_THRESHOLDS",
    "DEFAULT_TOP_K",
    "RESULTS_SCHEMA_VERSION",
    "ArmResult",
    "evaluate",
    "run_arm",
]
