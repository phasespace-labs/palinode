import pytest
from unittest.mock import patch, MagicMock
from palinode.core import git_tools

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

def test_diff_shows_changes():
    with patch("palinode.core.git_tools._run_git") as mock_run:
        mock_res = MagicMock()
        mock_res.stdout = "1 commit\n+ appended line"
        mock_run.return_value = mock_res
        
        res = git_tools.diff(days=7)
        assert "appended line" in res

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
        # First call: git log
        log_res = MagicMock()
        log_res.stdout = "abc1234|2026-04-10T12:00:00+00:00|palinode: update file\n"
        # Second call: git diff --stat
        stat_res = MagicMock()
        stat_res.stdout = " some/file.md | 3 ++-\n 1 file changed, 2 insertions(+), 1 deletion(-)\n"
        mock_run.side_effect = [log_res, stat_res]

        with patch("os.path.exists", return_value=True):
            result = git_tools.history("some/file.md", limit=10)
            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["hash"] == "abc1234"
            assert result[0]["date"] == "2026-04-10T12:00:00+00:00"
            assert result[0]["message"] == "palinode: update file"
            assert "changed" in result[0]["stats"]


def test_history_rejects_path_traversal():
    with pytest.raises(ValueError, match="Path traversal rejected"):
        git_tools.history("../../etc/passwd")


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


# ---- #337: git-persistence failures must reach the log, not just the return ----


def test_blame_failure_logs_warning(caplog):
    """A failed git blame logs a WARNING (#337) — not only a returned string."""
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
    """A failed rollback checkout is operator-critical → ERROR (#337)."""
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
    """A failed push logs a WARNING with the returncode/stderr (#337)."""
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
