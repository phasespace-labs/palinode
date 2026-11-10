"""A daily note that names no project retires on age alone.

The weekly pass retires a note only when every project group it belongs to
reached a decision. A note that belongs to *no* group can never meet that rule:
no pass reads it, so no pass ever decides on it, and it stayed in ``daily/``
forever — 105 of 107 stale notes on the dogfood store, all ranking in recall as
current. Waiting cannot help such a note, so once it is older than
``consolidation.daily_note_retention_days`` the weekly pass moves it to
``archive/`` deterministically, with no model involved.

Real ``tmp_path`` store, real files, the real runner; the only fake is the
propose seam (``llm_fn``).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from palinode.consolidation import runner
from palinode.core.config import config


def _days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _dated(days: int) -> str:
    return _days_ago(days).strftime("%Y-%m-%d")


def _write(path: Path, body: str, frontmatter: str = "category: daily\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8")
    return path


def _age(path: Path, days: int) -> Path:
    """Backdate *path*'s mtime — the date a note with no date in its name has."""
    stamp = _days_ago(days).timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    """One project (alpha) with a target document and one current note on it."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    (tmp_path / "specs" / "prompts").mkdir(parents=True)
    (tmp_path / "specs" / "prompts" / "compaction.md").write_text(
        "Return consolidation operations as a JSON array.\n", encoding="utf-8"
    )
    _write(
        tmp_path / "projects" / "alpha.md",
        "# alpha\n\n- [2026-06-01] Old alpha fact. <!-- fact:a1 -->",
        "id: projects-alpha\ncategory: project\n",
    )
    _write(tmp_path / "daily" / f"{_dated(1)}-alpha.md", "Worked on project/alpha.")
    return tmp_path


def _llm(response: str = "[]"):
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return response, "fake-model"
    return _fn


def _archived(memory_dir: Path) -> set[str]:
    return {p.name for p in (memory_dir / "archive").rglob("*.md")}


def _in_daily(memory_dir: Path) -> set[str]:
    return {p.name for p in (memory_dir / "daily").glob("*.md")}


def test_old_project_less_daily_notes_are_retired_by_age(store, caplog):
    """The issue's own two names: one note date-named, one dated only by mtime.
    Both are long out of the weekly's lookback window, so no pass would ever
    collect them again."""
    dated = _write(store / "daily" / f"{_dated(40)}.md", "A reflection that names nothing.")
    undated = _age(
        _write(store / "daily" / "_canary-edd5ec84.md", "Canary write, no project."), 45
    )

    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        result = runner.run_consolidation(llm_fn=_llm())

    assert {dated.name, undated.name} <= _archived(store)
    assert dated.name not in _in_daily(store)
    assert undated.name not in _in_daily(store)
    assert (store / "archive" / _dated(40)[:4] / dated.name).is_file()
    assert result["notes_no_project_retired"] == 2
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "retired 2 daily note(s) with no project/ reference older than 30 days" in m
        for m in info
    )


def test_young_project_less_note_and_old_project_notes_stay(store, monkeypatch):
    """Only the class no pass can ever read retires on age: a project-less note
    inside the window may still be tagged, and an old note that names a
    project — by ref, by ``entities:`` or by the keyword fallback — belongs to
    the decided-on rule, not this one."""
    monkeypatch.setattr(config.consolidation, "daily_note_retention_days", 30, raising=False)
    monkeypatch.setattr(
        config.consolidation, "keyword_map", {"project/alpha": ["Widgetron"]}
    )
    young = _write(store / "daily" / f"{_dated(10)}-young.md", "Names nothing yet.")
    by_ref = _write(store / "daily" / f"{_dated(60)}-ref.md", "Old work on project/alpha.")
    by_entity = _write(
        store / "daily" / f"{_dated(60)}-entity.md",
        "Old work, tagged in frontmatter.",
        "category: daily\nentities: [project/alpha]\n",
    )
    by_keyword = _write(store / "daily" / f"{_dated(60)}-kw.md", "Tuned the Widgetron.")
    person_only = _write(store / "daily" / f"{_dated(60)}-pat.md", "Lunch with person/pat.")

    result = runner.run_consolidation(llm_fn=_llm())

    left = _in_daily(store)
    assert {young.name, by_ref.name, by_entity.name, by_keyword.name} <= left
    # A person/ ref forms no group either, so it is project-less for this rule.
    assert person_only.name in _archived(store)
    assert result["notes_no_project_retired"] == 1


def test_superseded_only_daily_note_is_never_retired_by_age(store):
    """ADR-020: a document that declared itself identity-like retires only by
    supersession, never by age — the same classifier every age sweep asks."""
    kept = _write(
        store / "daily" / f"{_dated(60)}-pinned.md",
        "Pinned on purpose.",
        "category: daily\nretirement_policy: superseded-only\n",
    )

    result = runner.run_consolidation(llm_fn=_llm())

    assert kept.name in _in_daily(store)
    assert result["notes_no_project_retired"] == 0


def test_insights_are_never_retired_by_age(store):
    """The class the decided-on rule was written to protect: durable typed
    memories with no project ref. The age rule is for ``daily/`` alone."""
    insight = _age(
        _write(
            store / "insights" / "a-durable-finding.md",
            "A finding with no project.",
            "category: insight\nlast_updated: " + _dated(60) + "\n",
        ),
        60,
    )

    runner.run_consolidation(llm_fn=_llm())

    assert insight.is_file()
    assert not (store / "archive").exists() or insight.name not in _archived(store)


def test_dry_run_counts_and_moves_nothing(store):
    old = _write(store / "daily" / f"{_dated(40)}.md", "Names nothing.")

    result = runner.run_consolidation(dry_run=True, llm_fn=_llm())

    assert result["notes_no_project_retired"] == 1
    assert old.name in _in_daily(store)
    assert not (store / "archive").exists()


def test_zero_disables_the_sweep(store, monkeypatch):
    monkeypatch.setattr(config.consolidation, "daily_note_retention_days", 0, raising=False)
    old = _write(store / "daily" / f"{_dated(400)}.md", "Names nothing.")

    result = runner.run_consolidation(llm_fn=_llm())

    assert old.name in _in_daily(store)
    assert result["notes_no_project_retired"] == 0


def test_sweep_runs_when_the_window_holds_no_notes(store):
    """The backlog is exactly the case where nothing recent was written: a pass
    that returns early on an empty window must still retire it."""
    (store / "daily" / f"{_dated(1)}-alpha.md").unlink()
    old = _write(store / "daily" / f"{_dated(40)}.md", "Names nothing.")

    result = runner.run_consolidation(llm_fn=_llm())

    assert result["status"] == "no notes found"
    assert old.name in _archived(store)
    assert result["notes_no_project_retired"] == 1


def test_sweep_runs_when_every_group_fails(store):
    """A failed group gates the notes it read, not ones no group could read."""
    old = _write(store / "daily" / f"{_dated(40)}.md", "Names nothing.")

    def _boom(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        raise TimeoutError("llm timed out")

    result = runner.run_consolidation(llm_fn=_boom)

    assert result["projects_failed"] == 1
    assert old.name in _archived(store)
    assert f"{_dated(1)}-alpha.md" in _in_daily(store)
    assert result["notes_no_project_retired"] == 1


def test_retired_note_commit_names_the_age_rule(store, monkeypatch):
    """Every move lands as one commit whose subject says why it moved."""
    monkeypatch.setattr(config.git, "auto_commit", True)
    old = _write(store / "daily" / f"{_dated(40)}.md", "Names nothing.")
    git = ["git", "-C", str(store)]
    subprocess.run([*git, "init", "-q"], check=True)
    # In the repo's own config, not per command: the runner commits too, and a
    # CI runner has no global identity to fall back on.
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "seed"], check=True)

    runner.run_consolidation(llm_fn=_llm(json.dumps([])))

    archived = f"archive/{_dated(40)[:4]}/{old.name}"
    log = subprocess.run(
        [*git, "log", "-1", "--full-diff", "--format=%s", "--name-status", "--", archived],
        check=True, capture_output=True, text=True,
    ).stdout
    subject = log.splitlines()[0]
    assert "age-retention" in subject
    assert "no project" in subject
    assert old.name in subject
    # The deletion half rides in the same commit as the addition.
    assert f"daily/{old.name}" in log
