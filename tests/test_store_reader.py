"""``store.reader()``: the named reads over the derived index, on real SQLite."""
from __future__ import annotations

import hashlib

import pytest

from palinode.core import store
from tests import test_relative_floor_over_visible_hits as _floor
from tests._store_helpers import upsert_chunks
from tests.test_vector_arm_relative_floor import _at_cosine

memory = _floor.memory


@pytest.fixture()
def indexed(memory):
    a = str(memory / "insights" / "a.md")
    b = str(memory / "decisions" / "b.md")
    upsert_chunks([
        {"id": "a-root", "file_path": a, "section_id": "root", "content": "alpha root",
         "metadata": {"superseded_by": "decisions/b", "contradicts": ["insights/c"]},
         "created_at": "2026-10-01T00:00:00+00:00", "embedding": _at_cosine(0.5, 2)},
        {"id": "a-two", "file_path": a, "section_id": "s2", "content": "alpha two",
         "metadata": {}, "created_at": "2026-10-03T00:00:00+00:00", "embedding": None},
        {"id": "b-root", "file_path": b, "section_id": "root", "content": "beta",
         "metadata": {"backed_by": "insights/a"},
         "created_at": "2026-10-02T00:00:00+00:00", "embedding": _at_cosine(0.6, 3)},
    ], skip_unchanged=False)
    return a, b


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_counts_paths_and_summary(indexed):
    a, b = indexed
    with store.reader() as reader:
        assert reader.chunk_count() == 3
        assert reader.vector_count() == 2
        assert sorted(reader.indexed_paths()) == sorted([a, b])
        assert reader.file_chunk_summary() == [
            (a, 2, "2026-10-03T00:00:00+00:00"), (b, 1, "2026-10-02T00:00:00+00:00"),
        ]


def test_section_hashes_cover_every_chunk_under_the_paths(indexed):
    a, b = indexed
    with store.reader() as reader:
        assert sorted(reader.section_hashes([a, "missing.md"])) == [
            (a, "root", _hash("alpha root")), (a, "s2", _hash("alpha two")),
        ]
        assert reader.section_hashes([]) == []


def test_reverse_links_name_each_citing_file_and_field(indexed):
    a, b = indexed
    with store.reader() as reader:
        assert sorted(reader.reverse_links()) == sorted([
            (a, "superseded_by", "decisions/b"),
            (a, "contradicts", "insights/c"),
            (b, "backed_by", "insights/a"),
        ])


def test_stored_vector_and_files_still_waiting_for_one(indexed):
    a, b = indexed
    with store.reader() as reader:
        vec = reader.stored_vector("b-root")
        assert vec is not None and vec == pytest.approx(_at_cosine(0.6, 3), abs=1e-6)
        assert reader.stored_vector("a-two") is None
        assert sorted(path for path, _ in reader.indexed_files()) == sorted([a, b])
        assert [path for path, _ in reader.indexed_files(vectorless=True)] == [a]
        metadata = dict(reader.indexed_files())
        assert metadata[b] == {"backed_by": "insights/a"}


def test_recall_for_file_sums_its_chunks(indexed):
    a, _ = indexed
    store.record_recall(["a-root", "a-two"], mode="passive")
    store.record_recall(["a-root"], mode="passive")
    with store.reader() as reader:
        total, last = reader.recall_for_file(a)
        assert total == 3 and last is not None
        assert reader.recall_for_file("nowhere.md") == (0, None)
