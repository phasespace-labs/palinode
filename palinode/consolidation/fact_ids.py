"""
Fact ID Generator

Adds inline fact IDs (<!-- fact:slug -->) to list items in memory files.
Run once to bootstrap IDs for existing files, then the consolidation
executor maintains them going forward.
"""
from __future__ import annotations

import os
import re
import hashlib
from palinode.core.config import config
from palinode.core import parser


def generate_fact_id(file_path: str, line_text: str) -> str:
    """Generate a deterministic fact ID from file path + content.
    
    Format: {category}-{file_slug}-{content_hash[:6]}
    Example: mm-kmd-arch-a3f2b1
    """
    file_slug = os.path.splitext(os.path.basename(file_path))[0]
    content_hash = hashlib.md5(line_text.strip().encode()).hexdigest()[:6]
    return f"{file_slug}-{content_hash}"


def add_fact_ids_to_file(file_path: str) -> int:
    """Add fact IDs to all list items in a markdown file.
    
    Skips items that already have a fact ID comment.
    Returns the number of IDs added.
    """
    with open(file_path) as f:
        lines = f.readlines()
    
    modified = False
    count = 0
    new_lines = []
    
    for line in lines:
        # Match markdown list items (- or *) that don't already have a fact ID
        if re.match(r'^[\s]*[-*]\s+', line) and '<!-- fact:' not in line:
            stripped = line.rstrip('\n')
            fact_id = generate_fact_id(file_path, stripped)
            new_line = f"{stripped} <!-- fact:{fact_id} -->\n"
            new_lines.append(new_line)
            modified = True
            count += 1
        else:
            new_lines.append(line)
    
    if modified:
        with open(file_path, 'w') as f:
            f.writelines(new_lines)
    
    return count


def bootstrap_all_fact_ids() -> dict:
    """Add fact IDs to all memory files in people/, projects/, decisions/, insights/.
    
    Returns stats dict.
    """
    stats = {"files": 0, "facts_tagged": 0}
    dirs = ["people", "projects", "decisions", "insights"]
    
    for d in dirs:
        full_dir = os.path.join(config.memory_dir, d)
        if not os.path.exists(full_dir):
            continue
        for f in os.listdir(full_dir):
            if not f.endswith('.md'):
                continue
            fp = os.path.join(full_dir, f)
            count = add_fact_ids_to_file(fp)
            if count > 0:
                stats["files"] += 1
                stats["facts_tagged"] += count
    
    return stats
