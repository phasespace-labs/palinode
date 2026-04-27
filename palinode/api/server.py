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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from palinode.core import store, embedder, git_tools
from palinode.core.config import config
from palinode.core.defaults import (
    SAVE_SOURCE_API_DEFAULT,
    SAVE_SOURCE_HEADER,
    SESSION_END_DEDUP_THRESHOLD,
    SESSION_END_DEDUP_WINDOW_MINUTES,
)


logger = logging.getLogger("palinode.api")
logger.setLevel(getattr(logging, config.services.api.log_level.upper(), logging.INFO))


class JsonlFormatter(logging.Formatter):
    """Logging Formatter dictating a JSONL chronological schema format."""
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": _utc_now().isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()
        })


# Attach handlers to the "palinode" parent logger so all palinode.* modules
# (palinode.api, palinode.write_time, palinode.consolidation, etc.) share them.
# This ensures unified observability across background workers and request
# handlers without each module configuring its own handlers.
_parent_logger = logging.getLogger("palinode")
_parent_logger.setLevel(getattr(logging, config.services.api.log_level.upper(), logging.INFO))

sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
_parent_logger.addHandler(sh)

os.makedirs(os.path.join(config.palinode_dir, "logs"), exist_ok=True)
fh = logging.FileHandler(os.path.join(config.palinode_dir, config.logging.operations_log))
fh.setFormatter(JsonlFormatter())
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

# CORS: restrict to configured origins (default: localhost only)
_cors_origins = os.environ.get("PALINODE_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Request body size limit (default 5MB)
_MAX_REQUEST_BYTES = int(os.environ.get("PALINODE_MAX_REQUEST_BYTES", 5 * 1024 * 1024))

@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Reject oversized request bodies to prevent memory exhaustion."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_REQUEST_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    return await call_next(request)

# Rate limiting (in-memory, per-IP, resets each window)
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_SEARCH = int(os.environ.get("PALINODE_RATE_LIMIT_SEARCH", 100))
_RATE_LIMIT_WRITE = int(os.environ.get("PALINODE_RATE_LIMIT_WRITE", 30))
_rate_counters: dict[str, dict[str, Any]] = {}

def _check_rate_limit(client_ip: str, category: str, limit: int) -> bool:
    """Return True if request is within rate limit, False if exceeded."""
    now = time.time()
    key = f"{client_ip}:{category}"
    entry = _rate_counters.get(key)
    if not entry or now - entry["window_start"] > _RATE_LIMIT_WINDOW:
        _rate_counters[key] = {"window_start": now, "count": 1}
        return True
    entry["count"] += 1
    return entry["count"] <= limit

# Startup warning for unsafe binding
_api_host = os.environ.get("PALINODE_API_HOST", config.services.api.host)
if _api_host == "0.0.0.0":
    logger.warning(
        "API binding to 0.0.0.0 — accessible from any network. "
        "No authentication is configured. Set PALINODE_API_HOST=127.0.0.1 for local-only access."
    )

# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_500(e: Exception, context: str = "Internal error") -> HTTPException:
    """Log full exception, return sanitized 500 to client."""
    logger.exception(f"{context}: {e}")
    return HTTPException(status_code=500, detail=context)


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _memory_base_dir() -> str:
    """Return the canonical memory root."""
    return os.path.realpath(getattr(config, "memory_dir", config.palinode_dir))


def _resolve_memory_path(file_path: str) -> tuple[str, str]:
    """Resolve a relative memory path without allowing traversal outside memory_dir."""
    if "\x00" in file_path:
        raise HTTPException(status_code=400, detail="Null bytes are not allowed in paths")
    if os.path.isabs(file_path):
        raise HTTPException(status_code=403, detail="Absolute paths are not allowed")

    base_dir = _memory_base_dir()
    resolved = os.path.realpath(os.path.join(base_dir, file_path))
    try:
        within_root = os.path.commonpath([base_dir, resolved]) == base_dir
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Path traversal rejected") from exc
    if not within_root:
        raise HTTPException(status_code=403, detail="Path traversal rejected")
    return base_dir, resolved

# ── Entity normalization ─────────────────────────────────────────────────────

# Maps memory category dirs to singular entity-ref prefixes.
_CATEGORY_TO_ENTITY_PREFIX: dict[str, str] = {
    "people": "person",
    "decisions": "decision",
    "projects": "project",
    "insights": "insight",
    "research": "research",
    "inbox": "action",
}


_WIKI_FOOTER_MARKER = "<!-- palinode-auto-footer -->"


def _apply_wiki_footer(content: str, entities: list[str]) -> str:
    """Append or update a ``## See also`` auto-footer for un-linked entities.

    When ``entities`` are provided but some of them are not already referenced
    as ``[[wikilinks]]`` in *content*, this function appends a detectable
    auto-generated footer so that Obsidian graph view picks up the links.

    Canonicalization: entity refs use the slash form ``category/slug``; the
    wikilink target is only the *slug* part (everything after the last ``/``).
    This matches the existing ``_normalize_entities`` convention — entity refs
    are stored as ``project/palinode``, the corresponding wikilink is
    ``[[palinode]]``.

    Rules:
    - If *content* is empty / None, or *entities* is empty, return unchanged.
    - Extract existing ``[[target]]`` wikilinks from body; skip entities whose
      slug already appears as an inline link.
    - If a ``## See also`` block with ``_WIKI_FOOTER_MARKER`` exists, **replace**
      it (idempotent re-save).
    - If a ``## See also`` block exists **without** the marker it is user-authored
      — leave it alone and append a new auto-footer block after it.
    - If all entities are already linked inline, remove any stale auto-footer.
    """
    if not content or not entities:
        return content

    # Pattern that matches an existing auto-footer block up to end-of-string or
    # the next level-2 heading.  Compiled once; used twice below.
    auto_footer_re = re.compile(
        r"## See also\s*\n" + re.escape(_WIKI_FOOTER_MARKER) + r".*?(?=\n## |\Z)",
        re.DOTALL,
    )

    # Scan for existing inline wikilinks OUTSIDE the auto-footer block so that
    # links inside the footer itself are not mistaken for user-authored inline
    # links.  This is the key to idempotency: on re-save the footer's own
    # [[slug]] entries do not satisfy the "already linked inline" check.
    body_for_scan = auto_footer_re.sub("", content)
    existing_links: set[str] = set(re.findall(r"\[\[([^\]]+)\]\]", body_for_scan))

    # Derive the wikilink slug for each entity (part after the last '/').
    missing: list[str] = []
    for entity in entities:
        slug = entity.split("/")[-1]
        if slug not in existing_links:
            missing.append(slug)

    # Build the new auto-footer block.  Always ends with a newline so that the
    # substitution path and the append path produce identical output (idempotent).
    if missing:
        footer_lines = ["## See also", _WIKI_FOOTER_MARKER]
        footer_lines.extend(f"- [[{slug}]]" for slug in missing)
        new_footer = "\n".join(footer_lines) + "\n"
    else:
        new_footer = ""

    if auto_footer_re.search(content):
        if new_footer:
            content = auto_footer_re.sub(new_footer, content)
        else:
            # All links are now inline — strip the stale auto-footer.
            content = auto_footer_re.sub("", content).rstrip("\n") + "\n"
    elif new_footer:
        # No existing auto-footer; append after a blank-line separator.
        content = content.rstrip("\n") + "\n\n" + new_footer

    return content


def _normalize_entities(entities: list[str], category: str) -> list[str]:
    """Ensure every entity ref has a category/ prefix.

    Bare strings (no '/') get a prefix inferred from the memory's own
    category.  Falls back to 'project/' when the category is unknown
    (matches MCP context-resolution convention).
    """
    prefix = _CATEGORY_TO_ENTITY_PREFIX.get(category, "project")
    normalized = []
    for e in entities:
        if "/" in e:
            normalized.append(e)
        else:
            logger.info("Entity normalized: %r → %r", e, f"{prefix}/{e}")
            normalized.append(f"{prefix}/{e}")
    return normalized


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


def _generate_description(content: str) -> str:
    """Generate a one-line description for a memory file.

    Tries a cheap Ollama call first; falls back to first-line extraction
    if the LLM is unreachable.  Never raises — returns empty string on
    total failure.
    """
    MAX_CHARS = 150

    # Attempt LLM description
    prompt = (
        "Write one sentence (max 150 chars) describing what this memory is about. "
        "Be specific and factual. Output ONLY the sentence, no preamble.\n\n"
        + content[:1500]
    )
    url = config.auto_summary.ollama_url or config.embeddings.primary.url
    try:
        resp = httpx.post(
            f"{url}/api/generate",
            json={"model": config.auto_summary.model, "prompt": prompt, "stream": False},
            timeout=15.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip().strip('"\'').strip()
        if raw:
            return raw[:MAX_CHARS]
    except Exception as e:
        logger.info(f"Ollama description call failed, using fallback: {e}")

    # Fallback: first meaningful line of content
    return _extract_first_line(content, MAX_CHARS)


def _extract_first_line(content: str, max_chars: int = 150) -> str:
    """Extract the first non-empty, non-header line from markdown content."""
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Strip markdown headers
        line = re.sub(r'^#+\s*', '', line)
        line = line.strip()
        if line:
            return line[:max_chars]
    return ""


def _generate_summary(content: str) -> str:
    """Invokes Ollama to produce a single-sentence logical summary of file memory.

    Args:
        content (str): Complete file content string to evaluate.

    Returns:
        str: Generated summary text. Yields an empty string if generation fails.
    """
    prompt = (
        f"Summarize the following memory file in one sentence (max {config.auto_summary.max_chars} chars). "
        "Be specific and factual. Output ONLY the summary, no preamble.\n\n"
        + content[:2000]
    )
    url = config.auto_summary.ollama_url or config.embeddings.primary.url
    
    try:
        resp = httpx.post(
            f"{url}/api/generate",
            json={"model": config.auto_summary.model, "prompt": prompt, "stream": False},
            timeout=30.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # Trim and cleanly strip quotes appended by inference
        raw = raw.strip('"\'').strip()
        if len(raw) > config.auto_summary.max_chars:
            raw = raw[:config.auto_summary.max_chars - 3] + "..."
        return raw
    except Exception as e:
        logger.warning(f"Ollama summary call failed: {e}")
        return ""


def _inject_summary(file_path: str, summary: str) -> None:
    """Injects a calculated generic summary into an active YAML frontmatter block.

    Args:
        file_path (str): File disk path to augment.
        summary (str): Target text to insert as `summary:`.
    """
    with open(file_path, "r") as f:
        text = f.read()
        
    # Match the closing --- of the respective layout block
    pattern = re.compile(r'^(---\n.*?\n)(---\n)', re.DOTALL)
    m = pattern.match(text)
    if not m:
        return  # no frontmatter detected, skip injection natively
        
    fm_body = m.group(1)
    closing = m.group(2)
    rest = text[m.end():]
    
    # Escape programmatic quotes safely for string interpolation payload
    safe_summary = summary.replace('"', '\\"')
    new_text = fm_body + f'summary: "{safe_summary}"\n' + closing + rest
    with open(file_path, "w") as f:
        f.write(new_text)

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

    resolved = ""
    for candidate in candidates:
        _, resolved_candidate = _resolve_memory_path(candidate)
        if os.path.exists(resolved_candidate):
            file_path = candidate
            resolved = resolved_candidate
            break

    if not resolved:
        raise HTTPException(status_code=404, detail="File not found")
         
    try:
        with open(resolved, "r") as f:
            content = f.read()
            
        result = {
            "file": file_path,
            "content": content,
            "size_bytes": os.path.getsize(resolved)
        }
        
        if meta:
            metadata, _ = parser.parse_markdown(content)
            result["frontmatter"] = metadata
            
        return result
    except Exception as e:
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
        return results[:limit]
    except Exception as e:
        raise _safe_500(e, "Search failed")


@app.post("/search-associative")
def search_associative_api(req: SearchAssociativeRequest) -> list[dict[str, Any]]:
    """Entity graph spreading activation recall."""
    try:
        seed_entities = req.seed_entities
        if not seed_entities:
            seed_entities = store.detect_entities_in_text(req.query)
            
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
            
        store.add_trigger(
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
    return store.list_triggers()


@app.delete("/triggers/{trigger_id}")
def delete_trigger_api(trigger_id: str) -> dict[str, str]:
    """Remove a trigger."""
    store.delete_trigger(trigger_id)
    return {"status": "deleted"}


@app.post("/check-triggers")
def check_triggers_api(req: CheckTriggersRequest) -> list[dict[str, Any]]:
    """Check context against prospective triggers."""
    try:
        emb = embedder.embed(req.query)
        if not emb:
            return []
        results = store.check_triggers(
            query_embedding=emb,
            cooldown_bypass=req.cooldown_bypass or False
        )
        return results
    except Exception as e:
        raise _safe_500(e, "Trigger check failed")


# ── Embedding tools (#210) — Obsidian wiki maintenance helpers ──────────────


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    BGE-M3 outputs are L2-normalized so this reduces to a dot product, but we
    keep the explicit norm denominator for correctness against any embedder
    that doesn't normalize (e.g. Gemini at certain dimensions).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    import math
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _read_memory_body(file_path: str) -> str | None:
    """Read a memory file's full body for re-embedding.  Returns None on miss."""
    try:
        candidates = [file_path]
        if not file_path.endswith(".md"):
            candidates.append(f"{file_path}.md")
        for candidate in candidates:
            try:
                _, resolved = _resolve_memory_path(candidate)
            except HTTPException:
                continue
            if os.path.exists(resolved):
                with open(resolved, "r") as f:
                    return f.read()
    except Exception:
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
        "created_at": _utc_now().isoformat()
    }
    if req.metadata:
        frontmatter_dict.update(req.metadata)
    if req.core is not None:
        frontmatter_dict["core"] = req.core
    if req.confidence is not None:
        frontmatter_dict["confidence"] = req.confidence
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

    # Utilize auto backup procedures explicitly.
    if config.git.auto_commit:
        try:
            subprocess.run(["git", "add", file_path], cwd=config.palinode_dir, check=False)
            commit_msg = f"{config.git.commit_prefix} auto-save: {category}/{slug}.md"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=config.palinode_dir, check=False)
            
            if config.git.auto_push:
                subprocess.run(["git", "push"], cwd=config.palinode_dir, check=False)
        except Exception as e:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    files = store.get_entity_files(entity_ref)
    graph = store.get_entity_graph(entity_ref)
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
def history_api(file_path: str, limit: int = 20) -> dict[str, Any]:
    """Get the change history for a memory file.

    Uses --follow to track renames and includes diff stats per commit.
    """
    commits = git_tools.history(file_path, limit)
    if not commits:
        # Distinguish "file not found" from "no history"
        import os as _os
        full_path = _os.path.join(config.memory_dir, file_path)
        if not _os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")
    return {"file": file_path, "history": commits}


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


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equally-sized vectors.

    Returns 0.0 on shape mismatch or zero-magnitude inputs so the caller
    can treat "incomparable" the same as "not similar enough."
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    import math
    return dot / (math.sqrt(na) * math.sqrt(nb))


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

    # Git commit (covers daily + status + individual file if save_api didn't commit)
    if config.git.auto_commit:
        try:
            files_to_add = [daily_path]
            if status_file:
                files_to_add.append(os.path.join(_memory_base_dir(), status_file))
            for fp in files_to_add:
                subprocess.run(["git", "add", fp], cwd=_memory_base_dir(), check=False)
            commit_msg = f"{config.git.commit_prefix} session-end: {today}"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=_memory_base_dir(), check=False)
            if config.git.auto_push:
                subprocess.run(["git", "push"], cwd=_memory_base_dir(), check=False)
        except Exception as e:
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
            subprocess.run(
                ["git", "add", os.path.join("prompts", "*.md")],
                cwd=_memory_base_dir(), check=False,
            )
            subprocess.run(
                ["git", "commit", "-m", f"palinode: activate prompt {name} for task={task}"],
                cwd=_memory_base_dir(), check=False,
            )
        except Exception as e:
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
    import uvicorn
    uvicorn.run("palinode.api.server:app", host=config.services.api.host, port=config.services.api.port)


if __name__ == "__main__":
    main()
