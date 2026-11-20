"""
Layer Split — Separate Identity, Status, and History

Each project/entity file becomes three files:
  {name}.md          — Identity (slow-changing core facts, architecture, decisions)
  {name}-status.md   — Status (current milestones, this week's focus, open tasks)
  {name}-history.md  — History (archived statuses, superseded facts)

Identity and Status get core:true. History gets core:false.
"""
from __future__ import annotations

import logging
import os
import re
import yaml
from datetime import UTC, datetime
from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.memory_files import list_memory_files
from palinode.core.path_guard import to_rel_path

logger = logging.getLogger("palinode.consolidation.layer_split")

#: The complete set of accepted ``layer_hint`` values. Author-supplied and finite,
#: which is what makes an unrecognized one a typo worth naming rather than a
#: caller's free choice to ignore.
LAYER_HINTS = ("identity", "status", "history")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _read_layer(path: str) -> tuple[dict, str, str]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    parts = content.split("---", 2)
    if len(parts) < 3 or not content.startswith("---"):
        raise ValueError(f"{path}: existing layer has no frontmatter")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: existing layer frontmatter is not a mapping")
    return metadata, parts[2].strip(), content


def _inherit_privacy(layer_meta: dict, source_meta: dict) -> dict:
    source_visibility = source_meta.get("visibility")
    layer_visibility = layer_meta.get("visibility")
    if layer_visibility in ("private", "restricted"):
        if source_visibility in ("private", "restricted") and layer_visibility != source_visibility:
            raise ValueError("existing layer has conflicting privacy frontmatter")
        for key in ("scope", "access"):
            if key in source_meta and key in layer_meta and source_meta[key] != layer_meta[key]:
                raise ValueError("existing layer has conflicting privacy frontmatter")
        for key in ("scope", "access"):
            if key in source_meta and key not in layer_meta:
                layer_meta[key] = source_meta[key]
        return layer_meta
    if source_visibility in ("private", "restricted"):
        for key in ("visibility", "scope", "access"):
            if key in source_meta:
                layer_meta[key] = source_meta[key]
    else:
        for key in ("visibility", "scope", "access"):
            if key in source_meta and key not in layer_meta:
                layer_meta[key] = source_meta[key]
    return layer_meta


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
    with open(file_path, encoding="utf-8") as f:
        content = f.read()
    
    # Parse frontmatter
    metadata = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except Exception:
                pass
            # Frontmatter that parses to a scalar or list (``---\nplain text\n---``)
            # yields a non-dict here, and every ``metadata.get`` below would raise.
            # Same failure shape as a malformed hint: one bad file kills the sweep.
            if not isinstance(metadata, dict):
                logger.warning(
                    "%s: frontmatter parsed as %s, not a mapping — ignoring it "
                    "and classifying by heuristic",
                    file_path,
                    type(metadata).__name__,
                )
                metadata = {}
            body = parts[2].strip()
    
    # Split body into sections by ## headings
    sections = re.split(r'^(## .+)$', body, flags=re.MULTILINE)
    
    identity_sections = []
    status_sections = []
    history_sections = []
    
    # Check for frontmatter layer_hint — overrides ALL keyword heuristics for this file.
    # Add `layer_hint: identity`, `layer_hint: status`, or `layer_hint: history` to a
    # file's YAML frontmatter to force the whole body into that layer (useful for files
    # that don't follow standard heading conventions). All three hints move the body to
    # the named layer's file, so `status` and `history` both leave the identity file an
    # empty shell — that is the point of the hint, not a defect.
    #
    # The hint is an *optimization over a heuristic* that works fine without it, so an
    # unreadable one degrades to "no hint" for this file rather than raising. A bare
    # ``layer_hint:`` parses as ``None``, and ``None.lower()`` used to abort the whole
    # sweep mid-run — the worst shape for a batch mutation, since some files are
    # already written and others are not, with no obvious boundary between them.
    raw_hint = metadata.get("layer_hint")
    layer_hint = "" if raw_hint is None else str(raw_hint).strip().lower()
    # A bare ``layer_hint:`` gives ``raw_hint is None``, so the value alone cannot say
    # whether a hint was ignored — track that separately.
    hint_ignored = False
    if layer_hint in LAYER_HINTS:
        # Treat the entire file body as the specified layer — no classification needed.
        # An already-emptied body contributes nothing, which is what makes a re-split
        # idempotent rather than appending a blank entry to the history file.
        hinted = [body] if body.strip() else []
        if layer_hint == "status":
            status_sections = hinted
        elif layer_hint == "history":
            history_sections = hinted
        else:
            identity_sections = hinted
        # Short-circuit to file writing (skip section classification below)
        sections = []  # Empty sections triggers the fallback path below
    elif "layer_hint" in metadata:
        # Present but unusable: a bare key (``None``), an empty value, or a typo like
        # ``histroy``/``archive``. Falling through silently would leave the author
        # believing an override is in effect when it is not — the same class of defect
        # the hint is meant to resolve. Name the file and the accepted set.
        hint_ignored = True
        logger.warning(
            "%s: unrecognized layer_hint %r — falling back to heuristic "
            "classification for this file. Accepted values: %s",
            file_path,
            raw_hint,
            ", ".join(LAYER_HINTS),
        )
    
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
    status_path = os.path.join(dir_path, f"{name}-status.md")
    history_path = os.path.join(dir_path, f"{name}-history.md")
    status_prior = _read_layer(status_path) if status_sections and os.path.exists(status_path) else None
    history_prior = _read_layer(history_path) if os.path.exists(history_path) else None
    if status_prior:
        _inherit_privacy(dict(status_prior[0]), metadata)
    history_privacy = dict(metadata)
    if status_prior:
        _inherit_privacy(history_privacy, status_prior[0])
    if history_prior:
        _inherit_privacy(dict(history_prior[0]), history_privacy)
    
    results = {}
    if hint_ignored:
        # Reported alongside the written paths so a caller that only inspects the
        # return value still learns the hint did not apply.
        results['layer_hint_ignored'] = raw_hint

    # Identity file (original name, core:true)
    id_meta = dict(metadata)
    id_meta['core'] = True
    id_meta['layer'] = 'identity'
    # emit timezone-aware UTC ISO-8601 (``+00:00``) rather than
    # ``strftime("...Z")``, which silently drops sub-second precision and
    # diverges from the project timestamp standard.
    id_meta['last_updated'] = _utc_now().isoformat()
    id_content = f"---\n{yaml.dump(id_meta, default_flow_style=False)}---\n\n"
    id_content += "\n\n".join(identity_sections)
    
    git_tools.write_memory_file(file_path, id_content)
    results['identity'] = file_path
    
    # Status file (core:true)
    if status_sections:
        st_meta = dict(status_prior[0]) if status_prior else {
            'id': f"{id_meta.get('id', name)}-status",
            'category': metadata.get('category', ''),
            'parent': id_meta.get('id', name),
        }
        st_meta['core'] = True
        st_meta['layer'] = 'status'
        st_meta['last_updated'] = _utc_now().isoformat()
        _inherit_privacy(st_meta, metadata)
        if not status_prior and metadata.get('summary'):
            st_meta['summary'] = f"Current status: {metadata['summary'][:80]}"
        if not status_prior and metadata.get('entities'):
            st_meta['entities'] = metadata['entities']
        
        st_content = f"---\n{yaml.dump(st_meta, default_flow_style=False)}---\n\n"
        st_content += "\n\n".join(status_sections)
        
        git_tools.write_memory_file(status_path, st_content)
        results['status'] = status_path
    
    # History file (core:false). Seeded empty when there is nothing to archive yet,
    # so the layer always exists.
    history_body = "\n\n".join(
        s for s in ([status_prior[1]] if status_prior else []) + history_sections if s.strip()
    )
    history_exists = history_prior is not None

    if history_exists:
        # A history file accumulates archived material, so content is *appended*.
        # Rewriting it wholesale would destroy the superseded facts the layer
        # exists to preserve — the same silent data loss this branch is here to
        # avoid. Existing frontmatter, including `status: archived`, is kept;
        # privacy fields are added when the source requires them.
        existing_meta, _, existing = history_prior
        protected_meta = _inherit_privacy(dict(existing_meta), history_privacy)
        if protected_meta != existing_meta:
            existing = f"---\n{yaml.dump(protected_meta, default_flow_style=False)}---\n\n" + history_prior[1]
        if history_body or protected_meta != existing_meta:
            git_tools.write_memory_file(
                history_path,
                existing.rstrip("\n") + ("\n\n" + history_body if history_body else "") + "\n",
            )
            results['history'] = history_path
    else:
        h_meta = {
            'id': f"{id_meta.get('id', name)}-history",
            'category': metadata.get('category', ''),
            'core': False,
            'layer': 'history',
            'parent': id_meta.get('id', name),
            'created_at': _utc_now().isoformat(),
        }
        _inherit_privacy(h_meta, history_privacy)
        if metadata.get('entities'):
            h_meta['entities'] = metadata['entities']

        h_content = f"---\n{yaml.dump(h_meta, default_flow_style=False)}---\n\n# {name} — History\n\n"
        h_content += f"{history_body}\n" if history_body else "Archived statuses and superseded facts.\n"

        git_tools.write_memory_file(history_path, h_content)
        results['history'] = history_path

    # One split = one commit covering every sibling file it wrote (up to
    # three: identity/status/history), through the git_tools choke point —
    # these writes previously left the split correct on disk but uncommitted.
    if config.git.auto_commit:
        committed_paths = [
            path for key, path in results.items() if key != 'layer_hint_ignored'
        ]
        git_tools.commit_memory_files(
            committed_paths, f"{config.git.commit_prefix} layer-split: {name}"
        )

    return results


def split_all_core_files() -> dict:
    """Split all core:true files in projects/ and people/ into layers.
    
    Returns stats dict.
    """
    from palinode.core import store
    from palinode.core import embedder
    
    # ``hints_ignored`` keeps a sweep from reporting a clean run when it quietly
    # classified files by heuristic that asked to be classified by hint.
    stats = {
        "files_split": 0,
        "status_created": 0,
        "history_created": 0,
        "triggers_registered": 0,
        "hints_ignored": 0,
        "files_with_ignored_hints": [],
        "files_refused": [],
    }
    
    for d in ["projects", "people"]:
        full_dir = os.path.join(config.memory_dir, d)
        if not os.path.exists(full_dir):
            continue
        for memory_file in list_memory_files(config.memory_dir, under=d, is_recursive=False):
            f = memory_file.path
            # Skip already-split files
            if f.endswith("-status.md") or f.endswith("-history.md"):
                continue
            
            with open(f, encoding="utf-8") as fh:
                content = fh.read()
            
            # Only split core files
            if "core: true" not in content:
                continue
            
            try:
                results = split_file(f)
            except ValueError as exc:
                # One file whose layers cannot be rewritten safely (conflicting
                # privacy, unreadable frontmatter) must not stop the sweep.
                logger.warning("layer split refused for %s: %s", f, exc)
                stats["files_refused"].append(to_rel_path(f))
                continue
            stats["files_split"] += 1
            if "layer_hint_ignored" in results:
                stats["hints_ignored"] += 1
                stats["files_with_ignored_hints"].append(
                    # Stringified for the wire: this is a diagnostic, and the raw YAML
                    # scalar can be any type the parser produced.
                    {"file": f, "value": str(results["layer_hint_ignored"])}
                )
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
                        rel_path = to_rel_path(results["identity"])
                        store.add_trigger(trigger_id, desc, rel_path, emb)
                        stats["triggers_registered"] += 1
                except Exception as e:
                    # Optional enrichment: the sweep continues without it.
                    logger.warning(
                        "Failed to auto-register trigger for %s: %s", f, e
                    )
    
    return stats
