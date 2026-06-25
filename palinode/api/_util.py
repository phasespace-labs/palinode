"""Cross-cutting API plumbing with no single thematic home.

Extracted from the former ``routers/_shared.py`` junk drawer (#556). These are
the genuinely miscellaneous helpers + process-wide state that every router layer
leans on but that don't belong to any one themed module (path-safety,
rate-limiting, search shaping, write normalization): the sanitized-500 helper,
the UTC clock, the CWD→slug deriver, the retrieval-event logger, and the
reindex / auto-summary observability state dicts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from palinode.core.config import config
from palinode.core.retrieval_log import RetrievalLogger

logger = logging.getLogger("palinode.api")

# Issue #256: retrieval-event instrumentation (ADR-007 prerequisite).
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
    """Derive a project slug from a CWD path's basename (#145).

    Mirrors the slug rules used by `palinode init` so the slug a session
    self-reports matches the slug that scaffolding chose. Returns None if
    cwd is None / empty / produces an unusable slug.
    """
    if not cwd:
        return None
    base = os.path.basename(os.path.normpath(cwd))
    if not base:
        return None
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", base.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or None


# ── Reindex concurrency guard (#200) ─────────────────────────────────────────
# asyncio.Lock is safe because FastAPI runs on a single event loop.  The
# reindex work itself is synchronous (file I/O + Ollama HTTP) but the lock
# acquisition is async so concurrent HTTP callers fail fast rather than queue.
_reindex_lock = asyncio.Lock()
_reindex_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "files_processed": 0,
    "total_files": 0,
}

# #403: runtime state for auto_summary observability. Populated by
# /generate-summaries each run; surfaced via /status and /health/auto-summary
# so external monitors can detect a stalled summary pipeline.
# A separate URL is probed in /health/auto-summary because auto_summary may
# point at a different Ollama instance than embeddings (config-dependent).
_auto_summary_state: dict[str, Any] = {
    "last_run_at": None,           # ISO8601 Z of last /generate-summaries call
    "last_run_duration_ms": None,  # wallclock duration of last run
    "last_run_count": 0,           # summaries successfully generated in last run
    "last_run_errors": 0,          # per-file summary errors in last run
    # #405: the same /generate-summaries walk now also backfills the deferred
    # auto-description (moved off the /save hot path). Track description work
    # separately so operators can see the description pipeline independently of
    # the summary pipeline.
    "last_run_descriptions": 0,    # descriptions successfully generated in last run
    "last_run_description_errors": 0,  # per-file description errors in last run
    "last_error": None,            # most recent error message (truncated 200ch)
    "total_runs": 0,
    "total_errors": 0,
}
