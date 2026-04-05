import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format, OutputFormat

@click.command()
@click.argument("content", required=False)
@click.option("--type", "memory_type", required=True, help="Memory type (e.g. PersonMemory, Decision, Insight)")
@click.option("--entity", "entities", multiple=True, help="Entity tag (e.g. person/X, project/X)")
@click.option("--file", "file_path", type=click.Path(exists=True), help="Read content from file instead of argument")
@click.option("--title", help="Optional title override")
@click.option("--source", help="Source surface (e.g., claude-code, antigravity)")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def save(content, memory_type, entities, file_path, title, source, fmt):
    """Store a new memory."""
    if file_path:
        with open(file_path, "r") as f:
            content = f.read()
    
    if not content:
        console.print("[red]Error: Must provide content or a file.[/red]")
        click.Abort()
        return

    try:
        source_val = source or "cli"
        result = api_client.save(content, memory_type, entities=list(entities), title=title, source=source_val)
        
        output_fmt = OutputFormat(fmt) if fmt else get_default_format()
        
        if output_fmt == OutputFormat.JSON:
            print_result(result, fmt=output_fmt)
        else:
            filename = result.get("file", "unknown")
            id_str = result.get("id", "unknown")
            console.print(f"[green]Saved:[/green] {filename} (id: {id_str})")
            
    except Exception as e:
        console.print(f"[red]Error saving memory: {str(e)}[/red]")
        click.Abort()
