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
import struct
import hashlib
from typing import Any, Collection, Sequence
from datetime import UTC, datetime, timedelta
from palinode.core.config import config
from palinode.core import parser as _parser
# The hybrid-search scoring pipeline + its pure decay/predicate helpers live in
# ranker.py. Re-exported here so `store.effective_importance`,
# `store._is_daily_file`, etc. keep resolving for internal callers and tests.
from palinode.core.ranker import (  # noqa: F401
    _is_daily_file,
    _priority_value,
    effective_importance,
    rank_hybrid,
    score_with_decay,
)

_store_logger = __import__('logging').getLogger("palinode.store")

# Module-level flag: once we've verified the DB state on first connect, skip
# the check on subsequent calls (it's expensive — recursive glob of memory_dir).
_db_checked: bool = False

# ADR-015 §2.3: machine/monitor writes carry ``metadata.kind: telemetry``
# in their frontmatter (which flattens to a top-level ``kind`` field — see
# save_api, where ``req.metadata`` is merged into the top level). Such memories
# are HARD-EXCLUDED from default semantic recall (§6 Q3) so monitoring churn
# does not pollute human recall, but remain retrievable when a caller passes an
# explicit override. The exclusion lives in the shared store layer so all
# surfaces (API/MCP/CLI/plugin) inherit it (ADR-010 parity), mirroring.
DEFAULT_RECALL_EXCLUDED_KINDS: tuple[str, ...] = ("telemetry",)


def _excluded_by_kind(
    meta: dict[str, Any], kind_exclude_list: Sequence[str] | None
) -> bool:
    """Return True if this chunk's ``kind`` is recall-excluded (ADR-015 §2.3).

    ``kind_exclude_list`` is the active exclusion set. ``None`` means "use the
    default" (``DEFAULT_RECALL_EXCLUDED_KINDS``); an explicit empty sequence
    means "exclude nothing" — i.e. the caller asked to include telemetry.
    """
    excluded = DEFAULT_RECALL_EXCLUDED_KINDS if kind_exclude_list is None else kind_exclude_list
    if not excluded:
        return False
    return meta.get("kind") in excluded

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

def _glob_md_files(directory: str):
    """Yield .md file paths under *directory* (recursive)."""
    import glob as _glob
    yield from _glob.iglob(os.path.join(directory, "**", "*.md"), recursive=True)


def _ensure_db() -> None:
    """Disambiguate first-run from misconfiguration before SQLite auto-creates.

    Called once per process on the first ``get_db()`` invocation.  Sets the
    module-level ``_db_checked`` flag so subsequent calls are free.

    Three cases:
    - DB already exists -> nothing to do; just connect normally.
    - DB missing + memory_dir has 0 .md files (or PALINODE_ALLOW_FRESH_DB set)
      -> legitimate first run; log clearly and allow creation.
    - DB missing + memory_dir has .md files -> misconfiguration; raise
      RuntimeError with actionable guidance for the operator.
    """
    global _db_checked
    if _db_checked:
        return

    _db_checked = True

    db_path = config.db_path
    if os.path.exists(db_path):
        # Normal operation -- DB is present, nothing to check.
        return

    memory_dir = config.memory_dir
    allow_fresh = os.environ.get("PALINODE_ALLOW_FRESH_DB")

    # Count .md files in memory_dir (recursive).  If the directory doesn't
    # exist yet we treat that as 0 files (brand-new install).
    try:
        md_count = sum(1 for _ in _glob_md_files(memory_dir))
    except (OSError, ValueError):
        md_count = 0

    if md_count == 0 or allow_fresh:
        _store_logger.info(
            "palinode.store: First run detected -- creating fresh database at %s "
            "(memory_dir has %d .md file(s))",
            db_path,
            md_count,
        )
        return

    # Memory files exist but no DB -- almost certainly a misconfiguration.
    raise RuntimeError(
        f"palinode found {md_count} memory file(s) at {memory_dir} "
        f"but no database at {db_path}.\n"
        "This usually means PALINODE_DIR or db_path is misconfigured.\n\n"
        f"  - To verify:  ls {memory_dir}/*.md\n"
        "  - If you intended to start fresh, set PALINODE_ALLOW_FRESH_DB=1\n"
        "  - Otherwise, check that db_path in palinode.config.yaml matches "
        "your memory_dir"
    )


def get_db() -> sqlite3.Connection:
    """Gets an active connection to the SQLite database with vec extension active.

    On the first call per process, verifies that the DB state is consistent
    with the memory_dir contents -- raises RuntimeError on detected
    misconfiguration (DB missing but .md files present).

    Returns:
        sqlite3.Connection: Database connection featuring vec.

    Raises:
        RuntimeError: If the database is missing but memory_dir contains .md
            files, indicating a likely misconfiguration rather than first run.
    """
    _ensure_db()
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

    # ADR-007 §3.2: session-deduplicated importance nudge. One row per
    # (chunk, session) that has already reinforced importance, so a second
    # explicit hit on the same memory in the same session does NOT re-nudge.
    # recall_count still increments on every hit (raw frequency); only the
    # importance nudge is deduplicated. Rows are cheap and never read on the
    # hot search path beyond the INSERT OR IGNORE membership test.
    db.execute("""
        CREATE TABLE IF NOT EXISTS recall_nudges (
            chunk_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            nudged_at TEXT,
            PRIMARY KEY (chunk_id, session_id)
        )
    """)

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


def fts5_delete_chunk(cursor: sqlite3.Cursor, chunk_id: str) -> None:
    """Remove a chunk's row from the external-content FTS5 index ``chunks_fts``.

    ``chunks_fts`` is declared ``content=chunks`` (an *external-content* FTS5
    table). Such a table cannot reliably drop its inverted-index tokens via a
    bare per-rowid ``DELETE`` — FTS5 has to re-derive the row's tokens from its
    column values to remove them, and a plain delete can leave those tokens
    orphaned (``count(chunks_fts) > count(chunks)`` until the next
    ``rebuild_fts()``, which silently corrupts BM25 keyword recall). The
    sanctioned removal is the special ``'delete'`` command, fed the column
    values exactly as they were indexed (#439).

    MUST be called while the source ``chunks`` row still exists: the values are
    read from it. Best-effort — a missing source row or FTS mismatch is a no-op
    rather than an error (the periodic rebuild is the backstop).
    """
    row = cursor.execute(
        "SELECT rowid, content, file_path, category FROM chunks WHERE id = ?",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return
    # Column order here must match the FTS5 column declaration
    # (content, file_path, category) — see the chunks_fts CREATE above.
    cursor.execute(
        """
        INSERT INTO chunks_fts(chunks_fts, rowid, content, file_path, category)
        VALUES ('delete', ?, ?, ?, ?)
        """,
        (row[0], row[1], row[2], row[3]),
    )


def upsert_chunks(
    chunks_data: list[dict[str, Any]], skip_unchanged: bool = True
) -> dict[str, Any]:
    """Update or insert new chunks into the document and vector indices.

    Args:
        chunks_data (list[dict[str, Any]]): A list including dictionaries of id, 
            file_path, content, section_id, embedding, and optional keys category, metadata, 
            created_at, last_updated.
        skip_unchanged (bool): If True, skip re-embedding chunks whose content hash matches.

    Returns:
        dict with keys:
            ``written`` (int): chunks actually upserted (excluding unchanged).
            ``vec_ok`` (bool): True iff every chunks_vec write succeeded.
            ``fts_ok`` (bool): True iff every FTS5 sync write succeeded.

        Callers that need per-index health (e.g. ``index_file``) read
        ``vec_ok`` / ``fts_ok`` to surface failures in the API response (#385).
    """
    db = get_db()
    cursor = db.cursor()
    written = 0
    vec_ok = True
    fts_ok = True

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

        # Write the canonical chunks row.
        metadata_json = json.dumps(chunk.get("metadata", {}), default=str)
        # H2: INSERT OR REPLACE deletes + re-inserts the row, reverting the
        # recall columns (importance, recall_count, last_recalled) — which are
        # NOT in this column list — to their schema defaults on every re-index
        # (any content_hash change: consolidation UPDATE/MERGE, manual edit,
        # sticky-frontmatter rewrite). That silently wiped the ADR-007
        # reinforcement signal the ranker now reads from these columns.
        # ON CONFLICT(id) DO UPDATE refreshes only the content columns and
        # leaves the accumulated recall signal intact; a brand-new row still
        # gets the schema defaults.
        cursor.execute(
            """
            INSERT INTO chunks
            (id, file_path, section_id, category, content, metadata,
             created_at, last_updated, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                file_path = excluded.file_path,
                section_id = excluded.section_id,
                category = excluded.category,
                content = excluded.content,
                metadata = excluded.metadata,
                created_at = excluded.created_at,
                last_updated = excluded.last_updated,
                content_hash = excluded.content_hash
            """,
            (
                chunk["id"], chunk["file_path"], chunk["section_id"],
                chunk.get("category", ""), chunk["content"], metadata_json,
                chunk.get("created_at"), chunk.get("last_updated"), content_hash,
            ),
        )
        
        # --- vec0 index -------------------------------------------------------
        if not chunk["embedding"]:
            # Deferred/keyword-only row: no vector yet. Write chunks + FTS
            # only — the absent chunks_vec row is the exact signal the
            # index_file re-embed branch keys on, so the row converges to
            # fully-embedded once the embedder is reachable. Deliberate skip,
            # not a write failure: vec_ok is untouched.
            _store_logger.debug(
                "palinode.store: empty embedding for %r — chunks+FTS only, "
                "vec write deferred", chunk["id"],
            )
        else:
            # Pre-flight DELETE: remove the existing row if present before
            # INSERT. Failure here is not an error — the row may simply not
            # exist yet.
            emb_json = json.dumps(chunk["embedding"])
            try:
                cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (chunk["id"],))
            except Exception as _del_exc:
                _store_logger.debug(
                    "palinode.store: chunks_vec pre-INSERT DELETE skipped for %r: %s",
                    chunk["id"], _del_exc,
                )

            # Primary INSERT.  On UNIQUE collision (DELETE silently failed
            # above), fall back to a forced DELETE + retry.  Log at error on
            # total failure because a missing vec0 row means this chunk is
            # invisible to vector search.
            try:
                cursor.execute(
                    "INSERT INTO chunks_vec (id, embedding) VALUES (?, ?)",
                    (chunk["id"], emb_json),
                )
            except Exception as _ins_exc:
                _store_logger.debug(
                    "palinode.store: chunks_vec INSERT collided for %r — forcing replace: %s",
                    chunk["id"], _ins_exc,
                )
                try:
                    cursor.execute("DELETE FROM chunks_vec WHERE id = ?", (chunk["id"],))
                    cursor.execute(
                        "INSERT INTO chunks_vec (id, embedding) VALUES (?, ?)",
                        (chunk["id"], emb_json),
                    )
                except Exception as _retry_exc:
                    _store_logger.error(
                        "palinode.store: chunks_vec write failed for %r "
                        "(file=%r, content_hash=%s) — chunk absent from vector index: %s",
                        chunk["id"],
                        chunk.get("file_path"),
                        content_hash[:12],
                        _retry_exc,
                        exc_info=True,
                    )
                    vec_ok = False

        # --- FTS5 index -------------------------------------------------------
        # Best-effort: FTS5 external-content tables can get out of sync during
        # bulk writes.  Log at warning (not error) because the periodic rebuild
        # recovers FTS5; a vec0 miss is permanent until re-index.
        #
        # Pattern: always attempt INSERT directly.  If it fails with UNIQUE
        # (rowid already present from a prior sync), fall back to DELETE +
        # re-INSERT.  We never attempt DELETE first on a fresh row — doing so
        # against an empty FTS5 table with sqlite-vec loaded raises
        # "database disk image is malformed" (a vec extension side effect on
        # zero-row FTS5 tables).
        try:
            cursor.execute(
                """
                INSERT INTO chunks_fts(rowid, content, file_path, category)
                SELECT rowid, content, file_path, category
                FROM chunks WHERE id = ?
                """,
                (chunk["id"],),
            )
        except Exception as _fts_exc:
            # UNIQUE: a physical FTS5 row already exists — delete and retry.
            # We only reach here after the INSERT collided, so the row provably
            # exists — the "never DELETE-first on an empty FTS5 table" guard
            # (see the comment above) is preserved.
            try:
                fts5_delete_chunk(cursor, chunk["id"])
                cursor.execute(
                    """
                    INSERT INTO chunks_fts(rowid, content, file_path, category)
                    SELECT rowid, content, file_path, category
                    FROM chunks WHERE id = ?
                    """,
                    (chunk["id"],),
                )
            except Exception as _fts_retry_exc:
                _store_logger.warning(
                    "palinode.store: FTS5 sync failed for %r "
                    "(file=%r) — periodic rebuild will recover: %s",
                    chunk["id"],
                    chunk.get("file_path"),
                    _fts_retry_exc,
                )
                fts_ok = False

        written += 1

    db.commit()
    db.close()
    return {"written": written, "vec_ok": vec_ok, "fts_ok": fts_ok}

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
        # Clean FTS5 index before deleting the source rows (best-effort).
        # Must run first: the sanctioned 'delete' reads column values off the
        # still-present chunks row to re-derive the tokens to remove.
        for chunk_id in ids:
            try:
                fts5_delete_chunk(cursor, chunk_id)
            except Exception:
                pass  # FTS5 may be out of sync — periodic rebuild handles this
        
        placeholders, params = _parameterize_in_clause(ids)
        # B608 rationale - placeholders is "?,?,..." from _parameterize_in_clause; values bound via params
        cursor.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", params)  # nosec B608
        cursor.execute(f"DELETE FROM chunks_vec WHERE id IN ({placeholders})", params)  # nosec B608
        
    cursor.execute("DELETE FROM entities WHERE file_path = ?", (file_path,))
    db.commit()
    db.close()

def gc_orphaned_chunks(valid_paths: Collection[str]) -> tuple[int, int]:
    """Delete indexed chunks whose source file is no longer on disk.

    Args:
        valid_paths: The complete set of valid markdown paths from the current
            filesystem walk.

    Returns:
        ``(paths_removed, chunks_removed)``.
    """
    valid_path_set = set(valid_paths)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT file_path, COUNT(*) AS chunk_count FROM chunks GROUP BY file_path"
        ).fetchall()
    finally:
        db.close()

    orphan_rows = [
        (row["file_path"], row["chunk_count"])
        for row in rows
        if row["file_path"] not in valid_path_set
    ]
    for file_path, _chunk_count in orphan_rows:
        delete_file_chunks(file_path)

    return len(orphan_rows), sum(chunk_count for _file_path, chunk_count in orphan_rows)

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


def search(query_embedding: list[float], category: str | None = None,
           status_exclude_list: list[str] | None = None, top_k: int = 10, threshold: float = 0.6,
           date_after: str | None = None, date_before: str | None = None,
           context_entities: list[str] | None = None,
           include_daily: bool = False, record_access: bool = True,
           kind_exclude_list: Sequence[str] | None = None,
           mode: str = "explicit", session_id: str | None = None) -> list[dict[str, Any]]:
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
            for ADR-008 ambient context boost. Matching results get score * config.context.boost.
        include_daily (bool): If True, skip the daily/ penalty (search daily notes at full rank).
        kind_exclude_list (Sequence[str] | None): ADR-015 §2.3 recall-exclusion.
            None → exclude DEFAULT_RECALL_EXCLUDED_KINDS (telemetry). Empty
            sequence → exclude nothing (include telemetry).

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

    # use try/finally so the connection is closed on all paths (exception
    # during row processing previously left it open).
    db = get_db()
    try:
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
    finally:
        db.close()

    results = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        if meta.get("status", "active") in status_exclude_list:
            continue
        # ADR-015 §2.3: hard-exclude telemetry/machine writes from default recall.
        if _excluded_by_kind(meta, kind_exclude_list):
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

        result_entry: dict[str, Any] = {
            "id": row["id"] if "id" in row.keys() else None,
            "file_path": row["file_path"],
            "section_id": row["section_id"],
            "content": row["content"],
            "category": row["category"],
            "metadata": meta,
            "content_hash": row["content_hash"] if "content_hash" in row.keys() else None,
            # ADR-007 §3.4 decay-on-read reads the access-metadata *columns*
            # (chunks.importance / chunks.last_recalled), not frontmatter.
            "importance": row["importance"] if "importance" in row.keys() else None,
            "last_recalled": row["last_recalled"] if "last_recalled" in row.keys() else None,
            "recall_count": row["recall_count"] if "recall_count" in row.keys() else 0,
            "score": score,
            "raw_score": score,
        }
        # surface confidence as top-level key when present in frontmatter.
        if "confidence" in meta:
            result_entry["confidence"] = meta["confidence"]
        results.append(result_entry)
        if len(results) >= top_k:
            break

    # ADR-008: Ambient context boost (same logic as search_hybrid)
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

    # Issue Penalize daily/ files to prevent session notes from dominating results
    penalty = config.search.daily_penalty
    if not include_daily and penalty != 1.0:
        needs_resort = False
        for r in results:
            if _is_daily_file(r["file_path"]):
                r["score"] = r.get("score", 0) * penalty
                needs_resort = True
        if needs_resort:
            results.sort(key=lambda r: r.get("score", 0.0), reverse=True)

    # Record retrieval access metadata (ADR-006/007): batched, resilient.
    # Suppressed when called as the inner vector pass of search_hybrid, which
    # records recall against its final merged hit set instead (avoids double-
    # counting intermediate candidates).
    if record_access and results:
        record_recall([r.get("id") for r in results], mode=mode, session_id=session_id)

    return results


def search_internal(
    query_embedding: list[float],
    category: str | None = None,
    status_exclude_list: list[str] | None = None,
    top_k: int = 10,
    threshold: float = 0.6,
    date_after: str | None = None,
    date_before: str | None = None,
    context_entities: list[str] | None = None,
    include_daily: bool = False,
    kind_exclude_list: "Sequence[str] | None" = None,
) -> list[dict[str, Any]]:
    """Vector search for internal / maintenance callers — recall is never recorded.

    Thin wrapper around :func:`search` with ``record_access`` hard-set to
    ``False``.  Use this for any internal candidate lookup (consolidation dedup,
    orphan repair, cluster/topic maintenance) so maintenance scans cannot
    accidentally inflate ``recall_count`` or nudge ``importance`` (ADR-015 H1,
    #481).

    The ``record_access`` parameter is intentionally absent from this signature:
    callers cannot override it.  If you need the full ``search()`` API (including
    ``mode`` / ``session_id`` for user-facing recall), call :func:`search` directly
    and set ``record_access=True`` explicitly.
    """
    return search(
        query_embedding=query_embedding,
        category=category,
        status_exclude_list=status_exclude_list,
        top_k=top_k,
        threshold=threshold,
        date_after=date_after,
        date_before=date_before,
        context_entities=context_entities,
        include_daily=include_daily,
        kind_exclude_list=kind_exclude_list,
        record_access=False,  # hard-set; cannot be overridden via this API
    )


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


def search_fts(query: str, category: str | None = None, top_k: int = 10,
               kind_exclude_list: Sequence[str] | None = None) -> list[dict[str, Any]]:
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
    # use try/finally so the connection is closed on all paths (same
    # hygiene fix as search()).
    db = get_db()
    try:
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
    finally:
        db.close()

    results = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        if meta.get("status", "active") in config.search.exclude_status:
            continue
        # ADR-015 §2.3: hard-exclude telemetry/machine writes from default recall.
        if _excluded_by_kind(meta, kind_exclude_list):
            continue
        # BM25 rank is negative (more negative = better match).
        # Normalize to 0.0-1.0 where 1.0 is best.
        raw_score = abs(row["bm25_score"]) if row["bm25_score"] else 0
        # Cap at 25 for normalization (typical BM25 scores range 0-25)
        normalized = min(raw_score / 25.0, 1.0)
        results.append({
            "id": row["id"],
            "file_path": row["file_path"],
            "section_id": row["section_id"],
            "content": row["content"],
            "category": row["category"],
            "metadata": meta,
            "score": normalized,
        })

    return results

def check_freshness(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate search results with freshness status.

    Reads the source file for each result, parses it into sections (matching
    the indexer's per-section hashing), and compares the section's current hash
    to the stored content_hash for that chunk.

    The stored content_hash is computed over a single section's content (see
    ``palinode/indexer/index_file.py``), so comparing a whole-file body hash
    against it always mismatched for multi-section files (#203).  The fix is to
    locate the matching section by section_id, hash only that section, and
    compare.

    Returns results with added 'freshness' key: 'valid' | 'stale' | 'unknown'
    """
    # Cache parsed sections per file path to avoid re-reading the same file
    # once per result when multiple chunks come from the same file.
    _sections_cache: dict[str, list[dict[str, str]]] = {}

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
            if full_path not in _sections_cache:
                with open(full_path, "r") as f:
                    raw = f.read()
                _, sections = _parser.parse_markdown(raw)
                _sections_cache[full_path] = sections

            sections = _sections_cache[full_path]
            section_id = result.get("section_id", "root")

            # Find the section whose section_id matches this chunk.
            matching = next((s for s in sections if s["section_id"] == section_id), None)
            if matching is None:
                # Section no longer exists in the file — content was removed.
                result["freshness"] = "stale"
                continue

            # Hash the section content exactly as the indexer does (fix).
            full_hash = hashlib.sha256(matching["content"].encode()).hexdigest()
            # Support both full (64-char) and legacy truncated (16-char) hashes.
            current_hash = full_hash if len(stored_hash) > 16 else full_hash[:16]
            result["freshness"] = "valid" if current_hash == stored_hash else "stale"
        except Exception:
            result["freshness"] = "unknown"

    return results


def list_recent(
    types: list[str] | None = None,
    category: str | None = None,
    date_after: str | None = None,
    date_before: str | None = None,
    limit: int = 10,
    kind_exclude_list: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the most recently created/updated chunks (no semantic ranking).

    Backs the empty-query path on /search (#141). Orders by created_at desc.
    Push category and type filters into SQL via ``json_extract`` so the
    "first 100 chunks happen to all be untyped" failure mode doesn't bite
    us on databases where typed memories are a minority of the corpus.

    Args:
        types: Filter by frontmatter `type` field (Decision, Insight, etc.).
            None or empty list = no filter.
        category: Filter by directory category (people, projects, etc.).
        date_after/date_before: ISO-8601 inclusive bounds against
            `metadata.last_updated` (falls back to `chunks.created_at`).
            Compared as strings — fine for ISO-8601 with consistent zero-padding,
            including across `T`/space and `Z`/`+00:00` separator variants.
        limit: Maximum results to return.

    Returns:
        Result dicts with the same shape as `search()`, except `score` is
        always 1.0 (no semantic ranking applied) and `raw_score` is unset.
    """
    sql = "SELECT * FROM chunks"
    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if types:
        # SQLite parameter binding doesn't expand IN-lists, so build placeholders
        # of the right count. json_extract pulls the `type` from the JSON blob.
        placeholders = ", ".join("?" for _ in types)
        clauses.append(f"json_extract(metadata, '$.type') IN ({placeholders})")
        params.extend(types)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    # Over-fetch lightly so the per-row status/date filter has room. The cap
    # is per-row work, not per-row I/O, so a generous ceiling is cheap.
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(limit * 5, 50))

    db = get_db()
    cursor = db.cursor()
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        if meta.get("status", "active") in config.search.exclude_status:
            continue
        # ADR-015 §2.3: hard-exclude telemetry/machine writes from default recall.
        if _excluded_by_kind(meta, kind_exclude_list):
            continue
        if date_after or date_before:
            updated = meta.get("last_updated", row["created_at"]) or ""
            if updated:
                if date_after and updated < date_after:
                    continue
                if date_before and updated > date_before:
                    continue
        list_entry: dict[str, Any] = {
            "file_path": row["file_path"],
            "section_id": row["section_id"],
            "content": row["content"],
            "category": row["category"],
            "metadata": meta,
            "content_hash": row["content_hash"] if "content_hash" in row.keys() else None,
            "score": 1.0,
        }
        # surface confidence as top-level key when present in frontmatter.
        if "confidence" in meta:
            list_entry["confidence"] = meta["confidence"]
        results.append(list_entry)
        if len(results) >= limit:
            break

    db.close()
    return results


def recent_save_embeddings(
    window_minutes: int,
    now: datetime | None = None,
    fetch_limit: int = 200,
) -> list[tuple[str, list[float]]]:
    """Return embeddings of chunks indexed within the last ``window_minutes``.

    Backs the session-end semantic-dedup check (#126).  Joins ``chunks_vec``
    against ``chunks`` so we can pull the embedding alongside a useful slug
    (the ``id`` column doubles as a human-readable identifier the API uses
    when reporting which prior save matched).

    "Recent" is determined by file mtime, not by ``chunks.created_at``: the
    latter is sourced from frontmatter that's known to be inconsistent across
    write paths (some surfaces write local time with a ``Z`` suffix; the
    watcher reads ``created`` rather than ``created_at`` and so commonly
    leaves it empty).  File mtime is a reliable wall-clock signal for "this
    file was just written," which is what dedup actually needs.

    To bound the I/O, we order by ``rowid DESC`` (newer chunks have higher
    rowids) and inspect at most ``fetch_limit`` rows.  In a typical
    palinode deployment this comfortably covers a 60-minute window.

    Files under ``daily/`` are excluded — they're append-only logs that
    always overlap recent saves by construction; treating them as dedup
    candidates would suppress every session-end after the first.

    Args:
        window_minutes: Lookback window.  Negative or zero values yield an
            empty list (callers can disable dedup by passing 0).
        now: Override for the current time, primarily for tests.  Defaults
            to wall-clock UTC.
        fetch_limit: Max number of rows to inspect (rowid-ordered).

    Returns:
        List of ``(slug, embedding)`` tuples for chunks whose source file
        mtime is within the window.  Order is most-recent-first.  Empty
        list on any DB error or when the window is non-positive.
    """
    if window_minutes <= 0:
        return []

    cutoff_ts = ((now or _utc_now()) - timedelta(minutes=window_minutes)).timestamp()

    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT c.id, c.file_path, v.embedding
            FROM chunks c
            JOIN chunks_vec v ON v.id = c.id
            ORDER BY c.rowid DESC
            LIMIT ?
            """,
            (fetch_limit,),
        )
        rows = cursor.fetchall()
        db.close()
    except sqlite3.Error as _rse_exc:
        # Dedup silently disabled when the DB is unavailable. Log so operators
        # can correlate missing dedup signals with DB health events.
        _store_logger.warning(
            "palinode.store: recent_save_embeddings DB open failed — "
            "dedup disabled for this call (db_path=%r): %s",
            config.db_path,
            _rse_exc,
        )
        return []

    out: list[tuple[str, list[float]]] = []
    for row in rows:
        file_path = row["file_path"]
        if _is_daily_file(file_path):
            continue
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            # File gone (deleted, renamed) — skip; can't confirm freshness
            continue
        if mtime < cutoff_ts:
            continue
        raw = row["embedding"]
        try:
            if isinstance(raw, (bytes, bytearray)):
                # sqlite-vec stores embeddings as packed little-endian float32
                count = len(raw) // 4
                vec = list(struct.unpack(f"{count}f", raw))
            elif isinstance(raw, str):
                vec = json.loads(raw)
            else:
                vec = list(raw)
        except (ValueError, struct.error, TypeError):
            continue
        if vec:
            out.append((row["id"], vec))
    return out


# SQL fragment for the exponential-approach importance nudge (ADR-007 §3.3).
# `importance ← importance + (cap − importance) · α`, equivalently
# `importance ← importance·(1−α) + cap·α`. Smooth, self-limiting, cannot
# overshoot the cap. NULL importance is treated as `base` (0.5) before nudging.
# All numeric inputs (base, alpha, cap) are bound as parameters (?), never
# interpolated. The `(1-?)` / `?*?` arithmetic is evaluated by SQLite.
_IMPORTANCE_NUDGE_SQL = (
    "importance = COALESCE(importance, ?) "          # base for NULL
    "+ (? - COALESCE(importance, ?)) * ?"            # + (cap - imp) * alpha
)


def _explicit_nudge_keys(
    chunk_ids: Sequence[str],
    session_id: str | None,
    db: sqlite3.Connection,
    stamp: str,
) -> list[str]:
    """Return the subset of *chunk_ids* whose importance should be nudged now.

    ADR-007 §3.2 session-deduplication: a chunk is nudged at most once per
    ``(chunk, session_id)``. We record the claim in ``recall_nudges`` with
    ``INSERT OR IGNORE`` and treat the *newly inserted* rows as the nudge set —
    rows that already existed (this session already reinforced the chunk) are
    skipped.

    When ``session_id`` is None (harness gave us no session id) we cannot
    deduplicate, so we nudge every chunk: the session gate degrades to the
    pre-ADR-007 behavior rather than silently dropping reinforcement.
    """
    if session_id is None:
        return list(chunk_ids)
    nudged: list[str] = []
    for cid in chunk_ids:
        cur = db.execute(
            "INSERT OR IGNORE INTO recall_nudges (chunk_id, session_id, nudged_at) "
            "VALUES (?, ?, ?)",
            (cid, session_id, stamp),
        )
        if cur.rowcount:  # 1 → newly inserted → first demand this session
            nudged.append(cid)
    return nudged


def _record_recall_by(
    column: str,
    keys: Sequence[str],
    now: str | None,
    fn_name: str,
    *,
    mode: str = "explicit",
    session_id: str | None = None,
) -> int:
    """Batched, resilient access-metadata write keyed by *column* (#371, ADR-007).

    Always increments ``recall_count`` and stamps ``last_recalled`` (tz-aware
    UTC ISO-8601) for every chunk whose *column* is in *keys* — raw frequency
    and recency are mode-agnostic.

    The ``importance`` nudge (ADR-007 §3.2/§3.3) is gated:
      - only when ``mode == "explicit"`` (passive/ambient/session-start demand
        is *offered*, not *sought*, and must not inflate durability);
      - at most once per ``(chunk, session_id)`` (session-deduplicated);
      - by exponential approach toward ``importance_cap`` at rate
        ``importance_alpha``.

    Resilient by contract: the retrieval path is latency-sensitive, so a write
    failure must never propagate. Failures are logged and swallowed.

    ``column`` is a fixed internal literal ("id" or "file_path"), never caller
    input; *keys* are always bound as parameters.

    Returns:
        Number of chunk rows whose recall_count was updated (0 on no-op or
        swallowed failure). The importance-nudge subset may be smaller.
    """
    vals = [k for k in keys if k]
    if not vals:
        return 0
    stamp = now or _utc_now().isoformat()
    placeholders = ",".join("?" for _ in vals)
    db = None
    try:
        db = get_db()
        # Step 1: raw frequency + recency on every hit, mode-agnostic.
        # B608 rationale — `column` is a fixed internal literal ("id"/"file_path")
        # and `placeholders` is "?,?,..."; every value is bound below.
        cur = db.execute(
            f"UPDATE chunks SET last_recalled = ?, recall_count = recall_count + 1 "  # nosec B608
            f"WHERE {column} IN ({placeholders})",
            (stamp, *vals),
        )
        rowcount = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        # Step 2: importance nudge — explicit-only, session-deduplicated (§3.2).
        if mode == "explicit":
            cfg = config.decay
            if column == "id":
                nudge_ids = vals
            else:
                # file_path keys → resolve to chunk ids so dedup is per-chunk.
                rows = db.execute(
                    f"SELECT id FROM chunks WHERE {column} IN ({placeholders})",  # nosec B608
                    tuple(vals),
                ).fetchall()
                nudge_ids = [r["id"] for r in rows]
            to_nudge = _explicit_nudge_keys(nudge_ids, session_id, db, stamp)
            if to_nudge:
                nudge_ph = ",".join("?" for _ in to_nudge)
                db.execute(
                    f"UPDATE chunks SET {_IMPORTANCE_NUDGE_SQL} WHERE id IN ({nudge_ph})",  # nosec B608
                    (cfg.importance_base, cfg.importance_cap, cfg.importance_base,
                     cfg.importance_alpha, *to_nudge),
                )

        db.commit()
        return rowcount
    except Exception:  # never block retrieval on a stats write
        _store_logger.warning("%s: failed to persist access metadata", fn_name, exc_info=True)
        return 0
    finally:
        if db is not None:
            db.close()


def record_recall(
    chunk_ids: Sequence[str],
    now: str | None = None,
    *,
    mode: str = "explicit",
    session_id: str | None = None,
) -> int:
    """Persist access metadata for the given chunk ids (ADR-006/007, #371, ADR-007).

    Used by the search path, where hits are individual chunks. Falsy ids are
    skipped. ``recall_count``/``last_recalled`` update on every hit; the
    ``importance`` nudge is gated on ``mode == "explicit"`` and deduplicated per
    ``(chunk, session_id)`` (§3.2). See :func:`_record_recall_by`.

    Returns:
        Number of chunk rows updated (0 on no-op or swallowed failure).
    """
    return _record_recall_by(
        "id", chunk_ids, now, "record_recall", mode=mode, session_id=session_id
    )


def record_recall_for_paths(
    file_paths: Sequence[str],
    now: str | None = None,
    *,
    mode: str = "explicit",
    session_id: str | None = None,
) -> int:
    """Persist access metadata for every chunk of the given file paths (#371, ADR-007).

    Used by the read path (``/read`` / ``palinode_read``), which retrieves whole
    files, so recall is recorded against all chunks belonging to each path.
    Importance nudging follows the same explicit-only, session-deduplicated gate
    as :func:`record_recall`, deduplicated per *chunk* of the file.

    Returns:
        Number of chunk rows updated (0 on no-op or swallowed failure).
    """
    return _record_recall_by(
        "file_path", file_paths, now, "record_recall_for_paths",
        mode=mode, session_id=session_id,
    )


def set_status_for_path(file_path: str, status: str) -> int:
    """Set the stored chunk ``metadata.status`` for every chunk of a file (#482).

    The TTL auto-archive sweep (ADR-015 §2.3) flips an expired memory's
    frontmatter to ``status: archived``. That is a frontmatter-only change, so
    it does not move the body content-hash ``index_file`` keys its fast path on —
    a re-index would leave the indexed chunk metadata (which
    ``config.search.exclude_status`` reads) stale, and recall would not be
    suppressed. This updates the stored metadata directly, no body re-embed.

    ``file_path`` must be the absolute path stored in ``chunks.file_path``.

    Returns the number of chunk rows updated (rows already at ``status`` are
    left untouched).
    """
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, metadata FROM chunks WHERE file_path = ?", (file_path,)
        ).fetchall()
        updated = 0
        for row in rows:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            if meta.get("status") == status:
                continue
            meta["status"] = status
            db.execute(
                "UPDATE chunks SET metadata = ? WHERE id = ?",
                (json.dumps(meta, default=str), row["id"]),
            )
            updated += 1
        if updated:
            db.commit()
        return updated
    finally:
        db.close()


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
    kind_exclude_list: Sequence[str] | None = None,
    mode: str = "explicit",
    session_id: str | None = None,
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
    # Get results from both search methods. record_access=False: search_hybrid
    # records recall on its final merged hit set, not on these candidates.
    vec_results = search(query_embedding, category=category, top_k=top_k * 2, threshold=0.0,
                         record_access=False, kind_exclude_list=kind_exclude_list)
    try:
        fts_results = search_fts(query_text, category=category, top_k=top_k * 2,
                                 kind_exclude_list=kind_exclude_list)
    except Exception:
        # FTS5 corrupted — rebuild and retry once
        import logging
        logging.getLogger("palinode.store").warning("FTS5 corrupted, rebuilding...")
        rebuild_fts()
        try:
            fts_results = search_fts(query_text, category=category, top_k=top_k * 2,
                                     kind_exclude_list=kind_exclude_list)
        except Exception:
            fts_results = []  # Give up on BM25, return vector-only

    # Ambient context boost (ADR-008) resolves the caller's project-context files
    # from the entity index here — the only DB touch in the scoring path; the pure
    # ranker applies the boost given the resolved set.
    context_files: set[str] | None = None
    if context_entities and config.context.enabled and config.context.boost != 1.0:
        context_files = set()
        for entity in context_entities:
            for row in get_entity_files(entity):
                context_files.add(row["file_path"])

    # Fuse + re-rank (RRF → decay → priority → context → daily → dedup → threshold
    # → date) in the pure ranker. priority_weight is read from this module
    # so patch.object(store, "_PRIORITY_RANK_WEIGHT", ...) still tunes ordering.
    merged = rank_hybrid(
        vec_results,
        fts_results,
        top_k=top_k,
        threshold=threshold,
        hybrid_weight=hybrid_weight,
        priority_weight=_PRIORITY_RANK_WEIGHT,
        context_files=context_files,
        include_daily=include_daily,
        date_after=date_after,
        date_before=date_before,
    )

    if merged:
        merged = check_freshness(merged)

    # Record retrieval access metadata (ADR-006/007): batched, resilient.
    if merged:
        record_recall([r.get("id") for r in merged[:top_k]], mode=mode, session_id=session_id)

    return merged


# Human-priority ranking nudge weight (tuned in). A priority-5 memory
# gets at most +0.05 (priority-1 at most -0.05) added to its normalized score, so
# a clear relevance gap always outranks priority while similar-relevance hits are
# ordered by it. Confirmed at 0.025 against the ranking properties pinned in
# tests/test_priority_ranking_486.py (which is the tuning rationale — a larger
# weight lets a max-priority weak match overtake a stronger normal-priority one).
_PRIORITY_RANK_WEIGHT = 0.025


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
        # B608 rationale - ph is "?,?,..." from _parameterize_in_clause; values bound via activated_params
        rows = db.execute(f"""
            SELECT DISTINCT e2.entity_ref, COUNT(*) as co_count
            FROM entities e1
            JOIN entities e2 ON e1.file_path = e2.file_path
                AND e1.entity_ref != e2.entity_ref
            WHERE e1.entity_ref IN ({ph})
            GROUP BY e2.entity_ref
            ORDER BY co_count DESC
            LIMIT 20
        """, activated_params).fetchall()  # nosec B608
        
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
    # B608 rationale - ph is "?,?,..." from _parameterize_in_clause; values bound via entity_params
    files = db.execute(f"""
        SELECT DISTINCT e.file_path, MAX(?) as activation_score
        FROM entities e
        WHERE e.entity_ref IN ({ph})
        GROUP BY e.file_path
    """, (max(activation.values()), *entity_params)).fetchall()  # nosec B608
    
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
