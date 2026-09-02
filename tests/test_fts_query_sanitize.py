"""Regression: FTS5 syntax characters in a query must not cost hybrid its BM25 arm.

``sanitize_fts_query`` stripped quotes, boolean operators, and hyphens — but not
``?``, ``:``, ``(``, ``)``, ``*``, ``^``, ``.`` or the rest of the punctuation
FTS5's MATCH parser rejects (barewords admit only ``[A-Za-z0-9_]`` and
non-ASCII). A query like ``What breed is the user's dog?`` raised
``fts5: syntax error near "?"`` from ``search_fts``; ``search_hybrid`` caught
it, misdiagnosed it as index corruption, ran a full ``rebuild_fts()``, failed
again, and silently returned vector-only. Net effect: every question-shaped
query — the dominant caller shape — lost the keyword arm, plus a wasted index
rebuild per query. Keyword-only installs got the exception outright.

The old empty-query fallback ``'*'`` was itself a MATCH syntax error
("unknown special query"); the fallback is now ``'""'``, the empty phrase,
which is valid FTS5 that matches nothing.

Real SQLite + tmp_path, no DB mocking (per CLAUDE.md).
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.core.store import sanitize_fts_query
from tests._store_helpers import upsert_chunks


_FAKE_EMBEDDING = [0.01] * 1024


def _make_chunk(chunk_id: str, content: str) -> dict:
    return {
        "id": chunk_id,
        "file_path": f"insights/{chunk_id}.md",
        "section_id": "root",
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": "2026-08-31T00:00:00+00:00",
        "last_updated": "2026-08-31T00:00:00+00:00",
        "embedding": _FAKE_EMBEDDING,
    }


@pytest.fixture()
def store_db(tmp_path, monkeypatch):
    """Isolated, fully-initialised store in tmp_path (no git commits)."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    upsert_chunks(
        [_make_chunk("dog1", "the user's golden retriever is a friendly dog breed")],
        skip_unchanged=False,
    )
    return tmp_path


class TestSanitizeFtsQuery:
    """Unit: the sanitizer's output must always be a valid MATCH expression."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("What breed is the user's dog?", "What breed is the user s dog"),
            ("(dog breed)", "dog breed"),
            ("dog: breed", "dog breed"),
            ("dog* breed^2", "dog breed 2"),
            ("{dog breed}", "dog breed"),
            ("store.py", "store py"),
            ("bge-m3", "bge m3"),
            ('"quoted phrase"', "quoted phrase"),
            ("dog AND breed OR cat NOT fish", "dog breed cat fish"),
        ],
    )
    def test_syntax_characters_are_stripped(self, raw: str, expected: str):
        assert sanitize_fts_query(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "???", "()*^:{}", '"'])
    def test_degenerate_queries_fall_back_to_empty_phrase(self, raw: str):
        # '""' is the empty phrase — valid FTS5, matches nothing. The old
        # fallback '*' raised "unknown special query" when it reached MATCH.
        assert sanitize_fts_query(raw) == '""'

    def test_non_ascii_word_characters_survive(self):
        # FTS5 barewords include non-ASCII; the sanitizer must not eat them.
        assert sanitize_fts_query("café Zürich?") == "café Zürich"


class TestSearchFtsQuestionShapedQueries:
    """Integration: question-shaped queries return rows instead of raising."""

    def test_trailing_question_mark_returns_rows(self, store_db):
        results = store.search_fts("user dog breed?")
        assert len(results) == 1
        assert "golden retriever" in results[0]["content"]

    def test_parenthesised_query_returns_rows(self, store_db):
        results = store.search_fts("(user dog breed)")
        assert len(results) == 1

    def test_apostrophe_and_punctuation_mix_returns_rows(self, store_db):
        results = store.search_fts("the user's dog breed, friendly?")
        assert len(results) == 1

    def test_punctuation_only_query_matches_nothing_without_raising(self, store_db):
        assert store.search_fts("???") == []


class TestHybridKeepsBothArms:
    """Integration: hybrid search must not rebuild or drop the BM25 arm."""

    def test_question_query_keeps_bm25_arm_without_rebuild(self, store_db):
        # A syntax error used to be misread as corruption: full rebuild_fts(),
        # second failure, silent vector-only. With the sanitizer fixed, the
        # rebuild path must not fire and the FTS arm must contribute.
        real_search_fts = store.search_fts
        with patch.object(store, "rebuild_fts") as mock_rebuild, \
                patch.object(store, "search_fts", side_effect=real_search_fts) as mock_fts:
            merged = store.search_hybrid(
                "user dog breed?", _FAKE_EMBEDDING, top_k=5, threshold=0.0,
            )
        mock_rebuild.assert_not_called()
        assert mock_fts.call_count == 1
        assert any("golden retriever" in r["content"] for r in merged)

    def test_dropped_bm25_arm_is_logged_not_silent(self, store_db, caplog):
        # If FTS fails even after the rebuild retry, the degradation to
        # vector-only must be visible in the logs — silence is how the
        # sanitizer gap went unnoticed.
        with patch.object(store, "search_fts", side_effect=Exception("boom")), \
                patch.object(store, "rebuild_fts"), \
                caplog.at_level(logging.WARNING, logger="palinode.store"):
            merged = store.search_hybrid(
                "user dog breed?", _FAKE_EMBEDDING, top_k=5, threshold=0.0,
            )
        assert any("vector-only" in rec.message for rec in caplog.records)
        # Vector arm still serves results.
        assert any("golden retriever" in r["content"] for r in merged)
