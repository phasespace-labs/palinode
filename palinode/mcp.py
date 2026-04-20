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
import logging
import time
from typing import Any

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

import os

from palinode.core.config import config
from palinode.core.audit import AuditLogger

logger = logging.getLogger("palinode.mcp")
logging.basicConfig(level=logging.WARNING)  # quiet — don't pollute stdio

server = Server("palinode")
_audit = AuditLogger(config.memory_dir, config.audit)


def _resolve_context() -> list[str] | None:
    """Resolve ambient project context from environment.

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


async def _get(path: str, params: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP GET to the API server."""
    async with httpx.AsyncClient() as client:
        return await client.get(_api_url(path), params=params, timeout=timeout)


async def _post(path: str, json: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP POST to the API server."""
    async with httpx.AsyncClient() as client:
        return await client.post(_api_url(path), json=json, timeout=timeout)


async def _post_params(path: str, params: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP POST with query params (no JSON body) to the API server."""
    async with httpx.AsyncClient() as client:
        return await client.post(_api_url(path), params=params, timeout=timeout)


async def _delete(path: str, timeout: float = 30.0) -> httpx.Response:
    """Async HTTP DELETE to the API server."""
    async with httpx.AsyncClient() as client:
        return await client.delete(_api_url(path), timeout=timeout)


def _text(content: str) -> list[types.TextContent]:
    """Shorthand for returning a single text result."""
    return [types.TextContent(type="text", text=content)]


def _format_results(results: list[dict[str, Any]]) -> str:
    """Format search results as clean text — minimal context burn."""
    if not results:
        return "No results found."
    parts = []
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
        parts.append(f"[{rel}] ({score_pct}% match){fresh_label}\n{r.get('content', '').strip()}")
    return "\n\n---\n\n".join(parts)


# ── Tool definitions ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
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
                        "description": "Filter by category: person, project, decision, insight, research",
                        "enum": ["person", "project", "decision", "insight", "research"],
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
                "or project updates worth remembering across sessions."
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
                        "description": "Memory type",
                        "enum": ["PersonMemory", "Decision", "ProjectSnapshot", "Insight", "ResearchRef", "ActionItem"],
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
                    "source": {
                        "type": "string",
                        "description": "Source surface that created this memory (e.g., 'claude-code', 'cursor', 'api'). Auto-detected if omitted.",
                    },
                },
                "required": ["content", "type"],
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
                "and includes diff stats per commit."
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
                },
                "required": ["file_path"],
            },
            annotations=types.ToolAnnotations(
                title="File History",
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
            description="Run a manual knowledge consolidation pass.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
            annotations=types.ToolAnnotations(
                title="Run Consolidation",
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
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
                    "file": {
                        "type": "string",
                        "description": "Memory file path (e.g., 'projects/my-app.md')",
                    },
                    "search": {
                        "type": "string",
                        "description": "Optional: filter to lines containing this text",
                    },
                },
                "required": ["file"],
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
                    "file": {
                        "type": "string",
                        "description": "Memory file path to rollback",
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
                "required": ["file"],
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
                        "enum": ["compaction", "extraction", "update", "classification"],
                    },
                },
                "required": ["action"],
            },
            annotations=types.ToolAnnotations(
                title="Manage Prompts",
                readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
            ),
        ),
    ]


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
                                      "Session-end failed", "Lint failed"))
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
            resp = await _get("/read", params={"file_path": arguments["file_path"], "meta": "true"})
            if resp.status_code != 200:
                return _text(f"Error reading file: {resp.text}")
            return _text(resp.json()["content"])

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
            # Use MCP threshold, not API default
            body["threshold"] = config.search.mcp_threshold
            # ambient context boost
            context = _resolve_context()
            if context:
                body["context"] = context

            resp = await _post("/search", json=body, timeout=60.0)
            if resp.status_code != 200:
                return _text(f"Search failed: {resp.text}")
            return _text(_format_results(resp.json()))

        # ── save ──────────────────────────────────────────────────────────
        elif name == "palinode_save":
            body = {
                "content": arguments["content"],
                "type": arguments["type"],
                "source": arguments.get("source", "mcp"),
            }
            if arguments.get("slug"):
                body["slug"] = arguments["slug"]
            if arguments.get("core") is not None:
                body["core"] = arguments["core"]
            if arguments.get("entities"):
                body["entities"] = arguments["entities"]

            resp = await _post("/save", json=body)
            if resp.status_code != 200:
                return _text(f"Save failed: {resp.text}")
            data = resp.json()
            file_path = data.get("file_path", "")
            # Show relative path
            rel = file_path.rsplit("/palinode/", 1)[-1] if "/palinode/" in file_path else file_path
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
            resp = await _get(f"/history/{file_path}", params={"limit": str(limit)})
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
                lines.append(line)
            return _text("\n".join(lines))

        # ── entities ──────────────────────────────────────────────────────
        elif name == "palinode_entities":
            import json
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
            import json
            resp = await _post("/consolidate", timeout=300.0)
            if resp.status_code != 200:
                return _text(f"Consolidation failed: {resp.text}")
            return _text(json.dumps(resp.json(), indent=2))

        # ── status ────────────────────────────────────────────────────────
        elif name == "palinode_status":
            resp = await _get("/status")
            if resp.status_code != 200:
                return _text(f"API unreachable: {resp.text}")
            s = resp.json()
            lines = [
                "Palinode Status",
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
            paths = arguments.get("paths")
            if paths:
                params["paths"] = ",".join(paths)
            resp = await _get("/diff", params=params)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            return _text(resp.json().get("diff", "No changes."))

        # ── blame ─────────────────────────────────────────────────────────
        elif name == "palinode_blame":
            file_path = arguments["file"]
            params: dict[str, str] = {}
            if arguments.get("search"):
                params["search"] = arguments["search"]
            resp = await _get(f"/blame/{file_path}", params=params)
            if resp.status_code != 200:
                return _text(f"Error: {resp.text}")
            return _text(resp.json().get("blame", "No blame data."))

        # ── rollback ──────────────────────────────────────────────────────
        elif name == "palinode_rollback":
            params: dict[str, str] = {"file_path": arguments["file"]}
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
                import json
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
                resp = await _post("/triggers", json=body)
                if resp.status_code != 200:
                    return _text(f"Error: {resp.text}")
                data = resp.json()
                return _text(f"Created trigger {data.get('id', '?')} for {mem}")

        # ── session_end ───────────────────────────────────────────────────
        elif name == "palinode_session_end":
            body: dict[str, Any] = {"summary": arguments.get("summary", "")}
            if arguments.get("decisions"):
                body["decisions"] = arguments["decisions"]
            if arguments.get("blockers"):
                body["blockers"] = arguments["blockers"]
            if arguments.get("project"):
                body["project"] = arguments["project"]
            if arguments.get("source"):
                body["source"] = arguments["source"]

            resp = await _post("/session-end", json=body)
            if resp.status_code != 200:
                return _text(f"Session-end failed: {resp.text}")
            data = resp.json()
            status_msg = f" + status → {data['status_file']}" if data.get("status_file") else ""
            return _text(f"Session captured → {data['daily_file']}{status_msg}\n\n{data.get('entry', '')}")

        # ── lint ──────────────────────────────────────────────────────────
        elif name == "palinode_lint":
            import json
            resp = await _post("/lint", timeout=120.0)
            if resp.status_code != 200:
                return _text(f"Lint failed: {resp.text}")
            return _text(json.dumps(resp.json(), indent=2))

        # ── prompt ────────────────────────────────────────────────────────
        elif name == "palinode_prompt":
            import json as _json
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

        else:
            return _text(f"Unknown tool: {name}")

    except httpx.ConnectError:
        return _text(f"Error: Cannot reach Palinode API at {_api_url('')}. Is palinode-api running?")
    except httpx.TimeoutException:
        return _text(f"Error: Request to {_api_url('')} timed out.")
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


def main_http() -> None:
    """Entry point for Streamable HTTP transport — palinode-mcp-http.

    Exposes the MCP server over Streamable HTTP so remote clients (Claude Code,
    Claude Desktop, Cursor, Zed, etc.) can connect via URL without running a
    local process.

    Env vars:
      PALINODE_MCP_HTTP_HOST  — bind address (default: 0.0.0.0)
      PALINODE_MCP_HTTP_PORT  — bind port (default: 6341)
      PALINODE_MCP_LOG_LEVEL  — uvicorn log level (default: info)

    Legacy env var aliases (still honored for existing deployments):
      PALINODE_MCP_SSE_HOST, PALINODE_MCP_SSE_PORT

    Client config (any IDE):
      { "url": "http://your-server:6341/mcp/" }
    """
    import contextlib
    import os
    from collections.abc import AsyncIterator

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
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

    host = (
        os.environ.get("PALINODE_MCP_HTTP_HOST")
        or os.environ.get("PALINODE_MCP_SSE_HOST")  # legacy alias
        or "0.0.0.0"
    )
    port = int(
        os.environ.get("PALINODE_MCP_HTTP_PORT")
        or os.environ.get("PALINODE_MCP_SSE_PORT")  # legacy alias
        or "6341"
    )
    log_level = os.environ.get("PALINODE_MCP_LOG_LEVEL", "info")
    print(f"Palinode MCP (Streamable HTTP) listening on http://{host}:{port}/mcp/")
    print(f"  Health check: http://{host}:{port}/healthz")
    print(f"  API backend:  {_api_url('')}")
    uvicorn.run(starlette_app, host=host, port=port, log_level=log_level)


# Legacy alias for existing setuptools console_scripts (palinode-mcp-sse)
main_sse = main_http


if __name__ == "__main__":
    main()
