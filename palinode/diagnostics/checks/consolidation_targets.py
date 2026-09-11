"""Check: consolidation_targets_tagged

Consolidation harvests only the bullets that carry a ``<!-- fact:id -->``
marker: the executor addresses facts by id, so a bullet with no id is a bullet
no operation can name. A status document appended to exclusively by session-end
had, until the write path was fixed to mint them, no markers at all — so the
nightly pass collected its notes, found zero addressable facts, and skipped the
project. Measured on a real store: 449 untagged bullets, 79 consecutive
"successful" runs, not one proposal.

Nothing surfaced that. The runner logged it at INFO, the run summary folded it
into ``projects_compacted: 0``, and doctor had no check for it. This is that
check: a target document with body bullets and no markers is inert, and the
remediation is one command.

The forward path is fixed (session-end mints an id on every line it appends),
so this fires on two populations: stores that predate the fix, and documents
built by hand or by an importer that does not mint.

``fast``: globs one directory and reads the recent daily notes — bounded file
reads, no network, no index access.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from palinode.consolidation.fact_ids import count_body_facts
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

logger = logging.getLogger(__name__)

#: How a daily note names a project. Mirrors the runner's grouping, which keys
#: on ``project/`` entity refs.
_PROJECT_REF_RE = re.compile(r"project/([A-Za-z0-9][A-Za-z0-9._-]*)")

#: Cap on daily notes read while deriving targets, so a store with years of
#: notes cannot turn a ``fast`` check into a directory walk.
_MAX_DAILY_NOTES = 30


def _status_targets(projects_dir: Path) -> list[Path]:
    """Every ``projects/*-status.md`` — the layer session-end appends to."""
    return sorted(projects_dir.glob("*-status.md"))


def _target_for(projects_dir: Path, project_id: str) -> Path | None:
    """The document a compaction of *project_id* would write into.

    Mirrors ``runner._target_file_for`` — status layer first, project file as
    fallback, ``None`` when neither exists. Reimplemented here rather than
    imported because importing the runner pulls in the store and the embedder
    (about a second) for two ``exists`` calls; ``tests/
    test_doctor_consolidation_targets.py`` asserts the two agree so the
    duplication cannot drift unnoticed.
    """
    status_file = projects_dir / f"{project_id}-status.md"
    if status_file.is_file():
        return status_file
    project_file = projects_dir / f"{project_id}.md"
    if project_file.is_file():
        return project_file
    return None


def _recent_daily_targets(memory_dir: Path, projects_dir: Path, lookback_days: int) -> list[Path]:
    """Targets for the projects that recent daily notes mention.

    Catches the case the ``*-status.md`` glob misses: a project whose target is
    the plain ``projects/<slug>.md``. Scoped to the notes a pass would actually
    collect, so an untagged document nothing references is not reported — it is
    not a consolidation target until something names it.
    """
    daily_dir = memory_dir / "daily"
    if not daily_dir.is_dir():
        return []
    cutoff = (datetime.now(UTC) - timedelta(days=max(lookback_days, 1))).strftime("%Y-%m-%d")
    notes = sorted(
        (path for path in daily_dir.glob("*.md") if path.stem >= cutoff),
        reverse=True,
    )[:_MAX_DAILY_NOTES]

    found: list[Path] = []
    for note in notes:
        try:
            text = note.read_text(encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 — an unreadable note is not this check's business
            logger.debug("consolidation_targets_tagged: could not read %s: %r", note, exc)
            continue
        for project_id in set(_PROJECT_REF_RE.findall(text)):
            target = _target_for(projects_dir, project_id)
            if target is not None:
                found.append(target)
    return found


@register(tags=("fast",))
def consolidation_targets_tagged(ctx: DoctorContext) -> CheckResult:
    """Warn when a consolidation target has body bullets but no fact markers."""
    memory_dir = Path(ctx.config.memory_dir)
    projects_dir = memory_dir / "projects"
    if not projects_dir.is_dir():
        return CheckResult(
            name="consolidation_targets_tagged",
            severity="info",
            passed=True,
            message=(
                f"No projects directory — {projects_dir} does not exist, so there "
                f"is no consolidation target to check."
            ),
            remediation=None,
            tags=("fast",),
        )

    lookback_days = getattr(ctx.config.consolidation, "lookback_days", 7)
    targets: list[Path] = _status_targets(projects_dir)
    seen = {str(path) for path in targets}
    for path in _recent_daily_targets(memory_dir, projects_dir, lookback_days):
        if str(path) not in seen:
            seen.add(str(path))
            targets.append(path)

    if not targets:
        return CheckResult(
            name="consolidation_targets_tagged",
            severity="info",
            passed=True,
            message=(
                f"No consolidation target documents under {projects_dir} — nothing "
                f"for a pass to compact into yet."
            ),
            remediation=None,
            tags=("fast",),
        )

    inert: list[tuple[Path, int]] = []
    tagged_targets = 0
    empty_targets = 0
    for target in sorted(targets, key=str):
        bullets, tagged = count_body_facts(str(target))
        if bullets == 0:
            empty_targets += 1
        elif tagged == 0:
            inert.append((target, bullets))
        else:
            tagged_targets += 1

    if inert:
        rel = [os.path.relpath(path, memory_dir) for path, _ in inert]
        named = ", ".join(
            f"{os.path.relpath(path, memory_dir)} ({bullets} untagged bullet"
            f"{'' if bullets == 1 else 's'})"
            for path, bullets in inert
        )
        fixes = "\n".join(f"  palinode bootstrap-ids --file {path}" for path in rel)
        return CheckResult(
            name="consolidation_targets_tagged",
            severity="warn",
            passed=False,
            message=(
                f"{len(inert)} consolidation target(s) carry body bullets but no "
                f"<!-- fact:id --> markers, so consolidation skips them and reports "
                f"the pass as successful: {named}."
            ),
            remediation=(
                "Mint ids for the bullets already there — the runner addresses facts "
                "by id, so until then every pass over these projects proposes "
                f"nothing:\n{fixes}\n"
                "(`palinode bootstrap-ids` with no --file does the whole store.) "
                "Session-end now mints an id on each line it appends, so this does "
                "not come back."
            ),
            tags=("fast",),
        )

    parts = []
    if tagged_targets:
        parts.append(f"{tagged_targets} carry fact markers")
    if empty_targets:
        parts.append(f"{empty_targets} have no body bullets yet")
    message = (
        f"No consolidation target under {projects_dir} holds untagged bullets "
        f"({', '.join(parts)})."
    )
    return CheckResult(
        name="consolidation_targets_tagged",
        severity="info",
        passed=True,
        message=message,
        remediation=None,
        tags=("fast",),
    )
