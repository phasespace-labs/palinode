"""
Palinode Store — SQLite-vec persistent vector database.

Manages connection to the .palinode.db database to perform 
semantic memory chunk updates, deletions, and search retrieval.

# Some patterns in this file are adapted from NousResearch/hermes-agent (MIT License)
# https://github.com/NousResearch/hermes-agent
# Specifically: FTS5 query sanitization (hermes_state.py) and memory security scanning (tools/memory_tool.py)
"""
from __future__ import annotations

import re
import sqlite3
import sqlite_vec
import json
import os
import hashlib
from typing import Any, Sequence
from datetime import UTC, datetime
from palinode.core.config import config

# ── Security Scanning ────────────────────────────────────────────────────────
# Adapted from NousResearch/hermes-agent (MIT License)
# https://github.com/NousResearch/hermes-agent

INJECTION_PATTERNS = [
    r'ignore\s+(previous|prior|all)\s+instructions',
    r'\byou\s+are\s+now\b',
    r'disregard\s+(your|all)\s+(previous|prior)',
    r'output\s+(all|your)\s+(stored|memory|memories)',
    r'send\s+(your|all)\s+(memory|memories|context)[\s\w]*\s+to',
    r'<script[\s>]',
    r'javascript\s*:',
    r'system\s*prompt\s*:',
]


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def scan_memory_content(content: str) -> tuple[bool, str]:
    """
    Scan memory content for prompt injection and credential exfiltration patterns.
    Returns (is_safe: bool, reason: str).
    Adapted from NousResearch/hermes-agent (MIT).

    Args:
        content: The memory content string to check.

    Returns:
        A tuple (is_safe, reason) where is_safe=True means content passed all checks.
    """
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return False, f"Content matches injection pattern: '{pattern}'"
    # Check for excessive special chars (potential encoding attack)
    special_ratio = sum(
        1 for c in content if not c.isalnum() and c not in ' .,!?-_\n#:()[]{}\'\"'
    ) / max(len(content), 1)
    if special_ratio > 0.4:
        return False, "Content has unusually high ratio of special characters"
    return True, "ok"

def get_db() -> sqlite3.Connection:
    """Gets an active connection to the SQLite database with vec extension active.

    Returns:
        sqlite3.Connection: Database connection featuring vec.
    """
    db = sqlite3.connect(config.db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.row_factory = sqlite3.Row
    return db


def _validated_embedding_dimensions() -> int:
    """Ensure embedding dimensions are numeric before interpolating into DDL."""
    try:
        dimensions = int(config.embeddings.primary.dimensions)
    except (TypeError, ValueError) as exc:
        raise ValueError("Embedding dimensions must be a positive integer") from exc
    if dimensions <= 0:
        raise ValueError("Embedding dimensions must be a positive integer")
    return dimensions


def _parameterize_in_clause(values: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
    """Build placeholder-only IN clauses while keeping values parameterized."""
    params = tuple(values)
    if not params:
        raise ValueError("IN clause values must not be empty")
    return ",".join("?" for _ in params), params

def init_db() -> None:
    """Initializes the required tables in the database.

    Creates `chunks` for metadata/source and `chunks_vec` for the 
    corresponding fast vector index on embedding geometries.
    """
    os.makedirs(os.path.dirname(config.db_path), exist_ok=True)
    db = get_db()
    dimensions = _validated_embedding_dimensions()
    db.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            section_id TEXT,
            category TEXT,
            content TEXT NOT NULL,
            metadata JSON,
            created_at TEXT,
            last_updated TEXT
        )
    """)
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN content_hash TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        db.execute("ALTER TABLE chunks ADD COLUMN importance FLOAT DEFAULT 0.5")
        db.execute("ALTER TABLE chunks ADD COLUMN last_recalled TEXT")
        db.execute("ALTER TABLE chunks ADD COLUMN recall_count INT DEFAULT 0")
        db.execute("ALTER TABLE chunks ADD COLUMN memory_type TEXT DEFAULT 'general'")
    except sqlite3.OperationalError:
        pass  # Columns already exist

    db.execute("""
        CREATE TABLE IF NOT EXISTS triggers (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            memory_file TEXT NOT NULL,
            threshold FLOAT DEFAULT 0.75,
            cooldown_hours INT DEFAULT 24,
            last_fired TEXT,
            fire_count INT DEFAULT 0,
            created_at TEXT,
            enabled INT DEFAULT 1
        )
    """)
    db.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS triggers_vec USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[{dimensions}]
        )
    """)

    # Full-text search index for BM25 keyword matching.
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content,
            file_path,
            category,
            content=chunks,
            content_rowid=rowid,
            tokenize='unicode61'  -- Fallback to default unicode61 (tokenchars limit: macos system sqlite parse error on "-./_#@")
        )
    """)

    # 1024 float dimensions for bge-m3 default
    db.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            id TEXT PRIMARY KEY,
            embedding FLOAT[{dimensions}]
        )
    """)

    # Auto-populate FTS5 index if chunks exist but FTS5 is empty
    # (handles upgrade from pre-1.5 databases)
    fts_count = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    chunks_count = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    if fts_count == 0 and chunks_count > 0:
        db.execute("""
            INSERT INTO chunks_fts(rowid, content, file_path, category)
            SELECT rowid, content, file_path, category FROM chunks
        """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            entity_ref TEXT NOT NULL,
            file_path TEXT NOT NULL,
            category TEXT,
            last_seen TEXT,
            PRIMARY KEY (entity_ref, file_path)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_ref ON entities(entity_ref)
    """)

    db.commit()
    db.close()

def upsert_chunks(chunks_data: list[dict[str, Any]], skip_unchanged: bool = True) -> int:
    """Update or insert new chunks into the document and vector indices.

    Args:
        chunks_data (list[dict[str, Any]]): A list including dictionaries of id, 
            file_path, content, section_id, embedding, and optional keys category, metadata, 
            created_at, last_updated.
        skip_unchanged (bool): If True, skip re-embedding chunks whose content hash matches.

    Returns:
        int: Number of chunks actually written (excluding skipped unchanged ones).
    """
    db = get_db()
    cursor = db.cursor()
    written = 0

    for chunk in chunks_data:
        content_hash = hashlib.sha256(chunk["content"].encode()).hexdigest()

        if skip_unchanged:
            # Check if this chunk already exists with the same content
            cursor.execute(
                "SELECT content_hash FROM chunks WHERE id = ?",
                (chunk["id"],)
            )
            existing = cursor.fetchone()
            if existing and existing["content_hash"] == content_hash:
                # Content unchanged — skip expensive embedding + write
                continue

        # Content is new or changed — write everything
        metadata_json = json.dumps(chunk.get("metadata", {}), default=str)
        cursor.execute("""
            INSERT OR REPLACE INTO chunks 
            (id, file_path, section_id, category, content, metadata, created_at, last_updated, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chunk["id"], chunk["file_path"], chunk["section_id"], chunk.get("category", ""),
            chunk["content"], metadata_json, chunk.get("created_at"), chunk.get("last_updated"), content_hash
        ))
        
        emb_json = json.dumps(chunk["embedding"])
        try:
            cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (chunk["id"],))
        except Exception:
            pass  # May not exist yet — safe to ignore
        try:
            cursor.execute("""
                INSERT INTO chunks_vec (id, embedding)
                VALUES (?, ?)
            """, (chunk["id"], emb_json))
        except Exception:
            # UNIQUE constraint can fire if DELETE failed silently
            # (e.g. vec0 table internals). Force replace.
            cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (chunk["id"],))
            cursor.execute("""
                INSERT INTO chunks_vec (id, embedding)
                VALUES (?, ?)
            """, (chunk["id"], emb_json))

        # Sync FTS5 index (best-effort — FTS5 external content tables
        # can get out of sync during bulk writes. If it fails, we mark
        # for rebuild rather than crashing the whole upsert.)
        try:
            cursor.execute("DELETE FROM chunks_fts WHERE rowid = (SELECT rowid FROM chunks WHERE id = ?)", (chunk["id"],))
            cursor.execute("""
                INSERT INTO chunks_fts(rowid, content, file_path, category)
                SELECT rowid, content, file_path, category FROM chunks WHERE id = ?
            """, (chunk["id"],))
        except Exception:
            # FTS5 sync failed — will be caught by periodic rebuild
            pass

        written += 1

    db.commit()
    db.close()
    return written

def delete_file_chunks(file_path: str) -> None:
    """Deletes all chunks associated with a specific file path.

    Args:
        file_path (str): The path to the file whose chunks should be erased.
    """
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM chunks WHERE file_path = ?", (file_path,))
    ids = [row["id"] for row in cursor.fetchall()]
    if ids:
        # Clean FTS5 index before deleting the source rows (best-effort)
        for chunk_id in ids:
            try:
                cursor.execute("DELETE FROM chunks_fts WHERE rowid = (SELECT rowid FROM chunks WHERE id = ?)", (chunk_id,))
            except Exception:
                pass  # FTS5 may be out of sync — periodic rebuild handles this
        
        placeholders, params = _parameterize_in_clause(ids)
        cursor.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", params)
        cursor.execute(f"DELETE FROM chunks_vec WHERE id IN ({placeholders})", params)
        
    cursor.execute("DELETE FROM entities WHERE file_path = ?", (file_path,))
    db.commit()
    db.close()

def rebuild_fts() -> int:
    """Rebuild the FTS5 index from scratch.

    Drops and recreates the FTS5 table, repopulating from chunks.
    Use when FTS5 gets corrupted (external content tables are fragile).

    Returns:
        Number of chunks indexed.
    """
    db = get_db()
    db.execute("DROP TABLE IF EXISTS chunks_fts")
    db.execute("""
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            content, file_path, category,
            content=chunks, content_rowid=rowid,
            tokenize='unicode61'  -- Fallback to default unicode61 (tokenchars limit: macos system sqlite parse error on "-./_#@")
        )
    """)
    db.execute("""
        INSERT INTO chunks_fts(rowid, content, file_path, category)
        SELECT rowid, content, file_path, category FROM chunks
    """)
    db.commit()
    count = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    db.close()
    return count


def _is_daily_file(file_path: str) -> bool:
    """Check if a file path belongs to the daily/ directory."""
    return "/daily/" in file_path or file_path.startswith("daily/")


def search(query_embedding: list[float], category: str | None = None,
           status_exclude_list: list[str] | None = None, top_k: int = 10, threshold: float = 0.6,
           date_after: str | None = None, date_before: str | None = None,
           context_entities: list[str] | None = None,
           include_daily: bool = False) -> list[dict[str, Any]]:
    """Search the vector index for semantically similar memory chunks.

    Performs cosine similarity search via SQLite-vec, filtering by category
    and status. Results are scored and ranked by relevance.

    Args:
        query_embedding (list[float]): 1024-dimensional embedding vector (BGE-M3 defaults).
        category (str | None): Optional filter (e.g., "person", "project", "decision").
        status_exclude_list (list[str] | None): Disregarded meta status defaults (e.g., "archived").
            Uses `config.search.exclude_status` if unset.
        top_k (int): Maximum number of results to return.
        threshold (float): Minimum cosine similarity score (0.0-1.0).
        context_entities (list[str] | None): Entity refs (e.g. ["project/palinode"])
            for ambient context boost. Matching results get score * config.context.boost.
        include_daily (bool): If True, skip the daily/ penalty (search daily notes at full rank).

    Returns:
        list[dict]: List of dicts with keys: file_path, section_id, content, category, 
            metadata, score. Sorted by score in descending order.

    Note:
        SQLite-vec natively returns L2 distance. We convert this mathematically to cosine 
        similarity using: score = 1.0 - (distance² / 2.0).
        This is perfectly valid because BGE-M3 embeddings are consistently L2-normalized.
    """
    if status_exclude_list is None:
        status_exclude_list = config.search.exclude_status

    db = get_db()
    cursor = db.cursor()
    query_vec_json = json.dumps(query_embedding)
    
    sql = """
        SELECT c.*, v.distance
        FROM chunks_vec v
        JOIN chunks c ON v.id = c.id
        WHERE v.embedding MATCH ? AND k = ?
    """
    # Grab slightly more than we need so we can safely filter out exclusions 
    # without running out of return slots
    params = [query_vec_json, top_k * 3]
    if category:
        sql += " AND c.category = ?"
        params.append(category)

    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
    
    results = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        if meta.get("status", "active") in status_exclude_list:
            continue
            
        if date_after or date_before:
            updated = meta.get("last_updated", row["created_at"]) or ""
            if updated:
                if date_after and updated < date_after:
                    continue
                if date_before and updated > date_before:
                    continue

        # Mathematical conversion: L2 distance → Cosine similarity
        # Applies because BGE-M3 (default embedding engine) produces strictly L2-normalized vectors.
        dist = row["distance"] or 0
        score = 1.0 - ((dist ** 2) / 2.0)

        if score < threshold:
            continue

        results.append({
            "file_path": row["file_path"],
            "section_id": row["section_id"],
            "content": row["content"],
            "category": row["category"],
            "metadata": meta,
            "content_hash": row["content_hash"] if "content_hash" in row.keys() else None,
            "score": score,
            "raw_score": score,
        })
        if len(results) >= top_k:
            break

    db.close()

    # Ambient context boost (same logic as search_hybrid)
    if context_entities and config.context.enabled and config.context.boost != 1.0:
        context_files: set[str] = set()
        for entity in context_entities:
            for row in get_entity_files(entity):
                context_files.add(row["file_path"])
        if context_files:
            for r in results:
                if r["file_path"] in context_files:
                    r["score"] = r.get("score", 0) * config.context.boost
            results.sort(key=lambda r: r.get("score", 0.0), reverse=True)

    # Issue #93: Penalize daily/ files to prevent session notes from dominating results
    penalty = config.search.daily_penalty
    if not include_daily and penalty != 1.0:
        needs_resort = False
        for r in results:
            if _is_daily_file(r["file_path"]):
                r["score"] = r.get("score", 0) * penalty
                needs_resort = True
        if needs_resort:
            results.sort(key=lambda r: r.get("score", 0.0), reverse=True)

    return results

def get_stats() -> dict[str, int]:
    """Retrieves basic indexing statistical metrics.

    Returns:
        dict[str, int]: A dictionary detailing 'total_files' and 'total_chunks'.
    """
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT count(DISTINCT file_path) as files, count(id) as chunks FROM chunks")
    row = cursor.fetchone()
    db.close()
    return {"total_files": row["files"], "total_chunks": row["chunks"]}

def sanitize_fts_query(query: str) -> str:
    """
    Sanitize query for SQLite FTS5 to prevent syntax errors.
    Handles hyphenated terms, unmatched quotes, and dangling boolean operators.
    Adapted from NousResearch/hermes-agent (MIT).

    Args:
        query: Raw search query string from the user or internal call.

    Returns:
        A sanitized query string safe for FTS5 MATCH expressions.
    """
    # Remove quotes (FTS5 phrase search with unmatched quotes causes errors)
    query = re.sub(r'["\']', ' ', query)
    # Remove boolean operators that FTS5 would misinterpret
    query = re.sub(r'\b(AND|OR|NOT)\b', ' ', query, flags=re.IGNORECASE)
    # Convert hyphens to spaces (FTS5 treats hyphen as NOT operator)
    query = re.sub(r'-(?=\w)', ' ', query)
    # Normalize whitespace
    query = ' '.join(query.split())
    # Ensure non-empty
    return query if query.strip() else '*'


def search_fts(query: str, category: str | None = None, top_k: int = 10) -> list[dict[str, Any]]:
    """Search using BM25 full-text search for exact keyword matching.

    Complements vector search by catching exact terms (model names, IDs,
    ADR numbers) that semantic embeddings often miss.

    Args:
        query: Natural language search query (FTS5 will tokenize it).
        category: Optional category filter.
        top_k: Maximum number of results.

    Returns:
        List of dicts with keys: file_path, section_id, content,
        category, metadata, score. Score is BM25 rank (lower = better match,
        normalized to 0.0-1.0 range for RRF merging).
    """
    db = get_db()
    cursor = db.cursor()

    # FTS5 match query — sanitize before passing to MATCH
    # sanitize_fts_query handles quotes, hyphens, and boolean operators
    safe_query = sanitize_fts_query(query)

    sql = """
        SELECT c.id, c.file_path, c.section_id, c.content, c.category, c.metadata,
               rank AS bm25_score
        FROM chunks_fts fts
        JOIN chunks c ON c.rowid = fts.rowid
        WHERE chunks_fts MATCH ?
    """
    params: list[Any] = [safe_query]

    if category:
        sql += " AND c.category = ?"
        params.append(category)

    sql += " ORDER BY rank LIMIT ?"
    params.append(top_k)

    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()

    results = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        if meta.get("status", "active") in config.search.exclude_status:
            continue
        # BM25 rank is negative (more negative = better match).
        # Normalize to 0.0-1.0 where 1.0 is best.
        raw_score = abs(row["bm25_score"]) if row["bm25_score"] else 0
        # Cap at 25 for normalization (typical BM25 scores range 0-25)
        normalized = min(raw_score / 25.0, 1.0)
        results.append({
            "file_path": row["file_path"],
            "section_id": row["section_id"],
            "content": row["content"],
            "category": row["category"],
            "metadata": meta,
            "score": normalized,
        })

    db.close()
    return results

def check_freshness(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate search results with freshness status.

    Reads the source file for each result, computes current hash,
    compares against stored content_hash in chunk metadata.

    Returns results with added 'freshness' key: 'valid' | 'stale' | 'unknown'
    """
    for result in results:
        file_path = result.get("file_path", "")
        stored_hash = result.get("content_hash") or result.get("metadata", {}).get("content_hash")

        if not stored_hash:
            result["freshness"] = "unknown"
            continue

        full_path = os.path.join(config.palinode_dir, file_path) if not os.path.isabs(file_path) else file_path
        if not os.path.exists(full_path):
            result["freshness"] = "stale"
            continue

        try:
            with open(full_path, "r") as f:
                raw = f.read()
            # Hash body only (below frontmatter) to match what the indexer hashes.
            # Frontmatter changes (metadata edits) don't make content "stale".
            if raw.startswith("---"):
                end = raw.find("---", 3)
                body = raw[end + 3:].strip() if end != -1 else raw
            else:
                body = raw
            full_hash = hashlib.sha256(body.encode()).hexdigest()
            # Compare against both full (64-char) and truncated (16-char) hashes
            current_hash = full_hash if len(stored_hash) > 16 else full_hash[:16]
            result["freshness"] = "valid" if current_hash == stored_hash else "stale"
        except Exception:
            result["freshness"] = "unknown"

    return results

def search_hybrid(
    query_text: str,
    query_embedding: list[float],
    category: str | None = None,
    top_k: int = 10,
    threshold: float = 0.4,
    hybrid_weight: float = 0.5,
    date_after: str | None = None,
    date_before: str | None = None,
    context_entities: list[str] | None = None,
    include_daily: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid search combining semantic vectors and BM25 keyword matching.

    Uses Reciprocal Rank Fusion (RRF) to merge results from both search
    methods. This catches both semantic meaning AND exact terms.

    Args:
        query_text: The raw query string (for BM25).
        query_embedding: The embedded query vector (for cosine similarity).
        category: Optional category filter applied to both searches.
        top_k: Maximum results to return.
        threshold: Minimum score threshold (applied after RRF merging).
        hybrid_weight: Balance between vector and BM25.
            0.0 = vector only, 1.0 = BM25 only, 0.5 = equal weight.

    Returns:
        Merged and re-ranked list of result dicts, sorted by combined score.
    """
    # Get results from both search methods
    vec_results = search(query_embedding, category=category, top_k=top_k * 2, threshold=0.0)
    try:
        fts_results = search_fts(query_text, category=category, top_k=top_k * 2)
    except Exception:
        # FTS5 corrupted — rebuild and retry once
        import logging
        logging.getLogger("palinode.store").warning("FTS5 corrupted, rebuilding...")
        rebuild_fts()
        try:
            fts_results = search_fts(query_text, category=category, top_k=top_k * 2)
        except Exception:
            fts_results = []  # Give up on BM25, return vector-only

    # Reciprocal Rank Fusion (RRF)
    # Score = sum( 1 / (k + rank) ) for each result across both lists
    # k=60 is the standard RRF constant (dampens high-rank dominance)
    K = 60
    rrf_scores: dict[str, float] = {}
    result_map: dict[str, dict] = {}
    # Track raw cosine similarity from vector search before RRF normalization (#94)
    raw_cosine: dict[str, float] = {}

    # Score vector results
    vec_weight = 1.0 - hybrid_weight
    for rank, r in enumerate(vec_results):
        key = f"{r['file_path']}#{r.get('section_id', 'root')}"
        rrf_scores[key] = rrf_scores.get(key, 0) + vec_weight * (1.0 / (K + rank + 1))
        result_map[key] = r
        raw_cosine[key] = r.get("raw_score") or r.get("score", 0.0)

    # Score BM25 results
    bm25_weight = hybrid_weight
    for rank, r in enumerate(fts_results):
        key = f"{r['file_path']}#{r.get('section_id', 'root')}"
        rrf_scores[key] = rrf_scores.get(key, 0) + bm25_weight * (1.0 / (K + rank + 1))
        if key not in result_map:
            result_map[key] = r

    # Sort by RRF score descending, normalize to 0.0-1.0
    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)
    max_score = rrf_scores[sorted_keys[0]] if sorted_keys else 1.0

    if config.decay.enabled:
        # Apply temporal decay re-ranking if enabled
        for key in sorted_keys:
            r = result_map[key]
            meta = r.get("metadata", {})
            importance = meta.get("importance", 0.5)
            last_recalled = meta.get("last_recalled")
            recall_count = meta.get("recall_count", 0)
            memory_type = meta.get("memory_type", r.get("category", "general"))
            
            # Use un-normalized RRF score for decay calculation base to match expected baseline scale, 
            # or normalized if you prefer. We'll use normalized here for bounding to 1.0 max.
            norm_score = rrf_scores[key] / max_score
            r["score"] = score_with_decay(
                base_score=norm_score,
                importance=importance,
                last_recalled_date=last_recalled,
                recall_count=recall_count,
                memory_type=memory_type,
            )
        
        # Re-sort after applying decay
        sorted_keys = sorted(rrf_scores.keys(), key=lambda k: result_map[k].get("score", 0.0), reverse=True)
    else:
        for key in sorted_keys:
            result_map[key]["score"] = rrf_scores[key] / max_score

    # Ambient context boost: boost results matching caller's project context
    if context_entities and config.context.enabled and config.context.boost != 1.0:
        context_files: set[str] = set()
        for entity in context_entities:
            for row in get_entity_files(entity):
                context_files.add(row["file_path"])
        if context_files:
            for key in sorted_keys:
                r = result_map[key]
                if r["file_path"] in context_files:
                    r["score"] = r.get("score", 0) * config.context.boost
            # Re-sort after context boost
            sorted_keys = sorted(
                sorted_keys, key=lambda k: result_map[k].get("score", 0.0), reverse=True
            )

    # Issue #93: Penalize daily/ files to prevent session notes from dominating results
    penalty = config.search.daily_penalty
    if not include_daily and penalty != 1.0:
        needs_resort = False
        for key in sorted_keys:
            r = result_map[key]
            if _is_daily_file(r["file_path"]):
                r["score"] = r.get("score", 0) * penalty
                needs_resort = True
        if needs_resort:
            sorted_keys = sorted(
                sorted_keys, key=lambda k: result_map[k].get("score", 0.0), reverse=True
            )

    # Deduplicate by file: suppress additional chunks that score far below
    # the file's best chunk (#91). A second chunk from the same file is kept
    # only if its score is within dedup_score_gap of the file's best.
    file_best: dict[str, float] = {}
    deduped_keys: list[str] = []
    gap = config.search.dedup_score_gap
    for key in sorted_keys:
        r = result_map[key]
        fp = r["file_path"]
        score = r.get("score", 0.0)
        if fp not in file_best:
            file_best[fp] = score
            deduped_keys.append(key)
        elif file_best[fp] - score <= gap:
            deduped_keys.append(key)

    merged = []
    for key in deduped_keys[:top_k]:
        result = result_map[key]
        if result.get("score", 0) >= threshold:
            # Attach raw cosine similarity from vector search (#94).
            # BM25-only results (no vector match) get raw_score=None.
            result["raw_score"] = raw_cosine.get(key)
            merged.append(result)

    if date_after or date_before:
        filtered = []
        for r in merged:
            meta = r.get("metadata", {})
            updated = meta.get("last_updated", r.get("created_at", ""))
            if not updated:
                filtered.append(r)
                continue
            if date_after and updated < date_after:
                continue
            if date_before and updated > date_before:
                continue
            filtered.append(r)
        merged = filtered

    if merged:
        merged = check_freshness(merged)

    # Record retrieval for frequency tracking (batch update, non-blocking)
    if merged:
        now = _utc_now().isoformat()
        db = get_db()
        try:
            for r in merged[:top_k]:
                chunk_id = r.get("id")  # Using id since chunks table PK is 'id'
                if chunk_id:
                    db.execute("""
                        UPDATE chunks 
                        SET last_recalled = ?, recall_count = recall_count + 1
                        WHERE id = ?
                    """, (now, chunk_id))
            db.commit()
        except Exception:
            pass  # Never block search for stats update
        finally:
            db.close()

    return merged

def get_entity_files(entity_ref: str) -> list[dict[str, Any]]:
    """Get all files that reference a specific entity."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT file_path, category, last_seen FROM entities WHERE entity_ref = ? ORDER BY last_seen DESC",
        (entity_ref,)
    )
    results = [{"file_path": row[0], "category": row[1], "last_seen": row[2]} for row in cursor.fetchall()]
    db.close()
    return results

def get_entity_graph(entity_ref: str, depth: int = 1) -> dict[str, list[str]]:
    """Get the entity graph — which entities appear together."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT DISTINCT e2.entity_ref 
        FROM entities e1
        JOIN entities e2 ON e1.file_path = e2.file_path
        WHERE e1.entity_ref = ? AND e2.entity_ref != ?
    """, (entity_ref, entity_ref))
    
    co_occurring = [row[0] for row in cursor.fetchall()]
    db.close()
    return {entity_ref: co_occurring}


def upsert_entities(file_path: str, metadata: dict[str, Any]) -> None:
    """Extract and index entities from file metadata."""
    entities = metadata.get("entities", [])
    if not entities:
        return
        
    db = get_db()
    cursor = db.cursor()
    category = metadata.get("category", "")
    now = _utc_now().isoformat().replace("+00:00", "Z")
    
    for entity_ref in entities:
        cursor.execute("""
            INSERT OR REPLACE INTO entities (entity_ref, file_path, category, last_seen)
            VALUES (?, ?, ?, ?)
        """, (entity_ref, file_path, category, now))
    
    db.commit()
    db.close()


def detect_entities_in_text(text: str) -> list[str]:
    """Detect known entity refs mentioned in text.
    
    Scans for entity names from the entities index.
    Returns list of entity_ref strings (e.g., ['person/alice', 'project/alpha']).
    """
    db = get_db()
    known_entities = db.execute(
        "SELECT DISTINCT entity_ref FROM entities"
    ).fetchall()
    db.close()
    
    text_lower = text.lower()
    detected = []
    
    for (entity_ref,) in known_entities:
        # Extract the name part: "person/alice" -> "alice"
        name = entity_ref.split("/")[-1].replace("-", " ")
        if name in text_lower and len(name) > 2:
            detected.append(entity_ref)
    
    return list(set(detected))


def search_associative(
    query_text: str,
    seed_entities: list[str],
    top_k: int = 5,
    activation_threshold: float = 0.3,
    decay_factor: float = 0.5,
    max_hops: int = 2,
) -> list[dict[str, Any]]:
    """Associative recall via entity graph spreading activation.
    
    Given seed entities detected in the user's message, traverses
    the entity co-occurrence graph to find related memories — even
    those not directly matching the query.
    """
    if not seed_entities:
        return []
    
    db = get_db()
    
    # Step 1: Initialize activation scores
    activation: dict[str, float] = {}  # entity_ref -> activation score
    for entity in seed_entities:
        activation[entity] = 1.0
    
    # Step 2: Spread activation for max_hops
    for hop in range(max_hops):
        current_decay = decay_factor ** (hop + 1)
        new_activation: dict[str, float] = dict(activation)
        
        # Find all entities that co-occur with currently-activated entities
        activated_list = list(activation.keys())
        if not activated_list:
            break
        
        ph, activated_params = _parameterize_in_clause(activated_list)
        rows = db.execute(f"""
            SELECT DISTINCT e2.entity_ref, COUNT(*) as co_count
            FROM entities e1
            JOIN entities e2 ON e1.file_path = e2.file_path 
                AND e1.entity_ref != e2.entity_ref
            WHERE e1.entity_ref IN ({ph})
            GROUP BY e2.entity_ref
            ORDER BY co_count DESC
            LIMIT 20
        """, activated_params).fetchall()
        
        for row in rows:
            neighbor = row[0]
            co_count = row[1]
            # Activation = max source activation × decay × co-occurrence bonus
            source_act = max(activation.get(e, 0) for e in activated_list)
            new_act = source_act * current_decay * (1 + 0.1 * min(co_count, 5))
            
            if new_act > new_activation.get(neighbor, 0):
                new_activation[neighbor] = new_act
        
        activation = new_activation
    
    # Step 3: Collect files from activated entities (above threshold)
    activated_entities = [
        e for e, score in activation.items()
        if score >= activation_threshold and e not in seed_entities
    ]
    
    if not activated_entities:
        db.close()
        return []
    
    ph, entity_params = _parameterize_in_clause(activated_entities)
    files = db.execute(f"""
        SELECT DISTINCT e.file_path, MAX(?) as activation_score
        FROM entities e
        WHERE e.entity_ref IN ({ph})
        GROUP BY e.file_path
    """, (max(activation.values()), *entity_params)).fetchall()
    
    db.close()
    
    if not files:
        return []
    
    # Step 4: Get content for activated files and build results
    results = []
    seen_files = set()
    
    for file_row in files[:top_k * 2]:
        fp = file_row[0]
        if fp in seen_files or not os.path.exists(fp):
            continue
        seen_files.add(fp)
        
        # Get the most relevant chunk from this file
        try:
            with open(fp) as f:
                content = f.read()[:1500]
        except Exception:
            continue
        
        rel_path = fp.replace(config.memory_dir + "/", "").lstrip("/")
        entity_score = activation.get(
            next((e for e in activated_entities if e.split("/")[-1] in fp), ""), 0.5
        )
        
        results.append({
            "file_path": fp,
            "section_id": "root",
            "content": content[:700],
            "category": rel_path.split("/")[0] if "/" in rel_path else "general",
            "metadata": {},
            "score": min(entity_score, 1.0),
            "recall_type": "associative",
        })
    
    return sorted(results, key=lambda r: r["score"], reverse=True)[:top_k]


def add_trigger(
    trigger_id: str,
    description: str, 
    memory_file: str,
    embedding: list[float],
    threshold: float = 0.75,
    cooldown_hours: int = 24,
) -> None:
    """Register a prospective trigger.
    
    Args:
        trigger_id: Unique ID for this trigger.
        description: What context should fire this (e.g., "LoRA training").
        memory_file: Relative path to inject when fired.
        embedding: Pre-computed embedding of the description.
        threshold: Cosine similarity threshold to fire (0.0-1.0).
        cooldown_hours: Hours between refires.
    """
    db = get_db()
    now = _utc_now().isoformat().replace("+00:00", "Z")
    db.execute("""
        INSERT OR REPLACE INTO triggers (id, description, memory_file, threshold, cooldown_hours, created_at, enabled, fire_count)
        VALUES (?, ?, ?, ?, ?, ?, 1, 0)
    """, (trigger_id, description, memory_file, threshold, cooldown_hours, now))
    
    emb_json = json.dumps(embedding)
    db.execute("""
        INSERT OR REPLACE INTO triggers_vec (id, embedding)
        VALUES (?, ?)
    """, (trigger_id, emb_json))
    
    db.commit()
    db.close()

def check_triggers(
    query_embedding: list[float],
    cooldown_bypass: bool = False,
) -> list[dict[str, Any]]:
    """Check if any triggers match the current context.
    
    Args:
        query_embedding: Embedding of the current user message.
        cooldown_bypass: If True, ignore cooldown (useful for testing).
    
    Returns:
        List of fired trigger dicts with memory_file and trigger info.
    """
    db = get_db()
    query_vec_json = json.dumps(query_embedding)
    
    rows = db.execute("""
        SELECT t.*, v.distance
        FROM triggers_vec v
        JOIN triggers t ON v.id = t.id
        WHERE v.embedding MATCH ? AND k = 10
    """, (query_vec_json,)).fetchall()
    
    results = []
    now = _utc_now()
    
    for row in rows:
        if not row["enabled"]:
            continue
            
        dist = row["distance"] or 0
        score = 1.0 - ((dist ** 2) / 2.0)
        
        if score >= row["threshold"]:
            if not cooldown_bypass and row["last_fired"]:
                last_fired_date = datetime.fromisoformat(row["last_fired"][:19])
                hours_since = (now - last_fired_date).total_seconds() / 3600
                if hours_since < row["cooldown_hours"]:
                    continue  # In cooldown
            
            results.append({
                "id": row["id"],
                "description": row["description"],
                "memory_file": row["memory_file"],
                "score": score,
            })
            
            # Since it fired, record it
            update_trigger_fired(row["id"])
            
    db.close()
    return results

def list_triggers() -> list[dict]:
    """Return all registered triggers with their stats."""
    db = get_db()
    rows = db.execute("SELECT id, description, memory_file, threshold, cooldown_hours, last_fired, fire_count, created_at, enabled FROM triggers ORDER BY created_at DESC").fetchall()
    db.close()
    return [dict(r) for r in rows]

def delete_trigger(trigger_id: str) -> None:
    """Remove a trigger."""
    db = get_db()
    db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
    db.execute("DELETE FROM triggers_vec WHERE id = ?", (trigger_id,))
    db.commit()
    db.close()

def update_trigger_fired(trigger_id: str) -> None:
    """Record that a trigger fired — update last_fired and fire_count."""
    db = get_db()
    now = _utc_now().isoformat().replace("+00:00", "Z")
    db.execute("""
        UPDATE triggers 
        SET last_fired = ?, fire_count = fire_count + 1
        WHERE id = ?
    """, (now, trigger_id))
    db.commit()
    db.close()


def score_with_decay(
    base_score: float,
    importance: float,
    last_recalled_date: str | None,
    recall_count: int,
    memory_type: str = "general",
) -> float:
    """Apply temporal decay to a search score.
    
    Formula: Score = base × importance × e^(-Δt/τ) × (1 + log(1 + freq))
    
    Args:
        base_score: Original similarity score (0.0-1.0).
        importance: LLM-rated importance (0.0-1.0, default 0.5).
        last_recalled_date: ISO date of last retrieval (None = never recalled).
        recall_count: Number of times this chunk was returned in search.
        memory_type: Type for selecting τ constant.
    
    Returns:
        Adjusted score after decay (still 0.0-1.0 range).
    """
    import math
    from datetime import datetime
    
    cfg = config.decay
    TAU = {
        "critical": cfg.tau_critical, "decisions": cfg.tau_decisions, "insights": cfg.tau_insights,
        "general": cfg.tau_general, "status": cfg.tau_status, "ephemeral": cfg.tau_ephemeral,
    }
    tau = TAU.get(memory_type, cfg.tau_general)
    
    if last_recalled_date:
        try:
            last = datetime.fromisoformat(last_recalled_date[:10])
            delta_days = (_utc_now() - last).days
        except Exception:
            delta_days = 0
    else:
        delta_days = 30  # Default decay for never-recalled memories
    
    decay = math.exp(-delta_days / tau)
    frequency_boost = 1 + math.log1p(recall_count)
    
    return min(base_score * importance * decay * frequency_boost, 1.0)
