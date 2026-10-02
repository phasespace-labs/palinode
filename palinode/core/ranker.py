"""Pure hybrid-search ranking pipeline.

Extracted from ``store.search_hybrid`` so the scoring stages — per-arm
relevance floor, RRF fusion, demand-decay re-rank, the human-priority nudge,
ambient-context boost, daily penalty, per-file dedup and cap, and the date
window — live behind one small interface, separable from the I/O around them.
``store.search_hybrid`` stays the orchestrator: it does the two retrievals,
resolves ``context_files`` from the entity index, and records recall +
freshness on the ranked output. This module touches **no** database,
filesystem, or network — every input is passed in, so each scoring stage is
testable on plain dicts.

Knobs that already live in ``config`` (decay band, context boost, daily penalty,
dedup gap) are read from ``config`` here, matching the rest of the codebase and
the existing tests that monkeypatch it. The two inputs that aren't config —
``priority_weight`` (kept on ``store`` so ``patch.object(store, ...)`` still
tunes it) and ``context_files`` (resolved from the DB by the orchestrator) — are
passed explicitly.

``threshold`` is a PER-ARM relevance floor, not a fused-score cutoff. See
:func:`rank_hybrid`'s docstring for the measurement behind this: the
post-RRF score is a function of rank within the ``k=60`` formula, not of
relevance, so filtering on it after fusion silently selects a rank cutoff
that is nearly invariant to the query and to the caller's requested
``top_k`` — a hybrid-search result count that silently saturates well below
whatever ``top_k`` was requested. The fix moves ``threshold`` to where
relevance genuinely lives: each candidate's own arm score (real cosine
similarity for the vector arm, normalized BM25 for the FTS arm), applied
before RRF ever sees the candidates.

The decay/predicate helpers (:func:`effective_importance`,
:func:`_is_daily_file`, :func:`_priority_value`) moved
here with the pipeline; ``store`` re-exports them so ``store.effective_importance``
and friends keep resolving for existing callers and tests.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from palinode.core.config import config


def effective_importance(
    importance: float | None,
    last_recalled_date: str | None,
    now: datetime | None = None,
) -> float:
    """Decay-on-read effective importance (ADR-007 §3.3).

    The stored ``importance`` is the *peak*; decay is computed at read time, so
    there is no sweeper and no write on the read path::

        eff = base + (importance − base) · exp(−Δt / τ)
        eff = max(eff, base)        # floors at base — cold is never demoted

    ``Δt`` is days since ``last_recalled``. A NULL/None importance is treated as
    ``base`` (so eff == base). A never-recalled chunk (last_recalled is None)
    has no decay clock and returns its stored importance (already == base for a
    fresh chunk; floored at base regardless).

    Args:
        importance: stored peak importance (None ⇒ base).
        last_recalled_date: ISO-8601 timestamp of last recall (None ⇒ no decay).
        now: injectable clock for tests; defaults to UTC now.

    Returns:
        Effective importance in ``[base, cap]``-ish range, floored at base.
    """
    cfg = config.decay
    base = cfg.importance_base
    imp = base if importance is None else importance
    if not last_recalled_date:
        return max(imp, base)
    try:
        last = datetime.fromisoformat(str(last_recalled_date).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        delta_days = (datetime.now(UTC) if now is None else now).timestamp()
        delta_days = (delta_days - last.timestamp()) / 86400.0
        if delta_days < 0:
            delta_days = 0.0
    except (ValueError, TypeError):
        return max(imp, base)
    tau = cfg.importance_tau_days or 14.0
    eff = base + (imp - base) * math.exp(-delta_days / tau)
    return max(eff, base)


def _is_daily_file(file_path: str) -> bool:
    """Check if a file path belongs to the daily/ directory."""
    return "/daily/" in file_path or file_path.startswith("daily/")


def _priority_value(metadata: Any) -> int:
    if not isinstance(metadata, dict):
        return 3
    try:
        priority = int(metadata.get("priority", 3))
    except (TypeError, ValueError):
        return 3
    return priority if 1 <= priority <= 5 else 3


def rank_hybrid(
    vec_results: list[dict[str, Any]],
    fts_results: list[dict[str, Any]],
    *,
    top_k: int,
    threshold: float,
    hybrid_weight: float,
    priority_weight: float,
    context_files: set[str] | None = None,
    include_daily: bool = False,
    date_after: str | None = None,
    date_before: str | None = None,
    fts_threshold: float | None = None,
    vector_relative_floor: float | None = None,
) -> list[dict[str, Any]]:
    """Fuse and re-rank vector + BM25 candidate slates into the final hit list.

    Pure: no DB / filesystem / network. The orchestrator
    (:func:`store.search_hybrid`) supplies the two candidate lists, the resolved
    ``context_files`` set (from the entity index), and ``priority_weight`` (the
    ``store``-owned tuning knob); everything else is read from ``config``.

    Stages, in order: arm-specific relevance floors (each arm's absolute and
    relative cutoff) → Reciprocal Rank Fusion
    (RRF, k=60) → demand-decay re-rank (ADR-007, when
    ``config.decay.enabled``) → human-priority nudge → ambient context boost
    (ADR-008) → daily-file penalty → per-file dedup and per-file cap
    (``config.search.max_chunks_per_file``) → date window → top_k, with the
    capped overflow backfilling a short slate.
    Date window runs BEFORE top_k, not after: filtering the already-truncated
    top-k slice silently under-returns whenever the top-scoring candidates
    skew outside the window.  Returns the merged, ranked result dicts (each
    carrying ``score`` plus its two pre-fusion arm scores, ``raw_score``
    (cosine) and ``keyword_score`` (normalized BM25), either of which is
    ``None`` when that arm never retrieved the row); recall + freshness are recorded by
    the caller on this output.

    ``threshold`` filters ``vec_results`` by their real cosine score, and
    ``vector_relative_floor`` (``config.search.vector_relative_floor`` when
    ``None``) then filters what survives relative to the best cosine in that
    same candidate set — the vector arm's counterpart to ``fts_threshold``,
    because an absolute floor says only whether a candidate is plausible at
    all, never whether it is plausible beside the match this query found. A
    caller whose top hit is by construction a record it must discard passes
    ``0.0``: a floor measured against that hit would cut the candidates it is
    looking for (see ``consolidation.forget``).
    ``fts_threshold`` independently filters ``fts_results`` relative to the
    best normalized BM25 score in the same slate — a ratio, so it reads the
    same whatever constant that score is divided by (see
    :func:`palinode.core.store.bm25_query_scale`). Both arms' floors run BEFORE
    fusion, so a candidate needs only one arm to admit it. Both relative
    floors measure against the best candidate in their own arm, so the top
    match always survives: they bound a slate, they never empty one. The one exemption:
    an FTS candidate carrying ``has_vector=False`` (the store's mark for a
    chunk with no ``chunks_vec`` row) is kept regardless of its BM25 score,
    because the keyword arm is its only retrieval path. ``threshold`` is
    deliberately **not** applied to the
    fused/boosted score: a production
    measurement found that score to be a function of RRF rank, not
    relevance — two semantically unrelated queries against the same store
    produced byte-identical post-RRF sequences (``1.0, 0.4919, 0.4841,
    0.4766, …``, confirmed by ``tests/test_ranker.py``'s characterisation
    tests). A cutoff against that sequence is a rank cutoff wearing a
    relevance costume, and because the sequence decays slowly and
    predictably it lands at very nearly the same rank regardless of
    ``top_k`` — measured in production as a hybrid-search result count that
    silently plateaued well below the requested limit. Same lesson the
    forget resolver learned from the demand side
    (``palinode/consolidation/forget.py``): never threshold
    a post-RRF score.
    """
    # Arm-specific relevance floors — see the docstring above. Apply them before
    # RRF so each retrieval method is judged only on its own score scale.
    def _cosine(r: dict[str, Any]) -> float:
        raw = r.get("raw_score")
        return float(raw if raw is not None else r.get("score", 0.0))

    if threshold > 0.0:
        vec_results = [r for r in vec_results if _cosine(r) >= threshold]
    # The vector arm's own relative floor, on top of the absolute one: a
    # candidate is delivered only when its cosine is within
    # ``vector_relative_floor`` of the best cosine in the same candidate set.
    # The absolute floor answers "is this plausible at all"; it cannot answer
    # "is this plausible next to what this query actually found", so without
    # this the arm hands fusion every candidate it retrieved above the floor
    # and weak neighbours fill the slate. Relative to the best match, so the
    # top candidate always clears it — this bounds the slate, it never
    # abstains. ``None`` = the configured default; ``0.0`` = no relative floor.
    vec_frac = (
        config.search.vector_relative_floor
        if vector_relative_floor is None
        else vector_relative_floor
    )
    if vec_frac > 0.0 and vec_results:
        top_vec = max(_cosine(r) for r in vec_results)
        if top_vec > 0.0:
            vec_results = [r for r in vec_results if _cosine(r) >= vec_frac * top_vec]
    # The FTS arm has its own floor, relative to its best candidate: normalized
    # BM25 is not on the cosine scale, so an absolute floor at the cosine
    # threshold discarded correct keyword hits. See
    # ``SearchConfig.fts_threshold`` for the measurement. ``None`` = the
    # configured default; ``0.0`` = no FTS floor.
    fts_frac = config.search.fts_threshold if fts_threshold is None else fts_threshold
    if fts_frac > 0.0 and fts_results:
        top_fts = max(r.get("score", 0.0) for r in fts_results)
        # A candidate the store marked ``has_vector=False`` (an FTS-only row:
        # per-input embed rejection, deferred embed) is exempt — the vector arm
        # can never vouch for it, so the keyword arm is its only route in.
        fts_results = [
            r for r in fts_results
            if r.get("score", 0.0) >= fts_frac * top_fts or r.get("has_vector") is False
        ]

    # Reciprocal Rank Fusion (RRF)
    # Score = sum( 1 / (k + rank) ) for each result across both lists
    # k=60 is the standard RRF constant (dampens high-rank dominance)
    K = 60
    rrf_scores: dict[str, float] = {}
    result_map: dict[str, dict] = {}
    # Track raw cosine similarity from vector search before RRF normalization
    raw_cosine: dict[str, float] = {}
    # Same, for the keyword arm: normalized BM25 as ``search_fts`` scored it,
    # captured here because the fused score overwrites ``score`` on the very
    # dicts an FTS-only candidate is carried in. Both survive onto the merged
    # rows so a caller can read each arm on its own scale
    # (:mod:`palinode.core.confidence`) instead of on the rank the fusion
    # leaves behind.
    raw_keyword: dict[str, float] = {}

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
        raw_keyword[key] = r.get("score", 0.0)
        if key not in result_map:
            result_map[key] = r

    # Sort by RRF score descending, normalize to 0.0-1.0
    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)
    max_score = rrf_scores[sorted_keys[0]] if sorted_keys else 1.0

    if config.decay.enabled:
        # ADR-007 §3.4: demand-decay enters as a *bounded re-rank term*, not the
        # lead signal. Semantic relevance (normalized RRF) leads; effective
        # importance (decay-on-read, §3.3) modulates within a clamped band so a
        # hot memory gets a modest boost and a cold one is NEVER suppressed below
        # its relevance (eff floors at base ⇒ boost floors at 1.0). This is the
        # brake: a just-read memory does not snowball (the nudge is
        # session-deduplicated and eff decays on read).
        cfg = config.decay
        base = cfg.importance_base
        cap = cfg.importance_cap
        # Width of the re-rank band: hot (eff→cap) gets at most +`band` relative
        # boost; neutral (eff==base) is unchanged. Bounded so importance can
        # nudge ordering among similarly-relevant hits without overriding it.
        band = 0.25
        denom = (cap - base) or 1.0
        for key in sorted_keys:
            r = result_map[key]
            norm_score = rrf_scores[key] / max_score
            eff = effective_importance(r.get("importance"), r.get("last_recalled"))
            # eff ∈ [base, cap] ⇒ boost ∈ [1.0, 1.0 + band]; never < 1.0.
            boost = 1.0 + band * (eff - base) / denom
            r["score"] = min(norm_score * boost, 1.0)
            r["effective_importance"] = eff

        # Re-sort after applying the bounded re-rank term.
        sorted_keys = sorted(rrf_scores.keys(), key=lambda k: result_map[k].get("score", 0.0), reverse=True)
    else:
        for key in sorted_keys:
            result_map[key]["score"] = rrf_scores[key] / max_score

    for key in sorted_keys:
        r = result_map[key]
        priority = _priority_value(r.get("metadata", {}))
        r["score"] = min(max(r.get("score", 0.0) + priority_weight * (priority - 3), 0.0), 1.0)
    sorted_keys = sorted(
        sorted_keys, key=lambda k: result_map[k].get("score", 0.0), reverse=True
    )

    # Ambient context boost (ADR-008): boost results matching caller's project context
    if context_files and config.context.enabled and config.context.boost != 1.0:
        for key in sorted_keys:
            r = result_map[key]
            if r["file_path"] in context_files:
                r["score"] = r.get("score", 0) * config.context.boost
        # Re-sort after context boost
        sorted_keys = sorted(
            sorted_keys, key=lambda k: result_map[k].get("score", 0.0), reverse=True
        )

    # Issue Penalize daily/ files to prevent session notes from dominating results
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
    # the file's best chunk. A second chunk from the same file is kept
    # only if its score is within dedup_score_gap of the file's best.
    #
    # ``max_chunks_per_file`` is the second, harder bound on the same
    # redundancy. The gap above compares POST-FUSION scores, which are
    # rank-derived (see the docstring), so adjacent ranks differ by a fraction
    # of the gap and one file's chunks are never far enough apart for it to
    # fire — measured as one file taking 3 of 5 slots. Chunks past the cap are
    # *deferred*, not dropped: they go to ``overflow_keys`` and still fill the
    # slate when the competitive candidates run out, so the cap decides which
    # results fill ``top_k`` and never how many. A backfilled chunk lands
    # behind results it outscores; that is the point of having capped it.
    file_best: dict[str, float] = {}
    file_kept: dict[str, int] = {}
    deduped_keys: list[str] = []
    overflow_keys: list[str] = []
    gap = config.search.dedup_score_gap
    cap = config.search.max_chunks_per_file
    for key in sorted_keys:
        r = result_map[key]
        fp = r["file_path"]
        score = r.get("score", 0.0)
        if fp in file_best and file_best[fp] - score > gap:
            continue
        file_best.setdefault(fp, score)
        if cap > 0 and file_kept.get(fp, 0) >= cap:
            overflow_keys.append(key)
            continue
        file_kept[fp] = file_kept.get(fp, 0) + 1
        deduped_keys.append(key)

    # Date window is applied to the FULL deduped candidate list, BEFORE the
    # top_k slice below — not after. Applying it after (the old order)
    # truncates to top_k first, so a date-windowed hybrid search silently
    # under-returns whenever the top-k-by-score candidates are disproportionately
    # outside the window: they consume the slice and are then discarded, even
    # though later-ranked in-window candidates existed. Matches the vector-only
    # path's semantics, which filters inside the row loop before its own top_k
    # break.
    if date_after or date_before:
        def _in_window(key: str) -> bool:
            r = result_map[key]
            meta = r.get("metadata", {})
            updated = meta.get("last_updated", r.get("created_at", ""))
            if not updated:
                return True
            if date_after and updated < date_after:
                return False
            return not (date_before and updated > date_before)

        deduped_keys = [key for key in deduped_keys if _in_window(key)]
        overflow_keys = [key for key in overflow_keys if _in_window(key)]

    # top_k is the sole cardinality control past this point — no post-fusion
    # score cutoff (see the ``threshold`` paragraph on the docstring above).
    # The per-file overflow backfills only what the competitive candidates
    # could not fill, so capping a file never shortens a slate.
    selected = deduped_keys[:top_k]
    if len(selected) < top_k:
        selected += overflow_keys[: top_k - len(selected)]

    merged = []
    for key in selected:
        result = result_map[key]
        # Attach each arm's own pre-fusion score. A result the other arm never
        # retrieved gets None there — no similarity to report, which is not the
        # same as a zero one (see ``scoring.describe_match``).
        result["raw_score"] = raw_cosine.get(key)
        result["keyword_score"] = raw_keyword.get(key)
        merged.append(result)

    return merged
