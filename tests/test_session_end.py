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
    # the auto-description is no longer written inline on save — it is
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


# structured session-end metadata ------------------------------


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


# session_end push parameter (ship the note in one call) -----------


def _run_session_end_with_push(tmp_path, monkeypatch, *, push, auto_push,
                               push_result="Pushed to origin/main successfully."):
    """Drive session_end_api with git mocked out, returning (result, push_mock).

    subprocess.run is mocked so the commit step no-ops on a non-repo tmp dir;
    git_tools.push is mocked so we assert the push DECISION and response wiring
    without needing a real remote.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", auto_push)

    with mock.patch("palinode.api.server._generate_description", return_value="x"), \
         mock.patch("palinode.api.server.subprocess.run"), \
         mock.patch("palinode.api.server.git_tools.push", return_value=push_result) as mpush:
        from palinode.api.server import session_end_api, SessionEndRequest

        req = SessionEndRequest(summary="ship it", source="test", push=push)
        result = session_end_api(req)
    return result, mpush


def test_session_end_push_true_ships_note(tmp_path, monkeypatch):
    """push=True invokes the push and reports pushed=True, even with auto_push off (#378)."""
    result, mpush = _run_session_end_with_push(
        tmp_path, monkeypatch, push=True, auto_push=False)
    assert mpush.called, "push=True must invoke git_tools.push (#378)"
    assert result["pushed"] is True
    assert result["committed"] is True


def test_session_end_no_push_by_default(tmp_path, monkeypatch):
    """push omitted + auto_push off ⇒ committed but NOT pushed (legacy default)."""
    result, mpush = _run_session_end_with_push(
        tmp_path, monkeypatch, push=None, auto_push=False)
    assert not mpush.called, "default flow must not push when auto_push is off"
    assert result["pushed"] is False
    assert result["committed"] is True


def test_session_end_push_none_falls_back_to_auto_push(tmp_path, monkeypatch):
    """push=None defers to config.git.auto_push (True ⇒ push)."""
    result, mpush = _run_session_end_with_push(
        tmp_path, monkeypatch, push=None, auto_push=True)
    assert mpush.called, "push=None must honor auto_push=True"
    assert result["pushed"] is True


def test_session_end_push_false_overrides_auto_push(tmp_path, monkeypatch):
    """push=False suppresses the push even when auto_push is on."""
    result, mpush = _run_session_end_with_push(
        tmp_path, monkeypatch, push=False, auto_push=True)
    assert not mpush.called, "push=False must override auto_push=True"
    assert result["pushed"] is False


def test_session_end_push_failure_reported_honestly(tmp_path, monkeypatch):
    """A failed push (no remote/conflict) reports pushed=False so the wrap can
    tell the user the note is committed-but-not-pushed (#378)."""
    result, mpush = _run_session_end_with_push(
        tmp_path, monkeypatch, push=True, auto_push=False,
        push_result="Push failed: no configured push destination")
    assert mpush.called
    assert result["pushed"] is False
    assert result["committed"] is True


def test_session_end_date_is_wallclock_not_existing_files(tmp_path, monkeypatch):
    """Regression guard (#577): the daily filename and the `## Session End —` stamp
    must come from wall-clock now() at call time — never from the latest existing
    daily file or any cached/persisted date. (#577 reported a stale stamp that
    predated the server's own start; current code is wall-clock-correct and this
    pins it so a future refactor can't reintroduce file/state-derived dating.)"""
    from datetime import datetime, timezone

    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)

    # A pre-existing STALE daily note. A "latest-existing-file" bug would append
    # here and stamp the old date; wall-clock logic must ignore it entirely.
    daily_dir = os.path.join(memory_dir, "daily")
    os.makedirs(daily_dir, exist_ok=True)
    stale_path = os.path.join(daily_dir, "2020-01-01.md")
    stale_original = "## Session End — 2020-01-01T00:00:00Z\nold entry\n"
    with open(stale_path, "w") as f:
        f.write(stale_original)

    # Freeze wall-clock to a fixed instant, distinct from both today and the stale file.
    frozen = datetime(2030, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    import palinode.api.routers.session as session_mod
    monkeypatch.setattr(session_mod, "_utc_now", lambda: frozen)

    # The daily write (the thing under test) happens before dedup/individual-save;
    # mock those two so the test is hermetic (no Ollama embed / network).
    with mock.patch("palinode.api.routers.session._check_session_end_dedup", return_value=(None, None)), \
         mock.patch("palinode.api.routers.session.save_api", return_value={"file_path": None}):
        from palinode.api.server import SessionEndRequest, session_end_api

        result = session_end_api(SessionEndRequest(summary="wall-clock check", source="test"))

    # Date derives strictly from now(), not the stale file or any cache.
    assert result["daily_file"] == "daily/2030-06-15.md"
    assert os.path.exists(os.path.join(memory_dir, "daily", "2030-06-15.md"))
    assert "## Session End — 2030-06-15T12:00:00Z" in result["entry"]
    # The pre-existing stale file must be untouched (never appended to).
    assert open(stale_path).read() == stale_original
