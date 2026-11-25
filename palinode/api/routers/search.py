from __future__ import annotations
import os
import re
import logging
from typing import Any, Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from palinode.core import store, embedder
from palinode.core.config import config
from palinode.core.context_prime import InvalidProjectScope
from palinode.core.operations import CATEGORIES, MEMORY_TYPES, RESOLVE_MODES, TIERS
from palinode.core.path_guard import to_rel_path
from palinode.core.ranker import parse_iso_utc
from palinode.core.scope import search_scope_chain
from palinode.core.search import (
    Caller, RecallDenied, SearchQuery, _fetch_visible, search,
)
from palinode.api._util import WhereEntries, _retrieval_logger, _safe_500
from palinode.api.rate_limit import _RATE_LIMIT_SEARCH, _check_rate_limit
from palinode.api.search_helpers import (
    _embedding_candidates,
    _enrich_with_rel_path,
    _enrich_with_snippets,
    _filter_type_deny,
    _filter_types,
    _read_memory_body,
    _rerank_with_preprocessing,
)
logger = logging.getLogger("palinode.api")
router = APIRouter()


class SearchRequest(BaseModel):
    prompt: bool = False
    query: str
    category: Literal[*CATEGORIES] | None = None
    limit: int | None = Field(
        default=config.search.default_limit, ge=1, le=config.search.max_limit
    )
    # Server-owned default, chosen per caller by core search (ADR-022): MCP's
    # floor for an MCP request, the API's for everything else. None = unset.
    threshold: float | None = None
    hybrid: bool | None = None
    date_after: str | None = None
    date_before: str | None = None
    context: list[str] | None = None  # Entity refs for ambient context boost (ADR-008)
    include_daily: bool | None = False  # Skip daily/ penalty when True
    # ADR-015 §2.3: None → default hard-exclude of telemetry; [] → include
    # telemetry when the caller passes the explicit override.
    include_telemetry: bool | None = False
    # How much of each hit to render — "abstract" (summary-first, ~300
    # chars), "overview" (frontmatter + head of body), or "full". Omitted keeps
    # the snippet + content shape search returned before tiers existed.
    tier: Literal[*TIERS] | None = None
    # filter by memory `type` frontmatter (one of PersonMemory, Decision,
    # ProjectSnapshot, Insight, ResearchRef, ActionItem). Independent of `category`
    # which filters by directory. Applied as a post-fetch filter; pass multiple
    # types to OR them.
    types: list[Literal[*MEMORY_TYPES]] | None = None
    # deny-list complement to `types`. Results whose `type` is in this list
    # are excluded after fetch. Takes precedence: a result present in both `types`
    # and `type_deny` is dropped.
    type_deny: list[str] | None = None
    # Frontmatter `key=value` filters, resolved to chunks before anything ranks.
    where: WhereEntries = None
    min_priority: int | None = Field(default=None, ge=1, le=5)
    # relative recency window. If set, derives an effective `date_after`
    # of `now - since_days` days. Combined with explicit `date_after` by taking
    # the later (more restrictive) of the two.
    since_days: int | None = None
    # per-request snippet cap override. When set (positive int), overrides
    # config.search.snippet_max_chars for this request only. Clamped to [1, 8000].
    max_chars: int | None = None
    # ADR-007 §3.2: demand classification. "explicit" (default) = the agent
    # issued this search because it needed the memory → nudges importance.
    # "passive" = ambient/auto-inject/session-start priming (ADR-008) → the
    # memory was *offered*, not *sought*; recall_count/last_recalled still update
    # but importance is NOT nudged. The explicit-only gate (load-bearing once an
    # ambient caller sets mode="passive") flows down to store.record_recall.
    mode: str | None = None
    # ADR-007 §3.2: session id for per-(chunk, session) nudge deduplication. When
    # provided, importance is reinforced at most once per memory per session.
    session_id: str | None = None
    # Bounded evidence resolution around each hit (palinode.core.evidence):
    # "linked" follows superseded_by / contradicts / backed_by both ways under
    # fixed budgets; "full" adds bounded unlinked discovery. Each hit gains an
    # `evidence` block with its own `coverage`, and a `resolution` block
    # (palinode.core.resolution) saying whether a record stands, the sides are
    # contested, or the evidence is insufficient. Omitted / "none" attaches
    # nothing, so an ordinary request stays byte-identical.
    resolve: Literal[*RESOLVE_MODES] | None = None
    # History on request. With `resolve`, retired records the evidence layer
    # reaches (discovery, replaced predecessors, retired sources) are left out
    # of each hit's evidence by default, as they are out of the hits; true
    # keeps them, each labelled with its `retired` currency.
    include_retired: bool = False
    # Delivery-receipt transport. `/search` returns a bare JSON array and that
    # is a frozen contract, so the receipt cannot simply become a top-level
    # key: with `receipt=true` the response is
    # ``{"results": [...], "receipt": {...}}``, and the `results` array is
    # byte-identical to what the same request returns without the flag. Omitted
    # → today's array, unchanged for every existing caller. Deliberately not a
    # canonical parity param: the *capability* (a delivery receipt) is on every
    # surface, this flag is the REST envelope opt-in, like the `ps` shortcut.
    # The receipt itself is built and logged either way.
    receipt: bool | None = None
    # Passive injection is automatic recall.  Explicit callers may opt in to
    # the same policy classification without changing their result shape.
    automatic: bool = False
    cwd: str | None = None
    source_path: str | None = None
    # A request scoped to a project leaves out records tagged to a different
    # project (records naming no project are global and stay). True delivers
    # them too, each labelled with its own project under ``other_project``.
    # Unset (not False) by default so the receipt's request key is unchanged.
    include_other_projects: bool | None = None

    @field_validator("date_after", "date_before")
    @classmethod
    def _date_bound_is_iso(cls, value: str | None) -> str | None:
        # The window compares parsed instants; a bound that does not parse
        # would otherwise drop out of the filter silently.
        if value is not None and value.strip() and parse_iso_utc(value) is None:
            raise ValueError(
                "must be an ISO-8601 date or timestamp, "
                "e.g. 2026-10-12 or 2026-10-12T20:00:00-05:00"
            )
        return value


class SearchAssociativeRequest(BaseModel):
    query: str
    seed_entities: list[str] | None = None
    limit: int | None = 5
    # The same type filters as /search: an OR allow-list of memory types, and
    # a deny-list that wins over it.
    types: list[Literal[*MEMORY_TYPES]] | None = None
    type_deny: list[str] | None = None


class DedupSuggestRequest(BaseModel):
    """Find existing files semantically near the supplied draft content.

    Used by the LLM at write-time to decide "create new vs update existing".
    Both ``min_similarity`` and ``top_k`` are kwarg-tunable; defaults match
    the BGE-M3 thresholds validated for this search surface.
    """
    content: str
    min_similarity: float | None = 0.80
    top_k: int | None = 5
    # Threshold above which a candidate is flagged ``strong_dup=true`` —
    # "near-paraphrase territory" per the design doc; LLM should usually
    # update rather than create when this fires.
    strong_dup_threshold: float | None = 0.90


class OrphanRepairRequest(BaseModel):
    """Find files semantically near a broken `[[wikilink]]` target.

    The LLM uses the candidate slate to propose a redirect or seed a new
    target file with informed context.  ``min_similarity`` defaults are
    looser than ``dedup_suggest`` because we want a wider candidate slate
    here — the LLM picks one or none.
    """
    broken_link: str
    min_similarity: float | None = 0.65
    top_k: int | None = 10


class ClusterNeighborsRequest(BaseModel):
    """Find semantically related files not already linked to/from file_path.

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
    """Check whether any wiki page already covers a topic phrase.

    The LLM calls this BEFORE ingesting new content to ask "is this
    redundant?".  Different framing from ``dedup_suggest``: the input is a
    short topic phrase (not full draft content), and the return is a simple
    ``{covered, best_match, similarity}`` dict rather than a ranked list.
    Default threshold 0.78 — between dedup (0.80) and cluster (0.70).
    """

    query: str
    min_similarity: float | None = 0.78


@router.post("/search", response_model=None)
def search_api(
    req: SearchRequest, request: Request = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """Configured hybrid/lexical search against derived `.palinode.db` chunks.

    Transport for :func:`palinode.core.search.search`, which owns the policy:
    this parses the body, names the caller from the ``X-Palinode-Source``
    header, rate-limits, and maps the core's refusals to status codes.

    Empty query routes to recency-only mode: returns the most recent
    chunks ordered by created_at desc, optionally filtered by `types` and
    `since_days`. Skips embedding entirely.

    Returns:
        list[dict[str, Any]]: List payload sequence matching the criteria
        boundaries — or, when ``receipt=true``, ``{"results": [...],
        "receipt": {...}}`` with that same list under ``results`` and the
        delivery receipt (:mod:`palinode.core.receipt`) beside it.

    # Security audit (I2, 2026-04-30):
    # - All SQL goes through store.search_hybrid / store.list_recent /
    #   store.list_chunks_matching; every cursor.execute() in those helpers
    #   uses ? placeholders (no f-string interpolation of user input into SQL).
    # - No LLM call on the search path — only embedder.embed() on the query
    #   string, which is a vector model and not a prompt injection surface.
    # - Result sets are bounded: req.limit (config.search.default_limit when
    #   unset, capped at config.search.max_limit), with an internal over-fetch
    #   factor of 5x when a post-fetch filter is active, itself bounded by the
    #   underlying SQL LIMIT clause.
    # - FTS5 query string is sanitized via store.sanitize_fts_query()
    #   before MATCH, defending against operator-injection (`OR`, `NEAR`).
    """
    query, caller = _search_inputs(req, request)
    if request:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip, "search", _RATE_LIMIT_SEARCH):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    try:
        result = search(query, caller)
    except RecallDenied as denied:
        raise HTTPException(status_code=403, detail=denied.reason)
    except (InvalidProjectScope, embedder.EmbeddingInputError, embedder.EmbeddingUnavailable):
        raise  # typed 400 / 422 / 503 via the app-level handlers in server.py
    except Exception as e:
        raise _safe_500(e, "Search failed")
    return result.envelope() if req.receipt else result.rows


def _search_inputs(req: SearchRequest, request: Request | None) -> tuple[SearchQuery, Caller]:
    """The core's two inputs for a /search body: what is asked, and who asks."""
    from palinode.core.defaults import SAVE_SOURCE_HEADER

    header = request.headers.get(SAVE_SOURCE_HEADER, "") if request is not None else ""
    query = SearchQuery(
        query=req.query, category=req.category, limit=req.limit,
        date_after=req.date_after, date_before=req.date_before,
        include_daily=bool(req.include_daily), include_telemetry=bool(req.include_telemetry),
        include_other_projects=req.include_other_projects, since_days=req.since_days,
        types=req.types, where=req.where, min_priority=req.min_priority,
        threshold=req.threshold, tier=req.tier, resolve=req.resolve,
        include_retired=req.include_retired, type_deny=req.type_deny, hybrid=req.hybrid,
        max_chars=req.max_chars, prompt=req.prompt,
    )
    caller = Caller(
        source=header if header in ("mcp", "cli") else "api",
        session_id=req.session_id, context=req.context, cwd=req.cwd,
        source_path=req.source_path, automatic=req.automatic, mode=req.mode,
        log=_retrieval_logger,
    )
    return query, caller


@router.post("/search-associative")
def search_associative_api(req: SearchAssociativeRequest) -> list[dict[str, Any]]:
    """Entity graph spreading activation recall.

    Subject to the same ADR-009 Layer 2 visibility gate as ``/search``.
    Entity-graph traversal reaches memories no query term matched, so leaving
    it ungated would have made it the way around every rule ``/search``
    enforces. The gate reads each file's live frontmatter rather than trusting
    the row.

    ``types`` and ``type_deny`` filter on each row's frontmatter ``type`` as
    they do on ``/search``. They are applied inside the paging window, so
    rows they remove cannot crowd out the matches ranked below them.
    """
    try:
        seed_entities = req.seed_entities
        if not seed_entities:
            seed_entities = store.detect_entities_in_text(req.query)

        limit = req.limit or 5
        # No ambient context on this request shape, so the chain comes from
        # env identity alone (agent/harness/member/org).
        results = _fetch_visible(
            search_scope_chain(),
            lambda n: store.search_associative(
                query_text=req.query,
                seed_entities=seed_entities,
                top_k=n,
            ),
            limit,
            eligible=lambda rows: _filter_type_deny(
                _filter_types(rows, req.types), req.type_deny,
            ),
        )[:limit]

        # per-result snippet enrichment so MCP callers (and any other
        # budget-constrained consumer) can avoid pulling full chunk bodies.
        # `content` is preserved untouched for CLI/API consumers. Mirrors the
        # /search treatment shipped in the associative path was
        # overlooked there and still returned un-truncated content fields.
        _enrich_with_snippets(results, req.query, config.search.snippet_max_chars)
        _enrich_with_rel_path(results)

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
    except embedder.EmbeddingInputError:
        raise  # typed 422 via the app-level handler in server.py
    except embedder.EmbeddingUnavailable:
        raise  # typed 503 via the app-level handler in server.py
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
    except embedder.EmbeddingInputError:
        raise  # typed 422 via the app-level handler in server.py
    except embedder.EmbeddingUnavailable:
        raise  # typed 503 via the app-level handler in server.py
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
        with store.reader() as reader:
            all_fps = reader.indexed_paths()

        incoming_file_paths: set[str] = set()
        link_re = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")
        for fp in all_fps:
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
    except embedder.EmbeddingInputError:
        raise  # typed 422 via the app-level handler in server.py
    except embedder.EmbeddingUnavailable:
        raise  # typed 503 via the app-level handler in server.py
    except Exception as e:
        raise _safe_500(e, "Cluster neighbors failed")


@router.post("/topic-coverage")
def topic_coverage_api(req: TopicCoverageRequest) -> dict[str, Any]:
    """Check whether any wiki page already covers a topic phrase.

    Returns ``{covered: bool, best_match: str | None, rel_path: str | None,
    similarity: float}``. ``best_match`` is the absolute filesystem path
    (matching ``file_path`` elsewhere); ``rel_path`` is the same file
    relative to ``memory_dir``, for callers (MCP, CLI) that want the
    display/round-trippable spelling without guessing it themselves. The LLM
    calls this before ingesting new content to avoid creating a page for a
    topic that is already well-covered.

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
                "rel_path": best.get("rel_path", to_rel_path(best["file_path"])),
                "similarity": best["similarity"],
            }
        return {"covered": False, "best_match": None, "similarity": 0.0}
    except embedder.EmbeddingInputError:
        raise  # typed 422 via the app-level handler in server.py
    except embedder.EmbeddingUnavailable:
        raise  # typed 503 via the app-level handler in server.py
    except Exception as e:
        raise _safe_500(e, "Topic coverage failed")
