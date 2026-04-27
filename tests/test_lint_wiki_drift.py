"""Tests for the wiki_drift lint check.

Deliverable D of issue #210.
"""
from __future__ import annotations

import pytest

from palinode.core.lint import check_wiki_drift, _AUTO_FOOTER_MARKER
from palinode.core.config import config


# ── check_wiki_drift (unit-level) ─────────────────────────────────────────────


def test_aligned_no_warnings():
    """Frontmatter entity + matching body wikilink → no warnings."""
    metadata = {"entities": ["person/alice-smith"]}
    body = "Met with [[Alice Smith]] to discuss strategy."
    warnings = check_wiki_drift(metadata, body)
    assert warnings == []


def test_both_empty_no_warnings():
    """No entities, no wikilinks → no warnings."""
    warnings = check_wiki_drift({}, "Plain text with no links.")
    assert warnings == []


def test_body_wikilink_not_in_frontmatter_warns():
    """Body contains [[Alice Smith]] but entities: [] → body_not_in_frontmatter warning."""
    metadata = {"entities": []}
    body = "[[Alice Smith]] reviewed the PR."
    warnings = check_wiki_drift(metadata, body)

    assert len(warnings) == 1
    assert warnings[0]["kind"] == "body_not_in_frontmatter"
    assert "alice-smith" in warnings[0]["detail"]


def test_frontmatter_entity_not_in_body_warns():
    """Frontmatter has person/alice-smith but body has no wikilink → frontmatter_not_in_body."""
    metadata = {"entities": ["person/alice-smith"]}
    body = "Alice and I met about project planning."  # no [[wikilink]]
    warnings = check_wiki_drift(metadata, body)

    assert len(warnings) == 1
    assert warnings[0]["kind"] == "frontmatter_not_in_body"
    assert "alice-smith" in warnings[0]["detail"]


def test_multiple_mismatches_produce_multiple_warnings():
    """Multiple drift points → one warning per mismatched entity."""
    metadata = {"entities": ["person/alice-smith", "project/palinode"]}
    # body links to a third entity and doesn't link back the FM entities
    body = "[[Bob Jones]] showed up unexpectedly."
    warnings = check_wiki_drift(metadata, body)

    kinds = [w["kind"] for w in warnings]
    assert "body_not_in_frontmatter" in kinds   # bob-jones in body not FM
    assert "frontmatter_not_in_body" in kinds   # alice-smith and palinode not in body
    # alice-smith and palinode both missing from body → 2 frontmatter_not_in_body warnings
    assert kinds.count("frontmatter_not_in_body") == 2


def test_auto_footer_covers_frontmatter_entity_no_warning():
    """Entity in frontmatter + auto-footer wikilink → no warning (auto-footer counts)."""
    metadata = {"entities": ["person/alice-smith"]}
    body = (
        "Some text with no inline link.\n\n"
        f"{_AUTO_FOOTER_MARKER}\n"
        "## See also\n"
        "- [[alice-smith]]\n"
    )
    warnings = check_wiki_drift(metadata, body)
    assert warnings == []


def test_auto_footer_wikilinks_not_flagged_as_body_missing_from_fm():
    """Auto-footer links are NOT flagged as 'body wikilink not in frontmatter'."""
    metadata = {"entities": ["person/alice-smith"]}
    body = (
        "Some text.\n\n"
        f"{_AUTO_FOOTER_MARKER}\n"
        "## See also\n"
        "- [[alice-smith]]\n"
    )
    warnings = check_wiki_drift(metadata, body)
    # alice-smith in auto-footer satisfies the frontmatter requirement
    # and should not generate a spurious body_not_in_frontmatter warning
    body_warns = [w for w in warnings if w["kind"] == "body_not_in_frontmatter"]
    assert body_warns == []


def test_inline_body_link_covers_frontmatter_entity():
    """Inline [[wikilink]] in body satisfies the frontmatter entity → no warning."""
    metadata = {"entities": ["project/checkout-redesign", "person/alice-smith"]}
    body = (
        "[[Alice Smith]] and Paul agreed to drop legacy browser support "
        "from the [[checkout-redesign]] roadmap."
    )
    warnings = check_wiki_drift(metadata, body)
    assert warnings == []


def test_typed_body_wikilink_matches_frontmatter_entity():
    """[[person/alice-smith]] in body matches entities: [person/alice-smith] → no warning."""
    metadata = {"entities": ["person/alice-smith"]}
    body = "Discussed with [[person/alice-smith]] today."
    warnings = check_wiki_drift(metadata, body)
    assert warnings == []


# ── run_lint_pass integration ─────────────────────────────────────────────────


def test_run_lint_pass_includes_wiki_drift_key(tmp_path, monkeypatch):
    """run_lint_pass result always contains a wiki_drift key."""
    from palinode.core.lint import run_lint_pass

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "linked.md").write_text(
        "---\nid: people-linked\ncategory: people\ntype: Person\n"
        "entities:\n  - project/foo\n---\nMet [[project/foo]] today."
    )
    result = run_lint_pass()
    assert "wiki_drift" in result


def test_run_lint_pass_wiki_drift_no_drift_file(tmp_path, monkeypatch):
    """A file with aligned frontmatter + body wikilinks produces no drift entry."""
    from palinode.core.lint import run_lint_pass

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "alice.md").write_text(
        "---\nid: people-alice\ncategory: people\ntype: Person\n"
        "entities:\n  - project/palinode\ndescription: Alice\n---\n"
        "Worked on [[project/palinode]] this week.\n"
    )
    result = run_lint_pass()
    assert result["wiki_drift"] == []


def test_run_lint_pass_wiki_drift_catches_drift(tmp_path, monkeypatch):
    """A file with body wikilink not in frontmatter appears in wiki_drift."""
    from palinode.core.lint import run_lint_pass

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    # Body links to [[Bob Jones]] but frontmatter entities is empty
    (decisions_dir / "d001.md").write_text(
        "---\nid: decisions-d001\ncategory: decisions\ntype: Decision\n"
        "description: Test decision\n---\n"
        "[[Bob Jones]] proposed this approach.\n"
    )
    result = run_lint_pass()
    drift = result["wiki_drift"]
    assert any("d001.md" in entry["file"] for entry in drift)
    d001_entry = next(e for e in drift if "d001.md" in e["file"])
    assert any(w["kind"] == "body_not_in_frontmatter" for w in d001_entry["warnings"])
