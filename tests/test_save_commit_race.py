"""Real save and git contention, including a competing HEAD update."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from palinode.core import git_tools, store
from palinode.core.config import config
from palinode.core.save import save_memory


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", False)
    monkeypatch.setattr(store, "scan_memory_content", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr("palinode.core.embedder.embed", lambda *a, **kw: [0.01] * 1024)
    store.init_db()
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Save Race Test")
    _git(tmp_path, "config", "user.email", "race@example.invalid")
    (tmp_path / ".gitignore").write_text(".palinode.db*\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def _head_race_hook(repo: Path, *, once: bool) -> Path:
    """Advance HEAD in a separate git process after commit reads its parent.

    Plumbing avoids the index lock held by the outer commit's hook. This
    deterministically creates the same stale-parent failure as another writer
    advancing HEAD between a path commit's index preparation and ref update.
    """
    hook = repo / ".git" / "hooks" / "pre-commit"
    guard = "[ -e .git/raced ] && exit 0\ntouch .git/raced\n" if once else ""
    hook.write_text(
        "#!/bin/sh\nset -eu\n" + guard
        + "old=$(git rev-parse HEAD)\n"
        + "tree=$(git rev-parse 'HEAD^{tree}')\n"
        + "new=$(git commit-tree \"$tree\" -p \"$old\" -m 'competing writer')\n"
        + "git update-ref HEAD \"$new\" \"$old\"\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def test_many_concurrent_saves_with_head_race(repo: Path, caplog) -> None:
    _head_race_hook(repo, once=True)
    count = 32
    start = threading.Barrier(count)

    def save(i: int) -> dict:
        start.wait(timeout=10)
        return save_memory(content=f"Concurrent memory {i}.", type="Insight", slug=f"race-{i}")

    with caplog.at_level("DEBUG", logger="palinode.git_tools"):
        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(save, range(count)))

    assert all(r["git_committed"] for r in results), [r.get("git_error") for r in results]
    assert _git(repo, "status", "--porcelain") == ""
    for i in range(count):
        assert _git(repo, "log", "--format=%s", "--", f"insights/race-{i}.md").strip()
    assert any("cannot lock ref" in r.getMessage() for r in caplog.records)


def test_exhausted_head_retries_preserve_path_for_next_commit(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(git_tools, "_INDEX_LOCK_BACKOFF", (0.001,))
    hook = _head_race_hook(repo, once=False)
    result = save_memory(content="Keep this uncommitted content.", type="Insight", slug="recover")
    assert result["git_committed"] is False
    assert "cannot lock ref" in result["git_error"]
    assert "Keep this uncommitted content." in Path(result["file_path"]).read_text()
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == str(2 + git_tools._INDEX_LOCK_RETRIES)

    # An unrelated commit must not sweep up the failed save, even when staged.
    unrelated = repo / "unrelated.md"
    unrelated.write_text("Other mutation.\n")
    hook.unlink()
    assert git_tools.commit_memory_file(str(unrelated), "other mutation")
    assert not _git(repo, "log", "--format=%s", "--", "insights/recover.md").strip()
    assert git_tools.commit_memory_file(result["file_path"], "recover failed save")
    assert "Keep this uncommitted content." in _git(repo, "show", "HEAD:insights/recover.md")
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("writer", ["save", "push"])
def test_head_retry_commits_only_owned_paths(repo: Path, tmp_path: Path, writer: str) -> None:
    remote = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    # The remote is inside tmp_path, so exclude it from working-tree status.
    _git(repo, "config", "core.excludesFile", str(repo / ".git" / "test-excludes"))
    (repo / ".git" / "test-excludes").write_text("remote.git/\n")
    unrelated = repo / "operator.txt"
    unrelated.write_text("Operator work.\n")
    _git(repo, "add", "operator.txt")
    _head_race_hook(repo, once=True)

    if writer == "save":
        (repo / "operator.md").write_text("Other staged memory.\n")
        _git(repo, "add", "operator.md")
        result = save_memory(content="Owned memory.", type="Insight", slug="owned")
        assert result["git_committed"] is True
        owned = "insights/owned.md"
        assert _git(repo, "diff", "--cached", "--name-only").splitlines() == [
            "operator.md", "operator.txt",
        ]
    else:
        (repo / "owned.md").write_text("Owned memory.\n")
        assert git_tools.push().startswith("Pushed to origin/main successfully.")
        owned = "owned.md"
        assert _git(repo, "diff", "--cached", "--name-only").splitlines() == ["operator.txt"]
        assert _git(remote, "show", "main:owned.md") == "Owned memory.\n"

    assert _git(repo, "show", "--format=", "--name-only", "HEAD").splitlines() == [owned]


def test_push_and_save_share_commit_serialization(repo: Path, monkeypatch) -> None:
    """Observe real git calls; pause the first commit to expose any overlap.

    The wrapper only controls scheduling and measures concurrent commit calls.
    All commands still run against the real repository, without fake results.
    """
    original = git_tools._run_git
    save_in_commit = threading.Event()
    push_started = threading.Event()
    observations_lock = threading.Lock()
    active = 0
    maximum = 0

    def observed_git(*args: str, **kwargs):
        nonlocal active, maximum
        if args[0] != "commit":
            return original(*args, **kwargs)
        with observations_lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if "auto-save:" in args[2]:
                save_in_commit.set()
                assert push_started.wait(timeout=10)
                time.sleep(0.1)
            return original(*args, **kwargs)
        finally:
            with observations_lock:
                active -= 1

    monkeypatch.setattr(git_tools, "_run_git", observed_git)

    def push() -> str:
        assert save_in_commit.wait(timeout=10)
        (repo / "push-owned.md").write_text("Pre-push memory.\n")
        push_started.set()
        return git_tools.push()

    with ThreadPoolExecutor(max_workers=2) as pool:
        push_future = pool.submit(push)
        save_future = pool.submit(save_memory, content="Save memory.", type="Insight", slug="parallel")
        assert save_future.result()["git_committed"] is True
        # No remote is needed here: the pre-push commit must still land.
        assert push_future.result().startswith("Push failed:")

    assert maximum == 1, "pre-push commit raced the save commit"
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "log", "--format=%s", "--", "push-owned.md").strip()
