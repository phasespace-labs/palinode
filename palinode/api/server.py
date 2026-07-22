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
import re
import subprocess  # noqa: F401 — re-export: tests patch `server.subprocess.run`
from pathlib import Path
from urllib.parse import urlparse

from contextlib import asynccontextmanager


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from palinode.core import store, git_tools  # noqa: F401 — git_tools re-export: tests patch `server.git_tools.*`
from palinode.core.auth import (
    API_EXEMPT_PATHS as _API_EXEMPT_PATHS,
    BearerAuthMiddleware as _BearerAuthMiddleware,
    load_api_token as _load_api_token,
    validate_auth_config as _core_validate_auth_config,
)
from palinode.core.config import config

# Re-exported so the health/status routers can reach the client factory via
# `palinode.api.server.get_ollama_client` (late `_srv.get_ollama_client()`
# lookup — see routers/health.py), which is also the patch seam those liveness
# probes target. The enrichment module imports its own copy for its CHAT calls.
from palinode.core.ollama_client import get_ollama_client  # noqa: F401

# ── Re-exports from the focused _shared.py successor modules ──────────
# routers/_shared.py was resolved into path_safety / rate_limit / memory_write /
# search_helpers / _util. These names are re-exported (not redefined) so existing
# `from palinode.api.server import _X` imports — used widely by tests — keep
# resolving against the server module.
from palinode.api._util import (  # noqa: F401
    _auto_summary_state,
    _project_from_cwd,
    _reindex_lock,
    _reindex_state,
    _retrieval_logger,
    _safe_500,
    _utc_now,
)
from palinode.api.path_safety import (  # noqa: F401
    _memory_base_dir,
    _open_memory_file_text,
    _resolve_memory_path,
)
from palinode.api.rate_limit import (  # noqa: F401
    _MAX_REQUEST_BYTES,
    _RATE_LIMIT_MAX_KEYS,
    _RATE_LIMIT_SEARCH,
    _RATE_LIMIT_WINDOW,
    _RATE_LIMIT_WRITE,
    _check_rate_limit,
    _prune_rate_counters,
    _rate_counters,
)
from palinode.api.memory_write import (  # noqa: F401
    _CATEGORY_TO_ENTITY_PREFIX,
    _SAFE_SLUG_RE,
    _TYPE_TO_CATEGORY,
    _WIKI_FOOTER_MARKER,
    _apply_wiki_footer,
    _is_description_eligible,
    _normalize_entities,
    _resolve_source,
    _safe_wiki_slug,
)
from palinode.api.search_helpers import (  # noqa: F401
    _check_session_end_dedup,
    _compute_effective_date_after,
    _cosine,
    _cosine_similarity,
    _embedding_candidates,
    _enrich_with_snippets,
    _filter_min_priority,
    _filter_type_deny,
    _filter_types,
    _priority_value,
    _read_memory_body,
    _rerank_with_preprocessing,
    _resolve_snippet_max_chars,
    _windowed_snippet,
)


logger = logging.getLogger("palinode.api")
logger.setLevel(getattr(logging, config.services.api.log_level.upper(), logging.INFO))

# ── Secret redaction (L4) ───────────────────────────────────────────────────
# Memory files routinely contain credentials (API keys, tokens, basic-auth
# URLs) and any error path that calls logger.exception() will surface those in
# tracebacks/locals. The patterns below are scrubbed from log messages and
# exception text before emission. Patterns live in a module constant so an
# operator can audit exactly what's recognised.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anthropic-style sk-ant-... (must come before generic sk- so the longer
    # prefix wins).
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***REDACTED***"),
    # OpenAI / generic sk-...
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-***REDACTED***"),
    # Slack bot/user/app tokens
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "xox*-***REDACTED***"),
    # GitHub personal-access tokens
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "gh*_***REDACTED***"),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***REDACTED***"),
    # Basic-auth credentials embedded in URLs: scheme://user:password@host
    (
        re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]+):([^/\s:@]+)@"),
        r"\1\2:***REDACTED***@",
    ),
    # Generic 32+ char hex/base64 tokens preceded by a header-name keyword.
    # Covers `api_key=...`, `token: ...`, `Authorization: Bearer ...`,
    # `Bearer ...` (common bare prefix in HTTP traces), etc.
    (
        re.compile(
            r"((?:api[_\-]?key|bearer|authorization|token)"
            r"(?:\s*[:=]\s*(?:bearer\s+)?|\s+)[\"']?)"
            r"([A-Za-z0-9_\-=+/.]{32,})",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***",
    ),
)


def _redact_secrets(text: str) -> str:
    """Apply ``_SECRET_PATTERNS`` to *text*.  Returns text unchanged on no match."""
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Strip credentials from log messages and traceback text before emission.

    Mutates ``record.msg`` (after expansion) so that downstream formatters and
    handlers see scrubbed content. ``exc_text`` is rebuilt lazily by the
    formatter; we redact it here too if it has been pre-rendered, and we
    pre-render-and-redact when ``exc_info`` is present so the cached value
    handlers later rely on (``record.exc_text``) is already safe.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Render the formatted message once, then re-flow it as a literal so
        # downstream %-style formatting does not re-expand any redacted args.
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 — never let a logging filter raise
            rendered = str(record.msg)
        scrubbed = _redact_secrets(rendered)
        if scrubbed != rendered or record.args:
            record.msg = scrubbed
            record.args = None

        # Render and scrub the traceback up front so handlers see the
        # redacted version (Formatter caches via record.exc_text).
        if record.exc_info and not record.exc_text:
            record.exc_text = _redact_secrets(
                logging.Formatter().formatException(record.exc_info)
            )
        elif record.exc_text:
            record.exc_text = _redact_secrets(record.exc_text)

        return True


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

    # when auto_commit is enabled but memory_dir is not a git
    # repository, every /save's `git add` + `git commit` silently no-ops
    # (subprocess prints "fatal: not a git repository" but check=False
    # eats the exit code). Saves keep landing on disk, history vanishes,
    # the operator never knows. Warn-once at startup with the exact
    # `git init` command to fix it. Warn-once, not per-save, since
    # repeated failures get noisy at scale.
    if config.git.auto_commit and not (Path(config.memory_dir) / ".git").exists():
        _startup_logger.warning(
            "PALINODE_DIR %s is not a git repository — config.git.auto_commit "
            "is enabled but every save's git commit will silently no-op. "
            "Run `git init %s` to enable version-controlled saves, or set "
            "config.git.auto_commit=false to suppress this warning.",
            config.memory_dir,
            config.memory_dir,
        )

    # Refuse to start if the db_path parent doesn't exist — sqlite3.connect()
    # would silently auto-create the DB in a non-existent directory (raising an
    # OperationalError on first write), producing silent 500s.
    _db_parent = Path(config.db_path).parent
    if not _db_parent.exists():
        raise RuntimeError(
            f"Cannot start: db_path parent directory does not exist: {_db_parent}. "
            f"Create the directory or update db_path in palinode.config.yaml."
        )

    try:
        store.init_db()
    except RuntimeError as exc:
        # misconfiguration guard in store._ensure_db — DB missing but
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

# Reindex concurrency guard and auto_summary observability state
# live in palinode/api/_util.py so the handlers that read them — now in
# routers/maintenance.py and routers/health.py — share one source of truth.
# Re-exported at the top of this module for compatibility.

# ── Security middleware ──────────────────────────────────────────────────────

# CORS: restrict to configured origins (default: localhost only).
# I3: validate the env var so a wildcard or malformed value cannot silently
# disable origin checks. Wildcards are rejected outright; each entry must
# parse with an http/https scheme and a non-empty netloc.


def _parse_cors_origins(raw: str) -> list[str]:
    """Validate and normalize PALINODE_CORS_ORIGINS.

    - Reject literal '*' (with or without surrounding whitespace, anywhere
      in the comma-separated list) — silent wildcard CORS is the failure
      mode the marketplace flagged.
    - Strip whitespace and skip empty entries.
    - Each origin must parse as http(s)://host[:port][/path]; missing
      scheme or netloc raises ValueError.
    """
    origins: list[str] = []
    for raw_origin in raw.split(","):
        origin = raw_origin.strip()
        if not origin:
            continue
        if origin == "*":
            raise ValueError(
                "refusing to start with CORS wildcard — set "
                "PALINODE_CORS_ORIGINS to specific origins"
            )
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"invalid CORS origin {origin!r}: must be a full http(s) URL"
            )
        origins.append(origin)
    if not origins:
        raise ValueError(
            "PALINODE_CORS_ORIGINS resolved to an empty list — set at least "
            "one valid origin or unset the variable to use the default"
        )
    return origins


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

# ── Bearer token auth (Tier A finding — closes the last "high" from the
# marketplace security scan). Default-off to preserve zero-friction local
# dev: when no token is configured, the middleware is a no-op pass-through.
# When a token IS configured (PALINODE_API_TOKEN or PALINODE_API_TOKEN_FILE),
# every request except the health endpoints must carry a matching
# `Authorization: Bearer <token>` header. The startup gate further refuses
# to launch when PALINODE_API_BIND_INTENT=public is set without a token, so
# operators can't accidentally expose an unauthenticated API to the network.
#
# _BearerAuthMiddleware, _load_api_token, and the core gate logic live in
# palinode.core.auth and are imported above; the MCP HTTP server reuses them.

_api_token: str | None = _load_api_token()


def _validate_auth_config(token: str | None) -> None:
    """Refuse to start when binding public without a token.

    Fires at MODULE IMPORT (see call site below the function), so the
    SystemExit propagates out of any startup path — including ``uvicorn``
    invoked directly with ``palinode.api.server:app`` (the canonical
    systemd ExecStart pattern), which never calls ``main()``. A second
    call in ``main()`` is kept for defence in depth.

    Thin wrapper around ``palinode.core.auth.validate_auth_config``; kept
    here so the module-level gate call and the existing test suite continue
    to work unchanged.
    """
    _core_validate_auth_config(_bind_intent_public, token)


# Fire the gate at import time so it triggers under any startup path
# (CLI entry point ``palinode-api`` AND ``uvicorn palinode.api.server:app``).
# The canonical systemd ExecStart pattern uses uvicorn directly, which
# imports the module to read the ``app`` attribute but never calls
# ``main()``. Module-scope invocation ensures the SystemExit propagates
# regardless of how the server is brought up.
_validate_auth_config(_api_token)

# Registered after CORS so CORS-applied origin headers wrap auth failures,
# and before _BodySizeLimitMiddleware so unauthenticated callers can't
# spend bandwidth streaming a body that will be rejected anyway. The
# middleware is a cheap no-op when _api_token is None.
app.add_middleware(_BearerAuthMiddleware, token=_api_token, exempt_paths=_API_EXEMPT_PATHS)
if _api_token is not None:
    logger.info("API bearer-token auth: enabled")
else:
    logger.info("API bearer-token auth: disabled (no PALINODE_API_TOKEN)")

class _BodySizeLimitMiddleware:
    """ASGI middleware enforcing _MAX_REQUEST_BYTES on the *streamed* body.

    Tied to the marketplace security review (Tier B finding #3). The previous
    implementation only inspected the ``Content-Length`` header, which an
    attacker can omit entirely (HTTP/1.1 chunked encoding) or under-report
    relative to the actual streamed body. This wraps the ASGI ``receive``
    callable and tallies bytes as the body chunks arrive; once the running
    total exceeds the limit we short-circuit with 413 Payload Too Large and
    stop reading from the client.

    Because the downstream framework (Starlette) catches the receive-time
    abort and would otherwise surface its own 400 "error parsing the body",
    ``send`` is wrapped to override that with the correct 413 end-to-end.

    Header-based fast-path is preserved so well-behaved clients with an
    accurate ``Content-Length`` get rejected before any body is buffered.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Header fast-path: if the client supplied a Content-Length we trust
        # it as a tripwire (reject early) but still verify during streaming
        # in case the header lies.
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value.decode("latin-1"))
                except (UnicodeDecodeError, ValueError):
                    await self._send_413(send)
                    return
                if declared > self.max_bytes:
                    await self._send_413(send)
                    return
                break

        received = 0
        over_limit = False
        sent_413 = False

        async def limited_receive():
            nonlocal received, over_limit
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"") or b""
                received += len(body)
                if received > self.max_bytes:
                    # Short-circuit: raise so the receive loop unwinds. The
                    # downstream app should never see this oversized body.
                    over_limit = True
                    raise _BodyTooLargeError()
            return message

        async def guarded_send(message):
            # Once the limit is tripped, the downstream framework (Starlette)
            # catches the receive() exception and tries to surface its OWN
            # response — a 400 "error parsing the body". Override that with the
            # correct 413 Payload Too Large and swallow everything else it
            # emits, so the client sees 413 end-to-end (not just when the
            # exception happens to propagate uncaught).
            nonlocal sent_413
            if over_limit:
                if not sent_413:
                    sent_413 = True
                    await self._send_413(send)
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLargeError:
            # Reached when the downstream app does NOT catch the receive error
            # (e.g. a raw ASGI app that never started a response).
            if not sent_413:
                await self._send_413(send)

    @staticmethod
    async def _send_413(send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"connection", b"close"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"Request body too large"}',
            }
        )


class _BodyTooLargeError(Exception):
    """Internal sentinel for the body-size middleware."""


app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)

# Startup warning for unsafe binding.
# Set PALINODE_API_BIND_INTENT=public to suppress the warning for intentional
# network-exposed deployments (e.g., Tailscale). Without the env var, the
# warning fires on every 0.0.0.0 start.
# (_api_host and _bind_intent_public are resolved earlier so the bearer-auth
# startup gate can reference them; this block reuses the same values.)
# B104 rationale - "0.0.0.0" here is a literal compared to the resolved host;
# the actual bind decision is gated on PALINODE_API_BIND_INTENT=public
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

# ── Enrichment re-exports ─────────────────────────────────────────────
# Auto-summary/description logic lives in palinode/api/enrichment.py. Re-exported
# here (not redefined) so `patch("palinode.api.server._generate_description")` in
# tests rebinds the name routers/memory.py looks up via `_srv.<name>`, and so the
# _DESCRIPTION_DEFERRED sentinel + _fallback_state dict stay shared by reference.
from palinode.api.enrichment import (  # noqa: E402,F401
    _DESCRIPTION_DEFERRED,
    _LLM_PREAMBLE_RE,
    _chat_fallback_oneliner,
    _chat_primary_oneliner,
    _clean_llm_oneliner,
    _extract_first_line,
    _fallback_state,
    _generate_description,
    _generate_summary,
    _inject_description,
    _inject_summary,
    _wrap_user_content_for_llm,
)

#: The memory-category directories `save_api` writes to. A file outside these
#: (a `daily/` journal, `archive/`, `specs/` incl. `specs/prompts/`, or a
#: top-level doc like README.md / PROGRAM.md) is structural / non-memory: the
#: description backfill regenerates a description for it every run but
#: `_inject_description` never persists one (no memory frontmatter to land it
#: in), so counting it as "pending" loops the backfill forever.
_MEMORY_CATEGORY_DIRS: frozenset[str] = frozenset(_TYPE_TO_CATEGORY.values())




# ── Register sub-routers (routes moved from this module) ─────────────────────
from palinode.api.routers.consolidation import router as _consolidation_router  # noqa: E402
from palinode.api.routers.context import router as _context_router  # noqa: E402
from palinode.api.routers.git_history import router as _git_history_router  # noqa: E402
from palinode.api.routers.health import router as _health_router  # noqa: E402
from palinode.api.routers.maintenance import router as _maintenance_router  # noqa: E402
from palinode.api.routers.memory import router as _memory_router  # noqa: E402
from palinode.api.routers.search import router as _search_router  # noqa: E402
from palinode.api.routers.session import router as _session_router  # noqa: E402
from palinode.api.routers.triggers import router as _triggers_router  # noqa: E402
app.include_router(_triggers_router)
app.include_router(_consolidation_router)
app.include_router(_git_history_router)
app.include_router(_memory_router)
app.include_router(_search_router)
app.include_router(_health_router)
app.include_router(_maintenance_router)
app.include_router(_session_router)
app.include_router(_context_router)

# ── Local read-only provenance UI (Phase 0) ─────────────────────────────────
# Server-rendered HTML under /ui — no new service, loopback-only, read-only.
# Mounted after the API routers so the JSON API surface is unchanged; the UI
# is a pure client of existing capabilities (status/lint/read/git lineage).
from palinode.api.ui.router import (  # noqa: E402
    router as _ui_router,
    mount_static as _ui_mount_static,
)
app.include_router(_ui_router)
_ui_mount_static(app)

# Re-exports so existing `from palinode.api.server import X` imports still work.
from palinode.api.routers.memory import (  # noqa: E402,F401
    SaveRequest, list_api, save_api, read_api,
    generate_summaries_api,
)
from palinode.api.routers.search import (  # noqa: E402,F401
    SearchRequest, SearchAssociativeRequest, DedupSuggestRequest,
    OrphanRepairRequest, ClusterNeighborsRequest, TopicCoverageRequest,
    search_api, search_associative_api, dedup_suggest_api,
    orphan_repair_api, cluster_neighbors_api, topic_coverage_api,
)
from palinode.api.routers.triggers import (  # noqa: E402,F401
    TriggerRequest, CheckTriggersRequest, create_trigger_api,
    list_triggers_api, delete_trigger_api, check_triggers_api,
)
from palinode.api.routers.consolidation import (  # noqa: E402,F401
    ConsolidateRequest, ArchiveExpiredRequest, ArchiveRequest, consolidate_api,
    archive_expired_api, archive_api, split_layers_api, bootstrap_fact_ids_api,
)
from palinode.api.routers.git_history import (  # noqa: E402,F401
    history_api, timeline_api, diff_api, blame_api, rollback_api,
    push_api, git_stats_api,
)
from palinode.api.routers.health import (  # noqa: E402,F401
    status_api, health_api, watcher_health_api,
    auto_summary_health_api, doctor_api,
)
from palinode.api.routers.maintenance import (  # noqa: E402,F401
    MigrateOpenClawRequest, ingest_api, ingest_url_api,
    rebuild_fts_api, reindex_api, entity_api, entities_list_api,
    lint_api, migrate_openclaw_api, depends_unblocked_api,
    depends_api, migrate_mem0_api,
)
from palinode.api.routers.session import (  # noqa: E402,F401
    SessionEndRequest, session_end_api, list_prompts_api,
    get_prompt_api, activate_prompt_api,
)


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
