"""Bounded windows must not miss what the caller may receive.

A search fetches a window of candidates and then withholds what the caller may
not see and what its filters exclude. When those rows fill the window, an
eligible match ranked below it must still be found, and the counts the
delivery reports must cover what was withheld, not just the window.

Real SQLite and real memory files under ``tmp_path``; each record sits at an
exact cosine to the query, so the ranking is arithmetic.
"""
from __future__ import annotations

from tests import test_relative_floor_over_visible_hits as _floor

memory = _floor.memory
client = _floor.client
QUERY = _floor.QUERY


def _ids(payload) -> list[str]:
    rows = payload["results"] if isinstance(payload, dict) else payload
    return [row["id"] for row in rows]


def _search(client, **body):
    resp = client.post("/search", json={"query": QUERY, "threshold": 0.4, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── post-filters ─────────────────────────────────────────────────────────────


def test_a_type_filter_finds_a_match_ranked_below_the_window(memory, client):
    # limit=1 fetches 5; six better-ranked Insights fill that window.
    for n in range(6):
        _floor._record(memory, f"insight-{n}", 0.90 - n * 0.001, 10 + n, type="Insight")
    _floor._record(memory, "the-decision", 0.85, 30, type="Decision")
    assert _ids(_search(client, context=[], hybrid=False, types=["Decision"], limit=1)) == [
        "the-decision"
    ]


def test_a_type_deny_finds_a_match_ranked_below_the_window(memory, client):
    for n in range(6):
        _floor._record(memory, f"decision-{n}", 0.90 - n * 0.001, 10 + n, type="Decision")
    _floor._record(memory, "the-insight", 0.85, 30, type="Insight")
    assert _ids(_search(client, context=[], hybrid=False, type_deny=["Decision"], limit=1)) == [
        "the-insight"
    ]


def test_recency_with_type_deny_skips_past_the_denied_newest(memory, client):
    for n in range(3):
        _floor._record(memory, f"new-decision-{n}", 0.5, 10 + n, type="Decision")
    old = memory / "insights" / "old-insight.md"
    _floor._record(memory, "old-insight", 0.5, 40, type="Insight")
    from palinode.core import store

    with store.transaction() as db:
        db.execute("UPDATE chunks SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                   ("old-insight",))
    assert old.exists()
    payload = _search(client, query="", context=[], type_deny=["Decision"], limit=1)
    assert [row["rel_path"] for row in payload] == ["insights/old-insight.md"]


def test_a_priority_floor_finds_a_match_ranked_below_the_window(memory, client):
    # The priority nudge reorders the candidates the store fetched, so the
    # high-priority record has to rank below the whole candidate set.
    for n in range(30):
        _floor._record(memory, f"low-{n}", 0.95 - n * 0.0005, 10 + n, priority=1)
    _floor._record(memory, "high", 0.82, 30, priority=5)
    assert _ids(_search(client, context=[], hybrid=False, min_priority=4, limit=1)) == ["high"]


# ── visibility ───────────────────────────────────────────────────────────────


def test_many_hidden_better_matches_do_not_hide_a_visible_one(memory, client):
    # 40 private records outrank the visible one; limit=1 widens only to 5.
    for n in range(40):
        _floor._record(memory, f"private-{n}", 0.90 - n * 0.0005, 10 + n, visibility="private")
    _floor._record(memory, "visible", 0.86, 80)
    assert _ids(_search(client, context=[], hybrid=False, limit=1)) == ["visible"]


def test_other_projects_withheld_counts_every_one(memory, client):
    for n in range(30):
        _floor._record(memory, f"beta-{n}", 0.90 - n * 0.0005, 10 + n, entities="[project/beta]")
    _floor._record(memory, "alpha", 0.86, 80, entities="[project/alpha]")
    payload = _search(client, context=["project/alpha"], hybrid=False, limit=1, receipt=True)
    assert _ids(payload) == ["alpha"]
    assert payload["receipt"]["retrieval"]["other_projects_withheld"] == 30


def test_resolve_seeds_survive_many_hidden_better_matches(memory):
    from palinode.core.bundle import BundleBudget, BundleRequest, build_bundle
    from palinode.core.scope import search_scope_chain

    for n in range(200):
        _floor._record(memory, f"beta-{n}", 0.90 - n * 0.0001, 10 + n, entities="[project/beta]")
    _floor._record(memory, "alpha", 0.87, 400, entities="[project/alpha]")
    chain = search_scope_chain(context=["project/alpha"])
    bundle = build_bundle(
        BundleRequest(query=QUERY, budget=BundleBudget(max_items=3, max_chars=3000)),
        chain=chain,
    )
    assert "insights/alpha.md" in [s.assertion.rel_path for s in bundle.selected]


def test_a_window_stopped_by_its_cap_says_the_count_is_a_lower_bound(memory, client, monkeypatch):
    from palinode.core.config import config

    # max_limit 2 caps the window at 10, below the 30 records withheld.
    monkeypatch.setattr(config.search, "max_limit", 2)
    for n in range(30):
        _floor._record(memory, f"beta-{n}", 0.90 - n * 0.0005, 10 + n, entities="[project/beta]")
    _floor._record(memory, "alpha", 0.86, 80, entities="[project/alpha]")
    payload = _search(client, context=["project/alpha"], hybrid=False, limit=1, receipt=True)
    retrieval = payload["receipt"]["retrieval"]
    assert _ids(payload) == []
    assert retrieval["other_projects_withheld"] == 10
    assert retrieval["other_projects_withheld_at_least"] is True


def test_a_full_first_window_is_one_fetch_and_reports_no_bound(memory, client, monkeypatch):
    from palinode.core import store

    _floor._record(memory, "visible", 0.86, 80)
    calls: list[int] = []
    real = store.search_hybrid

    def spy(*args, **kwargs):
        calls.append(kwargs["top_k"])
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "search_hybrid", spy)
    payload = _search(client, context=[], hybrid=False, limit=1, receipt=True)
    assert _ids(payload) == ["visible"] and calls == [1]
    assert "other_projects_withheld_at_least" not in payload["receipt"]["retrieval"]
