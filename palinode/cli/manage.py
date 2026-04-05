import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def reindex(fmt):
    """Explicitly trigger absolute database rescans sequences."""
    try:
        result = api_client.reindex()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error reindexing: {str(e)}[/red]")

@click.command(name="rebuild-fts")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def rebuild_fts(fmt):
    """Rebuild the BM25 full-text search index."""
    try:
        result = api_client.rebuild_fts()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error rebuilding FTS: {str(e)}[/red]")

@click.command(name="split-layers")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def split_layers(fmt):
    """Split core files into layers."""
    try:
        result = api_client.split_layers()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error splitting layers: {str(e)}[/red]")

@click.command(name="bootstrap-ids")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def bootstrap_ids(fmt):
    """Bootstrap fact IDs."""
    try:
        result = api_client.bootstrap_ids()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error bootstrapping IDs: {str(e)}[/red]")

@click.command(name="migrate-mem0")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def migrate_mem0(fmt):
    """Backfill from Mem0/Qdrant."""
    try:
        result = api_client.migrate_mem0()
        print_result(result, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error migrating: {str(e)}[/red]")
