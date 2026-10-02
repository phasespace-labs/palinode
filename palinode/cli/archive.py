import json

import click

from palinode.cli._api import api_client
from palinode.cli._format import console, get_default_format, OutputFormat
from palinode.core.lifecycle_render import render_lifecycle_preview, render_retained_copies


@click.command(name="archive")
@click.argument("file_path")
@click.option("--reason", default=None, help="Why this memory is being retired")
@click.option(
    "--superseded-by",
    "superseded_by",
    default=None,
    help="Slug or path of the memory that replaces this one (makes it a SUPERSEDE)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview the delta, retained copies and recovery command; write nothing",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def archive(file_path, reason, superseded_by, dry_run, fmt):
    """Retire a specific memory: ARCHIVE it, or SUPERSEDE it with a replacement.

    Sets `status: archived` so the memory leaves default recall, records the
    reason in the `-history.md` audit sibling, and commits both. Never
    hard-deletes — the content stays on disk, in git, and in the index.

    Memories that quote, cite or link this one are not reached: they are
    listed, never changed, and stay in default recall.
    """
    try:
        data = api_client.archive(
            file_path, reason=reason, superseded_by=superseded_by, dry_run=dry_run
        )
    except Exception as e:
        console.print(f"[red]Error archiving {file_path}: {str(e)}[/red]")
        raise SystemExit(1)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        # click.echo (not console.print): machine-readable JSON must not pass
        # through Rich's highlighter, which would inject ANSI colour codes.
        click.echo(json.dumps(data, indent=2))
        return

    if data.get("dry_run"):
        for line in render_lifecycle_preview(data):
            click.echo(line)
        return
    if data.get("status") == "already_archived":
        console.print(f"[yellow]{data.get('file')} is already archived — no change.[/yellow]")
        for line in render_retained_copies(data.get("retained_copies")):
            click.echo(line)
        return
    successor = data.get("superseded_by")
    verb = f"Superseded by {successor}" if successor else "Archived"
    console.print(f"[green]{verb}: {data.get('file')}[/green]")
    if data.get("history_file"):
        console.print(f"  history: {data['history_file']}")
    console.print(f"  chunks suppressed from recall: {data.get('chunks_updated', 0)}")
    for line in render_retained_copies(data.get("retained_copies")):
        click.echo(line)
