"""The nightly consolidation watermark — per-project, advanced on success only.

The nightly used to select notes with a UTC calendar-date cutoff. These tests
drive the real runner→executor path on a real ``tmp_path`` store, with a fake
at the propose seam (``llm_fn``) and a frozen clock, and assert on two on-disk
facts: what the model was shown, and what the state file records afterwards.

Every note's "written at" is set with ``os.utime`` because that is what the
watermark compares against — the file's modification time, not a date parsed
out of its name.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from palinode.consolidation import activity_gate, runner, watermark
from palinode.core.config import config
from palinode.core.ollama_client import ChatCompletionText

#: 2026-09-21 11:00 UTC — the shipped crontab's nightly tick (4am PDT).
NOW = datetime(2026, 9, 21, 11, 0, 0, tzinfo=UTC)

DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    """The pass's start time, which is also the stamp a success records."""
    state = {"now": NOW}
    monkeypatch.setattr(activity_gate, "_utc_now", lambda: state["now"])
    return state


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """Two consolidatable projects, no notes yet, nothing committed."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for sub in ("projects", "daily", "specs/prompts"):
        (tmp_path / sub).mkdir(parents=True)
    for prompt in ("compaction.md", "nightly-consolidation.md"):
        (tmp_path / "specs" / "prompts" / prompt).write_text(
            "Return consolidation operations as a JSON array.\n", encoding="utf-8"
        )
    for slug, fact_id in (("alpha", "a1"), ("beta", "b1")):
        (tmp_path / "projects" / f"{slug}.md").write_text(
            f"---\nid: projects-{slug}\ncategory: project\n---\n\n# {slug}\n\n"
            f"- [2026-06-01] Old {slug} fact. <!-- fact:{fact_id} -->\n",
            encoding="utf-8",
        )
    return tmp_path


def _note(store: Path, name: str, body: str, written_at: datetime) -> Path:
    """A daily note whose *modification time* is ``written_at``."""
    path = store / "daily" / f"{name}.md"
    path.write_text(
        f"---\nid: {name}\ncategory: daily\n---\n\n{body}\n", encoding="utf-8"
    )
    stamp = written_at.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _update_op(fact_id: str) -> str:
    return json.dumps([{"op": "UPDATE", "id": fact_id, "new_text": f"Updated {fact_id}."}])


def _llm(seen: list[str] | None = None, **per_project):
    """Propose seam keyed on which project file the prompt names.

    A value that is an exception instance is raised; anything else is returned
    as the response. ``seen`` collects every user prompt, which is how these
    tests assert on *what the model was shown* rather than on an internal.
    """
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        if seen is not None:
            seen.append(user_prompt)
        for slug, behaviour in per_project.items():
            if f"from {slug}.md" in user_prompt:
                if isinstance(behaviour, BaseException):
                    raise behaviour
                return behaviour, "fake-model"
        return "[]", "fake-model"
    return _fn


def _marks(store: Path) -> dict[str, str]:
    path = activity_gate.state_path(store)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw.get("watermarks", {}).get("nightly", {})


# ---------------------------------------------------------------------------
# 1. Advance on success, per project
# ---------------------------------------------------------------------------


def test_a_success_advances_only_the_succeeding_projects_mark(store, clock) -> None:
    """The 2026-09-15 shape: one project fails while another could succeed."""
    _note(store, "2026-09-21", "Worked on project/alpha today.", NOW - timedelta(hours=2))
    _note(store, "2026-09-21-beta", "Worked on project/beta today.", NOW - timedelta(hours=2))

    result = runner.run_nightly(
        llm_fn=_llm(alpha=_update_op("a1"), beta=TimeoutError("llm timed out"))
    )

    assert result["status"] == "partial"
    assert result["projects_resolved"] == ["alpha"]
    assert result["watermark_advanced"] == ["alpha"]
    assert result["watermark_at"] == "2026-09-21T11:00:00Z"
    # The executor really ran for alpha.
    assert "Updated a1." in (store / "projects" / "alpha.md").read_text()
    # And only alpha's mark moved: beta has none at all, so its notes are still
    # selected from the cold-start moment next run.
    assert _marks(store) == {"alpha": "2026-09-21T11:00:00Z"}


def test_a_project_that_proposed_nothing_still_advances(store, clock) -> None:
    """The model saw the notes and chose to change nothing — that is a decision,
    and re-sending them tomorrow would buy the same answer at the same cost."""
    _note(store, "2026-09-21", "Worked on project/alpha today.", NOW - timedelta(hours=2))

    result = runner.run_nightly(llm_fn=_llm(alpha="[]"))

    assert result["status"] == "success"
    assert result["projects_compacted"] == 0
    assert result["projects_resolved"] == ["alpha"]
    assert _marks(store) == {"alpha": "2026-09-21T11:00:00Z"}


def test_a_truncated_response_does_not_advance(store, clock) -> None:
    """``finish_reason=length`` applied nothing, so nothing was consolidated."""
    _note(store, "2026-09-21", "Worked on project/alpha today.", NOW - timedelta(hours=2))
    cut_off = ChatCompletionText(
        '[{"op": "UPDATE", "id": "a1", "new_t', finish_reason="length"
    )

    result = runner.run_nightly(llm_fn=_llm(alpha=cut_off))

    assert result["status"] == "partial"
    assert result["failed_projects"] == ["alpha"]
    assert result["projects_resolved"] == []
    assert "watermark_advanced" not in result
    assert _marks(store) == {}


def test_an_executor_failure_does_not_advance(store, clock, monkeypatch) -> None:
    """A raise inside the per-project step is a failed group, mark included."""
    from palinode.consolidation import executor

    _note(store, "2026-09-21", "Worked on project/alpha today.", NOW - timedelta(hours=2))

    def _explode(*args, **kwargs):
        raise ValueError("executor rejected the ops")

    monkeypatch.setattr(executor, "apply_operations", _explode)

    result = runner.run_nightly(llm_fn=_llm(alpha=_update_op("a1")))

    assert result["failed_projects"] == ["alpha"]
    assert _marks(store) == {}


def test_a_dry_run_advances_nothing(store, clock) -> None:
    _note(store, "2026-09-21", "Worked on project/alpha today.", NOW - timedelta(hours=2))

    result = runner.run_nightly(dry_run=True, llm_fn=_llm(alpha=_update_op("a1")))

    assert result["projects_resolved"] == ["alpha"], "the preview still says what would advance"
    assert not activity_gate.state_path(store).exists()


def test_a_run_refused_by_the_lock_advances_nothing(store, clock) -> None:
    """The lock raises before the pass starts, so there is nothing to record."""
    from palinode.consolidation.run_lock import (
        ConsolidationAlreadyRunning,
        consolidation_run_lock,
    )

    _note(store, "2026-09-21", "Worked on project/alpha today.", NOW - timedelta(hours=2))
    with consolidation_run_lock():
        with pytest.raises(ConsolidationAlreadyRunning):
            runner.run_nightly(llm_fn=_llm(alpha=_update_op("a1")))

    assert _marks(store) == {}


# ---------------------------------------------------------------------------
# 2. Selection — once, and again only after a failure
# ---------------------------------------------------------------------------


def test_a_failed_project_is_re_selected_and_a_succeeding_one_is_not(store, clock) -> None:
    """The self-heal `--days 3` was bought to approximate, without the re-send."""
    _note(store, "2026-09-21-alpha", "Worked on project/alpha.", NOW - timedelta(hours=2))
    _note(store, "2026-09-21-beta", "Worked on project/beta.", NOW - timedelta(hours=2))
    runner.run_nightly(llm_fn=_llm(alpha=_update_op("a1"), beta=TimeoutError("down")))

    clock["now"] = NOW + DAY
    seen: list[str] = []
    result = runner.run_nightly(llm_fn=_llm(seen, alpha=_update_op("a1"), beta=_update_op("b1")))

    assert result["projects_resolved"] == ["beta"]
    assert result["processed_notes"] == 1
    assert len(seen) == 1, "alpha had nothing new, so it cost no inference at all"
    assert "2026-09-21-beta" in seen[0]
    assert "2026-09-21-alpha" not in seen[0]
    assert _marks(store) == {
        "alpha": "2026-09-21T11:00:00Z",
        "beta": "2026-09-22T11:00:00Z",
    }


def test_a_skipped_night_is_covered_without_reprocessing(store, clock) -> None:
    """Night 1 succeeds, night 2 fails outright, night 3 sees exactly nights 2
    and 3 — not night 1's note, which is already in the project document."""
    _note(store, "2026-09-21", "Day one on project/alpha.", NOW - timedelta(hours=2))
    runner.run_nightly(llm_fn=_llm(alpha=_update_op("a1")))

    clock["now"] = NOW + DAY
    _note(store, "2026-09-22", "Day two on project/alpha.", NOW + DAY - timedelta(hours=2))
    runner.run_nightly(llm_fn=_llm(alpha=TimeoutError("endpoint down")))

    clock["now"] = NOW + 2 * DAY
    _note(store, "2026-09-23", "Day three on project/alpha.", NOW + 2 * DAY - timedelta(hours=2))
    seen: list[str] = []
    result = runner.run_nightly(llm_fn=_llm(seen, alpha=_update_op("a1")))

    assert result["status"] == "success"
    assert result["processed_notes"] == 2
    assert "Day two" in seen[0]
    assert "Day three" in seen[0]
    assert "Day one" not in seen[0]
    assert _marks(store) == {"alpha": "2026-09-23T11:00:00Z"}


def test_notes_around_utc_midnight_are_picked_up_exactly_once(store, clock) -> None:
    """A PDT working day runs from 16:00 UTC to 02:00 UTC the next day, so its
    sessions land in two date-named files — and a capture appended to an older
    file is invisible to any date comparison. All three are selected once."""
    yesterday = NOW - DAY
    watermark.advance(["alpha"], yesterday, memory_dir=store)

    # Before midnight UTC, after midnight UTC, and an append to a file named
    # three days ago (the date cutoff would have rejected it unopened).
    _note(store, "2026-09-20", "Afternoon on project/alpha.", NOW - timedelta(hours=19))
    _note(store, "2026-09-21", "Late evening on project/alpha.", NOW - timedelta(hours=10, minutes=30))
    _note(store, "2026-09-18", "Backdated capture for project/alpha.", NOW - timedelta(hours=10, minutes=15))

    first: list[str] = []
    one = runner.run_nightly(llm_fn=_llm(first, alpha=_update_op("a1")))

    assert one["processed_notes"] == 3
    for name in ("2026-09-20", "2026-09-21", "2026-09-18"):
        assert name in first[0], f"{name} was not selected"

    clock["now"] = NOW + DAY
    second: list[str] = []
    two = runner.run_nightly(llm_fn=_llm(second, alpha=_update_op("a1")))

    assert two["status"] == "no_new_notes"
    assert second == [], "nothing was written since the mark, so nothing is re-sent"


def test_the_mark_survives_a_restart(store, clock) -> None:
    """Nothing is cached in the process: the mark is read back off disk, and
    the gate's own keys are still beside it."""
    _note(store, "2026-09-21", "Worked on project/alpha.", NOW - timedelta(hours=2))
    runner.run_nightly(llm_fn=_llm(alpha=_update_op("a1")))

    state = json.loads(activity_gate.state_path(store).read_text(encoding="utf-8"))
    assert state["watermarks"]["nightly"]["alpha"] == "2026-09-21T11:00:00Z"
    assert state["modes"]["nightly"]["last_run_at"] == "2026-09-21T11:00:00Z"
    assert [run["status"] for run in state["runs"]["nightly"]] == ["success"]
    assert watermark.load(memory_dir=store) == {"alpha": NOW}


def test_a_mark_never_moves_backwards(store) -> None:
    """A hand-run pass racing a long cron tick must not re-expose covered notes."""
    watermark.advance(["alpha"], NOW, memory_dir=store)

    assert watermark.advance(["alpha"], NOW - DAY, memory_dir=store) == []
    assert _marks(store) == {"alpha": "2026-09-21T11:00:00Z"}


# ---------------------------------------------------------------------------
# 3. Cold start
# ---------------------------------------------------------------------------


def test_cold_start_with_no_history_reaches_back_one_catchup_bound(store, clock, caplog) -> None:
    _note(store, "2026-09-12", "Ancient project/alpha note.", NOW - timedelta(days=9))
    _note(store, "2026-09-19", "Recent project/alpha note.", NOW - timedelta(days=2))

    seen: list[str] = []
    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        result = runner.run_nightly(llm_fn=_llm(seen, alpha=_update_op("a1")))

    assert result["processed_notes"] == 1
    assert "Recent" in seen[0]
    assert "Ancient" not in seen[0]
    assert "no successful nightly pass recorded" in caplog.text


def test_cold_start_seeds_from_the_last_successful_outcome(store, clock) -> None:
    """An existing store upgrading: the outcome log knows when the last pass
    that worked ran, and everything before it is already consolidated."""
    activity_gate.record_outcome(
        "nightly",
        started_at=NOW - timedelta(hours=25),
        finished_at=NOW - timedelta(hours=25) + timedelta(minutes=1),
        status="success",
        lookback_days=1,
        memory_dir=store,
    )
    _note(store, "2026-09-18", "Before the last good pass on project/alpha.", NOW - timedelta(hours=40))
    _note(store, "2026-09-21", "After the last good pass on project/alpha.", NOW - timedelta(hours=3))

    seen: list[str] = []
    result = runner.run_nightly(llm_fn=_llm(seen, alpha=_update_op("a1")))

    assert result["processed_notes"] == 1
    assert "After the last good pass" in seen[0]
    assert "Before the last good pass" not in seen[0]


def test_cold_start_falls_back_to_the_gate_clock(store, clock) -> None:
    """A state file written before outcomes existed carries only the clock, and
    it is still the last successful start this store knows of."""
    activity_gate.record_run("nightly", memory_dir=store, now=NOW - timedelta(hours=25))
    _note(store, "2026-09-18", "Before the last good pass on project/alpha.", NOW - timedelta(hours=40))
    _note(store, "2026-09-21", "After the last good pass on project/alpha.", NOW - timedelta(hours=3))

    seen: list[str] = []
    result = runner.run_nightly(llm_fn=_llm(seen, alpha=_update_op("a1")))

    assert result["processed_notes"] == 1
    assert "After the last good pass" in seen[0]


def test_a_cold_start_older_than_the_bound_is_clamped(store, clock) -> None:
    """The store consolidated last in spring; the bound, not the log, wins."""
    activity_gate.record_outcome(
        "nightly",
        started_at=NOW - timedelta(days=90),
        finished_at=NOW - timedelta(days=90),
        status="success",
        memory_dir=store,
    )

    floor = watermark.floor_at(NOW, config.consolidation.nightly.lookback_days)
    since, reason = watermark.cold_start(floor, memory_dir=store)

    assert since == floor
    assert "older than the catch-up bound" in reason


# ---------------------------------------------------------------------------
# 4. The floor — bounded, and never silent
# ---------------------------------------------------------------------------


def test_a_long_failed_mark_is_clamped_and_the_skip_is_logged(store, clock, caplog) -> None:
    """One abandoned project must not hand the model a month of notes — and
    the bound that stops it has to say what it left out."""
    watermark.advance(["alpha"], NOW - timedelta(days=30), memory_dir=store)
    _note(store, "2026-09-01", "Inside the gap for project/alpha.", NOW - timedelta(days=20))
    _note(store, "2026-09-05", "Also inside the gap for project/alpha.", NOW - timedelta(days=16))
    _note(store, "2026-09-21", "Fresh project/alpha note.", NOW - timedelta(hours=3))

    seen: list[str] = []
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation"):
        result = runner.run_nightly(llm_fn=_llm(seen, alpha=_update_op("a1")))

    assert result["watermark_clamped"] == ["alpha"]
    assert result["processed_notes"] == 1
    assert "Fresh" in seen[0]
    assert "Inside the gap" not in seen[0]
    assert "older than the 7-day catch-up bound" in caplog.text
    assert "2 note file(s) written in that gap are not selected" in caplog.text
    # The clamp is not a failure: the pass consolidated what it could see, and
    # the mark moves so the gap is not re-reported every night.
    assert result["status"] == "success"
    assert _marks(store) == {"alpha": "2026-09-21T11:00:00Z"}


# ---------------------------------------------------------------------------
# 5. `--days` compatibility — the flag in the wild
# ---------------------------------------------------------------------------


def test_days_is_the_catchup_bound_not_a_window(store, clock) -> None:
    """The dogfood host's `--nightly --days 3`: accepted, and now bounding the
    catch-up. Note the direction — it narrows the *floor*, not the selection of
    anything a mark already covers."""
    _note(store, "2026-09-16", "Five days back, project/alpha.", NOW - timedelta(days=5))
    _note(store, "2026-09-20", "Two days back, project/alpha.", NOW - timedelta(days=2))

    seen: list[str] = []
    result = runner.run_nightly(lookback_days=3, llm_fn=_llm(seen, alpha=_update_op("a1")))

    assert result["processed_notes"] == 1
    assert "Two days back" in seen[0]
    assert "Five days back" not in seen[0]
    # And the bound is what doctor reads back for its thresholds.
    assert activity_gate.run_history("nightly", store)[-1]["lookback_days"] == 3


def test_the_cron_entry_point_still_accepts_days(store, clock, monkeypatch, caplog) -> None:
    """The cron lines in the wild must not crash, and the log must say that the
    number now means something else."""
    import sys

    from palinode.consolidation import cron

    captured: dict = {}

    def _fake_nightly(lookback_days=None, **kwargs):
        captured["lookback_days"] = lookback_days
        return {"status": "no_new_notes", "processed_notes": 0}

    monkeypatch.setattr(cron, "run_nightly", _fake_nightly)
    monkeypatch.setattr(sys, "argv", ["cron", "--nightly", "--days", "3", "--ignore-gate"])

    with caplog.at_level(logging.INFO, logger="palinode.consolidation.cron"):
        cron.main()

    assert captured["lookback_days"] == 3
    assert "catch-up bound" in caplog.text
    assert "not a lookback window" in caplog.text
