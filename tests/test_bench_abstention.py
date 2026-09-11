"""Tests for the standalone abstention evaluation."""
from __future__ import annotations

import os

import pytest

from bench import abstention


@pytest.fixture(autouse=True)
def _restore_global_config():
    """Restore process-global store configuration after each evaluation."""
    from palinode.core import store
    from palinode.core.config import config

    snapshot = (config.memory_dir, config.db_path, store._db_checked)
    fresh_db = os.environ.get("PALINODE_ALLOW_FRESH_DB")
    try:
        yield
    finally:
        config.memory_dir, config.db_path, store._db_checked = snapshot
        if fresh_db is None:
            os.environ.pop("PALINODE_ALLOW_FRESH_DB", None)
        else:
            os.environ["PALINODE_ALLOW_FRESH_DB"] = fresh_db


def test_default_query_protocol_is_pinned():
    counts = abstention.query_kind_counts(abstention.DEFAULT_QUERY_CASES)

    assert counts == {
        "absent_natural": 10,
        "absent_keyword": 10,
        "absent_identifier": 6,
        "control_exact": 10,
        "control_paraphrase": 10,
    }
    assert len({case.case_id for case in abstention.DEFAULT_QUERY_CASES}) == 46
    assert all(
        case.expected_topic is None
        for case in abstention.DEFAULT_QUERY_CASES
        if case.kind.startswith("absent_")
    )


def test_summarize_counts_queries_and_score_ranges():
    observations = [
        {
            "kind": "absent_natural",
            "returned_count": 2,
            "top_score": 1.0,
            "top_raw_score": 0.42,
            "true_match": None,
        },
        {
            "kind": "absent_keyword",
            "returned_count": 0,
            "top_score": None,
            "top_raw_score": None,
            "true_match": None,
        },
        {
            "kind": "control_exact",
            "returned_count": 1,
            "top_score": 1.0,
            "top_raw_score": 0.91,
            "true_match": True,
        },
        {
            "kind": "control_paraphrase",
            "returned_count": 1,
            "top_score": 1.0,
            "top_raw_score": 0.48,
            "true_match": False,
        },
    ]

    summary = abstention.summarize_observations(observations)

    assert summary["no_answer"] == {
        "returned": 1,
        "total": 2,
        "false_positive_rate": 0.5,
    }
    assert summary["controls"]["exact"] == {"true_hits": 1, "total": 1}
    assert summary["controls"]["paraphrase"] == {"true_hits": 0, "total": 1}
    assert summary["false_positive_scores"]["fused"] == {
        "min": 1.0,
        "median": 1.0,
        "max": 1.0,
    }
    assert summary["false_positive_scores"]["raw"] == {
        "min": 0.42,
        "median": 0.42,
        "max": 0.42,
    }


def test_summary_counts_results_reached_through_bm25_alone():
    """A merged result with no raw cosine came from the FTS arm."""
    observations = [
        {"kind": "absent_natural", "returned_count": 2, "top_score": 1.0,
         "top_raw_score": 0.42, "true_match": None, "fts_only_count": 1},
        {"kind": "control_exact", "returned_count": 1, "top_score": 1.0,
         "top_raw_score": 0.91, "true_match": True, "fts_only_count": 0},
    ]

    assert abstention.summarize_observations(observations)["fts_only_results"] == 1


def test_summary_tolerates_observations_without_arm_provenance():
    observations = [
        {"kind": "absent_natural", "returned_count": 0, "top_score": None,
         "top_raw_score": None, "true_match": None},
    ]

    assert abstention.summarize_observations(observations)["fts_only_results"] == 0


def _measurement(mode, threshold, rows, fts_only=0):
    return {
        "mode": mode,
        "threshold": threshold,
        "summary": {"fts_only_results": fts_only},
        "observations": [
            {"case_id": case_id, "result_keys": keys} for case_id, keys in rows
        ],
    }


def test_compare_arms_separates_a_reorder_from_a_membership_change():
    """The counted metrics cannot see either; this is the point of the table."""
    vector = _measurement(
        "vector",
        0.40,
        [
            ("same", ["a#1", "b#1"]),
            ("reordered", ["a#1", "b#1"]),
            ("membership", ["a#1", "b#1"]),
        ],
    )
    hybrid = _measurement(
        "hybrid",
        0.40,
        [
            ("same", ["a#1", "b#1"]),
            ("reordered", ["b#1", "a#1"]),
            ("membership", ["a#1", "c#1"]),
        ],
        fts_only=1,
    )

    comparison = abstention.compare_arms(vector, hybrid)

    assert comparison == {
        "threshold": 0.40,
        "cases": 3,
        "cases_differing": 2,
        "cases_reordered_only": 1,
        "cases_membership_changed": 1,
        "fts_only_results": 1,
    }


def test_compare_arms_reports_no_difference_when_bm25_changes_nothing():
    rows = [("only", ["a#1"])]
    comparison = abstention.compare_arms(
        _measurement("vector", 0.50, rows), _measurement("hybrid", 0.50, rows)
    )

    assert comparison["cases_differing"] == 0
    assert comparison["cases_reordered_only"] == 0
    assert comparison["cases_membership_changed"] == 0


def test_evaluate_runs_real_store_and_preserves_controls(monkeypatch):
    """The evaluation uses real SQLite while the embedder is deterministic."""
    from palinode.core.config import config

    dimensions = int(config.embeddings.primary.dimensions)
    topic = "database migration"
    paraphrase = "moving persistent data to a new schema"

    def semantic_embed(text: str) -> list[float]:
        vector = [0.0] * dimensions
        lowered = text.lower()
        coordinate = 100
        for index, corpus_topic in enumerate(abstention.corpus.TOPICS):
            if corpus_topic in lowered:
                coordinate = index
                break
        if paraphrase in lowered:
            coordinate = 0
        vector[coordinate] = 1.0
        return vector

    monkeypatch.setattr("palinode.core.embedder.embed", semantic_embed)
    # A short reachability probe can time out while a real configured embed
    # call still succeeds; the evaluation must trust the actual call.
    monkeypatch.setattr("bench.harness.embedder_available", lambda: False)

    cases = (
        abstention.QueryCase("absent", "absent_natural", "missing answer"),
        abstention.QueryCase("exact", "control_exact", topic, topic),
        abstention.QueryCase("paraphrase", "control_paraphrase", paraphrase, topic),
    )
    results = abstention.evaluate(
        seeds=(7,),
        size=10,
        thresholds=(0.0, 0.5),
        fts_threshold=0.25,
        top_k=5,
        cases=cases,
    )

    assert results["parameters"]["query_counts"] == {
        "absent_natural": 1,
        "control_exact": 1,
        "control_paraphrase": 1,
    }
    assert results["parameters"]["fts_threshold"] == 0.25
    assert results["runs"][0]["num_chunks"] > 0
    vector_summaries = {
        row["threshold"]: row["summary"]
        for row in results["runs"][0]["measurements"]
        if row["mode"] == "vector"
    }
    assert vector_summaries[0.0]["no_answer"]["returned"] == 1
    assert vector_summaries[0.5]["no_answer"]["returned"] == 0
    assert vector_summaries[0.5]["controls"]["exact"]["true_hits"] == 1
    assert vector_summaries[0.5]["controls"]["paraphrase"]["true_hits"] == 1

    arm_rows = {row["threshold"]: row for row in results["arm_comparison"]}
    assert set(arm_rows) == {0.0, 0.5}
    assert arm_rows[0.5]["cases"] == 3
    assert results["runs"][0]["fts_candidates"]["cases"] == 3

    report = abstention.render_markdown(results)
    report.encode("ascii")
    assert "# Palinode abstention evaluation" in report
    assert "No-answer queries returning a hit" in report
    assert "Production defaults changed: **no**" in report
    assert "## BM25 arm contribution" in report
    assert "Results from BM25 alone" in report
    assert "BM25 relative floor: 0.25 x top keyword score" in report


def test_evaluate_rejects_negative_fts_threshold():
    with pytest.raises(ValueError, match="fts_threshold must be non-negative"):
        abstention.evaluate(fts_threshold=-0.1)
