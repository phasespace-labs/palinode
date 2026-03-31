"""
Palinode API Server

FastAPI application that serves Palinode endpoints over HTTP.
Provides semantic search capabilities (`/search`), saves new memories 
(`/save`), polls system status (`/status`), and handles ingestion tasks (`/ingest`).
"""
from __future__ import annotations

import os
import json
import logging
import time
import re
import yaml
import httpx
import subprocess
import glob
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from palinode.core import store, embedder, git_tools
from palinode.core.config import config


logger = logging.getLogger("palinode.api")
logger.setLevel(getattr(logging, config.services.api.log_level.upper(), logging.INFO))


class JsonlFormatter(logging.Formatter):
    """Logging Formatter dictating a JSONL chronological schema format."""
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()
        })


sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(sh)

os.makedirs(os.path.join(config.palinode_dir, "logs"), exist_ok=True)
fh = logging.FileHandler(os.path.join(config.palinode_dir, config.logging.operations_log))
fh.setFormatter(JsonlFormatter())
logger.addHandler(fh)

app = FastAPI(title="Palinode API")

# ── Auto-summary helpers ──────────────────────────────────────────────────────

def _generate_summary(content: str) -> str:
    """Invokes Ollama to produce a single-sentence logical summary of file memory.

    Args:
        content (str): Complete file content string to evaluate.

    Returns:
        str: Generated summary text. Yields an empty string if generation fails.
    """
    prompt = (
        f"Summarize the following memory file in one sentence (max {config.auto_summary.max_chars} chars). "
        "Be specific and factual. Output ONLY the summary, no preamble.\n\n"
        + content[:2000]
    )
    url = config.auto_summary.ollama_url or config.embeddings.primary.url
    
    try:
        resp = httpx.post(
            f"{url}/api/generate",
            json={"model": config.auto_summary.model, "prompt": prompt, "stream": False},
            timeout=30.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # Trim and cleanly strip quotes appended by inference
        raw = raw.strip('"\'').strip()
        if len(raw) > config.auto_summary.max_chars:
            raw = raw[:config.auto_summary.max_chars - 3] + "..."
        return raw
    except Exception as e:
        logger.warning(f"Ollama summary call failed: {e}")
        return ""


def _inject_summary(file_path: str, summary: str) -> None:
    """Injects a calculated generic summary into an active YAML frontmatter block.

    Args:
        file_path (str): File disk path to augment.
        summary (str): Target text to insert as `summary:`.
    """
    with open(file_path, "r") as f:
        text = f.read()
        
    # Match the closing --- of the respective layout block
    pattern = re.compile(r'^(---\n.*?\n)(---\n)', re.DOTALL)
    m = pattern.match(text)
    if not m:
        return  # no frontmatter detected, skip injection natively
        
    fm_body = m.group(1)
    closing = m.group(2)
    rest = text[m.end():]
    
    # Escape programmatic quotes safely for string interpolation payload
    safe_summary = summary.replace('"', '\\"')
    new_text = fm_body + f'summary: "{safe_summary}"\n' + closing + rest
    with open(file_path, "w") as f:
        f.write(new_text)

# ─────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup() -> None:
    """Initializes local filesystem database routines during FastAPI boot."""
    store.init_db()


class SearchRequest(BaseModel):
    query: str
    category: str | None = None
    limit: int | None = config.search.default_limit
    threshold: float | None = config.search.api_threshold
    hybrid: bool | None = None
    date_after: str | None = None
    date_before: str | None = None

class SearchAssociativeRequest(BaseModel):
    query: str
    seed_entities: list[str] | None = None
    limit: int | None = 5

class TriggerRequest(BaseModel):
    description: str
    memory_file: str
    trigger_id: str | None = None
    threshold: float | None = 0.75
    cooldown_hours: int | None = 24

class CheckTriggersRequest(BaseModel):
    query: str
    cooldown_bypass: bool | None = False

class SaveRequest(BaseModel):
    content: str
    type: str
    slug: str | None = None
    entities: list[str] | None = None
    metadata: Any | None = None
    core: bool | None = None


@app.post("/search")
def search_api(req: SearchRequest) -> list[dict[str, Any]]:
    """Semantic vector search against cached `.palinode.db` chunks.

    Returns:
        list[dict[str, Any]]: List payload sequence matching the criteria boundaries.
    """
    try:
        query_emb = embedder.embed(req.query)
        if not query_emb:
            return []
        
        use_hybrid = req.hybrid if req.hybrid is not None else config.search.hybrid_enabled
        
        if use_hybrid:
            results = store.search_hybrid(
                query_text=req.query,
                query_embedding=query_emb,
                category=req.category,
                top_k=req.limit or config.search.default_limit,
                threshold=req.threshold or config.search.api_threshold,
                hybrid_weight=config.search.hybrid_weight,
                date_after=req.date_after,
                date_before=req.date_before,
            )
        else:
            results = store.search(
                query_embedding=query_emb,
                category=req.category,
                top_k=req.limit or config.search.default_limit,
                threshold=req.threshold or config.search.api_threshold,
                date_after=req.date_after,
                date_before=req.date_before,
            )
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search-associative")
def search_associative_api(req: SearchAssociativeRequest) -> list[dict[str, Any]]:
    """Entity graph spreading activation recall."""
    try:
        seed_entities = req.seed_entities
        if not seed_entities:
            seed_entities = store.detect_entities_in_text(req.query)
            
        results = store.search_associative(
            query_text=req.query,
            seed_entities=seed_entities,
            top_k=req.limit or 5
        )
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/triggers")
def create_trigger_api(req: TriggerRequest) -> dict[str, Any]:
    """Register a new prospective trigger."""
    import uuid
    try:
        trigger_id = req.trigger_id or str(uuid.uuid4())
        emb = embedder.embed(req.description)
        if not emb:
            raise ValueError("Failed to embed trigger description")
            
        store.add_trigger(
            trigger_id=trigger_id,
            description=req.description,
            memory_file=req.memory_file,
            embedding=emb,
            threshold=req.threshold or 0.75,
            cooldown_hours=req.cooldown_hours or 24
        )
        return {"id": trigger_id, "status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/triggers")
def list_triggers_api() -> list[dict[str, Any]]:
    """List all registered triggers."""
    return store.list_triggers()


@app.delete("/triggers/{trigger_id}")
def delete_trigger_api(trigger_id: str) -> dict[str, str]:
    """Remove a trigger."""
    store.delete_trigger(trigger_id)
    return {"status": "deleted"}


@app.post("/check-triggers")
def check_triggers_api(req: CheckTriggersRequest) -> list[dict[str, Any]]:
    """Check context against prospective triggers."""
    try:
        emb = embedder.embed(req.query)
        if not emb:
            return []
        results = store.check_triggers(
            query_embedding=emb,
            cooldown_bypass=req.cooldown_bypass or False
        )
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/save")
def save_api(req: SaveRequest) -> dict[str, str]:
    """Persists a new memory instance chunk locally and initiates git backup sequences."""
    slug = req.slug
    if slug:
        # Prevent any potential JSON escape or traversal exploits if user defines slug
        slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')
        
    if not slug:
        slug = re.sub(r'[^a-z0-9]+', '-', req.content.split('\n')[0].lower()[:30]).strip('-')
        if not slug:
            slug = str(int(time.time()))
            
    type_map = {
        "PersonMemory": "people",
        "Decision": "decisions",
        "ProjectSnapshot": "projects",
        "Insight": "insights",
        "ResearchRef": "research",
        "ActionItem": "inbox"
    }
    category = type_map.get(req.type, "inbox")
    
    # Security scan: reject prompt injection and exfiltration attempts
    is_safe, reason = store.scan_memory_content(req.content)
    if not is_safe:
        raise HTTPException(status_code=400, detail=f"Security scan failed: {reason}")

    file_path = os.path.join(config.palinode_dir, category, f"{slug}.md")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    frontmatter_dict = {
        "id": f"{category}-{slug}",
        "category": category,
        "type": req.type,
        "entities": req.entities or [],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    if req.metadata:
        frontmatter_dict.update(req.metadata)
    if req.core is not None:
        frontmatter_dict["core"] = req.core
        
    doc = f"---\n{yaml.dump(frontmatter_dict)}---\n\n{req.content}\n"
    
    with open(file_path, "w") as f:
        f.write(doc)

    # Automatically generate summary block metadata explicitly
    if config.auto_summary.enabled:
        try:
            is_core = bool(frontmatter_dict.get("core", False))
            has_summary = bool(frontmatter_dict.get("summary"))
            if is_core and not has_summary and len(req.content) >= config.auto_summary.min_content_chars:
                summary = _generate_summary(doc)
                if summary:
                    _inject_summary(file_path, summary)
                    logger.info(f"Auto-summary injected for {file_path}")
        except Exception as e:
            logger.warning(f"Auto-summary generation failed (non-fatal): {e}")

    # Utilize auto backup procedures explicitly.
    if config.git.auto_commit:
        try:
            subprocess.run(["git", "add", file_path], cwd=config.palinode_dir, check=False)
            commit_msg = f"{config.git.commit_prefix} auto-save: {category}/{slug}.md"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=config.palinode_dir, check=False)
            
            if config.git.auto_push:
                subprocess.run(["git", "push"], cwd=config.palinode_dir, check=False)
        except Exception as e:
            logger.error(f"Git auto-commit failed: {e}")

    logger.info(f"Saved memory to {file_path}")
    return {"file_path": file_path, "id": frontmatter_dict["id"]}


@app.post("/generate-summaries")
def generate_summaries_api() -> dict[str, Any]:
    """Generate summaries for all core files that don't have one.
    
    Scans all markdown files with core: true in frontmatter.
    If summary: field is missing or empty, generates one via Ollama.
    """
    import glob
    from palinode.core import parser
    
    count = 0
    # Use palinode_dir since that's generally where memories are kept
    for filepath in glob.glob(os.path.join(config.palinode_dir, "**/*.md"), recursive=True):
        try:
            with open(filepath) as f:
                content = f.read()
            metadata, _ = parser.parse_markdown(content)
            if not metadata.get("core"):
                continue
            if metadata.get("summary"):
                continue  # Already has summary
            
            summary = _generate_summary(content)
            if summary:
                _inject_summary(filepath, summary)
                count += 1
                logger.info(f"Generated summary for {filepath}")
        except Exception as e:
            logger.warning(f"Summary generation failed for {filepath}: {e}")
    
    return {"status": "success", "summaries_generated": count}


@app.get("/status")
def status_api() -> dict[str, Any]:
    """Generates overarching health-checks to ensure pipeline availability."""
    stats: dict[str, Any] = dict(store.get_stats())
    
    git_stats = git_tools.commit_count(7)
    stats["git_commits_7d"] = git_stats["total_commits"]
    stats["git_summary_7d"] = git_stats["summary"]
    
    try:
        import subprocess
        unpushed = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"], cwd=config.palinode_dir, capture_output=True, text=True)
        stats["unpushed_commits"] = int(unpushed.stdout.strip()) if unpushed.stdout.strip() else 0
    except Exception:
        stats["unpushed_commits"] = 0

    db = store.get_db()
    try:
        fts_count = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        stats["fts_chunks"] = fts_count
    except Exception:
        stats["fts_chunks"] = 0
        
    try:
        entity_count = db.execute("SELECT count(DISTINCT entity_ref) FROM entities").fetchone()[0]
        stats["total_entities"] = entity_count
    except Exception:
        stats["total_entities"] = 0
        
    db.close()
    
    stats["hybrid_search"] = config.search.hybrid_enabled
    stats["associative_capability"] = stats["total_entities"] > 0
    
    try:
        httpx.get(config.embeddings.primary.url, timeout=2.0)
        ollama_reachable = True
    except Exception:
        ollama_reachable = False
        
    stats["ollama_reachable"] = ollama_reachable
    return stats


@app.post("/ingest")
def ingest_api() -> dict[str, str]:
    """Invoke document drop-box scanning routine."""
    from palinode.ingest.pipeline import process_inbox
    try:
        process_inbox()
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ingest-url")
def ingest_url_api(req: dict[str, str]) -> dict[str, str]:
    """Direct fetch and parse of an active hypertext url.

    Args:
        req (dict[str, str]): A standard dict providing "url" values.
    """
    from palinode.ingest.pipeline import ingest_url, is_safe_url
    url = req.get("url", "")
    name = req.get("name", url.split("/")[-1][:30])
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    if not is_safe_url(url):
        raise HTTPException(status_code=400, detail="Invalid or unsafe URL provided (SSRF protection)")
    try:
        result = ingest_url(url, name)
        if result:
            return {"status": "success", "file_path": result}
        return {"status": "no_content"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/rebuild-fts")
def rebuild_fts_api() -> dict[str, Any]:
    """Rebuild the FTS5 full-text search index from existing chunks.
    
    Run this once after upgrading to hybrid search, or if the FTS5
    index gets out of sync with the chunks table.
    """
    logger.info("Rebuilding FTS5 index...")
    count = store.rebuild_fts()
    logger.info(f"FTS5 rebuild complete: {count} chunks indexed")
    return {"status": "success", "chunks_indexed": count}


@app.post("/reindex")
def reindex_api() -> dict[str, Any]:
    """Resets memory boundaries enforcing a holistic index cycle across DB instances."""
    logger.info("Starting full reindex...")
    from palinode.indexer.watcher import PalinodeHandler
    handler = PalinodeHandler()
    count = 0
    errors = 0
    for filepath in glob.glob(os.path.join(config.palinode_dir, "**/*.md"), recursive=True):
        if handler.is_valid_file(filepath):
            try:
                handler._process_file(filepath)
                count += 1
            except Exception as e:
                errors += 1
                logger.warning(f"Reindex failed for {filepath}: {e}")
    # Rebuild FTS5 after bulk reindex to ensure consistency
    fts_count = store.rebuild_fts()
    logger.info(f"Reindex complete: {count} files processed, {errors} errors, FTS5: {fts_count}")
    return {"status": "success", "files_reindexed": count, "errors": errors, "fts_chunks": fts_count}


@app.get("/entities/{entity_ref:path}")
def entity_api(entity_ref: str) -> dict[str, Any]:
    """Get all files referencing an entity."""
    files = store.get_entity_files(entity_ref)
    graph = store.get_entity_graph(entity_ref)
    return {"entity": entity_ref, "files": files, "connected_entities": graph}


@app.get("/entities")
def entities_list_api() -> list[dict[str, Any]]:
    """List all known entities and their file counts."""
    db = store.get_db()
    cursor = db.cursor()
    try:
        cursor.execute("""
            SELECT entity_ref, count(*) as file_count
            FROM entities
            GROUP BY entity_ref
            ORDER BY file_count DESC
        """)
        results = [{"entity": row[0], "files": row[1]} for row in cursor.fetchall()]
    except Exception:
        results = []
    finally:
        db.close()
    return results


@app.get("/history/{file_path:path}")
def history_api(file_path: str, limit: int = 10) -> dict[str, Any]:
    """Get the change history for a memory file via git log.

    Shows when and how a memory was created, updated, or superseded.
    """
    import subprocess
    base_dir = os.path.abspath(getattr(config, 'memory_dir', config.palinode_dir))
    full_path = os.path.abspath(os.path.join(base_dir, file_path))
    
    if not full_path.startswith(base_dir):
        raise HTTPException(status_code=400, detail="Invalid path (path traversal detected)")
        
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    result = subprocess.run(
        ["git", "log", f"-{limit}", "--format=%H|%aI|%s", "--", file_path],
        capture_output=True, text=True,
        cwd=base_dir,
    )
    
    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({
                "hash": parts[0][:8],
                "date": parts[1],
                "message": parts[2],
            })
    
    return {"file": file_path, "history": commits}


@app.post("/consolidate")
def consolidate_api() -> dict[str, Any]:
    """Run a manual consolidation pass.

    Normally runs as a weekly cron, but can be triggered manually
    for testing or after a busy week.
    """
    from palinode.consolidation.runner import run_consolidation
    try:
        result = run_consolidation()
        return result
    except Exception as e:
        logger.error(f"Consolidation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/split-layers")
def split_layers_api() -> dict[str, Any]:
    """Split core files into Identity/Status/History layers."""
    from palinode.consolidation.layer_split import split_all_core_files
    stats = split_all_core_files()
    return stats


@app.post("/bootstrap-fact-ids")
def bootstrap_fact_ids_api() -> dict[str, Any]:
    """Add fact IDs to all memory files."""
    from palinode.consolidation.fact_ids import bootstrap_all_fact_ids
    stats = bootstrap_all_fact_ids()
    return stats


@app.get("/diff")
def diff_api(days: int = 7) -> dict[str, Any]:
    """Show memory changes in the last N days."""
    return {"diff": git_tools.diff(days)}


@app.get("/blame/{file_path:path}")
def blame_api(file_path: str, search: str | None = None) -> dict[str, Any]:
    """Show when each line of a memory file was last changed."""
    return {"blame": git_tools.blame(file_path, search)}


@app.get("/timeline/{file_path:path}")
def timeline_api(file_path: str, limit: int = 20) -> dict[str, Any]:
    """Show the evolution of a memory file over time."""
    return {"timeline": git_tools.timeline(file_path, limit)}


@app.post("/rollback")
def rollback_api(file_path: str, commit: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Revert a memory file to a previous version.
    
    Defaults to dry_run=True for safety. Set dry_run=False to actually revert.
    """
    return {"result": git_tools.rollback(file_path, commit, dry_run)}


@app.post("/push")
def push_api() -> dict[str, Any]:
    """Push memory changes to the remote repository."""
    return {"result": git_tools.push()}


@app.get("/git-stats")
def git_stats_api(days: int = 7) -> dict[str, Any]:
    """Get commit statistics for the memory repo."""
    return git_tools.commit_count(days)


@app.post("/migrate/mem0")
def migrate_mem0_api() -> dict[str, str]:
    """Run the Mem0 backfill pipeline.

    One-time migration: exports from Qdrant, deduplicates, classifies,
    and generates Palinode markdown files.
    """
    from palinode.migration.run_mem0_backfill import main as run_backfill
    try:
        run_backfill()
        return {"status": "success", "message": "Mem0 backfill complete. Review files and reindex."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def main() -> None:
    """Invokes Uvicorn CLI runner."""
    import uvicorn
    uvicorn.run("palinode.api.server:app", host=config.services.api.host, port=config.services.api.port)


if __name__ == "__main__":
    main()
