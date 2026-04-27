"""Tests for parser Layer 3-lite: body [[wikilink]] extraction + entity merge.

Deliverable D of issue #210.
"""
from __future__ import annotations

import pytest

from palinode.core.parser import canonicalize_wikilink, parse_entities, parse_markdown


# ── canonicalize_wikilink ─────────────────────────────────────────────────────


def test_canonicalize_already_typed():
    """Labels with a slash are normalised as-is."""
    assert canonicalize_wikilink("person/alice-smith") == "person/alice-smith"


def test_canonicalize_already_typed_normalised():
    """Typed labels have slugs lowercased and spaced → hyphenated."""
    assert canonicalize_wikilink("Person/Alice Smith") == "person/alice-smith"


def test_canonicalize_plain_label_no_known_entities():
    """Plain labels with no known entities fall back to entity/<slug>."""
    assert canonicalize_wikilink("Alice Smith") == "entity/alice-smith"


def test_canonicalize_plain_label_matched_against_known_entities():
    """[[Alice Smith]] matches known entity person/alice-smith via slug."""
    known = ["person/alice-smith", "project/palinode"]
    assert canonicalize_wikilink("Alice Smith", known_entities=known) == "person/alice-smith"


def test_canonicalize_slug_form_matched_against_known_entities():
    """[[alice-smith]] (slug form) also matches person/alice-smith."""
    known = ["person/alice-smith"]
    assert canonicalize_wikilink("alice-smith", known_entities=known) == "person/alice-smith"


def test_canonicalize_no_match_falls_back():
    """Label that doesn't match any known entity slug → entity/<slug>."""
    known = ["person/alice-smith"]
    assert canonicalize_wikilink("Bob Jones", known_entities=known) == "entity/bob-jones"


def test_canonicalize_special_chars_stripped():
    """Non-alphanumeric chars other than hyphens are stripped."""
    assert canonicalize_wikilink("Alice & Bob!") == "entity/alice-bob"


# ── parse_entities ────────────────────────────────────────────────────────────


def test_frontmatter_entities_only_no_body_wikilinks():
    """Frontmatter entities are preserved; body is empty → entities_body is empty."""
    metadata = {"entities": ["person/alice-smith", "project/palinode"]}
    body = "Some text with no wikilinks."
    result = parse_entities(metadata, body)

    assert result["entities_frontmatter"] == ["person/alice-smith", "project/palinode"]
    assert result["entities_body"] == []
    assert result["entities_resolved"] == ["person/alice-smith", "project/palinode"]


def test_body_wikilinks_only_no_frontmatter_entities():
    """Body wikilinks canonicalized into entities_resolved; entities_frontmatter is empty."""
    metadata = {}
    body = "We met with [[Alice Smith]] to discuss [[checkout-redesign]]."
    result = parse_entities(metadata, body)

    assert result["entities_frontmatter"] == []
    assert "entity/alice-smith" in result["entities_body"]
    assert "entity/checkout-redesign" in result["entities_body"]
    # resolved == body entities when no frontmatter
    assert set(result["entities_resolved"]) == set(result["entities_body"])


def test_both_overlap_deduplicated():
    """Same entity appears in frontmatter AND body → appears once in resolved."""
    metadata = {"entities": ["person/alice-smith"]}
    body = "See [[Alice Smith]] for details."  # resolves to person/alice-smith via slug match
    result = parse_entities(metadata, body)

    resolved = result["entities_resolved"]
    assert resolved.count("person/alice-smith") == 1, "Duplicate entity in resolved list"
    assert len(resolved) == 1


def test_both_disjoint_resolved_has_both():
    """Frontmatter and body reference different entities → resolved has all of them."""
    metadata = {"entities": ["project/palinode"]}
    body = "Great talk with [[Alice Smith]] yesterday."
    result = parse_entities(metadata, body)

    resolved = result["entities_resolved"]
    assert "project/palinode" in resolved
    assert "entity/alice-smith" in resolved
    assert len(resolved) == 2


def test_frontmatter_first_in_resolved():
    """Frontmatter entries appear before body-only entries in entities_resolved."""
    metadata = {"entities": ["project/palinode"]}
    body = "[[Alice Smith]] mentioned [[project/palinode]] again."
    result = parse_entities(metadata, body)

    resolved = result["entities_resolved"]
    # project/palinode should be first (from frontmatter)
    assert resolved[0] == "project/palinode"
    # alice-smith is body-only, comes after
    assert "entity/alice-smith" in resolved


def test_body_label_and_fm_slug_same_entity_deduped():
    """[[Alice Smith]] (label) and entities: [person/alice-smith] → one entry."""
    metadata = {"entities": ["person/alice-smith"]}
    body = "Session with [[Alice Smith]] today."
    result = parse_entities(metadata, body)

    resolved = result["entities_resolved"]
    assert resolved.count("person/alice-smith") == 1
    assert len(resolved) == 1


def test_body_duplicate_wikilinks_deduped():
    """Same wikilink appearing twice in body → single entry in entities_body."""
    metadata = {}
    body = "[[Alice Smith]] and [[Alice Smith]] both confirmed."
    result = parse_entities(metadata, body)

    assert result["entities_body"].count("entity/alice-smith") == 1


def test_typed_body_wikilink_preserved():
    """[[person/alice-smith]] in body uses slash-notation → typed entity preserved."""
    metadata = {}
    body = "Talked to [[person/alice-smith]] today."
    result = parse_entities(metadata, body)

    assert "person/alice-smith" in result["entities_body"]


def test_parse_entities_empty_frontmatter_and_body():
    """Empty document → all three lists empty."""
    result = parse_entities({}, "")
    assert result["entities_frontmatter"] == []
    assert result["entities_body"] == []
    assert result["entities_resolved"] == []


def test_wikilink_pipe_syntax_uses_target():
    """[[Target|Display text]] — target part is extracted, display ignored."""
    metadata = {}
    body = "Read [[checkout-redesign|the redesign doc]] first."
    result = parse_entities(metadata, body)

    assert "entity/checkout-redesign" in result["entities_body"]


def test_parse_entities_integrates_with_parse_markdown():
    """End-to-end: parse_markdown then parse_entities works on real content."""
    content = """\
---
id: decision-drop-legacy
category: decision
entities:
  - project/checkout-redesign
  - person/alice-smith
---
[[Alice Smith]] and Paul agreed to drop legacy browser support from [[checkout-redesign]].
"""
    metadata, _ = parse_markdown(content)
    import frontmatter as _fm
    body = _fm.loads(content).content
    result = parse_entities(metadata, body)

    # Both surfaces covered; should deduplicate to exactly 2 entities.
    assert len(result["entities_resolved"]) == 2
    assert "person/alice-smith" in result["entities_resolved"]
    assert "project/checkout-redesign" in result["entities_resolved"]
