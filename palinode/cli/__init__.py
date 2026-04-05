import click
import asyncio
from palinode.core.config import config
from palinode.cli.search import search
from palinode.cli.save import save
from palinode.cli.status import status
from palinode.cli.diff import diff
from palinode.cli.consolidate import consolidate
from palinode.cli.trigger import trigger
from palinode.cli.doctor import doctor
from palinode.cli.manage import reindex, rebuild_fts, split_layers, bootstrap_ids, migrate_mem0
from palinode.cli.git import blame, timeline, rollback, push
from palinode.cli.query import history, entities
from palinode.cli.session_end import session_end
from palinode.cli.read import read
from palinode.cli.list import list_cmd

@click.group()
def main():
    """Palinode — persistent agent memory."""
    pass

# Registration
main.add_command(search)
main.add_command(save)
main.add_command(status)
main.add_command(diff)
main.add_command(consolidate)
main.add_command(trigger)
main.add_command(doctor)

# Manage
main.add_command(reindex)
main.add_command(rebuild_fts)
main.add_command(split_layers)
main.add_command(bootstrap_ids)
main.add_command(migrate_mem0)

# Git
main.add_command(blame)
main.add_command(timeline)
main.add_command(rollback)
main.add_command(push)

# Query
main.add_command(history)
main.add_command(entities)
main.add_command(read)
main.add_command(list_cmd, name="list")

# Session
main.add_command(session_end)

@main.command()
@click.option("--watcher/--no-watcher", default=True, help="Run memory watcher")
@click.option("--api/--no-api", default=True, help="Run API server")
def start(watcher, api):
    """Start Palinode services in the foreground."""
    from palinode.api.server import APIServer
    from palinode.indexer.sqlite import SQLiteIndexer
    from palinode.ingest.watcher import Watcher
    from rich.live import Live
    from rich.panel import Panel
    from rich.console import Console
    
    console = Console()
    
    async def run_services():
        tasks = []
        if api:
            console.print("[green]Starting API server...[/green]")
            api_service = APIServer(config)
            tasks.append(api_service.start())
            
        if watcher:
            console.print("[green]Starting watcher...[/green]")
            indexer = SQLiteIndexer(config)
            watcher_service = Watcher(config, indexer)
            tasks.append(watcher_service.start())
            
        if not tasks:
            console.print("[yellow]No services specified to start.[/yellow]")
            return
            
        await asyncio.gather(*tasks)

    try:
        asyncio.run(run_services())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping services...[/yellow]")

@main.command()
@click.option("--watcher/--no-watcher", default=True, help="Stop memory watcher")
@click.option("--api/--no-api", default=True, help="Stop API server")
def stop(watcher, api):
    """Stop Palinode services (Linux/systemd only)."""
    import subprocess
    import shutil
    from rich.console import Console
    
    console = Console()
    
    if not shutil.which("systemctl"):
        console.print("[red]Error: 'systemctl' not found. This command requires systemd (Linux).[/red]")
        return
        
    services = []
    if api:
        services.append("palinode-api.service")
    if watcher:
        services.append("palinode-watcher.service")
        
    if not services:
        return
        
    for svc in services:
        console.print(f"[yellow]Stopping {svc}...[/yellow]")
        try:
            subprocess.run(["sudo", "systemctl", "stop", svc], check=True)
            console.print(f"[green]✓ {svc} stopped.[/green]")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]✗ Failed to stop {svc}: {e}[/red]")

@main.group()
def config_cmd():
    """Manage Palinode configuration."""
    pass

@config_cmd.command(name="view")
@click.option("--format", "fmt", type=click.Choice(["json", "yaml"]), default="yaml", help="Output format")
def config_view(fmt):
    """View current configuration."""
    from palinode.core.config import config
    import yaml
    import json
    from rich.syntax import Syntax
    
    if fmt == "json":
        # Pydantic core conversion
        content = json.dumps(config.__dict__, indent=2, default=str)
        syntax = Syntax(content, "json", theme="monokai")
    else:
        # Pydantic to dict then yaml
        # We need to handle the nested dataclasses
        def to_dict(obj):
            if hasattr(obj, "__dict__"):
                return {k: to_dict(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
            elif isinstance(obj, list):
                return [to_dict(x) for x in obj]
            else:
                return obj
        content = yaml.dump(to_dict(config), sort_keys=False)
        syntax = Syntax(content, "yaml", theme="monokai")
    
    console.print(syntax)

@config_cmd.command(name="edit")
def config_edit():
    """Open configuration file in default editor."""
    import os
    import subprocess
    
    config_file = os.environ.get("PALINODE_CONFIG", "palinode.config.yaml")
    if not os.path.exists(config_file):
        # Check standard locations
        from palinode.core.config import config
        config_file = os.path.join(config.memory_dir, "palinode.config.yaml")
        if not os.path.exists(config_file):
             console.print(f"[red]Error: Config file not found at default locations.[/red]")
             return
             
    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([editor, config_file], check=True)
    except Exception as e:
        console.print(f"[red]Error opening editor: {e}[/red]")

main.add_command(config_cmd, name="config")

if __name__ == "__main__":
    main()
