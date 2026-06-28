"""
Compaction Executor

Applies structured operations (KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE/RETRACT)
to markdown memory files. The LLM decides what to do; the executor
does it deterministically.

This separation ensures:
- LLMs never touch files directly
- Every change is a git commit with clear provenance
- Operations are auditable and reversible
"""
from __future__ import annotations

import os
import re
import logging
from datetime import UTC, datetime
from typing import Any

import yaml

from palinode.core import git_tools
from palinode.core.config import config
from palinode.consolidation.op_parse import op_kind

logger = logging.getLogger("palinode.consolidation.executor")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _is_replace_policy(content: str) -> bool:
    """Return True if the file's frontmatter declares ``update_policy: replace``.

    ADR-015 §2.2: a ``replace`` doc is a living/current-state document that
    consolidation must never SUPERSEDE/ARCHIVE-into-history. Parses the
    frontmatter via the shared markdown parser; any parse failure falls open to
    ``False`` (no protection) so a malformed file never blocks consolidation.

    A WARNING is emitted on the fail-open path when the raw text contains
    ``update_policy: replace`` but the parsed metadata does not — this
    indicates frontmatter corruption that silently removed the protection.
    The fail-open behaviour is intentional and preserved (#483).
    """
    try:
        from palinode.core.parser import parse_markdown

        metadata, _ = parse_markdown(content)
    except Exception as exc:  # noqa: BLE001 — defensive: never let the guard raise
        # parse_markdown itself swallows exceptions internally, so this branch
        # is a last-resort safety net. Log with the hint regardless.
        logger.warning(
            "replace-guard: unexpected error parsing frontmatter — "
            "falling open to no-protection (doc may be unprotected): %s",
            exc,
        )
        return False

    protected = metadata.get("update_policy") == "replace"

    # Cheap post-parse corruption check (#483): the parser returns {} on
    # garbled frontmatter — if the raw text contains the policy declaration
    # but the parsed metadata does not, the frontmatter silently failed and
    # the protection is lost. Warn so operators can detect this without
    # blocking consolidation.
    if not protected and "update_policy: replace" in content:
        logger.warning(
            "replace-guard: raw text contains 'update_policy: replace' but "
            "parsed metadata does not — frontmatter may be corrupt; "
            "falling open to no-protection (doc may be unprotected)",
        )

    return protected


def _normalize_fact_text(text: str) -> str:
    """Normalize LLM-proposed fact text to list-item content only."""
    normalized = text.strip()
    normalized = re.sub(r"^[-*]\s+", "", normalized)
    return normalized


def _extract_fact_date(content: str, fact_id: str) -> str | None:
    """Extract the date prefix from a fact's text, if present.

    Looks for a ``[YYYY-MM-DD]`` date tag at the start of the fact text.
    Returns the ``YYYY-MM-DD`` string, or ``None`` if no date is found.
    """
    pattern = re.compile(
        r'^[\s]*[-*]\s+\[(\d{4}-\d{2}-\d{2})\].*?<!-- fact:' + re.escape(fact_id) + r' -->',
        re.MULTILINE,
    )
    m = pattern.search(content)
    return m.group(1) if m else None


def _nightly_merge_allowed(content: str, ids: list[str]) -> bool:
    """Return True iff a nightly MERGE is permitted for the given fact IDs.

    Nightly policy: all facts in the merge must share the same calendar date
    (``[YYYY-MM-DD]`` prefix in their text).  Cross-date merges are deferred to
    the weekly pass.  Facts without a recognisable date prefix are rejected to
    avoid silent data loss.
    """
    if not ids:
        return False
    dates = [_extract_fact_date(content, fid) for fid in ids]
    # Any fact without a parseable date → reject
    if any(d is None for d in dates):
        return False
    # All dates must be the same calendar day
    return len(set(dates)) == 1


def _atomic_write_text(file_path: str, content: str) -> None:
    """Write text atomically via the git_tools mutation choke point.

    All memory-file writes route through :func:`git_tools.write_memory_file`
    (the single atomic write primitive) so a future signer can observe content
    at mutation time in one place. Retained as a thin local alias because the
    executor calls it from two sites (op write-back + history append).
    """
    git_tools.write_memory_file(file_path, content)


def apply_operations(file_path: str, operations: list[dict], *, nightly_policy: bool = False) -> dict:
    """Apply a list of operations to a memory file.

    Args:
        file_path: Path to the target markdown file.
        operations: List of operation dicts with 'op' key.
        nightly_policy: When True, MERGE ops are subject to the same-day guard:
            only facts sharing the same ``[YYYY-MM-DD]`` date prefix may be
            merged.  Cross-date or undated MERGE proposals are rejected with a
            log warning and counted as ``merge_rejected``.

    Returns:
        Stats dict: {kept, updated, merged, superseded, archived, retracted,
                     merge_rejected, protected_rejected, contradicts_proposed}.
    """
    with open(file_path) as f:
        content = f.read()

    # ADR-015 §2.2 / #431 §3: a memory declaring `update_policy: replace` is a
    # living/current-state document. Consolidation may UPDATE it in place but
    # must NEVER SUPERSEDE it (strikethrough + spawn a "supersedes-" sibling)
    # or ARCHIVE-into-history it — either would fork the single current fact
    # into a stale historical snapshot, the exact failure mode the axis exists
    # to prevent. Read the file's own declared regime once and guard the
    # history-forking ops. Parse defensively: an unreadable/garbled frontmatter
    # falls open to today's behaviour (no protection) rather than blocking
    # consolidation entirely.
    is_replace_doc = _is_replace_policy(content)

    stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0, "retracted": 0, "merge_rejected": 0, "protected_rejected": 0, "contradicts_proposed": 0}

    for op in operations:
        if not isinstance(op, dict):
            logger.warning(f"Malformed operation (expected dict, got {type(op).__name__}): {op}")
            continue

        op_type = op_kind(op) or "KEEP"

        # ADR-015 §2.2: refuse history-forking ops on a living (replace) doc.
        # UPDATE/MERGE/KEEP keep the one current fact current. SUPERSEDE and
        # ARCHIVE move content into history. RETRACT is ALSO history-forking on
        # a living doc (H3): _retract_fact strikethrough-tombstones the current
        # fact in place AND appends a `-history.md` sibling — exactly the stale-
        # snapshot fork this axis forbids. A provably-wrong value in a living
        # document must be corrected with UPDATE, not tombstoned; guard RETRACT.
        if is_replace_doc and op_type in ("SUPERSEDE", "ARCHIVE", "RETRACT"):
            logger.warning(
                "%s rejected by update_policy=replace guard (living document): "
                "%s on %s",
                op_type,
                op.get("id"),
                file_path,
            )
            stats["protected_rejected"] += 1
            continue

        if op_type == "KEEP":
            stats["kept"] += 1
            continue

        elif op_type == "UPDATE":
            fact_id = op.get("id")
            new_text = op.get("new_text", "")
            if fact_id and new_text:
                updated_content = _update_fact(content, fact_id, new_text)
                if updated_content != content:
                    content = updated_content
                    stats["updated"] += 1
        
        elif op_type == "MERGE":
            ids = op.get("ids", [])
            new_text = op.get("new_text", "")
            if ids and new_text:
                if nightly_policy and not _nightly_merge_allowed(content, ids):
                    id_list = ", ".join(ids)
                    logger.warning(
                        f"MERGE rejected by nightly policy: cross-date or undated facts "
                        f"({id_list}) in {file_path}"
                    )
                    stats["merge_rejected"] += 1
                    continue
                merged_content = _merge_facts(content, ids, new_text)
                if merged_content != content:
                    content = merged_content
                    stats["merged"] += 1
        
        elif op_type == "SUPERSEDE":
            fact_id = op.get("id")
            new_text = op.get("new_text", "")
            reason = op.get("reason", "")
            if fact_id and new_text:
                superseded_content = _supersede_fact(content, fact_id, new_text, reason, file_path)
                if superseded_content != content:
                    content = superseded_content
                    stats["superseded"] += 1
        
        elif op_type == "ARCHIVE":
            fact_id = op.get("id")
            reason = op.get("rationale", op.get("reason", ""))
            if fact_id:
                archived_content = _archive_fact(content, fact_id, reason, file_path)
                if archived_content != content:
                    content = archived_content
                    stats["archived"] += 1

        elif op_type == "RETRACT":
            fact_id = op.get("id")
            reason = op.get("reason", op.get("rationale", ""))
            if fact_id:
                retracted_content = _retract_fact(content, fact_id, reason, file_path)
                if retracted_content != content:
                    content = retracted_content
                    stats["retracted"] += 1

        elif op_type == "PROPOSE_CONTRADICTS":
            # #533 (G4): the executor may PROPOSE a typed contradiction link but
            # must NEVER auto-resolve a conflict. SUPERSEDE stays the only
            # winner-picking op. This op is non-destructive: it records the
            # `contradicts` link in frontmatter (idempotently) and picks no
            # winner. It is intentionally NOT subject to the replace-guard above
            # — recording a conflict forks nothing into history.
            refs = op.get("contradicts", op.get("refs", op.get("ids")))
            try:
                from palinode.core.typed_links import (
                    TypedLinkError,
                    merge_link_refs_into_content,
                    normalize_link_refs,
                )
                norm = normalize_link_refs(refs, "contradicts")
            except TypedLinkError as exc:
                logger.warning(
                    "PROPOSE_CONTRADICTS rejected (malformed refs) on %s: %s",
                    file_path, exc,
                )
                norm = []
            if norm:
                proposed_content = merge_link_refs_into_content(
                    content, "contradicts", norm
                )
                if proposed_content != content:
                    content = proposed_content
                    stats["contradicts_proposed"] += 1

    # Write back
    _atomic_write_text(file_path, content)
    
    return stats


def _update_fact(content: str, fact_id: str, new_text: str) -> str:
    """Replace a fact's text while preserving its ID."""
    pattern = re.compile(
        r'^([\s]*[-*]\s+).*?(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )
    replacement = rf'\1{_normalize_fact_text(new_text)} <!-- fact:{fact_id} -->'
    return pattern.sub(replacement, content, count=1)


def _merge_facts(content: str, ids: list[str], new_text: str) -> str:
    """Remove all source facts and insert merged fact at first occurrence."""
    first_id = ids[0]
    merged_id = f"merged-{ids[0]}"
    
    # Replace first with merged text
    updated_content = _update_fact(content, first_id, new_text)
    if updated_content == content:
        return content
    content = updated_content
    # Update the fact ID to the merged ID
    content = re.sub(
        r"<!-- fact:" + re.escape(first_id) + r" -->",
        f"<!-- fact:{merged_id} -->",
        content,
        count=1,
    )
    
    # Remove remaining source facts
    for fid in ids[1:]:
        pattern = re.compile(
            r'^[\s]*[-*]\s+.*?<!-- fact:' + re.escape(fid) + r' -->\n?',
            re.MULTILINE
        )
        content = pattern.sub('', content)
    
    return content


def _supersede_fact(content: str, fact_id: str, new_text: str,
                    reason: str, file_path: str) -> str:
    """Mark a fact as superseded and add the new version."""
    now = _utc_now().strftime("%Y-%m-%d")
    new_id = f"supersedes-{fact_id}"
    
    # Strikethrough the old fact and add superseded marker
    pattern = re.compile(
        r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )
    
    def replacer(m):
        old_text = m.group(2).strip()
        return (f"{m.group(1)}~~{old_text}~~ [superseded {now}] {m.group(3)}\n"
                f"{m.group(1)}{_normalize_fact_text(new_text)} <!-- fact:{new_id} -->")
    
    updated_content, substitutions = pattern.subn(replacer, content, count=1)
    if substitutions == 0:
        return content
    
    # Also append to history file
    _append_to_history(file_path, fact_id, f"Superseded ({now}): {reason}")
    
    return updated_content


def _archive_fact(content: str, fact_id: str, reason: str, file_path: str) -> str:
    """Remove a fact from the file and append it to the history file."""
    # Extract the fact text before removing
    pattern = re.compile(
        r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fact_id) + r' -->)\n?',
        re.MULTILINE
    )
    match = pattern.search(content)
    if match:
        archived_text = match.group(2).strip()
        _append_to_history(file_path, fact_id, 
                          f"Archived: {archived_text} (reason: {reason})")
    
    # Remove from main file
    content = pattern.sub('', content)
    return content


def _retract_fact(content: str, fact_id: str, reason: str, file_path: str) -> str:
    """Mark a fact as retracted — explicitly wrong, not just stale.

    Unlike ARCHIVE (removes silently), RETRACT leaves a visible tombstone
    with strikethrough and reason so readers know the fact was wrong and why.
    Aligns with IETF Knowledge Unit lifecycle (retract = known-incorrect).
    """
    now = _utc_now().strftime("%Y-%m-%d")
    reason_text = f" — {reason}" if reason else ""

    pattern = re.compile(
        r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )

    def replacer(m):
        old_text = m.group(2).strip()
        return f"{m.group(1)}~~{old_text}~~ [RETRACTED {now}{reason_text}] {m.group(3)}"

    updated_content, substitutions = pattern.subn(replacer, content, count=1)
    if substitutions == 0:
        return content

    _append_to_history(file_path, fact_id, f"Retracted ({now}): {reason}")

    return updated_content


def _ensure_archived_frontmatter(content: str) -> str:
    """Ensure a history file's frontmatter carries ``status: archived`` (#485).

    History files hold ARCHIVE'd / SUPERSEDE'd facts. They must be excluded
    from default recall (``config.search.exclude_status = ["archived"]``) while
    staying indexed and retrievable on demand. The status lives in file-level
    frontmatter, which the indexer propagates to every chunk's metadata.

    Legacy history files created before this fix lack the field; inject it on
    the next append rather than leaving them leaking into recall.
    """
    fm_match = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
    if not fm_match:
        # No frontmatter at all — prepend a complete archived block.
        return "---\ncategory: history\ncore: false\nstatus: archived\n---\n\n" + content
    fm_body = fm_match.group(1)
    if re.search(r'^status:', fm_body, re.MULTILINE):
        return content  # already carries an explicit status; respect it
    new_fm_body = fm_body + "\nstatus: archived"
    return content[:fm_match.start(1)] + new_fm_body + content[fm_match.end(1):]


def _append_to_history(file_path: str, fact_id: str, text: str) -> None:
    """Append an entry to the corresponding history file.

    The history file carries ``status: archived`` frontmatter so its content
    (archived + superseded facts) is suppressed from default recall while
    remaining indexed and retrievable on demand — preserving the audit trail
    PROGRAM.md's "never hard-delete" contract requires (#485).
    """
    base = re.sub(r'-status\.md$', '', file_path)
    base = re.sub(r'\.md$', '', base)
    history_path = f"{base}-history.md"

    now = _utc_now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{now}] {text} <!-- fact:{fact_id} -->\n"

    if os.path.exists(history_path):
        with open(history_path) as f:
            history_content = f.read()
        history_content = _ensure_archived_frontmatter(history_content)
    else:
        history_content = "---\ncategory: history\ncore: false\nstatus: archived\n---\n\n# History\n\n"

    _atomic_write_text(history_path, history_content + entry)
