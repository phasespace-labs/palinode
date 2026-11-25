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
script byte-for-byte with the packaged copy and every palinode registration
with the harness spec that wrote it (ADR-023), and warns on drift with the
command that fixes it. A registration someone changed or removed is noted,
not warned about: it is the user's choice. Discovery and comparison are shared with
``palinode hooks update`` (:mod:`palinode.hooks.installed`), so the diagnosis and
the fix agree on what is installed. Non-palinode hooks are never read.

``fast``: a few small local reads, no network.
"""
from __future__ import annotations


from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

_NAME = "hooks_current"


@register(tags=("fast",))
def hooks_current(ctx: DoctorContext) -> CheckResult:
    """Warn when an installed palinode hook or registration differs from the package."""
    from palinode.hooks.installed import (
        STATE_CURRENT,
        STATE_EDITED,
        STATE_FOREIGN,
        STATE_MISSING,
        STATE_STALE,
        STATE_SYMLINK,
        UPDATE_COMMAND,
        describe_version,
        hook_statuses,
        plan_registrations,
        registrations_to_fix_by_hand,
        settings_files,
    )

    home = ctx.home
    project = ctx.cwd.resolve()
    statuses = [s for s in hook_statuses(home, project) if s.state != STATE_FOREIGN]
    plans = plan_registrations(home, project)
    by_hand = registrations_to_fix_by_hand(home, project)

    if not statuses and not by_hand and not plans:
        checked = ", ".join(str(p) for p in settings_files(home, project))
        return CheckResult(
            name=_NAME,
            severity="info",
            passed=True,
            message=f"No palinode hooks registered (checked {checked}).",
            tags=("fast",),
        )

    problems: list[str] = []
    for plan in plans:
        for change in plan.repairs:
            problems.append(f"{plan.path}: {_explain(change)}")
        for reg in plan.new:
            problems.append(
                f"{plan.path}: the {reg.event} hook (added in palinode {reg.since}) is not registered"
            )
    problems += [f"{f.path}: `{f.command}` {f.problem}" for f in by_hand]

    drifted = [s for s in statuses if s.state != STATE_CURRENT]
    for s in drifted:
        if s.state == STATE_MISSING:
            problems.append(f"{s.path} (registered but missing)")
        elif s.state == STATE_STALE:
            problems.append(
                f"{s.path} (installed {describe_version(s.installed_version)}, "
                f"packaged {describe_version(s.packaged_version)})"
            )
        elif s.state == STATE_EDITED:
            problems.append(
                f"{s.path} (edited: matches no palinode release, so fixes shipped in it "
                f"are not running)"
            )
        elif s.state == STATE_SYMLINK:
            problems.append(f"{s.path} (symlink whose target differs from the packaged copy)")
        else:
            problems.append(f"{s.path} ({s.state})")

    notes: list[str] = []
    for plan in plans:
        for e in plan.edited:
            notes.append(
                f"{plan.path}: the {e.event} registration has timeout {e.timeout}"
                f"{' and async' if e.is_async else ''}, not what palinode writes"
            )
        for reg in plan.removed:
            notes.append(f"{plan.path}: the {reg.event} hook is not registered, though {reg.script} is installed")
        for reg in plan.not_offered:
            notes.append(f"{plan.path}: the {reg.event} hook (added in palinode {reg.since}) is not registered")
    note_text = (
        " Left as you set them (`" + UPDATE_COMMAND + " --force` resets them): " + "; ".join(notes) + "."
        if notes else ""
    )

    if not problems:
        versions = sorted({describe_version(s.packaged_version) for s in statuses})
        return CheckResult(
            name=_NAME,
            severity="info",
            passed=True,
            message=(
                f"All {len(statuses)} installed palinode hook(s) match the packaged "
                f"copies (hook version {', '.join(versions)})." + note_text
            ),
            tags=("fast",),
        )

    remediation = (
        f"Run `{UPDATE_COMMAND}` from this directory (preview first with "
        f"`{UPDATE_COMMAND} --dry-run`). It replaces each stale palinode hook with "
        f"the packaged copy and repairs palinode's own registrations, keeping a backup "
        f"of each file it changes, and never touches other hooks."
    )
    extra: list[str] = []
    if any(s.state == STATE_EDITED for s in drifted):
        extra.append(f"An edited hook is replaced only with `{UPDATE_COMMAND} --force`.")
    if any(s.state == STATE_SYMLINK for s in drifted):
        extra.append("A symlinked hook is left alone: refresh the file it points to.")
    if by_hand:
        extra.append(
            "A registration in a form palinode does not write is never rewritten: edit it "
            'by hand, registering the hook synchronously and writing the project directory as '
            '"${CLAUDE_PROJECT_DIR}".'
        )
    return CheckResult(
        name=_NAME,
        severity="warn",
        passed=False,
        message=(
            "Installed palinode hooks differ from what this release ships, so fixes "
            f"shipped in them are not running: {'; '.join(problems)}." + note_text
        ),
        remediation=" ".join([remediation, *extra]),
        tags=("fast",),
    )


def _explain(change: str) -> str:
    """What a pending registration repair means for the user."""
    if change.startswith("made the") and change.endswith("synchronous"):
        event = change.split()[2]
        return (
            f'palinode\'s {event} hook is registered with "async": true, which Claude Code '
            "cancels, so it does not finish" + (
                " and compactions leave no checkpoint" if event == "PostCompact" else ""
            )
        )
    if change == "quoted ${CLAUDE_PROJECT_DIR}":
        return (
            "a palinode hook command uses ${CLAUDE_PROJECT_DIR} unquoted, which the shell "
            "splits at a space, so in a project path with a space the hook does not run"
        )
    return f"a palinode registration needs a repair ({change})"
