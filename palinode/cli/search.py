import os
import click
from palinode.cli._api import api_client
from palinode.cli._format import print_result, console, OutputFormat, get_default_format
from palinode.core.config import config
from rich.panel import Panel


def _cli_resolve_context() -> list[str] | None:
    """Resolve ambient project context from CWD for CLI."""
    if not config.context.enabled:
        return None
    explicit = os.environ.get("PALINODE_PROJECT")
    if explicit:
        return [explicit] if "/" in explicit else [f"project/{explicit}"]
    basename = os.path.basename(os.getcwd())
    if not basename:
        return None
    if basename in config.context.project_map:
        entity = config.context.project_map[basename]
        return [entity] if "/" in entity else [f"project/{entity}"]
    if config.context.auto_detect:
        return [f"project/{basename}"]
    return None


@click.command()
@click.argument("query")
@click.option("--limit", default=3, help="Number of results (default: 3)")
@click.option("--category", help="Filter by memory type/category")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
@click.option("--score/--no-score", default=False, help="Show relevance scores")
@click.option("--no-context", is_flag=True, help="Disable ambient context boost")
def search(query, limit, category, fmt, score, no_context):
    """Search memory by meaning or keyword."""
    try:
        context = None if no_context else _cli_resolve_context()
        results = api_client.search(query, limit=limit, category=category, context=context)
        
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
