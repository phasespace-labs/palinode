"""
Mem0 → Palinode File Generator

Takes classified memories and generates typed markdown files,
merging related memories into coherent documents.

Usage:
    python -m palinode.migration.mem0_generate

Input:
    $PALINODE_DIR/migration/mem0_classified.json

Output:
    Markdown files in $PALINODE_DIR/{category}/
"""
from __future__ import annotations

import json
import os
import re
import time
import logging
from collections import defaultdict
from datetime import UTC, datetime

import yaml

from palinode.core.config import config

logger = logging.getLogger("palinode.migration.mem0_generate")


# Category mapping
TYPE_TO_CATEGORY = {
    "PersonMemory": "people",
    "Decision": "decisions",
    "ProjectSnapshot": "projects",
    "Insight": "insights",
    "ActionItem": "inbox",
    "Config": "decisions",  # Config decisions go to decisions/
}


def generate_files() -> dict:
    """Generate Palinode markdown files from classified Mem0 memories.

    Groups memories by their 'group' slug, then generates one
    markdown file per group. Related memories within a group are
    merged into a single coherent document.

    Returns:
        Stats dict with counts of files created per category.
    """
    classified_path = os.path.join(config.memory_dir, "migration", "mem0_classified.json")
    with open(classified_path) as f:
        memories = json.load(f)

    # Filter out Skip
    active = [m for m in memories if m.get("type") != "Skip"]
    skipped = len(memories) - len(active)
    logger.info(f"Active: {len(active)}, Skipped: {skipped}")

    # Group by slug
    groups = defaultdict(list)
    for m in active:
        group = m.get("group", "unclassified") or "unclassified"
        groups[group].append(m)

    logger.info(f"Groups: {len(groups)}")

    stats = defaultdict(int)

    for group_slug, mems in groups.items():
        # Determine category from dominant type
        type_counts = defaultdict(int)
        for m in mems:
            type_counts[m.get("type", "Insight")] += 1
        dominant_type = max(type_counts, key=type_counts.get)
        category = TYPE_TO_CATEGORY.get(dominant_type, "inbox")

        # Collect all entities
        all_entities = list(set(
            e for m in mems for e in m.get("entities", [])
        ))

        # Sort memories by date
        mems.sort(key=lambda m: m.get("created_at", ""))

        # Date range
        dates = [m.get("created_at", "")[:10] for m in mems if m.get("created_at")]
        first_date = dates[0] if dates else "unknown"
        last_date = dates[-1] if dates else "unknown"

        # Build content
        content_lines = []
        for m in mems:
            date = m.get("created_at", "")[:10]
            source = m.get("source_agent", "?")
            text = m["content"].strip()
            if date:
                content_lines.append(f"- [{date}] {text}")
            else:
                content_lines.append(f"- {text}")

        # Title from group slug
        title = group_slug.replace("-", " ").title()

        # Build frontmatter
        frontmatter = {
            "id": f"{category}-{group_slug}",
            "category": category,
            "status": "active",
            "entities": all_entities,
            "source": "mem0-backfill",
            "source_agents": list(set(m.get("source_agent", "") for m in mems)),
            "date_range": f"{first_date} to {last_date}",
            "memory_count": len(mems),
            # #193: timezone-aware UTC ISO-8601. Previously ``time.strftime``
            # emitted local time stamped with the UTC marker ``Z``.
            "created_at": mems[0].get("created_at", datetime.now(UTC).isoformat()),
            "last_updated": datetime.now(UTC).isoformat(),
        }

        doc = f"---\n{yaml.dump(frontmatter, default_flow_style=False)}---\n\n# {title}\n\n"
        doc += "\n".join(content_lines) + "\n"

        # Write file
        file_path = os.path.join(config.memory_dir, category, f"{group_slug}.md")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Don't overwrite existing files — append suffix
        if os.path.exists(file_path):
            file_path = os.path.join(config.memory_dir, category, f"{group_slug}-mem0.md")

        with open(file_path, "w") as f:
            f.write(doc)

        stats[category] += 1
        logger.info(f"  {category}/{group_slug}.md ({len(mems)} memories)")

    # Git commit
    import subprocess
    try:
        subprocess.run(["git", "add", "."], cwd=config.memory_dir, check=False)
        subprocess.run(
            ["git", "commit", "-m", f"palinode: Mem0 backfill — {sum(stats.values())} files from {len(active)} memories"],
            cwd=config.memory_dir, check=False,
        )
    except Exception as e:
        logger.error(f"Git commit failed: {e}")

    return dict(stats)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stats = generate_files()
    print(f"Generated: {stats}")
