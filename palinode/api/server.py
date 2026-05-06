"""
Palinode API Server

FastAPI application that serves Palinode endpoints over HTTP.
Provides semantic search capabilities (`/search`), saves new memories 
(`/save`), polls system status (`/status`), and handles ingestion tasks (`/ingest`).
"""
from __future__ import annotations

import os
import json
import logging
import time
import re
import yaml
import httpx
import hashlib
import subprocess
import glob
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from palinode.core import store, embedder, git_tools
from palinode.core import triggers, entity_graph
from palinode.core import memory_paths as _memory_paths
from palinode.core import git_persistence
from palinode.core.memory_paths import (
    MemoryPathError,
    MemoryPathNotFound,
    MemoryPathTraversal,
)
from palinode.core.config import config
from palinode.core.db import utc_now as _utc_now
from palinode.core.retrieval_log import RetrievalLogger
from palinode.core.defaults import (
    SAVE_SOURCE_API_DEFAULT,
    SAVE_SOURCE_HEADER,
    SESSION_END_DEDUP_THRESHOLD,
    SESSION_END_DEDUP_WINDOW_MINUTES,
)
from palinode.core.similarity import cosine as _cosine
from palinode.core.wiki import (
    CATEGORY_TO_ENTITY_PREFIX as _CATEGORY_TO_ENTITY_PREFIX,
    WIKI_FOOTER_MARKER as _WIKI_FOOTER_MARKER,
    SAFE_SLUG_RE as _SAFE_SLUG_RE,
    safe_wiki_slug as _safe_wiki_slug,
    apply_wiki_footer as _apply_wiki_footer,
    normalize_entities as _normalize_entities,
)
from palinode.core.summarize import (
    extract_first_line as _extract_first_line,
    wrap_user_content_for_llm as _wrap_user_content_for_llm,
    generate_description as _generate_description,
    generate_summary as _generate_summary,
    inject_summary as _inject_summary,
)

# Alias so the single call site using _cosine_similarity keeps working.
_cosine_similarity = _cosine


logger = logging.getLogger("palinode.api")
logger.setLevel(getattr(logging, config.services.api.log_level.upper(), logging.INFO))

# Issue #256: retrieval-event instrumentation (ADR-007 prerequisite).
# Lazy-initialised once at import time; honors PALINODE_INSTRUMENTATION_DISABLED env var.
_retrieval_logger = RetrievalLogger(
    config.memory_dir,
    enabled=config.instrumentation.capture_retrievals,
)


# ── Middleware & rate-limit imports (#325) ──────────────────────────────────
# Definitions extracted to palinode.api.middleware and palinode.api.rate_limit;
# backward-compatible aliases preserved so tests and internal call sites that
# reference the underscore-prefixed names continue to work.
from palinode.api.middleware import (
    SECRET_PATTERNS as _SECRET_PATTERNS,
    redact_secrets as _redact_secrets,
    SecretRedactingFilter,
    JsonlFormatter,
    BearerAuthMiddleware as _BearerAuthMiddleware,
    BodySizeLimitMiddleware as _BodySizeLimitMiddleware,
    BodyTooLargeError as _BodyTooLargeError,
    parse_cors_origins as _parse_cors_origins,
    load_api_token as _load_api_token,
    validate_auth_config as _validate_auth_config_impl,
)
from palinode.api.rate_limit import (
    _rate_counters,
    WINDOW as _RATE_LIMIT_WINDOW,
    LIMIT_SEARCH as _RATE_LIMIT_SEARCH,
    LIMIT_WRITE as _RATE_LIMIT_WRITE,
    MAX_KEYS as _RATE_LIMIT_MAX_KEYS,
    prune_counters as _prune_rate_counters,
    check as _check_rate_limit,
)


# Attach handlers to the "palinode" parent logger so all palinode.* modules
# (palinode.api, palinode.write_time, palinode.consolidation, etc.) share them.
# This ensures unified observability across background workers and request
# handlers without each module configuring its own handlers.
_parent_logger = logging.getLogger("palinode")
_parent_logger.setLevel(getattr(logging, config.services.api.log_level.upper(), logging.INFO))

# Install the secret-redaction filter at the parent so every palinode.* logger
# inherits it (logging filters on a logger are applied in addition to handler
# filters; placing it on the parent and on the root catches both stack traces
# routed through palinode loggers and any third-party logger that happens to
# log a secret-bearing string).
_secret_filter = SecretRedactingFilter()
_parent_logger.addFilter(_secret_filter)
logging.getLogger().addFilter(_secret_filter)

sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
sh.addFilter(_secret_filter)
_parent_logger.addHandler(sh)

os.makedirs(os.path.join(config.palinode_dir, "logs"), exist_ok=True)
fh = logging.FileHandler(os.path.join(config.palinode_dir, config.logging.operations_log))
fh.setFormatter(JsonlFormatter())
fh.addFilter(_secret_filter)
_parent_logger.addHandler(fh)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and background workers on startup."""
    _startup_logger = logging.getLogger("palinode.config")

    # Validate resolved paths and surface misconfigurations before first DB touch
    _startup_logger.info(
        "palinode.config: memory_dir=%s db_path=%s",
        config.memory_dir,
        config.db_path,
    )
    path_warnings = config.validate_paths()
    for warning in path_warnings:
        _startup_logger.warning(warning)

    # Refuse to start if the db_path parent doesn't exist — sqlite3.connect()
    # would silently auto-create the DB in a non-existent directory (raising an
    # OperationalError on first write), producing silent 500s identical to #201.
    _db_parent = Path(config.db_path).parent
    if not _db_parent.exists():
        raise RuntimeError(
            f"Cannot start: db_path parent directory does not exist: {_db_parent}. "
            f"Create the directory or update db_path in palinode.config.yaml."
        )

    try:
        store.init_db()
    except RuntimeError as exc:
        # #188: misconfiguration guard in store._ensure_db() — DB missing but
        # memory_dir has .md files. Log CRITICAL so the operator sees it in
        # journalctl before the process exits.
        logging.getLogger("palinode.api").critical(
            "Database misconfiguration detected — refusing to start: %s", exc
        )
        raise

    # Tier 2a (ADR-004): write-time contradiction check worker
    if config.consolidation.write_time.enabled:
        try:
            from palinode.consolidation import write_time
            await write_time.start_worker(app.state)
        except Exception as e:  # noqa: BLE001
            # Worker startup failures must never prevent the API from running
            logger = logging.getLogger("palinode.api")
            logger.error(f"write-time worker failed to start: {e}")

    yield

    # Shutdown: cancel worker task if it was started
    if config.consolidation.write_time.enabled:
        try:
            from palinode.consolidation import write_time
            await write_time.stop_worker(app.state)
        except Exception as e:  # noqa: BLE001
            logger = logging.getLogger("palinode.api")
            logger.error(f"write-time worker failed to stop cleanly: {e}")

app = FastAPI(title="Palinode API", lifespan=lifespan)

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

# ── Security middleware ──────────────────────────────────────────────────────

# CORS: restrict to configured origins (default: localhost only).
# Definitions now live in palinode.api.middleware (#325); _parse_cors_origins
# imported above.

_cors_origins = _parse_cors_origins(
    os.environ.get(
        "PALINODE_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
)
logger.info("CORS origins: %s", ", ".join(_cors_origins))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Bind-intent flag: must be resolved before _validate_auth_config below
# can reference it. The matching unsafe-bind warning still lives further
# down the file; this assignment is the single source of truth and the
# warning block reuses the value.
_api_host = os.environ.get("PALINODE_API_HOST", config.services.api.host)
_bind_intent_public = os.environ.get("PALINODE_API_BIND_INTENT", "").lower() == "public"

# ── Bearer token auth ──────────────────────────────────────────────────────
# Definitions now live in palinode.api.middleware (#325); imported above as
# _load_api_token, _BearerAuthMiddleware, _validate_auth_config_impl, etc.

_api_token: str | None = _load_api_token()


def _validate_auth_config(token: str | None) -> None:
    """Thin wrapper preserving the original one-arg call-site signature.

    The extracted ``validate_auth_config`` takes ``bind_intent_public`` as a
    keyword arg; this wrapper closes over the module-level flag so existing
    callers (and the import-time invocation below) keep working.
    """
    _validate_auth_config_impl(token, bind_intent_public=_bind_intent_public)


# Fire the gate at import time so it triggers under any startup path.
_validate_auth_config(_api_token)

# Registered after CORS so CORS-applied origin headers wrap auth failures.
app.add_middleware(_BearerAuthMiddleware, token=_api_token)
if _api_token is not None:
    logger.info("API bearer-token auth: enabled")
else:
    logger.info("API bearer-token auth: disabled (no PALINODE_API_TOKEN)")

# Request body size limit (default 5MB)
_MAX_REQUEST_BYTES = int(os.environ.get("PALINODE_MAX_REQUEST_BYTES", 5 * 1024 * 1024))

app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)

# Rate limiting — state and functions imported from palinode.api.rate_limit (#325).

# Startup warning for unsafe binding.
# Set PALINODE_API_BIND_INTENT=public to suppress the warning for intentional
# network-exposed deployments (e.g., Tailscale). Without the env var, the
# warning fires on every 0.0.0.0 start. Fixes #253.
# (_api_host and _bind_intent_public are resolved earlier so the bearer-auth
# startup gate can reference them; this block reuses the same values.)
# B104 rationale - "0.0.0.0" here is a literal compared to the resolved host;
# the actual bind decision is gated on PALINODE_API_BIND_INTENT=public per #253.
if _api_host == "0.0.0.0" and not _bind_intent_public:  # nosec B104
    if _api_token is None:
        logger.warning(
            "API binding to 0.0.0.0 — accessible from any network. "
            "No authentication is configured. Set PALINODE_API_HOST=127.0.0.1 for local-only access. "
            "Set PALINODE_API_BIND_INTENT=public to suppress this warning for intentional "
            "network-exposed deployments (e.g., Tailscale)."
        )
    else:
        logger.info(
            "API binding to 0.0.0.0 with PALINODE_API_TOKEN configured — bearer auth required."
        )
elif _api_host == "0.0.0.0" and _bind_intent_public:  # nosec B104
    logger.debug(
        "API binding to 0.0.0.0 — PALINODE_API_BIND_INTENT=public set; "
        "binding warning suppressed."
    )

# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_500(e: Exception, context: str = "Internal error") -> HTTPException:
    """Log full exception, return sanitized 500 to client."""
    logger.exception(f"{context}: {e}")
    return HTTPException(status_code=500, detail=context)


def _memory_base_dir() -> str:
    """Return the canonical memory root.

    Delegates to :func:`palinode.core.memory_paths.memory_base_dir`.
    """
    return _memory_paths.memory_base_dir()


def _resolve_memory_path(file_path: str) -> tuple[str, str]:
    """Resolve a relative memory path, mapping typed errors to HTTPException.

    Delegates to :func:`palinode.core.memory_paths.resolve` (#329).
    Null-byte inputs → 400; all other traversal failures → 403.
    """
    try:
        return _memory_paths.resolve(file_path)
    except MemoryPathTraversal:
        # Distinguish null-byte (400) from structural traversal (403) to
        # preserve the existing HTTP status codes asserted by tests.
        if "\x00" in file_path:
            raise HTTPException(status_code=400, detail="Invalid path")
        raise HTTPException(status_code=403, detail="Invalid path")
    except MemoryPathError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _open_memory_file_text(resolved_path: str) -> str:
    """Open a resolved memory path, rejecting symlinks on POSIX.

    Delegates to :func:`palinode.core.memory_paths.read_text` (#329).
    MemoryPathNotFound is re-raised as FileNotFoundError so existing
    call sites that catch FileNotFoundError keep working.
    """
    try:
        return _memory_paths.read_text(resolved_path)
    except MemoryPathNotFound as exc:
        raise FileNotFoundError(str(exc)) from exc

def _resolve_source(req_source: str | None, request: "Request | None") -> str:
    """Resolve the source-surface attribution for a write.

    Precedence (ADR-010 / #167):
      1. Explicit ``source`` field in the request body — caller's intent wins.
      2. ``X-Palinode-Source`` HTTP header — set automatically by CLI/MCP.
      3. ``PALINODE_SOURCE`` environment variable — operator override.
      4. ``"api"`` default — used when nothing above is set.
    """
    if req_source:
        return req_source
    if request is not None:
        # FastAPI normalizes header names to lowercase on read; supply both
        # spellings to be safe across stacks.
        hdr = request.headers.get(SAVE_SOURCE_HEADER) or request.headers.get(
            SAVE_SOURCE_HEADER.lower()
        )
        if hdr:
            return hdr
    return os.environ.get("PALINODE_SOURCE", SAVE_SOURCE_API_DEFAULT)


# ─────────────────────────────────────────────────────────────────────────────


class SearchRequest(BaseModel):
    query: str
    category: str | None = None
    limit: int | None = config.search.default_limit
    threshold: float | None = config.search.api_threshold
    hybrid: bool | None = None
    date_after: str | None = None
    date_before: str | None = None
    context: list[str] | None = None  # Entity refs for ambient context boost (ADR-008)
    include_daily: bool | None = False  # Skip daily/ penalty when True (#93)
    # #141: filter by memory `type` frontmatter (one of PersonMemory, Decision,
    # ProjectSnapshot, Insight, ResearchRef, ActionItem). Independent of `category`
    # which filters by directory. Applied as a post-fetch filter; pass multiple
    # types to OR them.
    types: list[str] | None = None
    # #141: relative recency window. If set, derives an effective `date_after`
    # of `now - since_days` days. Combined with explicit `date_after` by taking
    # the later (more restrictive) of the two.
    since_days: int | None = None

class SearchAssociativeRequest(BaseModel):
    query: str
    seed_entities: list[str] | None = None
    limit: int | None = 5

class TriggerRequest(BaseModel):
    description: str
    memory_file: str
    trigger_id: str | None = None
    threshold: float | None = 0.75
    cooldown_hours: int | None = 24

class CheckTriggersRequest(BaseModel):
    query: str
    cooldown_bypass: bool | None = False

class DedupSuggestRequest(BaseModel):
    """Find existing files semantically near the supplied draft content (#210).

    Used by the LLM at write-time to decide "create new vs update existing".
    Both ``min_similarity`` and ``top_k`` are kwarg-tunable per the design
    doc — defaults match the BGE-M3 thresholds research-validated in
    `artifacts/obsidian-integration/design.md`.
    """
    content: str
    min_similarity: float | None = 0.80
    top_k: int | None = 5
    # Threshold above which a candidate is flagged ``strong_dup=true`` —
    # "near-paraphrase territory" per the design doc; LLM should usually
    # update rather than create when this fires.
    strong_dup_threshold: float | None = 0.90


class OrphanRepairRequest(BaseModel):
    """Find files semantically near a broken `[[wikilink]]` target (#210).

    The LLM uses the candidate slate to propose a redirect or seed a new
    target file with informed context.  ``min_similarity`` defaults are
    looser than ``dedup_suggest`` because we want a wider candidate slate
    here — the LLM picks one or none.
    """
    broken_link: str
    min_similarity: float | None = 0.65
    top_k: int | None = 10


class ClusterNeighborsRequest(BaseModel):
    """Find semantically related files not already linked to/from file_path (#235).

    Used by the LLM during wiki-maintenance passes to surface implicit
    relationships that no ``[[wikilink]]`` yet captures.  Default threshold
    0.70 sits between the dedup default (0.80) and the orphan-repair default
    (0.65) — looser than "potential duplicate", tighter than "anything vaguely
    related".
    """

    file_path: str
    min_similarity: float | None = 0.70
    top_k: int | None = 10


class TopicCoverageRequest(BaseModel):
    """Check whether any wiki page already covers a topic phrase (#235).

    The LLM calls this BEFORE ingesting new content to ask "is this
    redundant?".  Different framing from ``dedup_suggest``: the input is a
    short topic phrase (not full draft content), and the return is a simple
    ``{covered, best_match, similarity}`` dict rather than a ranked list.
    Default threshold 0.78 — between dedup (0.80) and cluster (0.70).
    """

    query: str
    min_similarity: float | None = 0.78


class SaveRequest(BaseModel):
    content: str
    type: str
    slug: str | None = None
    entities: list[str] | None = None
    metadata: Any | None = None
    core: bool | None = None
    source: str | None = None
    confidence: float | None = None
    #: Optional human-readable title.  When set, it's stored in frontmatter
    #: and used for display in lists/search results.  ADR-010 / #166.
    title: str | None = None
    #: Sugar: ``project="foo"`` is equivalent to appending ``"project/foo"``
    #: to ``entities``.  ADR-010 / #159.  If both are given and there's a
    #: mismatch, both values land — same as supplying ``entities=["project/a",
    #: "project/b"]`` directly.
    project: str | None = None
    #: Optional dict of SDLC object references (GitLab MR/issue/pipeline,
    #: GitHub PR, Linear, Jira, etc.).  Free-form key/value pairs — recognised
    #: keys get pretty rendering; others pass through unchanged (#115).
    #: Typed as Any-value so Pydantic doesn't reject nested values before
    #: our parser helper can soft-warn and drop them.
    external_refs: dict[str, Any] | None = None


@app.get("/list")
def list_api(category: str | None = None, core_only: bool = False) -> list[dict[str, Any]]:
    import glob
    from palinode.core import parser
    
    results = []
    base_dir = _memory_base_dir()
    search_pattern = os.path.join(base_dir, "**/*.md")
    
    skip_dirs = {"daily", "archive", "inbox", "logs", "prompts"}
    
    for filepath in glob.glob(search_pattern, recursive=True):
        try:
            if os.path.commonpath([base_dir, os.path.realpath(filepath)]) != base_dir:
                continue
        except ValueError:
            continue
        rel_path = os.path.relpath(filepath, base_dir)
        parts = rel_path.split(os.sep)
        
        if parts[0] in skip_dirs:
            continue
            
        if category and parts[0] != category:
            continue
            
        try:
            with open(filepath, "r") as f:
                content = f.read()
            metadata, _ = parser.parse_markdown(content)
            
            is_core = bool(metadata.get("core", False))
            if core_only and not is_core:
                continue
                
            results.append({
                "file": rel_path,
                "name": metadata.get("name") or parts[-1].replace('.md', ''),
                "category": metadata.get("category", parts[0]),
                "core": is_core,
                "summary": metadata.get("summary", ""),
                "last_updated": metadata.get("last_updated", ""),
                "entities": metadata.get("entities", []),
                "size_bytes": os.path.getsize(filepath)
            })
        except Exception:
            pass

    # Sort newest first so listing surfaces recent activity.
    # `last_updated` may be a string (typical) or a datetime (yaml auto-converts
    # ISO timestamps without quotes); stringify in the key so mixed types don't
    # raise. Empty string sorts last in descending order — correct for files
    # with missing or malformed frontmatter.
    results.sort(key=lambda r: str(r.get("last_updated") or ""), reverse=True)
    return results


@app.get("/read")
def read_api(file_path: str, meta: bool = False) -> dict[str, Any]:
    from palinode.core import parser

    candidates = [file_path]
    if not file_path.endswith(".md"):
        candidates.append(f"{file_path}.md")

    # L5: open candidates directly with O_NOFOLLOW (POSIX) so a symlink swap
    # within memory_dir between the existence check and the open cannot
    # redirect us to a sensitive file. _resolve_memory_path already keeps
    # us inside memory_dir; this closes the residual symlink-swap window.
    # Falls back to a try-open for non-POSIX platforms.
    resolved = ""
    content = ""
    for candidate in candidates:
        _, resolved_candidate = _resolve_memory_path(candidate)
        try:
            content = _open_memory_file_text(resolved_candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _safe_500(exc, "File read failed")
        file_path = candidate
        resolved = resolved_candidate
        break

    if not resolved:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        result = {
            "file": file_path,
            "content": content,
            "size_bytes": len(content.encode("utf-8")),
        }

        if meta:
            metadata, _ = parser.parse_markdown(content)
            result["frontmatter"] = metadata

        # Issue #256: emit retrieval event (explicit — direct /read call).
        _retrieval_logger.record_file_read(
            file_path,
            source="palinode_read",
            mode="explicit",
        )

        return result
    except HTTPException:
        # Path / 404 errors should propagate untouched — they are not 500s.
        raise
    except (ValueError, KeyError) as e:
        # Frontmatter parser failures are 500s with a safe message.
        raise _safe_500(e, "File read failed")


def _compute_effective_date_after(req: SearchRequest) -> str | None:
    """Combine explicit date_after with since_days; pick the more restrictive.

    Returns the ISO-8601 string (UTC, "Z" suffix) representing the earliest
    creation/update time a result is allowed to have. ``since_days`` derives
    `now - since_days days`. If both are set, takes the later (later → more
    restrictive). If neither is set, returns None.
    """
    derived = None
    if req.since_days and req.since_days > 0:
        threshold_dt = _utc_now() - timedelta(days=req.since_days)
        derived = threshold_dt.isoformat().replace("+00:00", "Z")
    explicit = req.date_after
    if derived and explicit:
        return derived if derived > explicit else explicit
    return derived or explicit


def _filter_types(results: list[dict[str, Any]], types: list[str] | None) -> list[dict[str, Any]]:
    """Drop results whose frontmatter `type` isn't in the allowed list (#141).

    Empty / None ``types`` is a no-op. Filter is OR-style: a result keeps if its
    type matches any of the values.
    """
    if not types:
        return results
    allowed = set(types)
    return [r for r in results if r.get("metadata", {}).get("type") in allowed]


@app.post("/search")
def search_api(req: SearchRequest, request: Request = None) -> list[dict[str, Any]]:
    """Semantic vector search against cached `.palinode.db` chunks.

    Empty query routes to recency-only mode (#141): returns the most recent
    chunks ordered by created_at desc, optionally filtered by `types` and
    `since_days`. Skips embedding entirely.

    Returns:
        list[dict[str, Any]]: List payload sequence matching the criteria boundaries.

    # Security audit (I2, 2026-04-30):
    # - All SQL goes through store.search / store.search_hybrid /
    #   store.list_recent / store.search_fts; every cursor.execute() in
    #   those helpers uses ? placeholders (no f-string interpolation of
    #   user input into SQL). Verified directly in palinode/core/store.py.
    # - No LLM call inside search_api itself — only embedder.embed() on
    #   the query string, which is a vector model and not a prompt
    #   injection surface.
    # - Result sets are bounded: req.limit (capped server-side via
    #   config.search.default_limit when unset), with an internal
    #   over-fetch factor of 5x when `types` filter is active. The
    #   over-fetch ceiling is itself bounded by the underlying SQL
    #   LIMIT clause.
    # - FTS5 query string is sanitized via store.sanitize_fts_query()
    #   before MATCH, defending against operator-injection (`OR`, `NEAR`).
    """
    if request:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip, "search", _RATE_LIMIT_SEARCH):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    try:
        effective_date_after = _compute_effective_date_after(req)
        limit = req.limit or config.search.default_limit

        # #141: empty query → recency-only mode. Skip embedding, query chunks
        # directly ordered by created_at desc, apply types/date_after filter.
        if not req.query.strip():
            return store.list_recent(
                types=req.types,
                category=req.category,
                date_after=effective_date_after,
                date_before=req.date_before,
                limit=limit,
            )

        # ADR-008: Augment query with project context before embedding
        embed_query = req.query
        if req.context and config.context.enabled and config.context.embed_augment:
            # Extract project name from entity ref (e.g., "project/palinode" → "palinode")
            project_names = [e.split("/", 1)[-1] for e in req.context if "/" in e]
            if project_names:
                embed_query = f"In the context of {', '.join(project_names)}: {req.query}"

        query_emb = embedder.embed(embed_query)
        if not query_emb:
            return []

        use_hybrid = req.hybrid if req.hybrid is not None else config.search.hybrid_enabled

        # Over-fetch when types filter is in play so we still have a chance of
        # returning `limit` results after the post-fetch type filter (#141).
        store_limit = limit * 5 if req.types else limit

        if use_hybrid:
            results = store.search_hybrid(
                query_text=req.query,
                query_embedding=query_emb,
                category=req.category,
                top_k=store_limit,
                threshold=req.threshold or config.search.api_threshold,
                hybrid_weight=config.search.hybrid_weight,
                date_after=effective_date_after,
                date_before=req.date_before,
                context_entities=req.context,
                include_daily=bool(req.include_daily),
            )
        else:
            results = store.search(
                query_embedding=query_emb,
                category=req.category,
                top_k=store_limit,
                threshold=req.threshold or config.search.api_threshold,
                date_after=effective_date_after,
                date_before=req.date_before,
                context_entities=req.context,
                include_daily=bool(req.include_daily),
            )

        # Apply types filter post-fetch (#141), then trim to caller's limit.
        results = _filter_types(results, req.types)
        final = results[:limit]

        # Issue #256: emit retrieval events (explicit — came in via /search API).
        # Source attribution: the X-Palinode-Source header tells us the surface
        # (mcp → "palinode_search", cli → "cli_search", api → "api_search").
        _search_source = "api_search"
        if request is not None:
            from palinode.core.defaults import SAVE_SOURCE_HEADER
            hdr = request.headers.get(SAVE_SOURCE_HEADER, "")
            if hdr == "mcp":
                _search_source = "palinode_search"
            elif hdr == "cli":
                _search_source = "cli_search"
        _retrieval_logger.record_search_results(
            final,
            query=req.query,
            source=_search_source,
            mode="explicit",
            session_id=None,
        )
        return final
    except Exception as e:
        raise _safe_500(e, "Search failed")


@app.post("/search-associative")
def search_associative_api(req: SearchAssociativeRequest) -> list[dict[str, Any]]:
    """Entity graph spreading activation recall."""
    try:
        seed_entities = req.seed_entities
        if not seed_entities:
            seed_entities = entity_graph.detect_entities_in_text(req.query)
            
        results = store.search_associative(
            query_text=req.query,
            seed_entities=seed_entities,
            top_k=req.limit or 5
        )
        return results
    except Exception as e:
        raise _safe_500(e, "Associative search failed")


@app.post("/triggers")
def create_trigger_api(req: TriggerRequest) -> dict[str, Any]:
    """Register a new prospective trigger."""
    import uuid
    try:
        trigger_id = req.trigger_id or str(uuid.uuid4())
        emb = embedder.embed(req.description)
        if not emb:
            raise ValueError("Failed to embed trigger description")
            
        triggers.add_trigger(
            trigger_id=trigger_id,
            description=req.description,
            memory_file=req.memory_file,
            embedding=emb,
            threshold=req.threshold or 0.75,
            cooldown_hours=req.cooldown_hours or 24
        )
        return {"id": trigger_id, "status": "created"}
    except Exception as e:
        raise _safe_500(e, "Trigger creation failed")


@app.get("/triggers")
def list_triggers_api() -> list[dict[str, Any]]:
    """List all registered triggers."""
    return triggers.list_triggers()


@app.delete("/triggers/{trigger_id}")
def delete_trigger_api(trigger_id: str) -> dict[str, str]:
    """Remove a trigger."""
    triggers.delete_trigger(trigger_id)
    return {"status": "deleted"}


@app.post("/check-triggers")
def check_triggers_api(req: CheckTriggersRequest) -> list[dict[str, Any]]:
    """Check context against prospective triggers."""
    try:
        emb = embedder.embed(req.query)
        if not emb:
            return []
        results = triggers.check_triggers(
            query_embedding=emb,
            cooldown_bypass=req.cooldown_bypass or False
        )
        return results
    except Exception as e:
        raise _safe_500(e, "Trigger check failed")


# ── Embedding tools (#210) — Obsidian wiki maintenance helpers ──────────────


def _read_memory_body(file_path: str) -> str | None:
    """Read a memory file's full body for re-embedding.  Returns None on miss.

    L5: try-open with O_NOFOLLOW (POSIX) instead of exists+open so a symlink
    swap cannot redirect the read between the check and the open.
    """
    candidates = [file_path]
    if not file_path.endswith(".md"):
        candidates.append(f"{file_path}.md")
    for candidate in candidates:
        try:
            _, resolved = _resolve_memory_path(candidate)
        except HTTPException:
            continue
        try:
            return _open_memory_file_text(resolved)
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return None


def _embedding_candidates(
    query_embedding: list[float],
    top_k: int,
    over_fetch: int = 4,
) -> list[dict[str, Any]]:
    """Run the existing vector index for an over-fetched candidate slate.

    The corpus index was built without the wikilink-stripping preprocessing;
    we use it only to narrow down which files to re-embed.  Final ranking
    (caller's responsibility) re-embeds each candidate's preprocessed body so
    the cosine score is apples-to-apples with the preprocessed query.
    """
    if not query_embedding:
        return []
    return store.search(
        query_embedding=query_embedding,
        top_k=top_k * over_fetch,
        threshold=0.0,  # caller filters; we want the wider slate
    )


def _rerank_with_preprocessing(
    query_preprocessed: str,
    candidates: list[dict[str, Any]],
    min_similarity: float,
    top_k: int,
) -> list[dict[str, Any]]:
    """Re-embed each candidate's preprocessed body, score against the
    preprocessed query, and return the top_k above ``min_similarity``.

    This is the strip-at-query-AND-strip-at-rerank pipeline.  The corpus
    index stays raw (so existing ``palinode_search`` behaviour is unchanged);
    the dedup/orphan tools pay a small re-embed cost per candidate to get
    formatting-noise-free similarity.
    """
    from palinode.core.embedding_preprocess import preprocess_for_similarity

    query_emb = embedder.embed(query_preprocessed)
    if not query_emb:
        return []

    # Group by file_path so we re-embed each file once, not per chunk.  The
    # candidate list from store.search() may contain multiple chunks of the
    # same file; the wiki tools care about file-level dedup.
    seen: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        fp = cand.get("file_path", "")
        if not fp or fp in seen:
            continue
        body = _read_memory_body(fp)
        if body is None:
            # Fall back to the chunk content if the file is gone — better
            # than dropping the candidate silently.
            body = cand.get("content", "")
        preprocessed = preprocess_for_similarity(body)
        if not preprocessed:
            continue
        cand_emb = embedder.embed(preprocessed)
        if not cand_emb:
            continue
        sim = _cosine(query_emb, cand_emb)
        if sim < min_similarity:
            continue
        snippet = preprocessed[:200].strip()
        seen[fp] = {
            "file_path": fp,
            "similarity": round(sim, 4),
            "snippet": snippet,
        }

    ranked = sorted(seen.values(), key=lambda r: r["similarity"], reverse=True)
    return ranked[:top_k]


@app.post("/dedup-suggest")
def dedup_suggest_api(req: DedupSuggestRequest) -> list[dict[str, Any]]:
    """Return existing files semantically near the supplied draft content.

    Preprocessing pipeline (P1 per design doc): strip frontmatter, strip the
    auto-generated `## See also` footer, strip `[[wikilink]]` decoration —
    applied BOTH to the incoming draft AND to each candidate's body before
    re-embedding.  Without this, every note linking the same entities looks
    like a duplicate of every other one.

    Each result carries a ``strong_dup: bool`` flag — true when similarity
    crosses the strong-dup threshold (default 0.90).  The LLM uses this to
    pick "create new" vs "update existing".
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.80
        top_k = req.top_k or 5
        strong_threshold = (
            req.strong_dup_threshold if req.strong_dup_threshold is not None else 0.90
        )

        preprocessed_query = preprocess_for_similarity(req.content)
        if not preprocessed_query:
            return []

        # Initial candidate slate — over-fetched, filter-free.  The caller's
        # min_similarity gates only the post-rerank cosine score, not the
        # initial vector recall.
        query_emb = embedder.embed(preprocessed_query)
        if not query_emb:
            return []
        candidates = _embedding_candidates(query_emb, top_k=top_k, over_fetch=4)

        ranked = _rerank_with_preprocessing(
            query_preprocessed=preprocessed_query,
            candidates=candidates,
            min_similarity=min_similarity,
            top_k=top_k,
        )
        for r in ranked:
            r["strong_dup"] = r["similarity"] >= strong_threshold
        return ranked
    except Exception as e:
        raise _safe_500(e, "Dedup suggest failed")


@app.post("/orphan-repair")
def orphan_repair_api(req: OrphanRepairRequest) -> list[dict[str, Any]]:
    """Return existing files semantically near a broken `[[wikilink]]` target.

    The LLM proposes a redirect (rename the link to point at one of the
    returned files) or creates the missing target with informed context
    (knowing what existing pages are nearby in semantic space).

    The input ``broken_link`` may be the raw link text (``[[alice-meeting]]``)
    or just the target word — both are accepted; the wikilink stripper
    normalizes either form.
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.65
        top_k = req.top_k or 10

        # Accept either `[[name]]` or bare `name`.  Preprocessing handles both.
        preprocessed_query = preprocess_for_similarity(req.broken_link)
        # Replace hyphens with spaces so a slug like ``alice-meeting`` reads
        # as natural language to the embedder.  This is intent-preserving:
        # we want semantic neighbours of the target *concept*.
        preprocessed_query = preprocessed_query.replace("-", " ").replace("_", " ").strip()
        if not preprocessed_query:
            return []

        query_emb = embedder.embed(preprocessed_query)
        if not query_emb:
            return []
        candidates = _embedding_candidates(query_emb, top_k=top_k, over_fetch=4)

        return _rerank_with_preprocessing(
            query_preprocessed=preprocessed_query,
            candidates=candidates,
            min_similarity=min_similarity,
            top_k=top_k,
        )
    except Exception as e:
        raise _safe_500(e, "Orphan repair failed")


@app.post("/cluster-neighbors")
def cluster_neighbors_api(req: ClusterNeighborsRequest) -> list[dict[str, Any]]:
    """Return top-K semantically related files not already linked to/from file_path.

    Extracts all ``[[wikilinks]]`` in the source file and in every other file
    that links TO it, then excludes those already-linked files from the
    candidate slate.  The remaining candidates are re-ranked with the
    preprocessing pipeline (strip frontmatter, auto-footer, wikilink
    decoration) and filtered by ``min_similarity``.

    Designed for the Obsidian wiki-maintenance LLM: surfaces implicit
    relationships that no wikilink yet captures so the LLM can propose
    new cross-links.
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.70
        top_k = req.top_k or 10

        # Read the source file body.
        body = _read_memory_body(req.file_path)
        if body is None:
            return []

        preprocessed = preprocess_for_similarity(body)
        if not preprocessed:
            return []

        # Embed the source file to find semantic neighbours.
        query_emb = embedder.embed(preprocessed)
        if not query_emb:
            return []

        # Collect all files already explicitly linked TO or FROM file_path.
        # "To" = wikilinks inside file_path's body.
        # "From" = files that contain a wikilink to file_path's basename slug.
        linked_slugs: set[str] = set()
        # Slug of the source file (filename without extension).
        source_slug = os.path.splitext(os.path.basename(req.file_path))[0]

        # Extract outgoing links from the source file (raw body, not preprocessed).
        outgoing = set(re.findall(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", body))
        for link in outgoing:
            linked_slugs.add(link.split("/")[-1])

        # Also exclude the source file itself.
        linked_slugs.add(source_slug)

        # Scan all indexed files for incoming links to source_slug.
        db = store.get_db()
        try:
            all_fps = db.execute(
                "SELECT DISTINCT file_path FROM chunks"
            ).fetchall()
        finally:
            db.close()

        incoming_file_paths: set[str] = set()
        link_re = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")
        for (fp,) in all_fps:
            incoming_file_paths.add(fp)
            fp_body = _read_memory_body(fp)
            if fp_body and source_slug in fp_body:
                for m in link_re.finditer(fp_body):
                    if m.group(1).split("/")[-1] == source_slug:
                        linked_slugs.add(os.path.splitext(os.path.basename(fp))[0])
                        break

        # Fetch candidate slate from the vector index.
        candidates = _embedding_candidates(query_emb, top_k=top_k, over_fetch=6)

        # Exclude already-linked files.
        filtered_candidates = [
            c for c in candidates
            if os.path.splitext(os.path.basename(c.get("file_path", "")))[0] not in linked_slugs
            and c.get("file_path", "") != req.file_path
        ]

        ranked = _rerank_with_preprocessing(
            query_preprocessed=preprocessed,
            candidates=filtered_candidates,
            min_similarity=min_similarity,
            top_k=top_k,
        )
        # Rename "similarity" → also expose as "score" per issue spec shape.
        for r in ranked:
            r["score"] = r["similarity"]
        return ranked
    except Exception as e:
        raise _safe_500(e, "Cluster neighbors failed")


@app.post("/topic-coverage")
def topic_coverage_api(req: TopicCoverageRequest) -> dict[str, Any]:
    """Check whether any wiki page already covers a topic phrase.

    Returns ``{covered: bool, best_match: str | None, similarity: float}``
    where ``best_match`` is a relative file_path.  The LLM calls this
    before ingesting new content to avoid creating a page for a topic that
    is already well-covered.

    Uses the same preprocessing pipeline as the other embedding tools so
    that the query is compared against de-noised file bodies.
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.78

        # Treat the query phrase like a short document through the pipeline.
        # slug-style phrases ("machine-learning-deployment") become natural
        # language tokens ("machine learning deployment") for better recall.
        preprocessed_query = preprocess_for_similarity(req.query)
        preprocessed_query = preprocessed_query.replace("-", " ").replace("_", " ").strip()
        if not preprocessed_query:
            return {"covered": False, "best_match": None, "similarity": 0.0}

        query_emb = embedder.embed(preprocessed_query)
        if not query_emb:
            return {"covered": False, "best_match": None, "similarity": 0.0}

        candidates = _embedding_candidates(query_emb, top_k=5, over_fetch=4)
        if not candidates:
            return {"covered": False, "best_match": None, "similarity": 0.0}

        ranked = _rerank_with_preprocessing(
            query_preprocessed=preprocessed_query,
            candidates=candidates,
            min_similarity=min_similarity,
            top_k=1,
        )
        if ranked:
            best = ranked[0]
            return {
                "covered": True,
                "best_match": best["file_path"],
                "similarity": best["similarity"],
            }
        return {"covered": False, "best_match": None, "similarity": 0.0}
    except Exception as e:
        raise _safe_500(e, "Topic coverage failed")


@app.post("/save")
def save_api(req: SaveRequest, request: Request = None, sync: bool = False) -> dict[str, Any]:
    """Persists a new memory instance chunk locally and initiates git backup sequences.

    Query params:
        sync: If True, runs the write-time contradiction check (tier 2a, ADR-004)
              inline and returns its result. If False (default), the check is
              enqueued for background processing and the response returns as
              soon as the file is written and git-committed.
    """
    if request:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip, "write", _RATE_LIMIT_WRITE):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if len(req.content) > _MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Content too large")
    slug = req.slug
    if slug:
        # Prevent any potential JSON escape or traversal exploits if user defines slug
        slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')
        
    if not slug:
        slug = re.sub(r'[^a-z0-9]+', '-', req.content.split('\n')[0].lower()[:30]).strip('-')
        if not slug:
            slug = str(int(time.time()))
            
    type_map = {
        "PersonMemory": "people",
        "Decision": "decisions",
        "ProjectSnapshot": "projects",
        "Insight": "insights",
        "ResearchRef": "research",
        "ActionItem": "inbox"
    }
    category = type_map.get(req.type, "inbox")
    
    # Security scan: reject prompt injection and exfiltration attempts
    is_safe, reason = store.scan_memory_content(req.content)
    if not is_safe:
        raise HTTPException(status_code=400, detail=f"Security scan failed: {reason}")

    file_path = os.path.join(config.palinode_dir, category, f"{slug}.md")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    content_hash = hashlib.sha256(req.content.encode()).hexdigest()

    # Normalize entity refs: bare strings get a category prefix.
    # e.g. "palinode" → "project/palinode", "alice" → "person/alice"
    raw_entities = list(req.entities or [])
    # ADR-010 / #159: ``project`` is sugar for the ``project/<slug>`` entity.
    if req.project:
        project_ref = req.project if "/" in req.project else f"project/{req.project}"
        if project_ref not in raw_entities:
            raw_entities.append(project_ref)
    normalized_entities = _normalize_entities(raw_entities, category)

    # Capture a single UTC timestamp for both created_at and last_updated so
    # that they are identical on first write (#177: file must not be born stale).
    _now_iso = _utc_now().isoformat()
    frontmatter_dict = {
        "id": f"{category}-{slug}",
        "category": category,
        "type": req.type,
        "entities": normalized_entities,
        "content_hash": content_hash,
        # #191: write proper timezone-aware UTC ISO-8601 (`+00:00` suffix).
        # Previously used ``time.strftime("...%Z")`` which emitted local time
        # with a ``Z`` (UTC) marker — a mismatch that made `chunks.created_at`
        # unreliable as a recency signal.
        "created_at": _now_iso,
        # #177: populate last_updated on initial write so the file isn't born
        # stale.  The freshness checker treats a missing last_updated as stale;
        # setting it equal to created_at on first save avoids that false positive.
        # On re-saves the indexer re-reads frontmatter and this value is refreshed.
        "last_updated": _now_iso,
    }
    if req.metadata:
        frontmatter_dict.update(req.metadata)
    if req.core is not None:
        frontmatter_dict["core"] = req.core
    if req.confidence is not None:
        frontmatter_dict["confidence"] = req.confidence
    # #106: IETF KU frontmatter alignment — auto-populate KU fields when
    # ku_compat is enabled, or when the caller explicitly provides them.
    if config.ku_compat.enabled:
        if "ku_version" not in frontmatter_dict:
            frontmatter_dict["ku_version"] = config.ku_compat.ku_version
        if "lifecycle" not in frontmatter_dict:
            raw_status = frontmatter_dict.get("status") or (req.metadata or {}).get("status", "active")
            from palinode.core.parser import VALID_LIFECYCLES
            frontmatter_dict["lifecycle"] = raw_status if raw_status in VALID_LIFECYCLES else "active"
    # #115: external SDLC object references (free-form dict[str, str]).
    if req.external_refs is not None:
        from palinode.core.parser import parse_external_refs as _parse_ext_refs
        validated = _parse_ext_refs({"external_refs": req.external_refs})
        if validated is not None:
            frontmatter_dict["external_refs"] = validated
    # ADR-010 / #166: explicit ``title`` overrides metadata-supplied title.
    if req.title:
        frontmatter_dict["title"] = req.title

    # ADR-010 / #167: explicit body field > X-Palinode-Source header > env > "api".
    frontmatter_dict["source"] = _resolve_source(req.source, request)

    # Auto-generate description if not already provided via metadata
    if not frontmatter_dict.get("description"):
        try:
            desc = _generate_description(req.content)
            if desc:
                frontmatter_dict["description"] = desc
        except Exception as e:
            logger.warning(f"Description generation failed (non-fatal): {e}")

    # Layer 2 wiki contract (#210): auto-append See also footer for any entities
    # not already referenced as [[wikilinks]] in the body.
    body_content = _apply_wiki_footer(req.content, normalized_entities)

    doc = f"---\n{yaml.safe_dump(frontmatter_dict, default_flow_style=False, allow_unicode=True)}---\n\n{body_content}\n"

    with open(file_path, "w") as f:
        f.write(doc)

    # Automatically generate summary block metadata explicitly
    if config.auto_summary.enabled:
        try:
            is_core = bool(frontmatter_dict.get("core", False))
            has_summary = bool(frontmatter_dict.get("summary"))
            if is_core and not has_summary and len(req.content) >= config.auto_summary.min_content_chars:
                summary = _generate_summary(doc)
                if summary:
                    _inject_summary(file_path, summary)
                    logger.info(f"Auto-summary injected for {file_path}")
        except Exception as e:
            logger.warning(f"Auto-summary generation failed (non-fatal): {e}")

    # Git persistence: stage + commit (+ optional push).
    if config.git.auto_commit:
        try:
            rel_path = os.path.relpath(file_path, config.memory_dir)
            commit_msg = f"{config.git.commit_prefix} auto-save: {category}/{slug}.md"
            git_persistence.commit_existing(commit_msg, [rel_path])
            if config.git.auto_push:
                git_persistence.push()
        except (git_persistence.GitPersistenceError, OSError) as e:
            logger.error(f"Git auto-commit failed: {e}")

    logger.info(f"Saved memory to {file_path}")

    # #251: embed inline so that POST /save only returns once vector + FTS
    # entries actually exist. Previously the watcher embedded out-of-band,
    # leaving a race window where /search immediately after /save returned
    # zero results. The watcher remains the indexer for filesystem-direct
    # writes; this path covers API-driven saves.
    indexed = False
    index_error: str | None = None
    try:
        from palinode.indexer.index_file import index_file
        outcome = index_file(file_path)
        indexed = bool(outcome.get("embedded"))
        index_error = outcome.get("error")
    except Exception as e:
        # File is on disk; the watcher will pick it up later.
        logger.warning(f"Inline index failed for {file_path} (non-fatal): {e}")
        index_error = str(e)

    if not indexed:
        logger.warning(
            f"Saved {file_path} but inline embed did not complete "
            f"(reason: {index_error or 'unknown'}); watcher will retry."
        )

    result: dict[str, Any] = {
        "file_path": file_path,
        "id": frontmatter_dict["id"],
        "indexed": indexed,
        "embedded": indexed,
    }
    if index_error and not indexed:
        result["index_error"] = index_error

    # Tier 2a (ADR-004): schedule write-time contradiction check.
    # Always safe to call — returns None immediately if disabled in config.
    # Errors inside the scheduler are logged and swallowed; never propagate.
    if config.consolidation.write_time.enabled:
        try:
            from palinode.consolidation import write_time
            item = {
                "content": req.content,
                "category": category,
                "type": req.type,
                "entities": req.entities or [],
                "id": frontmatter_dict["id"],
            }
            check_result = write_time.schedule_contradiction_check(
                file_path, item, sync=sync
            )
            if sync and check_result is not None:
                result["write_time_check"] = check_result
        except Exception as e:
            # Load-bearing: save must never fail because of tier 2a
            logger.error(f"write-time schedule failed (non-fatal): {e}")

    return result


@app.post("/generate-summaries")
def generate_summaries_api() -> dict[str, Any]:
    """Generate summaries for all core files that don't have one.
    
    Scans all markdown files with core: true in frontmatter.
    If summary: field is missing or empty, generates one via Ollama.
    """
    import glob
    from palinode.core import parser
    
    count = 0
    # Use palinode_dir since that's generally where memories are kept
    for filepath in glob.glob(os.path.join(config.palinode_dir, "**/*.md"), recursive=True):
        try:
            with open(filepath) as f:
                content = f.read()
            metadata, _ = parser.parse_markdown(content)
            if not metadata.get("core"):
                continue
            if metadata.get("summary"):
                continue  # Already has summary
            
            summary = _generate_summary(content)
            if summary:
                _inject_summary(filepath, summary)
                count += 1
                logger.info(f"Generated summary for {filepath}")
        except Exception as e:
            logger.warning(f"Summary generation failed for {filepath}: {e}")
    
    return {"status": "success", "summaries_generated": count}


@app.get("/status")
def status_api() -> dict[str, Any]:
    """Generates overarching health-checks to ensure pipeline availability."""
    stats: dict[str, Any] = dict(store.get_stats())
    
    git_stats = git_tools.commit_count(7)
    stats["git_commits_7d"] = git_stats["total_commits"]
    stats["git_summary_7d"] = git_stats["summary"]
    
    try:
        import subprocess
        unpushed = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"], cwd=config.palinode_dir, capture_output=True, text=True)
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

    try:
        httpx.get(config.embeddings.primary.url, timeout=2.0)
        ollama_reachable = True
    except (httpx.HTTPError, OSError):
        # L1: narrowed from `Exception`. The probe only cares whether Ollama
        # responded; httpx.HTTPError covers connect errors, timeouts, and HTTP
        # status; OSError catches builtin ConnectionError. Anything broader
        # (e.g. AttributeError on a misconfigured url) should propagate so
        # we surface real bugs.
        ollama_reachable = False

    stats["ollama_reachable"] = ollama_reachable

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
            logger.warning(f"write-time status lookup failed: {e}")

    # Reindex progress (#200)
    stats["reindex"] = {
        "running": _reindex_state["running"],
        "started_at": _reindex_state["started_at"],
        "files_processed": _reindex_state["files_processed"],
        "total_files": _reindex_state["total_files"],
    }

    return stats


@app.get("/health")
def health_api() -> dict[str, Any]:
    """Lightweight liveness check — no side effects, <100ms.

    Returns live counts queried at request time via store.get_stats() — the
    same code path used by /status.  If chunks or entities are zero, the
    database is genuinely empty (not stale or cached).  Reports
    status="degraded" with a db_error key if the database cannot be reached.
    """
    result: dict[str, Any] = {"status": "ok"}

    # DB accessible + basic stats — delegate to store.get_stats() for chunk
    # count so the code path is identical to /status and cannot diverge (#187).
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

    # Ollama reachable
    try:
        httpx.get(config.embeddings.primary.url, timeout=2.0)
        result["ollama"] = True
    except (httpx.HTTPError, OSError):
        # L1: narrowed from `Exception` (see status_api). Probe only cares
        # whether the embedder responded; broader bugs should propagate.
        result["ollama"] = False

    return result


@app.get("/health/watcher")
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
            import subprocess
            journal = subprocess.run(
                ["journalctl", "--user", "-u", "palinode-watcher",
                 "--since", "1 hour ago", "--no-pager", "-p", "err"],
                capture_output=True, text=True, timeout=5
            )
            errors = [l for l in journal.stdout.strip().split("\n") if l.strip() and "-- No entries --" not in l]
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


@app.get("/doctor")
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
    from palinode.diagnostics.runner import run_all
    from palinode.diagnostics.types import DoctorContext
    from palinode.diagnostics.formatters import format_json
    import json as _json

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


@app.post("/ingest")
def ingest_api() -> dict[str, str]:
    """Invoke document drop-box scanning routine."""
    from palinode.ingest.pipeline import process_inbox
    try:
        process_inbox()
        return {"status": "success"}
    except Exception as e:
        raise _safe_500(e, "Ingestion failed")


@app.post("/ingest-url")
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


@app.post("/rebuild-fts")
def rebuild_fts_api() -> dict[str, Any]:
    """Rebuild the FTS5 full-text search index from existing chunks.
    
    Run this once after upgrading to hybrid search, or if the FTS5
    index gets out of sync with the chunks table.
    """
    logger.info("Rebuilding FTS5 index...")
    count = store.rebuild_fts()
    logger.info(f"FTS5 rebuild complete: {count} chunks indexed")
    return {"status": "success", "chunks_indexed": count}


@app.post("/reindex")
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
        "fts_chunks": fts_count,
    }


@app.get("/entities/{entity_ref:path}")
def entity_api(entity_ref: str) -> dict[str, Any]:
    """Get all files referencing an entity."""
    files = entity_graph.get_entity_files(entity_ref)
    graph = entity_graph.get_entity_graph(entity_ref)
    return {"entity": entity_ref, "files": files, "connected_entities": graph}


@app.get("/entities")
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


@app.post("/lint")
def lint_api() -> dict[str, Any]:
    """Scan memory and report orphans, stale files, and contradictions."""
    from palinode.core.lint import run_lint_pass
    return run_lint_pass()


@app.get("/history/{file_path:path}")
def history_api(
    file_path: str,
    limit: int = 20,
    detail: str = "summary",
) -> dict[str, Any]:
    """Get the change history for a memory file.

    Uses --follow to track renames and includes diff stats per commit.

    ``detail="full"`` additionally includes the unified diff body per commit
    (commit-level evolution view, formerly the /timeline endpoint).
    """
    if detail not in ("summary", "full"):
        raise HTTPException(status_code=422, detail="detail must be 'summary' or 'full'")
    commits = git_tools.history(file_path, limit, detail=detail)
    if not commits:
        # Distinguish "file not found" from "no history"
        import os as _os
        full_path = _os.path.join(config.memory_dir, file_path)
        if not _os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")

    # Issue #256: history access is an explicit retrieval.
    _retrieval_logger.record_file_read(
        file_path,
        source="palinode_history",
        mode="explicit",
    )
    return {"file": file_path, "history": commits}


@app.get("/timeline/{file_path:path}")
def timeline_api(
    request: Request,
    file_path: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Deprecated: use GET /history/{file_path}?detail=full instead.

    Kept for one release cycle for backward compatibility.  Returns the same
    response as /history?detail=full with a ``Deprecation`` response header.
    """
    from fastapi.responses import JSONResponse as _JSONResponse
    import logging as _logging
    _logging.getLogger("palinode.api").warning(
        "GET /timeline is deprecated — use GET /history/%s?detail=full", file_path
    )
    commits = git_tools.history(file_path, limit, detail="full")
    if not commits:
        import os as _os
        full_path = _os.path.join(config.memory_dir, file_path)
        if not _os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")
    body = {"file": file_path, "history": commits}
    return _JSONResponse(
        content=body,
        headers={"Deprecation": "true", "Link": f'</history/{file_path}?detail=full>; rel="successor-version"'},
    )


class ConsolidateRequest(BaseModel):
    dry_run: bool = False
    nightly: bool = False

@app.post("/consolidate")
def consolidate_api(req: ConsolidateRequest = None) -> dict[str, Any]:
    """Run a manual consolidation pass.

    Normally runs as a weekly cron, but can be triggered manually
    for testing or after a busy week.
    """
    from palinode.consolidation.runner import run_consolidation, run_nightly
    
    req = req or ConsolidateRequest()
    try:
        if req.nightly:
            result = run_nightly()
        else:
            result = run_consolidation()
        return result
    except Exception as e:
        raise _safe_500(e, "Consolidation failed")


@app.post("/split-layers")
def split_layers_api() -> dict[str, Any]:
    """Split core files into Identity/Status/History layers."""
    from palinode.consolidation.layer_split import split_all_core_files
    stats = split_all_core_files()
    return stats


@app.post("/bootstrap-fact-ids")
def bootstrap_fact_ids_api() -> dict[str, Any]:
    """Add fact IDs to all memory files."""
    from palinode.consolidation.fact_ids import bootstrap_all_fact_ids
    stats = bootstrap_all_fact_ids()
    return stats


@app.get("/diff")
def diff_api(days: int = 7, paths: str | None = None) -> dict[str, Any]:
    """Show memory changes in the last N days, optionally filtered by paths."""
    path_list = paths.split(",") if paths else None
    return {"diff": git_tools.diff(days, path_list)}


@app.get("/blame/{file_path:path}")
def blame_api(file_path: str, search: str | None = None) -> dict[str, Any]:
    """Show when each line of a memory file was last changed."""
    # Issue #256: blame access is an explicit retrieval.
    _retrieval_logger.record_file_read(
        file_path,
        source="palinode_blame",
        mode="explicit",
    )
    return {"blame": git_tools.blame(file_path, search)}


@app.post("/rollback")
def rollback_api(file_path: str, commit: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Revert a memory file to a previous version.
    
    Defaults to dry_run=True for safety. Set dry_run=False to actually revert.
    """
    return {"result": git_tools.rollback(file_path, commit, dry_run)}


@app.post("/push")
def push_api() -> dict[str, Any]:
    """Push memory changes to the remote repository."""
    return {"result": git_tools.push()}


class SessionEndRequest(BaseModel):
    summary: str
    decisions: list[str] | None = None
    blockers: list[str] | None = None
    project: str | None = None
    source: str | None = None
    # Structured metadata (#145). All optional; existing callers keep working.
    harness: str | None = None  # e.g. "claude-code", "claude-desktop", "cowork", "openclaw", "cursor", "zed", "vscode", "cli", "api", "hook", "other"
    cwd: str | None = None  # fully-qualified path the session ran in
    model: str | None = None  # e.g. "claude-opus-4-7"
    trigger: str | None = None  # e.g. "manual", "wrap-slash", "ps-slash", "session-end-hook", "clear-fallback-hook", "sigterm", "exit", "other"
    session_id: str | None = None  # opaque from harness if available
    duration_seconds: int | None = None


def _check_session_end_dedup(
    content: str,
    window_minutes: int = SESSION_END_DEDUP_WINDOW_MINUTES,
    threshold: float = SESSION_END_DEDUP_THRESHOLD,
) -> tuple[str | None, float]:
    """Look for a recent indexed save whose embedding is near-identical to ``content`` (#126).

    Returns ``(matched_slug, similarity)`` when a recent save scores at or
    above ``threshold``; ``(None, 0.0)`` otherwise.  Failure modes — empty
    embedding from the embedder, no recent saves, or DB error inside the
    helper — return ``(None, 0.0)`` so the caller writes both files.
    """
    try:
        new_emb = embedder.embed(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"session_end dedup: embed failed ({e}); writing without dedup")
        return None, 0.0
    if not new_emb:
        logger.warning("session_end dedup: embedder returned empty vector; writing without dedup")
        return None, 0.0

    try:
        recent = store.recent_save_embeddings(window_minutes)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"session_end dedup: recent_save_embeddings failed ({e}); writing without dedup")
        return None, 0.0

    best_slug: str | None = None
    best_sim = 0.0
    for slug, emb in recent:
        sim = _cosine_similarity(new_emb, emb)
        if sim > best_sim:
            best_sim = sim
            best_slug = slug

    if best_slug is not None and best_sim >= threshold:
        return best_slug, best_sim
    return None, best_sim


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


@app.post("/session-end")
def session_end_api(req: SessionEndRequest, request: Request = None) -> dict[str, Any]:
    """Capture session outcomes to daily notes and project status files."""
    today = _utc_now().strftime("%Y-%m-%d")
    now_iso = _utc_now().isoformat().replace("+00:00", "Z")
    # ADR-010 / #167: same precedence as save_api — explicit > header > env > default.
    source = _resolve_source(req.source, request)

    # Auto-derive project from cwd if caller didn't pass one (#145).
    project = req.project or _project_from_cwd(req.cwd)

    # Build session entry
    parts = [f"## Session End — {now_iso}\n"]
    parts.append(f"**Source:** {source}\n")
    parts.append(f"**Summary:** {req.summary}\n")
    if req.decisions:
        parts.append("**Decisions:**")
        for d in req.decisions:
            parts.append(f"- {d}")
        parts.append("")
    if req.blockers:
        parts.append("**Blockers/Next:**")
        for b in req.blockers:
            parts.append(f"- {b}")
        parts.append("")

    # Structured metadata footer (#145). Only emit lines that are populated so
    # the daily note stays uncluttered for callers that don't supply metadata.
    meta_lines: list[str] = []
    if req.harness:
        meta_lines.append(f"**Harness:** {req.harness}")
    if req.cwd:
        meta_lines.append(f"**CWD:** {req.cwd}")
    if req.model:
        meta_lines.append(f"**Model:** {req.model}")
    if req.trigger:
        meta_lines.append(f"**Trigger:** {req.trigger}")
    if req.session_id:
        meta_lines.append(f"**Session ID:** {req.session_id}")
    if req.duration_seconds is not None:
        meta_lines.append(f"**Duration:** {req.duration_seconds}s")
    if meta_lines:
        parts.extend(meta_lines)
        parts.append("")

    session_entry = "\n".join(parts)

    # Write to daily notes
    daily_dir = os.path.join(_memory_base_dir(), "daily")
    os.makedirs(daily_dir, exist_ok=True)
    daily_path = os.path.join(daily_dir, f"{today}.md")
    with open(daily_path, "a") as f:
        f.write(f"\n{session_entry}\n")

    # Append status to project file if specified (or auto-derived from cwd).
    status_file = None
    if project:
        status_path = os.path.join(_memory_base_dir(), "projects", f"{project}-status.md")
        if os.path.exists(status_path):
            one_liner = req.summary.replace("\n", " ").strip()[:200]
            with open(status_path, "a") as f:
                f.write(f"\n- [{today}] {one_liner}\n")
            status_file = f"projects/{project}-status.md"

    # Semantic dedup against recent saves (#126). The daily note + project
    # status file are append-only logs we always write — only the indexed
    # individual file is suppressed when a near-duplicate already exists,
    # because that file's value is the standalone embedding/searchable record
    # which we'd otherwise have twice for the same content.
    deduplicated_against, dedup_similarity = _check_session_end_dedup(session_entry)

    # Also save as an individual indexed memory file (M0: dual-write).
    # This gives each session-end its own frontmatter, entities, description,
    # and embedding — searchable and retractable independently.
    individual_file = None
    if deduplicated_against is not None:
        logger.info(
            f"session_end dedup: matched {deduplicated_against} (sim={dedup_similarity:.2f}) "
            f"— skipping individual file"
        )
    else:
        try:
            short_hash = hashlib.sha256(req.summary.encode()).hexdigest()[:8]
            # Pass structured metadata through to the indexed file's frontmatter so
            # it's queryable later (#145). Only include fields the caller set.
            extra_meta: dict[str, Any] = {}
            if req.harness:
                extra_meta["harness"] = req.harness
            if req.cwd:
                extra_meta["cwd"] = req.cwd
            if req.model:
                extra_meta["model"] = req.model
            if req.trigger:
                extra_meta["trigger"] = req.trigger
            if req.session_id:
                extra_meta["session_id"] = req.session_id
            if req.duration_seconds is not None:
                extra_meta["duration_seconds"] = req.duration_seconds
            save_req = SaveRequest(
                content=session_entry,
                type="ProjectSnapshot" if project else "Insight",
                slug=f"session-end-{today}-{project}-{short_hash}" if project else f"session-end-{today}-{short_hash}",
                entities=[f"project/{project}"] if project else [],
                source=source,
                metadata=extra_meta or None,
            )
            save_result = save_api(save_req)
            individual_file = save_result.get("file_path")
        except Exception as e:
            logger.error(f"Individual session-end file save failed (non-fatal): {e}")

    # Git persistence: stage daily + status files, commit.
    if config.git.auto_commit:
        try:
            base = _memory_base_dir()
            rel_paths = [os.path.relpath(daily_path, base)]
            if status_file:
                # status_file may already be relative; join + relpath normalizes.
                rel_paths.append(os.path.relpath(os.path.join(base, status_file), base))
            commit_msg = f"{config.git.commit_prefix} session-end: {today}"
            git_persistence.commit_existing(commit_msg, rel_paths)
            if config.git.auto_push:
                git_persistence.push()
        except (git_persistence.GitPersistenceError, OSError) as e:
            logger.error(f"Git commit failed for session-end: {e}")

    response: dict[str, Any] = {
        "daily_file": f"daily/{today}.md",
        "status_file": status_file,
        "individual_file": individual_file,
        "entry": session_entry,
    }
    if deduplicated_against is not None:
        response["deduplicated_against"] = deduplicated_against
    return response


@app.get("/git-stats")
def git_stats_api(days: int = 7) -> dict[str, Any]:
    """Get commit statistics for the memory repo."""
    return git_tools.commit_count(days)


PROMPT_TASKS = {"compaction", "extraction", "update", "classification"}


def _prompts_dir() -> str:
    return os.path.join(_memory_base_dir(), "prompts")


def _read_prompt_file(file_path: str) -> dict[str, Any]:
    """Read a prompt file and return its metadata + content."""
    from palinode.core import parser
    with open(file_path, "r") as f:
        raw = f.read()
    metadata, sections = parser.parse_markdown(raw)
    # Reconstruct body from sections
    body = "\n\n".join(s["content"] for s in sections if s.get("content"))
    name = os.path.basename(file_path).replace(".md", "")
    return {
        "name": name,
        "file": os.path.relpath(file_path, _memory_base_dir()),
        "model": metadata.get("model", ""),
        "task": metadata.get("task", ""),
        "version": metadata.get("version", ""),
        "active": bool(metadata.get("active", False)),
        "content": body.strip(),
        "size_bytes": os.path.getsize(file_path),
    }


@app.get("/prompts")
def list_prompts_api(task: str | None = None) -> list[dict[str, Any]]:
    """List all prompt files, optionally filtered by task."""
    prompts_dir = _prompts_dir()
    if not os.path.exists(prompts_dir):
        return []

    results = []
    for filepath in glob.glob(os.path.join(prompts_dir, "*.md")):
        try:
            if os.path.commonpath([_memory_base_dir(), os.path.realpath(filepath)]) != _memory_base_dir():
                continue
            info = _read_prompt_file(filepath)
            if task and info["task"] != task:
                continue
            results.append(info)
        except Exception:
            pass

    results.sort(key=lambda x: (x["task"], x["name"]))
    return results


@app.get("/prompts/{name}")
def get_prompt_api(name: str) -> dict[str, Any]:
    """Read a specific prompt by name."""
    prompts_dir = _prompts_dir()
    candidates = [
        os.path.join(prompts_dir, name),
        os.path.join(prompts_dir, f"{name}.md"),
    ]
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        try:
            within = os.path.commonpath([_memory_base_dir(), resolved]) == _memory_base_dir()
        except ValueError:
            continue
        if within and os.path.exists(resolved):
            return _read_prompt_file(resolved)

    raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")


@app.post("/prompts/{name}/activate")
def activate_prompt_api(name: str) -> dict[str, Any]:
    """Set active=true on this prompt and active=false on all others with the same task."""
    import re as _re
    prompts_dir = _prompts_dir()
    if not os.path.exists(prompts_dir):
        raise HTTPException(status_code=404, detail="No prompts directory found")

    # Resolve target file
    candidates = [
        os.path.join(prompts_dir, name),
        os.path.join(prompts_dir, f"{name}.md"),
    ]
    target_path = None
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        try:
            within = os.path.commonpath([_memory_base_dir(), resolved]) == _memory_base_dir()
        except ValueError:
            continue
        if within and os.path.exists(resolved):
            target_path = resolved
            break

    if not target_path:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")

    target_info = _read_prompt_file(target_path)
    task = target_info["task"]

    def _set_active(file_path: str, active: bool) -> None:
        with open(file_path, "r") as f:
            text = f.read()
        # Replace active: field in frontmatter
        new_text = _re.sub(
            r'^(active:\s*).*$',
            f'active: {"true" if active else "false"}',
            text,
            flags=_re.MULTILINE,
        )
        if new_text == text:
            # Field missing — inject before closing ---
            pattern = _re.compile(r'^(---\n.*?\n)(---\n)', _re.DOTALL)
            m = pattern.match(text)
            if m:
                new_text = m.group(1) + f'active: {"true" if active else "false"}\n' + m.group(2) + text[m.end():]
        with open(file_path, "w") as f:
            f.write(new_text)

    # Deactivate all prompts of the same task
    for filepath in glob.glob(os.path.join(prompts_dir, "*.md")):
        try:
            resolved = os.path.realpath(filepath)
            within = os.path.commonpath([_memory_base_dir(), resolved]) == _memory_base_dir()
            if not within:
                continue
            info = _read_prompt_file(resolved)
            if info["task"] == task and resolved != target_path:
                _set_active(resolved, False)
        except Exception:
            pass

    # Activate target
    _set_active(target_path, True)

    if config.git.auto_commit:
        try:
            git_persistence.commit_existing(
                f"palinode: activate prompt {name} for task={task}",
                [os.path.join("prompts", "*.md")],
            )
        except (git_persistence.GitPersistenceError, OSError) as e:
            logger.warning(f"Git commit for prompt activation failed: {e}")

    return {"activated": name, "task": task}


class MigrateOpenClawRequest(BaseModel):
    path: str
    dry_run: bool = False


@app.post("/migrate/openclaw")
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


@app.get("/depends/_unblocked")
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


@app.get("/depends/{slug:path}")
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


@app.post("/migrate/mem0")
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


def main() -> None:
    """Invokes Uvicorn CLI runner."""
    # Refuse to start if PALINODE_API_BIND_INTENT=public is set but no
    # bearer token is configured. This is the loud-fail counterpart to the
    # bearer-auth middleware's silent-no-op behaviour for local dev.
    _validate_auth_config(_api_token)
    import uvicorn
    uvicorn.run("palinode.api.server:app", host=config.services.api.host, port=config.services.api.port)


if __name__ == "__main__":
    main()
