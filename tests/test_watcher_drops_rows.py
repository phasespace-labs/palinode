"""The watcher drops the rows of every path that leaves the store, whatever the
rule says of it, and indexes no file the walker would not list (issue 251, step two).

Handlers are driven with watchdog event objects and no observer. Real SQLite
on tmp_path; rows are seeded directly, as a file indexed before a rule changed.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from watchdog.events import (
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.indexer.watcher import PalinodeHandler
from tests._store_helpers import upsert_chunks


def _configure_store(memory_dir: Path, db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    monkeypatch.setattr(config, "db_path", str(db_dir / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(store, "_db_checked", False)
    monkeypatch.setenv("PALINODE_DIR", str(memory_dir))


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    memory_dir = tmp_path / "store"
    memory_dir.mkdir()
    _configure_store(memory_dir, memory_dir, monkeypatch)
    store.init_db()
    return memory_dir


@pytest.fixture()
def handler():
    h = PalinodeHandler()
    h.scheduled = []
    h._schedule_index = lambda path, **kw: h.scheduled.append(path)  # type: ignore[method-assign]
    yield h
    h.shutdown()


def _seed(path: Path) -> None:
    upsert_chunks([{
        "id": f"{path}#0",
        "file_path": str(path),
        "section_id": "root",
        "content": f"fixture body {path.name}",
        "embedding": [0.1] * config.embeddings.primary.dimensions,
    }])


def _indexed_paths() -> list[str]:
    db = store.get_db()
    try:
        rows = db.execute("SELECT DISTINCT file_path FROM chunks ORDER BY file_path").fetchall()
    finally:
        db.close()
    return [r[0] for r in rows]


def test_a_new_file_named_with_a_leading_dot_is_not_indexed(root: Path, handler) -> None:
    draft = root / "people" / ".draft.md"
    draft.parent.mkdir()
    draft.write_text("# draft\n", encoding="utf-8")

    handler.on_created(FileCreatedEvent(str(draft)))

    assert handler.scheduled == []


def test_deleting_a_file_the_rule_skips_drops_its_rows(root: Path, handler) -> None:
    _seed(root / "logs" / "x.md")

    handler.on_deleted(FileDeletedEvent(str(root / "logs" / "x.md")))

    assert _indexed_paths() == []


def test_deleting_a_folder_drops_the_rows_under_it_and_no_sibling_with_the_same_prefix(
    root: Path, handler
) -> None:
    _seed(root / "people" / "a.md")
    _seed(root / "peoplex" / "b.md")

    handler.on_deleted(DirDeletedEvent(str(root / "people")))

    assert _indexed_paths() == [str(root / "peoplex" / "b.md")]


def test_deleting_a_folder_the_rule_skips_drops_the_rows_under_it(root: Path, handler) -> None:
    _seed(root / "logs" / "x.md")

    handler.on_deleted(DirDeletedEvent(str(root / "logs")))

    assert _indexed_paths() == []


def test_deleting_a_folder_in_the_real_spelling_of_a_linked_store_drops_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _configure_store(link, real, monkeypatch)
    store.init_db()
    _seed(link / "people" / "a.md")
    h = PalinodeHandler()
    try:
        h.on_deleted(DirDeletedEvent(str(real / "people")))
    finally:
        h.shutdown()

    assert _indexed_paths() == []


def test_deleting_a_folder_drops_rows_kept_in_the_link_spelling_of_a_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _configure_store(real, real, monkeypatch)
    store.init_db()
    _seed(link / "people" / "a.md")
    h = PalinodeHandler()
    try:
        h.on_deleted(DirDeletedEvent(str(real / "people")))
    finally:
        h.shutdown()

    assert _indexed_paths() == []


def test_deleting_the_store_root_keeps_every_row(root: Path, handler) -> None:
    _seed(root / "people" / "a.md")

    handler.on_deleted(DirDeletedEvent(config.palinode_dir))

    assert _indexed_paths() == [str(root / "people" / "a.md")]


def test_deleting_the_real_spelling_of_a_linked_store_root_keeps_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _configure_store(link, real, monkeypatch)
    store.init_db()
    _seed(link / "people" / "a.md")
    h = PalinodeHandler()
    try:
        h.on_deleted(DirDeletedEvent(str(real)))
    finally:
        h.shutdown()

    assert _indexed_paths() == [str(link / "people" / "a.md")]


def test_moving_the_store_root_keeps_every_row(root: Path, handler) -> None:
    _seed(root / "people" / "a.md")

    handler.on_moved(DirMovedEvent(config.palinode_dir, str(root.parent / "renamed")))

    assert _indexed_paths() == [str(root / "people" / "a.md")]


def test_moving_a_folder_away_drops_the_rows_under_its_source(root: Path, handler) -> None:
    _seed(root / "people" / "a.md")
    _seed(root / "peoplex" / "b.md")

    handler.on_moved(DirMovedEvent(str(root / "people"), str(root / "staff")))

    assert _indexed_paths() == [str(root / "peoplex" / "b.md")]


def test_moving_a_file_out_of_a_skipped_folder_drops_the_source_rows(root: Path, handler) -> None:
    _seed(root / "logs" / "x.md")
    dest = root / "people" / "x.md"
    dest.parent.mkdir()
    dest.write_text("# x\n", encoding="utf-8")

    handler.on_moved(FileMovedEvent(str(root / "logs" / "x.md"), str(dest)))

    assert _indexed_paths() == []


def test_moving_a_file_into_the_store_indexes_the_destination(root: Path, handler) -> None:
    dest = root / "people" / "x.md"
    dest.parent.mkdir()
    dest.write_text("# x\n", encoding="utf-8")

    handler.on_moved(FileMovedEvent(str(root / "logs" / "x.md"), str(dest)))

    assert handler.scheduled == [str(dest)]


# ── a path event in either spelling of a linked store ───────────────────────


@pytest.fixture()
def linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    real = tmp_path / "real"
    (real / "people").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _configure_store(link, real, monkeypatch)
    store.init_db()
    return real, link


def test_deleting_a_file_in_the_real_spelling_drops_its_link_spelled_rows(linked, handler) -> None:
    real, link = linked
    _seed(link / "people" / "a.md")
    _seed(link / "people" / "b.md")

    handler.on_deleted(FileDeletedEvent(str(real / "people" / "a.md")))

    assert _indexed_paths() == [str(link / "people" / "b.md")]


def test_moving_a_file_in_the_real_spelling_drops_its_link_spelled_rows(linked, handler) -> None:
    real, link = linked
    _seed(link / "people" / "a.md")
    _seed(link / "people" / "b.md")

    handler.on_moved(
        FileMovedEvent(str(real / "people" / "b.md"), str(real / "people" / "c.md"))
    )

    assert _indexed_paths() == [str(link / "people" / "a.md")]


def test_deleting_a_file_in_the_link_spelling_drops_its_real_spelled_rows(linked, handler) -> None:
    real, link = linked
    _seed(real / "people" / "a.md")
    _seed(real / "people" / "b.md")

    handler.on_deleted(FileDeletedEvent(str(link / "people" / "a.md")))

    assert _indexed_paths() == [str(real / "people" / "b.md")]


def test_deleting_a_folder_drops_its_rows_in_both_spellings_of_a_linked_store(
    linked, handler
) -> None:
    real, link = linked
    _seed(link / "people" / "a.md")
    _seed(real / "people" / "b.md")

    handler.on_deleted(DirDeletedEvent(str(real / "people")))

    assert _indexed_paths() == []


# ── a removed link to a folder arrives as a file event ──────────────────────


@pytest.fixture()
def shared_link(tmp_path: Path, root: Path) -> Path:
    outside = tmp_path / "outside" / "shared"
    outside.mkdir(parents=True)
    (outside / "e.md").write_text("# e\n", encoding="utf-8")
    (root / "shared").symlink_to(outside, target_is_directory=True)
    _seed(root / "shared" / "e.md")
    _seed(root / "sharedx" / "f.md")
    return root


def test_removing_a_link_to_a_folder_drops_the_rows_under_it(shared_link: Path, handler) -> None:
    (shared_link / "shared").unlink()

    handler.on_deleted(FileDeletedEvent(str(shared_link / "shared")))

    assert _indexed_paths() == [str(shared_link / "sharedx" / "f.md")]


def test_moving_a_link_to_a_folder_drops_the_rows_under_its_source(
    shared_link: Path, handler
) -> None:
    (shared_link / "shared").rename(shared_link / "elsewhere")

    handler.on_moved(
        FileMovedEvent(str(shared_link / "shared"), str(shared_link / "elsewhere"))
    )

    assert _indexed_paths() == [str(shared_link / "sharedx" / "f.md")]


# ── a folder name is matched literally ──────────────────────────────────────


@pytest.mark.parametrize(
    ("deleted", "sibling"),
    [("peo_le", "people"), ("100%", "1000"), ("a\\b", "a/b")],
)
def test_deleting_a_folder_with_pattern_characters_drops_only_its_own_rows(
    root: Path, handler, deleted: str, sibling: str
) -> None:
    _seed(root / deleted / "x.md")
    _seed(root / sibling / "y.md")

    handler.on_deleted(DirDeletedEvent(str(root / deleted)))

    assert _indexed_paths() == [str(root / sibling / "y.md")]


# ── a match ends at the path's own boundary ─────────────────────────────────


def test_deleting_a_folder_keeps_rows_whose_paths_sort_beside_it(root: Path, handler) -> None:
    _seed(root / "people" / "a.md")
    _seed(root / "people-x" / "x.md")
    _seed(root / "people.d" / "x.md")
    _seed(root / "people.md")
    _seed(root / "people0" / "x.md")

    handler.on_deleted(DirDeletedEvent(str(root / "people")))

    assert _indexed_paths() == [
        str(root / "people-x" / "x.md"),
        str(root / "people.d" / "x.md"),
        str(root / "people.md"),
        str(root / "people0" / "x.md"),
    ]


def test_deleting_a_file_keeps_rows_whose_paths_start_with_its_name(root: Path, handler) -> None:
    _seed(root / "people" / "a.md")
    _seed(root / "people" / "a.md.bak")
    _seed(root / "people" / "a.mdx")
    _seed(root / "people" / "a" / "x.md")

    handler.on_deleted(FileDeletedEvent(str(root / "people" / "a.md")))

    assert _indexed_paths() == [
        str(root / "people" / "a.md.bak"),
        str(root / "people" / "a.mdx"),
        str(root / "people" / "a" / "x.md"),
    ]


def test_removing_a_link_to_a_folder_keeps_a_file_named_like_it(root: Path, handler) -> None:
    _seed(root / "shared" / "e.md")
    _seed(root / "shared.md")
    _seed(root / "shared-x" / "f.md")

    handler.on_deleted(FileDeletedEvent(str(root / "shared")))

    assert _indexed_paths() == [str(root / "shared-x" / "f.md"), str(root / "shared.md")]


def test_deleting_a_folder_named_beyond_ascii_keeps_its_longer_sibling(root: Path, handler) -> None:
    _seed(root / "ludzie" / "żółw" / "a.md")
    _seed(root / "ludzie" / "żółwie" / "b.md")

    handler.on_deleted(DirDeletedEvent(str(root / "ludzie" / "żółw")))

    assert _indexed_paths() == [str(root / "ludzie" / "żółwie" / "b.md")]


def _is_case_sensitive(folder: Path) -> bool:
    probe = folder / "case-probe"
    probe.write_text("", encoding="utf-8")
    try:
        return not (folder / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_deleting_a_folder_keeps_a_sibling_that_differs_only_in_case(root: Path, handler) -> None:
    if not _is_case_sensitive(root):
        pytest.skip("one folder on a case-insensitive filesystem")
    _seed(root / "people" / "x.md")
    _seed(root / "People" / "y.md")

    handler.on_deleted(DirDeletedEvent(str(root / "people")))

    assert _indexed_paths() == [str(root / "People" / "y.md")]


# ── reindex walks the store once ─────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    memory_dir = tmp_path / "store"
    memory_dir.mkdir()
    _configure_store(memory_dir, memory_dir, monkeypatch)
    size = config.embeddings.primary.dimensions
    with mock.patch("palinode.core.embedder.embed", return_value=[0.1] * size):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_reindex_indexes_a_file_inside_a_symlink_loop_once(client: TestClient) -> None:
    root = Path(config.memory_dir)
    people = root / "people"
    people.mkdir()
    (people / "a.md").write_text(
        "---\nid: a\ncategory: people\n---\n\n# A\n\nA fact worth indexing.\n", encoding="utf-8"
    )
    (people / "loop").symlink_to(people, target_is_directory=True)

    response = client.post("/reindex")

    assert response.status_code == 200, response.text
    assert _indexed_paths() == [str(people / "a.md")]
