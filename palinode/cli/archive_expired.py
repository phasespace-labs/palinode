import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat


@click.command(name="archive-expired")
@click.option("--dry-run", is_flag=True, help="Preview which memories would be archived")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def archive_expired(dry_run, fmt):
    """Archive ephemeral memories whose `expires_at` has passed (ADR-015 §2.3)."""
    try:
        data = api_client.archive_expired(dry_run=dry_run)

        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
        else:
            verb = "Would archive" if data.get("dry_run") else "Archived"
            console.print(f"[green]{verb} {data.get('count', 0)} expired memory(ies).[/green]")
            for path in data.get("archived", []):
                console.print(f"  {path}")
    except Exception as e:
        console.print(f"[red]Error archiving expired memories: {str(e)}[/red]")
        click.Abort()
