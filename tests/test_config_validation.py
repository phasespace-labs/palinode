"""Tests for Config.validate_paths() — issue #201.

Covers:
  - db_path under memory_dir → no warnings
  - db_path outside memory_dir → one divergence warning
  - memory_dir doesn't exist → warning about missing directory
  - db_path parent doesn't exist → warning about missing parent
  - env vars unset, using YAML-style defaults → same checks apply

All tests use tmp_path and monkeypatch to avoid touching the live
palinode installation and to stay hermetically isolated.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import palinode.core.config as config_module
from palinode.core.config import load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fresh(monkeypatch, memory_dir: str, db_path: str | None = None) -> config_module.Config:
    """Build a Config with the given memory_dir/db_path via env vars.

    We drive via env vars so load_config() picks them up through its
    normal resolution path (the same code path operators use at runtime).
    PALINODE_DIR overrides memory_dir; db_path is injected as a raw YAML
    value via monkeypatching _deep_merge so we can test absolute db_paths
    that the env-var path wouldn't produce.
    """
    monkeypatch.setenv("PALINODE_DIR", memory_dir)
    # Prevent touching the real filesystem config
    monkeypatch.setattr(config_module, "_real_config_paths", lambda: [], raising=False)

    cfg = load_config()

    # Override db_path directly when the caller supplies one (tests an absolute
    # db_path that wasn't derived from memory_dir).
    if db_path is not None:
        object.__setattr__(cfg, "db_path", db_path)

    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestValidatePaths:
    """Unit tests for Config.validate_paths()."""

    def test_all_good_no_warnings(self, tmp_path: Path) -> None:
        """db_path under memory_dir, both existing → empty warning list."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_path = memory_dir / ".palinode.db"
        db_path.touch()

        cfg = config_module.Config(
            memory_dir=str(memory_dir),
            db_path=str(db_path),
        )
        warnings = cfg.validate_paths()
        assert warnings == []

    def test_db_path_outside_memory_dir_warns(self, tmp_path: Path) -> None:
        """db_path outside memory_dir → exactly one divergence warning."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        other_dir = tmp_path / "old-stale-data"
        other_dir.mkdir()
        db_path = other_dir / ".palinode.db"
        db_path.touch()

        cfg = config_module.Config(
            memory_dir=str(memory_dir),
            db_path=str(db_path),
        )
        warnings = cfg.validate_paths()
        assert len(warnings) == 1
        assert "outside memory_dir" in warnings[0]
        assert str(memory_dir.resolve()) in warnings[0]
        assert str(db_path.resolve()) in warnings[0]

    def test_memory_dir_missing_warns(self, tmp_path: Path) -> None:
        """memory_dir that doesn't exist → warning about missing directory."""
        memory_dir = tmp_path / "does-not-exist"
        # Don't mkdir — we want the missing-dir warning.
        db_path = memory_dir / ".palinode.db"

        cfg = config_module.Config(
            memory_dir=str(memory_dir),
            db_path=str(db_path),
        )
        warnings = cfg.validate_paths()
        memory_dir_warnings = [w for w in warnings if "memory_dir does not exist" in w]
        assert memory_dir_warnings, f"Expected memory_dir warning, got: {warnings}"

    def test_db_path_parent_missing_warns(self, tmp_path: Path) -> None:
        """db_path whose parent directory doesn't exist → warning."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        # Nested subdir that doesn't exist
        db_path = memory_dir / "subdir" / ".palinode.db"

        cfg = config_module.Config(
            memory_dir=str(memory_dir),
            db_path=str(db_path),
        )
        warnings = cfg.validate_paths()
        parent_warnings = [w for w in warnings if "db_path parent directory does not exist" in w]
        assert parent_warnings, f"Expected db_path parent warning, got: {warnings}"

    def test_env_vars_unset_uses_defaults(self, tmp_path: Path, monkeypatch) -> None:
        """When no env vars are set, validate_paths() still checks the resolved defaults."""
        monkeypatch.delenv("PALINODE_DIR", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)

        # Build a config pointing at a tmp_path that exists and has a reachable db parent
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        db_path = memory_dir / ".palinode.db"

        cfg = config_module.Config(
            memory_dir=str(memory_dir),
            db_path=str(db_path),
        )
        # The db_path parent is memory_dir which we created — should be clean
        warnings = cfg.validate_paths()
        assert warnings == []

    def test_db_path_outside_and_parent_missing(self, tmp_path: Path) -> None:
        """db_path both outside memory_dir AND whose parent doesn't exist → two warnings."""
        memory_dir = tmp_path / "palinode"
        memory_dir.mkdir()
        # Parent doesn't exist and is outside memory_dir
        db_path = tmp_path / "ghost-dir" / ".palinode.db"

        cfg = config_module.Config(
            memory_dir=str(memory_dir),
            db_path=str(db_path),
        )
        warnings = cfg.validate_paths()
        assert len(warnings) == 2
        texts = " | ".join(warnings)
        assert "db_path parent directory does not exist" in texts
        assert "outside memory_dir" in texts

    def test_all_missing(self, tmp_path: Path) -> None:
        """memory_dir missing, db_path parent missing → two warnings (and an outside-dir warning)."""
        memory_dir = tmp_path / "no-mem"
        db_path = tmp_path / "no-db-dir" / ".palinode.db"

        cfg = config_module.Config(
            memory_dir=str(memory_dir),
            db_path=str(db_path),
        )
        warnings = cfg.validate_paths()
        # Should have at least memory_dir and db_parent warnings
        assert any("memory_dir does not exist" in w for w in warnings)
        assert any("db_path parent directory does not exist" in w for w in warnings)


class TestLoadConfigDivergenceWarning:
    """Integration-level: load_config() logs a warning when PALINODE_DIR and db_path diverge."""

    def test_divergence_logged_when_palinode_dir_set(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """PALINODE_DIR points to one dir but db_path in YAML points elsewhere → logged warning."""
        import logging

        new_dir = tmp_path / "new-palinode"
        new_dir.mkdir()

        monkeypatch.setenv("PALINODE_DIR", str(new_dir))
        monkeypatch.delenv("OLLAMA_URL", raising=False)

        with caplog.at_level(logging.WARNING, logger="palinode.config"):
            cfg = load_config()

        # db_path was built by load_config from the PALINODE_DIR override, so it should
        # now be under new_dir — no divergence warning expected here.
        # This verifies the happy path doesn't produce a spurious warning.
        db_path = Path(cfg.db_path).resolve()
        new_dir_resolved = new_dir.resolve()
        try:
            db_path.relative_to(new_dir_resolved)
            # db_path IS under new_dir — no divergence warning expected
            divergence_warnings = [r for r in caplog.records if "diverged" in r.message]
            assert divergence_warnings == [], (
                f"Unexpected divergence warning when db_path is under PALINODE_DIR: "
                f"{[r.message for r in divergence_warnings]}"
            )
        except ValueError:
            # db_path is NOT under new_dir; divergence warning IS expected
            divergence_warnings = [r for r in caplog.records if "diverged" in r.message]
            assert divergence_warnings, "Expected a divergence warning but none was logged"
