"""Tests for /search types, since_days, and recency-only mode (#141)."""
from datetime import UTC, datetime, timedelta

from palinode.api.server import (
    SearchRequest,
    _compute_effective_date_after,
    _filter_min_priority,
    _filter_types,
)


# ---- _compute_effective_date_after --------------------------------------


def test_effective_date_after_neither_set():
    req = SearchRequest(query="x")
    assert _compute_effective_date_after(req) is None


def test_effective_date_after_only_explicit():
    req = SearchRequest(query="x", date_after="2026-01-01T00:00:00Z")
    assert _compute_effective_date_after(req) == "2026-01-01T00:00:00Z"


def test_effective_date_after_only_since_days():
    req = SearchRequest(query="x", since_days=7)
    out = _compute_effective_date_after(req)
    assert out is not None
    parsed = datetime.fromisoformat(out.replace("Z", "+00:00"))
    delta = datetime.now(UTC) - parsed
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_effective_date_after_picks_more_restrictive():
    """When both are set, the later (more restrictive) wins."""
    # since_days=1 → ~yesterday; explicit far in the past → yesterday wins
    req_since_wins = SearchRequest(
        query="x", since_days=1, date_after="2020-01-01T00:00:00Z"
    )
    out = _compute_effective_date_after(req_since_wins)
    parsed = datetime.fromisoformat(out.replace("Z", "+00:00"))
    assert (datetime.now(UTC) - parsed) < timedelta(days=2)

    # since_days=365 → ~year ago; explicit yesterday → explicit wins
    explicit_recent = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    req_explicit_wins = SearchRequest(
        query="x", since_days=365, date_after=explicit_recent
    )
    out2 = _compute_effective_date_after(req_explicit_wins)
    assert out2 == explicit_recent


def test_effective_date_after_zero_since_days_ignored():
    req = SearchRequest(query="x", since_days=0)
    assert _compute_effective_date_after(req) is None


# ---- _filter_types ------------------------------------------------------


def _row(t):
    return {"metadata": {"type": t}} if t else {"metadata": {}}


def test_filter_types_none_is_noop():
    rows = [_row("Decision"), _row("Insight"), _row(None)]
    assert _filter_types(rows, None) == rows
    assert _filter_types(rows, []) == rows


def test_filter_types_single():
    rows = [_row("Decision"), _row("Insight"), _row("Decision")]
    out = _filter_types(rows, ["Decision"])
    assert len(out) == 2
    assert all(r["metadata"]["type"] == "Decision" for r in out)


def test_filter_types_multiple_or():
    rows = [_row("Decision"), _row("Insight"), _row("ProjectSnapshot")]
    out = _filter_types(rows, ["Decision", "Insight"])
    assert len(out) == 2


def test_filter_types_drops_missing_type():
    """Rows with no `type` field don't accidentally match."""
    rows = [_row("Decision"), _row(None), _row("Insight")]
    out = _filter_types(rows, ["Decision", "Insight"])
    assert len(out) == 2  # the None-type row is dropped


# ---- _filter_min_priority ------------------------------------------------


def _priority_row(priority):
    meta = {} if priority is None else {"priority": priority}
    return {"metadata": meta}


def test_filter_min_priority_none_is_noop():
    rows = [_priority_row(5), _priority_row(None)]
    assert _filter_min_priority(rows, None) == rows


def test_filter_min_priority_treats_missing_as_normal():
    rows = [_priority_row(5), _priority_row(3), _priority_row(None), _priority_row(2)]
    assert _filter_min_priority(rows, 3) == rows[:3]
    assert _filter_min_priority(rows, 4) == [rows[0]]


def test_filter_min_priority_invalid_frontmatter_is_normal():
    rows = [_priority_row("not-an-int"), _priority_row(6), _priority_row(4)]
    assert _filter_min_priority(rows, 3) == rows
    assert _filter_min_priority(rows, 4) == [rows[2]]
