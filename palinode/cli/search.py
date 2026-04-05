import click
from palinode.cli._api import api_client
from palinode.cli._format import print_result, console, OutputFormat, get_default_format
from rich.panel import Panel

@click.command()
@click.argument("query")
@click.option("--top-k", default=3, help="Number of results (default: 3)")
@click.option("--type", "type_filter", help="Filter by memory type")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
@click.option("--score/--no-score", default=False, help="Show relevance scores")
def search(query, top_k, type_filter, fmt, score):
    """Search memory by meaning or keyword."""
    try:
        results = api_client.search(query, top_k=top_k, type_filter=type_filter)
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        
        if output_fmt == OutputFormat.JSON:
            print_result(results, fmt=output_fmt)
        else:
            if not results:
                console.print("[yellow]No results found.[/yellow]")
                return
            
            for res in results:
                score_str = f"[{res['score']:.2f}] " if score else ""
                title = res.get("file", "Untitled")
                content = res.get("content", "").strip()[:200] + "..." if len(res.get("content", "")) > 200 else res.get("content", "")
                
                console.print(f"[bold blue]{score_str}{title}[/bold blue]")
                console.print(f"  {content}")
                console.print()
                
    except Exception as e:
        console.print(f"[red]Error searching memory: {str(e)}[/red]")
        click.Abort()
