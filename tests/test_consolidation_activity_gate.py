"""The activity gate: automatic consolidation fires on use, not on the calendar.

Everything here runs against real files under ``tmp_path`` — the gate's inputs
are a JSON state file and the ``## Session End —`` headings in ``daily/*.md``,
so a fixture that fakes either would test nothing that ships.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

import palinode.consolidation.activity_gate as activity_gate
import palinode.consolidation.cron as cron
import palinode.consolidation.runner as runner
from palinode.cli.consolidate import consolidate as consolidate_cmd
from palinode.core.config import AutoGateConfig, Config, config


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.consolidation.auto_gate, "enabled", True)
    monkeypatch.setattr(config.consolidation.auto_gate, "min_hours_elapsed", 24.0)
    monkeypatch.setattr(config.consolidation.auto_gate, "min_sessions", 5)
    monkeypatch.setattr(config.consolidation.auto_gate, "max_hours_elapsed", 168.0)
    (tmp_path / "daily").mkdir()
    return tmp_path


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _record_last_run(memory_dir: Path, when: datetime, mode: str = "weekly") -> None:
    """Write the state file directly — the gate must read what a prior process
    wrote, not something this test handed it in memory."""
    path = memory_dir / ".palinode" / "consolidation-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state.setdefault("modes", {})[mode] = {"last_run_at": _iso(when), "sessions_at_run": 0}
    path.write_text(json.dumps(state), encoding="utf-8")


def _write_sessions(memory_dir: Path, moments: list[datetime]) -> None:
    """Append session-end entries in the exact shape ``POST /session-end`` writes."""
    by_day: dict[str, list[str]] = {}
    for moment in moments:
        day = moment.strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(
            f"## Session End — {_iso(moment)}\n\n**Summary:** work happened\n"
        )
    for day, entries in by_day.items():
        note = memory_dir / "daily" / f"{day}.md"
        note.write_text(
            f"---\nid: daily-{day}\ncategory: daily\n---\n\n" + "\n".join(entries),
            encoding="utf-8",
        )


# ── The dual gate ────────────────────────────────────────────────────────────


def test_defers_when_sessions_are_short(memory_dir):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=48))
    _write_sessions(memory_dir, [now - timedelta(hours=n) for n in (1, 2, 3)])

    decision = activity_gate.evaluate("weekly", now=now)

    assert decision.should_run is False
    assert decision.sessions_since_last_run == 3
    assert decision.reason == "deferred: 3 sessions / 5, 48 h / 24 h"


def test_defers_when_elapsed_time_is_short(memory_dir):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=11))
    _write_sessions(memory_dir, [now - timedelta(minutes=n) for n in (10, 20, 30, 40, 50, 60)])

    decision = activity_gate.evaluate("weekly", now=now)

    assert decision.should_run is False
    assert decision.sessions_since_last_run == 6
    assert decision.reason == "deferred: 6 sessions / 5, 11 h / 24 h"


def test_runs_when_both_conditions_are_met(memory_dir):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=30))
    _write_sessions(memory_dir, [now - timedelta(hours=n) for n in (1, 2, 3, 4, 5)])

    decision = activity_gate.evaluate("weekly", now=now)

    assert decision.should_run is True
    assert decision.reason == "5 sessions / 5, 30 h / 24 h"


def test_ceiling_fires_with_no_sessions_at_all(memory_dir):
    """A watcher-only store records no sessions. Without the ceiling the dual
    gate would turn "no sessions" into "never consolidate"."""
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=169))

    decision = activity_gate.evaluate("weekly", now=now)

    assert decision.should_run is True
    assert decision.sessions_since_last_run == 0
    assert "ceiling reached" in decision.reason


def test_no_recorded_run_bootstraps_a_pass(memory_dir):
    decision = activity_gate.evaluate("weekly")

    assert decision.should_run is True
    assert decision.hours_since_last_run is None
    assert decision.reason == "no previous run recorded"


def test_disabled_gate_always_runs(memory_dir, monkeypatch):
    monkeypatch.setattr(config.consolidation.auto_gate, "enabled", False)
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(minutes=1))

    decision = activity_gate.evaluate("weekly", now=now)

    assert decision.should_run is True
    assert decision.reason == "gate disabled"


def test_sessions_before_the_last_run_do_not_count(memory_dir):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=30))
    _write_sessions(
        memory_dir,
        [now - timedelta(hours=n) for n in (40, 35, 31)]  # before the last run
        + [now - timedelta(hours=n) for n in (2, 1)],     # after it
    )

    decision = activity_gate.evaluate("weekly", now=now)

    assert decision.sessions_since_last_run == 2
    assert decision.should_run is False


def test_weekly_and_nightly_clocks_are_independent(memory_dir):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=1), mode="nightly")
    _record_last_run(memory_dir, now - timedelta(hours=200), mode="weekly")

    assert activity_gate.evaluate("nightly", now=now).should_run is False
    assert activity_gate.evaluate("weekly", now=now).should_run is True


def test_unparseable_state_file_falls_back_to_running(memory_dir):
    path = activity_gate.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert activity_gate.evaluate("weekly").should_run is True


# ── Recording a run ──────────────────────────────────────────────────────────


def test_successful_run_resets_the_counters(memory_dir):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=30))
    _write_sessions(memory_dir, [now - timedelta(hours=n) for n in (1, 2, 3, 4, 5)])
    assert activity_gate.evaluate("weekly", now=now).should_run is True

    runner.run_consolidation()

    after = activity_gate.evaluate("weekly")
    assert after.should_run is False
    assert after.sessions_since_last_run == 0
    assert after.last_run_at is not None


def test_failed_run_does_not_reset_the_counters(memory_dir, monkeypatch):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=30))
    _write_sessions(memory_dir, [now - timedelta(hours=n) for n in (1, 2, 3, 4, 5)])
    before = activity_gate.evaluate("weekly", now=now)

    def explode(**kwargs):
        raise RuntimeError("LLM unreachable")

    monkeypatch.setattr(runner, "_run_consolidation_unlocked", explode)
    with pytest.raises(RuntimeError, match="LLM unreachable"):
        runner.run_consolidation()

    after = activity_gate.evaluate("weekly", now=now)
    assert after.last_run_at == before.last_run_at
    assert after.sessions_since_last_run == 5


def test_dry_run_does_not_reset_the_counters(memory_dir):
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=30))
    before = activity_gate.evaluate("weekly", now=now)

    runner.run_consolidation(dry_run=True)

    assert activity_gate.evaluate("weekly", now=now).last_run_at == before.last_run_at


def test_nightly_run_records_only_the_nightly_clock(memory_dir):
    runner.run_nightly()

    state = json.loads(activity_gate.state_path().read_text(encoding="utf-8"))
    assert set(state["modes"]) == {"nightly"}


# ── The cron entry point ─────────────────────────────────────────────────────


def _stub_runs(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(cron, "run_consolidation", lambda **kw: calls.append("weekly") or {})
    monkeypatch.setattr(cron, "run_nightly", lambda **kw: calls.append("nightly") or {})
    return calls


def test_cron_exits_quietly_when_the_gate_defers(memory_dir, monkeypatch, caplog):
    calls = _stub_runs(monkeypatch)
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=11))
    _write_sessions(memory_dir, [now - timedelta(minutes=n) for n in (10, 20, 30)])
    monkeypatch.setattr(sys, "argv", ["palinode.consolidation.cron"])

    with caplog.at_level(logging.INFO, logger="palinode.consolidation.cron"):
        with pytest.raises(SystemExit) as raised:
            cron.main()

    assert raised.value.code == 0
    assert calls == []
    assert "deferred: 3 sessions / 5, 11 h / 24 h" in caplog.text


def test_cron_runs_when_the_gate_is_met(memory_dir, monkeypatch):
    calls = _stub_runs(monkeypatch)
    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=30))
    _write_sessions(memory_dir, [now - timedelta(hours=n) for n in (1, 2, 3, 4, 5)])
    monkeypatch.setattr(sys, "argv", ["palinode.consolidation.cron"])

    cron.main()

    assert calls == ["weekly"]


def test_cron_ignore_gate_forces_the_pass(memory_dir, monkeypatch):
    calls = _stub_runs(monkeypatch)
    _record_last_run(memory_dir, datetime.now(UTC) - timedelta(minutes=1))
    monkeypatch.setattr(sys, "argv", ["palinode.consolidation.cron", "--ignore-gate"])

    cron.main()

    assert calls == ["weekly"]


# ── On-demand surfaces ───────────────────────────────────────────────────────


def test_on_demand_consolidate_bypasses_the_gate(memory_dir, monkeypatch):
    """The API's default: an operator who asked has already decided."""
    from palinode.api.routers.consolidation import ConsolidateRequest, consolidate_api

    _record_last_run(memory_dir, datetime.now(UTC) - timedelta(minutes=1))
    calls: list[bool] = []
    monkeypatch.setattr(
        runner, "_run_consolidation_unlocked",
        lambda **kw: calls.append(True) or {"status": "success"},
    )

    result = consolidate_api(ConsolidateRequest())

    assert calls == [True]
    assert result["status"] == "success"


def test_respect_gate_defers_instead_of_running(memory_dir, monkeypatch):
    from palinode.api.routers.consolidation import ConsolidateRequest, consolidate_api

    now = datetime.now(UTC)
    _record_last_run(memory_dir, now - timedelta(hours=11))
    calls: list[bool] = []
    monkeypatch.setattr(
        runner, "_run_consolidation_unlocked",
        lambda **kw: calls.append(True) or {"status": "success"},
    )

    result = consolidate_api(ConsolidateRequest(respect_gate=True))

    assert calls == []
    assert result["status"] == "deferred"
    assert result["gate"]["min_sessions"] == 5
    assert result["gate"]["should_run"] is False


def test_cli_reports_a_deferral_in_text_mode(memory_dir, monkeypatch):
    # `palinode.cli` rebinds the name `consolidate` to the click command, so
    # the module has to be reached through importlib, not attribute access.
    consolidate_module = importlib.import_module("palinode.cli.consolidate")

    monkeypatch.setattr(
        consolidate_module.api_client,
        "consolidate",
        lambda **kwargs: {
            "status": "deferred",
            "gate": {"reason": "deferred: 1 sessions / 5, 2 h / 24 h"},
        },
    )

    result = CliRunner().invoke(consolidate_cmd, ["--respect-gate", "--format", "text"])

    assert result.exit_code == 0
    assert "1 sessions / 5" in result.output


# ── Config ───────────────────────────────────────────────────────────────────


def test_gate_defaults_load():
    gate = Config().consolidation.auto_gate

    assert gate == AutoGateConfig()
    assert gate.enabled is True
    assert (gate.min_hours_elapsed, gate.min_sessions, gate.max_hours_elapsed) == (24, 5, 168)


def test_status_reports_both_modes(memory_dir):
    reported = activity_gate.status()

    assert reported["enabled"] is True
    assert set(reported["modes"]) == {"weekly", "nightly"}
    assert reported["modes"]["weekly"]["should_run"] is True
