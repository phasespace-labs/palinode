"""Tests for `palinode worktree-reconcile` — stale dead-PID worktree cleanup (#448).

Real git + tmp_path (no mocking). A bare repo serves as `origin` so the
upstream check has something real to resolve against.
"""
from __future__ import annotations

import os
import subprocess

import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.worktree import reconcile, _apply, _parse_porcelain, _pid_alive


def _run(args, cwd):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True
    )


DEAD_PID = 999_999  # not a live process (os.kill → ProcessLookupError)


@pytest.fixture()
def repo(tmp_path):
    """A git repo with a bare `origin` and a first commit on `main`."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True,
                   capture_output=True)
    root = tmp_path / "repo"
    _run(["init", "-b", "main", str(root)], cwd=tmp_path)
    (root / "README.md").write_text("hi\n")
    _run(["add", "-A"], cwd=root)
    _run(["commit", "-m", "init"], cwd=root)
    _run(["remote", "add", "origin", str(origin)], cwd=root)
    _run(["push", "-u", "origin", "main"], cwd=root)
    (root / ".claude" / "worktrees").mkdir(parents=True)
    return root


def _add_worktree(root, name, branch, *, lock_pid, push=True, dirty=False):
    path = root / ".claude" / "worktrees" / name
    _run(["worktree", "add", "-b", branch, str(path), "main"], cwd=root)
    if push:
        _run(["push", "-u", "origin", branch], cwd=path)
    if dirty:
        (path / "scratch.txt").write_text("uncommitted\n")
    _run(["worktree", "lock", "--reason", f"claude session pid {lock_pid}", str(path)],
         cwd=root)
    return str(path)


# ---------------------------------------------------------------------------
# Unit: pure helpers
# ---------------------------------------------------------------------------


def test_pid_alive_self_true_and_dead_false():
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(DEAD_PID) is False


def test_parse_porcelain_extracts_locked_and_branch():
    text = (
        "worktree /r\n\n"
        "worktree /r/.claude/worktrees/a\nbranch refs/heads/feat/a\nlocked pid 5\n"
    )
    entries = _parse_porcelain(text)
    assert entries[1]["path"].endswith("/a")
    assert entries[1]["branch"] == "refs/heads/feat/a"
    assert entries[1]["locked"] and "pid 5" in entries[1]["lock_reason"]


# ---------------------------------------------------------------------------
# Verdicts across the four cases
# ---------------------------------------------------------------------------


def test_dead_clean_upstream_is_removed(repo):
    dead = _add_worktree(repo, "dead", "feat/dead", lock_pid=DEAD_PID)
    verdicts = {v.path: v for v in reconcile(str(repo))}
    assert verdicts[dead].action == "remove"
    assert verdicts[dead].pid_alive is False and verdicts[dead].clean and verdicts[dead].has_upstream


def test_alive_lock_is_skipped(repo):
    alive = _add_worktree(repo, "alive", "feat/alive", lock_pid=os.getpid())
    v = {x.path: x for x in reconcile(str(repo))}[alive]
    assert v.action == "skip" and "alive" in v.reason


def test_dirty_tree_is_skipped(repo):
    dirty = _add_worktree(repo, "dirty", "feat/dirty", lock_pid=DEAD_PID, dirty=True)
    v = {x.path: x for x in reconcile(str(repo))}[dirty]
    assert v.action == "skip" and "dirty" in v.reason


def test_no_upstream_is_skipped(repo):
    nou = _add_worktree(repo, "nou", "feat/nou", lock_pid=DEAD_PID, push=False)
    v = {x.path: x for x in reconcile(str(repo))}[nou]
    assert v.action == "skip" and "upstream" in v.reason


# ---------------------------------------------------------------------------
# Apply: only the safe one is removed; branch/commits survive
# ---------------------------------------------------------------------------


def test_apply_removes_only_dead_clean_upstream(repo):
    dead = _add_worktree(repo, "dead", "feat/dead", lock_pid=DEAD_PID)
    alive = _add_worktree(repo, "alive", "feat/alive", lock_pid=os.getpid())
    verdicts = reconcile(str(repo))
    removed = _apply(str(repo), verdicts)

    assert removed == [dead]
    assert not os.path.exists(dead), "removed worktree dir should be gone"
    assert os.path.exists(alive), "alive worktree must be untouched"
    # The branch of the removed worktree survives (remove drops only the dir).
    branches = _run(["branch", "--list", "feat/dead"], cwd=repo).stdout
    assert "feat/dead" in branches


# ---------------------------------------------------------------------------
# CLI: dry-run is the default and mutates nothing
# ---------------------------------------------------------------------------


def test_cli_dry_run_default_removes_nothing(repo, monkeypatch):
    dead = _add_worktree(repo, "dead", "feat/dead", lock_pid=DEAD_PID)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(main, ["worktree-reconcile"])
    assert result.exit_code == 0, result.output
    assert "WOULD REMOVE" in result.output
    assert os.path.exists(dead), "dry-run must not remove anything"


def test_cli_execute_removes(repo, monkeypatch):
    dead = _add_worktree(repo, "dead", "feat/dead", lock_pid=DEAD_PID)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(main, ["worktree-reconcile", "--execute"])
    assert result.exit_code == 0, result.output
    assert not os.path.exists(dead)
