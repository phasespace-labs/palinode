import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat
from palinode.core.defaults import (
    TRIGGER_COOLDOWN_HOURS_DEFAULT,
    TRIGGER_THRESHOLD_DEFAULT,
)
from rich.table import Table

@click.group()
def trigger():
    """Manage auto-surface triggers."""
    pass

@trigger.command(name="add")
@click.argument("description")
@click.option("--file", "memory_file", required=True, help="Memory file to trigger")
@click.option(
    "--threshold",
    type=float,
    default=TRIGGER_THRESHOLD_DEFAULT,
    help=f"Similarity threshold (0.0 to 1.0; default {TRIGGER_THRESHOLD_DEFAULT})",
)
@click.option(
    "--cooldown-hours",
    type=int,
    default=TRIGGER_COOLDOWN_HOURS_DEFAULT,
    help=f"Hours to wait between firings of the same trigger "
         f"(default {TRIGGER_COOLDOWN_HOURS_DEFAULT})",
)
@click.option(
    "--trigger-id",
    "trigger_id",
    help="Stable trigger ID (UUID or slug).  Useful for re-creation / dedup.",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def trigger_add(description, memory_file, threshold, cooldown_hours, trigger_id, fmt):
    """Register a new auto-surface trigger."""
    try:
        result = api_client.trigger_add(
            description,
            memory_file,
            threshold=threshold,
            cooldown_hours=cooldown_hours,
            trigger_id=trigger_id,
        )
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        if output_fmt == OutputFormat.JSON:
            print_result(result, fmt=output_fmt)
        else:
            console.print(f"[green]Trigger added (id: {result['id']})[/green]")
    except Exception as e:
        console.print(f"[red]Error adding trigger: {str(e)}[/red]")
        raise click.Abort()

@trigger.command(name="list")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def trigger_list(fmt):
    """List registered triggers."""
    try:
        triggers = api_client.trigger_list()
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        if output_fmt == OutputFormat.JSON:
            print_result(triggers, fmt=output_fmt)
        else:
            if not triggers:
                console.print("[yellow]No triggers configured.[/yellow]")
                return
            
            table = Table(title="Palinode Triggers")
            table.add_column("ID", style="cyan")
            table.add_column("Description")
            table.add_column("Target File")
            table.add_column("Threshold")
            
            for t in triggers:
                table.add_row(
                    t['id'], 
                    t['description'], 
                    t.get('memory_file', t.get('file', '')),
                    f"{t['threshold']:.2f}"
                )
            
            console.print(table)
    except Exception as e:
        console.print(f"[red]Error listing triggers: {str(e)}[/red]")
        raise click.Abort()

@trigger.command(name="remove")
@click.argument("trigger_id")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def trigger_remove(trigger_id, fmt):
    """Remove a trigger by ID."""
    try:
        result = api_client.trigger_remove(trigger_id)
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        if output_fmt == OutputFormat.JSON:
            print_result(result, fmt=output_fmt)
        else:
            console.print("[green]Trigger removed.[/green]")
    except Exception as e:
        console.print(f"[red]Error removing trigger: {str(e)}[/red]")
        raise click.Abort()
