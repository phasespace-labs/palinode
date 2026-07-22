"""On-demand ARCHIVE / SUPERSEDE for one named memory (#664).

``PROGRAM.md`` §"Never hard-delete" already specifies how a memory is retired —
``status: archived`` frontmatter, the ``{base}-history.md`` audit sibling, and
``config.search.exclude_status`` suppressing the archived content from default
recall while it stays indexed and retrievable on demand. The mechanism was only
reachable two ways, both bulk and neither addressable: the TTL sweep
(:mod:`palinode.consolidation.ttl`) and an LLM-proposed ARCHIVE op inside a
consolidation pass. An agent that knew *this* memory was wrong had no sanctioned
move, so it hand-wrote a tombstone body via ``save(update_policy="replace")`` —
which never sets ``status``, leaving the superseded content live in recall.

This module is the missing entry point, not new archival semantics:

- the frontmatter flip is the primitive the TTL sweep already used
  (:func:`set_archived_frontmatter`, which ``ttl._archive_file`` now calls);
- the audit trail is the executor's own history writer
  (:func:`palinode.consolidation.executor.append_to_history`), so the sibling
  file and its ``status: archived`` frontmatter are byte-identical to what a
  consolidation ARCHIVE/SUPERSEDE produces, and ``palinode trace`` reads the
  result as the file's supersession trail with no change;
- the index propagation is :func:`palinode.core.store.set_status_for_path`, the
  no-re-embed status push the TTL sweep needs for the same reason (a
  frontmatter-only edit does not move the body content-hash ``index_file`` keys
  its fast path on).

ARCHIVE and SUPERSEDE are one verb with one optional argument: passing
``superseded_by`` names the replacement and records it in frontmatter; omitting
it retires the memory with no successor. Both end at ``status: archived``.
``status: superseded`` is deliberately *not* used — it is absent from
``config.search.exclude_status``, so it would leave the retired memory in
default recall, which is the exact bug this closes.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import frontmatter

from palinode.core import git_tools, store
from palinode.core.config import config

logger = logging.getLogger("palinode.archive")

ARCHIVED_STATUS = "archived"


def resolve_memory_ref(ref: str) -> tuple[str, str]:
    """Validate a caller-supplied memory ref; return ``(rel_path, abs_path)``.

    Rejects null bytes, ``..`` traversal, and symlinks that resolve outside
    ``config.memory_dir`` (``git_tools._resolve_memory_path`` realpath-resolves
    before the containment check, so a symlink pointing out of the tree fails
    here). Raises :class:`ValueError` on rejection.

    ``rel_path`` is the canonical memory-dir-relative spelling and ``abs_path``
    is ``memory_dir`` joined with it — the *un*-realpath'd form the indexer
    stores in ``chunks.file_path``, so :func:`store.set_status_for_path` matches.
    """
    git_tools._resolve_memory_path(ref)  # raises ValueError on traversal
    base = os.path.realpath(config.memory_dir)
    resolved = os.path.realpath(os.path.join(base, ref))
    rel = os.path.relpath(resolved, base)
    return rel, os.path.join(config.memory_dir, rel)


def set_archived_frontmatter(path: str, superseded_by: str | None = None) -> None:
    """Set ``status: archived`` in a file's frontmatter, preserving the body.

    The single frontmatter-flip primitive, shared by the TTL sweep and the
    on-demand op so there is one implementation of "retire this file". When
    ``superseded_by`` is given it is recorded alongside the status, per the
    ``PROGRAM.md`` decision-frontmatter schema.
    """
    with open(path, encoding="utf-8") as f:
        post = frontmatter.load(f)
    post["status"] = ARCHIVED_STATUS
    if superseded_by:
        post["superseded_by"] = superseded_by
    git_tools.write_memory_file(path, frontmatter.dumps(post) + "\n")


def _audit_id(metadata: dict[str, Any], rel_path: str) -> str:
    """The identifier the history entry is tagged with.

    Prefers the memory's own ``id`` frontmatter (``PROGRAM.md`` schema), falling
    back to its slug, so the ``<!-- fact:… -->`` marker in the history sibling
    names something a reader can resolve.
    """
    declared = str(metadata.get("id") or "").strip()
    if declared:
        return declared
    return os.path.splitext(os.path.basename(rel_path))[0]


def archive_memory(
    file_path: str,
    reason: str | None = None,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    """Retire one named memory: ARCHIVE, or SUPERSEDE when ``superseded_by`` is set.

    Flips the file to ``status: archived`` (recording ``superseded_by`` when a
    replacement is named), appends the reason to the ``{base}-history.md`` audit
    sibling via the executor's history writer, pushes the status into the chunk
    index so ``exclude_status`` suppresses it from default recall, and commits
    the file plus its history sibling as one mutation.

    Idempotent: a memory already at ``status: archived`` is reported as
    ``already_archived`` and nothing is written or committed.

    Raises:
        ValueError: the path is malformed or escapes ``memory_dir``.
        FileNotFoundError: no such memory file.
    """
    rel, abs_path = resolve_memory_ref(file_path)
    if superseded_by:
        # A successor ref is a user-supplied path too; hold it to the same guard.
        # A bare slug resolves inside memory_dir and passes; `../…` does not.
        resolve_memory_ref(superseded_by)

    if not os.path.isfile(abs_path):
        raise FileNotFoundError(rel)

    with open(abs_path, encoding="utf-8") as f:
        post = frontmatter.load(f)

    if post.get("status") == ARCHIVED_STATUS:
        return {
            "file": rel,
            "status": "already_archived",
            "superseded_by": post.get("superseded_by"),
            "reason": reason,
            "history_file": None,
            "chunks_updated": 0,
            "committed": False,
        }

    # ADR-015 §2.2's replace-guard deliberately does NOT apply here. That guard
    # stops *consolidation* from SUPERSEDE/ARCHIVE-ing a living (`update_policy:
    # replace`) document, because the executor's fact-level ops fork the one
    # current fact into a stale historical snapshot. This op forks nothing — it
    # retires the whole file in place. Applying the guard would also make the
    # feature unable to fix its own motivating case: the memories that needed
    # retiring had been hand-tombstoned with `save(update_policy="replace")`,
    # so they are precisely the `replace` docs the guard would refuse.

    set_archived_frontmatter(abs_path, superseded_by)

    from palinode.consolidation.executor import append_to_history

    if superseded_by:
        entry = f"Superseded by {superseded_by}"
    else:
        entry = f"Archived: {rel}"
    if reason:
        entry = f"{entry} (reason: {reason})"
    history_abs = append_to_history(abs_path, _audit_id(post.metadata, rel), entry)
    history_rel = os.path.relpath(history_abs, config.memory_dir)

    chunks_updated = store.set_status_for_path(abs_path, ARCHIVED_STATUS)

    verb = "supersede" if superseded_by else "archive"
    message = f"{config.git.commit_prefix} {verb}: {rel}"
    if superseded_by:
        message = f"{message} -> {superseded_by}"
    # One mutation = one commit, staging exactly the two files it touched.
    committed = git_tools.commit_memory_files([abs_path, history_abs], message)

    logger.info("Archived %s (superseded_by=%s)", rel, superseded_by)
    return {
        "file": rel,
        "status": ARCHIVED_STATUS,
        "superseded_by": superseded_by,
        "reason": reason,
        "history_file": history_rel,
        "chunks_updated": chunks_updated,
        "committed": committed,
    }


__all__ = [
    "ARCHIVED_STATUS",
    "archive_memory",
    "resolve_memory_ref",
    "set_archived_frontmatter",
]
