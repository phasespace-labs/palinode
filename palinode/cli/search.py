import os
import click
from palinode.cli._api import api_client
from palinode.cli._format import print_result, console, OutputFormat, get_default_format
from palinode.core.config import config
from rich.panel import Panel


def _cli_resolve_context() -> list[str] | None:
    """Resolve ambient project context from CWD for CLI (ADR-008)."""
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
@click.option(
    "--category",
    type=click.Choice(["people", "projects", "decisions", "insights", "research"]),
    help="Filter by memory directory (people, projects, decisions, insights, research)",
)
@click.option(
    "--threshold",
    type=float,
    help="Similarity threshold (0.0–1.0).  Higher = stricter; default from config.",
)
@click.option(
    "--since-days",
    type=int,
    help="Only return memories created/updated in the last N days.",
)
@click.option(
    "--types",
    "types",
    multiple=True,
    help=(
        "Filter by memory type (PersonMemory, Decision, ProjectSnapshot, Insight, "
        "ResearchRef, ActionItem).  Repeat to allow multiple."
    ),
)
@click.option(
    "--date-after",
    help="Only return memories created/updated after this ISO date (e.g. 2026-01-01).",
)
@click.option(
    "--date-before",
    help="Only return memories created/updated before this ISO date.",
)
@click.option(
    "--include-daily",
    is_flag=True,
    help="Include daily/ session notes at full rank (default: penalized).",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
@click.option("--score/--no-score", default=False, help="Show relevance scores")
@click.option("--no-context", is_flag=True, help="Disable ambient context boost")
def search(
    query,
    limit,
    category,
    threshold,
    since_days,
    types,
    date_after,
    date_before,
    include_daily,
    fmt,
    score,
    no_context,
):
    """Search memory by meaning or keyword."""
    try:
        context = None if no_context else _cli_resolve_context()
        results = api_client.search(
            query,
            limit=limit,
            category=category,
            context=context,
            threshold=threshold,
            since_days=since_days,
            types=list(types) if types else None,
            date_after=date_after,
            date_before=date_before,
            include_daily=include_daily or None,
        )
        
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
                # #352: prefer the API-provided snippet — already match-windowed
                # and bounded. Fall back to the legacy blind-truncation path
                # when talking to an older API server that doesn't populate it.
                snippet = res.get("snippet")
                if snippet is not None:
                    body = snippet.strip()
                else:
                    raw = res.get("content", "")
                    body = raw.strip()[:200] + "..." if len(raw) > 200 else raw

                console.print(f"[bold blue]{score_str}{title}[/bold blue]")
                console.print(f"  {body}")
                console.print()
                
    except Exception as e:
        console.print(f"[red]Error searching memory: {str(e)}[/red]")
        click.Abort()
