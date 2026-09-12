"""On-demand ARCHIVE / SUPERSEDE for one named memory.

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

RESTORE IS THE INVERSE, AND ONLY THE INVERSE. :func:`restore_memory` undoes
what archival did — ``status`` back to ``active``, ``superseded_by``
cleared — and nothing else: the archived file *is* the record, so the
frontmatter is reconstructed from it rather than by hand (``created_at``,
entities, epistemic marker and every other field survive untouched). A
retraction marker inside the body, its ``retracted_prefs`` record, and any
trigger that points at the file are separate lifecycle state with their own
surfaces and are deliberately not resurrected by a restore. The
resurrection is made visible, never silent: ``restored_at`` and
``restored_from`` land in frontmatter, the history sibling gains a line,
and the write is one commit. ``restored_at`` is also what keeps a restored
memory out of a re-triggered forget request's reach (see
:mod:`palinode.consolidation.forget`).

The one thing a restore does beyond undoing is re-check the memory's own
backing. ``backed_by`` propagation (:mod:`palinode.consolidation.propagate`)
skips archived dependents, so a source retired while this memory was
archived left no ``stale_backing`` flag on it; restoring it unchecked would
put a claim back into recall whose support was withdrawn, with no flag, no
lint finding and no review-queue entry. So the restore runs the same one-hop
check against the sources' current state and flags each one no longer active
under ``op: restore-check`` — idempotent per source ref, in the restore's own
commit, reported as ``stale_backing`` in the result.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import frontmatter

from palinode.core import git_tools, path_guard, store
from palinode.core.config import config

logger = logging.getLogger("palinode.archive")

ARCHIVED_STATUS = "archived"
ACTIVE_STATUS = "active"


def resolve_memory_ref(ref: str) -> tuple[str, str]:
    """Validate a caller-supplied memory ref; return ``(rel_path, abs_path)``.

    Rejects null bytes, absolute paths, ``..`` traversal, and symlinks that
    resolve outside ``config.memory_dir``, via the shared
    :func:`palinode.core.path_guard.resolve_memory_path` guard — this used to
    go through ``git_tools._resolve_memory_path``'s weaker, now-retired
    ``os.path.realpath``-based check. Raises
    :class:`~palinode.core.path_guard.PathTraversalError` (a ``ValueError``
    subclass) on rejection.

    ``rel_path`` is the canonical memory-dir-relative spelling and ``abs_path``
    is ``memory_dir`` joined with it — the *un*-realpath'd form the indexer
    stores in ``chunks.file_path``, so :func:`store.set_status_for_path` matches.
    """
    base, resolved = path_guard.resolve_memory_path(ref)
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
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Retire one named memory: ARCHIVE, or SUPERSEDE when ``superseded_by`` is set.

    Flips the file to ``status: archived`` (recording ``superseded_by`` when a
    replacement is named), appends the reason to the ``{base}-history.md`` audit
    sibling via the executor's history writer, pushes the status into the chunk
    index so ``exclude_status`` suppresses it from default recall, and commits
    the file plus its history sibling as one mutation.

    Idempotent: a memory already at ``status: archived`` is reported as
    ``already_archived`` and nothing is written or committed.

    ``actor`` names a non-human proposer whose finding is why this retirement is
    happening — today the deterministic lint→op mapping. It is recorded in both
    durable places, the history line and the commit subject, so a reader can
    tell an operator's on-demand archive from one a proposer earned. Omitted (a
    direct CLI/API/MCP call), nothing changes.

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
    if actor:
        entry = f"{entry} [actor: {actor}]"
    history_abs = append_to_history(abs_path, _audit_id(post.metadata, rel), entry)
    history_rel = os.path.relpath(history_abs, config.memory_dir)

    chunks_updated = store.set_status_for_path(abs_path, ARCHIVED_STATUS)

    verb = "supersede" if superseded_by else "archive"
    message = f"{config.git.commit_prefix} {verb}: {rel}"
    if superseded_by:
        message = f"{message} -> {superseded_by}"
    if actor:
        message = f"{message} (actor: {actor})"
    # One mutation = one commit, staging exactly the two files it touched.
    committed = git_tools.commit_memory_files([abs_path, history_abs], message)

    # The retirement has landed; now the memories whose `backed_by` cites this
    # one are flagged for review (one hop, flag-only, its own commit). After the
    # commit above so the archive is durable whatever propagation does.
    from palinode.consolidation.propagate import flag_dependents

    review_flagged = flag_dependents(
        abs_path,
        ops=["supersede" if superseded_by else "archive"],
        reason=entry,
    )

    logger.info("Archived %s (superseded_by=%s)", rel, superseded_by)
    return {
        "file": rel,
        "status": ARCHIVED_STATUS,
        "superseded_by": superseded_by,
        "reason": reason,
        "history_file": history_rel,
        "chunks_updated": chunks_updated,
        "committed": committed,
        "review_flagged": review_flagged,
    }


def restore_memory(file_path: str, reason: str | None = None) -> dict[str, Any]:
    """Bring one archived memory back into default recall.

    The inverse of :func:`archive_memory` for every archive path (on-demand,
    forget-driven, TTL, consolidation): flips ``status`` back to ``active``
    (the vocabulary's live value and the parser's default for a memory with
    no status), drops ``superseded_by``, records ``restored_at`` and
    ``restored_from`` (the successor the memory had been superseded by, or
    ``"archived"`` for a plain archive), appends a history line, pushes the
    status into the chunk index, and commits the memory plus its history
    sibling as one mutation. Every other frontmatter field — ``created_at``
    above all — is carried over from the archived file itself.

    Not resurrected on purpose: retraction markers and ``retracted_prefs``
    (see :func:`palinode.consolidation.retract.unretract_mentions`) and any
    trigger bound to the file. A still-past ``expires_at`` is left in place
    and surfaced in the result as ``expires_at`` so the caller knows the TTL
    sweep will retire the memory again unless the expiry is changed.

    Backing is re-checked on the way back: every ``backed_by`` source that is
    no longer active (archived, superseded, or gone) gains a ``stale_backing``
    entry with ``op: restore-check`` in this same write and commit, one per
    source ref the memory does not already carry an entry for
    (:func:`palinode.consolidation.propagate.stale_backing_on_restore`). The
    flagged refs are reported as ``stale_backing`` and named in the history
    line; the memory is restored either way — flagged, never held back.

    Idempotent: a memory that is not archived is reported as
    ``not_archived`` and nothing is written or committed.

    Raises:
        ValueError: the path is malformed or escapes ``memory_dir``.
        FileNotFoundError: no such memory file.
    """
    rel, abs_path = resolve_memory_ref(file_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(rel)

    with open(abs_path, encoding="utf-8") as f:
        post = frontmatter.load(f)

    if post.get("status") != ARCHIVED_STATUS:
        return {
            "file": rel,
            "status": "not_archived",
            "restored_from": None,
            "reason": reason,
            "history_file": None,
            "chunks_updated": 0,
            "committed": False,
        }

    from palinode.consolidation.executor import _utc_now, append_to_history

    successor = post.metadata.get("superseded_by")
    restored_from = str(successor) if successor else ARCHIVED_STATUS
    restored_at = _utc_now().isoformat()

    post["status"] = ACTIVE_STATUS
    post.metadata.pop("superseded_by", None)
    post["restored_from"] = restored_from
    post["restored_at"] = restored_at
    content = frontmatter.dumps(post) + "\n"

    # Propagation skips archived dependents, so a source retired while this
    # memory was archived left no flag on it. Re-check its own backing now
    # that it is about to assert its claim again — one hop, flag-only,
    # idempotent per source ref, in this restore's own write and commit.
    from palinode.consolidation.propagate import (
        merge_stale_backing_into_content,
        reindex_flagged,
        stale_backing_on_restore,
    )

    stale_entries = stale_backing_on_restore(post.metadata)
    for stale_entry in stale_entries:
        content = merge_stale_backing_into_content(content, stale_entry)
    stale_refs = [e["ref"] for e in stale_entries]
    git_tools.write_memory_file(abs_path, content)

    entry = f"Restored: {rel} (was {restored_from})"
    if successor:
        entry = f"Restored: {rel} (was superseded by {successor})"
    if reason:
        entry = f"{entry} (reason: {reason})"
    if stale_refs:
        entry = f"{entry}; stale backing: {', '.join(stale_refs)}"
    history_abs = append_to_history(abs_path, _audit_id(post.metadata, rel), entry)
    history_rel = os.path.relpath(history_abs, config.memory_dir)

    chunks_updated = store.set_status_for_path(abs_path, ACTIVE_STATUS)
    if stale_refs:
        # The status push above is metadata-only and does not carry the new
        # frontmatter field; the propagation path's re-index does.
        reindex_flagged([abs_path])

    message = f"{config.git.commit_prefix} restore: {rel}"
    if successor:
        message = f"{message} <- {successor}"
    committed = git_tools.commit_memory_files([abs_path, history_abs], message)

    logger.info("Restored %s (restored_from=%s)", rel, restored_from)
    result: dict[str, Any] = {
        "file": rel,
        "status": ACTIVE_STATUS,
        "restored_from": restored_from,
        "restored_at": restored_at,
        "reason": reason,
        "history_file": history_rel,
        "chunks_updated": chunks_updated,
        "committed": committed,
        "stale_backing": stale_refs,
    }
    expires_at = post.metadata.get("expires_at")
    if expires_at:
        # Surfaced, not cleared: the expiry is the memory's own declaration.
        # Without this the next TTL sweep would silently undo the restore.
        result["expires_at"] = str(expires_at)
    return result


__all__ = [
    "ACTIVE_STATUS",
    "ARCHIVED_STATUS",
    "archive_memory",
    "resolve_memory_ref",
    "restore_memory",
    "set_archived_frontmatter",
]
