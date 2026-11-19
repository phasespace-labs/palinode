"""The shared store walker of issue 251, step two: one place that turns a path
into a store path and lists the memory files under the store.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from palinode.core.memory_files import (
    MemoryFile,
    find_store_path,
    is_memory_path,
    is_spec_document,
    list_memory_files,
    list_spec_documents,
)
from palinode.core.skip_dirs import (
    SKIP_FOR_CROSS_REFS,
    SKIP_FOR_DIGEST,
    SKIP_FOR_LINT,
    SKIP_FOR_LISTING,
    SKIP_FOR_TTL,
)
from tests import _memory_rule_store as fixture


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    return root


def _write(path: Path, text: str = "# x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel_paths(files: list[MemoryFile]) -> list[str]:
    return [f.rel_path for f in files]


# ── find_store_path ──────────────────────────────────────────────────────────


def test_a_file_in_the_store_gives_its_store_path(store: Path) -> None:
    assert find_store_path(str(store / "people" / "a.md"), str(store)) == "people/a.md"


def test_a_real_path_event_under_a_linked_store_root_gives_the_store_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write(real / "people" / "a.md")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert find_store_path(str(real / "people" / "a.md"), str(link)) == "people/a.md"


def test_a_link_path_event_under_a_real_store_root_gives_the_store_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write(real / "people" / "a.md")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert find_store_path(str(link / "people" / "a.md"), str(real)) == "people/a.md"


def test_a_file_outside_the_store_gives_none(tmp_path: Path, store: Path) -> None:
    outside = _write(tmp_path / "elsewhere" / "x.md")

    assert find_store_path(str(outside), str(store)) is None


def test_the_store_parent_gives_none(tmp_path: Path, store: Path) -> None:
    assert find_store_path(str(tmp_path), str(store)) is None


def test_a_file_under_a_link_to_a_store_folder_keeps_the_link_spelling(store: Path) -> None:
    _write(store / "logs" / "x.md")
    (store / "shortcut").symlink_to(store / "logs", target_is_directory=True)

    assert find_store_path(str(store / "shortcut" / "x.md"), str(store)) == "shortcut/x.md"


# ── is_memory_path ───────────────────────────────────────────────────────────


def test_a_file_that_is_not_markdown_is_not_memory(store: Path) -> None:
    assert is_memory_path(str(store / "people" / "a.txt"), str(store)) is False


def test_a_file_name_starting_with_a_dot_is_not_memory(store: Path) -> None:
    assert is_memory_path(str(store / "people" / ".draft.md"), str(store)) is False


@pytest.mark.parametrize("rel", [".trash/x.md", "logs/x.md"])
def test_a_file_in_a_never_memory_folder_is_not_memory(store: Path, rel: str) -> None:
    assert is_memory_path(str(store / rel), str(store)) is False


def test_an_archived_file_is_not_memory_for_the_listing(store: Path) -> None:
    assert is_memory_path(str(store / "archive" / "x.md"), str(store), SKIP_FOR_LISTING) is False


def test_an_archived_file_is_memory_for_the_index(store: Path) -> None:
    assert is_memory_path(str(store / "archive" / "x.md"), str(store)) is True


def test_a_store_under_a_venv_folder_still_holds_memory(tmp_path: Path) -> None:
    store = tmp_path / "venv" / "store"
    _write(store / "people" / "a.md")

    assert is_memory_path(str(store / "people" / "a.md"), str(store)) is True


def test_a_file_outside_the_store_is_not_memory(tmp_path: Path, store: Path) -> None:
    outside = _write(tmp_path / "elsewhere" / "x.md")

    assert is_memory_path(str(outside), str(store)) is False


# ── list_memory_files: the shared fixture store, per exception set ───────────


@pytest.fixture()
def fixture_store(store: Path) -> Path:
    fixture.write_store(store)
    _write(store / "notes" / ".draft.md")
    return store


def test_the_index_walk_lists_every_memory_file(fixture_store: Path) -> None:
    assert _rel_paths(list_memory_files(str(fixture_store))) == [
        "archive/a.md",
        "daily/2026-01-01.md",
        "inbox/i.md",
        "logs.md",
        "notes/a.md",
        "team/archive/n.md",
        "team/daily/n.md",
        "team/specsheet/s.md",
    ]


def test_the_listing_walk_lists_curated_current_memory(fixture_store: Path) -> None:
    assert _rel_paths(list_memory_files(str(fixture_store), extra=SKIP_FOR_LISTING)) == [
        "logs.md",
        "notes/a.md",
        "team/specsheet/s.md",
    ]


def test_the_digest_walk_adds_the_inbox(fixture_store: Path) -> None:
    assert _rel_paths(list_memory_files(str(fixture_store), extra=SKIP_FOR_DIGEST)) == [
        "inbox/i.md",
        "logs.md",
        "notes/a.md",
        "team/specsheet/s.md",
    ]


def test_the_lint_walk_skips_only_archive(fixture_store: Path) -> None:
    assert _rel_paths(list_memory_files(str(fixture_store), extra=SKIP_FOR_LINT)) == [
        "daily/2026-01-01.md",
        "inbox/i.md",
        "logs.md",
        "notes/a.md",
        "team/daily/n.md",
        "team/specsheet/s.md",
    ]


def test_the_ttl_walk_skips_only_daily(fixture_store: Path) -> None:
    assert _rel_paths(list_memory_files(str(fixture_store), extra=SKIP_FOR_TTL)) == [
        "archive/a.md",
        "inbox/i.md",
        "logs.md",
        "notes/a.md",
        "team/archive/n.md",
        "team/specsheet/s.md",
    ]


def test_the_cross_refs_walk_lists_curated_current_memory(fixture_store: Path) -> None:
    assert _rel_paths(list_memory_files(str(fixture_store), extra=SKIP_FOR_CROSS_REFS)) == [
        "logs.md",
        "notes/a.md",
        "team/specsheet/s.md",
    ]


def test_a_listed_file_carries_its_store_spelling_and_store_path(store: Path) -> None:
    _write(store / "people" / "a.md")

    assert list_memory_files(str(store)) == [
        MemoryFile(path=os.path.join(str(store), "people/a.md"), rel_path="people/a.md"),
    ]


def test_a_linked_store_root_lists_paths_in_the_link_spelling(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write(real / "people" / "a.md")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert list_memory_files(str(link)) == [
        MemoryFile(path=os.path.join(str(link), "people/a.md"), rel_path="people/a.md"),
    ]


def test_files_are_ordered_by_store_path_not_folder_by_folder(store: Path) -> None:
    _write(store / "a" / "x.md")
    _write(store / "a.md")
    _write(store / "a-b.md")

    assert _rel_paths(list_memory_files(str(store))) == ["a-b.md", "a.md", "a/x.md"]


# ── list_memory_files: skipped folders are never entered ─────────────────────


@pytest.fixture()
def locked_node_modules(store: Path):
    _write(store / "people" / "a.md")
    locked = store / "node_modules"
    _write(locked / "pkg" / "readme.md")
    locked.chmod(0o000)
    yield store
    locked.chmod(0o755)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="root reads a mode 000 folder")
def test_an_unreadable_skipped_folder_does_not_fail_the_walk(locked_node_modules: Path) -> None:
    assert _rel_paths(list_memory_files(str(locked_node_modules))) == ["people/a.md"]


def test_the_walk_never_lists_a_skipped_folder(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(store / "people" / "a.md")
    _write(store / "node_modules" / "pkg" / "readme.md")
    listed: list[str] = []
    real_scandir, real_listdir = os.scandir, os.listdir

    def spy_scandir(path=".", *args, **kwargs):
        listed.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    def spy_listdir(path=".", *args, **kwargs):
        listed.append(os.fspath(path))
        return real_listdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", spy_scandir)
    monkeypatch.setattr(os, "listdir", spy_listdir)

    list_memory_files(str(store))

    assert str(store) in listed, f"the spy saw no listing of the store: {listed}"
    assert [p for p in listed if "node_modules" in p] == []


# ── list_memory_files: symlinked folders ─────────────────────────────────────


def test_a_symlink_loop_lists_each_file_once(store: Path) -> None:
    _write(store / "people" / "a.md")
    (store / "people" / "loop").symlink_to(store / "people", target_is_directory=True)

    assert _rel_paths(list_memory_files(str(store))) == ["people/a.md"]


def test_two_links_to_one_folder_list_both_store_paths(store: Path) -> None:
    _write(store / "notes" / "x.md")
    (store / "alias1").symlink_to(store / "notes", target_is_directory=True)
    (store / "alias2").symlink_to(store / "notes", target_is_directory=True)

    assert _rel_paths(list_memory_files(str(store))) == [
        "alias1/x.md",
        "alias2/x.md",
        "notes/x.md",
    ]


def test_a_link_to_a_skipped_folder_is_judged_by_its_store_path(store: Path) -> None:
    _write(store / "logs" / "x.md")
    (store / "shortcut").symlink_to(store / "logs", target_is_directory=True)

    assert _rel_paths(list_memory_files(str(store))) == ["shortcut/x.md"]


@pytest.fixture()
def linked_outside(tmp_path: Path, store: Path) -> Path:
    _write(store / "people" / "a.md")
    _write(tmp_path / "elsewhere" / "x.md")
    (store / "ext").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    return store


def test_a_folder_linked_outside_the_store_is_listed_under_its_store_path(linked_outside: Path) -> None:
    assert list_memory_files(str(linked_outside)) == [
        MemoryFile(path=os.path.join(str(linked_outside), "ext/x.md"), rel_path="ext/x.md"),
        MemoryFile(path=os.path.join(str(linked_outside), "people/a.md"), rel_path="people/a.md"),
    ]


def test_a_folder_linked_outside_the_store_is_dropped_when_the_walk_stays_inside(
    linked_outside: Path,
) -> None:
    assert _rel_paths(list_memory_files(str(linked_outside), stays_inside_store=True)) == [
        "people/a.md",
    ]


# ── list_memory_files: a folder inside the store ─────────────────────────────


def test_a_flat_walk_of_a_folder_lists_only_its_own_files_by_store_path(store: Path) -> None:
    _write(store / "decisions" / "x.md")
    _write(store / "decisions" / "sub" / "y.md")
    _write(store / "people" / "a.md")

    assert list_memory_files(str(store), under="decisions", is_recursive=False) == [
        MemoryFile(path=os.path.join(str(store), "decisions/x.md"), rel_path="decisions/x.md"),
    ]


def test_a_walk_under_a_folder_gives_paths_relative_to_the_store(fixture_store: Path) -> None:
    assert _rel_paths(list_memory_files(str(fixture_store), under="team")) == [
        "team/archive/n.md",
        "team/daily/n.md",
        "team/specsheet/s.md",
    ]


def test_a_walk_under_a_folder_the_surface_skips_lists_nothing(fixture_store: Path) -> None:
    assert list_memory_files(str(fixture_store), under="archive", extra=SKIP_FOR_LISTING) == []


# ── spec documents: hand-written specs/ files a cross-link may name ─────────


@pytest.mark.parametrize("rel", ["specs/a.md", "specs/sub/s.md"])
def test_a_hand_written_file_under_specs_is_a_spec_document(rel: str) -> None:
    assert is_spec_document(rel) is True


@pytest.mark.parametrize(
    "rel",
    [
        "specs/prompts/x.md",
        "specs/archive/x.md",
        "specs/.hid/x.md",
        "notes/specs/x.md",
        "specs.md",
    ],
)
def test_a_prompt_draft_or_file_outside_specs_is_not_a_spec_document(rel: str) -> None:
    assert is_spec_document(rel) is False


def test_spec_documents_are_listed_by_store_path_in_order(store: Path) -> None:
    _write(store / "specs" / "sub" / "s.md")
    _write(store / "specs" / "amr.md")
    _write(store / "specs" / ".draft.md")
    _write(store / "specs" / "prompts" / "p.md")
    _write(store / "specs" / "archive" / "old.md")
    _write(store / "notes" / "a.md")

    assert list_spec_documents(str(store)) == [
        MemoryFile(path=os.path.join(str(store), "specs/amr.md"), rel_path="specs/amr.md"),
        MemoryFile(path=os.path.join(str(store), "specs/sub/s.md"), rel_path="specs/sub/s.md"),
    ]


def test_a_store_without_specs_has_no_spec_documents(store: Path) -> None:
    _write(store / "notes" / "a.md")

    assert list_spec_documents(str(store)) == []


def test_a_symlink_loop_under_specs_lists_each_document_once(store: Path) -> None:
    _write(store / "specs" / "amr.md")
    (store / "specs" / "loop").symlink_to(store / "specs", target_is_directory=True)

    assert _rel_paths(list_spec_documents(str(store))) == ["specs/amr.md"]


def test_a_folder_linked_into_specs_is_listed_under_its_store_path(
    tmp_path: Path, store: Path
) -> None:
    _write(tmp_path / "elsewhere" / "x.md")
    (store / "specs").mkdir()
    (store / "specs" / "ext").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    assert _rel_paths(list_spec_documents(str(store))) == ["specs/ext/x.md"]


def test_a_link_into_specs_from_a_prompt_folder_is_judged_by_its_store_path(store: Path) -> None:
    _write(store / "specs" / "prompts" / "p.md")
    (store / "specs" / "shared").symlink_to(store / "specs" / "prompts", target_is_directory=True)

    assert _rel_paths(list_spec_documents(str(store))) == ["specs/shared/p.md"]
