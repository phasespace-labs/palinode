"""Pure-function tests for the hybrid-search ranker.

The point of extracting ``rank_hybrid`` out of ``store.search_hybrid``: each
scoring stage is now exercisable on plain dicts with **no** database, no
embedder, and no mocking of ``store.search`` / ``search_fts`` / ``get_db``.
These tests call the ranker directly with hand-built candidate slates and assert
the fusion / dedup / context / daily / threshold behaviour in isolation.

The end-to-end ranking properties (priority nudge, decay band) remain pinned via
``store.search_hybrid`` in test_priority_ranking_486 / test_demand_decay_importance;
this file guards the seam itself.
"""

from __future__ import annotations

import pytest

from palinode.core import ranker
from palinode.core.config import config


def _res(path, *, score=0.5, section="root", metadata=None, **extra):
    r = {"file_path": path, "section_id": section, "score": score, "id": path}
    if metadata is not None:
        r["metadata"] = metadata
    r.update(extra)
    return r


@pytest.fixture(autouse=True)
def _decay_and_context_off(monkeypatch):
    # Isolate the stage under test: no decay re-rank, neutral context/daily so a
    # plain RRF/dedup assertion isn't perturbed. Individual tests re-enable knobs.
    monkeypatch.setattr(config.decay, "enabled", False)
    monkeypatch.setattr(config.context, "enabled", False)
    monkeypatch.setattr(config.search, "daily_penalty", 1.0)
    monkeypatch.setattr(config.search, "dedup_score_gap", 0.05)


def _run(vec, fts, **kw):
    params = dict(top_k=10, threshold=0.0, hybrid_weight=0.5, priority_weight=0.025)
    params.update(kw)
    return ranker.rank_hybrid(vec, fts, **params)


def _order(results):
    return [r["file_path"] for r in results]


def test_rrf_fusion_rewards_agreement_across_both_lists():
    # `both` appears rank-0 in vec AND fts; `vonly`/`fonly` appear in one list.
    both = _res("both.md")
    vonly = _res("vonly.md")
    fonly = _res("fonly.md")
    out = _run([both, vonly], [both, fonly])
    assert _order(out)[0] == "both.md", "a hit in both lists should fuse to the top"


def test_threshold_is_a_per_arm_relevance_floor():
    """`threshold` filters each candidate's OWN (real) score before fusion —
    it is no longer a cutoff on the fused/RRF score. A low-own-score
    candidate is dropped even where RRF rank alone would have carried it to
    the top of the fused list.
    """
    strong = _res("strong.md", score=0.7)
    weak = _res("weak.md", score=0.3)
    # `weak` is listed first (rank 0) — under the old post-fusion semantics
    # rank 0 always normalizes to 1.0 and would have survived any threshold
    # below 1.0. It is dropped here because its own score never clears 0.5.
    out = _run([weak, strong], [], threshold=0.5)
    assert _order(out) == ["strong.md"], (
        "a candidate below the per-arm floor must be dropped regardless of "
        "the RRF rank it would fuse to"
    )


def test_threshold_lets_either_arm_vouch_for_a_candidate():
    """A candidate weak on one arm's own score still survives if the OTHER
    arm's own score clears the floor — hybrid search should still catch a
    strong keyword match with a weak vector score, or vice versa."""
    weak_vec = _res("hit.md", score=0.2)
    strong_fts = _res("hit.md", score=0.9)
    out = _run([weak_vec], [strong_fts], threshold=0.5)
    assert _order(out) == ["hit.md"]


def test_threshold_floor_is_independent_of_rrf_rank():
    """A candidate at/above the per-arm floor survives even buried deep in
    RRF rank — the floor no longer collapses into an accidental rank cutoff.
    This is the ranker-level shape of the hybrid-search saturation defect: a
    relevant candidate must not be silently dropped just because many other
    relevant candidates outrank it.
    """
    decoy = _res("decoy.md", score=0.9)
    padding = [_res(f"pad{i}.md", score=0.55) for i in range(30)]
    buried = _res("buried.md", score=0.6)  # rank 31, own score clears 0.55
    out = _run([decoy] + padding + [buried], [], threshold=0.55, top_k=50)
    assert "buried.md" in _order(out)


def test_hybrid_search_not_capped_by_post_fusion_threshold():
    """Direct reproduction of the rank-locked ~41-result ceiling a production
    measurement found on the hybrid search path. 100 candidates, all
    genuinely relevant (own score 0.9, well above the 0.6 API-default-shaped
    threshold used here), requesting top_k=80.

    Against the pre-fix ``rank_hybrid`` (post-fusion threshold on the RRF
    score) this scenario returns exactly 41 results — the RRF-normalized
    score sequence crosses 0.6 at rank ~40 regardless of how relevant the
    candidates actually are, independent of the requested top_k. Confirmed
    against the previous ``palinode/core/ranker.py`` (pre-this-fix) while
    writing this fix. Post-fix, thresholding happens on each candidate's own
    (0.9) score, so nothing is dropped and the count is exactly what was
    asked for.
    """
    candidates = [_res(f"c{i}.md", score=0.9) for i in range(100)]
    out = _run(candidates, [], threshold=0.6, top_k=80)
    assert len(out) == 80, (
        "top_k should be the only cardinality control once every candidate "
        "clears the per-arm relevance floor"
    )


def test_rrf_fused_score_is_a_rank_artifact_not_relevance():
    """Characterisation pinned from a production measurement: two
    unrelated queries against the same store produced byte-identical
    post-RRF score sequences (1.0, 0.4919, 0.4841, 0.4766, 0.4692, …). This
    reproduces the shape that produces it (top candidate agrees across both
    arms, everything after appears in only one) and shows the sequence is
    IDENTICAL whether the underlying candidates are highly relevant (own
    score 0.99) or barely relevant (own score 0.02) — which is exactly why
    it must never be used as a relevance threshold (see rank_hybrid's
    docstring, and palinode/consolidation/forget.py's commit 69c7e5a, which
    hit the same defect from the demand side).
    """
    def _fused(own_score: float) -> list[float]:
        top = _res("top.md", score=own_score)
        singles = [_res(f"s{i}.md", score=own_score) for i in range(6)]
        out = _run([top] + singles, [top], threshold=0.0, top_k=7)
        return [round(r["score"], 4) for r in out]

    expected = [1.0, 0.4919, 0.4841, 0.4766, 0.4692, 0.4621, 0.4552]
    assert _fused(0.99) == expected
    assert _fused(0.02) == expected, (
        "the fused score must not vary with relevance — it is purely a "
        "function of rank position and RRF's k=60 constant"
    )


def test_top_k_caps_results():
    vec = [_res(f"f{i}.md") for i in range(8)]
    out = _run(vec, [], top_k=3)
    assert len(out) <= 3


def test_per_file_dedup_suppresses_far_below_best_chunk(monkeypatch):
    monkeypatch.setattr(config.search, "dedup_score_gap", 0.05)
    # RRF recomputes each chunk's score from its *rank*, so the score gap that
    # drives dedup comes from rank distance, not the input `score`. Put the two
    # same-file chunks far apart in rank: the best at rank 0, the second buried
    # deep enough that its normalized RRF score is > dedup_score_gap below.
    best = _res("same.md", section="s1")
    other = _res("other.md")
    far = _res("same.md", section="s2")
    vec = [best, other] + [_res(f"pad{i}.md") for i in range(13)] + [far]
    out = _run(vec, [])
    paths = _order(out)
    assert "same.md" in paths and "other.md" in paths
    # only the file's best chunk survives; the far one is deduped away
    assert paths.count("same.md") == 1


def test_context_boost_lifts_in_context_files(monkeypatch):
    monkeypatch.setattr(config.context, "enabled", True)
    monkeypatch.setattr(config.context, "boost", 2.0)
    a = _res("a.md")          # rank 0 → higher base RRF
    b = _res("b.md")          # rank 1 → lower base RRF
    # Without boost a outranks b. Boost b via context_files and it should overtake.
    out = _run([a, b], [], context_files={"b.md"})
    assert _order(out)[0] == "b.md", "context boost should lift an in-context file"


def test_daily_penalty_demotes_daily_files(monkeypatch):
    monkeypatch.setattr(config.search, "daily_penalty", 0.1)
    daily = _res("daily/2026-06-21.md")   # rank 0, would win without penalty
    normal = _res("insights/x.md")        # rank 1
    out = _run([daily, normal], [])
    assert _order(out)[0] == "insights/x.md", "daily files are penalised unless include_daily"
    out_incl = _run([daily, normal], [], include_daily=True)
    assert _order(out_incl)[0] == "daily/2026-06-21.md", "include_daily disables the penalty"


def test_date_window_filters_by_last_updated():
    inwin = _res("in.md", metadata={"last_updated": "2026-06-10"})
    old = _res("old.md", metadata={"last_updated": "2026-01-01"})
    out = _run([inwin, old], [], date_after="2026-06-01")
    paths = _order(out)
    assert "in.md" in paths and "old.md" not in paths


def test_date_window_applied_before_top_k_truncation():
    """The date-window-vs-top_k-truncation defect: the date window must
    filter the full candidate list BEFORE the top_k slice, not after. Two
    out-of-window candidates outrank two in-window ones here; under the old
    order (top_k=2 slice first, date filter second) both survivors of the
    slice get filtered out and the search silently returns 0 results, even
    though 2 in-window candidates exist just past the slice boundary.
    """
    out_of_window = [
        _res(f"old{i}.md", metadata={"last_updated": "2026-01-01"}) for i in range(2)
    ]
    in_window = [
        _res(f"new{i}.md", metadata={"last_updated": "2026-06-10"}) for i in range(2)
    ]
    # out_of_window ranked first (rank 0, 1); in_window ranked after (rank 2, 3).
    out = _run(out_of_window + in_window, [], top_k=2, date_after="2026-06-01")
    assert _order(out) == ["new0.md", "new1.md"], (
        "date-windowed search must not be defeated by top_k truncating away "
        "the in-window candidates before the window is ever applied"
    )


def test_threshold_exempts_vectorless_fts_candidates():
    """An FTS candidate the store marked ``has_vector=False`` (a chunk with
    no ``chunks_vec`` row — written FTS-only by a per-input embed rejection
    or a deferred embed) survives the per-arm floor regardless of
    its BM25 score: the keyword arm is the only arm it has. A candidate with
    a vector, or with no flag at all (legacy slate), is floored as before.
    """
    # The FTS floor is relative to the slate's best keyword score (0.30 here,
    # floor 0.4 × 0.30 = 0.12), not the cosine threshold — so the 0.05 rows
    # are below it, and only the vectorless one is let through.
    top = _res("top.md", score=0.30, has_vector=True)
    fts_only = _res("fts-only.md", score=0.05, has_vector=False)
    vectored = _res("vectored.md", score=0.05, has_vector=True)
    legacy = _res("legacy.md", score=0.05)
    out = _run([], [top, fts_only, vectored, legacy], threshold=0.5)
    assert set(_order(out)) == {"top.md", "fts-only.md"}


def test_vectorless_exemption_leaves_vectored_candidates_untouched():
    """Pin: the exemption changes nothing for chunks that have a vector. The
    same slate with and without a vectorless FTS-only candidate yields the
    same vectored survivors, in the same order, with the same scores — the
    vectorless one simply enters fusion as an ordinary FTS candidate.
    """
    strong_vec = _res("strong.md", score=0.7)
    weak_vec = _res("weak.md", score=0.3, raw_score=0.3)
    strong_fts = _res("strong.md", score=0.5, has_vector=True)
    weak_fts_vectored = _res("weak.md", score=0.1, has_vector=True)   # below 0.4 × 0.5
    fts_only = _res("fts-only.md", score=0.02, has_vector=False)

    baseline = _run([strong_vec, weak_vec], [strong_fts, weak_fts_vectored], threshold=0.5)
    with_fts_only = _run(
        [strong_vec, weak_vec], [strong_fts, weak_fts_vectored, fts_only], threshold=0.5
    )

    def _vectored(results):
        return [(r["file_path"], r["score"]) for r in results if r["file_path"] != "fts-only.md"]

    assert _order(baseline) == ["strong.md"]
    assert _vectored(with_fts_only) == _vectored(baseline)
    assert "fts-only.md" in _order(with_fts_only)
