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
import time
from typing import Any

from palinode.core import embedder, parser, store
from palinode.core.hashing import stable_md5_hexdigest
from palinode.core.ollama_client import get_ollama_client

logger = logging.getLogger("palinode.indexer")

# Cold-embed probe cache. Only negative verdicts need
# caching: a successful probe IS an embed, so it flips the client's
# ``has_embedded_ok`` and this cache is never consulted again. The TTL keeps a
# keyword-only install (embedder absent forever) at one bounded probe per
# window instead of one per indexed file when the watcher sweeps a batch.
_PROBE_TTL_S = 30.0
_probe_cache: dict[str, Any] = {"ts": 0.0, "ok": None}


def _embeds_deferred(client: Any) -> bool:
    """True when this pass should skip embeds (cold/absent embed path).

    Until an embed has succeeded in-process, one bounded ``probe_embed``
    (cached ``_PROBE_TTL_S`` seconds on failure) stands in for letting every
    section pay the full embed timeout.
    """
    if client.has_embedded_ok:
        return False
    now = time.monotonic()
    if _probe_cache["ok"] is not None and (now - _probe_cache["ts"]) < _PROBE_TTL_S:
        return not _probe_cache["ok"]
    ok = client.probe_embed()
    _probe_cache["ts"] = now
    _probe_cache["ok"] = ok
    return not ok


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
    except Exception as e:
        # A DB error here is treated as "entry absent", which forces a needless
        # re-embed. That degradation is silent without this line — surface it at
        # DEBUG so a re-embed storm can be traced back to vec0 read errors.
        logger.debug(
            "index presence check failed; treating as absent op=index chunk_id=%s error=%r",
            chunk_id, str(e),
        )
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
        "indexed_vec": True,
        "indexed_fts": True,
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
            # Unreadable source file is operator-facing — previously the reason
            # only reached the caller's result dict, never the log.
            logger.warning(
                "index read failed op=index file_path=%s error=%r",
                filepath, str(e),
            )
            result["error"] = f"read failed: {e}"
            return result

    metadata, sections = parser.parse_markdown(content)
    category = metadata.get("category", os.path.basename(os.path.dirname(filepath)))

    # Cold-embed fast path: until an embed has succeeded in this process, one
    # bounded probe (~2 s, negative verdict cached) decides the fate of ALL
    # sections instead of each one paying the full embed timeout — the "first
    # save on a fully-cold host blocks until the embed timeout" gap. Deferred
    # sections are written as FTS-only rows (keyword-searchable immediately,
    # which makes the CLI's "keyword-searchable now" note true) with no
    # chunks_vec entry — exactly the missing-index signal the re-embed branch
    # below keys on, so the next pass after the embedder warms converges them
    # to fully-embedded rows.
    defer_embeds = _embeds_deferred(get_ollama_client())
    if defer_embeds:
        embedder._notice_keyword_only_once()

    chunks: list[dict[str, Any]] = []
    valid_chunk_ids: list[str] = []
    embed_failure = False
    sections_failed = 0

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
        # entries are missing. Either way, embed (fix B) — unless this pass
        # is deferring embeds: then write the row with no vector (FTS-only).
        if defer_embeds:
            emb = []
        else:
            emb = embedder.embed(sec["content"])
            if not emb:
                # Hard embedder failure (Ollama down, timeout, misconfig).
                # Fall through — the file is on disk, the watcher / a later
                # call can retry. Don't insert a half-baked row.
                # Previously this swallowed the miss into a bare flag: the indexer
                # never said which section failed. Per-section WARNING.
                embed_failure = True
                sections_failed += 1
                logger.warning(
                    "section embed returned empty; skipping op=index file_path=%s section_id=%s",
                    filepath, sec["section_id"],
                )
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
                # Sanctioned FTS5 external-content removal, before the source
                # chunks row is deleted below — a bare per-rowid DELETE orphans
                # the inverted-index tokens.
                store.fts5_delete_chunk(cursor, cid)
            except Exception:
                # Best-effort: chunks_fts is an external-content FTS5 index that
                # the periodic rebuild recovers, so a failed prune-delete here is
                # provably inert (docs/logging.md silent-except carve-out).
                pass
        # B608 rationale - placeholders is "?,?,..." built from len(to_delete); values bound via to_delete
        cursor.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", to_delete)  # nosec B608
        try:
            cursor.execute(f"DELETE FROM chunks_vec WHERE id IN ({placeholders})", to_delete)  # nosec B608
        except Exception as e:
            # Unlike chunks_fts, a failed vec0 prune leaves orphan vectors that
            # no periodic rebuild reclaims — emit DEBUG so the orphan source is
            # traceable.
            logger.debug(
                "chunks_vec prune failed; vectors may be orphaned "
                "op=vector file_path=%s ids_count=%d error=%r",
                filepath, len(to_delete), str(e),
            )
        db.commit()
        result["chunks_deleted"] = len(to_delete)
    db.close()

    if chunks:
        upsert_result = store.upsert_chunks(chunks, skip_unchanged=False)
        # Surface per-index health from upsert_chunks.
        if not upsert_result["vec_ok"]:
            result["indexed_vec"] = False
        if not upsert_result["fts_ok"]:
            result["indexed_fts"] = False

    store.upsert_entities(filepath, metadata)

    # ``embedded`` is True if every section ended up indexed — either by
    # writing fresh chunks or by hitting the unchanged-and-indexed fast
    # path. A hard embedder failure on any section flips it to False so
    # the API response can surface ``embedded: false``.
    if defer_embeds and chunks:
        # Rows were written FTS-only. Report as not-embedded (with the vec
        # index flagged absent) so callers surface the deferred state; one
        # INFO line, not a WARNING — this is the designed cold-host path,
        # not a failure.
        result["embedded"] = False
        result["indexed_vec"] = False
        if result["error"] is None:
            result["error"] = (
                "embed deferred: probe failed (cold or absent embedder); "
                "rows are keyword-searchable now, re-embed follows"
            )
        logger.info(
            "embeds deferred; sections written FTS-only "
            "op=index file_path=%s sections_deferred=%d",
            filepath, len(chunks),
        )
    elif embed_failure:
        result["embedded"] = False
        if result["error"] is None:
            result["error"] = "embedder unreachable"
        # One summary WARNING naming the file + count of failed sections, so a
        # partial index is visible without grepping per-section lines.
        logger.warning(
            "file partially indexed; some sections did not embed "
            "op=index file_path=%s sections_failed=%d",
            filepath, sections_failed,
        )
    else:
        result["embedded"] = True

    return result
