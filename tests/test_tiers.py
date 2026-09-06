"""Tiered read/search views.

Two things are load-bearing and get asserted directly rather than inferred:
the caps hold (a caller that asked for ~300 chars gets at most that, ellipsis
included), and omitting ``tier`` returns exactly what the surface returned
before tiers existed.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from palinode.api.search_helpers import _apply_tier
from palinode.api.server import app
from palinode.core.config import config
from palinode.core.tiers import abstract_for, apply_tier, first_paragraph, overview_for

client = TestClient(app)


SUMMARY_FILE = (
    "---\n"
    "name: Alice\n"
    "category: people\n"
    "summary: Alice runs the deployment rota.\n"
    "---\n"
    "\n"
    "# Heading\n"
    "\n"
    "First body paragraph, which is not the summary.\n"
    "\n"
    "Second body paragraph.\n"
)

NO_SUMMARY_FILE = (
    "---\n"
    "name: Bob\n"
    "category: people\n"
    "---\n"
    "\n"
    "## A heading that is only a label\n"
    "\n"
    "Bob maintains the ingest watcher.\n"
)


@pytest.fixture
def memory_dir(tmp_path):
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    os.makedirs(tmp_path / "people")
    (tmp_path / "people" / "alice.md").write_text(SUMMARY_FILE, encoding="utf-8")
    (tmp_path / "people" / "bob.md").write_text(NO_SUMMARY_FILE, encoding="utf-8")
    yield tmp_path
    config.memory_dir = old


# ── the view functions ───────────────────────────────────────────────────────


def test_abstract_prefers_summary_frontmatter():
    assert (
        abstract_for({"summary": "Alice runs the rota."}, "Body text.")
        == "Alice runs the rota."
    )


def test_abstract_falls_back_to_canonical_question_then_lede():
    assert (
        abstract_for({"canonical_question": "Who runs the rota?"}, "Body text.")
        == "Who runs the rota?"
    )
    assert abstract_for({}, "Body text.") == "Body text."
    assert abstract_for(None, "Body text.") == "Body text."


def test_abstract_ignores_a_blank_summary():
    assert abstract_for({"summary": "   "}, "Body text.") == "Body text."


def test_first_paragraph_skips_headings():
    body = "# Title\n\n## Subtitle\n\nThe actual first sentence.\n"
    assert first_paragraph(body) == "The actual first sentence."


def test_first_paragraph_of_empty_body_is_empty():
    assert first_paragraph("") == ""
    assert first_paragraph("\n\n   \n") == ""


def test_abstract_truncation_counts_the_ellipsis():
    long_summary = "x" * 500
    out = abstract_for({"summary": long_summary}, "", max_chars=50)
    assert len(out) == 50
    assert out.endswith("…")


def test_overview_never_exceeds_the_cap():
    body = "y" * 10_000
    content = f"---\nname: Big\n---\n\n{body}"
    for cap in (10, 60, 200, 4000):
        out = overview_for(content, body, max_chars=cap)
        assert len(out) <= cap, f"cap {cap} exceeded: {len(out)}"


def test_overview_keeps_the_frontmatter_block_whole():
    body = "First body paragraph, which is not the summary."
    out = overview_for(SUMMARY_FILE, body, max_chars=4000)
    assert out.startswith("---\nname: Alice\n")
    assert "summary: Alice runs the deployment rota." in out
    assert "First body paragraph" in out


def test_overview_drops_the_body_before_mangling_frontmatter():
    """A cap smaller than the frontmatter yields frontmatter, not half a key."""
    out = overview_for(SUMMARY_FILE, "body", max_chars=20)
    assert len(out) <= 20
    assert out == SUMMARY_FILE[:20]


def test_overview_without_frontmatter_is_just_the_body_head():
    out = overview_for("plain body, no fences", "plain body, no fences", max_chars=11)
    assert len(out) <= 11


def test_full_and_none_are_passthrough():
    assert apply_tier("full", SUMMARY_FILE) == SUMMARY_FILE
    assert apply_tier(None, SUMMARY_FILE) == SUMMARY_FILE


def test_unknown_tier_raises():
    with pytest.raises(ValueError, match="unknown tier"):
        apply_tier("summary", SUMMARY_FILE)


# ── the search helper ────────────────────────────────────────────────────────


def test_search_tier_helper_is_a_noop_without_a_tier():
    rows = [{"content": "x" * 900, "snippet": "x" * 400}]
    _apply_tier(rows, None)
    assert rows[0]["content"] == "x" * 900
    assert "tier" not in rows[0]


def test_search_tier_abstract_caps_every_hit():
    rows = [
        {"content": "z" * 5000, "metadata": {}},
        {"content": "body", "metadata": {"summary": "The summary."}},
    ]
    _apply_tier(rows, "abstract")
    for row in rows:
        assert len(row["content"]) <= config.read.abstract_max_chars
        assert row["snippet"] == row["content"]
        assert row["tier"] == "abstract"
    assert rows[0]["content_truncated"] is True
    assert rows[1]["content"] == "The summary."


# ── the /read surface ────────────────────────────────────────────────────────


def test_read_without_tier_is_unchanged(memory_dir):
    res = client.get("/read", params={"file_path": "people/alice.md"})
    assert res.status_code == 200
    data = res.json()
    assert data["content"] == SUMMARY_FILE
    assert "tier" not in data


def test_read_abstract_uses_the_summary(memory_dir):
    res = client.get(
        "/read", params={"file_path": "people/alice.md", "tier": "abstract"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["content"] == "Alice runs the deployment rota."
    assert data["tier"] == "abstract"
    assert len(data["content"]) <= config.read.abstract_max_chars


def test_read_abstract_falls_back_to_the_lede(memory_dir):
    res = client.get("/read", params={"file_path": "people/bob.md", "tier": "abstract"})
    assert res.json()["content"] == "Bob maintains the ingest watcher."


def test_read_size_bytes_reports_the_file_not_the_view(memory_dir):
    """An abstract still tells the caller what opening the full record costs."""
    res = client.get(
        "/read", params={"file_path": "people/alice.md", "tier": "abstract"}
    )
    data = res.json()
    assert data["size_bytes"] == len(SUMMARY_FILE.encode("utf-8"))
    assert data["size_bytes"] > len(data["content"])


def test_read_overview_respects_the_configured_cap(memory_dir):
    old = config.read.overview_max_chars
    config.read.overview_max_chars = 80
    try:
        res = client.get(
            "/read", params={"file_path": "people/alice.md", "tier": "overview"}
        )
        assert len(res.json()["content"]) <= 80
    finally:
        config.read.overview_max_chars = old


def test_read_rejects_an_unknown_tier(memory_dir):
    res = client.get("/read", params={"file_path": "people/alice.md", "tier": "wat"})
    assert res.status_code == 422


def test_read_meta_still_returns_frontmatter_with_a_tier(memory_dir):
    res = client.get(
        "/read",
        params={"file_path": "people/alice.md", "meta": "true", "tier": "abstract"},
    )
    data = res.json()
    assert data["frontmatter"]["name"] == "Alice"
    assert data["content"] == "Alice runs the deployment rota."
