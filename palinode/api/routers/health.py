"""Health, status, and diagnostic routes (#314 Stage 3).

Extracted from palinode/api/server.py:
  GET /status
  GET /health
  GET /health/watcher
  GET /health/auto-summary
  GET /doctor
"""
from __future__ import annotations

import glob
import os
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter

from palinode import __version__
from palinode.core import store, git_tools
from palinode.core.config import config
from palinode.core.ollama_client import OllamaRole

from palinode.api._util import _auto_summary_state, _reindex_state, _utc_now
from palinode.api.memory_write import _is_description_eligible

router = APIRouter()

# Cache the functional embed probe so a monitor polling /status doesn't trigger a
# real embed on every hit. The probe itself is bounded (a ~2 s one-token embed);
# this TTL keeps a warm result for a minute and a cold result from being re-paid
# on back-to-back calls.
_EMBED_PROBE_TTL_S = 60.0
_embed_probe_cache: dict[str, Any] = {"ts": 0.0, "ok": None}


def _embed_functional_cached(client: Any) -> bool:
    """Return the cached functional-embed verdict, refreshing past the TTL."""
    now = time.monotonic()
    cached = _embed_probe_cache["ok"]
    if cached is not None and (now - _embed_probe_cache["ts"]) < _EMBED_PROBE_TTL_S:
        return cached
    ok = client.probe_embed()
    _embed_probe_cache["ts"] = now
    _embed_probe_cache["ok"] = ok
    return ok


@router.get("/status")
def status_api() -> dict[str, Any]:
    """Generates overarching health-checks to ensure pipeline availability."""
    # Late lookup on the server module so a test that
    # `patch("palinode.api.server.get_ollama_client")` intercepts the liveness
    # probe (the pre-split handler lived in server.py and called the module-local
    # name). Deferred import avoids the server↔routers cycle at module load.
    import palinode.api.server as _srv
    stats: dict[str, Any] = dict(store.get_stats())

    # Deployed package version — single source of truth is
    # palinode.__version__ (importlib.metadata), the same value CLI --version
    # surfaces. Lets an operator confirm which release is actually running
    # (motivated by the v0.8.14/v0.8.15 confusion).
    stats["version"] = __version__

    git_stats = git_tools.commit_count(7)
    stats["git_commits_7d"] = git_stats["total_commits"]
    stats["git_summary_7d"] = git_stats["summary"]

    try:
        unpushed = subprocess.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            cwd=config.palinode_dir, capture_output=True, text=True,
        )
        stats["unpushed_commits"] = int(unpushed.stdout.strip()) if unpushed.stdout.strip() else 0
    except (subprocess.SubprocessError, OSError, ValueError):
        # L1: narrowed from `Exception`. SubprocessError covers process spawn
        # and timeout paths, OSError covers a missing `git` binary, ValueError
        # covers a non-numeric stdout. We don't want to mask programmer errors.
        stats["unpushed_commits"] = 0

    db = store.get_db()
    try:
        fts_count = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        stats["fts_chunks"] = fts_count
    except Exception:
        stats["fts_chunks"] = 0

    try:
        entity_count = db.execute("SELECT count(DISTINCT entity_ref) FROM entities").fetchone()[0]
        stats["total_entities"] = entity_count
    except Exception:
        stats["total_entities"] = 0

    db.close()

    stats["hybrid_search"] = config.search.hybrid_enabled
    stats["associative_capability"] = stats["total_entities"] > 0

    # Liveness via the centralized client's ping (raw GET, no circuit breaker).
    _ollama_client = _srv.get_ollama_client()
    ollama_reachable = _ollama_client.ping(OllamaRole.EMBED)
    stats["ollama_reachable"] = ollama_reachable

    # `ollama_reachable` only means the daemon answered a GET — it says nothing
    # about whether the embedding MODEL works. `embed_functional` is the honest
    # signal: a real, bounded one-token embed (cached, see _embed_functional_cached).
    # On a cold/absent bge-m3, ollama_reachable is True but embed_functional is
    # False — so /status can no longer be falsely green (keyword-only mode is the
    # actual state). Skip the probe entirely when the daemon isn't even reachable.
    stats["embed_functional"] = (
        _embed_functional_cached(_ollama_client) if ollama_reachable else False
    )

    # Per-role Ollama traffic metrics (Phase 5): p50/p95/error-rate over a
    # 5-minute window plus circuit state, for each role that has seen traffic in
    # this process. Lets monitors + `palinode doctor` distinguish "reachable but
    # degraded" (p95 high / circuit half-open) from a flat binary reachable bool.
    stats["ollama"] = _srv.get_ollama_client().metrics()

    # Tier 2a (ADR-004) observability
    stats["write_time_enabled"] = config.consolidation.write_time.enabled
    if config.consolidation.write_time.enabled:
        try:
            from palinode.consolidation import write_time
            queue = write_time._queue
            stats["write_time_queue_depth"] = queue.qsize() if queue else 0
            pending_dir = write_time._pending_dir()
            if os.path.isdir(pending_dir):
                pending = glob.glob(os.path.join(pending_dir, "*.json"))
                failed = glob.glob(os.path.join(pending_dir, "*.failed.json"))
                stats["write_time_pending_markers"] = len(pending) - len(failed)
                stats["write_time_failed_markers"] = len(failed)
            else:
                stats["write_time_pending_markers"] = 0
                stats["write_time_failed_markers"] = 0
        except Exception as e:
            import logging
            logging.getLogger("palinode.api").warning(f"write-time status lookup failed: {e}")

    # Reindex progress
    stats["reindex"] = {
        "running": _reindex_state["running"],
        "started_at": _reindex_state["started_at"],
        "files_processed": _reindex_state["files_processed"],
        "total_files": _reindex_state["total_files"],
    }

    # auto_summary observability. Since auto_summary moved off the
    # /save hot path, external monitors need a way to detect a stalled pipeline.
    # last_run_at == None means /generate-summaries has never been invoked
    # in this process — expected on a freshly-started API before the watcher
    # fires its first debounced trigger.
    stats["auto_summary"] = {
        "enabled": config.auto_summary.enabled,
        "last_run_at": _auto_summary_state["last_run_at"],
        "last_run_duration_ms": _auto_summary_state["last_run_duration_ms"],
        "last_run_count": _auto_summary_state["last_run_count"],
        "last_run_errors": _auto_summary_state["last_run_errors"],
        # description backfill shares the /generate-summaries run.
        "last_run_descriptions": _auto_summary_state["last_run_descriptions"],
        "last_run_description_errors": _auto_summary_state["last_run_description_errors"],
        "last_error": _auto_summary_state["last_error"],
        "total_runs": _auto_summary_state["total_runs"],
        "total_errors": _auto_summary_state["total_errors"],
    }

    return stats


@router.get("/health")
def health_api() -> dict[str, Any]:
    """Lightweight liveness check — no side effects, <100ms.

    Returns live counts queried at request time via store.get_stats() — the
    same code path used by /status.  If chunks or entities are zero, the
    database is genuinely empty (not stale or cached).  Reports
    status="degraded" with a db_error key if the database cannot be reached.
    """
    import palinode.api.server as _srv  # late lookup — see status_api
    # version surfaced here too so the lightweight liveness probe can
    # report the running release without a /status round-trip.
    result: dict[str, Any] = {"status": "ok", "version": __version__}

    # DB accessible + basic stats — delegate to store.get_stats() for chunk
    # count so the code path is identical to /status and cannot diverge.
    try:
        stats = store.get_stats()
        result["chunks"] = stats["total_chunks"]
        db = store.get_db()
        try:
            last_row = db.execute(
                "SELECT last_updated FROM chunks ORDER BY last_updated DESC LIMIT 1"
            ).fetchone()
            result["last_indexed"] = last_row["last_updated"] if last_row else None
            result["entities"] = db.execute(
                "SELECT count(DISTINCT entity_ref) FROM entities"
            ).fetchone()[0]
        finally:
            db.close()
    except Exception as e:
        result["status"] = "degraded"
        result["db_error"] = str(e)

    # Ollama reachable — liveness via the client's ping (raw GET, no breaker).
    result["ollama"] = _srv.get_ollama_client().ping(OllamaRole.EMBED)

    return result


@router.get("/health/watcher")
def watcher_health_api() -> dict[str, Any]:
    """Canary check: write a temp file, verify it gets indexed, clean up.

    Returns watcher_alive=True if the file was indexed within the timeout.
    Also checks systemd journal for recent watcher errors.
    """
    import uuid as _uuid
    canary_id = f"_canary-{_uuid.uuid4().hex[:8]}"
    canary_dir = os.path.join(config.palinode_dir, "insights")
    os.makedirs(canary_dir, exist_ok=True)
    canary_path = os.path.join(canary_dir, f"{canary_id}.md")
    canary_content = f"---\nid: {canary_id}\ncategory: insights\ntype: Insight\n---\nCanary check {canary_id}\n"

    result: dict[str, Any] = {"watcher_alive": False, "canary_id": canary_id}

    try:
        # Write canary file
        with open(canary_path, "w") as f:
            f.write(canary_content)

        # Wait for watcher to pick it up (check every 0.5s, up to 8s)
        import time as _time
        for _ in range(16):
            _time.sleep(0.5)
            db = store.get_db()
            row = db.execute(
                "SELECT id FROM chunks WHERE file_path = ?", (canary_path,)
            ).fetchone()
            db.close()
            if row:
                result["watcher_alive"] = True
                break

        # Check journal for recent watcher errors (last hour)
        try:
            journal = subprocess.run(
                ["journalctl", "--user", "-u", "palinode-watcher",
                 "--since", "1 hour ago", "--no-pager", "-p", "err"],
                capture_output=True, text=True, timeout=5,
            )
            errors = [
                line
                for line in journal.stdout.strip().split("\n")
                if line.strip() and "-- No entries --" not in line
            ]
            result["recent_errors"] = len(errors)
            if errors:
                result["last_error"] = errors[-1][:200]
        except Exception:
            result["recent_errors"] = -1  # couldn't check

    finally:
        # Clean up canary file and any indexed chunks
        try:
            os.remove(canary_path)
            store.delete_file_chunks(canary_path)
        except Exception:
            pass

    return result


@router.get("/health/auto-summary")
def auto_summary_health_api() -> dict[str, Any]:
    """Health check for the async auto_summary pipeline (#403).

    Auto_summary moved off the /save hot path; the watcher debounces calls to
    /generate-summaries instead. This endpoint lets external monitors detect a
    stalled pipeline without inspecting individual files.

    Status semantics:
      - "ok"        — auto_summary disabled, OR Ollama reachable AND
                      (pending < threshold OR pending == 0 with no last_run yet)
      - "degraded"  — Ollama reachable but pending backlog >= threshold,
                      OR last run had errors, OR last run was >stale_minutes
                      old with non-zero pending
      - "down"      — Ollama URL not reachable for the auto_summary model

    Thresholds are conservative defaults sized for a single-user dogfooding
    rig; tune via config if needed.
    """
    from palinode.core import parser
    import palinode.api.server as _srv  # late lookup — see status_api

    result: dict[str, Any] = {
        "enabled": config.auto_summary.enabled,
        "ollama_url": config.auto_summary.ollama_url or config.embeddings.primary.url,
        "model": config.auto_summary.model,
        "last_run_at": _auto_summary_state["last_run_at"],
        "last_run_count": _auto_summary_state["last_run_count"],
        "last_run_errors": _auto_summary_state["last_run_errors"],
        # description backfill shares this run; surface its counters too.
        "last_run_descriptions": _auto_summary_state["last_run_descriptions"],
        "last_run_description_errors": _auto_summary_state["last_run_description_errors"],
        "last_error": _auto_summary_state["last_error"],
        "total_runs": _auto_summary_state["total_runs"],
        "total_errors": _auto_summary_state["total_errors"],
    }

    if not config.auto_summary.enabled:
        result["status"] = "ok"
        result["reason"] = "auto_summary disabled in config"
        return result

    # Probe the auto_summary Ollama host (CHAT role — may differ from embed).
    # Liveness via the client's ping (raw GET, no circuit breaker). probe_url is
    # kept for the human-readable "down" reason below.
    probe_url = config.auto_summary.ollama_url or config.embeddings.primary.url
    ollama_reachable = _srv.get_ollama_client().ping(OllamaRole.CHAT)
    result["ollama_reachable"] = ollama_reachable

    # Count pending files in a single walk:
    #   - pending (summaries): core:true with no summary and content >= threshold.
    #   pending_descriptions: eligible memory files (see
    #     _is_description_eligible) missing a description field.
    # Both capped at 1000 — past that the count is a number, not an action item.
    pending = 0
    pending_descriptions = 0
    min_chars = config.auto_summary.min_content_chars
    try:
        for filepath in glob.glob(os.path.join(config.palinode_dir, "**/*.md"), recursive=True):
            if pending >= 1000 and pending_descriptions >= 1000:
                break
            try:
                with open(filepath) as f:
                    content = f.read()
                metadata, body = parser.parse_markdown(content)
                # description backlog — not core-gated, no length gate.
                # only count files that can actually persist a description
                # (the same eligibility predicate the backfill worklist uses), so
                # the count reflects real work and drains to a stable floor
                # instead of being pinned by structural / non-memory files.
                _rel = os.path.relpath(filepath, config.palinode_dir)
                if (
                    pending_descriptions < 1000
                    and not metadata.get("description")
                    and _is_description_eligible(_rel)
                ):
                    pending_descriptions += 1
                if pending >= 1000:
                    continue
                if not metadata.get("core"):
                    continue
                if metadata.get("summary"):
                    continue
                if len(body or "") < min_chars:
                    continue
                pending += 1
            except (OSError, ValueError):
                # Unreadable / unparseable file — skip; not this endpoint's job
                # to surface parser issues (use /lint or /doctor for that).
                continue
    except OSError as e:
        result["pending_count"] = -1
        result["pending_descriptions"] = -1
        result["pending_error"] = str(e)[:200]
    else:
        result["pending_count"] = pending
        result["pending_descriptions"] = pending_descriptions

    # Status decision tree.
    PENDING_THRESHOLD = 50          # >= this many backlog files = degraded
    STALE_MINUTES = 30              # last run older than this with pending = degraded

    if not ollama_reachable:
        result["status"] = "down"
        result["reason"] = f"Ollama not reachable at {probe_url}"
        return result

    last_run = _auto_summary_state["last_run_at"]
    last_run_dt = None
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        except ValueError:
            last_run_dt = None

    stale = False
    if last_run_dt is not None and pending > 0:
        if (_utc_now() - last_run_dt) > timedelta(minutes=STALE_MINUTES):
            stale = True

    if pending >= PENDING_THRESHOLD:
        result["status"] = "degraded"
        result["reason"] = f"pending backlog ({pending}) >= threshold ({PENDING_THRESHOLD})"
    elif _auto_summary_state["last_run_errors"] > 0 and pending > 0:
        result["status"] = "degraded"
        result["reason"] = f"last run had {_auto_summary_state['last_run_errors']} errors, {pending} still pending"
    elif stale:
        result["status"] = "degraded"
        result["reason"] = f"last run >{STALE_MINUTES}min ago, {pending} pending"
    else:
        result["status"] = "ok"

    return result


@router.get("/doctor")
def doctor_api(canary: bool = False, fast: bool = False) -> dict[str, Any]:
    """Run diagnostic checks; return structured report.

    Query params
    ------------
    fast:   When true, run only checks tagged "fast" (skips network probes
            and filesystem walks).  Target: <500ms.
    canary: When true, include canary-write checks (Phase 5 will populate
            these; for now the flag is accepted and passed through without
            error — no canary checks exist yet so the result set is the same
            as without the flag).
    """
    import json as _json
    from palinode.diagnostics.runner import run_all
    from palinode.diagnostics.types import DoctorContext
    from palinode.diagnostics.formatters import format_json

    ctx = DoctorContext(config=config)

    # Determine the tag filter.
    # fast=true  → only "fast"-tagged checks
    # canary=true → Phase 5 will add canary checks; accepted now, no-op
    # Neither flag → full run (all tags)
    tag_filter: str | None = "fast" if fast else None

    results = run_all(ctx, tag=tag_filter)

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    result_dicts = _json.loads(format_json(results))

    return {
        "version": __version__,  # deployed release, for operator triage
        "results": result_dicts,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
        },
        "params": {
            "fast": fast,
            "canary": canary,
        },
    }
