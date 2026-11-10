import click
from rich.markup import escape

from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat
from palinode.cli._options import validate_where
from palinode.core.parity import CATEGORIES


@click.command()
@click.option("--category", type=click.Choice(CATEGORIES))
@click.option("--core", "core_only", is_flag=True, help="Only show core memory files")
@click.option(
    "--where",
    multiple=True,
    callback=validate_where,
    help="Only files whose frontmatter has key=value (e.g. status=open). Repeat to require all.",
)
@click.option("--include-inbox", is_flag=True, help="Also list inbox/, where ActionItems are saved")
@click.option(
    "--project",
    help=(
        "List as a session scoped to this project does: its records and ones "
        "naming no project. Default: every project's records."
    ),
)
@click.option(
    "--include-other-projects",
    is_flag=True,
    help="With --project: also list records tagged to other projects, labelled with their project.",
)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]))
def list_cmd(category, core_only, where, include_inbox, project, include_other_projects, fmt):
    """List memory files."""
    try:
        # Browse, not injection: a superseded or archived core memory stays
        # visible here (marked with why it no longer acts), while the
        # injection consumers of the same endpoint get it withheld.
        data, withheld = api_client.list_files_scoped(
            category=category,
            core_only=core_only,
            include_retired_core=True,
            where=list(where) or None,
            include_inbox=include_inbox or None,
            project=project or None,
            include_other_projects=include_other_projects or None,
        )

        from palinode.core.scoring import (
            describe_other_projects_withheld,
            other_project_label,
            other_projects_heading,
            split_other_projects,
        )

        # Read by a person, so the line names the flag, as CLI search's does.
        withheld_line = describe_other_projects_withheld(
            withheld, delivered=len(data), project=project, human=True,
        )

        output_fmt = OutputFormat(fmt) if fmt else get_default_format()

        if output_fmt == OutputFormat.JSON:
            # stdout stays the array a script parses; a labelled row carries
            # ``other_project``, and the withheld count goes to stderr.
            print_result(data, fmt=output_fmt)
            if withheld_line:
                click.echo(withheld_line, err=True)
        else:
            if not data:
                console.print("No files found.")
                if withheld_line:
                    console.print("[yellow]" + escape(withheld_line) + "[/yellow]")
                return

            # Opted-in records from other projects print after this project's
            # own, under their own heading, as CLI search prints them.
            own_rows, other_rows = split_other_projects(data)

            # Group by category for text output
            from collections import defaultdict
            grouped = defaultdict(list)
            core_count = 0
            for item in own_rows:
                grouped[item["category"]].append(item)
            for item in data:
                if item["core"]:
                    core_count += 1

            for cat, items in grouped.items():
                console.print(f"[bold blue]{cat}/[/bold blue]")
                for item in items:
                    console.print(_row_line(item))
                console.print("")

            if other_rows:
                console.print(
                    "[bold yellow]" + escape(other_projects_heading(project)) + "[/bold yellow]"
                )
                for item in other_rows:
                    label = escape(other_project_label(item["other_project"], project))
                    console.print(f"[yellow]{label}[/yellow]" + _row_line(item, name=item["file"]))
                console.print("")

            if withheld_line:
                console.print("[yellow]" + escape(withheld_line) + "[/yellow]")
            console.print(f"[bold]{len(data)} files ({core_count} core)[/bold]")

    except Exception as e:
        console.print(f"[red]Error listing files: {str(e)}[/red]")
        raise click.Abort()


def _row_line(item: dict, name: str | None = None) -> str:
    name = name or item["file"].split("/")[-1]
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
        return f"  {name:<16} — {summary}{core_tag}"
    return f"  {name:<16}{core_tag}"
