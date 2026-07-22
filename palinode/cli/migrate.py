import click
from palinode.cli._format import console, print_result, get_default_format, OutputFormat
from palinode.migration.frontmatter_backfill import VALID_DAILY_MODES


@click.group()
def migrate():
    """Migration tools for importing memories from external sources."""
    pass


def _interactive_review(sections: list[dict]) -> list[dict]:
    """Prompt the user to confirm or change each section's detected type."""
    valid_types = ("person", "decision", "project", "insight")
    reviewed: list[dict] = []
    console.print("\n[bold]Review detected types[/bold]  (enter to accept, type name to change, 's' to skip)\n")
    for i, sec in enumerate(sections, 1):
        preview = sec["body"][:80].replace("\n", " ")
        console.print(f"  [cyan]{i}.[/cyan] [bold]{sec['heading']}[/bold]")
        console.print(f"     {preview}{'…' if len(sec['body']) > 80 else ''}")
        console.print(f"     detected: [yellow]{sec['type']}[/yellow]")
        answer = click.prompt(
            "     accept/change/skip",
            default=sec["type"],
            show_default=False,
        ).strip().lower()
        if answer == "s":
            console.print("     [dim]skipped[/dim]")
            continue
        if answer in valid_types:
            sec["type"] = answer
        elif answer:
            console.print(f"     [red]unknown type '{answer}', keeping {sec['type']}[/red]")
        reviewed.append(sec)
    console.print()
    return reviewed


@migrate.command(name="openclaw")
@click.argument("memory_file", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be imported without writing files")
@click.option("--review", is_flag=True, default=False, help="Interactively review and override detected types")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), help="Output format")
def openclaw(memory_file: str, dry_run: bool, review: bool, fmt: str | None) -> None:
    """Import a MEMORY.md from OpenClaw into Palinode.

    Parses each ## section into a separate memory file with heuristic
    type detection (person / decision / project / insight).

    Use --review to interactively confirm or change each section's type
    before writing.

    MEMORY_FILE: Path to the MEMORY.md file to import.
    """
    from palinode.migration.openclaw import run_migration

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    review_cb = _interactive_review if review else None

    try:
        result = run_migration(source_path=memory_file, dry_run=dry_run, review_callback=review_cb)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)
    except Exception as exc:
        console.print(f"[red]Migration failed:[/red] {exc}")
        raise SystemExit(1)

    if output_fmt == OutputFormat.JSON:
        print_result(result, fmt=output_fmt)
        return

    # Human-readable output
    prefix = "[yellow](dry-run)[/yellow] " if dry_run else ""
    console.print(f"\n{prefix}[bold]OpenClaw migration complete[/bold]")
    console.print(f"  Sections found:  {result['sections_found']}")
    console.print(f"  Files created:   {len(result['files_created'])}")
    console.print(f"  Files skipped:   {len(result['files_skipped'])} (duplicate content)")

    if result["files_created"]:
        console.print("\n[green]Created:[/green]")
        for fp in result["files_created"]:
            console.print(f"  {fp}")

    if result["files_skipped"]:
        console.print("\n[dim]Skipped (identical content already exists):[/dim]")
        for fp in result["files_skipped"]:
            console.print(f"  {fp}")

    if result.get("log_file"):
        console.print(f"\n[dim]Migration log:[/dim] {result['log_file']}")

    if dry_run:
        console.print(
            "\n[yellow]Dry run — no files written. Remove --dry-run to import.[/yellow]"
        )


@migrate.command(name="frontmatter")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Write and git-commit the backfill. Without it, nothing is written.",
)
@click.option(
    "--daily-mode",
    type=click.Choice(list(VALID_DAILY_MODES)),
    default="skip",
    show_default=True,
    help=(
        "How to treat daily/ notes. 'skip' leaves them alone — a daily note is "
        "an append-only log, not a memory, and is exempt from the "
        "required-frontmatter contract; 'minimal' fills id + category + dates "
        "anyway, but never a type:."
    ),
)
@click.option(
    "--no-commit",
    "no_commit",
    is_flag=True,
    default=False,
    help="With --apply, write the files but skip the per-file git commit.",
)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), help="Output format")
def frontmatter(apply_changes: bool, daily_mode: str, no_commit: bool, fmt: str | None) -> None:
    """Backfill missing required frontmatter on legacy memory files.

    Fills only fields that are absent, only from an honest derivation
    (directory, filename, a legacy field of identical meaning, or git history) —
    a field with no such source is left absent and reported, never guessed.
    Existing values are never overwritten, so re-running is a no-op.

    Dry-run by default; pass --apply to write. Each applied file is written
    through the mutation choke point and committed on its own, with the
    derivation of every value recorded in the commit body.

    Operates on the configured memory dir — set PALINODE_DIR to target another
    store.
    """
    from palinode.migration.frontmatter_backfill import BackfillError, run_backfill

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()

    try:
        result = run_backfill(
            apply=apply_changes,
            daily_mode=daily_mode,
            commit=not no_commit,
        )
    except BackfillError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    if output_fmt == OutputFormat.JSON:
        print_result(result, fmt=output_fmt)
        return

    prefix = "[yellow](dry-run)[/yellow] " if result["dry_run"] else ""
    console.print(f"\n{prefix}[bold]Frontmatter backfill[/bold]")
    console.print(f"  Files scanned:      {result['scanned']}")
    console.print(f"  Already conformant: {result['conformant']}")
    console.print(f"  Needing fields:     {len(result['files'])}")
    console.print(f"  Excluded:           {len(result['excluded'])}")
    if result["unreadable"]:
        console.print(f"  [red]Unreadable:         {len(result['unreadable'])}[/red]")

    for entry in result["files"]:
        console.print(f"\n  [cyan]{entry['path']}[/cyan]")
        for fill in entry["fills"]:
            console.print(
                f"    + {fill['field']}: {fill['value']} [dim](source: {fill['source']})[/dim]"
            )
        for item in entry["undeliverable"]:
            console.print(f"    [yellow]? {item['field']}: not derivable — {item['reason']}[/yellow]")
        for item in entry["withheld"]:
            console.print(f"    [dim]· {item['field']} withheld — {item['reason']}[/dim]")
        for note in entry["notes"]:
            console.print(f"    [dim]note: {note}[/dim]")

    if result["excluded"]:
        console.print("\n[dim]Excluded (not memory files, or out of scope):[/dim]")
        for item in result["excluded"]:
            console.print(f"  [dim]{item['path']} — {item['reason']}[/dim]")

    for item in result["unreadable"]:
        console.print(f"\n[red]Unreadable:[/red] {item['path']} — {item['error']}")

    if result["dry_run"]:
        console.print(
            "\n[yellow]Dry run — no files written. Re-run with --apply to write.[/yellow]"
        )
    else:
        console.print(
            f"\n[green]Wrote {len(result['files_written'])} file(s); "
            f"{result['commits']} commit(s).[/green]"
        )
