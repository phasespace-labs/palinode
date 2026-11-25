"""Search: the one retrieval path every surface calls.

``search(query, caller) -> SearchResult``. The REST route, the inspector UI and
the ``/resolve`` bundle all call it, so scope, the capture policy, the vector
floor, visibility, the filters, evidence, the receipt, the confidence verdict,
recall write-back and the retrieval log are decided in one place.

Two inputs, split by question:

* :class:`SearchQuery` is **what** is asked: the ``search`` operation spec's
  params (:data:`palinode.core.operations.SEARCH`) plus the options that shape
  which rows come back. A test checks it against the spec.
* :class:`Caller` is **who** asks and what the search may do on its behalf:
  the surface (which picks the floor and names the log source), the session,
  the scope inputs, whether the recall is automatic, and whether it records
  recall and writes the retrieval log.

The route owns transport only: parsing, rate limiting, mapping
:class:`RecallDenied` to 403, and choosing between :attr:`SearchResult.rows`
(the frozen bare array) and :meth:`SearchResult.envelope`.

``store.search_hybrid`` and ``ranker.rank_hybrid`` are this module's internal
seams; nothing above it reassembles their policy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Literal

from palinode.core import embedder, store
from palinode.core.config import config
from palinode.core.scope import effective_project_scope, search_scope_chain
from palinode.core.search_rows import (
    _apply_tier,
    _compute_effective_date_after,
    _enrich_with_rel_path,
    _enrich_with_snippets,
    _filter_min_priority,
    _filter_type_deny,
    _filter_types,
    _resolve_snippet_max_chars,
)
from palinode.core.visibility import is_row_visible
from palinode.core.where import parse_where

if TYPE_CHECKING:
    from palinode.core.context_prime import ProjectResolution
    from palinode.core.receipt import Receipt
    from palinode.core.retrieval_log import RetrievalLogger
    from palinode.core.scope import ScopeChain

logger = logging.getLogger("palinode.api")

#: How much wider to re-fetch when visibility filtering starved a result
#: window. Only paid when a hidden memory actually landed in the window.
_VISIBILITY_OVERFETCH = 5

#: Row fields kept off the wire: which query units a keyword row matched is the
#: confidence verdict's evidence (:mod:`palinode.core.confidence`), not part of
#: the result.
_INTERNAL_ROW_FIELDS = frozenset({"keyword_units"})

Surface = Literal["mcp", "cli", "api", "ui", "resolve"]

#: The retrieval log's ``source`` per surface. The inspector UI has always
#: logged as an API search, and still does.
_LOG_SOURCE: dict[str, str] = {
    "mcp": "palinode_search", "cli": "cli_search", "api": "api_search", "ui": "api_search",
}


def _as_tuple(value: Any) -> tuple[str, ...] | None:
    return None if value is None else tuple(value)


@dataclass(frozen=True)
class SearchQuery:
    """What a search asks for.

    The first sixteen fields are the ``search`` spec's params, by the same
    names; the rest shape which rows come back and are not on every surface.
    ``limit`` is ``None`` for the server's default.
    """

    query: str
    category: str | None = None
    limit: int | None = None
    date_after: str | None = None
    date_before: str | None = None
    include_daily: bool = False
    include_telemetry: bool = False
    include_other_projects: bool | None = None
    since_days: int | None = None
    types: tuple[str, ...] | None = None
    where: tuple[str, ...] | None = None
    min_priority: int | None = None
    threshold: float | None = None
    tier: str | None = None
    resolve: str | None = None
    include_retired: bool = False
    # Row-shaping options beyond the spec.
    type_deny: tuple[str, ...] | None = None
    hybrid: bool | None = None
    max_chars: int | None = None
    prompt: bool = False

    def __post_init__(self) -> None:
        for name in ("types", "where", "type_deny"):
            object.__setattr__(self, name, _as_tuple(getattr(self, name)))


_UNRESOLVED: Any = object()


@dataclass(frozen=True)
class Caller:
    """Who asks, and what the search may do on its behalf.

    Scope comes from the raw inputs (``context``, ``cwd``, ``source_path``),
    resolved here through the one shared resolver, or, for a caller that has
    already resolved it, from :meth:`from_chain`. Never both.

    ``record`` turns on recall write-back (ADR-007) and, when ``log`` is
    given, the retrieval log. ``keyword_fallback`` lets a caller that must not
    fail on an unreachable embedder take the keyword arm instead; the result
    says so in ``active_mode``.
    """

    source: Surface = "api"
    session_id: str | None = None
    context: tuple[str, ...] | None = None
    cwd: str | None = None
    source_path: str | None = None
    automatic: bool = False
    mode: str | None = None
    record: bool = True
    log: RetrievalLogger | None = field(default=None, compare=False, repr=False)
    keyword_fallback: bool = False
    chain: Any = field(default=_UNRESOLVED, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", _as_tuple(self.context))
        if self.resolved and (self.context is not None or self.cwd or self.source_path):
            raise ValueError("a Caller takes raw scope inputs or a resolved chain, not both")

    @classmethod
    def from_chain(
        cls, chain: ScopeChain | None, *, source: Surface, record: bool = False,
        keyword_fallback: bool = False,
    ) -> Caller:
        """A caller that resolved its scope chain itself (the ``/resolve`` bundle)."""
        return cls(source=source, chain=chain, record=record, keyword_fallback=keyword_fallback)

    @property
    def resolved(self) -> bool:
        return self.chain is not _UNRESOLVED

    @property
    def is_automatic(self) -> bool:
        """Automatic recall: an ambient caller, or one that said so."""
        return self.automatic or self.mode == "passive"


class RecallDenied(Exception):
    """The capture policy refuses this automatic recall; ``reason`` says why."""

    def __init__(self, reason: str | None) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SearchResult:
    """One delivery: the rows, and everything said about them.

    ``hits`` are the rows as ranked, internal fields included (the bundle's
    confidence verdict reads them); :attr:`rows` is the wire form.
    """

    hits: list[dict[str, Any]]
    receipt: Receipt
    active_mode: str
    scope: ProjectResolution
    chain: ScopeChain | None
    resolve: str | None = None
    confidence: dict[str, Any] | None = None
    other_projects_withheld: int = 0
    #: Rows access control withheld (not other projects') among those the
    #: search passed over to fill the slate.
    hidden_withheld: int = 0
    #: The window stopped at its cap, so the withheld counts are lower bounds.
    withheld_bounded: bool = False
    filter_exclusion: dict[str, Any] | None = None

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The frozen bare array ``/search`` returns."""
        return [
            {key: value for key, value in row.items() if key not in _INTERNAL_ROW_FIELDS}
            if isinstance(row, dict) else row
            for row in self.hits
        ]

    def envelope(self) -> dict[str, Any]:
        """The receipt envelope: ``{"results", "receipt", "project", "project_resolved_by"}``.

        With ``resolve`` on, the envelope carries the receipt's **public** view —
        refs, exact revisions, dispositions, lineage, coverage, scope, times; never
        memory content and never the caller's query. With ``resolve`` off there is
        no evidence to qualify, so it carries the compact reference
        (``bundle_id`` + ``evaluated_at``). Both shapes add caller-visible retrieval
        diagnostics, including on an empty result, without exposing global counts.

        The envelope also names the project this delivery was scoped to and the
        source that decided it, under the same ``project`` /
        ``project_resolved_by`` keys ``/context/prime`` already returns — so a
        caller can see *why* a search was scoped the way it was without a second
        request.

        The diagnostics carry this delivery's ``confidence`` verdict and the arm
        evidence behind it, alongside the ``outcome`` they always carried: rows
        came back is one question, any of them being worth trusting is another.

        A project-scoped delivery also says how many records tagged to other
        projects it withheld (``other_projects_withheld``), so a scoped empty
        result reads as "isolated", not as "nothing in memory". The count
        covers every such record the search passed over to fill the slate;
        ``other_projects_withheld_at_least: true`` marks it a lower bound,
        when the window reached its cap before the slate filled.

        An empty delivery that ``where`` or ``types`` emptied says so under
        ``filter_exclusion`` (:func:`_filter_exclusion`), so it does not read as
        "nothing matched this query". Absent otherwise.
        """
        from palinode.core.retrieval import search_diagnostics

        rows = self.rows
        resolving = bool(self.resolve and self.resolve != "none")
        view = self.receipt.public() if resolving else self.receipt.reference()
        view["retrieval"] = search_diagnostics(
            self.active_mode, matched=bool(rows), chain=self.chain, confidence=self.confidence,
        )
        view["retrieval"]["other_projects_withheld"] = self.other_projects_withheld
        if self.withheld_bounded:
            view["retrieval"]["other_projects_withheld_at_least"] = True
        # The server's abstention policy, which MCP applies to its rendering. MCP
        # may run on another machine with no config of its own, so it reads the
        # setting here rather than from its local config.
        view["retrieval"]["abstain_on_no_confident_match"] = bool(
            config.search.abstain_on_no_confident_match
        )
        if self.filter_exclusion is not None:
            view["retrieval"]["filter_exclusion"] = self.filter_exclusion
        envelope: dict[str, Any] = {"results": rows, "receipt": view}
        envelope.update(self.scope.fields())
        return envelope


# ── the search ───────────────────────────────────────────────────────────────


def search(query: SearchQuery, caller: Caller) -> SearchResult:
    """Run one search for ``caller``.

    An empty ``query.query`` is recency mode: the most recent chunks, newest
    first, under the same filters, with no embedding.

    Raises :class:`RecallDenied` when the capture policy refuses an automatic
    recall (before the query reaches embedding or the log), and the embedder's
    typed errors unless ``caller.keyword_fallback`` absorbs them.
    """
    from palinode.core.context_prime import ProjectResolution

    if caller.resolved:
        # The caller resolved scope and owns the policy decision that went with it.
        scope = ProjectResolution(None, "none")
        context = None
        chain = caller.chain
    else:
        # Resolved once through the shared resolver: the caller's own
        # ``context`` when it sent one, else the pinned setting, so what the
        # delivery reports is what it applied. An explicit ``context``
        # (including an empty one) is never overridden, except by this
        # server's ``context.enabled: false``.
        scope = effective_project_scope(caller.context)
        context = scope.context if caller.context is None or scope.basis == "disabled" else (
            list(caller.context)
        )
        _check_recall_policy(caller, scope)
        # ADR-009 Layer 2: `None` = no scope identity → access control only.
        chain = search_scope_chain(
            context=context, session_id=caller.session_id,
            include_other_projects=bool(query.include_other_projects),
        )
    return _Search(query, caller, scope=scope, context=context, chain=chain).run()


def _check_recall_policy(caller: Caller, scope: ProjectResolution) -> None:
    from palinode.core.capture_policy import evaluate_capture_policy

    decision = evaluate_capture_policy(
        "recall",
        automatic=caller.is_automatic,
        cwd=caller.cwd,
        project=scope.project.removeprefix("project/") if scope.project else None,
        source_path=caller.source_path,
    )
    if not decision.allowed:
        # The query must never reach embedding, retrieval logging, or an error
        # body when an automatic recall is denied.
        raise RecallDenied(decision.reason)


def _default_threshold(source: str) -> float:
    """The vector floor for a search that set none: the server's, per caller.

    The two floors were measured separately (``SearchConfig``'s docstring):
    ``mcp_threshold`` for agent recall over MCP, ``api_threshold`` for every
    other caller. The server owns both, so it picks by the surface the request
    came from rather than letting a client send its own local value (ADR-022 §3).
    """
    return config.search.mcp_threshold if source == "mcp" else config.search.api_threshold


class _Search:
    """One run of :func:`search`; the state its steps share."""

    def __init__(self, query: SearchQuery, caller: Caller, *, scope, context, chain) -> None:
        self.q = query
        self.caller = caller
        self.scope = scope
        self.context = context
        self.chain = chain
        self.text = query.query
        self.limit = query.limit or config.search.default_limit
        self.automatic = caller.is_automatic
        self.date_after = _compute_effective_date_after(query)
        # ADR-015 §2.3: None → default hard-exclude of telemetry; [] → include.
        self.kind_exclude_list = [] if query.include_telemetry else None
        self.where_conditions = parse_where(list(query.where) if query.where else None)
        self.where_matches_nothing = False
        self.where_chunk_ids: list[str] | None = None
        self.where_withheld: set[str] = set()
        self.other_withheld: set[str] = set()
        self.window: dict[str, Any] = {}
        self.active_mode = "lexical" if config.search.retrieval_mode == "lexical" else "hybrid"

    # ── the request the receipt records ──────────────────────────────────────

    def request_record(self) -> dict[str, Any]:
        """The request as the receipt records it: today's REST body after resolution.

        The same keys ``SearchRequest.model_dump(exclude_none=True)`` gave, with
        the applied ``context`` and the shaped ``query``; the receipt keeps only
        what selects (:func:`palinode.core.receipt.request_scope`).
        """
        q, c = self.q, self.caller
        record: dict[str, Any] = {
            "prompt": q.prompt, "query": self.text, "category": q.category,
            "limit": q.limit, "threshold": q.threshold, "hybrid": q.hybrid,
            "date_after": q.date_after, "date_before": q.date_before,
            "context": None if self.context is None else list(self.context),
            "include_daily": q.include_daily, "include_telemetry": q.include_telemetry,
            "tier": q.tier, "types": None if q.types is None else list(q.types),
            "type_deny": None if q.type_deny is None else list(q.type_deny),
            "where": None if q.where is None else list(q.where),
            "min_priority": q.min_priority, "since_days": q.since_days,
            "max_chars": q.max_chars, "mode": c.mode, "session_id": c.session_id,
            "resolve": q.resolve, "include_retired": q.include_retired,
            "automatic": c.automatic, "cwd": c.cwd, "source_path": c.source_path,
            "include_other_projects": q.include_other_projects,
        }
        return {key: value for key, value in record.items() if value is not None}

    # ── steps ────────────────────────────────────────────────────────────────

    def run(self) -> SearchResult:
        q = self.q
        if q.prompt:
            from palinode.core.prompt_query import shape_prompt

            self.text = shape_prompt(self.text)
            if not self.text:
                return self._result([])
        # `where` is resolved to chunk ids before anything ranks, so a match is
        # never lost for ranking low; no visible match needs neither an
        # embedding nor a query.
        if self.where_conditions:
            matches = store.list_chunks_matching(self.where_conditions)
            self.where_chunk_ids = [row["id"] for row in matches]
            self.where_matches_nothing = not _has_visible_match(
                self.chain, matches, self.where_withheld,
            )
        if not self.text.strip():
            return self._recent()
        return self._ranked()

    def _recent(self) -> SearchResult:
        """Recency mode: newest chunks first, no embedding."""
        q = self.q
        self.active_mode = "recency"
        recent_limit = self.limit * 5 if q.min_priority else self.limit
        rows = [] if self.where_matches_nothing else _fetch_visible(
            self.chain,
            lambda n, **kw: store.list_recent(
                types=list(q.types) if q.types else None, category=q.category,
                date_after=self.date_after, date_before=q.date_before, limit=n,
                kind_exclude_list=self.kind_exclude_list, chunk_ids=self.where_chunk_ids,
            ),
            recent_limit,
            want=self.limit,
            # list_recent allow-filters types but has no deny param.
            eligible=lambda rows: _filter_min_priority(
                _filter_type_deny(rows, _list(q.type_deny)), q.min_priority,
            ),
            other_projects=self.other_withheld,
            stats=self.window,
            automatic=self.automatic,
        )
        rows = rows[:self.limit]
        exclusion = None if rows or not self._explains_filters() else self._exclusion(
            rerun=lambda n, **kw: store.list_recent(
                category=q.category, date_after=self.date_after,
                date_before=q.date_before, limit=n,
                kind_exclude_list=self.kind_exclude_list,
            ),
            fetch_limit=recent_limit,
        )
        _enrich_with_snippets(rows, "", _resolve_snippet_max_chars(q.max_chars))
        _apply_tier(rows, q.tier)
        _enrich_with_rel_path(rows)
        _label_other_projects(rows, self.chain)
        _attach_evidence(rows, q, self.chain, automatic=self.automatic)
        # The recency path has no verdict of its own and writes no log row.
        return self._result(rows, exclusion=exclusion, assess=False)

    def _ranked(self) -> SearchResult:
        q, caller = self.q, self.caller
        query_emb = self._embed()
        use_hybrid = q.hybrid if q.hybrid is not None else config.search.hybrid_enabled
        # ADR-007 §3.2: an ambient caller passes mode="passive" to suppress the
        # importance nudge while still recording recall_count/last_recalled.
        recall_mode = "passive" if caller.mode == "passive" else "explicit"
        # Over-fetch when a post-fetch filter is in play, so `limit` survives it.
        store_limit = self.limit * 5 if (q.types or q.type_deny or q.min_priority) else self.limit
        # `is not None`: an explicit threshold=0.0 is a legitimate "no floor".
        threshold = q.threshold if q.threshold is not None else _default_threshold(caller.source)
        # The ranker's floors use the gate's own rule, so a withheld best match cannot set one.
        visible = partial(is_row_visible, self.chain, cache={})
        base: dict[str, Any] = dict(
            query_text=self.text, category=q.category,
            date_after=self.date_after, date_before=q.date_before,
            context_entities=self.context, include_daily=bool(q.include_daily),
            kind_exclude_list=self.kind_exclude_list, mode=recall_mode,
            session_id=caller.session_id, visible=visible,
        )
        if query_emb is None:
            arm = dict(query_embedding=None)
        elif use_hybrid:
            arm = dict(query_embedding=query_emb, threshold=threshold,
                       hybrid_weight=config.search.hybrid_weight)
        else:
            # Vector-only goes through search_hybrid(use_fts=False), so it gets
            # the same decay re-rank, priority nudge, context boost, daily
            # penalty, per-file dedup and date window as hybrid.
            arm = dict(query_embedding=query_emb, threshold=threshold, use_fts=False)
        tag_mode = query_emb is None

        def run(n: int, record_access: bool = True,
                chunk_ids: list[str] | None = self.where_chunk_ids) -> list[dict[str, Any]]:
            hits = store.search_hybrid(
                **base, **arm, top_k=n, chunk_ids=chunk_ids,
                record_access=record_access and caller.record,
            )
            if tag_mode:
                for hit in hits:
                    hit["mode"] = self.active_mode
            return hits

        # ADR-007 write-back is deferred until the delivered rows are known,
        # recording each of them once.
        record_recall = (
            (lambda ids: store.record_recall(ids, mode=recall_mode, session_id=caller.session_id))
            if caller.record else None
        )
        rows = [] if self.where_matches_nothing else _fetch_visible(
            self.chain, run, store_limit,
            want=self.limit,
            eligible=self._post_filters,
            record_recall=record_recall,
            suppress_access=not caller.record,
            other_projects=self.other_withheld,
            stats=self.window,
            automatic=self.automatic,
        )
        final = rows[:self.limit]
        exclusion = None if final or not self._explains_filters() else self._exclusion(
            rerun=lambda n, **kw: run(n, record_access=False, chunk_ids=None),
            fetch_limit=store_limit,
        )
        if self.active_mode == "hybrid" and not use_hybrid:
            self.active_mode = "vector"
        for hit in final:
            hit["retrieval_mode"] = self.active_mode
        # `content` is kept whole; `snippet` keeps budget-bound readers small.
        _enrich_with_snippets(final, self.text, _resolve_snippet_max_chars(q.max_chars))
        _apply_tier(final, q.tier)
        _enrich_with_rel_path(final)
        _label_other_projects(final, self.chain)
        # Evidence runs over the final list, after truncation: a linked
        # correction rides on its seed, so no window can cut it off.
        _attach_evidence(final, q, self.chain, automatic=self.automatic)
        result = self._result(final, exclusion=exclusion)
        if caller.record and caller.log is not None:
            # Written last, so each row carries the receipt this delivery
            # produced. The logged set is the plain hit list, never its evidence.
            caller.log.record_search_results(
                final, query=self.text, source=_LOG_SOURCE.get(caller.source, "api_search"),
                mode=recall_mode, session_id=caller.session_id, receipt=result.receipt,
                confidence=result.confidence["confidence"] if result.confidence else None,
            )
        return result

    def _post_filters(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The metadata filters applied after the fetch.

        ``type_deny`` is applied after the allow-list, so a type in both is excluded.
        """
        q = self.q
        rows = _filter_types(rows, _list(q.types))
        rows = _filter_type_deny(rows, _list(q.type_deny))
        return _filter_min_priority(rows, q.min_priority)

    def _embed(self) -> list[float] | None:
        """The query embedding, or ``None`` for the keyword arm alone."""
        if self.active_mode == "lexical" or self.where_matches_nothing:
            return None
        # ADR-008: augment the query with the project context before embedding.
        embed_query = self.text
        if self.context and config.context.enabled and config.context.embed_augment:
            names = [e.split("/", 1)[-1] for e in self.context if "/" in e]
            if names:
                embed_query = f"In the context of {', '.join(names)}: {self.text}"
        try:
            query_emb = embedder.embed(embed_query)
        except embedder.EmbeddingInputError as e:
            # Per-input rejection (e.g. bge-m3 emitting NaN for this string):
            # the backend is healthy, so degrade this one query to the keyword
            # arm. text_len only — the log never carries the query text.
            logger.warning(
                "query embed rejected; keyword fallback op=search "
                "outcome=keyword_fallback text_len=%d error=%r",
                e.text_len, e.ollama_message,
            )
            self.active_mode = "keyword-fallback"
            return None
        except embedder.EmbeddingUnavailable as e:
            if not self.caller.keyword_fallback:
                raise
            logger.info("search: embedder unavailable, keyword-only (%r)", e)
            self.active_mode = "keyword-fallback"
            return None
        if query_emb is not None and not query_emb:
            if self.caller.keyword_fallback:
                self.active_mode = "keyword-fallback"
                return None
            raise RuntimeError("Embedder returned an empty query vector")
        return query_emb

    def _explains_filters(self) -> bool:
        return bool(self.where_conditions or self.q.types)

    def _exclusion(self, *, rerun, fetch_limit: int) -> dict[str, Any] | None:
        return _filter_exclusion(
            self.q, chain=self.chain, where_matches_nothing=self.where_matches_nothing,
            where_chunk_ids=self.where_chunk_ids, rerun=rerun, fetch_limit=fetch_limit,
            automatic=self.automatic,
        )

    def _result(self, rows: list[dict[str, Any]], *, exclusion=None,
                assess: bool = True) -> SearchResult:
        from palinode.core.confidence import assess as assess_confidence
        from palinode.core.receipt import build_receipt

        receipt = build_receipt(
            rows,
            request=self.request_record(),
            scope=self.chain.as_list() if self.chain is not None else (),
            resolve_mode=self.q.resolve or "none",
            surface="search",
            memory_dir=config.memory_dir,
        )
        withheld = self.where_withheld if self.where_matches_nothing else self.other_withheld
        return SearchResult(
            hits=rows, receipt=receipt, active_mode=self.active_mode, scope=self.scope,
            chain=self.chain, resolve=self.q.resolve,
            confidence=assess_confidence(rows, active_mode=self.active_mode) if assess else None,
            other_projects_withheld=len(withheld),
            withheld_bounded=bool(self.window.get("bounded")) and not self.where_matches_nothing,
            hidden_withheld=self.window.get("hidden", 0),
            filter_exclusion=exclusion,
        )


def _list(value: tuple[str, ...] | None) -> list[str] | None:
    return None if value is None else list(value)


# ── visibility ───────────────────────────────────────────────────────────────


def _window_cap() -> int:
    """The widest a window grows while it pages: the largest slate, five times over."""
    return config.search.max_limit * _VISIBILITY_OVERFETCH


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row.get("id"), row.get("file_path"), row.get("section_id"))


def _fetch_visible(
    chain,
    run,
    base_limit: int,
    *,
    want: int | None = None,
    eligible: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    record_recall: Callable[[list[str]], None] | None = None,
    suppress_access: bool = False,
    other_projects: set[str] | None = None,
    stats: dict[str, Any] | None = None,
    automatic: bool = False,
) -> list[dict[str, Any]]:
    """Fetch rows the caller may receive: visible, current when automatic, and ``eligible``.

    ``run(n)`` performs the bounded store fetch. Visibility cannot be pushed
    into SQL — the authoritative frontmatter lives in the file, not in
    ``chunks.metadata`` (which the indexer's unchanged-content fast path
    leaves stale after a frontmatter-only edit) — and the ``eligible``
    post-filters (types, type_deny, min_priority) read the same metadata, so
    both are applied after the fetch, and the rows they remove can fill the
    window ahead of matches ranked below it.

    The window therefore **pages**: while fewer than ``want`` rows survive, it
    is fetched again ``_VISIBILITY_OVERFETCH`` times wider, until enough
    survive, the store returns fewer rows than asked (nothing lies behind it),
    or the window reaches :func:`_window_cap`. When the first window is
    enough — the overwhelmingly common case — this is a single fetch with
    today's exact limits and today's exact ranking.

    When a ``record_recall`` callback is supplied (hybrid/vector search), every
    fetch suppresses store-level recording, and afterwards each distinct valid
    ID among the first ``want`` surviving rows (the ones the caller is
    delivered) is recorded once, so a row the caller never received — hidden,
    retired, filtered out or past the limit — acquires no recall metadata. The
    callback carries mode and session_id for ADR-007's per-session importance
    deduplication. Callers without recall write-back (recency, associative)
    use plain fetches; ``suppress_access`` asks the store to record nothing.

    ``other_projects`` collects the records project isolation withheld, and
    ``stats["hidden"]`` how many rows access control withheld, among the rows
    ranked above the last one delivered — every row the search passed over to
    fill the slate. ``stats["bounded"]`` is true when the cap stopped the
    paging, so those counts are lower bounds.
    """
    from palinode.core.visibility import filter_visible, normalize_memory_path

    want = base_limit if want is None else want
    keep = eligible or (lambda rows: rows)
    cap = max(base_limit, _window_cap())
    fetch_options = {"record_access": False} if (record_recall is not None or suppress_access) else {}

    def gate(fetched: list[dict[str, Any]]) -> list[dict[str, Any]]:
        visible = filter_visible(chain, fetched, other_projects=set())
        if automatic:
            # Live lifecycle classification, including archive location and
            # frontmatter-only retirement behind an unchanged index body.
            visible = [r for r in store.check_freshness(visible)
                       if r.get("currency") != "retired"
                       and r.get("currency_reason") not in {"source missing", "source unreadable"}]
        return keep(visible)

    n = base_limit
    while True:
        fetched = run(n, **fetch_options)
        rows = gate(fetched)
        exhausted = len(fetched) < n
        if len(rows) >= want or exhausted or n >= cap:
            break
        n = min(n * _VISIBILITY_OVERFETCH, cap)
    delivered = rows[:want]

    # What the search passed over: the fetched rows ranked above the last one
    # delivered, or the whole window when the slate came up short.
    cut = len(fetched)
    if delivered and len(rows) >= want:
        last = _row_key(delivered[-1])
        cut = next((i + 1 for i, r in enumerate(fetched) if _row_key(r) == last), cut)
    passed = fetched[:cut]
    isolated: set[str] = set()
    seen = filter_visible(chain, passed, other_projects=isolated)
    if other_projects is not None:
        other_projects.clear()
        other_projects.update(isolated)
    if stats is not None:
        def rel(row: dict[str, Any]) -> str:
            path = str(row.get("file_path") or "")
            return normalize_memory_path(path) or path

        shown = {_row_key(r) for r in seen}
        stats["hidden"] = sum(
            1 for r in passed if _row_key(r) not in shown and rel(r) not in isolated
        )
        stats["bounded"] = len(rows) < want and not exhausted
        stats["exhausted"] = exhausted

    if record_recall is not None:
        ids = dict.fromkeys(
            row["id"] for row in delivered
            if isinstance(row.get("id"), str) and row["id"]
        )
        record_recall(list(ids))
    return rows


def _has_visible_match(chain, matches: list[dict[str, Any]], other_projects: set[str]) -> bool:
    """Is any ``where`` match a record a session on ``chain`` may see?

    Answers by the live visibility rule search applies to every row. When none is,
    ``other_projects`` collects the matches project isolation withheld.
    """
    from palinode.core.visibility import filter_visible

    cache: dict[str, dict[str, Any]] = {}
    if any(is_row_visible(chain, row, cache=cache) for row in matches):
        return True
    filter_visible(chain, matches, other_projects=other_projects)
    return False


def _keys_absent_in_scope(chain, keys: list[str]) -> list[str]:
    """The ``where`` keys that no record a session on ``chain`` may see has.

    Judged by the visibility rule search applies to every row, so a key that
    only private records or other projects' records carry reads as absent:
    the reply must not say what isolation withheld.
    """
    cache: dict[str, dict[str, Any]] = {}
    return [
        key for key in dict.fromkeys(keys)
        if not any(is_row_visible(chain, row, cache=cache)
                   for row in store.list_chunks_with_key(key))
    ]


def _filter_exclusion(
    q: SearchQuery,
    *,
    chain,
    where_matches_nothing: bool,
    where_chunk_ids: list[str] | None,
    rerun: Callable[..., list[dict[str, Any]]],
    fetch_limit: int,
    automatic: bool,
) -> dict[str, Any] | None:
    """Did ``where`` or ``types`` empty this delivery, rather than the query?

    Called only for an empty delivery that had either filter. ``None`` when
    the search finds nothing without them either: then the query, or another
    filter, is the reason, and the reply says what it always said.

    When no record the caller may see satisfies ``where``, that is the answer
    and nothing is run: the delivery skipped the query for that reason, and
    the reply names the keys no record in scope carries. Otherwise ``rerun``
    repeats the search without ``where`` and ``types`` (every other filter,
    the visibility gate included, still applies) and records no recall, and
    ``matches`` counts what it finds. Only rows the caller may see are
    counted, so no other project's record reaches the number.
    """
    where = list(q.where or [])
    if where_matches_nothing:
        return {
            "by": ["where"],
            "where": where,
            "matches": None,
            "matches_at_least": False,
            "missing_keys": _keys_absent_in_scope(
                chain, [key for key, _ in parse_where(where)],
            ),
        }
    window: dict[str, int] = {}

    def counted(n: int, **kw) -> list[dict[str, Any]]:
        rows = rerun(n)
        window.update(fetched=len(rows), limit=n)
        return rows

    rows = _fetch_visible(chain, counted, fetch_limit, automatic=automatic)
    rows = _filter_type_deny(rows, _list(q.type_deny))
    rows = _filter_min_priority(rows, q.min_priority)
    if not rows:
        return None
    applied = (["where"] if where_chunk_ids is not None else []) + (["types"] if q.types else [])
    allowed = set(where_chunk_ids or ())
    alone = [
        name for name in applied
        if (name == "where" and not any(row.get("id") in allowed for row in rows))
        or (name == "types" and not _filter_types(rows, _list(q.types)))
    ]
    value: dict[str, Any] = {
        # A filter that empties the slate on its own is named alone; when
        # neither does, it is the two together.
        "by": alone or applied,
        "matches": len(rows),
        # The window came back full, so more matches may lie behind it.
        "matches_at_least": window.get("fetched", 0) >= window.get("limit", 1),
        "missing_keys": [],
    }
    if "where" in value["by"]:
        value["where"] = where
    if "types" in value["by"]:
        value["types"] = list(q.types or [])
    return value


# ── enrichment ───────────────────────────────────────────────────────────────


def _label_other_projects(results: list[dict[str, Any]], chain) -> None:
    """Mark each hit tagged to a project other than the request's.

    Only reached when the request opted in with ``include_other_projects``;
    by default such a hit was never delivered. ``other_project`` lists the
    record's own project refs, so a reader sees whose decision it is.
    """
    from palinode.core.visibility import row_other_project_refs

    if chain is None or not chain.include_other_projects:
        return
    for hit in results:
        refs = row_other_project_refs(chain, hit)
        if refs:
            hit["other_project"] = refs


def _attach_evidence(results: list[dict[str, Any]], q: SearchQuery, chain, *,
                     automatic: bool) -> None:
    """Opt-in evidence closure over the final hit list, plus the resolution it
    supports. Read-only; additive.

    The resolution block is decided here, once, and rendered by every surface
    — so the MCP, CLI and REST readings of one hit cannot disagree about
    whether a record stands, is contested, or is unknown.
    """
    if q.resolve and q.resolve != "none" and results:
        from palinode.core.evidence import attach_evidence
        from palinode.core.resolution import attach_resolution

        evidence = attach_evidence(results, mode=q.resolve, chain=chain)
        attach_resolution(results, evidence)
        if not q.include_retired or automatic:
            from palinode.core.evidence import withhold_retired

            withhold_retired(results, evidence)


__all__ = ["Caller", "RecallDenied", "SearchQuery", "SearchResult", "search"]
