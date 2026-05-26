"""
Layer Split — Separate Identity, Status, and History

Each project/entity file becomes three files:
  {name}.md          — Identity (slow-changing core facts, architecture, decisions)
  {name}-status.md   — Status (current milestones, this week's focus, open tasks)
  {name}-history.md  — History (archived statuses, superseded facts)

Identity and Status get core:true. History gets core:false.
"""
from __future__ import annotations

import os
import re
import yaml
from datetime import UTC, datetime
from palinode.core.config import config


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def split_file(file_path: str) -> dict:
    """Split a memory file into Identity + Status + History layers.
    
    Heuristics for what goes where:
    - Identity: sections with titles containing: Architecture, Context, People,
      Canon, What This Is, Key Decisions, Overview, About
    - Status: sections with titles containing: Current, Status, Milestone,
      Active, This Week, Open, Consolidation Log, TODO
    - History: everything that's superseded, archived, or old consolidation logs
    
    Args:
        file_path: Path to the memory file to split.
        
    Returns:
        Dict with paths to the three new files.
    """
    with open(file_path) as f:
        content = f.read()
    
    # Parse frontmatter
    metadata = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except:
                pass
            body = parts[2].strip()
    
    # Split body into sections by ## headings
    sections = re.split(r'^(## .+)$', body, flags=re.MULTILINE)
    
    identity_sections = []
    status_sections = []
    history_sections = []
    
    # Check for frontmatter layer_hint — overrides ALL keyword heuristics for this file.
    # Add `layer_hint: status` or `layer_hint: identity` to a file's YAML frontmatter
    # to force all sections into that layer (useful for files that don't follow
    # standard heading conventions).
    layer_hint = metadata.get("layer_hint", "").lower()
    if layer_hint in ("identity", "status", "history"):
        # Treat the entire file body as the specified layer — no classification needed
        if layer_hint == "status":
            status_sections = [body]
        elif layer_hint == "history":
            history_sections = [body]
        else:
            identity_sections = [body]
        # Short-circuit to file writing (skip section classification below)
        sections = []  # Empty sections triggers the fallback path below
    
    # Load from config — these are tunable in palinode.config.yaml
    # under compaction.layer_split.identity_keywords / status_keywords
    IDENTITY_KEYWORDS = config.compaction.layer_split.identity_keywords
    STATUS_KEYWORDS = config.compaction.layer_split.status_keywords
    
    # First section (before any ##) goes to identity
    if sections and sections[0].strip():
        identity_sections.append(sections[0])
    
    # Classify each section
    for i in range(1, len(sections), 2):
        if i + 1 >= len(sections):
            break
        heading = sections[i]
        body_text = sections[i + 1]
        heading_lower = heading.lower()
        
        if any(kw in heading_lower for kw in STATUS_KEYWORDS):
            status_sections.append(heading + body_text)
        elif any(kw in heading_lower for kw in IDENTITY_KEYWORDS):
            identity_sections.append(heading + body_text)
        else:
            # Default: if it mentions dates/timestamps, it's status/history
            if re.search(config.compaction.layer_split.date_pattern, body_text):
                status_sections.append(heading + body_text)
            else:
                identity_sections.append(heading + body_text)
    
    # Write the three files
    base = os.path.splitext(file_path)[0]
    name = os.path.basename(base)
    dir_path = os.path.dirname(file_path)
    
    results = {}
    
    # Identity file (original name, core:true)
    id_meta = dict(metadata)
    id_meta['core'] = True
    id_meta['layer'] = 'identity'
    # #193: emit timezone-aware UTC ISO-8601 (``+00:00``) rather than
    # ``strftime("...Z")``, which silently drops sub-second precision and
    # diverges from the project standard set by #192.
    id_meta['last_updated'] = _utc_now().isoformat()
    id_content = f"---\n{yaml.dump(id_meta, default_flow_style=False)}---\n\n"
    id_content += "\n\n".join(identity_sections)
    
    with open(file_path, 'w') as f:
        f.write(id_content)
    results['identity'] = file_path
    
    # Status file (core:true)
    if status_sections:
        status_path = os.path.join(dir_path, f"{name}-status.md")
        st_meta = {
            'id': f"{id_meta.get('id', name)}-status",
            'category': metadata.get('category', ''),
            'core': True,
            'layer': 'status',
            'parent': id_meta.get('id', name),
            'last_updated': _utc_now().isoformat(),  # #193
        }
        if metadata.get('summary'):
            st_meta['summary'] = f"Current status: {metadata['summary'][:80]}"
        if metadata.get('entities'):
            st_meta['entities'] = metadata['entities']
        
        st_content = f"---\n{yaml.dump(st_meta, default_flow_style=False)}---\n\n"
        st_content += "\n\n".join(status_sections)
        
        with open(status_path, 'w') as f:
            f.write(st_content)
        results['status'] = status_path
    
    # History file (core:false, created empty for now)
    history_path = os.path.join(dir_path, f"{name}-history.md")
    if not os.path.exists(history_path):
        h_meta = {
            'id': f"{id_meta.get('id', name)}-history",
            'category': metadata.get('category', ''),
            'core': False,
            'layer': 'history',
            'parent': id_meta.get('id', name),
            'created_at': _utc_now().isoformat(),  # #193
        }
        if metadata.get('entities'):
            h_meta['entities'] = metadata['entities']
        
        h_content = f"---\n{yaml.dump(h_meta, default_flow_style=False)}---\n\n# {name} — History\n\nArchived statuses and superseded facts.\n"
        
        with open(history_path, 'w') as f:
            f.write(h_content)
        results['history'] = history_path
    
    return results


def split_all_core_files() -> dict:
    """Split all core:true files in projects/ and people/ into layers.
    
    Returns stats dict.
    """
    import glob
    from palinode.core import parser as md_parser
    from palinode.core import store
    from palinode.core import embedder
    
    stats = {"files_split": 0, "status_created": 0, "history_created": 0, "triggers_registered": 0}
    
    for d in ["projects", "people"]:
        full_dir = os.path.join(config.memory_dir, d)
        if not os.path.exists(full_dir):
            continue
        for f in glob.glob(os.path.join(full_dir, "*.md")):
            # Skip already-split files
            if f.endswith("-status.md") or f.endswith("-history.md"):
                continue
            
            with open(f) as fh:
                content = fh.read()
            
            # Only split core files
            if "core: true" not in content:
                continue
            
            results = split_file(f)
            stats["files_split"] += 1
            if "status" in results:
                stats["status_created"] += 1
            if "history" in results:
                stats["history_created"] += 1
            if "identity" in results:
                # Auto-register trigger for this entity using identity file (Phase 5.5)
                base = os.path.basename(results["identity"])
                desc = f"User is discussing or working on {base.replace('.md', '').replace('-', ' ')}"
                try:
                    emb = embedder.embed(desc)
                    if emb:
                        trigger_id = f"auto-{base}"
                        # Need to pass relative path to memory_file
                        rel_path = results["identity"].replace(config.memory_dir + "/", "")
                        store.add_trigger(trigger_id, desc, rel_path, emb)
                        stats["triggers_registered"] += 1
                except Exception as e:
                    print(f"Failed to auto-register trigger for {f}: {e}")
    
    return stats
