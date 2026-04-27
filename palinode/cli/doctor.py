"""
palinode doctor — connectivity and health diagnostics.

Delegates all diagnostic logic to palinode.diagnostics; this module is
purely CLI plumbing: flag parsing, context construction, output routing,
and the ``--fix`` mode confirmation flow.
"""
from __future__ import annotations

import logging
import sys

import click
from rich.console import Console

from palinode.cli._format import console
from palinode.core.config import config as _default_config

_logger = logging.getLogger("palinode.doctor")

# Dedicated stderr console for diagnostic/log output that should not
# pollute stdout when the user has asked for machine-readable JSON.
_err_console = Console(stderr=True)


@click.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Output results as JSON.")
@click.option("--check", "check_name", default=None, metavar="NAME", help="Run a single named check.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show remediation for all checks, not just failures.")
@click.option("--fix", "fix_mode", is_flag=True, default=False, help="Apply safe automated fixes for failed checks (whitelist only).")
@click.option("--yes", "-y", "assume_yes", is_flag=True, default=False, help="Skip confirmation prompts when used with --fix (CI-friendly).")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="With --fix, report what would be fixed without applying anything.")
def doctor(
    as_json: bool,
    check_name: str | None,
    verbose: bool,
    fix_mode: bool,
    assume_yes: bool,
    dry_run: bool,
) -> None:
    """Connectivity and health check."""
    from palinode.diagnostics.runner import run_all, run_one
    from palinode.diagnostics.formatters import format_text, format_json
    from palinode.diagnostics.registry import get_fix
    from palinode.diagnostics.types import DoctorContext

    ctx = DoctorContext(config=_default_config)

    if check_name:
        try:
            results = [run_one(ctx, check_name)]
        except ValueError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)
    else:
        results = run_all(ctx)

    if as_json:
        click.echo(format_json(results))
        # In JSON mode we still honor exit semantics but skip --fix interactivity.
        if fix_mode:
            _err_console.print(
                "[yellow]Note:[/yellow] --fix is not applied in --json mode."
            )
        # In JSON mode, the diagnosis line goes to stderr so stdout stays
        # parseable for `palinode doctor --json | jq …`.
        _exit_for(results, to_stderr=True)
        return

    console.print("Palinode Diagnostics", style="bold underline")
    console.print()
    console.print(format_text(results, verbose=verbose))
    console.print()

    if fix_mode:
        _run_fix_mode(ctx, results, get_fix, assume_yes=assume_yes, dry_run=dry_run)
        # After --fix, exit based on the original results. We do not re-run
        # checks here because that could repeat expensive deep checks; instead,
        # the operator runs `palinode doctor` again to confirm the state.

    _exit_for(results)


def _exit_for(results: list, *, to_stderr: bool = False) -> None:
    """Print the diagnosis line and exit with the right code.

    When ``to_stderr`` is True (used in --json mode), the diagnosis line is
    routed to stderr so it does not pollute the JSON document on stdout.
    """
    out = _err_console if to_stderr else console
    failed = [r for r in results if not r.passed]
    critical = [r for r in failed if r.severity == "critical"]

    if critical:
        out.print("[bold red]Diagnosis: Critical issues detected.[/bold red]")
        sys.exit(1)
    elif failed:
        out.print("[bold yellow]Diagnosis: Warnings detected — review above.[/bold yellow]")
        sys.exit(1)
    else:
        out.print("[bold green]Diagnosis: All checks passed.[/bold green]")


def _run_fix_mode(
    ctx,
    results: list,
    get_fix_fn,
    *,
    assume_yes: bool,
    dry_run: bool,
) -> None:
    """Iterate failed checks; apply or skip according to the whitelist.

    Behavior matrix:
      - Passed check                       → skipped silently.
      - Failed check, no fix registered    → "no automated fix available";
                                              remediation re-printed for clarity.
      - Failed check, fix registered, dry-run → reports what *would* be done.
      - Failed check, fix registered, --yes  → applies without prompting.
      - Failed check, fix registered, default → prompts y/N per fix.

    The default of "decline" on EOF/empty input matches every other doctor
    confirmation flow in the project — safe by default.
    """
    failed = [r for r in results if not r.passed]
    if not failed:
        console.print("[bold green]Nothing to fix.[/bold green]")
        return

    console.print("[bold]Fix mode[/bold]" + (" (dry-run)" if dry_run else ""))
    console.print()

    applied_count = 0
    skipped_count = 0
    no_fix_count = 0

    for result in failed:
        fix_fn = get_fix_fn(result.name)
        if fix_fn is None:
            no_fix_count += 1
            console.print(
                f"  [red]✗[/red] {result.name} — no automated fix available; "
                "doctor does not move user data."
            )
            if result.remediation:
                console.print("    Suggested manual action:")
                for line in result.remediation.splitlines():
                    console.print(f"      {line}")
            console.print()
            continue

        # A fix is available.
        prompt_summary = _fix_prompt_summary(result.name, ctx)
        console.print(f"  [yellow]✗[/yellow] {result.name} — {result.message}")

        if dry_run:
            console.print(
                f"    [dim](dry-run)[/dim] Would apply fix: {prompt_summary}"
            )
            console.print()
            continue

        if not assume_yes:
            answer = click.prompt(
                f"    Apply fix? {prompt_summary} [y/N]",
                default="N",
                show_default=False,
            )
            if answer.strip().lower() not in ("y", "yes"):
                skipped_count += 1
                console.print("    [dim]Skipped.[/dim]")
                console.print()
                continue

        try:
            fix_result = fix_fn(ctx, result)
        except Exception as exc:  # defensive: a buggy fix should not crash doctor
            _logger.exception("doctor --fix: fix for %s raised", result.name)
            console.print(f"    [red]Fix failed:[/red] {exc}")
            console.print()
            continue

        if fix_result.applied:
            applied_count += 1
            console.print(f"    [green]✓[/green] {fix_result.message}")
            _logger.info(
                "doctor --fix: %s applied — %s", result.name, fix_result.message
            )
        else:
            skipped_count += 1
            console.print(f"    [dim]No-op:[/dim] {fix_result.message}")
        console.print()

    console.print(
        f"[bold]Fix summary:[/bold] {applied_count} applied, "
        f"{skipped_count} skipped, {no_fix_count} not fixable. "
        "Re-run 'palinode doctor' to verify."
    )


# ---------------------------------------------------------------------------
# Per-fix prompt phrasing.
# ---------------------------------------------------------------------------
# Centralized so the tone is consistent across all fixes.  Each phrase is a
# short verb phrase that completes "Apply fix? <phrase> [y/N]".

def _fix_prompt_summary(check_name: str, ctx) -> str:
    """Return the action verb-phrase shown in the confirmation prompt."""
    from pathlib import Path

    if check_name == "memory_dir_exists":
        target = Path(ctx.config.memory_dir).expanduser().resolve()
        return f"Create directory at {target}"

    if check_name == "audit_log_writable":
        memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()
        log_path = getattr(getattr(ctx.config, "audit", None), "log_path", "")
        if log_path and not Path(log_path).is_absolute():
            parent = (memory_dir / log_path).parent
            return f"Create audit log parent directory at {parent}"
        return "Create audit log parent directory"

    if check_name == "claude_md_palinode_block":
        candidate = Path.cwd() / "CLAUDE.md"
        return f"Append Palinode memory block to {candidate}"

    return "Apply registered fix"
