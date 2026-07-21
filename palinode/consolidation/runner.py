"""
Consolidation Runner

Orchestrates weekly memory consolidation: daily → curated.
Uses a configurable LLM for distillation (any OpenAI-compatible endpoint).
"""
from __future__ import annotations

import os
import re
import json
import time
import glob
import logging
import shutil
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Callable

# Injectable consolidation callback shaped as
# (system_prompt, user_prompt) -> (response_text, model_used). Tests can return
# canned operation JSON while exercising parsing, application, and commit
# behavior without a live model or a wholesale mock of _consolidate_project.
# Kept here so the `runner.get_ollama_client` patch point stays stable.
LlmFn = Callable[[str, str], tuple[str, str]]

import yaml

from palinode.core.config import config
from palinode.core import store, embedder, git_tools
from palinode.core.ollama_client import OllamaError, OllamaRole, get_ollama_client
from palinode.consolidation.op_parse import op_kind, op_reason, parse_operations

logger = logging.getLogger("palinode.consolidation")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)

def _git_commit(message: str, files: list[str] | None = None) -> None:
    """Commit consolidation mutations through the git_tools choke point.

    One-mutation-one-commit: ``files`` is the explicit list of memory files this
    pass mutated; each gets its own per-file commit so a consolidation touching
    N files produces N commits, never a repo-wide ``git add *.md`` sweep that
    would conflate unrelated working-tree edits under one message (#565). The
    per-file ``message`` is suffixed with the file basename for blameability.

    ``files=None`` is retained only for callers with nothing concrete to stage;
    it is a no-op (we never sweep the repo). All real consolidation/ttl callers
    pass an explicit list.
    """
    if not config.git.auto_commit:
        return
    if not files:
        return
    # De-duplicate while preserving order — a project and its history sibling
    # may be listed more than once across a multi-project pass.
    seen: set[str] = set()
    for file_path in files:
        if file_path in seen or not os.path.exists(file_path):
            continue
        seen.add(file_path)
        base = os.path.basename(file_path)
        git_tools.commit_memory_file(file_path, f"{message} [{base}]")


def _touched_files(target: str) -> list[str]:
    """Files a single project compaction may have mutated.

    The op target itself plus its ``-history.md`` sibling, which the executor
    appends to on SUPERSEDE/ARCHIVE/RETRACT. Mirrors the path derivation in
    ``executor._append_to_history`` so a history append is committed alongside
    its parent mutation rather than swept up later (#565).
    """
    base = re.sub(r"-status\.md$", "", target)
    base = re.sub(r"\.md$", "", base)
    history_path = f"{base}-history.md"
    touched = [target]
    if os.path.exists(history_path):
        touched.append(history_path)
    return touched

def _get_decisions_for_project(project_id: str) -> list[dict]:
    """Fetch active decisions related to a specific project."""
    decisions_dir = os.path.join(config.memory_dir, "decisions")
    if not os.path.exists(decisions_dir):
        return []

    active_decisions = []
    for filepath in glob.glob(os.path.join(decisions_dir, "*.md")):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            parts = content.split("---")
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    entities = meta.get("entities", [])
                    if f"project/{project_id}" in entities and meta.get("status") != "superseded":
                        active_decisions.append({
                            "id": meta.get("id"),
                            "name": meta.get("name"),
                            "content": parts[2].strip()
                        })
                except Exception as _parse_exc:
                    # Silent skip was hiding corrupt frontmatter — log so
                    # operators can find and fix bad files.
                    # Recovery: run `palinode lint` to surface all parse errors.
                    logger.warning(
                        "palinode.consolidation: YAML parse failed in %r — "
                        "skipping for project decision lookup (run `palinode lint` "
                        "to find all bad files): %s",
                        filepath, _parse_exc,
                    )
                    continue
    return active_decisions

_CONSOLIDATION_SKIP_DIRS = {"daily", "archive", "inbox", "logs", "prompts", "specs"}


def _collect_daily_notes(lookback_days: int) -> tuple[list[dict], int]:
    """Collect recent daily notes from the daily directory.

    Returns:
        Tuple of (notes list, skipped_count) where skipped_count is the
        number of files whose YAML frontmatter failed to parse (#387).
        Callers surface skipped_count in the consolidation run summary so
        operators know to run ``palinode lint``.
    """
    daily_dir = os.path.join(config.memory_dir, "daily")
    if not os.path.exists(daily_dir):
        return [], 0

    cutoff_date = (_utc_now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    notes = []
    skipped = 0

    for filepath in glob.glob(os.path.join(daily_dir, "*.md")):
        filename = os.path.basename(filepath)
        date_str = filename.replace(".md", "")
        if date_str < cutoff_date:
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        meta = {}
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    content = parts[2].strip()
                except Exception as _parse_exc:
                    # Silent pass was hiding corrupt frontmatter — log so
                    # operators can find and fix bad files.
                    # Recovery: run `palinode lint` to surface all parse errors.
                    logger.warning(
                        "palinode.consolidation: YAML parse failed in %r — "
                        "frontmatter ignored, body text still collected "
                        "(run `palinode lint` to find all bad files): %s",
                        filepath, _parse_exc,
                    )
                    skipped += 1
                    # body text is kept — better to collect partial content
                    # than silently drop the note.
                    content = parts[2].strip() if len(parts) >= 3 else content

        mentions = list(set(re.findall(r"(project/[\w-]+|person/[\w-]+)", content)))

        # Fallback: detect projects by keyword if no entity refs found
        if not any(m.startswith("project/") for m in mentions):
            keyword_map = config.consolidation.keyword_map or {
                "project/palinode": ["Palinode", "palinode", "memory system", "SQLite-vec", "BGE-M3", "palinode_search"],
            }
            content_lower = content.lower()
            for project_ref, keywords in keyword_map.items():
                if any(kw.lower() in content_lower for kw in keywords):
                    mentions.append(project_ref)

        notes.append({
            "filepath": filepath,
            "date": date_str,
            "content": content,
            "mentions": mentions
        })

    return sorted(notes, key=lambda x: x["date"]), skipped

def _group_by_project(daily_notes: list[dict]) -> dict[str, list[dict]]:
    """Group daily notes by the projects they mention."""
    groups = {}
    for note in daily_notes:
        for m in note["mentions"]:
            if m.startswith("project/"):
                pid = m.split("project/")[1]
                if pid not in groups:
                    groups[pid] = []
                groups[pid].append(note)
    return groups

def _build_model_chain() -> list[dict[str, str]]:
    """Build ordered chain from config: primary + fallbacks.

    Returns list of {"model": ..., "url": ...} dicts.
    Primary is always first.
    """
    chain = [{"model": config.consolidation.llm_model, "url": config.consolidation.llm_url}]
    for fb in getattr(config.consolidation, "llm_fallbacks", []):
        chain.append({"model": fb["model"], "url": fb["url"]})
    return chain


def _call_llm_with_fallback(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    """Call the consolidation LLM with fallback chain.

    Tries primary model first. On timeout or HTTP error, tries each
    fallback in order. Returns (response_text, model_used).

    Raises:
        RuntimeError: All models in chain failed.
    """
    chain = _build_model_chain()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    client = get_ollama_client()

    last_error = None
    for i, endpoint in enumerate(chain):
        try:
            # Phase 4: route through the centralized client (CONSOLIDATION
            # role). retries=0 — the fallback chain itself is the retry strategy,
            # so the client shouldn't re-hammer each (slow, 600 s) host.
            result = client.chat_completions(
                messages,
                model=endpoint["model"],
                base_url=endpoint["url"],
                temperature=config.consolidation.llm_temperature,
                max_tokens=config.consolidation.llm_max_tokens,
                timeout=600.0,
                retries=0,
                role=OllamaRole.CONSOLIDATION,
            )
            if i > 0:
                logger.info(f"Fallback model succeeded: {endpoint['model']} @ {endpoint['url']} (primary failed)")
            return result, endpoint["model"]

        except OllamaError as e:
            last_error = e
            logger.warning(f"Model {endpoint['model']} @ {endpoint['url']} failed: {e}")
            continue

    raise RuntimeError(f"All {len(chain)} models failed. Last error: {last_error}")

def _consolidate_project(
    project_id: str,
    notes: list[dict],
    is_nightly: bool = False,
    llm_fn: LlmFn | None = None,
) -> tuple[list[dict], str]:
    """Consolidate a project by generating compaction operations.

    Reads the compaction prompt, extracts facts from the project file,
    sends both to the LLM, returns structured operations.

    Args:
        project_id: Project slug.
        notes: Recent daily notes mentioning this project.
        is_nightly: Use the lightweight nightly prompt.
        llm_fn: Injectable consolidation callback (#554).
            ``(system_prompt, user_prompt) ->
            (response_text, model_used)``. Defaults to the live fallback-chain
            caller; tests inject canned operation JSON while exercising the
            real fact-extraction, parsing, and application path.

    Returns:
        Tuple of (List of operation dicts, model_used).
    """
    # Load compaction prompt
    prompt_file = "nightly-consolidation.md" if is_nightly else "compaction.md"
    prompt_path = os.path.join(config.memory_dir, "specs", "prompts", prompt_file)
    if not os.path.exists(prompt_path):
        prompt_path = os.path.join(config.memory_dir, "specs", "prompts", "compaction.md")
        
    with open(prompt_path) as f:
        system_prompt = f.read()
    
    # Load project file and extract facts
    project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
    status_file = os.path.join(config.memory_dir, "projects", f"{project_id}-status.md")
    
    # Prefer status file for compaction (that's the fast-changing layer)
    target_file = status_file if os.path.exists(status_file) else project_file
    
    with open(target_file) as f:
        file_content = f.read()
    
    # Extract facts with IDs
    facts = []
    for match in re.finditer(r'^[\s]*[-*]\s+(.*?)<!-- fact:(\S+) -->', file_content, re.MULTILINE):
        facts.append({"id": match.group(2), "text": match.group(1).strip()})
    
    if not facts:
        logger.info(f"No tagged facts in {target_file}, skipping compaction")
        return [], "primary"
    
    # Format for LLM
    facts_text = "\n".join(f"[{f['id']}] {f['text']}" for f in facts)
    
    MAX_NOTES_CHARS = 6000
    notes_parts = []
    total = 0
    for n in reversed(notes):
        entry = f"### {n['date']}\n{n['content'][:1500]}"
        if total + len(entry) > MAX_NOTES_CHARS:
            break
        notes_parts.append(entry)
        total += len(entry)
    notes_parts.reverse()
    notes_text = "\n\n".join(notes_parts)
    
    user_prompt = f"""## EXISTING_FACTS ({len(facts)} facts from {os.path.basename(target_file)})

{facts_text}

## RECENT_NOTES (last {config.consolidation.lookback_days} days)

{notes_text}

Return the operations JSON array."""

    # Call the model through the injectable callback (default: live fallback chain).
    try:
        result_text, model_used = (llm_fn or _call_llm_with_fallback)(system_prompt, user_prompt)
    except Exception as e:
        logger.error(f"Failed to call LLM for {project_id}: {e}")
        return [], "failed"
    
    # Parse the operations JSON array — extraction + json_repair recovery +
    # nested-list/dict filtering all live in op_parse now.
    return parse_operations(result_text), model_used

def _check_contradictions(
    new_items: list[dict], project_id: str, llm_fn: LlmFn | None = None
) -> list[dict]:
    """Check new items for contradictions against existing knowledge base.

    ``llm_fn`` is the same injectable callback (#554) — defaults to the live caller;
    tests inject a fake returning a canned contradiction op so the embed/search
    + parse + translate path runs deterministically.
    """
    update_prompt_path = os.path.join(config.memory_dir, "specs", "prompts", "update.md")
    if not os.path.exists(update_prompt_path):
        return [{"operation": "ADD", "item": item} for item in new_items]
        
    with open(update_prompt_path) as f:
        system_prompt = f.read()

    operations = []
    for item in new_items:
        emb = embedder.embed(item.get("content", ""))
        if not emb:
            operations.append({"operation": "ADD", "item": item})
            continue

        # H1: consolidation dedup is an internal candidate lookup, not human
        # recall — use search_internal so recall_count / importance are never
        # bumped regardless of future refactors (ADR-015 H1).
        existing = store.search_internal(
            emb, category=item.get("category"), top_k=5, threshold=0.7,
        )

        if not existing:
            operations.append({"operation": "ADD", "item": item})
            continue

        user_prompt = f"""## Candidate
{json.dumps(item, indent=2)}

## Existing Similar Memories
{json.dumps(existing, indent=2)}

Return the operation as JSON."""

        try:
            result_text, model_used = (llm_fn or _call_llm_with_fallback)(system_prompt, user_prompt)

            json_match = re.search(r"```json\s*([\s\S]*?)```", result_text)
            if json_match:
                result_text = json_match.group(1)

            try:
                operation = json.loads(result_text)
                operation["item"] = item
                operations.append(operation)
            except json.JSONDecodeError:
                operations.append({"operation": "ADD", "item": item, "reason": "LLM parse failed"})
        except Exception as e:
            logger.error(f"Contradiction check failed: {e}")
            operations.append({"operation": "ADD", "item": item, "reason": f"API error: {e}"})

    return operations

def _write_project_summary(project_id: str, consolidation: dict) -> None:
    """Write the consolidated project summary back to the project markdown file."""
    project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
    
    # Simple write, but realistically we'd merge with existing body/frontmatter.
    # We will log the new summary block or replace the content.
    # human-readable UTC log heading. Previously used a literal ``Z``
    # suffix which trips the project-wide ``strftime("...Z")`` audit; ``UTC``
    # is unambiguous and keeps the log scannable.
    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    status_bullets = consolidation.get("status_bullets", [])
    bullets_text = "\n".join(f"- {b}" for b in status_bullets)
    
    if os.path.exists(project_file):
        with open(project_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Append consolidation log
        log_entry = f"\n\n## Consolidation Log ({now})\n{bullets_text}\n"
        content += log_entry
        git_tools.write_memory_file(project_file, content)
    else:
        # Create new
        metadata = {
            "id": f"project-{project_id}",
            "category": "project",
            "name": project_id,
            "entities": [f"project/{project_id}"],
            "last_updated": now
        }
        yaml_front = yaml.dump(metadata, default_flow_style=False)
        content = f"---\n{yaml_front}---\n\n# {project_id}\n\n## Summary\n{bullets_text}\n"
        git_tools.write_memory_file(project_file, content)

def _handle_superseded_decisions(superseded: list[dict]) -> None:
    """Mark superseded decisions in their frontmatter."""
    for decision in superseded:
        decision_path = os.path.join(config.memory_dir, "decisions", f"{decision['id']}.md")
        if not os.path.exists(decision_path):
            continue
            
        with open(decision_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # Update frontmatter status
        new_content = re.sub(
            r"^status:\s*active", 
            "status: superseded", 
            content, 
            flags=re.MULTILINE
        )
        
        # Add superseded_by if provided
        if "superseded_by" in decision:
            new_content = re.sub(
                r"^superseded_by:\s*\"\"", 
                f"superseded_by: {decision['superseded_by']}", 
                new_content, 
                flags=re.MULTILINE
            )
            
        git_tools.write_memory_file(decision_path, new_content)
        logger.info(f"Marked decision {decision['id']} as superseded")

def _extract_insights(all_notes: list[dict]) -> list[dict]:
    """Extract recurring patterns as insight candidates from notes.
    
    Looks for notes tagged as 'insight' category or containing
    keywords indicating a generalizable lesson.
    """
    if not all_notes:
        return []
        
    insights = []
    insight_keywords = ["lesson:", "insight:", "pattern:", "learned:", "takeaway:", "principle:"]
    
    for note in all_notes:
        content = note.get("content", "").lower()
        category = note.get("category", "").lower()
        
        if category in ("insight", "insights"):
            insights.append(note)
        elif any(kw in content for kw in insight_keywords):
            insights.append({
                **note,
                "category": "insight",
                "extracted": True,
            })
    
    if insights:
        logger.info(f"Extracted {len(insights)} insight candidates from {len(all_notes)} notes")
    return insights

def _archive_daily_notes(notes: list[dict]) -> None:
    """Move processed daily notes to the archive directory."""
    archive_dir = os.path.join(config.memory_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    
    for note in notes:
        try:
            date_prefix = note["date"][:4] # YYYY
            year_dir = os.path.join(archive_dir, date_prefix)
            os.makedirs(year_dir, exist_ok=True)
            new_path = os.path.join(year_dir, os.path.basename(note["filepath"]))
            shutil.move(note["filepath"], new_path)
        except Exception as e:
            logger.error(f"Failed to archive note {note['filepath']}: {e}")

def _update_status_summary(file_path: str, new_activity: list[dict]) -> None:
    """
    Update a -status.md file by merging new activity into existing sections
    rather than rewriting from scratch. Preserves longitudinal history.
    Inspired by NousResearch/hermes-agent trajectory compressor (MIT).

    Args:
        file_path: Absolute path to the status markdown file.
        new_activity: List of operation dicts with keys: op, fact_id, reason.
    """
    STATUS_SECTIONS = ["Current Work", "Open Tasks", "Recent Progress", "Next Steps", "Consolidation Log"]  # noqa: F841

    if not os.path.exists(file_path):
        return  # No existing status file to update

    with open(file_path, 'r') as f:
        existing = f.read()

    if not new_activity:
        return  # Nothing to update

    # Append new activity to "Consolidation Log" section if it exists,
    # otherwise append at end.
    timestamp = datetime.now().strftime("%Y-%m-%d")
    new_entry = f"\n### {timestamp}\n"
    for item in new_activity:
        op = item.get("op", item.get("operation", "UPDATE"))
        fact_id = item.get("fact_id", item.get("id", ""))
        reason = item.get("reason", "")
        new_entry += f"- [{op}] {fact_id}: {reason}\n"

    if "## Consolidation Log" in existing:
        updated = existing + new_entry
    else:
        updated = existing + f"\n## Consolidation Log\n{new_entry}"

    git_tools.write_memory_file(file_path, updated)

    logger.info(f"Updated status summary: {file_path} (+{len(new_activity)} entries)")


def _proposed_changes(target: str, operations: list[dict]) -> list[dict[str, str]]:
    return [
        {
            "type": op_kind(op),
            "file": target,
            "rationale": op_reason(op),
        }
        for op in operations
        if isinstance(op, dict)
    ]


def run_consolidation(lookback_days: int | None = None, dry_run: bool = False, llm_fn: LlmFn | None = None) -> dict[str, Any]:
    """Orchestrator for the entire memory consolidation process."""
    from palinode.consolidation.executor import apply_operations
    
    lookback = lookback_days or config.consolidation.lookback_days
    notes, yaml_skipped = _collect_daily_notes(lookback)
    if not notes:
        if dry_run:
            return {
                "status": "no notes found",
                "processed": 0,
                "processed_notes": 0,
                "projects_compacted": 0,
                "dry_run": True,
                "proposed_changes": [],
            }
        return {"status": "no notes found", "processed": 0}

    if yaml_skipped:
        logger.warning(
            "palinode.consolidation: %d daily note(s) had unparseable YAML frontmatter "
            "— run `palinode lint` to inspect. Proceeding with body text only.",
            yaml_skipped,
        )

    grouped = _group_by_project(notes)
    
    total_stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    projects_processed = 0
    proposed_changes: list[dict[str, str]] = []
    mutated_files: list[str] = []

    for project_id, pnotes in grouped.items():
        try:
            model_used_current = "primary"
            operations, model_used_current = _consolidate_project(project_id, pnotes, llm_fn=llm_fn)
            if not operations:
                continue

            model_used = model_used_current

            # Determine target file
            status_file = os.path.join(config.memory_dir, "projects", f"{project_id}-status.md")
            project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
            target = status_file if os.path.exists(status_file) else project_file

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                projects_processed += 1
                logger.info(f"Previewed compaction for {project_id}: {len(operations)} operation(s)")
                continue

            stats = apply_operations(target, operations)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            # Iteratively append operations to the status file, preserving history
            _update_status_summary(target, operations)

            # Track exactly the files this project's compaction touched so the
            # commit stages only them (one-mutation-one-commit).
            mutated_files.extend(_touched_files(target))

            projects_processed += 1
            logger.info(f"Compacted {project_id}: {stats}")

        except Exception as e:
            logger.error(f"Compaction failed for {project_id}: {e}")

    if dry_run:
        result = {
            "status": "success",
            "processed_notes": len(notes),
            "projects_compacted": projects_processed,
            "dry_run": True,
            "proposed_changes": proposed_changes,
        }
        if yaml_skipped:
            result["yaml_parse_errors"] = yaml_skipped
        return result
    
    # Extract insights and archive (only if at least one project compacted successfully)
    _extract_insights(notes)
    if projects_processed > 0:
        _archive_daily_notes(notes)
    else:
        logger.warning("No projects compacted successfully — skipping daily note archival")
    
    
    _git_commit(
        f"palinode: compaction {_utc_now().strftime('%Y-%m-%d')} — "
        f"{total_stats['updated']}u {total_stats['merged']}m "
        f"{total_stats['superseded']}s {total_stats['archived']}a"
        f" (model: {model_used})",
        files=mutated_files,
    )
    
    result: dict[str, Any] = {
        "status": "success",
        "processed_notes": len(notes),
        "projects_compacted": projects_processed,
        **total_stats,
    }
    if yaml_skipped:
        result["yaml_parse_errors"] = yaml_skipped
    return result


def run_nightly(lookback_days: int | None = None, dry_run: bool = False, llm_fn: LlmFn | None = None) -> dict[str, Any]:
    """Lightweight nightly consolidation — process today's daily notes only.

    Restricted to UPDATE and SUPERSEDE ops. No ARCHIVE or MERGE (those
    are weekly concerns). Smaller LLM context = better JSON output.
    """
    from palinode.consolidation.executor import apply_operations
    
    lookback = lookback_days or config.consolidation.nightly.lookback_days
    notes, yaml_skipped = _collect_daily_notes(lookback)
    if not notes:
        if dry_run:
            return {
                "status": "no_new_notes",
                "processed_notes": 0,
                "projects_compacted": 0,
                "dry_run": True,
                "proposed_changes": [],
            }
        return {"status": "no_new_notes", "processed_notes": 0, "projects_compacted": 0}

    if yaml_skipped:
        logger.warning(
            "palinode.consolidation: %d daily note(s) had unparseable YAML frontmatter "
            "— run `palinode lint` to inspect. Proceeding with body text only.",
            yaml_skipped,
        )
    
    grouped = _group_by_project(notes)
    
    total_stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    projects_processed = 0
    model_used = "primary"
    proposed_changes: list[dict[str, str]] = []
    mutated_files: list[str] = []

    for project_id, pnotes in grouped.items():
        try:
            operations, model_used_current = _consolidate_project(project_id, pnotes, is_nightly=True, llm_fn=llm_fn)
            if not operations:
                continue

            model_used = model_used_current

            # Enforce allows ops restriction
            allowed_ops = set(config.consolidation.nightly.allowed_ops)
            operations = [op for op in operations if op.get("op", op.get("operation", "")).upper() in allowed_ops]
            if not operations:
                continue

            # Determine target file
            status_file = os.path.join(config.memory_dir, "projects", f"{project_id}-status.md")
            project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
            target = status_file if os.path.exists(status_file) else project_file

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                projects_processed += 1
                logger.info(f"Previewed nightly compaction for {project_id}: {len(operations)} operation(s)")
                continue

            stats = apply_operations(target, operations, nightly_policy=True)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            _update_status_summary(target, operations)

            mutated_files.extend(_touched_files(target))

            projects_processed += 1
            logger.info(f"Nightly compacted {project_id}: {stats}")

        except Exception as e:
            logger.error(f"Nightly compaction failed for {project_id}: {e}")
            
    # Nightly does NOT archive daily notes (left for weekly)

    if dry_run:
        nightly_result = {
            "status": "success",
            "processed_notes": len(notes),
            "projects_compacted": projects_processed,
            "dry_run": True,
            "proposed_changes": proposed_changes,
        }
        if yaml_skipped:
            nightly_result["yaml_parse_errors"] = yaml_skipped
        return nightly_result
    
    if projects_processed > 0:
        _git_commit(
            f"palinode: nightly {_utc_now().strftime('%Y-%m-%d')} — "
            f"{total_stats['updated']}u {total_stats['superseded']}s"
            f" (model: {model_used})",
            files=mutated_files,
        )
    
    nightly_result: dict[str, Any] = {
        "status": "success",
        "processed_notes": len(notes),
        "projects_compacted": projects_processed,
        **total_stats,
    }
    if yaml_skipped:
        nightly_result["yaml_parse_errors"] = yaml_skipped
    return nightly_result
