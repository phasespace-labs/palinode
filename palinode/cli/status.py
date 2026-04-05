import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat
from rich.table import Table

@click.command()
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def status(fmt):
    """Show system health and stats."""
    try:
        data = api_client.get_status()
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        
        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
        else:
            table = Table(title="Palinode Status", box=None, show_header=False)
            table.add_column("Key", style="cyan")
            table.add_column("Value")
            
            table.add_row("Memory dir:", data.get("memory_dir", "Unknown"))
            table.add_row("Files:", str(data.get("files", 0)))
            table.add_row("Chunks:", str(data.get("chunks", 0)))
            table.add_row("Embeddings:", f"{data.get('embedding_model', 'Unknown')} @ {data.get('embedding_url', 'Unknown')}")
            
            api_status = data.get("api_status", "unknown")
            api_color = "green" if api_status == "healthy" else "red"
            table.add_row("API:", f"{api_client.base_url} ([{api_color}]{api_status}[/{api_color}])")
            
            table.add_row("Last indexed:", data.get("last_indexed", "Unknown"))
            table.add_row("Git:", data.get("git_status", "Unknown"))
            
            console.print(table)
            
    except Exception as e:
        console.print(f"[red]Error getting status: {str(e)}[/red]")
        click.Abort()
