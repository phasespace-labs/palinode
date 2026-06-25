"""`palinode init` — scaffold Palinode into a project for zero-friction adoption.

Creates:
  - .claude/CLAUDE.md  (memory section, appended if file exists)
  - .claude/settings.json  (SessionEnd hook for /clear auto-capture)
  - .claude/hooks/palinode-session-end.sh  (hook script)
  - .mcp.json  (MCP server block for palinode, if --mcp given)

With --obsidian, additionally writes:
  - .obsidian/app.json       (file recovery, daily/ default location, wikilinks)
  - .obsidian/graph.json     (pre-tuned graph: collapsed dirs, color groups)
  - .obsidian/workspace.json (sidebar opens on daily/)
  - _index.md                (starter MOC at vault root)
  - _README.md               (orientation for first-time openers)

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
  - `/save` → always `palinode_save` with `type="ProjectSnapshot"`. Use for
    mid-session checkpoints. (`/ps` is a back-compat alias for `/save`.)
  - `/wrap` → always `palinode_session_end` with summary/decisions/blockers.
    Use before `/clear`.
  Never dispatch one to the other's tool. See the `/save` and `/wrap`
  command/skill definitions (installed by `palinode init`) for the exact prompts.

### What NOT to save
- Raw code (git handles that).
- Step-by-step debug logs — save the resolution, not the journey.
- Trivial changes ("fixed typo" is not worth a memory).

### Project slug
This project's slug is `{project_slug}`. Pass it as the `project` argument to
`palinode_save` and `palinode_session_end` so status rolls up correctly.
{wrap_policy_note}"""


# Appended to the CLAUDE.md memory block only when `--wrap-policy heavy` is
# chosen (#419). This is the inspectable record of which `/wrap` variant the
# repo runs — the behaviour itself lives in the installed `/wrap` command/skill
# body (rendered from WRAP_HEAVY_COMMAND_BODY).
WRAP_POLICY_HEAVY_NOTE = """
### Wrap policy
`wrap-policy: heavy` — `/wrap` in this repo runs the heavy sequence (merge →
push → triage dangling items → `palinode_session_end`), halting loudly on any
failure. See the installed `/wrap` command/skill for the exact contract.
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
# Max time (seconds) the curl POST is allowed to run.  Raise with
# PALINODE_HOOK_TIMEOUT if your host is slow (cold Ollama, WAN private VPN, NFS).
# The Claude Code hook runner timeout in settings.json must be > this value.
HOOK_TIMEOUT="${PALINODE_HOOK_TIMEOUT:-30}"

# Reasons to capture on. Default broad: clear, logout, normal exit (other),
# and non-interactive EOF. Override with PALINODE_HOOK_REASONS to narrow
# (e.g. "clear") or extend (add "resume" / "bypass_permissions_disabled").
# See https://code.claude.com/docs/en/hooks.md for the full reason list.
ALLOWED_REASONS="${PALINODE_HOOK_REASONS:-clear logout prompt_input_exit other}"

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
SOURCE_REASON=$(echo "$INPUT" | jq -r '.source // .reason // "other"')

# Drop reasons we're not capturing. Word-boundary match on a space-padded
# allowlist so substrings (e.g. "log" in "logout") don't false-positive.
case " $ALLOWED_REASONS " in
  *" $SOURCE_REASON "*) ;;
  *) exit 0 ;;
esac

# No transcript → nothing to capture
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# Skip-if-/wrap-ran (#378 floor/ceiling): if the human already ran /wrap this
# session, the transcript holds a `palinode_session_end` tool call. That
# agent-authored capture (summary + decisions + blockers, each with a why) is
# strictly richer than this deterministic floor, so writing the floor too just
# duplicates. Skip. Override with PALINODE_HOOK_FORCE=1 to capture regardless.
if [ "${PALINODE_HOOK_FORCE:-0}" != "1" ] \\
   && grep -q 'palinode_session_end' "$TRANSCRIPT_PATH" 2>/dev/null; then
  exit 0
fi

# Claude Code transcript format:
#   user:      {type: "user", message: {role: "user", content: "text"}}
#   assistant: {type: "assistant", message: {content: [{type: "text", text: "..."}]}}
#
# Both extractions use `jq -s` (slurp) so all reductions happen INSIDE jq.
# Earlier versions piped `jq | head -1` and `jq | grep -c '.'`, which was
# fragile under `set -o pipefail`: the downstream consumer exits early, the
# next jq write hits a closed pipe → SIGPIPE → pipefail aborts the script.
# The MSG_COUNT case was first patched with `|| true` (#151); the
# FIRST_PROMPT case retained the same fragile shape until #267. Slurping
# reads JSONL lines into an array; map+filter+slice runs without an
# early-exit downstream consumer, eliminating the SIGPIPE class entirely.
# Mirrors examples/hooks/palinode-session-end.sh fix from #257.
MSG_COUNT=$(jq -r -s 'map(select(.type == "user") | .message.content // empty) | length' \\
  "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
MSG_COUNT=${MSG_COUNT:-0}

# Skip trivial sessions
if [ "$MSG_COUNT" -lt "$MIN_MESSAGES" ]; then
  exit 0
fi

PROJECT=$(basename "$CWD" 2>/dev/null || echo "unknown")
FIRST_PROMPT=$(jq -r -s 'map(select(.type == "user") | .message.content // empty) | .[0] // ""' \\
  "$TRANSCRIPT_PATH" 2>/dev/null | cut -c1-200)

SUMMARY="Auto-captured (${SOURCE_REASON}, ${MSG_COUNT} messages). Topic: ${FIRST_PROMPT}"

PAYLOAD=$(jq -n \\
  --arg summary "$SUMMARY" \\
  --arg project "$PROJECT" \\
  --arg source "claude-code-hook" \\
  '{summary: $summary, project: $project, source: $source, decisions: [], blockers: []}')

# Dry-run: print what would be POSTed and write nothing. Lets you verify the
# hook wiring (reasons, triviality gate, payload shape) without touching the
# API or persisting a memory. PALINODE_HOOK_DRYRUN=1 to enable.
if [ "${PALINODE_HOOK_DRYRUN:-0}" = "1" ]; then
  echo "[palinode-session-end DRYRUN] would POST ${PALINODE_API}/session-end"
  echo "$PAYLOAD"
  exit 0
fi

# POST the capture. `-f` makes curl fail on HTTP >=400 too (not just connection
# errors), so a 5xx also routes to the fallback below. On ANY failure, never
# lose the capture — append the payload to a local fallback log a later session
# can replay. Always exit 0: a floor-capture failure must not block session exit.
if ! curl -sS -o /dev/null -f \\
    -X POST "${PALINODE_API}/session-end" \\
    -H "Content-Type: application/json" \\
    -d "$PAYLOAD" \\
    --connect-timeout 5 \\
    --max-time "${HOOK_TIMEOUT}"; then
  FALLBACK="${CLAUDE_PROJECT_DIR:-$CWD}/.claude/session-floor-fallback.jsonl"
  mkdir -p "$(dirname "$FALLBACK")" 2>/dev/null || true
  printf '%s\\n' "$PAYLOAD" >> "$FALLBACK" 2>/dev/null || true
fi

exit 0
"""


SAVE_COMMAND_BODY = """\
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


PS_COMMAND_BODY = """\
---
description: "DEPRECATED — use /save instead. /ps remains for back-compat."
---

> **DEPRECATED:** `/ps` is the legacy name for this command. Use `/save`
> instead — it is identical and is now the canonical mid-session checkpoint.
> `/ps` continues to work exactly as before; no action required on existing
> installs.

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
description: Wrap up this session — sync prior work, then a structured session_end that commits AND pushes the note, before /clear.
---

**Step 1 — Push prior work (before archiving).**
Call `palinode_push` to sync any commits already on the branch to the remote
before the session is archived — a session end is a natural sync point; don't
strand local commits, and prior work stays safe even if the archive step is
interrupted (#353). If the push succeeds, continue. If it fails because there
is no remote configured, print: `(no remote configured — skipping push)` and
continue. If it fails for any other reason (conflict, auth, network), print the
error and ask the user whether to proceed or abort.

**Step 2 — Archive the session AND ship the note (one call).**
Call `palinode_session_end` with `push: true` and:
- `summary` — 1-2 sentences on what was accomplished this session
- `decisions` — array of key decisions made, each with its rationale (the
  *why*, not just the *what*)
- `blockers` — array of open questions, unfinished work, or next steps the
  next session needs to pick up
- `project` — the project slug from `.claude/CLAUDE.md` (or the directory
  name if no slug is set)

This writes and commits the daily note, the project status line, and an
individual indexed memory file, then — because of `push: true` — pushes the
memory repo so the note actually reaches the remote (#378). Without `push: true`
the note only pushes when `config.git.auto_push` is on (default: off), which is
how the final session before a gap used to end up stranded. Do not save as a
ProjectSnapshot first — this command is exclusively for structured wrap-ups.
The push is repo-wide, so it also ships anything Step 1 didn't.

Read the result's `pushed` field. If `pushed` is true, print exactly:
`✓ session saved + pushed — safe to /clear now.` If `pushed` is false (no remote,
or the push failed), print: `✓ session saved — note committed locally but NOT
pushed; run palinode_push when the remote is reachable.` In both cases follow
with the daily-note path from the result.

**This command is deterministic.** `palinode_push` → `palinode_session_end`
(`push: true`). The note-ship is a property of the session_end call, not a
forgettable third step. For a quick mid-session checkpoint, use `/save` instead
(`/ps` also works as a back-compat alias).
"""


# Heavy `/wrap` variant (#419). Installed as the `/wrap` command/skill body
# only when `palinode init --wrap-policy heavy` is chosen. The light body
# above stays the default — heavy is opt-in per repo because it takes
# repo-mutating actions (merge, push) that must never be a surprise.
WRAP_HEAVY_COMMAND_BODY = """\
---
description: "Heavy wrap (wrap-policy: heavy) — merge, push, triage dangling items, then structured session_end. Halts on any failure."
---

**This repo runs the heavy `/wrap` (`wrap-policy: heavy`).** Unlike the light
variant, `/wrap` here lands the session's work before archiving: it merges,
pushes, triages dangling items, and only then records the session. Run the
four steps **in order**. Any failure **halts the sequence** — print why and
stop; do not silently skip ahead.

**Step 1 — Merge.**
First check whether this is a GitHub repo. If `gh pr list` errors with
*"none of the git remotes … point to a known GitHub host"* (a Gitea / GitLab /
self-hosted remote), there are no GitHub PRs to merge — **skip this step and
proceed to Step 2.** That is a graceful skip, **not** a halt. (Merging/filing
on a non-GitHub host uses that host's own CLI/API — e.g. `tea` for Gitea — not
`gh`.) Only when `gh` *can* enumerate PRs:
- If exactly one PR is open and its CI is green and review is satisfied:
  squash-merge it with a sensible message (subject line summarising the
  change, body referencing the issue). For `main`-eligible solo-dev repos a
  squash-merge is fine.
- If multiple PRs are open: **list them and stop** unless the user passed
  `--all` to this command.
- If a *real* merge blocker exists (merge conflict, CI not green, review
  pending): **halt.** Print the blocking reason and do not continue to Step 2.
  The operator decides. (A `gh`-can't-see-this-host error is **not** a
  blocker — it's the skip case above.)

**Step 2 — Push.**
This step pushes **all** unpushed commits on the branch — commits stack, so it
is all-or-nothing, not selective. **Assumption: everything already committed is
ready to push.**
- First **list** what would push — `git log @{u}..HEAD --oneline` and any
  non-merged feature branches with follow-up work. If any commit looks
  not-ready (committed but not meant to ship yet), this is a **stop-and-ask**,
  not a blind push — surface it and let the operator decide.
- Otherwise `git push` those commits. **Never force-push by default.**
- If a push fails (non-fast-forward, branch protection, auth, network):
  **halt.** Print the error and do not continue to Step 3.

**Step 3 — Triage dangling items.**
Route everything this session flagged-but-didn't-act-on into the
four-destination hierarchy (papercut / INBOX / GH issue / Palinode) defined in
the workspace `CLAUDE.md`.
- Scan the session for items the agent marked but deferred ("worth a
  papercut", "file this", "separate concern", "TODO").
- Run the `triage` skill in **dry-run**, present its recommendations, and get
  one-shot OK before applying anything. **If routing is uncertain, ask — do
  not guess.**
- papercut / INBOX items: append to the matching concern doc (honour
  "append before create" — never spawn a new file when an existing doc fits).
- Issue-tracker items: draft the body; for solo-dev iteration repos you may
  auto-file with a sensible label. Use the host's own tool — `gh` for GitHub,
  `tea` / the Gitea API for a Gitea remote (don't assume `gh`).
- `Decision` / `Insight` items: save directly via `palinode_save`.

**Step 4 — Archive the session (LAST).**
Call `palinode_session_end` with `push: true` and `summary`, `decisions`,
`blockers`, and `project` (the slug from `.claude/CLAUDE.md`). Fired last so the
record captures the post-merge SHAs, the freshly-filed issue numbers, and the
papercut/INBOX updates — reference *what the wrap did* (merged #X, pushed Y,
filed #Z, appended N items), not just the work. `push: true` ships the note in
the same call — the note is committed *after* Step 2's push, so without it the
session record would sit unpushed despite a "heavy" wrap (#378).
- If Palinode is unreachable: **continue** — print a warning and emit a stub
  markdown block the operator can save manually later. Ending without a
  Palinode record is acceptable; silently skipping with no warning is not.

After all steps, print: `✓ heavy wrap complete — safe to /clear now.` and the
daily-note path (or the stub path if Palinode was down).

**This command is deterministic in sequence** (merge → push → triage →
session_end) **but halts loudly on any failure.** For a repo that should not
auto-merge, use the light `/wrap` instead (scaffold with the default
`--wrap-policy light`).
"""


SETTINGS_HOOK_BLOCK = {
    "hooks": {
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/palinode-session-end.sh",
                        "timeout": 35,
                    }
                ]
            }
        ]
    }
}


MCP_JSON_BLOCK = {
    "_warning": (
        "This is a project-local MCP config. "
        "Your client may also read a global config at ~/.claude.json or "
        "~/Library/Application Support/Claude/ (macOS). "
        "Run 'palinode mcp-config --diagnose' to see all of them."
    ),
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


def _write_claude_md(
    path: Path, project_slug: str, force: bool, wrap_policy: str = "light"
) -> str:
    wrap_policy_note = WRAP_POLICY_HEAVY_NOTE if wrap_policy == "heavy" else ""
    block = CLAUDE_MD_BLOCK.format(
        project_slug=project_slug, wrap_policy_note=wrap_policy_note
    )
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


def _skill_md(name: str, body: str) -> str:
    """Render a slash-command body as a Claude Code SKILL.md (#474).

    A skill needs a ``name:`` in its frontmatter; the command bodies open with
    ``---\\ndescription: …\\n---``. Inject ``name:`` so the same ``*_COMMAND_BODY``
    constant is the single source for both the legacy command and the skill —
    they can't drift.
    """
    if body.startswith("---\n"):
        return "---\nname: " + name + "\n" + body[len("---\n"):]
    return f"---\nname: {name}\ndescription: {name} (Palinode)\n---\n\n{body}"


def _write_skill(skills_root: Path, name: str, body: str, force: bool) -> str:
    """Write ``<skills_root>/<name>/SKILL.md`` (project or personal scope)."""
    path = skills_root / name / "SKILL.md"
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(_skill_md(name, body))
    return "created"


# ---------------------------------------------------------------------------
# Obsidian scaffold templates
# ---------------------------------------------------------------------------

# app.json
# Fields kept to the minimum that Obsidian needs on first open.
# - alwaysUpdateLinks / trashOption: safe file-recovery defaults
# - newFileFolderPath: new notes land in daily/ by default
# - useMarkdownLinks: false → Obsidian uses [[wikilinks]] (the default, but
#   explicit so the intent survives a settings reset)
# - newFileLocation: "folder" → honour newFileFolderPath
OBSIDIAN_APP_JSON: dict = {
    "alwaysUpdateLinks": True,
    "trashOption": "local",
    "newFileLocation": "folder",
    "newFileFolderPath": "daily",
    "useMarkdownLinks": False,
}

# graph.json
# Obsidian graph config is a flat JSON object.  Fields confirmed from the
# Obsidian desktop app's exported graph.json format (v1.x):
#   - colorGroups: list of {query, color:{r,g,b,a}}
#   - collapsedNodeGroups: list of query strings whose nodes are collapsed
#   - showTags, showAttachments, showOrphans: booleans
#   - scale, linksScalingFactor: physics tuning
# Node query syntax is Obsidian's native graph query language (same as
# search), e.g. "path:archive/" matches files under archive/.
OBSIDIAN_GRAPH_JSON: dict = {
    "colorGroups": [
        {"query": "path:people/",    "color": {"r": 74,  "g": 222, "b": 128, "a": 1}},
        {"query": "path:projects/",  "color": {"r": 96,  "g": 165, "b": 250, "a": 1}},
        {"query": "path:decisions/", "color": {"r": 251, "g": 146, "b": 60,  "a": 1}},
        {"query": "path:insights/",  "color": {"r": 192, "g": 132, "b": 252, "a": 1}},
    ],
    "collapsedNodeGroups": [
        "path:archive/",
        "path:logs/",
        "path:.palinode/",
    ],
    "showTags": False,
    "showAttachments": False,
    "showOrphans": True,
    "scale": 1.0,
    "linksScalingFactor": 1.0,
}

# workspace.json
# Obsidian owns this file after launch — the user should never need to
# hand-edit it.  We set a minimal structure so Obsidian opens without
# complaining about a malformed workspace.
# NOTE: --force-obsidian deliberately skips this file (it's Obsidian-owned
# post-launch).  The skip is implemented in _write_obsidian_scaffold().
OBSIDIAN_WORKSPACE_JSON: dict = {
    "main": {
        "id": "main",
        "type": "split",
        "children": [
            {
                "id": "leaf",
                "type": "leaf",
                "state": {
                    "type": "file-explorer",
                    "state": {"sortOrder": "alphabetical"},
                },
            }
        ],
        "direction": "vertical",
    },
    "left": {
        "id": "left",
        "type": "split",
        "children": [
            {
                "id": "left-leaf",
                "type": "leaf",
                "state": {
                    "type": "file-explorer",
                    "state": {"sortOrder": "alphabetical"},
                },
            }
        ],
        "direction": "vertical",
        "width": 280,
    },
    "right": {"id": "right", "type": "split", "children": [], "direction": "vertical"},
    "active": "leaf",
    "lastOpenFiles": ["daily"],
}

# _index.md  — starter MOC at vault root
OBSIDIAN_INDEX_MD = """\
# Index

This vault is managed by [Palinode](https://github.com/phasespace-labs/palinode) —
a persistent memory system for AI agents. Markdown files here are the source of
truth; Obsidian is a read/write UI on top of them.

## Categories

- [[people/_index|People]] — contacts and collaborators
- [[projects/_index|Projects]] — active and archived projects
- [[decisions/_index|Decisions]] — architectural and design decisions
- [[insights/_index|Insights]] — reusable findings and lessons
- [[research/_index|Research]] — background notes and references
- [[daily/_index|Daily]] — session notes and daily logs
- [[archive/_index|Archive]] — superseded content

## Getting started

Run `palinode --help` from your terminal for all available commands.

Check that the MCP server is reachable:

```
palinode mcp-config --diagnose
```

Save a new memory from the terminal:

```
palinode save "Your insight here"
```

Or use `palinode_save` from any connected AI agent (Claude Code, Cursor, etc).
"""

# _README.md  — vault orientation for cold openers
OBSIDIAN_README_MD = """\
# Palinode Vault

This directory is a **Palinode memory vault** opened in Obsidian.

Palinode is a persistent long-term memory system for AI agents. It stores
memories as git-versioned markdown files with hybrid (semantic + keyword)
search. Obsidian is the human-facing UI — browse, edit, and link memories
visually while your AI agents read and write through the CLI or MCP server.

## First steps

1. Make sure `palinode-api` is running (`palinode start`, or via systemd).
2. Open `_index.md` for a map of all memory categories.
3. Run `palinode mcp-config --diagnose` to confirm MCP connectivity.
4. Run `palinode --help` for all available commands.

## Directory structure

| Directory     | Contents                                      |
|---------------|-----------------------------------------------|
| `daily/`      | Session notes and daily logs (auto-created)   |
| `people/`     | Contacts, collaborators, entities             |
| `projects/`   | Active and archived project notes             |
| `decisions/`  | Architectural and design decision records     |
| `insights/`   | Reusable findings and lessons                 |
| `research/`   | Background notes and references               |
| `archive/`    | Superseded or historical content              |
| `.palinode/`  | Internal index state — do not edit            |

## Notes

- Wikilinks (`[[like this]]`) are first-class — Palinode reads and writes them.
- Do not edit files under `.palinode/` — that directory is managed by the daemon.
- The graph view collapses `archive/`, `logs/`, and `.palinode/` by default.
- Re-run `palinode init --obsidian <vault-path>` to restore scaffolded files
  if they are accidentally deleted (user-edited files are preserved).
"""


def _write_json_file(path: Path, data: dict, force: bool) -> str:
    """Write a JSON file; skip if exists and not forced."""
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return "created"


def _write_text_file(path: Path, content: str, force: bool) -> str:
    """Write a text/markdown file; skip if exists and not forced."""
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(content)
    return "created"


def _write_obsidian_scaffold(
    target: Path,
    force: bool,
    force_obsidian: bool,
) -> list[tuple[str, str]]:
    """Write all Obsidian scaffold files into *target*.

    Returns a list of (label, status) pairs suitable for the output table.

    Idempotency rules:
      - ``force=False, force_obsidian=False`` — skip any file that already exists
      - ``force_obsidian=True`` — overwrite all scaffold files EXCEPT
        ``.obsidian/workspace.json`` (Obsidian owns that post-launch)
      - ``force=True`` — same behaviour as ``force_obsidian=True`` for Obsidian
        files (the global --force applies everywhere)
    """
    obsidian_force = force or force_obsidian
    # workspace.json is excluded from force-overwrite — Obsidian owns it
    workspace_force = force  # only overwrite on global --force, not --force-obsidian

    obsidian_dir = target / ".obsidian"
    results: list[tuple[str, str]] = []

    # Create the standard memory category directories so the Obsidian graph
    # has seed nodes to render and Obsidian's file tree isn't empty.
    # A .gitkeep is placed in each so git tracks empty dirs.
    _VAULT_DIRS = (
        "people", "projects", "decisions", "insights",
        "research", "daily", "archive", "logs",
    )
    for dir_name in _VAULT_DIRS:
        d = target / dir_name
        d.mkdir(exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("")
            results.append((dir_name + "/", "created"))
        else:
            results.append((dir_name + "/", "skipped"))

    results.append((
        ".obsidian/app.json",
        _write_json_file(obsidian_dir / "app.json", OBSIDIAN_APP_JSON, obsidian_force),
    ))
    results.append((
        ".obsidian/graph.json",
        _write_json_file(obsidian_dir / "graph.json", OBSIDIAN_GRAPH_JSON, obsidian_force),
    ))
    results.append((
        ".obsidian/workspace.json",
        _write_json_file(obsidian_dir / "workspace.json", OBSIDIAN_WORKSPACE_JSON, workspace_force),
    ))
    results.append((
        "_index.md",
        _write_text_file(target / "_index.md", OBSIDIAN_INDEX_MD, obsidian_force),
    ))
    results.append((
        "_README.md",
        _write_text_file(target / "_README.md", OBSIDIAN_README_MD, obsidian_force),
    ))
    return results


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
    help="Install /save, /ps (back-compat alias), and /wrap slash commands for save-before-clear reflex",
)
@click.option(
    "--wrap-policy",
    type=click.Choice(["light", "heavy"]),
    default="light",
    help=(
        "Which /wrap variant to scaffold (#419). 'light' (default): /wrap just "
        "pushes + session_end. 'heavy': /wrap also merges, pushes, and triages "
        "dangling items before archiving — opt-in per repo because it mutates "
        "the repo (merge/push)."
    ),
)
@click.option(
    "--skills",
    type=click.Choice(["none", "project", "personal", "both"]),
    default="none",
    help=(
        "Also install /save /ps /wrap as Claude Code *skills* — the modern "
        "format (user-scope `.claude/commands/` is no longer searched). "
        "'personal' → ~/.claude/skills/ so /wrap is typeable in ALL projects "
        "(not just this one); 'project' → .claude/skills/; 'both'. Bodies come "
        "from the same source as the slash commands, so they can't drift. "
        "Default: none. (#474)"
    ),
)
@click.option(
    "--obsidian/--no-obsidian",
    default=False,
    help=(
        "Scaffold an opinionated Obsidian vault config alongside the standard "
        "palinode files (.obsidian/, _index.md, _README.md). Default: off."
    ),
)
@click.option(
    "--force-obsidian",
    is_flag=True,
    default=False,
    help=(
        "Overwrite scaffolded Obsidian files even if they exist (excluding "
        ".obsidian/workspace.json which Obsidian owns post-launch). "
        "Implies --obsidian."
    ),
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
def init(
    target_dir,
    project_slug,
    mcp,
    claudemd,
    hook,
    slash,
    wrap_policy,
    skills,
    obsidian,
    force_obsidian,
    force,
    dry_run,
):
    """Scaffold Palinode into a project for zero-friction adoption.

    Creates (or appends to):
      .claude/CLAUDE.md                     — memory instructions for the agent
      .claude/settings.json                 — SessionEnd hook registration
      .claude/hooks/palinode-session-end.sh — hook script (fires on /clear, exit)
      .mcp.json                             — palinode MCP server block

    With --obsidian, additionally writes:
      .obsidian/app.json       — wikilinks, daily/ as default file location
      .obsidian/graph.json     — pre-tuned graph (collapsed dirs, color groups)
      .obsidian/workspace.json — sidebar opens on daily/ by default
      _index.md                — starter MOC linking all category dirs
      _README.md               — vault orientation for first-time openers

    Re-run with --force to overwrite. --dry-run shows the plan without writing.
    --force-obsidian overwrites the Obsidian scaffold only (preserving workspace.json).
    """
    target = Path(target_dir).resolve()
    if not target.exists():
        raise click.ClickException(f"Directory not found: {target}")

    slug = project_slug or _slugify(target.name)

    # --force-obsidian implies --obsidian
    if force_obsidian:
        obsidian = True

    claude_md = target / ".claude" / "CLAUDE.md"
    settings = target / ".claude" / "settings.json"
    hook_script = target / ".claude" / "hooks" / "palinode-session-end.sh"
    mcp_json = target / ".mcp.json"
    save_cmd = target / ".claude" / "commands" / "save.md"
    ps_cmd = target / ".claude" / "commands" / "ps.md"
    wrap_cmd = target / ".claude" / "commands" / "wrap.md"

    # #474: optional skill-format install. Same bodies as the slash commands
    # (single source — no drift); 'personal' scope makes /wrap typeable in every
    # project, not just this one.
    skill_specs = [
        ("save", SAVE_COMMAND_BODY),
        ("ps", PS_COMMAND_BODY),
        ("wrap", WRAP_HEAVY_COMMAND_BODY if wrap_policy == "heavy" else WRAP_COMMAND_BODY),
    ]
    skill_roots: list[tuple[str, Path]] = []
    if skills in ("project", "both"):
        skill_roots.append(("project", target / ".claude" / "skills"))
    if skills in ("personal", "both"):
        skill_roots.append(("personal", Path.home() / ".claude" / "skills"))

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
            click.echo(f"  {save_cmd.relative_to(target)}  (/save slash command — canonical)")
            click.echo(f"  {ps_cmd.relative_to(target)}  (/ps slash command — back-compat alias)")
            click.echo(
                f"  {wrap_cmd.relative_to(target)}  (/wrap slash command — {wrap_policy} policy)"
            )
        for scope_label, root in skill_roots:
            for name, _ in skill_specs:
                click.echo(f"  {root / name / 'SKILL.md'}  (/{name} skill — {scope_label} scope)")
        if mcp:
            click.echo(f"  {mcp_json.relative_to(target)}  (MCP server block)")
        if obsidian:
            click.echo(f"  .obsidian/app.json  (Obsidian app config)")
            click.echo(f"  .obsidian/graph.json  (graph view settings)")
            click.echo(f"  .obsidian/workspace.json  (workspace layout)")
            click.echo(f"  _index.md  (MOC at vault root)")
            click.echo(f"  _README.md  (vault orientation)")
        return

    results = []
    if claudemd:
        results.append(
            ("CLAUDE.md", _write_claude_md(claude_md, slug, force, wrap_policy))
        )
    if hook:
        results.append(("hook script", _write_hook_script(hook_script, force)))
        results.append(("settings.json", _merge_settings(settings, force)))
    if slash:
        results.append(("/save command", _write_slash_command(save_cmd, SAVE_COMMAND_BODY, force)))
        results.append(("/ps command (alias)", _write_slash_command(ps_cmd, PS_COMMAND_BODY, force)))
        wrap_body = WRAP_HEAVY_COMMAND_BODY if wrap_policy == "heavy" else WRAP_COMMAND_BODY
        results.append(
            (f"/wrap command ({wrap_policy})", _write_slash_command(wrap_cmd, wrap_body, force))
        )
    for scope_label, root in skill_roots:
        for name, body in skill_specs:
            results.append(
                (f"/{name} skill ({scope_label})", _write_skill(root, name, body, force))
            )
    if mcp:
        results.append((".mcp.json", _merge_mcp_json(mcp_json, force)))
    if obsidian:
        results.extend(_write_obsidian_scaffold(target, force, force_obsidian))

    for label, status in results:
        mark = "✓" if status in ("created", "appended", "merged") else "·"
        click.echo(f"  {mark} {label}: {status}")

    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Make sure palinode-api is running (palinode start, or systemd)")
    if obsidian:
        click.echo("  2. Open the vault in Obsidian: open -a Obsidian " + str(target))
        click.echo("  3. Try it:  \"search palinode for recent decisions on this project\"")
    else:
        click.echo("  2. Open the project in Claude Code — the MCP server will connect on start")
        click.echo("  3. Try it:  \"search palinode for recent decisions on this project\"")
