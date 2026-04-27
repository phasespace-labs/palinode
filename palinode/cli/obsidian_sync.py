"""`palinode obsidian-sync` — backfill the wiki-contract auto-footer to legacy files.

Walks all ``.md`` files under ``${memory_dir}`` and applies the Layer 2
``_apply_wiki_footer`` transformation in bulk.  Brings files written before
Deliverable C (palinode_save auto-footer) shipped into compliance with the
wiki contract defined in PROGRAM.md § "Wiki maintenance".

Usage
-----
    # See what would change (default — no writes):
    palinode obsidian-sync

    # Actually write the changes:
    palinode obsidian-sync --apply

    # Scope to one directory:
    palinode obsidian-sync --include "decisions/*.md"

    # Exclude a subtree:
    palinode obsidian-sync --exclude "daily/**"

    # Apply and scope:
    palinode obsidian-sync --apply --include "projects/*.md"

Exit codes
----------
0   Dry-run completed cleanly, or --apply succeeded for every file.
1   One or more files failed to parse or write; summary printed to stderr.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import sys
from typing import Optional

import click

from palinode.api.server import _apply_wiki_footer
from palinode.core.config import config
from palinode.core.parser import parse_markdown

# Directories skipped by all palinode tooling — mirrors lint.py's skip set
# plus Obsidian's private config dir.
_SKIP_DIRS: frozenset[str] = frozenset({
    "archive",
    "logs",
    ".obsidian",
    ".palinode",
})


def _walk_memory_files(
    memory_dir: str,
    include: Optional[str],
    exclude: Optional[str],
) -> list[str]:
    """Return absolute paths for all candidate .md files in *memory_dir*.

    Args:
        memory_dir: Root of the Palinode memory store.
        include: Optional glob pattern relative to *memory_dir* (e.g.
            ``"decisions/*.md"``).  When provided, only files matching this
            pattern are considered.
        exclude: Optional glob pattern relative to *memory_dir*.  Files
            matching this pattern are removed from the candidate set after
            ``include`` filtering.

    Returns:
        Sorted list of absolute file paths.
    """
    if include:
        # User supplied a specific glob — expand it relative to memory_dir.
        raw = glob.glob(os.path.join(memory_dir, include), recursive=True)
    else:
        raw = glob.glob(os.path.join(memory_dir, "**/*.md"), recursive=True)

    results: list[str] = []
    for filepath in raw:
        rel = os.path.relpath(filepath, memory_dir)
        parts = rel.split(os.sep)

        # Skip top-level skip_dirs
        if parts[0] in _SKIP_DIRS:
            continue

        # Apply exclude glob (relative to memory_dir)
        if exclude and fnmatch.fnmatch(rel, exclude):
            continue

        results.append(filepath)

    return sorted(results)


@click.command("obsidian-sync")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Write changes to disk.  Omit for a dry-run (default).",
)
@click.option(
    "--include",
    "include_glob",
    default=None,
    metavar="GLOB",
    help=(
        "Restrict to files matching this glob relative to memory_dir "
        "(e.g. 'decisions/*.md').  Supports ** for recursive matching."
    ),
)
@click.option(
    "--exclude",
    "exclude_glob",
    default=None,
    metavar="GLOB",
    help=(
        "Skip files matching this glob relative to memory_dir "
        "(e.g. 'daily/**').  Applied after --include."
    ),
)
def obsidian_sync(apply: bool, include_glob: Optional[str], exclude_glob: Optional[str]) -> None:
    """Backfill the wiki-contract auto-footer to legacy memory files.

    Walks all .md files under memory_dir and applies the Layer 2
    ``_apply_wiki_footer`` transformation: when a file has ``entities:`` in its
    frontmatter but no corresponding ``[[wikilinks]]`` in the body, a detectable
    ``## See also`` footer is appended so that Obsidian graph view picks up the
    links.

    Default is a DRY RUN — pass ``--apply`` to write changes.

    Already-synced files (those with an up-to-date auto-footer) are silently
    skipped (idempotent).  Files with no ``entities:`` frontmatter are also
    skipped.
    """
    # Read memory_dir at invocation time so that environment variable overrides
    # (e.g. PALINODE_DIR set by the caller or by CliRunner in tests) take effect
    # even though the config singleton was loaded at module import time.
    memory_dir = os.path.expanduser(
        os.environ.get("PALINODE_DIR", config.memory_dir)
    )
    if not os.path.isdir(memory_dir):
        click.echo(
            f"Error: memory_dir does not exist or is not a directory: {memory_dir}",
            err=True,
        )
        sys.exit(1)

    files = _walk_memory_files(memory_dir, include_glob, exclude_glob)

    if not files:
        mode = "apply" if apply else "dry-run"
        click.echo(f"No .md files found under {memory_dir} ({mode}). Summary: 0 would be updated, 0 unchanged.")
        return

    would_update: list[str] = []
    unchanged: list[str] = []
    errors: list[tuple[str, str]] = []

    for filepath in files:
        rel = os.path.relpath(filepath, memory_dir)
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                original = fh.read()
        except OSError as exc:
            errors.append((rel, f"read error: {exc}"))
            continue

        # Parse frontmatter to extract entities.
        try:
            metadata, _ = parse_markdown(original)
        except Exception as exc:  # noqa: BLE001
            errors.append((rel, f"parse error: {exc}"))
            continue

        raw_entities = metadata.get("entities", [])
        if not raw_entities:
            # No entities → nothing to backfill.
            unchanged.append(rel)
            continue

        entities: list[str] = [str(e).strip() for e in raw_entities if e]
        if not entities:
            unchanged.append(rel)
            continue

        # Apply the transformation.
        updated = _apply_wiki_footer(original, entities)

        if updated == original:
            unchanged.append(rel)
            continue

        # File would change.
        # Derive which slugs were added for the log line.
        added_slugs = _slugs_added(original, updated)
        slug_str = " ".join(f"[[{s}]]" for s in added_slugs) if added_slugs else "(footer updated)"

        if apply:
            try:
                with open(filepath, "w", encoding="utf-8") as fh:
                    fh.write(updated)
                click.echo(f"updated: {rel} (added: {slug_str})")
            except OSError as exc:
                errors.append((rel, f"write error: {exc}"))
                continue
        else:
            click.echo(f"would update: {rel} (added: {slug_str})")

        would_update.append(rel)

    # --- Summary ---
    n_update = len(would_update)
    n_unchanged = len(unchanged)
    n_errors = len(errors)

    if apply:
        summary = f"Summary: {n_update} updated, {n_unchanged} unchanged"
    else:
        summary = f"Summary: {n_update} would be updated, {n_unchanged} unchanged"

    if n_errors:
        summary += f", {n_errors} error(s)"

    click.echo(summary)

    if errors:
        click.echo("Errors:", err=True)
        for rel, msg in errors:
            click.echo(f"  {rel}: {msg}", err=True)
        sys.exit(1)


def _slugs_added(original: str, updated: str) -> list[str]:
    """Return the list of slugs that appear in the new auto-footer but not the old.

    Used purely for the dry-run / apply log line — e.g.
    ``"added: [[alice]] [[palinode]]"``.
    """
    import re

    _FOOTER_MARKER = "<!-- palinode-auto-footer -->"
    _LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

    def _footer_slugs(content: str) -> set[str]:
        marker_pos = content.find(_FOOTER_MARKER)
        if marker_pos == -1:
            return set()
        footer_section = content[marker_pos:]
        return set(_LINK_RE.findall(footer_section))

    old_slugs = _footer_slugs(original)
    new_slugs = _footer_slugs(updated)
    added = new_slugs - old_slugs
    return sorted(added)
