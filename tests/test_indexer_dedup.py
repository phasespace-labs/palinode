"""Tests for the save/watcher dedup seam (#326).

The ``is_already_indexed`` function is the public guard that prevents
double-embedding when ``/save`` and the watcher both try to index the
same file from the same disk write.  These tests exercise it directly
against a real SQLite database (no mocks on the DB layer — per project
convention, integration tests use ``tmp_path``).

The concurrency test does not require real threads: it pre-populates
the DB to simulate the "first path won" state and then calls the second
path, asserting that no duplicate rows appear.
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.core.hashing import stable_md5_hexdigest
from palinode.indexer.index_file import index_file, is_already_indexed


_FAKE_VECTOR = [0.01] * 1024


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _init_db(tmp_path, monkeypatch):
    """Point palinode at a fresh tmp_path DB and initialise the schema."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    store.init_db()
    return tmp_path


def _insert_chunk(chunk_id: str, file_path: str, content: str, content_hash: str):
    """Insert a chunk row + vec0 row, simulating a completed index_file pass."""
    import json

    db = store.get_db()
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO chunks
        (id, file_path, section_id, category, content, metadata, created_at, last_updated, content_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, file_path, "root", "test", content, "{}", "", "", content_hash),
    )
    emb_json = json.dumps(_FAKE_VECTOR)
    try:
        cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (chunk_id,))
    except Exception:
        pass
    cursor.execute(
        "INSERT INTO chunks_vec (id, embedding) VALUES (?, ?)",
        (chunk_id, emb_json),
    )
    db.commit()
    db.close()


def _insert_chunk_without_vec(chunk_id: str, file_path: str, content: str, content_hash: str):
    """Insert a chunk row WITHOUT the vec0 entry — simulates a partial write."""
    db = store.get_db()
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO chunks
        (id, file_path, section_id, category, content, metadata, created_at, last_updated, content_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, file_path, "root", "test", content, "{}", "", "", content_hash),
    )
    db.commit()
    db.close()


def _count_vec_rows(chunk_id: str) -> int:
    """Count vec0 rows for a given chunk_id."""
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT count(*) as cnt FROM chunks_vec WHERE id = ?", (chunk_id,)
        ).fetchone()
        return row["cnt"]
    finally:
        db.close()


def _count_chunk_rows(file_path: str) -> int:
    """Count chunk rows for a given file_path."""
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT count(*) as cnt FROM chunks WHERE file_path = ?", (file_path,)
        ).fetchone()
        return row["cnt"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# is_already_indexed — unit tests
# ---------------------------------------------------------------------------


class TestIsAlreadyIndexed:

    def test_returns_true_for_matching_hash(self, _init_db):
        """Chunk exists with matching content_hash and vec0 row -> True."""
        content = "some content"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        chunk_id = stable_md5_hexdigest("/mem/test.md#root")

        _insert_chunk(chunk_id, "/mem/test.md", content, content_hash)

        assert is_already_indexed(chunk_id, content_hash) is True

    def test_returns_false_for_different_hash(self, _init_db):
        """Chunk exists but content_hash changed (content updated) -> False."""
        content_a = "original content"
        hash_a = hashlib.sha256(content_a.encode()).hexdigest()
        chunk_id = stable_md5_hexdigest("/mem/test.md#root")

        _insert_chunk(chunk_id, "/mem/test.md", content_a, hash_a)

        content_b = "updated content"
        hash_b = hashlib.sha256(content_b.encode()).hexdigest()

        assert is_already_indexed(chunk_id, hash_b) is False

    def test_returns_false_for_unknown_file(self, _init_db):
        """Empty DB, unknown chunk_id -> False."""
        content_hash = hashlib.sha256(b"anything").hexdigest()
        chunk_id = stable_md5_hexdigest("/mem/unknown.md#root")

        assert is_already_indexed(chunk_id, content_hash) is False

    def test_returns_false_when_vec_row_missing(self, _init_db):
        """Chunk row exists with matching hash but vec0 entry is gone -> False.

        This is the #251 failure mode: the chunk row landed but the vec0
        insert was silently swallowed.  The guard must return False so the
        caller knows to re-embed.
        """
        content = "vec-less content"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        chunk_id = stable_md5_hexdigest("/mem/test.md#root")

        _insert_chunk_without_vec(chunk_id, "/mem/test.md", content, content_hash)

        assert is_already_indexed(chunk_id, content_hash) is False


# ---------------------------------------------------------------------------
# Save/watcher race — the full seam test
# ---------------------------------------------------------------------------


class TestConcurrentSaveAndWatcherDedup:
    """Simulate the race: /save indexes a file, then the watcher fires.

    No real threading — we pre-populate the DB to represent the state
    after /save completes, then call ``index_file`` as the watcher would.
    The assertion is that no duplicate rows appear.
    """

    def _make_md_file(self, tmp_path, slug="dedup-race", body="Race dedup sentinel."):
        """Write a minimal markdown file and return its path."""
        subdir = tmp_path / "insights"
        subdir.mkdir(exist_ok=True)
        fp = subdir / f"{slug}.md"
        fp.write_text(
            f"---\n"
            f"id: insights-{slug}\n"
            f"category: insights\n"
            f"type: Insight\n"
            f"created_at: '2026-05-03T00:00:00+00:00'\n"
            f"---\n\n"
            f"{body}\n"
        )
        return str(fp)

    def test_watcher_skips_when_save_already_indexed(self, _init_db):
        """Pre-insert rows as if /save just landed, then call index_file.

        Expected: all chunks are ``chunks_unchanged``, none written or
        re-embedded, and the total row count stays the same.
        """
        tmp_path = _init_db
        file_path = self._make_md_file(tmp_path)

        # Phase 1: simulate /save completing successfully.
        with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
            outcome_save = index_file(file_path)

        assert outcome_save["embedded"] is True
        assert outcome_save["chunks_written"] >= 1

        rows_after_save = _count_chunk_rows(file_path)
        assert rows_after_save >= 1

        # Phase 2: watcher fires on the same file (same content on disk).
        with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR) as embed_mock:
            outcome_watcher = index_file(file_path)

        # The watcher path must short-circuit — no embed calls, no new rows.
        assert not embed_mock.called, "embedder was called — dedup guard failed"
        assert outcome_watcher["chunks_unchanged"] >= 1
        assert outcome_watcher["chunks_written"] == 0
        assert outcome_watcher["chunks_reembedded"] == 0

        rows_after_watcher = _count_chunk_rows(file_path)
        assert rows_after_watcher == rows_after_save, (
            f"duplicate rows: {rows_after_watcher} after watcher vs {rows_after_save} after save"
        )

    def test_watcher_re_embeds_if_save_left_broken_vec(self, _init_db):
        """If /save landed the chunk row but vec0 failed, the watcher must fix it.

        This is the complementary case — the guard correctly reports "not
        fully indexed" and the watcher path does the re-embed.
        """
        tmp_path = _init_db
        file_path = self._make_md_file(tmp_path, slug="broken-vec")

        # Phase 1: full index.
        with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
            index_file(file_path)

        # Break the vec entries.
        db = store.get_db()
        try:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM chunks WHERE file_path = ?", (file_path,))
            chunk_ids = [r["id"] for r in cursor.fetchall()]
            for cid in chunk_ids:
                cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (cid,))
            db.commit()
        finally:
            db.close()

        # Confirm vec entries are gone.
        for cid in chunk_ids:
            assert _count_vec_rows(cid) == 0

        # Phase 2: watcher fires — must re-embed.
        with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR) as embed_mock:
            outcome = index_file(file_path)

        assert embed_mock.called, "embedder not called — broken vec not detected"
        assert outcome["chunks_reembedded"] >= 1

        # Vec entries restored.
        for cid in chunk_ids:
            assert _count_vec_rows(cid) == 1
