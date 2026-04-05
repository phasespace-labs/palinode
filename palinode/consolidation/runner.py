"""
Consolidation Runner

Orchestrates weekly memory consolidation: daily → curated.
Uses OLMo 3.1 on vLLM for LLM-powered distillation.
"""
from __future__ import annotations

import os
import re
import json
import time
import glob
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import yaml

from palinode.core.config import config
from palinode.core import store, embedder

logger = logging.getLogger("palinode.consolidation")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)

def _git_commit(message: str) -> None:
    if not config.git.auto_commit:
        return
    try:
        # Only add markdown files — respect .gitignore, avoid .db-journal etc.
        subprocess.run(["git", "add", "*.md", "**/*.md"], cwd=config.memory_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=config.memory_dir, check=True, capture_output=True)
        logger.info(f"Git commit: {message}")
    except subprocess.CalledProcessError as e:
        if b"nothing to commit" not in e.stdout:
            logger.error(f"Git commit failed: {e.stderr.decode()}")

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
                except Exception:
                    continue
    return active_decisions

def _collect_daily_notes(lookback_days: int) -> list[dict]:
    """Collect recent daily notes from the daily directory."""
    daily_dir = os.path.join(config.memory_dir, "daily")
    if not os.path.exists(daily_dir):
        return []
    
    cutoff_date = (_utc_now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    notes = []
    
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
                except Exception:
                    pass
        
        mentions = list(set(re.findall(r"(project/[\w-]+|person/[\w-]+)", content)))
        
        # Fallback: detect projects by keyword if no entity refs found
        if not any(m.startswith("project/") for m in mentions):
            keyword_map = {
                "project/mm-kmd": ["MM-KMD", "MM_KMD", "Kill My Darlings", "murder mystery", "OLMo", "LoRA", "vLLM", "LangGraph", "character agent", "Director", "mastermind"],
                "project/palinode": ["Palinode", "palinode", "memory system", "SQLite-vec", "BGE-M3", "palinode_search"],
                "project/color-class": ["FPFV", "color grading", "DaVinci Resolve", "Color Class", "RAW grading", "Yumi"],
                "project/infrastructure": ["server", "GPU", "Ollama", "homelab"],
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
        
    return sorted(notes, key=lambda x: x["date"])

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

def _consolidate_project(project_id: str, notes: list[dict]) -> list[dict]:
    """Consolidate a project by generating compaction operations.
    
    Reads the compaction prompt, extracts facts from the project file,
    sends both to the LLM, returns structured operations.
    
    Args:
        project_id: Project slug.
        notes: Recent daily notes mentioning this project.
        
    Returns:
        List of operation dicts.
    """
    # Load compaction prompt
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
        return []
    
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

    # Call LLM
    response = httpx.post(
        f"{config.consolidation.llm_url}/v1/chat/completions",
        json={
            "model": config.consolidation.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": config.consolidation.llm_temperature,
            "max_tokens": config.consolidation.llm_max_tokens,
        },
        timeout=600.0,
    )
    response.raise_for_status()
    result_text = response.json()["choices"][0]["message"]["content"]
    
    # Parse JSON
    json_match = re.search(r'\[[\s\S]*\]', result_text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            # LLM often outputs malformed JSON — use json_repair
            try:
                from json_repair import repair_json
                repaired = repair_json(json_match.group(), return_objects=True)
                if isinstance(repaired, list):
                    # Filter out any non-dict entries (LLM sometimes nests lists)
                    valid_ops = [op for op in repaired if isinstance(op, dict) and "op" in op]
                    logger.info(f"Repaired malformed LLM JSON ({len(valid_ops)} valid ops from {len(repaired)} entries)")
                    return valid_ops
            except Exception as repair_err:
                logger.error(f"json_repair also failed: {repair_err}")
            logger.error(f"Could not parse LLM JSON for compaction")
            logger.debug(f"Raw LLM output: {json_match.group()[:500]}")
            return []
    
    logger.warning(f"Could not parse operations from LLM response for {project_id}")
    return []

def _check_contradictions(new_items: list[dict], project_id: str) -> list[dict]:
    """Check new items for contradictions against existing knowledge base."""
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

        existing = store.search(emb, category=item.get("category"), top_k=5, threshold=0.7)

        if not existing:
            operations.append({"operation": "ADD", "item": item})
            continue

        user_prompt = f"""## Candidate
{json.dumps(item, indent=2)}

## Existing Similar Memories
{json.dumps(existing, indent=2)}

Return the operation as JSON."""

        try:
            response = httpx.post(
                f"{config.consolidation.llm_url}/v1/chat/completions",
                json={
                    "model": config.consolidation.llm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
                timeout=60.0,
            )
            response.raise_for_status()
            result_text = response.json()["choices"][0]["message"]["content"]

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
    now = _utc_now().strftime("%Y-%m-%d %H:%M:%SZ")
    
    status_bullets = consolidation.get("status_bullets", [])
    bullets_text = "\n".join(f"- {b}" for b in status_bullets)
    
    if os.path.exists(project_file):
        with open(project_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Append consolidation log
        log_entry = f"\n\n## Consolidation Log ({now})\n{bullets_text}\n"
        content += log_entry
        with open(project_file, "w", encoding="utf-8") as f:
            f.write(content)
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
        with open(project_file, "w", encoding="utf-8") as f:
            f.write(content)

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
            
        with open(decision_path, "w", encoding="utf-8") as f:
            f.write(new_content)
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

    with open(file_path, 'w') as f:
        f.write(updated)

    logger.info(f"Updated status summary: {file_path} (+{len(new_activity)} entries)")


def run_consolidation(lookback_days: int | None = None) -> dict[str, Any]:
    """Orchestrator for the entire memory consolidation process."""
    from palinode.consolidation.executor import apply_operations
    
    lookback = lookback_days or config.consolidation.lookback_days
    notes = _collect_daily_notes(lookback)
    if not notes:
        return {"status": "no notes found", "processed": 0}
    
    grouped = _group_by_project(notes)
    
    total_stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    projects_processed = 0
    
    for project_id, pnotes in grouped.items():
        try:
            operations = _consolidate_project(project_id, pnotes)
            if not operations:
                continue
            
            # Determine target file
            status_file = os.path.join(config.memory_dir, "projects", f"{project_id}-status.md")
            project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
            target = status_file if os.path.exists(status_file) else project_file
            
            stats = apply_operations(target, operations)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            # Iteratively append operations to the status file, preserving history
            _update_status_summary(target, operations)

            projects_processed += 1
            logger.info(f"Compacted {project_id}: {stats}")
            
        except Exception as e:
            logger.error(f"Compaction failed for {project_id}: {e}")
    
    # Extract insights and archive (only if at least one project compacted successfully)
    _extract_insights(notes)
    if projects_processed > 0:
        _archive_daily_notes(notes)
    else:
        logger.warning("No projects compacted successfully — skipping daily note archival")
    
    _git_commit(f"palinode: compaction {_utc_now().strftime('%Y-%m-%d')} — "
                f"{total_stats['updated']}u {total_stats['merged']}m "
                f"{total_stats['superseded']}s {total_stats['archived']}a")
    
    return {
        "status": "success",
        "processed_notes": len(notes),
        "projects_compacted": projects_processed,
        **total_stats,
    }
