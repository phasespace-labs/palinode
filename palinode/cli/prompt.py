"""CLI commands for managing versioned LLM prompts stored as memory files."""
import json

import click
import httpx
from rich.table import Table

from palinode.core.config import config
from palinode.cli._format import console, get_default_format, OutputFormat, print_result


def _api_url(path: str) -> str:
    return f"http://{config.services.api.host}:{config.services.api.port}{path}"


@click.group()
def prompt():
    """Manage versioned LLM prompts stored as memory files."""
    pass


@prompt.command(name="list")
@click.option("--task", type=click.Choice(["compaction", "extraction", "update", "classification", "nightly-consolidation"]),
              help="Filter by task type")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None)
def prompt_list(task: str | None, fmt: str | None) -> None:
    """List all stored prompt versions."""
    params: dict = {}
    if task:
        params["task"] = task

    try:
        resp = httpx.get(_api_url("/prompts"), params=params, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        console.print(f"[red]Error listing prompts: {e}[/red]")
        raise click.Abort()

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        print_result(data, fmt=output_fmt)
        return

    if not data:
        console.print("[yellow]No prompts found.[/yellow]")
        return

    table = Table(title="Palinode Prompts")
    table.add_column("Name", style="cyan")
    table.add_column("Task", style="blue")
    table.add_column("Model")
    table.add_column("Version")
    table.add_column("Active", justify="center")

    for p in data:
        active_marker = "[green]yes[/green]" if p.get("active") else ""
        table.add_row(
            p["name"],
            p.get("task", ""),
            p.get("model", ""),
            str(p.get("version", "")),
            active_marker,
        )

    console.print(table)
    console.print(f"\n[bold]{len(data)} prompt(s)[/bold]")


@prompt.command(name="show")
@click.argument("name")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None)
def prompt_show(name: str, fmt: str | None) -> None:
    """Display the content of a specific prompt."""
    try:
        resp = httpx.get(_api_url(f"/prompts/{name}"), timeout=30.0)
        if resp.status_code == 404:
            console.print(f"[red]Prompt '{name}' not found.[/red]")
            raise click.Abort()
        resp.raise_for_status()
        data = resp.json()
    except click.Abort:
        raise
    except Exception as e:
        console.print(f"[red]Error reading prompt: {e}[/red]")
        raise click.Abort()

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        print_result(data, fmt=output_fmt)
        return

    active_label = " [green](active)[/green]" if data.get("active") else ""
    console.print(f"[bold cyan]{data['name']}[/bold cyan]{active_label}")
    console.print(f"  task={data.get('task','')}  model={data.get('model','')}  version={data.get('version','')}")
    console.print("")
    console.print(data.get("content", ""))


@prompt.command(name="activate")
@click.argument("name")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None)
def prompt_activate(name: str, fmt: str | None) -> None:
    """Activate a prompt version (deactivates others of the same task)."""
    try:
        resp = httpx.post(_api_url(f"/prompts/{name}/activate"), timeout=30.0)
        if resp.status_code == 404:
            console.print(f"[red]Prompt '{name}' not found.[/red]")
            raise click.Abort()
        resp.raise_for_status()
        data = resp.json()
    except click.Abort:
        raise
    except Exception as e:
        console.print(f"[red]Error activating prompt: {e}[/red]")
        raise click.Abort()

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        print_result(data, fmt=output_fmt)
        return

    console.print(
        f"[green]Activated[/green] [cyan]{data['activated']}[/cyan] "
        f"for task [blue]{data['task']}[/blue]"
    )
