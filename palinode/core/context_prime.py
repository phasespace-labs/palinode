"""Session-start context digest (ADR-012 Layer 4; the /context/prime core).

One bounded, deterministic digest of "what should a fresh session know":
the resolved project scope, `core: true` memories, recently-modified
decisions, and open action items — built from frontmatter reads and mtime
sorts only (no embeds, no LLM, no network), so it is safe on the session
cold-start path.

Serves every surface per ADR-010 parity:

- ``POST /context/prime`` — the endpoint the Claude Code SessionStart hook
  already calls (shipped forward-compat; this module makes it live).
- ``palinode_session_init`` — the MCP tool for MCP-only harnesses (Claude
  Desktop, Codex CLI, Gemini CLI), ADR-012 §3.4's discoverable
  "first call you should make".
- ``palinode prime`` — the CLI.

Scope discipline (the ADR's critical constraint): project-scoped rows are
returned ONLY when a project actually resolves — explicit ``project`` arg,
else ``cwd`` basename through ``config.context.project_map`` /
``auto_detect`` (the same ADR-008 resolution the ambient search boost uses).
With no resolvable project (e.g. Claude Desktop, which has no CWD), the
digest degrades to core memories only, clearly labelled — it never guesses
a project and never bleeds another project's context.
"""
from __future__ import annotations

import glob
import os
from typing import Any

from palinode.core.config import config

#: Bounded digest sizes — the digest rides the session cold-start path.
MAX_CORE_MEMORIES = 10
MAX_RECENT_DECISIONS = 5
MAX_OPEN_ACTION_ITEMS = 5
#: Hard cap on any single digest line (title + description).
MAX_LINE_CHARS = 200

PALINODE_HINT = (
    "Memory available. Call palinode_search before answering questions about "
    "prior decisions or project state; save decisions with palinode_save "
    "(include the rationale); call palinode_session_end before the session ends."
)

# Unlike the /list browse surface, `inbox` is NOT skipped — ActionItems live
# there, and open action items are one of the digest's three sections.
_SKIP_DIRS = {"daily", "archive", "logs", "prompts", ".obsidian"}


def resolve_project(cwd: str | None = None, project: str | None = None) -> str | None:
    """Resolve a project entity ref, or None when no project can be named.

    Explicit ``project`` wins (bare slugs gain the ``project/`` prefix), then
    the ``cwd`` basename through ``config.context.project_map`` and
    ``auto_detect`` — the same ADR-008 resolution order the ambient search
    boost uses. Never guesses when neither is usable.
    """
    if project:
        return project if "/" in project else f"project/{project}"
    if not cwd:
        return None
    basename = os.path.basename(os.path.normpath(cwd))
    if not basename:
        return None
    mapped = config.context.project_map.get(basename)
    if mapped:
        return mapped if "/" in mapped else f"project/{mapped}"
    if config.context.auto_detect:
        return f"project/{basename}"
    return None


def _scan_memories(base_dir: str) -> list[dict[str, Any]]:
    """Frontmatter scan of the memory dir (skip-dirs excluded), mtime attached."""
    from palinode.core import parser

    out: list[dict[str, Any]] = []
    for filepath in glob.glob(os.path.join(base_dir, "**/*.md"), recursive=True):
        rel = os.path.relpath(filepath, base_dir)
        if rel.split(os.sep)[0] in _SKIP_DIRS:
            continue
        try:
            with open(filepath, encoding="utf-8") as f:
                meta, _ = parser.parse_markdown(f.read())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        out.append({"file": rel, "meta": meta, "mtime": os.path.getmtime(filepath)})
    return out


def _digest_row(entry: dict[str, Any]) -> dict[str, str]:
    meta = entry["meta"]
    title = str(meta.get("title") or "").strip()
    if not title:
        title = os.path.splitext(os.path.basename(entry["file"]))[0]
    description = str(meta.get("description") or "").strip()
    line = f"{title} — {description}" if description else title
    return {"file": entry["file"], "summary": line[:MAX_LINE_CHARS]}


def _has_entity(meta: dict[str, Any], entity: str) -> bool:
    entities = meta.get("entities")
    return isinstance(entities, list) and entity in entities


def build_context_digest(
    cwd: str | None = None, project: str | None = None
) -> dict[str, Any]:
    """Build the bounded session-start digest for the resolved scope."""
    resolved = resolve_project(cwd=cwd, project=project)
    memories = _scan_memories(config.memory_dir)

    core = [m for m in memories if m["meta"].get("core") is True]
    core.sort(key=lambda m: m["mtime"], reverse=True)

    recent_decisions: list[dict[str, Any]] = []
    open_action_items: list[dict[str, Any]] = []
    if resolved:
        scoped = [m for m in memories if _has_entity(m["meta"], resolved)]
        decisions = [
            m for m in scoped
            if m["meta"].get("type") == "Decision" or m["file"].startswith("decisions/")
        ]
        decisions.sort(key=lambda m: m["mtime"], reverse=True)
        recent_decisions = decisions[:MAX_RECENT_DECISIONS]

        actions = [
            m for m in scoped
            if m["meta"].get("type") == "ActionItem"
            and m["meta"].get("status", "active") not in ("archived", "done", "resolved")
        ]
        actions.sort(key=lambda m: m["mtime"], reverse=True)
        open_action_items = actions[:MAX_OPEN_ACTION_ITEMS]

    return {
        "project": resolved,
        "core_memories": [_digest_row(m) for m in core[:MAX_CORE_MEMORIES]],
        "recent_decisions": [_digest_row(m) for m in recent_decisions],
        "open_action_items": [_digest_row(m) for m in open_action_items],
        "_palinode_hint": PALINODE_HINT,
    }


def format_context_digest(digest: dict[str, Any]) -> str:
    """Render the digest as compact text (shared by the MCP and CLI surfaces)."""
    lines: list[str] = []
    if digest.get("project"):
        lines.append(f"## Session context: {digest['project']}")
    else:
        lines.append("## Session context (no project resolved — core memories only)")
    sections = (
        ("Core memories", digest.get("core_memories", [])),
        ("Recent decisions", digest.get("recent_decisions", [])),
        ("Open action items", digest.get("open_action_items", [])),
    )
    for label, rows in sections:
        if rows:
            lines.append(f"### {label}")
            for row in rows:
                lines.append(f"- [{row['file']}] {row['summary']}")
    if len(lines) == 1:
        lines.append("(no memories in scope yet)")
    lines.append("")
    lines.append(digest.get("_palinode_hint", PALINODE_HINT))
    return "\n".join(lines)
