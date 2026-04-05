"""
Compaction Executor

Applies structured operations (KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE)
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
from datetime import datetime
from typing import Any

import yaml

from palinode.core.config import config

logger = logging.getLogger("palinode.consolidation.executor")


def apply_operations(file_path: str, operations: list[dict]) -> dict:
    """Apply a list of operations to a memory file.
    
    Args:
        file_path: Path to the target markdown file.
        operations: List of operation dicts with 'op' key.
        
    Returns:
        Stats dict: {kept, updated, merged, superseded, archived}.
    """
    with open(file_path) as f:
        content = f.read()
    
    stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    
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
                content = _update_fact(content, fact_id, new_text)
                stats["updated"] += 1
        
        elif op_type == "MERGE":
            ids = op.get("ids", [])
            new_text = op.get("new_text", "")
            if ids and new_text:
                content = _merge_facts(content, ids, new_text)
                stats["merged"] += 1
        
        elif op_type == "SUPERSEDE":
            fact_id = op.get("id")
            new_text = op.get("new_text", "")
            reason = op.get("reason", "")
            if fact_id and new_text:
                content = _supersede_fact(content, fact_id, new_text, reason, file_path)
                stats["superseded"] += 1
        
        elif op_type == "ARCHIVE":
            fact_id = op.get("id")
            reason = op.get("rationale", op.get("reason", ""))
            if fact_id:
                content = _archive_fact(content, fact_id, reason, file_path)
                stats["archived"] += 1
    
    # Write back
    with open(file_path, 'w') as f:
        f.write(content)
    
    return stats


def _update_fact(content: str, fact_id: str, new_text: str) -> str:
    """Replace a fact's text while preserving its ID."""
    pattern = re.compile(
        r'^([\s]*[-*]\s+).*?(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )
    replacement = rf'\1{new_text} <!-- fact:{fact_id} -->'
    return pattern.sub(replacement, content, count=1)


def _merge_facts(content: str, ids: list[str], new_text: str) -> str:
    """Remove all source facts and insert merged fact at first occurrence."""
    first_id = ids[0]
    merged_id = f"merged-{ids[0]}"
    
    # Replace first with merged text
    content = _update_fact(content, first_id, new_text)
    # Update the fact ID to the merged ID
    content = content.replace(f"<!-- fact:{first_id} -->", f"<!-- fact:{merged_id} -->")
    
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
    now = datetime.utcnow().strftime("%Y-%m-%d")
    new_id = f"supersedes-{fact_id}"
    
    # Strikethrough the old fact and add superseded marker
    pattern = re.compile(
        r'^([\s]*[-*]\s+)(.*?)(<!-- fact:' + re.escape(fact_id) + r' -->)',
        re.MULTILINE
    )
    
    def replacer(m):
        old_text = m.group(2).strip()
        return (f"{m.group(1)}~~{old_text}~~ [superseded {now}] {m.group(3)}\n"
                f"{m.group(1)}{new_text} <!-- fact:{new_id} -->")
    
    content = pattern.sub(replacer, content, count=1)
    
    # Also append to history file
    _append_to_history(file_path, fact_id, f"Superseded ({now}): {reason}")
    
    return content


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


def _append_to_history(file_path: str, fact_id: str, text: str) -> None:
    """Append an entry to the corresponding history file."""
    base = re.sub(r'-status\.md$', '', file_path)
    base = re.sub(r'\.md$', '', base)
    history_path = f"{base}-history.md"
    
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    entry = f"- [{now}] {text} <!-- fact:{fact_id} -->\n"
    
    if os.path.exists(history_path):
        with open(history_path, 'a') as f:
            f.write(entry)
    else:
        # Create history file
        with open(history_path, 'w') as f:
            f.write(f"---\ncategory: history\ncore: false\n---\n\n# History\n\n{entry}")
