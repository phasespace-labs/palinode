"""
Fact ID Generator

Adds inline fact IDs (<!-- fact:slug -->) to list items in memory files.
Run once to bootstrap IDs for existing files, then the consolidation
executor maintains them going forward.
"""
from __future__ import annotations

import os
import re
from palinode.core.config import config
from palinode.core import parser, git_tools
from palinode.core.hashing import stable_md5_hexdigest


def generate_fact_id(file_path: str, line_text: str) -> str:
    """Generate a deterministic fact ID from file path + content.
    
    Format: {category}-{file_slug}-{content_hash[:6]}
    Example: my-app-arch-a3f2b1
    """
    file_slug = os.path.splitext(os.path.basename(file_path))[0]
    content_hash = stable_md5_hexdigest(line_text.strip())[:6]
    return f"{file_slug}-{content_hash}"


def add_fact_ids_to_file(file_path: str) -> int:
    """Add fact IDs to all list items in a markdown file's **body**.

    Skips items that already have a fact ID comment, and never descends into
    the YAML frontmatter block: a frontmatter list entry (``- project/foo``
    under ``entities:``) is YAML syntax, not a memory fact. Tagging it made the
    consolidation executor treat it as a fact and rewrite it with LLM prose,
    which is how ``entities:`` came to hold status sentences that break strict
    ``yaml.safe_load`` (#470).

    Returns the number of IDs added.
    """
    with open(file_path) as f:
        content = f.read()

    frontmatter_block, body = parser.split_frontmatter(content)
    lines = body.splitlines(keepends=True)

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
        git_tools.write_memory_file(file_path, frontmatter_block + "".join(new_lines))

    return count


def bootstrap_all_fact_ids() -> dict:
    """Add fact IDs to all memory files in people/, projects/, decisions/, insights/.

    Operator-invoked, not automatic: reachable as ``POST /bootstrap-fact-ids``
    and ``palinode bootstrap-ids``, both registered admin capabilities. It walks
    all four memory directories, which is why frontmatter marker injection was
    never confined to status documents — every curated ``people/``,
    ``decisions/`` and ``insights/`` file was in range too. That is fixed at the
    source in :func:`add_fact_ids_to_file`, which now tags the body only;
    ``palinode repair-status --scope all`` removes markers a previous run left
    behind.

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
