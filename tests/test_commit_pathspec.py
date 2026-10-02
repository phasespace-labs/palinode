"""Auto-commits commit only their own paths, never the whole git index.

Both commit sites used to run ``git commit -m <message>`` with no pathspec,
so whatever an operator had already staged in the store rode along:

* :func:`git_tools.try_commit_memory_files` (every save, archive, rollback,
  consolidation write) committed it under a ``palinode auto-save`` message;
* :func:`git_tools.push` committed it under ``auto-commit before push`` and
  then pushed it to the remote, including non-``.md`` files its filter was
  meant to exclude.

These tests drive real git repositories under ``tmp_path`` (and a real bare
remote for push). Only the embedder and the content scanner are stubbed on the
save path; subprocess, git and SQLite are real.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from palinode.core import git_tools, store
from palinode.core.config import config
from palinode.core.save import save_memory

_FAKE_VECTOR = [0.01] * 1024


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def _ok(cwd: Path, *args: str) -> str:
    r = _git(cwd, *args)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout


def _head_files(repo: Path, rev: str = "HEAD") -> list[str]:
    """Paths the commit at ``rev`` changed relative to its parent."""
    out = _ok(repo, "show", "--name-only", "--format=", rev)
    return sorted(line for line in out.splitlines() if line)


def _staged(repo: Path) -> list[str]:
    out = _ok(repo, "diff", "--cached", "--name-status")
    return sorted(line for line in out.splitlines() if line)


def _tree(repo: Path, rev: str = "HEAD") -> list[str]:
    out = _ok(repo, "ls-tree", "-r", "--name-only", rev)
    return sorted(line for line in out.splitlines() if line)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A healthy memory repo on ``main`` with one base commit, wired into
    config with auto_commit on and no ambient git identity or config."""
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(mem))
    monkeypatch.setattr(config, "db_path", str(mem / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", False)

    store.init_db()
    _ok(mem, "init", "-q", "-b", "main")
    _ok(mem, "config", "user.name", "Palinode Tests")
    _ok(mem, "config", "user.email", "tests@example.com")
    (mem / ".gitignore").write_text(".palinode.db*\n", encoding="utf-8")
    (mem / "insights").mkdir()
    (mem / "insights" / "base.md").write_text("base\n", encoding="utf-8")
    (mem / "README.md").write_text("store readme\n", encoding="utf-8")
    _ok(mem, "add", "-A")
    _ok(mem, "commit", "-q", "-m", "base")
    return mem


@pytest.fixture()
def remote(repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _ok(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _ok(repo, "remote", "add", "origin", str(bare))
    _ok(repo, "push", "-q", "origin", "main")
    return bare


def _stage_unrelated(repo: Path) -> None:
    """An operator's in-progress work: a new non-.md file and a config edit."""
    (repo / "unrelated-notes.txt").write_text("hand edit\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".palinode.db*\n*.tmp\n", encoding="utf-8")
    _ok(repo, "add", "--", "unrelated-notes.txt", ".gitignore")


_UNRELATED = ["A\tunrelated-notes.txt", "M\t.gitignore"]


# ---------------------------------------------------------------------------
# commit_memory_files: the save-path choke point
# ---------------------------------------------------------------------------


class TestCommitMemoryFiles:

    def test_save_commits_only_the_memory(self, repo):
        _stage_unrelated(repo)
        with (
            patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
            patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR),
        ):
            result = save_memory(
                content="Probe insight.", type="Insight", slug="repro-staged",
            )

        assert result["git_committed"] is True
        assert _head_files(repo) == ["insights/repro-staged.md"]
        assert _staged(repo) == _UNRELATED

    def test_staged_unrelated_md_is_left_staged(self, repo):
        (repo / "README.md").write_text("readme edit in progress\n", encoding="utf-8")
        _ok(repo, "add", "--", "README.md")
        target = repo / "insights" / "new.md"
        target.write_text("new\n", encoding="utf-8")

        assert git_tools.commit_memory_files([str(target)], "palinode: m") is True
        assert _head_files(repo) == ["insights/new.md"]
        assert _staged(repo) == ["M\tREADME.md"]

    def test_deletion_is_committed(self, repo):
        _stage_unrelated(repo)
        target = repo / "insights" / "base.md"
        target.unlink()

        assert git_tools.commit_memory_files([str(target)], "palinode: delete") is True
        assert "insights/base.md" not in _tree(repo)
        assert _ok(repo, "show", "--name-status", "--format=", "HEAD").split() == [
            "D", "insights/base.md",
        ]
        assert _staged(repo) == _UNRELATED
        assert _ok(repo, "status", "--porcelain", "--", "insights").strip() == ""

    def test_move_lands_as_one_rename_commit(self, repo):
        _stage_unrelated(repo)
        (repo / "archive").mkdir()
        src = repo / "insights" / "base.md"
        dst = repo / "archive" / "base.md"
        git_tools.move_memory_file(str(src), str(dst))

        assert git_tools.commit_memory_files([str(src), str(dst)], "palinode: move") is True
        status = _ok(repo, "show", "-M", "--name-status", "--format=", "HEAD").split()
        assert status[0].startswith("R")
        assert status[1:] == ["insights/base.md", "archive/base.md"]
        tree = _tree(repo)
        assert "archive/base.md" in tree and "insights/base.md" not in tree
        assert _staged(repo) == _UNRELATED

    def test_nothing_to_commit_is_still_success(self, repo):
        _stage_unrelated(repo)
        head = _ok(repo, "rev-parse", "HEAD")
        base = repo / "insights" / "base.md"

        assert git_tools.commit_memory_files([str(base)], "palinode: noop") is True
        assert _ok(repo, "rev-parse", "HEAD") == head
        assert _staged(repo) == _UNRELATED

    def test_rollback_commits_only_the_rolled_back_file(self, repo):
        base = repo / "insights" / "base.md"
        base.write_text("edited\n", encoding="utf-8")
        assert git_tools.commit_memory_files([str(base)], "palinode: edit") is True
        _stage_unrelated(repo)

        git_tools.rollback("insights/base.md", commit="HEAD~1")

        assert base.read_text(encoding="utf-8") == "base\n"
        assert _head_files(repo) == ["insights/base.md"]
        assert _ok(repo, "log", "-1", "--format=%s").startswith("palinode: rollback")
        assert _staged(repo) == _UNRELATED


# ---------------------------------------------------------------------------
# push(): the auto-commit before push
# ---------------------------------------------------------------------------


class TestPush:

    def test_staged_non_md_is_neither_committed_nor_pushed(self, repo, remote):
        _stage_unrelated(repo)
        (repo / "README.md").write_text("readme edit\n", encoding="utf-8")
        new = repo / "insights" / "dirty.md"
        new.write_text("dirty\n", encoding="utf-8")

        out = git_tools.push()

        assert "successfully" in out
        assert _head_files(repo) == ["README.md", "insights/dirty.md"]
        assert _ok(remote, "rev-parse", "main") == _ok(repo, "rev-parse", "HEAD")
        pushed = _tree(remote, "main")
        assert "unrelated-notes.txt" not in pushed
        assert "insights/dirty.md" in pushed
        assert _ok(remote, "show", "main:.gitignore") == ".palinode.db*\n"
        assert _staged(repo) == _UNRELATED

    def test_only_non_md_staged_makes_no_commit(self, repo, remote):
        _stage_unrelated(repo)
        head = _ok(repo, "rev-parse", "HEAD")

        git_tools.push()

        assert _ok(repo, "rev-parse", "HEAD") == head
        assert "unrelated-notes.txt" not in _tree(remote, "main")
        assert _staged(repo) == _UNRELATED

    def test_staged_rename_lands_whole(self, repo, remote):
        _stage_unrelated(repo)
        (repo / "archive").mkdir()
        _ok(repo, "mv", "insights/base.md", "archive/base.md")

        git_tools.push()

        tree = _tree(remote, "main")
        assert "archive/base.md" in tree
        assert "insights/base.md" not in tree
        # Neither side of the rename is left behind in the index.
        assert _staged(repo) == _UNRELATED

    def test_staged_deletion_is_committed(self, repo, remote):
        _stage_unrelated(repo)
        _ok(repo, "rm", "-q", "--", "insights/base.md")

        git_tools.push()

        assert "insights/base.md" not in _tree(remote, "main")
        assert _staged(repo) == _UNRELATED

    def test_first_line_unstaged_modification_is_parsed(self, repo, remote):
        # " M README.md" sorts first; stripping the status output ate its
        # leading space and turned the path into "EADME.md", failing the add.
        (repo / "README.md").write_text("readme edit\n", encoding="utf-8")
        (repo / "insights" / "dirty.md").write_text("dirty\n", encoding="utf-8")

        git_tools.push()

        assert _head_files(repo) == ["README.md", "insights/dirty.md"]
        assert _ok(remote, "rev-parse", "main") == _ok(repo, "rev-parse", "HEAD")
        assert _ok(repo, "status", "--porcelain") == ""
