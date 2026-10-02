"""``palinode aliases`` — curate the store's entity-aliases.yaml over the API.

Writes apply by default and ``--dry-run`` previews them. The server owns the
file (always ``<PALINODE_DIR>/entity-aliases.yaml``), writes it sorted and
commits it in the store's git; memory files are never touched.
"""
from __future__ import annotations

from typing import Any

import click

from palinode.cli._api import HTTPStatusError, RequestError, api_client
from palinode.cli._format import OutputFormat, emit_json, get_default_format

_FORMAT = click.option(
    "--format", "fmt", type=click.Choice(["json", "text"]), help="Output format"
)


def _detail(exc: HTTPStatusError) -> str:
    """The API's own refusal text: a string ``detail``, or a validation list."""
    try:
        detail = exc.response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):
        detail = "; ".join(
            str(item.get("msg", item)) if isinstance(item, dict) else str(item)
            for item in detail
        )
    return str(detail) if detail else exc.response.text or "no detail"


def _call(fn, *args, **kwargs) -> dict[str, Any]:
    """Call the API; a refusal (404, 409, 422, …) prints its detail and exits 1.

    Printed to stdout with ``click.echo``, as the sibling commands print their
    API errors: stdout is what every click version's test runner and every
    terminal show, and plain echo keeps ``[...]`` in a ref from being read as
    rich markup.
    """
    try:
        return fn(*args, **kwargs)
    except HTTPStatusError as exc:
        click.echo(f"Error: API returned {exc.response.status_code}: {_detail(exc)}")
        raise SystemExit(1) from None
    except RequestError:
        click.echo("Error: the Palinode API is unreachable (run `palinode doctor`).")
        raise SystemExit(1) from None


def _json(fmt: str | None) -> bool:
    return (OutputFormat(fmt) if fmt else get_default_format()) == OutputFormat.JSON


def _count(n: Any) -> str:
    return "?" if n is None else str(n)


@click.group(name="aliases")
def aliases() -> None:
    """List, edit and check the store's curated entity aliases.

    Groups live in entity-aliases.yaml in the memory directory and are
    resolved at query time. See docs/ENTITY-ALIASES.md.
    """


@aliases.command(name="list")
@_FORMAT
def list_cmd(fmt: str | None) -> None:
    """Show every group, each ref with its file count from the index."""
    data = _call(api_client.aliases_list)
    if _json(fmt):
        emit_json(data)
        return
    groups = data.get("groups") or []
    if not groups:
        where = "" if data.get("exists") else f" ({data.get('file')} does not exist yet)"
        click.echo(f"No alias groups{where}.")
        return
    click.echo(f"{data.get('file')}: {len(groups)} group(s)")
    if not data.get("indexed"):
        click.echo("(no index: file counts unavailable)")
    for group in groups:
        click.echo(f"{group['canonical']} ({_count(group.get('files'))} files)")
        for member in group.get("members") or []:
            click.echo(f"  {member['ref']} ({_count(member.get('files'))})")


def _render_write(data: dict[str, Any], retry: str) -> None:
    if not data.get("changed"):
        click.echo("No change: the alias file already says this.")
        return
    if data.get("dry_run"):
        click.echo("Dry run: nothing written.")
    for moved in data.get("moved") or []:
        click.echo(f"  moves {moved['ref']} out of {moved['from']}")
    for group in data.get("removed_groups") or []:
        click.echo(f"  removes the emptied group {group}")
    if data.get("diff"):
        click.echo(data["diff"].rstrip("\n"))
    if data.get("dry_run"):
        click.echo(f"Apply: {retry}")
        return
    if data.get("committed"):
        click.echo(f"Written and committed: {data.get('file')}")
    else:
        reason = data.get("commit_error") or "git auto-commit is off"
        click.echo(f"Written, not committed ({reason}): {data.get('file')}")


@aliases.command(name="add")
@click.argument("canonical")
@click.argument("members", nargs=-1, required=True)
@click.option(
    "--move",
    is_flag=True,
    help="Take a member out of the group it already belongs to",
)
@click.option("--dry-run", is_flag=True, help="Show the change; write nothing")
@_FORMAT
def add_cmd(
    canonical: str, members: tuple[str, ...], move: bool, dry_run: bool, fmt: str | None
) -> None:
    """Create the group CANONICAL, or add MEMBERS to it.

    A member that already belongs to another group is refused unless --move.
    """
    data = _call(
        api_client.aliases_add, canonical, list(members), move=move, dry_run=dry_run
    )
    if _json(fmt):
        emit_json(data)
        return
    flags = " --move" if move else ""
    _render_write(data, f"palinode aliases add {canonical} {' '.join(members)}{flags}")


@aliases.command(name="remove")
@click.argument("member")
@click.option("--dry-run", is_flag=True, help="Show the change; write nothing")
@_FORMAT
def remove_cmd(member: str, dry_run: bool, fmt: str | None) -> None:
    """Drop MEMBER from its group; a group left empty is removed."""
    data = _call(api_client.aliases_remove, member, dry_run=dry_run)
    if _json(fmt):
        emit_json(data)
        return
    _render_write(data, f"palinode aliases remove {member}")


@aliases.command(name="check")
@_FORMAT
def check_cmd(fmt: str | None) -> None:
    """Run the alias lint and the project_tags_unmapped doctor check."""
    data = _call(api_client.aliases_check)
    if _json(fmt):
        emit_json(data)
        return
    if data.get("file_error"):
        click.echo(f"Alias file: {data['file_error']}")
    if not data.get("indexed"):
        click.echo("No index to read entity refs from: alias lint skipped.")
    clusters = data.get("alias_candidates") or []
    open_ = [c for c in clusters if not c.get("grouped")]
    click.echo(
        f"Alias candidates: {len(open_)} open, {len(clusters) - len(open_)} already grouped"
    )
    for cluster in open_:
        refs = ", ".join(f"{r['ref']} ({r['files']})" for r in cluster.get("refs") or [])
        click.echo(f"  [{cluster.get('confidence')}] {cluster.get('kind')}: {refs}")
    doctor = data.get("project_tags_unmapped") or {}
    status = "ok" if doctor.get("passed") else doctor.get("severity", "warn")
    click.echo(f"project_tags_unmapped [{status}]: {doctor.get('message')}")
    if doctor.get("remediation") and not doctor.get("passed"):
        click.echo(f"  {doctor['remediation']}")
