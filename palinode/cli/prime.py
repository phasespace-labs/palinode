import os

import click

from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat


@click.command()
@click.option(
    "--cwd",
    default=None,
    help=(
        "Working directory used to resolve the project scope "
        "(default: current directory)"
    ),
)
@click.option(
    "-p",
    "--project",
    default=None,
    help="Explicit project slug or entity ref; overrides cwd resolution",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def prime(cwd, project, fmt):
    """Show the session-start context digest for this project.

    The same digest the SessionStart hook warms and the MCP session-init
    tool returns: core memories, recent decisions, and open action items
    for the resolved scope.
    """
    try:
        data = api_client.context_prime(cwd=cwd or os.getcwd(), project=project)
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
        else:
            from palinode.core.context_prime import format_context_digest

            # markup=False: digest lines carry [file.md] refs that Rich would
            # otherwise consume as style tags.
            console.print(format_context_digest(data), markup=False)
    except Exception as e:
        console.print(f"[red]Error priming context: {str(e)}[/red]")
