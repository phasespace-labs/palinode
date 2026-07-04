"""Pure hybrid-search ranking pipeline (#553).

Extracted from ``store.search_hybrid`` so the scoring stages — RRF fusion,
demand-decay re-rank, the human-priority nudge, ambient-context boost, daily
penalty, per-file dedup, threshold/top_k, and the date window — live behind one
small interface, separable from the I/O around them. ``store.search_hybrid``
stays the orchestrator: it does the two retrievals, resolves ``context_files``
from the entity index, and records recall + freshness on the ranked output. This
module touches **no** database, filesystem, or network — every input is passed
in, so each scoring stage is testable on plain dicts.

Knobs that already live in ``config`` (decay band, context boost, daily penalty,
dedup gap) are read from ``config`` here, matching the rest of the codebase and
the existing tests that monkeypatch it. The two inputs that aren't config —
``priority_weight`` (kept on ``store`` so ``patch.object(store, ...)`` still
tunes it) and ``context_files`` (resolved from the DB by the orchestrator) — are
passed explicitly.

The decay/predicate helpers (:func:`effective_importance`,
:func:`score_with_decay`, :func:`_is_daily_file`, :func:`_priority_value`) moved
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
    cfg = config.decay
    TAU = {
        "critical": cfg.tau_critical, "decisions": cfg.tau_decisions, "insights": cfg.tau_insights,
        "general": cfg.tau_general, "status": cfg.tau_status, "ephemeral": cfg.tau_ephemeral,
    }
    tau = TAU.get(memory_type, cfg.tau_general)

    if last_recalled_date:
        try:
            last = datetime.fromisoformat(last_recalled_date[:10])
            delta_days = (datetime.now(UTC) - last.replace(tzinfo=UTC)).days
        except Exception:
            delta_days = 0
    else:
        delta_days = 30  # Default decay for never-recalled memories

    decay = math.exp(-delta_days / tau)
    frequency_boost = 1 + math.log1p(recall_count)

    return min(base_score * importance * decay * frequency_boost, 1.0)


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
) -> list[dict[str, Any]]:
    """Fuse and re-rank vector + BM25 candidate slates into the final hit list.

    Pure: no DB / filesystem / network. The orchestrator
    (:func:`store.search_hybrid`) supplies the two candidate lists, the resolved
    ``context_files`` set (from the entity index), and ``priority_weight`` (the
    ``store``-owned tuning knob); everything else is read from ``config``.

    Stages, in order: Reciprocal Rank Fusion (RRF, k=60) → demand-decay re-rank
    (ADR-007, when ``config.decay.enabled``) → human-priority nudge → ambient
    context boost (ADR-008) → daily-file penalty (#93) → per-file dedup (#91) →
    threshold + top_k → date window. Returns the merged, ranked result dicts
    (each carrying ``score`` and ``raw_score``); recall + freshness are recorded
    by the caller on this output.
    """
    # Reciprocal Rank Fusion (RRF)
    # Score = sum( 1 / (k + rank) ) for each result across both lists
    # k=60 is the standard RRF constant (dampens high-rank dominance)
    K = 60
    rrf_scores: dict[str, float] = {}
    result_map: dict[str, dict] = {}
    # Track raw cosine similarity from vector search before RRF normalization
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
            # Attach raw cosine similarity from vector search.
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

    return merged
