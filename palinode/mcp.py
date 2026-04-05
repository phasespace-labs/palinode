"""
Palinode MCP Server

Exposes Palinode memory as MCP tools for Claude Code and other MCP clients.
Runs over stdio — spawned on demand by the client.

Tools:
  palinode_search  — semantic search over memory files
  palinode_save    — write a new memory item
  palinode_ingest  — ingest a URL into research memory
  palinode_status  — health check + index stats

Usage (Claude Code / claude_desktop_config.json):
  {
    "mcpServers": {
      "palinode": {
        "command": "ssh",
        "args": ["user@your-server",
                 "cd /path/to/palinode && venv/bin/python -m palinode.mcp"]
      }
    }
  }
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from palinode.core import embedder, store
from palinode.core.config import config

logger = logging.getLogger("palinode.mcp")
logging.basicConfig(level=logging.WARNING)  # quiet — don't pollute stdio

server = Server("palinode")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _format_results(results: list[dict[str, Any]]) -> str:
    """Format search results as clean text — minimal context burn.

    Args:
        results (list[dict[str, Any]]): Retrieved database query outputs securely packing semantic text block values arrays targets metrics endpoints logics formats footprint.

    Returns:
        str: Block string concatenating cleanly formatted scores matches formats schemas layouts targets endpoints text formats footprint.
    """
    if not results:
        return "No results found."
    parts = []
    for r in results:
        rel = r["file_path"].replace(config.palinode_dir + "/", "")
        score_pct = int(r.get("score", 0) * 100)
        parts.append(f"[{rel}] ({score_pct}% match)\n{r['content'].strip()}")
    return "\n\n---\n\n".join(parts)


def _save_memory(content: str, category_type: str, slug: str | None, entities: list[str], core: bool | None = None, source: str = "mcp") -> str:
    """Write a memory file explicitly defining categories schemas formats target layouts blocks and return the absolute pathway string logic footprints payloads logic paths targets payloads.

    Mirrors FastAPI `/save` overarching logics targeting DB disk persistence targets schemas block outputs schemas formats.

    Args:
        content (str): Text markdown payload schema.
        category_type (str): Explicit mapping arrays types string payloads logic sequence schema targets.
        slug (str | None): User manually formatted block logic array strings formats footprint payloads endpoints target layouts paths payloads targets sequences.
        entities (list[str]): Relational tag logic.

    Returns:
        str: Destinational disk payload path natively schemas format logic blocks schemas formats sequences footprints natively endpoints payloads.
    """
    import yaml

    type_map = {
        "PersonMemory": "people",
        "Decision": "decisions",
        "ProjectSnapshot": "projects",
        "Insight": "insights",
        "ResearchRef": "research",
        "ActionItem": "inbox",
    }
    category = type_map.get(category_type, "inbox")

    if slug:
        slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')

    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "-", content.split("\n")[0].lower()[:30]).strip("-")
    if not slug:
        slug = str(int(time.time()))

    file_path = os.path.join(config.palinode_dir, category, f"{slug}.md")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    frontmatter = {
        "id": f"{category}-{slug}",
        "category": category,
        "type": category_type,
        "entities": entities or [],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
    }
    if core is not None:
        frontmatter["core"] = core

    doc = f"---\n{yaml.dump(frontmatter)}---\n\n{content}\n"
    with open(file_path, "w") as f:
        f.write(doc)

    # Git commit (best-effort)
    if config.git.auto_commit:
        try:
            import subprocess
            subprocess.run(["git", "add", file_path], cwd=config.palinode_dir, check=False)
            commit_msg = f"{config.git.commit_prefix} mcp-save: {category}/{slug}.md"
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=config.palinode_dir,
                check=False,
            )
            if config.git.auto_push:
                subprocess.run(["git", "push"], cwd=config.palinode_dir, check=False)
        except Exception:
            pass

    return file_path


# ── Tool definitions ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Generates MCP capabilities map declaring available API wrappers securely.

    Returns:
        list[types.Tool]: MCP payload array logically declaring interface logic mappings blocks schema schemas paths target formatting schemas formats schemas.
    """
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
                },
                "required": ["query"],
            },
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
                        "description": "Source surface that created this memory (e.g., 'claude-code', 'antigravity', 'roo-code', 'openclaw-attractor'). Auto-detected if omitted.",
                    },
                },
                "required": ["content", "type"],
            },
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
        ),
        types.Tool(
            name="palinode_status",
            description="Check Palinode health: API reachability, index stats, last watcher run.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="palinode_history",
            description="View the creation/modification history of a memory file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to the memory directory (e.g. people/alice.md)"
                    }
                },
                "required": ["file_path"],
            },
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
        ),
        types.Tool(
            name="palinode_consolidate",
            description="Run a manual knowledge consolidation pass.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="palinode_lint",
            description="Scan memory files and report health issues (orphaned files, stale files, missing fields).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
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
                        "description": "Memory file path (e.g., 'projects/mm-kmd.md')",
                    },
                    "search": {
                        "type": "string",
                        "description": "Optional: filter to lines containing this text",
                    },
                },
                "required": ["file"],
            },
        ),
        types.Tool(
            name="palinode_timeline",
            description=(
                "Show the evolution of a memory file over time. Lists every change "
                "with dates and descriptions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Memory file path (e.g., 'projects/mm-kmd.md')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of changes to show (default 20)",
                        "default": 20,
                    },
                },
                "required": ["file"],
            },
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
                        "description": "Target commit hash (from palinode_timeline). Default: previous version.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), show what would change without applying.",
                        "default": True,
                    },
                },
                "required": ["file"],
            },
        ),
        types.Tool(
            name="palinode_push",
            description="Sync memory changes to GitHub for backup and cross-machine access.",
            inputSchema={"type": "object", "properties": {}},
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
                        "description": "For 'create': What context should fire this trigger (e.g., 'User is configuring LoRA')",
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
                        "description": "Source surface that created this memory (e.g., 'claude-code', 'antigravity', 'roo-code', 'openclaw-attractor'). Auto-detected if omitted.",
                    },
                },
                "required": ["summary"],
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """Execute MCP callbacks depending logic footprints mapped cleanly targets schemas formats endpoints layouts.

    Args:
        name (str): Identifier map sequence payload key.
        arguments (dict[str, Any]): Client payloads mapping inputs variables formats schema strings lists mappings.

    Returns:
        list[types.TextContent]: Standard MCP layout structure logic strings safely encapsulating return target outputs schemas mapped perfectly formats blocks layouts targets sequences frameworks layouts.
    """
    try:
        if name == "palinode_list":
            category = arguments.get("category")
            core_only = arguments.get("core_only", False)
            import httpx
            api_port = config.services.api.port
            params = {}
            if category: params["category"] = category
            if core_only: params["core_only"] = "true"
            
            resp = httpx.get(f"http://localhost:{api_port}/list", params=params, timeout=30.0)
            if resp.status_code == 200:
                data = resp.json()
                if not data:
                    return [types.TextContent(type="text", text="No files found.")]
                parts = []
                for f in data:
                    c_tag = " [core]" if f.get("core") else ""
                    parts.append(f"{f['file']} — {f['summary']}{c_tag}")
                return [types.TextContent(type="text", text="\n".join(parts))]
            return [types.TextContent(type="text", text=f"API Error: {resp.text}")]

        elif name == "palinode_read":
            file_path = arguments["file_path"]
            import httpx
            api_port = config.services.api.port
            resp = httpx.get(f"http://localhost:{api_port}/read", params={"file_path": file_path, "meta": "true"}, timeout=30.0)
            if resp.status_code == 200:
                data = resp.json()
                return [types.TextContent(type="text", text=data["content"])]
            return [types.TextContent(type="text", text=f"Error reading file: {resp.text}")]

        elif name == "palinode_search":
            query = arguments["query"]
            category = arguments.get("category")
            limit = int(arguments.get("limit", config.search.default_limit))
            date_after = arguments.get("date_after")
            date_before = arguments.get("date_before")

            loop = asyncio.get_event_loop()
            query_emb = await loop.run_in_executor(None, embedder.embed, query)
            if not query_emb:
                return [types.TextContent(type="text", text="Embedding service unavailable.")]

            if config.search.hybrid_enabled:
                results = await loop.run_in_executor(
                    None,
                    lambda: store.search_hybrid(
                        query_text=query,
                        query_embedding=query_emb,
                        category=category,
                        top_k=limit,
                        threshold=config.search.mcp_threshold,
                        hybrid_weight=config.search.hybrid_weight,
                        date_after=date_after,
                        date_before=date_before,
                    ),
                )
            else:
                results = await loop.run_in_executor(
                    None,
                    lambda: store.search(
                        query_embedding=query_emb,
                        category=category,
                        top_k=limit,
                        threshold=config.search.mcp_threshold,
                        date_after=date_after,
                        date_before=date_before,
                    ),
                )
            return [types.TextContent(type="text", text=_format_results(results))]

        elif name == "palinode_save":
            content = arguments["content"]
            category_type = arguments["type"]
            slug = arguments.get("slug")
            core = arguments.get("core")
            entities = arguments.get("entities", [])
            source = arguments.get("source", "mcp")

            loop = asyncio.get_event_loop()
            file_path = await loop.run_in_executor(
                None,
                lambda: _save_memory(content, category_type, slug, entities, core, source),
            )
            rel = file_path.replace(config.palinode_dir + "/", "")
            return [types.TextContent(type="text", text=f"Saved to {rel}")]

        elif name == "palinode_ingest":
            url = arguments["url"]
            name_arg = arguments.get("name", url.split("/")[-1][:40])

            import httpx
            api_port = config.services.api.port
            resp = httpx.post(
                f"http://localhost:{api_port}/ingest-url",
                json={"url": url, "name": name_arg},
                timeout=60.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("file_path"):
                    rel = data["file_path"].replace(config.palinode_dir + "/", "")
                    return [types.TextContent(type="text", text=f"Ingested → {rel}")]
                return [types.TextContent(type="text", text="No content extracted from URL.")]
            return [types.TextContent(type="text", text=f"Ingest failed: {resp.text}")]

        elif name == "palinode_history":
            import subprocess
            file_path = arguments["file_path"]
            
            base_dir = os.path.abspath(config.palinode_dir)
            full_path = os.path.abspath(os.path.join(base_dir, file_path))
            
            if not full_path.startswith(base_dir):
                return [types.TextContent(type="text", text="Error: Invalid file path (path traversal detected)")]

            if not os.path.exists(full_path):
                return [types.TextContent(type="text", text=f"File not found: {file_path}")]

            result = subprocess.run(
                ["git", "log", "-10", "--format=%H|%aI|%s", "--", file_path],
                capture_output=True, text=True,
                cwd=config.palinode_dir,
            )
            return [types.TextContent(type="text", text=result.stdout.strip() or "No history found.")]
            
        elif name == "palinode_entities":
            import json
            entity_ref = arguments.get("entity_ref")
            if entity_ref:
                files = store.get_entity_files(entity_ref)
                graph = store.get_entity_graph(entity_ref)
                return [types.TextContent(type="text", text=json.dumps({"files": files, "connected": graph}, indent=2))]
            else:
                db = store.get_db()
                try:
                    rows = db.execute("SELECT entity_ref, count(*) as count FROM entities GROUP BY entity_ref ORDER BY count DESC").fetchall()
                    res = [{"entity": r[0], "count": r[1]} for r in rows]
                finally:
                    db.close()
                return [types.TextContent(type="text", text=json.dumps(res, indent=2))]
                
        elif name == "palinode_consolidate":
            import json
            from palinode.consolidation.runner import run_consolidation
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, run_consolidation)
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "palinode_status":
            stats = store.get_stats()
            
            db = store.get_db()
            try:
                fts_count = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            except Exception:
                fts_count = 0
            db.close()

            try:
                import httpx
                httpx.get(config.embeddings.primary.url, timeout=2.0)
                ollama_ok = True
            except Exception:
                ollama_ok = False

            import subprocess
            r = subprocess.run(
                ["systemctl", "is-active", "palinode-watcher.service"],
                capture_output=True, text=True
            )
            watcher_status = r.stdout.strip() if r.returncode == 0 else "unknown"

            r2 = subprocess.run(
                ["systemctl", "is-active", "palinode-api.service"],
                capture_output=True, text=True
            )
            api_status = r2.stdout.strip() if r2.returncode == 0 else "unknown"

            lines = [
                f"Palinode Status",
                f"  Files indexed:  {stats['total_files']}",
                f"  Chunks indexed: {stats['total_chunks']}",
                f"  Hybrid search:  {'✅ enabled' if config.search.hybrid_enabled else '❌ disabled (vector only)'}",
                f"  FTS5 chunks:    {fts_count}",
                f"  Ollama (embed): {'✅ reachable' if ollama_ok else '❌ unreachable'}",
                f"  API service:    {api_status}",
                f"  Watcher:        {watcher_status}",
                f"  DB:             {config.db_path}",
                f"  Memory dir:     {config.palinode_dir}",
            ]
            return [types.TextContent(type="text", text="\n".join(lines))]

        elif name == "palinode_lint":
            import json
            import httpx
            api_port = config.services.api.port
            try:
                resp = httpx.post(f"http://localhost:{api_port}/lint", timeout=30.0)
                if resp.status_code == 200:
                    return [types.TextContent(type="text", text=json.dumps(resp.json(), indent=2))]
                return [types.TextContent(type="text", text=f"Error running lint: {resp.text}")]
            except httpx.RequestError:
                from palinode.core.lint import run_lint_pass
                return [types.TextContent(type="text", text=json.dumps(run_lint_pass(), indent=2))]

        elif name == "palinode_diff":
            from palinode.core import git_tools
            days = int(arguments.get("days", 7))
            paths = arguments.get("paths")
            result = git_tools.diff(days, paths)
            return [types.TextContent(type="text", text=result)]

        elif name == "palinode_blame":
            from palinode.core import git_tools
            result = git_tools.blame(arguments["file"], arguments.get("search"))
            return [types.TextContent(type="text", text=result)]

        elif name == "palinode_timeline":
            from palinode.core import git_tools
            result = git_tools.timeline(arguments["file"], int(arguments.get("limit", 20)))
            return [types.TextContent(type="text", text=result)]

        elif name == "palinode_rollback":
            from palinode.core import git_tools
            result = git_tools.rollback(
                arguments["file"],
                arguments.get("commit"),
                arguments.get("dry_run", True),
            )
            return [types.TextContent(type="text", text=result)]

        elif name == "palinode_push":
            from palinode.core import git_tools
            result = git_tools.push()
            return [types.TextContent(type="text", text=result)]

        elif name == "palinode_trigger":
            action = arguments.get("action", "create")
            if action == "list":
                import json
                triggers = store.list_triggers()
                return [types.TextContent(type="text", text=json.dumps(triggers, indent=2))]
            elif action == "delete":
                tid = arguments.get("trigger_id")
                if not tid:
                    return [types.TextContent(type="text", text="Error: trigger_id required for delete")]
                store.delete_trigger(tid)
                return [types.TextContent(type="text", text=f"Deleted trigger {tid}")]
            else:
                desc = arguments.get("description")
                mem = arguments.get("memory_file")
                if not desc or not mem:
                    return [types.TextContent(type="text", text="Error: description and memory_file required for create")]
                
                import uuid
                tid = arguments.get("trigger_id") or str(uuid.uuid4())
                loop = asyncio.get_event_loop()
                emb = await loop.run_in_executor(None, embedder.embed, desc)
                if not emb:
                    return [types.TextContent(type="text", text="Error: embedding failed")]
                
                await loop.run_in_executor(
                    None,
                    lambda: store.add_trigger(tid, desc, mem, emb)
                )
                return [types.TextContent(type="text", text=f"Created trigger {tid} for {mem}")]

        elif name == "palinode_session_end":
            summary = arguments.get("summary", "")
            decisions = arguments.get("decisions", [])
            blockers = arguments.get("blockers", [])
            project = arguments.get("project")
            source = arguments.get("source", "mcp")

            from datetime import datetime
            today = datetime.utcnow().strftime("%Y-%m-%d")
            now_iso = datetime.utcnow().isoformat() + "Z"

            # Build session entry
            parts = [f"## Session End — {now_iso}\n"]
            parts.append(f"**Source:** {source}\n")
            parts.append(f"**Summary:** {summary}\n")
            if decisions:
                parts.append("**Decisions:**")
                for d in decisions:
                    parts.append(f"- {d}")
                parts.append("")
            if blockers:
                parts.append("**Blockers/Next:**")
                for b in blockers:
                    parts.append(f"- {b}")
                parts.append("")

            session_entry = "\n".join(parts)

            # Write to daily notes
            daily_dir = os.path.join(config.memory_dir, "daily")
            os.makedirs(daily_dir, exist_ok=True)
            daily_path = os.path.join(daily_dir, f"{today}.md")
            with open(daily_path, "a") as f:
                f.write(f"\n{session_entry}\n")

            # Append status to project -status.md if project specified or detectable
            status_msg = ""
            if project:
                status_path = os.path.join(config.memory_dir, "projects", f"{project}-status.md")
                if os.path.exists(status_path):
                    one_liner = summary.replace("\n", " ").strip()[:200]
                    with open(status_path, "a") as f:
                        f.write(f"\n- [{today}] {one_liner}\n")
                    status_msg = f" + status → {project}-status.md"

            return [types.TextContent(
                type="text",
                text=f"Session captured → daily/{today}.md{status_msg}\n\n{session_entry}",
            )]

        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return [types.TextContent(type="text", text=f"Error: {e}")]


# ── Entry point ───────────────────────────────────────────────────────────────

async def async_main() -> None:
    """Async boot sequence capturing STDIN channels logic targets."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )

def main() -> None:
    """Synchronous entry point for setuptools console_scripts."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
