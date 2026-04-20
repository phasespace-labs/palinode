"""Tests for #91: deduplicate search results by file (score-gap based)."""
import pytest
from unittest.mock import patch, MagicMock
from palinode.core import store
from palinode.core.config import config


def test_dedup_suppresses_low_scoring_chunks():
    """Chunks far below the file's best score should be suppressed.

    RRF compresses rank-based scores into a narrow band, so we use a tight
    gap (0.01) and wide rank separation to create a meaningful score delta.
    README intro appears at rank 1 in both vec+fts (strong signal), while
    README faq only appears deep in one list (weak signal).
    """
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                original_gap = config.search.dedup_score_gap
                try:
                    # Tight gap so that only chunks with near-identical RRF scores survive
                    config.search.dedup_score_gap = 0.01

                    # README intro ranks #1 in both lists → high combined RRF
                    # README faq ranks last in vec only → much lower RRF
                    # guide.md ranks #2 in both → second highest
                    mock_vec.return_value = [
                        {"file_path": "README.md", "section_id": "intro", "content": "intro", "score": 0.95},
                        {"file_path": "guide.md", "section_id": "root", "content": "guide", "score": 0.90},
                        {"file_path": "other1.md", "section_id": "root", "content": "o1", "score": 0.80},
                        {"file_path": "other2.md", "section_id": "root", "content": "o2", "score": 0.70},
                        {"file_path": "other3.md", "section_id": "root", "content": "o3", "score": 0.60},
                        {"file_path": "other4.md", "section_id": "root", "content": "o4", "score": 0.50},
                        {"file_path": "other5.md", "section_id": "root", "content": "o5", "score": 0.40},
                        {"file_path": "other6.md", "section_id": "root", "content": "o6", "score": 0.30},
                        {"file_path": "README.md", "section_id": "faq", "content": "faq", "score": 0.20},
                    ]
                    mock_fts.return_value = [
                        {"file_path": "README.md", "section_id": "intro", "content": "intro", "score": 0.90},
                        {"file_path": "guide.md", "section_id": "root", "content": "guide", "score": 0.80},
                    ]

                    results = store.search_hybrid("setup", query_embedding=[0.0]*1024, top_k=10, threshold=0.0)

                    readme_results = [r for r in results if r["file_path"] == "README.md"]
                    # faq chunk (rank 9 vec-only) should be suppressed vs intro (rank 1 in both)
                    assert len(readme_results) == 1
                    assert readme_results[0]["section_id"] == "intro"
                    assert any(r["file_path"] == "guide.md" for r in results)
                finally:
                    config.search.dedup_score_gap = original_gap


def test_dedup_keeps_close_scoring_chunks():
    """Chunks within the score gap of the file's best should be kept."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                # Two chunks from same file, both ranked high in vector results
                # After RRF normalization, they'll be close in score
                mock_vec.return_value = [
                    {"file_path": "notes.md", "section_id": "s1", "content": "chunk 1", "score": 0.95},
                    {"file_path": "notes.md", "section_id": "s2", "content": "chunk 2", "score": 0.93},
                ]
                mock_fts.return_value = [
                    {"file_path": "notes.md", "section_id": "s1", "content": "chunk 1", "score": 0.90},
                    {"file_path": "notes.md", "section_id": "s2", "content": "chunk 2", "score": 0.88},
                ]

                results = store.search_hybrid("test", query_embedding=[0.0]*1024, top_k=10, threshold=0.0)

                # Both chunks should survive — their scores are very close
                assert len(results) == 2


def test_dedup_respects_top_k_after_filtering():
    """top_k should limit total results after dedup."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [
                    {"file_path": "a.md", "section_id": "root", "content": "a", "score": 0.9},
                    {"file_path": "b.md", "section_id": "root", "content": "b", "score": 0.8},
                    {"file_path": "c.md", "section_id": "root", "content": "c", "score": 0.7},
                    {"file_path": "d.md", "section_id": "root", "content": "d", "score": 0.6},
                ]
                mock_fts.return_value = []

                results = store.search_hybrid("test", query_embedding=[0.0]*1024, top_k=2, threshold=0.0)

                assert len(results) == 2
                assert results[0]["file_path"] == "a.md"
                assert results[1]["file_path"] == "b.md"


def test_dedup_single_chunk_files_unchanged():
    """Files with only one chunk each should pass through unchanged."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [
                    {"file_path": "a.md", "content": "a", "score": 0.9},
                    {"file_path": "b.md", "content": "b", "score": 0.8},
                    {"file_path": "c.md", "content": "c", "score": 0.7},
                ]
                mock_fts.return_value = [
                    {"file_path": "b.md", "content": "b", "score": 0.9},
                    {"file_path": "a.md", "content": "a", "score": 0.7},
                ]

                results = store.search_hybrid("test", query_embedding=[0.0]*1024, top_k=10, threshold=0.0)

                assert len(results) == 3
                fps = {r["file_path"] for r in results}
                assert fps == {"a.md", "b.md", "c.md"}


def test_dedup_configurable_gap():
    """The score gap threshold should be configurable."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                # Two chunks: rank 1 and rank 2 in vector results only
                # RRF scores: rank1 = 1/61, rank2 = 1/62
                # Normalized: rank1 = 1.0, rank2 = 61/62 ≈ 0.984
                # Gap = 0.016 — within any reasonable threshold
                mock_vec.return_value = [
                    {"file_path": "f.md", "section_id": "s1", "content": "best", "score": 0.95},
                    {"file_path": "f.md", "section_id": "s2", "content": "second", "score": 0.90},
                ]
                mock_fts.return_value = []

                # With gap=0.0, only exact ties kept → only best chunk
                original_gap = config.search.dedup_score_gap
                try:
                    config.search.dedup_score_gap = 0.0
                    results_strict = store.search_hybrid(
                        "test", query_embedding=[0.0]*1024, top_k=10, threshold=0.0
                    )

                    # With gap=1.0, everything kept
                    config.search.dedup_score_gap = 1.0
                    results_loose = store.search_hybrid(
                        "test", query_embedding=[0.0]*1024, top_k=10, threshold=0.0
                    )
                finally:
                    config.search.dedup_score_gap = original_gap

                assert len(results_strict) == 1
                assert len(results_loose) == 2
