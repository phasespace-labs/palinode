import json

import click

from palinode.cli._api import api_client
from palinode.cli._format import console, get_default_format, OutputFormat


@click.command()
@click.argument("file_path")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default=None,
    help="Output format. Defaults to text on a TTY, JSON when piped.",
)
def trace(file_path, fmt):
    """Compose the full provenance lineage of a memory file.

    Joins source citations, git blame/history, the supersession trail, typed
    contradiction/evidence links, and the retrieval log into one lineage view.
    Rows whose provenance gap is not built yet render an honest "not yet
    captured" placeholder. JSON mode emits the structured object the review UI
    consumes.
    """
    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    try:
        data = api_client.trace(file_path)
    except Exception as e:
        console.print(f"[red]Error tracing: {str(e)}[/red]")
        return

    if output_fmt == OutputFormat.JSON:
        # click.echo (not console.print): machine-readable JSON must not pass
        # through Rich's highlighter, which would inject ANSI colour codes.
        click.echo(json.dumps(data, indent=2))
        return

    from palinode.core.trace import format_trace_text

    # markup=False: the render carries [status] and [fact:id] brackets that
    # Rich would otherwise consume as style tags (same guard as blame --claims).
    console.print(format_trace_text(data), markup=False)
