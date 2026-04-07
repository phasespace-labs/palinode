import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.option("--nightly", is_flag=True, help="Run lightweight nightly pass (today only, UPDATE/SUPERSEDE)")
@click.option("--dry-run", is_flag=True, help="Preview changes without applying")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def consolidate(nightly, dry_run, fmt):
    """Run or preview memory compaction (weekly full or --nightly lightweight)."""
    try:
        data = api_client.consolidate(dry_run=dry_run, nightly=nightly)
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        
        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
        else:
            if dry_run:
                console.print("[cyan]Previewing consolidation...[/cyan]")
                for change in data.get("proposed_changes", []):
                    console.print(f"  [{change['type']}] {change['file']}")
            else:
                console.print("[green]Consolidation complete.[/green]")
                console.print(f"Stats: {data.get('stats', 'none')}")
                
    except Exception as e:
        console.print(f"[red]Error consolidating: {str(e)}[/red]")
        click.Abort()
