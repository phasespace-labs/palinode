from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from palinode.api._util import _safe_500
from palinode.core.path_guard import PathTraversalError

router = APIRouter()


class ConsolidateRequest(BaseModel):
    dry_run: bool = False
    nightly: bool = False
    #: Directories under the memory dir to consolidate. ``None`` means
    #: ``daily/`` alone, which is what this endpoint did unconditionally
    #: before — hardcoding that scan left the deterministic executor
    #: unreachable for typed memories saved through /save or MCP.
    sources: list[str] | None = None
    #: Apply the activity gate (``consolidation.auto_gate``) to this call.
    #: Off by default: a caller who asked for a pass has already made the
    #: decision the gate exists to make. On, for a scheduler that wants the
    #: same policy the cron entry point applies.
    respect_gate: bool = False


@router.post("/consolidate")
def consolidate_api(req: ConsolidateRequest = None) -> dict[str, Any]:
    """Run a manual consolidation pass.

    Normally runs as a weekly cron, but can be triggered manually
    for testing or after a busy week.

    With ``respect_gate`` set and the gate unmet, returns
    ``{"status": "deferred", "gate": {...}}`` rather than an error — a
    deferral is a normal outcome of asking, not a failed request.
    """
    from palinode.consolidation import activity_gate
    from palinode.consolidation.runner import run_consolidation, run_nightly
    from palinode.consolidation.run_lock import ConsolidationAlreadyRunning

    req = req or ConsolidateRequest()
    if req.respect_gate:
        decision = activity_gate.evaluate("nightly" if req.nightly else "weekly")
        if not decision.should_run:
            return {"status": "deferred", "gate": decision.as_dict()}
    try:
        if req.nightly:
            result = run_nightly(dry_run=req.dry_run)
        else:
            result = run_consolidation(dry_run=req.dry_run, sources=req.sources)
        return result
    except ConsolidationAlreadyRunning as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except Exception as e:
        raise _safe_500(e, "Consolidation failed")


class ArchiveExpiredRequest(BaseModel):
    dry_run: bool = False


@router.post("/archive-expired")
def archive_expired_api(req: ArchiveExpiredRequest = None) -> dict[str, Any]:
    """Archive ephemeral memories whose `expires_at` has passed (ADR-015 §2.3, the
TTL/auto-archive work).

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
    """Retire one named memory on demand — ARCHIVE, or SUPERSEDE.

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
    except PathTraversalError as e:
        # Same split as every other path-guarded route: 400 for malformed
        # input (null byte), 403 for a path that resolves outside
        # memory_dir. This used to be a blanket 400 that echoed the legacy
        # guard's path-bearing message.
        status_code = 400 if e.malformed else 403
        raise HTTPException(status_code=status_code, detail="Invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        raise _safe_500(e, "Archive failed")


class RestoreRequest(BaseModel):
    file_path: str
    reason: str | None = None


@router.post("/restore")
def restore_api(req: RestoreRequest) -> dict[str, Any]:
    """Bring one archived memory back into default recall — the inverse of
    `/archive` for every archive path (on-demand, forget-driven, TTL,
    consolidation).

    Flips `status` back to `active`, drops `superseded_by`, records
    `restored_at` / `restored_from` provenance, appends a history line,
    propagates the status to the chunk index, and commits both files.
    Retraction markers and triggers are not resurrected. Idempotent: a memory
    that is not archived is reported unchanged.
    """
    from palinode.consolidation.archive import restore_memory

    try:
        return restore_memory(req.file_path, reason=req.reason)
    except PathTraversalError as e:
        status_code = 400 if e.malformed else 403
        raise HTTPException(status_code=status_code, detail="Invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        raise _safe_500(e, "Restore failed")


class UnretractRequest(BaseModel):
    file_path: str
    pref: str
    reason: str | None = None


@router.post("/unretract")
def unretract_api(req: UnretractRequest) -> dict[str, Any]:
    """Withdraw one pref's mention-level retraction from one memory.

    Un-strikes every span carrying the pref's retraction marker, removes the
    pref from the file's `retracted_prefs` record, appends a history line,
    commits, and re-indexes. The file's `status` is never changed. Idempotent:
    a pref not in the record is reported unchanged.
    """
    from palinode.consolidation.retract import unretract_mentions

    try:
        return unretract_mentions(req.file_path, req.pref, reason=req.reason)
    except PathTraversalError as e:
        status_code = 400 if e.malformed else 403
        raise HTTPException(status_code=status_code, detail="Invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        raise _safe_500(e, "Unretract failed")


class ForgetWithdrawRequest(BaseModel):
    file_path: str
    reason: str | None = None


@router.post("/forget-withdraw")
def forget_withdraw_api(req: ForgetWithdrawRequest) -> dict[str, Any]:
    """Take a forget request back: restore what it archived, un-strike what it
    retracted, and archive the request record(s) so they stop acting as
    tombstones. `file_path` names the forget-request memory; 409 when the
    memory carries no forget request.
    """
    from palinode.consolidation.forget import (
        NotAForgetRequest,
        withdraw_forget_request,
    )

    try:
        return withdraw_forget_request(req.file_path, reason=req.reason)
    except PathTraversalError as e:
        status_code = 400 if e.malformed else 403
        raise HTTPException(status_code=status_code, detail="Invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except NotAForgetRequest:
        raise HTTPException(
            status_code=409, detail="Memory carries no forget request"
        )
    except Exception as e:
        raise _safe_500(e, "Forget-withdraw failed")


@router.post("/split-layers")
def split_layers_api() -> dict[str, Any]:
    """Split core files into Identity/Status/History layers."""
    from palinode.consolidation.layer_split import split_all_core_files
    stats = split_all_core_files()
    return stats


class BootstrapFactIdsRequest(BaseModel):
    #: One memory file to tag, relative to the store
    #: (``projects/palinode-status.md``). ``None`` keeps the original whole-store
    #: walk. Present because the case that needs tagging is usually a single
    #: inert consolidation target named by ``doctor``, not the whole store.
    file: str | None = None


@router.post("/bootstrap-fact-ids")
def bootstrap_fact_ids_api(
    req: BootstrapFactIdsRequest = None,
) -> dict[str, Any]:
    """Add fact IDs to memory files — the whole store, or one named file.

    ``{"file": "projects/foo-status.md"}`` tags exactly that file; a bodyless
    POST keeps the default whole-store walk over
    ``people/``/``projects/``/``decisions/``/``insights/``. Both commit with the
    same provenance. The path is guarded: absolute paths, ``../`` traversal and
    symlink escapes are rejected before the file is read.
    """
    from palinode.consolidation.fact_ids import (
        bootstrap_all_fact_ids,
        bootstrap_fact_ids_for_file,
    )

    req = req or BootstrapFactIdsRequest()
    if req.file:
        try:
            return bootstrap_fact_ids_for_file(req.file)
        except PathTraversalError as e:
            status_code = 400 if e.malformed else 403
            raise HTTPException(status_code=status_code, detail="Invalid path")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found")
        except Exception as e:
            raise _safe_500(e, "Bootstrap fact ids failed")
    return bootstrap_all_fact_ids()
