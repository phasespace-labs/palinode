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
