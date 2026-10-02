"""Maintenance, ingest, reindex, entity, lint, migrate, and depends routes (Stage 3 of
the router split).

Extracted from palinode/api/server.py:
  POST /ingest
  POST /ingest-url
  POST /rebuild-fts
  POST /reindex
  GET /entities/{entity_ref:path}
  GET /entities
  POST /lint
  POST /corrections
  POST /corrections/preview
  POST /corrections/apply
  POST /corrections/dismiss
  POST /corrections/undo
  POST /migrate/openclaw
  GET /depends/_unblocked
  GET /depends/{slug:path}
"""
from __future__ import annotations

import glob
import logging
import os
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from palinode.core import store
from palinode.core.config import config
from palinode.core.parity import CORRECTION_ACTIONS
from palinode.core.path_guard import to_rel_path

from palinode.api._util import _reindex_lock, _reindex_state, _safe_500, _utc_now
from palinode.api.path_safety import _memory_base_dir

logger = logging.getLogger("palinode.api")
router = APIRouter()


@router.post("/ingest")
def ingest_api() -> dict[str, str]:
    """Invoke document drop-box scanning routine."""
    from palinode.ingest.pipeline import process_inbox
    try:
        process_inbox()
        return {"status": "success"}
    except Exception as e:
        raise _safe_500(e, "Ingestion failed")


@router.post("/ingest-url")
def ingest_url_api(req: dict[str, str]) -> dict[str, str]:
    """Direct fetch and parse of an active hypertext url.

    Args:
        req (dict[str, str]): A standard dict providing "url" values.
    """
    from palinode.ingest.pipeline import ingest_url, is_safe_url
    url = req.get("url", "")
    name = req.get("name", url.split("/")[-1][:30])
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    if not is_safe_url(url):
        raise HTTPException(status_code=400, detail="Invalid or unsafe URL provided (SSRF protection)")
    try:
        result = ingest_url(url, name)
        if result:
            return {"status": "success", "file_path": result, "rel_path": to_rel_path(result)}
        return {"status": "no_content"}
    except Exception as e:
        raise _safe_500(e, "URL ingestion failed")


@router.post("/rebuild-fts")
def rebuild_fts_api() -> dict[str, Any]:
    """Rebuild the FTS5 full-text search index from existing chunks.

    Run this once after upgrading to hybrid search, or if the FTS5
    index gets out of sync with the chunks table.
    """
    logger.info("Rebuilding FTS5 index...")
    count = store.rebuild_fts()
    logger.info(f"FTS5 rebuild complete: {count} chunks indexed")
    return {"status": "success", "chunks_indexed": count}


@router.post("/reindex")
def reindex_api(since: str | None = None) -> dict[str, Any]:
    """Reindex memory files.  Idempotent — unchanged files are skipped.

    Query params:
        since: ISO timestamp (e.g. '2026-04-09T00:00:00Z').  If provided,
               only files whose mtime is newer than this are processed.
               Without it, all files are visited (but content-hash dedup
               still skips unchanged content).

    Returns 409 if a reindex is already in progress — check /status for
    progress.
    """
    if not _reindex_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="reindex already running — check /status for progress",
        )

    try:
        from palinode.indexer.watcher import PalinodeHandler
        handler = PalinodeHandler()

        since_ts: float | None = None
        if since:
            try:
                dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                since_ts = dt.timestamp()
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid ISO timestamp: {since}")

        files = [
            fp
            for fp in glob.glob(os.path.join(config.palinode_dir, "**/*.md"), recursive=True)
            if handler.is_valid_file(fp)
        ]

        _reindex_state["running"] = True
        _reindex_state["started_at"] = _utc_now().isoformat().replace("+00:00", "Z")
        _reindex_state["files_processed"] = 0
        _reindex_state["total_files"] = len(files)

        logger.info("Starting %s reindex (%d files)...", "incremental" if since_ts else "full", len(files))
        count = 0
        skipped_mtime = 0
        errors = 0
        try:
            for filepath in files:
                if since_ts and os.path.getmtime(filepath) < since_ts:
                    skipped_mtime += 1
                    continue
                try:
                    handler._process_file(filepath)
                    count += 1
                except Exception as e:
                    errors += 1
                    logger.warning(f"Reindex failed for {filepath}: {e}")
                _reindex_state["files_processed"] = count + errors

            gc_paths_removed, gc_chunks_removed = store.gc_orphaned_chunks(files)
            logger.info(
                "reindex GC: %d orphaned paths removed from index (%d chunks)",
                gc_paths_removed,
                gc_chunks_removed,
            )

            # Rebuild FTS5 after bulk reindex to ensure consistency
            fts_count = store.rebuild_fts()
            logger.info(
                f"Reindex complete: {count} processed, {skipped_mtime} skipped (mtime), {errors} errors, FTS5: {fts_count}"
            )
        finally:
            _reindex_state["running"] = False

        return {
            "status": "success",
            "files_reindexed": count,
            "skipped_not_modified": skipped_mtime,
            "errors": errors,
            "gc_paths_removed": gc_paths_removed,
            "gc_chunks_removed": gc_chunks_removed,
            "fts_chunks": fts_count,
        }
    finally:
        _reindex_lock.release()


@router.get("/entities/{entity_ref:path}")
def entity_api(entity_ref: str) -> dict[str, Any]:
    """Get all files referencing an entity."""
    files = store.get_entity_files(entity_ref)
    graph = store.get_entity_graph(entity_ref)
    return {"entity": entity_ref, "files": files, "connected_entities": graph}


@router.get("/entities")
def entities_list_api() -> list[dict[str, Any]]:
    """List all known entities and their file counts."""
    db = store.get_db()
    cursor = db.cursor()
    try:
        cursor.execute("""
            SELECT entity_ref, count(*) as file_count
            FROM entities
            GROUP BY entity_ref
            ORDER BY file_count DESC
        """)
        results = [{"entity": row[0], "files": row[1]} for row in cursor.fetchall()]
    except Exception:
        results = []
    finally:
        db.close()
    return results


@router.post("/lint")
def lint_api(
    propose: bool = False,
    apply: bool = False,
    deep_contradictions: bool = False,
    max_llm_calls: int | None = None,
    similarity_threshold: float | None = None,
) -> dict[str, Any]:
    """Scan memory and report orphans, stale files, and contradictions.

    ``propose=true`` adds a ``proposals`` block: the deterministic findings
    translated into executor operations, each carrying the finding it came from.
    It is a dry run — nothing is written. ``apply=true`` (which implies
    ``propose``) runs the applicable ones through the existing deterministic
    write paths, stamped with an actor of ``lint``.

    ``deep_contradictions=true`` additionally runs the LLM-confirmed semantic
    pass over Decision memories, returns it under ``deep_contradictions``, and
    maps its findings to ``PROPOSE_CONTRADICTS`` proposals. Opt-in because it
    needs the configured embedder and LLM endpoint.
    """
    from palinode.core.lint import run_lint_pass
    report = run_lint_pass()

    deep = None
    if deep_contradictions:
        from palinode.lint.contradictions import (
            DEFAULT_MAX_LLM_CALLS,
            DEFAULT_SIMILARITY_THRESHOLD,
            run_deep_contradiction_check,
        )
        deep = run_deep_contradiction_check(
            similarity_threshold=(
                DEFAULT_SIMILARITY_THRESHOLD
                if similarity_threshold is None
                else similarity_threshold
            ),
            max_llm_calls=(
                DEFAULT_MAX_LLM_CALLS if max_llm_calls is None else max_llm_calls
            ),
        )
        report["deep_contradictions"] = deep

    if not (propose or apply):
        return report

    from palinode.consolidation.propose_from_lint import attach_proposals
    return attach_proposals(report, apply=apply, deep_contradictions=deep)


class ReviewRequest(BaseModel):
    #: Project slug (``"palinode"``) or typed ref (``"project/palinode"``).
    #: When omitted, the whole store is reviewed.
    project: str | None = None


@router.post("/review")
def review_api(req: ReviewRequest) -> dict[str, Any]:
    """Advisory project-memory review: composes the deterministic lint
    signals scoped to a project and proposes corrective ops. Read-only — applies
    nothing."""
    from palinode.core.review import run_review
    return run_review(project=req.project)


class CorrectionsRequest(BaseModel):
    #: Project slug (``"harbor-notes"``) or typed ref; filters the queue.
    project: str | None = None
    #: Only candidates from the last N days. Also narrows a scan's lookback.
    since_days: int | None = None
    #: Run a detection pass over the configured transcripts before listing.
    #: Deliberately absent from the MCP tool, which stays a read-only listing.
    scan: bool = False


@router.post("/corrections")
def corrections_api(req: CorrectionsRequest) -> dict[str, Any]:
    """List transcript-derived correction candidates. Nothing is applied.

    Each candidate is a proposal: a bounded quoted span from a user's own turn,
    its session/turn anchor, its project scope, and a classification. ``scan``
    additionally runs a detection pass over the configured harness transcript
    paths — which does nothing unless the source is enabled in config, and
    honours the capture policy like every other automatic capture source.
    """
    from palinode.corrections.scan import corrections_report

    return corrections_report(
        project=req.project, since_days=req.since_days, scan=req.scan
    )


# ── correction review: preview → apply / dismiss, and undo ──────────────────
#
# One backend contract behind four surfaces. The handlers below are transport
# only: they map :mod:`palinode.corrections.review`'s refusals onto status
# codes and pass everything else straight through. 409 is the code for "the
# caller must look again and decide" — a stale revision, an ambiguous target,
# a target the correction never named — because the request was well-formed
# and the store is not in the state it was previewed in. An apply that got one
# of its two writes in carries the same code and is told apart by its payload:
# a refusal carries `error`, a partial carries `applied: "partial"`.


class CorrectionPreviewRequest(BaseModel):
    #: Memory ref to correct: ``decisions/x.md``, ``decisions/x``, or a bare
    #: slug. A slug naming two memories is refused with both, never guessed.
    target: str | None = None
    #: Narrow the correction to one ``<!-- fact:id -->`` claim in the target.
    claim_id: str | None = None
    #: The text that would stand instead. Omit (or set ``action="retire"``)
    #: to withdraw the target with no successor.
    replacement: str | None = None
    #: Explicitly permit the omitted original text listed by preview.
    allow_content_loss: bool = False
    #: ``supersede`` or ``retire``. Derived from ``replacement`` when omitted.
    action: Literal[*CORRECTION_ACTIONS] | None = None  # type: ignore[valid-type]
    #: Why. Recorded in the history sibling and the commit subject.
    reason: str | None = None
    #: The transcript candidate this correction came from, if any.
    candidate_id: str | None = None
    #: Project scope. Inferred from the target's own entities when omitted.
    project: str | None = None
    #: The replacement's own supporting records, as typed ``backed_by`` refs.
    #: The superseded original is never cited as support for its replacement.
    backed_by: list[str] | None = None
    #: Memory type for the replacement. Inherits the target's when omitted.
    type: str | None = None
    #: Slug for the replacement. Derived from the target's when omitted.
    slug: str | None = None


class CorrectionApplyRequest(CorrectionPreviewRequest):
    #: The revision the preview showed. A mismatch is refused, never merged.
    expect_revision: str = ""
    #: Explicit confirmation. Apply is never the default on any surface.
    confirm: bool = False


class CorrectionDismissRequest(BaseModel):
    candidate_id: str
    #: Required: a dismissal with no reason is indistinguishable from a
    #: candidate that was never detected.
    reason: str


class CorrectionUndoRequest(BaseModel):
    target: str
    expect_revision: str = ""
    confirm: bool = False
    reason: str | None = None


def _correction_http(error: Exception) -> HTTPException:
    """Map a review refusal onto a status code without losing its payload."""
    from palinode.core.path_guard import PathTraversalError
    from palinode.corrections import review as review_module

    if isinstance(error, PathTraversalError):
        return HTTPException(status_code=400 if error.malformed else 403, detail="Invalid path")
    if isinstance(error, review_module.TargetNotFoundError):
        return HTTPException(status_code=404, detail=error.as_dict())
    if isinstance(error, review_module.CorrectionError):
        return HTTPException(status_code=409, detail=error.as_dict())
    return _safe_500(error, "Correction failed")


@router.post("/corrections/preview")
def correction_preview_api(req: CorrectionPreviewRequest) -> dict[str, Any]:
    """Preview one correction. Reads only — nothing is written or committed.

    Returns the old text, the proposed new text, the affected document (and
    claim), its exact source revision, the rationale, where the correction came
    from, the project scope, the replacement relation that would be recorded,
    every other record that quotes or derives from the target, and the exact
    recovery command. `confirm.expect_revision` is what `/corrections/apply`
    requires back.
    """
    from palinode.corrections.review import preview_correction

    try:
        return preview_correction(**req.model_dump())
    except Exception as error:  # noqa: BLE001 — mapped, never swallowed
        raise _correction_http(error)


@router.post("/corrections/apply")
def correction_apply_api(req: CorrectionApplyRequest) -> dict[str, Any]:
    """Apply a previewed correction. Requires `confirm` and the preview's revision.

    Persists only through the existing validated write path — `save_memory` +
    `archive_memory` for a document, the executor's own SUPERSEDE/ARCHIVE op
    for a claim — so the git provenance, the `-history.md` audit sibling and
    the index propagation are whatever those contracts already produce. The
    actor names this a reviewed correction and carries the candidate id when
    one was the source. A stale revision or an ambiguous target is refused
    with 409 and the information needed to decide.

    A document-level correction that saved the replacement and then failed to
    retire the original is **not** a refusal and not a 500: it returns 409 with
    `applied: "partial"` and the block naming what was written, what was not,
    and the command that completes it. 409 because this router's 409 already
    means "the store is not in the state your request assumed — look, then
    decide", which is exactly what a partial needs from its caller; a 5xx would
    lose the payload (`_safe_500` sanitizes the detail down to a context
    string, on purpose) and would read to generic client tooling as a blind
    retry, which is the wrong move on a store that is already half-written.
    """
    from palinode.core.parity import CORRECTION_APPLIED_PARTIAL
    from palinode.corrections.review import apply_correction

    try:
        result = apply_correction(**req.model_dump())
    except Exception as error:  # noqa: BLE001 — mapped, never swallowed
        raise _correction_http(error)
    if result.get("applied") == CORRECTION_APPLIED_PARTIAL:
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/corrections/dismiss")
def correction_dismiss_api(req: CorrectionDismissRequest) -> dict[str, Any]:
    """Record that a reviewer declined a candidate. Writes no memory.

    The queue row is marked `dismissed` through the queue's own atomic writer
    and kept: the dedupe key is (session id, span hash), read from every row,
    so the dismissed row is what stops the next scan re-proposing the span.
    """
    from palinode.corrections.review import dismiss_candidate

    try:
        return dismiss_candidate(req.candidate_id, reason=req.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such correction candidate")
    except Exception as error:  # noqa: BLE001 — mapped, never swallowed
        raise _correction_http(error)


@router.post("/corrections/undo")
def correction_undo_api(req: CorrectionUndoRequest) -> dict[str, Any]:
    """Preview (default) or apply the undo of a correction.

    Without `confirm` this is a read: it states what would be restored, what
    would not be deleted, and what cannot be reached at all. With `confirm` and
    the preview's revision it calls `restore_memory` — the exact inverse of the
    archive. It refuses to resurrect anything separately retracted or withdrawn
    by a forget request; those have their own surfaces.
    """
    from palinode.corrections.review import apply_undo, preview_undo

    try:
        if req.confirm:
            return apply_undo(
                target=req.target,
                expect_revision=req.expect_revision,
                confirm=True,
                reason=req.reason,
            )
        return preview_undo(target=req.target)
    except Exception as error:  # noqa: BLE001 — mapped, never swallowed
        raise _correction_http(error)


class MigrateOpenClawRequest(BaseModel):
    path: str
    dry_run: bool = False


@router.post("/migrate/openclaw")
def migrate_openclaw_api(req: MigrateOpenClawRequest) -> dict:
    """Import a MEMORY.md from OpenClaw into Palinode.

    Parses each ## section into a separate memory file with heuristic
    type detection (person / decision / project / insight).

    Args:
        req: Request body with ``path`` (absolute or relative to memory_dir)
             and optional ``dry_run`` flag.

    Returns:
        dict with sections_found, files_created, files_skipped, log_file, dry_run.
    """
    from palinode.migration.openclaw import run_migration

    path = req.path
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="Null bytes are not allowed in path")

    # Resolve against memory_dir; reject paths that escape it.
    base = _memory_base_dir()
    if os.path.isabs(path):
        resolved_path = os.path.realpath(path)
    else:
        resolved_path = os.path.realpath(os.path.join(base, path))
    try:
        within = os.path.commonpath([base, resolved_path]) == base
    except ValueError:
        within = False
    if not within:
        raise HTTPException(status_code=403, detail="Path traversal rejected")
    path = resolved_path

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        result = run_migration(source_path=path, dry_run=req.dry_run)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"OpenClaw migration failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/depends/_unblocked")
def depends_unblocked_api() -> list[dict]:
    """Return all slugs whose every depends_on dependency is status=done.

    Each entry is ``{slug, status, file_path}``.  Items whose own status is
    "done" or "archived" are excluded.  Answers "what can I work on right now?"
    """
    from palinode.core.depends import find_unblocked
    try:
        return find_unblocked()
    except Exception as exc:
        raise _safe_500(exc, "depends unblocked failed")


@router.get("/depends/{slug:path}")
def depends_api(slug: str) -> dict:
    """Return the dependency neighbourhood for a given slug.

    Response shape::

        {
            "slug": "milestone/M1.1-init",
            "depends_on": [{"slug": "...", "status": "done", "found": true}, ...],
            "blocks": [...],
            "parallel_with": [...],
            "unblocked": bool,
            "orphans": ["milestone/X"],
        }
    """
    from palinode.core.depends import traverse_depends
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    try:
        return traverse_depends(slug)
    except Exception as exc:
        raise _safe_500(exc, "depends traversal failed")
