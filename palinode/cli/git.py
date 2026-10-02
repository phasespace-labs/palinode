import click
from palinode.cli._api import api_client
from palinode.cli._format import console, print_result, get_default_format

@click.command()
@click.argument("file_path")
@click.option("--search", help="Filter to matching lines")
@click.option(
    "--claims",
    is_flag=True,
    default=False,
    help=(
        "Also resolve the file's claim-level source anchors: which source "
        "span justifies each claim, with live integrity status."
    ),
)
def blame(file_path, search, claims):
    """Show when lines were changed."""
    try:
        data = api_client.blame(file_path, search, claims=claims)
        if claims:
            from palinode.core.claims import format_claims_resolution

            # markup=False: blame/claims lines carry [status] and [git: …]
            # brackets that Rich would otherwise consume as style tags.
            console.print(data.get("blame", ""), markup=False)
            console.print("")
            console.print(
                format_claims_resolution(file_path, data.get("claims", [])),
                markup=False,
            )
        else:
            console.print(data)
    except Exception as e:
        console.print(f"[red]Error blaming: {str(e)}[/red]")
        raise SystemExit(1)

@click.command()
@click.argument("file_path")
@click.option("--limit", type=int, default=20, help="Max commits to show")
@click.option(
    "--detail",
    type=click.Choice(["summary", "full"]),
    default="summary",
    help=(
        "'summary' (default): hash/date/message/stats per commit. "
        "'full': also includes the unified diff body per commit "
        "(commit-level evolution view)."
    ),
)
def history(file_path, limit, detail):
    """Show file change history with diff stats."""
    try:
        data = api_client.get_history(file_path, limit, detail=detail)
        print_result(data, fmt=get_default_format())
    except Exception as e:
        console.print(f"[red]Error showing history: {str(e)}[/red]")
        raise SystemExit(1)


@click.command()
@click.argument("file_path")
@click.argument("commit", required=False)
@click.option(
    "--dry-run/--no-dry-run",
    "dry_run",
    default=True,
    help="Preview the change without applying.  Default: --dry-run.",
)
@click.option(
    "--undo-retirements",
    is_flag=True,
    default=False,
    help=(
        "Acknowledge that the rollback undoes a retirement (archive, "
        "supersede, retraction) and brings the record back as current. "
        "Without it such a rollback is refused; prefer `palinode restore`."
    ),
)
def rollback(file_path, commit, dry_run, undo_retirements):
    """Revert a file to a previous commit.

    By default this is a dry run — pass ``--no-dry-run`` to actually
    apply the rollback.  ``COMMIT`` is optional; when omitted, rolls
    back to the immediately previous version.  A rollback that would
    undo a retirement is named in the preview and refused unless
    ``--undo-retirements`` is passed.
    """
    try:
        data = api_client.rollback(
            file_path, commit, dry_run=dry_run, undo_retirements=undo_retirements
        )
    except Exception as e:
        console.print(f"[red]Error rolling back: {str(e)}[/red]")
        raise SystemExit(1)
    console.print(data)
    if isinstance(data, dict) and data.get("status") == "refused":
        raise SystemExit(1)

@click.command()
def push():
    """Sync to GitHub."""
    try:
        data = api_client.push()
        console.print(data)
    except Exception as e:
        console.print(f"[red]Error pushing: {str(e)}[/red]")
        raise SystemExit(1)
