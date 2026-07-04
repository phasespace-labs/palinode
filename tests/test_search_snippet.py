"""Regression tests for #352: /search snippet enrichment + MCP rendering.

Pathologically large chunks (e.g. flat files with no section breaks) used to
return un-truncated content from /search and blow the MCP tool-result budget.
The fix splits the contract: API always populates `snippet` (bounded) and
preserves `content`; MCP renders `snippet` by default and `full=True` opts
into the bounded full-content path.
"""
from __future__ import annotations

from palinode.api.server import (
    _enrich_with_snippets,
    _windowed_snippet,
)
from palinode.core.config import config
from palinode.mcp import _format_results


# ---- _windowed_snippet --------------------------------------------------


def test_windowed_snippet_short_content_passthrough():
    """Content under the cap is returned verbatim."""
    out = _windowed_snippet("a short note", "note", max_chars=400)
    assert out == "a short note"


def test_windowed_snippet_centers_on_query_match():
    """Long content centers the window on the matched query term."""
    filler = "x" * 1000
    needle = " HEARTBEAT-AGENT-IS-HERE "
    content = filler + needle + filler
    out = _windowed_snippet(content, "heartbeat-agent", max_chars=200)
    assert "HEARTBEAT-AGENT-IS-HERE" in out
    assert len(out) <= 202  # ≤ max_chars + 2 ellipses
    assert out.startswith("…") and out.endswith("…")


def test_windowed_snippet_leading_window_when_no_match():
    """Falls back to leading window for vector-only hits."""
    content = "alpha beta gamma " * 200
    out = _windowed_snippet(content, "zzzzzz-not-in-content", max_chars=100)
    # No query hit → leading window, ellipsis suffix only.
    assert out.startswith("alpha beta gamma")
    assert out.endswith("…")
    assert len(out) <= 101


def test_windowed_snippet_skips_short_query_tokens():
    """Tokens shorter than 3 chars are noise (to/in/a) — must not anchor."""
    content = "x" * 500 + " precise " + "x" * 500
    # "to" and "in" are length-2 and would otherwise grab a stupid anchor.
    out = _windowed_snippet(content, "to in precise", max_chars=100)
    assert "precise" in out


# ---- _enrich_with_snippets ---------------------------------------------


def test_enrich_preserves_content_field():
    """API/CLI consumers must still get full content; only snippet is added."""
    big = "y" * 5000
    results = [{"file_path": "a.md", "content": big, "score": 0.9}]
    _enrich_with_snippets(results, query="anything", max_chars=400)
    assert results[0]["content"] == big          # unchanged
    assert len(results[0]["snippet"]) <= 402     # ≤ max_chars + ellipses
    assert results[0]["content_truncated"] is True


def test_enrich_small_chunk_not_marked_truncated():
    """Small chunks: snippet == content, truncated flag is False."""
    results = [{"file_path": "b.md", "content": "tiny note", "score": 0.7}]
    _enrich_with_snippets(results, query="note", max_chars=400)
    assert results[0]["snippet"] == "tiny note"
    assert results[0]["content_truncated"] is False


def test_enrich_handles_missing_content():
    """Defensive: empty/missing content stays empty, never errors."""
    results = [{"file_path": "c.md", "score": 0.5}]
    _enrich_with_snippets(results, query="x", max_chars=400)
    assert results[0]["snippet"] == ""
    assert results[0]["content_truncated"] is False


# ---- _format_results (MCP rendering) ------------------------------------


def test_format_results_default_uses_snippet():
    """Default render path uses snippet — never the unbounded content."""
    big = "Z" * 50_000  # mimic the pathological 54KB chunk
    results = [{
        "file_path": "infrastructure-misc.md",
        "content": big,
        "snippet": "…matched window of ~400 chars…",
        "content_truncated": True,
        "score": 0.82,
    }]
    out = _format_results(results)
    assert "matched window" in out
    assert big not in out
    # Truncation footer must teach the escalation path.
    assert "full=true" in out or "palinode_read" in out


def test_format_results_full_true_returns_capped_content():
    """full=True renders content, but capped — no naked 50KB body."""
    big = "Z" * 50_000
    results = [{
        "file_path": "infrastructure-misc.md",
        "content": big,
        "snippet": "irrelevant when full=True",
        "content_truncated": True,
        "score": 0.82,
    }]
    out = _format_results(results, full=True)
    # Politeness cap (4000 chars) + score header + ellipsis — well under 50KB.
    assert len(out) < 5000
    assert "Z" in out  # actual content rendered, not snippet


def test_format_results_legacy_fallback_when_no_snippet():
    """Older API responses without `snippet` must still render bounded."""
    results = [{
        "file_path": "old.md",
        "content": "A" * 10_000,  # no snippet, no truncated flag
        "score": 0.5,
    }]
    out = _format_results(results)
    # Defensive 400-char fallback path.
    body_lines = [ln for ln in out.split("\n") if "A" in ln]
    assert body_lines
    assert all(len(ln) <= 500 for ln in body_lines)


def test_format_results_no_truncation_footer_when_clean():
    """No truncation footer when nothing was truncated."""
    results = [{
        "file_path": "tiny.md",
        "content": "small body",
        "snippet": "small body",
        "content_truncated": False,
        "score": 0.9,
    }]
    out = _format_results(results)
    assert "truncated" not in out
    assert "palinode_read" not in out


# Acceptance criterion from ------------------------------------


def test_352_acceptance_limit_10_stays_bounded():
    """The bug's acceptance criterion: limit=10 returns must stay reasonable.

    10 pathological 50KB chunks today produce >500KB of tool-result text.
    After the fix, snippet rendering keeps the same set under the MCP budget.
    """
    big_chunk = "Q" * 50_000
    results = [
        {"file_path": f"f{i}.md", "content": big_chunk, "score": 0.5}
        for i in range(10)
    ]
    _enrich_with_snippets(results, query="anything", max_chars=config.search.snippet_max_chars)
    out = _format_results(results)
    # 10 × 400-char snippets + headers + truncation footer ≪ 25KB (MCP budget).
    assert len(out) < 10_000
    assert all(r["content"] == big_chunk for r in results)  # content preserved


# CLI rendering (follow-up) -------------------------------------


def test_cli_search_prefers_snippet_over_blind_truncation(monkeypatch):
    """`palinode search` TTY render uses the API snippet when present."""
    from click.testing import CliRunner
    import importlib
    search_module = importlib.import_module("palinode.cli.search")

    big = "X" * 5000
    fake_results = [{
        "file": "infrastructure-misc.md",
        "score": 0.85,
        "content": big,
        "snippet": "…UNIQUE_MATCH_MARKER from the centered window…",
        "content_truncated": True,
    }]

    def _fake_search(*_a, **_kw):
        return fake_results

    monkeypatch.setattr(search_module.api_client, "search", _fake_search)

    runner = CliRunner()
    # --format=text forces TTY rendering path (CliRunner has no real TTY).
    result = runner.invoke(search_module.search, ["needle", "--format", "text"])
    assert result.exit_code == 0, result.output
    assert "UNIQUE_MATCH_MARKER" in result.output
    # The 5000-char blind body must NOT have been rendered.
    assert "XXXXXXXXXXXXXXXXXXXX" not in result.output


def test_cli_search_legacy_fallback_when_no_snippet(monkeypatch):
    """No snippet field → CLI falls back to the existing 200-char truncation."""
    from click.testing import CliRunner
    import importlib
    search_module = importlib.import_module("palinode.cli.search")

    fake_results = [{
        "file": "old.md",
        "score": 0.5,
        "content": "Y" * 5000,  # no snippet
    }]

    monkeypatch.setattr(
        search_module.api_client, "search", lambda *_a, **_kw: fake_results
    )

    runner = CliRunner()
    result = runner.invoke(search_module.search, ["q", "--format", "text"])
    assert result.exit_code == 0, result.output
    # Legacy path: 200 chars of Y plus ellipsis, NOT the full 5000.
    assert "..." in result.output
    assert "Y" * 5000 not in result.output
