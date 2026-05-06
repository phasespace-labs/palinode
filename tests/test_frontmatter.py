"""
Tests for palinode.consolidation.frontmatter — canonical YAML frontmatter
parsing and serialization used across the consolidation package.
"""
from __future__ import annotations

import pytest

from palinode.consolidation.frontmatter import parse_frontmatter, serialize_frontmatter


# ── parse_frontmatter ────────────────────────────────────────────────────────


class TestParseFrontmatter:
    """Verify parse_frontmatter extracts meta and body correctly."""

    def test_standard_frontmatter(self):
        text = "---\ntitle: Hello\ntags:\n  - a\n  - b\n---\nBody text here.\n"
        meta, body = parse_frontmatter(text)
        assert meta == {"title": "Hello", "tags": ["a", "b"]}
        assert body == "Body text here.\n"

    def test_no_frontmatter(self):
        text = "Just a plain markdown file.\n\nWith paragraphs.\n"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_malformed_yaml_returns_original(self):
        # Unclosed bracket is invalid YAML
        text = "---\ntitle: [broken\n---\nBody.\n"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_empty_frontmatter(self):
        text = "---\n---\nBody only.\n"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == "Body only.\n"

    def test_empty_body(self):
        text = "---\nid: test-123\n---\n"
        meta, body = parse_frontmatter(text)
        assert meta == {"id": "test-123"}
        assert body == ""

    def test_body_containing_triple_dash(self):
        """A '---' later in the body must NOT be treated as frontmatter terminator."""
        text = "---\nid: fact-1\n---\n\nSome text.\n\n---\n\nMore text after hr.\n"
        meta, body = parse_frontmatter(text)
        assert meta == {"id": "fact-1"}
        assert "---" in body
        assert "More text after hr." in body

    def test_no_closing_delimiter(self):
        text = "---\ntitle: Open\nNo closing delimiter here.\n"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_yaml_parses_to_non_dict(self):
        """YAML that parses to a bare string/list is not frontmatter."""
        text = "---\njust a string\n---\nBody.\n"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_entities_list_preserved(self):
        text = (
            "---\n"
            "entities:\n"
            "  - project/palinode\n"
            "  - person/paul-kyle\n"
            "---\n"
            "Content.\n"
        )
        meta, body = parse_frontmatter(text)
        assert meta["entities"] == ["project/palinode", "person/paul-kyle"]

    def test_nested_dict_preserved(self):
        text = (
            "---\n"
            "external_refs:\n"
            "  github: https://github.com/example\n"
            "  jira: PROJ-123\n"
            "---\n"
            "Body.\n"
        )
        meta, body = parse_frontmatter(text)
        assert meta["external_refs"] == {
            "github": "https://github.com/example",
            "jira": "PROJ-123",
        }

    def test_date_values(self):
        """YAML auto-parses dates — verify they come through."""
        text = "---\ncreated_at: 2026-05-03\n---\nBody.\n"
        meta, body = parse_frontmatter(text)
        # yaml.safe_load parses YYYY-MM-DD as datetime.date
        from datetime import date
        assert meta["created_at"] == date(2026, 5, 3)

    def test_empty_string_input(self):
        meta, body = parse_frontmatter("")
        assert meta == {}
        assert body == ""


# ── serialize_frontmatter ────────────────────────────────────────────────────


class TestSerializeFrontmatter:
    """Verify serialize_frontmatter produces correct output."""

    def test_empty_meta_returns_body_unchanged(self):
        body = "Just body text.\n"
        assert serialize_frontmatter({}, body) == body

    def test_basic_serialization(self):
        meta = {"id": "test-1", "category": "project"}
        body = "\nContent here.\n"
        result = serialize_frontmatter(meta, body)
        assert result.startswith("---\n")
        assert "id: test-1" in result
        assert "category: project" in result
        assert result.endswith("\nContent here.\n")

    def test_block_style_yaml(self):
        """Lists should be block-style for stable diffs, not inline."""
        meta = {"entities": ["project/palinode", "person/paul"]}
        result = serialize_frontmatter(meta, "Body.\n")
        # Block style uses '- item' notation, not '[item1, item2]'
        assert "- project/palinode" in result
        assert "- person/paul" in result

    def test_unicode_support(self):
        meta = {"name": "Ubersicht"}
        result = serialize_frontmatter(meta, "Body.\n")
        assert "Ubersicht" in result


# ── Round-trip ───────────────────────────────────────────────────────────────


class TestRoundTrip:
    """Verify parse -> serialize produces semantically equivalent content."""

    def test_round_trip_basic(self):
        original = "---\ntitle: Test\ncategory: insight\n---\nBody text.\n"
        meta, body = parse_frontmatter(original)
        result = serialize_frontmatter(meta, body)
        # Re-parse and compare semantically (key order may differ)
        meta2, body2 = parse_frontmatter(result)
        assert meta == meta2
        assert body == body2

    def test_round_trip_with_entities(self):
        original = (
            "---\n"
            "id: test-fact\n"
            "entities:\n"
            "  - project/palinode\n"
            "  - person/paul-kyle\n"
            "created_at: '2026-05-03'\n"
            "---\n"
            "Some factual content about the project.\n"
        )
        meta, body = parse_frontmatter(original)
        result = serialize_frontmatter(meta, body)
        meta2, body2 = parse_frontmatter(result)
        assert meta == meta2
        assert body == body2

    def test_round_trip_no_frontmatter(self):
        original = "Just plain markdown.\n\nNo frontmatter here.\n"
        meta, body = parse_frontmatter(original)
        result = serialize_frontmatter(meta, body)
        assert result == original

    def test_round_trip_nested_dict(self):
        original = (
            "---\n"
            "external_refs:\n"
            "  github: https://github.com/test\n"
            "  linear: PROJ-42\n"
            "---\n"
            "Content.\n"
        )
        meta, body = parse_frontmatter(original)
        result = serialize_frontmatter(meta, body)
        meta2, body2 = parse_frontmatter(result)
        assert meta == meta2
        assert body == body2

    def test_round_trip_empty_body(self):
        original = "---\nid: empty-body\n---\n"
        meta, body = parse_frontmatter(original)
        result = serialize_frontmatter(meta, body)
        meta2, body2 = parse_frontmatter(result)
        assert meta == meta2
        assert body2 == ""
