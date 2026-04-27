"""
Palinode shared indexer helper.

Hosts the canonical "parse markdown -> embed sections -> upsert chunks"
pipeline used by both the filesystem watcher (``palinode.indexer.watcher``)
and the API ``POST /save`` endpoint (``palinode.api.server.save_api``).

Defining this once removes the race window where ``/save`` returned 200
before the watcher had finished embedding (#251), and lets the watcher
treat ``content_hash`` as a first-pass guard while still re-validating
that the FTS5 + vec0 index entries actually exist for "unchanged" rows.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from palinode.core import embedder, parser, store
from palinode.core.hashing import stable_md5_hexdigest

logger = logging.getLogger("palinode.indexer")


def _index_entries_present(db: Any, chunk_id: str) -> bool:
    """Return True iff the vec0 entry exists for ``chunk_id``.

    Used by ``index_file`` to detect rows whose ``content_hash`` matches
    the stored chunk but whose vector entry was never populated (e.g. the
    embedder was unreachable on the original write, or the per-chunk
    ``upsert_chunks`` transaction silently swallowed a vec0 insert error
    and committed a chunks row with no embedding). In that case we treat
    the row as "needs re-embed" rather than skipping silently (#251).

    Notes
    -----
    The FTS5 ``chunks_fts`` table is declared with ``content=chunks`` (an
    external-content index), so a ``SELECT … FROM chunks_fts WHERE rowid
    = …`` always finds the row as long as the underlying ``chunks`` row
    exists — regardless of whether the inverted index actually holds any
    tokens for it. That makes it a useless presence check. The vec0
    ``chunks_vec`` table is real storage: a missing ``id`` row is the
    direct, observable symptom of the failure mode in #251 (vector
    search returning zero results immediately after /save).
    """
    try:
        vec_row = db.execute(
            "SELECT 1 FROM chunks_vec WHERE id = ?", (chunk_id,)
        ).fetchone()
    except Exception:
        vec_row = None
    return bool(vec_row)


def index_file(filepath: str, *, content: str | None = None) -> dict[str, Any]:
    """Parse a markdown file, embed its sections, and upsert chunks.

    Mirrors the logic that historically lived inline inside the watcher's
    ``_process_file``. Re-used verbatim by ``POST /save`` so the response
    only returns 200 once the embedding has actually landed.

    Args:
        filepath: absolute path to the .md file (must exist on disk).
        content: optional pre-read file content. If omitted, reads from disk.

    Returns:
        dict with keys:
            * ``embedded`` (bool): True iff at least one section was embedded
              successfully OR every section was already correctly indexed.
              False on hard embedder failure (Ollama unreachable, etc.).
            * ``chunks_written`` (int): number of chunks newly upserted.
            * ``chunks_unchanged`` (int): rows whose content_hash + index
              presence both matched (no work).
            * ``chunks_reembedded`` (int): rows where content_hash matched
              but FTS/vec entries were missing — re-embedded as defense-
              in-depth against silent index loss (#251).
            * ``chunks_deleted`` (int): obsolete rows pruned for this file.
            * ``error`` (str | None): one-line failure reason, if any.
    """
    result: dict[str, Any] = {
        "embedded": False,
        "chunks_written": 0,
        "chunks_unchanged": 0,
        "chunks_reembedded": 0,
        "chunks_deleted": 0,
        "error": None,
    }

    if not os.path.exists(filepath):
        result["error"] = "file not found"
        return result

    if content is None:
        try:
            with open(filepath, "r") as f:
                content = f.read()
        except Exception as e:
            result["error"] = f"read failed: {e}"
            return result

    metadata, sections = parser.parse_markdown(content)
    category = metadata.get("category", os.path.basename(os.path.dirname(filepath)))

    chunks: list[dict[str, Any]] = []
    valid_chunk_ids: list[str] = []
    embed_failure = False

    for sec in sections:
        chunk_id = stable_md5_hexdigest(f"{filepath}#{sec['section_id']}")
        valid_chunk_ids.append(chunk_id)
        content_hash = hashlib.sha256(sec["content"].encode()).hexdigest()

        db = store.get_db()
        existing = db.execute(
            "SELECT content_hash FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        index_ok = _index_entries_present(db, chunk_id) if existing else False
        db.close()

        # Fast path: content unchanged AND both index entries are present.
        if existing and existing["content_hash"] == content_hash and index_ok:
            result["chunks_unchanged"] += 1
            continue

        # Either the content changed, or the row exists but its FTS/vec
        # entries are missing. Either way, embed (#251 fix B).
        emb = embedder.embed(sec["content"])
        if not emb:
            # Hard embedder failure (Ollama down, timeout, misconfig).
            # Fall through — the file is on disk, the watcher / a later
            # call can retry. Don't insert a half-baked row.
            embed_failure = True
            continue

        if existing and existing["content_hash"] == content_hash and not index_ok:
            result["chunks_reembedded"] += 1
        else:
            result["chunks_written"] += 1

        chunks.append({
            "id": chunk_id,
            "file_path": filepath,
            "section_id": sec["section_id"],
            "category": category,
            "content": sec["content"],
            "metadata": metadata,
            "created_at": metadata.get("created_at", ""),
            "last_updated": metadata.get("last_updated", ""),
            "embedding": emb,
        })

    # Prune chunks that no longer correspond to a section in this file.
    db = store.get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM chunks WHERE file_path = ?", (filepath,))
    existing_ids = [row["id"] for row in cursor.fetchall()]
    to_delete = [cid for cid in existing_ids if cid not in valid_chunk_ids]
    if to_delete:
        placeholders = ",".join("?" * len(to_delete))
        for cid in to_delete:
            try:
                cursor.execute(
                    "DELETE FROM chunks_fts WHERE rowid = (SELECT rowid FROM chunks WHERE id = ?)",
                    (cid,),
                )
            except Exception:
                pass
        cursor.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", to_delete)
        try:
            cursor.execute(f"DELETE FROM chunks_vec WHERE id IN ({placeholders})", to_delete)
        except Exception:
            pass
        db.commit()
        result["chunks_deleted"] = len(to_delete)
    db.close()

    if chunks:
        store.upsert_chunks(chunks, skip_unchanged=False)

    store.upsert_entities(filepath, metadata)

    # ``embedded`` is True if every section ended up indexed — either by
    # writing fresh chunks or by hitting the unchanged-and-indexed fast
    # path. A hard embedder failure on any section flips it to False so
    # the API response can surface ``embedded: false``.
    if embed_failure:
        result["embedded"] = False
        if result["error"] is None:
            result["error"] = "embedder unreachable"
    else:
        result["embedded"] = True

    return result
