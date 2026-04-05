import click
import os
import glob
from palinode.cli._api import api_client
from palinode.cli._format import console, get_default_format
from palinode.core.config import config

@click.command()
def doctor():
    """Connectivity and health check."""
    console.print("Palinode Diagnostics", style="bold underline")
    console.print()
    
    # 1. Config Check
    config_path = os.environ.get("PALINODE_CONFIG", "palinode.config.yaml")
    if os.path.exists(config_path):
        console.print(f"[green]✓[/green] Config loaded: {config_path}")
    else:
        # Fallback to check default locations if needed, but for now:
        console.print(f"[yellow]![/yellow] Config at {config_path} not found (using defaults)")

    # 2. API Connectivity
    reachable, health_data = api_client.health_check()
    if reachable:
        console.print(f"[green]✓[/green] API reachable: {api_client.base_url} (healthy)")
    else:
        console.print(f"[red]✗[/red] API unreachable: {api_client.base_url}")
        console.print(f"  Error: {health_data.get('error')}")

    # 3. Memory Directory
    memory_dir = config.memory_dir
    if os.path.exists(memory_dir):
        files = glob.glob(os.path.join(memory_dir, "**/*.md"), recursive=True)
        console.print(f"[green]✓[/green] Memory dir: {len(files)} files found in {memory_dir}")
    else:
        console.print(f"[red]✗[/red] Memory dir {memory_dir} does not exist")

    # 4. Ollama & Embedding
    # This info usually comes from health data if API is up
    if reachable:
        ollama_url = health_data.get("embedding_url", "unknown")
        model = health_data.get("embedding_model", "unknown")
        console.print(f"[green]✓[/green] Ollama reachable: {ollama_url}")
        console.print(f"[green]✓[/green] Embedding model: {model}")
    else:
        console.print(f"[yellow]![/yellow] Could not verify embedding health (API down)")

    # 5. Git Status
    if reachable:
        git_remote = health_data.get("git_remote")
        if git_remote:
            console.print(f"[green]✓[/green] Git remote: {git_remote}")
        else:
            console.print(f"[yellow]✗[/yellow] Git remote: not configured (auto_push disabled)")
    
    console.print()
    if not reachable:
        console.print("[bold red]Diagnosis: Critical connectivity issues detected.[/bold red]")
        console.print("Try starting the backend: [cyan]palinode-api[/cyan]")
    else:
        console.print("[bold green]Diagnosis: All systems operational.[/bold green]")
