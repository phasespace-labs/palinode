import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.option("--days", type=int, default=7, help="Look back N days (default: 7)")
@click.option("--paths", type=str, default=None, help="Comma-separated path filters (e.g. projects/,decisions/)")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def diff(days, paths, fmt):
    """Show recent changes to memory files."""
    try:
        data = api_client.get_diff(days=days, paths=paths)
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        
        if output_fmt == OutputFormat.JSON:
            print_result(data, fmt=output_fmt)
        else:
            if not data.get("diff"):
                console.print("[yellow]No recent changes found.[/yellow]")
                return
            
            from rich.syntax import Syntax
            syntax = Syntax(data["diff"], "diff", theme="monokai", line_numbers=True)
            console.print(syntax)
            
    except Exception as e:
        console.print(f"[red]Error getting diff: {str(e)}[/red]")
        click.Abort()
