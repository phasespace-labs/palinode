"""Tests for /search type_deny and per-request max_chars (#391).

Coverage:
- _filter_type_deny: deny list excludes matched types; None/empty is no-op
- type_deny precedence over types (allow-list): a result in both is dropped
- _resolve_snippet_max_chars: per-request override, clamping, config fallback
- /search integration: omitting both new params preserves existing behavior
"""
from __future__ import annotations

import pytest

from palinode.api.server import (
    SearchRequest,
    _enrich_with_snippets,
    _filter_type_deny,
    _resolve_snippet_max_chars,
    _filter_types,
)
from palinode.core.config import config


# ---- helpers ----------------------------------------------------------------


def _row(t: str | None) -> dict:
    """Build a minimal search result row with the given frontmatter type."""
    return {"metadata": {"type": t}} if t else {"metadata": {}}


# ---- _filter_type_deny ------------------------------------------------------


def test_filter_type_deny_none_is_noop():
    rows = [_row("RCA"), _row("Decision"), _row("Insight")]
    assert _filter_type_deny(rows, None) == rows


def test_filter_type_deny_empty_list_is_noop():
    rows = [_row("RCA"), _row("Decision")]
    assert _filter_type_deny(rows, []) == rows


def test_filter_type_deny_single_type():
    rows = [_row("RCA"), _row("Decision"), _row("Postmortem")]
    out = _filter_type_deny(rows, ["RCA"])
    assert len(out) == 2
    types = {r["metadata"]["type"] for r in out}
    assert "RCA" not in types


def test_filter_type_deny_multiple_types():
    rows = [_row("RCA"), _row("Decision"), _row("Postmortem"), _row("Insight")]
    out = _filter_type_deny(rows, ["RCA", "Postmortem"])
    assert len(out) == 2
    types = {r["metadata"]["type"] for r in out}
    assert "RCA" not in types
    assert "Postmortem" not in types


def test_filter_type_deny_no_type_field_is_kept():
    """Results with no type field are not excluded by type_deny."""
    rows = [_row("RCA"), _row(None), _row("Decision")]
    out = _filter_type_deny(rows, ["RCA"])
    # The None-type row has no "type" key — not in the deny set, so kept.
    assert len(out) == 2
    # The RCA row is gone; Decision and no-type row remain.
    kept_types = [r["metadata"].get("type") for r in out]
    assert "RCA" not in kept_types
    assert None in kept_types or "Decision" in kept_types


# ---- type_deny precedence over types ----------------------------------------


def test_type_deny_takes_precedence_over_types():
    """A result whose type is in both allow-list and deny-list must be dropped.

    This verifies the intended ordering: _filter_types first, _filter_type_deny
    second — deny always wins.
    """
    rows = [_row("Decision"), _row("RCA"), _row("Insight")]
    # Allow Decision and RCA; deny RCA → RCA should be excluded.
    after_allow = _filter_types(rows, ["Decision", "RCA"])
    assert len(after_allow) == 2  # Decision + RCA both in allow set

    after_deny = _filter_type_deny(after_allow, ["RCA"])
    assert len(after_deny) == 1
    assert after_deny[0]["metadata"]["type"] == "Decision"


def test_type_deny_all_allowed_types_denied_returns_empty():
    rows = [_row("RCA"), _row("Postmortem")]
    after_allow = _filter_types(rows, ["RCA", "Postmortem"])
    after_deny = _filter_type_deny(after_allow, ["RCA", "Postmortem"])
    assert after_deny == []


# ---- _resolve_snippet_max_chars ---------------------------------------------


def test_resolve_snippet_max_chars_none_uses_config():
    result = _resolve_snippet_max_chars(None)
    assert result == config.search.snippet_max_chars


def test_resolve_snippet_max_chars_override():
    result = _resolve_snippet_max_chars(200)
    assert result == 200


def test_resolve_snippet_max_chars_clamps_to_minimum():
    # Negative or zero values are clamped to 1.
    assert _resolve_snippet_max_chars(0) == 1
    assert _resolve_snippet_max_chars(-50) == 1


def test_resolve_snippet_max_chars_clamps_to_maximum():
    # Values above 8000 are clamped down.
    assert _resolve_snippet_max_chars(99999) == 8000


def test_resolve_snippet_max_chars_boundary_values():
    assert _resolve_snippet_max_chars(1) == 1
    assert _resolve_snippet_max_chars(8000) == 8000


# ---- per-request max_chars in SearchRequest ---------------------------------


def test_search_request_max_chars_default_is_none():
    req = SearchRequest(query="something")
    assert req.max_chars is None


def test_search_request_max_chars_round_trips():
    req = SearchRequest(query="something", max_chars=150)
    assert req.max_chars == 150


def test_search_request_type_deny_default_is_none():
    req = SearchRequest(query="something")
    assert req.type_deny is None


# ---- _enrich_with_snippets with per-request max_chars -----------------------


def test_enrich_with_per_request_max_chars_overrides_config():
    """A small per-request cap produces shorter snippets than the config default."""
    big = "A" * 5000
    results = [{"file_path": "a.md", "content": big, "score": 0.9}]

    max_chars = _resolve_snippet_max_chars(100)
    _enrich_with_snippets(results, query="anything", max_chars=max_chars)

    assert len(results[0]["snippet"]) <= 102  # max_chars + up to 2 ellipsis chars
    assert results[0]["content"] == big        # original content preserved
    assert results[0]["content_truncated"] is True


def test_enrich_with_per_request_max_chars_larger_than_content():
    """When per-request cap exceeds content length, no truncation occurs."""
    content = "short note"
    results = [{"file_path": "b.md", "content": content, "score": 0.8}]

    max_chars = _resolve_snippet_max_chars(500)
    _enrich_with_snippets(results, query="note", max_chars=max_chars)

    assert results[0]["snippet"] == content
    assert results[0]["content_truncated"] is False


# ---- omitting new params preserves existing behavior -----------------------


def test_omitting_type_deny_and_max_chars_is_backward_compatible():
    """SearchRequest with neither new field behaves exactly as before #391."""
    req = SearchRequest(query="decision audit", types=["Decision"])
    assert req.type_deny is None
    assert req.max_chars is None
    # _resolve_snippet_max_chars(None) must equal the config default.
    assert _resolve_snippet_max_chars(req.max_chars) == config.search.snippet_max_chars
    # _filter_type_deny with None is a no-op.
    rows = [_row("Decision"), _row("Insight")]
    assert _filter_type_deny(rows, req.type_deny) == rows
