"""
Tier 2a: Write-time contradiction check on palinode_save (ADR-004).

When enabled, every save schedules a background contradiction check against
similar existing memories. Runs asynchronously via an in-process asyncio queue
(when the API server is handling the save) or via disk-backed marker files
(when the save comes from a CLI or plugin path without a long-lived worker).

Errors in the check are logged but never propagate to the save caller. The
save-never-fails invariant is load-bearing — see ADR-004 for rationale.

Public API:
    schedule_contradiction_check(file_path, item, *, sync=False) -> dict | None
    sweep_pending_markers() -> int
    start_worker(app_state) -> None
    stop_worker(app_state) -> None

Everything else in this module is internal.
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from palinode.core.config import config

logger = logging.getLogger("palinode.write_time")
# Ensure INFO logs propagate even if the parent logger tree hasn't been
# configured yet (e.g., when imported before the API logging setup).
logger.setLevel(logging.INFO)
logger.propagate = True

# ── In-process async queue (used when save came from the API server) ───────

# Module-level queue — created on first access, drained by the worker task
# started from the API lifespan. Bounded at config.write_time.queue_max_size;
# when full, new jobs fall through to disk-backed markers instead of blocking.
_queue: asyncio.Queue | None = None


def _get_queue() -> asyncio.Queue:
    """Lazily create the module-level queue on first access.

    Must be called from an event-loop-bearing context. Not thread-safe —
    all callers should be on the API server's event loop.
    """
    global _queue
    if _queue is None:
        max_size = config.consolidation.write_time.queue_max_size
        _queue = asyncio.Queue(maxsize=max_size)
    return _queue


# ── Public entry points ────────────────────────────────────────────────────


def schedule_contradiction_check(
    file_path: str,
    item: dict[str, Any],
    *,
    sync: bool = False,
) -> dict[str, Any] | None:
    """Schedule a write-time contradiction check for a just-saved memory.

    Args:
        file_path: Absolute path to the memory file that was just saved.
        item: Dict with at least {"content", "category", "type"}. May also
            contain "entities" and other metadata. Passed through to
            _check_contradictions as-is.
        sync: If True, runs the check inline and returns the result dict.
            If False (default), enqueues a job for background processing
            and returns None immediately.

    Returns:
        When sync=True: {"operations": [...], "applied_stats": {...}}
        When sync=False: None

    Never raises. Errors in the check are logged and swallowed — the save
    call path must never fail because of a tier 2a problem. This is the
    ADR-004 load-bearing invariant.
    """
    if not config.consolidation.write_time.enabled:
        return None

    try:
        if sync:
            return _run_check_and_apply(file_path, item)
        else:
            return _enqueue(file_path, item)
    except Exception as e:  # noqa: BLE001 — intentional catch-all
        logger.error(f"write-time: schedule failed (non-fatal): {e}")
        return None


def sweep_pending_markers() -> int:
    """Drain the disk-backed marker queue on API startup.

    Reads all *.json files under {PALINODE_DIR}/{pending_dir}/ in timestamp
    order, re-enqueues each one onto the in-process queue, and deletes the
    marker on successful enqueue. If enqueue fails (queue full, etc.) the
    marker is left in place and will be retried on the next sweep.

    Returns the number of markers successfully recovered.
    """
    cfg = config.consolidation.write_time
    if not cfg.sweep_on_startup:
        return 0

    pending_dir = _pending_dir()
    if not os.path.isdir(pending_dir):
        return 0

    markers = sorted(
        p for p in glob.glob(os.path.join(pending_dir, "*.json"))
        if not p.endswith(".failed.json")
    )
    recovered = 0

    for marker_path in markers:
        try:
            with open(marker_path) as f:
                job = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(
                f"write-time: corrupt marker {marker_path}: {e} — renaming to .failed.json"
            )
            _mark_failed(marker_path)
            continue

        file_path = job.get("file_path")
        item = job.get("item")
        if not file_path or not item:
            logger.error(
                f"write-time: marker missing file_path or item: {marker_path}"
            )
            _mark_failed(marker_path)
            continue

        try:
            queue = _get_queue()
            queue.put_nowait({"file_path": file_path, "item": item})
            os.remove(marker_path)
            recovered += 1
        except asyncio.QueueFull:
            # Queue is full; leave marker on disk for next sweep
            logger.warning(
                f"write-time: queue full during sweep, leaving marker: {marker_path}"
            )
            break
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"write-time: sweep enqueue failed for {marker_path}: {e}"
            )
            _mark_failed(marker_path)

    if recovered:
        logger.info(f"write-time: recovered {recovered} pending markers")
    return recovered


async def start_worker(app_state: Any) -> None:
    """Start the background worker task. Called from API server lifespan.

    Attaches the task handle to app_state.write_time_task so stop_worker
    can cancel it on shutdown.
    """
    if not config.consolidation.write_time.enabled:
        logger.info("write-time: disabled in config, not starting worker")
        return

    # Sweep first so recovered markers are in the queue before the worker starts
    sweep_pending_markers()

    queue = _get_queue()
    app_state.write_time_task = asyncio.create_task(_worker_loop(queue))
    logger.info("write-time: worker started")


async def stop_worker(app_state: Any) -> None:
    """Cancel the background worker task. Called from API server lifespan."""
    task = getattr(app_state, "write_time_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    logger.info("write-time: worker stopped")


# ── Internal: enqueue ──────────────────────────────────────────────────────


def _enqueue(file_path: str, item: dict[str, Any]) -> None:
    """Try to push onto the in-process queue; fall through to disk marker on failure.

    Two failure modes:
    1. No running event loop (e.g., save came via sync CLI path) → disk marker.
       The in-process queue is only useful when a worker task is draining it,
       and workers only run inside the API server's event loop.
    2. Queue full (backpressure) → disk marker.

    Either way, the job is durable and will be processed on the next API startup
    or when the queue has capacity.
    """
    # Detect whether we're inside the API server's event loop (where a worker
    # is draining the queue). CLI and plugin paths are sync and have no loop —
    # they must go to disk so the next API startup sweep picks them up.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            f"write-time: no running event loop, using disk marker for {file_path}"
        )
        _write_marker(file_path, item)
        return None

    try:
        queue = _get_queue()
        queue.put_nowait({"file_path": file_path, "item": item})
        logger.debug(f"write-time: enqueued {file_path}")
        return None
    except asyncio.QueueFull:
        logger.warning(
            f"write-time: queue full, falling through to disk marker for {file_path}"
        )
        _write_marker(file_path, item)
        return None


def _write_marker(file_path: str, item: dict[str, Any]) -> str:
    """Atomically write a disk marker for a pending check.

    Format: {PALINODE_DIR}/.palinode/pending/{utc_iso}-{uuid}.json
    Content: {"file_path": ..., "item": ..., "enqueued_at": ...}

    Atomic via write-to-tmp + rename.
    """
    pending_dir = _pending_dir()
    os.makedirs(pending_dir, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    job_id = uuid.uuid4().hex[:8]
    marker_name = f"{ts}-{job_id}.json"
    marker_path = os.path.join(pending_dir, marker_name)
    tmp_path = marker_path + ".tmp"

    job = {
        "file_path": file_path,
        "item": item,
        "enqueued_at": datetime.now(UTC).isoformat(),
    }

    with open(tmp_path, "w") as f:
        json.dump(job, f)
    os.rename(tmp_path, marker_path)
    return marker_path


def _mark_failed(marker_path: str) -> None:
    """Rename a corrupt or permanently-failed marker to .failed.json.

    Fail-loud design: failed markers are preserved for operator review
    rather than retried silently forever.
    """
    failed_path = marker_path.replace(".json", ".failed.json")
    try:
        os.rename(marker_path, failed_path)
    except OSError as e:
        logger.error(f"write-time: could not rename failed marker: {e}")


def _pending_dir() -> str:
    """Absolute path to the pending markers directory."""
    rel = config.consolidation.write_time.pending_dir
    if os.path.isabs(rel):
        return rel
    return os.path.join(config.palinode_dir, rel)


# ── Internal: worker loop ──────────────────────────────────────────────────


async def _worker_loop(queue: asyncio.Queue) -> None:
    """Background task that drains the queue one job at a time.

    Runs forever until cancelled. Never exits on a single-job failure —
    each job is wrapped in try/except so a bad input can't kill the worker.

    When the in-memory queue is idle (timeout fires with no job), the worker
    re-sweeps disk markers. This is critical because FastAPI's sync endpoint
    handlers run in a threadpool and can't push to the in-memory queue
    directly — they fall through to disk markers via _enqueue's no-loop
    detection. Without periodic sweeping, markers from live saves would
    accumulate forever between restarts.
    """
    logger.info("write-time: worker loop started")
    IDLE_SWEEP_INTERVAL = 10.0  # seconds

    while True:
        try:
            try:
                job = await asyncio.wait_for(queue.get(), timeout=IDLE_SWEEP_INTERVAL)
            except asyncio.TimeoutError:
                # Idle window: sweep disk markers that landed from sync contexts
                recovered = sweep_pending_markers()
                if recovered:
                    logger.debug(f"write-time: idle sweep recovered {recovered}")
                continue
        except asyncio.CancelledError:
            logger.info("write-time: worker loop cancelled")
            raise

        file_path = job.get("file_path", "<missing>")
        item = job.get("item", {})

        try:
            # Run the actual LLM call in a thread — _check_contradictions is
            # synchronous and we don't want to block the event loop on it.
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_check_and_apply, file_path, item),
                timeout=config.consolidation.write_time.check_timeout_seconds,
            )
            ops = result.get("operations", [])
            logger.info(
                f"write-time: file={os.path.basename(file_path)} "
                f"ops={len(ops)} applied={result.get('applied_stats', {})}"
            )
        except asyncio.TimeoutError:
            logger.error(f"write-time: timeout on {file_path}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"write-time: job failed for {file_path}: {e}")
        finally:
            queue.task_done()


# ── Internal: actual work (sync path and worker both call this) ────────────


def _run_check_and_apply(
    file_path: str, item: dict[str, Any]
) -> dict[str, Any]:
    """Run the contradiction check and apply resulting ops via the executor.

    This is the one place that calls the LLM and mutates files. Both the
    sync path (from the save API call) and the background worker call
    this function. It is synchronous — async callers must wrap it in
    asyncio.to_thread() to avoid blocking the event loop.

    Returns:
        {"operations": [...], "applied_stats": {...}}
        "applied_stats" is empty dict when no ops were applied.
    """
    # Import here to avoid circular import at module load time
    from palinode.consolidation.runner import _check_contradictions
    from palinode.consolidation.executor import apply_operations

    start = time.monotonic()
    operations = _check_contradictions([item], item.get("category", ""))
    llm_latency_ms = int((time.monotonic() - start) * 1000)

    # Filter out NOOPs and ADDs — those are not contradictions, just "fine as-is"
    # ADD means "this is new, save it normally" which the save path already did.
    actionable = [
        op
        for op in operations
        if op.get("operation", "").upper() not in ("NOOP", "ADD")
    ]

    applied_stats: dict[str, int] = {}
    if actionable:
        # Translate _check_contradictions output to executor input format.
        # _check_contradictions returns {"operation": "UPDATE", "item": {...}, ...}
        # apply_operations expects {"op": "UPDATE", "id": ..., ...}
        executor_ops = _translate_ops(actionable, file_path)
        if executor_ops:
            try:
                applied_stats = apply_operations(file_path, executor_ops)
                _git_commit_dedup(file_path)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"write-time: executor apply failed for {file_path}: {e}"
                )

    logger.debug(
        f"write-time: check complete file={file_path} "
        f"llm_ms={llm_latency_ms} ops={len(actionable)}"
    )

    return {
        "operations": operations,
        "applied_stats": applied_stats,
        "llm_latency_ms": llm_latency_ms,
    }


def _translate_ops(
    contradiction_ops: list[dict], file_path: str
) -> list[dict]:
    """Translate _check_contradictions output into executor input format.

    _check_contradictions emits:
        {"operation": "UPDATE"|"DELETE", "item": {...}, "target_id": "...", ...}

    The executor (apply_operations) expects:
        {"op": "UPDATE"|"SUPERSEDE"|..., "id": "...", ...}

    Mappings:
        "UPDATE"  → {"op": "UPDATE", ...}  (update the matched existing line)
        "DELETE"  → {"op": "SUPERSEDE", ...}  (we don't delete; supersede instead)
        Everything else is filtered out by the caller.
    """
    translated = []
    for op in contradiction_ops:
        operation = op.get("operation", "").upper()
        target_id = op.get("target_id") or op.get("id")
        if not target_id:
            # Without a target fact ID, we can't apply the op deterministically
            continue

        if operation == "UPDATE":
            translated.append(
                {
                    "op": "UPDATE",
                    "id": target_id,
                    "new_text": op.get("new_text")
                    or op.get("item", {}).get("content", ""),
                    "reason": op.get("reason", "write-time dedup"),
                }
            )
        elif operation == "DELETE":
            translated.append(
                {
                    "op": "SUPERSEDE",
                    "id": target_id,
                    "superseded_by": op.get("item", {}).get("id", ""),
                    "reason": op.get("reason", "write-time: superseded"),
                }
            )
    return translated


def _git_commit_dedup(file_path: str) -> None:
    """Create a separate git commit for the dedup pass.

    Keeps history clean: you can blame a memory line back to either the
    original user save or the subsequent write-time dedup pass.
    """
    if not config.git.auto_commit:
        return
    try:
        rel = os.path.relpath(file_path, config.palinode_dir)
        subprocess.run(
            ["git", "add", rel],
            cwd=config.palinode_dir,
            check=False,
            capture_output=True,
        )
        msg = f"{config.git.commit_prefix} write-time dedup: {rel}"
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=config.palinode_dir,
            check=False,
            capture_output=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"write-time: git commit failed: {e}")
