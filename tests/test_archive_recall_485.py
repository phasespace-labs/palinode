"""Regression tests for #485 — ARCHIVE must suppress facts from default recall.

PROGRAM.md's contract: obsolete/wrong facts are ARCHIVE'd ("move to
``status: archived``, never hard-delete"). The deterministic executor relocates
an ARCHIVE'd (or SUPERSEDE'd old-version) fact into a ``{base}-history.md``
sibling. Before #485 that history file carried no ``status`` frontmatter, so its
chunks indexed as ``active`` and the very facts ARCHIVE exists to suppress kept
surfacing in default recall.

The fix: history files carry ``status: archived`` frontmatter, which the indexer
propagates to every chunk's metadata, so ``config.search.exclude_status`` filters
them from default recall while leaving them indexed and retrievable on demand.
"""

import os
from unittest import mock

import pytest

from palinode.consolidation.executor import (
    apply_operations,
    _ensure_archived_frontmatter,
)


EMBED_DIM = 1024


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    """Deterministic fake embedder so tests don't need Ollama.

    All vectors are identical, so cosine similarity is 1.0 for every chunk —
    recall ranking is irrelevant here; we only assert presence/absence under
    the status filter.
    """
    return [0.1] * EMBED_DIM


# ---------------------------------------------------------------------------
# Unit: legacy history-file frontmatter injection
# ---------------------------------------------------------------------------


def test_ensure_archived_frontmatter_injects_into_legacy_file():
    """A pre-#485 history file (no status) gains ``status: archived``."""
    legacy = "---\ncategory: history\ncore: false\n---\n\n# History\n\n- old entry\n"
    fixed = _ensure_archived_frontmatter(legacy)
    assert "status: archived" in fixed
    # Body and existing fields are preserved.
    assert "category: history" in fixed
    assert "# History" in fixed
    assert "- old entry" in fixed


def test_ensure_archived_frontmatter_respects_existing_status():
    """An explicit status is not clobbered."""
    content = "---\ncategory: history\nstatus: superseded\n---\n\nbody\n"
    assert _ensure_archived_frontmatter(content) == content


def test_ensure_archived_frontmatter_prepends_when_no_frontmatter():
    """A frontmatter-less file gets a complete archived block."""
    fixed = _ensure_archived_frontmatter("# History\n\n- bare\n")
    assert fixed.startswith("---\n")
    assert "status: archived" in fixed
    assert "- bare" in fixed


# ---------------------------------------------------------------------------
# Unit: ARCHIVE / SUPERSEDE create archived-status history files
# ---------------------------------------------------------------------------


@pytest.fixture()
def memory_file(tmp_path):
    path = os.path.join(str(tmp_path), "foo-status.md")
    with open(path, "w") as f:
        f.write(
            "---\nid: foo\ncategory: project\n---\n\n"
            "# Foo\n\n"
            "- [2024-01-01] The project started <!-- fact:f1 -->\n"
            "- [2024-01-02] An obsolete claim that is wrong <!-- fact:f2 -->\n"
        )
    return path


def test_archive_history_file_is_status_archived(memory_file):
    apply_operations(memory_file, [{"op": "ARCHIVE", "id": "f2", "reason": "wrong"}])
    history_file = memory_file.replace("-status.md", "-history.md")
    assert os.path.exists(history_file)
    hist = open(history_file).read()
    assert "status: archived" in hist
    assert "An obsolete claim that is wrong" in hist


def test_supersede_history_file_is_status_archived(memory_file):
    apply_operations(
        memory_file,
        [{"op": "SUPERSEDE", "id": "f1", "new_text": "- [2024-02-01] Restarted", "reason": "replan"}],
    )
    history_file = memory_file.replace("-status.md", "-history.md")
    assert os.path.exists(history_file)
    assert "status: archived" in open(history_file).read()


# ---------------------------------------------------------------------------
# End-to-end: archived fact is excluded from default recall, retrievable on demand
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Fresh tmp memory dir backed by real SQLite, with a fake embedder."""
    from palinode.core.config import config
    from palinode.core import store

    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", os.path.join(memory_dir, ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    os.makedirs(os.path.join(memory_dir, "projects"), exist_ok=True)

    store.init_db()
    with mock.patch("palinode.core.embedder.embed", side_effect=_fake_embed):
        yield memory_dir


def test_archived_fact_excluded_from_default_recall_but_retrievable(isolated_store):
    from palinode.indexer.index_file import index_file
    from palinode.core import store

    obsolete = "An obsolete claim that is wrong"
    src = os.path.join(isolated_store, "projects", "foo-status.md")
    with open(src, "w") as f:
        f.write(
            "---\nid: foo\ncategory: project\n---\n\n"
            "# Foo\n\n"
            "- [2024-01-01] The project started <!-- fact:f1 -->\n"
            f"- [2024-01-02] {obsolete} <!-- fact:f2 -->\n"
        )

    # ARCHIVE relocates f2 into foo-history.md (status: archived).
    apply_operations(src, [{"op": "ARCHIVE", "id": "f2", "reason": "wrong"}])
    history = src.replace("-status.md", "-history.md")
    assert os.path.exists(history)

    # Index both the (now f2-free) source and the history file.
    index_file(src)
    index_file(history)

    query = [0.1] * EMBED_DIM

    # Default recall: archived content must NOT surface.
    default_hits = store.search(query, threshold=0.0, top_k=50, record_access=False)
    assert not any(obsolete in r["content"] for r in default_hits), (
        "ARCHIVE'd fact leaked into default recall — exclude_status not applied"
    )

    # Explicit include (status_exclude_list=[]): the audit trail is retrievable.
    all_hits = store.search(
        query, threshold=0.0, top_k=50, status_exclude_list=[], record_access=False
    )
    assert any(obsolete in r["content"] for r in all_hits), (
        "ARCHIVE'd fact not retrievable even with exclude_status disabled — "
        "history was hard-deleted, violating 'never hard-delete'"
    )
