"""palinode repair-status — one-time repair of rotted status documents (#679/#470).

Status docs written before the consolidation write path was fixed carry three
kinds of damage that no amount of forward-only fixing removes:

* a ``## Consolidation Log`` of blank-rationale entries and ``fact_id``s that
  reference nothing (self-nesting ``supersedes-supersedes-…`` chains, bracketed
  pseudo-ids, whole paragraphs of model deliberation in the id slot);
* frontmatter counts and date ranges months out of date with the body;
* ``entities:`` entries carrying ``<!-- fact:… -->`` residue, or replaced
  wholesale by status prose (#470).

This runs the *same* rules the writer now applies, plus the block cap. It is
deliberately local + dry-run by default: it edits memory files in place with no
git commit, so the operator reviews the diff and commits.

Two scopes, split by how much judgement the transformation needs:

``--scope status`` (default)
    Full repair of ``projects/*-status.md``: log re-render + bounding, entity
    relocation, frontmatter re-dump and reconciliation.

``--scope all``
    The above, plus a *marker-strip only* pass over every other memory file in
    ``people/``, ``projects/``, ``decisions/`` and ``insights/`` — the same four
    directories ``bootstrap_all_fact_ids`` walks. Nothing else about those files
    is touched: no re-dump, no log rewrite, no entity relocation. This exists
    because the marker residue actively fragments the entity graph (a marker
    lands inside the entity ref's string value), and that damage is store-wide
    while the log/frontmatter damage is status-doc-specific.

``--execute`` repoints the index itself — it does not rely on a follow-up
``palinode reindex``, which would not work: this repair is a frontmatter-only
change, so it does not move the body content-hash the indexer keys its fast
path on, and a re-index reports every repaired file unchanged.
"""
from __future__ import annotations

import glob
import json as _json
import os

import click

from palinode.consolidation.status_doc import (
    DEFAULT_MAX_LOG_BLOCKS,
    fact_ids,
    repair_status_doc,
    strip_frontmatter_fact_markers,
)
from palinode.core.config import config
from palinode.core.parser import split_frontmatter

#: The directories ``bootstrap_all_fact_ids`` walks — i.e. every directory whose
#: frontmatter could have been fact-tagged.
MEMORY_DIRS = ("people", "projects", "decisions", "insights")


def _propagate_entities(path: str, content: str) -> int:
    """Push a repaired file's ``entities:`` into the index.

    Writing the file is not enough. This repair is a frontmatter-only change,
    so it does not move the body content-hash ``index_file`` keys its fast path
    on: a subsequent ``palinode reindex`` reports the file unchanged and leaves
    the corrected refs stale in both ``chunks.metadata`` and the ``entities``
    table. Repairing the file and leaving the index disagreeing with it is the
    same defect this command exists to fix, one layer down.

    Best-effort by design: a store that is unreachable, unreadable, or simply
    absent (repairing a checkout with no index) must not fail the file repair —
    the file on disk is the source of truth and is already correct.

    Returns the number of chunk rows updated (0 if nothing was propagated).
    """
    import yaml

    from palinode.core import store

    try:
        frontmatter_block, _ = split_frontmatter(content)
        if not frontmatter_block:
            return 0
        meta = yaml.safe_load(frontmatter_block.strip().strip("-")) or {}
        if not isinstance(meta, dict):
            return 0
        entities = meta.get("entities") or []
        if not isinstance(entities, list):
            return 0
        entities = [str(e) for e in entities if isinstance(e, (str, int, float))]
        return store.set_entities_for_path(os.path.abspath(path), entities)
    except Exception:
        return 0


def _status_targets() -> list[str]:
    pattern = os.path.join(config.memory_dir, "projects", "*-status.md")
    return sorted(glob.glob(pattern))


def _all_memory_targets() -> list[str]:
    found: list[str] = []
    for directory in MEMORY_DIRS:
        found.extend(glob.glob(os.path.join(config.memory_dir, directory, "*.md")))
    return sorted(set(found))


def _history_fact_ids(status_path: str) -> set[str]:
    """Fact ids recorded in the sibling ``-history.md``.

    A fact the executor ARCHIVE'd is gone from the status doc but survives in
    history — without this its log entries would all be flagged unresolved.
    """
    history_path = status_path[: -len("-status.md")] + "-history.md"
    try:
        with open(history_path, encoding="utf-8") as f:
            return fact_ids(f.read())
    except OSError:
        return set()


@click.command(name="repair-status")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.option("--execute", is_flag=True, default=False,
              help="Write the repaired files (default: dry-run, report only).")
@click.option("--max-blocks", type=int, default=DEFAULT_MAX_LOG_BLOCKS,
              show_default=True,
              help="Consolidation Log date blocks to retain verbatim (0 = no cap).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the per-file report as JSON.")
@click.option("--scope", type=click.Choice(["status", "all"]), default="status",
              show_default=True,
              help="'status' fully repairs projects/*-status.md; 'all' adds a "
                   "frontmatter-marker-strip pass over every other memory file.")
def repair_status(paths: tuple[str, ...], execute: bool, max_blocks: int,
                  as_json: bool, scope: str) -> None:
    """Repair rotted memory documents (dry-run by default).

    With no PATHS, every ``projects/*-status.md`` under the configured memory
    directory is fully repaired. ``--scope all`` additionally strips fact
    markers wrongly tagged into the frontmatter of every other memory file,
    which is where the entity-graph damage lives. Nothing is committed — review
    the diff yourself. The index is repointed as part of ``--execute``; no
    follow-up re-index is needed (and would not help).
    """
    targets = list(paths) or _status_targets()
    extras = [] if paths else (
        [p for p in _all_memory_targets() if p not in set(targets)]
        if scope == "all" else []
    )
    if not targets and not extras:
        click.echo("No memory documents found.")
        return

    reports: list[dict] = []
    for path in targets:
        with open(path, encoding="utf-8") as f:
            original = f.read()
        extra = _history_fact_ids(path) if path.endswith("-status.md") else set()
        repaired, report = repair_status_doc(
            original, max_blocks=max_blocks, extra_known_ids=extra
        )
        changed = repaired != original
        report = {
            "file": path,
            "changed": changed,
            "bytes_before": len(original),
            "bytes_after": len(repaired),
            **report,
        }
        reports.append(report)
        if changed and execute:
            with open(path, "w", encoding="utf-8") as f:
                f.write(repaired)
            report["chunks_repointed"] = _propagate_entities(path, repaired)

    # Marker-strip only. These files get no re-dump, no log rewrite, no entity
    # relocation — the transformation is confined to removing text the tagger
    # should never have written.
    for path in extras:
        with open(path, encoding="utf-8") as f:
            original = f.read()
        stripped, count = strip_frontmatter_fact_markers(original)
        if not count:
            continue
        reports.append({
            "file": path,
            "changed": True,
            "bytes_before": len(original),
            "bytes_after": len(stripped),
            "frontmatter_markers_stripped": count,
            "entities_relocated": 0,
            "log_lines_elided": 0,
            "log_ids_unresolved": 0,
            "markers_only": True,
        })
        if execute:
            with open(path, "w", encoding="utf-8") as f:
                f.write(stripped)
            reports[-1]["chunks_repointed"] = _propagate_entities(path, stripped)

    if as_json:
        click.echo(_json.dumps({"dry_run": not execute, "files": reports}, indent=2))
        return

    for report in reports:
        if not report["changed"]:
            click.echo(f"  [ok] {report['file']} — already clean")
            continue
        verb = "repaired" if execute else "would repair"
        if report.get("markers_only"):
            click.echo(
                f"  [{verb}] {report['file']} "
                f"({report['frontmatter_markers_stripped']} frontmatter "
                f"marker(s) stripped; markers only)"
            )
            continue
        click.echo(
            f"  [{verb}] {report['file']} "
            f"({report['bytes_before']} → {report['bytes_after']} bytes; "
            f"{report['log_lines_elided']} log line(s) elided, "
            f"{report['log_ids_unresolved']} id(s) marked unresolved, "
            f"{report['frontmatter_markers_stripped']} frontmatter marker(s) stripped, "
            f"{report['entities_relocated']} entity value(s) relocated)"
        )

    pending = sum(1 for r in reports if r["changed"])
    markers = sum(r["frontmatter_markers_stripped"] for r in reports)
    if not execute and pending:
        click.echo(
            f"\n{pending} file(s) would be repaired "
            f"({markers} frontmatter marker(s) total). Re-run with --execute, "
            "then review the diff and commit. --execute also repoints the "
            "index, so no re-index is needed."
        )
