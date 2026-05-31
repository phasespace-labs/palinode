"""Tests for session-end dual-write: daily append + individual file (M0)."""
import os
from unittest import mock

import pytest

from palinode.core.config import config


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point config.db_path at a per-test DB so session-end dedup never queries
    the real ~/palinode database.

    These tests monkeypatch ``memory_dir`` but historically not ``db_path``;
    ``_check_session_end_dedup`` reads recent embeddings from ``db_path``. On a
    machine where real Ollama is reachable (so embeds succeed), the dedup could
    match a prior identical save in the real DB and skip the individual file,
    failing these tests non-deterministically. Isolating ``db_path`` here keeps
    the dedup window empty per test.
    """
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))


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
    # #405: the auto-description is no longer written inline on save — it is
    # deferred to the watcher-driven /generate-summaries backfill, so the file
    # is born without a description field (the mock above is never invoked on
    # the save hot path now). The description lands later, out of band.
    assert "description:" not in ind_content


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


# ---- #145: structured session-end metadata ------------------------------


def test_project_from_cwd_helper():
    """The helper that auto-derives a project slug from a cwd path."""
    from palinode.api.server import _project_from_cwd

    assert _project_from_cwd("/Users/alice/Code/my-project") == "my-project"
    assert _project_from_cwd("/Users/alice/Code/my-project/") == "my-project"
    assert _project_from_cwd("/Users/alice/Code/My Project") == "my-project"
    assert _project_from_cwd("") is None
    assert _project_from_cwd(None) is None
    # Trailing slash + odd chars
    assert _project_from_cwd("/tmp/!!!") is None


def test_session_end_with_full_metadata(tmp_path, monkeypatch):
    """All six metadata fields should land in the daily note AND in the
    indexed file's frontmatter (#145)."""
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)

    with mock.patch("palinode.api.server._generate_description", return_value="Metadata test"):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="Shipped #145 metadata",
            project="palinode",
            source="test",
            harness="claude-code",
            cwd="/Users/alice/Code/my-project",
            model="claude-opus-4-7",
            trigger="wrap-slash",
            session_id="abc-123",
            duration_seconds=4837,
        )
        result = session_end_api(req)

    daily_path = os.path.join(memory_dir, result["daily_file"])
    daily = open(daily_path).read()
    # Daily note should carry all six metadata lines
    assert "**Harness:** claude-code" in daily
    assert "**CWD:** /Users/alice/Code/my-project" in daily
    assert "**Model:** claude-opus-4-7" in daily
    assert "**Trigger:** wrap-slash" in daily
    assert "**Session ID:** abc-123" in daily
    assert "**Duration:** 4837s" in daily

    ind_path = result["individual_file"]
    ind = open(ind_path).read()
    # Frontmatter should carry the metadata as structured fields
    assert "harness: claude-code" in ind
    assert "model: claude-opus-4-7" in ind
    assert "trigger: wrap-slash" in ind
    assert "session_id: abc-123" in ind
    assert "duration_seconds: 4837" in ind


def test_session_end_without_metadata_keeps_daily_clean(tmp_path, monkeypatch):
    """No metadata fields supplied ⇒ no Harness/CWD/etc. lines in the
    daily note. Old callers must not see a regression."""
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)

    with mock.patch("palinode.api.server._generate_description", return_value="Plain"):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(summary="No metadata", source="test")
        result = session_end_api(req)

    daily = open(os.path.join(memory_dir, result["daily_file"])).read()
    for marker in ("**Harness:**", "**CWD:**", "**Model:**",
                   "**Trigger:**", "**Session ID:**", "**Duration:**"):
        assert marker not in daily, f"unexpected metadata line: {marker}"


def test_session_end_auto_derives_project_from_cwd(tmp_path, monkeypatch):
    """If `project` is omitted but `cwd` is provided, the project slug
    should be derived from cwd's basename so status rolls up correctly."""
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)

    # Pre-create a status file so the append path runs and we can verify
    # the auto-derived project name lands there
    projects_dir = os.path.join(memory_dir, "projects")
    os.makedirs(projects_dir)
    status_path = os.path.join(projects_dir, "my-project-status.md")
    with open(status_path, "w") as f:
        f.write("# my-project status\n")

    with mock.patch("palinode.api.server._generate_description", return_value="Auto-derive"):
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(
            summary="cwd auto-derivation works",
            cwd="/Users/alice/Code/my-project",
            source="test",
        )
        result = session_end_api(req)

    # status_file should be set and reference the auto-derived slug
    assert result.get("status_file") == "projects/my-project-status.md"
    status = open(status_path).read()
    assert "cwd auto-derivation works" in status

    # Individual file's slug should embed the auto-derived project too
    ind_path = result["individual_file"]
    assert "session-end-" in os.path.basename(ind_path)
    assert "my-project" in os.path.basename(ind_path)
