"""`palinode init` — scaffold Palinode into a project for zero-friction adoption.

Creates:
  - .claude/CLAUDE.md  (memory section, appended if file exists)
  - .claude/settings.json  (SessionEnd hook for /clear auto-capture)
  - .claude/hooks/palinode-session-end.sh  (hook script)
  - .mcp.json  (MCP server block for palinode, if --mcp given)

All writes are opt-out via flags. Existing files are preserved — we append
or skip, never overwrite without --force.
"""
import json
import os
import re
import stat
from pathlib import Path

import click


CLAUDE_MD_BLOCK = """\
## Memory (Palinode)

This project uses Palinode for persistent memory via MCP (server name: `palinode`).

### At session start
- Call `palinode_search` with the current task or project name to pull prior context.
- If the MCP server is down, fall back to the CLI: `palinode search "<query>"`.

### During work
- After a milestone (tests pass, feature shipped, bug root-caused), call
  `palinode_save` with the outcome. Include *why*, not just *what*.
- When making an architectural or design decision, save the decision AND the
  rationale as `type="Decision"`.
- Save surprising reusable findings as `type="Insight"`.
- Every ~30 minutes of active work, save a one-line progress note.

### At session end — including `/clear`
- Call `palinode_session_end` with `summary`, `decisions`, `blockers`, and
  `project="{project_slug}"` before the session terminates.
- `/clear` counts as a session end. The SessionEnd hook installed by
  `palinode init` captures a fallback snapshot automatically, but calling
  `palinode_session_end` from the agent first produces a far better record.
- The user may type `/ps` (Palinode Save) or `/wrap` (session wrap-up) as
  shortcuts. These are **deterministic** — each maps to exactly one tool:
  - `/ps` → always `palinode_save` with `type="ProjectSnapshot"`. Use for
    mid-session checkpoints.
  - `/wrap` → always `palinode_session_end` with summary/decisions/blockers.
    Use before `/clear`.
  Never dispatch one to the other's tool. See `.claude/commands/ps.md` and
  `.claude/commands/wrap.md` for the exact prompts.

### What NOT to save
- Raw code (git handles that).
- Step-by-step debug logs — save the resolution, not the journey.
- Trivial changes ("fixed typo" is not worth a memory).

### Project slug
This project's slug is `{project_slug}`. Pass it as the `project` argument to
`palinode_save` and `palinode_session_end` so status rolls up correctly.
"""


HOOK_SCRIPT = """\
#!/bin/bash
# palinode-session-end.sh — Auto-capture Claude Code sessions to Palinode.
#
# Fires on SessionEnd (including /clear, logout, exit). Reads the transcript
# from stdin JSON, extracts a minimal summary, and POSTs to palinode-api.
#
# Fail-silent by design — never block Claude Code exit. If the API is down
# we drop the capture and move on.

set -euo pipefail

PALINODE_API="${PALINODE_API_URL:-http://localhost:6340}"
MIN_MESSAGES="${PALINODE_HOOK_MIN_MESSAGES:-3}"

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
SOURCE_REASON=$(echo "$INPUT" | jq -r '.source // .reason // "other"')

# No transcript → nothing to capture
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# Claude Code transcript format:
#   user:      {type: "user", message: {role: "user", content: "text"}}
#   assistant: {type: "assistant", message: {content: [{type: "text", text: "..."}]}}
MSG_COUNT=$(jq -r 'select(.type == "user") | .message.content // empty' \\
  "$TRANSCRIPT_PATH" 2>/dev/null | grep -c '.' 2>/dev/null || echo "0")

# Skip trivial sessions
if [ "$MSG_COUNT" -lt "$MIN_MESSAGES" ]; then
  exit 0
fi

PROJECT=$(basename "$CWD" 2>/dev/null || echo "unknown")
FIRST_PROMPT=$(jq -r 'select(.type == "user") | .message.content // empty' \\
  "$TRANSCRIPT_PATH" 2>/dev/null | head -1 | cut -c1-200)

SUMMARY="Auto-captured (${SOURCE_REASON}, ${MSG_COUNT} messages). Topic: ${FIRST_PROMPT}"

curl -sS -o /dev/null \\
  -X POST "${PALINODE_API}/session-end" \\
  -H "Content-Type: application/json" \\
  -d "$(jq -n \\
    --arg summary "$SUMMARY" \\
    --arg project "$PROJECT" \\
    --arg source "claude-code-hook" \\
    '{summary: $summary, project: $project, source: $source, decisions: [], blockers: []}'
  )" \\
  --connect-timeout 5 \\
  --max-time 10 || true

exit 0
"""


PS_COMMAND_BODY = """\
---
description: Palinode Save — drop a mid-session ProjectSnapshot to persistent memory.
---

Call `palinode_save` with:
- `type` — **always** `"ProjectSnapshot"` (this command is exclusively for
  progress snapshots; use `/wrap` for end-of-session wrap-ups)
- `content` — a one-to-three sentence summary of what's been done since the
  last save and what's next. Written in past/present tense, specific enough
  that a future session could pick up where this one left off.
- `project` — the project slug from `.claude/CLAUDE.md` (or the directory
  name if no slug is set)

After saving, print one line: the file path and slug from the tool result.
Do not editorialise. Do not call any other tool.

**This command is deterministic.** Always `palinode_save`, always
`ProjectSnapshot`. If the user is wrapping up for the day, they should use
`/wrap` instead — that calls `palinode_session_end` with a structured
summary, decisions, and blockers.
"""


WRAP_COMMAND_BODY = """\
---
description: Wrap up this session — structured session_end save before /clear.
---

Call `palinode_session_end` with:
- `summary` — 1-2 sentences on what was accomplished this session
- `decisions` — array of key decisions made, each with its rationale (the
  *why*, not just the *what*)
- `blockers` — array of open questions, unfinished work, or next steps the
  next session needs to pick up
- `project` — the project slug from `.claude/CLAUDE.md` (or the directory
  name if no slug is set)

After the tool returns, print exactly: `✓ session saved — safe to /clear now.`
followed by the daily-note path from the tool result.

Do not call any other tool. Do not save as a ProjectSnapshot first — this
command is exclusively for structured session wrap-ups.

**This command is deterministic.** Always `palinode_session_end`. For a
quick mid-session checkpoint, use `/ps` instead.
"""


SETTINGS_HOOK_BLOCK = {
    "hooks": {
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/palinode-session-end.sh",
                        "timeout": 15,
                    }
                ]
            }
        ]
    }
}


MCP_JSON_BLOCK = {
    "mcpServers": {
        "palinode": {
            "command": "palinode-mcp",
            "env": {},
        }
    }
}


def _slugify(name: str) -> str:
    """Turn a directory name into a safe project slug."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "project"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_claude_md(path: Path, project_slug: str, force: bool) -> str:
    block = CLAUDE_MD_BLOCK.format(project_slug=project_slug)
    _ensure_parent(path)
    if not path.exists():
        path.write_text(block)
        return "created"
    existing = path.read_text()
    if "## Memory (Palinode)" in existing and not force:
        return "skipped (already has Palinode section)"
    with path.open("a") as f:
        if not existing.endswith("\n"):
            f.write("\n")
        f.write("\n" + block)
    return "appended"


def _write_hook_script(path: Path, force: bool) -> str:
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(HOOK_SCRIPT)
    # chmod +x
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return "created"


def _merge_settings(path: Path, force: bool) -> str:
    _ensure_parent(path)
    if not path.exists():
        path.write_text(json.dumps(SETTINGS_HOOK_BLOCK, indent=2) + "\n")
        return "created"
    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError:
        if not force:
            return "skipped (existing settings.json is not valid JSON — re-run with --force to overwrite)"
        existing = {}
    hooks = existing.setdefault("hooks", {})
    session_end_hooks = hooks.setdefault("SessionEnd", [])
    # Check for an existing palinode hook
    for entry in session_end_hooks:
        for h in entry.get("hooks", []):
            if "palinode-session-end.sh" in h.get("command", ""):
                return "skipped (palinode hook already registered)"
    session_end_hooks.append(SETTINGS_HOOK_BLOCK["hooks"]["SessionEnd"][0])
    path.write_text(json.dumps(existing, indent=2) + "\n")
    return "merged"


def _write_slash_command(path: Path, body: str, force: bool) -> str:
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(body)
    return "created"


def _merge_mcp_json(path: Path, force: bool) -> str:
    _ensure_parent(path)
    if not path.exists():
        path.write_text(json.dumps(MCP_JSON_BLOCK, indent=2) + "\n")
        return "created"
    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError:
        if not force:
            return "skipped (existing .mcp.json is not valid JSON — re-run with --force to overwrite)"
        existing = {}
    servers = existing.setdefault("mcpServers", {})
    if "palinode" in servers and not force:
        return "skipped (palinode MCP server already configured)"
    servers["palinode"] = MCP_JSON_BLOCK["mcpServers"]["palinode"]
    path.write_text(json.dumps(existing, indent=2) + "\n")
    return "merged"


@click.command("init")
@click.option(
    "--dir", "target_dir",
    default=".",
    type=click.Path(file_okay=False),
    help="Project directory to scaffold (default: current)",
)
@click.option(
    "--project", "project_slug",
    default=None,
    help="Project slug (default: inferred from directory name)",
)
@click.option(
    "--mcp/--no-mcp",
    default=True,
    help="Write .mcp.json with the palinode MCP server block",
)
@click.option(
    "--claudemd/--no-claudemd",
    default=True,
    help="Write the Palinode memory block to .claude/CLAUDE.md",
)
@click.option(
    "--hook/--no-hook",
    default=True,
    help="Install the SessionEnd hook script + .claude/settings.json",
)
@click.option(
    "--slash/--no-slash",
    default=True,
    help="Install /ps and /wrap slash commands for save-before-clear reflex",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing files (default: preserve / append / skip)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would change without writing anything",
)
def init(target_dir, project_slug, mcp, claudemd, hook, slash, force, dry_run):
    """Scaffold Palinode into a project for zero-friction adoption.

    Creates (or appends to):
      .claude/CLAUDE.md                     — memory instructions for the agent
      .claude/settings.json                 — SessionEnd hook registration
      .claude/hooks/palinode-session-end.sh — hook script (fires on /clear, exit)
      .mcp.json                             — palinode MCP server block

    Re-run with --force to overwrite. --dry-run shows the plan without writing.
    """
    target = Path(target_dir).resolve()
    if not target.exists():
        raise click.ClickException(f"Directory not found: {target}")

    slug = project_slug or _slugify(target.name)

    claude_md = target / ".claude" / "CLAUDE.md"
    settings = target / ".claude" / "settings.json"
    hook_script = target / ".claude" / "hooks" / "palinode-session-end.sh"
    mcp_json = target / ".mcp.json"
    ps_cmd = target / ".claude" / "commands" / "ps.md"
    wrap_cmd = target / ".claude" / "commands" / "wrap.md"

    click.echo(f"Palinode init → {target}")
    click.echo(f"  project slug: {slug}")
    click.echo("")

    if dry_run:
        click.echo("[dry-run] Would write:")
        if claudemd:
            click.echo(f"  {claude_md.relative_to(target)}  (memory instructions)")
        if hook:
            click.echo(f"  {hook_script.relative_to(target)}  (SessionEnd hook script)")
            click.echo(f"  {settings.relative_to(target)}  (hook registration)")
        if slash:
            click.echo(f"  {ps_cmd.relative_to(target)}  (/ps slash command)")
            click.echo(f"  {wrap_cmd.relative_to(target)}  (/wrap slash command)")
        if mcp:
            click.echo(f"  {mcp_json.relative_to(target)}  (MCP server block)")
        return

    results = []
    if claudemd:
        results.append(("CLAUDE.md", _write_claude_md(claude_md, slug, force)))
    if hook:
        results.append(("hook script", _write_hook_script(hook_script, force)))
        results.append(("settings.json", _merge_settings(settings, force)))
    if slash:
        results.append(("/ps command", _write_slash_command(ps_cmd, PS_COMMAND_BODY, force)))
        results.append(("/wrap command", _write_slash_command(wrap_cmd, WRAP_COMMAND_BODY, force)))
    if mcp:
        results.append((".mcp.json", _merge_mcp_json(mcp_json, force)))

    for label, status in results:
        mark = "✓" if status in ("created", "appended", "merged") else "·"
        click.echo(f"  {mark} {label}: {status}")

    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Make sure palinode-api is running (palinode start, or systemd)")
    click.echo("  2. Open the project in Claude Code — the MCP server will connect on start")
    click.echo("  3. Try it:  \"search palinode for recent decisions on this project\"")
