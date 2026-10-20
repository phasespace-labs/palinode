"""
Milestone dependency graph traversal — palinode_depends.

Reads `depends_on`, `blocks`, and `parallel_with` frontmatter fields from
ProjectSnapshot (and any) memory files, builds a dependency graph, and answers
two questions:

1. Given a slug, what does its dependency neighbourhood look like, and is it
   unblocked (all `depends_on` are status=done)?
2. Across all memory files, which milestones/tasks have all their dependencies
   done and are therefore ready to start?

The parser does not validate that referenced slugs exist — missing targets show
up as orphans in the returned dict.

Slug vocab: `milestone/M1`, `task/foo`, etc.  Any `kind/name` string is valid.
The module resolves slugs by scanning memory files for a `slug:` frontmatter
field that matches; it does not rely on filenames alone.
"""
from __future__ import annotations

import glob
import logging
import os
from typing import Any

import frontmatter as _frontmatter

from palinode.core.config import config
from palinode.core.skip_dirs import is_skipped_path

logger = logging.getLogger("palinode.depends")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _memory_dir() -> str:
    return os.path.realpath(getattr(config, "memory_dir", config.palinode_dir))


def _iter_md_files(base: str):
    """Yield absolute paths of the memory .md files under *base*."""
    for path in glob.iglob(os.path.join(base, "**", "*.md"), recursive=True):
        if not is_skipped_path(os.path.relpath(path, base)):
            yield path


def _load_frontmatter(path: str) -> dict[str, Any]:
    """Read and return the frontmatter dict for *path*, empty dict on error."""
    try:
        with open(path, encoding="utf-8") as f:
            post = _frontmatter.load(f)
        return dict(post.metadata)
    except Exception:
        return {}


def _coerce_list(value: Any) -> list[str]:
    """Coerce a frontmatter field to a list of strings (empty list on None)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(s).strip() for s in value if s]
    if isinstance(value, str):
        val = value.strip()
        return [val] if val else []
    return []


def _build_slug_index(base: str) -> dict[str, dict[str, Any]]:
    """Return ``{slug: {file_path, status, depends_on, blocks, parallel_with}}``.

    We scan every .md file; if it has a ``slug:`` frontmatter key we register
    it in the index.  Files without a slug are skipped (they can still appear
    as orphans if something references them).
    """
    index: dict[str, dict[str, Any]] = {}
    for path in _iter_md_files(base):
        meta = _load_frontmatter(path)
        slug = meta.get("slug")
        if not slug:
            continue
        slug = str(slug).strip()
        if not slug:
            continue
        index[slug] = {
            "file_path": path,
            "status": str(meta.get("status", "")).strip() or None,
            "depends_on": _coerce_list(meta.get("depends_on")),
            "blocks": _coerce_list(meta.get("blocks")),
            "parallel_with": _coerce_list(meta.get("parallel_with")),
        }
    return index


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def _node_entry(slug: str, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the ``{slug, status, found}`` representation for a dep entry."""
    info = index.get(slug)
    if info:
        return {"slug": slug, "status": info["status"], "found": True}
    return {"slug": slug, "status": None, "found": False}


def traverse_depends(slug: str, memory_dir: str | None = None) -> dict[str, Any]:
    """Return the dependency neighbourhood for *slug*.

    The result shape mirrors the issue spec::

        {
            "slug": "milestone/M1.1-init",
            "depends_on": [{"slug": "...", "status": "done", "found": true}, ...],
            "blocks": [...],
            "parallel_with": [...],
            "unblocked": bool,
            "orphans": ["milestone/X"],
        }

    *unblocked* is True only when every entry in ``depends_on`` has
    ``status == "done"`` (or when there are no dependencies at all).

    Slugs referenced in any of the three fields that have no corresponding
    memory file appear in ``orphans``.
    """
    base = memory_dir or _memory_dir()
    index = _build_slug_index(base)

    root = index.get(slug)
    if root is None:
        # Slug not found — return an empty neighbourhood with the slug marked
        # as an orphan itself so the caller knows it was not resolved.
        return {
            "slug": slug,
            "depends_on": [],
            "blocks": [],
            "parallel_with": [],
            "unblocked": True,
            "orphans": [slug],
        }

    depends_on_entries = [_node_entry(s, index) for s in root["depends_on"]]
    blocks_entries = [_node_entry(s, index) for s in root["blocks"]]
    parallel_entries = [_node_entry(s, index) for s in root["parallel_with"]]

    # unblocked = no depends_on, or every depends_on is status=done
    if not depends_on_entries:
        unblocked = True
    else:
        unblocked = all(e["status"] == "done" for e in depends_on_entries)

    # Orphans: referenced slugs (in any relation) with no matching memory file
    all_referenced = (
        [e["slug"] for e in depends_on_entries]
        + [e["slug"] for e in blocks_entries]
        + [e["slug"] for e in parallel_entries]
    )
    orphans = [s for s in all_referenced if not index.get(s)]

    return {
        "slug": slug,
        "depends_on": depends_on_entries,
        "blocks": blocks_entries,
        "parallel_with": parallel_entries,
        "unblocked": unblocked,
        "orphans": sorted(set(orphans)),
    }


def find_unblocked(memory_dir: str | None = None) -> list[dict[str, Any]]:
    """Return all slugs whose every ``depends_on`` is status=done.

    Each entry in the returned list is::

        {"slug": "...", "status": "in_progress", "file_path": "..."}

    Items with no depends_on (unconstrained) are included.  Items whose own
    status is "done" or "archived" are excluded — they're already finished.
    """
    base = memory_dir or _memory_dir()
    index = _build_slug_index(base)

    result: list[dict[str, Any]] = []
    for slug, info in sorted(index.items()):
        own_status = info["status"]
        # Skip already-done or archived items
        if own_status in ("done", "archived"):
            continue

        deps = info["depends_on"]
        if not deps:
            # No constraints → unblocked by definition
            result.append({
                "slug": slug,
                "status": own_status,
                "file_path": info["file_path"],
            })
            continue

        all_done = all(
            index.get(dep, {}).get("status") == "done"
            for dep in deps
        )
        if all_done:
            result.append({
                "slug": slug,
                "status": own_status,
                "file_path": info["file_path"],
            })

    return result


__all__ = [
    "traverse_depends",
    "find_unblocked",
]
