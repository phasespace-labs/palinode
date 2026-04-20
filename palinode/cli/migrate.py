import click
from palinode.cli._format import console, print_result, get_default_format, OutputFormat


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
