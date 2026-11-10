"""Consolidation retires only what it actually consolidated.

Before this fix ``run_consolidation`` archived *every* collected daily note as
soon as one project group compacted successfully. A group whose LLM call
raised, whose executor rejected the ops, or which had no target document was
logged and forgotten — and its notes were moved to ``archive/`` unconsolidated,
with the run reporting ``status: success``. The next run never saw them again.

The tests below drive the real runner→executor path with a fake at the propose
seam (``llm_fn``), on a real ``tmp_path`` store, and assert on what is left in
``daily/`` afterwards — the on-disk fact that matters.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from palinode.consolidation import runner
from palinode.core.config import config


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _project(memory_dir: Path, slug: str, fact_id: str) -> Path:
    path = memory_dir / "projects" / f"{slug}.md"
    path.write_text(
        f"---\nid: projects-{slug}\ncategory: project\n---\n\n"
        f"# {slug}\n\n- [2026-06-01] Old {slug} fact. <!-- fact:{fact_id} -->\n",
        encoding="utf-8",
    )
    return path


def _note(memory_dir: Path, name: str, body: str) -> Path:
    path = memory_dir / "daily" / f"{_today()}-{name}.md"
    path.write_text(f"---\nid: {name}\ncategory: daily\n---\n\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Two projects with documents (alpha, beta), one without (ghost), and four
    dated notes: one per project, one spanning both, one for the ghost."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for sub in ("projects", "specs/prompts", "daily"):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "specs" / "prompts" / "compaction.md").write_text(
        "Return consolidation operations as a JSON array.\n", encoding="utf-8"
    )
    _project(tmp_path, "alpha", "a1")
    _project(tmp_path, "beta", "b1")
    notes = {
        "alpha": _note(tmp_path, "alpha", "Worked on project/alpha today."),
        "beta": _note(tmp_path, "beta", "Worked on project/beta today."),
        "both": _note(tmp_path, "both", "Touched project/alpha and project/beta."),
        "ghost": _note(tmp_path, "ghost", "Only project/ghost, which has no document."),
    }
    return tmp_path, notes


def _update_op(fact_id: str) -> str:
    return json.dumps([{"op": "UPDATE", "id": fact_id, "new_text": f"Updated {fact_id}."}])


def _llm_by_project(**per_project):
    """Fake propose seam keyed on which project file the prompt names.

    The real ``_consolidate_project`` builds ``## EXISTING_FACTS (… from
    <slug>.md)``; the fake picks its behaviour from that, so it exercises the
    genuine prompt-building path rather than short-circuiting it. A value that
    is an exception instance is raised — the LLM-timeout case.
    """
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        for slug, behaviour in per_project.items():
            if f"from {slug}.md" in user_prompt:
                if isinstance(behaviour, BaseException):
                    raise behaviour
                return behaviour, "fake-model"
        raise AssertionError(f"unexpected prompt: {user_prompt[:80]!r}")
    return _fn


def _archived(memory_dir: Path) -> set[str]:
    return {p.name for p in (memory_dir / "archive").rglob("*.md")}


def _in_daily(memory_dir: Path) -> set[str]:
    return {p.name for p in (memory_dir / "daily").glob("*.md")}


@pytest.mark.parametrize("dry_run", [False, True])
def test_empty_results_include_compacted_ids(store, monkeypatch, dry_run):
    monkeypatch.setattr(runner, "_collect_daily_notes", lambda *args, **kwargs: ([], 0))

    weekly = runner.run_consolidation(dry_run=dry_run)
    nightly = runner.run_nightly(dry_run=dry_run)

    assert weekly["projects_compacted"] == 0
    assert weekly["projects_compacted_ids"] == []
    assert nightly["projects_compacted"] == 0
    assert nightly["projects_compacted_ids"] == []


# ---------------------------------------------------------------------------
# One group fails at the LLM
# ---------------------------------------------------------------------------

def test_llm_failure_keeps_that_groups_notes_in_place(store):
    memory_dir, notes = store
    llm = _llm_by_project(alpha=_update_op("a1"), beta=TimeoutError("llm timed out"))

    result = runner.run_consolidation(llm_fn=llm)

    assert result["status"] == "partial"
    assert result["projects_compacted"] == 1
    assert result["projects_compacted_ids"] == ["alpha"]
    assert result["projects_failed"] == 1
    assert result["failed_projects"] == ["beta"]
    # alpha was actually consolidated by the real executor.
    assert "Updated a1." in (memory_dir / "projects" / "alpha.md").read_text()
    assert "Old beta fact." in (memory_dir / "projects" / "beta.md").read_text()

    # Only alpha's own note is retired. beta's note, the note spanning both
    # projects, and the untargetable ghost note all stay where the next run
    # will find them.
    assert _archived(memory_dir) == {notes["alpha"].name}
    assert _in_daily(memory_dir) == {notes["beta"].name, notes["both"].name, notes["ghost"].name}
    assert result["notes_archived"] == 1
    assert result["notes_left_in_place"] == 3


def test_executor_exception_counts_as_failure(store, monkeypatch):
    """A raise anywhere inside the per-project step — here the executor — is a
    failed group, not a swallowed log line."""
    from palinode.consolidation import executor

    memory_dir, notes = store
    real_apply = executor.apply_operations

    def _apply(file_path: str, operations: list[dict], **kwargs):
        if file_path.endswith("beta.md"):
            raise ValueError("executor rejected the ops")
        return real_apply(file_path, operations, **kwargs)

    monkeypatch.setattr(executor, "apply_operations", _apply)
    llm = _llm_by_project(alpha=_update_op("a1"), beta=_update_op("b1"))

    result = runner.run_consolidation(llm_fn=llm)

    assert result["status"] == "partial"
    assert result["projects_failed"] == 1
    assert result["failed_projects"] == ["beta"]
    assert _archived(memory_dir) == {notes["alpha"].name}
    assert notes["beta"].name in _in_daily(memory_dir)
    assert notes["both"].name in _in_daily(memory_dir)


# ---------------------------------------------------------------------------
# Skipped (no target) notes stay put
# ---------------------------------------------------------------------------

def test_no_target_note_is_not_archived_when_others_succeed(store):
    memory_dir, notes = store
    llm = _llm_by_project(alpha=_update_op("a1"), beta=_update_op("b1"))

    result = runner.run_consolidation(llm_fn=llm)

    assert result["status"] == "success", "a no-target skip is not a failure"
    assert result["projects_failed"] == 0
    assert result["projects_skipped"] == 1
    assert result["projects_compacted"] == 2
    assert result["projects_compacted_ids"] == ["alpha", "beta"]
    assert result["projects_compacted"] == len(result["projects_compacted_ids"])
    assert _archived(memory_dir) == {notes["alpha"].name, notes["beta"].name, notes["both"].name}
    assert _in_daily(memory_dir) == {notes["ghost"].name}
    assert result["notes_left_in_place"] == 1


# ---------------------------------------------------------------------------
# Nothing succeeded → nothing archived, and the status says so
# ---------------------------------------------------------------------------

def test_all_groups_failing_archives_nothing(store):
    memory_dir, notes = store
    llm = _llm_by_project(alpha=RuntimeError("down"), beta=RuntimeError("down"))

    result = runner.run_consolidation(llm_fn=llm)

    assert result["status"] == "partial"
    assert result["projects_compacted"] == 0
    assert result["projects_compacted_ids"] == []
    assert result["projects_failed"] == 2
    assert result["notes_archived"] == 0
    assert result["notes_left_in_place"] == len(notes)
    assert not (memory_dir / "archive").exists()
    assert _in_daily(memory_dir) == {n.name for n in notes.values()}


def test_dry_run_reports_failures_without_touching_disk(store):
    memory_dir, notes = store
    llm = _llm_by_project(alpha=_update_op("a1"), beta=TimeoutError("llm timed out"))

    result = runner.run_consolidation(dry_run=True, llm_fn=llm)

    assert result["dry_run"] is True
    assert result["projects_compacted_ids"] == ["alpha"]
    assert result["projects_compacted"] == len(result["projects_compacted_ids"])
    assert result["status"] == "partial"
    assert result["projects_failed"] == 1
    assert result["failed_projects"] == ["beta"]
    assert _in_daily(memory_dir) == {n.name for n in notes.values()}
    assert "Old alpha fact." in (memory_dir / "projects" / "alpha.md").read_text()


def test_nightly_status_is_partial_when_a_group_fails(store):
    memory_dir, notes = store
    llm = _llm_by_project(alpha=_update_op("a1"), beta=TimeoutError("llm timed out"))

    result = runner.run_nightly(llm_fn=llm)

    assert result["status"] == "partial"
    assert result["projects_failed"] == 1
    assert result["projects_compacted"] == 1
    assert result["projects_compacted_ids"] == ["alpha"]
    # Nightly never archives; every note is still in daily/.
    assert _in_daily(memory_dir) == {n.name for n in notes.values()}


# ---------------------------------------------------------------------------
# The per-note partition
# ---------------------------------------------------------------------------

def test_partition_leaves_a_note_if_any_of_its_groups_is_unresolved():
    notes = [
        {"filepath": "a", "mentions": ["project/alpha"]},
        {"filepath": "ab", "mentions": ["project/alpha", "project/beta"]},
        {"filepath": "b", "mentions": ["project/beta", "person/pat"]},
        {"filepath": "none", "mentions": ["person/pat"]},
    ]

    partition = runner._partition_notes_for_archive(
        notes, unresolved={"beta"}, today="2026-01-01"
    )

    assert [n["filepath"] for n in partition.retire] == ["a"]
    assert [n["filepath"] for n in partition.unresolved] == ["ab", "b"]
    # A person-only note formed no group, so no pass ever saw it: it stays.
    assert [n["filepath"] for n in partition.no_project] == ["none"]
    assert [n["filepath"] for n in partition.left] == ["ab", "b", "none"]


def test_partition_keeps_todays_daily_note_whatever_its_groups():
    """Only ``daily/<today>.md`` is the live file — a same-day note elsewhere,
    or a daily note for another day, follows the ordinary rule."""
    notes = [
        {"filepath": "/m/daily/2026-09-12.md", "mentions": ["project/alpha"]},
        {"filepath": "/m/daily/2026-09-11.md", "mentions": ["project/alpha"]},
        {"filepath": "/m/insights/2026-09-12.md", "mentions": ["project/alpha"]},
    ]

    partition = runner._partition_notes_for_archive(notes, unresolved=set(), today="2026-09-12")

    assert [n["filepath"] for n in partition.today] == ["/m/daily/2026-09-12.md"]
    assert [n["filepath"] for n in partition.retire] == [
        "/m/daily/2026-09-11.md", "/m/insights/2026-09-12.md",
    ]


# ---------------------------------------------------------------------------
# Notes the pass never saw, groups that decided nothing, and the live daily note
# ---------------------------------------------------------------------------

def test_note_with_no_project_ref_stays_in_place_and_is_counted(store, caplog):
    memory_dir, notes = store
    # Nothing here trips the keyword fallback either, so it truly forms no group.
    loose = _note(memory_dir, "loose", "A reflection that names nobody and nothing.")
    llm = _llm_by_project(alpha=_update_op("a1"), beta=_update_op("b1"))

    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        result = runner.run_consolidation(llm_fn=llm)

    assert loose.name in _in_daily(memory_dir)
    assert loose.name not in _archived(memory_dir)
    assert result["notes_no_project"] == 1
    assert result["notes_left_in_place"] == 2  # loose + ghost
    warning = next(
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "left in place" in r.getMessage()
    )
    assert f"no project/ reference, so no group ever saw them: {loose.name}" in warning
    assert notes["ghost"].name in warning


def test_zero_op_group_retires_its_notes_and_is_named(store, caplog):
    """The model saw alpha's notes and chose to change nothing: that is a
    decision, so the notes retire — but the group must be visible."""
    memory_dir, notes = store
    llm = _llm_by_project(alpha="[]", beta=_update_op("b1"))

    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        result = runner.run_consolidation(llm_fn=llm)

    assert result["status"] == "success"
    assert result["projects_compacted"] == 1
    assert result["projects_no_ops"] == ["alpha"]
    assert result["groups_no_ops"] == 1
    assert result["projects_all_ops_filtered"] == []
    assert _archived(memory_dir) == {notes["alpha"].name, notes["beta"].name, notes["both"].name}
    assert "Old alpha fact." in (memory_dir / "projects" / "alpha.md").read_text()
    info = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO and "proposed no operations" in r.getMessage()
    ]
    assert info == ["palinode.consolidation: 1 project group(s) proposed no operations: alpha"]


def test_all_zero_op_groups_still_drain_daily(store):
    """A quiet week is the common case; it must not leave daily/ growing forever."""
    memory_dir, notes = store
    llm = _llm_by_project(alpha="[]", beta="[]")

    result = runner.run_consolidation(llm_fn=llm)

    assert result["projects_compacted"] == 0
    assert result["projects_no_ops"] == ["alpha", "beta"]
    assert result["notes_archived"] == 3
    assert _in_daily(memory_dir) == {notes["ghost"].name}


def test_group_whose_ops_were_all_filtered_is_named(store, caplog, monkeypatch):
    memory_dir, notes = store
    monkeypatch.setattr(config.consolidation, "allowed_ops", ["KEEP"])
    llm = _llm_by_project(
        alpha=_update_op("a1"),
        beta=json.dumps([{"op": "KEEP", "id": "b1"}]),
    )

    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        result = runner.run_consolidation(llm_fn=llm)

    assert result["projects_compacted"] == 1
    assert result["projects_all_ops_filtered"] == ["alpha"]
    assert result["groups_all_ops_filtered"] == 1
    assert result["projects_no_ops"] == []
    assert "Old alpha fact." in (memory_dir / "projects" / "alpha.md").read_text()
    assert notes["alpha"].name in _archived(memory_dir)
    info = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO and "allowed_ops" in r.getMessage()
    ]
    assert info == [
        "palinode.consolidation: 1 project group(s) had every proposed operation "
        "removed by allowed_ops: alpha"
    ]


def test_todays_daily_note_is_never_moved_but_yesterdays_is(store, caplog):
    """Keeping today's note is the expected outcome of every mid-day run: it
    gets its own INFO line, never the left-in-place WARNING."""
    memory_dir, notes = store
    # Drop the no-target ghost so today's note is the only one left in place.
    notes.pop("ghost").unlink()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    today_note = memory_dir / "daily" / f"{_today()}.md"
    today_note.write_text("Session on project/alpha, still in progress.\n", encoding="utf-8")
    yesterday_note = memory_dir / "daily" / f"{yesterday}.md"
    yesterday_note.write_text("Session on project/alpha, done.\n", encoding="utf-8")
    llm = _llm_by_project(alpha=_update_op("a1"), beta=_update_op("b1"))

    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        result = runner.run_consolidation(llm_fn=llm)

    assert result["projects_compacted"] == 2
    assert today_note.exists()
    assert not yesterday_note.exists()
    assert yesterday_note.name in _archived(memory_dir)
    assert result["notes_today_kept"] == 1
    assert result["notes_left_in_place"] == 1
    assert result["notes_archived"] == 4
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert (
        f"palinode.consolidation: kept today's daily note in place: daily/{today_note.name}, "
        "still being written"
    ) in info


def test_dry_run_carries_the_new_keys(store):
    memory_dir, notes = store
    loose = _note(memory_dir, "loose", "A reflection that names nobody and nothing.")
    (memory_dir / "daily" / f"{_today()}.md").write_text("On project/alpha.\n", encoding="utf-8")
    llm = _llm_by_project(alpha="[]", beta=_update_op("b1"))

    result = runner.run_consolidation(dry_run=True, llm_fn=llm)

    assert result["dry_run"] is True
    assert result["projects_no_ops"] == ["alpha"]
    assert result["groups_no_ops"] == 1
    assert result["projects_all_ops_filtered"] == []
    assert result["groups_all_ops_filtered"] == 0
    assert result["notes_no_project"] == 1
    assert result["notes_today_kept"] == 1
    assert loose.name in _in_daily(memory_dir)
    assert not (memory_dir / "archive").exists()


def test_nightly_names_zero_op_groups_too(store):
    memory_dir, notes = store
    llm = _llm_by_project(alpha="[]", beta=_update_op("b1"))

    result = runner.run_nightly(llm_fn=llm)

    assert result["projects_compacted"] == 1
    assert result["projects_no_ops"] == ["alpha"]
    assert result["groups_no_ops"] == 1
    assert result["projects_all_ops_filtered"] == []
    assert _in_daily(memory_dir) == {n.name for n in notes.values()}
