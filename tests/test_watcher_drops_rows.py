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
