"""Tests for the deduplicate-search-results-by-file work: deduplicate search
results by file (score-gap based).

Adapted for the double-daily-penalty fix: the architecture review that
prompted that fix claimed this file was "fully superseded by
tests/test_ranker.py's per-file-dedup test and can be deleted." Checked
against tests/test_ranker.py directly — false. It covers only the "best
chunk survives, far chunk suppressed" shape (its
``test_per_file_dedup_suppresses_far_below_best_chunk``) and a loose top_k
cap; it does not cover close-scoring chunks both surviving, single-chunk
files passing through unchanged, or the configurable-gap extremes (0.0 /
1.0) this file exercises. So: adapted, not deleted — rewritten against
``ranker.rank_hybrid`` on plain dicts (the pattern in tests/test_ranker.py)
instead of patching ``palinode.core.store.search`` / ``search_fts`` /
``get_db``, per the same "mock replaces the seam it should cross" lesson
that fix was built around.
"""
from __future__ import annotations

import pytest

from palinode.core import ranker
from palinode.core.config import config


def _res(path, *, score=0.5, section="root", **extra):
    r = {"file_path": path, "section_id": section, "score": score, "id": f"{path}#{section}"}
    r.update(extra)
    return r


@pytest.fixture(autouse=True)
def _decay_and_context_off(monkeypatch):
    monkeypatch.setattr(config.decay, "enabled", False)
    monkeypatch.setattr(config.context, "enabled", False)
    monkeypatch.setattr(config.search, "daily_penalty", 1.0)
    # Per-file dedup is the subject here, so the vector arm's relative floor is
    # off: these slates vary cosine to separate ranks, not to be competitive.
    monkeypatch.setattr(config.search, "vector_relative_floor", 0.0)


def _run(vec, fts=(), **kw):
    params = dict(top_k=10, threshold=0.0, hybrid_weight=0.5, priority_weight=0.025)
    params.update(kw)
    return ranker.rank_hybrid(vec, list(fts), **params)


def test_dedup_suppresses_low_scoring_chunks(monkeypatch):
    """Chunks far below the file's best score should be suppressed.

    RRF compresses rank-based scores into a narrow band, so we use a tight
    gap (0.01) and wide rank separation to create a meaningful score delta.
    README intro appears at rank 0 in both vec+fts (strong signal), while
    README faq only appears deep in the vec list alone (weak signal).
    """
    monkeypatch.setattr(config.search, "dedup_score_gap", 0.01)

    vec = [
        _res("README.md", section="intro"),
        _res("guide.md"),
        *[_res(f"other{i}.md") for i in range(1, 7)],
        _res("README.md", section="faq"),
    ]
    fts = [
        _res("README.md", section="intro"),
        _res("guide.md"),
    ]

    results = _run(vec, fts)

    readme_results = [r for r in results if r["file_path"] == "README.md"]
    # faq chunk (rank 8 vec-only) should be suppressed vs intro (rank 0 in both)
    assert len(readme_results) == 1
    assert readme_results[0]["section_id"] == "intro"
    assert any(r["file_path"] == "guide.md" for r in results)


def test_dedup_keeps_close_scoring_chunks():
    """Chunks within the score gap of the file's best should be kept."""
    # Two chunks from same file, both ranked high in vector results — after
    # RRF normalization they land close in score, well within the default gap.
    vec = [_res("notes.md", section="s1"), _res("notes.md", section="s2")]
    fts = [_res("notes.md", section="s1"), _res("notes.md", section="s2")]

    results = _run(vec, fts)

    # Both chunks should survive — their scores are very close.
    assert len(results) == 2


def test_dedup_respects_top_k_after_filtering():
    """top_k should limit total results after dedup."""
    vec = [_res(f"{c}.md") for c in "abcd"]

    results = _run(vec, [], top_k=2)

    assert len(results) == 2
    assert results[0]["file_path"] == "a.md"
    assert results[1]["file_path"] == "b.md"


def test_dedup_single_chunk_files_unchanged():
    """Files with only one chunk each should pass through unchanged."""
    vec = [_res("a.md"), _res("b.md"), _res("c.md")]
    fts = [_res("b.md"), _res("a.md")]

    results = _run(vec, fts)

    assert len(results) == 3
    fps = {r["file_path"] for r in results}
    assert fps == {"a.md", "b.md", "c.md"}


def test_dedup_configurable_gap(monkeypatch):
    """The score gap threshold should be configurable."""
    # Two chunks: rank 0 and rank 1 in vector results only.
    # RRF scores: rank0 = 1/61, rank1 = 1/62.
    # Normalized: rank0 = 1.0, rank1 = 61/62 ~= 0.984. Gap ~= 0.016.
    vec = [_res("f.md", section="s1"), _res("f.md", section="s2")]

    # With gap=0.0, only exact ties kept -> only best chunk.
    monkeypatch.setattr(config.search, "dedup_score_gap", 0.0)
    results_strict = _run(vec, [])

    # With gap=1.0, everything kept.
    monkeypatch.setattr(config.search, "dedup_score_gap", 1.0)
    results_loose = _run(vec, [])

    assert len(results_strict) == 1
    assert len(results_loose) == 2
