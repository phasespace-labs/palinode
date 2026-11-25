"""palinode hooks — keep installed hook scripts and registrations current.

``palinode init`` copies the hook scripts once and skips them on every later
run, and a manual install copies them once by hand. Nothing refreshed a stale
copy, so a release that fixed a hook never reached a machine that already had
one. ``palinode hooks update`` refreshes them, and repairs palinode's own
registrations; discovery, comparison and repair live in
:mod:`palinode.hooks.installed`, shared with doctor's ``hooks_current`` check
so the diagnosis and the fix agree on what is installed.

CLI-only by design: the hooks live on the machine running the agent harness,
which is often not the machine running the palinode server. An MCP or REST
twin would inspect the server's filesystem and report on hooks nobody runs.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import click

from palinode.cli._format import OutputFormat, console, get_default_format, print_result
from palinode.hooks.codex import CODEX, TRUST_NOTE
from palinode.hooks.installed import (
    FORCE_REFRESHABLE,
    REFRESHABLE,
    STATE_CURRENT,
    STATE_EDITED,
    STATE_FOREIGN,
    STATE_MISSING,
    STATE_SYMLINK,
    STATE_UNREADABLE,
    UPDATE_COMMAND,
    apply_plan,
    describe_version,
    hook_diff,
    hook_statuses,
    plan_registrations,
    refresh_hook,
    registrations_to_fix_by_hand,
    settings_files,
)

_SKIP_REASONS = {
    STATE_CURRENT: "already matches the packaged copy",
    STATE_SYMLINK: "symlink — refresh the file it points to",
    STATE_FOREIGN: "not a palinode script despite its name — left alone",
    STATE_UNREADABLE: "could not be read — left alone",
    STATE_EDITED: "edited: matches no palinode release; --force replaces it, keeping a backup",
}


@click.group()
def hooks() -> None:
    """Manage the palinode hook scripts Claude Code, Codex, Cursor and Antigravity run on this machine."""


@hooks.command(name="update")
@click.option("--dry-run", is_flag=True, help="Show what would change, with a diff, and write nothing.")
@click.option(
    "--project-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Project whose .claude/settings*.json, .codex/ hooks, .cursor/hooks.json and .agents/hooks.json to read (default: current directory).",
)
@click.option(
    "--force", is_flag=True,
    help="Also replace edited hook scripts, reset edited registrations and restore removed ones, keeping backups.",
)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None)
def hooks_update(dry_run: bool, project_dir: Path | None, force: bool, fmt: str | None) -> None:
    """Refresh installed palinode hooks to the packaged copies.

    Finds every palinode hook registered in ~/.claude/settings.json,
    .claude/settings.json and .claude/settings.local.json, in Codex's
    hooks.json and config.toml (user $CODEX_HOME or ~/.codex, and the
    project's .codex/), and in the project's .cursor/hooks.json and
    .agents/hooks.json, and replaces each stale copy with the one this palinode
    release ships, keeping the old one as <name>.bak-<timestamp> beside it. A hook registered but absent is
    installed. A copy matching no palinode release is someone's edit: it is
    reported and replaced only with --force. Hooks that are not palinode's
    are never touched; symlinked hooks are reported, not rewritten.

    Palinode's own registrations, in a form palinode writes, are repaired
    with a backup when an earlier release wrote them: an async handler
    (which Claude Code cancels) is made synchronous, an unquoted
    ${CLAUDE_PROJECT_DIR} (which splits at a space in the project path) is
    quoted, and an old timeout is reset. A registration a newer release
    added, whose script was never installed, is added with its script. A
    registration whose timeout you changed, or one you removed (its script is
    still there), is reported and changed only with --force. A registration
    in another form with a known defect is reported for you to fix by hand.
    Re-running when everything is current changes nothing.
    """
    home = Path.home()
    project = (project_dir or Path.cwd()).resolve()
    # Both are read before anything is written: a refreshed script must not
    # change what the registration plan sees.
    statuses = hook_statuses(home, project)
    plans = plan_registrations(home, project)
    now = datetime.now()
    writable = FORCE_REFRESHABLE if force else REFRESHABLE

    results: list[dict[str, object]] = []
    for status in statuses:
        entry: dict[str, object] = asdict(status)
        if status.state in writable:
            entry["action"] = "would update" if dry_run else "updated"
            if status.state == STATE_MISSING:
                entry["action"] = "would install" if dry_run else "installed"
            if dry_run:
                entry["diff"] = hook_diff(status)
                entry["backup"] = None
            else:
                entry["backup"] = refresh_hook(status, now)
        else:
            entry["action"] = "skipped"
            entry["reason"] = _SKIP_REASONS[status.state]
        results.append(entry)

    registrations: list[dict[str, object]] = []
    kept: list[dict[str, object]] = []
    notes: list[str] = []
    for plan in plans:
        if plan.writes(force):
            planned = plan.repairs + [f"registered {r.event}" for r in plan.new]
            if force:
                planned += [f"reset the edited {e.event} registration" for e in plan.edited]
                planned += [f"registered {r.event}" for r in plan.removed + plan.not_offered]
            backup, changes = None, planned
            if not dry_run:
                backup, changes = apply_plan(plan, project, force=force, now=now)
                if plan.spec is CODEX and changes and TRUST_NOTE not in notes:
                    notes.append(TRUST_NOTE)
            registrations.append({
                "path": str(plan.path),
                "action": "would repair" if dry_run else "repaired",
                "changes": changes,
                "backup": backup,
            })
        if not force:
            for e in plan.edited:
                kept.append({
                    "path": str(plan.path), "event": e.event, "state": STATE_EDITED,
                    "reason": f"timeout {e.timeout}{', async' if e.is_async else ''} is not one palinode "
                              "wrote; --force resets it",
                })
            for r in plan.removed:
                kept.append({
                    "path": str(plan.path), "event": r.event, "state": "removed",
                    "reason": f"not registered, though {r.script} is installed; --force restores it",
                })
            for r in plan.not_offered:
                kept.append({
                    "path": str(plan.path), "event": r.event, "state": "not registered",
                    "reason": f"palinode {r.since} added it; this file is not one palinode init "
                              "writes, so --force adds it",
                })
    by_hand = [
        {"path": str(fix.path), "command": fix.command, "problem": fix.problem}
        for fix in registrations_to_fix_by_hand(home, project)
    ]

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    checked = [str(p) for p in settings_files(home, project)]
    if output_fmt == OutputFormat.JSON:
        print_result(
            {
                "dry_run": dry_run,
                "settings_checked": checked,
                "hooks": results,
                "registrations": registrations,
                "registrations_kept": kept,
                "fix_by_hand": by_hand,
                "notes": notes,
            },
            fmt=output_fmt,
        )
        return

    if not results and not by_hand and not registrations and not kept:
        console.print("No palinode hooks registered in:")
        for path in checked:
            console.print(f"  {path}", markup=False, highlight=False)
        return
    for entry in results:
        action = str(entry["action"])
        line = f"{action}: {entry['path']}"
        if entry["state"] in writable:
            line += (
                f" ({describe_version(entry['installed_version'])} -> "
                f"{describe_version(entry['packaged_version'])})"
            )
            if entry.get("backup"):
                line += f", backup {entry['backup']}"
        else:
            line += f" — {entry['reason']}"
        console.print(line, markup=False, highlight=False)
        if dry_run and entry.get("diff"):
            click.echo(str(entry["diff"]))
    for reg in registrations:
        line = f"{reg['action']} registration: {reg['path']} ({'; '.join(reg['changes'])})"
        if reg.get("backup"):
            line += f", backup {reg['backup']}"
        console.print(line, markup=False, highlight=False)
    for item in kept:
        console.print(
            f"kept registration: {item['path']} {item['event']} — {item['reason']}",
            markup=False, highlight=False,
        )
    for fix in by_hand:
        console.print(
            f"fix by hand: {fix['path']}: `{fix['command']}` {fix['problem']}; "
            "palinode rewrites only the registrations it writes",
            markup=False, highlight=False,
        )
    for note in notes:
        console.print(note, markup=False, highlight=False)
    if dry_run and (registrations or any(e["state"] in writable for e in results)):
        console.print(f"Dry run — nothing written. Run `{UPDATE_COMMAND}` to apply.", markup=False)
