"""
tests/test_setup_worktree.py — Tests for scripts/setup-worktree.sh

Checks:
  1. --help exits cleanly (exit code 0, prints usage).
  2. Worktree detection: .git is a file → worktree, .git is a dir → main tree.
  3. Script is shellcheck-clean (skipped gracefully if shellcheck absent).

The actual venv-creation path is intentionally NOT tested here:
it is slow, side-effecting (writes .venv-worktree/), and already exercised
by the developer running the script manually per the DEVELOPMENT.md workflow.
The tests below focus on the detection logic by calling the script with a
controlled directory layout built in tmp_path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "setup-worktree.sh"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run(args: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _make_fake_worktree(base: Path) -> Path:
    """
    Build a minimal fake git worktree directory for detection tests.
    Layout:
        base/
            scripts/                    ← script called from here
            setup-worktree.sh           ← symlink to real script (avoids copy)
            .git                        ← file (worktree indicator)
            palinode/                   ← empty package dir (so pip -e . won't blow up)
                __init__.py
            pyproject.toml              ← minimal (detection test only)
    Returns the worktree root (base/).
    """
    worktree_scripts = base / "scripts"
    worktree_scripts.mkdir()
    # Symlink the real script so the shebang / set -e is exercised
    (worktree_scripts / "setup-worktree.sh").symlink_to(SCRIPT)

    # .git as a file → git worktree indicator
    (base / ".git").write_text("gitdir: ../../.git/worktrees/fake-worktree\n")

    # Minimal palinode package so pip install -e . can find a package
    pkg = base / "palinode"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    # Minimal pyproject.toml so pip install -e . doesn't error out on name/version
    (base / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=61.0.0"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        'name = "palinode-test"\n'
        'version = "0.0.1"\n'
        "requires-python = \">=3.11\"\n"
    )

    return base


def _make_fake_main_tree(base: Path) -> Path:
    """
    Build a minimal fake main git working tree directory.
    .git is a directory → main tree indicator.
    """
    worktree_scripts = base / "scripts"
    worktree_scripts.mkdir()
    (worktree_scripts / "setup-worktree.sh").symlink_to(SCRIPT)

    # .git as a directory → main working tree
    (base / ".git").mkdir()

    return base


# ── 1. Script exists and is executable ───────────────────────────────────────


def test_script_exists() -> None:
    assert SCRIPT.exists(), f"scripts/setup-worktree.sh not found at {SCRIPT}"


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


# ── 2. --help exits clean ─────────────────────────────────────────────────────


def test_help_exits_clean() -> None:
    result = _run(["bash", str(SCRIPT), "--help"])
    assert result.returncode == 0, (
        f"--help returned non-zero exit code {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_help_output_mentions_venv_worktree() -> None:
    result = _run(["bash", str(SCRIPT), "--help"])
    assert ".venv-worktree" in result.stdout, (
        "--help output should mention .venv-worktree"
    )


def test_help_output_mentions_activate() -> None:
    result = _run(["bash", str(SCRIPT), "--help"])
    assert "activate" in result.stdout.lower(), (
        "--help output should mention source ... activate"
    )


# ── 3. Worktree detection ─────────────────────────────────────────────────────
#
# We don't run the full venv-creation path (slow, side-effecting), but we DO
# call the script in dry-detect mode: we pass an env that sets PYTHON to a
# nonexistent binary, which causes the script to exit early with an error
# AFTER it has already printed the detection line. That way we can assert on
# the detection output without paying the venv creation cost.
#
# The "PYTHON=nonexistent" trick is only valid because the worktree detection
# happens before the `command -v "$PYTHON_BIN"` check.  If the script order
# changes, update accordingly.


def test_detects_worktree(tmp_path: Path) -> None:
    """Script prints 'Detected: git worktree' when .git is a file."""
    _make_fake_worktree(tmp_path)
    env = {**os.environ, "PYTHON": "__nonexistent_python_for_test__"}
    result = _run(
        ["bash", str(tmp_path / "scripts" / "setup-worktree.sh")],
        cwd=tmp_path,
        env=env,
    )
    combined = result.stdout + result.stderr
    assert "git worktree" in combined, (
        f"Expected 'git worktree' in output. Got:\n{combined}"
    )


def test_detects_main_tree(tmp_path: Path) -> None:
    """Script prints 'main working tree' when .git is a directory."""
    _make_fake_main_tree(tmp_path)
    env = {**os.environ, "PYTHON": "__nonexistent_python_for_test__"}
    result = _run(
        ["bash", str(tmp_path / "scripts" / "setup-worktree.sh")],
        cwd=tmp_path,
        env=env,
    )
    combined = result.stdout + result.stderr
    assert "main working tree" in combined, (
        f"Expected 'main working tree' in output. Got:\n{combined}"
    )


def test_missing_git_dir_exits_nonzero(tmp_path: Path) -> None:
    """Script exits non-zero when .git doesn't exist at all."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "setup-worktree.sh").symlink_to(SCRIPT)
    # no .git at all
    env = {**os.environ, "PYTHON": "__nonexistent_python_for_test__"}
    result = _run(
        ["bash", str(tmp_path / "scripts" / "setup-worktree.sh")],
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit when .git is absent. Got returncode={result.returncode}"
    )
    assert "ERROR" in result.stderr, (
        f"Expected 'ERROR' in stderr. Got:\n{result.stderr}"
    )


# ── 4. shellcheck lint ────────────────────────────────────────────────────────


def test_shellcheck_clean() -> None:
    """Script should be shellcheck-clean. Skipped gracefully if shellcheck is absent."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed — install via `brew install shellcheck` to enable this check")
    result = _run([shellcheck, "--severity=warning", str(SCRIPT)])
    assert result.returncode == 0, (
        f"shellcheck reported issues:\n{result.stdout}\n{result.stderr}"
    )
