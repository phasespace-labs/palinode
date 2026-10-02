"""Tests for the ``consolidation_schedule_effective`` doctor check.

Real cron files under ``tmp_path``, read with the same code that reads
``/etc/cron.d`` in production — the check's two module-level path constants are
repointed at a temp tree and nothing else about it is stubbed. The one thing
that cannot be a file is ``crontab -l``, so that shells out through a single
seam (``read_crontab``) which each test replaces with a canned answer.

The platform gate is exercised both ways: every Linux test pins
``sys.platform`` so the suite behaves identically on the macOS laptops this is
developed on and the Linux hosts it runs on.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import palinode.cli.doctor  # noqa: F401 — ensure the submodule is imported
from palinode.cli.doctor import doctor as doctor_cmd

# `palinode.cli.__init__` rebinds the name `doctor` to the click command, so
# `palinode.cli.doctor` is the Command, not the module. Reach the module here.
doctor_module = sys.modules["palinode.cli.doctor"]
from palinode.core.config import Config
from palinode.diagnostics.checks import consolidation_schedule as check_module
from palinode.diagnostics.checks.consolidation_schedule import (
    consolidation_schedule_effective,
    parse_cron_line,
)
from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import DoctorContext

_NIGHTLY = (
    "0 11 * * * root cd /opt/palinode && venv/bin/python -m "
    "palinode.consolidation.cron --nightly --days {days} >> /var/log/c.log 2>&1"
)
_WEEKLY = (
    "0 11 * * 0 root cd /opt/palinode && venv/bin/python -m "
    "palinode.consolidation.cron --days {days} >> /var/log/c.log 2>&1"
)


def _ctx(*, nightly: int = 7, weekly: int = 7, max_hours_elapsed: float = 168) -> DoctorContext:
    """Nightly defaults to 7 — the shipped default, and exactly the gate's
    ``max_hours_elapsed=168`` ceiling in days — so a test not about the ceiling
    check does not trip it incidentally. Tests of that check override one or
    both explicitly.
    """
    cfg = Config()
    cfg.consolidation.lookback_days = weekly
    cfg.consolidation.nightly.lookback_days = nightly
    cfg.consolidation.auto_gate.max_hours_elapsed = max_hours_elapsed
    return DoctorContext(config=cfg)


@pytest.fixture
def cron(tmp_path: Path, monkeypatch) -> Path:
    """A temp ``/etc/cron.d`` the check reads instead of the real one.

    Also pins the platform to Linux, points ``/etc/crontab`` at a path that does
    not exist, and answers ``crontab -l`` with "this user has no crontab" — so
    each test starts from exactly one readable source and adds to it.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    cron_d = tmp_path / "cron.d"
    cron_d.mkdir()
    monkeypatch.setattr(check_module, "CRON_D_DIR", cron_d)
    monkeypatch.setattr(check_module, "SYSTEM_CRONTAB", tmp_path / "no-such-crontab")
    monkeypatch.setattr(
        check_module,
        "read_crontab",
        lambda user=None: (None, f"no crontab for {user or 'tester'}", True),
    )
    return cron_d


def _write(cron_d: Path, *lines: str, name: str = "palinode") -> Path:
    path = cron_d / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------


class TestParsing:
    def test_a_cron_d_line_yields_schedule_mode_and_days(self) -> None:
        entry = parse_cron_line(_NIGHTLY.format(days=3), source="x:1")

        assert entry is not None
        assert entry.mode == "nightly"
        assert entry.schedule == "0 11 * * *"
        assert entry.days == 3
        assert entry.days_problem is None
        assert entry.schedule_problem is None

    def test_a_line_without_nightly_is_the_weekly_pass(self) -> None:
        entry = parse_cron_line(_WEEKLY.format(days=3), source="x:1")

        assert entry is not None
        assert entry.mode == "weekly"
        assert entry.schedule == "0 11 * * 0"

    def test_a_user_crontab_line_has_no_user_column_and_still_parses(self) -> None:
        line = "17 * * * * python -m palinode.consolidation.cron --nightly --days 2"

        entry = parse_cron_line(line, source="x:1")

        assert entry is not None
        assert entry.schedule == "17 * * * *"
        assert entry.days == 2

    def test_an_at_nickname_is_the_whole_schedule(self) -> None:
        line = "@daily root python -m palinode.consolidation.cron --nightly"

        entry = parse_cron_line(line, source="x:1")

        assert entry is not None
        assert entry.schedule == "@daily"
        assert entry.days is None

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "# 0 11 * * * root python -m palinode.consolidation.cron --days 3",
            "0 11 * * * root python -m palinode.indexer.watcher",
            "PALINODE_CRON=python -m palinode.consolidation.cron --days 3",
        ],
        ids=["blank", "whitespace", "commented", "other-job", "env-assignment"],
    )
    def test_lines_that_are_not_a_consolidation_job_are_skipped(self, line: str) -> None:
        assert parse_cron_line(line, source="x:1") is None


# ---------------------------------------------------------------------------
# 1. Match — the result that earns the check its keep
# ---------------------------------------------------------------------------


def test_cron_matching_config_passes_and_still_prints_the_effective_value(cron: Path) -> None:
    _write(cron, _NIGHTLY.format(days=7), _WEEKLY.format(days=7))

    result = consolidation_schedule_effective(_ctx(nightly=7, weekly=7))

    assert result.passed is True
    assert result.severity == "info"
    assert "nightly" in result.message and "--days 7" in result.message
    assert "weekly" in result.message and "--days 7" in result.message
    assert "effective catch-up bound 7 day(s)" in result.message
    assert "effective lookback 7 day(s)" in result.message
    assert "0 11 * * *" in result.message
    assert "0 11 * * 0" in result.message
    assert result.remediation is None


def test_a_cron_line_with_no_days_hands_the_decision_back_to_the_config(cron: Path) -> None:
    """The state the fix converges on: one source of truth, reported as such."""
    _write(
        cron,
        "0 11 * * * root python -m palinode.consolidation.cron --nightly",
        "0 11 * * 0 root python -m palinode.consolidation.cron",
    )

    result = consolidation_schedule_effective(_ctx(nightly=7, weekly=7))

    assert result.passed is True
    assert "passes no --days" in result.message
    assert "consolidation.nightly.lookback_days=7 governs" in result.message
    assert "consolidation.lookback_days=7 governs" in result.message


# ---------------------------------------------------------------------------
# 2. Mismatch, in both directions
# ---------------------------------------------------------------------------


def test_cron_wider_than_config_warns_and_names_the_winner(cron: Path) -> None:
    """The dogfood host's nightly: config says 1, cron runs 3."""
    _write(cron, _NIGHTLY.format(days=3))

    result = consolidation_schedule_effective(_ctx(nightly=1, weekly=7))

    assert result.passed is False
    assert result.severity == "warn"
    assert "runs --days 3" in result.message
    assert "consolidation.nightly.lookback_days=1" in result.message
    assert "the cron argument wins" in result.message
    assert "effective catch-up bound is 3 day(s), not 1" in result.message
    assert result.remediation


def test_cron_narrower_than_config_warns_the_same_way(cron: Path) -> None:
    """The dogfood host's weekly: a declared 7-day window that reads 3 days."""
    _write(cron, _WEEKLY.format(days=3))

    result = consolidation_schedule_effective(_ctx(nightly=1, weekly=7))

    assert result.passed is False
    assert "weekly" in result.message
    assert "runs --days 3" in result.message
    assert "consolidation.lookback_days=7" in result.message
    assert "effective lookback is 3 day(s), not 7" in result.message


def test_a_pass_with_no_cron_entry_is_named_without_failing(cron: Path) -> None:
    _write(cron, _NIGHTLY.format(days=7))

    result = consolidation_schedule_effective(_ctx(nightly=7, weekly=7))

    assert result.passed is True
    assert "weekly: no cron entry" in result.message


# ---------------------------------------------------------------------------
# 2b. Catch-up bound vs the gate's ceiling
# ---------------------------------------------------------------------------


def test_a_bound_below_the_gate_ceiling_warns_even_when_cron_matches_config(
    cron: Path,
) -> None:
    """No cron/config mismatch at all — the ceiling problem is independent."""
    _write(cron, _NIGHTLY.format(days=3), _WEEKLY.format(days=7))

    result = consolidation_schedule_effective(
        _ctx(nightly=3, weekly=7, max_hours_elapsed=168)
    )

    assert result.passed is False
    assert result.severity == "warn"
    assert "matching consolidation.nightly.lookback_days=3" in result.message
    assert "3-day catch-up bound is tighter than" in result.message
    assert "7-day (168 h) ceiling" in result.message
    assert "consolidation.auto_gate.max_hours_elapsed" in result.message
    assert "drops the notes it chose to wait on" in result.message
    assert result.remediation
    assert "max_hours_elapsed" in result.remediation


def test_a_bound_matching_the_gate_ceiling_does_not_warn(cron: Path) -> None:
    _write(cron, _NIGHTLY.format(days=7), _WEEKLY.format(days=7))

    result = consolidation_schedule_effective(
        _ctx(nightly=7, weekly=7, max_hours_elapsed=168)
    )

    assert result.passed is True
    assert "tighter than" not in result.message


def test_the_ceiling_check_reads_the_configured_gate_not_a_hardcoded_week(
    cron: Path,
) -> None:
    """A 48 h gate makes a 2-day bound the ceiling — not the shipped 168 h/7 d."""
    _write(cron, _NIGHTLY.format(days=2), _WEEKLY.format(days=7))

    passes = consolidation_schedule_effective(
        _ctx(nightly=2, weekly=7, max_hours_elapsed=48)
    )

    assert passes.passed is True


def test_the_ceiling_check_warns_below_a_non_default_gate(cron: Path) -> None:
    """Same 2-day bound as above, but the gate is a stricter 48 h: now a warn."""
    _write(cron, _NIGHTLY.format(days=1), _WEEKLY.format(days=7))

    warns = consolidation_schedule_effective(
        _ctx(nightly=1, weekly=7, max_hours_elapsed=48)
    )

    assert warns.passed is False
    assert "2-day (48 h) ceiling" in warns.message


def test_no_cron_entry_at_all_still_flags_a_bound_below_the_ceiling(cron: Path) -> None:
    """The smoke-observed case: no palinode cron, a tight nightly bound in config."""
    _write(cron, "0 * * * * root /usr/bin/logrotate", name="logrotate")

    result = consolidation_schedule_effective(
        _ctx(nightly=3, weekly=7, max_hours_elapsed=168)
    )

    assert result.passed is False
    assert result.severity == "warn"
    assert "No consolidation cron entry found" in result.message
    assert "3-day catch-up bound is tighter than" in result.message
    assert result.remediation


def test_no_cron_entry_at_all_with_a_compliant_bound_stays_not_applicable(
    cron: Path,
) -> None:
    _write(cron, "0 * * * * root /usr/bin/logrotate", name="logrotate")

    result = consolidation_schedule_effective(_ctx(nightly=7, weekly=7))

    assert result.passed is True
    assert result.severity == "info"
    assert "tighter than" not in result.message


# ---------------------------------------------------------------------------
# 3. Missing cron — not applicable, never a failure
# ---------------------------------------------------------------------------


def test_no_consolidation_entry_anywhere_is_not_applicable(cron: Path) -> None:
    _write(cron, "0 * * * * root /usr/bin/logrotate", name="logrotate")

    result = consolidation_schedule_effective(_ctx())

    assert result.passed is True
    assert result.severity == "info"
    assert "No consolidation cron entry found" in result.message
    assert str(cron) in result.message
    assert "consolidation.nightly.lookback_days=7" in result.message
    assert "consolidation.lookback_days=7" in result.message


def test_no_cron_sources_at_all_is_not_applicable_and_says_where_it_looked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(check_module, "CRON_D_DIR", tmp_path / "no-cron-d")
    monkeypatch.setattr(check_module, "SYSTEM_CRONTAB", tmp_path / "no-crontab")
    monkeypatch.setattr(
        check_module,
        "read_crontab",
        lambda user=None: (None, "the `crontab` command is not installed", True),
    )

    result = consolidation_schedule_effective(_ctx())

    assert result.passed is True
    assert result.severity == "info"
    assert "does not exist" in result.message
    assert "the `crontab` command is not installed" in result.message


def test_a_backup_file_cron_itself_ignores_is_not_read_as_live(cron: Path) -> None:
    """A dot in the name makes cron skip the file; reporting it would be a lie."""
    _write(cron, _NIGHTLY.format(days=1), name="palinode.bak-20260531")

    result = consolidation_schedule_effective(_ctx(nightly=7))

    assert result.passed is True
    assert "No consolidation cron entry found" in result.message
    assert "palinode.bak-20260531" in result.message
    assert "run-parts" in result.message


# ---------------------------------------------------------------------------
# 4. Unreadable cron — reported, never a failure
# ---------------------------------------------------------------------------


def test_a_crontab_it_may_not_read_is_named_not_assumed_empty(cron: Path, monkeypatch) -> None:
    _write(cron, _NIGHTLY.format(days=7), _WEEKLY.format(days=7))
    monkeypatch.setattr(
        check_module,
        "read_crontab",
        lambda user=None: (None, "crontab: must be privileged to use -u", False),
    )

    result = consolidation_schedule_effective(_ctx(nightly=7, weekly=7))

    assert result.passed is True
    assert "Not read:" in result.message
    assert "must be privileged" in result.message


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read a mode-000 file, so there is nothing to degrade from",
)
def test_a_cron_d_file_it_cannot_open_is_named_not_skipped(cron: Path) -> None:
    _write(cron, _NIGHTLY.format(days=7))
    secret = _write(cron, _WEEKLY.format(days=3), name="palinode-weekly")
    secret.chmod(0o000)

    try:
        result = consolidation_schedule_effective(_ctx(nightly=7, weekly=7))
    finally:
        secret.chmod(0o644)

    assert result.passed is True
    assert "Not read:" in result.message
    assert "palinode-weekly" in result.message
    # The readable half is still reported — a blind spot is not a blackout.
    assert "effective catch-up bound 7 day(s)" in result.message


def test_an_unlistable_cron_d_is_not_applicable(tmp_path: Path, monkeypatch) -> None:
    """``/etc/cron.d`` present but not listable: say so, do not guess."""
    monkeypatch.setattr(sys, "platform", "linux")
    cron_d = tmp_path / "cron.d"
    cron_d.mkdir()
    monkeypatch.setattr(check_module, "CRON_D_DIR", cron_d)
    monkeypatch.setattr(check_module, "SYSTEM_CRONTAB", tmp_path / "no-crontab")
    monkeypatch.setattr(
        check_module, "read_crontab", lambda user=None: (None, "no crontab for tester", True)
    )

    def _boom(self):  # noqa: ANN001 — Path.iterdir signature
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", _boom)

    result = consolidation_schedule_effective(_ctx())

    assert result.passed is True
    assert result.severity == "info"
    assert "could not be listed" in result.message


# ---------------------------------------------------------------------------
# 5. Lines it cannot parse — said plainly, never reported as a match
# ---------------------------------------------------------------------------


def test_a_line_whose_schedule_is_not_a_schedule_is_flagged(cron: Path) -> None:
    _write(
        cron,
        "every-night at-eleven o-clock please root python -m "
        "palinode.consolidation.cron --nightly --days 1",
    )

    result = consolidation_schedule_effective(_ctx(nightly=1))

    assert result.passed is False
    assert result.severity == "warn"
    assert "not a readable cron schedule" in result.message
    assert "cron will not run this line at all" in result.message


def test_a_non_integer_days_is_flagged_and_the_config_value_reported(cron: Path) -> None:
    _write(cron, _NIGHTLY.format(days="three"))

    result = consolidation_schedule_effective(_ctx(nightly=1))

    assert result.passed is False
    assert "`--days three` is not an integer" in result.message
    assert "consolidation.nightly.lookback_days=1 governs" in result.message


def test_the_attached_days_form_the_entry_point_cannot_read_is_flagged(cron: Path) -> None:
    _write(cron, "0 11 * * * root python -m palinode.consolidation.cron --nightly --days=3")

    result = consolidation_schedule_effective(_ctx(nightly=1))

    assert result.passed is False
    assert "attached form" in result.message
    assert "effective catch-up bound 1 day(s)" in result.message


def test_days_as_the_last_token_is_flagged(cron: Path) -> None:
    _write(cron, "0 11 * * * root python -m palinode.consolidation.cron --nightly --days")

    result = consolidation_schedule_effective(_ctx(nightly=1))

    assert result.passed is False
    assert "no value after it" in result.message


def test_two_entries_for_one_pass_are_reported_as_ambiguous_not_guessed(cron: Path) -> None:
    _write(cron, _NIGHTLY.format(days=1))
    _write(cron, _NIGHTLY.format(days=3), name="palinode-extra")

    result = consolidation_schedule_effective(_ctx(nightly=1))

    assert result.passed is False
    assert "2 entries invoke the pass" in result.message
    assert "no single effective catch-up bound" in result.message
    assert "--days 1" in result.message and "--days 3" in result.message


# ---------------------------------------------------------------------------
# 6. Not Linux
# ---------------------------------------------------------------------------


def test_macos_is_not_applicable_with_the_reason(tmp_path: Path, monkeypatch) -> None:
    """A check that fails on every laptop is a check nobody reads on the host."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def _never(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("the platform gate must return before touching cron")

    monkeypatch.setattr(check_module, "read_crontab", _never)
    monkeypatch.setattr(check_module, "CRON_D_DIR", tmp_path / "unused")

    result = consolidation_schedule_effective(_ctx())

    assert result.passed is True
    assert result.severity == "info"
    assert "darwin" in result.message
    assert "cron" in result.message


# ---------------------------------------------------------------------------
# Registration and surfaces
# ---------------------------------------------------------------------------


def test_the_check_is_registered_as_deep() -> None:
    names = {fn.__name__: tags for fn, tags in all_checks()}

    assert "deep" in names["consolidation_schedule_effective"]


class TestCliSurface:
    def test_text_output_reports_the_effective_value(self, cron: Path, monkeypatch) -> None:
        _write(cron, _NIGHTLY.format(days=3))
        monkeypatch.setattr(doctor_module, "_default_config", _ctx(nightly=1).config)

        result = CliRunner().invoke(
            doctor_cmd, ["--check", "consolidation_schedule_effective"]
        )

        assert "consolidation_schedule_effective" in result.output
        assert "3 day(s)" in result.output

    def test_json_output_is_a_list_carrying_the_warn(self, cron: Path, monkeypatch) -> None:
        _write(cron, _NIGHTLY.format(days=3))
        monkeypatch.setattr(doctor_module, "_default_config", _ctx(nightly=1).config)

        result = CliRunner().invoke(
            doctor_cmd, ["--json", "--check", "consolidation_schedule_effective"]
        )

        entries = json.loads(result.stdout)
        assert isinstance(entries, list)
        assert [e["name"] for e in entries] == ["consolidation_schedule_effective"]
        assert entries[0]["severity"] == "warn"
        assert entries[0]["passed"] is False
        assert entries[0]["remediation"]
