from __future__ import annotations

import os
import glob
import re
from datetime import datetime, timezone
from typing import Any

import frontmatter as _frontmatter

from palinode.core.config import config
from palinode.core import parser

# Marker written by Deliverable C (palinode_save auto-footer plumbing).
# Wikilinks that appear under this marker count as satisfying the entity
# requirement — the auto-footer is a derived view of ``entities:`` and
# deliberately links every frontmatter entity that has no inline body link.
_AUTO_FOOTER_MARKER = "<!-- palinode-auto-footer -->"

def check_wiki_drift(
    metadata: dict[str, Any],
    body: str,
) -> list[dict[str, str]]:
    """Check for drift between frontmatter ``entities:`` and body ``[[wikilinks]]``.

    Returns a (possibly empty) list of warning dicts, each with keys:
    ``kind`` (``"body_not_in_frontmatter"`` or ``"frontmatter_not_in_body"``) and
    ``detail`` (a human-readable description).

    Auto-footer-aware: wikilinks that appear after the
    ``<!-- palinode-auto-footer -->`` marker count as satisfying the frontmatter
    entity requirement.  Body wikilinks under the auto-footer are NOT flagged as
    missing from frontmatter — the auto-footer is derived from frontmatter, so
    those links are guaranteed to correspond to frontmatter entries.

    Args:
        metadata: Parsed frontmatter dict (from ``parser.parse_markdown``).
        body: Markdown body text (frontmatter stripped).

    Returns:
        List of warning dicts (empty list if surfaces are aligned).
    """
    entity_info = parser.parse_entities(metadata, body)
    fm_entities: list[str] = entity_info["entities_frontmatter"]
    body_entities: list[str] = entity_info["entities_body"]

    fm_set = set(fm_entities)
    body_set = set(body_entities)

    # Determine which wikilinks live under the auto-footer.
    auto_footer_entities: set[str] = set()
    if _AUTO_FOOTER_MARKER in body:
        _, _, footer_text = body.partition(_AUTO_FOOTER_MARKER)
        footer_labels = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', footer_text)
        for label in footer_labels:
            canonical = parser.canonicalize_wikilink(label.strip(), known_entities=fm_entities)
            auto_footer_entities.add(canonical)

    warnings: list[dict[str, str]] = []

    # 1. Body wikilinks not in frontmatter (skip auto-footer ones — they come from FM)
    for ent in body_entities:
        if ent not in fm_set and ent not in auto_footer_entities:
            warnings.append({
                "kind": "body_not_in_frontmatter",
                "detail": (
                    f"body wikilink not in entities frontmatter: {ent!r}"
                ),
            })

    # 2. Frontmatter entities not in body and not covered by auto-footer
    for ent in fm_entities:
        if ent not in body_set and ent not in auto_footer_entities:
            warnings.append({
                "kind": "frontmatter_not_in_body",
                "detail": (
                    f"entity not in body or see-also: {ent!r}"
                ),
            })

    return warnings


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
    wiki_drift: list[dict[str, Any]] = []
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

            # Extract body (strip frontmatter) for wiki_drift check.
            try:
                _post = _frontmatter.loads(content)
                body_text: str = _post.content
            except Exception:
                body_text = content

            entities = metadata.get("entities", [])
            for e in entities:
                entity_references[e] = entity_references.get(e, 0) + 1

            all_files.append({
                "path": rel_path,
                "metadata": metadata,
                "body": body_text,
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

        # 7. Wiki drift — frontmatter entities vs. body wikilinks
        body = f.get("body", "")
        drift_warnings = check_wiki_drift(meta, body)
        if drift_warnings:
            wiki_drift.append({"file": path, "warnings": drift_warnings})
                    
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
        "wiki_drift": wiki_drift,
        "core_count": core_count,
    }
