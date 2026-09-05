import pytest
from unittest.mock import patch, MagicMock
from palinode.core import git_tools
from palinode.core.config import config

def test_blame_attribution():
    with patch("palinode.core.git_tools._run_git") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "2024-01-01 Line 1 content\n2024-01-02 Line 2 content\n"
        mock_run.return_value = mock_res
        
        with patch("os.path.exists", return_value=True):
            res = git_tools.blame("some/file.md")
            assert "2024-01-01 Line 1" in res
            assert "2024-01-02 Line 2" in res

# ``diff`` is covered by tests/test_git_tools_diff.py, against real git
# repositories. The mocked test that used to live here returned one canned
# stdout for every `_run_git` call, so it asserted only that the stdout reached
# the output string — it could not see which arguments were handed to git. That
# is where the lookback-window defect lived (a `--reverse -1` base selection
# that always resolved to HEAD), so the mock reported green the whole time the
# tool was answering "nothing changed" over a store being written to daily.

def test_rollback_creates_new_commit():
    with patch("palinode.core.git_tools._run_git") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_run.return_value = mock_res
        
        with patch("os.path.exists", return_value=True):
            res = git_tools.rollback("some/file.md", commit="HEAD~1", dry_run=False)
            assert "Rolled back some/file.md to HEAD~1" in res
            
            calls = mock_run.call_args_list
            assert any(c[0][0] == "checkout" for c in calls)
            assert any(c[0][0] == "commit" for c in calls)

def test_history_returns_structured_data():
    with patch("palinode.core.git_tools._run_git") as mock_run:
        # One call now: the log carries its own --shortstat summary.
        log_res = MagicMock()
        log_res.stdout = (
            "abc1234|2026-04-10T12:00:00+00:00|palinode: update file\n"
            "\n"
            " 1 file changed, 2 insertions(+), 1 deletion(-)\n"
        )
        mock_run.return_value = log_res

        with patch("os.path.exists", return_value=True):
            result = git_tools.history("some/file.md", limit=10)
            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["hash"] == "abc1234"
            assert result[0]["date"] == "2026-04-10T12:00:00+00:00"
            assert result[0]["message"] == "palinode: update file"
            assert "changed" in result[0]["stats"]


def test_history_rejects_path_traversal():
    # The shared guard's message is deliberately generic (never echoes the
    # offending input) — it used to be
    # "Path traversal rejected: ../../etc/passwd".
    with pytest.raises(ValueError, match="^Invalid path$") as exc_info:
        git_tools.history("../../etc/passwd")
    assert isinstance(exc_info.value, git_tools.PathTraversalError)
    assert exc_info.value.malformed is False


def test_git_operations_on_non_git_fail_gracefully():
    with patch("palinode.core.git_tools._run_git") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 128
        mock_res.stderr = "fatal: not a git repository"
        mock_run.return_value = mock_res
        
        with patch("os.path.exists", return_value=True):
            res = git_tools.blame("some/file.md")
            assert "Git blame failed" in res
            assert "fatal: not a git repository" in res


# git-persistence failures must reach the log, not just the return ----


def test_blame_failure_logs_warning(caplog):
    """A failed git blame logs a WARNING — not only a returned string."""
    import logging as _logging
    with patch("palinode.core.git_tools._run_git") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 128
        mock_res.stderr = "fatal: not a git repository"
        mock_run.return_value = mock_res
        with patch("os.path.exists", return_value=True):
            with caplog.at_level(_logging.WARNING, logger="palinode.git_tools"):
                git_tools.blame("some/file.md")
    assert any(
        r.levelno == _logging.WARNING and "git blame failed" in r.message
        for r in caplog.records
    )


def test_rollback_checkout_failure_logs_error(caplog):
    """A failed rollback checkout is operator-critical → ERROR."""
    import logging as _logging
    with patch("palinode.core.git_tools._run_git") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stderr = "error: pathspec did not match"
        mock_run.return_value = mock_res
        with patch("os.path.exists", return_value=True):
            with caplog.at_level(_logging.ERROR, logger="palinode.git_tools"):
                res = git_tools.rollback("some/file.md", commit="HEAD~1", dry_run=False)
    assert "Rollback failed" in res
    assert any(
        r.levelno == _logging.ERROR and "rollback checkout failed" in r.message
        for r in caplog.records
    )


def test_push_failure_logs_warning(caplog):
    """A failed push logs a WARNING with the returncode/stderr."""
    import logging as _logging

    def fake_run(*args, **kwargs):
        res = MagicMock()
        if args and args[0] == "status":
            res.stdout = ""  # nothing to auto-commit
            res.returncode = 0
        elif args and args[0] == "push":
            res.returncode = 1
            res.stderr = "fatal: No configured push destination."
        else:
            res.returncode = 0
            res.stdout = ""
            res.stderr = ""
        return res

    with patch("palinode.core.git_tools._run_git", side_effect=fake_run):
        with caplog.at_level(_logging.WARNING, logger="palinode.git_tools"):
            res = git_tools.push()
    assert "Push failed" in res
    assert any(
        r.levelno == _logging.WARNING and "git push failed" in r.message
        for r in caplog.records
    )


def test_write_memory_file_skips_directory_fsync_on_windows(tmp_path, monkeypatch):
    """Windows cannot open a directory as a file descriptor for fsync."""
    # `write_memory_file` validates its target against `config.memory_dir`
    # before writing, so a test writing into a bare `tmp_path` must point
    # memory_dir at that same tmp_path or the write is (correctly) rejected
    # as an out-of-tree path.
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    target = tmp_path / "memory.md"
    # Fake the platform through git_tools' own probe rather than flipping the
    # real `os.name`: the latter is global, and the path guard that now runs
    # first uses pathlib, which would try to build a WindowsPath on POSIX.
    monkeypatch.setattr(git_tools, "_is_windows", lambda: True)
    with patch.object(git_tools, "_fsync_directory") as fsync_directory:
        git_tools.write_memory_file(str(target), "UTF-8 content: “quotes”\n")

    assert target.read_text(encoding="utf-8") == "UTF-8 content: “quotes”\n"
    fsync_directory.assert_not_called()


def test_move_memory_file_skips_directory_fsync_on_windows(tmp_path, monkeypatch):
    """Windows cannot open a directory as a file descriptor for fsync."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    source = tmp_path / "daily.md"
    archive = tmp_path / "archive"
    destination = archive / "daily.md"
    source.write_text("daily note\n", encoding="utf-8")
    archive.mkdir()
    monkeypatch.setattr(git_tools, "_is_windows", lambda: True)

    with patch.object(git_tools, "_fsync_directory") as fsync_directory:
        git_tools.move_memory_file(str(source), str(destination))

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "daily note\n"
    fsync_directory.assert_not_called()


def test_write_memory_file_overwrite_works_without_fchmod(tmp_path, monkeypatch):
    """Python 3.11/3.12 Windows needs the path-based chmod fallback."""
    # See the note in the sibling test above: the write target must live under
    # `config.memory_dir` for `write_memory_file`'s path guard to accept it.
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    target = tmp_path / "memory.md"
    target.write_text("old\n", encoding="utf-8")
    monkeypatch.delattr(git_tools.os, "fchmod", raising=False)

    git_tools.write_memory_file(str(target), "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
