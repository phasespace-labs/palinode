"""Tests for #93: penalize daily/ files in search results."""
import pytest
from unittest.mock import patch, MagicMock
from palinode.core import store
from palinode.core.config import config


def test_is_daily_file():
    """_is_daily_file should match daily/ paths in various forms."""
    assert store._is_daily_file("daily/2026-04-12.md") is True
    assert store._is_daily_file("/home/user/palinode/daily/2026-04-12.md") is True
    assert store._is_daily_file("projects/daily-standup.md") is False
    assert store._is_daily_file("decisions/use-daily-builds.md") is False
    assert store._is_daily_file("daily/notes/misc.md") is True


def test_hybrid_daily_penalty_demotes_daily_files():
    """Daily files should be penalized below real memories in hybrid search."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                original_penalty = config.search.daily_penalty
                try:
                    config.search.daily_penalty = 0.3

                    # daily file ranks #1 in vec, real memory ranks #2
                    mock_vec.return_value = [
                        {"file_path": "daily/2026-04-12.md", "section_id": "root",
                         "content": "discussed palinode architecture", "score": 0.95},
                        {"file_path": "projects/palinode.md", "section_id": "root",
                         "content": "palinode architecture overview", "score": 0.85},
                    ]
                    mock_fts.return_value = [
                        {"file_path": "daily/2026-04-12.md", "section_id": "root",
                         "content": "discussed palinode architecture", "score": 0.90},
                        {"file_path": "projects/palinode.md", "section_id": "root",
                         "content": "palinode architecture overview", "score": 0.80},
                    ]

                    results = store.search_hybrid(
                        "palinode architecture",
                        query_embedding=[0.0] * 1024,
                        top_k=10,
                        threshold=0.0,
                    )

                    # Real memory should rank first after penalty
                    assert len(results) == 2
                    assert results[0]["file_path"] == "projects/palinode.md"
                    assert results[1]["file_path"] == "daily/2026-04-12.md"
                    # Daily file score should be penalized
                    assert results[1]["score"] < results[0]["score"]
                finally:
                    config.search.daily_penalty = original_penalty


def test_hybrid_daily_penalty_include_daily_skips_penalty():
    """include_daily=True should skip the penalty entirely."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                original_penalty = config.search.daily_penalty
                try:
                    config.search.daily_penalty = 0.3

                    mock_vec.return_value = [
                        {"file_path": "daily/2026-04-12.md", "section_id": "root",
                         "content": "discussed palinode architecture", "score": 0.95},
                        {"file_path": "projects/palinode.md", "section_id": "root",
                         "content": "palinode architecture overview", "score": 0.85},
                    ]
                    mock_fts.return_value = []

                    results = store.search_hybrid(
                        "palinode architecture",
                        query_embedding=[0.0] * 1024,
                        top_k=10,
                        threshold=0.0,
                        include_daily=True,
                    )

                    # Daily file should still rank first (no penalty applied)
                    assert len(results) == 2
                    assert results[0]["file_path"] == "daily/2026-04-12.md"
                finally:
                    config.search.daily_penalty = original_penalty


def test_hybrid_daily_penalty_one_means_no_penalty():
    """daily_penalty=1.0 should be a no-op (no score change)."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                original_penalty = config.search.daily_penalty
                try:
                    config.search.daily_penalty = 1.0

                    mock_vec.return_value = [
                        {"file_path": "daily/2026-04-12.md", "section_id": "root",
                         "content": "session notes", "score": 0.95},
                        {"file_path": "projects/palinode.md", "section_id": "root",
                         "content": "palinode overview", "score": 0.85},
                    ]
                    mock_fts.return_value = []

                    results = store.search_hybrid(
                        "test",
                        query_embedding=[0.0] * 1024,
                        top_k=10,
                        threshold=0.0,
                    )

                    # Daily file should still rank first (penalty is 1.0 = no change)
                    assert len(results) == 2
                    assert results[0]["file_path"] == "daily/2026-04-12.md"
                finally:
                    config.search.daily_penalty = original_penalty


def test_vector_daily_penalty_demotes_daily_files():
    """Daily files should be penalized in vector-only search too."""
    with patch("palinode.core.store.get_db") as mock_db:
        original_penalty = config.search.daily_penalty
        try:
            config.search.daily_penalty = 0.3

            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [
                {"id": 1, "file_path": "daily/2026-04-12.md", "section_id": "root",
                 "content": "session notes", "category": "daily", "metadata": "{}",
                 "created_at": "2026-04-12", "distance": 0.3},
                {"id": 2, "file_path": "projects/palinode.md", "section_id": "root",
                 "content": "palinode overview", "category": "projects", "metadata": "{}",
                 "created_at": "2026-04-12", "distance": 0.35},
            ]
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_db.return_value.close = MagicMock()

            results = store.search(
                query_embedding=[0.0] * 1024,
                top_k=10,
                threshold=0.0,
            )

            # After penalty, projects file should rank first
            assert len(results) == 2
            assert results[0]["file_path"] == "projects/palinode.md"
            assert results[1]["file_path"] == "daily/2026-04-12.md"
        finally:
            config.search.daily_penalty = original_penalty


def test_vector_daily_penalty_include_daily_skips():
    """include_daily=True should skip penalty in vector search."""
    with patch("palinode.core.store.get_db") as mock_db:
        original_penalty = config.search.daily_penalty
        try:
            config.search.daily_penalty = 0.3

            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [
                {"id": 1, "file_path": "daily/2026-04-12.md", "section_id": "root",
                 "content": "session notes", "category": "daily", "metadata": "{}",
                 "created_at": "2026-04-12", "distance": 0.3},
                {"id": 2, "file_path": "projects/palinode.md", "section_id": "root",
                 "content": "palinode overview", "category": "projects", "metadata": "{}",
                 "created_at": "2026-04-12", "distance": 0.35},
            ]
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_db.return_value.close = MagicMock()

            results = store.search(
                query_embedding=[0.0] * 1024,
                top_k=10,
                threshold=0.0,
                include_daily=True,
            )

            # Daily file should remain first (no penalty)
            assert len(results) == 2
            assert results[0]["file_path"] == "daily/2026-04-12.md"
        finally:
            config.search.daily_penalty = original_penalty


def test_daily_penalty_config_default():
    """SearchConfig should have daily_penalty default of 0.3."""
    assert config.search.daily_penalty == 0.3


def test_no_daily_files_unaffected():
    """When no daily files in results, penalty logic is a no-op."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [
                    {"file_path": "projects/palinode.md", "section_id": "root",
                     "content": "overview", "score": 0.95},
                    {"file_path": "decisions/adr-001.md", "section_id": "root",
                     "content": "decision", "score": 0.85},
                ]
                mock_fts.return_value = []

                results = store.search_hybrid(
                    "test",
                    query_embedding=[0.0] * 1024,
                    top_k=10,
                    threshold=0.0,
                )

                # Order unchanged, no daily files to penalize
                assert len(results) == 2
                assert results[0]["file_path"] == "projects/palinode.md"
                assert results[1]["file_path"] == "decisions/adr-001.md"


def test_daily_penalty_absolute_path():
    """Daily penalty should work with absolute file paths containing /daily/."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                original_penalty = config.search.daily_penalty
                try:
                    config.search.daily_penalty = 0.3

                    mock_vec.return_value = [
                        {"file_path": "/home/user/palinode/daily/2026-04-12.md",
                         "section_id": "root", "content": "session notes", "score": 0.95},
                        {"file_path": "/home/user/palinode/projects/palinode.md",
                         "section_id": "root", "content": "overview", "score": 0.85},
                    ]
                    mock_fts.return_value = []

                    results = store.search_hybrid(
                        "test",
                        query_embedding=[0.0] * 1024,
                        top_k=10,
                        threshold=0.0,
                    )

                    # Absolute path with /daily/ should also be penalized
                    assert results[0]["file_path"] == "/home/user/palinode/projects/palinode.md"
                finally:
                    config.search.daily_penalty = original_penalty
