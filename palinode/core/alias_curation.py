"""Curating the store's ``entity-aliases.yaml``: list, add, remove, check.

:mod:`palinode.core.aliases` is the read side — it resolves refs through the
file at query time and never writes. This module is the operator's write side,
behind ``palinode aliases`` and the ``/aliases`` routes.

The rules it keeps:

* **The file is always** ``<PALINODE_DIR>/entity-aliases.yaml``. No caller
  names a path; :func:`palinode.core.aliases.alias_file_path` decides it.
* **Memory files are never touched.** Aliases stay query-time; only the alias
  file is written.
* **One ref, one group.** Putting a ref that already belongs to another group
  into a second one is refused unless the caller passes ``move``. Project refs
  compare case-insensitively here, as they do in recall isolation, so
  ``project/Harbor`` and ``project/harbor`` count as the same ref for this rule.
* **Deterministic output.** Groups sorted by canonical, members sorted, stable
  YAML — the same groups always produce the same bytes, so the store's git
  history shows real changes only. Comments in a hand-edited file are not kept
  across a write.
* **A malformed file is refused, not repaired.** Rewriting it would drop the
  entries the reader skips; the operator fixes it by hand.
* **Every write commits** through :mod:`palinode.core.git_tools`, and drops the
  alias cache so the next lookup in this process reads the new groups.
"""
from __future__ import annotations

import difflib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import yaml

from palinode.core import aliases, git_tools
from palinode.core.config import config
from palinode.core.typed_links import TypedLinkError, normalize_link_refs

#: Relative name reported to callers; the absolute path never leaves the server.
ALIAS_FILE = aliases.ALIAS_FILENAME

_HEADER = (
    "# Entity aliases: each key is the canonical ref, the list is the other\n"
    "# spellings of the same subject. Resolved at query time; memory files are\n"
    "# never rewritten. Written by `palinode aliases` (sorted; comments are not\n"
    "# kept). See docs/ENTITY-ALIASES.md.\n"
)

_LOCK = threading.Lock()


class AliasCurationError(ValueError):
    """A request the curation rules refuse (HTTP 400)."""

    status_code = 400


class AliasConflictError(AliasCurationError):
    """A ref already belongs to another group (HTTP 409)."""

    status_code = 409


class AliasNotFoundError(AliasCurationError):
    """The ref named for removal is in no group (HTTP 404)."""

    status_code = 404


class AliasFileError(AliasCurationError):
    """The alias file on disk cannot be read strictly (HTTP 422)."""

    status_code = 422


# ── refs ────────────────────────────────────────────────────────────────────


def validate_ref(ref: Any, field: str) -> str:
    """One entity ref in the store's ``category/name`` grammar, stripped.

    The character rules are the typed-link ref rules (no traversal, no
    whitespace, no leading slash); an entity ref additionally has exactly one
    ``/`` with a non-empty category and name.
    """
    try:
        (clean,) = normalize_link_refs([ref], field)
    except TypedLinkError as exc:
        raise AliasCurationError(str(exc)) from None
    category, _, name = clean.partition("/")
    if not category or not name or "/" in name:
        raise AliasCurationError(
            f"{field}: {clean!r} is not an entity ref (expected category/name, "
            "e.g. project/harbor)"
        )
    return clean


def _key(ref: str) -> str:
    """Conflict key: project refs compare case-insensitively, others exactly."""
    return ref.lower() if ref.lower().startswith("project/") else ref


# ── the file ────────────────────────────────────────────────────────────────


def _path() -> str:
    return aliases.alias_file_path()


def _read_document() -> tuple[dict[str, Any], dict[str, list[str]], str | None]:
    """``(other top-level keys, groups, current text)`` — strictly.

    ``groups`` maps each canonical to its sorted members (the canonical itself
    left out). Raises :class:`AliasFileError` when the file exists but is not a
    regular file or does not parse into that shape.
    """
    path = _path()
    if not os.path.lexists(path):
        return {}, {}, None
    if os.path.islink(path) or not os.path.isfile(path):
        raise AliasFileError(f"{ALIAS_FILE} is not a regular file; fix it by hand")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        doc = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AliasFileError(f"{ALIAS_FILE} could not be read ({exc}); fix it by hand") from None
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise AliasFileError(f"{ALIAS_FILE} is not a mapping; fix it by hand")
    raw_groups = doc.get("aliases")
    if raw_groups is None:
        raw_groups = {}
    if not isinstance(raw_groups, dict):
        raise AliasFileError(f"{ALIAS_FILE}: `aliases` is not a mapping; fix it by hand")
    groups: dict[str, list[str]] = {}
    for canonical, members in raw_groups.items():
        if not isinstance(canonical, str) or not canonical.strip():
            raise AliasFileError(f"{ALIAS_FILE}: a canonical ref is not a string; fix it by hand")
        canonical = canonical.strip()
        if members is None:
            members = []
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, list) or not all(
            isinstance(m, str) and m.strip() for m in members
        ):
            raise AliasFileError(
                f"{ALIAS_FILE}: {canonical!r} does not map to a list of refs; fix it by hand"
            )
        merged = set(groups.get(canonical, [])) | {m.strip() for m in members}
        merged.discard(canonical)
        groups[canonical] = sorted(merged)
    extra = {k: v for k, v in doc.items() if k != "aliases"}
    return extra, groups, text


def render(groups: dict[str, list[str]], extra: dict[str, Any] | None = None) -> str:
    """The file's bytes for ``groups``: header, then sorted, stable YAML."""
    body: dict[str, Any] = dict(sorted((extra or {}).items()))
    body["aliases"] = {c: sorted(set(groups[c])) for c in sorted(groups) if groups[c]}
    return _HEADER + yaml.safe_dump(
        body, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def _locations(groups: dict[str, list[str]]) -> dict[str, tuple[str, bool]]:
    """conflict key -> (the group's canonical, whether the ref is that canonical)."""
    where: dict[str, tuple[str, bool]] = {}
    for canonical in sorted(groups):
        where.setdefault(_key(canonical), (canonical, True))
        for member in groups[canonical]:
            where.setdefault(_key(member), (canonical, False))
    return where


# ── the entities index ──────────────────────────────────────────────────────


def entity_file_counts() -> dict[str, int] | None:
    """entity ref -> distinct file count from the index; None without an index."""
    db_path = Path(config.db_path).expanduser()
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            "SELECT entity_ref, COUNT(DISTINCT file_path) FROM entities GROUP BY entity_ref"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return {str(ref): int(n) for ref, n in rows}


def _group_view(
    canonical: str, members: list[str], counts: dict[str, int] | None
) -> dict[str, Any]:
    def files(ref: str) -> int | None:
        return None if counts is None else counts.get(ref, 0)

    return {
        "canonical": canonical,
        "files": files(canonical),
        "members": [{"ref": m, "files": files(m)} for m in members],
    }


# ── operations ──────────────────────────────────────────────────────────────


def list_groups() -> dict[str, Any]:
    """Every group, each ref with its file count from the entities index."""
    _extra, groups, text = _read_document()
    counts = entity_file_counts()
    return {
        "file": ALIAS_FILE,
        "exists": text is not None,
        "indexed": counts is not None,
        "groups": [_group_view(c, groups[c], counts) for c in sorted(groups)],
    }


def _apply(
    op: str,
    summary: str,
    extra: dict[str, Any],
    before: dict[str, list[str]],
    after: dict[str, list[str]],
    before_text: str | None,
    dry_run: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Shared tail of add/remove: diff, then (unless dry run) write + commit."""
    after = {c: m for c, m in after.items() if m}
    changed = after != {c: m for c, m in before.items() if m}
    new_text = render(after, extra) if changed else (before_text or "")
    diff = "".join(difflib.unified_diff(
        (before_text or "").splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{ALIAS_FILE}", tofile=f"b/{ALIAS_FILE}",
    )) if changed else ""
    result.update({
        "operation": op,
        "file": ALIAS_FILE,
        "dry_run": dry_run,
        "changed": changed,
        "diff": diff,
        "committed": False,
        "commit_error": None,
    })
    if dry_run or not changed:
        return result
    path = _path()
    git_tools.write_memory_file(path, new_text)
    # A same-tick, same-size rewrite must not be served from cache; the reader's
    # inode stamp covers other processes, this covers this one immediately.
    aliases.reset_cache()
    outcome = git_tools.try_commit_memory_files(
        [path], f"{config.git.commit_prefix} aliases {op}: {summary}"
    )
    result["committed"] = outcome.committed
    result["commit_error"] = outcome.error
    return result


def add(
    canonical: str,
    members: list[str],
    *,
    move: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create the group ``canonical`` or extend it with ``members``.

    A ref already in another group is refused unless ``move``; with it, the ref
    leaves that group (an emptied group is removed). A ref that is another
    group's canonical is always refused — moving it would orphan that group's
    members — so remove or regroup that group first.
    """
    canonical = validate_ref(canonical, "canonical")
    if not isinstance(members, list) or not members:
        raise AliasCurationError("name at least one member to add")
    clean: list[str] = []
    for i, member in enumerate(members):
        ref = validate_ref(member, f"members[{i}]")
        if ref != canonical and ref not in clean:
            clean.append(ref)
    if not clean:
        raise AliasCurationError("a group needs at least one member besides its canonical")

    with _LOCK:
        extra, before, before_text = _read_document()
        after = {c: list(m) for c, m in before.items()}
        where = _locations(before)
        moved: list[dict[str, str]] = []

        def take(ref: str, owner: str) -> None:
            k = _key(ref)
            after[owner] = [m for m in after[owner] if _key(m) != k]
            moved.append({"ref": ref, "from": owner})

        loc = where.get(_key(canonical))
        if loc is not None and loc[0] != canonical:
            owner, is_canonical = loc
            if is_canonical:
                raise AliasConflictError(
                    f"{canonical} is the same project as the existing canonical "
                    f"{owner}; add members to {owner} instead"
                )
            if not move:
                raise AliasConflictError(
                    f"{canonical} is already a member of {owner}; pass move to "
                    "take it out of that group"
                )
            take(canonical, owner)

        for ref in clean:
            loc = where.get(_key(ref))
            if loc is None or loc[0] == canonical:
                continue
            owner, is_canonical = loc
            if is_canonical:
                raise AliasConflictError(
                    f"{ref} is the canonical of its own group; remove that group's "
                    "members first, or add them to this group with move"
                )
            if not move:
                raise AliasConflictError(
                    f"{ref} is already a member of {owner}; pass move to regroup it"
                )
            take(ref, owner)

        existing = set(after.get(canonical, []))
        added = [r for r in clean if r not in existing]
        after[canonical] = sorted(existing | set(clean))
        removed_groups = sorted(c for c in before if before[c] and not after.get(c))
        result: dict[str, Any] = {
            "canonical": canonical,
            "added": added,
            "moved": moved,
            "removed_groups": removed_groups,
        }
        summary = f"{canonical} += {', '.join(added) or '(nothing new)'}"
        if moved:
            summary += " (moved: " + ", ".join(f"{m['ref']} from {m['from']}" for m in moved) + ")"
        return _apply("add", summary, extra, before, after, before_text, dry_run, result)


def remove(member: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Drop ``member`` from its group; a group left with no members is removed.

    A canonical is not removable this way — its members would be left without
    a group. Remove each member instead; the last removal removes the group.
    """
    member = validate_ref(member, "member")
    with _LOCK:
        extra, before, before_text = _read_document()
        owner = next((c for c in sorted(before) if member in before[c]), None)
        if owner is None:
            if member in before:
                raise AliasCurationError(
                    f"{member} is a group's canonical; remove its members instead "
                    "(the last removal removes the group)"
                )
            raise AliasNotFoundError(f"{member} is in no alias group")
        after = {c: list(m) for c, m in before.items()}
        after[owner] = [m for m in after[owner] if m != member]
        result: dict[str, Any] = {
            "member": member,
            "canonical": owner,
            "removed_groups": [owner] if not after[owner] else [],
        }
        return _apply(
            "remove", f"{member} from {owner}", extra, before, after, before_text,
            dry_run, result,
        )


def check() -> dict[str, Any]:
    """The alias lint over the entities index, plus ``project_tags_unmapped``.

    Each lint cluster is marked ``grouped`` when every ref in it already sits in
    one alias group, so what is left open is what still needs a decision.
    """
    from palinode.core.lint import check_entity_aliases
    from palinode.diagnostics.checks.project_tags import project_tags_unmapped
    from palinode.diagnostics.types import DoctorContext

    file_error: str | None = None
    try:
        _extra, groups, _text = _read_document()
    except AliasFileError as exc:
        file_error, groups = str(exc), {}
    where = _locations(groups)
    counts = entity_file_counts()
    clusters = check_entity_aliases(counts or {})
    for cluster in clusters:
        owners = {where.get(_key(r["ref"]), (None,))[0] for r in cluster.get("refs", [])}
        cluster["grouped"] = len(owners) == 1 and None not in owners
    doctor = project_tags_unmapped(DoctorContext(config=config))
    return {
        "file": ALIAS_FILE,
        "file_error": file_error,
        "indexed": counts is not None,
        "alias_candidates": clusters,
        "open_candidates": sum(1 for c in clusters if not c["grouped"]),
        "project_tags_unmapped": {
            "passed": doctor.passed,
            "severity": doctor.severity,
            "message": doctor.message,
            "remediation": doctor.remediation,
        },
    }
