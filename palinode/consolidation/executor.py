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
import tempfile
from datetime import UTC, datetime
from typing import Any

import yaml

from palinode.core.config import config

logger = logging.getLogger("palinode.consolidation.executor")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


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


def _fsync_directory(path: str) -> None:
    """Flush directory metadata so the rename survives a crash."""
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _atomic_write_text(file_path: str, content: str) -> None:
    """Write text via temp file + fsync + replace in the target directory."""
    directory = os.path.dirname(file_path) or "."
    prefix = f".{os.path.basename(file_path)}."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        if os.path.exists(file_path):
            os.fchmod(fd, os.stat(file_path).st_mode & 0o777)

        with os.fdopen(fd, "w") as tmp_file:
            fd = -1
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        os.replace(tmp_path, file_path)
        _fsync_directory(directory)
    except Exception:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


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
                     merge_rejected}.
    """
    with open(file_path) as f:
        content = f.read()

    stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0, "retracted": 0, "merge_rejected": 0}

    for op in operations:
        if not isinstance(op, dict):
            logger.warning(f"Malformed operation (expected dict, got {type(op).__name__}): {op}")
            continue

        op_type = op.get("op", "KEEP").upper()

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


def _append_to_history(file_path: str, fact_id: str, text: str) -> None:
    """Append an entry to the corresponding history file."""
    base = re.sub(r'-status\.md$', '', file_path)
    base = re.sub(r'\.md$', '', base)
    history_path = f"{base}-history.md"
    
    now = _utc_now().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{now}] {text} <!-- fact:{fact_id} -->\n"
    
    if os.path.exists(history_path):
        with open(history_path) as f:
            history_content = f.read()
    else:
        history_content = "---\ncategory: history\ncore: false\n---\n\n# History\n\n"

    _atomic_write_text(history_path, history_content + entry)
