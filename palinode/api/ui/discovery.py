"""Visible-store adapters for inspector discovery; maintenance stays full-store.

**The inspector's visibility rule, stated once.** Discovery — the
dashboard, the list, the recent panel — is visibility-filtered: it walks
``collect_memory_files``, which routes every row through
:func:`palinode.core.visibility.is_visible`, so a ``private`` or ``restricted``
memory is not listed. Direct reads are not filtered, and that is deliberate:

1. **A direct read stays allowed.** ``/ui/memory/<file>`` and
   ``/ui/history/<file>`` render a record the listing hides. The inspector is
   loopback-only and its user is the store's local operator, who already owns
   the files; refusing would protect nothing and would break inspecting history
   for exactly the records most worth inspecting.
2. **The page labels it.** :func:`discovery_visibility` is what both pages ask,
   so a hidden record is never mistaken for a default-visible one.
3. **No mutation is offered for a hidden record.** Anything the inspector
   offers that would change a record — the correction / retirement section — is
   replaced by a refusal pointing at the CLI and API, where the caller's
   authority is explicit. The correction action must not become a quiet way to
   edit restricted records from a page that never checked visibility.

``ui_memory`` and ``ui_history`` follow it identically.
"""
from __future__ import annotations

import os
from typing import Any

from palinode.core import git_tools, store
from palinode.core.config import config
from palinode.core.lint import run_lint_pass
from palinode.core.revalidation import normalize_ref


def discovery_lint(memories: list[dict[str, Any]]) -> dict[str, Any]:
    """Lint only the live visible set, including all cross-file computations."""
    paths = {r["path"] for r in memories}
    lint = run_lint_pass(file_paths=paths)
    refs = {normalize_ref(p) for p in paths}
    # Persisted backing entries can still name a source outside discovery.
    # Do not reveal its ref, retirement state, or even a finding count.
    backing = []
    for finding in lint.get("stale_backing", []):
        entries = [
            e for e in finding.get("stale_backing", [])
            if normalize_ref(str(e.get("ref", ""))) in refs
        ]
        if entries:
            backing.append({"file": finding["file"], "stale_backing": entries})
    lint["stale_backing"] = backing
    lint["no_extraction_meta"] = [{"file": p} for p in sorted(paths)]
    return lint


def indexed_discovery(memories: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """Count visible chunks and choose recent files before applying a limit.

    Read only aggregate index data; live file rows supply the displayed type.
    Hidden or deleted rows cannot consume the recent list's twelve slots.
    """
    visible = {os.path.join(config.memory_dir, r["path"]): r for r in memories}
    if not visible:
        return 0, []
    try:
        db = store.get_db()
        try:
            rows = db.execute(
                "SELECT file_path, COUNT(*) AS chunks, MAX(created_at) AS recent "
                "FROM chunks GROUP BY file_path ORDER BY recent DESC, file_path"
            ).fetchall()
        finally:
            db.close()
    except Exception:
        return 0, []
    total = 0
    recent = []
    for row in rows:
        memory = visible.get(row["file_path"])
        if memory is None:
            continue
        total += row["chunks"]
        if len(recent) < 12:
            recent.append({"path": memory["path"], "type": memory["type"]})
    return total, recent


#: The human-facing label for each way discovery hides a record. Keyed by the
#: reason, so a page renders the reason rather than re-deriving it.
_VISIBILITY_LABELS: dict[str, str] = {
    "private": "private — withheld from every listing, from recall and from the session-start digest",
    "restricted": "restricted — withheld from every surface that carries no matching scope",
    "scope": "out of scope — withheld from listings that carry no matching scope",
    "unreadable": "unreadable frontmatter — hidden rather than assumed public",
    "not-browsable": "not a browsable memory — excluded from the memory list",
}


def discovery_visibility(
    rel_path: str, *, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Would the inspector's discovery surfaces hide this record, and why?

    Asks the same choke point the listing asks
    (:func:`palinode.core.visibility.is_visible` with no scope chain — the
    ``GET /list`` contract), plus the browsable-memory predicate the memory
    list applies. Returns ``hidden``, a machine ``reason`` and a human
    ``label``; a visible record reports ``hidden: False`` with no reason.

    Reading-only, and never a refusal by itself: rule 1 above means a direct
    read is still served. What the caller does with ``hidden`` is rule 2 (label
    the page) and rule 3 (offer no mutation).
    """
    from palinode.api.ui.views import is_browsable_memory
    from palinode.core.visibility import is_visible

    rel = str(rel_path).replace(os.sep, "/").lstrip("/")
    abs_path = os.path.join(config.memory_dir, rel)

    meta = metadata
    if meta is None:
        try:
            from palinode.core import parser

            with open(abs_path, encoding="utf-8") as handle:
                meta, _ = parser.parse_frontmatter(handle.read())
        except (OSError, ValueError, UnicodeDecodeError):
            return {
                "hidden": True,
                "reason": "unreadable",
                "label": _VISIBILITY_LABELS["unreadable"],
            }

    if not is_visible(None, abs_path, metadata=meta):
        declared = str((meta or {}).get("visibility") or "").strip().lower()
        reason = declared if declared in {"private", "restricted"} else "scope"
        return {
            "hidden": True,
            "reason": reason,
            "label": _VISIBILITY_LABELS[reason],
        }

    if not is_browsable_memory(rel):
        return {
            "hidden": True,
            "reason": "not-browsable",
            "label": _VISIBILITY_LABELS["not-browsable"],
        }

    return {"hidden": False, "reason": None, "label": "listed in discovery"}


def visible_commit_count(memories: list[dict[str, Any]], days: int = 7) -> int:
    """Count commits touching currently visible files, with literal pathspecs.

    Batch paths to keep argv bounded; deduplicate commits touching multiple
    batches. No global limit can let hidden-only commits displace visible ones.
    """
    paths = sorted({r["path"] for r in memories})
    commits: set[str] = set()
    try:
        for start in range(0, len(paths), 200):
            result = git_tools._run_git(
                "--literal-pathspecs", "log", "--format=%H", f"--since={days}.days",
                "HEAD", "--", *paths[start:start + 200],
            )
            if result.returncode == 0:
                commits.update(result.stdout.splitlines())
    except OSError:
        return 0
    return len(commits)
