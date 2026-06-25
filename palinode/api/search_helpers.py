"""Search/recall result shaping: filters, snippets, similarity, dedup (#556).

Extracted from the former ``routers/_shared.py`` junk drawer. Everything the
search and session-end paths run over a candidate slate after the store returns
it: date/type/priority filters, query-centered snippet windowing, the
over-fetch + re-embed rerank used by the wiki/dedup maintenance tools, and the
session-end near-duplicate check.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from palinode.core import embedder, store
from palinode.core.config import config
from palinode.core.defaults import (
    SESSION_END_DEDUP_THRESHOLD,
    SESSION_END_DEDUP_WINDOW_MINUTES,
)

from palinode.api._util import _utc_now
from palinode.api.path_safety import _open_memory_file_text, _resolve_memory_path

logger = logging.getLogger("palinode.api")


def _compute_effective_date_after(req: Any) -> str | None:
    """Combine explicit date_after with since_days; pick the more restrictive.

    Returns the ISO-8601 string (UTC, "Z" suffix) representing the earliest
    creation/update time a result is allowed to have. ``since_days`` derives
    `now - since_days days`. If both are set, takes the later (later → more
    restrictive). If neither is set, returns None.
    """
    derived = None
    if req.since_days and req.since_days > 0:
        threshold_dt = _utc_now() - timedelta(days=req.since_days)
        derived = threshold_dt.isoformat().replace("+00:00", "Z")
    explicit = req.date_after
    if derived and explicit:
        return derived if derived > explicit else explicit
    return derived or explicit


def _filter_types(results: list[dict[str, Any]], types: list[str] | None) -> list[dict[str, Any]]:
    """Drop results whose frontmatter `type` isn't in the allowed list (#141).

    Empty / None ``types`` is a no-op. Filter is OR-style: a result keeps if its
    type matches any of the values.
    """
    if not types:
        return results
    allowed = set(types)
    return [r for r in results if r.get("metadata", {}).get("type") in allowed]


def _filter_type_deny(
    results: list[dict[str, Any]], type_deny: list[str] | None
) -> list[dict[str, Any]]:
    """Exclude results whose frontmatter `type` is in the deny list (#391).

    Empty / None ``type_deny`` is a no-op. Takes precedence over the allow-list:
    if a type is in both ``types`` and ``type_deny``, the result is dropped.
    """
    if not type_deny:
        return results
    denied = set(type_deny)
    return [r for r in results if r.get("metadata", {}).get("type") not in denied]


def _priority_value(metadata: Any) -> int:
    if not isinstance(metadata, dict):
        return 3
    try:
        priority = int(metadata.get("priority", 3))
    except (TypeError, ValueError):
        return 3
    return priority if 1 <= priority <= 5 else 3


def _filter_min_priority(
    results: list[dict[str, Any]], min_priority: int | None
) -> list[dict[str, Any]]:
    if min_priority is None:
        return results
    return [
        r for r in results
        if _priority_value(r.get("metadata", {})) >= min_priority
    ]


def _resolve_snippet_max_chars(req_max_chars: int | None) -> int:
    """Return the effective snippet cap for a request (#391).

    Uses the per-request override when supplied (clamped to [1, 8000]),
    falling back to the config default.
    """
    if req_max_chars is not None:
        return max(1, min(req_max_chars, 8000))
    return config.search.snippet_max_chars


def _windowed_snippet(content: str, query: str, max_chars: int) -> str:
    """Return a query-centered window of ``content`` no longer than ``max_chars``.

    Strategy: find the earliest case-insensitive substring hit for any
    whitespace-split token of ``query`` (len >= 3 to skip noise like "to"/"in"),
    then slice a window centered on that hit. Falls back to the leading
    ``max_chars`` when nothing matches — which is the correct vector-only
    behavior, since the chunk itself is already the relevant semantic window.

    No FTS5 round-trip: the chunk content is already in memory.
    """
    if len(content) <= max_chars:
        return content
    tokens = [t for t in re.split(r"\s+", query.strip()) if len(t) >= 3]
    lower = content.lower()
    hit = -1
    for tok in tokens:
        idx = lower.find(tok.lower())
        if idx != -1 and (hit == -1 or idx < hit):
            hit = idx
    if hit == -1:
        # Leading window — ellipsis suffix only (no prefix needed).
        return content[:max_chars].rstrip() + "…"
    # Center the window on the match, but clamp to content bounds.
    half = max_chars // 2
    start = max(0, hit - half)
    end = min(len(content), start + max_chars)
    # If we hit the right edge, shift the window left so we still fill it.
    if end - start < max_chars:
        start = max(0, end - max_chars)
    snippet = content[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{snippet}{suffix}"


def _enrich_with_snippets(
    results: list[dict[str, Any]], query: str, max_chars: int
) -> None:
    """In-place add ``snippet`` and ``content_truncated`` to each result (#352).

    The ``content`` field is preserved so API/CLI consumers that legitimately
    want full chunk bodies are unchanged. MCP callers render ``snippet`` by
    default to stay within MCP tool-result budgets.
    """
    for r in results:
        content = r.get("content") or ""
        if len(content) <= max_chars:
            r["snippet"] = content
            r["content_truncated"] = False
        else:
            r["snippet"] = _windowed_snippet(content, query, max_chars)
            r["content_truncated"] = True


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
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# Back-compat alias (#556): ``_cosine_similarity`` and ``_cosine`` were two
# byte-identical implementations in the old _shared.py. Collapsed to one;
# the alias keeps the historical name importable for any caller/re-export.
_cosine_similarity = _cosine


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
    # cannot accidentally re-introduce pollution (ADR-015 H1, #481).
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
            "similarity": round(sim, 4),
            "snippet": snippet,
        }

    ranked = sorted(seen.values(), key=lambda r: r["similarity"], reverse=True)
    return ranked[:top_k]


def _check_session_end_dedup(
    content: str,
    window_minutes: int = SESSION_END_DEDUP_WINDOW_MINUTES,
    threshold: float = SESSION_END_DEDUP_THRESHOLD,
) -> tuple[str | None, float]:
    """Look for a recent indexed save whose embedding is near-identical to ``content`` (#126).

    Returns ``(matched_slug, similarity)`` when a recent save scores at or
    above ``threshold``; ``(None, 0.0)`` otherwise.  Failure modes — empty
    embedding from the embedder, no recent saves, or DB error inside the
    helper — return ``(None, 0.0)`` so the caller writes both files.
    """
    try:
        new_emb = embedder.embed(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"session_end dedup: embed failed ({e}); writing without dedup")
        return None, 0.0
    if not new_emb:
        logger.warning("session_end dedup: embedder returned empty vector; writing without dedup")
        return None, 0.0

    try:
        recent = store.recent_save_embeddings(window_minutes)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"session_end dedup: recent_save_embeddings failed ({e}); writing without dedup")
        return None, 0.0

    best_slug: str | None = None
    best_sim = 0.0
    for slug, emb in recent:
        sim = _cosine_similarity(new_emb, emb)
        if sim > best_sim:
            best_sim = sim
            best_slug = slug

    if best_slug is not None and best_sim >= threshold:
        return best_slug, best_sim
    return None, best_sim
