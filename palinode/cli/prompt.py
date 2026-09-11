"""CLI commands for managing versioned LLM prompts stored as memory files."""
from pathlib import Path

import click
from rich.table import Table

from palinode.cli._api import HTTPStatusError, api_client
from palinode.cli._format import console, get_default_format, OutputFormat, print_result
from palinode.core.parity import PROMPT_TASKS


@click.group()
def prompt():
    """Manage versioned LLM prompts stored as memory files."""
    pass


@prompt.command(name="list")
@click.option("--task", type=click.Choice(PROMPT_TASKS),
              help="Filter by task type")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None)
def prompt_list(task: str | None, fmt: str | None) -> None:
    """List all stored prompt versions."""
    try:
        data = api_client.list_prompts(task=task)
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
        data = api_client.get_prompt(name)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Prompt '{name}' not found.[/red]")
            raise click.Abort()
        console.print(f"[red]Error reading prompt: {e}[/red]")
        raise click.Abort()
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
        data = api_client.activate_prompt(name)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Prompt '{name}' not found.[/red]")
            raise click.Abort()
        console.print(f"[red]Error activating prompt: {e}[/red]")
        raise click.Abort()
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


#: What `sync` decided about one store prompt.
_ACTION_EDITED = "kept-edited"
_ACTION_REFRESHED = "refreshed"
_ACTION_ADDED = "added"
_ACTION_UNCHANGED = "unchanged"


def sync_plan(store_dir: Path, force: bool = False) -> list[dict[str, str]]:
    """Decide, per packaged prompt, what `prompt sync` would do to *store_dir*.

    The rule, and why it needs a hash manifest rather than a diff: consolidation
    reads the *store's* prompts, and an operator is invited to edit them, so
    "the store differs from the package" is ambiguous — it means either "you are
    a release behind" or "you tuned this". Palinode ships the sha256 of every
    prompt revision it has ever released
    (``palinode/prompts/shipped-hashes.json``), so a store copy whose hash is in
    that list is provably a pristine copy of some release and safe to replace;
    anything else is the operator's and is left alone with a report.

    A missing file is provisioned (``added``); a copy already matching the
    packaged one is ``unchanged``. ``force`` is the deliberate "yes, discard my
    edits" escape hatch — nothing else can reach an edited file.
    """
    from palinode.prompts import content_hash, iter_packaged_prompts, shipped_hashes

    history = shipped_hashes()
    plan: list[dict[str, str]] = []
    for source in iter_packaged_prompts():
        dest = store_dir / source.name
        packaged_hash = content_hash(source.read_bytes())
        if not dest.is_file():
            action = _ACTION_ADDED
        else:
            store_hash = content_hash(dest.read_bytes())
            if store_hash == packaged_hash:
                action = _ACTION_UNCHANGED
            elif force or store_hash in history.get(source.name, []):
                action = _ACTION_REFRESHED
            else:
                action = _ACTION_EDITED
        plan.append({"prompt": source.name, "action": action, "path": str(dest)})
    return plan


def apply_sync_plan(plan: list[dict[str, str]], store_dir: Path) -> None:
    """Perform the writes *plan* describes. Only ``added``/``refreshed`` write."""
    from palinode.prompts import packaged_prompt_path

    store_dir.mkdir(parents=True, exist_ok=True)
    for entry in plan:
        if entry["action"] not in (_ACTION_ADDED, _ACTION_REFRESHED):
            continue
        source = packaged_prompt_path(entry["prompt"])
        Path(entry["path"]).write_bytes(source.read_bytes())


@prompt.command(name="sync")
@click.option(
    "--dry-run", is_flag=True,
    help="Report what would change without writing anything",
)
@click.option(
    "--force", is_flag=True,
    help="Also overwrite prompts you have edited (destructive - take a copy first)",
)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default=None)
def prompt_sync(dry_run: bool, force: bool, fmt: str | None) -> None:
    """Refresh the memory store's consolidation prompts from the packaged ones.

    Consolidation reads the store's copy of each prompt, so a release that
    changes a prompt changes nothing until that copy is refreshed. This replaces
    only the copies that still match a version palinode released; anything you
    have edited is reported and left alone.

    CLI-only by design: operator maintenance on local files, not a memory
    operation, so it has no MCP or REST counterpart to stay in parity with.
    """
    from palinode.core.config import config
    from palinode.prompts import store_prompts_dir

    store_dir = store_prompts_dir(config.memory_dir)
    plan = sync_plan(store_dir, force=force)
    if not dry_run:
        apply_sync_plan(plan, store_dir)

    counts: dict[str, int] = {}
    for entry in plan:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        print_result(
            {
                "store_prompts_dir": str(store_dir),
                "dry_run": dry_run,
                "counts": counts,
                "prompts": plan,
            },
            fmt=output_fmt,
        )
        return

    prefix = "would " if dry_run else ""
    console.print(f"Prompt sync → [cyan]{store_dir}[/cyan]")
    for entry in plan:
        if entry["action"] == _ACTION_EDITED:
            console.print(
                f"  [yellow]kept[/yellow] {entry['prompt']} — locally edited, not replaced"
            )
        elif entry["action"] == _ACTION_REFRESHED:
            console.print(f"  [green]{prefix}refresh[/green] {entry['prompt']}")
        elif entry["action"] == _ACTION_ADDED:
            console.print(f"  [green]{prefix}add[/green] {entry['prompt']}")
        else:
            console.print(f"  · {entry['prompt']} — already current")

    summary = ", ".join(f"{n} {action}" for action, n in sorted(counts.items()))
    console.print(f"\n[bold]{summary}[/bold]")
    if counts.get(_ACTION_EDITED):
        console.print(
            "Edited prompts stay as they are. Diff them against the packaged "
            "originals and re-apply your changes, or --force to discard them."
        )
