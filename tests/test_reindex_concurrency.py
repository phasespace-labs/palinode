"""Tests for /reindex concurrency guard (#200).

Verifies that:
- A single reindex call succeeds and returns `files_reindexed`.
- A second concurrent call receives HTTP 409 with a helpful message.
- After completion, _reindex_state["running"] is False and /status reflects it.
- The 409 response body contains the expected detail string.

handler._process_file, glob.glob and store.rebuild_fts are patched so the
suite stays Ollama-free and fast.  Config is redirected via memory_dir
(palinode_dir is a property alias for memory_dir).
"""
from __future__ import annotations

import threading
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
def _reset_reindex_state():
    """Ensure _reindex_state is clean before and after every test."""
    srv._reindex_state.update({
        "running": False,
        "started_at": None,
        "files_processed": 0,
        "total_files": 0,
    })
    yield
    srv._reindex_state.update({
        "running": False,
        "started_at": None,
        "files_processed": 0,
        "total_files": 0,
    })


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
        mock.patch("glob.glob", return_value=[]),
        mock.patch("palinode.core.store.rebuild_fts", return_value=0),
    ):
        resp = client.post("/reindex")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "files_reindexed" in body


def test_409_detail_message(client):
    """While the lock is held a second POST must get 409 with a clear message."""
    # Mock the lock as locked so the guard fires without needing a live async lock.
    with mock.patch.object(srv._reindex_lock, "locked", return_value=True):
        resp = client.post("/reindex")

    assert resp.status_code == 409
    body = resp.json()
    assert "detail" in body
    detail = body["detail"].lower()
    assert "already running" in detail
    assert "/status" in detail


def test_concurrent_calls_one_wins_one_loses(client):
    """Two truly concurrent calls: one 200, one 409.

    We hold the asyncio lock from a background thread so the second HTTP
    request fires while the first is in progress.
    """
    lock_held = threading.Event()
    release_lock = threading.Event()

    def _hold_lock():
        """Acquire the reindex lock from a new event loop, signal, then wait."""
        import asyncio

        async def _inner():
            async with srv._reindex_lock:
                srv._reindex_state["running"] = True
                lock_held.set()
                await asyncio.get_event_loop().run_in_executor(None, release_lock.wait)
            srv._reindex_state["running"] = False

        asyncio.run(_inner())

    lock_thread = threading.Thread(target=_hold_lock, daemon=True)
    lock_thread.start()
    lock_held.wait(timeout=5)

    try:
        # Second POST fires while the lock is held — must be 409.
        resp = client.post("/reindex")
        assert resp.status_code == 409
    finally:
        release_lock.set()
        lock_thread.join(timeout=5)


def test_reindex_state_resets_after_completion(client):
    """_reindex_state["running"] is False after /reindex completes."""
    with (
        mock.patch("glob.glob", return_value=[]),
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
        mock.patch("palinode.core.store.rebuild_fts", return_value=0),
    ):
        resp = c.post("/reindex")

    assert resp.status_code == 200
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
