"""Tests for session-end dual-write: daily append + individual file (M0)."""
import os
from unittest import mock

from palinode.core.config import config


def test_session_end_creates_daily_and_individual(tmp_path, monkeypatch):
    """session_end_api should write to daily/ AND create an individual memory file."""
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)

    # Disable git auto-commit for this test
    monkeypatch.setattr(config.git, "auto_commit", False)

    # Mock the description generator to avoid Ollama dependency
    with mock.patch("palinode.api.server._generate_description", return_value="Test session summary"):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="Implemented entity normalization",
            decisions=["Use category-based prefix inference"],
            blockers=["Need to test with live Ollama"],
            project="palinode",
            source="test",
        )
        result = session_end_api(req)

    # Daily file should exist
    daily_file = result["daily_file"]
    daily_path = os.path.join(memory_dir, daily_file)
    assert os.path.exists(daily_path), f"Daily file not found: {daily_path}"
    daily_content = open(daily_path).read()
    assert "Implemented entity normalization" in daily_content

    # Individual file should exist
    individual_file = result.get("individual_file")
    assert individual_file is not None, "individual_file should be set"
    assert os.path.exists(individual_file), f"Individual file not found: {individual_file}"

    # Individual file should have frontmatter with entities
    ind_content = open(individual_file).read()
    assert "project/palinode" in ind_content
    assert "description:" in ind_content


def test_session_end_no_project(tmp_path, monkeypatch):
    """session_end without project should still create individual file as Insight."""
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)

    with mock.patch("palinode.api.server._generate_description", return_value="Quick fix"):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="Quick debugging session",
            source="test",
        )
        result = session_end_api(req)

    individual_file = result.get("individual_file")
    assert individual_file is not None
    assert os.path.exists(individual_file)

    # Should be in insights/ category (Insight type)
    ind_content = open(individual_file).read()
    assert "category: insights" in ind_content
