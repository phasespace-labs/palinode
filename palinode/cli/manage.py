import click
from palinode.cli._api import api_client, HTTPStatusError
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def reindex(fmt):
    """Rescan every memory file and re-embed only what changed."""
    try:
        result = api_client.reindex()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except HTTPStatusError as e:
        if e.response.status_code == 409:
            console.print("[yellow]Reindex already running. Check 'palinode status' for progress.[/yellow]")
        else:
            console.print(f"[red]Error reindexing: {str(e)}[/red]")
            raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error reindexing: {str(e)}[/red]")
        raise SystemExit(1)

@click.command(name="rebuild-fts")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def rebuild_fts(fmt):
    """Rebuild the BM25 full-text search index."""
    try:
        result = api_client.rebuild_fts()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error rebuilding FTS: {str(e)}[/red]")
        raise SystemExit(1)

@click.command(name="split-layers")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def split_layers(fmt):
    """Split core files into layers."""
    try:
        result = api_client.split_layers()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error splitting layers: {str(e)}[/red]")
        raise SystemExit(1)

@click.command(name="bootstrap-ids")
@click.option("--file", "file_path", metavar="REL_PATH",
              help="Tag one memory file, relative to the store "
                   "(e.g. projects/palinode-status.md). Default: the whole store.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def bootstrap_ids(file_path, fmt):
    """Bootstrap fact IDs.

    With no options, walks people/, projects/, decisions/ and insights/. With
    --file, tags exactly that file — which is what `palinode doctor` asks for
    when it names a consolidation target carrying no markers. Idempotent either
    way, and committed with provenance.
    """
    try:
        if file_path:
            result = api_client.bootstrap_ids_file(file_path)
        else:
            result = api_client.bootstrap_ids()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except HTTPStatusError as e:
        status = e.response.status_code
        if status in (400, 403):
            console.print(
                f"[red]Refused: {file_path!r} does not resolve inside the memory "
                f"store.[/red]"
            )
        elif status == 404:
            console.print(f"[red]No such memory file: {file_path}[/red]")
        else:
            console.print(f"[red]Error bootstrapping IDs: {str(e)}[/red]")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error bootstrapping IDs: {str(e)}[/red]")
        raise SystemExit(1)
