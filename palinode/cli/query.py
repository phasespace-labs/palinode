import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.argument("file_path")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def history(file_path, fmt):
    """Show the git history of a specific memory file."""
    try:
        data = api_client.get_history(file_path)
        print_result(data, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error showing history: {str(e)}[/red]")

@click.command()
@click.argument("entity", required=False)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def entities(entity, fmt):
    """List entities or get files associated with an entity."""
    try:
        data = api_client.get_entities(entity)
        print_result(data, fmt=OutputFormat(fmt) if fmt else get_default_format())
    except Exception as e:
        console.print(f"[red]Error showing entities: {str(e)}[/red]")
