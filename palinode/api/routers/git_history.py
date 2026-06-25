from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from palinode.api._util import _retrieval_logger
from palinode.core import git_tools
from palinode.core.config import config

router = APIRouter()


@router.get("/history/{file_path:path}")
def history_api(
    file_path: str,
    limit: int = 20,
    detail: str = "summary",
) -> dict[str, Any]:
    """Get the change history for a memory file.

    Uses --follow to track renames and includes diff stats per commit.

    ``detail="full"`` additionally includes the unified diff body per commit
    (commit-level evolution view, formerly the /timeline endpoint).
    """
    if detail not in ("summary", "full"):
        raise HTTPException(status_code=422, detail="detail must be 'summary' or 'full'")
    commits = git_tools.history(file_path, limit, detail=detail)
    if not commits:
        # Distinguish "file not found" from "no history"
        import os as _os
        full_path = _os.path.join(config.memory_dir, file_path)
        if not _os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")

    # Issue #256: history access is an explicit retrieval.
    _retrieval_logger.record_file_read(
        file_path,
        source="palinode_history",
        mode="explicit",
    )
    return {"file": file_path, "history": commits}


@router.get("/timeline/{file_path:path}")
def timeline_api(
    request: Request,
    file_path: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Deprecated: use GET /history/{file_path}?detail=full instead.

    Kept for one release cycle for backward compatibility.  Returns the same
    response as /history?detail=full with a ``Deprecation`` response header.
    """
    from fastapi.responses import JSONResponse as _JSONResponse
    import logging as _logging
    _logging.getLogger("palinode.api").warning(
        "GET /timeline is deprecated — use GET /history/%s?detail=full", file_path
    )
    commits = git_tools.history(file_path, limit, detail="full")
    if not commits:
        import os as _os
        full_path = _os.path.join(config.memory_dir, file_path)
        if not _os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")
    body = {"file": file_path, "history": commits}
    return _JSONResponse(
        content=body,
        headers={"Deprecation": "true", "Link": f'</history/{file_path}?detail=full>; rel="successor-version"'},
    )


@router.get("/diff")
def diff_api(days: int = 7, paths: str | None = None) -> dict[str, Any]:
    """Show memory changes in the last N days, optionally filtered by paths."""
    path_list = paths.split(",") if paths else None
    return {"diff": git_tools.diff(days, path_list)}


@router.get("/blame/{file_path:path}")
def blame_api(file_path: str, search: str | None = None) -> dict[str, Any]:
    """Show when each line of a memory file was last changed."""
    # Issue #256: blame access is an explicit retrieval.
    _retrieval_logger.record_file_read(
        file_path,
        source="palinode_blame",
        mode="explicit",
    )
    return {"blame": git_tools.blame(file_path, search)}


@router.post("/rollback")
def rollback_api(file_path: str, commit: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Revert a memory file to a previous version.

    Defaults to dry_run=True for safety. Set dry_run=False to actually revert.
    """
    return {"result": git_tools.rollback(file_path, commit, dry_run)}


@router.post("/push")
def push_api() -> dict[str, Any]:
    """Push memory changes to the remote repository."""
    return {"result": git_tools.push()}


@router.get("/git-stats")
def git_stats_api(days: int = 7) -> dict[str, Any]:
    """Get commit statistics for the memory repo."""
    return git_tools.commit_count(days)
