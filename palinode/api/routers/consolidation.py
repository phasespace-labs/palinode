from __future__ import annotations

from typing import Any

from fastapi import APIRouter
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
