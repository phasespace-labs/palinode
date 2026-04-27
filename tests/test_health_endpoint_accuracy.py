"""Regression tests for /health endpoint accuracy.

Verifies that /health reports live chunk and entity counts rather than stale,
cached, or hardcoded zeros.

After a directory rename, /health reported
chunks=0/entities=0 while /list returned real content.  The fix delegates to
store.get_stats() — the same code path used by /status — so both endpoints
stay in sync.

Test strategy:
- Populate a real tmp_path-backed SQLite DB via store.upsert_chunks() and
  store.upsert_entities() (no mocks; project standard is real DBs in tmp_path)
- Hit GET /health via FastAPI TestClient used as a context manager so the
  lifespan startup runs and store.init_db() creates the schema before inserts
- Assert counts match exactly
- Degenerate case: empty DB → both report 0 (confirms "actually empty" is
  correctly distinguished from a bug)
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a fresh tmp_path DB.

    Uses TestClient as a context manager so the lifespan startup fires and
    store.init_db() creates the schema before any inserts.  Without the context
    manager, lifespan never runs; schema creation is skipped and every query
    raises OperationalError.
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_chunks(n: int) -> None:
    """Insert *n* minimal rows directly into the `chunks` table.

    We bypass store.upsert_chunks() because that function also inserts into
    chunks_vec, which requires a valid float-array embedding.  For counting
    tests we only need rows in `chunks`; a direct INSERT is faster and avoids
    the Ollama dependency.
    """
    db = store.get_db()
    try:
        for i in range(n):
            db.execute(
                """
                INSERT OR REPLACE INTO chunks
                (id, file_path, section_id, category, content,
                 metadata, created_at, last_updated, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"test-chunk-{i}",
                    f"tests/fixture-{i}.md",
                    "root",
                    "insights",
                    f"Fixture chunk content {i}",
                    "{}",
                    "2026-04-26T00:00:00Z",
                    "2026-04-26T00:00:00Z",
                    f"hash-{i}",
                ),
            )
        db.commit()
    finally:
        db.close()


def _insert_entities(entity_refs: list[str]) -> None:
    """Insert entity rows directly via store.upsert_entities()."""
    metadata = {"entities": entity_refs, "category": "insights"}
    store.upsert_entities("tests/fixture-entities.md", metadata)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health_reports_correct_counts_on_populated_db(client):
    """GET /health returns chunks=5 and entities=3 after inserting that data.

    This is the primary regression guard for this drift scenario. A fresh empty DB is
    populated with 5 chunks and 3 entities before the request; the response
    must reflect those live values — not zeros.
    """
    _insert_chunks(5)
    _insert_entities(["project/palinode", "person/paul", "tool/sqlite"])

    with mock.patch("httpx.get"):  # skip Ollama reachability check
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", f"unexpected status: {body}"
    assert body["chunks"] == 5, f"expected chunks=5, got {body['chunks']}"
    assert body["entities"] == 3, f"expected entities=3, got {body['entities']}"


def test_health_reports_zero_on_empty_db(client):
    """GET /health reports chunks=0 and entities=0 on a genuinely empty DB.

    This degenerate case confirms the endpoint correctly distinguishes an
    actually-empty database from a bug — zeros are valid when the DB is empty.
    """
    with mock.patch("httpx.get"):
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", f"unexpected status: {body}"
    assert body["chunks"] == 0, f"expected chunks=0, got {body['chunks']}"
    assert body["entities"] == 0, f"expected entities=0, got {body['entities']}"


def test_health_counts_match_status_counts(client):
    """/health and /status report the same chunk count after inserts.

    Guards against future divergence between the two endpoints' count paths.
    """
    _insert_chunks(7)
    _insert_entities(["project/alpha", "person/bob"])

    with (
        mock.patch("httpx.get"),
        mock.patch(
            "palinode.core.git_tools.commit_count",
            return_value={"total_commits": 0, "summary": ""},
        ),
        mock.patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = mock.Mock(stdout="0\n", returncode=0)
        health_resp = client.get("/health")
        status_resp = client.get("/status")

    assert health_resp.status_code == 200
    assert status_resp.status_code == 200

    health = health_resp.json()
    status = status_resp.json()

    assert health["chunks"] == status["total_chunks"], (
        f"/health chunks={health['chunks']} != /status total_chunks={status['total_chunks']}"
    )
