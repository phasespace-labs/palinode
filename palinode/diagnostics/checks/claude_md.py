"""
Check: claude_md_palinode_block

Walks the user's HOME directory for ~/.claude/CLAUDE.md (global) and any
CLAUDE.md in ancestor directories of cwd up to HOME (project-level).  For
each file found, checks whether it mentions "palinode" (case-insensitive).

If NEITHER the global nor any project CLAUDE.md mentions palinode, warns.
This is the #1 install-day footgun: the MCP tools are registered and work
fine, but the LLM is never told to use them at session boundaries.

Severity: warn (neither file mentions palinode)

Tag: fast (pure filesystem reads, no network, no SQLite)

 """
from __future__ import annotations

import os
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


def _find_claude_md_files(home: Path, cwd: Path) -> list[Path]:
    """Return a list of candidate CLAUDE.md file paths to inspect.

    Checks:
      1. ~/.claude/CLAUDE.md  (global)
      2. Every CLAUDE.md in cwd and its ancestors up to home (project-level)

    The list is deduplicated and only includes paths that actually exist.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        resolved = p.resolve()
        if resolved not in seen and resolved.exists():
            seen.add(resolved)
            candidates.append(resolved)

    # Global Claude Code config.
    _add(home / ".claude" / "CLAUDE.md")

    # Project-level: cwd up to (and including) home.
    current = cwd.resolve()
    while True:
        _add(current / "CLAUDE.md")
        if current == home or current == current.parent:
            break
        current = current.parent

    return candidates


def _mentions_palinode(path: Path) -> bool:
    """Return True if the file content contains 'palinode' (case-insensitive)."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        return "palinode" in content.lower()
    except OSError:
        return False


@register(tags=("fast",))
def claude_md_palinode_block(ctx: DoctorContext) -> CheckResult:
    """Warn when no CLAUDE.md in scope mentions palinode.

    The MCP tools work fine without a CLAUDE.md mention, but the LLM will
    not proactively call palinode_save / palinode_search at session boundaries
    unless it is instructed to.  This is the #1 install-day footgun.
    """
    home = Path.home()
    cwd = Path(os.getcwd())

    candidates = _find_claude_md_files(home, cwd)

    if not candidates:
        return CheckResult(
            name="claude_md_palinode_block",
            severity="warn",
            passed=False,
            message=(
                "No CLAUDE.md files found (checked ~/.claude/CLAUDE.md and "
                "project directories up to home).  "
                "The LLM will not know to use palinode tools at session boundaries."
            ),
            remediation=(
                "Run 'palinode init' in your project directory to scaffold a "
                "CLAUDE.md with the palinode memory block, or add the block manually:\n"
                "  ## Memory (Palinode)\n"
                "  Call palinode_search at session start, palinode_save after milestones."
            ),
        )

    # Check each file.
    found_with_palinode: list[Path] = []
    found_without_palinode: list[Path] = []

    for p in candidates:
        if _mentions_palinode(p):
            found_with_palinode.append(p)
        else:
            found_without_palinode.append(p)

    if found_with_palinode:
        # At least one CLAUDE.md mentions palinode — the LLM will see it.
        files_str = ", ".join(str(p) for p in found_with_palinode)
        return CheckResult(
            name="claude_md_palinode_block",
            severity="warn",
            passed=True,
            message=f"Palinode memory block found in: {files_str}",
            remediation=None,
        )

    # No CLAUDE.md mentions palinode.
    checked_str = "\n".join(f"  {p}" for p in candidates)
    return CheckResult(
        name="claude_md_palinode_block",
        severity="warn",
        passed=False,
        message=(
            "None of the CLAUDE.md files in scope mention palinode.  "
            "The LLM will not call palinode tools at session boundaries."
        ),
        remediation=(
            "Add a '## Memory (Palinode)' section to at least one of:\n"
            f"{checked_str}\n\n"
            "Or run 'palinode init --no-mcp --no-hook --no-slash' to add the "
            "memory block automatically.  Minimum block:\n"
            "  ## Memory (Palinode)\n"
            "  Call palinode_search at session start, "
            "palinode_save after milestones, palinode_session_end at wrap."
        ),
    )
