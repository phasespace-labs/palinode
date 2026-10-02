import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat
from palinode.core.parity import CATEGORIES

@click.command()
@click.option("--category", type=click.Choice(CATEGORIES))
@click.option("--core", "core_only", is_flag=True, help="Only show core memory files")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]))
def list_cmd(category, core_only, fmt):
    """List memory files."""
    try:
        # Browse, not injection: a superseded or archived core memory stays
        # visible here (marked with why it no longer acts), while the
        # injection consumers of the same endpoint get it withheld.
        data = api_client.list_files(
            category=category, core_only=core_only, include_retired_core=True
        )

        output_fmt = OutputFormat(fmt) if fmt else get_default_format()

        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
        else:
            if not data:
                console.print("No files found.")
                return

            # Group by category for text output
            from collections import defaultdict
            grouped = defaultdict(list)
            core_count = 0
            for item in data:
                grouped[item["category"]].append(item)
                if item["core"]:
                    core_count += 1

            for cat, items in grouped.items():
                console.print(f"[bold blue]{cat}/[/bold blue]")
                for item in items:
                    name = item["file"].split("/")[-1]
                    summary = item["summary"]
                    # Both tags are escaped (``\[``) — an unescaped ``[core]``
                    # is rich markup, and rich renders markup it doesn't know
                    # as nothing at all, so the tag never reached the terminal.
                    if item.get("core_retired_reason"):
                        core_tag = (
                            f" [yellow]\\[retired: {item['core_retired_reason']}][/yellow]"
                        )
                    else:
                        core_tag = " [bold green]\\[core][/bold green]" if item["core"] else ""
                    if summary:
                        console.print(f"  {name:<16} — {summary}{core_tag}")
                    else:
                        console.print(f"  {name:<16}{core_tag}")
                console.print("")

            console.print(f"[bold]{len(data)} files ({core_count} core)[/bold]")

    except Exception as e:
        console.print(f"[red]Error listing files: {str(e)}[/red]")
        raise click.Abort()
