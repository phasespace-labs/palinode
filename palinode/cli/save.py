import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.argument("content", required=False)
@click.option("--type", "memory_type", required=False, help="Memory type (e.g. PersonMemory, Decision, Insight, ProjectSnapshot)")
@click.option("--ps", "is_ps", is_flag=True, help="Shorthand for --type ProjectSnapshot (Palinode Save a mid-session snapshot)")
@click.option("--entity", "entities", multiple=True, help="Entity tag (e.g. person/X, project/X)")
@click.option("--file", "file_path", type=click.Path(exists=True), help="Read content from file instead of argument")
@click.option("--title", help="Optional title override")
@click.option("--source", help="Source surface (e.g., claude-code, cursor, api)")
@click.option(
    "--sync/--no-sync",
    default=False,
    help="Run the write-time contradiction check inline and include its result "
         "in the response. Default is async (fire-and-forget). Requires "
         "consolidation.write_time.enabled in config.",
)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def save(content, memory_type, is_ps, entities, file_path, title, source, sync, fmt):
    """Store a new memory.

    Use --ps as shorthand for --type ProjectSnapshot when dropping a quick
    mid-session note ("Palinode Save"). For structured session wrap-ups with
    decisions and blockers, use `palinode session-end` instead.
    """
    if file_path:
        with open(file_path, "r") as f:
            content = f.read()

    if not content:
        console.print("[red]Error: Must provide content or a file.[/red]")
        click.Abort()
        return

    # Resolve memory type: --ps is shorthand for ProjectSnapshot
    if is_ps and memory_type and memory_type != "ProjectSnapshot":
        console.print(f"[red]Error: --ps conflicts with --type {memory_type}. Pick one.[/red]")
        click.Abort()
        return
    if is_ps:
        memory_type = "ProjectSnapshot"
    if not memory_type:
        console.print("[red]Error: Must provide --type or --ps.[/red]")
        click.Abort()
        return

    try:
        source_val = source or "cli"
        result = api_client.save(
            content,
            memory_type,
            entities=list(entities),
            title=title,
            source=source_val,
            sync=sync,
        )

        output_fmt = OutputFormat(fmt) if fmt else get_default_format()

        if output_fmt == OutputFormat.JSON:
            print_result(result, fmt=output_fmt)
        else:
            filename = result.get("file_path", result.get("file", "unknown"))
            id_str = result.get("id", "unknown")
            console.print(f"[green]Saved:[/green] {filename} (id: {id_str})")
            if sync and "write_time_check" in result:
                check = result["write_time_check"]
                ops = check.get("operations", [])
                applied = check.get("applied_stats", {})
                ms = check.get("llm_latency_ms", 0)
                console.print(
                    f"[dim]Write-time check:[/dim] {len(ops)} ops proposed, "
                    f"applied={applied}, llm={ms}ms"
                )

    except Exception as e:
        console.print(f"[red]Error saving memory: {str(e)}[/red]")
        click.Abort()
