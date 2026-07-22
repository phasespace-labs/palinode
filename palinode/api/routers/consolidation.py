from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from palinode.api._util import _safe_500

router = APIRouter()


class ConsolidateRequest(BaseModel):
    dry_run: bool = False
    nightly: bool = False


@router.post("/consolidate")
def consolidate_api(req: ConsolidateRequest = None) -> dict[str, Any]:
    """Run a manual consolidation pass.

    Normally runs as a weekly cron, but can be triggered manually
    for testing or after a busy week.
    """
    from palinode.consolidation.runner import run_consolidation, run_nightly

    req = req or ConsolidateRequest()
    try:
        if req.nightly:
            result = run_nightly(dry_run=req.dry_run)
        else:
            result = run_consolidation(dry_run=req.dry_run)
        return result
    except Exception as e:
        raise _safe_500(e, "Consolidation failed")


class ArchiveExpiredRequest(BaseModel):
    dry_run: bool = False


@router.post("/archive-expired")
def archive_expired_api(req: ArchiveExpiredRequest = None) -> dict[str, Any]:
    """Archive ephemeral memories whose `expires_at` has passed (ADR-015 §2.3, #482).

    Deterministic, idempotent sweep. `dry_run=true` reports what would be
    archived without writing. Intended for cron / the monitor harness.
    """
    from palinode.consolidation.ttl import archive_expired
    req = req or ArchiveExpiredRequest()
    try:
        return archive_expired(dry_run=req.dry_run)
    except Exception as e:
        raise _safe_500(e, "Archive-expired sweep failed")


class ArchiveRequest(BaseModel):
    file_path: str
    reason: str | None = None
    superseded_by: str | None = None


@router.post("/archive")
def archive_api(req: ArchiveRequest) -> dict[str, Any]:
    """Retire one named memory on demand — ARCHIVE, or SUPERSEDE (#664).

    Sets `status: archived` (plus `superseded_by` when a replacement is named),
    appends the reason to the `{base}-history.md` audit sibling, propagates the
    status to the chunk index so the memory leaves default recall, and commits
    both files. Idempotent: an already-archived memory is reported unchanged.
    """
    from palinode.consolidation.archive import archive_memory

    try:
        return archive_memory(
            req.file_path,
            reason=req.reason,
            superseded_by=req.superseded_by,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        raise _safe_500(e, "Archive failed")


@router.post("/split-layers")
def split_layers_api() -> dict[str, Any]:
    """Split core files into Identity/Status/History layers."""
    from palinode.consolidation.layer_split import split_all_core_files
    stats = split_all_core_files()
    return stats


@router.post("/bootstrap-fact-ids")
def bootstrap_fact_ids_api() -> dict[str, Any]:
    """Add fact IDs to all memory files."""
    from palinode.consolidation.fact_ids import bootstrap_all_fact_ids
    stats = bootstrap_all_fact_ids()
    return stats
