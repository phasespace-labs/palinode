"""``git_tools.history`` must report stats and diffs across a rename.

``history`` documents that it "uses ``--follow`` to track renames", and the
``log`` call honours that. The per-commit ``diff --stat`` and ``show`` that
used to fill in ``stats`` and ``diff`` did not: each passed the file's
*current* path with no ``--follow``, so for any commit older than a rename
git was asked about a path that did not exist yet. Three of the four commits
below were wrong or blank.

The repository's root commit was blank for a second, unrelated reason:
``{sha}^..{sha}`` has no parent to compare against.

Real git repositories throughout. A mocked ``_run_git`` returns whatever it
was handed and cannot see either defect, which is why the mocked test stayed
green while the behaviour was wrong.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from palinode.core import git_tools
from palinode.core.config import config


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture()
def renamed_store(tmp_path, monkeypatch) -> str:
    """A memory dir whose only file was committed, edited, renamed, edited."""
    repo = str(tmp_path)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    monkeypatch.setattr(config, "memory_dir", repo)

    before = os.path.join(repo, "before.md")
    with open(before, "w", encoding="utf-8") as handle:
        handle.write("root line\n")
    _git(repo, "add", "before.md")
    _git(repo, "commit", "-qm", "add before.md")

    with open(before, "w", encoding="utf-8") as handle:
        handle.write("root line\npre-rename line\n")
    _git(repo, "commit", "-qam", "edit before the rename")

    _git(repo, "mv", "before.md", "after.md")
    _git(repo, "commit", "-qm", "rename to after.md")

    after = os.path.join(repo, "after.md")
    with open(after, "w", encoding="utf-8") as handle:
        handle.write("root line\npre-rename line\npost-rename line\n")
    _git(repo, "commit", "-qam", "edit after the rename")
    return repo


def test_history_reports_stats_for_commits_older_than_the_rename(renamed_store):
    commits = git_tools.history("after.md")

    assert [entry["message"] for entry in commits] == [
        "edit after the rename",
        "rename to after.md",
        "edit before the rename",
        "add before.md",
    ]
    # Newest-first, so index 2 is the edit made while the file was still
    # before.md. It was blank before: `diff --stat <sha>^..<sha> -- after.md`
    # names a path that did not exist at that commit.
    assert "changed" in commits[2]["stats"]


def test_history_reports_stats_for_the_root_commit(renamed_store):
    commits = git_tools.history("after.md")

    # The repository's first commit has no parent, so `{sha}^..{sha}` used to
    # fail outright and leave this blank whether or not a rename was involved.
    assert "changed" in commits[3]["stats"]


def test_history_full_detail_carries_the_pre_rename_change(renamed_store):
    commits = git_tools.history("after.md", detail="full")

    diff = commits[2]["diff"]
    # Assert on a hunk, not on emptiness: `git show <sha> -- after.md` still
    # printed the commit header for a path that did not exist yet, so the old
    # code produced a non-empty diff with nothing in it. A non-emptiness check
    # passes today and pins nothing.
    assert "@@" in diff
    assert "+pre-rename line" in diff


def test_history_issues_one_git_call_per_request(renamed_store, monkeypatch):
    """One walk, not one spawn per commit."""
    calls: list[tuple[str, ...]] = []
    real_run_git = git_tools._run_git

    def recording_run_git(*args: str, **kwargs):
        calls.append(args)
        return real_run_git(*args, **kwargs)

    monkeypatch.setattr(git_tools, "_run_git", recording_run_git)

    assert len(git_tools.history("after.md", detail="full")) == 4
    assert len(calls) == 1
    assert calls[0][0] == "log"


def test_history_does_not_read_file_content_as_a_stat_line(tmp_path, monkeypatch):
    """A memory file may contain a line that reads exactly like a shortstat.

    Under ``detail="full"`` that content travels in the same stream as the real
    summary. It has to reach the parser as an unchanged *context* line to be
    ambiguous at all -- an added line carries a "+" and cannot match -- so the
    lookalike is committed first and something else is changed on top of it.
    """
    repo = str(tmp_path)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    monkeypatch.setattr(config, "memory_dir", repo)

    lookalike = " 99 files changed, 99 insertions(+)\n"
    note = os.path.join(repo, "note.md")
    with open(note, "w", encoding="utf-8") as handle:
        handle.write("quoting git output:\n" + lookalike)
    _git(repo, "add", "note.md")
    _git(repo, "commit", "-qm", "add note.md")

    with open(note, "w", encoding="utf-8") as handle:
        handle.write("quoting git output:\n" + lookalike + "and one more line\n")
    _git(repo, "commit", "-qam", "append a line below the lookalike")

    commits = git_tools.history("note.md", detail="full")

    # The lookalike is context in this commit's patch, so it is offered to the
    # parser exactly as a shortstat line would be.
    assert lookalike.rstrip("\n") in commits[0]["diff"]
    assert commits[0]["stats"] == "1 file changed, 1 insertion(+)"


def test_history_survives_color_ui_always(tmp_path, monkeypatch):
    """``color.ui=always`` paints "diff --git", which the stat guard keys on.

    git honours that setting down a pipe, so without ``--no-color`` the guard
    never latches, and the stat-lookalike context line then overwrites the real
    summary. Both conditions are needed: the paint alone corrupts nothing.
    """
    repo = str(tmp_path)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "color.ui", "always")
    monkeypatch.setattr(config, "memory_dir", repo)

    lookalike = " 99 files changed, 99 insertions(+)\n"
    note = os.path.join(repo, "note.md")
    with open(note, "w", encoding="utf-8") as handle:
        handle.write("quoting git output:\n" + lookalike)
    _git(repo, "add", "note.md")
    _git(repo, "commit", "-qm", "add note.md")

    with open(note, "w", encoding="utf-8") as handle:
        handle.write("quoting git output:\n" + lookalike + "and one more line\n")
    _git(repo, "commit", "-qam", "append a line below the lookalike")

    commits = git_tools.history("note.md", detail="full")

    assert commits[0]["stats"] == "1 file changed, 1 insertion(+)"


def test_history_reads_a_short_abbreviated_hash(renamed_store):
    """``core.abbrev`` goes down to 4, and ``%h`` honours it."""
    _git(renamed_store, "config", "core.abbrev", "4")

    commits = git_tools.history("after.md")

    assert len(commits) == 4
    assert "changed" in commits[0]["stats"]


def test_history_keeps_a_pipe_in_the_commit_message(tmp_path, monkeypatch):
    """``%s`` is the last field, so a message may contain the delimiter."""
    repo = str(tmp_path)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    monkeypatch.setattr(config, "memory_dir", repo)

    note = os.path.join(repo, "piped.md")
    with open(note, "w", encoding="utf-8") as handle:
        handle.write("body\n")
    _git(repo, "add", "piped.md")
    _git(repo, "commit", "-qm", "save: a | b")

    commits = git_tools.history("piped.md")

    assert [entry["message"] for entry in commits] == ["save: a | b"]


def test_history_honours_the_limit(renamed_store):
    commits = git_tools.history("after.md", limit=2)

    assert [entry["message"] for entry in commits] == [
        "edit after the rename",
        "rename to after.md",
    ]
