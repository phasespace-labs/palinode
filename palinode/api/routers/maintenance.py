"""Maintenance, ingest, reindex, entity, lint, migrate, and depends routes (#314 Stage 3).

Extracted from palinode/api/server.py:
  POST /ingest
  POST /ingest-url
  POST /rebuild-fts
  POST /reindex
  GET  /entities/{entity_ref:path}
  GET  /entities
  POST /lint
  POST /migrate/openclaw
  GET  /depends/_unblocked
  GET  /depends/{slug:path}
  POST /migrate/mem0
"""
from __future__ import annotations

import glob
import logging
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from palinode.core import store
from palinode.core.config import config

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
            return {"status": "success", "file_path": result}
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
async def reindex_api(since: str | None = None) -> dict[str, Any]:
    """Reindex memory files.  Idempotent — unchanged files are skipped.

    Query params:
        since: ISO timestamp (e.g. '2026-04-09T00:00:00Z').  If provided,
               only files whose mtime is newer than this are processed.
               Without it, all files are visited (but content-hash dedup
               still skips unchanged content).

    Returns 409 if a reindex is already in progress — check /status for
    progress.  (#200)
    """
    if _reindex_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="reindex already running — check /status for progress",
        )

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

    async with _reindex_lock:
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
def lint_api() -> dict[str, Any]:
    """Scan memory and report orphans, stale files, and contradictions."""
    from palinode.core.lint import run_lint_pass
    return run_lint_pass()


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


@router.post("/migrate/mem0")
def migrate_mem0_api() -> dict[str, str]:
    """Run the Mem0 backfill pipeline.

    One-time migration: exports from Qdrant, deduplicates, classifies,
    and generates Palinode markdown files.
    """
    from palinode.migration.run_mem0_backfill import main as run_backfill
    try:
        run_backfill()
        return {"status": "success", "message": "Mem0 backfill complete. Review files and reindex."}
    except Exception as e:
        raise _safe_500(e, "Backfill failed")
