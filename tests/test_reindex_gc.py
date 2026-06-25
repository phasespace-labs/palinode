"""Regression tests for reindex garbage-collecting removed files (#308)."""
from __future__ import annotations

from pathlib import Path

import pytest

from palinode.core import store
from palinode.core.config import config


@pytest.fixture(autouse=True)
def _reset_db_checked():
    store._db_checked = False
    yield
    store._db_checked = False


def _chunk(chunk_id: str, file_path: str, content: str) -> dict:
    return {
        "id": chunk_id,
        "file_path": file_path,
        "section_id": "root",
        "category": "test",
        "content": content,
        "metadata": {},
        "embedding": [0.0] * config.embeddings.primary.dimensions,
    }


def _count(table: str, where: str = "", params: tuple = ()) -> int:
    db = store.get_db()
    try:
        sql = f"SELECT COUNT(*) FROM {table} {where}"  # nosec B608 - test helper only
        return db.execute(sql, params).fetchone()[0]
    finally:
        db.close()


def test_gc_orphaned_chunks_deletes_removed_file_index_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GC removes stale chunks, FTS rows, vec rows, and entity rows only."""
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    store.init_db()

    existing_file = tmp_path / "existing.md"
    orphan_file = tmp_path / "removed.md"
    existing_file.write_text("# Existing\nkept content\n")

    existing_path = str(existing_file)
    orphan_path = str(orphan_file)
    store.upsert_chunks(
        [
            _chunk("existing-1", existing_path, "kept alpha"),
            _chunk("orphan-1", orphan_path, "removed alpha"),
            _chunk("orphan-2", orphan_path, "removed beta"),
        ],
        skip_unchanged=False,
    )

    db = store.get_db()
    try:
        db.executemany(
            "INSERT INTO entities (entity_ref, file_path, category, last_seen) VALUES (?, ?, ?, ?)",
            [
                ("entity/kept", existing_path, "test", "2026-06-12T00:00:00Z"),
                ("entity/removed", orphan_path, "test", "2026-06-12T00:00:00Z"),
            ],
        )
        db.commit()
    finally:
        db.close()

    paths_removed, chunks_removed = store.gc_orphaned_chunks({existing_path})

    assert (paths_removed, chunks_removed) == (1, 2)
    assert _count("chunks", "WHERE file_path = ?", (orphan_path,)) == 0
    assert _count("chunks_fts", "WHERE file_path = ?", (orphan_path,)) == 0
    assert _count("chunks_vec", "WHERE id IN (?, ?)", ("orphan-1", "orphan-2")) == 0
    assert _count("entities", "WHERE file_path = ?", (orphan_path,)) == 0

    assert _count("chunks", "WHERE file_path = ?", (existing_path,)) == 1
    assert _count("chunks_fts", "WHERE file_path = ?", (existing_path,)) == 1
    assert _count("chunks_vec", "WHERE id = ?", ("existing-1",)) == 1
    assert _count("entities", "WHERE file_path = ?", (existing_path,)) == 1
    assert _count("chunks_fts") == _count("chunks")
