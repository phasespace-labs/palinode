"""Shared smoke-args registry for MCP tool-coverage tests.

Used by:
  - tests/integration/test_mcp_e2e.py (#343, in-process via _dispatch_tool)
  - tests/integration/test_mcp_stdio.py (#344, real stdio JSON-RPC subprocess)

Sourced from palinode/mcp.py::list_tools(); a drift guard in test_mcp_e2e.py
asserts that every registered tool has an entry here.

Each entry is (args, lenient):
  - lenient=False: tool response must NOT start with a dispatcher error prefix
  - lenient=True: tool may legitimately error in a sealed test env (no Ollama,
                  no git remote, no network) — only the no-crash invariant is
                  checked
"""
from __future__ import annotations

import asyncio
import os


TOOL_SMOKE_ARGS: dict[str, tuple[dict, bool]] = {
    # Read-only / index-side tools — should always succeed in a clean env
    "palinode_list":              ({}, False),
    "palinode_read":              ({"file_path": "insights/smoke-target.md"}, False),
    "palinode_search":            ({"query": "smoke", "threshold": 0.0}, False),
    "palinode_status":            ({}, False),
    "palinode_entities":          ({}, False),
    "palinode_diff":              ({"days": 7}, False),
    "palinode_doctor":            ({}, False),
    "palinode_lint":              ({}, False),
    "palinode_trigger":           ({"action": "list"}, False),
    "palinode_prompt":            ({"action": "list"}, False),
    "palinode_depends":           ({"unblocked": True}, False),

    # Write tools — exercise happy path against isolated tmp dir
    "palinode_save":              ({"content": "smoke save body", "type": "Insight",
                                    "slug": "smoke-loop-save"}, False),
    "palinode_session_end":       ({"summary": "smoke session end"}, False),

    # Git-aware tools — config.git.auto_commit=False, so endpoints
    # gracefully return "No history found." / "No blame data." (not error)
    "palinode_history":           ({"file_path": "insights/smoke-target.md"}, False),
    "palinode_timeline":          ({"file_path": "insights/smoke-target.md"}, False),
    "palinode_blame":             ({"file_path": "insights/smoke-target.md"}, False),
    "palinode_rollback":          ({"file_path": "insights/smoke-target.md"}, False),

    # Embedding-aware tools — _fake_embed returns a constant vector, so
    # similarity is uniform; tools should still dispatch and return either
    # results or "No ... found." (not an error)
    "palinode_dedup_suggest":     ({"content": "smoke draft content for dedup"}, False),
    "palinode_orphan_repair":     ({"broken_link": "[[smoke-broken-link]]"}, False),
    "palinode_cluster_neighbors": ({"file_path": "insights/smoke-target.md"}, False),
    "palinode_topic_coverage":    ({"query": "smoke topic phrase"}, False),

    # Deterministic maintenance sweep — dry-run completes in a clean env
    "palinode_archive_expired":   ({"dry_run": True}, False),

    # Lenient — legitimately may error in test env
    "palinode_ingest":            ({"url": "https://example.com/"}, True),       # no network in CI
    "palinode_consolidate":       ({"dry_run": True}, True),                     # needs LLM
    "palinode_push":              ({}, True),                                    # needs git remote
    "palinode_doctor_deep":       ({}, True),                                    # canary writes + network
}


# Tools excluded from hermetic parametrized dispatch (e2e + stdio).
# Entry stays in TOOL_SMOKE_ARGS so the drift guard still fires if the tool
# is removed from palinode/mcp.py; only execution is suppressed.
SKIP_TOOLS: dict[str, str] = {
    "palinode_doctor_deep": (
        "issues live network probes and canary writes; "
        "not safe in a hermetic CI env"
    ),
}


# Error-prefix strings the dispatcher uses to signal a failed call.
# Keep this in sync with the `_text(f"... failed: {resp.text}")` /
# `_text(f"Error: {e}")` patterns in palinode/mcp.py::_dispatch_tool.
DISPATCH_ERROR_PREFIXES: tuple[str, ...] = (
    "Error:",
    "API Error:",
    "API unreachable",
    "Search failed",
    "Save failed",
    "Session-end failed",
    "Doctor failed",
    "Doctor (deep) failed",
    "Lint failed",
    "Consolidation failed",
    "Archive-expired sweep failed",
    "Push failed",
    "Ingest failed",
    "Unknown tool",
)


def registered_tool_names() -> list[str]:
    """Source of truth: the running MCP server's @server.list_tools() output."""
    from palinode.mcp import list_tools
    previous = os.environ.get("PALINODE_MCP_SURFACE")
    os.environ["PALINODE_MCP_SURFACE"] = "full"
    try:
        tools = asyncio.run(list_tools())
    finally:
        if previous is None:
            os.environ.pop("PALINODE_MCP_SURFACE", None)
        else:
            os.environ["PALINODE_MCP_SURFACE"] = previous
    return [t.name for t in tools]
