"""Pure-function tests for the hybrid-search ranker (#553).

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


def test_threshold_drops_low_scoring_hits():
    a = _res("a.md")
    b = _res("b.md")
    # With a single shared rank-0 hit normalized to 1.0, a high threshold keeps
    # only the top; assert the threshold is actually applied post-fusion.
    out = _run([a], [a], threshold=0.99)
    assert _order(out) == ["a.md"]
    out_empty = _run([a, b], [], threshold=1.01)
    assert out_empty == [], "threshold above the normalized max drops everything"


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
