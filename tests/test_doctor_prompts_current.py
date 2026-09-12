"""Tests for the ``prompts_current`` doctor check.

Real prompt files under ``tmp_path`` on both sides — a fixture "packaged"
directory and a real store tree — so the check exercises the same frontmatter
read it does in production. The packaged side is a fixture rather than the
repo's own ``specs/prompts`` deliberately: the check has to warn about a
lagging store today, before any prompt in this repo is bumped, and a test that
depended on a particular in-tree version would start passing or failing for
reasons that have nothing to do with the check.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.cli.doctor  # noqa: F401 — ensure the submodule is imported
from palinode.cli.doctor import doctor as doctor_cmd

# `palinode.cli.__init__` rebinds the name `doctor` to the click command, so
# `palinode.cli.doctor` is the Command, not the module. Reach the module here.
doctor_module = sys.modules["palinode.cli.doctor"]
from palinode.core.config import Config, config as global_config
from palinode.diagnostics.checks import prompts_current as check_module
from palinode.diagnostics.checks.prompts_current import PACKAGED_PROMPTS_DIR, prompts_current
from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import DoctorContext

_VERSIONED = "---\nname: nightly-consolidation\nversion: {version}\n---\n\nBody.\n"
_UNVERSIONED = "# Compaction Prompt\n\nNo frontmatter at all.\n"


def _packaged(tmp_path: Path, *, version: int = 2, with_unversioned: bool = True) -> Path:
    """A stand-in for the prompts shipped alongside the package."""
    pkg = tmp_path / "packaged"
    pkg.mkdir()
    (pkg / "nightly-consolidation.md").write_text(
        _VERSIONED.format(version=version), encoding="utf-8"
    )
    if with_unversioned:
        (pkg / "compaction.md").write_text(_UNVERSIONED, encoding="utf-8")
    return pkg


def _store(tmp_path: Path, *, version: int | None) -> Path:
    """A memory store whose ``specs/prompts`` holds *version* (None = no file)."""
    store = tmp_path / "store"
    prompts = store / "specs" / "prompts"
    prompts.mkdir(parents=True)
    if version is not None:
        (prompts / "nightly-consolidation.md").write_text(
            _VERSIONED.format(version=version), encoding="utf-8"
        )
    return store


def _ctx(memory_dir: Path) -> DoctorContext:
    cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
    cfg.git.auto_commit = False
    return DoctorContext(config=cfg)


@pytest.fixture()
def use_packaged(monkeypatch):
    """Point the check at a fixture packaged-prompts directory."""
    def _use(path: Path) -> None:
        monkeypatch.setattr(check_module, "PACKAGED_PROMPTS_DIR", path)
    return _use


# ---------------------------------------------------------------------------
# Registration and the real in-tree packaged directory
# ---------------------------------------------------------------------------


def test_registered_as_fast_check() -> None:
    names = {fn.__name__: tags for fn, tags in all_checks()}
    assert "prompts_current" in names
    assert "fast" in names["prompts_current"]


def test_packaged_dir_resolves_to_the_installed_prompts() -> None:
    """The check's "packaged" side must be the prompts inside the install.

    It used to be ``specs/prompts`` in the source tree, reached by path
    arithmetic out of the package — present in a checkout, absent from a wheel,
    so the check reported "nothing to compare against" on the install where a
    lagging store is most likely.
    """
    from palinode.prompts import packaged_prompts_dir

    assert PACKAGED_PROMPTS_DIR == packaged_prompts_dir()
    assert PACKAGED_PROMPTS_DIR.parent.name == "palinode"
    assert (PACKAGED_PROMPTS_DIR / "compaction.md").is_file(), PACKAGED_PROMPTS_DIR
    assert (PACKAGED_PROMPTS_DIR / "nightly-consolidation.md").is_file()


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------


def test_lagging_store_warns_with_file_and_both_versions(tmp_path: Path, use_packaged) -> None:
    use_packaged(_packaged(tmp_path, version=2))
    store = _store(tmp_path, version=1)

    res = prompts_current(_ctx(store))

    assert res.name == "prompts_current"
    assert res.passed is False
    assert res.severity == "warn"
    assert "nightly-consolidation.md" in res.message
    assert "store version 1" in res.message
    assert "packaged version 2" in res.message
    assert "lags" in res.message
    # The remediation names the command that knows how to refresh without
    # discarding a tuned prompt — not "copy these files by hand", which is
    # what it had to say before `prompt sync` existed.
    assert "palinode prompt sync" in (res.remediation or "")


def test_current_store_passes(tmp_path: Path, use_packaged) -> None:
    use_packaged(_packaged(tmp_path, version=2))
    store = _store(tmp_path, version=2)

    res = prompts_current(_ctx(store))

    assert res.passed is True
    assert res.severity == "info"
    assert "match the packaged copies" in res.message
    # The unversioned packaged prompt is reported as uncovered, not as current.
    assert "declare no version" in res.message


def test_prompt_missing_from_store_warns(tmp_path: Path, use_packaged) -> None:
    use_packaged(_packaged(tmp_path, version=2))
    store = _store(tmp_path, version=None)

    res = prompts_current(_ctx(store))

    assert res.passed is False
    assert res.severity == "warn"
    assert "nightly-consolidation.md" in res.message
    assert "absent from the store" in res.message
    assert "packaged version 2" in res.message


def test_store_without_prompts_dir_is_info_and_names_the_path(
    tmp_path: Path, use_packaged
) -> None:
    use_packaged(_packaged(tmp_path, version=2))
    bare = tmp_path / "bare-store"
    bare.mkdir()

    res = prompts_current(_ctx(bare))

    assert res.passed is True
    assert res.severity == "info"
    assert str(bare / "specs" / "prompts") in res.message


def test_no_packaged_prompts_is_info(tmp_path: Path, use_packaged) -> None:
    absent = tmp_path / "not-installed"
    use_packaged(absent)

    res = prompts_current(_ctx(_store(tmp_path, version=1)))

    assert res.passed is True
    assert res.severity == "info"
    assert str(absent) in res.message


def test_no_packaged_prompt_declares_a_version_is_info(tmp_path: Path, use_packaged) -> None:
    pkg = tmp_path / "packaged"
    pkg.mkdir()
    (pkg / "compaction.md").write_text(_UNVERSIONED, encoding="utf-8")
    use_packaged(pkg)

    res = prompts_current(_ctx(_store(tmp_path, version=1)))

    assert res.passed is True
    assert res.severity == "info"
    assert "declares a version" in res.message


def test_store_ahead_of_package_reports_drift_not_a_lag(tmp_path: Path, use_packaged) -> None:
    use_packaged(_packaged(tmp_path, version=2))
    store = _store(tmp_path, version=3)

    res = prompts_current(_ctx(store))

    assert res.passed is False
    assert "differs from packaged version 2" in res.message
    assert "lags" not in res.message


# ---------------------------------------------------------------------------
# Surfaces: CLI (text + --json) and GET /doctor
# ---------------------------------------------------------------------------


class TestCliSurface:
    def test_text_output_reports_the_check(self, tmp_path: Path, monkeypatch, use_packaged) -> None:
        use_packaged(_packaged(tmp_path, version=2))
        store = _store(tmp_path, version=1)
        monkeypatch.setattr(doctor_module, "_default_config", _ctx(store).config)

        result = CliRunner().invoke(doctor_cmd, ["--check", "prompts_current"])

        assert "prompts_current" in result.output
        assert "nightly-consolidation.md" in result.output

    def test_json_output_carries_the_warn(self, tmp_path: Path, monkeypatch, use_packaged) -> None:
        use_packaged(_packaged(tmp_path, version=2))
        store = _store(tmp_path, version=1)
        monkeypatch.setattr(doctor_module, "_default_config", _ctx(store).config)

        result = CliRunner().invoke(doctor_cmd, ["--json", "--check", "prompts_current"])

        entries = json.loads(result.stdout)
        assert [e["name"] for e in entries] == ["prompts_current"]
        assert entries[0]["severity"] == "warn"
        assert entries[0]["passed"] is False
        assert "nightly-consolidation.md" in entries[0]["message"]


class TestDoctorRoute:
    def test_fast_run_includes_the_check(self, tmp_path: Path, monkeypatch, use_packaged) -> None:
        use_packaged(_packaged(tmp_path, version=2))
        store = _store(tmp_path, version=1)
        monkeypatch.setattr(global_config, "memory_dir", str(store))
        monkeypatch.setattr(global_config, "db_path", str(store / ".palinode.db"))
        monkeypatch.setattr(global_config.git, "auto_commit", False)
        monkeypatch.setattr(global_config.doctor, "search_roots", [str(tmp_path)])
        # The store carries prompt .md files and no DB yet; without this the
        # API refuses to start on the "memory files but no database" guard.
        monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")

        from palinode.api.server import app

        with TestClient(app, raise_server_exceptions=True) as client:
            body = client.get("/doctor", params={"fast": True}).json()

        entry = next(r for r in body["results"] if r["name"] == "prompts_current")
        assert entry["severity"] == "warn"
        assert entry["passed"] is False
        assert "nightly-consolidation.md" in entry["message"]
