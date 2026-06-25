"""FastAPI router for the local read-only provenance UI (Phases 0–1).

Mounted on the existing app under ``/ui`` — no new service, no build step.
Server-rendered HTML via Jinja2; CSS shipped in-package. The router is a pure
client of existing capabilities:

  - ``store.get_stats`` + ``lint.run_lint_pass``  → dashboard health summary
  - ``store.list_recent``                          → dashboard recent list
  - ``list_api`` (file scan) + ``search_api``      → memory list + search (P1)
  - ``git_tools.recent_commits`` + ``diff``        → diffs / compaction (P1)
  - ``_resolve_memory_path`` + ``parser``          → fact body + frontmatter
  - ``git_tools.history``                          → git lineage / Saved commit
  - chunk recall_count (read-only query)           → retrieval stats

No mutations, no new business logic. Loopback-only: ``_loopback_guard``
refuses to render when the API host resolves to a non-loopback address,
reusing the same bind-intent signal the API/MCP startup gates use
(``PALINODE_API_BIND_INTENT``).
"""
from __future__ import annotations

import ipaddress
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from palinode.core import git_tools, store
from palinode.core.config import config
from palinode.core.parser import parse_markdown

from palinode.api.path_safety import _resolve_memory_path
from palinode.api.ui.provenance import build_provenance
from palinode.api.ui.render import render_markdown
from palinode.api.ui.views import (
    build_compaction_view,
    build_diffs_view,
    build_memory_list,
    build_quality_view,
    is_browsable_memory,
    run_search,
    scan_memory_files,
)

router = APIRouter(prefix="/ui")

_UI_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_UI_DIR / "templates"))


def mount_static(app: Any) -> None:
    """Mount the in-package static dir at ``/ui/static`` (name ``ui_static``).

    Called by the server module after ``include_router``. Kept separate from
    router registration because ``StaticFiles`` mounts onto the app, not a
    router. ``url_for('ui_static', path=...)`` resolves against this mount.
    """
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(_UI_DIR / "static")),
        name="ui_static",
    )


# ── Loopback guard ───────────────────────────────────────────────────────────
def _host_is_loopback(host: str) -> bool:
    """True if *host* resolves to a loopback address (or is the bare hostname).

    ``localhost`` and ``127.0.0.0/8`` / ``::1`` are loopback. ``0.0.0.0`` (and
    any routable address) is not. Unresolvable hosts are treated as non-loopback
    (fail closed).
    """
    h = (host or "").strip().lower()
    if h in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        # Hostname, not a literal IP — resolve it and require every A/AAAA
        # record to be loopback before we trust it.
        try:
            infos = socket.getaddrinfo(h, None)
        except socket.gaierror:
            return False
        addrs = {info[4][0] for info in infos}
        if not addrs:
            return False
        try:
            return all(ipaddress.ip_address(a).is_loopback for a in addrs)
        except ValueError:
            return False


def _loopback_guard() -> None:
    """Refuse to serve the UI on a non-loopback bind.

    Reuses the API's bind-intent signal: the host comes from
    ``PALINODE_API_HOST`` (falling back to ``config.services.api.host``), the
    same resolution ``server.py`` uses for its unsafe-bind warning. Unlike the
    API (which warns and serves), the UI *hard-refuses* on a public bind — it
    is an unauthenticated, read-everything audit surface and must never be
    network-exposed in P0. ``PALINODE_API_BIND_INTENT=public`` does NOT lift
    this refusal; the UI has no auth, so there is no safe public path yet.
    """
    host = os.environ.get("PALINODE_API_HOST", config.services.api.host)
    if not _host_is_loopback(host):
        raise HTTPException(
            status_code=403,
            detail=(
                "Palinode UI is loopback-only — refusing to serve on a "
                f"non-loopback bind ({host}). Set PALINODE_API_HOST=127.0.0.1."
            ),
        )


# ── Shared template context ──────────────────────────────────────────────────
def _browsable_lint_files(lint: dict[str, Any], key: str) -> int:
    """Count a lint file-bucket restricted to browsable memories.

    The lint pass counts ``-history.md`` siblings (and may include skip-dir
    files); the UI's badges and Quality queues must not. Filtering through the
    same ``is_browsable_memory`` predicate the memory list/count use keeps every
    surface in agreement.
    """
    out = 0
    for it in lint.get(key, []):
        path = it.get("file") if isinstance(it, dict) else str(it)
        if path and is_browsable_memory(path):
            out += 1
    return out


def _page_context() -> dict[str, Any]:
    """Build the context every page needs: shell + health counts.

    ``total_memories`` is the count of *browsable* memory files on disk — the
    same ``scan_memory_files`` / ``is_browsable_memory`` definition the memory
    list uses — so the sidebar badge, the dashboard MEMORIES card, and the list
    can never disagree (the bug where the badge showed 3 vs the list's 2 because
    lint counted a ``-history.md`` sibling). The lint-derived health counts are
    likewise restricted to browsable memories. ``total_chunks`` stays the
    indexed-DB metric; ``unindexed`` is True when files exist on disk but no
    chunks are indexed yet (fresh store / watcher not running).
    """
    from palinode.core.lint import run_lint_pass

    lint = run_lint_pass()
    total_memories = len(scan_memory_files(config.memory_dir, _read_frontmatter))

    try:
        total_chunks = store.get_stats().get("total_chunks", 0)
    except Exception:
        total_chunks = 0

    # Health counts filtered to browsable memories so the header + sidebar badge
    # match the Quality view (which applies the same filter).
    stale_count = _browsable_lint_files(lint, "stale_files")
    orphaned_count = _browsable_lint_files(lint, "orphaned_files")
    missing_descriptions = _browsable_lint_files(lint, "missing_descriptions")
    contradictions = len(lint.get("contradictions", []))  # entity-keyed, not file

    return {
        "total_memories": total_memories,
        "total_chunks": total_chunks,
        "unindexed": total_memories > 0 and total_chunks == 0,
        "palinode_dir": config.memory_dir,
        "api_port": config.services.api.port,
        "stale_count": stale_count,
        "orphaned_count": orphaned_count,
        "missing_descriptions": missing_descriptions,
        "contradictions": contradictions,
        "core_count": lint.get("core_count", 0),
        # Sidebar "Quality" badge: total actionable lint findings (excludes the
        # all-facts missing-extraction-metadata bucket so the badge reflects
        # real problems, not the universal not-yet-captured placeholder).
        "nav_quality_count": stale_count + orphaned_count
        + missing_descriptions + contradictions,
    }


def _recall_for_path(resolved_abs: str) -> tuple[int, str | None]:
    """Read recall_count / last_recalled for a file's chunks (read-only).

    ``index_file`` stores absolute paths in ``chunks.file_path``; sum recall
    across the file's chunks and take the most-recent ``last_recalled``. Pure
    read — no mutation, no new business logic (the columns are ADR-007's).
    Returns ``(0, None)`` if the file isn't indexed or the DB is unavailable.
    """
    try:
        db = store.get_db()
    except Exception:
        return 0, None
    try:
        rows = db.execute(
            "SELECT recall_count, last_recalled FROM chunks WHERE file_path = ?",
            (resolved_abs,),
        ).fetchall()
    except Exception:
        return 0, None
    finally:
        db.close()
    if not rows:
        return 0, None
    total = sum((r["recall_count"] or 0) for r in rows)
    recalls = [r["last_recalled"] for r in rows if r["last_recalled"]]
    last = max(recalls) if recalls else None
    return total, last


# ── Views ─────────────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse, name="ui_dashboard")
@router.get("/", response_class=HTMLResponse, name="ui_dashboard_slash")
def ui_dashboard(request: Request) -> HTMLResponse:
    """Dashboard: memory-health summary from status + lint."""
    _loopback_guard()
    ctx = _page_context()

    git_stats = git_tools.commit_count(7)
    ctx["git_commits_7d"] = git_stats.get("total_commits", 0)

    recent: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        # list_recent returns one row per chunk; dedup to one entry per file.
        for row in store.list_recent(limit=40):
            rel = _rel_path(row.get("file_path", ""))
            if not rel or rel in seen:
                continue
            seen.add(rel)
            meta = row.get("metadata") or {}
            recent.append({"path": rel, "type": meta.get("type")})
            if len(recent) >= 12:
                break
    except Exception:
        recent = []
    ctx["recent"] = recent

    ctx["active"] = "dashboard"
    return templates.TemplateResponse(request, "dashboard.html", ctx)


def _read_frontmatter(filepath: str) -> dict[str, Any]:
    """Parse just the frontmatter dict of a file on disk (no body, never raises)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        metadata, _ = parse_markdown(content)
        return metadata or {}
    except Exception:
        return {}


@router.get("/memory", response_class=HTMLResponse, name="ui_memory_list")
def ui_memory_list(
    request: Request,
    q: str = "",
    type: str = "",
    core: bool = False,
    freshness: str = "",
) -> HTMLResponse:
    """Memory list (the "Memory" nav target) with type/core/freshness filters,
    plus a search box that queries the existing search endpoint.

    The list is file-sourced (markdown = truth) so it stays coherent with the
    dashboard counts even on an unindexed store. Search, by contrast, needs the
    index — it degrades to a soft banner when the embedder is unreachable.
    """
    _loopback_guard()
    ctx = _page_context()
    ctx["active"] = "memory"

    rows = build_memory_list(
        scan_memory_files(config.memory_dir, _read_frontmatter),
        type_filter=type or None,
        core_only=bool(core),
        freshness=freshness or None,
    )
    ctx["listing"] = rows

    # Search is opt-in via ?q= and routes through the existing search_api in
    # process (no new ranking logic). Empty q → no search section.
    ctx["search"] = run_search(q, _search_memory, _rel_path)

    return templates.TemplateResponse(request, "memory_list.html", ctx)


@router.get("/diffs", response_class=HTMLResponse, name="ui_diffs")
def ui_diffs(request: Request, days: int = 14) -> HTMLResponse:
    """Recent memory changes (the "Diffs" nav) from git, grouped by day."""
    _loopback_guard()
    days = max(1, min(days, 365))
    ctx = _page_context()
    ctx["active"] = "diffs"

    try:
        commits = git_tools.recent_commits(days=days, limit=200)
    except Exception:
        commits = []
    try:
        diff_summary = git_tools.diff(days)
    except Exception:
        diff_summary = ""
    ctx["diffs"] = build_diffs_view(commits, diff_summary, days)
    return templates.TemplateResponse(request, "diffs.html", ctx)


@router.get("/compaction", response_class=HTMLResponse, name="ui_compaction")
def ui_compaction(request: Request, days: int = 90) -> HTMLResponse:
    """Compaction review (the "Compaction" nav): the last consolidation passes,
    read from git history. Read-only — does NOT trigger consolidation."""
    _loopback_guard()
    days = max(1, min(days, 365))
    ctx = _page_context()
    ctx["active"] = "compaction"

    # Consolidation commits are subject-prefixed by the runner (compaction /
    # nightly). Pull both, newest-first, then the -history.md audit siblings.
    commits: list[dict[str, Any]] = []
    try:
        for prefix in ("palinode: compaction", "palinode: nightly"):
            commits.extend(git_tools.recent_commits(days=days, limit=100, message_prefix=prefix))
        commits.sort(key=lambda c: str(c.get("date", "")), reverse=True)
    except Exception:
        commits = []
    ctx["compaction"] = build_compaction_view(commits, _history_files(), days)
    return templates.TemplateResponse(request, "compaction.html", ctx)


@router.get("/quality", response_class=HTMLResponse, name="ui_quality")
def ui_quality(request: Request) -> HTMLResponse:
    """Quality queues (the "Quality" nav): lint findings — stale, orphaned,
    missing-description, contradictions, and missing-extraction-metadata —
    each linking to its fact."""
    _loopback_guard()
    from palinode.core.lint import run_lint_pass

    lint = run_lint_pass()
    # Augment with the new missing-extraction-metadata bucket: every browsable
    # memory fact, since extraction provenance (G2) is not captured yet. Scanned
    # the same way the file-based counts are, so it stays coherent.
    lint = dict(lint)
    lint["no_extraction_meta"] = [
        {"file": r["path"]}
        for r in scan_memory_files(config.memory_dir, _read_frontmatter)
    ]

    ctx = _page_context()
    ctx["active"] = "quality"
    ctx["quality"] = build_quality_view(lint)
    return templates.TemplateResponse(request, "quality.html", ctx)


@router.get("/memory/{file_path:path}", response_class=HTMLResponse, name="ui_memory")
def ui_memory(request: Request, file_path: str) -> HTMLResponse:
    """Fact detail: rendered body + frontmatter chips + provenance panel."""
    _loopback_guard()

    # Resolve + read (same traversal guard the /read endpoint uses).
    candidates = [file_path]
    if not file_path.endswith(".md"):
        candidates.append(f"{file_path}.md")
    resolved_abs = ""
    rel = ""
    content = ""
    for candidate in candidates:
        _, resolved_candidate = _resolve_memory_path(candidate)
        if os.path.exists(resolved_candidate):
            with open(resolved_candidate, "r", encoding="utf-8") as f:
                content = f.read()
            resolved_abs = resolved_candidate
            rel = candidate
            break
    if not resolved_abs:
        raise HTTPException(status_code=404, detail="Memory not found")

    # parse_markdown returns (metadata, sections-list); for the rendered body we
    # need the raw markdown, so split frontmatter directly (same approach as
    # core.lint). Falls back to the whole file if frontmatter parsing fails.
    metadata, _ = parse_markdown(content)
    body = _strip_frontmatter(content)
    body_html = render_markdown(body)

    recall_count, last_recalled = _recall_for_path(resolved_abs)

    history: list[dict[str, Any]] = []
    try:
        history = git_tools.history(rel, 20, detail="summary") or []
    except Exception:
        history = []

    rows = build_provenance(
        file_path=rel,
        frontmatter=metadata,
        history=history,
        recall_count=recall_count,
        last_recalled=last_recalled,
        content_hash_mismatch=False,  # P0: no content-hash check wired yet.
    )

    category = metadata.get("category") or _category_from_path(rel)
    mem_type = metadata.get("type")
    slug = Path(rel).stem
    title = (
        metadata.get("title")
        or metadata.get("name")
        or _first_heading(body)
        or slug
    )
    kicker = " · ".join(
        p for p in [mem_type, "core memory" if metadata.get("core") else "memory"] if p
    )

    extra_chips: list[dict[str, str]] = []
    if metadata.get("priority") is not None:
        extra_chips.append({"label": "priority", "value": str(metadata["priority"])})
    if metadata.get("status"):
        extra_chips.append({"label": "status", "value": str(metadata["status"])})

    ctx = _page_context()
    ctx.update(
        {
            "active": "memory",
            "memory_id": metadata.get("id") or rel.removesuffix(".md"),
            "title": title,
            "kicker": kicker or "memory",
            "category": category,
            "slug": slug,
            "confidence": metadata.get("confidence"),
            "recall_count": recall_count,
            "extra_chips": extra_chips,
            "body_html": body_html,
            "rows": rows,
            "broken_seal": False,  # P0: verified state; data-driven flip is wired.
        }
    )
    return templates.TemplateResponse(request, "fact.html", ctx)


@router.get("/history/{file_path:path}", response_class=HTMLResponse, name="ui_history")
def ui_history(request: Request, file_path: str) -> HTMLResponse:
    """Minimal commit-history view (target of the Saved-commit link).

    Kept intentionally thin in P0 — it routes back to the fact view; the rich
    diff/timeline view is Phase 1. Redirect-style: render the fact page so the
    Saved link is not a dead end.
    """
    return ui_memory(request, file_path)


# ── Capability adapters for the P1 views ────────────────────────────────────
def _search_memory(query: str) -> list[dict[str, Any]]:
    """Run the existing search capability in-process and return result rows.

    Calls ``search_api`` (the same handler the JSON ``/search`` endpoint uses)
    so the UI inherits its embedding + hybrid ranking with zero new logic. Any
    backend failure (embedder down, circuit open) propagates to ``run_search``,
    which renders a soft banner rather than 500-ing the page.
    """
    from palinode.api.routers.search import SearchRequest, search_api

    req = SearchRequest(query=query, limit=25)
    return list(search_api(req, request=None) or [])


def _history_files() -> list[str]:
    """List the ``-history.md`` consolidation-audit siblings on disk (rel paths)."""
    import glob

    base = os.path.realpath(config.memory_dir)
    found: list[str] = []
    for filepath in glob.glob(os.path.join(base, "**/*-history.md"), recursive=True):
        try:
            if os.path.commonpath([base, os.path.realpath(filepath)]) != base:
                continue
        except ValueError:
            continue
        found.append(os.path.relpath(filepath, base))
    return found


# ── Small path helpers ─────────────────────────────────────────────────────────
def _rel_path(abs_path: str) -> str:
    """Best-effort memory-relative path from an absolute chunk path."""
    if not abs_path:
        return ""
    try:
        return str(Path(abs_path).relative_to(Path(config.memory_dir)))
    except ValueError:
        return os.path.basename(abs_path)


def _category_from_path(rel: str) -> str:
    parts = Path(rel).parts
    return parts[0] if len(parts) > 1 else ""


def _strip_frontmatter(content: str) -> str:
    """Return the markdown body with YAML frontmatter removed.

    Mirrors ``core.lint``: use the ``frontmatter`` library and fall back to the
    raw content if parsing fails (never raises).
    """
    try:
        import frontmatter as _frontmatter

        return _frontmatter.loads(content).content
    except Exception:
        return content


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or None
    return None
