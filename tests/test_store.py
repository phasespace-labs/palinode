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

def test_detect_entities_in_text():
    with patch("palinode.core.store.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("person/alice",), ("project/alpha",)]
        mock_db.return_value = mock_conn
        
        res = store.detect_entities_in_text("Saw alice today regarding project alpha")
        assert "person/alice" in res
        assert "project/alpha" in res
        assert "person/bob" not in res
