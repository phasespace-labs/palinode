"""``backed_by`` propagation — retiring a source flags its dependents for review.

``backed_by`` (:mod:`palinode.core.typed_links`) is an *extension* edge: memory
B saying ``backed_by: [insights/a]`` means B's claim rests on A, so a change to
A can entail a change to B. Until this module existed the link was recorded and
never acted on — A could be superseded, retracted, archived or merged away and
B kept asserting a claim whose support was struck through, with nothing marking
B for a second look. ``contradicts`` is the other kind of edge, an
*association*: two memories disagree and neither wins. It is surfaced by
``lint`` and deliberately never propagated; that asymmetry is the whole design.

What propagation does, and does not do:

- **Flags, never rewrites.** Each dependent gains one ``stale_backing`` entry in
  its frontmatter naming the source ref, the retirement kind, the retired fact
  ids, the reason, and a timestamp. The body is untouched, ``status`` is
  untouched — the dependent stays live in recall, now visibly contested. The
  LLM never writes this; it is produced by the deterministic path (the executor
  and the on-demand archive/retract ops) and committed with provenance.
- **One hop.** Dependents of dependents are not walked. A flagged dependent is
  input to the next consolidation pass, which may retire *it*, and only then
  do its own dependents get flagged.
- **Idempotent per source.** A dependent already carrying an entry for a given
  source ref is not touched again, whatever else happens to that source, so a
  re-run of the same ops is a no-op and repeated retirements of one source do
  not pile up entries. The flag clears when the dependent is re-saved: the save
  path rebuilds frontmatter from its own inputs, so a re-save is precisely the
  "I have re-verified this against its sources" action, and no new verb is
  needed.
- **Archived dependents are skipped.** A ``status: archived`` memory asserts
  nothing in recall, so there is nothing to review. The gap that leaves — a
  source retired *while* its dependent was archived — is closed on the way
  back: a restore re-checks the restored memory's own ``backed_by`` refs
  against the sources' current state (:func:`stale_backing_on_restore`) and
  flags each one that is archived or gone, under ``op: restore-check``.

Lookup is a frontmatter scan of the memory dir (``backed_by`` lives only in
frontmatter — there is no links table to query), sorted so the write order and
the commit are deterministic for a given store state.
"""
from __future__ import annotations

import glob
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.typed_links import parse_link_refs

logger = logging.getLogger("palinode.consolidation.propagate")

#: Frontmatter field on the dependent. A list of entries, one per source ref.
STALE_BACKING_FIELD = "stale_backing"

#: Retirement kinds that propagate, in the order they are named in an entry.
RETIRING_OPS: tuple[str, ...] = ("supersede", "archive", "retract", "merge")

#: The ``op`` of an entry written by the restore-time re-check rather than by a
#: retirement: it records what produced the flag, not what retired the source
#: (that lives in the entry's ``reason``).
RESTORE_CHECK_OP = "restore-check"

_OP_ORDER: tuple[str, ...] = (*RETIRING_OPS, RESTORE_CHECK_OP)

# Directories that are never dependents (mirrors lint / review / cross_refs).
_SKIP_DIRS: frozenset[str] = frozenset(
    {"archive", "logs", ".obsidian", ".git", "daily", "inbox", "prompts"}
)


def _utc_now() -> datetime:
    """Timezone-aware UTC now (module-level so tests can pin it)."""
    return datetime.now(UTC)


def _normalize_ref(ref: str) -> str:
    """Canonical comparison form of a ``category/slug`` ref (no ``.md``)."""
    r = ref.strip().replace(os.sep, "/").lstrip("/")
    return r[:-3] if r.endswith(".md") else r


def source_refs_for(file_path: str, base_dir: str | None = None) -> list[str]:
    """The refs a dependent might cite to name ``file_path`` as its source.

    ``project/foo`` for ``project/foo.md``; a ``-status.md`` layer also answers
    to its base ref (``project/foo`` for ``project/foo-status.md``) because the
    history writer treats the two as one memory. Returns ``[]`` when the file
    is not inside the memory dir — nothing outside it can be cited.
    """
    base = base_dir or config.memory_dir
    base_real = os.path.realpath(base)
    target = os.path.realpath(file_path)
    try:
        if os.path.commonpath([base_real, target]) != base_real:
            return []
    except ValueError:
        return []
    rel = os.path.relpath(target, base_real).replace(os.sep, "/")
    primary = _normalize_ref(rel)
    refs = [primary]
    stripped = re.sub(r"-status$", "", primary)
    if stripped != primary:
        refs.append(stripped)
    return refs


def find_dependents(source_refs: list[str], base_dir: str | None = None) -> list[str]:
    """Absolute paths of every live memory whose ``backed_by`` names a source ref.

    Sorted by path for determinism. Skips the source itself, ``-history.md``
    siblings, skip-dir files, unreadable frontmatter, and ``status: archived``
    memories (see module docstring).
    """
    base = base_dir or config.memory_dir
    wanted = {_normalize_ref(r) for r in source_refs}
    if not wanted:
        return []

    import frontmatter as _frontmatter

    out: list[str] = []
    for path in sorted(glob.glob(os.path.join(base, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        if rel.split("/")[0] in _SKIP_DIRS or rel.endswith("-history.md"):
            continue
        if _normalize_ref(rel) in wanted:
            continue  # a memory is not its own dependent
        try:
            meta = _frontmatter.load(path).metadata
        except Exception:  # noqa: BLE001 — soft-fail read, like lint
            continue
        if meta.get("status") == "archived":
            continue
        cites = {_normalize_ref(r) for r in parse_link_refs(meta, "backed_by")}
        if cites & wanted:
            out.append(path)
    return out


def parse_stale_backing(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Soft-fail accessor: the ``stale_backing`` entries of parsed frontmatter.

    Malformed entries (non-dict, or missing ``ref``) are dropped so a hand-edited
    field never breaks a read; a missing field is ``[]``.
    """
    raw = metadata.get(STALE_BACKING_FIELD)
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and str(e.get("ref") or "").strip()]


def merge_stale_backing_into_content(content: str, entry: dict[str, Any]) -> str:
    """Return ``content`` with ``entry`` appended to its ``stale_backing`` list.

    Idempotent on ``entry["ref"]``: when the dependent already carries an entry
    for that source the original ``content`` is returned unchanged, so callers
    can skip a no-op write/commit. Body preserved verbatim; frontmatter is
    re-dumped with key order kept (same shape as the typed-links merger).
    """
    import frontmatter as _frontmatter
    import yaml

    post = _frontmatter.loads(content)
    existing = parse_stale_backing(post.metadata)
    ref = _normalize_ref(entry["ref"])
    if any(_normalize_ref(e["ref"]) == ref for e in existing):
        return content

    meta = dict(post.metadata)
    meta[STALE_BACKING_FIELD] = [*existing, entry]
    dumped = yaml.safe_dump(
        meta, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{dumped}---\n\n{post.content}\n"


def build_entry(
    source_ref: str,
    *,
    ops: list[str],
    facts: list[str] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """The ``stale_backing`` entry written to a dependent (fixed key order).

    ``ops`` are members of :data:`RETIRING_OPS`, or :data:`RESTORE_CHECK_OP`
    on its own.
    """
    kinds = sorted({o.lower() for o in ops}, key=_OP_ORDER.index)
    entry: dict[str, Any] = {
        "ref": _normalize_ref(source_ref),
        "op": ", ".join(kinds),
        "at": _utc_now().isoformat(timespec="seconds"),
    }
    if facts:
        entry["facts"] = sorted(set(facts))
    if reason:
        entry["reason"] = reason
    return entry


def flag_dependents(
    source_path: str,
    *,
    ops: list[str],
    facts: list[str] | None = None,
    reason: str = "",
    commit: bool = True,
) -> list[str]:
    """Flag every live dependent of ``source_path`` for review; return their rel paths.

    ``ops`` names the retirement kind(s) that fired on the source (members of
    :data:`RETIRING_OPS`); ``facts`` the retired fact ids, ``reason`` the op's
    reason. Only dependents that were not already flagged for this source are
    written, re-indexed (frontmatter-only, no re-embed) and committed — in one
    commit naming the source, through the same choke points every other memory
    write uses. Best-effort per dependent: a failure on one is logged and the
    rest proceed; the originating retirement has already landed and must not
    be failed by its side effect.
    """
    base = config.memory_dir
    refs = source_refs_for(source_path, base)
    if not refs:
        logger.debug("backed_by propagation: %s is outside the memory dir; skipped", source_path)
        return []

    # The recorded ref is the memory's base identity (`-status` stripped): the
    # history writer treats a status layer and its base as one memory, so the
    # flag — and its idempotency key — must too.
    canonical = refs[-1]
    entry = build_entry(canonical, ops=ops, facts=facts, reason=reason)
    modified: list[str] = []
    for dep_path in find_dependents(refs, base):
        try:
            with open(dep_path, encoding="utf-8") as f:
                current = f.read()
            updated = merge_stale_backing_into_content(current, entry)
            if updated == current:
                continue  # already flagged for this source — idempotent no-op
            git_tools.write_memory_file(dep_path, updated)
            modified.append(dep_path)
        except Exception as exc:  # noqa: BLE001 — never fail the retirement
            logger.warning(
                "backed_by propagation: could not flag %s for %s: %s",
                dep_path, canonical, exc,
            )

    reindex_flagged(modified)

    if modified and commit:
        git_tools.commit_memory_files(
            modified,
            f"{config.git.commit_prefix} backed_by review: {canonical} "
            f"{entry['op']} -> {len(modified)} dependent(s)",
        )

    rels = [os.path.relpath(p, base).replace(os.sep, "/") for p in modified]
    if rels:
        logger.info(
            "backed_by propagation: %s %s flagged %d dependent(s): %s",
            canonical, entry["op"], len(rels), ", ".join(rels),
        )
    return rels


def reindex_flagged(paths: list[str]) -> None:
    """Re-index files whose only change is a new ``stale_backing`` entry.

    Frontmatter-only change: the metadata path re-indexes without
    re-embedding, so the flag is visible in search-result metadata, not just
    on disk. Best-effort — a failure is logged, never raised: the flag is on
    disk and reaches the index when the file is next indexed.
    """
    for path in paths:
        try:
            from palinode.indexer.index_file import index_file

            outcome = index_file(path)
            if outcome.get("error"):
                logger.warning(
                    "backed_by propagation: reindex reported %s for %s",
                    outcome["error"], path,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backed_by propagation: reindex failed for %s: %s — the flag is on "
                "disk but not in the index until the file is next indexed",
                path, exc,
            )


def _inactive_reason(ref: str, base: str) -> str | None:
    """Why the source ``ref`` names is no longer active, or ``None`` if it is.

    Resolves ``<ref>.md``, falling back to the ``<ref>-status.md`` layer that
    answers to the same base ref (see :func:`source_refs_for`). Signals, in
    order: the file is ``status: archived`` (every whole-file retirement —
    on-demand archive/supersede, forget, TTL — ends there), or no file
    answers to the ref at all. A ref that escapes the memory dir, or a file
    whose frontmatter cannot be read, is not a retirement signal and yields
    ``None`` — the same soft-fail as :func:`find_dependents`.
    """
    import frontmatter as _frontmatter

    for candidate in (f"{ref}.md", f"{ref}-status.md"):
        try:
            real = os.path.realpath(os.path.join(base, candidate))
            if os.path.commonpath([base, real]) != base:
                return None
            if not os.path.isfile(real):
                continue
            meta = _frontmatter.load(real).metadata
        except Exception:  # noqa: BLE001 — bad path or unreadable frontmatter, like lint
            return None
        if meta.get("status") != "archived":
            return None
        successor = meta.get("superseded_by")
        if successor:
            return f"{ref} is archived, superseded by {successor}"
        return f"{ref} is archived"
    return f"{ref} is missing"


def stale_backing_on_restore(
    metadata: dict[str, Any], base_dir: str | None = None
) -> list[dict[str, Any]]:
    """Entries a memory being restored must gain for sources retired meanwhile.

    The restore-time half of propagation. :func:`find_dependents` skips
    archived dependents, so a source retired while this memory was archived
    never flagged it; now that it is about to assert its claim again, each
    ``backed_by`` ref is checked against the source's *current* state and one
    :data:`RESTORE_CHECK_OP` entry is built per source that is no longer
    active (:func:`_inactive_reason`). Idempotent per source ref: a ref the
    memory already carries an entry for — whatever that entry's ``op`` — is
    not re-flagged, so the returned entries are exactly the new ones and a
    second archive/restore cycle adds nothing. Nothing is written here; the
    caller merges the entries into the content it is about to commit.
    """
    base = os.path.realpath(base_dir or config.memory_dir)
    already = {_normalize_ref(e["ref"]) for e in parse_stale_backing(metadata)}
    entries: list[dict[str, Any]] = []
    for raw in parse_link_refs(metadata, "backed_by"):
        ref = _normalize_ref(raw)
        if not ref or ref in already:
            continue
        already.add(ref)
        reason = _inactive_reason(ref, base)
        if reason is None:
            continue
        entries.append(build_entry(ref, ops=[RESTORE_CHECK_OP], reason=reason))
    return entries


__all__ = [
    "RESTORE_CHECK_OP",
    "RETIRING_OPS",
    "STALE_BACKING_FIELD",
    "build_entry",
    "find_dependents",
    "flag_dependents",
    "merge_stale_backing_into_content",
    "parse_stale_backing",
    "reindex_flagged",
    "source_refs_for",
    "stale_backing_on_restore",
]
