"""The generated ``## See also`` footer never reaches the index.

Real markdown under ``tmp_path``, the real parser, the real reconcile seam,
real SQLite-vec + FTS5. The embedder is the only double, and it *records* its
inputs: a fake vector cannot tell footer text from body text, so the proof the
vector arm never sees the footer is the list of strings it was handed.

What the relevance baseline measured (``bench/results/
relevance-abstention-baseline-2026-09-20``): a chunk that was nothing but the
footer was the rank-1 keyword hit for three questions, two of them questions
the store cannot answer at all.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from palinode.core import parser, projection, store
from palinode.core.config import Config, config
from palinode.core.embedding_preprocess import AUTO_FOOTER_MARKER
from palinode.core.hashing import stable_md5_hexdigest
from palinode.diagnostics.runner import run_one
from palinode.diagnostics.types import DoctorContext
from palinode.indexer import reconcile
from palinode.indexer.index_file import index_file

_VEC = [0.02] * 1024

# Slugs that appear nowhere in any body, so a keyword hit on one of them can
# only have come through the footer.
FOOTER = (
    "## See also\n"
    f"{AUTO_FOOTER_MARKER}\n"
    "- [[quillon-marsh]]\n"
    "- [[zephyr-ledger]]\n"
)

FRONTMATTER = (
    "---\n"
    "id: harbor-release\n"
    "category: projects\n"
    "status: active\n"
    "entities:\n"
    "- person/quillon-marsh\n"
    "- project/zephyr-ledger\n"
    "---\n\n"
)

_FILLER = (
    "The ferrocene pipeline drains the staging queue before the batch window "
    "opens, and the operator sees the drain count in the run log.\n\n"
)


def _long_body() -> str:
    """A body over the parser's 2000-char single-chunk threshold."""
    return (
        "# Harbor release\n\n"
        "## Overview\n\n"
        "The batch window is fifteen minutes.\n\n"
        + _FILLER * 16
        + "## Rollback\n\n"
        "Roll back with the ferrocene snapshot taken before the window.\n\n"
    )


LONG_DOC = FRONTMATTER + _long_body() + FOOTER
SHORT_DOC = (
    FRONTMATTER
    + "# Harbor release\n\nThe batch window is fifteen minutes.\n\n"
    + FOOTER
)


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


def _write(tmp_store, content: str, name: str = "harbor-release.md") -> str:
    path = tmp_store / "projects" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _index(path: str, seen: list[str] | None = None):
    """Index through the real seam, recording what the embedder was asked for."""
    def _embed(text: str, backend: str = "local") -> list[float]:
        if seen is not None:
            seen.append(text)
        return _VEC

    with patch("palinode.core.embedder.embed", side_effect=_embed):
        return index_file(path)


def _rows(path: str) -> list[tuple[str, str, str, int | None]]:
    db = store.get_db()
    try:
        return [
            (r["id"], r["section_id"], r["content"], r["projection_version"])
            for r in db.execute(
                "SELECT id, section_id, content, projection_version "
                "FROM chunks WHERE file_path = ? ORDER BY section_id",
                (path,),
            ).fetchall()
        ]
    finally:
        db.close()


def _vec_ids() -> set[str]:
    db = store.get_db()
    try:
        return {r["id"] for r in db.execute("SELECT id FROM chunks_vec").fetchall()}
    finally:
        db.close()


# ── the footer-only chunk ─────────────────────────────────────────────────────


def test_a_footer_only_section_gets_no_chunk_at_all(tmp_store):
    path = _write(tmp_store, LONG_DOC)

    # Precondition: the parser still sections the footer off on its own. The
    # change is in what the indexer derives from that section, not in parsing.
    _, sections = parser.parse_markdown(LONG_DOC)
    assert [s["section_id"] for s in sections].count("see-also") == 1

    seen: list[str] = []
    result = _index(path, seen)
    assert result["indexed"] and result["error"] is None

    section_ids = {section_id for _, section_id, _, _ in _rows(path)}
    assert "see-also" not in section_ids
    assert section_ids == {"root", "overview", "rollback"}

    # Not reachable by keyword …
    assert store.search_fts("quillon") == []
    assert store.search_fts("zephyr ledger") == []
    # … nor by vector: there is no row and no vector to rank.
    footer_id = stable_md5_hexdigest(f"{path}#see-also")
    assert footer_id not in _vec_ids()
    hits = store.search_hybrid("quillon marsh", _VEC, top_k=10, threshold=0.0)
    assert [hit["section_id"] for hit in hits] == [
        hit["section_id"] for hit in hits if hit["section_id"] != "see-also"
    ]

    # The embedder was never handed the footer.
    assert seen and all(AUTO_FOOTER_MARKER not in text for text in seen)
    assert all("quillon-marsh" not in text for text in seen)
    assert any("fifteen minutes" in text for text in seen)


def test_a_section_with_a_body_and_a_trailing_footer_keeps_its_body(tmp_store):
    """Under the 2000-char threshold the whole note is one chunk."""
    path = _write(tmp_store, SHORT_DOC)
    seen: list[str] = []
    _index(path, seen)

    (row,) = _rows(path)
    _, section_id, content, version = row
    assert section_id == "root"
    assert "fifteen minutes" in content
    assert AUTO_FOOTER_MARKER not in content and "quillon-marsh" not in content
    assert version == projection.PROJECTION_VERSION

    assert [hit["section_id"] for hit in store.search_fts("fifteen minutes")] == ["root"]
    assert store.search_fts("quillon") == []
    assert seen == [content]


def test_a_user_written_see_also_is_still_indexed(tmp_store):
    """No marker, no footer: a hand-written heading is the author's content."""
    doc = (
        FRONTMATTER
        + "# Harbor release\n\n"
        + "The batch window is fifteen minutes.\n\n"
        + "## See also\n\n- [[quillon-marsh]] wrote the runbook\n"
    )
    path = _write(tmp_store, doc)
    _index(path)

    (row,) = _rows(path)
    assert "See also" in row[2] and "quillon-marsh" in row[2]
    assert [hit["section_id"] for hit in store.search_fts("quillon")] == ["root"]


def test_batch_embedding_is_handed_the_projected_text(tmp_store):
    """The batch path, asserted on its inputs rather than on its vectors."""
    path = _write(tmp_store, LONG_DOC)
    batches: list[list[str]] = []

    def _embed_many(texts: list[str]) -> list[list[float]]:
        batches.append(list(texts))
        return [_VEC for _ in texts]

    with patch("palinode.core.embedder.embed_many", side_effect=_embed_many):
        diff = reconcile.reconcile(path, LONG_DOC)

    assert diff.committed and diff.written == 3
    (batch,) = batches
    assert len(batch) == 3
    assert all(AUTO_FOOTER_MARKER not in text for text in batch)
    assert all("zephyr-ledger" not in text for text in batch)


# ── the links themselves are untouched ────────────────────────────────────────


def test_entity_and_cross_ref_extraction_read_the_file_not_the_index(tmp_store):
    """The footer still feeds the entity graph and ``cross_refs``.

    Both read the markdown on disk, so dropping the footer from the derived
    chunk cannot change either. Pinned here because the footer's whole purpose
    is the link graph — removing it from *retrieval* must not remove it from
    *linking*.
    """
    path = _write(tmp_store, LONG_DOC)
    _index(path)
    assert all("quillon" not in content.lower() for _, _, content, _ in _rows(path))

    # Entity rows: written by reconcile from `entities:` frontmatter.
    assert [f["file_path"] for f in store.get_entity_files("person/quillon-marsh")] == [path]
    assert [f["file_path"] for f in store.get_entity_files("project/zephyr-ledger")] == [path]

    # Body wikilinks: parsed straight from the file, footer included.
    metadata, _ = parser.parse_markdown(LONG_DOC)
    _, body = parser.parse_frontmatter(LONG_DOC)
    entities = parser.parse_entities(metadata, body)
    assert entities["entities_body"] == ["person/quillon-marsh", "project/zephyr-ledger"]
    assert entities["entities_resolved"] == [
        "person/quillon-marsh",
        "project/zephyr-ledger",
    ]

    # Mechanical cross-links: computed from the file's body, not from chunks.
    from palinode.core.cross_refs import build_registry, detect_refs

    other = _write(
        tmp_store,
        "---\nid: quillon-marsh\ncategory: people\n---\n\n# Quillon Marsh\n\nRunbook owner.\n",
        name="quillon-marsh.md",
    )
    _index(other)
    registry = build_registry(str(tmp_store), exclude_ref="projects/harbor-release")
    refs = detect_refs(_long_body() + FOOTER, registry, min_token_len=6)
    assert "projects/quillon-marsh" in refs


# ── an already-indexed store converges ────────────────────────────────────────


def test_a_store_indexed_before_the_change_converges_on_reindex(tmp_store, monkeypatch):
    """The existing projection-version mechanism is the whole migration.

    A pre-change row is stamped with an older ``projection_version``; the
    doctor's ``projection_current`` check counts those as migration progress
    and ``palinode reindex`` visits every file. Reconcile then prunes the
    footer-only row (``chunks_deleted``) and re-derives the mixed one.
    """
    path = _write(tmp_store, LONG_DOC)

    with monkeypatch.context() as legacy:
        # Exactly the pre-change derivation: the footer marker is not
        # recognised and the rows carry the previous version stamp.
        legacy.setattr(projection, "AUTO_FOOTER_MARKER", "<!-- not-a-marker -->")
        legacy.setattr(projection, "PROJECTION_VERSION", 1)
        _index(path)

    before = _rows(path)
    assert [section_id for _, section_id, _, _ in before].count("see-also") == 1
    assert all(version == 1 for _, _, _, version in before)
    assert store.search_fts("quillon") != []  # the footer tokens are live

    db_path = tmp_store / ".palinode.db"
    ctx = DoctorContext(config=Config(memory_dir=str(tmp_store), db_path=str(db_path)))
    behind = run_one(ctx, "projection_current")
    assert not behind.passed and "4 of 4 indexed chunks" in behind.message
    assert "palinode reindex" in behind.remediation

    result = _index(path)
    assert result["chunks_deleted"] == 1
    assert result["chunks_reprojected"] + result["chunks_stamped"] == 3
    assert "see-also" not in {section_id for _, section_id, _, _ in _rows(path)}
    assert store.search_fts("quillon") == []

    converged = run_one(ctx, "projection_current")
    assert converged.passed and "All 3 indexed chunks" in converged.message

    # And it is a fixed point: a second pass has nothing left to do.
    assert reconcile.plan(reconcile.derive(path, LONG_DOC)).is_noop
