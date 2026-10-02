"""Tests for the ``consolidation_last_run`` doctor check and the outcome
records that feed it.

Every state file here is a real file under ``tmp_path``, written either by
``activity_gate.record_outcome`` (the same code the runner calls) or by the
runner itself driving a real pass. The check reads it back through the same
path resolution production uses; the only seams stubbed are the clock (so the
"h ago" arithmetic is exact) and ``sys.platform`` (so the platform note is
exercised both ways on any developer machine).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

import palinode.cli.doctor  # noqa: F401 — ensure the submodule is imported
from palinode.cli.doctor import doctor as doctor_cmd

# `palinode.cli.__init__` rebinds the name `doctor` to the click command, so
# `palinode.cli.doctor` is the Command, not the module. Reach the module here.
doctor_module = sys.modules["palinode.cli.doctor"]
from palinode.consolidation import activity_gate, runner
from palinode.core.config import Config, config
from palinode.diagnostics.checks import consolidation_last_run as check_module
from palinode.diagnostics.checks.consolidation_last_run import consolidation_last_run
from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import DoctorContext

NOW = datetime(2026, 9, 16, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """An empty memory dir, a Linux platform, and a frozen clock."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(check_module, "_utc_now", lambda: NOW)
    return tmp_path


def _ctx(store: Path) -> DoctorContext:
    cfg = Config()
    cfg.memory_dir = str(store)
    return DoctorContext(config=cfg)


def _record(
    store: Path,
    mode: str,
    status: str,
    *,
    hours_ago: float,
    failed: tuple[str, ...] = (),
    error: str | None = None,
    days: int | None = 3,
) -> None:
    started = NOW - timedelta(hours=hours_ago)
    activity_gate.record_outcome(
        mode,
        started_at=started,
        finished_at=started + timedelta(minutes=2),
        status=status,
        failed_projects=failed,
        lookback_days=days,
        error=error,
        memory_dir=store,
    )


def _state(store: Path) -> dict:
    return json.loads(activity_gate.state_path(store).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The green result — the facts an operator wants even when nothing is wrong
# ---------------------------------------------------------------------------


def test_a_successful_nightly_passes_and_reports_the_facts(store: Path) -> None:
    _record(store, "nightly", "success", hours_ago=14)

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert result.remediation is None
    assert "nightly: last run success at 2026-09-15T11:00:00Z" in result.message
    assert "--days 3" in result.message
    assert "14 h ago" in result.message
    assert "0 consecutive failures" in result.message
    assert "last success 2026-09-15T11:00:00Z (14 h ago, daily cadence)" in result.message
    assert "weekly: no run recorded" in result.message


def test_both_modes_are_reported_with_their_own_cadence(store: Path) -> None:
    _record(store, "nightly", "success", hours_ago=14)
    _record(store, "weekly", "success", hours_ago=62)

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert "nightly: last run success" in result.message
    assert "weekly: last run success" in result.message
    assert "62 h ago, weekly cadence" in result.message


# ---------------------------------------------------------------------------
# 2. The streak
# ---------------------------------------------------------------------------


def test_one_partial_is_reported_but_does_not_fail(store: Path) -> None:
    """The 2026-09-15 shape: one partial, the previous night fine."""
    _record(store, "nightly", "success", hours_ago=38)
    _record(store, "nightly", "partial", hours_ago=14, failed=("palinode",))

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert "nightly: last run partial at 2026-09-15T11:00:00Z" in result.message
    assert "failed: palinode" in result.message
    assert "1 consecutive failure;" in result.message
    assert "last success 2026-09-14T11:00:00Z (38 h ago" in result.message
    assert "loses a day" not in result.message


def test_two_consecutive_failures_warn_and_say_why(store: Path) -> None:
    """Two in a row is "the nightly has stopped working" — before anything is
    lost, which under a watermark it is not yet."""
    _record(store, "nightly", "success", hours_ago=62)
    _record(store, "nightly", "partial", hours_ago=38, failed=("palinode",))
    _record(store, "nightly", "partial", hours_ago=14, failed=("palinode",))

    result = consolidation_last_run(_ctx(store))

    assert result.passed is False
    assert result.severity == "warn"
    assert "2 consecutive failures" in result.message
    assert "nothing is lost yet" in result.message
    assert "1 more day(s) of catch-up remain" in result.message
    assert "3-day catch-up bound" in result.message
    assert "weekly's 3-day window" in result.message
    assert "last success 2026-09-13T11:00:00Z (62 h ago" in result.message
    assert result.remediation
    assert "palinode consolidate --nightly" in result.remediation


def test_a_streak_as_long_as_the_catchup_bound_fails(store: Path) -> None:
    """The error is the streak that actually drops notes: a mark older than
    the bound is clamped by the floor, and the gap leaves the nightly's view."""
    _record(store, "nightly", "success", hours_ago=86)
    for hours in (62, 38, 14):
        _record(store, "nightly", "partial", hours_ago=hours, failed=("palinode",))

    result = consolidation_last_run(_ctx(store))

    assert result.passed is False
    assert result.severity == "error"
    assert "3 consecutive failures" in result.message
    assert "3 in a row reaches the 3-day catch-up bound" in result.message
    assert "no longer selected" in result.message
    assert result.remediation


def test_thresholds_are_the_catchup_bound_with_a_fixed_warn() -> None:
    """Error where notes start being dropped; warn at two regardless, since a
    failed nightly no longer costs a day."""
    assert check_module.nightly_thresholds(7) == (2, 7)  # the shipped default
    assert check_module.nightly_thresholds(3) == (2, 3)
    assert check_module.nightly_thresholds(2) == (2, 2)
    assert check_module.nightly_thresholds(1) == (1, 1)
    assert check_module.nightly_thresholds(0) == (1, 1)


def test_the_shipped_default_bound_makes_two_failures_a_warning(store: Path) -> None:
    """The message has to be true on a default install: with the shipped 7-day
    bound a single failure costs nothing, so it is not an error."""
    _record(store, "nightly", "success", hours_ago=38, days=7)
    _record(store, "nightly", "partial", hours_ago=14, failed=("palinode",), days=7)

    one = consolidation_last_run(_ctx(store))
    assert one.passed is True
    assert one.severity == "info"
    assert "1 consecutive failure;" in one.message

    _record(store, "nightly", "partial", hours_ago=10, failed=("palinode",), days=7)
    two = consolidation_last_run(_ctx(store))
    assert two.passed is False
    assert two.severity == "warn"
    assert "7-day catch-up bound" in two.message
    assert "5 more day(s) of catch-up remain" in two.message


def test_a_one_day_bound_is_an_error_on_the_first_failure(store: Path) -> None:
    """A bound that thin leaves no room to warn first: the next run's mark is
    already outside it."""
    _record(store, "nightly", "partial", hours_ago=14, failed=("palinode",), days=1)

    result = consolidation_last_run(_ctx(store))

    assert result.severity == "error"
    assert "1 in a row reaches the 1-day catch-up bound" in result.message
    assert "one run" in result.message


def test_a_record_without_a_lookback_falls_back_to_the_configured_one(store: Path) -> None:
    """The consequence is stated in the configured number, not a literal."""
    for hours in (38, 14):
        _record(store, "nightly", "partial", hours_ago=hours, failed=("palinode",), days=None)

    result = consolidation_last_run(_ctx(store))

    # Config() ships nightly.lookback_days = 7 and weekly lookback_days = 3.
    assert result.severity == "warn"
    assert "7-day catch-up bound" in result.message
    assert "weekly's 3-day window" in result.message


def test_a_success_resets_the_streak(store: Path) -> None:
    """The hand-run that consolidated the failing store on 2026-09-15."""
    _record(store, "nightly", "partial", hours_ago=38, failed=("palinode",))
    _record(store, "nightly", "partial", hours_ago=14, failed=("palinode",))
    _record(store, "nightly", "success", hours_ago=10, days=1)

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert "nightly: last run success" in result.message
    assert "--days 1" in result.message
    assert "0 consecutive failures" in result.message
    assert result.remediation is None


def test_a_pass_that_raised_counts_as_a_failure_and_names_the_exception(store: Path) -> None:
    _record(store, "nightly", "partial", hours_ago=38, failed=("palinode",))
    _record(store, "nightly", "error", hours_ago=14, error="RuntimeError: LLM unreachable")

    result = consolidation_last_run(_ctx(store))

    assert result.severity == "warn"
    assert "nightly: last run error" in result.message
    assert "raised RuntimeError: LLM unreachable" in result.message
    assert "2 consecutive failures" in result.message


def test_an_idle_pass_is_a_success_for_the_streak(store: Path) -> None:
    """A window with no notes leaves nothing unconsolidated."""
    _record(store, "nightly", "partial", hours_ago=38, failed=("palinode",))
    _record(store, "nightly", "no_new_notes", hours_ago=14)

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert "nightly: last run no_new_notes" in result.message
    assert "0 consecutive failures" in result.message


def test_no_success_in_the_history_is_said_rather_than_faked(store: Path) -> None:
    _record(store, "nightly", "partial", hours_ago=14, failed=("palinode",))

    result = consolidation_last_run(_ctx(store))

    assert "no successful run in the recorded history" in result.message


def test_a_weekly_streak_warns_without_the_nightly_arithmetic(store: Path) -> None:
    _record(store, "weekly", "partial", hours_ago=14 + 168, failed=("palinode",))
    _record(store, "weekly", "partial", hours_ago=14, failed=("palinode",))

    result = consolidation_last_run(_ctx(store))

    assert result.severity == "warn"
    assert "2 consecutive weekly passes" in result.message
    assert "loses a day" not in result.message


def test_the_worst_mode_sets_the_severity(store: Path) -> None:
    _record(store, "weekly", "success", hours_ago=62)
    for hours in (62, 38, 14):
        _record(store, "nightly", "partial", hours_ago=hours, failed=("palinode",))

    result = consolidation_last_run(_ctx(store))

    assert result.severity == "error"
    assert "weekly: last run success" in result.message


# ---------------------------------------------------------------------------
# 3. Degradation — info with the reason, never a failure
# ---------------------------------------------------------------------------


def test_missing_state_is_not_applicable_and_names_the_path(store: Path) -> None:
    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert "No consolidation run recorded" in result.message
    assert str(activity_gate.state_path(store)) in result.message
    assert "does not exist" in result.message
    assert "No palinode cron is shipped" not in result.message


def test_unparseable_state_is_not_applicable_with_the_reason(store: Path) -> None:
    path = activity_gate.state_path(store)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert "could not be read" in result.message
    assert "not valid JSON" in result.message


def test_a_state_file_that_is_not_an_object_is_not_applicable(store: Path) -> None:
    path = activity_gate.state_path(store)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert "not a JSON object" in result.message


def test_a_state_path_that_cannot_be_opened_is_not_applicable(store: Path) -> None:
    """A directory where the file should be: read_text raises an OSError that is
    neither missing nor malformed."""
    activity_gate.state_path(store).mkdir(parents=True)

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert "could not be read" in result.message


def test_a_state_file_from_before_outcomes_reports_the_gate_clock(store: Path) -> None:
    """Every host upgrading to this release: the gate's clock and nothing else."""
    path = activity_gate.state_path(store)
    path.parent.mkdir(parents=True)
    stamped = (NOW - timedelta(hours=14)).isoformat().replace("+00:00", "Z")
    path.write_text(
        json.dumps({"modes": {"nightly": {"last_run_at": stamped, "sessions_at_run": 3}}}),
        encoding="utf-8",
    )

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert result.severity == "info"
    assert "nightly: no outcome recorded yet" in result.message
    assert "last stamped a successful run at 2026-09-15T11:00:00Z (14 h ago)" in result.message
    assert "weekly: no run recorded" in result.message


def test_a_state_file_with_no_runs_at_all_is_not_applicable(store: Path) -> None:
    path = activity_gate.state_path(store)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert "records no run for either mode" in result.message


def test_a_malformed_record_is_reported_as_unknown_not_crashed(store: Path) -> None:
    path = activity_gate.state_path(store)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"runs": {"nightly": [{"started_at": "yesterday-ish"}, "not a record"]}}),
        encoding="utf-8",
    )

    result = consolidation_last_run(_ctx(store))

    assert result.passed is True
    assert "nightly: last run unknown at an unreadable time" in result.message
    assert "0 consecutive failures" in result.message


def test_macos_with_no_state_says_no_cron_is_shipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    result = consolidation_last_run(_ctx(tmp_path))

    assert result.passed is True
    assert result.severity == "info"
    assert "No consolidation run recorded" in result.message
    assert "No palinode cron is shipped for darwin" in result.message


def test_macos_with_a_recorded_run_reports_it_like_linux(tmp_path: Path, monkeypatch) -> None:
    """The state file is platform-neutral: a hand-run pass on a laptop counts."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(check_module, "_utc_now", lambda: NOW)
    _record(tmp_path, "nightly", "success", hours_ago=14)

    result = consolidation_last_run(_ctx(tmp_path))

    assert result.passed is True
    assert "nightly: last run success" in result.message


# ---------------------------------------------------------------------------
# 4. The record writer
# ---------------------------------------------------------------------------


def test_record_outcome_keeps_the_gate_clock_and_trims_the_history(store: Path) -> None:
    activity_gate.record_run("nightly", memory_dir=store, now=NOW - timedelta(hours=14))
    for index in range(activity_gate.RUN_HISTORY_LIMIT + 5):
        _record(store, "nightly", "success", hours_ago=1000 - index)

    state = _state(store)
    assert "last_run_at" in state["modes"]["nightly"]
    history = state["runs"]["nightly"]
    assert len(history) == activity_gate.RUN_HISTORY_LIMIT
    # Newest last, and the oldest records are the ones that went.
    assert history[-1]["started_at"] == (NOW - timedelta(hours=1000 - 34)).isoformat().replace(
        "+00:00", "Z"
    )
    assert activity_gate.run_history("nightly", store) == history
    assert activity_gate.run_history("weekly", store) == []


# ---------------------------------------------------------------------------
# 5. The runner writes the record — through real passes, not a stubbed writer
# ---------------------------------------------------------------------------


@pytest.fixture()
def memory_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.consolidation.nightly, "lookback_days", 1)
    (tmp_path / "daily").mkdir()
    return tmp_path


def _partial_pass_store(memory_dir: Path) -> None:
    """One tagged project and one note that names it; with a prose-only LLM
    reply the runner counts the group as failed and returns ``partial``."""
    (memory_dir / "projects").mkdir()
    (memory_dir / "specs" / "prompts").mkdir(parents=True)
    for prompt in ("compaction.md", "nightly-consolidation.md"):
        (memory_dir / "specs" / "prompts" / prompt).write_text(
            "Return consolidation operations as a JSON array.\n", encoding="utf-8"
        )
    (memory_dir / "projects" / "alpha.md").write_text(
        "---\nid: projects-alpha\ncategory: project\n---\n\n# alpha\n\n"
        "- [2026-06-01] Old alpha fact. <!-- fact:a1 -->\n",
        encoding="utf-8",
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    (memory_dir / "daily" / f"{today}-alpha.md").write_text(
        "---\nid: alpha\ncategory: daily\n---\n\nWorked on project/alpha today.\n",
        encoding="utf-8",
    )


def test_a_real_partial_pass_is_recorded_with_its_failed_project(memory_dir: Path, caplog) -> None:
    _partial_pass_store(memory_dir)

    def prose_only(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return "I need more context before I can decide.", "fake-model"

    with caplog.at_level(logging.INFO, logger="palinode.consolidation"):
        result = runner.run_nightly(llm_fn=prose_only)

    assert result["status"] == "partial"
    history = activity_gate.run_history("nightly")
    assert len(history) == 1
    assert history[0]["status"] == "partial"
    assert history[0]["failed_projects"] == ["alpha"]
    assert history[0]["lookback_days"] == 1
    assert history[0]["error"] is None
    # The gate's clock is still untouched by a partial — the outcome record is
    # additional bookkeeping, not a change to the retry contract.
    assert "modes" not in _state(memory_dir)


def test_an_idle_pass_is_recorded_with_its_status(memory_dir: Path) -> None:
    result = runner.run_nightly(lookback_days=2)

    assert result["status"] == "no_new_notes"
    history = activity_gate.run_history("nightly")
    assert [entry["status"] for entry in history] == ["no_new_notes"]
    assert history[0]["lookback_days"] == 2


def test_a_pass_that_raises_is_recorded_as_error_and_still_raises(memory_dir: Path, monkeypatch) -> None:
    def explode(**kwargs):
        raise RuntimeError("LLM unreachable")

    monkeypatch.setattr(runner, "_run_consolidation_unlocked", explode)
    with pytest.raises(RuntimeError, match="LLM unreachable"):
        runner.run_consolidation()

    history = activity_gate.run_history("weekly")
    assert len(history) == 1
    assert history[0]["status"] == "error"
    assert history[0]["error"] == "RuntimeError: LLM unreachable"
    assert history[0]["lookback_days"] == config.consolidation.lookback_days


def test_a_dry_run_records_nothing(memory_dir: Path) -> None:
    runner.run_nightly(dry_run=True)
    runner.run_consolidation(dry_run=True)

    assert not activity_gate.state_path().exists()


def test_the_record_is_stamped_from_the_pass_start(memory_dir: Path, monkeypatch) -> None:
    start = datetime(2026, 9, 15, 11, 0, 2, tzinfo=UTC)
    clock = {"now": start}
    monkeypatch.setattr(activity_gate, "_utc_now", lambda: clock["now"])

    def slow_pass(**kwargs):
        clock["now"] = start + timedelta(seconds=145)
        return {"status": "success", "projects_failed": 0, "projects_compacted": 1}

    monkeypatch.setattr(runner, "_run_nightly_unlocked", slow_pass)
    runner.run_nightly()

    record = activity_gate.run_history("nightly")[-1]
    assert record["started_at"] == "2026-09-15T11:00:02Z"
    assert record["finished_at"] == "2026-09-15T11:02:27Z"
    assert record["status"] == "success"


# ---------------------------------------------------------------------------
# 6. Registration and the CLI surface
# ---------------------------------------------------------------------------


def test_the_check_is_registered_as_fast() -> None:
    names = {fn.__name__: tags for fn, tags in all_checks()}

    assert "fast" in names["consolidation_last_run"]


class TestCliSurface:
    def test_text_output_reports_the_last_run(self, store: Path, monkeypatch) -> None:
        _record(store, "nightly", "partial", hours_ago=14, failed=("palinode",))
        monkeypatch.setattr(doctor_module, "_default_config", _ctx(store).config)

        result = CliRunner().invoke(doctor_cmd, ["--check", "consolidation_last_run"])

        assert "consolidation_last_run" in result.output
        assert "partial" in result.output

    def test_json_output_is_a_list_carrying_the_error(self, store: Path, monkeypatch) -> None:
        for hours in (62, 38, 14):
            _record(store, "nightly", "partial", hours_ago=hours, failed=("palinode",))
        monkeypatch.setattr(doctor_module, "_default_config", _ctx(store).config)

        result = CliRunner().invoke(doctor_cmd, ["--json", "--check", "consolidation_last_run"])

        entries = json.loads(result.stdout)
        assert isinstance(entries, list)
        assert [e["name"] for e in entries] == ["consolidation_last_run"]
        assert entries[0]["severity"] == "error"
        assert entries[0]["passed"] is False
        assert "3 consecutive failures" in entries[0]["message"]
        assert entries[0]["remediation"]
