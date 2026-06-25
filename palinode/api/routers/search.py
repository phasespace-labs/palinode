from __future__ import annotations
import os
import re
import logging
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from palinode.core import store, embedder
from palinode.core.config import config
from palinode.api._util import _retrieval_logger, _safe_500
from palinode.api.rate_limit import _RATE_LIMIT_SEARCH, _check_rate_limit
from palinode.api.search_helpers import (
    _compute_effective_date_after,
    _embedding_candidates,
    _enrich_with_snippets,
    _filter_min_priority,
    _filter_type_deny,
    _filter_types,
    _read_memory_body,
    _rerank_with_preprocessing,
    _resolve_snippet_max_chars,
)
logger = logging.getLogger("palinode.api")
router = APIRouter()


class SearchRequest(BaseModel):
    query: str
    category: str | None = None
    limit: int | None = config.search.default_limit
    threshold: float | None = config.search.api_threshold
    hybrid: bool | None = None
    date_after: str | None = None
    date_before: str | None = None
    context: list[str] | None = None  # Entity refs for ambient context boost (ADR-008)
    include_daily: bool | None = False  # Skip daily/ penalty when True (#93)
    # ADR-015 §2.3: None → default hard-exclude of telemetry; [] → include
    # telemetry when the caller passes the explicit override.
    include_telemetry: bool | None = False
    # #141: filter by memory `type` frontmatter (one of PersonMemory, Decision,
    # ProjectSnapshot, Insight, ResearchRef, ActionItem). Independent of `category`
    # which filters by directory. Applied as a post-fetch filter; pass multiple
    # types to OR them.
    types: list[str] | None = None
    # #391: deny-list complement to `types`. Results whose `type` is in this list
    # are excluded after fetch. Takes precedence: a result present in both `types`
    # and `type_deny` is dropped.
    type_deny: list[str] | None = None
    min_priority: int | None = Field(default=None, ge=1, le=5)
    # #141: relative recency window. If set, derives an effective `date_after`
    # of `now - since_days` days. Combined with explicit `date_after` by taking
    # the later (more restrictive) of the two.
    since_days: int | None = None
    # #391: per-request snippet cap override. When set (positive int), overrides
    # config.search.snippet_max_chars for this request only. Clamped to [1, 8000].
    max_chars: int | None = None
    # ADR-007 §3.2: demand classification. "explicit" (default) = the agent
    # issued this search because it needed the memory → nudges importance.
    # "passive" = ambient/auto-inject/session-start priming (ADR-008/#358) → the
    # memory was *offered*, not *sought*; recall_count/last_recalled still update
    # but importance is NOT nudged. The explicit-only gate (load-bearing once an
    # ambient caller sets mode="passive") flows down to store.record_recall.
    mode: str | None = None
    # ADR-007 §3.2: session id for per-(chunk, session) nudge deduplication. When
    # provided, importance is reinforced at most once per memory per session.
    session_id: str | None = None


class SearchAssociativeRequest(BaseModel):
    query: str
    seed_entities: list[str] | None = None
    limit: int | None = 5


class DedupSuggestRequest(BaseModel):
    """Find existing files semantically near the supplied draft content (#210).

    Used by the LLM at write-time to decide "create new vs update existing".
    Both ``min_similarity`` and ``top_k`` are kwarg-tunable per the design
    doc — defaults match the BGE-M3 thresholds research-validated in
    `artifacts/obsidian-integration/design.md`.
    """
    content: str
    min_similarity: float | None = 0.80
    top_k: int | None = 5
    # Threshold above which a candidate is flagged ``strong_dup=true`` —
    # "near-paraphrase territory" per the design doc; LLM should usually
    # update rather than create when this fires.
    strong_dup_threshold: float | None = 0.90


class OrphanRepairRequest(BaseModel):
    """Find files semantically near a broken `[[wikilink]]` target (#210).

    The LLM uses the candidate slate to propose a redirect or seed a new
    target file with informed context.  ``min_similarity`` defaults are
    looser than ``dedup_suggest`` because we want a wider candidate slate
    here — the LLM picks one or none.
    """
    broken_link: str
    min_similarity: float | None = 0.65
    top_k: int | None = 10


class ClusterNeighborsRequest(BaseModel):
    """Find semantically related files not already linked to/from file_path (#235).

    Used by the LLM during wiki-maintenance passes to surface implicit
    relationships that no ``[[wikilink]]`` yet captures.  Default threshold
    0.70 sits between the dedup default (0.80) and the orphan-repair default
    (0.65) — looser than "potential duplicate", tighter than "anything vaguely
    related".
    """

    file_path: str
    min_similarity: float | None = 0.70
    top_k: int | None = 10


class TopicCoverageRequest(BaseModel):
    """Check whether any wiki page already covers a topic phrase (#235).

    The LLM calls this BEFORE ingesting new content to ask "is this
    redundant?".  Different framing from ``dedup_suggest``: the input is a
    short topic phrase (not full draft content), and the return is a simple
    ``{covered, best_match, similarity}`` dict rather than a ranked list.
    Default threshold 0.78 — between dedup (0.80) and cluster (0.70).
    """

    query: str
    min_similarity: float | None = 0.78


@router.post("/search")
def search_api(req: SearchRequest, request: Request = None) -> list[dict[str, Any]]:
    """Semantic vector search against cached `.palinode.db` chunks.

    Empty query routes to recency-only mode (#141): returns the most recent
    chunks ordered by created_at desc, optionally filtered by `types` and
    `since_days`. Skips embedding entirely.

    Returns:
        list[dict[str, Any]]: List payload sequence matching the criteria boundaries.

    # Security audit (I2, 2026-04-30):
    # - All SQL goes through store.search / store.search_hybrid /
    #   store.list_recent / store.search_fts; every cursor.execute() in
    #   those helpers uses ? placeholders (no f-string interpolation of
    #   user input into SQL). Verified directly in palinode/core/store.py.
    # - No LLM call inside search_api itself — only embedder.embed() on
    #   the query string, which is a vector model and not a prompt
    #   injection surface.
    # - Result sets are bounded: req.limit (capped server-side via
    #   config.search.default_limit when unset), with an internal
    #   over-fetch factor of 5x when `types` filter is active. The
    #   over-fetch ceiling is itself bounded by the underlying SQL
    #   LIMIT clause.
    # - FTS5 query string is sanitized via store.sanitize_fts_query()
    #   before MATCH, defending against operator-injection (`OR`, `NEAR`).
    """
    if request:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip, "search", _RATE_LIMIT_SEARCH):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    try:
        effective_date_after = _compute_effective_date_after(req)
        limit = req.limit or config.search.default_limit

        # ADR-015 §2.3: None → default hard-exclude of telemetry; [] → include
        # telemetry when the caller passes the explicit override.
        kind_exclude_list = [] if req.include_telemetry else None

        # #141: empty query → recency-only mode. Skip embedding, query chunks
        # directly ordered by created_at desc, apply types/date_after filter.
        if not req.query.strip():
            recent_limit = limit * 5 if req.min_priority else limit
            recent = store.list_recent(
                types=req.types,
                category=req.category,
                date_after=effective_date_after,
                date_before=req.date_before,
                limit=recent_limit,
                kind_exclude_list=kind_exclude_list,
            )
            # #391: apply type_deny post-fetch (list_recent does allow-filter via
            # types, but has no deny param — mirror the same pattern as below).
            recent = _filter_type_deny(recent, req.type_deny)
            recent = _filter_min_priority(recent, req.min_priority)
            recent = recent[:limit]
            # #352: enrich with snippet so MCP callers stay within budget.
            _enrich_with_snippets(recent, "", _resolve_snippet_max_chars(req.max_chars))
            return recent

        # ADR-008: Augment query with project context before embedding
        embed_query = req.query
        if req.context and config.context.enabled and config.context.embed_augment:
            # Extract project name from entity ref (e.g., "project/palinode" → "palinode")
            project_names = [e.split("/", 1)[-1] for e in req.context if "/" in e]
            if project_names:
                embed_query = f"In the context of {', '.join(project_names)}: {req.query}"

        query_emb = embedder.embed(embed_query)
        if not query_emb:
            return []

        use_hybrid = req.hybrid if req.hybrid is not None else config.search.hybrid_enabled

        # ADR-007 §3.2: demand classification flows to the importance nudge.
        # Default explicit (the historical behavior of every existing caller);
        # an ambient/auto-inject caller passes mode="passive" to suppress the
        # importance nudge while still recording recall_count/last_recalled.
        recall_mode = "passive" if req.mode == "passive" else "explicit"

        # Over-fetch when types filter is in play so we still have a chance of
        # returning `limit` results after the post-fetch type filter (#141/#391).
        store_limit = limit * 5 if (req.types or req.type_deny or req.min_priority) else limit

        if use_hybrid:
            results = store.search_hybrid(
                query_text=req.query,
                query_embedding=query_emb,
                category=req.category,
                top_k=store_limit,
                threshold=req.threshold or config.search.api_threshold,
                hybrid_weight=config.search.hybrid_weight,
                date_after=effective_date_after,
                date_before=req.date_before,
                context_entities=req.context,
                include_daily=bool(req.include_daily),
                kind_exclude_list=kind_exclude_list,
                mode=recall_mode,
                session_id=req.session_id,
            )
        else:
            results = store.search(
                query_embedding=query_emb,
                category=req.category,
                top_k=store_limit,
                threshold=req.threshold or config.search.api_threshold,
                date_after=effective_date_after,
                date_before=req.date_before,
                context_entities=req.context,
                include_daily=bool(req.include_daily),
                kind_exclude_list=kind_exclude_list,
                mode=recall_mode,
                session_id=req.session_id,
            )

        # Apply type filters post-fetch (#141/#391), then trim to caller's limit.
        # type_deny takes precedence: applied after allow-list so a type in both
        # lists is excluded.
        results = _filter_types(results, req.types)
        results = _filter_type_deny(results, req.type_deny)
        results = _filter_min_priority(results, req.min_priority)
        final = results[:limit]

        # #352/#391: per-result snippet enrichment so MCP callers (and any other
        # budget-constrained consumer) can avoid pulling full chunk bodies.
        # `content` is preserved untouched for CLI/API consumers.
        # Per-request max_chars overrides config default when supplied.
        _enrich_with_snippets(final, req.query, _resolve_snippet_max_chars(req.max_chars))

        # Issue #256: emit retrieval events (explicit — came in via /search API).
        # Source attribution: the X-Palinode-Source header tells us the surface
        # (mcp → "palinode_search", cli → "cli_search", api → "api_search").
        _search_source = "api_search"
        if request is not None:
            from palinode.core.defaults import SAVE_SOURCE_HEADER
            hdr = request.headers.get(SAVE_SOURCE_HEADER, "")
            if hdr == "mcp":
                _search_source = "palinode_search"
            elif hdr == "cli":
                _search_source = "cli_search"
        _retrieval_logger.record_search_results(
            final,
            query=req.query,
            source=_search_source,
            mode=recall_mode,
            session_id=req.session_id,
        )
        return final
    except Exception as e:
        raise _safe_500(e, "Search failed")


@router.post("/search-associative")
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

        # #392: per-result snippet enrichment so MCP callers (and any other
        # budget-constrained consumer) can avoid pulling full chunk bodies.
        # `content` is preserved untouched for CLI/API consumers. Mirrors the
        # /search treatment shipped in #359 — the associative path was
        # overlooked there and still returned un-truncated content fields.
        _enrich_with_snippets(results, req.query, config.search.snippet_max_chars)

        return results
    except Exception as e:
        raise _safe_500(e, "Associative search failed")


@router.post("/dedup-suggest")
def dedup_suggest_api(req: DedupSuggestRequest) -> list[dict[str, Any]]:
    """Return existing files semantically near the supplied draft content.

    Preprocessing pipeline (P1 per design doc): strip frontmatter, strip the
    auto-generated `## See also` footer, strip `[[wikilink]]` decoration —
    applied BOTH to the incoming draft AND to each candidate's body before
    re-embedding.  Without this, every note linking the same entities looks
    like a duplicate of every other one.

    Each result carries a ``strong_dup: bool`` flag — true when similarity
    crosses the strong-dup threshold (default 0.90).  The LLM uses this to
    pick "create new" vs "update existing".
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.80
        top_k = req.top_k or 5
        strong_threshold = (
            req.strong_dup_threshold if req.strong_dup_threshold is not None else 0.90
        )

        preprocessed_query = preprocess_for_similarity(req.content)
        if not preprocessed_query:
            return []

        # Initial candidate slate — over-fetched, filter-free.  The caller's
        # min_similarity gates only the post-rerank cosine score, not the
        # initial vector recall.
        query_emb = embedder.embed(preprocessed_query)
        if not query_emb:
            return []
        candidates = _embedding_candidates(query_emb, top_k=top_k, over_fetch=4)

        ranked = _rerank_with_preprocessing(
            query_preprocessed=preprocessed_query,
            candidates=candidates,
            min_similarity=min_similarity,
            top_k=top_k,
        )
        for r in ranked:
            r["strong_dup"] = r["similarity"] >= strong_threshold
        return ranked
    except Exception as e:
        raise _safe_500(e, "Dedup suggest failed")


@router.post("/orphan-repair")
def orphan_repair_api(req: OrphanRepairRequest) -> list[dict[str, Any]]:
    """Return existing files semantically near a broken `[[wikilink]]` target.

    The LLM proposes a redirect (rename the link to point at one of the
    returned files) or creates the missing target with informed context
    (knowing what existing pages are nearby in semantic space).

    The input ``broken_link`` may be the raw link text (``[[alice-meeting]]``)
    or just the target word — both are accepted; the wikilink stripper
    normalizes either form.
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.65
        top_k = req.top_k or 10

        # Accept either `[[name]]` or bare `name`.  Preprocessing handles both.
        preprocessed_query = preprocess_for_similarity(req.broken_link)
        # Replace hyphens with spaces so a slug like ``alice-meeting`` reads
        # as natural language to the embedder.  This is intent-preserving:
        # we want semantic neighbours of the target *concept*.
        preprocessed_query = preprocessed_query.replace("-", " ").replace("_", " ").strip()
        if not preprocessed_query:
            return []

        query_emb = embedder.embed(preprocessed_query)
        if not query_emb:
            return []
        candidates = _embedding_candidates(query_emb, top_k=top_k, over_fetch=4)

        return _rerank_with_preprocessing(
            query_preprocessed=preprocessed_query,
            candidates=candidates,
            min_similarity=min_similarity,
            top_k=top_k,
        )
    except Exception as e:
        raise _safe_500(e, "Orphan repair failed")


@router.post("/cluster-neighbors")
def cluster_neighbors_api(req: ClusterNeighborsRequest) -> list[dict[str, Any]]:
    """Return top-K semantically related files not already linked to/from file_path.

    Extracts all ``[[wikilinks]]`` in the source file and in every other file
    that links TO it, then excludes those already-linked files from the
    candidate slate.  The remaining candidates are re-ranked with the
    preprocessing pipeline (strip frontmatter, auto-footer, wikilink
    decoration) and filtered by ``min_similarity``.

    Designed for the Obsidian wiki-maintenance LLM: surfaces implicit
    relationships that no wikilink yet captures so the LLM can propose
    new cross-links.
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.70
        top_k = req.top_k or 10

        # Read the source file body.
        body = _read_memory_body(req.file_path)
        if body is None:
            return []

        preprocessed = preprocess_for_similarity(body)
        if not preprocessed:
            return []

        # Embed the source file to find semantic neighbours.
        query_emb = embedder.embed(preprocessed)
        if not query_emb:
            return []

        # Collect all files already explicitly linked TO or FROM file_path.
        # "To" = wikilinks inside file_path's body.
        # "From" = files that contain a wikilink to file_path's basename slug.
        linked_slugs: set[str] = set()
        # Slug of the source file (filename without extension).
        source_slug = os.path.splitext(os.path.basename(req.file_path))[0]

        # Extract outgoing links from the source file (raw body, not preprocessed).
        outgoing = set(re.findall(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", body))
        for link in outgoing:
            linked_slugs.add(link.split("/")[-1])

        # Also exclude the source file itself.
        linked_slugs.add(source_slug)

        # Scan all indexed files for incoming links to source_slug.
        db = store.get_db()
        try:
            all_fps = db.execute(
                "SELECT DISTINCT file_path FROM chunks"
            ).fetchall()
        finally:
            db.close()

        incoming_file_paths: set[str] = set()
        link_re = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")
        for (fp,) in all_fps:
            incoming_file_paths.add(fp)
            fp_body = _read_memory_body(fp)
            if fp_body and source_slug in fp_body:
                for m in link_re.finditer(fp_body):
                    if m.group(1).split("/")[-1] == source_slug:
                        linked_slugs.add(os.path.splitext(os.path.basename(fp))[0])
                        break

        # Fetch candidate slate from the vector index.
        candidates = _embedding_candidates(query_emb, top_k=top_k, over_fetch=6)

        # Exclude already-linked files.
        filtered_candidates = [
            c for c in candidates
            if os.path.splitext(os.path.basename(c.get("file_path", "")))[0] not in linked_slugs
            and c.get("file_path", "") != req.file_path
        ]

        ranked = _rerank_with_preprocessing(
            query_preprocessed=preprocessed,
            candidates=filtered_candidates,
            min_similarity=min_similarity,
            top_k=top_k,
        )
        # Rename "similarity" → also expose as "score" per issue spec shape.
        for r in ranked:
            r["score"] = r["similarity"]
        return ranked
    except Exception as e:
        raise _safe_500(e, "Cluster neighbors failed")


@router.post("/topic-coverage")
def topic_coverage_api(req: TopicCoverageRequest) -> dict[str, Any]:
    """Check whether any wiki page already covers a topic phrase.

    Returns ``{covered: bool, best_match: str | None, similarity: float}``
    where ``best_match`` is a relative file_path.  The LLM calls this
    before ingesting new content to avoid creating a page for a topic that
    is already well-covered.

    Uses the same preprocessing pipeline as the other embedding tools so
    that the query is compared against de-noised file bodies.
    """
    try:
        from palinode.core.embedding_preprocess import preprocess_for_similarity

        min_similarity = req.min_similarity if req.min_similarity is not None else 0.78

        # Treat the query phrase like a short document through the pipeline.
        # slug-style phrases ("machine-learning-deployment") become natural
        # language tokens ("machine learning deployment") for better recall.
        preprocessed_query = preprocess_for_similarity(req.query)
        preprocessed_query = preprocessed_query.replace("-", " ").replace("_", " ").strip()
        if not preprocessed_query:
            return {"covered": False, "best_match": None, "similarity": 0.0}

        query_emb = embedder.embed(preprocessed_query)
        if not query_emb:
            return {"covered": False, "best_match": None, "similarity": 0.0}

        candidates = _embedding_candidates(query_emb, top_k=5, over_fetch=4)
        if not candidates:
            return {"covered": False, "best_match": None, "similarity": 0.0}

        ranked = _rerank_with_preprocessing(
            query_preprocessed=preprocessed_query,
            candidates=candidates,
            min_similarity=min_similarity,
            top_k=1,
        )
        if ranked:
            best = ranked[0]
            return {
                "covered": True,
                "best_match": best["file_path"],
                "similarity": best["similarity"],
            }
        return {"covered": False, "best_match": None, "similarity": 0.0}
    except Exception as e:
        raise _safe_500(e, "Topic coverage failed")
