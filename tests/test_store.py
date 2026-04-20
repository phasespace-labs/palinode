import pytest
from unittest.mock import patch, MagicMock
from palinode.core import store
from palinode.core.config import config

def test_empty_index_returns_empty():
    with patch("palinode.core.store.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cursor
        mock_db.return_value = mock_conn
        
        res = store.search(query_embedding=[0.0]*1024)
        assert len(res) == 0

def test_search_hybrid_rrf():
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [{"file_path": "a.md", "content": "text", "score": 0.9}]
                mock_fts.return_value = [{"file_path": "b.md", "content": "text", "score": 0.5}]

                res = store.search_hybrid("query", query_embedding=[0.0]*1024, top_k=2)
                assert len(res) == 2
                # Should normalize scores
                assert "score" in res[0]
                assert "score" in res[1]

def test_search_hybrid_empty():
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            mock_vec.return_value = []
            mock_fts.return_value = []
            
            res = store.search_hybrid("query", query_embedding=[0.0]*1024, top_k=2)
            assert len(res) == 0

def test_search_returns_raw_score():
    """search() should include raw_score equal to score (cosine similarity)."""
    with patch("palinode.core.store.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {
                "file_path": "test.md",
                "section_id": "root",
                "content": "hello world",
                "category": "insight",
                "metadata": '{}',
                "created_at": "2025-01-01",
                "last_updated": "2025-01-01",
                "distance": 0.5,  # L2 distance → cosine = 1 - (0.5^2 / 2) = 0.875
            }
        ]
        mock_conn.cursor.return_value = mock_cursor
        mock_db.return_value = mock_conn

        res = store.search(query_embedding=[0.0] * 1024, threshold=0.0)
        assert len(res) == 1
        assert "raw_score" in res[0]
        assert res[0]["raw_score"] == res[0]["score"]
        assert abs(res[0]["raw_score"] - 0.875) < 0.001


def test_search_hybrid_raw_score_from_vector():
    """search_hybrid() should expose the original cosine similarity as raw_score."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [
                    {"file_path": "a.md", "section_id": "root", "content": "text a", "score": 0.9, "raw_score": 0.9},
                    {"file_path": "b.md", "section_id": "root", "content": "text b", "score": 0.7, "raw_score": 0.7},
                ]
                mock_fts.return_value = [
                    {"file_path": "a.md", "section_id": "root", "content": "text a", "score": 0.8},
                ]

                res = store.search_hybrid("query", query_embedding=[0.0] * 1024, top_k=10, threshold=0.0)

                a_results = [r for r in res if r["file_path"] == "a.md"]
                b_results = [r for r in res if r["file_path"] == "b.md"]

                # a.md came from vector search → raw_score should be the cosine similarity
                assert len(a_results) == 1
                assert a_results[0]["raw_score"] == 0.9

                # b.md also came from vector search
                assert len(b_results) == 1
                assert b_results[0]["raw_score"] == 0.7

                # RRF score should differ from raw_score (it's normalized)
                assert a_results[0]["score"] != a_results[0]["raw_score"]


def test_search_hybrid_bm25_only_raw_score_is_none():
    """Results that only came from BM25 (no vector match) should have raw_score=None."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [
                    {"file_path": "a.md", "section_id": "root", "content": "text a", "score": 0.9, "raw_score": 0.9},
                ]
                mock_fts.return_value = [
                    {"file_path": "a.md", "section_id": "root", "content": "text a", "score": 0.8},
                    {"file_path": "bm25only.md", "section_id": "root", "content": "keyword match", "score": 0.7},
                ]

                res = store.search_hybrid("query", query_embedding=[0.0] * 1024, top_k=10, threshold=0.0)

                bm25_results = [r for r in res if r["file_path"] == "bm25only.md"]
                assert len(bm25_results) == 1
                assert bm25_results[0]["raw_score"] is None


def test_search_hybrid_raw_score_preserved_through_rrf():
    """raw_score should be the original cosine sim, not affected by RRF normalization."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [
                    {"file_path": "x.md", "section_id": "root", "content": "x", "score": 0.85, "raw_score": 0.85},
                ]
                mock_fts.return_value = []

                res = store.search_hybrid("query", query_embedding=[0.0] * 1024, top_k=10, threshold=0.0)
                assert len(res) == 1
                # raw_score should be the original cosine similarity (0.85)
                assert res[0]["raw_score"] == 0.85
                # RRF score is normalized to 1.0 (only result → max score)
                assert res[0]["score"] == 1.0


def test_detect_entities_in_text():
    with patch("palinode.core.store.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("person/alice",), ("project/alpha",)]
        mock_db.return_value = mock_conn

        res = store.detect_entities_in_text("Saw alice today regarding project alpha")
        assert "person/alice" in res
        assert "project/alpha" in res
        assert "person/bob" not in res
