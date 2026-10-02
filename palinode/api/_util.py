"""Cross-cutting API plumbing with no single thematic home.

Extracted from the former ``routers/_shared.py`` junk drawer. These are
the genuinely miscellaneous helpers + process-wide state that every router layer
leans on but that don't belong to any one themed module (path-safety,
rate-limiting, search shaping, write normalization): the sanitized-500 helper,
the UTC clock, the CWD→slug deriver, the retrieval-event logger, and the
reindex / auto-summary observability state dicts.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from palinode.core.config import config
from palinode.core.context_prime import ProjectResolution
from palinode.core.retrieval_log import RetrievalLogger

logger = logging.getLogger("palinode.api")

# Issue retrieval-event instrumentation (ADR-007 prerequisite).
# Lazy-initialised once at import time; honors PALINODE_INSTRUMENTATION_DISABLED env var.
_retrieval_logger = RetrievalLogger(
    config.memory_dir,
    enabled=config.instrumentation.capture_retrievals,
)


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _safe_500(e: Exception, context: str = "Internal error") -> HTTPException:
    """Log full exception, return sanitized 500 to client."""
    logger.exception(f"{context}: {e}")
    return HTTPException(status_code=500, detail=context)


def _project_from_cwd(cwd: str | None) -> str | None:
    """Compatibility slug view of the common project resolver."""
    from palinode.core.context_prime import resolve_context

    project = resolve_context(cwd=cwd).project
    return project.removeprefix("project/") if project else None


def effective_project_scope(context: Sequence[str] | None) -> ProjectResolution:
    """The project scope a delivery applies, and where it came from.

    Three cases, and the difference between the last two is load-bearing —
    what this returns is both what the delivery applies and what it reports,
    so the two can never disagree:

    - **No ``context`` at all** — the caller resolved nothing (the REST and
      plugin surfaces). The pinned ``PALINODE_PROJECT`` applies, and only
      that: never the API host's own directory, which is not the caller's
      repository.
    - **A ``context`` naming a project** — the caller already resolved scope
      on the surface closest to it (the MCP server and the CLI both do,
      through the one shared resolver) and that decision stands: ``explicit``.
    - **A ``context`` that names no project, empty list included** — the
      caller stated a scope with no project in it. That is a decision too, so
      no project is applied and none is reported. It is how a caller opts out
      of a pinned server's scope; ``palinode search --no-context`` sends
      exactly this.
    """
    from palinode.core.context_prime import resolve_context

    if context is None:
        return resolve_context()
    project = next((ref for ref in context
                    if isinstance(ref, str) and ref.startswith("project/")), None)
    return resolve_context(project=project) if project else ProjectResolution(None, "none")


# ── Reindex concurrency guard ─────────────────────────────────────────
# The synchronous reindex handler runs in FastAPI's threadpool.  A process-wide
# threading lock provides mutual exclusion across worker threads; callers use a
# non-blocking acquire so a concurrent request fails fast instead of queueing.
_reindex_lock = threading.Lock()
_reindex_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "files_processed": 0,
    "total_files": 0,
}

# runtime state for auto_summary observability. Populated by
# /generate-summaries each run; surfaced via /status and /health/auto-summary
# so external monitors can detect a stalled summary pipeline.
# A separate URL is probed in /health/auto-summary because auto_summary may
# point at a different Ollama instance than embeddings (config-dependent).
_auto_summary_state: dict[str, Any] = {
    "last_run_at": None,           # ISO8601 Z of last /generate-summaries call
    "last_run_duration_ms": None,  # wallclock duration of last run
    "last_run_count": 0,           # summaries successfully generated in last run
    "last_run_errors": 0,          # per-file summary errors in last run
    # the same /generate-summaries walk now also backfills the deferred
    # auto-description (moved off the /save hot path). Track description work
    # separately so operators can see the description pipeline independently of
    # the summary pipeline.
    "last_run_descriptions": 0,    # descriptions successfully generated in last run
    "last_run_description_errors": 0,  # per-file description errors in last run
    "last_error": None,            # most recent error message (truncated 200ch)
    "total_runs": 0,
    "total_errors": 0,
}
