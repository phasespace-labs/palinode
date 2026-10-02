from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from palinode.api._util import _retrieval_logger
from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.path_guard import PathTraversalError

router = APIRouter()
logger = logging.getLogger("palinode.api")


def _http_path_error(exc: PathTraversalError) -> HTTPException:
    """Map the shared path guard's typed error onto an ``HTTPException``.

    Same split ``api/path_safety.py`` uses: 400 for input that's malformed on
    its face (a null byte), 403 for a syntactically valid path that resolves
    outside ``memory_dir``. Before this router's guards were unified, its
    handlers either propagated the legacy guard's ``ValueError`` uncaught
    (``/history``, ``/rollback``, plain ``/blame`` — a 500, not a rejection)
    or caught it and echoed its path-bearing message at 400 (the ``claims``
    branch of ``/blame`` and ``/trace``). One guard, one mapping, applied
    everywhere a ``file_path`` reaches this router.
    """
    status_code = 400 if exc.malformed else 403
    return HTTPException(status_code=status_code, detail="Invalid path")


@router.get("/history/{file_path:path}")
def history_api(
    file_path: str,
    limit: int = 20,
    detail: str = "summary",
) -> dict[str, Any]:
    """Get the change history for a memory file.

    Uses --follow to track renames and includes diff stats per commit.

    ``detail="full"`` additionally includes the unified diff body per commit
    (commit-level evolution view).
    """
    if detail not in ("summary", "full"):
        raise HTTPException(status_code=422, detail="detail must be 'summary' or 'full'")
    try:
        commits = git_tools.history(file_path, limit, detail=detail)
    except PathTraversalError as exc:
        raise _http_path_error(exc)
    if not commits:
        # Distinguish "file not found" from "no history"
        import os as _os
        full_path = _os.path.join(config.memory_dir, file_path)
        if not _os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")

    # Issue history access is an explicit retrieval.
    _retrieval_logger.record_file_read(
        file_path,
        source="palinode_history",
        mode="explicit",
    )
    return {"file": file_path, "history": commits}


@router.get("/diff")
def diff_api(days: int = 7, paths: str | None = None) -> dict[str, Any]:
    """Show memory changes in the last N days, optionally filtered by paths."""
    path_list = paths.split(",") if paths else None
    return {"diff": git_tools.diff(days, path_list)}


@router.get("/blame/{file_path:path}")
def blame_api(file_path: str, search: str | None = None, claims: bool = False) -> dict[str, Any]:
    """Show when each line of a memory file was last changed.

    With ``claims=true``, additionally resolves each of the file's ``claims:``
    anchors to its cited source span — answering "which source span justifies
    this claim," not just "when was this line written" — returned as a
    structured ``claims`` list alongside the blame text. Each resolution
    carries the live span integrity status (ok / anchor_tampered /
    source_drifted / source_missing), whether the stored claim_id still
    matches its content-addressed derivation, and whether the cited source is
    declared in the memory's ``sources:`` anchors.
    """
    # Issue blame access is an explicit retrieval.
    _retrieval_logger.record_file_read(
        file_path,
        source="palinode_blame",
        mode="explicit",
    )
    try:
        result: dict[str, Any] = {"blame": git_tools.blame(file_path, search)}
    except PathTraversalError as exc:
        raise _http_path_error(exc)
    if claims:
        from palinode.core.claims import resolve_memory_claims

        try:
            safe_rel = git_tools._resolve_memory_path(file_path)
            result["claims"] = resolve_memory_claims(safe_rel, config.memory_dir)
        except PathTraversalError as exc:
            raise _http_path_error(exc)
        except FileNotFoundError:
            # blame already reports the missing file in its text output.
            result["claims"] = []
    return result


@router.get("/trace/{file_path:path}")
def trace_api(file_path: str) -> dict[str, Any]:
    """Compose the full provenance lineage for one memory file.

    The consumer that joins every provenance primitive — source-citation anchors (G1),
    git blame/history, the supersession trail, typed ``contradicts`` / ``backed_by``
    links (G4), and the retrieval log — into one lineage object. Rows whose provenance
    gap is not built yet (G2 extraction metadata, G3 terminal edge) render an honest
    ``not_captured`` placeholder. The JSON is the structured object the review UI
    consumes. """
    from palinode.core.trace import compose_trace

    try:
        safe_rel = git_tools._resolve_memory_path(file_path)
    except PathTraversalError as exc:
        raise _http_path_error(exc)
    try:
        trace = compose_trace(safe_rel, config.memory_dir)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

    # Composing a trace is an explicit retrieval. Log the CANONICAL ref that
    # compose resolved (``trace["file"]``), not the raw request path:
    # _resolve_memory_path validates but returns its input verbatim, so a
    # request for `./decisions/x.md` would otherwise log an event that a later
    # trace of the same memory could never match — silently undercounting the
    # recall this feature exists to surface.
    _retrieval_logger.record_file_read(
        trace["file"],
        source="palinode_trace",
        mode="explicit",
    )
    return trace


@router.post("/rollback")
def rollback_api(
    file_path: str,
    commit: str | None = None,
    dry_run: bool = True,
    undo_retirements: bool = False,
) -> dict[str, Any]:
    """Revert a memory file to a previous version.

    Defaults to dry_run=True for safety. Set dry_run=False to actually revert.
    A rollback that would undo a retirement is named in the preview and
    refused (``status: refused``, nothing written) unless
    ``undo_retirements=true``; an acknowledged one reports
    ``status: undid_retirements`` and names each record in ``resurrected``.
    """
    try:
        report = git_tools.rollback_report(
            file_path, commit, dry_run, undo_retirements
        )
    except PathTraversalError as exc:
        raise _http_path_error(exc)
    if report["resurrected"]:
        # The file is current again; make the index say so now rather than
        # whenever the watcher gets to it, so search agrees with resolve and
        # prime from the moment this call returns.
        import os as _os

        from palinode.indexer.index_file import index_file

        try:
            outcome = index_file(_os.path.join(config.memory_dir, file_path))
            report["index_converged"] = bool(outcome.get("indexed"))
        except Exception:  # noqa: BLE001 — the watcher converges it later
            logger.warning(
                "rollback reindex failed op=rollback file_path=%s", file_path,
                exc_info=True,
            )
            report["index_converged"] = False
    return report


@router.post("/push")
def push_api() -> dict[str, Any]:
    """Push memory changes to the remote repository."""
    return {"result": git_tools.push()}


@router.get("/git-stats")
def git_stats_api(days: int = 7) -> dict[str, Any]:
    """Get commit statistics for the memory repo."""
    return git_tools.commit_count(days)
