"""
palinode doctor `--fix` mode whitelist.

Registers the *only* fix actions doctor is allowed to apply.  The design
constraint is non-negotiable:

    Doctor never moves user data, even with --fix.

The whitelist below is the entire safe surface.  Adding a new entry must be
justified explicitly in the PR description; data-touching fixes are off-
limits regardless of how convenient they would be.

Whitelist
---------
1. ``memory_dir_exists``       → create the configured memory_dir if missing.
2. ``audit_log_writable``      → create the parent dir of audit.log_path if
                                 it is relative and missing.  Never creates
                                 the log file itself; the audit subsystem
                                 owns that.
3. ``claude_md_palinode_block`` → append a Memory (Palinode) block to an
                                 existing CLAUDE.md.  Never creates
                                 CLAUDE.md from nothing — that file is
                                 user-owned.

Explicitly NOT fixable (and the reason is "doctor never moves user data"):

  - db_path_under_memory_dir  → suggests where db_path *should* point, but
    moving the DB file would constitute data motion.
  - phantom_db_files          → prints the suggested ``mv`` commands; doctor
    never executes them.  Phantom DB files often contain partial writes from
    a stale watcher; the user must inspect them before any move.
  - watcher_indexes_correct_db → editing the systemd unit file is a deploy
    action, not a doctor concern.  Prints the remediation only.
"""
from __future__ import annotations

import logging
from pathlib import Path

from palinode.diagnostics.registry import register_fix
from palinode.diagnostics.types import CheckResult, DoctorContext, FixResult

_logger = logging.getLogger("palinode.doctor.fix")


# ---------------------------------------------------------------------------
# fix #1: memory_dir_exists
# ---------------------------------------------------------------------------

def fix_memory_dir_exists(ctx: DoctorContext, result: CheckResult) -> FixResult:
    """Create the configured memory_dir (and any missing parents)."""
    target = Path(ctx.config.memory_dir).expanduser().resolve()
    if target.exists():
        return FixResult(
            applied=False,
            message=f"memory_dir already exists at {target}; nothing to do.",
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not create {target}: {exc}",
        )
    _logger.info("doctor --fix: created memory_dir at %s", target)
    return FixResult(applied=True, message=f"Created directory {target}")


# ---------------------------------------------------------------------------
# fix #2: audit_log_writable
# ---------------------------------------------------------------------------

def fix_audit_log_writable(ctx: DoctorContext, result: CheckResult) -> FixResult:
    """Create the parent dir of a relative-and-missing audit log path.

    Conservative scope:
      - Only creates the *parent directory* of audit.log_path.  Never the
        log file itself — the audit subsystem owns that.
      - Only acts when audit.log_path is relative.  Absolute paths under
        operator control are out of scope (they may live on a separate
        mount with intentional permissions).
      - Anchors the relative path under config.memory_dir so the resulting
        directory is colocated with the memory store (matches the design-
        doc remediation: "Set audit.log_path to an absolute path under
        memory_dir").
    """
    audit = getattr(ctx.config, "audit", None)
    if audit is None or not getattr(audit, "enabled", False):
        return FixResult(
            applied=False,
            message="audit logging is disabled; nothing to do.",
        )
    log_path_str = getattr(audit, "log_path", "")
    if not log_path_str:
        return FixResult(
            applied=False,
            message="audit.log_path is empty; nothing to do.",
        )
    log_path = Path(log_path_str)
    if log_path.is_absolute():
        return FixResult(
            applied=False,
            message=(
                f"audit.log_path is absolute ({log_path}); doctor leaves "
                "operator-managed absolute paths alone.  Edit "
                "palinode.config.yaml manually if needed."
            ),
        )
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()
    parent = (memory_dir / log_path).parent
    if parent.exists():
        return FixResult(
            applied=False,
            message=f"Audit log parent {parent} already exists; nothing to do.",
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not create {parent}: {exc}",
        )
    _logger.info("doctor --fix: created audit log parent dir at %s", parent)
    return FixResult(
        applied=True,
        message=f"Created audit log parent directory {parent}",
    )


# ---------------------------------------------------------------------------
# fix #3: claude_md_palinode_block
# ---------------------------------------------------------------------------

# Default block appended when CLAUDE.md exists but is missing the Palinode
# section.  Mirrors the block scaffolded by `palinode init`.  Newline at the
# top guarantees separation from any prior content.
_PALINODE_BLOCK = """
## Memory (Palinode)

This project uses Palinode for persistent memory (MCP server: palinode).

### At session start:
- Call `palinode_search` with the current task or project name for prior context

### During work:
- After major milestones: call `palinode_save` with the decision or outcome
- When making architectural decisions: save the decision AND the rationale

### At session end:
- Call `palinode_session_end` with summary, decisions, and blockers
"""


# Marker used to detect an existing block.  Both possible header forms are
# checked so a hand-rolled "Memory (Palinode)" subheading is not duplicated.
_PALINODE_HEADER_MARKERS = ("## Memory (Palinode)", "# Memory (Palinode)")


def fix_claude_md_palinode_block(ctx: DoctorContext, result: CheckResult) -> FixResult:
    """Append a Palinode memory block to an existing CLAUDE.md.

    Strict guard: only appends when CLAUDE.md ALREADY exists.  Never creates
    CLAUDE.md from scratch — that is a user-owned project file and creating
    it without consent would be presumptuous.

    The CLAUDE.md path is resolved from the doctor result message when the
        check provides one; otherwise we look in cwd, which matches the
        heuristic ("In each cwd ancestor, look for CLAUDE.md"). The
    fix never walks the filesystem on its own — if no CLAUDE.md exists in
    cwd, the fix declines with a clear message.
    """
    candidate = Path.cwd() / "CLAUDE.md"
    if not candidate.exists():
        return FixResult(
            applied=False,
            message=(
                f"No CLAUDE.md at {candidate}; doctor will not create one. "
                "CLAUDE.md is user-owned — create it manually, then re-run "
                "'palinode doctor --fix'."
            ),
        )
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not read {candidate}: {exc}",
        )
    if any(marker in content for marker in _PALINODE_HEADER_MARKERS):
        return FixResult(
            applied=False,
            message=(
                f"{candidate} already contains a Palinode memory block; "
                "nothing to do."
            ),
        )
    # Ensure exactly one blank line of separation before the appended block.
    sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
    try:
        with candidate.open("a", encoding="utf-8") as fh:
            fh.write(sep + _PALINODE_BLOCK)
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not append to {candidate}: {exc}",
        )
    _logger.info("doctor --fix: appended Palinode memory block to %s", candidate)
    return FixResult(
        applied=True,
        message=f"Appended Palinode memory block to {candidate}",
    )


# ---------------------------------------------------------------------------
# Registration — THE WHITELIST.
# ---------------------------------------------------------------------------
# Adding any line below requires explicit reasoning in the PR description.
# Doctor never moves user data, even with --fix.

register_fix("memory_dir_exists", fix_memory_dir_exists)
register_fix("audit_log_writable", fix_audit_log_writable)
register_fix("claude_md_palinode_block", fix_claude_md_palinode_block)
