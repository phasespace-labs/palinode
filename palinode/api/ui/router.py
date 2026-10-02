"""FastAPI router for the local read-only provenance UI (Phases 0–1).

Mounted on the existing app under ``/ui`` — no new service, no build step.
Server-rendered HTML via Jinja2; CSS shipped in-package. The router is a pure
client of existing capabilities:

  - visible files + selected lint / chunk counts → dashboard health summary
  - visible indexed files                         → dashboard recent list
  - ``list_api`` (file scan) + ``search_api``      → memory list + search (P1)
  - ``git_tools.recent_commits`` + ``diff``        → diffs / compaction (P1)
  - ``_resolve_memory_path`` + ``parser``          → fact body + frontmatter
  - ``git_tools.history``                          → git lineage / Saved commit
  - chunk recall_count (read-only query)           → retrieval stats

No mutations, no new business logic. Loopback-only: ``_loopback_guard``
refuses to render when the API host resolves to a non-loopback address,
reusing the same resolved bind host the API startup gate keys on
(``PALINODE_API_HOST``); neither ``PALINODE_API_BIND_INTENT=public`` nor
``PALINODE_API_ALLOW_UNAUTH=1`` lifts it.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from palinode.core import git_tools, store
from palinode.core.config import config
from palinode.core.explain import (
    DEFAULT_MAX_RECORDS,
    MAX_RECORDS_CEILING,
    InvalidBundleId,
    QueryAccess,
    explain_delivery,
    format_field,
    validate_bundle_id,
)
from palinode.core.parser import parse_markdown, split_frontmatter

from palinode.api.path_safety import _resolve_memory_path
from palinode.api.ui.provenance import build_provenance
from palinode.api.ui.render import render_markdown
from palinode.api.ui.discovery import (
    discovery_lint,
    discovery_visibility,
    indexed_discovery,
    visible_commit_count,
)
from palinode.api.ui.views import (
    build_compaction_view,
    build_correction_panel,
    build_diffs_view,
    build_memory_list,
    build_quality_view,
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


def _bind_host() -> str:
    """The resolved API bind host — ``PALINODE_API_HOST``, else the config.

    The same resolution ``server.py`` uses for its startup bind gate.
    """
    return os.environ.get("PALINODE_API_HOST", config.services.api.host)


def bind_is_loopback() -> bool:
    """Is the API bound where only this machine can reach it?

    The predicate behind :func:`_loopback_guard`, exposed because a second
    caller needs the same question answered. ``GET /explain`` serves the
    diagnostics view — the caller's own query prose — on exactly this test, so
    it must be *this* test and not a lookalike: a JSON route that disagreed
    with the inspector about what "local" means is precisely the drift that
    would hand one surface's refusal to another surface's caller.
    """
    return _host_is_loopback(_bind_host())


def _loopback_guard() -> None:
    """Refuse to serve the UI on a non-loopback bind.

    Reuses the API's bind-intent signal (see :func:`_bind_host`). Unlike the
    API, the UI hard-refuses a non-loopback bind even with a configured token.
    The app's bearer middleware also protects UI requests when enabled; this
    extra bind restriction is independent of deployment authentication.
    Neither ``PALINODE_API_BIND_INTENT=public`` nor
    ``PALINODE_API_ALLOW_UNAUTH=1`` lifts it.
    """
    if not bind_is_loopback():
        host = _bind_host()
        raise HTTPException(
            status_code=403,
            detail=(
                "Palinode UI is loopback-only — refusing to serve on a "
                f"non-loopback bind ({host}). Set PALINODE_API_HOST=127.0.0.1."
            ),
        )


# ── Shared template context ──────────────────────────────────────────────────
def _page_context() -> dict[str, Any]:
    """Build one visible discovery snapshot for the shell and page contents."""
    memories = scan_memory_files()
    lint = discovery_lint(memories)
    quality = build_quality_view(lint)
    counts = {q["key"]: len(q["rows"]) for q in quality["queues"]}
    total_chunks, recent = indexed_discovery(memories)
    total_memories = len(memories)

    return {
        "memory_rows": memories,
        "quality": quality,
        "recent": recent,
        "total_memories": total_memories,
        "total_chunks": total_chunks,
        "unindexed": total_memories > 0 and total_chunks == 0,
        "palinode_dir": config.memory_dir,
        "api_port": config.services.api.port,
        "stale_count": counts["stale"],
        "orphaned_count": counts["orphaned"],
        "missing_descriptions": counts["missing_description"],
        "contradictions": counts["contradictions"],
        "core_count": sum(bool(r["core"]) for r in memories),
        "nav_quality_count": quality["total"] - counts["no_extraction_meta"],
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

    ctx["git_commits_7d"] = visible_commit_count(ctx["memory_rows"])

    ctx["active"] = "dashboard"
    return templates.TemplateResponse(request, "dashboard.html", ctx)


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
        ctx["memory_rows"],
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
    ctx = _page_context()
    ctx["active"] = "quality"
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
        or _filename_title(rel)
    )
    kicker = " · ".join(
        p for p in [mem_type, "core memory" if metadata.get("core") else "memory"] if p
    )

    extra_chips: list[dict[str, str]] = []
    if metadata.get("priority") is not None:
        extra_chips.append({"label": "priority", "value": str(metadata["priority"])})
    if metadata.get("status"):
        extra_chips.append({"label": "status", "value": str(metadata["status"])})

    # A direct read of a hidden record is served, labelled, and offered no
    # mutation (see palinode.api.ui.discovery for the rule). All three come
    # from the one visibility verdict.
    visibility = discovery_visibility(rel, metadata=metadata)

    ctx = _page_context()
    ctx.update(
        {
            "active": "memory",
            "visibility": visibility,
            "correction": build_correction_panel(
                rel,
                visibility,
                preview=lambda: _correction_preview(rel),
                candidates=lambda: _correction_candidates(rel),
            ),
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


@router.get("/delivery/{bundle_id}", response_class=HTMLResponse, name="ui_delivery")
def ui_delivery(request: Request, bundle_id: str, limit: int = DEFAULT_MAX_RECORDS) -> HTMLResponse:
    """Read-only explanation of one delivery, addressed by its receipt reference.

    The operator's view of what an agent was handed: it asks for the
    diagnostics view, so the caller's own query prose is shown here and nowhere
    an agent can reach. That is safe for exactly the reason the whole UI is —
    ``_loopback_guard`` refuses a non-loopback bind, so this is a person reading
    their own store on their own machine. ``GET /explain`` gates the same two
    fields on the same predicate (:func:`bind_is_loopback`), so the page and
    the JSON route disclose the same thing under the same conditions.

    Visibility filtering still applies with no scope identity (``chain=None``):
    private and restricted records are counted, never named. The page renders
    through Jinja2 with autoescaping on, which is what keeps a memory ref or a
    recorded query containing markup from being markup.
    """
    _loopback_guard()
    try:
        safe_id = validate_bundle_id(bundle_id)
    except InvalidBundleId as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    explanation = explain_delivery(
        safe_id,
        memory_dir=config.memory_dir,
        chain=None,
        # The same gate ``GET /explain`` applies, evaluated rather than
        # assumed: ``_loopback_guard`` above has already refused every request
        # this could be false for, so the page shows the query exactly when the
        # JSON route would — and stops showing it if that guard ever changes.
        query_access=QueryAccess(requested=True, loopback=bind_is_loopback()),
        max_records=max(1, min(limit, MAX_RECORDS_CEILING)),
    )
    ctx = _page_context()
    ctx.update(
        {
            "active": "memory",
            "explanation": explanation,
            "field": format_field,
            "project_text": _project_text(explanation),
            "coverage_text": _coverage_text(explanation),
            "scope_text": _scope_text(explanation),
        }
    )
    return templates.TemplateResponse(request, "delivery.html", ctx)


def _scope_text(explanation: dict[str, Any]) -> str:
    """The scope line. An empty chain is a resolved answer, not a blank cell."""
    delivery = explanation.get("delivery") or {}
    scope = delivery.get("scope")
    if isinstance(scope, list) and not scope:
        return "no scope identity (access control only)"
    return format_field(scope)


def _project_text(explanation: dict[str, Any]) -> str:
    """The project line, pre-composed so the template holds no field logic."""
    delivery = explanation.get("delivery") or {}
    project = delivery.get("project")
    if not isinstance(project, dict) or project.get("available") is False:
        return format_field(project)
    return f"{project['value']} (resolved by: {format_field(project['resolved_by'])})"


def _coverage_text(explanation: dict[str, Any]) -> str:
    """The coverage line: status plus its qualifiers, or why there is none."""
    delivery = explanation.get("delivery") or {}
    coverage = delivery.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("available") is False:
        return format_field(coverage)
    reasons = ", ".join(coverage.get("reasons") or []) or "no qualifiers"
    return f"{coverage.get('status', '?')} — {reasons}"


@router.get("/history/{file_path:path}", response_class=HTMLResponse, name="ui_history")
def ui_history(request: Request, file_path: str) -> HTMLResponse:
    """Read-only Git timeline for one memory (target of the Saved link)."""
    _loopback_guard()

    # Keep the detail route's convenience suffix and its traversal/symlink
    # boundary before passing a user-supplied path to Git.
    candidates = [file_path]
    if not file_path.endswith(".md"):
        candidates.append(f"{file_path}.md")
    rel = ""
    for candidate in candidates:
        _, resolved_candidate = _resolve_memory_path(candidate)
        if os.path.isfile(resolved_candidate):
            rel = candidate
            break
    if not rel:
        raise HTTPException(status_code=404, detail="Memory not found")

    history_unavailable = False
    try:
        history = git_tools.history(rel, 20, detail="summary") or []
    except Exception:
        # A Git failure is materially different from a file that was never
        # committed. Keep the page read-only, but make that uncertainty clear.
        history = []
        history_unavailable = True

    ctx = _page_context()
    ctx.update(
        {
            "active": "memory",
            "file_path": rel,
            "filename": _filename_title(rel),
            "history": history,
            "history_unavailable": history_unavailable,
            "history_limit": 20,
            # The label, identically to ui_memory: the timeline of a hidden
            # record is readable and says so.
            "visibility": discovery_visibility(rel),
        }
    )
    return templates.TemplateResponse(request, "history.html", ctx)


# ── Capability adapters for the P1 views ────────────────────────────────────
def _correction_preview(rel: str) -> dict[str, Any]:
    """Build the read-only half of the correction contract for one record.

    Calls :func:`palinode.corrections.review.preview_correction` in process —
    the same function the CLI, API and MCP preview call — with ``retire`` as
    the action, because a preview needs no replacement text to report the
    record's revision, its policies and what references it. No write path is
    reachable from here.
    """
    from palinode.corrections.review import ACTION_RETIRE, preview_correction

    return preview_correction(target=rel, action=ACTION_RETIRE)


def _correction_candidates(rel: str) -> list[dict[str, Any]]:
    """Queued correction candidates whose own span quotes text in this record."""
    from palinode.corrections.review import candidates_naming

    return candidates_naming(rel)



def _search_memory(query: str) -> dict[str, Any]:
    """Run the existing search capability in-process and return result rows.

    Calls ``search_api`` (the same handler the JSON ``/search`` endpoint uses)
    so the UI inherits its embedding + hybrid ranking with zero new logic. Any
    backend failure (embedder down, circuit open) propagates to ``run_search``,
    which renders a soft banner rather than 500-ing the page.
    """
    from palinode.api.routers.search import SearchRequest, search_api

    req = SearchRequest(query=query, limit=25, receipt=True)
    return search_api(req, request=None)


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

    Delegates to the canonical, lossless splitter
    (:func:`palinode.core.parser.split_frontmatter`) — the single source of
    truth for frontmatter/body separation, not the ``frontmatter`` library or
    an ad hoc string split. Never raises: an unparseable/absent frontmatter
    block degrades to returning the whole content unchanged.
    """
    _, body = split_frontmatter(content)
    return body


def _first_heading(body: str) -> str | None:
    lines = body.splitlines()
    for index, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
            if not heading:
                continue
            # The save pipeline appends this generated footer. It is a useful
            # navigational section, never the memory's user-authored title.
            following = next((item.strip() for item in lines[index + 1:] if item.strip()), "")
            if heading.casefold() == "see also" and following == "<!-- palinode-auto-footer -->":
                continue
            return heading
    return None


def _filename_title(rel: str) -> str:
    """Turn a heading-less filename into a readable, stable page title."""
    stem = Path(rel).stem.replace("-", " ").replace("_", " ").strip()
    return stem or rel
