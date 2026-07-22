"""Tests for ADR-009 Layer 1 scope frontmatter parsing (Slice 2).

Covers `scope`, `visibility`, and `access` extraction in
``palinode.core.parser``. This slice is parser-only — store and search
wiring lands in Slice 3.
"""
from __future__ import annotations

import logging

import pytest

from palinode.core.parser import (
    DEFAULT_VISIBILITY,
    VALID_VISIBILITIES,
    parse_markdown,
    parse_scope,
)


# ---------- explicit scope ----------


def test_explicit_scope_string_is_preserved():
    content = """---
id: auth-decision
scope: project/palinode
---
We chose JWT.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta, file_path="/x/decisions/auth-decision.md")
    assert parsed["scope"] == "project/palinode"


def test_explicit_scope_overrides_directory_default():
    content = """---
id: cross-cutting
scope: org/phasespace
---
Org-wide policy.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta, file_path="/x/decisions/cross-cutting.md")
    # Explicit `scope` wins over the directory inference.
    assert parsed["scope"] == "org/phasespace"


def test_explicit_scope_is_stripped():
    content = """---
id: x
scope: "   project/palinode   "
---
Body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta)
    assert parsed["scope"] == "project/palinode"


# ---------- default scope from directory ----------


def test_default_scope_from_decisions_dir():
    content = """---
id: foo
category: decision
---
A decision body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta, file_path="/memory/decisions/foo.md")
    assert parsed["scope"] == "project/decisions"


def test_default_scope_from_people_dir():
    content = """---
id: alice
category: person
---
Notes about Alice.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta, file_path="/memory/people/alice.md")
    assert parsed["scope"] == "project/people"


def test_default_scope_none_when_no_path_and_no_field():
    content = """---
id: floating
---
No path, no scope field.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta)
    assert parsed["scope"] is None


def test_default_scope_none_when_file_in_root():
    content = """---
id: top-level
---
Body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta, file_path="top-level.md")
    # Bare filename has no parent dir → no inferred scope.
    assert parsed["scope"] is None


def test_empty_scope_string_falls_back_to_directory_default():
    content = """---
id: x
scope: ""
---
Body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta, file_path="/memory/insights/x.md")
    # Whitespace-only / empty scope is treated as absent.
    assert parsed["scope"] == "project/insights"


# ---------- visibility values ----------


@pytest.mark.parametrize("value", list(VALID_VISIBILITIES))
def test_visibility_accepts_all_three_allowed_values(value):
    content = f"""---
id: x
visibility: {value}
---
Body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta)
    assert parsed["visibility"] == value


def test_visibility_default_is_inherited_when_field_absent():
    content = """---
id: x
---
Body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta)
    assert parsed["visibility"] == DEFAULT_VISIBILITY == "inherited"


def test_invalid_visibility_warns_and_falls_back(caplog):
    content = """---
id: x
visibility: secret
---
Body.
"""
    meta, _ = parse_markdown(content)
    with caplog.at_level(logging.WARNING, logger="palinode.parser"):
        parsed = parse_scope(meta)
    assert parsed["visibility"] == DEFAULT_VISIBILITY
    assert any("visibility" in r.getMessage().lower() for r in caplog.records)


def test_non_string_visibility_falls_back():
    # YAML-typed values that aren't strings (e.g. a list) should not crash.
    parsed = parse_scope({"visibility": ["private"]})
    assert parsed["visibility"] == DEFAULT_VISIBILITY


# ---------- restricted + access list ----------


def test_restricted_with_access_list():
    content = """---
id: secret-doc
scope: org/phasespace
visibility: restricted
access:
  - member/alice
  - harness/claude-code
---
AWS rotation schedule.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta)
    assert parsed["visibility"] == "restricted"
    assert parsed["access"] == ["member/alice", "harness/claude-code"]


def test_access_defaults_to_empty_list_when_absent():
    content = """---
id: x
visibility: inherited
---
Body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta)
    assert parsed["access"] == []


def test_access_non_list_coerces_to_empty():
    # Malformed: scalar where a list was expected.
    parsed = parse_scope({"access": "member/alice"})
    assert parsed["access"] == []


def test_access_filters_blank_entries():
    parsed = parse_scope(
        {"access": ["member/alice", "", None, "  ", "harness/claude-code"]}
    )
    assert parsed["access"] == ["member/alice", "harness/claude-code"]


# ---------- backwards compatibility ----------


def test_no_scope_or_visibility_fields_does_not_break_parse_markdown():
    """A file with zero scope-related frontmatter still parses fine."""
    content = """---
id: legacy
category: decision
core: true
entities: [project/my-app]
---
# Legacy file
Body content here.
"""
    meta, sections = parse_markdown(content)
    assert meta["id"] == "legacy"
    assert meta["category"] == "decision"
    assert meta["core"] is True
    assert meta["entities"] == ["project/my-app"]
    assert "scope" not in meta
    assert "visibility" not in meta
    assert len(sections) == 1
    assert "Body content here." in sections[0]["content"]


def test_legacy_file_gets_directory_default_scope_and_inherited_visibility():
    """ADR-009 §7: legacy files default to project/<dir> and inherited."""
    content = """---
id: legacy
category: decision
---
Body.
"""
    meta, _ = parse_markdown(content)
    parsed = parse_scope(meta, file_path="/memory/decisions/legacy.md")
    assert parsed["scope"] == "project/decisions"
    assert parsed["visibility"] == "inherited"
    assert parsed["access"] == []


# ---------- interaction with existing fields ----------


def test_scope_coexists_with_core_category_entities():
    content = """---
id: composite
category: insight
core: true
entities:
  - project/palinode
  - person/alice
scope: project/palinode
visibility: private
canonical_question: How does scope interact with existing metadata?
---
Body.
"""
    meta, sections = parse_markdown(content)
    # Existing fields untouched.
    assert meta["category"] == "insight"
    assert meta["core"] is True
    assert meta["entities"] == ["project/palinode", "person/alice"]
    # canonical_question prefix still applied to the first section.
    assert sections[0]["content"].startswith(
        "Q: How does scope interact with existing metadata?\n\n"
    )
    # Scope fields parsed correctly.
    parsed = parse_scope(meta, file_path="/memory/insights/composite.md")
    assert parsed["scope"] == "project/palinode"
    assert parsed["visibility"] == "private"
    assert parsed["access"] == []
