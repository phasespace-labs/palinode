from __future__ import annotations

import os
import glob
from datetime import datetime, timezone
from typing import Any

from palinode.core.config import config
from palinode.core import parser

def run_lint_pass() -> dict[str, Any]:
    """Scan PALINODE_DIR for memory health issues.
    
    Checks for:
    - Orphaned files (no entities, no references from other files)
    - Stale files (not updated in 90+ days, still marked status: active)
    - Missing fields (missing 'type', 'id', 'category')
    - Contradictions (potential contradictions, heuristic check)
    """
    base_dir = getattr(config, 'memory_dir', config.palinode_dir)
    pattern = os.path.join(base_dir, "**/*.md")
    
    orphaned_files = []
    stale_files = []
    missing_fields = []
    contradictions = []  # Heuristic placeholder
    missing_entities: list[str] = []
    missing_descriptions: list[str] = []
    core_count = 0

    now = datetime.now(timezone.utc)

    entity_references: dict[str, int] = {}
    all_files = []
    
    skip_dirs = {"archive", "logs", ".obsidian"}
    
    for filepath in glob.glob(pattern, recursive=True):
        rel_path = os.path.relpath(filepath, base_dir)
        parts = rel_path.split(os.sep)
        if parts[0] in skip_dirs:
            continue
            
        try:
            with open(filepath, "r") as f:
                content = f.read()
            metadata, _ = parser.parse_markdown(content)
            
            entities = metadata.get("entities", [])
            for e in entities:
                entity_references[e] = entity_references.get(e, 0) + 1
                
            all_files.append({
                "path": rel_path,
                "metadata": metadata,
            })
        except Exception:
            pass

    for f in all_files:
        path = f["path"]
        meta = f["metadata"]
        
        # 1. Missing fields
        missing = []
        if not meta.get("id"): missing.append("id")
        if not meta.get("type"): missing.append("type")
        if not meta.get("category"): missing.append("category")
        if missing:
            missing_fields.append({"file": path, "missing": missing})
            
        # 2. Orphans
        category = meta.get("category", "")
        if category and not path.startswith("daily/"):
            slug = path.split(os.sep)[-1].replace(".md", "")
            # Removing any layer suffixes like -status or -history
            if slug.endswith("-status"): slug = slug[:-7]
            if slug.endswith("-history"): slug = slug[:-8]
            
            own_entity_ref = f"{category}/{slug}"
            has_entities = len(meta.get("entities", [])) > 0
            is_referenced = entity_references.get(own_entity_ref, 0) > 0
            
            # An orphan has NO entities AND is not referenced by anything else
            if not has_entities and not is_referenced:
                orphaned_files.append(path)
                
        # 3. Missing entities (non-daily files with empty entities list)
        if not path.startswith("daily/") and not meta.get("entities"):
            missing_entities.append(path)

        # 4. Missing description
        if not path.startswith("daily/") and not meta.get("description"):
            missing_descriptions.append(path)

        # 5. Core count
        if meta.get("core"):
            core_count += 1

        # 6. Stale
        if meta.get("status") == "active":
            last_updated = meta.get("last_updated") or meta.get("created_at")
            if last_updated:
                try:
                    if isinstance(last_updated, str):
                        dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                    else:
                        dt = last_updated
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    
                    days_old = (now - dt).days
                    if days_old > 90:
                        stale_files.append({"file": path, "days_old": days_old})
                except Exception:
                    pass
                    
    # 4. Contradictions heuristics
    # Simple check: Any entity that has multiple active files
    file_statuses = {}
    for f in all_files:
         cat = f["metadata"].get("category", "")
         if not cat or f["path"].startswith("daily/"): continue
         slug = f["path"].split(os.sep)[-1].replace(".md", "")
         if slug.endswith("-status"): slug = slug[:-7]
         if slug.endswith("-history"): slug = slug[:-8]
         ent = f"{cat}/{slug}"
         
         status = f["metadata"].get("status", "active")
         if status == "active":
             file_statuses[ent] = file_statuses.get(ent, 0) + 1
             if file_statuses[ent] > 1:
                 contradictions.append({
                     "entity": ent, 
                     "issue": "Multiple 'active' files detected for the same entity."
                 })

    # Deduplicate contradictions
    unique_contradictions = [dict(t) for t in {tuple(d.items()) for d in contradictions}]

    return {
        "orphaned_files": orphaned_files,
        "stale_files": stale_files,
        "missing_fields": missing_fields,
        "contradictions": unique_contradictions,
        "missing_entities": missing_entities,
        "missing_descriptions": missing_descriptions,
        "core_count": core_count,
    }
