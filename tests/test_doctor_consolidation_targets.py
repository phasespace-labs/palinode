"""Tests for the ``consolidation_targets_tagged`` doctor check.

Real store trees under ``tmp_path``: the check reads markdown and nothing else,
so there is nothing worth faking. The case it exists for is the dogfood store —
one ``projects/<slug>-status.md`` with hundreds of session-end bullets and no
``<!-- fact:id -->`` markers, which consolidation skipped silently on every run
for six months.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from palinode.consolidation.fact_ids import add_fact_ids_to_file
from palinode.core.config import Config, config as global_config
from palinode.diagnostics.checks.consolidation_targets import (
    _target_for,
    consolidation_targets_tagged,
)
from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import DoctorContext

UNTAGGED = (
    "---\nid: projects-palinode-status\n---\n\n"
    "# Palinode Status\n\n"
    "- [2026-09-09] Shipped the thing.\n"
    "- [2026-09-10] Shipped the other thing.\n"
)
TAGGED = (
    "---\nid: projects-alpha-status\n---\n\n"
    "- [2026-09-10] A fact the runner can address. <!-- fact:alpha-status-abc123 -->\n"
)


def _ctx(memory_dir: Path) -> DoctorContext:
    cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
    cfg.git.auto_commit = False
    return DoctorContext(config=cfg)


@pytest.fixture
def store(tmp_path) -> Path:
    (tmp_path / "projects").mkdir()
    return tmp_path


# ── Registration ─────────────────────────────────────────────────────────────


def test_the_check_is_registered_as_fast():
    names = {fn.__name__: tags for fn, tags in all_checks()}

    assert "fast" in names["consolidation_targets_tagged"]


# ── The warn case — the whole point ──────────────────────────────────────────


def test_an_untagged_status_doc_warns(store):
    (store / "projects" / "palinode-status.md").write_text(UNTAGGED, encoding="utf-8")

    result = consolidation_targets_tagged(_ctx(store))

    assert result.severity == "warn"
    assert result.passed is False
    assert "projects/palinode-status.md" in result.message
    assert "2 untagged bullets" in result.message
    assert "palinode bootstrap-ids --file projects/palinode-status.md" in result.remediation
    assert result.linked_issue is None


def test_tagging_the_file_makes_the_check_pass(store):
    path = store / "projects" / "palinode-status.md"
    path.write_text(UNTAGGED, encoding="utf-8")
    assert consolidation_targets_tagged(_ctx(store)).passed is False

    add_fact_ids_to_file(str(path))

    result = consolidation_targets_tagged(_ctx(store))
    assert result.passed is True
    assert result.severity == "info"
    assert "1 carry fact markers" in result.message


def test_a_partially_tagged_doc_does_not_warn(store):
    """One marker is enough for the runner to have something to address.

    The check's job is "is this document inert", not "is every line tagged" —
    consolidation itself tags what it rewrites, so a mixed document is normal.
    """
    (store / "projects" / "mixed-status.md").write_text(
        UNTAGGED + "- [2026-09-11] A tagged one. <!-- fact:mixed-status-abc123 -->\n",
        encoding="utf-8",
    )

    assert consolidation_targets_tagged(_ctx(store)).passed is True


def test_each_inert_target_is_named(store):
    (store / "projects" / "one-status.md").write_text(UNTAGGED, encoding="utf-8")
    (store / "projects" / "two-status.md").write_text(UNTAGGED, encoding="utf-8")
    (store / "projects" / "ok-status.md").write_text(TAGGED, encoding="utf-8")

    result = consolidation_targets_tagged(_ctx(store))

    assert "2 consolidation target(s)" in result.message
    assert "projects/one-status.md" in result.message
    assert "projects/two-status.md" in result.message
    assert "projects/ok-status.md" not in result.message


# ── The quiet cases ──────────────────────────────────────────────────────────


def test_a_store_with_no_projects_dir_is_info(tmp_path):
    result = consolidation_targets_tagged(_ctx(tmp_path))

    assert result.passed is True
    assert result.severity == "info"


def test_a_status_doc_with_no_bullets_is_not_inert(store):
    """Nothing to compact is not the same as nothing the runner can see."""
    (store / "projects" / "empty-status.md").write_text(
        "---\nid: projects-empty-status\n---\n\n# Empty\n\nProse only.\n",
        encoding="utf-8",
    )

    result = consolidation_targets_tagged(_ctx(store))

    assert result.passed is True
    assert "no body bullets yet" in result.message


def test_frontmatter_bullets_alone_do_not_count(store):
    """``- project/foo`` under ``entities:`` is YAML, not a fact."""
    (store / "projects" / "meta-status.md").write_text(
        "---\nid: projects-meta-status\nentities:\n  - project/meta\n---\n\nProse.\n",
        encoding="utf-8",
    )

    assert consolidation_targets_tagged(_ctx(store)).passed is True


# ── Targets derived from recent daily notes ──────────────────────────────────


def test_a_plain_project_file_named_by_a_recent_note_is_checked(store, monkeypatch):
    """The ``*-status.md`` glob misses ``projects/<slug>.md`` targets."""
    from datetime import UTC, datetime

    (store / "projects" / "plain.md").write_text(
        "---\nid: projects-plain\n---\n\n- [2026-09-10] An untagged bullet.\n",
        encoding="utf-8",
    )
    (store / "daily").mkdir()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    (store / "daily" / f"{today}.md").write_text(
        "---\nid: d\n---\n\nWorked on project/plain today.\n", encoding="utf-8"
    )

    result = consolidation_targets_tagged(_ctx(store))

    assert result.passed is False
    assert "projects/plain.md" in result.message


def test_an_unreferenced_plain_project_file_is_left_alone(store):
    """It is not a consolidation target until something names it."""
    (store / "projects" / "plain.md").write_text(
        "---\nid: projects-plain\n---\n\n- [2026-09-10] An untagged bullet.\n",
        encoding="utf-8",
    )

    result = consolidation_targets_tagged(_ctx(store))

    assert result.passed is True


def test_an_old_daily_note_does_not_widen_the_scan(store):
    (store / "projects" / "plain.md").write_text(
        "---\nid: projects-plain\n---\n\n- [2020-01-01] An untagged bullet.\n",
        encoding="utf-8",
    )
    (store / "daily").mkdir()
    (store / "daily" / "2020-01-01.md").write_text(
        "---\nid: d\n---\n\nWorked on project/plain.\n", encoding="utf-8"
    )

    assert consolidation_targets_tagged(_ctx(store)).passed is True


# ── The duplicated target derivation cannot drift ────────────────────────────


@pytest.mark.parametrize(
    "layout,expected",
    [
        (("alpha-status.md", "alpha.md"), "alpha-status.md"),
        (("alpha.md",), "alpha.md"),
        ((), None),
    ],
)
def test_target_derivation_matches_the_runner(store, monkeypatch, layout, expected):
    """``_target_for`` is a local copy of ``runner._target_file_for`` (importing
    the runner costs a second of store/embedder imports for two stat calls).
    This is the guard that keeps the copy honest."""
    from palinode.consolidation import runner

    for name in layout:
        (store / "projects" / name).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(global_config, "memory_dir", str(store))

    mine = _target_for(store / "projects", "alpha")
    theirs = runner._target_file_for("alpha")

    assert (str(mine) if mine else None) == theirs
    assert (mine.name if mine else None) == expected
