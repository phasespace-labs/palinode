"""
Tests for palinode doctor config and process drift checks.

Covers:
  - env_vs_yaml_consistency
  - mcp_config_homes
  - process_env_drift

Uses tmp_path + monkeypatch for env vars and synthetic /proc trees.
No mocking of SQLite, no real subprocess spawns.

Each check is exercised via run_one() so the registration side-effect path
is also covered.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from palinode.core.config import Config
from palinode.diagnostics.runner import run_one
from palinode.diagnostics.types import DoctorContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(memory_dir: Path) -> DoctorContext:
    cfg = Config(
        memory_dir=str(memory_dir),
        db_path=str(memory_dir / ".palinode.db"),
    )
    return DoctorContext(config=cfg)


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write a YAML file using a tiny by-hand serializer (no PyYAML dump dep)."""
    import yaml as _yaml
    path.write_text(_yaml.safe_dump(payload), encoding="utf-8")


def _isolate_yaml_search(monkeypatch: pytest.MonkeyPatch, yaml_dir: Path) -> None:
    """Make `_candidate_yaml_paths` find only files under *yaml_dir*.

    Sets PALINODE_DIR so the second candidate path falls inside *yaml_dir*,
    and stubs the package-locator import so the first candidate falls in a
    non-existent place (so we never read the real repo's yaml).
    """
    # Push PALINODE_DIR so the per-data-dir yaml lookup lands in tmp.
    monkeypatch.setenv("PALINODE_DIR", str(yaml_dir))
    # Make the repo-root candidate point at a spot guaranteed not to exist.
    bogus_pkg = yaml_dir / "__bogus_pkg__" / "palinode"
    bogus_pkg.mkdir(parents=True, exist_ok=True)
    init = bogus_pkg / "__init__.py"
    init.write_text("", encoding="utf-8")

    import palinode  # noqa: F401  — ensure loaded so monkeypatch can replace
    monkeypatch.setattr(
        "palinode.__file__", str(init), raising=True
    )


# ===========================================================================
# env_vs_yaml_consistency
# ===========================================================================

class TestEnvVsYamlConsistency:
    def test_no_yaml_file_passes_with_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        # No yaml present anywhere we look.
        _isolate_yaml_search(monkeypatch, memory_dir)
        # Drop env vars so the check's "no yaml found" branch is the headline.
        monkeypatch.delenv("PALINODE_DIR", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        # We need PALINODE_DIR for the per-dir lookup, but want the file
        # itself to be absent — re-set then check that no yaml exists.
        monkeypatch.setenv("PALINODE_DIR", str(memory_dir))

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "env_vs_yaml_consistency")

        assert result.passed is True
        assert result.severity == "info"
        assert "No palinode.config.yaml" in result.message

    def test_env_and_yaml_agree_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        _isolate_yaml_search(monkeypatch, memory_dir)

        # YAML and env both reference the same memory_dir
        _write_yaml(memory_dir / "palinode.config.yaml", {
            "memory_dir": str(memory_dir),
        })
        monkeypatch.setenv("PALINODE_DIR", str(memory_dir))
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "env_vs_yaml_consistency")

        assert result.passed is True
        assert result.severity == "info"

    def test_env_shadows_non_default_yaml_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # YAML says one thing, env overrides with another.
        env_dir = tmp_path / "palinode-data"
        yaml_dir = tmp_path / "stale-data"
        env_dir.mkdir()
        yaml_dir.mkdir()
        _isolate_yaml_search(monkeypatch, env_dir)

        # YAML is checked under the *env* PALINODE_DIR location
        _write_yaml(env_dir / "palinode.config.yaml", {
            "memory_dir": str(yaml_dir),  # YAML disagrees with env
        })
        monkeypatch.setenv("PALINODE_DIR", str(env_dir))
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

        ctx = _ctx(env_dir)
        result = run_one(ctx, "env_vs_yaml_consistency")

        assert result.passed is False
        assert result.severity == "warn"
        assert "PALINODE_DIR" in (result.remediation or "")
        assert str(env_dir) in (result.remediation or "")
        assert str(yaml_dir) in (result.remediation or "")

    def test_yaml_unset_env_set_does_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # YAML doesn't set memory_dir → no shadow, no warn.
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        _isolate_yaml_search(monkeypatch, memory_dir)

        # YAML present but doesn't define memory_dir
        _write_yaml(memory_dir / "palinode.config.yaml", {
            "search": {"default_limit": 10},
        })
        monkeypatch.setenv("PALINODE_DIR", str(memory_dir))
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "env_vs_yaml_consistency")

        # No drift (YAML didn't set the value)
        assert result.passed is True

    def test_ollama_url_drift_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        _isolate_yaml_search(monkeypatch, memory_dir)

        _write_yaml(memory_dir / "palinode.config.yaml", {
            "embeddings": {
                "primary": {"url": "http://yaml-host:11434"},
            },
        })
        monkeypatch.setenv("PALINODE_DIR", str(memory_dir))
        monkeypatch.setenv("OLLAMA_URL", "http://env-host:11434")
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "env_vs_yaml_consistency")

        assert result.passed is False
        assert result.severity == "warn"
        assert "OLLAMA_URL" in (result.remediation or "")


# ===========================================================================
# mcp_config_homes
# ===========================================================================

class TestMcpConfigHomes:
    def _stub_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
        files: list[tuple[str, Path]],
    ) -> None:
        """Replace _candidate_paths with a fixed list."""
        def _stub() -> list[tuple[str, Path]]:
            return files
        monkeypatch.setattr(
            "palinode.diagnostics.checks.mcp_config_check._candidate_paths",
            _stub,
        )

    def test_no_palinode_entries_passes_with_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only an empty config file
        f = tmp_path / "claude.json"
        f.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        self._stub_paths(monkeypatch, [("Test config", f)])

        ctx = _ctx(tmp_path)
        result = run_one(ctx, "mcp_config_homes")

        assert result.passed is True
        assert result.severity == "info"
        assert "No MCP config files contain" in result.message

    def test_single_palinode_entry_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "claude.json"
        f.write_text(json.dumps({
            "mcpServers": {
                "palinode": {"command": "palinode-mcp"},
            },
        }), encoding="utf-8")
        self._stub_paths(monkeypatch, [("Only", f)])

        ctx = _ctx(tmp_path)
        result = run_one(ctx, "mcp_config_homes")

        assert result.passed is True
        assert result.severity == "info"

    def test_multiple_agreeing_entries_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        entry = {"command": "palinode-mcp", "args": ["--port", "6340"]}
        a.write_text(json.dumps({"mcpServers": {"palinode": entry}}), encoding="utf-8")
        b.write_text(json.dumps({"mcpServers": {"palinode": entry}}), encoding="utf-8")
        self._stub_paths(monkeypatch, [("A", a), ("B", b)])

        ctx = _ctx(tmp_path)
        result = run_one(ctx, "mcp_config_homes")

        assert result.passed is True
        assert result.severity == "info"
        assert "all agree" in result.message

    def test_multiple_diverging_entries_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps({
            "mcpServers": {"palinode": {"command": "palinode-mcp"}},
        }), encoding="utf-8")
        b.write_text(json.dumps({
            "mcpServers": {"palinode": {"url": "http://localhost:6340"}},
        }), encoding="utf-8")
        self._stub_paths(monkeypatch, [("A", a), ("B", b)])

        ctx = _ctx(tmp_path)
        result = run_one(ctx, "mcp_config_homes")

        assert result.passed is False
        assert result.severity == "warn"
        assert "divergent" in result.message
        # Remediation should reference both files in the diff
        assert str(a) in (result.remediation or "")
        assert str(b) in (result.remediation or "")


# ===========================================================================
# process_env_drift
# ===========================================================================

@pytest.fixture
def linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the check to take the Linux branch on any host platform."""
    monkeypatch.setattr(
        "palinode.diagnostics.checks.process_env.platform.system",
        lambda: "Linux",
    )


def _make_proc_tree(root: Path, procs: list[dict[str, Any]]) -> Path:
    """Build a synthetic /proc tree at *root*.

    Each entry must have keys:
      pid: int
      cmdline: str
      environ: dict[str, str]  (or omit to make environ unreadable)
    """
    proc_dir = root / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)
    for p in procs:
        d = proc_dir / str(p["pid"])
        d.mkdir(parents=True, exist_ok=True)

        # cmdline: NUL-separated argv, NUL-terminated
        cmd = p["cmdline"]
        argv = cmd.split() if isinstance(cmd, str) else cmd
        cmd_bytes = b"\x00".join(a.encode() for a in argv) + b"\x00"
        (d / "cmdline").write_bytes(cmd_bytes)

        if "environ" in p:
            env = p["environ"]
            env_bytes = b"".join(
                f"{k}={v}".encode() + b"\x00" for k, v in env.items()
            )
            (d / "environ").write_bytes(env_bytes)
        # else: no environ file → unreadable case
    return proc_dir


def _patch_proc_root(monkeypatch: pytest.MonkeyPatch, proc_root: Path) -> None:
    monkeypatch.setattr(
        "palinode.diagnostics.checks.process_env._proc_root",
        lambda: proc_root,
    )


class TestProcessEnvDrift:
    def test_macos_returns_info_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "palinode.diagnostics.checks.process_env.platform.system",
            lambda: "Darwin",
        )
        ctx = _ctx(tmp_path)
        result = run_one(ctx, "process_env_drift")
        assert result.passed is True
        assert result.severity == "info"
        assert "Skipped" in result.message
        assert "ps -E" in (result.remediation or "")

    def test_no_palinode_processes_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        proc_root = _make_proc_tree(tmp_path, [
            {"pid": 1234, "cmdline": "bash", "environ": {}},
        ])
        _patch_proc_root(monkeypatch, proc_root)
        ctx = _ctx(tmp_path / "mem")
        (tmp_path / "mem").mkdir()
        result = run_one(ctx, "process_env_drift")
        assert result.passed is True
        assert result.severity == "info"
        assert "No palinode" in result.message

    def test_singleton_in_sync_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        proc_root = _make_proc_tree(tmp_path, [
            {
                "pid": 5001,
                "cmdline": "/usr/bin/python /usr/bin/palinode-watcher",
                "environ": {"PALINODE_DIR": str(memory_dir)},
            },
        ])
        _patch_proc_root(monkeypatch, proc_root)

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "process_env_drift")
        assert result.passed is True
        assert result.severity == "info"
        assert "matching configured value" in result.message

    def test_singleton_with_drift_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        # Watcher started before rename and is still on
        # old PALINODE_DIR.
        new_dir = tmp_path / "palinode-data"
        old_dir = tmp_path / "stale-data"
        new_dir.mkdir()
        old_dir.mkdir()

        proc_root = _make_proc_tree(tmp_path, [
            {
                "pid": 610112,
                "cmdline": "/usr/bin/python /usr/local/bin/palinode-watcher",
                "environ": {"PALINODE_DIR": str(old_dir)},
            },
        ])
        _patch_proc_root(monkeypatch, proc_root)

        ctx = _ctx(new_dir)
        result = run_one(ctx, "process_env_drift")

        assert result.passed is False
        assert result.severity == "warn"
        assert "stale PALINODE_DIR" in result.message
        rem = result.remediation or ""
        assert "610112" in rem
        assert str(old_dir) in rem
        assert str(new_dir) in rem
        assert "systemctl" in rem
        # Heuristic note must be visible to the operator
        assert "Heuristic" in rem or "heuristic" in rem

    def test_multiple_of_same_kind_treated_as_intentional(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        # Two watchers with different PALINODE_DIR — operator-deliberate.
        new_dir = tmp_path / "prod-data"
        test_dir = tmp_path / "test-data"
        new_dir.mkdir()
        test_dir.mkdir()

        proc_root = _make_proc_tree(tmp_path, [
            {
                "pid": 5001,
                "cmdline": "palinode-watcher --prod",
                "environ": {"PALINODE_DIR": str(new_dir)},
            },
            {
                "pid": 5002,
                "cmdline": "palinode-watcher --test",
                "environ": {"PALINODE_DIR": str(test_dir)},
            },
        ])
        _patch_proc_root(monkeypatch, proc_root)

        ctx = _ctx(new_dir)
        result = run_one(ctx, "process_env_drift")

        # The mismatched test-data watcher is reported but at info severity.
        # No `warn` because two watchers of the same kind → intentional.
        assert result.severity == "info"
        assert result.passed is True
        # The mismatched one is mentioned in remediation
        assert str(test_dir) in (result.remediation or "")

    def test_multiple_in_sync_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        proc_root = _make_proc_tree(tmp_path, [
            {
                "pid": 5001,
                "cmdline": "palinode-api",
                "environ": {"PALINODE_DIR": str(memory_dir)},
            },
            {
                "pid": 5002,
                "cmdline": "palinode-mcp",
                "environ": {"PALINODE_DIR": str(memory_dir)},
            },
            {
                "pid": 5003,
                "cmdline": "palinode-watcher",
                "environ": {"PALINODE_DIR": str(memory_dir)},
            },
        ])
        _patch_proc_root(monkeypatch, proc_root)

        ctx = _ctx(memory_dir)
        result = run_one(ctx, "process_env_drift")
        assert result.passed is True
        assert result.severity == "info"

    def test_process_without_palinode_dir_env_does_not_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        # Process inherits no PALINODE_DIR override → can't say it's stale.
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        proc_root = _make_proc_tree(tmp_path, [
            {
                "pid": 5001,
                "cmdline": "palinode-api",
                "environ": {"PATH": "/usr/bin"},  # no PALINODE_DIR
            },
        ])
        _patch_proc_root(monkeypatch, proc_root)
        ctx = _ctx(memory_dir)
        result = run_one(ctx, "process_env_drift")
        assert result.passed is True

    def test_unreadable_environ_is_info_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        # Process exists with no environ file → permission-denied surrogate.
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        proc_root = _make_proc_tree(tmp_path, [
            {
                "pid": 5001,
                "cmdline": "palinode-watcher",
                # no "environ" key → file not created
            },
        ])
        _patch_proc_root(monkeypatch, proc_root)
        ctx = _ctx(memory_dir)
        result = run_one(ctx, "process_env_drift")
        assert result.severity == "info"
        assert "unreadable" in (result.remediation or "").lower()

    def test_skips_self_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_only: None
    ) -> None:
        memory_dir = tmp_path / "mem"
        memory_dir.mkdir()
        # Plant a "drift" process at our own PID — should be skipped.
        my_pid = os.getpid()
        proc_root = _make_proc_tree(tmp_path, [
            {
                "pid": my_pid,
                "cmdline": "palinode-watcher",
                "environ": {"PALINODE_DIR": "/some/wrong/dir"},
            },
        ])
        _patch_proc_root(monkeypatch, proc_root)
        ctx = _ctx(memory_dir)
        result = run_one(ctx, "process_env_drift")
        # Self skipped → no palinode procs found
        assert result.passed is True
        assert "No palinode" in result.message
