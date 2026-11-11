"""Check: hooks_current

``palinode init`` copies the Claude Code and Codex hook scripts once and never
revisits them, so an install keeps running the hook it started with while the
server and the packaged hooks move on. Release smokes install the current hook
fresh, so they cannot see this; the stale copy surfaces only as recall or
capture behaviour that a release was supposed to have fixed.

This check walks the Claude Code settings cascade (user, project, project
local — relative to the current directory), Codex's hook layers (user
``$CODEX_HOME`` or ``~/.codex``, and the project's ``.codex/``; ``hooks.json``
and ``config.toml`` in each), the project's Cursor ``.cursor/hooks.json`` and
Antigravity ``.agents/hooks.json``, compares every registered palinode hook
script byte-for-byte with the packaged copy, and warns on drift with the
command that refreshes it. Discovery and comparison are shared with
``palinode hooks update`` (:mod:`palinode.cli.hooks`), so the diagnosis and
the fix agree on what is installed. Non-palinode hooks are never read.

``fast``: a few small local reads, no network.
"""
from __future__ import annotations

from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

_NAME = "hooks_current"


@register(tags=("fast",))
def hooks_current(ctx: DoctorContext) -> CheckResult:
    """Warn when an installed palinode hook differs from the packaged copy."""
    from palinode.cli.hooks import (
        STATE_CURRENT,
        STATE_FOREIGN,
        STATE_MISSING,
        STATE_STALE,
        STATE_SYMLINK,
        UPDATE_COMMAND,
        CHANGE_QUOTED,
        CHANGE_SYNCHRONOUS,
        registrations_needing_repair,
        describe_version,
        hook_statuses,
        settings_files,
    )

    home = Path.home()
    project = Path.cwd()
    statuses = [s for s in hook_statuses(home, project) if s.state != STATE_FOREIGN]

    if not statuses:
        checked = ", ".join(str(p) for p in settings_files(home, project))
        return CheckResult(
            name=_NAME,
            severity="info",
            passed=True,
            message=f"No palinode hooks registered (checked {checked}).",
            tags=("fast",),
        )

    drifted = [s for s in statuses if s.state != STATE_CURRENT]
    broken = registrations_needing_repair(home, project)
    if broken:
        reasons = {
            CHANGE_SYNCHRONOUS: (
                'palinode\'s PostCompact hook is registered with "async": true, which '
                "Claude Code cancels, so compactions leave no checkpoint"
            ),
            CHANGE_QUOTED: (
                "a palinode hook command uses ${CLAUDE_PROJECT_DIR} unquoted, which the "
                "shell splits at a space, so in a project path with a space the hook "
                "does not run"
            ),
        }
        details = "; ".join(
            f"{path}: " + ", and ".join(reasons[c] for c in changes) for path, changes in broken
        )
        return CheckResult(
            name=_NAME,
            severity="warn",
            passed=False,
            message=(
                f"palinode's Claude Code hook registration needs a repair — {details}."
                + (f" {len(drifted)} hook script(s) also differ from the packaged copies." if drifted else "")
            ),
            remediation=(
                f"Run `{UPDATE_COMMAND}` from this directory (preview with "
                f"`{UPDATE_COMMAND} --dry-run`). It repairs palinode's own registrations, "
                f"keeping a backup of the settings file, and refreshes stale hooks."
            ),
            tags=("fast",),
        )
    if not drifted:
        versions = sorted({describe_version(s.packaged_version) for s in statuses})
        return CheckResult(
            name=_NAME,
            severity="info",
            passed=True,
            message=(
                f"All {len(statuses)} installed palinode hook(s) match the packaged "
                f"copies (hook version {', '.join(versions)})."
            ),
            tags=("fast",),
        )

    details: list[str] = []
    for s in drifted:
        if s.state == STATE_MISSING:
            details.append(f"{s.path} (registered but missing)")
        elif s.state == STATE_STALE:
            details.append(
                f"{s.path} (installed {describe_version(s.installed_version)}, "
                f"packaged {describe_version(s.packaged_version)})"
            )
        elif s.state == STATE_SYMLINK:
            details.append(f"{s.path} (symlink whose target differs from the packaged copy)")
        else:
            details.append(f"{s.path} ({s.state})")

    fixable = any(s.state in (STATE_STALE, STATE_MISSING) for s in drifted)
    remediation = (
        f"Run `{UPDATE_COMMAND}` from this directory (preview first with "
        f"`{UPDATE_COMMAND} --dry-run`). It replaces each stale palinode hook with "
        f"the packaged copy, keeping the old one as <name>.bak-<timestamp>, and "
        f"never touches other hooks."
    )
    if not fixable:
        remediation = (
            "These hooks are symlinks or unreadable, so "
            f"`{UPDATE_COMMAND}` leaves them alone — refresh the file they point to."
        )
    return CheckResult(
        name=_NAME,
        severity="warn",
        passed=False,
        message=(
            f"{len(drifted)} of {len(statuses)} installed palinode hook(s) differ from "
            f"the packaged copies, so fixes shipped in hooks are not running: "
            f"{'; '.join(details)}."
        ),
        remediation=remediation,
        tags=("fast",),
    )
