"""Search/recall result shaping: filters, snippets, similarity, dedup.

Extracted from the former ``routers/_shared.py`` junk drawer. Everything the
search and session-end paths run over a candidate slate after the store returns
it: date/type/priority filters, query-centered snippet windowing, the
over-fetch + re-embed rerank used by the wiki/dedup maintenance tools, and the
session-end near-duplicate check.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import HTTPException

from palinode.core import embedder, store
from palinode.core.defaults import (
    SESSION_END_DEDUP_THRESHOLD,
    SESSION_END_DEDUP_WINDOW_MINUTES,
)
from palinode.core.path_guard import to_rel_path

from palinode.api.path_safety import _open_memory_file_text, _resolve_memory_path
# The row shaping that search runs lives in core, beside the search module.
from palinode.core.search_rows import (  # noqa: F401
    _apply_tier,
    _compute_effective_date_after,
    _enrich_with_rel_path,
    _enrich_with_snippets,
    _filter_min_priority,
    _filter_type_deny,
    _filter_types,
    _priority_value,
    _resolve_snippet_max_chars,
    _windowed_snippet,
)

logger = logging.getLogger("palinode.api")


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    BGE-M3 outputs are L2-normalized so this reduces to a dot product, but we
    keep the explicit norm denominator for correctness against any embedder
    that doesn't normalize (e.g. Gemini at certain dimensions).

    Returns 0.0 on shape mismatch or zero-magnitude inputs so callers can treat
    "incomparable" the same as "not similar enough."
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _read_memory_body(file_path: str) -> str | None:
    """Read a memory file's full body for re-embedding.  Returns None on miss.

    L5: try-open with O_NOFOLLOW (POSIX) instead of exists+open so a symlink
    swap cannot redirect the read between the check and the open.
    """
    candidates = [file_path]
    if not file_path.endswith(".md"):
        candidates.append(f"{file_path}.md")
    for candidate in candidates:
        try:
            _, resolved = _resolve_memory_path(candidate)
        except HTTPException:
            continue
        try:
            return _open_memory_file_text(resolved)
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return None


def _embedding_candidates(
    query_embedding: list[float],
    top_k: int,
    over_fetch: int = 4,
) -> list[dict[str, Any]]:
    """Run the existing vector index for an over-fetched candidate slate.

    The corpus index was built without the wikilink-stripping preprocessing;
    we use it only to narrow down which files to re-embed.  Final ranking
    (caller's responsibility) re-embeds each candidate's preprocessed body so
    the cosine score is apples-to-apples with the preprocessed query.
    """
    if not query_embedding:
        return []
    # H1: this over-fetched candidate slate feeds dedup_suggest / orphan_repair
    # / cluster_neighbors / topic_coverage — internal maintenance, not human
    # recall. search_internal hard-sets record_access=False so future refactors
    # cannot accidentally re-introduce pollution (ADR-015 H1).
    return store.search_internal(
        query_embedding=query_embedding,
        top_k=top_k * over_fetch,
        threshold=0.0,  # caller filters; we want the wider slate
    )


def _rerank_with_preprocessing(
    query_preprocessed: str,
    candidates: list[dict[str, Any]],
    min_similarity: float,
    top_k: int,
) -> list[dict[str, Any]]:
    """Re-embed each candidate's preprocessed body, score against the
    preprocessed query, and return the top_k above ``min_similarity``.

    This is the strip-at-query-AND-strip-at-rerank pipeline.  The corpus
    index stays raw (so existing ``palinode_search`` behaviour is unchanged);
    the dedup/orphan tools pay a small re-embed cost per candidate to get
    formatting-noise-free similarity.
    """
    from palinode.core.embedding_preprocess import preprocess_for_similarity

    query_emb = embedder.embed(query_preprocessed)
    if not query_emb:
        return []

    # Group by file_path so we re-embed each file once, not per chunk.  The
    # candidate list from store.search() may contain multiple chunks of the
    # same file; the wiki tools care about file-level dedup.
    seen: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        fp = cand.get("file_path", "")
        if not fp or fp in seen:
            continue
        body = _read_memory_body(fp)
        if body is None:
            # Fall back to the chunk content if the file is gone — better
            # than dropping the candidate silently.
            body = cand.get("content", "")
        preprocessed = preprocess_for_similarity(body)
        if not preprocessed:
            continue
        cand_emb = embedder.embed(preprocessed)
        if not cand_emb:
            continue
        sim = _cosine(query_emb, cand_emb)
        if sim < min_similarity:
            continue
        snippet = preprocessed[:200].strip()
        seen[fp] = {
            "file_path": fp,
            "rel_path": to_rel_path(fp),
            "similarity": round(sim, 4),
            "snippet": snippet,
        }

    ranked = sorted(seen.values(), key=lambda r: r["similarity"], reverse=True)
    return ranked[:top_k]


def _dedup_session_id(value: Any) -> str | None:
    """Normalise a session id: a non-blank string, else ``None`` (no identity)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dedup_candidate_matches(
    file_path: str, project: str | None, session_id: str,
) -> bool:
    """Is the candidate at ``file_path`` a save from the entry's own session?

    It must name the same ``session_id`` in its frontmatter and belong to the
    entry's project. Both are decided on live frontmatter (the visibility
    module's rule: the file, not the DB cache). ``project=None`` matches only
    candidates that name no project. A candidate that is unreadable or carries
    no session id never matches, so it can never suppress.
    """
    from palinode.core import parser
    from palinode.core.scope import project_entities

    try:
        with open(file_path, encoding="utf-8") as fh:
            meta, _ = parser.parse_frontmatter(fh.read())
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(meta, dict):
        meta = {}
    if _dedup_session_id(meta.get("session_id")) != session_id:
        return False
    keys = {e[len("project/"):].lower() for e in project_entities(meta)}
    if project is None:
        return not keys
    return project.removeprefix("project/").lower() in keys


def _check_session_end_dedup(
    content: str,
    window_minutes: int = SESSION_END_DEDUP_WINDOW_MINUTES,
    threshold: float = SESSION_END_DEDUP_THRESHOLD,
    *,
    project: str | None = None,
    session_id: str | None = None,
) -> tuple[str | None, float]:
    """Look for a recent save from the same session that is near-identical to ``content``.

    Returns ``(matched_slug, similarity)`` when a recent save scores at or
    above ``threshold``; ``(None, 0.0)`` otherwise.  Failure modes — empty
    embedding from the embedder, no recent saves, or DB error inside the
    helper — return ``(None, 0.0)`` so the caller writes both files.

    Only saves from the entry's own session are candidates: the candidate's
    frontmatter must carry the same ``session_id``, and it must be in the
    entry's ``project`` (``None``: only saves naming no project). The dedup
    exists for one session restating itself (a snapshot, then the wrap-up).
    Two different sessions in one project that differ only in a decided value
    ("use batch X", then "use batch Y") embed above the threshold, and matching
    them dropped the newer decision's searchable record, so search served the
    stale value. With no ``session_id`` on the entry nothing is suppressed and
    the embedder is not called: a duplicate file is the cheaper failure.
    """
    session_id = _dedup_session_id(session_id)
    if session_id is None:
        return None, 0.0

    try:
        new_emb = embedder.embed(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"session_end dedup: embed failed ({e}); writing without dedup")
        return None, 0.0
    if not new_emb:
        logger.warning("session_end dedup: embedder returned empty vector; writing without dedup")
        return None, 0.0

    try:
        recent = store.recent_save_embeddings(window_minutes, with_paths=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"session_end dedup: recent_save_embeddings failed ({e}); writing without dedup")
        return None, 0.0

    best_slug: str | None = None
    best_sim = 0.0
    eligible: dict[str, bool] = {}
    for slug, file_path, emb in recent:
        if file_path not in eligible:
            eligible[file_path] = _dedup_candidate_matches(file_path, project, session_id)
        if not eligible[file_path]:
            continue
        sim = _cosine(new_emb, emb)
        if sim > best_sim:
            best_sim = sim
            best_slug = slug

    if best_slug is not None and best_sim >= threshold:
        return best_slug, best_sim
    return None, best_sim
