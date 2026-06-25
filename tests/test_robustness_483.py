"""Robustness tests for #483.

Two items:
1. Replace-guard fail-open emits WARNING when frontmatter is unreadable.
2. search() / search_fts() connections are closed on all paths (try/finally).
"""
from __future__ import annotations

import logging
import math
import os

import pytest

from palinode.consolidation.executor import _is_replace_policy, apply_operations
from palinode.core import store
from palinode.core.config import config


# ── Helpers ──────────────────────────────────────────────────────────────────

EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _embedding(seed: int) -> list[float]:
    vec = [0.0] * EMBED_DIM
    vec[seed % EMBED_DIM] = 0.9
    vec[(seed * 7 + 3) % EMBED_DIM] = 0.4
    return _normalize(vec)


def _index_chunk(*, chunk_id: str, file_path: str, content: str, seed: int) -> None:
    import datetime as _dt
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    store.upsert_chunks([{
        "id": chunk_id,
        "file_path": file_path,
        "section_id": None,
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": now_iso,
        "last_updated": now_iso,
        "embedding": _embedding(seed),
    }])


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point config at a fresh tmp dir + real SQLite DB for the store tests."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    store._db_checked = False
    store.init_db()
    yield


# ── Item 1: replace-guard fail-open WARNING ───────────────────────────────────

def test_corrupt_frontmatter_emits_warning(caplog):
    """When raw text contains 'update_policy: replace' but the parser returns
    metadata without that field (indicating silent corruption), a WARNING is
    emitted. The guard still falls open to False — consolidation is never blocked.

    The python-frontmatter library swallows YAML parse errors and returns {}
    metadata; we detect the mismatch between raw text and parsed metadata.
    """
    # Frontmatter is gone (e.g. stripped by a bad merge), but the keyword
    # still appears in the body — a realistic corruption scenario.
    corrupted = "update_policy: replace\n\n- fact text <!-- fact:f1 -->\n"
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.executor"):
        result = _is_replace_policy(corrupted)

    assert result is False, "fail-open: guard must not block on corrupt doc"
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_messages, "expected at least one WARNING"
    assert any("unprotected" in m for m in warning_messages), (
        "warning should mention 'may be unprotected' when keyword present but metadata absent"
    )


def test_corrupt_frontmatter_keyword_in_body_emits_warning(caplog):
    """Variant: valid frontmatter exists but 'update_policy: replace' appears
    only in the body (e.g. documentation text). The post-parse check triggers
    a warning since raw-text check cannot distinguish body from frontmatter."""
    # This scenario is an acceptable false-positive: better to warn than miss.
    content = "---\nid: doc\n---\n\nSee update_policy: replace for details.\n"
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.executor"):
        result = _is_replace_policy(content)

    # Returns False (the frontmatter has no update_policy: replace) and warns.
    assert result is False
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_messages, "expected WARNING for keyword-in-body case"


def test_corrupt_frontmatter_still_does_not_block_consolidation(tmp_path):
    """End-to-end: a file with corrupted frontmatter (keyword in raw text but
    no parseable frontmatter) still allows SUPERSEDE — fail-open is preserved.
    The warning fires but consolidation proceeds.
    """
    # Frontmatter stripped, keyword only in body — realistic corruption.
    doc_path = str(tmp_path / "corrupted.md")
    content = "update_policy: replace\n\n- a fact <!-- fact:f1 -->\n"
    with open(doc_path, "w") as f:
        f.write(content)

    ops = [{"op": "SUPERSEDE", "id": "f1", "new_text": "updated fact", "reason": "test"}]
    stats = apply_operations(doc_path, ops)

    # Fail-open → SUPERSEDE is allowed (no protection applied due to corrupt frontmatter).
    assert stats["protected_rejected"] == 0
    assert stats["superseded"] == 1


def test_clean_replace_doc_still_guards(tmp_path):
    """Regression: a well-formed replace doc is still guarded after the WARNING
    path is added — the WARNING path must not affect the non-exception branch."""
    doc_path = str(tmp_path / "living.md")
    content = (
        "---\nid: living-doc\nupdate_policy: replace\n---\n\n"
        "- current state <!-- fact:f1 -->\n"
    )
    with open(doc_path, "w") as f:
        f.write(content)

    ops = [{"op": "SUPERSEDE", "id": "f1", "new_text": "new state", "reason": "test"}]
    stats = apply_operations(doc_path, ops)

    assert stats["protected_rejected"] == 1
    assert stats["superseded"] == 0


# ── Item 2: search() connection hygiene ──────────────────────────────────────

def test_search_returns_correct_results_after_try_finally(tmp_path):
    """search() behaviour is unchanged after the try/finally refactor."""
    _index_chunk(chunk_id="c1", file_path="notes/a.md", content="hello world memory", seed=1)

    results = store.search(query_embedding=_embedding(1), threshold=0.0, top_k=5)
    assert len(results) >= 1
    assert results[0]["id"] == "c1"
    assert "score" in results[0]


def test_search_empty_returns_empty(tmp_path):
    """search() with no indexed chunks returns an empty list (regression guard)."""
    results = store.search(query_embedding=_embedding(42), threshold=0.0)
    assert results == []


def test_search_fts_returns_correct_results_after_try_finally(tmp_path):
    """search_fts() behaviour is unchanged after the try/finally refactor."""
    _index_chunk(chunk_id="c2", file_path="notes/b.md", content="palinode memory system", seed=2)

    results = store.search_fts("palinode", top_k=5)
    assert any(r["id"] == "c2" for r in results), "expected c2 in FTS results"


def test_search_fts_empty_returns_empty():
    """search_fts() with no indexed chunks returns an empty list."""
    results = store.search_fts("anything")
    assert results == []
