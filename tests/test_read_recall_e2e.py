"""End-to-end coverage for the /read → recall-recording path (#479).

ADR-006/007 require that a direct ``GET /read`` call persists access metadata
(``recall_count`` / ``last_recalled``) back to all SQLite chunks of the read
file. ``record_recall_for_paths`` is unit-tested in ``test_recall_feedback.py``;
this file covers the integration seam: the FastAPI endpoint must actually invoke
that function, and the DB must reflect the increment.

The test drives the full pipeline end-to-end:

1. POST /save (embed mocked) — creates the file and inline-indexes its chunks.
2. GET /read          — triggers ``store.record_recall_for_paths`` in server.py.
3. Direct DB query    — asserts ``recall_count == 1`` and ``last_recalled`` is
   a non-null timestamp on the saved chunk.

A second GET /read asserts ``recall_count == 2``, proving accumulation works
end-to-end, not just on the first hit.

No Ollama required — the embedder and security scanner are mocked.
Real SQLite in tmp_path (no mocked DB — repo rule).
"""
from __future__ import annotations

import importlib
import os
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with fresh tmp memory_dir + real SQLite; git + auth off.

    Mirrors the fixture in test_update_policy_axis.py: reload the server
    module with no PALINODE_API_TOKEN so bearer auth does not 401 the test.
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_io():
    """Mock the embedder and security scanner — neither is under test here."""
    return (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR),
    )


def _recall_row(db_path: str, file_path: str) -> tuple[int, str | None]:
    """Return (recall_count, last_recalled) for all chunks of file_path.

    Sums recall_count across chunks so the assertion is file-level (a multi-
    chunk file increments each chunk; a single-chunk file is the simplest case).
    Returns the MAX last_recalled so NULL vs timestamp is clearly distinguishable.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT SUM(recall_count) as rc, MAX(last_recalled) as lr "
            "FROM chunks WHERE file_path LIKE ?",
            (f"%{file_path}%",),
        ).fetchone()
    finally:
        conn.close()
    return (row["rc"] or 0, row["lr"])


# ── Main assertion: GET /read stamps recall_count end-to-end ─────────────────

def test_read_stamps_recall_count_e2e(client, tmp_path):
    """POST /save then GET /read: the chunk's recall_count increments in the DB.

    This is the integration seam #479 requires: the FastAPI /read handler calls
    store.record_recall_for_paths. If that call is removed or the file_path
    doesn't match any chunks, recall_count stays at 0 and this test fails.
    """
    scan, embed = _patch_io()
    with scan, embed:
        resp = client.post("/save", json={
            "content": "Palinode recall recording verification content.",
            "type": "Insight",
            "slug": "recall-e2e-slug",
        })
    assert resp.status_code == 200, resp.text
    save_data = resp.json()
    # The relative path the /read endpoint accepts (e.g. "insights/recall-e2e-slug.md")
    rel_path = "insights/recall-e2e-slug.md"

    # Pre-condition: chunk exists but has never been recalled.
    db_path = str(tmp_path / ".palinode.db")
    rc_before, lr_before = _recall_row(db_path, rel_path)
    assert rc_before == 0, f"recall_count should be 0 before any /read, got {rc_before}"
    assert lr_before is None, f"last_recalled should be NULL before any /read, got {lr_before}"

    # Act: GET /read — this is the seam under test.
    resp2 = client.get(f"/read?file_path={rel_path}")
    assert resp2.status_code == 200, resp2.text
    assert "recall-e2e-slug" in resp2.json().get("file", "")

    # Assert: recall_count incremented, last_recalled is now set.
    rc_after, lr_after = _recall_row(db_path, rel_path)
    assert rc_after == 1, (
        f"recall_count should be 1 after one /read, got {rc_after}. "
        "If store.record_recall_for_paths was removed from the /read handler, "
        "this test catches that regression."
    )
    assert lr_after is not None, "last_recalled must be set after /read"


def test_read_recall_accumulates_on_repeated_reads(client, tmp_path):
    """A second GET /read increments recall_count to 2, proving accumulation."""
    scan, embed = _patch_io()
    with scan, embed:
        client.post("/save", json={
            "content": "Repeated-read recall accumulation test content.",
            "type": "Insight",
            "slug": "recall-repeat-slug",
        })

    rel_path = "insights/recall-repeat-slug.md"
    db_path = str(tmp_path / ".palinode.db")

    client.get(f"/read?file_path={rel_path}")
    rc1, _ = _recall_row(db_path, rel_path)
    assert rc1 == 1, f"Expected recall_count=1 after first read, got {rc1}"

    client.get(f"/read?file_path={rel_path}")
    rc2, _ = _recall_row(db_path, rel_path)
    assert rc2 == 2, f"Expected recall_count=2 after second read, got {rc2}"


def test_read_404_does_not_stamp_recall(client, tmp_path):
    """A 404 /read (nonexistent file) must NOT stamp any recall — record_recall_for_paths
    is only called after the file is confirmed to exist and content is returned."""
    db_path = str(tmp_path / ".palinode.db")

    resp = client.get("/read?file_path=insights/does-not-exist.md")
    assert resp.status_code == 404

    # No chunks exist for this path, and none should have been created.
    rc, lr = _recall_row(db_path, "does-not-exist")
    assert rc == 0
    assert lr is None
