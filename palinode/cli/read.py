"""
``palinode read`` — fetch a memory file via the API.

Per ADR-010, this command goes through ``palinode/cli/_api.py``
rather than reading disk directly.  Path validation, traversal
protection, and ``.md`` extension fallback live server-side in the
``/read`` handler — the CLI is now a thin presenter.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import click

from palinode.cli._api import HTTPStatusError, api_client
from palinode.cli._format import OutputFormat, get_default_format
from palinode.core.parser import split_frontmatter


@click.command()
@click.argument("file_path")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default=None,
    help="Output format",
)
@click.option(
    "--meta/--no-meta",
    default=False,
    help="Include YAML frontmatter as structured data",
)
def read(file_path, fmt, meta):
    """Read a specific memory file.

    FILE_PATH is relative to the memory directory (e.g., "people/peter.md",
    "decisions/cli-pivot.md").

    Examples:

        palinode read people/peter.md

        palinode read projects/palinode-status.md --meta --format json
    """
    try:
        result = api_client.read(file_path, meta=meta)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise click.ClickException(f"File not found: {file_path}") from e
        raise click.ClickException(f"Read failed: {e.response.text}") from e

    effective_fmt = OutputFormat(fmt) if fmt else get_default_format()

    if meta:
        if effective_fmt == OutputFormat.JSON:
            click.echo(json.dumps(result, indent=2, default=_json_default))
        else:
            click.echo(_format_with_meta(result))
    else:
        if effective_fmt == OutputFormat.JSON:
            # Drop frontmatter from the no-meta JSON view for symmetry with
            # the API's no-meta response shape.
            slim = {k: v for k, v in result.items() if k != "frontmatter"}
            click.echo(json.dumps(slim, indent=2, default=_json_default))
        else:
            click.echo(result.get("content", ""))


def _json_default(obj: Any):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _format_with_meta(result: dict) -> str:
    lines: list[str] = []
    fm = result.get("frontmatter") or {}
    if fm:
        lines.append("── Frontmatter ──")
        for k, v in fm.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
    lines.append("── Content ──")
    # When meta=True, the API returns the full file content (frontmatter +
    # body).  Strip the leading frontmatter block for cleaner CLI output.
    content = result.get("content", "")
    _, body = split_frontmatter(content)
    lines.append(body)
    return "\n".join(lines)
