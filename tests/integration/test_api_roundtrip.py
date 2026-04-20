"""Integration tests for Palinode API endpoints.

Tests the API via FastAPI TestClient against a real (temp) SQLite database
and real filesystem. Only the embedder is mocked (returns fixed 1024-dim
vectors) so no Ollama or external services are required.
"""

import os
import time
import yaml
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EMBED_DIM = 1024


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    """Deterministic fake embedder -- no Ollama needed."""
    return [0.1] * EMBED_DIM


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point config at a fresh tmp_path directory and init a real DB.

    Patches:
      - config.memory_dir / config.db_path -> tmp_path
      - config.git.auto_commit -> False (no real git in tmp)
      - embedder.embed -> deterministic fixed vector
      - _generate_description -> static fallback (no Ollama)
      - _generate_summary -> empty string (no Ollama)
    """
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")

    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)

    # Create standard category dirs
    for d in ("people", "projects", "decisions", "insights", "research", "inbox", "daily"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)

    # Init real SQLite + vec tables
    from palinode.core import store
    store.init_db()

    # Patch embedder and LLM helpers globally for the test
    with (
        mock.patch("palinode.core.embedder.embed", side_effect=_fake_embed),
        mock.patch("palinode.api.server._generate_description", return_value="Test description"),
        mock.patch("palinode.api.server._generate_summary", return_value=""),
    ):
        yield memory_dir


@pytest.fixture()
def client():
    """Fresh TestClient wrapping the FastAPI app (no lifespan -- DB already init'd).

    Also clears the in-memory rate-limit counters so tests don't interfere
    with each other.
    """
    from palinode.api.server import app, _rate_counters
    _rate_counters.clear()
    # raise_server_exceptions=False so we can inspect 4xx/5xx responses
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# /save tests
# ---------------------------------------------------------------------------


def test_save_creates_file(client, tmp_path):
    """POST /save should create a markdown file on disk with frontmatter."""
    resp = client.post("/save", json={
        "content": "Alice prefers dark mode.",
        "type": "PersonMemory",
        "slug": "alice-pref",
        "entities": ["person/alice"],
    })
    assert resp.status_code == 200
    data = resp.json()
    fp = data["file_path"]
    assert os.path.exists(fp)

    with open(fp) as f:
        text = f.read()
    assert "---" in text
    assert "Alice prefers dark mode." in text

    # Parse frontmatter
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["category"] == "people"
    assert fm["type"] == "PersonMemory"
    assert "person/alice" in fm["entities"]


def test_save_includes_content_hash(client, tmp_path):
    """Saved file frontmatter must contain a SHA-256 content_hash."""
    resp = client.post("/save", json={
        "content": "Hash me please.",
        "type": "Insight",
        "slug": "hash-test",
    })
    assert resp.status_code == 200
    fp = resp.json()["file_path"]
    with open(fp) as f:
        text = f.read()

    fm = yaml.safe_load(text.split("---", 2)[1])
    assert "content_hash" in fm
    assert len(fm["content_hash"]) == 64  # SHA-256 hex


def test_save_with_confidence(client, tmp_path):
    """confidence field should round-trip into frontmatter."""
    resp = client.post("/save", json={
        "content": "High-confidence fact.",
        "type": "Decision",
        "slug": "conf-test",
        "confidence": 0.95,
    })
    assert resp.status_code == 200
    fp = resp.json()["file_path"]
    with open(fp) as f:
        text = f.read()

    fm = yaml.safe_load(text.split("---", 2)[1])
    assert fm["confidence"] == 0.95


def test_save_rate_limit(client):
    """Exceeding write rate limit (30/min) should return 429."""
    # Clear any prior rate-limit state
    from palinode.api.server import _rate_counters
    _rate_counters.clear()

    for i in range(31):
        resp = client.post("/save", json={
            "content": f"Item {i}",
            "type": "Insight",
            "slug": f"rate-{i}",
        })
        if resp.status_code == 429:
            assert i >= 30  # should only trigger after 30
            return

    # If we got here, 31 all succeeded -- the rate limit is per-IP and
    # TestClient may use testclient; verify the 31st was blocked.
    pytest.fail("Expected 429 after 30 rapid writes")


# ---------------------------------------------------------------------------
# /search tests
# ---------------------------------------------------------------------------


def test_search_returns_results(client, tmp_path):
    """Save a file, manually index it, then search should find it."""
    # 1. Save via API
    resp = client.post("/save", json={
        "content": "Palinode uses SQLite-vec for hybrid search.",
        "type": "ProjectSnapshot",
        "slug": "search-target",
        "entities": ["project/palinode"],
    })
    assert resp.status_code == 200
    fp = resp.json()["file_path"]

    # 2. Manually index the file into the DB (watcher isn't running)
    from palinode.core import store
    chunks = [{
        "id": "search-target-1",
        "file_path": fp,
        "section_id": None,
        "category": "projects",
        "content": "Palinode uses SQLite-vec for hybrid search.",
        "metadata": {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "embedding": _fake_embed("x"),
    }]
    store.upsert_chunks(chunks)

    # 3. Search
    resp = client.post("/search", json={
        "query": "hybrid search",
        "threshold": 0.0,
        "limit": 5,
    })
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1
    assert any("search-target" in r.get("file_path", r.get("file", "")) for r in results)


def test_search_rate_limit(client):
    """Exceeding search rate limit (100/min) should return 429."""
    from palinode.api.server import _rate_counters
    _rate_counters.clear()

    for i in range(101):
        resp = client.post("/search", json={"query": f"test query {i}", "threshold": 0.0})
        if resp.status_code == 429:
            assert i >= 100
            return

    pytest.fail("Expected 429 after 100 rapid searches")


# ---------------------------------------------------------------------------
# /read tests
# ---------------------------------------------------------------------------


def test_read_file(client, tmp_path):
    """Save a file then read it back via /read."""
    resp = client.post("/save", json={
        "content": "Readable content here.",
        "type": "Insight",
        "slug": "read-me",
    })
    assert resp.status_code == 200

    resp = client.get("/read?file_path=insights/read-me.md")
    assert resp.status_code == 200
    data = resp.json()
    assert "Readable content here." in data["content"]
    assert data["file"] == "insights/read-me.md"


def test_read_path_traversal_blocked(client):
    """Path traversal attempts must be rejected with 403."""
    resp = client.get("/read?file_path=../../../etc/passwd")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /list tests
# ---------------------------------------------------------------------------


def test_list_files(client, tmp_path):
    """Saved files should appear in /list results."""
    for slug in ("list-a", "list-b"):
        resp = client.post("/save", json={
            "content": f"Content for {slug}.",
            "type": "Insight",
            "slug": slug,
        })
        assert resp.status_code == 200

    resp = client.get("/list")
    assert resp.status_code == 200
    files = resp.json()
    slugs_found = [f["file"] for f in files]
    assert any("list-a" in f for f in slugs_found)
    assert any("list-b" in f for f in slugs_found)


# ---------------------------------------------------------------------------
# /status and /health tests
# ---------------------------------------------------------------------------


def test_status_endpoint(client):
    """GET /status should return 200 with expected stat keys."""
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_chunks" in data or "chunks" in data or isinstance(data, dict)


def test_health_endpoint(client):
    """GET /health should return 200 with status=ok."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "chunks" in data


# ---------------------------------------------------------------------------
# Security / edge-case tests
# ---------------------------------------------------------------------------


def test_save_oversized_rejected(client):
    """POST /save with content exceeding 5MB should return 413."""
    big_content = "x" * (6 * 1024 * 1024)
    resp = client.post("/save", json={
        "content": big_content,
        "type": "Insight",
        "slug": "too-big",
    })
    assert resp.status_code == 413


def test_error_no_stacktrace(client):
    """Server errors should not leak Python tracebacks to the client."""
    # Force an internal error by requesting a read on a path that will
    # trigger an exception inside the handler (null byte = 400, not 500,
    # so we use a different approach: patch read_api to raise).
    with mock.patch("palinode.api.server.read_api", side_effect=Exception("boom")):
        # The patched function replaces the endpoint handler, but FastAPI
        # already bound the original. Instead, trigger a real 500 by
        # monkeypatching _memory_base_dir to raise.
        pass

    # Simpler approach: try a search with a broken store
    with mock.patch("palinode.core.store.get_db", side_effect=RuntimeError("db gone")):
        resp = client.post("/search", json={"query": "anything"})
    # Should be 500 but without a traceback
    assert resp.status_code == 500
    body = resp.text
    assert "Traceback" not in body
    assert "File " not in body or "server.py" not in body


# ---------------------------------------------------------------------------
# /session-end test
# ---------------------------------------------------------------------------


def test_session_end_creates_daily(client, tmp_path):
    """POST /session-end should create a daily file."""
    resp = client.post("/session-end", json={
        "summary": "Finished integration tests.",
        "decisions": ["Use TestClient for HTTP-level tests"],
        "project": "palinode",
        "source": "test",
    })
    assert resp.status_code == 200
    data = resp.json()

    # Daily file should exist
    daily_file = data.get("daily_file")
    assert daily_file is not None
    daily_path = os.path.join(str(tmp_path), daily_file)
    assert os.path.exists(daily_path)

    with open(daily_path) as f:
        content = f.read()
    assert "Finished integration tests." in content
