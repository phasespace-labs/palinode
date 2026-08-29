"""CLI command: palinode depends — milestone dependency traversal."""
from __future__ import annotations

import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat


@click.command()
@click.argument("slug", required=False)
@click.option(
    "--unblocked",
    is_flag=True,
    default=False,
    help="List all slugs whose every depends_on is done (ready to start).",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def depends(slug, unblocked, fmt):
    """Show the dependency tree for a milestone or task slug.

    Given SLUG (e.g. milestone/M1 or task/foo), returns its depends_on,
    blocks, and parallel_with neighbours, plus whether it is unblocked.

    Use --unblocked to list everything that is ready to work on right now
    (every depends_on is status=done).
    """
    output_fmt = OutputFormat(fmt) if fmt else get_default_format()

    if unblocked:
        try:
            data = api_client.depends_unblocked()
            if output_fmt == OutputFormat.JSON:
                print_result(data, fmt=output_fmt)
            else:
                if not data:
                    console.print("[yellow]No unblocked items found.[/yellow]")
                    return
                console.print("[bold green]Unblocked items:[/bold green]")
                for item in data:
                    status_str = f" ({item['status']})" if item.get("status") else ""
                    console.print(f"  [cyan]{item['slug']}[/cyan]{status_str}")
        except Exception as e:
            console.print(f"[red]Error fetching unblocked items: {e}[/red]")
            raise SystemExit(1)
        return

    if not unblocked and not slug:
        raise click.UsageError("SLUG is required unless --unblocked is passed.")

    try:
        data = api_client.depends(slug)
        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
            return

        # Human-readable output
        unblocked_label = (
            "[bold green]UNBLOCKED[/bold green]"
            if data.get("unblocked")
            else "[bold red]BLOCKED[/bold red]"
        )
        console.print(f"\n[bold]{data['slug']}[/bold] — {unblocked_label}")

        def _render_entries(label: str, entries: list[dict]) -> None:
            if not entries:
                return
            console.print(f"\n[bold]{label}:[/bold]")
            for e in entries:
                found_tag = "" if e.get("found") else " [dim](orphan)[/dim]"
                status_str = f" [dim]status={e['status']}[/dim]" if e.get("status") else ""
                console.print(f"  [cyan]{e['slug']}[/cyan]{status_str}{found_tag}")

        _render_entries("depends_on", data.get("depends_on", []))
        _render_entries("blocks", data.get("blocks", []))
        _render_entries("parallel_with", data.get("parallel_with", []))

        orphans = data.get("orphans", [])
        if orphans:
            console.print("\n[yellow]Orphan refs (no matching memory file):[/yellow]")
            for o in orphans:
                console.print(f"  [yellow]{o}[/yellow]")

        console.print()

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise SystemExit(1)
