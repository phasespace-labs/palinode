"""The one place that turns a path into a store path and lists the memory files.

Every reader of the store asks this module which files are memory. The rule for
directory segments stays in :mod:`palinode.core.skip_dirs`; this module adds the
store-relative spelling of a path (the given spelling first, then the resolved
spelling of the path and the store) and the walk.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from palinode.core.skip_dirs import SKIP_FOR_CROSS_REFS, is_skipped_path


@dataclass(frozen=True)
class MemoryFile:
    """A memory file: ``path`` in the store's own spelling, ``rel_path`` relative to the store."""

    path: str
    rel_path: str


def _relative_inside(path: str, store_dir: str) -> str | None:
    try:
        rel = os.path.relpath(path, store_dir)
    except ValueError:  # another drive on Windows
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


def find_store_path(path: str, store_dir: str) -> str | None:
    """Return ``path`` relative to ``store_dir`` with ``/`` separators, or None outside it.

    A symlinked store root makes events arrive in the other spelling, so the
    resolved paths are tried when the given ones do not match.
    """
    rel = _relative_inside(path, store_dir)
    if rel is None:
        rel = _relative_inside(os.path.realpath(path), os.path.realpath(store_dir))
    return rel


def _is_memory_name(name: str) -> bool:
    return name.endswith(".md") and not name.startswith(".")


def is_memory_path(path: str, store_dir: str, extra: Iterable[str] = frozenset()) -> bool:
    """True when ``path`` is a memory file of the store, judged by its store path."""
    if not _is_memory_name(os.path.basename(path)):
        return False
    rel = find_store_path(path, store_dir)
    return rel is not None and not is_skipped_path(rel, extra)


def is_spec_document(rel_path: str) -> bool:
    """True for a hand-written ``specs/<name>.md`` that is not a prompt, draft or log.

    ``specs/`` is never memory, but a cross-link may name a document in it.
    """
    head, _, rest = rel_path.replace("\\", "/").partition("/")
    return head == "specs" and not is_skipped_path(rest, SKIP_FOR_CROSS_REFS)


def _is_inside(path: str, root_real: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), root_real]) == root_real
    except ValueError:
        return False


def _get_folder_identity(path: str) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _walk_files(
    store_dir: str,
    start: str,
    is_pruned_dir: Callable[[str], bool],
    is_recursive: bool,
    stays_inside_store: bool,
) -> list[MemoryFile]:
    store_real = os.path.realpath(store_dir)
    found: list[MemoryFile] = []

    def visit(rel_dir: str, ancestors: frozenset[tuple[int, int]]) -> None:
        disk_dir = os.path.join(store_dir, rel_dir) if rel_dir else store_dir
        try:
            with os.scandir(disk_dir) as entries:
                listed = sorted(entries, key=lambda e: e.name)
        except OSError:
            return
        for entry in listed:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_dir():
                if not is_recursive or is_pruned_dir(rel):
                    continue
                identity = _get_folder_identity(entry.path)
                if identity is None or identity in ancestors:
                    continue
                visit(rel, ancestors | {identity})
            elif _is_memory_name(entry.name) and entry.is_file():
                if stays_inside_store and not _is_inside(entry.path, store_real):
                    continue
                found.append(MemoryFile(path=os.path.join(store_dir, rel), rel_path=rel))

    start_identity = _get_folder_identity(os.path.join(store_dir, start) if start else store_dir)
    visit(start, frozenset({start_identity}) if start_identity else frozenset())
    return sorted(found, key=lambda f: f.rel_path)


def list_memory_files(
    store_dir: str,
    *,
    under: str = "",
    extra: Iterable[str] = frozenset(),
    is_recursive: bool = True,
    stays_inside_store: bool = False,
) -> list[MemoryFile]:
    """List the memory files under ``store_dir``, or under its folder ``under``, sorted by store path.

    ``rel_path`` is always relative to ``store_dir``. Linked folders are followed;
    a link back to a folder on the current descent is not entered, so a loop ends.
    A folder the rule skips is never entered. ``stays_inside_store`` drops a file
    whose real path lies outside the real store root.
    """
    skip = frozenset(extra)
    start = under.replace("\\", "/").strip("/")
    if start and is_skipped_path(f"{start}/x", skip):
        return []
    return _walk_files(
        store_dir,
        start,
        lambda rel_dir: is_skipped_path(f"{rel_dir}/x", skip),
        is_recursive,
        stays_inside_store,
    )


def list_spec_documents(store_dir: str) -> list[MemoryFile]:
    """List the hand-written spec documents under ``<store_dir>/specs``, sorted by store path.

    ``rel_path`` starts with ``specs/``. Folders and links are handled as in
    :func:`list_memory_files`; a folder that is not a spec document is not entered.
    """
    return _walk_files(
        store_dir,
        "specs",
        lambda rel_dir: not is_spec_document(f"{rel_dir}/x"),
        is_recursive=True,
        stays_inside_store=False,
    )
