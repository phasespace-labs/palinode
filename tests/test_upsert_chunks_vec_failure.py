"""#385 regression: upsert_chunks must log and surface vec0 write failures.

Previously, both the pre-INSERT DELETE and the INSERT-after-DELETE pair on
chunks_vec were wrapped in bare `except Exception: pass`.  A vec0 structural
failure (corrupt table, missing virtual table extension, etc.) silently
produced a chunk row in `chunks` with no corresponding `chunks_vec` row —
the chunk was FTS5-searchable but invisible to vector search.

The fix:
  1. Log the final vec write failure at error level with exc_info.
  2. Return ``{"written": int, "vec_ok": bool, "fts_ok": bool}`` instead of
     ``int`` so callers can detect per-index health.
  3. FTS5 sync failures are logged at warning (recoverable) instead of pass.

Tests use real SQLite with tmp_path (no mocking the DB per CLAUDE.md).
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from palinode.core import store
from palinode.core.config import config


_FAKE_EMBEDDING = [0.01] * 1024


def _make_chunk(chunk_id: str, file_path: str) -> dict:
    return {
        "id": chunk_id,
        "file_path": file_path,
        "section_id": "root",
        "category": "insights",
        "content": "Test content for upsert_chunks vec failure test.",
        "metadata": {},
        "created_at": "2026-05-24T00:00:00+00:00",
        "last_updated": "2026-05-24T00:00:00+00:00",
        "embedding": _FAKE_EMBEDDING,
    }


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Isolated DB in tmp_path with store fully initialised."""
    db = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db))
    store.init_db()
    return db


# ---------------------------------------------------------------------------
# Return contract
# ---------------------------------------------------------------------------


class TestUpsertChunksReturnContract:

    def test_successful_upsert_returns_dict_with_true_flags(self, db_path):
        """Happy path: both flags True, written count matches input."""
        chunk = _make_chunk("test-ok-1", str(db_path.parent / "insights/test.md"))
        result = store.upsert_chunks([chunk], skip_unchanged=False)

        assert isinstance(result, dict), "upsert_chunks must return a dict (#385)"
        assert result["written"] == 1
        assert result["vec_ok"] is True
        assert result["fts_ok"] is True

    def test_empty_input_returns_zero_written(self, db_path):
        result = store.upsert_chunks([], skip_unchanged=False)
        assert result["written"] == 0
        assert result["vec_ok"] is True
        assert result["fts_ok"] is True


# ---------------------------------------------------------------------------
# Vec failure: drop chunks_vec to simulate structural failure
# ---------------------------------------------------------------------------


class TestUpsertChunksVecFailure:

    def test_vec_failure_sets_vec_ok_false_and_logs_error(
        self, db_path, caplog
    ):
        """Dropping chunks_vec simulates a structural vec0 write failure.

        Expected: vec_ok=False in return, error-level log, chunk still in
        `chunks` table (write did not abort mid-transaction).
        """
        # Must use store.get_db() to load the vec0 extension before DROP;
        # plain sqlite3.connect() raises "no such module: vec0" on DROP.
        conn = store.get_db()
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        conn.commit()
        conn.close()

        file_path = str(db_path.parent / "insights/vec-fail.md")
        chunk = _make_chunk("test-vec-fail-1", file_path)

        with caplog.at_level(logging.ERROR, logger="palinode.store"):
            result = store.upsert_chunks([chunk], skip_unchanged=False)

        # Return contract
        assert result["vec_ok"] is False, (
            "vec_ok must be False when chunks_vec write fails (#385)"
        )

        # Error must be logged — operator needs a signal
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, (
            "upsert_chunks must log at ERROR when chunks_vec write fails (#385)"
        )
        combined = " ".join(r.getMessage() for r in error_records)
        assert "chunks_vec" in combined or "vector index" in combined, (
            f"Error log must mention chunks_vec or vector index. Got: {combined!r}"
        )

        # The chunk row itself must still be in `chunks` (write is not aborted)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT id FROM chunks WHERE id = ?", (chunk["id"],)
        ).fetchone()
        conn.close()
        assert row is not None, "chunks row must be written even when vec write fails"


# ---------------------------------------------------------------------------
# FTS failure: drop chunks_fts to simulate FTS5 sync failure
# ---------------------------------------------------------------------------


class TestUpsertChunksFTSFailure:

    def test_fts_failure_sets_fts_ok_false_and_logs_warning(
        self, db_path, caplog
    ):
        """Dropping chunks_fts simulates an FTS5 sync failure.

        Expected: fts_ok=False in return, warning-level log.  vec_ok must
        remain True (the failure is isolated to FTS5).
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
        conn.commit()
        conn.close()

        file_path = str(db_path.parent / "insights/fts-fail.md")
        chunk = _make_chunk("test-fts-fail-1", file_path)

        with caplog.at_level(logging.WARNING, logger="palinode.store"):
            result = store.upsert_chunks([chunk], skip_unchanged=False)

        assert result["fts_ok"] is False, (
            "fts_ok must be False when FTS5 sync fails (#385)"
        )
        assert result["vec_ok"] is True, (
            "vec_ok must not be affected by an isolated FTS5 failure"
        )

        warning_records = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warning_records, (
            "upsert_chunks must log at WARNING when FTS5 sync fails (#385)"
        )
        combined = " ".join(r.getMessage() for r in warning_records)
        assert "fts" in combined.lower() or "fts5" in combined.lower(), (
            f"Warning log must mention FTS/FTS5. Got: {combined!r}"
        )
