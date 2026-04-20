"""Tests for ADR-008 ambient context search (Phase G1)."""
import pytest
from unittest.mock import patch, MagicMock
from palinode.core import store
from palinode.core.config import config


def test_search_hybrid_context_boost():
    """Context-matching results should get boosted above non-matching ones."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                with patch("palinode.core.store.get_entity_files") as mock_entities:
                    # Two results: palinode file and other-project file, initially ranked equally
                    mock_vec.return_value = [
                        {"file_path": "/mem/projects/palinode-adr.md", "content": "ADR-004", "score": 0.85},
                        {"file_path": "/mem/projects/other-adr.md", "content": "ADR-052", "score": 0.86},
                    ]
                    mock_fts.return_value = [
                        {"file_path": "/mem/projects/other-adr.md", "content": "ADR-052", "score": 0.7},
                        {"file_path": "/mem/projects/palinode-adr.md", "content": "ADR-004", "score": 0.6},
                    ]
                    # Entity lookup: project/palinode maps to the palinode file
                    mock_entities.return_value = [
                        {"file_path": "/mem/projects/palinode-adr.md", "category": "projects", "last_seen": "2026-04-12"}
                    ]

                    # Without context: other-project should rank first (higher combined score)
                    results_no_ctx = store.search_hybrid(
                        "ADR-004", query_embedding=[0.0]*1024, top_k=2, threshold=0.0,
                        context_entities=None,
                    )
                    assert len(results_no_ctx) == 2

                    # With context: palinode should rank first due to boost
                    results_ctx = store.search_hybrid(
                        "ADR-004", query_embedding=[0.0]*1024, top_k=2, threshold=0.0,
                        context_entities=["project/palinode"],
                    )
                    assert len(results_ctx) == 2
                    assert "palinode" in results_ctx[0]["file_path"]


def test_search_hybrid_no_context_unchanged():
    """Without context entities, search should behave exactly as before."""
    with patch("palinode.core.store.search") as mock_vec:
        with patch("palinode.core.store.search_fts") as mock_fts:
            with patch("palinode.core.store.get_db"):
                mock_vec.return_value = [{"file_path": "a.md", "content": "text", "score": 0.9}]
                mock_fts.return_value = []

                res = store.search_hybrid(
                    "query", query_embedding=[0.0]*1024, top_k=2,
                    context_entities=None,
                )
                assert len(res) >= 1
                # No boost applied — should work identically to pre-ADR-008


def test_search_hybrid_context_disabled():
    """When context.enabled is False, boost should not apply even with entities."""
    original = config.context.enabled
    try:
        config.context.enabled = False
        with patch("palinode.core.store.search") as mock_vec:
            with patch("palinode.core.store.search_fts") as mock_fts:
                with patch("palinode.core.store.get_db"):
                    with patch("palinode.core.store.get_entity_files") as mock_entities:
                        mock_vec.return_value = [
                            {"file_path": "a.md", "content": "text", "score": 0.9},
                        ]
                        mock_fts.return_value = []
                        # Should NOT be called when disabled
                        mock_entities.return_value = []

                        res = store.search_hybrid(
                            "query", query_embedding=[0.0]*1024, top_k=2,
                            context_entities=["project/palinode"],
                        )
                        mock_entities.assert_not_called()
    finally:
        config.context.enabled = original


def test_search_hybrid_boost_factor():
    """Boost factor of 1.0 should be a no-op."""
    original = config.context.boost
    try:
        config.context.boost = 1.0
        with patch("palinode.core.store.search") as mock_vec:
            with patch("palinode.core.store.search_fts") as mock_fts:
                with patch("palinode.core.store.get_db"):
                    with patch("palinode.core.store.get_entity_files") as mock_entities:
                        mock_vec.return_value = [
                            {"file_path": "a.md", "content": "text", "score": 0.9},
                        ]
                        mock_fts.return_value = []
                        # Should NOT be called when boost is 1.0
                        mock_entities.return_value = []

                        res = store.search_hybrid(
                            "query", query_embedding=[0.0]*1024, top_k=2,
                            context_entities=["project/palinode"],
                        )
                        mock_entities.assert_not_called()
    finally:
        config.context.boost = original


def test_search_vector_context_boost():
    """Non-hybrid search should also apply context boost (#92)."""
    with patch("palinode.core.store.get_db") as mock_db:
        with patch("palinode.core.store.get_entity_files") as mock_entities:
            # Simulate two vector results: kmd ranks higher by raw cosine
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [
                {"id": 1, "file_path": "/mem/projects/kmd-adr.md", "section_id": "root",
                 "content": "ADR-052", "category": "projects", "metadata": "{}",
                 "created_at": "2026-04-12", "distance": 0.3},
                {"id": 2, "file_path": "/mem/projects/palinode-adr.md", "section_id": "root",
                 "content": "ADR-004", "category": "projects", "metadata": "{}",
                 "created_at": "2026-04-12", "distance": 0.35},
            ]
            mock_db.return_value.cursor.return_value = mock_cursor
            mock_db.return_value.close = MagicMock()

            mock_entities.return_value = [
                {"file_path": "/mem/projects/palinode-adr.md", "category": "projects", "last_seen": "2026-04-12"}
            ]

            # Without context: kmd first (lower distance = higher score)
            results_no_ctx = store.search(
                query_embedding=[0.0]*1024, top_k=2, threshold=0.0,
                context_entities=None,
            )
            assert len(results_no_ctx) == 2
            assert "kmd" in results_no_ctx[0]["file_path"]

            # With context: palinode should be boosted to first
            results_ctx = store.search(
                query_embedding=[0.0]*1024, top_k=2, threshold=0.0,
                context_entities=["project/palinode"],
            )
            assert len(results_ctx) == 2
            assert "palinode" in results_ctx[0]["file_path"]


def test_search_vector_context_disabled_no_boost():
    """Non-hybrid search should not boost when context.enabled is False (#92)."""
    original = config.context.enabled
    try:
        config.context.enabled = False
        with patch("palinode.core.store.get_db") as mock_db:
            with patch("palinode.core.store.get_entity_files") as mock_entities:
                mock_cursor = MagicMock()
                mock_cursor.fetchall.return_value = [
                    {"id": 1, "file_path": "a.md", "section_id": "root",
                     "content": "text", "category": "projects", "metadata": "{}",
                     "created_at": "2026-04-12", "distance": 0.3},
                ]
                mock_db.return_value.cursor.return_value = mock_cursor
                mock_db.return_value.close = MagicMock()

                store.search(
                    query_embedding=[0.0]*1024, top_k=2, threshold=0.0,
                    context_entities=["project/palinode"],
                )
                mock_entities.assert_not_called()
    finally:
        config.context.enabled = original


def test_search_vector_boost_factor_one_noop():
    """Non-hybrid search: boost=1.0 should be a no-op (#92)."""
    original = config.context.boost
    try:
        config.context.boost = 1.0
        with patch("palinode.core.store.get_db") as mock_db:
            with patch("palinode.core.store.get_entity_files") as mock_entities:
                mock_cursor = MagicMock()
                mock_cursor.fetchall.return_value = [
                    {"id": 1, "file_path": "a.md", "section_id": "root",
                     "content": "text", "category": "projects", "metadata": "{}",
                     "created_at": "2026-04-12", "distance": 0.3},
                ]
                mock_db.return_value.cursor.return_value = mock_cursor
                mock_db.return_value.close = MagicMock()

                store.search(
                    query_embedding=[0.0]*1024, top_k=2, threshold=0.0,
                    context_entities=["project/palinode"],
                )
                mock_entities.assert_not_called()
    finally:
        config.context.boost = original


def test_context_config_defaults():
    """ContextConfig should have sane defaults."""
    assert config.context.enabled is True
    assert config.context.boost == 1.5
    assert config.context.auto_detect is True
    assert config.context.embed_augment is True
    assert isinstance(config.context.project_map, dict)


def test_mcp_resolve_context_explicit_env():
    """PALINODE_PROJECT env var should be used as explicit context."""
    from palinode.mcp import _resolve_context
    with patch.dict("os.environ", {"PALINODE_PROJECT": "project/palinode"}):
        result = _resolve_context()
        assert result == ["project/palinode"]


def test_mcp_resolve_context_short_name():
    """Short project name should be expanded to entity ref."""
    from palinode.mcp import _resolve_context
    with patch.dict("os.environ", {"PALINODE_PROJECT": "palinode"}):
        result = _resolve_context()
        assert result == ["project/palinode"]


def test_mcp_resolve_context_from_cwd():
    """CWD basename should resolve to project entity via auto-detect."""
    from palinode.mcp import _resolve_context
    with patch.dict("os.environ", {"CWD": "/Users/admin/Code/palinode"}, clear=False):
        with patch.dict("os.environ", {}, clear=False):
            # Remove PALINODE_PROJECT if set
            import os
            env = os.environ.copy()
            env.pop("PALINODE_PROJECT", None)
            with patch.dict("os.environ", env, clear=True):
                result = _resolve_context()
                assert result == ["project/palinode"]


def test_mcp_resolve_context_disabled():
    """When context is disabled, should return None."""
    from palinode.mcp import _resolve_context
    original = config.context.enabled
    try:
        config.context.enabled = False
        result = _resolve_context()
        assert result is None
    finally:
        config.context.enabled = original


def test_cli_resolve_context():
    """CLI context resolver should work from CWD."""
    from palinode.cli.search import _cli_resolve_context
    with patch("os.getcwd", return_value="/Users/admin/Code/palinode"):
        with patch.dict("os.environ", {}, clear=False):
            import os
            env = os.environ.copy()
            env.pop("PALINODE_PROJECT", None)
            with patch.dict("os.environ", env, clear=True):
                result = _cli_resolve_context()
                assert result == ["project/palinode"]
