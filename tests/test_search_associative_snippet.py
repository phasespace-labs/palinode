"""Regression tests for #392: /search-associative snippet enrichment.

The /search and /search-recent endpoints got per-result snippet bounding via
``_enrich_with_snippets`` in #359 (closing #352). The /search-associative
endpoint was overlooked and still returned un-truncated ``content`` fields,
so a single associative hit on a multi-fact aggregated file could blow MCP
tool-result budgets on its own.

These tests pin the behaviour:
1. Every result row has a ``snippet`` field.
2. Pathological content (5KB+) is windowed to ``config.search.snippet_max_chars``.
3. ``content`` is preserved untouched for API/CLI consumers.
4. ``content_truncated`` is set correctly per row.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.api import server as srv
from palinode.api.server import app
from palinode.core.config import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with memory_dir + db_path on tmp_path."""
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    srv._rate_counters.clear()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _stub_associative_results(rows):
    """Patch the store call the endpoint delegates to."""
    return patch.object(srv.store, "search_associative", return_value=rows)


def _stub_entity_detect():
    """Skip the embedding-based seed-entity detection branch."""
    return patch.object(
        srv.store, "detect_entities_in_text", return_value=["entity-stub"]
    )


def test_associative_results_get_snippet_field(client):
    """Every row in the /search-associative response carries a snippet."""
    rows = [
        {"file_path": "a.md", "content": "short alpha", "score": 0.9},
        {"file_path": "b.md", "content": "short beta", "score": 0.8},
    ]
    with _stub_entity_detect(), _stub_associative_results(rows):
        res = client.post(
            "/search-associative",
            json={"query": "alpha beta", "seed_entities": [], "limit": 5},
        )
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 2
    for row in body:
        assert "snippet" in row, f"missing snippet on {row['file_path']}"
        assert "content_truncated" in row


def test_associative_pathological_content_is_windowed(client):
    """A 5KB content field gets snippet-bounded; content preserved untouched."""
    big = ("infrastructure " * 400)  # ~6400 chars
    rows = [{"file_path": "infrastructure-misc.md", "content": big, "score": 0.82}]
    with _stub_entity_detect(), _stub_associative_results(rows):
        res = client.post(
            "/search-associative",
            json={"query": "infrastructure", "seed_entities": [], "limit": 1},
        )
    assert res.status_code == 200
    [row] = res.json()
    snippet_cap = config.search.snippet_max_chars
    # Snippet bounded (allow up to +2 for ellipses).
    assert len(row["snippet"]) <= snippet_cap + 2, (
        f"snippet={len(row['snippet'])} > cap={snippet_cap}"
    )
    # Original content preserved for callers that want it.
    assert row["content"] == big
    assert row["content_truncated"] is True


def test_associative_small_content_passes_through(client):
    """Content under the cap is preserved verbatim, not marked truncated."""
    rows = [{"file_path": "small.md", "content": "tiny content", "score": 0.7}]
    with _stub_entity_detect(), _stub_associative_results(rows):
        res = client.post(
            "/search-associative",
            json={"query": "tiny", "seed_entities": [], "limit": 1},
        )
    assert res.status_code == 200
    [row] = res.json()
    assert row["snippet"] == "tiny content"
    assert row["content_truncated"] is False
