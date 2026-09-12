import json

import click

from palinode.cli._api import api_client
from palinode.cli._format import console, get_default_format, OutputFormat


def _emit_json(data) -> None:
    # click.echo (not console.print): machine-readable JSON must not pass
    # through Rich's highlighter, which would inject ANSI colour codes.
    click.echo(json.dumps(data, indent=2))


@click.command(name="restore")
@click.argument("file_path")
@click.option("--reason", default=None, help="Why this memory is being restored")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def restore(file_path, reason, fmt):
    """Bring an archived memory back into default recall.

    The inverse of `palinode archive` for every archive path (on-demand,
    forget request, TTL expiry, consolidation). Flips `status` back to
    `active`, drops `superseded_by`, records `restored_at` / `restored_from`
    and a history line, and commits. Retraction markers are not un-struck
    (see `palinode unretract`) and triggers are not re-enabled.
    """
    try:
        data = api_client.restore(file_path, reason=reason)
    except Exception as e:
        console.print(f"[red]Error restoring {file_path}: {str(e)}[/red]")
        raise SystemExit(1)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        _emit_json(data)
        return

    if data.get("status") == "not_archived":
        console.print(f"[yellow]{data.get('file')} is not archived — no change.[/yellow]")
        return
    console.print(
        f"[green]Restored: {data.get('file')}[/green] (was {data.get('restored_from')})"
    )
    if data.get("history_file"):
        console.print(f"  history: {data['history_file']}")
    console.print(f"  chunks returned to recall: {data.get('chunks_updated', 0)}")
    if data.get("stale_backing"):
        console.print(
            "  [yellow]stale backing flagged (source no longer active): "
            f"{', '.join(data['stale_backing'])}[/yellow]"
        )
    if data.get("expires_at"):
        console.print(
            f"  [yellow]expires_at is still {data['expires_at']} — the TTL sweep "
            "will re-archive it unless the expiry is changed[/yellow]"
        )


@click.command(name="unretract")
@click.argument("file_path")
@click.argument("pref")
@click.option("--reason", default=None, help="Why the retraction is being withdrawn")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def unretract(file_path, pref, reason, fmt):
    """Withdraw one preference's mention-level retraction from one memory.

    Un-strikes every `~~…~~ [RETRACTED …]` span PREF produced in FILE_PATH
    and removes PREF from the file's `retracted_prefs` record, so a later
    forget request for the same pref can strike again. PREF is the phrase as
    recorded in the file's history sibling. `status` is never changed.
    """
    try:
        data = api_client.unretract(file_path, pref, reason=reason)
    except Exception as e:
        console.print(f"[red]Error unretracting {file_path}: {str(e)}[/red]")
        raise SystemExit(1)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        _emit_json(data)
        return

    if data.get("status") == "not_retracted":
        console.print(
            f"[yellow]{data.get('file')} carries no retraction for that pref — no change.[/yellow]"
        )
        return
    console.print(
        f"[green]Unretracted {data.get('mentions', 0)} mention(s) in {data.get('file')}[/green]"
    )
    if data.get("history_file"):
        console.print(f"  history: {data['history_file']}")
    if data.get("index_error"):
        console.print(f"  [yellow]re-index failed: {data['index_error']}[/yellow]")


@click.command(name="forget-withdraw")
@click.argument("file_path")
@click.option("--reason", default=None, help="Why the forget request is being withdrawn")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), help="Output format")
def forget_withdraw(file_path, reason, fmt):
    """Take a forget request back.

    FILE_PATH is the memory holding the request ("please forget that I…").
    Restores every memory it archived, un-strikes every mention it retracted,
    and archives the request record(s) so they stop acting as the retraction.
    Each step is its own audited commit; failures are reported per target.
    """
    try:
        data = api_client.forget_withdraw(file_path, reason=reason)
    except Exception as e:
        console.print(f"[red]Error withdrawing {file_path}: {str(e)}[/red]")
        raise SystemExit(1)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        _emit_json(data)
        return

    console.print(f"[green]Withdrawn: {data.get('file')}[/green] (pref: {data.get('pref')!r})")
    restored = data.get("restored", [])
    console.print(f"  restored ({len(restored)}): {', '.join(restored) or 'none'}")
    unretracted = data.get("unretracted", [])
    console.print(
        f"  unretracted ({len(unretracted)}): "
        f"{', '.join(u['path'] for u in unretracted) or 'none'}"
    )
    console.print(
        f"  request records archived: {', '.join(data.get('requests_archived', [])) or 'none'}"
    )
    for f in data.get("failed", []):
        console.print(f"  [red]failed: {f['path']} ({f['op']})[/red]")
