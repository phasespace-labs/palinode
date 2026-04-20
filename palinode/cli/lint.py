import click
import json
import httpx
from rich.console import Console

from palinode.core.config import config

console = Console()

@click.command()
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text", help="Output format")
def lint(fmt):
    """Scan memory and report orphans, stale files, and contradictions."""
    api_url = f"http://localhost:{config.services.api.port}/lint"
    
    try:
        resp = httpx.post(api_url, timeout=30.0)
        if resp.status_code != 200:
            console.print(f"[red]Error: API returned {resp.status_code}[/red]")
            return
        data = resp.json()
    except httpx.RequestError:
        # Fallback to local import if API is down
        from palinode.core.lint import run_lint_pass
        data = run_lint_pass()

    if fmt == "json":
        console.print(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold green]Palinode Memory Lint Report[/bold green]\n")
    
    if data["missing_fields"]:
        console.print(f"[bold yellow]Missing Frontmatter ({len(data['missing_fields'])})[/bold yellow]")
        for mf in data["missing_fields"]:
             console.print(f"  - {mf['file']}: missing {', '.join(mf['missing'])}")
    else:
        console.print("[green]✓ No files missing frontmatter[/green]")
        
    console.print("")
        
    if data["orphaned_files"]:
        console.print(f"[bold yellow]Orphaned Files ({len(data['orphaned_files'])})[/bold yellow]")
        for of in data["orphaned_files"]:
             console.print(f"  - {of}")
    else:
        console.print("[green]✓ No orphaned files[/green]")
        
    console.print("")
        
    if data["stale_files"]:
        console.print(f"[bold yellow]Stale Active Files ({len(data['stale_files'])})[/bold yellow]")
        for sf in data["stale_files"]:
             console.print(f"  - {sf['file']} ({sf['days_old']} days old)")
    else:
        console.print("[green]✓ No stale active files (>90 days)[/green]")
        
    console.print("")

    if data["contradictions"]:
        console.print(f"[bold yellow]Potential Contradictions ({len(data['contradictions'])})[/bold yellow]")
        for ct in data["contradictions"]:
             console.print(f"  - {ct['entity']}: {ct['issue']}")
    else:
        console.print("[green]✓ No contradictions detected[/green]")

    console.print("")

    # M0: new checks
    missing_ent = data.get("missing_entities", [])
    if missing_ent:
        console.print(f"[bold yellow]Missing Entities ({len(missing_ent)})[/bold yellow]")
        for me in missing_ent:
            console.print(f"  - {me}")
    else:
        console.print("[green]✓ All files have entity refs[/green]")

    console.print("")

    missing_desc = data.get("missing_descriptions", [])
    if missing_desc:
        console.print(f"[bold yellow]Missing Descriptions ({len(missing_desc)})[/bold yellow]")
        for md in missing_desc:
            console.print(f"  - {md}")
    else:
        console.print("[green]✓ All files have descriptions[/green]")

    console.print("")

    core_count = data.get("core_count", 0)
    if core_count > 10:
        console.print(f"[bold red]Core Files: {core_count}[/bold red] (recommended: ≤10 — prune with `palinode list --core-only`)")
    elif core_count > 0:
        console.print(f"[green]Core Files: {core_count}[/green]")
    else:
        console.print("[dim]No core files found[/dim]")

    console.print("")
