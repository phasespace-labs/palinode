"""Consolidation must be reachable for memories that are not daily notes.

``run_consolidation`` collected ``daily/*.md`` and nothing else, so the
deterministic op executor — the architecture's headline differentiator — could
only ever be triggered for one directory. A store built the documented way,
with typed saves through ``/save`` or MCP, consolidated to
``{"status": "no notes found"}`` no matter how many Insights it held.

The defect is a gap between the architecture story and the API rather than a
crash, which is why nothing caught it: every existing test fed the collector
daily notes, the shape it already handled.

These tests pin both directions — the new corpora are reachable, *and* the
default path is byte-for-byte what it was, because a silent change to what
consolidation touches is worse than the gap it fixes.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from palinode.consolidation import runner
from palinode.core.config import config


def _write(path, body: str, frontmatter: str | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\n{frontmatter}---\n\n{body}\n" if frontmatter else body
    path.write_text(text, encoding="utf-8")
    return str(path)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A memory dir with one recent daily note and one typed Insight."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    _write(
        tmp_path / "daily" / f"{_today()}.md",
        "Worked on project/alpha today; shipped the parser.",
        "id: daily-today\n",
    )
    _write(
        tmp_path / "insights" / "parser-lesson.md",
        "The parser only fails on inputs no caller produces.",
        f"id: insights-parser-lesson\ntype: Insight\n"
        f"entities:\n  - project/alpha\nlast_updated: '{_today()}T10:00:00+00:00'\n",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------

def test_insights_are_collected_by_default(store):
    """The weekly default covers durable findings, not just the daily stream.

    A store built the documented way keeps its findings in ``insights/``. Making
    those opt-in would have left the executor unreachable for exactly the
    memories most worth consolidating, which is the defect this fixes.
    """
    notes, _ = runner._collect_daily_notes(7)

    collected = {os.path.basename(n["filepath"]) for n in notes}
    assert collected == {f"{_today()}.md", "parser-lesson.md"}


def test_a_narrower_corpus_can_still_be_requested(store):
    """`sources` narrows as well as widens."""
    notes, _ = runner._collect_daily_notes(7, sources=["insights"])

    assert len(notes) == 1
    assert notes[0]["filepath"].endswith("parser-lesson.md")
    assert "no caller produces" in notes[0]["content"]


def test_nightly_stays_daily_only(store, monkeypatch):
    """Nightly must not inherit the widened weekly default.

    "Today's daily notes only" is `run_nightly`'s contract. It is pinned to its
    own source tuple precisely so widening the weekly default cannot broaden
    the nightly sweep by accident.
    """
    seen: dict = {}
    real = runner._collect_daily_notes

    def _spy(*args, **kwargs):
        seen["sources"] = kwargs.get("sources")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "_collect_daily_notes", _spy)

    def _no_ops(_prompt: str, _content: str) -> tuple[str, str]:
        return "[]", "stub"

    runner.run_nightly(dry_run=True, llm_fn=_no_ops)

    assert seen["sources"] == runner.NIGHTLY_CONSOLIDATION_SOURCES
    assert "insights" not in seen["sources"]


def test_multiple_sources_are_unioned(store):
    notes, _ = runner._collect_daily_notes(7, sources=["daily", "insights"])

    assert len(notes) == 2
    assert {os.path.basename(n["filepath"]) for n in notes} == {
        f"{_today()}.md",
        "parser-lesson.md",
    }


def test_a_store_with_no_daily_notes_still_consolidates(tmp_path, monkeypatch):
    """The exact shape reported: typed memories, no daily/ at all.

    Drives the real orchestrator with an injected LLM so the assertion is about
    reachability, not about what a model proposes.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _write(
        tmp_path / "insights" / "only-memory.md",
        "A finding about project/alpha worth keeping.",
        f"id: insights-only\ntype: Insight\nentities:\n  - project/alpha\n"
        f"last_updated: '{_today()}T10:00:00+00:00'\n",
    )

    def _no_ops(prompt: str, _content: str) -> tuple[str, str]:
        return "[]", "stub"

    # The reported shape: no daily/ at all. Before the fix this returned
    # "no notes found" because the scan was hardcoded to daily/.
    default_run = runner.run_consolidation(dry_run=True, llm_fn=_no_ops)
    assert default_run["status"] != "no notes found", (
        "consolidation is still unreachable for typed memories"
    )
    assert default_run["processed_notes"] == 1

    # And narrowing back to daily/ reproduces the old, empty behaviour —
    # proving the default is what changed, not the collector's floor.
    daily_only = runner.run_consolidation(
        dry_run=True, llm_fn=_no_ops, sources=["daily"]
    )
    assert daily_only["status"] == "no notes found"


# ---------------------------------------------------------------------------
# Dates: filename for daily, frontmatter for everything else
# ---------------------------------------------------------------------------

def test_typed_memory_outside_the_lookback_is_excluded(tmp_path, monkeypatch):
    """Frontmatter supplies the date a non-date-named file is filed under.

    Without this the cutoff compares a *filename* against a date string, and a
    typed memory is included or dropped according to how its first characters
    happen to sort — not according to the lookback.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _write(
        tmp_path / "insights" / "ancient.md",
        "Old finding about project/alpha.",
        f"id: i-old\ntype: Insight\nlast_updated: '{_days_ago(90)}T10:00:00+00:00'\n",
    )
    _write(
        tmp_path / "insights" / "recent.md",
        "New finding about project/alpha.",
        f"id: i-new\ntype: Insight\nlast_updated: '{_days_ago(1)}T10:00:00+00:00'\n",
    )

    notes, _ = runner._collect_daily_notes(7, sources=["insights"])

    names = {os.path.basename(n["filepath"]) for n in notes}
    assert names == {"recent.md"}, f"lookback not honoured for typed memories: {names}"


def test_undated_memory_falls_back_to_mtime(tmp_path, monkeypatch):
    """No usable frontmatter date — mtime beats guessing from the filename."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _write(
        tmp_path / "insights" / "zzz-no-date.md",
        "Finding about project/alpha with no date anywhere.",
        "id: i-undated\ntype: Insight\n",
    )

    notes, _ = runner._collect_daily_notes(7, sources=["insights"])

    assert len(notes) == 1
    assert notes[0]["date"] == _today()


def test_daily_note_date_still_comes_from_the_filename(store):
    notes, _ = runner._collect_daily_notes(7)

    assert notes[0]["date"] == _today()


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_entities_frontmatter_supplies_project_refs(tmp_path, monkeypatch):
    """A typed memory names its subject in `entities:`, not necessarily in prose.

    ``palinode_save`` records entities as frontmatter, so a body-only regex is
    the wrong place to look for a typed memory's subject.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _write(
        tmp_path / "insights" / "quiet.md",
        "A finding whose body names no reference at all.",
        f"id: i-quiet\ntype: Insight\nentities:\n  - project/beta\n  - person/casey\n"
        f"last_updated: '{_today()}T10:00:00+00:00'\n",
    )

    notes, _ = runner._collect_daily_notes(7, sources=["insights"])

    assert "project/beta" in notes[0]["mentions"]
    assert "person/casey" in notes[0]["mentions"]
    assert runner._group_by_project(notes).get("beta"), "should group under its entity"


def test_malformed_entities_do_not_break_collection(tmp_path, monkeypatch):
    """`entities:` as a bare string, or carrying junk, must not raise."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    _write(
        tmp_path / "insights" / "odd.md",
        "Body mentions project/alpha.",
        f"id: i-odd\ntype: Insight\nentities: project/gamma\n"
        f"last_updated: '{_today()}T10:00:00+00:00'\n",
    )

    notes, _ = runner._collect_daily_notes(7, sources=["insights"])

    assert "project/gamma" in notes[0]["mentions"]
    assert "project/alpha" in notes[0]["mentions"], "body refs still collected"


def test_non_mapping_frontmatter_is_survivable(tmp_path, monkeypatch):
    """Frontmatter parsing to a scalar must not abort the sweep."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    path = tmp_path / "insights" / "scalar.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\njust a string\n---\n\nAbout project/alpha.\n", encoding="utf-8")

    notes, _ = runner._collect_daily_notes(7, sources=["insights"])

    assert len(notes) == 1


def test_missing_source_directory_is_not_an_error(store):
    notes, _ = runner._collect_daily_notes(7, sources=["nope-not-here"])

    assert notes == []
