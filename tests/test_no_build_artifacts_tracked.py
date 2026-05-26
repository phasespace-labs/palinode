"""Regression guard for #295: build/ and dist/ must not be tracked in git.

These directories are rebuilt fresh by the publish pipeline on tag push,
so tracking them only causes staleness, bloat, and accidental public leaks
(they were the largest leak category caught by the 2026-05-01 scrub audit
that triggered the #292 belt-and-suspenders path scrub).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _git_ls_files(*patterns: str) -> list[str]:
    """Return tracked file paths matching the given pathspecs (empty if none)."""
    result = subprocess.run(
        ["git", "ls-files", "--", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_build_directory_not_tracked():
    tracked = _git_ls_files("build/")
    assert not tracked, (
        "build/ contains tracked files — these should be in .gitignore "
        "(#295). Tracked: " + ", ".join(tracked[:5]) + ("..." if len(tracked) > 5 else "")
    )


def test_dist_directory_not_tracked():
    tracked = _git_ls_files("dist/")
    assert not tracked, (
        "dist/ contains tracked files — these should be in .gitignore "
        "(#295). Tracked: " + ", ".join(tracked[:5]) + ("..." if len(tracked) > 5 else "")
    )


def test_egg_info_not_tracked():
    tracked = _git_ls_files("*.egg-info/")
    assert not tracked, (
        "*.egg-info/ contains tracked files — these should be in .gitignore "
        "(#295). Tracked: " + ", ".join(tracked[:5])
    )


def test_gitignore_excludes_build_artifacts():
    """Belt-and-suspenders: even if someone re-adds build/ files, .gitignore
    catches them on the next commit attempt. Keep both layers honest.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert "build/" in gitignore
    assert "dist/" in gitignore
    assert "*.egg-info/" in gitignore
