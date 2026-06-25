"""Ranking tests for the human-priority nudge weight (#486, follow-up to #259/#478).

`search_hybrid` applies a bounded priority nudge after RRF normalization:

    score += _PRIORITY_RANK_WEIGHT * (priority - 3)     # priority in 1..5

clamped to [0, 1]. #259 specified the weight should be *tuned against a ranking
test* rather than asserted as a magic fused score. These tests pin the two
ordering properties that justify the value:

  1. **Priority must not override clear relevance** — a strong match at normal
     priority (3) still outranks a clearly weaker match at max priority (5).
  2. **Priority orders similar-relevance hits** — among results of effectively
     equal relevance, the higher-priority one ranks first.

The weight (`0.025` → a ±0.05 band over priority 1..5) is the value that
satisfies both. A teeth test (monkeypatching the weight large) proves the
property is load-bearing: inflate the weight and property (1) breaks.

Construction notes (no magic fused score is asserted — only ordering):
  - RRF is rank-based (`1/(K+rank)`, K=60) and *compressed*: adjacent ranks
    differ by ~0.016 in normalized score, so a "clear" relevance gap must be
    several ranks, not one.
  - The top-ranked hit normalizes to exactly 1.0, where a positive nudge clamps
    and becomes invisible. Every scenario therefore places a neutral **decoy**
    at rank 0 so the hits under test sit below the clamp ceiling and the nudge
    is observable.
"""

from unittest.mock import patch

import pytest

from palinode.core import store
from palinode.core.config import config


def _res(path, *, priority=3, section="root"):
    """A minimal search-result dict carrying a priority in its metadata."""
    return {
        "file_path": path,
        "section_id": section,
        "content": f"content of {path}",
        "metadata": {"priority": priority},
        "score": 0.5,
    }


def _order(results):
    """File paths in returned rank order."""
    return [r["file_path"] for r in results]


@pytest.fixture(autouse=True)
def _decay_off(monkeypatch):
    """Isolate the priority nudge from the (default-off) decay re-rank term."""
    monkeypatch.setattr(config.decay, "enabled", False)


def _run(vec_results, fts_results, **kwargs):
    with (
        patch("palinode.core.store.search", return_value=vec_results),
        patch("palinode.core.store.search_fts", return_value=fts_results),
        patch("palinode.core.store.get_db"),
    ):
        return store.search_hybrid(
            "q", query_embedding=[0.0] * 1024, top_k=10, threshold=0.0,
            hybrid_weight=0.5, **kwargs,
        )


def test_priority_does_not_override_clear_relevance():
    """A strong match @priority-3 outranks a clearly weaker match @priority-5.

    - decoy: rank 0 in both lists  → normalizes to 1.0 (holds the clamp ceiling)
    - strong: rank 1 in both lists → high normalized score, neutral priority 3
    - weak:   rank 15 in vector only → much lower normalized score, max priority 5

    The ±0.05 priority band cannot bridge the strong→weak relevance gap.
    """
    decoy = _res("decoy.md", priority=3)
    strong = _res("strong.md", priority=3)
    weak = _res("weak.md", priority=5)

    vec = [decoy, strong] + [_res(f"f{i}.md") for i in range(13)] + [weak]
    fts = [decoy, strong]

    order = _order(_run(vec, fts))
    assert order[0] == "decoy.md"
    assert order.index("strong.md") < order.index("weak.md"), (
        "max-priority weak match overtook a clearly stronger normal-priority "
        "match — priority weight is too large"
    )


def test_priority_weight_is_load_bearing_teeth():
    """The SAME clear-relevance inputs flip once the weight is inflated.

    Proves the ordering in the test above is enforced by the *value* of
    `_PRIORITY_RANK_WEIGHT`, not by the relevance gap alone — i.e. the test
    actually constrains the weight.
    """
    decoy = _res("decoy.md", priority=3)
    strong = _res("strong.md", priority=3)
    weak = _res("weak.md", priority=5)
    vec = [decoy, strong] + [_res(f"f{i}.md") for i in range(13)] + [weak]
    fts = [decoy, strong]

    with patch.object(store, "_PRIORITY_RANK_WEIGHT", 0.5):
        order = _order(_run(vec, fts))
    assert order.index("weak.md") < order.index("strong.md"), (
        "inflating the weight should let max-priority overtake a stronger "
        "match; if it doesn't, the teeth test is not exercising the nudge"
    )


def test_priority_orders_similar_relevance_hits():
    """Among effectively equal-relevance hits, higher priority ranks first.

    fileA and fileB sit at the same sub-rank (rank 1) in opposite lists, so
    their RRF scores are equal; only the priority nudge separates them.
    """
    decoy = _res("decoy.md", priority=3)

    def run(pa, pb):
        a = _res("fileA.md", priority=pa)
        b = _res("fileB.md", priority=pb)
        vec = [decoy, a]   # a at rank 1 in vector
        fts = [decoy, b]   # b at rank 1 in fts → equal RRF to a
        return _order(_run(vec, fts))

    high_a = run(5, 3)
    assert high_a.index("fileA.md") < high_a.index("fileB.md")

    high_b = run(3, 5)
    assert high_b.index("fileB.md") < high_b.index("fileA.md")


def test_priority_rank_weight_is_the_tuned_value():
    """Pin the tuned weight. The ordering tests above are its rationale.

    0.025 → a ±0.05 swing across priority 1..5 (a p5 hit gains at most +0.05,
    a p1 hit loses at most 0.05). Small enough that a clear relevance gap is
    never bridged (``test_priority_does_not_override_clear_relevance``), large
    enough to order similar-relevance hits
    (``test_priority_orders_similar_relevance_hits``).
    """
    assert store._PRIORITY_RANK_WEIGHT == 0.025
    # Full p1↔p5 band is 4 * weight = 0.1; one-sided (p3→p5) is 0.05.
    assert 4 * store._PRIORITY_RANK_WEIGHT == pytest.approx(0.1)
