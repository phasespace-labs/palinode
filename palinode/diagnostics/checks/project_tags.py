"""Check: project_tags_unmapped

A request whose project resolves is isolated to it: recall leaves out records
tagged to a *different* project. The comparison is case-insensitive and goes
through the store's curated ``entity-aliases.yaml``, but a store whose records
spell one project several ways (``project/harbor`` beside the resolved
``project/harbor-dev``; ``orbit-app``, ``orbit``, ``orbitapp`` for one
repository) still has every unaliased spelling treated as another project,
silently.

This check lists the ``project/*`` tags carried by at least
:data:`MIN_FILES` files that the operator's configuration does not cover:
not a canonical ref or a member of a group in ``entity-aliases.yaml``, and
not the target of a ``project_map`` entry. It is a warning — an unmapped tag may be a real,
separate project — and it names the counts so the operator can decide which
spellings belong together.

``fast``: one grouped query over the index's ``entities`` table, read-only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from palinode.core.scope import alias_index, canonical_project
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

NAME = "project_tags_unmapped"

#: Tags on fewer files than this are left out: a stray tag on a handful of
#: records is noise, not a fragmented project.
MIN_FILES = 10

#: How many unmapped tags the message names; the rest are counted.
MAX_LISTED = 15


def _project_tag_counts(db_path: Path) -> dict[str, int] | None:
    """``project/*`` entity ref → distinct file count, or None without an index."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            "SELECT entity_ref, COUNT(DISTINCT file_path) FROM entities "
            "WHERE lower(entity_ref) LIKE 'project/%' GROUP BY entity_ref"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return {str(ref): int(n) for ref, n in rows}


def covered_keys(config) -> set[str]:
    """Keys the operator covered: alias-file members and canonicals, map targets."""
    index = alias_index()
    covered = set(index)
    for target in (getattr(config.context, "project_map", None) or {}).values():
        covered.add(canonical_project(str(target), index))
    return covered


def unmapped_project_tags(config, counts: dict[str, int]) -> list[tuple[str, int]]:
    """Tags with at least :data:`MIN_FILES` files that nothing configured covers."""
    index = alias_index()
    covered = covered_keys(config)
    out = [
        (tag, n) for tag, n in counts.items()
        if n >= MIN_FILES and canonical_project(tag, index) not in covered
    ]
    return sorted(out, key=lambda item: (-item[1], item[0]))


@register(tags=("fast",))
def project_tags_unmapped(ctx: DoctorContext) -> CheckResult:
    """Warn about large project tags recall isolation will treat as other projects."""
    db_path = Path(ctx.config.db_path).expanduser()
    counts = _project_tag_counts(db_path) if db_path.exists() else None
    if counts is None:
        return CheckResult(
            name=NAME, severity="info", passed=True,
            message="No index to read project tags from — skipped.",
            tags=("fast",),
        )
    unmapped = unmapped_project_tags(ctx.config, counts)
    if not unmapped:
        return CheckResult(
            name=NAME, severity="info", passed=True,
            message=(
                f"Every project tag on {MIN_FILES}+ files is a configured project "
                "or alias."
            ),
            tags=("fast",),
        )
    listed = ", ".join(f"{tag} ({n})" for tag, n in unmapped[:MAX_LISTED])
    more = len(unmapped) - MAX_LISTED
    if more > 0:
        listed += f", and {more} more"
    return CheckResult(
        name=NAME, severity="warn", passed=False,
        message=(
            "Fragmented or unmapped project tags: recall isolation will treat these "
            f"as other projects — {listed}"
        ),
        remediation=(
            "Group the spellings of one project in entity-aliases.yaml in the "
            "memory directory (`palinode aliases add project/<slug> <other "
            "project/ refs>`), or map the repository with context.project_map in "
            "palinode.config.yaml. A tag that really is a separate project needs "
            "nothing. See docs/ENTITY-ALIASES.md."
        ),
        tags=("fast",),
    )
