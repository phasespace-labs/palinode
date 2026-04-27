"""CLI commands for the Obsidian embedding-tool MVP (#210).

Two commands, mirroring the MCP / API surface:

* ``palinode dedup-suggest`` — given draft content, list semantically near
  existing files; flags strong duplicates at ≥0.90 similarity.
* ``palinode orphan-repair`` — given a broken ``[[wikilink]]``, list files
  semantically near the link target.

Both honor TTY-aware output (text for humans, JSON when piped) per the project
CLI convention.
"""
from __future__ import annotations

import sys

import click

from palinode.cli._api import api_client
from palinode.cli._format import (
    OutputFormat,
    console,
    get_default_format,
    print_result,
)


@click.command("dedup-suggest")
@click.option(
    "--content",
    help="Draft content to check for near-duplicates. Mutually exclusive with --file.",
)
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Read draft content from a file instead of --content.",
)
@click.option(
    "--min-similarity",
    type=float,
    default=None,
    help="Minimum cosine similarity to surface (0.0–1.0). Default 0.80.",
)
@click.option(
    "--top-k",
    type=int,
    default=None,
    help="Maximum candidates to return. Default 5.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "text"]),
    help="Output format (default: text on TTY, json when piped).",
)
def dedup_suggest(content, file_path, min_similarity, top_k, fmt):
    """Find existing memory files semantically near draft content.

    Use this BEFORE saving a new memory to decide "create new" vs "update
    existing".  Results flagged ``STRONG-DUP`` are near-paraphrases (similarity
    ≥ 0.90); the LLM should usually update those rather than create.

    Preprocessing strips wikilinks and the auto-generated `## See also`
    footer from both the draft and the candidates so notes linking the same
    entities don't false-positive as duplicates.
    """
    if not content and not file_path:
        console.print(
            "[red]Error:[/red] either --content or --file is required."
        )
        sys.exit(2)
    if content and file_path:
        console.print(
            "[red]Error:[/red] --content and --file are mutually exclusive."
        )
        sys.exit(2)

    if file_path:
        with open(file_path, "r") as f:
            content = f.read()

    try:
        results = api_client.dedup_suggest(
            content=content,
            min_similarity=min_similarity,
            top_k=top_k,
        )
    except Exception as e:  # noqa: BLE001 — surface the error verbatim
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        print_result(results, fmt=output_fmt)
        return

    if not results:
        console.print(
            "[green]No semantically similar files found above threshold — "
            "safe to create new.[/green]"
        )
        return

    for r in results:
        fp = r.get("file_path", "")
        pct = int(r.get("similarity", 0) * 100)
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:200]
        if r.get("strong_dup"):
            console.print(
                f"[bold red]⚠ {fp}[/bold red] [yellow]({pct}% — STRONG-DUP, "
                f"likely should update not create)[/yellow]"
            )
        else:
            console.print(f"[bold blue]{fp}[/bold blue] ({pct}% similar)")
        console.print(f"  {snippet}")
        console.print()


@click.command("orphan-repair")
@click.option(
    "--link",
    "broken_link",
    required=True,
    help="The broken wikilink (with or without [[brackets]]) or bare target slug.",
)
@click.option(
    "--min-similarity",
    type=float,
    default=None,
    help="Minimum cosine similarity to surface (0.0–1.0). Default 0.65.",
)
@click.option(
    "--top-k",
    type=int,
    default=None,
    help="Maximum candidates to return. Default 10.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "text"]),
    help="Output format (default: text on TTY, json when piped).",
)
def orphan_repair(broken_link, min_similarity, top_k, fmt):
    """Find existing files semantically near a broken `[[wikilink]]` target.

    Use during wiki-maintenance passes to either propose a redirect (rename
    the link to point at one of the returned files) or to create the
    missing target file with informed context.
    """
    try:
        results = api_client.orphan_repair(
            broken_link=broken_link,
            min_similarity=min_similarity,
            top_k=top_k,
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    output_fmt = OutputFormat(fmt) if fmt else get_default_format()
    if output_fmt == OutputFormat.JSON:
        print_result(results, fmt=output_fmt)
        return

    if not results:
        console.print(
            "[yellow]No semantically related files found above threshold.[/yellow]"
        )
        return

    for r in results:
        fp = r.get("file_path", "")
        pct = int(r.get("similarity", 0) * 100)
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:200]
        console.print(f"[bold blue]{fp}[/bold blue] ({pct}% similar)")
        console.print(f"  {snippet}")
        console.print()
