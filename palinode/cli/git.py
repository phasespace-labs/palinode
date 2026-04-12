import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format

@click.command()
@click.argument("file_path")
@click.option("--search", help="Filter to matching lines")
def blame(file_path, search):
    """Show when lines were changed."""
    try:
        data = api_client.blame(file_path, search)
        console.print(data)
    except Exception as e:
        console.print(f"[red]Error blaming: {str(e)}[/red]")

@click.command()
@click.argument("file_path")
@click.option("--limit", type=int, default=20, help="Max commits to show")
def history(file_path, limit):
    """Show file change history with diff stats."""
    try:
        data = api_client.get_history(file_path, limit)
        print_result(data, fmt=get_default_format())
    except Exception as e:
        console.print(f"[red]Error showing history: {str(e)}[/red]")

@click.command()
@click.argument("file_path")
@click.argument("commit")
@click.option("--execute", is_flag=True, help="Actually apply (default: dry run)")
def rollback(file_path, commit, execute):
    """Revert a file."""
    try:
        data = api_client.rollback(file_path, commit, dry_run=not execute)
        console.print(data)
    except Exception as e:
        console.print(f"[red]Error rolling back: {str(e)}[/red]")

@click.command()
def push():
    """Sync to GitHub."""
    try:
        data = api_client.push()
        console.print(data)
    except Exception as e:
        console.print(f"[red]Error pushing: {str(e)}[/red]")
