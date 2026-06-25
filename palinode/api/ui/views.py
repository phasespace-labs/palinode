"""Data-shaping helpers for the Phase 1 provenance-UI views.

Store-agnostic by intent: each ``build_*`` function takes plain inputs (or
calls an injected capability callable) and returns template-ready dicts/lists.
They contain no business logic the store/API doesn't already expose — they
*shape* the output of existing capabilities (``list_api`` file scan,
``search_api``, ``git_tools.recent_commits``/``diff``, ``run_lint_pass``) for
rendering. Keeping them here (not in the router) lets ``weir`` reuse the same
list/search/diff/quality shaping against equivalent inputs.

Read-only throughout. None of these trigger a write — the compaction view, in
particular, reads git history rather than invoking the consolidation endpoint.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# Memory-list scan skips the same non-browsable dirs ``list_api`` does, so the
# UI list matches the canonical "memories a human browses" definition.
_LIST_SKIP_DIRS = frozenset({"daily", "archive", "inbox", "logs", "prompts", ".obsidian"})

# Freshness buckets (days since last_updated). "stale" mirrors lint's 90-day
# threshold so the memory-list freshness filter and the quality view agree.
_FRESH_DAYS = 7
_STALE_DAYS = 90


def is_browsable_memory(rel_path: str) -> bool:
    """Canonical "is this a browsable memory file?" predicate for the UI.

    The single source of truth every UI memory surface agrees on — the memory
    list, the sidebar/dashboard count, and the Quality queues all filter through
    this so they can't disagree (the bug where the sidebar badge showed 3 while
    the list showed 2 because lint counted a ``-history.md`` sibling the scan
    excluded). A path is browsable when it is a ``.md`` file, not under a
    skip-dir (``daily``/``archive``/``inbox``/``logs``/``prompts``/``.obsidian``),
    and not a ``-history.md`` consolidation sibling (those belong to the
    compaction view, not the memory list).
    """
    rel = rel_path.replace(os.sep, "/").lstrip("/")
    if not rel.endswith(".md"):
        return False
    parts = rel.split("/")
    if parts[0] in _LIST_SKIP_DIRS:
        return False
    if parts[-1].endswith("-history.md"):
        return False
    return True


def is_memory_file(rel_path: str) -> bool:
    """True for memory-content files; False for index/log/journal noise.

    Used to scrub commit file lists in the diffs / compaction views: the
    documented ``git add -A`` known-issue stages ``.palinode.db`` /
    ``.db-journal`` / ``logs/operations.jsonl`` alongside the real ``.md``
    writes, and those must not appear as memory "changes". Keeps any ``.md``
    file (including ``-history.md``, which IS a real change to surface in the
    diff/compaction context); drops the DB, its journal/wal/shm sidecars, and
    anything under ``logs/``.
    """
    rel = rel_path.replace(os.sep, "/").lstrip("/")
    if not rel:
        return False
    # Anything under logs/ is operational noise, even a stray .md.
    if rel.startswith("logs/") or "/logs/" in rel:
        return False
    # SQLite store + sidecars (.db, .db-journal, .db-wal, .db-shm) are not memory.
    base = rel.split("/")[-1]
    if base.endswith((".db", ".db-journal", ".db-wal", ".db-shm")):
        return False
    # Memory content is markdown; everything else (jsonl, yaml, etc.) is dropped.
    return rel.endswith(".md")


def _rel_to_id(rel_path: str) -> str:
    """Drop the .md suffix for the fact-detail URL (``/ui/memory/<id>``)."""
    return rel_path[:-3] if rel_path.endswith(".md") else rel_path


def _days_since(value: Any, now: datetime) -> int | None:
    """Whole days between an ISO-8601 ``last_updated`` and *now* (None if absent/bad)."""
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).days
    except (ValueError, TypeError):
        return None


def _freshness(days: int | None) -> str:
    """Bucket a days-old value into fresh / aging / stale / unknown."""
    if days is None:
        return "unknown"
    if days <= _FRESH_DAYS:
        return "fresh"
    if days <= _STALE_DAYS:
        return "aging"
    return "stale"


def scan_memory_files(
    memory_dir: str,
    read_frontmatter: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """One file walk of *memory_dir* → list of memory rows (markdown = truth).

    Mirrors ``list_api``'s skip-dir set so the count and contents agree with the
    dashboard's file-based "memories" total. ``read_frontmatter`` is injected
    (the router passes a parser-backed reader) so this stays store-agnostic.

    Each row: ``path`` (rel), ``id`` (path sans .md), ``name``, ``type``,
    ``category``, ``core`` (bool), ``last_updated``, ``days_old``, ``freshness``.
    """
    import glob

    now = datetime.now(timezone.utc)
    base = os.path.realpath(memory_dir)
    rows: list[dict[str, Any]] = []
    for filepath in glob.glob(os.path.join(base, "**/*.md"), recursive=True):
        try:
            if os.path.commonpath([base, os.path.realpath(filepath)]) != base:
                continue
        except ValueError:
            continue
        rel = os.path.relpath(filepath, base)
        # Single source of truth for "is this a browsable memory" — shared with
        # the sidebar/dashboard count and the Quality queues so they can't drift.
        if not is_browsable_memory(rel):
            continue
        parts = rel.split(os.sep)
        try:
            meta = read_frontmatter(filepath)
        except Exception:
            meta = {}
        last_updated = meta.get("last_updated") or meta.get("created_at") or ""
        days_old = _days_since(last_updated, now)
        rows.append(
            {
                "path": rel,
                "id": _rel_to_id(rel),
                "name": meta.get("title") or meta.get("name") or parts[-1][:-3],
                "type": meta.get("type"),
                "category": meta.get("category") or parts[0],
                "core": bool(meta.get("core", False)),
                "last_updated": str(last_updated),
                "days_old": days_old,
                "freshness": _freshness(days_old),
            }
        )
    rows.sort(key=lambda r: str(r.get("last_updated") or ""), reverse=True)
    return rows


def build_memory_list(
    rows: list[dict[str, Any]],
    *,
    type_filter: str | None = None,
    core_only: bool = False,
    freshness: str | None = None,
) -> dict[str, Any]:
    """Apply the type / core / freshness filters to scanned memory rows.

    Returns ``{"rows": [...], "types": [...], "filters": {...}, "total": N}``
    where ``types`` is the sorted distinct type set (for the filter UI) computed
    *before* filtering, and ``total`` is the post-filter count.
    """
    types = sorted({r["type"] for r in rows if r.get("type")})
    out = rows
    if type_filter:
        out = [r for r in out if r.get("type") == type_filter]
    if core_only:
        out = [r for r in out if r.get("core")]
    if freshness in {"fresh", "aging", "stale", "unknown"}:
        out = [r for r in out if r.get("freshness") == freshness]
    return {
        "rows": out,
        "types": types,
        "filters": {
            "type": type_filter or "",
            "core": core_only,
            "freshness": freshness or "",
        },
        "total": len(out),
    }


def run_search(
    query: str,
    search_callable: Callable[[str], Iterable[dict[str, Any]]],
    rel_path: Callable[[str], str],
) -> dict[str, Any]:
    """Shape search results for the UI.

    ``search_callable`` runs the existing search capability and yields result
    rows (``file_path``, ``snippet``, ``score``, ``metadata``); ``rel_path``
    maps an absolute chunk path to its memory-relative form. Returns
    ``{"query", "results", "count", "error"}``. ``error`` is set (and results
    empty) when the search backend is unavailable — e.g. the embedder is down —
    so a read-only audit UI degrades gracefully instead of 500-ing.
    """
    q = (query or "").strip()
    if not q:
        return {"query": "", "results": [], "count": 0, "error": None}
    try:
        raw = list(search_callable(q))
    except Exception as exc:  # noqa: BLE001 — surface as a soft banner, not a 500
        return {
            "query": q,
            "results": [],
            "count": 0,
            "error": f"search unavailable ({type(exc).__name__}) — is the embedder reachable?",
        }
    results = []
    for r in raw:
        rel = rel_path(r.get("file_path", ""))
        if not rel:
            continue
        meta = r.get("metadata") or {}
        results.append(
            {
                "path": rel,
                "id": _rel_to_id(rel),
                "name": meta.get("title") or meta.get("name") or Path(rel).stem,
                "type": meta.get("type"),
                "snippet": r.get("snippet") or "",
                "score": round(float(r.get("score", 0.0)), 3),
            }
        )
    return {"query": q, "results": results, "count": len(results), "error": None}


def _memory_files_only(files: list[str]) -> list[str]:
    """Drop index/log/journal noise (``.palinode.db``, ``logs/``, sidecars) from
    a commit's touched-file list — keep only memory-content files."""
    return [f for f in files if is_memory_file(f)]


def _group_commits_by_day(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group commit rows (newest-first) into ``[{"day", "commits": [...]}, ...]``.

    Each commit's file list is scrubbed to memory files only (defensive against
    the ``git add -A`` known-issue staging the DB/journal). A commit that touched
    *zero* memory files after scrubbing is dropped — it's not a memory change.
    """
    groups: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for c in commits:
        files = _memory_files_only(c.get("files", []) or [])
        if not files:
            continue  # no memory content changed — omit from the memory-changes view
        day = str(c.get("date", ""))[:10] or "unknown"
        grp = index.get(day)
        if grp is None:
            grp = {"day": day, "commits": []}
            index[day] = grp
            groups.append(grp)
        grp["commits"].append(
            {
                "hash": c.get("hash", ""),
                "message": c.get("message", ""),
                "time": str(c.get("date", ""))[11:16],
                "files": [{"path": f, "id": _rel_to_id(f)} for f in files],
                "file_count": len(files),
            }
        )
    return groups


def build_diffs_view(
    commits: list[dict[str, Any]],
    diff_summary: str,
    days: int,
) -> dict[str, Any]:
    """Recent memory changes grouped by day, plus the raw git diff summary.

    ``commit_count`` reflects commits that actually touched memory files (after
    scrubbing DB/log noise), so it agrees with what the grouped view shows.
    """
    groups = _group_commits_by_day(commits)
    shown = sum(len(g["commits"]) for g in groups)
    return {
        "days": days,
        "groups": groups,
        "commit_count": shown,
        "diff_summary": diff_summary or "",
    }


def build_compaction_view(
    consolidation_commits: list[dict[str, Any]],
    history_files: list[str],
    days: int,
) -> dict[str, Any]:
    """The last consolidation passes, read from git history (no triggering).

    ``consolidation_commits`` are the commits whose subject marks them as a
    compaction / nightly pass; ``history_files`` are the ``-history.md`` siblings
    on disk (the archived-fact audit trail consolidation appends to). Each pass's
    file list is scrubbed to memory files only (a consolidation commit's
    ``git add -A`` likewise stages the DB/journal). A pass is still listed even
    if its scrubbed file list is empty — the pass itself is the signal, and its
    subject line carries the summary.
    """
    passes = []
    for c in consolidation_commits:
        files = _memory_files_only(c.get("files", []) or [])
        passes.append(
            {
                "hash": c.get("hash", ""),
                "date": str(c.get("date", ""))[:16].replace("T", " "),
                "message": c.get("message", ""),
                "files": [{"path": f, "id": _rel_to_id(f)} for f in files],
                "file_count": len(files),
            }
        )
    return {
        "days": days,
        "passes": passes,
        "pass_count": len(passes),
        "history_files": [
            {"path": f, "id": _rel_to_id(f)} for f in sorted(history_files)
        ],
    }


# Quality buckets surfaced in the "what needs attention" view. Each maps a lint
# return key to a display label + the row-rendering shape.
def build_quality_view(lint: dict[str, Any]) -> dict[str, Any]:
    """Shape ``run_lint_pass`` output into linkable attention queues.

    Buckets: stale, orphaned, missing-description, contradictions, and (new)
    missing-extraction-metadata — facts with no captured extraction provenance
    (the G2 attestation field). P0/P1 carry no extraction metadata yet, so this
    queue lists every real memory fact honestly as "not yet captured", matching
    the provenance panel's gated placeholders.

    Every file-bearing bucket is filtered through ``is_browsable_memory`` so a
    ``-history.md`` consolidation sibling (or a skip-dir file) never shows up as
    something needing attention — the same predicate the memory list and the
    sidebar/dashboard count use, so the surfaces can't disagree.
    """
    def _files(items: list[Any], key: str = "file") -> list[dict[str, str]]:
        out = []
        for it in items:
            path = it.get(key) if isinstance(it, dict) else str(it)
            if not path or not is_browsable_memory(path):
                continue
            row: dict[str, str] = {"path": path, "id": _rel_to_id(path)}
            if isinstance(it, dict) and it.get("days_old") is not None:
                row["detail"] = f"{it['days_old']}d old"
            out.append(row)
        return out

    stale = _files(lint.get("stale_files", []))
    orphaned = _files(lint.get("orphaned_files", []))
    missing_desc = _files(lint.get("missing_descriptions", []))
    contradictions = [
        {"entity": c.get("entity", ""), "issue": c.get("issue", "")}
        for c in lint.get("contradictions", [])
        if isinstance(c, dict)
    ]
    # missing-extraction-metadata is keyed off the same scanned file set the
    # lint counts use (passed in by the router as ``no_extraction_meta``).
    no_extraction = _files(lint.get("no_extraction_meta", []))

    queues = [
        {
            "key": "stale",
            "label": "Stale",
            "blurb": f"active, not updated in {_STALE_DAYS}+ days",
            "rows": stale,
            "kind": "files",
        },
        {
            "key": "orphaned",
            "label": "Orphaned",
            "blurb": "no entities, unreferenced by any other memory",
            "rows": orphaned,
            "kind": "files",
        },
        {
            "key": "missing_description",
            "label": "No description",
            "blurb": "missing the one-line description",
            "rows": missing_desc,
            "kind": "files",
        },
        {
            "key": "contradictions",
            "label": "Contradictions",
            "blurb": "multiple active files for one entity",
            "rows": contradictions,
            "kind": "contradictions",
        },
        {
            "key": "no_extraction_meta",
            "label": "No extraction metadata",
            "blurb": "extraction provenance not yet captured (G2)",
            "rows": no_extraction,
            "kind": "files",
        },
    ]
    total = sum(len(q["rows"]) for q in queues)
    return {"queues": queues, "total": total}
