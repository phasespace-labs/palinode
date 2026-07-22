"""Frontmatter backfill (#309) — fill missing required fields, honestly.

138 memory files on the reference corpus predate required-field validation and
are missing ``id`` / ``type`` / ``category`` — the three fields
:func:`palinode.core.lint.run_lint_pass` reports as ``missing_fields``. Typed
recall (``palinode search --types Decision``), strict consolidation dispatch,
and lint's value as a regression signal all degrade while they are absent.

Three properties define this module, and every design choice below follows from
them:

**Non-destructive.** A field that already has a value is never rewritten, never
"corrected", never removed. The backfill only ever *adds* absent keys. A
``type: Insight`` sitting in ``decisions/`` is left exactly as it is — a
mismatch is lint's business to report, not a migration's business to silently
resolve.

**Idempotent.** The plan drives the write, not a content diff: a file with no
missing fields produces no fill, and therefore no write and no commit. Run it
twice and the second run is a no-op — the fields it would fill are the ones it
already filled.

**Honest.** Every filled value carries the derivation it came from
(:class:`FieldFill.source`), and a field with no honest derivation is left
absent and *reported* rather than guessed. This is the whole point: for an
audit-grade store a fabricated ``created_at`` is strictly worse than a missing
one. Concretely — filesystem mtime is deliberately NOT a date source (a copy,
an rsync, or a checkout rewrites it), so a store with no git history reports
``created_at``/``last_updated`` as undeliverable instead of inventing them.

Derivations
-----------
============  ================================================================
field         source
============  ================================================================
``category``  the containing top-level directory. Not an inference: the save
              path *writes* to ``<memory_dir>/<category>/<slug>.md`` and the
              indexer derives ``chunks.category`` from that same directory
              name, so the directory IS the category by construction.
``type``      the canonical category→type map, the inverse of the save path's
              ``_TYPE_TO_CATEGORY``. Directory placement is chosen by type at
              save time, so this inverts a deterministic mapping.
``id``        ``{category}-{filename stem}`` — byte-identical to the id the
              save path stamps for the same file.
``created_at``  a legacy ``created:`` field if the file has one (same
              semantics, just the pre-canonical spelling), else the file's
              first git commit date, else UNDELIVERABLE.
``last_updated``  the file's most recent git commit date, else UNDELIVERABLE.
============  ================================================================

Scope
-----
The file universe is exactly :func:`palinode.core.lint.run_lint_pass`'s — the
same ``**/*.md`` glob under ``config.memory_dir`` minus the same skipped
directories — so "run the backfill, watch lint's ``missing_fields`` count drop"
is a meaningful sentence. Within that universe:

- files directly in the memory root (``README.md``, ``PROGRAM.md``, …) are
  structural docs, not memories → skipped;
- files under a directory that is not one of the save path's memory categories
  (``specs/``, ``migrations/``, …) have no honest ``category``/``type`` →
  skipped, and named in the report so the excluded set is visible rather than
  implicit;
- ``daily/`` is governed by ``daily_mode`` — see below.

``daily/`` is the structural log tier
-------------------------------------
Settled by PROGRAM.md § File tiers: a ``daily/`` note is an append-only log, not
a memory. Session-end *appends* to it (N sessions per file, which no single set
of frontmatter can honestly describe) and separately persists each session as a
fully-typed memory, so the daily file is the transcript those were extracted
from. It is therefore exempt from the required-frontmatter contract, and
``daily_mode`` defaults to ``"skip"``: the default run leaves ``daily/``
untouched and names it under ``excluded``.

``"minimal"`` remains available for an operator who wants ``id`` + ``category``
+ dates on daily notes anyway; it still never writes a ``type:``. A full typed
daily memory is not offered at all — it would need a ``SessionEnd``/``DailyLog``
value in the canonical type enum, which is a schema change, not a backfill.
"""
from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass
from datetime import date as _date
from typing import Any, Literal

import frontmatter as _frontmatter
import yaml

from palinode.api.memory_write import _MEMORY_CATEGORY_DIRS, _TYPE_TO_CATEGORY
from palinode.core import git_tools
from palinode.core.config import config

logger = logging.getLogger("palinode.migration.frontmatter_backfill")

#: The fields :func:`palinode.core.lint.run_lint_pass` reports as
#: ``missing_fields``. Written in the order the save path emits them.
REQUIRED_FIELDS: tuple[str, ...] = ("id", "category", "type")

#: Date fields the backfill will fill *only* from an honest source (a legacy
#: field of the same meaning, or git). Never invented, never taken from mtime.
PROVENANCE_FIELDS: tuple[str, ...] = ("created_at", "last_updated")

#: Directory → canonical memory type. The inverse of the save path's
#: ``_TYPE_TO_CATEGORY``; derived rather than restated so the two can't drift.
CATEGORY_TO_TYPE: dict[str, str] = {v: k for k, v in _TYPE_TO_CATEGORY.items()}

#: The one pre-canonical spelling with an exact canonical counterpart:
#: ``created`` → ``created_at`` is a rename, not a reinterpretation. Applied in
#: :func:`_plan_file`'s ``created_at`` derivation.
LEGACY_CREATED_FIELD: str = "created"

#: Legacy fields recognised but deliberately left alone, with the reason.
#: ``topic`` is here rather than mapped because no canonical field shares its
#: meaning: dropping it or folding it into ``description`` would discard or
#: reinterpret a value the author actually recorded.
UNMAPPED_LEGACY_FIELDS: dict[str, str] = {
    "topic": (
        "legacy 'topic' left in place — no canonical field shares its meaning "
        "(it is neither a description nor a title); mapping it would invent one"
    ),
}

DAILY_DIR: str = "daily"

#: ``skip`` — leave ``daily/`` alone. The default, and the tiering PROGRAM.md
#: settles: a daily note is a log, not a memory.
#: ``minimal`` — opt-in ``id`` + ``category`` + dates; still never a ``type:``.
#: A full typed daily memory is absent by design — see the module docstring.
VALID_DAILY_MODES: tuple[str, ...] = ("skip", "minimal")

#: Mirrors ``run_lint_pass``'s skip set so the two agree on the file universe.
SKIP_DIRS: frozenset[str] = frozenset({"archive", "logs", ".obsidian"})

#: ``daily/<YYYY-MM-DD>.md`` — the filename the session-end writer uses.
_DAILY_FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class BackfillError(ValueError):
    """Raised on an invalid backfill invocation (bad ``daily_mode``)."""


@dataclass(frozen=True)
class FieldFill:
    """One frontmatter field the backfill would add, and where it came from."""

    field: str
    value: Any
    #: Stable derivation identifier — ``"directory"``, ``"category-map"``,
    #: ``"filename-slug"``, ``"filename-date"``, ``"legacy:created"``,
    #: ``"git:first-commit"``, ``"git:last-commit"``.
    source: str


@dataclass(frozen=True)
class FilePlan:
    """What the backfill would do to one file, and what it refuses to do."""

    path: str
    fills: tuple[FieldFill, ...] = ()
    #: Fields that are missing and have no honest derivation available. Named
    #: with the reason so "we couldn't" is never confused with "we didn't need
    #: to".
    undeliverable: tuple[tuple[str, str], ...] = ()
    #: Fields deliberately NOT asserted even though they are missing — a policy
    #: choice, not a derivation failure (``type`` under ``daily_mode=minimal``).
    withheld: tuple[tuple[str, str], ...] = ()
    #: Non-blocking observations (unmapped legacy fields, …).
    notes: tuple[str, ...] = ()

    @property
    def changes(self) -> bool:
        return bool(self.fills)


@dataclass(frozen=True)
class BackfillPlan:
    """The whole run: what changes, what is already conformant, what is out."""

    scanned: int = 0
    planned: tuple[FilePlan, ...] = ()
    conformant: tuple[str, ...] = ()
    #: ``(path, reason)`` for every file the backfill will not touch.
    excluded: tuple[tuple[str, str], ...] = ()
    #: Files that were scanned but whose frontmatter could not be parsed.
    unreadable: tuple[tuple[str, str], ...] = ()
    daily_mode: str = "skip"


# ── Discovery ────────────────────────────────────────────────────────────────


def _iter_memory_files(base_dir: str) -> list[str]:
    """Return memory-dir-relative paths of every candidate ``.md`` file.

    Same universe as ``run_lint_pass``: a recursive ``**/*.md`` glob (which
    already skips dot-directories) minus :data:`SKIP_DIRS`. Every hit is
    additionally required to resolve *inside* ``base_dir``, so a symlink
    pointing out of the memory dir is dropped rather than followed and
    rewritten — the path-validation rule that applies to every palinode surface
    that touches a file path.
    """
    base_real = os.path.realpath(base_dir)
    out: list[str] = []
    for filepath in sorted(glob.glob(os.path.join(base_dir, "**/*.md"), recursive=True)):
        rel_path = os.path.relpath(filepath, base_dir)
        parts = rel_path.split(os.sep)
        if parts[0] in SKIP_DIRS:
            continue
        resolved = os.path.realpath(filepath)
        if resolved != base_real and not resolved.startswith(base_real + os.sep):
            logger.warning(
                "frontmatter backfill: %r resolves outside the memory dir; skipped",
                rel_path,
            )
            continue
        out.append(rel_path)
    return out


# ── Derivation ───────────────────────────────────────────────────────────────


def _is_missing(metadata: dict[str, Any], key: str) -> bool:
    """Whether ``key`` needs filling.

    Mirrors lint's ``not meta.get(field)`` truthiness exactly, so "missing" here
    means the same thing it means in the report this backfill is meant to
    clear — an empty string counts as missing, ``False``/``0`` do not occur for
    these fields.
    """
    return not metadata.get(key)


def _daily_date_from_filename(stem: str) -> str | None:
    """Return the ISO date a ``daily/`` filename encodes, or ``None``.

    Only applied inside ``daily/``, where the session-end writer names the file
    ``daily/<YYYY-MM-DD>.md`` — there the filename *is* the note's subject date
    by construction. Elsewhere a leading date is just a naming convention and is
    not treated as provenance.
    """
    match = _DAILY_FILENAME_RE.match(stem)
    if not match:
        return None
    try:
        return _date(int(match[1]), int(match[2]), int(match[3])).isoformat()
    except ValueError:
        return None


def _git_date(rel_path: str, which: Literal["first", "last"]) -> str | None:
    """Return the ISO date of a file's first/last commit, or ``None``.

    ``None`` covers every no-history case identically — not a git repo, never
    committed, git unavailable — and every one of them means the same thing to
    the caller: no honest date is available from version control.
    """
    lookup = git_tools.first_commit if which == "first" else git_tools.last_commit
    try:
        entry = lookup(rel_path)
    except (ValueError, OSError) as exc:  # traversal rejection / git I/O
        logger.debug("git %s-commit lookup failed for %r: %s", which, rel_path, exc)
        return None
    if not entry:
        return None
    return entry.get("date") or None


def _plan_file(rel_path: str, metadata: dict[str, Any]) -> FilePlan:
    """Compute the (possibly empty) plan for one in-scope memory file."""
    parts = rel_path.split(os.sep)
    category = parts[0]
    stem = os.path.splitext(parts[-1])[0]
    is_daily = category == DAILY_DIR

    fills: list[FieldFill] = []
    undeliverable: list[tuple[str, str]] = []
    withheld: list[tuple[str, str]] = []
    notes: list[str] = []

    if _is_missing(metadata, "id"):
        fills.append(FieldFill("id", f"{category}-{stem}", "filename-slug"))
    if _is_missing(metadata, "category"):
        fills.append(FieldFill("category", category, "directory"))
    if _is_missing(metadata, "type"):
        if is_daily:
            withheld.append(
                (
                    "type",
                    "a daily note is a log, not a memory — it holds N sessions "
                    "and each is already saved as its own typed memory",
                )
            )
        else:
            fills.append(FieldFill("type", CATEGORY_TO_TYPE[category], "category-map"))

    # created_at: a legacy field of identical meaning first (it is the value the
    # author actually recorded), then git, then nothing.
    if _is_missing(metadata, "created_at"):
        legacy_value = metadata.get(LEGACY_CREATED_FIELD)
        if legacy_value:
            fills.append(FieldFill("created_at", legacy_value, "legacy:created"))
        elif is_daily and (filename_date := _daily_date_from_filename(stem)):
            fills.append(FieldFill("created_at", filename_date, "filename-date"))
        elif git_created := _git_date(rel_path, "first"):
            fills.append(FieldFill("created_at", git_created, "git:first-commit"))
        else:
            undeliverable.append(
                (
                    "created_at",
                    "no legacy 'created' field and no git history for this file; "
                    "mtime is not provenance, so no value is written",
                )
            )

    if _is_missing(metadata, "last_updated"):
        if git_updated := _git_date(rel_path, "last"):
            fills.append(FieldFill("last_updated", git_updated, "git:last-commit"))
        else:
            undeliverable.append(
                (
                    "last_updated",
                    "no git history for this file; mtime is not provenance, so "
                    "no value is written",
                )
            )

    for legacy_key, reason in UNMAPPED_LEGACY_FIELDS.items():
        if legacy_key in metadata:
            notes.append(reason)

    # Emit fills in canonical save-path order regardless of discovery order.
    order = {name: i for i, name in enumerate(REQUIRED_FIELDS + PROVENANCE_FIELDS)}
    fills.sort(key=lambda f: order.get(f.field, len(order)))

    return FilePlan(
        path=rel_path,
        fills=tuple(fills),
        undeliverable=tuple(undeliverable),
        withheld=tuple(withheld),
        notes=tuple(notes),
    )


def _exclusion_reason(rel_path: str, *, daily_mode: str) -> str | None:
    """Return why ``rel_path`` is out of scope, or ``None`` if it is in scope."""
    parts = rel_path.split(os.sep)
    if len(parts) < 2:
        return "top-level document — structural, not a memory"
    top = parts[0]
    if top == DAILY_DIR:
        if daily_mode == "skip":
            return (
                "daily/ is the structural log tier, exempt from the "
                "required-frontmatter contract; re-run with "
                "daily_mode='minimal' to fill id/category/dates anyway"
            )
        return None
    if top not in _MEMORY_CATEGORY_DIRS:
        return f"{top}/ is not a memory-category directory — no honest category/type"
    return None


# ── Planning ─────────────────────────────────────────────────────────────────


def plan_backfill(
    base_dir: str | None = None,
    *,
    daily_mode: str = "skip",
) -> BackfillPlan:
    """Compute what a backfill run would change, without touching anything.

    This is the whole decision procedure; :func:`run_backfill` only executes its
    output. That split is what makes ``--dry-run`` trustworthy: the reported
    plan and the applied plan are the same object, not two code paths that could
    disagree.

    Args:
        base_dir: Memory directory to scan. Defaults to ``config.memory_dir``.
        daily_mode: One of :data:`VALID_DAILY_MODES`.

    Raises:
        BackfillError: on an unknown ``daily_mode``.
    """
    if daily_mode not in VALID_DAILY_MODES:
        raise BackfillError(
            f"Invalid daily_mode {daily_mode!r}; expected one of "
            f"{list(VALID_DAILY_MODES)}"
        )

    base = base_dir or config.memory_dir
    planned: list[FilePlan] = []
    conformant: list[str] = []
    excluded: list[tuple[str, str]] = []
    unreadable: list[tuple[str, str]] = []

    rel_paths = _iter_memory_files(base)
    for rel_path in rel_paths:
        reason = _exclusion_reason(rel_path, daily_mode=daily_mode)
        if reason is not None:
            excluded.append((rel_path, reason))
            continue
        try:
            with open(os.path.join(base, rel_path), encoding="utf-8") as fh:
                post = _frontmatter.loads(fh.read())
            metadata = dict(post.metadata)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            # Loud, not silent: an unparseable file is reported, never skipped
            # into invisibility and never rewritten on a guess at its content.
            unreadable.append((rel_path, str(exc)))
            continue

        plan = _plan_file(rel_path, metadata)
        if plan.changes:
            planned.append(plan)
        else:
            conformant.append(rel_path)

    return BackfillPlan(
        scanned=len(rel_paths),
        planned=tuple(planned),
        conformant=tuple(conformant),
        excluded=tuple(excluded),
        unreadable=tuple(unreadable),
        daily_mode=daily_mode,
    )


# ── Application ──────────────────────────────────────────────────────────────


def apply_fills(content: str, fills: tuple[FieldFill, ...]) -> str:
    """Return ``content`` with ``fills`` added to its frontmatter.

    Additive only: existing keys keep their values *and* their order, and the
    new keys are appended after them. The body is preserved verbatim (modulo the
    leading/trailing-whitespace normalisation every frontmatter round-trip in
    this codebase performs — see ``typed_links.merge_link_refs_into_content``).
    Returns ``content`` unchanged when ``fills`` is empty.
    """
    if not fills:
        return content

    post = _frontmatter.loads(content)
    meta = dict(post.metadata)
    for fill in fills:
        if meta.get(fill.field):
            # Defence in depth: the plan already excludes present values. If one
            # appeared between planning and writing, the file's value wins.
            continue
        meta[fill.field] = fill.value
    dumped = yaml.safe_dump(
        meta, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{dumped}---\n\n{post.content}\n"


def commit_message(plan: FilePlan) -> str:
    """Build the provenance commit message for one file's backfill.

    The subject names the file; the body records every filled field with the
    derivation it came from, and every field left absent with the reason. The
    commit is the audit record — reading ``git log`` alone should be enough to
    answer "where did this value come from".
    """
    prefix = config.git.commit_prefix
    lines = [
        f"{prefix} frontmatter backfill: {plan.path}",
        "",
        f"Filled {len(plan.fills)} missing field(s):",
    ]
    lines.extend(f"- {f.field}: {f.value} (source: {f.source})" for f in plan.fills)
    if plan.undeliverable:
        lines.extend(["", "Left absent — no honest derivation available:"])
        lines.extend(f"- {name}: {reason}" for name, reason in plan.undeliverable)
    if plan.withheld:
        lines.extend(["", "Deliberately not asserted:"])
        lines.extend(f"- {name}: {reason}" for name, reason in plan.withheld)
    return "\n".join(lines) + "\n"


def run_backfill(
    base_dir: str | None = None,
    *,
    apply: bool = False,
    daily_mode: str = "skip",
    commit: bool = True,
) -> dict[str, Any]:
    """Plan — and optionally apply — the frontmatter backfill.

    Dry-run is the default: ``apply=False`` computes and returns the full plan
    without opening a file for writing. With ``apply=True`` each planned file is
    written through the ``git_tools.write_memory_file`` mutation choke point and
    committed on its own via ``commit_memory_file`` (one mutation, one commit —
    #567), carrying the derivation of every value in the commit body.

    Args:
        base_dir: Memory directory to operate on. Defaults to ``config.memory_dir``.
        apply: Write and commit. When False, nothing is written.
        daily_mode: One of :data:`VALID_DAILY_MODES`.
        commit: Whether applied writes are git-committed. Only meaningful with
            ``apply=True``; ``config.git.auto_commit`` still gates the commit.

    Returns:
        A JSON-serialisable report — see the ``files`` / ``excluded`` /
        ``undeliverable`` keys. ``commits`` follows the same contract as the
        save path's ``git_committed``: it counts commits the choke point
        accepted, which on a memory dir that is not a git repo means the
        commit was attempted, not that history now records it.
    """
    base = base_dir or config.memory_dir
    plan = plan_backfill(base, daily_mode=daily_mode)
    written: list[str] = []
    commits = 0

    if apply:
        for file_plan in plan.planned:
            abs_path = os.path.join(base, file_plan.path)
            try:
                with open(abs_path, encoding="utf-8") as fh:
                    original = fh.read()
                updated = apply_fills(original, file_plan.fills)
                if updated == original:
                    continue
                git_tools.write_memory_file(abs_path, updated)
            except OSError as exc:
                # Loud: a write failure is reported as an unreadable/unwritable
                # entry rather than swallowed into a "success" count.
                logger.error("frontmatter backfill failed for %r: %s", file_plan.path, exc)
                continue
            written.append(file_plan.path)
            if commit and git_tools.commit_memory_file(abs_path, commit_message(file_plan)):
                commits += 1

    return {
        "dry_run": not apply,
        "daily_mode": plan.daily_mode,
        "scanned": plan.scanned,
        "conformant": len(plan.conformant),
        "files": [
            {
                "path": p.path,
                "fills": [
                    {"field": f.field, "value": str(f.value), "source": f.source}
                    for f in p.fills
                ],
                "undeliverable": [
                    {"field": name, "reason": reason} for name, reason in p.undeliverable
                ],
                "withheld": [
                    {"field": name, "reason": reason} for name, reason in p.withheld
                ],
                "notes": list(p.notes),
            }
            for p in plan.planned
        ],
        "excluded": [{"path": path, "reason": reason} for path, reason in plan.excluded],
        "unreadable": [{"path": path, "error": err} for path, err in plan.unreadable],
        "files_written": written,
        "commits": commits,
    }
