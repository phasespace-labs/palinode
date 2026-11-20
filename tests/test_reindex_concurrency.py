"""Tests for /reindex concurrency guard.

Verifies that:
- A single reindex call succeeds and returns `files_reindexed`.
- A second concurrent call receives HTTP 409 with a helpful message.
- After completion, _reindex_state["running"] is False and /status reflects it.
- The 409 response body contains the expected detail string.

handler._process_file and store.rebuild_fts are patched so the suite stays
Ollama-free and fast; reindex walks a real file in tmp_path.  Config is redirected via memory_dir
(palinode_dir is a property alias for memory_dir).
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.api import server as srv
from palinode.core.config import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_reindex_state(monkeypatch):
    """Ensure _reindex_state is clean before and after every test."""
    monkeypatch.setitem(srv._reindex_state, "running", False)
    monkeypatch.setitem(srv._reindex_state, "started_at", None)
    monkeypatch.setitem(srv._reindex_state, "files_processed", 0)
    monkeypatch.setitem(srv._reindex_state, "total_files", 0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with memory_dir + db_path pointing at tmp_path.

    Uses TestClient as a context manager so lifespan startup runs and
    store.init_db() creates the schema in the tmp_path-backed DB. Without
    the context manager, lifespan never fires and /status crashes when it
    queries chunks_fts (CI repro: tests pass on a developer machine that
    has a populated ~/palinode DB, fail on a fresh CI runner).
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# /reindex tests
# ---------------------------------------------------------------------------


def test_single_reindex_succeeds(client):
    """A lone /reindex call with no markdown files returns status=success."""
    with (
        mock.patch("palinode.core.store.gc_orphaned_chunks", return_value=(0, 0)),
        mock.patch("palinode.core.store.rebuild_fts", return_value=0),
    ):
        resp = client.post("/reindex")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "files_reindexed" in body
    assert body["gc_paths_removed"] == 0
    assert body["gc_chunks_removed"] == 0


def test_409_detail_message(client):
    """While the lock is held a second POST must get 409 with a clear message."""
    assert srv._reindex_lock.acquire(blocking=False)
    try:
        resp = client.post("/reindex")
    finally:
        srv._reindex_lock.release()

    assert resp.status_code == 409
    body = resp.json()
    assert "detail" in body
    detail = body["detail"].lower()
    assert "already running" in detail
    assert "/status" in detail


def test_invalid_since_releases_lock(client):
    """An early validation error must not leave future reindexes locked out."""
    response = client.post("/reindex", params={"since": "not-a-timestamp"})

    assert response.status_code == 400
    assert srv._reindex_lock.acquire(blocking=False)
    srv._reindex_lock.release()


def test_concurrent_calls_one_wins_one_loses(client, tmp_path):
    """Two concurrent POSTs run the underlying reindex exactly once."""
    (tmp_path / "reindex.md").write_text("# reindex\n", encoding="utf-8")
    start_requests = threading.Barrier(3)
    work_started = threading.Event()
    release_work = threading.Event()
    rejected = threading.Event()
    responses = []
    work_calls = []
    responses_lock = threading.Lock()

    def _slow_process(_self, filepath):
        work_calls.append(filepath)
        work_started.set()
        assert release_work.wait(timeout=5)

    def _post_reindex():
        start_requests.wait(timeout=5)
        response = client.post("/reindex")
        with responses_lock:
            responses.append(response)
        if response.status_code == 409:
            rejected.set()

    threads = [threading.Thread(target=_post_reindex, daemon=True) for _ in range(2)]
    with (
        mock.patch(
            "palinode.indexer.watcher.PalinodeHandler._process_file",
            new=_slow_process,
        ),
        mock.patch("palinode.core.store.gc_orphaned_chunks", return_value=(0, 0)),
        mock.patch("palinode.core.store.rebuild_fts", return_value=0),
    ):
        for thread in threads:
            thread.start()
        start_requests.wait(timeout=5)
        assert work_started.wait(timeout=5)
        assert rejected.wait(timeout=5)
        release_work.set()
        for thread in threads:
            thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert work_calls == [str(tmp_path / "reindex.md")]


def test_health_responds_while_reindex_runs(client, tmp_path):
    """Blocking reindex work must not occupy the API event loop."""
    (tmp_path / "reindex.md").write_text("# reindex\n", encoding="utf-8")
    work_started = threading.Event()
    release_work = threading.Event()
    reindex_responses = []

    def _slow_process(_self, _filepath):
        work_started.set()
        assert release_work.wait(timeout=5)

    def _post_reindex():
        reindex_responses.append(client.post("/reindex"))

    reindex_thread = threading.Thread(target=_post_reindex, daemon=True)
    with (
        mock.patch(
            "palinode.indexer.watcher.PalinodeHandler._process_file",
            new=_slow_process,
        ),
        mock.patch("palinode.core.store.gc_orphaned_chunks", return_value=(0, 0)),
        mock.patch("palinode.core.store.rebuild_fts", return_value=0),
        mock.patch("httpx.get"),
    ):
        reindex_thread.start()
        assert work_started.wait(timeout=5)
        started_at = time.monotonic()
        health_response = client.get("/health")
        elapsed = time.monotonic() - started_at
        release_work.set()
        reindex_thread.join(timeout=5)

    assert not reindex_thread.is_alive()
    assert health_response.status_code == 200
    assert elapsed < 1.0
    assert [response.status_code for response in reindex_responses] == [200]


def test_reindex_state_resets_after_completion(client):
    """_reindex_state["running"] is False after /reindex completes."""
    with (
        mock.patch("palinode.core.store.gc_orphaned_chunks", return_value=(0, 0)),
        mock.patch("palinode.core.store.rebuild_fts", return_value=0),
    ):
        resp = client.post("/reindex")

    assert resp.status_code == 200
    assert srv._reindex_state["running"] is False


def test_reindex_state_tracks_progress(tmp_path, monkeypatch):
    """_reindex_state fields are populated during a reindex run.

    We patch _process_file to capture the state mid-run without needing Ollama.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    # Create two fake markdown files
    (tmp_path / "a.md").write_text("# A\ncontent\n")
    (tmp_path / "b.md").write_text("# B\ncontent\n")

    state_snapshots: list[dict] = []

    def _fake_process(self, filepath):  # noqa: ANN001
        state_snapshots.append(dict(srv._reindex_state))

    c = TestClient(app, raise_server_exceptions=False)
    with (
        mock.patch(
            "palinode.indexer.watcher.PalinodeHandler._process_file",
            new=_fake_process,
        ),
        mock.patch(
            "palinode.indexer.watcher.PalinodeHandler.is_valid_file",
            return_value=True,
        ),
        mock.patch("palinode.core.store.gc_orphaned_chunks", return_value=(1, 2)),
        mock.patch("palinode.core.store.rebuild_fts", return_value=0),
    ):
        resp = c.post("/reindex")

    assert resp.status_code == 200
    assert resp.json()["gc_paths_removed"] == 1
    assert resp.json()["gc_chunks_removed"] == 2
    # During processing, running should have been True
    assert any(s["running"] for s in state_snapshots)
    # total_files should have been set before the loop
    assert all(s["total_files"] == 2 for s in state_snapshots)
    # After completion, state should be reset
    assert srv._reindex_state["running"] is False


# ---------------------------------------------------------------------------
# /status tests
# ---------------------------------------------------------------------------


def test_status_includes_reindex_key(client):
    """GET /status includes a 'reindex' sub-dict with the expected keys."""
    with (
        mock.patch("httpx.get"),
        mock.patch(
            "palinode.core.git_tools.commit_count",
            return_value={"total_commits": 0, "summary": ""},
        ),
        mock.patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = mock.Mock(stdout="0\n", returncode=0)
        resp = client.get("/status")

    assert resp.status_code == 200
    body = resp.json()
    assert "reindex" in body
    reindex = body["reindex"]
    for key in ("running", "started_at", "files_processed", "total_files"):
        assert key in reindex, f"Missing key {key!r} in reindex status"
    assert reindex["running"] is False


def test_status_reindex_running_true_while_locked(client):
    """GET /status reports running=True while a reindex is in progress."""
    srv._reindex_state.update({
        "running": True,
        "started_at": "2026-04-26T12:00:00Z",
        "files_processed": 42,
        "total_files": 564,
    })

    with (
        mock.patch("httpx.get"),
        mock.patch(
            "palinode.core.git_tools.commit_count",
            return_value={"total_commits": 0, "summary": ""},
        ),
        mock.patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = mock.Mock(stdout="0\n", returncode=0)
        resp = client.get("/status")

    assert resp.status_code == 200
    reindex = resp.json()["reindex"]
    assert reindex["running"] is True
    assert reindex["files_processed"] == 42
    assert reindex["total_files"] == 564
    assert reindex["started_at"] == "2026-04-26T12:00:00Z"
