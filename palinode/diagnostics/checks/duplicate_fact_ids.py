"""Check: status_fact_ids_unique

A fact id is an address: the executor resolves ``<!-- fact:id -->`` to a line
and rewrites, retires or retracts *it*. An id carried by two lines is not an
address — an UPDATE aimed at one rewrites both, an ARCHIVE of one retires both.

That happened for real. Session-end derives the id from the line's text, so two
byte-identical log lines — a repeated summary, the same session ending twice —
minted the same id, and nothing checked the document for a collision. The first
live age sweep logged six ``ARCHIVE unmatched`` warnings for five ids: the first
ARCHIVE had already taken every line its id named, so the per-line ops behind it
found nothing.

Minting is fixed at both writers, so no new duplicate appears. This is the
operator's view of the ones already on disk: nothing is lost while they sit
there — retirement still removes every line — but any op the compaction model
aims at one of them hits the others too, and that is worth seeing before the
executor surfaces it as a warning nobody was watching for.

``fast``: globs one directory and reads the status documents in it — no network,
no index access, no walk.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path

from palinode.consolidation.fact_ids import FACT_LINE_RE
from palinode.core.memory_files import list_memory_files
from palinode.core.parser import split_frontmatter
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

logger = logging.getLogger(__name__)


def duplicate_fact_ids(path: Path) -> dict[str, int]:
    """Fact ids carried by more than one body bullet in *path*, id → count.

    Body only, for the reason every other reader of these markers splits first:
    a ``- project/foo`` under ``entities:`` is YAML, not a fact, and a marker
    that landed in frontmatter is residue ``repair-status`` strips rather than a
    duplicate fact. An unreadable file reports nothing — this is a reporter.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — an unreadable doc is not this check's business
        logger.debug("status_fact_ids_unique: could not read %s: %r", path, exc)
        return {}
    _, body = split_frontmatter(content)
    counts = Counter(match[1] for match in FACT_LINE_RE.findall(body))
    return {fact_id: n for fact_id, n in sorted(counts.items()) if n > 1}


@register(tags=("fast",))
def status_fact_ids_unique(ctx: DoctorContext) -> CheckResult:
    """Warn when a status document carries the same fact id on two lines."""
    memory_dir = Path(ctx.config.memory_dir)
    projects_dir = memory_dir / "projects"
    targets = (
        sorted(
            Path(f.path)
            for f in list_memory_files(str(memory_dir), under="projects", is_recursive=False)
            if f.path.endswith("-status.md")
        )
        if projects_dir.is_dir()
        else []
    )

    if not targets:
        return CheckResult(
            name="status_fact_ids_unique",
            severity="info",
            passed=True,
            message=(
                f"No status documents under {projects_dir} — nothing carrying "
                f"fact ids for session-end to collide on yet."
            ),
            remediation=None,
            tags=("fast",),
        )

    offenders: list[tuple[str, dict[str, int]]] = []
    for target in targets:
        duplicates = duplicate_fact_ids(target)
        if duplicates:
            offenders.append((os.path.relpath(target, memory_dir), duplicates))

    if not offenders:
        return CheckResult(
            name="status_fact_ids_unique",
            severity="info",
            passed=True,
            message=(
                f"Every fact id in {len(targets)} status document(s) under "
                f"{projects_dir} names exactly one line."
            ),
            remediation=None,
            tags=("fast",),
        )

    named = ", ".join(
        f"{rel} ({len(dupes)} id{'' if len(dupes) == 1 else 's'} on "
        f"{sum(dupes.values())} lines: {', '.join(sorted(dupes)[:3])}"
        f"{', …' if len(dupes) > 3 else ''})"
        for rel, dupes in offenders
    )
    return CheckResult(
        name="status_fact_ids_unique",
        severity="warn",
        passed=False,
        message=(
            f"{len(offenders)} status document(s) carry a fact id on more than "
            f"one line, so an operation naming that id addresses every line it "
            f"is on: {named}."
        ),
        remediation=(
            "These are lines whose rendering is byte-identical, minted before "
            "the write path deduplicated ids — a repeated session summary, or "
            "one session ended twice. Nothing is lost while they sit there and "
            "age retirement removes them all together, so the safe fix is by "
            "hand, one document at a time: delete the repeated line, or give it "
            "a distinct id by editing its marker. They are not re-minted "
            "automatically because the id already in the file is what the "
            "history entries and consolidation log lines reference."
        ),
        tags=("fast",),
    )
