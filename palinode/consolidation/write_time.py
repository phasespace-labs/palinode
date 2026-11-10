"""
Tier 2a: Write-time contradiction check on palinode_save (ADR-004).

When enabled, every save schedules a background contradiction check against
similar existing memories. Runs asynchronously via an in-process asyncio queue
(when the API server is handling the save) or via disk-backed marker files
(when the save comes from a CLI or plugin path without a long-lived worker).

Errors in the check are logged but never propagate to the save caller. The
save-never-fails invariant is load-bearing — see ADR-004 for rationale.

A disk marker is the durable record of a job, so it outlives the enqueue: the
sweep hands a copy to the worker and leaves the file in place, and only a
completed run deletes it. A worker that times out, dies, or takes the process
down with it therefore leaves the job pending for the next sweep instead of
consuming it. Retries are bounded by ``write_time.max_attempts`` (3), counted
in the marker itself so a restart cannot reset the bound; past it the marker
is retired to ``.failed.json`` with the reason logged, because a poison job
that loops forever and a marker that can never be cleared are both outages.

The queue carries one other job kind. A ``revalidate`` item (written by
:func:`palinode.core.revalidation.enqueue_revalidation`) is deterministic
deferred work: it calls no LLM, re-derives its decision from live disk, and
records a record's current backing state in its frontmatter. It rides this
queue because this is where deferred memory writes already go — the same
marker format, the same startup and idle sweeps, the same ``.failed.json``
surface when one cannot be applied, and the same ``git_tools`` commit path.

Public API:
    schedule_contradiction_check(file_path, item, *, sync=False, llm_fn=None) -> dict | None
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
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from palinode.core import git_tools
from palinode.core.config import config
from palinode.consolidation.op_parse import op_kind

if TYPE_CHECKING:
    # Type-only: importing `runner` at module load time risks a circular
    # import (see the local import in `_run_check_and_apply` below). Safe
    # under `from __future__ import annotations`, which defers evaluation of
    # every annotation in this module to strings.
    from palinode.consolidation.runner import LlmFn

#: Marker item kind for the deterministic backing-revalidation job. Spelled
#: out rather than imported so the save hot path does not pull in the
#: resolution stack to enqueue a contradiction check; mirrors
#: :data:`palinode.core.revalidation.MARKER_KIND`, and
#: ``tests/test_write_time.py`` pins the two together.
_REVALIDATE_KIND = "revalidate"

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

#: Marker paths handed to the worker and not yet resolved. Deleting the marker
#: at enqueue used to be what stopped a job being processed twice; now that the
#: marker survives until the run reports success, this set is that guard. It is
#: in-memory on purpose — a process that dies holds no claims, so its markers
#: are free for the next startup sweep.
_inflight: set[str] = set()


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
    llm_fn: LlmFn | None = None,
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
        llm_fn: The propose seam — same shape as
            ``runner._call_llm_with_fallback`` ((system_prompt, user_prompt)
            -> (result_text, model_used)). Defaults to the live caller.
            Only honoured on the sync path: the async worker (`_worker_loop`)
            always uses the live adapter, since queued/disk-marker jobs are
            plain dicts and a callable can't survive that round trip. Tests
            inject a fake adapter here to drive the real
            translate→apply_operations pipeline deterministically instead of
            mocking across the translate/apply seam.

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
            return _run_check_and_apply(file_path, item, llm_fn=llm_fn)
        else:
            return _enqueue(file_path, item)
    except Exception as e:  # noqa: BLE001 — intentional catch-all
        logger.error(f"write-time: schedule failed (non-fatal): {e}")
        return None


def sweep_pending_markers() -> int:
    """Hand the disk-backed marker queue to the worker, oldest first.

    Reads all *.json files under {PALINODE_DIR}/{pending_dir}/ in timestamp
    order and enqueues each one onto the in-process queue. The marker file is
    **left on disk**: it is the job's durable record, and only a completed run
    deletes it (`_consume_marker`, from the worker). A job whose worker times
    out or dies is therefore still pending when the next sweep runs, which is
    the retry ADR-004 describes.

    Each handoff increments the marker's ``attempts`` counter before the job
    goes on the queue. Past ``max_attempts`` the marker is retired to
    ``.failed.json`` with the reason logged rather than retried forever. A
    marker already in flight is skipped, not enqueued twice.

    Returns the number of markers handed to the worker this pass.
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
    if not markers:
        return 0

    try:
        queue = _get_queue()
    except Exception as e:  # noqa: BLE001
        logger.error(f"write-time: sweep could not reach the queue: {e}")
        return 0

    max_attempts = max(1, cfg.max_attempts)
    recovered = 0

    for marker_path in markers:
        if marker_path in _inflight:
            continue

        try:
            with open(marker_path, encoding="utf-8") as f:
                job = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _mark_failed(marker_path, f"corrupt marker: {e}")
            continue

        file_path = job.get("file_path")
        item = job.get("item")
        if not file_path or not item:
            _mark_failed(marker_path, "marker missing file_path or item")
            continue

        attempts = _attempts(job)
        if attempts >= max_attempts:
            _mark_failed(
                marker_path,
                f"no successful run after {attempts} attempt(s) "
                f"(max_attempts={max_attempts})",
            )
            continue

        # Checked before the counter is written so a full queue costs the job
        # a sweep, not an attempt. Nothing awaits between here and put_nowait,
        # so no other coroutine can take the slot.
        if queue.full():
            logger.warning(
                f"write-time: queue full during sweep, leaving marker: {marker_path}"
            )
            break

        job["attempts"] = attempts + 1
        try:
            _record_attempt(marker_path, job)
        except OSError as e:
            # The persisted count is what bounds the retry. Unable to write it,
            # this job would be retried forever — retire it instead.
            _mark_failed(marker_path, f"could not record attempt: {e}")
            continue

        try:
            queue.put_nowait(
                {
                    "file_path": file_path,
                    "item": item,
                    "marker_path": marker_path,
                    "attempt": job["attempts"],
                }
            )
        except Exception as e:  # noqa: BLE001
            _mark_failed(marker_path, f"sweep enqueue failed: {e}")
            continue

        _inflight.add(marker_path)
        recovered += 1

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

    # A starting worker owns nothing yet: any claim left by a previous one
    # died with it, and holding it here would make those markers permanently
    # unsweepable.
    _inflight.clear()

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
    Content: {"file_path": ..., "item": ..., "enqueued_at": ..., "attempts": 0}

    ``attempts`` is the number of times the sweep has handed this job to a
    worker. It lives here, not in memory, because the bound it feeds has to
    survive the restart that a crashed worker causes.

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
        "attempts": 0,
    }

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.rename(tmp_path, marker_path)
    return marker_path


def _attempts(job: dict[str, Any]) -> int:
    """How many times this job has already been handed to a worker.

    Markers written before the counter existed have no ``attempts`` key; they
    start at zero and get the full bound.
    """
    try:
        return max(0, int(job.get("attempts", 0)))
    except (TypeError, ValueError):
        return 0


def _record_attempt(marker_path: str, job: dict[str, Any]) -> None:
    """Rewrite a marker in place with its updated attempt count.

    Same write-to-tmp + rename as `_write_marker`, and the same file name, so
    the marker keeps its position in the oldest-first sweep order. Raises
    OSError to its caller: an attempt that cannot be recorded is not bounded,
    and the caller retires the marker rather than retry it blind.
    """
    tmp_path = marker_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.rename(tmp_path, marker_path)


def _consume_marker(marker_path: str | None) -> None:
    """Delete the marker of a job that ran to completion.

    Success is what retires a pending marker — enqueue is not (ADR-004). A
    marker that survives its run is retried by the next sweep, so failing to
    delete one costs a duplicate pass, not a lost job.
    """
    if not marker_path:
        return
    try:
        os.remove(marker_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.error(
            f"write-time: could not remove completed marker {marker_path}: {e}"
        )


def _mark_failed(marker_path: str, reason: str = "") -> None:
    """Rename a corrupt or permanently-failed marker to .failed.json.

    Fail-loud design: failed markers are preserved for operator review
    rather than retried silently forever. The reason is logged at ERROR so
    the rename is never the only record of why the job stopped.
    """
    if reason:
        logger.error(
            f"write-time: retiring marker {marker_path} to .failed.json: {reason}"
        )
    failed_path = marker_path.replace(".json", ".failed.json")
    try:
        os.rename(marker_path, failed_path)
    except OSError as e:
        logger.error(f"write-time: could not rename failed marker: {e}")
    finally:
        _inflight.discard(marker_path)


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
        marker_path = job.get("marker_path")

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
            # The run completed. Only now is the job's durable record spent.
            _consume_marker(marker_path)
        except asyncio.TimeoutError:
            logger.error(f"write-time: timeout on {file_path}{_retry_note(job)}")
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"write-time: job failed for {file_path}: {e}{_retry_note(job)}"
            )
        finally:
            if marker_path:
                _inflight.discard(marker_path)
            queue.task_done()


def _retry_note(job: dict[str, Any]) -> str:
    """The retry clause for a failed job's log line.

    A marker-backed job is left pending for the next sweep; an in-memory one
    has no durable record and is genuinely gone, which the log should say
    rather than imply a recovery that will not happen.
    """
    if not job.get("marker_path"):
        return " (no pending marker — job not retried)"
    max_attempts = max(1, config.consolidation.write_time.max_attempts)
    return (
        f" (attempt {job.get('attempt', '?')}/{max_attempts}; "
        f"marker left pending for the next sweep)"
    )


# ── Internal: actual work (sync path and worker both call this) ────────────


def _run_check_and_apply(
    file_path: str, item: dict[str, Any], *, llm_fn: LlmFn | None = None
) -> dict[str, Any]:
    """Run the contradiction check and apply resulting ops via the executor.

    This is the one place that calls the LLM and mutates files. Both the
    sync path (from the save API call) and the background worker call
    this function. It is synchronous — async callers must wrap it in
    asyncio.to_thread() to avoid blocking the event loop.

    Args:
        llm_fn: The propose seam, threaded through from
            `schedule_contradiction_check` (default None → the live
            `runner._call_llm_with_fallback`). Passed straight through to
            `_check_contradictions`, which already accepts it — this is the
            join a real test must cross rather than mock.

    Returns:
        {"operations": [...], "applied_stats": {...}}
        "applied_stats" is empty dict when the check proposed nothing
        actionable. Otherwise it carries `translation_skipped` — the
        actionable ops `_translate_ops` dropped as malformed — plus, once
        anything was routed, the executor's stats summed across every file
        the ops were routed to (see `_route_ops`) and this applier's own
        `ROUTE_STATS`.
    """
    # A `revalidate` job is not a contradiction check: it is deterministic,
    # calls no LLM, and re-derives its own decision from live disk. It rides
    # this queue because this is where deferred memory writes already go —
    # same marker, same sweep, same failure surface, same commit path.
    if item.get("kind") == _REVALIDATE_KIND:
        from palinode.core import revalidation

        stats = revalidation.apply_revalidation(file_path, item)
        return {"operations": [], "applied_stats": stats, "llm_latency_ms": 0}

    # Import here to avoid circular import at module load time
    from palinode.consolidation.runner import _check_contradictions
    from palinode.consolidation.executor import apply_operations

    from palinode.core.capture_policy import automatic_note_processing_allowed
    from palinode.core.parser import parse_frontmatter

    with open(file_path, encoding="utf-8") as handle:
        metadata, _ = parse_frontmatter(handle.read())
    if not automatic_note_processing_allowed(metadata):
        return {"operations": [], "applied_stats": {}, "llm_latency_ms": 0}

    start = time.monotonic()
    operations = _check_contradictions(
        [item], item.get("category", ""), llm_fn=llm_fn
    )
    llm_latency_ms = int((time.monotonic() - start) * 1000)

    # Filter out NOOPs and ADDs — those are not contradictions, just "fine as-is"
    # ADD means "this is new, save it normally" which the save path already did.
    actionable = [
        op
        for op in operations
        if op_kind(op) not in ("NOOP", "ADD")
    ]

    # The candidate rows each proposal was generated against. Popped so the
    # returned `operations` keep the shape callers (the sync save result, the
    # CLI's summary line) already print — the rows are routing input, not
    # part of the proposal.
    candidates: list[dict] = []
    for op in operations:
        candidates.extend(op.pop("candidates", None) or [])

    applied_stats: dict[str, int] = {}
    if actionable:
        # Translate _check_contradictions output to executor input format.
        # _check_contradictions returns {"operation": "UPDATE", "item": {...}, ...}
        # apply_operations expects {"op": "UPDATE", "id": ..., ...}
        executor_ops = _translate_ops(actionable, file_path)
        # An actionable op the translator produced nothing for was malformed
        # (no target id, an unknown kind, or an UPDATE with no replacement
        # text). Counted so a pass that applied nothing still says why.
        applied_stats["translation_skipped"] = len(actionable) - len(executor_ops)
        if executor_ops:
            routed, route_stats = _route_ops(executor_ops, file_path, candidates)
            applied_stats.update(route_stats)
            for target, target_ops in routed:
                try:
                    file_stats = apply_operations(target, target_ops)
                    for key, value in file_stats.items():
                        applied_stats[key] = applied_stats.get(key, 0) + value
                    _git_commit_dedup(target)
                    if _mutated(file_stats):
                        _reindex(target)
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        f"write-time: executor apply failed for {target}: {e}"
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
        "UPDATE"  → {"op": "UPDATE", ...}  when the op carries ``new_text``
                    (rewrite the matched existing line with exactly that text)
        "UPDATE"  → nothing when it carries none: the op is malformed and is
                    skipped, with a warning naming the target id
        "DELETE"  → {"op": "SUPERSEDE", ...}  when the op carries ``new_text``
                    (we don't delete; the text is the successor line)
        "DELETE"  → {"op": "ARCHIVE", ...}  when it carries none: a retirement
                    with no replacement, retired into history
        Everything else is filtered out by the caller.

    The replacement text is never synthesised from the saved item. The item's
    ``content`` is the whole body of the file that triggered the check; a
    SUPERSEDE inserts ``new_text`` verbatim as one fact line after the
    tombstone and an UPDATE rewrites the target line with it — a multi-line
    save used to land in the target as a single line carrying every fact id
    it contained, duplicating each of them. A text-less DELETE is what the
    checker means by "this fact is retired"; the executor's ARCHIVE is that
    op, and its ADR-020 guard rejects it on a superseded-only (identity)
    document rather than forging a successor. A text-less UPDATE says nothing
    the executor could apply, so it is dropped rather than guessed at.
    """
    translated = []
    for op in contradiction_ops:
        operation = op_kind(op)
        target_id = op.get("target_id") or op.get("id")
        if not target_id:
            # Without a target fact ID, we can't apply the op deterministically
            continue

        if operation == "UPDATE":
            new_text = op.get("new_text")
            if not new_text:
                logger.warning(
                    "write-time: UPDATE skipped — fact id=%r carries no new_text; "
                    "the saved body is never used as the replacement",
                    target_id,
                )
                continue
            translated.append(
                {
                    "op": "UPDATE",
                    "id": target_id,
                    "new_text": new_text,
                    "reason": op.get("reason", "write-time dedup"),
                }
            )
        elif operation == "DELETE":
            new_text = op.get("new_text")
            if new_text:
                translated.append(
                    {
                        "op": "SUPERSEDE",
                        "id": target_id,
                        # Inserted verbatim as the successor fact line after
                        # the tombstone (see executor._supersede_fact).
                        "new_text": new_text,
                        "superseded_by": op.get("item", {}).get("id", ""),
                        "reason": op.get("reason", "write-time: superseded"),
                    }
                )
            else:
                translated.append(
                    {
                        "op": "ARCHIVE",
                        "id": target_id,
                        "reason": op.get("reason", "write-time: retired"),
                    }
                )
    return translated


#: Executor stats that mean the target file's body changed. The rejection and
#: no-op counters (``unmatched``, ``*_rejected``, ``kept``) are excluded so a
#: dropped op never triggers a reindex of an untouched file.
_MUTATION_STATS = ("updated", "merged", "superseded", "archived", "retracted",
                   "contradicts_proposed")

#: Stats this applier adds to the executor's, always present once ops were
#: routed so a quiet pass reports ``0`` rather than omitting the key.
ROUTE_STATS: tuple[str, ...] = ("ambiguous_rejected", "stale_rejected")


def _mutated(stats: dict[str, int]) -> bool:
    return any(stats.get(key, 0) for key in _MUTATION_STATS)


def _route_ops(
    executor_ops: list[dict], file_path: str, candidates: list[dict]
) -> tuple[list[tuple[str, list[dict]]], dict[str, int]]:
    """Group executor ops by the file that owns each op's target fact.

    The contradiction check retrieves candidates across every stored file,
    but ``apply_operations`` is per-file and used to be called on the
    just-saved file only. A proposal naming a fact that lives in another file
    was therefore dropped as ``unmatched`` (the fact is not in the saved
    file), and one naming an id that happens to exist in *both* the saved
    file and a candidate mutated the saved file's copy — the wrong target.

    Ownership is resolved against the candidate rows the proposal was
    generated from, not a store-wide lookup: the model could only name a fact
    it saw. Three outcomes per op:

    * exactly one candidate file carries the id → the op goes to that file,
      **provided** the candidate section it was proposed against is still
      what is on disk (``store.check_freshness`` against the row's
      ``content_hash``). A stale section means the target changed between
      proposal and application; the op is rejected as ``stale_rejected``
      rather than applied last-write-wins.
    * more than one candidate file carries the id → ``ambiguous_rejected``.
      The executor could only ever pick one, and there is no deterministic
      way to know which the model meant.
    * no candidate carries the id → the op goes to the saved file, exactly
      as before. The executor reports it ``unmatched`` if the id is not there
      either.

    Returns ``(routed, stats)``: ``routed`` is ``[(target_path, ops), …]`` in
    first-seen order, ``stats`` holds the two rejection counters.
    """
    from palinode.consolidation.status_doc import fact_ids
    from palinode.core import store

    stats = {key: 0 for key in ROUTE_STATS}

    # fact id → {realpath: (spelling, [candidate rows carrying the id])}
    owners: dict[str, dict[str, tuple[str, list[dict]]]] = {}
    for row in candidates:
        path = row.get("file_path") or ""
        if not path:
            continue
        if not os.path.isabs(path):
            path = os.path.join(config.palinode_dir, path)
        key = os.path.realpath(path)
        for fid in fact_ids(row.get("content") or ""):
            spelling, rows = owners.setdefault(fid, {}).setdefault(key, (path, []))
            rows.append(row)

    saved_key = os.path.realpath(file_path)
    routed: dict[str, list[dict]] = {}
    for op in executor_ops:
        fid = op.get("id")
        files = owners.get(fid, {})
        if len(files) > 1:
            logger.warning(
                "write-time: %s rejected — fact id=%r is present in %d candidate "
                "files, target is ambiguous: %s",
                op.get("op"), fid, len(files),
                ", ".join(sorted(os.path.relpath(p, config.palinode_dir) for p, _ in files.values())),
            )
            stats["ambiguous_rejected"] += 1
            continue
        if files:
            key, (spelling, rows) = next(iter(files.items()))
            target = file_path if key == saved_key else spelling
            fresh = store.check_freshness([dict(r) for r in rows])
            if any(r.get("freshness") == "stale" for r in fresh):
                logger.warning(
                    "write-time: %s rejected — fact id=%r in %s changed on disk "
                    "since the proposal was generated (stale precondition)",
                    op.get("op"), fid, os.path.relpath(target, config.palinode_dir),
                )
                stats["stale_rejected"] += 1
                continue
        else:
            target = file_path
        routed.setdefault(target, []).append(op)

    return list(routed.items()), stats


def _reindex(file_path: str) -> None:
    """Re-index a file this pass mutated, so the store's row (and the
    ``content_hash`` the next proposal's precondition is checked against)
    reflects what is on disk without waiting for the watcher. Best-effort:
    a failure is logged, never raised — the file is on disk and reaches the
    index when the watcher next sees it."""
    try:
        from palinode.indexer.index_file import index_file

        outcome = index_file(file_path)
        if outcome.get("error"):
            logger.warning(
                "write-time: reindex reported %s for %s", outcome["error"], file_path,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("write-time: reindex failed for %s: %s", file_path, exc)


def _git_commit_dedup(file_path: str) -> None:
    """Create a separate git commit for the dedup pass.

    Keeps history clean: you can blame a memory line back to either the
    original user save or the subsequent write-time dedup pass. Through the
    git_tools choke point (commit_memory_files) rather than a raw
    subprocess.run — that primitive already no-ops when
    config.git.auto_commit is off and already logs its own I/O failures, so
    this is a thin wrapper for the dedup-specific commit message.

    Stages the target's ``-history.md`` sibling with it when one exists: a
    SUPERSEDE appends the retired text there, and the runner's compaction
    commit (``runner._touched_files``) already treats the pair as one
    mutation. Left out, the sibling sat untracked until some later sweep.
    """
    from palinode.consolidation.runner import _touched_files

    rel = os.path.relpath(file_path, config.palinode_dir)
    msg = f"{config.git.commit_prefix} write-time dedup: {rel}"
    git_tools.commit_memory_files(_touched_files(file_path), msg)
