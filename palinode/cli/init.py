"""`palinode init` — scaffold Palinode into a project for zero-friction adoption.

Creates:
  - .claude/CLAUDE.md  (memory section, appended if file exists)
  - .claude/settings.json  (SessionStart + SessionEnd + UserPromptSubmit + PostCompact hook registration)
  - .claude/hooks/palinode-session-start.sh  (core-memory inject + context prime)
  - .claude/hooks/palinode-session-end.sh  (/clear auto-capture)
  - .claude/hooks/palinode-user-prompt-submit.sh  (per-turn implicit recall — triggers + strict search)
  - .mcp.json  (MCP server block for palinode, if --mcp given)
  - .cursor/hooks.json + .cursor/hooks/  (Cursor session-start and per-prompt
    recall, only with --cursor-hooks; existing entries are kept)
  - .agents/hooks.json + .agents/hooks/  (Antigravity session-start and
    per-prompt recall, only with --agy-hooks; existing hooks are kept)

With --codex-hook, additionally writes the Codex hooks (the same scripts):
  - .codex/hooks/palinode-session-start.sh, .codex/hooks/palinode-user-prompt-submit.sh
  - .codex/hooks/palinode-session-end.sh  (SessionEnd floor: topic + last reply)
  - .codex/hooks.json  (merged after any hooks already registered there)

Also provisions, in the *memory store* rather than the project:
  - $PALINODE_DIR/specs/prompts/*.md  (the consolidation prompts, from the
    copies packaged with palinode — an install had none of them before, so a
    fresh store could not consolidate at all)

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
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import click

from palinode.hooks import script as _hook_script
from palinode.hooks.agy import ANTIGRAVITY
from palinode.hooks.claude import CLAUDE
from palinode.hooks.codex import CODEX, TRUST_NOTE as CODEX_TRUST_NOTE
from palinode.hooks.cursor import CURSOR
from palinode.hooks.installed import repair_registrations


# ADR-012 Layer 1 (instruction file): ONE harness-neutral memory block,
# inherited by every surface that reads an instruction file. The shared pieces
# below are the single source of truth — per-surface variants may only vary
# the session-end section (Claude Code has /clear, /wrap, and the SessionEnd
# hook; other harnesses don't). Do not fork the shared text per surface.
_MEMORY_BLOCK_TOP = """\
## Memory (Palinode)

This project uses Palinode for persistent memory via MCP (server name: `palinode`).

### At session start
- Call `palinode_search` with the current task or project name to pull prior context.
- If the MCP server is down, fall back to the CLI: `palinode search "<query>"`.
- Records from other projects come back only when you pass `include_other_projects`, and they are context only, never this project's decisions. Don't opt in to find a value this project's memory lacks; ask the user instead.

### During work
- When a value, ID, name, decision or plan is settled (for example a batch ID fixed or an approach agreed), save it right away with `palinode_save` as `type="Decision"`, with the value and why. Don't wait for the end of the session.
- After a milestone (tests pass, feature shipped, bug root-caused), call
  `palinode_save` with the outcome. Include *why*, not just *what*.
- When making an architectural or design decision, save the decision AND the
  rationale as `type="Decision"`.
- Save surprising reusable findings as `type="Insight"`.
- After roughly 30 minutes, save a progress note only if new durable progress
  has accumulated. Skip timer-based writes during recall-only work.
- `palinode_save` and `palinode_session_end` are explicit, content-bearing
  writes. They are separate from any opt-in client hook and from background
  auto-summary enrichment.

"""

_MEMORY_BLOCK_TAIL = """\
### What NOT to save
- Raw code (git handles that).
- Step-by-step debug logs — save the resolution, not the journey.
- Trivial changes ("fixed typo" is not worth a memory).

### Project slug
This project's slug is `{project_slug}`. Pass it as the `project` argument to
`palinode_save` and `palinode_session_end` so status rolls up correctly.
"""

_CLAUDE_SESSION_END = """\
### At session end — including `/clear`
- At a user-requested wrap-up, or when the session produced new durable
  information, call `palinode_session_end` with `summary`, `decisions`,
  `blockers`, and `project="{project_slug}"`.
- Do not create a recap for a recall-only or no-new-information session, and
  honor an explicit request not to save.
- `/clear` by itself does not override that choice. The SessionEnd hook
  installed by `palinode init --hook` is an opt-in automatic fallback;
  plain `palinode init` does not enable transcript capture.
- The user may type `/wrap` (session wrap-up) as a shortcut. It is
  an explicit request to capture: perform its deterministic
  `palinode_session_end` with summary/decisions/blockers before `/clear`,
  unless the user separately says not to save. See the `/wrap` command/skill
  definition (installed by `palinode init`) for the exact prompt.
- Mid-session checkpoints go through the `palinode_save` tool directly
  (`type="ProjectSnapshot"`); there is no separate slash command for them.
  (`/save` and `/ps` are deprecated — existing installs keep working.)

"""

_NEUTRAL_SESSION_END = """\
### At session end
- At a user-requested wrap-up, or when the session produced new durable
  information, call `palinode_session_end` with `summary`, `decisions`,
  `blockers`, and `project="{project_slug}"`.
- Do not create a recap for a recall-only or no-new-information session, and
  honor an explicit request not to save. Explicit tool writes remain separate
  from opt-in client hooks and background auto-summary enrichment.

"""

# The Claude Code rendering: shared core + Claude-only session-end machinery.
# Byte-identical to the pre-split monolithic block (regression-pinned by
# tests/fixtures/claude_md_memory_block_*.txt).
CLAUDE_MD_BLOCK = (
    _MEMORY_BLOCK_TOP + _CLAUDE_SESSION_END + _MEMORY_BLOCK_TAIL + "{wrap_policy_note}"
)

#: Harness-neutral rendering — what AGENTS.md (Antigravity/Codex) and
#: .cursor/rules/ (Cursor) receive: the same recall / save-with-rationale /
#: session-end contract, none of the Claude-Code-only machinery.
MEMORY_BLOCK_CORE = _MEMORY_BLOCK_TOP + _NEUTRAL_SESSION_END + _MEMORY_BLOCK_TAIL
MEMORY_BLOCK_END = "<!-- palinode:memory:end -->\n"


#: Disclosure printed when a run leaves BOTH a CLAUDE.md-family instruction
#: file and an AGENTS.md carrying the memory block. The two are not one file
#: read by everyone: Claude Code's documented default is to read its CLAUDE.md
#: files *instead of* AGENTS.md whenever a CLAUDE.md, .claude/CLAUDE.md or
#: CLAUDE.local.md exists in the working directory or above it
#: (<https://code.claude.com/docs/en/memory#agents-md>, reading AGENTS.md
#: directly requires Claude Code v2.1.277+), while Codex and other
#: AGENTS.md-aware harnesses read AGENTS.md and not CLAUDE.md. `init` keeps
#: writing both — this only says so out loud, because the failure it prevents
#: is editing one copy and believing every harness saw the change.
INSTRUCTION_FILE_DISCLOSURE = """\
  Two instruction files, two audiences — they are separate copies:
    .claude/CLAUDE.md  — read by Claude Code. By default Claude Code reads its
      CLAUDE.md files INSTEAD OF AGENTS.md whenever a CLAUDE.md, .claude/CLAUDE.md
      or CLAUDE.local.md exists in the working directory or above it.
    AGENTS.md          — read by Codex and other AGENTS.md-aware harnesses,
      which do not read CLAUDE.md.
    Maintain both: editing one block does not change the other. Without --force,
      `palinode init` does not reconcile drift between them; with --force, it
      replaces each file's Palinode block independently.
    Reference: https://code.claude.com/docs/en/memory#agents-md\
"""


#: Canonical detector for "an instruction file already has the Palinode
#: memory block". The ONE definition shared by `palinode init` (create-vs-
#: append idempotency below), the `claude_md_palinode_block` doctor check
#: (what gets reported), and its `--fix` (what gets repaired) — so the three
#: can never again disagree about what "already wired up" means. Checks both
#: heading levels so a hand-rolled top-level "# Memory (Palinode)" heading is
#: also recognised, not only the "##" level this module writes.
MEMORY_BLOCK_MARKERS = ("## Memory (Palinode)", "# Memory (Palinode)")


def has_memory_block(content: str) -> bool:
    """True if *content* already carries a Palinode memory block heading.

    Deliberately NOT a substring match on "palinode" anywhere in the file —
    a CLAUDE.md that merely mentions the project in prose has not been wired
    up; only the section heading counts as the block being present.
    """
    return any(marker in content for marker in MEMORY_BLOCK_MARKERS)


# Appended to the CLAUDE.md memory block only when `--wrap-policy heavy` is
# chosen. This is the inspectable record of which `/wrap` variant the
# repo runs — the behaviour itself lives in the installed `/wrap` command/skill
# body (rendered from WRAP_HEAVY_COMMAND_BODY).
WRAP_POLICY_HEAVY_NOTE = """
### Wrap policy
`wrap-policy: heavy` — `/wrap` in this repo runs the heavy sequence (merge →
push → triage dangling items → `palinode_session_end`), halting loudly on any
failure. See the installed `/wrap` command/skill for the exact contract.
"""


WRAP_COMMAND_BODY = """\
---
description: Wrap up this session — offer to land any open git work, sync prior memory work, then a structured session_end that commits AND pushes the note, before /clear.
---

**Step 0 — Pre-flight: offer to land the working repo's git work.**
Before archiving, glance at the *working repo's* git state — the code repo you
were editing, not the Palinode memory repo (Steps 1–2 handle that). This is a
light courtesy check, **not** the heavy wrap's halt-on-failure merge sequence:
you **offer** to close things out, you never commit, merge, or push without an
explicit yes in this session.
- Run `git status --short`. If on a feature branch, also check whether it is
  ahead of `main` — e.g. `git log --oneline main..HEAD`.
- **Clean tree and not ahead of `main`?** Say nothing and go straight to
  Step 1 — there is nothing to land.
- **Dirty tree or unmerged branch?** State it in **one line** (e.g. `3
  uncommitted files; branch feat/x is 2 commits ahead of main`) and **offer**
  to close it out first: commit the changes and/or ff-only merge the branch
  back to `main`. Push only if the user explicitly asks, and honor the repo's
  push policy (some repos forbid direct pushes — don't assume one is wanted).
- If the user declines, or says something like "just wrap" / "leave it",
  proceed to Step 1 and archive anyway — a dirty tree is theirs to keep. The
  offer is a courtesy, never a gate.

**Step 1 — Push prior work (before archiving).**
Call `palinode_push` to sync any commits already on the branch to the remote
before the session is archived — a session end is a natural sync point; don't
strand local commits, and prior work stays safe even if the archive step is
interrupted. If the push succeeds, continue. If it fails because there
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

Two things across all three fields, because a later reader cannot repair either:
write **absolute dates**, resolving "yesterday" / "last week" against today's
date; and when something was chosen from a set of options, name **every option
considered**, not just the one that won. A note that says "picked the second
approach" is unreadable once the list is gone.

This writes and commits the daily note, the project status line, and an
individual indexed memory file, then — because of `push: true` — pushes the
memory repo so the note actually reaches the remote. Without `push: true`
the note only pushes when `config.git.auto_push` is on (default: off), which is
how the final session before a gap used to end up stranded. Do not save as a
ProjectSnapshot first — this command is exclusively for structured wrap-ups.
The push is repo-wide, so it also ships anything Step 1 didn't.

Read the result's `pushed` field. If `pushed` is true, print exactly:
`✓ session saved + pushed — safe to /clear now.` If `pushed` is false (no remote,
or the push failed), print: `✓ session saved — note committed locally but NOT
pushed; run palinode_push when the remote is reachable.` In both cases follow
with the daily-note path from the result.

**This command is deterministic.** The archive path is `palinode_push` →
`palinode_session_end` (`push: true`); the note-ship is a property of the
session_end call, not a forgettable third step. Step 0's git offer never blocks
that path and never acts without your explicit yes — decline it and the wrap
proceeds unchanged. For a quick mid-session checkpoint, call the `palinode_save`
tool directly with `type="ProjectSnapshot"`.
"""


# Heavy `/wrap` variant. Installed as the `/wrap` command/skill body
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
`blockers`, and `project` (the slug from `.claude/CLAUDE.md`). Write absolute
dates rather than relative ones, and when a decision chose between options,
name every option considered — neither is recoverable later. Fired last so the
record captures the post-merge SHAs, the freshly-filed issue numbers, and the
papercut/INBOX updates — reference *what the wrap did* (merged #X, pushed Y,
filed #Z, appended N items), not just the work. `push: true` ships the note in
the same call — the note is committed *after* Step 2's push, so without it the
session record would sit unpushed despite a "heavy" wrap.
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


# Allow-rules so agents can reclaim their own stale worktrees without hitting
# the auto-mode permission classifier. `git worktree remove/unlock/prune` only
# ever touch the working directory — branches + commits are preserved — so they
# are safe to pre-approve in scaffolded projects.
WORKTREE_ALLOW_RULES = [
    "Bash(git worktree remove:*)",
    "Bash(git worktree prune:*)",
    "Bash(git worktree unlock:*)",
]


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


@dataclass(frozen=True)
class PlannedWrite:
    """One filesystem mutation `palinode init` may perform.

    `build_plan()` produces the single, complete list of these. `--dry-run`
    prints `label`/`path`/`payload` for every entry without ever calling
    `writer`; the real run calls `writer()` for every entry and prints
    `label` + the returned status. Both walk the SAME list, so the plan and
    the write-set are the same object — dry-run cannot under- or
    over-report relative to what actually happens.
    """

    label: str
    path: Path
    payload: str
    writer: Callable[[], str]


def _slugify(name: str) -> str:
    """Turn a directory name into a safe project slug."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "project"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


_MEMORY_HEADING = re.compile(r"(?m)^#{1,2} Memory \(Palinode\)[ \t]*(?:\r?\n|$)")
_LEGACY_END = "`palinode_save` and `palinode_session_end` so status rolls up correctly."
_LEGACY_HEAVY_END = "failure. See the installed `/wrap` command/skill for the exact contract."


def _memory_block_spans(content: str, path: Path) -> list[tuple[int, int]]:
    """Find complete generated blocks; refuse an edited legacy tail."""
    starts = list(_MEMORY_HEADING.finditer(content))
    headings = list(re.finditer(r"(?m)^#{1,2} Memory \(Palinode\)", content))
    if len(starts) != len(headings):
        raise click.ClickException(
            f"Cannot determine the Palinode block boundaries in {path}. "
            "Edit the file by hand: remove the incomplete block, then re-run palinode init."
        )
    spans = []
    for index, start in enumerate(starts):
        limit = starts[index + 1].start() if index + 1 < len(starts) else len(content)
        closes = list(re.finditer(r"(?m)^<!-- palinode:memory:end -->\r?\n?", content[start.end():limit]))
        if len(closes) > 1:
            raise click.ClickException(
                f"Cannot determine the end of the Palinode block in {path}. "
                "Edit the file by hand: remove the incomplete block, then re-run palinode init."
            )
        if closes:
            end = start.end() + closes[0].end()
        else:
            tail = content[start.end():limit]
            footers = list(re.finditer(r"(?m)^" + re.escape(_LEGACY_END) + r"\r?\n?", tail))
            if len(footers) != 1 or not re.search(r"(?m)^### Project slug[ \t]*\r?$", tail[:footers[0].start()]):
                raise click.ClickException(
                    f"Cannot determine the end of the Palinode block in {path}. "
                    "Edit the file by hand: remove the incomplete block, then re-run palinode init."
                )
            end = start.end() + footers[0].end()
            remainder = content[end:limit]
            if re.search(r"(?m)^### (?!Wrap policy[ \t]*\r?$)", remainder):
                raise click.ClickException(
                    f"Cannot determine the end of the Palinode block in {path}. "
                    "Edit the file by hand: remove the incomplete block, then re-run palinode init."
                )
            if re.search(r"(?m)^### Wrap policy[ \t]*\r?$", remainder):
                heavy = re.search(re.escape(_LEGACY_HEAVY_END) + r"\r?\n?", remainder)
                if heavy is None:
                    raise click.ClickException(
                        f"Cannot determine the end of the Palinode block in {path}. "
                        "Edit the file by hand: remove the incomplete block, then re-run palinode init."
                    )
                end += heavy.end()
        if re.search(r"(?m)^#{1,2} ", content[start.end():end]):
            raise click.ClickException(
                f"Cannot determine the end of the Palinode block in {path}. "
                "Edit the file by hand: remove the incomplete block, then re-run palinode init."
            )
        spans.append((start.start(), end))
    return spans


def _memory_block_action(path: Path, force: bool) -> str:
    if not path.exists():
        return "would create"
    existing = path.read_bytes().decode("utf-8")
    if has_memory_block(existing):
        if force:
            if not _memory_block_spans(existing, path):
                raise click.ClickException(
                    f"Cannot determine the Palinode block in {path}. "
                    "Edit the file by hand: remove the incomplete block, then re-run palinode init."
                )
            return "would replace"
        return "would skip"
    return "would append"


def _write_memory_block(path: Path, block: str, force: bool) -> str:
    """Create, append, or replace a complete Palinode instruction block."""
    block += MEMORY_BLOCK_END
    _ensure_parent(path)
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return "created"
    existing = path.read_bytes().decode("utf-8")
    if has_memory_block(existing) and not force:
        return "skipped (already has Palinode section)"
    if has_memory_block(existing):
        spans = _memory_block_spans(existing, path)
        if not spans:
            raise click.ClickException(
                f"Cannot determine the Palinode block in {path}. "
                "Edit the file by hand: remove the incomplete block, then re-run palinode init."
            )
        updated = existing[:spans[0][0]] + block
        for (_, end), (next_start, _) in zip(spans, spans[1:], strict=False):
            updated += existing[end:next_start]
        updated += existing[spans[-1][1]:]
        path.write_bytes(updated.encode("utf-8"))
        return "replaced" if len(spans) == 1 else f"replaced ({len(spans)} blocks collapsed)"
    with path.open("a", encoding="utf-8") as f:
        if not existing.endswith("\n"):
            f.write("\n")
        f.write("\n" + block)
    return "appended"


def _write_claude_md(
    path: Path, project_slug: str, force: bool, wrap_policy: str = "light"
) -> str:
    wrap_policy_note = WRAP_POLICY_HEAVY_NOTE if wrap_policy == "heavy" else ""
    block = CLAUDE_MD_BLOCK.format(
        project_slug=project_slug, wrap_policy_note=wrap_policy_note
    )
    return _write_memory_block(path, block, force)


def _write_agents_md(path: Path, project_slug: str, force: bool) -> str:
    """Write the harness-neutral memory block to AGENTS.md (Antigravity/Codex)."""
    block = MEMORY_BLOCK_CORE.format(project_slug=project_slug)
    return _write_memory_block(path, block, force)


def _write_cursor_rules(path: Path, project_slug: str, force: bool) -> str:
    """Write the harness-neutral memory block to .cursor/rules/palinode.md."""
    block = MEMORY_BLOCK_CORE.format(project_slug=project_slug)
    return _write_memory_block(path, block, force)


def _write_hook_script(path: Path, force: bool, content: str) -> str:
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(content, encoding="utf-8")
    # chmod +x
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Record what was installed: the version stamp and content hash are what
    # `palinode doctor` (hooks_current) and `palinode hooks update` compare
    # against later, once this copy has had a chance to go stale.
    from palinode.hooks import content_sha256, hook_version
    from palinode.hooks.installed import describe_version

    return (
        f"created (hook version {describe_version(hook_version(content))}, "
        f"sha256 {content_sha256(content)[:12]})"
    )


def settings_block(project_dir: Path) -> dict:
    """The ``.claude/settings.json`` ``palinode init --hook`` creates."""
    return {
        "permissions": {"allow": list(WORKTREE_ALLOW_RULES)},
        **CLAUDE.registration_block(project_dir),
    }


def _merge_settings(path: Path, force: bool) -> str:
    """Register palinode's Claude Code hooks in a settings file, keeping every other entry."""
    _ensure_parent(path)
    project_dir = path.parent.parent
    if not path.exists():
        path.write_text(json.dumps(settings_block(project_dir), indent=2) + "\n", encoding="utf-8")
        return "created"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if not force:
            return "skipped (existing settings.json is not valid JSON — re-run with --force to overwrite)"
        existing = {}
    # Merge the worktree allow-rules idempotently, independent of the hook so a
    # re-run without the hook still tops them up.
    allow = existing.setdefault("permissions", {}).setdefault("allow", [])
    for rule in WORKTREE_ALLOW_RULES:
        if rule not in allow:
            allow.append(rule)

    merged_any = CLAUDE.add_missing(existing, Path.home(), project_dir)
    # Earlier installs registered PostCompact async (Claude Code cancels it) and
    # left ${CLAUDE_PROJECT_DIR} unquoted (split at a space); a re-run repairs both.
    if repair_registrations(CLAUDE, existing, project_dir):
        merged_any = True
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return "merged" if merged_any else "skipped (palinode hooks already registered)"


def _write_slash_command(path: Path, body: str, force: bool) -> str:
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(body, encoding="utf-8")
    return "created"


def _skill_md(name: str, body: str) -> str:
    """Render a slash-command body as a Claude Code SKILL.md.

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
    path.write_text(_skill_md(name, body), encoding="utf-8")
    return "created"


def _write_session_skill(skills_root: Path, force: bool) -> str:
    """Install the canonical palinode-session skill verbatim.

    A symlinked SKILL.md is never touched — even with ``--force``. A symlink
    means the install is curated externally (e.g. a dotfiles/agents repo owns
    the real file), and overwriting it would silently break that ownership. A
    regular existing file is skipped unless forced, so a user's customized
    skill survives a re-run.
    """
    path = skills_root / "palinode-session" / "SKILL.md"
    if path.is_symlink():
        return "skipped (symlink — curated externally, not touching)"
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists — re-run with --force to overwrite)"
    path.write_text(PALINODE_SESSION_SKILL, encoding="utf-8")
    return "created"


# The canonical palinode-session skill (ADR-012 Layer 2), embedded so
# `palinode init` can install it from the packaged CLI without the repo
# checkout. Byte-for-byte identical to skill/palinode-session/SKILL.md —
# a drift-guard test pins the two; edit the canonical file first, then
# mirror here. Written VERBATIM (it carries its own frontmatter), unlike
# the wrap skill which is rendered from a command body via _skill_md.
PALINODE_SESSION_SKILL = """\
---
name: palinode-session
description: "Guide intentional persistent-memory capture during coding sessions via Palinode MCP. Recall at session start; save durable milestones, explicit wrap-ups, and user-requested memories. Do NOT write for recall-only/no-new-information sessions or after an explicit no-save request."
---

# Palinode Session Memory

This skill keeps your AI agent's memory fresh across coding sessions using Palinode MCP tools.

## On Session Start

Search for prior context before beginning work:

```
palinode_search(query="[current project or task description]", limit=5)
```

Review results and reference relevant decisions or blockers from previous sessions.

## During Work — Save Milestones

After each major milestone, save the outcome:

```
palinode_save(
  content="[what was accomplished and why]",
  type="Decision",          # or "Insight" for reusable lessons
  project="[project-slug]"
)
```

### When to save:
- Tests pass after a significant change
- Feature is complete and working
- Architectural or design decision made (include rationale)
- Bug fixed that took >15 minutes (save the root cause)
- Something surprising discovered (save as Insight)

### When NOT to save:
- Routine file edits, typo fixes
- Intermediate debug steps (save the resolution only)
- Things git already tracks (code changes, file history)

## Every ~30 Minutes

If actively working and new durable progress has accumulated, consider a brief
progress note after roughly 30 minutes. Do not turn this into a timer-based
write for a recall-only/no-new-information session, and honor an explicit
request not to save:

```
palinode_save(
  content="Progress: [what's been done so far, what's next]",
  type="ProjectSnapshot"
)
```

## On Session End

At a user-requested wrap-up, or when the session produced new durable
information, capture the session:

```
palinode_session_end(
  summary="[1-2 sentence summary of accomplishments]",
  decisions=["decision 1 with rationale", "decision 2"],
  blockers=["open question or next step"],
  project="[project-slug]"
)
```

Do not create a recap for a recall-only or no-new-information session, and
honor an explicit request not to save. `palinode_save` and
`palinode_session_end` are intentional, content-bearing MCP writes; they are
separate from opt-in client hooks and background auto-summary enrichment.
`palinode init --no-hook` leaves automatic hooks off, but this skill can still
guide an agent to make an intentional explicit tool call. When the user invokes
`/wrap`, preserve that explicit requested session-end write unless the user
separately says not to save.

## Tool Reference

| Tool | When |
|---|---|
| `palinode_search` | Start of session, or "what do we know about X" |
| `palinode_save` | Milestones, decisions, insights, progress |
| `palinode_session_end` | End of session — structured summary |
| `palinode_diff` | "What changed recently?" |
| `palinode_blame` | "When was this decided?" |
| `palinode_trigger` | Register auto-recall for recurring topics |
"""


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
# post-launch).  The skip is implemented in _obsidian_plan().
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
- Re-run `palinode init --obsidian --dir <vault-path>` to restore scaffolded
  files if they are accidentally deleted (user-edited files are preserved).
"""


def _write_json_file(path: Path, data: dict, force: bool) -> str:
    """Write a JSON file; skip if exists and not forced."""
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "created"


def _write_text_file(path: Path, content: str, force: bool) -> str:
    """Write a text/markdown file; skip if exists and not forced."""
    _ensure_parent(path)
    if path.exists() and not force:
        return "skipped (exists)"
    path.write_text(content, encoding="utf-8")
    return "created"


def _write_store_prompt(source: Path, dest: Path) -> str:
    """Provision one consolidation prompt into the memory store.

    Never overwrites, and deliberately does not honour ``--force``: unlike
    everything else `init` writes, these are files the operator is invited to
    edit (`docs/HOW-MEMORY-WORKS.md` says so), and they live in the memory
    store rather than in the scaffolded project. A flag whose job is "redo the
    scaffolding" must not be able to discard tuning. `palinode prompt sync` is
    the sanctioned refresh path — it can tell a pristine copy from an edited
    one, and this cannot.
    """
    _ensure_parent(dest)
    if dest.exists():
        return "skipped (exists)"
    dest.write_bytes(source.read_bytes())
    return "created"


def _prompts_plan(memory_dir: Path) -> list[PlannedWrite]:
    """One entry per packaged consolidation prompt, into the memory store.

    Consolidation reads ``<memory_dir>/specs/prompts/*.md``. Before this, a
    `pip install` + `palinode init` store had none of them and the first
    `palinode consolidate` raised — the prompts only ever arrived by cloning
    the repo. The runner now falls back to the packaged copies, so this is
    about the *editable* copy: provisioning the store is what makes "tune the
    prompt" a thing an operator can do.
    """
    from palinode.prompts import iter_packaged_prompts, store_prompts_dir

    dest_dir = store_prompts_dir(memory_dir)
    return [
        PlannedWrite(
            f"prompt {source.name}",
            dest_dir / source.name,
            "consolidation prompt (memory store)",
            lambda source=source: _write_store_prompt(source, dest_dir / source.name),
        )
        for source in iter_packaged_prompts()
    ]


# The standard memory category directories so the Obsidian graph has seed
# nodes to render and Obsidian's file tree isn't empty.
_VAULT_DIRS = (
    "people", "projects", "decisions", "insights",
    "research", "daily", "archive", "logs",
)


def _write_vault_dir(d: Path) -> str:
    """Create one Obsidian vault category directory with a `.gitkeep`.

    A `.gitkeep` is placed in each so git tracks the (otherwise empty)
    directory. Status is keyed on the `.gitkeep`, not the directory itself,
    matching the pre-existing idempotency rule: re-running never disturbs a
    directory a user has since populated.
    """
    d.mkdir(exist_ok=True)
    gitkeep = d / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")
        return "created"
    return "skipped"


def _obsidian_plan(target: Path, force: bool, force_obsidian: bool) -> list[PlannedWrite]:
    """Enumerate every Obsidian scaffold write into *target* — 8 vault
    category directories plus 5 files, 13 entries total.

    Idempotency rules (mirrored per-entry in each `writer`):
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
    plan: list[PlannedWrite] = []

    for dir_name in _VAULT_DIRS:
        d = target / dir_name
        plan.append(PlannedWrite(
            dir_name + "/", d, "vault category directory",
            lambda d=d: _write_vault_dir(d),
        ))

    plan.append(PlannedWrite(
        ".obsidian/app.json", obsidian_dir / "app.json", "Obsidian app config",
        lambda: _write_json_file(obsidian_dir / "app.json", OBSIDIAN_APP_JSON, obsidian_force),
    ))
    plan.append(PlannedWrite(
        ".obsidian/graph.json", obsidian_dir / "graph.json", "graph view settings",
        lambda: _write_json_file(obsidian_dir / "graph.json", OBSIDIAN_GRAPH_JSON, obsidian_force),
    ))
    plan.append(PlannedWrite(
        ".obsidian/workspace.json", obsidian_dir / "workspace.json", "workspace layout",
        lambda: _write_json_file(
            obsidian_dir / "workspace.json", OBSIDIAN_WORKSPACE_JSON, workspace_force
        ),
    ))
    plan.append(PlannedWrite(
        "_index.md", target / "_index.md", "MOC at vault root",
        lambda: _write_text_file(target / "_index.md", OBSIDIAN_INDEX_MD, obsidian_force),
    ))
    plan.append(PlannedWrite(
        "_README.md", target / "_README.md", "vault orientation",
        lambda: _write_text_file(target / "_README.md", OBSIDIAN_README_MD, obsidian_force),
    ))
    return plan


def _mcp_json_block(pin_project: str | None = None) -> dict:
    """The ``.mcp.json`` block this run writes.

    With *pin_project*, the generated client carries ``PALINODE_PROJECT`` in
    its own process environment: every recall call from that client — the
    session-init digest and every later search alike — then resolves the same
    project through the shared resolver, without each call passing one and
    without the server remembering anything between calls. Without it the
    block is byte-identical to the one Palinode has always written.
    """
    block = json.loads(json.dumps(MCP_JSON_BLOCK))
    if pin_project:
        block["mcpServers"]["palinode"]["env"]["PALINODE_PROJECT"] = pin_project
    return block


def _merge_mcp_json(path: Path, force: bool, pin_project: str | None = None) -> str:
    _ensure_parent(path)
    block = _mcp_json_block(pin_project)
    if not path.exists():
        path.write_text(json.dumps(block, indent=2) + "\n", encoding="utf-8")
        return "created"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if not force:
            return "skipped (existing .mcp.json is not valid JSON — re-run with --force to overwrite)"
        existing = {}
    servers = existing.setdefault("mcpServers", {})
    if "palinode" in servers and not force:
        return "skipped (palinode MCP server already configured)"
    servers["palinode"] = block["mcpServers"]["palinode"]
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return "merged"


@dataclass(frozen=True)
class InitOptions:
    """Resolved inputs to `build_plan()`.

    Tri-state CLI flags (``agents``/``cursor`` auto-detect) and role-derived
    lists (``skill_roots``/``session_skill_roots``) are already resolved by
    the caller — `build_plan()` only branches on plain booleans and lists,
    it never re-derives them, so all the "which harness footprint did we
    detect" logic stays in one place (the `init` command below).
    """

    slug: str
    claudemd: bool
    agents: bool
    cursor: bool
    hook: bool
    slash: bool
    wrap_policy: str
    skill_roots: list[tuple[str, Path]]
    session_skill_roots: list[tuple[str, Path]]
    mcp: bool
    obsidian: bool
    force: bool
    force_obsidian: bool
    #: Pin ``slug`` as the generated MCP client's ``PALINODE_PROJECT``, so
    #: every recall call from it resolves that project. Off by default: the
    #: emitted block is unchanged unless the pin is asked for.
    pin_project: bool = False
    #: Install the Codex recall hooks (SessionStart, UserPromptSubmit) into
    #: ``.codex/``. Off unless asked for, like ``hook``.
    codex_hook: bool = False
    #: Where to provision ``specs/prompts/`` — the memory store, not the
    #: project. ``None`` skips it (``--no-prompts``). Passed in rather than
    #: read from config inside `build_plan()` so the plan stays a pure
    #: function of its inputs and tests can point it at a tmp_path.
    prompts_dir: Path | None = None
    #: Install the Cursor hooks (``.cursor/hooks.json`` + ``.cursor/hooks/``).
    #: Opt-in: off unless ``--cursor-hooks`` is passed.
    cursor_hooks: bool = False
    #: Install the Antigravity hooks (``.agents/hooks.json`` + ``.agents/hooks/``).
    #: Opt-in: off unless ``--agy-hooks`` is passed.
    agy_hooks: bool = False


def build_plan(target: Path, opts: InitOptions) -> list[PlannedWrite]:
    """Enumerate every write ``palinode init`` may perform — ONCE.

    Previously the dry-run branch (a run of ``click.echo`` lines) and the
    real-write branch (a run of ``results.append`` calls) were two
    hand-maintained enumerations of the same thing, and they had already
    drifted: the Obsidian scaffold's 8 vault-directory ``mkdir``s were
    invisible to ``--dry-run`` because it printed 5 hard-coded lines instead
    of walking what the real run walked. Building the list once and having
    both modes iterate it makes that class of drift structurally impossible
    — ``--dry-run`` prints ``label``/``path``/``payload`` without calling
    ``writer``; the real run calls ``writer()`` for the same entries.
    """
    plan: list[PlannedWrite] = []

    claude_md = target / ".claude" / "CLAUDE.md"
    agents_md = target / "AGENTS.md"
    cursor_rules = target / ".cursor" / "rules" / "palinode.md"
    settings = target / ".claude" / "settings.json"
    hook_script = target / ".claude" / "hooks" / "palinode-session-end.sh"
    start_hook_script = target / ".claude" / "hooks" / "palinode-session-start.sh"
    recall_hook_script = target / ".claude" / "hooks" / "palinode-user-prompt-submit.sh"
    compact_hook_script = target / ".claude" / "hooks" / "palinode-post-compact.sh"
    codex_hooks_json = target / ".codex" / "hooks.json"
    codex_start_hook = target / ".codex" / "hooks" / "palinode-session-start.sh"
    codex_recall_hook = target / ".codex" / "hooks" / "palinode-user-prompt-submit.sh"
    codex_end_hook = target / ".codex" / "hooks" / "palinode-session-end.sh"
    mcp_json = target / ".mcp.json"
    wrap_cmd = target / ".claude" / "commands" / "wrap.md"

    if opts.claudemd:
        plan.append(PlannedWrite(
            "CLAUDE.md", claude_md,
            f"memory instructions — {_memory_block_action(claude_md, opts.force)}",
            lambda: _write_claude_md(claude_md, opts.slug, opts.force, opts.wrap_policy),
        ))
    if opts.agents:
        plan.append(PlannedWrite(
            "AGENTS.md", agents_md,
            f"memory instructions — Antigravity/Codex; {_memory_block_action(agents_md, opts.force)}",
            lambda: _write_agents_md(agents_md, opts.slug, opts.force),
        ))
    if opts.cursor:
        plan.append(PlannedWrite(
            ".cursor/rules/palinode.md", cursor_rules,
            f"memory instructions — Cursor rules; {_memory_block_action(cursor_rules, opts.force)}",
            lambda: _write_cursor_rules(cursor_rules, opts.slug, opts.force),
        ))
    if opts.hook:
        plan.append(PlannedWrite(
            "session-start hook", start_hook_script, "SessionStart hook script",
            lambda: _write_hook_script(start_hook_script, opts.force, _hook_script("palinode-session-start.sh")),
        ))
        plan.append(PlannedWrite(
            "session-end hook", hook_script, "SessionEnd hook script",
            lambda: _write_hook_script(hook_script, opts.force, _hook_script("palinode-session-end.sh")),
        ))
        plan.append(PlannedWrite(
            "recall hook", recall_hook_script, "UserPromptSubmit per-turn recall hook script",
            lambda: _write_hook_script(recall_hook_script, opts.force, _hook_script("palinode-user-prompt-submit.sh")),
        ))
        plan.append(PlannedWrite(
            "compaction hook", compact_hook_script, "PostCompact checkpoint hook script",
            lambda: _write_hook_script(compact_hook_script, opts.force, _hook_script("palinode-post-compact.sh")),
        ))
        plan.append(PlannedWrite(
            "settings.json", settings, "hook registration",
            lambda: _merge_settings(settings, opts.force),
        ))
    if opts.codex_hook:
        # The same scripts Claude Code runs, copied under .codex/ so a Codex-only
        # project does not depend on a .claude/ install.
        plan.append(PlannedWrite(
            "Codex session-start hook", codex_start_hook, "Codex SessionStart hook script",
            lambda: _write_hook_script(codex_start_hook, opts.force, _hook_script("palinode-session-start.sh")),
        ))
        plan.append(PlannedWrite(
            "Codex recall hook", codex_recall_hook, "Codex UserPromptSubmit per-turn recall hook script",
            lambda: _write_hook_script(codex_recall_hook, opts.force, _hook_script("palinode-user-prompt-submit.sh")),
        ))
        plan.append(PlannedWrite(
            "Codex session-end hook", codex_end_hook, "Codex SessionEnd capture floor (topic + last reply)",
            lambda: _write_hook_script(codex_end_hook, opts.force, _hook_script("palinode-session-end.sh")),
        ))
        plan.append(PlannedWrite(
            ".codex/hooks.json", codex_hooks_json,
            "Codex hook registration, merged after any existing hooks",
            lambda: CODEX.merge(codex_hooks_json, Path.home(), target, opts.force),
        ))
    if opts.cursor_hooks:
        cursor_hooks_dir = target / CURSOR.hooks_dir
        for name, content, what in (
            ("palinode-cursor-hook.sh", _hook_script("palinode-cursor-hook.sh"), "Cursor hook adapter"),
            ("palinode-session-start.sh", _hook_script("palinode-session-start.sh"), "sessionStart recall script"),
            ("palinode-user-prompt-submit.sh", _hook_script("palinode-user-prompt-submit.sh"), "beforeSubmitPrompt recall script"),
        ):
            script_path = cursor_hooks_dir / name
            plan.append(PlannedWrite(
                f"Cursor hook {name}", script_path, what,
                lambda script_path=script_path, content=content: _write_hook_script(
                    script_path, opts.force, content),
            ))
        cursor_hooks_json = target / ".cursor" / "hooks.json"
        plan.append(PlannedWrite(
            ".cursor/hooks.json", cursor_hooks_json,
            "Cursor hook registration (sessionStart + beforeSubmitPrompt; existing entries kept)",
            lambda: CURSOR.merge(cursor_hooks_json, Path.home(), target, opts.force),
        ))
    if opts.agy_hooks:
        agy_hooks_dir = target / ANTIGRAVITY.hooks_dir
        for name, content, what in (
            ("palinode-agy-hook.sh", _hook_script("palinode-agy-hook.sh"), "Antigravity hook adapter"),
            ("palinode-session-start.sh", _hook_script("palinode-session-start.sh"), "SessionStart recall script"),
            ("palinode-user-prompt-submit.sh", _hook_script("palinode-user-prompt-submit.sh"), "PreInvocation recall script"),
        ):
            script_path = agy_hooks_dir / name
            plan.append(PlannedWrite(
                f"Antigravity hook {name}", script_path, what,
                lambda script_path=script_path, content=content: _write_hook_script(
                    script_path, opts.force, content),
            ))
        agy_hooks_json = target / ".agents" / "hooks.json"
        plan.append(PlannedWrite(
            ".agents/hooks.json", agy_hooks_json,
            "Antigravity hook registration (SessionStart + PreInvocation; existing hooks kept)",
            lambda: ANTIGRAVITY.merge(agy_hooks_json, Path.home(), target, opts.force),
        ))
    if opts.slash:
        wrap_body = WRAP_HEAVY_COMMAND_BODY if opts.wrap_policy == "heavy" else WRAP_COMMAND_BODY
        plan.append(PlannedWrite(
            f"/wrap command ({opts.wrap_policy})", wrap_cmd,
            f"/wrap slash command — {opts.wrap_policy} policy",
            lambda: _write_slash_command(wrap_cmd, wrap_body, opts.force),
        ))

    # optional skill-format install. Same body as the slash command (single
    # source — no drift); 'personal' scope makes /wrap typeable in every
    # project, not just this one.
    skill_specs = [
        ("wrap", WRAP_HEAVY_COMMAND_BODY if opts.wrap_policy == "heavy" else WRAP_COMMAND_BODY),
    ]
    for scope_label, root in opts.skill_roots:
        for name, body in skill_specs:
            path = root / name / "SKILL.md"
            plan.append(PlannedWrite(
                f"/{name} skill ({scope_label})", path, f"/{name} skill — {scope_label} scope",
                lambda root=root, name=name, body=body: _write_skill(root, name, body, opts.force),
            ))

    for scope_label, root in opts.session_skill_roots:
        path = root / "palinode-session" / "SKILL.md"
        plan.append(PlannedWrite(
            f"palinode-session skill ({scope_label})", path,
            f"memory skill — {scope_label} scope",
            lambda root=root: _write_session_skill(root, opts.force),
        ))

    if opts.mcp:
        pin = opts.slug if opts.pin_project else None
        plan.append(PlannedWrite(
            ".mcp.json", mcp_json,
            f"MCP server block, project pinned: {pin}" if pin else "MCP server block",
            lambda: _merge_mcp_json(mcp_json, opts.force, pin),
        ))

    if opts.obsidian:
        plan.extend(_obsidian_plan(target, opts.force, opts.force_obsidian))

    if opts.prompts_dir is not None:
        plan.extend(_prompts_plan(opts.prompts_dir))

    return plan


def both_instruction_files(target: Path, opts: InitOptions) -> bool:
    """True when this run leaves *target* holding both instruction surfaces.

    "Leaves" covers written-now and already-there: a project whose AGENTS.md
    predates Palinode is exactly the case the disclosure exists for, and a
    ``--no-claudemd`` run into a project that already has a CLAUDE.md is the
    same collision from the other side. The CLAUDE.md side matches the set of
    files Claude Code counts when it decides whether to read AGENTS.md.
    """
    claude_side = opts.claudemd or any(
        (target / name).exists()
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")
    )
    agents_side = opts.agents or (target / "AGENTS.md").exists()
    return claude_side and agents_side


def _display_path(path: Path, target: Path) -> str:
    """Render *path* for the ``--dry-run`` listing: relative to *target*
    when it's under the project (the common case), absolute otherwise
    (personal-scope skills under ``~/.claude/skills``, or a custom
    ``--skill-path``)."""
    try:
        return str(path.relative_to(target))
    except ValueError:
        return str(path)


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
    "--pin-project",
    is_flag=True,
    default=False,
    help=(
        "Pin the project slug as the generated MCP client's PALINODE_PROJECT, "
        "so every recall call from that client resolves it without passing a "
        "project argument. Requires --mcp."
    ),
)
@click.option(
    "--claudemd/--no-claudemd",
    default=True,
    help="Write the Palinode memory block to .claude/CLAUDE.md",
)
@click.option(
    "--agents/--no-agents",
    "agents",
    default=None,
    help=(
        "Write the harness-neutral memory block to AGENTS.md (read by "
        "Antigravity, Codex, and other AGENTS.md-aware harnesses). Default: "
        "auto — on when AGENTS.md or a .agent/ directory exists in the target, "
        "off otherwise. Explicit --agents forces it on."
    ),
)
@click.option(
    "--cursor/--no-cursor",
    "cursor",
    default=None,
    help=(
        "Write the harness-neutral memory block to .cursor/rules/palinode.md "
        "(read by Cursor). Default: auto — on when a .cursor/ directory exists "
        "in the target, off otherwise. Explicit --cursor forces it on."
    ),
)
@click.option(
    "--hook/--no-hook",
    default=None,
    help=(
        "Opt in to Claude Code automatic hooks (including transcript-derived "
        "SessionEnd capture). Default: off for a new project; preserve the "
        "existing choice when Palinode hooks are already installed."
    ),
)
@click.option(
    "--codex-hook/--no-codex-hook",
    default=None,
    help=(
        "Opt in to the Codex hooks: SessionStart (scoped core-memory "
        "digest), UserPromptSubmit (per-prompt recall) and SessionEnd (the "
        "session-end capture floor, which reads the Codex rollout), written to "
        ".codex/hooks/ and merged into .codex/hooks.json without touching "
        "other tools' entries. Default: off for a new project; preserve the "
        "existing choice when they are already installed."
    ),
)
@click.option(
    "--cursor-hooks/--no-cursor-hooks",
    "cursor_hooks",
    default=False,
    help=(
        "Opt in to Cursor hooks: .cursor/hooks.json registers sessionStart "
        "(the project's core memories) and beforeSubmitPrompt (per-prompt "
        "recall), merged into any existing hooks.json, with the scripts in "
        ".cursor/hooks/. Default: off."
    ),
)
@click.option(
    "--agy-hooks/--no-agy-hooks",
    "agy_hooks",
    default=False,
    help=(
        "Opt in to Antigravity hooks (agy CLI and IDE): .agents/hooks.json registers SessionStart "
        "(the project's core memories) and PreInvocation (per-prompt recall, "
        "first model call of each turn), merged into any existing hooks.json, "
        "with the scripts in .agents/hooks/. Default: off."
    ),
)
@click.option(
    "--slash/--no-slash",
    default=True,
    help="Install the /wrap slash command for the save-before-clear reflex (/save and /ps are deprecated and no longer scaffolded)",
)
@click.option(
    "--wrap-policy",
    type=click.Choice(["light", "heavy"]),
    default="light",
    help=(
        "Which /wrap variant to scaffold. 'light' (default): /wrap just "
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
        "Also install /wrap as a Claude Code *skill* — the modern "
        "format (user-scope `.claude/commands/` is no longer searched). "
        "'personal' → ~/.claude/skills/ so /wrap is typeable in ALL projects "
        "(not just this one); 'project' → .claude/skills/; 'both'. The body "
        "comes from the same source as the slash command, so they can't drift. "
        "Default: none."
    ),
)
@click.option(
    "--skill/--no-skill",
    "session_skill",
    default=True,
    help=(
        "Install the palinode-session skill (instruction-driven guidance: "
        "recall at start and conditional explicit MCP saves/session-end calls, "
        "not automatic hook capture) into the project's "
        "harness skill paths — .claude/skills/ always, plus .cursor/skills/ "
        "and .agent/skills/ when those harness footprints exist. Default: on."
    ),
)
@click.option(
    "--skill-path",
    "skill_path",
    type=click.Path(file_okay=False),
    default=None,
    help=(
        "Override the skill install root: the palinode-session skill is "
        "written to <path>/palinode-session/SKILL.md instead of the "
        "auto-detected harness paths."
    ),
)
@click.option(
    "--user",
    "user_skill",
    is_flag=True,
    default=False,
    help=(
        "Install the palinode-session skill to ~/.claude/skills/ (per-user, "
        "available in every project) instead of the project-scoped paths."
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
    "--prompts/--no-prompts",
    default=True,
    help=(
        "Provision the consolidation prompts into the memory store "
        "($PALINODE_DIR/specs/prompts/). Existing files are never overwritten, "
        "with or without --force — use `palinode prompt sync` to refresh them. "
        "Default: on."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing files; replace Palinode blocks in instruction files (default: preserve / append / skip)",
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
    pin_project,
    claudemd,
    agents,
    cursor,
    hook,
    codex_hook,
    cursor_hooks,
    agy_hooks,
    slash,
    wrap_policy,
    skills,
    session_skill,
    skill_path,
    user_skill,
    obsidian,
    force_obsidian,
    prompts,
    force,
    dry_run,
):
    """Scaffold Palinode into a project for zero-friction adoption.

    Creates (or appends to):
      .claude/CLAUDE.md                       — memory instructions for the agent
      .claude/settings.json                   — SessionStart + SessionEnd + UserPromptSubmit + PostCompact hook registration
      .claude/hooks/palinode-session-start.sh — hook script (core-memory inject + context prime)
      .claude/hooks/palinode-session-end.sh   — hook script (fires on /clear, exit)
      .claude/hooks/palinode-user-prompt-submit.sh — hook script (per-turn implicit recall)
      .claude/hooks/palinode-post-compact.sh  — hook script (saves the compaction summary as a checkpoint)
      .mcp.json                               — palinode MCP server block
      .codex/hooks.json + .codex/hooks/       — Codex SessionStart + UserPromptSubmit
                                                recall hooks and the SessionEnd capture
                                                floor (with --codex-hook; merged
                                                after existing entries, never replacing them)
      AGENTS.md                               — harness-neutral memory block (when AGENTS.md
                                                or .agent/ is detected, or --agents)
      .cursor/rules/palinode.md               — harness-neutral memory block (when .cursor/
                                                is detected, or --cursor)
      .cursor/hooks.json, .cursor/hooks/      — Cursor sessionStart + beforeSubmitPrompt
                                                recall (only with --cursor-hooks)
      .agents/hooks.json, .agents/hooks/      — Antigravity SessionStart + PreInvocation
                                                recall (only with --agy-hooks)
      .claude/skills/palinode-session/SKILL.md — ambient memory skill (plus
                                                .cursor/skills/ and .agent/skills/ when
                                                detected; --user for ~/.claude/skills/)

    And, in the memory store (not the project):
      $PALINODE_DIR/specs/prompts/*.md        — the consolidation prompts, copied
                                                from the ones packaged with palinode.
                                                Never overwritten; `palinode prompt sync`
                                                refreshes them. --no-prompts skips.

    With --obsidian, additionally writes:
      .obsidian/app.json       — wikilinks, daily/ as default file location
      .obsidian/graph.json     — pre-tuned graph (collapsed dirs, color groups)
      .obsidian/workspace.json — sidebar opens on daily/ by default
      _index.md                — starter MOC linking all category dirs
      _README.md               — vault orientation for first-time openers

    Re-run with --force to overwrite files or replace instruction-file Palinode
    blocks in place. --dry-run shows the plan without writing.
    --force-obsidian overwrites the Obsidian scaffold only (preserving workspace.json).
    """
    target = Path(target_dir).resolve()
    if not target.exists():
        raise click.ClickException(f"Directory not found: {target}")

    slug = project_slug or _slugify(target.name)

    if pin_project:
        if not mcp:
            raise click.UsageError("--pin-project requires --mcp; it pins the generated client's scope.")
        # The pinned value lands in a process environment and is resolved on
        # every recall call, so it is validated here by the same rule the
        # resolver enforces rather than written and rejected later.
        from palinode.core.context_prime import InvalidProjectScope, validate_project_setting

        try:
            validate_project_setting(slug, source="The project slug")
        except InvalidProjectScope as exc:
            raise click.UsageError(str(exc)) from exc

    # --force-obsidian implies --obsidian
    if force_obsidian:
        obsidian = True

    agents_md = target / "AGENTS.md"
    # ADR-012 Layer 1 detection defaults: scaffold the other instruction-file
    # surfaces only where the harness footprint already exists, unless the
    # caller opts in/out explicitly (tri-state flags: None = auto-detect).
    if agents is None:
        agents = agents_md.exists() or (target / ".agent").is_dir()
    if cursor is None:
        cursor = (target / ".cursor").is_dir()

    # A transcript is a new automatic source, so a fresh project must opt in
    # explicitly with --hook. Existing hook installations are left in their
    # chosen mode on re-run; --force remains required to replace their scripts.
    if hook is None:
        hook = (target / ".claude" / "hooks" / "palinode-session-end.sh").is_file()
    if codex_hook is None:
        codex_hook = any(
            (target / CODEX.hooks_dir / reg.script).is_file() for reg in CODEX.registrations
        )

    # 'personal' scope makes /wrap typeable in every project, not just this
    # one. /wrap is the sole lifecycle command — /save and /ps are
    # deprecated and no longer scaffolded (mid-session checkpoints call the
    # palinode_save tool directly).
    skill_roots: list[tuple[str, Path]] = []
    if skills in ("project", "both"):
        skill_roots.append(("project", target / ".claude" / "skills"))
    if skills in ("personal", "both"):
        skill_roots.append(("personal", Path.home() / ".claude" / "skills"))

    # ADR-012 Layer 2: the palinode-session skill (ambient memory behavior)
    # installs into every harness skill path detected in the project.
    # Precedence: --skill-path override > --user (per-user, replaces
    # project scope) > detection (.claude always; .cursor/.agent when present).
    session_skill_roots: list[tuple[str, Path]] = []
    if session_skill:
        if skill_path:
            session_skill_roots.append(("custom", Path(skill_path)))
        elif user_skill:
            session_skill_roots.append(("user", Path.home() / ".claude" / "skills"))
        else:
            session_skill_roots.append(("project", target / ".claude" / "skills"))
            if (target / ".cursor").is_dir():
                session_skill_roots.append(("cursor", target / ".cursor" / "skills"))
            if (target / ".agent").is_dir():
                session_skill_roots.append(("agent-dir", target / ".agent" / "skills"))

    # The prompts go into the memory store, which is usually somewhere else
    # entirely than the project being scaffolded. Config is imported here
    # rather than at module scope so `palinode init --no-prompts` and the
    # scaffolding tests keep working without a resolvable memory dir.
    prompts_dir: Path | None = None
    if prompts:
        from palinode.core.config import config as _config

        prompts_dir = Path(_config.memory_dir)

    opts = InitOptions(
        slug=slug,
        claudemd=claudemd,
        agents=agents,
        cursor=cursor,
        hook=hook,
        slash=slash,
        wrap_policy=wrap_policy,
        skill_roots=skill_roots,
        session_skill_roots=session_skill_roots,
        mcp=mcp,
        obsidian=obsidian,
        force=force,
        force_obsidian=force_obsidian,
        prompts_dir=prompts_dir,
        pin_project=pin_project,
        codex_hook=codex_hook,
        cursor_hooks=cursor_hooks,
        agy_hooks=agy_hooks,
    )
    plan = build_plan(target, opts)

    click.echo("Before your first capture:")
    click.echo("  Memories are readable Markdown and Git history.")
    click.echo("  private/restricted control discovery by scope, not encryption or user/agent authentication.")
    click.echo("  API callers can read hidden known paths and access full-store maintenance tools.")
    click.echo("  Protect the store, backups and API token; use filesystem permissions or separate instances.")
    click.echo("  Contract: docs/PRIVACY.md (privacy and visibility).")
    click.echo("  Before enabling hooks, run `palinode controls status` for control state and redacted effective destinations.")
    click.echo("")
    click.echo(f"Palinode init → {target}")
    click.echo(f"  project slug: {slug}")
    if hook:
        click.echo("  automatic Claude hooks: enabled by --hook (SessionEnd reads the Claude transcript, PostCompact saves Claude Code's compaction summary; controls can pause/exclude both)")
    else:
        click.echo("  automatic Claude hooks: not installed (use --hook to opt in to SessionStart, prompt recall, transcript-derived SessionEnd capture, and compaction checkpoints)")
    if codex_hook:
        click.echo("  automatic Codex hooks: enabled by --codex-hook (SessionStart core-memory digest, per-prompt recall, and SessionEnd capture that reads the Codex rollout; controls can pause/exclude them)")
        click.echo(f"    {CODEX_TRUST_NOTE}")
    if cursor_hooks:
        click.echo("  Cursor hooks: enabled by --cursor-hooks (sessionStart injects this project's core memories; beforeSubmitPrompt recalls per prompt; controls can pause both)")
    if agy_hooks:
        click.echo("  Antigravity hooks: enabled by --agy-hooks (SessionStart injects this project's core memories; PreInvocation recalls per prompt; controls can pause both)")
    if session_skill:
        click.echo("  instruction-driven session capture: palinode-session skill selected; it can guide conditional explicit MCP writes even without --hook")
    else:
        click.echo("  palinode-session skill: not selected (--no-skill); generated memory instructions can still guide explicit MCP writes")
    # Printed before the dry-run branch so --dry-run and a real run disclose
    # the split identically.
    if both_instruction_files(target, opts):
        click.echo("")
        click.echo(INSTRUCTION_FILE_DISCLOSURE)
    click.echo("")

    if dry_run:
        click.echo("[dry-run] Would write:")
        for pw in plan:
            click.echo(f"  {_display_path(pw.path, target)}  ({pw.payload})")
        return

    for pw in plan:
        status = pw.writer()
        mark = "✓" if status.startswith(("created", "appended", "merged", "replaced")) else "·"
        click.echo(f"  {mark} {pw.label}: {status}")

    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Make sure palinode-api is running (palinode start, or systemd)")
    if obsidian:
        click.echo("  2. Open the vault in Obsidian: open -a Obsidian " + str(target))
        click.echo("  3. Try it:  \"search palinode for recent decisions on this project\"")
    else:
        click.echo("  2. Open the project in Claude Code — the MCP server will connect on start")
        click.echo("  3. Try it:  \"search palinode for recent decisions on this project\"")
    if codex_hook:
        click.echo("  Codex: open Codex in this project and run /hooks to trust the palinode hooks.")
