"""Tests for inline embedding on POST /save and watcher index-presence guard (#251).

Two failure modes covered:

A. Race between /save returning 200 and the watcher embedding the file.
   Fix: API save calls ``palinode.indexer.index_file.index_file`` synchronously
   before returning, so the chunk is queryable as soon as the response lands.

B. Re-saving identical content was a silent no-op even when the original embed
   never made it to the FTS5 / vec0 indices. Fix: ``index_file`` checks for the
   presence of both index entries and re-embeds when either is missing,
   regardless of ``content_hash`` equality.

Mocks the embedder so tests don't require Ollama.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.api import server as srv
from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.indexer.index_file import index_file


_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with memory_dir + db_path on tmp_path; git auto-commit off.

    Also clears the in-memory rate-limit counters before each test so a
    test file with several saves does not leak into the next file's
    budget when run as part of the full suite.
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    srv._rate_counters.clear()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_scan():
    return patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))


def _patch_embed_ok():
    """Patch the embedder module-attribute used by ``index_file``."""
    return patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR)


def _patch_embed_fail():
    return patch("palinode.core.embedder.embed", return_value=[])


# ---------------------------------------------------------------------------
# Failure mode A: race between /save and embed completion
# ---------------------------------------------------------------------------


class TestSaveEmbedsInline:

    def test_save_response_marks_indexed_true(self, client):
        """POST /save returns ``indexed: true`` when the embedder succeeds."""
        with _patch_scan(), _patch_embed_ok():
            res = client.post(
                "/save",
                json={
                    "content": "Inline-embed sentinel decision marker.",
                    "type": "Insight",
                    "slug": "inline-embed-sentinel",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("indexed") is True
        assert body.get("embedded") is True

    def test_chunk_visible_in_db_immediately_after_save(self, client):
        """After /save returns, the chunk row + vec0 row both exist.

        The race fix is that /save shouldn't return until these exist —
        no sleep or polling should be required. We assert vec0 presence
        explicitly because vector-search returning zero results is the
        externally observable symptom in #251.
        """
        with _patch_scan(), _patch_embed_ok():
            res = client.post(
                "/save",
                json={
                    "content": "No-race-window sentinel for embedding test.",
                    "type": "Insight",
                    "slug": "no-race-window",
                },
            )
        assert res.status_code == 200, res.text
        file_path = res.json()["file_path"]

        db = store.get_db()
        try:
            chunk_rows = db.execute(
                "SELECT id FROM chunks WHERE file_path = ?", (file_path,)
            ).fetchall()
            assert len(chunk_rows) >= 1, "no chunk written by inline indexer"

            chunk_id = chunk_rows[0]["id"]

            vec_row = db.execute(
                "SELECT 1 FROM chunks_vec WHERE id = ?", (chunk_id,)
            ).fetchone()
            assert vec_row is not None, "vec0 row missing immediately after /save"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Failure mode B: re-save of identical content with broken index entries
# ---------------------------------------------------------------------------


class TestResaveRecoversFromBrokenIndex:

    def test_resave_after_vec_loss_re_embeds(self, client):
        """If the vec0 row is gone but the chunk row remains, /save again re-embeds.

        Pre-fix behaviour: watcher's ``content_hash`` shortcut hit, said
        "all chunks unchanged", and the vec0 row stayed missing — the
        memory was unsearchable forever via vector recall (the exact
        symptom in #251).

        Post-fix: ``index_file`` notices the missing vec0 row and embeds.
        """
        payload = {
            "content": "Resave recovery sentinel for vec loss.",
            "type": "Insight",
            "slug": "resave-vec-loss",
        }
        with _patch_scan(), _patch_embed_ok():
            res1 = client.post("/save", json=payload)
        assert res1.status_code == 200, res1.text
        file_path = res1.json()["file_path"]

        # Simulate the broken state: delete the vec0 row but leave the
        # chunk row + content_hash intact. This mirrors the production
        # failure mode where ``upsert_chunks`` swallowed a vec0 write
        # error and committed a chunks row with no embedding.
        db = store.get_db()
        try:
            cursor = db.cursor()
            cursor.execute(
                "SELECT id FROM chunks WHERE file_path = ?", (file_path,)
            )
            ids = [r["id"] for r in cursor.fetchall()]
            assert ids
            for cid in ids:
                cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (cid,))
            db.commit()
        finally:
            db.close()

        # Confirm the vec entries really are gone.
        db = store.get_db()
        try:
            for cid in ids:
                vec_row = db.execute(
                    "SELECT 1 FROM chunks_vec WHERE id = ?", (cid,)
                ).fetchone()
                assert vec_row is None, "test setup: vec row still present"
        finally:
            db.close()

        # Re-save the same content. Pre-fix this was a no-op.
        with _patch_scan(), _patch_embed_ok():
            res2 = client.post("/save", json=payload)
        assert res2.status_code == 200, res2.text
        assert res2.json().get("indexed") is True

        # vec rows should be back.
        db = store.get_db()
        try:
            for cid in ids:
                vec_row = db.execute(
                    "SELECT 1 FROM chunks_vec WHERE id = ?", (cid,)
                ).fetchone()
                assert vec_row is not None, "vec0 row not restored on resave"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Graceful degradation: embedder unreachable
# ---------------------------------------------------------------------------


class TestSaveDegradesGracefullyWhenEmbedderDown:

    def test_save_succeeds_with_embedded_false_when_ollama_down(self, client):
        """Embedder failure must not fail the save — file lands on disk,
        response carries ``embedded: false``, and the watcher can retry
        later. Failing the request would be a worse UX than the pre-fix
        race window.
        """
        with _patch_scan(), _patch_embed_fail():
            res = client.post(
                "/save",
                json={
                    "content": "Embedder-down graceful-degradation marker.",
                    "type": "Insight",
                    "slug": "embedder-down",
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("embedded") is False
        assert body.get("indexed") is False
        # File still on disk — caller can verify.
        import os
        assert os.path.exists(body["file_path"])


# ---------------------------------------------------------------------------
# Watcher fix: index_file re-embeds on missing FTS / vec rows
# ---------------------------------------------------------------------------


class TestIndexFileVerifiesIndexPresence:
    """Direct exercise of ``index_file`` (the watcher's underlying call).

    Asserts the defense-in-depth fix: ``content_hash`` match is no longer
    sufficient to skip embedding — both FTS5 and vec0 rows must also exist.
    """

    def test_missing_vec_row_triggers_reembed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "memory_dir", str(tmp_path))
        monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
        store.init_db()

        insights_dir = tmp_path / "insights"
        insights_dir.mkdir()
        file_path = insights_dir / "watcher-defense.md"
        md = (
            "---\n"
            "id: insights-watcher-defense\n"
            "category: insights\n"
            "type: Insight\n"
            "created_at: '2026-04-26T00:00:00+00:00'\n"
            "---\n\n"
            "Watcher defense-in-depth sentinel.\n"
        )
        file_path.write_text(md)

        # First pass: full embed.
        with _patch_embed_ok():
            outcome1 = index_file(str(file_path))
        assert outcome1["embedded"] is True
        assert outcome1["chunks_written"] >= 1
        assert outcome1["chunks_reembedded"] == 0

        # Wreck the vec rows but leave content_hash matching.
        db = store.get_db()
        try:
            cursor = db.cursor()
            cursor.execute(
                "SELECT id FROM chunks WHERE file_path = ?", (str(file_path),)
            )
            ids = [r["id"] for r in cursor.fetchall()]
            assert ids
            for cid in ids:
                cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (cid,))
            db.commit()
        finally:
            db.close()

        # Second pass: same file content, but missing vec0 entry.
        # Pre-fix this would have been a no-op ("All N chunks unchanged").
        # Post-fix this must re-embed.
        with _patch_embed_ok() as embed_mock:
            outcome2 = index_file(str(file_path))
        assert embed_mock.called, "embedder was not called — silent no-op regression"
        assert outcome2["chunks_reembedded"] >= 1
        assert outcome2["chunks_unchanged"] == 0

        # And the vec entries are back.
        db = store.get_db()
        try:
            for cid in ids:
                vec_row = db.execute(
                    "SELECT 1 FROM chunks_vec WHERE id = ?", (cid,)
                ).fetchone()
                assert vec_row is not None
        finally:
            db.close()

    def test_unchanged_with_intact_index_is_skipped(self, tmp_path, monkeypatch):
        """Sanity-check the fast path still skips when both indices are intact."""
        monkeypatch.setattr(config, "memory_dir", str(tmp_path))
        monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
        store.init_db()

        insights_dir = tmp_path / "insights"
        insights_dir.mkdir()
        file_path = insights_dir / "fast-path.md"
        md = (
            "---\n"
            "id: insights-fast-path\n"
            "category: insights\n"
            "type: Insight\n"
            "---\n\n"
            "Fast-path skip sentinel.\n"
        )
        file_path.write_text(md)

        with _patch_embed_ok():
            index_file(str(file_path))

        with _patch_embed_ok() as embed_mock:
            outcome = index_file(str(file_path))
        assert not embed_mock.called, "embedder was called for unchanged-and-indexed file"
        assert outcome["chunks_unchanged"] >= 1
        assert outcome["chunks_written"] == 0
        assert outcome["chunks_reembedded"] == 0
