"""Regression: chunks_fts stays in lockstep with chunks across delete/supersede (#439).

``chunks_fts`` is an external-content FTS5 table (``content=chunks``). A bare
``DELETE FROM chunks_fts WHERE rowid=?`` does NOT remove the inverted-index
tokens of such a table — FTS5 must re-derive them from the row's column values —
so the tokens are orphaned (``count(chunks_fts) > count(chunks)``) and BM25
keyword recall is silently wrong until the next ``rebuild_fts()``. The delete
paths must use the sanctioned FTS5 ``'delete'`` command instead.

These tests exercise the production delete paths (``delete_file_chunks`` and the
``index_file`` re-index prune) and assert ``count(chunks) == count(chunks_fts)``
with the deleted content gone from ``search_fts`` — and deliberately NEVER call
``rebuild_fts()``, which would mask the drift by dropping + recreating the table
from ``chunks``.

Real SQLite + tmp_path, no DB mocking (per CLAUDE.md).
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.indexer import index_file as index_file_mod
from palinode.indexer.index_file import index_file


_FAKE_EMBEDDING = [0.01] * 1024
_FAKE_VECTOR = [0.02] * 1024


def _make_chunk(
    chunk_id: str, file_path: str, content: str, section_id: str = "root"
) -> dict:
    return {
        "id": chunk_id,
        "file_path": file_path,
        "section_id": section_id,
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": "2026-07-02T00:00:00+00:00",
        "last_updated": "2026-07-02T00:00:00+00:00",
        "embedding": _FAKE_EMBEDDING,
    }


def _counts() -> tuple[int, int]:
    """Return ``(count(chunks), count(chunks_fts))`` from the live store."""
    db = store.get_db()
    try:
        chunks = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        fts = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        return chunks, fts
    finally:
        db.close()


@pytest.fixture()
def store_db(tmp_path, monkeypatch):
    """Isolated, fully-initialised store in tmp_path (no git commits)."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


class TestDeleteFileChunksLockstep:
    """``store.delete_file_chunks`` must not orphan chunks_fts rows."""

    def test_delete_keeps_fts_in_lockstep(self, store_db):
        f_del = str(store_db / "insights" / "doomed.md")
        f_keep = str(store_db / "insights" / "kept.md")
        store.upsert_chunks(
            [
                _make_chunk("d1", f_del, "zebradelete alpha content", "s1"),
                _make_chunk("d2", f_del, "zebradelete beta content", "s2"),
                _make_chunk("k1", f_keep, "keptgiraffe gamma content", "s1"),
            ],
            skip_unchanged=False,
        )
        assert _counts() == (3, 3)

        store.delete_file_chunks(f_del)

        chunks, fts = _counts()
        assert chunks == 1, "both chunks of the deleted file must be gone (k1 remains)"
        assert fts == chunks, (
            f"chunks_fts orphaned rows after delete: "
            f"count(chunks)={chunks} != count(chunks_fts)={fts} (#439)"
        )
        # Keyword recall, with NO rebuild_fts(): deleted token gone, kept present.
        assert store.search_fts("zebradelete") == []
        kept = store.search_fts("keptgiraffe")
        assert kept and all("zebradelete" not in r["content"] for r in kept)


class TestReindexPruneLockstep:
    """The ``index_file`` re-index prune must not orphan chunks_fts rows."""

    def _write(self, path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: doomed\ncategory: insights\n---\n\n" + body,
            encoding="utf-8",
        )

    def test_reindex_with_section_removed_keeps_fts_in_lockstep(self, store_db):
        md = store_db / "insights" / "doomed.md"
        # Sections must be long: parse_markdown collapses a <2000-char body into
        # a single "root" chunk, so pad each section past that to force a split.
        pad = ("lorem ipsum dolor sit amet consectetur adipiscing elit " * 30)
        sec_one = f"## Section One\n\nzebrasection unique keyword one. {pad}\n"
        sec_two = f"## Section Two\n\ngiraffesection unique keyword two. {pad}\n"

        # v1: two sections, each with a rare keyword.
        self._write(md, f"# Doomed Doc\n\n{sec_one}\n{sec_two}")
        with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
            index_file(str(md))

        before, before_fts = _counts()
        assert before == before_fts and before >= 2
        assert store.search_fts("giraffesection")  # section two indexed

        # v2: Section Two removed → its chunk must be pruned from BOTH tables.
        self._write(md, f"# Doomed Doc\n\n{sec_one}")
        with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
            index_file(str(md))

        after, after_fts = _counts()
        assert after < before, "the removed section's chunk must be pruned"
        assert after_fts == after, (
            f"chunks_fts orphaned rows after re-index prune: "
            f"count(chunks)={after} != count(chunks_fts)={after_fts} (#439)"
        )
        # No rebuild_fts(): the pruned keyword is gone, the kept one remains.
        assert store.search_fts("giraffesection") == []
        assert store.search_fts("zebrasection")


class TestNoBareFtsDeleteIdiom:
    """Build-independent guard: whether a bare ``DELETE FROM chunks_fts`` orphans
    tokens depends on the sqlite build (it did on the ``.85`` prod host; not on
    every dev sqlite), so the runtime tests above can't catch a reintroduction
    everywhere. This one can: the sanctioned FTS5 ``'delete'`` command must stay
    the ONLY way chunks_fts rows are removed (#439).
    """

    def test_delete_paths_use_sanctioned_fts5_delete(self):
        for mod in (store, index_file_mod):
            src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
            assert "DELETE FROM chunks_fts" not in src, (
                f"{mod.__name__} reintroduced a bare `DELETE FROM chunks_fts` — "
                "use the sanctioned FTS5 'delete' command (store.fts5_delete_chunk) "
                "so external-content tokens aren't orphaned (#439)."
            )

    def test_sanctioned_delete_command_is_present(self):
        src = Path(inspect.getfile(store)).read_text(encoding="utf-8")
        assert "INSERT INTO chunks_fts(chunks_fts, rowid" in src and "'delete'" in src, (
            "store.fts5_delete_chunk must issue the FTS5 external-content "
            "'delete' command (#439)."
        )
