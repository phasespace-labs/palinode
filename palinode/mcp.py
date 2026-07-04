"""
Palinode MCP Server

Exposes Palinode memory as MCP tools for Claude Code and other MCP clients.
Runs over stdio — spawned on demand by the client.

All tool implementations are thin HTTP wrappers around the Palinode API server.
The MCP server itself holds no database connections, embedder state, or git handles.
Set PALINODE_API_HOST to point at a remote API server (e.g. over Tailscale).

Tools:
  palinode_search  — semantic search over memory files
  palinode_save    — write a new memory item
  palinode_ingest  — ingest a URL into research memory
  palinode_status  — health check + index stats

Usage (Claude Code / claude_desktop_config.json):
  {
    "mcpServers": {
      "palinode": {
        "command": "palinode-mcp",
        "env": {
          "PALINODE_API_HOST": "your-server"
        }
      }
    }
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from palinode.core.audit import AuditLogger
from palinode.core.config import ToolSurface, config, validate_tool_surface
from palinode.core.defaults import (
    SAVE_SOURCE_HEADER as _SOURCE_HEADER,
    SESSION_END_TIMEOUT_SECONDS as _SESSION_END_TIMEOUT,
    _SESSION_END_TIMEOUT_SENTINEL as _SENTINEL,
)

logger = logging.getLogger("palinode.mcp")
logging.basicConfig(level=logging.WARNING)  # quiet — don't pollute stdio

server = Server("palinode")
_audit = AuditLogger(config.memory_dir, config.audit)


def _coerce_str_array(value: Any) -> Any:
    """Tolerate JSON-encoded array strings from MCP clients that double-encode.

    Some MCP transports/clients serialize array arguments as JSON strings
    (e.g. ``'["a","b"]'``) instead of native arrays. FastAPI's Pydantic
    validation rejects those with "expected array, received string". This
    helper decodes the string form when it's clearly a JSON array; otherwise
    it returns ``value`` unchanged so native lists pass through.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
        if isinstance(decoded, list):
            return decoded
    return value


def _resolve_context() -> list[str] | None:
    """Resolve ambient project context from environment (ADR-008).

    Resolution order:
    1. PALINODE_PROJECT env var (explicit entity ref, e.g. "project/palinode")
    2. CWD basename → config.context.project_map lookup
    3. CWD basename → auto-detect as project/{basename} (if auto_detect=True)
    """
    if not config.context.enabled:
        return None

    # 1. Explicit env var
    explicit = os.environ.get("PALINODE_PROJECT")
    if explicit:
        return [explicit] if "/" in explicit else [f"project/{explicit}"]

    # 2/3. CWD-based resolution
    cwd = os.environ.get("CWD") or os.getcwd()
    basename = os.path.basename(cwd)
    if not basename:
        return None

    # Check config map
    if basename in config.context.project_map:
        entity = config.context.project_map[basename]
        return [entity] if "/" in entity else [f"project/{entity}"]

    # Auto-detect
    if config.context.auto_detect:
        return [f"project/{basename}"]

    return None


# ── HTTP client helpers ──────────────────────────────────────────────────────

def _api_url(path: str) -> str:
    """Build full API URL from config host/port."""
    host = config.services.api.host
    port = config.services.api.port
    return f"http://{host}:{port}{path}"


# Cross-surface drift guard: assert the constant matches its sentinel
# unless the operator has set an explicit env-var override.
assert _SESSION_END_TIMEOUT == _SENTINEL or os.environ.get(
    "PALINODE_SESSION_END_TIMEOUT"
), (
    f"SESSION_END_TIMEOUT_SECONDS ({_SESSION_END_TIMEOUT}) differs from sentinel "
    f"({_SENTINEL}) without PALINODE_SESSION_END_TIMEOUT override — "
    "update mcp.py or defaults.py to stay in sync (#377)"
)

_DEFAULT_HEADERS = {_SOURCE_HEADER: "mcp"}


async def _get(path: str, params: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP GET to the API server."""
    async with httpx.AsyncClient(headers=_DEFAULT_HEADERS) as client:
        return await client.get(_api_url(path), params=params, timeout=timeout)


async def _post(path: str, json: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP POST to the API server."""
    async with httpx.AsyncClient(headers=_DEFAULT_HEADERS) as client:
        return await client.post(_api_url(path), json=json, timeout=timeout)


async def _post_params(path: str, params: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP POST with query params (no JSON body) to the API server."""
    async with httpx.AsyncClient(headers=_DEFAULT_HEADERS) as client:
        return await client.post(_api_url(path), params=params, timeout=timeout)


async def _delete(path: str, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP DELETE to the API server."""
    async with httpx.AsyncClient(headers=_DEFAULT_HEADERS) as client:
        return await client.delete(_api_url(path), timeout=timeout)


def _text(content: str) -> list[types.TextContent]:
    """Shorthand for returning a single text result."""
    return [types.TextContent(type="text", text=content)]


# write-path tools can commit server-side even when the client's request
# times out. A slow LLM-derived field (auto_summary, embedding refresh) can
# outlast the HTTP timeout *after* the durable write has already landed, so the
# generic "Request ... timed out" message led operators to retry blindly and
# create duplicate entries. For these tools, surface the verify-before-retry
# path instead.
_WRITE_PATH_TOOLS = frozenset({"palinode_save", "palinode_session_end"})


def _timeout_message(tool: str) -> str:
    """Build the client-facing message for an httpx timeout (#416).

    Write-path tools get a verify-before-retry hint because the save may have
    succeeded server-side; read-path tools keep the plain timeout message.
    """
    if tool in _WRITE_PATH_TOOLS:
        return (
            f"Timeout: `{tool}` did not return before the request timeout. "
            "The write may have succeeded server-side — a slow auto-summary or "
            "embedding step can outlast the timeout after the durable save has "
            "already landed. Before retrying, call `palinode_search` with a "
            "distinctive phrase from your content to confirm whether it saved; "
            "retrying blindly can create a duplicate entry."
        )
    return f"Error: Request to {_api_url('')} timed out."


_FULL_CONTENT_HARD_CAP = 4000  # Politeness ceiling for full=True.


def _format_results(results: list[dict[str, Any]], full: bool = False) -> str:
    """Format search results as clean text — minimal context burn.

    Renders ``snippet`` by default (populated by ``/search`` per #352) so
    pathologically large chunks don't blow the MCP tool-result budget. When
    ``full=True``, renders ``content`` capped at ``_FULL_CONTENT_HARD_CAP``;
    callers that want untruncated bodies should use ``palinode_read``.

    Falls back to a defensive 400-char ``content`` slice if neither field is
    populated (older API or external caller).
    """
    if not results:
        return "No results found."
    parts = []
    any_truncated = False
    for r in results:
        file_path = r.get("file_path", "")
        # Strip absolute prefix if present
        if "/" in file_path:
            rel = file_path.rsplit("/palinode/", 1)[-1] if "/palinode/" in file_path else file_path
        else:
            rel = file_path
        score_pct = int(r.get("score", 0) * 100)
        freshness = r.get("freshness")
        fresh_label = f" ✓ {freshness}" if freshness == "valid" else (f" ⚠ {freshness}" if freshness == "stale" else "")
        # Render external_refs when present in result metadata.
        meta = r.get("metadata") or {}
        ext_refs = meta.get("external_refs")
        refs_label = ""
        if ext_refs and isinstance(ext_refs, dict):
            _PRETTY_KEYS = {
                "gitlab_mr": "MR",
                "gitlab_issue": "Issue",
                "gitlab_pipeline": "Pipeline",
                "github_pr": "PR",
                "linear_issue": "Linear",
                "jira_issue": "Jira",
            }
            ref_parts = [
                f"{_PRETTY_KEYS.get(k, k)}: {v}" for k, v in ext_refs.items()
            ]
            refs_label = " [" + ", ".join(ref_parts) + "]"

        # ADR-018: surface a non-default epistemic marker so a reader sees
        # at a glance that a hit is an inference or an open question rather than a
        # verified fact. `fact` (the default) is left unlabelled to avoid noise.
        epi = meta.get("epistemic")
        epi_label = ""
        if epi in ("inference", "open_question"):
            epi_label = " [inference]" if epi == "inference" else " [open question?]"

        # pick body — snippet (default) or capped content (full=True).
        if full:
            body = (r.get("content") or "")[:_FULL_CONTENT_HARD_CAP]
            if r.get("content") and len(r["content"]) > _FULL_CONTENT_HARD_CAP:
                body = body.rstrip() + "…"
                any_truncated = True
        else:
            body = r.get("snippet")
            if body is None:
                # Defensive fallback for callers that bypass the snippet
                # enrichment path. 400 matches snippet_max_chars default.
                body = (r.get("content") or "")[:400]
            if r.get("content_truncated"):
                any_truncated = True

        parts.append(
            f"[{rel}] ({score_pct}% match){fresh_label}{epi_label}{refs_label}\n{(body or '').strip()}"
        )

    rendered = "\n\n---\n\n".join(parts)
    if any_truncated and not full:
        rendered += (
            "\n\n(some results truncated — call palinode_search with full=true, "
            "or palinode_read <file> for the complete text.)"
        )
    return rendered


def _resolve_save_type(arg_type: str | None, arg_ps: bool | None) -> str:
    """Resolve the effective `type` for palinode_save (#136).

    Either ``arg_type`` (one of the enum values) or ``arg_ps=True``
    (ProjectSnapshot shortcut) must be set. ``arg_ps=True`` combined with a
    ``type`` other than ``"ProjectSnapshot"`` is a conflict and raises.
    """
    if arg_ps and arg_type and arg_type != "ProjectSnapshot":
        raise ValueError(
            f"ps=true conflicts with type='{arg_type}' — "
            "the ps shortcut is only for ProjectSnapshot memories."
        )
    if arg_ps:
        return "ProjectSnapshot"
    if arg_type:
        return arg_type
    raise ValueError(
        "must specify either 'type' (one of the enum values) "
        "or 'ps=true' (shortcut for ProjectSnapshot)."
    )


# ── Tool definitions ──────────────────────────────────────────────────────────

CORE_TOOL_NAMES = frozenset(
    {
        "palinode_save",
        "palinode_search",
        "palinode_read",
        "palinode_session_end",
        "palinode_status",
        "palinode_push",
        "palinode_list",
        "palinode_entities",
        "palinode_trigger",
        "palinode_ingest",
        "palinode_doctor",
    }
)


def _resolve_tool_surface() -> ToolSurface:
    if "PALINODE_MCP_SURFACE" in os.environ:
        return validate_tool_surface(
            os.environ["PALINODE_MCP_SURFACE"], "PALINODE_MCP_SURFACE"
        )
    return validate_tool_surface(config.tool_surface)


def _all_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="palinode_list",
            description=(
                "List memory files, optionally filtered by category or core status. "
                "Use to browse what memories exist before reading or searching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category: people, projects, decisions, insights, research",
                        "enum": ["people", "projects", "decisions", "insights", "research"],
                    },
                    "core_only": {
                        "type": "boolean",
                        "description": "If true, only return files with core: true in frontmatter",
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="List Memory Files",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_read",
            description=(
                "Read the full contents of a memory file. Use after palinode_list or palinode_search "
                "to see the complete content of a specific file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path to the memory file (e.g., 'people/alice.md', 'projects/palinode-status.md')",
                    },
                    "meta": {
                        "type": "boolean",
                        "description": (
                            "If true, the response includes parsed frontmatter "
                            "alongside the body.  Default false (body only) to "
                            "match prior behavior."
                        ),
                        "default": False,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Read Memory File",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_search",
            description=(
                "Search Palinode memory for relevant context about people, projects, "
                "decisions, insights, or research. Returns the most relevant memory "
                "file excerpts ranked by semantic similarity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category (memory directory name): people, projects, decisions, insights, research",
                        "enum": ["people", "projects", "decisions", "insights", "research"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Max results to return (default {config.search.default_limit})",
                        "default": config.search.default_limit,
                    },
                    "date_after": {
                        "type": "string",
                        "description": "Filter results after an ISO date (e.g. 2024-01-01)",
                    },
                    "date_before": {
                        "type": "string",
                        "description": "Filter results before an ISO date",
                    },
                    "include_daily": {
                        "type": "boolean",
                        "description": "Include daily session notes at full rank (default: false, daily/ files are penalized)",
                        "default": False,
                    },
                    "include_telemetry": {
                        "type": "boolean",
                        # Telemetry stays out of default recall so monitoring
                        # churn does not pollute human memory search.
                        "description": "Include machine/monitor telemetry memories.",
                        "default": False,
                    },
                    "since_days": {
                        "type": "integer",
                        "description": (
                            "Only return memories created/updated in the last "
                            "N days.  Equivalent to setting `date_after` to "
                            "now-N days; the API derives one from the other."
                        ),
                    },
                    "types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "PersonMemory",
                                "Decision",
                                "ProjectSnapshot",
                                "Insight",
                                "ResearchRef",
                                "ActionItem",
                            ],
                        },
                        "description": "Filter by memory type (matches frontmatter `type`).",
                    },
                    "min_priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Only return memories with human-assigned priority at least this value. Missing priority counts as normal (3).",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Override similarity threshold (0.0-1.0); higher is stricter.",
                    },
                    "full": {
                        "type": "boolean",
                        # Default snippets keep search results within MCP
                        # budget; full=True still caps rendered content.
                        "description": "Return full chunk content instead of snippets.",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
            annotations=types.ToolAnnotations(
                title="Search Memory",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_save",
            description=(
                "Save a memory to Palinode. Use for important facts, decisions, insights, "
                "or project updates worth remembering across sessions. Provide either "
                "`type` (one of the enum values) or `ps=true` for the ProjectSnapshot "
                "shortcut — exactly one is required. "
                "If this call times out, the save may still have committed server-side: "
                "call `palinode_search` with a distinctive phrase from your content to "
                "confirm before retrying, so you don't create a duplicate entry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content to save (markdown supported)",
                    },
                    "type": {
                        "type": "string",
                        "description": "Memory type. Required unless `ps=true` is given.",
                        "enum": ["PersonMemory", "Decision", "ProjectSnapshot", "Insight", "ResearchRef", "ActionItem"],
                    },
                    "ps": {
                        "type": "boolean",
                        "description": "Shorthand for type=ProjectSnapshot — matches the CLI `--ps` flag and the `/ps` slash command. If true, `type` may be omitted (or set to ProjectSnapshot redundantly); other type values conflict and error.",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Optional URL-safe filename slug (auto-generated if omitted)",
                    },
                    "core": {
                        "type": "boolean",
                        "description": "If true, this memory is always injected at session start (core memory).",
                    },
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Related entity refs e.g. ['person/alice', 'project/alpha']",
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Project slug shorthand — e.g. 'palinode' becomes "
                            "entity 'project/palinode'.  Pairs with "
                            "`palinode_session_end`'s `project` field for "
                            "consistent project tagging across save and "
                            "session-end."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Optional human-readable title.  Stored in "
                            "frontmatter and used in list/search displays."
                        ),
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Additional frontmatter fields to merge into the saved memory.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence in this memory's accuracy (0.0-1.0).",
                    },
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Human-assigned memory priority (1–5). Stored as `priority` frontmatter; missing means normal (3).",
                    },
                    "epistemic": {
                        "type": "string",
                        "enum": ["fact", "inference", "open_question"],
                        # ADR-018: the KIND of claim this memory makes.
                        # Omitting it leaves the memory `unmarked` (no claim —
                        # NOT fact); no frontmatter is written.
                        "description": "Epistemic marker: 'fact' (observed/verified), 'inference' (derived, lower trust), or 'open_question' (unresolved). Omit to leave the memory unmarked (no claim is made — not treated as fact).",
                    },
                    "external_refs": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        # External refs preserve SDLC provenance while still
                        # allowing integration-specific keys.
                        "description": "SDLC object references such as github_pr or jira_issue.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source surface that created this memory.",
                    },
                    "update_policy": {
                        "type": "string",
                        "enum": ["append", "replace"],
                        # append is episodic; replace marks a sticky living
                        # document protected from history-forking compaction.
                        "description": "Save behavior: append episodic memory or replace a living document.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ref": {"type": "string", "description": "Path under the memory dir of the cited source."},
                                "quote": {"type": "string", "description": "The exact passage cited from the source."},
                                "quote_hash": {"type": "string", "description": "Optional integrity hash; computed on save if omitted."},
                            },
                            "required": ["ref", "quote"],
                        },
                        # Source-citation anchors: each anchors a memory
                        # to the exact passage it cites. quote_hash is computed
                        # server-side when omitted; the verifier reads these back.
                        "description": "Source-citation anchors: list of {ref, quote, quote_hash} for passages this memory cites.",
                    },
                    "contradicts": {
                        "type": "array",
                        "items": {"type": "string"},
                        # (G4): typed conflict link. Records that this memory
                        # conflicts with the listed refs WITHOUT picking a winner
                        # (that's supersession's job). Surfaced by `palinode lint`.
                        "description": "Refs (category/slug) this memory conflicts with; neither wins — surfaced for review.",
                    },
                    "backed_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        # (G4): typed evidence link — this memory is supported
                        # by the listed source/fact refs.
                        "description": "Refs (category/slug) that support/back this memory (evidence links).",
                    },
                },
                "required": ["content"],
            },
            annotations=types.ToolAnnotations(
                title="Save Memory",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_ingest",
            description="Fetch a URL and save it as a research reference in Palinode memory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch and ingest",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional title/name for the reference",
                    },
                },
                "required": ["url"],
            },
            annotations=types.ToolAnnotations(
                title="Ingest URL",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
            ),
        ),
        types.Tool(
            name="palinode_status",
            description="Check Palinode health: API reachability, index stats, last watcher run.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
            annotations=types.ToolAnnotations(
                title="Health Status",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_history",
            description=(
                "Show the change history of a memory file. Tracks renames (--follow) "
                "and includes diff stats per commit. Use detail='full' for the commit-level "
                "evolution view (previously palinode_timeline)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to the memory directory (e.g. people/alice.md)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of commits to show (default 20)",
                        "default": 20,
                    },
                    "detail": {
                        "type": "string",
                        "description": (
                            "'summary' (default) returns hash/date/message/stats. "
                            "'full' additionally includes the unified diff body per commit "
                            "(commit-level evolution view, formerly palinode_timeline)."
                        ),
                        "enum": ["summary", "full"],
                        "default": "summary",
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="File History",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_timeline",
            description=(
                "Deprecated: use palinode_history with detail='full' instead. "
                "Shows commit-level evolution of a memory file including unified diffs per commit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to the memory directory (e.g. people/alice.md)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of commits to show (default 20)",
                        "default": 20,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="File Timeline (deprecated — use palinode_history detail=full)",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_entities",
            description="List all known entities, or get memory files referencing a specific entity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_ref": {
                        "type": "string",
                        "description": "Optional entity reference (e.g. person/alice) to lookup files."
                    }
                },
            },
            annotations=types.ToolAnnotations(
                title="Entity Graph",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_consolidate",
            description=(
                "Run a manual knowledge consolidation pass.  Set `dry_run=true` "
                "to preview the proposed operations without applying them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": (
                            "Preview operations without writing changes.  "
                            "Recommended when invoking from MCP — the tool is "
                            "annotated destructive."
                        ),
                        "default": False,
                    },
                    "nightly": {
                        "type": "boolean",
                        "description": (
                            "Run the nightly compaction prompt instead of the "
                            "default write-time pass."
                        ),
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Run Consolidation",
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_archive_expired",
            description=(
                "Archive ephemeral memories whose `expires_at` has passed "
                "(ADR-015 §2.3 TTL regime). Deterministic + idempotent — flips "
                "expired memories to status: archived so they drop out of default "
                "recall while staying on disk. Set `dry_run=true` to preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview which memories would be archived without writing.",
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Archive Expired",
                readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_diff",
            description=(
                "Show what memories changed recently. Use to review what was learned, "
                "decisions made, or facts updated in the last N days."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Look back this many days (default 7)",
                        "default": 7,
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter to specific directories (e.g., ['projects/', 'decisions/'])",
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Recent Changes",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_blame",
            description=(
                "Trace a fact back to when it was first recorded. Shows which session "
                "or commit created each line in a memory file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Memory file path (e.g., 'projects/my-app.md')",
                    },
                    "file": {
                        "type": "string",
                        "description": "Deprecated alias for `file_path`; use `file_path` instead.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Optional: filter to lines containing this text",
                    },
                },
                # `file_path` or `file` (legacy) — validated in the dispatcher.
            },
            annotations=types.ToolAnnotations(
                title="Blame / Provenance",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_rollback",
            description=(
                "Revert a memory file to a previous version. Safe: creates a new commit "
                "preserving the old version in history. Defaults to dry run."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Memory file path to rollback",
                    },
                    "file": {
                        "type": "string",
                        "description": "Deprecated alias for `file_path`; use `file_path` instead.",
                    },
                    "commit": {
                        "type": "string",
                        "description": "Target commit hash (from palinode_history). Default: previous version.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), show what would change without applying.",
                        "default": True,
                    },
                },
                # `file_path` or `file` (legacy) — validated in the dispatcher.
            },
            annotations=types.ToolAnnotations(
                title="Rollback File",
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_push",
            description="Sync memory changes to GitHub for backup and cross-machine access.",
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title="Push to Remote",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
            ),
        ),
        types.Tool(
            name="palinode_trigger",
            description=(
                "Register or manage a prospective trigger for Palinode. When a future user message semantically "
                "matches the description, the specified memory file will be automatically injected."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action to perform: 'create', 'list', or 'delete'",
                        "enum": ["create", "list", "delete"],
                        "default": "create",
                    },
                    "description": {
                        "type": "string",
                        "description": "For 'create': What context should fire this trigger (e.g., 'User is discussing deployment')",
                    },
                    "memory_file": {
                        "type": "string",
                        "description": "For 'create': Relative path to the memory file to inject when fired (e.g., 'projects/my-app.md')",
                    },
                    "trigger_id": {
                        "type": "string",
                        "description": "For 'delete' or 'create': Custom UUID or ID to delete/create",
                    },
                    "threshold": {
                        "type": "number",
                        "description": (
                            "For 'create': Similarity threshold (0.0–1.0).  "
                            "Higher = stricter match required to fire.  "
                            "Default 0.75."
                        ),
                    },
                    "cooldown_hours": {
                        "type": "integer",
                        "description": (
                            "For 'create': Hours to wait between consecutive "
                            "firings of the same trigger.  Default 24."
                        ),
                    },
                },
                "required": ["action"],
            },
            annotations=types.ToolAnnotations(
                title="Manage Triggers",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_session_end",
            description=(
                "Call at the end of a coding or chat session to capture key outcomes to persistent memory. "
                "Writes a session summary to today's daily notes and appends status to relevant project files. "
                "Provide a brief summary of what was accomplished, decisions made, and any blockers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What was accomplished in this session (1-3 sentences)",
                    },
                    "decisions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key decisions made (optional)",
                    },
                    "blockers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Open blockers or next steps (optional)",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project slug to append status to (e.g., 'palinode'). Auto-detected if omitted.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source surface that created this memory (e.g., 'claude-code', 'cursor', 'api'). Auto-detected if omitted.",
                    },
                    "push": {
                        "type": "boolean",
                        # push=true lets wrap-style callers commit and ship the
                        # session note in one call; omitted uses server config.
                        "description": "Push the memory repo after committing the session note.",
                    },
                },
                "required": ["summary"],
            },
            annotations=types.ToolAnnotations(
                title="End Session",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_lint",
            description=(
                "Scan memory for health issues: orphaned files, stale active files (>90 days), "
                "missing frontmatter fields, and potential contradictions. Returns a report without modifying files."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
            annotations=types.ToolAnnotations(
                title="Lint Memory",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_review",
            description=(
                "Advisory project-memory review. Composes the deterministic health "
                "signals (stale files, long-unresolved open questions, open contradictions, "
                "orphans, missing descriptions, wiki drift) scoped to a project, and proposes "
                "corrective ops (PROPOSE_ARCHIVE/UPDATE/SUPERSEDE). Read-only — proposes, never "
                "applies. Omit `project` to review the whole store."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project slug (e.g. 'palinode') or typed ref ('project/palinode'). Omit to review the whole store.",
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Review Project Memory",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_dedup_suggest",
            description=(
                "Given draft memory content the LLM is about to save, return the top-K existing "
                "memory files whose embeddings are semantically near it. Use BEFORE writing a new "
                "memory to decide 'create new' vs 'update existing'. Each result includes a "
                "`strong_dup` flag — when true (similarity ≥ 0.90), the existing file is a "
                "near-paraphrase and the LLM should usually update rather than create. "
                "Preprocessing strips wikilink syntax and the auto-generated `## See also` footer "
                "so notes linking the same entities don't false-positive as duplicates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The draft memory body about to be saved (markdown, with or without frontmatter).",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to surface (0.0–1.0). Default 0.80.",
                        "default": 0.80,
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of candidate files to return. Default 5.",
                        "default": 5,
                    },
                },
                "required": ["content"],
            },
            annotations=types.ToolAnnotations(
                title="Dedup Suggest",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_orphan_repair",
            description=(
                "Given a `[[wikilink]]` whose target file does not exist, return existing memory "
                "files semantically near the link target text. Use during wiki-maintenance passes "
                "to either propose a redirect (rename the link to point at an existing file) or "
                "to create the missing target file with informed context about its semantic "
                "neighbours. Accepts either `[[name]]` or bare `name`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "broken_link": {
                        "type": "string",
                        "description": "The wikilink text (e.g. '[[alice-meeting]]') or bare target slug.",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to surface (0.0–1.0). Default 0.65 — looser than dedup_suggest because the LLM picks from a wider slate.",
                        "default": 0.65,
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of candidate files to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": ["broken_link"],
            },
            annotations=types.ToolAnnotations(
                title="Orphan Repair",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_cluster_neighbors",
            description=(
                "Given a memory file path, find the top-K semantically related files that are NOT "
                "currently linked to or from it (no existing [[wikilink]] in either direction). "
                "Use during wiki-maintenance passes to surface implicit relationships that no "
                "wikilink yet captures — the LLM can then propose new cross-links. "
                "Preprocessing strips wikilink syntax and the auto-generated `## See also` footer "
                "so notes linking the same entities don't false-positive as related."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'decisions/palinode-arch.md') to find unlinked semantic neighbours for.",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to surface (0.0–1.0). Default 0.70.",
                        "default": 0.70,
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of candidate files to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="Cluster Neighbors",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_topic_coverage",
            description=(
                "Given a topic phrase (not a file), check whether any wiki page already covers it. "
                "Returns {covered: bool, best_match: str | null, similarity: float}. "
                "Use BEFORE ingesting new content to ask 'is this already covered?'. "
                "Different framing from palinode_dedup_suggest: takes a short topic phrase rather "
                "than full draft content, and answers the binary 'already covered?' question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic phrase to check coverage for (e.g. 'machine learning deployment').",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Minimum cosine similarity to count as 'covered' (0.0–1.0). Default 0.78.",
                        "default": 0.78,
                    },
                },
                "required": ["query"],
            },
            annotations=types.ToolAnnotations(
                title="Topic Coverage",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_doctor",
            description=(
                "Fast palinode health check (<500ms). "
                "Skips network probes and canary writes. "
                "Checks path integrity, config consistency, and env-var drift. "
                "Use this first; call palinode_doctor_deep when results are unclear."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title="Doctor (fast)",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_doctor_deep",
            description=(
                "Full palinode health check including network probes and canary write tests. "
                "Takes 10-15s. Use when palinode_doctor reports unclear results or you need "
                "to verify the API, watcher, and service connectivity."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(
                title="Doctor (deep)",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_prompt",
            description=(
                "List, read, or activate versioned LLM prompts stored as memory files in the prompts/ directory. "
                "Use 'list' to browse available prompts, 'read' to view a specific prompt's content, "
                "or 'activate' to set a prompt version as active (deactivates others of the same task)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action to perform: 'list', 'read', or 'activate'",
                        "enum": ["list", "read", "activate"],
                        "default": "list",
                    },
                    "name": {
                        "type": "string",
                        "description": "Prompt name (required for 'read' and 'activate')",
                    },
                    "task": {
                        "type": "string",
                        "description": "For 'list': filter by task type",
                        "enum": ["compaction", "extraction", "update", "classification", "nightly-consolidation"],
                    },
                },
                "required": ["action"],
            },
            annotations=types.ToolAnnotations(
                title="Manage Prompts",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
        types.Tool(
            name="palinode_depends",
            description=(
                "Return the dependency tree for a milestone or task slug, or list all unblocked items. "
                "Reads depends_on / blocks / parallel_with frontmatter from ProjectSnapshot files. "
                "Set unblocked=true to answer 'what can I work on right now?' across all slugs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": (
                            "Milestone or task slug to inspect (e.g. 'milestone/M1'). "
                            "Required unless unblocked=true."
                        ),
                    },
                    "unblocked": {
                        "type": "boolean",
                        "description": (
                            "If true, return the list of all slugs whose every depends_on is done "
                            "(ignores slug). Default false."
                        ),
                        "default": False,
                    },
                },
            },
            annotations=types.ToolAnnotations(
                title="Dependency Tree",
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        ),
    ]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = _all_tools()
    if _resolve_tool_surface() == "core":
        return [tool for tool in tools if tool.name in CORE_TOOL_NAMES]
    return tools


# ── Tool handlers ─────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    start_time = time.monotonic()
    result = await _dispatch_tool(name, arguments)
    duration_ms = (time.monotonic() - start_time) * 1000

    # Detect error responses (the dispatch handler returns error text rather than raising)
    first_text = result[0].text if result else ""
    is_error = first_text.startswith(("Error", "API Error", "Search failed", "Save failed",
                                      "Ingest failed", "Push failed", "Consolidation failed",
                                      "Session-end failed", "Lint failed",
                                      "Doctor failed", "Doctor (deep) failed",
                                      "Timeout:"))
    _audit.log_call(
        name, arguments, duration_ms,
        status="error" if is_error else "success",
        error=first_text if is_error else None,
    )
    return result


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        # ── list ──────────────────────────────────────────────────────────
        if name == "palinode_list":
            params: dict[str, Any] = {}
            if arguments.get("category"):
                params["category"] = arguments["category"]
            if arguments.get("core_only"):
                params["core_only"] = "true"

            resp = await _get("/list", params=params)
            if resp.status_code != 200:
                return _text(f"API Error: {resp.text}")
            data = resp.json()
            if not data:
                return _text("No files found.")
            parts = []
            for f in data:
                c_tag = " [core]" if f.get("core") else ""
                parts.append(f"{f['file']} — {f.get('summary', '')}{c_tag}")
            return _text("\n".join(parts))

        # ── read ──────────────────────────────────────────────────────────
        elif name == "palinode_read":
            # ADR-010: honor caller's `meta` request. We always fetch
            # with meta=true (cheap; parser already runs) but only render
            # frontmatter when the caller asked for it.
            include_meta = bool(arguments.get("meta", False))
            resp = await _get(
                "/read",
                params={"file_path": arguments["file_path"], "meta": "true"},
            )
            if resp.status_code != 200:
                return _text(f"Error reading file: {resp.text}")
            data = resp.json()
            content = data.get("content", "")
            if include_meta:
                fm = data.get("frontmatter") or {}
                # Render as YAML-ish frontmatter + body so downstream consumers
                # can re-parse if they want.  Keep it simple: the file already
                # has the same structure on disk.
                fm_lines = "\n".join(f"{k}: {v!r}" for k, v in fm.items())
                return _text(f"---\n{fm_lines}\n---\n{content}")
            return _text(content)

        # ── search ────────────────────────────────────────────────────────
        elif name == "palinode_search":
            body: dict[str, Any] = {"query": arguments["query"]}
            if arguments.get("category"):
                body["category"] = arguments["category"]
            if arguments.get("limit"):
                body["limit"] = int(arguments["limit"])
            if arguments.get("date_after"):
                body["date_after"] = arguments["date_after"]
            if arguments.get("date_before"):
                body["date_before"] = arguments["date_before"]
            if arguments.get("include_daily"):
                body["include_daily"] = True
            if arguments.get("include_telemetry"):
                body["include_telemetry"] = True
            if arguments.get("since_days") is not None:
                body["since_days"] = int(arguments["since_days"])
            if arguments.get("types"):
                body["types"] = _coerce_str_array(arguments["types"])
            if arguments.get("min_priority") is not None:
                body["min_priority"] = int(arguments["min_priority"])
            # ADR-010: caller-supplied threshold wins; otherwise use
            # the MCP-tuned default (typically tighter than the API default
            # to keep auto-context noise low).
            if arguments.get("threshold") is not None:
                body["threshold"] = float(arguments["threshold"])
            else:
                body["threshold"] = config.search.mcp_threshold
            # ADR-008: ambient context boost
            context = _resolve_context()
            if context:
                body["context"] = context

            resp = await _post("/search", json=body, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Search failed: {resp.text}")
            # `full` is purely a rendering choice — the API always
            # populates `snippet` and preserves `content`, so the MCP picks
            # which to render without an extra round-trip.
            return _text(_format_results(resp.json(), full=bool(arguments.get("full"))))

        # ── save ──────────────────────────────────────────────────────────
        elif name == "palinode_save":
            # Resolve memory type from either explicit `type` or `ps=true`
            # shortcut (parity with CLI `palinode save --ps`).
            try:
                resolved_type = _resolve_save_type(
                    arguments.get("type"), arguments.get("ps")
                )
            except ValueError as e:
                return _text(f"Error: {e}")

            body: dict[str, Any] = {
                "content": arguments["content"],
                "type": resolved_type,
            }
            # ADR-010: only set body source when caller explicitly
            # supplied one.  Otherwise the X-Palinode-Source header (set on
            # every MCP request) carries attribution to the API.
            if arguments.get("source"):
                body["source"] = arguments["source"]
            if arguments.get("slug"):
                body["slug"] = arguments["slug"]
            if arguments.get("core") is not None:
                body["core"] = arguments["core"]
            if arguments.get("entities"):
                body["entities"] = _coerce_str_array(arguments["entities"])
            if arguments.get("project"):
                body["project"] = arguments["project"]
            if arguments.get("title"):
                body["title"] = arguments["title"]
            if arguments.get("metadata") is not None:
                body["metadata"] = arguments["metadata"]
            if arguments.get("confidence") is not None:
                body["confidence"] = float(arguments["confidence"])
            if arguments.get("priority") is not None:
                body["priority"] = int(arguments["priority"])
            # ADR-018: epistemic marker. Forwarded verbatim; the API
            # validates against VALID_EPISTEMICS and 400s on an unknown value
            # (surfaced below as the standard "Save failed" message).
            if arguments.get("epistemic") is not None:
                body["epistemic"] = arguments["epistemic"]
            if arguments.get("external_refs") is not None:
                body["external_refs"] = arguments["external_refs"]
            # ADR-015 §2.1: write-semantics axis. Forwarded verbatim; the API
            # validates against VALID_UPDATE_POLICIES and 400s on an unknown
            # value (surfaced below as the standard "Save failed" message).
            if arguments.get("update_policy") is not None:
                body["update_policy"] = arguments["update_policy"]
            # source-citation anchors. Forwarded verbatim; the API
            # validates each entry and computes/verifies quote_hash, 400ing on a
            # malformed or inconsistent anchor (surfaced as "Save failed").
            if arguments.get("sources") is not None:
                body["sources"] = arguments["sources"]
            # (G4): typed relationship links. Forwarded verbatim; the API
            # validates each ref and 400s on a malformed one (surfaced below as
            # the standard "Save failed" message).
            if arguments.get("contradicts") is not None:
                body["contradicts"] = arguments["contradicts"]
            if arguments.get("backed_by") is not None:
                body["backed_by"] = arguments["backed_by"]

            resp = await _post("/save", json=body)
            if resp.status_code != 200:
                return _text(f"Save failed: {resp.text}")
            data = resp.json()
            file_path = data.get("file_path", "")
            # Show relative path
            rel = file_path.rsplit("/palinode/", 1)[-1] if "/palinode/" in file_path else file_path
            # Surface per-index health signals from if either index
            # write failed — these are warnings, not save failures.
            warnings: list[str] = []
            if not data.get("indexed_vec", True):
                warnings.append("vec index write failed (chunk absent from vector search)")
            if not data.get("indexed_fts", True):
                warnings.append("FTS5 sync failed (periodic rebuild will recover)")
            if not data.get("git_committed", True):
                warnings.append("git auto-commit failed (file on disk, not versioned)")
            if warnings:
                return _text(f"Saved to {rel} [warnings: {'; '.join(warnings)}]")
            return _text(f"Saved to {rel}")

        # ── ingest ────────────────────────────────────────────────────────
        elif name == "palinode_ingest":
            url = arguments["url"]
            name_arg = arguments.get("name", url.split("/")[-1][:40])

            resp = await _post("/ingest-url", json={"url": url, "name": name_arg}, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Ingest failed: {resp.text}")
            data = resp.json()
            if data.get("file_path"):
                fp = data["file_path"]
                rel = fp.rsplit("/palinode/", 1)[-1] if "/palinode/" in fp else fp
                return _text(f"Ingested → {rel}")
            return _text("No content extracted from URL.")

        # ── history ───────────────────────────────────────────────────────
        elif name == "palinode_history":
            file_path = arguments["file_path"]
            limit = int(arguments.get("limit", 20))
            detail = arguments.get("detail", "summary")
            if detail not in ("summary", "full"):
                return _text("Error: detail must be 'summary' or 'full'")
            resp = await _get(f"/history/{file_path}", params={"limit": str(limit), "detail": detail})
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            data = resp.json()
            if not data.get("history"):
                return _text("No history found.")
            lines = []
            for c in data["history"]:
                line = f"{c['hash']} | {c['date'][:10]} | {c['message']}"
                if c.get("stats"):
                    line += f"\n  {c['stats']}"
                if detail == "full" and c.get("diff"):
                    line += f"\n{c['diff']}"
                lines.append(line)
            return _text("\n\n---\n\n".join(lines) if detail == "full" else "\n".join(lines))

        # ── timeline (deprecated alias for history detail=full) ───────────
        elif name == "palinode_timeline":
            logger.warning("palinode_timeline is deprecated — use palinode_history with detail='full'")
            file_path = arguments["file_path"]
            limit = int(arguments.get("limit", 20))
            resp = await _get(f"/history/{file_path}", params={"limit": str(limit), "detail": "full"})
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            data = resp.json()
            if not data.get("history"):
                return _text("No history found.")
            lines = []
            for c in data["history"]:
                line = f"{c['hash']} | {c['date'][:10]} | {c['message']}"
                if c.get("stats"):
                    line += f"\n  {c['stats']}"
                if c.get("diff"):
                    line += f"\n{c['diff']}"
                lines.append(line)
            deprecation_note = "[DEPRECATED] palinode_timeline is deprecated — use palinode_history with detail='full' instead.\n\n"
            return _text(deprecation_note + "\n\n---\n\n".join(lines))

        # ── entities ──────────────────────────────────────────────────────
        elif name == "palinode_entities":
            entity_ref = arguments.get("entity_ref")
            if entity_ref:
                resp = await _get(f"/entities/{entity_ref}")
            else:
                resp = await _get("/entities")
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            return _text(json.dumps(resp.json(), indent=2))

        # ── consolidate ───────────────────────────────────────────────────
        elif name == "palinode_consolidate":
            body: dict[str, Any] = {}
            if arguments.get("dry_run"):
                body["dry_run"] = True
            if arguments.get("nightly"):
                body["nightly"] = True
            resp = await _post("/consolidate", json=body, timeout=300.0)
            if resp.status_code != 200:
                return _text(f"Consolidation failed: {resp.text}")
            return _text(json.dumps(resp.json(), indent=2))

        # ── archive-expired ────────────────────────────────────────────────
        elif name == "palinode_archive_expired":
            body = {}
            if arguments.get("dry_run"):
                body["dry_run"] = True
            resp = await _post("/archive-expired", json=body, timeout=120.0)
            if resp.status_code != 200:
                return _text(f"Archive-expired sweep failed: {resp.text}")
            return _text(json.dumps(resp.json(), indent=2))

        # ── status ────────────────────────────────────────────────────────
        elif name == "palinode_status":
            resp = await _get("/status")
            if resp.status_code != 200:
                return _text(f"API unreachable: {resp.text}")
            s = resp.json()
            lines = [
                "Palinode Status",
                f"  Version:        {s.get('version', '?')}",
                f"  Files indexed:  {s.get('total_files', '?')}",
                f"  Chunks indexed: {s.get('total_chunks', '?')}",
                f"  Hybrid search:  {'✅ enabled' if s.get('hybrid_search') else '❌ disabled'}",
                f"  FTS5 chunks:    {s.get('fts_chunks', '?')}",
                f"  Entities:       {s.get('total_entities', '?')}",
                f"  Ollama (embed): {'✅ reachable' if s.get('ollama_reachable') else '❌ unreachable'}",
                f"  Git commits 7d: {s.get('git_commits_7d', '?')}",
                f"  Unpushed:       {s.get('unpushed_commits', '?')}",
                f"  API:            {_api_url('')}",
            ]
            return _text("\n".join(lines))

        # ── diff ──────────────────────────────────────────────────────────
        elif name == "palinode_diff":
            days = int(arguments.get("days", 7))
            params = {"days": str(days)}
            paths = _coerce_str_array(arguments.get("paths"))
            if paths:
                params["paths"] = ",".join(paths)
            resp = await _get("/diff", params=params)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            return _text(resp.json().get("diff", "No changes."))

        # ── blame ─────────────────────────────────────────────────────────
        elif name == "palinode_blame":
            # ADR-010: prefer canonical `file_path`; accept legacy
            # `file` for one release.
            file_path = arguments.get("file_path") or arguments.get("file")
            if not file_path:
                return _text("Error: file_path is required")
            params: dict[str, str] = {}
            if arguments.get("search"):
                params["search"] = arguments["search"]
            resp = await _get(f"/blame/{file_path}", params=params)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            return _text(resp.json().get("blame", "No blame data."))

        # ── rollback ──────────────────────────────────────────────────────
        elif name == "palinode_rollback":
            # ADR-010: prefer canonical `file_path`; accept legacy
            # `file` for one release.
            file_path = arguments.get("file_path") or arguments.get("file")
            if not file_path:
                return _text("Error: file_path is required")
            params: dict[str, str] = {"file_path": file_path}
            if arguments.get("commit"):
                params["commit"] = arguments["commit"]
            params["dry_run"] = str(arguments.get("dry_run", True)).lower()
            resp = await _post_params("/rollback", params=params)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            return _text(resp.json().get("result", "Done."))

        # ── push ──────────────────────────────────────────────────────────
        elif name == "palinode_push":
            resp = await _post("/push")
            if resp.status_code != 200:
                return _text(f"Push failed: {resp.text}")
            return _text(resp.json().get("result", "Pushed."))

        # ── trigger ───────────────────────────────────────────────────────
        elif name == "palinode_trigger":
            action = arguments.get("action", "create")
            if action == "list":
                resp = await _get("/triggers")
                if resp.status_code != 200:
                    return _text(f"Error: {resp.text}")
                return _text(json.dumps(resp.json(), indent=2))

            elif action == "delete":
                tid = arguments.get("trigger_id")
                if not tid:
                    return _text("Error: trigger_id required for delete")
                resp = await _delete(f"/triggers/{tid}")
                if resp.status_code != 200:
                    return _text(f"Error: {resp.text}")
                return _text(f"Deleted trigger {tid}")

            else:  # create
                desc = arguments.get("description")
                mem = arguments.get("memory_file")
                if not desc or not mem:
                    return _text("Error: description and memory_file required for create")
                body = {
                    "description": desc,
                    "memory_file": mem,
                }
                if arguments.get("trigger_id"):
                    body["trigger_id"] = arguments["trigger_id"]
                if arguments.get("threshold") is not None:
                    body["threshold"] = arguments["threshold"]
                if arguments.get("cooldown_hours") is not None:
                    body["cooldown_hours"] = arguments["cooldown_hours"]
                resp = await _post("/triggers", json=body)
                if resp.status_code != 200:
                    return _text(f"Error: {resp.text}")
                data = resp.json()
                return _text(f"Created trigger {data.get('id', '?')} for {mem}")

        # ── session_end ───────────────────────────────────────────────────
        elif name == "palinode_session_end":
            body: dict[str, Any] = {"summary": arguments.get("summary", "")}
            if arguments.get("decisions"):
                body["decisions"] = _coerce_str_array(arguments["decisions"])
            if arguments.get("blockers"):
                body["blockers"] = _coerce_str_array(arguments["blockers"])
            if arguments.get("project"):
                body["project"] = arguments["project"]
            if arguments.get("source"):
                body["source"] = arguments["source"]
            if arguments.get("push") is not None:
                body["push"] = bool(arguments["push"])

            resp = await _post("/session-end", json=body, timeout=_SESSION_END_TIMEOUT)
            if resp.status_code != 200:
                return _text(f"Session-end failed: {resp.text}")
            data = resp.json()
            status_msg = f" + status → {data['status_file']}" if data.get("status_file") else ""
            # Report push outcome so the wrap flow can say "pushed" vs "pending"
            # without a second tool call.
            if body.get("push"):
                push_msg = " + pushed" if data.get("pushed") else " (push pending — commit local, push did not succeed)"
            else:
                push_msg = ""
            return _text(f"Session captured → {data['daily_file']}{status_msg}{push_msg}\n\n{data.get('entry', '')}")

        # ── dedup_suggest ─────────────────────────────────────────────────
        elif name == "palinode_dedup_suggest":
            body: dict[str, Any] = {"content": arguments.get("content", "")}
            if arguments.get("min_similarity") is not None:
                body["min_similarity"] = float(arguments["min_similarity"])
            if arguments.get("top_k") is not None:
                body["top_k"] = int(arguments["top_k"])
            resp = await _post("/dedup-suggest", json=body, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            data = resp.json()
            if not data:
                return _text("No semantically similar files found.")
            lines = []
            for r in data:
                fp = r.get("file_path", "")
                rel = fp.rsplit("/palinode/", 1)[-1] if "/palinode/" in fp else fp
                tag = " ⚠ STRONG-DUP (likely should update, not create)" if r.get("strong_dup") else ""
                pct = int(r.get("similarity", 0) * 100)
                snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:160]
                lines.append(f"[{rel}] ({pct}% similar){tag}\n  {snippet}")
            return _text("\n\n".join(lines))

        # ── orphan_repair ─────────────────────────────────────────────────
        elif name == "palinode_orphan_repair":
            body: dict[str, Any] = {"broken_link": arguments.get("broken_link", "")}
            if arguments.get("min_similarity") is not None:
                body["min_similarity"] = float(arguments["min_similarity"])
            if arguments.get("top_k") is not None:
                body["top_k"] = int(arguments["top_k"])
            resp = await _post("/orphan-repair", json=body, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            data = resp.json()
            if not data:
                return _text("No semantically related files found.")
            lines = []
            for r in data:
                fp = r.get("file_path", "")
                rel = fp.rsplit("/palinode/", 1)[-1] if "/palinode/" in fp else fp
                pct = int(r.get("similarity", 0) * 100)
                snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:160]
                lines.append(f"[{rel}] ({pct}% similar)\n  {snippet}")
            return _text("\n\n".join(lines))

        # ── cluster_neighbors ─────────────────────────────────────────────
        elif name == "palinode_cluster_neighbors":
            body: dict[str, Any] = {"file_path": arguments.get("file_path", "")}
            if arguments.get("min_similarity") is not None:
                body["min_similarity"] = float(arguments["min_similarity"])
            if arguments.get("top_k") is not None:
                body["top_k"] = int(arguments["top_k"])
            resp = await _post("/cluster-neighbors", json=body, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            data = resp.json()
            if not data:
                return _text("No unlinked semantic neighbours found above threshold.")
            lines = []
            for r in data:
                fp = r.get("file_path", "")
                rel = fp.rsplit("/palinode/", 1)[-1] if "/palinode/" in fp else fp
                pct = int(r.get("similarity", 0) * 100)
                snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:160]
                lines.append(f"[{rel}] ({pct}% similar)\n  {snippet}")
            return _text("\n\n".join(lines))

        # ── topic_coverage ────────────────────────────────────────────────
        elif name == "palinode_topic_coverage":
            body: dict[str, Any] = {"query": arguments.get("query", "")}
            if arguments.get("min_similarity") is not None:
                body["min_similarity"] = float(arguments["min_similarity"])
            resp = await _post("/topic-coverage", json=body, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            data = resp.json()
            covered = data.get("covered", False)
            best = data.get("best_match")
            sim = data.get("similarity", 0.0)
            if covered and best:
                fp = best.rsplit("/palinode/", 1)[-1] if "/palinode/" in best else best
                pct = int(sim * 100)
                return _text(f"COVERED — {fp} ({pct}% similar). Consider updating the existing page.")
            return _text(f"NOT COVERED — no existing page matches above threshold (best similarity: {sim:.2f}). Safe to create new.")

        # ── doctor ────────────────────────────────────────────────────────
        elif name == "palinode_doctor":
            resp = await _get("/doctor", params={"fast": "true"}, timeout=10.0)
            if resp.status_code != 200:
                return _text(f"Doctor failed: {resp.text}")
            data = resp.json()
            return _text(json.dumps(data, indent=2))

        elif name == "palinode_doctor_deep":
            resp = await _get("/doctor", params={"canary": "true"}, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Doctor (deep) failed: {resp.text}")
            data = resp.json()
            return _text(json.dumps(data, indent=2))

        # ── lint ──────────────────────────────────────────────────────────
        elif name == "palinode_lint":
            resp = await _post("/lint", timeout=120.0)
            if resp.status_code != 200:
                return _text(f"Lint failed: {resp.text}")
            return _text(json.dumps(resp.json(), indent=2))

        # ── review ───────────────────────────────────────────────────
        elif name == "palinode_review":
            body: dict[str, Any] = {}
            if arguments.get("project"):
                body["project"] = arguments["project"]
            resp = await _post("/review", json=body, timeout=120.0)
            if resp.status_code != 200:
                return _text(f"Review failed: {resp.text}")
            return _text(json.dumps(resp.json(), indent=2))

        # ── prompt ────────────────────────────────────────────────────────
        elif name == "palinode_prompt":
            action = arguments.get("action", "list")

            if action == "list":
                params: dict[str, str] = {}
                if arguments.get("task"):
                    params["task"] = arguments["task"]
                resp = await _get("/prompts", params=params)
                if resp.status_code != 200:
                    return _text(f"Error listing prompts: {resp.text}")
                data = resp.json()
                if not data:
                    return _text("No prompts found.")
                lines = []
                for p in data:
                    active_tag = " [active]" if p.get("active") else ""
                    lines.append(
                        f"{p['name']} (task={p.get('task','')}, "
                        f"model={p.get('model','')}, "
                        f"v{p.get('version','')}){active_tag}"
                    )
                return _text("\n".join(lines))

            elif action == "read":
                pname = arguments.get("name")
                if not pname:
                    return _text("Error: name required for 'read'")
                resp = await _get(f"/prompts/{pname}")
                if resp.status_code == 404:
                    return _text(f"Prompt '{pname}' not found.")
                if resp.status_code != 200:
                    return _text(f"Error reading prompt: {resp.text}")
                data = resp.json()
                header = (
                    f"# {data['name']} (task={data.get('task','')}, "
                    f"model={data.get('model','')}, v{data.get('version','')})"
                )
                active_note = " [ACTIVE]" if data.get("active") else ""
                return _text(f"{header}{active_note}\n\n{data.get('content','')}")

            elif action == "activate":
                pname = arguments.get("name")
                if not pname:
                    return _text("Error: name required for 'activate'")
                resp = await _post(f"/prompts/{pname}/activate")
                if resp.status_code == 404:
                    return _text(f"Prompt '{pname}' not found.")
                if resp.status_code != 200:
                    return _text(f"Error activating prompt: {resp.text}")
                data = resp.json()
                return _text(f"Activated '{data['activated']}' for task={data['task']}")

            else:
                return _text(f"Unknown action: {action}. Use 'list', 'read', or 'activate'.")

        # ── depends ───────────────────────────────────────────────────────
        elif name == "palinode_depends":
            if arguments.get("unblocked"):
                resp = await _get("/depends/_unblocked")
                if resp.status_code != 200:
                    return _text(f"API Error: {resp.text}")
                items = resp.json()
                if not items:
                    return _text("No unblocked items found.")
                lines = [
                    f"{it['slug']}" + (f" (status={it['status']})" if it.get("status") else "")
                    for it in items
                ]
                return _text("Unblocked items:\n" + "\n".join(lines))
            else:
                slug = arguments.get("slug", "").strip()
                if not slug:
                    return _text("Error: 'slug' is required unless unblocked=true")
                resp = await _get(f"/depends/{slug}")
                if resp.status_code != 200:
                    return _text(f"API Error: {resp.text}")
                import json as _json
                return _text(_json.dumps(resp.json(), indent=2))

        else:
            return _text(f"Unknown tool: {name}")

    except httpx.ConnectError:
        return _text(f"Error: Cannot reach Palinode API at {_api_url('')}. Is palinode-api running?")
    except httpx.TimeoutException:
        return _text(_timeout_message(name))
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return _text(f"Error: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def async_main() -> None:
    """Async boot sequence — start MCP server over stdio."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Synchronous entry point for setuptools console_scripts (stdio transport)."""
    asyncio.run(async_main())


def _build_mcp_http_app(token: str | None):
    """Build and return the Starlette MCP HTTP application.

    Extracted for testability — ``main_http`` builds the app then hands it
    to uvicorn; tests drive it directly via ``TestClient``.

    Parameters
    ----------
    token:
        Bearer token to protect the server, or ``None`` for no auth.
    """
    import contextlib
    from collections.abc import AsyncIterator

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from palinode.core.auth import BearerAuthMiddleware, MCP_EXEMPT_PATHS
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    session_manager = StreamableHTTPSessionManager(app=server)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    async def healthz(request):
        """Health check — returns 200 if the session manager is running.

        Clients can poll this for connection-liveness detection without
        initiating a full MCP session.
        """
        return JSONResponse({
            "status": "ok",
            "service": "palinode-mcp-http",
            "transport": "streamable-http",
            "api_backend": _api_url(""),
        })

    starlette_app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Mount("/mcp", app=session_manager.handle_request),
        ],
    )
    # Registered before request routing so unauthenticated callers never
    # reach the MCP session handler. The middleware is a no-op when token
    # is None. /healthz is exempt so uptime probes don't need the token.
    starlette_app.add_middleware(
        BearerAuthMiddleware,
        token=token,
        exempt_paths=MCP_EXEMPT_PATHS,
    )
    return starlette_app


def main_http() -> None:
    """Entry point for Streamable HTTP transport — palinode-mcp-http.

    Exposes the MCP server over Streamable HTTP so remote clients (Claude Code,
    Claude Desktop, Cursor, Zed, etc.) can connect via URL without running a
    local process.

    Env vars:
      PALINODE_MCP_HTTP_HOST  — bind address (default: 0.0.0.0)
      PALINODE_MCP_HTTP_PORT  — bind port (default: 6341)
      PALINODE_MCP_LOG_LEVEL  — uvicorn log level (default: info)
      PALINODE_MCP_BIND_INTENT — set to ``public`` to confirm intentional
                                 non-loopback bind; requires PALINODE_API_TOKEN.

    Legacy env var aliases (still honored for existing deployments):
      PALINODE_MCP_SSE_HOST, PALINODE_MCP_SSE_PORT

    Client config (any IDE):
      { "url": "http://your-server:6341/mcp/" }
    """
    import os

    import uvicorn
    from palinode.core.auth import load_api_token, validate_auth_config

    # Resolve token and run the public-bind gate INSIDE this entry point,
    # not at module level. palinode/mcp.py is imported for the stdio
    # transport too — a module-level gate would fire on every
    # ``import palinode.mcp``, killing stdio sessions when
    # PALINODE_MCP_BIND_INTENT=public is set.
    token = load_api_token()
    mcp_bind_intent_public = (
        os.environ.get("PALINODE_MCP_BIND_INTENT", "").lower() == "public"
    )
    validate_auth_config(
        mcp_bind_intent_public,
        token,
        bind_intent_var="PALINODE_MCP_BIND_INTENT",
    )

    starlette_app = _build_mcp_http_app(token)

    # B104 rationale - opt-in MCP HTTP server fallback; deployers must set
    # PALINODE_MCP_HTTP_HOST for a restricted bind (e.g., 127.0.0.1).
    host = (
        os.environ.get("PALINODE_MCP_HTTP_HOST")
        or os.environ.get("PALINODE_MCP_SSE_HOST")  # legacy alias
        or "0.0.0.0"  # nosec B104
    )
    port = int(
        os.environ.get("PALINODE_MCP_HTTP_PORT")
        or os.environ.get("PALINODE_MCP_SSE_PORT")  # legacy alias
        or "6341"
    )
    log_level = os.environ.get("PALINODE_MCP_LOG_LEVEL", "info")

    # Parity with the API server's 0.0.0.0 exposure warning: the MCP
    # HTTP transport defaults to binding 0.0.0.0, which serves the full tool
    # surface (save/search/read/...) on every interface. The hard refusal —
    # PALINODE_MCP_BIND_INTENT=public with no token — already fired in
    # validate_auth_config above. The remaining silent-exposure case is the
    # DEFAULT bind: 0.0.0.0, no token, no explicit public intent. Warn loudly
    # so an unauthenticated network bind is never silent (unlike the API
    # server, the MCP startup banner previously only said "Bearer auth:
    # disabled" without naming the exposure).
    if host == "0.0.0.0" and not mcp_bind_intent_public:  # nosec B104
        if token is None:
            logger.warning(
                "MCP HTTP binding to 0.0.0.0 — accessible from any network. "
                "No authentication is configured. Set "
                "PALINODE_MCP_HTTP_HOST=127.0.0.1 for local-only access. Set "
                "PALINODE_MCP_BIND_INTENT=public (with PALINODE_API_TOKEN) to "
                "suppress this warning for intentional network-exposed "
                "deployments (e.g., Tailscale)."
            )
        else:
            logger.info(
                "MCP HTTP binding to 0.0.0.0 with PALINODE_API_TOKEN configured "
                "— bearer auth required."
            )

    print(f"Palinode MCP (Streamable HTTP) listening on http://{host}:{port}/mcp/")
    print(f"  Health check: http://{host}:{port}/healthz")
    print(f"  API backend:  {_api_url('')}")
    if token:
        print("  Bearer auth: enabled (PALINODE_API_TOKEN)")
    else:
        print("  Bearer auth: disabled (no PALINODE_API_TOKEN)")
    uvicorn.run(starlette_app, host=host, port=port, log_level=log_level)


# Legacy alias for existing setuptools console_scripts (palinode-mcp-sse)
main_sse = main_http


if __name__ == "__main__":
    main()
