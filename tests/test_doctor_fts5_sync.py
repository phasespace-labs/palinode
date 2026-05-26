"""
Tests for the fts5_sync doctor check (#316).

Verifies that row-count drift between ``chunks`` and ``chunks_fts`` is
detected and reported correctly.

All DB work uses real SQLite with tmp_path — no mocking per project standard.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from palinode.core.config import Config
from palinode.diagnostics.runner import run_one
from palinode.diagnostics.types import DoctorContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(db_path: Path) -> DoctorContext:
    cfg = Config(
        memory_dir=str(db_path.parent),
        db_path=str(db_path),
    )
    return DoctorContext(config=cfg)


def _make_schema(db_path: Path) -> sqlite3.Connection:
    """Create the minimal schema needed for the fts5_sync check.

    Returns the open connection so callers can manipulate data before closing.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            section_id TEXT,
            category TEXT,
            content TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content,
            file_path,
            category,
            content=chunks,
            content_rowid=rowid,
            tokenize='unicode61'
        )
    """)
    con.commit()
    return con


def _insert_chunk(con: sqlite3.Connection, chunk_id: str, content: str) -> None:
    """Insert a chunk row and its FTS5 entry (simulating a normal upsert).

    External-content FTS5 tables must be populated manually.  We use the
    same INSERT INTO ... SELECT rowid pattern that store.upsert_chunks uses,
    but without a preceding DELETE (no prior row exists on fresh inserts).
    """
    con.execute(
        "INSERT OR REPLACE INTO chunks (id, file_path, section_id, category, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (chunk_id, f"test/{chunk_id}.md", "root", "test", content),
    )
    # Populate the external-content FTS5 table.
    # Using INSERT OR IGNORE so re-insertions don't fail on rowid collision.
    con.execute(
        "INSERT OR IGNORE INTO chunks_fts(rowid, content, file_path, category) "
        "SELECT rowid, content, file_path, category FROM chunks WHERE id = ?",
        (chunk_id,),
    )
    con.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFts5Sync:
    def test_db_missing_passes(self, tmp_path: Path) -> None:
        """When the DB file doesn't exist, the check passes (skip, not error)."""
        db = tmp_path / "nonexistent.db"
        ctx = _ctx(db)
        result = run_one(ctx, "fts5_sync")

        assert result.passed is True
        assert result.name == "fts5_sync"
        assert "skipping" in result.message.lower()

    def test_empty_db_passes(self, tmp_path: Path) -> None:
        """An empty but initialised DB passes — nothing to be out of sync."""
        db = tmp_path / ".palinode.db"
        con = _make_schema(db)
        con.close()

        ctx = _ctx(db)
        result = run_one(ctx, "fts5_sync")

        assert result.passed is True
        assert "empty" in result.message.lower()

    def test_synced_db_passes(self, tmp_path: Path) -> None:
        """DB with chunks and a matching FTS5 row count passes."""
        db = tmp_path / ".palinode.db"
        con = _make_schema(db)
        _insert_chunk(con, "chunk-1", "The quick brown fox")
        _insert_chunk(con, "chunk-2", "Jumps over the lazy dog")
        con.close()

        ctx = _ctx(db)
        result = run_one(ctx, "fts5_sync")

        assert result.passed is True
        assert "2" in result.message  # 2 rows both sides
        assert "in sync" in result.message.lower()

    def test_fts5_missing_rows_fails(self, tmp_path: Path) -> None:
        """FTS5 index missing one entry triggers FAIL.

        We delete from ``chunks_fts_docsize`` directly — the FTS5 shadow table
        that tracks actual indexed documents — because the external-content
        virtual table's COUNT(*) proxies back through the source (chunks) table
        and would always match.
        """
        db = tmp_path / ".palinode.db"
        con = _make_schema(db)
        _insert_chunk(con, "chunk-a", "Memory alpha content")
        _insert_chunk(con, "chunk-b", "Memory beta content")
        _insert_chunk(con, "chunk-c", "Memory gamma content")

        # Corrupt: remove one entry from the FTS docsize shadow table.
        # This simulates the mid-write exception path where upsert_chunks
        # committed to chunks but the FTS5 sync raised before completing.
        con.execute(
            "DELETE FROM chunks_fts_docsize WHERE id = "
            "(SELECT rowid FROM chunks WHERE id = 'chunk-b')"
        )
        con.commit()
        con.close()

        ctx = _ctx(db)
        result = run_one(ctx, "fts5_sync")

        assert result.passed is False
        assert result.severity == "warn"
        assert "out of sync" in result.message.lower()
        assert "3" in result.message  # chunks count
        assert "2" in result.message  # fts count
        assert result.remediation is not None
        assert "palinode reindex" in result.remediation

    def test_fts5_table_missing_fails(self, tmp_path: Path) -> None:
        """A DB where chunks_fts was never created fails with an absent-table message."""
        db = tmp_path / ".palinode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        # Only create chunks, not chunks_fts — simulates a very old schema.
        con.execute(
            "CREATE TABLE chunks (id TEXT PRIMARY KEY, file_path TEXT, "
            "section_id TEXT, category TEXT, content TEXT)"
        )
        con.execute("INSERT INTO chunks VALUES ('x', 'f.md', 'root', 'test', 'hi')")
        con.commit()
        con.close()

        ctx = _ctx(db)
        result = run_one(ctx, "fts5_sync")

        assert result.passed is False
        assert "does not exist" in result.message.lower()
        assert result.remediation is not None
        assert "palinode reindex" in result.remediation

    def test_drift_message_includes_counts(self, tmp_path: Path) -> None:
        """Failure message must include both row counts and drift magnitude.

        We insert chunks without FTS entries (skipping the FTS insert step)
        to simulate a partial-reindex where chunks exist but were never indexed
        into FTS5.
        """
        db = tmp_path / ".palinode.db"
        con = _make_schema(db)

        # Insert 2 chunks normally (FTS populated).
        _insert_chunk(con, "chunk-0", "Content zero")
        _insert_chunk(con, "chunk-1", "Content one")

        # Insert 3 more chunks WITHOUT FTS entries — simulates partial indexing.
        for i in range(2, 5):
            con.execute(
                "INSERT OR REPLACE INTO chunks (id, file_path, section_id, category, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"chunk-{i}", f"test/chunk-{i}.md", "root", "test", f"Content {i}"),
            )
        con.commit()
        con.close()

        ctx = _ctx(db)
        result = run_one(ctx, "fts5_sync")

        assert result.passed is False
        assert "5" in result.message   # chunks count (total)
        assert "2" in result.message   # fts count (only 2 indexed)
        assert result.remediation is not None
        assert "palinode reindex" in result.remediation
