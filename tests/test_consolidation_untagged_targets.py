"""A target with no fact markers is a counted skip, not a successful pass.

Consolidation harvests only bullets carrying ``<!-- fact:id -->``. A status
document appended to exclusively by session-end had none, so the runner found
zero addressable facts, returned no operations, and the summary said
``status: success · projects_compacted: 0 · projects_skipped: 0`` — identical
to a quiet week. On the dogfood store that ran 79 consecutive nightly times
against a 449-bullet document.

What these tests pin:
  1. the skip is partitioned out *before* the LLM call, so it costs no inference;
  2. the run summary carries ``groups_skipped_untagged`` + the project names,
     and ``projects_skipped`` counts them;
  3. it is logged at WARNING, naming the file and the fix — not INFO;
  4. the notes of an untagged group are not archived as if they had been
     consolidated;
  5. once the target is tagged, the same store consolidates normally.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import pytest

from palinode.consolidation import runner
from palinode.consolidation.fact_ids import add_fact_ids_to_file
from palinode.core.config import config


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _no_ops(_system: str, _user: str) -> tuple[str, str]:
    return "[]", "stub"


def _update_op(fact_id: str) -> str:
    return (
        '[{"op": "UPDATE", "id": "%s", "new_text": "Updated by the test.", '
        '"reason": "test"}]' % fact_id
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Two projects in one daily note: ``alpha`` tagged, ``untagged`` not."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    (tmp_path / "specs" / "prompts").mkdir(parents=True)
    (tmp_path / "specs" / "prompts" / "compaction.md").write_text("prompt\n")
    (tmp_path / "specs" / "prompts" / "nightly-consolidation.md").write_text("prompt\n")

    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "alpha-status.md").write_text(
        "---\nid: projects-alpha-status\n---\n\n- A tagged fact. <!-- fact:alpha-1 -->\n"
    )
    # Exactly the shape session-end produced before this fix: dated bullets,
    # no markers anywhere.
    (projects / "untagged-status.md").write_text(
        "---\nid: projects-untagged-status\n---\n\n"
        "- [2026-09-09] Shipped the thing.\n"
        "- [2026-09-10] Shipped the other thing.\n"
    )

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / f"{_today()}.md").write_text(
        "---\nid: d\n---\n\nWorked on project/alpha and project/untagged today.\n"
    )
    return tmp_path


# ── The partition itself ─────────────────────────────────────────────────────


def test_tagged_fact_count_counts_only_marked_body_bullets(store):
    status = str(store / "projects" / "untagged-status.md")

    assert runner._tagged_fact_count(status) == 0
    assert runner._tagged_fact_count(str(store / "projects" / "alpha-status.md")) == 1


def test_partition_splits_on_markers_not_on_content(store):
    grouped = {"alpha": [{"content": "x"}], "untagged": [{"content": "y"}]}

    keep, skipped = runner._partition_by_tagged_facts(grouped)

    assert list(keep) == ["alpha"]
    assert skipped == ["untagged"]


def test_partition_keeps_groups_with_no_target(store):
    """No-target groups belong to the *other* partition, which runs first.

    Swallowing them here would silently reclassify a missing document as a
    missing marker, and the two have different fixes.
    """
    keep, skipped = runner._partition_by_tagged_facts({"ghost": [{"content": "x"}]})

    assert list(keep) == ["ghost"]
    assert skipped == []


def test_frontmatter_markers_do_not_count_as_facts(store, tmp_path):
    """A tagged ``entities:`` entry is YAML residue, not an addressable fact."""
    path = store / "projects" / "residue-status.md"
    path.write_text(
        "---\nid: projects-residue-status\nentities:\n"
        "  - project/residue <!-- fact:residue-status-abc123 -->\n---\n\n"
        "- [2026-09-10] A body bullet with no marker.\n"
    )

    assert runner._tagged_fact_count(str(path)) == 0


# ── The summary is honest ────────────────────────────────────────────────────


@pytest.mark.parametrize("dry_run", [True, False])
def test_weekly_run_reports_the_untagged_skip(store, dry_run):
    result = runner.run_consolidation(dry_run=dry_run, llm_fn=_no_ops)

    assert result["groups_skipped_untagged"] == 1
    assert result["skipped_untagged_projects"] == ["untagged"]
    assert result["projects_skipped"] == 1
    assert "bootstrap-ids" in result["untagged_remediation"]


def test_nightly_run_reports_the_untagged_skip(store):
    result = runner.run_nightly(dry_run=True, llm_fn=_no_ops)

    assert result["groups_skipped_untagged"] == 1
    assert result["skipped_untagged_projects"] == ["untagged"]
    assert result["projects_skipped"] == 1


def test_untagged_and_no_target_are_separate_counts(store):
    """Different causes, different fixes — one key each, both in the total."""
    (store / "daily" / f"{_today()}.md").write_text(
        "---\nid: d\n---\n\nproject/alpha, project/untagged and project/ghost.\n"
    )

    result = runner.run_consolidation(dry_run=True, llm_fn=_no_ops)

    assert result["skipped_no_target_projects"] == ["ghost"]
    assert result["skipped_untagged_projects"] == ["untagged"]
    assert result["projects_skipped"] == 2


def test_no_untagged_target_means_no_key(store):
    """Absent rather than zero, matching ``yaml_parse_errors``."""
    add_fact_ids_to_file(str(store / "projects" / "untagged-status.md"))

    result = runner.run_consolidation(dry_run=True, llm_fn=_no_ops)

    assert "groups_skipped_untagged" not in result
    assert "skipped_untagged_projects" not in result
    assert result["projects_skipped"] == 0


# ── Logged loudly, and before any inference ──────────────────────────────────


def test_the_skip_is_a_warning_naming_the_file_and_the_fix(store, caplog):
    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        runner.run_consolidation(dry_run=True, llm_fn=_no_ops)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    matching = [r for r in warnings if "fact:" in r.getMessage()]
    assert matching, f"no WARNING about untagged facts in {[r.getMessage() for r in caplog.records]}"
    message = matching[0].getMessage()
    assert "untagged-status.md" in message
    assert "bootstrap-ids" in message


def test_an_untagged_target_never_reaches_the_llm(store):
    seen: list[str] = []

    def _tracking(_system: str, user: str) -> tuple[str, str]:
        seen.append(user)
        return "[]", "stub"

    runner.run_consolidation(dry_run=True, llm_fn=_tracking)

    assert len(seen) == 1, "only the tagged project should be proposed for"
    assert "A tagged fact." in seen[0]


# ── Not swallowing: notes stay, tagged neighbours still compact ──────────────


def test_the_untagged_groups_notes_are_not_archived(store):
    """Its notes were never consolidated, so retiring them would lose them."""
    llm = _update_op("alpha-1")

    result = runner.run_consolidation(llm_fn=lambda _s, _u: (llm, "stub"))

    assert result["projects_compacted"] == 1
    assert result["notes_left_in_place"] == 1
    assert result["notes_archived"] == 0
    assert os.path.exists(store / "daily" / f"{_today()}.md")


def test_tagging_the_target_makes_the_pass_run(store):
    """The fix works end to end: the same store, one bootstrap call apart."""
    before = runner.run_consolidation(dry_run=True, llm_fn=_no_ops)
    assert before["skipped_untagged_projects"] == ["untagged"]

    tagged = add_fact_ids_to_file(str(store / "projects" / "untagged-status.md"))
    assert tagged == 2

    seen: list[str] = []

    def _tracking(_system: str, user: str) -> tuple[str, str]:
        seen.append(user)
        return "[]", "stub"

    after = runner.run_consolidation(dry_run=True, llm_fn=_tracking)

    assert "groups_skipped_untagged" not in after
    assert len(seen) == 2, "both projects now have addressable facts"
    assert any("Shipped the thing." in prompt for prompt in seen)
